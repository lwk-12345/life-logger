"""F4：do_POST 内不允许局部 import sys（会让整个函数的 sys 变局部变量）。"""
from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "src" / "life_logger" / "local_web.py").read_text(encoding="utf-8")


def _local_sys_imports() -> list[tuple[str, int]]:
    tree = ast.parse(SOURCE)
    found = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for inner in ast.walk(node):
                if isinstance(inner, ast.Import) and any(a.name == "sys" for a in inner.names):
                    found.append((node.name, inner.lineno))
    return found


def test_no_function_local_import_sys():
    assert _local_sys_imports() == [], _local_sys_imports()


def test_module_level_sys_import_exists():
    tree = ast.parse(SOURCE)
    top = [n for n in tree.body if isinstance(n, ast.Import)
           and any(a.name == "sys" for a in n.names)]
    assert top, "模块顶层必须 import sys"


def _app(reminders):
    class _Store:
        def save_task_state(self, **kwargs):
            return {"task_key": kwargs.get("task_key"), "status": kwargs.get("status")}
    app = type("App", (), {})()
    app.store = _Store()
    app.reminders = reminders
    return app


def test_tasks_status_returns_json_when_reminders_fail():
    """/api/tasks/status：提醒本写入失败时也必须返回结构化结果，而不是断开连接。"""
    from life_logger.local_web import apply_task_status

    class _Reminders:
        def set_status(self, *a, **kw):
            raise ValueError("状态无效")

    out = apply_task_status(_app(_Reminders()), {"task_key": "t1", "status": "in_progress"},
                            sync_openclaw=lambda **kw: {"status": "skipped"})
    assert out["reminders_synced"] is False
    assert "提醒本未同步" in out["warning"]
    assert out["task"]["task_key"] == "t1"


def test_tasks_status_ok_path_has_no_warning():
    from life_logger.local_web import apply_task_status

    class _Reminders:
        def set_status(self, *a, **kw):
            return {"task_key": "t1"}

    out = apply_task_status(_app(_Reminders()), {"task_key": "t1", "status": "pending"},
                            sync_openclaw=lambda **kw: {"status": "skipped"})
    assert out["reminders_synced"] is True
    assert "warning" not in out
