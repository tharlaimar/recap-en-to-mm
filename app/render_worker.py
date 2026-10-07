from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from core import EdgeSmartSync

PREFIX = "@@VRS_EVENT@@"


def emit(kind: str, **data) -> None:
    payload = {"kind": kind, **data}
    print(PREFIX + json.dumps(payload, ensure_ascii=False), flush=True)


def main() -> int:
    if len(sys.argv) != 2:
        emit("error", message="render_worker requires one JSON config path")
        return 2
    cfg_path = Path(sys.argv[1]).resolve()
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    stop_file = Path(cfg["stop_file"]).resolve()

    def log(msg: str) -> None:
        emit("log", text=str(msg))

    def progress(current: int, total: int, label: str) -> None:
        emit("progress", current=int(current), total=int(total), label=str(label), stage="render")

    try:
        runner = EdgeSmartSync(
            job_dir=cfg["job_dir"],
            source_path=cfg["source_path"],
            plan_path=cfg["plan_path"],
            voice=cfg["voice"],
            edge_tts_rate=cfg.get("edge_tts_rate", "+0%"),
            edge_tts_pitch=cfg.get("edge_tts_pitch", "+0Hz"),
            ffmpeg_path=cfg.get("ffmpeg_path"),
            ffprobe_path=cfg.get("ffprobe_path"),
            max_video_speed=float(cfg.get("max_video_speed", 1.5)),
            max_video_slow=float(cfg.get("max_video_slow", 1.35)),
            mirror_video=bool(cfg.get("mirror_video", True)),
            zoom_factor=float(cfg.get("zoom_factor", 1.05)),
            smooth_freeze_fallback=bool(cfg.get("smooth_freeze_fallback", False)),
            max_freeze_hold=float(cfg.get("max_freeze_hold", 0.75)),
            render_final_video=bool(cfg.get("render_final_video", True)),
            output_ratio=str(cfg.get("output_ratio", "16:9")),
            frame_fill=str(cfg.get("frame_fill", "blur")),
            overlays=cfg.get("overlays") or {},
            log=log,
            progress=progress,
            should_stop=stop_file.exists,
        )
        runner.voice_volume_percent = int(cfg.get("voice_volume_percent", 100) or 100)
        runner.short_pauses = bool(cfg.get("short_pauses", False))
        result = runner.run()
        emit("result", result=result)
        return 0
    except InterruptedError as exc:
        emit("stopped", message=str(exc))
        return 10
    except Exception as exc:
        emit("log", text=traceback.format_exc())
        emit("error", message=str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
