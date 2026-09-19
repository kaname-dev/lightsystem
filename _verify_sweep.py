"""Offline verify tip-band sweep detector vs reference / snare / pointed."""
from __future__ import annotations

import importlib.util
import sys
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent


def _load_dmx_audio():
    path = ROOT / "DMX" / "audio_reactive.py"
    spec = importlib.util.spec_from_file_location("dmx_audio_reactive_verify2", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


dmx = _load_dmx_audio()
AudioSnapshot = dmx.AudioSnapshot
StyleGuess = dmx.StyleGuess
is_high_point_synth_pulse = dmx.is_high_point_synth_pulse


def load_wav(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as w:
        sr = w.getframerate()
        n = w.getnframes()
        ch = w.getnchannels()
        raw = np.frombuffer(w.readframes(n), dtype=np.int16).astype(np.float64)
        if ch > 1:
            raw = raw.reshape(-1, ch).mean(axis=1)
        return raw / 32768.0, sr


def synth_pointed(sr: int, dur: float = 3.0, pulse_hz: float = 7.8) -> np.ndarray:
    t = np.arange(int(sr * dur)) / sr
    x = np.zeros_like(t)
    period = 1.0 / pulse_hz
    for k in range(int(dur / period)):
        i0 = int(k * period * sr)
        n = int(0.012 * sr)
        if i0 + n >= len(x):
            break
        tt = np.arange(n) / sr
        burst = np.sin(2 * np.pi * 4200 * tt) * np.exp(-tt * 220)
        burst += 0.45 * np.sin(2 * np.pi * 6800 * tt) * np.exp(-tt * 280)
        x[i0 : i0 + n] += burst
    x += 0.02 * np.sin(2 * np.pi * 110 * t)
    return 0.55 * x / (np.max(np.abs(x)) + 1e-9)


def synth_snare_roll(sr: int, dur: float = 3.0, pulse_hz: float = 8.3) -> np.ndarray:
    t = np.arange(int(sr * dur)) / sr
    x = np.zeros_like(t)
    period = 1.0 / pulse_hz
    rng = np.random.default_rng(0)
    for k in range(int(dur / period)):
        i0 = int(k * period * sr)
        n = int(0.045 * sr)
        if i0 + n >= len(x):
            break
        noise = rng.normal(0, 1, n)
        noise = np.convolve(noise, np.ones(5) / 5, mode="same")
        env = np.exp(-np.arange(n) / sr * 45)
        body = 0.35 * np.sin(2 * np.pi * 220 * np.arange(n) / sr) * env
        x[i0 : i0 + n] += 0.7 * noise * env + body
    x += 0.12 * np.sin(2 * np.pi * 55 * t)
    return 0.55 * x / (np.max(np.abs(x)) + 1e-9)


def synth_pointed_with_bass(sr: int) -> np.ndarray:
    """Club-like: pointed clicks buried under kick (like phone-recorded ref)."""
    tip = synth_pointed(sr)
    t = np.arange(len(tip)) / sr
    kick = 0.45 * np.sin(2 * np.pi * 55 * t) * (np.sin(2 * np.pi * 2.1 * t) > 0.7)
    return 0.7 * (tip * 0.55 + kick)


def analyze_tip(x: np.ndarray, sr: int) -> tuple[float, float, float]:
    """Mirror DMX tip-band pulse / ratio / high_point_level."""
    hop = max(1, int(sr / 200))
    win = max(hop * 2, int(0.020 * sr))
    tip_env = []
    mid_env = []
    for i0 in range(0, max(0, len(x) - win) + 1, hop):
        if i0 + win > len(x):
            break
        seg = x[i0 : i0 + win] * np.hanning(win)
        sp = np.abs(np.fft.rfft(seg)) ** 2
        fr = np.fft.rfftfreq(win, d=1.0 / sr)
        tip_env.append(float(np.sum(sp[(fr >= 2800.0) & (fr < 7500.0)])))
        mid_env.append(float(np.sum(sp[(fr >= 900.0) & (fr < 2800.0)])))
    tip_a = np.asarray(tip_env, dtype=np.float64)
    mid_a = np.asarray(mid_env, dtype=np.float64)
    if tip_a.size < 4:
        return 0.0, 0.0, 0.0
    tip_hi = float(np.percentile(tip_a, 80))
    mid_hi = float(np.percentile(mid_a, 80))
    tip_sum = float(np.sum(tip_a))
    mid_sum = float(np.sum(mid_a))
    tip_ratio = float(max(tip_hi / (mid_hi + 1e-12), tip_sum / (mid_sum + 1e-12)))
    peak_mask = tip_a >= (float(np.mean(tip_a)) * 1.25 + 1e-12)
    if np.any(peak_mask):
        tip_ratio = max(
            tip_ratio,
            float(np.mean(tip_a[peak_mask]) / (float(np.mean(mid_a[peak_mask])) + 1e-12)),
        )
    mu = float(np.mean(tip_a)) + 1e-12
    times = []
    last = 0.0
    dt = hop / float(sr)
    for i, vv in enumerate(tip_a.tolist()):
        if vv > last * 1.28 and vv > mu * 1.35 and vv > 1e-9:
            t = i * dt
            if not times or (t - times[-1]) >= 0.050:
                times.append(t)
        last = vv
    gaps = np.diff(times)
    gaps = gaps[(gaps >= 0.085) & (gaps <= 0.32)]
    hz = float(1.0 / np.median(gaps)) if gaps.size else 0.0
    cv = float(np.std(tip_a) / (np.mean(tip_a) + 1e-12))
    tip_n = float(np.clip(np.log1p(tip_ratio) / np.log1p(3.0), 0.0, 1.0))
    hp = float(np.clip(0.40 * tip_n + 0.30 * min(1.0, cv / 2.2) + 0.20 * 0.3 + 0.10 * 0.5, 0, 1))
    return hz, tip_ratio, hp


def decide(hp: float, tip: float, pulse: float) -> tuple[bool, bool]:
    snap = AudioSnapshot(
        rms=0.1,
        rms_smooth=0.1,
        bass=0.2,
        mid=0.3,
        high=0.3,
        centroid_hz=500.0,
        centroid_norm=0.2,
        flux=0.1,
        flux_smooth=0.1,
        bpm=128.0,
        onset_strength=0.2,
        beat_phasor=0.0,
        steadiness=0.5,
        style=StyleGuess.CLUB,
        techno_score=0.3,
        hiphop_score=0.3,
        club_score=0.5,
        device_sr=16000,
        block_ms=40.0,
        valid=True,
        music_active=True,
        impact_generation=0,
        transient_level=0.0,
        laser_pew_generation=0,
        shake_level=0.3,
        envelope_wobble_hz=pulse,
        shake_generation=0,
        lfo_hz=0.0,
        lfo_depth=0.0,
        synth_pulse_hz=pulse,
        high_point_level=hp,
        synth_tip_ratio=tip,
        led_effect="none",
    )
    pulse_ok = bool(is_high_point_synth_pulse(snap))
    want = bool(pulse_ok and hp >= 0.20 and tip >= 0.75 and 3.4 <= pulse <= 11.5)
    return want, pulse_ok


def summarize(name: str, expect_fire: bool, x: np.ndarray, sr: int) -> bool:
    hz, tip, hp = analyze_tip(x, sr)
    want, pulse_ok = decide(hp, tip, hz)
    ok = want == expect_fire
    mark = "PASS" if ok else "FAIL"
    print(f"\n=== {name} expect_fire={expect_fire} [{mark}] ===")
    print(f"pulse_hz={hz:.2f} tip_ratio={tip:.2f} hp={hp:.3f}")
    print(f"is_pulse={pulse_ok} want_ref_fan={want}")
    return ok


def main() -> None:
    ref = ROOT / "_vid_frames" / "audio.wav"
    x_ref, sr = load_wav(ref)
    results = [
        summarize("REFERENCE clip", True, x_ref, sr),
        summarize("SYNTH pointed", True, synth_pointed(sr), sr),
        summarize("SYNTH pointed+bass bed", True, synth_pointed_with_bass(sr), sr),
        summarize("SYNTH snare roll", False, synth_snare_roll(sr), sr),
        summarize("SYNTH pointed quiet", True, 0.35 * synth_pointed(sr), sr),
    ]
    print("\n==== SUMMARY ====")
    print(f"passed {sum(results)}/{len(results)}")
    raise SystemExit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
