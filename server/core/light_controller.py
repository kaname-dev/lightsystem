"""Headless controller: DMX, BLE, timeline playback, punch tiles/cues."""

from __future__ import annotations

import asyncio
import json
import math
import shutil
import sys
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
DMX_DIR = ROOT / "DMX"
BLE_APP = ROOT / "BluetoothLED" / "app"
# DMX を BLE より前に（audio_reactive / music_precalc が DMX 版になるように）
for p in (str(BLE_APP), str(DMX_DIR), str(ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

from server.core.dmx_sender import DMX_CHANNELS, OpenDMXSender, list_com_ports  # noqa: E402

try:
    import sounddevice as sd
except ImportError:
    sd = None  # type: ignore[assignment]

try:
    from music_precalc import load_audio_for_playback as _load_audio_for_playback
except ImportError:
    _load_audio_for_playback = None  # type: ignore[misc, assignment]


def load_audio_for_playback(path: str | Path, *, target_sr: int | None = 44100):
    """再生用モノラル読込（music_precalc 優先、失敗時はローカル実装）。"""
    if _load_audio_for_playback is not None:
        return _load_audio_for_playback(path, target_sr=target_sr)
    path = Path(path)
    if not path.is_file():
        return None
    # soundfile
    try:
        import soundfile as sf  # type: ignore

        data, sr = sf.read(str(path), always_2d=False)
        y = np.asarray(data, dtype=np.float32)
        if y.ndim > 1:
            y = np.mean(y, axis=1).astype(np.float32)
        sr_i = int(sr)
        if target_sr is not None and sr_i != int(target_sr):
            try:
                import librosa

                y = np.asarray(
                    librosa.resample(y, orig_sr=sr_i, target_sr=int(target_sr)),
                    dtype=np.float32,
                )
                sr_i = int(target_sr)
            except Exception:
                pass
        return y, sr_i, float(len(y) / max(1, sr_i))
    except Exception:
        pass
    # librosa
    try:
        import librosa

        y, sr = librosa.load(str(path), sr=target_sr, mono=True)
        y = np.asarray(y, dtype=np.float32)
        sr_i = int(sr)
        return y, sr_i, float(len(y) / max(1, sr_i))
    except Exception:
        pass
    # wav
    try:
        import wave

        with wave.open(str(path), "rb") as wf:
            nch = wf.getnchannels()
            sw = wf.getsampwidth()
            sr_i = int(wf.getframerate())
            raw = wf.readframes(wf.getnframes())
        if sw == 1:
            arr = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
        elif sw == 2:
            arr = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
        elif sw == 4:
            arr = np.frombuffer(raw, dtype=np.int32).astype(np.float32) / 2147483648.0
        else:
            return None
        if nch > 1:
            arr = arr.reshape(-1, nch).mean(axis=1)
        if target_sr is not None and sr_i != int(target_sr):
            try:
                import librosa

                arr = np.asarray(
                    librosa.resample(arr, orig_sr=sr_i, target_sr=int(target_sr)),
                    dtype=np.float32,
                )
                sr_i = int(target_sr)
            except Exception:
                pass
        return arr.astype(np.float32), sr_i, float(len(arr) / max(1, sr_i))
    except Exception:
        return None

try:
    from ble_controller import AsyncWorker, LedController
except ImportError:
    AsyncWorker = None  # type: ignore[misc, assignment]
    LedController = None  # type: ignore[misc, assignment]

try:
    from audio_reactive import ClubAudioAnalyzer
except ImportError:
    ClubAudioAnalyzer = None  # type: ignore[misc, assignment]

try:
    from laser_dmx_app import (  # type: ignore
        CLUB_DOT_POSITION_PRESETS,
        DOT_CH9_DOT_SWATCHES,
        DOT_POINT_MOTIONS,
        LED_FX_FLAME,
        MOTION_SPEED_MAX,
        MOTION_SPEED_MIN,
        _DOT_BASE_CH1,
        _DOT_BASE_CH2,
        _DOT_BASE_CH8,
        _LED_FX_ASSETS,
        _MOTION_SY_OUT_MAX,
        _MOTION_SY_OUT_MIN,
        _pattern_tile_from_led_fx,
        set_motion_sy_range,
    )
except Exception:
    DOT_POINT_MOTIONS = []  # type: ignore[misc, assignment]
    MOTION_SPEED_MIN = 0.25
    MOTION_SPEED_MAX = 48.0
    CLUB_DOT_POSITION_PRESETS = []  # type: ignore[misc, assignment]
    DOT_CH9_DOT_SWATCHES = [  # type: ignore[misc, assignment]
        ("0–19 赤（代表）", 12),
        ("20–39 緑（代表）", 28),
        ("40–59 オレンジ（代表）", 48),
        ("60–79 青（代表）", 68),
        ("80–99 紫（代表）", 90),
        ("100–119 水色（代表）", 110),
        ("120–255 自動・中速目安", 155),
        ("120–255 自動・高速目安", 225),
    ]
    LED_FX_FLAME = -101
    _DOT_BASE_CH1, _DOT_BASE_CH2, _DOT_BASE_CH8 = 100, 128, 63
    _LED_FX_ASSETS = []  # type: ignore[misc, assignment]
    _MOTION_SY_OUT_MIN, _MOTION_SY_OUT_MAX = 0.0, 0.32

    def set_motion_sy_range(lo: float, hi: float) -> tuple[float, float]:  # type: ignore[misc]
        return lo, hi

    def _pattern_tile_from_led_fx(asset: Any, *, rgb: Any = None) -> Any:  # type: ignore[misc]
        raise RuntimeError("LED アセット未対応")

# CH9 代表値 → ボタン表示用の近似色（機材帯域の見た目）
_CH9_SWATCH_RGB: dict[int, tuple[int, int, int]] = {
    12: (255, 48, 48),
    28: (48, 220, 72),
    48: (255, 140, 32),
    68: (48, 96, 255),
    90: (180, 48, 255),
    110: (48, 220, 255),
    155: (210, 210, 230),
    225: (255, 255, 255),
}


def _ch9_swatches_payload() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for label, value in DOT_CH9_DOT_SWATCHES:
        rgb = _CH9_SWATCH_RGB.get(int(value), (160, 160, 160))
        short = str(value)
        if "自動" in str(label) and "高速" in str(label):
            short = "自動高"
        elif "自動" in str(label):
            short = "自動中"
        else:
            for name in ("オレンジ", "水色", "赤", "緑", "青", "紫"):
                if name in str(label):
                    short = name
                    break
        out.append(
            {
                "label": str(label),
                "short": short,
                "value": int(value),
                "r": rgb[0],
                "g": rgb[1],
                "b": rgb[2],
            }
        )
    return out

try:
    from app_settings import load_settings, update_settings
except ImportError:
    def load_settings() -> dict[str, Any]:  # type: ignore[misc]
        return {}

    def update_settings(**kwargs: Any) -> dict[str, Any]:  # type: ignore[misc]
        return kwargs

try:
    from protocol import MODE_LABELS_JA, BuiltInMode
except ImportError:
    MODE_LABELS_JA = {}  # type: ignore[misc, assignment]
    BuiltInMode = []  # type: ignore[misc, assignment]

LED_COLOR_PRESETS: list[tuple[str, tuple[int, int, int]]] = [
    ("赤", (255, 0, 0)),
    ("緑", (0, 255, 0)),
    ("青", (0, 0, 255)),
    ("白", (255, 255, 255)),
    ("暖白", (255, 180, 100)),
    ("紫", (180, 0, 255)),
    ("黄", (255, 220, 0)),
    ("シアン", (0, 220, 255)),
    ("橙", (255, 120, 20)),
    ("ピンク", (255, 80, 160)),
]

# Python UI と同じ AI リアクションモード（日本語ラベル）
AI_REACTION_MODES = ["バランス", "パーティー", "シネマ", "チル"]


def _default_tile() -> dict[str, Any]:
    return {
        "title": "新規タイル",
        "channels": [100, 128, 0, 0, 0, 64, 16, 63, 12, 0],
        "motion_index": 0,
        "motion_speed": 1.35,
        "apply_dot_base": True,
        "club_dot_ch9": 12,
        "run_motion": True,
        "hotkey": None,
        "apply_laser": True,
        "apply_led": False,
        "led_r": 255,
        "led_g": 80,
        "led_b": 40,
        "led_brightness": 100.0,
        "led_mode": None,
        "led_speed": 50,
    }


def _normalize_tile(raw: dict[str, Any]) -> dict[str, Any]:
    base = _default_tile()
    base.update({k: v for k, v in raw.items() if k in base or k == "title"})
    ch = list(base.get("channels") or [0] * 10)
    while len(ch) < 10:
        ch.append(0)
    base["channels"] = [max(0, min(255, int(v))) for v in ch[:10]]
    base["title"] = str(base.get("title") or "タイル")[:80]
    base["motion_index"] = int(base.get("motion_index") or 0)
    base["motion_speed"] = float(base.get("motion_speed") or 1.35)
    base["apply_dot_base"] = bool(base.get("apply_dot_base", True))
    base["club_dot_ch9"] = max(0, min(255, int(base.get("club_dot_ch9") or 0)))
    base["run_motion"] = bool(base.get("run_motion", True))
    hk = base.get("hotkey")
    base["hotkey"] = None if hk in (None, "", "null") else str(hk)[:16]
    base["apply_laser"] = bool(base.get("apply_laser", True))
    base["apply_led"] = bool(base.get("apply_led", False))
    base["led_r"] = max(0, min(255, int(base.get("led_r", 255))))
    base["led_g"] = max(0, min(255, int(base.get("led_g", 80))))
    base["led_b"] = max(0, min(255, int(base.get("led_b", 40))))
    base["led_brightness"] = float(base.get("led_brightness", 100.0))
    lm = base.get("led_mode")
    if lm in (None, "", "null"):
        base["led_mode"] = None
    else:
        base["led_mode"] = int(lm)
    base["led_speed"] = max(0, min(100, int(base.get("led_speed", 50))))
    return base

TILES_PATH = DMX_DIR / "pattern_tiles.json"
DATA_DIR = ROOT / "server" / "data"
UPLOAD_DIR = DATA_DIR / "uploads"
PROJECT_DIR = DATA_DIR / "projects"


@dataclass
class PunchCue:
    t: float
    hold_sec: float
    recorded_at: str
    cue_id: str
    tile: dict[str, Any]

    def label(self) -> str:
        title = str(self.tile.get("title", "?"))[:40]
        return f"{self.t:7.2f}s ▬{self.hold_sec:5.2f}s  {title}"


@dataclass
class LightController:
    """Singleton-ish app state for the Web backend."""

    _listeners: list[Callable[[dict[str, Any]], None]] = field(default_factory=list)
    _lock: threading.RLock = field(default_factory=threading.RLock)

    # DMX
    base_addr: int = 1
    channels: list[int] = field(default_factory=lambda: [0] * 10)
    dmx_port: str | None = None
    _sender: Any = None
    motion_index: int = 0
    motion_speed: float = 1.35
    _motion_fn: Any = None
    _motion_phase: float = 0.0
    _motion_stop: threading.Event = field(default_factory=threading.Event)
    _motion_thread: threading.Thread | None = None

    # Tiles / punch
    tiles: list[dict[str, Any]] = field(default_factory=list)
    holds: dict[str, dict[str, Any]] = field(default_factory=dict)  # source -> tile
    hold_order: list[str] = field(default_factory=list)
    cues: list[PunchCue] = field(default_factory=list)
    punch_record: bool = False
    punch_autoplay: bool = True
    _punch_pending: dict[str, dict[str, Any]] = field(default_factory=dict)
    _punch_play_idx: int = 0

    # DMX motion extras (Python UI パレット相当)
    apply_dot_base: bool = True
    club_dot_ch9: int = 12
    sy_lo_pct: float = 0.0
    sy_hi_pct: float = 32.0
    _cue_hold_until: dict[str, float] = field(default_factory=dict)

    # Timeline audio
    audio_path: str | None = None
    audio_mono: Any = None
    audio_sr: int = 44100
    audio_duration: float = 0.0
    volume: float = 0.8
    playing: bool = False
    play_sample: int = 0
    zoom: float = 1.0
    view_start: float = 0.0
    wave_peaks: list[float] | None = None
    _stream: Any = None
    _transport_busy: bool = False
    _transport_gen: int = 0
    _tick_stop: threading.Event = field(default_factory=threading.Event)
    _tick_thread: threading.Thread | None = None

    # BLE
    _ble_worker: Any = None
    _ble: Any = None
    ble_connected: bool = False
    ble_address: str = ""
    ble_name: str = ""
    ble_devices: list[dict[str, Any]] = field(default_factory=list)
    ble_auto_connect: bool = False
    ble_protocol: str = "triones"
    dmx_auto_connect: bool = False
    last_error: str = ""

    # AI
    _analyzer: Any = None
    ai_active: bool = False
    ai_status: str = "停止中"
    ai_sensitivity: float = 1.4
    ai_mode: str = "バランス"
    ai_device_id: int | None = None
    ai_device_label: str = ""
    ai_devices: list[dict[str, Any]] = field(default_factory=list)
    _ai_ble_last: float = 0.0

    def __post_init__(self) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        PROJECT_DIR.mkdir(parents=True, exist_ok=True)
        self.reload_tiles()
        if LedController is not None and AsyncWorker is not None:
            self._ble = LedController()
            self._ble_worker = AsyncWorker()
            threading.Thread(target=self._ble_worker.start_in_thread, daemon=True).start()
            self._ble_worker.wait_ready()
        self._load_boot_settings()
        self.refresh_audio_devices()
        try:
            self.sy_lo_pct = round(float(_MOTION_SY_OUT_MIN) * 100.0, 1)
            self.sy_hi_pct = round(float(_MOTION_SY_OUT_MAX) * 100.0, 1)
        except Exception:
            pass
        # 起動直後の自動接続（UI 起動を待たず）
        threading.Thread(target=self._boot_auto_connect, daemon=True).start()

    def _load_boot_settings(self) -> None:
        try:
            s = load_settings()
        except Exception:
            s = {}
        self.dmx_auto_connect = bool(s.get("dmx_auto_connect", False))
        self.ble_auto_connect = bool(s.get("ble_auto_connect", False))
        self.ble_address = str(s.get("ble_address", "") or "").strip()
        self.ble_protocol = str(s.get("ble_protocol", "triones") or "triones")
        try:
            self.base_addr = max(1, int(s.get("dmx_base_addr", 1) or 1))
        except (TypeError, ValueError):
            self.base_addr = 1
        saved_com = str(s.get("dmx_com_port", "") or "").strip()
        if saved_com:
            self.dmx_port = saved_com  # 未接続でも選択値として保持
        self.ai_device_label = str(s.get("audio_input_label", "") or "")
        try:
            aid = int(s.get("audio_input_id", -1))
            self.ai_device_id = None if aid < 0 else aid
        except (TypeError, ValueError):
            self.ai_device_id = None

    def _boot_auto_connect(self) -> None:
        time.sleep(0.8)
        if self.dmx_auto_connect:
            port = (self.dmx_port or "").strip()
            if port:
                try:
                    self.dmx_connect(port, self.base_addr)
                except Exception as e:
                    self.last_error = f"DMX自動接続失敗: {e}"
                    self._emit("error")
        if self.ble_auto_connect and self.ble_address:
            try:
                self.ble_connect(self.ble_address)
            except Exception as e:
                self.last_error = f"BLE自動接続失敗: {e}"
                self._emit("error")

    # ---- events ----
    def add_listener(self, cb: Callable[[dict[str, Any]], None]) -> None:
        self._listeners.append(cb)

    def remove_listener(self, cb: Callable[[dict[str, Any]], None]) -> None:
        if cb in self._listeners:
            self._listeners.remove(cb)

    def _emit(self, event: str, **payload: Any) -> None:
        msg = {"event": event, **payload, "state": self.snapshot()}
        for cb in list(self._listeners):
            try:
                cb(msg)
            except Exception:
                pass

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            ble_modes = []
            try:
                for m in BuiltInMode:
                    ble_modes.append({"id": int(m), "label": MODE_LABELS_JA.get(m, str(m))})
            except Exception:
                ble_modes = []
            return {
                "dmx": {
                    "port": self.dmx_port,
                    "connected": bool(self._sender is not None and self._sender.is_alive()),
                    "base_addr": self.base_addr,
                    "channels": list(self.channels),
                    "motion_index": self.motion_index,
                    "motion_speed": self.motion_speed,
                    "motion_running": self._motion_fn is not None,
                    "motions": [n for n, _ in DOT_POINT_MOTIONS],
                    "ports": [{"id": a, "label": b} for a, b in list_com_ports()],
                    "auto_connect": self.dmx_auto_connect,
                    "apply_dot_base": self.apply_dot_base,
                    "club_dot_ch9": self.club_dot_ch9,
                    "sy_lo_pct": self.sy_lo_pct,
                    "sy_hi_pct": self.sy_hi_pct,
                    "dot_positions": [
                        {"name": n, "ch6": a, "ch7": b}
                        for n, a, b in CLUB_DOT_POSITION_PRESETS
                    ],
                    "ch9_swatches": _ch9_swatches_payload(),
                },
                "ble": {
                    "connected": self.ble_connected,
                    "address": self.ble_address,
                    "name": self.ble_name,
                    "devices": list(self.ble_devices),
                    "auto_connect": self.ble_auto_connect,
                    "protocol": self.ble_protocol,
                    "modes": ble_modes,
                    "color_presets": [
                        {"name": n, "r": rgb[0], "g": rgb[1], "b": rgb[2]}
                        for n, rgb in LED_COLOR_PRESETS
                    ],
                    "custom_fx": [{"id": LED_FX_FLAME, "label": "炎（ぱっと点灯）"}],
                },
                "ai": {
                    "active": self.ai_active,
                    "status": self.ai_status,
                    "sensitivity": self.ai_sensitivity,
                    "mode": self.ai_mode,
                    "modes": list(AI_REACTION_MODES),
                    "device_id": self.ai_device_id,
                    "device_label": self.ai_device_label,
                    "devices": list(self.ai_devices),
                },
                "timeline": {
                    "path": self.audio_path,
                    "name": Path(self.audio_path).name if self.audio_path else "",
                    "duration": self.audio_duration,
                    "playing": self.playing,
                    "position": self._elapsed(),
                    "volume": self.volume,
                    "zoom": self.zoom,
                    "view_start": self.view_start,
                    "peaks": self.wave_peaks,
                    "record": self.punch_record,
                    "autoplay": self.punch_autoplay,
                },
                "tiles": list(self.tiles),
                "holds": list(self.hold_order),
                "cues": [
                    {
                        "t": c.t,
                        "hold_sec": c.hold_sec,
                        "cue_id": c.cue_id,
                        "title": c.tile.get("title", "?"),
                        "tile": c.tile,
                        "label": c.label(),
                    }
                    for c in self.cues
                ],
                "last_error": self.last_error,
            }

    # ---- tiles ----
    def reload_tiles(self) -> None:
        if not TILES_PATH.is_file():
            self.tiles = []
            return
        try:
            raw = json.loads(TILES_PATH.read_text(encoding="utf-8"))
            self.tiles = [_normalize_tile(t) for t in raw if isinstance(t, dict)]
        except (OSError, json.JSONDecodeError):
            self.tiles = []

    def save_tiles(self) -> None:
        TILES_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = [_normalize_tile(t) for t in self.tiles]
        TILES_PATH.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        self._emit("tiles")

    def upsert_tile(self, index: int | None, tile: dict[str, Any]) -> int:
        norm = _normalize_tile(tile)
        if index is None or index < 0 or index >= len(self.tiles):
            self.tiles.append(norm)
            idx = len(self.tiles) - 1
        else:
            self.tiles[index] = norm
            idx = index
        self.save_tiles()
        return idx

    def delete_tile(self, index: int) -> None:
        if not (0 <= index < len(self.tiles)):
            raise RuntimeError("タイル番号が不正です")
        self.tiles.pop(index)
        self.save_tiles()

    def capture_tile(
        self,
        title: str | None = None,
        *,
        apply_laser: bool = True,
        apply_led: bool = False,
    ) -> int:
        name = (title or f"スロット {len(self.tiles) + 1}").strip()[:80]
        tile = _normalize_tile(
            {
                "title": name,
                "channels": list(self.channels),
                "motion_index": self.motion_index,
                "motion_speed": self.motion_speed,
                "apply_dot_base": self.apply_dot_base,
                "club_dot_ch9": self.club_dot_ch9,
                "run_motion": self._motion_fn is not None,
                "apply_laser": apply_laser,
                "apply_led": apply_led,
                "led_r": 255,
                "led_g": 80,
                "led_b": 40,
                "led_brightness": 100.0,
                "led_mode": None,
                "led_speed": 50,
            }
        )
        self.tiles.append(tile)
        self.save_tiles()
        return len(self.tiles) - 1

    def install_led_fx_pack(self) -> int:
        existing = {str(t.get("title")) for t in self.tiles}
        added = 0
        for asset in _LED_FX_ASSETS:
            title = getattr(asset, "title", None) or str(asset)
            if title in existing:
                continue
            try:
                pt = _pattern_tile_from_led_fx(asset)
                if hasattr(pt, "__dict__"):
                    raw = {
                        "title": pt.title,
                        "channels": list(pt.channels),
                        "motion_index": pt.motion_index,
                        "motion_speed": pt.motion_speed,
                        "apply_dot_base": pt.apply_dot_base,
                        "club_dot_ch9": pt.club_dot_ch9,
                        "run_motion": pt.run_motion,
                        "hotkey": pt.hotkey,
                        "apply_laser": pt.apply_laser,
                        "apply_led": pt.apply_led,
                        "led_r": pt.led_r,
                        "led_g": pt.led_g,
                        "led_b": pt.led_b,
                        "led_brightness": pt.led_brightness,
                        "led_mode": pt.led_mode,
                        "led_speed": pt.led_speed,
                    }
                else:
                    continue
                self.tiles.append(_normalize_tile(raw))
                existing.add(title)
                added += 1
            except Exception:
                continue
        if added:
            self.save_tiles()
        else:
            self._emit("tiles")
        return added

    def _tile_by_index(self, index: int) -> dict[str, Any] | None:
        if 0 <= index < len(self.tiles):
            return self.tiles[index]
        return None

    def hold_add(self, source: str, tile: dict[str, Any], *, record: bool = True) -> None:
        with self._lock:
            self.holds[source] = tile
            if source in self.hold_order:
                self.hold_order.remove(source)
            self.hold_order.append(source)
            if record and self.punch_record and self.playing and not source.startswith("cue:"):
                if source not in self._punch_pending:
                    self._punch_pending[source] = {
                        "t": self._elapsed(),
                        "mono": time.monotonic(),
                        "tile": dict(tile),
                        "cue_id": f"{time.time_ns()}-{source}",
                    }
            self._sync_holds()
        self._emit("hold")

    def hold_remove(self, source: str) -> None:
        with self._lock:
            pend = self._punch_pending.pop(source, None)
            if pend is not None:
                hold = max(0.02, time.monotonic() - float(pend["mono"]))
                cue = PunchCue(
                    t=float(pend["t"]),
                    hold_sec=hold,
                    recorded_at=datetime.now().isoformat(timespec="milliseconds"),
                    cue_id=str(pend["cue_id"]),
                    tile=dict(pend["tile"]),
                )
                self.cues.append(cue)
                self.cues.sort(key=lambda c: (c.t, c.cue_id))
            if source in self.holds:
                del self.holds[source]
            if source in self.hold_order:
                self.hold_order.remove(source)
            self._cue_hold_until.pop(source, None)
            self._sync_holds()
        self._emit("hold")

    def _sync_holds(self) -> None:
        if not self.holds:
            self.stop_motion()
            self.set_channels([0] * 10, push=True)
            self._ble_override(None)
            return
        order = [s for s in self.hold_order if s in self.holds]
        tiles = [self.holds[s] for s in order]
        laser = next((t for t in reversed(tiles) if t.get("apply_laser", True)), None)
        led = next((t for t in reversed(tiles) if t.get("apply_led", False)), None)
        if led is not None:
            self._ble_override(
                {
                    "r": int(led.get("led_r", 255)),
                    "g": int(led.get("led_g", 80)),
                    "b": int(led.get("led_b", 40)),
                    "brightness": float(led.get("led_brightness", 100.0)) / 100.0,
                    "mode": led.get("led_mode"),
                    "speed": int(led.get("led_speed", 50)),
                }
            )
        else:
            self._ble_override(None)
        if laser is not None:
            self._apply_laser_tile(laser)
        else:
            self.stop_motion()
            self.set_channels([0] * 10, push=True)

    def _apply_laser_tile(self, tile: dict[str, Any]) -> None:
        ch = tile.get("channels") or [0] * 10
        if len(ch) != 10:
            return
        vals = [max(0, min(255, int(v))) for v in ch]
        if len(vals) == 10:
            vals[8] = max(0, min(255, int(tile.get("club_dot_ch9", vals[8]))))
        self.set_channels(vals, push=True)
        sp = float(tile.get("motion_speed", 1.35))
        self.motion_speed = max(MOTION_SPEED_MIN, min(MOTION_SPEED_MAX, sp))
        mi = int(tile.get("motion_index", 0))
        if tile.get("run_motion") and DOT_POINT_MOTIONS:
            mi = max(0, min(len(DOT_POINT_MOTIONS) - 1, mi))
            self.start_motion(mi)
        else:
            self.stop_motion()

    def _ble_override(self, payload: dict[str, Any] | None) -> None:
        if self._ble is None or self._ble_worker is None or not self.ble_connected:
            return
        try:
            if payload is None:
                self._ble_worker.submit(self._ble.set_power(False))
                return
            mode = payload.get("mode")
            if mode is not None and mode != "":
                mid = int(mode)
                self._ble_worker.submit(self._ble.set_power(True))
                if mid < 0:
                    # ソフト炎など: 機材モードではなく色フラッシュ
                    self._ble_worker.submit(
                        self._ble.set_color(
                            int(payload.get("r", 255)),
                            int(payload.get("g", 120)),
                            int(payload.get("b", 20)),
                            float(payload.get("brightness", 1.0)),
                        )
                    )
                else:
                    self._ble_worker.submit(
                        self._ble.set_mode(mid, int(payload.get("speed", 50)))
                    )
            else:
                self._ble_worker.submit(self._ble.set_power(True))
                self._ble_worker.submit(
                    self._ble.set_color(
                        int(payload["r"]),
                        int(payload["g"]),
                        int(payload["b"]),
                        float(payload.get("brightness", 1.0)),
                    )
                )
        except Exception:
            pass

    # ---- DMX ----
    def list_ports(self) -> list[dict[str, str]]:
        return [{"id": a, "label": b} for a, b in list_com_ports()]

    def dmx_connect(self, port: str, base_addr: int = 1) -> None:
        port = (port or "").strip()
        if not port:
            raise RuntimeError("COM ポートを選択してください")
        self.last_error = ""
        self.dmx_disconnect()
        try:
            ser = OpenDMXSender.try_open(port)
        except Exception as e:
            self.last_error = f"DMX接続失敗: {e}"
            self._emit("error")
            raise RuntimeError(f"COM を開けません ({port}): {e}") from e
        self._sender = OpenDMXSender(ser)
        self._sender.start()
        self.dmx_port = port
        self.base_addr = max(1, min(DMX_CHANNELS - 9, int(base_addr)))
        self._push_dmx()
        try:
            update_settings(dmx_com_port=port, dmx_base_addr=self.base_addr)
        except Exception:
            pass
        self._emit("dmx")

    def set_dmx_auto_connect(self, enabled: bool) -> None:
        self.dmx_auto_connect = bool(enabled)
        try:
            update_settings(dmx_auto_connect=self.dmx_auto_connect)
        except Exception:
            pass
        self._emit("dmx")

    def dmx_disconnect(self) -> None:
        self.stop_motion()
        if self._sender is not None:
            try:
                self._sender.stop()
            except Exception:
                pass
            self._sender = None
        # COM 選択値は保持（UI / 自動再接続用）
        self._emit("dmx")

    def set_channels(self, values: list[int], *, push: bool = True) -> None:
        if len(values) != 10:
            return
        self.channels = [max(0, min(255, int(v))) for v in values]
        if push:
            self._push_dmx()

    def _push_dmx(self) -> None:
        if self._sender is None:
            return
        try:
            self._sender.set_channels(self.base_addr, self.channels)
        except Exception:
            pass

    def start_motion(self, index: int | None = None) -> None:
        if not DOT_POINT_MOTIONS:
            return
        if index is not None:
            self.motion_index = max(0, min(len(DOT_POINT_MOTIONS) - 1, int(index)))
        if self.apply_dot_base:
            self._apply_dot_look_channels()
        self._motion_fn = DOT_POINT_MOTIONS[self.motion_index][1]
        self._motion_phase = 0.0
        self._motion_stop.clear()
        if self._motion_thread is None or not self._motion_thread.is_alive():
            self._motion_thread = threading.Thread(target=self._motion_loop, daemon=True)
            self._motion_thread.start()
        self._emit("dmx")

    def stop_motion(self) -> None:
        self._motion_stop.set()
        self._motion_fn = None
        self._emit("dmx")

    def set_apply_dot_base(self, enabled: bool) -> None:
        self.apply_dot_base = bool(enabled)
        if self.apply_dot_base and self._motion_fn is not None:
            self._apply_dot_look_channels()
        self._emit("dmx")

    def set_club_dot_ch9(self, value: int) -> None:
        self.club_dot_ch9 = max(0, min(255, int(value)))
        ch = list(self.channels)
        ch[8] = self.club_dot_ch9
        self.set_channels(ch, push=True)
        self._emit("dmx")

    def set_height_range(self, lo_pct: float, hi_pct: float) -> None:
        lo = max(0.0, min(100.0, float(lo_pct))) / 100.0
        hi = max(0.0, min(100.0, float(hi_pct))) / 100.0
        lo, hi = set_motion_sy_range(lo, hi)
        self.sy_lo_pct = round(lo * 100.0, 1)
        self.sy_hi_pct = round(hi * 100.0, 1)
        self._emit("dmx")

    def apply_dot_position(self, index: int) -> None:
        if not CLUB_DOT_POSITION_PRESETS:
            raise RuntimeError("位置プリセットがありません")
        i = max(0, min(len(CLUB_DOT_POSITION_PRESETS) - 1, int(index)))
        _name, ch6, ch7 = CLUB_DOT_POSITION_PRESETS[i]
        ch = list(self.channels)
        if self.apply_dot_base:
            ch[0] = _DOT_BASE_CH1
            ch[1] = _DOT_BASE_CH2
            ch[7] = _DOT_BASE_CH8
        ch[5] = max(0, min(127, int(ch6)))
        ch[6] = max(0, min(127, int(ch7)))
        ch[8] = self.club_dot_ch9
        self.set_channels(ch, push=True)
        self._emit("dmx")

    def _apply_dot_look_channels(self) -> None:
        ch = list(self.channels)
        ch[0] = _DOT_BASE_CH1
        ch[1] = _DOT_BASE_CH2
        ch[7] = _DOT_BASE_CH8
        self.set_channels(ch, push=True)

    def _motion_loop(self) -> None:
        while not self._motion_stop.is_set():
            fn = self._motion_fn
            if fn is None:
                time.sleep(0.05)
                continue
            try:
                sp = max(MOTION_SPEED_MIN, min(MOTION_SPEED_MAX, float(self.motion_speed)))
                self._motion_phase += 0.036 * sp
                step = fn(self._motion_phase)
                ch = list(self.channels)
                if isinstance(step, (tuple, list)) and len(step) >= 3:
                    # (ch6, ch7, ch9, ch8, ch2, ch4, ch1, ch3)
                    ch6 = step[0]
                    ch7 = step[1]
                    ch9 = step[2] if len(step) > 2 else None
                    ch8 = step[3] if len(step) > 3 else None
                    ch2 = step[4] if len(step) > 4 else None
                    ch4 = step[5] if len(step) > 5 else None
                    ch1 = step[6] if len(step) > 6 else None
                    ch3 = step[7] if len(step) > 7 else None
                    if ch6 is not None:
                        ch[5] = max(0, min(255, int(ch6)))
                    if ch7 is not None:
                        ch[6] = max(0, min(255, int(ch7)))
                    if ch9 is not None:
                        ch[8] = max(0, min(255, int(ch9)))
                    if ch8 is not None:
                        ch[7] = max(0, min(255, int(ch8)))
                    if ch2 is not None:
                        ch[1] = max(0, min(255, int(ch2)))
                    if ch4 is not None:
                        ch[3] = max(0, min(255, int(ch4)))
                    if ch1 is not None:
                        ch[0] = max(0, min(255, int(ch1)))
                    if ch3 is not None:
                        ch[2] = max(0, min(255, int(ch3)))
                    self.set_channels(ch, push=True)
            except Exception:
                pass
            time.sleep(0.036)

    # ---- BLE ----
    def ble_scan(self, timeout: float = 8.0) -> list[dict[str, Any]]:
        if self._ble is None or self._ble_worker is None:
            raise RuntimeError("BLE モジュールがありません")
        fut = self._ble_worker.submit(self._ble.scan(timeout=timeout, led_only=True))
        result = fut.result(timeout=timeout + 5)
        self.ble_devices = [
            {"name": d.name, "address": d.address, "rssi": d.rssi, "label": d.label}
            for d in result.devices
        ]
        self._emit("ble")
        return self.ble_devices

    def ble_connect(self, address: str) -> None:
        address = (address or "").strip()
        if not address:
            raise RuntimeError("BLE アドレスを入力してください")
        if self._ble is None or self._ble_worker is None:
            raise RuntimeError("BLE モジュールがありません")
        self.last_error = ""

        def _on_disc() -> None:
            self.ble_connected = False
            self._emit("ble")

        try:
            fut = self._ble_worker.submit(
                self._ble.connect_address(address, on_disconnect=_on_disc)
            )
            fut.result(timeout=25)
        except Exception as e:
            self.last_error = f"BLE接続失敗: {e}"
            self._emit("error")
            raise RuntimeError(str(e)) from e
        self.ble_connected = True
        self.ble_address = self._ble.address or address
        self.ble_name = self._ble.device_name or ""
        self.ble_protocol = getattr(self._ble, "protocol", self.ble_protocol) or "triones"
        try:
            self._ble_worker.submit(self._ble.set_power(True)).result(timeout=5)
        except Exception:
            pass
        try:
            update_settings(ble_address=self.ble_address, ble_protocol=self.ble_protocol)
        except Exception:
            pass
        self._emit("ble")

    def set_ble_auto_connect(self, enabled: bool) -> None:
        self.ble_auto_connect = bool(enabled)
        try:
            update_settings(
                ble_auto_connect=self.ble_auto_connect,
                ble_address=self.ble_address,
            )
        except Exception:
            pass
        self._emit("ble")

    def ble_power(self, on: bool) -> None:
        if self._ble is None or self._ble_worker is None or not self.ble_connected:
            raise RuntimeError("BLE 未接続")
        self._ble_worker.submit(self._ble.set_power(bool(on))).result(timeout=5)

    def ble_mode(self, mode_id: int, speed: int = 50) -> None:
        if self._ble is None or self._ble_worker is None or not self.ble_connected:
            raise RuntimeError("BLE 未接続")
        self._ble_worker.submit(self._ble.set_power(True))
        self._ble_worker.submit(self._ble.set_mode(int(mode_id), int(speed))).result(timeout=5)

    def refresh_audio_devices(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        try:
            if ClubAudioAnalyzer is not None:
                for idx, label in ClubAudioAnalyzer.list_input_devices():
                    dev_id = None if idx < 0 else int(idx)
                    rows.append({"id": dev_id, "label": label})
        except Exception as e:
            rows = [{"id": None, "label": f"default: システム既定（取得失敗: {e}）"}]
        self.ai_devices = rows
        labels = [r["label"] for r in rows]
        if self.ai_device_label and self.ai_device_label in labels:
            for r in rows:
                if r["label"] == self.ai_device_label:
                    self.ai_device_id = r["id"]
                    break
        elif rows:
            # 保存 ID 優先
            if self.ai_device_id is not None:
                for r in rows:
                    if r["id"] == self.ai_device_id:
                        self.ai_device_label = r["label"]
                        break
            if not self.ai_device_label or self.ai_device_label not in labels:
                self.ai_device_label = rows[0]["label"]
                self.ai_device_id = rows[0]["id"]
        return rows

    def set_ai_device(self, device_id: int | None, label: str) -> None:
        self.ai_device_id = device_id
        self.ai_device_label = label
        try:
            update_settings(
                audio_input_id=-1 if device_id is None else int(device_id),
                audio_input_label=label,
            )
        except Exception:
            pass
        self._emit("ai")

    def set_ai_mode(self, mode: str) -> None:
        self.ai_mode = str(mode)
        if self._analyzer is not None and hasattr(self._analyzer, "set_reaction_mode"):
            try:
                self._analyzer.set_reaction_mode(self.ai_mode)
            except Exception:
                pass
        self._emit("ai")

    def ble_disconnect(self) -> None:
        if self._ble is None or self._ble_worker is None:
            return
        try:
            self._ble_worker.submit(self._ble.disconnect()).result(timeout=5)
        except Exception:
            pass
        self.ble_connected = False
        self._emit("ble")

    def ble_color(self, r: int, g: int, b: int, brightness: float = 1.0) -> None:
        if self._ble is None or self._ble_worker is None or not self.ble_connected:
            raise RuntimeError("BLE 未接続")
        self._ble_worker.submit(self._ble.set_power(True))
        self._ble_worker.submit(self._ble.set_color(r, g, b, brightness)).result(timeout=5)

    def _on_ai_lighting_frame(self, frame: Any) -> None:
        if not self.ai_active:
            return
        # ポン出しホールド中は LED を触らない
        if self.hold_order:
            return
        if not self.ble_connected or self._ble is None or self._ble_worker is None:
            return
        now = time.monotonic()
        if now - self._ai_ble_last < 0.045:
            return
        self._ai_ble_last = now
        try:
            bright = max(0.01, min(1.0, float(getattr(frame, "brightness", 1.0))))
            if getattr(frame, "beat", False) and getattr(frame, "style", "") == "impact":
                bright = min(1.0, bright + 0.12)
            if getattr(frame, "effect", "") == "accel_blink":
                bright = 1.0 if bright >= 0.35 or getattr(frame, "beat", False) else 0.03
            self._ble_worker.submit(
                self._ble.set_color(
                    int(getattr(frame, "r", 0)),
                    int(getattr(frame, "g", 0)),
                    int(getattr(frame, "b", 0)),
                    bright,
                )
            )
        except Exception:
            pass

    # ---- AI ----
    def ai_start(self, sensitivity: float = 1.4, mode: str | None = None) -> None:
        if ClubAudioAnalyzer is None:
            raise RuntimeError("音声解析モジュールがありません")
        self.ai_stop()
        self.ai_sensitivity = float(sensitivity)
        if mode:
            self.ai_mode = str(mode)
        self.refresh_audio_devices()
        # ラベルから ID を再解決
        for r in self.ai_devices:
            if r.get("label") == self.ai_device_label:
                self.ai_device_id = r.get("id")
                break
        try:
            an = ClubAudioAnalyzer(
                device=self.ai_device_id,
                on_lighting_frame=self._on_ai_lighting_frame,
            )
            an.gate_sensitivity = self.ai_sensitivity
            if hasattr(an, "set_reaction_mode"):
                an.set_reaction_mode(self.ai_mode)
            an.start()
        except Exception as e:
            self.last_error = f"AI開始失敗: {e}"
            self.ai_status = f"開始失敗: {e}"
            self._emit("error")
            raise RuntimeError(str(e)) from e
        self._analyzer = an
        self.ai_active = True
        self.ai_status = f"動作中（{self.ai_device_label or '既定'}）— LED 連動"
        # レーザー側: AI 中はモーションを回す（未開始なら）
        if self._sender is not None and self._sender.is_alive() and self._motion_fn is None:
            try:
                self.start_motion(self.motion_index)
            except Exception:
                pass
        self._emit("ai")

    def ai_stop(self) -> None:
        if self._analyzer is not None:
            try:
                self._analyzer.stop()
            except Exception:
                pass
            self._analyzer = None
        self.ai_active = False
        self.ai_status = "停止中"
        self._emit("ai")

    # ---- timeline ----
    def _elapsed(self) -> float:
        """現在の再生位置（秒）。停止中も play_sample を返す（シーク後に先頭へ戻さない）。"""
        if self.audio_sr <= 0:
            return 0.0
        return max(0.0, float(self.play_sample) / float(self.audio_sr))

    def _compute_peaks(self, columns: int = 600) -> None:
        if self.audio_mono is None:
            self.wave_peaks = None
            return
        y = np.asarray(self.audio_mono, dtype=np.float32)
        n = int(y.size)
        if n < 2:
            self.wave_peaks = None
            return
        cols = max(64, min(int(columns), 2000))
        step = max(1, n // cols)
        peaks: list[float] = []
        for i in range(cols):
            a = i * step
            b = min(n, a + step)
            seg = y[a:b]
            peaks.append(float(np.max(np.abs(seg))) if seg.size else 0.0)
        mx = max(peaks) or 1.0
        self.wave_peaks = [p / mx for p in peaks]

    def load_audio_file(self, path: str | Path) -> dict[str, Any]:
        path = Path(path)
        loaded = load_audio_for_playback(str(path), target_sr=44100)
        if loaded is None:
            raise RuntimeError(
                f"音声を読めません: {path}\n"
                "pip install soundfile を試すか、WAV を指定してください。"
            )
        y, sr, dur = loaded
        self.stop_playback()
        self.audio_path = str(path)
        self.audio_mono = y
        self.audio_sr = int(sr)
        self.audio_duration = float(dur)
        self.play_sample = 0
        self.zoom = 1.0
        self.view_start = 0.0
        self._compute_peaks()
        self._emit("timeline")
        return {"path": self.audio_path, "duration": self.audio_duration, "sr": self.audio_sr}

    def set_volume(self, v: float) -> None:
        self.volume = max(0.0, min(1.0, float(v)))

    def seek(self, t: float) -> None:
        dur = self.audio_duration
        t = max(0.0, min(dur, float(t)))
        if self.playing:
            self._start_stream_at(t)
            self._resync_cues(t)
        else:
            self.play_sample = int(t * self.audio_sr)
        self._emit("timeline")

    def toggle_playback(self) -> None:
        if self.playing:
            self.stop_playback()
        else:
            self.start_playback()

    def start_playback(self, t: float | None = None) -> None:
        if self._transport_busy:
            return
        if self.audio_mono is None or sd is None:
            raise RuntimeError("音声未読込、または sounddevice がありません")
        if t is None:
            t = self._elapsed()
        dur = self.audio_duration
        if dur > 0 and t >= dur - 0.05:
            t = 0.0
        self._transport_busy = True
        try:
            self.playing = True
            self._start_stream_at(float(t))
            self._resync_cues(float(t))
            self._start_tick()
            self._emit("timeline")
        finally:
            self._transport_busy = False

    def stop_playback(self) -> None:
        if self._transport_busy:
            return
        if not self.playing and self._stream is None:
            return
        self._transport_busy = True
        try:
            self._transport_gen += 1
            self.playing = False
            self._tick_stop.set()
            self._stop_stream()
            # release auto cues
            for src in [s for s in list(self.holds) if s.startswith("cue:")]:
                self.hold_remove(src)
            self._cue_hold_until.clear()
            for src in list(self._punch_pending.keys()):
                self.hold_remove(src)
            self._emit("timeline")
        finally:
            self._transport_busy = False

    def _stop_stream(self) -> None:
        stream = self._stream
        self._stream = None
        if stream is None:
            return
        try:
            stream.abort()
        except Exception:
            try:
                stream.stop()
            except Exception:
                pass
        try:
            stream.close()
        except Exception:
            pass

    def _start_stream_at(self, t_sec: float) -> None:
        if sd is None or self.audio_mono is None:
            raise RuntimeError("再生不可")
        n = int(getattr(self.audio_mono, "size", 0))
        sr = int(self.audio_sr)
        t = max(0.0, min(self.audio_duration, float(t_sec)))
        start = max(0, min(max(0, n - 1), int(t * sr)))
        self._transport_gen += 1
        self._stop_stream()
        self.play_sample = start
        gen = self._transport_gen

        def callback(outdata, frames, _time_info, _status):  # type: ignore[no-untyped-def]
            if not self.playing:
                outdata.fill(0)
                raise sd.CallbackAbort  # type: ignore[misc]
            audio = self.audio_mono
            pos = int(self.play_sample)
            nn = int(getattr(audio, "size", 0))
            if pos >= nn:
                outdata.fill(0)
                raise sd.CallbackStop  # type: ignore[misc]
            end = min(nn, pos + int(frames))
            chunk = np.asarray(audio[pos:end], dtype=np.float32)
            gain = float(self.volume)
            if gain < 0.999:
                chunk = chunk * gain
            outdata.fill(0.0)
            length = int(chunk.size)
            if length > 0:
                if outdata.ndim == 1:
                    outdata[:length] = chunk
                else:
                    outdata[:length, 0] = chunk
                    if outdata.shape[1] > 1:
                        outdata[:length, 1] = chunk
            self.play_sample = end
            if end >= nn:
                raise sd.CallbackStop  # type: ignore[misc]

        def finished() -> None:
            def _done() -> None:
                if gen != self._transport_gen:
                    return
                if self.playing:
                    self.play_sample = int(self.audio_duration * self.audio_sr)
                    self.stop_playback()

            threading.Thread(target=_done, daemon=True).start()

        stream = sd.OutputStream(
            samplerate=sr,
            channels=1,
            dtype="float32",
            callback=callback,
            finished_callback=finished,
        )
        self._stream = stream
        stream.start()

    def _start_tick(self) -> None:
        self._tick_stop.clear()
        if self._tick_thread is not None and self._tick_thread.is_alive():
            return

        def loop() -> None:
            while not self._tick_stop.is_set() and self.playing:
                elapsed = self._elapsed()
                if self.audio_duration > 0 and elapsed >= self.audio_duration - 0.02:
                    self.stop_playback()
                    break
                self._drain_cues(elapsed)
                self._release_expired_cues(elapsed)
                msg = {"event": "tick", "position": elapsed}
                for cb in list(self._listeners):
                    try:
                        cb(msg)
                    except Exception:
                        pass
                time.sleep(0.05)

        self._tick_thread = threading.Thread(target=loop, daemon=True)
        self._tick_thread.start()

    def _resync_cues(self, t_sec: float) -> None:
        for src in [s for s in list(self.holds) if s.startswith("cue:")]:
            self.hold_remove(src)
        self._cue_hold_until.clear()
        play_idx = 0
        for i, c in enumerate(self.cues):
            if c.t > t_sec + 0.0005:
                play_idx = i
                break
            end = float(c.t) + max(0.02, float(c.hold_sec))
            if self.punch_autoplay and end > t_sec + 0.0005:
                self._fire_cue(c, hold_until=end)
            play_idx = i + 1
        else:
            play_idx = len(self.cues)
        self._punch_play_idx = play_idx

    def _drain_cues(self, elapsed: float) -> None:
        if not self.punch_autoplay:
            return
        i = self._punch_play_idx
        while i < len(self.cues):
            c = self.cues[i]
            if c.t > elapsed + 0.0005:
                break
            self._fire_cue(c)
            i += 1
        self._punch_play_idx = i

    def _fire_cue(self, cue: PunchCue, *, hold_until: float | None = None) -> None:
        src = f"cue:{cue.cue_id}"
        until = float(hold_until) if hold_until is not None else float(cue.t) + max(0.02, float(cue.hold_sec))
        self._cue_hold_until[src] = until
        self.hold_add(src, dict(cue.tile), record=False)

    def _release_expired_cues(self, elapsed: float) -> None:
        expired = [s for s, u in self._cue_hold_until.items() if elapsed >= u - 0.0005]
        for src in expired:
            self._cue_hold_until.pop(src, None)
            if src in self.holds:
                self.hold_remove(src)

    def delete_cues(self, cue_ids: list[str]) -> None:
        ids = set(cue_ids)
        self.cues = [c for c in self.cues if c.cue_id not in ids]
        self._emit("cues")

    def clear_cues(self) -> None:
        self.cues.clear()
        self._emit("cues")

    def set_zoom(self, zoom: float, view_start: float | None = None) -> None:
        self.zoom = max(1.0, min(64.0, float(zoom)))
        vis = max(0.05, self.audio_duration / self.zoom) if self.audio_duration > 0 else 1.0
        if view_start is not None:
            self.view_start = float(view_start)
        max_start = max(0.0, self.audio_duration - vis)
        self.view_start = max(0.0, min(max_start, self.view_start))
        self._emit("timeline")

    # ---- project ----
    def save_project(self, name: str | None = None) -> dict[str, str]:
        if not self.audio_path or self.audio_mono is None:
            raise RuntimeError("先に音楽を読み込んでください")
        src = Path(self.audio_path)
        if not src.is_file():
            raise RuntimeError("元の音楽ファイルが見つかりません")
        stem = name or (src.stem + ".timeline")
        if not stem.endswith(".json"):
            stem = stem + ".json"
        proj = PROJECT_DIR / stem
        audio_dest = proj.with_name(proj.stem + src.suffix)
        if src.resolve() != audio_dest.resolve():
            shutil.copy2(src, audio_dest)
        payload = {
            "version": 2,
            "kind": "lightsystem_timeline",
            "audio_file": audio_dest.name,
            "audio_path_original": str(src),
            "duration_sec": self.audio_duration,
            "sr": self.audio_sr,
            "zoom": self.zoom,
            "view_start": self.view_start,
            "saved_at": datetime.now().isoformat(timespec="seconds"),
            "cues": [
                {
                    "t": c.t,
                    "hold_sec": c.hold_sec,
                    "recorded_at": c.recorded_at,
                    "cue_id": c.cue_id,
                    "tile": c.tile,
                }
                for c in self.cues
            ],
        }
        proj.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"project": str(proj), "audio": str(audio_dest)}

    def load_project(self, path: str | Path) -> None:
        path = Path(path)
        raw = json.loads(path.read_text(encoding="utf-8"))
        audio_name = str(raw.get("audio_file") or "").strip()
        audio_orig = str(raw.get("audio_path_original") or "").strip()
        candidates = []
        if audio_name:
            candidates.append(path.parent / audio_name)
        if audio_orig:
            candidates.append(Path(audio_orig))
        audio_path = next((p for p in candidates if p.is_file()), None)
        if audio_path is None:
            raise RuntimeError("プロジェクトの音楽ファイルが見つかりません")
        self.load_audio_file(audio_path)
        out: list[PunchCue] = []
        for item in raw.get("cues") or []:
            if not isinstance(item, dict) or not isinstance(item.get("tile"), dict):
                continue
            out.append(
                PunchCue(
                    t=float(item.get("t", 0.0)),
                    hold_sec=max(0.0, float(item.get("hold_sec", 0.05))),
                    recorded_at=str(item.get("recorded_at", "")),
                    cue_id=str(item.get("cue_id") or f"load-{uuid.uuid4()}"),
                    tile=dict(item["tile"]),
                )
            )
        out.sort(key=lambda c: (c.t, c.cue_id))
        self.cues = out
        try:
            self.zoom = max(1.0, min(64.0, float(raw.get("zoom", 1.0))))
            self.view_start = max(0.0, float(raw.get("view_start", 0.0)))
        except (TypeError, ValueError):
            pass
        self._emit("timeline")

    def shutdown(self) -> None:
        self.stop_playback()
        self.ai_stop()
        self.dmx_disconnect()
        self.ble_disconnect()
        if self._ble_worker is not None:
            try:
                self._ble_worker.stop()
            except Exception:
                pass


_CONTROLLER: LightController | None = None
_CTRL_LOCK = threading.Lock()


def get_controller() -> LightController:
    global _CONTROLLER
    with _CTRL_LOCK:
        if _CONTROLLER is None:
            _CONTROLLER = LightController()
        return _CONTROLLER
