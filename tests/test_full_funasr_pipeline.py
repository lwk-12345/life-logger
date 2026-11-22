import json
import subprocess
from datetime import datetime
from pathlib import Path

import pytest

import scripts.run_full_funasr_pipeline as full_pipeline
from scripts.run_full_funasr_pipeline import (
    normalized_segment_from_result,
    recording_windows,
)


def test_full_runner_adds_src_root_for_the_fun_asr_environment() -> None:
    assert full_pipeline.SRC_ROOT == Path(full_pipeline.REPO_ROOT) / "src"


def test_probe_duration_distinguishes_missing_ffprobe(monkeypatch) -> None:
    def missing_ffprobe(*args, **kwargs):
        del args, kwargs
        raise FileNotFoundError("ffprobe")

    monkeypatch.setattr(full_pipeline.subprocess, "run", missing_ffprobe)

    with pytest.raises(RuntimeError, match="requires ffprobe"):
        full_pipeline._probe_duration(Path("voice.wav"))


def test_probe_duration_reports_corrupt_audio(monkeypatch) -> None:
    def rejected_audio(*args, **kwargs):
        del args, kwargs
        raise subprocess.CalledProcessError(
            1,
            ["ffprobe"],
            stderr="Invalid data found when processing input",
        )

    monkeypatch.setattr(full_pipeline.subprocess, "run", rejected_audio)

    with pytest.raises(
        RuntimeError,
        match="audio file may be corrupt or unsupported.*Invalid data",
    ):
        full_pipeline._probe_duration(Path("broken.wav"))


def test_full_runner_suppresses_unattributed_events_by_default(tmp_path: Path) -> None:
    source = tmp_path / "2026-09-04_08-00.wav"
    source.write_bytes(b"audio")

    result = full_pipeline._build_daily_outputs(
        output_dir=tmp_path / "output",
        source=source,
        transcript_records=[
            {
                "segment_id": "seg_001",
                "recording_id": "rec_001",
                "start_offset_seconds": 1.0,
                "end_offset_seconds": 5.0,
                "absolute_started_at": "2026-09-04T08:00:01+08:00",
                "absolute_ended_at": "2026-09-04T08:00:05+08:00",
                "text": "直播间三十二块一颗砖。",
            }
        ],
        start_at=datetime.fromisoformat("2026-09-04T08:00:00+08:00"),
        timezone_name="Asia/Shanghai",
        model_path="fun-asr",
        warnings=[],
    )

    events = json.loads(Path(result["paths"]["events"]).read_text())

    assert events == []
    assert result["event_count"] == 0
    assert result["event_source_policy"] == "source_attribution_required"
    assert any("声源归属" in item for item in result["warnings"])


def test_recording_windows_cover_duration_without_overlap() -> None:
    windows = recording_windows(3_601.0, window_seconds=1_800.0)

    assert windows == [
        {"index": 0, "start_s": 0.0, "end_s": 1_800.0},
        {"index": 1, "start_s": 1_800.0, "end_s": 3_600.0},
        {"index": 2, "start_s": 3_600.0, "end_s": 3_601.0},
    ]


def test_normalized_segment_shifts_native_timestamps_to_recording_time() -> None:
    segment = normalized_segment_from_result(
        {
            "text": "我刚起床。",
            "language": "zh",
            "timestamps": [
                {"token": "我", "start_time": 0.2, "end_time": 0.3},
                {"token": "刚", "start_time": 0.4, "end_time": 0.5},
            ],
        },
        recording_id="rec_001",
        segment_index=1,
        chunk_start_s=120.0,
        chunk_end_s=123.0,
        start_at=datetime.fromisoformat("2026-09-02T08:00:00+08:00"),
    )

    assert segment["start_offset_seconds"] == 120.2
    assert segment["end_offset_seconds"] == 120.5
    assert segment["absolute_started_at"] == "2026-09-02T08:02:00.200000+08:00"
    assert segment["native_timestamps"][0]["token"] == "我"
