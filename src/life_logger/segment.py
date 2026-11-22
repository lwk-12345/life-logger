"""Convert ASR segments into smaller candidate utterances."""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import datetime, timedelta

from life_logger.domain import TranscriptSegment

_BOUNDARY = re.compile(r"(?<=[。！？；!?;\n])|(?<=[，,])(?=(?:然后|接着|等会儿|之后))")
_MIN_FRAGMENT_LENGTH = 2


def _absolute_at(segment: TranscriptSegment, offset: float) -> str | None:
    if segment.absolute_started_at is None:
        return None
    base = datetime.fromisoformat(segment.absolute_started_at)
    relative_offset = offset - segment.start_offset_seconds
    return (base + timedelta(seconds=relative_offset)).isoformat()


def split_utterances(segment: TranscriptSegment) -> list[TranscriptSegment]:
    """Split one ASR segment and approximate offsets by character proportion."""
    pieces = [piece.strip() for piece in _BOUNDARY.split(segment.text) if piece.strip()]
    if len(pieces) > 1:
        compacted: list[str] = []
        for piece in pieces:
            if (
                compacted
                and len(piece) <= _MIN_FRAGMENT_LENGTH
                and not re.search(r"[，,、]$", compacted[-1])
            ):
                compacted[-1] += piece
            else:
                compacted.append(piece)
        pieces = compacted
    if len(pieces) <= 1:
        return [segment] if pieces else []

    span = max(segment.end_offset_seconds - segment.start_offset_seconds, 0.0)
    char_count = max(len(segment.text), 1)
    result: list[TranscriptSegment] = []
    cursor = segment.start_offset_seconds
    for index, piece in enumerate(pieces, start=1):
        duration = span * len(piece) / char_count
        end = segment.end_offset_seconds if index == len(pieces) else cursor + duration
        result.append(
            replace(
                segment,
                segment_id=f"{segment.segment_id}_u{index:02d}",
                start_offset_seconds=round(cursor, 3),
                end_offset_seconds=round(end, 3),
                absolute_started_at=_absolute_at(segment, cursor),
                absolute_ended_at=_absolute_at(segment, end),
                text=piece,
            )
        )
        cursor = end
    return result
