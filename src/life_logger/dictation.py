"""「电脑前说话」(dictation) 的识别与生活看板隐藏规则（唯一实现处）。

两件事放在同一个模块里，因为它们是同一条链路的上下游：

1. **识别**：AirPassport 是 Mac 上的语音输入，Apple Watch 连续录音会把同一段话
   一并录进去。守护进程 ``scripts/watch_airpassport.py --mark-only`` 把每段
   dictation 的起止时刻写进 ``dictation-windows.jsonl``；再叠加一份「对着电脑
   说话」的语义特征词表，就能判断一段转写是不是 dictation。
   （本模块是这段逻辑的唯一实现；``scripts/attribute_funasr_transcript.py``
   从这里导入，历史上它自己实现过一份。）

2. **看板隐藏**：2026-10-13 root 拍板「数据全留、看板看不到」——dictation 内容
   一条都不从产物里删，只在 API 读层过滤。判据 = 上游标记 + 「录音绝对时间落在
   dictation 时间窗内」的重叠比对（复用本模块 ``record_time_window`` /
   ``is_dictation_record``，不做文本技术词的独立判断）。开关在
   ``local-config.json``：``hide_dictation_from_dashboard``（默认 true，关掉就
   恢复原样）。

过滤只作用于「生活看板展示层」的读接口（``/api/daily``、``/api/daily-brief``、
``/api/dashboard/daily``、``/api/timeline-scenes``、``/api/reminders``）；
``/api/pending-sources``（待确认区）、``/api/search``、任务产物、转写与录音一律不动。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

DICTATION_WINDOWS_FILE = "dictation-windows.jsonl"
DICTATION_OVERLAP_RATIO = 0.5

# 看板隐藏开关（local-config.json）。默认开启；显式写 false 就完全恢复原样。
DASHBOARD_FILTER_CONFIG_KEY = "hide_dictation_from_dashboard"
# 可选的补充时间窗：{"start": iso, "end": iso} 列表。
# 用途：PTT 没有记录到的「电脑前说话」（例如用户直接和 AI 对话而没按 PTT）可以
# 在这里手工补一段，仍然不改任何产物文件，删掉即恢复。
EXTRA_WINDOWS_CONFIG_KEY = "dictation_windows_extra"

# 显式口令产生的条目不算要隐藏的调试话：「记录一下…」是置顶便签（pinned_memo），
# 「查询一下…」是派发查询（query_dispatch）——两者都是用户主动要求记下来的，
# 上游策略（contextual_extraction）也把它们当作 dictation 的豁免口。
EXPLICIT_CAPTURE_FLAGS = ("pinned_memo", "query_dispatch")

_AI_DICTATION_MARKERS = (
    "单声道",
    "双声道",
    "模型内核",
    "固定阈值",
    "声纹识别",
    "声纹库",
    "SenseVoice",
    "Faster-Whisper",
    "FunASR",
    "GitHub",
    "API额度",
    "派个子系统",
    "C层对话",
    "子代理",
    "汇报一下现在",
    "之前让你做的是什么",
    "互动的东西历史",
    "整个项目还可以优化",
    "代码库",
    "重构一下",
)

_PERSONAL_LIFE_EVENT_GUARDS = (
    "吃了",
    "喝了",
    "点了",
    "花了",
    "买了",
    "付款",
    "支付",
    "汉堡",
    "嗨棒",
    "威士忌",
    "气泡水",
    "咖啡",
    "拿铁",
    "美式",
    "瑞幸",
    "星巴克",
    "乌冬面",
    "生脉饮",
    "深蹲",
    "卧推",
    "抽烟",
    "香烟",
    "记录一下",
    "记一下",
    "记录下",
    "记下",
    "查一下",
    "我要查",
    "帮我查",
    "查询一下",
    "检查一下",
    "待办",
    "提醒我",
    "便签",
)


# ---------------------------------------------------------------------------
# 识别：时间窗 + 语义特征
# ---------------------------------------------------------------------------


def load_dictation_windows(path: Path | None) -> list[tuple[Any, Any]]:
    """读 dictation 时间窗索引；文件不存在或读不动就返回空表。

    剔除功能绝不能成为新的失败点：索引缺失时应当静默退化为「一条都不剔」，
    而不是让整条流水线报错。
    """
    if path is None or not path.is_file():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    windows: list[tuple[Any, Any]] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
            start = datetime.fromisoformat(str(payload["start"]))
            end = datetime.fromisoformat(str(payload["end"]))
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            continue
        if end > start:
            windows.append((start, end))
    return windows


def record_time_window(record: Mapping[str, Any]) -> tuple[Any, Any] | None:
    """取一条转写记录的绝对起止时刻；时间缺失时返回 None。

    注意：``absolute_started_at`` **本身就是绝对时刻**（= 录音起点 + 偏移），
    不能再叠加 ``start_offset_seconds``，否则会把时间轴推后一倍偏移量，
    导致时间窗比对全部落空（2026-10-02 实测踩过）。
    """
    start_raw = record.get("absolute_started_at")
    end_raw = record.get("absolute_ended_at")
    if start_raw and end_raw:
        try:
            start = datetime.fromisoformat(str(start_raw))
            end = datetime.fromisoformat(str(end_raw))
        except ValueError:
            return None
        if end < start:
            start, end = end, start
        return (start, end)

    # 回退：只有基准时间时才用偏移推算
    if not start_raw:
        return None
    try:
        base = datetime.fromisoformat(str(start_raw))
        start_offset = float(record.get("start_offset_seconds") or 0.0)
        end_offset = float(record.get("end_offset_seconds") or 0.0)
    except (TypeError, ValueError):
        return None
    if end_offset < start_offset:
        start_offset, end_offset = end_offset, start_offset
    return (
        base + timedelta(seconds=start_offset),
        base + timedelta(seconds=end_offset),
    )


def is_semantic_ai_dictation(text: str) -> bool:
    cleaned = str(text or "").strip()
    if not cleaned:
        return False
    if any(guard in cleaned for guard in _PERSONAL_LIFE_EVENT_GUARDS):
        return False
    return any(marker in cleaned for marker in _AI_DICTATION_MARKERS)


# 历史名字：老代码从 scripts/attribute_funasr_transcript.py 里用过这个私有名。
_is_semantic_ai_dictation = is_semantic_ai_dictation


def span_is_dictation(
    span: tuple[datetime, datetime],
    windows: Sequence[tuple[datetime, datetime]],
    *,
    overlap_ratio: float = DICTATION_OVERLAP_RATIO,
    point_if_empty: bool = False,
) -> bool:
    """时间窗重合判断（全模块唯一的比对实现）。

    ``point_if_empty=True`` 时，零时长的时间点退化为「点落在窗口内」（看板的即时抓取
    卡片只有时刻、没有时长，需要这个口径）；默认 False，保持流水线原语义。
    """
    start, end = span
    duration = (end - start).total_seconds()
    if duration <= 0 and not point_if_empty:
        return False
    for win_start, win_end in windows:
        try:
            overlap = (min(end, win_end) - max(start, win_start)).total_seconds()
        except TypeError:
            # 一边是 aware、一边是 naive：比不了就不隐藏，宁可不剔。
            continue
        if duration <= 0:
            if win_start <= start <= win_end:
                return True
            continue
        if overlap > 0 and overlap / duration > overlap_ratio:
            return True
    return False


def is_dictation_record(
    record: Mapping[str, Any],
    windows: Sequence[tuple[Any, Any]],
    *,
    overlap_ratio: float = DICTATION_OVERLAP_RATIO,
    include_semantic: bool = True,
) -> bool:
    """这条记录是否属于「在电脑前说话」。

    判据：与某个 dictation 时间窗重合过半，或语义上为对 AI 编程助手的系统指令。
    ``include_semantic=False`` 时只做时间窗比对 —— 看板读层用这个口径：它只认
    「录音绝对时间落在窗口内」和上游已经打好的标记，不做文本特征判断。
    """
    if include_semantic and is_semantic_ai_dictation(str(record.get("text") or "")):
        return True
    if not windows:
        return False
    span = record_time_window(record)
    if span is None:
        return False
    return span_is_dictation(span, windows, overlap_ratio=overlap_ratio)


# ---------------------------------------------------------------------------
# 看板隐藏：事件 / 场景 / 待办
# ---------------------------------------------------------------------------


def _coerce_bool(value: Any, *, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _coerce_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_moment(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if value in (None, ""):
        return None
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def event_evidence(event: Mapping[str, Any]) -> Mapping[str, Any]:
    evidence = event.get("evidence")
    return evidence if isinstance(evidence, Mapping) else {}


def event_text(event: Mapping[str, Any]) -> str:
    """事件里所有能代表「说了什么」的文本，用于语义特征判断。"""
    parts = [str(event.get("title") or "")]
    evidence = event_evidence(event)
    parts.append(str(evidence.get("quote") or ""))
    return " ".join(part for part in parts if part).strip()


def event_time_window(event: Mapping[str, Any]) -> tuple[datetime, datetime] | None:
    """事件的绝对起止时刻（= 转写行的录音绝对时间）。

    取时间的三级口径，全部复用共享实现：

    1. 事件自带 ``absolute_started_at`` / ``absolute_ended_at``（转写行有）→
       ``record_time_window``；
    2. 否则用 ``evidence.reported_at``（实测逐条等于该转写行的
       ``absolute_started_at``）/ ``time.absolute`` 作为绝对起点，时长取 evidence 里
       ``end_offset_seconds - start_offset_seconds`` 的差 —— 不叠加偏移，避免把时间
       轴推后一倍（2026-10-02 踩过的坑）；
    3. 都没有就返回 None（宁可不隐藏，也不猜）。

    注意：dictation 判定的输入是「绝对时刻」本身，不是文本内容。
    """
    span = record_time_window(event)
    if span is not None:
        return span
    evidence = event_evidence(event)
    raw_time = event.get("time")
    time_block: Mapping[str, Any] = raw_time if isinstance(raw_time, Mapping) else {}
    start = _parse_moment(evidence.get("reported_at")) or _parse_moment(
        time_block.get("absolute")
    )
    if start is None:
        return None
    duration = 0.0
    try:
        if (
            evidence.get("start_offset_seconds") is not None
            and evidence.get("end_offset_seconds") is not None
        ):
            duration = float(evidence["end_offset_seconds"]) - float(
                evidence["start_offset_seconds"]
            )
    except (TypeError, ValueError):
        duration = 0.0
    if duration < 0:
        duration = -duration
    return (start, start + timedelta(seconds=duration))


def _has_explicit_capture(event: Mapping[str, Any]) -> bool:
    evidence = event_evidence(event)
    for flag in EXPLICIT_CAPTURE_FLAGS:
        if event.get(flag) or evidence.get(flag):
            return True
    return False


def _dictation_flag(event: Mapping[str, Any]) -> bool:
    if bool(event.get("is_dictation")):
        return True
    return bool(event_evidence(event).get("dictation_context"))


def _is_chat_app_event(event: Mapping[str, Any]) -> bool:
    """对微信/企业微信等社交软件说话属于个人生活沟通，不当作电脑工作 dictation 隐藏。"""
    chat_bundles = {"com.tencent.xinwechat", "com.tencent.weworkmac", "com.tencent.qq", "com.bytedance.feishu"}
    chat_labels = {"微信", "企业微信", "wechat", "wework", "qq", "飞书"}
    ev = event_evidence(event)
    for ctx in (event.get("input_context"), ev.get("input_context"), event.get("foreground_context")):
        if not isinstance(ctx, Mapping):
            continue
        bundle = str(ctx.get("target_bundle") or ctx.get("target_app") or ctx.get("foreground_bundle") or "").strip().lower()
        label = str(ctx.get("app_label") or ctx.get("foreground_app") or "").strip().lower()
        if bundle in chat_bundles or label in chat_labels:
            return True
    target = str(event.get("speech_target") or "").strip().lower().lstrip("对")
    return target in chat_labels


def _has_life_guard(text: str) -> bool:
    return any(guard in text for guard in _PERSONAL_LIFE_EVENT_GUARDS)


def load_extra_windows(raw: Any) -> list[tuple[datetime, datetime]]:
    """读 ``dictation_windows_extra``：``[{"start": iso, "end": iso}, ...]``。

    解析不了的条目直接跳过（和主时间窗文件同样的「绝不成为新的失败点」原则）。
    """
    if not isinstance(raw, (list, tuple)):
        return []
    windows: list[tuple[datetime, datetime]] = []
    for item in raw:
        start = end = None
        if isinstance(item, Mapping):
            start = _parse_moment(item.get("start"))
            end = _parse_moment(item.get("end"))
        elif isinstance(item, (list, tuple)) and len(item) == 2:
            start = _parse_moment(item[0])
            end = _parse_moment(item[1])
        if start is None or end is None:
            continue
        if end < start:
            start, end = end, start
        if end > start:
            windows.append((start, end))
    return windows


@dataclass(frozen=True)
class DashboardDictationFilter:
    """生活看板读层的 dictation 过滤器。

    ``enabled=False``（配置开关关掉）时所有方法都返回「不需要隐藏」，即完全恢复原样。
    """

    enabled: bool = True
    windows: tuple[tuple[datetime, datetime], ...] = ()
    extra_windows: tuple[tuple[datetime, datetime], ...] = ()

    @property
    def all_windows(self) -> tuple[tuple[datetime, datetime], ...]:
        return tuple(self.windows) + tuple(self.extra_windows)

    def hide_event(self, event: Mapping[str, Any]) -> bool:
        """这条事件该不该从生活看板上消失。

        判据只有两条，都不做「文本里出现技术词」的独立判断：

        1. 上游（流水线）已经把它标成 dictation（``is_dictation`` /
           ``evidence.dictation_context``）；
        2. 读时把它的**录音绝对时间**与该时间窗做重叠比对，命中共享实现
           ``is_dictation_record(..., include_semantic=False)`` —— 与
           ``scripts/attribute_funasr_transcript.py`` 用同一份 ``record_time_window``
           / 重合比例的判定，不另行重写。

        保留网：显式口令产物（便签 / 查询派发）与生活保护词命中的句子不隐藏。
        """
        if not self.enabled or not isinstance(event, Mapping):
            return False
        # 1) 用户主动要求记下来的条目（便签 / 查询派发）与对微信等聊天软件说的话永远保留。
        if _has_explicit_capture(event) or _is_chat_app_event(event):
            return False
        # 2) 上游已经打好标记的，直接隐藏。
        if _dictation_flag(event):
            return True
        # 3) 生活保护词（吃了 / 买了 / 抽烟 / 记录一下…）优先，哪怕落在时间窗里。
        if _has_life_guard(event_text(event)):
            return False
        # 4) 读时按绝对时间比时间窗（时间拿不到的宁可不隐藏）。
        span = event_time_window(event)
        if span is None:
            return False
        return is_dictation_record(
            {
                "text": event_text(event),
                "absolute_started_at": span[0].isoformat(),
                "absolute_ended_at": span[1].isoformat(),
            },
            self.all_windows,
            include_semantic=False,
        )

    def partition_events(
        self, events: Iterable[Mapping[str, Any]]
    ) -> tuple[list[Mapping[str, Any]], list[Mapping[str, Any]]]:
        kept: list[Mapping[str, Any]] = []
        hidden: list[Mapping[str, Any]] = []
        for event in events:
            (hidden if self.hide_event(event) else kept).append(event)
        return kept, hidden

    def filter_events(
        self, events: Iterable[Mapping[str, Any]]
    ) -> list[Mapping[str, Any]]:
        return self.partition_events(events)[0]

    def hidden_event_ids(self, events: Iterable[Mapping[str, Any]]) -> set[str]:
        hidden = self.partition_events(events)[1]
        return {
            str(event.get("event_id"))
            for event in hidden
            if isinstance(event, Mapping) and event.get("event_id")
        }

    def _minute_spans(self, date: str) -> list[tuple[int, int]]:
        """把时间窗映射成某天本地时间的「当天分钟」区间（跨天窗口自动裁剪）。"""
        try:
            day_start = datetime.fromisoformat(f"{date}T00:00:00+08:00")
        except ValueError:
            return []
        day_end = day_start + timedelta(days=1)
        spans: list[tuple[int, int]] = []
        for win_start, win_end in self.all_windows:
            try:
                start = max(win_start, day_start)
                end = min(win_end, day_end)
            except TypeError:
                continue
            if end <= start:
                continue
            spans.append(
                (
                    int((start - day_start).total_seconds() // 60),
                    int((end - day_start).total_seconds() // 60),
                )
            )
        return spans

    def hide_scene(self, scene: Mapping[str, Any], *, date: str) -> bool:
        """场景整段都落在某个 dictation 窗口里 → 连场景名一起隐藏。"""
        if not self.enabled or not isinstance(scene, Mapping):
            return False
        start_minute = _coerce_int(scene.get("start_minute"))
        end_minute = _coerce_int(scene.get("end_minute"))
        if start_minute is None or end_minute is None:
            return False
        if end_minute <= start_minute:
            return False
        return any(
            win_start <= start_minute and end_minute <= win_end
            for win_start, win_end in self._minute_spans(date)
        )

    def filter_scenes(
        self, scenes: Iterable[Mapping[str, Any]], *, date: str
    ) -> list[Any]:
        if not self.enabled:
            return list(scenes)
        return [scene for scene in scenes if not self.hide_scene(scene, date=date)]

    def hide_captured_item(self, item: Mapping[str, Any]) -> bool:
        """看板上的即时抓取卡片（待办 / 记账 / 灵感）。

        卡片没有 event_id，只有 ``captured_at``：落在 dictation 窗口里就隐藏。
        带口令标记的卡片（``trigger_type == memo/query`` 之类由口令产生）保留。
        """
        if not self.enabled or not isinstance(item, Mapping):
            return False
        if str(item.get("event_id") or ""):
            return False
        captured_at = _parse_moment(item.get("captured_at"))
        if captured_at is None:
            return False
        return span_is_dictation(
            (captured_at, captured_at), self.all_windows, point_if_empty=True
        )

    def filter_captured_items(
        self, items: Iterable[Mapping[str, Any]]
    ) -> list[Any]:
        if not self.enabled:
            return list(items)
        return [item for item in items if not self.hide_captured_item(item)]

    def filter_reminders(
        self,
        reminders: Iterable[Mapping[str, Any]],
        *,
        event_lookup: (
            Callable[[Mapping[str, Any]], Mapping[str, Any] | None] | None
        ) = None,
    ) -> list[Mapping[str, Any]]:
        """待办列表：只在展示层隐去 dictation 来源的条目。

        事实源（OpenClaw / reminders.json / 产物）一条不动。判不出来源的一律保留。
        """
        if not self.enabled:
            return list(reminders)
        kept: list[Mapping[str, Any]] = []
        for item in reminders:
            if not isinstance(item, Mapping):
                kept.append(item)
                continue
            if not item.get("event_id"):
                # OpenClaw 的人工任务没有 event_id：不是 audio 来源，保留。
                kept.append(item)
                continue
            event = event_lookup(item) if event_lookup is not None else None
            if event is None or not self.hide_event(event):
                kept.append(item)
        return kept


def build_dashboard_dictation_filter(
    config: Mapping[str, Any] | None,
    *,
    root: Path | None = None,
    enabled: bool | None = None,
) -> DashboardDictationFilter:
    """从 local-config.json 的配置内容构造过滤器。

    * ``hide_dictation_from_dashboard``（默认 true）
    * ``dictation_windows`` 覆盖时间窗索引路径（既有约定）
    * ``dictation_windows_extra`` 追加手工补的窗口
    """
    settings = config if isinstance(config, Mapping) else {}
    resolved_enabled = (
        bool(enabled)
        if enabled is not None
        else _coerce_bool(settings.get(DASHBOARD_FILTER_CONFIG_KEY), default=True)
    )
    windows_path: Path | None = None
    raw_path = settings.get("dictation_windows")
    if raw_path:
        windows_path = Path(str(raw_path)).expanduser()
    elif root is not None:
        windows_path = Path(root) / DICTATION_WINDOWS_FILE
    windows = tuple(load_dictation_windows(windows_path)) if windows_path else ()
    extra = tuple(load_extra_windows(settings.get(EXTRA_WINDOWS_CONFIG_KEY)))
    return DashboardDictationFilter(
        enabled=resolved_enabled, windows=windows, extra_windows=extra
    )


def local_day_minute_spans(
    windows: Sequence[tuple[datetime, datetime]], date: str
) -> list[tuple[int, int]]:
    """把时间窗换算成 ``date`` 当天的分钟区间（供测试与外部复用）。"""
    filter_stub = DashboardDictationFilter(windows=tuple(windows))
    return filter_stub._minute_spans(date)


__all__ = [
    "DICTATION_OVERLAP_RATIO",
    "DICTATION_WINDOWS_FILE",
    "DASHBOARD_FILTER_CONFIG_KEY",
    "EXTRA_WINDOWS_CONFIG_KEY",
    "EXPLICIT_CAPTURE_FLAGS",
    "DashboardDictationFilter",
    "build_dashboard_dictation_filter",
    "event_evidence",
    "event_text",
    "event_time_window",
    "is_dictation_record",
    "is_semantic_ai_dictation",
    "load_dictation_windows",
    "load_extra_windows",
    "local_day_minute_spans",
    "record_time_window",
    "span_is_dictation",
]
