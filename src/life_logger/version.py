"""Life Logger 版本标识：让「现在跑的是哪一版」有一个唯一答案。

存在理由（2026-10-19 事故）：
服务进程连续跑了 40 小时，磁盘上 9 个后端文件早已更新，但没人能从界面上看出来
——于是「功能做了却没生效」被反复误判成「功能有 bug」，一轮轮往上叠。

这里定义三样东西，全部由**运行中的进程**自己报告，不依赖任何人手工填：

1. ``APP_VERSION``：产品语义版本（人工递增，见下）
2. ``CODE_FINGERPRINT``：后端全部 ``.py`` 的内容指纹（文件路径+大小+mtime_ns 排序后哈希）
   —— 与 ``SERVER_BUILD_ID``（只哈希 local_web.py）不同，它能覆盖 watch_formal_asr /
   contextual_extraction / expense_policy 等所有会影响行为的模块
3. ``UNLOADED_FILES``：比本进程启动时间更新的后端文件（即「改了但没重启」的那批）

版本号规则（semver 简化版，只用于人类沟通，不作兼容承诺）：
- ``0.MINOR.0``：每交付一个可感知功能
- ``0.MINOR.PATCH``：纯修复
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

#: 产品语义版本。改动请同时更新下面的 RELEASE_NOTE。
APP_VERSION = "0.10.0"
RELEASE_NOTE = "时间线与金额口径统一 + 日报金额对齐账本"

_PACKAGE_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _PACKAGE_DIR.parents[1]

#: 进程启动时间（导入即固定）。用于判定「磁盘上有比它更新的代码」＝未加载。
_PROCESS_STARTED_AT = time.time()


def _backend_files() -> list[Path]:
    """服务进程使用的后端源码（包模块与本地 Web 服务入口）。"""
    files = sorted(_PACKAGE_DIR.glob("*.py"))
    scripts = _PROJECT_ROOT / "scripts"
    entrypoint = scripts / "run_local_web.py"
    if entrypoint.is_file():
        files.append(entrypoint)
    return [path for path in files if path.is_file()]


def code_fingerprint() -> str:
    """后端代码内容指纹（12 位十六进制）。不读文件内容，用 大小+mtime_ns 做代理，
    避免每次调用都全量哈希几十万行；mtime 变化同样意味着「代码变了」。"""
    digest = hashlib.sha256()
    for path in _backend_files():
        try:
            stat = path.stat()
        except OSError:
            continue
        digest.update(f"{path.name}:{stat.st_size}:{stat.st_mtime_ns}\n".encode())
    return digest.hexdigest()[:12]


def git_commit() -> str:
    """当前 git 提交（短哈希）；非 git 环境返回空串，绝不抛异常。"""
    try:
        out = subprocess.run(
            ["git", "-C", str(_PROJECT_ROOT), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=3,
        )
        return out.stdout.strip() if out.returncode == 0 else ""
    except Exception:
        return ""


def _started_at_timestamp(process_started_at: str | float | datetime | None) -> float:
    if process_started_at is None:
        return _PROCESS_STARTED_AT
    if isinstance(process_started_at, (int, float)):
        return float(process_started_at)
    if isinstance(process_started_at, datetime):
        return process_started_at.timestamp()
    try:
        return datetime.fromisoformat(str(process_started_at).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return _PROCESS_STARTED_AT


def unloaded_files(process_started_at: str | float | datetime | None = None) -> list[str]:
    """比服务进程启动时间更新的后端文件名字（即改了但没重启的那批）。"""
    started_at = _started_at_timestamp(process_started_at)
    stale: list[str] = []
    for path in _backend_files():
        try:
            if path.stat().st_mtime > started_at:
                stale.append(path.name)
        except OSError:
            continue
    return stale


def deployed_at(process_started_at: str | float | datetime | None = None) -> str:
    """服务进程启动时间（本地时区 ISO）。"""
    # [2026-11-04 UTC+8] 以前是 `fromtimestamp(...).astimezone()`：`astimezone()` 无参
    # 会转成**系统时区**，机器在 UTC-8 时 /api/version 回的是 `...-08:00`，
    # 页面上比北京时间早一天。项目统一 UTC+8，这里直接带上固定偏移。
    return (
        datetime.fromtimestamp(
            _started_at_timestamp(process_started_at),
            tz=timezone(timedelta(hours=8)),
        ).isoformat(timespec="seconds")
    )


def build_info(
    *,
    process_started_at: str | float | datetime | None = None,
    include_unloaded: bool = True,
) -> dict[str, Any]:
    """给 API / 页面角标用的一份运行版本快照。"""
    stale = unloaded_files(process_started_at) if include_unloaded else []
    return {
        "app_version": APP_VERSION,
        "release_note": RELEASE_NOTE,
        "code_fingerprint": code_fingerprint(),
        "git_commit": git_commit(),
        "deployed_at": deployed_at(process_started_at),
        "pid": os.getpid(),
        "unloaded_files": stale,
        "deploy_state": "stale" if stale else "current",
    }
