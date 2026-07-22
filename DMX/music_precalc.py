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
    if librosa is None:
        return False
    path = Path(p.source_path)
    if not path.is_file():
        return False
    try:
        y, _sr = librosa.load(str(path), sr=int(p.sr), mono=True)
        p.audio_mono = np.asarray(y, dtype=np.float32)
    except OSError:
        return False
    return True


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
    centroid_n = 0.45 + 0.12 * math.sin(t * 0.3) * shake_level
    flux_art = transient_level * 0.0004

    return AudioSnapshot(
        rms=rms_s,
        rms_smooth=rms_s,
        bass=rms_s * 0.9,
        mid=rms_s * 0.75,
        high=rms_s * 0.55,
        centroid_hz=1800.0 + 2200.0 * centroid_n,
        centroid_norm=max(0.0, min(1.0, centroid_n)),
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
    for k, bt in enumerate(beat_times):
        if k % step == 0:
            events.append((float(bt), "shake"))

    events.sort(key=lambda x: (x[0], {"pew": 0, "impact": 1, "shake": 2}[x[1]]))

    dedup: list[tuple[float, EventKind]] = []
    last_t = -1.0
    merge_gap = 0.055
    for tt, kk in events:
        if tt - last_t < merge_gap:
            if dedup and kk == "pew" and dedup[-1][1] != "pew":
                dedup[-1] = (dedup[-1][0], "pew")
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
