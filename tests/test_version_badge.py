"""版本标识与部署状态（`life_logger.version` + 首页角标）回归。

背景：2026-10-19 发现服务连续运行 40 小时、磁盘上 9 个后端文件未加载，
「功能做了却没生效」被反复误判成「功能有 bug」。
这里锁住三件事：
1. 版本快照字段齐全，且 `unloaded_files` 能真的抓到"比进程新"的文件；
2. 部署状态有 current / stale 两态，页面角标据此变红；
3. 角标脚本可被关掉（配置开关），且内容是服务端生成的合法 JS。
"""

from __future__ import annotations

import json
import time

import pytest

import life_logger.version as version


def test_build_info_has_required_fields():
    info = version.build_info()
    for key in ("app_version", "code_fingerprint", "git_commit", "deployed_at", "pid", "deploy_state"):
        assert key in info, key
    assert info["app_version"].count(".") == 2, "语义版本应为 X.Y.Z"
    assert len(info["code_fingerprint"]) == 12
    assert info["deploy_state"] in {"current", "stale"}


def test_unloaded_files_detects_files_newer_than_process(tmp_path, monkeypatch):
    stale_file = tmp_path / "touched_after_start.py"
    stale_file.write_text("# newer than process start\n")
    old_file = tmp_path / "old.py"
    old_file.write_text("# old\n")

    monkeypatch.setattr(version, "_backend_files", lambda: [stale_file, old_file])
    now = time.time()
    monkeypatch.setattr(version, "_PROCESS_STARTED_AT", now)
    import os

    os.utime(old_file, (now - 7200, now - 7200))      # 早于进程启动
    os.utime(stale_file, (now + 60, now + 60))        # 晚于进程启动

    assert version.unloaded_files() == ["touched_after_start.py"]
    info = version.build_info()
    assert info["deploy_state"] == "stale"
    assert info["unloaded_files"] == ["touched_after_start.py"]


def test_deploy_state_is_current_when_nothing_is_newer(tmp_path, monkeypatch):
    fresh = tmp_path / "fresh.py"
    fresh.write_text("# ok\n")
    monkeypatch.setattr(version, "_backend_files", lambda: [fresh])
    monkeypatch.setattr(version, "_PROCESS_STARTED_AT", time.time() + 3600)
    assert version.unloaded_files() == []
    assert version.build_info()["deploy_state"] == "current"


def test_badge_script_contains_both_deploy_states():
    from life_logger.local_web import _VERSION_BADGE_SCRIPT

    script = _VERSION_BADGE_SCRIPT
    assert "__BUILD_INFO__" in script, "必须留占位符给服务端注入运行中版本"
    assert "未部署" in script, "stale 状态必须可见"
    assert 'getElementById("life-logger-version-badge")' in script
    assert "fromCharCode(10)" in script, "不要再用转义换行拼 JS 字符串"
    # 【2026-10-19 修】角标必须**流式插入**，不能浮层压在别的 UI 上：
    # 实测旧版压住侧栏「本机处理 · LAN 8765」，移动端挡住底部导航。
    assert "position:" not in script, "禁止任何浮层定位"
    assert "z-index" not in script, "禁止叠层"
    assert 'getElementById("page-title")' in script, "应插入页面标题容器"


def test_badge_script_is_valid_javascript():
    """用 node 校验服务端生成的脚本（无 node 时跳过）。"""
    import shutil
    import subprocess

    if shutil.which("node") is None:
        pytest.skip("node 不可用")
    from life_logger.local_web import _VERSION_BADGE_SCRIPT

    script = _VERSION_BADGE_SCRIPT.replace("__BUILD_INFO__", json.dumps(version.build_info()))
    result = subprocess.run(["node", "--check"], input=script, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr[:400]


def test_badge_can_be_disabled_by_config(tmp_path):
    from life_logger.local_web import _version_badge_enabled

    config = tmp_path / "local-config.json"
    config.write_text(json.dumps({"web_version_badge_enabled": False}))
    assert _version_badge_enabled(config) is False

    config.write_text(json.dumps({"web_version_badge_enabled": True}))
    assert _version_badge_enabled(config) is True

    # 缺字段/坏文件都默认开启（角标本身是只读信息，默认可见更有价值）
    config.write_text(json.dumps({}))
    assert _version_badge_enabled(config) is True
    config.write_text("{not json")
    assert _version_badge_enabled(config) is True
    assert _version_badge_enabled(tmp_path / "missing.json") is True
