"""模型失败 → 暂停 + 通知 + 待补标记（2026-10-22，用户要求）。

链路：三级模型全挂 → `announce_failure()` → notify.sh（失败入队）+ `pending-<date>.json`
→ 调度器记 PAUSED（不是 OK）→ `/api/daily-health` 显示「日报未生成（模型失败）」
→ 补生成成功后 `clear_pending()` 清标记并留审计行。
"""
from __future__ import annotations

import json
from pathlib import Path


from life_logger.local_web import build_daily_health
from life_logger.synthesis_pause import (
    announce_failure,
    clear_pending,
    list_pending,
    pending_path,
    queue_path,
    read_pending,
)


def _fake_notify(tmp_path: Path, *, exit_code: int, banner: str = "", status: str | None = None) -> Path:
    """伪造 notify.sh。2026-10-24 起真实脚本带 --json，stub 也照此输出状态行。

    语义：只有 status=sent（且 rc=0）才算送达；rc≠0 或 status=failed/skipped 都不算。
    """
    status = status or ("sent" if exit_code == 0 else ("skipped" if exit_code == 3 else "failed"))
    payload = json.dumps({
        "schema": "notify-status-v1", "status": status,
        "ok": status == "sent", "rc": exit_code, "reason": f"stub {status}",
    }, ensure_ascii=False)
    path = tmp_path / "notify.sh"
    path.write_text(
        f"#!/bin/sh\necho '{banner}notify: $1'\necho '{payload}'\nexit {exit_code}\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def test_announce_failure_writes_marker_and_queues_when_notify_fails(tmp_path, monkeypatch):
    script = _fake_notify(tmp_path, exit_code=1, banner="prepare failed ")
    monkeypatch.setenv("LIFE_LOGGER_NOTIFY_SCRIPT", str(script))

    marker_path, notify_result = announce_failure(
        tmp_path,
        "2026-10-22",
        errors=[{"tier": "primary", "error": "RuntimeError: 503"},
                {"tier": "tier3", "error": "TimeoutError: 90s"}],
        event_count=7,
        expense_total=41.6,
    )

    assert notify_result["status"] == "failed"
    assert marker_path == pending_path(tmp_path, "2026-10-22")
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    assert marker["state"] == "pending" and marker["paused"] is True
    assert marker["event_count"] == 7 and marker["expense_total"] == 41.6
    assert [e["tier"] for e in marker["errors"]] == ["primary", "tier3"]
    assert marker["next_action"].startswith("模型恢复后执行")

    queued = [json.loads(line) for line in queue_path(tmp_path).read_text(encoding="utf-8").splitlines()]
    assert queued[-1]["date"] == "2026-10-22"
    assert queued[-1]["kind"] == "daily_synthesis_failed"


def test_announce_failure_when_notify_succeeds_leaves_no_queue(tmp_path, monkeypatch):
    script = _fake_notify(tmp_path, exit_code=0)
    monkeypatch.setenv("LIFE_LOGGER_NOTIFY_SCRIPT", str(script))

    _marker, notify_result = announce_failure(tmp_path, "2026-10-21", event_count=2)
    assert notify_result["status"] == "sent"
    assert not queue_path(tmp_path).exists()


def test_marker_is_idempotent_and_counts_attempts(tmp_path, monkeypatch):
    monkeypatch.setenv("LIFE_LOGGER_NOTIFY_SCRIPT", str(_fake_notify(tmp_path, exit_code=1)))
    announce_failure(tmp_path, "2026-10-20", event_count=1)
    announce_failure(tmp_path, "2026-10-20", event_count=1)
    marker = read_pending(tmp_path, "2026-10-20")
    assert marker["attempts"] == 2
    assert marker["first_failed_at"]  # 首次失败时间不被覆盖
    assert len(list_pending(tmp_path)) == 1


def test_clear_pending_removes_marker_and_keeps_audit(tmp_path, monkeypatch):
    monkeypatch.setenv("LIFE_LOGGER_NOTIFY_SCRIPT", str(_fake_notify(tmp_path, exit_code=1)))
    announce_failure(tmp_path, "2026-10-19", event_count=3)

    assert clear_pending(tmp_path, "2026-10-19", reason="synthesis_ok") is True
    assert not pending_path(tmp_path, "2026-10-19").exists()
    assert clear_pending(tmp_path, "2026-10-19") is False  # 幂等

    audit = (tmp_path / "daily-synthesis" / "pending-resolved.jsonl").read_text(encoding="utf-8")
    assert json.loads(audit.strip().splitlines()[-1])["reason"] == "synthesis_ok"


def test_daily_health_reports_paused_day_not_ok(tmp_path, monkeypatch):
    monkeypatch.setenv("LIFE_LOGGER_NOTIFY_SCRIPT", str(_fake_notify(tmp_path, exit_code=1)))
    from datetime import datetime
    from zoneinfo import ZoneInfo

    today = datetime.now(tz=ZoneInfo("Asia/Shanghai")).date().isoformat()
    announce_failure(tmp_path, today, errors=[{"tier": "tier3", "error": "boom"}], event_count=0)

    payload = build_daily_health(tmp_path, days=2)
    entry = next(e for e in payload["days_detail"] if e["date"] == today)
    assert entry["has_synthesis"] is False
    assert entry["pending_synthesis"] is True
    assert entry["status"] == "paused_model_failure"
    assert "模型失败" in entry["status_text"]
    assert payload["pending_count"] == 1
    assert today in payload["pending_dates"]
    assert "补生成" in payload["pending_hint"]


def test_scheduler_records_paused_when_pending_marker_exists(tmp_path, monkeypatch):
    """调度器：没有 synthesis 文件但有 pending 标记 → PAUSED（不是 FAILED，也不是 OK）。"""
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import daily_synthesis_scheduler as scheduler

    out_dir = tmp_path / "synth"
    out_dir.mkdir()
    log_path = tmp_path / "scheduler.log"
    monkeypatch.setattr(scheduler, "OUT_DIR", out_dir, raising=False)
    monkeypatch.setattr(scheduler, "LOG_PATH", log_path, raising=False)
    monkeypatch.setattr(scheduler, "MAX_RETRIES", 0, raising=False)
    monkeypatch.setattr(scheduler.rr, "_build_app", lambda: object(), raising=False)
    monkeypatch.setattr(
        scheduler.rr, "_events_for", lambda app, date: ([{"title": "x"}], {"total_amount": 0.0}),
        raising=False,
    )

    from life_logger.synthesis_pause import record_pending

    record_pending(out_dir.parent, "2026-10-20", errors=[{"tier": "tier3", "error": "boom"}])

    def fake_generate(date, events, *, force, cache_dir, activities=None):
        return {"date": date, "pending_synthesis": True, "model_tier": "local-fallback"}

    monkeypatch.setattr(scheduler.rr.dcs, "generate_daily_cognitive_synthesis", fake_generate)
    rc, record = scheduler._run_day("2026-10-20", force=True, dry_run=False, max_retries=0, stage="test")

    assert record["result"] == "PAUSED"
    assert rc == 2
    assert "MODEL_TIERS_FAILED" in record["reason"]
    assert record["pending_file"] == "pending-2026-10-20.json"
