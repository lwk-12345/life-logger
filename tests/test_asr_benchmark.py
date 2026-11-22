from argparse import Namespace
from pathlib import Path

from scripts.asr_benchmark import (
    _infer_chunk,
    merge_vad_regions,
    next_chunk_index,
    runtime_metrics,
    whisper_command,
)


def test_merge_vad_regions_keeps_small_gaps_inside_a_safe_chunk() -> None:
    regions = [
        {"start_ms": 100, "end_ms": 1_000},
        {"start_ms": 1_400, "end_ms": 2_000},
        {"start_ms": 5_000, "end_ms": 5_500},
    ]

    chunks = merge_vad_regions(
        regions,
        merge_gap_seconds=0.5,
        max_chunk_seconds=30.0,
    )

    assert chunks == [
        {"start_s": 0.1, "end_s": 2.0},
        {"start_s": 5.0, "end_s": 5.5},
    ]


def test_merge_vad_regions_splits_when_merged_chunk_exceeds_limit() -> None:
    regions = [
        {"start_ms": 0, "end_ms": 20_000},
        {"start_ms": 20_100, "end_ms": 40_000},
    ]

    chunks = merge_vad_regions(
        regions,
        merge_gap_seconds=0.5,
        max_chunk_seconds=30.0,
    )

    assert chunks == [
        {"start_s": 0.0, "end_s": 20.0},
        {"start_s": 20.1, "end_s": 40.0},
    ]


def test_next_chunk_index_uses_checkpoint_progress_not_segment_count() -> None:
    chunks = [{"start_s": 0.0, "end_s": 1.0}] * 3
    checkpoint = {
        "model": "sensevoice",
        "audio": "sample.wav",
        "chunks": chunks,
        "completed_chunks": 2,
        "segments": [{"index": 0, "text": "第一段"}],
    }

    assert (
        next_chunk_index(
            checkpoint,
            model_name="sensevoice",
            audio_path="sample.wav",
            chunks=chunks,
        )
        == 2
    )


def test_funasr_chunk_inference_uses_a_file_path() -> None:
    class FakeModel:
        def __init__(self) -> None:
            self.input = None

        def generate(self, **kwargs):
            self.input = kwargs["input"]
            return [{"text": "测试"}]

    model = FakeModel()
    chunk_path = Path("chunk.wav")

    result = _infer_chunk(
        "fun-asr-nano",
        model,
        waveform=[0.0],
        audio_path=chunk_path,
    )

    assert result == {"text": "测试"}
    assert model.input == str(chunk_path)


def test_sensevoice_chunk_inference_requests_native_timestamps() -> None:
    class FakeModel:
        def __init__(self) -> None:
            self.kwargs = None

        def generate(self, **kwargs):
            self.kwargs = kwargs
            return [{"text": "测试"}]

    model = FakeModel()

    _infer_chunk("sensevoice", model, waveform=[0.0])

    assert model.kwargs["output_timestamp"] is True


def test_whisper_command_passes_no_speech_threshold_as_a_value() -> None:
    args = Namespace(
        model_path="model.bin",
        audio=Path("sample.wav"),
        vad_model=Path("vad.bin"),
        cpu_threads=2,
    )

    command = whisper_command(args)

    assert command[command.index("-nth") + 1] == "0.80"
    assert "-sns" not in command


def test_runtime_metrics_separate_model_load_from_inference() -> None:
    metrics = runtime_metrics(
        total_seconds=12.5,
        load_seconds=2.5,
        audio_duration_seconds=100.0,
    )

    assert metrics == {
        "total_seconds": 12.5,
        "inference_seconds": 10.0,
        "real_time_factor": 0.1,
    }
