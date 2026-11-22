"""Q590 测试：失败窗口主动重试调度 + 绕开 cadence 闸 + 消除 head-of-line blocking + 网络错误分类。

必须严格满足红->绿验证。
"""
from __future__ import annotations

import datetime
import json
from pathlib import Path

from life_logger.activity_timeline_runtime import (
    ActivityTimelineRuntime,
    BEIJING_TZ,
    is_transient_network_error,
    SCHEMA,
)

DAY_A = "2026-11-01"
DAY_B = "2026-10-29"


def _make_runtime(tmp_path: Path) -> ActivityTimelineRuntime:
    root = tmp_path / "data"
    root.mkdir(parents=True, exist_ok=True)
    cfg = tmp_path / "local-config.json"
    cfg.write_text(json.dumps({"activity_timeline": {
        "enabled": True, "route": "jev", "cadence_minutes": 10,
        "context_minutes": 30, "enabled_at": "2026-10-26T20:15:40+08:00"}}), encoding="utf-8")
    rt = ActivityTimelineRuntime(root, cfg)
    rt._failure_cooldown_seconds = 0
    return rt


def _write_day(rt: ActivityTimelineRuntime, day: str, payload: dict) -> None:
    d = rt.output_root / "activity-timeline" / "jev"
    d.mkdir(parents=True, exist_ok=True)
    full_payload = {
        "schema": SCHEMA,
        "date": day,
        "route": "jev",
        "timezone": "Asia/Shanghai",
        "cadence_minutes": 10,
        "status": "ready",
        "activities": [],
        "unassociated_items": [],
        "conflicts": [],
        "coverage": {"through": None, "source_count": 0},
        "errors": [],
        "metadata": {"processed_source_ids": []},
    }
    full_payload.update(payload)
    (d / f"{day}.json").write_text(json.dumps(full_payload, ensure_ascii=False), encoding="utf-8")


def _row(row_id: str, day_ts: str, text: str = "测试文本") -> dict:
    return {
        "source_id": f"src_{row_id}", "segment_id": "seg_1", "row_id": 1,
        "timestamp": day_ts, "text": text, "speaker": "",
        "source_label": "self", "source": "moss", "recording_id": "rec_1",
        "available_at": day_ts,
    }


def test_use_case_a_due_failed_window_retried_despite_cadence_or_planner_target(tmp_path, monkeypatch):
    """用例 a：
    1. failed_window retry_at 已到期。
    2. 当前时间尚在 cadence 窗口内（cutoff > now，若按旧代码会直接 awaiting_next_cutoff 阻断！）。
    3. 正常扫描有 pending，且 pending 字典序属于别的日期（如 2026-11-02）。
    修复后：必须优先主动调度该到期的 failed_window，不被 cadence 或 planner 选桶阻断！
    """
    rt = _make_runtime(tmp_path)
    now_real = datetime.datetime.now(BEIJING_TZ)
    today = now_real.date().isoformat()

    # 今天已经更新到 now_real + 5min 对应的 cadence，保证 cutoff > now
    through_dt = now_real.replace(second=0, microsecond=0)
    _write_day(rt, today, {
        "coverage": {"through": through_dt.isoformat(), "source_count": 0},
    })

    # 11-01 有到期的 failed_window (retry_at 过去 10 分钟 < now_real)
    retry_at_dt = (now_real - datetime.timedelta(minutes=10)).isoformat()
    first_failed_dt = (now_real - datetime.timedelta(minutes=20)).isoformat()
    failed_cutoff = "2026-11-01T18:00:00+08:00"
    failed_lookback = "2026-11-01T17:30:00+08:00"
    _write_day(rt, DAY_A, {
        "status": "error",
        "coverage": {"through": "2026-11-01T17:30:00+08:00", "source_count": 10},
        "metadata": {
            "processed_source_ids": [],
            "failed_window": {
                "cutoff": failed_cutoff,
                "lookback": failed_lookback,
                "attempts": 1,
                "first_failed_at": first_failed_dt,
                "retry_at": retry_at_dt,
                "key": f"{DAY_A}|2026-11-01T17:35:00+08:00"
            }
        }
    })

    unprocessed_row = _row("1101_fail_row", "2026-11-01T17:35:00+08:00")
    today_pending_row = _row("1102_pending_row", (now_real - datetime.timedelta(minutes=2)).isoformat())

    def mock_scan(self, since_mtime=None, max_files=150):
        # 正常扫描会发现今天的行；定向放大扫描会发现 11-01 的行
        if max_files >= getattr(self, "FAILED_WINDOW_SCAN_MAX_FILES", 4000):
            return [unprocessed_row]
        return [today_pending_row]

    monkeypatch.setattr(ActivityTimelineRuntime, "scan_recent_asr_records", mock_scan)

    captured = {}
    def mock_process(self, unit_cutoff, unit_lookback, records, payload, extractor):
        captured["target_day"] = payload.get("date")
        captured["cutoff"] = unit_cutoff.isoformat()
        captured["lookback"] = unit_lookback.isoformat()
        captured["rows"] = [r["source_id"] for r in records]
        return {"activities": [], "unassociated_items": [], "conflicts": [], "errors": []}

    monkeypatch.setattr(ActivityTimelineRuntime, "_process_window_model", mock_process)

    res = rt._bounded_step()

    # 验证是否成功突破 cadence 阻挡并主动调度了 11-01
    assert captured, f"未触发模型处理，tick 返回: {res}"
    assert captured["target_day"] == DAY_A, f"目标日期错误: {captured['target_day']}"
    assert captured["cutoff"] == failed_cutoff
    assert captured["lookback"] == failed_lookback
    assert captured["rows"] == ["src_1101_fail_row"]


def test_use_case_b_not_due_failed_window_not_retried(tmp_path, monkeypatch):
    """用例 b：
    retry_at 未到期（在未来）→ 严禁重试！若此时 cutoff > now，应正常返回 awaiting_next_cutoff。
    """
    rt = _make_runtime(tmp_path)
    now_real = datetime.datetime.now(BEIJING_TZ)
    today = now_real.date().isoformat()
    through_dt = now_real.replace(second=0, microsecond=0)
    _write_day(rt, today, {
        "coverage": {"through": through_dt.isoformat(), "source_count": 0},
    })

    future_retry = (now_real + datetime.timedelta(minutes=30)).isoformat()
    _write_day(rt, DAY_A, {
        "status": "error",
        "coverage": {"through": "2026-11-01T17:30:00+08:00", "source_count": 10},
        "metadata": {
            "processed_source_ids": [],
            "failed_window": {
                "cutoff": "2026-11-01T18:00:00+08:00",
                "lookback": "2026-11-01T17:30:00+08:00",
                "attempts": 1,
                "first_failed_at": (now_real - datetime.timedelta(minutes=5)).isoformat(),
                "retry_at": future_retry,
                "key": f"{DAY_A}|2026-11-01T17:35:00+08:00"
            }
        }
    })

    called = {"count": 0}
    monkeypatch.setattr(ActivityTimelineRuntime, "_process_window_model",
                        lambda *a, **k: called.__setitem__("count", called["count"] + 1) or {})

    res = rt._bounded_step()
    assert called["count"] == 0, "未到期的失败窗口被错误调用了模型！"
    assert res.get("status") == "idle"
    assert res.get("reason") == "awaiting_next_cutoff"


def test_use_case_c_transient_network_error_does_not_consume_attempts(tmp_path, monkeypatch):
    """用例 c：
    网络类瞬态错误（如 Connection refused / timeout / RemoteDisconnected）
    → attempts 不涨！network_attempts 增长，且走指数退避。
    """
    rt = _make_runtime(tmp_path)
    now_real = datetime.datetime.now(BEIJING_TZ)
    today = now_real.date().isoformat()
    through_dt = (now_real - datetime.timedelta(minutes=20)).replace(second=0, microsecond=0)
    _write_day(rt, today, {
        "coverage": {"through": through_dt.isoformat(), "source_count": 0},
    })

    row_data = _row("network_row", (through_dt + datetime.timedelta(minutes=5)).isoformat())
    monkeypatch.setattr(ActivityTimelineRuntime, "scan_recent_asr_records",
                        lambda self, since_mtime=None, max_files=150: [row_data])

    def mock_net_fail(*args, **kwargs):
        raise RuntimeError("Connection refused: 127.0.0.1:3425")

    monkeypatch.setattr(ActivityTimelineRuntime, "_process_window_model", mock_net_fail)

    # 第 1 次网络失败
    res1 = rt._bounded_step()
    assert res1["status"] == "error", f"网络失败应返回 error，实际: {res1}"
    doc1 = json.loads((rt.output_root / "activity-timeline" / "jev" / f"{today}.json").read_text(encoding="utf-8"))
    fw1 = doc1["metadata"]["failed_window"]
    assert fw1["attempts"] == 0, f"网络错误不应消耗 attempts，实际: {fw1['attempts']}"
    assert fw1["network_attempts"] == 1
    assert fw1["failure_kind"] == "network"
    assert fw1["backoff_seconds"] == 60.0

    # 模拟经过 60 秒退避期（修改 retry_at 为过去）后再次触发网络失败
    fw1["retry_at"] = (now_real - datetime.timedelta(seconds=5)).isoformat()
    (rt.output_root / "activity-timeline" / "jev" / f"{today}.json").write_text(json.dumps(doc1, ensure_ascii=False), encoding="utf-8")

    res2 = rt._bounded_step()
    assert res2["status"] == "error", f"第 2 次网络失败仍应返回 error，实际: {res2}"
    doc2 = json.loads((rt.output_root / "activity-timeline" / "jev" / f"{today}.json").read_text(encoding="utf-8"))
    fw2 = doc2["metadata"]["failed_window"]
    assert fw2["attempts"] == 0, f"第2次网络错误 attempts 仍应为 0，实际: {fw2['attempts']}"
    assert fw2["network_attempts"] == 2
    assert fw2["failure_kind"] == "network"
    assert fw2["backoff_seconds"] == 120.0  # 指数退避 60 -> 120


def test_use_case_d_eliminate_head_of_line_blocking(tmp_path, monkeypatch):
    """用例 d（消除 head-of-line blocking 验证）：
    DAY_B (10-29) 拥有更早的 cutoff (08:30)，但已经 attempts=3 处于 30 分钟熔断黑屏中。
    DAY_A (11-01) 拥有稍晚的 cutoff (18:00)，attempts=1，已到 retry_at。
    旧代码：_due_failed_window_unit 选出 10-29，在后续撞熔断黑屏 return retry_limit_reached，11-01 饿死！
    新代码：_due_failed_window_unit 自动跳过处于熔断黑屏的 10-29，成功调度 11-01！
    """
    rt = _make_runtime(tmp_path)
    now_real = datetime.datetime.now(BEIJING_TZ)
    today = now_real.date().isoformat()
    through_dt = now_real.replace(second=0, microsecond=0)
    _write_day(rt, today, {
        "coverage": {"through": through_dt.isoformat(), "source_count": 0},
    })

    # DAY_B (10-29): cutoff 较早，attempts=3 熔断中，卡住仅 10 分钟 (<= 30min)
    _write_day(rt, DAY_B, {
        "status": "error",
        "coverage": {"through": "2026-10-29T08:00:00+08:00", "source_count": 10},
        "metadata": {
            "processed_source_ids": [],
            "failed_window": {
                "cutoff": "2026-10-29T08:30:00+08:00",
                "lookback": "2026-10-29T08:00:00+08:00",
                "attempts": 3,
                "first_failed_at": (now_real - datetime.timedelta(minutes=10)).isoformat(),  # 卡住 10 分钟
                "retry_at": (now_real - datetime.timedelta(minutes=5)).isoformat(),
                "key": f"{DAY_B}|2026-10-29T08:05:00+08:00"
            }
        }
    })

    # DAY_A (11-01): cutoff 较晚，attempts=1，已到期，待重试
    _write_day(rt, DAY_A, {
        "status": "error",
        "coverage": {"through": "2026-11-01T17:30:00+08:00", "source_count": 10},
        "metadata": {
            "processed_source_ids": [],
            "failed_window": {
                "cutoff": "2026-11-01T18:00:00+08:00",
                "lookback": "2026-11-01T17:30:00+08:00",
                "attempts": 1,
                "first_failed_at": (now_real - datetime.timedelta(minutes=20)).isoformat(),
                "retry_at": (now_real - datetime.timedelta(minutes=10)).isoformat(),
                "key": f"{DAY_A}|2026-11-01T17:35:00+08:00"
            }
        }
    })

    row_b = _row("1029_row", "2026-10-29T08:05:00+08:00")
    row_a = _row("1101_row", "2026-11-01T17:35:00+08:00")

    def mock_scan(self, since_mtime=None, max_files=150):
        if max_files >= getattr(self, "FAILED_WINDOW_SCAN_MAX_FILES", 4000):
            return [row_b, row_a]
        return []

    monkeypatch.setattr(ActivityTimelineRuntime, "scan_recent_asr_records", mock_scan)

    captured = {}
    def mock_process(self, unit_cutoff, unit_lookback, records, payload, extractor):
        captured["target_day"] = payload.get("date")
        captured["cutoff"] = unit_cutoff.isoformat()
        return {"activities": [], "unassociated_items": [], "conflicts": [], "errors": []}

    monkeypatch.setattr(ActivityTimelineRuntime, "_process_window_model", mock_process)

    res = rt._bounded_step()
    # 验证是否跳过了黑屏的 10-29，直接处理了 11-01
    assert captured.get("target_day") == DAY_A, f"预期调度 {DAY_A}，实际调度: {captured.get('target_day')}，tick 结果: {res}"


def test_use_case_e_is_transient_network_error_helper():
    """用例 e：验证网络瞬态错误判定辅助函数。"""
    assert is_transient_network_error(RuntimeError("Connection refused: 127.0.0.1:3425"))
    assert is_transient_network_error(RuntimeError("RemoteDisconnected('Remote end closed connection')"))
    assert is_transient_network_error(TimeoutError("Read timed out"))
    # 硬业务错误不应被判定为瞬态网络错误
    assert not is_transient_network_error(RuntimeError("all extraction tiers failed: Insufficient Balance"))
    assert not is_transient_network_error(ValueError("invalid JSON from model"))
