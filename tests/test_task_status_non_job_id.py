"""任务台账必须容忍「非作业编号」的 job_id。

背景（2026-10-14 用户实测）：
行动候选面板上的待办按钮点下去全部返回
    POST /api/tasks/status → 400 {"error": "任务编号无效"}

根因：Watch 实时会话事件在前端用 `_jobId = "watch_<session_id>"` 当**显示用**标识，
它不是可定位的作业编号；把它塞进 payload.job_id 后，`job_path()` 的
「必须以 job- 开头」校验直接抛错。

契约（本文件锁死的两条）：
1. 非 `job-` 前缀的 job_id **不得**让写操作失败 —— 它是可选元数据，降级为无作业定位。
2. 真正的路径穿越仍然必须被拒绝（`../` 之类不能因为放宽而溜进来）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def _make_store(tmp_path: Path):
    from life_logger.local_web import JobStore

    output = tmp_path / "local-web"
    output.mkdir(parents=True, exist_ok=True)
    # recover=False：单元测试不触发启动恢复扫描
    return JobStore(output, recover=False)


def test_watch_session_display_id_is_accepted_and_stored_without_job(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    task_key = "watch_4FA1E286D7B64B8FB80D3338C3D0A1E1::evt_ctx_2e4e0b698dee4cf9"

    record = store.save_task_state(
        task_key=task_key,
        status="completed",
        title="查询：检查 VPN 设置",
        event_id="evt_ctx_2e4e0b698dee4cf9",
        job_id="watch_4FA1E286D7B64B8FB80D3338C3D0A1E1",
        artifact_path=None,
    )

    assert record["status"] == "completed"
    assert record["task_key"] == task_key
    # 显示用标识不应被当成作业编号落盘
    assert record["job_id"] is None

    persisted = json.loads(store.task_states_path().read_text(encoding="utf-8"))
    assert persisted["tasks"][task_key]["status"] == "completed"


def test_job_prefixed_id_still_validated(tmp_path: Path) -> None:
    store = _make_store(tmp_path)

    # 合法作业编号：不存在的目录不报错（这里只校验格式，不要求目录存在）
    store.save_task_state(
        task_key="job-20261012-010044-9586eff4:files/x/result/2026-10-11/events.json:evt_ctx_a1",
        status="pending",
        job_id="job-20261012-010044-9586eff4",
    )


@pytest.mark.parametrize("bad", ["../escape", "a/b", "job-../x"])
def test_path_traversal_is_still_rejected(tmp_path: Path, bad: str) -> None:
    store = _make_store(tmp_path)

    with pytest.raises(ValueError):
        store.save_task_state(task_key="k", status="pending", job_id=bad)


def test_invalid_status_is_rejected(tmp_path: Path) -> None:
    store = _make_store(tmp_path)

    with pytest.raises(ValueError):
        store.save_task_state(task_key="k", status="done")
