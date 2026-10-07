"""Output ratio (16:9 / 9:16 / 1:1 / Original) with Blur / Black / Crop fill, render and preview."""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))

import core  # noqa: E402
import preview_tools  # noqa: E402

FFMPEG, FFPROBE = shutil.which("ffmpeg"), shutil.which("ffprobe")


class LayoutTests(unittest.TestCase):
    def test_canvas_sizes(self):
        self.assertEqual(core.output_canvas("16:9", 1280, 720), (1920, 1080))
        self.assertEqual(core.output_canvas("9:16", 1280, 720), (1080, 1920))
        self.assertEqual(core.output_canvas("1:1", 1280, 720), (1080, 1080))
        self.assertEqual(core.output_canvas("Original", 1280, 720), (1280, 720))
        self.assertEqual(core.output_canvas("Original", 2160, 3840), (1080, 1920))  # long side capped at 1920
        self.assertEqual(core.output_canvas("Original", 0, 0), (1920, 1080))

    def test_16_9_crop_is_the_old_filter(self):
        vf, zoom = core._visual_filters(True, 1.05, None)
        self.assertEqual(vf, "hflip,scale=2016:1134:force_original_aspect_ratio=increase,crop=1920:1080,setsar=1,format=yuv420p")
        same, _ = core._visual_filters(True, 1.05, core.frame_layout("16:9", "blur", 1920, 1080))
        self.assertEqual(same, vf)  # a 16:9 source on a 16:9 frame: nothing to fill

    def test_9_16_blur_and_black_keep_the_whole_picture(self):
        blur, _ = core._visual_filters(False, 1.0, core.frame_layout("9:16", "blur", 1920, 1080))
        self.assertIn("split=2", blur)
        self.assertIn("boxblur", blur)
        self.assertIn("overlay=0:656", blur)  # 1080x608 picture centred on 1080x1920
        black, _ = core._visual_filters(False, 1.0, core.frame_layout("9:16", "black", 1920, 1080))
        self.assertIn("pad=1080:1920:0:656:color=black", black)
        crop, _ = core._visual_filters(False, 1.0, core.frame_layout("9:16", "crop", 1920, 1080))
        self.assertIn("crop=1080:1920", crop)


@unittest.skipUnless(FFMPEG and FFPROBE, "ffmpeg/ffprobe not on PATH")
class RenderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.dir = Path(cls.tmp.name)
        cls.src = cls.dir / "src.mp4"
        subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                        "testsrc=size=640x360:rate=30:duration=3", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                        str(cls.src)], check=True)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def _size_and_frames(self, path: Path) -> tuple[int, int, int]:
        out = subprocess.run([FFPROBE, "-v", "error", "-count_frames", "-select_streams", "v:0", "-show_entries",
                              "stream=width,height,nb_read_frames", "-of", "csv=p=0", str(path)],
                             capture_output=True, text=True, check=True).stdout.strip().split(",")
        return int(out[0]), int(out[1]), int(out[2])

    def test_parts_come_out_in_the_chosen_shape_and_frame_count(self):
        self.assertEqual(core.probe_video_size(FFPROBE, self.src), (640, 360))
        seg = {"video_start": 0.0, "video_end": 2.0}
        cases = [("9:16", "blur", (1080, 1920)), ("9:16", "black", (1080, 1920)), ("9:16", "crop", (1080, 1920)),
                 ("1:1", "blur", (1080, 1080)), ("Original", "blur", (640, 360)), ("16:9", "crop", (1920, 1080))]
        for ratio, fill, size in cases:
            out = self.dir / f"part_{ratio.replace(':', 'x')}_{fill}.mp4"
            layout = core.frame_layout(ratio, fill, 640, 360)
            core.render_video_part(FFMPEG, self.src, out, seg, 2.5, "libx264", mirror_video=True, zoom_factor=1.1,
                                   frame_count=75, layout=layout)
            self.assertEqual(self._size_and_frames(out), (*size, 75), (ratio, fill))

    def test_slow_motion_with_holds_also_uses_the_ratio(self):
        out = self.dir / "hybrid.mp4"
        core.render_video_part(FFMPEG, self.src, out, {"video_start": 0.0, "video_end": 1.0}, 3.0, "libx264",
                               smooth_freeze_fallback=True, max_video_slow=1.35, frame_count=90,
                               layout=core.frame_layout("9:16", "blur", 640, 360))
        self.assertEqual(self._size_and_frames(out), (1080, 1920, 90))

    def test_overlays_follow_the_frame_size(self):
        base = self.dir / "base_9x16.mp4"
        core.render_video_part(FFMPEG, self.src, base, {"video_start": 0.0, "video_end": 1.0}, 1.0, "libx264",
                               frame_count=30, layout=core.frame_layout("9:16", "black", 640, 360))
        logo = self.dir / "logo.png"
        subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                        "color=c=red:s=200x100", "-frames:v", "1", str(logo)], check=True)
        out = core.apply_production_overlays(
            FFMPEG, base, self.dir / "decorated.mp4",
            {"blur_boxes": [{"x": 0.9, "y": 0.95, "w": 0.3, "h": 0.2}], "logo_enabled": True, "logo_path": str(logo),
             "logo_position": [0.8, 0.05], "logo_width_fraction": 0.16},
            "libx264", self.dir, frame_size=(1080, 1920))
        self.assertEqual(self._size_and_frames(out)[:2], (1080, 1920))

    def test_preview_frame_matches_the_ratio(self):
        out = self.dir / "preview.png"
        preview_tools.extract_preview_frame(FFMPEG, str(self.src), 1.0, 1.05, True, str(out), 216, 384,
                                            ratio="9:16", fill="blur", source_size=(640, 360))
        w, h, _frames = self._size_and_frames(out)
        self.assertEqual((w, h), (216, 384))


if __name__ == "__main__":
    unittest.main()
