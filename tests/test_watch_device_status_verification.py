"""设备状态自报值 vs 服务端 manifest 的对账校验。

背景（2026-10-23 实测）：设备 19:02 自报 pending_segment_count=20 / pending_completion=14，
而服务端 142 个会话 expected 11131 / received 11180 / missing 0 / completion_intent 0。
自报数只能代表"设备本地队列"，不能当真实积压 —— 所以服务端必须在读的时候叠一层校验。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from life_logger.watch_stream import (
    DEVICE_STATUS_FRESHNESS_SECONDS,
    WatchSessionStore,
)

DEVICE = "watch-test-device"


def _write_manifest(root: Path, session: str, *, sequences, expected) -> None:
    directory = root / DEVICE / session
    directory.mkdir(parents=True, exist_ok=True)
    manifest = {
        "device_id": DEVICE,
        "session_id": session,
        "state": "complete",
        "expected_segments": expected,
        "segments": [
            {"sequence": seq, "duration_ms": 60_000, "size_bytes": 1024, "file": f"segment-{seq:06d}.m4a"}
            for seq in sequences
        ],
    }
    (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


def _write_device_status(root: Path, *, reported_at: datetime, pending_segments: int,
                         pending_completions: int = 0) -> Path:
    directory = root / "device-status"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{DEVICE}.json"
    path.write_text(
        json.dumps(
            {
                "device_id": DEVICE,
                "open_session_count": 0,
                "pending_completion_count": pending_completions,
                "pending_segment_bytes": 5_257_062,
                "pending_segment_count": pending_segments,
                "reported_at": reported_at.isoformat(),
                "received_at": reported_at.isoformat(),
                "sync_state": "idle",
            }
        ),
        encoding="utf-8",
    )
    return path


def _now() -> datetime:
    return datetime.now(timezone.utc).astimezone()


def test_stale_self_report_with_zero_server_gap_is_not_real_backlog(tmp_path: Path) -> None:
    """自报 20 段待传，但服务端没有任何缺段 → 不能算真实积压。"""
    _write_manifest(tmp_path, "session-a", sequences=range(0, 5), expected=5)
    _write_device_status(
        tmp_path,
        reported_at=_now() - timedelta(minutes=150),
        pending_segments=20,
        pending_completions=14,
    )

    status = WatchSessionStore(tmp_path).list_device_statuses()[0]

    assert status["missing_segments"] == 0
    assert status["pending_is_real"] is False
    assert status["reported_only_segments"] == 20
    assert status["stale"] is True
    assert status["reported_age_seconds"] >= DEVICE_STATUS_FRESHNESS_SECONDS
    assert status["reported_vs_manifest"]["verdict"] == "reported_only_stale"
    assert status["reported_vs_manifest"]["server_missing_segments"] == 0
    assert "不能当真实积压" in status["reported_vs_manifest"]["note"]
    # 自报值必须原样保留，方便前端同时展示“设备说有”和“服务端确认”。
    assert status["pending_segment_count"] == 20
    assert status["pending_completion_count"] == 14


def test_real_missing_segments_are_reported_as_real(tmp_path: Path) -> None:
    """服务端 manifest 真的缺段 → missing>0 且 pending_is_real。"""
    _write_manifest(tmp_path, "session-b", sequences=[0, 1, 2], expected=5)
    _write_device_status(tmp_path, reported_at=_now(), pending_segments=2, pending_completions=1)

    status = WatchSessionStore(tmp_path).list_device_statuses()[0]

    assert status["missing_segments"] == 2
    assert status["pending_is_real"] is True
    assert status["reported_vs_manifest"]["verdict"] == "server_confirms_missing"
    assert status["server_expected_segments"] == 5
    assert status["server_received_segments"] == 3


def test_extra_segments_are_counted_but_never_offset_missing(tmp_path: Path) -> None:
    """多收段（sequence ≥ expected）单独计数，不许拿来抵缺口。"""
    _write_manifest(tmp_path, "session-c", sequences=[0, 1, 2, 3, 4], expected=3)
    _write_device_status(tmp_path, reported_at=_now(), pending_segments=0)

    status = WatchSessionStore(tmp_path).list_device_statuses()[0]

    assert status["extra_segments"] == 2
    assert status["missing_segments"] == 0
    assert status["server_expected_segments"] == 3
    assert status["server_received_segments"] == 5


def test_extra_and_missing_together_are_reported_separately(tmp_path: Path) -> None:
    """既缺又多的会话：两个数各自成立，互不抵消。"""
    _write_manifest(tmp_path, "session-d", sequences=[0, 2, 5], expected=4)
    _write_device_status(tmp_path, reported_at=_now(), pending_segments=1)

    status = WatchSessionStore(tmp_path).list_device_statuses()[0]

    assert status["missing_segments"] == 2  # 1 和 3
    assert status["extra_segments"] == 1  # 5
    assert status["pending_is_real"] is True


def test_fresh_consistent_report_is_clean(tmp_path: Path) -> None:
    _write_manifest(tmp_path, "session-e", sequences=range(0, 3), expected=3)
    _write_device_status(tmp_path, reported_at=_now(), pending_segments=0, pending_completions=0)

    status = WatchSessionStore(tmp_path).list_device_statuses()[0]

    assert status["stale"] is False
    assert status["pending_is_real"] is False
    assert status["reported_vs_manifest"]["verdict"] == "clean"


def test_fresh_report_with_pending_but_no_server_gap_is_reported_only(tmp_path: Path) -> None:
    """快照还很新，但服务端看不到缺口 → 标成“设备说有，服务端未证实”。"""
    _write_manifest(tmp_path, "session-f", sequences=range(0, 2), expected=2)
    _write_device_status(tmp_path, reported_at=_now(), pending_segments=5)

    status = WatchSessionStore(tmp_path).list_device_statuses()[0]

    assert status["stale"] is False
    assert status["missing_segments"] == 0
    assert status["reported_only_segments"] == 5
    assert status["pending_is_real"] is False
    assert status["reported_vs_manifest"]["verdict"] == "reported_only"


def test_freshness_window_boundary(tmp_path: Path) -> None:
    _write_manifest(tmp_path, "session-g", sequences=[0], expected=1)
    fresh = WatchSessionStore(tmp_path)
    _write_device_status(
        tmp_path,
        reported_at=_now() - timedelta(seconds=DEVICE_STATUS_FRESHNESS_SECONDS - 60),
        pending_segments=0,
    )
    assert fresh.list_device_statuses()[0]["stale"] is False

    _write_device_status(
        tmp_path,
        reported_at=_now() - timedelta(seconds=DEVICE_STATUS_FRESHNESS_SECONDS + 60),
        pending_segments=0,
    )
    assert fresh.list_device_statuses()[0]["stale"] is True


def test_manifest_without_expected_segments_does_not_invent_gaps(tmp_path: Path) -> None:
    """老 manifest 没有 expected_segments 时，不许凭空算缺口。"""
    directory = tmp_path / DEVICE / "session-h"
    directory.mkdir(parents=True)
    (directory / "manifest.json").write_text(
        json.dumps({"device_id": DEVICE, "state": "complete",
                    "segments": [{"sequence": 0}, {"sequence": 1}]}),
        encoding="utf-8",
    )
    _write_device_status(tmp_path, reported_at=_now(), pending_segments=0)

    status = WatchSessionStore(tmp_path).list_device_statuses()[0]

    assert status["missing_segments"] == 0
    assert status["extra_segments"] == 0
    assert status["server_expected_segments"] == 0


def test_malformed_counts_do_not_crash_verification(tmp_path: Path) -> None:
    _write_manifest(tmp_path, "session-i", sequences=[0], expected=1)
    path = _write_device_status(tmp_path, reported_at=_now(), pending_segments=0)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["pending_segment_count"] = "not-a-number"
    path.write_text(json.dumps(payload), encoding="utf-8")

    status = WatchSessionStore(tmp_path).list_device_statuses()[0]

    assert status["reported_vs_manifest"]["reported_pending_segment_count"] == 0
    assert status["missing_segments"] == 0


def test_verification_does_not_rewrite_snapshot_file(tmp_path: Path) -> None:
    """校验只在读取时叠加，快照文件必须原样保留（厂商自报是证据）。"""
    _write_manifest(tmp_path, "session-j", sequences=[0], expected=1)
    path = _write_device_status(tmp_path, reported_at=_now(), pending_segments=3)
    before = path.read_bytes()

    WatchSessionStore(tmp_path).list_device_statuses()

    assert path.read_bytes() == before


def test_save_device_status_response_includes_verification(tmp_path: Path) -> None:
    _write_manifest(tmp_path, "session-k", sequences=[0, 1], expected=3)
    store = WatchSessionStore(tmp_path)

    result = store.save_device_status(
        {
            "device_id": DEVICE,
            "pending_segment_count": 1,
            "pending_segment_bytes": 1024,
            "open_session_count": 0,
            "pending_completion_count": 0,
            "sync_state": "idle",
            "reported_at": _now().isoformat(),
        }
    )

    assert result["accepted"] is True
    assert result["missing_segments"] == 1
    assert result["pending_is_real"] is True
    assert result["reported_vs_manifest"]["verdict"] == "server_confirms_missing"
    assert result["stale"] is False


def test_ignored_older_report_still_returns_verification(tmp_path: Path) -> None:
    """设备补发旧快照被拒时，回包也要带校验字段，不能只回一个 accepted:false。"""
    _write_manifest(tmp_path, "session-l", sequences=[0], expected=1)
    store = WatchSessionStore(tmp_path)
    newer = _now()
    store.save_device_status(
        {
            "device_id": DEVICE,
            "pending_segment_count": 0,
            "pending_segment_bytes": 0,
            "open_session_count": 0,
            "pending_completion_count": 0,
            "sync_state": "idle",
            "reported_at": newer.isoformat(),
        }
    )

    result = store.save_device_status(
        {
            "device_id": DEVICE,
            "pending_segment_count": 9,
            "pending_segment_bytes": 0,
            "open_session_count": 0,
            "pending_completion_count": 0,
            "sync_state": "idle",
            "reported_at": (newer - timedelta(minutes=30)).isoformat(),
        }
    )

    assert result["accepted"] is False
    assert result["pending_segment_count"] == 0  # 仍是较新的那份
    assert "reported_vs_manifest" in result
