"""Real-time mic → mood/rhythm → RGB/brightness for reactive LED lighting."""

from __future__ import annotations

import colorsys
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable

import numpy as np


SAMPLE_RATE = 16_000
BLOCK_MS = 40
BLOCK_SIZE = SAMPLE_RATE * BLOCK_MS // 1000  # 640 samples


class ReactionMode(str, Enum):
    BALANCED = "バランス"
    PARTY = "パーティー"
    CINEMA = "シネマ"
    CHILL = "チル"


MOOD_LABELS_JA = {
    "silent": "無音",
    "bright": "明るい",
    "warm": "暖かい",
    "calm": "穏やか",
    "dark": "暗い",
    "intense": "激しい",
    "party": "パーティー",
}

STYLE_LABELS_JA = {
    "atmosphere": "雰囲気",
    "impact": "インパクト",
}

GENRE_LABELS_JA = {
    "silent": "無音",
    "bright_pop": "明るいポップ",
    "warm_acoustic": "ウォーム",
    "soft_ballad": "バラード",
    "calm_ambient": "アンビエント",
    "dark_bass": "ダークベース",
    "punchy_bass": "パンチー",
    "house_groove": "ハウス",
    "energetic": "エナジー",
    "edgy": "エッジ",
}

SECTION_LABELS_JA = {
    "silent": "無音",
    "intro": "イントロ",
    "verse": "ヴァース",
    "build": "ビルドアップ",
    "chorus": "サビ",
    "bridge": "ブリッジ",
    "drop": "ドロップ",
    "outro": "アウトロ",
    "hold": "溜め",
}

_BRIGHT_GENRES = frozenset(
    {"bright_pop", "energetic", "warm_acoustic", "house_groove", "soft_ballad"}
)
_DARK_GENRES = frozenset({"dark_bass", "punchy_bass", "edgy"})

EFFECT_LABELS_JA = {
    "none": "",
    "subbass": "重低音",
    "vibrato": "ビブラート",
    "subbass+vibrato": "重低音+ビブラート",
    "tension_hold": "溜め",
    "drop_hit": "解放",
    "accel_blink": "加速点滅",
}


@dataclass
class LightingFrame:
    r: int
    g: int
    b: int
    brightness: float  # 0–1
    mood: str
    level: float  # 0–1 smoothed loudness
    beat: bool
    style: str = "atmosphere"  # atmosphere | impact
    style_score: float = 0.0  # 0=atmosphere … 1=impact
    effect: str = "none"
    genre: str = "calm_ambient"
    section: str = "verse"
    tension: float = 0.0  # 0–1 build-up hold amount
    release: float = 0.0  # 0–1 drop explosion envelope


@dataclass
class _ModeParams:
    beat_flash: float
    beat_hue_jump: float
    bass_bright: float
    min_bright: float
    max_bright: float
    color_smooth: float
    envelope_follow: float
    onset_gate: float  # min onset strength to count as beat (0–1)


# Manual UI bias overlays (blended with auto style)
_MODE_PRESETS: dict[ReactionMode, _ModeParams] = {
    ReactionMode.BALANCED: _ModeParams(0.55, 0.06, 0.70, 0.18, 1.0, 0.35, 0.55, 0.15),
    ReactionMode.PARTY: _ModeParams(0.85, 0.12, 0.90, 0.25, 1.0, 0.55, 0.75, 0.05),
    ReactionMode.CINEMA: _ModeParams(0.28, 0.03, 0.45, 0.12, 0.80, 0.20, 0.35, 0.35),
    ReactionMode.CHILL: _ModeParams(0.18, 0.02, 0.30, 0.14, 0.65, 0.15, 0.25, 0.45),
}

# Auto performance styles (calmer defaults)
_STYLE_ATMOSPHERE = _ModeParams(0.08, 0.008, 0.18, 0.18, 0.68, 0.06, 0.16, 0.65)
_STYLE_IMPACT = _ModeParams(0.55, 0.06, 0.70, 0.16, 0.95, 0.22, 0.45, 0.18)


def _lerp_params(a: _ModeParams, b: _ModeParams, t: float) -> _ModeParams:
    t = float(np.clip(t, 0.0, 1.0))
    return _ModeParams(
        a.beat_flash + (b.beat_flash - a.beat_flash) * t,
        a.beat_hue_jump + (b.beat_hue_jump - a.beat_hue_jump) * t,
        a.bass_bright + (b.bass_bright - a.bass_bright) * t,
        a.min_bright + (b.min_bright - a.min_bright) * t,
        a.max_bright + (b.max_bright - a.max_bright) * t,
        a.color_smooth + (b.color_smooth - a.color_smooth) * t,
        a.envelope_follow + (b.envelope_follow - a.envelope_follow) * t,
        a.onset_gate + (b.onset_gate - a.onset_gate) * t,
    )


class GenreClassifier:
    """Multi-genre music classifier (ignores speech-only; keeps instrumental mids)."""

    def __init__(self) -> None:
        self._onset_times: list[float] = []
        self._bass_hist: list[float] = []
        self._sub_hist: list[float] = []
        self._bright_hist: list[float] = []
        self._flux_hist: list[float] = []
        self._level_hist: list[float] = []
        self._crest_hist: list[float] = []
        self._centroid_hist: list[float] = []
        self._impact = 0.35
        self._brightness = 0.55  # 0 dark … 1 bright song
        self.style = "atmosphere"
        self.genre = "calm_ambient"
        self._last_switch = 0.0
        self._last_genre_switch = 0.0

    def update(self, features: dict, *, bias: float = 0.0) -> tuple[str, float, str, float]:
        """Returns style, impact_score, genre, brightness_score."""
        now = time.monotonic()
        speech = float(features.get("speech", 0.0))
        onset = bool(features.get("onset")) and speech < 0.6
        bass = float(features.get("bass", 0.0))
        sub = float(features.get("sub_bass", 0.0))
        high = float(features.get("high", 0.0))
        presence = float(features.get("presence", 0.0))
        brightness = float(features.get("brightness", 0.5))
        flux = float(features.get("flux", 0.0))
        level = float(features.get("music_level", features.get("level", 0.0)))
        centroid = float(features.get("centroid", 1500.0))
        peak = float(features.get("peak", 0.0))
        rms = max(float(features.get("rms", 0.0)), 1e-6)
        crest = float(np.clip(peak / rms / 8.0, 0.0, 1.0))

        # Speech-only (talking): soften beat-driven impact, keep brightness tracking
        if speech > 0.7:
            onset = False
            # Mild freeze only when clearly talking over silence
            if level < 0.06:
                return self.style, float(self._impact), self.genre, float(self._brightness)

        if onset and level > 0.04:
            self._onset_times.append(now)
        self._onset_times = [t for t in self._onset_times if now - t < 3.0]

        for hist, val in (
            (self._bass_hist, bass),
            (self._sub_hist, sub),
            (self._bright_hist, brightness),
            (self._flux_hist, flux),
            (self._level_hist, level),
            (self._crest_hist, crest),
            (self._centroid_hist, min(centroid / 6000.0, 1.0)),
        ):
            hist.append(val)
            if len(hist) > 90:  # ~3.6s
                del hist[0]

        if len(self._level_hist) < 12:
            return self.style, float(self._impact), self.genre, float(self._brightness)

        onset_rate = len(self._onset_times) / 3.0
        bass_m = float(np.mean(self._bass_hist))
        sub_m = float(np.mean(self._sub_hist))
        bright_m = float(np.mean(self._bright_hist))
        flux_std = float(np.std(self._flux_hist))
        level_m = float(np.mean(self._level_hist))
        level_std = float(np.std(self._level_hist))
        crest_m = float(np.mean(self._crest_hist))
        cent_m = float(np.mean(self._centroid_hist))
        presence_now = presence

        # --- Brightness axis (song feel: dark←→bright) ---
        # Mid/high absolute presence wins over kick fraction.
        has_body = presence_now > 0.10 or bright_m > 0.42
        low_penalty = (sub_m * 0.18 + bass_m * 0.06) * float(
            np.clip(1.0 - presence_now * 2.0 - bright_m * 1.2, 0.0, 1.0)
        )
        bright_raw = float(
            np.clip(
                bright_m * 0.58
                + presence_now * 0.32
                + high * 0.16
                + cent_m * 0.05
                + level_m * 0.04
                - low_penalty,
                0.0,
                1.0,
            )
        )
        # Floor: audible midrange → at least mildly bright
        if has_body:
            bright_raw = max(bright_raw, 0.42 + presence_now * 0.25)
        self._brightness = 0.62 * self._brightness + 0.38 * bright_raw

        # --- Impact axis (calm bed ←→ punchy beats) ---
        impact_raw = (
            float(np.clip(onset_rate / 2.5, 0.0, 1.0)) * 0.30
            + float(np.clip((bass_m + sub_m) * 1.2, 0.0, 1.0)) * 0.28
            + crest_m * 0.24
            + float(np.clip(flux_std * 2.2, 0.0, 1.0)) * 0.12
            + float(np.clip(level_std * 2.8, 0.0, 1.0)) * 0.06
        )
        calm_raw = (
            float(np.clip(1.0 - onset_rate / 1.3, 0.0, 1.0)) * 0.35
            + float(np.clip(1.0 - crest_m, 0.0, 1.0)) * 0.25
            + float(np.clip(1.0 - (bass_m + sub_m), 0.0, 1.0)) * 0.20
            + float(np.clip(1.0 - level_std * 3.5, 0.0, 1.0)) * 0.20
        )
        if level_m < 0.05:
            impact_raw *= 0.25
            calm_raw = max(calm_raw, 0.7)

        score = float(np.clip(0.5 + (impact_raw - calm_raw) * 0.8 + bias * 0.22, 0.0, 1.0))
        self._impact = 0.95 * self._impact + 0.05 * score

        if self.style == "atmosphere" and self._impact > 0.68 and now - self._last_switch > 4.0:
            self.style = "impact"
            self._last_switch = now
        elif self.style == "impact" and self._impact < 0.34 and now - self._last_switch > 4.0:
            self.style = "atmosphere"
            self._last_switch = now

        # --- Genre labels (multi-class from axes) ---
        b = self._brightness
        i = self._impact
        genre = self._pick_genre(
            b, i, bass_m, sub_m, level_m, onset_rate, crest_m, presence_now
        )
        if genre != self.genre:
            # Force switch when bright↔dark axis clearly flipped (avoid sticky wrong label)
            flipped = (self.genre in _BRIGHT_GENRES and genre in _DARK_GENRES and b < 0.38) or (
                self.genre in _DARK_GENRES and genre in _BRIGHT_GENRES and b > 0.52
            ) or (self.genre == "silent") or (genre == "silent" and level_m < 0.04)
            dwell = 0.45 if flipped else 1.4
            if now - self._last_genre_switch > dwell:
                self.genre = genre
                self._last_genre_switch = now

        return self.style, float(self._impact), self.genre, float(self._brightness)

    @staticmethod
    def _pick_genre(
        bright: float,
        impact: float,
        bass: float,
        sub: float,
        level: float,
        onset_rate: float,
        crest: float,
        presence: float = 0.0,
    ) -> str:
        if level < 0.035:
            return "silent"
        heavy_low = (bass + sub) > 0.55 or sub > 0.4
        punchy = impact > 0.52 and (bass + sub) > 0.28 and (crest > 0.28 or onset_rate > 1.4)
        # Midrange body ⇒ not a dark track even with kick
        open_mix = presence > 0.12 or bright >= 0.45

        # Dark only when spectrum is actually dark (no mid body)
        if bright < 0.36 and heavy_low and not open_mix:
            return "punchy_bass" if punchy else "dark_bass"
        # Bright pop / happy (allow moderate kick)
        if bright >= 0.52 and impact < 0.70:
            return "bright_pop"
        if bright >= 0.48 and impact >= 0.60:
            return "energetic"
        # House / four-on-floor groove: mid-bright + steady pulse
        if 0.40 <= bright < 0.62 and impact > 0.48 and onset_rate > 1.5 and not (
            heavy_low and not open_mix
        ):
            return "house_groove"
        # Punchy bass genres (phonk / hip-hop / trap)
        if punchy and bright < 0.48 and not open_mix:
            return "punchy_bass"
        if bright < 0.32 and (sub > 0.22 or bass > 0.28) and not open_mix:
            return "dark_bass"
        if impact > 0.5 and bright >= 0.40 and onset_rate > 1.2:
            return "edgy"
        # Soft ballad: mid brightness, low impact, little sub
        if 0.32 <= bright <= 0.62 and impact < 0.42 and sub < 0.30:
            return "soft_ballad"
        # Warm acoustic — exclude truly dark bass beds
        if 0.28 <= bright <= 0.58 and impact < 0.50 and (not heavy_low or open_mix):
            return "warm_acoustic"
        if impact < 0.38 and (not heavy_low or open_mix):
            return "calm_ambient"
        if bright < 0.36 and not open_mix:
            return "dark_bass"
        if bright >= 0.45:
            return "bright_pop"
        return "warm_acoustic"


# Back-compat alias
StyleClassifier = GenreClassifier


class SectionDetector:
    """Detect intro / verse / chorus(サビ) / bridge / drop / outro from relative energy."""

    def __init__(self) -> None:
        self._level_hist: list[float] = []
        self._bright_hist: list[float] = []
        self._flux_hist: list[float] = []
        self._bass_hist: list[float] = []
        self._onset_hist: list[float] = []
        self._presence_hist: list[float] = []
        self.section = "silent"
        self._last_switch = 0.0
        self._music_started = 0.0
        self._music_frames = 0
        self._frame_i = 0
        self._peak_level = 0.0
        self._baseline = 0.12  # slow bed level (verse/intro reference)
        self._chorus_hits = 0
        self._declining = 0.0
        self._chorus_until = 0.0
        self._build_score = 0.0  # 持続するビルド確信度（一瞬の上昇でサビへ飛ばない）

    def update(self, features: dict, song_bright: float) -> str:
        self._frame_i += 1
        now = self._frame_i * (BLOCK_MS / 1000.0)
        # Prefer pre-AGC level so intro stays quieter than chorus after normalization
        level = float(features.get("raw_level", features.get("music_level", 0.0)))
        rich = float(features.get("richness", 0.0))
        bright = float(features.get("brightness", 0.5))
        flux = float(features.get("flux", 0.0))
        bass = float(features.get("bass", 0.0)) + float(features.get("sub_bass", 0.0))
        presence = float(features.get("presence", 0.0))
        # ロール検知用: micro_onset があれば優先（通常 onset は 0.22s ゲートで疎）
        onset = 1.0 if (features.get("micro_onset") or features.get("onset")) else 0.0
        speech = float(features.get("speech", 0.0))
        music_gate = float(features.get("music_level", level))

        if speech > 0.75 and music_gate < 0.08:
            if now - self._last_switch > 1.5:
                self.section = "silent"
                self._last_switch = now
                self._music_started = 0.0
                self._build_score = 0.0
            return self.section

        for hist, val in (
            (self._level_hist, level),
            (self._bright_hist, bright),
            (self._flux_hist, flux),
            (self._bass_hist, bass),
            (self._onset_hist, onset),
            (self._presence_hist, presence * 0.6 + rich * 0.4),
        ):
            hist.append(val)
            if len(hist) > 250:
                del hist[0]

        if len(self._level_hist) < 15:
            return self.section

        level_m = float(np.mean(self._level_hist[-20:]))
        recent = float(np.mean(self._level_hist[-10:]))
        older = (
            float(np.mean(self._level_hist[-60:-25]))
            if len(self._level_hist) >= 60
            else float(np.mean(self._level_hist[: max(5, len(self._level_hist) // 2)]))
        )
        flux_m = float(np.mean(self._flux_hist[-15:]))
        bass_m = float(np.mean(self._bass_hist[-15:]))
        dense_m = float(np.mean(self._presence_hist[-15:]))
        onset_rate = float(np.mean(self._onset_hist[-30:])) * 25.0
        onset_recent = float(np.mean(self._onset_hist[-12:])) * 25.0
        onset_older = (
            float(np.mean(self._onset_hist[-45:-20])) * 25.0
            if len(self._onset_hist) >= 45
            else onset_recent
        )
        flux_recent = float(np.mean(self._flux_hist[-10:]))
        flux_older = (
            float(np.mean(self._flux_hist[-40:-18]))
            if len(self._flux_hist) >= 40
            else flux_recent
        )
        bright_recent = float(np.mean(self._bright_hist[-10:]))
        bright_older = (
            float(np.mean(self._bright_hist[-40:-18]))
            if len(self._bright_hist) >= 40
            else bright_recent
        )

        if music_gate < 0.03 and level_m < 0.04:
            cand = "silent"
            self._music_started = 0.0
            self._peak_level *= 0.97
            self._baseline = 0.92 * self._baseline + 0.08 * 0.02
            self._build_score = 0.0
        else:
            if self._music_started <= 0.0:
                self._music_started = now
                self._music_frames = 0
            self._music_frames += 1
            music_age = self._music_frames * (BLOCK_MS / 1000.0)
            self._peak_level = max(self._peak_level * 0.997, level_m)

            # 最初の短い時間は baseline 学習。高域ロールが来たら intro に閉じ込めない
            if music_age < 1.2:
                self._baseline = 0.75 * self._baseline + 0.25 * recent
                cand = "intro"
            else:
                # Bed = lower percentile of recent history (resists chorus inflation)
                p30 = float(np.percentile(self._level_hist, 30))
                p60 = float(np.percentile(self._level_hist, 60))
                if self.section in {"chorus", "drop"}:
                    self._baseline = 0.998 * self._baseline + 0.002 * min(p30, self._baseline)
                else:
                    # intro 中も baseline をゆっくり更新
                    blend = 0.12 if music_age < 5.5 else 0.07
                    self._baseline = (1.0 - blend) * self._baseline + blend * p30

                rising = recent > older * 1.14 + 0.025
                mild_rising = recent > older * 1.06 + 0.012
                falling = recent < older * 0.80 and music_age > 14.0
                if falling:
                    self._declining = min(1.0, self._declining + 0.1)
                else:
                    self._declining = max(0.0, self._declining - 0.05)

                vs_base = recent / max(self._baseline, 0.08)
                vs_mid = recent / max(p60, 0.08)
                densifying = (
                    onset_recent > onset_older * 1.15 + 0.04
                    or flux_recent > flux_older * 1.20 + 0.01
                    or bright_recent > bright_older + 0.035
                    or onset_rate >= 5.5  # ~16分ロール帯
                )
                # Breakaway 型: 高域16分ロール自体がビルドシグナル
                hat_roll = onset_rate >= 6.0 and onset_recent >= 4.5
                # まだピーク前の上昇・ロール詰め込み → ビルド
                build_like = (
                    (
                        vs_base > 1.05
                        and recent > self._baseline + 0.02
                        and vs_base < 1.75
                        and (
                            (rising and vs_base > 1.12)
                            or (mild_rising and densifying)
                            or (densifying and (flux_m > 0.04 or onset_rate > 0.8))
                            or (rising and densifying)
                            or hat_roll
                        )
                    )
                    or (hat_roll and music_age > 1.2 and self.section not in {"chorus", "drop"})
                )
                if music_age < 5.5 and not build_like and not densifying and not hat_roll:
                    cand = "intro"
                    # fall through only for early ambient; otherwise evaluate below
                    early_intro = True
                else:
                    early_intro = False
                # 明確なサビ（ビルドより高いバー）
                chorus_like = (
                    vs_base > 1.55
                    and recent > self._baseline + 0.14
                    and dense_m > 0.24
                    and (rich > 0.38 or flux_m > 0.14 or onset_rate > 0.65)
                    and not (build_like and rising and vs_base < 1.62)
                    and not hat_roll
                )
                peak_chorus = vs_base > 1.75 and dense_m > 0.28 and rich > 0.35 and not hat_roll
                drop_like = (
                    bass_m > 0.42 and rising and vs_base > 1.5 and song_bright < 0.55
                )

                if early_intro:
                    cand = "intro"
                    self._build_score = max(0.0, self._build_score - 0.05)
                else:
                    if build_like or hat_roll:
                        self._build_score = min(1.0, self._build_score + (0.20 if hat_roll else 0.14))
                    elif self.section == "build" and (mild_rising or densifying or hat_roll) and vs_base < 1.70:
                        self._build_score = min(1.0, self._build_score + 0.08)
                    else:
                        self._build_score = max(0.0, self._build_score - 0.10)

                    if now < self._chorus_until and self.section in {"chorus", "drop"}:
                        if recent > self._baseline * 1.15 or vs_mid > 1.05:
                            cand = self.section
                        else:
                            cand = "bridge" if self._chorus_hits else "verse"
                    elif (
                        self._chorus_hits > 0
                        and recent < self._peak_level * 0.48
                        and self.section in {"chorus", "drop", "build"}
                    ):
                        cand = "bridge"
                    elif drop_like and self._build_score < 0.35 and not hat_roll:
                        cand = "drop"
                        self._chorus_until = now + 3.5
                        self._chorus_hits += 1
                    # ビルドをサビより先に確定（上昇中に chorus_like で飛ばされるのを防ぐ）
                    elif self._build_score >= 0.35 or (
                        build_like and self.section in {"verse", "bridge", "intro", "build", "hold", "silent"}
                    ):
                        if (chorus_like or peak_chorus) and self._build_score < 0.35 and not rising and not hat_roll:
                            cand = "chorus"
                            self._chorus_until = now + 4.5
                            self._chorus_hits += 1
                            self._build_score = 0.0
                        else:
                            cand = "build"
                    elif chorus_like or peak_chorus:
                        cand = "chorus"
                        self._chorus_until = now + 4.5
                        self._chorus_hits += 1
                        self._build_score = 0.0
                    elif self._declining > 0.5 and falling and music_age > 22.0:
                        cand = "outro"
                    elif (
                        self._chorus_hits > 0
                        and recent < self._baseline * 1.2
                        and music_age > 12.0
                    ):
                        cand = "bridge" if dense_m < 0.28 else "verse"
                    elif music_age < 5.5:
                        cand = "intro"
                    else:
                        cand = "verse"

        if cand != self.section:
            fast = {"chorus", "drop", "build"}
            dwell = 0.55 if cand in fast or self.section in fast else 1.8
            if cand == "build" or self.section == "build":
                dwell = 0.40  # ビルドへ素早く入る／維持しやすい
            if self.section == "silent" or cand == "silent":
                dwell = 0.5
            if now - self._last_switch > dwell:
                self.section = cand
                self._last_switch = now

        return self.section


class TensionDropEngine:
    """Club-style 溜め→解放: suppress while quiet/building, explode on the hit."""

    def __init__(self) -> None:
        self.tension = 0.0
        self.release = 0.0
        self.phase = "idle"  # idle | hold | release
        self._peak = 0.15
        self._level_slow = 0.0
        self._level_fast = 0.0
        self._hold_frames = 0
        self._loud_frames = 0
        self._release_frames = 0
        self._was_loud = False

    def update(self, features: dict, section: str) -> tuple[float, float, str]:
        level = float(features.get("raw_level", features.get("music_level", 0.0)))
        level_n = float(np.clip(level / 2.2, 0.0, 1.0))
        flux = float(features.get("flux", 0.0))
        bass = float(features.get("bass", 0.0)) + float(features.get("sub_bass", 0.0))
        high = float(features.get("high", 0.0))
        onset = bool(features.get("onset"))
        rich = float(features.get("richness", 0.0))
        music = float(features.get("music_level", level_n))

        self._level_fast = 0.50 * self._level_fast + 0.50 * level_n
        self._level_slow = 0.94 * self._level_slow + 0.06 * level_n
        self._peak = max(self._peak * 0.997, self._level_fast)

        if self._level_fast > 0.28 or music > 0.25:
            self._loud_frames += 1
            if self._loud_frames > 10:
                self._was_loud = True
        else:
            self._loud_frames = max(0, self._loud_frames - 1)

        quiet = self._level_fast < max(0.14, self._peak * 0.50)
        very_quiet = self._level_fast < max(0.10, self._peak * 0.38)
        building = section in {"build", "hold"} or (
            quiet and (flux > 0.2 or high > 0.10 or onset or rich > 0.2)
        )

        surge = self._level_fast > self._level_slow * 1.45 + 0.05 and (
            bass > 0.18 or rich > 0.28 or flux > 0.18 or self._level_fast > 0.35
        )
        hard_hit = self._level_fast > self._peak * 0.72 and self._level_fast > 0.30

        # --- Release decay (frame-based ~2.8s) ---
        if self._release_frames > 0:
            self._release_frames -= 1
            self.phase = "release"
            self.release = max(self.release * 0.94, self._release_frames / 70.0)
            self.tension = 0.0
            self._hold_frames = 0
            return float(self.tension), float(self.release), self.phase

        # --- Trigger drop after 溜め ---
        if self._was_loud and self.tension > 0.22 and (surge or hard_hit) and not very_quiet:
            self.phase = "release"
            self.release = 1.0
            self._release_frames = 70
            self.tension = 0.0
            self._hold_frames = 0
            self._peak = max(self._peak, self._level_fast)
            return float(self.tension), float(self.release), self.phase

        if section == "drop" and self.tension > 0.15:
            self.phase = "release"
            self.release = 1.0
            self._release_frames = 55
            self.tension = 0.0
            self._hold_frames = 0
            return float(self.tension), float(self.release), self.phase

        # --- Enter / stay in 溜め ---
        if self._was_loud and (quiet or section == "build"):
            self._hold_frames += 1
            self.phase = "hold"
            rate = 0.018
            if building:
                rate += 0.012
            if very_quiet:
                rate += 0.010
            if section == "build":
                rate += 0.015
            rate += flux * 0.02
            if self._hold_frames > 8:
                self.tension = min(1.0, self.tension + rate)
            self.release = max(0.0, self.release - 0.12)
            return float(self.tension), float(self.release), self.phase

        # --- Soft exit ---
        self._hold_frames = 0
        if self.tension > 0.05:
            self.tension = max(0.0, self.tension - 0.02)
            self.phase = "hold" if self.tension > 0.2 else "idle"
        else:
            self.phase = "idle"
            self.tension = 0.0
            if self._level_fast < 0.05 and music < 0.04:
                self._was_loud = False
                self._peak = max(0.12, self._peak * 0.99)
        self.release = max(0.0, self.release - 0.1)
        return float(self.tension), float(self.release), self.phase


class AudioAnalyzer:
    """AGC + bands + flux/bass onset + sub-bass / vibrato cues."""

    def __init__(self, sample_rate: int = SAMPLE_RATE) -> None:
        self.sample_rate = sample_rate
        self._noise_rms = 0.002
        self._agc = 8.0
        self._energy_slow = 0.0
        self._energy_fast = 0.0
        self._bass_slow = 0.0
        self._bass_fast = 0.0
        self._flux_slow = 0.0
        self._onset_cooldown = 0.0
        # ロール／加速点滅用: 短いクールダウン（ビート用 0.22s だと ~4.5Hz 上限で点滅不能）
        self._micro_onset_cooldown = 0.0
        self._samples_seen = 0
        self._micro_cooldown_until_sample = 0
        self._onset_cooldown_until_sample = 0
        self._prev_spectrum: np.ndarray | None = None
        self._prev_hat_spec: np.ndarray | None = None
        self._hat_flux_slow = 0.0
        self._hat_fast = 0.0
        self._hat_slow = 0.0
        self._prev_raw_rms = 0.0
        self._peak_freq_hist: list[float] = []
        self._sub_ema = 0.0
        self._vibrato_ema = 0.0

    def analyze(self, samples: np.ndarray, sensitivity: float) -> dict:
        if samples.size == 0:
            return self._empty()

        sens = max(0.3, min(3.0, sensitivity))
        raw = samples.astype(np.float64)
        raw_rms = float(np.sqrt(np.mean(raw * raw)) + 1e-12)
        raw_peak = float(np.max(np.abs(raw)))

        if raw_rms < self._noise_rms * 1.8:
            self._noise_rms = 0.98 * self._noise_rms + 0.02 * raw_rms
        desired = 0.12 / max(raw_rms, self._noise_rms * 3.0, 1e-5)
        desired = float(np.clip(desired, 1.0, 40.0))
        self._agc = 0.92 * self._agc + 0.08 * desired

        x = np.tanh(raw * self._agc * sens)
        rms = float(np.sqrt(np.mean(x * x)))
        peak = float(np.max(np.abs(x)))

        window = np.hanning(len(x))
        spectrum = np.abs(np.fft.rfft(x * window))
        power = spectrum * spectrum
        freqs = np.fft.rfftfreq(len(x), d=1.0 / self.sample_rate)
        total = float(np.sum(power)) + 1e-12

        # Bands: keep instrumental mids for brightness; speech = vocals without music bed
        sub_p = float(np.sum(power[(freqs >= 25) & (freqs < 80)]))
        bass_p = float(np.sum(power[(freqs >= 30) & (freqs < 150)]))
        low_inst_p = float(np.sum(power[(freqs >= 80) & (freqs < 280)]))
        # Musical presence (guitars/keys/synths/vocals) — include lower mids for warm songs
        presence_p = float(np.sum(power[(freqs >= 350) & (freqs < 4500)]))
        # Narrower speech core
        speech_core_p = float(np.sum(power[(freqs >= 300) & (freqs < 3000)]))
        high_p = float(np.sum(power[(freqs >= 4500) & (freqs < 8000)]))
        sparkle_p = float(np.sum(power[(freqs >= 2000) & (freqs < 6000)]))
        low_mid_p = float(np.sum(power[(freqs >= 150) & (freqs < 500)]))
        mid_p = float(np.sum(power[(freqs >= 500) & (freqs < 2500)]))

        music_bed_p = sub_p + bass_p + low_inst_p + high_p + sparkle_p * 0.5 + 1e-12
        # Speech-only gate: mid-band chatter without kick/hats. Soft instruments keep presence.
        upper_air_p = float(np.sum(power[(freqs >= 3500) & (freqs < 8000)]))
        non_voice_p = bass_p + sub_p + high_p + sparkle_p + upper_air_p + 1e-12
        voice_vs_music = float(speech_core_p / (speech_core_p + non_voice_p))
        bed_frac = float((bass_p + sub_p + high_p + sparkle_p) / total)
        # Spectral flatness in voice band ≈ noise-like speech; tonal music has sharp peaks
        voice_band = (freqs >= 300) & (freqs < 3000)
        vb_power = power[voice_band] + 1e-18
        log_mean = float(np.mean(np.log(vb_power)))
        flatness = float(np.exp(log_mean) / (np.mean(vb_power) + 1e-18))
        speech = 0.0
        if (
            voice_vs_music > 0.88
            and bed_frac < 0.10
            and flatness > 0.35
            and upper_air_p / total < 0.05
            and bass_p / total < 0.08
        ):
            speech = float(np.clip(voice_vs_music * 0.5 + flatness * 0.5, 0.0, 1.0))
        voice_ratio = speech

        sub_bass = sub_p / total
        bass = bass_p / total
        low_mid = low_mid_p / total
        mid = mid_p / total
        high = high_p / total
        presence = presence_p / total
        centroid = float(np.sum(freqs * power) / total)
        rolloff_idx = int(np.searchsorted(np.cumsum(power), 0.85 * total))
        rolloff = float(freqs[min(rolloff_idx, len(freqs) - 1)])
        timbre_bright = float(sparkle_p / total)

        sub_abs = float(np.clip(np.sqrt(sub_p) * 0.015, 0.0, 1.0))
        sub_raw = float(np.clip(sub_bass * 2.2 + sub_abs * 0.8, 0.0, 1.0))
        if rms < 0.03:
            sub_raw *= 0.4
        if speech > 0.65:
            sub_raw *= 0.25
        self._sub_ema = 0.90 * self._sub_ema + 0.10 * sub_raw

        # Flux: mild attenuation of speech core only when speech-like
        music_spec = spectrum.copy()
        if speech > 0.55:
            music_spec[(freqs >= 300) & (freqs < 3000)] *= 0.2
        else:
            music_spec[(freqs >= 300) & (freqs < 3000)] *= 0.55  # keep instruments
        flux = 0.0
        if self._prev_spectrum is not None and self._prev_spectrum.shape == music_spec.shape:
            diff = music_spec - self._prev_spectrum
            flux = float(np.sqrt(np.mean(np.maximum(diff, 0.0) ** 2)))
        self._prev_spectrum = music_spec

        if speech < 0.55:
            vibrato = self._estimate_vibrato(freqs, power, high + presence * 0.3)
        else:
            self._vibrato_ema *= 0.85
            vibrato = float(self._vibrato_ema)

        # Music level includes presence (bright songs need this)
        music_p = music_bed_p + presence_p * 0.65
        music_rms = float(np.sqrt(music_p / max(len(spectrum), 1)) * 0.08)
        music_energy = music_rms * music_rms
        self._energy_fast = 0.50 * self._energy_fast + 0.50 * music_energy
        self._energy_slow = 0.92 * self._energy_slow + 0.08 * music_energy
        self._bass_fast = 0.45 * self._bass_fast + 0.55 * (bass_p + sub_p)
        self._bass_slow = 0.90 * self._bass_slow + 0.10 * (bass_p + sub_p)
        self._flux_slow = 0.85 * self._flux_slow + 0.15 * flux

        # スネア・ビルドアップ（Breakaway: ~128BPM 16分 ≈ 8.3Hz, gap≈120ms）
        # クラック帯 0.9–5kHz（キック/シンバル誤検知を避ける）
        snare_mask = (freqs >= 900.0) & (freqs < 5000.0)
        kick_mask = (freqs >= 30.0) & (freqs < 180.0)
        cym_mask = (freqs >= 5500.0) & (freqs < 8000.0)
        snare_energy = float(np.sum(power[snare_mask]) + 1e-12)
        kick_energy = float(np.sum(power[kick_mask]) + 1e-12)
        cym_energy = float(np.sum(power[cym_mask]) + 1e-12)
        snare_dom = snare_energy / (snare_energy + kick_energy * 0.85 + cym_energy * 0.9 + 1e-12)
        hat_spec = spectrum[snare_mask]
        hat_flux = 0.0
        if self._prev_hat_spec is not None and self._prev_hat_spec.shape == hat_spec.shape:
            d = np.maximum(hat_spec - self._prev_hat_spec, 0.0)
            hat_flux = float(np.sqrt(np.mean(d * d)))
        self._prev_hat_spec = hat_spec.copy()
        self._hat_flux_slow = 0.72 * self._hat_flux_slow + 0.28 * hat_flux
        self._hat_fast = 0.55 * self._hat_fast + 0.45 * snare_energy
        self._hat_slow = 0.92 * self._hat_slow + 0.08 * snare_energy

        # スネア帯のみでサブブロック peak（広帯域 diff は誤検知が多い）
        sub_n = 4
        sub_len = max(16, len(raw) // sub_n)
        sub_scores: list[float] = []
        for si in range(sub_n):
            seg = raw[si * sub_len : (si + 1) * sub_len]
            if seg.size < 16:
                continue
            win = seg * np.hanning(seg.size)
            sp = np.abs(np.fft.rfft(win))
            # rfft 周波数はブロック長基準なのでマスクを再計算
            sf = np.fft.rfftfreq(seg.size, d=1.0 / self.sample_rate)
            sm = (sf >= 900.0) & (sf < 5000.0)
            e = float(np.sqrt(np.sum((sp[sm] ** 2)) + 1e-12))
            pk = float(np.max(sp[sm])) if np.any(sm) else 0.0
            sub_scores.append(pk * 0.55 + e * 0.45)
        hat_sub_hit = False
        if len(sub_scores) >= 2 and snare_dom > 0.08:
            arr = np.asarray(sub_scores, dtype=np.float64)
            local = float(np.max(arr))
            bed = float(np.median(arr))
            hat_sub_hit = local > bed * 1.12 + 1e-6
        hat_flux_hit = hat_flux > self._hat_flux_slow * 1.25 + 1e-6 and snare_dom > 0.12
        hat_energy_hit = self._hat_fast > self._hat_slow * 1.15 + 1e-9 and snare_dom > 0.14
        hat_hit = hat_sub_hit or (hat_flux_hit and hat_energy_hit)

        raw_jump = raw_rms > self._prev_raw_rms * 1.75 + 0.003 and speech < 0.55
        energy_hit = self._energy_fast > self._energy_slow * 1.38 + 1e-7
        bass_hit = self._bass_fast > self._bass_slow * 1.42 + 1e-9
        flux_hit = flux > self._flux_slow * 1.65 + 1e-5
        self._samples_seen += int(raw.size)
        now_samp = self._samples_seen
        onset = False
        micro_onset = False
        music_hit = bass_hit or (energy_hit and speech < 0.55) or (flux_hit and speech < 0.6)
        hit_ok = False
        if music_hit or raw_jump:
            if speech < 0.7 or (bass_p + sub_p + high_p) > speech_core_p * 0.4:
                if raw_rms > self._noise_rms * 2.5 or raw_peak > 0.02:
                    hit_ok = True
        if hit_ok and now_samp >= self._onset_cooldown_until_sample:
            onset = True
            # Longer cooldown reduces strobe from dense micro-onsets (beat flash)
            self._onset_cooldown_until_sample = now_samp + int(0.22 * self.sample_rate)
        soft_flux = flux > self._flux_slow * 1.22 + 1e-6
        soft_energy = self._energy_fast > self._energy_slow * 1.15 + 1e-7
        soft_raw = raw_rms > self._prev_raw_rms * 1.28 + 0.0012
        # ロール用 micro: スネア帯。マイク劣化でも拾えるよう緩く
        micro_floor = raw_rms > self._noise_rms * 1.05 or raw_peak > 0.004 or hat_sub_hit
        micro_hit = (
            speech < 0.70
            and micro_floor
            and snare_dom > 0.08
            and (
                hat_sub_hit
                or hat_flux_hit
                or hat_energy_hit
                or (hat_hit and soft_flux)
                or (soft_flux and snare_dom > 0.16 and soft_raw)
            )
        )
        if micro_hit and now_samp >= self._micro_cooldown_until_sample:
            micro_onset = True
            # 終盤の32分(~60ms@128BPM)も拾う。最大 ~20Hz
            self._micro_cooldown_until_sample = now_samp + int(0.048 * self.sample_rate)
        self._prev_raw_rms = 0.7 * self._prev_raw_rms + 0.3 * raw_rms
        # 互換: 古い monotonic クーダウンも更新（未使用）
        now = time.monotonic()
        self._onset_cooldown = now
        self._micro_onset_cooldown = now

        level = float(np.clip((rms - 0.015) / 0.32, 0.0, 1.0))
        # Pre-AGC dynamics with headroom (>1 allowed) for intro vs chorus contrast
        raw_level = float(np.clip(raw_rms / 0.045, 0.0, 4.0))
        # Robust music loudness from bed + presence
        music_level = float(
            np.clip(0.55 * level + 0.45 * np.tanh(np.sqrt(music_p) * 0.0008), 0.0, 1.0)
        )
        if speech > 0.75:
            music_level *= 0.15
            raw_level *= 0.15
            onset = False
            micro_onset = False
        # Spectral richness (how many bands are active) — choruses fill the spectrum
        band_powers = np.array(
            [sub_p, bass_p, low_inst_p, low_mid_p, mid_p, presence_p, sparkle_p, high_p],
            dtype=np.float64,
        )
        band_norm = band_powers / (np.max(band_powers) + 1e-12)
        richness = float(np.mean(band_norm > 0.12))
        # Brightness = how "open/cheerful" the spectrum feels.
        # Use absolute mid/high energy so a loud kick cannot mark a bright song as dark.
        sparkle_frac = float(sparkle_p / total)
        presence_abs = float(np.clip(np.tanh(np.sqrt(presence_p) * 0.012), 0.0, 1.0))
        sparkle_abs = float(np.clip(np.tanh(np.sqrt(sparkle_p + high_p) * 0.014), 0.0, 1.0))
        mid_high = presence + high + sparkle_frac + low_mid * 0.35
        low_mass = sub_bass * 1.0 + bass * 0.55
        balance = float(mid_high / (mid_high + low_mass + 0.08))
        # Only penalize lows when mids/highs are truly absent
        low_drag = low_mass * 0.12 * float(np.clip(1.0 - presence_abs * 1.8 - sparkle_abs, 0.0, 1.0))
        brightness = float(
            np.clip(
                presence_abs * 0.42
                + sparkle_abs * 0.28
                + balance * 0.28
                + sparkle_frac * 0.12
                + high * 0.10
                + (rolloff / 8000.0) * 0.08
                - low_drag,
                0.0,
                1.0,
            )
        )
        flux_n = float(np.clip(flux * 6.0, 0.0, 1.0))
        return {
            "rms": rms,
            "peak": peak,
            "level": level,
            "music_level": music_level,
            "raw_level": raw_level,
            "richness": richness,
            "voice": voice_ratio,
            "speech": speech,
            "bass": bass,
            "sub_bass": float(self._sub_ema),
            "low_mid": low_mid,
            "mid": mid,
            "high": high,
            "presence": presence,
            "brightness": brightness,
            "centroid": centroid,
            "timbre_bright": timbre_bright,
            "flux": flux_n,
            "vibrato": float(vibrato),
            "onset": onset,
            "micro_onset": micro_onset,
            "block_sec": float(raw.size) / float(self.sample_rate),
            "snare_dom": float(snare_dom),
        }

    def _estimate_vibrato(self, freqs: np.ndarray, power: np.ndarray, mid_presence: float) -> float:
        """Instrumental pitch wobble only (exclude typical speech/singing band)."""
        # Prefer upper instrumental range; skip 280–3500 voice core
        band = (freqs >= 3500) & (freqs < 7000)
        if not np.any(band) or mid_presence < 0.05:
            self._vibrato_ema *= 0.9
            return float(self._vibrato_ema)

        band_power = power[band]
        band_freqs = freqs[band]
        peak_idx = int(np.argmax(band_power))
        peak_f = float(band_freqs[peak_idx])
        peak_strength = float(band_power[peak_idx] / (np.sum(band_power) + 1e-12))

        self._peak_freq_hist.append(peak_f)
        if len(self._peak_freq_hist) > 28:  # ~1.1s
            del self._peak_freq_hist[0]

        vibrato_raw = 0.0
        if len(self._peak_freq_hist) >= 16 and peak_strength > 0.12:
            hist = np.asarray(self._peak_freq_hist, dtype=np.float64)
            # Detrend
            hist = hist - np.mean(hist)
            std = float(np.std(hist))
            # Depth in Hz relative to mean pitch — vibrato often 20–80 cents ≈ few–tens Hz
            depth = float(np.clip(std / 18.0, 0.0, 1.0))
            # Periodicity via autocorrelation at lag ~4–8 Hz (block=40ms → lag 3–6)
            if std > 2.5:
                best = 0.0
                for lag in (3, 4, 5, 6):
                    if lag >= len(hist):
                        continue
                    a = hist[:-lag]
                    b = hist[lag:]
                    denom = float(np.sqrt(np.sum(a * a) * np.sum(b * b)) + 1e-9)
                    corr = float(np.dot(a, b) / denom)
                    best = max(best, corr)
                if best > 0.25:
                    vibrato_raw = float(np.clip(depth * (0.4 + 0.6 * best) * mid_presence * 1.5, 0.0, 1.0))

        self._vibrato_ema = 0.92 * self._vibrato_ema + 0.08 * vibrato_raw
        return float(self._vibrato_ema)

    @staticmethod
    def _empty() -> dict:
        return {
            "rms": 0.0,
            "peak": 0.0,
            "level": 0.0,
            "music_level": 0.0,
            "raw_level": 0.0,
            "richness": 0.0,
            "voice": 0.0,
            "speech": 0.0,
            "bass": 0.0,
            "sub_bass": 0.0,
            "low_mid": 0.0,
            "mid": 0.0,
            "high": 0.0,
            "presence": 0.0,
            "brightness": 0.5,
            "centroid": 0.0,
            "timbre_bright": 0.0,
            "flux": 0.0,
            "vibrato": 0.0,
            "onset": False,
            "micro_onset": False,
            "block_sec": BLOCK_MS / 1000.0,
            "snare_dom": 0.0,
        }


class MoodRhythmMapper:
    """Genre-aware lighting: bright songs → warm/gold; dark/punchy → indigo/crimson."""

    _BAD_LO = 0.14
    _BAD_HI = 0.64

    def __init__(self) -> None:
        self.mode = ReactionMode.BALANCED
        self.classifier = GenreClassifier()
        self.sections = SectionDetector()
        self.tension_engine = TensionDropEngine()
        self._hue = 0.08
        self._sat = 0.75
        self._val = 0.7
        self._bright = 0.45
        self._level_s = 0.0
        self._level_fast = 0.0
        self._flash = 0.0
        self._warmth = 0.65
        self._cheer = 0.55
        self._drive = 0.3
        self._mood = "silent"
        self._style = "atmosphere"
        self._impact = 0.35
        self._song_bright = 0.55
        self._genre = "calm_ambient"
        self._section = "verse"
        self._display_section = "verse"
        self._section_hold_until = 0.0
        self._tension = 0.0
        self._release = 0.0
        self._tension_phase = "idle"
        self._phase = 0.0
        self._sub_glow = 0.0
        self._vib_amt = 0.0
        self._effect = "none"
        self._effect_until = 0.0
        self._display_style = "atmosphere"
        self._style_hold_until = 0.0
        self._display_genre = "calm_ambient"
        self._genre_hold_until = 0.0
        # 加速ビート／ロール点滅（onset 間隔の短縮＝加速を実測）
        self._roll_onset_times: list[float] = []
        self._roll_hz = 0.0
        self._roll_phase = 0.0
        self._roll_active = 0.0  # 0–1 エンベロープ
        self._accel_score = 0.0  # 0–1 加速確信度
        self._accel_until = 0.0
        self._audio_t = 0.0
        self._roll_last_t = 0.0
        self._roll_gap_hist: list[float] = []
        self._blink_latch_until = 0.0  # 一度発火したら欠落でも点滅を維持
        self._roll_miss_grace = 0.0
        self._roll_end_streak = 0  # 終了判定の連続フレーム（フリッカー防止）
        self._blink_sticky_until = 0.0  # 点滅開始後はスネア停止まで粘る

    def set_mode(self, mode: ReactionMode | str) -> None:
        if isinstance(mode, str):
            for m in ReactionMode:
                if m.value == mode or m.name.lower() == mode.lower():
                    self.mode = m
                    return
            self.mode = ReactionMode.BALANCED
        else:
            self.mode = mode

    def _ui_bias(self) -> float:
        if self.mode == ReactionMode.PARTY:
            return 0.55
        if self.mode == ReactionMode.CHILL:
            return -0.55
        if self.mode == ReactionMode.CINEMA:
            return -0.35
        return 0.0

    def _effective_params(self) -> _ModeParams:
        auto = _lerp_params(_STYLE_ATMOSPHERE, _STYLE_IMPACT, self._impact)
        ui = _MODE_PRESETS[self.mode]
        return _lerp_params(auto, ui, 0.25)

    def map(self, features: dict) -> LightingFrame:
        self._style, self._impact, self._genre, self._song_bright = self.classifier.update(
            features, bias=self._ui_bias()
        )
        self._section = self.sections.update(features, self._song_bright)
        self._tension, self._release, self._tension_phase = self.tension_engine.update(
            features, self._section
        )
        # Surface 溜め / 解放 immediately (club timing matters)
        if self._tension_phase == "hold" and self._tension > 0.35:
            self._section = "hold"
            self._display_section = "hold"
        elif self._tension_phase == "release" and self._release > 0.45:
            self._section = "drop"
            self._display_section = "drop"
        p = self._effective_params()

        level = float(features.get("music_level", features["level"]))
        speech = float(features.get("speech", features.get("voice", 0.0)))
        bass = float(features["bass"])
        sub_bass = float(features.get("sub_bass", 0.0))
        high = float(features["high"])
        presence = float(features.get("presence", 0.0))
        feat_bright = float(features.get("brightness", 0.5))
        timbre_bright = float(features.get("timbre_bright", 0.0))
        flux = float(features.get("flux", 0.0))
        vibrato = float(features.get("vibrato", 0.0))
        onset = bool(features["onset"])
        centroid = float(features.get("centroid", 1000.0))

        if speech > 0.7:
            level *= 0.15
            onset = False
            vibrato = 0.0
            flux *= 0.2

        self._level_fast = 0.55 * self._level_fast + 0.45 * level
        self._level_s = 0.85 * self._level_s + 0.15 * level
        self._sub_glow = 0.88 * self._sub_glow + 0.12 * sub_bass
        self._vib_amt = 0.90 * self._vib_amt + 0.10 * vibrato
        self._phase = (self._phase + 0.02 + self._vib_amt * 0.04) % 1.0

        onset_strength = 0.0
        if onset:
            onset_strength = float(
                np.clip(0.35 + bass * 0.4 + sub_bass * 0.3 + high * 0.2 + flux * 0.25, 0.0, 1.0)
            )
        beat = onset and self._level_fast > 0.04 and onset_strength >= p.onset_gate
        if self._style == "atmosphere" and beat and onset_strength < 0.55:
            beat = False
        if speech > 0.65:
            beat = False


        # --- 加速点滅: スネア・ビルドアップの16分ロール（Breakaway≈8.3Hz） ---
        # セクション確定を待たず、スネア onset を常時バッファしてからゲートする
        block_sec = float(features.get("block_sec", BLOCK_MS / 1000.0) or (BLOCK_MS / 1000.0))
        block_sec = float(np.clip(block_sec, 0.01, 0.12))
        self._audio_t += block_sec
        now_roll = self._audio_t
        dt_roll = max(1e-3, min(0.12, now_roll - self._roll_last_t))
        self._roll_last_t = now_roll
        build_pressure = float(getattr(self.sections, "_build_score", 0.0) or 0.0)
        latched = now_roll < self._blink_latch_until
        micro_onset = bool(features.get("micro_onset", False))

        # 常時バッファ（ビルド前のスネアロールを捨てない）
        if speech < 0.70 and self._level_fast > 0.006 and micro_onset:
            if not self._roll_onset_times or (now_roll - self._roll_onset_times[-1]) >= 0.040:
                self._roll_onset_times.append(now_roll)
                self._roll_miss_grace = now_roll + 0.55
        self._roll_onset_times = [t for t in self._roll_onset_times if now_roll - t < 2.8]
        n_on = len(self._roll_onset_times)
        added = bool(micro_onset and n_on > 0 and abs(self._roll_onset_times[-1] - now_roll) < 1e-6)

        accelerating = False
        densifying = False
        dense_roll = False
        steady_16th = False
        rate_hz = 0.0
        regular = False
        if n_on >= 3:
            recent_n = sum(1 for t in self._roll_onset_times if now_roll - t <= 1.00)
            older_n = sum(1 for t in self._roll_onset_times if 1.00 < now_roll - t <= 2.00)
            rate_hz = recent_n / 1.00
            older_hz = older_n / 1.00
            if rate_hz >= 3.5:
                self._roll_hz = float(np.clip(0.55 * self._roll_hz + 0.45 * rate_hz, 2.0, 28.0))
            dense_roll = rate_hz >= 5.5 and recent_n >= 4
            rate_up = rate_hz >= older_hz * 1.12 + 0.5 and rate_hz >= 5.0 and older_n >= 2
            in_snare_band = 5.5 <= rate_hz <= 20.0

            gaps = np.diff(np.asarray(self._roll_onset_times[-16:], dtype=np.float64))
            gaps = gaps[(gaps >= 0.038) & (gaps <= 0.34)]
            if gaps.size >= 2:
                mid = max(1, gaps.size // 2)
                early_g = gaps[:mid]
                late_g = gaps[mid:] if gaps.size - mid >= 1 else gaps[-1:]
                early = float(np.median(early_g))
                late = float(np.median(late_g))
                gap_hz = float(np.clip(1.0 / max(1e-3, late), 2.0, 28.0))
                self._roll_hz = float(np.clip(0.4 * self._roll_hz + 0.6 * gap_hz, 2.0, 28.0))
                med_g = float(np.median(gaps))
                pause_reject = float(np.max(gaps)) > max(med_g * 2.5, med_g + 0.16)
                gap_cv = float(np.std(gaps) / max(1e-3, med_g))
                regular = gap_cv < 0.55 and not pause_reject
                early_ok = 0.060 <= early <= 0.28
                late_ok = 0.060 <= late <= 0.24
                ratio = early / max(1e-3, late)
                abs_gain = early - late
                if gaps.size >= 3:
                    xs = np.arange(gaps.size, dtype=np.float64)
                    slope = float(np.polyfit(xs, gaps, 1)[0])
                else:
                    slope = -abs_gain
                late_cv = float(np.std(late_g) / max(1e-3, late)) if late_g.size else 1.0
                mono = float(np.mean(late_g)) <= float(np.mean(early_g)) * 0.97
                strong = (
                    early_ok and late_ok and regular and late < early
                    and ratio >= 1.08 and abs_gain >= 0.008 and slope < -0.001
                )
                mild = (
                    early_ok and late_ok and regular and late <= early * 0.98
                    and ratio >= 1.04 and (slope <= 0.0 or mono) and late_cv < 0.65
                )
                # 16〜32分の定常／加速ロール
                steady_16th = (
                    regular
                    and in_snare_band
                    and 0.048 <= late <= 0.160
                    and 0.048 <= early <= 0.180
                )
                accelerating = strong or mild or rate_up
                densifying = steady_16th or (
                    regular and early_ok and late_ok and late <= 0.160 and rate_hz >= 5.5
                )
                self._roll_gap_hist = gaps.tolist()[-10:]
            elif dense_roll and in_snare_band:
                densifying = True
                accelerating = rate_up

        last_onset_age = (
            (now_roll - self._roll_onset_times[-1]) if self._roll_onset_times else 9.0
        )
        # 序盤の早期発火: 3発以上で16〜32分間隔なら即ロール扱い
        early_16th = False
        if n_on >= 3 and last_onset_age < 0.22:
            eg = np.diff(np.asarray(self._roll_onset_times[-6:], dtype=np.float64))
            eg = eg[(eg >= 0.045) & (eg <= 0.180)]
            if eg.size >= 2:
                emed = float(np.median(eg))
                ecv = float(np.std(eg) / max(1e-3, emed))
                if ecv < 0.60:
                    early_16th = True
                    self._roll_hz = float(
                        np.clip(max(self._roll_hz, 1.0 / max(1e-3, emed)), 5.0, 22.0)
                    )
        # 本物の16分ロールのみ（通常ビート/ハット連打は除外）
        snare_roll_live = (
            dense_roll
            or steady_16th
            or early_16th
            or (accelerating and rate_hz >= 5.5 and regular)
        ) if n_on >= 3 else False

        # ロール終了: ドロップは強い解放のみ。点滅中は欠落に強く、無ヒットが続いたら終了
        recent_short = sum(1 for t in self._roll_onset_times if now_roll - t <= 0.55)
        recent_short_hz = recent_short / 0.55
        sticky = now_roll < self._blink_sticky_until
        hard_end = (
            (self._section == "outro")
            or (self._tension_phase == "release" and self._release > 0.72 and last_onset_age > 0.25)
            or (self._section == "drop" and self._release > 0.60 and last_onset_age > 0.30)
        )
        if sticky or self._roll_active > 0.35 or self._accel_score > 0.45:
            # 点滅中: 約0.5s無ヒットかつ低密度のみ終了（途中切れ防止）
            soft_end = last_onset_age > 0.52 and recent_short_hz < 2.2
        else:
            soft_end = (
                last_onset_age > 0.32
                or (recent_short_hz < 2.8 and last_onset_age > 0.22)
            )
        if hard_end:
            self._roll_end_streak = 12
        elif soft_end:
            self._roll_end_streak = min(16, self._roll_end_streak + 1)
        else:
            self._roll_end_streak = max(0, self._roll_end_streak - 3)
        roll_ended = hard_end or self._roll_end_streak >= 10
        if roll_ended:
            snare_roll_live = False
            latched = False
            self._blink_latch_until = 0.0
            self._roll_miss_grace = 0.0
            self._blink_sticky_until = 0.0

        # 開始用ゲート: セクション誤判定だけでは点滅しない
        in_buildup = (
            self._section in {"build", "hold"}
            or (self._tension_phase == "hold" and self._tension > 0.32)
            or (build_pressure >= 0.42 and self._section in {"intro", "verse", "bridge", "build", "hold"})
            or snare_roll_live
            or latched
            or sticky
        )

        if hard_end:
            in_buildup = False
            snare_roll_live = False
            latched = False
            sticky = False
            self._blink_latch_until = 0.0
            self._blink_sticky_until = 0.0

        # 開始は加速/高密度16分のみ。sticky単独では開始しない
        true_accel = accelerating or densifying or dense_roll or early_16th or steady_16th
        enter_blink = (not roll_ended) and true_accel and n_on >= 4 and self._roll_hz >= 6.0 and (
            snare_roll_live or accelerating
        ) and (
            in_buildup
            or build_pressure >= 0.30
            or rate_hz >= 6.5
        )

        if roll_ended or (not in_buildup and not latched and not sticky):
            decay_a = 0.40 if roll_ended else 0.25
            decay_r = 0.45 if roll_ended else 0.28
            self._accel_score = max(0.0, self._accel_score - decay_a)
            self._accel_until = 0.0
            self._roll_active = max(0.0, self._roll_active - decay_r)
            self._roll_hz *= 0.85 if roll_ended else 0.92
            if roll_ended:
                self._blink_latch_until = 0.0
                self._blink_sticky_until = 0.0
        elif enter_blink:
            boost = 0.75 if accelerating or early_16th else 0.50
            self._accel_score = min(1.0, max(self._accel_score, 0.55) + boost)
            self._roll_active = min(1.0, max(self._roll_active, 0.35) + 0.55)
            self._accel_until = now_roll + 0.90
            self._blink_latch_until = max(self._blink_latch_until, now_roll + 0.75)
            self._blink_sticky_until = max(self._blink_sticky_until, now_roll + 0.95)
            self._roll_end_streak = 0
        elif (latched or sticky) and (added or snare_roll_live or last_onset_age < 0.40):
            # 点滅中は欠落に強く維持（開始条件は緩くしない）
            self._accel_score = max(self._accel_score, 0.55)
            self._accel_until = max(self._accel_until, now_roll + 0.70)
            self._roll_active = max(self._roll_active, 0.42)
            if added or last_onset_age < 0.28:
                self._blink_latch_until = max(self._blink_latch_until, now_roll + 0.70)
                self._blink_sticky_until = max(self._blink_sticky_until, now_roll + 0.85)
            self._roll_end_streak = max(0, self._roll_end_streak - 3)
        elif (latched or sticky) and last_onset_age > 0.48:
            self._blink_latch_until = min(self._blink_latch_until, now_roll + 0.12)
            self._accel_score = max(0.0, self._accel_score - 0.08)
            self._roll_active = max(0.0, self._roll_active - 0.10)
        elif added and in_buildup:
            self._accel_score = max(0.0, self._accel_score - 0.04)
        elif now_roll > self._accel_until:
            self._accel_score = max(0.0, self._accel_score - 0.08)

        latched = now_roll < self._blink_latch_until and not roll_ended
        sticky = now_roll < self._blink_sticky_until and not roll_ended
        accel_latched = (in_buildup or latched or sticky) and self._accel_score >= 0.35 and (
            now_roll < self._accel_until or self._accel_score >= 0.50 or latched or sticky
        )
        want_roll_blink = (
            (not roll_ended)
            and accel_latched
            and self._roll_hz >= 5.5
            and (enter_blink or latched or sticky)
            and (enter_blink or snare_roll_live or last_onset_age < 0.48)
        )
        if want_roll_blink:
            self._roll_active = min(1.0, self._roll_active + 0.50)
            if added or last_onset_age < 0.30:
                self._blink_sticky_until = max(self._blink_sticky_until, now_roll + 0.80)
        else:
            decay = 0.18 if roll_ended else (0.05 if (latched or sticky) else 0.12)
            self._roll_active = max(0.0, self._roll_active - decay)

        if self._roll_active > 0.16 and self._roll_hz >= 5.5 and (latched or sticky or enter_blink) and not roll_ended:
            # 実測ギャップ優先（テンポ同期）。粘着時の強制 6.5Hz 床は外す
            gap_hz_live = 0.0
            if n_on >= 3:
                g_live = np.diff(np.asarray(self._roll_onset_times[-8:], dtype=np.float64))
                g_live = g_live[(g_live >= 0.040) & (g_live <= 0.22)]
                if g_live.size >= 1:
                    gap_hz_live = float(1.0 / max(1e-3, float(np.median(g_live))))
            if gap_hz_live >= 4.5:
                self._roll_hz = float(np.clip(0.35 * self._roll_hz + 0.65 * gap_hz_live, 4.5, 20.0))
            hz_use = float(np.clip(self._roll_hz if self._roll_hz >= 4.5 else 8.0, 4.5, 20.0))
            # スネア onset に位相ロック（点滅＝ヒットタイミング）
            if added:
                self._roll_phase = 0.0
            else:
                self._roll_phase = min(0.999, self._roll_phase + dt_roll * hz_use)
        else:
            self._roll_phase *= 0.88

        # Soft / atmospheric genres: no strobe — only punchy styles get flash
        soft_genre = self._genre in {
            "warm_acoustic",
            "soft_ballad",
            "calm_ambient",
            "bright_pop",
            "silent",
        }
        # 溜め中はフラッシュ禁止、解放時は強制許可（加速ロール点滅は例外）
        in_hold = self._tension_phase == "hold" and self._tension > 0.25
        in_release = self._release > 0.35
        section_flash = self._section in {"chorus", "drop"} and self._song_bright < 0.55
        allow_flash = in_release or (
            not in_hold
            and (not soft_genre or section_flash)
            and (
                self._style == "impact"
                or self._genre in {"punchy_bass", "energetic", "edgy", "house_groove"}
                or self._impact > 0.58
                or self._section == "drop"
            )
        )
        roll_blink = (
            (not roll_ended)
            and (enter_blink or latched or sticky)
            and self._roll_active > 0.18
            and self._accel_score >= 0.35
            and self._roll_hz >= 5.5
            and (enter_blink or snare_roll_live or last_onset_age < 0.50)
        )
        if roll_blink:
            allow_flash = True
            in_hold = False  # 加速ビート中は消灯ホールドより点滅を優先
        if in_hold and not roll_blink:
            beat = False
            allow_flash = False
        elif soft_genre and self._impact < 0.62 and self._section not in {"chorus", "drop"} and not in_release and not roll_blink:
            beat = False
            allow_flash = False
        elif self._style == "atmosphere" and self._impact < 0.50 and self._section not in {
            "chorus",
            "drop",
        } and not in_release and not roll_blink:
            if onset_strength < 0.70:
                beat = False
            allow_flash = False

        chorus_lift = self._section == "chorus" and soft_genre

        if in_release:
            # Explosive hit — force a strong flash envelope
            self._flash = min(1.0, max(self._flash, 0.75 + self._release * 0.25))
            tip = 0.02 if self._song_bright > 0.45 else 0.95
            self._hue = self._blend_hue_safe(self._hue, tip, 0.45 * self._release)
            self._val = min(1.0, self._val + 0.22 * self._release)
            beat = True
        elif beat and allow_flash:
            self._flash = min(1.0, self._flash + (0.50 if self._style == "impact" else 0.18))
            jump = p.beat_hue_jump
            if self._style == "impact" or self._genre in {"punchy_bass", "energetic", "edgy"}:
                tip = 0.95 if (sub_bass > 0.35 or bass > 0.3) else 0.06
                self._hue = self._blend_hue_safe(self._hue, tip, 0.28)
                self._val = min(1.0, self._val + 0.10)
            elif self._genre == "house_groove":
                self._val = min(1.0, self._val + 0.06)
            else:
                tip = self._hue - jump if self._warmth >= 0.45 else self._hue + jump
                self._hue = self._blend_hue_safe(self._hue, self._clamp_hue(tip), 0.12)
                self._val = min(1.0, self._val + 0.03)
        else:
            decay = 0.22 if self._style == "impact" else 0.16
            if soft_genre or in_hold:
                decay = 0.30
            self._flash = max(0.0, self._flash - decay)
            if not allow_flash:
                self._flash *= 0.85
            if in_hold:
                self._flash = 0.0


        cheer_raw = float(
            np.clip(
                self._song_bright * 0.48
                + feat_bright * 0.34
                + presence * 0.28
                + timbre_bright * 0.16
                + high * 0.12
                + self._level_s * 0.08
                - sub_bass * 0.06 * float(np.clip(1.0 - presence * 2.0, 0.0, 1.0))
                - speech * 0.15,
                0.0,
                1.0,
            )
        )
        warmth_raw = float(
            np.clip(
                0.30
                + cheer_raw * 0.58
                + presence * 0.18
                + self._level_s * 0.08
                - sub_bass * 0.08 * float(np.clip(1.0 - presence * 2.0, 0.0, 1.0))
                - (0.10 if centroid < 700 and bass > 0.45 and presence < 0.10 else 0.0),
                0.0,
                1.0,
            )
        )
        # Presence floor: audible midrange ⇒ warm/bright, never force dark
        if presence > 0.12 or feat_bright > 0.45:
            cheer_raw = max(cheer_raw, 0.55)
            warmth_raw = max(warmth_raw, 0.55)
        if self._song_bright > 0.48:
            cheer_raw = max(cheer_raw, 0.62)
            warmth_raw = max(warmth_raw, 0.60)
        elif self._song_bright < 0.28 and presence < 0.08:
            cheer_raw = min(cheer_raw, 0.30)
            warmth_raw = min(warmth_raw, 0.32)

        if self._genre == "bright_pop" or (self._song_bright > 0.50 and presence > 0.10):
            cheer_raw = max(cheer_raw, 0.70)
            warmth_raw = max(warmth_raw, 0.68)
        elif self._genre in {"dark_bass", "punchy_bass"} and self._song_bright < 0.40 and presence < 0.10:
            cheer_raw = min(cheer_raw, 0.30)
            warmth_raw = min(warmth_raw, 0.32)
        elif self._genre in {"warm_acoustic", "soft_ballad"}:
            warmth_raw = max(warmth_raw, 0.60)
        elif self._genre == "house_groove":
            cheer_raw = max(cheer_raw, 0.50)
        elif self._genre == "punchy_bass" and self._song_bright >= 0.45:
            warmth_raw = min(warmth_raw, 0.50)

        drive_raw = float(
            np.clip(self._level_fast * 0.4 + bass * 0.25 + sub_bass * 0.25 + flux * 0.25, 0.0, 1.0)
        )
        self._cheer = 0.88 * self._cheer + 0.12 * cheer_raw
        self._warmth = 0.88 * self._warmth + 0.12 * warmth_raw
        self._drive = 0.88 * self._drive + 0.12 * drive_raw
        self._mood = self._label_mood()

        target_hue, target_sat, target_val = self._genre_color()
        target_hue, target_sat, target_val = self._apply_section_color(
            target_hue, target_sat, target_val
        )
        target_hue, target_sat, target_val = self._apply_tension_color(
            target_hue, target_sat, target_val
        )
        target_hue, target_sat, target_val, effect = self._apply_special_effects(
            target_hue, target_sat, target_val
        )
        # Tension / release override effect label
        if roll_blink:
            effect = "accel_blink"
        elif in_release and self._release > 0.4:
            effect = "drop_hit"
        elif in_hold and self._tension > 0.35:
            effect = "tension_hold"

        if (self._style == "impact" and self._flash > 0.55) or in_release:
            a = 0.08 if in_release else 0.05
        else:
            a = min(p.color_smooth, 0.16) if not beat else min(0.26, p.color_smooth + 0.1)
        if in_hold:
            a = min(a, 0.10)
        self._hue = self._blend_hue_safe(self._hue, target_hue, a * 0.75)
        self._sat = (1 - a) * self._sat + a * target_sat
        self._val = (1 - a) * self._val + a * target_val

        env = min(p.envelope_follow, 0.35)
        flash_amt = self._flash if allow_flash or in_release else 0.0
        if self._style == "atmosphere" or soft_genre:
            env_bright = p.min_bright + self._level_s * (p.max_bright - p.min_bright)
            env_bright += self._cheer * 0.10
            env_bright += flash_amt * p.beat_flash * 0.08
        else:
            bed = p.min_bright * 0.85
            env_bright = bed + self._level_fast * (p.max_bright - bed) * 0.5
            env_bright += bass * p.bass_bright * self._level_fast * 0.25
            env_bright += flash_amt * p.beat_flash * 0.55
        # Section dynamics
        sec = self._section
        if sec == "intro":
            env_bright *= 0.78
        elif sec == "verse":
            env_bright *= 0.92
        elif sec == "build":
            env_bright *= 0.70  # 溜め寄り：まだ抑えめ
        elif sec == "hold":
            env_bright *= 0.42
        elif sec == "chorus":
            env_bright *= 1.18 if self._song_bright >= 0.45 else 1.12
            if chorus_lift:
                env_bright += 0.06
        elif sec == "drop":
            env_bright *= 1.22
        elif sec == "bridge":
            env_bright *= 0.88
        elif sec == "outro":
            env_bright *= 0.72
        # Club tension / release envelope
        if in_hold:
            # Dim hard; tiny shimmer as tension rises (anticipation)
            env_bright *= 0.38 + 0.12 * (1.0 - self._tension)
            env_bright += self._tension * 0.04 * (0.5 + 0.5 * np.sin(self._phase * 4 * np.pi))
        if in_release:
            env_bright = max(env_bright, 0.55) + self._release * 0.45
            env_bright += flash_amt * 0.35
        # Sub / vibrato: only when clearly present; keep subtle (not a flash)
        if self._sub_glow > 0.55 and not soft_genre and not in_hold:
            rumble = 0.5 + 0.5 * np.sin(self._phase * 2 * np.pi * 0.35)
            env_bright += self._sub_glow * (0.06 + 0.08 * rumble)
        if self._vib_amt > 0.48 and not soft_genre and not in_hold:
            shimmer = 0.5 + 0.5 * np.sin(self._phase * 2 * np.pi * 0.7)
            env_bright += self._vib_amt * 0.03 * shimmer
        env_bright += self._cheer * 0.08
        env_bright = float(np.clip(env_bright, 0.06 if in_hold else 0.10, min(1.0, p.max_bright + (0.15 if in_release else 0.0))))
        # Snap brightness on release; lag more during hold
        env_use = 0.55 if in_release else (0.12 if in_hold else env)
        self._bright = (1 - env_use) * self._bright + env_use * env_bright

        # 加速ビートに合わせたハード点滅（スネア onset 位相ロック）
        if roll_blink:
            # ヒット直後をオン。周期の約28〜38%（速いほど短く）
            hz_blink = float(np.clip(self._roll_hz if self._roll_hz >= 4.5 else 8.0, 4.5, 20.0))
            duty = float(np.clip(0.34 * (8.5 / hz_blink), 0.22, 0.40))
            # 位相が 1 に張り付いた欠落中はオフ（次の onset 待ち）
            on = self._roll_phase < duty
            self._bright = 1.0 if on else 0.04
            self._val = min(1.0, max(self._val, 0.85 if on else 0.25))
            beat = bool(on)
            if on:
                self._flash = max(self._flash, 0.65)

        r, g, b = colorsys.hsv_to_rgb(self._hue, self._sat, self._val)
        r, g, b = self._discourage_green(int(r * 255), int(g * 255), int(b * 255))

        now = time.monotonic()
        if self._style != self._display_style and now >= self._style_hold_until:
            self._display_style = self._style
            self._style_hold_until = now + 3.5
        if self._genre != self._display_genre:
            axis_flip = (self._display_genre in _BRIGHT_GENRES and self._genre in _DARK_GENRES) or (
                self._display_genre in _DARK_GENRES and self._genre in _BRIGHT_GENRES
            )
            bright_clear = self._song_bright > 0.50 or self._song_bright < 0.32
            hold_ok = now >= self._genre_hold_until or axis_flip or bright_clear
            if hold_ok:
                self._display_genre = self._genre
                self._genre_hold_until = now + (0.9 if axis_flip or bright_clear else 2.0)
        if self._section != self._display_section and now >= self._section_hold_until:
            self._display_section = self._section
            # Hold/drop switch faster for club feel
            self._section_hold_until = now + (
                0.45 if self._section in {"hold", "drop", "build"} else 1.2
            )

        return LightingFrame(
            r=r,
            g=g,
            b=b,
            brightness=float(self._bright),
            mood=self._mood,
            level=float(self._level_fast),
            beat=beat,
            style=self._display_style,
            style_score=float(self._impact),
            effect=effect,
            genre=self._display_genre,
            section=self._display_section,
            tension=float(self._tension),
            release=float(self._release),
        )

    def _apply_tension_color(
        self, hue: float, sat: float, val: float
    ) -> tuple[float, float, float]:
        """溜め: darken/desaturate; 解放: punchy bright hit."""
        if self._tension_phase == "hold" and self._tension > 0.2:
            t = self._tension
            # Sink into deep indigo / near-black
            hue = self._blend_hue_safe(hue, 0.72, 0.25 + t * 0.35)
            sat = float(np.clip(sat * (0.55 - t * 0.2) + t * 0.15, 0.25, 1.0))
            val = float(np.clip(val * (0.45 - t * 0.25), 0.12, 0.55))
        elif self._release > 0.25:
            r = self._release
            tip = 0.04 if self._song_bright > 0.45 else 0.95
            hue = self._blend_hue_safe(hue, tip, 0.35 + r * 0.4)
            sat = float(np.clip(sat * 0.7 + 0.35 * r, 0.5, 1.0))
            val = float(np.clip(val * 0.6 + 0.55 * r, 0.4, 1.0))
        return (
            self._clamp_hue(hue),
            float(np.clip(sat, 0.2, 1.0)),
            float(np.clip(val, 0.1, 1.0)),
        )

    def _apply_section_color(
        self, hue: float, sat: float, val: float
    ) -> tuple[float, float, float]:
        """Shift color/intensity by song section (intro→chorus→outro)."""
        sec = self._section
        bright = self._song_bright
        if sec == "intro":
            sat *= 0.82
            val *= 0.85
            if bright > 0.45:
                hue = self._blend_hue_safe(hue, 0.07, 0.15)
            else:
                hue = self._blend_hue_safe(hue, 0.72, 0.12)
        elif sec == "build":
            sat *= 0.75
            val *= 0.65
            hue = self._blend_hue_safe(hue, 0.72, 0.2)
        elif sec == "hold":
            sat *= 0.55
            val *= 0.40
            hue = self._blend_hue_safe(hue, 0.75, 0.35)
        elif sec == "chorus":
            sat = min(1.0, sat * 1.12)
            val = min(1.0, val * 1.14)
            if bright >= 0.45:
                hue = self._blend_hue_safe(hue, 0.04, 0.22)  # warmer gold
            else:
                hue = self._blend_hue_safe(hue, 0.92, 0.18)  # crimson lift
        elif sec == "drop":
            sat = min(1.0, sat * 1.20)
            val = min(1.0, val * 1.18)
            hue = self._blend_hue_safe(hue, 0.95 if bright < 0.5 else 0.02, 0.32)
        elif sec == "bridge":
            sat *= 0.90
            val *= 0.92
        elif sec == "outro":
            sat *= 0.75
            val *= 0.78
        return (
            self._clamp_hue(hue),
            float(np.clip(sat, 0.35, 1.0)),
            float(np.clip(val, 0.28, 1.0)),
        )

    def _genre_color(self) -> tuple[float, float, float]:
        g = self._genre
        c = self._cheer
        # Flash only boosts punchy genres; soft genres ignore residual flash
        flash = (
            self._flash
            if g in {"punchy_bass", "energetic", "edgy", "house_groove"}
            else self._flash * 0.15
        )
        if g == "silent" or self._level_s < 0.03:
            return 0.07, 0.45, 0.32
        if g == "bright_pop":
            hue = 0.04 + c * 0.06
            sat = 0.72 + 0.15 * c
            val = 0.72 + 0.20 * c  # no flash strobe
        elif g == "warm_acoustic":
            hue = 0.06 + c * 0.03
            sat = 0.55 + 0.15 * c
            val = 0.58 + 0.18 * c
        elif g == "soft_ballad":
            hue = 0.05 + c * 0.04
            sat = 0.48 + 0.12 * c
            val = 0.52 + 0.16 * c
        elif g == "calm_ambient":
            if self._song_bright > 0.45:
                hue, sat, val = 0.08, 0.45, 0.55
            else:
                hue, sat, val = 0.72, 0.50, 0.45
        elif g == "house_groove":
            hue = 0.03 + 0.03 * flash
            sat = 0.78
            val = 0.58 + 0.18 * flash
        elif g == "dark_bass":
            hue = 0.74 + (1.0 - c) * 0.05
            sat = 0.70
            val = 0.38 + 0.08 * flash
        elif g == "punchy_bass":
            hue = 0.92 if flash < 0.4 else 0.02
            sat = 0.85
            val = 0.45 + 0.32 * flash
        elif g == "energetic":
            hue = 0.02 + c * 0.05
            sat = 0.80
            val = 0.65 + 0.16 * flash
        elif g == "edgy":
            hue = 0.95 if self._song_bright > 0.45 else 0.78
            sat = 0.82
            val = 0.55 + 0.20 * flash
        else:
            # Fallback: follow measured song brightness, not a cold default
            if self._song_bright > 0.5:
                hue, sat, val = 0.06, 0.68, 0.62
            elif self._song_bright < 0.35:
                hue, sat, val = 0.74, 0.65, 0.42
            else:
                hue = 0.06 if self._warmth > 0.5 else 0.72
                sat = 0.65
                val = 0.55
        # Override color cast if genre lags behind song brightness
        if self._song_bright > 0.60 and g in _DARK_GENRES:
            hue = 0.05
            sat = max(sat, 0.70)
            val = max(val, 0.65)
        elif self._song_bright < 0.28 and g in _BRIGHT_GENRES:
            hue = 0.75
            sat = max(sat, 0.65)
            val = min(val, 0.48)
        if self.mode == ReactionMode.CHILL:
            sat *= 0.8
            val *= 0.88
        elif self.mode == ReactionMode.PARTY:
            sat = min(1.0, sat + 0.08)
            val = min(1.0, val + 0.08)
        elif self.mode == ReactionMode.CINEMA:
            sat *= 0.88
            val *= 0.85
        return (
            self._clamp_hue(hue),
            float(np.clip(sat, 0.4, 1.0)),
            float(np.clip(val, 0.3, 1.0)),
        )

    def _apply_special_effects(
        self, hue: float, sat: float, val: float
    ) -> tuple[float, float, float, str]:
        sub = self._sub_glow
        vib = self._vib_amt
        now = time.monotonic()
        soft = self._genre in {"warm_acoustic", "soft_ballad", "calm_ambient", "bright_pop", "silent"}
        raw_effect = "none"
        # Soft genres: no pulsing special effects
        if soft:
            if now >= self._effect_until:
                self._effect = "none"
            return hue, sat, val, self._effect
        if sub > 0.58:
            deep = self._blend_hue_safe(
                hue, 0.78 if self._warmth < 0.5 else 0.95, 0.18 + sub * 0.22
            )
            hue = deep
            sat = min(1.0, sat + sub * 0.10)
            breath = 0.5 + 0.5 * np.sin(self._phase * 2 * np.pi * 0.3)
            val = float(np.clip(val * (0.90 + 0.12 * sub) + breath * sub * 0.04, 0.28, 1.0))
            raw_effect = "subbass"
        if vib > 0.50:
            wobble = np.sin(self._phase * 2 * np.pi) * vib * 0.015
            hue = self._clamp_hue((hue + wobble) % 1.0)
            sat = float(np.clip(sat - vib * 0.03, 0.4, 1.0))
            val = float(np.clip(val + vib * 0.02 * np.sin(self._phase * 3 * np.pi), 0.3, 1.0))
            raw_effect = "subbass+vibrato" if raw_effect == "subbass" else "vibrato"
        if raw_effect != "none":
            self._effect = raw_effect
            self._effect_until = now + 2.5
        elif now >= self._effect_until:
            self._effect = "none"
        return hue, sat, val, self._effect

    def _label_mood(self) -> str:
        if self._level_s < 0.03:
            return "silent"
        if self._song_bright > 0.48 or self._genre == "bright_pop" or self._cheer > 0.55:
            return "bright"
        if (
            self._song_bright < 0.28
            and self._cheer < 0.35
            and self._genre in _DARK_GENRES
        ) or (self._cheer < 0.26 and self._warmth < 0.35):
            return "dark"
        if self._genre in {"punchy_bass", "energetic", "house_groove"} and self._drive > 0.4:
            return "party"
        if self._section == "chorus" and self._cheer > 0.4:
            return "bright" if self._song_bright >= 0.4 else "intense"
        if self._drive > 0.7 and self._cheer > 0.35:
            return "intense"
        if self._warmth > 0.52 or self._genre in {"warm_acoustic", "soft_ballad"}:
            return "warm"
        return "calm"

    def _clamp_hue(self, hue: float) -> float:
        h = hue % 1.0
        if self._BAD_LO < h < self._BAD_HI:
            return 0.08 if h < 0.39 else 0.72
        return h

    def _blend_hue_safe(self, a: float, b: float, t: float) -> float:
        a, b = a % 1.0, b % 1.0
        dh = b - a
        if dh > 0.5:
            dh -= 1.0
        elif dh < -0.5:
            dh += 1.0
        for sign in (1.0, -1.0):
            dtry = dh if sign > 0 else (dh - 1.0 if dh > 0 else dh + 1.0)
            ok = True
            for s in (0.25, 0.5, 0.75):
                h = (a + s * dtry) % 1.0
                if self._BAD_LO < h < self._BAD_HI:
                    ok = False
                    break
            if ok:
                return self._clamp_hue((a + t * dtry) % 1.0)
        return self._clamp_hue(b)

    @staticmethod
    def _discourage_green(r: int, g: int, b: int) -> tuple[int, int, int]:
        if g > r + 25 and g > b + 10:
            return min(255, r + 80), max(0, g - 60), max(0, b - 20)
        if g > 40 and b > 40 and r < g - 15 and r < b - 15:
            return min(255, r + 40), max(0, g - 70), min(255, b + 30)
        return r, g, b


@dataclass
class InputDevice:
    index: int | None
    name: str
    channels: int
    is_default: bool = False

    @property
    def label(self) -> str:
        tag = " [既定]" if self.is_default else ""
        return f"{self.name}{tag}"


def list_input_devices() -> list[InputDevice]:
    try:
        import sounddevice as sd
    except ImportError as exc:
        raise RuntimeError(
            "sounddevice が未インストールです。pip install sounddevice numpy"
        ) from exc

    devices: list[InputDevice] = []
    try:
        default = sd.default.device
        default_idx = default[0] if isinstance(default, (list, tuple)) else default
    except Exception:
        default_idx = None

    devices.append(InputDevice(index=None, name="システム既定", channels=1, is_default=True))

    try:
        hostapis = sd.query_hostapis()
    except Exception:
        hostapis = []

    for i, info in enumerate(sd.query_devices()):
        max_in = int(info.get("max_input_channels", 0) or 0)
        if max_in <= 0:
            continue
        name = str(info.get("name") or f"Device {i}")
        api_idx = info.get("hostapi")
        if isinstance(api_idx, int) and 0 <= api_idx < len(hostapis):
            api_name = str(hostapis[api_idx].get("name") or "")
            if api_name and api_name not in name:
                name = f"{name} ({api_name})"
        devices.append(
            InputDevice(index=i, name=name, channels=max_in, is_default=(i == default_idx))
        )
    return devices


class ReactiveLightingEngine:
    def __init__(
        self,
        on_frame: Callable[[LightingFrame], None],
        *,
        sample_rate: int = SAMPLE_RATE,
        block_size: int = BLOCK_SIZE,
        device: int | None = None,
    ) -> None:
        self.on_frame = on_frame
        self.sample_rate = sample_rate
        self.block_size = block_size
        self.device = device
        self.analyzer = AudioAnalyzer(sample_rate)
        self.mapper = MoodRhythmMapper()
        self.sensitivity = 1.0
        self._stream = None
        self._running = False
        self._error: str | None = None

    @property
    def running(self) -> bool:
        return self._running

    @property
    def last_error(self) -> str | None:
        return self._error

    def set_sensitivity(self, value: float) -> None:
        self.sensitivity = max(0.2, min(3.0, float(value)))

    def set_mode(self, mode: ReactionMode | str) -> None:
        self.mapper.set_mode(mode)

    def start(self) -> None:
        if self._running:
            return
        try:
            import sounddevice as sd
        except ImportError as exc:
            self._error = "sounddevice が未インストールです。pip install sounddevice numpy"
            raise RuntimeError(self._error) from exc

        self._error = None
        self._running = True

        def _callback(indata, frames, time_info, status):  # noqa: ARG001
            if not self._running:
                return
            mono = indata[:, 0] if indata.ndim > 1 else indata
            samples = np.copy(mono)
            features = self.analyzer.analyze(samples, self.sensitivity)
            frame = self.mapper.map(features)
            try:
                self.on_frame(frame)
            except Exception:
                pass

        kwargs: dict = {
            "samplerate": self.sample_rate,
            "channels": 1,
            "dtype": "float32",
            "blocksize": self.block_size,
            "callback": _callback,
        }
        if self.device is not None:
            kwargs["device"] = self.device

        try:
            self._stream = sd.InputStream(**kwargs)
            self._stream.start()
        except Exception as exc:
            self._running = False
            self._stream = None
            self._error = str(exc)
            raise

    def stop(self) -> None:
        self._running = False
        stream = self._stream
        self._stream = None
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:
                pass
