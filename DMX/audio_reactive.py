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
    # 高いとがった電子音の反復（実測 Hz / 位相 0〜1 / 強さ 0〜1）
    synth_pulse_hz: float = 0.0
    synth_pulse_phasor: float = 0.0
    high_point_level: float = 0.0
    # tip帯(2.8–7.5k) / snare胴(0.9–2.8k)。スネア誤爆切り分け用
    synth_tip_ratio: float = 0.0
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
    # BluetoothLED の effect（accel_blink 中は左右ファン禁止）
    led_effect: str = "none"


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


def estimate_stutter_from_samples(
    samples: np.ndarray, sample_rate: int
) -> tuple[float, float]:
    """
    1 ブロック波形から細かいゲート／スタッター変調を推定。
    戻り値: (変調 Hz, 深さ 0〜1)。ブロック内だけでは足りないので呼び出し側で履歴結合する想定。
    """
    if samples is None:
        return 0.0, 0.0
    x = np.asarray(samples, dtype=np.float64).reshape(-1)
    sr = max(1000, int(sample_rate))
    if x.size < 48:
        return 0.0, 0.0
    hop = max(1, int(sr / 400))  # ~400 Hz エンベロープ
    n = (x.size // hop) * hop
    if n < hop * 6:
        return 0.0, 0.0
    env = np.abs(x[:n]).reshape(-1, hop).mean(axis=1)
    mu = float(np.mean(env))
    if mu < 1e-6:
        return 0.0, 0.0
    depth = float(min(1.0, (float(np.std(env)) / (mu + 1e-9)) * 2.8))
    return 0.0, depth  # Hz は履歴側で計算


def stutter_rate_from_envelope_hist(
    env_hist: np.ndarray, dt_sec: float
) -> tuple[float, float]:
    """高時間分解能エンベロープ履歴からスタッター Hz と深さ。"""
    if env_hist.size < 12 or dt_sec < 1e-6:
        return 0.0, 0.0
    x = env_hist.astype(np.float64, copy=False)
    mu = float(np.mean(x))
    if mu < 1e-7:
        return 0.0, 0.0
    depth = float(min(1.0, (float(np.std(x)) / (mu + 1e-9)) * 3.0))
    d = np.diff(x)
    if d.size < 6:
        return 0.0, depth
    # 小さなノイズを無視してゼロクロス
    thr = max(1e-8, float(np.std(d)) * 0.12)
    sg = np.zeros(d.size, dtype=np.float64)
    sg[d > thr] = 1.0
    sg[d < -thr] = -1.0
    for i in range(1, sg.size):
        if sg[i] == 0.0:
            sg[i] = sg[i - 1]
    if sg[0] == 0.0:
        sg[0] = 1.0
    zc = int(np.sum(np.abs(np.diff(sg)) > 0))
    span = float(env_hist.size) * dt_sec
    hz = zc / (2.0 * max(1e-6, span))
    return float(min(40.0, hz)), depth


def effective_wobble_hz(s: AudioSnapshot) -> float:
    """高速ゼロクロス由来の wobble Hz と LFO 推定を合体（揺れ速度の参照用）。"""
    w = float(s.envelope_wobble_hz)
    if s.lfo_depth > 0.11 and s.lfo_hz > 0.07:
        w = max(w, float(s.lfo_hz) * (0.66 + 0.40 * float(s.lfo_depth)))
    return w


def triplet_hz_from_bpm(bpm: float) -> float:
    """1拍を3分割した繰り返し周波数（3連符）。"""
    b = max(70.0, min(180.0, float(bpm)))
    return b / 60.0 * 3.0


def is_high_point_synth_pulse(s: AudioSnapshot) -> bool:
    """
    高いとがった電子音の反復。
    宽带域 centroid ではなく tip帯比率で判定（キック混じりでも拾い、スネア胴は落とす）。
    スネア加速ロール（accel_blink）中は False。
    """
    if not s.music_active or getattr(s, "laser_hold", False):
        return False
    if str(getattr(s, "led_effect", "") or "") == "accel_blink":
        return False
    hp = float(getattr(s, "high_point_level", 0.0))
    tip = float(getattr(s, "synth_tip_ratio", 0.0))
    pulse_hz = float(getattr(s, "synth_pulse_hz", 0.0))
    # tip帯が snare 胴より強い／同程度 + 尖りレベル
    pointed = hp >= 0.18 and tip >= 0.65
    if not pointed:
        return False
    # 参考クリップ実測 ≈4.3Hz / 動画コメント 7.8Hz の両方を許容
    if 3.4 <= pulse_hz <= 11.5:
        return True
    w = effective_wobble_hz(s)
    target = triplet_hz_from_bpm(float(s.bpm))
    if 3.4 <= w <= 11.5 and abs(w - target) <= max(1.4, target * 0.45) and tip >= 0.75:
        return True
    return False


def is_triplet_synth_pulse(s: AudioSnapshot) -> bool:
    """互換エイリアス: 高いとがった電子音の反復。"""
    return is_high_point_synth_pulse(s)


def synth_pulse_side(s: AudioSnapshot) -> int:
    """反復1回ごとに左右を切替（0 / 1）。phasor は 0〜2（1.0 進むごとに側が変わる）。"""
    hz = float(getattr(s, "synth_pulse_hz", 0.0))
    if hz >= 2.5:
        ph2 = float(getattr(s, "synth_pulse_phasor", 0.0)) % 2.0
        return 0 if ph2 < 1.0 else 1
    return int(float(s.beat_phasor) * 3.0 + 1e-9) % 2


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

        # 可能ならステレオ入力（後で平均）— モノラル固定だと片ch欠損が起きやすい
        self.channels = 1
        try:
            if self._device is not None and self._device != "" and self._device != -1:
                info = sd.query_devices(self._device)
            else:
                info = sd.query_devices(kind="input")
            max_in = int(info.get("max_input_channels", 1) or 1)
            if max_in >= 2:
                self.channels = 2
        except Exception:
            self.channels = 1
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
        # 高いとがった電子音: tip帯(2.8–7.5k)エンベロープと snare胴 比率
        self._high_env_hist: deque[float] = deque(maxlen=100)
        self._high_env_dt = 1.0 / 250.0
        self._high_peak_times: deque[float] = deque(maxlen=24)
        self._high_peak_intervals: deque[float] = deque(maxlen=12)
        self._synth_pulse_hz_ema = 0.0
        self._synth_pulse_phase_acc = 0.0
        self._last_high_env = 0.0
        self._synth_tip_ratio_ema = 0.0
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
            led_effect="none",
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
            # ステレオは左右平均（片chだけだとスネアを取りこぼす）
            if getattr(indata, "ndim", 1) > 1 and indata.shape[1] > 1:
                mono = np.mean(indata, axis=1)
            else:
                mono = indata[:, 0] if getattr(indata, "ndim", 1) > 1 else indata
            samples = np.ascontiguousarray(mono, dtype=np.float32)
            ble_sens = _dmx_sens_to_ble(float(self.gate_sensitivity))
            features = self._analyzer.analyze(samples, ble_sens)
            # 実コールバック長で時間を進める（固定40ms前提をやめる）
            features["block_sec"] = float(frames) / float(self.sample_rate)
            frame = self._mapper.map(features)
            self._ingest(features, frame, frames, samples=samples)
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

    def _ingest(
        self,
        features: dict,
        frame,
        frames: int,
        samples: np.ndarray | None = None,
    ) -> None:
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

        # 高いとがった電子音: tip帯 STFT エンベロープから反復を実測（キック混じり耐性）
        high_point_level = 0.0
        synth_pulse_hz = 0.0
        synth_tip_ratio = float(self._synth_tip_ratio_ema)
        if samples is not None:
            x = np.asarray(samples, dtype=np.float64).reshape(-1)
            sr = max(1000, int(self.sample_rate))
            if x.size >= 64:
                hop = max(1, int(sr / 200))
                win = max(hop * 2, int(0.020 * sr))
                n_frames = max(0, (x.size - win) // hop + 1)
                if n_frames >= 4:
                    tip_env: list[float] = []
                    mid_env: list[float] = []
                    self._high_env_dt = hop / float(sr)
                    for fi in range(n_frames):
                        i0 = fi * hop
                        seg = x[i0 : i0 + win] * np.hanning(win)
                        sp = np.abs(np.fft.rfft(seg)) ** 2
                        fr = np.fft.rfftfreq(win, d=1.0 / sr)
                        tip_e = float(np.sum(sp[(fr >= 2800.0) & (fr < 7500.0)]))
                        mid_e = float(np.sum(sp[(fr >= 900.0) & (fr < 2800.0)]))
                        tip_env.append(tip_e)
                        mid_env.append(mid_e)
                    tip_a = np.asarray(tip_env, dtype=np.float64)
                    mid_a = np.asarray(mid_env, dtype=np.float64)
                    # 疎なクリックでも潰さない: 上位帯のエネルギー比 + 全体和
                    tip_hi = float(np.percentile(tip_a, 80))
                    mid_hi = float(np.percentile(mid_a, 80))
                    tip_sum = float(np.sum(tip_a))
                    mid_sum = float(np.sum(mid_a))
                    ratio_p = tip_hi / (mid_hi + 1e-12)
                    ratio_s = tip_sum / (mid_sum + 1e-12)
                    inst_tip = float(max(ratio_p, ratio_s))
                    # ピークフレームだけでも評価（無音ホップの中央値落ち対策）
                    peak_mask = tip_a >= (float(np.mean(tip_a)) * 1.25 + 1e-12)
                    if np.any(peak_mask):
                        ratio_pk = float(np.mean(tip_a[peak_mask]) / (float(np.mean(mid_a[peak_mask])) + 1e-12))
                        inst_tip = max(inst_tip, ratio_pk)
                    self._synth_tip_ratio_ema = 0.70 * self._synth_tip_ratio_ema + 0.30 * inst_tip
                    synth_tip_ratio = float(self._synth_tip_ratio_ema)
                    mu_e = float(np.mean(tip_a)) + 1e-12
                    for i, vv in enumerate(tip_a.tolist()):
                        self._high_env_hist.append(float(vv))
                        prev = self._last_high_env
                        self._last_high_env = float(vv)
                        # tip帯の立ち上がりだけをパルス候補に
                        if vv > prev * 1.28 and vv > mu_e * 1.35 and vv > 1e-9:
                            t_peak = now - (len(tip_a) - 1 - i) * self._high_env_dt
                            if not self._high_peak_times or (t_peak - self._high_peak_times[-1]) >= 0.050:
                                if self._high_peak_times:
                                    gap = t_peak - self._high_peak_times[-1]
                                    # 参考≈4.3Hz(0.23s)〜電子パルス≈8Hz(0.12s)
                                    if 0.085 <= gap <= 0.32:
                                        self._high_peak_intervals.append(float(gap))
                                self._high_peak_times.append(float(t_peak))
                    if len(self._high_env_hist) >= 12:
                        ha = np.array(self._high_env_hist, dtype=np.float64)
                        cv = float(np.std(ha) / (float(np.mean(ha)) + 1e-12))
                        tip_n = float(np.clip(np.log1p(synth_tip_ratio) / np.log1p(3.0), 0.0, 1.0))
                        high_point_level = float(
                            np.clip(
                                0.40 * tip_n
                                + 0.30 * min(1.0, cv / 2.2)
                                + 0.20 * float(high)
                                + 0.10 * float(brightness),
                                0.0,
                                1.0,
                            )
                        )

        if len(self._high_peak_intervals) >= 2:
            med = float(np.median(np.array(self._high_peak_intervals, dtype=np.float64)))
            if med > 1e-4:
                inst_hz = 1.0 / med
                if 3.0 <= inst_hz <= 12.0:
                    if self._synth_pulse_hz_ema <= 1e-6:
                        self._synth_pulse_hz_ema = inst_hz
                    else:
                        self._synth_pulse_hz_ema = 0.72 * self._synth_pulse_hz_ema + 0.28 * inst_hz
                    synth_pulse_hz = float(self._synth_pulse_hz_ema)

        # 位相: 実測パルス Hz で進め、1 反復ごとに左右が切り替わる
        if synth_pulse_hz >= 2.5 and high_point_level >= 0.16 and self._music_active:
            self._synth_pulse_phase_acc += block_dur * synth_pulse_hz
        elif self._high_peak_times and (now - self._high_peak_times[-1]) < 0.40:
            period = 1.0 / max(2.5, synth_pulse_hz if synth_pulse_hz >= 2.5 else triplet_hz_from_bpm(bpm))
            self._synth_pulse_phase_acc = float(len(self._high_peak_times)) + (
                (now - self._high_peak_times[-1]) / period
            )
        else:
            synth_pulse_hz = synth_pulse_hz if high_point_level >= 0.12 else 0.0

        synth_pulse_phasor = float(self._synth_pulse_phase_acc % 2.0)

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

        # 高いとがった音の反復 Hz を揺れ指標へ（3–10Hz のみ）
        if 3.0 <= synth_pulse_hz <= 10.5 and high_point_level >= 0.20:
            envelope_wobble_hz = float(synth_pulse_hz)
            shake_level = max(shake_level, min(1.0, 0.25 + high_point_level * 0.7))

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
        pointed_pulse = (
            self._music_active
            and effect != "accel_blink"
            and high_point_level >= 0.20
            and synth_tip_ratio >= 0.70
            and 3.4 <= synth_pulse_hz <= 11.5
        )
        if pointed_pulse:
            laser_hold = False
        # スネア加速ロール中は左右ファン／シェイクを出さない
        if effect == "accel_blink":
            pointed_pulse = False
            shake_level = 0.0
            synth_pulse_hz = 0.0
            high_point_level = min(high_point_level, 0.12)
            synth_tip_ratio = min(synth_tip_ratio, 0.35)

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

            want_pew = release > 0.7 and high > 0.35 and beat
            if want_pew and (now - self._last_pew_fire_t) >= 1.2:
                self._last_pew_fire_t = now
                self._laser_pew_generation += 1

            if pointed_pulse:
                if (now - self._last_shake_emit_t) >= 0.18:
                    self._last_shake_emit_t = now
                    self._shake_generation += 1
            else:
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
            synth_pulse_hz=float(synth_pulse_hz),
            synth_pulse_phasor=float(synth_pulse_phasor),
            high_point_level=float(high_point_level),
            synth_tip_ratio=float(synth_tip_ratio),
            tension=float(tension),
            release=float(release),
            section=section,
            led_r=led_r,
            led_g=led_g,
            led_b=led_b,
            laser_hold=laser_hold,
            beat=bool(beat),
            led_effect=str(effect or "none"),
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
    """テンポ主体の穏やかな速度。とがった高音の反復時は実測 Hz に合わせる。"""
    from laser_dmx_app import MOTION_SPEED_MAX, MOTION_SPEED_MIN

    if not s.music_active or getattr(s, "laser_hold", False):
        return float(MOTION_SPEED_MIN)

    if is_high_point_synth_pulse(s):
        # 左右切替は synth_pulse_phasor が主。内部位相は補助程度
        hz = float(getattr(s, "synth_pulse_hz", 0.0)) or effective_wobble_hz(s)
        base = 1.8 + min(3.2, max(0.0, hz - 3.0) * 0.45)
        base *= 0.85 + 0.25 * max(0.35, min(1.5, float(sens)))
        return max(float(MOTION_SPEED_MIN), min(5.5, base))

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
    # 盛り上がり中は上限を上げて激しく
    if is_hype_energy(s):
        base *= 1.35 + 0.35 * min(1.0, max(rel, ons))
        soft_max = min(float(MOTION_SPEED_MAX), 7.5)
    return max(float(MOTION_SPEED_MIN), min(soft_max, base))


def _motion_indices_matching(*needles: str) -> list[int]:
    from laser_dmx_app import DOT_POINT_MOTIONS, _is_point_control_motion_name

    out: list[int] = []
    for i, (name, _fn) in enumerate(DOT_POINT_MOTIONS):
        if not _is_point_control_motion_name(name):
            continue
        if any(n in name for n in needles):
            out.append(i)
    return out


def _point_motion_pool_fallback() -> list[int]:
    from laser_dmx_app import _point_control_motion_indices

    return list(_point_control_motion_indices())


def is_hype_energy(s: AudioSnapshot) -> bool:
    """サビ／ドロップ／解放・高エネルギー時 True。"""
    if not s.music_active or getattr(s, "laser_hold", False):
        return False
    sec = str(getattr(s, "section", "") or "")
    release = float(getattr(s, "release", 0.0))
    tension = float(getattr(s, "tension", 0.0))
    rms = float(getattr(s, "rms_smooth", 0.0))
    onset = float(getattr(s, "onset_strength", 0.0))
    transient = float(getattr(s, "transient_level", 0.0))
    club = float(getattr(s, "club_score", 0.0))
    if sec in {"chorus", "drop"}:
        return True
    if release >= 0.42:
        return True
    if sec == "build" and (tension >= 0.55 or onset >= 0.55):
        return True
    if rms >= 0.12 and (onset >= 0.55 or transient >= 0.55) and club >= 0.35:
        return True
    if rms >= 0.16 and onset >= 0.45:
        return True
    return False


def pick_hype_motion_index(s: AudioSnapshot, *, rng) -> int:
    """盛り上がり用: 疑似点モーションのみで激しめローテ。"""
    if getattr(s, "laser_hold", False):
        return 0
    from laser_dmx_app import DOT_POINT_MOTIONS as _DPM

    pool = _motion_indices_matching(
        "テクノ",
        "クラブ",
        "ヒップホップ",
        "キックウィップ",
        "スターバースト",
        "十字スキャン",
        "ジグザグ",
        "ハイパー",
        "トンネル",
        "グリッド",
        "二段レール",
        "ドットレイン",
        "ミラーツイン",
        "三点左右",
        "共有横シェイク",
        "扇状",
        "四隅",
        "∞字",
        "バラ曲線",
        "バウンスボール",
        "ウェーブリボン",
        "スパイラル",
        "リサージュ",
        "サークル軌道",
        "振れ",
        "シンセ",
        "4点上空ファン",
        "参考】4点",
    )
    exclude = ("単発横ステップ", "中心ブリーズ", "色のみ", "床エッジ広がる", "床エッジ閉じる")
    pool = [i for i in pool if not any(x in _DPM[i][0] for x in exclude)]

    if s.style == StyleGuess.TECHNO:
        tech = _motion_indices_matching(
            "テクノ",
            "ハイパー",
            "トンネル",
            "ジグザグ",
            "十字",
            "キック",
            "スターバースト",
            "三点左右",
        )
        pool = list(dict.fromkeys(tech + pool))
    elif s.style == StyleGuess.HIPHOP:
        hh = _motion_indices_matching(
            "ヒップホップ",
            "キック",
            "四隅",
            "バウンス",
            "三点左右",
            "ドットレイン",
            "ステップ",
        )
        pool = list(dict.fromkeys(hh + pool))

    if float(getattr(s, "release", 0.0)) >= 0.50 or str(getattr(s, "section", "")) == "drop":
        hot = _motion_indices_matching(
            "最高速",
            "混沌",
            "ストロボグリッド",
            "スターバースト",
            "キックウィップ",
            "ハイパー",
            "ジグザグ",
            "トンネル",
            "左右フラッシュ",
            "三点左右",
            "振れ",
        )
        pool = list(dict.fromkeys(hot + pool))

    n = _motion_count()
    pool = [i for i in pool if 0 <= i < n]
    if not pool:
        pool = [i for i in _point_motion_pool_fallback() if 0 <= i < n]
    if not pool:
        return 0
    return int(rng.choice(pool))


def pick_motion_index(s: AudioSnapshot, *, rng) -> int:
    """通常は穏やか。盛り上がり時は激しめ多彩へ。"""
    if getattr(s, "laser_hold", False):
        return 0
    if is_hype_energy(s):
        return pick_hype_motion_index(s, rng=rng)
    calm = _motion_indices_matching(
        "水平スイープ",
        "垂直スイープ",
        "サークル",
        "対角",
        "リサージュ",
        "床ライン",
        "天井ライン",
        "スパイラル",
        "∞字",
        "振り子",
        "ウェーブリボン",
        "ダイヤ",
        "ヘリックス",
        "バラ曲線",
        "バウンスボール",
        "扇状",
        "中心ブリーズ",
        "矩形周回",
        "ミラーツイン",
    )
    if s.style == StyleGuess.HIPHOP:
        calm = _motion_indices_matching(
            "ヒップホップ",
            "水平スイープ",
            "対角",
            "サークル軌道",
            "単発横ステップ",
            "バウンスボール",
            "四隅",
            "ウェーブリボン",
            "三点左右",
        )
    elif s.style == StyleGuess.TECHNO:
        from laser_dmx_app import DOT_POINT_MOTIONS as _DPM

        calm = _motion_indices_matching(
            "テクノ",
            "水平スイープ",
            "サークル",
            "リサージュ",
            "床ライン",
            "スパイラル",
            "∞字",
            "十字スキャン",
            "スターバースト",
            "ハイパー",
        )
        calm = [
            i
            for i in calm
            if "混沌" not in _DPM[i][0]
            and "最高速" not in _DPM[i][0]
            and "ストロボグリッド" not in _DPM[i][0]
        ]
    n = _motion_count()
    pool = [i for i in calm if 0 <= i < n]
    if not pool:
        return 0
    return int(rng.choice(pool))


def pick_impact_motion_index(s: AudioSnapshot, *, rng) -> int:
    """立ち上がり用。盛り上がり中は激しめ池から。"""
    if getattr(s, "laser_hold", False):
        return 0
    if is_hype_energy(s) and float(getattr(s, "release", 0.0)) >= 0.35:
        return pick_hype_motion_index(s, rng=rng)
    one = _index_one_shot_horizontal_dot_motion()
    lo = _motion_indices_matching(
        "水平スイープ",
        "サークル",
        "対角",
        "単発横ステップ",
        "ヒップホップ",
        "キックウィップ",
        "四隅",
        "スターバースト",
        "バウンスボール",
    )
    if one is not None:
        lo = [one] + [i for i in lo if i != one][:4]
    if float(getattr(s, "release", 0.0)) > 0.55:
        return pick_hype_motion_index(s, rng=rng)
    n = _motion_count()
    pool = [i for i in lo if 0 <= i < n]
    if not pool:
        return pick_motion_index(s, rng=rng)
    return int(rng.choice(pool))


def pick_shake_horizontal_index(s: AudioSnapshot, *, rng) -> int:
    """高いとがった電子音の反復 → 条件が強いときだけ参考4点ファン。"""
    from laser_dmx_app import DOT_POINT_MOTIONS

    if str(getattr(s, "led_effect", "") or "") == "accel_blink":
        # スネアロール中は左右ファンにしない
        return pick_motion_index(s, rng=rng)

    ref: list[int] = []
    three: list[int] = []
    triplet: list[int] = []
    for i, (name, _fn) in enumerate(DOT_POINT_MOTIONS):
        if "4点上空ファン" in name:
            ref.append(i)
        if "三点左右シェイク" in name or "共有横シェイク" in name:
            three.append(i)
        if "シンセ】とがった高音" in name or "シンセ】3連符" in name or "シンセ】左右激振" in name:
            triplet.append(i)
    hp = float(getattr(s, "high_point_level", 0.0))
    pulse = float(getattr(s, "synth_pulse_hz", 0.0))
    tip = float(getattr(s, "synth_tip_ratio", 0.0))
    # tip帯優位の反復のみ参考4点ファン（宽带域 centroid は使わない）
    if (
        ref
        and is_high_point_synth_pulse(s)
        and hp >= 0.20
        and tip >= 0.75
        and 3.4 <= pulse <= 11.5
    ):
        return int(rng.choice(ref))
    if three and is_high_point_synth_pulse(s):
        return int(rng.choice(three))
    if triplet and is_high_point_synth_pulse(s):
        if len(triplet) >= 2 and rng.random() < 0.22:
            return int(triplet[1])
        return int(triplet[0])
    pool = (three or [0, 6, 7]) + triplet
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
