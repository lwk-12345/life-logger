"""run-with-timeout.py 回归测试（进程外硬超时执行器）。

背景（2026-10-24 P0）：Pi 会话里出现过 pytest / Chrome CDP 无界挂起
（一次 pytest 在 12% 卡了 43 分钟）。这个脚本是进程外兜底，必须保证：
正常命令回原退出码、失败命令回原退出码、超时必退 124、
父进程退出后孙进程被清理、输出能完整落盘。
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "run-with-timeout.py"


def _run(args, timeout=60):
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True, text=True, timeout=timeout,
    )


def test_normal_command_returns_child_exit_code_and_output():
    result = _run(["10", "--", "echo", "hello-from-child"])
    assert result.returncode == 0, result.stderr
    assert "hello-from-child" in result.stdout


def test_failing_command_keeps_original_exit_code():
    result = _run(["10", "--", "sh", "-c", "echo boom; exit 7"])
    assert result.returncode == 7, (result.returncode, result.stdout, result.stderr)
    assert "boom" in result.stdout


def test_timeout_returns_124_and_does_not_hang():
    started = time.monotonic()
    result = _run(["1", "--grace", "1", "--", "sleep", "30"])
    elapsed = time.monotonic() - started
    assert result.returncode == 124, (result.returncode, result.stdout, result.stderr)
    assert elapsed < 15, f"超时后仍等了 {elapsed:.1f}s（没有真正 kill）"
    assert "TIMEOUT after 1.0s" in result.stdout


def test_stubborn_child_that_ignores_sigterm_is_killed():
    """忽略 SIGTERM 的子进程必须在 grace 后被 SIGKILL（否则又是挂起）。"""
    started = time.monotonic()
    result = _run(["1", "--grace", "1", "--", "sh", "-c",
                   "trap '' TERM; sleep 30"])
    elapsed = time.monotonic() - started
    assert result.returncode == 124
    assert elapsed < 20, f"忽略 TERM 的进程没被 KILL，耗时 {elapsed:.1f}s"


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def test_grandchild_is_cleaned_up_after_parent_exits(tmp_path):
    """父进程退出了但留下孙进程（sleep 30）→ 必须被一起清理，不能变成孤儿。"""
    pidfile = tmp_path / "grandchild.pid"
    result = _run(["20", "--", "sh", "-c",
                   f"sh -c 'sleep 30' & echo $! > {pidfile}; echo spawned; exit 0"])
    assert result.returncode == 0, result.stderr
    assert "spawned" in result.stdout
    grandchild = int(pidfile.read_text().strip())
    for _ in range(50):                     # 最多等 5s，等它被回收
        if not _pid_alive(grandchild):
            break
        time.sleep(0.1)
    assert not _pid_alive(grandchild), f"孙进程 {grandchild} 未被清理（变成孤儿）"


def test_output_is_written_to_log_file(tmp_path):
    log = tmp_path / "nested" / "run.log"
    result = _run(["10", "--log", str(log), "--", "sh", "-c", "echo to-file; exit 3"])
    assert result.returncode == 3
    assert log.is_file(), "日志文件没有生成"
    assert "to-file" in log.read_text()
    assert log.read_text() == result.stdout


def test_timeout_message_is_also_written_to_log(tmp_path):
    log = tmp_path / "timeout.log"
    result = _run(["1", "--grace", "1", "--log", str(log), "--", "sleep", "30"])
    assert result.returncode == 124
    text = log.read_text()
    assert "TIMEOUT after 1.0s" in text


@pytest.mark.parametrize("args", [["0", "--", "echo", "x"], ["5"], ["5", "--"]])
def test_usage_errors_exit_120(args):
    result = _run(args)
    assert result.returncode == 120, (result.returncode, result.stderr)


def test_no_orphan_process_group_members_after_normal_run(tmp_path):
    """正常退出时同组残留也要清掉（用 PID 文件判定，避免 pgrep 误伤别的进程）。"""
    pidfile = tmp_path / "bg.pid"
    result = _run(["20", "--", "sh", "-c", f"sleep 30 >/dev/null 2>&1 & echo $! > {pidfile}; echo started"])
    assert result.returncode == 0
    assert "started" in result.stdout
    pid = int(pidfile.read_text().strip())
    for _ in range(50):
        if not _pid_alive(pid):
            break
        time.sleep(0.1)
    assert not _pid_alive(pid), f"同组残留进程 {pid} 未被清理"


# ---------------------------------------------------------------------------
# 红线（2026-10-24 返工）：信号致死 / 用法错误统一 120 / 日志不可写
# ---------------------------------------------------------------------------
def test_child_killed_by_signal_returns_128_plus_signal():
    """子进程被 SIGKILL 杀死时返回 137（128+9），不是 247。"""
    result = _run(["10", "--", "sh", "-c", "kill -9 $$"])
    assert result.returncode == 137, (result.returncode, result.stdout, result.stderr)


def test_child_killed_by_sigterm_returns_143():
    result = _run(["10", "--", "sh", "-c", "kill -TERM $$"])
    assert result.returncode == 143, result.returncode


@pytest.mark.parametrize(
    "args",
    [
        ["10", "echo", "x"],          # 省略 -- ，argparse 不再自己退 2
        ["10", "--unknown", "--", "true"],
        ["abc", "--", "true"],        # seconds 不是数字
    ],
)
def test_malformed_usage_exits_120(args):
    result = _run(args)
    assert result.returncode == 120, (result.returncode, result.stderr)
    assert "Traceback" not in result.stderr


def test_unwritable_log_exits_120_without_traceback(tmp_path):
    """--log 指到不可写目录：报明确错误并退 120，不能 Traceback/1。"""
    readonly = tmp_path / "readonly"
    readonly.mkdir()
    os.chmod(readonly, 0o500)
    try:
        result = _run(["5", "--log", str(readonly / "run.log"), "--", "true"])
    finally:
        os.chmod(readonly, 0o700)
    assert result.returncode == 120, (result.returncode, result.stdout, result.stderr)
    assert "Traceback" not in result.stderr
    assert "无法写日志" in result.stderr
