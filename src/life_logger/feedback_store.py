"""标注回灌（数据飞轮）最小实现：一条人工判定 = 一行 JSONL，后端立即生效。

为什么要有这个文件
------------------
用户 2026-11-03：「我再标注1万次，你他妈的都是浪费了」。
核对下来确实如此：历史上人工判定散在 5 个互不相通的地方，其中只有一部分会被
前端读到，后端 `activity_lifecycle()` 从来不查任何人工表 —— 标了等于没标。

本模块把「人工判定」收敛成唯一的事实来源：

    data/feedback/YYYY-MM-DD.jsonl      追加写，一行一条
    {"schema":"feedback-v1","day":"2026-11-03","target":"lifecycle",
     "activity_id":"act_007","before":"media","after":"live",
     "basis":"用户原话…","source":"q745-audit","recorded_at":"…"}

两种消费方式（这就是"回灌"）：
  A. 立即生效（本单实现）：`overrides(day, "lifecycle")` 被
     `activity_timeline_runtime.activity_lifecycle()` 优先查询 ——
     API/页面/导出**同一套口径**，不再依赖前端硬编码表。
  B. few-shot（设计+demo）：`few_shot_examples()` 产出可拼进 prompt 的样例块。

设计约束（都是踩过的坑）：
  · **只追加不改写**：判定会被推翻，历史必须留痕，取"最后一条生效"。
  · **按日分区**：`act_NNN` 每天从 1 重号，跨日套用会命中别的活动。
  · **fail-open**：读不到/写坏了都绝不能拖垮主链路（返回空覆盖）。
  · **幂等迁移**：`migrate_legacy()` 可重复跑，同一 (day,target,id,after) 不重复写。

用法：
    python3 scripts/feedback.py migrate                    # 把散落的表搬进来
    python3 scripts/feedback.py record --day 2026-11-03 \
        --target lifecycle --id act_012 --after media --basis "..." --source manual
    python3 scripts/feedback.py list --day 2026-11-03
    python3 scripts/feedback.py overrides --day 2026-11-03 --target lifecycle
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

logger = logging.getLogger(__name__)

SCHEMA = "feedback-v1"
ROOT = Path(__file__).resolve().parents[2]
FEEDBACK_DIR = Path(os.environ.get("LL_FEEDBACK_DIR") or (ROOT / "data" / "feedback"))
BEIJING = timezone(timedelta(hours=8))

#: 允许的判定维度。新增维度必须同时加一条消费方，否则就是"存着不生效"。
TARGETS = ("lifecycle", "category", "scene", "event_review")
#: lifecycle 的合法取值
LIFECYCLE_VALUES = ("live", "media", "unknown")  # [2026-11-04] unknown 是合法结论，不是"没答案"

# ---------------------------------------------------------------------------
# [Q826] 判定来源等级（provenance）
#
# 背景：data/feedback 里 591 行**全部**是 source=migrate:*（迁移脚本从 JS 分档表
# 批量猜出来的），0 条用户显式确认；591/591 的 before 为 null。
# 但它们被当成"人工判定"直接压过了声纹证据 —— activity_lifecycle(act_072) 因为
# 一条 migrate 记录返回 media，而该卡有 5 条 source_label=self。
#
# 所以引入等级，只有 user_confirmed 有"一票否决"权：
#
#   user_confirmed      用户明确点过/说过（人工审核、用户纠正）
#   assistant_heuristic 助手按规则推的（可作弱信号，不得压过证据）
#   migrated_guess      从旧 JS 表批量迁移来的猜测（**旧行默认值**）
#
# 铁律：**缺字段 = migrated_guess**。历史数据一个字节都不用改，
# 读取时按最低等级解释即可 —— 不重写用户原始判定。
# ---------------------------------------------------------------------------
PROVENANCE_LEVELS = ("user_confirmed", "assistant_heuristic", "migrated_guess")
DEFAULT_PROVENANCE = "migrated_guess"
#: 同一 id 出现 live/media 冲突且无人工裁决时的显式解析值（不再静默取最后一条）
CONFLICT_VALUE = "unknown"

_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")


class FeedbackError(ValueError):
    """记录不合法（缺字段 / 取值越界 / 日期格式错）。"""


def provenance_of(rec: Mapping[str, Any]) -> str:
    """一条记录的来源等级。**缺字段 → migrated_guess**（旧行一律降级，不改文件）。"""
    raw = str((rec or {}).get("provenance") or "").strip()
    return raw if raw in PROVENANCE_LEVELS else DEFAULT_PROVENANCE


def _infer_provenance(raw: Mapping[str, Any]) -> str:
    """写新记录时定级：显式给的优先，否则按 source 推断。"""
    explicit = str(raw.get("provenance") or "").strip()
    if explicit in PROVENANCE_LEVELS:
        return explicit
    source = str(raw.get("source") or "").strip().lower()
    if source.startswith("migrate:"):
        return "migrated_guess"
    if source in ("user", "user_confirmed", "manual-user", "user-mark"):
        return "user_confirmed"
    return "assistant_heuristic"


def _now_iso() -> str:
    return datetime.now(BEIJING).isoformat(timespec="seconds")


def _path_for(day: str) -> Path:
    if not _DAY_RE.match(str(day or "")):
        raise FeedbackError("day 必须是 YYYY-MM-DD，实际 %r" % (day,))
    return FEEDBACK_DIR / ("%s.jsonl" % day)


def normalize(raw: Mapping[str, Any]) -> dict[str, Any]:
    """校验 + 补默认值，返回可直接落盘的一条记录。"""
    if not isinstance(raw, Mapping):
        raise FeedbackError("记录必须是 dict")
    target = str(raw.get("target") or "").strip()
    if target not in TARGETS:
        raise FeedbackError("target 必须是 %s 之一，实际 %r" % ("/".join(TARGETS), target))
    ident = str(raw.get("activity_id") or raw.get("event_id") or "").strip()
    if not _ID_RE.match(ident):
        raise FeedbackError("activity_id/event_id 不合法：%r" % (ident,))
    after = raw.get("after")
    if target == "lifecycle":
        after = str(after or "").strip().lower()
        if after not in LIFECYCLE_VALUES:
            raise FeedbackError("lifecycle 的 after 必须是 live/media，实际 %r" % (after,))
    else:
        after = "" if after is None else str(after)
        if not after.strip():
            raise FeedbackError("target=%s 必须给 after" % target)

    day = str(raw.get("day") or "").strip()
    if not day:
        # 没给 day 就从 recorded_at 推，再不行用今天
        stamp = str(raw.get("recorded_at") or "")[:10]
        day = stamp if _DAY_RE.match(stamp) else datetime.now(BEIJING).strftime("%Y-%m-%d")
    _path_for(day)  # 校验格式

    out = {
        "schema": SCHEMA,
        "day": day,
        "target": target,
        "activity_id": ident,
        "before": None if raw.get("before") is None else str(raw.get("before")),
        "after": after,
        "basis": str(raw.get("basis") or "").strip()[:500],
        "source": str(raw.get("source") or "manual").strip()[:60],
        "recorded_at": str(raw.get("recorded_at") or _now_iso()),
        # [Q826] 等级必须显式落盘：新记录不再享受"默认=人工"的待遇
        "provenance": _infer_provenance(raw),
    }
    if target != "lifecycle":
        out["event_id"] = ident
    return out


def record(raw: Mapping[str, Any], *, dedupe: bool = True) -> dict[str, Any]:
    """追加一条判定。默认去重：同一 (day,target,id,after) 已存在则跳过（迁移幂等）。"""
    rec = normalize(raw)
    path = _path_for(rec["day"])
    path.parent.mkdir(parents=True, exist_ok=True)
    if dedupe:
        for existing in iter_records(rec["day"]):
            if (existing.get("target") == rec["target"]
                    and existing.get("activity_id") == rec["activity_id"]
                    and existing.get("after") == rec["after"]):
                return {**existing, "_skipped": "duplicate"}
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())
    _CACHE.clear()
    return rec


def iter_records(day: str | None = None) -> Iterable[dict[str, Any]]:
    """按文件顺序产出记录（坏行跳过，不抛）。day=None 时读全部日期。"""
    if day is not None:
        files = [_path_for(day)]
    else:
        files = sorted(FEEDBACK_DIR.glob("*.jsonl")) if FEEDBACK_DIR.is_dir() else []
    for path in files:
        if not path.exists():
            continue
        try:
            with path.open(encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        logger.warning("feedback: 跳过坏行 %s", path.name)
                        continue
                    if isinstance(obj, dict) and obj.get("schema") == SCHEMA:
                        yield obj
        except OSError:
            continue


def days() -> list[str]:
    if not FEEDBACK_DIR.is_dir():
        return []
    return sorted(p.stem for p in FEEDBACK_DIR.glob("*.jsonl"))


_CACHE: dict[tuple[str, str], tuple[float, dict[str, dict]]] = {}


def resolve(day: str, target: str = "lifecycle") -> dict[str, dict]:
    """把某天某维度的记录解析成 {id: {value, provenance, records, conflict, values}}。

    [Q826] 冲突不再"静默取最后一条"：
      · 有 user_confirmed → 用它（它是人工的最终裁决，可以覆盖历史猜测）
      · 无 user_confirmed 且 live/media 并存 → value=``unknown`` 且 ``conflict=True``
      · 其余 → 唯一值，providence 取池内**最高**等级（即最小值下标）
    """
    if not day or target not in TARGETS:
        return {}
    try:
        path = _path_for(day)
    except FeedbackError:
        return {}
    try:
        mtime = path.stat().st_mtime if path.exists() else 0.0
    except OSError:
        mtime = 0.0
    key = (day, target)
    cached = _CACHE.get(key)
    if cached and cached[0] == mtime:
        return {k: dict(v) for k, v in cached[1].items()}

    grouped: dict[str, list[dict[str, Any]]] = {}
    for rec in iter_records(day):
        if rec.get("target") != target:
            continue
        ident = str(rec.get("activity_id") or "")
        if ident:
            grouped.setdefault(ident, []).append(rec)

    result: dict[str, dict] = {}
    for ident, recs in grouped.items():
        confirmed = [r for r in recs if provenance_of(r) == "user_confirmed"]
        pool = confirmed or recs
        values = [str(r.get("after") or "") for r in pool]
        distinct = {v for v in values if v}
        level = ("user_confirmed" if confirmed
                 else min((provenance_of(r) for r in pool), key=PROVENANCE_LEVELS.index))
        if len(distinct) > 1:
            result[ident] = {"value": CONFLICT_VALUE, "provenance": level,
                             "records": len(recs), "conflict": True,
                             "values": sorted(distinct)}
        elif distinct:
            result[ident] = {"value": next(iter(distinct)), "provenance": level,
                             "records": len(recs), "conflict": False,
                             "values": sorted(distinct)}
    _CACHE[key] = (mtime, {k: dict(v) for k, v in result.items()})
    return result


def resolution_stats(day: str, target: str = "lifecycle") -> dict[str, int]:
    """各级计数 + 冲突条数（给 --audit / 页面观测用）。"""
    stats = {"ids": 0, "user_confirmed": 0, "assistant_heuristic": 0,
             "migrated_guess": 0, "conflict": 0}
    for info in resolve(day, target).values():
        stats["ids"] += 1
        stats[info["provenance"]] = stats.get(info["provenance"], 0) + 1
        if info["conflict"]:
            stats["conflict"] += 1
    return stats


def overrides(day: str, target: str = "lifecycle") -> dict[str, str]:
    """{id: 解析后的判定}（冲突 → ``unknown``）。读失败一律返回 {}（fail-open）。"""
    try:
        return {k: v["value"] for k, v in resolve(day, target).items()}
    except Exception:  # 绝不让回灌链路拖垮主链路
        logger.warning("feedback: overrides 失败（已忽略）", exc_info=True)
        return {}


def lifecycle_override(day: str, activity_id: str) -> str | None:
    """**只返回 user_confirmed 的判定**（给 activity_lifecycle 的 ⓪ 优先级用）。

    [Q826] 这是本单的核心改动：迁移来的猜测（migrated_guess / assistant_heuristic）
    **不再**能压过声纹 self 证据。冲突（value=unknown）也不返回，交回证据链判定。
    """
    try:
        info = resolve(day, "lifecycle").get(str(activity_id or ""))
        if not info or info["conflict"]:
            return None
        if info["provenance"] != "user_confirmed":
            return None
        return info["value"] if info["value"] in LIFECYCLE_VALUES else None
    except Exception:  # 绝不让回灌链路拖垮主链路
        logger.warning("feedback: lifecycle_override 失败（已忽略）", exc_info=True)
        return None


def lifecycle_weak(day: str, activity_id: str) -> str | None:
    """非人工晋级的判定（迁移猜测/助手启发式），只作**普通证据权重**。

    调用方必须把它放在证据链的末尾（tiebreaker），不能放在 ⓪。
    """
    try:
        info = resolve(day, "lifecycle").get(str(activity_id or ""))
        if not info or info["conflict"]:
            return None
        if info["provenance"] == "user_confirmed":
            return None          # 已经由 lifecycle_override 负责，避免重复
        return info["value"] if info["value"] in LIFECYCLE_VALUES else None
    except Exception:  # noqa: BLE001
        return None


def audit_provenance() -> dict[str, Any]:
    """[Q826] 忠实来源统计：多少是真人工、多少是迁移猜的、多少互相冲突。"""
    from collections import Counter

    by_prov: Counter[str] = Counter()
    by_source: Counter[str] = Counter()
    by_target: Counter[str] = Counter()
    days_n = 0
    total = 0
    conflict_days: dict[str, dict[str, int]] = {}
    for day in days():
        days_n += 1
        for rec in iter_records(day):
            total += 1
            by_prov[provenance_of(rec)] += 1
            by_source[str(rec.get("source") or "")] += 1
            by_target[str(rec.get("target") or "")] += 1
        stats = resolution_stats(day)
        if stats["conflict"]:
            conflict_days[day] = {"ids": stats["ids"], "conflict": stats["conflict"]}
    return {
        "days": days_n,
        "records": total,
        "by_provenance": dict(by_prov),
        "by_target": dict(by_target),
        "by_source": dict(by_source),
        "conflict_days": conflict_days,
        "conflict_total": sum(v["conflict"] for v in conflict_days.values()),
    }


# ---------------------------------------------------------------------------
# few-shot 样例池（路径 B 的数据来源；prompt 注入见 activity_timeline_runtime）
# ---------------------------------------------------------------------------
def few_shot_examples(day: str | None = None, target: str = "lifecycle",
                      limit: int = 5) -> list[dict[str, str]]:
    """取人工判定作为 few-shot 样例。

    四条挑选规则（都是为了"样例真的有用"）：
      0. **[Q826] 只收 user_confirmed** —— 迁移来的猜测不是人工判定，当样例喂给模型
         等于"用机器猜的结果去教机器"，会把错误固化进 prompt（实测 163 条迁移记录
         曾被当成历史真值注入）。
      1. **类别均衡**：live / media 各取一半，否则样例全是 media 会把模型往一个方向推；
      2. **要有理由**：basis < 8 字符（"标错了""人工逐条核定"这种）不进样例；
      3. **新的一天优先**，同一 activity 只出现一次。
    """
    if target != "lifecycle":
        limit = max(1, limit)
    buckets: dict[str, list[dict[str, str]]] = {"live": [], "media": [], "unknown": []}
    seen: set[str] = set()
    for d in (sorted(days(), reverse=True) if day is None else [day]):
        for rec in reversed(list(iter_records(d))):
            if rec.get("target") != target:
                continue
            if provenance_of(rec) != "user_confirmed":
                continue                      # [Q826] 非人工确认的一律不进样例
            ident = str(rec.get("activity_id") or "")
            after = str(rec.get("after") or "")
            basis = str(rec.get("basis") or "").strip()
            if not ident or ident in seen or len(basis) < 8:
                continue
            # 迁移时留下的"人工逐条核定（xxx.js）"没有信息量，进样例只会教模型凑热闹
            if basis.startswith(("人工逐条核定", "人工分类覆盖")):
                continue
            if target == "lifecycle" and after not in buckets:
                continue
            seen.add(ident)
            # 注意：不能用 buckets.get(after) or ... —— 空 list 是 falsy，会全落到 "_" 桶里（踩过）
            if after in buckets:
                target_list = buckets[after]
            else:
                target_list = buckets.setdefault("_", [])
            target_list.append({
                "day": d, "activity_id": ident,
                "before": str(rec.get("before") or ""),
                "after": after, "basis": basis[:200],
            })
    if target != "lifecycle":
        return list(buckets.get("_") or [])[:limit]

    picked: list[dict[str, str]] = []
    while len(picked) < limit and (buckets["live"] or buckets["media"] or buckets["unknown"]):
        for key in ("live", "media", "unknown"):
            if len(picked) >= limit:
                break
            if buckets[key]:
                picked.append(buckets[key].pop(0))
    return picked


def render_few_shot_block(examples: Iterable[Mapping[str, Any]]) -> str:
    """把样例渲染成可直接拼进 prompt 的文本块；没有样例返回 ""。"""
    rows = list(examples)
    if not rows:
        return ""
    lines = [
        "【历史人工判定样例（同一判据下的真值；遇到同类内容按此口径判断）】：",
    ]
    for i, ex in enumerate(rows, 1):
        lines.append(
            "%d. 活动 %s → %s（系统原判 %s）理由：%s"
            % (i, ex.get("activity_id"), ex.get("after"),
               ex.get("before") or "未知", ex.get("basis"))
        )
    lines.append(
        "注意：样例只说明「哪类内容该判什么」，不要照抄 activity_id；"
        "拿不准时宁可判 live（宁可多显示，不要藏用户的事）。"
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 迁移：把历史上散落的 5 种人工表搬进 feedback
# ---------------------------------------------------------------------------
def _parse_js_id_array(text: str, key: str) -> list[str]:
    m = re.search(r'%s["\']?\s*:\s*\[(.*?)\]' % re.escape(key), text, re.S)
    return re.findall(r'"(act_[^"]+|[A-Za-z0-9_.:-]+)"', m.group(1)) if m else []


def migrate_legacy(root: Path | None = None, *, dry_run: bool = False) -> dict[str, int]:
    """把散落的人工表搬进 data/feedback/。可重复执行（按 (day,target,id,after) 去重）。

    只搬"以前不生效"的维度 —— 已经生效的不搬（搬了只是多一份事实源）：

      ✅ web/activity-buckets-<day>.js            人工 live/media 分档 → lifecycle
         （原来只有前端读，后端 `activity_lifecycle()` 完全不看 → 标了等于没标）
      ✅ web/activity-category-override.js        人工分类覆盖 → category
         （原来是手写 JS；现在 JSONL 为源，`emit_category_js()` 反向生成 JS）
      ❌ output/local-web/scene-corrections.json  场景撤销
         （`daily_dashboard.py:1806` 已在应用 revoke_scene，**本来就生效**）
      ❌ output/local-web/pending-review-states.json 用户"忽略/已看过"
         （`pending_review.py:466` 已经在消费，**本来就生效**）
      ❌ work/**/raw/*.tsv 里的人工判定列          （格式各异；结论已被 ① 吸收）
    """
    root = Path(root or ROOT)
    stats = {"lifecycle": 0, "category": 0, "scene": 0, "event_review": 0, "skipped": 0}

    # 1) 分档表
    for js in sorted((root / "web").glob("activity-buckets-[0-9]*-[0-9]*-[0-9]*.js")):
        day = js.stem.replace("activity-buckets-", "")
        if not _DAY_RE.match(day):
            continue
        text = js.read_text(encoding="utf-8")
        for key, after in (("live", "live"), ("media", "media")):
            for ident in _parse_js_id_array(text, key):
                rec = {"day": day, "target": "lifecycle", "activity_id": ident,
                       "after": after, "basis": "人工逐条核定（%s）" % js.name,
                       "source": "migrate:" + js.name}
                if dry_run:
                    stats["lifecycle"] += 1
                    continue
                try:
                    got = record(rec)
                    if got.get("_skipped"):
                        stats["skipped"] += 1
                    else:
                        stats["lifecycle"] += 1
                except FeedbackError:
                    stats["skipped"] += 1

    # 2) 分类覆盖（同一天里 key=分类名，value=id 列表 → after=分类名）
    ov = root / "web" / "activity-category-override.js"
    if ov.exists():
        text = ov.read_text(encoding="utf-8")
        for m in re.finditer(r'"(\d{4}-\d{2}-\d{2})"\s*:\s*\{(.*?)\n\s*\}', text, re.S):
            day, body = m.group(1), m.group(2)
            for cm in re.finditer(r'"([^"]+)"\s*:\s*\[(.*?)\]', body, re.S):
                cat, ids = cm.group(1), re.findall(r'"(act_[^"]+)"', cm.group(2))
                for ident in ids:
                    rec = {"day": day, "target": "category", "activity_id": ident,
                           "after": cat, "basis": "人工分类覆盖（%s）" % ov.name,
                           "source": "migrate:" + ov.name}
                    if dry_run:
                        stats["category"] += 1
                        continue
                    try:
                        got = record(rec)
                        if got.get("_skipped"):
                            stats["skipped"] += 1
                        else:
                            stats["category"] += 1
                    except FeedbackError:
                        stats["skipped"] += 1

    # 3) 场景撤销 / 待确认台账：**已生效，故意不搬**（见 docstring）
    #    统计里记 0，避免让人误以为"搬过了"。

    return stats


def emit_category_js(root: Path | None = None) -> dict[str, Any]:
    """把 feedback 里的 category 判定反向生成 `web/activity-category-override.js`。

    为什么需要反向生成：前端只能读 JS，JSONL 后端读。没有这一步，
    category 判定就还是"存着不生效"。生成的是**同一份数据**，不是第二份事实源。
    """
    root = Path(root or ROOT)
    out = root / "web" / "activity-category-override.js"
    by_day: dict[str, dict[str, list[str]]] = {}
    for rec in iter_records(None):
        if rec.get("target") != "category":
            continue
        day = str(rec.get("day") or "")
        cat = str(rec.get("after") or "")
        ident = str(rec.get("activity_id") or "")
        if not (_DAY_RE.match(day) and cat and ident):
            continue
        by_day.setdefault(day, {}).setdefault(cat, [])
        if ident not in by_day[day][cat]:
            by_day[day][cat].append(ident)

    stamp = datetime.now(BEIJING).strftime("%Y-%m-%dT%H:%M:%S%z")
    lines = [
        "/* ⚠️ 生成文件，请勿手改。",
        " * 源：data/feedback/YYYY-MM-DD.jsonl 里 target=category 的人工判定。",
        " * 生成器：python3 scripts/feedback.py emit-category-js",
        " * 生成时间：%s" % stamp,
        " *",
        " * 原来这张表是手写的（标了不会回流）。现在 JSONL 是唯一源，本文件由它生成。",
        " */",
        "window.ACTIVITY_CATEGORY_OVERRIDE = {",
    ]
    for day in sorted(by_day):
        lines.append('  "%s": {' % day)
        for cat in sorted(by_day[day]):
            ids = ", ".join('"%s"' % i for i in by_day[day][cat])
            lines.append('    "%s": [%s]' % (cat, ids))
        lines.append("  },")
    lines.append("};")
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"path": str(out), "days": sorted(by_day),
            "count": sum(len(v) for d in by_day.values() for v in d.values())}


def backfill_basis_from_audits(root: Path | None = None, *, dry_run: bool = False) -> dict[str, int]:
    """用审计 TSV 里的「依据」补全 basis（迁移时 buckets 文件只有"人工逐条核定"，没理由）。

    为什么值得单独做：few-shot 的价值全在"为什么"，只存 live/media 两个词，样例对模型没用。
    做法是**一次性**改写各日 JSONL（同 (day,target,id) 保留最后一条，basis 换成审计里的依据），
    改写前留 `.bak`。之后仍然只追加，不改写。

    来源：
      work/six/card-audit/raw/visible-80.tsv        11-02 的 80 条（aid/verdict/why）
      work/six/fold-audit/raw/media-63.tsv          11-02 的 63 条（aid/verdict/action/why）
      work/1103-live-audit-20261103/raw/audit-91.tsv 11-03 的 91 条（aid/now/verdict/why）
    """
    import csv

    root = Path(root or ROOT)
    reasons: dict[tuple[str, str], str] = {}
    DAYS = {"visible-80.tsv": "2026-11-02", "media-63.tsv": "2026-11-02",
            "audit-91.tsv": "2026-11-03"}
    paths = [
        root / "work/six/card-audit/raw/visible-80.tsv",
        root / "work/six/fold-audit/raw/media-63.tsv",
        root / "work/1103-live-audit-20261103/raw/audit-91.tsv",
    ]
    for path in paths:
        if not path.exists():
            continue
        day = DAYS.get(path.name)
        if not day:
            continue
        with path.open(encoding="utf-8") as f:
            for row in csv.DictReader(f, delimiter="\t"):
                ident = (row.get("aid") or row.get("activity_id") or "").strip()
                why = (row.get("why") or "").strip()
                verdict = (row.get("verdict") or "").strip()
                if not ident or not why:
                    continue
                after = {"本人": "live", "媒体": "media", "误藏": "live"}.get(verdict)
                if after:
                    reasons[(day, ident)] = why

    stats = {"days": 0, "rewritten": 0, "basis_filled": 0, "skipped": 0}
    for day in days():
        path = _path_for(day)
        rows = list(iter_records(day))
        if not rows:
            continue
        changed = False
        for rec in rows:
            if rec.get("target") != "lifecycle":
                continue
            why = reasons.get((day, str(rec.get("activity_id") or "")))
            if not why:
                continue
            if str(rec.get("basis") or "").startswith("人工逐条核定"):
                rec["basis"] = why[:500]
                rec["source"] = "migrate:audit-tsv"
                changed = True
                stats["basis_filled"] += 1
        if not changed:
            continue
        stats["days"] += 1
        if dry_run:
            continue
        backup = path.with_suffix(".jsonl.bak-basis")
        if not backup.exists():
            backup.write_bytes(path.read_bytes())
        tmp = path.with_suffix(".jsonl.tmp")
        with tmp.open("w", encoding="utf-8") as f:
            for rec in rows:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        os.replace(tmp, path)
        _CACHE.clear()
        stats["rewritten"] += len(rows)
    return stats
