import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from life_logger import transcribe as transcribe_module
from life_logger.domain import RecordingSegment
from life_logger.transcribe import (
    DemoTranscriber,
    JsonSidecarTranscriber,
    LocalWhisperTranscriber,
    SrtVttSidecarTranscriber,
    TextSidecarTranscriber,
)


def recording(path: Path) -> RecordingSegment:
    return RecordingSegment(
        recording_id="rec_test",
        source_file=str(path),
        started_at="2026-09-02T08:00:00+08:00",
        ended_at=None,
        duration_seconds=None,
        sequence=1,
        time_source="user",
        time_precision="exact",
        ingestion_warnings=[],
    )


def test_demo_transcriber_reads_timestamped_fixture() -> None:
    result = DemoTranscriber(Path("fixtures/demo_transcript.json")).transcribe(
        recording(Path("demo.m4a"))
    )

    assert len(result) >= 7
    assert result[0].source == "demo"
    assert result[0].text


def test_json_sidecar_transcriber_maps_offsets(tmp_path: Path) -> None:
    audio = tmp_path / "recording.m4a"
    sidecar = tmp_path / "recording.json"
    audio.write_bytes(b"audio")
    sidecar.write_text(
        json.dumps(
            {
                "segments": [
                    {"start": 2.0, "end": 4.5, "text": "我刚起床。"},
                ]
            }
        ),
        encoding="utf-8",
    )

    result = JsonSidecarTranscriber().transcribe(recording(audio), sidecar)

    assert result[0].start_offset_seconds == 2.0
    assert result[0].text == "我刚起床。"
    assert result[0].source == "sidecar"


def test_srt_sidecar_transcriber_maps_timecodes(tmp_path: Path) -> None:
    audio = tmp_path / "recording.m4a"
    sidecar = tmp_path / "recording.srt"
    audio.write_bytes(b"audio")
    sidecar.write_text(
        "1\n00:00:02,000 --> 00:00:04,500\n我刚起床。\n",
        encoding="utf-8",
    )

    result = SrtVttSidecarTranscriber().transcribe(recording(audio), sidecar)

    assert result[0].start_offset_seconds == 2.0
    assert result[0].end_offset_seconds == 4.5


def test_text_sidecar_transcriber_keeps_relative_time(tmp_path: Path) -> None:
    audio = tmp_path / "recording.m4a"
    sidecar = tmp_path / "recording.txt"
    audio.write_bytes(b"audio")
    sidecar.write_text("我刚起床。\n记录一下，喝了一杯水。\n", encoding="utf-8")

    result = TextSidecarTranscriber().transcribe(recording(audio), sidecar)

    assert result[0].absolute_started_at == "2026-09-02T08:00:00+08:00"
    assert result[0].start_offset_seconds == 0.0
    assert result[0].text == "我刚起床。"
    assert result[1].start_offset_seconds >= result[0].end_offset_seconds


def test_missing_local_asr_dependency_has_actionable_error() -> None:
    with pytest.raises(RuntimeError, match="faster-whisper"):
        LocalWhisperTranscriber(model_name="small")._load_model(force_missing=True)


def test_local_whisper_defaults_to_limited_cpu_threads() -> None:
    transcriber = LocalWhisperTranscriber(model_name="small")

    assert transcriber.cpu_threads == 2


def test_local_whisper_processes_long_audio_in_chunks_with_global_offsets(
    tmp_path: Path,
    monkeypatch,
) -> None:
    audio = tmp_path / "long.m4a"
    audio.write_bytes(b"audio")
    rendered: list[tuple[float, float]] = []

    class FakeModel:
        def transcribe(self, path: str, language: str, vad_filter: bool):
            assert language == "zh"
            assert vad_filter is True
            return [SimpleNamespace(start=1.0, end=2.0, text="一段话")], None

    def fake_render(
        source: str,
        start: float,
        duration: float,
        destination: Path,
    ) -> None:
        rendered.append((start, duration))
        destination.write_bytes(b"wav")

    monkeypatch.setattr(transcribe_module, "_probe_duration", lambda path: 25.0)
    monkeypatch.setattr(transcribe_module, "_render_audio_chunk", fake_render)

    transcriber = LocalWhisperTranscriber(
        model_name="small",
        chunk_seconds=10.0,
        checkpoint_dir=tmp_path / "checkpoints",
    )
    monkeypatch.setattr(transcriber, "_load_model", lambda: FakeModel())

    result = transcriber.transcribe(recording(audio))

    assert rendered == [(0.0, 10.0), (10.0, 10.0), (20.0, 5.0)]
    assert [item.start_offset_seconds for item in result] == [1.0, 11.0, 21.0]
    assert [item.text for item in result] == ["一段话", "一段话", "一段话"]


def test_local_whisper_resumes_from_completed_chunk_checkpoint(
    tmp_path: Path,
    monkeypatch,
) -> None:
    audio = tmp_path / "long.m4a"
    audio.write_bytes(b"audio")
    rendered: list[float] = []
    calls = 0

    class FlakyModel:
        def transcribe(self, path: str, language: str, vad_filter: bool):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("simulated interruption")
            return [SimpleNamespace(start=0.0, end=1.0, text=f"片段{calls}")], None

    def fake_render(
        source: str,
        start: float,
        duration: float,
        destination: Path,
    ) -> None:
        rendered.append(start)
        destination.write_bytes(b"wav")

    monkeypatch.setattr(transcribe_module, "_probe_duration", lambda path: 20.0)
    monkeypatch.setattr(transcribe_module, "_render_audio_chunk", fake_render)
    checkpoint_dir = tmp_path / "checkpoints"
    first = LocalWhisperTranscriber(
        model_name="small",
        chunk_seconds=10.0,
        checkpoint_dir=checkpoint_dir,
    )
    monkeypatch.setattr(first, "_load_model", lambda: FlakyModel())

    with pytest.raises(RuntimeError, match="simulated interruption"):
        first.transcribe(recording(audio))

    class StableModel:
        def transcribe(self, path: str, language: str, vad_filter: bool):
            return [SimpleNamespace(start=0.0, end=1.0, text="恢复片段")], None

    second = LocalWhisperTranscriber(
        model_name="small",
        chunk_seconds=10.0,
        checkpoint_dir=checkpoint_dir,
    )
    monkeypatch.setattr(second, "_load_model", lambda: StableModel())

    result = second.transcribe(recording(audio))

    assert rendered == [0.0, 10.0, 10.0]
    assert [item.text for item in result] == ["片段1", "恢复片段"]
    assert [item.start_offset_seconds for item in result] == [0.0, 10.0]


def test_doubao_transcriber_parses_utterances(monkeypatch, tmp_path: Path) -> None:
    from life_logger.transcribe import DoubaoASRTranscriber

    audio = tmp_path / "sample.wav"
    audio.write_bytes(b"dummy")

    class FakeResponse:
        def read(self):
            return json.dumps({
                "result": {
                    "text": "测试整句",
                    "utterances": [
                        {"start_time": 1000, "end_time": 3500, "text": "第一句话", "additions": {"speaker": "1"}},
                        {"start_time": 4000, "end_time": 6000, "text": "第二句话", "additions": {"speaker": "2"}},
                    ]
                }
            }).encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=60: FakeResponse())

    transcriber = DoubaoASRTranscriber(appid="test", access_token="token")
    result = transcriber.transcribe(recording(audio))

    assert len(result) == 2
    assert result[0].text == "第一句话"
    assert result[0].start_offset_seconds == 1.0
    assert result[0].end_offset_seconds == 3.5
    assert result[0].source == "doubao_asr"
    assert result[0].speaker == "1"
    assert result[1].text == "第二句话"
    assert result[1].start_offset_seconds == 4.0
    assert result[1].end_offset_seconds == 6.0
    assert result[1].speaker == "2"

