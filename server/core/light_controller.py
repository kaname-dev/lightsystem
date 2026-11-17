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
from server.core.artnet_sender import ArtNetSender  # noqa: E402
from server.core.esp32_patterns import (  # noqa: E402
    ESP32_PATTERN_PRESETS,
    render_pattern,
)

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
        _motion_name_drives_ch9,
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

    def _motion_name_drives_ch9(name: str) -> bool:  # type: ignore[misc]
        if not name:
            return False
        return ("4点上空ファン" in name) or ("色のみ" in name) or ("オート色" in name)

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
        "apply_esp32": False,
        "led_r": 255,
        "led_g": 80,
        "led_b": 40,
        "led_brightness": 100.0,
        "led_mode": None,
        "led_speed": 50,
        # ESP32: all | segments | group
        "esp32_target": "all",
        "esp32_segments": [],
        "esp32_segment_count": None,  # 保存時の分割数（再マップ用）
        "esp32_ranges": [],  # [[start_frac, end_frac], ...] 0..1
        "esp32_group_id": None,
        # color | pattern
        "esp32_fx": "color",
        "esp32_pattern_id": None,
        "esp32_pattern_speed": 1.0,
        "esp32_others_off": True,
    }


def _esp32_indices_to_ranges(
    indices: list[int], segment_count: int
) -> list[list[float]]:
    """区間インデックス → ストリップ上の正規化範囲（連続区間は結合）。"""
    n = max(1, int(segment_count))
    clean = sorted({max(0, min(n - 1, int(i))) for i in (indices or [])})
    if not clean:
        return []
    ranges: list[list[float]] = []
    start = clean[0]
    prev = clean[0]
    for i in clean[1:]:
        if i == prev + 1:
            prev = i
            continue
        ranges.append([start / n, (prev + 1) / n])
        start = prev = i
    ranges.append([start / n, (prev + 1) / n])
    return ranges


def _esp32_normalize_ranges(
    ranges: list[list[float]] | list[tuple[float, float]] | None,
) -> list[list[float]]:
    out: list[list[float]] = []
    for raw in ranges or []:
        if not isinstance(raw, (list, tuple)) or len(raw) < 2:
            continue
        try:
            a = max(0.0, min(1.0, float(raw[0])))
            b = max(0.0, min(1.0, float(raw[1])))
        except (TypeError, ValueError):
            continue
        if b < a:
            a, b = b, a
        if b - a > 1e-12:
            out.append([a, b])
    return out


def _esp32_ranges_to_indices(
    ranges: list[list[float]] | list[tuple[float, float]], segment_count: int
) -> list[int]:
    """正規化範囲 → 現在の分割数での区間インデックス（重なりがあれば含む・UI用）。"""
    n = max(1, int(segment_count))
    out: set[int] = set()
    for a, b in _esp32_normalize_ranges(ranges):
        for i in range(n):
            s = i / n
            e = (i + 1) / n
            if e > a + 1e-12 and s < b - 1e-12:
                out.add(i)
    return sorted(out)


def _esp32_ranges_to_pixel_spans(
    ranges: list[list[float]] | list[tuple[float, float]],
    pixel_count: int,
) -> list[tuple[int, int]]:
    """正規化範囲 → 正確なピクセル半開区間 [start, end)。分割数に依存しない。"""
    n = max(1, int(pixel_count))
    spans: list[tuple[int, int]] = []
    for a, b in _esp32_normalize_ranges(ranges):
        start = int(math.floor(a * n + 1e-12))
        end = int(math.ceil(b * n - 1e-12))
        start = max(0, min(n, start))
        end = max(0, min(n, end))
        if end <= start and b > a:
            end = min(n, start + 1)
        if end > start:
            spans.append((start, end))
    # merge overlaps
    if not spans:
        return []
    spans.sort()
    merged: list[tuple[int, int]] = [spans[0]]
    for s, e in spans[1:]:
        ps, pe = merged[-1]
        if s <= pe:
            merged[-1] = (ps, max(pe, e))
        else:
            merged.append((s, e))
    return merged


def _esp32_pixel_spans_to_set(
    spans: list[tuple[int, int]], pixel_count: int
) -> set[int]:
    n = max(1, int(pixel_count))
    out: set[int] = set()
    for s, e in spans:
        for i in range(max(0, s), min(n, e)):
            out.add(i)
    return out


def _esp32_normalize_pixel_spans(
    spans: list[list[int]] | list[tuple[int, int]] | None,
    pixel_count: int,
) -> list[list[int]]:
    """絶対ピクセル半開区間 [start, end) を正規化・結合。分割数変換はしない。"""
    n = max(1, int(pixel_count))
    cleaned: list[tuple[int, int]] = []
    for raw in spans or []:
        if not isinstance(raw, (list, tuple)) or len(raw) < 2:
            continue
        try:
            s = int(raw[0])
            e = int(raw[1])
        except (TypeError, ValueError):
            continue
        if e < s:
            s, e = e, s
        s = max(0, min(n, s))
        e = max(0, min(n, e))
        if e > s:
            cleaned.append((s, e))
    if not cleaned:
        return []
    cleaned.sort()
    merged: list[list[int]] = [[cleaned[0][0], cleaned[0][1]]]
    for s, e in cleaned[1:]:
        if s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    return merged


def _esp32_segment_pixel_bounds(
    segment_count: int, pixel_count: int
) -> list[tuple[int, int]]:
    n_pix = max(1, int(pixel_count))
    n_seg = max(1, int(segment_count))
    base = n_pix // n_seg
    rem = n_pix % n_seg
    out: list[tuple[int, int]] = []
    start = 0
    for i in range(n_seg):
        length = base + (1 if i < rem else 0)
        end = start + length
        out.append((start, end))
        start = end
    return out


def _esp32_indices_to_pixel_spans(
    indices: list[int], segment_count: int, pixel_count: int
) -> list[list[int]]:
    """保存時点の分割で選んだ区間 → 絶対ピクセル範囲（以後変換しない）。"""
    bounds = _esp32_segment_pixel_bounds(segment_count, pixel_count)
    n = len(bounds)
    chosen = sorted(
        {max(0, min(n - 1, int(i))) for i in (indices or []) if n > 0}
    )
    spans = [list(bounds[i]) for i in chosen if 0 <= i < n]
    return _esp32_normalize_pixel_spans(spans, pixel_count)


def _esp32_spans_overlap_indices(
    spans: list[list[int]] | list[tuple[int, int]],
    segment_count: int,
    pixel_count: int,
) -> list[int]:
    """表示用: 現在分割のどの区間が保存ピクセルと重なるか（グループ定義の変換ではない）。"""
    bounds = _esp32_segment_pixel_bounds(segment_count, pixel_count)
    hit: set[int] = set()
    for i, (a, b) in enumerate(bounds):
        for s, e in spans or []:
            if e > a and s < b:
                hit.add(i)
                break
    return sorted(hit)


def _esp32_remap_indices(
    indices: list[int], from_count: int, to_count: int
) -> list[int]:
    if int(from_count) == int(to_count):
        n = max(1, int(to_count))
        return sorted({max(0, min(n - 1, int(i))) for i in (indices or [])})
    ranges = _esp32_indices_to_ranges(indices, from_count)
    return _esp32_ranges_to_indices(ranges, to_count)


def _normalize_esp32_group(
    raw: dict[str, Any],
    *,
    fallback_segment_count: int = 8,
    fallback_pixel_count: int = 720,
) -> dict[str, Any] | None:
    """グループは絶対ピクセル spans で保持。分割数による再マップは行わない。"""
    gid = str(raw.get("id") or "").strip()
    name = str(raw.get("name") or "").strip() or "グループ"
    if not gid:
        return None
    try:
        pc = int(raw.get("pixel_count") or fallback_pixel_count or 720)
    except (TypeError, ValueError):
        pc = max(1, int(fallback_pixel_count or 720))
    pc = max(1, min(2040, pc))

    spans = _esp32_normalize_pixel_spans(raw.get("spans") or [], pc)
    if not spans:
        # 旧形式: ranges / indices → 保存時のピクセルへ一度だけ変換
        try:
            sc = int(raw.get("segment_count") or fallback_segment_count or 8)
        except (TypeError, ValueError):
            sc = max(1, int(fallback_segment_count or 8))
        sc = max(1, min(64, sc))
        idxs_raw = raw.get("indices") or []
        indices: list[int] = []
        if isinstance(idxs_raw, list):
            indices = sorted(
                {
                    max(0, min(sc - 1, int(i)))
                    for i in idxs_raw
                    if isinstance(i, (int, float)) or str(i).isdigit()
                }
            )
        ranges = _esp32_normalize_ranges(raw.get("ranges") or [])
        if not ranges and indices:
            ranges = _esp32_indices_to_ranges(indices, sc)
        if ranges:
            spans = [
                [s, e]
                for s, e in _esp32_ranges_to_pixel_spans(ranges, pc)
            ]
        elif indices:
            spans = _esp32_indices_to_pixel_spans(indices, sc, pc)
    if not spans:
        return None
    px_total = sum(e - s for s, e in spans)
    return {
        "id": gid[:64],
        "name": name[:40],
        "spans": spans,
        "pixel_count": pc,
        "pixel_total": px_total,
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
    base["apply_esp32"] = bool(base.get("apply_esp32", False))
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
    tgt = str(base.get("esp32_target") or "all").strip().lower()
    if tgt not in ("all", "segments", "group"):
        tgt = "all"
    base["esp32_target"] = tgt
    segs_raw = base.get("esp32_segments") or []
    if not isinstance(segs_raw, list):
        segs_raw = []
    base["esp32_segments"] = sorted(
        {
            max(0, min(63, int(x)))
            for x in segs_raw
            if str(x).lstrip("-").isdigit() or isinstance(x, int)
        }
    )
    sc_raw = base.get("esp32_segment_count")
    if sc_raw in (None, "", "null"):
        base["esp32_segment_count"] = None
    else:
        try:
            base["esp32_segment_count"] = max(1, min(64, int(sc_raw)))
        except (TypeError, ValueError):
            base["esp32_segment_count"] = None
    ranges_raw = base.get("esp32_ranges") or []
    ranges: list[list[float]] = []
    if isinstance(ranges_raw, list):
        for rr in ranges_raw:
            if not isinstance(rr, (list, tuple)) or len(rr) < 2:
                continue
            try:
                a = max(0.0, min(1.0, float(rr[0])))
                b = max(0.0, min(1.0, float(rr[1])))
            except (TypeError, ValueError):
                continue
            if b < a:
                a, b = b, a
            if b - a > 1e-9:
                ranges.append([a, b])
    if not ranges and base["esp32_segments"]:
        sc = base["esp32_segment_count"] or max(1, (max(base["esp32_segments"]) + 1))
        ranges = _esp32_indices_to_ranges(base["esp32_segments"], sc)
        if base["esp32_segment_count"] is None:
            base["esp32_segment_count"] = sc
    base["esp32_ranges"] = ranges
    gid = base.get("esp32_group_id")
    base["esp32_group_id"] = None if gid in (None, "", "null") else str(gid)[:64]
    fx = str(base.get("esp32_fx") or "color").strip().lower()
    base["esp32_fx"] = "pattern" if fx == "pattern" else "color"
    pid = base.get("esp32_pattern_id")
    base["esp32_pattern_id"] = None if pid in (None, "", "null") else str(pid)[:64]
    try:
        base["esp32_pattern_speed"] = max(
            0.05, min(5.0, float(base.get("esp32_pattern_speed", 1.0) or 1.0))
        )
    except (TypeError, ValueError):
        base["esp32_pattern_speed"] = 1.0
    base["esp32_others_off"] = bool(base.get("esp32_others_off", True))
    return base

TILES_PATH = DMX_DIR / "pattern_tiles.json"
DATA_DIR = ROOT / "server" / "data"
UPLOAD_DIR = DATA_DIR / "uploads"
PROJECT_DIR = DATA_DIR / "projects"
ESP32_GROUPS_PATH = DATA_DIR / "esp32_groups.json"


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
    _cue_hist: list[list[dict[str, Any]]] = field(default_factory=list)
    _cue_hist_i: int = 0
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

    # ESP32 PixelCtl (Art-Net)
    _artnet: ArtNetSender = field(default_factory=ArtNetSender)
    esp32_host: str = ""
    esp32_universe: int = 0
    esp32_pixel_count: int = 720
    esp32_enabled: bool = False
    esp32_auto_connect: bool = False
    esp32_status: str = "無効"
    _ai_esp_last: float = 0.0
    esp32_pattern_id: str = ""
    esp32_pattern_speed: float = 1.0
    esp32_pattern_running: bool = False
    _esp32_pattern_stop: threading.Event = field(default_factory=threading.Event)
    _esp32_pattern_thread: threading.Thread | None = None
    _esp32_color: tuple[int, int, int] = (255, 80, 40)
    _esp32_brightness: float = 1.0
    _esp32_color2: tuple[int, int, int] = (40, 80, 255)
    esp32_segment_count: int = 8
    # each: {"r":int,"g":int,"b":int,"on":bool}
    esp32_segments: list[dict[str, Any]] = field(default_factory=list)
    esp32_selected: list[int] = field(default_factory=list)
    esp32_groups: list[dict[str, Any]] = field(default_factory=list)
    _esp32_pattern_mask: set[int] | None = None  # segment indices (legacy)
    _esp32_pattern_pixel_mask: set[int] | None = None  # exact pixels
    _esp32_pattern_others_off: bool = True
    _esp32_pattern_ignore_holds: bool = False
    _esp32_compose_stop: threading.Event = field(default_factory=threading.Event)
    _esp32_compose_thread: threading.Thread | None = None
    _esp32_compose_t0: float = 0.0
    esp32_overlap_warnings: list[dict[str, Any]] = field(default_factory=list)
    esp32_last_overlap_alert: dict[str, Any] | None = None

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
        self._reset_cue_history()
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
        self.esp32_host = str(s.get("esp32_host", "") or "").strip()
        self.esp32_auto_connect = bool(s.get("esp32_auto_connect", False))
        self.esp32_enabled = bool(s.get("esp32_enabled", False))
        try:
            self.esp32_universe = max(0, int(s.get("esp32_universe", 0) or 0))
        except (TypeError, ValueError):
            self.esp32_universe = 0
        try:
            self.esp32_pixel_count = max(
                1, min(2040, int(s.get("esp32_pixel_count", 720) or 720))
            )
        except (TypeError, ValueError):
            self.esp32_pixel_count = 720
        try:
            self.esp32_segment_count = max(
                1, min(64, int(s.get("esp32_segment_count", 8) or 8))
            )
        except (TypeError, ValueError):
            self.esp32_segment_count = 8
        self._esp32_init_segments(self.esp32_segment_count, keep_colors=False)
        self.esp32_groups = self._load_esp32_groups()
        # 旧 ranges/indices 形式を spans に移行して保存
        if self.esp32_groups:
            self._save_esp32_groups()
        self._artnet.configure(
            host=self.esp32_host,
            universe=self.esp32_universe,
            pixel_count=self.esp32_pixel_count,
            enabled=False,
        )
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
        if self.esp32_auto_connect and self.esp32_host:
            try:
                self.esp32_enable(True)
            except Exception as e:
                self.last_error = f"ESP32自動接続失敗: {e}"
                self._emit("error")
        elif self.esp32_enabled and self.esp32_host:
            try:
                self.esp32_enable(True)
            except Exception:
                pass

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
                "esp32": {
                    "enabled": self.esp32_enabled,
                    "host": self.esp32_host,
                    "universe": self.esp32_universe,
                    "pixel_count": self.esp32_pixel_count,
                    "auto_connect": self.esp32_auto_connect,
                    "status": self.esp32_status,
                    "last_rgb": list(self._artnet.last_rgb),
                    "last_error": self._artnet.last_error,
                    "pattern_id": self.esp32_pattern_id,
                    "pattern_speed": self.esp32_pattern_speed,
                    "pattern_running": self.esp32_pattern_running,
                    "patterns": list(ESP32_PATTERN_PRESETS),
                    "color": list(self._esp32_color),
                    "color2": list(self._esp32_color2),
                    "brightness": self._esp32_brightness,
                    "segment_count": self.esp32_segment_count,
                    "segments": [dict(x) for x in self.esp32_segments],
                    "selected": list(self.esp32_selected),
                    "groups": [self._esp32_group_public(g) for g in self.esp32_groups],
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
                "cue_history": {
                    "can_undo": self._cue_hist_i > 0,
                    "can_redo": self._cue_hist_i < len(self._cue_hist) - 1,
                    "index": self._cue_hist_i,
                    "length": len(self._cue_hist),
                },
                "esp32_overlap_warnings": list(self.esp32_overlap_warnings),
                "esp32_last_overlap_alert": self.esp32_last_overlap_alert,
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
        recorded = False
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
                recorded = True
                alert = self._refresh_esp32_overlap_warnings(new_cue=cue)
                self.esp32_last_overlap_alert = alert
            if source in self.holds:
                del self.holds[source]
            if source in self.hold_order:
                self.hold_order.remove(source)
            self._cue_hold_until.pop(source, None)
            self._sync_holds()
        if recorded:
            self._commit_cue_history()
            self._emit("cues")
            self._emit("esp32")
        self._emit("hold")

    def _sync_holds(self) -> None:
        if not self.holds:
            self.stop_motion()
            self.set_channels([0] * 10, push=True)
            self._ble_override(None)
            self._esp32_compose_stop_loop()
            self.esp32_pattern_stop()
            self._esp32_override(None)
            return
        order = [s for s in self.hold_order if s in self.holds]
        tiles = [self.holds[s] for s in order]
        laser = next((t for t in reversed(tiles) if t.get("apply_laser", True)), None)
        led = next((t for t in reversed(tiles) if t.get("apply_led", False)), None)
        esp_tiles = [t for t in tiles if t.get("apply_esp32", False)]
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
        if not esp_tiles:
            self._esp32_compose_stop_loop()
            self.esp32_pattern_stop()
            self._esp32_override(None)
        elif len(esp_tiles) == 1 and str(esp_tiles[0].get("esp32_fx") or "color") == "pattern":
            # 単独パターンはそのまま
            self._esp32_compose_stop_loop()
            self._esp32_apply_tile(esp_tiles[0])
        elif len(esp_tiles) == 1:
            self._esp32_compose_stop_loop()
            self.esp32_pattern_stop()
            self._esp32_apply_tile(esp_tiles[0])
        else:
            # 複数同時: 重なり区間を等分して両方（複数）点灯
            self.esp32_pattern_stop()
            needs_loop = any(
                str(t.get("esp32_fx") or "color") == "pattern" for t in esp_tiles
            )
            if needs_loop:
                self._esp32_compose_start_loop(esp_tiles)
            else:
                self._esp32_compose_stop_loop()
                self._esp32_compose_push(esp_tiles, time.monotonic())
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
        ch9 = max(0, min(255, int(tile.get("club_dot_ch9", vals[8]))))
        if len(vals) == 10:
            vals[8] = ch9
        self.club_dot_ch9 = ch9
        self.set_channels(vals, push=True)
        sp = float(tile.get("motion_speed", 1.35))
        self.motion_speed = max(MOTION_SPEED_MIN, min(MOTION_SPEED_MAX, sp))
        mi = int(tile.get("motion_index", 0))
        if tile.get("apply_dot_base", True):
            self.apply_dot_base = True
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

    def _esp32_init_segments(self, count: int, *, keep_colors: bool = True) -> None:
        old_count = max(1, int(self.esp32_segment_count or 1))
        n = max(1, min(64, int(count)))
        old = list(self.esp32_segments) if keep_colors else []
        # 色状態も正規化範囲経由で引き継ぐ
        old_lit_ranges: list[list[float]] = []
        if keep_colors and old:
            lit = [i for i, s in enumerate(old) if s.get("on")]
            if lit:
                old_lit_ranges = _esp32_indices_to_ranges(lit, old_count)
        segs: list[dict[str, Any]] = []
        for i in range(n):
            segs.append({"r": 0, "g": 0, "b": 0, "on": False})
        if keep_colors and old:
            # 代表色: 旧区間の色を重なる新区間へコピー
            for new_i in range(n):
                ns, ne = new_i / n, (new_i + 1) / n
                # 最も重なる旧区間の色を採用
                best_j = None
                best_ov = 0.0
                for j, s in enumerate(old):
                    os, oe = j / old_count, (j + 1) / old_count
                    ov = max(0.0, min(ne, oe) - max(ns, os))
                    if ov > best_ov:
                        best_ov = ov
                        best_j = j
                if best_j is not None and best_ov > 1e-9:
                    src = old[best_j]
                    segs[new_i] = {
                        "r": int(src.get("r", 0)),
                        "g": int(src.get("g", 0)),
                        "b": int(src.get("b", 0)),
                        "on": False,
                    }
            for i in _esp32_ranges_to_indices(old_lit_ranges, n):
                segs[i]["on"] = True
        # 選択も再マップ
        if self.esp32_selected:
            self.esp32_selected = _esp32_remap_indices(
                self.esp32_selected, old_count, n
            )
        self.esp32_segment_count = n
        self.esp32_segments = segs

    def _esp32_segment_ranges(self) -> list[tuple[int, int]]:
        n_pix = max(1, int(self.esp32_pixel_count))
        n_seg = max(1, int(self.esp32_segment_count))
        base = n_pix // n_seg
        rem = n_pix % n_seg
        ranges: list[tuple[int, int]] = []
        start = 0
        for i in range(n_seg):
            length = base + (1 if i < rem else 0)
            end = start + length
            ranges.append((start, end))
            start = end
        return ranges

    def _esp32_pixels_from_segments(self) -> list[tuple[int, int, int]]:
        bri = max(0.0, min(1.0, float(self._esp32_brightness)))
        pixels: list[tuple[int, int, int]] = [(0, 0, 0)] * max(1, self.esp32_pixel_count)
        ranges = self._esp32_segment_ranges()
        for i, seg in enumerate(self.esp32_segments):
            if i >= len(ranges):
                break
            a, b = ranges[i]
            if not seg.get("on", False) or a >= b:
                col = (0, 0, 0)
            else:
                col = (
                    max(0, min(255, int(round(int(seg.get("r", 0)) * bri)))),
                    max(0, min(255, int(round(int(seg.get("g", 0)) * bri)))),
                    max(0, min(255, int(round(int(seg.get("b", 0)) * bri)))),
                )
            for p in range(a, b):
                if p < len(pixels):
                    pixels[p] = col
        return pixels

    def _esp32_push_segments(self) -> None:
        if not self.esp32_enabled or not self.esp32_host:
            return
        pixels = self._esp32_pixels_from_segments()
        self._artnet.send_rgb_buffer(pixels, brightness=1.0)
        if self._artnet.last_error:
            self.esp32_status = f"区間送信エラー: {self._artnet.last_error}"
        else:
            lit = sum(1 for s in self.esp32_segments if s.get("on"))
            self.esp32_status = (
                f"区間送信 {self.esp32_host} "
                f"{self.esp32_segment_count}分割 / 点灯{lit}"
            )

    def _esp32_override(self, payload: dict[str, Any] | None) -> None:
        if not self.esp32_enabled or not self.esp32_host:
            return
        # 固体色オーバーライド時はパターン停止
        if payload is not None and self.esp32_pattern_running:
            self.esp32_pattern_stop()
        try:
            if payload is None:
                for seg in self.esp32_segments:
                    seg["on"] = False
                self._artnet.blackout()
                self.esp32_status = f"送信中 {self.esp32_host}（消灯）"
                return
            r = int(payload.get("r", 0))
            g = int(payload.get("g", 0))
            b = int(payload.get("b", 0))
            bri = float(payload.get("brightness", 1.0))
            self._esp32_color = (
                max(0, min(255, r)),
                max(0, min(255, g)),
                max(0, min(255, b)),
            )
            self._esp32_brightness = max(0.0, min(1.0, bri))
            # 全区間を同じ色に同期（全体単色）
            on = not (r == 0 and g == 0 and b == 0)
            for seg in self.esp32_segments:
                seg["r"] = self._esp32_color[0]
                seg["g"] = self._esp32_color[1]
                seg["b"] = self._esp32_color[2]
                seg["on"] = on
            self._artnet.send_rgb_fill(
                self._esp32_color[0],
                self._esp32_color[1],
                self._esp32_color[2],
                brightness=self._esp32_brightness,
            )
            if self._artnet.last_error:
                self.esp32_status = f"送信エラー: {self._artnet.last_error}"
            else:
                rr, gg, bb = self._artnet.last_rgb
                self.esp32_status = f"送信中 {self.esp32_host} RGB({rr},{gg},{bb})"
        except Exception as e:
            self.esp32_status = f"送信失敗: {e}"

    def esp32_configure(
        self,
        host: str,
        *,
        universe: int = 0,
        pixel_count: int = 720,
        auto_connect: bool | None = None,
    ) -> None:
        self.esp32_host = (host or "").strip()
        self.esp32_universe = max(0, min(32767, int(universe)))
        self.esp32_pixel_count = max(1, min(2040, int(pixel_count)))
        if auto_connect is not None:
            self.esp32_auto_connect = bool(auto_connect)
        self._artnet.configure(
            host=self.esp32_host,
            universe=self.esp32_universe,
            pixel_count=self.esp32_pixel_count,
            enabled=self.esp32_enabled,
        )
        try:
            update_settings(
                esp32_host=self.esp32_host,
                esp32_universe=self.esp32_universe,
                esp32_pixel_count=self.esp32_pixel_count,
                esp32_auto_connect=self.esp32_auto_connect,
                esp32_enabled=self.esp32_enabled,
            )
        except Exception:
            pass
        self._emit("esp32")

    def esp32_enable(self, enabled: bool) -> None:
        self.esp32_enabled = bool(enabled)
        if not self.esp32_host and self.esp32_enabled:
            self.esp32_enabled = False
            raise RuntimeError("ESP32 の IP / ホスト名を入力してください")
        if not self.esp32_enabled:
            self.esp32_pattern_stop()
        self._artnet.configure(
            host=self.esp32_host,
            universe=self.esp32_universe,
            pixel_count=self.esp32_pixel_count,
            enabled=self.esp32_enabled,
        )
        if self.esp32_enabled:
            self.esp32_status = (
                f"有効 → {self.esp32_host}:{self._artnet.port} "
                f"Uni{self.esp32_universe}+ ({self.esp32_pixel_count}px)"
            )
            self._artnet.blackout()
        else:
            try:
                self._artnet.blackout()
            except Exception:
                pass
            self.esp32_status = "無効"
        try:
            update_settings(esp32_enabled=self.esp32_enabled, esp32_host=self.esp32_host)
        except Exception:
            pass
        self._emit("esp32")

    def esp32_color(self, r: int, g: int, b: int, brightness: float = 1.0) -> None:
        if not self.esp32_enabled:
            raise RuntimeError("ESP32 送信が無効です。先に有効化してください")
        self._esp32_override(
            {"r": int(r), "g": int(g), "b": int(b), "brightness": float(brightness)}
        )
        self._emit("esp32")

    def esp32_pattern_stop(self) -> None:
        self._esp32_pattern_stop.set()
        self.esp32_pattern_running = False
        th = self._esp32_pattern_thread
        if th is not None and th.is_alive() and th is not threading.current_thread():
            th.join(timeout=0.6)
        self._esp32_pattern_thread = None
        if not self.esp32_pattern_id:
            return
        # keep last id for UI, mark stopped
        self.esp32_status = f"パターン停止（{self.esp32_pattern_id}）"
        self._emit("esp32")

    def esp32_pattern_start(
        self,
        pattern_id: str,
        *,
        speed: float = 1.0,
        brightness: float | None = None,
        r: int | None = None,
        g: int | None = None,
        b: int | None = None,
        r2: int | None = None,
        g2: int | None = None,
        b2: int | None = None,
        segment_indices: list[int] | None = None,
        ranges: list[list[float]] | None = None,
        pixel_spans: list[list[int]] | list[tuple[int, int]] | None = None,
        others_off: bool = True,
        ignore_holds: bool = False,
    ) -> None:
        if not self.esp32_enabled:
            raise RuntimeError("ESP32 送信が無効です。先に有効化してください")
        pid = (pattern_id or "").strip().lower()
        known = {p["id"] for p in ESP32_PATTERN_PRESETS}
        if pid not in known:
            raise RuntimeError(f"未知のパターン: {pattern_id}")
        if r is not None and g is not None and b is not None:
            self._esp32_color = (
                max(0, min(255, int(r))),
                max(0, min(255, int(g))),
                max(0, min(255, int(b))),
            )
        if r2 is not None and g2 is not None and b2 is not None:
            self._esp32_color2 = (
                max(0, min(255, int(r2))),
                max(0, min(255, int(g2))),
                max(0, min(255, int(b2))),
            )
        if brightness is not None:
            self._esp32_brightness = max(0.0, min(1.0, float(brightness)))
        self.esp32_pattern_speed = max(0.05, min(5.0, float(speed)))
        # 絶対ピクセル spans 最優先（グループ）。分割数変換なし
        abs_spans = _esp32_normalize_pixel_spans(
            pixel_spans, self.esp32_pixel_count
        )
        norm_ranges = _esp32_normalize_ranges(ranges)
        if abs_spans:
            self._esp32_pattern_pixel_mask = _esp32_pixel_spans_to_set(
                [(s, e) for s, e in abs_spans], self.esp32_pixel_count
            )
            self._esp32_pattern_mask = None
        elif norm_ranges:
            spans = _esp32_ranges_to_pixel_spans(norm_ranges, self.esp32_pixel_count)
            self._esp32_pattern_pixel_mask = _esp32_pixel_spans_to_set(
                spans, self.esp32_pixel_count
            )
            self._esp32_pattern_mask = None
        elif segment_indices is None:
            self._esp32_pattern_mask = None
            self._esp32_pattern_pixel_mask = None
        else:
            # 現在分割の区間 → 正規化範囲 → ピクセル（確実に位置固定）
            idx_ranges = _esp32_indices_to_ranges(
                list(segment_indices), self.esp32_segment_count
            )
            spans = _esp32_ranges_to_pixel_spans(idx_ranges, self.esp32_pixel_count)
            self._esp32_pattern_pixel_mask = _esp32_pixel_spans_to_set(
                spans, self.esp32_pixel_count
            )
            self._esp32_pattern_mask = None
        self._esp32_pattern_others_off = bool(others_off)
        self._esp32_pattern_ignore_holds = bool(ignore_holds)
        self.esp32_pattern_stop()
        self._esp32_pattern_stop.clear()
        self.esp32_pattern_id = pid
        self.esp32_pattern_running = True
        self.esp32_status = f"パターン再生中: {pid}"
        self._esp32_pattern_thread = threading.Thread(
            target=self._esp32_pattern_loop, daemon=True
        )
        self._esp32_pattern_thread.start()
        self._emit("esp32")

    def _esp32_pattern_loop(self) -> None:
        fps = 30.0
        dt = 1.0 / fps
        t0 = time.monotonic()
        pid = self.esp32_pattern_id
        ignore_holds = bool(getattr(self, "_esp32_pattern_ignore_holds", False))
        while not self._esp32_pattern_stop.is_set() and self.esp32_enabled:
            if not self.esp32_enabled or not self.esp32_host:
                break
            # 手動パターン中はホールドで一時停止。タイル起点パターンは継続
            if self.hold_order and not ignore_holds:
                if self._esp32_pattern_stop.wait(0.05):
                    break
                continue
            now = time.monotonic()
            t = now - t0
            try:
                pixels = render_pattern(
                    pid,
                    self.esp32_pixel_count,
                    t,
                    color=self._esp32_color,
                    color2=self._esp32_color2,
                    speed=self.esp32_pattern_speed,
                    brightness=self._esp32_brightness,
                )
                allowed = self._esp32_pattern_pixel_mask
                if allowed is None and self._esp32_pattern_mask is not None:
                    # 互換: 区間マスク → ピクセル
                    seg_ranges = self._esp32_segment_ranges()
                    allowed = set()
                    for si in self._esp32_pattern_mask:
                        if 0 <= si < len(seg_ranges):
                            a, b = seg_ranges[si]
                            allowed.update(range(a, b))
                if allowed is not None:
                    if self._esp32_pattern_others_off:
                        pixels = [
                            px if i in allowed else (0, 0, 0)
                            for i, px in enumerate(pixels)
                        ]
                    else:
                        base = self._esp32_pixels_from_segments()
                        pixels = [
                            px if i in allowed else base[i]
                            for i, px in enumerate(pixels)
                        ]
                self._artnet.send_rgb_buffer(pixels, brightness=1.0)
                if self._artnet.last_error:
                    self.esp32_status = f"パターン送信エラー: {self._artnet.last_error}"
            except Exception as e:
                self.esp32_status = f"パターン失敗: {e}"
                break
            if self._esp32_pattern_stop.wait(dt):
                break
        self.esp32_pattern_running = False
        self._esp32_pattern_ignore_holds = False
        self._emit("esp32")

    def esp32_set_segment_count(self, count: int) -> None:
        self._esp32_init_segments(count, keep_colors=True)
        try:
            update_settings(esp32_segment_count=self.esp32_segment_count)
        except Exception:
            pass
        if self.esp32_enabled and not self.esp32_pattern_running:
            self._esp32_push_segments()
        self.esp32_status = f"分割数 {self.esp32_segment_count}"
        self._emit("esp32")

    def esp32_set_segment(
        self,
        index: int,
        *,
        r: int | None = None,
        g: int | None = None,
        b: int | None = None,
        on: bool | None = None,
        brightness: float | None = None,
        push: bool = True,
    ) -> None:
        if not self.esp32_segments:
            self._esp32_init_segments(self.esp32_segment_count, keep_colors=False)
        idx = int(index)
        if idx < 0 or idx >= len(self.esp32_segments):
            raise RuntimeError(f"区間インデックス不正: {index}")
        if self.esp32_pattern_running:
            self.esp32_pattern_stop()
        if brightness is not None:
            self._esp32_brightness = max(0.0, min(1.0, float(brightness)))
        seg = self.esp32_segments[idx]
        if r is not None and g is not None and b is not None:
            seg["r"] = max(0, min(255, int(r)))
            seg["g"] = max(0, min(255, int(g)))
            seg["b"] = max(0, min(255, int(b)))
            self._esp32_color = (seg["r"], seg["g"], seg["b"])
        if on is None:
            # 色指定のみなら点灯、明示なければ現状維持だが色指定時は点灯
            if r is not None:
                seg["on"] = True
        else:
            seg["on"] = bool(on)
        if push:
            if not self.esp32_enabled:
                raise RuntimeError("ESP32 送信が無効です。先に有効化してください")
            self._esp32_push_segments()
        self._emit("esp32")

    def esp32_segments_apply_all(
        self,
        *,
        r: int,
        g: int,
        b: int,
        on: bool = True,
        brightness: float | None = None,
    ) -> None:
        if self.esp32_pattern_running:
            self.esp32_pattern_stop()
        if brightness is not None:
            self._esp32_brightness = max(0.0, min(1.0, float(brightness)))
        rr = max(0, min(255, int(r)))
        gg = max(0, min(255, int(g)))
        bb = max(0, min(255, int(b)))
        self._esp32_color = (rr, gg, bb)
        for seg in self.esp32_segments:
            seg["r"] = rr
            seg["g"] = gg
            seg["b"] = bb
            seg["on"] = bool(on) and not (rr == 0 and gg == 0 and bb == 0)
        if not self.esp32_enabled:
            raise RuntimeError("ESP32 送信が無効です。先に有効化してください")
        self._esp32_push_segments()
        self._emit("esp32")

    def _load_esp32_groups(self) -> list[dict[str, Any]]:
        try:
            if not ESP32_GROUPS_PATH.exists():
                return []
            raw = json.loads(ESP32_GROUPS_PATH.read_text(encoding="utf-8"))
            if not isinstance(raw, list):
                return []
            out: list[dict[str, Any]] = []
            for g in raw:
                if not isinstance(g, dict):
                    continue
                norm = _normalize_esp32_group(
                    g,
                    fallback_segment_count=self.esp32_segment_count or 8,
                    fallback_pixel_count=self.esp32_pixel_count or 720,
                )
                if norm:
                    out.append(norm)
            return out
        except Exception:
            return []

    def _save_esp32_groups(self) -> None:
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            payload = []
            for g in self.esp32_groups:
                payload.append(
                    {
                        "id": g.get("id"),
                        "name": g.get("name"),
                        "spans": list(g.get("spans") or []),
                        "pixel_count": int(
                            g.get("pixel_count") or self.esp32_pixel_count or 720
                        ),
                    }
                )
            ESP32_GROUPS_PATH.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:
            pass

    def _esp32_group_public(self, g: dict[str, Any]) -> dict[str, Any]:
        """UI向け。spans が本体。overlap_indices は表示用の重なり区間のみ。"""
        spans = _esp32_normalize_pixel_spans(
            g.get("spans") or [], self.esp32_pixel_count
        )
        overlap = _esp32_spans_overlap_indices(
            spans, self.esp32_segment_count, self.esp32_pixel_count
        )
        px_total = sum(e - s for s, e in spans)
        return {
            "id": g.get("id"),
            "name": g.get("name"),
            "spans": spans,
            "pixel_count": int(g.get("pixel_count") or self.esp32_pixel_count or 720),
            "pixel_total": px_total,
            "overlap_indices": overlap,
        }

    def _esp32_resolve_group_spans(self, group_id: str) -> list[list[int]]:
        gid = str(group_id or "")
        for g in self.esp32_groups:
            if g.get("id") == gid:
                return _esp32_normalize_pixel_spans(
                    g.get("spans") or [], self.esp32_pixel_count
                )
        return []

    def _esp32_resolve_group_indices(self, group_id: str) -> list[int]:
        """表示・互換用。グループ本体は spans。"""
        spans = self._esp32_resolve_group_spans(group_id)
        return _esp32_spans_overlap_indices(
            spans, self.esp32_segment_count, self.esp32_pixel_count
        )

    def _esp32_resolve_tile_ranges(
        self, tile: dict[str, Any]
    ) -> list[list[float]] | None:
        """None = 全体。list = 正規化範囲。グループは spans 優先のため None を返し別経路。"""
        target = str(tile.get("esp32_target") or "all").lower()
        if target == "segments":
            ranges = _esp32_normalize_ranges(tile.get("esp32_ranges") or [])
            if ranges:
                return ranges
            idxs = [int(i) for i in (tile.get("esp32_segments") or [])]
            src_n = int(tile.get("esp32_segment_count") or self.esp32_segment_count or 8)
            return _esp32_indices_to_ranges(idxs, src_n)
        if target == "group":
            return None
        return None

    def esp32_set_selection(self, indices: list[int]) -> None:
        n = max(1, self.esp32_segment_count)
        clean = sorted({max(0, min(n - 1, int(i))) for i in (indices or [])})
        self.esp32_selected = clean
        self._emit("esp32")

    def esp32_paint_indices(
        self,
        indices: list[int],
        *,
        r: int,
        g: int,
        b: int,
        on: bool = True,
        brightness: float | None = None,
        others_off: bool = False,
        segment_count: int | None = None,
    ) -> None:
        """区間インデックス指定。保存時分割数が違っても位置比率→ピクセルで正確に塗る。"""
        src_n = max(1, int(segment_count or self.esp32_segment_count or 8))
        ranges = _esp32_indices_to_ranges(list(indices or []), src_n)
        self.esp32_paint_ranges(
            ranges,
            r=r,
            g=g,
            b=b,
            on=on,
            brightness=brightness,
            others_off=others_off,
        )

    def esp32_paint_ranges(
        self,
        ranges: list[list[float]] | list[tuple[float, float]],
        *,
        r: int,
        g: int,
        b: int,
        on: bool = True,
        brightness: float | None = None,
        others_off: bool = False,
    ) -> None:
        """正規化範囲をピクセル単位で正確に点灯（現在の分割数に依存しない）。"""
        if not self.esp32_segments:
            self._esp32_init_segments(self.esp32_segment_count, keep_colors=False)
        if self.esp32_pattern_running:
            self.esp32_pattern_stop()
        if brightness is not None:
            self._esp32_brightness = max(0.0, min(1.0, float(brightness)))
        rr = max(0, min(255, int(r)))
        gg = max(0, min(255, int(g)))
        bb = max(0, min(255, int(b)))
        self._esp32_color = (rr, gg, bb)
        lit = bool(on) and not (rr == 0 and gg == 0 and bb == 0)
        bri = max(0.0, min(1.0, float(self._esp32_brightness)))
        col = (
            max(0, min(255, int(round(rr * bri)))),
            max(0, min(255, int(round(gg * bri)))),
            max(0, min(255, int(round(bb * bri)))),
        ) if lit else (0, 0, 0)

        n_pix = max(1, int(self.esp32_pixel_count))
        spans = _esp32_ranges_to_pixel_spans(ranges, n_pix)
        allowed = _esp32_pixel_spans_to_set(spans, n_pix)

        if others_off:
            pixels = [(0, 0, 0)] * n_pix
        else:
            pixels = list(self._esp32_pixels_from_segments())
            if len(pixels) < n_pix:
                pixels.extend([(0, 0, 0)] * (n_pix - len(pixels)))
            elif len(pixels) > n_pix:
                pixels = pixels[:n_pix]

        for i in allowed:
            pixels[i] = col

        # UI 区間表示は近似（重なり区間）に同期
        approx = _esp32_ranges_to_indices(ranges, self.esp32_segment_count)
        approx_set = set(approx)
        if others_off:
            for i, seg in enumerate(self.esp32_segments):
                if i in approx_set:
                    seg["r"], seg["g"], seg["b"] = rr, gg, bb
                    seg["on"] = lit
                else:
                    seg["on"] = False
        else:
            for i in approx_set:
                if 0 <= i < len(self.esp32_segments):
                    seg = self.esp32_segments[i]
                    seg["r"], seg["g"], seg["b"] = rr, gg, bb
                    seg["on"] = lit

        if not self.esp32_enabled:
            raise RuntimeError("ESP32 送信が無効です。先に有効化してください")
        self._artnet.send_rgb_buffer(pixels, brightness=1.0)
        if self._artnet.last_error:
            self.esp32_status = f"区間送信エラー: {self._artnet.last_error}"
        else:
            self.esp32_status = (
                f"位置固定送信 {self.esp32_host} "
                f"{len(spans)}範囲 / {len(allowed)}px"
            )
        self._emit("esp32")

    def esp32_paint_pixel_spans(
        self,
        spans: list[list[int]] | list[tuple[int, int]],
        *,
        r: int,
        g: int,
        b: int,
        on: bool = True,
        brightness: float | None = None,
        others_off: bool = False,
    ) -> None:
        """絶対ピクセル範囲で点灯（分割数変換なし）。"""
        if not self.esp32_segments:
            self._esp32_init_segments(self.esp32_segment_count, keep_colors=False)
        if self.esp32_pattern_running:
            self.esp32_pattern_stop()
        if brightness is not None:
            self._esp32_brightness = max(0.0, min(1.0, float(brightness)))
        rr = max(0, min(255, int(r)))
        gg = max(0, min(255, int(g)))
        bb = max(0, min(255, int(b)))
        self._esp32_color = (rr, gg, bb)
        lit = bool(on) and not (rr == 0 and gg == 0 and bb == 0)
        bri = max(0.0, min(1.0, float(self._esp32_brightness)))
        col = (
            max(0, min(255, int(round(rr * bri)))),
            max(0, min(255, int(round(gg * bri)))),
            max(0, min(255, int(round(bb * bri)))),
        ) if lit else (0, 0, 0)

        n_pix = max(1, int(self.esp32_pixel_count))
        norm = _esp32_normalize_pixel_spans(spans, n_pix)
        allowed = _esp32_pixel_spans_to_set(
            [(s, e) for s, e in norm], n_pix
        )

        if others_off:
            pixels = [(0, 0, 0)] * n_pix
        else:
            pixels = list(self._esp32_pixels_from_segments())
            if len(pixels) < n_pix:
                pixels.extend([(0, 0, 0)] * (n_pix - len(pixels)))
            elif len(pixels) > n_pix:
                pixels = pixels[:n_pix]

        for i in allowed:
            pixels[i] = col

        approx = _esp32_spans_overlap_indices(
            norm, self.esp32_segment_count, n_pix
        )
        approx_set = set(approx)
        if others_off:
            for i, seg in enumerate(self.esp32_segments):
                if i in approx_set:
                    seg["r"], seg["g"], seg["b"] = rr, gg, bb
                    seg["on"] = lit
                else:
                    seg["on"] = False
        else:
            for i in approx_set:
                if 0 <= i < len(self.esp32_segments):
                    seg = self.esp32_segments[i]
                    seg["r"], seg["g"], seg["b"] = rr, gg, bb
                    seg["on"] = lit

        if not self.esp32_enabled:
            raise RuntimeError("ESP32 送信が無効です。先に有効化してください")
        self._artnet.send_rgb_buffer(pixels, brightness=1.0)
        if self._artnet.last_error:
            self.esp32_status = f"ピクセル送信エラー: {self._artnet.last_error}"
        else:
            self.esp32_status = (
                f"ピクセル固定送信 {self.esp32_host} "
                f"{len(norm)}範囲 / {len(allowed)}px"
            )
        self._emit("esp32")

    def esp32_group_upsert(
        self,
        *,
        name: str,
        indices: list[int] | None = None,
        spans: list[list[int]] | None = None,
        group_id: str | None = None,
        segment_count: int | None = None,
        pixel_count: int | None = None,
    ) -> dict[str, Any]:
        pc = max(
            1,
            min(2040, int(pixel_count or self.esp32_pixel_count or 720)),
        )
        n = max(1, min(64, int(segment_count or self.esp32_segment_count or 8)))
        if spans:
            norm_spans = _esp32_normalize_pixel_spans(spans, pc)
        else:
            clean = sorted({max(0, min(n - 1, int(i))) for i in (indices or [])})
            if not clean:
                raise RuntimeError("グループにする区間を選択してください")
            # 保存時点の分割→ピクセルへ固定。以後分割が変わっても変換しない
            norm_spans = _esp32_indices_to_pixel_spans(clean, n, pc)
        if not norm_spans:
            raise RuntimeError("グループにするピクセル範囲が空です")
        gid = (group_id or "").strip() or uuid.uuid4().hex[:10]
        label = (name or "").strip() or f"グループ{len(self.esp32_groups) + 1}"
        payload = {
            "id": gid[:64],
            "name": label[:40],
            "spans": norm_spans,
            "pixel_count": pc,
            "pixel_total": sum(e - s for s, e in norm_spans),
        }
        found = False
        for i, g in enumerate(self.esp32_groups):
            if g.get("id") == gid:
                self.esp32_groups[i] = payload
                found = True
                break
        if not found:
            self.esp32_groups.append(payload)
        self._save_esp32_groups()
        self._emit("esp32")
        return self._esp32_group_public(payload)

    def esp32_group_delete(self, group_id: str) -> None:
        gid = (group_id or "").strip()
        self.esp32_groups = [g for g in self.esp32_groups if g.get("id") != gid]
        self._save_esp32_groups()
        self._emit("esp32")

    def _esp32_resolve_tile_indices(self, tile: dict[str, Any]) -> list[int] | None:
        """None = 全体。list = 現在の分割数での対象区間。"""
        target = str(tile.get("esp32_target") or "all").lower()
        cur_n = max(1, int(self.esp32_segment_count or 8))
        if target == "segments":
            ranges = tile.get("esp32_ranges") or []
            if ranges:
                return _esp32_ranges_to_indices(ranges, cur_n)
            src_n = tile.get("esp32_segment_count")
            idxs = [int(i) for i in (tile.get("esp32_segments") or [])]
            if src_n:
                return _esp32_remap_indices(idxs, int(src_n), cur_n)
            # 古いタイル: インデックスを現在の分割にクリップ
            return sorted({max(0, min(cur_n - 1, i)) for i in idxs})
        if target == "group":
            return self._esp32_resolve_group_indices(str(tile.get("esp32_group_id") or ""))
        return None

    def _esp32_tile_pixel_set(self, tile: dict[str, Any]) -> set[int]:
        n = max(1, int(self.esp32_pixel_count))
        target = str(tile.get("esp32_target") or "all").lower()
        if target == "group":
            spans = self._esp32_resolve_group_spans(
                str(tile.get("esp32_group_id") or "")
            )
            return _esp32_pixel_spans_to_set(
                [(s, e) for s, e in spans], n
            )
        ranges = self._esp32_resolve_tile_ranges(tile)
        if ranges is None:
            return set(range(n))
        spans = _esp32_ranges_to_pixel_spans(ranges, n)
        return _esp32_pixel_spans_to_set(spans, n)

    def _esp32_layer_color_at(
        self, tile: dict[str, Any], pixel_i: int, t: float
    ) -> tuple[int, int, int]:
        bri = max(0.0, min(1.0, float(tile.get("led_brightness", 100.0)) / 100.0))
        fx = str(tile.get("esp32_fx") or "color").lower()
        if fx == "pattern":
            pid = str(tile.get("esp32_pattern_id") or "").strip()
            if pid:
                try:
                    frames = render_pattern(
                        pid,
                        self.esp32_pixel_count,
                        t,
                        color=(
                            int(tile.get("led_r", 255)),
                            int(tile.get("led_g", 80)),
                            int(tile.get("led_b", 40)),
                        ),
                        speed=float(tile.get("esp32_pattern_speed", 1.0) or 1.0),
                        brightness=bri,
                    )
                    if 0 <= pixel_i < len(frames):
                        return frames[pixel_i]
                except Exception:
                    pass
        rr = max(0, min(255, int(round(int(tile.get("led_r", 255)) * bri))))
        gg = max(0, min(255, int(round(int(tile.get("led_g", 80)) * bri))))
        bb = max(0, min(255, int(round(int(tile.get("led_b", 40)) * bri))))
        return (rr, gg, bb)

    def _esp32_compose_pixels(
        self, tiles: list[dict[str, Any]], t: float
    ) -> list[tuple[int, int, int]]:
        """複数タイルを合成。同一ピクセルが複数に属する連続区間は等分して両方点灯。"""
        n = max(1, int(self.esp32_pixel_count))
        layers: list[tuple[dict[str, Any], set[int]]] = []
        for tile in tiles:
            layers.append((tile, self._esp32_tile_pixel_set(tile)))
        owners: list[tuple[int, ...]] = [tuple() for _ in range(n)]
        for li, (_tile, pset) in enumerate(layers):
            for p in pset:
                if 0 <= p < n:
                    owners[p] = owners[p] + (li,)
        out: list[tuple[int, int, int]] = [(0, 0, 0)] * n
        i = 0
        while i < n:
            key = owners[i]
            j = i + 1
            while j < n and owners[j] == key:
                j += 1
            if key:
                m = len(key)
                span = j - i
                for k, li in enumerate(key):
                    a = i + (span * k) // m
                    b = i + (span * (k + 1)) // m
                    tile = layers[li][0]
                    for p in range(a, b):
                        out[p] = self._esp32_layer_color_at(tile, p, t)
            i = j
        return out

    def _esp32_compose_push(self, tiles: list[dict[str, Any]], t: float) -> None:
        if not self.esp32_enabled or not self.esp32_host or not tiles:
            return
        try:
            pixels = self._esp32_compose_pixels(tiles, t)
            self._artnet.send_rgb_buffer(pixels, brightness=1.0)
            if self._artnet.last_error:
                self.esp32_status = f"合成送信エラー: {self._artnet.last_error}"
            else:
                self.esp32_status = f"合成送信 {len(tiles)}レイヤ（重なり等分）"
        except Exception as e:
            self.esp32_status = f"合成失敗: {e}"

    def _esp32_compose_stop_loop(self) -> None:
        self._esp32_compose_stop.set()
        th = self._esp32_compose_thread
        if th is not None and th.is_alive() and th is not threading.current_thread():
            th.join(timeout=0.5)
        self._esp32_compose_thread = None

    def _esp32_compose_start_loop(self, tiles: list[dict[str, Any]]) -> None:
        self._esp32_compose_stop_loop()
        self._esp32_compose_stop.clear()
        self._esp32_compose_t0 = time.monotonic()
        snap = [dict(t) for t in tiles]

        def _loop() -> None:
            fps = 30.0
            dt = 1.0 / fps
            while not self._esp32_compose_stop.is_set() and self.esp32_enabled:
                # 最新ホールドを参照
                with self._lock:
                    order = [s for s in self.hold_order if s in self.holds]
                    cur = [
                        self.holds[s]
                        for s in order
                        if self.holds[s].get("apply_esp32", False)
                    ]
                if len(cur) < 2:
                    break
                t = time.monotonic() - self._esp32_compose_t0
                self._esp32_compose_push(cur, t)
                if self._esp32_compose_stop.wait(dt):
                    break

        self._esp32_compose_thread = threading.Thread(target=_loop, daemon=True)
        self._esp32_compose_thread.start()
        # 初回即時
        self._esp32_compose_push(snap, 0.0)

    def _esp32_cue_time_overlap(self, a: PunchCue, b: PunchCue) -> bool:
        a0, a1 = float(a.t), float(a.t) + max(0.02, float(a.hold_sec))
        b0, b1 = float(b.t), float(b.t) + max(0.02, float(b.hold_sec))
        return a0 < b1 and b0 < a1

    def _refresh_esp32_overlap_warnings(
        self, *, new_cue: PunchCue | None = None
    ) -> dict[str, Any] | None:
        """ESP32 キューの時間×空間重なりを検出し警告一覧を更新。"""
        cues = [
            c
            for c in self.cues
            if isinstance(c.tile, dict) and c.tile.get("apply_esp32", False)
        ]
        warnings: list[dict[str, Any]] = []
        alert: dict[str, Any] | None = None
        for i in range(len(cues)):
            for j in range(i + 1, len(cues)):
                a, b = cues[i], cues[j]
                if not self._esp32_cue_time_overlap(a, b):
                    continue
                pa = self._esp32_tile_pixel_set(a.tile)
                pb = self._esp32_tile_pixel_set(b.tile)
                inter = pa & pb
                if not inter:
                    continue
                item = {
                    "cue_a": a.cue_id,
                    "cue_b": b.cue_id,
                    "title_a": str(a.tile.get("title") or a.cue_id),
                    "title_b": str(b.tile.get("title") or b.cue_id),
                    "t_a": float(a.t),
                    "t_b": float(b.t),
                    "hold_a": float(a.hold_sec),
                    "hold_b": float(b.hold_sec),
                    "overlap_pixels": len(inter),
                    "message": (
                        f"「{a.tile.get('title', '?')}」と「{b.tile.get('title', '?')}」が"
                        f"時間・位置で重なっています（{len(inter)}px）。"
                        "同時点灯時は重なり区間を半分ずつ点灯します。"
                    ),
                }
                warnings.append(item)
                if new_cue is not None and (
                    new_cue.cue_id == a.cue_id or new_cue.cue_id == b.cue_id
                ):
                    alert = item
        self.esp32_overlap_warnings = warnings
        return alert

    def _esp32_apply_tile(self, tile: dict[str, Any]) -> None:
        if not self.esp32_enabled or not self.esp32_host:
            return
        bri = float(tile.get("led_brightness", 100.0)) / 100.0
        r = int(tile.get("led_r", 255))
        g = int(tile.get("led_g", 80))
        b = int(tile.get("led_b", 40))
        others_off = bool(tile.get("esp32_others_off", True))
        target = str(tile.get("esp32_target") or "all").lower()
        group_spans = (
            self._esp32_resolve_group_spans(str(tile.get("esp32_group_id") or ""))
            if target == "group"
            else None
        )
        ranges = None if group_spans is not None else self._esp32_resolve_tile_ranges(tile)
        fx = str(tile.get("esp32_fx") or "color").lower()
        if fx == "pattern":
            pid = str(tile.get("esp32_pattern_id") or "").strip()
            if not pid:
                fx = "color"
            else:
                try:
                    self.esp32_pattern_start(
                        pid,
                        speed=float(tile.get("esp32_pattern_speed", 1.0) or 1.0),
                        brightness=bri,
                        r=r,
                        g=g,
                        b=b,
                        ranges=None if group_spans is not None else ranges,
                        pixel_spans=group_spans,
                        others_off=others_off,
                        ignore_holds=True,
                    )
                except Exception as e:
                    self.esp32_status = f"タイルパターン失敗: {e}"
                return
        # color — グループは絶対ピクセル、区間は正規化範囲、なければ全体
        if group_spans is not None:
            if not group_spans:
                self._esp32_override({"r": 0, "g": 0, "b": 0, "brightness": 0.0})
                return
            try:
                self.esp32_paint_pixel_spans(
                    group_spans,
                    r=r,
                    g=g,
                    b=b,
                    on=True,
                    brightness=bri,
                    others_off=others_off,
                )
            except Exception as e:
                self.esp32_status = f"タイルグループ失敗: {e}"
        elif ranges is None:
            self._esp32_override({"r": r, "g": g, "b": b, "brightness": bri})
        else:
            try:
                self.esp32_paint_ranges(
                    ranges,
                    r=r,
                    g=g,
                    b=b,
                    on=True,
                    brightness=bri,
                    others_off=others_off,
                )
            except Exception as e:
                self.esp32_status = f"タイル区間失敗: {e}"

    def esp32_probe(self) -> dict[str, Any]:
        """PixelCtl HTTP /api/status を叩いて存在確認（任意）。"""
        host = self.esp32_host.strip()
        if not host:
            raise RuntimeError("ホスト未設定")
        import urllib.request

        url = f"http://{host}/api/status"
        try:
            with urllib.request.urlopen(url, timeout=2.5) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
            self.esp32_status = f"応答あり {host}"
            self._emit("esp32")
            return {"ok": True, "body": raw[:2000]}
        except Exception as e:
            self.esp32_status = f"HTTP 未応答（Art-Net は使える場合あり）: {e}"
            self._emit("esp32")
            return {"ok": False, "error": str(e)}

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
                    # Python UI と同じ: ほとんどのモーションは内蔵色ローテを捨て、
                    # パレット／CH9（club_dot_ch9）を優先する
                    cur_name = ""
                    try:
                        if 0 <= self.motion_index < len(DOT_POINT_MOTIONS):
                            cur_name = DOT_POINT_MOTIONS[self.motion_index][0]
                    except Exception:
                        cur_name = ""
                    if ch9 is not None and not _motion_name_drives_ch9(cur_name):
                        ch9 = None
                    if ch6 is not None:
                        ch[5] = max(0, min(255, int(ch6)))
                    if ch7 is not None:
                        ch[6] = max(0, min(255, int(ch7)))
                    if ch9 is not None:
                        ch[8] = max(0, min(255, int(ch9)))
                        self.club_dot_ch9 = ch[8]
                    else:
                        # 手動色を毎フレーム維持（モーション内蔵ローテで上書きしない）
                        ch[8] = max(0, min(255, int(self.club_dot_ch9)))
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
        # ESP32 有効時は同じ色をミラー
        if self.esp32_enabled:
            self._esp32_override({"r": r, "g": g, "b": b, "brightness": brightness})

    def _on_ai_lighting_frame(self, frame: Any) -> None:
        if not self.ai_active:
            return
        # ポン出しホールド中は LED を触らない
        if self.hold_order:
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
            r = int(getattr(frame, "r", 0))
            g = int(getattr(frame, "g", 0))
            b = int(getattr(frame, "b", 0))
            if self.ble_connected and self._ble is not None and self._ble_worker is not None:
                self._ble_worker.submit(self._ble.set_color(r, g, b, bright))
            if self.esp32_enabled:
                self._esp32_override({"r": r, "g": g, "b": b, "brightness": bright})
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
        if not ids:
            return
        before = len(self.cues)
        self.cues = [c for c in self.cues if c.cue_id not in ids]
        if len(self.cues) != before:
            self._commit_cue_history()
            self.esp32_last_overlap_alert = None
            self._refresh_esp32_overlap_warnings()
        self._emit("cues")
        self._emit("esp32")

    def clear_cues(self) -> None:
        if not self.cues:
            self._emit("cues")
            return
        self.cues.clear()
        self._commit_cue_history()
        self.esp32_overlap_warnings = []
        self.esp32_last_overlap_alert = None
        self._emit("cues")
        self._emit("esp32")

    def _cues_to_snap(self) -> list[dict[str, Any]]:
        return [
            {
                "t": float(c.t),
                "hold_sec": float(c.hold_sec),
                "recorded_at": str(c.recorded_at),
                "cue_id": str(c.cue_id),
                "tile": dict(c.tile),
            }
            for c in self.cues
        ]

    def _cues_from_snap(self, snap: list[dict[str, Any]]) -> list[PunchCue]:
        out: list[PunchCue] = []
        for item in snap or []:
            if not isinstance(item, dict) or not isinstance(item.get("tile"), dict):
                continue
            try:
                out.append(
                    PunchCue(
                        t=float(item.get("t", 0.0)),
                        hold_sec=float(item.get("hold_sec", 0.05)),
                        recorded_at=str(item.get("recorded_at") or ""),
                        cue_id=str(item.get("cue_id") or f"hist-{len(out)}"),
                        tile=dict(item["tile"]),
                    )
                )
            except (TypeError, ValueError):
                continue
        out.sort(key=lambda c: (c.t, c.cue_id))
        return out

    def _reset_cue_history(self) -> None:
        self._cue_hist = [self._cues_to_snap()]
        self._cue_hist_i = 0

    def _commit_cue_history(self) -> None:
        """キュー変更後に履歴へ確定（先送り分岐は破棄）。"""
        snap = self._cues_to_snap()
        self._cue_hist = self._cue_hist[: self._cue_hist_i + 1]
        # 連続同一状態は積まない
        if self._cue_hist and len(self._cue_hist[-1]) == len(snap):
            same = True
            for a, b in zip(self._cue_hist[-1], snap):
                if a.get("cue_id") != b.get("cue_id") or abs(float(a.get("t", 0)) - float(b.get("t", 0))) > 1e-9:
                    same = False
                    break
            if same:
                return
        self._cue_hist.append(snap)
        max_n = 80
        if len(self._cue_hist) > max_n:
            overflow = len(self._cue_hist) - max_n
            self._cue_hist = self._cue_hist[overflow:]
        self._cue_hist_i = len(self._cue_hist) - 1

    def undo_cues(self) -> bool:
        if self._cue_hist_i <= 0:
            return False
        self._cue_hist_i -= 1
        self.cues = self._cues_from_snap(self._cue_hist[self._cue_hist_i])
        self.esp32_last_overlap_alert = None
        self._refresh_esp32_overlap_warnings()
        self._emit("cues")
        self._emit("esp32")
        return True

    def redo_cues(self) -> bool:
        if self._cue_hist_i >= len(self._cue_hist) - 1:
            return False
        self._cue_hist_i += 1
        self.cues = self._cues_from_snap(self._cue_hist[self._cue_hist_i])
        self.esp32_last_overlap_alert = None
        self._refresh_esp32_overlap_warnings()
        self._emit("cues")
        self._emit("esp32")
        return True

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
        self._reset_cue_history()
        self.esp32_last_overlap_alert = None
        self._refresh_esp32_overlap_warnings()
        try:
            self.zoom = max(1.0, min(64.0, float(raw.get("zoom", 1.0))))
            self.view_start = max(0.0, float(raw.get("view_start", 0.0)))
        except (TypeError, ValueError):
            pass
        self._emit("timeline")
        self._emit("cues")
        self._emit("esp32")

    def shutdown(self) -> None:
        self._esp32_compose_stop_loop()
        self.esp32_pattern_stop()
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
