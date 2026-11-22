"""跨存储状态翻译的回归测试。

背景（2026-10-15）：
    task state（任务本）与 reminders（提醒本）各自定义了一套状态集，
    历史上从未对齐（任务本 2026-09-21 定型、提醒本 2026-09-22 定型）。
    /api/tasks/status 双写时，reminders 侧收到 in_progress 会抛
    ValueError("状态无效")，而 task state 已写入 → 两个存储长期不一致，
    此前被 `except Exception: pass` 静默吞掉。

    修复方式：在两处跨界写入点加显式翻译（local_web.to_reminder_status /
    to_task_status）。本测试把这个翻译钉住，防止以后再漂。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from life_logger.local_web import (  # noqa: E402
    REMINDER_TO_TASK_STATUS,
    TASK_TO_REMINDER_STATUS,
    to_reminder_status,
    to_task_status,
)
from life_logger.local_reminders import LocalReminderStore  # noqa: E402


# ── 翻译表本身 ────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("task_status", "expected"),
    [
        ("pending", "unscheduled"),
        ("in_progress", "scheduled"),
        ("blocked", "scheduled"),
        ("completed", "completed"),
        ("dismissed", "dismissed"),
    ],
)
def test_task_status_translates_to_reminder_vocabulary(task_status: str, expected: str) -> None:
    assert to_reminder_status(task_status) == expected


@pytest.mark.parametrize(
    ("reminder_status", "expected"),
    [
        ("unscheduled", "pending"),
        ("scheduled", "in_progress"),
        ("completed", "completed"),
        ("dismissed", "dismissed"),
    ],
)
def test_reminder_status_translates_to_task_vocabulary(reminder_status: str, expected: str) -> None:
    assert to_task_status(reminder_status) == expected


def test_every_mapped_value_is_accepted_by_its_target_store() -> None:
    """翻译出来的值必须真的被对端接受 —— 否则等于没修。"""
    reminder_accepted = {"unscheduled", "scheduled", "completed", "dismissed"}
    task_accepted = {"pending", "in_progress", "completed", "dismissed"}

    for task_status in TASK_TO_REMINDER_STATUS:
        assert to_reminder_status(task_status) in reminder_accepted, task_status
    for reminder_status in REMINDER_TO_TASK_STATUS:
        assert to_task_status(reminder_status) in task_accepted, reminder_status


def test_common_statuses_are_identity_mapped() -> None:
    """completed / dismissed 两边语义一致，必须是恒等映射（不能漂）。"""
    for shared in ("completed", "dismissed"):
        assert to_reminder_status(shared) == shared
        assert to_task_status(shared) == shared


def test_unknown_status_passes_through_而不是静默改写() -> None:
    """未知值原样返回 —— 让下游照常报错，不要偷偷改成语义相近的值。"""
    assert to_reminder_status("whatever") == "whatever"
    assert to_task_status("whatever") == "whatever"


def test_empty_and_whitespace_are_normalised() -> None:
    assert to_reminder_status("") == ""
    assert to_reminder_status("  in_progress  ") == "scheduled"
    assert to_task_status(None) == ""  # type: ignore[arg-type]


# ── 端到端：真的写进 LocalReminders 不报错 ──────────────────────


def test_translated_status_is_accepted_by_real_reminders_store(tmp_path: Path) -> None:
    """核心回归：任务本说 in_progress，翻译后提醒本必须收下（此前会抛「状态无效」）。"""
    store = LocalReminderStore(tmp_path)  # 构造参数是「存放目录」
    task_key = "watch_abc::evt_ctx_xyz"

    # 修复前：直接把任务本词汇丢给提醒本 → ValueError("状态无效")
    with pytest.raises(ValueError, match="状态无效"):
        store.set_status(task_key, "in_progress")

    # 修复后：先翻译成提醒本词汇 → 通过
    result = store.set_status(task_key, to_reminder_status("in_progress"))
    assert result["status"] == "scheduled"

    # 再验一次另一个任务本专属值：pending → unscheduled
    result2 = store.set_status(task_key, to_reminder_status("pending"))
    assert result2["status"] == "unscheduled"


def test_reverse_translation_is_accepted_by_task_store() -> None:
    """反向：提醒本说 scheduled，翻译后任务本必须收下（对照 save_task_state 的校验集）。"""
    task_accepted = {"pending", "in_progress", "completed", "dismissed"}
    assert to_task_status("scheduled") == "in_progress"
    assert to_task_status("unscheduled") == "pending"
    assert to_task_status("scheduled") in task_accepted
    assert to_task_status("unscheduled") in task_accepted
