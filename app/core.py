from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import re
import unicodedata
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable


LogFn = Callable[[str], None]
def _noop(_: str) -> None:
    pass


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def resolve_binary(explicit: str | None, executable: str, common: Iterable[str] = ()) -> str:
    candidates: list[str] = []
    if explicit:
        candidates.append(explicit)
    candidates.extend(common)
    for candidate in candidates:
        if candidate and os.path.isfile(candidate):
            return os.path.abspath(candidate)
    found = shutil.which(executable)
    if found:
        return found
    raise FileNotFoundError(f"{executable} မတွေ့ပါ။ config.json / PATH ကိုစစ်ပါ။")


class EdgeTTSEngine:
    """Local Edge-TTS adapter with resilient direct/system-route retries.

    Edge TTS uses a WebSocket service. ``NoAudioReceived`` is often a
    transport/route failure rather than a Smart Sync failure. Generate the
    MP3 in-process so the adapter can classify errors, back off, and try both
    a proxy-bypassed route and the inherited system/proxy route.
    """

    _PROXY_KEYS = (
        "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
        "http_proxy", "https_proxy", "all_proxy",
        "NO_PROXY", "no_proxy",
    )
    _env_lock = threading.Lock()

    def __init__(self, *, ffmpeg: str, work_dir: Path, rate: str = "+0%", pitch: str = "+0Hz", log: LogFn = _noop) -> None:
        import importlib.util
        if importlib.util.find_spec("edge_tts") is None:
            raise RuntimeError("edge-tts မတွေ့ပါ။ pip install -U edge-tts လုပ်ပြီး ပြန်စမ်းပါ။")
        self.ffmpeg = ffmpeg
        self.work_dir = Path(work_dir)
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.rate = str(rate or "+0%").strip()
        self.pitch = str(pitch or "+0Hz").strip()
        self.log = log
        self.VOICE_DATABASE: dict[str, dict[str, Any]] = {}
        self._counter = 0
        try:
            from importlib.metadata import version as _pkg_version
            edge_ver = _pkg_version("edge-tts")
        except Exception:
            edge_ver = "unknown"
        self.log(f"✅ Edge TTS READY — version={edge_ver}, rate={self.rate}, pitch={self.pitch}")

    @staticmethod
    def _clean_text(text: str) -> str:
        value = unicodedata.normalize("NFC", str(text or ""))
        value = "".join(ch if ch in "\n\t" or ord(ch) >= 32 else " " for ch in value)
        value = re.sub(r"\s+", " ", value).strip()
        if not value:
            raise RuntimeError("Edge TTS text is empty after cleanup.")
        if not any(ch.isalnum() for ch in value):
            raise RuntimeError(
                f"Edge TTS text contains no speakable letters/numbers: {value!r}. "
                "This is a translation placeholder, not a network failure."
            )
        return value

    async def _save_mp3_async(self, text: str, voice: str, mp3: Path) -> None:
        import edge_tts
        communicate = edge_tts.Communicate(
            text,
            voice=str(voice),
            rate=self.rate,
            pitch=self.pitch,
        )
        await communicate.save(str(mp3))

    def _synthesize_once(self, text: str, voice: str, mp3: Path, *, direct: bool) -> None:
        # edge_tts/aiohttp respects environment/system proxy settings. Some
        # setups need a proxy for Gemini while Edge TTS succeeds only direct.
        with self._env_lock:
            backup = {key: os.environ.get(key) for key in self._PROXY_KEYS}
            try:
                if direct:
                    for key in self._PROXY_KEYS:
                        os.environ.pop(key, None)
                    os.environ["NO_PROXY"] = "*"
                    os.environ["no_proxy"] = "*"
                asyncio.run(self._save_mp3_async(text, voice, mp3))
            finally:
                for key, old_value in backup.items():
                    if old_value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = old_value

    def generate_channel99_voice(self, text: str, voice: str):
        clean_text = self._clean_text(text)
        self._counter += 1
        stem = f"edge_{self._counter:05d}_{hashlib.sha1(clean_text.encode('utf-8')).hexdigest()[:10]}"
        mp3 = self.work_dir / f"{stem}.mp3"
        wav = self.work_dir / f"{stem}.wav"
        mp3.unlink(missing_ok=True)
        wav.unlink(missing_ok=True)

        # The previous 0.5-second retry was too short for transient WebSocket
        # failures. Try direct twice, then inherited system/proxy twice.
        routes = [
            (True, "direct", 0.0),
            (True, "direct", 1.5),
            (False, "system/proxy", 2.5),
            (False, "system/proxy", 5.0),
        ]
        errors: list[str] = []
        for attempt, (direct, route_name, delay) in enumerate(routes, start=1):
            if delay:
                time.sleep(delay)
            mp3.unlink(missing_ok=True)
            try:
                self.log(
                    f"🌐 Edge TTS request {attempt}/{len(routes)} — {route_name} route | "
                    f"voice={voice} | chars={len(clean_text)}"
                )
                self._synthesize_once(clean_text, voice, mp3, direct=direct)
                if not mp3.is_file() or mp3.stat().st_size < 256:
                    raise RuntimeError("Edge TTS returned no usable MP3 data.")
                if attempt > 1:
                    self.log(f"✅ Edge TTS recovered on {route_name} route (attempt {attempt}/{len(routes)})")
                break
            except Exception as exc:
                name = type(exc).__name__
                msg = str(exc).strip()
                errors.append(f"{route_name}#{attempt}: {name}: {msg}")
                self.log(f"⚠️ Edge TTS {route_name} attempt {attempt}/{len(routes)} failed: {name}: {msg}")
        else:
            preview = clean_text[:160].replace("\n", " ")
            raise RuntimeError(
                "Edge TTS service returned no audio after direct + system/proxy retries. "
                f"voice={voice}, rate={self.rate}, pitch={self.pitch}, chars={len(clean_text)}, "
                f"text_preview={preview!r}. Last errors: " + " | ".join(errors[-4:])
            )

        conv = subprocess.run(
            [self.ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", str(mp3),
             "-ac", "1", "-ar", "24000", "-c:a", "pcm_s16le", str(wav)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        try:
            mp3.unlink(missing_ok=True)
        except Exception:
            pass
        if conv.returncode != 0 or not wav.is_file():
            raise RuntimeError("Edge TTS WAV conversion failed: " + (conv.stderr or "")[-2000:])
        return str(wav)


def _seconds(value: Any) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value or "").strip()
    if not text:
        return 0.0
    try:
        return float(text)
    except ValueError:
        pass
    total = 0.0
    for part in text.split(":"):
        total = total * 60.0 + float(part)
    return total


def normalize_segments(plan: dict[str, Any]) -> list[dict[str, Any]]:
    raw = plan.get("segments")
    if not isinstance(raw, list) or not raw:
        raise ValueError("smart_sync_plan.json ထဲမှာ timestamp-resolved 'segments' မရှိပါ။")
    out: list[dict[str, Any]] = []
    for source_index, item in enumerate(raw):
        if not isinstance(item, dict):
            continue
        text = str(item.get("narration", item.get("text", ""))).strip()
        start = _seconds(item.get("video_start", item.get("start", 0.0)))
        end = _seconds(item.get("video_end", item.get("end", 0.0)))
        if not text or end <= start + 0.01:
            continue
        out.append({
            "segment_id": len(out) + 1,
            "source_index": source_index,
            "video_start": round(max(0.0, start), 3),
            "video_end": round(max(0.0, end), 3),
            "narration": text,
        })
    if not out:
        raise ValueError("အသုံးပြုလို့ရတဲ့ narration + timestamp segments မရှိပါ။")
    out.sort(key=lambda x: (x["video_start"], x["video_end"]))
    for i, seg in enumerate(out, 1):
        seg["segment_id"] = i
    return out


def _stamp(seconds: float) -> str:
    ms = max(0, int(round(seconds * 1000.0)))
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}h{m:02d}m{s:02d}s{ms:03d}"


def segment_basename(seg: dict[str, Any]) -> str:
    return f"S{int(seg['segment_id']):04d}_{_stamp(float(seg['video_start']))}__{_stamp(float(seg['video_end']))}"


def segment_fingerprint(seg: dict[str, Any], voice: str) -> str:
    payload = {
        "voice": voice,
        "video_start": seg["video_start"],
        "video_end": seg["video_end"],
        "narration": seg["narration"],
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def probe_duration(ffprobe: str, path: Path) -> float:
    result = subprocess.run(
        [ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {result.stderr[-2000:]}")
    return float(result.stdout.strip())


def _concat_list_line(path: Path) -> str:
    p = str(path.resolve()).replace("\\", "/")
    p = p.replace("'", "'\\''")
    return f"file '{p}'"


def trim_edge_silence(
    source: Path,
    target: Path,
    *,
    keep_lead: float = 0.05,
    keep_tail: float = 0.20,
    min_duration: float = 0.0,
    threshold_db: float = -50.0,
) -> tuple[Path, float]:
    """Short pauses: cut the silence Edge TTS puts before (~0.2 s) and after (~0.8 s) each clip.

    Joined clips then pause keep_lead + keep_tail (~0.25 s, like a sentence break) instead of ~1 s.
    A clip is never made shorter than min_duration: below source window / max_video_speed Smart
    Sync would have to cut the picture, so there a longer pause is kept. Pauses inside the clip
    are untouched and the source WAV (the TTS cache) is never changed.
    Returns (file to use, its duration).
    """
    import wave

    import numpy as np

    with wave.open(str(source), "rb") as reader:
        params = reader.getparams()
        raw = reader.readframes(params.nframes)
    rate = int(params.framerate or 1)
    if params.sampwidth != 2 or not params.nframes:
        return source, params.nframes / float(rate)
    samples = np.frombuffer(raw, dtype="<i2").reshape(-1, params.nchannels)
    duration = len(samples) / rate
    window = max(1, rate // 100)  # 10 ms
    count = len(samples) // window
    if not count:
        return source, duration
    blocks = samples[: count * window].astype(np.float32).reshape(count, window, params.nchannels)
    level = np.sqrt(np.mean(blocks ** 2, axis=(1, 2))) / 32768.0
    loud = np.nonzero(level > 10 ** (threshold_db / 20.0))[0]
    if not loud.size:
        return source, duration
    start = max(0.0, loud[0] * window / rate - keep_lead)
    end = min(duration, (loud[-1] + 1) * window / rate + keep_tail)
    if end - start < min_duration:
        end = min(duration, start + min_duration)
        start = max(0.0, end - min_duration)
    first, last = int(round(start * rate)), int(round(end * rate))
    if first <= 0 and last >= len(samples):
        return source, duration
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_file() and target.stat().st_mtime >= source.stat().st_mtime:
        try:
            with wave.open(str(target), "rb") as existing:
                if existing.getnframes() == last - first:
                    return target, (last - first) / rate
        except (wave.Error, EOFError):
            pass
    partial = target.with_name(target.stem + ".partial.wav")
    with wave.open(str(partial), "wb") as writer:
        writer.setnchannels(params.nchannels)
        writer.setsampwidth(2)
        writer.setframerate(rate)
        writer.writeframes(samples[first:last].tobytes())
    partial.replace(target)
    return target, (last - first) / rate


def concat_audio(ffmpeg: str, parts: list[Path], output: Path, work_dir: Path) -> None:
    if not parts:
        raise ValueError("Audio parts မရှိပါ။")
    list_file = work_dir / "audio_parts.txt"
    list_file.write_text("\n".join(_concat_list_line(p) for p in parts) + "\n", encoding="utf-8")
    cmd = [
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-f", "concat", "-safe", "0", "-i", str(list_file),
        "-ar", "48000", "-ac", "2", "-c:a", "pcm_s16le", str(output),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        raise RuntimeError(f"Audio concat failed:\n{result.stderr[-4000:]}")


def _sync_filter(
    source_duration: float,
    target_duration: float,
    *,
    max_video_speed: float = 1.25,
    max_video_slow: float = 1.35,
    smooth_freeze_fallback: bool = False,
    max_freeze_hold: float = 0.75,
) -> tuple[str, dict[str, float]]:
    """Recap-style hybrid timing plan for one timestamp segment.

    - Normal case: exact setpts to the actual TTS duration.
    - Audio much shorter: cap speed-up at max_video_speed, then trim.
    - Audio much longer: cap slow-down at max_video_slow.  When the requested
      stretch exceeds that visual limit, the caller may distribute short
      freeze holds across the segment instead of making motion unnaturally slow.
    """
    if source_duration <= 0.01 or target_duration <= 0.01:
        raise ValueError("Smart Sync duration invalid")
    max_speed = max(1.0, float(max_video_speed))
    max_slow = max(1.0, float(max_video_slow))
    desired = target_duration / source_duration
    min_factor = 1.0 / max_speed

    if desired < min_factor:
        applied = min_factor
        adjusted = source_duration * applied
        return (
            f"trim=duration={source_duration:.6f},setpts={applied:.9f}*(PTS-STARTPTS),trim=duration={target_duration:.6f},setpts=PTS-STARTPTS,fps=30",
            {
                "desired_factor": desired,
                "applied_factor": applied,
                "source_duration": source_duration,
                "target_duration": target_duration,
                "freeze_duration": 0.0,
                "freeze_count": 0.0,
                "hybrid_used": 0.0,
            },
        )

    if desired <= max_slow or not smooth_freeze_fallback:
        # With fallback disabled this deliberately behaves like V0.2.4 exact sync.
        applied = desired
        return (
            f"trim=duration={source_duration:.6f},setpts={applied:.9f}*(PTS-STARTPTS),fps=30",
            {
                "desired_factor": desired,
                "applied_factor": applied,
                "source_duration": source_duration,
                "target_duration": target_duration,
                "freeze_duration": 0.0,
                "freeze_count": 0.0,
                "hybrid_used": 0.0,
            },
        )

    applied = max_slow
    adjusted = source_duration * applied
    freeze_duration = max(0.0, target_duration - adjusted)
    # The filter string is not used for the hybrid case; render_video_part builds
    # a split/concat graph so the hold time is distributed across the segment.
    return (
        "",
        {
            "desired_factor": desired,
            "applied_factor": applied,
            "source_duration": source_duration,
            "target_duration": target_duration,
            "freeze_duration": freeze_duration,
            "freeze_count": 0.0,
            "hybrid_used": 1.0,
            "max_freeze_hold": max(0.15, float(max_freeze_hold)),
        },
    )


OUTPUT_RATIOS = ("16:9", "9:16", "1:1", "Original")
FRAME_FILLS = ("blur", "black", "crop")  # fit on a blurred copy / fit with black bars / crop to fill
OUTPUT_LONG_SIDE = 1920


def _even(value: float) -> int:
    return max(2, int(round(float(value) / 2.0)) * 2)


def probe_video_size(ffprobe: str, path: Path | str) -> tuple[int, int]:
    """Width/height of the first video stream as ffmpeg decodes it (phone rotation applied)."""
    result = subprocess.run(
        [ffprobe, "-v", "error", "-select_streams", "v:0", "-show_streams", "-of", "json", str(path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {result.stderr[-1200:]}")
    streams = (json.loads(result.stdout or "{}").get("streams") or [{}])
    video = streams[0] if streams else {}
    width, height = int(video.get("width") or 0), int(video.get("height") or 0)
    rotation = 0
    for side in video.get("side_data_list") or []:
        if "rotation" in side:
            try:
                rotation = int(round(float(side["rotation"])))
            except (TypeError, ValueError):
                pass
    if not rotation:
        try:
            rotation = int(round(float((video.get("tags") or {}).get("rotate", 0) or 0)))
        except (TypeError, ValueError):
            rotation = 0
    if abs(rotation) % 180 == 90:
        width, height = height, width
    return width, height


def output_canvas(ratio: str, source_w: int = 0, source_h: int = 0) -> tuple[int, int]:
    """Final frame size: 16:9 = 1920x1080, 9:16 = 1080x1920, 1:1 = 1080x1080, Original = the
    source shape (long side at most 1920)."""
    if ratio == "9:16":
        return 1080, 1920
    if ratio == "1:1":
        return 1080, 1080
    if ratio == "Original" and source_w > 0 and source_h > 0:
        scale = min(1.0, OUTPUT_LONG_SIDE / max(source_w, source_h))
        return _even(source_w * scale), _even(source_h * scale)
    return 1920, 1080


def frame_layout(ratio: str, fill: str, source_w: int, source_h: int,
                 canvas: tuple[int, int] | None = None) -> dict[str, Any]:
    """Everything _visual_filters needs; `canvas` overrides the size (the preview draws smaller)."""
    ratio = ratio if ratio in OUTPUT_RATIOS else "16:9"
    cw, ch = canvas or output_canvas(ratio, source_w, source_h)
    return {"ratio": ratio, "fill": fill if fill in FRAME_FILLS else "blur",
            "cw": _even(cw), "ch": _even(ch), "sw": int(source_w or 0), "sh": int(source_h or 0)}


def _visual_filters(mirror_video: bool, zoom_factor: float,
                    layout: dict[str, Any] | None = None) -> tuple[str, float]:
    """Flip + zoom + placement on the output frame, as one filtergraph string.

    Crop (and any source already of the frame's shape) fills the frame and cuts the overflow —
    exactly the old 16:9 behaviour. Blur / black keep the whole picture visible, centred, with a
    blurred copy of it or black bars in the free room. Zoom always zooms the picture itself.
    """
    zoom = max(1.0, min(float(zoom_factor), 1.25))
    lay = layout or {"fill": "crop", "cw": 1920, "ch": 1080, "sw": 0, "sh": 0}
    cw, ch = int(lay["cw"]), int(lay["ch"])
    sw, sh = int(lay.get("sw") or 0) or cw, int(lay.get("sh") or 0) or ch
    head = "hflip," if mirror_video else ""
    if lay.get("fill") == "crop" or abs(sw / sh - cw / ch) < 0.01:
        fill_w, fill_h = max(cw, _even(cw * zoom)), max(ch, _even(ch * zoom))
        return (f"{head}scale={fill_w}:{fill_h}:force_original_aspect_ratio=increase,"
                f"crop={cw}:{ch},setsar=1,format=yuv420p"), zoom
    scale = min(cw / sw, ch / sh)
    pw, ph = min(cw, _even(sw * scale)), min(ch, _even(sh * scale))
    zw, zh = max(pw, _even(pw * zoom)), max(ph, _even(ph * zoom))
    ox, oy = _even((cw - pw) / 2.0) if cw > pw else 0, _even((ch - ph) / 2.0) if ch > ph else 0
    picture = f"scale={zw}:{zh},crop={pw}:{ph},setsar=1"
    if lay.get("fill") == "black":
        return f"{head}{picture},pad={cw}:{ch}:{ox}:{oy}:color=black,setsar=1,format=yuv420p", zoom
    bw, bh = _even(cw / 4.0), _even(ch / 4.0)  # the blurred copy is made small (fast), then scaled up
    luma = max(1, min(14, min(bw, bh) // 2 - 1))
    chroma = max(1, min(7, min(bw, bh) // 4 - 1))
    return (f"{head}split=2[vffg0][vfbg0];"
            f"[vfbg0]scale={bw}:{bh}:force_original_aspect_ratio=increase,crop={bw}:{bh},"
            f"boxblur=luma_radius={luma}:luma_power=2:chroma_radius={chroma}:chroma_power=1,"
            f"scale={cw}:{ch},eq=brightness=-0.06:saturation=0.85,setsar=1[vfbg];"
            f"[vffg0]{picture}[vffg];"
            f"[vfbg][vffg]overlay={ox}:{oy},format=yuv420p"), zoom


def _distributed_freeze_graph(
    source_duration: float,
    target_duration: float,
    slow_factor: float,
    freeze_duration: float,
    mirror_video: bool,
    zoom_factor: float,
    max_freeze_hold: float,
    layout: dict[str, Any] | None = None,
) -> tuple[str, str, dict[str, float]]:
    """Build a smooth-ish hybrid graph: moderate slow motion + distributed holds.

    Rather than one long frozen tail, the remaining hold time is spread over
    several positions inside the source segment.  Each hold clones the nearby
    frame for a short period, then motion continues.
    """
    import math

    max_hold = max(0.15, min(float(max_freeze_hold), 1.50))
    # Aim for <= max_hold per pause, but avoid pathological graph sizes.
    hold_count = max(1, int(math.ceil(freeze_duration / max_hold)))
    hold_count = min(24, hold_count)
    hold_each = freeze_duration / hold_count if hold_count else 0.0

    # Split source into equal pieces; hold after each piece (including the last).
    # This makes long extra duration less noticeable than one multi-second tail freeze.
    chunk_count = hold_count
    chunk_source = source_duration / chunk_count
    filters: list[str] = []
    split_labels = "".join(f"[src{i}]" for i in range(chunk_count))
    filters.append(f"[0:v]split={chunk_count}{split_labels}")
    out_labels: list[str] = []
    for i in range(chunk_count):
        a = i * chunk_source
        b = source_duration if i == chunk_count - 1 else (i + 1) * chunk_source
        src_len = b - a
        slowed_len = src_len * slow_factor
        target_len = slowed_len + hold_each
        label = f"[h{i}]"
        filters.append(
            f"[src{i}]trim=start={a:.6f}:end={b:.6f},"
            f"setpts={slow_factor:.9f}*(PTS-STARTPTS),fps=30,"
            f"tpad=stop_mode=clone:stop_duration={hold_each:.6f},"
            f"trim=duration={target_len:.6f},setpts=PTS-STARTPTS{label}"
        )
        out_labels.append(label)
    filters.append(f"{''.join(out_labels)}concat=n={chunk_count}:v=1:a=0[vhyb]")
    visual, zoom = _visual_filters(mirror_video, zoom_factor, layout)
    filters.append(f"[vhyb]{visual}[vout]")
    metrics = {
        "desired_factor": target_duration / source_duration,
        "applied_factor": slow_factor,
        "source_duration": source_duration,
        "target_duration": target_duration,
        "freeze_duration": freeze_duration,
        "freeze_count": float(hold_count),
        "freeze_each": hold_each,
        "hybrid_used": 1.0,
        "zoom_factor": zoom,
        "mirror_video": 1.0 if mirror_video else 0.0,
    }
    return ";".join(filters), "[vout]", metrics

def ffmpeg_encoders(ffmpeg: str) -> set[str]:
    result = subprocess.run([ffmpeg, "-hide_banner", "-encoders"], capture_output=True, text=True, encoding="utf-8", errors="replace")
    text = (result.stdout or "") + "\n" + (result.stderr or "")
    encoders: set[str] = set()
    for line in text.splitlines():
        fields = line.split()
        if len(fields) >= 2 and fields[0].startswith("V"):
            encoders.add(fields[1])
    return encoders


def render_video_part(
    ffmpeg: str,
    source: Path,
    output: Path,
    seg: dict[str, Any],
    target_duration: float,
    encoder: str,
    mirror_video: bool = True,
    zoom_factor: float = 1.05,
    max_video_speed: float = 1.25,
    max_video_slow: float = 1.35,
    smooth_freeze_fallback: bool = False,
    max_freeze_hold: float = 0.75,
    frame_count: int | None = None,
    layout: dict[str, Any] | None = None,
) -> dict[str, float]:
    """frame_count (when given) locks the part to exactly that many 30 fps frames.
    layout (frame_layout) is the output ratio / fill; None = the original 16:9 fill.

    Without it each part rounds up to the next whole frame; over ~270 parts that
    added ~6 s, so the picture drifted behind the narration and -shortest cut the end.
    """
    source_duration = float(seg["video_end"]) - float(seg["video_start"])
    frame_lock = ["-frames:v", str(int(frame_count))] if frame_count and frame_count > 0 else []
    sync_vf, metrics = _sync_filter(
        source_duration,
        target_duration,
        max_video_speed=max_video_speed,
        max_video_slow=max_video_slow,
        smooth_freeze_fallback=smooth_freeze_fallback,
        max_freeze_hold=max_freeze_hold,
    )

    common_input = [
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-ss", f"{float(seg['video_start']):.6f}",
        "-t", f"{source_duration:.6f}",
        "-i", str(source), "-an",
    ]

    if metrics.get("hybrid_used", 0.0) >= 0.5:
        graph, out_label, metrics = _distributed_freeze_graph(
            source_duration=source_duration,
            target_duration=target_duration,
            slow_factor=float(metrics["applied_factor"]),
            freeze_duration=float(metrics["freeze_duration"]),
            mirror_video=mirror_video,
            zoom_factor=zoom_factor,
            max_freeze_hold=max_freeze_hold,
            layout=layout,
        )
        cmd = [
            *common_input,
            "-filter_complex", graph,
            "-map", out_label,
            *frame_lock,
            *_encoder_args(encoder),
            "-movflags", "+faststart", str(output),
        ]
    else:
        visual, zoom = _visual_filters(mirror_video, zoom_factor, layout)
        vf = sync_vf + "," + visual
        if frame_lock:
            # Clone the last frame briefly so -frames:v can always be satisfied.
            vf += ",tpad=stop_mode=clone:stop_duration=0.5"
            metrics["frame_count"] = float(frame_count)
        metrics["zoom_factor"] = zoom
        metrics["mirror_video"] = 1.0 if mirror_video else 0.0
        cmd = [
            *common_input,
            "-vf", vf,
            *frame_lock,
            *_encoder_args(encoder),
            "-movflags", "+faststart", str(output),
        ]

    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        raise RuntimeError(f"Video segment render failed ({encoder}):\n{result.stderr[-4000:]}")
    return metrics

def concat_video_parts(ffmpeg: str, parts: list[Path], output: Path, work_dir: Path) -> None:
    list_file = work_dir / "video_parts.txt"
    list_file.write_text("\n".join(_concat_list_line(p) for p in parts) + "\n", encoding="utf-8")
    cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(list_file), "-c", "copy", "-movflags", "+faststart", str(output)]
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        raise RuntimeError(f"Video concat failed:\n{result.stderr[-4000:]}")


def mux_audio(ffmpeg: str, video: Path, audio: Path, output: Path, volume_percent: int = 100) -> None:
    # Narration gain (100 = unchanged); the limiter keeps a raised voice from clipping.
    volume = [] if int(volume_percent) == 100 else [
        "-af", f"volume={int(volume_percent) / 100.0:.3f},alimiter=limit=0.95:level=disabled"]
    cmd = [
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(video), "-i", str(audio),
        "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", *volume, "-c:a", "aac", "-b:a", "192k",
        "-shortest", "-movflags", "+faststart", str(output),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        raise RuntimeError(f"Final mux failed:\n{result.stderr[-4000:]}")



def _clamp01(value: Any, default: float = 0.0) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except Exception:
        return default


def _render_title_png(text: str, output: Path, font_path: str, font_size: int, max_width: int = 1500) -> Path:
    """Render Myanmar title through shared Python Chromium renderer."""
    import sys
    project_root = Path(__file__).resolve().parents[1]
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))

    try:
        from services.text_overlay_service import make_video_title_png
    except Exception as exc:
        raise RuntimeError(
            f"Myanmar Chromium renderer import failed: services.text_overlay_service ({exc})"
        ) from exc

    text = str(text or "").strip()
    if not text:
        raise ValueError("Empty title")

    cfg = {
        "font_path": str(font_path or ""),
        "font_size": int(font_size),
        "max_width": int(max_width),
    }
    try:
        return make_video_title_png(text, output, cfg)
    except Exception as exc:
        raise RuntimeError(f"Myanmar Chromium title render failed: {exc}") from exc


def _render_extra_text_png(
    text: str,
    output: Path,
    font_size: int,
    background_alpha: float = 0.18,
    max_width: int = 1500,
) -> Path:
    """Render the draggable extra-text overlay with Unicode-safe Myanmar shaping.

    Extra text uses the same proven Chromium path as the main title, but keeps
    its own lighter panel, smaller font, and three-line allowance.  This helper
    intentionally lives beside ``_render_title_png`` so the production overlay
    pass cannot reference an editor-only or missing renderer.
    """
    import sys
    project_root = Path(__file__).resolve().parents[1]
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))

    try:
        from services.text_overlay_service import make_video_title_png
    except Exception as exc:
        raise RuntimeError(
            f"Myanmar Chromium renderer import failed: services.text_overlay_service ({exc})"
        ) from exc

    text = str(text or "").strip()
    if not text:
        raise ValueError("Empty extra text")

    size = max(18, min(int(font_size), 84))
    cfg = {
        "font_size": size,
        "min_title_font_size": max(16, min(size, int(round(size * 0.62)))),
        "panel_width": max(320, min(int(max_width), 1800)),
        "video_title_panel_height": max(110, min(360, int(round(size * 4.2)))),
        "max_title_lines": 3,
        "panel_bg_alpha": max(0.0, min(1.0, float(background_alpha))),
        "panel_radius": 22,
        "title_horizontal_padding": 34,
        "title_vertical_padding": 14,
        "stroke_px": 1.4,
        "font_weight": 700,
    }
    try:
        return make_video_title_png(text, output, cfg)
    except Exception as exc:
        raise RuntimeError(f"Myanmar Chromium extra-text render failed: {exc}") from exc

def _encoder_args(encoder: str) -> list[str]:
    if encoder == "h264_nvenc":
        return ["-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr", "-cq", "19", "-b:v", "0"]
    return ["-c:v", "libx264", "-preset", "medium", "-crf", "18"]


def apply_production_overlays(
    ffmpeg: str,
    input_video: Path,
    output_video: Path,
    overlays: dict[str, Any] | None,
    encoder: str,
    work_dir: Path,
    log: LogFn = _noop,
    frame_size: tuple[int, int] = (1920, 1080),
) -> Path:
    overlays = dict(overlays or {})
    fw, fh = int(frame_size[0]), int(frame_size[1])
    text_width = int(fw * 0.78)  # 1500 px on the 1920 frame
    boxes = [x for x in (overlays.get("blur_boxes") or []) if isinstance(x, dict)]
    title_text = str(overlays.get("title_text") or "").strip()
    # V19: logo overlay is explicitly opt-in. Legacy callers without the flag keep
    # their old behavior; new UI sends logo_enabled on every run.
    logo_enabled = bool(overlays.get("logo_enabled", True))
    logo_path = str(overlays.get("logo_path") or "").strip() if logo_enabled else ""
    logo_file = Path(logo_path) if logo_path else None
    extra_text = str(overlays.get("extra_text") or "").strip()
    if not boxes and not title_text and not extra_text and not (logo_file and logo_file.is_file()):
        return input_video

    cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", str(input_video)]
    input_index = 1
    title_file: Path | None = None
    title_idx: int | None = None
    logo_idx: int | None = None
    extra_file: Path | None = None
    extra_idx: int | None = None
    if title_text:
        title_file = _render_title_png(
            title_text, work_dir / "title_overlay.png",
            str(overlays.get("title_font_path") or ""), int(overlays.get("title_font_size", 54)),
            max_width=text_width,
        )
        title_idx = input_index
        cmd += ["-i", str(title_file)]
        input_index += 1
    if logo_file and logo_file.is_file():
        logo_idx = input_index
        cmd += ["-i", str(logo_file)]
        input_index += 1
    if extra_text:
        extra_file = _render_extra_text_png(
            extra_text,
            work_dir / "extra_text_overlay.png",
            int(overlays.get("extra_text_font_size", 42)),
            0.18,
            max_width=text_width,
        )
        extra_idx = input_index
        cmd += ["-i", str(extra_file)]
        input_index += 1

    graph: list[str] = []
    current = "[0:v]"
    blur_strength = max(4, min(40, int(overlays.get("blur_strength", 18))))
    # yuv420p chroma planes are half-resolution. Letting FFmpeg inherit the
    # luma radius (for example 18) can exceed the chroma-plane limit (9) and
    # fail only at the final production overlay pass after all parts are done.
    chroma_blur_strength = min(9, blur_strength)
    for i, b in enumerate(boxes):
        x = min(fw - 8, int(round(_clamp01(b.get("x")) * fw)))
        y = min(fh - 8, int(round(_clamp01(b.get("y")) * fh)))
        w = int(round(_clamp01(b.get("w"), 0.1) * fw))
        h = int(round(_clamp01(b.get("h"), 0.1) * fh))
        w = max(8, min(w, fw - x)); h = max(8, min(h, fh - y))
        base = f"bbase{i}"; crop = f"bcrop{i}"; blur = f"bblur{i}"; out = f"bv{i}"
        graph.append(f"{current}split=2[{base}][{crop}]")
        graph.append(
            f"[{crop}]crop={w}:{h}:{x}:{y},"
            f"boxblur=luma_radius={blur_strength}:luma_power=2:"
            f"chroma_radius={chroma_blur_strength}:chroma_power=1[{blur}]"
        )
        graph.append(f"[{base}][{blur}]overlay={x}:{y}[{out}]")
        current = f"[{out}]"

    if title_idx is not None and title_file is not None:
        pos = overlays.get("title_position") or [0.5, 0.12]
        tx = _clamp01(pos[0] if len(pos) > 0 else 0.5, 0.5)
        ty = _clamp01(pos[1] if len(pos) > 1 else 0.12, 0.12)
        out = "vtitle"
        graph.append(f"{current}[{title_idx}:v]overlay=x='W*{tx:.6f}-w/2':y='H*{ty:.6f}-h/2':eof_action=repeat[{out}]")
        current = f"[{out}]"

    if logo_idx is not None:
        pos = overlays.get("logo_position") or [0.80, 0.06]
        lx = _clamp01(pos[0] if len(pos) > 0 else 0.80, 0.80)
        ly = _clamp01(pos[1] if len(pos) > 1 else 0.06, 0.06)
        lw = max(0.04, min(0.40, float(overlays.get("logo_width_fraction", 0.16))))
        lo = max(0.10, min(1.00, float(overlays.get("logo_opacity", 0.65))))
        graph.append(f"[{logo_idx}:v]scale={max(24, int(round(fw * lw)))}:-1[lg0]")
        if lo < 0.999:
            graph.append(f"[lg0]format=rgba,colorchannelmixer=aa={lo:.3f}[lg]")
        else:
            graph.append(f"[lg0]copy[lg]")
        out = "vlogo"
        graph.append(f"{current}[lg]overlay=x='W*{lx:.6f}':y='H*{ly:.6f}':eof_action=repeat[{out}]")
        current = f"[{out}]"

    if extra_idx is not None and extra_file is not None:
        pos = overlays.get("extra_text_position") or [0.50, 0.84]
        ex = _clamp01(pos[0] if len(pos) > 0 else 0.50, 0.50)
        ey = _clamp01(pos[1] if len(pos) > 1 else 0.84, 0.84)
        eo = max(0.10, min(1.00, float(overlays.get("extra_text_opacity", 0.65))))
        if eo < 0.999:
            graph.append(f"[{extra_idx}:v]format=rgba,colorchannelmixer=aa={eo:.3f}[etxt]")
        else:
            graph.append(f"[{extra_idx}:v]copy[etxt]")
        out = "vextra"
        graph.append(f"{current}[etxt]overlay=x='W*{ex:.6f}-w/2':y='H*{ey:.6f}-h/2':eof_action=repeat[{out}]")
        current = f"[{out}]"

    graph.append(f"{current}format=yuv420p[vout]")
    cmd += ["-filter_complex", ";".join(graph), "-map", "[vout]", "-an"] + _encoder_args(encoder) + ["-movflags", "+faststart", str(output_video)]
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if p.returncode != 0 and encoder == "h264_nvenc":
        log("⚠️ Overlay NVENC failed — libx264 fallback")
        cmd2 = cmd[:]
        # Rebuild tail safely rather than trying to mutate codec options in-place.
        map_pos = cmd2.index("-map")
        prefix = cmd2[:map_pos] + ["-map", "[vout]", "-an"]
        cmd2 = prefix + _encoder_args("libx264") + ["-movflags", "+faststart", str(output_video)]
        p = subprocess.run(cmd2, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if p.returncode != 0 or not output_video.is_file() or output_video.stat().st_size < 5000:
        raise RuntimeError("Production overlay render failed: " + p.stderr[-3000:])
    return output_video


@dataclass
class PipelinePaths:
    root: Path
    tts_parts: Path
    video_parts: Path
    work: Path
    checkpoint: Path
    voice: Path
    synced_video_noaudio: Path
    decorated_video_noaudio: Path
    plan: Path
    final: Path


def make_paths(job_dir: Path) -> PipelinePaths:
    root = job_dir / "edge_tts_smart_sync"
    tts = root / "tts_parts"
    video = root / "video_parts"
    work = root / "work"
    for p in (root, tts, video, work):
        p.mkdir(parents=True, exist_ok=True)
    return PipelinePaths(
        root=root,
        tts_parts=tts,
        video_parts=video,
        work=work,
        checkpoint=root / "checkpoint.json",
        voice=root / "voice_edge_tts.wav",
        synced_video_noaudio=work / "video_synced_noaudio.mp4",
        decorated_video_noaudio=work / "video_synced_decorated_noaudio.mp4",
        plan=root / "smart_sync_plan_edge_tts.json",
        final=root / "final_edge_tts_smart_sync.mp4",
    )


class EdgeSmartSync:
    def __init__(
        self,
        *,
        job_dir: str = "",
        source_path: str | None = None,
        plan_path: str | None = None,
        voice: str,
        edge_tts_rate: str = "+0%",
        edge_tts_pitch: str = "+0Hz",
        ffmpeg_path: str | None = None,
        ffprobe_path: str | None = None,
        max_video_speed: float = 1.25,
        max_video_slow: float = 1.35,
        mirror_video: bool = True,
        zoom_factor: float = 1.05,
        smooth_freeze_fallback: bool = False,
        max_freeze_hold: float = 0.75,
        render_final_video: bool = True,
        output_ratio: str = "16:9",
        frame_fill: str = "blur",
        overlays: dict[str, Any] | None = None,
        log: LogFn = _noop,
        progress: Callable[[int, int, str], None] | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> None:
        self.job_dir = Path(job_dir).resolve()
        self.source_path = Path(source_path).resolve() if source_path else (self.job_dir / "source.mp4")
        self.plan_path = Path(plan_path).resolve() if plan_path else (self.job_dir / "smart_sync_plan.json")
        self.voice = voice
        self.edge_tts_rate = str(edge_tts_rate or "+0%")
        self.edge_tts_pitch = str(edge_tts_pitch or "+0Hz")
        self.ffmpeg = resolve_binary(ffmpeg_path, "ffmpeg", [r"C:\ffmpeg\bin\ffmpeg.exe"])
        self.ffprobe = resolve_binary(ffprobe_path, "ffprobe", [r"C:\ffmpeg\bin\ffprobe.exe"])
        # Kept for backward config compatibility; exact Recap-style sync does
        # not clamp segment speed/slow when Auto Freeze is OFF.
        self.max_video_speed = max(1.0, float(max_video_speed))
        self.max_video_slow = max(1.0, float(max_video_slow))
        self.mirror_video = bool(mirror_video)
        self.zoom_factor = max(1.0, min(float(zoom_factor), 1.25))
        self.output_ratio = output_ratio if output_ratio in OUTPUT_RATIOS else "16:9"
        self.frame_fill = frame_fill if frame_fill in FRAME_FILLS else "blur"
        self.smooth_freeze_fallback = bool(smooth_freeze_fallback)
        self.max_freeze_hold = max(0.15, min(float(max_freeze_hold), 1.50))
        self.render_final_video = bool(render_final_video)
        # Short pauses: see trim_edge_silence. Set by render_worker from the UI box.
        self.short_pauses = False
        self.overlays = dict(overlays or {})
        self.log = log
        self.progress = progress or (lambda a, b, c: None)
        self.should_stop = should_stop or (lambda: False)

    def _checkpoint_load(self, paths: PipelinePaths) -> dict[str, Any]:
        if paths.checkpoint.is_file():
            try:
                data = load_json(paths.checkpoint)
                if isinstance(data, dict):
                    return data
            except Exception:
                pass
        return {"version": 1, "audio": {}, "video": {}}

    def _log_sync_check(self, paths: "PipelinePaths", segments: list[dict[str, Any]], checkpoint: dict[str, Any]) -> None:
        """Automatic, language-independent sync report written to the run log.

        Narration is the clock; every video part is frame-locked to it. So if the
        concatenated picture and the narration track have the same length, every
        segment starts on its own narration line. Speed-capped/trimmed segments are
        listed only for information: trimming shortens footage, not the timeline.
        """
        try:
            video_len = probe_duration(self.ffprobe, paths.synced_video_noaudio)
            audio_len = probe_duration(self.ffprobe, paths.voice)
            diff = video_len - audio_len
            trimmed = 0
            for seg in segments:
                sync = ((checkpoint.get("video") or {}).get(segment_basename(seg)) or {}).get("sync") or {}
                if float(sync.get("applied_factor", 0.0) or 0.0) > float(sync.get("desired_factor", 0.0) or 0.0) + 1e-6:
                    trimmed += 1
            status = "✅ SYNC OK" if abs(diff) <= 0.1 else "⚠️ SYNC DRIFT"
            self.log(
                f"{status}: picture={video_len:.2f}s · narration={audio_len:.2f}s · diff={diff:+.3f}s "
                f"| {len(segments)} segments, {trimmed} speed-capped ({self.max_video_speed:g}x) + footage trimmed"
            )
        except Exception as exc:
            self.log(f"⚠️ Sync check skipped: {exc}")

    def run(self) -> dict[str, Any]:
        source = self.source_path
        plan_file = self.plan_path
        if not source.is_file():
            raise FileNotFoundError(f"Source video မတွေ့ပါ: {source}")
        if not plan_file.is_file():
            raise FileNotFoundError(f"smart_sync_plan.json မတွေ့ပါ: {plan_file}")

        plan = load_json(plan_file)
        segments = normalize_segments(plan)
        paths = make_paths(self.job_dir)
        checkpoint = self._checkpoint_load(paths)
        checkpoint.setdefault("audio", {})
        checkpoint.setdefault("video", {})
        checkpoint["engine"] = "edge_tts"
        checkpoint["voice"] = self.voice

        self.log(f"📁 Job: {self.job_dir}")
        self.log(f"🧩 Timestamp segments: {len(segments)}")
        self.log(f"🎙️ TTS: Edge TTS | Voice: {self.voice}")
        self.log(f"🎞️ Smart Sync: Exact Recap timing — distributed freeze fallback={'ON' if self.smooth_freeze_fallback else 'OFF'}")
        try:
            source_w, source_h = probe_video_size(self.ffprobe, source)
        except Exception as exc:  # only the frame shape depends on it: Original falls back to 16:9
            source_w, source_h = 0, 0
            self.log(f"⚠️ Video size not readable ({exc}); Original ratio falls back to 16:9")
        layout = frame_layout(self.output_ratio, self.frame_fill, source_w, source_h)
        self.log(f"🪞 Mirror={'ON' if self.mirror_video else 'OFF'} | Ratio={layout['ratio']} "
                 f"{layout['cw']}x{layout['ch']} ({layout['fill']}) from {source_w}x{source_h} | Zoom={self.zoom_factor:.3f}x")

        engine = EdgeTTSEngine(
            ffmpeg=self.ffmpeg,
            work_dir=paths.work / "edge_tts_temp",
            rate=self.edge_tts_rate,
            pitch=self.edge_tts_pitch,
            log=self.log,
        )
        fingerprint_voice = (
            self.voice + "|edge-tts-v1|rate=" + self.edge_tts_rate + "|pitch=" + self.edge_tts_pitch
        )
        checkpoint["tts_provider"] = "edge_tts"

        timing: list[dict[str, Any]] = []
        audio_parts: list[Path] = []
        cursor = 0.0
        short_pauses = bool(self.short_pauses)
        tts_total = 0.0

        # AUDIO: timestamp segment = master save unit. The user's engine may split
        # sentences internally, but only one deterministic WAV is kept per timestamp segment.
        for idx, seg in enumerate(segments, 1):
            if self.should_stop():
                raise InterruptedError("STOP requested — လက်ရှိပြီးထားတာတွေ checkpoint ထဲသိမ်းထားပါတယ်။")
            base = segment_basename(seg)
            dst = paths.tts_parts / f"{base}.wav"
            fp = segment_fingerprint(seg, fingerprint_voice)
            rec = checkpoint["audio"].get(base, {})
            reusable = rec.get("fingerprint") == fp and dst.is_file() and dst.stat().st_size > 1000
            if reusable:
                duration = probe_duration(self.ffprobe, dst)
                self.log(f"♻️ [{idx}/{len(segments)}] {base}.wav reuse ({duration:.2f}s)")
            else:
                self.progress(idx - 1, len(segments), f"Edge TTS {idx}/{len(segments)}")
                self.log(f"🗣️ [{idx}/{len(segments)}] {base} generate via Edge TTS")
                try:
                    produced = engine.generate_channel99_voice(seg["narration"], self.voice)
                    if not produced or not Path(produced).is_file():
                        raise RuntimeError("TTS output path မရပါ။")
                except Exception as exc:
                    raise RuntimeError(f"TTS segment generate failed: {exc}") from exc
                produced_path = Path(produced).resolve()
                shutil.copy2(produced_path, dst)
                produced_path.unlink(missing_ok=True)
                duration = probe_duration(self.ffprobe, dst)
                if duration <= 0.05:
                    raise RuntimeError(f"Generated audio duration invalid: {dst}")
                checkpoint["audio"][base] = {
                    "fingerprint": fp,
                    "file": str(dst),
                    "duration": round(duration, 3),
                    "video_start": seg["video_start"],
                    "video_end": seg["video_end"],
                    "text": seg["narration"],
                }
                atomic_write_json(paths.checkpoint, checkpoint)
                self.log(f"✅ {base}.wav = {duration:.2f}s")

            part = dst
            tts_total += duration
            if short_pauses:
                window = float(seg["video_end"]) - float(seg["video_start"])
                part, duration = trim_edge_silence(dst, paths.tts_parts.parent / "tts_parts_short" / dst.name,
                                                   min_duration=window / self.max_video_speed)
            start = cursor
            end = cursor + duration
            timing.append({
                "segment_id": seg["segment_id"],
                "source_index": seg["source_index"],
                "video_start": seg["video_start"],
                "video_end": seg["video_end"],
                "audio_start": round(start, 3),
                "audio_end": round(end, 3),
                "audio_duration": round(duration, 3),
                "text": seg["narration"],
                "audio_file": str(part),
            })
            cursor = end
            audio_parts.append(part)
            self.progress(idx, len(segments), f"Audio {idx}/{len(segments)}")

        if short_pauses:
            self.log(f"✂️ Short pauses: Edge TTS silence trimmed · voice {tts_total / 60:.1f} → {cursor / 60:.1f} min "
                     f"(~0.25 s between lines; picture never cut for it)")
        self.log("🔗 Timestamp audio parts concat...")
        concat_audio(self.ffmpeg, audio_parts, paths.voice, paths.work)
        final_audio_duration = probe_duration(self.ffprobe, paths.voice)
        self.log(f"✅ {paths.voice.name} = {final_audio_duration:.2f}s")

        sync_segments = [
            {
                "video_start": item["video_start"],
                "video_end": item["video_end"],
                "audio_start": item["audio_start"],
                "audio_end": item["audio_end"],
            }
            for item in timing
        ]
        out_plan = dict(plan)
        out_plan["segments"] = [dict(seg) for seg in plan.get("segments", [])]
        out_plan["tts_provider"] = "edge_tts"
        out_plan["tts_voice"] = self.voice
        out_plan["actual_audio_duration"] = round(final_audio_duration, 3)
        out_plan["tts_timing_segments"] = timing
        out_plan["sync_segments"] = sync_segments
        out_plan["subtitle_segments"] = [
            {"start": item["audio_start"], "end": item["audio_end"], "text": item["text"]}
            for item in timing
        ]
        out_plan["production_overlays"] = self.overlays
        atomic_write_json(paths.plan, out_plan)

        if not self.render_final_video:
            self.log("✅ Audio + Smart Sync plan READY (final video render skipped).")
            return {
                "audio": str(paths.voice),
                "plan": str(paths.plan),
                "final": None,
                "segments": len(timing),
            }

        self.log("🎬 Smart Sync video parts render စမယ်...")
        encoders = ffmpeg_encoders(self.ffmpeg)
        preferred = "h264_nvenc" if "h264_nvenc" in encoders else "libx264"
        remembered = str(checkpoint.get("selected_encoder") or "")
        encoder = remembered if remembered in {"h264_nvenc", "libx264"} else preferred
        video_parts: list[Path] = []
        # Cumulative frame lock: each part gets round(audio_end*30) - frames_so_far
        # frames, so video never drifts more than half a frame from the narration.
        audio_cursor = 0.0
        frames_done = 0

        for idx, (seg, item) in enumerate(zip(segments, timing), 1):
            if self.should_stop():
                raise InterruptedError("STOP requested — video parts checkpoint ထဲကနေ Resume လုပ်နိုင်ပါတယ်။")
            base = segment_basename(seg)
            dst = paths.video_parts / f"{base}.mp4"
            target = float(item["audio_duration"])
            audio_cursor += target
            frame_count = max(1, int(round(audio_cursor * 30)) - frames_done)
            frames_done += frame_count
            vfp_raw = json.dumps({
                "frame_count": frame_count,
                "source": str(source.resolve()),
                "source_mtime": source.stat().st_mtime_ns,
                "start": seg["video_start"], "end": seg["video_end"],
                "target": target, "sync_mode": "hybrid_recap_distributed_freeze",
                "max_speed": self.max_video_speed, "max_slow": self.max_video_slow,
                "smooth_freeze": self.smooth_freeze_fallback, "max_freeze_hold": self.max_freeze_hold,
                "mirror": self.mirror_video, "zoom": self.zoom_factor, "encoder": encoder,
                "frame": [layout["ratio"], layout["fill"], layout["cw"], layout["ch"]],
            }, sort_keys=True).encode()
            vfp = hashlib.sha256(vfp_raw).hexdigest()
            rec = checkpoint["video"].get(base, {})
            reusable = rec.get("fingerprint") == vfp and dst.is_file() and dst.stat().st_size > 5000
            if reusable:
                self.log(f"♻️ VIDEO [{idx}/{len(segments)}] {base}.mp4 reuse")
            else:
                self.progress(idx - 1, len(segments), f"Smart Sync video {idx}/{len(segments)}")
                self.log(f"🎞️ VIDEO [{idx}/{len(segments)}] {base} → {target:.2f}s")
                try:
                    metrics = render_video_part(
                        self.ffmpeg, source, dst, seg, target, encoder,
                        self.mirror_video, self.zoom_factor,
                        self.max_video_speed, self.max_video_slow,
                        self.smooth_freeze_fallback, self.max_freeze_hold,
                        frame_count=frame_count, layout=layout,
                    )
                except Exception:
                    # Only on the first non-reused part, allow global NVENC -> CPU fallback.
                    if encoder == "h264_nvenc" and not video_parts:
                        self.log("⚠️ NVENC မရပါ — libx264 CPU encoder ကို fallback လုပ်မယ်။")
                        encoder = "libx264"
                        # Encoder changed, invalidate any video checkpoint rows to avoid mixed codecs.
                        checkpoint["video"] = {}
                        for old in paths.video_parts.glob("*.mp4"):
                            old.unlink(missing_ok=True)
                        metrics = render_video_part(
                            self.ffmpeg, source, dst, seg, target, encoder,
                            self.mirror_video, self.zoom_factor,
                            self.max_video_speed, self.max_video_slow,
                            self.smooth_freeze_fallback, self.max_freeze_hold,
                            frame_count=frame_count, layout=layout,
                        )
                        # Recompute fingerprint with CPU encoder.
                        vfp_raw = json.dumps({
                            "frame_count": frame_count,
                            "source": str(source.resolve()),
                            "source_mtime": source.stat().st_mtime_ns,
                            "start": seg["video_start"], "end": seg["video_end"],
                            "target": target, "sync_mode": "hybrid_recap_distributed_freeze",
                            "max_speed": self.max_video_speed, "max_slow": self.max_video_slow,
                            "smooth_freeze": self.smooth_freeze_fallback, "max_freeze_hold": self.max_freeze_hold,
                            "mirror": self.mirror_video, "zoom": self.zoom_factor, "encoder": encoder,
                            "frame": [layout["ratio"], layout["fill"], layout["cw"], layout["ch"]],
                        }, sort_keys=True).encode()
                        vfp = hashlib.sha256(vfp_raw).hexdigest()
                    else:
                        raise
                if metrics.get("hybrid_used", 0.0) >= 0.5:
                    self.log(
                        f"   ↳ Hybrid: slow {metrics['applied_factor']:.3f}x + "
                        f"{int(metrics.get('freeze_count', 0))} short holds "
                        f"({metrics.get('freeze_duration', 0.0):.2f}s total)"
                    )
                checkpoint["video"][base] = {
                    "fingerprint": vfp,
                    "file": str(dst),
                    "target_duration": round(target, 3),
                    "encoder": encoder,
                    "sync": {k: round(v, 6) for k, v in metrics.items()},
                }
                checkpoint["selected_encoder"] = encoder
                atomic_write_json(paths.checkpoint, checkpoint)
            video_parts.append(dst)
            self.progress(idx, len(segments), f"Video {idx}/{len(segments)}")

        self.log("🔗 Smart Sync video parts concat...")
        concat_video_parts(self.ffmpeg, video_parts, paths.synced_video_noaudio, paths.work)
        self._log_sync_check(paths, segments, checkpoint)
        video_for_mux = paths.synced_video_noaudio
        if self.overlays.get("blur_boxes") or str(self.overlays.get("title_text") or "").strip() or str(self.overlays.get("logo_path") or "").strip() or str(self.overlays.get("extra_text") or "").strip():
            self.log("🎨 Production overlay pass — Blur / Title PNG / Logo...")
            video_for_mux = apply_production_overlays(
                self.ffmpeg, paths.synced_video_noaudio, paths.decorated_video_noaudio,
                self.overlays, encoder, paths.work, self.log, frame_size=(layout["cw"], layout["ch"]),
            )
        self.log("🎧 Edge TTS audio mux...")
        volume_percent = int(getattr(self, "voice_volume_percent", 100) or 100)
        if volume_percent != 100:
            self.log(f"🔊 Voice volume {volume_percent}% applied at final mux (limiter on)")
        mux_audio(self.ffmpeg, video_for_mux, paths.voice, paths.final, volume_percent)
        final_duration = probe_duration(self.ffprobe, paths.final)
        self.log(f"✅ FINAL READY: {paths.final}")
        self.log(f"⏱️ Final duration: {final_duration:.2f}s")
        return {
            "audio": str(paths.voice),
            "plan": str(paths.plan),
            "final": str(paths.final),
            "segments": len(timing),
            "duration": final_duration,
        }
