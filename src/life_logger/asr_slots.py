"""跨进程的 ASR 槽位闸门。

## 为什么需要它

这套系统里有好几条路会启动 ASR / 声纹推理：

* 网页服务处理任务（`JobStore.start_job`）
* 任务恢复循环（`resume_interrupted_jobs`）
* 手工/辅助脚本（手表长录音转写、模板标定等）

每一份 ASR 推理实测常驻约 4.6 GB、~2 个核。此前**没有任何统一上限**，
于是发生过两次事故：

1. 一次放行 11 个任务 → 内存压缩段打满、内核 watchdog 超时 → **机器 kernel panic 重启**
2. 手工脚本与网页任务叠加 → 进程被 jetsam 杀掉，且不留 traceback

本模块把上限收口到一个**基于文件的槽位表**，因此对**跨进程**也有效：
网页服务和任何外部脚本共用同一个上限，谁都不能偷偷多开。

## 用法

    from life_logger.asr_slots import acquire_asr_slot, release_asr_slot

    token = acquire_asr_slot("watch-transcribe", limit=2)
    if token is None:
        raise SystemExit("拿不到槽位，稍后重试")
    try:
        ...跑推理...
    finally:
        release_asr_slot(token)

`acquire_asr_slot` 在拿不到槽位时不会无限等：超过 `wait_seconds` 就返回 None
（调用方自行决定重试策略），避免把调用方线程永久挂住。
"""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]

#: 默认上限：一次最多几个 ASR 推理进程。
DEFAULT_LIMIT = 2

#: 槽位表位置。放在 local-web 目录下，和其余运行态文件在一起。
DEFAULT_STATE_PATH = PROJECT_ROOT / "output" / "local-web" / "asr-slots.json"

#: 单个槽位的租约时长。持有者崩溃（被 kill -9 / jetsam）时不会留下永久占位：
#: 超过这个时长且 PID 已不存在的槽位会被回收。
SLOT_LEASE_SECONDS = 6 * 3600

# Once a tracked owner and child are both observed dead, leave a short window
# for process-table/lock state to settle before making the slot available.
DEAD_SLOT_GRACE_SECONDS = 30

#: Fun-ASR 进程在 MPS/CPU 上实测常驻约 4.6 GB。资源闸门至少要求这份余量，
#: 可通过环境变量 LIFE_LOGGER_ASR_MEMORY_GB 调整（默认 4.6 GB）。
ASR_MEMORY_GB = float(os.environ.get("LIFE_LOGGER_ASR_MEMORY_GB", "4.6"))


def _process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def available_memory_gb() -> float:
    """可回收内存（空闲 + 非活跃）估算，单位 GB。

    取不到时返回一个很大的数 —— 宁可放行也不要因为读不到指标就永久卡住。
    """
    try:
        import subprocess

        out = subprocess.run(
            ["/usr/bin/vm_stat"], capture_output=True, text=True, timeout=5
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return 999.0
    free = inactive = speculative = purgeable = file_backed = 0
    for line in out.splitlines():
        if "Pages free" in line:
            free = int(line.split(":")[1].strip().rstrip("."))
        elif "Pages inactive" in line:
            inactive = int(line.split(":")[1].strip().rstrip("."))
        elif "Pages speculative" in line:
            speculative = int(line.split(":")[1].strip().rstrip("."))
        elif "Pages purgeable" in line:
            purgeable = int(line.split(":")[1].strip().rstrip("."))
        elif "File-backed pages" in line:
            file_backed = int(line.split(":")[1].strip().rstrip("."))
    # vm_stat 的页大小在 Apple Silicon 上是 16384；包含可立即回收的页缓存
    return (free + inactive + speculative + purgeable + file_backed) * 16384 / 1024**3


def _read_state(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"slots": []}
    if not isinstance(payload, dict) or not isinstance(payload.get("slots"), list):
        return {"slots": []}
    return payload


def _write_state(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # 唯一临时名 + fsync + replace（原实现固定 `<name>.tmp`，并发写会互相覆盖）
    from life_logger.atomic import write_json_atomic

    write_json_atomic(path, payload, indent=2)


def _live_slots(payload: dict[str, Any], now: float) -> list[dict[str, Any]]:
    """Keep live owners/children and conservatively leased legacy slots."""
    live: list[dict[str, Any]] = []
    for slot in payload.get("slots") or []:
        if not isinstance(slot, dict):
            continue
        try:
            pid = int(slot.get("pid") or 0)
        except (TypeError, ValueError, OverflowError):
            pid = 0
        started = float(slot.get("started_at") or 0)
        if _process_alive(pid):
            live.append(slot)
            continue

        # Old slot records do not track the ASR child. Preserve their original
        # six-hour lease rather than guessing whether untracked work is alive.
        try:
            child_pid = int(slot.get("child_pid") or 0)
        except (TypeError, ValueError, OverflowError):
            child_pid = 0
        if child_pid <= 0:
            if now - started < SLOT_LEASE_SECONDS:
                live.append(slot)
            continue
        if _process_alive(child_pid):
            slot.setdefault("owner_dead_at", now)
            live.append(slot)
            continue

        dead_since = slot.get("child_dead_at")
        try:
            dead_since = float(dead_since)
        except (TypeError, ValueError, OverflowError):
            dead_since = 0
        if dead_since <= 0:
            slot["child_dead_at"] = now
            live.append(slot)
        elif now - dead_since < DEAD_SLOT_GRACE_SECONDS:
            live.append(slot)
    return live


def active_slot_count(state_path: Path | None = None) -> int:
    path = state_path or DEFAULT_STATE_PATH
    with _locked(path):
        payload = _read_state(path)
        live = _live_slots(payload, time.time())
        _write_state(path, {"slots": live})
        return len(live)


class _FileLock:
    """极简的跨进程互斥：用 O_CREAT|O_EXCL 抢锁文件。

    只保护槽位表的读改写，持有时间极短（毫秒级），不需要复杂实现。
    """

    def __init__(self, path: Path, timeout: float = 5.0) -> None:
        self.path = path
        self.timeout = timeout

    def __enter__(self) -> "_FileLock":
        deadline = time.time() + self.timeout
        while True:
            try:
                fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode())
                os.close(fd)
                return self
            except FileExistsError:
                # 陈旧锁（持有者已死）：清掉重试
                try:
                    owner = int(self.path.read_text() or "0")
                except (OSError, ValueError):
                    owner = 0
                if owner and not _process_alive(owner):
                    self.path.unlink(missing_ok=True)
                    continue
                if time.time() > deadline:
                    raise TimeoutError(f"抢锁超时: {self.path}")
                time.sleep(0.05)

    def __exit__(self, *exc: object) -> None:
        self.path.unlink(missing_ok=True)


def _locked(path: Path) -> _FileLock:
    return _FileLock(path.with_suffix(path.suffix + ".lock"))


def acquire_asr_slot(
    owner: str,
    *,
    limit: int | None = None,
    state_path: Path | None = None,
    wait_seconds: float = 0.0,
    require_memory_gb: float | None = None,
) -> dict[str, Any] | None:
    """尝试占一个 ASR 槽位。

    返回槽位 dict（需原样交给 `release_asr_slot`），拿不到返回 None。

    * `limit` 缺省读 `LIFE_LOGGER_JOB_CONCURRENCY`，再缺省 `DEFAULT_LIMIT`。
    * `require_memory_gb` 缺省按 `ASR_MEMORY_GB`；内存不足时直接拒绝，
      而不是放行后被系统杀掉。
    * `wait_seconds > 0` 时会在上限内轮询等待。
    """
    path = state_path or DEFAULT_STATE_PATH
    if limit is None:
        try:
            limit = int(os.environ.get("LIFE_LOGGER_JOB_CONCURRENCY", DEFAULT_LIMIT))
        except ValueError:
            limit = DEFAULT_LIMIT
    limit = max(1, limit)
    need = ASR_MEMORY_GB if require_memory_gb is None else require_memory_gb

    deadline = time.time() + max(0.0, wait_seconds)
    while True:
        now = time.time()
        with _locked(path):
            payload = _read_state(path)
            live = _live_slots(payload, now)
            _write_state(path, {"slots": live})
            free = available_memory_gb()
            if len(live) < limit and free >= need:
                token = {
                    "token": uuid.uuid4().hex,
                    "owner": owner,
                    "pid": os.getpid(),
                    "started_at": now,
                    "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                }
                live.append(token)
                _write_state(path, {"slots": live})
                return token
        if now >= deadline:
            return None
        time.sleep(min(2.0, max(0.2, deadline - now)))


def release_asr_slot(
    token: dict[str, Any] | None, *, state_path: Path | None = None
) -> None:
    """释放槽位。传 None 或已被回收的 token 都是安全的空操作。"""
    if not token:
        return
    path = state_path or DEFAULT_STATE_PATH
    wanted = str(token.get("token") or "")
    if not wanted:
        return
    with _locked(path):
        payload = _read_state(path)
        remaining = [
            slot
            for slot in (payload.get("slots") or [])
            if isinstance(slot, dict) and str(slot.get("token")) != wanted
        ]
        _write_state(path, {"slots": remaining})


def update_asr_slot_child_pid(
    token: dict[str, Any] | None,
    child_pid: int,
    *,
    state_path: Path | None = None,
) -> bool:
    """Record the spawned ASR child under the same cross-process file lock."""
    if not token:
        return False
    wanted = str(token.get("token") or "")
    if not wanted:
        return False
    try:
        pid = int(child_pid)
    except (TypeError, ValueError, OverflowError):
        return False
    if pid <= 0:
        return False
    path = state_path or DEFAULT_STATE_PATH
    with _locked(path):
        payload = _read_state(path)
        found = False
        for slot in payload.get("slots") or []:
            if isinstance(slot, dict) and str(slot.get("token") or "") == wanted:
                slot["child_pid"] = pid
                slot.pop("child_dead_at", None)
                found = True
                break
        if found:
            _write_state(path, payload)
        return found
