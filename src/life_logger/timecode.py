"""Timestamp parsing and provenance rules for exported recording files."""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Literal

TimeSource = Literal["filename", "user", "unavailable"]
TimePrecision = Literal["exact", "unknown"]

_PATTERNS = (
    re.compile(
        r"(?P<year>\d{4})年(?P<month>\d{1,2})月(?P<day>\d{1,2})日"
        r"[ _-]+(?P<clock>\d{1,2}[_-]\d{2}[_-]\d{2})"
    ),
    re.compile(
        r"(?P<date>\d{4}-\d{2}-\d{2})[_-]"
        r"(?P<time>\d{2}[-:]\d{2}[-:]\d{2})"
    ),
    re.compile(
        r"(?P<date>\d{4}-\d{2}-\d{2})[_-]"
        r"(?P<time>\d{2}[-:]\d{2})"
    ),
    re.compile(r"(?P<value>\d{8}[-_]\d{6})"),
)


def parse_timestamp_from_name(path: Path) -> datetime | None:
    """Parse common recorder filename timestamp conventions."""
    for pattern in _PATTERNS:
        match = pattern.search(path.stem)
        if not match:
            continue
        if "year" in match.groupdict():
            clock = match.group("clock").replace("_", ":").replace("-", ":")
            return datetime.fromisoformat(
                f"{match.group('year')}-{int(match.group('month')):02d}-"
                f"{int(match.group('day')):02d}T{clock}"
            )
        if "value" in match.groupdict():
            value = match.group("value").replace("_", "-")
            return datetime.strptime(value, "%Y%m%d-%H%M%S")
        clock = match.group("time").replace("-", ":")
        if len(clock) == 5:
            clock = f"{clock}:00"
        return datetime.fromisoformat(f"{match.group('date')}T{clock}")
    return None


def infer_start_time(
    path: Path,
    *,
    user_start: datetime | None = None,
) -> tuple[datetime | None, TimeSource, TimePrecision]:
    """Return an explicit recording start or mark it unavailable.

    Only a user-provided start or a timestamp embedded in the filename counts as
    an absolute start. Container metadata, sidecars, and filesystem mtime are not
    promoted into recording time.
    """
    if user_start is not None:
        return user_start, "user", "exact"
    timestamp = parse_timestamp_from_name(path)
    if timestamp is not None:
        return timestamp, "filename", "exact"
    return None, "unavailable", "unknown"
