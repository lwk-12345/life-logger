"""「当时在对哪个应用说话」——读时关联（不写回活动文件）。

背景（2026-11-05 核实）：
* `input-context/days/<day>.jsonl` 归档的 mic 窗口里，今天 306 条有 201 条 `target_unknown`
  （采样点落在 0.003–1.2s 的 mic 窗口内几乎不可能）；
* `front_windows`（177 段，真的有 `target_app`）只存在 `latest.json`，30 分钟就被裁掉，
  从不进按日归档 —— 手表转写晚到就永远对不上。

本模块只做三件事，全部是**纯读**：
1. 从归档 + latest 里取窗口，按时间点判定「语音输入到 X」还是「当时前台是 X」；
2. 把结果挂到 `items[].input_context`，活动级取出现最多的应用；
3. 提供归档保留策略（7 天 + 单文件上限）的纯函数。

文案红线：只写「语音输入到 X」「当时前台：X」。**绝不**写「已发送给 X」「对方是 X」——
前者我们根本没有证据，后者会把人误当联系人。
"""
from __future__ import annotations

import bisect
import json
import logging
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

logger = logging.getLogger(__name__)

SHANGHAI = timezone(timedelta(hours=8))
#: 空闲超过这个秒数就不算「人对着应用说话」
DEFAULT_MAX_IDLE_SECONDS = 120.0
#: ns 窗口兜底：mic 窗口两侧多远内的前台采样还能当作「当时的前台」
DEFAULT_NEAREST_GRACE_SECONDS = 10.0
#: 归档保留天数与单文件上限（超出就停止追加，绝不删已有内容）
DEFAULT_KEEP_DAYS = 7
DEFAULT_MAX_DAY_FILE_BYTES = 12_000_000


def _parse(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return None          # naive 不猜时区
    return parsed


def _label(bundle: str | None, app_label: str | None = None) -> str | None:
    if app_label:
        return str(app_label)
    from life_logger.input_context import bundle_to_app_label

    return bundle_to_app_label(bundle)


def _window_span(window: Mapping[str, Any]) -> tuple[datetime, datetime] | None:
    start = _parse(window.get("started_at"))
    end = _parse(window.get("ended_at"))
    if start is None:
        return None
    return start, (end or start)


def _is_mic(window: Mapping[str, Any]) -> bool:
    provider = window.get("provider") or {}
    if isinstance(provider, Mapping) and str(provider.get("source") or "") == "mic_event":
        return True
    return bool(window.get("front_bundles") is not None and window.get("provider_pid"))


def _idle_seconds(window: Mapping[str, Any]) -> float | None:
    for key in ("idle_seconds", "hid_idle_seconds"):
        raw = window.get(key)
        if raw is None:
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if value >= 0:
            return value
    return None


def load_windows(*, root: Path | str, day: str, include_latest: bool = True) -> list[dict[str, Any]]:
    """读一天可用的窗口：按日归档 + latest.json 的 mic/front 两段（去重）。"""
    from life_logger.input_context import read_archived_day, window_archive_key

    base = Path(root)
    windows: list[dict[str, Any]] = []
    seen: set[str] = set()

    for item in read_archived_day(base, day):
        key = window_archive_key(item) or ""
        if key and key in seen:
            continue
        seen.add(key)
        windows.append(dict(item))

    if include_latest:
        latest = base / "input-context" / "latest.json"
        try:
            payload = json.loads(latest.read_text(encoding="utf-8")) if latest.is_file() else {}
        except (OSError, ValueError):
            payload = {}
        for key_name in ("windows", "front_windows"):
            for item in payload.get(key_name) or []:
                if not isinstance(item, Mapping):
                    continue
                span = _window_span(item)
                if span is None:
                    continue
                if span[0].astimezone(SHANGHAI).strftime("%Y-%m-%d") != day and span[1].astimezone(SHANGHAI).strftime("%Y-%m-%d") != day:
                    continue
                key = window_archive_key(item) or ""
                if key and key in seen:
                    continue
                if key:
                    seen.add(key)
                windows.append(dict(item))
    return windows


class WindowIndex:
    """把窗口的起止时间**预解析一次**并按起始分钟分桶（只读、纯函数产出的快照）。

    为什么需要：`attach_to_payload` 里每个 item 都要问一次「这个时刻覆盖在哪个窗口」，
    旧实现每次都把当天全部窗口线性扫一遍并重新 `datetime.fromisoformat()`；
    2026-11-06 实测 1,131 个 item × 27,275 个窗口 = 3,080 万次重复解析，占 `read_day` 的 99%。

    语义保证：`hits()` 返回的命中窗口**严格按原 `windows` 下标顺序**，
    与线性扫描的追加顺序一致（`_nearest_front` 的 `gap < best` 先到先得依赖这一点）。
    """

    __slots__ = ("_windows", "spans", "by_minute", "_minutes", "max_duration", "complete")

    def __init__(self, windows: Sequence[Mapping[str, Any]]) -> None:
        self._windows = windows
        spans: list[tuple[int, float, float, bool]] = []
        by_minute: dict[int, list[tuple[int, float, float]]] = {}
        max_duration = 0.0
        for idx, window in enumerate(windows):
            if not isinstance(window, Mapping):
                continue
            span = _window_span(window)
            if span is None:
                continue
            start_s = span[0].timestamp()
            end_s = span[1].timestamp()
            spans.append((idx, start_s, end_s, _is_mic(window)))
            by_minute.setdefault(int(start_s // 60), []).append((idx, start_s, end_s))
            max_duration = max(max_duration, end_s - start_s)
        self.spans = tuple(spans)
        self.by_minute = by_minute
        self._minutes = sorted(by_minute)
        self.max_duration = max_duration
        #: True = 每个窗口都是合法 Mapping 且时间戳可解析（`_nearest_front` 走索引的前提）
        self.complete = len(spans) == len(windows)

    def matches(self, windows: Sequence[Mapping[str, Any]]) -> bool:
        """索引只对**同一份**窗口列表有效（防止串用别的 day 的索引）。"""
        return self._windows is windows

    def hits(self, at: datetime) -> tuple[list[Mapping[str, Any]], list[Mapping[str, Any]]]:
        """覆盖该时刻的窗口：返回 (mic_hits, front_hits)，顺序 = 原 windows 下标顺序。"""
        at_s = at.timestamp()
        lo = bisect.bisect_left(self._minutes, int((at_s - self.max_duration) // 60))
        hi = bisect.bisect_right(self._minutes, int(at_s // 60))
        matched: list[int] = []
        for bucket in self._minutes[lo:hi]:
            for idx, start_s, end_s in self.by_minute[bucket]:
                if start_s <= at_s <= end_s:
                    matched.append(idx)
        if len(matched) > 1:
            matched.sort()
        windows = self._windows
        mic_hits: list[Mapping[str, Any]] = []
        front_hits: list[Mapping[str, Any]] = []
        for idx in matched:
            window = windows[idx]
            (mic_hits if _is_mic(window) else front_hits).append(window)
        return mic_hits, front_hits


def build_window_index(windows: Sequence[Mapping[str, Any]]) -> "WindowIndex":
    """预解析窗口时间戳并分桶。对同一份 ``windows`` 只建一次，反复传给 ``context_at``。"""
    return WindowIndex(windows)


def context_at(
    moment: Any,
    windows: Sequence[Mapping[str, Any]],
    *,
    max_idle_seconds: float = DEFAULT_MAX_IDLE_SECONDS,
    nearest_grace_seconds: float = DEFAULT_NEAREST_GRACE_SECONDS,
    index: WindowIndex | None = None,
) -> dict[str, Any] | None:
    """某个时刻的输入目标。返回 None = 没有可用证据（调用方不要编）。

    优先级：
      1. 命中 mic 窗口且该窗口有 target_app → kind=dictation（语音输入到 X）
      2. 否则取覆盖该时刻（或最近一次采样在 grace 内）的前台窗口 → kind=front_app
    两种情况都要求「人没在挂机」：已知空闲 > 120s 直接判无证据；
    空闲时长拿不到时仍给 front_app，但 confidence=low。

    ``index`` 可选：同一个 ``windows`` 要连续问很多时刻时，先 ``build_window_index(windows)``
    再传进来，避免每次都线性重扫（结果逐字段不变）。不传则走原来的线性路径。
    """
    at = _parse(moment) if isinstance(moment, str) else moment
    if at is None or getattr(at, "tzinfo", None) is None:
        return None
    at = at.astimezone(SHANGHAI)

    if index is not None and index.matches(windows):
        mic_hits, front_hits = index.hits(at)
    else:
        mic_hits = []
        front_hits = []
        for window in windows:
            if not isinstance(window, Mapping):
                continue
            span = _window_span(window)
            if span is None:
                continue
            start, end = span
            if not (start <= at <= end):
                continue
            (mic_hits if _is_mic(window) else front_hits).append(window)

    for window in mic_hits:
        bundle = window.get("target_app") or window.get("target_bundle")
        if window.get("target_unknown") is True or not bundle:
            continue
        if _idle_too_long(window, max_idle_seconds):
            continue
        return {
            "app_label": _label(str(bundle), window.get("app_label")),
            "target_bundle": str(bundle),
            "kind": "dictation",
            "confidence": "high" if window.get("complete") is not False else "medium",
            "meaning": f"语音输入到 {_label(str(bundle), window.get('app_label'))}",
            "evidence": "mic_window",
        }

    # mic 命中但没目标（no_front_samples）：用两侧最近的前台采样兜底
    if mic_hits:
        nearest = _nearest_front(mic_hits[0], windows, nearest_grace_seconds,
                                 index=index if (index is not None and index.matches(windows)) else None)
        if nearest is not None:
            front_hits = [nearest]

    for window in front_hits:
        bundle = window.get("target_app") or window.get("target_bundle")
        if not bundle or window.get("target_unknown") is True:
            continue
        if _idle_too_long(window, max_idle_seconds):
            continue
        idle = _idle_seconds(window)
        label = _label(str(bundle), window.get("app_label"))
        if idle is None:
            confidence, reason = "low", "idle_unknown"
        elif window.get("complete") is False:
            confidence, reason = "low", "incomplete_window"
        else:
            confidence, reason = "medium", "front_sample"
        return {
            "app_label": label,
            "target_bundle": str(bundle),
            "kind": "front_app",
            "confidence": confidence,
            "meaning": f"当时前台：{label}",
            "evidence": reason,
        }
    return None


def _idle_too_long(window: Mapping[str, Any], limit: float) -> bool:
    idle = _idle_seconds(window)
    return idle is not None and idle > limit


def _nearest_front(window: Mapping[str, Any], windows: Sequence[Mapping[str, Any]],
                   grace: float, *, index: "WindowIndex | None" = None) -> Mapping[str, Any] | None:
    span = _window_span(window)
    if span is None:
        return None
    target = span[0]
    best: tuple[float, Mapping[str, Any]] | None = None
    # 走索引的前提：同一份 windows，且索引里**没有任何元素被跳过**（否则退回线性路径，
    # 保证「窗口列表里混了非 Mapping 元素」时行为与旧实现一致，包括异常类型）。
    if index is not None and index.matches(windows) and index.complete:
        target_s = target.timestamp()
        for idx, start_s, end_s, is_mic in index.spans:
            if is_mic:
                continue
            candidate = windows[idx]
            if not (candidate.get("target_app") or candidate.get("target_bundle")):
                continue
            gap = min(abs(start_s - target_s), abs(target_s - end_s))
            if gap <= grace and (best is None or gap < best[0]):
                best = (gap, candidate)
        return best[1] if best else None
    for candidate in windows:
        if _is_mic(candidate):
            continue
        cspan = _window_span(candidate)
        if cspan is None:
            continue
        if not (candidate.get("target_app") or candidate.get("target_bundle")):
            continue
        gap = min(abs((cspan[0] - target).total_seconds()), abs((target - cspan[1]).total_seconds()))
        if gap <= grace and (best is None or gap < best[0]):
            best = (gap, candidate)
    return best[1] if best else None


def context_for_item(item: Mapping[str, Any], windows: Sequence[Mapping[str, Any]], **kwargs: Any) -> dict[str, Any] | None:
    """活动 item 的时刻：优先 spoken_at（讲述时间），其次 reported_at / occurrence_start。"""
    for key in ("spoken_at", "reported_at", "occurrence_start", "occurred_at"):
        value = item.get(key)
        if value:
            ctx = context_at(value, windows, **kwargs)
            if ctx is not None:
                return ctx
    return None


_ORIGINAL_LOAD_WINDOWS = load_windows
_ARCHIVED_WINDOWS_CACHE: dict[tuple[str, int, int], tuple[list[dict[str, Any]], set[str], "WindowIndex | None"]] = {}
_COMBINED_WINDOWS_CACHE: dict[tuple[str, str, tuple[int, int], tuple[int, int]], tuple[list[dict[str, Any]], "WindowIndex | None"]] = {}
_WINDOWS_CACHE_MAX = 32


def _file_sig(path: Path) -> tuple[int, int]:
    try:
        st = path.stat()
        return (int(st.st_mtime_ns), int(st.st_size))
    except OSError:
        return (-1, -1)


def _load_windows_with_index(*, root: Path | str, day: str) -> tuple[list[dict[str, Any]], "WindowIndex | None"]:
    """带 (mtime_ns, size) 签名缓存的 load_windows + build_window_index。"""
    if load_windows is not _ORIGINAL_LOAD_WINDOWS:
        windows = load_windows(root=root, day=day)
        return (windows, build_window_index(windows) if windows else None)
    from life_logger.input_context import read_archived_day, window_archive_key

    base = Path(root)
    day_file = base / "input-context" / "days" / f"{day}.jsonl"
    latest_file = base / "input-context" / "latest.json"
    day_sig = _file_sig(day_file)
    latest_sig = _file_sig(latest_file)
    combined_key = (str(base), str(day), day_sig, latest_sig)
    hit = _COMBINED_WINDOWS_CACHE.get(combined_key)
    if hit is not None:
        return hit

    arch_key = (str(day_file), day_sig[0], day_sig[1])
    arch_hit = _ARCHIVED_WINDOWS_CACHE.get(arch_key)
    if arch_hit is None:
        arch_windows: list[dict[str, Any]] = []
        arch_seen: set[str] = set()
        for item in read_archived_day(base, day):
            key = window_archive_key(item) or ""
            if key and key in arch_seen:
                continue
            arch_seen.add(key)
            arch_windows.append(dict(item))
        arch_index = build_window_index(arch_windows) if arch_windows else None
        if len(_ARCHIVED_WINDOWS_CACHE) >= _WINDOWS_CACHE_MAX:
            _ARCHIVED_WINDOWS_CACHE.pop(next(iter(_ARCHIVED_WINDOWS_CACHE)))
        arch_hit = (arch_windows, arch_seen, arch_index)
        if day_sig[0] >= 0:
            _ARCHIVED_WINDOWS_CACHE[arch_key] = arch_hit

    arch_windows, arch_seen, arch_index = arch_hit
    extra_windows: list[dict[str, Any]] = []
    if latest_sig[0] >= 0:
        try:
            payload = json.loads(latest_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            payload = {}
        seen_local = set(arch_seen)
        for key_name in ("windows", "front_windows"):
            for item in payload.get(key_name) or []:
                if not isinstance(item, Mapping):
                    continue
                span = _window_span(item)
                if span is None:
                    continue
                if span[0].astimezone(SHANGHAI).strftime("%Y-%m-%d") != day and span[1].astimezone(SHANGHAI).strftime("%Y-%m-%d") != day:
                    continue
                key = window_archive_key(item) or ""
                if key and key in seen_local:
                    continue
                if key:
                    seen_local.add(key)
                extra_windows.append(dict(item))

    if not extra_windows:
        result = (arch_windows, arch_index)
    else:
        combined = list(arch_windows) + extra_windows
        result = (combined, build_window_index(combined))

    if len(_COMBINED_WINDOWS_CACHE) >= _WINDOWS_CACHE_MAX:
        _COMBINED_WINDOWS_CACHE.pop(next(iter(_COMBINED_WINDOWS_CACHE)))
    _COMBINED_WINDOWS_CACHE[combined_key] = result
    return result


def attach_to_payload(
    payload: dict[str, Any],
    *,
    date: str,
    root: Path | str,
    max_idle_seconds: float = DEFAULT_MAX_IDLE_SECONDS,
) -> dict[str, Any]:
    """给 items[] 补 input_context，活动级汇总出现最多的应用。纯读，不改其他字段。"""
    try:
        windows, index = _load_windows_with_index(root=root, day=date)
    except Exception as error:  # noqa: BLE001 - 上下文失败不能拖垮读接口
        logger.warning("app context windows 读取失败（已忽略）: %s: %s", type(error).__name__, error)
        return payload
    if not windows:
        return payload

    for activity in payload.get("activities") or []:
        if not isinstance(activity, dict):
            continue
        per_item: list[dict[str, Any]] = []
        for item in activity.get("items") or []:
            if not isinstance(item, dict):
                continue
            ctx = context_for_item(item, windows, max_idle_seconds=max_idle_seconds, index=index)
            if ctx is None:
                continue
            item["input_context"] = ctx
            per_item.append(ctx)
        summary = summarize(per_item)
        if summary:
            activity["input_context"] = summary
    return payload


def summarize(contexts: Iterable[Mapping[str, Any]]) -> dict[str, Any] | None:
    """活动级：出现最多的应用（同数时优先 dictation，其次高置信）。"""
    items = [c for c in contexts if isinstance(c, Mapping) and c.get("app_label")]
    if not items:
        return None
    counter = Counter((str(c["app_label"]), str(c.get("kind") or "")) for c in items)
    (label, kind), count = sorted(
        counter.items(),
        key=lambda kv: (kv[1], kv[0][1] == "dictation", kv[0][0]),
        reverse=True,
    )[0]
    ctx = next(c for c in items if str(c["app_label"]) == label and str(c.get("kind") or "") == kind)
    return {
        "app_label": label,
        "target_bundle": ctx.get("target_bundle"),
        "kind": kind,
        "confidence": ctx.get("confidence"),
        "meaning": ctx.get("meaning"),
        "item_count": count,
        "item_total": len(items),
    }


# --------------------------------------------------------------------- 归档保留策略


def prune_day_files(days_dir: Path, *, keep_days: int = DEFAULT_KEEP_DAYS, today: str | None = None) -> list[str]:
    """删掉超过 keep_days 的日归档（只认 `YYYY-MM-DD.jsonl`）。返回被删的文件名。"""
    if not days_dir.is_dir():
        return []
    anchor = _parse(f"{today}T00:00:00+08:00") if today else datetime.now(SHANGHAI)
    if anchor is None:
        return []
    removed: list[str] = []
    for path in sorted(days_dir.glob("*.jsonl")):
        day = _parse(f"{path.stem}T00:00:00+08:00")
        if day is None:
            continue
        if (anchor - day).days > keep_days:
            try:
                path.unlink()
                removed.append(path.name)
            except OSError:
                pass
    return removed


def day_file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def can_append(path: Path, pending_bytes: int, *, max_bytes: int = DEFAULT_MAX_DAY_FILE_BYTES) -> bool:
    """单文件上限：超了就不再追加（宁可不记，也不删已有内容）。"""
    return day_file_size(path) + pending_bytes <= max_bytes
