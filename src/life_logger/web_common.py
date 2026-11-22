"""从 local_web.py 机械搬出的 web_common 部分，行为不变；分组依据见 work/split-prep-localweb-20261104/。"""
from __future__ import annotations

from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any
from typing import Mapping
import logging
import os
import re
import secrets
import shutil


logger = logging.getLogger(__name__)


PROJECT_ROOT = Path(__file__).resolve().parents[2]


SUPPORTED_ASR_MODELS = {"mlx-whisper-turbo", "moss"}  # 【2026-10-15】移除 doubao-asr：用户明确「豆包可以删除」，实际已在用 moss


SUPPORTED_SPEAKER_MODELS = {"cam++"}


_SEARCH_CATEGORY_ALIASES = {
    "diet": "food",
    "work": "work_communication",
    "communication": "work_communication",
}


DEFAULT_PREFERENCES = {
    "onboarding_completed": False,
    "asr_model": "mlx-whisper-turbo",
    "speaker_model": "cam++",
    "lan_access_enabled": False,
    "trusted_network_bind": "",
    "mobile_capture_enabled": True,
    "mobile_capture_auto_start": False,
}


_READ_CONTRACT_SCHEMA = "event-read-contract-v1"


def _read_contract(
    scope: str,
    view: str,
    *,
    canonical: bool = True,
    date_basis: str = "Asia/Shanghai occurrence date",
    notice: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": _READ_CONTRACT_SCHEMA,
        "timezone": "Asia/Shanghai",
        "scope": scope,
        "view": view,
        "canonical_event_view": canonical,
        "date_basis": date_basis,
    }
    if notice:
        payload["notice"] = notice
    return payload


def _timestamp_key(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


#: 搜索用的「事件全集」与「来源映射」缓存。
#
# 为什么需要（2026-10-19 A0 派单 P-9）：
# `/api/events/search` 每次请求都会 `build_event_view(2000-01-01..2099-12-31)`
# 全量扫描（实测 0.3~0.7s），并且调 `JobStore.list()` 把 170+ 个 job 的 status.json
# 全读一遍（0.03~0.43s，**持有 JobStore 锁**）。当后台任务正在写产物时，
# 搜索会卡在这把锁上——这才是那次 92s 的真凶（不是搜索算法本身慢）。
#
# 现在：事件全集按「源数据指纹」缓存；来源映射改为**不取 JobStore 锁**、
# 用 status.json 的 stat 指纹做键缓存。文本过滤与账本计算仍然每次请求重算
# （它们总共约 0.03s，且必须反映当次 event_filter）。
_SEARCH_EVENTS_CACHE: dict[str, list[dict[str, Any]]] = {}


_SEARCH_SOURCE_MAP_CACHE: dict[str, dict[tuple[str, str], str]] = {}


_SEARCH_CACHE_MAX_ENTRIES = 4


#: 写前备份的体积上限：超过则跳过备份（避免把大文件复制两遍）
_BACKUP_MAX_BYTES = 8 * 1024 * 1024


def _backup_previous(path: Path) -> None:
    """写前备份：把上一版内容**原子**保存为 `<name>.bak`（只保留一份）。

    只在目标存在且体积合理时做；备份失败不影响主写入（但会留 warning），
    因为丢失备份不应阻断用户操作。
    """
    try:
        if not path.is_file():
            return
        size = path.stat().st_size
        if size > _BACKUP_MAX_BYTES:
            return
        backup = path.with_name(f"{path.name}.bak")
        temporary = path.with_name(f".{path.name}.{secrets.token_hex(4)}.bak.tmp")
        shutil.copy2(path, temporary)
        os.replace(temporary, backup)
    except OSError as error:
        logger.warning("备份失败（不影响本次写入）%s: %s", path, error)


def _atomic_json(path: Path, payload: Any) -> None:
    """跨进程安全的 JSON 落盘：写前备份 + 唯一临时文件 + fsync + os.replace + 目录 fsync。

    失败语义（与 `life_logger.atomic` 一致）：
      * replace 之前失败 → 抛 `AtomicWriteError`（`OSError` 子类），目标保持旧值；
      * replace 之后、目录 fsync 失败 → 持久性未知：**告警**但不再抛，
        因为此时盘上已是新值，抛错会让调用方误以为“没写成”。
    """
    from life_logger.atomic import write_json_atomic

    path.parent.mkdir(parents=True, exist_ok=True)
    _backup_previous(path)
    result = write_json_atomic(path, payload, indent=2, on_durability_error="report")
    if result.durability == "unknown":
        logger.warning("已替换但持久性未知（durability unknown）: %s", path)


# 阶段失败的性质：
#   fatal      → 该阶段的产物不可信/缺失，任务必须降级为 partial
#   degradable → 主流程仍能产出可用结果，只损失质量（可选决策增强、声纹、上下文增强）
# 没见过的阶段一律按致命处理 —— 宁可保守告警，也不要让未知失败悄悄通过。
FATAL_SEVERITY = "fatal"


DEGRADABLE_SEVERITY = "degradable"


STAGE_SEVERITY: dict[str, str] = {
    "asr": FATAL_SEVERITY,
    "attribution": FATAL_SEVERITY,
    "jev": DEGRADABLE_SEVERITY,
    "decision": DEGRADABLE_SEVERITY,
    "speaker": DEGRADABLE_SEVERITY,
    "voiceprint": DEGRADABLE_SEVERITY,
    "context": DEGRADABLE_SEVERITY,
    "cognitive": DEGRADABLE_SEVERITY,
}


# 阶段自称完成时必须落地的产物。用来拆穿「状态说完成、盘上没有东西」这种假成功；
# 只有在该阶段已经出现失败证据时才会去核对。用文件名递归匹配 —— 同样的产物在
# 两种布局里都存在（``asr/transcript.json`` 与 ``asr/<日期>/transcript.json``）。
STAGE_ARTIFACTS: dict[str, tuple[str, ...]] = {
    "asr": ("transcript.json",),
    "attribution": ("source-attribution.json", "run-summary.json"),
    "jev": ("jev-decisions.json", "decision-decisions.json"),
}


_TRACEBACK_MARKER = "Traceback (most recent call last)"


_ERROR_LINE_PATTERN = re.compile(r"^[A-Za-z_][\w.]*(?:Error|Exception)\b.*: ")


# 阶段脚本跑完会把自己的一份 JSON 摘要打到 stdout；它是「这次真的跑成功了」的
# 唯一可靠标记。
_STAGE_SUCCESS_PATTERN = re.compile(
    r'^\{.*"status":\s*"completed".*\}\s*$|^\{.*"decisions":\s*\d+.*\}\s*$'
)


# 只留尾部做证据核对：阶段日志会 append 多次运行，且可能很长。
_STAGE_LOG_TAIL_BYTES = 64 * 1024


_MAX_STAGE_ERROR_TEXT = 400


def stage_severity(stage: Any) -> str:
    """返回阶段的严重性：致命失败会让任务 partial，可降级失败只让它 degraded。"""
    return STAGE_SEVERITY.get(str(stage or "").strip(), FATAL_SEVERITY)


def _success_evidence_marker(text: str) -> int:
    """返回最后一次「阶段成功」标记的行号；没有则返回 -1。"""
    last = -1
    for index, line in enumerate(text.splitlines()):
        if _STAGE_SUCCESS_PATTERN.match(line.strip()):
            last = index
    return last


def _stage_log_text(job_dir: Path, item: Mapping[str, Any], stage: str) -> str | None:
    """读某个阶段的落盘日志尾部；读不到返回 None。"""
    file_id = str(item.get("sha256") or item.get("file_id") or "").strip()
    if not file_id or Path(file_id).name != file_id:
        return None
    log_path = Path(job_dir) / "files" / file_id / f"{stage}.log"
    try:
        if not log_path.is_file():
            return None
        with log_path.open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            size = stream.tell()
            stream.seek(max(0, size - _STAGE_LOG_TAIL_BYTES))
            return stream.read().decode("utf-8", errors="replace")
    except OSError:
        return None


def _missing_stage_artifact(
    job_dir: Path | None, item: Mapping[str, Any], stage: str
) -> str | None:
    """该阶段的产物一个都不在盘上时，返回第一个期待的文件名。"""
    if job_dir is None:
        return None
    names = STAGE_ARTIFACTS.get(stage)
    if not names:
        return None
    file_id = str(item.get("sha256") or item.get("file_id") or "").strip()
    if not file_id or Path(file_id).name != file_id:
        return None
    root = Path(job_dir) / "files" / file_id
    if not root.is_dir():
        return names[0]
    for name in names:
        if next(root.rglob(name), None) is not None:
            return None
    return names[0]


def _stage_error(
    *,
    item: Mapping[str, Any],
    stage: Any,
    kind: str,
    severity: str,
    error: str,
    resolved: bool,
) -> dict[str, Any]:
    text = str(error or "").strip().replace("\n", " ")
    if len(text) > _MAX_STAGE_ERROR_TEXT:
        text = text[: _MAX_STAGE_ERROR_TEXT - 1] + "…"
    return {
        "file_id": item.get("file_id") or item.get("sha256"),
        "name": item.get("original_name"),
        "stage": stage,
        "kind": kind,
        "severity": severity,
        "resolved": resolved,
        "error": text,
    }


#: 区间账本聚合缓存目录（本地文件，随源数据变化失效）。
_EXPENSE_RANGE_CACHE_DIRNAME = "cache"
