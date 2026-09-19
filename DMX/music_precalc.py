#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
音楽ファイルの事前解析（BPM・拍位置・オンセット・ざっくり特徴列）。
再生同期用にモノラル波形も保持する（メモリ注意: 長尺は sr を下げる）。
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import numpy as np

try:
    import librosa
except ImportError:
    librosa = None  # type: ignore[assignment]

from audio_reactive import AudioSnapshot, StyleGuess, estimate_lfo_from_envelope

PRECALC_AVAILABLE = librosa is not None

EventKind = Literal["pew", "impact", "shake"]


@dataclass
class TrackProfile:
    """解析結果（オンメモリ）。JSON には audio を含めない。"""

    source_path: str
    duration_sec: float
    sr: int
    tempo_bpm: float
    beat_times: np.ndarray
    onset_times: np.ndarray
    # ダウンサンプル RMS エンベロープ（同期用の音圧おおよそ）
    rms_t: np.ndarray
    rms_v: np.ndarray
    # 事前モーションイベント（秒, 種別）昇順
    events: list[tuple[float, EventKind]] = field(default_factory=list)
    # 再生用（同期スタート時に使用）。大きい場合は None にしてファイルから再ロード可
    audio_mono: np.ndarray | None = None

    def summary_lines(self) -> list[str]:
        lines = [
            f"ファイル: {self.source_path}",
            f"長さ: {self.duration_sec:.2f} s",
            f"サンプルレート: {self.sr} Hz",
            f"推定テンポ: {self.tempo_bpm:.1f} BPM",
            f"拍イベント: {len(self.beat_times)}",
            f"オンセット候補: {len(self.onset_times)}",
            f"モーションイベント: {len(self.events)}",
        ]
        n_pew = sum(1 for _, k in self.events if k == "pew")
        n_imp = sum(1 for _, k in self.events if k == "impact")
        n_shk = sum(1 for _, k in self.events if k == "shake")
        lines.append(f"  内訳 — ピュン:{n_pew} 突発:{n_imp} 揺れ:{n_shk}")
        return lines

    def to_json_metadata(self) -> str:
        """波形以外を JSON で保存。"""
        d: dict[str, Any] = {
            "source_path": self.source_path,
            "duration_sec": self.duration_sec,
            "sr": self.sr,
            "tempo_bpm": self.tempo_bpm,
            "beat_times": self.beat_times.tolist(),
            "onset_times": self.onset_times.tolist(),
            "rms_t": self.rms_t.tolist(),
            "rms_v": self.rms_v.tolist(),
            "events": [{"t": t, "k": k} for t, k in self.events],
        }
        return json.dumps(d, ensure_ascii=False, indent=2)


def ensure_audio_buffer(p: TrackProfile) -> bool:
    """再生用 waveform が無いとき source_path から再ロードする。"""
    if p.audio_mono is not None and getattr(p.audio_mono, "size", 0) > 0:
        return True
    loaded = load_audio_for_playback(p.source_path, target_sr=int(p.sr) if p.sr else None)
    if loaded is None:
        return False
    y, sr, _dur = loaded
    p.audio_mono = y
    if sr > 0:
        p.sr = int(sr)
    return True


def load_audio_for_playback(
    path: str | Path,
    *,
    target_sr: int | None = 44100,
) -> tuple[Any, int, float] | None:
    """解析なしで再生用モノラル波形を読む。(y, sr, duration_sec) または None。

    soundfile → librosa → wav(標準ライブラリ) の順で試す。
    """
    path = Path(path)
    if not path.is_file():
        return None

    # 1) soundfile（軽量・mp3以外も広い）
    try:
        import soundfile as sf  # type: ignore

        data, sr = sf.read(str(path), always_2d=False)
        y = np.asarray(data, dtype=np.float32)
        if y.ndim > 1:
            y = np.mean(y, axis=1).astype(np.float32)
        sr_i = int(sr)
        if target_sr is not None and sr_i != int(target_sr) and librosa is not None:
            y = np.asarray(librosa.resample(y, orig_sr=sr_i, target_sr=int(target_sr)), dtype=np.float32)
            sr_i = int(target_sr)
        dur = float(len(y) / max(1, sr_i))
        return y, sr_i, dur
    except Exception:
        pass

    # 2) librosa
    if librosa is not None:
        try:
            y, sr = librosa.load(str(path), sr=target_sr, mono=True)
            y = np.asarray(y, dtype=np.float32)
            sr_i = int(sr)
            dur = float(len(y) / max(1, sr_i))
            return y, sr_i, dur
        except Exception:
            pass

    # 3) 標準 wave（PCM wav のみ）
    try:
        import wave

        with wave.open(str(path), "rb") as wf:
            nch = wf.getnchannels()
            sw = wf.getsampwidth()
            sr_i = int(wf.getframerate())
            nframes = wf.getnframes()
            raw = wf.readframes(nframes)
        if sw == 1:
            arr = np.frombuffer(raw, dtype=np.uint8).astype(np.float32)
            arr = (arr - 128.0) / 128.0
        elif sw == 2:
            arr = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
        elif sw == 4:
            arr = np.frombuffer(raw, dtype=np.int32).astype(np.float32) / 2147483648.0
        else:
            return None
        if nch > 1:
            arr = arr.reshape(-1, nch).mean(axis=1)
        y = np.asarray(arr, dtype=np.float32)
        if target_sr is not None and sr_i != int(target_sr) and librosa is not None:
            y = np.asarray(librosa.resample(y, orig_sr=sr_i, target_sr=int(target_sr)), dtype=np.float32)
            sr_i = int(target_sr)
        dur = float(len(y) / max(1, sr_i))
        return y, sr_i, dur
    except Exception:
        return None


def load_profile_from_json(path: Path) -> TrackProfile:
    raw = json.loads(path.read_text(encoding="utf-8"))
    evs = [(float(e["t"]), e["k"]) for e in raw["events"]]
    return TrackProfile(
        source_path=str(raw["source_path"]),
        duration_sec=float(raw["duration_sec"]),
        sr=int(raw["sr"]),
        tempo_bpm=float(raw["tempo_bpm"]),
        beat_times=np.array(raw["beat_times"], dtype=np.float64),
        onset_times=np.array(raw["onset_times"], dtype=np.float64),
        rms_t=np.array(raw["rms_t"], dtype=np.float64),
        rms_v=np.array(raw["rms_v"], dtype=np.float64),
        events=[(t, k) for t, k in evs if k in ("pew", "impact", "shake")],
        audio_mono=None,
    )


def _style_from_tempo(tempo: float) -> tuple[StyleGuess, float, float, float]:
    if tempo >= 124:
        return StyleGuess.TECHNO, 0.55, 0.22, 0.23
    if tempo <= 118:
        return StyleGuess.HIPHOP, 0.22, 0.53, 0.25
    return StyleGuess.CLUB, 0.28, 0.28, 0.44


def snapshot_at_time(p: TrackProfile, t: float) -> AudioSnapshot:
    """時刻 t における疑似 AudioSnapshot（ファイル同期用）。カウンタ系は 0 固定。"""
    t = max(0.0, min(float(t), p.duration_sec + 1e-3))
    music = t < p.duration_sec - 1e-4

    bt = p.beat_times
    ibpm = float(p.tempo_bpm)
    ph = 0.0
    if bt.size >= 2:
        i = int(np.searchsorted(bt, t, side="right") - 1)
        i = max(0, min(i, bt.size - 2))
        d = float(bt[i + 1] - bt[i])
        if d > 1e-6:
            ph = (t - float(bt[i])) / d
            ibpm = max(70.0, min(178.0, 60.0 / d))

    if p.rms_t.size > 0:
        rms_s = float(np.interp(t, p.rms_t, p.rms_v))
    else:
        rms_s = 0.01
    rms_s = max(1e-6, rms_s)

    # 揺れっぽさ: 近傍 ±0.25s の RMS 変動
    shake_level = 0.0
    wobble_hz = 0.0
    if p.rms_t.size > 4:
        m = (p.rms_t >= t - 0.25) & (p.rms_t <= t + 0.25)
        if np.any(m):
            seg = p.rms_v[m]
            mu = float(np.mean(seg))
            if mu > 1e-8:
                shake_level = float(min(1.0, (np.std(seg) / mu) * 4.5))
            dseg = np.diff(seg)
            if dseg.size > 4:
                sg = np.sign(dseg)
                sg[sg == 0.0] = 1.0
                zc = int(np.sum(np.abs(np.diff(sg)) > 0))
                span = float(len(seg)) * (float(p.rms_t[1] - p.rms_t[0]) if p.rms_t.size > 1 else 0.05)
                if span > 1e-6:
                    wobble_hz = float(min(32.0, zc / (2.0 * span)))

    # 超細かいゲートに埋もれても、テンポの 3連符（1/3拍）へ寄せる
    tri_hz = max(70.0, min(190.0, float(p.tempo_bpm))) / 60.0 * 3.0
    if shake_level > 0.15 and wobble_hz > 10.0:
        wobble_hz = float(tri_hz)
        shake_level = max(shake_level, 0.32)
    elif shake_level > 0.20 and wobble_hz < 3.0 and music:
        # RMS が平坦でも楽曲テンポの 3連符を仮定（Breakaway 系）
        wobble_hz = float(tri_hz)
        shake_level = max(shake_level, 0.28)

    lfo_hz, lfo_depth = 0.0, 0.0
    if p.rms_t.size >= 32:
        m = (p.rms_t >= t - 1.28) & (p.rms_t <= t + 1.28)
        if int(np.sum(m)) >= 30:
            tt = p.rms_t[m].astype(np.float64)
            vv = p.rms_v[m].astype(np.float64)
            dt = float(np.median(np.diff(tt))) if tt.size > 2 else 0.05
            lfo_hz, lfo_depth = estimate_lfo_from_envelope(vv, dt)

    # onset 強度おおよそ: オンセット時刻との近さ
    onset_str = 0.0
    if p.onset_times.size > 0:
        dt = np.min(np.abs(p.onset_times - t))
        if dt < 0.08:
            onset_str = float(max(0.0, 1.0 - dt / 0.08)) * 1.1

    transient_level = min(1.0, 0.35 * shake_level + 0.5 * onset_str)

    st, ts, hs, cs = _style_from_tempo(p.tempo_bpm)
    # 3連符帯の揺れでは中高域が立っている想定
    centroid_n = 0.45 + 0.12 * math.sin(t * 0.3) * shake_level
    if 3.0 <= wobble_hz <= 10.0 and shake_level > 0.2:
        centroid_n = min(1.0, 0.55 + 0.2 * min(1.0, wobble_hz / 8.0))
    flux_art = transient_level * 0.0004

    mid_v = rms_s * (0.95 if 3.0 <= wobble_hz <= 10.0 else 0.75)
    high_v = rms_s * (0.90 if 3.0 <= wobble_hz <= 10.0 else 0.55)
    # 高いとがった電子音: テンポ由来の反復位相（ファイル同期）
    pulse_hz = float(wobble_hz) if 3.0 <= wobble_hz <= 10.5 else float(tri_hz)
    high_point = float(min(1.0, 0.35 + high_v * 0.5 + shake_level * 0.3)) if music else 0.0
    pulse_phasor = (t * pulse_hz) % 2.0

    return AudioSnapshot(
        rms=rms_s,
        rms_smooth=rms_s,
        bass=rms_s * 0.9,
        mid=mid_v,
        high=high_v,
        centroid_hz=3200.0 + 1800.0 * centroid_n,
        centroid_norm=max(0.0, min(1.0, max(centroid_n, 0.62 if high_point > 0.4 else centroid_n))),
        flux=flux_art,
        flux_smooth=flux_art,
        bpm=ibpm,
        onset_strength=onset_str,
        beat_phasor=float(ph % 1.0),
        steadiness=0.55 if p.tempo_bpm >= 122 else 0.42,
        style=st,
        techno_score=ts,
        hiphop_score=hs,
        club_score=cs,
        device_sr=p.sr,
        block_ms=23.0,
        valid=True,
        music_active=music,
        impact_generation=0,
        transient_level=transient_level,
        laser_pew_generation=0,
        shake_level=shake_level,
        envelope_wobble_hz=wobble_hz,
        shake_generation=0,
        lfo_hz=lfo_hz,
        lfo_depth=lfo_depth,
        synth_pulse_hz=pulse_hz if high_point >= 0.2 else 0.0,
        synth_pulse_phasor=pulse_phasor,
        high_point_level=high_point,
        synth_tip_ratio=float(min(2.5, 0.55 + high_v * 1.6)) if music and high_point >= 0.2 else 0.0,
    )


def analyze_audio_file(
    path: str | Path,
    *,
    target_sr: int = 44100,
    max_duration_sec: float | None = None,
    keep_audio: bool = True,
) -> TrackProfile:
    if librosa is None:
        raise RuntimeError("librosa が必要です: pip install librosa soundfile")

    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(str(path))

    y, sr = librosa.load(str(path), sr=target_sr, mono=True)
    if max_duration_sec is not None and y.size > int(max_duration_sec * sr):
        y = y[: int(max_duration_sec * sr)]

    duration = float(len(y) / max(1, sr))
    onset_env = librosa.onset.onset_strength(y=y, sr=sr, aggregate=np.median)
    times_o = librosa.times_like(onset_env, sr=sr)
    tempo_arr, beats = librosa.beat.beat_track(y=y, sr=sr, onset_envelope=onset_env)
    ta = np.asarray(tempo_arr).ravel()
    tempo_f = float(ta[0]) if ta.size > 0 else 120.0
    tempo_f = max(70.0, min(190.0, tempo_f))
    beat_times = librosa.frames_to_time(beats, sr=sr)

    peaks = librosa.util.peak_pick(
        onset_env,
        pre_max=3,
        post_max=3,
        pre_avg=10,
        post_avg=10,
        delta=0.15,
        wait=max(1, int(sr / 512 * 0.12)),
    )
    onset_times = np.clip(times_o[peaks], 0.0, duration)

    # RMS ホップごと（粗めで軽量）
    hop = 512
    rms = librosa.feature.rms(y=y, hop_length=hop)[0]
    rms_times = librosa.frames_to_time(np.arange(len(rms)), sr=sr, hop_length=hop)
    rms_times = np.asarray(rms_times, dtype=np.float64)
    rms = np.asarray(rms, dtype=np.float64)
    if rms.size:
        rms = rms / max(1e-6, float(np.max(rms)))

    # 高音寄りオンセットでピュン候補
    y_hp = librosa.effects.preemphasis(y)
    on_hf = librosa.onset.onset_strength(y=y_hp, sr=sr, aggregate=np.max)
    times_hf = librosa.times_like(on_hf, sr=sr)
    pk_hf = librosa.util.peak_pick(
        on_hf, pre_max=3, post_max=3, pre_avg=8, post_avg=8, delta=0.18, wait=max(1, int(sr / 512 * 0.15))
    )
    pew_times = np.clip(times_hf[pk_hf], 0.0, duration)
    pew_set = [float(x) for x in pew_times]

    events: list[tuple[float, EventKind]] = []
    for ot in onset_times:
        t = float(ot)
        if any(abs(t - rp) < 0.05 for rp in pew_set):
            events.append((t, "pew"))
        else:
            events.append((t, "impact"))
    for rp in pew_set:
        if not any(abs(rp - e[0]) < 0.05 for e in events):
            events.append((rp, "pew"))

    step = 2 if tempo_f < 130 else 3
    beat_period = 60.0 / max(70.0, tempo_f)
    third = beat_period / 3.0
    for k, bt in enumerate(beat_times):
        if k % step == 0:
            events.append((float(bt), "shake"))
        # 1/3 拍ごとの左右切替用イベント
        for j in (0, 1, 2):
            events.append((float(bt) + j * third, "shake"))

    # 中高域の振幅変調が 3–10Hz（3連符帯）の区間だけ揺れイベントに
    try:
        S = np.abs(librosa.stft(y, n_fft=2048, hop_length=hop))
        freqs = librosa.fft_frequencies(sr=sr, n_fft=2048)
        band = (freqs >= 350.0) & (freqs <= 4500.0)
        if np.any(band) and S.shape[1] > 16:
            synth_env = np.mean(S[band], axis=0)
            synth_env = synth_env / max(1e-9, float(np.max(synth_env)))
            synth_t = librosa.frames_to_time(np.arange(len(synth_env)), sr=sr, hop_length=hop)
            hop_sec = float(synth_t[1] - synth_t[0]) if len(synth_t) > 1 else 0.02
            win = max(10, int(0.45 / max(1e-6, hop_sec)))
            stride = max(1, win // 3)
            for i in range(0, len(synth_env) - win, stride):
                seg = synth_env[i : i + win]
                mu = float(np.mean(seg))
                if mu < 0.08:
                    continue
                dseg = np.diff(seg)
                if dseg.size < 6:
                    continue
                sg = np.sign(dseg)
                sg[sg == 0.0] = 1.0
                zc = int(np.sum(np.abs(np.diff(sg)) > 0))
                span = float(win) * hop_sec
                wob = zc / (2.0 * max(1e-6, span))
                cv = float(np.std(seg) / max(1e-9, mu))
                if 3.0 <= wob <= 10.0 and cv >= 0.05:
                    events.append((float(synth_t[i + win // 2]), "shake"))
    except Exception:
        pass

    events.sort(key=lambda x: (x[0], {"pew": 0, "impact": 1, "shake": 2}[x[1]]))

    dedup: list[tuple[float, EventKind]] = []
    last_t = -1.0
    merge_gap = 0.055
    for tt, kk in events:
        # シンセ揺れは少し密に残す
        gap = 0.040 if kk == "shake" else merge_gap
        if tt - last_t < gap:
            if dedup and kk == "pew" and dedup[-1][1] != "pew":
                dedup[-1] = (dedup[-1][0], "pew")
            elif dedup and kk == "shake" and dedup[-1][1] != "shake" and tt - last_t >= 0.028:
                dedup[-1] = (dedup[-1][0], "shake")
            continue
        dedup.append((tt, kk))
        last_t = tt

    audio_mono = y.astype(np.float32) if keep_audio else None

    return TrackProfile(
        source_path=str(path.resolve()),
        duration_sec=duration,
        sr=int(sr),
        tempo_bpm=tempo_f,
        beat_times=np.asarray(beat_times, dtype=np.float64),
        onset_times=np.asarray(onset_times, dtype=np.float64),
        rms_t=rms_times,
        rms_v=rms,
        events=dedup,
        audio_mono=audio_mono,
    )
