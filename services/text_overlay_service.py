from __future__ import annotations

import base64
import html
import os
from pathlib import Path

from PIL import Image

try:
    from playwright.sync_api import sync_playwright
except Exception:
    sync_playwright = None

MY_DIGITS = str.maketrans("0123456789", "၀၁၂၃၄၅၆၇၈၉")


def mm_number(n: int) -> str:
    return str(n).translate(MY_DIGITS)


def find_mm_font() -> Path:
    """Find the exact Myanmar font file used by the browser renderer.

    User preference is honored first through RECAP_MM_FONT.  On
    Windows we then prefer the installed Burma-020 Thin file before other
    Myanmar-capable fallbacks.
    """
    candidates: list[Path] = []

    env = os.environ.get("RECAP_MM_FONT")
    if env:
        candidates.append(Path(env))

    local = os.environ.get("LOCALAPPDATA")
    user = os.environ.get("USERPROFILE")
    windir = os.environ.get("WINDIR", r"C:\Windows")

    user_font_dirs: list[Path] = []
    if local:
        user_font_dirs.append(Path(local) / "Microsoft/Windows/Fonts")
    if user:
        user_font_dirs.append(Path(user) / "AppData/Local/Microsoft/Windows/Fonts")

    exact_names = [
        "BURMA020-THIN.TTF",
        "Burma020-Thin.ttf",
        "Burma020-Light.ttf",
        "BURMA020-LIGHT.TTF",
        "Pyidaungsu.ttf",
        "NotoSansMyanmar-Regular.ttf",
        "mmrtext.ttf",
        "MyanmarText.ttf",
    ]

    for d in user_font_dirs:
        for name in exact_names:
            candidates.append(d / name)

    win_fonts = Path(windir) / "Fonts"
    for name in exact_names:
        candidates.append(win_fonts / name)

    candidates += [
        Path("/usr/share/fonts/truetype/noto/NotoSansMyanmar-Regular.ttf"),
        Path("/usr/share/fonts/truetype/noto/NotoSansMyanmar-Bold.ttf"),
        Path("/usr/share/fonts/truetype/padauk/Padauk-Regular.ttf"),
        Path("/usr/share/fonts/truetype/padauk/PadaukBook-Regular.ttf"),
    ]

    seen: set[str] = set()
    for p in candidates:
        key = str(p).casefold()
        if key in seen:
            continue
        seen.add(key)
        if p.is_file():
            return p.resolve()

    # Last Windows fallback: discover any Burma020 file regardless of suffix/case.
    for d in [*user_font_dirs, win_fonts]:
        if not d.is_dir():
            continue
        try:
            found = sorted(
                [p for p in d.iterdir() if p.is_file() and "burma020" in p.name.casefold().replace("-", "").replace("_", "")],
                key=lambda p: ("thin" not in p.name.casefold(), "light" not in p.name.casefold(), p.name.casefold()),
            )
            if found:
                return found[0].resolve()
        except OSError:
            pass

    raise FileNotFoundError(
        "No Myanmar font found. Set RECAP_MM_FONT to a Myanmar .TTF font file."
    )


def _browser_candidates(playwright):
    """Yield browser launch callables in production-friendly order.

    Recap uses Playwright Chromium. If Playwright's managed Chromium is not
    installed, try installed Chrome/Edge/Chromium so Episode Splitter does not
    require a second browser download.
    """
    yield ("playwright-chromium", lambda: playwright.chromium.launch(headless=True))
    yield ("google-chrome-channel", lambda: playwright.chromium.launch(channel="chrome", headless=True))
    yield ("msedge-channel", lambda: playwright.chromium.launch(channel="msedge", headless=True))

    paths = []
    pf = os.environ.get("PROGRAMFILES")
    pf86 = os.environ.get("PROGRAMFILES(X86)")
    local = os.environ.get("LOCALAPPDATA")
    if pf:
        paths += [
            Path(pf) / "Google/Chrome/Application/chrome.exe",
            Path(pf) / "Microsoft/Edge/Application/msedge.exe",
        ]
    if pf86:
        paths += [
            Path(pf86) / "Google/Chrome/Application/chrome.exe",
            Path(pf86) / "Microsoft/Edge/Application/msedge.exe",
        ]
    if local:
        paths += [
            Path(local) / "Google/Chrome/Application/chrome.exe",
            Path(local) / "Microsoft/Edge/Application/msedge.exe",
        ]
    paths += [Path("/usr/bin/chromium"), Path("/usr/bin/google-chrome")]

    seen = set()
    for exe in paths:
        key = str(exe).casefold()
        if key in seen or not exe.is_file():
            continue
        seen.add(key)
        yield (str(exe), lambda exe=exe: playwright.chromium.launch(executable_path=str(exe), headless=True))


def _launch_browser(playwright):
    errors = []
    for label, launcher in _browser_candidates(playwright):
        try:
            return launcher(), label
        except Exception as exc:
            errors.append(f"{label}: {type(exc).__name__}: {str(exc)[-180:]}")
    raise RuntimeError(
        "No Chromium/Chrome/Edge browser could be launched for Myanmar PNG rendering. "
        + " | ".join(errors[-4:])
    )


def _render_browser_text_png(
    *,
    text: str,
    out_path: Path,
    width: int,
    height: int,
    font_path: Path,
    font_size: int,
    min_font_size: int,
    max_lines: int,
    single_line: bool,
    bg_alpha: float,
    radius: int,
    padding_x: int,
    padding_y: int,
    stroke_px: float,
    font_weight: int = 400,
) -> Path:
    """Render Myanmar with the exact Recap-style Chromium shaping path.

    No PIL/FFmpeg text drawing is used. The whole Unicode string is handed to
    Chromium, which performs Myanmar OpenType shaping and visual line wrapping.
    """
    if sync_playwright is None:
        raise RuntimeError(
            "Playwright is required for Unicode-safe Myanmar rendering. "
            "Install with: python -m pip install playwright"
        )

    text = str(text or "").strip()
    if not text:
        raise ValueError("Cannot render empty text")

    font_bytes = font_path.read_bytes()
    font_b64 = base64.b64encode(font_bytes).decode("ascii")
    escaped = html.escape(text, quote=True)

    white_space = "nowrap" if single_line else "normal"
    overflow_wrap = "normal" if single_line else "break-word"
    max_lines = 1 if single_line else max(1, int(max_lines))

    markup = f"""<!doctype html>
<html><head><meta charset='utf-8'><style>
@font-face {{
  font-family:'EpisodeMM';
  src:url(data:font/ttf;base64,{font_b64}) format('truetype');
  font-style:normal;
  font-weight:100 900;
  font-display:block;
}}
html,body {{
  margin:0; padding:0; width:{int(width)}px; height:{int(height)}px;
  background:transparent!important; overflow:hidden;
}}
body {{ display:flex; align-items:center; justify-content:center; }}
#wrap {{
  box-sizing:border-box;
  width:calc(100% - 8px);
  height:calc(100% - 8px);
  padding:{int(padding_y)}px {int(padding_x)}px;
  border-radius:{int(radius)}px;
  background:rgba(0,0,0,{float(bg_alpha):.3f});
  display:flex; align-items:center; justify-content:center;
  overflow:hidden;
}}
#txt {{
  width:100%; text-align:center;
  font-family:'EpisodeMM','Myanmar Text','Pyidaungsu','Noto Sans Myanmar',sans-serif;
  font-size:{int(font_size)}px;
  line-height:1.30;
  font-weight:{int(font_weight)};
  color:#fff;
  white-space:{white_space};
  word-break:normal;
  overflow-wrap:{overflow_wrap};
  line-break:auto;
  -webkit-text-stroke:{float(stroke_px):.2f}px #000;
  paint-order:stroke fill;
  text-shadow:0 2px 0 rgba(0,0,0,.88);
}}
</style></head>
<body><div id='wrap'><div id='txt'>{escaped}</div></div>
<script>
(async function() {{
  await document.fonts.ready;
  const el=document.getElementById('txt');
  const wrap=document.getElementById('wrap');
  let size={int(font_size)};
  const minSize={int(min_font_size)};
  const maxLines={int(max_lines)};
  const oneLine={'true' if single_line else 'false'};
  function fits() {{
    if (oneLine) return el.scrollWidth <= wrap.clientWidth - 4 && el.scrollHeight <= wrap.clientHeight - 4;
    const cs=getComputedStyle(el);
    const lh=parseFloat(cs.lineHeight) || size*1.30;
    const lines=Math.max(1,Math.ceil(el.scrollHeight/lh));
    return el.scrollWidth <= wrap.clientWidth + 2 && el.scrollHeight <= wrap.clientHeight - 4 && lines <= maxLines;
  }}
  while(size>minSize && !fits()) {{ size-=1; el.style.fontSize=size+'px'; }}
  document.documentElement.dataset.renderReady='1';
}})();
</script></body></html>"""

    out_path.parent.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser, browser_label = _launch_browser(p)
        try:
            page = browser.new_page(viewport={"width": int(width), "height": int(height)}, device_scale_factor=1)
            page.set_content(markup, wait_until="load")
            page.wait_for_function("document.documentElement.dataset.renderReady === '1'", timeout=10_000)
            page.wait_for_timeout(80)
            page.screenshot(path=str(out_path), omit_background=True)
        finally:
            browser.close()

    if not out_path.is_file() or out_path.stat().st_size < 500:
        raise RuntimeError(f"Browser Myanmar PNG render failed: {out_path}")
    return out_path


def make_video_title_png(title: str, out_path: Path, cfg: dict) -> Path:
    font_path = find_mm_font()
    return _render_browser_text_png(
        text=title,
        out_path=out_path,
        width=int(cfg.get("panel_width", 980)),
        height=int(cfg.get("video_title_panel_height", 170)),
        font_path=font_path,
        font_size=int(cfg.get("font_size", 58)),
        min_font_size=int(cfg.get("min_title_font_size", 34)),
        max_lines=int(cfg.get("max_title_lines", 2)),
        single_line=False,
        bg_alpha=float(cfg.get("panel_bg_alpha", 0.57)),
        radius=int(cfg.get("panel_radius", 30)),
        padding_x=int(cfg.get("title_horizontal_padding", 44)),
        padding_y=int(cfg.get("title_vertical_padding", 18)),
        stroke_px=float(cfg.get("stroke_px", 1.6)),
        font_weight=int(cfg.get("font_weight", 400)),
    )


def make_video_part_png(part_index: int, part_count: int, out_path: Path, cfg: dict) -> Path:
    font_path = find_mm_font()
    text = f"အပိုင်း {mm_number(part_index)} / {mm_number(part_count)}"
    return _render_browser_text_png(
        text=text,
        out_path=out_path,
        width=int(cfg.get("panel_width", 980)),
        height=int(cfg.get("video_part_panel_height", 118)),
        font_path=font_path,
        font_size=int(cfg.get("part_font_size", 70)),
        min_font_size=int(cfg.get("min_part_font_size", 42)),
        max_lines=1,
        single_line=True,
        bg_alpha=float(cfg.get("panel_bg_alpha", 0.57)),
        radius=int(cfg.get("panel_radius", 28)),
        padding_x=int(cfg.get("part_horizontal_padding", 40)),
        padding_y=int(cfg.get("part_vertical_padding", 10)),
        stroke_px=float(cfg.get("stroke_px", 1.6)),
        font_weight=int(cfg.get("font_weight", 400)),
    )


def make_title_png(title: str, part_index: int, part_count: int, out_path: Path, cfg: dict) -> Path:
    """Backward-compatible combined helper. Text itself is still browser-rendered."""
    title_h = int(cfg.get("video_title_panel_height", 170))
    part_h = int(cfg.get("video_part_panel_height", 118))
    gap = int(cfg.get("video_header_gap", 8))
    w = int(cfg.get("panel_width", 980))

    title_tmp = out_path.with_name(out_path.stem + ".title.tmp.png")
    part_tmp = out_path.with_name(out_path.stem + ".part.tmp.png")
    make_video_title_png(title, title_tmp, cfg)
    make_video_part_png(part_index, part_count, part_tmp, cfg)

    with Image.open(title_tmp) as a, Image.open(part_tmp) as b:
        top = a.convert("RGBA")
        bottom = b.convert("RGBA")
        img = Image.new("RGBA", (w, title_h + gap + part_h), (0, 0, 0, 0))
        img.alpha_composite(top, (0, 0))
        img.alpha_composite(bottom, (0, title_h + gap))
        out_path.parent.mkdir(parents=True, exist_ok=True)
        img.save(out_path)

    title_tmp.unlink(missing_ok=True)
    part_tmp.unlink(missing_ok=True)
    return out_path


def make_part_only_png(part_index: int, part_count: int, out_path: Path, cfg: dict) -> Path:
    """Thumbnail part label, rendered with the same Chromium Myanmar shaping."""
    font_path = find_mm_font()
    text = f"အပိုင်း {mm_number(part_index)} / {mm_number(part_count)}"
    return _render_browser_text_png(
        text=text,
        out_path=out_path,
        width=int(cfg.get("thumbnail_part_panel_width", 650)),
        height=int(cfg.get("thumbnail_part_panel_height", 150)),
        font_path=font_path,
        font_size=int(cfg.get("thumbnail_part_font_size", 70)),
        min_font_size=int(cfg.get("min_part_font_size", 42)),
        max_lines=1,
        single_line=True,
        bg_alpha=float(cfg.get("thumbnail_panel_bg_alpha", 0.59)),
        radius=int(cfg.get("thumbnail_panel_radius", 32)),
        padding_x=28,
        padding_y=12,
        stroke_px=float(cfg.get("stroke_px", 1.6)),
        font_weight=int(cfg.get("font_weight", 400)),
    )
