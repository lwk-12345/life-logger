"""Unit tests for Acoustic Scene Classification and Sound Event Detection."""

from __future__ import annotations

from life_logger.scene_classifier import (
    classify_audio_scene,
    classify_scene_from_features,
    extract_acoustic_tags,
)


def test_extract_acoustic_tags() -> None:
    tags, emotion = extract_acoustic_tags("今天好开心啊<|HAPPY|><|LAUGHTER|>")
    assert "<|HAPPY|>" in tags
    assert "<|LAUGHTER|>" in tags
    assert emotion == "happy"

    tags2, emotion2 = extract_acoustic_tags("<|ANGRY|>生气了")
    assert emotion2 == "stressed"

    tags3, emotion3 = extract_acoustic_tags("正常说话")
    assert tags3 == []
    assert emotion3 == "neutral"


def test_classify_scene_from_features_office() -> None:
    res = classify_scene_from_features(
        rms=0.02,
        zcr=0.18,
        flatness=0.55,
        tonality=0.10,
        acoustic_tags=["<|NEUTRAL|>"],
    )
    assert res.primary_scene == "office"
    assert "typing" in res.sound_events


def test_classify_scene_from_features_transit() -> None:
    res = classify_scene_from_features(
        rms=0.03,
        zcr=0.04,
        flatness=0.35,
        tonality=0.10,
    )
    assert res.primary_scene == "transit"
    assert "transit_rumble" in res.sound_events


def test_classify_scene_from_features_quiet() -> None:
    res = classify_scene_from_features(
        rms=0.001,
        zcr=0.01,
        flatness=0.10,
        tonality=0.0,
    )
    assert res.primary_scene == "home_quiet"
    assert "quiet" in res.sound_events
    assert res.emotion == "calm"


def test_classify_audio_scene_empty_or_zeros() -> None:
    res = classify_audio_scene([])
    assert res.primary_scene in ("home_quiet", "general")
