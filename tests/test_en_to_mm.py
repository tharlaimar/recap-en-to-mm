"""English → Myanmar narrator tool: only that direction is left, and the Smart Sync plan stays V2."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))

import core  # noqa: E402
import recap_planner as rp  # noqa: E402


def _unit(uid: int, start: float, end: float, text: str) -> dict:
    return {"id": uid, "speech_start": start, "speech_end": end, "source_text": text,
            "source_transcript_id": uid - 1, "timestamp": f"{rp._fmt_ts(start)} --> {rp._fmt_ts(end)}"}


class OnlyEnglishToMyanmarTests(unittest.TestCase):
    def test_no_other_language_mode_or_voice_engine_is_left(self):
        for name in ("recap_planner.py", "core.py", "controller.py", "planner_worker.py", "render_worker.py"):
            text = (APP / name).read_text(encoding="utf-8")
            for word in ("INDONESIAN", "HINDI", "THAI", "SPANISH", "Mandarin", "pyannote", "Pyannote",
                         "VoxCPM", "voxcpm", "Colab", "gradio", "mm_to_en", "include_dialogue"):
                self.assertNotIn(word, text, f"{word} in {name}")

    def test_translation_prompt_is_english_to_burmese(self):
        prompts = []

        def answer(_ai, prompt, **_kw):
            prompts.append(prompt)
            return {"items": [{"id": 1, "translation": "သူ အိမ်ပြန်လာခဲ့တယ်။"}]}

        with mock.patch.object(rp, "_gemini_json", answer):
            out = rp.translate_batch(None, story_context={}, previous_context=[], current=[_unit(1, 0.0, 2.0, "He came home.")],
                                     next_context=[], log=lambda _m: None)
        self.assertEqual(out[0]["translation"], "သူ အိမ်ပြန်လာခဲ့တယ်။")
        self.assertIn("BURMESE NARRATION TRANSLATOR", prompts[0])
        self.assertIn("Translate ONLY the CURRENT English items", prompts[0])

    def test_letters_of_another_script_are_asked_again(self):
        answers = [{"items": [{"id": 1, "translation": "მისခန်းထဲမှာ မီးခိုးတွေ"}]},
                   {"items": [{"id": 1, "translation": "ဧည့်ခန်းထဲမှာ မီးခိုးတွေ"}]}]
        prompts = []

        def answer(_ai, prompt, **_kw):
            prompts.append(prompt)
            return answers[len(prompts) - 1]

        with mock.patch.object(rp, "_gemini_json", answer):
            out = rp.translate_batch(None, story_context={}, previous_context=[],
                                     current=[_unit(1, 0.0, 2.0, "The living room was full of smoke.")],
                                     next_context=[], log=lambda _m: None)
        self.assertEqual(out[0]["translation"], "ဧည့်ခန်းထဲမှာ မီးခိုးတွေ")
        self.assertIn("letters of another script", prompts[1])
        self.assertEqual(rp._foreign_letters("ဧည့်ခန်း (living room)"), [])

    def test_plan_covers_the_whole_video_with_midpoint_boundaries(self):
        units = [dict(_unit(1, 1.0, 3.0, "One."), translation="တစ်။"),
                 dict(_unit(2, 5.0, 7.0, "Two."), translation="နှစ်။")]
        plan = rp.make_translation_plan(units, 10.0, source_title="t", transcript={}, story_context={})
        windows = [(s["video_start"], s["video_end"]) for s in plan["segments"]]
        self.assertEqual(windows, [(0.0, 4.0), (4.0, 10.0)])

    def test_smart_sync_caps_the_speed_then_trims(self):
        _vf, fast = core._sync_filter(10.0, 4.0, max_video_speed=1.25)
        self.assertAlmostEqual(fast["applied_factor"], 0.8)
        _vf, slow = core._sync_filter(2.0, 3.0, max_video_speed=1.25)
        self.assertAlmostEqual(slow["applied_factor"], 1.5)


if __name__ == "__main__":
    unittest.main()
