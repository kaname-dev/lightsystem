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
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, simpledialog, ttk

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
        load_profile_from_json,
        snapshot_at_time,
    )
except ImportError:
    PRECALC_AVAILABLE = False  # type: ignore[misc, assignment]
    TrackProfile = None  # type: ignore[misc, assignment]
    analyze_audio_file = None  # type: ignore[misc, assignment]
    load_profile_from_json = None  # type: ignore[misc, assignment]
    ensure_audio_buffer = None  # type: ignore[misc, assignment]

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
        motion_speed_from_snapshot,
        pick_impact_motion_index,
        pick_laser_pew_motion_index,
        pick_motion_index,
        pick_shake_horizontal_index,
    )

    _AUDIO_REACTIVE_AVAILABLE = True
except ImportError:
    ClubAudioAnalyzer = None  # type: ignore[misc, assignment]

    def effective_wobble_hz(s):  # type: ignore[no-redef]
        return float(getattr(s, "envelope_wobble_hz", 0.0))

    def ch9_from_snapshot(_s):  # type: ignore[no-redef]
        return 12

    def motion_speed_from_snapshot(_s, *, sens=1.0):  # type: ignore[no-redef]
        return max(0.25, min(16.0, 1.0 * sens))

    def pick_motion_index(_s, *, rng=None):  # type: ignore[no-redef]
        return 0

    def pick_impact_motion_index(_s, *, rng=None):  # type: ignore[no-redef]
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
MOTION_SPEED_MAX = 16.0

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
    """パターンタイル 1 枚分（10CH＋点モーション周り）。JSON 入出力可。"""

    title: str
    channels: tuple[int, int, int, int, int, int, int, int, int, int]
    motion_index: int
    motion_speed: float
    apply_dot_base: bool
    club_dot_ch9: int
    run_motion: bool
    hotkey: str | None = None


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
    """複数本が横に並び同調して震える風：多数位置を高速多重化＋共有の横シェイク。"""
    n = 6
    idx = int(ph * 24.0) % n
    bases = [0.07 + i * (0.86 / max(1, n - 1)) for i in range(n)]
    drift = 0.085 * math.sin(ph * 15.5)
    sx = bases[idx] + drift
    sy = 0.22 + 0.58 * ((idx * 2) % 3) / 2.0
    c6, c7 = _motion_xy(sx, sy)
    return c6, c7, _MOTION_CH9_PALETTE[int(ph * 13.0) % len(_MOTION_CH9_PALETTE)]


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
# ※疑似点の CH1／CH2／CH8（pattern.txt「点にする場合」の3つ）は通常の点モーションでは変更しない。
#    例外：水平線開閉・単発横ステップ点など CH1 を明示制御するプリセットあり。
DotMotionTick = Callable[
    [float],
    tuple[int | None, int | None, int | None]
    | tuple[int | None, int | None, int | None, int | None]
    | tuple[int | None, int | None, int | None, int | None, int | None]
    | tuple[int | None, int | None, int | None, int | None, int | None, int | None]
    | tuple[int | None, int | None, int | None, int | None, int | None, int | None, int | None],
]
DOT_POINT_MOTIONS: list[tuple[str, DotMotionTick]] = [
    ("水平スイープ＋色（CH7は手動）", _mot_horiz_color),
    ("垂直スイープ＋色（CH6は手動）", _mot_vert_color),
    ("サークル軌道＋色同期", _mot_circle_color),
    ("対角往来＋色ステップ", _mot_diag_color),
    ("リサージュ（8の字風）＋色", _mot_lissajous),
    ("色のみローテーション（CH6/CH7は手動）", _mot_color_only),
    ("床ライン高速スキャン＋色（CH7手動）", _mot_floor_fast),
    ("天井ライン左右＋色（CH7手動）", _mot_ceiling_sweep),
    ("スパイラル風（半径脈動）＋色", _mot_spiral),
    ("【点】単発横ステップ一方向（速度可・CH7/CH9は手動）", _mot_dot_pulse_step_horizontal),
    ("【クラブ】マルチビーム・共有横シェイク", _mot_multi_beam_swarm),
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
]


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
        self._tiles_key_status_lbl: ttk.Label | None = None
        self._tiles_resize_job: str | None = None
        self._club_dot_ch9 = 32  # 位置プリセットに載せる CH9（CH9スライダー／色一覧と同期）

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
        self._precalc_event_idx = 0

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
                    "タッチ向けUIオン: 長押しはOSが右クリック扱いになることがあるため、"
                    "「タップ」でパターンをキープし、同じタイルでもう一度タップで止めます（全CHゼロ寄り）。"
                    "削除は各タイル右上の×。キーでポン出し: ツールバー「キー登録」でタイル指定→キー押し。"
                    "全画面はツールバーから。Escで全画面解除／閉じます。"
                ),
            )
        else:
            lbl.configure(
                wraplength=wrap,
                text=(
                    "タイルを押している間だけパターンを送信。離すと全CHを0に戻します。"
                    "削除は右クリック。キーでポン出し: 「キー登録」にチェックしてタイルをクリック→任意のキー。"
                    "（Ctrl+クリックでタイルだけ指定してからキーでも可。キーは押している間だけ送出）"
                    "全画面はツールバーから。Escで全画面解除／閉じます。"
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
        self._pattern_tile_kb_hold_tile = None
        if self._tile_press_frame is not None:
            try:
                self._tile_press_frame.configure(highlightbackground="#666", highlightthickness=2)
            except tk.TclError:
                pass
            self._tile_press_frame = None
        self._pattern_tile_hold_tile = None
        self._clear_all()

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
                self._pattern_tile_kb_hold_tile = t
                self._apply_pattern_tile(t)
                return

    def _on_global_key_release(self, event: tk.Event) -> None:
        try:
            if not self.winfo_exists():
                return
        except tk.TclError:
            return
        if self._hotkey_focus_blocks_tile_hotkey(self.focus_get()):
            return
        keysym = event.keysym
        if keysym in _PATTERN_HOTKEY_MODIFIER_KEYS:
            return
        nt = _pattern_hotkey_normalize(keysym)
        held = self._pattern_tile_kb_hold_tile
        if held is None:
            return
        if held.hotkey and _pattern_hotkey_normalize(held.hotkey) == nt:
            self._pattern_tile_kb_release_clear()

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

    def _build_ui(self) -> None:
        shell = ttk.Frame(self)
        shell.pack(fill=tk.BOTH, expand=True)

        top_body = self._add_collapsible_section(shell, "接続・DMX 送信", default_open=True, expand=False)
        top = ttk.Frame(top_body, padding=4)
        top.pack(fill=tk.X)

        top.columnconfigure(1, weight=1)

        ttk.Label(top, text="COM ポート").grid(row=0, column=0, sticky="w")
        self._port_combo = ttk.Combobox(top, width=int(self._if_touch(34, 40)), state="readonly")
        self._port_combo.grid(row=0, column=1, padx=(8, 4), sticky="ew")
        ttk.Button(top, text="再検索", command=self._refresh_ports).grid(row=0, column=2, padx=4)

        ttk.Label(top, text="開始アドレス (1–503)").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self._base_addr_spin = ttk.Spinbox(
            top,
            from_=1,
            to=503,
            width=int(self._if_touch(10, 14)),
            textvariable=self._base_addr,
            command=self._on_values_changed,
        )
        self._base_addr_spin.grid(row=1, column=1, sticky="w", pady=(6, 0))

        btns = ttk.Frame(top)
        btns.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(10, 0))
        ttk.Button(btns, text="送信開始", command=self._start_sending).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(btns, text="送信停止", command=self._stop_sending).pack(side=tk.LEFT, padx=6)

        ttk.Checkbutton(
            top,
            text="タッチ向けUI（大きめ操作域・タイルはタップで切替）",
            variable=self._touch_friendly_ui,
            command=self._on_touch_friendly_toggled,
        ).grid(row=3, column=0, columnspan=3, sticky="w", pady=(10, 0))

        presets_body = self._add_collapsible_section(
            shell, "モード別プリセット（CH1）", default_open=True, expand=False
        )
        presets = ttk.Frame(presets_body, padding=4)
        presets.pack(fill=tk.X)
        f = ttk.Frame(presets)
        f.pack(fill=tk.X)
        presets_map = [
            ("ブラックアウト（0–63）", 0),
            ("マニュアル（64–127）", 100),
            ("オート（128–191）", 160),
            ("サウンド（192–255）", 220),
        ]
        for txt, val in presets_map:
            ttk.Button(f, text=txt, width=22, command=lambda v=val: self._set_ch1_preset(v)).pack(
                side=tk.LEFT, padx=4, pady=2
            )

        if self._ch2_patterns:
            dot_f = ttk.Frame(presets)
            dot_f.pack(fill=tk.X, pady=(8, 0))
            ttk.Label(dot_f, text="CH2 図形は pattern.txt に準拠").pack(side=tk.LEFT, padx=(0, 8))
            ttk.Button(
                dot_f,
                text="疑似点の土台だけ（CH1／CH2／CH8 のみ）",
                command=self._apply_dot_look_preset,
            ).pack(side=tk.LEFT, padx=4)

        club_body = self._add_collapsible_section(
            shell,
            "クラブ疑似点 — 静止シーン＋点モーション（1点・位置 CH6/CH7／色 CH9）",
            default_open=True,
            expand=False,
        )
        scene_dot = ttk.Frame(club_body, padding=4)
        scene_dot.pack(fill=tk.X)
        sf = ttk.Frame(scene_dot)
        sf.pack(fill=tk.X)
        ttk.Label(sf, text="静止", width=6).pack(side=tk.LEFT)
        self._dot_scene_combo = ttk.Combobox(
            sf,
            values=[n for n, _ch6, _ch7 in CLUB_DOT_POSITION_PRESETS],
            width=int(self._if_touch(52, 58)),
            state="readonly",
        )
        self._dot_scene_combo.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(4, 8))
        if CLUB_DOT_POSITION_PRESETS:
            self._dot_scene_combo.current(0)
        ttk.Button(sf, text="適用", width=10, command=self._apply_selected_dot_scene).pack(side=tk.LEFT)
        ttk.Button(
            sf,
            text="別ウィンドウで一覧…",
            command=self._open_pattern_palette,
        ).pack(side=tk.LEFT, padx=(10, 0))
        ttk.Button(
            sf,
            text="タイルでポン出し…",
            command=self._open_pattern_tiles_window,
        ).pack(side=tk.LEFT, padx=(8, 0))

        ttk.Separator(scene_dot, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=(10, 8))
        ttk.Label(scene_dot, text="点モーション（連続変化）", font=("Segoe UI", 9, "bold")).pack(anchor="w")
        ttk.Checkbutton(
            scene_dot,
            text="開始時・種類切替時に疑似点の土台をセット（CH1／CH2／CH8）",
            variable=self._motion_apply_dot_base,
        ).pack(anchor="w")
        mf = ttk.Frame(scene_dot)
        mf.pack(fill=tk.X, pady=(4, 0))
        ttk.Label(mf, text="種類").pack(side=tk.LEFT)
        self._motion_combo = ttk.Combobox(
            mf,
            values=[n for n, _fn in DOT_POINT_MOTIONS],
            width=int(self._if_touch(38, 44)),
            state="readonly",
        )
        self._motion_combo.pack(side=tk.LEFT, padx=(6, 12))
        if DOT_POINT_MOTIONS:
            self._motion_combo.current(0)
        self._motion_combo.bind("<<ComboboxSelected>>", self._on_motion_combo_selected)
        ttk.Label(mf, text="速度").pack(side=tk.LEFT)
        self._motion_speed_scale = ttk.Scale(
            mf,
            from_=MOTION_SPEED_MIN,
            to=MOTION_SPEED_MAX,
            orient=tk.HORIZONTAL,
            length=int(self._if_touch(180, 280)),
            command=lambda v: self._motion_speed.set(
                max(MOTION_SPEED_MIN, min(MOTION_SPEED_MAX, float(v)))
            ),
        )
        self._motion_speed_scale.pack(side=tk.LEFT, padx=(4, 8))
        self._motion_speed_scale.set(1.35)
        ttk.Button(mf, text="モーション開始", command=self._start_dot_motion).pack(side=tk.LEFT, padx=4)
        ttk.Button(mf, text="停止", command=self._stop_dot_motion).pack(side=tk.LEFT, padx=4)
        _mot_hint = ttk.Label(
            scene_dot,
            text="モーションが更新しないチャンネル（戻り値 None）は、実行中もスライダー／スピンで調整できます。",
            foreground="#444",
            wraplength=int(self._if_touch(560, 680)),
            font=("Segoe UI", 8),
        )
        _mot_hint.pack(anchor="w", pady=(4, 0))
        self._touch_wraplength_items.append((_mot_hint, 560, 680))

        audio_section_body = self._add_collapsible_section(
            shell,
            "オーディオ・リアクティブ（BluetoothLED 音楽解析）— モーション・速度・色（CH9）＋音楽ファイル事前解析",
            default_open=True,
            expand=False,
        )
        audio_frame = ttk.Frame(audio_section_body, padding=4)
        audio_frame.pack(fill=tk.X)
        au_top = ttk.Frame(audio_frame)
        au_top.pack(fill=tk.X)
        if self._hide_audio_section:
            ttk.Label(
                au_top,
                text="マイク連動は統合アプリ上部の「共有 AI リアクティブ」を使用します（下のファイル事前解析はそのまま利用可）",
                foreground="#444",
                wraplength=int(self._if_touch(620, 720)),
            ).pack(side=tk.LEFT)
            self._audio_chk = None
        else:
            self._audio_chk = ttk.Checkbutton(
                au_top,
                text="オン（BluetoothLED 解析でモーション種類も自動切替）",
                variable=self._audio_enable,
                command=self._on_audio_reactive_toggled,
                state="normal" if _AUDIO_REACTIVE_AVAILABLE else "disabled",
            )
            self._audio_chk.pack(side=tk.LEFT)
        if not self._hide_audio_section:
            ttk.Button(au_top, text="入力デバイス再検索", command=self._refresh_audio_input_devices).pack(
                side=tk.LEFT, padx=(12, 0)
            )
        au_row2 = ttk.Frame(audio_frame)
        if not self._hide_audio_section:
            au_row2.pack(fill=tk.X, pady=(6, 0))
        ttk.Label(au_row2, text="マイク").pack(side=tk.LEFT)
        self._audio_device_combo = ttk.Combobox(
            au_row2,
            width=int(self._if_touch(52, 58)),
            state="readonly" if _AUDIO_REACTIVE_AVAILABLE else "disabled",
        )
        self._audio_device_combo.pack(side=tk.LEFT, padx=(6, 8), fill=tk.X, expand=True)
        ttk.Label(au_row2, text="感度（速度＋無音判定）").pack(side=tk.LEFT)
        self._audio_sens_scale = ttk.Scale(
            au_row2,
            from_=0.45,
            to=1.85,
            orient=tk.HORIZONTAL,
            length=int(self._if_touch(140, 220)),
            command=lambda v: self._audio_sensitivity.set(max(0.45, min(1.85, float(v)))),
        )
        self._audio_sens_scale.pack(side=tk.LEFT, padx=(4, 0))
        self._audio_sens_scale.set(1.0)

        # 高さ範囲（音声連動・点モーション共通）— 統合アプリでも常に表示
        au_h = ttk.Frame(audio_frame)
        au_h.pack(fill=tk.X, pady=(8, 0))
        ttk.Label(au_h, text="高さ範囲").pack(side=tk.LEFT)
        ttk.Label(au_h, text="下端%").pack(side=tk.LEFT, padx=(10, 2))
        self._audio_sy_lo_scale = ttk.Scale(
            au_h,
            from_=0.0,
            to=98.0,
            orient=tk.HORIZONTAL,
            length=int(self._if_touch(120, 180)),
            command=self._on_audio_sy_range_scale,
        )
        self._audio_sy_lo_scale.pack(side=tk.LEFT, padx=(0, 8))
        self._audio_sy_lo_scale.set(float(self._audio_sy_lo_pct.get()))
        ttk.Label(au_h, text="上端%").pack(side=tk.LEFT, padx=(4, 2))
        self._audio_sy_hi_scale = ttk.Scale(
            au_h,
            from_=2.0,
            to=100.0,
            orient=tk.HORIZONTAL,
            length=int(self._if_touch(120, 180)),
            command=self._on_audio_sy_range_scale,
        )
        self._audio_sy_hi_scale.pack(side=tk.LEFT, padx=(0, 8))
        self._audio_sy_hi_scale.set(float(self._audio_sy_hi_pct.get()))
        self._audio_sy_range_lbl = ttk.Label(
            au_h,
            text="",
            foreground="#333",
            font=("Segoe UI", 8),
        )
        self._audio_sy_range_lbl.pack(side=tk.LEFT, padx=(4, 0))
        self._apply_audio_sy_range_from_ui(announce=False)
        _h_hint = ttk.Label(
            audio_frame,
            text="0%=床（CH7最小）／100%=天井（CH7最大）。音声モーションの上下移動はこの範囲に収まります。",
            foreground="#666",
            font=("Segoe UI", 8),
            wraplength=int(self._if_touch(620, 720)),
        )
        _h_hint.pack(anchor="w", pady=(2, 0))
        self._touch_wraplength_items.append((_h_hint, 620, 720))

        self._audio_status_lbl = ttk.Label(
            audio_frame,
            text=(
                "共有 AI 連動中は、統合アプリ側の解析ステータスを参照してください。"
                if self._hide_audio_section
                else (
                    "（オフ）BluetoothLED 音楽解析の BPM・ジャンル推定がここに表示されます。"
                    if _AUDIO_REACTIVE_AVAILABLE
                    else "オーディオ連動には pip install numpy sounddevice が必要です。"
                )
            ),
            foreground="#444",
            wraplength=int(self._if_touch(620, 720)),
            font=("Segoe UI", 8),
        )
        if not self._hide_audio_section:
            self._audio_status_lbl.pack(anchor="w", pady=(6, 0))
            self._touch_wraplength_items.append((self._audio_status_lbl, 620, 720))
            self._refresh_audio_input_devices(init=True)

        precalc_frm = tk.LabelFrame(audio_frame, text="音楽ファイル・事前解析", padx=8, pady=8)
        precalc_frm.pack(fill=tk.X, pady=(8, 0))
        self._precalc_status = tk.StringVar(
            value=(
                "未解析（wav/mp3 等を選んで解析）"
                if PRECALC_AVAILABLE
                else "要: pip install librosa soundfile"
            )
        )
        _prec_st = tk.Label(
            precalc_frm,
            textvariable=self._precalc_status,
            font=("Segoe UI", 9),
            wraplength=int(self._if_touch(620, 720)),
        )
        _prec_st.pack(anchor="w")
        self._touch_wraplength_items.append((_prec_st, 620, 720))
        _prec_hint = ttk.Label(
            precalc_frm,
            text=(
                "取り込み解析後、「同期再生」で曲と同時にレーザーを駆動します。"
                "事前イベント（ピュン／揺れ／突発）と拍でモーションが切り替わり、"
                "解析スペクトルベースで CH9・速度も更新されます（マイクのオン／オフに依存しません）。"
                "点モーション未開始なら自動で開始します。"
            ),
            wraplength=int(self._if_touch(620, 720)),
            foreground="#555",
            font=("Segoe UI", 8),
        )
        _prec_hint.pack(anchor="w", pady=(2, 0))
        self._touch_wraplength_items.append((_prec_hint, 620, 720))
        pf = tk.Frame(precalc_frm)
        pf.pack(fill=tk.X, pady=4)
        self._btn_precalc_analyze = tk.Button(
            pf,
            text="ファイルを解析…",
            command=self._precalc_analyze_pick,
        )
        self._btn_precalc_analyze.pack(side=tk.LEFT, padx=(0, 6))
        self._btn_precalc_save_json = tk.Button(
            pf, text="JSON保存", command=self._precalc_save_json, state=tk.DISABLED
        )
        self._btn_precalc_save_json.pack(side=tk.LEFT, padx=(0, 6))
        self._btn_precalc_load_json = tk.Button(pf, text="JSONを開く", command=self._precalc_load_json)
        self._btn_precalc_load_json.pack(side=tk.LEFT, padx=(0, 6))
        self._btn_play_sync = tk.Button(pf, text="同期再生", command=self._toggle_file_playback, state=tk.DISABLED)
        self._btn_play_sync.pack(side=tk.LEFT, padx=(0, 6))

        self._precalc_log = scrolledtext.ScrolledText(
            precalc_frm, height=5, font=("Consolas", 8), wrap=tk.WORD
        )
        self._precalc_log.pack(fill=tk.BOTH, expand=True, pady=(4, 0))

        info_body = self._add_collapsible_section(
            shell, "接続・機器メモ（DMX・本機の制約）", default_open=False, expand=False
        )
        info = (
            "接続：USB-DMX512（Open DMX / FTDI RS485 が多い構成を想定）\n"
            "シリアル：250000 bps・8データ・无奇偶・2ストップ。\n"
            "※ Enttec USB DMX PRO などは別プロトコルのため、このアプリでは使えません。\n"
            "※ 本機の 10CH にディマー（輝度）チャンネルはなく、アプリから出力強さだけを下げることはできません。"
        )
        _info_lbl = ttk.Label(info_body, text=info, wraplength=int(self._if_touch(680, 780)), foreground="#444")
        _info_lbl.pack(fill=tk.X, padx=2)
        self._touch_wraplength_items.append((_info_lbl, 680, 780))

        sliders_body = self._add_collapsible_section(
            shell, "チャンネル値（各 0–255）— 開始アドレスから10CH", default_open=True, expand=True
        )
        sliders = ttk.Frame(sliders_body, padding=4)
        sliders.pack(fill=tk.BOTH, expand=True)

        canvas = tk.Canvas(sliders, highlightthickness=0)
        vsb = ttk.Scrollbar(sliders, orient=tk.VERTICAL, command=canvas.yview)
        inner = ttk.Frame(canvas)

        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=vsb.set)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)

        for idx, ((name, tip), var) in enumerate(zip(self.CH_LABELS, self._channel_vars, strict=True)):
            blk = ttk.Frame(inner, padding=(0, 8))
            blk.pack(fill=tk.X)

            lh = ttk.Frame(blk)
            lh.pack(fill=tk.X)
            ttk.Label(lh, text=f"{name}", width=5, anchor="w").pack(side=tk.LEFT)
            tip_lbl = ttk.Label(lh, text=tip, wraplength=int(self._if_touch(520, 640)), font=("Segoe UI", 9))
            tip_lbl.pack(side=tk.LEFT, fill=tk.X, padx=(6, 0))
            self._touch_wraplength_items.append((tip_lbl, 520, 640))

            row_ctrl = ttk.Frame(blk)
            row_ctrl.pack(fill=tk.X, pady=(4, 0))
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
            # スライダー操作中も毎コマ送信（ドラッグ中の値取りこぼし対策）
            sc.bind("<B1-Motion>", lambda _e, i=idx: self._drag_scale_live(i))
            sc.bind("<ButtonRelease-1>", lambda _e, i=idx: self._drag_scale_live(i))

            if idx == 1 and self._ch2_patterns:
                row_pat = ttk.Frame(blk)
                row_pat.pack(fill=tk.X, pady=(6, 0))
                ttk.Label(row_pat, text="図形", width=5, anchor="w").pack(side=tk.LEFT)
                labels = [f"{s}–{e} {n}" for s, e, n in self._ch2_patterns]
                cb = ttk.Combobox(row_pat, values=labels, width=int(self._if_touch(58, 66)), state="readonly")
                cb.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(6, 0))
                cb.bind("<<ComboboxSelected>>", self._on_ch2_pattern_selected)
                self._ch2_combo = cb

        def _on_wheel(e: tk.Event) -> None:
            canvas.yview_scroll(int(-1 * (e.delta / 120)), "units")

        canvas.bind("<Enter>", lambda _e: canvas.bind_all("<MouseWheel>", _on_wheel))
        canvas.bind("<Leave>", lambda _e: canvas.unbind_all("<MouseWheel>"))

        bottom = ttk.Frame(sliders_body, padding=(0, 8, 0, 4))
        bottom.pack(fill=tk.X)
        ttk.Button(bottom, text="全チャンネル 0（ブラックアウト寄り）", command=self._clear_all).pack(side=tk.LEFT)

        self._refresh_ports(init=True)
        self.after_idle(self._boot_sync_channels)
        self.after_idle(self._fit_initial_geometry)
        self._sync_touch_widget_sizes()

    def _boot_sync_channels(self) -> None:
        for i in range(10):
            self._spin_from_var(i)
        self._club_dot_ch9 = max(0, min(255, self._read_channel_value(8)))

    def _apply_ch2_pattern_index(self, i: int) -> None:
        """pattern.txt の CH2 レンジ一覧のインデックス i を適用（中央値）。"""
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

        if file_sync:
            prof = self._track_profile
            elapsed = time.monotonic() - self._file_t0
            dur = float(getattr(prof, "duration_sec", 0.0))
            if elapsed >= dur:
                self._stop_file_sync_playback()
                file_sync = False
            else:
                snap_for_audio = snapshot_at_time(prof, elapsed)
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
            if not snap_for_audio.valid or not snap_for_audio.music_active:
                phase_gain = 0.0
            elif getattr(snap_for_audio, "laser_hold", False):
                # 溜め中は動きを止め、後段で CH1 ブラックアウト
                phase_gain = 0.0
        self._motion_phase += _MOTION_DPH_BASE * sp * phase_gain
        step = fn(self._motion_phase)
        ch6_opt = step[0]
        ch7_opt = step[1]
        ch9_opt = step[2]
        ch8_opt = step[3] if len(step) > 3 else None
        ch2_opt = step[4] if len(step) > 4 else None
        ch4_opt = step[5] if len(step) > 5 else None
        ch1_opt = step[6] if len(step) > 6 else None
        if ch6_opt is not None:
            self._channel_vars[5].set(max(0, min(255, int(ch6_opt))))
        if ch7_opt is not None:
            self._channel_vars[6].set(max(0, min(255, int(ch7_opt))))
        # 音声連動／ファイル同期中はモーションの色を無視し、後段で LED 色を適用する
        led_color_drive = file_sync or (
            self._audio_enable.get() and self._audio_analyzer is not None
        )
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
        if (
            not file_sync
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
        if file_sync and snap_for_audio is not None:
            self._apply_audio_reactive_step(snap_for_audio, from_precalc_file=True)
        elif self._audio_enable.get() and not file_sync:
            self._apply_audio_reactive_step()
        if sid != self._motion_seq or self._motion_fn is None:
            self._motion_after_id = None
            return
        self._motion_after_id = self.after(36, lambda s=sid: self._run_motion_tick(s))

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
        self._btn_play_sync.config(state=tk.NORMAL)

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
        self._btn_play_sync.config(state=tk.NORMAL)

    def _stop_file_sync_playback(self) -> None:
        if sd is not None:
            try:
                sd.stop()
            except Exception:
                pass
        self._file_playback_active = False
        bs = getattr(self, "_btn_play_sync", None)
        if bs is not None:
            try:
                bs.config(text="同期再生")
            except tk.TclError:
                pass

    def _toggle_file_playback(self) -> None:
        if self._file_playback_active:
            self._stop_file_sync_playback()
            return
        prof = self._track_profile
        if prof is None or ensure_audio_buffer is None:
            messagebox.showwarning("再生不可", "先に音楽ファイルを解析するか JSON を開いてください。", parent=self)
            return
        if not ensure_audio_buffer(prof):
            messagebox.showerror(
                "音声を読めません",
                "波形データがありません。元の音声ファイルのパスが存在するか確認してください。",
                parent=self,
            )
            return
        if sd is None:
            messagebox.showerror(
                "sounddevice 未インストール",
                "pip install sounddevice で同期再生が使えます。",
                parent=self,
            )
            return
        audio = getattr(prof, "audio_mono", None)
        sr = int(getattr(prof, "sr", 44100))
        if audio is None or getattr(audio, "size", 0) < 1:
            messagebox.showerror("再生不可", "波形バッファが空です。", parent=self)
            return

        self._cancel_audio_after_callbacks()
        if self._audio_analyzer is not None:
            self._stop_audio_analyzer()

        self._precalc_event_idx = 0
        need_new_motion = self._motion_fn is None
        if need_new_motion and not DOT_POINT_MOTIONS:
            messagebox.showerror(
                "点モーションがありません",
                "点モーション定義が空のため、解析に同期したレーザー駆動ができません。",
                parent=self,
            )
            return

        self._file_t0 = time.monotonic()
        self._file_playback_active = True

        if need_new_motion:
            self._start_dot_motion()
        if self._motion_fn is None:
            self._file_playback_active = False
            messagebox.showerror(
                "モーション開始失敗",
                "点モーションを開始できませんでした。手動で「モーション開始」を押してから再度お試しください。",
                parent=self,
            )
            return

        self._btn_play_sync.config(text="停止")
        try:
            sd.play(audio, sr, blocking=False)
        except Exception as e:
            self._file_playback_active = False
            self._btn_play_sync.config(text="同期再生")
            messagebox.showerror("再生開始失敗", str(e), parent=self)

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
                "同期再生中はマイク入力と併用できません。\n先に「停止」でファイル再生を止めてください。",
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
                    idx = pick_motion_index(snap, rng=self._audio_rng)
                    try:
                        cur = int(self._motion_combo.current())
                    except (tk.TclError, TypeError, ValueError):
                        cur = -1
                    if cur != idx:
                        self._apply_motion_index_for_audio(idx)
        # テンポに合わせたゆったり切替（以前より間隔を長く）
        delay_ms = 5200
        if an is not None:
            snap = an.snapshot()
            if snap.valid and snap.bpm > 1:
                # 約 8〜12 拍ごと
                beat_ms = 60_000.0 / max(70.0, min(160.0, float(snap.bpm)))
                delay_ms = int(beat_ms * (8.0 + self._audio_rng.random() * 4.0))
        self._audio_motion_after = self.after(max(3200, min(9000, delay_ms)), self._audio_motion_picker_loop)

    def _apply_motion_index_for_audio(self, idx: int) -> None:
        if not DOT_POINT_MOTIONS or idx < 0 or idx >= len(DOT_POINT_MOTIONS):
            return
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
        if self._motion_apply_dot_base.get():
            self._apply_dot_look_channels()
        self._motion_fn = DOT_POINT_MOTIONS[idx][1]
        if self._audio_rng.random() < 0.42:
            self._motion_phase = 0.0

    def _apply_audio_reactive_step(
        self, snap: object | None = None, *, from_precalc_file: bool = False
    ) -> None:
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

        # LED 色に合わせる（モーションが CH9 を書いても毎フレーム上書き）
        ch9 = ch9_from_snapshot(snap)
        self._channel_vars[8].set(ch9)
        self._club_dot_ch9 = ch9
        self._spin_from_var(8)

        target_sp = motion_speed_from_snapshot(snap, sens=sens)
        try:
            cur_sp = float(self._motion_speed.get())
        except (tk.TclError, TypeError, ValueError):
            cur_sp = 1.0
        # 速度はゆっくり追従（急加速しない）
        blend = 0.08
        if getattr(snap, "beat", False) or float(getattr(snap, "onset_strength", 0.0)) > 0.4:
            blend = 0.18
        new_sp = cur_sp + blend * (target_sp - cur_sp)
        soft_max = min(MOTION_SPEED_MAX, 3.4)
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
            )
        except (KeyError, TypeError, ValueError):
            return None

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
        if DOT_POINT_MOTIONS and 0 <= tile.motion_index < len(DOT_POINT_MOTIONS):
            name = DOT_POINT_MOTIONS[tile.motion_index][0]
            mo = name[:10] + ("…" if len(name) > 10 else "")
        rm = "●" if tile.run_motion else "○"
        hk = ""
        if tile.hotkey:
            disp = tile.hotkey
            hk = f"\n⌨ {disp}"
        return f"{tile.title}\nCH2={tile.channels[1]} CH9={tile.channels[8]} {rm}{mo}{hk}"

    def _on_pattern_tile_touch_tap(self, tile: PatternTileState, fr: tk.Frame, event: tk.Event | None = None) -> None:
        """タッチ向け: 長押し＝OS右クリックを避けるため、タップでキープ／再タップでオフ。"""
        ctrl = event is not None and (event.state & 0x4)
        if ctrl or self._pattern_tile_key_assign_mode.get():
            self._pattern_tile_arm_hotkey_target(tile, fr)
            return
        if self._pattern_tile_hold_tile is tile:
            try:
                fr.configure(highlightbackground="#666", highlightthickness=2)
            except tk.TclError:
                pass
            self._tile_press_frame = None
            self._pattern_tile_hold_tile = None
            self._pattern_tile_kb_hold_tile = None
            self._clear_all()
            return
        if self._tile_press_frame is not None:
            try:
                self._tile_press_frame.configure(highlightbackground="#666", highlightthickness=2)
            except tk.TclError:
                pass
        self._tile_press_frame = fr
        self._pattern_tile_hold_tile = tile
        self._pattern_tile_kb_hold_tile = None
        try:
            fr.configure(highlightbackground="#4af", highlightthickness=3)
        except tk.TclError:
            pass
        self._apply_pattern_tile(tile)

    def _on_pattern_tile_press(
        self, tile: PatternTileState, fr: tk.Frame, event: tk.Event | None = None
    ) -> None:
        ctrl = event is not None and (event.state & 0x4)
        if ctrl or self._pattern_tile_key_assign_mode.get():
            self._pattern_tile_arm_hotkey_target(tile, fr)
            return
        self._tile_press_frame = fr
        self._pattern_tile_hold_tile = tile
        self._pattern_tile_kb_hold_tile = None
        try:
            fr.configure(highlightbackground="#4af", highlightthickness=3)
        except tk.TclError:
            pass
        self._apply_pattern_tile(tile)

    def _on_pattern_tile_motion(self, tile: PatternTileState, _event: tk.Event | None = None) -> None:
        if self._pattern_tile_hold_tile is not tile:
            return
        now = time.perf_counter()
        if now - self._last_tile_motion_apply_t < 0.08:
            return
        self._last_tile_motion_apply_t = now
        self._apply_pattern_tile(tile)

    def _on_pattern_tile_release(self) -> None:
        if self._pattern_tile_key_assign_mode.get() or self._pattern_tile_key_target is not None:
            return
        if self._tile_press_frame is not None:
            try:
                self._tile_press_frame.configure(highlightbackground="#666", highlightthickness=2)
            except tk.TclError:
                pass
            self._tile_press_frame = None
        if self._pattern_tile_hold_tile is not None:
            self._pattern_tile_hold_tile = None
            self._pattern_tile_kb_hold_tile = None
            self._clear_all()

    def _apply_pattern_tile(self, tile: PatternTileState) -> None:
        self._motion_apply_dot_base.set(tile.apply_dot_base)
        for i, v in enumerate(tile.channels):
            self._channel_vars[i].set(max(0, min(255, int(v))))
        self._club_dot_ch9 = max(0, min(255, int(tile.club_dot_ch9)))
        for i in range(10):
            self._spin_from_var(i)
        cb = self._motion_combo
        if cb is not None and DOT_POINT_MOTIONS:
            mi = max(0, min(len(DOT_POINT_MOTIONS) - 1, int(tile.motion_index)))
            try:
                cb.current(mi)
            except tk.TclError:
                pass
        sp = max(MOTION_SPEED_MIN, min(MOTION_SPEED_MAX, float(tile.motion_speed)))
        try:
            self._motion_speed.set(sp)
            self._motion_speed_scale.set(sp)
        except (tk.TclError, AttributeError):
            pass
        if tile.run_motion and DOT_POINT_MOTIONS:
            self._start_dot_motion()
        else:
            self._stop_dot_motion()

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
        )
        self._pattern_tiles.append(tile)
        self._save_pattern_tiles_to_disk()
        self._rebuild_pattern_tiles_grid()

    def _delete_pattern_tile_at(self, index: int) -> None:
        if 0 <= index < len(self._pattern_tiles):
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

        if self._pattern_tile_hold_tile is not None:
            self._pattern_tile_hold_tile = None
            self._tile_press_frame = None
            self._pattern_tile_kb_hold_tile = None
            self._clear_all()

        self._pattern_tile_key_target = None
        self._pattern_tile_key_target_frame = None

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
            return

        inner.update_idletasks()
        W = max(400, inner.winfo_width())
        H = max(280, inner.winfo_height())
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

        for i, tile in enumerate(tiles):
            r, c = divmod(i, cols)
            fr = tk.Frame(
                inner,
                width=cell,
                height=cell,
                bg="#333",
                highlightthickness=2,
                highlightbackground="#666",
                cursor="hand2",
            )
            fr.grid(row=r, column=c, padx=pad // 2, pady=pad // 2, sticky="")
            fr.grid_propagate(False)
            bind_tile_events(fr, tile)
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

    def _schedule_tiles_grid_resize(self, event: tk.Event | None = None) -> None:
        if event is not None and event.widget is not self._tiles_inner:
            return
        if self._tiles_resize_job is not None:
            try:
                self.after_cancel(self._tiles_resize_job)
            except tk.TclError:
                pass
        self._tiles_resize_job = self.after(90, self._tiles_resize_rebuild)

    def _tiles_resize_rebuild(self) -> None:
        self._tiles_resize_job = None
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
        self._tiles_toolbar_buttons.extend([b_full, b_cap, b_sav, b_imp, b_close])
        self._sync_tiles_toolbar_button_styles()

        host = tk.Frame(win, bg="#252525")
        host.pack(fill=tk.BOTH, expand=True, padx=6, pady=(0, 8))
        self._tiles_inner = host

        host.bind("<Configure>", self._schedule_tiles_grid_resize)
        self._sync_tiles_win_release_binding()
        win.bind("<Escape>", self._tiles_escape)

        self._rebuild_pattern_tiles_grid()
        self.after(80, self._rebuild_pattern_tiles_grid)

        def _on_close() -> None:
            self._pattern_tile_key_target = None
            self._pattern_tile_key_target_frame = None
            self._on_pattern_tile_release()
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
        self._on_pattern_tile_release()
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

    def _refresh_ports(self, init: bool = False) -> None:
        ports = list_com_ports()
        values = [p[0] for p in ports]
        self._port_combo["values"] = values if values else [""]
        if values:
            if init or self._port_combo.get() not in values:
                self._port_combo.current(0)
        else:
            self._port_combo.set("")

    def _clear_all(self) -> None:
        self._stop_dot_motion()
        for i, var in enumerate(self._channel_vars):
            var.set(0)
            getattr(self, f"_scale_{i}").set(0)
        self._sync_ch2_combo_from_value(0)
        self._club_dot_ch9 = 0
        self._on_values_changed()

    def _start_sending(self) -> None:
        port = (self._port_combo.get() or "").strip()
        if not port:
            messagebox.showwarning("ポート未選択", "COM ポートを選択してください。", parent=self)
            return
        base = int(self._base_addr.get())
        if base < 1 or base > DMX_CHANNELS - 10 + 1:
            messagebox.showerror("無効なアドレス", "開始アドレスは 1〜503 で指定してください。", parent=self)
            return

        self._stop_sending()
        try:
            ser = OpenDMXSender.try_open(port)
        except serial.SerialException as e:
            messagebox.showerror(
                "ポートを開けません",
                f"{port} を開けませんでした。\n\n{e}\n\n"
                "他のアプリが COM を掴んでいないか確認してください。\n"
                "Enttec USB DMX PRO は Open DMX ではないため未対応です。",
                parent=self,
            )
            return

        self._sender = OpenDMXSender(ser)
        self._sender.set_universe_snapshot(self._universe_bytes())
        self._sender.start()

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
        app.after(300, app._start_sending)
    app.mainloop()


if __name__ == "__main__":
    main()
