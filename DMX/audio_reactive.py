#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
クラブ向けリアクティブ解析ブリッジ。

ライブマイク解析は BluetoothLED の AudioAnalyzer / MoodRhythmMapper を使用し、
DMX 用 AudioSnapshot（モーション選定・CH9・速度）へ変換する。
ファイル事前解析（music_precalc）向けの StyleGuess / AudioSnapshot 定義もここに置く。
"""

from __future__ import annotations

import math
import threading
import time
from collections import deque
from dataclasses import dataclass
from enum import Enum, auto
from collections.abc import Callable
from pathlib import Path

_BLE_AUDIO_MOD = None


def _ble_audio():
    """BluetoothLED/app/audio_reactive.py を名前衝突を避けてロード。"""
    global _BLE_AUDIO_MOD
    if _BLE_AUDIO_MOD is not None:
        return _BLE_AUDIO_MOD
    import importlib.util
    import sys

    ble_path = Path(__file__).resolve().parents[1] / "BluetoothLED" / "app" / "audio_reactive.py"
    spec = importlib.util.spec_from_file_location("ble_led_audio_reactive", ble_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"BluetoothLED の音楽解析を読めません: {ble_path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    _BLE_AUDIO_MOD = mod
    return mod



import numpy as np

try:
    import sounddevice as sd
except ImportError as e:  # pragma: no cover
    raise SystemExit("sounddevice / NumPy が必要です: pip install -r requirements.txt") from e


class StyleGuess(Enum):
    """音に合わせたモーション大分類（推定）。"""

    TECHNO = auto()
    HIPHOP = auto()
    CLUB = auto()


@dataclass(frozen=True)
class AudioSnapshot:
    """メインスレッドが読むスナップショット（スレッドセーフにコピー）。"""

    rms: float
    rms_smooth: float
    bass: float
    mid: float
    high: float
    centroid_hz: float
    centroid_norm: float  # 0〜1 程度（1k–8k に正規化した簡易明るさ）
    flux: float
    flux_smooth: float
    bpm: float
    onset_strength: float
    beat_phasor: float  # 0〜1 推定拍の位相（表示・変調用）
    steadiness: float  # 4つ打ち規則性 0〜1
    style: StyleGuess
    techno_score: float
    hiphop_score: float
    club_score: float
    device_sr: int
    block_ms: float
    valid: bool
    # RMS が十分あるとき True（無音〜環境ノイズでは False。オン／オフにヒステリシスあり）
    music_active: bool
    # 突発音検出: 増分カウンタ。前回値と比較して即モーション切替。
    impact_generation: int
    # 直近トランジェント強さ 0〜1（減衰）。速度・CH9 ブースト用
    transient_level: float
    # 「ピュン」系レーザー／ビームSFX検出（高音ドミナントの短い突上がり）。交戦モーション用。
    laser_pew_generation: int
    # 揺れ・ウォブル系: RMS エンベロープの変動（0〜1）と推定変調周波数 [Hz]
    shake_level: float
    envelope_wobble_hz: float
    shake_generation: int
    # エンベロープ上の低速周期成分（LFO / サイドチェイン風のたぎり）推定 [Hz], 強さ 0〜1
    lfo_hz: float
    lfo_depth: float
    # BluetoothLED LightingFrame 連動
    tension: float = 0.0
    release: float = 0.0
    section: str = "verse"
    led_r: int = 255
    led_g: int = 80
    led_b: int = 40
    # 溜め中はレーザー消灯
    laser_hold: bool = False
    beat: bool = False


def estimate_lfo_from_envelope(envelope: np.ndarray, dt_sec: float) -> tuple[float, float]:
    """
    ラウドネス列から ~0.1–14 Hz 帯の主成分を LFO 相当として推定する。
    dt_sec: 各サンプルの時間間隔（秒）
    """
    if envelope.size < 36 or dt_sec < 1e-7:
        return 0.0, 0.0
    x = envelope.astype(np.float64, copy=False)
    x = x - np.mean(x)
    if float(np.std(x)) < 1e-9:
        return 0.0, 0.0
    n = int(x.size)
    w = np.hanning(n)
    xw = x * w
    spec = np.abs(np.fft.rfft(xw))
    freqs = np.fft.rfftfreq(n, d=dt_sec)
    m = (freqs >= 0.1) & (freqs <= 14.0)
    if not np.any(m):
        return 0.0, 0.0
    fb = freqs[m]
    sb = spec[m]
    if sb.size < 2:
        return 0.0, 0.0
    peak_i = int(np.argmax(sb))
    tot = float(np.sum(sb * sb)) + 1e-15
    peak_e = float(sb[peak_i] * sb[peak_i])
    depth = min(1.0, 4.2 * peak_e / tot)
    if depth < 0.07:
        return 0.0, 0.0
    return float(fb[peak_i]), depth


def effective_wobble_hz(s: AudioSnapshot) -> float:
    """高速ゼロクロス由来の wobble Hz と LFO 推定を合体（揺れ速度の参照用）。"""
    w = float(s.envelope_wobble_hz)
    if s.lfo_depth > 0.11 and s.lfo_hz > 0.07:
        w = max(w, float(s.lfo_hz) * (0.66 + 0.40 * float(s.lfo_depth)))
    return w


class ClubAudioAnalyzer:
    """
    BluetoothLED の音楽解析（AudioAnalyzer + MoodRhythmMapper）でスナップショットを生成。
    DMX 側の既存 API（snapshot / gate_sensitivity / list_input_devices）を維持する。
    """

    def __init__(
        self,
        *,
        sample_rate: int | None = None,
        block_ms: float | None = None,
        device: str | int | None = None,
        on_lighting_frame: Callable | None = None,
    ) -> None:
        ble = _ble_audio()
        self.sample_rate = int(sample_rate or ble.SAMPLE_RATE)
        self.block_ms = float(block_ms if block_ms is not None else ble.BLOCK_MS)
        self.blocksize = int(ble.BLOCK_SIZE)
        self.channels = 1
        self._device = device
        self._on_lighting_frame = on_lighting_frame

        self._stream: sd.InputStream | None = None
        self._lock = threading.Lock()
        self._snap = self._silent_snapshot()
        self._lighting_frame = None

        self._analyzer = ble.AudioAnalyzer(self.sample_rate)
        self._mapper = ble.MoodRhythmMapper()
        # アプリの「感度」スライダー（DMX 0.45–1.85 / BLE 相当も可）
        self.gate_sensitivity = 1.0

        self._onset_times: deque[float] = deque(maxlen=48)
        self._intervals: deque[float] = deque(maxlen=12)
        self._bpm_ema = 120.0
        self._last_beat_t = 0.0
        self._beat_period = 0.5

        self._impact_generation = 0
        self._laser_pew_generation = 0
        self._shake_generation = 0
        self._last_impact_fire_t = 0.0
        self._last_pew_fire_t = 0.0
        self._last_shake_emit_t = 0.0
        self._transient_env = 0.0
        self._rms_env_hist: deque[float] = deque(maxlen=36)
        self._rms_lfo_hist: deque[float] = deque(maxlen=100)
        self._music_active = False

    def _silent_snapshot(self) -> AudioSnapshot:
        return AudioSnapshot(
            rms=0.0,
            rms_smooth=0.0,
            bass=0.0,
            mid=0.0,
            high=0.0,
            centroid_hz=0.0,
            centroid_norm=0.35,
            flux=0.0,
            flux_smooth=0.0,
            bpm=120.0,
            onset_strength=0.0,
            beat_phasor=0.0,
            steadiness=0.45,
            style=StyleGuess.CLUB,
            techno_score=0.33,
            hiphop_score=0.33,
            club_score=0.34,
            device_sr=self.sample_rate,
            block_ms=1000.0 * self.blocksize / max(1, self.sample_rate),
            valid=False,
            music_active=False,
            impact_generation=0,
            transient_level=0.0,
            laser_pew_generation=0,
            shake_level=0.0,
            envelope_wobble_hz=0.0,
            shake_generation=0,
            lfo_hz=0.0,
            lfo_depth=0.0,
            tension=0.0,
            release=0.0,
            section="silent",
            led_r=255,
            led_g=80,
            led_b=40,
            laser_hold=False,
            beat=False,
        )

    def set_reaction_mode(self, mode: str) -> None:
        self._mapper.set_mode(mode)

    def set_on_lighting_frame(self, cb: Callable | None) -> None:
        self._on_lighting_frame = cb

    def lighting_frame(self):
        with self._lock:
            return self._lighting_frame

    def start(self) -> None:
        if self._stream is not None:
            return

        def _callback(indata, frames, _time_info, _status):  # noqa: ARG001
            mono = indata[:, 0] if getattr(indata, "ndim", 1) > 1 else indata
            samples = np.copy(mono)
            ble_sens = _dmx_sens_to_ble(float(self.gate_sensitivity))
            features = self._analyzer.analyze(samples, ble_sens)
            frame = self._mapper.map(features)
            self._ingest(features, frame, frames)
            cb = self._on_lighting_frame
            if cb is not None:
                try:
                    cb(frame)
                except Exception:
                    pass

        kwargs: dict = {
            "channels": self.channels,
            "samplerate": self.sample_rate,
            "blocksize": self.blocksize,
            "dtype": "float32",
            "callback": _callback,
        }
        if self._device is not None and self._device != "" and self._device != -1:
            kwargs["device"] = self._device
        self._stream = sd.InputStream(**kwargs)
        self._stream.start()

    def stop(self) -> None:
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None

    @staticmethod
    def list_input_devices() -> list[tuple[int, str]]:
        out: list[tuple[int, str]] = []
        try:
            ble = _ble_audio()
            for d in ble.list_input_devices():
                if d.index is None:
                    out.insert(0, (-1, f"default: {d.label}"))
                else:
                    out.append((int(d.index), f"{d.index}: {d.name}"))
        except Exception:
            try:
                for i, d in enumerate(sd.query_devices()):
                    if int(d.get("max_input_channels", 0) or 0) > 0:
                        out.append((i, f"{i}: {d.get('name', '?')}"))
            except Exception:
                pass
        return out

    def snapshot(self) -> AudioSnapshot:
        with self._lock:
            return self._snap

    def _ingest(self, features: dict, frame, frames: int) -> None:
        now = time.perf_counter()
        level = float(getattr(frame, "level", 0.0))
        music_level = float(features.get("music_level", level))
        speech = float(features.get("speech", 0.0))
        bass = float(features.get("bass", 0.0))
        sub = float(features.get("sub_bass", 0.0))
        mid = float(features.get("mid", features.get("presence", 0.0)))
        high = float(features.get("high", 0.0))
        presence = float(features.get("presence", 0.0))
        flux_n = float(features.get("flux", 0.0))
        brightness = float(features.get("brightness", 0.5))
        centroid_hz = float(features.get("centroid", 800.0))
        raw_level = float(features.get("raw_level", level))

        # DMX 側の速度・ゲート計算に合わせたスケール
        rms_smooth = max(1e-6, float(level) * 0.045)
        rms_disp = max(rms_smooth, float(np.clip(raw_level, 0.0, 4.0)) * 0.012)
        flux_smooth = flux_n * 0.00045

        open_th = 0.035 / max(0.45, min(1.85, float(self.gate_sensitivity)))
        if self._music_active:
            if music_level < open_th * 0.55 or speech > 0.8:
                self._music_active = False
        else:
            if music_level > open_th and speech < 0.7 and getattr(frame, "mood", "") != "silent":
                self._music_active = True

        beat = bool(getattr(frame, "beat", False))
        onset_strength = 0.0
        if beat and self._music_active:
            onset_strength = float(
                np.clip(0.35 + bass * 0.4 + sub * 0.3 + high * 0.2 + flux_n * 0.25, 0.0, 1.0)
            )
            self._onset_times.append(now)
            if len(self._onset_times) >= 2:
                dt = self._onset_times[-1] - self._onset_times[-2]
                if 0.22 < dt < 1.4:
                    self._intervals.append(dt)
            self._last_beat_t = now

        bpm = self._bpm_ema
        if len(self._intervals) >= 4:
            arr = np.array(self._intervals, dtype=np.float64)
            med = float(np.median(arr))
            if med > 1e-3:
                inst = max(70.0, min(178.0, 60.0 / med))
                self._bpm_ema = 0.78 * self._bpm_ema + 0.22 * inst
                bpm = self._bpm_ema
        self._beat_period = 60.0 / max(48.0, bpm)
        ph = ((now - self._last_beat_t) % self._beat_period) / self._beat_period if self._last_beat_t else 0.0

        self._rms_env_hist.append(float(rms_disp))
        self._rms_lfo_hist.append(float(rms_smooth))
        block_dur = float(frames) / max(1, self.sample_rate)
        lfo_hz, lfo_depth = 0.0, 0.0
        if len(self._rms_lfo_hist) >= 40:
            lfo_hz, lfo_depth = estimate_lfo_from_envelope(
                np.array(self._rms_lfo_hist, dtype=np.float64), block_dur
            )

        shake_level = 0.0
        envelope_wobble_hz = 0.0
        if len(self._rms_env_hist) >= 18 and block_dur > 0:
            xa = np.array(self._rms_env_hist, dtype=np.float64)
            mu_e = float(np.mean(xa))
            if mu_e > 1e-8:
                shake_level = min(1.0, float(np.std(xa)) / (mu_e + 1e-9) * 5.5)
                d = np.diff(xa)
                if d.size >= 8:
                    sg = np.sign(d)
                    sg[sg == 0.0] = 1.0
                    zc = int(np.sum(np.abs(np.diff(sg)) > 0))
                    span = max(1e-6, float(len(xa)) * block_dur)
                    envelope_wobble_hz = max(0.0, min(32.0, float(zc / (2.0 * span))))

        tension = float(getattr(frame, "tension", 0.0))
        release = float(getattr(frame, "release", 0.0))
        style_name = str(getattr(frame, "style", "atmosphere"))
        genre = str(getattr(frame, "genre", "calm_ambient"))
        section = str(getattr(frame, "section", "verse"))
        effect = str(getattr(frame, "effect", "none"))
        impact_score = float(getattr(frame, "style_score", 0.35))
        led_r = int(getattr(frame, "r", 255))
        led_g = int(getattr(frame, "g", 80))
        led_b = int(getattr(frame, "b", 40))
        # 溜め / tension_hold / build の強い抑制区間はレーザー消灯
        laser_hold = bool(
            (section in {"hold", "build"} and tension > 0.28)
            or (effect == "tension_hold" and tension > 0.30)
            or (tension > 0.45)
        )

        self._transient_env = max(0.0, self._transient_env * 0.93)
        if self._music_active and not laser_hold:
            # 立ち上がりのみ: 解放・ドロップ・強いオンセットだけモーション切替
            want_impact = (
                release > 0.55
                or (section == "drop" and beat and onset_strength > 0.6)
                or (beat and onset_strength > 0.72 and style_name == "impact")
            )
            if want_impact and (now - self._last_impact_fire_t) >= 0.45:
                self._last_impact_fire_t = now
                self._impact_generation += 1
                self._transient_env = max(self._transient_env, min(1.0, 0.35 + release * 0.4))

            # ピュン／揺れの自動切替は抑制（激しさの主因だった）
            want_pew = release > 0.7 and high > 0.35 and beat
            if want_pew and (now - self._last_pew_fire_t) >= 1.2:
                self._last_pew_fire_t = now
                self._laser_pew_generation += 1

            shake_level = min(1.0, shake_level * 0.55)
            if shake_level < 0.55:
                self._last_shake_emit_t = 0.0
        elif laser_hold:
            self._transient_env *= 0.8
            shake_level = 0.0

        # ジャンル → テクノ / ヒップホップ / クラブ
        t_sc = h_sc = c_sc = 0.15
        if genre in {"house_groove", "energetic"} or (
            style_name == "impact" and section in {"drop", "chorus", "build"}
        ):
            t_sc += 0.55
        if genre in {"punchy_bass", "dark_bass"}:
            h_sc += 0.55
        if genre in {"bright_pop", "edgy", "warm_acoustic", "soft_ballad", "calm_ambient"}:
            c_sc += 0.45
        if bpm >= 124 and impact_score > 0.5:
            t_sc += 0.2
        if 82 <= bpm <= 118 and (bass + sub) > 0.35:
            h_sc += 0.2
        ssum = t_sc + h_sc + c_sc + 1e-6
        t_sc, h_sc, c_sc = t_sc / ssum, h_sc / ssum, c_sc / ssum
        if t_sc >= h_sc and t_sc >= c_sc:
            style = StyleGuess.TECHNO
        elif h_sc >= c_sc:
            style = StyleGuess.HIPHOP
        else:
            style = StyleGuess.CLUB

        steadiness = 0.5
        if len(self._intervals) >= 5:
            arr = np.array(self._intervals, dtype=np.float64)
            mu = float(np.mean(arr))
            sd_ = float(np.std(arr))
            cv = sd_ / max(1e-6, mu)
            steadiness = 1.0 / (1.0 + 3.2 * cv)

        centroid_norm = float(np.clip(brightness * 0.65 + presence * 0.25 + high * 0.15, 0.0, 1.0))

        snap = AudioSnapshot(
            rms=float(rms_disp),
            rms_smooth=float(rms_smooth),
            bass=float(bass + sub * 0.5),
            mid=float(mid if mid > 0 else presence),
            high=float(high),
            centroid_hz=centroid_hz,
            centroid_norm=centroid_norm,
            flux=float(flux_n * 0.0005),
            flux_smooth=float(flux_smooth),
            bpm=float(bpm),
            onset_strength=float(onset_strength),
            beat_phasor=float(ph),
            steadiness=float(steadiness),
            style=style,
            techno_score=float(t_sc),
            hiphop_score=float(h_sc),
            club_score=float(c_sc),
            device_sr=self.sample_rate,
            block_ms=1000.0 * frames / max(1, self.sample_rate),
            valid=True,
            music_active=bool(self._music_active),
            impact_generation=int(self._impact_generation),
            transient_level=float(self._transient_env),
            laser_pew_generation=int(self._laser_pew_generation),
            shake_level=float(shake_level),
            envelope_wobble_hz=float(envelope_wobble_hz),
            shake_generation=int(self._shake_generation),
            lfo_hz=float(lfo_hz),
            lfo_depth=float(lfo_depth),
            tension=float(tension),
            release=float(release),
            section=section,
            led_r=led_r,
            led_g=led_g,
            led_b=led_b,
            laser_hold=laser_hold,
            beat=bool(beat),
        )
        with self._lock:
            self._snap = snap
            self._lighting_frame = frame


def _dmx_sens_to_ble(sens: float) -> float:
    """DMX 感度(0.45–1.85) または BLE 感度(0.3–2.5) を BLE analyze 用に正規化。"""
    s = float(sens)
    if 0.4 <= s <= 2.0:
        t = (max(0.45, min(1.85, s)) - 0.45) / (1.85 - 0.45)
        return 0.3 + t * (2.5 - 0.3)
    return max(0.3, min(2.5, s))




def _ch9_boost_transient(ch9: int, s: AudioSnapshot) -> int:
    """突発時も固定色帯を維持（自動色帯には上げない）。"""
    return max(0, min(119, int(ch9)))


# 点モード CH9 帯の代表値（pattern.txt: 赤/緑/橙/青/紫/水色）
_CH9_RED = 10
_CH9_GREEN = 30
_CH9_ORANGE = 50
_CH9_BLUE = 70
_CH9_PURPLE = 90
_CH9_CYAN = 110


def ch9_from_rgb(r: int, g: int, b: int) -> int:
    """
    LED RGB → 点モード CH9 固定色帯（pattern.txt）。
    0–19赤 / 20–39緑 / 40–59オレンジ / 60–79青 / 80–99紫 / 100–119水色

    BluetoothLED の暖色（金〜橙）・涼色（藍〜紫）が、旧 RGB 比判定では
    赤／青に潰れていたため HSV で帯を選ぶ。
    """
    import colorsys

    r = max(0, min(255, int(r)))
    g = max(0, min(255, int(g)))
    b = max(0, min(255, int(b)))
    mx = max(r, g, b)
    if mx < 14:
        return _CH9_RED

    h, s, v = colorsys.rgb_to_hsv(r / 255.0, g / 255.0, b / 255.0)

    # ほぼ白〜淡色（LED の明るさだけが残るとき）
    if s < 0.18:
        if v > 0.75:
            return _CH9_CYAN
        # 暖白寄り（R≥B）→ オレンジ、涼白 → 紫
        return _CH9_ORANGE if r >= b else _CH9_PURPLE

    # h: 0=赤, ~0.08=橙/金, ~0.33=緑, ~0.5=シアン, ~0.66=青, ~0.78=紫, ~0.92=マゼンタ
    # 暖色は広くオレンジへ（純赤はごく狭い帯だけ）
    if h < 0.02 or h >= 0.97:
        return _CH9_RED
    if h < 0.18:
        return _CH9_ORANGE  # 金／暖色／黄（レーザーに黄帯なし）
    if h < 0.42:
        return _CH9_GREEN
    if h < 0.55:
        return _CH9_CYAN
    if h < 0.68:
        return _CH9_BLUE
    if h < 0.88:
        return _CH9_PURPLE  # 藍〜紫（LED dark/hold の主帯）
    # 深紅／マゼンタ：B が目立れば紫、否则赤
    if b > r * 0.85 and b > g + 15:
        return _CH9_PURPLE
    return _CH9_RED


def ch9_from_snapshot(s: AudioSnapshot) -> int:
    """LED 色を優先。無いときだけスペクトルから控えめに推定。"""
    led_r = int(getattr(s, "led_r", 0) or 0)
    led_g = int(getattr(s, "led_g", 0) or 0)
    led_b = int(getattr(s, "led_b", 0) or 0)
    if led_r + led_g + led_b > 12:
        return ch9_from_rgb(led_r, led_g, led_b)
    # フォールバックも赤青偏りを避ける
    centroid = float(s.centroid_norm)
    if centroid < 0.30:
        return _CH9_PURPLE
    if centroid < 0.45:
        return _CH9_ORANGE
    if centroid < 0.58:
        return _CH9_CYAN
    if centroid < 0.72:
        return _CH9_BLUE
    return _CH9_GREEN


def motion_speed_from_snapshot(s: AudioSnapshot, *, sens: float) -> float:
    """テンポ主体の穏やかな速度。立ち上がりで少しだけ加速。"""
    from laser_dmx_app import MOTION_SPEED_MAX, MOTION_SPEED_MIN

    if not s.music_active or getattr(s, "laser_hold", False):
        return float(MOTION_SPEED_MIN)

    bpm = max(70.0, min(160.0, float(s.bpm)))
    base = 0.55 + (bpm / 120.0) * 0.55
    ons = float(s.onset_strength)
    if ons > 0.35:
        base *= 1.0 + 0.22 * min(1.0, ons)
    rel = float(getattr(s, "release", 0.0))
    if rel > 0.4:
        base *= 1.0 + 0.28 * min(1.0, rel)
    base *= 0.75 + 0.35 * max(0.35, min(1.5, float(sens)))
    soft_max = min(float(MOTION_SPEED_MAX), 3.4)
    return max(float(MOTION_SPEED_MIN), min(soft_max, base))


def pick_motion_index(s: AudioSnapshot, *, rng) -> int:
    """穏やかな軌道中心。混沌・ストロボ系は使わない。"""
    if getattr(s, "laser_hold", False):
        return 0
    calm = [0, 1, 2, 3, 4, 6, 7, 8]
    if s.style == StyleGuess.HIPHOP:
        calm = [0, 3, 14, 2, 9]
    elif s.style == StyleGuess.TECHNO:
        calm = [0, 2, 4, 6, 8, 11]
    n = _motion_count()
    pool = [i for i in calm if 0 <= i < n]
    if not pool:
        return 0
    return int(rng.choice(pool))


def pick_impact_motion_index(s: AudioSnapshot, *, rng) -> int:
    """立ち上がり用: 単発ステップ or 穏やかなスイープのみ。"""
    if getattr(s, "laser_hold", False):
        return 0
    one = _index_one_shot_horizontal_dot_motion()
    lo: list[int | None] = [0, 2, 3, 9, 14]
    if one is not None:
        lo = [one, 0, 2, 3]
    if float(getattr(s, "release", 0.0)) > 0.55:
        lo = [0, 2, 6, 14] + ([one] if one is not None else [])
    n = _motion_count()
    pool = [i for i in lo if i is not None and 0 <= i < n]
    if not pool:
        return pick_motion_index(s, rng=rng)
    return int(rng.choice(pool))


def pick_shake_horizontal_index(s: AudioSnapshot, *, rng) -> int:
    """揺れ時も水平系のみ・穏やか。"""
    pool = [0, 6, 7]
    n = _motion_count()
    pool = [i for i in pool if 0 <= i < n]
    if not pool:
        return 0
    return int(rng.choice(pool))


def _index_one_shot_horizontal_dot_motion() -> int | None:
    """【点】単発横ステップ一方向のインデックス。"""
    from laser_dmx_app import DOT_POINT_MOTIONS

    for i, (name, _fn) in enumerate(DOT_POINT_MOTIONS):
        if "単発横ステップ一方向" in name:
            return i
    return None


def pick_laser_pew_motion_index(s: AudioSnapshot, *, rng) -> int:
    """ピュン系も単発横ステップに限定。"""
    idx = _index_one_shot_horizontal_dot_motion()
    if idx is not None:
        return int(idx)
    return pick_impact_motion_index(s, rng=rng)


def _motion_count() -> int:
    from laser_dmx_app import DOT_POINT_MOTIONS

    return len(DOT_POINT_MOTIONS)
