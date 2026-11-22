"""输入应用上下文证据（input context）· 纯函数 · 2026-10-25

用途：把**已采集**的前台应用窗口（不是常驻采集器）作为**上下文证据**，辅助理解
"这句话是对哪个应用说的"。它只回答"区间重叠 + 唯一目标应用"，**不推断是否已发送**。

严格条件（任一不满足 → unknown + 原因）：
  * window 的 start/end 都能解析（缺结束 → `missing_end`）；
  * 与记录的时间区间**有重叠**（无重叠 → `no_overlap`）；
  * 重叠的窗口**只有一个**且其 `target_bundle` 唯一（多个 → `multiple_targets`）；
  * 时间标冲突（窗口时区/顺序非法）→ `time_conflict`；
  * 完全没有任何窗口 → `no_signal`（旧路等价，不附加字段）。

不读窗口文本、不做应用分类、不改 source_label/身份。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

SCHEMA = "input-context-v1"

#: 允许透传给模型/事件的字段（白名单）
INPUT_CONTEXT_FIELDS = ("start", "end", "target_bundle", "app_label", "provider", "source", "confidence")

#: 已知 bundle → 人类可读应用名。没收录的退化为「bundle 最后一段首字母大写」。
#: 只收录**能确定对应关系**的；宁可显示 `Alma` 也不要瞎猜成「笔记」。
BUNDLE_APP_LABELS: dict[str, str] = {
    # ── 本项目/工作相关 ──
    "com.yetone.alma": "Alma",
    "com.openai.codex": "Codex",
    "com.microsoft.VSCode": "VS Code",
    "com.apple.Terminal": "Terminal",
    "com.googlecode.iterm2": "iTerm2",
    "com.jetbrains.pycharm": "PyCharm",
    "com.jetbrains.intellij": "IntelliJ IDEA",
    "com.todesktop.230313mzl4w4u92": "Cursor",
    "com.github.GitHubClient": "GitHub Desktop",
    "com.apple.dt.Xcode": "Xcode",
    # ── 浏览器 ──
    "com.google.Chrome": "Chrome",
    "com.google.Chrome.canary": "Chrome Canary",
    "org.mozilla.firefox": "Firefox",
    "com.apple.Safari": "Safari",
    "com.microsoft.edgemac": "Edge",
    "company.thebrowser.Browser": "Arc",
    # ── 即时通讯 / 社交 ──
    "com.tencent.xinWeChat": "微信",
    "com.tencent.qq": "QQ",
    "com.tencent.WeWorkMac": "企业微信",
    "com.alibaba.DingTalkMac": "钉钉",
    "com.bytedance.feishu": "飞书",
    "com.tinyspeck.slackmacgap": "Slack",
    "com.telegram.desktop": "Telegram",
    "ru.keepcoder.Telegram": "Telegram",
    "com.apple.MobileSMS": "信息",
    "com.apple.mail": "邮件",
    # ── 影音 / 内容 ──
    "com.google.Chrome.helper": "Chrome",
    "com.apple.Music": "音乐",
    "com.apple.QuickTimePlayerX": "QuickTime Player",
    "com.apple.TV": "Apple TV",
    "tv.danmaku.bilibili": "哔哩哔哩",
    "com.bilibili.bilibili": "哔哩哔哩",
    "com.zhihu.ios": "知乎",
    "com.ss.iphone.ugc.Aweme": "抖音",
    # ── 办公 / 笔记 ──
    "com.apple.Notes": "备忘录",
    "com.apple.Preview": "预览",
    "com.apple.finder": "访达",
    "com.apple.systempreferences": "系统设置",
    "com.microsoft.Word": "Word",
    "com.microsoft.Excel": "Excel",
    "com.microsoft.Powerpoint": "PowerPoint",
    "notion.id": "Notion",
    "md.obsidian": "Obsidian",
    # ── 语音输入法 ──
    "com.yetone.alma.ime": "Alma 输入法",
    "com.bytedance.inputmethod.DoubaoIme": "豆包输入法",
    "com.apple.inputmethod.SCIM": "系统拼音",
    "com.baidu.inputMethod": "百度输入法",
    "com.sogou.inputmethod.sogou": "搜狗输入法",
}


def bundle_to_app_label(bundle_id: Any) -> str | None:
    """把反向域名 bundle 转成人类可读应用名。

    * 已知 → `BUNDLE_APP_LABELS` 的值；
    * 未知 → bundle 最后一段首字母大写（`com.foo.BarBaz` → `BarBaz`）；
    * 空/非字符串 → ``None``（**不编造名字**）。

    纯函数、无 I/O；大小写不敏感匹配已知表（bundle 大小写在不同 app 版本里会飘）。
    """
    if not isinstance(bundle_id, str):
        return None
    raw = bundle_id.strip()
    if not raw:
        return None
    exact = BUNDLE_APP_LABELS.get(raw)
    if exact:
        return exact
    lowered = raw.lower()
    for known, label in BUNDLE_APP_LABELS.items():
        if known.lower() == lowered:
            return label
    tail = raw.rsplit(".", 1)[-1].strip()
    if not tail:
        return None
    return tail[0].upper() + tail[1:]


def window_app_label(window: Mapping[str, Any] | None) -> str | None:
    """从窗口取人类可读应用名：优先已写入的 `app_label`，否则从 bundle 推。"""
    if not isinstance(window, Mapping):
        return None
    existing = window.get("app_label")
    if isinstance(existing, str) and existing.strip():
        return existing.strip()
    return bundle_to_app_label(window.get("target_app") or window.get("target_bundle"))


def _text(value: Any) -> str:
    return str(value or "")


def _parse(value: Any) -> datetime | None:
    raw = _text(value).strip()
    if not raw:
        return None
    try:
        moment = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return moment


def _unknown(reason: str) -> dict[str, Any]:
    return {"schema": SCHEMA, "status": "unknown", "reason": reason}


def normalize_windows(windows: Any) -> tuple[list[Mapping[str, Any]], Mapping[str, Any] | None]:
    """接受采集器真实输出（`{"windows": [...], "provider": {...}}` 或裸数组），返回 (windows, root_provider)。"""
    root_provider = None
    if isinstance(windows, Mapping):
        if isinstance(windows.get("provider"), Mapping):
            root_provider = windows.get("provider")
        windows = windows.get("windows") or []
    if not isinstance(windows, Sequence) or isinstance(windows, (str, bytes)):
        return [], root_provider
    return [item for item in windows if isinstance(item, Mapping)], root_provider


def _window_provider(window: Mapping[str, Any], root_provider: Mapping[str, Any] | None = None) -> dict[str, Any] | str | None:
    """提取语音输入程序证据（provider）；绝不等于目标接收者或已发送。"""
    raw = window.get("provider")
    if isinstance(raw, Mapping):
        pid = _text(raw.get("pid")).strip() or None
        process = _text(raw.get("process")).strip() or None
        source = _text(raw.get("source")).strip() or None
        if pid or process or source:
            return {"pid": pid, "process": process, "source": source}
        return None
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    w_pid = _text(window.get("provider_pid")).strip() or None
    if isinstance(root_provider, Mapping):
        r_pid = _text(root_provider.get("pid")).strip() or None
        r_proc = _text(root_provider.get("process")).strip() or None
        r_src = _text(root_provider.get("source")).strip() or None
        pid = w_pid or r_pid
        if pid or r_proc or r_src:
            return {"pid": pid, "process": r_proc, "source": r_src}
    if w_pid:
        return {"pid": w_pid, "process": None, "source": None}
    return None


def _window_times(window: Mapping[str, Any]) -> tuple[Any, Any]:
    """采集器用 `started_at/ended_at`；旧规范用 `start/end`（别名兼容）。"""
    start = window.get("started_at") or window.get("start")
    end = window.get("ended_at") or window.get("end")
    return start, end


def _window_target(window: Mapping[str, Any]) -> str:
    """采集器用 `target_app`；旧规范用 `target_bundle`（别名兼容）。"""
    return _text(window.get("target_app") or window.get("target_bundle")).strip()


def _is_naive(dt: datetime | None) -> bool:
    return dt is not None and dt.tzinfo is None


def _safe_cmp(t1: datetime | None, t2: datetime | None) -> int | None:
    """安全比较两个时间点。若时区不兼容（naive vs aware）返回 None，防止崩溃。"""
    if t1 is None or t2 is None:
        return None
    if (t1.tzinfo is None) != (t2.tzinfo is None):
        return None
    try:
        if t1 < t2:
            return -1
        if t1 > t2:
            return 1
        return 0
    except TypeError:
        return None


def resolve_input_context(
    *,
    record_start: Any,
    record_end: Any,
    windows: Any,
) -> dict[str, Any]:
    """返回该记录可用的 input_context（或 unknown + 原因）。纯函数。

    时间边界保守原则：
    - naive/aware 不混比，未知时区不猜（返回 time_zone_unknown）；
    - 正时长区间端点接触不算重叠（0ms 瞬时接触排除）；
    - 对有正时长的 Watch 行，只有目标窗口或同 bundle 连续无冲突窗口完整覆盖该行才 resolved；
    - partial 用 unknown / partial_overlap 并保留审计字段，不当整行已绑定；
    - 点时刻保留精确半开区间 [w_start, w_end) 语义。
    """
    windows_list, root_provider = normalize_windows(windows)
    if not windows_list:
        return _unknown("no_signal")
    windows = windows_list
    start, end = _parse(record_start), _parse(record_end) or _parse(record_start)
    if start is None:
        return _unknown("missing_record_time")
    record_finish = end or start

    # naive/aware 不混比，未知时区不猜
    if _is_naive(start) or _is_naive(record_finish):
        return _unknown("time_zone_unknown")
    if _safe_cmp(start, record_finish) is None:
        return _unknown("time_zone_unknown")
    if _safe_cmp(record_finish, start) == -1:
        return _unknown("time_conflict")

    candidates: list[tuple[Mapping[str, Any], datetime, datetime]] = []
    temporal_risks: list[str] = []
    record_dur = max(0.0, (record_finish - start).total_seconds())

    for window in windows:
        if not isinstance(window, Mapping):
            continue
        raw_start, raw_end = _window_times(window)
        w_start, w_end = _parse(raw_start), _parse(raw_end)

        # 检查时区兼容性：naive 与 aware 不混比，未知时区不猜
        if _is_naive(w_start) or _is_naive(w_end):
            temporal_risks.append("time_zone_unknown")
            continue

        if w_start is None:
            if w_end is not None and _safe_cmp(w_end, start) == 1:
                temporal_risks.append("unparseable_time")
            continue
        if w_end is None:
            # 缺结束时间：若起点早于记录结束则可能重叠
            cmp_start_fin = _safe_cmp(w_start, record_finish)
            if cmp_start_fin is not None and cmp_start_fin < 0:
                temporal_risks.append("missing_end")
            continue

        # 检查窗口本身时标顺序
        cmp_window_order = _safe_cmp(w_end, w_start)
        if cmp_window_order is not None and cmp_window_order == -1:
            if _safe_cmp(w_end, record_finish) == -1 and _safe_cmp(w_start, start) == 1:
                temporal_risks.append("time_conflict")
            continue

        window_dur = max(0.0, (w_end - w_start).total_seconds())

        # 正时长区间端点接触不算重叠：严格要求 w_start < record_finish 且 w_end > start
        # 点时刻：半开区间 [w_start, w_end) 语义，要求 w_start <= start < w_end
        if record_dur > 0 and window_dur > 0:
            if _safe_cmp(w_start, record_finish) >= 0 or _safe_cmp(w_end, start) <= 0:
                continue
        elif record_dur == 0 and window_dur > 0:
            if _safe_cmp(w_start, start) == 1 or _safe_cmp(w_end, start) <= 0:
                continue
        else:
            if _safe_cmp(w_start, record_finish) > 0 or _safe_cmp(w_end, start) < 0:
                continue

        candidates.append((window, w_start, w_end))

    # 阶段 2：校验重叠候选的质量与异常
    for window, _, _ in candidates:
        if window.get("complete") is False:
            return _unknown("incomplete_window")
        if window.get("target_unknown") is True:
            return _unknown("target_unknown")
        fronts = window.get("front_bundles")
        if isinstance(fronts, (list, tuple)) and len({str(x) for x in fronts}) > 1:
            return _unknown("multiple_front_apps")

    overlaps = [w for w, _, _ in candidates]
    if not overlaps:
        if temporal_risks:
            return _unknown(sorted(set(temporal_risks))[0])
        return _unknown("no_overlap")

    bundles = {target for target in (_window_target(w) for w in overlaps) if target}
    if not bundles:
        return _unknown("missing_target")
    if len(bundles) > 1:
        return _unknown("multiple_targets")

    sorted_cand = sorted(candidates, key=lambda item: item[1])
    primary_window = sorted_cand[0][0]

    # 1. 检查是否有冲突的 provider PID
    pids = {str(_window_provider(w, root_provider).get("pid")) for w in overlaps
            if isinstance(_window_provider(w, root_provider), Mapping) and _window_provider(w, root_provider).get("pid")}
    if len(pids) > 1:
        return _unknown("multiple_targets")

    # 2. 检查覆盖范围与连续性
    starts_on_or_before = (_safe_cmp(sorted_cand[0][1], start) <= 0)
    max_covered = sorted_cand[0][2]
    has_gap = False
    for i in range(1, len(sorted_cand)):
        next_start = sorted_cand[i][1]
        next_end = sorted_cand[i][2]
        if _safe_cmp(max_covered, next_start) == -1:
            has_gap = True
            break
        if _safe_cmp(max_covered, next_end) == -1:
            max_covered = next_end
    ends_on_or_after = (_safe_cmp(max_covered, record_finish) >= 0)
    is_complete_coverage = (starts_on_or_before and ends_on_or_after and not has_gap)

    # 计算总重叠时长与覆盖比例（保留审计）
    clipped_intervals: list[tuple[datetime, datetime]] = []
    for _, ws, we in candidates:
        cs = max(ws, start)
        ce = min(we, record_finish)
        if cs < ce:
            clipped_intervals.append((cs, ce))
    total_overlap_s = 0.0
    if clipped_intervals:
        merged_clipped: list[list[datetime]] = []
        for cs, ce in sorted(clipped_intervals, key=lambda x: x[0]):
            if not merged_clipped:
                merged_clipped.append([cs, ce])
            else:
                prev_s, prev_e = merged_clipped[-1]
                if cs <= prev_e:
                    merged_clipped[-1][1] = max(prev_e, ce)
                else:
                    merged_clipped.append([cs, ce])
        total_overlap_s = sum((ce - cs).total_seconds() for cs, ce in merged_clipped)

    record_s = max(0.001, record_dur)
    coverage_ratio = round(total_overlap_s / record_s, 3) if record_dur > 0 else 1.0

    final_raw_start, _ = _window_times(sorted_cand[0][0])
    latest_window = max(sorted_cand, key=lambda item: item[2])[0]
    _, final_raw_end = _window_times(latest_window)

    # 正时长记录：未完整覆盖不得冒认整句，返回 unknown: partial_overlap 并保留审计
    if record_dur > 0 and not is_complete_coverage:
        res = _unknown("partial_overlap")
        res["target_bundle"] = list(bundles)[0]
        res["app_label"] = bundle_to_app_label(res["target_bundle"])
        res["overlap_seconds"] = round(total_overlap_s, 3)
        res["record_seconds"] = round(record_dur, 3)
        res["coverage_ratio"] = coverage_ratio
        res["start"] = _text(final_raw_start)
        res["end"] = _text(final_raw_end)
        res["provider"] = _window_provider(primary_window, root_provider)
        res["meaning"] = "input_target_only_not_delivery"
        return res

    # 完整覆盖（或点时刻精准覆盖）：判定为 resolved
    raw_conf = primary_window.get("confidence")
    confidence = float(raw_conf) if raw_conf is not None else None

    context = {
        "schema": SCHEMA,
        "status": "resolved",
        "start": _text(final_raw_start),
        "end": _text(final_raw_end),
        "target_bundle": list(bundles)[0],
        "app_label": bundle_to_app_label(list(bundles)[0]),
        "provider": _window_provider(primary_window, root_provider),
        "schema_source": "collector" if "started_at" in primary_window or "target_app" in primary_window else "legacy",
        "source": _text(primary_window.get("source")) or None,
        "confidence": confidence,
        "coverage_ratio": coverage_ratio,
        "meaning": "input_target_only_not_delivery",
    }
    return context


def record_input_context(record: Mapping[str, Any], windows: Sequence[Mapping[str, Any]] | None) -> dict[str, Any] | None:
    """若 record 已带 input_context（上游已判定）则原样白名单透传，否则就地解析。"""
    existing = record.get("input_context")
    if isinstance(existing, Mapping) and existing.get("status") == "resolved":
        return {key: existing.get(key) for key in INPUT_CONTEXT_FIELDS} | {
            "schema": SCHEMA, "status": "resolved",
            "meaning": "input_target_only_not_delivery",
        }
    if windows is None:
        return None
    resolved = resolve_input_context(
        record_start=record.get("timestamp") or record.get("absolute_started_at"),
        record_end=record.get("absolute_ended_at") or record.get("timestamp"),
        windows=windows,
    )
    return resolved if resolved.get("status") == "resolved" else resolved


# ─────────────────────────────────────────────────────────────────────────────
# 按日归档（2026-11-04）
#
# 为什么要有：`input-context/latest.json` 只保留 30 分钟（retention），过了就
# 永远回溯不到"我当时在哪个应用里说话"。而活动时间线是在数小时后才跑的 ——
# 等它要查，窗口早就被裁掉了。
#
# 所以运行时把**已闭合**的窗口增量追加到 `input-context/days/YYYY-MM-DD.jsonl`
# （按北京时间日期分文件），读取时优先 latest.json，缺历史再回落归档。
# 每行一个 JSON 对象，append-only，不重写历史文件 —— 归档被打断也不会丢已写的行。
# ─────────────────────────────────────────────────────────────────────────────

ARCHIVE_DIR_RELATIVE = "input-context/days"
ARCHIVE_SCHEMA = "input-context-archive-v1"


def window_archive_key(window: Mapping[str, Any] | None) -> str | None:
    """归档去重键：同一段窗口无论被收割几次，键都相同。

    用 `(started_at, ended_at, target_app)` —— 集采器的窗口边界/目标应用是它的身份；
    不把 `_end` 之类的内存字段算进来（它们不落盘）。
    """
    if not isinstance(window, Mapping):
        return None
    start = _text(window.get("started_at") or window.get("start")).strip()
    if not start:
        return None
    end = _text(window.get("ended_at") or window.get("end")).strip()
    target = _text(window.get("target_app") or window.get("target_bundle")).strip()
    return f"{start}|{end}|{target}"


def archive_day_for_window(window: Mapping[str, Any]) -> str | None:
    """窗口归属日期（北京时间 YYYY-MM-DD）：优先 ended_at，其次 started_at。"""
    for field in ("ended_at", "started_at", "end", "start"):
        moment = _parse(window.get(field))
        if moment is None:
            continue
        if moment.tzinfo is None:
            continue                      # naive 不猜时区
        return moment.astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")
    return None


def read_archived_day(root: Path | str, day: str) -> list[dict[str, Any]]:
    """读 `input-context/days/<day>.jsonl` 的全部窗口（坏行跳过，不抛）。"""
    path = Path(root) / ARCHIVE_DIR_RELATIVE / f"{day}.jsonl"
    if not path.is_file():
        return []
    out: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except ValueError:
                    continue
                if isinstance(item, dict):
                    out.append(item)
    except OSError:
        return []
    out.sort(key=lambda w: _text(w.get("started_at") or w.get("start")))
    return out


def load_input_context_windows(root: Path | str, day: str | None = None) -> list[dict[str, Any]]:
    """读窗口：`latest.json`（近 30 分钟）+ 可选的历史归档。

    * `day=None` → 只读 latest.json（**旧行为完全等价**）；
    * `day="YYYY-MM-DD"` → latest.json 与 `days/<day>.jsonl` 合并，按归档键去重。
    合并顺序保证「归档在前、latest 在后」，同一个键以 latest 为准（更新鲜）。
    """
    root_path = Path(root)
    windows: list[dict[str, Any]] = []
    seen: set[str] = set()

    if day:
        for w in read_archived_day(root_path, day):
            key = window_archive_key(w)
            if key and key in seen:
                continue
            if key:
                seen.add(key)
            windows.append(w)

    latest = root_path / "input-context" / "latest.json"
    try:
        payload = json.loads(latest.read_text(encoding="utf-8")) if latest.is_file() else None
    except (OSError, ValueError):
        payload = None
    for key_name in ("windows", "front_windows"):
        items = (payload or {}).get(key_name) if isinstance(payload, dict) else None
        for w in items or []:
            if not isinstance(w, dict):
                continue
            if day and archive_day_for_window(w) != day:
                continue
            k = window_archive_key(w)
            if k and k in seen:
                continue
            if k:
                seen.add(k)
            windows.append(w)

    windows.sort(key=lambda w: _text(w.get("started_at") or w.get("start")))
    return windows
