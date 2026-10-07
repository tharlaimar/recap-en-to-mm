from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from recap_planner import RecapPlanBuilder

PREFIX = "@@VRS_EVENT@@"


def emit(kind: str, **data) -> None:
    payload = {"kind": kind, **data}
    print(PREFIX + json.dumps(payload, ensure_ascii=False), flush=True)


def main() -> int:
    if len(sys.argv) != 2:
        emit("error", message="planner_worker requires one JSON config path")
        return 2
    cfg_path = Path(sys.argv[1]).resolve()
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    stop_file = Path(cfg["stop_file"]).resolve()

    def log(msg: str) -> None:
        emit("log", text=str(msg))

    def progress(current: int, total: int, label: str) -> None:
        emit("progress", current=int(current), total=int(total), label=str(label), stage="planner")

    try:
        planner = RecapPlanBuilder(
            runtime_root=cfg["runtime_root"],
            gemini_env_path=cfg.get("gemini_env_path", ""),
            ffprobe_path=cfg["ffprobe_path"],
            log=log,
            progress=progress,
            should_stop=stop_file.exists,
            translation_batch_max_items=int(cfg.get("translation_batch_max_items", 40)),
            story_context_batch_max_items=int(cfg.get("story_context_batch_max_items", 80)),
            context_overlap=int(cfg.get("context_overlap", 6)),
            lookahead=int(cfg.get("lookahead", 6)),
            translation_audit=bool(cfg.get("translation_audit", True)),
            atomic_max_duration=float(cfg.get("atomic_max_duration", 5.6)),
            atomic_target_duration=float(cfg.get("atomic_target_duration", 4.2)),
            gemini_request_timeout_seconds=int(cfg.get("gemini_request_timeout_seconds", 120)),
            gemini_text_model=str(cfg.get("gemini_text_model", "gemini-3.5-flash")),
        )
        plan_path, plan = planner.build(
            source=Path(cfg["source_path"]),
            job_dir=Path(cfg["job_dir"]),
            source_title=cfg.get("source_title", ""),
            reuse=bool(cfg.get("reuse", True)),
        )
        emit("result", plan_path=str(plan_path), segments=len(plan.get("segments") or []))
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
