"""上游（OpenClaw）已删除的待办必须在本地过期/清出。

背景：OpenClaw 里消失的 15 条【语音自查】永远留在 reminders.json —— 两个缺陷：
1. sync_tasks 只 upsert、从不处理上游已消失的条目；
2. clean_invalid_reminders 对 review_status == "confirmed" 网开一面，
   而走 OpenClaw 路径的条目恰好全被标成 confirmed。

保护规则（本文件同时回归）：用户手动加的（无 OpenClaw 硬标记）和已设提醒时间
（scheduled / remind_at）的条目绝不能被误清。
"""

import json
from pathlib import Path

from life_logger.local_reminders import LocalReminderStore


class FakeNotifier:
    name = "fake-local"

    def notify(self, *, title: str, body: str) -> bool:
        del title, body
        return True


def _store(tmp_path: Path) -> LocalReminderStore:
    return LocalReminderStore(tmp_path, notifier=FakeNotifier())


def _raw(tmp_path: Path) -> dict:
    payload = json.loads((tmp_path / "reminders.json").read_text(encoding="utf-8"))
    return payload["reminders"]


def _openclaw_selfcheck(task_id: str, title: str) -> dict:
    # 历史形态：只有 oc_task_ 键，没有 openclaw_id 字段（sync_tasks 当时不落盘它）
    return {
        "task_key": task_id,
        "title": title,
        "status": "pending",
        "review_status": "confirmed",
        "source_type": "manual",
    }


def test_upstream_removed_confirmed_entry_is_dismissed_then_purged(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    store.sync_tasks(
        [
            _openclaw_selfcheck("oc_task_keep", "接入苹果 Health 数据"),
            _openclaw_selfcheck(
                "oc_task_gone", "【语音自查】测试：查询北京今天天气"
            ),
        ],
        prune_missing_source="openclaw",
    )
    assert set(_raw(tmp_path)) == {"oc_task_keep", "oc_task_gone"}

    # 上游只剩 oc_task_keep（另一条被 OpenClaw 删除/归档）
    store.sync_tasks(
        [_openclaw_selfcheck("oc_task_keep", "接入苹果 Health 数据")],
        prune_missing_source="openclaw",
    )

    raw = _raw(tmp_path)
    assert raw["oc_task_keep"]["status"] == "unscheduled"
    gone = raw["oc_task_gone"]
    assert gone["status"] == "dismissed"
    assert gone["dismissed_reason"] == "upstream_removed"

    # 下一步：clean_invalid_reminders 把标记条目最终清出（confirmed 不再豁免）
    assert store.clean_invalid_reminders() == 1
    assert "oc_task_gone" not in _raw(tmp_path)
    assert "oc_task_keep" in _raw(tmp_path)


def test_manual_and_scheduled_entries_survive_upstream_pruning(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    store.sync_tasks(
        [
            {
                "task_key": "todo-manual",
                "title": "手动加的待办：交水电费",
                "status": "pending",
                "review_status": "confirmed",
                "source_type": "manual",
            },
            {
                "task_key": "oc_task_backlog",
                "title": "上游任务：整理周报",
                "status": "pending",
                "review_status": "confirmed",
                "source_type": "manual",
            },
        ]
    )
    # 用户给上游任务设了提醒时间
    store.schedule("oc_task_backlog", remind_at="2099-09-22T09:00:00+08:00")

    # 这个批次（OpenClaw 权威清单）里两条都不在 —— 但都不能被裁掉
    store.sync_tasks(
        [_openclaw_selfcheck("oc_task_other", "别的新任务")],
        prune_missing_source="openclaw",
    )

    raw = _raw(tmp_path)
    assert raw["todo-manual"]["status"] == "unscheduled"
    assert "dismissed_reason" not in raw["todo-manual"]
    assert raw["oc_task_backlog"]["status"] == "scheduled"
    assert "dismissed_reason" not in raw["oc_task_backlog"]
    assert store.clean_invalid_reminders() == 0


def test_local_fallback_batch_never_prunes_openclaw_entries(tmp_path: Path) -> None:
    """OpenClaw 不可达时本地只回退到兜底清单，不能拿它裁掉上游记录。"""

    store = _store(tmp_path)
    store.sync_tasks(
        [_openclaw_selfcheck("oc_task_offline", "上游任务")],
        prune_missing_source="openclaw",
    )
    store.sync_tasks(
        [{"task_key": "evt_local", "title": "本地兜底待办", "status": "pending"}],
        prune_missing_source=None,
    )

    raw = _raw(tmp_path)
    assert raw["oc_task_offline"]["status"] == "unscheduled"
    assert "dismissed_reason" not in raw["oc_task_offline"]


def test_reappearing_upstream_task_revives_from_auto_dismissal(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    store.sync_tasks(
        [_openclaw_selfcheck("oc_task_bounce", "可能被上游误删的任务")],
        prune_missing_source="openclaw",
    )
    store.sync_tasks([], prune_missing_source="openclaw")
    assert _raw(tmp_path)["oc_task_bounce"]["status"] == "dismissed"

    store.sync_tasks(
        [_openclaw_selfcheck("oc_task_bounce", "可能被上游误删的任务")],
        prune_missing_source="openclaw",
    )

    revived = _raw(tmp_path)["oc_task_bounce"]
    assert revived["status"] == "unscheduled"
    assert "dismissed_reason" not in revived
    assert "dismissed_at" not in revived


def test_user_dismissal_without_upstream_marker_is_untouched(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    store.sync_tasks(
        [{"task_key": "evt_manual_dismiss", "title": "用户自己关掉的待办", "status": "pending"}]
    )
    store.set_status("evt_manual_dismiss", "dismissed")
    store.sync_tasks([], prune_missing_source="openclaw")

    raw = _raw(tmp_path)
    assert raw["evt_manual_dismiss"]["status"] == "dismissed"
    assert "dismissed_reason" not in raw["evt_manual_dismiss"]
    assert store.clean_invalid_reminders() == 0
