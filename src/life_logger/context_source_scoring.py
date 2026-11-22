"""Temporal/context scoring for speaker-source attribution.

This layer intentionally sits between the generic CAM++ decision and the
personal prototype memory.  It does not replace speaker identity; it only lets
strong, bilateral nearby-self context rescue an otherwise ambiguous segment.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from statistics import median
from typing import Any, Mapping, Sequence


@dataclass(frozen=True)
class ContextScoringConfig:
    """Conservative thresholds for contextual self-speaker rescue."""

    window_seconds: float = 30.0
    self_threshold: float = 0.50
    minimum_voice_similarity: float = 0.15
    minimum_context_score: float = 0.58
    minimum_side_self_ratio: float = 0.20
    minimum_continuity: float = 0.35
    minimum_environment_continuity: float = 0.35
    maximum_context_bonus: float = 0.25

    def __post_init__(self) -> None:
        if self.window_seconds <= 0:
            raise ValueError("window_seconds must be positive")
        for value in (
            self.self_threshold,
            self.minimum_context_score,
            self.minimum_side_self_ratio,
            self.minimum_continuity,
            self.minimum_environment_continuity,
            self.maximum_context_bonus,
        ):
            if not isfinite(float(value)) or not 0.0 <= float(value) <= 1.0:
                raise ValueError(
                    "context thresholds must be finite and between 0 and 1"
                )
        if not isfinite(float(self.minimum_voice_similarity)):
            raise ValueError("minimum_voice_similarity must be finite")
        if not -1.0 <= self.minimum_voice_similarity <= 1.0:
            raise ValueError("minimum_voice_similarity must be between -1 and 1")


def _span(record: Mapping[str, Any]) -> tuple[float, float]:
    start = float(record.get("start_offset_seconds", 0.0))
    end = float(record.get("end_offset_seconds", start))
    return start, max(start, end)


def _overlap(start: float, end: float, window_start: float, window_end: float) -> float:
    return max(0.0, min(end, window_end) - max(start, window_start))


def _side_self_ratio(
    records: Sequence[Mapping[str, Any]],
    *,
    window_start: float,
    window_end: float,
) -> tuple[float, float, float]:
    total = 0.0
    self_total = 0.0
    for record in records:
        start, end = _span(record)
        duration = _overlap(start, end, window_start, window_end)
        if duration <= 0.0:
            continue
        total += duration
        if record.get("source_label") == "self":
            self_total += duration
    ratio = self_total / total if total > 0.0 else 0.0
    return ratio, self_total, total


def _nearest_gap(
    records: Sequence[Mapping[str, Any]],
    *,
    start: float,
    end: float,
    before: bool,
    horizon: float,
) -> float | None:
    gaps: list[float] = []
    for record in records:
        candidate_start, candidate_end = _span(record)
        if before and candidate_end <= start:
            gap = start - candidate_end
        elif not before and candidate_start >= end:
            gap = candidate_start - end
        else:
            continue
        if gap <= horizon:
            gaps.append(gap)
    return min(gaps) if gaps else None


def _gap_support(gap: float | None, horizon: float) -> float:
    if gap is None:
        return 0.0
    return max(0.0, min(1.0, 1.0 - gap / horizon))


def _continuous_speech_probability(
    neighbors: Sequence[Mapping[str, Any]],
    *,
    start: float,
    end: float,
    horizon: float,
) -> float:
    before = _gap_support(
        _nearest_gap(neighbors, start=start, end=end, before=True, horizon=horizon),
        horizon,
    )
    after = _gap_support(
        _nearest_gap(neighbors, start=start, end=end, before=False, horizon=horizon),
        horizon,
    )
    if before > 0.0 and after > 0.0:
        return min(1.0, 0.30 + 0.35 * before + 0.35 * after)
    return 0.55 * max(before, after)


def _environment_vector(record: Mapping[str, Any]) -> tuple[float, float, float] | None:
    values = (
        record.get("speech_rms_dbfs"),
        record.get("noise_floor_dbfs"),
        record.get("proximity_score"),
    )
    if any(value is None for value in values):
        return None
    vector = tuple(float(value) for value in values)
    if not all(isfinite(value) for value in vector):
        return None
    return vector


def _same_environment_continuity(
    record: Mapping[str, Any],
    nearby_self: Sequence[Mapping[str, Any]],
    nearby_all: Sequence[Mapping[str, Any]],
) -> float:
    target = _environment_vector(record)
    if target is None:
        return 0.5
    references = [
        vector
        for vector in (_environment_vector(item) for item in nearby_self)
        if vector is not None
    ]
    if not references:
        references = [
            vector
            for vector in (_environment_vector(item) for item in nearby_all)
            if vector is not None
        ]
    if not references:
        return 0.5

    reference = tuple(median(row[index] for row in references) for index in range(3))
    speech_support = max(0.0, 1.0 - abs(target[0] - reference[0]) / 16.0)
    noise_support = max(0.0, 1.0 - abs(target[1] - reference[1]) / 12.0)
    proximity_support = max(0.0, 1.0 - abs(target[2] - reference[2]))
    return max(
        0.0,
        min(
            1.0,
            0.25 * speech_support
            + 0.45 * noise_support
            + 0.30 * proximity_support,
        ),
    )


def apply_context_source_scoring(
    records: Sequence[Mapping[str, Any]],
    config: ContextScoringConfig | None = None,
) -> list[dict[str, Any]]:
    """Add 30-second context features and conservatively rescue ambiguous self speech.

    The decision is based on four explicit dimensions:
    current CAM++ similarity, nearby confirmed-self ratio, temporal speech
    continuity, and acoustic-environment continuity.  Automatic promotion
    requires confirmed self speech on both sides of the candidate.
    """

    config = config or ContextScoringConfig()
    original = [dict(record) for record in records]
    ordered = sorted(enumerate(original), key=lambda item: _span(item[1])[0])
    results_by_index: dict[int, dict[str, Any]] = {}

    ordered_records = [record for _original_index, record in ordered]
    for ordered_index, (original_index, source) in enumerate(ordered):
        record = dict(source)
        start, end = _span(record)
        before_start = start - config.window_seconds
        after_end = end + config.window_seconds
        neighbors = [
            candidate
            for candidate_index, candidate in enumerate(ordered_records)
            if candidate_index != ordered_index
            and _span(candidate)[1] >= before_start
            and _span(candidate)[0] <= after_end
        ]
        before = [
            candidate
            for candidate in neighbors
            if _span(candidate)[1] <= start and _span(candidate)[1] >= before_start
        ]
        after = [
            candidate
            for candidate in neighbors
            if _span(candidate)[0] >= end and _span(candidate)[0] <= after_end
        ]
        before_ratio, before_self_seconds, before_speech_seconds = _side_self_ratio(
            before,
            window_start=before_start,
            window_end=start,
        )
        after_ratio, after_self_seconds, after_speech_seconds = _side_self_ratio(
            after,
            window_start=end,
            window_end=after_end,
        )
        total_speech = before_speech_seconds + after_speech_seconds
        total_self = before_self_seconds + after_self_seconds
        self_ratio = total_self / total_speech if total_speech > 0.0 else 0.0
        continuity = _continuous_speech_probability(
            neighbors,
            start=start,
            end=end,
            horizon=config.window_seconds,
        )
        nearby_self = [
            candidate
            for candidate in neighbors
            if candidate.get("source_label") == "self"
        ]
        environment = _same_environment_continuity(record, nearby_self, neighbors)
        context_score = max(
            0.0,
            min(1.0, 0.55 * self_ratio + 0.25 * continuity + 0.20 * environment),
        )
        bonus = config.maximum_context_bonus * context_score
        similarity_raw = record.get("voice_similarity")
        similarity = float(similarity_raw) if similarity_raw is not None else None
        contextual_similarity = (
            max(-1.0, min(1.0, similarity + bonus))
            if similarity is not None
            else None
        )
        bilateral = (
            before_ratio >= config.minimum_side_self_ratio
            and after_ratio >= config.minimum_side_self_ratio
        )

        record.update(
            context_window_seconds=config.window_seconds,
            context_before_self_ratio=round(before_ratio, 6),
            context_after_self_ratio=round(after_ratio, 6),
            context_self_ratio=round(self_ratio, 6),
            continuous_speech_probability=round(continuity, 6),
            same_environment_continuity=round(environment, 6),
            context_score=round(context_score, 6),
            context_bonus=round(bonus, 6),
            contextual_voice_score=(
                round(contextual_similarity, 6)
                if contextual_similarity is not None
                else None
            ),
            context_bilateral_self=bilateral,
            context_layer_applied=False,
            context_base_source_label=record.get("source_label"),
        )

        eligible_label = record.get("source_label") in {"unknown", "non_self"}
        strong_personal_negative = (
            record.get("personal_decision_reason") == "strong_personal_negative_match"
        )
        if (
            eligible_label
            and not strong_personal_negative
            and similarity is not None
            and similarity >= config.minimum_voice_similarity
            and contextual_similarity is not None
            and contextual_similarity >= config.self_threshold
            and context_score >= config.minimum_context_score
            and bilateral
            and continuity >= config.minimum_continuity
            and environment >= config.minimum_environment_continuity
        ):
            record["source_label"] = "self"
            record["review_required"] = False
            record["decision_reason"] = "context_supported_self"
            record["context_layer_applied"] = True

        results_by_index[original_index] = record

    return [results_by_index[index] for index in range(len(original))]
