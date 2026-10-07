from __future__ import annotations

import gc
import hashlib
import importlib.util
import json
import os
import re
import difflib
import shutil
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable

LogFn = Callable[[str], None]
ProgressFn = Callable[[int, int, str], None]
PLANNER_MODE = "timestamp_translation_en_to_mm_narrator_v1"
TRANSLATION_DIRECTION = "en_to_mm"
SEGMENTATION_PROFILE = "v23_v2_local_sync_4p2_5p6"
# Original V2 visual boundary: first frame to last frame, every gap between two
# narration units split at its midpoint.
BOUNDARY_PROFILE = "v2_full_coverage_midpoint"
WHISPER_MODEL = "small.en"
WHISPER_LANGUAGE = "en"


GEMINI_MODEL_FALLBACK_CHAIN = [
    "gemini-3.5-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.6-flash",
    "gemini-3.7-flash",
    "gemini-3.1-flash-lite",
]


class GeminiKeyPoolExhaustedError(RuntimeError):
    """Every configured Gemini model/key fallback was exhausted."""


def _is_gemini_key_rejection(exc: BaseException) -> bool:
    """Errors that should advance to the next key inside the SAME model."""
    text = str(exc).lower()
    return (
        "monthly spending cap" in text
        or "project-spend-caps" in text
        or "resource_exhausted" in text
        or "api_key_invalid" in text
        or "api key not valid" in text
        or "unauthenticated" in text
        or "permission_denied" in text
        or "permission denied" in text
        or bool(re.search(r"\b(?:401|403|429)\b", text))
    )


def _is_gemini_transient_model_error(exc: BaseException) -> bool:
    """Errors that should skip immediately to the NEXT model, restarting at key #1."""
    text = str(exc).lower()
    markers = (
        "503",
        "504",
        "unavailable",
        "service unavailable",
        "high demand",
        "overloaded",
        "deadline_exceeded",
        "deadline exceeded",
        "timeout",
        "timed out",
    )
    return any(marker in text for marker in markers)


def _gemini_key_pool_message(pool_size: int, model_count: int) -> str:
    return (
        f"Gemini fallback chain exhausted — {model_count} model(s) × up to {pool_size} key(s).\n\n"
        "429/quota/auth errors exhausted the configured key pool and no fallback model remains, "
        "or the final model returned a non-recoverable service error.\n"
        "ပြီးရင် START ကိုပြန်နှိပ်ပါ။ Gemini transcript နဲ့ ပြီးထားတဲ့ translation checkpoint ကို "
        "ပြန်သုံးပြီး ကျန်တဲ့ batch ကနေ ဆက်လုပ်ပါမယ်။"
    )


def _configure_gemini_key_pool(
    ai: Any,
    key_pool: list[tuple[str, str]],
    *,
    client_factory: Callable[..., Any],
    log: LogFn,
    request_timeout_seconds: int = 120,
    model_chain: list[str] | None = None,
) -> None:
    """Attach ordered model-major/key-minor Gemini fallback state.

    Policy:
      * 429/auth/quota -> next key in the current model.
      * After every key is rejected -> next model and restart from key #1.
      * 503/high-demand/timeout -> next model immediately and restart from key #1.
      * Model/key switches retry the exact same request without consuming validation retries.
    """
    if not key_pool:
        return

    models = [str(x).strip() for x in (model_chain or GEMINI_MODEL_FALLBACK_CHAIN) if str(x).strip()]
    if not models:
        models = [str(getattr(ai, "text_model", "gemini-3.5-flash") or "gemini-3.5-flash")]

    model_index = 0
    key_index = 0
    timeout_seconds = max(30, min(int(request_timeout_seconds), 600))
    http_options = {
        "timeout": timeout_seconds * 1000,
        "retryOptions": {"attempts": 1},
    }

    def build_client(key: str) -> Any:
        ai._enable_proxy()
        try:
            return client_factory(api_key=key, http_options=http_options)
        finally:
            ai._disable_proxy()

    def activate(mi: int, ki: int, *, reason: str | None = None) -> None:
        nonlocal model_index, key_index
        model_index = mi
        key_index = ki
        label, key = key_pool[key_index]
        model = models[model_index]
        ai.client = build_client(key)
        ai.text_model = model
        os.environ["GEMINI_API_KEY"] = key
        os.environ["GEMINI_TEXT_MODEL"] = model
        ai.api_key = key
        ai._recap_active_key_label = label
        ai._recap_active_model = model
        ai._recap_model_index = model_index
        ai._recap_key_index = key_index
        if reason:
            log(
                f"🔄 Gemini fallback ({reason}) → {model} | {label} "
                f"(model {model_index + 1}/{len(models)}, key {key_index + 1}/{len(key_pool)})"
            )

    # Replace the AIService client before the first planner request.
    activate(0, 0)

    def rotate_key() -> bool:
        next_key = key_index + 1
        if next_key >= len(key_pool):
            return False
        activate(model_index, next_key, reason="next key")
        return True

    def advance_model(reason: str = "next model") -> bool:
        next_model = model_index + 1
        if next_model >= len(models):
            return False
        # Required behavior: every model starts again at Key 1.
        activate(next_model, 0, reason=reason)
        return True

    def advance_after_key_pool() -> bool:
        return advance_model("key pool exhausted")

    ai._recap_active_key_label = key_pool[0][0]
    ai._recap_active_model = models[0]
    ai._recap_key_pool_size = len(key_pool)
    ai._recap_model_pool_size = len(models)
    ai._recap_model_chain = list(models)
    ai._recap_rotate_gemini_key = rotate_key
    ai._recap_advance_gemini_model = advance_model
    ai._recap_advance_after_key_pool = advance_after_key_pool
    log(f"🔑 Gemini key pool ready — {len(key_pool)} key(s); active: {key_pool[0][0]}")
    log("🤖 Gemini fallback chain: " + " → ".join(models))
    log(f"⏱️ Gemini request hard timeout: {timeout_seconds}s; planner retries remain enabled")


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Python module load မရပါ: {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@contextmanager
def _pushd(path: Path):
    old = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(old)


def _probe_video_duration(ffprobe: str, source: Path) -> float:
    """Use the primary VIDEO stream as the source-of-truth duration.

    Some downloaded/trimmed MP4s contain a valid ~13 minute video stream but an
    accidentally long audio stream (for example ~44 minutes). format.duration then
    follows the long audio tail and would transcribe audio that has no video.
    Recap sync is visual, so v:0 duration is authoritative.
    """
    cmd = [
        ffprobe, "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(source),
    ]
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if p.returncode == 0:
        raw = (p.stdout or "").strip().splitlines()
        for line in raw:
            try:
                value = float(line.strip())
                if value > 0.05:
                    return value
            except Exception:
                pass

    # Fallback for unusual files where stream.duration is unavailable.
    p2 = subprocess.run(
        [ffprobe, "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(source)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if p2.returncode != 0:
        raise RuntimeError("ffprobe duration failed:\n" + (p2.stderr or p.stderr)[-3000:])
    return float((p2.stdout or "0").strip())


def _probe_audio_duration(ffprobe: str, source: Path) -> float | None:
    p = subprocess.run(
        [ffprobe, "-v", "error", "-select_streams", "a:0",
         "-show_entries", "stream=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(source)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if p.returncode != 0:
        return None
    for line in (p.stdout or "").strip().splitlines():
        try:
            value = float(line.strip())
            if value > 0.0:
                return value
        except Exception:
            pass
    return None


# Real speech peaks far above this; -91 dB was observed on a video's muted end credits.


def _prepare_whisper_audio(ffmpeg: str, source: Path, job_dir: Path, video_duration: float, log: LogFn) -> Path:
    """Decode the video-length audio to a reusable 16 kHz mono WAV for local Whisper."""
    out = job_dir / "_whisper_audio_16k.wav"
    if out.is_file() and out.stat().st_size > 4096:
        log(f"♻️ Whisper trimmed audio reuse: {out.name}")
        return out
    cmd = [
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(source), "-map", "0:a:0", "-vn",
        "-t", f"{video_duration:.6f}",
        "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(out),
    ]
    log(f"✂️ Preparing Whisper audio: trim to video duration {video_duration:.3f}s")
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0 or not out.is_file():
        raise RuntimeError("Whisper audio preparation failed:\n" + (proc.stderr or "")[-3000:])
    return out


def _atomic_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _source_fingerprint_for_mode(source: Path, planner_mode: str) -> str:
    st = source.stat()
    raw = json.dumps({
        "path": str(source.resolve()),
        "size": st.st_size,
        "mtime_ns": st.st_mtime_ns,
        "planner_mode": planner_mode,
    }, sort_keys=True).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _fmt_ts(seconds: float) -> str:
    ms = max(0, int(round(float(seconds) * 1000)))
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


def _clean(s: Any) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip()


def _has_speakable_content(value: Any) -> bool:
    """Return True when text contains at least one Unicode letter/number.

    This rejects punctuation-only placeholders such as ``...`` or ``…`` before
    they can reach TTS. ``str.isalnum`` treats Burmese letters as content too.
    """
    text = _clean(value)
    return any(ch.isalnum() for ch in text)


def _sanitize_translation_text(value: Any) -> str:
    """Strip known Gemini audit/meta wrappers while preserving translated speech.

    V26 exposed a rare audit leak such as::

        [Unused filler adjustment skipped - translated faithfully: Reinvent the pyramid game.]

    The bracketed prefix is tool/audit metadata and must never be spoken by TTS.  When
    that exact wrapper appears, keep only the faithful translation inside it.
    """
    text = _clean(value)
    match = re.fullmatch(
        r"\[\s*Unused filler adjustment skipped\s*[-–—:]\s*translated faithfully\s*:\s*(.*?)\s*\]",
        text,
        flags=re.IGNORECASE,
    )
    if match:
        inner = _clean(match.group(1))
        if inner:
            return inner
    return text


def _foreign_letters(text: Any) -> list[str]:
    """Letters that are neither Myanmar nor Latin outside ( ): Gemini sometimes writes Georgian
    letters inside a Burmese word (ဧည့်ခန်း came back as "მისခန်း"), which the voice cannot read."""
    bare = re.sub(r"\([^()]*\)", "", str(text or ""))
    return list(dict.fromkeys(
        ch for ch in bare
        if ch.isalpha() and not (0x1000 <= ord(ch) <= 0x109F or 0xA9E0 <= ord(ch) <= 0xA9FF
                                 or 0xAA60 <= ord(ch) <= 0xAA7F or ord(ch) <= 0x024F
                                 or 0x1E00 <= ord(ch) <= 0x1EFF)
    ))


def _drop_foreign_letters(items: list[dict[str, Any]], log: LogFn) -> list[dict[str, Any]]:
    """Last resort after the re-asks: drop the stray letters and name the line for review."""
    out = []
    for item in items:
        letters = _foreign_letters(item.get("translation"))
        if letters:
            text = _clean("".join(ch for ch in str(item["translation"]) if ch not in letters))
            log(f"⚠️ ID {int(item['id']):05d}: removed letters of another script ({' '.join(letters)}); check this line")
            item = {**item, "translation": text}
        out.append(item)
    return out


def _assert_speakable_translation_items(items: list[dict[str, Any]]) -> None:
    for item in items:
        source = _clean(item.get("source_text"))
        translation = _sanitize_translation_text(item.get("translation"))
        if _has_speakable_content(source) and not _has_speakable_content(translation):
            raise ValueError(
                f"Translation ID {item.get('id')} contains no speakable words "
                f"(got {translation!r}); placeholders/ellipsis are not allowed"
            )


def _pathological_repetition_segment_ids(transcript: dict[str, Any]) -> set[int]:
    """Detect unmistakable local-Whisper repetition hallucinations.

    This is deliberately conservative: only long raw ASR segments with extreme
    repetition are flagged.  It is mainly a safety net for cases such as a
    20-30 second segment made almost entirely of ``我...我...`` or ``冲冲冲...``.
    Ordinary stutters/repeated dialogue are not long or repetitive enough to trip it.
    """
    backend = str(transcript.get("backend") or "").lower()
    if "whisper" not in backend:
        return set()
    bad: set[int] = set()
    for index, seg in enumerate(transcript.get("segments") or []):
        if not isinstance(seg, dict):
            continue
        try:
            start = float(seg.get("start", 0.0) or 0.0)
            end = float(seg.get("end", start) or start)
        except Exception:
            continue
        duration = max(0.0, end - start)
        if duration < 4.0:
            continue
        text = _clean(seg.get("text"))
        chars = [ch.casefold() for ch in text if ch.isalnum()]
        if len(chars) < 24:
            continue
        counts: dict[str, int] = {}
        for ch in chars:
            counts[ch] = counts.get(ch, 0) + 1
        top_ratio = (max(counts.values()) / len(chars)) if counts else 0.0
        unique_ratio = (len(counts) / len(chars)) if chars else 1.0
        if top_ratio >= 0.72 and unique_ratio <= 0.18:
            try:
                sid = int(seg.get("id", index))
            except Exception:
                sid = index
            bad.add(sid)
    return bad


def _unit_is_low_information_repetition(unit: dict[str, Any]) -> bool:
    text = _clean(unit.get("source_text"))
    chars = [ch.casefold() for ch in text if ch.isalnum()]
    if not chars:
        return True
    if len(set(chars)) <= 1:
        return True
    tokens = []
    for token in text.split():
        cleaned = "".join(ch.casefold() for ch in token if ch.isalnum())
        if cleaned:
            tokens.append(cleaned)
    if len(tokens) >= 4:
        counts: dict[str, int] = {}
        for token in tokens:
            counts[token] = counts.get(token, 0) + 1
        top_ratio = max(counts.values()) / len(tokens)
        unique_ratio = len(counts) / len(tokens)
        if top_ratio >= 0.75 and unique_ratio <= 0.25:
            return True
    return False


def _filter_local_whisper_artifact_units(
    units: list[dict[str, Any]],
    transcript: dict[str, Any],
    *,
    log: LogFn | None = None,
) -> list[dict[str, Any]]:
    """Remove only atomic narration rows proven to come from ASR junk.

    IDs are intentionally NOT renumbered.  Keeping stable IDs lets an existing
    timestamp_translations.json reuse every unaffected translated row while obsolete
    hallucination rows are simply ignored.
    """
    pathological_ids = _pathological_repetition_segment_ids(transcript)
    kept: list[dict[str, Any]] = []
    dropped: list[tuple[int, str]] = []
    for unit in units:
        uid = int(unit.get("id", -1))
        source = _clean(unit.get("source_text"))
        if not _has_speakable_content(source):
            dropped.append((uid, "punctuation-only source"))
            continue
        source_ids = {int(value) for value in (unit.get("source_transcript_ids") or [unit.get("source_transcript_id", -1)])}
        if source_ids and source_ids.issubset(pathological_ids) and _unit_is_low_information_repetition(unit):
            dropped.append((uid, "Whisper repetition hallucination"))
            continue
        kept.append(unit)
    if dropped and log:
        preview = ", ".join(f"{uid:05d}" for uid, _reason in dropped[:12])
        suffix = "..." if len(dropped) > 12 else ""
        log(
            f"⚠️ Local Whisper artifact guard removed {len(dropped)} non-narration unit(s): "
            f"IDs {preview}{suffix}. Existing good translation cache remains reusable."
        )
        if pathological_ids:
            seg_preview = ", ".join(str(x) for x in sorted(pathological_ids)[:10])
            log(f"   ↳ pathological raw Whisper segment(s): {seg_preview}")
    return kept


def _plan_has_speakable_narration(plan: Any) -> bool:
    if not isinstance(plan, dict):
        return False
    segments = plan.get("segments")
    if not isinstance(segments, list) or not segments:
        return False
    return all(
        isinstance(seg, dict) and _has_speakable_content(seg.get("narration"))
        for seg in segments
    )


_COMMON_ABBREVIATIONS = {
    "mr.", "mrs.", "ms.", "dr.", "prof.", "sr.", "jr.", "st.",
    "vs.", "etc.", "e.g.", "i.e.",
}


def _word_ends_sentence(text: str) -> bool:
    cleaned = _clean(text).lower()
    if cleaned in _COMMON_ABBREVIATIONS:
        return False
    # ASR may split a person's name as "N." + "Ben".  Treat a
    # lone Latin initial as part of the following name instead of inventing a
    # standalone translation ID.
    if re.fullmatch(r"[a-z]\.['’\"”)]?", cleaned):
        return False
    return bool(re.search(r"[.!?…။।॥。！？][\"'’”)]?$", cleaned))


def _word_ends_soft_clause(text: str) -> bool:
    return bool(re.search(r"[;,:၊；，、][\"'’”)]?$", _clean(text)))


def _fallback_word_tokens(text: str, start: float, end: float, transcript_id: int) -> list[dict[str, Any]]:
    words = re.findall(r"\S+", _clean(text))
    if not words or end <= start:
        return []
    step = (end - start) / len(words)
    return [
        {
            "text": word,
            "start": start + index * step,
            "end": start + (index + 1) * step,
            "source_transcript_id": transcript_id,
        }
        for index, word in enumerate(words)
    ]


def _transcript_word_tokens(transcript: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten timestamped transcript words while retaining measured times and source IDs."""
    tokens: list[dict[str, Any]] = []
    for raw_index, raw in enumerate(transcript.get("segments") or []):
        text = _clean(raw.get("text"))
        start = float(raw.get("start", 0.0) or 0.0)
        end = float(raw.get("end", start) or start)
        if not text or end <= start:
            continue
        transcript_id = int(raw.get("id", raw_index))
        measured: list[dict[str, Any]] = []
        for word in raw.get("words") or []:
            wt = _clean(word.get("text"))
            ws = float(word.get("start", start) or start)
            we = float(word.get("end", ws) or ws)
            if wt and we > ws:
                measured.append({
                    "text": wt,
                    "start": ws,
                    "end": we,
                    "source_transcript_id": transcript_id,
                })
        tokens.extend(measured or _fallback_word_tokens(text, start, end, transcript_id))
    tokens.sort(key=lambda item: (float(item["start"]), float(item["end"])))
    return tokens


def atomicize_timestamp_segments(
    transcript: dict[str, Any],
    *,
    max_duration: float = 5.6,
    target_duration: float = 4.2,
    max_words: int = 24,
) -> list[dict[str, Any]]:
    """Build V2-style local sync units from measured word timestamps.

    V23 deliberately keeps Smart Sync windows small.  The old sentence-locked planner could
    merge many raw ASR rows into 10-30 second units, so one short/long TTS result could make
    an entire scene feel late.  This profile still prefers real sentence/clause/pause
    boundaries, but it also applies a local ~4.2s target and ~5.6s hard cap.  Translation
    context is still supplied across IDs, so splitting a long sentence does not remove story
    context; it only gives Smart Sync more frequent timestamp anchors.
    """
    tokens = _transcript_word_tokens(transcript)
    units: list[dict[str, Any]] = []
    current: list[dict[str, Any]] = []
    target_duration = max(2.5, float(target_duration))
    max_duration = max(target_duration + 0.5, float(max_duration))
    min_natural_duration = min(1.8, target_duration * 0.4)

    def close_current(boundary: str) -> None:
        nonlocal current
        if not current:
            return
        source_ids = list(dict.fromkeys(int(word["source_transcript_id"]) for word in current))
        units.append({
            "source_transcript_id": source_ids[0],
            "source_transcript_ids": source_ids,
            "speech_start": round(float(current[0]["start"]), 3),
            "speech_end": round(float(current[-1]["end"]), 3),
            "source_text": _clean(" ".join(str(word["text"]) for word in current)),
            "semantic_boundary": boundary,
            "forced_boundary": boundary.startswith("forced"),
        })
        current = []

    for index, token in enumerate(tokens):
        current.append(token)
        duration = float(current[-1]["end"]) - float(current[0]["start"])
        text = str(token["text"])
        next_gap = 0.0
        if index + 1 < len(tokens):
            next_gap = max(0.0, float(tokens[index + 1]["start"]) - float(token["end"]))

        # A real acoustic gap is always a safe boundary, even when the phrase is short.
        # This prevents a 20-50s silent/dialogue hole from being absorbed by the next unit.
        if next_gap >= 1.0:
            close_current("pause")
        elif _word_ends_sentence(text):
            close_current("sentence")
        elif _word_ends_soft_clause(text) and duration >= min_natural_duration:
            close_current("clause")
        elif duration >= min_natural_duration and next_gap >= 0.50:
            close_current("pause")
        elif duration >= target_duration and next_gap >= 0.16:
            close_current("pause")
        # V2-style local anchor: never allow a normal spoken unit to grow into a long
        # sentence-locked block merely because punctuation is missing.
        elif duration >= max_duration:
            close_current("forced_local")
    close_current("end_of_transcript")

    # A repeated name can also arrive as a measured 40–120 ms sentence of its
    # own (for example "Han." + "Han accidentally ...").  It is not useful as
    # narration and would create a false semantic ID, so fold only these tiny
    # one/two-token artifacts into the following complete unit.
    merged_units: list[dict[str, Any]] = []
    index = 0
    while index < len(units):
        unit = units[index]
        duration = float(unit["speech_end"]) - float(unit["speech_start"])
        token_count = len(re.findall(r"\S+", str(unit["source_text"])))
        following_gap = (
            float(units[index + 1]["speech_start"]) - float(unit["speech_end"])
            if index + 1 < len(units) else 9999.0
        )
        if duration <= 0.5 and token_count <= 2 and index + 1 < len(units) and following_gap <= 0.60:
            following = dict(units[index + 1])
            left_text = _clean(unit["source_text"])
            right_text = _clean(following["source_text"])
            left_word = _comparison_text(left_text.rstrip(".!?…。！？"))
            right_first = _comparison_text(right_text.split(maxsplit=1)[0] if right_text else "")
            combined_text = right_text if left_word and left_word == right_first else _clean(f"{left_text} {right_text}")
            following["speech_start"] = unit["speech_start"]
            following["source_text"] = combined_text
            following["source_transcript_ids"] = list(dict.fromkeys(
                list(unit["source_transcript_ids"]) + list(following["source_transcript_ids"])
            ))
            following["source_transcript_id"] = following["source_transcript_ids"][0]
            units[index + 1] = following
            index += 1
            continue
        merged_units.append(unit)
        index += 1
    units = merged_units

    for i, unit in enumerate(units, 1):
        unit["id"] = i
        unit["timestamp"] = f"{_fmt_ts(unit['speech_start'])} --> {_fmt_ts(unit['speech_end'])}"
    return units


def assign_full_coverage_boundaries(units: list[dict[str, Any]], video_duration: float) -> list[dict[str, Any]]:
    """Give every atomic narration unit a contiguous visual interval.

    Source speech timestamps remain stored as speech_start/speech_end. Gaps between
    spoken units are split at the midpoint so the final rendered video retains full
    source coverage without merging narration IDs.
    """
    if not units:
        return []
    out = [dict(x) for x in units]
    out[0]["video_start"] = 0.0
    for i in range(len(out) - 1):
        left = out[i]
        right = out[i + 1]
        le = float(left["speech_end"])
        rs = float(right["speech_start"])
        boundary = (le + rs) / 2.0 if rs >= le else max(le, rs)
        boundary = max(0.0, min(float(video_duration), boundary))
        left["video_end"] = round(boundary, 3)
        right["video_start"] = round(boundary, 3)
    out[-1]["video_end"] = round(float(video_duration), 3)
    for i, unit in enumerate(out):
        unit.setdefault("video_start", 0.0 if i == 0 else out[i - 1]["video_end"])
        unit.setdefault("video_end", round(float(unit["speech_end"]), 3))
    return out


def _batch_by_size(items: list[dict[str, Any]], *, max_items: int, max_chars: int) -> list[list[dict[str, Any]]]:
    batches: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    chars = 0
    for item in items:
        cost = len(str(item.get("source_text", ""))) + 96
        if current and (len(current) >= max_items or chars + cost > max_chars):
            batches.append(current)
            current = []
            chars = 0
        current.append(item)
        chars += cost
    if current:
        batches.append(current)
    return batches


def _source_lines(items: list[dict[str, Any]]) -> str:
    return "\n".join(
        f"[ID {int(x['id']):05d}] [{x['timestamp']}] {_clean(x['source_text'])}"
        for x in items
    )


def _translation_lines(items: list[dict[str, Any]]) -> str:
    return "\n".join(
        f"[ID {int(x['id']):05d}] [{x['timestamp']}] SOURCE: {_clean(x['source_text'])}\n"
        f"OUTPUT: {_clean(x.get('translation'))}"
        for x in items
    )


def _comparison_text(value: Any) -> str:
    return re.sub(r"[^\w\u1000-\u109f]+", "", _clean(value).lower(), flags=re.UNICODE)


def find_suspicious_adjacent_duplicates(
    items: list[dict[str, Any]],
    previous_context: list[dict[str, Any]] | None = None,
) -> list[tuple[int, int]]:
    """Find adjacent Burmese rows that repeat despite materially different sources."""
    sequence = list(previous_context[-1:] if previous_context else []) + list(items)
    suspicious: list[tuple[int, int]] = []
    for left, right in zip(sequence, sequence[1:]):
        left_tr = _comparison_text(left.get("translation"))
        right_tr = _comparison_text(right.get("translation"))
        if min(len(left_tr), len(right_tr)) < 12:
            continue
        translation_similarity = difflib.SequenceMatcher(None, left_tr, right_tr).ratio()
        if translation_similarity < 0.94:
            continue
        left_source = _comparison_text(left.get("source_text"))
        right_source = _comparison_text(right.get("source_text"))
        source_similarity = difflib.SequenceMatcher(None, left_source, right_source).ratio()
        if source_similarity < 0.86:
            suspicious.append((int(left["id"]), int(right["id"])))
    return suspicious


def _assert_no_adjacent_translation_duplicates(
    items: list[dict[str, Any]],
    previous_context: list[dict[str, Any]] | None = None,
) -> None:
    pairs = find_suspicious_adjacent_duplicates(items, previous_context)
    if pairs:
        labels = ", ".join(f"{left}/{right}" for left, right in pairs)
        raise ValueError(f"Suspicious adjacent duplicate Burmese translation IDs: {labels}")


def _extract_json_payload(raw_text: str) -> Any:
    raw = (raw_text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.IGNORECASE)
        raw = re.sub(r"\s*```$", "", raw)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        for opener, closer in (("{", "}"), ("[", "]")):
            a, b = raw.find(opener), raw.rfind(closer)
            if a >= 0 and b > a:
                try:
                    return json.loads(raw[a:b + 1])
                except json.JSONDecodeError:
                    pass
        raise


def _gemini_json(ai: Any, prompt: str, *, retries: int = 3, log: LogFn | None = None) -> Any:
    """Run one logical Gemini request with model/key failover.

    Fallback switches never consume JSON/validation retries, so the exact same
    translation/cleanup/audit batch is resent after each switch.
    """
    last: Exception | None = None
    attempt = 1
    while attempt <= retries:
        try:
            ai._enable_proxy()
            try:
                response = ai.client.models.generate_content(model=ai.text_model, contents=prompt)
            finally:
                ai._disable_proxy()
            return _extract_json_payload(getattr(response, "text", ""))
        except Exception as exc:
            last = exc
            active_key = str(getattr(ai, "_recap_active_key_label", "GEMINI_API_KEY"))
            active_model = str(getattr(ai, "_recap_active_model", getattr(ai, "text_model", "Gemini")))

            if _is_gemini_key_rejection(exc):
                if log:
                    log(f"⚠️ {active_model} | {active_key} quota/auth rejection; trying next key...")
                rotate = getattr(ai, "_recap_rotate_gemini_key", None)
                if callable(rotate):
                    try:
                        if rotate():
                            continue  # exact same prompt; validation retry is untouched
                    except Exception as rotate_exc:
                        raise RuntimeError(f"Gemini key switch failed: {rotate_exc}") from rotate_exc

                # All keys in THIS model were rejected. Move to next model and Key 1.
                next_model = getattr(ai, "_recap_advance_after_key_pool", None)
                if callable(next_model):
                    try:
                        if next_model():
                            continue
                    except Exception as model_exc:
                        raise RuntimeError(f"Gemini model switch failed: {model_exc}") from model_exc

                pool_size = int(getattr(ai, "_recap_key_pool_size", 1) or 1)
                model_count = int(getattr(ai, "_recap_model_pool_size", 1) or 1)
                if log:
                    log(f"⛔ Gemini fallback exhausted at {active_model}; no model/key remains.")
                raise GeminiKeyPoolExhaustedError(_gemini_key_pool_message(pool_size, model_count)) from exc

            if _is_gemini_transient_model_error(exc):
                # Required behavior: 503 / high-demand / timeout skips the rest of
                # the current model's keys and starts the next model from Key 1.
                if log:
                    log(f"⚠️ {active_model} service/timeout error; switching to next model from Key 1...")
                next_model = getattr(ai, "_recap_advance_gemini_model", None)
                if callable(next_model):
                    try:
                        if next_model("503/high-demand/timeout"):
                            continue  # exact same prompt; validation retry is untouched
                    except Exception as model_exc:
                        raise RuntimeError(f"Gemini model switch failed: {model_exc}") from model_exc
                if log:
                    log(f"⛔ Final Gemini model failed with service/timeout error: {exc}")
                raise RuntimeError(f"Gemini fallback chain exhausted after service/timeout error: {exc}") from exc

            if log:
                log(f"⚠️ Gemini JSON attempt {attempt}/{retries} failed: {exc}")
            if attempt < retries:
                time.sleep(1.2 * attempt)
            attempt += 1
    raise RuntimeError(f"Gemini JSON response failed after {retries} attempts: {last}")


def _load_runtime_config(runtime_root: Path) -> dict[str, Any]:
    path = runtime_root / "config" / "whisper.json"  # optional: whisper_backend / whisper_device / whisper_compute_type
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def build_story_context(
    ai: Any,
    units: list[dict[str, Any]],
    *,
    batch_max_items: int,
    log: LogFn,
    should_stop: Callable[[], bool],
    checkpoint_path: Path | None = None,
    source_fingerprint: str = "",
    reuse: bool = True,
) -> dict[str, Any]:
    """Create durable context memory in a separate pass before translation, resumably."""
    batches = _batch_by_size(units, max_items=batch_max_items, max_chars=24000)
    memory: dict[str, Any] = {
        "characters": [], "relationships": [], "places": [], "terms": [],
        "plot_state": [], "name_consistency": [], "unresolved": []
    }
    completed = 0
    if reuse and checkpoint_path and checkpoint_path.is_file():
        try:
            saved = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            if saved.get("planner_mode") == PLANNER_MODE and saved.get("source_fingerprint") == source_fingerprint:
                completed = int(saved.get("completed_batches", 0) or 0)
                saved_memory = saved.get("context")
                if 0 <= completed <= len(batches) and isinstance(saved_memory, dict):
                    memory = saved_memory
                    if completed:
                        log(f"♻️ Story Context resume — {completed}/{len(batches)} batches already done")
                else:
                    completed = 0
        except Exception:
            completed = 0
    for i, batch in enumerate(batches, 1):
        if i <= completed:
            continue
        if should_stop():
            raise InterruptedError("STOP requested during story-context pass")
        prompt = f"""
You are building CONTEXT MEMORY for a later translation task.
Do not translate the transcript now. Do not write narration. Do not summarize away named facts.
Read this next chronological source chunk and UPDATE the existing compact context memory.
The memory exists only to keep names, relationships, terminology, locations, identities,
and the current story state consistent when later translation requests are sent in separate API calls.

RULES:
- Preserve exact character/place/object names as they appear in the source when known.
- Keep important relationships, aliases, roles, reveals, and terminology.
- Keep plot_state chronological but compact. Do not invent anything.
- If an earlier assumption is corrected by this chunk, update it.
- Return JSON only with these keys:
  characters, relationships, places, terms, plot_state, name_consistency, unresolved
- Each key must contain a compact JSON array. Keep the total memory concise enough to reuse in every later request.

EXISTING MEMORY:
{json.dumps(memory, ensure_ascii=False)}

SOURCE CHUNK {i}/{len(batches)}:
{_source_lines(batch)}
"""
        data = _gemini_json(ai, prompt, log=log)
        if not isinstance(data, dict):
            raise RuntimeError("Story context response must be a JSON object")
        memory = {
            "characters": list(data.get("characters") or []),
            "relationships": list(data.get("relationships") or []),
            "places": list(data.get("places") or []),
            "terms": list(data.get("terms") or []),
            "plot_state": list(data.get("plot_state") or []),
            "name_consistency": list(data.get("name_consistency") or []),
            "unresolved": list(data.get("unresolved") or []),
        }
        if checkpoint_path:
            _atomic_json(checkpoint_path, {
                "planner_mode": PLANNER_MODE,
                "source_fingerprint": source_fingerprint,
                "completed_batches": i,
                "total_batches": len(batches),
                "context": memory,
            })
        log(f"🧠 Story Context {i}/{len(batches)} updated")
    return memory


def _validate_translation_items(
    source_batch: list[dict[str, Any]],
    payload: Any,
    *,
    log: LogFn | None = None,
) -> list[dict[str, Any]]:
    items = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        raise ValueError("Translation response must contain an items array")
    expected = [int(x["id"]) for x in source_batch]
    got: list[int] = []
    by_id: dict[int, dict[str, Any]] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            uid = int(item.get("id"))
        except Exception:
            continue
        if uid in by_id:
            raise ValueError(f"Duplicate translation ID: {uid}")
        tr = _sanitize_translation_text(item.get("translation"))
        if not tr:
            raise ValueError(f"Empty translation ID: {uid}")
        source_row = next((src for src in source_batch if int(src["id"]) == uid), None)
        if source_row is not None and _has_speakable_content(source_row.get("source_text")) and not _has_speakable_content(tr):
            raise ValueError(
                f"Translation ID {uid} contains no speakable words (got {tr!r}); "
                "do not return ellipsis/placeholders"
            )
        by_id[uid] = item
        got.append(uid)
    if got != expected:
        raise ValueError(f"ID mismatch. expected={expected[:3]}...{expected[-3:]} got={got[:3]}...{got[-3:]}")
    out = []
    for src in source_batch:
        uid = int(src["id"])
        raw = by_id[uid]
        # Gemini does not own timing. Older/cached prompt variants may still return a
        # timestamp and models occasionally typo or copy a neighbouring row's value.
        # Never let that discard an otherwise ID-complete translation batch: the
        # locally measured transcript timestamp remains authoritative in ``src``.
        returned_ts = _clean(raw.get("timestamp"))
        if returned_ts and returned_ts != src["timestamp"] and log:
            log(
                f"ℹ️ Ignored Gemini timestamp for ID {uid}: {returned_ts}; "
                f"using local {src['timestamp']}"
            )
        out.append({**src, "translation": _sanitize_translation_text(raw.get("translation"))})
    return out


def translate_batch(
    ai: Any,
    *,
    story_context: dict[str, Any],
    previous_context: list[dict[str, Any]],
    current: list[dict[str, Any]],
    next_context: list[dict[str, Any]],
    log: LogFn,
    _split_depth: int = 0,
) -> list[dict[str, Any]]:
    task_rules = """
You are a CONTEXT-AWARE BURMESE NARRATION TRANSLATOR.
Translate ONLY the CURRENT English items into smooth, natural spoken Burmese that sounds as if it was originally written by a fluent Burmese movie/story narrator.

CORE PRINCIPLE:
Preserve the SOURCE MEANING and VISUAL EVENTS 100%, NOT the original wording or sentence structure.

NATIVE BURMESE NARRATOR STYLE:
- Use natural spoken Burmese narrator phrasing, rhythm, and connectors.
- Avoid stiff word-for-word translation; restructure wording and clause order INSIDE THE SAME ID when needed.
- Keep Burmese pronouns, kinship terms, honorifics, gendered self-reference, names, and relationships contextually correct.
- Keep dialogue intent and social register natural without turning ordinary speech into formal literary Burmese.

HARD RULES:
1. Do NOT remove, condense away, or omit meaningful source information. Every action, event, object, person, relationship, cause/effect, dialogue, and important visual detail must remain in the same ID.
2. Do NOT translate word-for-word when it sounds stiff or machine-like. You MAY restructure wording and clause order INSIDE THE SAME ID.
3. Small expansion is allowed only for clarity/natural flow; never exaggerate or add unrelated filler.
4. Do not invent plot events, opinions, hooks, intros, outros, or facts.
5. Keep every CURRENT ID exactly once, in order. Do not merge or split IDs.
6. Context is READ-ONLY. Never copy events from previous/next context into a current ID.
7. Maintain consistent names, relationships, places, objects, and terminology.
8. Timestamps are local metadata. Return no timestamps.
9. Return JSON only with top-level key "items". Each item must contain integer "id" and non-empty string "translation".
10. NEVER return placeholders such as "...", "…", dashes, empty text, or schema/example text. Every spoken SOURCE row must produce actual target-language narration words.
"""
    prompt = f"""
{task_rules}

STORY CONTEXT MEMORY:
{json.dumps(story_context, ensure_ascii=False)}

PREVIOUS CONTEXT (READ-ONLY):
{_translation_lines(previous_context) if previous_context else '(none)'}

CURRENT ITEMS — CONVERT ALL OF THESE, ONE-FOR-ONE:
{_source_lines(current)}

NEXT CONTEXT (READ-ONLY LOOK-AHEAD):
{_source_lines(next_context) if next_context else '(none)'}

CRITICAL OUTPUT ID PROTOCOL:
- CURRENT IDs are immutable SOURCE IDs, not row positions.
- Return exactly {len(current)} items.
- Copy this exact ID sequence without dropping, renumbering, or inventing any ID:
{json.dumps([int(x["id"]) for x in current], ensure_ascii=False)}
- Before responding, verify that items[].id exactly equals that sequence.
"""
    last: Exception | None = None
    for attempt in range(1, 4):
        try:
            payload = _gemini_json(ai, prompt, retries=1, log=log)
            translated = _validate_translation_items(current, payload, log=log)
            _assert_no_adjacent_translation_duplicates(translated, previous_context)
            stray = [f"ID {int(x['id']):05d}: {' '.join(_foreign_letters(x['translation']))}"
                     for x in translated if _foreign_letters(x["translation"])]
            if stray and attempt < 3:  # the last answer is kept; build() drops the letters and logs the line
                raise ValueError("letters of another script inside the Burmese (" + "; ".join(stray) +
                                 "); write every word in Burmese script")
            return translated
        except GeminiKeyPoolExhaustedError:
            raise
        except Exception as exc:
            last = exc
            log(f"⚠️ Translation validation attempt {attempt}/3 failed: {exc}")
            exact_ids = [int(x["id"]) for x in current]
            prompt += (
                f"\n\nCORRECTION REQUIRED: Previous response was invalid because: {exc}. "
                f"Return the full CURRENT batch again with exactly {len(exact_ids)} items. "
                f"The exact items[].id sequence MUST be {json.dumps(exact_ids, ensure_ascii=False)}. "
                "Do not omit the final ID, renumber rows, or merge adjacent IDs."
            )

    # V26 translation-ID resilience: V23's short local-sync segmentation creates many
    # more IDs per long video.  Some Gemini responses occasionally omit the last row
    # of an otherwise-correct 40-item translation batch (for example 401..439 instead
    # of 401..440).  After the normal retries, split only this failed batch into smaller
    # windows and translate those same source IDs.  Completed checkpoints, timestamps,
    # segmentation, narration mode, TTS, and Smart Sync remain unchanged.
    if len(current) > 6:
        mid = len(current) // 2
        left_rows = current[:mid]
        right_rows = current[mid:]
        expected_ids = [int(x["id"]) for x in current]
        prefix = "   " * _split_depth
        log(
            f"{prefix}↪️ Translation ID protocol fallback — splitting IDs "
            f"{expected_ids[0]:05d}–{expected_ids[-1]:05d} into "
            f"{len(left_rows)} + {len(right_rows)} smaller translation window(s)"
        )
        context_limit = max(1, len(previous_context) or 6)
        lookahead_limit = max(6, len(next_context) or 0)
        left_next = (right_rows + list(next_context or []))[:lookahead_limit]
        translated_left = translate_batch(
            ai,
            story_context=story_context,
            previous_context=list(previous_context or [])[-context_limit:],
            current=left_rows,
            next_context=left_next,
            log=log,
            _split_depth=_split_depth + 1,
        )
        right_prev = (list(previous_context or []) + translated_left)[-context_limit:]
        translated_right = translate_batch(
            ai,
            story_context=story_context,
            previous_context=right_prev,
            current=right_rows,
            next_context=list(next_context or []),
            log=log,
            _split_depth=_split_depth + 1,
        )
        combined = translated_left + translated_right
        _assert_no_adjacent_translation_duplicates(combined, list(previous_context or []))
        _assert_speakable_translation_items(combined)
        return combined

    raise RuntimeError(f"Translation batch could not be validated: {last}")


def audit_and_repair_batch(
    ai: Any,
    translated: list[dict[str, Any]],
    *,
    story_context: dict[str, Any],
    previous_context: list[dict[str, Any]] | None = None,
    next_context: list[dict[str, Any]] | None = None,
    log: LogFn,
) -> list[dict[str, Any]]:
    """Mandatory per-ID ownership/completeness audit; only failed rows may be rewritten.

    V24 hardens the audit protocol for V23's larger ID numbers.  Gemini occasionally
    renumbered a late batch (for example IDs 01018-01055) as small local IDs even
    though the translation itself had already passed validation.  We now anchor the
    prompt to the exact current IDs and, if the model still returns the wrong ID set,
    automatically re-audit the *same translated rows* in smaller windows.  Translation,
    timestamps, segmentation, TTS and Smart Sync are not changed by this fallback.
    """
    previous_context = list(previous_context or [])
    next_context = list(next_context or [])
    target_name = "Burmese/Myanmar narrator output"

    def _audit_window(
        current_rows: list[dict[str, Any]],
        prev_rows: list[dict[str, Any]],
        next_rows: list[dict[str, Any]],
        *,
        depth: int = 0,
    ) -> list[dict[str, Any]]:
        expected_ids = [int(item["id"]) for item in current_rows]
        if not expected_ids:
            return []
        exact_ids_json = json.dumps(expected_ids, ensure_ascii=False)
        sample_checks = [{"id": expected_ids[0], "status": "pass"}]
        if len(expected_ids) > 1:
            sample_checks.append({
                "id": expected_ids[1],
                "status": "repair",
                "translation": f"complete corrected output for ID {expected_ids[1]} only",
            })
        sample_json = json.dumps({"checks": sample_checks}, ensure_ascii=False)
        base_prompt = f"""
You are a STRICT SENTENCE-LOCKED TRANSLATION AUDITOR.
Audit the {target_name}. Every action/fact/object/dialogue/detail must remain inside the same ID as its SOURCE.
PREVIOUS and NEXT context are READ-ONLY. This is never a summarization task.

CRITICAL ID PROTOCOL:
- CURRENT IDs are SOURCE IDs, not positions and not local row numbers.
- NEVER renumber, restart, shorten, substitute, or invent IDs.
- Copy each CURRENT ID exactly as printed, including large IDs above 999.
- Return exactly {len(expected_ids)} checks.
- The exact required CURRENT ID sequence is: {exact_ids_json}

For EVERY CURRENT ID check:
- semantic completeness: no meaningful source information omitted or condensed away;
- no event borrowed from previous/next IDs;
- no shifted chain where a row translates another row;
- no adjacent duplicate/near-duplicate outputs for different sources;
- natural spoken narrator style, stable names/pronouns, and no invented information.

Return JSON ONLY with every CURRENT ID once and in the exact sequence above.
Example shape using ACTUAL IDs from this batch:
{sample_json}
Use status=pass to preserve a good translation byte-for-byte. Use repair only when necessary.

STORY CONTEXT:
{json.dumps(story_context, ensure_ascii=False)}

PREVIOUS APPROVED CONTEXT (READ-ONLY):
{_translation_lines(prev_rows) if prev_rows else '(none)'}

CURRENT ITEMS TO AUDIT:
{_translation_lines(current_rows)}

NEXT SOURCE CONTEXT (READ-ONLY):
{_source_lines(next_rows) if next_rows else '(none)'}

FINAL ID CHECK BEFORE YOU RESPOND:
Your checks[].id list MUST equal exactly: {exact_ids_json}
"""
        prompt = base_prompt
        last_error: Exception | None = None
        for attempt in range(1, 3):
            try:
                payload = _gemini_json(ai, prompt, retries=2, log=log)
                checks = payload.get("checks") if isinstance(payload, dict) else None
                if not isinstance(checks, list):
                    raise ValueError("audit response must contain a checks array")
                got_ids: list[int] = []
                by_id = {int(item["id"]): dict(item) for item in current_rows}
                repaired = 0
                for check in checks:
                    if not isinstance(check, dict):
                        raise ValueError("every audit check must be an object")
                    uid = int(check.get("id"))
                    got_ids.append(uid)
                    status = _clean(check.get("status")).lower()
                    if status == "pass":
                        continue
                    if status != "repair":
                        raise ValueError(f"invalid audit status for ID {uid}: {status}")
                    replacement = _clean(check.get("translation"))
                    if uid not in by_id or not replacement:
                        raise ValueError(f"missing repair translation for ID {uid}")
                    by_id[uid]["translation"] = replacement
                    repaired += 1
                if got_ids != expected_ids:
                    raise ValueError(f"audit ID mismatch expected={expected_ids} got={got_ids}")
                audited = [by_id[uid] for uid in expected_ids]
                _assert_no_adjacent_translation_duplicates(audited, prev_rows)
                _assert_speakable_translation_items(audited)
                prefix = "   " * depth
                log(
                    f"{prefix}🛠️ Sentence-lock audit repaired {repaired} item(s)"
                    if repaired else
                    f"{prefix}✅ Sentence-lock audit: every ID passed"
                )
                return audited
            except GeminiKeyPoolExhaustedError:
                raise
            except Exception as exc:
                last_error = exc
                prefix = "   " * depth
                log(f"{prefix}⚠️ Sentence-lock audit attempt {attempt}/2 failed: {exc}")
                prompt = (
                    base_prompt
                    + f"\n\nCORRECTION REQUIRED: {exc}. "
                      f"Return the full checks array for every CURRENT ID. "
                      f"The exact ID list is {exact_ids_json}; do not renumber it."
                )

        # V23 creates roughly twice as many short local-sync IDs as the older planner.
        # If a large late batch still confuses Gemini's audit response, preserve the
        # already-validated translations and audit them in smaller windows instead of
        # aborting the whole multi-hour job.
        if len(current_rows) > 6:
            mid = len(current_rows) // 2
            left_rows = current_rows[:mid]
            right_rows = current_rows[mid:]
            prefix = "   " * depth
            log(
                f"{prefix}↪️ Audit ID protocol fallback — splitting IDs "
                f"{expected_ids[0]:05d}–{expected_ids[-1]:05d} into "
                f"{len(left_rows)} + {len(right_rows)} smaller audit window(s)"
            )
            context_limit = max(1, len(previous_context) or 6)
            left_next = (right_rows + next_rows)[:max(6, len(next_context) or 0)]
            audited_left = _audit_window(
                left_rows,
                prev_rows[-context_limit:],
                left_next,
                depth=depth + 1,
            )
            right_prev = (prev_rows + audited_left)[-context_limit:]
            audited_right = _audit_window(
                right_rows,
                right_prev,
                next_rows,
                depth=depth + 1,
            )
            combined = audited_left + audited_right
            _assert_no_adjacent_translation_duplicates(combined, prev_rows)
            _assert_speakable_translation_items(combined)
            return combined

        raise RuntimeError(f"Sentence-lock translation audit could not be validated: {last_error}")

    return _audit_window(translated, previous_context, next_context)

def make_translation_plan(units: list[dict[str, Any]], video_duration: float, *, source_title: str, transcript: dict[str, Any], story_context: dict[str, Any]) -> dict[str, Any]:
    # V32 global Smart Sync boundary: the original V2 visual-timing policy is used
    # by all source modes. Speech timestamps stay authoritative; only visual coverage
    # is expanded so gaps are split at midpoint and the source timeline stays contiguous.
    covered = assign_full_coverage_boundaries(units, video_duration)
    segments = []
    for unit in covered:
        tr = _clean(unit.get("translation"))
        if not tr:
            raise RuntimeError(f"Missing translation for ID {unit.get('id')}")
        if _has_speakable_content(unit.get("source_text")) and not _has_speakable_content(tr):
            raise RuntimeError(
                f"Invalid non-speech translation for ID {unit.get('id')}: {tr!r}. "
                "Translation must be regenerated before TTS."
            )
        source_ids = [int(value) for value in (unit.get("source_transcript_ids") or [unit["source_transcript_id"]])]
        segments.append({
            "source_segment_ids": source_ids,
            "atomic_id": int(unit["id"]),
            "gemini_start": round(float(unit["speech_start"]), 3),
            "gemini_end": round(float(unit["speech_end"]), 3),
            "timestamp": unit["timestamp"],
            "video_start": round(float(unit["video_start"]), 3),
            "video_end": round(float(unit["video_end"]), 3),
            "source_text": unit["source_text"],
            "narration": tr,
            "translation": tr,
            "semantic_boundary": str(unit.get("semantic_boundary") or ""),
            "forced_boundary": bool(unit.get("forced_boundary", False)),
            "visual_reason": "V2-style local timestamp translation anchor",
        })
    return {
        "planner_mode": PLANNER_MODE,
        "translation_task": True,
        "summary_task": False,
        "original_title": source_title,
        "source_duration": round(float(video_duration), 3),
        "transcript_backend": transcript.get("backend", ""),
        "transcript_model": transcript.get("model", ""),
        "transcript_language": transcript.get("language", ""),
        "story_context": story_context,
        "segments": segments,
    }


class RecapPlanBuilder:
    """English narrator video → timestamp-locked Burmese narration plan (narrator-only source)."""

    def __init__(
        self,
        *,
        runtime_root: str,
        gemini_env_path: str = "",
        ffprobe_path: str,
        log: LogFn,
        progress: ProgressFn,
        should_stop: Callable[[], bool],
        translation_batch_max_items: int = 40,
        story_context_batch_max_items: int = 80,
        context_overlap: int = 6,
        lookahead: int = 6,
        translation_audit: bool = True,
        atomic_max_duration: float = 5.6,
        atomic_target_duration: float = 4.2,
        gemini_request_timeout_seconds: int = 120,
        gemini_text_model: str = "gemini-3.5-flash",
    ):
        self.runtime_root = Path(runtime_root).resolve()
        self.gemini_env_path = Path(gemini_env_path).expanduser().resolve() if str(gemini_env_path or "").strip() else None
        self.ffprobe = ffprobe_path
        # Prefer ffmpeg next to ffprobe; fallback to PATH.
        fp = Path(ffprobe_path)
        sibling = fp.with_name("ffmpeg.exe" if fp.suffix.lower() == ".exe" else "ffmpeg")
        self.ffmpeg = str(sibling) if sibling.is_file() else (shutil.which("ffmpeg") or "ffmpeg")
        self.log = log
        self.progress = progress
        self.should_stop = should_stop
        self.translation_batch_max_items = max(5, int(translation_batch_max_items))
        self.story_context_batch_max_items = max(10, int(story_context_batch_max_items))
        self.context_overlap = max(0, int(context_overlap))
        self.lookahead = max(0, int(lookahead))
        self.translation_audit = bool(translation_audit)
        self.atomic_max_duration = max(4.0, float(atomic_max_duration))
        self.atomic_target_duration = max(2.5, min(float(atomic_target_duration), self.atomic_max_duration))
        self.gemini_request_timeout_seconds = max(30, min(int(gemini_request_timeout_seconds), 600))
        self.gemini_text_model = _clean(gemini_text_model) or "gemini-3.5-flash"

    def _validate_runtime(self) -> None:
        required = [
            self.runtime_root / "services" / "whisper_service.py",
            self.runtime_root / "services" / "ai_service.py",
        ]
        missing = [str(p) for p in required if not p.is_file()]
        if missing:
            raise FileNotFoundError("Bundled runtime မပြည့်စုံပါ:\n" + "\n".join(missing))

    @staticmethod
    def _release_whisper_gpu(whisper_obj: Any) -> None:
        try:
            if hasattr(whisper_obj, "_model"):
                whisper_obj._model = None
        except Exception:
            pass
        try:
            del whisper_obj
        except Exception:
            pass
        gc.collect()

    def build(self, *, source: Path, job_dir: Path, source_title: str = "", reuse: bool = True) -> tuple[Path, dict[str, Any]]:
        self._validate_runtime()
        source = source.resolve()
        job_dir.mkdir(parents=True, exist_ok=True)
        whisper_model, whisper_language = WHISPER_MODEL, WHISPER_LANGUAGE
        transcript_file = job_dir / "whisper_transcript.json"
        transcript_text_file = job_dir / "whisper_transcript.txt"
        atomic_file = job_dir / "atomic_units.json"
        context_file = job_dir / "story_context.json"
        context_checkpoint_file = job_dir / "story_context_checkpoint.json"
        translations_file = job_dir / "timestamp_translations.json"
        plan_file = job_dir / "smart_sync_plan.json"
        state_file = job_dir / "planner_checkpoint.json"
        fp = _source_fingerprint_for_mode(
            source, PLANNER_MODE + "|" + TRANSLATION_DIRECTION + f"|whisper:{whisper_model}:{whisper_language}")

        if reuse and plan_file.is_file() and transcript_file.is_file() and state_file.is_file():
            try:
                state = json.loads(state_file.read_text(encoding="utf-8"))
                plan = json.loads(plan_file.read_text(encoding="utf-8"))
                if (
                    state.get("source_fingerprint") == fp
                    and state.get("plan_ready") is True
                    and state.get("planner_mode") == PLANNER_MODE
                    and plan.get("planner_mode") == PLANNER_MODE
                    and str(state.get("segmentation_profile") or "") == SEGMENTATION_PROFILE
                    and str(plan.get("segmentation_profile") or "") == SEGMENTATION_PROFILE
                    and str(state.get("boundary_profile") or "") == BOUNDARY_PROFILE
                    and str(plan.get("boundary_profile") or "") == BOUNDARY_PROFILE
                ):
                    if _plan_has_speakable_narration(plan):
                        self.log("♻️ Timestamp transcript + Translation plan reuse")
                        self.progress(4, 4, "Plan reused")
                        return plan_file, plan
                    self.log("⚠️ Cached Smart Sync plan contains non-speech/placeholder narration; rebuilding translation from safe checkpoint")
            except Exception:
                pass

        duration = _probe_video_duration(self.ffprobe, source)
        audio_duration = _probe_audio_duration(self.ffprobe, source)
        self.log(f"🎬 Source: {source}")
        self.log(f"⏱️ Video duration (MASTER): {duration:.3f}s")
        if audio_duration is not None:
            self.log(f"🎧 Source audio duration: {audio_duration:.3f}s")
            if audio_duration > duration + 1.0:
                self.log(f"⚠️ Audio tail detected: +{audio_duration-duration:.3f}s beyond video; transcription audio preparation will trim it.")

        whisper_mod = _load_module("timestamp_whisper_service", self.runtime_root / "services" / "whisper_service.py")
        ai_mod = _load_module("timestamp_ai_service", self.runtime_root / "services" / "ai_service.py")

        env_file = self.gemini_env_path
        key_pool: list[tuple[str, str]] = []
        if env_file and env_file.is_file():
            from dotenv import dotenv_values, load_dotenv
            load_dotenv(env_file, override=True)
            self.log(f"🔑 Gemini .env loaded: {env_file}")
            env_values = dotenv_values(env_file)
            seen_keys: set[str] = set()
            # Every GEMINI_IMAGE_PROJECT_<N>_KEY in numeric order, else GEMINI_API_KEY.
            numbers = sorted(
                int(m.group(1)) for m in (re.fullmatch(r"GEMINI_IMAGE_PROJECT_(\d+)_KEY", str(k or "").strip())
                                          for k in env_values) if m)
            for number in numbers:
                label = f"GEMINI_IMAGE_PROJECT_{number}_KEY"
                key = str(env_values.get(label) or "").strip()
                if key and key not in seen_keys:
                    key_pool.append((label, key))
                    seen_keys.add(key)
            if not key_pool:
                key = str(env_values.get("GEMINI_API_KEY") or "").strip()
                if key:
                    key_pool.append(("GEMINI_API_KEY", key))
        if not key_pool:
            inherited_key = str(os.getenv("GEMINI_API_KEY") or "").strip()
            if inherited_key:
                key_pool.append(("GEMINI_API_KEY", inherited_key))
        if not key_pool:
            raise RuntimeError(
                "Gemini API key မတွေ့ပါ။ .env ထဲမှာ GEMINI_API_KEY=... "
                "(သို့) GEMINI_IMAGE_PROJECT_1_KEY, GEMINI_IMAGE_PROJECT_2_KEY ... ထည့်ပေးပါ။"
            )
        os.environ["GEMINI_API_KEY"] = key_pool[0][1]
        os.environ["GEMINI_TEXT_MODEL"] = self.gemini_text_model
        self.log(f"🔑 Gemini key pool ready — {len(key_pool)} key(s) | translation only (English source uses local Whisper {whisper_model})")

        transcript = None
        if reuse and transcript_file.is_file() and state_file.is_file():
            try:
                state = json.loads(state_file.read_text(encoding="utf-8"))
                if state.get("source_fingerprint") == fp and state.get("transcript_ready") is True:
                    candidate = json.loads(transcript_file.read_text(encoding="utf-8"))
                    if (
                        candidate.get("backend") in {"faster-whisper", "openai-whisper"}
                        and candidate.get("model") == whisper_model
                        and str(candidate.get("language") or "").lower().startswith(whisper_language.lower())
                    ):
                        transcript = candidate
                        if not transcript_text_file.is_file():
                            transcript_text_file.write_text(str(transcript.get("text") or ""), encoding="utf-8")
                        self.log("♻️ Full local Whisper transcript reuse")
            except Exception:
                transcript = None

        if transcript is None:
            if self.should_stop():
                raise InterruptedError("STOP requested before local Whisper")
            runtime_cfg = _load_runtime_config(self.runtime_root)
            self.log(f"🌐 English → Myanmar | Local Whisper language={whisper_language} | model={whisper_model}")
            self.progress(0, 4, "Whisper timestamp transcript")
            self.log("🎙️ [1/4] Local Whisper — full video timestamp transcript...")
            whisper = whisper_mod.WhisperTimestampService(
                model_size=whisper_model,
                backend=str(runtime_cfg.get("whisper_backend", os.getenv("WHISPER_BACKEND", "auto"))),
                device=str(runtime_cfg.get("whisper_device", os.getenv("WHISPER_DEVICE", "cuda"))),
                compute_type=str(runtime_cfg.get("whisper_compute_type", os.getenv("WHISPER_COMPUTE_TYPE", "float16"))),
                language=whisper_language,
            )
            whisper_media = _prepare_whisper_audio(self.ffmpeg, source, job_dir, duration, self.log)
            transcript = whisper.transcribe(str(whisper_media), duration)
            transcript["source_title"] = source_title or source.stem
            transcript["backend"] = transcript.get("backend") or "faster-whisper"
            transcript["model"] = whisper_model
            transcript["language"] = whisper_language
            _atomic_json(transcript_file, transcript)
            transcript_text_file.write_text(str(transcript.get("text") or ""), encoding="utf-8")
            self.log(f"✅ Whisper raw segments: {len(transcript.get('segments') or [])}")
            self.log(f"📝 Raw transcript: {transcript_text_file.name}")
            self._release_whisper_gpu(whisper)

        self.log("✅ Narrator source: all transcript speech kept; original V2 Smart Sync boundaries")
        units = atomicize_timestamp_segments(
            transcript,
            max_duration=self.atomic_max_duration,
            target_duration=self.atomic_target_duration,
        )
        units = _filter_local_whisper_artifact_units(units, transcript, log=self.log)
        if not units:
            raise RuntimeError("Timestamp transcript units မရှိပါ။")
        _atomic_json(atomic_file, {
            "planner_mode": PLANNER_MODE,
            "segmentation_profile": SEGMENTATION_PROFILE,
            "units": units,
        })
        forced_units = sum(1 for unit in units if unit.get("forced_boundary"))
        durations = [max(0.0, float(unit["speech_end"]) - float(unit["speech_start"])) for unit in units]
        avg_duration = (sum(durations) / len(durations)) if durations else 0.0
        self.log(
            f"🧩 V2-style local sync units: {len(units)} | avg={avg_duration:.2f}s | "
            f"target={self.atomic_target_duration:.1f}s max={self.atomic_max_duration:.1f}s | forced={forced_units}"
        )

        _atomic_json(state_file, {
            "version": 5,
            "planner_mode": PLANNER_MODE,
            "segmentation_profile": SEGMENTATION_PROFILE,
            "boundary_profile": BOUNDARY_PROFILE,
            "source_fingerprint": fp,
            "source_path": str(source),
            "translation_direction": TRANSLATION_DIRECTION,
            "transcript_backend": "faster-whisper",
            "transcript_ready": True,
            "plan_ready": False,
        })

        if self.should_stop():
            raise InterruptedError("STOP requested after timestamp transcription")

        with _pushd(self.runtime_root):
            ai = ai_mod.AIService()
            self.log(f"🤖 Gemini primary text model: {ai.text_model}")
            _configure_gemini_key_pool(
                ai,
                key_pool,
                client_factory=ai_mod.genai.Client,
                log=self.log,
                request_timeout_seconds=self.gemini_request_timeout_seconds,
            )

            self.progress(1, 4, "Story context memory")
            self.log("🧠 [2/4] Gemini Story Context Memory pass...")
            context_source_fp = fp + "|seg:" + SEGMENTATION_PROFILE
            story_context = build_story_context(
                ai, units,
                batch_max_items=self.story_context_batch_max_items,
                log=self.log,
                should_stop=self.should_stop,
                checkpoint_path=context_checkpoint_file,
                source_fingerprint=context_source_fp,
                reuse=reuse,
            )
            _atomic_json(context_file, {
                "planner_mode": PLANNER_MODE,
                "source_fingerprint": context_source_fp,
                "context": story_context,
            })

            self.progress(2, 4, "Timestamp translation")
            self.log("🌐 [3/4] Gemini English → Myanmar translation — narrator source | V2 Smart Sync | no summary / no shortening...")
            batches = _batch_by_size(units, max_items=self.translation_batch_max_items, max_chars=16000)
            translated_all: list[dict[str, Any]] = []
            if reuse and translations_file.is_file():
                try:
                    saved = json.loads(translations_file.read_text(encoding="utf-8"))
                    saved_items = saved.get("items") or []
                    if (saved.get("planner_mode") == PLANNER_MODE and saved.get("source_fingerprint") == fp
                            and str(saved.get("segmentation_profile") or "") == SEGMENTATION_PROFILE
                            and isinstance(saved_items, list)):
                        saved_by_id: dict[int, dict[str, Any]] = {}
                        for row in saved_items:
                            if not isinstance(row, dict):
                                continue
                            try:
                                uid = int(row.get("id"))
                            except Exception:
                                continue
                            saved_by_id.setdefault(uid, row)

                        safe_prefix: list[dict[str, Any]] = []
                        bad_id: int | None = None
                        for src in units:
                            uid = int(src["id"])
                            saved_row = saved_by_id.get(uid)
                            if saved_row is None:
                                bad_id = uid
                                break
                            if _clean(saved_row.get("source_text")) != _clean(src.get("source_text")):
                                bad_id = uid
                                break
                            saved_ts = _clean(saved_row.get("timestamp"))
                            if saved_ts and saved_ts != _clean(src.get("timestamp")):
                                bad_id = uid
                                break
                            tr = _sanitize_translation_text(saved_row.get("translation"))
                            if not _has_speakable_content(tr):
                                bad_id = uid
                                break
                            clean_saved = dict(saved_row)
                            clean_saved["translation"] = tr
                            safe_prefix.append(clean_saved)

                        translated_all = safe_prefix
                        ignored_obsolete = max(0, len(saved_items) - len(safe_prefix))
                        if len(safe_prefix) == len(units) and ignored_obsolete:
                            self.log(
                                f"♻️ Translation checkpoint sanitized — reusing all {len(safe_prefix)} valid unit(s); "
                                f"ignoring {ignored_obsolete} obsolete ASR-artifact row(s)"
                            )
                        elif bad_id is not None:
                            self.log(
                                f"⚠️ Translation checkpoint stops before ID {bad_id:05d}; "
                                "resume will regenerate from that batch"
                            )
                        if translated_all:
                            self.log(f"♻️ Timestamp Translation resume — {len(translated_all)}/{len(units)} safe units already done")
                except Exception as exc:
                    self.log(f"⚠️ Translation checkpoint ignored: {exc}")
                    translated_all = []

            unit_index = {int(x["id"]): idx for idx, x in enumerate(units)}
            completed_ids = {int(x["id"]) for x in translated_all}
            for bi, batch in enumerate(batches, 1):
                batch_ids = [int(x["id"]) for x in batch]
                if all(uid in completed_ids for uid in batch_ids):
                    continue
                if any(uid in completed_ids for uid in batch_ids):
                    # Checkpoints are written at complete-batch boundaries; a partial batch is unsafe to reuse.
                    translated_all = [x for x in translated_all if int(x["id"]) < batch_ids[0]]
                    completed_ids = {int(x["id"]) for x in translated_all}
                if self.should_stop():
                    raise InterruptedError("STOP requested during timestamp translation")
                last_index = unit_index[int(batch[-1]["id"])]
                prev = translated_all[-self.context_overlap:] if self.context_overlap else []
                nxt = units[last_index + 1:last_index + 1 + self.lookahead] if self.lookahead else []
                self.log(f"🌐 Translate batch {bi}/{len(batches)} — IDs {batch[0]['id']:05d}–{batch[-1]['id']:05d}")
                try:
                    translated = translate_batch(
                        ai,
                        story_context=story_context,
                        previous_context=prev,
                        current=batch,
                        next_context=nxt,
                        log=self.log,
                    )
                except GeminiKeyPoolExhaustedError as exc:
                    saved = len(translated_all)
                    remaining = len(units) - saved
                    self.log(f"💾 Translation checkpoint safe — {saved}/{len(units)} units saved; {remaining} remaining")
                    raise GeminiKeyPoolExhaustedError(
                        f"{exc}\n\nSaved progress: {saved}/{len(units)} units; "
                        f"remaining: {remaining}. Resume will start at ID {batch_ids[0]:05d}."
                    ) from exc
                if self.translation_audit:
                    translated = audit_and_repair_batch(
                        ai,
                        translated,
                        story_context=story_context,
                        previous_context=prev,
                        next_context=nxt,
                        log=self.log,
                    )
                translated = _drop_foreign_letters(translated, self.log)
                translated_all.extend(translated)
                completed_ids.update(int(x["id"]) for x in translated)
                _atomic_json(translations_file, {
                    "planner_mode": PLANNER_MODE,
                    "segmentation_profile": SEGMENTATION_PROFILE,
                    "source_fingerprint": fp,
                    "translation_direction": TRANSLATION_DIRECTION,
                    "completed": len(translated_all),
                    "total": len(units),
                    "items": translated_all,
                })

        if [int(x["id"]) for x in translated_all] != [int(x["id"]) for x in units]:
            raise RuntimeError("Final translation ID sequence mismatch")

        self.progress(3, 4, "Build Smart Sync plan")
        self.log("🧩 [4/4] V2 Smart Sync boundaries → full source coverage + midpoint gap split...")
        plan = make_translation_plan(
            translated_all,
            duration,
            source_title=source_title or source.stem,
            transcript=transcript,
            story_context=story_context,
        )
        plan["standalone_source_path"] = str(source)
        plan["segmentation_profile"] = SEGMENTATION_PROFILE
        plan["boundary_profile"] = BOUNDARY_PROFILE
        plan["translation_direction"] = TRANSLATION_DIRECTION
        plan["source_language"] = "en"
        plan["target_language"] = "mm"
        plan["translation_batch_count"] = len(batches)
        plan["atomic_unit_count"] = len(translated_all)
        plan["translation_rules"] = {
            "task": "translate",
            "summarize": False,
            "shorten": False,
            "merge_ids": False,
            "delete_ids": False,
            "reorder_ids": False,
            "natural_expansion_allowed": True,
            "new_plot_invention_allowed": False,
            "natural_boundary_preferred": True,
            "local_time_cap_may_split_long_sentence": True,
            "v2_local_sync_anchors": True,
            "sync_target_seconds": self.atomic_target_duration,
            "sync_max_seconds": self.atomic_max_duration,
            "context_read_only_for_each_id": True,
            "adjacent_duplicate_rejected": True,
            "mandatory_per_id_alignment_audit": bool(self.translation_audit),
            "timestamp_authority": "local-whisper",
            "boundary_profile": BOUNDARY_PROFILE,
        }
        _atomic_json(plan_file, plan)
        _atomic_json(state_file, {
            "version": 5,
            "planner_mode": PLANNER_MODE,
            "segmentation_profile": SEGMENTATION_PROFILE,
            "boundary_profile": BOUNDARY_PROFILE,
            "source_fingerprint": fp,
            "source_path": str(source),
            "translation_direction": TRANSLATION_DIRECTION,
            "transcript_ready": True,
            "context_ready": True,
            "translation_ready": True,
            "plan_ready": True,
            "atomic_units": len(translated_all),
        })
        self.progress(4, 4, "Plan ready")
        self.log(f"✅ smart_sync_plan.json READY — V2-style local sync units={len(plan['segments'])}")
        return plan_file, plan
