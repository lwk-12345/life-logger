"""Discover audio exports and normalize them into recording segments."""

from __future__ import annotations

import hashlib
import subprocess
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from life_logger.domain import RecordingSegment
from life_logger.timecode import infer_start_time

AUDIO_EXTENSIONS = {
    ".aac",
    ".flac",
    ".m4a",
    ".mp3",
    ".mp4",
    ".mov",
    ".ogg",
    ".opus",
    ".wav",
    ".webm",
}
SIDECAR_EXTENSIONS = {".json", ".srt", ".txt", ".vtt"}


def find_sidecar(path: Path) -> Path | None:
    for extension in SIDECAR_EXTENSIONS:
        candidate = path.with_suffix(extension)
        if candidate.exists():
            return candidate
    return None


def _content_id(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return f"rec_{digest.hexdigest()[:12]}"


def _duration_seconds(path: Path) -> float | None:
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            check=True,
            capture_output=True,
            text=True,
                timeout=60,
    )
    except (FileNotFoundError, subprocess.CalledProcessError):
        return None
    try:
        return float(result.stdout.strip())
    except ValueError:
        return None


def _with_timezone(timestamp: datetime, timezone: ZoneInfo) -> datetime:
    if timestamp.tzinfo is None:
        return timestamp.replace(tzinfo=timezone)
    return timestamp.astimezone(timezone)


def discover_recordings(
    input_path: Path,
    *,
    timezone_name: str,
    user_start: datetime | None = None,
) -> list[RecordingSegment]:
    """Return timestamp-ordered recording segments for a file or directory."""
    timezone = ZoneInfo(timezone_name)
    if input_path.is_file():
        candidates = [input_path]
    elif input_path.is_dir():
        candidates = sorted(input_path.rglob("*"))
    else:
        candidates = []
    audio_files = [
        path
        for path in candidates
        if (
            path.is_file()
            and not path.name.startswith("._")
            and path.suffix.lower() in AUDIO_EXTENSIONS
        )
    ]
    if user_start is not None and len(audio_files) != 1:
        raise ValueError("--start-at can only be used with one audio file")

    normalized: list[tuple[datetime | None, Path, str, str, Path | None]] = []
    for path in audio_files:
        sidecar_path = find_sidecar(path)
        timestamp, source, precision = infer_start_time(
            path,
            user_start=user_start,
        )
        if timestamp is not None:
            timestamp = _with_timezone(timestamp, timezone)
        normalized.append((timestamp, path, source, precision, sidecar_path))

    max_timestamp = datetime.max.replace(tzinfo=timezone)
    normalized.sort(
        key=lambda item: (item[0] is None, item[0] or max_timestamp, item[1].name)
    )
    manifest: list[RecordingSegment] = []
    for sequence, item in enumerate(normalized, start=1):
        timestamp, path, source, precision, sidecar_path = item
        duration = _duration_seconds(path)
        ended_at = None
        if timestamp is not None and duration is not None:
            ended_at = timestamp.timestamp() + duration
            ended_at = datetime.fromtimestamp(ended_at, tz=timezone).isoformat()
        warnings: list[str] = []
        if timestamp is None:
            source = "unavailable"
            precision = "unknown"
            warnings.append("absolute start time unavailable; marked unknown")
        if sidecar_path is not None:
            warnings.append(f"sidecar detected: {sidecar_path.name}")
        manifest.append(
            RecordingSegment(
                recording_id=_content_id(path),
                source_file=str(path),
                started_at=timestamp.isoformat() if timestamp is not None else None,
                ended_at=ended_at,
                duration_seconds=duration,
                sequence=sequence,
                time_source=source,
                time_precision=precision,
                ingestion_warnings=warnings,
            )
        )
    return manifest
