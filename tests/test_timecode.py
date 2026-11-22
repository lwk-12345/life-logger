from datetime import datetime
from pathlib import Path

from life_logger.timecode import infer_start_time, parse_timestamp_from_name


def test_parse_underscore_timestamp() -> None:
    result = parse_timestamp_from_name(Path("2026-09-02_08-07-05.m4a"))

    assert result == datetime.fromisoformat("2026-09-02T08:07:05")


def test_parse_compact_timestamp() -> None:
    result = parse_timestamp_from_name(Path("rec-20260902-080705.wav"))

    assert result == datetime.fromisoformat("2026-09-02T08:07:05")


def test_parse_chinese_date_timestamp() -> None:
    result = parse_timestamp_from_name(Path("2026年9月2日 09_21_47.m4a"))

    assert result == datetime.fromisoformat("2026-09-02T09:21:47")


def test_missing_explicit_time_is_unknown(tmp_path: Path) -> None:
    path = tmp_path / "segment-a.m4a"
    path.touch()

    timestamp, source, precision = infer_start_time(path)

    assert timestamp is None
    assert source == "unavailable"
    assert precision == "unknown"


def test_user_start_is_explicit_exact_time(tmp_path: Path) -> None:
    audio = tmp_path / "recording.m4a"
    audio.touch()
    user = datetime.fromisoformat("2026-09-02T08:00:00+08:00")

    timestamp, source, precision = infer_start_time(audio, user_start=user)

    assert timestamp == user
    assert source == "user"
    assert precision == "exact"
