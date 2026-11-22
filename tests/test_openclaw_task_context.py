"""`/api/openclaw/tasks` 的语音待办补「当时在对哪个应用说话」（2026-11-06）。

背景：OpenClaw 汇报卡只拿到 `title`，于是「查询：检查 LiveLog 页面……」这类
**对着编程工具说的话**被当成生活待办上卡。这里把活动层已有的
`input_context`（app_context 那套窗口判定）接到任务读层，字段固定三个：
`{app_label, kind, confidence}`，**判不出来就是 None，绝不根据标题猜**。
"""
from __future__ import annotations

import json
import threading
import urllib.request
from pathlib import Path

import pytest

from life_logger import openclaw_task_context as ctx_mod
from life_logger.local_web import JobStore, LocalWebServer, create_server
from life_logger.openclaw_bridge import OpenClawBridgeClient
from life_logger.openclaw_task_context import (
    INPUT_CONTEXT_FIELDS,
    attach_input_context,
    event_index,
    input_context_for_event,
)

TOKEN = "openclaw-task-token"
DAY = "2026-11-04"
SPOKEN_AT = "2026-11-04T10:15:00+08:00"
SPOKEN_UTC = "2026-11-04T02:15:00+00:00"


def _reset_caches() -> None:
    ctx_mod._EVENT_INDEX_CACHE.clear()
    ctx_mod._WINDOWS_CACHE.clear()


@pytest.fixture(autouse=True)
def _clean_caches():
    _reset_caches()
    yield
    _reset_caches()


def _write_event(root: Path, event_id: str, *, reported_at: str = SPOKEN_UTC,
                 evidence_extra: dict | None = None) -> None:
    result = root / "jobs" / "job-20261104-000000-aaaa" / "files" / "hash1" / "result"
    result.mkdir(parents=True, exist_ok=True)
    events = []
    path = result / "events.json"
    if path.is_file():
        events = json.loads(path.read_text(encoding="utf-8"))
    evidence = {
        "source_id": "src_1",
        "segment_id": "rec_1_seg_000001",
        "source_label": "self",
        "reported_at": reported_at,
        "quote": "帮我检查一下这个页面",
        "input_context": None,
    }
    evidence.update(evidence_extra or {})
    events.append({
        "event_id": event_id,
        "title": "查询：检查 LiveLog 页面并推荐优化措施",
        "categories": ["todo"],
        "activity_type": "task",
        "time": {"absolute": reported_at},
        "evidence": evidence,
    })
    path.write_text(json.dumps(events, ensure_ascii=False), encoding="utf-8")


def _write_mic_window(root: Path, *, target_app: str, started_at: str, ended_at: str) -> None:
    """写一条 mic 窗口：`_is_mic()` 认 `provider.source == "mic_event"`。"""
    day_file = root / "watch-sessions" / "input-context" / "days" / f"{DAY}.jsonl"
    day_file.parent.mkdir(parents=True, exist_ok=True)
    window = {
        "started_at": started_at,
        "ended_at": ended_at,
        "target_app": target_app,
        "target_unknown": False,
        "complete": True,
        "provider": {"source": "mic_event", "pid": 42},
        "provider_pid": 42,
        "front_bundles": [target_app],
        "sample_count": 3,
    }
    day_file.write_text(json.dumps(window, ensure_ascii=False) + "\n", encoding="utf-8")


# --- 单元：判据 -----------------------------------------------------------

def test_programming_tool_todo_gets_kind_and_app_label(tmp_path: Path) -> None:
    """对着编程工具说的话 → kind=dictation + app_label，供 OpenClaw 过滤。"""
    _write_event(tmp_path, "evt_ctx_codex_1")
    _write_mic_window(
        tmp_path, target_app="com.openai.codex",
        started_at="2026-11-04T10:14:58+08:00", ended_at="2026-11-04T10:15:05+08:00",
    )
    tasks = [{"title": "查询：检查 LiveLog 页面", "source": {"event_id": "evt_ctx_codex_1"}}]

    rows = attach_input_context(tasks, root=tmp_path)

    assert rows[0]["input_context"] == {
        "app_label": "Codex", "kind": "dictation", "confidence": "high",
    }
    assert set(rows[0]["input_context"]) == set(INPUT_CONTEXT_FIELDS)


def test_ordinary_todo_without_windows_is_null(tmp_path: Path) -> None:
    """普通待办：那一刻没有窗口证据 → null（不是 kind=self，也不猜）。"""
    _write_event(tmp_path, "evt_ctx_shop_1")
    tasks = [{"title": "查询：计划去手机店维修并咨询", "source": {"event_id": "evt_ctx_shop_1"}}]

    rows = attach_input_context(tasks, root=tmp_path)

    assert "input_context" in rows[0]
    assert rows[0]["input_context"] is None


def test_unknown_event_id_is_null(tmp_path: Path) -> None:
    rows = attach_input_context(
        [{"title": "处理录音", "source": {"event_id": "task-1"}},
         {"title": "人工待办"}],  # 没有 source
        root=tmp_path,
    )
    assert [row["input_context"] for row in rows] == [None, None]


def test_stored_evidence_context_is_not_upgraded_with_guessed_kind(tmp_path: Path) -> None:
    """事件层已落盘的 app_label 直接透传，但 kind/confidence 留空 —— 不补猜。"""
    _write_event(
        tmp_path, "evt_ctx_stored_1",
        evidence_extra={"input_context": {"app_label": "Terminal",
                                          "target_bundle": "com.apple.Terminal",
                                          "meaning": "front_app_at_speech_time_not_delivery"}},
    )
    event = event_index(tmp_path)["evt_ctx_stored_1"]

    result = input_context_for_event(event, root=tmp_path)

    assert result == {"app_label": "Terminal", "kind": None, "confidence": None}


def test_naive_timestamp_is_not_guessed(tmp_path: Path) -> None:
    """没有时区的时间不瞎猜时区 → null。"""
    _write_event(tmp_path, "evt_ctx_naive_1", reported_at="2026-11-04T10:15:00")
    event = event_index(tmp_path)["evt_ctx_naive_1"]

    assert input_context_for_event(event, root=tmp_path) is None


# --- 单元：不改别的 -------------------------------------------------------

def test_attach_keeps_other_fields_order_and_does_not_mutate(tmp_path: Path) -> None:
    _write_event(tmp_path, "evt_ctx_a")
    tasks = [
        {"id": "t2", "title": "第二条", "priority": "P1", "source": {"event_id": "evt_ctx_a"}},
        {"id": "t1", "title": "第一条", "priority": "P2", "source": {"event_id": "evt_ctx_a"}},
    ]
    before = json.loads(json.dumps(tasks))

    rows = attach_input_context(tasks, root=tmp_path)

    assert [row["id"] for row in rows] == ["t2", "t1"]          # 排序不变
    assert tasks == before                                        # 上游没被就地改写
    for original, enriched in zip(tasks, rows):
        assert {k: v for k, v in enriched.items() if k != "input_context"} == original


def test_non_mapping_task_is_passed_through(tmp_path: Path) -> None:
    rows = attach_input_context(["不是 dict"], root=tmp_path)  # type: ignore[list-item]
    assert rows == ["不是 dict"]


def test_event_index_cache_invalidates_on_change(tmp_path: Path) -> None:
    _write_event(tmp_path, "evt_ctx_old")
    assert "evt_ctx_old" in event_index(tmp_path)

    _write_event(tmp_path, "evt_ctx_new")
    index = event_index(tmp_path)

    assert "evt_ctx_new" in index


def test_route_helper_never_adds_the_key_when_upstream_omits_it(tmp_path: Path) -> None:
    """上游没有 life_logger_todos 时不能凭空造一个空列表（形状保持）。"""
    from life_logger.web_routes_events import _attach_task_input_context

    assert _attach_task_input_context(None, tmp_path) == []


# --- 端到端：真接口 -------------------------------------------------------

def _request(base: str) -> dict:
    request = urllib.request.Request(
        base + "/api/openclaw/tasks", headers={"Authorization": f"Bearer {TOKEN}"}
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.loads(response.read().decode("utf-8"))


def test_api_openclaw_tasks_carries_input_context(tmp_path: Path, monkeypatch) -> None:
    """整条读路径：桥 → /api/openclaw/tasks → life_logger_todos[].input_context。"""
    root = tmp_path / "local-web"
    _write_event(root, "evt_ctx_codex_api")
    # 同一天但**不在** mic 窗口里（21:00 没有窗口）→ 必须是 null
    _write_event(root, "evt_ctx_plain_api", reported_at="2026-11-04T21:00:00+08:00")
    _write_mic_window(
        root, target_app="com.openai.codex",
        started_at="2026-11-04T10:14:58+08:00", ended_at="2026-11-04T10:15:05+08:00",
    )
    config = tmp_path / "local-config.json"
    config.write_text("{}", encoding="utf-8")
    store = JobStore(root, recover=False)

    monkeypatch.setattr(LocalWebServer, "_watch_live_loop", lambda self: None)
    monkeypatch.setattr(LocalWebServer, "_reminder_loop", lambda self: None)
    monkeypatch.setattr(LocalWebServer, "_auto_ingest_loop", lambda self: None)
    monkeypatch.setattr(LocalWebServer, "_warm_vip_transcriber", staticmethod(lambda: None))

    def fake_todos(_self):
        return {
            "total_open": 2,
            "wechat_matters": [],
            "general_todos": [],
            "life_logger_todos": [
                {"id": "oc_1", "title": "查询：检查 LiveLog 页面",
                 "status": "pending", "source": {"system": "life_logger",
                                                 "event_id": "evt_ctx_codex_api"}},
                {"id": "oc_2", "title": "查询：计划去手机店维修并咨询",
                 "status": "pending", "source": {"system": "life_logger",
                                                 "event_id": "evt_ctx_plain_api"}},
            ],
            "raw_count": 2,
        }

    monkeypatch.setattr(OpenClawBridgeClient, "get_unified_dashboard_todos", fake_todos)
    monkeypatch.setattr(OpenClawBridgeClient, "is_healthy", lambda _self: True)

    server = create_server(host="127.0.0.1", port=0, store=store,
                          config_path=config, access_token=TOKEN)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        payload = _request(base)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    todos = payload["life_logger_todos"]
    assert [t["id"] for t in todos] == ["oc_1", "oc_2"]      # 排序没变
    assert todos[0]["input_context"] == {
        "app_label": "Codex", "kind": "dictation", "confidence": "high",
    }
    assert todos[1]["input_context"] is None
    assert todos[0]["title"] == "查询：检查 LiveLog 页面"      # 其它字段没改
