#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Single Head RGB スキャンレーザー — DMX 制御（USB-DMX512 / Open DMX 想定）
マニュアル manual.html の 10CH 定義に準拠。
※ 当該マッピングにディマー（輝度）専用CHはない。
"""

from __future__ import annotations

import argparse
import json
import math
import random
import re
import shutil
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
import sys

import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, simpledialog, ttk

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

_BLE_APP = _ROOT / "BluetoothLED" / "app"
if str(_BLE_APP) not in sys.path:
    sys.path.insert(0, str(_BLE_APP))

try:
    from color_picker import ColorPalette
except ImportError:  # pragma: no cover
    ColorPalette = None  # type: ignore[misc, assignment]

try:
    from app_settings import load_settings, update_settings
except ImportError:

    def load_settings():  # type: ignore[misc, no-redef]
        return {
            "dmx_com_port": "",
            "dmx_base_addr": 1,
            "dmx_auto_connect": False,
        }

    def update_settings(**kwargs):  # type: ignore[misc, no-redef]
        return kwargs


try:
    import serial
    import serial.tools.list_ports
except ImportError as e:
    raise SystemExit("pyserial が必要です: pip install -r requirements.txt") from e

try:
    from music_precalc import (
        PRECALC_AVAILABLE,
        TrackProfile,
        analyze_audio_file,
        ensure_audio_buffer,
        load_audio_for_playback,
        load_profile_from_json,
        snapshot_at_time,
    )
except ImportError:
    PRECALC_AVAILABLE = False  # type: ignore[misc, assignment]
    TrackProfile = None  # type: ignore[misc, assignment]
    analyze_audio_file = None  # type: ignore[misc, assignment]
    load_profile_from_json = None  # type: ignore[misc, assignment]
    ensure_audio_buffer = None  # type: ignore[misc, assignment]
    load_audio_for_playback = None  # type: ignore[misc, assignment]

    def snapshot_at_time(_p, _t):  # type: ignore[no-redef]
        raise RuntimeError("music_precalc がありません")


try:
    import sounddevice as sd
except ImportError:
    sd = None  # type: ignore[assignment]

try:
    from audio_reactive import (
        ClubAudioAnalyzer,
        ch9_from_snapshot,
        effective_wobble_hz,
        is_hype_energy,
        is_triplet_synth_pulse,
        motion_speed_from_snapshot,
        pick_hype_motion_index,
        pick_impact_motion_index,
        pick_laser_pew_motion_index,
        pick_motion_index,
        pick_shake_horizontal_index,
        synth_pulse_side,
    )

    _AUDIO_REACTIVE_AVAILABLE = True
except ImportError:
    ClubAudioAnalyzer = None  # type: ignore[misc, assignment]

    def effective_wobble_hz(s):  # type: ignore[no-redef]
        return float(getattr(s, "envelope_wobble_hz", 0.0))

    def is_triplet_synth_pulse(_s):  # type: ignore[no-redef]
        return False

    def is_hype_energy(_s):  # type: ignore[no-redef]
        return False

    def synth_pulse_side(_s):  # type: ignore[no-redef]
        return 0

    def ch9_from_snapshot(_s):  # type: ignore[no-redef]
        return 12

    def motion_speed_from_snapshot(_s, *, sens=1.0):  # type: ignore[no-redef]
        return max(0.25, min(16.0, 1.0 * sens))

    def pick_motion_index(_s, *, rng=None):  # type: ignore[no-redef]
        return 0

    def pick_impact_motion_index(_s, *, rng=None):  # type: ignore[no-redef]
        return 0

    def pick_hype_motion_index(_s, *, rng=None):  # type: ignore[no-redef]
        return 0

    def pick_laser_pew_motion_index(_s, *, rng=None):  # type: ignore[no-redef]
        return 0

    def pick_shake_horizontal_index(_s, *, rng=None):  # type: ignore[no-redef]
        return 0

    _AUDIO_REACTIVE_AVAILABLE = False


DMX_CHANNELS = 512
OPEN_DMX_BAUD = 250_000
# 目標フレームレート（DMXは ~44Hz が一般的）
TARGET_FPS = 40

# 点モーション「速度」スライダー範囲（UI と _run_motion_tick のクランプで共通）
MOTION_SPEED_MIN = 0.25
MOTION_SPEED_MAX = 48.0

# タイルに登録するキー割当で無視する keysym（単体では登録しない）
_PATTERN_HOTKEY_MODIFIER_KEYS = frozenset(
    {
        "Shift_L",
        "Shift_R",
        "Control_L",
        "Control_R",
        "Alt_L",
        "Alt_R",
        "Super_L",
        "Super_R",
        "Meta_L",
        "Meta_R",
        "Caps_Lock",
        "Num_Lock",
        "Scroll_Lock",
        "ISO_Prev_Group",
        "ISO_Next_Group",
    }
)


def _pattern_hotkey_normalize(sym: str) -> str:
    if not sym:
        return ""
    if len(sym) == 1 and sym.isascii() and sym.isalpha():
        return sym.lower()
    return sym


@dataclass
class PatternTileState:
    """パターンタイル 1 枚分（レーザー10CH＋点モーション＋任意で LED）。JSON 入出力可。"""

    title: str
    channels: tuple[int, int, int, int, int, int, int, int, int, int]
    motion_index: int
    motion_speed: float
    apply_dot_base: bool
    club_dot_ch9: int
    run_motion: bool
    hotkey: str | None = None
    # 発火時にレーザー／LED のどちらを書き込むか
    apply_laser: bool = True
    apply_led: bool = False
    led_r: int = 255
    led_g: int = 80
    led_b: int = 40
    led_brightness: float = 100.0
    # None = 固定色、int = BuiltInMode 値
    led_mode: int | None = None
    led_speed: int = 50


@dataclass
class PunchCue:
    """音楽タイムライン上のポン出しキーポイント。"""

    t: float  # 押下時の曲位置（秒）
    hold_sec: float  # 押し続けた時間（秒）
    recorded_at: str  # 記録した壁時計（ISO）
    cue_id: str
    tile: dict[str, Any]  # PatternTileState のスナップショット

    def label(self) -> str:
        title = str(self.tile.get("title", "?"))[:40]
        hk = self.tile.get("hotkey")
        hk_s = f" [{hk}]" if hk else ""
        return f"{self.t:7.2f}s ▬{self.hold_sec:5.2f}s  {title}{hk_s}"

    def display_title(self) -> str:
        return str(self.tile.get("title", "ポン出し"))[:24]

    def clip_color(self) -> str:
        """タイムライン上のクリップ色（LED色があればそれを使う）。"""
        try:
            r = int(self.tile.get("led_r", 60))
            g = int(self.tile.get("led_g", 140))
            b = int(self.tile.get("led_b", 220))
        except (TypeError, ValueError):
            r, g, b = 60, 140, 220
        # 暗すぎ／白すぎは視認用に補正
        if r + g + b < 80:
            r, g, b = 80, 120, 200
        if r + g + b > 700:
            r, g, b = min(r, 220), min(g, 220), min(b, 220)
        return f"#{r:02x}{g:02x}{b:02x}"


_LED_COLOR_PRESETS: list[tuple[str, tuple[int, int, int]]] = [
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


# ソフト制御のカスタム LED エフェクト（負の mode id。機材 BuiltInMode と衝突しない）
LED_FX_FLAME = -101  # 最暗 → 滑らかに最大輝度 → 保持（速度は UI バー）

_LED_CUSTOM_FX_CATALOG: list[tuple[str, int]] = [
    ("炎（ぱっと点灯）", LED_FX_FLAME),
]


@dataclass(frozen=True)
class LedFxAsset:
    """LED ポン出し用の光型アセット。

    mode is None → 固定色（rgb で色をセット）
    mode あり → エフェクト（色はセットしない。UI 下段のパレットで決める）
    """

    title: str
    mode: int | None  # None = 固定色 RGB
    speed: int = 50
    brightness: float = 100.0
    rgb: tuple[int, int, int] | None = None


def _led_builtin_mode_ids() -> dict[str, int]:
    """protocol.BuiltInMode 名 → 値。読込失敗時は空。"""
    try:
        ble_proto = Path(__file__).resolve().parents[1] / "BluetoothLED" / "app"
        if str(ble_proto) not in sys.path:
            sys.path.insert(0, str(ble_proto))
        from protocol import BuiltInMode  # type: ignore

        return {m.name: int(m) for m in BuiltInMode}
    except Exception:
        # フォールバック（Triones 系と一致）
        return {
            "SEVEN_COLOR_CROSS_FADE": 0x25,
            "RED_GRADUAL": 0x26,
            "GREEN_GRADUAL": 0x27,
            "BLUE_GRADUAL": 0x28,
            "YELLOW_GRADUAL": 0x29,
            "CYAN_GRADUAL": 0x2A,
            "PURPLE_GRADUAL": 0x2B,
            "WHITE_GRADUAL": 0x2C,
            "RED_GREEN_CROSS": 0x2D,
            "RED_BLUE_CROSS": 0x2E,
            "GREEN_BLUE_CROSS": 0x2F,
            "SEVEN_COLOR_STROBE": 0x30,
            "RED_STROBE": 0x31,
            "GREEN_STROBE": 0x32,
            "BLUE_STROBE": 0x33,
            "YELLOW_STROBE": 0x34,
            "CYAN_STROBE": 0x35,
            "PURPLE_STROBE": 0x36,
            "WHITE_STROBE": 0x37,
            "SEVEN_COLOR_JUMP": 0x38,
        }


def _build_led_fx_assets() -> list[LedFxAsset]:
    """点滅・ストロボ・フェード・ジャンプ・固定色などの LED 光型カタログ。"""
    m = _led_builtin_mode_ids()

    def mid(name: str) -> int | None:
        return m.get(name)

    def fx(title: str, mode_name: str, brightness: float = 100.0) -> LedFxAsset:
        # エフェクトは色なし（rgb=None）。速度は UI バーで指定（アセットに埋め込まない）。
        return LedFxAsset(title, mid(mode_name), 50, brightness, None)

    return [
        # --- 固定色（キメ／ベース）※これだけ色をセット ---
        LedFxAsset("LED・白キメ", None, 50, 100.0, (255, 255, 255)),
        LedFxAsset("LED・赤キメ", None, 50, 100.0, (255, 0, 0)),
        LedFxAsset("LED・青キメ", None, 50, 100.0, (0, 40, 255)),
        LedFxAsset("LED・緑キメ", None, 50, 100.0, (0, 255, 40)),
        LedFxAsset("LED・紫キメ", None, 50, 100.0, (180, 0, 255)),
        LedFxAsset("LED・暖白キメ", None, 50, 100.0, (255, 180, 100)),
        LedFxAsset("LED・シアンキメ", None, 50, 100.0, (0, 220, 255)),
        LedFxAsset("LED・橙キメ", None, 50, 100.0, (255, 120, 20)),
        LedFxAsset("LED・ピンクキメ", None, 50, 100.0, (255, 80, 160)),
        LedFxAsset("LED・暗め白", None, 50, 35.0, (255, 255, 255)),
        # --- ストロボ／点滅（色はセットしない。速度はバー） ---
        fx("LED・ストロボ点滅", "WHITE_STROBE"),
        fx("LED・赤ストロボ", "RED_STROBE"),
        fx("LED・緑ストロボ", "GREEN_STROBE"),
        fx("LED・青ストロボ", "BLUE_STROBE"),
        fx("LED・黄ストロボ", "YELLOW_STROBE"),
        fx("LED・シアンストロボ", "CYAN_STROBE"),
        fx("LED・紫ストロボ", "PURPLE_STROBE"),
        # --- 多色ストロボ／ジャンプ ---
        fx("LED・7色ストロボ", "SEVEN_COLOR_STROBE"),
        fx("LED・7色ジャンプ", "SEVEN_COLOR_JUMP"),
        # --- フェード／グラデーション ---
        fx("LED・7色フェード", "SEVEN_COLOR_CROSS_FADE"),
        fx("LED・赤フェード", "RED_GRADUAL"),
        fx("LED・緑フェード", "GREEN_GRADUAL"),
        fx("LED・青フェード", "BLUE_GRADUAL"),
        fx("LED・白フェード", "WHITE_GRADUAL"),
        fx("LED・紫フェード", "PURPLE_GRADUAL"),
        fx("LED・黄フェード", "YELLOW_GRADUAL"),
        fx("LED・シアンフェード", "CYAN_GRADUAL"),
        # --- クロスフェード ---
        fx("LED・赤緑クロス", "RED_GREEN_CROSS"),
        fx("LED・赤青クロス", "RED_BLUE_CROSS"),
        fx("LED・緑青クロス", "GREEN_BLUE_CROSS"),
        # --- ソフト炎（色は下のパレット。速度はバー） ---
        LedFxAsset("LED・炎（ぱっと点灯）", LED_FX_FLAME, 50, 100.0, None),
    ]


_LED_FX_ASSETS: list[LedFxAsset] = _build_led_fx_assets()


def _led_mode_catalog() -> list[tuple[str, int | None]]:
    """(表示名, mode_id or None=固定色)。"""
    out: list[tuple[str, int | None]] = [("固定色（RGB）", None)]
    for name, mid in _LED_CUSTOM_FX_CATALOG:
        out.append((name, mid))
    try:
        ble_proto = Path(__file__).resolve().parents[1] / "BluetoothLED" / "app"
        if str(ble_proto) not in sys.path:
            sys.path.insert(0, str(ble_proto))
        from protocol import MODE_LABELS_JA, BuiltInMode  # type: ignore

        for m in BuiltInMode:
            out.append((str(MODE_LABELS_JA.get(m, m.name)), int(m)))
    except Exception:
        for a in _LED_FX_ASSETS:
            if a.mode is not None and a.mode >= 0 and not any(x[1] == a.mode for x in out):
                out.append((a.title.replace("LED・", ""), a.mode))
    return out


def _empty_laser_channels(ch9: int = 0) -> tuple[int, int, int, int, int, int, int, int, int, int]:
    return (0, 0, 0, 0, 0, 0, 0, 0, max(0, min(255, int(ch9))), 0)


def _pattern_tile_from_led_fx(
    asset: LedFxAsset,
    *,
    rgb: tuple[int, int, int] | None = None,
) -> PatternTileState:
    """光型アセット → タイル。エフェクトの色は引数 rgb（未指定なら白）。"""
    if asset.mode is None:
        col = asset.rgb or (255, 255, 255)
    else:
        col = rgb or (255, 255, 255)
    return PatternTileState(
        title=asset.title[:80],
        channels=_empty_laser_channels(),
        motion_index=0,
        motion_speed=1.35,
        apply_dot_base=True,
        club_dot_ch9=0,
        run_motion=False,
        hotkey=None,
        apply_laser=False,
        apply_led=True,
        led_r=col[0],
        led_g=col[1],
        led_b=col[2],
        led_brightness=max(1.0, min(100.0, float(asset.brightness))),
        led_mode=asset.mode,
        led_speed=max(1, min(100, int(asset.speed))),
    )


def _pattern_tiles_json_path() -> Path:
    return Path(__file__).resolve().parent / "pattern_tiles.json"


_PATTERN_LINE_RE = re.compile(r"^(\d+)\s*-\s*(\d+)\s+(.+)$")

# pattern.txt では CH9 の色帯も「0–19 赤」のように同じ行形式のため、ここまでを CH2 図形ブロックとみなす。
_CH2_TABLE_STOP_PREFIXES = ("点にする場合", "点の場合の")


def load_ch2_patterns(pattern_path: Path | None = None) -> list[tuple[int, int, str]]:
    """pattern.txt の CH2 図形ブロックだけを読む（色の CH9 表は含めない）。"""
    path = pattern_path or Path(__file__).resolve().parent / "pattern.txt"
    out: list[tuple[int, int, str]] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("CH2"):
            continue
        if line.startswith(_CH2_TABLE_STOP_PREFIXES):
            break
        m = _PATTERN_LINE_RE.match(line)
        if not m:
            continue
        a, b = int(m.group(1)), int(m.group(2))
        name = m.group(3).strip()
        if a <= b and name:
            out.append((a, b, name))
    return out


# pattern.txt の疑似点：CH1=マニュアル / CH2=126–130 中央 / CH8=63（これ以外は触らない）
_DOT_BASE_CH1 = 100
_DOT_STEP_CH1_BLACKOUT = 0  # 点フラッシュの消灯（CH1 ブラックアウト帯）
_DOT_BASE_CH2 = 128
_DOT_BASE_CH8 = 63
# 【クラブ】床エッジ 広がる／閉じる：正規化水平の線の中心（0〜1）。実機の中央がずれるときはこの値だけ調整。
# ※ sx が大きいほど左。左に寄りすぎる場合は小さめにする。
_CLUB_LINE_CENTER_SX = 0.28
# 広がる／閉じる：時間の大半は左右端（線の見え方）。中央は左右の切り替え「間」だけ短く挟む。
_CLUB_MUX_CYCLE_SPEED = 1.12
_CLUB_MUX_ENDPOINT_FRAC = 0.37  # 左・右それぞれ（間をさらに長く）
_CLUB_MUX_MID_GAP_FRAC = 0.13  # 中央はこの割合×2（計26%）
# pattern.txt「11–15 水平線」レンジの中央（開く／閉じる・水平線 CH2 版）
_CH2_HLINE_MID = (11 + 15) // 2
# 水平線 CH8「開く／閉じる」：1 周の時間配分（ランプアップ・ピークホールド・復帰ランプ）
_HLINE_CH8_CYCLE_SPEED = 0.40
_HLINE_CH8_RAMP_FRAC = 0.38
_HLINE_CH8_HOLD_PEAK_FRAC = 0.10
# 復帰ランプ中はレーザーを消す（CH1 のブラックアウト帯）。見せる区間はマニュアルへ戻す。
_HLINE_RETURN_CH1_OFF = 0
_HLINE_VISIBLE_CH1 = _DOT_BASE_CH1

# モーション見え方補正（正規化: sx 大=左 / sy 大=上）。左上寄りなら MAX を下げ SHIFT を負に。
_MOTION_SX_SHIFT = -0.18
_MOTION_SX_OUT_MIN = 0.00
_MOTION_SX_OUT_MAX = 0.50
# 垂直レンジは UI から変更可能（初期は床寄り 0〜32%）
_MOTION_SY_OUT_MIN = 0.00
_MOTION_SY_OUT_MAX = 0.32
# 水平のみモーションで CH7 を触るときの帯内位置（0=下端寄り）
_MOTION_HORIZ_DEFAULT_SY = 0.08


def set_motion_sy_range(lo: float, hi: float) -> tuple[float, float]:
    """モーション垂直レンジ（0〜1）を設定。戻り値は正規化後の (下端, 上端)。"""
    global _MOTION_SY_OUT_MIN, _MOTION_SY_OUT_MAX
    lo = max(0.0, min(1.0, float(lo)))
    hi = max(0.0, min(1.0, float(hi)))
    if hi < lo:
        lo, hi = hi, lo
    if hi - lo < 0.02:
        hi = min(1.0, lo + 0.02)
        lo = max(0.0, hi - 0.02)
    _MOTION_SY_OUT_MIN = lo
    _MOTION_SY_OUT_MAX = hi
    return lo, hi


def _apply_sx_bias(sx: float) -> float:
    """論理 sx(0〜1) を実機向けに右寄りへ再マップする。"""
    sx = max(0.0, min(1.0, float(sx) + _MOTION_SX_SHIFT))
    return _MOTION_SX_OUT_MIN + sx * (_MOTION_SX_OUT_MAX - _MOTION_SX_OUT_MIN)


def _apply_sy_bias(sy: float) -> float:
    """論理 sy(0〜1) を UI 設定の高さ範囲へ再マップする。"""
    sy = max(0.0, min(1.0, float(sy)))
    lo = float(_MOTION_SY_OUT_MIN)
    hi = float(_MOTION_SY_OUT_MAX)
    if hi < lo:
        lo, hi = hi, lo
    return lo + sy * (hi - lo)

def _hline_cycle_in_return_phase(t: float) -> bool:
    """開く／閉じる共通：細／太へ戻す最終ランプ区間。"""
    ru = _HLINE_CH8_RAMP_FRAC
    hp = _HLINE_CH8_HOLD_PEAK_FRAC
    return t >= ru + hp


def _hline_expand_cycle_ch8(t: float) -> int:
    """開く：細(63)→太(0)→また細(63)へ戻してから次周。"""
    ru = _HLINE_CH8_RAMP_FRAC
    hp = _HLINE_CH8_HOLD_PEAK_FRAC
    if t < ru:
        return max(0, min(63, int(round(63.0 * (1.0 - t / ru)))))
    if t < ru + hp:
        return 0
    u = (t - ru - hp) / max(1e-9, (1.0 - ru - hp))
    return max(0, min(63, int(round(63.0 * u))))


def _hline_contract_cycle_ch8(t: float) -> int:
    """閉じる：太(0)→細(63)→また太(0)へ戻してから次周。"""
    ru = _HLINE_CH8_RAMP_FRAC
    hp = _HLINE_CH8_HOLD_PEAK_FRAC
    if t < ru:
        return max(0, min(63, int(round(63.0 * (t / ru)))))
    if t < ru + hp:
        return 63
    u = (t - ru - hp) / max(1e-9, (1.0 - ru - hp))
    return max(0, min(63, int(round(63.0 * (1.0 - u)))))

# クラブ向け疑似点 — 位置のみ（名前に色を混ぜない）。CH9 は別指定（`_club_dot_ch9` / パレット「色」）。
# CH3=0、CH4=CH5=0、CH10=48 固定。原点は CH6=0・CH7=0（右下）。値を上げるほど左・上へ。
def _club_dot_full_tuple(
    ch6: int,
    ch7: int,
    ch9: int,
    ch10: int = 48,
) -> tuple[int, int, int, int, int, int, int, int, int, int]:
    return (
        _DOT_BASE_CH1,
        _DOT_BASE_CH2,
        0,
        0,
        0,
        max(0, min(127, ch6)),
        max(0, min(127, ch7)),
        _DOT_BASE_CH8,
        max(0, min(255, ch9)),
        max(0, min(255, ch10)),
    )


CLUB_DOT_POSITION_PRESETS: list[tuple[str, int, int]] = [
    ("ホーム・右下原点（キック合わせ）", 0, 0),
    ("右下床キメ（サイドキック）", 26, 22),
    ("左下コーナー（ハウス）", 118, 18),
    ("左上天井シャワー（クラブアンテム）", 118, 118),
]


# 点モード時の CH9 のみ変更するとき用（pattern.txt「点の場合の9ch」準拠・代表値）
DOT_CH9_DOT_SWATCHES: list[tuple[str, int]] = [
    ("0–19 赤（代表）", 12),
    ("20–39 緑（代表）", 28),
    ("40–59 オレンジ（代表）", 48),
    ("60–79 青（代表）", 68),
    ("80–99 紫（代表）", 90),
    ("100–119 水色（代表）", 110),
    ("120–255 自動・中速目安", 155),
    ("120–255 自動・高速目安", 225),
]


def _clamp127f(v: float) -> int:
    return max(0, min(127, int(round(v))))


_MOTION_CH9_PALETTE = [12, 28, 48, 68, 90, 110, 155]


def _motion_ch9(ph: float) -> int:
    n = len(_MOTION_CH9_PALETTE)
    if n == 0:
        return 32
    span = 2 * math.pi
    idx = int(((ph % span) / span) * n) % n
    return _MOTION_CH9_PALETTE[idx]


def _motion_xy(sx: float, sy: float) -> tuple[int, int]:
    """正規化座標→CH6/CH7。原点は (0,0)＝右下（CH6・CH7 最小）。sx,sy は 0〜1 で左・上へ増える。"""
    sx = _apply_sx_bias(sx)
    sy = _apply_sy_bias(sy)
    return _clamp127f(sx * 127), _clamp127f(sy * 127)


def _norm_axis01(u: float) -> int:
    """正規化 0〜1 を位置値 0〜127 に（バイアスなし）。"""
    return _clamp127f(max(0.0, min(1.0, u)) * 127)


def _norm_sx01(u: float) -> int:
    """水平 CH6 用（右寄り補正付き）。"""
    return _clamp127f(_apply_sx_bias(u) * 127)


def _norm_sx01_shake(u: float) -> int:
    """左右激振用: 補正帯を広く使い、左右端をしっかり振り切る。"""
    side = 0.0 if float(u) < 0.5 else 1.0
    # 通常バイアスより広い水平レンジ（CH6 およそ 5〜100）
    sx = 0.04 + side * 0.78
    return _clamp127f(sx * 127)


def _norm_sx01_wide(u: float) -> int:
    """三点シェイク用: 左・中・右が分かれる広めの連続マッピング。"""
    u = max(0.0, min(1.0, float(u)))
    sx = 0.05 + u * 0.78
    return _clamp127f(sx * 127)


def _norm_sy01(u: float) -> int:
    """垂直 CH7 用（下寄り補正付き）。"""
    return _clamp127f(_apply_sy_bias(u) * 127)


def _tri01(ph: float) -> float:
    """三角波で 0→1→0 を往復（周期 ≈ π）。"""
    t = (ph / math.pi) % 2.0
    return t if t <= 1.0 else 2.0 - t


def _ch9_auto_fast(ph: float) -> int:
    """CH9 自動帯（120–255）を高速変調（マニュアル帯域）。"""
    v = int(165 + 72 * math.sin(ph * 19.1))
    return max(120, min(255, v))


def _mot_horiz_color(ph: float) -> tuple[int | None, int | None, int]:
    sx = (math.sin(ph) + 1) / 2
    # CH7 も床寄りに固定（前回値の左上残りを防ぐ）
    return _norm_sx01(sx), _norm_sy01(_MOTION_HORIZ_DEFAULT_SY), _motion_ch9(ph)


def _mot_vert_color(ph: float) -> tuple[int | None, int | None, int]:
    sy = (math.sin(ph) + 1) / 2
    return _norm_sx01(0.28), _norm_sy01(sy), _motion_ch9(ph * 1.31)


def _ramp01_once(ph: float) -> float:
    """0→1 を一回だけ進み、到達後は 1 のまま（繰り返さない）。基準周期 ≈ π。"""
    return max(0.0, min(1.0, float(ph) / math.pi))


def _mot_vert_sweep_up_hold_ch9(ph: float) -> tuple[int | None, int | None, int | None]:
    """下端から上端へすっと上昇し、上端で止まる。CH6／CH9 は手動。"""
    sy = _ramp01_once(ph)
    return _norm_sx01(0.50), _norm_sy01(sy), None


def _mot_vert_sweep_down_hold_ch9(ph: float) -> tuple[int | None, int | None, int | None]:
    """上端から下端へすっと下降し、下端で止まる。CH6／CH9 は手動。"""
    sy = 1.0 - _ramp01_once(ph)
    return _norm_sx01(0.50), _norm_sy01(sy), None


def _mot_circle_color(ph: float) -> tuple[int, int, int]:
    sx = 0.42 + 0.28 * math.cos(ph)
    sy = 0.10 + 0.16 * math.sin(ph)
    c6, c7 = _motion_xy(sx, sy)
    return c6, c7, _motion_ch9(ph)


def _mot_diag_color(ph: float) -> tuple[int, int, int]:
    u = (math.sin(ph) + 1) / 2
    # 対角も右下〜中央寄りに圧縮
    c6, c7 = _motion_xy(0.15 + u * 0.55, 0.02 + u * 0.22)
    return c6, c7, _motion_ch9(ph)


def _mot_lissajous(ph: float) -> tuple[int, int, int]:
    sx = 0.15 + 0.50 * ((math.sin(ph) + 1) / 2)
    sy = 0.02 + 0.22 * ((math.sin(2 * ph + 0.6) + 1) / 2)
    c6, c7 = _motion_xy(sx, sy)
    return c6, c7, _motion_ch9(ph * 2)


def _mot_color_only(ph: float) -> tuple[int | None, int | None, int]:
    """CH9 のみ更新。CH6/CH7 は None でスキップしスライダー操作を維持。"""
    return None, None, _motion_ch9(ph * 3)


def _mot_floor_fast(ph: float) -> tuple[int | None, int | None, int]:
    sx = (math.sin(ph * 1.65) + 1) / 2
    return _norm_sx01(sx), _norm_sy01(0.04), _motion_ch9(ph * 2.2)


def _mot_ceiling_sweep(ph: float) -> tuple[int | None, int | None, int]:
    # 「天井」でも実機上寄りを抑える
    sx = (math.sin(ph * 1.15) + 1) / 2
    return _norm_sx01(sx), _norm_sy01(0.18), _motion_ch9(ph)


def _mot_spiral(ph: float) -> tuple[int, int, int]:
    R = 0.12 + 0.16 * ((math.sin(ph * 0.4) + 1) / 2)
    sx = 0.32 + R * math.cos(ph * 1.75)
    sy = 0.08 + R * math.sin(ph * 1.75)
    c6, c7 = _motion_xy(sx, sy)
    return c6, c7, _motion_ch9(ph)


def _mot_multi_beam_swarm(ph: float) -> tuple[int, int, int]:
    """三点が横一列に並び、共有位相で左右に同調振れ（高速多重化）。"""
    bases = (0.16, 0.50, 0.84)
    idx = int(ph * 54.0) % 3
    swing = 0.145 * math.sin(ph * 9.2)
    sx = max(0.04, min(0.96, bases[idx] + swing))
    return (
        _norm_sx01_wide(sx),
        _norm_sy01(_MOTION_HORIZ_DEFAULT_SY),
        _MOTION_CH9_PALETTE[idx % len(_MOTION_CH9_PALETTE)],
    )


def _mot_three_point_lr_shake(ph: float) -> tuple[int | None, int | None, int | None]:
    """振れ専用: 同一高さの三点を左右に同調シェイク（CH9は手動）。"""
    bases = (0.14, 0.50, 0.86)
    idx = int(ph * 60.0) % 3
    # 三角波で端まで振り切る
    swing = 0.16 * (2.0 * _tri01(ph * 2.6) - 1.0)
    sx = max(0.03, min(0.97, bases[idx] + swing))
    return _norm_sx01_wide(sx), _norm_sy01(_MOTION_HORIZ_DEFAULT_SY), None


def _three_point_lr_sx(ph: float, swing: float) -> float:
    """三点多重の正規化 sx（swing は -1〜1）。戻りは _norm_sx01_wide 用 0〜1。"""
    bases = (0.14, 0.50, 0.86)
    idx = int(ph * 60.0) % 3
    amp = 0.16
    sx = bases[idx] + amp * max(-1.0, min(1.0, float(swing)))
    return max(0.03, min(0.97, sx))


def _mot_hyperscan(ph: float) -> tuple[int | None, int | None, int]:
    """全幅を三角波でほぼ無減速スキャン（縦は床寄り固定）。"""
    sx = _tri01(ph * 5.8)
    return _norm_sx01(sx), _norm_sy01(_MOTION_HORIZ_DEFAULT_SY), _motion_ch9(ph * 5.5)


def _mot_grid_strobe(ph: float) -> tuple[int, int, int]:
    """グリッド交点をバラバラに高速跳躍。"""
    cells = [
        (0.18, 0.22),
        (0.52, 0.22),
        (0.85, 0.22),
        (0.34, 0.52),
        (0.68, 0.52),
        (0.5, 0.82),
    ]
    i = int(ph * 17.0) % len(cells)
    jiggle = 0.035 * math.sin(ph * 31.0)
    sx, sy = cells[i]
    c6, c7 = _motion_xy(sx + jiggle, sy + jiggle)
    return c6, c7, _MOTION_CH9_PALETTE[(i + int(ph * 6)) % len(_MOTION_CH9_PALETTE)]


def _mot_side_battle(ph: float) -> tuple[int, int, int]:
    """左右フィールドを交互にフラッシュしつつ微細橋。"""
    left = int(ph * 10.5) % 2 == 0
    band = 0.34 + 0.06 * math.sin(ph * 24.0)
    sx = (0.5 - band) if left else (0.5 + band)
    sy = 0.38 + 0.22 * math.sin(ph * 3.1 + (0 if left else 1.7))
    c6, c7 = _motion_xy(sx, sy)
    return c6, c7, _ch9_auto_fast(ph)


def _mot_hh_step_sweep(ph: float) -> tuple[int, int, int]:
    """ヒップホップ：段階横移動＋ゆるい縦バウンス。"""
    steps = 7
    seg = int(ph * 2.8) % steps
    sx = 0.06 + (seg / max(1, steps - 1)) * 0.52
    sy = 0.02 + 0.12 * abs(math.sin(ph * 1.15))
    c6, c7 = _motion_xy(sx, sy)
    return c6, c7, _motion_ch9(ph * 1.9)


def _mot_zigzag_lightning(ph: float) -> tuple[int, int, int]:
    """高速ジグザグ＋オートカラー帯。"""
    sx = _tri01(ph * 7.2 + math.sin(ph * 0.9))
    sy = 0.14 + 0.72 * _tri01(ph * 3.4 + 1.1)
    c6, c7 = _motion_xy(sx, sy)
    return c6, c7, _ch9_auto_fast(ph * 1.3)


def _mot_dual_row_multiplex(ph: float) -> tuple[int, int, int]:
    """上下二段レールを交互多重化し水平シェイクを共有。"""
    n = 8
    idx = int(ph * 26.0) % n
    row = 0 if (idx % 2 == 0) else 1
    col = idx // 2
    sx = 0.09 + (col / 3.5) * 0.82 + 0.06 * math.sin(ph * 14.8)
    sy = 0.26 if row == 0 else 0.68
    c6, c7 = _motion_xy(sx, sy)
    return c6, c7, _MOTION_CH9_PALETTE[int(ph * 16.0) % len(_MOTION_CH9_PALETTE)]


def _mot_techno_tunnel(ph: float) -> tuple[int, int, int]:
    """急速リサージュ＋短周期色ステップ。"""
    sx = (math.sin(ph * 9.2) + 1) / 2
    sy = (math.sin(ph * 11.7 + 1.1) + 1) / 2
    c6, c7 = _motion_xy(sx, sy)
    return c6, c7, _motion_ch9(ph * 9)


def _mot_chaos_floor_auto(ph: float) -> tuple[int, int, int]:
    """床付近トライアングル走査＋CH9自動高速。"""
    sx = _tri01(ph * 4.6)
    sy = 0.12 + 0.1 * _tri01(ph * 11.0)
    c6, c7 = _motion_xy(sx, sy)
    return c6, c7, _ch9_auto_fast(ph * 2.1)


def _mot_chaos_floor_ultra_hold_ch9(ph: float) -> tuple[int | None, int | None, int | None]:
    """高速水平トライアングルのみ（CH6）。CH7・CH9 はモーションが触らずスライダー／プリセットのまま。"""
    return _norm_sx01(_tri01(ph * 10.8)), _norm_sy01(_MOTION_HORIZ_DEFAULT_SY), None


def _club_symmetric_open_close_sx(ph: float, env_phase: float) -> float:
    """開く／閉じる系共通：正規化水平 sx（CH6 に変換前）。"""
    u = (math.sin(ph * 0.30 + env_phase) + 1.0) / 2.0
    half = 0.06 + u * 0.42
    mid = _CLUB_LINE_CENTER_SX
    el = _CLUB_MUX_ENDPOINT_FRAC
    mg = _CLUB_MUX_MID_GAP_FRAC
    t = (ph * _CLUB_MUX_CYCLE_SPEED) % 1.0
    if t < el:
        return mid - half
    if t < el + mg:
        return mid
    if t < el + mg + el:
        return mid + half
    return mid


def _mot_club_symmetric_hline_hold_ch9(ph: float, env_phase: float) -> tuple[int | None, int | None, int | None]:
    """左右端に主に滞在して線状に見せ、中央は短いギャップのみ。CH8・CH9 は更新しない。"""
    return _norm_sx01(_club_symmetric_open_close_sx(ph, env_phase)), _norm_sy01(
        _MOTION_HORIZ_DEFAULT_SY
    ), None


def _mot_hline_symmetric_expand_hold_ch9(ph: float) -> tuple[int | None, int | None, int | None, int, int, None, int]:
    """CH2 水平線固定。開く→ピーク→閉じた位置へ戻す（戻し中は CH1 で消灯）、を繰り返す。"""
    t = (ph * _HLINE_CH8_CYCLE_SPEED) % 1.0
    ch1 = _HLINE_RETURN_CH1_OFF if _hline_cycle_in_return_phase(t) else _HLINE_VISIBLE_CH1
    return (
        None,
        None,
        None,
        _hline_expand_cycle_ch8(t),
        _CH2_HLINE_MID,
        None,
        ch1,
    )


def _mot_hline_symmetric_contract_hold_ch9(ph: float) -> tuple[int | None, int | None, int | None, int, int, None, int]:
    """閉じる→細い状態→開いた位置へ戻す（戻し中は CH1 で消灯）、を繰り返す。"""
    t = (ph * _HLINE_CH8_CYCLE_SPEED) % 1.0
    ch1 = _HLINE_RETURN_CH1_OFF if _hline_cycle_in_return_phase(t) else _HLINE_VISIBLE_CH1
    return (
        None,
        None,
        None,
        _hline_contract_cycle_ch8(t),
        _CH2_HLINE_MID,
        None,
        ch1,
    )


def _mot_club_floor_edge_expand_hold_ch9(ph: float) -> tuple[int | None, int | None, int | None]:
    """開く方向のエンベロープ（閉じると位相逆）。"""
    return _mot_club_symmetric_hline_hold_ch9(ph, 0.0)


def _mot_club_floor_edge_contract_hold_ch9(ph: float) -> tuple[int | None, int | None, int | None]:
    """閉じる方向のエンベロープ。"""
    return _mot_club_symmetric_hline_hold_ch9(ph, math.pi)


def _mot_floor_edge_max_lines_hyperspeed(ph: float) -> tuple[int, int, int]:
    """床帯に多数の水平ラインを時間多重化し、最高速で横トライアングル走査＋色ステップ。"""
    n_lines = 22
    idx = int(ph * 76.0) % n_lines
    sy_lo, sy_hi = 0.062, 0.30
    sy = sy_lo + (idx / max(1, n_lines - 1)) * (sy_hi - sy_lo)
    phase_off = idx * math.pi * 0.33
    sx = _tri01(ph * 16.2 + phase_off)
    c6, c7 = _motion_xy(sx, sy)
    return c6, c7, _motion_ch9(ph * 22.0)


def _mot_kick_whip(ph: float) -> tuple[int, int, int]:
    """キック風に横を鞭のように叩く（sin のべきでエッジ強調）。"""
    e = math.sin(ph * 6.8)
    sx = (math.copysign(abs(e) ** 0.35, e) + 1) / 2
    sy = 0.2 + 0.55 * ((math.sin(ph * 2.1) + 1) / 2)
    c6, c7 = _motion_xy(sx, sy)
    return c6, c7, _MOTION_CH9_PALETTE[int(ph * 20.0) % len(_MOTION_CH9_PALETTE)]


def _mot_synth_stutter_lr(ph: float) -> tuple[int | None, int | None, int | None]:
    """高いとがった電子音向け左右切替。音声連動時は実測パルス位相で上書き。"""
    e = math.sin(ph * 6.8)
    sx = 0.05 if e >= 0.0 else 0.95
    return _norm_sx01_shake(sx), _norm_sy01(_MOTION_HORIZ_DEFAULT_SY), None


def _mot_synth_stutter_triangle(ph: float) -> tuple[int | None, int | None, int | None]:
    """高いとがった電子音向け左右トライアングル。"""
    return _norm_sx01_shake(_tri01(ph * 6.8)), _norm_sy01(_MOTION_HORIZ_DEFAULT_SY), None


def _mot_synth_triplet_lr(ph: float) -> tuple[int | None, int | None, int | None]:
    """3連符風: 左・中・右を短い滞在で切替。"""
    slot = int(ph * 9.6) % 3
    sx = (0.08, 0.50, 0.92)[slot]
    return _norm_sx01_shake(sx), _norm_sy01(_MOTION_HORIZ_DEFAULT_SY), None


def _mot_synth_hard_shake_lr(ph: float) -> tuple[int | None, int | None, int | None]:
    """左右激振: 端点フル振り＋微細ジッター。"""
    side = 0.0 if math.sin(ph * 11.2) >= 0.0 else 1.0
    jit = 0.04 * math.sin(ph * 37.0)
    return _norm_sx01_shake(side + jit * (1.0 if side < 0.5 else -1.0)), _norm_sy01(
        _MOTION_HORIZ_DEFAULT_SY
    ), None


def _mot_infinity_loop(ph: float) -> tuple[int, int, int]:
    """∞字（レムニスケート風）軌道＋色。"""
    t = ph * 1.15
    sx = 0.50 + 0.38 * math.sin(t)
    sy = 0.14 + 0.18 * math.sin(t) * math.cos(t)
    c6, c7 = _motion_xy(sx, sy)
    return c6, c7, _motion_ch9(ph * 1.7)


def _mot_pendulum_arc(ph: float) -> tuple[int, int, int]:
    """振り子弧: 横は大きく、縦は弧の高さ。"""
    ang = math.sin(ph * 1.05) * 0.95
    sx = 0.50 + 0.42 * math.sin(ang)
    sy = 0.06 + 0.20 * (1.0 - abs(math.sin(ang)))
    c6, c7 = _motion_xy(sx, sy)
    return c6, c7, _motion_ch9(ph * 1.4)


def _mot_wave_ribbon(ph: float) -> tuple[int, int, int]:
    """横進み＋正弦波リボン。"""
    sx = _tri01(ph * 1.35)
    sy = 0.08 + 0.22 * ((math.sin(ph * 4.2 + sx * 6.0) + 1) / 2)
    c6, c7 = _motion_xy(sx, sy)
    return c6, c7, _motion_ch9(ph * 2.4)


def _mot_diamond_orbit(ph: float) -> tuple[int, int, int]:
    """菱形軌道を周回。"""
    t = (ph / math.pi) % 4.0
    if t < 1.0:
        sx, sy = 0.50 + 0.35 * t, 0.18 + 0.18 * t
    elif t < 2.0:
        u = t - 1.0
        sx, sy = 0.85 - 0.35 * u, 0.36 - 0.18 * u
    elif t < 3.0:
        u = t - 2.0
        sx, sy = 0.50 - 0.35 * u, 0.18 - 0.10 * u
    else:
        u = t - 3.0
        sx, sy = 0.15 + 0.35 * u, 0.08 + 0.10 * u
    c6, c7 = _motion_xy(sx, sy)
    return c6, c7, _motion_ch9(ph * 1.8)


def _mot_corner_chase(ph: float) -> tuple[int, int, int]:
    """四隅を順番にチェイス。"""
    corners = [(0.12, 0.08), (0.88, 0.08), (0.88, 0.30), (0.12, 0.30)]
    i = int(ph * 3.4) % 4
    j = (i + 1) % 4
    u = (ph * 3.4) % 1.0
    sx = corners[i][0] + (corners[j][0] - corners[i][0]) * u
    sy = corners[i][1] + (corners[j][1] - corners[i][1]) * u
    c6, c7 = _motion_xy(sx, sy)
    return c6, c7, _MOTION_CH9_PALETTE[i % len(_MOTION_CH9_PALETTE)]


def _mot_starburst_rays(ph: float) -> tuple[int, int, int]:
    """中心から放射状に光線を多重化。"""
    n = 8
    idx = int(ph * 28.0) % n
    ang = (idx / n) * math.pi * 2.0 + ph * 0.35
    r = 0.08 + 0.28 * _tri01(ph * 2.2 + idx)
    sx = 0.48 + r * math.cos(ang)
    sy = 0.16 + r * 0.55 * math.sin(ang)
    c6, c7 = _motion_xy(sx, sy)
    return c6, c7, _MOTION_CH9_PALETTE[idx % len(_MOTION_CH9_PALETTE)]


def _mot_helix_climb(ph: float) -> tuple[int, int, int]:
    """螺旋登攀（床→やや上へ脈動）。"""
    climb = (math.sin(ph * 0.55) + 1) / 2
    sx = 0.50 + 0.34 * math.cos(ph * 2.4)
    sy = 0.04 + 0.26 * climb + 0.04 * math.sin(ph * 2.4)
    c6, c7 = _motion_xy(sx, sy)
    return c6, c7, _motion_ch9(ph * 2.1)


def _mot_cross_scan(ph: float) -> tuple[int, int, int]:
    """十字スキャン: 横→縦を交互。"""
    if int(ph * 2.2) % 2 == 0:
        sx = _tri01(ph * 3.6)
        sy = 0.16
    else:
        sx = 0.50
        sy = 0.04 + 0.28 * _tri01(ph * 3.6 + 0.5)
    c6, c7 = _motion_xy(sx, sy)
    return c6, c7, _ch9_auto_fast(ph)


def _mot_rose_petal(ph: float) -> tuple[int, int, int]:
    """バラ曲線（花弁）風。"""
    k = 3.0
    th = ph * 1.6
    r = 0.28 * abs(math.cos(k * th))
    sx = 0.50 + r * math.cos(th)
    sy = 0.14 + r * 0.7 * math.sin(th)
    c6, c7 = _motion_xy(sx, sy)
    return c6, c7, _motion_ch9(ph * 2.6)


def _mot_bounce_ball(ph: float) -> tuple[int, int, int]:
    """跳ね玉: 放物線バウンス＋横ドリフト。"""
    t = (ph * 1.8) % (2.0 * math.pi)
    u = (t / math.pi) % 2.0
    sy = 0.04 + 0.26 * abs(math.sin(math.pi * min(u, 2.0 - u)))
    sx = 0.12 + 0.70 * _tri01(ph * 0.55)
    c6, c7 = _motion_xy(sx, sy)
    return c6, c7, _motion_ch9(ph * 1.9)


def _mot_fan_sweep(ph: float) -> tuple[int, int, int]:
    """扇状スイープ: 角度が開閉しながら走査。"""
    open_amt = 0.25 + 0.55 * ((math.sin(ph * 0.7) + 1) / 2)
    ang = -open_amt + 2.0 * open_amt * _tri01(ph * 2.8)
    sx = 0.50 + 0.40 * math.sin(ang)
    sy = 0.06 + 0.22 * abs(math.cos(ang * 0.85))
    c6, c7 = _motion_xy(sx, sy)
    return c6, c7, _motion_ch9(ph * 2.0)


def _mot_dot_rain(ph: float) -> tuple[int, int, int]:
    """ドット雨: 複数列を上から下へ落とし多重化。"""
    cols = 6
    idx = int(ph * 22.0) % cols
    fall = (ph * 3.5 + idx * 0.37) % 1.0
    sx = 0.10 + (idx / max(1, cols - 1)) * 0.80
    sy = 0.30 - fall * 0.26
    c6, c7 = _motion_xy(sx, sy)
    return c6, c7, _MOTION_CH9_PALETTE[(idx + int(ph * 5)) % len(_MOTION_CH9_PALETTE)]


def _mot_mirror_twins(ph: float) -> tuple[int, int, int]:
    """左右対称ツインを高速切替で同時感。"""
    left = int(ph * 18.0) % 2 == 0
    u = (math.sin(ph * 2.2) + 1) / 2
    sx = (0.50 - 0.38 * u) if left else (0.50 + 0.38 * u)
    sy = 0.08 + 0.16 * ((math.sin(ph * 3.1) + 1) / 2)
    c6, c7 = _motion_xy(sx, sy)
    return c6, c7, _motion_ch9(ph * 3.3)


def _mot_slow_breathe_center(ph: float) -> tuple[int, int, int]:
    """中心付近のゆっくり息継ぎ（静かな区間向け）。"""
    r = 0.04 + 0.10 * ((math.sin(ph * 0.85) + 1) / 2)
    sx = 0.50 + r * math.cos(ph * 0.9)
    sy = 0.12 + r * 0.55 * math.sin(ph * 0.9)
    c6, c7 = _motion_xy(sx, sy)
    return c6, c7, _motion_ch9(ph * 0.8)


def _mot_square_orbit(ph: float) -> tuple[int, int, int]:
    """矩形周回。"""
    t = (ph / (math.pi * 0.5)) % 4.0
    lo_x, hi_x, lo_y, hi_y = 0.18, 0.82, 0.06, 0.28
    if t < 1.0:
        sx, sy = lo_x + (hi_x - lo_x) * t, lo_y
    elif t < 2.0:
        sx, sy = hi_x, lo_y + (hi_y - lo_y) * (t - 1.0)
    elif t < 3.0:
        sx, sy = hi_x - (hi_x - lo_x) * (t - 2.0), hi_y
    else:
        sx, sy = lo_x, hi_y - (hi_y - lo_y) * (t - 3.0)
    c6, c7 = _motion_xy(sx, sy)
    return c6, c7, _motion_ch9(ph * 1.5)


# --- EDM / ヒップホップ定番：CH2 図形＋位置／サイズ／回転 ---
def _ch2_mid(lo: int, hi: int) -> int:
    return (int(lo) + int(hi)) // 2


# pattern.txt 中央値
_CH2_SQUARE = _ch2_mid(0, 5)
_CH2_CIRCLE = _ch2_mid(6, 10)
_CH2_HLINE = _ch2_mid(11, 15)
_CH2_VLINE = _ch2_mid(16, 20)
_CH2_DIAG = _ch2_mid(21, 25)
_CH2_TRI = _ch2_mid(36, 40)
_CH2_DUB_HLINE = _ch2_mid(46, 50)
_CH2_DUB_VLINE = _ch2_mid(51, 55)
_CH2_NOTE = _ch2_mid(76, 80)
_CH2_STAR = _ch2_mid(101, 105)
_CH2_SINE = _ch2_mid(106, 110)
_CH2_HEART = _ch2_mid(121, 125)
_CH2_CROSS = _ch2_mid(156, 160)
_CH2_STAR5 = _ch2_mid(171, 175)
_CH2_TRI_RAYS = _ch2_mid(176, 180)
_CH2_PENT = _ch2_mid(186, 190)
_CH2_ARROW = _ch2_mid(201, 205)
_CH2_HOURGLASS = _ch2_mid(206, 210)
_CH2_SHURIKEN = _ch2_mid(211, 215)
_CH2_PROP3 = _ch2_mid(216, 220)
_CH2_DIAMOND = _ch2_mid(231, 240)
_CH2_TRIWAVE = _ch2_mid(241, 245)
_CH2_PROP4 = _ch2_mid(246, 250)


def _ch3_spin_cw(speed01: float = 0.55) -> int:
    """正回転速度帯 128–191。"""
    u = max(0.0, min(1.0, float(speed01)))
    return int(128 + u * 63)


def _ch3_spin_ccw(speed01: float = 0.55) -> int:
    """逆回転速度帯 192–255。"""
    u = max(0.0, min(1.0, float(speed01)))
    return int(192 + u * 63)


def _ch3_angle(ph: float, turns: float = 1.0) -> int:
    """静止角 0–127 を位相で回す。"""
    return int((ph * turns * 20.0) % 128)


def _ch8_zoom_pulse(ph: float, lo: int = 8, hi: int = 55) -> int:
    """CH8 サイズ脈動（小さい値ほど大きく見える機材が多い）。"""
    u = (math.sin(ph) + 1.0) / 2.0
    return max(0, min(63, int(round(hi - (hi - lo) * u))))


def _shape_pack(
    ph: float,
    *,
    ch2: int,
    sx: float,
    sy: float,
    ch8: int | None = None,
    ch3: int | None = None,
    ch9: int | None = None,
    ch1: int | None = None,
) -> tuple:
    c6 = _norm_sx01_wide(sx)
    c7 = _norm_sy01(sy)
    if ch9 is None:
        ch9 = _motion_ch9(ph * 2.2)
    # (ch6,ch7,ch9,ch8,ch2,ch4,ch1,ch3)
    return c6, c7, ch9, ch8, ch2, None, ch1, ch3


def _mot_edm_circle_tunnel(ph: float) -> tuple:
    """【EDM】円トンネル：回転＋ズーム脈動。"""
    sx = 0.50 + 0.06 * math.sin(ph * 0.7)
    sy = 0.14 + 0.05 * math.cos(ph * 0.9)
    return _shape_pack(
        ph,
        ch2=_CH2_CIRCLE,
        sx=sx,
        sy=sy,
        ch8=_ch8_zoom_pulse(ph * 1.8, 6, 48),
        ch3=_ch3_spin_cw(0.45 + 0.35 * ((math.sin(ph * 0.5) + 1) / 2)),
    )


def _mot_edm_square_spin(ph: float) -> tuple:
    """【EDM】正方形スピン＋サイズキック。"""
    kick = abs(math.sin(ph * 3.4))
    return _shape_pack(
        ph,
        ch2=_CH2_SQUARE,
        sx=0.50,
        sy=0.12 + 0.08 * kick,
        ch8=int(45 - 30 * kick),
        ch3=_ch3_spin_cw(0.65),
    )


def _mot_edm_hline_scanner(ph: float) -> tuple:
    """【EDM】水平ビーム・床スキャナ。"""
    sx = _tri01(ph * 2.8)
    return _shape_pack(
        ph,
        ch2=_CH2_HLINE,
        sx=sx,
        sy=_MOTION_HORIZ_DEFAULT_SY,
        ch8=int(30 + 25 * ((math.sin(ph * 4.0) + 1) / 2)),
        ch3=0,
        ch9=_ch9_auto_fast(ph),
    )


def _mot_edm_vline_gate(ph: float) -> tuple:
    """【EDM】垂直ゲート左右往復。"""
    sx = 0.18 + 0.64 * _tri01(ph * 2.2)
    return _shape_pack(
        ph,
        ch2=_CH2_VLINE,
        sx=sx,
        sy=0.16,
        ch8=_ch8_zoom_pulse(ph * 2.5, 12, 50),
        ch3=0,
    )


def _mot_edm_triangle_drop(ph: float) -> tuple:
    """【EDM】三角形ドロップ脈動。"""
    punch = abs(math.sin(ph * 4.2)) ** 0.5
    return _shape_pack(
        ph,
        ch2=_CH2_TRI,
        sx=0.50,
        sy=0.08 + 0.18 * punch,
        ch8=int(50 - 38 * punch),
        ch3=_ch3_angle(ph, 0.8),
    )


def _mot_edm_star_strobe(ph: float) -> tuple:
    """【EDM】星ストロボ（点滅＋回転）。"""
    on = (int(ph * 14.0) % 2) == 0
    return _shape_pack(
        ph,
        ch2=_CH2_STAR if (int(ph * 7.0) % 2 == 0) else _CH2_STAR5,
        sx=0.50,
        sy=0.16,
        ch8=10 if on else 55,
        ch3=_ch3_spin_cw(0.8),
        ch1=_DOT_BASE_CH1 if on else _DOT_STEP_CH1_BLACKOUT,
        ch9=_MOTION_CH9_PALETTE[int(ph * 11) % len(_MOTION_CH9_PALETTE)],
    )


def _mot_edm_cross_spin(ph: float) -> tuple:
    """【EDM】十字／X スピン。"""
    return _shape_pack(
        ph,
        ch2=_CH2_CROSS,
        sx=0.50 + 0.08 * math.sin(ph * 1.3),
        sy=0.14,
        ch8=_ch8_zoom_pulse(ph * 2.0, 10, 42),
        ch3=_ch3_spin_ccw(0.7),
    )


def _mot_edm_sine_ribbon(ph: float) -> tuple:
    """【EDM】正弦波リボン横走。"""
    return _shape_pack(
        ph,
        ch2=_CH2_SINE,
        sx=_tri01(ph * 1.6),
        sy=0.10 + 0.12 * ((math.sin(ph * 2.4) + 1) / 2),
        ch8=28,
        ch3=_ch3_angle(ph, 0.4),
    )


def _mot_edm_diamond_tunnel(ph: float) -> tuple:
    """【EDM】ダイヤ・トンネルズーム。"""
    return _shape_pack(
        ph,
        ch2=_CH2_DIAMOND,
        sx=0.50,
        sy=0.14,
        ch8=_ch8_zoom_pulse(ph * 2.6, 4, 52),
        ch3=_ch3_spin_cw(0.55),
    )


def _mot_edm_shuriken_spin(ph: float) -> tuple:
    """【EDM】手裏剣／プロペラ高速回転。"""
    shape = _CH2_SHURIKEN if int(ph * 3.0) % 2 == 0 else _CH2_PROP3
    return _shape_pack(
        ph,
        ch2=shape,
        sx=0.50,
        sy=0.15,
        ch8=18,
        ch3=_ch3_spin_cw(0.95),
        ch9=_ch9_auto_fast(ph * 1.4),
    )


def _mot_edm_prop4_chaos(ph: float) -> tuple:
    """【EDM】四方向プロペラ＋色カオス。"""
    return _shape_pack(
        ph,
        ch2=_CH2_PROP4,
        sx=0.50 + 0.10 * math.sin(ph * 5.5),
        sy=0.12 + 0.10 * math.cos(ph * 4.2),
        ch8=_ch8_zoom_pulse(ph * 3.2, 8, 40),
        ch3=_ch3_spin_ccw(0.9),
        ch9=_ch9_auto_fast(ph * 2.0),
    )


def _mot_edm_tri_rays_fan(ph: float) -> tuple:
    """【EDM】中心3本線ファン開閉。"""
    open_u = (math.sin(ph * 1.6) + 1) / 2
    return _shape_pack(
        ph,
        ch2=_CH2_TRI_RAYS,
        sx=0.50,
        sy=0.12,
        ch8=int(48 - 36 * open_u),
        ch3=_ch3_angle(ph, 1.2),
    )


def _mot_edm_dual_beam(ph: float) -> tuple:
    """【EDM】二重水平／垂直ビーム切替スキャン。"""
    use_h = int(ph * 2.5) % 2 == 0
    return _shape_pack(
        ph,
        ch2=_CH2_DUB_HLINE if use_h else _CH2_DUB_VLINE,
        sx=_tri01(ph * 3.0),
        sy=0.10 + (0.0 if use_h else 0.12 * _tri01(ph * 2.0)),
        ch8=22,
        ch3=0,
    )


def _mot_edm_hourglass_melt(ph: float) -> tuple:
    """【EDM】砂時計メルト（サイズ揺らし）。"""
    return _shape_pack(
        ph,
        ch2=_CH2_HOURGLASS,
        sx=0.50,
        sy=0.10 + 0.14 * _tri01(ph * 1.4),
        ch8=_ch8_zoom_pulse(ph * 1.7, 12, 50),
        ch3=_ch3_spin_cw(0.35),
    )


def _mot_edm_arrow_chase(ph: float) -> tuple:
    """【EDM】矢印チェイス左右。"""
    return _shape_pack(
        ph,
        ch2=_CH2_ARROW,
        sx=0.12 + 0.76 * _tri01(ph * 2.0),
        sy=0.14,
        ch8=24,
        ch3=64,  # 向き固定寄り
    )


def _mot_edm_triwave_bass(ph: float) -> tuple:
    """【EDM】三角波バス・ワブル。"""
    return _shape_pack(
        ph,
        ch2=_CH2_TRIWAVE,
        sx=0.50 + 0.28 * math.sin(ph * 2.8),
        sy=0.08 + 0.10 * abs(math.sin(ph * 5.5)),
        ch8=_ch8_zoom_pulse(ph * 4.0, 15, 48),
        ch3=0,
    )


def _mot_hh_note_stutter(ph: float) -> tuple:
    """【ヒップホップ】音符スタッター点滅。"""
    on = (int(ph * 8.0) % 3) != 2
    slot = int(ph * 4.0) % 4
    sx = 0.20 + slot * 0.20
    return _shape_pack(
        ph,
        ch2=_CH2_NOTE,
        sx=sx,
        sy=0.12 + 0.08 * (slot % 2),
        ch8=20 if on else 50,
        ch3=0,
        ch1=_DOT_BASE_CH1 if on else _DOT_STEP_CH1_BLACKOUT,
    )


def _mot_hh_square_punch(ph: float) -> tuple:
    """【ヒップホップ】正方形パンチ（拍風ズーム）。"""
    punch = max(0.0, math.sin(ph * 6.0)) ** 2
    return _shape_pack(
        ph,
        ch2=_CH2_SQUARE,
        sx=0.50,
        sy=0.10 + 0.12 * punch,
        ch8=int(50 - 42 * punch),
        ch3=_ch3_angle(ph * 0.3, 0.5),
    )


def _mot_hh_diag_slash(ph: float) -> tuple:
    """【ヒップホップ】斜めスラッシュ往復。"""
    u = _tri01(ph * 2.4)
    return _shape_pack(
        ph,
        ch2=_CH2_DIAG,
        sx=0.15 + 0.70 * u,
        sy=0.06 + 0.20 * u,
        ch8=26,
        ch3=0,
    )


def _mot_hh_pent_spin(ph: float) -> tuple:
    """【ヒップホップ】五角形スロー～ファスト回転。"""
    spd = 0.25 + 0.70 * ((math.sin(ph * 0.6) + 1) / 2)
    return _shape_pack(
        ph,
        ch2=_CH2_PENT,
        sx=0.50,
        sy=0.14,
        ch8=22,
        ch3=_ch3_spin_cw(spd),
    )


def _mot_hh_heart_sway(ph: float) -> tuple:
    """【ヒップホップ】ハートゆらし（ブリッジ／フック向け）。"""
    return _shape_pack(
        ph,
        ch2=_CH2_HEART,
        sx=0.50 + 0.18 * math.sin(ph * 1.8),
        sy=0.12 + 0.08 * abs(math.sin(ph * 2.2)),
        ch8=_ch8_zoom_pulse(ph * 1.5, 14, 40),
        ch3=_ch3_angle(ph, 0.35),
        ch9=_MOTION_CH9_PALETTE[int(ph * 3) % len(_MOTION_CH9_PALETTE)],
    )


def _mot_edm_build_morph(ph: float) -> tuple:
    """【EDM】ビルドアップ図形切替（円→三角→星→十字）。"""
    shapes = (_CH2_CIRCLE, _CH2_TRI, _CH2_STAR, _CH2_CROSS, _CH2_DIAMOND)
    idx = int(ph * 1.8) % len(shapes)
    return _shape_pack(
        ph,
        ch2=shapes[idx],
        sx=0.50,
        sy=0.12 + 0.06 * ((idx % 3) / 2),
        ch8=_ch8_zoom_pulse(ph * 2.2 + idx, 8, 45),
        ch3=_ch3_spin_cw(0.4 + 0.1 * idx),
    )


def _norm_sy01_fan(u: float) -> int:
    """上空ファン用: 垂直を広めに使い、床→上空の扇が見えるようにする。"""
    u = max(0.0, min(1.0, float(u)))
    sy = 0.06 + u * 0.78
    return _clamp127f(sy * 127)


# 参考動画 IMG_3749 実測: 開閉≈1.89Hz / 音パルス≈7.8Hz / 視覚フラッシュ≈16Hz
_REF_FAN_OPEN_HZ = 1.89
_REF_FAN_PULSE_HZ = 7.81
_REF_FAN_CLAP_HZ = 1.60
_REF_FAN_SPEED_LOCK = 2.05
_REF_FAN_LIVE_PULSE_HZ = 0.0
_REF_FAN_LIVE_PHASOR = 0.0
_REF_FAN_LIVE_LEVEL = 0.0


def set_ref_fan_audio_sync(*, pulse_hz: float, phasor: float = 0.0, level: float = 0.0) -> None:
    """音声連動から参考ファンのパルスHz／位相／強度を供給。"""
    global _REF_FAN_LIVE_PULSE_HZ, _REF_FAN_LIVE_PHASOR, _REF_FAN_LIVE_LEVEL
    _REF_FAN_LIVE_PULSE_HZ = float(pulse_hz)
    _REF_FAN_LIVE_PHASOR = float(phasor)
    _REF_FAN_LIVE_LEVEL = max(0.0, min(1.0, float(level)))


def _ref_fan_pulse_hz() -> float:
    live = float(_REF_FAN_LIVE_PULSE_HZ)
    if 5.0 <= live <= 12.0:
        return live
    return _REF_FAN_PULSE_HZ


def _ref_fan_sweep_gate() -> float:
    """弱音は小さく、尖ったパルス時ははっきり左右スイープ。"""
    lv = float(_REF_FAN_LIVE_LEVEL)
    hz = float(_REF_FAN_LIVE_PULSE_HZ)
    if hz < 4.2:
        return 0.18
    if lv < 0.18:
        return 0.22
    if lv < 0.32:
        return 0.48
    if lv < 0.48:
        return 0.72
    return min(1.0, 0.78 + (lv - 0.48) * 0.9)


def _mot_ref_quad_fan_sky(ph: float) -> tuple[int, int, int]:
    """参考動画風: 4起点扇の左右スイープ（振幅控えめ・弱音時はほぼ停止）。"""
    t = time.monotonic()
    pulse_hz = _ref_fan_pulse_hz()
    gate = _ref_fan_sweep_gate()
    n_src = 4
    n_ray = 14
    sweep = gate * math.sin(2.0 * math.pi * _REF_FAN_OPEN_HZ * t)
    open_u = 0.45 + 0.35 * gate * abs(math.sin(2.0 * math.pi * _REF_FAN_CLAP_HZ * t))
    idx = int(ph * 110.0) % (n_src * n_ray)
    si = idx // n_ray
    ri = idx % n_ray
    base_shift = 0.14 * sweep
    ox = 0.22 + si * (0.56 / (n_src - 1)) + base_shift
    ox = max(0.12, min(0.88, ox))
    oy = 0.08
    side_bias = -0.35 if si < 2 else 0.35
    half = 0.22 + 0.28 * open_u
    center = side_bias + 0.22 * sweep
    theta = (center - half) + (2.0 * half) * (ri / max(1, n_ray - 1))
    length = 0.22 + 0.16 * open_u
    sx = max(0.12, min(0.88, ox + length * math.sin(theta)))
    sy = max(0.05, min(0.28, oy + length * 0.18 * abs(math.cos(theta))))
    pulse_phase = (t * pulse_hz) % 1.0
    if gate > 0.4 and pulse_phase < 0.07:
        sx = ox
        sy = oy + 0.04
    ch9 = 28 if int(t * pulse_hz) % 2 == 0 else 90
    return _norm_sx01_wide(sx), _norm_sy01(sy), ch9


def _mot_ref_quad_fan_pulse(ph: float) -> tuple[int, int, int]:
    """参考動画パルス寄り: 感度控えめの左右ゲート。"""
    t = time.monotonic()
    pulse_hz = _ref_fan_pulse_hz()
    gate = _ref_fan_sweep_gate()
    n_src = 4
    n_ray = 12
    g = (t * pulse_hz) % 1.0
    sweep = gate * math.sin(2.0 * math.pi * _REF_FAN_OPEN_HZ * t)
    if gate < 0.28:
        open_u = 0.40
    elif g < 0.10:
        open_u = 0.15
    elif g < 0.18:
        open_u = 0.75
    else:
        open_u = 0.40 + 0.30 * gate * abs(math.sin(2.0 * math.pi * _REF_FAN_CLAP_HZ * t))
    idx = int(ph * 120.0) % (n_src * n_ray)
    si = idx // n_ray
    ri = idx % n_ray
    base_shift = 0.16 * sweep
    ox = 0.20 + si * (0.60 / (n_src - 1)) + base_shift
    ox = max(0.12, min(0.88, ox))
    oy = 0.07
    side_bias = -0.38 if si < 2 else 0.38
    half = 0.18 + 0.32 * open_u
    center = side_bias + 0.24 * sweep
    theta = (center - half) + (2.0 * half) * (ri / max(1, n_ray - 1))
    length = 0.20 + 0.18 * open_u
    sx = max(0.12, min(0.88, ox + length * math.sin(theta)))
    sy = max(0.05, min(0.26, oy + length * 0.16 * abs(math.cos(theta))))
    if gate > 0.4 and g < 0.06:
        sx = ox
        sy = oy + 0.04
    ch9 = 90 if int(t * pulse_hz * 1.5) % 3 == 0 else 28
    return _norm_sx01_wide(sx), _norm_sy01(sy), ch9


# 疑似点：止まって点灯→消灯→暗転でコマ送り→再点灯（単一タイムライン）
# 以下の秒は「速度スライダー 1.0」のときの名目時間。速くすると周期が短くなる。
_MOTION_TICK_MS = 36  # _run_motion_tick の after(ms) と一致させる
_MOTION_DPH_BASE = 0.072  # 1 tick あたりの _motion_phase 増分に speed が掛かる
_DOT_HSTEP_ON_SEC = 0.5
_DOT_HSTEP_OFF_AT_SRC_SEC = 0.5
_DOT_HSTEP_OFF_AT_DST_SEC = 0.35
_DOT_HSTEP_N_SLOTS = 12

_DOT_HSTEP_TOTAL_SEC = _DOT_HSTEP_ON_SEC + _DOT_HSTEP_OFF_AT_SRC_SEC + _DOT_HSTEP_OFF_AT_DST_SEC
_PH_PER_WALL_SEC_AT_SPEED1 = (1000.0 / _MOTION_TICK_MS) * _MOTION_DPH_BASE
_DOT_HSTEP_PERIOD_PH = _DOT_HSTEP_TOTAL_SEC * _PH_PER_WALL_SEC_AT_SPEED1
_DOT_HSTEP_PH_ON = _DOT_HSTEP_PERIOD_PH * (_DOT_HSTEP_ON_SEC / _DOT_HSTEP_TOTAL_SEC)
_DOT_HSTEP_PH_SRC = _DOT_HSTEP_PERIOD_PH * (_DOT_HSTEP_OFF_AT_SRC_SEC / _DOT_HSTEP_TOTAL_SEC)
_DOT_HSTEP_PH_DST = _DOT_HSTEP_PERIOD_PH * (_DOT_HSTEP_OFF_AT_DST_SEC / _DOT_HSTEP_TOTAL_SEC)
# ピュン連打などで位相が 0 に戻るとき、前回と違う横位置から始める（0 … N_SLOTS-1）
_DOT_HSTEP_SLOT_OFFSET = 0


def _mot_dot_pulse_step_horizontal(ph: float) -> tuple[int, None, None, None, None, None, int]:
    """単一ビーム：静止→点灯→消灯→コマ移動（暗転）→静止。横のみ CH6、CH7／CH9 は変更しない。"""
    period_ph = max(1e-9, _DOT_HSTEP_PERIOD_PH)
    full = int(ph / period_ph)
    local_ph = ph % period_ph
    n = max(2, _DOT_HSTEP_N_SLOTS)
    slot = (full + _DOT_HSTEP_SLOT_OFFSET) % n
    nxt = (slot + 1) % n
    sx_from = 0.04 + (slot / (n - 1)) * 0.55
    sx_to = 0.04 + (nxt / (n - 1)) * 0.55
    t_on = _DOT_HSTEP_PH_ON
    t_src = _DOT_HSTEP_PH_SRC
    if local_ph < t_on:
        ch1 = _DOT_BASE_CH1
        sx = sx_from
    elif local_ph < t_on + t_src:
        ch1 = _DOT_STEP_CH1_BLACKOUT
        sx = sx_from
    else:
        ch1 = _DOT_STEP_CH1_BLACKOUT
        sx = sx_to
    return _norm_sx01(sx), None, None, None, None, None, ch1


# 戻り値：CH6/CH7/CH9/CH8/CH2 で None のチャンネルはモーションが触らず、その間スライダー操作が有効。
# 4タプル … ch8 が None なら CH8 はスキップ。
# 5タプル … (ch6,ch7,ch9,ch8,ch2)。ch2 が None なら CH2 はスキップ。
# 6タプル … 末尾に CH4（レガシー・現状未使用）。
# 7タプル … 末尾に CH1。
# 8タプル … 末尾に CH3（角度／回転）。
# ※疑似点の CH1／CH2／CH8（pattern.txt「点にする場合」の3つ）は通常の点モーションでは変更しない。
#    例外：水平線開閉・単発横ステップ点・図形EDMモーションなど CH1/CH2/CH8/CH3 を明示制御するプリセットあり。
DotMotionTick = Callable[
    [float],
    tuple[int | None, int | None, int | None]
    | tuple[int | None, int | None, int | None, int | None]
    | tuple[int | None, int | None, int | None, int | None, int | None]
    | tuple[int | None, int | None, int | None, int | None, int | None, int | None]
    | tuple[int | None, int | None, int | None, int | None, int | None, int | None, int | None]
    | tuple[
        int | None,
        int | None,
        int | None,
        int | None,
        int | None,
        int | None,
        int | None,
        int | None,
    ],
]
DOT_POINT_MOTIONS: list[tuple[str, DotMotionTick]] = [
    ("水平スイープ＋色（CH7は手動）", _mot_horiz_color),
    ("垂直スイープ＋色（CH6は手動）", _mot_vert_color),
    ("【点】下→上すっと上昇（行ききり・CH6/CH9は手動）", _mot_vert_sweep_up_hold_ch9),
    ("【点】上→下すっと下降（行ききり・CH6/CH9は手動）", _mot_vert_sweep_down_hold_ch9),
    ("サークル軌道＋色同期", _mot_circle_color),
    ("対角往来＋色ステップ", _mot_diag_color),
    ("リサージュ（8の字風）＋色", _mot_lissajous),
    ("色のみローテーション（CH6/CH7は手動）", _mot_color_only),
    ("床ライン高速スキャン＋色（CH7手動）", _mot_floor_fast),
    ("天井ライン左右＋色（CH7手動）", _mot_ceiling_sweep),
    ("スパイラル風（半径脈動）＋色", _mot_spiral),
    ("【点】単発横ステップ一方向（速度可・CH7/CH9は手動）", _mot_dot_pulse_step_horizontal),
    ("【クラブ】マルチビーム・共有横シェイク", _mot_multi_beam_swarm),
    ("【振れ】三点左右シェイク（同調・CH9手動）", _mot_three_point_lr_shake),
    ("【テクノ】ハイパー全幅トライアングル（CH7手動）", _mot_hyperscan),
    ("【クラブ】ストロボグリッド跳び", _mot_grid_strobe),
    ("【テクノ】左右フラッシュ分割", _mot_side_battle),
    ("【ヒップホップ】ステップ横移動＋バウンス", _mot_hh_step_sweep),
    ("【テクノ】ジグザグ雷スキャン", _mot_zigzag_lightning),
    ("【クラブ】二段レール高速多重", _mot_dual_row_multiplex),
    ("【テクノ】トンネルリサージュ急回転", _mot_techno_tunnel),
    ("【クラブ】床エッジ走査＋オート色混沌", _mot_chaos_floor_auto),
    ("【クラブ】床エッジ超高速走査（CH9固定・CH7手動）", _mot_chaos_floor_ultra_hold_ch9),
    ("【クラブ】床エッジ広がる（中心固定・開く／CH8・CH9は手動）", _mot_club_floor_edge_expand_hold_ch9),
    ("【クラブ】床エッジ閉じる（中心固定・閉じる／CH8・CH9は手動）", _mot_club_floor_edge_contract_hold_ch9),
    ("【水平線CH2】開く（復帰中は消灯・CH1）", _mot_hline_symmetric_expand_hold_ch9),
    ("【水平線CH2】閉じる（復帰中は消灯・CH1）", _mot_hline_symmetric_contract_hold_ch9),
    ("【クラブ】床エッジ多重線・最高速", _mot_floor_edge_max_lines_hyperspeed),
    ("【テクノ】キックウィップ横ブチ", _mot_kick_whip),
    ("【シンセ】とがった高音・左右（反復同期・CH7/CH9手動）", _mot_synth_stutter_lr),
    ("【シンセ】とがった高音・トライアングル（反復同期・CH7/CH9手動）", _mot_synth_stutter_triangle),
    ("【シンセ】3連符・左中右（反復同期・CH7/CH9手動）", _mot_synth_triplet_lr),
    ("【シンセ】左右激振（端点フル・CH7/CH9手動）", _mot_synth_hard_shake_lr),
    ("∞字ループ＋色", _mot_infinity_loop),
    ("振り子アーク＋色", _mot_pendulum_arc),
    ("ウェーブリボン横進み＋色", _mot_wave_ribbon),
    ("ダイヤ軌道周回＋色", _mot_diamond_orbit),
    ("四隅チェイス＋色", _mot_corner_chase),
    ("【クラブ】スターバースト放射多重", _mot_starburst_rays),
    ("ヘリックス登攀＋色", _mot_helix_climb),
    ("【テクノ】十字スキャン＋オート色", _mot_cross_scan),
    ("バラ曲線（花弁）＋色", _mot_rose_petal),
    ("バウンスボール＋色", _mot_bounce_ball),
    ("扇状ファン・スイープ＋色", _mot_fan_sweep),
    ("【クラブ】ドットレイン落下多重", _mot_dot_rain),
    ("ミラーツイン左右対称＋色", _mot_mirror_twins),
    ("中心ブリーズ（静）＋色", _mot_slow_breathe_center),
    ("矩形周回＋色", _mot_square_orbit),
    # EDM / ヒップホップ定番図形
    ("【EDM】円トンネル回転ズーム", _mot_edm_circle_tunnel),
    ("【EDM】正方形スピンスクエア", _mot_edm_square_spin),
    ("【EDM】水平ビーム床スキャナ", _mot_edm_hline_scanner),
    ("【EDM】垂直ゲート左右", _mot_edm_vline_gate),
    ("【EDM】三角形ドロップ脈動", _mot_edm_triangle_drop),
    ("【EDM】星ストロボ回転", _mot_edm_star_strobe),
    ("【EDM】十字Xスピン", _mot_edm_cross_spin),
    ("【EDM】正弦波リボン", _mot_edm_sine_ribbon),
    ("【EDM】ダイヤトンネルズーム", _mot_edm_diamond_tunnel),
    ("【EDM】手裏剣プロペラ回転", _mot_edm_shuriken_spin),
    ("【EDM】四方向プロペラ混沌", _mot_edm_prop4_chaos),
    ("【EDM】三本線ファン開閉", _mot_edm_tri_rays_fan),
    ("【EDM】二重ビームスキャン", _mot_edm_dual_beam),
    ("【EDM】砂時計メルト", _mot_edm_hourglass_melt),
    ("【EDM】矢印チェイス", _mot_edm_arrow_chase),
    ("【EDM】三角波バスワブル", _mot_edm_triwave_bass),
    ("【EDM】ビルド図形モーフィング", _mot_edm_build_morph),
    ("【ヒップホップ】音符スタッター", _mot_hh_note_stutter),
    ("【ヒップホップ】正方形パンチ", _mot_hh_square_punch),
    ("【ヒップホップ】斜めスラッシュ", _mot_hh_diag_slash),
    ("【ヒップホップ】五角形スピン", _mot_hh_pent_spin),
    ("【ヒップホップ】ハートゆらし", _mot_hh_heart_sway),
    ("【参考】4点上空ファン（緑紫・開閉）", _mot_ref_quad_fan_sky),
    ("【参考】4点上空ファン・パルス", _mot_ref_quad_fan_pulse),
]


def _is_point_control_motion_name(name: str) -> bool:
    """CH2 図形プリセットではなく、疑似点の位置制御向けモーションか。"""
    if "【EDM】" in name:
        return False
    if "水平線CH2" in name:
        return False
    # 図形を出すヒップホップ系は除外（点制御のみ）
    if any(
        x in name
        for x in (
            "音符スタッター",
            "正方形パンチ",
            "斜めスラッシュ",
            "五角形スピン",
            "ハートゆらし",
        )
    ):
        return False
    return True


def _motion_name_drives_ch9(name: str) -> bool:
    """モーション自身が CH9（色）を駆動するか。それ以外はパレット／手動 CH9 を維持。"""
    if not name:
        return False
    if "4点上空ファン" in name:
        return True
    if "色のみ" in name:
        return True
    if "オート色" in name:
        return True
    return False


def _point_control_motion_indices() -> list[int]:
    return [i for i, (name, _fn) in enumerate(DOT_POINT_MOTIONS) if _is_point_control_motion_name(name)]


def _dot_synth_stutter_motion_indices() -> list[int]:
    out: list[int] = []
    for i, (name, _fn) in enumerate(DOT_POINT_MOTIONS):
        if not _is_point_control_motion_name(name):
            continue
        if (
            "シンセ】とがった高音" in name
            or "シンセ】3連符" in name
            or "シンセ】左右激振" in name
            or "三点左右シェイク" in name
            or "共有横シェイク" in name
        ):
            out.append(i)
    return out


def _dot_ref_fan_motion_indices() -> list[int]:
    return [i for i, (name, _fn) in enumerate(DOT_POINT_MOTIONS) if "4点上空ファン" in name]


def _dot_three_point_lr_motion_indices() -> list[int]:
    out: list[int] = []
    for i, (name, _fn) in enumerate(DOT_POINT_MOTIONS):
        if not _is_point_control_motion_name(name):
            continue
        if "三点左右シェイク" in name or "共有横シェイク" in name:
            out.append(i)
    return out


def _dot_one_shot_horizontal_motion_index() -> int | None:
    for i, (name, _fn) in enumerate(DOT_POINT_MOTIONS):
        if "単発横ステップ一方向" in name:
            return i
    return None


def bump_dot_hstep_slot_offset(rng: random.Random) -> None:
    """前回の開始スロットと異なる横位置を選ぶ（連続単発で同じ点に見えないように）。"""
    global _DOT_HSTEP_SLOT_OFFSET
    n = max(2, _DOT_HSTEP_N_SLOTS)
    cur = _DOT_HSTEP_SLOT_OFFSET % n
    choices = [j for j in range(n) if j != cur]
    if choices:
        _DOT_HSTEP_SLOT_OFFSET = int(rng.choice(choices))


def list_com_ports() -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for p in serial.tools.list_ports.comports():
        desc = p.description or ""
        out.append((p.device, f"{p.device} — {desc}"))
    return sorted(out, key=lambda x: x[0])


class OpenDMXSender(threading.Thread):
    """Open DMX（Enttec Open 系 FTDI+RS485 等）向けの簡易送信スレッド。"""

    def __init__(self, ser: serial.Serial) -> None:
        super().__init__(daemon=True)
        # Thread 本体が内部属性 self._stop() を使うため、名前が衝突しないようにする
        self._halt = threading.Event()
        self._lock = threading.Lock()
        self._universe = bytearray(DMX_CHANNELS)
        self._ser: serial.Serial = ser

    @staticmethod
    def try_open(port: str) -> serial.Serial:
        """Open DMX で使用するポートを開き、失敗時は SerialException を送出。"""
        return serial.Serial(
            port=port,
            baudrate=OPEN_DMX_BAUD,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_TWO,
            timeout=0,
            write_timeout=None,
            rtscts=False,
            dsrdtr=False,
        )

    def stop(self) -> None:
        self._halt.set()

    def set_universe_snapshot(self, data: bytes) -> None:
        if len(data) != DMX_CHANNELS:
            raise ValueError("universe length must be 512")
        with self._lock:
            self._universe[:] = data

    def _send_frame(self) -> None:
        if not self._ser.is_open:
            return
        with self._lock:
            payload = bytes(self._universe)

        packet = bytes([0]) + payload

        try:
            self._ser.reset_output_buffer()
            self._ser.send_break(duration=0.002)
            time.sleep(0.000050)
            self._ser.write(packet)
            self._ser.flush()
        except serial.SerialException:
            pass

    def run(self) -> None:
        frame_gap = max(1.0 / TARGET_FPS - 0.003, 0.001)
        while not self._halt.is_set():
            t0 = time.perf_counter()
            self._send_frame()
            dt = time.perf_counter() - t0
            time.sleep(max(frame_gap - dt, 0))

        try:
            if self._ser.is_open:
                self._ser.close()
        except serial.SerialException:
            pass


class LaserDMXApp(tk.Frame):
    CH_LABELS = [
        ("CH1", "モード選択（0–63:ブラックアウト / 64–127:マニュアル / 128–191:オート / 192–255:サウンド）"),
        ("CH2", "図形・パターン形状のみ（DMX値に応じたスキャン形状）。色は CH9 で別制御—一覧は pattern.txt の CH2 ブロックのみ読込"),
        ("CH3", "角度（0–127:回転角 / 128–191:正回転速度 / 192–255:逆回転速度）。※疑似点では実質効かないことが多い—静止プリセットでは CH3=0"),
        ("CH4", "水平フリップ（0–127:位置 / 128–255:速度）。※効くのはマニュアルモード（CH1=64〜127）時"),
        ("CH5", "垂直フリップ（0–127:位置 / 128–255:速度）。※同上（オート／サウンド時は効かない場合あり）"),
        ("CH6", "水平位置（0–127:位置 / 128–255:速度）"),
        ("CH7", "垂直位置（0–127:位置 / 128–255:速度）"),
        (
            "CH8",
            "サイズ：0–63 固定（0=最大・63=最小）／64–127 徐々に大きく（値↑で変化が速い）／"
            "128–191 徐々に小さく（値↑で変化が速い）／192–255 大小を繰り返す（値↑で変化が速い）",
        ),
        (
            "CH9",
            "カラー：一般モードは 0–63単色／64–127混合／128–191モノクロ自動／192–255自動。"
            "疑似点時は pattern.txt「点の場合の9ch」（0–19赤 … 100–119水色／120–255自動）。",
        ),
        ("CH10", "コード（0–127ドット・ライン / 128–255ワイヤーストリップ）"),
    ]

    def __init__(
        self,
        master: tk.Misc | None = None,
        *,
        shared_audio: Any | None = None,
        hide_audio_section: bool = False,
        shared_host: tk.Misc | None = None,
    ) -> None:
        self._owns_root = master is None
        root = tk.Tk() if self._owns_root else master
        assert root is not None
        super().__init__(root)
        if self._owns_root:
            self.pack(fill=tk.BOTH, expand=True)
            root.title("Single Head RGB レーザー — DMX 制御")

        self._shared_audio = shared_audio
        self._hide_audio_section = bool(hide_audio_section)
        self._shared_host = shared_host

        self._touch_friendly_ui = tk.BooleanVar(value=True)
        self._last_tile_motion_apply_t = 0.0
        self._touch_wraplength_items: list[tuple[tk.Misc, int, int]] = []
        self._tiles_toolbar_buttons: list[ttk.Button] = []

        self._sender: OpenDMXSender | None = None
        self._channel_vars = [tk.IntVar(value=0) for _ in range(10)]
        self._spinboxes: list[tk.Spinbox] = []
        self._base_addr = tk.IntVar(value=1)
        self._ch2_patterns = load_ch2_patterns()
        self._ch2_combo: ttk.Combobox | None = None
        self._palette_win: tk.Toplevel | None = None
        self._pattern_tiles: list[PatternTileState] = []
        self._tiles_win: tk.Toplevel | None = None
        self._tiles_inner: tk.Frame | None = None
        self._tiles_help_lbl: ttk.Label | None = None
        self._pattern_tile_hold_tile: PatternTileState | None = None
        self._tile_press_frame: tk.Frame | None = None
        self._pattern_tile_key_assign_mode = tk.BooleanVar(value=False)
        self._pattern_tile_key_target: PatternTileState | None = None
        self._pattern_tile_key_target_frame: tk.Frame | None = None
        self._pattern_tile_kb_hold_tile: PatternTileState | None = None
        # 複数押し: source("ptr"|キー正規化名) → タイル。押した順に order
        self._pattern_tile_holds: dict[str, PatternTileState] = {}
        self._pattern_tile_hold_order: list[str] = []
        self._pattern_tile_hold_frames: dict[str, tk.Frame] = {}
        self._pattern_tile_active_laser: PatternTileState | None = None
        self._tiles_key_status_lbl: ttk.Label | None = None
        self._tiles_resize_job: str | None = None
        self._tiles_last_geom: tuple[int, int] | None = None
        self._tiles_rebuilding = False
        self._club_dot_ch9 = 32  # 位置プリセットに載せる CH9（CH9スライダー／色一覧と同期）
        # ポン出し中はライブ AI よりタイルを優先（LED へも通知）
        self._on_tile_led_override: Callable[[dict | bool | None], None] | None = None
        self._tile_override_latched = False

        self._motion_after_id: str | None = None
        self._motion_phase = 0.0
        self._motion_fn: DotMotionTick | None = None
        self._motion_speed = tk.DoubleVar(value=1.35)
        self._motion_apply_dot_base = tk.BooleanVar(value=True)
        self._motion_seq = 0

        self._audio_analyzer: ClubAudioAnalyzer | None = None
        self._audio_enable = tk.BooleanVar(value=False)
        self._audio_sensitivity = tk.DoubleVar(value=1.0)
        # 高さ範囲 %（CH7 位置帯 0〜100。下端〜上端）
        self._audio_sy_lo_pct = tk.DoubleVar(value=round(_MOTION_SY_OUT_MIN * 100.0, 1))
        self._audio_sy_hi_pct = tk.DoubleVar(value=round(_MOTION_SY_OUT_MAX * 100.0, 1))
        self._audio_sy_range_lbl: ttk.Label | None = None
        self._audio_sy_lo_scale: ttk.Scale | None = None
        self._audio_sy_hi_scale: ttk.Scale | None = None
        self._audio_device_combo: ttk.Combobox | None = None
        self._audio_status_lbl: ttk.Label | None = None
        self._audio_motion_after: str | None = None
        self._audio_status_after: str | None = None
        self._audio_rng = random.Random()
        self._last_impact_generation = 0
        self._last_laser_pew_generation = 0
        self._last_shake_generation = 0

        self._track_profile: Any = None
        self._file_playback_active = False
        self._file_t0 = 0.0
        self._file_seek_offset = 0.0  # シーク後の曲位置オフセット（秒）
        self._precalc_event_idx = 0
        # タイムライン再生で解析連動モーションを回しているか
        self._timeline_reactive_motion = False
        # タイムライン専用（解析なしで再生可能）
        self._tl_audio_path: str | None = None
        self._tl_audio_mono: Any = None
        self._tl_sr: int = 44100
        self._tl_duration: float = 0.0
        self._tl_status = tk.StringVar(value="未読込（音楽ファイルを開く）")
        self._tl_volume = tk.DoubleVar(value=80.0)
        self._tl_volume_label = tk.StringVar(value="80%")
        self._tl_wave_peaks: list[float] | None = None
        self._tl_editor_canvas: tk.Canvas | None = None
        self._tl_editor_w = 800
        self._tl_editor_h = 140
        self._tl_ruler_h = 22
        self._tl_cue_lane_h = 28  # 1レーンの高さ（クリップ）
        self._tl_cue_h = 36  # 実描画時にレーン数で更新
        self._tl_wave_h = 72
        self._tl_drag_seek = False
        self._tl_clip_hits: list[tuple[str, float, float, float, float]] = []
        self._tl_selected_cue_id: str | None = None
        self._tl_stream: Any = None
        self._tl_play_sample: int = 0
        self._tl_volume_gain_cached = 0.8  # オーディオスレッドから読む（Tk 非タッチ）
        self._tl_transport_busy = False
        self._tl_transport_gen = 0  # 停止世代（finished コールバックの競合防止）
        self._tl_last_toggle_mono = 0.0
        self._tl_zoom = 1.0  # 1=全体、大きいほど拡大
        self._tl_view_start = 0.0  # 表示左端の曲位置（秒）
        self._tl_zoom_label = tk.StringVar(value="100%")
        self._tl_pan_drag = False
        self._tl_pan_anchor_x = 0.0
        self._tl_pan_anchor_start = 0.0
        self._punch_cues: list[PunchCue] = []
        self._punch_pending: dict[str, dict[str, Any]] = {}
        self._punch_play_idx = 0
        self._punch_release_jobs: dict[str, str] = {}
        self._punch_cue_hold_until: dict[str, float] = {}  # cue source → 解除時刻（曲位置秒）
        self._punch_timeline_after: str | None = None
        self._tl_playhead_last_draw = -1.0
        self._punch_record_enabled = tk.BooleanVar(value=False)
        self._punch_autoplay_enabled = tk.BooleanVar(value=True)
        self._punch_timeline_var = tk.DoubleVar(value=0.0)
        self._punch_time_label = tk.StringVar(value="0.00 / 0.00 s")
        self._punch_status = tk.StringVar(value="ポン出しキーポイント: 0 件")
        self._punch_scrubbing = False

        self._apply_touch_ui_scaling()
        style = ttk.Style(self.winfo_toplevel())
        # 埋め込み時に theme_use すると親の Combobox スタイルが壊れ、展開できなくなる
        if self._owns_root and "clam" in style.theme_names():
            style.theme_use("clam")
        self._configure_touch_ttk(style)

        self._build_ui()
        self._load_pattern_tiles_from_disk()
        top = self.winfo_toplevel()
        top.bind_all("<KeyPress>", self._on_global_key_press)
        top.bind_all("<KeyRelease>", self._on_global_key_release)
        # Button / TButton は Space で「押される」のが先に走るため、クラス綁定で先に奪う
        top.bind_class("Button", "<KeyPress-space>", self._on_space_play_toggle)
        top.bind_class("TButton", "<KeyPress-space>", self._on_space_play_toggle)
        top.bind_class("Button", "<KeyRelease-space>", lambda _e: "break")
        top.bind_class("TButton", "<KeyRelease-space>", lambda _e: "break")
        top.bind_all("<KeyPress-space>", self._on_space_play_toggle)
        # フォーカス喪失時にキー解放が来ないことがある → キーボード保持を強制解除
        top.bind("<FocusOut>", self._on_toplevel_focus_out, add="+")
        if self._owns_root:
            self.after(0, self._fit_initial_geometry)

    def _apply_touch_ui_scaling(self) -> None:
        """タッチパネル向けに UI スケール（pt→px）を粗く上げる。"""
        try:
            self.tk.call("tk", "scaling", 1.24 if self._touch_friendly_ui.get() else 1.0)
        except tk.TclError:
            pass

    def _configure_touch_ttk(self, style: ttk.Style | None = None) -> None:
        st = style or ttk.Style()
        if self._touch_friendly_ui.get():
            st.configure("Touch.TButton", padding=(12, 8))
        else:
            st.configure("Touch.TButton", padding=(5, 3))

    def _on_touch_friendly_toggled(self) -> None:
        self._apply_touch_ui_scaling()
        self._configure_touch_ttk()
        self._sync_touch_widget_sizes()
        self._sync_tiles_toolbar_button_styles()
        if self._tiles_inner is not None:
            def _touch_tile_ui_refresh() -> None:
                self._rebuild_pattern_tiles_grid()
                self._sync_tiles_win_release_binding()
                self._refresh_pattern_tiles_help_label()

            self.after(20, _touch_tile_ui_refresh)

    def _if_touch(self, n: float | int, t: float | int) -> float | int:
        return t if self._touch_friendly_ui.get() else n

    def _sync_touch_widget_sizes(self) -> None:
        ml = int(self._if_touch(180, 280))
        try:
            self._motion_speed_scale.configure(length=ml)
        except (tk.TclError, AttributeError):
            pass
        al = int(self._if_touch(140, 220))
        try:
            self._audio_sens_scale.configure(length=al)
        except (tk.TclError, AttributeError):
            pass
        for sc in (self._audio_sy_lo_scale, self._audio_sy_hi_scale):
            if sc is None:
                continue
            try:
                sc.configure(length=al)
            except (tk.TclError, AttributeError):
                pass
        for sv in self._spinboxes:
            try:
                sv.configure(width=int(self._if_touch(6, 9)))
            except tk.TclError:
                pass
        for lbl, n, t in self._touch_wraplength_items:
            try:
                lbl.configure(wraplength=int(self._if_touch(n, t)))
            except tk.TclError:
                pass
        try:
            self._base_addr_spin.configure(width=int(self._if_touch(10, 14)))
        except (tk.TclError, AttributeError):
            pass
        try:
            self._port_combo.configure(width=int(self._if_touch(34, 40)))
        except (tk.TclError, AttributeError):
            pass
        ad = self._audio_device_combo
        if ad is not None:
            try:
                ad.configure(width=int(self._if_touch(52, 58)))
            except tk.TclError:
                pass
        cb = self._dot_scene_combo
        if cb is not None:
            try:
                cb.configure(width=int(self._if_touch(52, 58)))
            except tk.TclError:
                pass
        mc = self._motion_combo
        if mc is not None:
            try:
                mc.configure(width=int(self._if_touch(38, 44)))
            except tk.TclError:
                pass
        ch2 = self._ch2_combo
        if ch2 is not None:
            try:
                ch2.configure(width=int(self._if_touch(58, 66)))
            except tk.TclError:
                pass
        tl = self._tiles_help_lbl
        if tl is not None:
            try:
                tl.configure(wraplength=int(self._if_touch(720, 820)))
            except tk.TclError:
                pass
        self._refresh_pattern_tiles_help_label()

    def _sync_tiles_toolbar_button_styles(self) -> None:
        st = "Touch.TButton" if self._touch_friendly_ui.get() else "TButton"
        for b in self._tiles_toolbar_buttons:
            try:
                b.configure(style=st)
            except tk.TclError:
                pass

    def _on_tiles_win_pointer_release(self, _event: tk.Event | None = None) -> None:
        """マウスでの押しっぱなしモードのみ、窓全体のボタン離しで送出停止。タッチ向けでは無効。"""
        if self._touch_friendly_ui.get():
            return
        self._on_pattern_tile_release()

    def _sync_tiles_win_release_binding(self) -> None:
        w = self._tiles_win
        if w is None:
            return
        try:
            if not w.winfo_exists():
                return
        except tk.TclError:
            return
        try:
            w.unbind("<ButtonRelease-1>")
        except tk.TclError:
            pass
        w.bind("<ButtonRelease-1>", self._on_tiles_win_pointer_release)

    def _refresh_pattern_tiles_help_label(self) -> None:
        lbl = self._tiles_help_lbl
        if lbl is None:
            return
        wrap = int(self._if_touch(720, 820))
        if self._touch_friendly_ui.get():
            lbl.configure(
                wraplength=wrap,
                text=(
                    "タッチ: タップでキープ、再タップでそのタイルだけ解除（複数同時キープ可・LED＋レーザー合成）。"
                    "キーボード同時押しも可。ライブ中もタイル優先。削除は右上×。"
                    "登録は「現在をキャプチャ」または「アセットから登録」。"
                ),
            )
        else:
            lbl.configure(
                wraplength=wrap,
                text=(
                    "押している間だけ送信。複数キー同時押し・連打切替可（LED＋レーザー合成）。"
                    "全部離すと解除（フォーカスが外れてもキー保持はクリア）。"
                    "登録: 「現在をキャプチャ」／「アセットから登録」。"
                ),
            )

    def _on_pattern_tile_key_mode_toggled(self) -> None:
        if not self._pattern_tile_key_assign_mode.get():
            self._pattern_tile_clear_key_target_full()
        else:
            self._refresh_tiles_key_status_lbl()

    def _refresh_tiles_key_status_lbl(self) -> None:
        lb = self._tiles_key_status_lbl
        if lb is None:
            return
        if self._pattern_tile_key_target is not None:
            lb.configure(
                text=f"キー入力待ち: 「{self._pattern_tile_key_target.title}」…（Escで中止）",
                foreground="#a50",
            )
        elif self._pattern_tile_key_assign_mode.get():
            lb.configure(
                text="キー登録: タイルをクリック→キー（Ctrl+クリックでもタイル指定可）",
                foreground="#333",
            )
        else:
            lb.configure(
                text="登録キーは押している間ポン出し。割当は「キー登録」またはCtrl+クリック",
                foreground="#555",
            )

    def _pattern_tile_arm_hotkey_target(self, tile: PatternTileState, fr: tk.Frame) -> None:
        if (
            self._pattern_tile_key_target is tile
            and self._pattern_tile_key_target_frame is fr
        ):
            self._pattern_tile_clear_key_target_full()
            return
        old_fr = self._pattern_tile_key_target_frame
        if old_fr is not None and old_fr is not fr:
            try:
                if old_fr.winfo_exists():
                    old_fr.configure(highlightbackground="#666", highlightthickness=2)
            except tk.TclError:
                pass
        self._pattern_tile_key_target = tile
        self._pattern_tile_key_target_frame = fr
        try:
            fr.configure(highlightbackground="#fa3", highlightthickness=3)
        except tk.TclError:
            pass
        self._refresh_tiles_key_status_lbl()

    def _pattern_tile_clear_key_target_full(self) -> None:
        self._pattern_tile_key_target = None
        kf = self._pattern_tile_key_target_frame
        self._pattern_tile_key_target_frame = None
        if kf is not None:
            try:
                if kf.winfo_exists():
                    kf.configure(highlightbackground="#666", highlightthickness=2)
            except tk.TclError:
                pass
        self._refresh_tiles_key_status_lbl()

    def _pattern_tile_commit_hotkey(self, event: tk.Event) -> None:
        tile = self._pattern_tile_key_target
        if tile is None:
            return
        k = event.keysym
        if k in _PATTERN_HOTKEY_MODIFIER_KEYS:
            return
        if not k:
            return
        if len(k) == 1 and k.isascii() and k.isalpha():
            stored = k.lower()
        else:
            stored = k[:32]
        nt = _pattern_hotkey_normalize(stored)
        for t in self._pattern_tiles:
            if t is not tile and t.hotkey and _pattern_hotkey_normalize(t.hotkey) == nt:
                t.hotkey = None
        tile.hotkey = stored
        self._save_pattern_tiles_to_disk()
        self._pattern_tile_clear_key_target_full()
        self._rebuild_pattern_tiles_grid()
        if self._tiles_key_status_lbl is not None:
            try:
                self._tiles_key_status_lbl.configure(
                    text=f"登録しました: [{stored}] → 「{tile.title}」",
                    foreground="#070",
                )
            except tk.TclError:
                pass

    def _pattern_tile_clear_hotkey_at(self, index: int) -> None:
        if 0 <= index < len(self._pattern_tiles):
            self._pattern_tiles[index].hotkey = None
            self._save_pattern_tiles_to_disk()
            self._rebuild_pattern_tiles_grid()

    def _hotkey_focus_blocks_tile_hotkey(self, w: tk.Misc | None) -> bool:
        if w is None:
            return False
        cur: tk.Misc | None = w
        for _ in range(48):
            try:
                cls = cur.winfo_class()
            except tk.TclError:
                return True
            if cls in ("Entry", "TEntry", "Text", "TSpinbox", "Spinbox", "TCombobox"):
                return True
            master = getattr(cur, "master", None)
            if master is None:
                break
            cur = master
        return False

    def _pattern_tile_kb_release_clear(self) -> None:
        # 互換: 全キー解放時のみ呼ばれる想定
        if not self._pattern_tile_holds:
            return
        self._end_pattern_tile_override()

    def _is_pointer_or_auto_hold_source(self, source: str) -> bool:
        """マウス／タッチ／自動キューのソース（キー解放や FocusOut では触らない）。"""
        return source == "ptr" or source.startswith("tap:") or source.startswith("cue:")

    def _clear_keyboard_tile_holds(self) -> None:
        """キーボード由来の保持だけ解除（ポインタ／タッチキープ／自動キューは残す）。"""
        kb_srcs = [
            s for s in list(self._pattern_tile_holds) if not self._is_pointer_or_auto_hold_source(s)
        ]
        for s in kb_srcs:
            self._pattern_tile_hold_remove(s)

    def _on_toplevel_focus_out(self, event: tk.Event) -> None:
        # 子ウィジェットへの移動は無視（トップレベルが本当にフォーカスを失ったときだけ）
        try:
            if event.widget is not self.winfo_toplevel():
                return
        except tk.TclError:
            return
        try:
            if self.focus_get() is not None:
                return
        except tk.TclError:
            pass
        self._clear_keyboard_tile_holds()

    def _blackout_laser_channels(self) -> None:
        """ポン出し合成でレーザー担当が居なくなったときの消灯（LED 優先は維持）。"""
        self._stop_dot_motion()
        self._club_dot_ch9 = 0
        self._push_channels_fast((0, 0, 0, 0, 0, 0, 0, 0, 0, 0))

    def _touch_hold_source_for_tile(self, tile: PatternTileState) -> str:
        """タッチキープ用ソース。同一タイルは常に同じキー。"""
        for s, t in self._pattern_tile_holds.items():
            if t is tile and (s == "ptr" or s.startswith("tap:")):
                return s
        idx = next((i for i, t in enumerate(self._pattern_tiles) if t is tile), -1)
        return f"tap:{idx}" if idx >= 0 else f"tap:{id(tile)}"

    def _pattern_tile_hold_add(
        self,
        source: str,
        tile: PatternTileState,
        fr: tk.Frame | None = None,
        *,
        force_reapply: bool = False,
    ) -> None:
        """ポン出しソースを追加／更新して合成反映。"""
        already = self._pattern_tile_holds.get(source) is tile
        self._pattern_tile_holds[source] = tile
        if source in self._pattern_tile_hold_order:
            self._pattern_tile_hold_order.remove(source)
        self._pattern_tile_hold_order.append(source)
        if fr is not None:
            old = self._pattern_tile_hold_frames.get(source)
            if old is not None and old is not fr:
                try:
                    old.configure(highlightbackground="#666", highlightthickness=2)
                except tk.TclError:
                    pass
            self._pattern_tile_hold_frames[source] = fr
            try:
                fr.configure(highlightbackground="#4af", highlightthickness=3)
            except tk.TclError:
                pass
        if already and not force_reapply:
            # オートリピート: 位相リセットしない（記録も重複させない）
            return
        self._punch_record_on_press(source, tile)
        self._pattern_tile_hold_sync()

    def _pattern_tile_hold_remove(self, source: str) -> None:
        self._punch_record_on_release(source)
        if source not in self._pattern_tile_holds:
            return
        del self._pattern_tile_holds[source]
        if source in self._pattern_tile_hold_order:
            self._pattern_tile_hold_order.remove(source)
        fr = self._pattern_tile_hold_frames.pop(source, None)
        if fr is not None:
            try:
                fr.configure(highlightbackground="#666", highlightthickness=2)
            except tk.TclError:
                pass
        self._pattern_tile_hold_sync()

    def _pattern_tile_hold_sync(self) -> None:
        """保持中ソースを合成して LED／レーザーを反映。空なら解除。"""
        if not self._pattern_tile_holds:
            self._end_pattern_tile_override()
            return
        order = [s for s in self._pattern_tile_hold_order if s in self._pattern_tile_holds]
        tiles = [self._pattern_tile_holds[s] for s in order]
        laser_tile = next(
            (t for t in reversed(tiles) if bool(getattr(t, "apply_laser", True))),
            None,
        )
        led_tile = next(
            (t for t in reversed(tiles) if bool(getattr(t, "apply_led", False))),
            None,
        )
        self._tile_override_latched = True
        self._pattern_tile_hold_tile = next(
            (
                self._pattern_tile_holds[s]
                for s in reversed(order)
                if s == "ptr" or s.startswith("tap:")
            ),
            None,
        )
        self._pattern_tile_kb_hold_tile = next(
            (
                self._pattern_tile_holds[s]
                for s in reversed(order)
                if not self._is_pointer_or_auto_hold_source(s)
            ),
            None,
        )
        # UI ハイライト用
        self._tile_press_frame = None
        for s in reversed(order):
            fr = self._pattern_tile_hold_frames.get(s)
            if fr is not None:
                self._tile_press_frame = fr
                break

        if led_tile is not None:
            self._notify_tile_led_override(
                {
                    "r": int(getattr(led_tile, "led_r", 255)),
                    "g": int(getattr(led_tile, "led_g", 80)),
                    "b": int(getattr(led_tile, "led_b", 40)),
                    "brightness": float(getattr(led_tile, "led_brightness", 100.0)),
                    "mode": getattr(led_tile, "led_mode", None),
                    "speed": int(getattr(led_tile, "led_speed", 50)),
                }
            )
        else:
            self._notify_tile_led_override(None)

        if laser_tile is not None:
            reset = laser_tile is not self._pattern_tile_active_laser
            self._apply_pattern_tile_laser(laser_tile, reset_phase=reset)
            self._pattern_tile_active_laser = laser_tile
        else:
            # LED だけ残っているとき、レーザー出力が前タイルのまま残るのを防ぐ
            self._pattern_tile_active_laser = None
            self._blackout_laser_channels()

    def _on_space_play_toggle(self, event: tk.Event) -> str:
        """Space で再生／停止。ボタンのデフォルト Space 動作より先に処理し、必ず break。"""
        try:
            if not self.winfo_exists():
                return "break"
        except tk.TclError:
            return "break"
        # Entry 等でのスペース入力は許可
        if self._hotkey_focus_blocks_tile_hotkey(self.focus_get()):
            return None  # type: ignore[return-value]
        if self._pattern_tile_key_target is not None:
            return "break"
        try:
            if int(getattr(event, "state", 0) or 0) & 0x4000:
                return "break"
        except (TypeError, ValueError):
            pass
        audio = self._tl_audio_mono
        if audio is None or getattr(audio, "size", 0) < 1:
            # 未読込時もボタン誤発火だけは止める（ダイアログは開かない）
            return "break"
        now = time.monotonic()
        if now - self._tl_last_toggle_mono < 0.28:
            return "break"
        self._tl_last_toggle_mono = now
        try:
            self._toggle_file_playback()
        except Exception:
            pass
        return "break"

    def _on_global_key_press(self, event: tk.Event) -> None:
        try:
            if not self.winfo_exists():
                return
        except tk.TclError:
            return
        fw = self.focus_get()
        if self._hotkey_focus_blocks_tile_hotkey(fw):
            return
        keysym = event.keysym

        # Space: タイムライン再生／停止（キー割当登録中は除外）
        if keysym in ("space", "Space") and self._pattern_tile_key_target is None:
            # <space> 専用バインド側で処理
            return

        # Delete: 選択中ポン出しクリップを削除
        if keysym in ("Delete", "BackSpace") and self._pattern_tile_key_target is None:
            if self._tl_selected_cue_id or (
                getattr(self, "_punch_cue_list", None) is not None
                and self._punch_cue_list.curselection()
            ):
                self._punch_cues_delete_selected()
            return

        if self._pattern_tile_key_target is not None:
            if keysym == "Escape":
                self._pattern_tile_clear_key_target_full()
                return
            if keysym in _PATTERN_HOTKEY_MODIFIER_KEYS:
                return
            self._pattern_tile_commit_hotkey(event)
            return

        nt = _pattern_hotkey_normalize(keysym)
        for t in self._pattern_tiles:
            if not t.hotkey:
                continue
            if _pattern_hotkey_normalize(t.hotkey) == nt:
                self._pattern_tile_hold_add(nt, t)
                return

    def _on_global_key_release(self, event: tk.Event) -> None:
        try:
            if not self.winfo_exists():
                return
        except tk.TclError:
            return
        keysym = event.keysym
        if keysym in _PATTERN_HOTKEY_MODIFIER_KEYS:
            return
        nt = _pattern_hotkey_normalize(keysym)
        # 保持中キーの解放はフォーカス位置に関係なく必ず処理
        # （Entry フォーカス中に KeyRelease を捨てると複数押しが残る）
        if nt in self._pattern_tile_holds:
            self._pattern_tile_hold_remove(nt)
            return
        if keysym in self._pattern_tile_holds:
            self._pattern_tile_hold_remove(keysym)
            return

    def _add_collapsible_section(
        self,
        parent: tk.Misc,
        title: str,
        *,
        default_open: bool = True,
        expand: bool = False,
    ) -> ttk.Frame:
        """見出し横の ▼/▶ で中身を開閉。expand=True のセクションが縦の余白を伸ばす。"""
        outer = ttk.Frame(parent)
        outer.pack(fill=(tk.BOTH if expand else tk.X), expand=expand, pady=(0, 2))
        head = ttk.Frame(outer)
        head.pack(fill=tk.X, padx=8, pady=(2, 0))
        body = ttk.Frame(outer)
        st: dict[str, bool] = {"open": bool(default_open)}
        toggle_btn = ttk.Button(head, width=3)

        def refresh() -> None:
            toggle_btn.configure(text=("▼" if st["open"] else "▶"))

        def do_toggle() -> None:
            st["open"] = not st["open"]
            if st["open"]:
                body.pack(fill=(tk.BOTH if expand else tk.X), expand=expand, padx=10, pady=(0, 8))
            else:
                body.pack_forget()
            refresh()

        toggle_btn.configure(command=do_toggle)
        toggle_btn.pack(side=tk.LEFT, padx=(0, 6))
        try:
            toggle_btn.configure(style="Touch.TButton")
        except tk.TclError:
            pass
        title_lbl = ttk.Label(head, text=title, font=("Segoe UI", 9, "bold"), cursor="hand2")
        title_lbl.pack(side=tk.LEFT, anchor="w")
        title_lbl.bind("<Button-1>", lambda _e: do_toggle())
        refresh()
        if st["open"]:
            body.pack(fill=(tk.BOTH if expand else tk.X), expand=expand, padx=10, pady=(0, 8))
        return body

    def _fit_initial_geometry(self) -> None:
        """起動時ウィンドウが画面外・画面より大きくならないようにし、おおよそ中央に置く。"""
        if not self._owns_root:
            return
        root = self.winfo_toplevel()
        root.update_idletasks()
        margin = 80
        sw = int(root.winfo_screenwidth())
        sh = int(root.winfo_screenheight())
        rw = int(self.winfo_reqwidth())
        rh = int(self.winfo_reqheight())
        max_w = max(520, sw - margin)
        max_h = max(420, sh - margin)
        tw = min(max(640, rw + 28), max_w, int(sw * 0.94))
        th = min(max(560, rh + 36), max_h, int(sh * 0.92))
        x = max(0, (sw - tw) // 2)
        y = max(0, (sh - th) // 2)
        root.geometry(f"{tw}x{th}+{x}+{y}")
        root.minsize(min(520, max_w), min(400, max_h))

    def _make_scrollable(self, parent: tk.Misc) -> tuple[ttk.Frame, ttk.Frame]:
        """タブ内のはみ出し防止用スクロール領域。(outer, inner) を返す。"""
        outer = ttk.Frame(parent)
        outer.pack(fill=tk.BOTH, expand=True)
        canvas = tk.Canvas(outer, highlightthickness=0, bd=0)
        vsb = ttk.Scrollbar(outer, orient=tk.VERTICAL, command=canvas.yview)
        inner = ttk.Frame(canvas)
        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        win = canvas.create_window((0, 0), window=inner, anchor="nw")

        def _sync_width(event: tk.Event) -> None:
            canvas.itemconfigure(win, width=max(1, int(event.width)))

        canvas.bind("<Configure>", _sync_width)
        canvas.configure(yscrollcommand=vsb.set)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)

        def _on_wheel(e: tk.Event) -> None:
            canvas.yview_scroll(int(-1 * (e.delta / 120)), "units")

        def _bind(_e: tk.Event) -> None:
            canvas.bind_all("<MouseWheel>", _on_wheel)

        def _unbind(_e: tk.Event) -> None:
            canvas.unbind_all("<MouseWheel>")

        canvas.bind("<Enter>", _bind)
        canvas.bind("<Leave>", _unbind)
        return outer, inner

    def _build_ui(self) -> None:
        shell = ttk.Frame(self)
        shell.pack(fill=tk.BOTH, expand=True)

        nb = ttk.Notebook(shell)
        nb.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)
        self._main_notebook = nb

        tab_ops = ttk.Frame(nb)
        tab_audio = ttk.Frame(nb)
        tab_ch = ttk.Frame(nb)
        tab_info = ttk.Frame(nb)
        nb.add(tab_ops, text="操作")
        # 共通ホストがあるときはタイムライン／ポン出しはそちらへ
        if self._shared_host is None:
            tab_tl = ttk.Frame(nb)
            nb.add(tab_tl, text="タイムライン")
            self._build_tab_timeline(tab_tl)
        else:
            self._build_shared_controls(self._shared_host)
        nb.add(tab_audio, text="音声")
        nb.add(tab_ch, text="CH値")
        nb.add(tab_info, text="メモ")

        self._build_tab_ops(tab_ops)
        self._build_tab_audio(tab_audio)
        self._build_tab_channels(tab_ch)
        self._build_tab_info(tab_info)

        self._refresh_ports(init=True)
        self.after_idle(self._boot_sync_channels)
        self.after_idle(self._fit_initial_geometry)
        self._sync_touch_widget_sizes()

    def _build_shared_controls(self, parent: tk.Misc) -> None:
        """LED/レーザー非依存のタイムラインを共通ホストへ載せる。"""
        self._build_tab_timeline(parent)

    def _build_tab_ops(self, parent: tk.Misc) -> None:
        _, body = self._make_scrollable(parent)
        pad = ttk.Frame(body, padding=8)
        pad.pack(fill=tk.BOTH, expand=True)

        # --- 接続 ---
        conn = ttk.LabelFrame(pad, text="接続・DMX 送信", padding=8)
        conn.pack(fill=tk.X, pady=(0, 8))
        conn.columnconfigure(1, weight=1)
        ttk.Label(conn, text="COM").grid(row=0, column=0, sticky="w")
        self._port_combo = ttk.Combobox(conn, width=int(self._if_touch(28, 36)), state="readonly")
        self._port_combo.grid(row=0, column=1, padx=(8, 4), sticky="ew")
        ttk.Button(conn, text="再検索", command=self._refresh_ports).grid(row=0, column=2, padx=4)
        ttk.Label(conn, text="開始アドレス").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self._base_addr_spin = ttk.Spinbox(
            conn,
            from_=1,
            to=503,
            width=int(self._if_touch(8, 12)),
            textvariable=self._base_addr,
            command=self._on_values_changed,
        )
        self._base_addr_spin.grid(row=1, column=1, sticky="w", pady=(6, 0))
        btns = ttk.Frame(conn)
        btns.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(8, 0))
        ttk.Button(btns, text="送信開始", command=self._start_sending).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(btns, text="送信停止", command=self._stop_sending).pack(side=tk.LEFT, padx=6)
        try:
            _boot = load_settings()
            self._base_addr.set(int(_boot.get("dmx_base_addr", 1) or 1))
            self._dmx_auto_connect = tk.BooleanVar(value=bool(_boot.get("dmx_auto_connect", False)))
        except (TypeError, ValueError, tk.TclError):
            self._dmx_auto_connect = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            conn,
            text="起動時にこの COM へ自動接続",
            variable=self._dmx_auto_connect,
            command=self._on_dmx_auto_connect_toggled,
        ).grid(row=3, column=0, columnspan=3, sticky="w", pady=(8, 0))
        if self._shared_host is None:
            ttk.Checkbutton(
                conn,
                text="タッチ向けUI",
                variable=self._touch_friendly_ui,
                command=self._on_touch_friendly_toggled,
            ).grid(row=4, column=0, columnspan=3, sticky="w", pady=(4, 0))

        # --- CH1 ---
        presets = ttk.LabelFrame(pad, text="CH1 モード", padding=8)
        presets.pack(fill=tk.X, pady=(0, 8))
        f = ttk.Frame(presets)
        f.pack(fill=tk.X)
        for txt, val in [
            ("ブラックアウト", 0),
            ("マニュアル", 100),
            ("オート", 160),
            ("サウンド", 220),
        ]:
            ttk.Button(f, text=txt, width=12, command=lambda v=val: self._set_ch1_preset(v)).pack(
                side=tk.LEFT, padx=3, pady=2
            )
        if self._ch2_patterns:
            ttk.Button(
                presets,
                text="疑似点土台のみ（CH1/CH2/CH8）",
                command=self._apply_dot_look_preset,
            ).pack(anchor="w", pady=(6, 0))

        # --- 点・モーション ---
        club = ttk.LabelFrame(pad, text="疑似点・モーション", padding=8)
        club.pack(fill=tk.X, pady=(0, 8))
        sf = ttk.Frame(club)
        sf.pack(fill=tk.X)
        ttk.Label(sf, text="静止").pack(side=tk.LEFT)
        self._dot_scene_combo = ttk.Combobox(
            sf,
            values=[n for n, _ch6, _ch7 in CLUB_DOT_POSITION_PRESETS],
            width=int(self._if_touch(36, 44)),
            state="readonly",
        )
        self._dot_scene_combo.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(6, 6))
        if CLUB_DOT_POSITION_PRESETS:
            self._dot_scene_combo.current(0)
        ttk.Button(sf, text="適用", width=8, command=self._apply_selected_dot_scene).pack(side=tk.LEFT)
        row_b = ttk.Frame(club)
        row_b.pack(fill=tk.X, pady=(6, 0))
        ttk.Button(row_b, text="パレット…", command=self._open_pattern_palette).pack(side=tk.LEFT, padx=(0, 6))
        if self._shared_host is None:
            ttk.Button(row_b, text="ポン出しタイル…", command=self._open_pattern_tiles_window).pack(
                side=tk.LEFT
            )

        ttk.Separator(club, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=8)
        ttk.Checkbutton(
            club,
            text="開始／種類切替時に疑似点土台をセット",
            variable=self._motion_apply_dot_base,
        ).pack(anchor="w")
        mf = ttk.Frame(club)
        mf.pack(fill=tk.X, pady=(4, 0))
        ttk.Label(mf, text="モーション").pack(side=tk.LEFT)
        self._motion_combo = ttk.Combobox(
            mf,
            values=[n for n, _fn in DOT_POINT_MOTIONS],
            width=int(self._if_touch(28, 36)),
            state="readonly",
        )
        self._motion_combo.pack(side=tk.LEFT, padx=(6, 8), fill=tk.X, expand=True)
        if DOT_POINT_MOTIONS:
            self._motion_combo.current(0)
        self._motion_combo.bind("<<ComboboxSelected>>", self._on_motion_combo_selected)
        mf2 = ttk.Frame(club)
        mf2.pack(fill=tk.X, pady=(4, 0))
        ttk.Label(mf2, text="速度").pack(side=tk.LEFT)
        self._motion_speed_scale = ttk.Scale(
            mf2,
            from_=MOTION_SPEED_MIN,
            to=MOTION_SPEED_MAX,
            orient=tk.HORIZONTAL,
            command=lambda v: self._motion_speed.set(
                max(MOTION_SPEED_MIN, min(MOTION_SPEED_MAX, float(v)))
            ),
        )
        self._motion_speed_scale.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(6, 8))
        self._motion_speed_scale.set(1.35)
        ttk.Button(mf2, text="開始", command=self._start_dot_motion).pack(side=tk.LEFT, padx=2)
        ttk.Button(mf2, text="停止", command=self._stop_dot_motion).pack(side=tk.LEFT, padx=2)

        # --- 高さ（共通） ---
        hbox = ttk.LabelFrame(pad, text="高さ範囲（モーション共通）", padding=8)
        hbox.pack(fill=tk.X, pady=(0, 8))
        au_h = ttk.Frame(hbox)
        au_h.pack(fill=tk.X)
        ttk.Label(au_h, text="下端%").pack(side=tk.LEFT)
        self._audio_sy_lo_scale = ttk.Scale(
            au_h,
            from_=0.0,
            to=98.0,
            orient=tk.HORIZONTAL,
            command=self._on_audio_sy_range_scale,
        )
        self._audio_sy_lo_scale.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(4, 8))
        self._audio_sy_lo_scale.set(float(self._audio_sy_lo_pct.get()))
        ttk.Label(au_h, text="上端%").pack(side=tk.LEFT)
        self._audio_sy_hi_scale = ttk.Scale(
            au_h,
            from_=2.0,
            to=100.0,
            orient=tk.HORIZONTAL,
            command=self._on_audio_sy_range_scale,
        )
        self._audio_sy_hi_scale.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(4, 8))
        self._audio_sy_hi_scale.set(float(self._audio_sy_hi_pct.get()))
        self._audio_sy_range_lbl = ttk.Label(au_h, text="", font=("Segoe UI", 8))
        self._audio_sy_range_lbl.pack(side=tk.LEFT)
        self._apply_audio_sy_range_from_ui(announce=False)
        ttk.Label(
            hbox,
            text="0%=床／100%=天井。点モーションの上下はこの範囲内。",
            foreground="#666",
            font=("Segoe UI", 8),
        ).pack(anchor="w", pady=(4, 0))

    def _build_tab_timeline(self, parent: tk.Misc) -> None:
        pad = ttk.Frame(parent, padding=8)
        pad.pack(fill=tk.BOTH, expand=True)

        ttk.Label(
            pad,
            text="Space=再生/停止 ／ ホイール=拡大 ／ Shift+ドラッグ=横スクロール ／ Delete=選択クリップ削除",
            foreground="#555",
            wraplength=int(self._if_touch(560, 720)),
            font=("Segoe UI", 8),
        ).pack(anchor="w")

        self._tl_status_lbl = ttk.Label(pad, textvariable=self._tl_status, font=("Segoe UI", 9))
        self._tl_status_lbl.pack(anchor="w", pady=(4, 0))

        # ツール行: タイル窓・プロジェクト（Space で誤発火しないよう takefocus=False）
        tools = ttk.Frame(pad)
        tools.pack(fill=tk.X, pady=(8, 0))
        ttk.Button(
            tools,
            text="ポン出しタイルを開く…",
            command=self._open_pattern_tiles_window,
            takefocus=False,
        ).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(
            tools, text="パレット…", command=self._open_pattern_palette, takefocus=False
        ).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Checkbutton(
            tools,
            text="タッチ向けUI",
            variable=self._touch_friendly_ui,
            command=self._on_touch_friendly_toggled,
            takefocus=False,
        ).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(
            tools,
            text="プロジェクト保存…",
            command=self._timeline_project_save,
            takefocus=False,
        ).pack(side=tk.RIGHT, padx=(4, 0))
        ttk.Button(
            tools,
            text="プロジェクト開く…",
            command=self._timeline_project_load,
            takefocus=False,
        ).pack(side=tk.RIGHT)

        # トランスポート
        tl_btns = ttk.Frame(pad)
        tl_btns.pack(fill=tk.X, pady=(8, 0))
        ttk.Button(
            tl_btns,
            text="音楽を開く…",
            command=self._timeline_open_audio,
            takefocus=False,
        ).pack(side=tk.LEFT, padx=(0, 6))
        self._btn_play_sync = tk.Button(
            tl_btns,
            text="▶ 再生",
            command=self._toggle_file_playback,
            state=tk.DISABLED,
            takefocus=0,
        )
        self._btn_play_sync.pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(
            tl_btns, text="⏹", width=3, command=self._timeline_stop_to_start, takefocus=False
        ).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Label(tl_btns, textvariable=self._punch_time_label, width=16).pack(side=tk.LEFT, padx=(4, 8))

        zoom_row = ttk.Frame(tl_btns)
        zoom_row.pack(side=tk.LEFT, padx=(4, 8))
        ttk.Button(
            zoom_row, text="−", width=3, command=lambda: self._tl_zoom_by(1 / 1.4), takefocus=False
        ).pack(side=tk.LEFT)
        ttk.Label(zoom_row, textvariable=self._tl_zoom_label, width=5).pack(side=tk.LEFT, padx=2)
        ttk.Button(
            zoom_row, text="＋", width=3, command=lambda: self._tl_zoom_by(1.4), takefocus=False
        ).pack(side=tk.LEFT)
        ttk.Button(
            zoom_row, text="全体", width=4, command=self._tl_zoom_fit, takefocus=False
        ).pack(side=tk.LEFT, padx=(4, 0))

        vol_row = ttk.Frame(tl_btns)
        vol_row.pack(side=tk.RIGHT, fill=tk.X, expand=True)
        ttk.Label(vol_row, text="音量").pack(side=tk.LEFT, padx=(8, 4))
        self._tl_volume_scale = ttk.Scale(
            vol_row,
            from_=0.0,
            to=100.0,
            variable=self._tl_volume,
            orient=tk.HORIZONTAL,
            command=self._on_tl_volume_change,
        )
        self._tl_volume_scale.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 4))
        ttk.Label(vol_row, textvariable=self._tl_volume_label, width=4).pack(side=tk.LEFT)
        self._on_tl_volume_change()

        # エディタキャンバス（定規＋波形＋キューレーン＋プレイヘッド）
        editor_wrap = ttk.Frame(pad)
        editor_wrap.pack(fill=tk.X, pady=(10, 0))
        total_h = self._tl_ruler_h + self._tl_wave_h + self._tl_cue_h
        self._tl_editor_canvas = tk.Canvas(
            editor_wrap,
            height=total_h,
            bg="#1a1d23",
            highlightthickness=1,
            highlightbackground="#3a3f4b",
            cursor="sb_h_double_arrow",
        )
        self._tl_editor_canvas.pack(fill=tk.X, expand=True)
        self._tl_editor_canvas.bind("<Configure>", self._on_tl_editor_configure)
        self._tl_editor_canvas.bind("<ButtonPress-1>", self._on_tl_editor_press)
        self._tl_editor_canvas.bind("<B1-Motion>", self._on_tl_editor_drag)
        self._tl_editor_canvas.bind("<ButtonRelease-1>", self._on_tl_editor_release)
        self._tl_editor_canvas.bind("<MouseWheel>", self._on_tl_editor_wheel)
        self._tl_editor_canvas.bind("<Shift-ButtonPress-1>", self._on_tl_pan_press)
        self._tl_editor_canvas.bind("<Shift-B1-Motion>", self._on_tl_pan_drag)
        self._tl_editor_canvas.bind("<Shift-ButtonRelease-1>", self._on_tl_pan_release)
        self._tl_editor_canvas.bind("<Delete>", lambda _e: self._punch_cues_delete_selected())
        self._tl_editor_canvas.bind("<BackSpace>", lambda _e: self._punch_cues_delete_selected())

        self._punch_timeline_scale = None  # type: ignore[assignment]

        pk = ttk.Frame(pad)
        pk.pack(fill=tk.X, pady=(8, 0))
        ttk.Checkbutton(
            pk, text="ポン出しを記録", variable=self._punch_record_enabled
        ).pack(side=tk.LEFT, padx=(0, 12))
        ttk.Checkbutton(
            pk, text="再生時に自動ポン出し", variable=self._punch_autoplay_enabled
        ).pack(side=tk.LEFT, padx=(0, 12))
        ttk.Label(pk, textvariable=self._punch_status, foreground="#336").pack(side=tk.LEFT)

        pk2 = ttk.Frame(pad)
        pk2.pack(fill=tk.X, pady=(6, 0))
        ttk.Button(
            pk2, text="キューのみ保存…", command=self._punch_cues_save, takefocus=False
        ).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(
            pk2, text="キューのみ読込…", command=self._punch_cues_load, takefocus=False
        ).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(
            pk2, text="選択削除", command=self._punch_cues_delete_selected, takefocus=False
        ).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(pk2, text="全消去", command=self._punch_cues_clear, takefocus=False).pack(
            side=tk.LEFT
        )

        cue_row = ttk.Frame(pad)
        cue_row.pack(fill=tk.BOTH, expand=True, pady=(8, 0))
        self._punch_cue_list = tk.Listbox(
            cue_row,
            height=8,
            exportselection=False,
            selectmode=tk.EXTENDED,
            font=("Consolas", 9),
            bg="#1e2228",
            fg="#e8eaed",
            selectbackground="#3d8bfd",
        )
        cue_sb = ttk.Scrollbar(cue_row, orient=tk.VERTICAL, command=self._punch_cue_list.yview)
        self._punch_cue_list.configure(yscrollcommand=cue_sb.set)
        self._punch_cue_list.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        cue_sb.pack(side=tk.RIGHT, fill=tk.Y)
        self._punch_cue_list.bind("<<ListboxSelect>>", self._on_punch_cue_list_select)
        self._punch_cue_list.bind("<Delete>", lambda _e: self._punch_cues_delete_selected())
        self._punch_cue_list.bind("<BackSpace>", lambda _e: self._punch_cues_delete_selected())

        self._tl_refresh_zoom_label()
        self.after_idle(self._tl_redraw_editor)

    def _build_tab_audio(self, parent: tk.Misc) -> None:
        _, body = self._make_scrollable(parent)
        pad = ttk.Frame(body, padding=8)
        pad.pack(fill=tk.BOTH, expand=True)

        live = ttk.LabelFrame(pad, text="マイク連動", padding=8)
        live.pack(fill=tk.X, pady=(0, 8))
        au_top = ttk.Frame(live)
        au_top.pack(fill=tk.X)
        if self._hide_audio_section:
            ttk.Label(
                au_top,
                text="マイク連動は統合アプリの「共通」タブの共有 AI を使います。",
                foreground="#555",
                wraplength=int(self._if_touch(520, 640)),
            ).pack(anchor="w")
            self._audio_chk = None
            # 属性参照用の非表示プレースホルダ
            _hid = ttk.Frame(live)
            self._audio_device_combo = ttk.Combobox(_hid, state="disabled")
            self._audio_sens_scale = ttk.Scale(_hid, from_=0.45, to=1.85)
        else:
            self._audio_chk = ttk.Checkbutton(
                au_top,
                text="オン（モーション自動切替）",
                variable=self._audio_enable,
                command=self._on_audio_reactive_toggled,
                state="normal" if _AUDIO_REACTIVE_AVAILABLE else "disabled",
            )
            self._audio_chk.pack(side=tk.LEFT)
            ttk.Button(au_top, text="デバイス再検索", command=self._refresh_audio_input_devices).pack(
                side=tk.LEFT, padx=(10, 0)
            )
            au_row2 = ttk.Frame(live)
            au_row2.pack(fill=tk.X, pady=(6, 0))
            ttk.Label(au_row2, text="マイク").pack(side=tk.LEFT)
            self._audio_device_combo = ttk.Combobox(
                au_row2,
                width=int(self._if_touch(36, 44)),
                state="readonly" if _AUDIO_REACTIVE_AVAILABLE else "disabled",
            )
            self._audio_device_combo.pack(side=tk.LEFT, padx=(6, 8), fill=tk.X, expand=True)
            ttk.Label(au_row2, text="感度").pack(side=tk.LEFT)
            self._audio_sens_scale = ttk.Scale(
                au_row2,
                from_=0.45,
                to=1.85,
                orient=tk.HORIZONTAL,
                command=lambda v: self._audio_sensitivity.set(max(0.45, min(1.85, float(v)))),
            )
            self._audio_sens_scale.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(4, 0))
            self._audio_sens_scale.set(1.0)
            self._refresh_audio_input_devices(init=True)

        self._audio_status_lbl = ttk.Label(
            live,
            text=(
                "共有 AI 連動中は統合アプリ側のステータスを参照。"
                if self._hide_audio_section
                else (
                    "（オフ）BPM・ジャンル推定がここに表示されます。"
                    if _AUDIO_REACTIVE_AVAILABLE
                    else "要: pip install numpy sounddevice"
                )
            ),
            foreground="#555",
            wraplength=int(self._if_touch(520, 640)),
            font=("Segoe UI", 8),
        )
        self._audio_status_lbl.pack(anchor="w", pady=(6, 0))

        pre = ttk.LabelFrame(pad, text="ファイル事前解析（任意）", padding=8)
        pre.pack(fill=tk.BOTH, expand=True)
        self._precalc_status = tk.StringVar(
            value=(
                "未解析（拍連動用。タイムライン再生には不要）"
                if PRECALC_AVAILABLE
                else "要: pip install librosa soundfile"
            )
        )
        ttk.Label(pre, textvariable=self._precalc_status, wraplength=int(self._if_touch(520, 640))).pack(
            anchor="w"
        )
        ttk.Label(
            pre,
            text="解析すると再生中に拍／イベントでモーションが切り替わります。",
            foreground="#555",
            font=("Segoe UI", 8),
            wraplength=int(self._if_touch(520, 640)),
        ).pack(anchor="w", pady=(2, 0))
        pf = ttk.Frame(pre)
        pf.pack(fill=tk.X, pady=6)
        self._btn_precalc_analyze = tk.Button(pf, text="ファイルを解析…", command=self._precalc_analyze_pick)
        self._btn_precalc_analyze.pack(side=tk.LEFT, padx=(0, 6))
        self._btn_precalc_save_json = tk.Button(
            pf, text="JSON保存", command=self._precalc_save_json, state=tk.DISABLED
        )
        self._btn_precalc_save_json.pack(side=tk.LEFT, padx=(0, 6))
        self._btn_precalc_load_json = tk.Button(pf, text="JSONを開く", command=self._precalc_load_json)
        self._btn_precalc_load_json.pack(side=tk.LEFT)
        self._precalc_log = scrolledtext.ScrolledText(pre, height=8, font=("Consolas", 8), wrap=tk.WORD)
        self._precalc_log.pack(fill=tk.BOTH, expand=True, pady=(4, 0))

    def _build_tab_channels(self, parent: tk.Misc) -> None:
        outer = ttk.Frame(parent, padding=4)
        outer.pack(fill=tk.BOTH, expand=True)
        canvas = tk.Canvas(outer, highlightthickness=0)
        vsb = ttk.Scrollbar(outer, orient=tk.VERTICAL, command=canvas.yview)
        inner = ttk.Frame(canvas)
        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        win = canvas.create_window((0, 0), window=inner, anchor="nw")

        def _sync_width(event: tk.Event) -> None:
            canvas.itemconfigure(win, width=max(1, int(event.width)))

        canvas.bind("<Configure>", _sync_width)
        canvas.configure(yscrollcommand=vsb.set)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)

        for idx, ((name, tip), var) in enumerate(zip(self.CH_LABELS, self._channel_vars, strict=True)):
            blk = ttk.Frame(inner, padding=(4, 6))
            blk.pack(fill=tk.X)
            lh = ttk.Frame(blk)
            lh.pack(fill=tk.X)
            ttk.Label(lh, text=f"{name}", width=5, anchor="w").pack(side=tk.LEFT)
            tip_lbl = ttk.Label(lh, text=tip, wraplength=int(self._if_touch(420, 560)), font=("Segoe UI", 8))
            tip_lbl.pack(side=tk.LEFT, fill=tk.X, padx=(6, 0))
            self._touch_wraplength_items.append((tip_lbl, 420, 560))
            row_ctrl = ttk.Frame(blk)
            row_ctrl.pack(fill=tk.X, pady=(2, 0))
            sc = ttk.Scale(
                row_ctrl,
                from_=0,
                to=255,
                orient=tk.HORIZONTAL,
                command=lambda _v, i=idx: self._sync_scale_spin(i),
            )
            sc.pack(side=tk.LEFT, fill=tk.X, expand=True)
            setattr(self, f"_scale_{idx}", sc)
            sv = tk.Spinbox(
                row_ctrl,
                from_=0,
                to=255,
                width=int(self._if_touch(6, 9)),
                textvariable=var,
                command=lambda i=idx: self._spinbox_commit(i),
            )
            sv.pack(side=tk.LEFT, padx=(8, 0))
            self._spinboxes.append(sv)
            var.trace_add("write", lambda *_a, i=idx: self._spin_from_var(i))
            for seq in ("<Return>", "<FocusOut>", "<ButtonRelease-1>"):
                sv.bind(seq, lambda _e, i=idx: self._spinbox_commit(i))
            sc.bind("<B1-Motion>", lambda _e, i=idx: self._drag_scale_live(i))
            sc.bind("<ButtonRelease-1>", lambda _e, i=idx: self._drag_scale_live(i))
            if idx == 1 and self._ch2_patterns:
                row_pat = ttk.Frame(blk)
                row_pat.pack(fill=tk.X, pady=(4, 0))
                ttk.Label(row_pat, text="図形", width=5, anchor="w").pack(side=tk.LEFT)
                labels = [f"{s}–{e} {n}" for s, e, n in self._ch2_patterns]
                cb = ttk.Combobox(row_pat, values=labels, width=int(self._if_touch(48, 58)), state="readonly")
                cb.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(6, 0))
                cb.bind("<<ComboboxSelected>>", self._on_ch2_pattern_selected)
                self._ch2_combo = cb

        def _on_wheel(e: tk.Event) -> None:
            canvas.yview_scroll(int(-1 * (e.delta / 120)), "units")

        canvas.bind("<Enter>", lambda _e: canvas.bind_all("<MouseWheel>", _on_wheel))
        canvas.bind("<Leave>", lambda _e: canvas.unbind_all("<MouseWheel>"))

        bottom = ttk.Frame(parent, padding=(8, 4))
        bottom.pack(fill=tk.X, side=tk.BOTTOM)
        ttk.Button(bottom, text="全チャンネル 0", command=self._clear_all).pack(side=tk.LEFT)

    def _build_tab_info(self, parent: tk.Misc) -> None:
        pad = ttk.Frame(parent, padding=10)
        pad.pack(fill=tk.BOTH, expand=True)
        info = (
            "接続：USB-DMX512（Open DMX / FTDI RS485 想定）\n"
            "シリアル：250000 bps・8データ・無奇偶・2ストップ。\n"
            "※ Enttec USB DMX PRO などは別プロトコルのため非対応。\n"
            "※ 本機 10CH にディマー専用CHはない。\n\n"
            "タブ構成：\n"
            "・操作 … 接続・モード・モーション・高さ\n"
            "・音声 … マイク連動と任意のファイル解析\n"
            "・CH値 … 10CH 手動スライダー\n"
            "※ タイムライン（曲・ポン出し）は統合アプリの「共通 → タイムライン」タブ。"
            if self._shared_host is not None
            else (
                "接続：USB-DMX512（Open DMX / FTDI RS485 想定）\n"
                "シリアル：250000 bps・8データ・無奇偶・2ストップ。\n"
                "※ Enttec USB DMX PRO などは別プロトコルのため非対応。\n"
                "※ 本機 10CH にディマー専用CHはない。\n\n"
                "タブ構成：\n"
                "・操作 … 接続・モード・モーション・高さ\n"
                "・タイムライン … 音楽再生・ポン出し記録・プロジェクト保存\n"
                "・音声 … マイク連動と任意のファイル解析\n"
                "・CH値 … 10CH 手動スライダー"
            )
        )
        ttk.Label(pad, text=info, justify=tk.LEFT, wraplength=int(self._if_touch(520, 680))).pack(
            anchor="nw"
        )

    def _boot_sync_channels(self) -> None:
        for i in range(10):
            self._spin_from_var(i)
        self._club_dot_ch9 = max(0, min(255, self._read_channel_value(8)))

    def _apply_ch2_pattern_index(self, i: int) -> None:
        """pattern.txt の CH2 レンジ一覧のインデックス i を適用（中央値）。"""
        # ポン出し中にコンボ同期でモーションが止まらないようにする
        if not self._pattern_tile_override_active():
            self._stop_dot_motion()
        if i < 0 or i >= len(self._ch2_patterns):
            return
        s, e, _name = self._ch2_patterns[i]
        self._channel_vars[1].set((s + e) // 2)
        self._spin_from_var(1)

    def _apply_dot_scene_index(self, i: int) -> None:
        """クラブ疑似点の「位置」プリセットのみ適用（CH9 は self._club_dot_ch9 を使用）。"""
        if i < 0 or i >= len(CLUB_DOT_POSITION_PRESETS):
            return
        _name, ch6, ch7 = CLUB_DOT_POSITION_PRESETS[i]
        tup = _club_dot_full_tuple(ch6, ch7, self._club_dot_ch9)
        self._apply_full_preset(tup)
        cb = getattr(self, "_dot_scene_combo", None)
        if cb is not None:
            try:
                cb.current(i)
            except tk.TclError:
                pass

    def _apply_dot_ch9_pick(self, value: int) -> None:
        """点モード用 CH9 のみ変更（次回の位置適用にもこの色を載せる）。"""
        self._stop_dot_motion()
        v = max(0, min(255, int(value)))
        self._club_dot_ch9 = v
        self._channel_vars[8].set(v)
        self._spin_from_var(8)

    def _stop_dot_motion(self) -> None:
        if self._motion_after_id is not None:
            try:
                self.after_cancel(self._motion_after_id)
            except tk.TclError:
                pass
            self._motion_after_id = None
        self._motion_fn = None
        self._motion_seq += 1

    def _start_dot_motion(self) -> None:
        """単一点のまま CH6/CH7 を時間変化（プリセットにより CH9 も変える／固定も可）。"""
        self._stop_dot_motion()
        if not DOT_POINT_MOTIONS:
            return
        i = self._motion_combo.current()
        if i < 0:
            i = 0
        one = _dot_one_shot_horizontal_motion_index()
        if one is not None and i == one:
            bump_dot_hstep_slot_offset(self._audio_rng)
        if self._motion_apply_dot_base.get():
            self._apply_dot_look_channels()
        self._motion_fn = DOT_POINT_MOTIONS[i][1]
        self._motion_phase = 0.0
        sid = self._motion_seq
        self._run_motion_tick(sid)

    def _on_motion_combo_selected(self, _evt: tk.Event | None = None) -> None:
        """実行中にコンボで別モーションを選んだら、その場で切替え（停止ボタン不要）。土台チェックがオンなら CH1／CH2／CH8 をセット。"""
        if self._pattern_tile_override_active():
            # ポン出し中の UI 同期は stop/start せず高速切替のみ
            if not DOT_POINT_MOTIONS:
                return
            i = self._motion_combo.current()
            if i < 0:
                return
            self._switch_dot_motion_fast(i, reset_phase=True)
            return
        if self._motion_fn is None:
            return
        if not DOT_POINT_MOTIONS:
            return
        i = self._motion_combo.current()
        if i < 0:
            return
        one = _dot_one_shot_horizontal_motion_index()
        if one is not None and i == one:
            bump_dot_hstep_slot_offset(self._audio_rng)
        self._stop_dot_motion()
        if self._motion_apply_dot_base.get():
            self._apply_dot_look_channels()
        self._motion_fn = DOT_POINT_MOTIONS[i][1]
        self._motion_phase = 0.0
        sid = self._motion_seq
        self._run_motion_tick(sid)

    def _run_motion_tick(self, sid: int) -> None:
        if sid != self._motion_seq:
            return
        fn = self._motion_fn
        if fn is None:
            self._motion_after_id = None
            return
        try:
            sp = max(MOTION_SPEED_MIN, min(MOTION_SPEED_MAX, float(self._motion_speed.get())))
        except (tk.TclError, ValueError, TypeError):
            sp = 1.0
        phase_gain = 1.0
        snap_for_audio: object | None = None
        file_sync = self._file_playback_active and self._track_profile is not None
        # ポン出し中はライブ／事前解析のゲート・上書きを一切かけない
        tile_prio = self._pattern_tile_override_active()

        if file_sync:
            prof = self._track_profile
            elapsed = self._file_elapsed_sec()
            dur = float(getattr(prof, "duration_sec", 0.0))
            if elapsed >= dur:
                self._stop_file_sync_playback()
                file_sync = False
            else:
                snap_for_audio = snapshot_at_time(prof, elapsed)
                if not tile_prio:
                    if not snap_for_audio.valid or not snap_for_audio.music_active:
                        phase_gain = 0.0
                    self._drain_precalc_motion_events(float(elapsed), prof)

        if not file_sync and self._audio_enable.get() and self._audio_analyzer is not None:
            if self._audio_analyzer is not self._shared_audio:
                try:
                    self._audio_analyzer.gate_sensitivity = float(self._audio_sensitivity.get())
                except (tk.TclError, TypeError, ValueError):
                    self._audio_analyzer.gate_sensitivity = 1.0
            snap_for_audio = self._audio_analyzer.snapshot()
            if not tile_prio:
                if not snap_for_audio.valid or not snap_for_audio.music_active:
                    phase_gain = 0.0
                elif getattr(snap_for_audio, "laser_hold", False):
                    # 溜め中は動きを止め、後段で CH1 ブラックアウト
                    phase_gain = 0.0

        # 高いとがった電子音: 実測の反復位相で左右を切替（速さ＝その音の反復）
        stutter_idxs = _dot_synth_stutter_motion_indices()
        three_idxs = _dot_three_point_lr_motion_indices()
        try:
            cur_mi = int(self._motion_combo.current())
        except (tk.TclError, TypeError, ValueError):
            cur_mi = -1
        on_triplet_motion = bool(stutter_idxs) and cur_mi in stutter_idxs
        on_three_point = bool(three_idxs) and cur_mi in three_idxs
        try:
            cur_name = DOT_POINT_MOTIONS[cur_mi][0] if 0 <= cur_mi < len(DOT_POINT_MOTIONS) else ""
        except Exception:
            cur_name = ""
        on_ref_fan = "4点上空ファン" in cur_name
        triplet_pulse = False
        if snap_for_audio is not None and phase_gain > 0 and not tile_prio:
            try:
                triplet_pulse = bool(is_triplet_synth_pulse(snap_for_audio))
            except Exception:
                triplet_pulse = False
        # 参考4点ファン中は左右パン上書きしない（扇形を壊さない）
        use_triplet_pan = (
            (not tile_prio)
            and phase_gain > 0
            and (on_triplet_motion or triplet_pulse)
            and not on_ref_fan
        )

        # 参考ファンは動画実測速度にロック（タイル優先中はロックしない）
        if on_ref_fan and not tile_prio:
            sp = _REF_FAN_SPEED_LOCK
            try:
                self._motion_speed.set(sp)
                self._motion_speed_scale.set(sp)
            except (tk.TclError, AttributeError):
                pass

        self._motion_phase += _MOTION_DPH_BASE * sp * phase_gain
        step = fn(self._motion_phase)
        ch6_opt = step[0]
        ch7_opt = step[1]
        ch9_opt = step[2]
        if use_triplet_pan and snap_for_audio is not None:
            try:
                side = int(synth_pulse_side(snap_for_audio))
            except Exception:
                try:
                    phasor = float(getattr(snap_for_audio, "beat_phasor", 0.0)) % 1.0
                    side = int(phasor * 3.0 + 1e-9) % 2
                except (TypeError, ValueError):
                    side = 0
            # 三点シェイク中（または振れ検出時）は単点端飛ばしではなく三点同調振れ
            if on_three_point or (triplet_pulse and three_idxs):
                try:
                    ph2 = float(getattr(snap_for_audio, "synth_pulse_phasor", 0.0))
                except (TypeError, ValueError):
                    ph2 = float(side)
                # -1（左）〜 +1（右）へ滑らかに
                swing = math.sin(ph2 * math.pi) if ph2 > 0 else (-1.0 if side == 0 else 1.0)
                if abs(swing) < 1e-6:
                    swing = -1.0 if side == 0 else 1.0
                ch6_opt = _norm_sx01_wide(_three_point_lr_sx(self._motion_phase, swing))
                ch7_opt = _norm_sy01(_MOTION_HORIZ_DEFAULT_SY)
            else:
                ch6_opt = _norm_sx01_shake(0.05 if side == 0 else 0.95)
                ch7_opt = _norm_sy01(_MOTION_HORIZ_DEFAULT_SY)
        ch8_opt = step[3] if len(step) > 3 else None
        ch2_opt = step[4] if len(step) > 4 else None
        ch4_opt = step[5] if len(step) > 5 else None
        ch1_opt = step[6] if len(step) > 6 else None
        ch3_opt = step[7] if len(step) > 7 else None
        # ほとんどのモーションは内蔵色ローテを持っているが、点モードの色は
        # パレット／CH9 スライダー（_club_dot_ch9）を優先する。
        # 「色のみ」「オート色」「参考4点ファン」だけモーション側の色を出す。
        if ch9_opt is not None and not _motion_name_drives_ch9(cur_name):
            ch9_opt = None
        # 音声連動／ファイル同期中は図形CH2／回転CH3を使わず疑似点のまま位置だけで制御
        # ※ポン出し優先中は登録モーションの CH2/CH8/CH3 をそのまま出す（ライブの疑似点ロックを外す）
        point_lock = (not tile_prio) and bool(file_sync or self._audio_enable.get())
        if point_lock:
            ch2_opt = None
            ch3_opt = None
            # サイズCH8も疑似点固定（点モーションが触らない）
            if ch8_opt is not None:
                ch8_opt = None
            # 土台が崩れていたら戻す
            try:
                if int(self._channel_vars[1].get()) != _DOT_BASE_CH2:
                    self._channel_vars[1].set(_DOT_BASE_CH2)
                    self._spin_from_var(1)
                    self._sync_ch2_combo_from_value(_DOT_BASE_CH2)
                if int(self._channel_vars[7].get()) != _DOT_BASE_CH8:
                    self._channel_vars[7].set(_DOT_BASE_CH8)
                    self._spin_from_var(7)
            except (tk.TclError, TypeError, ValueError):
                pass
        if ch6_opt is not None:
            self._channel_vars[5].set(max(0, min(255, int(ch6_opt))))
        if ch7_opt is not None:
            self._channel_vars[6].set(max(0, min(255, int(ch7_opt))))
        # 音声連動／ファイル同期中はモーションの色を無視し、後段で LED 色を適用する
        # タイル優先中はタイル側 CH9／モーション色を維持
        led_color_drive = (not tile_prio) and (
            file_sync or (self._audio_enable.get() and self._audio_analyzer is not None)
        )
        # 参考4点ファンは動画どおり緑／紫をモーション側で出す
        if on_ref_fan:
            led_color_drive = False
        if ch9_opt is not None and not led_color_drive:
            self._channel_vars[8].set(ch9_opt)
        if ch8_opt is not None:
            self._channel_vars[7].set(max(0, min(255, int(ch8_opt))))
        if ch2_opt is not None:
            self._channel_vars[1].set(max(0, min(255, int(ch2_opt))))
        if ch4_opt is not None:
            self._channel_vars[3].set(max(0, min(255, int(ch4_opt))))
        if ch1_opt is not None:
            self._channel_vars[0].set(max(0, min(255, int(ch1_opt))))
        if ch3_opt is not None:
            self._channel_vars[2].set(max(0, min(255, int(ch3_opt))))
        if ch6_opt is not None:
            self._spin_from_var(5)
        if ch7_opt is not None:
            self._spin_from_var(6)
        if ch9_opt is not None and not led_color_drive:
            self._spin_from_var(8)
        if ch8_opt is not None:
            self._spin_from_var(7)
        if ch2_opt is not None:
            self._spin_from_var(1)
            self._sync_ch2_combo_from_value(int(ch2_opt))
        if ch4_opt is not None:
            self._spin_from_var(3)
        if ch1_opt is not None:
            self._spin_from_var(0)
        if ch3_opt is not None:
            self._spin_from_var(2)
        if (
            not tile_prio
            and not file_sync
            and snap_for_audio is not None
            and getattr(snap_for_audio, "valid", False)
            and getattr(snap_for_audio, "music_active", False)
            and not getattr(snap_for_audio, "laser_hold", False)
        ):
            pew_g = int(getattr(snap_for_audio, "laser_pew_generation", 0))
            imp_g = int(getattr(snap_for_audio, "impact_generation", 0))
            shake_g = int(getattr(snap_for_audio, "shake_generation", 0))
            if pew_g != self._last_laser_pew_generation:
                self._last_laser_pew_generation = pew_g
                self._last_impact_generation = imp_g
                self._last_shake_generation = shake_g
                idx = pick_laser_pew_motion_index(snap_for_audio, rng=self._audio_rng)
                self._apply_motion_index_for_audio(idx)
                self._motion_phase = 0.0
            elif shake_g != self._last_shake_generation:
                self._last_shake_generation = shake_g
                self._last_impact_generation = imp_g
                idx = pick_shake_horizontal_index(snap_for_audio, rng=self._audio_rng)
                self._apply_motion_index_for_audio(idx)
                self._motion_phase = 0.0
            elif imp_g != self._last_impact_generation:
                self._last_impact_generation = imp_g
                idx = pick_impact_motion_index(snap_for_audio, rng=self._audio_rng)
                self._apply_motion_index_for_audio(idx)
                self._motion_phase = 0.0
        # ポン出し中は色・速度・溜め消灯など音声リアクティブを一切かけない
        if not tile_prio:
            if file_sync and snap_for_audio is not None:
                self._apply_audio_reactive_step(snap_for_audio, from_precalc_file=True)
            elif self._audio_enable.get() and not file_sync:
                self._apply_audio_reactive_step()
        if sid != self._motion_seq or self._motion_fn is None:
            self._motion_after_id = None
            return
        self._motion_after_id = self.after(36, lambda s=sid: self._run_motion_tick(s))

    def _file_elapsed_sec(self) -> float:
        """現在の曲位置（秒）。再生中はサンプル位置を優先。"""
        if self._file_playback_active and self._tl_sr > 0:
            return max(0.0, float(self._tl_play_sample) / float(self._tl_sr))
        if not self._file_playback_active:
            try:
                return float(self._punch_timeline_var.get())
            except (tk.TclError, TypeError, ValueError):
                return 0.0
        return max(0.0, self._file_seek_offset + (time.monotonic() - self._file_t0))

    def _punch_tile_to_dict(self, tile: PatternTileState) -> dict[str, Any]:
        d = asdict(tile)
        d["channels"] = list(d["channels"])
        return d

    def _punch_record_on_press(self, source: str, tile: PatternTileState) -> None:
        """押下瞬間を高精度で記録（オートリピート／既に保持中は呼ばれない）。"""
        if source.startswith("cue:"):
            return
        try:
            if not bool(self._punch_record_enabled.get()):
                return
        except tk.TclError:
            return
        if not self._file_playback_active:
            return
        if source in self._punch_pending:
            return
        # 再生位置はサンプル基準（自動再生と同じ時計）
        t = max(0.0, self._file_elapsed_sec())
        mono = time.monotonic()
        self._punch_pending[source] = {
            "t": t,
            "mono": mono,
            "tile": self._punch_tile_to_dict(tile),
            "recorded_at": datetime.now().isoformat(timespec="milliseconds"),
            "cue_id": f"{time.time_ns()}-{source}",
        }

    def _punch_record_on_release(self, source: str) -> None:
        pend = self._punch_pending.pop(source, None)
        if pend is None:
            return
        hold = max(0.0, time.monotonic() - float(pend["mono"]))
        cue = PunchCue(
            t=float(pend["t"]),
            hold_sec=hold,
            recorded_at=str(pend["recorded_at"]),
            cue_id=str(pend["cue_id"]),
            tile=dict(pend["tile"]),
        )
        # 連打でも欠かさず挿入（時刻順を維持）
        self._punch_cues.append(cue)
        self._punch_cues.sort(key=lambda c: (c.t, c.cue_id))
        self._refresh_punch_cue_list(select_id=cue.cue_id)
        self._append_precalc_log(
            f"[ポン出し記録] t={cue.t:.3f}s hold={cue.hold_sec:.3f}s "
            f"{cue.tile.get('title', '?')} @ {cue.recorded_at}"
        )

    def _refresh_punch_cue_list(self, select_id: str | None = None) -> None:
        lb = getattr(self, "_punch_cue_list", None)
        if lb is None:
            return
        lb.delete(0, tk.END)
        sel = 0
        for i, c in enumerate(self._punch_cues):
            lb.insert(tk.END, c.label())
            if select_id is not None and c.cue_id == select_id:
                sel = i
        n = len(self._punch_cues)
        self._punch_status.set(f"ポン出しキーポイント: {n} 件")
        if n and select_id is not None:
            lb.selection_clear(0, tk.END)
            lb.selection_set(sel)
            lb.see(sel)
        self._tl_redraw_editor()

    def _punch_cues_clear(self) -> None:
        if self._punch_cues and not messagebox.askyesno(
            "全消去", "ポン出しキーポイントをすべて消しますか？", parent=self
        ):
            return
        self._punch_cues.clear()
        self._punch_pending.clear()
        self._refresh_punch_cue_list()

    def _punch_cues_delete_selected(self) -> None:
        lb = getattr(self, "_punch_cue_list", None)
        ids: set[str] = set()
        if lb is not None:
            for i in lb.curselection():
                if 0 <= int(i) < len(self._punch_cues):
                    ids.add(self._punch_cues[int(i)].cue_id)
        if self._tl_selected_cue_id:
            ids.add(self._tl_selected_cue_id)
        if not ids:
            return
        self._punch_cues = [c for c in self._punch_cues if c.cue_id not in ids]
        self._tl_selected_cue_id = None
        self._refresh_punch_cue_list()

    def _punch_cues_default_path(self) -> Path:
        if self._tl_audio_path:
            p = Path(self._tl_audio_path)
            return p.with_name(p.name + ".punch_cues.json")
        prof = self._track_profile
        if prof is not None:
            src = getattr(prof, "source_path", None)
            if src:
                p = Path(str(src))
                return p.with_name(p.name + ".punch_cues.json")
        return Path(__file__).resolve().parent / "punch_cues.json"

    def _punch_cues_save(self) -> None:
        path = filedialog.asksaveasfilename(
            parent=self,
            title="ポン出しキーポイントを保存",
            defaultextension=".json",
            initialfile=self._punch_cues_default_path().name,
            filetypes=[("JSON", "*.json"), ("すべて", "*.*")],
        )
        if not path:
            return
        payload = {
            "version": 1,
            "source_path": str(self._tl_audio_path or getattr(self._track_profile, "source_path", "") or ""),
            "duration_sec": float(self._punch_track_duration()),
            "saved_at": datetime.now().isoformat(timespec="seconds"),
            "cues": [
                {
                    "t": c.t,
                    "hold_sec": c.hold_sec,
                    "recorded_at": c.recorded_at,
                    "cue_id": c.cue_id,
                    "tile": c.tile,
                }
                for c in self._punch_cues
            ],
        }
        try:
            Path(path).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            self._append_precalc_log(f"[ポン出し] 保存: {path}（{len(self._punch_cues)} 件）")
        except OSError as e:
            messagebox.showerror("保存失敗", str(e), parent=self)

    def _punch_cues_load(self) -> None:
        path = filedialog.askopenfilename(
            parent=self,
            title="ポン出しキーポイントを読み込み",
            filetypes=[("JSON", "*.json"), ("すべて", "*.*")],
        )
        if not path:
            return
        self._punch_cues_load_path(Path(path))

    def _punch_cues_load_path(self, path: Path) -> None:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            messagebox.showerror("読込失敗", str(e), parent=self)
            return
        cues_raw = raw.get("cues") if isinstance(raw, dict) else raw
        if not isinstance(cues_raw, list):
            messagebox.showerror("読込失敗", "キーポイント一覧が不正です。", parent=self)
            return
        out: list[PunchCue] = []
        for item in cues_raw:
            if not isinstance(item, dict):
                continue
            tile = item.get("tile")
            if not isinstance(tile, dict):
                continue
            try:
                out.append(
                    PunchCue(
                        t=float(item.get("t", 0.0)),
                        hold_sec=max(0.0, float(item.get("hold_sec", 0.05))),
                        recorded_at=str(item.get("recorded_at", "")),
                        cue_id=str(item.get("cue_id") or f"load-{time.time_ns()}-{len(out)}"),
                        tile=dict(tile),
                    )
                )
            except (TypeError, ValueError):
                continue
        out.sort(key=lambda c: (c.t, c.cue_id))
        self._punch_cues = out
        self._punch_play_idx = 0
        self._refresh_punch_cue_list()
        self._append_precalc_log(f"[ポン出し] 読込: {path}（{len(out)} 件）")

    def _timeline_project_default_name(self) -> str:
        if self._tl_audio_path:
            return Path(self._tl_audio_path).stem + ".timeline.json"
        return "project.timeline.json"

    def _timeline_project_save(self) -> None:
        """曲ファイル＋ポン出しキューをセットで保存（音声は同フォルダへコピー）。"""
        audio_src = self._tl_audio_path
        if not audio_src or self._tl_audio_mono is None:
            messagebox.showwarning(
                "保存不可",
                "先に音楽ファイルを開いてください。",
                parent=self,
            )
            return
        src_path = Path(audio_src)
        if not src_path.is_file():
            # メモリ上のみの場合は書き出せない
            messagebox.showerror(
                "保存不可",
                "元の音楽ファイルが見つかりません。もう一度「音楽を開く」から読み込んでください。",
                parent=self,
            )
            return
        path = filedialog.asksaveasfilename(
            parent=self,
            title="タイムラインプロジェクトを保存",
            defaultextension=".json",
            initialfile=self._timeline_project_default_name(),
            filetypes=[
                ("タイムラインプロジェクト", "*.timeline.json *.json"),
                ("JSON", "*.json"),
                ("すべて", "*.*"),
            ],
        )
        if not path:
            return
        proj = Path(path)
        audio_dest = proj.with_name(proj.stem + src_path.suffix)
        try:
            if src_path.resolve() != audio_dest.resolve():
                shutil.copy2(src_path, audio_dest)
        except OSError as e:
            messagebox.showerror("音声コピー失敗", str(e), parent=self)
            return
        payload = {
            "version": 2,
            "kind": "lightsystem_timeline",
            "audio_file": audio_dest.name,
            "audio_path_original": str(src_path),
            "duration_sec": float(self._punch_track_duration()),
            "sr": int(self._tl_sr),
            "zoom": float(self._tl_zoom),
            "view_start": float(self._tl_view_start),
            "saved_at": datetime.now().isoformat(timespec="seconds"),
            "cues": [
                {
                    "t": c.t,
                    "hold_sec": c.hold_sec,
                    "recorded_at": c.recorded_at,
                    "cue_id": c.cue_id,
                    "tile": c.tile,
                }
                for c in self._punch_cues
            ],
        }
        try:
            proj.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as e:
            messagebox.showerror("保存失敗", str(e), parent=self)
            return
        self._tl_status.set(
            f"保存済: {proj.name} ＋ {audio_dest.name}（キュー {len(self._punch_cues)} 件）"
        )
        self._append_precalc_log(
            f"[タイムライン] プロジェクト保存: {proj}（audio={audio_dest.name}, cues={len(self._punch_cues)}）"
        )
        messagebox.showinfo(
            "保存完了",
            f"プロジェクトを保存しました。\n\n{proj.name}\n{audio_dest.name}\nキュー {len(self._punch_cues)} 件",
            parent=self,
        )

    def _timeline_project_load(self) -> None:
        path = filedialog.askopenfilename(
            parent=self,
            title="タイムラインプロジェクトを開く",
            filetypes=[
                ("タイムラインプロジェクト", "*.timeline.json *.json"),
                ("JSON", "*.json"),
                ("すべて", "*.*"),
            ],
        )
        if not path:
            return
        self._timeline_project_load_path(Path(path))

    def _timeline_project_load_path(self, path: Path) -> None:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            messagebox.showerror("読込失敗", str(e), parent=self)
            return
        if not isinstance(raw, dict):
            messagebox.showerror("読込失敗", "プロジェクト形式が不正です。", parent=self)
            return

        # 音声パス解決
        audio_name = str(raw.get("audio_file") or "").strip()
        audio_orig = str(raw.get("audio_path_original") or raw.get("source_path") or "").strip()
        candidates: list[Path] = []
        if audio_name:
            candidates.append(path.parent / audio_name)
        if audio_orig:
            candidates.append(Path(audio_orig))
        audio_path: Path | None = next((p for p in candidates if p.is_file()), None)
        if audio_path is None:
            messagebox.showerror(
                "読込失敗",
                "プロジェクトに紐づく音楽ファイルが見つかりません。\n"
                f"探し先: {audio_name or '(なし)'} / {audio_orig or '(なし)'}",
                parent=self,
            )
            return

        # キュー
        cues_raw = raw.get("cues")
        out: list[PunchCue] = []
        if isinstance(cues_raw, list):
            for item in cues_raw:
                if not isinstance(item, dict):
                    continue
                tile = item.get("tile")
                if not isinstance(tile, dict):
                    continue
                try:
                    out.append(
                        PunchCue(
                            t=float(item.get("t", 0.0)),
                            hold_sec=max(0.0, float(item.get("hold_sec", 0.05))),
                            recorded_at=str(item.get("recorded_at", "")),
                            cue_id=str(item.get("cue_id") or f"load-{time.time_ns()}-{len(out)}"),
                            tile=dict(tile),
                        )
                    )
                except (TypeError, ValueError):
                    continue
        out.sort(key=lambda c: (c.t, c.cue_id))

        # 音声読込
        if load_audio_for_playback is None:
            messagebox.showerror(
                "読込失敗",
                "音声読込モジュールがありません。",
                parent=self,
            )
            return
        target_sr = int(raw.get("sr") or 44100)
        loaded = load_audio_for_playback(str(audio_path), target_sr=target_sr if target_sr > 0 else 44100)
        if loaded is None:
            messagebox.showerror(
                "読込失敗",
                f"音声を読めませんでした:\n{audio_path}",
                parent=self,
            )
            return
        y, sr, dur = loaded
        self._set_timeline_audio(str(audio_path), y, sr, dur, clear_cues=True)
        self._punch_cues = out
        self._punch_play_idx = 0
        try:
            self._tl_zoom = max(1.0, min(64.0, float(raw.get("zoom", 1.0))))
            self._tl_view_start = max(0.0, float(raw.get("view_start", 0.0)))
        except (TypeError, ValueError):
            self._tl_zoom = 1.0
            self._tl_view_start = 0.0
        self._tl_clamp_view()
        self._tl_refresh_zoom_label()
        self._refresh_punch_cue_list()
        self._append_precalc_log(
            f"[タイムライン] プロジェクト読込: {path}（audio={audio_path.name}, cues={len(out)}）"
        )
        messagebox.showinfo(
            "読込完了",
            f"{audio_path.name}\nキュー {len(out)} 件",
            parent=self,
        )

    def _on_tl_volume_change(self, _v: str | None = None) -> None:
        try:
            v = int(round(float(self._tl_volume.get())))
        except (tk.TclError, TypeError, ValueError):
            v = 80
        v = max(0, min(100, v))
        self._tl_volume_label.set(f"{v}%")
        # オーディオコールバックはキャッシュだけ読む（Tk 変数に触らない）
        self._tl_volume_gain_cached = v / 100.0

    def _tl_volume_gain(self) -> float:
        g = float(self._tl_volume_gain_cached)
        if g < 0.0:
            return 0.0
        if g > 1.0:
            return 1.0
        return g

    def _tl_stop_stream(self, *, abort: bool = True) -> None:
        """ストリーム停止。UI スレッドからのみ呼ぶ。sd.stop() は使わない（他ストリームを巻き込む）。"""
        stream = self._tl_stream
        self._tl_stream = None
        if stream is None:
            return
        try:
            if abort:
                try:
                    stream.abort()
                except TypeError:
                    stream.abort(ignore_errors=True)  # type: ignore[call-arg]
                except Exception:
                    try:
                        stream.stop()
                    except Exception:
                        pass
            else:
                try:
                    stream.stop()
                except Exception:
                    pass
        except Exception:
            pass
        try:
            stream.close()
        except Exception:
            pass

    def _tl_output_callback(self, outdata, frames, _time_info, _status) -> None:  # type: ignore[no-untyped-def]
        """再生コールバック：Tk には触れない。"""
        try:
            import numpy as np
        except ImportError:
            outdata.fill(0)
            raise sd.CallbackStop  # type: ignore[misc]

        if not self._file_playback_active:
            outdata.fill(0)
            raise sd.CallbackAbort  # type: ignore[misc]

        audio = self._tl_audio_mono
        if audio is None or sd is None:
            outdata.fill(0)
            raise sd.CallbackAbort  # type: ignore[misc]
        pos = int(self._tl_play_sample)
        n = int(getattr(audio, "size", 0))
        if pos >= n or n < 1:
            outdata.fill(0)
            raise sd.CallbackStop  # type: ignore[misc]
        end = min(n, pos + int(frames))
        gain = self._tl_volume_gain_cached
        try:
            chunk = np.asarray(audio[pos:end], dtype=np.float32)
            if gain < 0.999:
                chunk = chunk * float(gain)
            outdata.fill(0.0)
            length = int(chunk.size)
            if length > 0:
                if outdata.ndim == 1:
                    outdata[:length] = chunk
                else:
                    outdata[:length, 0] = chunk
                    if outdata.shape[1] > 1:
                        outdata[:length, 1] = chunk
        except Exception:
            outdata.fill(0)
            raise sd.CallbackAbort  # type: ignore[misc]
        self._tl_play_sample = end
        if end >= n:
            raise sd.CallbackStop  # type: ignore[misc]

    def _tl_on_stream_finished_gen(self, gen: int) -> None:
        # finished_callback 内では stop/close しない（デッドロック防止）
        try:
            self.after(0, lambda g=gen: self._tl_handle_stream_finished(g))
        except Exception:
            pass

    def _tl_handle_stream_finished(self, gen: int | None = None) -> None:
        if gen is not None and int(gen) != int(self._tl_transport_gen):
            return
        if not self._file_playback_active:
            return
        # 自然終端
        dur = self._punch_track_duration()
        try:
            self._punch_timeline_var.set(dur)
        except tk.TclError:
            pass
        try:
            self._punch_time_label.set(f"{dur:.2f} / {dur:.2f} s")
        except tk.TclError:
            pass
        self._stop_file_sync_playback(from_finished=True)

    def _tl_start_stream_at(self, t_sec: float) -> None:
        if sd is None:
            raise RuntimeError("sounddevice がありません")
        audio = self._tl_audio_mono
        sr = int(self._tl_sr)
        if audio is None or getattr(audio, "size", 0) < 1:
            raise RuntimeError("波形が空です")
        dur = self._punch_track_duration()
        t = max(0.0, min(dur, float(t_sec)))
        n = int(getattr(audio, "size", 0))
        start = int(t * sr)
        start = max(0, min(max(0, n - 1), start))
        # 旧ストリームの finished を無効化してから差し替え
        self._tl_transport_gen += 1
        self._tl_stop_stream(abort=True)
        self._tl_play_sample = start
        self._file_seek_offset = t
        self._file_t0 = time.monotonic()
        try:
            self._tl_volume_gain_cached = max(0.0, min(1.0, float(self._tl_volume.get()) / 100.0))
        except (tk.TclError, TypeError, ValueError):
            pass
        gen = int(self._tl_transport_gen)
        stream = sd.OutputStream(
            samplerate=sr,
            channels=1,
            dtype="float32",
            callback=self._tl_output_callback,
            finished_callback=lambda g=gen: self._tl_on_stream_finished_gen(g),
        )
        self._tl_stream = stream
        stream.start()

    def _timeline_stop_to_start(self) -> None:
        was = self._file_playback_active
        if was:
            self._stop_file_sync_playback()
        try:
            self._punch_timeline_var.set(0.0)
        except tk.TclError:
            pass
        self._tl_play_sample = 0
        self._punch_time_label.set(f"0.00 / {self._punch_track_duration():.2f} s")
        self._resync_event_indices(0.0)
        self._tl_redraw_editor()

    def _tl_compute_wave_peaks(self, columns: int = 800) -> None:
        audio = self._tl_audio_mono
        if audio is None or getattr(audio, "size", 0) < 2:
            self._tl_wave_peaks = None
            return
        try:
            import numpy as np

            y = np.asarray(audio, dtype=np.float32)
            n = int(y.size)
            cols = max(64, min(int(columns), 2000))
            step = max(1, n // cols)
            peaks: list[float] = []
            for i in range(cols):
                a = i * step
                b = min(n, a + step)
                if b <= a:
                    peaks.append(0.0)
                    continue
                seg = y[a:b]
                peaks.append(float(np.max(np.abs(seg))))
            mx = max(peaks) if peaks else 1.0
            if mx < 1e-9:
                mx = 1.0
            self._tl_wave_peaks = [p / mx for p in peaks]
        except Exception:
            self._tl_wave_peaks = None

    def _tl_fmt_time(self, sec: float) -> str:
        sec = max(0.0, float(sec))
        m = int(sec // 60)
        s = sec - m * 60
        return f"{m}:{s:05.2f}"

    def _tl_visible_duration(self) -> float:
        dur = self._punch_track_duration()
        if dur <= 0:
            return 1.0
        return max(0.05, dur / max(1.0, float(self._tl_zoom)))

    def _tl_clamp_view(self) -> None:
        dur = self._punch_track_duration()
        vis = self._tl_visible_duration()
        if dur <= 0:
            self._tl_view_start = 0.0
            return
        max_start = max(0.0, dur - vis)
        self._tl_view_start = max(0.0, min(max_start, float(self._tl_view_start)))

    def _tl_refresh_zoom_label(self) -> None:
        try:
            self._tl_zoom_label.set(f"{int(round(self._tl_zoom * 100))}%")
        except tk.TclError:
            pass

    def _tl_zoom_fit(self) -> None:
        self._tl_zoom = 1.0
        self._tl_view_start = 0.0
        self._tl_refresh_zoom_label()
        self._tl_redraw_editor()

    def _tl_zoom_by(self, factor: float, *, anchor_t: float | None = None) -> None:
        dur = self._punch_track_duration()
        if dur <= 0:
            return
        if anchor_t is None:
            try:
                anchor_t = float(self._punch_timeline_var.get())
            except (tk.TclError, TypeError, ValueError):
                anchor_t = self._tl_view_start + self._tl_visible_duration() * 0.5
        old_vis = self._tl_visible_duration()
        frac = 0.5
        if old_vis > 1e-9:
            frac = (float(anchor_t) - self._tl_view_start) / old_vis
            frac = max(0.0, min(1.0, frac))
        self._tl_zoom = max(1.0, min(64.0, float(self._tl_zoom) * float(factor)))
        new_vis = self._tl_visible_duration()
        self._tl_view_start = float(anchor_t) - frac * new_vis
        self._tl_clamp_view()
        self._tl_refresh_zoom_label()
        self._tl_redraw_editor()

    def _tl_ensure_time_visible(self, t: float, *, margin: float = 0.08) -> bool:
        """プレイヘッドが見えるよう横スクロール。変更したら True。"""
        dur = self._punch_track_duration()
        if dur <= 0 or self._tl_zoom <= 1.001:
            return False
        vis = self._tl_visible_duration()
        left = self._tl_view_start
        right = left + vis
        m = vis * margin
        changed = False
        if t < left + m:
            self._tl_view_start = t - m
            changed = True
        elif t > right - m:
            self._tl_view_start = t - vis + m
            changed = True
        if changed:
            self._tl_clamp_view()
        return changed

    def _tl_x_to_time(self, x: float) -> float:
        w = max(1, self._tl_editor_w)
        dur = self._punch_track_duration()
        vis = self._tl_visible_duration()
        t = self._tl_view_start + (float(x) / w) * vis
        return max(0.0, min(dur, t)) if dur > 0 else 0.0

    def _tl_time_to_x(self, t: float) -> float:
        w = max(1, self._tl_editor_w)
        vis = self._tl_visible_duration()
        if vis <= 1e-12:
            return 0.0
        return (float(t) - self._tl_view_start) / vis * w

    def _on_tl_editor_wheel(self, event: tk.Event) -> None:
        if self._punch_track_duration() <= 0:
            return
        delta = int(getattr(event, "delta", 0) or 0)
        if delta == 0:
            return
        factor = 1.25 if delta > 0 else 1 / 1.25
        anchor = self._tl_x_to_time(float(event.x))
        self._tl_zoom_by(factor, anchor_t=anchor)

    def _on_tl_pan_press(self, event: tk.Event) -> str:
        self._tl_pan_drag = True
        self._tl_pan_anchor_x = float(event.x)
        self._tl_pan_anchor_start = float(self._tl_view_start)
        return "break"

    def _on_tl_pan_drag(self, event: tk.Event) -> str:
        if not self._tl_pan_drag:
            return "break"
        w = max(1, self._tl_editor_w)
        vis = self._tl_visible_duration()
        dx = float(event.x) - self._tl_pan_anchor_x
        self._tl_view_start = self._tl_pan_anchor_start - (dx / w) * vis
        self._tl_clamp_view()
        self._tl_redraw_editor()
        return "break"

    def _on_tl_pan_release(self, _event: tk.Event | None = None) -> str:
        self._tl_pan_drag = False
        return "break"

    def _on_tl_editor_configure(self, event: tk.Event) -> None:
        if event.width < 40:
            return
        if abs(int(event.width) - self._tl_editor_w) < 2:
            return
        self._tl_editor_w = int(event.width)
        if self._tl_audio_mono is not None:
            self._tl_compute_wave_peaks(columns=max(200, self._tl_editor_w))
        self._tl_redraw_editor()

    def _on_tl_editor_press(self, event: tk.Event) -> None:
        # Shift+ドラッグはパン専用
        if int(getattr(event, "state", 0) or 0) & 0x0001:
            return
        hit = self._tl_hit_clip_at(float(event.x), float(event.y))
        if hit is not None:
            self._tl_select_cue_id(hit)
            # クリップ先頭へシーク
            for c in self._punch_cues:
                if c.cue_id == hit:
                    t = float(c.t)
                    try:
                        self._punch_timeline_var.set(t)
                    except tk.TclError:
                        pass
                    self._punch_time_label.set(f"{t:.2f} / {self._punch_track_duration():.2f} s")
                    if self._file_playback_active:
                        self._seek_file_playback(t)
                    else:
                        self._tl_redraw_editor(playhead_t=t)
                    return
        self._tl_drag_seek = True
        self._punch_scrubbing = True
        t = self._tl_x_to_time(event.x)
        try:
            self._punch_timeline_var.set(t)
        except tk.TclError:
            pass
        self._punch_time_label.set(f"{t:.2f} / {self._punch_track_duration():.2f} s")
        self._tl_redraw_editor(playhead_t=t)

    def _on_tl_editor_drag(self, event: tk.Event) -> None:
        if not self._tl_drag_seek:
            return
        t = self._tl_x_to_time(event.x)
        try:
            self._punch_timeline_var.set(t)
        except tk.TclError:
            pass
        self._punch_time_label.set(f"{t:.2f} / {self._punch_track_duration():.2f} s")
        self._tl_redraw_editor(playhead_t=t)

    def _on_tl_editor_release(self, event: tk.Event) -> None:
        self._tl_drag_seek = False
        self._punch_scrubbing = False
        t = self._tl_x_to_time(event.x)
        try:
            self._punch_timeline_var.set(t)
        except tk.TclError:
            pass
        if self._file_playback_active:
            self._seek_file_playback(t)
        else:
            self._resync_event_indices(t)
            self._tl_redraw_editor(playhead_t=t)

    def _on_punch_cue_list_select(self, _e: tk.Event | None = None) -> None:
        lb = getattr(self, "_punch_cue_list", None)
        if lb is None:
            return
        sel = lb.curselection()
        if not sel:
            return
        i = int(sel[0])
        if 0 <= i < len(self._punch_cues):
            c = self._punch_cues[i]
            self._tl_selected_cue_id = c.cue_id
            t = float(c.t)
            try:
                self._punch_timeline_var.set(t)
            except tk.TclError:
                pass
            if self._file_playback_active:
                self._seek_file_playback(t)
            else:
                self._punch_time_label.set(f"{t:.2f} / {self._punch_track_duration():.2f} s")
                self._tl_redraw_editor(playhead_t=t)

    def _tl_assign_clip_lanes(self) -> dict[str, int]:
        """重ならないようクリップをレーン割当（動画編集のトラック風）。"""
        items = sorted(self._punch_cues, key=lambda c: (c.t, c.cue_id))
        lane_ends: list[float] = []
        out: dict[str, int] = {}
        for c in items:
            start = float(c.t)
            end = start + max(0.04, float(c.hold_sec))
            placed = False
            for i, e in enumerate(lane_ends):
                if start >= e - 1e-4:
                    lane_ends[i] = end
                    out[c.cue_id] = i
                    placed = True
                    break
            if not placed:
                out[c.cue_id] = len(lane_ends)
                lane_ends.append(end)
        return out

    def _tl_contrast_text(self, hex_color: str) -> str:
        try:
            h = hex_color.lstrip("#")
            r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
            lum = 0.299 * r + 0.587 * g + 0.114 * b
            return "#111111" if lum > 160 else "#ffffff"
        except Exception:
            return "#ffffff"

    def _tl_hit_clip_at(self, x: float, y: float) -> str | None:
        for cue_id, x0, x1, y0, y1 in self._tl_clip_hits:
            if x0 <= x <= x1 and y0 <= y <= y1:
                return cue_id
        return None

    def _tl_select_cue_id(self, cue_id: str | None) -> None:
        self._tl_selected_cue_id = cue_id
        lb = getattr(self, "_punch_cue_list", None)
        if lb is None or cue_id is None:
            self._tl_redraw_editor()
            return
        for i, c in enumerate(self._punch_cues):
            if c.cue_id == cue_id:
                lb.selection_clear(0, tk.END)
                lb.selection_set(i)
                lb.see(i)
                break
        self._tl_redraw_editor()

    def _tl_redraw_editor(self, playhead_t: float | None = None) -> None:
        cv = self._tl_editor_canvas
        if cv is None:
            return
        try:
            w = max(40, int(cv.winfo_width()))
        except tk.TclError:
            return
        if w < 40:
            return
        self._tl_editor_w = w
        lanes = self._tl_assign_clip_lanes()
        n_lanes = max(1, (max(lanes.values()) + 1) if lanes else 1)
        n_lanes = min(n_lanes, 6)
        lane_h = self._tl_cue_lane_h
        ch = 6 + n_lanes * lane_h
        self._tl_cue_h = ch
        rh = self._tl_ruler_h
        wh = self._tl_wave_h
        h = rh + wh + ch
        try:
            cv.configure(height=h)
        except tk.TclError:
            pass
        cv.delete("all")
        self._tl_clip_hits = []

        # 背景レーン
        cv.create_rectangle(0, 0, w, rh, fill="#242830", outline="")
        cv.create_rectangle(0, rh, w, rh + wh, fill="#12151a", outline="")
        cv.create_rectangle(0, rh + wh, w, h, fill="#1a2030", outline="")
        cv.create_text(
            4,
            rh + wh + 10,
            text="CLIP",
            fill="#6a7380",
            anchor="w",
            font=("Segoe UI", 7),
        )
        for li in range(n_lanes):
            y = rh + wh + 4 + li * lane_h
            cv.create_line(0, y + lane_h - 1, w, y + lane_h - 1, fill="#2a3140")

        dur = self._punch_track_duration()
        self._tl_clamp_view()
        vis0 = self._tl_view_start
        vis = self._tl_visible_duration()
        vis1 = vis0 + vis
        # 時間定規（表示範囲に合わせて刻み）
        if dur > 0:
            if vis <= 8:
                step = 1.0
            elif vis <= 30:
                step = 5.0
            elif vis <= 120:
                step = 10.0
            elif vis <= 600:
                step = 30.0
            else:
                step = 60.0
            t0 = math.floor(vis0 / step) * step
            t = t0
            while t <= vis1 + 1e-6:
                x = self._tl_time_to_x(t)
                if -20 <= x <= w + 20:
                    cv.create_line(x, rh - 8, x, rh, fill="#6a7380")
                    cv.create_text(
                        x + 2,
                        4,
                        text=self._tl_fmt_time(t),
                        fill="#9aa0a6",
                        anchor="nw",
                        font=("Consolas", 7),
                    )
                t += step
        else:
            cv.create_text(
                w // 2,
                rh + wh // 2,
                text="音楽ファイルを開くと波形が表示されます",
                fill="#5a6270",
                font=("Segoe UI", 10),
            )

        # 波形（表示範囲だけ）
        peaks = self._tl_wave_peaks
        if peaks and dur > 0:
            mid = rh + wh / 2.0
            n = len(peaks)
            # キャンバス幅ぶんサンプリング（拡大時も滑らか）
            for xi in range(w):
                t = vis0 + (xi + 0.5) / w * vis
                pi = int(t / dur * n)
                pi = max(0, min(n - 1, pi))
                p = peaks[pi]
                amp = max(1.0, p * (wh * 0.45))
                cv.create_rectangle(
                    xi,
                    mid - amp,
                    xi + 1,
                    mid + amp,
                    fill="#3d8bfd",
                    outline="",
                )

        # ポン出しクリップ（幅＝押下時間）
        for c in self._punch_cues:
            li = int(lanes.get(c.cue_id, 0)) % n_lanes
            x0 = self._tl_time_to_x(c.t)
            hold = max(0.04, float(c.hold_sec))
            x1 = self._tl_time_to_x(c.t + hold)
            # 画面外はスキップ
            if x1 < -4 or x0 > w + 4:
                continue
            # 最低幅（短いタップでも見える）
            if x1 - x0 < 10:
                x1 = x0 + 10
            y0 = rh + wh + 5 + li * lane_h
            y1 = y0 + lane_h - 6
            fill = c.clip_color()
            selected = c.cue_id == self._tl_selected_cue_id
            outline = "#ffffff" if selected else "#0d1117"
            ow = 2 if selected else 1
            cv.create_rectangle(x0, y0, x1, y1, fill=fill, outline=outline, width=ow)
            # 左端のアクセント
            cv.create_rectangle(x0, y0, min(x0 + 3, x1), y1, fill="#000000", outline="")
            title = c.display_title()
            tw = x1 - x0
            if tw >= 28:
                cv.create_text(
                    x0 + 6,
                    (y0 + y1) / 2,
                    text=title,
                    fill=self._tl_contrast_text(fill),
                    anchor="w",
                    font=("Segoe UI", 8, "bold"),
                    width=max(20, int(tw - 10)),
                )
            self._tl_clip_hits.append((c.cue_id, x0, x1, y0, y1))

        # プレイヘッド（最前面）
        if playhead_t is None:
            try:
                playhead_t = float(self._punch_timeline_var.get())
            except (tk.TclError, TypeError, ValueError):
                playhead_t = 0.0
        self._tl_draw_playhead_marks(cv, float(playhead_t), h)
        self._tl_playhead_last_draw = float(playhead_t)

    def _tl_draw_playhead_marks(self, cv: tk.Canvas, playhead_t: float, h: int) -> None:
        px = self._tl_time_to_x(playhead_t)
        cv.create_line(px, 0, px, h, fill="#ff5c5c", width=2, tags=("playhead",))
        cv.create_polygon(
            px - 6, 0, px + 6, 0, px, 10, fill="#ff5c5c", outline="", tags=("playhead",)
        )

    def _tl_update_playhead(self, playhead_t: float) -> None:
        """再生中用: プレイヘッドだけ動かす（波形の全再描画を避ける）。"""
        cv = self._tl_editor_canvas
        if cv is None:
            return
        t = float(playhead_t)
        # 拡大中はプレイヘッドを追従（はみ出したら再描画）
        if self._tl_ensure_time_visible(t):
            self._tl_redraw_editor(playhead_t=t)
            return
        # 1px 未満の更新は間引き
        try:
            w = max(1, self._tl_editor_w)
        except Exception:
            w = 1
        vis = max(1e-6, self._tl_visible_duration())
        if abs(t - self._tl_playhead_last_draw) * w / vis < 0.5:
            return
        try:
            h = int(cv.winfo_height())
        except tk.TclError:
            return
        if h < 10:
            return
        cv.delete("playhead")
        self._tl_draw_playhead_marks(cv, t, h)
        self._tl_playhead_last_draw = t

    def _on_punch_timeline_drag(self, _v: str | None = None) -> None:
        # 互換（Scale廃止後も呼ばれうる）
        if not self._punch_scrubbing:
            return
        try:
            t = float(self._punch_timeline_var.get())
        except (tk.TclError, TypeError, ValueError):
            return
        dur = self._punch_track_duration()
        self._punch_time_label.set(f"{t:.2f} / {dur:.2f} s")

    def _on_punch_timeline_release(self, _event: tk.Event | None = None) -> None:
        self._punch_scrubbing = False
        try:
            t = float(self._punch_timeline_var.get())
        except (tk.TclError, TypeError, ValueError):
            return
        if self._file_playback_active:
            self._seek_file_playback(t)

    def _punch_track_duration(self) -> float:
        if self._tl_duration > 0:
            return float(self._tl_duration)
        prof = self._track_profile
        if prof is None:
            return 0.0
        return max(0.0, float(getattr(prof, "duration_sec", 0.0) or 0.0))

    def _set_timeline_audio(
        self,
        path: str,
        y: Any,
        sr: int,
        duration: float,
        *,
        clear_cues: bool = False,
    ) -> None:
        """タイムライン用オーディオをセット（解析不要）。"""
        was_playing = self._file_playback_active
        if was_playing:
            self._stop_file_sync_playback()
        self._tl_audio_path = str(path)
        self._tl_audio_mono = y
        self._tl_sr = int(sr)
        self._tl_duration = max(0.0, float(duration))
        name = Path(path).name
        self._tl_status.set(f"読込済: {name}  {self._tl_duration:.1f}s  {self._tl_sr} Hz")
        try:
            self._punch_timeline_var.set(0.0)
        except tk.TclError:
            pass
        self._tl_zoom = 1.0
        self._tl_view_start = 0.0
        self._tl_refresh_zoom_label()
        self._tl_compute_wave_peaks(columns=max(200, self._tl_editor_w))
        self._update_punch_timeline_range()
        self._tl_redraw_editor(playhead_t=0.0)
        bs = getattr(self, "_btn_play_sync", None)
        if bs is not None:
            try:
                bs.config(state=tk.NORMAL, text="▶ 再生")
            except tk.TclError:
                pass
        if clear_cues:
            self._punch_cues.clear()
            self._punch_pending.clear()
            self._refresh_punch_cue_list()
        else:
            auto = self._punch_cues_default_path()
            if auto.is_file() and not self._punch_cues:
                try:
                    self._punch_cues_load_path(auto)
                except Exception:
                    pass

    def _timeline_open_audio(self) -> None:
        path = filedialog.askopenfilename(
            parent=self,
            title="タイムライン用の音楽ファイル",
            filetypes=[
                ("音声", "*.wav;*.mp3;*.flac;*.ogg;*.m4a;*.aac;*.*"),
                ("すべて", "*.*"),
            ],
        )
        if not path:
            return
        if load_audio_for_playback is None:
            messagebox.showerror(
                "読込不可",
                "music_precalc が読み込めません。",
                parent=self,
            )
            return
        self._tl_status.set("読込中…")
        self.update_idletasks()

        def work() -> None:
            try:
                loaded = load_audio_for_playback(path, target_sr=44100)
                if loaded is None:
                    raise RuntimeError(
                        "音声を読めませんでした。pip install soundfile を試すか、WAV を指定してください。"
                    )
                y, sr, dur = loaded
                self.after(0, lambda: self._timeline_open_done(path, y, sr, dur, None))
            except Exception as e:
                self.after(0, lambda err=e: self._timeline_open_done(path, None, 0, 0.0, err))

        threading.Thread(target=work, daemon=True).start()

    def _timeline_open_done(
        self,
        path: str,
        y: Any,
        sr: int,
        dur: float,
        err: BaseException | None,
    ) -> None:
        if err is not None or y is None:
            self._tl_status.set("読込失敗")
            messagebox.showerror("読込失敗", str(err or "不明なエラー"), parent=self)
            return
        self._set_timeline_audio(path, y, sr, dur, clear_cues=False)
        self._append_precalc_log(f"[タイムライン] 読込: {path} ({dur:.2f}s)")

    def _sync_timeline_from_profile(self, prof: Any) -> None:
        """解析／JSON読込後、タイムラインにも同じ曲を載せる。"""
        path = str(getattr(prof, "source_path", "") or "")
        audio = getattr(prof, "audio_mono", None)
        sr = int(getattr(prof, "sr", 44100) or 44100)
        dur = float(getattr(prof, "duration_sec", 0.0) or 0.0)
        if audio is not None and getattr(audio, "size", 0) > 0 and dur > 0:
            self._set_timeline_audio(path or "analyzed", audio, sr, dur, clear_cues=False)
            return
        if path and load_audio_for_playback is not None:
            loaded = load_audio_for_playback(path, target_sr=sr if sr > 0 else 44100)
            if loaded is not None:
                y, sr2, dur2 = loaded
                self._set_timeline_audio(path, y, sr2, dur2, clear_cues=False)
                # profile にも載せておく
                try:
                    prof.audio_mono = y
                    prof.sr = int(sr2)
                except Exception:
                    pass

    def _update_punch_timeline_range(self) -> None:
        dur = max(0.0, self._punch_track_duration())
        self._punch_time_label.set(f"0.00 / {dur:.2f} s")
        self._tl_redraw_editor()

    def _cancel_punch_timeline_tick(self) -> None:
        jid = self._punch_timeline_after
        self._punch_timeline_after = None
        if jid is not None:
            try:
                self.after_cancel(jid)
            except (tk.TclError, ValueError):
                pass

    def _cancel_punch_release_jobs(self) -> None:
        for jid in list(self._punch_release_jobs.values()):
            try:
                self.after_cancel(jid)
            except (tk.TclError, ValueError):
                pass
        self._punch_release_jobs.clear()
        self._punch_cue_hold_until.clear()
        # 自動発火中の保持を解放
        for src in [s for s in list(self._pattern_tile_holds) if s.startswith("cue:")]:
            self._pattern_tile_hold_remove(src)

    def _punch_autoplay_ok(self) -> bool:
        try:
            return bool(self._punch_autoplay_enabled.get())
        except tk.TclError:
            return False

    def _start_punch_timeline_tick(self) -> None:
        self._cancel_punch_timeline_tick()
        self._punch_timeline_tick()

    def _punch_timeline_tick(self) -> None:
        self._punch_timeline_after = None
        if not self._file_playback_active:
            return
        elapsed = self._file_elapsed_sec()
        dur = self._punch_track_duration()
        if dur > 0 and elapsed >= dur - 0.02:
            # 終端
            if not self._punch_scrubbing:
                self._punch_timeline_var.set(dur)
                self._punch_time_label.set(f"{dur:.2f} / {dur:.2f} s")
                self._tl_update_playhead(dur)
            self._stop_file_sync_playback()
            return
        if not self._punch_scrubbing:
            try:
                self._punch_timeline_var.set(elapsed)
            except tk.TclError:
                pass
            self._punch_time_label.set(f"{elapsed:.2f} / {dur:.2f} s")
            # 再生中はプレイヘッドだけ動かす（波形全再描画は重い）
            self._tl_update_playhead(elapsed)
        self._drain_punch_cues(elapsed)
        self._release_expired_punch_cues(elapsed)
        try:
            self._punch_timeline_after = self.after(20, self._punch_timeline_tick)
        except tk.TclError:
            self._punch_timeline_after = None

    def _drain_punch_cues(self, elapsed: float) -> None:
        if not self._punch_autoplay_ok():
            return
        # 記録ONでも自動発火する（記録は手動押しだけ追加）
        cues = self._punch_cues
        i = self._punch_play_idx
        n = len(cues)
        while i < n:
            c = cues[i]
            if c.t > elapsed + 0.0005:
                break
            self._fire_punch_cue(c)
            i += 1
        self._punch_play_idx = i

    def _release_expired_punch_cues(self, elapsed: float) -> None:
        """曲位置ベースでキュー保持を解除（UI遅延で after が遅れても確実に消す）。"""
        expired = [s for s, until in self._punch_cue_hold_until.items() if elapsed >= until - 0.0005]
        for src in expired:
            self._punch_cue_hold_until.pop(src, None)
            jid = self._punch_release_jobs.pop(src, None)
            if jid is not None:
                try:
                    self.after_cancel(jid)
                except (tk.TclError, ValueError):
                    pass
            if src in self._pattern_tile_holds:
                self._pattern_tile_hold_remove(src)

    def _fire_punch_cue(self, cue: PunchCue, *, hold_until: float | None = None) -> None:
        tile = self._tile_from_dict(cue.tile)
        if tile is None:
            return
        src = f"cue:{cue.cue_id}"
        old = self._punch_release_jobs.pop(src, None)
        if old is not None:
            try:
                self.after_cancel(old)
            except (tk.TclError, ValueError):
                pass
        until = float(hold_until) if hold_until is not None else float(cue.t) + max(0.02, float(cue.hold_sec))
        self._punch_cue_hold_until[src] = until
        self._pattern_tile_hold_add(src, tile, force_reapply=True)
        # after はバックアップ（メインは曲位置で _release_expired_punch_cues）
        remain_ms = int(max(20.0, min(120_000.0, (until - self._file_elapsed_sec()) * 1000.0)))
        jid = self.after(remain_ms, lambda s=src: self._punch_cue_auto_release(s))
        self._punch_release_jobs[src] = jid

    def _punch_cue_auto_release(self, source: str) -> None:
        self._punch_release_jobs.pop(source, None)
        self._punch_cue_hold_until.pop(source, None)
        if source in self._pattern_tile_holds:
            self._pattern_tile_hold_remove(source)

    def _seek_file_playback(self, t_sec: float) -> None:
        """再生中にタイムライン位置へシーク。"""
        if not self._file_playback_active or sd is None:
            return
        audio = self._tl_audio_mono
        if audio is None or getattr(audio, "size", 0) < 1:
            return
        try:
            self._tl_start_stream_at(t_sec)
        except Exception as e:
            messagebox.showerror("シーク失敗", str(e), parent=self)
            return
        t = max(0.0, min(self._punch_track_duration(), float(t_sec)))
        self._resync_event_indices(t)
        try:
            self._punch_timeline_var.set(t)
        except tk.TclError:
            pass
        self._tl_update_playhead(t)

    def _resync_event_indices(self, t_sec: float) -> None:
        """再生位置に合わせて事前イベント／ポン出し再生カーソルを合わせる。"""
        t = float(t_sec)
        prof = self._track_profile
        if prof is not None:
            evs = getattr(prof, "events", None) or ()
            i = 0
            while i < len(evs) and float(evs[i][0]) <= t + 0.0005:
                i += 1
            self._precalc_event_idx = i
        self._sync_punch_autoplay_at(t)

    def _sync_punch_autoplay_at(self, t_sec: float) -> None:
        """シーク／再生開始位置のポン出しを同期（進行中クリップは残り時間で再点火）。"""
        # 既存の自動キューを一旦クリア
        for jid in list(self._punch_release_jobs.values()):
            try:
                self.after_cancel(jid)
            except (tk.TclError, ValueError):
                pass
        self._punch_release_jobs.clear()
        self._punch_cue_hold_until.clear()
        for src in [s for s in list(self._pattern_tile_holds) if s.startswith("cue:")]:
            self._pattern_tile_hold_remove(src)

        auto = self._punch_autoplay_ok()
        play_idx = 0
        for i, c in enumerate(self._punch_cues):
            if c.t > t_sec + 0.0005:
                play_idx = i
                break
            end = float(c.t) + max(0.02, float(c.hold_sec))
            if auto and end > t_sec + 0.0005:
                self._fire_punch_cue(c, hold_until=end)
            play_idx = i + 1
        else:
            play_idx = len(self._punch_cues)
        self._punch_play_idx = play_idx

    def _drain_precalc_motion_events(self, elapsed: float, prof: Any) -> None:
        evs = getattr(prof, "events", None) or ()
        i = self._precalc_event_idx
        while i < len(evs):
            t_ev, kind = evs[i]
            if float(t_ev) > elapsed + 0.0005:
                break
            i += 1
            dur = float(getattr(prof, "duration_sec", 0.0))
            t_snap = min(float(t_ev), max(0.0, dur - 1e-6))
            s_ev = snapshot_at_time(prof, t_snap)
            if kind == "pew":
                idx = pick_laser_pew_motion_index(s_ev, rng=self._audio_rng)
            elif kind == "shake":
                idx = pick_shake_horizontal_index(s_ev, rng=self._audio_rng)
            else:
                idx = pick_impact_motion_index(s_ev, rng=self._audio_rng)
            self._apply_motion_index_for_audio(idx)
            self._motion_phase = 0.0
        self._precalc_event_idx = i

    def _append_precalc_log(self, text: str) -> None:
        w = getattr(self, "_precalc_log", None)
        if w is None:
            return
        w.insert(tk.END, text + "\n")
        w.see(tk.END)

    def _precalc_analyze_pick(self) -> None:
        if not PRECALC_AVAILABLE or analyze_audio_file is None:
            detail = (
                "事前解析には Python パッケージ「librosa」と「soundfile」が必要です。\n\n"
                "PowerShell またはコマンドプロンプトで、"
                "このアプリを起動しているのと同じ python で次を実行してください:\n"
                "  python -m pip install librosa soundfile\n"
                "または:\n"
                "  python -m pip install -r requirements.txt\n\n"
                "インストール直後は、ここがまだ無効のままなら一度アプリを終了して起動し直してください"
                "（起動時に librosa の有無が決まります）。\n"
            )
            if not PRECALC_AVAILABLE:
                try:
                    __import__("librosa")
                except ImportError as e:
                    detail += f"\n（現在のエラー: {e}）"
            messagebox.showerror("解析には librosa が必要です", detail, parent=self)
            return
        path = filedialog.askopenfilename(
            parent=self,
            title="音楽ファイルを選択",
            filetypes=[
                ("音声", "*.wav;*.mp3;*.flac;*.ogg;*.m4a;*.aac;*.*"),
                ("すべて", "*.*"),
            ],
        )
        if not path:
            return
        self._btn_precalc_analyze.config(state=tk.DISABLED)
        self._precalc_status.set("解析中…（長い曲は数十秒かかります）")

        def work() -> None:
            try:
                prof = analyze_audio_file(path, keep_audio=True)  # type: ignore[misc]
                self.after(0, lambda: self._precalc_analyze_done(prof, None))
            except Exception as e:
                self.after(0, lambda err=e: self._precalc_analyze_done(None, err))

        threading.Thread(target=work, daemon=True).start()

    def _precalc_analyze_done(self, prof: Any | None, err: BaseException | None) -> None:
        self._btn_precalc_analyze.config(state=tk.NORMAL)
        if err is not None:
            messagebox.showerror("解析失敗", str(err), parent=self)
            self._precalc_status.set("解析に失敗しました")
            return
        if prof is None:
            return
        self._track_profile = prof
        self._precalc_status.set(
            f"解析済: {Path(str(getattr(prof, 'source_path', ''))).name}  "
            f"{getattr(prof, 'duration_sec', 0):.1f}s  {getattr(prof, 'tempo_bpm', 0):.0f} BPM"
        )
        if hasattr(prof, "summary_lines"):
            self._append_precalc_log("\n".join(prof.summary_lines()))
        self._btn_precalc_save_json.config(state=tk.NORMAL)
        self._sync_timeline_from_profile(prof)
        # 同名のキーポイントがあれば自動読込を試みる
        auto = self._punch_cues_default_path()
        if auto.is_file() and not self._punch_cues:
            try:
                self._punch_cues_load_path(auto)
            except Exception:
                pass

    def _precalc_save_json(self) -> None:
        if self._track_profile is None or not hasattr(self._track_profile, "to_json_metadata"):
            return
        path = filedialog.asksaveasfilename(
            parent=self,
            title="解析 JSON を保存",
            defaultextension=".json",
            filetypes=[("JSON", "*.json"), ("すべて", "*.*")],
        )
        if not path:
            return
        try:
            Path(path).write_text(self._track_profile.to_json_metadata(), encoding="utf-8")
        except OSError as e:
            messagebox.showerror("保存失敗", str(e), parent=self)

    def _precalc_load_json(self) -> None:
        if load_profile_from_json is None:
            messagebox.showerror("エラー", "music_precalc が読み込めません。", parent=self)
            return
        path = filedialog.askopenfilename(
            parent=self,
            title="解析 JSON を開く",
            filetypes=[("JSON", "*.json"), ("すべて", "*.*")],
        )
        if not path:
            return
        try:
            prof = load_profile_from_json(Path(path))
        except Exception as e:
            messagebox.showerror("読込失敗", str(e), parent=self)
            return
        self._track_profile = prof
        self._precalc_status.set(
            f"JSON 読込: {Path(path).name}  {prof.duration_sec:.1f}s  {prof.tempo_bpm:.0f} BPM"
        )
        self._append_precalc_log("\n".join(prof.summary_lines()))
        self._btn_precalc_save_json.config(state=tk.NORMAL)
        self._sync_timeline_from_profile(prof)
        auto = self._punch_cues_default_path()
        if auto.is_file() and not self._punch_cues:
            try:
                self._punch_cues_load_path(auto)
            except Exception:
                pass

    def _stop_file_sync_playback(self, *, from_finished: bool = False) -> None:
        # 二重停止・再生中の競合を防ぐ
        if self._tl_transport_busy:
            return
        if not self._file_playback_active and self._tl_stream is None:
            return
        self._tl_transport_busy = True
        try:
            # 世代を進めてからフラグを落とす → 旧 finished / callback を無効化
            self._tl_transport_gen += 1
            self._file_playback_active = False
            self._timeline_reactive_motion = False
            self._cancel_punch_timeline_tick()
            for src in list(self._punch_pending.keys()):
                try:
                    self._punch_record_on_release(src)
                except Exception:
                    pass
            self._tl_stop_stream(abort=True)
            try:
                self._cancel_punch_release_jobs()
            except Exception:
                pass
            mic_live = False
            try:
                mic_live = bool(self._audio_enable.get() and self._audio_analyzer is not None)
            except tk.TclError:
                mic_live = False
            if not mic_live:
                try:
                    self._stop_dot_motion()
                except Exception:
                    pass
                try:
                    self._blackout_laser_channels()
                except Exception:
                    pass
            bs = getattr(self, "_btn_play_sync", None)
            if bs is not None:
                try:
                    bs.config(text="▶ 再生")
                except tk.TclError:
                    pass
            try:
                ph = None if from_finished else None
                if from_finished:
                    try:
                        ph = float(self._punch_timeline_var.get())
                    except (tk.TclError, TypeError, ValueError):
                        ph = self._punch_track_duration()
                self._tl_redraw_editor(playhead_t=ph)
            except Exception:
                pass
        finally:
            self._tl_transport_busy = False

    def _toggle_file_playback(self) -> None:
        if self._tl_transport_busy:
            return
        if self._file_playback_active:
            self._stop_file_sync_playback()
            return
        audio = self._tl_audio_mono
        if audio is None or getattr(audio, "size", 0) < 1:
            messagebox.showwarning(
                "再生不可",
                "先に「音楽ファイルを開く」で曲を読み込んでください（ファイル解析は不要です）。",
                parent=self,
            )
            return
        if sd is None:
            messagebox.showerror(
                "sounddevice 未インストール",
                "pip install sounddevice で再生が使えます。",
                parent=self,
            )
            return

        self._tl_transport_busy = True
        try:
            self._cancel_audio_after_callbacks()
            if self._audio_analyzer is not None:
                try:
                    self._stop_audio_analyzer()
                except Exception:
                    pass

            start_t = 0.0
            try:
                start_t = float(self._punch_timeline_var.get())
            except (tk.TclError, TypeError, ValueError):
                start_t = 0.0
            dur = self._punch_track_duration()
            if dur > 0 and start_t >= dur - 0.05:
                start_t = 0.0
                try:
                    self._punch_timeline_var.set(0.0)
                except tk.TclError:
                    pass

            self._timeline_reactive_motion = False
            autoplay = self._punch_autoplay_ok() and bool(self._punch_cues)
            if (
                not autoplay
                and self._track_profile is not None
                and DOT_POINT_MOTIONS
            ):
                self._timeline_reactive_motion = True

            self._file_playback_active = True
            bs = getattr(self, "_btn_play_sync", None)
            if bs is not None:
                try:
                    bs.config(text="⏸ 停止")
                except tk.TclError:
                    pass
            try:
                self._tl_start_stream_at(start_t)
            except Exception as e:
                self._file_playback_active = False
                self._timeline_reactive_motion = False
                try:
                    self._stop_dot_motion()
                except Exception:
                    pass
                if bs is not None:
                    try:
                        bs.config(text="▶ 再生")
                    except tk.TclError:
                        pass
                messagebox.showerror("再生開始失敗", str(e), parent=self)
                return

            if self._timeline_reactive_motion and self._motion_fn is None:
                try:
                    self._start_dot_motion()
                except Exception:
                    pass

            try:
                self._resync_event_indices(start_t)
            except Exception:
                pass
            self._start_punch_timeline_tick()
            try:
                self._tl_redraw_editor(playhead_t=start_t)
            except Exception:
                pass
        finally:
            self._tl_transport_busy = False

    def _refresh_audio_input_devices(self, init: bool = False) -> None:
        cb = self._audio_device_combo
        if cb is None or not _AUDIO_REACTIVE_AVAILABLE or ClubAudioAnalyzer is None:
            return
        try:
            devs = ClubAudioAnalyzer.list_input_devices()
        except Exception:
            devs = []
        vals = [d[1] for d in devs]
        cb["values"] = vals if vals else [""]
        if vals and (init or cb.get() not in vals):
            cb.current(0)
        elif not vals:
            cb.set("")

    def _parse_audio_device_id(self) -> int | None:
        cb = self._audio_device_combo
        if cb is None or not _AUDIO_REACTIVE_AVAILABLE:
            return None
        s = (cb.get() or "").strip()
        if not s:
            return None
        head = s.split(":", 1)[0].strip()
        if head == "default":
            return None
        try:
            idx = int(head)
        except ValueError:
            return None
        return None if idx < 0 else idx

    def _cancel_audio_after_callbacks(self) -> None:
        for name in ("_audio_motion_after", "_audio_status_after"):
            aid = getattr(self, name, None)
            if aid is not None:
                try:
                    self.after_cancel(aid)
                except tk.TclError:
                    pass
                setattr(self, name, None)

    def _stop_audio_analyzer(self) -> None:
        if self._audio_analyzer is not None and self._audio_analyzer is not self._shared_audio:
            try:
                self._audio_analyzer.stop()
            except Exception:
                pass
        self._audio_analyzer = None

    def _on_audio_sy_range_scale(self, _v: str | None = None) -> None:
        self._apply_audio_sy_range_from_ui(announce=True)

    def _apply_audio_sy_range_from_ui(self, *, announce: bool = True) -> None:
        """スライダーから垂直レンジを反映。"""
        try:
            lo_s = self._audio_sy_lo_scale
            hi_s = self._audio_sy_hi_scale
            lo_pct = float(lo_s.get()) if lo_s is not None else float(self._audio_sy_lo_pct.get())
            hi_pct = float(hi_s.get()) if hi_s is not None else float(self._audio_sy_hi_pct.get())
        except (tk.TclError, TypeError, ValueError, AttributeError):
            lo_pct = float(self._audio_sy_lo_pct.get())
            hi_pct = float(self._audio_sy_hi_pct.get())
        lo_pct = max(0.0, min(98.0, lo_pct))
        hi_pct = max(2.0, min(100.0, hi_pct))
        if hi_pct < lo_pct + 2.0:
            # 交差時は動かしていない側を補正（ドラッグ中のジャンプを防ぐ）
            try:
                if hi_s is not None and lo_s is not None and announce:
                    # 上端を優先して押し上げ、無理なら下端を下げる
                    new_hi = min(100.0, lo_pct + 2.0)
                    if new_hi >= lo_pct + 2.0 - 1e-6:
                        hi_pct = new_hi
                        if abs(float(hi_s.get()) - hi_pct) > 0.15:
                            hi_s.set(hi_pct)
                    else:
                        lo_pct = max(0.0, hi_pct - 2.0)
                        if abs(float(lo_s.get()) - lo_pct) > 0.15:
                            lo_s.set(lo_pct)
                else:
                    hi_pct = min(100.0, lo_pct + 2.0)
            except (tk.TclError, TypeError, ValueError):
                hi_pct = min(100.0, lo_pct + 2.0)
        lo, hi = set_motion_sy_range(lo_pct / 100.0, hi_pct / 100.0)
        self._audio_sy_lo_pct.set(round(lo * 100.0, 1))
        self._audio_sy_hi_pct.set(round(hi * 100.0, 1))
        lbl = self._audio_sy_range_lbl
        if lbl is not None:
            ch7_lo = int(round(lo * 127))
            ch7_hi = int(round(hi * 127))
            lbl.config(text=f"{lo * 100:.0f}%〜{hi * 100:.0f}%（CH7≈{ch7_lo}–{ch7_hi}）")

    def _on_audio_reactive_toggled(self) -> None:
        if self._shared_audio is not None:
            # 共有エンジン使用中はチェックをオンに固定
            self._audio_enable.set(True)
            self._audio_analyzer = self._shared_audio
            return
        if self._file_playback_active:
            self._audio_enable.set(False)
            messagebox.showwarning(
                "ファイル再生中",
                "タイムライン再生中はマイク入力と併用できません。\n先に「停止」でファイル再生を止めてください。",
                parent=self,
            )
            return
        if not _AUDIO_REACTIVE_AVAILABLE or ClubAudioAnalyzer is None:
            self._audio_enable.set(False)
            return
        if self._audio_enable.get():
            dev = self._parse_audio_device_id()
            try:
                self._stop_audio_analyzer()
                self._audio_analyzer = ClubAudioAnalyzer(device=dev)
                try:
                    self._audio_analyzer.gate_sensitivity = float(self._audio_sensitivity.get())
                except (tk.TclError, TypeError, ValueError):
                    self._audio_analyzer.gate_sensitivity = 1.0
                self._audio_analyzer.start()
                try:
                    s0 = self._audio_analyzer.snapshot()
                    self._last_impact_generation = int(s0.impact_generation)
                    self._last_laser_pew_generation = int(s0.laser_pew_generation)
                    self._last_shake_generation = int(s0.shake_generation)
                except (AttributeError, TypeError, ValueError):
                    self._last_impact_generation = 0
                    self._last_laser_pew_generation = 0
                    self._last_shake_generation = 0
            except Exception as e:
                self._audio_enable.set(False)
                messagebox.showerror(
                    "マイクを開けません",
                    f"オーディオ入力を開始できませんでした。\n\n{e}\n\n"
                    "Windows のマイク権限・既定録音デバイス・他アプリの独占を確認してください。",
                    parent=self,
                )
                return
            if self._motion_fn is None:
                self._start_dot_motion()
            self._audio_motion_picker_loop()
            self._audio_status_tick()
        else:
            self._cancel_audio_after_callbacks()
            self._stop_audio_analyzer()
            self._last_impact_generation = 0
            self._last_laser_pew_generation = 0
            self._last_shake_generation = 0
            if self._audio_status_lbl is not None:
                self._audio_status_lbl.config(text="（オフ）")

    def _audio_status_tick(self) -> None:
        self._audio_status_after = None
        if not self._audio_enable.get():
            return
        lbl = self._audio_status_lbl
        an = self._audio_analyzer
        if lbl is not None and an is not None:
            snap = an.snapshot()
            if snap.valid:
                if not snap.music_active:
                    lbl.config(
                        text=(
                            f"無音〜低音量（しきい値未満） RMS={snap.rms_smooth * 100:.2f}% — "
                            "レーザーモーション停止中"
                        )
                    )
                else:
                    st = snap.style.name
                    lfo_h = float(getattr(snap, "lfo_hz", 0.0))
                    lfo_d = float(getattr(snap, "lfo_depth", 0.0))
                    w_eff = effective_wobble_hz(snap)
                    lbl.config(
                        text=(
                            f"BPM≈{snap.bpm:.0f}  推定:{st}  "
                            f"T/H/C={snap.techno_score:.2f}/{snap.hiphop_score:.2f}/{snap.club_score:.2f}  "
                            f"拍={snap.beat_phasor:.2f}  RMS={snap.rms_smooth * 100:.1f}%  "
                            f"揺れ={snap.shake_level:.2f}@{snap.envelope_wobble_hz:.1f}Hz  "
                            f"合成={w_eff:.1f}Hz  LFO≈{lfo_h:.1f}Hz×{lfo_d:.2f}  "
                            f"突発={snap.transient_level:.2f}"
                        )
                    )
            else:
                lbl.config(text="（解析待ち… マイク入力が無い可能性）")
        self._audio_status_after = self.after(420, self._audio_status_tick)

    def _audio_motion_picker_loop(self) -> None:
        self._audio_motion_after = None
        if not self._audio_enable.get():
            return
        if self._file_playback_active:
            return
        if self._pattern_tile_override_active():
            self._audio_motion_after = self.after(600, self._audio_motion_picker_loop)
            return
        an = self._audio_analyzer
        if an is not None:
            if self._motion_fn is None:
                self._start_dot_motion()
            else:
                snap = an.snapshot()
                if (
                    snap.valid
                    and snap.music_active
                    and not getattr(snap, "laser_hold", False)
                ):
                    triplet_pulse = False
                    hype = False
                    try:
                        triplet_pulse = bool(is_triplet_synth_pulse(snap))
                    except Exception:
                        triplet_pulse = False
                    try:
                        hype = bool(is_hype_energy(snap))
                    except Exception:
                        hype = False
                    try:
                        cur = int(self._motion_combo.current())
                    except (tk.TclError, TypeError, ValueError):
                        cur = -1
                    if triplet_pulse:
                        # 穏やか切替で 3連符左右を上書きしない
                        idx = pick_shake_horizontal_index(snap, rng=self._audio_rng)
                        if cur != idx:
                            self._apply_motion_index_for_audio(idx)
                    elif hype:
                        idx = pick_hype_motion_index(snap, rng=self._audio_rng)
                        # 盛り上がり中は同じモーションに留まりにくくする
                        if idx == cur and self._audio_rng.random() < 0.72:
                            idx = pick_hype_motion_index(snap, rng=self._audio_rng)
                        if cur != idx:
                            self._apply_motion_index_for_audio(idx)
                    else:
                        idx = pick_motion_index(snap, rng=self._audio_rng)
                        stutter_idxs = _dot_synth_stutter_motion_indices()
                        # 3連符モーションに居るときは、揺れが落ちてもすぐ穏やかへ戻さない
                        if stutter_idxs and cur in stutter_idxs and float(getattr(snap, "shake_level", 0.0)) > 0.08:
                            pass
                        elif cur != idx:
                            self._apply_motion_index_for_audio(idx)
        # テンポに合わせた切替。盛り上がり中は短めにローテ
        delay_ms = 5200
        if an is not None:
            snap = an.snapshot()
            if snap.valid and snap.bpm > 1:
                beat_ms = 60_000.0 / max(70.0, min(160.0, float(snap.bpm)))
                delay_ms = int(beat_ms * (8.0 + self._audio_rng.random() * 4.0))
            if snap.valid:
                try:
                    if is_triplet_synth_pulse(snap):
                        delay_ms = min(delay_ms, 1200)
                except Exception:
                    pass
                try:
                    if is_hype_energy(snap):
                        # 約 2〜4 拍ごとに激しめを切替
                        beat_ms = 60_000.0 / max(70.0, min(160.0, float(getattr(snap, "bpm", 128.0) or 128.0)))
                        delay_ms = int(beat_ms * (2.0 + self._audio_rng.random() * 2.0))
                        delay_ms = max(450, min(2200, delay_ms))
                except Exception:
                    pass
        lo = 450 if delay_ms <= 2200 else 800
        self._audio_motion_after = self.after(max(lo, min(9000, delay_ms)), self._audio_motion_picker_loop)

    def _apply_motion_index_for_audio(self, idx: int) -> None:
        if self._pattern_tile_override_active():
            return
        if not DOT_POINT_MOTIONS or idx < 0 or idx >= len(DOT_POINT_MOTIONS):
            return
        # 音声連動は常に疑似点制御（図形CH2モーションは点側へ差し替え）
        name = DOT_POINT_MOTIONS[idx][0]
        if not _is_point_control_motion_name(name):
            pool = _point_control_motion_indices()
            if pool:
                idx = int(self._audio_rng.choice(pool))
            else:
                idx = 0
        cb = self._motion_combo
        if cb is not None:
            try:
                cb.current(idx)
            except tk.TclError:
                pass
        if self._motion_fn is None:
            self._start_dot_motion()
            return
        one = _dot_one_shot_horizontal_motion_index()
        if one is not None and idx == one:
            bump_dot_hstep_slot_offset(self._audio_rng)
        # 点制御を強制（CH1／CH2／CH8 を疑似点土台に）
        self._apply_dot_look_channels()
        self._motion_fn = DOT_POINT_MOTIONS[idx][1]
        if self._audio_rng.random() < 0.42:
            self._motion_phase = 0.0

    def _apply_audio_reactive_step(
        self, snap: object | None = None, *, from_precalc_file: bool = False
    ) -> None:
        # ポン出しタイル操作中はライブ／事前解析よりタイルを優先
        if self._pattern_tile_override_active():
            return
        if snap is None:
            if not self._audio_enable.get() or self._audio_analyzer is None:
                return
            snap = self._audio_analyzer.snapshot()
        elif not from_precalc_file and not self._audio_enable.get():
            return
        if not getattr(snap, "valid", False):
            return

        # 溜め中: レーザー消灯（CH1 ブラックアウト）
        if getattr(snap, "laser_hold", False):
            self._channel_vars[0].set(_DOT_STEP_CH1_BLACKOUT)
            self._spin_from_var(0)
            self._motion_speed.set(MOTION_SPEED_MIN)
            try:
                self._motion_speed_scale.set(MOTION_SPEED_MIN)
            except (tk.TclError, AttributeError):
                pass
            return

        if not getattr(snap, "music_active", False):
            return

        # 溜め解除後はマニュアル帯へ戻す（疑似点土台）
        try:
            if int(self._channel_vars[0].get()) <= 63:
                self._channel_vars[0].set(_DOT_BASE_CH1)
                self._spin_from_var(0)
        except (tk.TclError, TypeError, ValueError):
            pass

        try:
            if self._audio_analyzer is not None and hasattr(self._audio_analyzer, "gate_sensitivity"):
                sens = float(self._audio_analyzer.gate_sensitivity)
            else:
                sens = float(self._audio_sensitivity.get())
        except (tk.TclError, TypeError, ValueError):
            sens = 1.0
        sens = max(0.35, min(1.85, 0.45 + (sens - 0.3) * (1.4 / 2.2)))

        # LED 色に合わせる（参考4点ファン中は緑／紫をモーション優先）
        try:
            cur_mi = int(self._motion_combo.current())
            cur_name = DOT_POINT_MOTIONS[cur_mi][0] if 0 <= cur_mi < len(DOT_POINT_MOTIONS) else ""
        except (tk.TclError, TypeError, ValueError, IndexError):
            cur_mi = -1
            cur_name = ""
        on_ref_fan = "4点上空ファン" in cur_name
        if not on_ref_fan:
            ch9 = ch9_from_snapshot(snap)
            self._channel_vars[8].set(ch9)
            self._club_dot_ch9 = ch9
            self._spin_from_var(8)

        target_sp = motion_speed_from_snapshot(snap, sens=sens)
        try:
            cur_sp = float(self._motion_speed.get())
        except (tk.TclError, TypeError, ValueError):
            cur_sp = 1.0

        triplet_pulse = False
        try:
            triplet_pulse = bool(is_triplet_synth_pulse(snap))
        except Exception:
            triplet_pulse = False
        # スネア加速ロール中は左右ファン禁止（Breakaway 等での誤スイープ防止）
        if str(getattr(snap, "led_effect", "") or "") == "accel_blink":
            set_ref_fan_audio_sync(pulse_hz=0.0, phasor=0.0, level=0.0)
            if on_ref_fan:
                idx = pick_motion_index(snap, rng=self._audio_rng)
                self._apply_motion_index_for_audio(idx)
                on_ref_fan = False
            # 通常速度へ
            blend = 0.12
            new_sp = cur_sp + blend * (target_sp - cur_sp)
            new_sp = max(MOTION_SPEED_MIN, min(min(MOTION_SPEED_MAX, 3.4), new_sp))
            self._motion_speed.set(new_sp)
            try:
                self._motion_speed_scale.set(new_sp)
            except (tk.TclError, AttributeError):
                pass
            return

        # 鋭い電子パルス → 参考4点ファン（tip帯比率でスネア胴と分離）
        hp = float(getattr(snap, "high_point_level", 0.0))
        pulse = float(getattr(snap, "synth_pulse_hz", 0.0))
        tip = float(getattr(snap, "synth_tip_ratio", 0.0))
        want_ref_fan = bool(
            triplet_pulse
            and hp >= 0.20
            and tip >= 0.75
            and 3.4 <= pulse <= 11.5
        )
        if want_ref_fan:
            if not on_ref_fan:
                fan_idxs = _dot_ref_fan_motion_indices()
                if fan_idxs:
                    idx = int(self._audio_rng.choice(fan_idxs))
                else:
                    idx = pick_shake_horizontal_index(snap, rng=self._audio_rng)
                self._apply_motion_index_for_audio(idx)
                on_ref_fan = True
        elif triplet_pulse and hp >= 0.16 and tip >= 0.60:
            stutter_idxs = _dot_synth_stutter_motion_indices()
            if stutter_idxs and cur_mi not in stutter_idxs and not on_ref_fan:
                # ファン閾値未満でも三点／シンセ左右へ
                idx = pick_shake_horizontal_index(snap, rng=self._audio_rng)
                name = DOT_POINT_MOTIONS[idx][0] if 0 <= idx < len(DOT_POINT_MOTIONS) else ""
                if "4点上空ファン" not in name:
                    self._apply_motion_index_for_audio(idx)

        if on_ref_fan:
            pulse = float(getattr(snap, "synth_pulse_hz", 0.0))
            phasor = float(getattr(snap, "synth_pulse_phasor", 0.0))
            level = max(
                float(getattr(snap, "high_point_level", 0.0)),
                float(getattr(snap, "shake_level", 0.0)) * 0.85,
            )
            # ファン中はゲートが潰れないよう最低レベルを確保
            if want_ref_fan:
                level = max(level, 0.34)
            set_ref_fan_audio_sync(pulse_hz=pulse, phasor=phasor, level=level)
            sp_lock = _REF_FAN_SPEED_LOCK
            if 3.4 <= pulse <= 11.5 and level >= 0.20:
                sp_lock = max(1.5, min(3.2, _REF_FAN_SPEED_LOCK * (pulse / _REF_FAN_PULSE_HZ)))
            new_sp = sp_lock
        else:
            set_ref_fan_audio_sync(pulse_hz=0.0, phasor=0.0, level=0.0)
            blend = 0.08
            if getattr(snap, "beat", False) or float(getattr(snap, "onset_strength", 0.0)) > 0.4:
                blend = 0.18
            if triplet_pulse:
                blend = 0.35
            new_sp = cur_sp + blend * (target_sp - cur_sp)
            soft_max = 5.5 if triplet_pulse else min(MOTION_SPEED_MAX, 3.4)
            new_sp = max(MOTION_SPEED_MIN, min(soft_max, new_sp))
        self._motion_speed.set(new_sp)
        try:
            self._motion_speed_scale.set(new_sp)
        except (tk.TclError, AttributeError):
            pass

    def _open_pattern_palette(self) -> None:
        """CH2・疑似点・点モーションを別ウィンドウに表示し、クリックで即適用／開始。"""
        self._close_pattern_palette()
        win = tk.Toplevel(self)
        self._palette_win = win
        win.title("パターン一覧 — クリックで適用")
        # 初期サイズは中身に任せる。高さは固定しない（ユーザーが縦横リサイズ可能）。
        win.minsize(380, 220)
        win.transient(self)
        win.resizable(True, True)

        nb = ttk.Notebook(win, padding=6)
        nb.pack(fill=tk.BOTH, expand=True, padx=8, pady=(8, 4))

        tab_ch2 = ttk.Frame(nb, padding=4)
        nb.add(tab_ch2, text="CH2 図形")
        ttk.Label(
            tab_ch2,
            text="行をクリック → CH2 にレンジ中央値をセット（他チャンネルは変わりません）。",
            wraplength=520,
        ).pack(anchor="w")
        f_ch2 = ttk.Frame(tab_ch2)
        f_ch2.pack(fill=tk.BOTH, expand=True, pady=(6, 0))
        sb_ch2 = ttk.Scrollbar(f_ch2)
        lb_ch2 = tk.Listbox(f_ch2, height=20, activestyle="dotbox", exportselection=False, font=("Segoe UI", 10))
        lb_ch2.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb_ch2.pack(side=tk.RIGHT, fill=tk.Y)
        lb_ch2.configure(yscrollcommand=sb_ch2.set)
        sb_ch2.configure(command=lb_ch2.yview)
        if self._ch2_patterns:
            for s, e, n in self._ch2_patterns:
                lb_ch2.insert(tk.END, f"{s}–{e}  {n}")
            lb_ch2.bind("<ButtonRelease-1>", self._palette_ch2_click)
        else:
            lb_ch2.insert(tk.END, "（pattern.txt から図形一覧を読めませんでした）")

        tab_dot = ttk.Frame(nb, padding=4)
        nb.add(tab_dot, text="クラブ疑似点")

        lf_pos = ttk.LabelFrame(tab_dot, text="位置（レイアウトのみ）", padding=4)
        lf_pos.pack(fill=tk.BOTH, expand=True, pady=(0, 8))
        ttk.Label(
            lf_pos,
            text="クリックで CH6／CH7 など位置プリセットを適用。色（CH9）は変わりません（下の「色」か CH9スライダーで別指定）。",
            wraplength=520,
        ).pack(anchor="w")
        f_dp = ttk.Frame(lf_pos)
        f_dp.pack(fill=tk.BOTH, expand=True, pady=(6, 0))
        sb_dp = ttk.Scrollbar(f_dp)
        lb_dp = tk.Listbox(f_dp, height=11, activestyle="dotbox", exportselection=False, font=("Segoe UI", 10))
        lb_dp.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb_dp.pack(side=tk.RIGHT, fill=tk.Y)
        lb_dp.configure(yscrollcommand=sb_dp.set)
        sb_dp.configure(command=lb_dp.yview)
        for name, _a, _b in CLUB_DOT_POSITION_PRESETS:
            lb_dp.insert(tk.END, name)
        lb_dp.bind("<ButtonRelease-1>", self._palette_dot_position_click)

        lf_col = ttk.LabelFrame(tab_dot, text="色（CH9 のみ・点モード）", padding=4)
        lf_col.pack(fill=tk.BOTH, expand=True)
        ttk.Label(
            lf_col,
            text="クリックで CH9 だけ変更。次に「位置」を選んでもこの色が載ります。",
            wraplength=520,
        ).pack(anchor="w")
        f_dc = ttk.Frame(lf_col)
        f_dc.pack(fill=tk.BOTH, expand=True, pady=(6, 0))
        sb_dc = ttk.Scrollbar(f_dc)
        lb_dc = tk.Listbox(f_dc, height=8, activestyle="dotbox", exportselection=False, font=("Segoe UI", 10))
        lb_dc.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb_dc.pack(side=tk.RIGHT, fill=tk.Y)
        lb_dc.configure(yscrollcommand=sb_dc.set)
        sb_dc.configure(command=lb_dc.yview)
        for label, val in DOT_CH9_DOT_SWATCHES:
            lb_dc.insert(tk.END, f"{label}  →  CH9={val}")
        lb_dc.bind("<ButtonRelease-1>", self._palette_dot_color_click)

        tab_motion = ttk.Frame(nb, padding=4)
        nb.add(tab_motion, text="点モーション")
        ttk.Label(
            tab_motion,
            text="行をクリック → メインのモーション種類にセットして即「モーション開始」相当（実行中なら切替）。CH9固定プリセットは CH9 を変えません。更新しないチャンネル（None）は実行中もスライダーで調整可能。メインの「開始時・種類切替時に疑似点の土台」チェックがオンのときは、その設定に従って CH1／CH2／CH8 をセットします。",
            wraplength=520,
        ).pack(anchor="w")
        f_m = ttk.Frame(tab_motion)
        f_m.pack(fill=tk.BOTH, expand=True, pady=(6, 0))
        sb_m = ttk.Scrollbar(f_m)
        lb_m = tk.Listbox(f_m, height=18, activestyle="dotbox", exportselection=False, font=("Segoe UI", 10))
        lb_m.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb_m.pack(side=tk.RIGHT, fill=tk.Y)
        lb_m.configure(yscrollcommand=sb_m.set)
        sb_m.configure(command=lb_m.yview)
        if DOT_POINT_MOTIONS:
            for name, _fn in DOT_POINT_MOTIONS:
                lb_m.insert(tk.END, name)
            lb_m.bind("<ButtonRelease-1>", self._palette_dot_motion_click)
        else:
            lb_m.insert(tk.END, "（点モーション定義がありません）")

        ttk.Label(win, text="メイン画面のスライダー／送信はそのまま有効です。", foreground="#444").pack(
            anchor="w", padx=14, pady=(0, 8)
        )

        def _on_palette_close() -> None:
            self._palette_win = None
            try:
                win.destroy()
            except tk.TclError:
                pass

        win.protocol("WM_DELETE_WINDOW", _on_palette_close)

    def _palette_ch2_click(self, event: tk.Event) -> None:
        lb = event.widget
        if not isinstance(lb, tk.Listbox) or not self._ch2_patterns:
            return
        if lb.size() <= 0:
            return
        idx = lb.nearest(event.y)
        if idx < 0 or idx >= len(self._ch2_patterns):
            return
        lb.selection_clear(0, tk.END)
        lb.selection_set(idx)
        lb.activate(idx)
        self._apply_ch2_pattern_index(idx)

    def _palette_dot_position_click(self, event: tk.Event) -> None:
        lb = event.widget
        if not isinstance(lb, tk.Listbox) or not CLUB_DOT_POSITION_PRESETS:
            return
        idx = lb.nearest(event.y)
        if idx < 0 or idx >= len(CLUB_DOT_POSITION_PRESETS):
            return
        lb.selection_clear(0, tk.END)
        lb.selection_set(idx)
        lb.activate(idx)
        self._apply_dot_scene_index(idx)

    def _palette_dot_color_click(self, event: tk.Event) -> None:
        lb = event.widget
        if not isinstance(lb, tk.Listbox) or not DOT_CH9_DOT_SWATCHES:
            return
        idx = lb.nearest(event.y)
        if idx < 0 or idx >= len(DOT_CH9_DOT_SWATCHES):
            return
        lb.selection_clear(0, tk.END)
        lb.selection_set(idx)
        lb.activate(idx)
        _label, val = DOT_CH9_DOT_SWATCHES[idx]
        self._apply_dot_ch9_pick(val)

    def _palette_dot_motion_click(self, event: tk.Event) -> None:
        lb = event.widget
        if not isinstance(lb, tk.Listbox) or not DOT_POINT_MOTIONS:
            return
        idx = lb.nearest(event.y)
        if idx < 0 or idx >= len(DOT_POINT_MOTIONS):
            return
        cb = getattr(self, "_motion_combo", None)
        if cb is None:
            return
        lb.selection_clear(0, tk.END)
        lb.selection_set(idx)
        lb.activate(idx)
        cb.current(idx)
        self._start_dot_motion()

    def _close_pattern_palette(self) -> None:
        w = self._palette_win
        self._palette_win = None
        if w is not None:
            try:
                w.destroy()
            except tk.TclError:
                pass

    def _tile_from_dict(self, d: Any) -> PatternTileState | None:
        try:
            ch = d["channels"]
            if not isinstance(ch, (list, tuple)) or len(ch) != 10:
                return None
            tch = tuple(max(0, min(255, int(x))) for x in ch)
            mi = int(d.get("motion_index", 0))
            if DOT_POINT_MOTIONS:
                mi = max(0, min(len(DOT_POINT_MOTIONS) - 1, mi))
            else:
                mi = 0
            hk = d.get("hotkey")
            hotkey: str | None = None
            if isinstance(hk, str):
                s = hk.strip()
                if s:
                    hotkey = s[:32]
            led_mode_raw = d.get("led_mode", None)
            led_mode: int | None
            if led_mode_raw is None or led_mode_raw == "":
                led_mode = None
            else:
                led_mode = int(led_mode_raw)
            return PatternTileState(
                title=str(d.get("title", "名称なし"))[:80],
                channels=tch,  # type: ignore[arg-type]
                motion_index=mi,
                motion_speed=max(
                    MOTION_SPEED_MIN,
                    min(MOTION_SPEED_MAX, float(d.get("motion_speed", 1.35))),
                ),
                apply_dot_base=bool(d.get("apply_dot_base", True)),
                club_dot_ch9=max(0, min(255, int(d.get("club_dot_ch9", 32)))),
                run_motion=bool(d.get("run_motion", False)),
                hotkey=hotkey,
                apply_laser=bool(d.get("apply_laser", True)),
                apply_led=bool(d.get("apply_led", False)),
                led_r=max(0, min(255, int(d.get("led_r", 255)))),
                led_g=max(0, min(255, int(d.get("led_g", 80)))),
                led_b=max(0, min(255, int(d.get("led_b", 40)))),
                led_brightness=max(1.0, min(100.0, float(d.get("led_brightness", 100.0)))),
                led_mode=led_mode,
                led_speed=max(1, min(100, int(d.get("led_speed", 50)))),
            )
        except (KeyError, TypeError, ValueError):
            return None

    def set_tile_led_override_handler(
        self, cb: Callable[[dict | bool | None], None] | None
    ) -> None:
        """統合アプリから LED ポン出し優先のハンドラを渡す。"""
        self._on_tile_led_override = cb

    def _pattern_tile_override_active(self) -> bool:
        # latched 単独では判定しない（解除漏れで LED/AI が永久ブロックされるのを防ぐ）
        return bool(self._pattern_tile_holds) or (
            self._pattern_tile_hold_tile is not None
            or self._pattern_tile_kb_hold_tile is not None
        )

    def _notify_tile_led_override(self, payload: dict | bool | None) -> None:
        cb = self._on_tile_led_override
        if not callable(cb):
            return
        try:
            cb(payload)
        except Exception:
            pass

    def _begin_pattern_tile_override(self, tile: PatternTileState) -> None:
        """単体適用用（プレビュー等）。通常ポン出しは hold_sync 経由。"""
        self._tile_override_latched = True
        if bool(getattr(tile, "apply_led", False)):
            self._notify_tile_led_override(
                {
                    "r": int(getattr(tile, "led_r", 255)),
                    "g": int(getattr(tile, "led_g", 80)),
                    "b": int(getattr(tile, "led_b", 40)),
                    "brightness": float(getattr(tile, "led_brightness", 100.0)),
                    "mode": getattr(tile, "led_mode", None),
                    "speed": int(getattr(tile, "led_speed", 50)),
                }
            )

    def _push_channels_fast(self, values: tuple[int, ...] | list[int], *, sync_ch2: bool = True) -> None:
        """10CH をまとめて反映し、DMX 送信は1回だけ。"""
        if len(values) != 10:
            return
        for i, raw in enumerate(values):
            v = max(0, min(255, int(raw)))
            try:
                self._channel_vars[i].set(v)
            except tk.TclError:
                continue
            scale = getattr(self, f"_scale_{i}", None)
            if scale is not None:
                try:
                    if abs(round(float(scale.get())) - v) >= 1:
                        scale.set(v)
                except (tk.TclError, TypeError, ValueError):
                    pass
        self._club_dot_ch9 = max(0, min(255, int(values[8])))
        if sync_ch2:
            try:
                self._sync_ch2_combo_from_value(int(values[1]))
            except Exception:
                pass
        self._on_values_changed()

    def _switch_dot_motion_fast(self, motion_index: int, *, reset_phase: bool = True) -> None:
        """ライブ中ポン出し用: stop/start せずモーション関数だけ切替＋即1フレーム。"""
        if not DOT_POINT_MOTIONS:
            return
        mi = max(0, min(len(DOT_POINT_MOTIONS) - 1, int(motion_index)))
        cb = self._motion_combo
        if cb is not None:
            try:
                if int(cb.current()) != mi:
                    cb.current(mi)
            except (tk.TclError, TypeError, ValueError):
                try:
                    cb.current(mi)
                except tk.TclError:
                    pass
        one = _dot_one_shot_horizontal_motion_index()
        if one is not None and mi == one:
            bump_dot_hstep_slot_offset(self._audio_rng)
        if self._motion_apply_dot_base.get():
            # 土台3CHだけ差分更新（フル stop しない）
            need = False
            try:
                need = (
                    int(self._channel_vars[0].get()) != _DOT_BASE_CH1
                    or int(self._channel_vars[1].get()) != _DOT_BASE_CH2
                    or int(self._channel_vars[7].get()) != _DOT_BASE_CH8
                )
            except (tk.TclError, TypeError, ValueError):
                need = True
            if need:
                self._channel_vars[0].set(_DOT_BASE_CH1)
                self._channel_vars[1].set(_DOT_BASE_CH2)
                self._channel_vars[7].set(_DOT_BASE_CH8)
                self._spin_from_var(0)
                self._spin_from_var(1)
                self._spin_from_var(7)
        self._motion_fn = DOT_POINT_MOTIONS[mi][1]
        if reset_phase:
            self._motion_phase = 0.0
        if self._motion_after_id is None:
            # 止まっていたら起動
            sid = self._motion_seq
            self._run_motion_tick(sid)
        else:
            # 稼働中なら次の after を待たず今フレームを出す
            sid = self._motion_seq
            # 進行中 tick の二重 after を避けるため一旦キャンセルして即時実行
            try:
                self.after_cancel(self._motion_after_id)
            except (tk.TclError, ValueError):
                pass
            self._motion_after_id = None
            self._run_motion_tick(sid)

    def _end_pattern_tile_override(self, *, clear_laser: bool | None = None) -> None:
        was = (
            bool(self._pattern_tile_holds)
            or self._tile_override_latched
            or self._pattern_tile_hold_tile is not None
            or self._pattern_tile_kb_hold_tile is not None
        )
        for fr in list(self._pattern_tile_hold_frames.values()):
            try:
                fr.configure(highlightbackground="#666", highlightthickness=2)
            except tk.TclError:
                pass
        self._pattern_tile_holds.clear()
        self._pattern_tile_hold_order.clear()
        self._pattern_tile_hold_frames.clear()
        self._pattern_tile_active_laser = None
        self._pattern_tile_hold_tile = None
        self._pattern_tile_kb_hold_tile = None
        self._tile_override_latched = False
        if self._tile_press_frame is not None:
            try:
                self._tile_press_frame.configure(highlightbackground="#666", highlightthickness=2)
            except tk.TclError:
                pass
            self._tile_press_frame = None
        # LED 優先は常に解除を通知（付けっぱなし防止。未使用時は no-op）
        self._notify_tile_led_override(None)
        if not was:
            return

        # ポン出しで動かしたモーションは必ず止める（タイムライン中に永久動作するのを防ぐ）
        self._stop_dot_motion()

        mic_live = bool(self._audio_enable.get() and self._audio_analyzer is not None)
        file_reactive = bool(self._file_playback_active and self._timeline_reactive_motion)
        if clear_laser is None:
            clear_laser = not (mic_live or file_reactive)
        if clear_laser:
            # clear_all（Scale連動）は重いので高速消灯
            try:
                self._blackout_laser_channels()
            except Exception:
                self._clear_all()
        elif mic_live or file_reactive:
            try:
                self._start_dot_motion()
            except Exception:
                pass

    def _load_pattern_tiles_from_disk(self) -> None:
        path = _pattern_tiles_json_path()
        if not path.is_file():
            return
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(raw, list):
            return
        out: list[PatternTileState] = []
        for item in raw:
            if isinstance(item, dict):
                t = self._tile_from_dict(item)
                if t is not None:
                    out.append(t)
        self._pattern_tiles = out

    def _save_pattern_tiles_to_disk(self) -> None:
        path = _pattern_tiles_json_path()
        try:
            data = []
            for t in self._pattern_tiles:
                d = asdict(t)
                d["channels"] = list(d["channels"])
                data.append(d)
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass

    def _short_tile_caption(self, tile: PatternTileState) -> str:
        mo = ""
        if getattr(tile, "apply_laser", True) and DOT_POINT_MOTIONS and 0 <= tile.motion_index < len(DOT_POINT_MOTIONS):
            name = DOT_POINT_MOTIONS[tile.motion_index][0]
            mo = name[:10] + ("…" if len(name) > 10 else "")
        rm = "●" if tile.run_motion else "○"
        tags = []
        if getattr(tile, "apply_laser", True):
            tags.append("L")
        if getattr(tile, "apply_led", False):
            tags.append("LED")
        tag_s = f"[{'+'.join(tags)}] " if tags else ""
        hk = ""
        if tile.hotkey:
            disp = tile.hotkey
            hk = f"\n⌨ {disp}"
        ch_bit = f"CH2={tile.channels[1]} CH9={tile.channels[8]} {rm}{mo}" if getattr(tile, "apply_laser", True) else "レーザーなし"
        return f"{tag_s}{tile.title}\n{ch_bit}{hk}"

    def _on_pattern_tile_touch_tap(self, tile: PatternTileState, fr: tk.Frame, event: tk.Event | None = None) -> None:
        """タッチ向け: タップでキープ／再タップでそのタイルだけオフ（複数同時キープ可）。"""
        ctrl = event is not None and (event.state & 0x4)
        if ctrl or self._pattern_tile_key_assign_mode.get():
            self._pattern_tile_arm_hotkey_target(tile, fr)
            return
        src = self._touch_hold_source_for_tile(tile)
        if self._pattern_tile_holds.get(src) is tile:
            self._pattern_tile_hold_remove(src)
            return
        self._pattern_tile_hold_add(src, tile, fr, force_reapply=True)

    def _on_pattern_tile_press(
        self, tile: PatternTileState, fr: tk.Frame, event: tk.Event | None = None
    ) -> None:
        ctrl = event is not None and (event.state & 0x4)
        if ctrl or self._pattern_tile_key_assign_mode.get():
            self._pattern_tile_arm_hotkey_target(tile, fr)
            return
        self._pattern_tile_hold_add("ptr", tile, fr, force_reapply=True)

    def _on_pattern_tile_motion(self, tile: PatternTileState, _event: tk.Event | None = None) -> None:
        # ドラッグ中の再適用は stop/start で遅延の元になるため無視（初回 press で確定済み）
        return

    def _on_pattern_tile_release(self) -> None:
        if self._pattern_tile_key_assign_mode.get() or self._pattern_tile_key_target is not None:
            return
        if not self._pattern_tile_holds:
            return
        # タッチキープ（tap:*）は指離しで消さない（再タップで解除）
        if self._touch_friendly_ui.get():
            return
        if "ptr" in self._pattern_tile_holds:
            self._pattern_tile_hold_remove("ptr")
            return
        # ポインタ以外だけ残っている場合はキー側に任せる
        return

    def _apply_pattern_tile_laser(self, tile: PatternTileState, *, reset_phase: bool = True) -> None:
        """レーザー側だけ適用（複数押し合成用）。"""
        if not bool(getattr(tile, "apply_laser", True)):
            return
        self._motion_apply_dot_base.set(bool(tile.apply_dot_base))
        ch = tuple(max(0, min(255, int(v))) for v in tile.channels)
        self._club_dot_ch9 = max(0, min(255, int(tile.club_dot_ch9)))
        if len(ch) == 10:
            ch_list = list(ch)
            ch_list[8] = self._club_dot_ch9
            ch = tuple(ch_list)
        self._push_channels_fast(ch)
        sp = max(MOTION_SPEED_MIN, min(MOTION_SPEED_MAX, float(tile.motion_speed)))
        try:
            self._motion_speed.set(sp)
            self._motion_speed_scale.set(sp)
        except (tk.TclError, AttributeError):
            pass
        mi = max(0, int(tile.motion_index))
        if tile.run_motion and DOT_POINT_MOTIONS:
            mi = max(0, min(len(DOT_POINT_MOTIONS) - 1, mi))
            already = (
                not reset_phase
                and self._motion_fn is DOT_POINT_MOTIONS[mi][1]
                and self._motion_after_id is not None
            )
            self._switch_dot_motion_fast(mi, reset_phase=not already)
        else:
            self._stop_dot_motion()

    def _apply_pattern_tile(self, tile: PatternTileState) -> None:
        # プレビュー／単発用: 優先ラッチしてフル適用
        if self._pattern_tile_hold_tile is None and self._pattern_tile_kb_hold_tile is None:
            if "ptr" not in self._pattern_tile_holds and not any(
                s != "ptr" for s in self._pattern_tile_holds
            ):
                self._pattern_tile_hold_tile = tile
        self._begin_pattern_tile_override(tile)
        if bool(getattr(tile, "apply_laser", True)):
            self._apply_pattern_tile_laser(tile, reset_phase=True)

    def _install_led_fx_asset_pack(self) -> None:
        """点滅・フェード等の LED 光型タイルを一括追加（同名はスキップ）。"""
        if not _LED_FX_ASSETS:
            messagebox.showinfo("LED光型", "アセットが空です。", parent=self._tiles_win or self)
            return
        existing = {t.title for t in self._pattern_tiles}
        added = 0
        for asset in _LED_FX_ASSETS:
            if asset.title in existing:
                continue
            # mode が解決できないエフェクトはスキップ（固定色は残す）
            if asset.mode is None:
                self._pattern_tiles.append(_pattern_tile_from_led_fx(asset))
                added += 1
                existing.add(asset.title)
                continue
            self._pattern_tiles.append(_pattern_tile_from_led_fx(asset))
            added += 1
            existing.add(asset.title)
        if added:
            self._save_pattern_tiles_to_disk()
            self._rebuild_pattern_tiles_grid()
        messagebox.showinfo(
            "LED光型パック",
            f"{added} 件追加しました（既存同名はスキップ）。\n"
            f"カタログ合計 {len(_LED_FX_ASSETS)} 種：点滅／ストロボ／フェード／ジャンプ／固定色。",
            parent=self._tiles_win or self,
        )

    def _capture_pattern_tile_dialog(self) -> None:
        default = f"スロット {len(self._pattern_tiles) + 1}"
        title = simpledialog.askstring(
            "パターンをキャプチャ",
            "タイル表示名（例: 赤ドット・横走査）",
            initialvalue=default,
            parent=self,
        )
        if title is None or not str(title).strip():
            return
        title = str(title).strip()[:80]
        ch = tuple(self._read_channel_value(i) for i in range(10))
        mi = 0
        cb = self._motion_combo
        if cb is not None and DOT_POINT_MOTIONS:
            try:
                c = int(cb.current())
                mi = max(0, min(len(DOT_POINT_MOTIONS) - 1, c))
            except (tk.TclError, TypeError, ValueError):
                mi = 0
        try:
            sp = float(self._motion_speed.get())
        except (tk.TclError, ValueError):
            sp = 1.35
        sp = max(MOTION_SPEED_MIN, min(MOTION_SPEED_MAX, sp))
        tile = PatternTileState(
            title=title,
            channels=ch,  # type: ignore[arg-type]
            motion_index=mi,
            motion_speed=sp,
            apply_dot_base=bool(self._motion_apply_dot_base.get()),
            club_dot_ch9=int(self._club_dot_ch9),
            run_motion=self._motion_fn is not None,
            apply_laser=True,
            apply_led=False,
        )
        self._pattern_tiles.append(tile)
        self._save_pattern_tiles_to_disk()
        self._rebuild_pattern_tiles_grid()

    def _register_pattern_tile_from_assets_dialog(self) -> None:
        """LED／レーザーモーション等のアセットからタイルを登録（選択はリアルタイムプレビュー）。"""
        parent = self._tiles_win or self
        win = tk.Toplevel(parent)
        win.title("アセットからポン出しタイル登録")
        win.transient(parent)
        win.grab_set()
        win.minsize(520, 560)

        # --- 開いた時点の状態を退避（キャンセル／閉じるで復元） ---
        snap_channels = [self._read_channel_value(i) for i in range(10)]
        snap_club_ch9 = int(self._club_dot_ch9)
        try:
            snap_speed = float(self._motion_speed.get())
        except (tk.TclError, TypeError, ValueError):
            snap_speed = 1.35
        snap_apply_dot = bool(self._motion_apply_dot_base.get())
        snap_motion_idx = 0
        cb0 = self._motion_combo
        if cb0 is not None and DOT_POINT_MOTIONS:
            try:
                snap_motion_idx = max(0, min(len(DOT_POINT_MOTIONS) - 1, int(cb0.current())))
            except (tk.TclError, TypeError, ValueError):
                snap_motion_idx = 0
        snap_motion_running = self._motion_fn is not None
        preview_closed = {"done": False}
        preview_job: dict[str, str | None] = {"id": None}

        pad = ttk.Frame(win, padding=10)
        pad.pack(fill=tk.BOTH, expand=True)

        title_var = tk.StringVar(value=f"スロット {len(self._pattern_tiles) + 1}")
        ttk.Label(pad, text="表示名").grid(row=0, column=0, sticky="w")
        ttk.Entry(pad, textvariable=title_var, width=40).grid(row=0, column=1, sticky="ew", padx=6)
        ttk.Label(
            pad,
            text="チェックした側の選択内容は、変更のたびに実機へリアルタイムプレビューします。",
            foreground="#666",
            wraplength=480,
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(4, 0))

        # --- レーザー ---
        apply_laser = tk.BooleanVar(value=False)
        lf = ttk.LabelFrame(pad, text="レーザー", padding=8)
        lf.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(10, 4))
        ttk.Checkbutton(lf, text="レーザーを登録する（チェック時のみプレビュー／発火）", variable=apply_laser).pack(
            anchor="w"
        )

        motion_names = [n for n, _ in DOT_POINT_MOTIONS] if DOT_POINT_MOTIONS else ["（なし）"]
        motion_var = tk.StringVar(value=motion_names[0] if motion_names else "")
        row_m = ttk.Frame(lf)
        row_m.pack(fill=tk.X, pady=4)
        ttk.Label(row_m, text="モーション").pack(side=tk.LEFT)
        motion_cb = ttk.Combobox(row_m, textvariable=motion_var, values=motion_names, state="readonly", width=42)
        motion_cb.pack(side=tk.LEFT, padx=6, fill=tk.X, expand=True)

        speed_var = tk.DoubleVar(value=snap_speed)
        row_s = ttk.Frame(lf)
        row_s.pack(fill=tk.X, pady=4)
        ttk.Label(row_s, text="速度").pack(side=tk.LEFT)
        ttk.Scale(
            row_s,
            from_=MOTION_SPEED_MIN,
            to=MOTION_SPEED_MAX,
            variable=speed_var,
            orient=tk.HORIZONTAL,
        ).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=6)
        spd_lbl = ttk.Label(row_s, text=f"{snap_speed:.2f}", width=5)
        spd_lbl.pack(side=tk.LEFT)

        ch9_names = [n for n, _ in DOT_CH9_DOT_SWATCHES]
        ch9_var = tk.StringVar(value=ch9_names[0] if ch9_names else "")
        row_c = ttk.Frame(lf)
        row_c.pack(fill=tk.X, pady=4)
        ttk.Label(row_c, text="CH9 色").pack(side=tk.LEFT)
        ch9_cb = ttk.Combobox(row_c, textvariable=ch9_var, values=ch9_names, state="readonly", width=28)
        ch9_cb.pack(side=tk.LEFT, padx=6)
        run_motion = tk.BooleanVar(value=True)
        apply_dot = tk.BooleanVar(value=True)
        ttk.Checkbutton(lf, text="点モーションを自動開始", variable=run_motion).pack(anchor="w")
        ttk.Checkbutton(lf, text="疑似点土台（CH1/CH2/CH8）を適用", variable=apply_dot).pack(anchor="w")

        def _use_current_laser() -> None:
            apply_laser.set(True)
            if DOT_POINT_MOTIONS and 0 <= snap_motion_idx < len(DOT_POINT_MOTIONS):
                motion_var.set(DOT_POINT_MOTIONS[snap_motion_idx][0])
            speed_var.set(snap_speed)
            run_motion.set(snap_motion_running)
            apply_dot.set(snap_apply_dot)
            best = (
                min(DOT_CH9_DOT_SWATCHES, key=lambda p: abs(p[1] - snap_club_ch9))
                if DOT_CH9_DOT_SWATCHES
                else None
            )
            if best:
                ch9_var.set(best[0])
            _schedule_preview()

        ttk.Button(lf, text="いまのレーザー状態を読み込む", command=_use_current_laser).pack(anchor="w", pady=(4, 0))

        # --- LED ---
        apply_led = tk.BooleanVar(value=False)
        led_f = ttk.LabelFrame(pad, text="LED（Bluetooth）", padding=8)
        led_f.grid(row=3, column=0, columnspan=2, sticky="ew", pady=4)
        ttk.Checkbutton(
            led_f, text="LED を登録する（チェック時のみプレビュー／発火・ライブAIより優先）", variable=apply_led
        ).pack(anchor="w")

        ttk.Label(
            led_f,
            text="光型アセット（エフェクトは色を変えません。色は下のパレットで指定）",
            foreground="#555",
        ).pack(anchor="w", pady=(6, 2))
        fx_row = ttk.Frame(led_f)
        fx_row.pack(fill=tk.BOTH, expand=True, pady=(0, 4))
        fx_lb = tk.Listbox(
            fx_row,
            height=7,
            exportselection=False,
            bg="#2b3038",
            fg="#e8eaed",
            selectbackground="#3d8bfd",
            font=("Segoe UI", 9),
        )
        fx_sb = ttk.Scrollbar(fx_row, orient=tk.VERTICAL, command=fx_lb.yview)
        fx_lb.configure(yscrollcommand=fx_sb.set)
        fx_lb.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        fx_sb.pack(side=tk.RIGHT, fill=tk.Y)
        for a in _LED_FX_ASSETS:
            if a.mode is None:
                kind = "固定色"
                fx_lb.insert(tk.END, f"{a.title}  [{kind} / 明{int(a.brightness)}]")
            else:
                fx_lb.insert(tk.END, f"{a.title}  [エフェクト]")

        ttk.Label(led_f, text="色（ドラッグ／RGB で細かく指定）", foreground="#555").pack(
            anchor="w", pady=(4, 0)
        )
        led_rgb = [255, 80, 40]
        color_preview = tk.Label(
            led_f, text="  ", bg="#%02x%02x%02x" % tuple(led_rgb), width=8, relief=tk.GROOVE
        )
        color_preview.pack(anchor="w", pady=4)

        def _set_rgb(rgb: tuple[int, int, int]) -> None:
            led_rgb[0], led_rgb[1], led_rgb[2] = int(rgb[0]), int(rgb[1]), int(rgb[2])
            color_preview.configure(bg="#%02x%02x%02x" % tuple(led_rgb))
            # 色だけ更新（エフェクト選択は維持。固定色にしたいときはモードで「固定色」を選ぶ）
            _schedule_preview()

        if ColorPalette is not None:
            led_palette = ColorPalette(
                led_f,
                initial=(led_rgb[0], led_rgb[1], led_rgb[2]),
                on_change=lambda r, g, b: _set_rgb((r, g, b)),
                width=180,
                height=140,
            )
            led_palette.pack(anchor="w", pady=(0, 4))
        else:
            led_palette = None
            presets = ttk.Frame(led_f)
            presets.pack(fill=tk.X)
            for label, rgb in _LED_COLOR_PRESETS:
                tk.Button(
                    presets,
                    text=label,
                    bg="#%02x%02x%02x" % rgb,
                    fg="#000" if sum(rgb) > 400 else "#fff",
                    relief="flat",
                    width=4,
                    command=lambda c=rgb: _set_rgb(c),
                ).pack(side=tk.LEFT, padx=2, pady=2)

        mode_catalog = _led_mode_catalog()
        mode_names = [n for n, _ in mode_catalog]
        mode_var = tk.StringVar(value=mode_names[0])
        row_mode = ttk.Frame(led_f)
        row_mode.pack(fill=tk.X, pady=6)
        ttk.Label(row_mode, text="モード").pack(side=tk.LEFT)
        mode_cb = ttk.Combobox(row_mode, textvariable=mode_var, values=mode_names, state="readonly", width=28)
        mode_cb.pack(side=tk.LEFT, padx=6)

        bright_var = tk.DoubleVar(value=100.0)
        speed_led_var = tk.IntVar(value=50)
        row_b = ttk.Frame(led_f)
        row_b.pack(fill=tk.X, pady=2)
        ttk.Label(row_b, text="明るさ").pack(side=tk.LEFT)
        ttk.Scale(row_b, from_=1, to=100, variable=bright_var, orient=tk.HORIZONTAL).pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=6
        )
        row_ls = ttk.Frame(led_f)
        row_ls.pack(fill=tk.X, pady=2)
        ttk.Label(row_ls, text="エフェクト速度").pack(side=tk.LEFT)
        ttk.Scale(row_ls, from_=1, to=100, variable=speed_led_var, orient=tk.HORIZONTAL).pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=6
        )

        def _apply_led_fx_asset(idx: int) -> None:
            if idx < 0 or idx >= len(_LED_FX_ASSETS):
                return
            asset = _LED_FX_ASSETS[idx]
            apply_led.set(True)
            apply_laser.set(False)
            title_var.set(asset.title)
            bright_var.set(float(asset.brightness))
            # 速度はバーで調整するため、アセットから上書きしない
            if asset.mode is None:
                # 固定色アセットだけ色をセット
                col = asset.rgb or (255, 255, 255)
                led_rgb[0], led_rgb[1], led_rgb[2] = col
                color_preview.configure(bg="#%02x%02x%02x" % col)
                if led_palette is not None:
                    try:
                        led_palette.set_rgb(*col, notify=False)
                    except Exception:
                        pass
                mode_var.set(mode_names[0] if mode_names else "固定色（RGB）")
            else:
                # エフェクト: 色は下のパレットのまま維持。モードだけ適用
                matched = False
                for n, mid in mode_catalog:
                    if mid is not None and int(mid) == int(asset.mode):
                        mode_var.set(n)
                        matched = True
                        break
                if not matched and mode_names:
                    mode_var.set(mode_names[0])
            _schedule_preview()

        def _on_fx_select(_e: tk.Event | None = None) -> None:
            sel = fx_lb.curselection()
            if not sel:
                return
            _apply_led_fx_asset(int(sel[0]))

        fx_lb.bind("<<ListboxSelect>>", _on_fx_select)
        fx_lb.bind("<Double-Button-1>", _on_fx_select)

        status_var = tk.StringVar(value="プレビュー待機（レーザー／LED をチェック）")
        ttk.Label(pad, textvariable=status_var, foreground="#2a6").grid(
            row=4, column=0, columnspan=2, sticky="w", pady=(8, 0)
        )

        pad.columnconfigure(1, weight=1)

        def _resolve_motion_index() -> int:
            if DOT_POINT_MOTIONS:
                for i, (n, _) in enumerate(DOT_POINT_MOTIONS):
                    if n == motion_var.get():
                        return i
            return 0

        def _resolve_ch9() -> int:
            for n, v in DOT_CH9_DOT_SWATCHES:
                if n == ch9_var.get():
                    return int(v)
            return 32

        def _resolve_led_mode() -> int | None:
            for n, mid in mode_catalog:
                if n == mode_var.get():
                    return mid
            return None

        def _build_preview_channels(ch9: int) -> list[int]:
            if apply_dot.get():
                return [
                    _DOT_BASE_CH1,
                    _DOT_BASE_CH2,
                    0,
                    0,
                    0,
                    64,
                    16,
                    _DOT_BASE_CH8,
                    ch9,
                    0,
                ]
            out = [self._read_channel_value(i) for i in range(10)]
            out[8] = ch9
            return out

        def _preview_now() -> None:
            if preview_closed["done"]:
                return
            preview_job["id"] = None
            parts: list[str] = []
            want_laser = bool(apply_laser.get())
            want_led = bool(apply_led.get())

            if want_laser:
                mi = _resolve_motion_index()
                ch9 = _resolve_ch9()
                channels_l = _build_preview_channels(ch9)
                self._motion_apply_dot_base.set(bool(apply_dot.get()))
                for i, v in enumerate(channels_l):
                    self._channel_vars[i].set(max(0, min(255, int(v))))
                self._club_dot_ch9 = ch9
                for i in range(10):
                    self._spin_from_var(i)
                cb = self._motion_combo
                if cb is not None and DOT_POINT_MOTIONS:
                    try:
                        cb.current(max(0, min(len(DOT_POINT_MOTIONS) - 1, mi)))
                    except tk.TclError:
                        pass
                sp = max(MOTION_SPEED_MIN, min(MOTION_SPEED_MAX, float(speed_var.get())))
                try:
                    self._motion_speed.set(sp)
                    self._motion_speed_scale.set(sp)
                except (tk.TclError, AttributeError):
                    pass
                if run_motion.get() and DOT_POINT_MOTIONS:
                    self._start_dot_motion()
                else:
                    self._stop_dot_motion()
                self._pattern_tile_hold_tile = PatternTileState(
                    title="__preview__",
                    channels=tuple(channels_l),  # type: ignore[arg-type]
                    motion_index=mi,
                    motion_speed=sp,
                    apply_dot_base=bool(apply_dot.get()),
                    club_dot_ch9=ch9,
                    run_motion=bool(run_motion.get()),
                    apply_laser=True,
                    apply_led=want_led,
                )
                self._tile_override_latched = True
                parts.append("レーザー")
            else:
                # レーザーOFF → 開いたときのレーザー状態へ戻す
                for i, v in enumerate(snap_channels):
                    self._channel_vars[i].set(max(0, min(255, int(v))))
                self._club_dot_ch9 = snap_club_ch9
                for i in range(10):
                    self._spin_from_var(i)
                cb = self._motion_combo
                if cb is not None and DOT_POINT_MOTIONS:
                    try:
                        cb.current(snap_motion_idx)
                    except tk.TclError:
                        pass
                try:
                    self._motion_speed.set(snap_speed)
                    self._motion_speed_scale.set(snap_speed)
                except (tk.TclError, AttributeError):
                    pass
                if snap_motion_running:
                    try:
                        self._start_dot_motion()
                    except Exception:
                        pass
                else:
                    self._stop_dot_motion()

            if want_led:
                payload = {
                    "r": int(led_rgb[0]),
                    "g": int(led_rgb[1]),
                    "b": int(led_rgb[2]),
                    "brightness": float(bright_var.get()),
                    "mode": _resolve_led_mode(),
                    "speed": int(float(speed_led_var.get())),
                }
                self._notify_tile_led_override(payload)
                self._tile_override_latched = True
                if self._pattern_tile_hold_tile is None or getattr(
                    self._pattern_tile_hold_tile, "title", ""
                ) != "__preview__":
                    self._pattern_tile_hold_tile = PatternTileState(
                        title="__preview__",
                        channels=tuple(snap_channels),  # type: ignore[arg-type]
                        motion_index=snap_motion_idx,
                        motion_speed=snap_speed,
                        apply_dot_base=snap_apply_dot,
                        club_dot_ch9=snap_club_ch9,
                        run_motion=False,
                        apply_laser=False,
                        apply_led=True,
                    )
                parts.append("LED")
            elif want_laser:
                # レーザーのみ: LED は触らない（凍結すると付けっぱなしの原因になる）
                pass
            else:
                # 両方OFF
                if getattr(self._pattern_tile_hold_tile, "title", "") == "__preview__":
                    self._pattern_tile_hold_tile = None
                self._tile_override_latched = False
                self._notify_tile_led_override(None)

            if parts:
                status_var.set("プレビュー中: " + " + ".join(parts))
            else:
                status_var.set("プレビュー待機（レーザー／LED をチェック）")

        def _schedule_preview(_a=None, _b=None, _c=None) -> None:
            if preview_closed["done"]:
                return
            try:
                spd_lbl.configure(text=f"{float(speed_var.get()):.2f}")
            except (tk.TclError, ValueError):
                pass
            jid = preview_job.get("id")
            if jid is not None:
                try:
                    self.after_cancel(jid)
                except (tk.TclError, ValueError):
                    pass
            preview_job["id"] = self.after(80, _preview_now)

        def _restore_snapshot() -> None:
            """ダイアログ開始前のレーザー／LED 状態へ戻す。"""
            if getattr(self._pattern_tile_hold_tile, "title", "") == "__preview__":
                self._pattern_tile_hold_tile = None
            self._pattern_tile_kb_hold_tile = None
            self._tile_override_latched = False
            self._notify_tile_led_override(None)
            self._motion_apply_dot_base.set(snap_apply_dot)
            for i, v in enumerate(snap_channels):
                self._channel_vars[i].set(max(0, min(255, int(v))))
            self._club_dot_ch9 = snap_club_ch9
            for i in range(10):
                self._spin_from_var(i)
            cb = self._motion_combo
            if cb is not None and DOT_POINT_MOTIONS:
                try:
                    cb.current(snap_motion_idx)
                except tk.TclError:
                    pass
            try:
                self._motion_speed.set(snap_speed)
                self._motion_speed_scale.set(snap_speed)
            except (tk.TclError, AttributeError):
                pass
            mic_live = bool(self._audio_enable.get() and self._audio_analyzer is not None)
            file_reactive = bool(self._file_playback_active and self._timeline_reactive_motion)
            if snap_motion_running or mic_live or file_reactive:
                try:
                    self._start_dot_motion()
                except Exception:
                    pass
            else:
                self._stop_dot_motion()

        def _close_dialog(*, restore: bool) -> None:
            if preview_closed["done"]:
                return
            preview_closed["done"] = True
            jid = preview_job.get("id")
            if jid is not None:
                try:
                    self.after_cancel(jid)
                except (tk.TclError, ValueError):
                    pass
                preview_job["id"] = None
            if restore:
                _restore_snapshot()
            else:
                # 登録後もプレビュー優先を解除してライブへ戻す（見た目は登録値のまま一瞬残る）
                if getattr(self._pattern_tile_hold_tile, "title", "") == "__preview__":
                    self._pattern_tile_hold_tile = None
                self._tile_override_latched = False
                self._notify_tile_led_override(None)
                live = bool(self._audio_enable.get() and self._audio_analyzer is not None)
                if live and self._motion_fn is None:
                    try:
                        self._start_dot_motion()
                    except Exception:
                        pass
            try:
                win.grab_release()
            except tk.TclError:
                pass
            try:
                win.destroy()
            except tk.TclError:
                pass

        def _save() -> None:
            name = str(title_var.get()).strip()[:80]
            if not name:
                messagebox.showwarning("名称", "表示名を入力してください。", parent=win)
                return
            if not apply_laser.get() and not apply_led.get():
                messagebox.showwarning(
                    "アセット",
                    "レーザーまたは LED のチェックを1つ以上入れてください（両方可）。",
                    parent=win,
                )
                return
            mi = _resolve_motion_index()
            ch9 = _resolve_ch9()
            channels_l = _build_preview_channels(ch9)
            # 保存直前にもう一度プレビューを確定
            _preview_now()
            tile = PatternTileState(
                title=name,
                channels=tuple(int(x) for x in channels_l),  # type: ignore[arg-type]
                motion_index=mi,
                motion_speed=max(MOTION_SPEED_MIN, min(MOTION_SPEED_MAX, float(speed_var.get()))),
                apply_dot_base=bool(apply_dot.get()),
                club_dot_ch9=ch9,
                run_motion=bool(run_motion.get()),
                apply_laser=bool(apply_laser.get()),
                apply_led=bool(apply_led.get()),
                led_r=int(led_rgb[0]),
                led_g=int(led_rgb[1]),
                led_b=int(led_rgb[2]),
                led_brightness=float(bright_var.get()),
                led_mode=_resolve_led_mode(),
                led_speed=int(float(speed_led_var.get())),
            )
            self._pattern_tiles.append(tile)
            self._save_pattern_tiles_to_disk()
            self._rebuild_pattern_tiles_grid()
            _close_dialog(restore=True)

        btns = ttk.Frame(pad)
        btns.grid(row=5, column=0, columnspan=2, sticky="e", pady=(12, 0))
        ttk.Button(btns, text="キャンセル", command=lambda: _close_dialog(restore=True)).pack(
            side=tk.RIGHT, padx=4
        )
        ttk.Button(btns, text="登録", command=_save).pack(side=tk.RIGHT, padx=4)

        # 変更でプレビュー
        for var in (apply_laser, apply_led, run_motion, apply_dot, motion_var, ch9_var, mode_var):
            var.trace_add("write", _schedule_preview)
        speed_var.trace_add("write", _schedule_preview)
        bright_var.trace_add("write", _schedule_preview)
        speed_led_var.trace_add("write", _schedule_preview)
        motion_cb.bind("<<ComboboxSelected>>", lambda _e: _schedule_preview())
        ch9_cb.bind("<<ComboboxSelected>>", lambda _e: _schedule_preview())
        mode_cb.bind("<<ComboboxSelected>>", lambda _e: _schedule_preview())

        win.protocol("WM_DELETE_WINDOW", lambda: _close_dialog(restore=True))

    def _delete_pattern_tile_at(self, index: int) -> None:
        if 0 <= index < len(self._pattern_tiles):
            doomed = self._pattern_tiles[index]
            doomed_srcs = [s for s, t in self._pattern_tile_holds.items() if t is doomed]
            for s in doomed_srcs:
                self._pattern_tile_hold_remove(s)
            if self._pattern_tile_key_target is doomed:
                self._pattern_tile_clear_key_target_full()
            del self._pattern_tiles[index]
            self._save_pattern_tiles_to_disk()
            self._rebuild_pattern_tiles_grid()

    def _export_pattern_tiles_dialog(self) -> None:
        path = filedialog.asksaveasfilename(
            parent=self._tiles_win or self,
            title="タイルを JSON 保存",
            defaultextension=".json",
            filetypes=[("JSON", "*.json"), ("すべて", "*.*")],
        )
        if not path:
            return
        try:
            data = []
            for t in self._pattern_tiles:
                d = asdict(t)
                d["channels"] = list(d["channels"])
                data.append(d)
            Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as e:
            messagebox.showerror("保存失敗", str(e), parent=self._tiles_win or self)

    def _import_pattern_tiles_dialog(self) -> None:
        path = filedialog.askopenfilename(
            parent=self._tiles_win or self,
            title="タイル JSON を読込",
            filetypes=[("JSON", "*.json"), ("すべて", "*.*")],
        )
        if not path:
            return
        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            messagebox.showerror("読込失敗", str(e), parent=self._tiles_win or self)
            return
        if not isinstance(raw, list):
            messagebox.showerror("形式エラー", "JSON は配列である必要があります。", parent=self._tiles_win or self)
            return
        add = messagebox.askyesno(
            "既存タイル",
            "既存のタイルに「追加」しますか？\nいいえ＝全部置き換え",
            parent=self._tiles_win or self,
        )
        new_list: list[PatternTileState] = []
        for item in raw:
            if isinstance(item, dict):
                t = self._tile_from_dict(item)
                if t is not None:
                    new_list.append(t)
        if add:
            self._pattern_tiles.extend(new_list)
        else:
            self._pattern_tiles = new_list
        self._save_pattern_tiles_to_disk()
        self._rebuild_pattern_tiles_grid()

    def _rebuild_pattern_tiles_grid(self) -> None:
        inner = self._tiles_inner
        if inner is None:
            return

        # Configure 連打での再構築でもポン出しを殺さない
        keep_holds = dict(self._pattern_tile_holds)
        keep_order = list(self._pattern_tile_hold_order)
        keep_key = self._pattern_tile_key_target
        key_idx = next((i for i, t in enumerate(self._pattern_tiles) if t is keep_key), -1)
        preview_hold = any(
            getattr(t, "title", "") == "__preview__" and t not in self._pattern_tiles
            for t in keep_holds.values()
        )

        self._tiles_rebuilding = True
        self._tile_press_frame = None
        self._pattern_tile_key_target_frame = None
        self._pattern_tile_hold_frames.clear()

        try:
            for w in inner.winfo_children():
                w.destroy()

            tiles = self._pattern_tiles
            if not tiles:
                tk.Label(
                    inner,
                    text="（タイルなし。「現在をキャプチャ」で追加）",
                    bg=inner.cget("bg"),
                    fg="#ccc",
                    font=("Segoe UI", 11),
                ).pack(pady=24)
                if not preview_hold and keep_holds:
                    self._end_pattern_tile_override()
                return

            inner.update_idletasks()
            W = max(400, inner.winfo_width())
            H = max(280, inner.winfo_height())
            self._tiles_last_geom = (int(W), int(H))
            n = len(tiles)
            touch = self._touch_friendly_ui.get()
            col_div = 120 if touch else 100
            min_cell = 112 if touch else 76
            pad = 16 if touch else 8
            cols = max(1, min(12, W // col_div))
            cols = max(1, min(cols, n))
            rows = (n + cols - 1) // cols
            cell = min((W - pad * (cols + 1)) // max(1, cols), (H - pad * (rows + 1)) // max(1, rows))
            cell = int(max(min_cell, min(cell, 320)))

            def bind_tile_events(fr: tk.Frame, tile: PatternTileState) -> None:
                if touch:

                    def tap(_e: tk.Event | None = None, t: PatternTileState = tile, f: tk.Frame = fr) -> None:
                        self._on_pattern_tile_touch_tap(t, f, _e)

                    fr.bind("<ButtonRelease-1>", tap)
                else:

                    def press(_e: tk.Event | None = None, t: PatternTileState = tile, f: tk.Frame = fr) -> None:
                        self._on_pattern_tile_press(t, f, _e)

                    def release(_e: tk.Event | None = None) -> None:
                        self._on_pattern_tile_release()

                    def motion(_e: tk.Event | None = None, t: PatternTileState = tile) -> None:
                        self._on_pattern_tile_motion(t, _e)

                    fr.bind("<ButtonPress-1>", press)
                    fr.bind("<ButtonRelease-1>", release)
                    fr.bind("<B1-Motion>", motion)

            active_idxs = {
                i
                for i, tile in enumerate(tiles)
                if any(t is tile for t in keep_holds.values())
            }

            for i, tile in enumerate(tiles):
                r, c = divmod(i, cols)
                active = i in active_idxs
                fr = tk.Frame(
                    inner,
                    width=cell,
                    height=cell,
                    bg="#333",
                    highlightthickness=3 if active else 2,
                    highlightbackground="#4af" if active else "#666",
                    cursor="hand2",
                )
                fr.grid(row=r, column=c, padx=pad // 2, pady=pad // 2, sticky="")
                fr.grid_propagate(False)
                bind_tile_events(fr, tile)
                for src, ht in keep_holds.items():
                    if ht is tile:
                        self._pattern_tile_hold_frames[src] = fr
                        if self._tile_press_frame is None:
                            self._tile_press_frame = fr
                if i == key_idx:
                    self._pattern_tile_key_target = tile
                    self._pattern_tile_key_target_frame = fr
                    try:
                        fr.configure(highlightbackground="#fa0", highlightthickness=3)
                    except tk.TclError:
                        pass
                cap = self._short_tile_caption(tile)
                lbl = tk.Label(
                    fr,
                    text=cap,
                    bg="#333",
                    fg="#f0f0f0",
                    font=("Segoe UI", 10),
                    wraplength=max(40, cell - 16),
                    justify=tk.CENTER,
                    cursor="hand2",
                )
                lbl.place(relx=0.5, rely=0.5, anchor=tk.CENTER)
                if touch:
                    lbl.bind(
                        "<ButtonRelease-1>",
                        lambda e, t=tile, f=fr: self._on_pattern_tile_touch_tap(t, f, e),
                    )
                else:
                    lbl.bind("<ButtonPress-1>", lambda e, t=tile, f=fr: self._on_pattern_tile_press(t, f, e))
                    lbl.bind("<ButtonRelease-1>", lambda _e: self._on_pattern_tile_release())
                    lbl.bind("<B1-Motion>", lambda e, t=tile: self._on_pattern_tile_motion(t, e))
                if touch:
                    del_btn = tk.Button(
                        fr,
                        text="×",
                        font=("Segoe UI", 11, "bold"),
                        fg="#fff",
                        bg="#662222",
                        activebackground="#883333",
                        activeforeground="#fff",
                        bd=0,
                        padx=4,
                        pady=0,
                        cursor="hand2",
                        command=lambda idx=i: self._delete_pattern_tile_at(idx),
                    )
                    del_btn.place(relx=1.0, rely=0.0, x=-2, y=2, anchor="ne")
                else:
                    lbl.bind("<Button-3>", lambda e, idx=i: self._pattern_tile_context_menu(e, idx))
                    fr.bind("<Button-3>", lambda e, idx=i: self._pattern_tile_context_menu(e, idx))

            for c in range(cols):
                inner.columnconfigure(c, weight=1)
            for r in range(rows):
                inner.rowconfigure(r, weight=1)

            # 保持ソースを復元（消えたタイルは捨てる）
            restored: dict[str, PatternTileState] = {}
            for src, ht in keep_holds.items():
                if ht in tiles or getattr(ht, "title", "") == "__preview__":
                    restored[src] = ht
            self._pattern_tile_holds = restored
            self._pattern_tile_hold_order = [s for s in keep_order if s in restored]
            self._pattern_tile_hold_tile = next(
                (
                    restored[s]
                    for s in reversed(self._pattern_tile_hold_order)
                    if s == "ptr" or s.startswith("tap:")
                ),
                None,
            )
            self._pattern_tile_kb_hold_tile = next(
                (
                    restored[s]
                    for s in reversed(self._pattern_tile_hold_order)
                    if not self._is_pointer_or_auto_hold_source(s)
                ),
                None,
            )
            if keep_key is not None and key_idx < 0:
                self._pattern_tile_key_target = None
            if keep_holds and not restored and not preview_hold:
                self._end_pattern_tile_override()
        finally:
            # Configure 再入を避けるため、次のイベントループでフラグ解除
            try:
                self.after_idle(self._tiles_clear_rebuilding_flag)
            except tk.TclError:
                self._tiles_rebuilding = False

    def _tiles_clear_rebuilding_flag(self) -> None:
        self._tiles_rebuilding = False

    def _schedule_tiles_grid_resize(self, event: tk.Event | None = None) -> None:
        if self._tiles_rebuilding:
            return
        if event is not None and event.widget is not self._tiles_inner:
            return
        inner = self._tiles_inner
        if inner is not None and event is not None:
            try:
                w, h = int(inner.winfo_width()), int(inner.winfo_height())
            except tk.TclError:
                return
            last = self._tiles_last_geom
            if last is not None and abs(last[0] - w) < 4 and abs(last[1] - h) < 4:
                return
        if self._tiles_resize_job is not None:
            try:
                self.after_cancel(self._tiles_resize_job)
            except tk.TclError:
                pass
        self._tiles_resize_job = self.after(90, self._tiles_resize_rebuild)

    def _tiles_resize_rebuild(self) -> None:
        self._tiles_resize_job = None
        if self._tiles_rebuilding:
            return
        if self._tiles_win is not None:
            try:
                if self._tiles_win.winfo_exists():
                    self._rebuild_pattern_tiles_grid()
            except tk.TclError:
                pass

    def _pattern_tile_context_menu(self, event: tk.Event, index: int) -> None:
        self._pattern_tile_context_menu_at(index, int(event.x_root), int(event.y_root))

    def _pattern_tile_context_menu_at(self, index: int, x_root: int, y_root: int) -> None:
        if not (0 <= index < len(self._pattern_tiles)):
            return
        menu = tk.Menu(self, tearoff=0)
        t = self._pattern_tiles[index]
        if t.hotkey:
            menu.add_command(
                label="キー割当を解除",
                command=lambda i=index: self._pattern_tile_clear_hotkey_at(i),
            )
            menu.add_separator()
        menu.add_command(label="削除", command=lambda: self._delete_pattern_tile_at(index))
        try:
            menu.tk_popup(x_root, y_root)
        finally:
            menu.grab_release()

    def _tiles_escape(self, _event: tk.Event | None = None) -> None:
        if self._pattern_tile_key_target is not None:
            self._pattern_tile_clear_key_target_full()
            return
        w = self._tiles_win
        if w is None:
            return
        try:
            if not w.winfo_exists():
                return
        except tk.TclError:
            return
        try:
            if int(w.attributes("-fullscreen")):
                w.attributes("-fullscreen", False)
                return
        except (tk.TclError, ValueError):
            pass
        self._close_pattern_tiles_window()

    def _tiles_toggle_fullscreen(self) -> None:
        w = self._tiles_win
        if w is None:
            return
        try:
            cur = int(w.attributes("-fullscreen"))
        except (tk.TclError, ValueError):
            cur = 0
        try:
            w.attributes("-fullscreen", not cur)
        except tk.TclError:
            try:
                w.state("zoomed" if not cur else "normal")
            except tk.TclError:
                pass

    def _open_pattern_tiles_window(self) -> None:
        if self._tiles_win is not None:
            try:
                if self._tiles_win.winfo_exists():
                    self._tiles_win.lift()
                    self._tiles_win.focus_force()
                    return
            except tk.TclError:
                pass
            self._tiles_win = None

        win = tk.Toplevel(self)
        self._tiles_win = win
        win.title("パターンタイル — ポン出し")
        win.minsize(480, 360)

        top = ttk.Frame(win, padding=8)
        top.pack(fill=tk.X)
        self._tiles_help_lbl = ttk.Label(top, text="", wraplength=int(self._if_touch(720, 820)))
        self._tiles_help_lbl.pack(anchor="w")
        self._refresh_pattern_tiles_help_label()

        bar = ttk.Frame(win, padding=(8, 0, 8, 6))
        bar.pack(fill=tk.X)
        self._tiles_toolbar_buttons.clear()
        b_full = ttk.Button(bar, text="全画面", command=self._tiles_toggle_fullscreen)
        b_full.pack(side=tk.LEFT, padx=(0, 8))
        b_cap = ttk.Button(bar, text="現在をキャプチャ…", command=self._capture_pattern_tile_dialog)
        b_cap.pack(side=tk.LEFT, padx=(0, 6))
        b_asset = ttk.Button(bar, text="アセットから登録…", command=self._register_pattern_tile_from_assets_dialog)
        b_asset.pack(side=tk.LEFT, padx=(0, 6))
        b_ledpack = ttk.Button(bar, text="LED光型パック追加", command=self._install_led_fx_asset_pack)
        b_ledpack.pack(side=tk.LEFT, padx=(0, 6))
        b_sav = ttk.Button(bar, text="保存…", command=self._export_pattern_tiles_dialog)
        b_sav.pack(side=tk.LEFT, padx=4)
        b_imp = ttk.Button(bar, text="読込…", command=self._import_pattern_tiles_dialog)
        b_imp.pack(side=tk.LEFT, padx=4)
        ttk.Checkbutton(
            bar,
            text="キー登録",
            variable=self._pattern_tile_key_assign_mode,
            command=self._on_pattern_tile_key_mode_toggled,
        ).pack(side=tk.LEFT, padx=(10, 4))
        self._tiles_key_status_lbl = ttk.Label(
            bar, text="", font=("Segoe UI", 9), foreground="#555", wraplength=380
        )
        self._tiles_key_status_lbl.pack(side=tk.LEFT, padx=(4, 8), fill=tk.X, expand=True)
        self._refresh_tiles_key_status_lbl()
        b_close = ttk.Button(bar, text="閉じる", command=self._close_pattern_tiles_window)
        b_close.pack(side=tk.RIGHT, padx=4)
        self._tiles_toolbar_buttons.extend([b_full, b_cap, b_asset, b_ledpack, b_sav, b_imp, b_close])
        self._sync_tiles_toolbar_button_styles()

        host = tk.Frame(win, bg="#252525")
        host.pack(fill=tk.BOTH, expand=True, padx=6, pady=(0, 8))
        self._tiles_inner = host
        self._tiles_last_geom = None
        self._tiles_rebuilding = False

        host.bind("<Configure>", self._schedule_tiles_grid_resize)
        self._sync_tiles_win_release_binding()
        win.bind("<Escape>", self._tiles_escape)

        self._rebuild_pattern_tiles_grid()
        self.after(80, self._rebuild_pattern_tiles_grid)

        def _on_close() -> None:
            self._pattern_tile_key_target = None
            self._pattern_tile_key_target_frame = None
            self._end_pattern_tile_override()
            if self._tiles_resize_job is not None:
                try:
                    self.after_cancel(self._tiles_resize_job)
                except tk.TclError:
                    pass
                self._tiles_resize_job = None
            try:
                win.unbind_all("<MouseWheel>")
            except tk.TclError:
                pass
            self._tiles_toolbar_buttons.clear()
            self._tiles_help_lbl = None
            self._tiles_key_status_lbl = None
            self._tiles_win = None
            self._tiles_inner = None
            try:
                win.destroy()
            except tk.TclError:
                pass

        win.protocol("WM_DELETE_WINDOW", _on_close)

    def _close_pattern_tiles_window(self) -> None:
        self._pattern_tile_key_target = None
        self._pattern_tile_key_target_frame = None
        # タッチキープ中でも確実に解除（_on_pattern_tile_release はタッチ時に no-op）
        self._end_pattern_tile_override()
        if self._tiles_resize_job is not None:
            try:
                self.after_cancel(self._tiles_resize_job)
            except tk.TclError:
                pass
            self._tiles_resize_job = None
        w = self._tiles_win
        self._tiles_win = None
        self._tiles_inner = None
        self._tiles_toolbar_buttons.clear()
        self._tiles_help_lbl = None
        self._tiles_key_status_lbl = None
        if w is not None:
            try:
                w.unbind_all("<MouseWheel>")
            except tk.TclError:
                pass
            try:
                w.destroy()
            except tk.TclError:
                pass

    def _on_ch2_pattern_selected(self, _evt: tk.Event | None = None) -> None:
        cb = self._ch2_combo
        if cb is None or not self._ch2_patterns:
            return
        i = cb.current()
        if i < 0:
            return
        self._apply_ch2_pattern_index(i)

    def _sync_ch2_combo_from_value(self, v: int) -> None:
        cb = self._ch2_combo
        if cb is None or not self._ch2_patterns:
            return
        for i, (s, e, _) in enumerate(self._ch2_patterns):
            if s <= v <= e:
                cb.current(i)
                return
        cb.set("")

    def _apply_dot_look_channels(self) -> None:
        """CH1／CH2／CH8 を疑似点の土台に（モーションは停止しない）。"""
        self._channel_vars[0].set(_DOT_BASE_CH1)
        self._spin_from_var(0)
        self._channel_vars[1].set(_DOT_BASE_CH2)
        self._spin_from_var(1)
        self._channel_vars[7].set(_DOT_BASE_CH8)
        self._spin_from_var(7)

    def _apply_dot_look_preset(self) -> None:
        """pattern.txt の疑似点：CH1／CH2／CH8 のみ（位置などはそのまま）。モーションは止める。"""
        self._stop_dot_motion()
        self._apply_dot_look_channels()

    def _apply_full_preset(self, values: tuple[int, ...]) -> None:
        """10CH を一度に反映（スライダー／CH2コンボも同期）。"""
        self._stop_dot_motion()
        if len(values) != 10:
            return
        for i, v in enumerate(values):
            self._channel_vars[i].set(max(0, min(255, int(v))))
        for i in range(10):
            self._spin_from_var(i)

    def _apply_selected_dot_scene(self) -> None:
        cb = getattr(self, "_dot_scene_combo", None)
        if cb is None:
            return
        self._apply_dot_scene_index(cb.current())

    def _spinbox_commit(self, idx: int) -> None:
        """スピンへの直接入力・アロー確定を反映（変数と表示のズレを除去）。"""
        try:
            w = self._spinboxes[idx]
            v = int(str(w.get()).strip() or "0")
        except (ValueError, tk.TclError, IndexError):
            v = 0
        v = max(0, min(255, v))
        self._channel_vars[idx].set(v)
        self._spin_from_var(idx)

    def _drag_scale_live(self, idx: int) -> None:
        """スライダーを掴んで動かしている間も毎フレーム反映。"""
        self._sync_scale_spin(idx)

    def _set_ch1_preset(self, v: int) -> None:
        self._channel_vars[0].set(v)
        self._spin_from_var(0)

    def _spin_from_var(self, idx: int) -> None:
        try:
            v = int(str(self._channel_vars[idx].get()))
        except (tk.TclError, ValueError):
            return
        v = max(0, min(255, v))
        scale: ttk.Scale = getattr(self, f"_scale_{idx}")
        cur = scale.get()
        if abs(round(cur) - v) >= 1:
            scale.set(v)
        self._on_values_changed()
        if idx == 1:
            self._sync_ch2_combo_from_value(v)
        if idx == 8:
            self._club_dot_ch9 = v

    def _sync_scale_spin(self, idx: int) -> None:
        scale: ttk.Scale = getattr(self, f"_scale_{idx}")
        v = int(round(float(scale.get())))
        self._channel_vars[idx].set(v)
        # 同値 set では trace が飛ぶことがあり、ドラッグ中の送信が欠けるので必ず送信
        self._on_values_changed()
        if idx == 1:
            self._sync_ch2_combo_from_value(v)
        if idx == 8:
            self._club_dot_ch9 = v

    def _read_channel_value(self, idx: int) -> int:
        """スライダーとスピンを両方読みズレがあるときは実スライダー値を優先（送信漏れ対策）。"""
        scale: ttk.Scale = getattr(self, f"_scale_{idx}")
        try:
            sv = int(round(float(scale.get())))
        except (tk.TclError, TypeError, ValueError):
            sv = 0
        try:
            iv = int(str(self._channel_vars[idx].get()))
        except (tk.TclError, ValueError):
            iv = sv
        if abs(sv - iv) > 1:
            return sv
        return iv

    def _universe_bytes(self) -> bytes:
        u = bytearray(DMX_CHANNELS)
        base = int(self._base_addr.get())
        base = max(1, min(503, base))
        self._base_addr.set(base)
        for i in range(10):
            v = self._read_channel_value(i)
            u[base - 1 + i] = max(0, min(255, v))
        return bytes(u)

    def _on_values_changed(self, *_args) -> None:
        if self._sender is None:
            return
        self._sender.set_universe_snapshot(self._universe_bytes())

    def _preferred_com_port(self) -> str:
        try:
            return str(load_settings().get("dmx_com_port", "") or "").strip()
        except Exception:
            return ""

    def _on_dmx_auto_connect_toggled(self) -> None:
        port = ""
        try:
            port = (self._port_combo.get() or "").strip()
        except (tk.TclError, AttributeError):
            port = ""
        if port:
            self._persist_dmx_connection(port)
            return
        try:
            update_settings(dmx_auto_connect=bool(self._dmx_auto_connect.get()))
        except Exception:
            pass

    def set_auto_connect(self, enabled: bool) -> None:
        """統合アプリの起動設定から同期する。"""
        if not hasattr(self, "_dmx_auto_connect"):
            self._dmx_auto_connect = tk.BooleanVar(value=bool(enabled))
        else:
            self._dmx_auto_connect.set(bool(enabled))

    def _persist_dmx_connection(self, port: str) -> None:
        port = (port or "").strip()
        if not port:
            return
        try:
            base = int(self._base_addr.get())
        except (tk.TclError, TypeError, ValueError):
            base = 1
        auto = False
        if hasattr(self, "_dmx_auto_connect"):
            try:
                auto = bool(self._dmx_auto_connect.get())
            except (tk.TclError, TypeError, ValueError):
                auto = False
        try:
            update_settings(
                dmx_com_port=port,
                dmx_base_addr=max(1, min(503, base)),
                dmx_auto_connect=auto,
            )
        except Exception:
            pass

    def _refresh_ports(self, init: bool = False) -> None:
        ports = list_com_ports()
        values = [p[0] for p in ports]
        self._port_combo["values"] = values if values else [""]
        preferred = self._preferred_com_port()
        if values:
            cur = (self._port_combo.get() or "").strip()
            if init and preferred and preferred in values:
                self._port_combo.set(preferred)
            elif cur in values:
                self._port_combo.set(cur)
            elif preferred and preferred in values:
                self._port_combo.set(preferred)
            else:
                self._port_combo.current(0)
        else:
            self._port_combo.set("")

    def try_auto_connect(self, *, silent: bool = True) -> bool:
        """前回の COM が利用可能なら送信開始。失敗時は silent ならダイアログなし。"""
        preferred = self._preferred_com_port()
        self._refresh_ports(init=True)
        if not preferred:
            return False
        values = [str(v) for v in (self._port_combo["values"] or ())]
        if preferred not in values:
            return False
        self._port_combo.set(preferred)
        return self._start_sending(silent=silent)

    def _clear_all(self) -> None:
        self._stop_dot_motion()
        for i, var in enumerate(self._channel_vars):
            var.set(0)
            getattr(self, f"_scale_{i}").set(0)
        self._sync_ch2_combo_from_value(0)
        self._club_dot_ch9 = 0
        self._on_values_changed()

    def _start_sending(self, *, silent: bool = False) -> bool:
        port = (self._port_combo.get() or "").strip()
        if not port:
            if not silent:
                messagebox.showwarning("ポート未選択", "COM ポートを選択してください。", parent=self)
            return False
        try:
            base = int(self._base_addr.get())
        except (tk.TclError, TypeError, ValueError):
            base = 1
        if base < 1 or base > DMX_CHANNELS - 10 + 1:
            if not silent:
                messagebox.showerror("無効なアドレス", "開始アドレスは 1〜503 で指定してください。", parent=self)
            return False

        self._stop_sending()
        try:
            ser = OpenDMXSender.try_open(port)
        except serial.SerialException as e:
            if not silent:
                messagebox.showerror(
                    "ポートを開けません",
                    f"{port} を開けませんでした。\n\n{e}\n\n"
                    "他のアプリが COM を掴んでいないか確認してください。\n"
                    "Enttec USB DMX PRO は Open DMX ではないため未対応です。",
                    parent=self,
                )
            return False

        self._sender = OpenDMXSender(ser)
        self._sender.set_universe_snapshot(self._universe_bytes())
        self._sender.start()
        self._persist_dmx_connection(port)
        return True

    def _stop_sending(self) -> None:
        if self._sender:
            self._sender.stop()
            self._sender.join(timeout=2.5)
            self._sender = None

    def attach_shared_audio(self, analyzer: Any | None) -> None:
        """統合アプリから共有の音楽解析エンジンを渡す。"""
        prev = self._shared_audio
        if analyzer is not None:
            self._shared_audio = analyzer
            self._audio_enable.set(True)
            self._audio_analyzer = analyzer
            try:
                s0 = analyzer.snapshot()
                self._last_impact_generation = int(s0.impact_generation)
                self._last_laser_pew_generation = int(s0.laser_pew_generation)
                self._last_shake_generation = int(s0.shake_generation)
            except (AttributeError, TypeError, ValueError):
                pass
            if self._motion_fn is None:
                self._start_dot_motion()
            self._cancel_audio_after_callbacks()
            self._audio_motion_picker_loop()
            self._audio_status_tick()
            return
        if self._audio_analyzer is prev:
            self._audio_analyzer = None
        self._shared_audio = None
        self._audio_enable.set(False)
        self._cancel_audio_after_callbacks()
        if self._audio_status_lbl is not None:
            self._audio_status_lbl.config(text="（オフ）")

    def cleanup(self) -> None:
        """埋め込み時用: ウィジェット破棄なしで送出・解析・モーションを止める。"""
        self._stop_dot_motion()
        self._stop_file_sync_playback()
        self._cancel_audio_after_callbacks()
        if self._audio_analyzer is not None and self._audio_analyzer is not self._shared_audio:
            self._stop_audio_analyzer()
        else:
            self._audio_analyzer = None
        self._close_pattern_palette()
        self._close_pattern_tiles_window()
        self._stop_sending()

    def destroy(self) -> None:
        self.cleanup()
        owns = self._owns_root
        root = self.winfo_toplevel()
        super().destroy()
        if owns:
            try:
                root.destroy()
            except tk.TclError:
                pass

    def mainloop(self, *args, **kwargs):  # type: ignore[override]
        self.winfo_toplevel().mainloop(*args, **kwargs)


def main() -> None:
    parser = argparse.ArgumentParser(description="Single Head RGB レーザー DMX GUI")
    parser.add_argument("--com", metavar="COMx", help="起動後に自動で送信開始するポート（例: COM3）")
    args = parser.parse_args()

    app = LaserDMXApp()
    if args.com:
        app._port_combo.set(args.com)
        app.after(300, lambda: app._start_sending(silent=False))
    else:
        try:
            if bool(load_settings().get("dmx_auto_connect", False)):
                app.after(400, lambda: app.try_auto_connect(silent=True))
        except Exception:
            pass
    app.mainloop()


if __name__ == "__main__":
    main()
