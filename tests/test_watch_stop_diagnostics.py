"""Watch 停录归因 / completion ack 台账 / 设备状态历史 的回归测试。

对应 Q144：10-23 17:08:40 录音正常收口（198/198 段全到、pending_segment=0），
但设备没上报 ended_by，15 个 completion 没有服务端台账，device-status 只保留最新一条，
导致「为什么停录」不可追。这些测试锁住三类新增证据 + 旧数据兼容。
"""
from __future__ import annotations

import hashlib
import json
import sys
import threading
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from life_logger.watch_stream import (  # noqa: E402
    DEVICE_STATUS_HISTORY_MAX,
    WatchSessionStore,
)

DEVICE = "watch-q144"
SESSION = "SESSQ144"


def _segment(store: WatchSessionStore, seq: int) -> None:
    body = b"x" * 128
    store.receive_segment(
        device_id=DEVICE,
        session_id=SESSION,
        sequence=seq,
        start_offset_ms=seq * 60000,
        duration_ms=60000,
        checksum="sha256:" + hashlib.sha256(body).hexdigest(),
        body=body,
    )


def _prepare(tmp_path: Path, count: int = 3) -> WatchSessionStore:
    store = WatchSessionStore(tmp_path)
    store.start_session(device_id=DEVICE, session_id=SESSION,
                        started_at="2026-10-23T15:00:00+08:00")
    for seq in range(count):
        _segment(store, seq)
    return store


# ---------------------------------------------------------------- ended_by
def test_client_reported_ended_by_is_persisted(tmp_path: Path) -> None:
    store = _prepare(tmp_path)
    result = store.complete_session(
        device_id=DEVICE, session_id=SESSION, expected_segments=3,
        ended_at="2026-10-23T17:08:40+08:00",
        ended_by="user", end_reason="user_stop",
    )
    manifest = result["session"]
    assert manifest["ended_by"] == "user"
    assert manifest["ended_by_source"] == "client_reported"
    assert manifest["end_reason"] == "user_stop"
    # 落盘后可再次读到
    assert store.load_session(DEVICE, SESSION)["ended_by"] == "user"


def test_missing_ended_by_is_marked_inferred_not_guessed(tmp_path: Path) -> None:
    store = _prepare(tmp_path)
    manifest = store.complete_session(
        device_id=DEVICE, session_id=SESSION, expected_segments=3,
        ended_at="2026-10-23T17:08:40+08:00",
    )["session"]
    assert manifest["ended_by"] == "watch_client"
    assert manifest["ended_by_source"] == "inferred"
    assert manifest["end_reason"] == "completion_request_without_ended_by"


def test_legacy_manifest_without_ended_by_still_loads_and_backfills(tmp_path: Path) -> None:
    store = _prepare(tmp_path)
    store.complete_session(device_id=DEVICE, session_id=SESSION, expected_segments=3,
                           ended_at="2026-10-23T17:08:40+08:00")
    # 模拟旧产物：把新字段删掉
    path = tmp_path / DEVICE / SESSION / "manifest.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    for key in ("ended_by", "ended_by_source", "end_reason"):
        payload.pop(key, None)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    first = store.stop_diagnostics()
    assert first["recent_stops"][0]["ended_by"] is None
    assert first["recent_stops"][0]["ended_by_source"] == "missing"

    # 设备重试并带上 ended_by → 补写，不报错
    duplicate = store.complete_session(
        device_id=DEVICE, session_id=SESSION, expected_segments=3,
        ended_at="2026-10-23T17:08:40+08:00", ended_by="user",
    )
    assert duplicate["duplicate"] is True
    assert duplicate["session"]["ended_by"] == "user"
    assert duplicate["session"]["ended_by_source"] == "client_reported"


# ---------------------------------------------------------------- ack 台账
def test_completion_ack_is_appended_and_read_newest_first(tmp_path: Path) -> None:
    store = _prepare(tmp_path)
    manifest = store.complete_session(device_id=DEVICE, session_id=SESSION, expected_segments=3,
                                      ended_at="2026-10-23T17:08:40+08:00")["session"]
    store.record_completion_ack(device_id=DEVICE, session_id=SESSION, job_id="job-a",
                                result="complete", ended_by=manifest["ended_by"],
                                end_reason=manifest["end_reason"], received_segments=3)
    store.record_completion_ack(device_id=DEVICE, session_id=SESSION, job_id="job-b",
                                result="duplicate")

    acks = store.list_completion_acks()
    assert [a["job_id"] for a in acks] == ["job-b", "job-a"]
    assert acks[0]["acked_at"] and acks[0]["result"] == "duplicate"
    assert (tmp_path / "completion-acks.jsonl").is_file()


def test_ack_ledger_ignores_corrupt_lines(tmp_path: Path) -> None:
    store = _prepare(tmp_path)
    store.record_completion_ack(device_id=DEVICE, session_id=SESSION, job_id="job-a", result="complete")
    path = tmp_path / "completion-acks.jsonl"
    path.write_text(path.read_text(encoding="utf-8") + "{not json}\n", encoding="utf-8")
    acks = store.list_completion_acks()
    assert len(acks) == 1 and acks[0]["job_id"] == "job-a"


# ---------------------------------------------------------------- 诊断
def test_stop_diagnostics_reports_unacked_completion(tmp_path: Path) -> None:
    store = _prepare(tmp_path)
    store.complete_session(device_id=DEVICE, session_id=SESSION, expected_segments=3,
                           ended_at="2026-10-23T17:08:40+08:00")
    diag = store.stop_diagnostics()
    assert diag["counts"]["complete_sessions"] == 1
    assert diag["counts"]["unacked_completions"] == 1
    assert diag["unacked_completions"][0]["session_id"] == SESSION
    assert diag["recent_stops"][0]["ended_by_source"] == "inferred"
    assert diag["end_reason_counts"] == {"completion_request_without_ended_by": 1}

    store.record_completion_ack(device_id=DEVICE, session_id=SESSION, job_id=None, result="complete")
    assert store.stop_diagnostics()["counts"]["unacked_completions"] == 0


def test_stop_diagnostics_explains_pending_completion(tmp_path: Path) -> None:
    store = _prepare(tmp_path, count=3)
    # 声明要 4 段但只到 3 段 → 服务端挂 completion_intent
    with pytest.raises(Exception):
        store.complete_session(device_id=DEVICE, session_id=SESSION, expected_segments=4,
                              ended_at="2026-10-23T17:08:40+08:00")
    diag = store.stop_diagnostics()
    assert diag["counts"]["pending_completion_sessions"] == 1
    pending = diag["pending_completion_sessions"][0]
    assert pending["completion_intent"]["expected_segments"] == 4
    assert pending["received_segments"] == 3


# ---------------------------------------------------------------- 设备状态历史
def test_device_status_keeps_history_and_first_last_seen(tmp_path: Path) -> None:
    store = WatchSessionStore(tmp_path)
    for minute, pending in enumerate([5, 3, 0]):
        store.save_device_status({
            "device_id": DEVICE, "pending_segment_count": pending,
            "pending_segment_bytes": 0, "open_session_count": 0,
            "pending_completion_count": 1, "sync_state": "idle",
            "reported_at": f"2026-10-23T17:0{minute}:00+08:00",
        })
    status = store.list_device_statuses()[0]
    assert status["pending_segment_count"] == 0
    assert [h["pending_segment_count"] for h in status["history"]] == [5, 3, 0]
    assert status["first_seen"] and status["last_seen"]
    assert status["first_seen"] <= status["last_seen"]


def test_device_status_history_is_capped(tmp_path: Path) -> None:
    store = WatchSessionStore(tmp_path)
    total = DEVICE_STATUS_HISTORY_MAX + 5
    for i in range(total):
        store.save_device_status({
            "device_id": DEVICE, "pending_segment_count": i,
            "pending_segment_bytes": 0, "open_session_count": 0,
            "pending_completion_count": 0, "sync_state": "idle",
            "reported_at": f"2026-10-23T{10 + i // 60:02d}:{i % 60:02d}:00+08:00",
        })
    status = store.list_device_statuses()[0]
    assert len(status["history"]) == DEVICE_STATUS_HISTORY_MAX
    assert status["history"][-1]["pending_segment_count"] == total - 1


def test_legacy_single_snapshot_device_status_is_still_readable(tmp_path: Path) -> None:
    """旧文件只有一条快照（没有 history/first_seen）→ 读得出来，下一次上报补齐。"""
    directory = tmp_path / "device-status"
    directory.mkdir(parents=True)
    legacy = {
        "device_id": DEVICE, "reported_at": "2026-10-23T19:02:47+08:00",
        "received_at": "2026-10-23T19:02:47+08:00", "pending_segment_count": 20,
        "pending_segment_bytes": 5257062, "open_session_count": 0,
        "pending_completion_count": 14, "sync_state": "idle",
    }
    (directory / f"{DEVICE}.json").write_text(json.dumps(legacy), encoding="utf-8")
    store = WatchSessionStore(tmp_path)

    status = store.list_device_statuses()[0]
    assert status["pending_segment_count"] == 20
    assert status.get("history") is None  # 旧文件没有历史，不编造

    store.save_device_status({
        "device_id": DEVICE, "pending_segment_count": 0, "pending_segment_bytes": 0,
        "open_session_count": 0, "pending_completion_count": 0, "sync_state": "idle",
        "reported_at": "2026-10-23T19:10:00+08:00",
    })
    merged = store.list_device_statuses()[0]
    assert merged["first_seen"] == "2026-10-23T19:02:47+08:00"
    assert [h["pending_segment_count"] for h in merged["history"]] == [0]


# ---------------------------------------------------------------- API
def test_diagnostics_api_is_read_only_and_requires_auth(tmp_path: Path) -> None:
    from life_logger.local_web import JobStore, create_server

    store = JobStore(tmp_path / "web", recover=False)
    server = create_server(host="127.0.0.1", port=0, store=store,
                           config_path=tmp_path / "config.json", access_token="test-token")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        # 无鉴权 → 401/403
        with pytest.raises(Exception):
            urllib.request.urlopen(f"{base}/api/watch-sessions/diagnostics", timeout=10)

        # [Q161 修复 2026-10-24] 原来比较整个 web/ 根目录的文件列表，会撞上 LocalWebServer
        # 启动后的异步 housekeeping（.auto-ingest.json.*.tmp / watch-sessions/ / .reminders.lock），
        # 与本端点无关却导致 1/2 概率假红。这里只比较诊断端点真正可能碰到的范围。
        ws_root = tmp_path / "web" / "watch-sessions"

        def _snapshot() -> dict:
            out = {}
            for f in sorted(ws_root.rglob("*")) if ws_root.is_dir() else []:
                if f.is_file():
                    stat = f.stat()
                    out[f.relative_to(ws_root).as_posix()] = (stat.st_mtime_ns, stat.st_size)
            return out

        before = _snapshot()
        req = urllib.request.Request(
            f"{base}/api/watch-sessions/diagnostics?recent=5",
            headers={"Authorization": "Bearer test-token",
                     "Origin": urllib.parse.urlparse(base).netloc and f"http://{urllib.parse.urlparse(base).netloc}"},
        )
        with urllib.request.urlopen(req, timeout=10) as response:
            payload = json.loads(response.read())
        assert payload["ok"] is True
        for key in ("recent_stops", "pending_completion_sessions", "unacked_completions", "counts"):
            assert key in payload
        after = _snapshot()
        assert before == after, f"诊断端点不得写 watch 数据，但变了：{set(before) ^ set(after)}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


# ---------------------------------------------------------------------------
# Q161：占位值曾挡住设备重报（inferred → client_reported 必须能升级）+ 枚举校验
# ---------------------------------------------------------------------------
def test_inferred_is_upgraded_when_device_reports_later(tmp_path: Path) -> None:
    """首次 complete 没带 ended_by（写 watch_client/inferred），设备重试补报时必须改写。"""
    store = _prepare(tmp_path)
    first = store.complete_session(
        device_id=DEVICE, session_id=SESSION, expected_segments=3,
        ended_at="2026-10-23T17:08:40+08:00",
    )
    assert first["session"]["ended_by"] == "watch_client"
    assert first["session"]["ended_by_source"] == "inferred"

    second = store.complete_session(
        device_id=DEVICE, session_id=SESSION, expected_segments=3,
        ended_at="2026-10-23T17:08:40+08:00",
        ended_by="battery", end_reason="battery_low",
    )
    manifest = second["session"]
    assert second["duplicate"] is True
    assert manifest["ended_by"] == "battery", "占位值不得再挡住设备真实上报"
    assert manifest["ended_by_source"] == "client_reported"
    assert manifest["end_reason"] == "battery_low"


def test_existing_client_reported_is_never_overwritten(tmp_path: Path) -> None:
    store = _prepare(tmp_path)
    store.complete_session(
        device_id=DEVICE, session_id=SESSION, expected_segments=3,
        ended_at="2026-10-23T17:08:40+08:00", ended_by="user", end_reason="user_stop",
    )
    again = store.complete_session(
        device_id=DEVICE, session_id=SESSION, expected_segments=3,
        ended_at="2026-10-23T17:08:40+08:00", ended_by="battery", end_reason="battery_low",
    )
    assert again["session"]["ended_by"] == "user"
    assert again["session"]["end_reason"] == "user_stop"


def test_legacy_manifest_gets_backfilled_on_retry(tmp_path: Path) -> None:
    """旧 manifest（完全没有 ended_by 字段）在设备重试时会补写成 client_reported。"""
    store = _prepare(tmp_path)
    manifest = store.load_session(DEVICE, SESSION)
    manifest["state"] = "complete"
    manifest.pop("ended_by", None)
    manifest.pop("ended_by_source", None)
    manifest.pop("end_reason", None)
    (tmp_path / DEVICE / SESSION / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
    )
    result = store.complete_session(
        device_id=DEVICE, session_id=SESSION, expected_segments=3,
        ended_at="2026-10-23T17:08:40+08:00", ended_by="disconnect", end_reason="bt_drop",
    )
    assert result["duplicate"] is True
    assert result["session"]["ended_by"] == "disconnect"
    assert result["session"]["ended_by_source"] == "client_reported"
    assert result["session"]["end_reason"] == "bt_drop"


def test_invalid_ended_by_is_kept_with_warning(tmp_path: Path) -> None:
    """未知枚举：保留原文 + 标 invalid_ended_by_enum，不能静默丢弃。"""
    store = _prepare(tmp_path)
    result = store.complete_session(
        device_id=DEVICE, session_id=SESSION, expected_segments=3,
        ended_at="2026-10-23T17:08:40+08:00", ended_by="poked_the_crown",
    )
    manifest = result["session"]
    assert manifest["ended_by"] == "poked_the_crown"
    assert manifest["ended_by_warning"] == "invalid_ended_by_enum"
    assert "invalid_ended_by_enum" in result["warnings"]

    diag = store.stop_diagnostics(recent=5)
    row = [r for r in diag["recent_stops"] if r["session_id"] == SESSION][0]
    assert row["ended_by_warning"] == "invalid_ended_by_enum"


def test_invalid_ended_by_does_not_overwrite_confirmed_value(tmp_path: Path) -> None:
    store = _prepare(tmp_path)
    store.complete_session(
        device_id=DEVICE, session_id=SESSION, expected_segments=3,
        ended_at="2026-10-23T17:08:40+08:00", ended_by="user", end_reason="user_stop",
    )
    again = store.complete_session(
        device_id=DEVICE, session_id=SESSION, expected_segments=3,
        ended_at="2026-10-23T17:08:40+08:00", ended_by="garbage_value",
    )
    assert again["session"]["ended_by"] == "user", "非法值不得覆盖已确认的归因"
    assert again["session"]["ended_by_warning"] == "invalid_ended_by_enum", "但仍要留下告警痕迹"


def test_all_allowed_enum_values_are_accepted(tmp_path: Path) -> None:
    from life_logger.watch_stream import ENDED_BY_VALUES
    assert ENDED_BY_VALUES == ("user", "battery", "disconnect", "app_crash", "watch_client")
    for value in ENDED_BY_VALUES:
        store = WatchSessionStore(tmp_path)
        # 每个值单独会话，避免同 root 互相覆盖
        sid = f"SESS{value.upper().replace('_', '')}"
        store.start_session(device_id=DEVICE, session_id=sid,
                            started_at="2026-10-23T15:00:00+08:00")
        body = b"e" * 64
        store.receive_segment(device_id=DEVICE, session_id=sid, sequence=0, start_offset_ms=0,
                              duration_ms=60000,
                              checksum="sha256:" + hashlib.sha256(body).hexdigest(), body=body)
        result = store.complete_session(device_id=DEVICE, session_id=sid, expected_segments=1,
                                        ended_at="2026-10-23T17:00:00+08:00", ended_by=value)
        assert result["session"]["ended_by"] == value
        assert result["session"].get("ended_by_warning") is None
