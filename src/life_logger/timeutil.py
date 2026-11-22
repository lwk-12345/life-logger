"""统一时间口径：全仓唯一的时间解析 / 本地化工具。

【为什么需要这个模块】
2026-10-17 排查时踩到真实事故：Watch 会话 manifest 的 ``started_at`` 是
``2026-10-17T06:43:15+00:00``（UTC），而事件 ``time.absolute`` 也是 UTC。
临时排查脚本直接 ``ts[11:19]`` 切字符串，把**下午 14:56 的烧鸭饭读成了早上 06:56**，
进而编出「通宵后吃早餐」的假叙事。

根因不是「数据错了」，而是**同一天里存在两套时间表达**：
  - manifest / transcript row / event.time.absolute → UTC（``+00:00``）
  - Web 前端 / 台账 / 用户口头 → 北京时间（``+08:00``）
任何下游只要偷懒截字符串就会错 8 小时。

修复策略：
  1. **存储保留 UTC 绝对时刻**（不丢信息、可排序、跨时区安全）；
  2. **所有落盘的事件 / 会话元数据同时写入本地可读字段**
     （``time.local`` / ``time.clock`` / ``manifest.started_at_local``），
     让「看文件的人」不用心算；
  3. 所有需要「人看到的时间」的地方，统一走本模块，禁止再手写时区换算。

用法：
    from life_logger.timeutil import local_iso, local_clock, parse_ts
    local_iso("2026-10-17T06:56:32+00:00")   # '2026-10-17T14:56:32+08:00'
    local_clock("2026-10-17T06:56:32+00:00") # '14:56'
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

#: 日记 / 事件 / 台账统一使用的本地时区。
LOCAL_TZ = ZoneInfo("Asia/Shanghai")

#: 兼容旧代码里写死的固定偏移（无需加载 tzdata 的场景）。
LOCAL_TZ_FIXED = timezone(timedelta(hours=8))


def parse_ts(value: Any) -> datetime | None:
    """把各种时间写法解析成**带时区**的 datetime。

    支持：
      - ISO 8601 字符串（``Z`` / ``+08:00`` / ``+00:00`` / 无时区）
      - epoch 秒 / 毫秒（int / float）
      - 已经是 datetime 的对象

    无时区信息时按本地时区理解（Watch 上传的都是带偏移的，这里只是兜底）。
    解析失败返回 ``None``，不抛异常 —— 时间字段缺失是常态。
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=LOCAL_TZ)
    if isinstance(value, (int, float)):
        seconds = float(value)
        if seconds > 1e11:  # 毫秒
            seconds /= 1000.0
        try:
            return datetime.fromtimestamp(seconds, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=LOCAL_TZ)


def to_local(value: Any) -> datetime | None:
    """任意时间写法 → 本地时区 datetime。"""
    parsed = parse_ts(value)
    return parsed.astimezone(LOCAL_TZ) if parsed is not None else None


def local_iso(value: Any) -> str | None:
    """任意时间写法 → 带 ``+08:00`` 偏移的 ISO 字符串（人直接可读）。"""
    local = to_local(value)
    return local.isoformat() if local is not None else None


def local_clock(value: Any) -> str | None:
    """任意时间写法 → ``HH:MM``（本地时区）。"""
    local = to_local(value)
    return local.strftime("%H:%M") if local is not None else None


def local_date(value: Any) -> str | None:
    """任意时间写法 → 本地时区的 ``YYYY-MM-DD``。

    注意：不要用 ``iso[:10]`` 切 UTC 字符串判日期 ——
    凌晨 00:30（北京）在 UTC 里是**前一天** 16:30，会跨天归档错。
    """
    local = to_local(value)
    return local.strftime("%Y-%m-%d") if local is not None else None


def utc_iso(value: Any) -> str | None:
    """任意时间写法 → UTC ISO 字符串（落盘用的规范绝对时刻）。"""
    parsed = parse_ts(value)
    return parsed.astimezone(timezone.utc).isoformat() if parsed is not None else None


#: 事件对象里需要「同时补一份本地可读副本」的字段路径。
_EVENT_TS_PATHS: tuple[tuple[str, ...], ...] = (
    ("time", "absolute"),
    ("time", "occurrence_time"),
    ("time", "absolute_ended_at"),
    ("evidence", "reported_at"),
    ("evidence", "occurrence_time_evidence"),
    ("evidence", "absolute_started_at"),
    ("evidence", "absolute_ended_at"),
    ("evidence", "captured_at"),
)


def annotate_local_time(event: dict[str, Any]) -> dict[str, Any]:
    """给事件补上本地时区可读字段（原地修改并返回）。

    只**新增**字段，不改写原有 UTC 值 —— 下游读 UTC 的逻辑完全不受影响，
    而人 / 外部 Agent 打开 events.json 时能直接看到 ``2026-10-17T14:56``。
    """
    anchor = None
    for path in _EVENT_TS_PATHS:
        node: Any = event
        for key in path[:-1]:
            node = node.get(key) if isinstance(node, dict) else None
        if not isinstance(node, dict):
            continue
        raw = node.get(path[-1])
        local = to_local(raw)
        if local is None:
            continue
        if anchor is None:
            anchor = local
        node.setdefault(f"{path[-1]}_local", local.isoformat())

    time_node = event.get("time")
    if isinstance(time_node, dict) and anchor is not None:
        time_node.setdefault("local", anchor.isoformat())
        time_node.setdefault("clock", anchor.strftime("%H:%M"))
        time_node.setdefault("date", anchor.strftime("%Y-%m-%d"))
        time_node.setdefault("timezone", "Asia/Shanghai")
    return event


def annotate_segment_local_times(
    manifest: dict[str, Any],
    *,
    started_at_key: str = "started_at",
) -> dict[str, Any]:
    """给会话 manifest 补上 ``started_at_local`` 以及每段的 ``started_at_local``。

    手表上传的每段只有 ``start_offset_ms``，必须靠 session 起点才能算出绝对时刻。
    每次都让人在前端 / 脚本里心算一次，迟早出错 —— 直接写进 manifest。
    """
    session_start = to_local(manifest.get(started_at_key))
    if session_start is None:
        return manifest
    manifest.setdefault("started_at_local", session_start.isoformat())
    manifest.setdefault("timezone", "Asia/Shanghai")
    for segment in manifest.get("segments") or []:
        if not isinstance(segment, dict):
            continue
        if segment.get("started_at_local"):
            continue
        try:
            offset_ms = int(segment.get("start_offset_ms") or 0)
        except (TypeError, ValueError):
            continue
        segment["started_at_local"] = (
            session_start + timedelta(milliseconds=offset_ms)
        ).isoformat()
    return manifest


def normalize_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    """写盘前统一整理会话时间字段（幂等）。"""
    return annotate_segment_local_times(manifest)
