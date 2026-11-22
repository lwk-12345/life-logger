from __future__ import annotations

from typing import Any, Sequence


from life_logger.source_attribution import AttributionConfig
from life_logger.subsegment_attribution import (
    apply_mixed_speaker_subsegment_attribution,
    attribute_subsegment_utterances,
    is_mixed_speaker_candidate,
    split_record_into_utterances,
)


def _make_record(
    segment_id: str = "seg_001",
    start: float = 0.0,
    end: float = 10.0,
    text: str = "句子一。句子二。",
    source_label: str = "unknown",
    voice_similarity: float | None = 0.32,
    review_required: bool = True,
) -> dict[str, Any]:
    return {
        "segment_id": segment_id,
        "recording_id": "rec_001",
        "start_offset_seconds": start,
        "end_offset_seconds": end,
        "absolute_started_at": "2026-09-26T23:40:52.590+08:00",
        "absolute_ended_at": "2026-09-26T23:41:08.370+08:00",
        "text": text,
        "source_label": source_label,
        "voice_similarity": voice_similarity,
        "review_required": review_required,
        "decision_reason": "similarity_in_review_band",
    }


def test_is_mixed_speaker_candidate_criteria() -> None:
    # Single sentence unknown is not a candidate
    single_sentence = _make_record(text="只有一句话。")
    assert not is_mixed_speaker_candidate(single_sentence)

    # Multi-sentence high-confidence self is not a candidate
    clear_self = _make_record(source_label="self", review_required=False)
    assert not is_mixed_speaker_candidate(clear_self)

    # Multi-sentence unknown is an eligible candidate
    candidate = _make_record(source_label="unknown", review_required=True)
    assert is_mixed_speaker_candidate(candidate)


def test_split_record_into_utterances_offsets_and_parent_id() -> None:
    record = _make_record(
        segment_id="rec_seg_125",
        start=10.0,
        end=20.0,
        text="今天接口有点问题。你这麻袋装的啥？",
    )

    sub_records = split_record_into_utterances(record)
    assert len(sub_records) == 2
    assert sub_records[0]["segment_id"] == "rec_seg_125_u01"
    assert sub_records[1]["segment_id"] == "rec_seg_125_u02"
    assert sub_records[0]["parent_segment_id"] == "rec_seg_125"
    assert sub_records[1]["parent_segment_id"] == "rec_seg_125"
    assert sub_records[0]["start_offset_seconds"] == 10.0
    assert sub_records[-1]["end_offset_seconds"] == 20.0
    assert sub_records[0]["text"] == "今天接口有点问题。"
    assert sub_records[1]["text"] == "你这麻袋装的啥？"


def test_subsegment_attribution_resolves_mixed_self_and_video_speech() -> None:
    # Ground truth test case: 2026-09-26 23:40:52 segment
    record = _make_record(
        segment_id="rec_funasr_nano_2026_09_02_seg_000125",
        start=0.0,
        end=15.78,
        text="今天这个 hope club 的微信接口还是也是有点问题。你这麻袋装的啥呀？这是买的糖。",
        source_label="unknown",
        voice_similarity=0.321169,
        review_required=True,
    )

    # Mock audio: 15.78 seconds of mono samples at 16kHz
    sample_rate = 16_000
    total_samples = int(15.78 * sample_rate)
    audio = [0.05] * total_samples

    template = [1.0, 0.0, 0.0]

    call_count = 0
    def mock_extractor(_slice_samples: Sequence[float]) -> list[float]:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # First utterance (user speech) -> high similarity with [1,0,0] ~ 0.95 (self)
            return [0.95, 0.31, 0.0]
        # Subsequent utterances (video speech) -> clear non-self (< 0.10 review score floor)
        return [0.05, 0.99, 0.0]

    results = attribute_subsegment_utterances(
        record=record,
        segment_audio=audio,
        sample_rate=sample_rate,
        embedding_extractor=mock_extractor,
        template_embedding=template,
        config=AttributionConfig(self_threshold=0.50, unknown_threshold=0.25),
    )

    # 3 utterances: u01 (user speech), u02 (video), u03 (video)
    assert len(results) == 3
    u01, u02, u03 = results[0], results[1], results[2]

    # Subsegment 1 (user speech) is resolved to self without review required
    assert u01["source_label"] == "self"
    assert u01["voice_similarity"] > 0.50
    assert u01["review_required"] is False
    assert u01["parent_segment_id"] == record["segment_id"]

    # Subsegments 2 & 3 (video speech) are resolved to non_self without review required
    assert u02["source_label"] == "non_self"
    assert u02["voice_similarity"] < 0.25
    assert u02["review_required"] is False
    assert u02["parent_segment_id"] == record["segment_id"]

    assert u03["source_label"] == "non_self"
    assert u03["voice_similarity"] < 0.25
    assert u03["review_required"] is False
    assert u03["parent_segment_id"] == record["segment_id"]


def test_subsegment_attribution_skips_too_short_slices() -> None:
    # A 2.5s segment where the second sentence is 4 chars (> _MIN_FRAGMENT_LENGTH 2)
    # but its calculated slice duration is ~0.47s (< min_slice_duration 0.8s)
    record = _make_record(
        start=0.0,
        end=2.5,
        text="这里有一句非常长的句子内容在这里。买包烟。",
    )
    audio = [0.01] * int(2.5 * 16_000)
    template = [1.0, 0.0]

    def mock_extractor(_samples: Sequence[float]) -> list[float]:
        return [0.9, 0.1]

    results = attribute_subsegment_utterances(
        record=record,
        segment_audio=audio,
        sample_rate=16_000,
        embedding_extractor=mock_extractor,
        template_embedding=template,
        min_slice_duration=0.8,
    )

    assert len(results) == 2
    # Second slice was too short for CAM++ embedding (< 0.8s)
    assert results[1]["source_label"] == "unknown"
    assert results[1]["review_required"] is True
    assert "subsegment_too_short" in results[1]["decision_reason"]


def test_subsegment_attribution_returns_single_record_if_no_split() -> None:
    record = _make_record(text="没有分句标点的单句话")
    audio = [0.0] * 16_000
    template = [1.0, 0.0]

    results = attribute_subsegment_utterances(
        record=record,
        segment_audio=audio,
        sample_rate=16_000,
        embedding_extractor=lambda _: [1.0, 0.0],
        template_embedding=template,
    )

    assert len(results) == 1
    assert results[0]["segment_id"] == record["segment_id"]


def test_apply_mixed_speaker_subsegment_attribution_expands_candidates() -> None:
    rec_self = _make_record(segment_id="seg_01", source_label="self", review_required=False)
    rec_mixed = _make_record(
        segment_id="seg_02_mixed",
        start=0.0,
        end=10.0,
        text="今天接口有点问题。你这麻袋装的啥？",
        source_label="unknown",
        review_required=True,
    )
    rec_nonself = _make_record(segment_id="seg_03", source_label="non_self", review_required=False)

    records = [rec_self, rec_mixed, rec_nonself]
    audio_map = {"seg_02_mixed": [0.02] * (10 * 16_000)}

    call_count = 0
    def mock_extractor(_samples: Sequence[float]) -> list[float]:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return [0.95, 0.31, 0.0]  # u01 -> self
        return [0.05, 0.99, 0.0]      # u02 -> non_self

    expanded = apply_mixed_speaker_subsegment_attribution(
        records,
        audio_provider=lambda r: audio_map[r["segment_id"]],
        embedding_extractor=mock_extractor,
        template_embedding=[1.0, 0.0, 0.0],
    )

    # 1 unchanged + 2 subsegments + 1 unchanged = 4
    assert len(expanded) == 4
    assert expanded[0]["segment_id"] == "seg_01"
    assert expanded[1]["segment_id"] == "seg_02_mixed_u01"
    assert expanded[1]["source_label"] == "self"
    assert expanded[2]["segment_id"] == "seg_02_mixed_u02"
    assert expanded[2]["source_label"] == "non_self"
    assert expanded[3]["segment_id"] == "seg_03"

