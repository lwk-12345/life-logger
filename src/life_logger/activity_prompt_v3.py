"""Opt-in activity facts v3 requests, validation and isolated window archives.

Off leaves the v2 path untouched. No archive is a read source for timeline/ledgers.
Only schema capability refusals retry with json_object; invalid facts stay archived.
"""
from __future__ import annotations

import copy
import datetime
import hashlib
import json
import logging
import math
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Mapping

from life_logger import fact_schema, model_usage
from life_logger.atomic import write_json_atomic

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
UTC8 = datetime.timezone(datetime.timedelta(hours=8))
TEMPLATE_VERSION = "v3.3"
TEMPLATE_PATH = ROOT / "config/activity-prompt-v3.3.md"
# Twelve recent merge candidates plus eight distant topic/time matches, no items.
HISTORY_RECENT_LIMIT = 12
HISTORY_RELATED_LIMIT = 8
HISTORY_MAX_ACTIVITIES = HISTORY_RECENT_LIMIT + HISTORY_RELATED_LIMIT
HISTORY_MAX_CHARS = 12_000
HISTORY_TITLE_MAX_CHARS = 160
HISTORY_NEARBY_SECONDS = 3600


def check_live_configuration(config: Mapping[str, Any]) -> dict[str, Any]:
    raw = config.get("activity_prompt") or {}
    raw = raw if isinstance(raw, Mapping) else {}
    threshold = raw.get("confidence_threshold")
    required = raw.get("version") == "v3" and raw.get("mode") == "live"
    valid = isinstance(threshold, (int, float)) and not isinstance(threshold, bool) and math.isfinite(threshold) and 0 <= threshold <= 1
    if required and not valid:
        raise ValueError("v3 live requires explicit finite confidence_threshold in [0,1]; no default")
    return {"ok": True, "live_requested": required, "confidence_threshold": threshold}


def settings(config: Mapping[str, Any]) -> dict[str, Any]:
    check_live_configuration(config)
    raw = config.get("activity_prompt")
    raw = raw if isinstance(raw, Mapping) else {}
    version = raw.get("version", "v2")
    mode = raw.get("mode", "off")
    return {
        "version": version if version in ("v2", "v3") else "v2",
        "mode": mode if mode in ("off", "shadow", "live") else "off",
        "user_profile": raw.get("user_profile"),
        "user_profile_path": raw.get("user_profile_path", "config/user-profile.md"),
        # No v3 threshold existed. An explicit value is required to enable this gate.
        "confidence_threshold": raw.get("confidence_threshold"),
    }


_SMOKE_BRAND_TOPIC_RE = re.compile(r"紫云|云烟|红塔山|红糖起|贵烟|桂烟|乐途|芒果烟|香烟")
_SMOKE_BUY_TOPIC_RE = re.compile(
    r"(?:买|拿|来|要|打包|补报).{0,8}(?:一包|包|盒|条|那个)?(?:烟|香烟|紫云|云烟|红塔山)"
    r"|(?:紫云|云烟|红塔山).{0,6}(?:硬盒|软包|一包|十块|十三|香烟)"
)


def _terms(text: str) -> set[str]:
    words = set(re.findall(r"[a-z0-9]{2,}", text.lower()))
    for run in re.findall(r"[\u4e00-\u9fff]{2,}", text):
        words.update(run[i:i + 2] for i in range(len(run) - 1))
    if "烟" in text or _SMOKE_BRAND_TOPIC_RE.search(text):
        words.add("__topic_smoke__")
    if _SMOKE_BUY_TOPIC_RE.search(text) or (
        ("烟" in text or _SMOKE_BRAND_TOPIC_RE.search(text))
        and any(k in text for k in ("买", "拿包", "来包", "要包", "打包", "一包", "硬盒", "软包"))
    ):
        words.update(("__topic_smoke_buy_1__", "__topic_smoke_buy_2__", "__topic_smoke_buy_3__"))
    return words - {"这个", "一下", "然后", "什么", "我们", "可以", "一个", "现在", "就是", "还是"}


def _history_time(activity, fallback):
    provenance = activity.get("provenance") or {}
    for value in (provenance.get("last_updated_at_cutoff"), activity.get("reported_at"),
                  activity.get("start_at"), activity.get("occurrence_start")):
        try:
            return datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
        except (TypeError, ValueError, OverflowError):
            continue
    return -1e12 + fallback


def select_history(activities, records, cutoff, lookback) -> list[dict[str, str]]:
    """Bounded merge candidates, preserving complete selected activity identities."""
    text = "\n".join(str(row.get("text") or "") for row in records)
    terms = _terms(text)
    candidates = {}
    for index, activity in enumerate(activities):
        if not isinstance(activity, Mapping):
            continue
        ident = activity.get("activity_id")
        if not isinstance(ident, str) or not ident:
            continue
        stamp = _history_time(activity, index)
        if stamp > cutoff.timestamp():
            continue
        brief = {"activity_id": ident, "title": str(activity.get("title") or "")[:HISTORY_TITLE_MAX_CHARS],
                 "category": str(activity.get("category") or "")[:64],
                 "status": str(activity.get("status") or "unknown")[:32]}
        score = len(_terms(brief["title"]) & terms)
        nearby = lookback.timestamp() - HISTORY_NEARBY_SECONDS <= stamp <= cutoff.timestamp()
        candidates[ident] = (stamp, score, nearby, brief)
    recent = sorted(candidates, key=lambda ident: candidates[ident][0], reverse=True)[:HISTORY_RECENT_LIMIT]
    related = sorted((ident for ident in candidates if ident not in recent and
                      (candidates[ident][1] or candidates[ident][2] or ident in text)),
                     key=lambda ident: (ident in text, candidates[ident][1], candidates[ident][2],
                                        candidates[ident][0]), reverse=True)[:HISTORY_RELATED_LIMIT]
    selected = []
    for ident in recent + related:
        proposed = selected + [candidates[ident][3]]
        if len(json.dumps(proposed, ensure_ascii=False)) <= HISTORY_MAX_CHARS:
            selected = proposed
    return selected


def resolve_references(body, records):
    """Map only valid request-local ordinals; model text can never overwrite ASR."""
    resolved = copy.deepcopy(body)
    sources = {fact_schema.line_id(i): row for i, row in enumerate(records, 1)}
    errors = []
    if not isinstance(resolved, dict) or not isinstance(resolved.get("activities"), list):
        return resolved, errors
    for ai, activity in enumerate(resolved["activities"]):
        if not isinstance(activity, dict) or not isinstance(activity.get("items"), list):
            continue
        for ii, item in enumerate(activity["items"]):
            if not isinstance(item, dict) or not isinstance(item.get("evidence"), list):
                continue
            for ei, evidence in enumerate(item["evidence"]):
                ref = evidence.get(fact_schema.LINE_REFERENCE_FIELD) if isinstance(evidence, dict) else None
                if (not isinstance(evidence, dict) or set(evidence) != {fact_schema.LINE_REFERENCE_FIELD}
                        or not isinstance(ref, str) or ref not in sources):
                    errors.append({"where": f"$.activities[{ai}].items[{ii}].evidence[{ei}]",
                                   "field": fact_schema.LINE_REFERENCE_FIELD, "error": "invalid_line_reference"})
                    continue
                row = sources[ref]
                item["evidence"][ei] = {"source_id": row.get("source_id"), "quote": row.get("text")}
    return resolved, errors


def input_app(row):
    from life_logger import work_apps
    context = work_apps.normalize_context(row.get("input_context"))
    if not context:
        return None
    return {**context, "work_app": context.get("kind") == "dictation" and
            work_apps.is_work_app_bundle(context.get("target_bundle"))}


def prepare_records(records, root):
    """Only v3 reads timestamp-matched app windows; never mutate frozen ASR.

    [2026-11-07] 接入 `app_context.build_window_index()` 的分钟桶索引：
    2026-11-06 有 **27,275** 个窗口，而每个 v3 窗口最多 400 条记录 ——
    逐条线性扫一遍是 ``400 × 27275 ≈ 1.09e7`` 次比较（实测约 12 秒纯 CPU）。
    改成"每天建一次索引、每行查桶"后，语义完全不变，只去掉这层重复扫描。

    兼容性（重要）：测试替身经常把 `context_at` 换成**只收两个参数**的版本
    （`lambda ts, wins: ...`）。带 `index=` 调用会抛 `TypeError`，这里**回退**成两参数调用；
    回退只做一次，第二次仍失败就照常抛出（不吞异常、不无限重试）。
    """
    from life_logger import app_context, work_apps
    rows = copy.deepcopy(records)
    windows: dict[str, Any] = {}
    indices: dict[str, Any] = {}
    build_index = getattr(app_context, "build_window_index", None)
    for row in rows:
        if work_apps.normalize_context(row.get("input_context")) is not None:
            continue
        day = str(row.get("timestamp") or "")[:10]
        if not day:
            continue
        if day not in windows:
            windows[day] = app_context.load_windows(root=Path(root) / "watch-sessions", day=day)
            indices[day] = None
            if build_index is not None and windows[day]:
                try:
                    indices[day] = build_index(windows[day])
                except Exception as _idx_exc:  # noqa: BLE001 - 建索引失败就退回线性扫描
                    logger.warning(
                        "prepare_records: 建窗口索引失败（已退回线性扫描）%s: %s: %s",
                        day, type(_idx_exc).__name__, _idx_exc,
                    )
                    indices[day] = None
        index = indices.get(day)
        if index is not None:
            try:
                context = app_context.context_at(row.get("timestamp"), windows[day], index=index)
            except TypeError:
                # 只收 (moment, windows) 的替身/旧签名 → 回退，语义与旧版完全一致
                context = app_context.context_at(row.get("timestamp"), windows[day])
        else:
            context = app_context.context_at(row.get("timestamp"), windows[day])
        if context:
            row["input_context"] = context
    return rows


#: 「买烟」不是吸烟：买/囤/要 到烟，但整句没有任何"抽/吸"的动作词。
#: 只用于 `fact_type == "smoke"` 的降级（纯文本规则，不问模型、不改金额）。
SMOKE_PURCHASE_RE = re.compile(
    r"买(?:一|两|二|三|几|半|\d+)?(?:包|盒|条|支|袋|根)?(?:烟|香烟|烟草|雪茄|电子烟)"
    r"|要(?:一|两|二|三|几|\d+)?(?:包|盒|条)?(?:烟|香烟)"
    r"|来(?:一|两|二|三|几|\d+)?(?:包|盒|条)(?:烟|香烟)"
    r"|购(?:买|入)(?:一|两|二|三|几|\d+)?(?:包|盒|条)?(?:烟|香烟)"
    r"|买烟"
)
SMOKE_ACTION_RE = re.compile(
    r"抽|吸(?:了|一|两|几|烟)|点(?:了|上|着)?(?:一|两|几|\d+)?(?:根|支)"
    r"|来(?:一|两|几|\d+)(?:根|支)|烟瘾|过瘾|嘬"
)
SMOKE_PURCHASE_REASON = "；买烟属购买或计划，非吸烟事实"
#: 被买烟规则降级过的条目标记（纯内存字段，供合并层识别"这条曾是 smoke"）。
SMOKE_DEMOTED_FLAG = "_v3_smoke_demoted"

#: 对 Agent/AI 编程产品说的话：即便模型误标 `fact_type == "todo"`，也确定性降级为 `other`（工作记录），不进生活待办。
AGENT_OR_WORK_TODO_RE = re.compile(
    r"助手|总控|子系统|(?:[一二三四五六1-6]|\d+)\s*号(?:窗|终端)|(?:六|6)个(?:终端|窗口)"
    r"|定时任务|周期任务|巡检|提示词|prompt|更新台账|planning\s*with\s*files"
    r"|GitHub|Google\s*查|资料库查|官方资料库|Web\s*Codex|Codex|Claude|Antigravity|OpenClaw|OpenDesk|DeepSeek|DeepSick|Alma"
    r"|向我汇报|给我汇报|汇报一下(?:你|什么|一晚上|工作|呗)?|实时汇报|利用好大模型|时间戳.*对比"
    r"|任务千万不要停|任务你自己安排|任务分发与任务验收|难点跟重点.{0,8}总结一下|没有记录下来|未记录下来"
    r"|ASR的模型|跑在GPU上|Sensel|Moss|热量计算器|六个是派|前后端|架构.{0,6}升级|复盘.*热量.*统计"
    r"|测试新的设备|设备的性能和精度|全频段传输",
    re.IGNORECASE,
)
AGENT_TODO_DEMOTED_REASON = "；对Agent/开发工具的指令或工作沟通，非个人生活待办"

# A123: classify addressed assistant turns without treating foreground alone as proof.
_A123_TOOL = re.compile(
    r"Alma|Codex|Claude|Antigravity|OpenClaw|OpenCloud|OpenDesk|DeepSeek|Terminal|终端|AI助手|总控"
    r"|子系统|提示词|定时任务|定时巡检|异常终止|台账|(?:[一二三四五六1-6]号)?窗口.{0,8}(?:任务|验收|分发)|编程|代码|GitHub"
    r"|\bClub\b.{0,12}(?:怎么用|如何用|怎么使用|使用方法)",
    re.I,
)
_A123_TURN = re.compile(
    r"怎么用|如何用|怎么使用|使用方法|帮我|给我|你.{0,5}(?:看|查|做|修)|看一下|还有工作吗|这边都成功了|报告.{0,8}成功"
)
_A123_LIFE = frozenset({"expense", "food", "exercise", "smoke"})

_A123_VAGUE_TITLE_REPLACEMENTS: tuple[tuple[re.Pattern[str], re.Pattern[str], str], ...] = (
    (re.compile(r"^报告事项成功$"), re.compile(r"这边都成功了"), "反馈这边均成功（事项未明）"),
    (re.compile(r"^提及主播与订阅量$"), re.compile(r"订阅量.*SUVID|SUVID.*订阅量"), "谈主播订阅量及SUVID"),
    (re.compile(r"^凌晨感叹状态$"), re.compile(r"今天崩了"), "称今天崩了（指代未明）"),
    (re.compile(r"^询问Club使用方法$"), re.compile(r"Club", re.I), "询问Club使用方式"),
    (re.compile(r"^提及炒米粉并表示[''’]?不吃了[''’]?$"), re.compile(r"还有炒米粉"), "表示不吃炒米粉"),
)

_MERCHANT_DIALOGUE_QUOTE_RE = re.compile(
    r"来一[份个碗包]|要一[份个碗包]|点一[份个碗]|拿[一包个]|少放|微辣|还有什么要加|欢迎光临|结账|买单"
)
_UNADDRESSED_OR_NEGATED_RE = re.compile(
    r"未说明对象|无证据显示.*点单|未证明.*点单|第三方|非本人"
)


def _sanitize_false_merchant_target(activity: dict[str, Any]) -> None:
    """清除因 summary 含“无证据显示…点单”或仅提食物名而被旧规则误标的 speech_target='商家/店员'。"""
    if (
        activity.get("speech_target_source") == "user_correction"
        or activity.get("target_source") == "user_correction"
        or activity.get("user_corrected") is True
    ):
        return
    if str(activity.get("speech_target") or "").strip() != "商家/店员":
        return
    items = [it for it in (activity.get("items") or []) if isinstance(it, Mapping)]
    quotes = " ".join(
        str(e.get("quote") or "")
        for it in items
        for e in (it.get("evidence") or [])
        if isinstance(e, Mapping)
    )
    unc_list = [str(u) for u in (activity.get("uncertainties") or [])]
    for it in items:
        unc_list.extend(str(u) for u in (it.get("uncertainties") or []))
    meta_text = f"{activity.get('summary') or ''} {' '.join(unc_list)}"
    if _UNADDRESSED_OR_NEGATED_RE.search(meta_text) or not _MERCHANT_DIALOGUE_QUOTE_RE.search(quotes):
        activity["speech_target"] = "未知"
        if (
            activity.get("category") == "餐饮·消费"
            and "无证据显示" in str(activity.get("summary") or "")
            and "不吃了" in quotes
        ):
            activity["category"] = "生活·日常"


def _clean_a123_vague_title(activity: dict[str, Any]) -> None:
    if activity.get("title_source") == "user_correction" or activity.get("user_corrected") is True:
        return
    raw_title = str(activity.get("title_original") or activity.get("title") or "").strip()
    if not raw_title:
        return
    quotes = " ".join(
        str(e.get("quote") or "")
        for it in (activity.get("items") or []) if isinstance(it, Mapping)
        for e in (it.get("evidence") or []) if isinstance(e, Mapping)
    )
    for title_re, quote_re, replacement in _A123_VAGUE_TITLE_REPLACEMENTS:
        if title_re.search(raw_title) and quote_re.search(quotes):
            old_title = str(activity.get("title") or "")
            if old_title == raw_title:
                activity["title"] = replacement
            elif old_title.endswith(raw_title):
                prefix = old_title[: len(old_title) - len(raw_title)]
                activity["title"] = prefix + replacement
            else:
                activity["title"] = replacement
            if "title_original" in activity:
                activity["title_original"] = replacement
            break


_CHAT_APP_BUNDLES = frozenset({
    "com.tencent.xinwechat", "com.tencent.weworkmac", "com.tencent.qq",
    "com.bytedance.feishu", "com.alibaba.dingtalkmac",
})
_CHAT_APP_LABELS = frozenset({"微信", "企业微信", "wechat", "wework", "qq", "飞书", "钉钉"})


def _is_chat_context(ctx: Any) -> bool:
    if not isinstance(ctx, Mapping):
        return False
    bundle = str(ctx.get("target_bundle") or ctx.get("target_app") or "").strip().lower()
    label = str(ctx.get("app_label") or "").strip().lower()
    return bool((bundle and bundle in _CHAT_APP_BUNDLES) or (label and label in _CHAT_APP_LABELS))


def assistant_work_item(item, apps=None, activity_context=None) -> bool:
    apps = apps if isinstance(apps, Mapping) else {}
    evidence = item.get("evidence") or []
    quote = " ".join(str(e.get("quote") or "") for e in evidence if isinstance(e, Mapping))
    # A typed physical/financial fact is never erased by its app label or a work keyword.
    if item.get("fact_type") in _A123_LIFE:
        return False
    from life_logger import work_apps
    contexts = [
        apps.get(str(e.get("source_id")), {}) or e.get("input_context") or {}
        for e in evidence if isinstance(e, Mapping)
    ]
    if isinstance(item.get("input_context"), Mapping):
        contexts.append(item["input_context"])
    elif isinstance(activity_context, Mapping):
        contexts.append(activity_context)
    # Speaking into WeChat / chat apps is personal communication, never assistant work.
    if any(_is_chat_context(c) for c in contexts):
        return False
    if _A123_TOOL.search(quote):
        return True
    if any(
        isinstance(c, Mapping)
        and c.get("kind") == "dictation"
        and (
            c.get("work_app") is True
            or work_apps.is_work_app(bundle=c.get("target_bundle"), label=c.get("app_label"))
        )
        for c in contexts
    ):
        return True
    # Front-app + addressed/tool-operation language: two independent cues required.
    return bool(
        _A123_TURN.search(quote)
        and any(
            isinstance(c, Mapping)
            and (
                c.get("work_app") is True
                or work_apps.is_work_app(bundle=c.get("target_bundle"), label=c.get("app_label"))
            )
            for c in contexts
        )
    )


def is_agent_or_work_todo_text(text: str) -> bool:
    return bool(text and AGENT_OR_WORK_TODO_RE.search(text))


def demote_agent_and_work_todos(body: Mapping[str, Any]) -> dict[str, Any]:
    """把对 Agent/编程工具说的 `fact_type == "todo"` 条目确定性降级为 `other`，并应用 A1/A2 确定性修正。"""
    if not isinstance(body, dict):
        return body
    from life_logger import work_apps
    for activity in body.get("activities") or []:
        if not isinstance(activity, dict):
            continue
        _clean_a123_vague_title(activity)
        _sanitize_false_merchant_target(activity)
        cat = str(activity.get("category") or "")
        work_cat = cat.startswith(("工作", "编程", "开发"))
        act_title = str(activity.get("title") or "") + " " + str(activity.get("summary") or "")
        items = [it for it in (activity.get("items") or []) if isinstance(it, dict)]
        flags = [assistant_work_item(it, {}, activity.get("input_context")) for it in items]
        for item, is_assistant_work in zip(items, flags):
            if item.get("fact_type") != "todo":
                continue
            ic = work_apps.normalize_context(item.get("input_context"))
            is_work_dictation = bool(
                ic and ic.get("kind") == "dictation"
                and work_apps.is_work_app(bundle=ic.get("target_bundle"), label=ic.get("app_label"))
            )
            text = f"{act_title} {item_text(item)}".strip()
            if work_cat or is_work_dictation or is_assistant_work or is_agent_or_work_todo_text(text):
                item["fact_type"] = "other"
                if "todo" in item:
                    item["todo"] = None
                reason = str(item.get("reason") or "")
                if (
                    AGENT_TODO_DEMOTED_REASON not in reason
                    and "工作软件语音输入" not in reason
                    and "A123：面向助手的工作沟通" not in reason
                ):
                    item["reason"] = reason + AGENT_TODO_DEMOTED_REASON
        if (
            activity.get("lifecycle") == "live"
            and flags
            and any(flags)
            and not any(it.get("fact_type") in _A123_LIFE or it.get("fact_type") == "todo" for it in items)
        ):
            activity["category"] = "工作·项目"
    for key in ("unassociated_items", "non_confirmed"):
        value = body.get(key)
        if not isinstance(value, list):
            continue
        for holder in value:
            item = holder.get("item") if isinstance(holder, dict) and isinstance(holder.get("item"), dict) else holder
            if not isinstance(item, dict) or item.get("fact_type") != "todo":
                continue
            if is_agent_or_work_todo_text(item_text(item)) or assistant_work_item(item, {}):
                item["fact_type"] = "other"
                if "todo" in item:
                    item["todo"] = None
                reason = str(item.get("reason") or "")
                if AGENT_TODO_DEMOTED_REASON not in reason and "A123：面向助手的工作沟通" not in reason:
                    item["reason"] = reason + AGENT_TODO_DEMOTED_REASON
    return body


def item_text(item: Mapping[str, Any]) -> str:
    """item 的可判定文本：action + 所有 evidence.quote（不含 title，title 是模型结论）。"""
    parts = [str(item.get("action") or "")]
    for evidence in item.get("evidence") or []:
        if isinstance(evidence, Mapping):
            quote = evidence.get("quote")
            if quote:
                parts.append(str(quote))
    return " ".join(parts).strip()


_AI_INSPECTION_CUE_RE = re.compile(
    r"再检查一下|检查一下.{0,24}(?:买|吃|烟|结果|问题|记录|转写)"
    r"|找到蛛丝马迹|蛛丝马迹|查一下.{0,16}(?:有没有|为什么|漏|记录|识别)"
    r"|看一下能(?:不能)?找到|转写成.{1,10}了|改热词|全链路.{0,8}结果"
)
AI_INSPECTION_DEMOTED_REASON = "；对AI/总控下达的系统排查或纠错指令，非个人补报事实"
AI_INSPECTION_DEMOTED_FLAG = "_v3_ai_inspection_demoted"


def _suppress_ai_inspection_false_facts(body: Mapping[str, Any]) -> dict[str, Any]:
    """抑制将对电脑/AI/总控说的系统排查指令（如“再检查一下今天上午是有买一包烟的，你看一下能找到蛛丝马迹吗”）误拆成个人消费/买烟/饮食补报。"""
    if not isinstance(body, dict) or not isinstance(body.get("activities"), list):
        return body
    work_inspection_sids: set[str] = set()
    work_covered_sids: set[str] = set()
    for act in body["activities"]:
        if not isinstance(act, dict):
            continue
        cat = str(act.get("category") or "")
        is_work_cat = cat.startswith(("工作", "编程", "开发"))
        act_quotes = " ".join(
            str(e.get("quote") or "")
            for it in (act.get("items") or []) if isinstance(it, Mapping)
            for e in (it.get("evidence") or []) if isinstance(e, Mapping)
        )
        act_sids = {
            str(e.get("source_id") or "")
            for it in (act.get("items") or []) if isinstance(it, Mapping)
            for e in (it.get("evidence") or []) if isinstance(e, Mapping)
            if e.get("source_id")
        }
        if is_work_cat:
            work_covered_sids.update(act_sids)
            if _AI_INSPECTION_CUE_RE.search(act_quotes):
                work_inspection_sids.update(act_sids)

    kept_activities: list[Any] = []
    for act in body["activities"]:
        if not isinstance(act, dict) or not isinstance(act.get("items"), list):
            kept_activities.append(act)
            continue
        cat = str(act.get("category") or "")
        is_work_cat = cat.startswith(("工作", "编程", "开发"))
        demoted_count = 0
        life_items = [it for it in act["items"] if isinstance(it, dict) and it.get("fact_type") in _A123_LIFE]
        for it in life_items:
            it_quotes = " ".join(
                str(e.get("quote") or "") for e in (it.get("evidence") or []) if isinstance(e, Mapping)
            )
            it_sids = {
                str(e.get("source_id") or "")
                for e in (it.get("evidence") or []) if isinstance(e, Mapping)
                if e.get("source_id")
            }
            # 只在「这条子项自己引用的原话」就是排查指令、且有工作语境时才降级：
            # 要么本活动已归工作类，要么同一句原话已被某条工作排查活动引用。
            # 不按整条活动的拼接原话判断，避免把对店员说的“再检查一下”连带抹掉真实消费。
            if bool(it_sids and (it_sids & work_inspection_sids)) or (
                _AI_INSPECTION_CUE_RE.search(it_quotes) and is_work_cat
            ):
                it["fact_type"] = "other"
                for k in ("expense", "smoke", "food", "exercise"):
                    if k in it:
                        it[k] = None
                reason = str(it.get("reason") or "")
                if AI_INSPECTION_DEMOTED_REASON not in reason:
                    it["reason"] = reason + AI_INSPECTION_DEMOTED_REASON
                it[AI_INSPECTION_DEMOTED_FLAG] = True
                demoted_count += 1
        if demoted_count and not is_work_cat:
            all_act_sids = {
                str(e.get("source_id") or "")
                for it in act["items"] if isinstance(it, Mapping)
                for e in (it.get("evidence") or []) if isinstance(e, Mapping)
                if e.get("source_id")
            }
            if all_act_sids and all_act_sids.issubset(work_covered_sids) and all(
                isinstance(it, Mapping) and it.get("fact_type") == "other" for it in act["items"]
            ):
                continue
            if all(isinstance(it, Mapping) and it.get("fact_type") == "other" for it in act["items"]):
                act["category"] = "工作·项目"
        kept_activities.append(act)
    body["activities"] = kept_activities
    return body


def _strip_internal_item_flags(body: Any) -> None:
    """校验前剥掉程序内部标记（不在 fact_schema 白名单里，留着会整窗 unknown_field）。"""
    if not isinstance(body, dict):
        return
    holders: list[Any] = []
    for act in body.get("activities") or []:
        if isinstance(act, dict):
            holders.extend(act.get("items") or [])
    for key in ("unassociated_items", "non_confirmed"):
        value = body.get(key)
        if isinstance(value, list):
            holders.extend(value)
    for holder in holders:
        item = holder.get("item") if isinstance(holder, dict) and isinstance(holder.get("item"), dict) else holder
        if isinstance(item, dict):
            item.pop(SMOKE_DEMOTED_FLAG, None)
            item.pop(AI_INSPECTION_DEMOTED_FLAG, None)


def demote_smoke_purchases(body: Mapping[str, Any]) -> dict[str, Any]:
    """把 `fact_type == "smoke"` 的单纯计划/孤立「买烟」条目降级为 `other`（浅拷贝后原地改，返回 body）。

    规则（照派单）：
      * 若所属活动是已完成的真实买烟消费（同活动含 `fact_type == "expense"` 且本条
        `smoke.kind == "bought"`、`smoke.count is None`、`status == "completed"`），
        则保留 `fact_type="smoke"`（`kind="bought"`，表示现场买烟事实，不计入已抽支数）；
      * 否则文本（action + evidence.quote）命中买烟表达，**且**没有任何真实抽烟动作词 → 降级；
      * 降级 = `fact_type="other"`、`smoke=None`、`reason` 追加
        「；买烟属购买或计划，非吸烟事实」、打 `_v3_smoke_demoted=True`；
      * 真抽烟（「我已经抽了三根烟了」「刚抽了一根」）**原样保留** `smoke`。
    幂等：已降级过的条目不会再次追加 reason。
    """
    if not isinstance(body, dict):
        return body
    candidates: list[tuple[dict[str, Any] | None, Any]] = []
    for activity in body.get("activities") or []:
        if isinstance(activity, dict):
            for it in activity.get("items") or []:
                candidates.append((activity, it))
    for key in ("unassociated_items", "non_confirmed"):
        value = body.get(key)
        if isinstance(value, list):
            for it in value:
                candidates.append((None, it))
    for parent_act, holder in candidates:
        # non_confirmed 的条目可能是 {"item": {...}} 包装
        item = holder.get("item") if isinstance(holder, dict) and isinstance(holder.get("item"), dict) else holder
        if not isinstance(item, dict) or item.get("fact_type") != "smoke":
            continue
        smoke_obj = item.get("smoke")
        has_sibling_expense = bool(
            isinstance(parent_act, Mapping)
            and any(isinstance(sib, Mapping) and sib.get("fact_type") == "expense" for sib in (parent_act.get("items") or []))
        )
        act_completed = bool(
            isinstance(parent_act, Mapping)
            and (parent_act.get("status") == "completed" or item.get("status") == "completed")
        )
        if (
            has_sibling_expense
            and act_completed
            and isinstance(smoke_obj, Mapping)
            and smoke_obj.get("kind") == "bought"
            and smoke_obj.get("count") is None
        ):
            continue                          # 真实买烟（expense + smoke kind=bought, count=None）保留
        text = item_text(item)
        if not text or not SMOKE_PURCHASE_RE.search(text):
            continue
        if SMOKE_ACTION_RE.search(text):
            continue                          # 整句里确有"抽/吸" → 是真吸烟事实
        item["fact_type"] = "other"
        if "smoke" in item:
            item["smoke"] = None
        reason = str(item.get("reason") or "")
        if SMOKE_PURCHASE_REASON not in reason:
            item["reason"] = reason + SMOKE_PURCHASE_REASON
        item[SMOKE_DEMOTED_FLAG] = True
    return body


def _legacy_app_policy(body, apps):
    """Keep work discussion; do not turn dictated work into personal facts."""
    result = copy.deepcopy(body)
    if not isinstance(result, dict) or not isinstance(result.get("activities"), list):
        return result
    for activity in result["activities"]:
        if not isinstance(activity, dict) or not isinstance(activity.get("items"), list):
            continue
        work_category = str(activity.get("category") or "").startswith(("工作", "编程", "开发"))
        all_work = bool(activity["items"])
        for item in activity["items"]:
            if not isinstance(item, dict):
                all_work = False
                continue
            evidence = item.get("evidence") or []
            flags = [bool(apps.get(e.get("source_id"), {}).get("work_app")) for e in evidence if isinstance(e, dict)]
            all_work = all_work and bool(flags) and all(flags)
            work_item = any(flags) or (work_category and item.get("fact_type") == "todo")
            if work_item and item.get("fact_type") in ("expense", "food", "todo"):
                item["fact_type"] = "other"
                item.update(expense=None, food=None, todo=None)
                item["reason"] = str(item.get("reason") or "") + "；工作软件语音输入，仅作工作记录"
        if all_work:
            activity["category"] = "工作·项目"
    return result


def apply_app_policy(body, apps):
    """Keep work discussion; do not turn dictated work into personal facts, while preserving _A123_LIFE facts."""
    result = _legacy_app_policy(body, apps)
    if not isinstance(result, dict):
        return result
    for original, activity in zip(body.get("activities") or [], result.get("activities") or []):
        if not isinstance(activity, dict) or not isinstance(original, dict):
            continue
        has_life_fact = False
        for old_item, item in zip(original.get("items") or [], activity.get("items") or []):
            if isinstance(old_item, dict) and isinstance(item, dict) and old_item.get("fact_type") in _A123_LIFE:
                item.clear()
                item.update(copy.deepcopy(old_item))
                has_life_fact = True
        if has_life_fact and "category" in original:
            activity["category"] = original["category"]
        if activity.get("lifecycle") != "live":
            continue
        items = [it for it in (activity.get("items") or []) if isinstance(it, dict)]
        flags = []
        for item in items:
            work = assistant_work_item(item, apps, activity.get("input_context"))
            flags.append(work)
            if work and item.get("fact_type") == "todo":
                item.update(fact_type="other", todo=None)
                reason = str(item.get("reason") or "")
                if "A123：面向助手的工作沟通" not in reason and "工作软件语音输入" not in reason:
                    item["reason"] = reason + "；A123：面向助手的工作沟通，不是生活待办"
        if (
            flags
            and any(flags)
            and not any(it.get("fact_type") in _A123_LIFE or it.get("fact_type") == "todo" for it in items)
        ):
            activity["category"] = "工作·项目"
    result = _suppress_ai_inspection_false_facts(result)
    # [2026-11-07] 「买烟」不是吸烟：模型即使照抄提示词也会把"买包烟"标成 smoke，
    # 在政策体上做确定性的文本降级（真抽烟及真实买烟 expense+smoke[kind=bought] 保留）。
    result = demote_agent_and_work_todos(result)
    return demote_smoke_purchases(result)


def build_prompt(config, cutoff, lookback, records, activities) -> str:
    options = settings(config)
    profile = options["user_profile"]
    if not isinstance(profile, str):
        path = Path(options["user_profile_path"])
        if not path.is_absolute():
            path = ROOT / path
        profile = path.read_text(encoding="utf-8")
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    template = template.replace("{title_max_length}", str(fact_schema.STRING_MAX_LENGTHS["activity"]["title"]))
    template = template.replace("{action_max_length}", str(fact_schema.STRING_MAX_LENGTHS["item"]["action"]))
    rows = [{fact_schema.LINE_REFERENCE_FIELD: fact_schema.line_id(index),
             **{key: row.get(key) for key in ("timestamp", "source_label", "speaker", "text")}}
            for index, row in enumerate(records, 1)]
    for row, source in zip(rows, records):
        app = input_app(source)
        if app and app.get("kind") == "dictation":
            row["app"] = app
        elif app:
            row["foreground_reference"] = {k: v for k, v in app.items() if k != "work_app"}
    schema = fact_schema.request_json_schema(fact_schema.line_id(i) for i in range(1, len(records) + 1))
    return "\n\n".join([
        template.replace("{user_profile}", profile),
        "【JSON Schema（必须严格符合）】：\n" + json.dumps(schema, ensure_ascii=False),
        f"【窗口坐标】：lookback={lookback.isoformat()} cutoff={cutoff.isoformat()}",
        "【输入 ASR 记录】：\n" + json.dumps(rows, ensure_ascii=False),
        "【上轮已整理活动（有限合并候选，保留完整id）】：\n" +
        json.dumps(select_history(activities, records, cutoff, lookback), ensure_ascii=False),
    ])


def _supports_error_fallback(text: str) -> bool:
    text = text.lower()
    return any(word in text for word in ("schema", "response_format", "structured output"))


def _mask(value: Any, secret: str) -> Any:
    if isinstance(value, str):
        return value.replace(secret, "[REDACTED]") if secret else value
    if isinstance(value, list):
        return [_mask(item, secret) for item in value]
    if isinstance(value, dict):
        return {key: _mask(item, secret) for key, item in value.items()}
    return value


def input_key(lookback, cutoff, prepared_records) -> str:
    """窗口输入的指纹，归档文件名里已经用它（前 20 位）。

    v3 影子去重也用它：同一 (lookback, cutoff, records) 不再调模型。
    """
    return hashlib.sha256(json.dumps(
        [lookback.isoformat(), cutoff.isoformat(), prepared_records], sort_keys=True,
        ensure_ascii=False, default=str,
    ).encode()).hexdigest()[:20]


def archive_dir(runtime, day: str):
    return runtime.output_root / "activity-shadow-v3" / day


def prior_runs(runtime, day: str, key: str) -> list:
    """该窗口该输入哈希的历史归档（按文件名排序，最后一个最新）。"""
    folder = archive_dir(runtime, day)
    if not folder.is_dir():
        return []
    return sorted(folder.glob(f"*-{key}-*.json"))


def prior_status(runtime, day: str, key: str) -> str | None:
    """上一次同输入运行的状态：valid / invalid / error；没有返回 None。"""
    runs = prior_runs(runtime, day, key)
    if not runs:
        return None
    try:
        payload = json.loads(runs[-1].read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    status = payload.get("status")
    return str(status) if status else None


def record_skip(runtime, day: str, key: str, cutoff, lookback, records_count: int) -> None:
    """记一条「同输入跳过」：jsonl + 计数，便于审计。"""
    folder = archive_dir(runtime, day)
    folder.mkdir(parents=True, exist_ok=True)
    row = {"schema": "activity-v3-skip-v1", "date": day, "key": key,
           "cutoff": cutoff.isoformat(), "lookback": lookback.isoformat(),
           "records": records_count, "trigger": "same_input_skipped",
           "at": datetime.datetime.now(datetime.timezone.utc).isoformat()}
    with (folder / "_skips.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    counts_path = folder / "_counters.json"
    try:
        counts = json.loads(counts_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        counts = {}
    key_name = "same_input_skipped"
    counts[key_name] = int(counts.get(key_name) or 0) + 1
    write_json_atomic(counts_path, counts, indent=1)


def run_window(
    runtime, cutoff, lookback, records, activities,
    *, mode: str, gate, sender: Callable[[dict], dict] | None = None,
    trigger: str = "first",
) -> dict[str, Any]:
    """At most two requests. Preserve every response/body and validation reason."""
    records = prepare_records(records, getattr(runtime, "input_context_root", runtime.output_root))
    day = cutoff.astimezone(UTC8).date().isoformat()
    key = input_key(lookback, cutoff, records)
    archive = runtime.output_root / "activity-shadow-v3" / day / (
        cutoff.astimezone(UTC8).strftime("%H%M%S") + "-" + key + "-" + str(time.time_ns()) + ".json"
    )
    report: dict[str, Any] = {
        "schema": "activity-v3-window-v1", "mode": mode, "date": day, "trigger": trigger,
        "template_version": TEMPLATE_VERSION,
        "cutoff": cutoff.isoformat(), "lookback": lookback.isoformat(),
        "records": len(records), "attempts": [], "schema_fallbacks": 0,
        "status": "started", "result": None,
        "reference_map": {fact_schema.line_id(i): row.get("source_id") for i, row in enumerate(records, 1)},
        "input_apps": {str(row.get("source_id")): input_app(row) for row in records if input_app(row)},
        "history": {"available": len(activities),
                    "selected": len(select_history(activities, records, cutoff, lookback)),
                    "max_activities": HISTORY_MAX_ACTIVITIES, "max_chars": HISTORY_MAX_CHARS},
    }
    secret = ""
    try:
        url, secret, model = runtime.get_deepseek_settings()
        if not secret and sender is None:
            raise RuntimeError("missing_credentials")
        window_config = runtime.load_config()
        confidence_threshold = settings(window_config)["confidence_threshold"]
        if mode == "live":
            check_live_configuration({"activity_prompt": {"version": "v3", "mode": "live",
                                      "confidence_threshold": confidence_threshold}})
        prompt = build_prompt(window_config, cutoff, lookback, records, activities)
        for format_name in ("json_schema", "json_object"):
            response_format: dict[str, Any] = {"type": format_name}
            if format_name == "json_schema":
                response_format["json_schema"] = {
                    "name": "activity_facts_v3", "strict": True,
                    "schema": fact_schema.request_json_schema(report["reference_map"]),
                }
            payload = {
                "model": model, "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.1, "response_format": response_format,
                "max_tokens": runtime.get_deepseek_max_tokens(),
            }
            effort = runtime.get_deepseek_reasoning_effort()
            if effort:
                payload["reasoning_effort"] = effort
            attempt: dict[str, Any] = {"format": format_name}
            report["attempts"].append(attempt)
            started = time.monotonic()
            response = None
            retry = False
            try:
                with gate:
                    if sender is not None:
                        response = sender(copy.deepcopy(payload))
                    else:
                        request = urllib.request.Request(
                            url.rstrip("/") + "/chat/completions",
                            data=json.dumps(payload, ensure_ascii=False).encode(),
                            headers={"Content-Type": "application/json", "Authorization": "Bearer " + secret},
                            method="POST",
                        )
                        with urllib.request.urlopen(request, timeout=runtime.get_deepseek_timeout_s()) as handle:
                            response = json.load(handle)
                attempt["response"] = response
                if not isinstance(response, dict):
                    raise ValueError("response_not_object")
                if response.get("error"):
                    retry = format_name == "json_schema" and _supports_error_fallback(json.dumps(response["error"]))
                    raise ValueError("provider_error")
                message = response.get("choices", [{}])[0].get("message", {})
                if message.get("refusal"):
                    retry = format_name == "json_schema" and _supports_error_fallback(str(message["refusal"]))
                    raise ValueError("provider_refusal")
                content = message.get("content")
                attempt["content"] = content
                body = json.loads(content) if isinstance(content, str) else content
                attempt["body"] = body
                resolved, reference_errors = resolve_references(body, records)
                policy_body = apply_app_policy(resolved, report["input_apps"])
                schema_check_body = copy.deepcopy(policy_body)
                _strip_internal_item_flags(schema_check_body)
                validation = fact_schema.validate_payload(
                    schema_check_body, confidence_threshold=confidence_threshold,
                )
                if reference_errors:
                    validation["errors"] = reference_errors + validation["errors"]
                    validation["ok"] = False
                    validation["confirmed_payload"] = None
                    validation["counts"]["errors"] = len(validation["errors"])
                attempt["validation"] = validation
                report["result"] = policy_body if isinstance(resolved, dict) else resolved
                report["confirmed_result"] = validation.get("confirmed_payload")
                report["non_confirmed"] = validation.get("non_confirmed", [])
                report["confidence_gate"] = validation.get("confidence_gate", {"configured": False, "threshold": None})
                report["validation"] = validation
                report["status"] = "valid" if validation["ok"] else "invalid"
            except urllib.error.HTTPError as exc:
                # Inspect capability errors in memory; never persist request URLs/keys.
                detail = exc.read(8192).decode("utf-8", errors="replace")
                retry = format_name == "json_schema" and exc.code in (400, 422, 501) and _supports_error_fallback(detail)
                attempt["error"] = {"kind": "http_error", "status": exc.code}
            except Exception as exc:
                # No exception text: urllib/parser messages may embed credentials/body.
                attempt["error"] = {"kind": type(exc).__name__}
            finally:
                model_usage.record(
                    "activity_v3_shadow" if mode == "shadow" else "activity_v3_live",
                    payload=response, model=model,
                    elapsed_ms=(time.monotonic() - started) * 1000,
                    ok=attempt.get("validation", {}).get("ok", False),
                    error_kind=("schema_unsupported" if retry else
                                "invalid_output" if report["status"] == "invalid" else
                                "request_error" if attempt.get("error") else None),
                    endpoint_kind="chat.completions",
                    extra={"mode": mode, "schema_fallback": retry},
                )
            if retry:
                report["schema_fallbacks"] += 1
                continue
            if attempt.get("error"):
                report["status"] = "error"
            break
    except Exception as exc:
        report["status"] = "error"
        report["error"] = {"kind": type(exc).__name__}
    # Every invalid item is kept in body/result along with structured error paths.
    report["artifact_path"] = str(archive)
    write_json_atomic(archive, _mask(report, secret), indent=2)
    return report


def retain_publication_error(runtime, report, error) -> None:
    """Record live evidence/time rejection without dropping the raw v3 items."""
    report["publication_validation"] = {
        "ok": False, "error_type": type(error).__name__, "reason": str(error)[:240],
    }
    _, secret, _ = runtime.get_deepseek_settings()
    write_json_atomic(Path(report["artifact_path"]), _mask(report, secret), indent=2)


def project_live(report, records) -> dict[str, Any]:
    """Hydrate v3 references for the existing strict evidence gate; no money inference."""
    if report.get("status") != "valid":
        raise ValueError("Activity v3 output is not valid; retained in isolated archive")
    result = copy.deepcopy(report.get("confirmed_result") or report["result"])
    # Unconfirmed facts use the existing evidence-checked unassociated lane;
    # shadow archives stay isolated and are never a timeline/ledger read source.
    pending = []
    for held in report.get("non_confirmed", []):
        if isinstance(held.get("item"), dict) and held.get("reason") != "no_evidence":
            projected = copy.deepcopy(held["item"])
            if held["reason"] == "time_format":
                bad_fields = {f["field"]: projected.get(f["field"]) for f in held["fields"] if f["error"] == "time_format"}
                projected["v3_original_time_fields"] = bad_fields
                for name in bad_fields:
                    if name in projected:
                        projected[name] = None
            pending.append({**projected, "review_status": "non_confirmed", "fact_schema_version": 3,
                            "v3_lifecycle": held.get("activity", {}).get("lifecycle", "unknown"),
                            "pending_reason": held["reason"], "validation_fields": copy.deepcopy(held["fields"]),
                            "v3_activity_id": held.get("activity_id")})
    if pending:
        result["unassociated_items"] = pending
    by_id: dict[str, list[dict]] = {}
    for row in records:
        by_id.setdefault(str(row.get("source_id")), []).append(row)
    for activity in result["activities"]:
        activity["fact_schema_version"] = 3
    # Hydrate both lanes so held items cannot bypass the verbatim-evidence gate.
    items = [item for activity in result["activities"] for item in activity["items"]]
    items += result.get("unassociated_items", [])
    for item in items:
        spoken = []
        for evidence in item["evidence"]:
            candidates = by_id.get(str(evidence["source_id"]), [])
            if len(candidates) != 1:
                raise ValueError("Unknown or ambiguous v3 source_id")
            row = candidates[0]
            evidence.update({key: row.get(key) for key in ("segment_id", "row_id", "source_label")})
            app = report.get("input_apps", {}).get(str(evidence["source_id"])) or input_app(row)
            if app:
                evidence["input_context"] = app
                evidence["v3_work_dictation"] = bool(app.get("work_app"))
            if row.get("timestamp"):
                spoken.append(row["timestamp"])
        # spoken_at is a source coordinate, never a model guess.
        item["spoken_at"] = min(spoken, key=lambda value: datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))) if spoken else None
        expense = item.get("expense")
        if expense:
            for name in ("amount", "currency", "payment_status"):
                item[name] = expense[name]
    # [2026-11-07] 幂等：修复前落盘的影子报告也在这里被纠正（真抽烟不动）。
    result = demote_smoke_purchases(result)
    return autofill_cigarette_prices(result)


# ---------------------------------------------------------------------------
# 跨窗口日级合并（[2026-11-07]）
# ---------------------------------------------------------------------------
#: 合并时视为"同一件事"的 fact_type 分组：同为这三类的条目，只要 source_id 集合相同就算重复。
MERGE_FACT_GROUPS = {
    "expense": "money",
    "food": "food",
    "smoke": "smoke",
    "todo": "todo",
    "exercise": "body",
    "idea": "idea",
    "other": "other",
    "none": "none",
}


def _item_source_ids(item: Mapping[str, Any]) -> frozenset[str]:
    ids = set()
    for evidence in item.get("evidence") or []:
        if isinstance(evidence, Mapping):
            sid = evidence.get("source_id")
            if sid is not None:
                ids.add(str(sid))
    return frozenset(ids)


def _normalise_action(text: Any) -> str:
    return re.sub(r"[\s，。、,.!?！？；;：:\"'“”‘’()（）]", "", str(text or ""))


def _item_quotes_only(item: Mapping[str, Any]) -> str:
    """只看 evidence 的原话（**不含 action**）：跨窗口的稳定身份。

    为什么不能把 action 算进来：同一句原话在不同窗口被拆成不同 action
    （`买一包烟` / `买包烟`、`下楼丢垃圾` / `下楼丢垃圾后回来睡觉`），
    带上 action 就等于给每条都换了个身份，去重率会变成 0%（上一轮实测）。
    """
    parts = []
    for evidence in item.get("evidence") or []:
        if isinstance(evidence, Mapping):
            quote = evidence.get("quote")
            if quote:
                parts.append(str(quote))
    return _normalise_action(" ".join(parts))


def _item_quote_key(item: Mapping[str, Any]) -> str:
    """跨窗口的稳定身份 = 归一化后的原话（**不是** source_id，见下）。"""
    return _item_quotes_only(item)


def _iter_report_items(body: Mapping[str, Any]):
    """(owner_activity_id, item, lane) —— lane ∈ confirmed / unassociated。"""
    for activity in body.get("activities") or []:
        if not isinstance(activity, dict):
            continue
        aid = str(activity.get("activity_id") or "")
        for item in activity.get("items") or []:
            if isinstance(item, dict):
                yield aid, item, "confirmed"
    for item in body.get("unassociated_items") or []:
        if isinstance(item, dict):
            yield "__unassociated__", item, "unassociated"


#: [2026-11-07 Q1045] 跨活动"接链"用的原话至少要有这么多字（去标点后）。
#: 实测反例：11-06 的「嗯」（1 个字）会把「乒乓球比分」和「玄幻剧情」两个媒体活动硬接在一起。
ACTIVITY_LINK_MIN_QUOTE_CHARS = 8
#: 这些 category 不做**跨 id** 合并：媒体内容本身是"在放什么"，不同节目合并会丢时刻与标题。
ACTIVITY_LINK_SKIP_CATEGORIES = frozenset({"媒体内容"})
#: `lifecycle` 取这些值表示"没判断"，与任何值都兼容（实测同一件事相邻窗口会 live ↔ unknown 抖）。
UNKNOWN_LIFECYCLES = frozenset({"", "unknown", "none", "None"})
#: `unassociated_items` 在合并结果里的伪活动 id。
UNASSOCIATED_ACTIVITY_ID = "__unassociated__"


def fold_runs_by_cutoff(reports) -> list[Mapping[str, Any]]:
    """同一个 `cutoff` 的多次运行只保留**最后一次**（同一输入重跑，不能当跨窗口重复）。

    实测 2026-11-07：47 次 valid 运行其实只有 **38** 个 cutoff；不折叠的话
    活动数会从 109 被灌水到 136（每个重跑都新建一套 `activity_id`）。
    没有 `cutoff` 的报告原样保留，排在最后。
    """
    kept: dict[str, Mapping[str, Any]] = {}
    without: list[Mapping[str, Any]] = []
    for report in reports or []:
        if not isinstance(report, Mapping):
            continue
        cutoff = report.get("cutoff")
        if not cutoff:
            without.append(report)
            continue
        kept[str(cutoff)] = report                 # 入参按时间序 → 后者覆盖前者
    return [kept[c] for c in sorted(kept)] + without


def _item_quote_set(item: Mapping[str, Any]) -> frozenset[str]:
    """条目引用到的**归一化**原话集合（不含 action：action 措辞会被模型改写）。"""
    quotes = set()
    for evidence in item.get("evidence") or []:
        if isinstance(evidence, Mapping) and evidence.get("quote"):
            quotes.add(_normalise_action(evidence["quote"]))
    return frozenset(quotes)


def _scope_compatible(scope_a: tuple[str, str], scope_b: tuple[str, str]) -> bool:
    """两个活动能不能算"同一件事"的作用域判据。

    * `category` **必须相同**（分类不同 = 不是一件事）；
    * `lifecycle` 相同，或**任一侧是 unknown/空**。实测 11-07 03:40 vs 03:50 同一句原话：
      03:40 判 `live`、03:50 判 `unknown` —— 同一件事的 lifecycle 会抖，unknown 是"没判断"。
    """
    cat_a, life_a = scope_a
    cat_b, life_b = scope_b
    if cat_a != cat_b:
        return False
    if life_a == life_b:
        return True
    return life_a in UNKNOWN_LIFECYCLES or life_b in UNKNOWN_LIFECYCLES


#: [2026-11-07 Q1051] live 路径里视为"媒体"的 category（这些**不**做跨 id 合并）。
V3_MEDIA_CATEGORIES = frozenset({"媒体内容", "浏览·视频", "媒体·科技资讯"})
#: 合并后活动上的回查字段（只读，不改 title）。
LIVE_MERGE_FIELDS = ("merged_activity_ids", "merged_windows", "merged_titles")


def _normalise_category(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or ""))


def _is_media_scope(category: Any, lifecycle: Any) -> bool:
    if str(lifecycle or "") == "media":
        return True
    return _normalise_category(category) in {_normalise_category(c) for c in V3_MEDIA_CATEGORIES}


def is_v3_activity(activity: Any) -> bool:
    """活动本身或它的子项带 `fact_schema_version == 3`。"""
    if not isinstance(activity, Mapping):
        return False
    if activity.get("fact_schema_version") == 3:
        return True
    return any(isinstance(item, Mapping) and item.get("fact_schema_version") == 3
               for item in activity.get("items") or [])


def _item_merge_key(item: Mapping[str, Any]) -> tuple:
    """条目身份 = (归一化原话, fact_type 分组)；没有原话时退回 source_id 集合。"""
    fact = str(item.get("fact_type") or "none")
    group = MERGE_FACT_GROUPS.get(fact, fact)
    quotes = _item_quote_set(item)
    if quotes:
        return ("q", frozenset(quotes), group)
    sources = _item_source_ids(item)
    if sources:
        return ("s", sources, group)
    return ("n", id(item))


def merge_live_v3_activities(activities, incoming_activities=None) -> list[dict[str, Any]]:
    """**live 增量**路径的 v3 活动级合并（规则 A/B/C），挂在 `apply_window_update` 之后。

    为什么需要：`apply_window_update` 只按**相同** `activity_id` 合并，而 v3 在相邻窗口
    经常给同一件事起不同 id（11-07 03:40 `act_throw_trash_buy_cigs_sleep_01` vs
    03:50 `act_plan_trash_cigarette_sleep_01`）。不接这一步，切 live 后
    `day_payload["activities"]` 会攒出跨窗重复活动和重复子项。

    规则与 `merge_v3_window_reports()` 完全一致：
      * **A**：同 `activity_id`（`apply_window_update` 已按 id 合并，这里再做一次条目去重，
        把"同一句、不同措辞"的旧 item 收掉）；
      * **B**：跨 id 但共享 ≥ ``ACTIVITY_LINK_MIN_QUOTE_CHARS``(8) 字归一化原话、
        `category` 归一化后相同、非媒体（不是 `V3_MEDIA_CATEGORIES` 且 `lifecycle != "media"`）、
        `lifecycle` 相同或任一侧 unknown → 合为一个活动；
      * **C**：组内按 ``(归一化原话, fact_type 分组)`` 去重，**保留最新一轮**（`incoming_activities`
        里出现过的键优先，其次是列表靠后的那个）；条目被全部收掉的空活动移除。

    边界：
      * **只处理 v3 活动**（`is_v3_activity()`）；v2 活动原样留在原位、不参与任何合并 ⇒ 对 v2 零影响；
      * 元信息（`title`/`category`/`speech_target`/`lifecycle`/`status`/`fact_schema_version`…）取
        **最新**成员（优先 `incoming_activities` 里的那个），**不拼接标题**；
      * 条目整体 deepcopy，`identity_safe`/`provenance`/`occurred_at`/`spoken_at`/`source_label`
        等字段原样保留；
      * 纯函数：不改入参，不读盘、不写盘。
    """
    # 先把整份活动复制一份并在**去重之前**跑「买烟不是吸烟」的降级：
    # 否则 `买包烟` 还是 smoke、`回来睡觉` 是 other，两者 fact 分组不同 ⇒ 同一句会留两条。
    rows = [copy.deepcopy(a) if isinstance(a, Mapping) else a for a in (activities or [])]
    demote_smoke_purchases({"activities": [a for a in rows if isinstance(a, dict)]})
    incoming_list = [a for a in (incoming_activities or []) if isinstance(a, Mapping)]
    incoming_ids = {str(a.get("activity_id")) for a in incoming_list if a.get("activity_id")}
    incoming_keys = {_item_merge_key(item)
                     for a in incoming_list for item in (a.get("items") or [])
                     if isinstance(item, Mapping)}

    positions = [i for i, a in enumerate(rows) if is_v3_activity(a)]
    if not positions:
        return [copy.deepcopy(a) if isinstance(a, Mapping) else a for a in rows]

    parent = {i: i for i in positions}

    def find(node: int) -> int:
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    scope: dict[int, tuple[str, str]] = {}
    quote_to_positions: dict[str, set[int]] = {}
    for index in positions:
        activity = rows[index]
        scope[index] = (_normalise_category(activity.get("category")),
                        str(activity.get("lifecycle") or ""))
        for item in activity.get("items") or []:
            if not isinstance(item, Mapping):
                continue
            for quote in _item_quote_set(item):
                if len(quote) >= ACTIVITY_LINK_MIN_QUOTE_CHARS:
                    quote_to_positions.setdefault(quote, set()).add(index)

    for linked in quote_to_positions.values():
        if len(linked) < 2:
            continue
        ordered = sorted(linked)
        for i in range(len(ordered)):
            for j in range(i + 1, len(ordered)):
                a, b = ordered[i], ordered[j]
                if a == b:
                    continue
                if _is_media_scope(*scope[a]) or _is_media_scope(*scope[b]):
                    continue
                cat_a, life_a = scope[a]
                cat_b, life_b = scope[b]
                if cat_a != cat_b:
                    continue
                if life_a != life_b and life_a not in UNKNOWN_LIFECYCLES and life_b not in UNKNOWN_LIFECYCLES:
                    continue
                union(a, b)

    groups: dict[int, list[int]] = {}
    for index in positions:
        groups.setdefault(find(index), []).append(index)

    merged_by_root: dict[int, dict[str, Any]] = {}
    for root, members in groups.items():
        members.sort()
        copies = [copy.deepcopy(rows[i]) for i in members]
        latest = copies[-1]
        for copied, index in zip(copies, members):
            if str(rows[index].get("activity_id")) in incoming_ids:
                latest = copied
        merged = {k: v for k, v in latest.items() if k != "items"}
        # 规则 C：保留**最新一轮**的那份；位置取首次出现的顺序。
        # "最新"的判据：这一轮的 item 一定赢过旧窗口的同键 item（`incoming_keys`），
        # 同属旧窗口时列表靠后的赢（live 里旧活动按落盘顺序在前面）。
        order: list[tuple] = []
        slot: dict[tuple, dict[str, Any]] = {}
        slot_is_incoming: dict[tuple, bool] = {}
        for copied in copies:
            for item in copied.get("items") or []:
                if not isinstance(item, Mapping):
                    continue
                key = _item_merge_key(item)
                is_incoming = key in incoming_keys
                if key not in slot:
                    order.append(key)
                elif slot_is_incoming[key] and not is_incoming:
                    continue                            # 旧窗口的副本不许覆盖本轮
                slot[key] = copy.deepcopy(dict(item))
                slot_is_incoming[key] = is_incoming
        items = [slot[key] for key in order]
        if not items:
            continue                                   # 条目被收空 → 活动移除
        cancelled_items = [
            it for it in items
            if isinstance(it, dict) and str(it.get("status") or "") in ("cancelled", "canceled")
        ]
        if cancelled_items:
            for it in items:
                if not isinstance(it, dict) or it in cancelled_items or it.get("fact_type") != "expense":
                    continue
                exp_obj = it.get("expense") if isinstance(it.get("expense"), Mapping) else {}
                exp_name = str(exp_obj.get("item") or "").strip()
                if not exp_name:
                    continue
                for c_it in cancelled_items:
                    c_text = f"{c_it.get('action') or ''} {c_it.get('reason') or ''}"
                    if exp_name in c_text and any(w in c_text for w in ("未买", "未购买", "没有买", "没买", "否认", "放弃")):
                        it["fact_type"] = "other"
                        it["expense"] = None
                        it["status"] = "cancelled"
                        it["correction_of"] = c_it.get("item_id")
                        break
        merged["items"] = items
        merged["activity_id"] = latest.get("activity_id") or copies[-1].get("activity_id")
        merged["merged_activity_ids"] = sorted({str(c.get("activity_id")) for c in copies})
        merged["merged_windows"] = []                  # live 路径没有 cutoff；保留字段形状
        titles: list[str] = []
        for copied in copies:
            title = copied.get("title")
            if title and title not in titles:
                titles.append(title)
        merged["merged_titles"] = titles
        merged_by_root[root] = merged

    belongs = {index: root for root, members in groups.items() for index in members}
    first_position = {root: min(members) for root, members in groups.items()}
    out: list[Any] = []
    for index, activity in enumerate(rows):
        if index not in belongs:
            out.append(copy.deepcopy(activity) if isinstance(activity, Mapping) else activity)
            continue
        root = belongs[index]
        if index != first_position[root] or root not in merged_by_root:
            continue                                   # 合并组的非首位 / 空活动 → 跳过
        out.append(merged_by_root[root])
    return _backfill_unpriced_v3_expenses(out)


_NEW_PURCHASE_CUE_RE = re.compile(r"又买|再买|另外买|第二[瓶份杯个包]")
_PAYMENT_BROADCAST_CUE_RE = re.compile(r"解锁手机|碰一下|支付|付款|收款|结账|买单")
_RETROSPECTIVE_BACKREPORT_CUE_RE = re.compile(
    r"今天(?:上午|早上|早些时候|中午|下午)?|上午|早上|刚才|之前|稍早|补报|没带手表"
)
CIGARETTE_PRICE_TABLE_PATH = ROOT / "config" / "cigarette-prices.json"


def _is_smoke_purchase_text(text: str) -> bool:
    return bool(text and ("烟" in text or _SMOKE_BRAND_TOPIC_RE.search(text)))


def load_cigarette_price_table(path: Path | str | None = None) -> dict[str, Any]:
    """从本地 JSON 文件加载香烟价格表（读数据路径严格禁止任何联网请求）。"""
    target = Path(path) if path is not None else CIGARETTE_PRICE_TABLE_PATH
    if not target.is_file():
        return {"schema": "cigarette-price-table-v1", "brands": []}
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"schema": "cigarette-price-table-v1", "brands": []}
    if not isinstance(data, dict) or not isinstance(data.get("brands"), list):
        return {"schema": "cigarette-price-table-v1", "brands": []}
    return data


def _match_cigarette_brand(
    text: str,
    smoke_brand: str | None,
    price_table: Mapping[str, Any],
) -> dict[str, Any] | None:
    """在本地香烟价格表中匹配品牌（优先匹配 smoke.brand，其次按别名长度降序匹配文本）。"""
    brands = [b for b in (price_table.get("brands") or []) if isinstance(b, Mapping)]
    if not brands:
        return None
    candidates: list[tuple[int, str, dict[str, Any]]] = []
    for entry in brands:
        canonical = str(entry.get("canonical_brand") or "").strip()
        aliases = [str(a).strip() for a in (entry.get("aliases") or []) if str(a).strip()]
        if canonical and canonical not in aliases:
            aliases.append(canonical)
        for alias in aliases:
            candidates.append((len(alias), alias, dict(entry)))
    candidates.sort(key=lambda item: -item[0])

    if smoke_brand:
        sb = str(smoke_brand).strip()
        for _, alias, entry in candidates:
            if alias in sb or sb in alias:
                return entry
    haystack = str(text or "")
    for _, alias, entry in candidates:
        if alias and alias in haystack:
            return entry
    return None


def autofill_cigarette_prices(
    payload_or_activities: dict[str, Any] | list[Any],
    price_table: Mapping[str, Any] | None = None,
) -> Any:
    """按本地价格表为真实买烟活动自动填充/标注烟价（读路径零联网）。

    规则：
    1. 原话已直报金额（spoken_quote）：保留原话金额，标注“你报过”；
    2. 金额为空且命中用户报过/确认过的同款品牌（user_reported / user_confirmed，confirmed=True）：
       直接填入该品牌价格，标注来源（“你上次买同款 X 元” 或 “用户确认价 X 元（你确认过的价）”）；
    3. 金额为空且命中未确认品牌（reference_unconfirmed，confirmed=False）：
       绝不写入已确认 amount（保持 amount=None, payment_status="unknown"），
       仅写入 reference_amount 并标注“参考价，待你确认”，同时提供 review_actions=["confirm_amount", "edit_amount"]。
    """
    table = price_table if isinstance(price_table, Mapping) else load_cigarette_price_table()
    if isinstance(payload_or_activities, dict):
        activities = payload_or_activities.get("activities") or []
    elif isinstance(payload_or_activities, list):
        activities = payload_or_activities
    else:
        return payload_or_activities

    for act in activities:
        if not isinstance(act, dict) or act.get("lifecycle") != "live":
            continue
        if _is_media_scope(act.get("category"), act.get("lifecycle")):
            continue
        items = [it for it in (act.get("items") or []) if isinstance(it, dict)]
        smoke_brand = None
        has_bought_smoke_item = False
        for it in items:
            if it.get("fact_type") == "smoke" and isinstance(it.get("smoke"), Mapping):
                if it["smoke"].get("kind") == "bought":
                    has_bought_smoke_item = True
                    if it["smoke"].get("brand"):
                        smoke_brand = str(it["smoke"]["brand"])

        for item in items:
            if item.get("fact_type") != "expense" or not isinstance(item.get("expense"), dict):
                continue
            if str(item.get("status") or act.get("status") or "") in ("planned", "cancelled", "canceled", "unknown"):
                continue
            exp = item["expense"]
            quotes = " ".join(
                str(ev.get("quote") or "")
                for ev in (item.get("evidence") or [])
                if isinstance(ev, Mapping)
            )
            combined_text = f"{exp.get('item') or ''} {item.get('action') or ''} {act.get('title') or ''} {quotes}"
            if not (has_bought_smoke_item or _is_smoke_purchase_text(combined_text)):
                continue

            brand_entry = _match_cigarette_brand(combined_text, smoke_brand, table)
            existing_src = exp.get("amount_source") or item.get("amount_source")
            raw_amt = exp.get("amount")

            # Case 1: 原话已带金额（且非之前自动回填的参考价/确认价）
            if raw_amt is not None and not (
                isinstance(existing_src, Mapping)
                and existing_src.get("source_type") in ("user_reported", "user_confirmed", "reference_unconfirmed")
            ):
                if brand_entry is not None:
                    src_info = {
                        "source_type": "spoken_quote",
                        "canonical_brand": brand_entry["canonical_brand"],
                        "amount": raw_amt,
                        "currency": exp.get("currency") or "CNY",
                        "confirmed": True,
                        "label": f"你报过（当场直报 {raw_amt} 元；{brand_entry.get('label', '')}）",
                        "short_label": "你报过",
                        "detail": brand_entry.get("detail", ""),
                    }
                else:
                    src_info = {
                        "source_type": "spoken_quote",
                        "canonical_brand": None,
                        "amount": raw_amt,
                        "currency": exp.get("currency") or "CNY",
                        "confirmed": True,
                        "label": f"你报过（本人口头直报 {raw_amt} 元）",
                        "short_label": "你报过",
                        "detail": "本人在录音原话中直接报出买烟金额",
                    }
                exp["amount_source"] = copy.deepcopy(src_info)
                item["amount_source"] = copy.deepcopy(src_info)
                item["amount"] = raw_amt
                continue

            # Case 2 & 3: 现场未报金额（或已由价格表自动回填），查本地价格表
            if brand_entry is None:
                continue

            table_amt = brand_entry.get("amount")
            table_curr = str(brand_entry.get("currency") or "CNY")
            is_confirmed = bool(brand_entry.get("confirmed"))
            source_type = str(brand_entry.get("source_type") or "")

            if is_confirmed and table_amt is not None:
                exp["amount"] = table_amt
                exp["currency"] = table_curr
                item["amount"] = table_amt
                item["currency"] = table_curr
                short_label = "你确认过的价" if source_type == "user_confirmed" else "你报过"
                src_info = {
                    "source_type": source_type,
                    "canonical_brand": brand_entry["canonical_brand"],
                    "amount": table_amt,
                    "currency": table_curr,
                    "confirmed": True,
                    "label": str(brand_entry.get("label") or short_label),
                    "short_label": short_label,
                    "detail": str(brand_entry.get("detail") or ""),
                }
                exp["amount_source"] = copy.deepcopy(src_info)
                item["amount_source"] = copy.deepcopy(src_info)
            else:
                # 未确认参考价：绝不当作已确认金额，保留 amount=None, payment_status="unknown"
                exp["amount"] = None
                item["amount"] = None
                exp["payment_status"] = "unknown"
                item["payment_status"] = "unknown"
                exp["reference_amount"] = table_amt
                item["reference_amount"] = table_amt
                actions = list(brand_entry.get("review_actions") or ["confirm_amount", "edit_amount"])
                src_info = {
                    "source_type": "reference_unconfirmed",
                    "canonical_brand": brand_entry["canonical_brand"],
                    "amount": table_amt,
                    "currency": table_curr,
                    "confirmed": False,
                    "label": str(brand_entry.get("label") or "参考价，待你确认"),
                    "short_label": "参考价待确认",
                    "detail": str(brand_entry.get("detail") or ""),
                    "reference_url": brand_entry.get("reference_url"),
                    "queried_at": brand_entry.get("queried_at"),
                    "review_actions": actions,
                }
                exp["amount_source"] = copy.deepcopy(src_info)
                item["amount_source"] = copy.deepcopy(src_info)
                item["review_actions"] = list(actions)
                act["review_actions"] = list(actions)

    return payload_or_activities


def _backfill_unpriced_v3_expenses(activities: list[Any]) -> list[Any]:
    """将同日后续补报的商品价格（如“可乐是一块八”）或同日买烟事后补报（如“今天上午买了一包烟”）回填合并到原现场购买子项，消除重复入账。"""
    targets: list[tuple[dict[str, Any], dict[str, Any], str]] = []
    for act in activities:
        if not isinstance(act, dict) or not is_v3_activity(act) or act.get("lifecycle") != "live":
            continue
        for item in act.get("items") or []:
            if not isinstance(item, dict) or item.get("fact_type") != "expense":
                continue
            if str(item.get("status") or "") != "completed":
                continue
            exp = item.get("expense")
            if not isinstance(exp, dict):
                continue
            t_name = str(exp.get("item") or "").strip()
            if len(t_name) >= 2:
                targets.append((act, item, t_name))
    if not targets:
        return activities

    try:
        from life_logger.expense_policy import _extract_amount
    except Exception:
        _extract_amount = None  # type: ignore[assignment]

    absorbed_items: set[int] = set()
    for t_idx, (t_act, t_item, t_name) in enumerate(targets):
        if id(t_item) in absorbed_items:
            continue
        t_exp = t_item["expense"]
        t_amt = t_exp.get("amount")
        t_id = str(t_item.get("item_id") or "")
        t_is_smoke = _is_smoke_purchase_text(f"{t_name} {t_item.get('action') or ''} {t_act.get('title') or ''}")

        for s_act in activities:
            if not isinstance(s_act, dict) or _is_media_scope(s_act.get("category"), s_act.get("lifecycle")):
                continue
            for s_item in s_act.get("items") or []:
                if not isinstance(s_item, dict) or s_item is t_item or id(s_item) in absorbed_items:
                    continue
                if str(s_item.get("status") or "") in ("cancelled", "canceled"):
                    continue
                s_ev = [e for e in (s_item.get("evidence") or []) if isinstance(e, Mapping)]
                if not any(e.get("source_label") == "self" for e in s_ev):
                    continue
                s_quotes = " ".join(str(e.get("quote") or "") for e in s_ev)
                if _NEW_PURCHASE_CUE_RE.search(s_quotes):
                    continue

                matched_amt: float | int | None = None
                matched_ev: list[Any] = []
                absorb_src = False
                smoke_backreport_merged = False

                s_ref = str(s_item.get("restatement_of") or s_item.get("correction_of") or "")
                is_explicit_ref = bool(t_id and s_ref == t_id)

                if s_item.get("fact_type") == "expense" and isinstance(s_item.get("expense"), Mapping):
                    s_exp = s_item["expense"]
                    s_name = str(s_exp.get("item") or "").strip()
                    s_raw_amt = s_exp.get("amount")
                    name_match = len(s_name) >= 2 and (s_name in t_name or t_name in s_name)
                    s_meta_text = f"{s_act.get('title') or ''} {s_item.get('action') or ''} {s_quotes}"
                    s_is_smoke_backreport = (
                        t_is_smoke
                        and _is_smoke_purchase_text(f"{s_name} {s_item.get('action') or ''}")
                        and bool(_RETROSPECTIVE_BACKREPORT_CUE_RE.search(s_meta_text))
                    )
                    if s_raw_amt is not None and (
                        is_explicit_ref
                        or ((name_match or s_is_smoke_backreport) and (t_amt is None or t_amt == s_raw_amt))
                    ):
                        try:
                            val = float(s_raw_amt)
                            if val > 0 and math.isfinite(val):
                                matched_amt = int(val) if val == int(val) else round(val, 2)
                                matched_ev = list(s_item.get("evidence") or [])
                                absorb_src = True
                                smoke_backreport_merged = s_is_smoke_backreport
                        except (TypeError, ValueError):
                            pass
                    elif s_raw_amt is None and (is_explicit_ref or s_is_smoke_backreport):
                        matched_ev = list(s_item.get("evidence") or [])
                        absorb_src = True
                        smoke_backreport_merged = True
                    if smoke_backreport_merged and not _SMOKE_BRAND_TOPIC_RE.search(t_name) and _SMOKE_BRAND_TOPIC_RE.search(s_name):
                        t_exp["item"] = s_name
                        t_name = s_name
                elif (
                    t_amt is None
                    and s_item.get("fact_type") == "other"
                    and _extract_amount is not None
                    and (is_explicit_ref or (t_name in str(s_item.get("action") or "") and any(k in str(s_item.get("action") or "") for k in ("价格", "多少钱", "元", "块"))))
                ):
                    for ev in s_ev:
                        if ev.get("source_label") != "self":
                            continue
                        q = str(ev.get("quote") or "")
                        if t_name in q:
                            parsed = _extract_amount(q)
                            if parsed is not None and parsed > 0:
                                val = float(parsed)
                                matched_amt = int(val) if val == int(val) else round(val, 2)
                                matched_ev = [copy.deepcopy(dict(ev))]
                                break

                if matched_amt is not None or smoke_backreport_merged:
                    if matched_amt is not None:
                        t_exp["amount"] = matched_amt
                        t_item["amount"] = matched_amt
                        t_amt = matched_amt
                    existing_sids = {str(e.get("source_id") or "") for e in (t_item.get("evidence") or []) if isinstance(e, Mapping)}
                    for ev in matched_ev:
                        if isinstance(ev, Mapping) and str(ev.get("source_id") or "") not in existing_sids:
                            t_item.setdefault("evidence", []).append(copy.deepcopy(dict(ev)))
                            existing_sids.add(str(ev.get("source_id") or ""))
                    if matched_amt is not None:
                        all_t_quotes = " ".join(str(e.get("quote") or "") for e in (t_item.get("evidence") or []) if isinstance(e, Mapping))
                        s_paid = isinstance(s_item.get("expense"), Mapping) and s_item["expense"].get("payment_status") == "paid"
                        if t_exp.get("payment_status") in (None, "", "unknown") and (s_paid or _PAYMENT_BROADCAST_CUE_RE.search(all_t_quotes) or "是" in s_quotes or "花" in s_quotes or "块" in s_quotes or "元" in s_quotes):
                            t_exp["payment_status"] = "paid"
                            t_item["payment_status"] = "paid"
                        amt_label = f"{matched_amt}元"
                        if "（金额未录清）" in str(t_item.get("action") or ""):
                            t_item["action"] = str(t_item["action"]).replace("（金额未录清）", f"（{amt_label}）")
                        if "（金额未录清）" in str(t_act.get("summary") or ""):
                            t_act["summary"] = str(t_act["summary"]).replace("（金额未录清）", f"（{amt_label}）")
                    if smoke_backreport_merged:
                        t_smoke_item = next(
                            (it for it in (t_act.get("items") or []) if isinstance(it, dict) and it.get("fact_type") == "smoke" and id(it) not in absorbed_items),
                            None,
                        )
                        for sib in s_act.get("items") or []:
                            if not isinstance(sib, dict) or sib is t_smoke_item or id(sib) in absorbed_items:
                                continue
                            if sib.get("fact_type") == "smoke" and isinstance(sib.get("smoke"), Mapping) and sib["smoke"].get("kind") == "bought":
                                if t_smoke_item is not None and isinstance(t_smoke_item.get("smoke"), dict):
                                    if not t_smoke_item["smoke"].get("brand") and sib["smoke"].get("brand"):
                                        t_smoke_item["smoke"]["brand"] = sib["smoke"].get("brand")
                                    ts_sids = {str(e.get("source_id") or "") for e in (t_smoke_item.get("evidence") or []) if isinstance(e, Mapping)}
                                    for ev in sib.get("evidence") or []:
                                        if isinstance(ev, Mapping) and str(ev.get("source_id") or "") not in ts_sids:
                                            t_smoke_item.setdefault("evidence", []).append(copy.deepcopy(dict(ev)))
                                            ts_sids.add(str(ev.get("source_id") or ""))
                                absorbed_items.add(id(sib))
                    if absorb_src:
                        absorbed_items.add(id(s_item))
                        s_aid = str(s_act.get("activity_id") or "")
                        if s_aid and s_act is not t_act:
                            m_ids = list(t_act.get("merged_activity_ids") or [str(t_act.get("activity_id") or "")])
                            if s_aid not in m_ids:
                                m_ids.append(s_aid)
                            t_act["merged_activity_ids"] = sorted(set(m_ids))
                            s_title = str(s_act.get("title") or "")
                            if s_title:
                                m_titles = list(t_act.get("merged_titles") or [str(t_act.get("title") or "")])
                                if s_title not in m_titles:
                                    m_titles.append(s_title)
                                t_act["merged_titles"] = m_titles

    if absorbed_items:
        cleaned_activities: list[Any] = []
        for act in activities:
            if not isinstance(act, dict) or not isinstance(act.get("items"), list):
                cleaned_activities.append(act)
                continue
            kept_items = [it for it in act["items"] if id(it) not in absorbed_items]
            if not kept_items:
                continue
            act["items"] = kept_items
            cleaned_activities.append(act)
        return autofill_cigarette_prices(cleaned_activities)
    return autofill_cigarette_prices(activities)


def merge_v3_window_reports(reports) -> dict[str, Any]:
    """把多个 v3 窗口快照合并成"一天"的活动层（同 cutoff 折叠 + 活动级去重）。

    为什么需要：
      * v3 影子是**按窗口**产出的，同一句原话会在相邻 cutoff 各生成一轮条目，措辞还会变；
      * 同一 `cutoff` 还可能被**重跑**多次（同一输入，每次新建 `activity_id`）。

    **去重键为什么不是 source_id**：`source_id` 是**窗口内 prepared_records 的编号**，
    实测 2026-11-07 折叠后仍有 94.7% 的唯一原话在不同窗口拿到不同 `source_id`
    （同一句「下楼丢个垃圾…」03:40 是 `src_d635310ff77611f6`、03:50 是 `src_6a1f6589707830e9`）。
    所以主键用**归一化原话**，`source_id` 只在"没有原话"时兜底。

    规则（[2026-11-07 Q1045] A/B/C）：
      0. **同 cutoff 折叠**：`fold_runs_by_cutoff()`，每个 cutoff 只留最后一次；
      1. 每条目先过 `demote_smoke_purchases()`（幂等）——「买烟」不算吸烟；
      2. **规则 A（同 id）**：同一 `activity_id` 出现在多个窗口 → 同一个活动，
         元信息（title/category/status/lifecycle…）取**最后**窗口版本（不拼接标题）；
      3. **规则 B（跨 id 接链）**：不同 `activity_id`，但 `category` 相同、
         `lifecycle` 相同或任一侧 unknown、至少共享 1 条**实质原话**
         （去标点后 ≥ ``ACTIVITY_LINK_MIN_QUOTE_CHARS`` 字）、且 `category` 不在
         ``ACTIVITY_LINK_SKIP_CATEGORIES`` → 并查集并为一组（链式传递）；
      4. **规则 C（条目归属）**：全局按 ``(原话集合, fact_type 分组)`` 只保留**最后窗口**
         那一份，早窗口的重复条目丢弃；条目被全部丢空的活动移除。
         即使两个活动因为 scope 不同**不该合并**，同一句话也不会在两个活动里各记一遍。

    返回：``{"schema_version": 3, "cutoff": 最后一窗 cutoff, "windows": n, "activities": [...],
    "items": 总条目数, "merge_rule": {...}}``；每个合并后活动额外带
    ``merged_activity_ids`` / ``merged_windows`` / ``merged_titles``（只读回查，不改 title）。
    纯函数：不改入参（内部 deepcopy），不读盘、不写盘。
    """
    ordered = fold_runs_by_cutoff([r for r in (reports or []) if isinstance(r, Mapping)])
    ordered.sort(key=lambda r: str(r.get("cutoff") or "~"))
    folded = len([r for r in (reports or []) if isinstance(r, Mapping)]) - len(ordered)

    # ---- 收集 (window_index, cutoff, activity_id, activity) ----
    records: list[tuple[int, str, str, dict[str, Any]]] = []
    for window_index, report in enumerate(ordered):
        body = report.get("confirmed_result") or report.get("result") or report
        if not isinstance(body, Mapping):
            continue
        body = copy.deepcopy(dict(body))
        demote_smoke_purchases(body)
        cutoff = str(report.get("cutoff") or "")
        for activity in body.get("activities") or []:
            if not isinstance(activity, dict):
                continue
            aid = str(activity.get("activity_id") or f"w{window_index}-{len(records)}")
            activity["activity_id"] = aid
            records.append((window_index, cutoff, aid, activity))
        pending = [i for i in (body.get("unassociated_items") or []) if isinstance(i, dict)]
        if pending:
            records.append((window_index, cutoff, UNASSOCIATED_ACTIVITY_ID,
                            {"activity_id": UNASSOCIATED_ACTIVITY_ID, "items": pending}))

    # ---- 规则 A（同 id 天然一组）+ 规则 B（跨 id 接链）----
    parent: dict[str, str] = {}

    def find(node: str) -> str:
        parent.setdefault(node, node)
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    scope: dict[str, tuple[str, str]] = {}
    quote_to_ids: dict[str, set[str]] = {}
    for _, _, aid, activity in records:
        find(aid)
        scope[aid] = (str(activity.get("category") or ""), str(activity.get("lifecycle") or ""))
        for item in activity.get("items") or []:
            for quote in _item_quote_set(item):
                if len(quote) >= ACTIVITY_LINK_MIN_QUOTE_CHARS:
                    quote_to_ids.setdefault(quote, set()).add(aid)

    for ids in quote_to_ids.values():
        if len(ids) < 2:
            continue
        ordered_ids = sorted(ids)
        for i in range(len(ordered_ids)):
            for j in range(i + 1, len(ordered_ids)):
                a, b = ordered_ids[i], ordered_ids[j]
                if a == b:
                    continue
                if scope[a][0] in ACTIVITY_LINK_SKIP_CATEGORIES or scope[b][0] in ACTIVITY_LINK_SKIP_CATEGORIES:
                    continue
                if _scope_compatible(scope[a], scope[b]):
                    union(a, b)

    # ---- 分组（规则 A + B 的结果）----
    groups: dict[str, list[tuple[int, str, str, dict[str, Any]]]] = {}
    for record in records:
        groups.setdefault(find(record[2]), []).append(record)

    # ---- 规则 C：条目归属（两级，且**不跨"本来就不同"的活动**）----
    # ① 组内（同 activity_id / 已按规则 B 接链）：跨窗口按 (原话, fact_type) 只留最后窗口那份；
    # ② 同窗口内（不同活动之间）：同一 (原话, fact_type) 也只留后出现的那个。
    #
    # 为什么**不做**全局跨窗口去重：实测反例 —— 11-07 08:00「武侠剧」与 08:40「游戏解说」
    # 是两个不同活动（只因共享一句长原话），全局去重会把前者的唯一一条 item 抢走后
    # 让整个活动变空被删掉 ⇒ **一个真实活动凭空消失**。组内去重不影响这个。
    def _item_key(item: Mapping[str, Any], window_index: int) -> tuple:
        fact = str(item.get("fact_type") or "none")
        group = MERGE_FACT_GROUPS.get(fact, fact)
        quotes = _item_quote_set(item)
        if quotes:
            return ("q", frozenset(quotes), group)
        sources = _item_source_ids(item)
        if sources:
            return ("s", sources, group)
        return ("w", window_index, id(item))          # 无任何身份 → 不合并

    dropped_duplicates = 0
    group_items: dict[str, dict[tuple, tuple[int, dict[str, Any]]]] = {}
    for root, members in groups.items():
        slot: dict[tuple, tuple[int, dict[str, Any]]] = {}
        for window_index, _, _, activity in sorted(members, key=lambda m: (m[0], m[2])):
            for item in activity.get("items") or []:
                key = _item_key(item, window_index)
                previous = slot.get(key)
                if previous is not None and previous[1].get(SMOKE_DEMOTED_FLAG):
                    dropped_duplicates += 1
                slot[key] = (window_index, item)
        group_items[root] = slot

    # ② 同窗口内跨活动：同一个 (原话, fact_type) 只留在**后出现**的那个活动里。
    #    两遍式（先收集 → 再删），避免"链式 pop 到已删的 key"导致同一条被两边都留着。
    occurrences: dict[tuple, list[tuple[int, str, tuple]]] = {}
    for order, root in enumerate(sorted(group_items, key=lambda r: min(m[0] for m in groups[r]))):
        for key, (window_index, item) in group_items[root].items():
            occurrences.setdefault((window_index, key), []).append((order, root, key))
    for owners in occurrences.values():
        if len(owners) < 2:
            continue
        owners.sort(key=lambda o: o[0])
        for _, root, key in owners[:-1]:
            removed = group_items[root].pop(key, None)
            if removed is not None and removed[1].get(SMOKE_DEMOTED_FLAG):
                dropped_duplicates += 1

    # ---- 重建活动（元信息取最后窗口；空活动移除）----
    groups: dict[str, list[tuple[int, str, str, dict[str, Any]]]] = {}
    for record in records:
        groups.setdefault(find(record[2]), []).append(record)

    activities: list[dict[str, Any]] = []
    for root, members in groups.items():
        members.sort(key=lambda m: (m[0], m[2]))
        kept = [item for _, item in sorted(group_items.get(root, {}).values(),
                                           key=lambda pair: pair[0])]
        if not kept:
            continue                                   # 规则 C 的收尾：空活动移除
        last = members[-1][3]
        meta = {k: v for k, v in last.items() if k != "items"}
        meta["items"] = kept
        meta["merged_activity_ids"] = sorted({m[2] for m in members})
        meta["merged_windows"] = sorted({m[1] for m in members})
        titles: list[str] = []
        for _, _, _, activity in members:
            title = activity.get("title")
            if title and title not in titles:
                titles.append(title)
        meta["merged_titles"] = titles
        activities.append((members[0][0], meta))

    activities.sort(key=lambda pair: pair[0])
    merged_activities = [meta for _, meta in activities]

    return {
        "schema_version": 3,
        "cutoff": ordered[-1].get("cutoff") if ordered else None,
        "windows": len(ordered),
        "folded_runs": folded,
        "activities": merged_activities,
        "items": sum(len(a.get("items") or []) for a in merged_activities),
        "merge_rule": {
            "key": "normalised quote + fact_type group (source_id only as fallback)",
            "winner": "later window replaces earlier",
            "smoke_purchase_demoted": True,
            "empty_activities_removed": True,
            "dropped_redundant_demoted": dropped_duplicates,
            "duplicate_scope": "intra-group (cross-window) + intra-window (cross-activity)",
            "same_cutoff_folded": True,
            "activity_link": {
                "min_quote_chars": ACTIVITY_LINK_MIN_QUOTE_CHARS,
                "skip_categories": sorted(ACTIVITY_LINK_SKIP_CATEGORIES),
                "lifecycle_unknown_compatible": True,
            },
        },
    }
