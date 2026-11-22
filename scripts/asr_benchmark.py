"""Run repeatable, resumable local ASR comparisons for Life Logger."""

from __future__ import annotations

import argparse
import json
import os
import re
import resource
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

MODEL_NAMES = ("qwen3-asr", "fun-asr-nano", "sensevoice", "paraformer")
DEFAULT_MAX_CHUNK_SECONDS = 30.0
DEFAULT_MERGE_GAP_SECONDS = 1.5


def merge_vad_regions(
    regions: list[dict[str, Any]],
    *,
    merge_gap_seconds: float = DEFAULT_MERGE_GAP_SECONDS,
    max_chunk_seconds: float = DEFAULT_MAX_CHUNK_SECONDS,
) -> list[dict[str, float]]:
    """Merge nearby VAD regions without creating oversized model inputs."""

    if merge_gap_seconds < 0:
        raise ValueError("merge_gap_seconds must be non-negative")
    if max_chunk_seconds <= 0:
        raise ValueError("max_chunk_seconds must be greater than zero")

    normalized = []
    for region in regions:
        start = float(region["start_ms"]) / 1000.0
        end = float(region["end_ms"]) / 1000.0
        if end > start:
            normalized.append((start, end))
    normalized.sort()

    chunks: list[dict[str, float]] = []
    current: list[float] | None = None

    def flush() -> None:
        nonlocal current
        if current is not None:
            chunks.append(
                {
                    "start_s": round(current[0], 3),
                    "end_s": round(current[1], 3),
                }
            )
            current = None

    for start, end in normalized:
        if current is not None:
            gap = start - current[1]
            combined_duration = end - current[0]
            if gap <= merge_gap_seconds and combined_duration <= max_chunk_seconds:
                current[1] = end
                continue
            flush()

        while end - start > max_chunk_seconds:
            chunks.append(
                {
                    "start_s": round(start, 3),
                    "end_s": round(start + max_chunk_seconds, 3),
                }
            )
            start += max_chunk_seconds
        current = [start, end]

    flush()
    return chunks


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


def _safe_json(value: Any) -> Any:
    """Convert model-library objects into JSON-compatible data."""

    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _safe_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe_json(item) for item in value]
    if hasattr(value, "to_dict"):
        return _safe_json(value.to_dict())
    if hasattr(value, "__dict__"):
        return _safe_json(vars(value))
    if hasattr(value, "item"):
        try:
            return value.item()
        except (TypeError, ValueError):
            pass
    return str(value)


def _peak_rss_mib() -> float:
    value = float(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    if sys.platform == "darwin":
        value /= 1024 * 1024
    else:
        value /= 1024
    return round(value, 1)


def runtime_metrics(
    *,
    total_seconds: float,
    load_seconds: float,
    audio_duration_seconds: float,
) -> dict[str, float]:
    inference_seconds = max(total_seconds - load_seconds, 0.0)
    return {
        "total_seconds": round(total_seconds, 3),
        "inference_seconds": round(inference_seconds, 3),
        "real_time_factor": round(inference_seconds / audio_duration_seconds, 4)
        if audio_duration_seconds > 0
        else 0.0,
    }


def _load_waveform(path: Path) -> tuple[Any, int]:
    try:
        import soundfile as sf
    except ImportError as error:
        raise RuntimeError("benchmark workers require soundfile") from error

    waveform, sample_rate = sf.read(path, dtype="float32", always_2d=False)
    if getattr(waveform, "ndim", 1) == 2:
        waveform = waveform.mean(axis=1)
    if sample_rate != 16000:
        raise RuntimeError(f"expected 16kHz sample, got {sample_rate}Hz")
    return waveform, sample_rate


def _read_vad_chunks(
    vad_path: Path,
    *,
    merge_gap_seconds: float,
    max_chunk_seconds: float,
) -> list[dict[str, float]]:
    payload = json.loads(vad_path.read_text(encoding="utf-8"))
    regions = payload.get("regions", payload)
    if not isinstance(regions, list):
        raise ValueError(f"VAD file must contain a regions list: {vad_path}")
    return merge_vad_regions(
        regions,
        merge_gap_seconds=merge_gap_seconds,
        max_chunk_seconds=max_chunk_seconds,
    )


def _clean_sensevoice_text(text: str) -> str:
    return re.sub(r"<\|[^|]+\|>", "", text).strip()


def _sensevoice_tags(text: str) -> list[str]:
    return re.findall(r"<\|([^|]+)\|>", text)


def _model_text(model_name: str, raw: dict[str, Any]) -> tuple[str, str | None]:
    if model_name == "qwen3-asr":
        return str(raw.get("text", "")).strip(), raw.get("language")
    if model_name == "paraformer":
        preds = raw.get("preds", "")
        if isinstance(preds, (list, tuple)):
            text = preds[0] if preds else ""
        else:
            text = preds
        return str(text).strip(), raw.get("language")
    text = str(raw.get("text", "")).strip()
    if model_name == "sensevoice":
        text = _clean_sensevoice_text(text)
    return text, raw.get("language")


def _timestamp_type(model_name: str, raw: dict[str, Any]) -> str:
    if model_name == "qwen3-asr":
        return "coarse_vad"
    if model_name == "paraformer":
        return "native_character" if raw.get("timestamp") else "coarse_vad"
    if raw.get("timestamps") or raw.get("timestamp"):
        return "native_token"
    return "coarse_vad"


def _load_model(model_name: str, model_path: str, *, cpu_threads: int) -> Any:
    if model_name == "qwen3-asr":
        import torch
        from qwen_asr import Qwen3ASRModel

        return Qwen3ASRModel.from_pretrained(
            model_path,
            dtype=torch.bfloat16,
            device_map="cpu",
            max_inference_batch_size=1,
            max_new_tokens=512,
        )

    if model_name in {"sensevoice", "fun-asr-nano"}:
        from funasr import AutoModel

        return AutoModel(
            model=model_path,
            device="cpu",
            ncpu=cpu_threads,
            disable_update=True,
            disable_pbar=True,
        )

    if model_name == "paraformer":
        from funasr_onnx import Paraformer

        return Paraformer(
            model_path,
            batch_size=1,
            quantize=True,
            intra_op_num_threads=cpu_threads,
        )

    raise ValueError(f"unsupported model: {model_name}")


def _infer_chunk(
    model_name: str,
    model: Any,
    waveform: Any,
    *,
    audio_path: Path | None = None,
) -> dict[str, Any]:
    if model_name == "qwen3-asr":
        result = model.transcribe(
            audio=(waveform, 16000),
            language="Chinese",
            return_time_stamps=False,
        )[0]
        return {
            "language": result.language,
            "text": result.text,
            "timestamps": result.time_stamps,
        }

    if model_name in {"sensevoice", "fun-asr-nano"}:
        generate_kwargs = {
            "input": str(audio_path) if audio_path is not None else waveform,
            "language": "zh",
            "use_itn": True,
            "return_raw_text": True,
        }
        if model_name == "sensevoice":
            generate_kwargs["output_timestamp"] = True
        result = model.generate(
            **generate_kwargs,
        )[0]
        return _safe_json(result)

    result = model(waveform)[0]
    return _safe_json(result)


def _infer_chunk_with_temp_audio(
    model_name: str,
    model: Any,
    waveform: Any,
) -> dict[str, Any]:
    if model_name not in {"sensevoice", "fun-asr-nano"}:
        return _infer_chunk(model_name, model, waveform)

    try:
        import soundfile as sf
    except ImportError as error:
        raise RuntimeError("FunASR benchmark workers require soundfile") from error

    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as handle:
            temporary_path = Path(handle.name)
        sf.write(temporary_path, waveform, 16000)
        return _infer_chunk(
            model_name,
            model,
            waveform,
            audio_path=temporary_path,
        )
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _build_life_events(
    model_name: str,
    segments: list[dict[str, Any]],
    *,
    start_at: datetime | None,
) -> list[dict[str, Any]]:
    repo_root = Path(__file__).resolve().parents[1]
    src_dir = repo_root / "src"
    if str(src_dir) not in sys.path:
        sys.path.insert(0, str(src_dir))
    try:
        from life_logger.domain import TranscriptSegment
        from life_logger.extract import RuleBasedExtractor
        from life_logger.segment import split_utterances
    except ImportError:
        return []

    recording_id = f"asr_benchmark_{model_name}"
    extractor = RuleBasedExtractor()
    events: list[dict[str, Any]] = []
    for index, item in enumerate(segments, start=1):
        absolute = None
        if start_at is not None:
            absolute = (start_at + timedelta(seconds=item["start_s"])).isoformat()
        transcript = TranscriptSegment(
            segment_id=f"{recording_id}_seg_{index:04d}",
            recording_id=recording_id,
            start_offset_seconds=item["start_s"],
            end_offset_seconds=item["end_s"],
            absolute_started_at=absolute,
            absolute_ended_at=(
                (start_at + timedelta(seconds=item["end_s"])).isoformat()
                if start_at is not None
                else None
            ),
            text=item["text"],
            source=f"benchmark:{model_name}",
            language=item.get("language") or "zh",
            transcription_confidence=None,
        )
        for utterance in split_utterances(transcript):
            events.extend(extractor.extract(utterance))
    return [event.to_dict() for event in events]


def _checkpoint_matches(
    payload: dict[str, Any],
    *,
    model_name: str,
    audio_path: Path,
    chunks: list[dict[str, float]],
) -> bool:
    return (
        payload.get("model") == model_name
        and payload.get("audio") == str(audio_path)
        and payload.get("chunks") == chunks
    )


def next_chunk_index(
    payload: dict[str, Any],
    *,
    model_name: str,
    audio_path: str | Path,
    chunks: list[dict[str, float]],
) -> int:
    """Return the first unfinished chunk represented by a valid checkpoint."""

    if not _checkpoint_matches(
        payload,
        model_name=model_name,
        audio_path=Path(audio_path),
        chunks=chunks,
    ):
        return 0
    return min(max(int(payload.get("completed_chunks", 0)), 0), len(chunks))


def run_model(
    *,
    model_name: str,
    model_path: str,
    audio_path: Path,
    vad_path: Path,
    output_dir: Path,
    start_at: datetime | None,
    merge_gap_seconds: float,
    max_chunk_seconds: float,
    cpu_threads: int,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    checkpoint_path = output_dir / "checkpoint.json"
    result_path = output_dir / "result.json"
    waveform, sample_rate = _load_waveform(audio_path)
    chunks = _read_vad_chunks(
        vad_path,
        merge_gap_seconds=merge_gap_seconds,
        max_chunk_seconds=max_chunk_seconds,
    )
    checkpoint: dict[str, Any] = {}
    if checkpoint_path.exists():
        try:
            candidate = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            candidate = {}
        if _checkpoint_matches(
            candidate,
            model_name=model_name,
            audio_path=audio_path,
            chunks=chunks,
        ):
            checkpoint = candidate

    segments = list(checkpoint.get("segments", []))
    raw_results = list(checkpoint.get("raw_results", []))
    errors = list(checkpoint.get("errors", []))
    completed_chunks = next_chunk_index(
        checkpoint,
        model_name=model_name,
        audio_path=audio_path,
        chunks=chunks,
    )
    started = time.perf_counter()
    load_seconds = None
    model = None
    status = "running"

    try:
        load_started = time.perf_counter()
        model = _load_model(model_name, model_path, cpu_threads=cpu_threads)
        load_seconds = round(time.perf_counter() - load_started, 3)
    except Exception as error:
        status = "failed_to_load"
        errors.append({"stage": "load", "error": repr(error)})

    if model is not None:
        for index in range(completed_chunks, len(chunks)):
            chunk = chunks[index]
            start_index = max(0, int(round(chunk["start_s"] * sample_rate)))
            end_index = min(len(waveform), int(round(chunk["end_s"] * sample_rate)))
            item_base = {
                "index": index,
                "start_s": chunk["start_s"],
                "end_s": chunk["end_s"],
            }
            try:
                raw = _infer_chunk_with_temp_audio(
                    model_name,
                    model,
                    waveform[start_index:end_index],
                )
                raw = _safe_json(raw)
                text, language = _model_text(model_name, raw)
                if text:
                    item = {
                        **item_base,
                        "text": text,
                        "raw_text": raw.get("text", text),
                        "language": language or "zh",
                        "timestamp_type": _timestamp_type(model_name, raw),
                    }
                    if model_name == "sensevoice":
                        item["sensevoice_tags"] = _sensevoice_tags(
                            str(raw.get("text", ""))
                        )
                    segments.append(item)
                raw_results.append({**item_base, "raw": raw})
            except Exception as error:
                errors.append({**item_base, "stage": "inference", "error": repr(error)})
                raw_results.append({**item_base, "raw": None})

            _atomic_write_json(
                checkpoint_path,
                {
                    "model": model_name,
                    "audio": str(audio_path),
                    "chunks": chunks,
                    "completed_chunks": index + 1,
                    "segments": segments,
                    "raw_results": raw_results,
                    "errors": errors,
                },
            )
            completed_chunks = index + 1

        status = "completed" if not errors else "completed_with_errors"

    total_seconds = round(time.perf_counter() - started, 3)
    metrics = runtime_metrics(
        total_seconds=total_seconds,
        load_seconds=load_seconds or 0.0,
        audio_duration_seconds=len(waveform) / sample_rate,
    )
    downstream_input = {
        "contract": "life_logger_transcript_to_events_v1",
        "instruction": (
            "从带时间范围的 ASR 片段中提取有证据的生活事件；只使用原文，"
            "不补写录音没有说出的事实。"
        ),
        "categories": [
            "routine",
            "food",
            "expense",
            "exercise",
            "work_communication",
            "idea",
            "todo",
        ],
        "asr_model": model_name,
        "segments": segments,
    }
    _atomic_write_json(output_dir / "downstream-input.json", downstream_input)
    events = _build_life_events(model_name, segments, start_at=start_at)
    payload = {
        "status": status,
        "model": model_name,
        "model_path": model_path,
        "audio": str(audio_path),
        "vad": str(vad_path),
        "audio_duration_s": round(len(waveform) / sample_rate, 3),
        "chunk_count": len(chunks),
        "completed_chunks": completed_chunks,
        "speech_input_s": round(
            sum(item["end_s"] - item["start_s"] for item in chunks), 3
        ),
        "load_seconds": load_seconds,
        "total_seconds": metrics["total_seconds"],
        "inference_seconds": metrics["inference_seconds"],
        "real_time_factor": metrics["real_time_factor"],
        "peak_rss_mib": _peak_rss_mib(),
        "segment_count": len(segments),
        "segments": segments,
        "raw_results": raw_results,
        "events_rule_baseline": events,
        "errors": errors,
    }
    _atomic_write_json(result_path, payload)
    return payload


def whisper_command(args: argparse.Namespace) -> list[str]:
    return [
        "whisper-cli",
        "-m",
        args.model_path,
        "-f",
        str(args.audio),
        "-l",
        "zh",
        "-t",
        str(args.cpu_threads),
        "-p",
        "1",
        "-np",
        "--vad",
        "--vad-model",
        str(args.vad_model),
        "--vad-threshold",
        "0.70",
        "--vad-min-speech-duration-ms",
        "500",
        "--vad-min-silence-duration-ms",
        "300",
        "--vad-max-speech-duration-s",
        "30",
        "--vad-speech-pad-ms",
        "30",
        "-nth",
        "0.80",
    ]


def _run_whisper_cpp(args: argparse.Namespace) -> int:
    args.output_dir.mkdir(parents=True, exist_ok=True)
    output_prefix = args.output_dir / "whisper-large-v3-turbo"
    command = whisper_command(args)
    command.extend(["-ojf", "-of", str(output_prefix)])
    started = time.perf_counter()
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    elapsed = round(time.perf_counter() - started, 3)
    (args.output_dir / "whisper.log").write_text(
        completed.stdout + "\n" + completed.stderr,
        encoding="utf-8",
    )
    result_path = Path(f"{output_prefix}.json")
    payload = {
        "status": "completed" if completed.returncode == 0 else "failed",
        "model": "whisper-large-v3-turbo",
        "audio": str(args.audio),
        "inference_seconds": elapsed,
        "returncode": completed.returncode,
        "json_path": str(result_path),
    }
    _atomic_write_json(args.output_dir / "whisper-metadata.json", payload)
    return completed.returncode


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="asr-benchmark")
    parser.add_argument("--model", choices=MODEL_NAMES + ("whisper-large-v3-turbo",))
    parser.add_argument("--model-path", required=True)
    parser.add_argument("--audio", type=Path, required=True)
    parser.add_argument("--vad", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--start-at", type=datetime.fromisoformat)
    parser.add_argument(
        "--merge-gap-seconds",
        type=float,
        default=DEFAULT_MERGE_GAP_SECONDS,
    )
    parser.add_argument(
        "--max-chunk-seconds",
        type=float,
        default=DEFAULT_MAX_CHUNK_SECONDS,
    )
    parser.add_argument("--cpu-threads", type=int, default=2)
    parser.add_argument("--vad-model", type=Path)
    return parser


def main() -> int:
    args = _parser().parse_args()
    if args.model == "whisper-large-v3-turbo":
        if args.vad_model is None:
            raise SystemExit("--vad-model is required for whisper.cpp")
        return _run_whisper_cpp(args)
    payload = run_model(
        model_name=args.model,
        model_path=args.model_path,
        audio_path=args.audio,
        vad_path=args.vad,
        output_dir=args.output_dir,
        start_at=args.start_at,
        merge_gap_seconds=args.merge_gap_seconds,
        max_chunk_seconds=args.max_chunk_seconds,
        cpu_threads=args.cpu_threads,
    )
    print(
        json.dumps(
            {
                key: payload[key]
                for key in (
                    "status",
                    "model",
                    "chunk_count",
                    "segment_count",
                    "load_seconds",
                    "inference_seconds",
                    "real_time_factor",
                    "peak_rss_mib",
                    "errors",
                )
            },
            ensure_ascii=False,
        )
    )
    return 0 if payload["status"].startswith("completed") else 2


if __name__ == "__main__":
    raise SystemExit(main())
