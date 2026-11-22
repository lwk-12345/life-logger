"""服务启动时**预加载** life_logger 全部生产模块（2026-11-07）。

## 为什么需要

`local_web.py` / `processing_runtime.py` 里大量函数内延迟 import
（`from life_logger.food_nutrition import ...`）。磁盘上的代码改了、服务没重启时，
**新请求**会把**新版**模块读进**旧版**进程，与已加载的旧模块混跑 —— 已实测两次崩溃：

* 2026-11-06 18:10 `nutrition_for_item() got an unexpected keyword argument 'count'`
* 2026-11-07 `/api/daily?date=2026-11-06` `AttributeError: 'FoodNutrition' object has no attribute 'estimate_basis'`

启动时把所有生产模块导入一遍，函数内的延迟 import 就只会命中 `sys.modules` 里的
**已加载版本**；磁盘改动在重启前不再生效，"新旧混跑"从根上消失。

## 边界

* **只导入，不改行为**：不调用 `__main__` 块、不启动线程、不改配置。
* **失败不致命**：与现在一致 —— 记 warning 继续起服务；报告里有 `failed` 清单。
* **不预加载的只有两类**（见 `PRELOAD_EXCLUDE`）：CLI 入口、常驻子进程入口。
  它们都不在服务进程里被 import，预加载它们反而会诱导"在本进程里跑 serve()"。
* **不做版本校验/热更新**：磁盘比进程新 => 仍需上线（`scripts/check_deploy_state.py` 的语义不变）。
"""
from __future__ import annotations

import importlib
import logging
import pkgutil
import resource
import sys
import time
from typing import Any, Mapping

logger = logging.getLogger(__name__)

#: 预加载白名单之外的模块：**逐个给了理由**，不要随手加。
#: 判据 = 「服务进程会不会 import 它」。会 import 的一律必须预加载（否则就还有混跑窗口）。
PRELOAD_EXCLUDE: Mapping[str, str] = {
    "life_logger.cli": (
        "CLI 入口（`__main__` 里 argparse 分发）。服务进程从不 import 它"
        "（grep `life_logger.cli` 在 src/ scripts/ 里零引用），预加载没有收益。"
    ),
    "life_logger.asr_resident": (
        "常驻 ASR 子进程入口：`__main__` 里直接 `ResidentWorker().serve()`。"
        "服务是用 `python -m life_logger.asr_resident` 起**另一个进程**（asr_resident.py:170），"
        "在本进程 import 它毫无用处，却容易让人误以为可以在本进程里 serve()。"
    ),
}

#: 不预加载的模块名前缀（第三方/测试包，留作扩展口；当前为空）。
PRELOAD_EXCLUDE_PREFIXES: tuple[str, ...] = ("life_logger.tests",)

#: 已被证实在服务 venv 里**不需要专用解释器**的"重"模块 —— 记录在案，避免以后误加进排除表。
#: 它们只在**被调用时** spawn `~/.codex/.../.venv-funasr/bin/python` 子进程，
#: 模块顶层不 import funasr/torch/onnxruntime（2026-11-07 实测：全部 <0.15s 导入成功）。
_HEAVY_BUT_SAFE = (
    "life_logger.moss_transcribe",
    "life_logger.transcribe",
    "life_logger.speech_separation",
    "life_logger.campplus_voiceprint",
    "life_logger.audio_scene",
    "life_logger.audio_vad",
    "life_logger.vip_fast_track",
)


def rss_mb() -> float:
    """当前进程的 RSS 峰值（MB）。macOS 上 `ru_maxrss` 单位是字节，Linux 是 KB。"""
    value = float(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    return value / (1024 * 1024) if sys.platform == "darwin" else value / 1024


def iter_module_names(package: str = "life_logger") -> list[str]:
    """遍历包内**所有**子模块名（不递归子包，本包只有一层）。"""
    try:
        pkg = importlib.import_module(package)
    except Exception:  # noqa: BLE001 - 包本身都导不进来时交给调用方处理
        return []
    names: list[str] = []
    for info in pkgutil.iter_modules(getattr(pkg, "__path__", [])):
        if info.name.startswith("_"):
            continue
        names.append(f"{package}.{info.name}")
    return sorted(names)


def is_excluded(module: str, exclude: Mapping[str, str] | None = None) -> str | None:
    """返回排除理由（未排除则 None）。"""
    table = PRELOAD_EXCLUDE if exclude is None else exclude
    if module in table:
        return table[module]
    for prefix in PRELOAD_EXCLUDE_PREFIXES:
        if module == prefix or module.startswith(prefix + "."):
            return f"前缀排除：{prefix}"
    return None


def preload_modules(
    *,
    package: str = "life_logger",
    exclude: Mapping[str, str] | None = None,
    logger_: logging.Logger | None = None,
    quiet: bool = False,
) -> dict[str, Any]:
    """把包内生产模块全部 import 一遍。**永不抛异常**（除了解释器级错误）。

    返回 `{loaded, already, skipped, failed, duration_s, rss_before_mb, rss_after_mb, rss_delta_mb, modules}`。
    """
    log = logger_ or logger
    names = iter_module_names(package)
    rss_before = rss_mb()
    started = time.monotonic()
    loaded: list[str] = []
    already: list[str] = []
    skipped: list[tuple[str, str]] = []
    failed: list[tuple[str, str]] = []

    for full in names:
        if full == package or full.endswith(".__init__"):
            continue
        reason = is_excluded(full, exclude)
        if reason:
            skipped.append((full, reason))
            continue
        if full in sys.modules:
            already.append(full)
            continue
        try:
            importlib.import_module(full)
            loaded.append(full)
        except BaseException as error:  # noqa: BLE001 - 任何导入失败都不能挡住服务启动
            detail = f"{type(error).__name__}: {error}"
            failed.append((full, detail))
            log.warning("预加载失败（已忽略，功能用到时才会真正暴露）: %s: %s", full, detail)

    duration = time.monotonic() - started
    rss_after = rss_mb()
    report = {
        "package": package,
        "total": len(names),
        "loaded": len(loaded),
        "already": len(already),
        "skipped": len(skipped),
        "failed": len(failed),
        "duration_s": round(duration, 3),
        "rss_before_mb": round(rss_before, 1),
        "rss_after_mb": round(rss_after, 1),
        "rss_delta_mb": round(rss_after - rss_before, 1),
        "modules": loaded,
        "skipped_modules": [name for name, _reason in skipped],
        "failed_modules": [{"module": name, "error": detail} for name, detail in failed],
    }
    if not quiet:
        log.info(
            "预加载 %s：新导入 %d / 已在内存 %d / 跳过 %d / 失败 %d，耗时 %.3fs，RSS %.1f→%.1f MB (+%.1f)",
            package, report["loaded"], report["already"], report["skipped"],
            report["failed"], report["duration_s"], report["rss_before_mb"],
            report["rss_after_mb"], report["rss_delta_mb"],
        )
    return report


def format_startup_line(report: Mapping[str, Any]) -> str:
    """给启动日志用的一行摘要（stdout，带 flush 由调用方负责）。"""
    return (
        f"[preload] {report.get('package')} 模块 {report.get('total')} 个："
        f"新导入 {report.get('loaded')} · 已在内存 {report.get('already')} · "
        f"排除 {report.get('skipped')} · 失败 {report.get('failed')}｜"
        f"耗时 {report.get('duration_s')}s｜"
        f"RSS {report.get('rss_before_mb')}→{report.get('rss_after_mb')} MB "
        f"(+{report.get('rss_delta_mb')})"
    )
