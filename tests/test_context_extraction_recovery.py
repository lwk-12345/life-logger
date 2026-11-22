import json
import os
import shutil
import sys
import threading
from pathlib import Path

import pytest

from life_logger import context_extraction_recovery as recovery
from life_logger.local_web import JobStore


def _write_checkpoint_tree(job_dir: Path, file_id: str, *, status: str = "degraded"):
    file_root = job_dir / "files" / file_id
    result_dir = file_root / "result"
    asr_day = file_root / "asr" / "2026-10-06"
    result_dir.mkdir(parents=True)
    asr_day.mkdir(parents=True)
    transcript = [
        {
            "segment_id": "seg-1",
            "recording_id": "rec-1",
            "start_offset_seconds": 0.0,
            "end_offset_seconds": 1.0,
            "absolute_started_at": "2026-10-06T09:00:00+08:00",
            "absolute_ended_at": "2026-10-06T09:00:01+08:00",
            "text": "test phrase",
            "source": "local_funasr_nano",
            "language": "zh",
            "transcription_confidence": None,
        }
    ]
    recordings = [
        {
            "recording_id": "rec-1",
            "source_file": "recording.m4a",
            "started_at": "2026-10-06T09:00:00+08:00",
            "ended_at": "2026-10-06T09:00:01+08:00",
            "duration_seconds": 1.0,
            "sequence": 0,
            "time_source": "metadata",
            "time_precision": "exact",
            "ingestion_warnings": [],
        }
    ]
    attribution = [{**transcript[0], "source_label": "self"}]
    (asr_day / "transcript.json").write_text(json.dumps(transcript), encoding="utf-8")
    (asr_day / "recordings.json").write_text(json.dumps(recordings), encoding="utf-8")
    (result_dir / "source-attribution.json").write_text(
        json.dumps(attribution), encoding="utf-8"
    )
    (result_dir / "context-extraction.json").write_text(
        json.dumps({"status": status, "formal_events": [], "batches": []}),
        encoding="utf-8",
    )
    day_dir = result_dir / "2026-10-06"
    day_dir.mkdir()
    (day_dir / "events.json").write_text("[]", encoding="utf-8")
    (day_dir / "daily_summary.json").write_text("{}", encoding="utf-8")
    return {
        "file_id": file_id,
        "result_dir": result_dir,
        "attribution_path": result_dir / "source-attribution.json",
        "transcript_path": asr_day / "transcript.json",
        "recordings_path": asr_day / "recordings.json",
        "extraction_status": status,
    }


def _state_for_target(target: dict, *, status: str = "completed") -> dict:
    return {
        "job_id": "job-context-recovery",
        "status": status,
        "timezone": "Asia/Shanghai",
        "files": [
            {
                "sha256": target["file_id"],
                "stored_path": f"content/{target['file_id']}.m4a",
                "status": "completed",
                "results": [
                    f"files/{target['file_id']}/result/context-extraction.json"
                ],
            }
        ],
        "results": [f"files/{target['file_id']}/result/context-extraction.json"],
    }


def test_target_discovery_only_selects_degraded_or_failed_with_checkpoints(tmp_path):
    job_dir = tmp_path / "job"
    failed = _write_checkpoint_tree(job_dir, "a" * 64, status="failed")
    _write_checkpoint_tree(job_dir, "b" * 64, status="completed")
    missing = _write_checkpoint_tree(job_dir, "c" * 64, status="degraded")
    missing["attribution_path"].unlink()
    state = {
        "files": [
            {"sha256": "a" * 64},
            {"sha256": "b" * 64},
            {"sha256": "c" * 64},
        ]
    }

    targets, skipped = recovery.find_context_recovery_targets(job_dir, state)

    assert [target["file_id"] for target in targets] == [failed["file_id"]]
    assert skipped == 1


def test_target_discovery_rejects_duplicate_or_mismatched_checkpoint_rows(tmp_path):
    job_dir = tmp_path / "job"
    duplicate = _write_checkpoint_tree(job_dir, "1" * 64, status="degraded")
    duplicate["transcript_path"].write_text(
        json.dumps(
            [
                {
                    "segment_id": "seg-1",
                    "recording_id": "rec-1",
                    "start_offset_seconds": 0.0,
                    "end_offset_seconds": 1.0,
                    "absolute_started_at": "2026-10-06T09:00:00+08:00",
                    "absolute_ended_at": "2026-10-06T09:00:01+08:00",
                    "text": "test phrase",
                },
                {"segment_id": "seg-1", "recording_id": "rec-1", "text": "duplicate"},
            ]
        ),
        encoding="utf-8",
    )
    mismatched = _write_checkpoint_tree(job_dir, "2" * 64, status="failed")
    attribution = json.loads(mismatched["attribution_path"].read_text())
    attribution[0]["start_offset_seconds"] = 2.0
    mismatched["attribution_path"].write_text(json.dumps(attribution), encoding="utf-8")

    targets, skipped = recovery.find_context_recovery_targets(
        job_dir,
        {"files": [{"sha256": "1" * 64}, {"sha256": "2" * 64}]},
    )

    assert targets == []
    assert skipped == 2


def test_context_retry_stages_output_and_preserves_published_results(
    tmp_path, monkeypatch
):
    job_dir = tmp_path / "job"
    target = _write_checkpoint_tree(job_dir, "a" * 64, status="failed")
    source_before = target["attribution_path"].read_bytes()
    original_context = json.loads(
        (target["result_dir"] / "context-extraction.json").read_text()
    )

    class FakeProcess:
        stdout = iter(["context batch 1/2 running\n", "context batch 1/2 completed\n"])

        def wait(self):
            return 0

    def fake_popen(command, **_kwargs):
        output_dir = Path(command[command.index("--output-dir") + 1])
        (output_dir / "context-extraction.json").write_text(
            json.dumps({"status": "completed", "formal_events": [], "batches": []}),
            encoding="utf-8",
        )
        return FakeProcess()

    monkeypatch.setattr(recovery.subprocess, "Popen", fake_popen)
    progress = []
    prepared = recovery.prepare_context_recovery_target(
        job_dir=job_dir,
        target=target,
        recovery_id="a" * 32,
        config={"python": sys.executable},
        env={"DEEPSEEK_API_KEY": "test-key"},
        timezone_name="Asia/Shanghai",
        dictation_windows=None,
        progress_callback=progress.append,
    )

    archived = recovery.publish_context_recovery_target(job_dir, prepared)

    assert progress == [
        {"batch": 1, "total_batches": 2, "status": "running"},
        {"batch": 1, "total_batches": 2, "status": "completed"},
    ]
    assert (
        json.loads((target["result_dir"] / "context-extraction.json").read_text())[
            "status"
        ]
        == "completed"
    )
    assert (
        target["result_dir"] / "source-attribution.json"
    ).read_bytes() == source_before
    archived_context = next(
        path for path in archived if path.endswith("context-extraction.json")
    )
    assert json.loads((job_dir / archived_context).read_text()) == original_context
    assert any(path.endswith("daily_summary.json") for path in archived)
    assert any(path.endswith("events.json") for path in archived)


def test_failed_context_retry_does_not_replace_old_results(tmp_path, monkeypatch):
    job_dir = tmp_path / "job"
    target = _write_checkpoint_tree(job_dir, "a" * 64, status="failed")
    original = (target["result_dir"] / "context-extraction.json").read_bytes()

    class FakeProcess:
        stdout = iter(["context batch 1/1 failed\n"])

        def wait(self):
            return 1

    monkeypatch.setattr(
        recovery.subprocess, "Popen", lambda *_args, **_kwargs: FakeProcess()
    )
    with pytest.raises(RuntimeError, match="command failed"):
        recovery.prepare_context_recovery_target(
            job_dir=job_dir,
            target=target,
            recovery_id="b" * 32,
            config={"python": sys.executable},
            env={"DEEPSEEK_API_KEY": "test-key"},
            timezone_name="Asia/Shanghai",
            dictation_windows=None,
            progress_callback=lambda _progress: None,
        )

    assert (target["result_dir"] / "context-extraction.json").read_bytes() == original
    assert not (
        target["result_dir"].parent / f".result.context-recovery-{'b' * 32}"
    ).exists()


def test_tree_signature_detects_same_size_edit_with_restored_mtime(tmp_path):
    result_dir = tmp_path / "result"
    result_dir.mkdir()
    artifact = result_dir / "context-extraction.json"
    artifact.write_text("failed", encoding="utf-8")
    before = recovery._tree_signature(result_dir)
    stat_before = artifact.stat()

    artifact.write_text("manual", encoding="utf-8")
    os.utime(artifact, ns=(stat_before.st_atime_ns, stat_before.st_mtime_ns))

    assert artifact.stat().st_size == stat_before.st_size
    assert artifact.stat().st_mtime_ns == stat_before.st_mtime_ns
    assert recovery._tree_signature(result_dir) != before


def test_publication_recovery_rolls_forward_only_matching_staged_tree(tmp_path):
    job_dir = tmp_path / "job"
    target = _write_checkpoint_tree(job_dir, "f" * 64, status="failed")
    recovery_id = "c" * 32
    staging_dir = (
        target["result_dir"].parent / f".result.context-recovery-{recovery_id}"
    )
    shutil.copytree(target["result_dir"], staging_dir)
    (staging_dir / "context-extraction.json").write_text(
        json.dumps({"status": "completed"}), encoding="utf-8"
    )
    original_signature = recovery._tree_signature(target["result_dir"])
    staged_signature = recovery._tree_signature(staging_dir)
    archive_dir = (
        job_dir
        / "recovery"
        / "context-extraction"
        / recovery_id
        / target["file_id"]
        / "result"
    )
    archive_dir.parent.mkdir(parents=True)
    os.replace(target["result_dir"], archive_dir)
    publication = {
        "recovery_id": recovery_id,
        "file_id": target["file_id"],
        "archive_path": str(archive_dir.relative_to(job_dir)),
        "staging_path": str(staging_dir.relative_to(job_dir)),
        "original_signature": recovery._signature_payload(original_signature),
        "staged_signature": recovery._signature_payload(staged_signature),
        "old_artifacts": [],
    }

    outcome, _archived = recovery.recover_context_recovery_publication(
        job_dir, publication
    )

    assert outcome == "committed"
    assert (
        json.loads((target["result_dir"] / "context-extraction.json").read_text())[
            "status"
        ]
        == "completed"
    )
    assert (
        json.loads((archive_dir / "context-extraction.json").read_text())["status"]
        == "failed"
    )
    assert not staging_dir.exists()


def test_publication_recovery_refuses_unrelated_result_tree(tmp_path):
    job_dir = tmp_path / "job"
    target = _write_checkpoint_tree(job_dir, "9" * 64, status="failed")
    recovery_id = "d" * 32
    staging_dir = (
        target["result_dir"].parent / f".result.context-recovery-{recovery_id}"
    )
    shutil.copytree(target["result_dir"], staging_dir)
    (staging_dir / "context-extraction.json").write_text(
        json.dumps({"status": "completed"}), encoding="utf-8"
    )
    original_signature = recovery._tree_signature(target["result_dir"])
    staged_signature = recovery._tree_signature(staging_dir)
    archive_dir = (
        job_dir
        / "recovery"
        / "context-extraction"
        / recovery_id
        / target["file_id"]
        / "result"
    )
    archive_dir.parent.mkdir(parents=True)
    os.replace(target["result_dir"], archive_dir)
    target["result_dir"].mkdir()
    unrelated = target["result_dir"] / "context-extraction.json"
    unrelated.write_text(json.dumps({"status": "completed", "other": True}))
    original_unrelated = unrelated.read_bytes()
    publication = {
        "recovery_id": recovery_id,
        "file_id": target["file_id"],
        "archive_path": str(archive_dir.relative_to(job_dir)),
        "staging_path": str(staging_dir.relative_to(job_dir)),
        "original_signature": recovery._signature_payload(original_signature),
        "staged_signature": recovery._signature_payload(staged_signature),
        "old_artifacts": [],
    }

    outcome, _archived = recovery.recover_context_recovery_publication(
        job_dir, publication
    )

    assert outcome == "unsafe"
    assert unrelated.read_bytes() == original_unrelated
    assert archive_dir.is_dir()
    assert staging_dir.is_dir()


def test_context_only_script_reuses_checkpoints_without_overwriting_attribution(
    tmp_path, monkeypatch
):
    from life_logger import contextual_extraction
    from scripts import attribute_funasr_transcript

    result_dir = tmp_path / "result"
    result_dir.mkdir()
    target = _write_checkpoint_tree(tmp_path / "job", "a" * 64, status="failed")
    original_attribution = target["attribution_path"].read_bytes()
    output_dir = tmp_path / "staged-result"
    shutil.copytree(target["result_dir"], output_dir)
    calls = []

    def fake_extraction(records, **kwargs):
        calls.append((records, kwargs))
        report = {
            "schema_version": "context-extraction-v1",
            "status": "completed",
            "counts": {"formal_events": 0, "pending_events": 0, "pending_sources": 0},
            "kev": {"status": "completed"},
            "batches": [],
            "formal_events": [],
            "pending_events": [],
            "pending_sources": [],
        }
        (Path(kwargs["output_dir"]) / "context-extraction.json").write_text(
            json.dumps(report), encoding="utf-8"
        )
        kwargs["progress_callback"](
            {"batch": 1, "total_batches": 1, "status": "completed"}
        )
        return report

    monkeypatch.setattr(
        contextual_extraction, "run_contextual_extraction", fake_extraction
    )
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    args = attribute_funasr_transcript._parser().parse_args(
        [
            "--context-only",
            "--deepseek-enabled",
            "--transcript",
            str(target["transcript_path"]),
            "--recordings",
            str(target["recordings_path"]),
            "--source-attribution",
            str(output_dir / "source-attribution.json"),
            "--context-source-namespace",
            str(target["result_dir"]),
            "--output-dir",
            str(output_dir),
        ]
    )

    summary = attribute_funasr_transcript._run(args)

    assert summary["status"] == "completed"
    assert calls and calls[0][1]["source_namespace"] == str(target["result_dir"])
    assert calls[0][1]["progress_callback"]
    assert (output_dir / "2026-10-06" / "events.json").is_file()
    assert (output_dir / "2026-10-06" / "daily_summary.json").is_file()
    assert (output_dir / "source-attribution.json").read_bytes() == original_attribution


def test_jobstore_recovery_keeps_job_and_asr_state_and_archives_old_results(
    tmp_path, monkeypatch
):
    from life_logger import context_extraction_recovery as recovery_module

    store = JobStore(tmp_path / "store", recover=False)
    job_id = store.create_job()
    file_id = "d" * 64
    target = _write_checkpoint_tree(store.job_path(job_id), file_id, status="degraded")
    state = store.read(job_id)
    state.update(_state_for_target(target))
    state["job_id"] = job_id
    state["files"][0]["stages"] = {"asr": {"status": "completed"}}
    store._save_unlocked(state)
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"python": sys.executable}), encoding="utf-8")
    secrets_path = tmp_path / "secrets.json"
    secrets_path.write_text(
        json.dumps({"deepseek_api_key": "test-key"}), encoding="utf-8"
    )
    entered = threading.Event()
    release = threading.Event()

    def fake_prepare(**kwargs):
        entered.set()
        assert release.wait(2)
        current_target = kwargs["target"]
        result_dir = current_target["result_dir"]
        staging_dir = (
            result_dir.parent / f".result.context-recovery-{kwargs['recovery_id']}"
        )
        shutil.copytree(result_dir, staging_dir)
        (staging_dir / "context-extraction.json").write_text(
            json.dumps({"status": "completed"}), encoding="utf-8"
        )
        archive_dir = (
            kwargs["job_dir"]
            / "recovery"
            / "context-extraction"
            / kwargs["recovery_id"]
            / current_target["file_id"]
            / "result"
        )
        return {
            "file_id": current_target["file_id"],
            "recovery_id": kwargs["recovery_id"],
            "result_dir": result_dir,
            "staging_dir": staging_dir,
            "archive_dir": archive_dir,
            "extraction_status": "completed",
            "old_artifacts": [
                str(
                    (archive_dir / "context-extraction.json").relative_to(
                        kwargs["job_dir"]
                    )
                )
            ],
            "original_signature": recovery_module._tree_signature(result_dir),
            "staged_signature": recovery_module._tree_signature(staging_dir),
        }

    monkeypatch.setattr(
        recovery_module, "prepare_context_recovery_target", fake_prepare
    )
    queued = store.retry_context_extraction(
        job_id, config_path, secrets_path=secrets_path
    )
    assert queued["job_id"] == job_id
    assert queued["status"] == "completed"
    assert entered.wait(2)
    repeated = store.retry_context_extraction(
        job_id, config_path, secrets_path=secrets_path
    )
    assert (
        repeated["context_recovery"]["recovery_id"]
        == queued["context_recovery"]["recovery_id"]
    )
    release.set()
    worker = store._context_recovery_active.get(job_id)
    if worker:
        worker.join(3)

    final = store.read(job_id)
    assert final["status"] == "completed"
    assert final["files"][0]["stages"]["asr"]["status"] == "completed"
    assert final["context_recovery"]["status"] == "completed"
    assert final["context_recovery"]["extraction_status"] == "completed"
    assert (
        json.loads((target["result_dir"] / "context-extraction.json").read_text())[
            "status"
        ]
        == "completed"
    )
    archived_path = final["context_recovery"]["archived_artifacts"][0]
    assert (
        json.loads((store.job_path(job_id) / archived_path).read_text())["status"]
        == "degraded"
    )


def test_jobstore_recovery_rejects_active_jobs_and_manual_corrections(tmp_path):
    store = JobStore(tmp_path / "store", recover=False)
    job_id = store.create_job()
    file_id = "e" * 64
    target = _write_checkpoint_tree(store.job_path(job_id), file_id, status="degraded")
    state = store.read(job_id)
    state.update(_state_for_target(target, status="running"))
    state["job_id"] = job_id
    store._save_unlocked(state)
    config_path = tmp_path / "config.json"
    config_path.write_text("{}", encoding="utf-8")

    with pytest.raises(ValueError, match="尚未完成"):
        store.retry_context_extraction(job_id, config_path)

    state = store.read(job_id)
    state["status"] = "completed"
    store._save_unlocked(state)
    corrections = {
        "corrections": {
            "files/"
            + file_id
            + "/result/2026-10-06/events.json::evt-1": {
                "artifact_path": f"files/{file_id}/result/2026-10-06/events.json",
                "event_id": "evt-1",
                "changes": {"title": "manual correction"},
            }
        }
    }
    store.event_corrections_path(job_id).write_text(
        json.dumps(corrections), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="人工事件修正"):
        store.retry_context_extraction(job_id, config_path)


def test_startup_recovery_cleans_unpublished_staging_for_interrupted_attempt(tmp_path):
    store = JobStore(tmp_path / "store", recover=False)
    job_id = store.create_job()
    file_id = "8" * 64
    target = _write_checkpoint_tree(store.job_path(job_id), file_id, status="degraded")
    recovery_id = "e" * 32
    staging_dir = (
        target["result_dir"].parent / f".result.context-recovery-{recovery_id}"
    )
    shutil.copytree(target["result_dir"], staging_dir)
    state = store.read(job_id)
    state.update(_state_for_target(target))
    state["job_id"] = job_id
    state["context_recovery"] = {
        "recovery_id": recovery_id,
        "status": "running",
        "target_files": 1,
        "completed_files": 0,
    }
    store._save_unlocked(state)

    recovered_store = JobStore(store.root)

    assert recovered_store.read(job_id)["context_recovery"]["status"] == "interrupted"
    assert not staging_dir.exists()


def test_staging_cleanup_failure_does_not_abort_startup_recovery(tmp_path, monkeypatch):
    store = JobStore(tmp_path / "store", recover=False)
    job_id = store.create_job()
    file_id = "6" * 64
    target = _write_checkpoint_tree(store.job_path(job_id), file_id, status="degraded")
    recovery_id = "a" * 32
    staging_dir = (
        target["result_dir"].parent / f".result.context-recovery-{recovery_id}"
    )
    shutil.copytree(target["result_dir"], staging_dir)
    state = store.read(job_id)
    state.update(_state_for_target(target))
    state["job_id"] = job_id
    state["context_recovery"] = {
        "recovery_id": recovery_id,
        "status": "running",
        "target_files": 1,
        "completed_files": 0,
    }
    store._save_unlocked(state)

    def fail_remove(_path):
        raise PermissionError("staging tree is busy")

    monkeypatch.setattr(recovery.shutil, "rmtree", fail_remove)
    recovered_store = JobStore(store.root)
    recovered = recovered_store.read(job_id)["context_recovery"]

    assert recovered["status"] == "interrupted"
    assert recovered["staging_cleanup_failures"] == 1
    assert staging_dir.is_dir()


def test_startup_recovery_counts_publication_only_when_new_tree_matches(tmp_path):
    store = JobStore(tmp_path / "store", recover=False)
    job_id = store.create_job()
    file_id = "7" * 64
    target = _write_checkpoint_tree(store.job_path(job_id), file_id, status="failed")
    recovery_id = "f" * 32
    staging_dir = (
        target["result_dir"].parent / f".result.context-recovery-{recovery_id}"
    )
    shutil.copytree(target["result_dir"], staging_dir)
    (staging_dir / "context-extraction.json").write_text(
        json.dumps({"status": "completed"}), encoding="utf-8"
    )
    original_signature = recovery._tree_signature(target["result_dir"])
    staged_signature = recovery._tree_signature(staging_dir)
    archive_dir = (
        store.job_path(job_id)
        / "recovery"
        / "context-extraction"
        / recovery_id
        / file_id
        / "result"
    )
    archive_dir.parent.mkdir(parents=True)
    os.replace(target["result_dir"], archive_dir)
    os.replace(staging_dir, target["result_dir"])
    state = store.read(job_id)
    state.update(_state_for_target(target))
    state["job_id"] = job_id
    state["context_recovery"] = {
        "recovery_id": recovery_id,
        "status": "running",
        "target_files": 1,
        "completed_files": 0,
        "publication": {
            "recovery_id": recovery_id,
            "file_id": file_id,
            "archive_path": str(archive_dir.relative_to(store.job_path(job_id))),
            "staging_path": str(staging_dir.relative_to(store.job_path(job_id))),
            "original_signature": recovery._signature_payload(original_signature),
            "staged_signature": recovery._signature_payload(staged_signature),
            "extraction_status": "completed",
            "old_artifacts": [
                str(
                    (archive_dir / "context-extraction.json").relative_to(
                        store.job_path(job_id)
                    )
                )
            ],
        },
    }
    store._save_unlocked(state)

    recovered_store = JobStore(store.root)
    recovered = recovered_store.read(job_id)["context_recovery"]

    assert recovered["status"] == "completed"
    assert recovered["completed_files"] == 1
    assert recovered["extraction_status"] == "completed"
    assert recovered["archived_artifacts"] == [
        f"recovery/context-extraction/{recovery_id}/{file_id}/result/context-extraction.json"
    ]
    assert (
        json.loads((target["result_dir"] / "context-extraction.json").read_text())[
            "status"
        ]
        == "completed"
    )
    assert (
        json.loads((archive_dir / "context-extraction.json").read_text())["status"]
        == "failed"
    )
