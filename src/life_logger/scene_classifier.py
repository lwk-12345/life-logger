"""Acoustic Scene Classification (ASC) and Sound Event Detection (SED) for Life Logger.

Classifies continuous or windowed audio into lifestyle contexts:
- Scenes: office, transit, dining, outdoor_street, home_quiet, general
- Sound Events: typing, transit_rumble, bgm, babble, clatter, quiet, laughter, cough
- Emotion & Paralinguistic cues: neutral, happy, stressed, calm
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence


SCENE_CLASSES = (
    "office",
    "transit",
    "dining",
    "outdoor_street",
    "home_quiet",
    "general",
)

SOUND_EVENT_CLASSES = (
    "typing",
    "transit_rumble",
    "bgm",
    "babble",
    "clatter",
    "quiet",
    "laughter",
    "cough",
)

EMOTION_CLASSES = ("neutral", "happy", "stressed", "calm")


@dataclass(frozen=True)
class SceneClassificationResult:
    primary_scene: str
    scene_confidence: float
    scene_probabilities: dict[str, float]
    sound_events: list[str]
    detected_tags: list[str]
    emotion: str
    energy_rms: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "primary_scene": self.primary_scene,
            "scene_confidence": round(self.scene_confidence, 4),
            "scene_probabilities": {
                k: round(v, 4) for k, v in self.scene_probabilities.items()
            },
            "sound_events": list(self.sound_events),
            "detected_tags": list(self.detected_tags),
            "emotion": self.emotion,
            "energy_rms": round(self.energy_rms, 6),
        }


def extract_acoustic_tags(text_or_tags: str | Sequence[str]) -> tuple[list[str], str]:
    """Parse SenseVoice or rich ASR tags like <|HAPPY|>, <|BGM|>, <|LAUGHTER|>, <|SIGH|>."""
    if isinstance(text_or_tags, str):
        found_tags = re.findall(r"<\|[A-Z_]+\|>", text_or_tags)
    else:
        found_tags = [str(t) for t in text_or_tags if str(t).startswith("<|")]

    emotion = "neutral"
    upper_joined = " ".join(found_tags).upper()
    if "<|HAPPY|>" in upper_joined or "<|LAUGHTER|>" in upper_joined:
        emotion = "happy"
    elif "<|ANGRY|>" in upper_joined or "<|SAD|>" in upper_joined or "<|CRY|>" in upper_joined:
        emotion = "stressed"
    elif "<|NEUTRAL|>" in upper_joined:
        emotion = "neutral"

    return found_tags, emotion


def classify_scene_from_features(
    *,
    rms: float,
    zcr: float,
    flatness: float,
    tonality: float,
    acoustic_tags: Sequence[str] | None = None,
    ambient_location: str | None = None,
) -> SceneClassificationResult:
    """Infer scene and sound events from signal dynamics, rich tags, and network hints."""
    tags = list(acoustic_tags or [])
    tag_set = {t.upper() for t in tags}
    sound_events: list[str] = []

    # 1. Base acoustic events from signal properties
    if rms < 0.003:
        sound_events.append("quiet")
    if flatness > 0.45 and zcr > 0.15:
        sound_events.append("typing")
    if tonality > 0.35 and rms > 0.01:
        sound_events.append("bgm")
    if rms > 0.02 and 0.05 <= zcr <= 0.16 and flatness < 0.38:
        sound_events.append("babble")
    if zcr < 0.06 and rms > 0.012 and flatness > 0.30:
        sound_events.append("transit_rumble")

    # 2. Rich paralinguistic tags incorporation
    if "<|BGM|>" in tag_set and "bgm" not in sound_events:
        sound_events.append("bgm")
    if "<|LAUGHTER|>" in tag_set:
        sound_events.append("laughter")
    if "<|COUGH|>" in tag_set:
        sound_events.append("cough")

    # 3. Scene probability scoring
    scores: dict[str, float] = {k: 0.10 for k in SCENE_CLASSES}

    # Signal evidence
    if "quiet" in sound_events:
        scores["home_quiet"] += 0.60
    if "typing" in sound_events:
        scores["office"] += 0.55
    if "transit_rumble" in sound_events:
        scores["transit"] += 0.65
    if "bgm" in sound_events:
        scores["dining"] += 0.35
        scores["home_quiet"] += 0.20
    if "babble" in sound_events:
        scores["office"] += 0.25
        scores["dining"] += 0.40
        scores["outdoor_street"] += 0.20

    # Ambient location context hints
    if ambient_location:
        loc = ambient_location.lower()
        if "home" in loc or "家" in loc:
            scores["home_quiet"] += 0.50
        elif "work" in loc or "office" in loc or "公司" in loc:
            scores["office"] += 0.50
        elif "transit" in loc or "metro" in loc or "subway" in loc or "地铁" in loc:
            scores["transit"] += 0.60

    # Normalize
    total = sum(scores.values()) or 1.0
    probs = {k: scores[k] / total for k in SCENE_CLASSES}
    primary = max(SCENE_CLASSES, key=lambda k: probs[k])
    confidence = probs[primary]

    # Emotion mapping
    _, emotion = extract_acoustic_tags(tags)
    if "laughter" in sound_events:
        emotion = "happy"
    elif "quiet" in sound_events and emotion == "neutral":
        emotion = "calm"

    return SceneClassificationResult(
        primary_scene=primary,
        scene_confidence=confidence,
        scene_probabilities=probs,
        sound_events=sound_events,
        detected_tags=tags,
        emotion=emotion,
        energy_rms=rms,
    )


def classify_audio_scene(
    samples: Any,
    *,
    sample_rate: int = 16_000,
    acoustic_tags: Sequence[str] | None = None,
    ambient_location: str | None = None,
) -> SceneClassificationResult:
    """Classify audio waveform using acoustic features."""
    try:
        import numpy as np
    except ImportError:
        # Fallback to pure python baseline if numpy not present
        return classify_scene_from_features(
            rms=0.01,
            zcr=0.08,
            flatness=0.25,
            tonality=0.15,
            acoustic_tags=acoustic_tags,
            ambient_location=ambient_location,
        )

    waveform = np.asarray(samples, dtype=np.float32).reshape(-1)
    if waveform.size == 0:
        return classify_scene_from_features(
            rms=0.0,
            zcr=0.0,
            flatness=1.0,
            tonality=0.0,
            acoustic_tags=acoustic_tags,
            ambient_location=ambient_location,
        )

    rms = float(np.sqrt(np.mean(np.square(waveform), dtype=np.float64)))
    signs = waveform >= 0
    zcr = float(np.mean(signs[1:] != signs[:-1])) if waveform.size > 1 else 0.0

    # Spectral flatness & tonality
    frame_len = min(1024, waveform.size)
    frame = waveform[:frame_len] * np.hanning(frame_len)
    power = np.abs(np.fft.rfft(frame)) ** 2 + 1e-12
    mean_power = float(np.mean(power))
    flatness = float(np.exp(np.mean(np.log(power))) / max(mean_power, 1e-12))
    sorted_power = np.sort(power)
    top_bins = sorted_power[-max(1, min(8, sorted_power.size)) :]
    tonality = float(np.sum(top_bins) / max(float(np.sum(power)), 1e-12))

    return classify_scene_from_features(
        rms=rms,
        zcr=zcr,
        flatness=flatness,
        tonality=tonality,
        acoustic_tags=acoustic_tags,
        ambient_location=ambient_location,
    )
