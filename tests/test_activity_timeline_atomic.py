"""活动时间轴 JSON 的原子写 + 读侧完整性校验回归。

背景（2026-11-03 11:39）：`/api/activity-timeline?date=2026-11-03` 返回 activities=0，
而同一文件实际有 91 条。事后查明：那一刻文件**还没生成**（birthtime 12:00:12），
但 `read_day()` 把「文件不存在 / 解析失败 / 投影失败」三种情况统一吞成
`status="pending" + activities=[]` —— 前端只能显示「0 条」，无法区分原因。

本文件守四条：
  1) 并发写读：另一进程持续读到的**永远是完整 JSON**（不会半截），且 schema/date 齐全；
  2) 写入不留 `.tmp` 残留；
  3) 文件存在但坏（截断/缺 schema/date 不符）→ 抛 `TimelineFileCorrupt`，**不返回空**；
  4) 文件不存在 → `read_state="no_file"` + 显式 error 条目（不是静默空）。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

from life_logger.activity_timeline_runtime import (  # noqa: E402
    ActivityTimelineRuntime,
    TimelineFileCorrupt,
)

DAY = "2026-11-03"


def _payload(n: int) -> dict:
    """构造 n 条活动的完整 payload；`coverage.source_count == n` 作为自校验锚点。"""
    return {
        "activities": [
            {
                "activity_id": f"act_{i:03d}",
                "title": f"活动 {i} " + "填充" * 40,      # 撑大文件，让"半截读"更容易暴露
                "summary": "用于并发测试的填充摘要" * 4,
                "status": "进行中",
                "items": [],
                "related_transactions_ids": [],
                "uncertainties": [],
                "reported_at": "2026-11-03T10:00:00+08:00",
            }
            for i in range(n)
        ],
        "coverage": {"through": "2026-11-03T10:00:00+08:00", "source_count": n},
    }


def _runtime(root: Path) -> ActivityTimelineRuntime:
    return ActivityTimelineRuntime(root, root / "local-config.json")


# ─────────────────────────────────────────── 1) 并发写 / 读（跨进程）
RUNNER = r'''
import json, os, sys, time
from pathlib import Path
SRC = Path(sys.argv[1]); ROOT = Path(sys.argv[2]); MODE = sys.argv[3]
sys.path.insert(0, str(SRC))
from life_logger.activity_timeline_runtime import ActivityTimelineRuntime

rt = ActivityTimelineRuntime(ROOT, ROOT / "local-config.json")
DAY = sys.argv[4]
target = ROOT / "activity-timeline" / "jev" / f"{DAY}.json"
done = ROOT / "writer.done"

if MODE == "writer":
    for n in range(1, 121):                 # 1..120 条，文件持续变大
        acts = [{"activity_id": f"act_{i:03d}", "title": f"活动 {i} " + "填充" * 40,
                 "summary": "用于并发测试的填充摘要" * 4, "status": "进行中",
                 "items": [], "reported_at": "2026-11-03T10:00:00+08:00"}
                for i in range(n)]
        rt.publish_day(DAY, {"activities": acts,
                             "coverage": {"through": "2026-11-03T10:00:00+08:00", "source_count": n}})
    done.write_text("1")
else:
    bad, reads, last = [], 0, 0
    deadline = time.time() + 60
    while not done.exists() and time.time() < deadline:
        try:
            text = target.read_text(encoding="utf-8")
        except FileNotFoundError:
            continue
        reads += 1
        try:
            data = json.loads(text)
        except Exception as exc:
            bad.append(f"partial json: {type(exc).__name__}: {exc}")
            continue
        if data.get("schema") != "activity-timeline-v1":
            bad.append(f"schema missing: {data.get('schema')!r}")
            continue
        if data.get("date") != DAY:
            bad.append(f"date mismatch: {data.get('date')!r}")
            continue
        n_acts = len(data.get("activities") or [])
        anchor = (data.get("coverage") or {}).get("source_count")
        if n_acts != anchor:
            bad.append(f"torn payload: activities={n_acts} anchor={anchor}")
        last = n_acts
    print(json.dumps({"reads": reads, "bad": bad[:5], "bad_count": len(bad), "last": last}))
'''


def test_concurrent_write_read_never_sees_partial_json(tmp_path: Path):
    runner = tmp_path / "runner.py"
    runner.write_text(RUNNER, encoding="utf-8")
    root = tmp_path / "out"

    writer = subprocess.Popen(
        [sys.executable, str(runner), str(SRC), str(root), "writer", DAY],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    reader = subprocess.Popen(
        [sys.executable, str(runner), str(SRC), str(root), "reader", DAY],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        w_out, w_err = writer.communicate(timeout=180)
        r_out, r_err = reader.communicate(timeout=180)
    finally:
        for proc in (writer, reader):
            if proc.poll() is None:
                proc.kill()

    assert writer.returncode == 0, f"writer failed: {w_err[-800:]}"
    assert reader.returncode == 0, f"reader failed: {r_err[-800:]}"

    stats = json.loads(r_out.strip().splitlines()[-1])
    assert stats["bad_count"] == 0, f"读到过不完整/不合规 JSON: {stats['bad']}"
    assert stats["reads"] > 20, f"读取次数太少，测试没有真正并发（{stats['reads']}）"

    # 最终内容仍然是完整版本
    final = json.loads((root / "activity-timeline" / "jev" / f"{DAY}.json").read_text(encoding="utf-8"))
    assert len(final["activities"]) == final["coverage"]["source_count"] == 120


def test_publish_day_leaves_no_tmp_files(tmp_path: Path):
    rt = _runtime(tmp_path)
    rt.publish_day(DAY, _payload(5))
    rt.publish_day(DAY, _payload(9))
    leftovers = list((tmp_path / "activity-timeline" / "jev").glob("*tmp*"))
    assert leftovers == [], leftovers


# ─────────────────────────────────────────── 2) 读侧完整性校验
def _write_raw(root: Path, text: str) -> Path:
    target = root / "activity-timeline" / "jev" / f"{DAY}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return target


def test_truncated_json_raises_not_empty(tmp_path: Path):
    rt = _runtime(tmp_path)
    rt.publish_day(DAY, _payload(3))
    target = tmp_path / "activity-timeline" / "jev" / f"{DAY}.json"
    raw = target.read_text(encoding="utf-8")
    target.write_text(raw[: len(raw) // 2], encoding="utf-8")   # 模拟半截文件

    with pytest.raises(TimelineFileCorrupt) as excinfo:
        rt.read_day(DAY)
    assert excinfo.value.reason == "invalid_json"
    payload = excinfo.value.to_dict()
    assert payload["error"] == "timeline_corrupt"


def test_missing_schema_raises(tmp_path: Path):
    rt = _runtime(tmp_path)
    _write_raw(tmp_path, json.dumps({"date": DAY, "activities": []}))
    with pytest.raises(TimelineFileCorrupt) as excinfo:
        rt.read_day(DAY)
    assert excinfo.value.reason == "schema_missing_or_mismatch"


def test_date_mismatch_raises(tmp_path: Path):
    rt = _runtime(tmp_path)
    rt.publish_day(DAY, _payload(1))
    target = tmp_path / "activity-timeline" / "jev" / f"{DAY}.json"
    data = json.loads(target.read_text(encoding="utf-8"))
    data["date"] = "2026-11-02"                      # 文件名/内容串味
    target.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(TimelineFileCorrupt) as excinfo:
        rt.read_day(DAY)
    assert excinfo.value.reason == "date_mismatch"


def test_non_object_top_level_raises(tmp_path: Path):
    rt = _runtime(tmp_path)
    _write_raw(tmp_path, json.dumps([1, 2, 3]))
    with pytest.raises(TimelineFileCorrupt) as excinfo:
        rt.read_day(DAY)
    assert excinfo.value.reason == "not_an_object"


# ─────────────────────────────────────────── 3) 「不存在」必须可区分
def test_missing_file_is_explicitly_marked(tmp_path: Path):
    rt = _runtime(tmp_path)
    data = rt.read_day(DAY)
    assert data["status"] == "pending"
    assert data["activities"] == []
    assert data["read_state"] == "no_file"          # ← 关键：不是静默空
    assert data["read_state_detail"]["kind"] == "timeline_file_absent"
    assert data["errors"] == []                     # 不污染「处理错误」语义


# ─────────────────────────────────────────── 4) 投影失败不伪装成空
def test_projection_failure_returns_raw_payload(tmp_path: Path, monkeypatch):
    rt = _runtime(tmp_path)
    rt.publish_day(DAY, _payload(4))

    import life_logger.activity_timeline_runtime as mod

    def boom(_payload):
        raise RuntimeError("projection exploded")

    monkeypatch.setattr(mod, "_hedge_payload_for_display", boom)
    data = rt.read_day(DAY)

    assert len(data["activities"]) == 4, "投影失败必须返回原始数据，不能返回空"
    assert data["read_state"] == "ready"
    assert "projection exploded" in data["read_projection_error"]


# ─────────────────────────── 5) [2026-11-03 v2] SIGKILL 残留的孤立 tmp 必须在发布时被清掉
def test_publish_day_cleans_stale_tmp_but_keeps_fresh_one(tmp_path: Path):
    """发布时清理超龄孤立 tmp；**正在写**（新鲜）的 tmp 绝不碰。"""
    import os
    import time

    rt = _runtime(tmp_path)
    rt.publish_day(DAY, _payload(3))
    day_dir = tmp_path / "activity-timeline" / "jev"

    stale = day_dir / f".{DAY}.json.deadbeef.tmp"
    stale.write_bytes(b"x" * 1024)
    old = time.time() - 7200                       # 2 小时前 → 超龄（阈值 30 分钟）
    os.utime(stale, (old, old))

    fresh = day_dir / f".{DAY}.json.cafe0001.tmp"
    fresh.write_bytes(b"y" * 16)                   # mtime = 现在 → 可能仍在写，必须保留

    rt.publish_day(DAY, _payload(4))

    assert not stale.exists(), "超龄孤立 tmp 未被清理"
    assert fresh.exists(), "新鲜 tmp 被误删（可能删掉别人正在写的文件）"


# ─────────────────────────── 6) [2026-11-03 v2] 修复脚本不得再自带一份"缺 fsync"的原子写
def test_repair_script_reuses_shared_atomic_writer():
    """守回归：repair_coverage_through._write_atomic 必须委托给 atomic.write_json_atomic。

    旧实现是本地手写 mkstemp + os.replace，**缺 fsync**：断电/SIGKILL 后 rename 可能
    回退到旧文件，而脚本已经打印"已修复"并退出 0。这类"第二个实现"是最容易漂移的地方。
    """
    src = (ROOT / "scripts" / "repair_coverage_through.py").read_text(encoding="utf-8")
    assert "write_json_atomic" in src, "未委托给 atomic.write_json_atomic"

    # 只查**代码调用**，不查注释/文档字符串（docstring 里会提到 mkstemp 作为反例）
    import ast

    tree = ast.parse(src)
    calls = {
        ast.unparse(node.func)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
    }
    assert "tempfile.mkstemp" not in calls, "仍在手写 mkstemp（会绕过 fsync）"
    assert "os.replace" not in calls, "仍在手写 os.replace（会绕过 fsync）"


def test_repair_script_write_atomic_is_complete_and_clean(tmp_path: Path):
    """功能面：修复脚本的 _write_atomic 写出完整 JSON，且不留 tmp。"""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "repair_coverage_through", ROOT / "scripts" / "repair_coverage_through.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    target = tmp_path / "2026-11-03.json"
    payload = {"schema": "activity-timeline-v1", "date": DAY, "activities": [1, 2, 3]}
    mod._write_atomic(target, payload)

    assert json.loads(target.read_text(encoding="utf-8")) == payload
    assert list(tmp_path.glob("*tmp*")) == [], "留下了临时文件"
