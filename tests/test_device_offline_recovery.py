# -*- coding: utf-8 -*-
"""device_offline 的**恢复路径**回归：失联 → 手表重新上报 → 自动消警。

为什么需要这批测试
------------------
线上形态：手表失联 17h，`device_offline.level=alert`、前端红条常驻。
手表重新连上后**只补传分段、不再发状态快照** —— 旧实现只拿设备自报的 `reported_at`
算 silence，于是 silence 继续增长，alert 永不消失（用户看到永久红灯）。

本文件锁定三件事：
  1. 纯设备自报失联 → alert；
  2. **只补传分段（服务端 manifest 有新 mtime）→ 必须恢复成 ok，并给出 recovered_at**；
  3. 既没有设备状态 → level 仍是 None（"不知道" ≠ "正常"，既有契约不能破）。
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from life_logger.local_web import _device_offline_summary  # noqa: E402
from life_logger.watch_stream import (  # noqa: E402
    LOCAL_TZ,
    DEVICE_OFFLINE_ALERT_SECONDS,
    DEVICE_OFFLINE_WARN_SECONDS,
    WatchSessionStore,
)

DEVICE = "watchTESTDEVICE0001"
SESSION = "3F2A1B4C5D6E7F8091A2B3C4D5E6F708"


def _write_status(root: Path, *, reported_at: datetime, pending: int = 0) -> Path:
    """按 record_device_status 的落盘形状写一份设备状态快照。"""
    path = root / "device-status" / f"{DEVICE}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "device_id": DEVICE,
        "reported_at": reported_at.isoformat(),
        "pending_segment_count": pending,
        "pending_segment_bytes": 0,
        "open_session_count": 0,
        "pending_completion_count": 0,
        "sync_state": "idle",
        "received_at": reported_at.isoformat(),
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _write_manifest(root: Path, *, mtime: datetime | None = None, expected: int = 1) -> Path:
    """造一个"服务端已经收过分段"的会话 manifest。

    分段被接受时 `record_segment` 会重写 manifest → 其 mtime 就是**服务端视角**的存活证据。
    """
    session_dir = root / DEVICE / SESSION
    session_dir.mkdir(parents=True, exist_ok=True)
    path = session_dir / "manifest.json"
    manifest = {
        "session_id": SESSION,
        "device_id": DEVICE,
        "state": "recording",
        "expected_segments": expected,
        "segments": [{
            "sequence": 0,
            "start_offset_ms": 0,
            "duration_ms": 60000,
            "file": "segment-000000.m4a",
            "sha256": "0" * 64,
        }],
        "started_at": (mtime or datetime.now(LOCAL_TZ)).isoformat(),
    }
    path.write_text(json.dumps(manifest), encoding="utf-8")
    if mtime is not None:
        ts = mtime.timestamp()
        os.utime(path, (ts, ts))
    return path


def _statuses(root: Path) -> list[dict]:
    return WatchSessionStore(root).list_device_statuses()


def test_offline_alert_when_only_device_report_is_stale(tmp_path: Path):
    """只有设备自报、且自报已 17 小时 → alert（原行为不变）。"""
    root = tmp_path / "watch"
    root.mkdir()
    stale = datetime.now(LOCAL_TZ) - timedelta(hours=17)
    _write_status(root, reported_at=stale)

    statuses = _statuses(root)
    assert len(statuses) == 1
    assert statuses[0]["offline"] == "alert"
    assert statuses[0]["silent_seconds"] >= DEVICE_OFFLINE_ALERT_SECONDS
    assert statuses[0]["recovered_at"] is None

    summary = _device_offline_summary(statuses)
    assert summary["level"] == "alert"
    assert summary["recovered_at"] is None
    assert "17" in summary["message"]


def test_recovery_by_server_segment_receipt_clears_alert(tmp_path: Path):
    """**关键场景**：手表只补传分段、不发状态 → 必须恢复成 ok。

    设备自报仍然是 17 小时前，但服务端刚刚收到了分段（manifest mtime = now）。
    """
    root = tmp_path / "watch"
    root.mkdir()
    stale = datetime.now(LOCAL_TZ) - timedelta(hours=17)
    _write_status(root, reported_at=stale)
    assert _device_offline_summary(_statuses(root))["level"] == "alert"      # 恢复前：红

    _write_manifest(root)                                                    # 手表开始补传分段

    statuses = _statuses(root)
    st = statuses[0]
    assert st["offline"] is None, "服务端刚收到分段，不能再判失联"
    assert st["last_evidence_kind"] == "server_segment_receipt"
    assert st["recovered_at"] is not None

    summary = _device_offline_summary(statuses)
    assert summary["level"] == "ok", "恢复后必须明确 ok（而不是让前端猜空字段）"
    assert summary["recovered_at"] is not None
    assert summary["message"] is None
    assert summary["recovered_devices"][0]["device_id"] == DEVICE
    assert summary["silent_seconds"] < DEVICE_OFFLINE_WARN_SECONDS


def test_recovery_by_fresh_device_status(tmp_path: Path):
    """正常路径：手表重新发状态快照 → ok + recovered_at。"""
    root = tmp_path / "watch"
    root.mkdir()
    _write_status(root, reported_at=datetime.now(LOCAL_TZ) - timedelta(hours=17))
    assert _device_offline_summary(_statuses(root))["level"] == "alert"

    _write_status(root, reported_at=datetime.now(LOCAL_TZ))                  # 恢复上报

    statuses = _statuses(root)
    assert statuses[0]["offline"] is None
    summary = _device_offline_summary(statuses)
    assert summary["level"] == "ok"
    assert summary["recovered_at"] is not None


def test_no_device_status_keeps_level_none(tmp_path: Path):
    """没有任何设备状态 → level 仍是 None（既有契约：不知道 ≠ 正常）。

    对应 tests/test_watch_stream_api.py 里那条 `level is None` 断言。
    """
    root = tmp_path / "watch"
    root.mkdir()
    assert _statuses(root) == []
    summary = _device_offline_summary([])
    assert summary["level"] is None
    assert summary["recovered_at"] is None


def test_alert_while_still_silent_even_with_old_manifest(tmp_path: Path):
    """manifest 也是 17 小时前 → 仍然 alert（恢复证据必须是**新的**才算数）。"""
    root = tmp_path / "watch"
    root.mkdir()
    stale = datetime.now(LOCAL_TZ) - timedelta(hours=17)
    _write_status(root, reported_at=stale)
    _write_manifest(root, mtime=stale)

    statuses = _statuses(root)
    assert statuses[0]["offline"] == "alert"
    assert statuses[0]["recovered_at"] is None
    assert _device_offline_summary(statuses)["level"] == "alert"


def test_recovery_is_driven_by_server_receipt_not_by_luck(tmp_path: Path, monkeypatch):
    """证明「恢复」确实是**服务端收到分段**这条新证据决定的（否则就是假绿）。

    做法：把服务端证据掐掉（模拟旧实现只看 reported_at），同一个"刚补传完"的现场，
    必须回到 alert。这样一旦有人把 last_receipt_at 删掉，这条测试立刻红。
    """
    root = tmp_path / "watch"
    root.mkdir()
    stale = datetime.now(LOCAL_TZ) - timedelta(hours=17)
    _write_status(root, reported_at=stale)
    _write_manifest(root)                      # 服务端刚收到分段

    assert _device_offline_summary(_statuses(root))["level"] == "ok"   # 新逻辑：恢复

    store = WatchSessionStore(root)
    original = type(store)._device_manifest_facts

    def _without_receipt(self, device_id):     # 掐掉服务端证据
        facts = original(self, device_id)
        return {**facts, "last_receipt_at": None}

    monkeypatch.setattr(WatchSessionStore, "_device_manifest_facts", _without_receipt)
    # 状态文件的 received_at 也是恢复证据，一并掐掉（模拟"只补传分段、从不发状态"的旧判据）
    status_path = root / "device-status" / f"{DEVICE}.json"
    payload = json.loads(status_path.read_text(encoding="utf-8"))
    payload.pop("received_at", None)
    status_path.write_text(json.dumps(payload), encoding="utf-8")

    summary = _device_offline_summary(WatchSessionStore(root).list_device_statuses())
    assert summary["level"] == "alert", "去掉服务端证据后必须回到 alert（否则恢复判定与证据无关）"
