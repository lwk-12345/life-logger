"""Process one long recording with Fun-ASR-Nano and the Life Logger pipeline."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
for import_root in (REPO_ROOT, SRC_ROOT):
    if str(import_root) not in sys.path:
        sys.path.insert(0, str(import_root))

from life_logger.source_attribution import normalize_audio_gain  # noqa: E402
from scripts.asr_benchmark import _safe_json, merge_vad_regions  # noqa: E402


def recording_windows(
    duration_seconds: float,
    *,
    window_seconds: float = 1_800.0,
) -> list[dict[str, float | int]]:
    """Return contiguous, non-overlapping windows covering a recording."""

    if duration_seconds < 0:
        raise ValueError("duration_seconds must be non-negative")
    if window_seconds <= 0:
        raise ValueError("window_seconds must be greater than zero")

    windows: list[dict[str, float | int]] = []
    start = 0.0
    index = 0
    while start < duration_seconds:
        end = min(start + window_seconds, duration_seconds)
        windows.append(
            {
                "index": index,
                "start_s": round(start, 3),
                "end_s": round(end, 3),
            }
        )
        start = end
        index += 1
    return windows


def _timestamp_bounds(timestamps: Any) -> tuple[float, float] | None:
    bounds: list[tuple[float, float]] = []
    for item in timestamps or []:
        if isinstance(item, dict):
            start = item.get("start_time", item.get("start"))
            end = item.get("end_time", item.get("end"))
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            start, end = item[0], item[1]
            start = float(start) / 1000.0
            end = float(end) / 1000.0
        else:
            continue
        if start is None or end is None:
            continue
        start_float = float(start)
        end_float = float(end)
        if end_float > start_float:
            bounds.append((start_float, end_float))
    if not bounds:
        return None
    return min(item[0] for item in bounds), max(item[1] for item in bounds)


def normalized_segment_from_result(
    raw: dict[str, Any],
    *,
    recording_id: str,
    segment_index: int,
    chunk_start_s: float,
    chunk_end_s: float,
    start_at: datetime | None,
) -> dict[str, Any] | None:
    """Normalize one Fun-ASR result while retaining native token timestamps."""

    text = str(raw.get("text", "")).strip()
    if not text:
        return None

    timestamps = raw.get("timestamps") or raw.get("timestamp") or []
    bounds = _timestamp_bounds(timestamps)
    if bounds is None:
        start_s, end_s = chunk_start_s, chunk_end_s
        timestamp_type = "coarse_vad"
    else:
        start_s = chunk_start_s + bounds[0]
        end_s = min(chunk_end_s, chunk_start_s + bounds[1])
        timestamp_type = "native_token"
    end_s = max(end_s, start_s)

    absolute_start = None
    absolute_end = None
    if start_at is not None:
        absolute_start = (start_at + timedelta(seconds=start_s)).isoformat()
        absolute_end = (start_at + timedelta(seconds=end_s)).isoformat()

    return {
        "segment_id": f"{recording_id}_seg_{segment_index:06d}",
        "recording_id": recording_id,
        "start_offset_seconds": round(start_s, 3),
        "end_offset_seconds": round(end_s, 3),
        "absolute_started_at": absolute_start,
        "absolute_ended_at": absolute_end,
        "text": text,
        "raw_text": str(raw.get("text", text)),
        "source": "local_funasr_nano",
        "language": str(raw.get("language", "zh")),
        "timestamp_type": timestamp_type,
        "native_timestamps": _safe_json(timestamps),
    }


def _probe_duration(path: Path) -> float:
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
        )
    except FileNotFoundError as error:
        raise RuntimeError("full Fun-ASR processing requires ffprobe") from error
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or "").strip()
        suffix = f": {detail}" if detail else ""
        raise RuntimeError(
            f"could not read audio duration for {path}; "
            f"the audio file may be corrupt or unsupported{suffix}"
        ) from error
    try:
        return float(result.stdout.strip())
    except ValueError as error:
        raise RuntimeError(f"could not read duration for {path}") from error


def _render_window(
    source: Path,
    *,
    start_s: float,
    duration_s: float,
    destination: Path,
) -> None:
    try:
        subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-ss",
                str(start_s),
                "-i",
                str(source),
                "-t",
                str(duration_s),
                "-vn",
                "-ac",
                "1",
                "-ar",
                "16000",
                "-c:a",
                "pcm_s16le",
                "-y",
                str(destination),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as error:
        raise RuntimeError("full Fun-ASR processing requires ffmpeg") from error
    except subprocess.CalledProcessError as error:
        detail = error.stderr.strip() or "unknown ffmpeg error"
        raise RuntimeError(f"could not render window: {detail}") from error


def _load_waveform(path: Path) -> tuple[Any, int]:
    try:
        import soundfile as sf
    except ImportError as error:
        raise RuntimeError("full Fun-ASR processing requires soundfile") from error
    waveform, sample_rate = sf.read(path, dtype="float32", always_2d=False)
    if getattr(waveform, "ndim", 1) == 2:
        waveform = waveform.mean(axis=1)
    if sample_rate != 16000:
        raise RuntimeError(f"expected 16kHz window, got {sample_rate}Hz")
    return waveform, sample_rate


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        delete=False,
    ) as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, default=str)
        temporary = Path(handle.name)
    os.replace(temporary, path)


def _transcript_from_dict(payload: dict[str, Any]) -> Any:
    from life_logger.domain import TranscriptSegment

    return TranscriptSegment(
        segment_id=payload["segment_id"],
        recording_id=payload["recording_id"],
        start_offset_seconds=float(payload["start_offset_seconds"]),
        end_offset_seconds=float(payload["end_offset_seconds"]),
        absolute_started_at=payload.get("absolute_started_at"),
        absolute_ended_at=payload.get("absolute_ended_at"),
        text=payload["text"],
        source=payload.get("source", "local_funasr_nano"),
        language=payload.get("language", "zh"),
        transcription_confidence=None,
        # 以下字段原先被漏掉：任何带说话人/性别/情绪/词级时间戳的 provider
        # （doubao / MOSS）产出到这里都会被清空，导致下游 speaker 恒为 None。
        speaker=payload.get("speaker"),
        gender=payload.get("gender"),
        emotion=payload.get("emotion"),
        words=payload.get("words"),
    )


def _checkpoint_matches(
    payload: dict[str, Any],
    *,
    source: Path,
    model_path: str,
    duration_s: float,
    window_seconds: float,
    max_chunk_seconds: float,
    merge_gap_seconds: float,
) -> bool:
    return (
        payload.get("source") == str(source)
        and payload.get("model_path") == model_path
        and payload.get("duration_s") == round(duration_s, 3)
        and payload.get("window_seconds") == window_seconds
        and payload.get("max_chunk_seconds") == max_chunk_seconds
        and payload.get("merge_gap_seconds") == merge_gap_seconds
    )


def _load_checkpoint(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _build_daily_outputs(
    *,
    output_dir: Path,
    source: Path,
    transcript_records: list[dict[str, Any]],
    start_at: datetime | None,
    timezone_name: str,
    model_path: str,
    warnings: list[str],
    include_events: bool = False,
) -> dict[str, Any]:
    from life_logger.aggregate import build_daily_summary
    from life_logger.domain import LifeEvent
    from life_logger.ingest import discover_recordings
    from life_logger.merge import merge_adjacent_events
    from life_logger.render import write_daily_outputs
    from life_logger.segment import split_utterances

    recordings = discover_recordings(
        source,
        timezone_name=timezone_name,
        user_start=start_at,
    )
    if not recordings:
        raise RuntimeError(f"could not build recording manifest for {source}")

    transcripts = [_transcript_from_dict(item) for item in transcript_records]
    events: list[LifeEvent] = []
    effective_warnings = list(warnings)
    if include_events:
        from life_logger.extract import RuleBasedExtractor

        extractor = RuleBasedExtractor()
        for transcript in transcripts:
            for utterance in split_utterances(transcript):
                events.extend(extractor.extract(utterance))
        events = merge_adjacent_events(events)
        event_source_policy = "unfiltered_opt_in"
        effective_warnings.append(
            "当前 events.json 未经过声源归属过滤，仅可作为未过滤基线。"
        )
    else:
        event_source_policy = "source_attribution_required"
        effective_warnings.append(
            "已抑制未经过声源归属的生活事件；请使用声源归属阶段生成正式日报。"
        )

    date = None
    if start_at is not None:
        # Convert start_at to the target timezone for correct archive date.
        date = start_at.astimezone(ZoneInfo(timezone_name)).date().isoformat()
    elif recordings[0].started_at:
        date = recordings[0].started_at[:10]
    summary = build_daily_summary(
        events,
        date=date,
        timezone_name=timezone_name,
        warnings=effective_warnings,
    )
    summary["event_source_policy"] = event_source_policy
    paths = write_daily_outputs(
        output_dir,
        date=date,
        recordings=recordings,
        transcripts=transcripts,
        events=events,
        summary=summary,
    )
    category_counts: dict[str, int] = {}
    for event in events:
        for category in event.categories:
            category_counts[category] = category_counts.get(category, 0) + 1
    return {
        "model_path": model_path,
        "recording_count": len(recordings),
        "transcript_count": len(transcripts),
        "event_count": len(events),
        "event_category_counts": category_counts,
        "event_source_policy": event_source_policy,
        "warnings": effective_warnings,
        "paths": {key: str(value) for key, value in paths.items()},
    }


def run_full_pipeline(
    *,
    source: Path,
    output_dir: Path,
    model_path: str,
    vad_model_path: str,
    start_at: datetime | None,
    timezone_name: str = "Asia/Shanghai",
    window_seconds: float = 1_800.0,
    max_chunk_seconds: float = 30.0,
    merge_gap_seconds: float = 1.5,
    batch_size_s: float = 120.0,
    cpu_threads: int = 2,
    include_events: bool = False,
) -> dict[str, Any]:
    source = source.resolve()
    duration_s = round(_probe_duration(source), 3)
    windows = recording_windows(duration_s, window_seconds=window_seconds)
    output_dir.mkdir(parents=True, exist_ok=True)
    checkpoint_path = output_dir / "checkpoint.json"
    raw_dir = output_dir / "raw-windows"
    checkpoint = _load_checkpoint(checkpoint_path)
    if not _checkpoint_matches(
        checkpoint,
        source=source,
        model_path=model_path,
        duration_s=duration_s,
        window_seconds=window_seconds,
        max_chunk_seconds=max_chunk_seconds,
        merge_gap_seconds=merge_gap_seconds,
    ):
        checkpoint = {}

    transcript_records = list(checkpoint.get("transcripts", []))
    window_summaries = list(checkpoint.get("window_summaries", []))
    warnings = list(checkpoint.get("warnings", []))
    completed_windows = min(
        max(int(checkpoint.get("completed_windows", 0)), 0),
        len(windows),
    )

    if completed_windows < len(windows):
        try:
            from funasr import AutoModel
            from funasr_onnx import Fsmn_vad
        except ImportError as error:
            raise RuntimeError(
                "full Fun-ASR processing requires the existing FunASR and "
                "funasr-onnx environment"
            ) from error

        model = AutoModel(
            model=model_path,
            device="cpu",
            ncpu=cpu_threads,
            disable_update=True,
            disable_pbar=True,
        )
        vad_model = Fsmn_vad(
            vad_model_path,
            quantize=True,
            intra_op_num_threads=cpu_threads,
        )

        for window in windows[completed_windows:]:
            window_index = int(window["index"])
            window_start = float(window["start_s"])
            window_end = float(window["end_s"])
            window_duration = window_end - window_start
            with tempfile.TemporaryDirectory(
                prefix=f"life-logger-window-{window_index:04d}-"
            ) as temporary_directory:
                temporary_root = Path(temporary_directory)
                window_audio = temporary_root / "window.wav"
                _render_window(
                    source,
                    start_s=window_start,
                    duration_s=window_duration,
                    destination=window_audio,
                )
                waveform, sample_rate = _load_waveform(window_audio)
                if len(waveform) == 0:
                    warning = f"window {window_index}: decoded empty audio; skipped"
                    warnings.append(warning)
                    raw_window = {
                        "window_index": window_index,
                        "start_s": window_start,
                        "end_s": window_end,
                        "duration_s": window_duration,
                        "sample_rate": sample_rate,
                        "vad_regions": [],
                        "chunks": [],
                        "raw_results": [],
                        "segments": [],
                        "warning": warning,
                    }
                    raw_window_path = raw_dir / f"window-{window_index:04d}.json"
                    _atomic_write_json(raw_window_path, raw_window)
                    window_summaries.append(
                        {
                            "window_index": window_index,
                            "start_s": window_start,
                            "end_s": window_end,
                            "duration_s": window_duration,
                            "chunks": 0,
                            "segments": 0,
                            "warning": warning,
                        }
                    )
                    completed_windows = window_index + 1
                    checkpoint = {
                        "status": "running",
                        "source": str(source),
                        "model_path": model_path,
                        "vad_model_path": vad_model_path,
                        "duration_s": duration_s,
                        "window_seconds": window_seconds,
                        "max_chunk_seconds": max_chunk_seconds,
                        "merge_gap_seconds": merge_gap_seconds,
                        "batch_size_s": batch_size_s,
                        "completed_windows": completed_windows,
                        "total_windows": len(windows),
                        "window_summaries": window_summaries,
                        "transcripts": transcript_records,
                        "warnings": warnings,
                    }
                    _atomic_write_json(checkpoint_path, checkpoint)
                    print(
                        f"window {completed_windows}/{len(windows)}: "
                        "chunks=0 segments=0",
                        flush=True,
                    )
                    continue
                waveform, window_normalization = normalize_audio_gain(waveform)
                import soundfile as sf

                sf.write(window_audio, waveform, sample_rate)
                vad_raw = vad_model([str(window_audio)])[0]
                chunks = merge_vad_regions(
                    [{"start_ms": item[0], "end_ms": item[1]} for item in vad_raw],
                    merge_gap_seconds=merge_gap_seconds,
                    max_chunk_seconds=max_chunk_seconds,
                )
                chunk_directory = temporary_root / "chunks"
                chunk_directory.mkdir()
                chunk_paths: list[str] = []
                for chunk_index, chunk in enumerate(chunks):
                    start_index = max(0, int(round(chunk["start_s"] * sample_rate)))
                    end_index = min(
                        len(waveform), int(round(chunk["end_s"] * sample_rate))
                    )
                    if end_index <= start_index:
                        continue
                    chunk_path = chunk_directory / f"chunk-{chunk_index:04d}.wav"
                    chunk_audio, _normalization = normalize_audio_gain(
                        waveform[start_index:end_index]
                    )
                    sf.write(
                        chunk_path,
                        chunk_audio,
                        sample_rate,
                    )
                    chunk_paths.append(str(chunk_path))

                raw_results: list[dict[str, Any]] = []
                if chunk_paths:
                    raw_results = [
                        _safe_json(item)
                        for item in model.generate(
                            input=chunk_paths,
                            batch_size_s=batch_size_s,
                            language="中文",
                            itn=True,
                        )
                    ]

                window_segments: list[dict[str, Any]] = []
                for index, chunk in enumerate(chunks):
                    if index >= len(raw_results):
                        warnings.append(
                            f"window {window_index} chunk {index}: no ASR result"
                        )
                        continue
                    segment = normalized_segment_from_result(
                        raw_results[index],
                        recording_id="rec_funasr_nano_2026_09_02",
                        segment_index=(
                            len(transcript_records) + len(window_segments) + 1
                        ),
                        chunk_start_s=window_start + chunk["start_s"],
                        chunk_end_s=window_start + chunk["end_s"],
                        start_at=start_at,
                    )
                    if segment is not None:
                        segment["window_index"] = window_index
                        window_segments.append(segment)

                transcript_records.extend(window_segments)
                transcript_records.sort(key=lambda item: item["start_offset_seconds"])
                window_record = {
                    "window": window,
                    "vad_regions": _safe_json(vad_raw),
                    "chunks": chunks,
                    "raw_results": raw_results,
                    "segments": window_segments,
                }
                _atomic_write_json(
                    raw_dir / f"window-{window_index:04d}.json",
                    window_record,
                )
                window_summaries.append(
                    {
                        "window": window,
                        "vad_region_count": len(vad_raw),
                        "chunk_count": len(chunks),
                        "segment_count": len(window_segments),
                    }
                )
                completed_windows = window_index + 1
                _atomic_write_json(
                    checkpoint_path,
                    {
                        "status": "running",
                        "source": str(source),
                        "model_path": model_path,
                        "vad_model_path": vad_model_path,
                        "duration_s": duration_s,
                        "window_seconds": window_seconds,
                        "max_chunk_seconds": max_chunk_seconds,
                        "merge_gap_seconds": merge_gap_seconds,
                        "batch_size_s": batch_size_s,
                        "completed_windows": completed_windows,
                        "total_windows": len(windows),
                        "transcripts": transcript_records,
                        "window_summaries": window_summaries,
                        "warnings": warnings,
                    },
                )
                print(
                    f"window {completed_windows}/{len(windows)}: "
                    f"chunks={len(chunks)} segments={len(window_segments)}",
                    flush=True,
                )

    daily = _build_daily_outputs(
        output_dir=output_dir,
        source=source,
        transcript_records=transcript_records,
        start_at=start_at,
        timezone_name=timezone_name,
        model_path=model_path,
        warnings=warnings,
        include_events=include_events,
    )
    summary = {
        "status": "completed",
        "source": str(source),
        "model": "Fun-ASR-Nano",
        "model_path": model_path,
        "vad_model_path": vad_model_path,
        "duration_s": duration_s,
        "window_seconds": window_seconds,
        "total_windows": len(windows),
        "completed_windows": completed_windows,
        "transcript_count": len(transcript_records),
        "window_summaries": window_summaries,
        "warnings": warnings,
        "event_source_policy": daily["event_source_policy"],
        "daily_outputs": daily,
    }
    _atomic_write_json(output_dir / "run-summary.json", summary)
    _atomic_write_json(
        checkpoint_path,
        {
            "status": "completed",
            "source": str(source),
            "model_path": model_path,
            "vad_model_path": vad_model_path,
            "duration_s": duration_s,
            "window_seconds": window_seconds,
            "max_chunk_seconds": max_chunk_seconds,
            "merge_gap_seconds": merge_gap_seconds,
            "batch_size_s": batch_size_s,
            "completed_windows": completed_windows,
            "total_windows": len(windows),
            "transcripts": transcript_records,
            "window_summaries": window_summaries,
            "warnings": warnings,
        },
    )
    return summary


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="run-full-funasr")
    parser.add_argument("--audio", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--model-path", required=True)
    parser.add_argument("--vad-model", required=True)
    parser.add_argument("--start-at", type=datetime.fromisoformat)
    parser.add_argument("--timezone", default="Asia/Shanghai")
    parser.add_argument("--window-seconds", type=float, default=1_800.0)
    parser.add_argument("--max-chunk-seconds", type=float, default=30.0)
    parser.add_argument("--merge-gap-seconds", type=float, default=1.5)
    parser.add_argument("--batch-size-s", type=float, default=120.0)
    parser.add_argument("--cpu-threads", type=int, default=2)
    parser.add_argument(
        "--allow-unattributed-events",
        action="store_true",
        help="opt in to generating an unfiltered event baseline",
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        summary = run_full_pipeline(
            source=args.audio,
            output_dir=args.output_dir,
            model_path=args.model_path,
            vad_model_path=args.vad_model,
            start_at=args.start_at,
            timezone_name=args.timezone,
            window_seconds=args.window_seconds,
            max_chunk_seconds=args.max_chunk_seconds,
            merge_gap_seconds=args.merge_gap_seconds,
            batch_size_s=args.batch_size_s,
            cpu_threads=args.cpu_threads,
            include_events=args.allow_unattributed_events,
        )
    except (RuntimeError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "status": summary["status"],
                "transcript_count": summary["transcript_count"],
                "completed_windows": summary["completed_windows"],
                "total_windows": summary["total_windows"],
                "warnings": len(summary["warnings"]),
                "daily_outputs": summary["daily_outputs"],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
