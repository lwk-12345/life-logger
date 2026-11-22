"""看板改 OpenClaw 状态的路由：校验参数，冲突如实返回 409。"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "src" / "life_logger" / "local_web.py").read_text(encoding="utf-8")


def test_route_validates_task_id_and_status():
    block = SOURCE[SOURCE.index('"/api/openclaw/tasks/status"'):]
    block = block[:block.index('"/api/openclaw/reconciliation-callback"')]
    assert 'oc_task_[\\w-]+' in block
    assert '"dismissed"' in block and '"completed"' in block
    assert "patch_task_status" in block
    assert "HTTPStatus.CONFLICT" in block


class _Client:
    def __init__(self, current, conflicts=1):
        self.current = current
        self.conflicts = conflicts
        self.calls = []

    def patch_task(self, task_id, updates, expected_revision=None):
        self.calls.append(expected_revision)
        if self.conflicts:
            self.conflicts -= 1
            raise RuntimeError("OpenClaw patch_task HTTP 409: conflict")
        return {"id": task_id, **updates, "revision": 3}

    def get_task(self, task_id):
        return self.current


def test_no_conflict_passes_through():
    from life_logger.web_openclaw import patch_task_status

    client = _Client({}, conflicts=0)
    assert patch_task_status(client, "oc_task_a", "completed", 1)["status"] == "completed"
    assert client.calls == [1]


def test_conflict_already_at_target_counts_as_success():
    """超时重发：第一次其实写成了，不能再报错。"""
    from life_logger.web_openclaw import patch_task_status

    client = _Client({"status": "completed", "revision": 2})
    assert patch_task_status(client, "oc_task_a", "completed", 1)["revision"] == 2
    assert client.calls == [1]


def test_conflict_retries_once_with_latest_revision():
    from life_logger.web_openclaw import patch_task_status

    client = _Client({"status": "pending", "revision": 2})
    assert patch_task_status(client, "oc_task_a", "completed", 1)["status"] == "completed"
    assert client.calls == [1, 2]


def test_second_conflict_is_reported():
    import pytest

    from life_logger.web_openclaw import patch_task_status

    client = _Client({"status": "pending", "revision": 2}, conflicts=2)
    with pytest.raises(RuntimeError, match="409"):
        patch_task_status(client, "oc_task_a", "completed", 1)
