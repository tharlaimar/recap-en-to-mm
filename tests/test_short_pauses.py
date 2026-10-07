from __future__ import annotations

import json
import math
import struct
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))

import core

RATE = 24000


def make_clip(path: Path, lead: float, voice: float, tail: float) -> None:
    """Edge TTS-like clip: silence, a tone standing in for the voice, silence (mono 16-bit 24 kHz)."""
    frames = []
    for i in range(int((lead + voice + tail) * RATE)):
        t = i / RATE
        loud = lead <= t < lead + voice
        frames.append(struct.pack("<h", int(8000 * math.sin(2 * math.pi * 440 * t)) if loud else 0))
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(RATE)
        writer.writeframes(b"".join(frames))


def wav_seconds(path: Path) -> float:
    with wave.open(str(path), "rb") as reader:
        return reader.getnframes() / reader.getframerate()


class TrimEdgeSilenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.clip = self.dir / "S0001.wav"
        make_clip(self.clip, lead=0.21, voice=1.0, tail=0.76)  # measured Edge TTS padding

    def test_join_pause_becomes_about_a_quarter_second(self) -> None:
        before = self.clip.read_bytes()
        used, seconds = core.trim_edge_silence(self.clip, self.dir / "short" / "S0001.wav")
        self.assertAlmostEqual(seconds, 0.05 + 1.0 + 0.20, delta=0.02)
        self.assertAlmostEqual(wav_seconds(used), seconds, delta=0.001)
        self.assertEqual(self.clip.read_bytes(), before)  # the TTS cache is never changed

    def test_never_shorter_than_the_picture_allows(self) -> None:
        _used, seconds = core.trim_edge_silence(self.clip, self.dir / "short" / "S0001.wav", min_duration=1.6)
        self.assertAlmostEqual(seconds, 1.6, delta=0.001)
        used, seconds = core.trim_edge_silence(self.clip, self.dir / "short" / "S0001.wav", min_duration=5.0)
        self.assertEqual(used, self.clip)  # already shorter than the floor: left as it was
        self.assertAlmostEqual(seconds, 1.97, delta=0.001)

    def test_silent_clip_is_left_alone(self) -> None:
        silent = self.dir / "silent.wav"
        make_clip(silent, lead=0.5, voice=0.0, tail=0.5)
        used, seconds = core.trim_edge_silence(silent, self.dir / "short" / "silent.wav")
        self.assertEqual(used, silent)
        self.assertAlmostEqual(seconds, 1.0, delta=0.001)

    def test_second_run_reuses_the_trimmed_clip(self) -> None:
        target = self.dir / "short" / "S0001.wav"
        core.trim_edge_silence(self.clip, target)
        stamp = target.stat().st_mtime_ns
        core.trim_edge_silence(self.clip, target)
        self.assertEqual(target.stat().st_mtime_ns, stamp)


class ShortPausesPipelineTests(unittest.TestCase):
    def _run(self, short_pauses: bool) -> tuple[list[float], dict]:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            job = base / "job"
            job.mkdir()
            source = base / "source.mp4"
            source.write_bytes(b"source")
            plan_path = job / "smart_sync_plan.json"
            plan_path.write_text(json.dumps({"segments": [
                # window 1.2 s: floor 0.96 s, trimmed clip 1.25 s fits
                {"video_start": 0.0, "video_end": 1.2, "narration": "ပထမ စာကြောင်း။"},
                # window 3.6 s: floor 2.88 s, trimmed clip 2.25 s would cut the picture -> 2.88 s kept
                {"video_start": 1.2, "video_end": 4.8, "narration": "ဒုတိယ စာကြောင်း။"},
            ]}, ensure_ascii=False), encoding="utf-8")
            render_targets: list[float] = []

            class FakeEdgeEngine:
                def __init__(self, *, ffmpeg, work_dir, rate, pitch, log):
                    self.work_dir = Path(work_dir)
                    self.work_dir.mkdir(parents=True, exist_ok=True)
                    self.counter = 0

                def generate_channel99_voice(self, text, voice):
                    self.counter += 1
                    out = self.work_dir / f"fake_{self.counter}.wav"
                    make_clip(out, lead=0.2, voice=1.0 if self.counter == 1 else 2.0, tail=0.8)
                    return str(out)

            def probe(_ffprobe, path):
                try:
                    return wav_seconds(Path(path))  # the TTS clips are real WAVs
                except (wave.Error, EOFError, OSError):
                    return 9.0  # the fake joined voice / video files

            def fake_render(_ffmpeg, _source, dst, _seg, target, _encoder, *_args, **_kwargs):
                render_targets.append(round(float(target), 3))
                Path(dst).write_bytes(b"v" * 6000)
                return {"hybrid_used": 0.0, "applied_factor": 1.0}

            runner = core.EdgeSmartSync(
                job_dir=str(job), source_path=str(source), plan_path=str(plan_path),
                voice="my-MM-ThihaNeural", ffmpeg_path="/bin/true", ffprobe_path="/bin/true",
                render_final_video=True, mirror_video=False, zoom_factor=1.0, max_video_speed=1.25)
            runner.short_pauses = short_pauses
            with mock.patch.object(core, "EdgeTTSEngine", FakeEdgeEngine), \
                 mock.patch.object(core, "probe_duration", side_effect=probe), \
                 mock.patch.object(core, "concat_audio", side_effect=lambda _f, _p, out, _w: Path(out).write_bytes(b"a" * 2000)), \
                 mock.patch.object(core, "ffmpeg_encoders", return_value={"libx264"}), \
                 mock.patch.object(core, "render_video_part", side_effect=fake_render), \
                 mock.patch.object(core, "concat_video_parts", side_effect=lambda _f, _p, out, _w: Path(out).write_bytes(b"v" * 6000)), \
                 mock.patch.object(core, "mux_audio", side_effect=lambda _f, _v, _a, out, *_x: Path(out).write_bytes(b"m" * 6000)):
                runner.run()
            plan = json.loads((job / "edge_tts_smart_sync" / "smart_sync_plan_edge_tts.json").read_text(encoding="utf-8"))
            return render_targets, plan

    def test_short_pauses_trim_each_line_and_the_picture_follows(self) -> None:
        targets, plan = self._run(short_pauses=True)
        self.assertEqual(len(targets), 2)
        self.assertAlmostEqual(targets[0], 1.25, delta=0.02)
        self.assertAlmostEqual(targets[1], 2.88, delta=0.01)  # not cut below window / 1.25
        timing = plan["tts_timing_segments"]
        self.assertAlmostEqual(timing[1]["audio_start"], timing[0]["audio_end"], delta=0.001)
        self.assertEqual([round(x["audio_end"] - x["audio_start"], 3) for x in timing], [round(t, 3) for t in targets])

    def test_box_off_keeps_the_old_lengths(self) -> None:
        targets, _plan = self._run(short_pauses=False)
        self.assertEqual(targets, [2.0, 3.0])


if __name__ == "__main__":
    unittest.main()
