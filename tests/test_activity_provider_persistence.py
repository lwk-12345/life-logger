"""Tests for activity provider persistence in window_history.

Ensures that provider metadata is retained across apply_window serialization and
re-hydration.

[2026-11-06] 本文件曾经还有一条 `test_pipeline_provider_idempotency_and_route_isolation`，
用来验证 `activity_window_pipeline.run_window()` 的幂等与"provider 路由隔离"。
该模块已按审计结论删除（src/ 下无任何引用，只有测试在用），所以这条用例一并删除 ——
它测的是被删模块自身，不是 `activity_window_state` 的能力。

随之消失的还有一个只在该模块里实现的保护：`error_type="provider_route_mismatch"`
（旧 `activity_window_pipeline.py:358`）。当前生产链路
（`activity_timeline_runtime.py` → `activity_window_state.apply_window_update`）
里**没有**这个保护，也没有别的测试覆盖它。若将来重启窗口管线，需要连同这个保护一起恢复
（代码在 git 历史 `3ee475d` 里）。`window_history[].provider` 的持久化仍由下面这条用例覆盖。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from life_logger.activity_window_state import ActivityWindowState


def test_provider_retained_in_window_history():
    """Verify provider field in window_meta is preserved in window_history."""
    state = ActivityWindowState()
    resp = {
        "activities": [
            {
                "activity_id": "act-test-01",
                "title": "晨起测试",
                "status": "in_progress",
                "items": [
                    {
                        "item_id": "it-test-01",
                        "action": "打卡记录",
                        "evidence": [{"source_id": "src-1", "segment_id": 1, "quote": "打卡"}],
                    }
                ],
            }
        ]
    }
    meta = {
        "index": 1,
        "cutoff": "2026-10-26T06:10:00+08:00",
        "lookback": "2026-10-26T05:55:00+08:00",
        "record_count": 5,
        "provider": "deepseek",
    }
    new_state = state.apply_window(resp, window_meta=meta)
    hist = new_state.window_history
    assert len(hist) == 1
    assert hist[0]["provider"] == "deepseek"
    assert hist[0]["cutoff"] == "2026-10-26T06:10:00+08:00"

    # Verify after full serialization to dict and reloading
    d = new_state.to_dict()
    reloaded = ActivityWindowState(d)
    assert reloaded.window_history[0]["provider"] == "deepseek"
