#!/usr/bin/env python3
"""带硬超时的命令执行器：跑任何命令都不会无界挂起。

为什么需要它（2026-10-24）：Pi/agent 会话里多次出现 pytest / Chrome CDP
无界挂起（有一次 pytest 在 12% 卡了 43 分钟），而 shell 层的 `timeout` 在
macOS 上默认不存在，`pytest-timeout` 又只管测试进程内部。这个脚本是**进程外**
的兜底：新会话（setsid）+ 进程组信号，超时必退，固定 exit 124。

用法::

    scripts/run-with-timeout.py 120 -- pytest tests/ -q
    scripts/run-with-timeout.py 600 --grace 10 --log /tmp/run.log -- npm test
    scripts/run-with-timeout.py 30 -- curl -s http://127.0.0.1:8765/api/health

语义：
* 正常结束 → 返回子进程的原退出码；
* 超时 → 先 SIGTERM **整个进程组**，等 `--grace` 秒仍未退则 SIGKILL 整组；
* 无论正常/超时，子进程退出后都会清理**同进程组残留**（父进程退了但孙进程还在的场景）；
* stdout/stderr 实时合并输出，并可 `--log` 落盘；
* 超时退出码固定 **124**（与 GNU timeout 一致），120 留给「自身异常」。
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
import threading
import time
from pathlib import Path
from typing import IO, Sequence

EXIT_TIMEOUT = 124
EXIT_USAGE = 120
DEFAULT_GRACE_SECONDS = 5.0
POLL_INTERVAL_SECONDS = 0.05


class _UsageParser(argparse.ArgumentParser):
    """argparse 的用法错误统一退 120（而不是 argparse 默认的 2）。

    为什么：调用方用 120 判断「自己的参数没给对」，124 判断「被超时杀掉」。
    argparse 默认的 2 会和「子进程恰好退出 2」混淆。
    """

    def error(self, message: str) -> None:  # type: ignore[override]
        self.print_usage(sys.stderr)
        print(f"{self.prog}: error: {message}", file=sys.stderr)
        raise SystemExit(EXIT_USAGE)


def _pump(stream: IO[bytes], sinks: Sequence[IO]) -> None:
    """把子进程输出按块转发给多个 sink（stdout + 可选日志）。"""
    while True:
        chunk = stream.read(8192)
        if not chunk:
            return
        for sink in sinks:
            try:
                sink.write(chunk)
                sink.flush()
            except (BrokenPipeError, ValueError):
                continue


def _signal_group(pid: int, sig: int) -> None:
    """向**进程组**发信号。

    `start_new_session=True` 会让子进程成为新会话首进程，因此 **pgid == pid**；
    这里直接用 `killpg(pid, ...)`，**不能**先用 `os.getpgid(pid)` ——
    子进程已退出时 getpgid 会抛 ProcessLookupError，于是孙进程就成了孤儿
    （实测：`sh -c 'sleep 30 &'` 的 sleep 会残留）。
    """
    try:
        os.killpg(pid, sig)
        return
    except (ProcessLookupError, PermissionError, OSError):
        pass
    try:
        os.kill(pid, sig)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def run_with_timeout(
    command: Sequence[str],
    *,
    timeout: float,
    grace: float = DEFAULT_GRACE_SECONDS,
    log_path: Path | None = None,
    stdout: IO | None = None,
) -> int:
    """执行命令并保证在 timeout+grace 内返回。返回退出码（超时=124）。"""
    import subprocess

    out = stdout if stdout is not None else sys.stdout.buffer
    log_file: IO[bytes] | None = None
    if log_path is not None:
        # 日志目录不可写时不能抛 Traceback（那会退 1，与「子进程失败」混淆）。
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_file = log_path.open("wb")
        except OSError as error:
            print(
                f"run-with-timeout: 无法写日志 {log_path}: {error.strerror or error}",
                file=sys.stderr,
            )
            return EXIT_USAGE

    sinks: list[IO] = [out]
    if log_file is not None:
        sinks.append(log_file)

    started = time.monotonic()
    process = subprocess.Popen(
        list(command),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,   # setsid：独立进程组，pgid = pid
        bufsize=0,
    )
    assert process.stdout is not None
    pump = threading.Thread(target=_pump, args=(process.stdout, sinks), daemon=True)
    pump.start()

    timed_out = False
    try:
        while True:
            remaining = timeout - (time.monotonic() - started)
            if remaining <= 0:
                timed_out = True
                break
            try:
                process.wait(timeout=min(POLL_INTERVAL_SECONDS, remaining))
                break
            except subprocess.TimeoutExpired:
                continue
    finally:
        if timed_out:
            # 先 TERM 整组（子进程 + 孙进程），宽限后 KILL 整组
            _signal_group(process.pid, signal.SIGTERM)
            deadline = time.monotonic() + grace
            while time.monotonic() < deadline and process.poll() is None:
                time.sleep(0.02)
            if process.poll() is None:
                _signal_group(process.pid, signal.SIGKILL)
                try:
                    process.wait(timeout=grace)
                except subprocess.TimeoutExpired:  # pragma: no cover - 极端兜底
                    pass
        else:
            # 正常退出：清理同组残留（父进程退了、孙进程还在的情况）
            _signal_group(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=grace)
            except subprocess.TimeoutExpired:  # pragma: no cover
                _signal_group(process.pid, signal.SIGKILL)

    pump.join(timeout=grace)
    if timed_out:
        # 先写日志再关文件：否则 --log 里看不到超时原因（曾经就漏了）
        message = (
            f"\n[run-with-timeout] TIMEOUT after {timeout:.1f}s "
            f"(grace {grace:.1f}s) → kill process group {process.pid}\n"
        )
        for sink in sinks:
            try:
                sink.write(message.encode())
                sink.flush()
            except (BrokenPipeError, ValueError, OSError):
                continue
    if log_file is not None:
        log_file.close()
    try:
        process.stdout.close()
    except OSError:
        pass
    if timed_out:
        return EXIT_TIMEOUT
    code = process.returncode
    if code is None:  # pragma: no cover - wait() 正常返回后不会为 None
        return 0
    if code < 0:
        # 子进程被信号杀死：按 shell 约定返回 128+signal（SIGKILL -> 137），
        # 不能把 -9 直接交给 sys.exit（shell 会看到 247）。
        return 128 - code
    return int(code)


def main(argv: Sequence[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    # 先按第一个 "--" 切成「自身参数」与「被执行的命令」，
    # 这样 `run-with-timeout.py 120 --grace 5 -- pytest -q` 里的 --grace 不会被
    # argparse.REMAINDER 吞进命令里（曾经的 bug：把 '--grace' 当命令去 exec）。
    if "--" in raw:
        split = raw.index("--")
        own_args, command = raw[:split], raw[split + 1:]
    else:
        own_args, command = raw, []

    parser = _UsageParser(
        description="Run a command with a hard timeout (exit 124 on timeout).",
        usage="%(prog)s SECONDS [--grace SEC] [--log FILE] -- COMMAND [ARGS...]",
    )
    parser.add_argument("seconds", type=float, help="超时秒数（>0）")
    parser.add_argument("--grace", type=float, default=DEFAULT_GRACE_SECONDS,
                        help=f"SIGTERM 后等待秒数，超时再 SIGKILL（默认 {DEFAULT_GRACE_SECONDS:g}s）")
    parser.add_argument("--log", type=Path, default=None, help="把输出同时写入该文件")
    args = parser.parse_args(own_args)
    if not command:
        print("run-with-timeout: 缺少命令（用法：run-with-timeout.py SECONDS -- CMD ...）",
              file=sys.stderr)
        return EXIT_USAGE
    if args.seconds <= 0:
        print("run-with-timeout: 超时秒数必须 > 0", file=sys.stderr)
        return EXIT_USAGE
    return run_with_timeout(
        command, timeout=args.seconds, grace=args.grace, log_path=args.log
    )


if __name__ == "__main__":
    sys.exit(main())
