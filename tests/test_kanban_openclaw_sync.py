"""看板操作 → OpenClaw 同步（含持久重试队列）的行为测试。

用**假 bridge**：不连 8791，不碰 ~/.openclaw，不写 output/（临时目录）。
守六件事：
  1. 没有映射 → 先 create（external_id 用稳定 task_key）
  2. 已有映射 → PATCH（带 revision）
  3. 409 → 读最新 + 重试一次（复用 patch_task_status 的语义）
  4. bridge 不可用 → 本地不抛错、进队列、state() 显示未同步
  5. 恢复后 flush 自动补，队列清空
  6. 新语音待办自动建（幂等：已映射的不再建）
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from life_logger.kanban_openclaw_sync import KanbanOpenClawSync  # noqa: E402


class FakeBridge:
    """最小的 task-store 假实现，行为对齐 openclaw_bridge 的返回形状。"""

    def __init__(self) -> None:
        self.tasks: dict[str, dict] = {}
        self.patch_calls: list[tuple[str, dict, int | None]] = []
        self.create_calls: list[dict] = []
        self.down = False
        self.conflict_once = False
        self._next = 0

    def _new_id(self) -> str:
        self._next += 1
        return f"oc_task_fake{self._next:03d}"

    def create_task_from_life_event(self, event, priority="P2", project_id="life-logger"):
        if self.down:
            raise RuntimeError("cannot reach OpenClaw bridge: Connection refused")
        self.create_calls.append(dict(event))
        external = str(event.get("event_id") or "")
        for task in self.tasks.values():
            if task.get("external_id") == external and external:
                return {"task": dict(task), "created": False}
        task_id = self._new_id()
        task = {
            "id": task_id, "title": event.get("title"), "status": "pending",
            "revision": 1, "external_id": external,
        }
        self.tasks[task_id] = task
        return {"task": dict(task), "created": True}

    def patch_task(self, task_id, updates, expected_revision=None):
        if self.down:
            raise RuntimeError("cannot reach OpenClaw bridge: Connection refused")
        self.patch_calls.append((task_id, dict(updates), expected_revision))
        task = self.tasks.get(task_id)
        if task is None:
            raise RuntimeError(f"OpenClaw patch_task HTTP 404: no task {task_id}")
        if self.conflict_once:
            self.conflict_once = False
            task["revision"] += 1          # 别处改过
            raise RuntimeError("OpenClaw patch_task HTTP 409: revision conflict")
        if expected_revision is not None and int(expected_revision) != task["revision"]:
            raise RuntimeError("OpenClaw patch_task HTTP 409: revision conflict")
        task.update(updates)
        task["revision"] += 1
        return dict(task)

    def get_task(self, task_id):
        if self.down:
            raise RuntimeError("cannot reach OpenClaw bridge: Connection refused")
        return dict(self.tasks.get(task_id) or {})


@pytest.fixture()
def sync(tmp_path: Path):
    bridge = FakeBridge()
    return KanbanOpenClawSync(tmp_path, client_factory=lambda: bridge), bridge


# ───────────────────────────────────────────────── 1) 无映射 → create
def test_first_change_creates_task_with_stable_external_id(sync):
    syncer, bridge = sync
    result = syncer.sync_change(task_key="event:evt_1", status="completed",
                                title="买牛奶", event_id="evt_1")
    assert result["synced"] is True
    assert len(bridge.create_calls) == 1
    assert bridge.create_calls[0]["event_id"] == "evt_1"
    task = bridge.tasks[result["task_id"]]
    # 建出来就顺手把状态推成 completed（否则看板说完成、OpenClaw 还 pending）
    assert task["status"] == "completed"


def test_task_key_is_external_id_when_event_id_missing(sync):
    syncer, bridge = sync
    result = syncer.sync_change(task_key="journal:abc", status="in_progress", title="写周报")
    assert result["synced"] is True
    assert bridge.create_calls[0]["event_id"] == "journal:abc"


# ───────────────────────────────────────────────── 2) 有映射 → PATCH 带 revision
def test_second_change_patches_with_revision(sync):
    syncer, bridge = sync
    first = syncer.sync_change(task_key="event:evt_1", status="pending",
                               title="买牛奶", event_id="evt_1")
    second = syncer.sync_change(task_key="event:evt_1", status="completed", title="买牛奶")
    assert second["synced"] is True
    assert len(bridge.create_calls) == 1, "第二次不该再建任务"
    task_id, updates, revision = bridge.patch_calls[-1]
    assert task_id == first["task_id"]
    assert updates == {"status": "completed"}
    assert revision is not None, "PATCH 必须带 revision"


# ───────────────────────────────────────────────── 3) 409 → 读最新重试一次
def test_conflict_retries_once_after_reading_latest(sync):
    syncer, bridge = sync
    first = syncer.sync_change(task_key="event:evt_1", status="pending", title="买牛奶",
                               event_id="evt_1")
    bridge.conflict_once = True     # 下一次 PATCH 必然 409
    result = syncer.sync_change(task_key="event:evt_1", status="completed", title="买牛奶")
    assert result["synced"] is True
    assert bridge.tasks[first["task_id"]]["status"] == "completed"


# ───────────────────────────────────────────────── 4) bridge 挂了 → 不抛、进队列
def test_bridge_down_queues_and_reports_unsynced(sync):
    syncer, bridge = sync
    bridge.down = True
    result = syncer.sync_change(task_key="event:evt_2", status="completed",
                                title="交房租", event_id="evt_2")
    assert result["synced"] is False        # 本地写成功、同步失败 —— 不抛异常
    assert result["queued"] is True
    state = syncer.state("event:evt_2")
    assert state["pending"] is True
    assert state["synced"] is False
    assert syncer.pending_count() == 1


def test_queue_survives_new_instance(sync, tmp_path):
    syncer, bridge = sync
    bridge.down = True
    syncer.sync_change(task_key="event:evt_3", status="completed", title="还书", event_id="evt_3")
    reopened = KanbanOpenClawSync(tmp_path, client_factory=lambda: bridge)
    assert reopened.pending_count() == 1, "队列必须落盘（进程重启不丢）"


# ───────────────────────────────────────────────── 5) 恢复后自动补
def test_flush_after_recovery_clears_queue(sync):
    syncer, bridge = sync
    bridge.down = True
    syncer.sync_change(task_key="event:evt_4", status="completed", title="订机票", event_id="evt_4")
    bridge.down = False
    flushed = syncer.flush_queue()
    assert flushed["flushed"] == 1
    assert syncer.pending_count() == 0
    assert syncer.state("event:evt_4")["synced"] is True


# ───────────────────────────────────────────────── 6) 新语音待办自动建
def test_new_todo_events_are_created_once(sync):
    syncer, bridge = sync
    events = [
        {"event_id": "evt_todo_1", "title": "买洗衣液", "categories": ["todo"]},
        {"event_id": "evt_todo_2", "title": "历史垃圾",
         "data": {"openclaw": {"candidate_type": "todo_candidate", "ignored": True}}},
        {"event_id": "evt_x", "title": "不是待办", "categories": ["expense"]},
    ]
    created = syncer.sync_new_todos(events)
    assert [item["task_key"] for item in created] == ["event:evt_todo_1"]
    # 再跑一遍：已经映射的不再建
    again = syncer.sync_new_todos(events)
    assert again == []
    assert len(bridge.create_calls) == 1


def test_auto_sync_respects_ttl(sync):
    syncer, bridge = sync
    events = [{"event_id": "evt_todo_9", "title": "买灯泡", "categories": ["todo"]}]
    first = syncer.auto_sync(lambda: events, ttl=60.0)
    assert first.get("created") == 1
    second = syncer.auto_sync(lambda: events, ttl=60.0)
    assert second.get("skipped") == "ttl", "60 秒内不该再打一次 bridge"


# ───────────────────────────────────────────────── 边界
def test_dismissed_before_create_is_local_only(sync):
    syncer, bridge = sync
    result = syncer.sync_change(task_key="event:evt_never", status="dismissed", title="误建")
    assert result["synced"] is True
    assert result.get("skipped") == "dismissed_before_create"
    assert bridge.create_calls == [], "从没同步过又被删掉的任务不该在 OpenClaw 建一条"


def test_invalid_status_is_rejected_without_touching_bridge(sync):
    syncer, bridge = sync
    result = syncer.sync_change(task_key="event:evt_1", status="nonsense")
    assert result["synced"] is False
    assert bridge.create_calls == [] and bridge.patch_calls == []


def test_state_file_is_readable_json(sync, tmp_path):
    syncer, bridge = sync
    syncer.sync_change(task_key="event:evt_5", status="pending", title="x", event_id="evt_5")
    payload = json.loads((tmp_path / "kanban-openclaw-sync.json").read_text(encoding="utf-8"))
    assert payload["schema_version"] == "1"
    assert "event:evt_5" in payload["tasks"]


# ───────────────────────────────────────────────── 路由层：同步失败不能带崩写入
def test_handler_helper_never_raises_when_sync_explodes(monkeypatch, tmp_path):
    """`/api/tasks/status` 里调的就是这个方法：它抛了就等于看板点不动。"""
    import life_logger.kanban_openclaw_sync as module
    import life_logger.local_web as local_web

    class Boom:
        def __init__(self, *a, **kw):
            raise RuntimeError("bridge 炸了")

    monkeypatch.setattr(module, "KanbanOpenClawSync", Boom)

    class StubApp:
        class store:
            root = tmp_path

    handler = object.__new__(local_web._Handler)
    handler.server = StubApp()      # `app` 是只读属性 → self.server
    result = local_web._Handler._sync_kanban_openclaw(
        handler, task_key="event:evt_1", status="completed", title="x"
    )
    assert result["synced"] is False
    assert "RuntimeError" in (result["error"] or "")


def test_handler_helper_returns_bridge_result(monkeypatch, tmp_path):
    import life_logger.kanban_openclaw_sync as module
    import life_logger.local_web as local_web

    bridge = FakeBridge()
    real = module.KanbanOpenClawSync
    monkeypatch.setattr(module, "KanbanOpenClawSync",
                        lambda root: real(root, client_factory=lambda: bridge))

    class StubApp:
        class store:
            root = tmp_path

    handler = object.__new__(local_web._Handler)
    handler.server = StubApp()
    result = local_web._Handler._sync_kanban_openclaw(
        handler, task_key="event:evt_9", status="completed", title="买盐", event_id="evt_9"
    )
    assert result["synced"] is True
    assert bridge.tasks[result["task_id"]]["status"] == "completed"


def test_auto_sync_feed_only_recent_and_open(tmp_path):
    """自动补建只喂最近 7 天、本地未关闭的待办，不把历史灌进汇报卡。"""
    from datetime import datetime, timedelta
    from life_logger.timeutil import LOCAL_TZ
    from life_logger.web_routes_events import _EventRoutesMixin as EventsRoutesMixin

    today = datetime.now(LOCAL_TZ).strftime("%Y%m%d")
    old = (datetime.now(LOCAL_TZ) - timedelta(days=30)).strftime("%Y%m%d")
    jobs = {f"job-{today}-000000-a": [{"event_id": "evt_new", "categories": ["todo"]},
                                       {"event_id": "evt_done", "categories": ["todo"]}],
            f"job-{old}-000000-b": [{"event_id": "evt_old", "categories": ["todo"]}]}

    class Store:
        def list(self):
            return [{"job_id": job_id, "status": "completed"} for job_id in jobs]

        def read_job_events(self, job_id):
            return jobs[job_id]

        def read_task_states(self):
            return [{"event_id": "evt_done", "status": "completed"}]

    class App:
        store = Store()

    handler = EventsRoutesMixin.__new__(EventsRoutesMixin)
    handler.app = App()
    ids = [event["event_id"] for event in EventsRoutesMixin._kanban_todo_events(handler)]
    assert ids == ["evt_new"]
