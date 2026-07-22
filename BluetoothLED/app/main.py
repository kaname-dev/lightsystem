"""Windows GUI for Happy Lighting compatible Bluetooth LED controllers."""

from __future__ import annotations

import threading
import tkinter as tk
from tkinter import messagebox, ttk

from audio_reactive import (
    EFFECT_LABELS_JA,
    GENRE_LABELS_JA,
    MOOD_LABELS_JA,
    SECTION_LABELS_JA,
    STYLE_LABELS_JA,
    InputDevice,
    LightingFrame,
    ReactionMode,
    ReactiveLightingEngine,
    list_input_devices,
)
from ble_controller import AsyncWorker, DiscoveredDevice, LedController, ScanResult
from color_picker import ColorPalette
from protocol import MODE_LABELS_JA, BuiltInMode


DEFAULT_ADDRESS = "C6:70:45:84:1B:D8"


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Bluetooth LED Controller")
        self.geometry("780x880")
        self.minsize(720, 820)
        self.configure(bg="#1a1d23")

        self.worker = AsyncWorker()
        self.controller = LedController()
        self.devices: list[DiscoveredDevice] = []
        self._color = (255, 80, 40)
        self._brightness = tk.DoubleVar(value=100)
        self._speed = tk.IntVar(value=50)
        self._status = tk.StringVar(value="未接続")
        self._show_all = tk.BooleanVar(value=False)
        self._protocol = tk.StringVar(value="triones")
        self._address = tk.StringVar(value=DEFAULT_ADDRESS)

        # AI reactive lighting
        self._ai_active = False
        self._ai_sensitivity = tk.DoubleVar(value=1.4)
        self._ai_mode = tk.StringVar(value=ReactionMode.BALANCED.value)
        self._ai_mood = tk.StringVar(value="—")
        self._ai_level = 0.0
        self._reactive: ReactiveLightingEngine | None = None
        self._pending_ai_frame: LightingFrame | None = None
        self._ai_ui_scheduled = False
        self._input_devices: list[InputDevice] = []
        self._ai_device_label = tk.StringVar(value="システム既定 [既定]")

        # Live write coalescing (latest value wins while BLE write in flight)
        self._color_busy = False
        self._pending_color: tuple[int, int, int, float] | None = None
        self._mode_busy = False
        self._pending_mode: tuple[int, int] | None = None

        self._style()
        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self._loop_thread = threading.Thread(target=self.worker.start_in_thread, daemon=True)
        self._loop_thread.start()
        self.worker.wait_ready()

    def _style(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        bg = "#1a1d23"
        panel = "#242830"
        fg = "#e8eaed"
        accent = "#3d8bfd"
        style.configure(".", background=bg, foreground=fg, fieldbackground=panel)
        style.configure("TFrame", background=bg)
        style.configure("Card.TFrame", background=panel)
        style.configure("TLabel", background=bg, foreground=fg, font=("Segoe UI", 10))
        style.configure("Card.TLabel", background=panel, foreground=fg, font=("Segoe UI", 10))
        style.configure("Title.TLabel", background=bg, foreground=fg, font=("Segoe UI Semibold", 16))
        style.configure("Status.TLabel", background=bg, foreground="#9aa0a6", font=("Segoe UI", 9))
        style.configure("TButton", font=("Segoe UI", 10), padding=6)
        style.configure("Accent.TButton", font=("Segoe UI Semibold", 10), padding=8)
        style.map("TButton", background=[("active", "#3a3f4b")])
        style.configure("TCheckbutton", background=bg, foreground=fg)
        style.configure("Horizontal.TScale", background=bg)
        style.configure("TCombobox", fieldbackground=panel, foreground=fg)
        style.configure("TLabelframe", background=panel, foreground=fg)
        style.configure("TLabelframe.Label", background=panel, foreground=fg, font=("Segoe UI Semibold", 10))
        self._panel = panel
        self._accent = accent

    def _build_ui(self) -> None:
        pad = {"padx": 16, "pady": 8}

        header = ttk.Frame(self)
        header.pack(fill="x", **pad)
        ttk.Label(header, text="Bluetooth LED", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            header,
            text="操作はドラッグ中も即時反映されます",
            style="Status.TLabel",
        ).pack(anchor="w")

        conn = ttk.LabelFrame(self, text="接続", padding=12)
        conn.pack(fill="x", padx=16, pady=8)

        row = ttk.Frame(conn, style="Card.TFrame")
        row.pack(fill="x")
        ttk.Button(row, text="スキャン (12秒)", command=self._scan).pack(side="left")
        ttk.Checkbutton(row, text="すべて表示", variable=self._show_all).pack(side="left", padx=12)
        ttk.Label(row, text="プロトコル", style="Card.TLabel").pack(side="left", padx=(8, 4))
        ttk.Combobox(
            row,
            textvariable=self._protocol,
            values=["auto", "triones", "elk"],
            state="readonly",
            width=10,
        ).pack(side="left")
        ttk.Button(row, text="接続", style="Accent.TButton", command=self._connect).pack(side="right")
        ttk.Button(row, text="切断", command=self._disconnect).pack(side="right", padx=8)

        addr_row = ttk.Frame(conn, style="Card.TFrame")
        addr_row.pack(fill="x", pady=(10, 0))
        ttk.Label(addr_row, text="アドレス", style="Card.TLabel").pack(side="left")
        ttk.Entry(addr_row, textvariable=self._address, width=22).pack(side="left", padx=8)
        ttk.Button(addr_row, text="アドレスで接続", command=self._connect_address).pack(side="left")

        self.device_list = tk.Listbox(
            conn,
            height=4,
            bg="#1e2229",
            fg="#e8eaed",
            selectbackground=self._accent,
            selectforeground="#ffffff",
            highlightthickness=0,
            borderwidth=0,
            font=("Consolas", 10),
        )
        self.device_list.pack(fill="x", pady=(10, 0))

        ttk.Label(self, textvariable=self._status, style="Status.TLabel").pack(anchor="w", padx=16)

        body = ttk.Frame(self)
        body.pack(fill="both", expand=True, padx=16, pady=8)

        # Left: color palette
        color_frame = ttk.LabelFrame(body, text="カラーパレット", padding=12)
        color_frame.pack(side="left", fill="y", padx=(0, 12))

        preview_row = ttk.Frame(color_frame, style="Card.TFrame")
        preview_row.pack(fill="x", pady=(0, 8))
        self.color_preview = tk.Canvas(
            preview_row, width=36, height=36, bg=self._rgb_hex(), highlightthickness=0, bd=0
        )
        self.color_preview.pack(side="left")
        self.rgb_label = ttk.Label(preview_row, text=self._rgb_text(), style="Card.TLabel")
        self.rgb_label.pack(side="left", padx=10)

        self.palette = ColorPalette(
            color_frame,
            initial=self._color,
            on_change=self._on_palette_color,
            width=260,
            height=220,
        )
        self.palette.pack()

        presets = ttk.Frame(color_frame, style="Card.TFrame")
        presets.pack(fill="x", pady=(10, 0))
        for label, rgb in [
            ("赤", (255, 0, 0)),
            ("緑", (0, 255, 0)),
            ("青", (0, 0, 255)),
            ("白", (255, 255, 255)),
            ("暖白", (255, 180, 100)),
            ("紫", (180, 0, 255)),
            ("黄", (255, 220, 0)),
            ("シアン", (0, 220, 255)),
        ]:
            btn = tk.Button(
                presets,
                text=label,
                bg="#%02x%02x%02x" % rgb,
                fg="#000000" if sum(rgb) > 400 else "#ffffff",
                relief="flat",
                width=4,
                command=lambda c=rgb: self._set_preset(c),
            )
            btn.pack(side="left", padx=2, pady=2)

        # Right: controls
        right = ttk.Frame(body)
        right.pack(side="left", fill="both", expand=True)

        control = ttk.LabelFrame(right, text="コントロール", padding=12)
        control.pack(fill="x")

        power_row = ttk.Frame(control, style="Card.TFrame")
        power_row.pack(fill="x")
        ttk.Button(power_row, text="電源 ON", command=lambda: self._power(True)).pack(side="left")
        ttk.Button(power_row, text="電源 OFF", command=lambda: self._power(False)).pack(
            side="left", padx=8
        )

        bright_row = ttk.Frame(control, style="Card.TFrame")
        bright_row.pack(fill="x", pady=(14, 0))
        ttk.Label(bright_row, text="明るさ", style="Card.TLabel").pack(side="left")
        self.bright_scale = ttk.Scale(
            bright_row,
            from_=1,
            to=100,
            variable=self._brightness,
            command=self._on_brightness,
            orient="horizontal",
        )
        self.bright_scale.pack(side="left", fill="x", expand=True, padx=8)
        self.bright_label = ttk.Label(bright_row, text="100%", style="Card.TLabel", width=5)
        self.bright_label.pack(side="right")

        effects = ttk.LabelFrame(right, text="エフェクト（変更で即時反映）", padding=12)
        effects.pack(fill="x", pady=(12, 0))

        mode_row = ttk.Frame(effects, style="Card.TFrame")
        mode_row.pack(fill="x")
        ttk.Label(mode_row, text="モード", style="Card.TLabel").pack(side="left")
        self.mode_values = [MODE_LABELS_JA[m] for m in BuiltInMode]
        self.mode_ids = list(BuiltInMode)
        self.mode_combo = ttk.Combobox(mode_row, values=self.mode_values, state="readonly", width=28)
        self.mode_combo.current(0)
        self.mode_combo.pack(side="left", padx=8)
        self.mode_combo.bind("<<ComboboxSelected>>", lambda _e: self._apply_mode_live())

        speed_row = ttk.Frame(effects, style="Card.TFrame")
        speed_row.pack(fill="x", pady=(10, 0))
        ttk.Label(speed_row, text="速度（遅い← →速い）", style="Card.TLabel").pack(side="left")
        self.speed_scale = ttk.Scale(
            speed_row,
            from_=1,
            to=100,
            variable=self._speed,
            command=self._on_speed,
            orient="horizontal",
        )
        self.speed_scale.pack(side="left", fill="x", expand=True, padx=8)

        ai = ttk.LabelFrame(right, text="AI リアクティブ", padding=8)
        ai.pack(fill="x", pady=(12, 0))

        src_row = ttk.Frame(ai, style="Card.TFrame")
        src_row.pack(fill="x")
        ttk.Label(src_row, text="入力", style="Card.TLabel").pack(side="left")
        self.ai_device_combo = ttk.Combobox(
            src_row,
            textvariable=self._ai_device_label,
            state="readonly",
            width=36,
        )
        self.ai_device_combo.pack(side="left", fill="x", expand=True, padx=6)
        ttk.Button(src_row, text="更新", width=4, command=self._refresh_input_devices).pack(
            side="left"
        )

        ai_row = ttk.Frame(ai, style="Card.TFrame")
        ai_row.pack(fill="x", pady=(6, 0))
        self.ai_toggle_btn = ttk.Button(
            ai_row, text="開始", style="Accent.TButton", command=self._toggle_ai, width=6
        )
        self.ai_toggle_btn.pack(side="left")
        self.ai_mode_combo = ttk.Combobox(
            ai_row,
            textvariable=self._ai_mode,
            values=[m.value for m in ReactionMode],
            state="readonly",
            width=10,
        )
        self.ai_mode_combo.pack(side="left", padx=(8, 0))
        self.ai_mode_combo.bind("<<ComboboxSelected>>", lambda _e: self._on_ai_mode())
        ttk.Label(ai_row, textvariable=self._ai_mood, style="Card.TLabel").pack(
            side="left", padx=8
        )
        self.level_canvas = tk.Canvas(
            ai_row, width=72, height=10, bg="#1e2229", highlightthickness=0, bd=0
        )
        self.level_canvas.pack(side="right")
        self._level_bar = self.level_canvas.create_rectangle(
            0, 0, 0, 10, fill=self._accent, outline=""
        )

        sens_row = ttk.Frame(ai, style="Card.TFrame")
        sens_row.pack(fill="x", pady=(6, 0))
        ttk.Label(sens_row, text="感度", style="Card.TLabel").pack(side="left")
        self.ai_sens_scale = ttk.Scale(
            sens_row,
            from_=0.3,
            to=2.5,
            variable=self._ai_sensitivity,
            command=self._on_ai_sensitivity,
            orient="horizontal",
        )
        self.ai_sens_scale.pack(side="left", fill="x", expand=True, padx=6)
        self.ai_sens_label = ttk.Label(sens_row, text="1.0", style="Card.TLabel", width=3)
        self.ai_sens_label.pack(side="right")

        self._refresh_input_devices()

    def _rgb_hex(self) -> str:
        r, g, b = self._color
        return f"#{r:02x}{g:02x}{b:02x}"

    def _rgb_text(self) -> str:
        r, g, b = self._color
        return f"RGB ({r}, {g}, {b})"

    def _update_preview(self) -> None:
        self.color_preview.configure(bg=self._rgb_hex())
        self.rgb_label.configure(text=self._rgb_text())

    def _set_status(self, text: str) -> None:
        self.after(0, lambda: self._status.set(text))

    def _run(self, coro, on_ok=None, on_err=None, *, silent: bool = False):
        future = self.worker.submit(coro)

        def _watch():
            try:
                result = future.result()
                if on_ok:
                    self.after(0, lambda: on_ok(result))
            except Exception as exc:
                if on_err:
                    self.after(0, lambda: on_err(exc))
                elif not silent:
                    self.after(0, lambda: messagebox.showerror("エラー", str(exc)))

        threading.Thread(target=_watch, daemon=True).start()

    def _scan(self) -> None:
        self._set_status("スキャン中（約12秒）… スマホ側の接続は切ってください")
        led_only = not self._show_all.get()

        def ok(result: ScanResult):
            self.devices = result.devices
            self.device_list.delete(0, tk.END)
            if not result.devices:
                self.device_list.insert(
                    tk.END,
                    f"（LED機器なし / 周囲BLE {result.total_seen}件）アドレス接続を試してください",
                )
                self._set_status(
                    f"スキャン完了: LED 0件 / 全体 {result.total_seen}件。"
                    "スマホのHappy Lightingを切断して再スキャンしてください"
                )
                return
            for d in result.devices:
                self.device_list.insert(tk.END, d.label)
            self.device_list.selection_set(0)
            self.device_list.activate(0)
            self._address.set(result.devices[0].address)
            self._set_status(
                f"スキャン完了: LED {len(result.devices)}件 / 全体 {result.total_seen}件"
            )

        self._run(self.controller.scan(timeout=12.0, led_only=led_only), on_ok=ok)

    def _selected_device(self) -> DiscoveredDevice | None:
        sel = self.device_list.curselection()
        if not sel or not self.devices:
            return None
        idx = sel[0]
        if idx >= len(self.devices):
            return None
        return self.devices[idx]

    def _apply_protocol_choice(self) -> None:
        choice = self._protocol.get()
        if choice in {"triones", "elk"}:
            self.controller.set_protocol(choice)

    def _connect(self) -> None:
        device = self._selected_device()
        if not device:
            messagebox.showinfo("接続", "リストからデバイスを選択するか、「アドレスで接続」を使ってください")
            return
        self._set_status(f"接続中… {device.name}")

        def on_disc():
            self._set_status("切断されました")

        def ok(_):
            self._apply_protocol_choice()
            proto = self.controller.protocol
            char = self.controller.write_char.uuid if self.controller.write_char else "?"
            self._set_status(f"接続済み: {device.name}  [{proto}]  write={char}")
            self._queue_color()

        self._run(
            self.controller.connect(device.device, on_disconnect=on_disc),
            on_ok=ok,
        )

    def _connect_address(self) -> None:
        address = self._address.get().strip()
        if not address:
            messagebox.showinfo("接続", "アドレスを入力してください（例: C6:70:45:84:1B:D8）")
            return
        self._set_status(f"アドレス接続中… {address}")

        def on_disc():
            self._set_status("切断されました")

        def ok(_):
            self._apply_protocol_choice()
            if self._protocol.get() == "auto":
                self.controller.set_protocol("triones")
            proto = self.controller.protocol
            name = self.controller.device_name or address
            char = self.controller.write_char.uuid if self.controller.write_char else "?"
            self._set_status(f"接続済み: {name}  [{proto}]  write={char}")
            self._queue_color()

        self._run(
            self.controller.connect_address(address, on_disconnect=on_disc, timeout=12.0),
            on_ok=ok,
        )

    def _disconnect(self) -> None:
        self._run(self.controller.disconnect(), on_ok=lambda _: self._set_status("未接続"))

    def _power(self, on: bool) -> None:
        if not self.controller.connected:
            return
        self._run(
            self.controller.set_power(on),
            on_ok=lambda _: self._set_status("電源 ON" if on else "電源 OFF"),
            silent=True,
        )

    def _on_palette_color(self, r: int, g: int, b: int) -> None:
        if self._ai_active:
            return
        self._color = (r, g, b)
        self._update_preview()
        self._queue_color()

    def _set_preset(self, rgb: tuple[int, int, int]) -> None:
        if self._ai_active:
            return
        self._color = rgb
        self.palette.set_rgb(*rgb, notify=False)
        self._update_preview()
        self._queue_color()

    def _on_brightness(self, _value: str | None = None) -> None:
        value = int(float(self._brightness.get()))
        self.bright_label.configure(text=f"{value}%")
        if self._ai_active:
            return
        self._queue_color()

    def _on_speed(self, _value: str | None = None) -> None:
        self._apply_mode_live()

    def _on_ai_sensitivity(self, _value: str | None = None) -> None:
        sens = float(self._ai_sensitivity.get())
        self.ai_sens_label.configure(text=f"{sens:.1f}")
        if self._reactive is not None:
            self._reactive.set_sensitivity(sens)

    def _on_ai_mode(self) -> None:
        if self._reactive is not None:
            self._reactive.set_mode(self._ai_mode.get())

    def _refresh_input_devices(self) -> None:
        prev = self._ai_device_label.get()
        try:
            self._input_devices = list_input_devices()
        except Exception as exc:
            self._input_devices = [
                InputDevice(index=None, name="システム既定", channels=1, is_default=True)
            ]
            self._set_status(f"入力デバイス取得失敗: {exc}")
        labels = [d.label for d in self._input_devices]
        self.ai_device_combo.configure(values=labels)
        if prev in labels:
            self._ai_device_label.set(prev)
        elif labels:
            # Prefer first real default device label if present, else システム既定
            preferred = next((d.label for d in self._input_devices if d.index is None), labels[0])
            self._ai_device_label.set(preferred)

    def _selected_input_device(self) -> InputDevice | None:
        label = self._ai_device_label.get()
        for d in self._input_devices:
            if d.label == label:
                return d
        return self._input_devices[0] if self._input_devices else None

    def _toggle_ai(self) -> None:
        if self._ai_active:
            self._stop_ai()
        else:
            self._start_ai()

    def _start_ai(self) -> None:
        if self._reactive is not None and self._reactive.running:
            return

        device = self._selected_input_device()
        device_index = device.index if device else None

        def on_frame(frame: LightingFrame) -> None:
            self._pending_ai_frame = frame
            if not self._ai_ui_scheduled:
                self._ai_ui_scheduled = True
                self.after(0, self._drain_ai_frame)

        engine = ReactiveLightingEngine(on_frame=on_frame, device=device_index)
        engine.set_sensitivity(float(self._ai_sensitivity.get()))
        engine.set_mode(self._ai_mode.get())
        try:
            engine.start()
        except Exception as exc:
            messagebox.showerror(
                "マイク",
                f"入力を開始できませんでした。\n{exc}\n\n"
                "別の入力ソースを選ぶか、マイク権限を確認してください。",
            )
            return

        self._reactive = engine
        self._ai_active = True
        self.ai_toggle_btn.configure(text="停止")
        self.ai_device_combo.configure(state="disabled")
        self._ai_mood.set("…")
        name = device.name if device else "既定"
        self._set_status(f"AI 動作中 — {name}")

    def _drain_ai_frame(self) -> None:
        self._ai_ui_scheduled = False
        frame = self._pending_ai_frame
        self._pending_ai_frame = None
        if frame is not None and self._ai_active:
            self._apply_ai_frame(frame)

    def _stop_ai(self) -> None:
        self._ai_active = False
        self._pending_ai_frame = None
        self._ai_ui_scheduled = False
        if self._reactive is not None:
            self._reactive.stop()
            self._reactive = None
        self.ai_toggle_btn.configure(text="開始")
        self.ai_device_combo.configure(state="readonly")
        self._ai_mood.set("—")
        self._ai_level = 0.0
        self._draw_level(0.0)
        if self.controller.connected:
            self._set_status("接続済み（AI 停止）")
        else:
            self._set_status("未接続")

    def _apply_ai_frame(self, frame: LightingFrame) -> None:
        if not self._ai_active:
            return
        self._color = (frame.r, frame.g, frame.b)
        bright_pct = max(1.0, min(100.0, frame.brightness * 100.0))
        # Beat flash only for impact / punchy styles — avoid strobing calm tracks
        if frame.beat and frame.style == "impact" and frame.style_score > 0.5:
            bright_pct = min(100.0, bright_pct + 12.0)
        # Club drop release: hard brightness punch
        if frame.release > 0.45:
            bright_pct = min(100.0, max(bright_pct, 70.0 + frame.release * 30.0))
        elif frame.tension > 0.4 and frame.effect == "tension_hold":
            bright_pct = min(bright_pct, 35.0 + (1.0 - frame.tension) * 20.0)
        self._brightness.set(bright_pct)
        self.bright_label.configure(text=f"{int(bright_pct)}%")
        # Skip rebuilding palette marker every tick — preview only (snappier)
        self.color_preview.configure(bg=f"#{frame.r:02x}{frame.g:02x}{frame.b:02x}")
        self.rgb_label.configure(text=f"RGB ({frame.r}, {frame.g}, {frame.b})")
        mood_ja = MOOD_LABELS_JA.get(frame.mood, frame.mood)
        style_ja = STYLE_LABELS_JA.get(frame.style, frame.style)
        genre_ja = GENRE_LABELS_JA.get(frame.genre, frame.genre)
        section_ja = SECTION_LABELS_JA.get(frame.section, frame.section)
        effect_ja = EFFECT_LABELS_JA.get(frame.effect, "")
        beat_mark = "♪" if frame.beat else ""
        extra = f"·{effect_ja}" if effect_ja else ""
        self._ai_mood.set(f"{section_ja}/{genre_ja}/{mood_ja}{extra}{beat_mark}")
        self._ai_level = frame.level
        self._draw_level(frame.level)
        self._queue_color()

    def _draw_level(self, level: float) -> None:
        width = max(self.level_canvas.winfo_width(), 72)
        w = int(max(0.0, min(1.0, level)) * width)
        self.level_canvas.coords(self._level_bar, 0, 0, w, 10)

    def _queue_color(self) -> None:
        if not self.controller.connected:
            return
        r, g, b = self._color
        bright = float(self._brightness.get()) / 100.0
        self._pending_color = (r, g, b, bright)
        self._flush_color()

    def _flush_color(self) -> None:
        if self._color_busy or self._pending_color is None:
            return
        if not self.controller.connected:
            self._pending_color = None
            return
        r, g, b, bright = self._pending_color
        self._pending_color = None
        self._color_busy = True

        def done(_=None):
            self._color_busy = False
            if self._pending_color is not None:
                self._flush_color()

        self._run(
            self.controller.set_color(r, g, b, bright),
            on_ok=done,
            on_err=done,
            silent=True,
        )

    def _apply_mode_live(self) -> None:
        if not self.controller.connected:
            return
        idx = self.mode_combo.current()
        if idx < 0:
            return
        mode = int(self.mode_ids[idx])
        speed = int(float(self._speed.get()))
        self._pending_mode = (mode, speed)
        self._flush_mode()

    def _flush_mode(self) -> None:
        if self._mode_busy or self._pending_mode is None:
            return
        if not self.controller.connected:
            self._pending_mode = None
            return
        mode, speed = self._pending_mode
        self._pending_mode = None
        self._mode_busy = True

        def done(_=None):
            self._mode_busy = False
            if self._pending_mode is not None:
                self._flush_mode()

        self._run(
            self.controller.set_mode(mode, speed),
            on_ok=done,
            on_err=done,
            silent=True,
        )

    def _on_close(self) -> None:
        self._ai_active = False
        if self._reactive is not None:
            try:
                self._reactive.stop()
            except Exception:
                pass
            self._reactive = None
        try:
            fut = self.worker.submit(self.controller.disconnect())
            fut.result(timeout=2)
        except Exception:
            pass
        self.worker.stop()
        self.destroy()


def main() -> None:
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
