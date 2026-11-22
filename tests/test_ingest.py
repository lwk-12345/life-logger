from datetime import datetime
from pathlib import Path

from life_logger.ingest import discover_recordings


def test_discover_directory_sorts_timestamped_segments_and_skips_sidecars(
    tmp_path: Path,
) -> None:
    first = tmp_path / "2026-09-02_08-00.m4a"
    second = tmp_path / "2026-09-02_09-00.m4a"
    sidecar = tmp_path / "2026-09-02_08-00.json"
    apple_double = tmp_path / "._2026-09-02_08-00.WAV"
    first.write_bytes(b"one")
    second.write_bytes(b"two")
    sidecar.write_text("{}", encoding="utf-8")
    apple_double.write_bytes(b"metadata")

    manifest = discover_recordings(tmp_path, timezone_name="Asia/Shanghai")

    assert [item.sequence for item in manifest] == [1, 2]
    assert [Path(item.source_file).name for item in manifest] == [
        first.name,
        second.name,
    ]
    assert manifest[0].started_at == "2026-09-02T08:00:00+08:00"
    assert manifest[0].time_source == "filename"
    assert "sidecar detected" in manifest[0].ingestion_warnings[0]


def test_discover_single_file_preserves_relative_time_when_unavailable(
    tmp_path: Path,
) -> None:
    audio = tmp_path / "unknown-name.m4a"
    audio.write_bytes(b"audio")

    manifest = discover_recordings(
        audio,
        timezone_name="Asia/Shanghai",
    )

    assert len(manifest) == 1
    assert manifest[0].started_at is None
    assert manifest[0].time_source == "unavailable"
    assert manifest[0].time_precision == "unknown"
    assert "absolute start time unavailable" in manifest[0].ingestion_warnings[0]


def test_user_start_overrides_filename_for_single_file(tmp_path: Path) -> None:
    audio = tmp_path / "unknown-name.m4a"
    audio.write_bytes(b"audio")

    manifest = discover_recordings(
        audio,
        timezone_name="Asia/Shanghai",
        user_start=datetime.fromisoformat("2026-09-02T08:00:00+08:00"),
    )

    assert manifest[0].started_at == "2026-09-02T08:00:00+08:00"
    assert manifest[0].time_source == "user"
    assert manifest[0].time_precision == "exact"
