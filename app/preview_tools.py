from __future__ import annotations

import subprocess
from pathlib import Path


def probe_duration(ffprobe: str, source: str) -> float:
    # Preview follows the primary video stream, not container/audio-tail duration.
    p = subprocess.run(
        [ffprobe, '-v', 'error', '-select_streams', 'v:0', '-show_entries', 'stream=duration',
         '-of', 'default=noprint_wrappers=1:nokey=1', source],
        capture_output=True, text=True, encoding='utf-8', errors='replace'
    )
    if p.returncode == 0:
        for line in (p.stdout or '').strip().splitlines():
            try:
                d = float(line.strip())
                if d > 0.05:
                    return d
            except Exception:
                pass
    p2 = subprocess.run(
        [ffprobe, '-v', 'error', '-show_entries', 'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1', source],
        capture_output=True, text=True, encoding='utf-8', errors='replace'
    )
    if p2.returncode != 0:
        raise RuntimeError((p2.stderr or p.stderr)[-1200:] or 'ffprobe failed')
    return float(p2.stdout.strip())


def extract_preview_frame(
    ffmpeg: str,
    source: str,
    timestamp: float,
    zoom: float,
    mirror: bool,
    output: str,
    width: int = 720,
    height: int = 405,
    ratio: str = "16:9",
    fill: str = "blur",
    source_size: tuple[int, int] = (0, 0),
) -> str:
    """One frame drawn exactly like the render (same flip/zoom/ratio/fill filters), at preview size."""
    from core import _visual_filters, frame_layout

    layout = frame_layout(ratio, fill, source_size[0], source_size[1], canvas=(width, height))
    vf, _zoom = _visual_filters(mirror, zoom, layout)
    out = Path(output)
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        ffmpeg, '-y', '-hide_banner', '-loglevel', 'error',
        '-ss', f'{max(0.0, float(timestamp)):.3f}', '-i', source,
        '-frames:v', '1', '-vf', vf, str(out),
    ]
    p = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='replace')
    if p.returncode != 0 or not out.is_file():
        raise RuntimeError(p.stderr[-1600:] or 'preview extraction failed')
    return str(out)


def scale_logo_preview(ffmpeg: str, source: str, width_px: int, output: str) -> str:
    out = Path(output)
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        ffmpeg, '-y', '-hide_banner', '-loglevel', 'error', '-i', source,
        '-frames:v', '1', '-vf', f'scale={max(24, int(width_px))}:-1', str(out),
    ]
    p = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='replace')
    if p.returncode != 0 or not out.is_file():
        raise RuntimeError(p.stderr[-1600:] or 'logo preview scale failed')
    return str(out)
