#!/usr/bin/env python3
"""对账线：代码扫转写原文，和当天账本比对 —— **只报告，不改任何东西**。

定位（见 `docs/architecture/five-layer-framework.md` 的「旁路 · 对账线」）
-------------------------------------------------------------------------
第 3 层（大模型读活动卡）会漏；本脚本反过来：**直接从转写原文里找"像消费的句子"**，
再和当天 `expense_ledger`（入账）/ 待确认 / 已驳回 逐个比对，把"有原话但没入账"的挑出来。
它不调模型、不改账本、不写"需要我判断"、不加定时 —— 上线条件里写的是"只报告，误报可接受后"，
所以本脚本的产出是**给人看的候选清单**，不是决策。

三条边界
--------
1. **不调模型**：全部是正则 + 中文数字解析 + 文本相似度。
2. **不改账本**：只读 `/api/daily`（GET）、只读 `output/local-web/*.json`，不写任何 output。
3. **不静默**：每条候选都写清楚"为什么认为它是消费"和"为什么判定已入账/没入账"。

转写口径
--------
直接复用 `life_logger.capture_funnel._iter_transcript_paths` 的同一套 glob + 排除规则
（recovery/live/preview/backup 不算，同内容按 md5 只算一次），
这样"对账线看到多少句"与页面「说了多少」永远一致。

用法
----
    python3 scripts/reconcile_transcript.py --date 2026-11-04
    python3 scripts/reconcile_transcript.py --from 2026-11-02 --to 2026-11-05
    python3 scripts/reconcile_transcript.py --date 2026-11-04 --json out.json --limit 50
"""

from __future__ import annotations

import argparse
import gzip
import json
import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, List

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from life_logger.capture_funnel import (  # noqa: E402  口径唯一来源
    _EXCLUDED_PARTS,
    _JUNK_TEXTS,
    _iter_transcript_paths,
    _md5,
)
from life_logger.jsonio import read_json  # noqa: E402

BJ_TZ = timezone(timedelta(hours=8))
DEFAULT_WEB_ROOT = ROOT / "output" / "local-web"
DEFAULT_BASE_URL = "http://127.0.0.1:8765"
TOKEN_ENV = "LL_RECONCILE_TOKEN"

# ─────────────────────────────────────────────────────────────── 金额
CN_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
             "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
CN_UNITS = {"十": 10, "百": 100, "千": 1000}
CN_NUM_CHARS = "".join(CN_DIGITS) + "".join(CN_UNITS) + "万"

#: 「四块」「一百四」「四百二十三」「四十三」「两块五」这类口语金额
CN_AMOUNT = re.compile(rf"[{CN_NUM_CHARS}]+(?:点[{CN_NUM_CHARS}]+)?(?:块|元|毛|角)?")
#: 阿拉伯数字金额：「32.5」「70」「42」
AR_AMOUNT = re.compile(r"(?<![\d.])(\d{1,6}(?:\.\d{1,2})?)(?![\d.])\s*(?:块|元|块钱)?")
MONEY_UNIT = re.compile(r"(?:块|元|块钱|毛|角|钱)")

#: 命中任何一条就**不算干净候选**（不是删掉，是分堆：报告里单独列，误报率才算得清）
HARD_FP_FLAGS = {"news", "question", "bargain", "hypothetical", "third_party", "fragment"}
#: 强线索：出现即几乎一定是"一笔交易"，可以没有金额（如「拿一包红塔山」）
STRONG_VERBS = ("买了", "买个", "买下", "买一", "购买", "付了", "付款", "支付", "付钱",
                "订阅", "充值", "充了", "下单", "结账", "买单", "扫了", "刷卡", "刷了",
                "拿一包", "来一包", "要一包",
                "快递费", "运费", "话费", "电费", "水费", "停车费", "会员费", "服务费",
                "交了", "缴费", "转账", "到账", "退款", "退了", "红包", "请客")
#: 弱线索：单靠它不算（「花了近百年」「采购」这类太容易误报），只用来给金额句加权
WEAK_CUES = ("买", "花", "费", "购", "付", "块", "元")
#: 问价 / 假设 / 犹豫 —— 这些**不是消费事实**（"这修大概多少钱"）
QUESTION_CUES = ("多少钱", "多少", "几块", "贵不贵", "贵吗", "便宜吗", "多少钱啊",
                 "大概多少", "要多少", "怎么卖", "能不能", "可不可以", "行不行",
                 "是不是", "有没有", "吗？", "吗?", "呢？", "呢?")
HYPOTHETICAL_CUES = ("如果", "要是", "假如", "假设", "估计", "可能", "好像", "差不多")
#: 谈价 / 估价 / 报价 —— 说的是一笔"可能要花的钱"，不是已经花掉的钱
BARGAIN_CUES = ("报价", "左右", "手工费", "能不能少", "太贵", "划不划算", "不划算",
                "大概", "估价", "多少钱", "哪个划算", "包风险", "拆屏幕", "修一下", "修大概")
#: 外放 / 视频 / 播报线索 —— 只用于**排序和标注**，不用于过滤（派单要求不筛声纹/不外放硬筛）
EXTERNAL_CUES = ("播报", "收款播报", "到账", "视频", "解说", "直播", "弹幕", "背景播放",
                 "外放", "歌词", "主播", "观众", "游戏", "皮肤", "抽卡", "会员")
#: 新闻 / 短视频旁白 / 剧情配音 —— 说的是别人的钱，不是本人的消费
NEWS_CUES = ("记者", "报道", "通报", "警方", "法院", "嫌疑人", "网友", "新闻", "罚款",
             "打赏", "主播", "节目", "调查", "案发", "判刑", "起诉", "直播间", "公告",
             "采访", "评论员", "某某", "男子", "女子", "陈某", "被告人", "判决")
#: 明显不是本人消费的场景词（内容消费/别人的钱）
THIRD_PARTY_CUES = ("他请", "别人买", "室友", "同事", "老板买", "我给我女儿", "我给我妈",
                    "给老人", "帮人买", "给她买", "给他买", "给你买", "帮他买", "帮她买")


def cn_to_number(text: str) -> float | None:
    """中文数字 → 数值。支持「四」「十六」「四十三」「一百四」「四百二十三」「两块五」。

    「一百四」按口语算 **140**（末尾单位省略十位），这是中文金额最常见的坑。
    """
    if not text:
        return None
    total = 0
    section = 0
    current = 0
    last_unit = 0
    seen = False
    for char in text:
        if char in CN_DIGITS:
            current = CN_DIGITS[char]
            seen = True
        elif char in CN_UNITS:
            unit = CN_UNITS[char]
            section += (current or 1) * unit
            current = 0
            last_unit = unit
            seen = True
        elif char == "万":
            section = (section + current) * 10000
            total += section
            section = 0
            current = 0
            last_unit = 0
            seen = True
    if not seen:
        return None
    # 口语省略：「一百四」=140、「两千三」=2300
    if current and last_unit and last_unit >= 100:
        section += current * (last_unit // 10)
        current = 0
    return float(total + section + current)


#: 「一块玻璃/一块表/一块砖」——块在这里是量词不是钱
CLASSIFIER_AFTER_KUAI = ("玻璃", "表", "砖", "石头", "面", "地", "布", "肉", "饼", "蛋糕",
                         "钱", "儿", "冰", "铁", "板")


def _is_money_kuai(text: str, end: int) -> bool:
    """「块」后面不能跟名词（一块玻璃），否则是量词不是钱。"""
    tail = text[end:end + 4]
    if not tail:
        return True
    return not any(tail.startswith(noun) for noun in CLASSIFIER_AFTER_KUAI if noun != "钱")


#: 这些量词/单位跟在数字后面说明**不是钱**（「近百年」「三个人」「十来个」）
NON_MONEY_TAIL = ("年", "个", "天", "人", "次", "分钟", "小时", "岁", "点", "号", "楼",
                  "层", "遍", "下", "米", "公里", "斤", "克", "本", "页", "集", "条",
                  "张", "台", "部", "只", "件", "包", "瓶", "杯", "碗", "根", "支")


def parse_amounts(text: str, *, allow_bare: bool = False) -> list[tuple[str, float]]:
    """从一句话里抽金额。返回 [(原文片段, 数值)]，按出现顺序去重。

    `allow_bare=True`：允许**没有「块/元」单位**的中文/阿拉伯数字（只给「订阅四百二十三」
    「快递费四十三」这种强线索句用；「近百年」「三包」这类由 NON_MONEY_TAIL 挡掉）。
    """
    found: list[tuple[str, float]] = []
    seen_span: list[tuple[int, int]] = []

    for match in AR_AMOUNT.finditer(text):
        raw = match.group(1)
        start, end = match.span()
        tail = text[end:end + 2]
        has_unit = bool(MONEY_UNIT.match(tail or "")) or bool(match.group(0).strip().endswith(("块", "元")))
        # 没单位的纯数字（年份 2026、数量 3）容易误报：要求带单位，或 allow_bare + 强线索
        if not has_unit:
            if not allow_bare:
                continue
            if any(text[end:end + 2].startswith(tail) for tail in NON_MONEY_TAIL):
                continue
        if "块" in (tail or "")[:1] and not _is_money_kuai(text, match.end()):
            continue
        try:
            value = float(raw)
        except ValueError:
            continue
        if value <= 0 or value > 100000:
            continue
        found.append((match.group(0).strip(), value))
        seen_span.append((start, end))

    for match in CN_AMOUNT.finditer(text):
        raw = match.group(0)
        if not raw:
            continue
        start, end = match.span()
        if any(a <= start < b or a < end <= b for a, b in seen_span):
            continue
        has_unit = bool(MONEY_UNIT.search(raw))
        # 没有「块/元/毛/角」的中文数字：默认不算金额（「一百四」单独出现分不清是钱还是数量），
        # 只有调用方明确 allow_bare（该句有强线索）时才收，并要求是 ≥10 的多位数字。
        if not has_unit:
            if not allow_bare:
                continue
            if not any(unit in raw for unit in ("十", "百", "千", "万")):
                continue
            if any(text[end:end + 2].startswith(tail) for tail in NON_MONEY_TAIL):
                continue
        if "块" in raw and not _is_money_kuai(text, end):
            continue
        if raw in {"一块", "两块", "三块"} and not any(v in text for v in STRONG_VERBS):
            # 「一块玻璃」之外还有一种：单纯数数。没有强线索时不当金额。
            if not MONEY_UNIT.search(text.replace(raw, "")):
                continue
        number_part = re.split(r"[块元毛角]", raw)[0]
        decimal_part = ""
        if "点" in number_part:
            number_part, decimal_part = number_part.split("点", 1)
        value = cn_to_number(number_part)
        if value is None or value <= 0:
            continue
        if decimal_part:
            digits = "".join(str(CN_DIGITS.get(c, "")) for c in decimal_part)
            if digits:
                value += float(f"0.{digits}")
        found.append((raw, value))

    deduped: list[tuple[str, float]] = []
    seen_value: set[float] = set()
    for raw, value in found:
        if value in seen_value:
            continue
        seen_value.add(value)
        deduped.append((raw, value))
    return deduped


# ─────────────────────────────────────────────────────────────── 转写读取
_DEDUP_STRIP = re.compile(r"[\s，。！？、；：,.!?;:\"'“”‘’（）()【】\[\]…—\-~·]")


def _norm_for_dedup(text: str) -> str:
    return _DEDUP_STRIP.sub("", str(text or ""))


@dataclass
class Row:
    text: str
    at: datetime
    speaker: str
    source: str
    segment_id: str
    recording_id: str
    source_label: str = ""          # self / non_self / unknown —— 只有带标签的副本才有
    acoustic_proximity: str = ""    # near / far / uncertain

    @property
    def has_label(self) -> bool:
        return bool(self.source_label or self.acoustic_proximity)


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(BJ_TZ)


def load_rows(date: str, web_root: Path = DEFAULT_WEB_ROOT) -> tuple[list[Row], dict[str, int]]:
    """读当天全部转写行（跳过重复文件/垃圾句子），口径与 capture_funnel 一致。"""
    rows: list[Row] = []
    seen_fingerprints: set[str] = set()
    stats = {"files": 0, "files_dup": 0, "rows": 0, "rows_junk": 0, "rows_no_text": 0}

    for path in sorted(set(_iter_transcript_paths(web_root, date))):
        posix = path.as_posix()
        if any(part in posix for part in _EXCLUDED_PARTS):
            continue
        fingerprint = _md5(path)
        if fingerprint in seen_fingerprints:
            stats["files_dup"] += 1
            continue
        seen_fingerprints.add(fingerprint)
        payload = read_json(path)
        if not isinstance(payload, list):
            continue
        stats["files"] += 1
        for row in payload:
            if not isinstance(row, dict):
                continue
            text = str(row.get("text") or "").strip()
            if not text:
                stats["rows_no_text"] += 1
                continue
            if text in _JUNK_TEXTS:
                stats["rows_junk"] += 1
                continue
            at = _parse_time(row.get("absolute_started_at")) or _parse_time(row.get("absolute_ended_at"))
            if at is None or at.strftime("%Y-%m-%d") != date:
                # 跨日的行只保留落在当天时钟日的（避免把前一晚算进今天）
                if at is None:
                    continue
                if at.strftime("%Y-%m-%d") != date:
                    continue
            stats["rows"] += 1
            rows.append(Row(
                text=text,
                at=at,
                speaker=str(row.get("speaker") or ""),
                source=str(row.get("source") or ""),
                segment_id=str(row.get("segment_id") or ""),
                recording_id=str(row.get("recording_id") or ""),
                source_label=str(row.get("source_label") or "").strip().lower(),
                acoustic_proximity=str(row.get("acoustic_proximity") or "").strip().lower(),
            ))
    # 行级去重：同一条音频会落在多份转写里（watch 正式 ASR 一份、作业包一份），
    # 文件 md5 不同但句子是同一句。**关键是"带标签的那份优先"**：
    # 作业包（jobs/*/result/*/transcript.json）没有 source_label/acoustic_proximity，
    # 手表正式 ASR（watch-sessions/*/formal-asr/segments/*）才有 ——
    # [2026-11-06 Q915] 老版本按"先到先得 + 有 speaker 优先"去重，结果把有标签的那份丢了，
    # 于是对外宣称"全库 0 条 source_label"。现在的优先级是：
    #   带标签 > 带 speaker > 其他；时间容差 ±3 秒（两份副本的秒级时间偶尔差 1 秒）。
    rows.sort(key=lambda r: r.at)
    kept: list[Row] = []
    stats["rows_dup"] = 0
    stats["rows_labeled"] = 0
    for row in rows:
        text_key = _norm_for_dedup(row.text)
        twin_index = None
        for index in range(len(kept) - 1, -1, -1):
            other = kept[index]
            if (row.at - other.at).total_seconds() > 3.0:
                break
            if _norm_for_dedup(other.text) == text_key:
                twin_index = index
                break
        if twin_index is None:
            kept.append(row)
            continue
        stats["rows_dup"] += 1
        existing = kept[twin_index]
        if _row_priority(row) > _row_priority(existing):
            kept[twin_index] = row
    rows = kept
    stats["rows_labeled"] = sum(1 for r in rows if r.has_label)
    return rows, stats


def _row_priority(row: Row) -> int:
    """去重时谁更该被保留：带 source_label/acoustic_proximity 的最优先。"""
    return (2 if row.has_label else 0) + (1 if row.speaker else 0)


# ─────────────────────────────────────────────────────────────── 候选
@dataclass
class Candidate:
    at: datetime
    text: str
    amount: float | None
    amount_raw: str
    cues: list[str] = field(default_factory=list)
    speaker: str = ""
    source_label: str = "unknown"      # self / non_self / unknown（来自带标签的转写副本）
    acoustic_proximity: str = "unknown"  # near / far / uncertain
    external_hint: str = "unknown"     # 非本人/外放：non_self/far（有标签）或 keyword（只能猜）
    flags: list[str] = field(default_factory=list)
    strength: str = "noise"            # strong / medium / noise
    verdict: str = "candidate"         # booked / pending / rejected / candidate
    rank_score: float = 0.0
    rank_reason: str = ""
    reason: str = ""
    match: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "at": self.at.strftime("%H:%M:%S"),
            "date": self.at.strftime("%Y-%m-%d"),
            "text": self.text,
            "amount": self.amount,
            "amount_raw": self.amount_raw,
            "cues": self.cues,
            "speaker": self.speaker,
            "source_label": self.source_label,
            "acoustic_proximity": self.acoustic_proximity,
            "external_hint": self.external_hint,
            "flags": self.flags,
            "strength": self.strength,
            "verdict": self.verdict,
            "rank_score": self.rank_score,
            "rank_reason": self.rank_reason,
            "reason": self.reason,
            "match": self.match,
        }


CONTEXT_WINDOW_SECONDS = 15.0


def extract_candidates(rows: List[Row]) -> list[Candidate]:  # noqa: F821
    """规则筛"可能的消费句"。不筛声纹、不筛外放 —— 只打标，便于人工排序。

    三档强度（决定它进"候选"还是进"噪音"，避免把上百条垃圾塞给人看）：
      strong：有带单位的金额（四块 / 32.5 元）—— 基本就是一笔钱
      medium：有强动词短语但没说金额（拿一包红塔山 / Cloud 的订阅）
      noise ：只有弱线索（花/费/买）且没金额 —— 留在 JSON 里可查，不进候选清单

    **上下文窗口**：单句判断不了"这是新闻还是本人消费"。同一句「两百块是吧」，
    前一句是「拆屏幕搞，包风险咯」就是谈价，前一句是「收钱吧」就是到账。
    所以每条候选都带 ±15 秒的相邻句做上下文，新闻/谈价线索在上下文里出现也算。
    """
    ordered = list(rows)
    out: list[Candidate] = []
    for index, row in enumerate(ordered):
        text = row.text
        strong_hits = [verb for verb in STRONG_VERBS if verb in text]
        amounts = parse_amounts(text)
        if not amounts and strong_hits:
            amounts = parse_amounts(text, allow_bare=True)
        if not amounts and not strong_hits:
            continue

        if amounts and strong_hits:
            strength = "strong"
        elif amounts:
            strength = "strong" if MONEY_UNIT.search(text) else "noise"
        else:
            strength = "medium"

        amount_raw, amount = (amounts[0] if amounts else ("", None))
        cand = Candidate(at=row.at, text=text, amount=amount, amount_raw=amount_raw,
                         cues=strong_hits or [c for c in WEAK_CUES if c in text],
                         speaker=row.speaker)
        cand.strength = strength
        # [Q915] 标签优先：手表正式 ASR 的副本带 source_label/acoustic_proximity，
        # 作业包副本没有。有标签就**用标签**，没有才退回关键词猜（并标明是猜的）。
        cand.source_label = row.source_label or "unknown"
        cand.acoustic_proximity = row.acoustic_proximity or "unknown"
        if row.source_label == "non_self" or row.acoustic_proximity == "far":
            cand.external_hint = "non_self"
        elif row.source_label == "self" or row.acoustic_proximity == "near":
            cand.external_hint = "self"

        context = _context_text(ordered, index)
        if any(cue in text for cue in EXTERNAL_CUES):
            if cand.external_hint in {"unknown"}:
                cand.external_hint = "keyword"   # 没有标签，只能靠词猜
            cand.flags.append("external_playback")
        if any(cue in text for cue in QUESTION_CUES):
            cand.flags.append("question")
        if any(cue in text for cue in HYPOTHETICAL_CUES):
            cand.flags.append("hypothetical")
        if any(cue in text for cue in THIRD_PARTY_CUES):
            cand.flags.append("third_party")
        if any(cue in text for cue in BARGAIN_CUES) or any(cue in context for cue in BARGAIN_CUES):
            cand.flags.append("bargain")
        if any(cue in text for cue in NEWS_CUES) or any(cue in context for cue in NEWS_CUES):
            cand.flags.append("news")
        if len(_norm(text)) < 5:
            cand.flags.append("fragment")
        if amounts and not MONEY_UNIT.search(amount_raw or ""):
            cand.flags.append("unit_missing")
        if amount is None:
            cand.flags.append("no_amount")
        out.append(cand)
    return out


def _context_text(ordered: list[Row], index: int) -> str:
    """±15 秒内的相邻句子（用来看"这句是在什么场景里说的"）。"""
    anchor = ordered[index].at
    parts = []
    for offset in (-2, -1, 1, 2):
        pos = index + offset
        if 0 <= pos < len(ordered):
            other = ordered[pos]
            if abs((other.at - anchor).total_seconds()) <= CONTEXT_WINDOW_SECONDS:
                parts.append(other.text)
    return " ".join(parts)


# ─────────────────────────────────────────────────────────────── 账本 / 驳回
def _load_token(token_file: Path) -> str:
    import os
    from_env = str(os.environ.get(TOKEN_ENV) or "").strip()
    if from_env:
        return from_env
    try:
        payload = json.loads(Path(token_file).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    return str((payload or {}).get("mobile_access_token") or "").strip()


def load_ledger(date: str, *, base_url: str = DEFAULT_BASE_URL, token: str = "",
                timeout: float = 60.0, opener=None) -> dict[str, Any]:
    """只读 GET /api/daily，取 expense_ledger。失败时抛 RuntimeError（不静默当 0）。"""
    url = f"{base_url.rstrip('/')}/api/daily?date={date}"
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    open_fn = opener or urllib.request.urlopen
    try:
        with open_fn(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.HTTPError, urllib.error.URLError, OSError, ValueError) as error:
        raise RuntimeError(f"{date}: 读 /api/daily 失败（{type(error).__name__}）") from None
    ledger = payload.get("expense_ledger")
    if not isinstance(ledger, dict):
        raise RuntimeError(f"{date}: /api/daily 没有 expense_ledger")
    return ledger


def load_rejections(web_root: Path, date: str) -> list[dict[str, Any]]:
    """已驳回 = ledger-review.json 里 action=exclude 且 date 匹配的决定。"""
    path = web_root / "ledger-review.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    decisions = payload.get("decisions")
    if not isinstance(decisions, dict):
        return []
    out = []
    for key, value in decisions.items():
        if not isinstance(value, dict):
            continue
        if str(value.get("date") or "") != date:
            continue
        if str(value.get("action") or "").lower() not in {"exclude", "reject", "rejected"}:
            continue
        out.append({"key": key, **{k: value.get(k) for k in ("amount", "quote", "basis", "decided_at")}})
    return out


# ─────────────────────────────────────────────────────────────── 匹配
_PUNCT = re.compile(r"[\s，。！？、；：,.!?;:\"'“”‘’（）()【】\[\]…—\-~·]")


def _norm(text: str) -> str:
    return _PUNCT.sub("", str(text or ""))


def _overlap(a: str, b: str) -> float:
    """字符二元组重合率（对中文短句比编辑距离稳）。"""
    a, b = _norm(a), _norm(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if a in b or b in a:
        return min(len(a), len(b)) / max(len(a), len(b))
    grams_a = {a[i:i + 2] for i in range(len(a) - 1)}
    grams_b = {b[i:i + 2] for i in range(len(b) - 1)}
    if not grams_a or not grams_b:
        return 0.0
    return len(grams_a & grams_b) / len(grams_a | grams_b)


def _amount_agree(cand: float | None, booked: float | None) -> str:
    if cand is None or booked is None:
        return "unknown"
    if abs(cand - booked) < 0.01:
        return "exact"
    tolerance = max(1.0, abs(booked) * 0.10)
    if abs(cand - booked) <= tolerance:
        return "close"
    return "mismatch"


def _find_booked_twin(cand: Candidate, booked: list[tuple[str, dict]]) -> dict | None:
    """同一笔被说了两遍（「四块是吧」vs 账本「收钱吧，到账四元」）：金额相等 + 20 分钟内。"""
    if cand.amount is None:
        return None
    for kind, row in booked:
        if kind != "booked" or row.get("amount") is None:
            continue
        try:
            if abs(float(row["amount"]) - float(cand.amount)) > 0.01:
                continue
        except (TypeError, ValueError):
            continue
        clock = str(row.get("clock") or "")
        try:
            hour, minute = (int(part) for part in clock.split(":")[:2])
        except ValueError:
            continue
        booked_at = cand.at.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if abs((cand.at - booked_at).total_seconds()) <= 20 * 60:
            return row
    return None


def match_candidates(candidates: list[Candidate], ledger: dict[str, Any],
                     rejections: list[dict[str, Any]], *, threshold: float = 0.34) -> None:
    """把候选和 入账/待确认/已驳回 逐条对上，写回 Candidate.verdict/reason。"""
    booked = []
    for tx in (ledger.get("payment_transactions") or []):
        if isinstance(tx, dict):
            booked.append(("booked", tx))
    for tx in (ledger.get("pending_transactions") or []):
        if isinstance(tx, dict):
            pending = dict(tx)
            pending.setdefault("amount", tx.get("amount"))
            booked.append(("pending", pending))
    for item in rejections:
        booked.append(("rejected", item))

    used: set[int] = set()
    for cand in candidates:
        best: tuple[float, int, str] | None = None
        for index, (kind, row) in enumerate(booked):
            quote = str(row.get("quote") or row.get("title") or "")
            score = _overlap(cand.text, quote)
            if cand.amount is not None and row.get("amount") is not None:
                agree = _amount_agree(cand.amount, float(row["amount"]))
                score += {"exact": 0.25, "close": 0.10, "mismatch": -0.35}.get(agree, 0.0)
            if score > threshold and (best is None or score > best[0]):
                best = (score, index, kind)
        if best is None:
            # 同笔重复说：金额相同 + 时间在 ±20 分钟内已有一条入账 → 视为"同一笔已入账"
            twin = _find_booked_twin(cand, booked)
            if twin is not None:
                cand.verdict = "booked"
                cand.match = {"score": None, "clock": twin.get("clock"),
                              "amount": twin.get("amount"), "title": twin.get("title"),
                              "quote": str(twin.get("quote") or "")[:80],
                              "amount_agree": "exact", "same_purchase": True}
                cand.reason = (f"已入账（同一笔重复说）：{twin.get('clock')} 已记 "
                               f"「{str(twin.get('title') or '')[:24]}」{twin.get('amount')}")
                continue
            cand.verdict = "candidate"
            cand.reason = "当天账本/待确认/已驳回里没有对得上的记录"
            continue
        score, index, kind = best
        row = booked[index][1]
        used.add(index)
        agree = _amount_agree(cand.amount, row.get("amount") if row.get("amount") is not None else None)
        cand.verdict = kind
        cand.match = {
            "score": round(score, 3),
            "clock": row.get("clock"),
            "amount": row.get("amount"),
            "title": row.get("title"),
            "quote": str(row.get("quote") or "")[:80],
            "amount_agree": agree,
        }
        label = {"booked": "已入账", "pending": "待确认", "rejected": "已驳回"}[kind]
        extra = "" if agree in {"exact", "unknown"} else f"（金额不一致：原话 {cand.amount} / 账本 {row.get('amount')}）"
        cand.reason = f"{label}：相似度 {score:.2f}，账本「{str(row.get('title') or '')}」{extra}"



# ═══════════════════════════════════════════════════════════════ 饮食四项（--diet）
#
# 只报告，不进"需要我判断"。四项检查的阈值都写在常量里，改阈值只改这里。
#
# 为什么克数检查在**账本内部**就能做：nutrition_ledger 的每条记录都带
#   `quote`（原话）+ `portion`（份量原文）+ `weight_g`（落到计算里的克数），
# 所以"原话说三百克、weight_g 记 100"这种不需要转写就能抓。
# 转写只在"漏记"这一项用得上（要说的话根本没进账本，账本里当然没有 quote）。

#: 完美体吃/喝：「吃了」「喝了」「刚吃」——单说「好吃」「多吃点」不算一次进食
DIET_PERFECT_CUES = ("吃了", "喝了", "吃了个", "喝了个", "吃完了", "喝完了", "刚吃", "刚喝",
                     "吃完", "喝完", "吃了一", "喝了一", "吃了点", "喝了点", "吃了两",
                     "吃了三", "喝了半", "吃下", "喝下", "干了一碗", "干了一瓶")
#: 数量词：有它才能把「吃一口」和「好吃」分开
DIET_QUANTITY = ("克", "碗", "杯", "份", "瓶", "袋", "盒", "串", "勺", "斤",
                 "千卡", "大卡", "毫升", "ml")
#: 第三方吃：他吃了一碗 / 她刚喝
DIET_THIRD_PERSON = ("他吃", "她吃", "他喝", "她喝", "他们吃", "她们吃", "别人吃")

DIET_EAT_CUES = ("吃", "喝", "嚼", "尝", "咬", "干了一碗", "下肚", "早", "午饭", "晚饭",
                 "早餐", "午餐", "晚餐", "夜宵", "零食", "饮料", "千卡", "大卡", "卡路里")
#: 「买菜 / 点单 / 计划」不是"已经吃进肚子"，不算漏记
DIET_NOT_EATEN_CUES = ("买", "采购", "下单", "点单", "叫了外卖", "打算", "准备", "计划",
                       "明天", "等会", "晚点", "下次", "要不要", "想买", "想吃", "种子",
                       "优惠", "食谱", "做法", "热量是多少")
#: 克数允许的偏差（超过就报）
WEIGHT_TOLERANCE_RATIO = 0.05
WEIGHT_TOLERANCE_ABS_G = 5.0
#: 合理性阈值（派单指定）
CALORIE_MACRO_TOLERANCE = 0.25     # 4C+4P+9F 与标称热量偏差 >25% 报
KCAL_PER_100G_LIMIT = 900.0        # 每 100 克超过 900 千卡报
SINGLE_RECORD_KCAL_LIMIT = 2000.0  # 单条超过 2000 千卡报

#: 烹饪/配方记录：里面的克数是食材用量，不能拿去和"吃了多少"对
COOKING_CUES = ("制作", "做法", "食谱", "配方", "烹饪", "下厨")
#: 含酒精的饮品：4C+4P+9F **不含酒精的 7 千卡/克**，热量核对一定对不上，跳过并单列
ALCOHOL_CUES = ("酒", "威士忌", "啤", "白酒", "红酒", "嗨棒", "清酒", "烧酒", "米酒")

AGGREGATE_GRAM = re.compile(r"\d+\s*(?:g|G|克)")
GRAM = re.compile(r"([0-9零一二三四五六七八九十百千两]+)\s*(?:克|g|G|GRAM|grams|毫升|ml|ML)")


def parse_grams(text: str) -> float | None:
    """从原话/份量里抽克数：「三百克」→300、「五十克」→50、「330ml」→330。"""
    if not text:
        return None
    for match in GRAM.finditer(str(text)):
        raw = match.group(1)
        try:
            value = float(raw)
        except ValueError:
            value = cn_to_number(raw)
        if value and 0 < value < 20000:
            return float(value)
    return None


def load_daily_payload(date: str, *, daily_source: str | None = None,
                       base_url: str = DEFAULT_BASE_URL, token: str = "") -> dict[str, Any]:
    """取某天的 /api/daily 响应体：`--daily-source <dir>` 读快照（*.daily.json.gz），否则读线上。"""
    if daily_source:
        root = Path(daily_source)
        for candidate in (root / f"{date}.daily.json.gz", root / f"{date}.daily.json"):
            if not candidate.exists():
                continue
            raw = (gzip.open(candidate, "rb").read() if candidate.suffix == ".gz"
                   else candidate.read_bytes())
            payload = json.loads(raw.decode("utf-8"))
            # 快照存的是 {status, body} 包装；直接是 body 的也兼容
            if isinstance(payload, dict) and "body" in payload and isinstance(payload["body"], dict):
                return payload["body"]
            if isinstance(payload, dict):
                return payload
            raise RuntimeError(f"{date}: 快照格式不认识（{candidate.name}）")
        raise RuntimeError(f"{date}: 快照目录里没有这个日期（{root}）")
    return load_ledger(date, base_url=base_url, token=token) and _load_whole_daily(
        date, base_url=base_url, token=token)


def _load_whole_daily(date: str, *, base_url: str = DEFAULT_BASE_URL, token: str = "") -> dict[str, Any]:
    url = f"{base_url.rstrip('/')}/api/daily?date={date}"
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.HTTPError, urllib.error.URLError, OSError, ValueError) as error:
        raise RuntimeError(f"{date}: 读 /api/daily 失败（{type(error).__name__}）") from None
    if not isinstance(payload, dict):
        raise RuntimeError(f"{date}: /api/daily 返回的不是对象")
    return payload


def nutrition_records(payload: dict) -> list[dict[str, Any]]:
    """把 nutrition_ledger 里所有记录摊平（按 event_id+item 去重，保留 meals 里的那份）。"""
    ledger = payload.get("nutrition_ledger")
    if not isinstance(ledger, dict):
        raise RuntimeError("没有 nutrition_ledger")
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    ordered: list[dict[str, Any]] = []
    for meal, items in (ledger.get("meals") or {}).items():
        for item in (items or []):
            if isinstance(item, dict):
                ordered.append({**item, "_meal": meal})
    for key in ("food_records", "other_records", "purchases"):
        for item in (ledger.get(key) or []):
            if isinstance(item, dict):
                ordered.append({**item, "_bucket": key})
    for item in ordered:
        key = (str(item.get("event_id") or ""), str(item.get("item") or ""))
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def _strip_brackets(text: str) -> str:
    return re.sub(r"[（(\[［].*?[)）\]］]", "", str(text or "")).strip()


def _longest_common(a: str, b: str) -> int:
    """最长公共子串长度（短句够用，不做 DP 优化）。"""
    best = 0
    for start in range(len(a)):
        for end in range(start + best + 1, len(a) + 1):
            if a[start:end] in b:
                best = end - start
            else:
                break
    return best


def check_weights(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """② 克数：原话/份量里的克数 vs weight_g。"""
    findings = []
    for record in records:
        blob = f"{record.get('item') or ''}{record.get('title') or ''}"
        # 只对**条目名里带 ≥2 个克数**的汇总行跳过（「蒸土豆300克」的 title 里那个 300 克不算汇总）
        if len(AGGREGATE_GRAM.findall(str(record.get("item") or ""))) >= 2:
            # 「土豆300g、腊肠50g、蒸毛豆仁100g」这种汇总行，weight_g 是几项之和，
            # 用单项原话对它必错——跳过（不是忽略，是这里没有可比对象）
            continue
        if any(cue in blob for cue in COOKING_CUES) and not record.get("weight_g"):
            # 「制作沙茶红肠粉丝汤 200 克」= 做法里的食材量，不是吃掉的量
            continue
        stated = parse_grams(record.get("quote")) or parse_grams(record.get("portion"))
        weight = record.get("weight_g")
        if stated is None:
            continue
        if weight in (None, 0):
            findings.append({
                "kind": "weight_missing", "item": record.get("item"),
                "quote": record.get("quote"), "stated_g": stated, "weight_g": weight,
                "reason": f"原话说 {stated:g} 克，但 weight_g 是空的（没有落到计算里）",
            })
            continue
        weight = float(weight)
        diff = abs(stated - weight)
        if diff > max(WEIGHT_TOLERANCE_ABS_G, weight * WEIGHT_TOLERANCE_RATIO):
            findings.append({
                "kind": "weight_mismatch", "item": record.get("item"),
                "quote": record.get("quote"), "stated_g": stated, "weight_g": weight,
                "ratio": round(stated / weight, 2) if weight else None,
                "reason": f"原话 {stated:g} 克 / weight_g {weight:g} 克（差 {diff:g} 克，"
                          f"超容差 {WEIGHT_TOLERANCE_RATIO:.0%} 且 >{WEIGHT_TOLERANCE_ABS_G:g} 克）",
            })
    return findings


def check_categories(records: list[dict[str, Any]], *, threshold: float = 0.34) -> list[dict[str, Any]]:
    """③ 品类：记录的食物名 vs 营养库命中的条目名。"""
    findings = []
    for record in records:
        item = str(record.get("item") or "").strip()
        matched = str(record.get("nutrition_matched_name") or "").strip()
        if not item or not matched:
            continue
        plain_item = _strip_brackets(item)
        plain_matched = _strip_brackets(matched)
        # 包含关系算命中：「咖啡」→「咖啡（酿造、用水制备）」是**同一个东西**，
        # 只是营养库条目名更长；按二元组重合率算会误判成错配。
        if plain_item in plain_matched or plain_matched in plain_item:
            continue
        # 共同食物词够长也算命中：「咖啡+椰乳」→「菲诺厚椰乳」共享「椰乳」
        common = _longest_common(plain_item, plain_matched)
        shorter = min(len(plain_item), len(plain_matched)) or 1
        if common >= 2 and common / shorter >= 0.4:
            continue
        score = _overlap(item, matched)
        if score >= threshold:
            continue
        findings.append({
            "kind": "category_mismatch", "item": item, "matched": matched,
            "score": round(score, 2),
            "source": record.get("nutrition_source"),
            "source_label": record.get("nutrition_source_label"),
            "quote": record.get("quote"),
            "reason": f"「{item}」命中营养库「{matched}」（重合 {score:.2f} < {threshold}），"
                      f"来源 {record.get('nutrition_source_label') or record.get('nutrition_source') or '未标'}",
        })
    return findings


def check_plausibility(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """④ 合理性：4C+4P+9F 偏差、每 100 克热量、单条热量。"""
    findings = []
    for record in records:
        item = record.get("item")
        calories = record.get("calories_kcal")
        nutrients = record.get("nutrients") or {}
        known = record.get("nutrient_known") or {}
        weight = record.get("weight_g")

        if calories is not None:
            calories = float(calories)
            if calories > SINGLE_RECORD_KCAL_LIMIT:
                findings.append({
                    "kind": "single_record_kcal", "item": item, "calories_kcal": calories,
                    "reason": f"单条 {calories:g} 千卡 > 上限 {SINGLE_RECORD_KCAL_LIMIT:g}",
                })
            if weight and float(weight) > 0:
                per100 = calories / float(weight) * 100.0
                if per100 > KCAL_PER_100G_LIMIT:
                    findings.append({
                        "kind": "kcal_per_100g", "item": item, "calories_kcal": calories,
                        "weight_g": float(weight), "kcal_per_100g": round(per100, 1),
                        "reason": f"{calories:g} 千卡 / {float(weight):g} 克 = {per100:.0f} 千卡每 100 克"
                                  f" > {KCAL_PER_100G_LIMIT:g}",
                    })

        carbs, protein, fat = (nutrients.get("carbohydrates_g"), nutrients.get("protein_g"),
                               nutrients.get("fat_g"))
        if calories is None or None in (carbs, protein, fat):
            continue
        if not all(known.get(k, True) for k in ("carbohydrates_g", "protein_g", "fat_g")):
            continue
        computed = 4 * float(carbs) + 4 * float(protein) + 9 * float(fat)
        if calories <= 0:
            continue
        if any(cue in f"{item or ''}{record.get('quote') or ''}" for cue in ALCOHOL_CUES):
            # 酒精 7 千卡/克不在 4C+4P+9F 里，这类一定"偏差过大"，属口径限制，不报
            continue
        deviation = abs(computed - calories) / calories
        if deviation > CALORIE_MACRO_TOLERANCE:
            findings.append({
                "kind": "macro_calorie_deviation", "item": item,
                "calories_kcal": calories, "computed_kcal": round(computed, 1),
                "deviation": round(deviation, 3),
                "macros": {"carbs": carbs, "protein": protein, "fat": fat},
                "reason": f"4×{carbs}+4×{protein}+9×{fat} = {computed:.0f} 千卡，"
                          f"与标称 {calories:g} 相差 {deviation:.0%} > {CALORIE_MACRO_TOLERANCE:.0%}",
            })
    return findings


DUPLICATE_QUOTE_OVERLAP = 0.5


def check_duplicates(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """重复：同一餐里、同一样食物、原话重叠 → 同一条被记了两次。

    判据：同一 `_meal` 桶 + 同名 `item` + 原话互相包含或重合 ≥ 0.5。
    （10-26 的「中午吃了一个汉堡」/「吃了中午吃了一个汉堡」就是同一句被 ASR 重复起头的两遍。）
    """
    findings = []
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for record in records:
        meal = str(record.get("_meal") or record.get("_bucket") or "")
        item = str(record.get("item") or "").strip()
        if not item or record.get("record_type") not in (None, "consumed"):
            continue
        grouped.setdefault((meal, item), []).append(record)

    for (meal, item), group in grouped.items():
        if len(group) < 2:
            continue
        reported: set[int] = set()
        for i in range(len(group)):
            if i in reported:
                continue
            quote_i = str(group[i].get("quote") or "")
            twins = [j for j in range(i + 1, len(group)) if j not in reported]
            hits = []
            for j in twins:
                quote_j = str(group[j].get("quote") or "")
                if not quote_i or not quote_j:
                    continue
                if quote_i in quote_j or quote_j in quote_i or _overlap(quote_i, quote_j) >= DUPLICATE_QUOTE_OVERLAP:
                    hits.append(j)
            if not hits:
                continue
            reported.add(i)
            reported.update(hits)
            members = [group[i]] + [group[j] for j in hits]
            kcals = [m.get("calories_kcal") for m in members]
            findings.append({
                "kind": "duplicate_record", "item": item, "meal": meal,
                "event_ids": [m.get("event_id") for m in members],
                "quotes": [str(m.get("quote") or "")[:40] for m in members],
                "calories_kcal": kcals,
                "reason": f"{meal} 里「{item}」被记了 {len(members)} 次（原话重叠），"
                          f"热量 {kcals}；若不重复，当天热量被多算约 "
                          f"{sum(k for k in kcals[1:] if isinstance(k, (int, float))) or 0:g} 千卡",
            })
    return findings


_FOOD_VOCAB: set[str] | None = None


def food_vocab(web_root: Path = DEFAULT_WEB_ROOT) -> set[str]:
    """营养库里的食物名词表（foodwake + cfct + brand）。

    为什么要它：光看"吃/喝"两个字，一天的闲聊能凑出二三十条假候选
    （「好吃」「多吃点」「不吃猪杂」）。要求句子里**出现库里真实存在的食物名**，
    才能把"说了一句关于吃的话"和"报了一笔吃进去的东西"分开。
    """
    global _FOOD_VOCAB
    if _FOOD_VOCAB is not None:
        return _FOOD_VOCAB
    names: set[str] = set()
    for filename in ("foodwake.json", "cfct.json", "brand-library.json"):
        path = Path(web_root) / "food-data" / filename
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        entries = payload.get("entries") if isinstance(payload, dict) else payload
        if isinstance(entries, dict):
            entries = list(entries.values())
        for entry in (entries or []):
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name") or entry.get("item") or "").strip()
            if 2 <= len(name) <= 8:
                names.add(name)
                names.add(_strip_brackets(name))
    _FOOD_VOCAB = {n for n in names if len(n) >= 2}
    return _FOOD_VOCAB


def food_hits(text: str, vocab: set[str]) -> list[str]:
    """句子命中了哪些库里的食物名（按 2~8 字子串查表，避免逐个名字扫全文）。"""
    hits = []
    length = len(text)
    for start in range(length):
        for size in range(2, 9):
            chunk = text[start:start + size]
            if len(chunk) < 2:
                break
            if chunk in vocab:
                hits.append(chunk)
                break
    return sorted(set(hits), key=len, reverse=True)


def check_missing(rows: list[Row], records: list[dict[str, Any]], *,
                  threshold: float = 0.34, web_root: Path = DEFAULT_WEB_ROOT) -> list[dict[str, Any]]:
    """① 漏记：本人现场说"吃/喝/千卡"但账本里没有对应记录。

    只认**本人 + 现场**：`source_label=non_self` 或 `acoustic=far` 的跳过（别人/外放），
    「买菜/点单/计划/明天」跳过（还没吃），问句跳过。
    """
    quotes = [str(r.get("quote") or "") for r in records if r.get("quote")]
    items = [str(r.get("item") or "") for r in records if len(str(r.get("item") or "")) >= 2]
    vocab = food_vocab(web_root)
    findings = []
    for row in rows:
        text = row.text
        if any(cue in text for cue in DIET_NOT_EATEN_CUES):
            continue
        if any(cue in text for cue in DIET_THIRD_PERSON):
            continue
        if any(cue in text for cue in QUESTION_CUES):
            continue
        if row.source_label == "non_self" or row.acoustic_proximity == "far":
            continue
        # ① 必须是"已经吃了/喝了"，不是"好吃""多吃点""想吃"
        perfect = any(cue in text for cue in DIET_PERFECT_CUES)
        quantity = any(cue in text for cue in DIET_QUANTITY)
        if not perfect and not (quantity and ("吃" in text or "喝" in text)):
            continue
        # ② 必须点到具体的食物（库里有名字），否则只是聊"吃饭"
        hits = food_hits(text, vocab)
        if not hits:
            continue
        # ③ 账本里已经有这条 → 不算漏
        if any(item in text for item in items):
            continue
        if any(_overlap(text, quote) >= threshold for quote in quotes):
            continue
        findings.append({
            "kind": "missing_record", "at": row.at.strftime("%H:%M:%S"), "text": text,
            "food_hits": hits[:4],
            "source_label": row.source_label or "unknown",
            "acoustic_proximity": row.acoustic_proximity or "unknown",
            "reason": f"本人现场说吃了/喝了「{'、'.join(hits[:3])}」，但 nutrition_ledger 里没有对得上的记录",
        })
    return findings


def run_diet(date: str, *, daily_source: str | None = None, web_root: Path = DEFAULT_WEB_ROOT,
             base_url: str = DEFAULT_BASE_URL, token: str = "",
             with_transcript: bool = True) -> dict[str, Any]:
    payload = load_daily_payload(date, daily_source=daily_source, base_url=base_url, token=token)
    records = nutrition_records(payload)
    findings = {
        "missing": [], "weight": check_weights(records),
        "category": check_categories(records), "plausibility": check_plausibility(records),
        "duplicate": check_duplicates(records),
    }
    rows: list[Row] = []
    if with_transcript:
        rows, _ = load_rows(date, web_root)
        findings["missing"] = check_missing(rows, records, web_root=web_root)
    ledger = payload.get("nutrition_ledger") or {}
    return {
        "date": date,
        "records": len(records),
        "transcript_rows": len(rows),
        "ledger_totals": ledger.get("totals"),
        "counts": {k: len(v) for k, v in findings.items()},
        "findings": findings,
    }


def print_diet(result: dict[str, Any]) -> None:
    counts = result["counts"]
    print(f"\n=== {result['date']} 饮食核对  "
          f"记录 {result['records']} 条 / 转写 {result['transcript_rows']} 句  →  "
          f"漏记 {counts['missing']} | 克数 {counts['weight']} | "
          f"品类 {counts['category']} | 合理性 {counts['plausibility']} | "
          f"重复 {counts['duplicate']}")
    labels = {"missing": "① 漏记", "weight": "② 克数", "category": "③ 品类",
              "plausibility": "④ 合理性", "duplicate": "⑤ 重复（同餐同物说了两遍）"}
    for key, title in labels.items():
        items = result["findings"].get(key) or []
        if not items:
            continue
        print(f"  -- {title}（{len(items)}）")
        for item in items:
            head = item.get("at") or ""
            name = item.get("item") or item.get("text") or ""
            print(f"     {head:8} {str(name)[:40]:42} {item['reason']}")


# ─────────────────────────────────────────────────────────────── 输出
OBJECT_CLASSIFIERS = ("个", "包", "瓶", "条", "份", "斤", "箱", "袋", "杯", "碗",
                      "盒", "张", "支", "根", "双", "件", "台", "本")
FIRST_PERSON_CUES = ("我", "俺")


def rank_candidates(candidates: list[Candidate], ledger: dict[str, Any]) -> None:
    """给每条候选打分（**只用于排序**，不做过滤、不改判定）。

    分数说话的"值不值得人看一眼"：
      +3 说了带单位的钱   +1 只有动词     +2 这个金额账本里没有（漏账嫌疑）
      +2 第一人称         +1 带了量词宾语  -3 疑似外放/他人 -2 没说金额
      -3 与已入账记录在 5 分钟内（很可能是同一笔的碎片）
    """
    booked_amounts = {round(float(t.get("amount") or 0), 2)
                      for t in (ledger.get("payment_transactions") or []) if isinstance(t, dict)}
    booked_times = []
    for tx in (ledger.get("payment_transactions") or []):
        if not isinstance(tx, dict):
            continue
        try:
            hour, minute = (int(part) for part in str(tx.get("clock") or "").split(":")[:2])
        except ValueError:
            continue
        booked_times.append((hour, minute))

    for cand in candidates:
        score = 0.0
        score += 3 if cand.strength == "strong" else 1
        if cand.amount is not None and round(cand.amount, 2) not in booked_amounts:
            score += 2
        if any(cue in cand.text for cue in FIRST_PERSON_CUES):
            score += 2
        if any(kind in cand.text for kind in OBJECT_CLASSIFIERS):
            score += 1
        if cand.amount is None:
            score -= 2
        if cand.external_hint == "non_self":
            score -= 5
        elif cand.external_hint == "keyword":
            score -= 3
        elif cand.external_hint == "self":
            score += 2
        if len(_norm(cand.text)) < 8:
            score -= 2
        hour, minute = cand.at.hour, cand.at.minute
        for booked_hour, booked_minute in booked_times:
            gap = abs((hour * 60 + minute) - (booked_hour * 60 + booked_minute))
            if gap <= 5:
                score -= 3
                break
        cand.rank_score = round(score, 1)
        cand.rank_reason = _rank_reason(cand, booked_amounts)


def _rank_reason(cand: Candidate, booked_amounts: set[float]) -> str:
    bits = []
    if cand.strength == "strong":
        bits.append("说了带单位的钱")
    if cand.amount is not None and round(cand.amount, 2) not in booked_amounts:
        bits.append("金额账本里没有")
    if cand.amount is None:
        bits.append("没说金额")
    if cand.external_hint == "non_self":
        bits.append("标签判非本人/远场")
    elif cand.external_hint == "keyword":
        bits.append("疑似外放（仅关键词，无标签）")
    if cand.amount is not None and round(cand.amount, 2) in booked_amounts:
        bits.append("金额已入账")
    return "；".join(bits) or "一般"


#: 「严格模式」的额外排除：这些信号说明说话人在**问别人**或**转述新闻**，不是自己花钱
SECOND_PERSON = ("你", "您")
NEGOTIATION_CONTEXT = ("投诉", "退款", "赔偿", "协议", "报警", "活动", "优惠", "薅",
                       "报销", "手续费", "价格", "报价", "套餐")


def strict_keep(cand: Candidate, *, max_amount: float = 1000.0) -> tuple[bool, str]:
    """把"人一眼就想跳过"的候选剔掉（保守：宁可漏，不制造假漏账）。

    返回 (是否保留, 理由)。**不改判定、不改账本**，只决定要不要出现在"请你看这 10 条"里。
    """
    if cand.external_hint == "non_self":
        return False, "标签判为他人/远场（source_label=non_self 或 acoustic=far）"
    hard = HARD_FP_FLAGS & set(cand.flags)
    if hard:
        return False, f"规则判误报：{','.join(sorted(hard))}"
    if cand.amount is not None and cand.amount >= max_amount:
        return False, f"金额 {cand.amount:g} 元过大，多半不是一笔日常消费"
    text = cand.text
    if any(cue in text for cue in SECOND_PERSON) and "到账" not in text:
        return False, "句子里在问/跟对方说话（第二人称）"
    if any(cue in text for cue in NEGOTIATION_CONTEXT):
        return False, "谈价/报销/活动类上下文"
    if cand.rank_score < 0:
        return False, f"排序分为 {cand.rank_score:g}（偏低）"
    return True, "保留"


def summarize(candidates: list[Candidate]) -> dict[str, int]:
    """命中数 / 已入账 / 待确认 / 已驳回 / **未入账候选** / 噪音，分开数。

    噪音（noise）不计入候选：它们只有「花/费/买」这类弱线索，人工看是浪费。
    """
    counts = {"all": len(candidates), "booked": 0, "pending": 0, "rejected": 0,
              "candidate": 0, "likely_fp": 0, "noise": 0, "strong": 0, "medium": 0,
              "strict": 0, "labeled": 0, "non_self": 0,
              "candidate_self": 0, "candidate_non_self": 0, "candidate_unknown": 0}
    for cand in candidates:
        if cand.strength == "noise":
            counts["noise"] += 1
            continue
        counts[cand.strength] = counts.get(cand.strength, 0) + 1
        if cand.verdict != "candidate":
            counts[cand.verdict] = counts.get(cand.verdict, 0) + 1
            continue
        # 未入账的再分两堆：干净候选（值得人看）/ 规则已判为误报（新闻、谈价、疑问、他人、碎片）
        if HARD_FP_FLAGS & set(cand.flags):
            counts["likely_fp"] += 1
        else:
            counts["candidate"] += 1
        if strict_keep(cand)[0]:
            counts["strict"] += 1
        if cand.source_label != "unknown" or cand.acoustic_proximity != "unknown":
            counts["labeled"] += 1
        if cand.external_hint == "non_self":
            counts["non_self"] += 1
        if not (HARD_FP_FLAGS & set(cand.flags)):
            key = ("candidate_non_self" if cand.external_hint == "non_self"
                   else "candidate_self" if cand.external_hint == "self"
                   else "candidate_unknown")
            counts[key] = counts.get(key, 0) + 1
    return counts


def format_table(candidates: list[Candidate], *, limit: int | None = None,
                strict: bool = False) -> str:
    lines = []
    order = {"candidate": 0, "pending": 1, "rejected": 2, "booked": 3}
    live = [c for c in candidates if c.strength != "noise"]
    if strict:
        live = [c for c in live if c.verdict == "candidate" and strict_keep(c)[0]]
    shown = sorted(live, key=lambda c: (order.get(c.verdict, 9), -c.rank_score, c.at))
    if limit:
        shown = shown[:limit]
    for cand in shown:
        flags = ",".join(cand.flags) or "-"
        amount = "" if cand.amount is None else f"{cand.amount:g}"
        lines.append(
            f"{cand.at.strftime('%H:%M:%S')}  {cand.rank_score:>5}  {cand.verdict:9}  {amount:>8}  "
            f"{cand.speaker or '-':>6}  外放={cand.external_hint:8}  [{flags}]  "
            f"{cand.text[:60]}"
        )
        if cand.reason:
            lines.append(f"            └ {cand.reason}")
    return "\n".join(lines)


def run_date(date: str, *, web_root: Path = DEFAULT_WEB_ROOT, base_url: str = DEFAULT_BASE_URL,
             token: str = "", threshold: float = 0.34) -> dict[str, Any]:
    rows, stats = load_rows(date, web_root)
    candidates = extract_candidates(rows)
    ledger = load_ledger(date, base_url=base_url, token=token)
    rejections = load_rejections(web_root, date)
    match_candidates(candidates, ledger, rejections, threshold=threshold)
    rank_candidates(candidates, ledger)
    counts = summarize(candidates)
    return {
        "date": date,
        "rows": stats,
        "ledger": {
            "total_amount": ledger.get("total_amount"),
            "payment_count": ledger.get("payment_transaction_count"),
            "pending_count": ledger.get("pending_count"),
            "booked_quotes": [str(t.get("quote") or "") for t in (ledger.get("payment_transactions") or [])],
        },
        "rejected_count": len(rejections),
        "counts": counts,
        "candidates": [c.as_dict() for c in candidates],
        "strict": [
            {"at": c.at.strftime("%H:%M:%S"), "text": c.text, "amount": c.amount,
             "rank_score": c.rank_score, "keep": strict_keep(c)[0], "why": strict_keep(c)[1]}
            for c in candidates if c.strength != "noise" and c.verdict == "candidate"
        ],
        "_objects": candidates,   # 只给控制台表格用，写 JSON 前会摘掉
    }


def _iter_dates(start: str, end: str) -> list[str]:
    first = datetime.strptime(start, "%Y-%m-%d").replace(tzinfo=BJ_TZ)
    last = datetime.strptime(end, "%Y-%m-%d").replace(tzinfo=BJ_TZ)
    out = []
    while first <= last:
        out.append(first.strftime("%Y-%m-%d"))
        first += timedelta(days=1)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="对账线：扫转写原文 × 当天账本（只报告）")
    parser.add_argument("--date", action="append", default=None, help="YYYY-MM-DD，可重复")
    parser.add_argument("--from", dest="start", default=None)
    parser.add_argument("--to", dest="end", default=None)
    parser.add_argument("--web-root", default=str(DEFAULT_WEB_ROOT))
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--token-file", default=str(DEFAULT_WEB_ROOT / "secrets.json"))
    parser.add_argument("--json", default=None, help="把完整结果写成 JSON")
    parser.add_argument("--limit", type=int, default=None, help="每个日期最多打印多少条")
    parser.add_argument("--threshold", type=float, default=0.34)
    parser.add_argument("--diet", action="store_true",
                        help="饮食四项核对（漏记/克数/品类/合理性），只报告")
    parser.add_argument("--daily-source", default=None,
                        help="读快照目录（含 <date>.daily.json.gz）而不是线上 /api/daily")
    parser.add_argument("--no-transcript", action="store_true",
                        help="--diet 时跳过转写（只做账本内三项：克数/品类/合理性）")
    parser.add_argument("--strict", action="store_true",
                        help="只打印严格模式下保留的候选（每天争取 <10 条）")
    args = parser.parse_args(argv)

    if args.date:
        dates = list(args.date)
    elif args.start and args.end:
        dates = _iter_dates(args.start, args.end)
    else:
        print("需要 --date 或 --from/--to", file=sys.stderr)
        return 2

    token = _load_token(Path(args.token_file))
    web_root = Path(args.web_root)

    if args.diet:
        failures = 0
        collected = []
        for date in dates:
            try:
                result = run_diet(date, daily_source=args.daily_source, web_root=web_root,
                                  base_url=args.base_url, token=token,
                                  with_transcript=not args.no_transcript)
            except RuntimeError as error:
                print(f"[diet] {error}", file=sys.stderr)
                failures += 1
                continue
            collected.append(result)
            print_diet(result)
        if args.json:
            Path(args.json).write_text(json.dumps(collected, ensure_ascii=False, indent=1),
                                       encoding="utf-8")
            print(f"\n[diet] 完整结果已写入 {args.json}")
        return 1 if failures else 0
    results = []
    failures = 0
    for date in dates:
        try:
            result = run_date(date, web_root=web_root, base_url=args.base_url,
                              token=token, threshold=args.threshold)
        except RuntimeError as error:
            print(f"[reconcile] {error}", file=sys.stderr)
            failures += 1
            continue
        results.append(result)
        counts = result["counts"]
        print(f"\n=== {date}  "
              f"转写 {result['rows']['rows']} 句 / {result['rows']['files']} 文件"
              f"  →  命中 {counts['all']} 条 | 已入账 {counts['booked']} | "
              f"待确认 {counts['pending']} | 已驳回 {counts['rejected']} | "
              f"**未入账候选 {counts['candidate']}**"
              f"（本人 {counts['candidate_self']} / 非本人 {counts['candidate_non_self']} / 未知 {counts['candidate_unknown']}）"
              f" | 严格模式 {counts['strict']}")
        print(format_table(result["_objects"], limit=args.limit, strict=args.strict))

    if args.json:
        serializable = [{k: v for k, v in result.items() if not k.startswith("_")} for result in results]
        Path(args.json).write_text(json.dumps(serializable, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\n[reconcile] 完整结果已写入 {args.json}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
