from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from core import normalize_segments
from preview_tools import probe_duration as preview_probe_duration, extract_preview_frame, scale_logo_preview

ROOT_DIR = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT_DIR / "config.json"
JOBS_DIR = ROOT_DIR / "jobs"
LOGS_DIR = ROOT_DIR / "logs"
JOBS_DIR.mkdir(parents=True, exist_ok=True)
LOGS_DIR.mkdir(parents=True, exist_ok=True)
EVENT_PREFIX = "@@VRS_EVENT@@"

BG = "#0d0f14"
PANEL = "#151922"
FIELD = "#0a0c10"
BORDER = "#303744"
FG = "#e7eaf0"
MUTED = "#9da7b6"
ACCENT = "#5b8cff"
ACCENT_HOVER = "#739dff"
DANGER = "#ff6b6b"
SUCCESS = "#55d187"
PREVIEW_W = 640
PREVIEW_H = 360

EDGE_VOICES = ("my-MM-ThihaNeural", "my-MM-NilarNeural")


def _tool_path(cfg: dict, name: str) -> str:
    """ffmpeg / ffprobe: config.json path, the tool's own ffmpeg folder, PATH, then C:\\ffmpeg\\bin."""
    for candidate in (str(cfg.get(f"{name}_path") or ""), str(ROOT_DIR / "ffmpeg" / f"{name}.exe"),
                      str(ROOT_DIR / "ffmpeg" / "bin" / f"{name}.exe"), shutil.which(name) or "",
                      rf"C:\ffmpeg\bin\{name}.exe"):
        if candidate and Path(candidate).is_file():
            return candidate
    return name


def load_config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_config(data: dict) -> None:
    CONFIG_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _parse_edge_rate_percent(value: object) -> int:
    text = str(value or "+0%").strip()
    match = re.fullmatch(r"([+-]?\d{1,3})%", text)
    percent = int(match.group(1)) if match else 0
    return max(-50, min(percent, 100))


def _format_edge_rate(percent: object) -> str:
    value = max(-50, min(int(round(float(percent))), 100))
    return f"{value:+d}%"


def _safe_name(text: str) -> str:
    text = re.sub(r"[^\w\-]+", "_", text, flags=re.UNICODE).strip("_")
    return text[:80] or "recap_test"


def _job_dir_for(source: Path) -> Path:
    raw = str(source.resolve()).encode("utf-8")
    sig = hashlib.sha1(raw).hexdigest()[:8]
    return JOBS_DIR / f"{_safe_name(source.stem)}_{sig}"


BATCH_VIDEO_EXTS = {".mp4", ".mkv", ".mov", ".webm", ".avi"}


def _natural_key(path: Path) -> list:
    return [int(part) if part.isdigit() else part.casefold() for part in re.split(r"(\d+)", path.name)]


def _list_batch_videos(folder: Path) -> list[Path]:
    """Videos directly inside folder (not subfolders), in natural filename order."""
    return sorted(
        (p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in BATCH_VIDEO_EXTS),
        key=_natural_key,
    )


def _job_final_output(job_dir: Path) -> Path | None:
    final = job_dir / "edge_tts_smart_sync" / "final_edge_tts_smart_sync.mp4"
    return final if final.is_file() and final.stat().st_size > 10000 else None


def _native_crash_message(code: int) -> str:
    unsigned = code & 0xFFFFFFFF
    if code == -1073740791 or unsigned == 0xC0000409:
        return (
            "Child process native crash (0xC0000409).\n\n"
            "ဒီ native crash ထပ်ပေါ်ရင် jobs/<job>/run.log ကိုစစ်ပါ။"
        )
    return f"Child process closed with exit code {code}. run.log ကိုစစ်ပါ။"


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Recap English → Myanmar — Narrator · Edge TTS · Smart Sync")
        self.geometry("1580x900")
        self.minsize(1100, 660)
        try:
            self.state("zoomed")  # start maximised so all three panes get real space
        except tk.TclError:
            pass
        self.configure(bg=BG)
        self.cfg = load_config()
        self.msgq: queue.Queue[tuple[str, object]] = queue.Queue()
        self.stop_event = threading.Event()
        self.worker: threading.Thread | None = None
        self.current_proc: subprocess.Popen | None = None
        self.current_job: Path | None = None
        self.run_log_path: Path | None = None
        self.proc_lock = threading.Lock()

        self.gemini_env = tk.StringVar(value=self.cfg.get("gemini_env_path") or str(ROOT_DIR / ".env"))
        self.source = tk.StringVar(value="")
        self.source_title = tk.StringVar(value="")
        saved_voice = str(self.cfg.get("edge_tts_voice_mm") or self.cfg.get("edge_tts_voice") or EDGE_VOICES[0])
        self.edge_voice = tk.StringVar(value=saved_voice if saved_voice in EDGE_VOICES else EDGE_VOICES[0])
        self.edge_rate_percent = tk.DoubleVar(value=float(_parse_edge_rate_percent(self.cfg.get("edge_tts_rate", "+0%"))))
        self.max_speed = tk.StringVar(value=str(self.cfg.get("max_video_speed", 1.25)))
        self.max_slow = tk.StringVar(value=str(self.cfg.get("max_video_slow", 1.35)))
        self.mirror_video = tk.BooleanVar(value=bool(self.cfg.get("mirror_video", True)))
        self.voice_volume = tk.IntVar(value=int(self.cfg.get("voice_volume_percent", 100) or 100))
        self.short_pauses = tk.BooleanVar(value=bool(self.cfg.get("short_pauses", True)))
        self.zoom_factor = tk.DoubleVar(value=float(self.cfg.get("zoom_factor", 1.05)))
        self.smooth_freeze = tk.BooleanVar(value=bool(self.cfg.get("smooth_freeze_fallback", False)))
        self.max_freeze_hold = tk.StringVar(value=str(self.cfg.get("max_freeze_hold", 0.75)))
        self.render_final = tk.BooleanVar(value=bool(self.cfg.get("render_final_video", True)))
        self.reuse_plan = tk.BooleanVar(value=bool(self.cfg.get("reuse_transcript_and_plan", self.cfg.get("reuse_whisper_and_plan", True))))
        self.status = tk.StringVar(value="READY")
        self.stage = tk.StringVar(value="Idle")
        self.job_display = tk.StringVar(value="Job output: not selected")
        # Folder batch: when non-empty, START processes these files one by one.
        self.batch_files: list[Path] = []
        self.batch_folder: Path | None = None

        # Minimal production editor state. V0.3 sync/mirror/freeze behavior remains hidden/fixed.
        self.title_text = tk.StringVar(value="")
        self.title_size = tk.IntVar(value=int(self.cfg.get("title_font_size", 54)))
        self.logo_path = tk.StringVar(value=str(self.cfg.get("logo_path", "")))
        self.logo_enabled = tk.BooleanVar(value=bool(self.cfg.get("logo_enabled", False)))
        self.logo_width_frac = tk.DoubleVar(value=float(self.cfg.get("logo_width_fraction", 0.16)))
        self.logo_opacity = tk.DoubleVar(value=float(self.cfg.get("logo_opacity", 0.65)))
        self.extra_text = tk.StringVar(value="")
        self.extra_text_size = tk.IntVar(value=int(self.cfg.get("extra_text_font_size", 42)))
        self.extra_text_opacity = tk.DoubleVar(value=float(self.cfg.get("extra_text_opacity", 0.65)))
        self.preview_time = tk.DoubleVar(value=float(self.cfg.get("preview_seek_seconds", 1.0)))
        self.preview_duration = 0.0
        self.blur_boxes: list[dict[str, float]] = []
        self.selected_blur: int | None = None
        self.title_pos = list(self.cfg.get("title_position", [0.50, 0.12]))
        self.logo_pos = list(self.cfg.get("logo_position", [0.80, 0.06]))
        self.extra_text_pos = list(self.cfg.get("extra_text_position", [0.50, 0.84]))
        self._preview_photo = None
        self._logo_photo = None
        self._draw_blur_mode = False
        self._draw_start: tuple[float, float] | None = None
        self._temp_rect = None
        self._drag_kind: str | None = None
        self._drag_index: int | None = None
        self._drag_start: tuple[float, float] | None = None
        self._drag_original = None

        self._apply_dark_theme()
        self._build()
        self.after(150, self._pump)

    def _apply_dark_theme(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(".", background=BG, foreground=FG, font=("Segoe UI", 10))
        style.configure("TFrame", background=BG)
        style.configure("Panel.TFrame", background=PANEL)
        style.configure("TLabel", background=BG, foreground=FG)
        style.configure("Muted.TLabel", background=BG, foreground=MUTED)
        style.configure("Status.TLabel", background=BG, foreground=SUCCESS, font=("Segoe UI Semibold", 10))
        style.configure("TLabelframe", background=BG, foreground=FG, bordercolor=BORDER, relief="solid")
        style.configure("TLabelframe.Label", background=BG, foreground=FG, font=("Segoe UI Semibold", 10))
        style.configure("TEntry", fieldbackground=FIELD, foreground=FG, insertcolor=FG, bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER)
        style.map("TEntry", fieldbackground=[("disabled", PANEL)], foreground=[("disabled", MUTED)])
        style.configure("TCombobox", fieldbackground=FIELD, foreground=FG, background=PANEL, arrowcolor=FG, bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER)
        style.map("TCombobox", fieldbackground=[("readonly", FIELD)], foreground=[("readonly", FG)], selectbackground=[("readonly", FIELD)], selectforeground=[("readonly", FG)])
        style.configure("TButton", background="#252b36", foreground=FG, bordercolor=BORDER, padding=(10, 6))
        style.map("TButton", background=[("active", "#333b49"), ("disabled", PANEL)], foreground=[("disabled", "#657080")])
        style.configure("Compact.TButton", background="#252b36", foreground=FG, bordercolor=BORDER, padding=(6, 2))
        style.map("Compact.TButton", background=[("active", "#333b49"), ("disabled", PANEL)], foreground=[("disabled", "#657080")])
        style.configure("Accent.TButton", background=ACCENT, foreground="#ffffff", bordercolor=ACCENT, padding=(12, 7), font=("Segoe UI Semibold", 10))
        style.map("Accent.TButton", background=[("active", ACCENT_HOVER), ("disabled", "#33445f")], foreground=[("disabled", "#8892a0")])
        style.configure("Danger.TButton", background="#4a252a", foreground="#ffd9dc", bordercolor="#6a3038", padding=(10, 6))
        style.map("Danger.TButton", background=[("active", "#653039")])
        style.configure("TCheckbutton", background=BG, foreground=FG)
        style.map("TCheckbutton", background=[("active", BG)], foreground=[("active", FG)])
        style.configure("Horizontal.TProgressbar", troughcolor=FIELD, background=ACCENT, bordercolor=FIELD, lightcolor=ACCENT, darkcolor=ACCENT)
        self.option_add("*TCombobox*Listbox.background", FIELD)
        self.option_add("*TCombobox*Listbox.foreground", FG)
        self.option_add("*TCombobox*Listbox.selectBackground", ACCENT)
        self.option_add("*TCombobox*Listbox.selectForeground", "#ffffff")

    def _build(self) -> None:
        # Layout V33: one-line header, compact source bar, then three resizable panes
        # (Settings+Logs | Preview | Tools). The preview follows its pane size and always
        # shows the whole 16:9 frame; the long settings list scrolls inside its own pane.
        outer = ttk.Frame(self, padding=(12, 8, 12, 8))
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(2, weight=1)

        header = tk.Frame(outer, bg=BG)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        tk.Label(header, text="Recap Production", bg=BG, fg=FG, font=("Segoe UI Semibold", 15)).pack(side="left")
        tk.Label(header, text="English → Myanmar  |  Narrator only  |  Whisper + Gemini  |  Edge TTS  |  Smart Sync",
                 bg=BG, fg=MUTED, font=("Segoe UI", 9)).pack(side="left", padx=(14, 0), pady=(5, 0))

        source_bar = ttk.Frame(outer)
        source_bar.grid(row=1, column=0, sticky="ew", pady=(0, 6))
        source_bar.columnconfigure(1, weight=3)
        source_bar.columnconfigure(4, weight=1)
        ttk.Label(source_bar, text="Source Video").grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.source_entry = ttk.Entry(source_bar, textvariable=self.source)
        self.source_entry.grid(row=0, column=1, sticky="ew")
        self.source_entry.bind("<KeyRelease>", lambda _event: self._clear_batch())
        source_buttons = ttk.Frame(source_bar)
        source_buttons.grid(row=0, column=2, padx=(6, 18), sticky="w")
        ttk.Button(source_buttons, text="Browse", style="Compact.TButton", command=self._browse_source).pack(side="left")
        ttk.Button(source_buttons, text="Folder", style="Compact.TButton", command=self._browse_folder).pack(side="left", padx=(6, 0))
        ttk.Label(source_bar, text="Gemini .env").grid(row=0, column=3, sticky="w", padx=(0, 8))
        ttk.Entry(source_bar, textvariable=self.gemini_env).grid(row=0, column=4, sticky="ew")
        ttk.Button(source_bar, text="Browse", style="Compact.TButton", command=self._browse_env).grid(row=0, column=5, padx=(6, 0))

        main = ttk.PanedWindow(outer, orient="horizontal")
        main.grid(row=2, column=0, sticky="nsew")

        # ---------------- left: scrollable settings (top) + Run Logs (bottom)
        left = ttk.PanedWindow(main, orient="vertical")
        settings_host = ttk.LabelFrame(left, text="Translation / Voice Settings", padding=3)
        settings_host.rowconfigure(0, weight=1)
        settings_host.columnconfigure(0, weight=1)
        settings_canvas = tk.Canvas(settings_host, bg=BG, highlightthickness=0, height=340)
        settings_canvas.grid(row=0, column=0, sticky="nsew")
        settings_scroll = ttk.Scrollbar(settings_host, orient="vertical", command=settings_canvas.yview)
        settings_scroll.grid(row=0, column=1, sticky="ns")
        settings_canvas.configure(yscrollcommand=settings_scroll.set)
        source_box = ttk.Frame(settings_canvas, padding=(6, 4, 8, 4))
        settings_window = settings_canvas.create_window((0, 0), window=source_box, anchor="nw")
        source_box.bind("<Configure>", lambda _e: settings_canvas.configure(scrollregion=settings_canvas.bbox("all")))
        settings_canvas.bind("<Configure>", lambda e: settings_canvas.itemconfigure(settings_window, width=e.width))
        self._bind_wheel(settings_canvas, settings_canvas)
        source_box.columnconfigure(1, weight=1)
        hint_wrap = 200

        def hint(row: int, text: str) -> ttk.Label:
            label = ttk.Label(source_box, text=text, style="Muted.TLabel", wraplength=hint_wrap, justify="left")
            label.grid(row=row, column=2, sticky="w", padx=(8, 0))
            return label

        ttk.Label(source_box, text="Direction").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=3)
        ttk.Label(source_box, text="English → Myanmar  ·  Narrator only  ·  Edge TTS").grid(
            row=1, column=1, columnspan=2, sticky="w", pady=3)

        self.edge_voice_label = ttk.Label(source_box, text="Edge TTS Voice")
        self.edge_voice_label.grid(row=11, column=0, sticky="w", padx=(0, 8), pady=3)
        self.edge_voice_combo = ttk.Combobox(source_box, textvariable=self.edge_voice, state="readonly", values=EDGE_VOICES)
        self.edge_voice_combo.grid(row=11, column=1, sticky="ew", pady=3)
        self.edge_voice_hint = hint(11, "Local Edge TTS")

        self.edge_speed_label = ttk.Label(source_box, text="Edge TTS Speed")
        self.edge_speed_label.grid(row=12, column=0, sticky="w", padx=(0, 8), pady=3)
        self.edge_speed_frame = ttk.Frame(source_box)
        self.edge_speed_frame.grid(row=12, column=1, sticky="ew", pady=3)
        self.edge_speed_frame.columnconfigure(0, weight=1)
        self.edge_speed_slider = ttk.Scale(
            self.edge_speed_frame, from_=-50, to=100, orient="horizontal",
            variable=self.edge_rate_percent, command=self._edge_rate_changed,
        )
        self.edge_speed_slider.grid(row=0, column=0, sticky="ew")
        self.edge_speed_value = ttk.Label(self.edge_speed_frame, text=_format_edge_rate(self.edge_rate_percent.get()), width=7)
        self.edge_speed_value.grid(row=0, column=1, padx=(8, 0))
        self.edge_speed_hint = hint(12, "-50% slower · 0% normal · +100% faster")

        self._bind_wheel_recursive(source_box, settings_canvas)
        self._settings_canvas = settings_canvas

        log_panel = ttk.LabelFrame(left, text="Run Logs", padding=5)
        log_panel.rowconfigure(0, weight=1)
        log_panel.columnconfigure(0, weight=1)
        self.log_notebook = ttk.Notebook(log_panel)
        self.log_notebook.grid(row=0, column=0, sticky="nsew")
        self.logbox = self._create_log_tab("Main Log")
        left.add(settings_host, weight=3)
        left.add(log_panel, weight=2)

        # ---------------- center: preview that fills its pane
        preview_box = ttk.LabelFrame(main, text="Preview — drag Title / Logo, draw Blur box", padding=7)
        preview_box.columnconfigure(0, weight=1)
        preview_box.rowconfigure(0, weight=1)
        self.preview_canvas = tk.Canvas(preview_box, width=PREVIEW_W, height=PREVIEW_H, bg="#05070a", highlightthickness=1, highlightbackground=BORDER, cursor="crosshair")
        self.preview_canvas.grid(row=0, column=0, sticky="nsew")
        self.preview_canvas.bind("<ButtonPress-1>", self._preview_press)
        self.preview_canvas.bind("<B1-Motion>", self._preview_motion)
        self.preview_canvas.bind("<ButtonRelease-1>", self._preview_release)
        self.preview_canvas.bind("<Configure>", self._preview_resized)
        self._preview_resize_job = None
        self._preview_canvas_size = (PREVIEW_W, PREVIEW_H)

        seek_row = ttk.Frame(preview_box)
        seek_row.grid(row=1, column=0, sticky="ew", pady=(7, 0))
        seek_row.columnconfigure(1, weight=1)
        ttk.Button(seek_row, text="◀ 5s", width=5, style="Compact.TButton", command=lambda: self._seek_preview(-5)).grid(row=0, column=0, padx=(0, 6))
        self.preview_slider = ttk.Scale(seek_row, from_=0.0, to=1.0, orient="horizontal", variable=self.preview_time,
                                        command=lambda _v: self._update_preview_time_label())
        self.preview_slider.grid(row=0, column=1, sticky="ew")
        self.preview_slider.bind("<ButtonRelease-1>", lambda _e: self._refresh_preview())
        ttk.Button(seek_row, text="5s ▶", width=5, style="Compact.TButton", command=lambda: self._seek_preview(5)).grid(row=0, column=2, padx=(6, 8))
        self.preview_time_label = ttk.Label(seek_row, text="00:00 / 00:00", style="Muted.TLabel", width=15)
        self.preview_time_label.grid(row=0, column=3)
        ttk.Button(seek_row, text="Refresh Frame", command=self._refresh_preview).grid(row=0, column=4, padx=(6, 0))

        # ---------------- right: scrollable production tools
        tools_host = ttk.LabelFrame(main, text="Production Tools", padding=3)
        tools_host.rowconfigure(0, weight=1)
        tools_host.columnconfigure(0, weight=1)
        tools_canvas = tk.Canvas(tools_host, bg=BG, highlightthickness=0, width=320)
        tools_canvas.grid(row=0, column=0, sticky="nsew")
        tools_scroll = ttk.Scrollbar(tools_host, orient="vertical", command=tools_canvas.yview)
        tools_scroll.grid(row=0, column=1, sticky="ns")
        tools_canvas.configure(yscrollcommand=tools_scroll.set)
        tools = ttk.Frame(tools_canvas, padding=(8, 6, 10, 6))
        tools_window = tools_canvas.create_window((0, 0), window=tools, anchor="nw")
        tools.bind("<Configure>", lambda _e: tools_canvas.configure(scrollregion=tools_canvas.bbox("all")))
        tools_canvas.bind("<Configure>", lambda e: tools_canvas.itemconfigure(tools_window, width=e.width))
        self._bind_wheel(tools_canvas, tools_canvas)
        tools.columnconfigure(0, weight=1)

        def section(row: int, text: str) -> None:
            if row:
                ttk.Separator(tools).grid(row=row - 1, column=0, sticky="ew", pady=(2, 10))
            ttk.Label(tools, text=text, font=("Segoe UI Semibold", 10)).grid(row=row, column=0, sticky="w")

        def slider_row(row: int, label: str, from_: float, to: float, variable, on_change, value_label_attr: str,
                       text: str, pady=(3, 10)) -> ttk.Scale:
            frame = ttk.Frame(tools)
            frame.grid(row=row, column=0, sticky="ew", pady=pady)
            frame.columnconfigure(1, weight=1)
            ttk.Label(frame, text=label, width=9).grid(row=0, column=0, sticky="w")
            scale = ttk.Scale(frame, from_=from_, to=to, orient="horizontal", variable=variable, command=on_change)
            scale.grid(row=0, column=1, sticky="ew")
            value = ttk.Label(frame, text=text, width=6, anchor="e")
            value.grid(row=0, column=2, padx=(8, 0))
            setattr(self, value_label_attr, value)
            return scale

        section(0, "Video")
        self.zoom_slider = slider_row(1, "Zoom", 1.00, 1.25, self.zoom_factor, self._zoom_changed,
                                      "zoom_value_label", f"{float(self.zoom_factor.get()):.2f}x")
        self.zoom_slider.bind("<ButtonRelease-1>", lambda _e: self._refresh_preview())
        ttk.Checkbutton(self.zoom_slider.master, text="Flip (mirror left ↔ right)", variable=self.mirror_video,
                        command=self._mirror_changed).grid(row=1, column=0, columnspan=3, sticky="w", pady=(6, 0))
        self._voice_volume_scale_var = tk.DoubleVar(value=float(self.voice_volume.get()))
        volume_frame = ttk.Frame(self.zoom_slider.master)
        volume_frame.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(8, 0))
        volume_frame.columnconfigure(1, weight=1)
        ttk.Label(volume_frame, text="Voice vol", width=9).grid(row=0, column=0, sticky="w")
        ttk.Scale(volume_frame, from_=50, to=300, orient="horizontal", variable=self._voice_volume_scale_var,
                  command=self._voice_volume_changed).grid(row=0, column=1, sticky="ew")
        self.voice_volume_label = ttk.Label(volume_frame, text="", width=13, anchor="e")
        self.voice_volume_label.grid(row=0, column=2, padx=(8, 0))
        self._voice_volume_changed(self.voice_volume.get())
        ttk.Checkbutton(self.zoom_slider.master, text="Short pauses (~0.25 s between lines)",
                        variable=self.short_pauses).grid(row=3, column=0, columnspan=3, sticky="w", pady=(6, 0))

        section(3, "Blur Watermark Area")
        blur_row = ttk.Frame(tools)
        blur_row.grid(row=4, column=0, sticky="ew", pady=(5, 10))
        ttk.Button(blur_row, text="+ Draw Blur Box", style="Compact.TButton", command=self._begin_blur_box).pack(side="left")
        ttk.Button(blur_row, text="Delete Selected", style="Compact.TButton", command=self._delete_selected_blur).pack(side="left", padx=6)
        self.blur_count_label = ttk.Label(blur_row, text="0 box", style="Muted.TLabel")
        self.blur_count_label.pack(side="left", padx=(4, 0))

        section(6, "Title")
        self.title_entry = ttk.Entry(tools, textvariable=self.title_text, font=("Myanmar Text", 10))
        self.title_entry.grid(row=7, column=0, sticky="ew", pady=(5, 4))
        self.title_entry.bind("<KeyRelease>", lambda _e: self._redraw_editor_items())
        self._title_size_scale_var = tk.DoubleVar(value=float(self.title_size.get()))
        title_scale = slider_row(8, "Size", 24, 96, self._title_size_scale_var,
                                 lambda v: self._int_slider_changed(v, self.title_size, "title_size_label"),
                                 "title_size_label", str(int(self.title_size.get())), pady=(0, 2))
        title_scale.bind("<ButtonRelease-1>", lambda _e: self._redraw_editor_items())
        ttk.Label(tools, text="Drag the title on the preview to move it", style="Muted.TLabel").grid(row=9, column=0, sticky="w", pady=(0, 10))

        ttk.Separator(tools).grid(row=10, column=0, sticky="ew", pady=(2, 10))
        logo_header = ttk.Frame(tools)
        logo_header.grid(row=11, column=0, sticky="ew")
        logo_header.columnconfigure(0, weight=1)
        ttk.Label(logo_header, text="Logo", font=("Segoe UI Semibold", 10)).grid(row=0, column=0, sticky="w")
        ttk.Checkbutton(logo_header, text="Enable Logo", variable=self.logo_enabled, command=self._logo_enabled_changed).grid(row=0, column=1, sticky="e")
        logo_row = ttk.Frame(tools)
        logo_row.grid(row=12, column=0, sticky="ew", pady=(5, 4))
        logo_row.columnconfigure(0, weight=1)
        ttk.Entry(logo_row, textvariable=self.logo_path).grid(row=0, column=0, sticky="ew")
        ttk.Button(logo_row, text="Browse", width=7, style="Compact.TButton", command=self._browse_logo).grid(row=0, column=1, padx=(6, 0))
        ttk.Button(logo_row, text="Remove", width=7, style="Compact.TButton", command=self._remove_logo).grid(row=0, column=2, padx=(6, 0))
        self.logo_size_slider = slider_row(13, "Size", 0.06, 0.35, self.logo_width_frac, lambda _v: self._redraw_editor_items(),
                                           "logo_size_label", f"{self.logo_width_frac.get()*100:.0f}%", pady=(2, 2))
        self.logo_size_slider.bind("<ButtonRelease-1>", lambda _e: self._redraw_editor_items())
        self.logo_opacity_slider = slider_row(14, "Opacity", 0.10, 1.00, self.logo_opacity, lambda _v: self._logo_opacity_changed(),
                                              "logo_opacity_label", f"{self.logo_opacity.get()*100:.0f}%", pady=(2, 10))
        self.logo_opacity_slider.bind("<ButtonRelease-1>", lambda _e: self._redraw_editor_items())

        section(16, "Extra Text Box")
        self.extra_text_entry = ttk.Entry(tools, textvariable=self.extra_text, font=("Myanmar Text", 10))
        self.extra_text_entry.grid(row=17, column=0, sticky="ew", pady=(5, 4))
        self.extra_text_entry.bind("<KeyRelease>", lambda _e: self._redraw_editor_items())
        self._extra_size_scale_var = tk.DoubleVar(value=float(self.extra_text_size.get()))
        extra_scale = slider_row(18, "Size", 18, 84, self._extra_size_scale_var,
                                 lambda v: self._int_slider_changed(v, self.extra_text_size, "extra_size_label"),
                                 "extra_size_label", str(int(self.extra_text_size.get())), pady=(0, 2))
        extra_scale.bind("<ButtonRelease-1>", lambda _e: self._redraw_editor_items())
        self.extra_opacity_slider = slider_row(19, "Opacity", 0.10, 1.00, self.extra_text_opacity, lambda _v: self._extra_opacity_changed(),
                                               "extra_opacity_label", f"{self.extra_text_opacity.get()*100:.0f}%", pady=(2, 2))
        self.extra_opacity_slider.bind("<ButtonRelease-1>", lambda _e: self._redraw_editor_items())
        ttk.Label(tools, text="Drag the text on the preview to move it", style="Muted.TLabel").grid(row=20, column=0, sticky="w", pady=(0, 10))
        self._bind_wheel_recursive(tools, tools_canvas)
        # Job resume / config loads set the IntVars directly; keep the sliders in step.
        self.title_size.trace_add("write", lambda *_a: self._sync_int_slider(self.title_size, self._title_size_scale_var, "title_size_label"))
        self.extra_text_size.trace_add("write", lambda *_a: self._sync_int_slider(self.extra_text_size, self._extra_size_scale_var, "extra_size_label"))

        main.add(left, weight=3)
        main.add(preview_box, weight=6)
        main.add(tools_host, weight=2)
        self._main_pane, self._left_pane = main, left
        self.after(250, self._apply_pane_layout)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        controls = ttk.Frame(outer)
        controls.grid(row=3, column=0, sticky="ew", pady=(6, 0))
        controls.columnconfigure(0, weight=1)
        btns = ttk.Frame(controls)
        btns.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        self.start_btn = ttk.Button(btns, text="START / RESUME", style="Accent.TButton", command=self._start)
        self.start_btn.pack(side="left")
        self.stop_btn = ttk.Button(btns, text="STOP SAFELY", style="Danger.TButton", command=self._stop, state="disabled")
        self.stop_btn.pack(side="left", padx=8)
        ttk.Button(btns, text="Open Job Folder", command=self._open_output).pack(side="left")
        ttk.Label(btns, textvariable=self.job_display, style="Muted.TLabel").pack(side="left", padx=(12, 0))
        ttk.Label(btns, textvariable=self.stage, style="Muted.TLabel").pack(side="right", padx=(8, 0))
        ttk.Label(btns, textvariable=self.status, style="Status.TLabel").pack(side="right")

        self.progress = ttk.Progressbar(controls, mode="determinate", maximum=100)
        self.progress.grid(row=1, column=0, sticky="ew")

    # ---------------------------------------------------------------- layout helpers
    UI_LAYOUT_FILE = LOGS_DIR / "ui_layout.json"

    @staticmethod
    def _bind_wheel(widget: tk.Misc, canvas: tk.Canvas) -> None:
        widget.bind("<MouseWheel>", lambda e: canvas.yview_scroll(int(-e.delta / 120), "units"), add="+")

    def _bind_wheel_recursive(self, widget: tk.Misc, canvas: tk.Canvas) -> None:
        # Sliders and combo/spin boxes keep their own wheel behaviour.
        if not isinstance(widget, (ttk.Scale, ttk.Combobox, ttk.Spinbox)):
            self._bind_wheel(widget, canvas)
        for child in widget.winfo_children():
            self._bind_wheel_recursive(child, canvas)

    def _apply_pane_layout(self) -> None:
        """Default sashes (settings pane ~38%, tools ~19%); a layout dragged last time wins."""
        try:
            self.update_idletasks()
            width = self._main_pane.winfo_width()
            height = self._left_pane.winfo_height()
            if width < 400 or height < 200:
                self.after(250, self._apply_pane_layout)
                return
            try:
                saved = json.loads(self.UI_LAYOUT_FILE.read_text(encoding="utf-8"))
            except Exception:
                saved = {}
            left_w = int(saved.get("left_fraction", 0.38) * width)
            tools_w = int(saved.get("tools_fraction", 0.19) * width)
            left_w = max(520, min(left_w, width - tools_w - 420))
            self._main_pane.sashpos(0, left_w)
            self._main_pane.sashpos(1, max(left_w + 420, width - tools_w))
            setup_h = int(saved.get("setup_fraction", 0) * height) or int(height * 0.62)
            self._left_pane.sashpos(0, max(160, min(setup_h, height - 120)))
        except Exception as exc:
            self._log(f"⚠️ Layout restore skipped: {exc}")

    def _save_pane_layout(self) -> None:
        try:
            width = max(1, self._main_pane.winfo_width())
            height = max(1, self._left_pane.winfo_height())
            data = {
                "left_fraction": round(self._main_pane.sashpos(0) / width, 4),
                "tools_fraction": round((width - self._main_pane.sashpos(1)) / width, 4),
                "setup_fraction": round(self._left_pane.sashpos(0) / height, 4),
            }
            self.UI_LAYOUT_FILE.parent.mkdir(parents=True, exist_ok=True)
            self.UI_LAYOUT_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception:
            pass

    def _on_close(self) -> None:
        self._save_pane_layout()
        self.destroy()

    def _voice_volume_changed(self, value) -> None:
        import math
        percent = int(round(float(value) / 5.0) * 5)  # 5% steps
        self.voice_volume.set(percent)
        self.voice_volume_label.configure(text=f"{percent}% ({20 * math.log10(percent / 100.0):+.1f}dB)")

    def _mirror_changed(self) -> None:
        # Persist right away so the choice survives a restart even before START.
        self.cfg["mirror_video"] = bool(self.mirror_video.get())
        save_config(self.cfg)
        self._log(f"🔁 Flip / mirror {'ON' if self.mirror_video.get() else 'OFF'} — video parts re-render on next run; TTS is reused.")
        self._refresh_preview()

    def _sync_int_slider(self, source: tk.IntVar, scale_var: tk.DoubleVar, label_attr: str) -> None:
        try:
            number = int(source.get())
        except (tk.TclError, ValueError):
            return
        scale_var.set(float(number))
        getattr(self, label_attr).configure(text=str(number))

    def _int_slider_changed(self, value: str, target: tk.IntVar, label_attr: str) -> None:
        number = int(round(float(value)))
        target.set(number)
        getattr(self, label_attr).configure(text=str(number))
        self._redraw_editor_items()

    def _preview_resized(self, event) -> None:
        size = (max(160, int(event.width) - 2), max(90, int(event.height) - 2))
        if size == self._preview_canvas_size:
            return
        self._preview_canvas_size = size
        self._redraw_editor_items()
        # Re-extract the frame only after the user stops resizing.
        if self._preview_resize_job is not None:
            self.after_cancel(self._preview_resize_job)
        self._preview_resize_job = self.after(300, self._refresh_preview)

    def _seek_preview(self, seconds: float) -> None:
        upper = float(self.preview_duration or 0.0)
        value = float(self.preview_time.get()) + seconds
        self.preview_time.set(max(0.0, min(value, max(0.0, upper - 0.05)) if upper else max(0.0, value)))
        self._update_preview_time_label()
        self._refresh_preview()

    def _update_preview_time_label(self) -> None:
        def fmt(sec: float) -> str:
            sec = max(0, int(sec))
            h, rem = divmod(sec, 3600)
            m, s = divmod(rem, 60)
            return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"
        if hasattr(self, "preview_time_label"):
            self.preview_time_label.configure(text=f"{fmt(self.preview_time.get())} / {fmt(self.preview_duration or 0)}")

    def _edge_rate_changed(self, value: str) -> None:
        try:
            self.edge_speed_value.configure(text=_format_edge_rate(value))
        except Exception:
            pass

    def _create_log_tab(self, title: str) -> tk.Text:
        frame = ttk.Frame(self.log_notebook)
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        box = tk.Text(
            frame, wrap="word", width=36, height=12, bg="#080a0e", fg="#d9dee8",
            insertbackground=FG, selectbackground="#274d8b", selectforeground="#ffffff",
            relief="flat", borderwidth=0, padx=8, pady=8, font=("Consolas", 9),
        )
        box.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(frame, command=box.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        box.configure(yscrollcommand=scroll.set)
        self.log_notebook.add(frame, text=title)
        return box

    def _zoom_changed(self, value: str) -> None:
        try:
            self.zoom_value_label.configure(text=f"{float(value):.2f}x")
        except Exception:
            pass

    def _logo_opacity_changed(self) -> None:
        try:
            self.logo_opacity_label.configure(text=f"{float(self.logo_opacity.get())*100:.0f}%")
        except Exception:
            pass

    def _extra_opacity_changed(self) -> None:
        try:
            self.extra_opacity_label.configure(text=f"{float(self.extra_text_opacity.get())*100:.0f}%")
        except Exception:
            pass

    def _persist_logo_choice(self) -> None:
        """Persist logo enable/path immediately so a removed logo cannot return on restart."""
        try:
            self.cfg["logo_enabled"] = bool(self.logo_enabled.get())
            self.cfg["logo_path"] = self.logo_path.get().strip() if self.logo_enabled.get() else ""
            save_config(self.cfg)
        except Exception as exc:
            self._log(f"⚠️ Could not save logo setting: {exc}")

    def _logo_enabled_changed(self) -> None:
        # OFF means no logo is passed to the renderer even when a legacy path still exists.
        self._persist_logo_choice()
        self._redraw_editor_items()

    def _remove_logo(self) -> None:
        # Full removal: clear both the toggle and saved path so app/job resume cannot revive it.
        self.logo_enabled.set(False)
        self.logo_path.set("")
        self._logo_photo = None
        self._persist_logo_choice()
        self._redraw_editor_items()
        self.status.set("LOGO REMOVED")
        self._log("🧹 Logo removed and saved setting cleared.")

    def _browse_logo(self) -> None:
        p = filedialog.askopenfilename(title="Select logo", filetypes=[("Image", "*.png *.webp *.jpg *.jpeg"), ("All", "*.*")])
        if p:
            self.logo_path.set(p)
            self.logo_enabled.set(True)
            self._persist_logo_choice()
            self._redraw_editor_items()

    def _begin_blur_box(self) -> None:
        self._draw_blur_mode = True
        self.selected_blur = None
        self.preview_canvas.configure(cursor="crosshair")
        self.status.set("DRAW BLUR BOX")

    def _delete_selected_blur(self) -> None:
        if self.selected_blur is None:
            return
        if 0 <= self.selected_blur < len(self.blur_boxes):
            self.blur_boxes.pop(self.selected_blur)
        self.selected_blur = None
        self._redraw_editor_items()

    def _preview_bounds(self) -> tuple[int, int, int, int]:
        """Largest 16:9 frame that fits the canvas: (x offset, y offset, width, height).

        Overlay positions are stored as 0..1 fractions of the output frame, so the preview
        can be any size without moving where Title/Logo/Blur land in the render.
        """
        cw, ch = getattr(self, "_preview_canvas_size", (PREVIEW_W, PREVIEW_H))
        scale = min(cw / 16.0, ch / 9.0)
        width, height = max(16, int(16 * scale)), max(9, int(9 * scale))
        return (cw - width) // 2, (ch - height) // 2, width, height

    def _preview_xy(self, event) -> tuple[float, float]:
        ox, oy, width, height = self._preview_bounds()
        return (max(float(ox), min(float(event.x), ox + width)),
                max(float(oy), min(float(event.y), oy + height)))

    def _refresh_preview(self) -> None:
        src = self.source.get().strip()
        if not src or not Path(src).is_file():
            return
        try:
            ffmpeg = _tool_path(self.cfg, "ffmpeg")
            ox, oy, width, height = self._preview_bounds()
            out = LOGS_DIR / "preview_frame.png"
            extract_preview_frame(
                ffmpeg, src, float(self.preview_time.get()), float(self.zoom_factor.get()),
                bool(self.mirror_video.get()), str(out), width, height,
            )
            self._preview_photo = tk.PhotoImage(file=str(out))
            self.preview_canvas.delete("frame")
            self.preview_canvas.create_image(ox, oy, image=self._preview_photo, anchor="nw", tags=("frame",))
            self.preview_canvas.tag_lower("frame")
            self._redraw_editor_items()
            self._update_preview_time_label()
        except Exception as exc:
            self._log(f"⚠️ Preview frame error: {exc}")

    def _redraw_editor_items(self) -> None:
        if not hasattr(self, "preview_canvas"):
            return
        c = self.preview_canvas
        c.delete("editor")
        ox, oy, pw, ph = self._preview_bounds()
        self.blur_count_label.configure(text=f"{len(self.blur_boxes)} box" + ("es" if len(self.blur_boxes) != 1 else ""))
        for i, b in enumerate(self.blur_boxes):
            x1, y1 = ox + b["x"] * pw, oy + b["y"] * ph
            x2, y2 = ox + (b["x"] + b["w"]) * pw, oy + (b["y"] + b["h"]) * ph
            color = "#ffd166" if i == self.selected_blur else "#48d7ff"
            c.create_rectangle(x1, y1, x2, y2, outline=color, width=2, dash=(6, 4), tags=("editor", "blur", f"blur_{i}"))
            c.create_text(x1 + 5, y1 + 5, text="BLUR", fill=color, anchor="nw", font=("Segoe UI Semibold", 9), tags=("editor", f"blur_{i}"))
            if i == self.selected_blur:
                c.create_rectangle(x2-5, y2-5, x2+5, y2+5, fill=color, outline="#111111", tags=("editor", f"blur_handle_{i}"))

        txt = self.title_text.get().strip()
        if txt:
            px = ox + float(self.title_pos[0]) * pw
            py = oy + float(self.title_pos[1]) * ph
            fs = max(10, int(self.title_size.get() * pw / 1920))
            c.create_text(px+2, py+2, text=txt, fill="#000000", anchor="center", font=("Myanmar Text", fs, "bold"), width=int(pw*0.82), tags=("editor", "title"))
            c.create_text(px, py, text=txt, fill="#ffffff", anchor="center", font=("Myanmar Text", fs, "bold"), width=int(pw*0.82), tags=("editor", "title"))

        extra_txt = self.extra_text.get().strip()
        if extra_txt:
            ex = ox + float(self.extra_text_pos[0]) * pw
            ey = oy + float(self.extra_text_pos[1]) * ph
            efs = max(9, int(self.extra_text_size.get() * pw / 1920))
            opacity = max(0.10, min(1.00, float(self.extra_text_opacity.get())))
            shade = max(140, min(255, int(255 * opacity)))
            fill = f"#{shade:02x}{shade:02x}{shade:02x}"
            c.create_text(ex+2, ey+2, text=extra_txt, fill="#101010", anchor="center", font=("Myanmar Text", efs, "bold"), width=int(pw*0.78), tags=("editor", "extra_text"))
            c.create_text(ex, ey, text=extra_txt, fill=fill, anchor="center", font=("Myanmar Text", efs, "bold"), width=int(pw*0.78), tags=("editor", "extra_text"))

        logo = self.logo_path.get().strip() if self.logo_enabled.get() else ""
        if logo and Path(logo).is_file():
            try:
                ffmpeg = _tool_path(self.cfg, "ffmpeg")
                lw = max(36, int(pw * float(self.logo_width_frac.get())))
                tmp = LOGS_DIR / "preview_logo.png"
                scale_logo_preview(ffmpeg, logo, lw, str(tmp))
                self._logo_photo = tk.PhotoImage(file=str(tmp))
                self.logo_size_label.configure(text=f"{float(self.logo_width_frac.get())*100:.0f}%")
                x, y = ox + float(self.logo_pos[0])*pw, oy + float(self.logo_pos[1])*ph
                c.create_image(x, y, image=self._logo_photo, anchor="nw", tags=("editor", "logo"))
            except Exception as exc:
                self._log(f"⚠️ Logo preview error: {exc}")

    def _preview_press(self, event) -> None:
        x, y = self._preview_xy(event)
        if self._draw_blur_mode:
            self._draw_start = (x, y)
            self._temp_rect = self.preview_canvas.create_rectangle(x, y, x, y, outline="#48d7ff", width=2, dash=(6,4), tags=("editor", "temp_blur"))
            return
        items = self.preview_canvas.find_overlapping(x-3, y-3, x+3, y+3)
        tags = []
        for item in reversed(items):
            t = self.preview_canvas.gettags(item)
            if t:
                tags.append(t)
        self._drag_kind = None
        self._drag_index = None
        self._drag_start = (x, y)
        for t in tags:
            handle = next((z for z in t if z.startswith("blur_handle_")), None)
            blur = next((z for z in t if z.startswith("blur_") and not z.startswith("blur_handle_")), None)
            if handle:
                i = int(handle.rsplit("_",1)[1]); self.selected_blur=i; self._drag_kind="blur_resize"; self._drag_index=i; self._drag_original=dict(self.blur_boxes[i]); break
            if "title" in t:
                self._drag_kind="title"; break
            if "extra_text" in t:
                self._drag_kind="extra_text"; break
            if "logo" in t:
                self._drag_kind="logo"; self._drag_original=list(self.logo_pos); break
            if blur:
                i=int(blur.rsplit("_",1)[1]); self.selected_blur=i; self._drag_kind="blur_move"; self._drag_index=i; self._drag_original=dict(self.blur_boxes[i]); break
        self._redraw_editor_items()

    def _preview_motion(self, event) -> None:
        x, y = self._preview_xy(event)
        if self._draw_blur_mode and self._draw_start and self._temp_rect:
            self.preview_canvas.coords(self._temp_rect, self._draw_start[0], self._draw_start[1], x, y)
            return
        if not self._drag_kind or not self._drag_start:
            return
        ox, oy, pw, ph = self._preview_bounds()
        dx=(x-self._drag_start[0])/pw; dy=(y-self._drag_start[1])/ph
        if self._drag_kind == "title":
            self.title_pos=[(x-ox)/pw, (y-oy)/ph]
        elif self._drag_kind == "extra_text":
            self.extra_text_pos=[(x-ox)/pw, (y-oy)/ph]
        elif self._drag_kind == "logo":
            lx,ly=self._drag_original
            self.logo_pos=[max(0.0,min(0.95,lx+dx)), max(0.0,min(0.95,ly+dy))]
        elif self._drag_index is not None:
            i=self._drag_index; old=self._drag_original; b=dict(old)
            if self._drag_kind == "blur_move":
                b["x"]=max(0.0,min(1.0-old["w"],old["x"]+dx)); b["y"]=max(0.0,min(1.0-old["h"],old["y"]+dy))
            else:
                b["w"]=max(0.015,min(1.0-old["x"],old["w"]+dx)); b["h"]=max(0.015,min(1.0-old["y"],old["h"]+dy))
            self.blur_boxes[i]=b
        self._redraw_editor_items()

    def _preview_release(self, event) -> None:
        x, y = self._preview_xy(event)
        if self._draw_blur_mode and self._draw_start:
            ox, oy, pw, ph = self._preview_bounds()
            x1,y1=self._draw_start; x2,y2=x,y
            if abs(x2-x1)>=12 and abs(y2-y1)>=12:
                xa,xb=sorted((x1,x2)); ya,yb=sorted((y1,y2))
                self.blur_boxes.append({"x":(xa-ox)/pw,"y":(ya-oy)/ph,"w":(xb-xa)/pw,"h":(yb-ya)/ph})
                self.selected_blur=len(self.blur_boxes)-1
            self._draw_blur_mode=False; self._draw_start=None; self._temp_rect=None
            self.preview_canvas.configure(cursor="arrow")
            self.status.set("READY")
            self._redraw_editor_items()
            return
        self._drag_kind=None; self._drag_index=None; self._drag_start=None; self._drag_original=None
        self._redraw_editor_items()

    def _append_log_file(self, text: str) -> None:
        paths = [LOGS_DIR / "latest.log"]
        if self.run_log_path:
            paths.append(self.run_log_path)
        stamp = datetime.now().strftime("%H:%M:%S")
        for path in paths:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="utf-8") as f:
                    f.write(f"[{stamp}] {text}\n")
            except Exception:
                pass

    def _log(self, text: str) -> None:
        self.msgq.put(("log", str(text)))

    def _set_progress(self, current: int, total: int, label: str) -> None:
        self.msgq.put(("progress", (current, total, label)))

    def _pump(self) -> None:
        try:
            while True:
                kind, payload = self.msgq.get_nowait()
                if kind == "log":
                    text = str(payload)
                    self.logbox.insert("end", text + "\n")
                    self.logbox.see("end")
                    self._append_log_file(text)
                elif kind == "progress":
                    current, total, label = payload
                    pct = 0 if not total else current * 100.0 / total
                    self.progress["value"] = max(0, min(100, pct))
                    self.status.set(str(label))
                elif kind == "stage":
                    self.stage.set(str(payload))
                elif kind == "job":
                    self.job_display.set(str(payload))
                elif kind == "done":
                    self._running(False)
                    self.progress["value"] = 100
                    self.status.set("DONE")
                    self.stage.set("Completed")
                    messagebox.showinfo("DONE", str(payload))
                elif kind == "error":
                    self._running(False)
                    self.status.set("ERROR")
                    self.stage.set("Stopped")
                    messagebox.showerror("ERROR", str(payload))
        except queue.Empty:
            pass
        self.after(150, self._pump)

    def _browse_env(self) -> None:
        current = self.gemini_env.get().strip()
        initial = str(Path(current).parent) if current else str(ROOT_DIR.parent)
        p = filedialog.askopenfilename(title="Select .env containing GEMINI_API_KEY", initialdir=initial, filetypes=[("ENV", ".env"), ("All", "*.*")])
        if p:
            self.gemini_env.set(p)

    def _clear_batch(self) -> None:
        if self.batch_files:
            self.batch_files = []
            self.batch_folder = None
            self.start_btn.configure(text="START / RESUME")

    def _browse_folder(self) -> None:
        folder = filedialog.askdirectory(title="Select folder of source videos")
        if not folder:
            return
        files = _list_batch_videos(Path(folder))
        if not files:
            messagebox.showinfo("Folder Batch", "ဒီ folder ထဲမှာ video ဖိုင် (mp4/mkv/mov/webm/avi) မတွေ့ပါ။")
            return
        # Load the first video so preview / editor overlays / plan state work as usual.
        self._load_source(str(files[0]))
        self.batch_folder = Path(folder)
        self.batch_files = files
        self.start_btn.configure(text=f"START BATCH ({len(files)})")
        self.job_display.set(f"Batch: {len(files)} video(s) · {Path(folder).name}")
        self._log(f"📂 Folder batch: {len(files)} video(s) in {folder}")
        for i, f in enumerate(files, 1):
            done = " (already done — will skip)" if _job_final_output(_job_dir_for(f)) else ""
            self._log(f"   {i}. {f.name}{done}")
        self._log("ℹ️ Title / Logo / Blur settings on screen are applied to every video in the batch.")

    def _browse_source(self) -> None:
        p = filedialog.askopenfilename(title="Select source video", filetypes=[("Video", "*.mp4 *.mkv *.mov *.webm *.avi"), ("All", "*.*")])
        if p:
            self._clear_batch()
            self._load_source(p)

    def _load_source(self, p: str) -> None:
        if p:
            self.source.set(p)
            self.source_title.set(Path(p).stem)
            self.blur_boxes = []
            self.selected_blur = None
            job = _job_dir_for(Path(p))
            # Resume editor layout too, not only transcript/TTS checkpoints.
            editor_file = job / "editor_overlays.json"
            if editor_file.is_file():
                try:
                    ed = json.loads(editor_file.read_text(encoding="utf-8"))
                    self.blur_boxes = [dict(x) for x in (ed.get("blur_boxes") or []) if isinstance(x, dict)]
                    self.title_text.set(str(ed.get("title_text") or ""))
                    self.title_pos = list(ed.get("title_position") or self.title_pos)
                    self.title_size.set(int(ed.get("title_font_size", self.title_size.get())))
                    # V19: legacy editor files had no logo_enabled flag. Do not let those
                    # cached paths silently resurrect a logo the user has disabled globally.
                    job_logo_enabled = bool(ed.get("logo_enabled", self.cfg.get("logo_enabled", False)))
                    self.logo_enabled.set(job_logo_enabled)
                    if job_logo_enabled and str(ed.get("logo_path") or "").strip():
                        self.logo_path.set(str(ed.get("logo_path")))
                    elif not job_logo_enabled:
                        self.logo_path.set("")
                    self.logo_pos = list(ed.get("logo_position") or self.logo_pos)
                    self.logo_width_frac.set(float(ed.get("logo_width_fraction", self.logo_width_frac.get())))
                    self.logo_opacity.set(float(ed.get("logo_opacity", self.logo_opacity.get())))
                    self.extra_text.set(str(ed.get("extra_text") or ""))
                    self.extra_text_pos = list(ed.get("extra_text_position") or self.extra_text_pos)
                    self.extra_text_size.set(int(ed.get("extra_text_font_size", self.extra_text_size.get())))
                    self.extra_text_opacity.set(float(ed.get("extra_text_opacity", self.extra_text_opacity.get())))
                except Exception as exc:
                    self._log(f"⚠️ Editor layout resume error: {exc}")
            self.job_display.set(f"Job: {job.name}")
            try:
                ffprobe = _tool_path(self.cfg, "ffprobe")
                self.preview_duration = preview_probe_duration(ffprobe, p)
                self.preview_slider.configure(to=max(0.1, self.preview_duration))
                t = min(max(0.0, float(self.cfg.get("preview_seek_seconds", 1.0))), max(0.0, self.preview_duration-0.05))
                self.preview_time.set(t)
            except Exception as exc:
                self.preview_duration = 0.0
                self._log(f"⚠️ Preview duration error: {exc}")
            self._refresh_preview()

    def _save_cfg(self, settings: dict) -> None:
        self.cfg["gemini_env_path"] = settings["gemini_env_path"]
        self.cfg["reuse_transcript_and_plan"] = settings["reuse_transcript_and_plan"]
        self.cfg["edge_tts_voice_mm"] = settings["edge_tts_voice"]
        self.cfg["edge_tts_rate"] = settings["edge_tts_rate"]
        # Engine settings stay fixed/hidden; only user-facing production values persist.
        self.cfg["zoom_factor"] = settings["zoom_factor"]
        self.cfg["mirror_video"] = bool(settings["mirror_video"])
        self.cfg["voice_volume_percent"] = int(settings.get("voice_volume_percent", 100))
        self.cfg["short_pauses"] = bool(settings.get("short_pauses", True))
        self.cfg["logo_enabled"] = bool(settings["overlays"].get("logo_enabled", False))
        self.cfg["logo_path"] = settings["overlays"].get("logo_path", "") if self.cfg["logo_enabled"] else ""
        self.cfg["logo_width_fraction"] = settings["overlays"].get("logo_width_fraction", 0.16)
        self.cfg["logo_opacity"] = settings["overlays"].get("logo_opacity", 0.65)
        self.cfg["logo_position"] = settings["overlays"].get("logo_position", [0.80, 0.06])
        self.cfg["title_position"] = settings["overlays"].get("title_position", [0.50, 0.12])
        self.cfg["title_font_size"] = settings["overlays"].get("title_font_size", 54)
        self.cfg["extra_text_position"] = settings["overlays"].get("extra_text_position", [0.50, 0.84])
        self.cfg["extra_text_font_size"] = settings["overlays"].get("extra_text_font_size", 42)
        self.cfg["extra_text_opacity"] = settings["overlays"].get("extra_text_opacity", 0.65)
        self.cfg["preview_seek_seconds"] = float(self.preview_time.get())
        save_config(self.cfg)

    def _capture_settings(self) -> dict:
        source = Path(self.source.get().strip()).resolve()
        env_text = str(self.gemini_env.get().strip() or self.cfg.get("gemini_env_path") or "")
        gemini_env = Path(env_text).resolve() if env_text else None
        voice = self.edge_voice.get().strip()
        if not source.is_file():
            raise ValueError("Source video ကိုရွေးပါ။")
        if gemini_env is not None and not gemini_env.is_file():
            raise ValueError("Gemini .env path မမှန်ပါ။ Gemini .env မှာ Browse နှိပ်ပြီး GEMINI_API_KEY ပါတဲ့ .env ဖိုင်ကိုရွေးပါ။")
        if voice not in EDGE_VOICES:
            raise ValueError("Edge TTS Voice ကိုရွေးပါ။")
        for tool in ("ffmpeg", "ffprobe"):
            if not Path(_tool_path(self.cfg, tool)).is_file():  # otherwise the job dies with "[WinError 2]"
                raise ValueError(
                    f"{tool}.exe မတွေ့ပါ — FFmpeg မသွင်းရသေးပါ။\n\n"
                    "INSTALL_REQUIREMENTS.bat ကို ပြန် run ပါ။ FFmpeg ကို ဒီ tool ရဲ့ ffmpeg folder ထဲ အလိုလို ထည့်ပေးပါမယ်။\n"
                    "(သို့) https://www.gyan.dev/ffmpeg/builds/ ကနေ download လုပ်ပြီး ffmpeg.exe နဲ့ ffprobe.exe ကို "
                    "RUN.bat ဘေးက ffmpeg folder ထဲ ထည့်ပါ။"
                )
        runtime_root = ROOT_DIR / "recap_runtime"
        for rel in ("services/whisper_service.py", "services/ai_service.py"):
            if not (runtime_root / rel).is_file():
                raise ValueError(f"Bundled Recap runtime file missing: {rel}")
        zoom = float(self.zoom_factor.get())
        if not (1.0 <= zoom <= 1.25):
            raise ValueError("Zoom must be between 1.00 and 1.25")
        logo_enabled = bool(self.logo_enabled.get())
        logo_path = self.logo_path.get().strip() if logo_enabled else ""
        overlays = {
            "blur_boxes": [dict(x) for x in self.blur_boxes],
            "blur_strength": int(self.cfg.get("overlay_blur_strength", 18)),
            "title_text": self.title_text.get().strip(),
            "title_position": [float(self.title_pos[0]), float(self.title_pos[1])],
            "title_font_size": int(self.title_size.get()),
            "title_font_path": str(self.cfg.get("title_font_path", r"C:\Windows\Fonts\mmrtext.ttf")),
            "logo_enabled": logo_enabled,
            "logo_path": logo_path,
            "logo_position": [float(self.logo_pos[0]), float(self.logo_pos[1])],
            "logo_width_fraction": float(self.logo_width_frac.get()),
            "logo_opacity": float(self.logo_opacity.get()),
            "extra_text": self.extra_text.get().strip(),
            "extra_text_position": [float(self.extra_text_pos[0]), float(self.extra_text_pos[1])],
            "extra_text_font_size": int(self.extra_text_size.get()),
            "extra_text_opacity": float(self.extra_text_opacity.get()),
        }
        if overlays["logo_path"] and not Path(overlays["logo_path"]).is_file():
            raise ValueError("Logo image path မမှန်ပါ။")
        return {
            "source": source,
            "source_title": self.source_title.get().strip() or source.stem,
            "edge_tts_voice": voice,
            "edge_tts_rate": _format_edge_rate(self.edge_rate_percent.get()),
            "edge_tts_pitch": str(self.cfg.get("edge_tts_pitch", "+0Hz")),
            "gemini_env_path": str(gemini_env) if gemini_env is not None else "",
            "voice": voice,
            # V0.3 behavior, hidden from UI.
            "max_video_speed": float(self.cfg.get("max_video_speed", 1.25)),
            "max_video_slow": float(self.cfg.get("max_video_slow", 1.35)),
            "smooth_freeze_fallback": bool(self.cfg.get("smooth_freeze_fallback", False)),
            "max_freeze_hold": float(self.cfg.get("max_freeze_hold", 0.75)),
            "mirror_video": bool(self.mirror_video.get()),
            "voice_volume_percent": int(self.voice_volume.get()),
            "short_pauses": bool(self.short_pauses.get()),
            "zoom_factor": zoom,
            "render_final_video": True,
            "reuse_transcript_and_plan": bool(self.cfg.get("reuse_transcript_and_plan", self.cfg.get("reuse_whisper_and_plan", True))),
            "overlays": overlays,
        }

    def _write_worker_cfg(self, path: Path, data: dict) -> None:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def _run_child(self, script_name: str, cfg_path: Path, stage_name: str) -> dict:
        self.msgq.put(("stage", stage_name))
        self._log(f"\n{'=' * 64}\n{stage_name}\n{'=' * 64}")
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        cmd = [sys.executable, "-u", str(ROOT_DIR / "app" / script_name), str(cfg_path)]
        self._log("▶ " + " ".join(f'"{x}"' if " " in x else x for x in cmd))
        proc = subprocess.Popen(
            cmd,
            cwd=str(ROOT_DIR),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=env,
        )
        with self.proc_lock:
            self.current_proc = proc
        result: dict = {}
        reported_error = ""
        stopped_message = ""
        try:
            assert proc.stdout is not None
            for raw in proc.stdout:
                line = raw.rstrip("\r\n")
                if not line:
                    continue
                if line.startswith(EVENT_PREFIX):
                    try:
                        event = json.loads(line[len(EVENT_PREFIX):])
                    except Exception:
                        self._log(line)
                        continue
                    kind = event.get("kind")
                    if kind == "log":
                        self._log(str(event.get("text", "")))
                    elif kind == "progress":
                        self._set_progress(int(event.get("current", 0)), int(event.get("total", 0)), str(event.get("label", stage_name)))
                    elif kind == "result":
                        result = event
                    elif kind == "error":
                        reported_error = str(event.get("message", ""))
                    elif kind == "stopped":
                        stopped_message = str(event.get("message", "Stopped"))
                    continue
                self._log(line)
            code = proc.wait()
        finally:
            with self.proc_lock:
                if self.current_proc is proc:
                    self.current_proc = None

        if code == 10 or stopped_message:
            raise InterruptedError(stopped_message or "Stopped safely")
        if code != 0:
            if reported_error:
                raise RuntimeError(reported_error + f"\n\nChild exit code: {code}\nLog: {self.run_log_path}")
            raise RuntimeError(_native_crash_message(code) + f"\n\nLog: {self.run_log_path}")
        return result

    def _start(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        try:
            settings = self._capture_settings()
            self._save_cfg(settings)
        except Exception as exc:
            messagebox.showerror("Setup Error", str(exc))
            return

        batch = [Path(p) for p in self.batch_files]
        sources: list[Path] = batch or [settings["source"]]

        self.stop_event.clear()
        self._running(True)
        self.logbox.delete("1.0", "end")
        self.progress["value"] = 0
        self.status.set("STARTING")
        self.stage.set("Preparing")
        self._log("📦 English → Myanmar · Narrator only · Edge TTS · V2 Smart Sync")
        self._log(f"📁 App root: {ROOT_DIR}")
        if batch:
            self._log(f"📂 Folder batch: {len(batch)} video(s) — one at a time")

        # Capture every Tk variable on the GUI thread; worker thread uses plain data only.
        ffmpeg = _tool_path(self.cfg, "ffmpeg")
        ffprobe = _tool_path(self.cfg, "ffprobe")
        cfg_values = {
            "translation_batch_max_items": int(self.cfg.get("translation_batch_max_items", 40)),
            "story_context_batch_max_items": int(self.cfg.get("story_context_batch_max_items", 80)),
            "context_overlap": int(self.cfg.get("context_overlap", 6)),
            "lookahead": int(self.cfg.get("lookahead", 6)),
            "translation_audit": bool(self.cfg.get("translation_audit", True)),
            "atomic_max_duration": float(self.cfg.get("atomic_max_duration", 5.6)),
            "atomic_target_duration": float(self.cfg.get("atomic_target_duration", 4.2)),
            "gemini_request_timeout_seconds": int(self.cfg.get("gemini_request_timeout_seconds", 120)),
            "gemini_text_model": str(self.cfg.get("gemini_text_model", "gemini-3.5-flash")),
        }

        def worker() -> None:
            if not batch:
                try:
                    final, job_dir = self._run_one_job(settings, sources[0], ffmpeg, ffprobe, cfg_values)
                    self.msgq.put(("done", f"ပြီးပါပြီ။\n\nOutput:\n{final}\n\nJob folder:\n{job_dir}"))
                except InterruptedError as exc:
                    self._log(str(exc))
                    self.msgq.put(("error", str(exc)))
                except Exception as exc:
                    self._log(traceback.format_exc())
                    self.msgq.put(("error", str(exc)))
                return

            ok: list[str] = []
            skipped: list[str] = []
            failed: list[str] = []
            stopped = False
            for index, source in enumerate(sources, 1):
                if self.stop_event.is_set():
                    stopped = True
                    break
                label = f"[BATCH {index}/{len(sources)}] {source.name}"
                self.msgq.put(("job", f"Batch {index}/{len(sources)} · {source.name}"))
                if not source.is_file():
                    failed.append(f"{source.name} · file missing")
                    self._log(f"❌ {label} — file missing, skipped")
                    continue
                existing = _job_final_output(_job_dir_for(source))
                if existing is not None:
                    skipped.append(source.name)
                    self._log(f"⏭️ {label} — already done ({existing.name}); job folder ဖျက်ရင် ပြန်လုပ်ပါမယ်")
                    continue
                self._log(f"\n{'#' * 64}\n▶ {label}\n{'#' * 64}")
                one = dict(settings)
                one["source"] = source
                one["source_title"] = source.stem
                try:
                    final, _job_dir = self._run_one_job(one, source, ffmpeg, ffprobe, cfg_values, batch_mode=True)
                    ok.append(source.name)
                    self._log(f"✅ {label} COMPLETE → {final}")
                except InterruptedError as exc:
                    self._log(str(exc))
                    stopped = True
                    failed.append(f"{source.name} · stopped")
                    break
                except Exception as exc:
                    self._log(traceback.format_exc())
                    first_line = (str(exc).splitlines() or [type(exc).__name__])[0]
                    failed.append(f"{source.name} · {first_line[:160]}")
                    self._log(f"❌ {label} FAILED — continuing with next video")

            lines = [
                f"Folder batch {'STOPPED' if stopped else 'DONE'}",
                f"OK={len(ok)} · SKIPPED={len(skipped)} · FAILED={len(failed)} / {len(sources)}",
            ]
            if failed:
                lines.append("")
                lines.append("Failed:")
                lines.extend(f"• {x}" for x in failed)
            summary = "\n".join(lines)
            self._log("\n" + summary)
            self.msgq.put(("job", f"Batch: {len(sources)} video(s) · OK {len(ok)} / FAILED {len(failed)}"))
            self.msgq.put(("error" if stopped else "done", summary))

        self.worker = threading.Thread(target=worker, daemon=True)
        self.worker.start()

    def _run_one_job(self, settings: dict, source: Path, ffmpeg: str, ffprobe: str,
                     cfg_values: dict, batch_mode: bool = False) -> tuple[str, Path]:
        """Planner + Edge TTS/Smart Sync for one video. Runs on the worker thread (no Tk calls)."""
        job_dir = _job_dir_for(source)
        job_dir.mkdir(parents=True, exist_ok=True)
        self.current_job = job_dir
        self.run_log_path = job_dir / "run.log"
        try:
            self.run_log_path.write_text("", encoding="utf-8")
        except Exception:
            pass
        stop_file = job_dir / ".stop_requested"
        if not self.stop_event.is_set():
            stop_file.unlink(missing_ok=True)
        self.msgq.put(("job", f"Batch · {job_dir.name}" if batch_mode else f"Job: {job_dir.name}"))
        try:
            (job_dir / "editor_overlays.json").write_text(json.dumps(settings["overlays"], ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass
        self._log(f"📁 Job folder: {job_dir}")

        planner_cfg = job_dir / "_planner_worker.json"
        planner_data = {
            "source_path": str(source),
            "source_title": settings["source_title"],
            "job_dir": str(job_dir),
            "runtime_root": str(ROOT_DIR / "recap_runtime"),
            "gemini_env_path": settings["gemini_env_path"],
            "ffprobe_path": ffprobe,
            "reuse": settings["reuse_transcript_and_plan"],
            "stop_file": str(stop_file),
        }
        for key in (
            "translation_batch_max_items", "story_context_batch_max_items", "context_overlap",
            "lookahead", "translation_audit", "atomic_max_duration", "atomic_target_duration",
            "gemini_request_timeout_seconds", "gemini_text_model",
        ):
            planner_data[key] = cfg_values[key]
        self._write_worker_cfg(planner_cfg, planner_data)
        planner_result = self._run_child(
            "planner_worker.py", planner_cfg,
            "STEP 1 — Transcript + Context + Timestamp Translation",
        )
        plan_path = str(planner_result.get("plan_path") or (job_dir / "smart_sync_plan.json"))

        plan_data = json.loads(Path(plan_path).read_text(encoding="utf-8"))
        segment_count = len(normalize_segments(plan_data))
        self._log(f"🧩 Final plan: {segment_count} segments → Edge TTS")

        if stop_file.exists():
            raise InterruptedError("Stopped after planner stage. START / RESUME ပြန်နှိပ်လို့ရပါတယ်။")

        self._log("✅ Transcript/translation complete.")
        self._log(f"🎙️ Edge TTS — voice={settings['voice']}")
        time.sleep(0.2)

        render_cfg = job_dir / "_render_worker.json"
        self._write_worker_cfg(render_cfg, {
            "job_dir": str(job_dir),
            "source_path": str(source),
            "plan_path": plan_path,
            "voice": settings["voice"],
            "edge_tts_rate": settings["edge_tts_rate"],
            "edge_tts_pitch": settings["edge_tts_pitch"],
            "ffmpeg_path": ffmpeg,
            "ffprobe_path": ffprobe,
            "max_video_speed": settings["max_video_speed"],
            "max_video_slow": settings["max_video_slow"],
            "smooth_freeze_fallback": settings["smooth_freeze_fallback"],
            "max_freeze_hold": settings["max_freeze_hold"],
            "mirror_video": settings["mirror_video"],
            "zoom_factor": settings["zoom_factor"],
            "render_final_video": settings["render_final_video"],
            "overlays": settings["overlays"],
            "voice_volume_percent": settings["voice_volume_percent"],
            "short_pauses": bool(settings.get("short_pauses", True)),
            "stop_file": str(stop_file),
        })
        result = self._run_child("render_worker.py", render_cfg, "STEP 2 — Edge TTS + Smart Sync").get("result") or {}
        final = result.get("final") or result.get("audio") or str(job_dir)
        return str(final), job_dir

    def _stop(self) -> None:
        self.stop_event.set()
        if self.current_job:
            try:
                (self.current_job / ".stop_requested").write_text("stop", encoding="utf-8")
            except Exception:
                pass
        self.status.set("STOP REQUESTED")
        self._log("⏹️ Stop requested — current safe boundary ရောက်တာနဲ့ရပ်မယ်။ Resume data မဖျက်ပါ။")

    def _running(self, yes: bool) -> None:
        self.start_btn.configure(state="disabled" if yes else "normal")
        self.stop_btn.configure(state="normal" if yes else "disabled")

    def _open_output(self) -> None:
        if not self.source.get().strip():
            messagebox.showinfo("Output", "Source video မရွေးရသေးပါ။")
            return
        job = _job_dir_for(Path(self.source.get().strip()))
        if not job.exists():
            messagebox.showinfo("Output", "Job folder မရှိသေးပါ။")
            return
        os.startfile(str(job))


if __name__ == "__main__":
    App().mainloop()
