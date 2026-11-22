"""Coordinate ingestion, transcription, extraction, and output."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from life_logger.aggregate import build_daily_summary
from life_logger.domain import LifeEvent, RecordingSegment, TranscriptSegment
from life_logger.extract import RuleBasedExtractor
from life_logger.ingest import discover_recordings, find_sidecar
from life_logger.merge import merge_adjacent_events
from life_logger.render import write_daily_outputs
from life_logger.segment import split_utterances
from life_logger.transcribe import (
    DemoTranscriber,
    LocalWhisperTranscriber,
    Transcriber,
    select_sidecar_transcriber,
)


@dataclass(frozen=True)
class PipelineResult:
    events_path: Path
    markdown_path: Path
    database_path: Path
    event_count: int
    warnings: list[str]


def _date_from_recordings(recordings: list[RecordingSegment]) -> str | None:
    dates = {item.started_at[:10] for item in recordings if item.started_at}
    return dates.pop() if len(dates) == 1 else None


def _build_transcriber(
    name: str,
    output_dir: Path,
    *,
    chunk_seconds: float = 300.0,
    cpu_threads: int = 2,
    progress_callback: Callable[[int, int], None] | None = None,
) -> Transcriber:
    if name == "demo":
        fixture = (
            Path(__file__).resolve().parents[2] / "fixtures" / "demo_transcript.json"
        )
        return DemoTranscriber(fixture)
    if name in {"local", "auto"}:
        return LocalWhisperTranscriber(
            model_name="small",
            chunk_seconds=chunk_seconds,
            cpu_threads=cpu_threads,
            checkpoint_dir=output_dir / ".checkpoints",
            progress_callback=progress_callback,
        )
    if name in {"doubao", "volcano"}:
        from life_logger.transcribe import DoubaoASRTranscriber
        return DoubaoASRTranscriber()
    if name in {"local_turbo", "turbo", "mlx_whisper_turbo"}:
        from life_logger.transcribe import get_shared_turbo_transcriber
        return get_shared_turbo_transcriber()
    raise ValueError(f"unsupported transcriber: {name}")


def _sidecar_transcript(
    recording: RecordingSegment,
) -> list[TranscriptSegment] | None:
    sidecar = find_sidecar(Path(recording.source_file))
    if sidecar is None:
        return None
    parser = select_sidecar_transcriber(sidecar)
    segments = parser.transcribe(recording, sidecar)
    return segments or None


def _transcribe_recording(
    recording: RecordingSegment,
    *,
    transcriber_name: str,
    transcriber: Transcriber,
) -> list[TranscriptSegment]:
    if transcriber_name in {"local", "auto"}:
        sidecar_result = _sidecar_transcript(recording)
        if sidecar_result is not None:
            return sidecar_result
    return transcriber.transcribe(recording)


def run_process(
    input_path: Path,
    output_dir: Path,
    *,
    timezone_name: str,
    transcriber_name: str,
    user_start: datetime | None = None,
    chunk_seconds: float = 300.0,
    cpu_threads: int = 2,
    progress_callback: Callable[[int, int], None] | None = None,
) -> PipelineResult:
    """Run the full audio-to-events pipeline for a file or directory."""
    recordings = discover_recordings(
        input_path,
        timezone_name=timezone_name,
        user_start=user_start,
    )
    if not recordings:
        raise ValueError(
            f"no supported audio files found under {input_path}; "
            "copy an audio file there or pass a file path"
        )

    transcriber = _build_transcriber(
        transcriber_name,
        output_dir,
        chunk_seconds=chunk_seconds,
        cpu_threads=cpu_threads,
        progress_callback=progress_callback,
    )
    extractor = RuleBasedExtractor()
    transcripts: list[TranscriptSegment] = []
    events: list[LifeEvent] = []
    warnings = [warning for item in recordings for warning in item.ingestion_warnings]
    failed_recordings = 0
    transcription_failures: list[str] = []

    for recording in recordings:
        try:
            segments = _transcribe_recording(
                recording,
                transcriber_name=transcriber_name,
                transcriber=transcriber,
            )
        except Exception as error:
            failed_recordings += 1
            failure = f"{recording.source_file}: transcription failed: {error}"
            transcription_failures.append(failure)
            warnings.append(failure)
            continue
        transcripts.extend(segments)
        for segment in segments:
            for utterance in split_utterances(segment):
                events.extend(extractor.extract(utterance))

    if failed_recordings == len(recordings):
        details = "; ".join(transcription_failures)
        raise RuntimeError(f"all recordings failed to transcribe: {details}")

    events = merge_adjacent_events(events)
    date = _date_from_recordings(recordings)
    summary = build_daily_summary(
        events,
        date=date,
        timezone_name=timezone_name,
        warnings=warnings,
    )
    paths = write_daily_outputs(
        output_dir,
        date=date,
        recordings=recordings,
        transcripts=transcripts,
        events=events,
        summary=summary,
    )
    return PipelineResult(
        events_path=paths["events"],
        markdown_path=paths["markdown"],
        database_path=paths["database"],
        event_count=len(events),
        warnings=warnings,
    )


def run_demo(output_dir: Path, *, timezone_name: str) -> PipelineResult:
    """Run the complete pipeline with a bundled transcript fixture."""
    demo_input = output_dir / ".demo-input"
    demo_input.mkdir(parents=True, exist_ok=True)
    demo_file = demo_input / "2026-09-02_08-00.m4a"
    if not demo_file.exists():
        demo_file.write_bytes(b"demo audio placeholder")
    return run_process(
        demo_input,
        output_dir,
        timezone_name=timezone_name,
        transcriber_name="demo",
    )
