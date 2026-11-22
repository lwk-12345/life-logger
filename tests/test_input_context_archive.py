#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""input_context 按日归档 + app_label 映射 的单测。

对应交付：`work/input-context-archive/REPORT.md`

三组断言：
  A. `bundle_to_app_label` 映射（已知 / 未知 / 空 / 大小写）
  B. 运行时把**已闭合**窗口增量追加到 `input-context/days/<date>.jsonl`，且不重复追加
  C. 读取时能从归档回溯历史窗口（`latest.json` 只有 30 分钟，历史日靠归档）
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from life_logger.input_context import (
    BUNDLE_APP_LABELS,
    archive_day_for_window,
    bundle_to_app_label,
    load_input_context_windows,
    read_archived_day,
    resolve_input_context,
    window_app_label,
    window_archive_key,
)
from life_logger.input_context_runtime import InputContextRuntimeManager

SHANGHAI_TZ = timezone(timedelta(hours=8))


# --------------------------------------------------------------------------
# A. app_label 映射
# --------------------------------------------------------------------------
@pytest.mark.parametrize("bundle, expected", [
    ("com.yetone.alma", "Alma"),
    ("com.google.Chrome", "Chrome"),
    ("com.tencent.xinWeChat", "微信"),
    ("com.apple.Terminal", "Terminal"),
    ("com.openai.codex", "Codex"),
])
def test_known_bundles_map_to_human_labels(bundle, expected):
    assert bundle_to_app_label(bundle) == expected


def test_unknown_bundle_falls_back_to_last_segment_capitalized():
    assert bundle_to_app_label("com.example.someWeirdApp") == "SomeWeirdApp"
    assert bundle_to_app_label("net.foo.bar") == "Bar"


def test_empty_or_invalid_bundle_returns_none():
    assert bundle_to_app_label(None) is None
    assert bundle_to_app_label("") is None
    assert bundle_to_app_label("   ") is None
    assert bundle_to_app_label(123) is None
    assert bundle_to_app_label("com.") is None


def test_bundle_match_is_case_insensitive():
    assert bundle_to_app_label("COM.YETONE.ALMA") == "Alma"
    assert bundle_to_app_label("com.Google.Chrome") == "Chrome"


def test_known_table_has_no_empty_values():
    assert BUNDLE_APP_LABELS, "映射表不能为空"
    assert all(isinstance(k, str) and k for k in BUNDLE_APP_LABELS)
    assert all(isinstance(v, str) and v.strip() for v in BUNDLE_APP_LABELS.values())


def test_window_app_label_prefers_field_then_bundle():
    assert window_app_label({"app_label": "Alma", "target_app": "x.y.z"}) == "Alma"
    assert window_app_label({"target_app": "com.google.Chrome"}) == "Chrome"
    assert window_app_label({}) is None
    assert window_app_label(None) is None


def test_resolve_result_carries_app_label():
    start = "2026-11-02T10:00:00+08:00"
    end = "2026-11-02T10:00:10+08:00"
    windows = [{
        "started_at": "2026-11-02T09:59:50+08:00",
        "ended_at": "2026-11-02T10:00:20+08:00",
        "target_app": "com.yetone.alma",
        "complete": True,
        "target_unknown": False,
    }]
    ctx = resolve_input_context(record_start=start, record_end=end, windows=windows)
    assert ctx["status"] == "resolved"
    assert ctx["target_bundle"] == "com.yetone.alma"
    assert ctx["app_label"] == "Alma"


# --------------------------------------------------------------------------
# 假采集器 / 假 runtime
# --------------------------------------------------------------------------
class _Sample:
    def __init__(self, at, bundle, pid="468"):
        self.at = at
        self.bundle = bundle
        self.pid = pid
        self.source = "lsappinfo"


class _FakeCollector:
    """最小假采集器。必须提供 `sample_once` —— 否则 `step_once` 不会收割。"""

    def __init__(self, windows=None, samples=None):
        self.windows = list(windows or [])
        self._pending = list(samples or [])
        self.front_samples = []
        self.open_window = None

    def sample_once(self, *, include_front=True, at=None):
        # 每次喂 4 条（与既有 front_windows 测试同一节奏）
        for _ in range(4):
            if not self._pending:
                return
            self.front_samples.append(self._pending.pop(0))


def _mk_runtime(tmp_path: Path, collector: _FakeCollector) -> InputContextRuntimeManager:
    rt = InputContextRuntimeManager(tmp_path, poll_interval_seconds=0.05)
    rt.collector = collector
    rt._external_collector = True
    return rt


def _closed_window(start: datetime, end: datetime, bundle: str) -> dict:
    return {
        "started_at": start.isoformat(),
        "ended_at": end.isoformat(),
        "duration_seconds": (end - start).total_seconds(),
        "target_app": bundle,
        "front_bundles": [bundle],
        "target_unknown": False,
        "complete": True,
    }


def _archive_path(tmp_path: Path, day: str) -> Path:
    return tmp_path / "input-context" / "days" / f"{day}.jsonl"


# --------------------------------------------------------------------------
# B. 按日归档
# --------------------------------------------------------------------------
def test_closed_windows_are_archived_to_daily_jsonl(tmp_path: Path):
    now = datetime.now(SHANGHAI_TZ)
    day = now.strftime("%Y-%m-%d")
    collector = _FakeCollector(windows=[
        _closed_window(now - timedelta(minutes=5), now - timedelta(minutes=4), "com.yetone.alma"),
    ])
    rt = _mk_runtime(tmp_path, collector)

    payload = rt.step_once(at=now)

    path = _archive_path(tmp_path, day)
    assert path.is_file(), "应按北京时间日期建立归档文件"
    lines = [json.loads(x) for x in path.read_text().splitlines() if x.strip()]
    assert len(lines) == 1
    row = lines[0]
    assert row["target_app"] == "com.yetone.alma"
    assert row["app_label"] == "Alma"
    assert row["archive_key"] == window_archive_key(row)
    assert row["archived_at"]
    assert payload["archived_windows"] == 1


def test_archiving_is_idempotent_across_publishes(tmp_path: Path):
    """同一段窗口被重复收割时，归档里不能出现第二条。"""
    now = datetime.now(SHANGHAI_TZ)
    day = now.strftime("%Y-%m-%d")
    win = _closed_window(now - timedelta(minutes=5), now - timedelta(minutes=4), "com.google.Chrome")
    collector = _FakeCollector(windows=[win])
    rt = _mk_runtime(tmp_path, collector)

    rt.step_once(at=now)
    # 第二次：把同一窗口再塞回采集器（模拟收割边界重复）
    collector.windows = [dict(win)]
    payload = rt.step_once(at=now + timedelta(seconds=1))

    lines = [json.loads(x) for x in _archive_path(tmp_path, day).read_text().splitlines() if x.strip()]
    assert len(lines) == 1, f"重复追加了：{lines}"
    assert payload["archived_windows"] == 0


def test_archiving_survives_process_restart(tmp_path: Path):
    """重启后（内存缓存清空）也必须靠读文件去重。"""
    now = datetime.now(SHANGHAI_TZ)
    day = now.strftime("%Y-%m-%d")
    win = _closed_window(now - timedelta(minutes=5), now - timedelta(minutes=4), "com.tencent.xinWeChat")

    rt1 = _mk_runtime(tmp_path, _FakeCollector(windows=[win]))
    rt1.step_once(at=now)

    rt2 = _mk_runtime(tmp_path, _FakeCollector(windows=[dict(win)]))
    assert rt2._archive_seen == {}, "新实例的缓存应为空（模拟重启）"
    rt2.step_once(at=now + timedelta(seconds=2))

    lines = [json.loads(x) for x in _archive_path(tmp_path, day).read_text().splitlines() if x.strip()]
    assert len(lines) == 1


def test_open_window_without_ended_at_is_not_archived(tmp_path: Path):
    now = datetime.now(SHANGHAI_TZ)
    day = now.strftime("%Y-%m-%d")
    collector = _FakeCollector(windows=[{
        "started_at": (now - timedelta(seconds=5)).isoformat(),
        "ended_at": None,
        "target_app": None,
        "complete": False,
        "target_unknown": True,
    }])
    rt = _mk_runtime(tmp_path, collector)
    payload = rt.step_once(at=now)

    assert payload["archived_windows"] == 0
    assert not _archive_path(tmp_path, day).exists() or _archive_path(tmp_path, day).stat().st_size == 0


def test_front_windows_carry_app_label(tmp_path: Path):
    now = datetime.now(SHANGHAI_TZ)
    samples = [_Sample(now - timedelta(seconds=s), "com.yetone.alma") for s in range(8, 0, -1)]
    rt = _mk_runtime(tmp_path, _FakeCollector(samples=samples))
    rt.step_once(at=now)

    payload = json.loads((tmp_path / "input-context/latest.json").read_text(encoding="utf-8"))
    front = payload["front_windows"]
    assert front and front[0]["app_label"] == "Alma"
    assert front[0]["target_app"] == "com.yetone.alma"


# --------------------------------------------------------------------------
# C. 从归档回溯历史窗口
# --------------------------------------------------------------------------
def test_reader_merges_archive_and_latest_with_dedupe(tmp_path: Path):
    day = "2026-11-02"
    archive = _archive_path(tmp_path, day)
    archive.parent.mkdir(parents=True, exist_ok=True)
    old = {
        "started_at": "2026-11-02T05:00:00+08:00",
        "ended_at": "2026-11-02T05:01:00+08:00",
        "target_app": "com.google.Chrome",
        "app_label": "Chrome",
        "complete": True,
    }
    dup_in_latest = {
        "started_at": "2026-11-02T06:00:00+08:00",
        "ended_at": "2026-11-02T06:01:00+08:00",
        "target_app": "com.yetone.alma",
        "complete": True,
    }
    archive.write_text(
        json.dumps(old, ensure_ascii=False) + "\n"
        + json.dumps(dup_in_latest, ensure_ascii=False) + "\n"
        + "not-a-json-line\n",          # 坏行必须被跳过
        encoding="utf-8",
    )
    (tmp_path / "input-context").mkdir(exist_ok=True)
    (tmp_path / "input-context/latest.json").write_text(json.dumps({
        "windows": [dup_in_latest],
        "front_windows": [{
            "started_at": "2026-11-02T07:00:00+08:00",
            "ended_at": "2026-11-02T07:00:30+08:00",
            "target_app": "com.tencent.xinWeChat",
            "complete": True,
        }],
    }), encoding="utf-8")

    windows = load_input_context_windows(tmp_path, day)
    keys = [window_archive_key(w) for w in windows]
    assert len(windows) == 3, keys
    assert window_archive_key(old) in keys
    assert window_archive_key(dup_in_latest) in keys            # latest 覆盖，但仍只有一份
    assert sum(1 for k in keys if k == window_archive_key(dup_in_latest)) == 1
    # 排序：按 started_at 升序
    assert [w["started_at"] for w in windows] == sorted(w["started_at"] for w in windows)


def test_reader_without_day_is_backwards_compatible(tmp_path: Path):
    """`day=None` 时只读 latest.json（旧行为等价），**不**读归档。"""
    archive = _archive_path(tmp_path, "2026-11-02")
    archive.parent.mkdir(parents=True, exist_ok=True)
    archive.write_text(json.dumps({
        "started_at": "2026-11-02T05:00:00+08:00",
        "ended_at": "2026-11-02T05:01:00+08:00",
        "target_app": "com.google.Chrome",
        "complete": True,
    }) + "\n", encoding="utf-8")
    (tmp_path / "input-context").mkdir(exist_ok=True)
    (tmp_path / "input-context/latest.json").write_text(json.dumps({"windows": []}), encoding="utf-8")

    assert load_input_context_windows(tmp_path) == []
    assert len(load_input_context_windows(tmp_path, "2026-11-02")) == 1


def test_reader_ignores_other_days_in_latest(tmp_path: Path):
    (tmp_path / "input-context").mkdir(parents=True, exist_ok=True)
    (tmp_path / "input-context/latest.json").write_text(json.dumps({"windows": [
        {"started_at": "2026-11-02T05:00:00+08:00", "ended_at": "2026-11-02T05:01:00+08:00",
         "target_app": "com.google.Chrome", "complete": True},
        {"started_at": "2026-11-03T05:00:00+08:00", "ended_at": "2026-11-03T05:01:00+08:00",
         "target_app": "com.yetone.alma", "complete": True},
    ]}), encoding="utf-8")

    only_1102 = load_input_context_windows(tmp_path, "2026-11-02")
    assert [w["target_app"] for w in only_1102] == ["com.google.Chrome"]


def test_archive_day_uses_beijing_date_of_ended_at():
    # UTC 16:30 = 北京时间次日 00:30 → 归档应落到次日
    w = {"started_at": "2026-11-02T16:00:00+00:00", "ended_at": "2026-11-02T16:30:00+00:00",
         "target_app": "com.yetone.alma"}
    assert archive_day_for_window(w) == "2026-11-03"


def test_archive_day_rejects_naive_timestamps():
    w = {"started_at": "2026-11-02T16:00:00", "ended_at": "2026-11-02T16:30:00",
         "target_app": "com.yetone.alma"}
    assert archive_day_for_window(w) is None, "naive 时间不猜时区"


def test_read_archived_day_missing_file_returns_empty(tmp_path: Path):
    assert read_archived_day(tmp_path, "2099-01-01") == []
