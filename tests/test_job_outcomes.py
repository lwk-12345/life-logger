"""任务级状态必须如实上报文件级失败；统计必须把它们露出来。

另外覆盖「假成功」：阶段失败/产物缺失/阶段自称完成但日志是崩的，都不能再算
干净的 completed；可降级的失败只把任务降为 degraded，不能整单判失败。
"""

from __future__ import annotations

import io
import json
from pathlib import Path

from life_logger.local_web import (
    DEGRADABLE_SEVERITY,
    FATAL_SEVERITY,
    apply_file_outcomes,
    collect_stage_errors,
    failed_file_summaries,
    stage_severity,
)
from life_logger.statistics import build_range_statistics
from scripts.scan_job_health import build_report, scan


def _write_job(
    job_root: Path,
    *,
    job_id: str,
    status: str,
    created_at: str,
    files: list[dict],
) -> None:
    job_dir = job_root / "jobs" / job_id
    job_dir.mkdir(parents=True)
    (job_dir / "status.json").write_text(
        json.dumps(
            {
                "job_id": job_id,
                "status": status,
                "created_at": created_at,
                "files": files,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _file(name: str, status: str, *, stage: str = "attribution") -> dict:
    return {
        "file_id": name,
        "original_name": name,
        "status": status,
        "current_stage": status if status != "failed" else stage,
        "stages": {stage: {"status": status}},
    }


def test_failed_file_summaries_names_the_stuck_stage() -> None:
    state = {
        "files": [
            _file("ok.wav", "completed"),
            _file("empty.wav", "failed", stage="attribution"),
        ]
    }
    assert failed_file_summaries(state) == [
        {
            "file_id": "empty.wav",
            "name": "empty.wav",
            "stage": "attribution",
            "error": None,
        }
    ]


def test_completed_job_with_failed_file_becomes_partial() -> None:
    state = {
        "status": "completed",
        "current_stage": "completed",
        "files": [_file("ok.wav", "completed"), _file("empty.wav", "failed")],
    }

    apply_file_outcomes(state)

    assert state["status"] == "partial"
    assert state["current_stage"] == "partial"
    assert state["failed_file_count"] == 1


def test_completed_job_without_failures_stays_completed() -> None:
    state = {"status": "completed", "files": [_file("ok.wav", "completed")]}

    apply_file_outcomes(state)

    assert state["status"] == "completed"
    assert state["failed_file_count"] == 0


def test_failed_job_is_not_downgraded_to_partial() -> None:
    state = {"status": "failed", "files": [_file("bad.wav", "failed")]}

    apply_file_outcomes(state)

    assert state["status"] == "failed"
    assert state["failed_file_count"] == 1


def test_statistics_exposes_processed_and_failed_files(tmp_path: Path) -> None:
    _write_job(
        tmp_path,
        job_id="job-1",
        status="completed",
        created_at="2026-10-01T10:00:00+08:00",
        files=[_file("a.wav", "completed"), _file("b.wav", "failed")],
    )
    _write_job(
        tmp_path,
        job_id="job-2",
        status="completed",
        created_at="2026-10-01T11:00:00+08:00",
        files=[_file("c.wav", "completed")],
    )
    _write_job(
        tmp_path,
        job_id="job-3",
        status="failed",
        created_at="2026-09-01T11:00:00+08:00",
        files=[_file("d.wav", "failed")],
    )

    result = build_range_statistics(
        tmp_path, date_from="2026-10-01", date_to="2026-10-01"
    )

    processing = result["processing"]
    assert processing["job_count"] == 2
    assert processing["processed_file_count"] == 2
    assert processing["failed_file_count"] == 1
    assert processing["failed_files"][0]["name"] == "b.wav"
    assert processing["failed_files"][0]["stage"] == "attribution"
    # 9/1 的任务在范围外，不能混进来
    assert processing["unprocessed_job_count"] == 0


# ---------------------------------------------------------------- 假成功 / 降级

def _job_dir(tmp_path: Path, job_id: str = "job-1") -> Path:
    job_dir = tmp_path / "jobs" / job_id
    job_dir.mkdir(parents=True)
    return job_dir


def _stage_log(job_dir: Path, file_id: str, stage: str, text: str) -> None:
    file_root = job_dir / "files" / file_id
    file_root.mkdir(parents=True, exist_ok=True)
    (file_root / f"{stage}.log").write_text(text, encoding="utf-8")


def _artifact(job_dir: Path, file_id: str, relative: str) -> None:
    path = job_dir / "files" / file_id / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}", encoding="utf-8")


def _state(
    *,
    status: str,
    file_id: str = "a" * 64,
    stage: str = "attribution",
    stage_state: dict | None = None,
    results: list[str] | None = None,
) -> dict:
    return {
        "job_id": "job-1",
        "status": status,
        "files": [
            {
                "file_id": file_id,
                "sha256": file_id,
                "original_name": "voice.m4a",
                "status": "completed",
                "current_stage": "completed",
                "results": results or [],
                "stages": {stage: stage_state or {"status": "completed"}},
            }
        ],
    }


def test_stage_severity_defaults_to_fatal_for_unknown_stage() -> None:
    assert stage_severity("asr") == FATAL_SEVERITY
    assert stage_severity("attribution") == FATAL_SEVERITY
    assert stage_severity("jev") == DEGRADABLE_SEVERITY
    # 没见过的阶段宁可保守：不能让它悄悄通过
    assert stage_severity("some_new_stage") == FATAL_SEVERITY
    assert stage_severity(None) == FATAL_SEVERITY


def test_failed_optional_stage_is_fatal_for_the_file_not_the_policy() -> None:
    """可选阶段（jev）失败仍然让文件失败 —— 但严重性记的是 degradable。"""
    state = _state(
        status="completed",
        stage="jev",
        stage_state={"status": "failed", "error": "Jev 增强失败，退出码 2"},
    )
    apply_file_outcomes(state)

    assert state["status"] == "partial"
    assert state["stage_errors"][0]["severity"] == DEGRADABLE_SEVERITY
    assert state["stage_errors"][0]["kind"] == "stage_failed"


def test_retried_stage_keeps_job_completed_but_records_the_failure() -> None:
    """重跑成功后任务仍是 completed，但失败历史必须留下来。"""
    state = _state(
        status="completed",
        stage_state={
            "status": "completed",
            "elapsed_seconds": 12.0,
            "error": "attribution 失败，退出码 1",
            "failures": [
                {"error": "attribution 失败，退出码 1", "stage": "attribution"}
            ],
        },
    )
    apply_file_outcomes(state)

    assert state["status"] == "completed"
    assert [item["kind"] for item in state["stage_errors"]] == ["stage_retry"]
    assert state["stage_errors"][0]["resolved"] is True
    assert "退出码 1" in state["stage_errors"][0]["error"]


def test_unverified_stage_with_artifacts_degrades_the_job(tmp_path: Path) -> None:
    """阶段日志最后是 Traceback、但产物在盘上：结果可用，完成度不可信 → degraded。"""
    job_dir = _job_dir(tmp_path)
    file_id = "a" * 64
    _stage_log(
        job_dir,
        file_id,
        "attribution",
        "Traceback (most recent call last):\n"
        "  File \"attribute_funasr_transcript.py\", line 1\n"
        "UnboundLocalError: cannot access local variable 're'\n",
    )
    _artifact(job_dir, file_id, "result/source-attribution.json")

    state = _state(status="completed", file_id=file_id)
    apply_file_outcomes(state, job_dir=job_dir)

    assert state["status"] == "degraded"
    assert state["current_stage"] == "degraded"
    errors = [item for item in state["stage_errors"] if not item["resolved"]]
    assert errors and errors[0]["kind"] == "stage_unverified"
    assert errors[0]["severity"] == DEGRADABLE_SEVERITY


def test_unverified_stage_without_artifacts_is_fatal(tmp_path: Path) -> None:
    """阶段日志崩了、产物也没了：这是真·假成功 → partial。"""
    job_dir = _job_dir(tmp_path)
    file_id = "a" * 64
    _stage_log(
        job_dir,
        file_id,
        "asr",
        "Traceback (most recent call last):\n"
        "NameError: name 'source_sha256' is not defined\n",
    )

    state = _state(status="completed", file_id=file_id, stage="asr")
    apply_file_outcomes(state, job_dir=job_dir)

    assert state["status"] == "partial"
    errors = [item for item in state["stage_errors"] if not item["resolved"]]
    assert errors[0]["kind"] == "stage_unverified"
    assert errors[0]["severity"] == FATAL_SEVERITY
    assert errors[0]["stage"] == "asr"


def test_stage_log_traceback_followed_by_success_stays_completed(
    tmp_path: Path,
) -> None:
    """崩过一次、之后同一个阶段日志里留下了成功记录 → 已完成，只留记录。"""
    job_dir = _job_dir(tmp_path)
    file_id = "a" * 64
    _stage_log(
        job_dir,
        file_id,
        "attribution",
        "Traceback (most recent call last):\n"
        "UnboundLocalError: cannot access local variable 'template_embedding'\n"
        '{"status": "completed", "counts": {"self": 19, "events": 7}}\n',
    )
    _artifact(job_dir, file_id, "result/source-attribution.json")

    state = _state(status="completed", file_id=file_id)
    apply_file_outcomes(state, job_dir=job_dir)

    assert state["status"] == "completed"
    assert [item["kind"] for item in state["stage_errors"]] == ["log_traceback"]
    assert state["stage_errors"][0]["resolved"] is True


def test_missing_promised_result_is_fatal(tmp_path: Path) -> None:
    """status 声明的产物其实不在盘上：不能算完成。"""
    job_dir = _job_dir(tmp_path)
    file_id = "a" * 64
    _artifact(job_dir, file_id, "result/source-attribution.json")

    state = _state(
        status="completed",
        file_id=file_id,
        results=[f"files/{file_id}/result/2026-10-09/events.json"],
    )
    apply_file_outcomes(state, job_dir=job_dir)

    assert state["status"] == "partial"
    kinds = [item["kind"] for item in state["stage_errors"]]
    assert "missing_result" in kinds


def test_watch_sourced_stage_log_is_not_used_as_evidence(tmp_path: Path) -> None:
    """ASR 复用 watch 切片检查点时，旧 asr.log 不是本次运行的证据。"""
    job_dir = _job_dir(tmp_path)
    file_id = "a" * 64
    _stage_log(
        job_dir,
        file_id,
        "asr",
        "Traceback (most recent call last):\nNameError: name 'old' is not defined\n",
    )
    _artifact(job_dir, file_id, "asr/2026-10-09/transcript.json")

    state = _state(
        status="completed",
        file_id=file_id,
        stage="asr",
        stage_state={"status": "completed", "source": "watch_segment_checkpoints"},
    )
    apply_file_outcomes(state, job_dir=job_dir)

    assert state["status"] == "completed"
    assert collect_stage_errors(state, job_dir=job_dir) == []


def test_statistics_reports_false_successes_and_degraded_jobs(tmp_path: Path) -> None:
    _write_job(
        tmp_path,
        job_id="job-1",
        status="completed",
        created_at="2026-10-01T10:00:00+08:00",
        files=[_file("a.wav", "completed")],
    )
    _write_job(
        tmp_path,
        job_id="job-2",
        status="degraded",
        created_at="2026-10-01T11:00:00+08:00",
        files=[_file("b.wav", "completed")],
    )
    # stage_errors 是任务级字段（apply_file_outcomes 写在这里）
    for job_id, error in (
        ("job-1", "attribution 阶段日志最后一次失败之后没有成功记录"),
        ("job-2", "Kev 不可用，决策增强降级"),
    ):
        path = tmp_path / "jobs" / job_id / "status.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["stage_errors"] = [
            {
                "stage": "attribution",
                "kind": "stage_unverified",
                "severity": "degradable",
                "resolved": False,
                "error": error,
            }
        ]
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    processing = build_range_statistics(
        tmp_path, date_from="2026-10-01", date_to="2026-10-01"
    )["processing"]

    assert processing["false_success_job_count"] == 1
    assert processing["false_success_jobs"][0]["job_id"] == "job-1"
    assert processing["degraded_job_count"] == 1
    assert processing["degraded_jobs"][0]["job_id"] == "job-2"
    # degraded 不算「没处理」，但也不该混进 completed
    assert processing["completed_job_count"] == 1
    assert processing["unprocessed_job_count"] == 0


# ------------------------------------------------- 工作进程：失败要带结构化字段

def _prepare_worker_job(tmp_path: Path):
    from life_logger.local_web import JobStore

    store = JobStore(tmp_path / "local-web", recover=False)
    job_id = store.create_job(start_at="2026-09-09T08:00:00+08:00")
    store.add_file(
        job_id,
        filename="voice.wav",
        relative_path="voice.wav",
        stream=io.BytesIO(b"fake audio"),
    )
    return store, job_id


def _worker_config(tmp_path: Path) -> dict:
    return {
        "python": "python",
        "asr": "fun",
        "vad": "vad",
        "template": str(tmp_path / "template.json"),
        "speaker": "cam++",
    }


def _fake_asr(job_dir: Path, file_id: str) -> None:
    day = job_dir / "files" / file_id / "asr" / "2026-09-09"
    day.mkdir(parents=True, exist_ok=True)
    (day / "transcript.json").write_text(
        json.dumps([{"text": "你好", "start_offset_seconds": 0.0}]), encoding="utf-8"
    )
    (day / "recordings.json").write_text("[]", encoding="utf-8")


def test_worker_reports_partial_and_records_stage_error(
    tmp_path: Path, monkeypatch
) -> None:
    """归因阶段退出码非 0：任务必须是 partial，并且失败进结构化字段。"""
    store, job_id = _prepare_worker_job(tmp_path)
    job_dir = store.job_path(job_id)

    def fake_run(command, **kwargs):
        if "run_cloud_asr_pipeline.py" in command[1]:
            _fake_asr(job_dir, store.read(job_id)["files"][0]["sha256"])
            return 0
        return 1

    monkeypatch.setattr("scripts.local_jobs.run_command", fake_run)

    from scripts.local_jobs import run_web_job

    store.prepare_for_start(job_id)
    run_web_job(
        job_dir,
        _worker_config(tmp_path),
        lambda event: store.update_from_worker(job_id, event),
    )

    state = store.read(job_id)
    assert state["status"] == "partial"
    assert state["failed_file_count"] == 1
    attribution = state["files"][0]["stages"]["attribution"]
    assert attribution["status"] == "failed"
    assert attribution["severity"] == FATAL_SEVERITY
    assert attribution["failures"]
    errors = [item for item in state["stage_errors"] if item["stage"] == "attribution"]
    assert errors and errors[0]["severity"] == FATAL_SEVERITY
    assert state["files"][0]["status"] == "failed"


def test_worker_second_run_records_recovery_without_hiding_it(
    tmp_path: Path, monkeypatch
) -> None:
    """第一轮归因失败、第二轮成功：状态回到 completed，但失败历史留痕。"""
    store, job_id = _prepare_worker_job(tmp_path)
    job_dir = store.job_path(job_id)
    attempts = {"count": 0}

    def fake_run(command, **kwargs):
        if "run_cloud_asr_pipeline.py" in command[1]:
            _fake_asr(job_dir, store.read(job_id)["files"][0]["sha256"])
            return 0
        attempts["count"] += 1
        return 1 if attempts["count"] == 1 else 0

    monkeypatch.setattr("scripts.local_jobs.run_command", fake_run)

    from scripts.local_jobs import run_web_job

    config = _worker_config(tmp_path)
    callback = lambda event: store.update_from_worker(job_id, event)  # noqa: E731
    store.prepare_for_start(job_id)
    run_web_job(job_dir, config, callback)
    assert store.read(job_id)["status"] == "partial"

    store.prepare_for_start(job_id)
    run_web_job(job_dir, config, callback)

    state = store.read(job_id)
    assert state["status"] == "completed"
    assert state["failed_file_count"] == 0
    kinds = [item["kind"] for item in state["stage_errors"]]
    assert "stage_retry" in kinds
    retry = next(
        item for item in state["stage_errors"] if item["kind"] == "stage_retry"
    )
    assert retry["resolved"] is True
    assert "退出码 1" in retry["error"]


# ------------------------------------------------------------------ 扫描工具

def _write_scannable_job(
    root: Path,
    *,
    job_id: str,
    status: str,
    file_id: str,
    stage_state: dict,
    log_text: str | None = None,
    stage: str = "attribution",
    artifacts: tuple[str, ...] = (),
) -> None:
    job_dir = root / "jobs" / job_id
    job_dir.mkdir(parents=True)
    (job_dir / "status.json").write_text(
        json.dumps(
            {
                "job_id": job_id,
                "status": status,
                "created_at": "2026-10-01T10:00:00+08:00",
                "files": [
                    {
                        "file_id": file_id,
                        "sha256": file_id,
                        "original_name": "voice.m4a",
                        "status": "completed",
                        "current_stage": "completed",
                        "results": [],
                        "stages": {stage: stage_state},
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    if log_text is not None:
        _stage_log(job_dir, file_id, stage, log_text)
    for relative in artifacts:
        _artifact(job_dir, file_id, relative)


def test_scanner_separates_false_success_from_recovered_history(tmp_path: Path) -> None:
    file_a, file_b, file_c, file_d = "a" * 64, "b" * 64, "c" * 64, "d" * 64
    _write_scannable_job(
        tmp_path,
        job_id="job-clean",
        status="completed",
        file_id=file_a,
        stage_state={"status": "completed"},
    )
    _write_scannable_job(
        tmp_path,
        job_id="job-recovered",
        status="completed",
        file_id=file_b,
        stage_state={
            "status": "completed",
            "error": "attribution 失败，退出码 1",
            "failures": [
                {"error": "attribution 失败，退出码 1", "stage": "attribution"}
            ],
        },
    )
    _write_scannable_job(
        tmp_path,
        job_id="job-degraded",
        status="completed",
        file_id=file_c,
        stage_state={"status": "completed"},
        log_text=(
            "Traceback (most recent call last):\n"
            "UnboundLocalError: cannot access local variable 're'\n"
        ),
        artifacts=("result/source-attribution.json",),
    )
    _write_scannable_job(
        tmp_path,
        job_id="job-fatal",
        status="completed",
        file_id=file_d,
        stage_state={"status": "completed"},
        stage="asr",
        log_text=(
            "Traceback (most recent call last):\n"
            "NameError: name 'source_sha256' is not defined\n"
        ),
    )

    rows = scan(tmp_path / "jobs")
    buckets = {row["job_id"]: row["bucket"] for row in rows}

    assert buckets["job-clean"] == "clean"
    assert buckets["job-recovered"] == "completed_with_history"
    assert buckets["job-degraded"] == "false_success_degraded"
    assert buckets["job-fatal"] == "false_success_fatal"
    report = build_report(rows)
    assert "job-fatal" in report
    assert "假成功" in report
