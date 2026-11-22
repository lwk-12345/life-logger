"""Subsegment audio slicing and speaker attribution for mixed-speech segments."""

from __future__ import annotations

from typing import Any, Callable, Mapping, Sequence

from life_logger.domain import TranscriptSegment
from life_logger.segment import split_utterances
from life_logger.source_attribution import (
    AttributionConfig,
    build_attribution_record,
    classify_similarity,
    cosine_similarity,
    normalize_audio_gain,
    protect_short_low_similarity,
    unknown_decision,
)

SUBSEGMENT_SOURCE_METHOD = "cam++_subsegment_cosine"
DEFAULT_MIN_SLICE_DURATION = 0.8


def split_record_into_utterances(record: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Split a record into child utterance records using text boundary rules."""
    start = float(record.get("start_offset_seconds", 0.0))
    end = float(record.get("end_offset_seconds", start))
    transcript = TranscriptSegment(
        segment_id=str(record.get("segment_id", "seg")),
        recording_id=str(record.get("recording_id", "")),
        start_offset_seconds=start,
        end_offset_seconds=end,
        absolute_started_at=record.get("absolute_started_at"),
        absolute_ended_at=record.get("absolute_ended_at"),
        text=str(record.get("text", "")),
        source=str(record.get("source", "watch")),
        language=str(record.get("language", "zh")),
        transcription_confidence=record.get("transcription_confidence"),
    )

    utterances = split_utterances(transcript)
    if len(utterances) <= 1:
        return [dict(record)]

    parent_id = str(record.get("segment_id", "seg"))
    results: list[dict[str, Any]] = []
    for utt in utterances:
        item = dict(record)
        item.update(
            segment_id=utt.segment_id,
            parent_segment_id=parent_id,
            start_offset_seconds=utt.start_offset_seconds,
            end_offset_seconds=utt.end_offset_seconds,
            absolute_started_at=utt.absolute_started_at,
            absolute_ended_at=utt.absolute_ended_at,
            text=utt.text,
            is_subsegment=True,
        )
        results.append(item)
    return results


def is_mixed_speaker_candidate(
    record: Mapping[str, Any],
    min_subsegments: int = 2,
    min_total_duration: float = 2.0,
) -> bool:
    """Determine whether an ambiguous record should undergo subsegment attribution."""
    source_label = record.get("source_label", "unknown")
    review_required = bool(record.get("review_required", True))
    if source_label != "unknown" and not review_required:
        return False

    start = float(record.get("start_offset_seconds", 0.0))
    end = float(record.get("end_offset_seconds", start))
    duration = max(0.0, end - start)
    if duration < min_total_duration:
        return False

    sub_records = split_record_into_utterances(record)
    return len(sub_records) >= min_subsegments


def attribute_subsegment_utterances(
    record: Mapping[str, Any],
    segment_audio: Sequence[float] | Any,
    *,
    sample_rate: int = 16_000,
    embedding_extractor: Callable[[Sequence[float]], Sequence[float]],
    template_embedding: Sequence[float],
    config: AttributionConfig | None = None,
    min_slice_duration: float = DEFAULT_MIN_SLICE_DURATION,
    short_review_seconds: float = 4.0,
    short_review_score_floor: float = 0.10,
) -> list[dict[str, Any]]:
    """Slice segment audio per utterance, compute CAM++ embeddings, and attribute each."""
    sub_records = split_record_into_utterances(record)
    if len(sub_records) <= 1:
        return [dict(record)]

    config = config or AttributionConfig()
    parent_start = float(record.get("start_offset_seconds", 0.0))
    total_samples = len(segment_audio)

    results: list[dict[str, Any]] = []
    for sub in sub_records:
        sub_start = float(sub.get("start_offset_seconds", parent_start))
        sub_end = float(sub.get("end_offset_seconds", sub_start))
        slice_duration = max(0.0, sub_end - sub_start)

        start_sample = max(0, int(round((sub_start - parent_start) * sample_rate)))
        end_sample = min(total_samples, int(round((sub_end - parent_start) * sample_rate)))

        if slice_duration < min_slice_duration or (end_sample - start_sample) < int(min_slice_duration * sample_rate):
            decision = unknown_decision("subsegment_too_short")
            attributed = build_attribution_record(sub, decision, method=SUBSEGMENT_SOURCE_METHOD)
            attributed["parent_segment_id"] = record.get("segment_id")
            attributed["is_subsegment"] = True
            results.append(attributed)
            continue

        slice_audio = segment_audio[start_sample:end_sample]
        normalized_audio, _ = normalize_audio_gain(slice_audio)

        try:
            sub_embedding = embedding_extractor(normalized_audio)
            similarity = cosine_similarity(template_embedding, sub_embedding)
            decision = classify_similarity(similarity, config)
            decision = protect_short_low_similarity(
                decision,
                duration_seconds=slice_duration,
                review_seconds=short_review_seconds,
                review_score_floor=short_review_score_floor,
            )
        except Exception as error:
            decision = unknown_decision(f"subsegment_model_error:{type(error).__name__}")

        attributed = build_attribution_record(sub, decision, method=SUBSEGMENT_SOURCE_METHOD)
        attributed["parent_segment_id"] = record.get("segment_id")
        attributed["is_subsegment"] = True
        results.append(attributed)

    return results


def apply_mixed_speaker_subsegment_attribution(
    records: Sequence[Mapping[str, Any]],
    *,
    audio_provider: Callable[[Mapping[str, Any]], Any],
    embedding_extractor: Callable[[Sequence[float]], Sequence[float]],
    template_embedding: Sequence[float],
    config: AttributionConfig | None = None,
    min_slice_duration: float = DEFAULT_MIN_SLICE_DURATION,
) -> list[dict[str, Any]]:
    """Expand mixed-speaker candidate records into separately attributed subsegment records."""
    expanded: list[dict[str, Any]] = []
    for record in records:
        if not is_mixed_speaker_candidate(record):
            expanded.append(dict(record))
            continue

        try:
            audio = audio_provider(record)
        except Exception:
            expanded.append(dict(record))
            continue

        sub_records = attribute_subsegment_utterances(
            record=record,
            segment_audio=audio,
            embedding_extractor=embedding_extractor,
            template_embedding=template_embedding,
            config=config,
            min_slice_duration=min_slice_duration,
        )
        if len(sub_records) > 1:
            expanded.extend(sub_records)
        else:
            expanded.append(dict(record))

    return expanded

