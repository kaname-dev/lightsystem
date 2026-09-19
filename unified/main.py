"""統合照明アプリ — Bluetooth LED + DMX レーザー（音楽解析は BluetoothLED 方式）。"""

from __future__ import annotations

import os
import sys
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

ROOT = Path(__file__).resolve().parents[1]
DMX_DIR = ROOT / "DMX"
BLE_APP = ROOT / "BluetoothLED" / "app"
UNIFIED_DIR = Path(__file__).resolve().parent

# DMX を先に載せ、audio_reactive = ClubAudioAnalyzer ブリッジを確定させる
for p in (str(UNIFIED_DIR), str(DMX_DIR), str(ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

from app_settings import load_settings, update_settings  # noqa: E402
from audio_reactive import ClubAudioAnalyzer  # noqa: E402
from laser_dmx_app import LaserDMXApp  # noqa: E402
from ble_panel import BleLedPanel  # noqa: E402

import importlib.util  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "ble_led_audio_reactive_ui", BLE_APP / "audio_reactive.py"
)
assert _spec and _spec.loader
_ble_ar = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _ble_ar
_spec.loader.exec_module(_ble_ar)
ReactionMode = _ble_ar.ReactionMode
LightingFrame = _ble_ar.LightingFrame


class LightSystemApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Light System — Bluetooth LED + DMX")
        self.geometry("980x820")
        self.minsize(860, 700)
        self.configure(bg="#1a1d23")

        self._analyzer: ClubAudioAnalyzer | None = None
        self._ai_active = False
        self._pending_frame: LightingFrame | None = None
        self._ai_ui_scheduled = False

        self._ai_sensitivity = tk.DoubleVar(value=1.4)
        self._ai_mode = tk.StringVar(value=ReactionMode.BALANCED.value)
        self._ai_device_label = tk.StringVar(value="")
        self._ai_status = tk.StringVar(value="共有 AI: 停止中")
        self._device_rows: list[tuple[int | None, str]] = []
        try:
            boot = load_settings()
            self._ble_auto_connect = tk.BooleanVar(value=bool(boot.get("ble_auto_connect", False)))
            self._dmx_auto_connect = tk.BooleanVar(value=bool(boot.get("dmx_auto_connect", False)))
        except Exception:
            self._ble_auto_connect = tk.BooleanVar(value=False)
            self._dmx_auto_connect = tk.BooleanVar(value=False)

        self._build_chrome()
        self._refresh_devices()
        # 子パネル生成後に Combobox スタイルを再適用（埋め込み側の theme 操作対策）
        self._apply_combobox_style()
        self.after(500, self._maybe_auto_connect)

        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _apply_combobox_style(self) -> None:
        style = ttk.Style(self)
        panel = "#2b3038"
        fg = "#e8eaed"
        accent = "#3d8bfd"
        muted = "#9aa0a6"
        style.configure(
            "TCombobox",
            fieldbackground=panel,
            background=panel,
            foreground=fg,
            arrowcolor=fg,
            insertcolor=fg,
            selectbackground=accent,
            selectforeground="#ffffff",
        )
        style.map(
            "TCombobox",
            fieldbackground=[("readonly", panel), ("!disabled", panel), ("disabled", "#1e2229")],
            foreground=[("readonly", fg), ("!disabled", fg), ("disabled", muted)],
            background=[("readonly", panel), ("!disabled", panel)],
            selectbackground=[("readonly", accent), ("!disabled", accent)],
            selectforeground=[("readonly", "#ffffff"), ("!disabled", "#ffffff")],
            arrowcolor=[("readonly", fg), ("!disabled", fg)],
        )
        # Listbox ポップダウン（font 指定は不正値だと展開失敗の原因になるので色のみ）
        self.option_add("*TCombobox*Listbox.background", panel)
        self.option_add("*TCombobox*Listbox.foreground", fg)
        self.option_add("*TCombobox*Listbox.selectBackground", accent)
        self.option_add("*TCombobox*Listbox.selectForeground", "#ffffff")
        try:
            style.configure("ComboboxPopdownFrame", background=panel, borderwidth=1, relief="solid")
        except tk.TclError:
            pass

    def _build_chrome(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        bg = "#1a1d23"
        panel = "#2b3038"
        fg = "#e8eaed"
        muted = "#9aa0a6"

        style.configure(".", background=bg, foreground=fg)
        style.configure("TFrame", background=bg)
        style.configure("TLabel", background=bg, foreground=fg)
        style.configure("TLabelframe", background=panel, foreground=fg)
        style.configure("TLabelframe.Label", background=panel, foreground=fg)
        style.configure("TNotebook", background=bg)
        style.configure("TNotebook.Tab", padding=(14, 6), background=panel, foreground=fg)
        style.map("TNotebook.Tab", background=[("selected", bg)], foreground=[("selected", fg)])
        style.configure("TButton", background=panel, foreground=fg, padding=6)
        style.map("TButton", background=[("active", "#3a3f4b")], foreground=[("disabled", muted)])
        style.configure("TCheckbutton", background=bg, foreground=fg)
        style.configure("Horizontal.TScale", background=bg)
        style.configure(
            "TEntry",
            fieldbackground=panel,
            foreground=fg,
            insertcolor=fg,
        )
        style.configure(
            "TSpinbox",
            fieldbackground=panel,
            foreground=fg,
            insertcolor=fg,
            arrowcolor=fg,
        )
        self._apply_combobox_style()

        # 上部はコンパクトなステータスのみ（詳細は「共通」タブ）
        status_bar = ttk.Frame(self)
        status_bar.pack(fill="x", padx=12, pady=(10, 4))
        self._ai_btn = ttk.Button(status_bar, text="AI開始", command=self._toggle_ai, width=8)
        self._ai_btn.pack(side="left")
        ttk.Label(status_bar, textvariable=self._ai_status).pack(side="left", padx=(10, 0))

        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        self._main_nb = nb

        common_tab = ttk.Frame(nb)
        ble_tab = ttk.Frame(nb)
        dmx_tab = ttk.Frame(nb)
        nb.add(common_tab, text="共通")
        nb.add(ble_tab, text="Bluetooth LED")
        nb.add(dmx_tab, text="DMX レーザー")

        # --- 共通タブ：AI／接続 ＋ タイムライン（LED/Laser非依存） ---
        common_nb = ttk.Notebook(common_tab)
        common_nb.pack(fill="both", expand=True, padx=4, pady=4)

        common_ai = ttk.Frame(common_nb)
        common_shared = ttk.Frame(common_nb)
        common_nb.add(common_ai, text="AI・接続")
        common_nb.add(common_shared, text="タイムライン")

        ai = ttk.LabelFrame(common_ai, text="共有 AI リアクティブ（LED + レーザー）", padding=10)
        ai.pack(fill="x", padx=8, pady=8)

        row1 = ttk.Frame(ai)
        row1.pack(fill="x")
        ttk.Label(row1, text="入力").pack(side="left")
        self._device_combo = None  # 互換用（未使用）
        self._device_entry = ttk.Entry(
            row1, textvariable=self._ai_device_label, state="readonly", width=48
        )
        self._device_entry.pack(side="left", fill="x", expand=True, padx=(8, 4))
        self._device_entry.bind("<Button-1>", lambda _e: self._open_device_picker())
        self._device_pick_btn = ttk.Button(
            row1, text="▼", width=3, command=self._open_device_picker
        )
        self._device_pick_btn.pack(side="left")
        ttk.Button(row1, text="更新", width=5, command=self._refresh_devices).pack(
            side="left", padx=(6, 0)
        )
        self._device_picker: tk.Toplevel | None = None

        row2 = ttk.Frame(ai)
        row2.pack(fill="x", pady=(8, 0))
        self._mode_combo = ttk.Combobox(
            row2,
            textvariable=self._ai_mode,
            values=[m.value for m in ReactionMode],
            state="readonly",
            width=12,
            height=8,
        )
        self._mode_combo.pack(side="left")
        self._mode_combo.bind("<<ComboboxSelected>>", lambda _e: self._on_ai_mode())
        ttk.Label(row2, text="感度").pack(side="left", padx=(14, 4))
        ttk.Scale(
            row2,
            from_=0.3,
            to=2.5,
            variable=self._ai_sensitivity,
            command=self._on_ai_sensitivity,
            orient="horizontal",
            length=180,
        ).pack(side="left", fill="x", expand=True)
        self._sens_lbl = ttk.Label(row2, text="1.4", width=4)
        self._sens_lbl.pack(side="left", padx=4)

        boot = ttk.LabelFrame(common_ai, text="起動時の自動接続", padding=8)
        boot.pack(fill="x", padx=8, pady=(0, 8))
        boot_row = ttk.Frame(boot)
        boot_row.pack(fill="x")
        ttk.Checkbutton(
            boot_row,
            text="Bluetooth LED を自動接続",
            variable=self._ble_auto_connect,
            command=self._on_boot_auto_connect_changed,
        ).pack(side="left", padx=(0, 16))
        ttk.Checkbutton(
            boot_row,
            text="DMX（COM）を自動接続",
            variable=self._dmx_auto_connect,
            command=self._on_boot_auto_connect_changed,
        ).pack(side="left")
        ttk.Label(
            boot,
            text="前回の BLE アドレス / COM を記憶し、次回起動時に再接続します。",
            foreground="#9aa0a6",
        ).pack(anchor="w", pady=(4, 0))

        self.ble = BleLedPanel(ble_tab, hide_local_ai=True)
        self.ble.pack(fill="both", expand=True)

        self.dmx = LaserDMXApp(
            dmx_tab, hide_audio_section=True, shared_host=common_shared
        )
        self.dmx.pack(fill="both", expand=True)
        try:
            self.dmx.set_tile_led_override_handler(self.ble.set_tile_override)
        except Exception:
            pass
        # パネル側チェックと上部設定を同期
        try:
            self.ble.set_auto_connect(bool(self._ble_auto_connect.get()))
            self.dmx.set_auto_connect(bool(self._dmx_auto_connect.get()))
        except Exception:
            pass

    def _on_boot_auto_connect_changed(self) -> None:
        ble_on = bool(self._ble_auto_connect.get())
        dmx_on = bool(self._dmx_auto_connect.get())
        try:
            update_settings(ble_auto_connect=ble_on, dmx_auto_connect=dmx_on)
        except Exception:
            pass
        try:
            self.ble.set_auto_connect(ble_on)
            self.ble._persist_ble_settings(include_address=True)
        except Exception:
            pass
        try:
            self.dmx.set_auto_connect(dmx_on)
            self.dmx._on_dmx_auto_connect_toggled()
        except Exception:
            pass

    def _maybe_auto_connect(self) -> None:
        """起動直後に、設定されていれば BLE / DMX へ自動接続する。"""
        try:
            settings = load_settings()
        except Exception:
            settings = {}
        if bool(settings.get("dmx_auto_connect", False)) or bool(self._dmx_auto_connect.get()):
            try:
                ok = self.dmx.try_auto_connect(silent=True)
                if ok:
                    self._ai_status.set("共有 AI: 停止中（DMX 自動接続済み）")
            except Exception:
                pass
        if bool(settings.get("ble_auto_connect", False)) or bool(self._ble_auto_connect.get()):
            try:
                self.ble.try_auto_connect()
            except Exception:
                pass

    def _refresh_devices(self) -> None:
        rows: list[tuple[int | None, str]] = []
        try:
            for idx, label in ClubAudioAnalyzer.list_input_devices():
                dev_id: int | None = None if idx < 0 else int(idx)
                rows.append((dev_id, label))
        except Exception as exc:
            rows = [(None, f"default: システム既定（取得失敗: {exc}）")]
        self._device_rows = rows
        labels = [r[1] for r in rows]
        chosen = self._pick_saved_device_label(labels)
        if chosen:
            self._ai_device_label.set(chosen)
        elif labels:
            self._ai_device_label.set(labels[0])
        else:
            self._ai_device_label.set("")

    def _device_name_key(self, label: str) -> str:
        """'12: Device Name' / 'default: …' から比較用の名前部分を取る。"""
        s = (label or "").strip()
        if ": " in s:
            return s.split(": ", 1)[1].strip().lower()
        return s.lower()

    def _pick_saved_device_label(self, labels: list[str]) -> str | None:
        if not labels:
            return None
        try:
            settings = load_settings()
        except Exception:
            settings = {}
        saved_label = str(settings.get("audio_input_label", "") or "").strip()
        try:
            saved_id = int(settings.get("audio_input_id", -1))
        except (TypeError, ValueError):
            saved_id = -1
        # 1) ラベル完全一致
        if saved_label and saved_label in labels:
            return saved_label
        # 2) デバイス ID 一致
        if saved_id >= 0:
            for idx, lab in self._device_rows:
                if idx == saved_id:
                    return lab
        # 3) 名前部分の部分一致（インデックスが変わっても復元）
        if saved_label:
            key = self._device_name_key(saved_label)
            if key:
                for lab in labels:
                    if self._device_name_key(lab) == key:
                        return lab
                for lab in labels:
                    if key in self._device_name_key(lab) or self._device_name_key(lab) in key:
                        return lab
        # 4) 今の UI 選択がまだ有効なら維持
        cur = self._ai_device_label.get()
        if cur in labels:
            return cur
        return None

    def _persist_audio_input(self) -> None:
        label = self._ai_device_label.get().strip()
        dev_id = self._selected_device_id()
        try:
            update_settings(
                audio_input_label=label,
                audio_input_id=(-1 if dev_id is None else int(dev_id)),
            )
        except Exception:
            pass

    def _close_device_picker(self) -> None:
        win = self._device_picker
        self._device_picker = None
        if win is not None:
            try:
                win.destroy()
            except tk.TclError:
                pass

    def _on_global_button1(self, event: tk.Event) -> None:
        if self._device_picker is None:
            return
        win = self._device_picker
        w = event.widget
        try:
            if str(w).startswith(str(win)):
                return
        except tk.TclError:
            return
        if w in (self._device_pick_btn, self._device_entry):
            return
        self._close_device_picker()

    def _open_device_picker(self) -> None:
        if self._ai_active:
            return
        self._close_device_picker()
        labels = [r[1] for r in self._device_rows]
        if not labels:
            self._refresh_devices()
            labels = [r[1] for r in self._device_rows]
        if not labels:
            messagebox.showinfo("入力", "入力デバイスが見つかりませんでした。", parent=self)
            return

        win = tk.Toplevel(self)
        self._device_picker = win
        win.withdraw()
        win.overrideredirect(True)
        win.configure(bg="#2b3038", highlightbackground="#3d8bfd", highlightthickness=1)
        win.attributes("-topmost", True)

        lb = tk.Listbox(
            win,
            height=min(14, max(4, len(labels))),
            bg="#2b3038",
            fg="#e8eaed",
            selectbackground="#3d8bfd",
            selectforeground="#ffffff",
            activestyle="dotbox",
            highlightthickness=0,
            borderwidth=0,
            font=("Segoe UI", 10),
            exportselection=False,
        )
        sb = ttk.Scrollbar(win, orient="vertical", command=lb.yview)
        lb.configure(yscrollcommand=sb.set)
        lb.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        for lab in labels:
            lb.insert(tk.END, lab)
        cur = self._ai_device_label.get()
        if cur in labels:
            i = labels.index(cur)
            lb.selection_set(i)
            lb.see(i)

        def choose(_event: tk.Event | None = None) -> None:
            sel = lb.curselection()
            if not sel:
                return
            self._ai_device_label.set(labels[int(sel[0])])
            self._persist_audio_input()
            self._close_device_picker()

        lb.bind("<ButtonRelease-1>", choose)
        lb.bind("<Return>", choose)
        lb.bind("<Double-Button-1>", choose)
        win.bind("<Escape>", lambda _e: self._close_device_picker())

        self.update_idletasks()
        anchor = self._device_entry
        x = anchor.winfo_rootx()
        y = anchor.winfo_rooty() + anchor.winfo_height()
        w = max(anchor.winfo_width() + self._device_pick_btn.winfo_width(), 320)
        win.geometry(f"{w}x{min(320, 24 * min(14, len(labels)) + 8)}+{x}+{y}")
        win.deiconify()
        win.lift()
        lb.focus_set()
        if not getattr(self, "_device_picker_bound", False):
            self.bind_all("<Button-1>", self._on_global_button1, add="+")
            self._device_picker_bound = True

    def _selected_device_id(self) -> int | None:
        label = self._ai_device_label.get()
        for idx, lab in self._device_rows:
            if lab == label:
                return idx
        return None

    def _on_ai_sensitivity(self, _v: str | None = None) -> None:
        sens = float(self._ai_sensitivity.get())
        self._sens_lbl.configure(text=f"{sens:.1f}")
        if self._analyzer is not None:
            self._analyzer.gate_sensitivity = sens

    def _on_ai_mode(self) -> None:
        if self._analyzer is not None:
            self._analyzer.set_reaction_mode(self._ai_mode.get())

    def _toggle_ai(self) -> None:
        if self._ai_active:
            self._stop_ai()
        else:
            self._start_ai()

    def _start_ai(self) -> None:
        if self._analyzer is not None:
            return
        self._close_device_picker()
        self._persist_audio_input()
        device = self._selected_device_id()

        def on_frame(frame: LightingFrame) -> None:
            self._pending_frame = frame
            if not self._ai_ui_scheduled:
                self._ai_ui_scheduled = True
                self.after(0, self._drain_frame)

        try:
            analyzer = ClubAudioAnalyzer(device=device, on_lighting_frame=on_frame)
            analyzer.gate_sensitivity = float(self._ai_sensitivity.get())
            analyzer.set_reaction_mode(self._ai_mode.get())
            analyzer.start()
        except Exception as exc:
            messagebox.showerror(
                "マイク",
                f"入力を開始できませんでした。\n{exc}\n\n"
                "別の入力ソースを選ぶか、マイク権限を確認してください。",
            )
            return

        self._analyzer = analyzer
        self._ai_active = True
        self.ble.set_ai_active(True)
        self.dmx.attach_shared_audio(analyzer)
        self._ai_btn.configure(text="AI停止")
        self._device_pick_btn.configure(state="disabled")
        self._ai_status.set("共有 AI: 動作中 — LED + レーザー連動")

    def _drain_frame(self) -> None:
        self._ai_ui_scheduled = False
        frame = self._pending_frame
        self._pending_frame = None
        if frame is not None and self._ai_active:
            self.ble.apply_ai_frame(frame)

    def _stop_ai(self) -> None:
        self._ai_active = False
        self._pending_frame = None
        self._ai_ui_scheduled = False
        self.ble.set_ai_active(False)
        self.dmx.attach_shared_audio(None)
        if self._analyzer is not None:
            try:
                self._analyzer.stop()
            except Exception:
                pass
            self._analyzer = None
        self._ai_btn.configure(text="AI開始")
        self._device_pick_btn.configure(state="normal")
        self._ai_status.set("共有 AI: 停止中")

    def _on_close(self) -> None:
        """バツボタンでクリーン終了（BLE/PortAudio 解体時のクラッシュ風ハングを防ぐ）。"""
        for step in (
            self._close_device_picker,
            self._persist_audio_input,
            self._stop_ai,
            self.ble.shutdown,
            self.dmx.cleanup,
        ):
            try:
                step()
            except Exception:
                pass
        try:
            self.quit()
        except Exception:
            pass
        try:
            self.destroy()
        except Exception:
            pass
        # バックグラウンドスレッド／ネイティブコールバックの終了待ちで
        # インタプリタ解体が例外になるのを避け、正常終了にする
        os._exit(0)


def main() -> None:
    app = LightSystemApp()
    try:
        app.mainloop()
    finally:
        os._exit(0)


if __name__ == "__main__":
    main()
