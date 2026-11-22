"""P-23 回归：日报必须真的落盘，否则调度器不许写 OK。

事故（2026-10-20）：模型调用失败 → `generate_daily_cognitive_synthesis` 返回
`model_tier="local-fallback"` 的兜底字典，但**没有写文件**；调度器只看
`synthesis is None` 判成功，于是日志记 `result=OK / model_calls=0`，磁盘上却没有
`synthesis-2026-10-20.json`。用户看到的后果是「那天日报凭空消失」。

本文件锁两条：
1. fallback 路径必须原子写盘，且文件里保留 `model_tier=local-fallback` / `fallback=true`；
2. 调度器：函数返回非 None 但文件不存在 → 必须写 FAILED（不是 OK）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from life_logger.daily_cognitive_session import (  # noqa: E402
    generate_daily_cognitive_synthesis,
)

sys.path.insert(0, str(ROOT / "scripts"))
import daily_synthesis_scheduler as scheduler  # noqa: E402


EVENTS = [
    {
        "event_id": "e1",
        "title": "购买烧鸭饭 (¥12.00)",
        "categories": ["expense", "food"],
        "activity_type": "meal",
        "confidence": 0.95,
        "time": {"absolute": "2026-10-20T00:01:00+08:00"},
        "evidence": {"quote": "要一份烧鸭饭", "reported_at": "2026-10-20T00:01:00+08:00"},
        "data": {"expense": {"amount": 12.0, "currency": "CNY", "item": "烧鸭饭"}},
    }
]


# ---------------------------------------------------------------------------
# a) 2026-10-22 契约变更：模型三级全挂 → 暂停（不落"完整日报"，只落 pending 标记）
#    P-23 的旧契约是「fallback 也必须写盘」。用户 2026-10-22 明确要求改为
#    「模型坏了就停在这里 + 通知我」，所以现在拆成两档：
#      · 默认（暂停）：只落 pending-<date>.json，synthesis-<date>.json 不存在
#      · 环境变量 LIFE_LOGGER_PAUSE_ON_MODEL_FAILURE=0 时回到旧行为（仍写 fallback 文件）
# ---------------------------------------------------------------------------
def test_model_failure_pauses_and_writes_pending_marker(tmp_path, monkeypatch):
    """三级全挂 → 不落完整日报，只落 pending 标记（含三级错误）。"""
    monkeypatch.setenv("LIFE_LOGGER_PAUSE_ON_MODEL_FAILURE", "1")
    monkeypatch.setenv(
        "LIFE_LOGGER_NOTIFY_SCRIPT", str(_fake_notify_script(tmp_path, exit_code=1))
    )
    with patch(
        "life_logger.daily_cognitive_session._get_deepseek_settings",
        return_value=("https://api.deepseek.com", "fake_key", "deepseek-chat", "tier2", {"enabled": False}),
    ), patch("urllib.request.urlopen", side_effect=RuntimeError("upstream 503")):
        synthesis = generate_daily_cognitive_synthesis(
            "2026-10-20", EVENTS, force=True, cache_dir=tmp_path / "daily-synthesis"
        )

    assert synthesis.get("pending_synthesis") is True
    assert synthesis.get("persisted") is False
    assert not (tmp_path / "daily-synthesis" / "synthesis-2026-10-20.json").exists(), \
        "模型全挂时不许落一份看起来正常的日报"

    marker = json.loads(
        (tmp_path / "daily-synthesis" / "pending-2026-10-20.json").read_text(encoding="utf-8")
    )
    assert marker["state"] == "pending"
    assert marker["paused"] is True
    assert marker["reason"] == "model_tiers_failed"
    assert len(marker["errors"]) >= 1
    assert marker["event_count"] == len(EVENTS)
    # 通知失败（exit 1）→ 状态记录下来，并进本地队列，等微信恢复补发
    assert marker["notify"]["status"] == "failed"
    queued = (tmp_path / "notify-queue.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert queued and json.loads(queued[-1])["date"] == "2026-10-20"


def test_fallback_synthesis_is_persisted_when_pause_disabled(tmp_path, monkeypatch):
    """关掉暂停开关 → 回到 P-23 的旧行为：fallback 写盘并标明 local-fallback。"""
    monkeypatch.setenv("LIFE_LOGGER_PAUSE_ON_MODEL_FAILURE", "0")
    with patch(
        "life_logger.daily_cognitive_session._get_deepseek_settings",
        return_value=("https://api.deepseek.com", "fake_key", "deepseek-chat", None, None),
    ), patch("urllib.request.urlopen", side_effect=RuntimeError("upstream 503")):
        synthesis = generate_daily_cognitive_synthesis(
            "2026-10-20", EVENTS, force=True, cache_dir=tmp_path
        )

    assert synthesis.get("fallback") is True
    assert synthesis.get("model_tier") == "local-fallback"
    target = tmp_path / "synthesis-2026-10-20.json"
    assert target.is_file(), "关闭暂停后 fallback 仍应写盘（旧契约）"
    saved = json.loads(target.read_text(encoding="utf-8"))
    assert saved["expense_analysis"]["total_amount_cny"] == saved["expense_ledger"]["total_amount"]


def _fake_notify_script(tmp_path: Path, *, exit_code: int) -> Path:
    path = tmp_path / "notify.sh"
    path.write_text(f"#!/bin/sh\necho \"notify called: $1\"\nexit {exit_code}\n", encoding="utf-8")
    path.chmod(0o755)
    return path


# ---------------------------------------------------------------------------
# b) 调度器：返回非 None 但没文件 → FAILED
# ---------------------------------------------------------------------------
@pytest.fixture()
def scheduler_env(tmp_path, monkeypatch):
    """把调度器的输出目录与数据读取都换成轻量桩。"""
    monkeypatch.setattr(scheduler, "OUT_DIR", tmp_path, raising=False)
    monkeypatch.setattr(scheduler.rr, "_build_app", lambda: object(), raising=False)
    monkeypatch.setattr(
        scheduler.rr, "_events_for", lambda app, date: (list(EVENTS), {"total_amount": 12.0}),
        raising=False,
    )
    log_path = tmp_path / "daily-scheduler.log"
    monkeypatch.setattr(scheduler, "LOG_PATH", log_path, raising=False)
    monkeypatch.setattr(scheduler, "MAX_RETRIES", 0, raising=False)
    return tmp_path, log_path


def test_scheduler_never_logs_ok_when_file_missing(scheduler_env, monkeypatch):
    """函数返回了字典但没写盘、且没有 pending 标记 → 必须 FAILED（不是 OK）。"""
    out_dir, log_path = scheduler_env

    def fake_generate(date, events, *, force, cache_dir, activities=None):
        # 致命点：只返回、不写文件（复现 2026-10-20 的现场）
        return {
            "date": date,
            "model_tier": "local-fallback",
            "fallback": True,
            "fallback_reason": "RuntimeError: upstream 503",
            "expense_analysis": {"total_amount_cny": 12.0},
            "expense_ledger": {"total_amount": 12.0},
            "event_count": len(events),
        }

    monkeypatch.setattr(scheduler.rr.dcs, "generate_daily_cognitive_synthesis", fake_generate)

    rc, record = scheduler._run_day("2026-10-20", force=True, dry_run=False, max_retries=0, stage="test")

    assert record["result"] == "FAILED"
    assert record["result"] != "OK"
    assert "NOT_PERSISTED" in str(record.get("reason"))
    assert rc == 1
    logged = json.loads(log_path.read_text(encoding="utf-8").splitlines()[-1])
    assert logged["result"] == "FAILED"


def test_scheduler_logs_ok_when_file_is_written(scheduler_env, monkeypatch):
    """正常路径（函数自己写文件）仍应记 OK，避免把修复做成"永远失败"。"""
    out_dir, _log_path = scheduler_env

    def fake_generate(date, events, *, force, cache_dir, activities=None):
        payload = {
            "date": date,
            "model_tier": "primary",
            "expense_analysis": {"total_amount_cny": 12.0},
            "expense_ledger": {"total_amount": 12.0},
            "event_count": len(events),
        }
        (out_dir / f"synthesis-{date}.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )
        return payload

    monkeypatch.setattr(scheduler.rr.dcs, "generate_daily_cognitive_synthesis", fake_generate)

    rc, record = scheduler._run_day("2026-10-20", force=True, dry_run=False, max_retries=0, stage="test")

    assert record["result"] == "OK"
    assert record["aligned_with_ledger"] is True
    assert rc == 0


def test_scheduler_does_not_claim_success_when_existing_report_preserved(scheduler_env, monkeypatch):
    out_dir, log_path = scheduler_env
    target = out_dir / "synthesis-2026-10-20.json"
    target.write_text(json.dumps({"date": "2026-10-20", "executive_summary": "old report"}), encoding="utf-8")
    monkeypatch.setattr(scheduler.rr.dcs, "generate_daily_cognitive_synthesis",
                        lambda *args, **kwargs: {"fallback": True, "persisted": False,
                                                 "preserved_existing": True})
    rc, record = scheduler._run_day("2026-10-20", force=True, dry_run=False, max_retries=0, stage="test")
    assert rc == 1
    assert record["result"] == "FAILED"
    assert "PRESERVED_EXISTING" in record["reason"]
    assert json.loads(target.read_text(encoding="utf-8"))["executive_summary"] == "old report"
    assert json.loads(log_path.read_text().splitlines()[-1])["result"] == "FAILED"


def test_scheduler_marks_pending_even_if_previous_report_exists(scheduler_env, monkeypatch):
    out_dir, log_path = scheduler_env
    target = out_dir / "synthesis-2026-10-20.json"
    target.write_text(json.dumps({"date": "2026-10-20", "executive_summary": "old report"}), encoding="utf-8")
    monkeypatch.setattr(scheduler.rr.dcs, "generate_daily_cognitive_synthesis",
                        lambda *args, **kwargs: {"fallback": True, "persisted": False,
                                                 "pending_synthesis": True})
    rc, record = scheduler._run_day("2026-10-20", force=True, dry_run=False, max_retries=0, stage="test")
    assert rc == 2
    assert record["result"] == "PAUSED"
    assert json.loads(target.read_text())["executive_summary"] == "old report"
    assert json.loads(log_path.read_text().splitlines()[-1])["result"] == "PAUSED"


def test_scheduler_marks_write_error_even_if_previous_report_exists(scheduler_env, monkeypatch):
    out_dir, log_path = scheduler_env
    target = out_dir / "synthesis-2026-10-20.json"
    target.write_text(json.dumps({"date": "2026-10-20", "executive_summary": "old report"}), encoding="utf-8")
    monkeypatch.setattr(scheduler.rr.dcs, "generate_daily_cognitive_synthesis",
                        lambda *args, **kwargs: {"fallback": True, "persist_error": "OSError: link denied",
                                                 "expense_analysis": {"total_amount_cny": 12.0}})
    rc, record = scheduler._run_day("2026-10-20", force=True, dry_run=False, max_retries=0, stage="test")
    assert rc == 1
    assert record["result"] == "FAILED"
    assert "PERSIST_ERROR" in record["reason"]
    assert json.loads(target.read_text())["executive_summary"] == "old report"
    assert json.loads(log_path.read_text().splitlines()[-1])["result"] == "FAILED"
