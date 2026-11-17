"""ESP32 / PixelCtl Art-Net LED pattern presets (generated on PC)."""

from __future__ import annotations

import colorsys
import math
import random
from typing import Callable

# id, label, description
ESP32_PATTERN_PRESETS: list[dict[str, str]] = [
    # --- クラブ定番 ---
    {"id": "club_strobe_white", "label": "白ストロボ", "desc": "ブラインダー風の白点滅"},
    {"id": "club_strobe_color", "label": "カラーストロボ", "desc": "メイン色の高速点滅"},
    {"id": "club_blinder", "label": "ブラインダー", "desc": "白フラッシュ→減衰"},
    {"id": "club_bass_pulse", "label": "バスパルス", "desc": "キックっぽい全体パルス"},
    {"id": "club_buildup", "label": "ビルドアップ", "desc": "暗→徐々に明るく→ヒット"},
    {"id": "club_drop", "label": "ドロップ", "desc": "黒→白ヒット→色残光"},
    {"id": "club_uv_wash", "label": "UVウォッシュ", "desc": "紫〜青紫の全体洗い"},
    {"id": "club_rgb_wash", "label": "RGBウォッシュ", "desc": "赤緑青の切り替え洗い"},
    {"id": "club_halves", "label": "ハーフ切替", "desc": "左右で色が交互"},
    {"id": "club_quarters", "label": "4分割フラッシュ", "desc": "4セクション巡回点灯"},
    {"id": "club_tunnel", "label": "トンネル", "desc": "中央へ収束するリング"},
    {"id": "club_expand", "label": "エクスプロード", "desc": "中央から外側へ爆発"},
    {"id": "club_snake", "label": "スネーク", "desc": "複数ヘッドが這う"},
    {"id": "club_rain", "label": "レイン", "desc": "上から落ちる光点"},
    {"id": "club_heartbeat", "label": "ハートビート", "desc": "ドン・ドンの鼓動"},
    {"id": "club_laser_dots", "label": "レーザードット", "desc": "細点の高速ラン"},
    {"id": "club_sweep", "label": "スイープ", "desc": "太いビームが往復"},
    {"id": "club_zone_chase", "label": "ゾーンチェイス", "desc": "ブロックが階段状に走る"},
    {"id": "club_blackout_hit", "label": "ブラックアウトヒット", "desc": "ほぼ消灯→瞬間ヒット"},
    {"id": "club_warm_cool", "label": "ウォーム／クール", "desc": "暖色と寒色の波"},
    {"id": "club_magenta_teal", "label": "マゼンタ／ティール", "desc": "クラブ定番2色"},
    {"id": "club_sparkle_dark", "label": "ダークスパークル", "desc": "暗い床にキラ"},
    {"id": "club_roller", "label": "ローラー", "desc": "色帯がロール"},
    {"id": "club_alert", "label": "アラート", "desc": "赤点滅警告風"},
    # --- 汎用 ---
    {"id": "rainbow", "label": "虹（固定）", "desc": "全長レインボー"},
    {"id": "rainbow_cycle", "label": "虹サイクル", "desc": "虹が流れる"},
    {"id": "chase", "label": "チェイス", "desc": "色が走る"},
    {"id": "theater", "label": "シアター", "desc": "点滅チェイス"},
    {"id": "comet", "label": "コメット", "desc": "尾を引く光"},
    {"id": "scanner", "label": "スキャナー", "desc": "往復ビーム"},
    {"id": "breathe", "label": "ブリーズ", "desc": "全体が呼吸"},
    {"id": "strobe", "label": "ストロボ", "desc": "点滅"},
    {"id": "sparkle", "label": "スパークル", "desc": "きらめき"},
    {"id": "fire", "label": "炎", "desc": "炎っぽい揺らぎ"},
    {"id": "wave", "label": "ウェーブ", "desc": "正弦波の波"},
    {"id": "stripes", "label": "ストライプ", "desc": "2色交互"},
    {"id": "gradient", "label": "グラデ", "desc": "端から端へ色変化"},
    {"id": "wipe", "label": "ワイプ", "desc": "塗りつぶし進行"},
    {"id": "police", "label": "ポリス", "desc": "赤青交互"},
    {"id": "aurora", "label": "オーロラ", "desc": "緑紫の揺らぎ"},
    {"id": "ocean", "label": "オーシャン", "desc": "青緑の波"},
    {"id": "candy", "label": "キャンディ", "desc": "ポップな多色"},
]


def _clamp_byte(v: float) -> int:
    return max(0, min(255, int(round(v))))


def _hsv(h: float, s: float, v: float) -> tuple[int, int, int]:
    r, g, b = colorsys.hsv_to_rgb(h % 1.0, max(0.0, min(1.0, s)), max(0.0, min(1.0, v)))
    return _clamp_byte(r * 255), _clamp_byte(g * 255), _clamp_byte(b * 255)


def _mix(
    a: tuple[int, int, int], b: tuple[int, int, int], t: float
) -> tuple[int, int, int]:
    t = max(0.0, min(1.0, t))
    return (
        _clamp_byte(a[0] + (b[0] - a[0]) * t),
        _clamp_byte(a[1] + (b[1] - a[1]) * t),
        _clamp_byte(a[2] + (b[2] - a[2]) * t),
    )


def _scale(rgb: tuple[int, int, int], k: float) -> tuple[int, int, int]:
    return _clamp_byte(rgb[0] * k), _clamp_byte(rgb[1] * k), _clamp_byte(rgb[2] * k)


def render_pattern(
    pattern_id: str,
    n: int,
    t: float,
    *,
    color: tuple[int, int, int] = (255, 80, 40),
    color2: tuple[int, int, int] = (40, 80, 255),
    speed: float = 1.0,
    brightness: float = 1.0,
) -> list[tuple[int, int, int]]:
    """Return RGB tuples for n pixels at time t (seconds)."""
    n = max(1, int(n))
    bri = max(0.0, min(1.0, float(brightness)))
    sp = max(0.05, float(speed))
    tt = t * sp
    pid = (pattern_id or "rainbow").strip().lower()
    fn = _PATTERNS.get(pid, _pat_rainbow)
    out = fn(n, tt, color, color2)
    if bri >= 0.999:
        return out
    return [_scale(p, bri) for p in out]


def _pat_rainbow(
    n: int, t: float, c: tuple[int, int, int], c2: tuple[int, int, int]
) -> list[tuple[int, int, int]]:
    return [_hsv(i / max(1, n), 1.0, 1.0) for i in range(n)]


def _pat_rainbow_cycle(
    n: int, t: float, c: tuple[int, int, int], c2: tuple[int, int, int]
) -> list[tuple[int, int, int]]:
    off = (t * 0.12) % 1.0
    return [_hsv(i / max(1, n) + off, 1.0, 1.0) for i in range(n)]


def _pat_chase(
    n: int, t: float, c: tuple[int, int, int], c2: tuple[int, int, int]
) -> list[tuple[int, int, int]]:
    head = int(t * 18) % n
    width = max(3, n // 20)
    out: list[tuple[int, int, int]] = []
    for i in range(n):
        d = (head - i) % n
        if d < width:
            out.append(_scale(c, 1.0 - d / width))
        else:
            out.append((0, 0, 0))
    return out


def _pat_theater(
    n: int, t: float, c: tuple[int, int, int], c2: tuple[int, int, int]
) -> list[tuple[int, int, int]]:
    step = int(t * 8) % 3
    return [c if (i % 3) == step else (0, 0, 0) for i in range(n)]


def _pat_comet(
    n: int, t: float, c: tuple[int, int, int], c2: tuple[int, int, int]
) -> list[tuple[int, int, int]]:
    head = int(t * 22) % n
    tail = max(8, n // 12)
    out: list[tuple[int, int, int]] = []
    for i in range(n):
        d = (head - i) % n
        if d < tail:
            k = (1.0 - d / tail) ** 2
            out.append(_scale(c, k))
        else:
            out.append((0, 0, 0))
    return out


def _pat_scanner(
    n: int, t: float, c: tuple[int, int, int], c2: tuple[int, int, int]
) -> list[tuple[int, int, int]]:
    period = max(2, n * 2 - 2)
    pos = int(t * 28) % period
    if pos >= n:
        pos = period - pos
    out: list[tuple[int, int, int]] = []
    for i in range(n):
        d = abs(i - pos)
        if d == 0:
            out.append(c)
        elif d == 1:
            out.append(_scale(c, 0.35))
        elif d == 2:
            out.append(_scale(c, 0.12))
        else:
            out.append((0, 0, 0))
    return out


def _pat_breathe(
    n: int, t: float, c: tuple[int, int, int], c2: tuple[int, int, int]
) -> list[tuple[int, int, int]]:
    k = 0.5 + 0.5 * math.sin(t * 2.2)
    pix = _scale(c, 0.08 + 0.92 * k)
    return [pix] * n


def _pat_strobe(
    n: int, t: float, c: tuple[int, int, int], c2: tuple[int, int, int]
) -> list[tuple[int, int, int]]:
    on = (math.sin(t * 18) > 0.35)
    return [c if on else (0, 0, 0)] * n


def _pat_sparkle(
    n: int, t: float, c: tuple[int, int, int], c2: tuple[int, int, int]
) -> list[tuple[int, int, int]]:
    # deterministic-ish sparkle from time buckets
    seed = int(t * 14)
    rng = random.Random(seed)
    base = _scale(c, 0.08)
    out = [base] * n
    count = max(2, n // 25)
    for _ in range(count):
        i = rng.randrange(n)
        out[i] = _mix(c, (255, 255, 255), rng.random() * 0.6)
    return out


def _pat_fire(
    n: int, t: float, c: tuple[int, int, int], c2: tuple[int, int, int]
) -> list[tuple[int, int, int]]:
    out: list[tuple[int, int, int]] = []
    for i in range(n):
        # heat rises toward ends with noise
        heat = 0.55 + 0.45 * math.sin(i * 0.21 + t * 6.0) * math.sin(i * 0.07 - t * 3.5)
        heat = max(0.0, min(1.0, heat))
        if heat < 0.35:
            out.append(_scale((80, 10, 0), heat / 0.35))
        elif heat < 0.7:
            out.append(_mix((180, 30, 0), (255, 120, 10), (heat - 0.35) / 0.35))
        else:
            out.append(_mix((255, 140, 20), (255, 240, 160), (heat - 0.7) / 0.3))
    return out


def _pat_wave(
    n: int, t: float, c: tuple[int, int, int], c2: tuple[int, int, int]
) -> list[tuple[int, int, int]]:
    out: list[tuple[int, int, int]] = []
    for i in range(n):
        k = 0.5 + 0.5 * math.sin(i / max(1, n) * math.pi * 4 - t * 4.5)
        out.append(_mix(c2, c, k))
    return out


def _pat_stripes(
    n: int, t: float, c: tuple[int, int, int], c2: tuple[int, int, int]
) -> list[tuple[int, int, int]]:
    w = max(2, n // 24)
    off = int(t * 10) % (w * 2)
    out: list[tuple[int, int, int]] = []
    for i in range(n):
        out.append(c if ((i + off) // w) % 2 == 0 else c2)
    return out


def _pat_gradient(
    n: int, t: float, c: tuple[int, int, int], c2: tuple[int, int, int]
) -> list[tuple[int, int, int]]:
    # slowly morph endpoints
    pulse = 0.5 + 0.5 * math.sin(t * 1.2)
    a = _mix(c, c2, pulse * 0.25)
    b = _mix(c2, c, pulse * 0.25)
    return [_mix(a, b, i / max(1, n - 1)) for i in range(n)]


def _pat_wipe(
    n: int, t: float, c: tuple[int, int, int], c2: tuple[int, int, int]
) -> list[tuple[int, int, int]]:
    cycle = n * 2
    pos = int(t * 40) % cycle
    filling = pos < n
    edge = pos if filling else (cycle - pos)
    use = c if filling else c2
    bg = c2 if filling else c
    return [use if i < edge else bg for i in range(n)]


def _pat_police(
    n: int, t: float, c: tuple[int, int, int], c2: tuple[int, int, int]
) -> list[tuple[int, int, int]]:
    phase = int(t * 6) % 2
    left = (255, 0, 0) if phase == 0 else (0, 0, 0)
    right = (0, 0, 255) if phase == 1 else (0, 0, 0)
    mid = n // 2
    return [left if i < mid else right for i in range(n)]


def _pat_aurora(
    n: int, t: float, c: tuple[int, int, int], c2: tuple[int, int, int]
) -> list[tuple[int, int, int]]:
    out: list[tuple[int, int, int]] = []
    for i in range(n):
        x = i / max(1, n)
        h = 0.35 + 0.25 * math.sin(x * 6 + t * 0.7) + 0.08 * math.sin(x * 18 - t * 1.4)
        v = 0.35 + 0.65 * (0.5 + 0.5 * math.sin(x * 3 - t * 1.1))
        out.append(_hsv(h, 0.85, v))
    return out


def _pat_ocean(
    n: int, t: float, c: tuple[int, int, int], c2: tuple[int, int, int]
) -> list[tuple[int, int, int]]:
    out: list[tuple[int, int, int]] = []
    deep = (0, 40, 90)
    foam = (80, 220, 255)
    for i in range(n):
        x = i / max(1, n)
        k = 0.5 + 0.5 * math.sin(x * math.pi * 5 - t * 2.8)
        k2 = 0.5 + 0.5 * math.sin(x * math.pi * 2 + t * 1.3)
        out.append(_mix(deep, foam, k * 0.7 + k2 * 0.3))
    return out


def _pat_candy(
    n: int, t: float, c: tuple[int, int, int], c2: tuple[int, int, int]
) -> list[tuple[int, int, int]]:
    colors = [
        (255, 60, 120),
        (255, 180, 40),
        (80, 220, 120),
        (80, 140, 255),
        (200, 80, 255),
    ]
    off = int(t * 12)
    w = max(3, n // 30)
    return [colors[((i + off) // w) % len(colors)] for i in range(n)]


# ---- club classics ----

def _pat_club_strobe_white(
    n: int, t: float, c: tuple[int, int, int], c2: tuple[int, int, int]
) -> list[tuple[int, int, int]]:
    # ~10–12 Hz blinder strobe
    on = (math.sin(t * 70) > 0.2)
    return [(255, 255, 255) if on else (0, 0, 0)] * n


def _pat_club_strobe_color(
    n: int, t: float, c: tuple[int, int, int], c2: tuple[int, int, int]
) -> list[tuple[int, int, int]]:
    on = (math.sin(t * 55) > 0.15)
    return [c if on else (0, 0, 0)] * n


def _pat_club_blinder(
    n: int, t: float, c: tuple[int, int, int], c2: tuple[int, int, int]
) -> list[tuple[int, int, int]]:
    # flash every ~0.85s then exponential decay
    period = 0.85
    phase = (t % period) / period
    if phase < 0.06:
        k = 1.0
    else:
        k = math.exp(-(phase - 0.06) * 8.0) * 0.85
    return [_scale((255, 255, 255), k)] * n


def _pat_club_bass_pulse(
    n: int, t: float, c: tuple[int, int, int], c2: tuple[int, int, int]
) -> list[tuple[int, int, int]]:
    # ~128 BPM feel: hard attack, short decay
    bpm = 128.0
    beat = (t * bpm / 60.0) % 1.0
    k = math.exp(-beat * 7.0)
    base = _scale(c, 0.06)
    hit = _mix(c, (255, 255, 255), 0.25)
    return [_mix(base, hit, k)] * n


def _pat_club_buildup(
    n: int, t: float, c: tuple[int, int, int], c2: tuple[int, int, int]
) -> list[tuple[int, int, int]]:
    # 4s build then white hit
    cycle = 4.0
    phase = (t % cycle) / cycle
    if phase > 0.92:
        return [(255, 255, 255)] * n
    k = phase / 0.92
    # accelerate sparkle density
    seed = int(t * (4 + 40 * k))
    rng = random.Random(seed)
    out = [_scale(c, 0.05 + 0.55 * k)] * n
    sparks = max(1, int(n * 0.02 * k))
    for _ in range(sparks):
        out[rng.randrange(n)] = _mix(c, (255, 255, 255), 0.5 + 0.5 * k)
    return out


def _pat_club_drop(
    n: int, t: float, c: tuple[int, int, int], c2: tuple[int, int, int]
) -> list[tuple[int, int, int]]:
    cycle = 2.4
    phase = (t % cycle) / cycle
    if phase < 0.35:
        return [(0, 0, 0)] * n
    if phase < 0.42:
        return [(255, 255, 255)] * n
    k = math.exp(-(phase - 0.42) * 4.5)
    return [_mix(c, (255, 255, 255), k * 0.4)] * n


def _pat_club_uv_wash(
    n: int, t: float, c: tuple[int, int, int], c2: tuple[int, int, int]
) -> list[tuple[int, int, int]]:
    a = (90, 0, 180)
    b = (40, 0, 255)
    k = 0.5 + 0.5 * math.sin(t * 1.6)
    return [_mix(a, b, k)] * n


def _pat_club_rgb_wash(
    n: int, t: float, c: tuple[int, int, int], c2: tuple[int, int, int]
) -> list[tuple[int, int, int]]:
    colors = [(255, 0, 0), (0, 255, 0), (0, 40, 255)]
    idx = int(t * 0.7) % 3
    nxt = (idx + 1) % 3
    blend = (t * 0.7) % 1.0
    # hard-ish crossfade near end
    if blend < 0.75:
        col = colors[idx]
    else:
        col = _mix(colors[idx], colors[nxt], (blend - 0.75) / 0.25)
    return [col] * n


def _pat_club_halves(
    n: int, t: float, c: tuple[int, int, int], c2: tuple[int, int, int]
) -> list[tuple[int, int, int]]:
    flip = int(t * 4) % 2
    mid = n // 2
    left = c if flip == 0 else c2
    right = c2 if flip == 0 else c
    return [left if i < mid else right for i in range(n)]


def _pat_club_quarters(
    n: int, t: float, c: tuple[int, int, int], c2: tuple[int, int, int]
) -> list[tuple[int, int, int]]:
    q = max(1, n // 4)
    active = int(t * 6) % 4
    out: list[tuple[int, int, int]] = []
    for i in range(n):
        sec = min(3, i // q)
        out.append(c if sec == active else (0, 0, 0))
    return out


def _pat_club_tunnel(
    n: int, t: float, c: tuple[int, int, int], c2: tuple[int, int, int]
) -> list[tuple[int, int, int]]:
    mid = (n - 1) / 2.0
    # radius shrinks toward center then resets
    phase = (t * 0.55) % 1.0
    radius = (1.0 - phase) * mid
    width = max(2.0, n * 0.035)
    out: list[tuple[int, int, int]] = []
    for i in range(n):
        d = abs(abs(i - mid) - radius)
        if d < width:
            k = 1.0 - d / width
            out.append(_mix(c2, c, k))
        else:
            out.append(_scale(c2, 0.04))
    return out


def _pat_club_expand(
    n: int, t: float, c: tuple[int, int, int], c2: tuple[int, int, int]
) -> list[tuple[int, int, int]]:
    mid = (n - 1) / 2.0
    phase = (t * 0.7) % 1.0
    radius = phase * (mid + 8)
    width = max(3.0, n * 0.04)
    out: list[tuple[int, int, int]] = []
    for i in range(n):
        d = abs(abs(i - mid) - radius)
        if d < width:
            k = (1.0 - d / width) ** 1.5
            out.append(_scale(_mix(c, (255, 255, 255), 0.35), k))
        else:
            out.append((0, 0, 0))
    return out


def _pat_club_snake(
    n: int, t: float, c: tuple[int, int, int], c2: tuple[int, int, int]
) -> list[tuple[int, int, int]]:
    length = max(6, n // 16)
    heads = 3
    out = [(0, 0, 0)] * n
    buf = list(out)
    for s in range(heads):
        head = int(t * (16 + s * 3) + s * (n / heads)) % n
        col = c if s % 2 == 0 else c2
        for d in range(length):
            i = (head - d) % n
            k = (1.0 - d / length) ** 2
            prev = buf[i]
            pix = _scale(col, k)
            buf[i] = (
                max(prev[0], pix[0]),
                max(prev[1], pix[1]),
                max(prev[2], pix[2]),
            )
    return buf


def _pat_club_rain(
    n: int, t: float, c: tuple[int, int, int], c2: tuple[int, int, int]
) -> list[tuple[int, int, int]]:
    # treat strip as vertical rain columns conceptually: moving bright dots
    drops = max(8, n // 40)
    out = [_scale(c2, 0.03)] * n
    buf = list(out)
    for d in range(drops):
        # each drop has its own lane speed
        pos = int((t * (18 + (d % 5) * 4) + d * 97)) % n
        buf[pos] = _mix(c, (255, 255, 255), 0.4)
        trail = (pos - 1) % n
        buf[trail] = _scale(c, 0.35)
    return buf


def _pat_club_heartbeat(
    n: int, t: float, c: tuple[int, int, int], c2: tuple[int, int, int]
) -> list[tuple[int, int, int]]:
    # lub-dub: two pulses then rest
    period = 1.05
    p = (t % period) / period
    if p < 0.12:
        k = math.sin(p / 0.12 * math.pi)
    elif 0.18 < p < 0.30:
        k = 0.75 * math.sin((p - 0.18) / 0.12 * math.pi)
    else:
        k = 0.0
    return [_mix(_scale(c, 0.05), _mix(c, (255, 80, 80), 0.3), k)] * n


def _pat_club_laser_dots(
    n: int, t: float, c: tuple[int, int, int], c2: tuple[int, int, int]
) -> list[tuple[int, int, int]]:
    spacing = max(8, n // 36)
    off = int(t * 40) % spacing
    out: list[tuple[int, int, int]] = []
    for i in range(n):
        out.append(c if (i + off) % spacing == 0 else (0, 0, 0))
    return out


def _pat_club_sweep(
    n: int, t: float, c: tuple[int, int, int], c2: tuple[int, int, int]
) -> list[tuple[int, int, int]]:
    period = max(2, n * 2 - 2)
    pos = int(t * 36) % period
    if pos >= n:
        pos = period - pos
    width = max(8, n // 18)
    out: list[tuple[int, int, int]] = []
    for i in range(n):
        d = abs(i - pos)
        if d < width:
            k = (1.0 - d / width) ** 1.2
            out.append(_mix(c2, c, k))
        else:
            out.append(_scale(c2, 0.03))
    return out


def _pat_club_zone_chase(
    n: int, t: float, c: tuple[int, int, int], c2: tuple[int, int, int]
) -> list[tuple[int, int, int]]:
    zones = 8
    zlen = max(1, n // zones)
    active = int(t * 5) % zones
    out: list[tuple[int, int, int]] = []
    for i in range(n):
        z = min(zones - 1, i // zlen)
        if z == active:
            out.append(c)
        elif z == (active - 1) % zones:
            out.append(_scale(c, 0.35))
        else:
            out.append((0, 0, 0))
    return out


def _pat_club_blackout_hit(
    n: int, t: float, c: tuple[int, int, int], c2: tuple[int, int, int]
) -> list[tuple[int, int, int]]:
    # mostly dark, occasional full hit synced ~1/1.5s
    period = 1.5
    p = (t % period) / period
    if p < 0.08:
        return [_mix(c, (255, 255, 255), 0.5)] * n
    return [(0, 0, 0)] * n


def _pat_club_warm_cool(
    n: int, t: float, c: tuple[int, int, int], c2: tuple[int, int, int]
) -> list[tuple[int, int, int]]:
    warm = (255, 90, 20)
    cool = (40, 100, 255)
    out: list[tuple[int, int, int]] = []
    for i in range(n):
        x = i / max(1, n - 1)
        k = 0.5 + 0.5 * math.sin(x * math.pi * 2 - t * 2.0)
        out.append(_mix(warm, cool, k))
    return out


def _pat_club_magenta_teal(
    n: int, t: float, c: tuple[int, int, int], c2: tuple[int, int, int]
) -> list[tuple[int, int, int]]:
    mag = (255, 0, 140)
    teal = (0, 220, 180)
    flip = 0.5 + 0.5 * math.sin(t * 2.4)
    # moving boundary
    cut = int((0.5 + 0.45 * math.sin(t * 1.3)) * n)
    out: list[tuple[int, int, int]] = []
    for i in range(n):
        if abs(i - cut) < max(2, n // 80):
            out.append(_mix(mag, teal, flip))
        else:
            out.append(mag if i < cut else teal)
    return out


def _pat_club_sparkle_dark(
    n: int, t: float, c: tuple[int, int, int], c2: tuple[int, int, int]
) -> list[tuple[int, int, int]]:
    seed = int(t * 16)
    rng = random.Random(seed)
    out = [(0, 0, 0)] * n
    buf = list(out)
    for _ in range(max(3, n // 50)):
        i = rng.randrange(n)
        buf[i] = _mix(c, (255, 255, 255), rng.random())
    return buf


def _pat_club_roller(
    n: int, t: float, c: tuple[int, int, int], c2: tuple[int, int, int]
) -> list[tuple[int, int, int]]:
    band = max(10, n // 10)
    off = int(t * 28) % n
    out: list[tuple[int, int, int]] = []
    for i in range(n):
        d = (i - off) % n
        if d < band:
            k = 0.5 + 0.5 * math.sin(d / band * math.pi)
            out.append(_mix(c2, c, k))
        else:
            out.append(_scale(c2, 0.05))
    return out


def _pat_club_alert(
    n: int, t: float, c: tuple[int, int, int], c2: tuple[int, int, int]
) -> list[tuple[int, int, int]]:
    on = (math.sin(t * 28) > 0)
    return [(255, 0, 0) if on else (0, 0, 0)] * n


_PATTERNS: dict[str, Callable[..., list[tuple[int, int, int]]]] = {
    "rainbow": _pat_rainbow,
    "rainbow_cycle": _pat_rainbow_cycle,
    "chase": _pat_chase,
    "theater": _pat_theater,
    "comet": _pat_comet,
    "scanner": _pat_scanner,
    "breathe": _pat_breathe,
    "strobe": _pat_strobe,
    "sparkle": _pat_sparkle,
    "fire": _pat_fire,
    "wave": _pat_wave,
    "stripes": _pat_stripes,
    "gradient": _pat_gradient,
    "wipe": _pat_wipe,
    "police": _pat_police,
    "aurora": _pat_aurora,
    "ocean": _pat_ocean,
    "candy": _pat_candy,
    "club_strobe_white": _pat_club_strobe_white,
    "club_strobe_color": _pat_club_strobe_color,
    "club_blinder": _pat_club_blinder,
    "club_bass_pulse": _pat_club_bass_pulse,
    "club_buildup": _pat_club_buildup,
    "club_drop": _pat_club_drop,
    "club_uv_wash": _pat_club_uv_wash,
    "club_rgb_wash": _pat_club_rgb_wash,
    "club_halves": _pat_club_halves,
    "club_quarters": _pat_club_quarters,
    "club_tunnel": _pat_club_tunnel,
    "club_expand": _pat_club_expand,
    "club_snake": _pat_club_snake,
    "club_rain": _pat_club_rain,
    "club_heartbeat": _pat_club_heartbeat,
    "club_laser_dots": _pat_club_laser_dots,
    "club_sweep": _pat_club_sweep,
    "club_zone_chase": _pat_club_zone_chase,
    "club_blackout_hit": _pat_club_blackout_hit,
    "club_warm_cool": _pat_club_warm_cool,
    "club_magenta_teal": _pat_club_magenta_teal,
    "club_sparkle_dark": _pat_club_sparkle_dark,
    "club_roller": _pat_club_roller,
    "club_alert": _pat_club_alert,
}
