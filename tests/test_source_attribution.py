from math import isfinite

import pytest

from life_logger.context_source_scoring import apply_context_source_scoring
from life_logger.source_attribution import (
    AttributionConfig,
    apply_multi_evidence_policy,
    build_attribution_record,
    classify_acoustic_proximity,
    classify_similarity,
    cosine_similarity,
    decisions_for_embeddings,
    normalize_audio_gain,
    partition_attributions,
    protect_short_low_similarity,
    reclassify_attribution_record,
    should_extract_life_events,
    unknown_decision,
)


def test_high_similarity_is_classified_as_self() -> None:
    decision = classify_similarity(0.62)

    assert decision.label == "self"
    assert decision.is_review_required is False


def test_low_similarity_is_classified_as_non_self() -> None:
    decision = classify_similarity(0.12)

    assert decision.label == "non_self"
    assert decision.is_review_required is False


def test_middle_similarity_is_held_for_manual_review() -> None:
    decision = classify_similarity(0.37)

    assert decision.label == "unknown"
    assert decision.is_review_required is True
    assert decision.reason == "similarity_in_review_band"


def test_short_audio_can_be_held_for_review_without_a_similarity_score() -> None:
    decision = unknown_decision("segment_too_short")

    assert decision.label == "unknown"
    assert decision.score is None
    assert decision.is_review_required is True
    assert decision.reason == "segment_too_short"


def test_ambiguous_short_low_similarity_is_held_for_review() -> None:
    decision = protect_short_low_similarity(
        classify_similarity(0.127535),
        duration_seconds=2.58,
    )

    assert decision.label == "unknown"
    assert decision.score == pytest.approx(0.127535)
    assert decision.is_review_required is True
    assert decision.reason == "short_segment_low_similarity_review"


def test_strongly_non_self_short_segment_stays_non_self() -> None:
    decision = protect_short_low_similarity(
        classify_similarity(0.039223),
        duration_seconds=1.14,
    )

    assert decision.label == "non_self"
    assert decision.is_review_required is False


def test_low_similarity_long_segment_stays_non_self() -> None:
    decision = protect_short_low_similarity(
        classify_similarity(0.127535),
        duration_seconds=6.0,
    )

    assert decision.label == "non_self"


def test_existing_attribution_can_be_reclassified_without_audio() -> None:
    record = {
        "segment_id": "seg_purchase",
        "start_offset_seconds": 3247.79,
        "end_offset_seconds": 3250.37,
        "text": "老板，来一包那个十块的红塔山。",
        "source_label": "non_self",
        "voice_similarity": 0.127535,
        "source_method": "cam++_cosine",
        "review_required": False,
        "decision_reason": "below_non_self_threshold",
    }

    updated = reclassify_attribution_record(record)

    assert updated["source_label"] == "unknown"
    assert updated["review_required"] is True
    assert updated["decision_reason"] == "short_segment_low_similarity_review"
    assert updated["text"] == record["text"]


def test_thresholds_can_be_made_conservative() -> None:
    config = AttributionConfig(self_threshold=0.60, unknown_threshold=0.25)

    assert classify_similarity(0.59, config).label == "unknown"
    assert classify_similarity(0.24, config).label == "non_self"


def test_cosine_similarity_is_normalized() -> None:
    assert cosine_similarity([2.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
    assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)


def test_zero_vector_cannot_be_used_as_a_voice_decision() -> None:
    with pytest.raises(ValueError, match="non-zero"):
        cosine_similarity([0.0, 0.0], [1.0, 0.0])


def test_batch_embeddings_keep_candidate_order() -> None:
    decisions = decisions_for_embeddings(
        [1.0, 0.0],
        [[1.0, 0.0], [0.0, 1.0], [0.8, 0.2]],
    )

    assert [decision.label for decision in decisions] == [
        "self",
        "non_self",
        "self",
    ]


def test_acoustic_proximity_combines_multiple_cues() -> None:
    near = classify_acoustic_proximity(
        speech_rms_dbfs=-20.0,
        noise_floor_dbfs=-42.0,
        relative_loudness_db=4.0,
    )
    far = classify_acoustic_proximity(
        speech_rms_dbfs=-38.0,
        noise_floor_dbfs=-42.0,
        relative_loudness_db=-6.0,
    )

    assert near["acoustic_proximity"] == "near"
    assert far["acoustic_proximity"] == "far"
    assert near["proximity_score"] > far["proximity_score"]


def test_multi_evidence_can_downgrade_isolated_far_short_speech() -> None:
    records = [
        {
            "segment_id": "candidate",
            "start_offset_seconds": 30.0,
            "end_offset_seconds": 32.0,
            "source_label": "unknown",
            "voice_similarity": 0.18,
            "review_required": True,
            "decision_reason": "short_segment_low_similarity_review",
            "acoustic_proximity": "far",
            "proximity_score": 0.18,
        }
    ]

    result = apply_multi_evidence_policy(records)[0]

    assert result["source_label"] == "non_self"
    assert result["review_required"] is False
    assert result["decision_reason"] == "multi_evidence_far_low_similarity"
    assert result["review_priority"] == "low"
    assert result["review_layer"] == "background"


def test_near_field_hint_never_promotes_ambiguous_voice_to_self() -> None:
    records = [
        {
            "segment_id": "candidate",
            "start_offset_seconds": 30.0,
            "end_offset_seconds": 34.0,
            "source_label": "unknown",
            "voice_similarity": 0.38,
            "review_required": True,
            "decision_reason": "similarity_in_review_band",
            "acoustic_proximity": "near",
            "proximity_score": 0.85,
        }
    ]

    result = apply_multi_evidence_policy(records)[0]

    assert result["source_label"] == "unknown"
    assert result["review_required"] is True
    assert result["review_layer"] in {"focus", "standard", "background"}


def test_confirmed_self_context_raises_review_priority() -> None:
    records = [
        {
            "segment_id": "self",
            "start_offset_seconds": 10.0,
            "end_offset_seconds": 15.0,
            "source_label": "self",
            "voice_similarity": 0.62,
            "review_required": False,
            "decision_reason": "above_self_threshold",
            "acoustic_proximity": "near",
            "proximity_score": 0.8,
        },
        {
            "segment_id": "candidate",
            "start_offset_seconds": 16.0,
            "end_offset_seconds": 19.0,
            "source_label": "unknown",
            "voice_similarity": 0.34,
            "review_required": True,
            "decision_reason": "similarity_in_review_band",
            "acoustic_proximity": "uncertain",
            "proximity_score": 0.5,
        },
    ]

    candidate = apply_multi_evidence_policy(records)[1]

    assert candidate["self_context_support"] > 0.9
    assert candidate["review_priority"] == "high"
    assert candidate["review_layer"] == "focus"


def test_context_layer_rescues_ambiguous_voice_between_confirmed_self_speech() -> None:
    records = [
        {
            "segment_id": "before",
            "start_offset_seconds": 0.0,
            "end_offset_seconds": 20.0,
            "source_label": "self",
            "voice_similarity": 0.66,
            "speech_rms_dbfs": -23.0,
            "noise_floor_dbfs": -43.0,
            "proximity_score": 0.82,
        },
        {
            "segment_id": "candidate",
            "start_offset_seconds": 20.4,
            "end_offset_seconds": 23.0,
            "source_label": "unknown",
            "voice_similarity": 0.42,
            "review_required": True,
            "decision_reason": "similarity_in_review_band",
            "speech_rms_dbfs": -24.0,
            "noise_floor_dbfs": -42.5,
            "proximity_score": 0.78,
        },
        {
            "segment_id": "after",
            "start_offset_seconds": 23.5,
            "end_offset_seconds": 43.5,
            "source_label": "self",
            "voice_similarity": 0.64,
            "speech_rms_dbfs": -22.5,
            "noise_floor_dbfs": -42.0,
            "proximity_score": 0.80,
        },
    ]

    result = apply_context_source_scoring(records)[1]

    assert result["source_label"] == "self"
    assert result["context_layer_applied"] is True
    assert result["decision_reason"] == "context_supported_self"
    assert result["context_before_self_ratio"] == pytest.approx(1.0)
    assert result["context_after_self_ratio"] == pytest.approx(1.0)
    assert result["context_bonus"] > 0.20
    assert result["contextual_voice_score"] > 0.62


def test_context_layer_does_not_promote_without_bilateral_self_context() -> None:
    records = [
        {
            "segment_id": "before",
            "start_offset_seconds": 0.0,
            "end_offset_seconds": 20.0,
            "source_label": "self",
            "voice_similarity": 0.66,
        },
        {
            "segment_id": "candidate",
            "start_offset_seconds": 20.4,
            "end_offset_seconds": 23.0,
            "source_label": "unknown",
            "voice_similarity": 0.42,
            "review_required": True,
            "decision_reason": "similarity_in_review_band",
        },
        {
            "segment_id": "after",
            "start_offset_seconds": 23.5,
            "end_offset_seconds": 43.5,
            "source_label": "non_self",
            "voice_similarity": 0.05,
        },
    ]

    result = apply_context_source_scoring(records)[1]

    assert result["source_label"] == "unknown"
    assert result["context_layer_applied"] is False
    assert result["context_bilateral_self"] is False


def test_context_layer_keeps_very_low_similarity_non_self_final() -> None:
    records = [
        {
            "segment_id": "before",
            "start_offset_seconds": 0.0,
            "end_offset_seconds": 20.0,
            "source_label": "self",
            "voice_similarity": 0.66,
        },
        {
            "segment_id": "candidate",
            "start_offset_seconds": 20.1,
            "end_offset_seconds": 22.0,
            "source_label": "non_self",
            "voice_similarity": 0.05,
            "review_required": False,
            "decision_reason": "below_non_self_threshold",
        },
        {
            "segment_id": "after",
            "start_offset_seconds": 22.1,
            "end_offset_seconds": 42.0,
            "source_label": "self",
            "voice_similarity": 0.64,
        },
    ]

    result = apply_context_source_scoring(records)[1]

    assert result["source_label"] == "non_self"
    assert result["context_layer_applied"] is False


def test_context_layer_never_overrides_strong_personal_negative_match() -> None:
    records = [
        {
            "segment_id": "before",
            "start_offset_seconds": 0.0,
            "end_offset_seconds": 20.0,
            "source_label": "self",
            "voice_similarity": 0.66,
        },
        {
            "segment_id": "candidate",
            "start_offset_seconds": 20.1,
            "end_offset_seconds": 22.0,
            "source_label": "non_self",
            "voice_similarity": 0.42,
            "personal_decision_reason": "strong_personal_negative_match",
            "personal_self_similarity": 0.40,
            "personal_negative_similarity": 0.88,
        },
        {
            "segment_id": "after",
            "start_offset_seconds": 22.1,
            "end_offset_seconds": 42.0,
            "source_label": "self",
            "voice_similarity": 0.64,
        },
    ]

    result = apply_context_source_scoring(records)[1]

    assert result["source_label"] == "non_self"
    assert result["context_layer_applied"] is False


def test_only_self_segments_enter_life_event_extraction() -> None:
    assert should_extract_life_events("self") is True
    assert should_extract_life_events("non_self") is False
    assert should_extract_life_events("unknown") is False


def test_important_unknown_purchase_is_eligible_for_review_extraction() -> None:
    assert should_extract_life_events(
        "unknown",
        text="买了一个智慧充电口，268",
        review_priority="medium",
    ) is True
    assert should_extract_life_events(
        "unknown",
        text="电视里正在播放广告",
        review_priority="medium",
    ) is False


def test_sep26_similarity_0317_stays_reviewable_and_keeps_purchase_candidate() -> None:
    decision = classify_similarity(0.317103)

    assert decision.label == "unknown"
    assert decision.is_review_required is True
    assert should_extract_life_events(
        decision.label,
        text="买了一个智慧充电口，268，然后去超市准备消费。",
        review_priority="medium",
    ) is True


def test_low_volume_audio_gain_normalization_raises_rms_without_clipping() -> None:
    normalized, metadata = normalize_audio_gain([0.001, -0.001, 0.001, -0.001])

    assert metadata["applied"] is True
    assert metadata["gain_db"] > 0
    assert max(abs(value) for value in normalized) < 1.0


def test_audio_gain_normalization_does_not_amplify_digital_silence() -> None:
    samples = [0.0, 1e-8, -1e-8, 0.0]
    normalized, metadata = normalize_audio_gain(samples)

    assert metadata["applied"] is False
    assert metadata["gain_db"] == 0.0
    assert list(normalized) == samples


def test_audio_gain_normalization_caps_extremely_quiet_input_gain() -> None:
    normalized, metadata = normalize_audio_gain([0.0001, -0.0001] * 64)

    assert metadata["applied"] is True
    assert metadata["gain_db"] == pytest.approx(18.0)
    assert max(abs(value) for value in normalized) < 0.001


def test_audio_gain_normalization_keeps_subthreshold_near_silence_unchanged() -> None:
    samples = [9.9e-6, -9.9e-6] * 16
    normalized, metadata = normalize_audio_gain(samples)

    assert metadata["applied"] is False
    assert metadata["gain_db"] == 0.0
    assert list(normalized) == samples


def test_audio_gain_normalization_peak_limits_single_sample_impulse() -> None:
    samples = [0.0] * 127 + [0.5]
    normalized, metadata = normalize_audio_gain(samples)

    assert metadata["applied"] is True
    assert metadata["gain_db"] <= 3.03
    assert max(abs(value) for value in normalized) <= (10 ** (-3 / 20)) + 1e-9


def test_audio_gain_normalization_attenuates_hot_input_below_peak_ceiling() -> None:
    normalized, metadata = normalize_audio_gain([0.9, -0.9] * 64)

    assert metadata["applied"] is True
    assert metadata["gain_db"] < 0
    assert max(abs(value) for value in normalized) < 0.1


def test_audio_gain_normalization_handles_extreme_hot_input_without_clipping() -> None:
    normalized, metadata = normalize_audio_gain([100.0, -100.0] * 8)

    assert metadata["applied"] is True
    assert metadata["gain_db"] < -24.0
    assert max(abs(value) for value in normalized) <= (10 ** (-3 / 20)) + 1e-9


def test_audio_gain_normalization_avoids_overflow_for_huge_finite_samples() -> None:
    normalized, metadata = normalize_audio_gain([1e308, -1e308])

    assert metadata["applied"] is True
    assert metadata["input_rms_dbfs"] > 6000
    assert all(isfinite(value) for value in normalized)
    assert max(abs(value) for value in normalized) <= (10 ** (-3 / 20)) + 1e-9


@pytest.mark.parametrize("bad_value", [float("nan"), float("inf"), float("-inf")])
def test_audio_gain_normalization_rejects_non_finite_samples(bad_value: float) -> None:
    with pytest.raises(ValueError, match="finite"):
        normalize_audio_gain([0.1, bad_value, -0.1])


def test_attribution_record_preserves_transcript_evidence() -> None:
    segment = {
        "segment_id": "seg_001",
        "recording_id": "rec_001",
        "start_offset_seconds": 12.3,
        "end_offset_seconds": 15.6,
        "absolute_started_at": "2026-09-02T09:22:00+08:00",
        "absolute_ended_at": "2026-09-02T09:22:03+08:00",
        "text": "记录一下，今天吃了饭。",
    }

    result = build_attribution_record(
        segment,
        classify_similarity(0.62),
        method="cam++_cosine",
    )

    assert result["segment_id"] == "seg_001"
    assert result["text"] == segment["text"]
    assert result["source_label"] == "self"
    assert result["voice_similarity"] == pytest.approx(0.62)
    assert result["review_required"] is False


def test_partition_attributions_keeps_unknown_segments_for_review() -> None:
    records = [
        {"segment_id": "self", "source_label": "self"},
        {"segment_id": "other", "source_label": "non_self"},
        {"segment_id": "review", "source_label": "unknown"},
    ]

    result = partition_attributions(records)

    assert [item["segment_id"] for item in result["self"]] == ["self"]
    assert [item["segment_id"] for item in result["non_self"]] == ["other"]
    assert [item["segment_id"] for item in result["unknown"]] == ["review"]
