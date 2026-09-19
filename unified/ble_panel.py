"""Bluetooth LED panel (embeddable Frame) for the unified light system."""

from __future__ import annotations

import importlib.util
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

_ROOT = Path(__file__).resolve().parents[1]
_BLE_APP = _ROOT / "BluetoothLED" / "app"
if str(_BLE_APP) not in sys.path:
    sys.path.insert(0, str(_BLE_APP))
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# ソフト炎エフェクト（laser_dmx_app の負 mode id と一致）
_LED_FX_FLAME = -101
# 旧 ID（ゆらぎ／一閃）も同じ着火→保持にマップ
_LED_CUSTOM_FX_IDS = frozenset({_LED_FX_FLAME, -102, -103})

try:
    from app_settings import load_settings, update_settings
except ImportError:

    def load_settings():  # type: ignore[misc, no-redef]
        return {
            "ble_address": "C6:70:45:84:1B:D8",
            "ble_protocol": "triones",
            "ble_auto_connect": False,
        }

    def update_settings(**kwargs):  # type: ignore[misc, no-redef]
        return kwargs

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
        try:
            _boot = load_settings()
            addr0 = str(_boot.get("ble_address", "") or "").strip() or DEFAULT_ADDRESS
            proto0 = str(_boot.get("ble_protocol", "triones") or "triones")
            if proto0 not in {"auto", "triones", "elk"}:
                proto0 = "triones"
            auto0 = bool(_boot.get("ble_auto_connect", False))
        except Exception:
            addr0 = DEFAULT_ADDRESS
            proto0 = "triones"
            auto0 = False
        self._protocol = tk.StringVar(value=proto0)
        self._address = tk.StringVar(value=addr0)
        self._ble_auto_connect = tk.BooleanVar(value=auto0)

        self._ai_active = False
        self._tile_override_active = False
        self._tile_override_lit = False
        self._color_busy = False
        self._pending_color: tuple[int, int, int, float] | None = None
        self._mode_busy = False
        self._pending_mode: tuple[int, int] | None = None
        self._custom_fx_job: str | None = None
        self._custom_fx_kind: int | None = None
        self._custom_fx_t0 = 0.0
        self._custom_fx_base = (255, 120, 20)
        self._custom_fx_bright = 100.0
        self._custom_fx_speed = 50

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

        auto_row = ttk.Frame(conn, style="BleCard.TFrame")
        auto_row.pack(fill="x", pady=(8, 0))
        ttk.Checkbutton(
            auto_row,
            text="起動時にこのアドレスへ自動接続",
            variable=self._ble_auto_connect,
            command=self._on_ble_auto_connect_toggled,
        ).pack(side="left")

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
            width=200,
            height=160,
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

    def set_tile_override(self, payload: dict | bool | None) -> None:
        """
        ポン出しタイル優先。
        - dict: LED を即時適用して AI 上書きを止める
        - True: 色は変えず AI 上書きだけ止める（凍結）
        - None/False: 優先解除（AI 停止中ならポン出しで点灯した LED を消灯）
        """
        if payload is None or payload is False:
            was = self._tile_override_active
            lit = bool(getattr(self, "_tile_override_lit", False))
            self._tile_override_active = False
            self._tile_override_lit = False
            self._stop_custom_fx()
            # AI が動いていないと最後のポン出し色のまま残る → 消灯
            if was and lit and not self._ai_active:
                try:
                    self._power(False)
                except Exception:
                    pass
            return
        self._tile_override_active = True
        if payload is True:
            # 凍結のみ（色は触らない）→ 解除時に消灯しない
            return
        if not isinstance(payload, dict):
            return
        self._tile_override_lit = True
        mode = payload.get("mode", None)
        speed = int(payload.get("speed", 50) or 50)
        bright = float(payload.get("brightness", 100.0) or 100.0)
        bright = max(1.0, min(100.0, bright))
        self._brightness.set(bright)
        self.bright_label.configure(text=f"{int(bright)}%")
        r = max(0, min(255, int(payload.get("r", 255))))
        g = max(0, min(255, int(payload.get("g", 80))))
        b = max(0, min(255, int(payload.get("b", 40))))

        # ソフト炎などカスタム FX
        if mode is not None:
            try:
                mode_i = int(mode)
            except (TypeError, ValueError):
                mode_i = None
            if mode_i is not None and mode_i in _LED_CUSTOM_FX_IDS:
                self._color = (r, g, b)
                try:
                    self.palette.set_rgb(r, g, b, notify=False)
                except Exception:
                    pass
                self.color_preview.configure(bg=f"#{r:02x}{g:02x}{b:02x}")
                self.rgb_label.configure(text=f"RGB ({r}, {g}, {b})")
                self._start_custom_fx(mode_i, r, g, b, bright, speed)
                return

        if mode is None:
            self._stop_custom_fx()
            self._color = (r, g, b)
            try:
                self.palette.set_rgb(r, g, b, notify=False)
            except Exception:
                pass
            self.color_preview.configure(bg=f"#{r:02x}{g:02x}{b:02x}")
            self.rgb_label.configure(text=f"RGB ({r}, {g}, {b})")
            self._queue_color()
            try:
                self._power(True)
            except Exception:
                pass
        else:
            self._stop_custom_fx()
            try:
                mode_i = int(mode)
            except (TypeError, ValueError):
                return
            try:
                if mode_i in self.mode_ids:
                    self.mode_combo.current(self.mode_ids.index(mode_i))
            except Exception:
                pass
            self._speed.set(max(1, min(100, speed)))
            self._pending_mode = (mode_i, max(1, min(100, speed)))
            self._flush_mode()
            try:
                self._power(True)
            except Exception:
                pass

    def _stop_custom_fx(self) -> None:
        jid = self._custom_fx_job
        self._custom_fx_job = None
        self._custom_fx_kind = None
        if jid is not None:
            try:
                self.after_cancel(jid)
            except (tk.TclError, ValueError):
                pass

    def _start_custom_fx(
        self,
        kind: int,
        r: int,
        g: int,
        b: int,
        bright: float,
        speed: int,
    ) -> None:
        self._stop_custom_fx()
        self._custom_fx_kind = int(kind)
        self._custom_fx_base = (r, g, b)
        self._custom_fx_bright = float(bright)
        self._custom_fx_speed = max(1, min(100, int(speed)))
        self._custom_fx_t0 = time.monotonic()
        try:
            self._power(True)
        except Exception:
            pass
        self._custom_fx_tick()

    def _flame_rgb(self, intensity: float) -> tuple[int, int, int]:
        """パレット色を強度でスケール（色は変えず明るさだけ）。"""
        br, bg, bb = self._custom_fx_base
        u = max(0.0, min(1.0, float(intensity)))
        u *= max(0.05, min(1.0, self._custom_fx_bright / 100.0))
        return int(br * u), int(bg * u), int(bb * u)

    def _custom_fx_tick(self) -> None:
        self._custom_fx_job = None
        if not self._tile_override_active or self._custom_fx_kind is None:
            return
        elapsed = time.monotonic() - self._custom_fx_t0
        sp = self._custom_fx_speed
        # 速度 1..100 → 立ち上がり時間（遅い〜一瞬）。滑らかに最高輝度へ。
        rise_dur = max(0.04, 0.95 - (sp / 100.0) * 0.90)

        if elapsed < rise_dur:
            u = elapsed / rise_dur
            # smoothstep: 最暗から滑らかに最大へ
            intensity = u * u * (3.0 - 2.0 * u)
            hold = False
        else:
            intensity = 1.0
            hold = True

        rgb = self._flame_rgb(intensity)
        self._color = rgb
        self.color_preview.configure(bg=f"#{rgb[0]:02x}{rgb[1]:02x}{rgb[2]:02x}")
        self._brightness.set(100.0)
        self.bright_label.configure(text="100%")
        self._queue_color()

        if hold:
            # 最高輝度で保持（以降のチカチカなし）
            return

        delay = int(max(16, min(40, rise_dur * 1000 / 24)))
        try:
            self._custom_fx_job = self.after(delay, self._custom_fx_tick)
        except tk.TclError:
            self._custom_fx_job = None

    def apply_ai_frame(self, frame: LightingFrame) -> None:
        if not self._ai_active:
            return
        if self._tile_override_active:
            # タイル優先中は解析表示だけ更新し、色は触らない
            mood_ja = MOOD_LABELS_JA.get(frame.mood, frame.mood)
            genre_ja = GENRE_LABELS_JA.get(frame.genre, frame.genre)
            section_ja = SECTION_LABELS_JA.get(frame.section, frame.section)
            self._ai_mood.set(f"{section_ja}/{genre_ja}/{mood_ja}·タイル優先")
            self._draw_level(frame.level)
            return
        self._color = (frame.r, frame.g, frame.b)
        bright_pct = max(1.0, min(100.0, frame.brightness * 100.0))
        if frame.beat and frame.style == "impact" and frame.style_score > 0.5:
            bright_pct = min(100.0, bright_pct + 12.0)
        if frame.release > 0.45:
            bright_pct = min(100.0, max(bright_pct, 70.0 + frame.release * 30.0))
        elif frame.tension > 0.4 and frame.effect == "tension_hold":
            bright_pct = min(bright_pct, 35.0 + (1.0 - frame.tension) * 20.0)
        # 加速ビート点滅: ON/OFF をはっきり出す
        if frame.effect == "accel_blink":
            bright_pct = 100.0 if frame.brightness >= 0.35 or frame.beat else 3.0
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
                    self.after(0, lambda r=result: on_ok(r))
            except Exception as exc:
                if on_err:
                    self.after(0, lambda e=exc: on_err(e))
                elif not silent:
                    self.after(0, lambda e=exc: messagebox.showerror("エラー", str(e)))

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

    def _on_ble_auto_connect_toggled(self) -> None:
        self._persist_ble_settings(include_address=True)

    def set_auto_connect(self, enabled: bool) -> None:
        self._ble_auto_connect.set(bool(enabled))

    def _persist_ble_settings(self, *, include_address: bool = True) -> None:
        kwargs: dict = {
            "ble_auto_connect": bool(self._ble_auto_connect.get()),
            "ble_protocol": str(self._protocol.get() or "triones"),
        }
        if include_address:
            addr = self._address.get().strip()
            if addr:
                kwargs["ble_address"] = addr
        try:
            update_settings(**kwargs)
        except Exception:
            pass

    def try_auto_connect(self) -> None:
        """保存済みアドレスへ接続を試みる（起動時用）。"""
        address = self._address.get().strip()
        if not address:
            try:
                address = str(load_settings().get("ble_address", "") or "").strip()
            except Exception:
                address = ""
            if address:
                self._address.set(address)
        if not address:
            self._set_status("自動接続スキップ: アドレス未設定")
            return
        self._connect_address()

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
            self._address.set(device.address)
            self._persist_ble_settings(include_address=True)
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
            self._persist_ble_settings(include_address=True)
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
        self._stop_custom_fx()
        try:
            fut = self.worker.submit(self.controller.disconnect())
            fut.result(timeout=0.6)
        except Exception:
            pass
        try:
            self.worker.stop()
        except Exception:
            pass
