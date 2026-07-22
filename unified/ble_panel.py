"""Bluetooth LED panel (embeddable Frame) for the unified light system."""

from __future__ import annotations

import importlib.util
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

_BLE_APP = Path(__file__).resolve().parents[1] / "BluetoothLED" / "app"
if str(_BLE_APP) not in sys.path:
    sys.path.insert(0, str(_BLE_APP))

_spec = importlib.util.spec_from_file_location(
    "ble_led_audio_reactive_panel", _BLE_APP / "audio_reactive.py"
)
assert _spec and _spec.loader
_ble_ar = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _ble_ar
_spec.loader.exec_module(_ble_ar)

EFFECT_LABELS_JA = _ble_ar.EFFECT_LABELS_JA
GENRE_LABELS_JA = _ble_ar.GENRE_LABELS_JA
MOOD_LABELS_JA = _ble_ar.MOOD_LABELS_JA
SECTION_LABELS_JA = _ble_ar.SECTION_LABELS_JA
STYLE_LABELS_JA = _ble_ar.STYLE_LABELS_JA
LightingFrame = _ble_ar.LightingFrame

from ble_controller import AsyncWorker, DiscoveredDevice, LedController, ScanResult  # noqa: E402
from color_picker import ColorPalette  # noqa: E402
from protocol import MODE_LABELS_JA, BuiltInMode  # noqa: E402


DEFAULT_ADDRESS = "C6:70:45:84:1B:D8"


class BleLedPanel(ttk.Frame):
    """Bluetooth LED 操作パネル。音楽解析フレームは外部（統合 AI）から供給可能。"""

    def __init__(self, master: tk.Misc, *, hide_local_ai: bool = False) -> None:
        super().__init__(master)
        self._hide_local_ai = hide_local_ai
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

        self._ai_active = False
        self._color_busy = False
        self._pending_color: tuple[int, int, int, float] | None = None
        self._mode_busy = False
        self._pending_mode: tuple[int, int] | None = None

        self._style()
        self._build_ui()

        self._loop_thread = threading.Thread(target=self.worker.start_in_thread, daemon=True)
        self._loop_thread.start()
        self.worker.wait_ready()

    def _style(self) -> None:
        style = ttk.Style(self)
        bg = "#1a1d23"
        panel = "#242830"
        fg = "#e8eaed"
        accent = "#3d8bfd"
        # theme_use は親（統合アプリ）側に任せる。ここで再適用すると Combobox が壊れる。
        style.configure("Ble.TFrame", background=bg)
        style.configure("BleCard.TFrame", background=panel)
        style.configure("Ble.TLabel", background=bg, foreground=fg, font=("Segoe UI", 10))
        style.configure("BleCard.TLabel", background=panel, foreground=fg, font=("Segoe UI", 10))
        style.configure("BleTitle.TLabel", background=bg, foreground=fg, font=("Segoe UI Semibold", 14))
        style.configure("BleStatus.TLabel", background=bg, foreground="#9aa0a6", font=("Segoe UI", 9))
        style.configure("Ble.TLabelframe", background=panel, foreground=fg)
        style.configure(
            "Ble.TLabelframe.Label",
            background=panel,
            foreground=fg,
            font=("Segoe UI Semibold", 10),
        )
        self.configure(style="Ble.TFrame")
        self._panel = panel
        self._accent = accent
        self._bg = bg

    def _build_ui(self) -> None:
        pad = {"padx": 12, "pady": 6}
        self.configure(padding=4)

        header = ttk.Frame(self, style="Ble.TFrame")
        header.pack(fill="x", **pad)
        ttk.Label(header, text="Bluetooth LED", style="BleTitle.TLabel").pack(anchor="w")
        ttk.Label(
            header,
            text="操作はドラッグ中も即時反映されます",
            style="BleStatus.TLabel",
        ).pack(anchor="w")

        conn = ttk.LabelFrame(self, text="接続", padding=10, style="Ble.TLabelframe")
        conn.pack(fill="x", padx=12, pady=6)

        row = ttk.Frame(conn, style="BleCard.TFrame")
        row.pack(fill="x")
        ttk.Button(row, text="スキャン (12秒)", command=self._scan).pack(side="left")
        ttk.Checkbutton(row, text="すべて表示", variable=self._show_all).pack(side="left", padx=12)
        ttk.Label(row, text="プロトコル", style="BleCard.TLabel").pack(side="left", padx=(8, 4))
        ttk.Combobox(
            row,
            textvariable=self._protocol,
            values=["auto", "triones", "elk"],
            state="readonly",
            width=10,
        ).pack(side="left")
        ttk.Button(row, text="接続", command=self._connect).pack(side="right")
        ttk.Button(row, text="切断", command=self._disconnect).pack(side="right", padx=8)

        addr_row = ttk.Frame(conn, style="BleCard.TFrame")
        addr_row.pack(fill="x", pady=(10, 0))
        ttk.Label(addr_row, text="アドレス", style="BleCard.TLabel").pack(side="left")
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

        ttk.Label(self, textvariable=self._status, style="BleStatus.TLabel").pack(anchor="w", padx=12)

        body = ttk.Frame(self, style="Ble.TFrame")
        body.pack(fill="both", expand=True, padx=12, pady=6)

        color_frame = ttk.LabelFrame(body, text="カラーパレット", padding=10, style="Ble.TLabelframe")
        color_frame.pack(side="left", fill="y", padx=(0, 12))

        preview_row = ttk.Frame(color_frame, style="BleCard.TFrame")
        preview_row.pack(fill="x", pady=(0, 8))
        self.color_preview = tk.Canvas(
            preview_row, width=36, height=36, bg=self._rgb_hex(), highlightthickness=0, bd=0
        )
        self.color_preview.pack(side="left")
        self.rgb_label = ttk.Label(preview_row, text=self._rgb_text(), style="BleCard.TLabel")
        self.rgb_label.pack(side="left", padx=10)

        self.palette = ColorPalette(
            color_frame,
            initial=self._color,
            on_change=self._on_palette_color,
            width=240,
            height=200,
        )
        self.palette.pack()

        presets = ttk.Frame(color_frame, style="BleCard.TFrame")
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

        right = ttk.Frame(body, style="Ble.TFrame")
        right.pack(side="left", fill="both", expand=True)

        control = ttk.LabelFrame(right, text="コントロール", padding=10, style="Ble.TLabelframe")
        control.pack(fill="x")

        power_row = ttk.Frame(control, style="BleCard.TFrame")
        power_row.pack(fill="x")
        ttk.Button(power_row, text="電源 ON", command=lambda: self._power(True)).pack(side="left")
        ttk.Button(power_row, text="電源 OFF", command=lambda: self._power(False)).pack(
            side="left", padx=8
        )

        bright_row = ttk.Frame(control, style="BleCard.TFrame")
        bright_row.pack(fill="x", pady=(14, 0))
        ttk.Label(bright_row, text="明るさ", style="BleCard.TLabel").pack(side="left")
        self.bright_scale = ttk.Scale(
            bright_row,
            from_=1,
            to=100,
            variable=self._brightness,
            command=self._on_brightness,
            orient="horizontal",
        )
        self.bright_scale.pack(side="left", fill="x", expand=True, padx=8)
        self.bright_label = ttk.Label(bright_row, text="100%", style="BleCard.TLabel", width=5)
        self.bright_label.pack(side="right")

        effects = ttk.LabelFrame(right, text="エフェクト（変更で即時反映）", padding=10, style="Ble.TLabelframe")
        effects.pack(fill="x", pady=(12, 0))

        mode_row = ttk.Frame(effects, style="BleCard.TFrame")
        mode_row.pack(fill="x")
        ttk.Label(mode_row, text="モード", style="BleCard.TLabel").pack(side="left")
        self.mode_values = [MODE_LABELS_JA[m] for m in BuiltInMode]
        self.mode_ids = list(BuiltInMode)
        self.mode_combo = ttk.Combobox(mode_row, values=self.mode_values, state="readonly", width=28)
        self.mode_combo.current(0)
        self.mode_combo.pack(side="left", padx=8)
        self.mode_combo.bind("<<ComboboxSelected>>", lambda _e: self._apply_mode_live())

        speed_row = ttk.Frame(effects, style="BleCard.TFrame")
        speed_row.pack(fill="x", pady=(10, 0))
        ttk.Label(speed_row, text="速度（遅い← →速い）", style="BleCard.TLabel").pack(side="left")
        self.speed_scale = ttk.Scale(
            speed_row,
            from_=1,
            to=100,
            variable=self._speed,
            command=self._on_speed,
            orient="horizontal",
        )
        self.speed_scale.pack(side="left", fill="x", expand=True, padx=8)

        if not self._hide_local_ai:
            ttk.Label(
                right,
                text="※ 音楽連動は統合アプリ上部の「共有 AI リアクティブ」を使います",
                style="BleStatus.TLabel",
            ).pack(anchor="w", pady=(12, 0))

        self._ai_mood = tk.StringVar(value="—")
        mood_row = ttk.Frame(right, style="Ble.TFrame")
        mood_row.pack(fill="x", pady=(8, 0))
        ttk.Label(mood_row, text="解析:", style="Ble.TLabel").pack(side="left")
        ttk.Label(mood_row, textvariable=self._ai_mood, style="BleStatus.TLabel").pack(
            side="left", padx=6
        )
        self.level_canvas = tk.Canvas(
            mood_row, width=72, height=10, bg="#1e2229", highlightthickness=0, bd=0
        )
        self.level_canvas.pack(side="right")
        self._level_bar = self.level_canvas.create_rectangle(
            0, 0, 0, 10, fill=self._accent, outline=""
        )

    def set_ai_active(self, active: bool) -> None:
        self._ai_active = bool(active)
        if not active:
            self._ai_mood.set("—")
            self._draw_level(0.0)

    def apply_ai_frame(self, frame: LightingFrame) -> None:
        if not self._ai_active:
            return
        self._color = (frame.r, frame.g, frame.b)
        bright_pct = max(1.0, min(100.0, frame.brightness * 100.0))
        if frame.beat and frame.style == "impact" and frame.style_score > 0.5:
            bright_pct = min(100.0, bright_pct + 12.0)
        if frame.release > 0.45:
            bright_pct = min(100.0, max(bright_pct, 70.0 + frame.release * 30.0))
        elif frame.tension > 0.4 and frame.effect == "tension_hold":
            bright_pct = min(bright_pct, 35.0 + (1.0 - frame.tension) * 20.0)
        self._brightness.set(bright_pct)
        self.bright_label.configure(text=f"{int(bright_pct)}%")
        self.color_preview.configure(bg=f"#{frame.r:02x}{frame.g:02x}{frame.b:02x}")
        self.rgb_label.configure(text=f"RGB ({frame.r}, {frame.g}, {frame.b})")
        mood_ja = MOOD_LABELS_JA.get(frame.mood, frame.mood)
        genre_ja = GENRE_LABELS_JA.get(frame.genre, frame.genre)
        section_ja = SECTION_LABELS_JA.get(frame.section, frame.section)
        effect_ja = EFFECT_LABELS_JA.get(frame.effect, "")
        beat_mark = "♪" if frame.beat else ""
        extra = f"·{effect_ja}" if effect_ja else ""
        self._ai_mood.set(f"{section_ja}/{genre_ja}/{mood_ja}{extra}{beat_mark}")
        self._draw_level(frame.level)
        self._queue_color()

    def _rgb_hex(self) -> str:
        r, g, b = self._color
        return f"#{r:02x}{g:02x}{b:02x}"

    def _rgb_text(self) -> str:
        r, g, b = self._color
        return f"RGB ({r}, {g}, {b})"

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
            messagebox.showinfo(
                "接続", "リストからデバイスを選択するか、「アドレスで接続」を使ってください"
            )
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
        self.color_preview.configure(bg=self._rgb_hex())
        self.rgb_label.configure(text=self._rgb_text())
        self._queue_color()

    def _set_preset(self, rgb: tuple[int, int, int]) -> None:
        if self._ai_active:
            return
        self._color = rgb
        self.palette.set_rgb(*rgb, notify=False)
        self.color_preview.configure(bg=self._rgb_hex())
        self.rgb_label.configure(text=self._rgb_text())
        self._queue_color()

    def _on_brightness(self, _value: str | None = None) -> None:
        value = int(float(self._brightness.get()))
        self.bright_label.configure(text=f"{value}%")
        if self._ai_active:
            return
        self._queue_color()

    def _on_speed(self, _value: str | None = None) -> None:
        self._apply_mode_live()

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

    def shutdown(self) -> None:
        self._ai_active = False
        try:
            fut = self.worker.submit(self.controller.disconnect())
            fut.result(timeout=2)
        except Exception:
            pass
        self.worker.stop()
