"""统一原子写（最小实现）：唯一临时文件 + fsync(file) + os.replace + fsync(dir)。

设计约束（来自 docs/architecture-audit-2026-10-29.md §12/§13）：
  * 现有 10 份「原子写」实现，其中至少 2 处用**固定 .tmp 名**，两个写者同目录同名
    → 互相覆盖（watch-coverage-intervals.json、passport_stream）。
  * 本模块只做一件事：把「一份完整内容替换到目标路径」做到进程间安全：
      - 临时文件用 `mkstemp`（唯一名，同目录，保证同盘 rename 原子）
      - 写完 `flush` + `os.fsync(fd)`；`os.replace` 后再 `fsync(dir)`
      - 任何异常（序列化/写/fsync/replace）都**保留目标旧值**，并清理自己的临时文件
  * 只提供 opt-in 的 `file_lock()`（flock），默认路径不加锁：原子替换本身已保证
    "不会读到半个文件"；需要"读-改-写"串行化时再显式加锁。

不负责：缓存失效、schema 校验、业务契约（那些仍归各自模块）。
"""

from __future__ import annotations

import contextlib
import dataclasses
import errno
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Iterator

__all__ = [
    "AtomicWriteError", "AtomicWriteDurabilityError", "AtomicWriteResult",
    "write_bytes_atomic", "write_json_atomic", "file_lock",
]

#: 目录 fsync 在这些 errno 上视为「文件系统不支持」（不谎报为失败，也不谎报为已持久化）
_DIR_FSYNC_UNSUPPORTED = {errno.EINVAL, getattr(errno, "ENOTSUP", errno.EINVAL),
                          getattr(errno, "EOPNOTSUPP", errno.EINVAL)}

#: 平台判定单独成常量，便于测试注入（**不要**去改 `os.name`：pathlib 依赖它）
_PLATFORM = os.name


@dataclasses.dataclass(frozen=True)
class AtomicWriteResult:
    """一次原子写的结果：**明确区分**「替换前失败」与「已替换但持久化未知」。"""
    path: Path
    replaced: bool
    durability: str          # "synced" | "unknown" | "unsupported" | "skipped"
    phase: str               # 失败或跳过发生在哪一步
    warning: str | None = None

    def __fspath__(self) -> str:
        return str(self.path)


class AtomicWriteError(OSError):
    """替换**之前**失败：目标旧值保持原样。"""

    def __init__(self, phase: str, message: str, *, target: Path, cause: BaseException | None = None):
        super().__init__(message)
        self.phase = phase
        self.target = Path(target)
        self.replaced = False
        self.durability = "old_value_intact"
        self.cause = cause


class AtomicWriteDurabilityError(OSError):
    """`os.replace` **已经发生**，但目录 fsync 失败 → 新值可见，持久性未知。

    调用方**不得**再声称「旧文件仍在」。
    """

    def __init__(self, phase: str, message: str, *, target: Path,
                 cause: BaseException | None = None, durability: str = "unknown"):
        super().__init__(message)
        self.phase = phase
        self.target = Path(target)
        self.replaced = True
        self.durability = durability
        self.cause = cause


def _fsync_dir(directory: Path) -> tuple[str, str | None]:
    """目录 fsync。

    返回 (durability, warning)：
      * ("synced", None)                  —— 成功
      * ("skipped", "non_posix")          —— 平台不支持（Windows），无法断言持久化
      * ("unsupported", "EINVAL…")        —— 文件系统不支持目录 fsync（不谎报成功）
    其它错误（EIO/EACCES/ENOSPC…）**向上抛出** OSError，由调用方转成
    `AtomicWriteDurabilityError`（此时 replace 已发生，旧值已不在）。
    """
    if _PLATFORM != "posix":
        return "skipped", "non_posix"
    fd = os.open(str(directory), os.O_RDONLY)      # 失败直接抛（不吞）
    try:
        os.fsync(fd)                               # 失败直接抛（不吞）
    except OSError as exc:
        if exc.errno in _DIR_FSYNC_UNSUPPORTED:
            return "unsupported", f"dir_fsync_unsupported:{exc.errno}"
        raise
    finally:
        os.close(fd)
    return "synced", None


def write_bytes_atomic(
    path: Path | str,
    data: bytes,
    *,
    mode: int = 0o600,
    fsync: bool = True,
    on_durability_error: str = "raise",
) -> AtomicWriteResult:
    """把 `data` 原子写进 `path`。

    失败语义（**这是本轮返工的核心**）：
      * replace 之前失败 → `AtomicWriteError`，`replaced=False`，旧值**确实**仍在；
      * replace 之后（目录 fsync）失败 → `AtomicWriteDurabilityError`，
        `replaced=True`，`durability="unknown"` —— **不得声称旧文件还在**。

    `on_durability_error="report"` 时不抛，返回 `AtomicWriteResult(durability="unknown")`，
    由调用方决定如何告警。
    """
    if on_durability_error not in {"raise", "report"}:
        raise ValueError(f"on_durability_error must be 'raise' or 'report', got {on_durability_error!r}")
    target = Path(path)
    parent = target.parent
    tmp_path: Path | None = None
    replaced = False
    phase = "prepare"

    try:
        parent.mkdir(parents=True, exist_ok=True)
        phase = "mkstemp"
        fd, tmp_name = tempfile.mkstemp(dir=str(parent), prefix=f".{target.name}.", suffix=".tmp")
        tmp_path = Path(tmp_name)
        phase = "write"
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            if fsync:
                phase = "fsync_file"
                os.fsync(handle.fileno())
        phase = "chmod"
        os.chmod(tmp_path, mode)
        phase = "replace"
        os.replace(tmp_path, target)      # ← 越过这条线，旧值就不在了
        replaced = True

        # fsync=False：没有做任何落盘动作 → 绝不能宣称 synced（1 号 v2 的 F1）
        durability, warning = (("not_synced", "fsync_disabled") if not fsync else ("synced", None))
        if fsync:
            phase = "fsync_dir"
            durability, warning = _fsync_dir(parent)
        return AtomicWriteResult(path=target, replaced=True, durability=durability,
                                 phase=phase, warning=warning)
    except BaseException as exc:
        if tmp_path is not None and not replaced:
            with contextlib.suppress(OSError):
                tmp_path.unlink()
        if replaced:
            # 已替换：新值在盘上可见，但持久性未知
            err = AtomicWriteDurabilityError(
                "fsync_dir",
                f"replace succeeded but durability unknown at phase={phase}: {type(exc).__name__}: {exc}",
                target=target, cause=exc, durability="unknown",
            )
            if on_durability_error == "report":
                return AtomicWriteResult(path=target, replaced=True, durability="unknown",
                                        phase=phase, warning=str(err))
            raise err from exc
        raise AtomicWriteError(phase, f"{type(exc).__name__}: {exc}", target=target, cause=exc) from exc


def write_json_atomic(
    path: Path | str,
    payload: Any,
    *,
    ensure_ascii: bool = False,
    indent: int | None = None,
    fsync: bool = True,
    on_durability_error: str = "raise",
) -> AtomicWriteResult:
    """`json.dumps` 后原子写。序列化失败不会碰目标文件（不产生 tmp）。"""
    text = json.dumps(payload, ensure_ascii=ensure_ascii, indent=indent)
    return write_bytes_atomic(path, text.encode("utf-8"), fsync=fsync,
                              on_durability_error=on_durability_error)


@contextlib.contextmanager
def file_lock(path: Path | str) -> Iterator[None]:
    """opt-in 进程间互斥（flock，POSIX）。用于「读-改-写」需要串行的场景。

    单独使用时不会让写入变原子；它与 write_*_atomic 组合才提供 CAS 语义。
    """
    lock_path = Path(str(path) + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+b")
    try:
        try:
            import fcntl
        except ImportError:          # 非 POSIX：退化为进程内无锁（显式记录，不假装加锁）
            yield
            return
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


# ─────────────────────────────────────────────────────────────────────────────
# 跨进程追加（JSON Lines）：批量 ack 等“只追加不改写”的场景
# ─────────────────────────────────────────────────────────────────────────────
@dataclasses.dataclass(frozen=True)
class AppendResult:
    """一次追加的结果。语义与 AtomicWriteResult 对齐：

    * 追加前失败 → 抛 `AtomicWriteError`（本次没有写入任何字节）；
    * 追加已成功、目录 fsync 失败 → `durability="unknown"`（**数据已可见**，不得声称没写）。
    """
    path: Path
    appended_bytes: int
    durability: str          # synced | unknown | unsupported | skipped
    warning: str | None = None


def append_json_line(
    path: Path | str,
    payload: Any,
    *,
    ensure_ascii: bool = False,
    fsync: bool = True,
    on_durability_error: str = "raise",
    lock: bool = True,
) -> AppendResult:
    """**跨进程安全**地追加一行 JSON（一条完整记录）。

    为什么需要单独的原语：
      * 追加不能走 `os.replace`（会丢历史），但 `open("a") + write` 在多进程下
        没有互斥，长行可能交错成“半行”，读侧只能跳过 → 静默丢记录；
      * 这里用 `file_lock()`（flock）做跨进程互斥 + **单次 `os.write`** 写完整行 + fsync。

    格式与旧实现完全一致（每行一个 JSON 对象），旧文件可继续读。
    """
    if on_durability_error not in {"raise", "report"}:
        raise ValueError(f"on_durability_error must be 'raise' or 'report', got {on_durability_error!r}")
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    line = (json.dumps(payload, ensure_ascii=ensure_ascii) + "\n").encode("utf-8")

    guard = file_lock(target) if lock else contextlib.nullcontext()
    try:
        with guard:
            fd = os.open(str(target), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                written = os.write(fd, line)      # 单次写入整行，避免 Python 层分片
                if written != len(line):
                    raise OSError(errno.EIO, f"short write: {written}/{len(line)}")
                if fsync:
                    os.fsync(fd)
            finally:
                os.close(fd)
    except BaseException as exc:
        raise AtomicWriteError("append", f"{type(exc).__name__}: {exc}",
                               target=target, cause=exc) from exc

    durability, warning = (("synced", None) if fsync else ("not_synced", "fsync_disabled"))
    if fsync:
        try:
            durability, warning = _fsync_dir(target.parent)
        except OSError as exc:
            err = AtomicWriteDurabilityError(
                "fsync_dir",
                f"append succeeded but durability unknown: {type(exc).__name__}: {exc}",
                target=target, cause=exc, durability="unknown",
            )
            if on_durability_error == "report":
                return AppendResult(path=target, appended_bytes=len(line),
                                    durability="unknown", warning=str(err))
            raise err from exc
    return AppendResult(path=target, appended_bytes=len(line), durability=durability, warning=warning)


def sync_directory(path: Path | str) -> tuple[str, str | None]:
    """公开的目录 fsync 封装：返回 (durability, warning)，硬错误向上抛。

    供「自己管理临时文件 + os.replace」的调用方补齐最后一步持久化用
    （例如上传落地：临时文件已 fsync，replace 之后还要 fsync 目录）。
    """
    return _fsync_dir(Path(path))


# ─────────────────────────────────────────────────────────────────────────────
# 孤立临时文件清理（SIGKILL 残留）
# ─────────────────────────────────────────────────────────────────────────────
_TMP_PATTERNS: tuple[str, ...] = (".*.tmp", "*.tmp", "*.partial", ".*.bak.tmp")


def cleanup_stale_tmp(
    directories: Iterable[Path | str],
    *,
    max_age_seconds: float = 1800.0,
    dry_run: bool = False,
) -> dict[str, Any]:
    """删除 `directories` 下**超龄**的孤立临时文件，返回统计。

    安全规则（宁可不删，不可误删）：
      * 只匹配临时命名（`.*.tmp` / `*.tmp` / `*.partial` / `.*.bak.tmp`），**绝不删 `.bak`**；
      * 只删 `mtime` 早于 `max_age_seconds` 的文件——正在写的临时文件 mtime 会被持续更新；
      * 目标不是普通文件（符号链接/目录）→ 跳过；
      * 同名的 `.lock` 若存在且正被持有（flock 拿不到），说明可能仍在写 → 跳过；
      * 任何 OSError 都只记录，不抛出。

    年龄默认 30 分钟：普通上传/缓存写都在秒级完成；SIGKILL 后的残留 30 分钟后必然已无写者，
    同时足够长，避免误删慢速大文件上传（例如 100MB 移动网络上行 > 10 分钟）。
    """
    import time as _time

    now = _time.time()
    removed: list[str] = []
    skipped: list[str] = []
    errors: list[str] = []
    for directory in directories:
        base = Path(directory)
        if not base.is_dir():
            continue
        for pattern in _TMP_PATTERNS:
            for path in base.glob(pattern):
                try:
                    stat = path.lstat()
                except OSError as exc:
                    errors.append(f"{path}: {exc}")
                    continue
                if not path.is_file() or path.is_symlink():
                    skipped.append(str(path))
                    continue
                if now - stat.st_mtime < max_age_seconds:
                    skipped.append(str(path))
                    continue
                lock_path = path.with_name(path.name + ".lock")
                if lock_path.exists():
                    try:
                        import fcntl

                        with lock_path.open("rb") as handle:
                            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                    except (OSError, ImportError):
                        skipped.append(str(path))   # 可能仍在写
                        continue
                if dry_run:
                    removed.append(str(path))
                    continue
                try:
                    path.unlink()
                    removed.append(str(path))
                except OSError as exc:
                    errors.append(f"{path}: {exc}")
    return {"removed": removed, "skipped": skipped, "errors": errors,
            "removed_count": len(removed), "max_age_seconds": max_age_seconds}
