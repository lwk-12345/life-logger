import json
from pathlib import Path

import pytest

from life_logger.pipeline import run_demo, run_process
from life_logger.transcribe import LocalWhisperTranscriber


def test_demo_keeps_full_transcript_and_emits_only_high_confidence_points(
    tmp_path: Path,
) -> None:
    result = run_demo(tmp_path / "output", timezone_name="Asia/Shanghai")

    events = json.loads(result.events_path.read_text(encoding="utf-8"))
    transcript_path = result.events_path.with_name("transcript.json")
    transcript_markdown = result.events_path.with_name("transcript.md")
    transcripts = json.loads(transcript_path.read_text(encoding="utf-8"))
    categories = {category for event in events for category in event["categories"]}

    assert categories == {"routine", "food", "expense", "exercise", "todo"}
    assert result.event_count == 5
    assert len(transcripts) == 7
    assert transcript_markdown.is_file()
    assert "上午和同事沟通了项目方向。" in transcript_markdown.read_text(
        encoding="utf-8"
    )
    assert "work_communication" not in categories
    assert "idea" not in categories


def test_process_is_idempotent_for_same_text_input(tmp_path: Path) -> None:
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    (input_dir / "2026-09-02_08-00.m4a").write_bytes(b"demo audio placeholder")

    first = run_process(
        input_dir,
        tmp_path / "output",
        timezone_name="Asia/Shanghai",
        transcriber_name="demo",
    )
    second = run_process(
        input_dir,
        tmp_path / "output",
        timezone_name="Asia/Shanghai",
        transcriber_name="demo",
    )

    assert first.events_path.read_bytes() == second.events_path.read_bytes()
    assert first.database_path.exists()


def test_local_mode_reports_when_every_recording_fails(tmp_path: Path) -> None:
    audio = tmp_path / "2026-09-02_08-00.m4a"
    audio.write_bytes(b"not a real audio file")

    with pytest.raises(RuntimeError, match="all recordings failed"):
        run_process(
            audio,
            tmp_path / "output",
            timezone_name="Asia/Shanghai",
            transcriber_name="local",
        )


def test_pipeline_preserves_transcription_failure_details(
    tmp_path: Path, monkeypatch
) -> None:
    audio = tmp_path / "2026-09-02_08-00.m4a"
    audio.write_bytes(b"placeholder")

    def fail_transcription(*args, **kwargs):
        del args, kwargs
        raise RuntimeError("Local ASR requires faster-whisper")

    monkeypatch.setattr(LocalWhisperTranscriber, "transcribe", fail_transcription)

    with pytest.raises(
        RuntimeError,
        match=(
            "all recordings failed to transcribe: .*"
            "Local ASR requires faster-whisper"
        ),
    ):
        run_process(
            audio,
            tmp_path / "output",
            timezone_name="Asia/Shanghai",
            transcriber_name="local",
        )


def test_local_pipeline_enables_chunk_checkpoints(tmp_path: Path) -> None:
    from life_logger import pipeline

    transcriber = pipeline._build_transcriber("local", tmp_path)

    assert isinstance(transcriber, LocalWhisperTranscriber)
    assert transcriber.chunk_seconds == 300.0
    assert transcriber.cpu_threads == 2
    assert transcriber.checkpoint_dir == tmp_path / ".checkpoints"
