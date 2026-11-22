"""Tests for InputContextRuntimeManager integration with LocalWebServer lifecycle.

Covers:
1. Default behavior (missing or disabled config): input_context_manager is NOT started (zero side effects).
2. Explicit config input_context_capture.enabled = true: input_context_manager is started on combined mode.
3. server_close stops input_context_manager safely and idempotently.
4. Exception during manager init/start/stop does not block web server creation or teardown.
5. In split processing mode, input_context_manager is NOT auto-started (prevents multi-process duplicate collection).
6. Root path alignment: runtime.artifact_path strictly equals watch_session_store.root / INPUT_CONTEXT_ARTIFACT.
7. Full pipeline integration: real manager + fake collector in shared temp root -> watch load -> row / packet contains provider and target.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock


from life_logger.contextual_extraction import _deepseek_packet_records
from life_logger.input_context_runtime import InputContextRuntimeManager
from life_logger.local_web import JobStore, create_server
from life_logger.watch_formal_asr import (
    INPUT_CONTEXT_ARTIFACT,
    attach_watch_input_context,
    load_input_context_windows,
)

SHANGHAI = timezone(timedelta(hours=8))


def _write_config(path: Path, *, enabled: bool | None = None) -> Path:
    cfg: dict = {"input": "dummy"}
    if enabled is not None:
        cfg["input_context_capture"] = {"enabled": enabled}
    path.write_text(json.dumps(cfg), encoding="utf-8")
    return path


def test_default_config_does_not_start_manager(tmp_path: Path):
    """缺设置/默认配置：manager 保持实例化但绝不自动 start。"""
    config_path = _write_config(tmp_path / "config.json", enabled=None)
    fake_mgr = MagicMock()
    fake_mgr.is_running = False

    server = create_server(
        host="127.0.0.1",
        port=0,
        store=JobStore(tmp_path / "store", recover=False),
        config_path=config_path,
        processing_mode="combined",
        input_context_manager=fake_mgr,
    )
    try:
        assert server.input_context_manager is fake_mgr
        fake_mgr.start.assert_not_called()
    finally:
        server.server_close()
        fake_mgr.stop.assert_called_once()


def test_explicit_enabled_starts_manager_in_combined_mode(tmp_path: Path):
    """显式配置 input_context_capture.enabled=true 时，combined 模式调用 start()。"""
    config_path = _write_config(tmp_path / "config.json", enabled=True)
    fake_mgr = MagicMock()

    server = create_server(
        host="127.0.0.1",
        port=0,
        store=JobStore(tmp_path / "store", recover=False),
        config_path=config_path,
        processing_mode="combined",
        input_context_manager=fake_mgr,
    )
    try:
        fake_mgr.start.assert_called_once()
    finally:
        server.server_close()
        fake_mgr.stop.assert_called_once()


def test_split_mode_does_not_auto_start_to_prevent_duplicate_collection(tmp_path: Path):
    """processing_mode=split 时，不自动 start 避免多进程重复采集。

    [2026-11-04] external 模式已删除，本用例改为守护仍然存在的 split 模式：
    非 combined 模式下采集只能由一方启动。
    """
    config_path = _write_config(tmp_path / "config.json", enabled=True)
    fake_mgr = MagicMock()

    server = create_server(
        host="127.0.0.1",
        port=0,
        store=JobStore(tmp_path / "store", recover=False),
        config_path=config_path,
        processing_mode="split",
        input_context_manager=fake_mgr,
    )
    try:
        fake_mgr.start.assert_not_called()
    finally:
        server.server_close()
        fake_mgr.stop.assert_called_once()


def test_server_close_is_safe_and_idempotent_with_manager(tmp_path: Path):
    """server_close 对重复调用免疫，stop 抛异常不阻塞关闭。"""
    config_path = _write_config(tmp_path / "config.json", enabled=False)
    fake_mgr = MagicMock()
    fake_mgr.stop.side_effect = RuntimeError("stop error")

    server = create_server(
        host="127.0.0.1",
        port=0,
        store=JobStore(tmp_path / "store", recover=False),
        config_path=config_path,
        input_context_manager=fake_mgr,
    )
    # 调用两次 server_close 不抛异常
    server.server_close()
    server.server_close()
    assert fake_mgr.stop.call_count == 2


def test_root_path_alignment_between_runtime_and_watch_reader(tmp_path: Path):
    """【路径对齐硬性契约】runtime.artifact_path 必须严格等于 watch_session_store.root / INPUT_CONTEXT_ARTIFACT。"""
    config_path = _write_config(tmp_path / "config.json", enabled=False)
    store = JobStore(tmp_path / "output" / "local-web", recover=False)

    server = create_server(
        host="127.0.0.1",
        port=0,
        store=store,
        config_path=config_path,
        processing_mode="combined",
    )
    try:
        expected_read_path = server.watch_session_store.root / INPUT_CONTEXT_ARTIFACT
        actual_write_path = server.input_context_manager.artifact_path
        assert actual_write_path == expected_read_path, f"{actual_write_path} != {expected_read_path}"
        assert server.input_context_manager.store_root == server.watch_session_store.root
    finally:
        server.server_close()


def test_full_pipeline_shared_root_write_read_attach_packet(tmp_path: Path):
    """真实 manager + fake collector 同一临时 root：生成闭合窗 -> Watch 读取 -> row/packet 携带 provider 和 target。"""
    config_path = _write_config(tmp_path / "config.json", enabled=False)
    store = JobStore(tmp_path / "output" / "local-web", recover=False)
    watch_root = store.root / "watch-sessions"

    now = datetime(2026, 10, 26, 10, 5, 0, tzinfo=SHANGHAI)
    w_start = now - timedelta(minutes=2)
    w_end = now + timedelta(minutes=2)

    fake_coll = MagicMock()
    fake_coll.windows = [{
        "started_at": w_start.isoformat(),
        "ended_at": w_end.isoformat(),
        "target_app": "com.openai.codex",
        "target_unknown": False,
        "complete": True,
        "front_bundles": ["com.openai.codex"],
        "provider": "DoubaoIme",
    }]

    real_mgr = InputContextRuntimeManager(watch_root, collector=fake_coll)
    server = create_server(
        host="127.0.0.1",
        port=0,
        store=store,
        config_path=config_path,
        processing_mode="combined",
        input_context_manager=real_mgr,
    )
    try:
        # 1. 触发一轮采样发布
        real_mgr.step_once(at=now)
        assert real_mgr.artifact_path.is_file()

        # 2. Watch 从同一 watch_session_store.root 读取
        windows = load_input_context_windows(server.watch_session_store.root)
        assert len(windows) == 1
        assert windows[0]["target_app"] == "com.openai.codex"
        assert windows[0]["provider"] == "DoubaoIme"

        # 3. 关联到行
        row = {
            "source_id": "src_watch_01",
            "segment_id": "watch_000001_000001",
            "recording_id": "rec_01",
            "text": "测试输入指令",
            "source_label": "self",
            "timestamp": now.isoformat(),
            "utterance_timestamp": now.isoformat(),
            "start_offset_seconds": 0.0,
            "end_offset_seconds": 5.0,
            "is_dictation": False,
            "absolute_started_at": (now - timedelta(seconds=10)).isoformat(),
            "absolute_ended_at": (now + timedelta(seconds=10)).isoformat(),
        }
        stats = attach_watch_input_context([row], windows)
        assert stats["resolved"] == 1
        assert row.get("input_context") is not None
        assert row["input_context"]["target_bundle"] == "com.openai.codex"
        assert row["input_context"]["provider"] == "DoubaoIme"

        # 4. 进入模型抽取 packet 检查白名单透传
        packet = _deepseek_packet_records([row], [])
        assert len(packet) == 1
        assert packet[0].get("input_context") is not None
        assert packet[0]["input_context"]["target_bundle"] == "com.openai.codex"
        assert packet[0]["input_context"]["provider"] == "DoubaoIme"
    finally:
        server.server_close()
