"""给 `/api/openclaw/tasks` 的 `life_logger_todos[]` 补「这句话是对哪个应用说的」。

背景（2026-11-06）：OpenClaw 汇报卡把 Life Logger 的语音待办直接上卡，只拿到 `title`。
于是「查询：检查 LiveLog 页面……」这类**对着编程工具说的话**也被当成生活待办。
活动层其实已经有「当时在对哪个应用说话」的判断（`app_context` + `items[].input_context`），
但那条链路只挂在 `/api/daily` 的活动投影上，任务读层拿不到。

本模块只做三件事，全部**纯读**：
1. 按 `task["source"]["event_id"]` 在 `jobs/*/files/*/result/events.json` 里找回源事件；
2. 用事件的发声时刻（`evidence.reported_at` ＞ `time.absolute`）去 `app_context` 的
   归档窗口里判「语音输入到 X / 当时前台是 X」；
3. 把结果压成 `{app_label, kind, confidence}` 挂到任务上。

红线：**取不到就 null，不猜**。没有窗口、时间不合法、id 对不上，一律 `input_context=None`，
绝不根据标题关键词推断「这是在跟 Codex 说话」。
所有缓存都按文件 (mtime_ns, size) 指纹失效，文件一变立刻重读。
"""
from __future__ import annotations

import glob
import hashlib
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

SHANGHAI = ZoneInfo("Asia/Shanghai")
#: 输出契约：只有这三个字段，缺的就是 None。
INPUT_CONTEXT_FIELDS: tuple[str, ...] = ("app_label", "kind", "confidence")

_EVENT_INDEX_CACHE: dict[str, tuple[str, dict[str, Mapping[str, Any]]]] = {}
_WINDOWS_CACHE: dict[str, tuple[str, list[dict[str, Any]]]] = {}
#: [2026-11-07] 每个 (day, 指纹) 的窗口索引；和 `_WINDOWS_CACHE` 同一份指纹失效。
#: 为什么需要：`context_at()` 每次都线性扫全部窗口（2026-11-06 是 27,275 个），
#: 一个任务列表有 N 条任务就扫 N 遍。索引把这一步降到 O(log W)。
_WINDOW_INDEX_CACHE: dict[str, tuple[str, Any]] = {}
_CACHE_LIMIT = 8


def _event_files(root: Path) -> list[Path]:
    pattern = str(root / "jobs" / "*" / "files" / "*" / "result" / "events.json")
    return sorted(Path(p) for p in glob.glob(pattern))


def _fingerprint(paths: Iterable[Path]) -> str:
    digest = hashlib.sha256()
    for path in paths:
        try:
            stat = path.stat()
        except OSError:
            digest.update(f"{path}|missing".encode("utf-8"))
            continue
        digest.update(f"{path}|{stat.st_mtime_ns}|{stat.st_size}".encode("utf-8"))
    return digest.hexdigest()


def event_index(root: Path | str) -> dict[str, Mapping[str, Any]]:
    """`event_id → 事件对象` 索引（只读 `result/events.json`，按指纹缓存）。

    为什么不用 `event_view.build_event_view()`：那个视图会跳过 `decision-variants/`，
    并且做了规范化；而任务的 `source.event_id` 记的是**当时选中的那一支**的原始 id，
    必须按原样对得上。这里只读 33 个文件（实测 0.01s），不做任何语义加工。
    """
    base = Path(root)
    files = _event_files(base)
    key = str(base)
    fingerprint = _fingerprint(files)
    cached = _EVENT_INDEX_CACHE.get(key)
    if cached is not None and cached[0] == fingerprint:
        return cached[1]

    index: dict[str, Mapping[str, Any]] = {}
    for path in files:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        events = payload.get("events") if isinstance(payload, Mapping) else payload
        if not isinstance(events, list):
            continue
        for event in events:
            if not isinstance(event, Mapping):
                continue
            event_id = str(event.get("event_id") or "").strip()
            if event_id and event_id not in index:
                index[event_id] = event
    if len(_EVENT_INDEX_CACHE) >= _CACHE_LIMIT:
        _EVENT_INDEX_CACHE.clear()
    _EVENT_INDEX_CACHE[key] = (fingerprint, index)
    return index


def _windows_and_index(root: Path, day: str) -> tuple[list[dict[str, Any]], Any]:
    """一天的窗口 + 它们的 `WindowIndex`；按 (day jsonl + latest.json) 指纹一起失效。

    返回的 `index` 只对返回的 `windows` 这份列表有效（`WindowIndex.matches()` 会校验），
    两个缓存用**同一个指纹**，所以「文件一变更 → 窗口和索引一起重建」，不会串用旧索引。
    """
    from life_logger import app_context

    day_file = root / "input-context" / "days" / f"{day}.jsonl"
    latest_file = root / "input-context" / "latest.json"
    fingerprint = _fingerprint([day_file, latest_file])
    cached = _WINDOWS_CACHE.get(day)
    if cached is not None and cached[0] == fingerprint:
        windows = cached[1]
    else:
        windows = app_context.load_windows(root=root, day=day)
        if len(_WINDOWS_CACHE) >= _CACHE_LIMIT:
            _WINDOWS_CACHE.clear()
        _WINDOWS_CACHE[day] = (fingerprint, windows)

    index_cached = _WINDOW_INDEX_CACHE.get(day)
    if index_cached is not None and index_cached[0] == fingerprint:
        index = index_cached[1]
        # 索引必须绑定当前这份窗口列表；列表被换掉（缓存被清）就重建，绝不串用。
        if getattr(index, "matches", None) is not None and index.matches(windows):
            return windows, index
    index = app_context.build_window_index(windows)
    if len(_WINDOW_INDEX_CACHE) >= _CACHE_LIMIT:
        _WINDOW_INDEX_CACHE.clear()
    _WINDOW_INDEX_CACHE[day] = (fingerprint, index)
    return windows, index


def _windows(root: Path, day: str) -> list[dict[str, Any]]:
    """一天的输入上下文窗口；按 (day jsonl + latest.json) 指纹缓存。

    保留这个入口是因为既有测试/调用方只用它取窗口；索引见 `_windows_and_index()`。
    """
    return _windows_and_index(root, day)[0]


def _context_at(app_context: Any, moment: str, windows: list[dict[str, Any]], index: Any) -> Any:
    """调 `context_at`，**带索引**；对方不接受 `index`（老版本/测试替身）就退回原调用。

    为什么用 try/except TypeError 而不是写死签名：`app_context.context_at` 在测试里会被
    monkeypatch 成只接受 (moment, windows) 的假函数，写死 `index=` 会让那些测试直接崩。
    """
    if index is not None:
        try:
            return app_context.context_at(moment, windows, index=index)
        except TypeError:
            pass
    return app_context.context_at(moment, windows)


def _speech_moment(event: Mapping[str, Any]) -> str | None:
    evidence = event.get("evidence") if isinstance(event.get("evidence"), Mapping) else {}
    for value in (evidence.get("reported_at"), (event.get("time") or {}).get("absolute")
                  if isinstance(event.get("time"), Mapping) else None):
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _contract(app_label: Any, kind: Any, confidence: Any) -> dict[str, Any] | None:
    """只输出契约里的三个字段；`app_label` 为空就整体返回 None（没有对象等于没证据）。"""
    label = str(app_label or "").strip() or None
    if not label:
        return None
    return {
        "app_label": label,
        "kind": str(kind).strip() if kind else None,
        "confidence": str(confidence).strip() if confidence else None,
    }


def input_context_for_event(
    event: Mapping[str, Any] | None, *, root: Path | str
) -> dict[str, Any] | None:
    """源事件 → `{app_label, kind, confidence}`；判不出来返回 None（不猜）。"""
    if not isinstance(event, Mapping):
        return None

    # 1) 事件层已经算好并落盘的上下文（只在 status 已解析时才会写，见 activity_timeline_runtime）。
    #    它没有 kind/confidence，这两个字段保持 None，不补猜。
    evidence = event.get("evidence") if isinstance(event.get("evidence"), Mapping) else {}
    stored = evidence.get("input_context")
    if isinstance(stored, Mapping):
        resolved = _contract(stored.get("app_label"), stored.get("kind"), stored.get("confidence"))
        if resolved is not None:
            return resolved

    # 2) 用发声时刻去归档窗口里判（与 /api/daily 的活动投影同一套函数）。
    moment = _speech_moment(event)
    if not moment:
        return None
    try:
        at = datetime.fromisoformat(moment.replace("Z", "+00:00"))
    except ValueError:
        return None
    if at.tzinfo is None:
        return None
    day = at.astimezone(SHANGHAI).date().isoformat()

    from life_logger import app_context

    try:
        windows, index = _windows_and_index(Path(root) / "watch-sessions", day)
        context = _context_at(app_context, moment, windows, index)
    except Exception as error:  # noqa: BLE001 - 上下文读失败只能是 None，不能拖垮任务接口
        logger.warning("任务输入上下文判定失败（已忽略）: %s: %s", type(error).__name__, error)
        return None
    if not isinstance(context, Mapping):
        return None
    return _contract(context.get("app_label"), context.get("kind"), context.get("confidence"))


def attach_input_context(
    tasks: Iterable[Mapping[str, Any]], *, root: Path | str
) -> list[dict[str, Any]]:
    """给每个任务浅拷贝后加 `input_context`；**不改其它字段、不改顺序**。

    `event_id` 为空（OpenClaw 人工任务）或查不到源事件时，`input_context` 为 None。
    """
    index: dict[str, Mapping[str, Any]] | None = None
    out: list[dict[str, Any]] = []
    for task in tasks:
        if not isinstance(task, Mapping):
            out.append(task)  # 非 dict 原样透传，绝不改造上游形状
            continue
        row = dict(task)
        source = task.get("source") if isinstance(task.get("source"), Mapping) else {}
        event_id = str(source.get("event_id") or "").strip()
        context = None
        if event_id:
            if index is None:
                try:
                    index = event_index(root)
                except Exception as error:  # noqa: BLE001
                    logger.warning("事件索引构建失败（已忽略）: %s: %s", type(error).__name__, error)
                    index = {}
            context = input_context_for_event(index.get(event_id), root=root)
        row["input_context"] = context
        out.append(row)
    return out
