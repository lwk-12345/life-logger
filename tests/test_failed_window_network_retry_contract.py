"""网络失败窗口的重试契约：写 failed_window / 不推进水位 / 按退避 / 历史日也能续账。

背景（2026-11-07）：`work/f841-review-20261107/report.md §3` 报告
`_bounded_step()` 在网络失败时没有写 `failed_window`（KeyError）。
本文件把这个契约**逐条钉死**，防止再次出现"失败被静默跳过"或"退避失效"。

只覆盖既有实现的行为，不改生产代码：
  * 网络类失败不消耗 `attempts`（不吃熔断配额），只涨 `network_attempts`；
  * 退避 60 → 120 → … 封顶 900 秒；
  * 失败**绝不推进** `coverage.through`（否则这一段就被当成"已处理"跳过）；
  * 未到 `retry_at` 时 `_due_failed_window_unit()` 不返回该窗口；
  * 历史日的失败窗口重试时，账记在**那一天**的文件上，且计数能接着涨（靠 `key`）。
"""
from __future__ import annotations

import datetime
import json
from pathlib import Path

from life_logger.activity_timeline_runtime import (
    ActivityTimelineRuntime,
    BEIJING_TZ,
    FAILURE_BACKOFF_MAX_SECONDS,
    SCHEMA,
    failure_backoff_seconds,
)


def _runtime(tmp_path: Path) -> ActivityTimelineRuntime:
    root = tmp_path / "data"
    root.mkdir(parents=True, exist_ok=True)
    cfg = tmp_path / "local-config.json"
    cfg.write_text(json.dumps({"activity_timeline": {
        "enabled": True, "route": "jev", "cadence_minutes": 10,
        "context_minutes": 30, "enabled_at": "2026-10-26T20:15:40+08:00"}}), encoding="utf-8")
    rt = ActivityTimelineRuntime(root, cfg)
    rt._failure_cooldown_seconds = 0
    return rt


def _write_day(rt: ActivityTimelineRuntime, day: str, payload: dict) -> Path:
    d = rt.output_root / "activity-timeline" / "jev"
    d.mkdir(parents=True, exist_ok=True)
    base = {"schema": SCHEMA, "date": day, "route": "jev", "timezone": "Asia/Shanghai",
            "cadence_minutes": 10, "status": "ready", "activities": [],
            "unassociated_items": [], "conflicts": [],
            "coverage": {"through": None, "source_count": 0}, "errors": [],
            "metadata": {"processed_source_ids": []}}
    base.update(payload)
    path = d / f"{day}.json"
    path.write_text(json.dumps(base, ensure_ascii=False), encoding="utf-8")
    return path


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _row(ts: str) -> dict:
    return {"source_id": "src_net", "segment_id": "seg_1", "row_id": 1, "timestamp": ts,
            "text": "测试文本", "speaker": "", "source_label": "self", "source": "moss",
            "recording_id": "rec_1", "available_at": ts}


def _network_boom(*_args, **_kwargs):
    raise RuntimeError("Connection refused: 127.0.0.1:3425")


def _wire(rt: ActivityTimelineRuntime, monkeypatch, row: dict, exc=_network_boom) -> None:
    monkeypatch.setattr(ActivityTimelineRuntime, "scan_recent_asr_records",
                        lambda self, since_mtime=None, max_files=150: [row])
    monkeypatch.setattr(ActivityTimelineRuntime, "_process_window_model", exc)


def test_network_failure_writes_failed_window_and_never_advances_through(tmp_path, monkeypatch):
    """核心：网络失败必须留下 failed_window，且水位不动（否则这段就被跳过）。"""
    rt = _runtime(tmp_path)
    now = datetime.datetime.now(BEIJING_TZ)
    day = now.date().isoformat()
    through = (now - datetime.timedelta(minutes=20)).replace(second=0, microsecond=0)
    path = _write_day(rt, day, {"coverage": {"through": through.isoformat(), "source_count": 0}})
    _wire(rt, monkeypatch, _row((through + datetime.timedelta(minutes=5)).isoformat()))

    res = rt._bounded_step()

    assert res["status"] == "error"
    doc = _read(path)
    fw = doc["metadata"]["failed_window"]            # ← 旧 bug 就是这里 KeyError
    assert fw["attempts"] == 0, "网络错误不消耗 attempts"
    assert fw["network_attempts"] == 1
    assert fw["failure_kind"] == "network"
    assert fw["backoff_seconds"] == 60.0
    assert doc["coverage"]["through"] == through.isoformat(), "失败不得推进水位（否则被当成已处理）"
    assert fw["cutoff"] and fw["lookback"], "必须记下窗口坐标，重试才知道从哪读"


def test_second_network_failure_backs_off_and_keeps_attempts_zero(tmp_path, monkeypatch):
    rt = _runtime(tmp_path)
    now = datetime.datetime.now(BEIJING_TZ)
    day = now.date().isoformat()
    through = (now - datetime.timedelta(minutes=20)).replace(second=0, microsecond=0)
    path = _write_day(rt, day, {"coverage": {"through": through.isoformat(), "source_count": 0}})
    _wire(rt, monkeypatch, _row((through + datetime.timedelta(minutes=5)).isoformat()))

    rt._bounded_step()
    doc = _read(path)
    doc["metadata"]["failed_window"]["retry_at"] = (now - datetime.timedelta(seconds=5)).isoformat()
    path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")

    res = rt._bounded_step()

    assert res["status"] == "error"
    fw = _read(path)["metadata"]["failed_window"]
    assert fw["attempts"] == 0
    assert fw["network_attempts"] == 2
    assert fw["backoff_seconds"] == 120.0, "指数退避 60 → 120"


def test_due_failed_window_is_gated_by_retry_at(tmp_path, monkeypatch):
    """退避期内不得重试；到期后必须能被调度器捡起来（不能被跳过）。"""
    rt = _runtime(tmp_path)
    now = datetime.datetime.now(BEIJING_TZ)
    day = now.date().isoformat()
    through = (now - datetime.timedelta(minutes=20)).replace(second=0, microsecond=0)
    ts = (through + datetime.timedelta(minutes=5)).isoformat()
    path = _write_day(rt, day, {"coverage": {"through": through.isoformat(), "source_count": 0}})
    _wire(rt, monkeypatch, _row(ts))
    rt._bounded_step()
    doc = _read(path)
    fw = doc["metadata"]["failed_window"]

    assert rt._due_failed_window_unit(now, None, 30, "jev") is None, "冷却期内不该重试"

    fw["retry_at"] = (now - datetime.timedelta(seconds=5)).isoformat()
    path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(ActivityTimelineRuntime, "_scan_asr_records_in_window",
                        lambda self, lookback, cutoff, seen, since_mtime=None: [_row(ts)])
    unit = rt._due_failed_window_unit(datetime.datetime.now(BEIJING_TZ), None, 30, "jev")
    assert unit is not None, "到期后必须被调度器捡起来"
    assert unit[0] == day


def test_backoff_is_exponential_and_capped():
    assert [failure_backoff_seconds(n) for n in range(1, 5)] == [60.0, 120.0, 240.0, 480.0]
    assert failure_backoff_seconds(5) == FAILURE_BACKOFF_MAX_SECONDS == 900.0
    assert failure_backoff_seconds(50) == FAILURE_BACKOFF_MAX_SECONDS, "封顶后不再增长（不会无限翻倍）"


def test_historical_day_retry_keeps_writing_its_own_file(tmp_path, monkeypatch):
    """历史日的失败窗口重试时：账要记在那一天，且 network_attempts 接着涨（靠 key）。"""
    rt = _runtime(tmp_path)
    now = datetime.datetime.now(BEIJING_TZ)
    hist = "2026-11-01"
    past = (now - datetime.timedelta(hours=6)).replace(second=0, microsecond=0)
    ts = (past + datetime.timedelta(minutes=5)).isoformat()
    key = f"{hist}|{ts}"
    path = _write_day(rt, hist, {
        "coverage": {"through": past.isoformat(), "source_count": 0},
        "metadata": {"processed_source_ids": [], "failed_window": {
            "cutoff": (past + datetime.timedelta(minutes=10)).isoformat(),
            "lookback": past.isoformat(), "attempts": 0, "network_attempts": 1,
            "failure_kind": "network", "backoff_seconds": 60,
            "first_failed_at": (now - datetime.timedelta(minutes=3)).isoformat(),
            "retry_at": (now - datetime.timedelta(seconds=5)).isoformat(),
            "key": key}},
    })
    _wire(rt, monkeypatch, _row(ts))
    monkeypatch.setattr(ActivityTimelineRuntime, "_scan_asr_records_in_window",
                        lambda self, lookback, cutoff, seen, since_mtime=None: [_row(ts)])

    res = rt._bounded_step()

    assert res["status"] == "error"
    doc = _read(path)
    fw = doc["metadata"]["failed_window"]
    assert fw["network_attempts"] == 2, "历史日重试必须接着涨，不能被当成新窗口"
    assert fw["backoff_seconds"] == 120.0
    assert doc["coverage"]["through"] == past.isoformat(), "历史日水位也不得推进"
    today_file = rt.output_root / "activity-timeline" / "jev" / f"{now.date().isoformat()}.json"
    if today_file.is_file():
        assert "failed_window" not in (_read(today_file).get("metadata") or {}), \
            "历史日的失败账不能被写进今天"
