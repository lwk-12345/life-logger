"""Regression contract for permanent decision-layer removal (all inference is fake)."""
from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path

import pytest

from life_logger import contextual_extraction as extraction
from life_logger.decision_history import collect_comparisons, read_kev_section, read_kev_annotation


class Response:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return json.dumps(self.payload).encode()


def source(label="self", proximity="near", content_class=None):
    row = {
        "segment_id": "segment-1", "recording_id": "recording-1",
        "text": "今天下楼散步。", "source_label": label,
        "absolute_started_at": "2026-11-06T08:00:00+08:00",
        "start_offset_seconds": 0, "end_offset_seconds": 3,
        "acoustic_proximity": proximity,
    }
    if content_class:
        row["audio_content_class"] = content_class
    return row


def configure_old_enabled_settings(tmp_path, monkeypatch):
    path = tmp_path / "local-config.json"
    path.write_text(json.dumps({
        "deepseek": {"base_url": "http://extractor.invalid/v1", "model": "extractor-test"},
        "decision_comparison": {"enabled": True, "providers": ["jev", "clef"]},
        "kev": {"annotation_mode": "dimensions", "event_gate": True,
                "base_url": "http://decision.invalid:8000"},
        "jev": {"enabled_by_default": True},
        "decision_shadow": {"enabled": True},
    }))
    monkeypatch.setenv("LIFE_LOGGER_LOCAL_CONFIG", str(path))
    monkeypatch.setenv("LIFE_LOGGER_INTENT_POOL_PATH", str(tmp_path / "intents.json"))


def test_new_extraction_only_calls_extractor_even_with_old_flags_enabled(tmp_path, monkeypatch):
    configure_old_enabled_settings(tmp_path, monkeypatch)
    calls = []

    def fake_network(request, **kwargs):
        assert request.full_url == "http://extractor.invalid/v1/chat/completions"
        body = json.loads(request.data)
        assert body["model"] == "extractor-test"
        prompt = body["messages"][0]["content"]
        assert "decision_hints" not in prompt
        assert "kev_annotation" not in prompt
        packets = json.loads(prompt.rsplit("输入记录：\n", 1)[1])
        assert all("decision_hints" not in row and "kev_annotation" not in row for row in packets)
        calls.append(packets)
        return Response({"choices": [{"message": {"content": json.dumps({
            "records": [{"source_id": row["source_id"], "is_event": False, "events": []}
                        for row in packets]
        })}}]})

    monkeypatch.setattr("urllib.request.urlopen", fake_network)
    rows = [source(label) for label in ("self", "non_self", "unknown")]
    for index, row in enumerate(rows):
        row["segment_id"] = f"segment-{index}"
    result = extraction.run_contextual_extraction(
        rows, deepseek_api_key="test-key", deepseek_opener=fake_network,
    )
    assert len(calls) == 1
    assert len(calls[0]) == 3
    assert result["counts"]["input_records"] == 3
    assert len(result["source_outcomes"]) == 3
    assert all(item["semantic_status"] == "no_event" for item in result["source_outcomes"])
    assert not ({"kev", "kev_gate", "decision_annotations", "comparison_mode", "variants"} & result.keys())
    assert not hasattr(extraction, "_annotate_kev")


@pytest.mark.parametrize("label,proximity,content_class,skipped", [
    ("non_self", "far", "stream", True),
    ("non_self", "far", "music", True),
    ("non_self", "near", "stream", False),
    ("self", "far", "music", False),
    ("unknown", "far", "stream", False),
    ("non_self", "far", None, False),
])
def test_acoustic_pure_media_guard_survives(tmp_path, monkeypatch, label, proximity, content_class, skipped):
    configure_old_enabled_settings(tmp_path, monkeypatch)
    calls = []

    def fake_network(request, **kwargs):
        assert request.full_url == "http://extractor.invalid/v1/chat/completions"
        calls.append(request)
        return Response({"choices": [{"message": {"content": '{"records":[]}'}}]})

    result = extraction.run_contextual_extraction(
        [source(label, proximity, content_class)],
        deepseek_api_key="test-key", deepseek_opener=fake_network,
    )
    assert len(calls) == (0 if skipped else 1)
    assert len(result["source_outcomes"]) == 1
    assert (result["batches"][0].get("skipped_reason") == "non_self_media_stream") is skipped


def test_new_upload_job_ignores_persisted_decision_options(tmp_path, monkeypatch):
    from life_logger import local_pipeline
    from life_logger.local_web import JobStore
    from scripts import local_jobs

    monkeypatch.setattr(local_pipeline, "PROJECT_ROOT", tmp_path)
    store = JobStore(tmp_path / "local-web", recover=False)
    job_id = store.create_job(processing_options={"use_jev": True, "decision_provider": "kev"})
    assert store.read(job_id)["processing_options"] == {
        "asr_model": "mlx-whisper-turbo", "speaker_model": "cam++",
    }
    store.add_file(job_id, filename="voice.wav", relative_path="voice.wav", stream=io.BytesIO(b"fake-audio"))
    store.prepare_for_start(job_id)
    # A pre-removal saved job must also be unable to revive the old stage.
    state = store.read(job_id)
    state["processing_options"].update({"use_jev": True, "decision_provider": "kev"})
    store.status_path(job_id).write_text(json.dumps(state))
    commands = []

    def fake_command(command, **kwargs):
        commands.append(command)
        assert len(commands) <= 2
        if len(commands) == 1:
            assert "run_cloud_asr_pipeline.py" in str(command[1])
            day = Path(command[command.index("--output-dir") + 1]) / "2026-11-06"
            day.mkdir(parents=True)
            (day / "transcript.json").write_text('[{"text":"今天散步"}]')
            (day / "recordings.json").write_text("[]")
        else:
            assert "attribute_funasr_transcript.py" in str(command[1])
            result = Path(command[command.index("--output-dir") + 1])
            result.mkdir(parents=True, exist_ok=True)
            (result / "events.json").write_text("[]")
        return 0

    def forbidden_network(*args, **kwargs):
        raise AssertionError("A decision/health request escaped the fake job")

    monkeypatch.setattr(local_jobs, "run_command", fake_command)
    monkeypatch.setattr("urllib.request.urlopen", forbidden_network)
    events = []

    def callback(event):
        events.append(event)
        store.update_from_worker(job_id, event)

    local_jobs.run_web_job(store.job_path(job_id), {"python": "python", "asr": "asr", "vad": "vad", "speaker": "cam++", "template": "template"}, callback)
    assert len(commands) == 2
    assert store.read(job_id)["status"] == "completed"
    assert not any(event.get("stage") == "jev" or event["type"] == "decision_provider_resolved" for event in events)


def test_history_is_disabled_read_only_and_preserves_values(tmp_path):
    path = tmp_path / "jobs/job-old/files/file/result/decision-comparison.json"
    path.parent.mkdir(parents=True)
    report = {"status": "completed", "variants": {"jev": {
        "status": "completed", "counts": {"formal_events": 1},
        "formal_events": [{"event_id": "old-event", "data": {"expense": {"amount": 43, "currency": "CNY"}}}],
        "pending_sources": [{"source_id": "unresolved-old", "quote": "原话"}],
    }}}
    path.write_text(json.dumps(report))
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    payload = collect_comparisons(tmp_path)
    assert payload["enabled"] is False
    assert payload["read_only"] is True
    assert payload["label"] == "已停用"
    assert payload["items"][0]["variants"]["jev"]["formal_events"] == report["variants"]["jev"]["formal_events"]
    assert payload["items"][0]["variants"]["jev"]["pending_sources"] == report["variants"]["jev"]["pending_sources"]
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    assert read_kev_section({"laya": {"status": "degraded"}}) == {"status": "degraded"}
    assert read_kev_annotation(
        {"laya_annotation": {"category": "food", "event_state": "completed"}}
    ) == {"category": "food", "event_state": "completed"}
