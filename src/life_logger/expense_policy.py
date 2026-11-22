"""Conservative, evidence-first expense ledger policy.

Stable callable API
-------------------
``build_expense_ledger(events) -> dict[str, Any]``

The caller must pass the events of *one* decision branch (Jev **or** Clef)
at a time.  This module never merges two decision variants, never rewrites
``source_id`` / ``event_id``, and never edits historical output.  Duplicate
removal is expressed as an in-ledger read decision with provenance.

Money rules
-----------
* Arithmetic uses :class:`decimal.Decimal`; public JSON values are rounded
  only at the output boundary.
* A counted payment must be finite and strictly positive.
* Missing / non-positive / non-finite amounts are kept in
  ``pending_transactions`` instead of becoming zero.
* Missing amounts are recovered only from the original quote and the existing
  amount parser, never from a title alone.
* Currencies are separate buckets.  An explicit 美元/USD quote overrides a
  conflicting legacy ``currency`` field; a bare ``$`` is not assumed to mean
  USD.
* Withdrawals are tracked separately from consumption.  Transfers are treated
  as real payments unless separately classified as a withdrawal/refund.

The ledger output keeps ``total_amount`` / ``currency`` / ``category_breakdown``
for legacy consumers.  Those legacy totals mean **CNY payments only**; use
``totals_by_currency`` / ``category_totals_by_currency`` for real multi-currency
views.
"""

from __future__ import annotations

import itertools
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

from life_logger.fact_types import has_self_evidence
from life_logger.extract import _amount as _extract_amount
from life_logger.extract import _number

JOURNAL_TIMEZONE = ZoneInfo("Asia/Shanghai")
POLICY_VERSION = "expense-policy-v2"
MONEY_QUANT = Decimal("0.01")

# Aggregate summaries are reconciled only against nearby detail evidence.
RECONCILE_WINDOW_SECONDS = 300
CROSS_SOURCE_RECONCILE_WINDOW_SECONDS = 120
MAX_RECONCILE_CANDIDATES = 8
MAX_RECONCILE_SUBSET = 6

_NON_FACT_STATES = {
    "plan",
    "planned",
    "planning",
    "intent",
    "intention",
    "intentional",
    "hypothetical",
    "hypothesis",
    "question",
    "query",
    "negated",
    "negation",
    "rejected",
    "reject",
    "cancelled",
    "canceled",
    "aborted",
    "future",
    "scheduled",
    "proposed",
    "draft",
}

_FACT_STATES = {
    "completed",
    "complete",
    "done",
    "paid",
    "payment",
    "payment_completed",
    "purchased",
    "observed",
    "ongoing",
    "paid_ongoing",
    "at_speech",
    "past",
}

_PLAN_RE = re.compile(
    r"(?:打算|准备|计划|想要|想买|要买|去买|在网上买|等会儿|等会|回头|"
    r"明天|下次|以后|改天|要不要|该不该|能不能|可不可以|如果|假如|要是|考虑)"
)
# Q404: 「能不能加个雪球」是点单时的口味请求，不等于未发生计划。
# 只有完整上下文能证明「本人明确点单 + 可追溯报价/金额」时才允许绕过
# `_PLAN_RE`/`_QUESTION_RE`；真正计划、假设询价、影视/非本人/POS 仍走原规则。
_STRONG_ORDER_PLAN_RE = re.compile(
    r"(?:打算|准备|计划|想要|想买|要买|去买|在网上买|等会儿|等会|回头|"
    r"明天|下次|以后|改天|要不要|该不该|可不可以|如果|假如|要是|考虑)"
)
_ORDER_ACTION_RE = re.compile(
    r"(?:要|来|点|加|拿|买)(?:了)?(?:一|两|三|几|\d+)?"
    r"(?:个|份|份儿|杯|碗|根|包|瓶|盒|袋|支|张)(?!\s*[吗呢?？])"
)
_ORDER_INTERROGATIVE_RE = re.compile(
    r"(?:要|来|点|加|拿|买)(?:了)?(?:一|两|三|几|\d+)?"
    r"(?:个|份|份儿|杯|碗|根|包|瓶|盒|袋|支|张)\s*[吗呢?？]"
)
_ORDER_PRICE_EVIDENCE_RE = re.compile(
    r"(?:多少钱|多少|一共|总共|合计|收你|只要|价格|块|元|说)"
)
_NEGATION_RE = re.compile(
    r"(?:不买了|不买|没买|没有买|不想买|不抽了|算了|不要了|取消购买|放弃购买|先不买)"
)
_QUESTION_RE = re.compile(
    r"(?:多少钱|多少|是不是|值不值|划算吗|可以吗|能买吗|有没有|哪个|怎么买)"
)
_CONFIRM_RE = re.compile(r"(?:对|是的|好的|行|OK|ok|没错)")
_COMPLETION_RE = re.compile(
    r"(?:买了|买过|刚买|已经买|购入|购买了|花了|花费|付了|付款|支付|实付|"
    r"消费了|下了单|下单了|给钱了|给了钱|结了账|买单|买到)"
)

_CURRENCY_MARKERS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "USD",
        re.compile(
            r"(?:美元|美金|\bUSD\b|\busd\b|\d+(?:\.\d+)?\s*刀|[零〇一二两三四五六七八九十百千]+\s*刀)"
        ),
    ),
    ("EUR", re.compile(r"(?:欧元|\bEUR\b|\beur\b)")),
    ("JPY", re.compile(r"(?:日元|日币|\bJPY\b|\bjpy\b)")),
    ("HKD", re.compile(r"(?:港币|港元|\bHKD\b|\bhkd\b)")),
    ("GBP", re.compile(r"(?:英镑|\bGBP\b|\bgbp\b)")),
    (
        "CNY",
        re.compile(r"(?:人民币|\bCNY\b|\bRMB\b|\bcny\b|\brmb\b|(?<!美|欧|日|港)元|块)"),
    ),
)

_CURRENCY_ALIASES = {
    "CNY": "CNY",
    "RMB": "CNY",
    "人民币": "CNY",
    "USD": "USD",
    "US$": "USD",
    "美元": "USD",
    "美金": "USD",
    "EUR": "EUR",
    "欧元": "EUR",
    "JPY": "JPY",
    "日元": "JPY",
    "HKD": "HKD",
    "港币": "HKD",
    "GBP": "GBP",
    "英镑": "GBP",
}

_WITHDRAWAL_RE = re.compile(r"(?:提现|取现|取钱|提款|取出)")
_REFUND_RE = re.compile(r"(?:退款|退货|冲正|退回|返还)")
_TRANSFER_RE = re.compile(r"(?:转账|转给|转钱|转了|转过去|汇款)")
_TOPUP_RE = re.compile(r"(?:充值|充钱|充到|零钱充值|话费充值|油卡充值|游戏充值)")
_SELF_TRANSFER_RE = re.compile(
    r"(?:自己的(?:另一个)?账户|自己账户|转到自己|转到我自己的|"
    r"自己的(?:另一张)?卡|同名账户|内部转账|自己名下|自己的零钱)"
)
_PURCHASE_TRANSFER_RE = re.compile(
    r"(?:买|购买|购入|消费|商品|店家|商家|商户|收银|超市|便利店|"
    r"餐厅|饭店|药店|付款买|买单)"
)
_SUMMARY_LIST_RE = re.compile(r"[（(][^）)]*[+＋][^）)]*[）)]|[+＋]")

_PAYMENT_KINDS = frozenset({"consumption", "transfer"})

_MEASURE_WORD = (
    r"(?:个|包|瓶|杯|份|碗|盒|袋|支|张|只|条|根|件|套|位|片|颗|粒|罐|听|块|双)"
)

_ITEM_SPLIT_RE = re.compile(r"[+＋、,，/&和与及]+")
_ITEM_PREFIX_RE = re.compile(
    r"^(?:消费支出|消费|支出|购买|购入|买了|买|一|两|二|三|四|五|六|七|八|九|十|\d+)+"
)
_ITEM_SUFFIX_RE = re.compile(
    rf"\s*(?:一|两|二|三|四|五|六|七|八|九|十|\d+)?\s*{_MEASURE_WORD}\s*$"
)

# ---------------------------------------------------------------------------
# [2026-11-07] 视频/直播音轨里的钱不计入消费（模块级开关，默认 "x"）
#
# 事故：2026-11-06 18:35:22「他一百块钱，」被记成 100 元消费（evt_6daa11c44387）。
# 前后 120 秒每一行都是 ``audio_content_class=stream``（新闻/评论视频），
# 说话人标签却是 self（同段还反复输出 100+ 条「他这个，」——ASR 卡在视频音轨上）。
#
# 用户经常一边放视频一边说话，所以**不能**"只要 stream 就不算"。两档：
#   "off" → 不做判断（旧行为，逐位一致）
#   "x"   → 证据句是 stream/music，**且**原话用第三人称（他/她/他们）**或**没有第一人称（我/咱），
#           **且**没有支付动作词（付/扫/转/买了/花了/给了/结账/支付/下单…）→ 进待确认
#   "y"   → 只要证据句是 stream/music 就进待确认（激进档，用于对比）
# 被判定的记录**不会消失**，而是带 ``pending_reason`` 进 ``pending_transactions``。
# ---------------------------------------------------------------------------
MEDIA_AUDIO_EXPENSE_POLICY = "x"
MEDIA_AUDIO_CLASSES = frozenset({"stream", "music"})
PENDING_MEDIA_THIRD_PERSON = "media_audio_third_person"
PENDING_MEDIA_STREAM = "media_audio_stream"
_THIRD_PERSON_RE = re.compile(r"他|她|人家")
_FIRST_PERSON_RE = re.compile(r"我|咱")
_PAYMENT_ACTION_RE = re.compile(
    r"付了|付款|付钱|支付|付掉|扫了|扫码|转账|转给|转钱|汇款|买了|买下|买过|花了|花掉|"
    r"给了|给钱|给了钱|结账|买单|下单|充值|缴费|交费"
)


def media_audio_pending_reason(event: Mapping[str, Any]) -> str | None:
    """证据句是不是"视频/直播音轨里的第三人称金额"。返回 pending_reason 或 None。

    只读 ``evidence``：``audio_content_class`` + ``quote``。拿不到类别 = 不判定（未知不压制）。
    """
    policy = str(MEDIA_AUDIO_EXPENSE_POLICY or "x").strip().lower()
    if policy not in {"x", "y"}:
        return None
    evidence = event.get("evidence") if isinstance(event.get("evidence"), Mapping) else {}
    content_class = str(evidence.get("audio_content_class") or "").strip().lower()
    if content_class not in MEDIA_AUDIO_CLASSES:
        return None
    if policy == "y":
        return PENDING_MEDIA_STREAM
    quote = str(evidence.get("quote") or event.get("title") or "").strip()
    if not quote:
        return None                      # 没有原话 = 没有判据，不压制
    if _PAYMENT_ACTION_RE.search(quote):
        return None                      # 明确的支付动作 → 按本人消费处理
    if _THIRD_PERSON_RE.search(quote):
        return PENDING_MEDIA_THIRD_PERSON
    if not _FIRST_PERSON_RE.search(quote):
        return PENDING_MEDIA_THIRD_PERSON
    return None


def _decimal(value: Any) -> Decimal | None:
    """Return a finite Decimal or None.  Never accepts bool as money."""
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value).strip())
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not parsed.is_finite():
        return None
    return parsed


def _money(value: Decimal) -> Decimal:
    return value.quantize(MONEY_QUANT, rounding=ROUND_HALF_UP)


def _money_float(value: Decimal | None) -> float | None:
    if value is None:
        return None
    return float(_money(value))


def _parse_dt(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=JOURNAL_TIMEZONE)
    return parsed.astimezone(JOURNAL_TIMEZONE)


def _normalise_quote(value: str) -> str:
    return "".join(ch.casefold() for ch in value if ch.isalnum())


def _collect_states(
    event: Mapping[str, Any], evidence: Mapping[str, Any], data: Mapping[str, Any]
) -> set[str]:
    states: set[str] = set()
    for key in ("event_state", "claim_type", "state", "temporal_relation"):
        for source in (event, evidence):
            raw = source.get(key)
            if isinstance(raw, str) and raw.strip():
                states.add(raw.strip().lower())
    for source_key in ("openclaw", "decision"):
        source = data.get(source_key)
        if isinstance(source, Mapping):
            for key in ("state", "candidate_type", "action"):
                raw = source.get(key)
                if isinstance(raw, str) and raw.strip():
                    states.add(raw.strip().lower())
    activity_type = str(event.get("activity_type") or "").strip().lower()
    if activity_type:
        states.add(activity_type)
    return states


def _confirmed_self_order_context(
    event: Mapping[str, Any],
    evidence: Mapping[str, Any],
    data: Mapping[str, Any],
) -> bool:
    """Return True only for a self order with traceable amount/price evidence.

    This is deliberately narrower than "text contains 能不能".  It requires:
    * ``source_label == self`` (not non_self/unknown);
    * an explicit order action such as 要一份/点了一份/加个;
    * ``data.expense.amount`` present and appearing in the original quote;
    * a price/settlement phrase (多少钱/一共/块/元/说...);
    * no strong future/plan marker (打算/计划/想买/以后/如果...).

    ``能不能`` alone never qualifies: e.g. 「这个能不能三十八」 has no order
    action and stays excluded.  The quote is read, never rewritten.
    """
    quote = str(evidence.get("quote") or "")
    expense = data.get("expense") if isinstance(data.get("expense"), Mapping) else {}
    amount = _decimal(expense.get("amount"))
    if amount is None or amount <= 0 or not quote:
        return False
    if str(evidence.get("source_label") or "").strip().lower() != "self":
        return False
    if _STRONG_ORDER_PLAN_RE.search(quote):
        return False
    if not _ORDER_ACTION_RE.search(quote):
        return False
    if not _ORDER_PRICE_EVIDENCE_RE.search(quote):
        return False
    if not _quote_contains_number(quote, amount):
        return False
    return True


_PRICE_DIALOGUE_RE = re.compile(
    r"(?:多少钱|多少|一共|总共|算你|只要|说|是|老板)\s*[:：?？，,\s]*"
    r"(?P<price>[零一二两三四五六七八九十百\d]+)\s*(?:块|元|块钱)?"
)


def _order_text_matches_amount(text: str, amount: Decimal) -> bool:
    """Check if text contains the amount, properly handling spoken currency units like 三块/3块."""
    if _quote_contains_number(text, amount):
        return True
    target = _money(amount)
    for m in re.finditer(r"(?P<num>[零〇一二两三四五六七八九十百千\d]+(?:\.\d+)?)\s*(?:块|元|块钱)?", text):
        num_str = m.group("num")
        try:
            val = Decimal(num_str) if num_str.replace(".", "", 1).isdigit() else Decimal(_number(num_str) or 0)
            if _money(val) == target:
                return True
        except Exception:
            continue
    return False


def _evaluate_order_price_evidence(
    event: Mapping[str, Any],
    evidence: Mapping[str, Any],
    data: Mapping[str, Any],
    text: str,
) -> tuple[bool, str | None, dict[str, Any] | None]:
    """评估事件是否具有明确的分角色订单与店员报价证据。

    规则（2026-10-25 订单报价证据边界修复）：
    - 纯 POS 事件（pos_payment_broadcast / title_source=pos_broadcast）绝不豁免；
    - 必须为本人点单（source_label == self）；
    - 必须不含强计划词（打算/计划/想买/以后/如果...）；
    - 路径 A（显式 related_quotes）：需同时包含带坐标的 self 点单行与带坐标的店员/对方
      独立报价行，且报价金额与 expense.amount 严格一致可追溯；若缺少坐标则返回
      order_evidence_missing_coordinates 保持 pending；
    - 路径 B（结构化问答原 quote）：原 quote 中包含明确点单动作 + 针对当前商品的
      店员独立问答报价对（如 多少钱 三块 / 一共三十八），且报价与金额一致；
    - 若只有点单动作和背景 POS 播报而无店员报价原句，不得盲目绑定放行，返回
      pos_broadcast_unbound_order 保持 pending；
    - 商品名与原话不符时返回 order_item_mismatch。
    """
    activity = str(event.get("activity_type") or "").strip().lower()
    title_src = str(evidence.get("title_source") or "").strip()
    if activity == POS_BROADCAST_ACTIVITY or title_src == "pos_broadcast":
        return False, None, None

    expense = data.get("expense") if isinstance(data.get("expense"), Mapping) else {}
    amount = _decimal(expense.get("amount"))
    if amount is None or amount <= 0:
        return False, "order_amount_invalid", None

    speaker = str(evidence.get("source_label") or "").strip().lower()
    if speaker != "self":
        return False, "pos_broadcast_unattributed", {"source_label": speaker}

    quote = str(evidence.get("quote") or "")
    if _STRONG_ORDER_PLAN_RE.search(quote):
        return False, "order_contains_plan", None

    item = str(expense.get("item") or event.get("title") or "").strip()

    # 路径 A：显式 related_quotes / speaker_quotes 结构
    related = evidence.get("related_quotes") or evidence.get("speaker_quotes")
    if isinstance(related, Sequence) and len(related) >= 2:
        has_coords = True
        self_quote_hit = None
        clerk_quote_hit = None
        for rq in related:
            if not isinstance(rq, Mapping):
                continue
            rq_text = str(rq.get("text") or rq.get("quote") or "").strip()
            rq_spk = str(rq.get("source_label") or rq.get("speaker") or "").strip().lower()
            coords = [
                rq.get(k)
                for k in ("segment_id", "source_id", "timestamp", "absolute", "start_offset_seconds")
            ]
            if not any(c is not None and str(c).strip() for c in coords):
                has_coords = False

            if rq_spk == "self" and _ORDER_ACTION_RE.search(rq_text):
                if item and any(c in rq_text for c in item if c not in "、，, "):
                    self_quote_hit = rq
            elif rq_spk in ("non_self", "unknown", "clerk", "seller", "other"):
                if _order_text_matches_amount(rq_text, amount):
                    clerk_quote_hit = rq

        if not has_coords:
            return False, "order_evidence_missing_coordinates", {"missing": "coordinates"}
        if self_quote_hit and clerk_quote_hit:
            return True, None, {"self_quote": self_quote_hit, "clerk_quote": clerk_quote_hit}
        if self_quote_hit and not clerk_quote_hit:
            return False, "pos_broadcast_unbound_order", {"missing": "clerk_price_evidence"}

    # 路径 B：原 quote 内部的问答点单报价对
    if not _ORDER_ACTION_RE.search(quote):
        return False, "order_action_missing", None

    if _ORDER_INTERROGATIVE_RE.search(quote):
        return False, "order_action_is_question", None

    if item and not any(part in quote for part in re.split(r"[、，,\s]+", item) if part):
        return False, "order_item_mismatch", {"expected_item": item}

    dialogue_matches = list(_PRICE_DIALOGUE_RE.finditer(quote))
    matched_dialogue = None
    for dm in dialogue_matches:
        p_text = dm.group("price")
        if _order_text_matches_amount(p_text, amount):
            matched_dialogue = dm
            break

    if matched_dialogue:
        return True, None, {"dialogue_quote": matched_dialogue.group(0)}

    # 有点单动作与商品，但未找到店员独立报价问答（仅有背景播报）：
    # 不得盲目按金额相等放行，返回明确 pending 理由
    return False, "pos_broadcast_unbound_order", {"missing": "clerk_independent_price_quote"}


def _non_fact_reason(
    states: set[str], text: str, *, confirmed_order: bool = False
) -> str | None:
    for state in states:
        if state in _NON_FACT_STATES:
            return f"explicit_non_fact_state:{state}"
    if _NEGATION_RE.search(text):
        return "negated_purchase"
    has_completion = bool(_COMPLETION_RE.search(text))
    has_fact_state = any(state in _FACT_STATES for state in states)
    if _ORDER_INTERROGATIVE_RE.search(text) and not confirmed_order:
        return "question_not_confirmed"
    if _PLAN_RE.search(text) and not has_completion and not confirmed_order:
        return "planned_or_hypothetical"
    if (
        not has_fact_state
        and ("?" in text or "？" in text)
        and _QUESTION_RE.search(text)
        and not _CONFIRM_RE.search(text)
        and not confirmed_order
    ):
        return "question_not_confirmed"
    # Legacy events may omit event_state/claim_type entirely.  If the text does
    # not carry an explicit plan/negation/question, the presence of structured
    # expense data (handled by the caller) is the compatible evidence basis.
    return None


def _detect_currency(
    declared: str | None,
    quote: str,
) -> tuple[str, str, bool, str | None]:
    """Return (currency, source, conflict, note).

    Explicit quote evidence wins over the legacy field.  A bare ``$`` is not
    enough to assume USD.
    """
    marker_values: list[str] = []
    for currency, pattern in _CURRENCY_MARKERS:
        if pattern.search(quote):
            marker_values.append(currency)
    distinct = list(dict.fromkeys(marker_values))
    declared_norm = _CURRENCY_ALIASES.get(
        (declared or "").strip().upper()
    ) or _CURRENCY_ALIASES.get((declared or "").strip())

    if distinct:
        if len(distinct) == 1:
            currency = distinct[0]
            conflict = bool(declared_norm and declared_norm != currency)
            note = (
                f"currency_from_quote:{currency} overrides declared {declared_norm}"
                if conflict
                else None
            )
            return currency, "quote", conflict, note
        if declared_norm and declared_norm in distinct:
            return (
                declared_norm,
                "field_matching_ambiguous_quote",
                False,
                "quote_names_multiple_currencies",
            )
        return "UNKNOWN", "ambiguous_quote", False, "quote_names_multiple_currencies"

    if declared_norm:
        return declared_norm, "field", False, None
    if "$" in quote or "＄" in quote:
        return "UNKNOWN", "ambiguous_symbol", False, "bare_dollar_not_assumed_usd"
    return "CNY", "legacy_default", False, None


def _is_completed_purchase_text(text: str) -> bool:
    return bool(_COMPLETION_RE.search(text))


def _quote_contains_number(quote: str, proposed: Decimal) -> bool:
    """Check a proposed amount against number tokens in the original quote."""
    target = _money(proposed)
    for match in re.finditer(r"(?<![\d.])(\d+(?:\.\d+)?)(?![\d.])", quote):
        tail = quote[match.end() : match.end() + 4]
        if re.match(rf"\s*{_MEASURE_WORD}", tail):
            continue
        parsed = _decimal(match.group(1))
        if parsed is not None and _money(parsed) == target:
            return True
    for match in re.finditer(r"[零〇一二两三四五六七八九十百千]+", quote):
        tail = quote[match.end() : match.end() + 4]
        if re.match(rf"\s*{_MEASURE_WORD}", tail):
            continue
        parsed_int = _number(match.group(0))
        if parsed_int is not None and _money(Decimal(parsed_int)) == target:
            return True
    return False


def _evidence_quote_texts(evidence: Mapping[str, Any] | None) -> list[str]:
    """收集一个 evidence 里**所有**原话文本（保序、去重、非空）。

    [2026-11-03 Q809] 背景：活动层的 items[].evidence 是**一个列表**，同一笔消费的
    金额常常只在第二/第三条里（例：11-01「然后练完之后。」+「买了一包烟，十块钱，
    又买了一瓶水，六块九。」）。旧实现只看 `evidence["quote"]` 单条 → 第一条没有金额
    就判 amount_unknown，明明原话里有“十块钱”。
    """
    if not isinstance(evidence, Mapping):
        return []
    texts: list[str] = []
    seen: set[str] = set()

    def _push(value: Any) -> None:
        text = str(value or "").strip()
        if text and text not in seen:
            seen.add(text)
            texts.append(text)

    _push(evidence.get("quote"))
    for key in ("quotes", "related_quotes", "speaker_quotes", "evidence"):
        value = evidence.get(key)
        if isinstance(value, str):
            _push(value)
        elif isinstance(value, Sequence):
            for entry in value:
                if isinstance(entry, Mapping):
                    _push(entry.get("quote") or entry.get("text"))
                else:
                    _push(entry)
    return texts


#: 「X 块」后面这些字符仍然表示**金额**（块五=2.5 元、块八毛=3.8 元）。
_KUAI_AFTER_IS_MONEY = set("0123456789０１２３４５６７８９零一二三四五六七八九十两半钱毛角分"
                           # 总控 11-07：块后接虚词/动词（二十块的、十块了、五块买的、十块给他）也是钱
                           "整的了买给付花收退补差吧呢啊嘛就才多左右来去")
#: 单价说法：「十块一斤」「五块一个」——块后面直接跟这些量词短语仍是价格。
_KUAI_UNIT_PRICE_PREFIXES = ("一斤", "一两", "一个", "一份", "一瓶", "一杯", "一包", "一只", "一件", "一套", "一盒")
#: 数字 + 块（+ 紧跟的一个字符）
_KUAI_RE = re.compile(r"[0-9０-９零一二三四五六七八九十百千万两半]+\s*块(?P<after>.)?")


def _kuai_is_measure_word(text: str) -> bool:
    """整句里**所有**「数字+块」都是物品量词时才返回 True。

    [2026-11-07 返工] 原实现「只要有一处像量词就整句拦掉」会误杀真实消费：
      * 花了两块五（=2.5 元）、三块八毛（=3.8 元）→ 块后跟数字，是钱；
      * 十块一斤 / 五块一个 → 单价，是钱；
      * 三块蛋糕十块钱 → 前面是量词，但后一处是钱，整句必须仍能补出金额。
    规则：逐个「数字+块」判断，只有全部都是量词（块后紧跟普通名词）才拦。
    """
    raw = str(text or "")
    occurrences = list(_KUAI_RE.finditer(raw))
    if not occurrences:
        return False
    seen_measure = False
    for match in occurrences:
        after = (match.group("after") or "").strip()
        if not after:
            return False                      # 「一共十五块」结尾 → 钱
        if after[0] in _KUAI_AFTER_IS_MONEY:
            return False                      # 块五 / 块八毛 / 块钱 → 钱
        if any(after.startswith(prefix) for prefix in _KUAI_UNIT_PRICE_PREFIXES):
            return False                      # 十块一斤 / 五块一个 → 单价
        if "\u4e00" <= after[0] <= "\u9fff":
            seen_measure = True               # 块 + 普通名词 → 这一处是量词
            continue
        return False                          # 标点/字母 → 钱（如「二十块。」）
    return seen_measure


#: 媒体/外放来源标记（结构性字段，不含 source_label）
_MEDIA_ACTIVITY_TYPES = ("media", "video", "tv", "stream", "broadcast_media", "film", "documentary")
_MEDIA_CATEGORIES = ("浏览·视频", "媒体内容", "浏览", "媒体")


def _is_media_source(event: Mapping[str, Any], evidence: Mapping[str, Any],
                     data: Mapping[str, Any]) -> bool:
    """这条记录是不是外放/媒体旁白（用记录上已有的字段判断，不猜）。

    命中任一即算：
      * 活动/子项 lifecycle == media（v3 与旧字段都看）；
      * event_id 或 activity_type 里带 media；
      * categories 是浏览/媒体类；
      * （不再包含 source_label，见下方注释）。
    """
    for key in ("lifecycle", "item_lifecycle", "v3_lifecycle"):
        if str(event.get(key) or "").strip().lower() == "media":
            return True
    event_id = str(event.get("event_id") or "").lower()
    if "media" in event_id:
        return True
    activity_type = str(event.get("activity_type") or "").lower()
    if any(marker in activity_type for marker in _MEDIA_ACTIVITY_TYPES):
        return True
    categories = event.get("categories") or []
    if isinstance(categories, str):
        categories = [categories]
    for category in categories:
        text = str(category or "")
        if any(marker in text for marker in _MEDIA_CATEGORIES):
            return True
    # [2026-11-07 返工] 不再用 source_label=non_self 兜底：戴耳机小声说话时
    # 声纹会把本人误判成 non_self，单靠它会误杀本人消费。只认媒体/外放的结构性标记。
    return False


def _recover_amount_from_evidence(
    evidence: Mapping[str, Any] | None,
    quote: str,
    proposed: Decimal | None,
) -> Decimal | None:
    """逐条原话找金额：**每条都过同一道 `_recover_amount` 闸门**，不是把多句拼起来。

    为什么不是 join：拼接会让「第一条是问句/点单 + 第二条是别的价格」也凑出一个金额，
    等于绕过了 `_is_completed_purchase_text` 的保守闸门。逐条试 = 只有**某一条原话本身**
    具备“已完成购买 + 有金额”时才采用。
    """
    for text in _evidence_quote_texts(evidence) or ([quote] if quote else []):
        recovered = _recover_amount(text, proposed)
        if recovered is not None and recovered > 0:
            return recovered
    return None


def _recover_amount(quote: str, proposed: Decimal | None) -> Decimal | None:
    """Recover a missing amount from a validated original quote only.

    The title is deliberately not an input.  ``proposed_expense_amount`` is
    accepted only when it is grounded in the quote using the existing number
    parser.  Without a proposal, the existing quote amount parser is used.
    """
    if not quote or not _is_completed_purchase_text(quote):
        return None
    parsed_quote = _extract_amount(quote)
    parsed_decimal = _decimal(parsed_quote) if parsed_quote is not None else None
    if parsed_decimal is not None and parsed_decimal > 0:
        parsed_decimal = _money(parsed_decimal)
    else:
        parsed_decimal = None

    if proposed is not None:
        proposed = _money(proposed)
        if parsed_decimal is not None and parsed_decimal == proposed:
            return proposed
        if _quote_contains_number(quote, proposed):
            return proposed
        return None
    return parsed_decimal


def _detect_kind(
    activity_type: str,
    title: str,
    quote: str,
    item: str,
    expense: Mapping[str, Any] | None = None,
) -> str:
    """Classify a transaction without assuming every transfer is a purchase.

    ``transfer`` is reserved for transfers with actual merchant/purchase
    evidence and is counted as payment.  Account moves, person-to-person
    transfers without purchase evidence, and wallet/card top-ups are
    non-payment kinds.
    """
    expense = expense or {}
    merchant = str(expense.get("merchant") or "")
    text = f"{activity_type} {title} {quote} {item} {merchant}"
    if "withdraw" in activity_type or _WITHDRAWAL_RE.search(text):
        return "withdrawal"
    if "refund" in activity_type or _REFUND_RE.search(text):
        return "refund"
    if (
        "topup" in activity_type
        or "recharge" in activity_type
        or _TOPUP_RE.search(text)
    ):
        return "topup"
    is_transfer = "transfer" in activity_type or bool(_TRANSFER_RE.search(text))
    if is_transfer:
        if _SELF_TRANSFER_RE.search(text):
            return "account_transfer"
        if _PURCHASE_TRANSFER_RE.search(text):
            return "transfer"
        return "person_transfer"
    return "consumption"


def _detect_category(kind: str, item: str, title: str, quote: str) -> str:
    if kind not in _PAYMENT_KINDS:
        return kind
    text = f"{item} {title} {quote}".lower()
    if any(word in text for word in ("烟", "打火机")):
        return "tobacco"
    if any(word in text for word in ("茶", "饮", "水", "饮料", "咖啡", "奶")):
        return "beverage"
    if any(
        word in text
        for word in ("餐", "面", "肠", "饭", "食", "肉", "果", "菜", "桂圆")
    ):
        return "food"
    return "other"


def _payment_context(expense: Mapping[str, Any], quote: str) -> str:
    context = expense.get("payment_context")
    if context:
        return str(context)
    if "会员" in quote or "碰一下" in quote or "支付盒" in quote:
        return "手机会员碰一下支付盒"
    if "零钱" in quote or "现金" in quote:
        return "现金/零钱支付"
    return "电子支付"


def _provenance(
    event: Mapping[str, Any], evidence: Mapping[str, Any]
) -> dict[str, Any]:
    return {
        "event_id": event.get("event_id"),
        "source_id": evidence.get("source_id"),
        "recording_id": evidence.get("recording_id"),
        "segment_id": evidence.get("segment_id"),
        "reported_at": evidence.get("reported_at")
        or (event.get("time") or {}).get("absolute"),
        "absolute_started_at": evidence.get("absolute_started_at"),
        "start_offset_seconds": evidence.get("start_offset_seconds"),
        "end_offset_seconds": evidence.get("end_offset_seconds"),
        "extractor": evidence.get("extractor"),
        "decision_source": evidence.get("decision_source"),
        "source_label": evidence.get("source_label"),
        # [2026-10-23 P-50] 音频内容 sha：能取到时用于跨命名空间去重
        # （10-20 同一段音频被 live/batch 各抽一遍，recording_id 前缀不同但 sha 相同）。
        "content_sha256": evidence.get("content_sha256") or _content_sha_hint(event),
    }


def _l2_expense_review_signal(
    evidence: Mapping[str, Any],
) -> tuple[bool, str | None, str | None, Mapping[str, Any] | None]:
    """Return an explicit L2 rejection signal, if the event carries one.

    Explicit source/event pending markers take priority over an accepted money
    contract: the contract only approves the model's amount evidence, not the
    speaker attribution or overall event state.  Only explicit extraction
    markings count here.  A generic ``review_required`` flag is intentionally
    not enough: it is also used by source attribution and by legacy passive
    events that still have a valid amount.
    """
    contract = evidence.get("expense_model_contract")
    contract = contract if isinstance(contract, Mapping) else None
    contract_status = (
        str(contract.get("status") or "").strip().lower() if contract else ""
    )
    contract_reason = (
        str(contract.get("reason") or "").strip() or None if contract else None
    )

    # Source/event review state outranks accepted money-integrity metadata.
    pending_reason = evidence.get("pending_reason")
    if isinstance(pending_reason, str) and pending_reason.strip():
        return True, pending_reason.strip(), "pending_reason", contract

    deepseek_review = evidence.get("deepseek_review")
    if isinstance(deepseek_review, str) and deepseek_review.strip():
        return True, deepseek_review.strip(), "deepseek_review", contract

    if "unverified_expense_data" in evidence:
        return (
            True,
            "unverified_expense_data",
            "unverified_expense_data",
            contract,
        )

    if contract is not None:
        if contract_status == "accepted":
            return False, None, "expense_model_contract", contract
        if contract_status or contract_reason:
            return (
                True,
                contract_reason
                or f"expense_model_contract_{contract_status or 'pending'}",
                "expense_model_contract",
                contract,
            )

    return False, None, None, contract


def _positive_amount(value: Any) -> Decimal | None:
    parsed = _decimal(value)
    if parsed is None or parsed <= 0:
        return None
    return parsed


def _l2_candidate_amount(
    evidence: Mapping[str, Any],
    expense: Mapping[str, Any],
    contract: Mapping[str, Any] | None,
) -> Decimal | None:
    """Keep the rejected model proposal readable without counting it."""
    candidates: list[Any] = [expense.get("amount"), evidence.get("proposed_expense_amount")]
    unverified = evidence.get("unverified_expense_data")
    if isinstance(unverified, Mapping):
        candidates.append(unverified.get("amount"))
    if isinstance(contract, Mapping):
        candidates.append(contract.get("amount"))
    for value in candidates:
        parsed = _positive_amount(value)
        if parsed is not None:
            return parsed
    return None


@dataclass
class _Record:
    event_id: str | None
    title: str
    quote: str
    item: str
    amount: Decimal | None
    currency: str
    amount_source: str
    currency_source: str
    category: str
    kind: str
    payment_context: str
    explanation: list[str]
    provenance: dict[str, Any]
    reported_at: str | None
    reported_dt: datetime | None
    clock: str | None
    pending_reason: str | None
    proposed_amount: Decimal | None
    review_status: str | None
    aggregate: bool = False
    broadcast: bool = False
    duplicate_ids: list[str] = field(default_factory=list)
    currency_conflict: bool = False

    @property
    def minute_key(self) -> str | None:
        if self.reported_dt is not None:
            return self.reported_dt.strftime("%Y-%m-%dT%H:%M")
        return None


_SHA_IN_ARTIFACT = re.compile(r"/(job-\d{8}-\d{6}-[0-9a-f]+)/files/([0-9a-f]{32,64})/")


def _content_sha_hint(event: Mapping[str, Any]) -> str | None:
    """从产物路径里取音频内容 sha（`jobs/<job>/files/<sha>/...`）。

    Watch 实时链路的事件没有这个字段（sha 在 manifest 里，不在事件里），
    返回 None；调用方不得把 None 当成"不同音频"。
    """
    artifact = str(event.get("source_artifact") or "")
    match = _SHA_IN_ARTIFACT.search(artifact)
    return match.group(2) if match else None


def _is_aggregate(record: _Record) -> bool:
    text = f"{record.title} {record.item}"
    if not re.search(r"(?:合计|一共|总计|总共|汇总|共计|总价|小计)", text):
        return False
    # A concrete single purchase may legitimately say "一共买了 X".  Treat it
    # as a detail unless the text also shows a summary list.
    if re.search(r"(?:买了|购买|购入)", text) and not _SUMMARY_LIST_RE.search(text):
        return False
    return True


def _item_tokens(record: _Record) -> list[str]:
    values: list[str] = []
    if record.item:
        values.append(record.item)
    if record.title:
        values.append(record.title)
    tokens: list[str] = []
    for value in values:
        for part in _ITEM_SPLIT_RE.split(value):
            candidate = part.strip(" ：:，,。.！？!?()（）[]【】")
            candidate = _ITEM_PREFIX_RE.sub("", candidate)
            previous = None
            while candidate and candidate != previous:
                previous = candidate
                candidate = _ITEM_SUFFIX_RE.sub("", candidate).strip()
            candidate = re.sub(r"(?:合计|共计|总价|小计).*$", "", candidate).strip()
            if len(candidate) >= 2 and candidate not in tokens:
                tokens.append(candidate)
    return tokens


def _item_mentioned(record: _Record, aggregate_text: str) -> bool:
    tokens = _item_tokens(record)
    if not tokens:
        return True
    return any(token in aggregate_text for token in tokens)


def _aggregate_has_item_names(record: _Record) -> bool:
    generic = {"合计", "本次采买", "采买", "总计", "一共", "汇总"}
    return any(
        token not in generic and len(token) >= 2 for token in _item_tokens(record)
    )


def _can_reconcile_nearby(aggregate: _Record, detail: _Record) -> bool:
    if aggregate.reported_dt is None or detail.reported_dt is None:
        return False
    delta = abs((aggregate.reported_dt - detail.reported_dt).total_seconds())
    if delta > RECONCILE_WINDOW_SECONDS:
        return False
    agg_recording = str(aggregate.provenance.get("recording_id") or "")
    det_recording = str(detail.provenance.get("recording_id") or "")
    if agg_recording and det_recording and agg_recording == det_recording:
        return True
    return delta <= CROSS_SOURCE_RECONCILE_WINDOW_SECONDS


def _find_reconciliation(
    aggregate: _Record, details: Sequence[_Record]
) -> list[_Record] | None:
    """Find an unambiguous nearby detail subset that sums to the aggregate."""
    candidates = [d for d in details if _can_reconcile_nearby(aggregate, d)]
    if len(candidates) < 2:
        return None
    candidates = sorted(
        candidates,
        key=lambda r: r.reported_dt or datetime.min.replace(tzinfo=JOURNAL_TIMEZONE),
    )
    candidates = candidates[-MAX_RECONCILE_CANDIDATES:]
    aggregate_text = f"{aggregate.title} {aggregate.item}"
    name_present = _aggregate_has_item_names(aggregate)

    matches: list[tuple[bool, int, list[_Record]]] = []
    max_size = min(MAX_RECONCILE_SUBSET, len(candidates))
    for size in range(2, max_size + 1):
        for combo in itertools.combinations(candidates, size):
            if sum((r.amount or Decimal(0)) for r in combo) != aggregate.amount:
                continue
            mentioned = all(_item_mentioned(r, aggregate_text) for r in combo)
            if name_present and not mentioned:
                continue
            matches.append((mentioned, size, list(combo)))

    if not matches:
        return None
    # Prefer the most completely named detail set, then the largest set.
    best_score = max((mentioned, size) for mentioned, size, _ in matches)
    best = [
        combo for mentioned, size, combo in matches if (mentioned, size) == best_score
    ]
    if len(best) != 1:
        return None
    if not name_present:
        # Amount-only aggregates are risky; require same recording evidence.
        agg_recording = str(aggregate.provenance.get("recording_id") or "")
        selected = best[0]
        if not agg_recording or any(
            str(r.provenance.get("recording_id") or "") != agg_recording
            for r in selected
        ):
            return None
    return best[0]


def _quotes_compatible(first: str, second: str) -> bool:
    left = _normalise_quote(first)
    right = _normalise_quote(second)
    if not left or not right:
        return False
    if left == right:
        return True
    shorter, longer = (left, right) if len(left) <= len(right) else (right, left)
    return len(shorter) >= 4 and shorter in longer


def _canonical_item(value: str) -> str:
    """Canonical item identity for duplicate defence (not for ledger identity)."""
    text = _ITEM_SPLIT_RE.split(value)[0] if value else ""
    text = text.strip(" ：:，,。.！？!?()（）[]【】")
    text = _ITEM_PREFIX_RE.sub("", text)
    previous = None
    while text and text != previous:
        previous = text
        text = _ITEM_SUFFIX_RE.sub("", text).strip()
    text = re.sub(r"(?:合计|共计|总价|小计).*$", "", text).strip()
    return "".join(ch.casefold() for ch in text if ch.isalnum())


def _item_is_quote_like(record: _Record) -> bool:
    """True when an item field is just a raw quote/price fragment.

    Live extraction sometimes emits ``两瓶杯两块钱`` while the formal variant
    emits the concrete item ``冰杯`` for the same quote.  Such a quote-like
    field must not block duplicate defence, but two concrete items from one
    utterance (``冰水`` / ``茶``) stay distinct.
    """
    raw = _normalise_quote(record.item)
    quote_norm = _normalise_quote(record.quote)
    if not raw or not quote_norm or raw not in quote_norm:
        return False
    if re.search(r"(?:\d|元|块|钱)", record.item):
        return True
    return len(raw) >= 6


def _items_compatible(first: _Record, second: _Record) -> bool:
    left = _canonical_item(first.item)
    right = _canonical_item(second.item)
    if not left or not right:
        return False
    if left == right or left in right or right in left:
        return True
    # Same quote + same amount + same evidence anchor, with one side merely
    # echoing the quote as its item: accept the naming variant.
    return _item_is_quote_like(first) != _item_is_quote_like(second)


def _same_evidence_anchor(first: _Record, second: _Record) -> bool:
    """Precise evidence anchor; a shared minute alone is not enough."""
    left_reported = first.provenance.get("reported_at")
    right_reported = second.provenance.get("reported_at")
    if left_reported and right_reported and str(left_reported) == str(right_reported):
        return True
    left_recording = str(first.provenance.get("recording_id") or "")
    right_recording = str(second.provenance.get("recording_id") or "")
    left_segment = str(first.provenance.get("segment_id") or "")
    right_segment = str(second.provenance.get("segment_id") or "")
    if (
        left_recording
        and left_recording == right_recording
        and left_segment
        and left_segment == right_segment
    ):
        return True
    left_start = first.provenance.get("start_offset_seconds")
    right_start = second.provenance.get("start_offset_seconds")
    left_end = first.provenance.get("end_offset_seconds")
    right_end = second.provenance.get("end_offset_seconds")
    if (
        left_start is not None
        and right_start is not None
        and left_end is not None
        and right_end is not None
        and left_start == right_start
        and left_end == right_end
    ):
        return True
    return False


_EXPENSE_ITEM_REPORT_PREFIX_RE = re.compile(
    r"^(?:录音中(?:疑似|提及)[：:]\s*|未确认[：:]\s*)"
    r"|^(?:称(?:练完之后|自己)?(?:又)?|陈述(?:自己)?|提及(?:自己)?|表示(?:自己)?|说(?:自己)?)\s*"
)
_EXPENSE_ITEM_AMOUNT_TAIL_RE = re.compile(
    r"(?:[，,、\s]*(?:并)?(?:花费|花了|付了|支付|共计|一共|花了约|大约花了)\s*"
    r"[零〇一二两三四五六七八九十百千万点\d.]+\s*(?:元|块|块钱|毛|角|人民币|刀|美元|USD|CNY)?"
    r"(?:[零〇一二两三四五六七八九十\d]+(?:毛|角|分)?)?"
    r"|[，,、\s]+[零〇一二两三四五六七八九十百千万点\d.]+\s*(?:元|块|块钱)"
    r"(?:[零〇一二两三四五六七八九十\d]+(?:毛|角|分)?)?)\s*$"
)
_EXPENSE_ITEM_VERB_QTY_PREFIX_RE = re.compile(
    r"^(?:去)?(?:购买了?|买了|买|拿了|点了一份|点了个|点了)"
    r"(?:[一二两三四五六七八九十\d]+(?:包|瓶|盒|袋|份|碗|杯|个|件|听|罐|提|箱|根|支|串)\s*)?"
)
_EXPENSE_ITEM_BARE_OBJECT_RE = re.compile(
    r"^(?:菜|饭|单|水|烟|酒|药|东西|早饭|午饭|晚饭|早餐|午餐|晚餐|宵夜|夜宵)$"
)


def clean_expense_item_name(item: str, title: str = "") -> str:
    """把消费项名字清洗成『买了什么』，去掉『花费X元 / 花了X / 称… / 陈述…』等口语赘述。"""
    raw = str(item or title or "").strip()
    if not raw:
        return ""
    s = raw
    prev = None
    while s and s != prev:
        prev = s
        s = _EXPENSE_ITEM_REPORT_PREFIX_RE.sub("", s).strip()
    s = re.sub(r"^去买菜[，,、\s]*买菜", "买菜", s)
    prev = None
    while s and s != prev:
        prev = s
        s = _EXPENSE_ITEM_AMOUNT_TAIL_RE.sub("", s).strip(" ，,、。；;：:")
    m_paren = re.match(r"^(?:买烟|买零食|买水|买饮料|买菜)[（(]([^（）()]+)[）)]$", s)
    if m_paren:
        inner = m_paren.group(1).strip()
        if inner and not inner.startswith("钟点"):
            return inner
    stripped = _EXPENSE_ITEM_VERB_QTY_PREFIX_RE.sub("", s).strip()
    if stripped and not _EXPENSE_ITEM_BARE_OBJECT_RE.match(stripped):
        s = stripped
    else:
        s = re.sub(
            r"^(?:去)?(?:购买了?|买了|买)(?:[一二两三四五六七八九十\d]+(?:包|瓶|盒|袋|份|碗|杯|个|件))?(?=菜|饭|单|水|烟|酒|药|东西)",
            "买",
            s,
        )
    return s or raw


def _record_to_transaction(record: _Record) -> dict[str, Any]:
    return {
        "event_id": record.event_id,
        "entry_key": record.event_id,
        "clock": record.clock,
        "title": record.title,
        "item": clean_expense_item_name(record.item, record.title),
        "amount": _money_float(record.amount),
        "amount_cents": int(_money(record.amount) * 100)
        if record.amount is not None
        else None,
        "currency": record.currency,
        "category": record.category,
        "kind": record.kind,
        "payment_context": record.payment_context,
        "quote": record.quote,
        "reported_at": record.reported_at,
        "amount_source": record.amount_source,
        "currency_source": record.currency_source,
        "currency_conflict": record.currency_conflict,
        "aggregate": record.aggregate,
        "explanation": list(record.explanation),
        "decision": "; ".join(record.explanation),
        "provenance": dict(record.provenance),
        "duplicate_event_ids": list(record.duplicate_ids),
        "review_status": record.review_status,
    }


def _record_to_pending(record: _Record) -> dict[str, Any]:
    return {
        "event_id": record.event_id,
        "clock": record.clock,
        "title": record.title,
        "item": clean_expense_item_name(record.item, record.title),
        "amount": _money_float(record.amount),
        "currency": record.currency,
        "category": record.category,
        "kind": record.kind,
        "quote": record.quote,
        "reported_at": record.reported_at,
        "reason": record.pending_reason,
        "proposed_amount": _money_float(record.proposed_amount),
        "explanation": list(record.explanation),
        "provenance": dict(record.provenance),
        "review_status": record.review_status,
    }


def _sort_key(record: _Record) -> str:
    return record.reported_at or record.minute_key or ""


POS_BROADCAST_ACTIVITY = "pos_payment_broadcast"

#: 钱**进商家账户**的播报词（商家收款/声匣子）。出现这些 = 收款方向，不是本人支出。
POS_INFLOW_MARKERS: tuple[str, ...] = (
    "到账", "收款", "收到", "记账成功", "收钱", "入账",
)
#: 明确的**付款方**信号（本人扣款）。优先级低于收款词：商家声匣子也会说"支付"。
POS_OUTFLOW_MARKERS: tuple[str, ...] = (
    "支付成功", "付款成功", "已扣款", "付款码", "扣款成功", "消费", "支出",
)


def _pos_broadcast_direction(text: str) -> str:
    """判断 POS 播报的方向：`inflow`（商家收款）/ `outflow`（本人付款）/ `ambiguous`。

    背景（2026-10-23 P-50，用户裁决）：商家声匣子念的是
    「支付宝到账十五元 / 收款单到账三元 / 支付到账2元」——**钱进商家账户**，
    不是用户花钱。原来一律当成支出，10-20 因此多算 ¥25。
    反过来「支付成功 / 已扣款」才是付款方播报，可以算本人支出。
    两头都不像（只有金额）→ 保守判 `ambiguous`，进待确认，不计数。
    """
    raw = str(text or "")
    if not raw.strip():
        return "ambiguous"
    has_inflow = any(marker in raw for marker in POS_INFLOW_MARKERS)
    has_outflow = any(marker in raw for marker in POS_OUTFLOW_MARKERS)
    if has_inflow:
        # 「支付到账」「微信支付收款到账」这类混合句：方向词仍是"到账" → 收款方
        return "inflow"
    if has_outflow:
        return "outflow"
    return "ambiguous"


#: 播报句式：命中即视为「播报类」（B 档：待确认，不进总额）。
POS_BROADCAST_PHRASES = re.compile(r"(?:到账|收款|播报|收钱|已收|入账|记账成功)")


def _is_broadcast_event(
    event: Mapping[str, Any], evidence: Mapping[str, Any], text: str
) -> bool:
    """是不是「收款播报类」事件（2026-10-23 P-50 三档制 B 档）。

    依据（用户 10-22 原话）：支付宝播报**多数时候是本人付款后店家音箱响的**，
    但不是 100% 可靠（有的是别人的、有的店家不响）。所以播报一律降级为
    「待确认」，不进 `total_amount`；唯一 100% 可信的是本人开口说的。
    判定不能只看 source_label：10-18 有一条 `self` 的
    「呃，也可以，像那个播报收款收款二十二元」——他在**描述播报这个现象**，
    不是自己花钱，也必须进 B 档。
    """
    if str(event.get("activity_type") or "").strip().lower() == POS_BROADCAST_ACTIVITY:
        return True
    if str(evidence.get("title_source") or "").strip() == "pos_broadcast":
        return True
    return bool(POS_BROADCAST_PHRASES.search(text or ""))


def _pos_broadcast_pending_reason(
    event: Mapping[str, Any], evidence: Mapping[str, Any], text: str
) -> str | None:
    """POS 播报是否该进「待确认」而不是账本；返回待确认原因（None = 正常记账）。"""
    activity = str(event.get("activity_type") or "").strip().lower()
    # 以 activity_type 为准；只有它缺失时才看 title_source（老产物可能没有 activity_type）。
    if activity == POS_BROADCAST_ACTIVITY:
        is_pos = True
    elif activity:
        is_pos = False
    else:
        is_pos = str(evidence.get("title_source") or "").strip() == "pos_broadcast"
    if not is_pos:
        return None

    direction = _pos_broadcast_direction(text)
    speaker = str(evidence.get("source_label") or "").strip().lower()
    if speaker == "non_self":
        # 别人在付款（或别人说话时的播报）：不能算到本人头上
        return "pos_broadcast_unattributed"
    if direction == "ambiguous":
        # 只有金额、没有方向词：保守起见不计数，让用户自己确认
        return "pos_broadcast_ambiguous"
    # 剩下的是 self / unknown：
    #   · 「支付到账六点九元」这类是**商家声匣子在播报本人刚才的付款**，方向词是"到账"
    #     但钱是本人付的 —— 2026-10-16 实测就是这种情况（¥6.9 冰杯，用户自己在场买单）；
    #   · 「支付成功 / 已扣款」是付款方播报。
    # 两种都对本人有效，因此**不按方向词一刀切**（那会把本人真实消费也删掉）。
    # 方向只作为可审计标注记进 explanation。
    return None


def _pos_broadcast_unattributed(
    event: Mapping[str, Any], evidence: Mapping[str, Any]
) -> bool:
    """POS 声匣子播报是不是「别人的付款」（2026-10-23 P-46）。

    背景（10-20 实测）：用户在小店吃饭，麦克风把**别的顾客**的
    「支付宝到账十五元 / 十元」也录进来，抽取层生成 `pos_payment_broadcast`
    事件时**完全不看 source_label**，于是陌生人的付款直接进了本人账本
    （22 天共 6 笔 / ¥42.8）。

    抽取层这边不改事实（事件照落，零数据丢失），只在**账本层**拦钱：
      · 只处理 `pos_payment_broadcast`（或 evidence.title_source == pos_broadcast）；
      · 说话人是 `non_self` → 不是本人付的钱 → 进 pending，不计数；
      · `self` / `unknown` 维持原行为（unknown 由既有 l2/金额规则兜底）。
    与 `contextual_extraction.py:2359`（对话路径遇 non_self 置 amount=None）保持同一口径。
    """
    activity = str(event.get("activity_type") or "").strip().lower()
    # 以 activity_type 为准；只有它缺失时才看 title_source（老产物可能没有 activity_type）。
    if activity == POS_BROADCAST_ACTIVITY:
        is_pos = True
    elif activity:
        is_pos = False
    else:
        is_pos = str(evidence.get("title_source") or "").strip() == "pos_broadcast"
    if not is_pos:
        return False
    speaker = str(evidence.get("source_label") or "").strip().lower()
    return speaker == "non_self"


#: 「物品+金额」片段（如「肉夹馍8元、煎豆腐7元、捞粉10元」）
_ITEMIZED_PRICE_RE = re.compile(
    r"(?P<name>[\u4e00-\u9fa5A-Za-z0-9]{1,12}?)(?P<num>\d+(?:\.\d+)?)\s*元"
)
ITEMIZED_DUPLICATE_WINDOW_SECONDS = 60


def _itemized_amounts(text: str) -> list[tuple[str, Decimal]]:
    out: list[tuple[str, Decimal]] = []
    for match in _ITEMIZED_PRICE_RE.finditer(text or ""):
        try:
            out.append((match.group("name"), Decimal(match.group("num"))))
        except (InvalidOperation, ValueError):
            continue
    return out


CROSS_CHANNEL_ORDER_WINDOW_SECONDS = 180
_AMOUNT_IN_TEXT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:元|块)")


def _is_order_channel(record: _Record) -> bool:
    """order（点单）通道产生的事件：`evt_order_*`，或 evidence 标注了 order 抽取器。"""
    event_id = str(record.event_id or "")
    if event_id.startswith("evt_order_"):
        return True
    extractor = str(record.provenance.get("extractor") or "").lower()
    return "order" in extractor


def _mentions_other_amount(record: _Record, amount: Decimal) -> bool:
    """文本里是否还出现**别的**金额（出现就说明它可能不是同一笔）。"""
    text = f"{record.title} {record.quote}"
    for match in _AMOUNT_IN_TEXT_RE.finditer(text or ""):
        try:
            value = Decimal(match.group(1))
        except (InvalidOperation, ValueError):
            continue
        if abs(value - amount) > Decimal("0.005"):
            return True
    return False


def _suppress_cross_channel_order_duplicates(
    records: Sequence[_Record],
) -> tuple[list[_Record], list[dict[str, Any]]]:
    """同一次录音里「点单」与「付款」两条通道各抽一遍 → 合并成一条（2026-10-23 Q7-①）。

    实测 10-19：`watch_6EBCD807…` 同一场录音，
      · `evt_order_9b2c5747dd841eae`「要一个肉夹馍 八块」¥8（order 通道，seg/offset 都是 None）
      · `evt_ctx_fad147c6dc7e0781`「八块」¥8（ctx 通道，seg=watch_000075_000002）
    原始转录是「要一个肉夹馍」(23:53:42) → 「啊，付过了老板」(23:54:59) → 「八块」(23:55:01)，
    **同一笔 ¥8**，只是相隔 82 秒、跨了两个底层 segment。

    判据（四条全满足才合并）：
      1. 外层 `recording_id` 相同且非空；
      2. 金额与币种完全相同；
      3. 时间差 ≤180s；
      4. 至少一侧来自 order 通道，且**另一侧文本里没有第二个不同金额**。
    保留 order 条（有 item、通道语义清晰），另一条打 `suppressed` + `merged_into`。
    10-18 两笔 ¥15.9（止汗露 / 冷冻菠菜，相隔 37 分钟、两条都是 ctx）不满足 #4，
    因此不会被误并。
    """
    orders = [r for r in records if _is_order_channel(r) and r.amount is not None]
    if not orders:
        return list(records), []
    suppressed: dict[str, str] = {}
    removed: list[dict[str, Any]] = []
    for other in records:
        if _is_order_channel(other) or other.amount is None:
            continue
        if other.event_id and other.event_id in suppressed:
            continue
        other_recording = str(other.provenance.get("recording_id") or "")
        if not other_recording:
            continue
        for order in orders:
            if str(order.provenance.get("recording_id") or "") != other_recording:
                continue
            if order.currency != other.currency or order.amount != other.amount:
                continue
            if order.reported_dt is None or other.reported_dt is None:
                continue
            delta = abs((order.reported_dt - other.reported_dt).total_seconds())
            if delta > CROSS_CHANNEL_ORDER_WINDOW_SECONDS:
                continue
            if _mentions_other_amount(other, other.amount):
                continue
            if _mentions_other_amount(order, order.amount):
                continue
            suppressed[str(other.event_id)] = str(order.event_id)
            removed.append(
                {
                    "event_id": other.event_id,
                    "merged_into": order.event_id,
                    "amount": _money_float(other.amount),
                    "currency": other.currency,
                    "delta_seconds": round(delta, 1),
                    "reason": "cross_channel_order_duplicate",
                    "provenance": {
                        "order": dict(order.provenance),
                        "duplicate": dict(other.provenance),
                    },
                }
            )
            break
    kept: list[_Record] = []
    for record in records:
        target = suppressed.get(str(record.event_id))
        if target:
            record.explanation.append(f"cross_channel_duplicate_of:{target}")
            continue
        kept.append(record)
    return kept, removed


def _suppress_itemized_duplicates(
    records: Sequence[_Record],
) -> tuple[list[_Record], list[dict[str, Any]]]:
    """同一段音频被两套抽取器各抽一遍时的合计/明细重复（2026-10-23 P-50）。

    实测 10-20：同一段音频（sha256 `f1d24d89ac43`）被 live 与 batch 分别抽成
    「购买肉夹馍8元、煎豆腐7元、捞粉10元」(¥25.00) 与「购买肉夹馍8元 / 购买煎豆腐7元 /
    购买捞粉10元」(¥8+¥7+¥10)，四条 offset 完全相同（181.58–189.82s），
    结果一顿饭被算成 ¥50。

    判定（宁漏不错，只认"能对得上的明细"）：
      · 合计方：文本里出现 ≥2 个「物品+数字元」，且这些数字之和**精确等于**该事件金额；
      · 明细方：与合计方 reported_at 相差 ≤60s、金额是合计里列过的数字、物品名出现在合计文本里；
      · 明细方合计必须**恰好**等于合计方金额（分不差），且至少 2 条；
      · 满足则**丢合计、留明细**（保留逐项金额，信息更多），并写进 duplicates_removed。
    """
    kept: list[_Record] = []
    removed: list[dict[str, Any]] = []
    details_pool = [r for r in records if r.amount is not None]
    for record in records:
        if record.amount is None or record.reported_dt is None:
            kept.append(record)
            continue
        text = f"{record.title} {record.item} {record.quote}"
        parts = _itemized_amounts(text)
        # title 与 item 往往同文，拼在一起会重复计数 → 先按 (物品, 金额) 去重
        seen_parts: set[tuple[str, Decimal]] = set()
        unique_parts: list[tuple[str, Decimal]] = []
        for name, value in parts:
            key = (name, value)
            if key in seen_parts:
                continue
            seen_parts.add(key)
            unique_parts.append((name, value))
        parts = unique_parts
        if len(parts) < 2:
            kept.append(record)
            continue
        if sum((value for _, value in parts), Decimal(0)) != record.amount:
            kept.append(record)
            continue
        listed = {str(name).strip() for name, _ in parts if str(name).strip()}
        values = {value for _, value in parts}
        matched: list[_Record] = []
        for detail in details_pool:
            if detail is record or detail.amount is None or detail.reported_dt is None:
                continue
            if detail.amount not in values:
                continue
            delta = abs((detail.reported_dt - record.reported_dt).total_seconds())
            if delta > ITEMIZED_DUPLICATE_WINDOW_SECONDS:
                continue
            detail_text = f"{detail.title} {detail.item}"
            if not any(name and name in detail_text for name in listed):
                continue
            matched.append(detail)
        unique = {d.event_id: d for d in matched if d.event_id}
        if len(unique) < 2:
            kept.append(record)
            continue
        if sum((d.amount for d in unique.values()), Decimal(0)) != record.amount:
            kept.append(record)
            continue
        removed.append(
            {
                "event_id": record.event_id,
                "title": record.title,
                "amount": _money_float(record.amount),
                "currency": record.currency,
                "detail_event_ids": sorted(unique),
                "reason": "itemized_aggregate_matches_nearby_details",
                "provenance": dict(record.provenance),
            }
        )
    return kept, removed


# 活动层 item 状态的计数闸门：[2026-11-03 Q809]
# 只有「据陈述已完成 / 已完成 / completed / done」才算钱。
# 为什么必须卡这么死：11-01 19:25 那条活动的 items 是
#   `已点单/请求` ¥10 、`已陈述` None 、`已报价` ¥11.5
# 11.5 是店员报价、10 是本人点单 —— 都不是“已经付了的钱”。
# 旧报告（progress.md 05:20）已经判过「这是正确保守行为，不是 bug」，这条正则就是把那个判断固化。
_ACTIVITY_ITEM_COMPLETED_RE = re.compile(r"(?:据陈述已完成|已完成|完成|completed|done)", re.I)
_ACTIVITY_NOT_DONE_RE = re.compile(r"planned|cancel|计划|取消|已播报|已提出|requested", re.I)
_ACTIVITY_INTENT_RE = re.compile(r"想买|要买|打算|准备买|等会|明天|后天|下次|多少钱|吗[？?]?$|能不能|要不要|假如|如果")


def _activity_item_is_completed(item: Mapping[str, Any]) -> bool:
    status = str(item.get("status") or "").strip()
    if not status:
        return False
    return bool(_ACTIVITY_ITEM_COMPLETED_RE.search(status))


def _activity_quotes(item: Mapping[str, Any]) -> list[str]:
    quotes: list[str] = []
    for entry in item.get("evidence") or []:
        if not isinstance(entry, Mapping):
            continue
        text = str(entry.get("quote") or entry.get("text") or "").strip()
        if text and text not in quotes:
            quotes.append(text)
    return quotes


_MONEY_TOKEN_RE = re.compile(
    r"(?:[¥￥]\s*\d)|(?:\d+(?:\.\d+)?\s*(?:元|块|块钱))|(?:[零〇一二两三四五六七八九十百千]+\s*(?:元|块|块钱))"
)


def _has_money_token(text: str) -> bool:
    """文本里是否真的有“钱”的痕迹（不是时长/数量词）。

    为什么需要：[2026-11-03 Q809] 只靠「完成」状态会把「做了四十五分钟有氧」
    「终端窗口已经换新」这类**带数字但不是钱**的 item 也送进账本，变成一堆
    amount_unknown 噪声（实测 8 条）。金额单位必须出现过。
    """
    return bool(_MONEY_TOKEN_RE.search(str(text or "")))


def _quote_overlaps(first: str, second: str) -> bool:
    """两条原话是否描述同一句话（归一化后互为子串）。"""
    a = _normalise_quote(first)
    b = _normalise_quote(second)
    if not a or not b:
        return False
    return a in b or b in a


_SAME_QUOTE_AMOUNT_FROM = "2026-11-05"


def _event_quote_amounts(events: Sequence[Mapping[str, Any]]) -> dict[str, Decimal]:
    """事件原话 → 金额。同一句话对应多个金额时不记，按没有金额处理。"""
    out: dict[str, Decimal] = {}
    ambiguous: set[str] = set()
    for event in events:
        evidence = event.get("evidence")
        data = event.get("data")
        expense = data.get("expense") if isinstance(data, Mapping) else None
        quote = str(evidence.get("quote") or "") if isinstance(evidence, Mapping) else ""
        amount = _decimal(expense.get("amount")) if isinstance(expense, Mapping) else None
        if not quote or amount is None:
            continue
        if quote in out and out[quote] != amount:
            ambiguous.add(quote)
        out[quote] = amount
    return {quote: amount for quote, amount in out.items() if quote not in ambiguous}


def _is_v3_fact(activity, item):
    return any(value == 3 for value in (activity.get("fact_schema_version"), activity.get("schema_version"),
                                       item.get("fact_schema_version"))) or str(activity.get("template_version") or "").startswith("v3.")


def _v3_expense_event(activity, item, index, event_quotes, event_quote_amounts):
    """Typed v3 admission only: never recover money from action or ASR text."""
    expense = item.get("expense")
    if item.get("fact_type") != "expense" or not isinstance(expense, Mapping) or not expense:
        return None
    lifecycle = activity.get("lifecycle") or item.get("v3_lifecycle") or "unknown"
    if lifecycle == "media" or item.get("item_lifecycle") == "media":
        return None
    evidence = [e for e in item.get("evidence") or [] if isinstance(e, Mapping)]
    if evidence and all(e.get("v3_work_dictation") for e in evidence):
        return None
    amount = _decimal(expense.get("amount"))
    valid_amount = amount is not None and amount.is_finite() and amount > 0
    quotes = _activity_quotes(item)
    overlapping = [old for q in quotes for old in event_quotes if _quote_overlaps(q, old)]
    if overlapping and not (valid_amount and event_quote_amounts is not None
                            and all(event_quote_amounts.get(old) not in (None, amount) for old in overlapping)):
        return None
    reason = None
    if lifecycle != "live":
        reason = "v3_unknown_lifecycle"
    elif not has_self_evidence(item):
        reason = "v3_self_evidence_missing"
    elif item.get("review_status") == "non_confirmed":
        reason = "v3_review_pending"
    elif not valid_amount:
        # [2026-11-08] 金额没录清（amount 缺失/非正/非法）时，先说"金额待补填"，
        # 而不是笼统的"付款未确认"——前者用户能直接补，后者看不出缺什么。
        # 实例：11-07 03:51 粉摊点炒河粉，本人点单+店家应答「好的」，但价格没录清。
        reason = "v3_amount_unverified"
    elif item.get("status") != "completed" or expense.get("payment_status") != "paid":
        reason = "v3_payment_unconfirmed"
    ident = str(activity.get("activity_id") or "")
    primary = next((e for e in evidence if e.get("source_label") == "self"), evidence[0] if evidence else {})
    return {"event_id": f"{ident}#it{index + 1}", "title": str(expense.get("item") or item.get("action") or ""),
            "categories": ["expense"], "evidence": {**primary, "quote": max(quotes, key=len) if quotes else "",
            "quotes": quotes, "source": "activity_track"}, "data": {"expense": dict(expense)},
            "time": {"absolute": item.get("spoken_at") or activity.get("reported_at")},
            "source": "activity_track", "activity_id": ident, "fact_schema_version": 3,
            **({"activity_pending_reason": reason} if reason else {})}


def _activity_items_as_existing_events(
    activities: Sequence[Mapping[str, Any]] | None,
    *,
    event_quotes: Sequence[str] = (),
    rules_from: str | None = None,
    event_quote_amounts: Mapping[str, Decimal] | None = None,
) -> list[dict[str, Any]]:
    """把**活动层**已完成且带金额的 item 转成事件形状，供同一条账本管道处理。

    [2026-11-03 Q809] 为什么需要：11-01 的 `/api/daily` 事件轨是 **0 条**（formal 管道那天
    什么都没产出），而 32 条活动里有「买了一包烟，十块钱，又买了一瓶水，六块九」。
    账本只读事件 ⇒ 钱永远为 0。本函数是**显式 opt-in** 的补位通路（见 build_expense_ledger 的
    ``activities=`` 参数），不改变没有任何活动输入时的旧行为。
    """
    if not activities:
        return []
    out: list[dict[str, Any]] = []
    for activity in activities:
        if not isinstance(activity, Mapping) or activity.get("suppressed"):
            continue
        activity_id = str(activity.get("activity_id") or activity.get("activityId") or "")
        activity_title = str(activity.get("title") or "")
        fallback_time = activity.get("occurrence_start") or activity.get("reported_at")
        for index, item in enumerate(activity.get("items") or []):
            if not isinstance(item, Mapping):
                continue
            if _is_v3_fact(activity, item):
                event = _v3_expense_event(activity, item, index, event_quotes, event_quote_amounts)
                if event is not None:
                    out.append(event)
                continue
            # [2026-11-07] 闸门 0：**没有本人证据、又不是 expense 类型**的活动子项，
            # 一律不得进正式消费账。
            # 实例：11-06 `act-20261106-frag-499-01#it1`（视频里他人的「五根，两块一把，
            # 但是稍微应该算一块钱一把」）—— fact_type=none、证据全 non_self，
            # 只因为 status=completed 且原话里有「买了」就被算成 2 元。
            # 支付播报（收钱吧/到账）走 `_ACTIVITY_GAP_BROADCAST_RE` 那条待确认通道，
            # 不依赖这里放行。
            # 只拦「证据**全部**是他人音轨（non_self）」的情况：
            # `unknown` 标签常常是本人被自动说话人分离标错（实例 10-29「买了一份寿司，
            # 十四块钱。」near+unknown），不能一起拦，否则会把真账打掉。
            if item.get("fact_type") != "expense":
                labels = {str(e.get("source_label") or "") for e in item.get("evidence") or []
                          if isinstance(e, Mapping)}
                if not has_self_evidence(item) and labels and labels <= {"non_self"}:
                    continue
            raw_amount = item.get("amount")
            quotes = _activity_quotes(item)
            action = str(item.get("action") or "")
            completed = _activity_item_is_completed(item)
            spoken = str(item.get("spoken_at") or fallback_time or "")[:10]
            # 用户 2026-11-04 确认的两条规则，只对分界日之后生效，历史账冻结。
            new_rules = bool(rules_from and spoken and spoken >= rules_from
                             and has_self_evidence(item)
                             and not _ACTIVITY_NOT_DONE_RE.search(str(item.get("status") or ""))
                             and not any(_ACTIVITY_INTENT_RE.search(q) for q in quotes))
            quote_paid = bool(_COMPLETION_RE.search(" ".join(quotes)))
            if not completed and not (new_rules and quote_paid):
                continue
            # 金额闸门：要么结构化 amount > 0，要么原话里出现过金额单位。
            # 两者都没有 = 不是消费 item，不进账本（避免 amount_unknown 噪声）。
            amount_value = _decimal(raw_amount)
            has_amount = amount_value is not None and amount_value > 0
            if not has_amount and not any(_has_money_token(q) for q in quotes):
                continue
            # 消费语义闸门：必须真的有“买了/花了/付了/支付/下单”这类**付款完成**表述。
            # 为什么要这一步：11-03 有一条 `completed` 的 item「转述该款折叠屏手机售价约两千美元」
            # （原话「报道称，该款手机售价约两千美元。」）—— 它是**转述报价**，不是消费。
            # 只卡「status=completed + 有金额」会把它算成 2000 CNY（实测假阳性）。
            bare_purchase = False
            if not _COMPLETION_RE.search(f"{action} {' '.join(quotes)}"):
                bare_purchase = new_rules and has_amount and any("买" in q for q in quotes)
                if not bare_purchase:
                    continue
            # 与事件轨去重：原话已经被事件承载的，不再重复计一次。
            # 一句话可能说了好几笔（“买冰杯花了两块，买菜花了十六”），事件只抽到其中一笔时，
            # 金额不同的那笔不能算重复。只对分界日之后生效，历史账不变。
            overlapping = [existing for q in quotes for existing in event_quotes if _quote_overlaps(q, existing)]
            if overlapping and not (
                has_amount and spoken >= _SAME_QUOTE_AMOUNT_FROM and event_quote_amounts is not None
                and all(event_quote_amounts.get(existing) not in (None, amount_value) for existing in overlapping)
            ):
                continue
            out.append(
                {
                    "event_id": f"{activity_id}#it{index + 1}",
                    "title": action or activity_title,
                    "categories": ["expense"],
                    "evidence": {
                        "quote": max(quotes, key=len) if quotes else "",
                        "quotes": quotes,
                        "source": "activity_track",
                    },
                    "data": {
                        "expense": {
                            "amount": raw_amount,
                            "currency": item.get("currency"),
                            "item": action or activity_title,
                        }
                    },
                    "time": {"absolute": item.get("spoken_at") or fallback_time},
                    "source": "activity_track",
                    "activity_id": activity_id,
                    **({"activity_pending_reason": "activity_purchase_unconfirmed"} if bare_purchase else {}),
                }
            )
    return out


# 规则三、四只补待确认；不改变既有活动入账路径。用户 2026-11-05：没说币种一律按人民币。
_ACTIVITY_GAP_FROM = datetime(2026, 10, 26).date().isoformat()
_ACTIVITY_GAP_OTHER_RE = re.compile(r"他|她|别人|朋友|同事|报道称|听说|据说|售价|报价|没买|没拿|未买|未拿|不买|不拿|取消|去年|往年|以前|不抵|优惠|券|减\s*[零〇一二两三四五六七八九十百千\d]|是吧|吗|要搞|估计|大概|左右|讨论|转手|卖出|块儿")
_ACTIVITY_GAP_BROADCAST_RE = re.compile(r"(?:收钱吧|支付宝|微信|收款).{0,8}到账|到账\s*[零〇一二两三四五六七八九十百千\d.]+\s*元")
_ACTIVITY_GAP_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?|[零〇一二两三四五六七八九十百千]+")
# [2026-11-07] 「一」后面的**固定副词/虚词**不是数量也不是金额：
# 「看一下是消费的什么」曾因为「一」+「费」被判成"说了数字 + 说了费用"，
# 于是这条查账问话被塞进待确认。这里把这类词尾并入"数量后缀"，
# 使 `number_spoken` 判否；`点` 只在**不是小数点**（后面不跟数字）时才排除，
# 避免误伤「三点五元」「四十一点六」。
_ACTIVITY_GAP_QUANTITY_RE = re.compile(
    r"\s*(?:个|包|瓶|袋|盒|份|件|公斤|千克|斤|克|个月|月|年|天|分钟|小时|次"
    r"|下|起|样|直|共|般|会|边|定|致|块儿|点(?![0-9零〇一二两三四五六七八九十]))"
)
# [2026-11-07] 针对**系统/转写/统计**的查账与核对问话：不是本人消费。
# 实例：
#   11-06「快递费四十一点六从哪里来，转写没有这个数，我是明确的说了。」
#   11-07「看一下是消费的什么，金额是多少，有没有统计好」
_ACTIVITY_GAP_AUDIT_RE = re.compile(
    r"从哪里来|哪里来的|哪来的|转写没有|转写里没有|没有这个数|记录中未显示|"
    r"有没有统计|统计好|算对了吗|算错了|核对一下|对一下账|查一下账|"
    r"消费的什么|消费的是什么|消费了什么|金额是多少|多少钱来着"
)
_ACTIVITY_GAP_PRICE_OFFER_RE = re.compile(r"[元块](?:钱)?(?:买)?[零〇一二两三四五六七八九十百千\d]+\s*(?:个|份|包|瓶|件|袋|盒)")
_ACTIVITY_GAP_SPEND_RE = re.compile(r"订阅|费|花了|付了|支付|元|块")
_ACTIVITY_GAP_PURCHASE_RE = re.compile(r"买了|拿了|拿\s*(?:一|两|[1-9])\s*(?:包|瓶|袋|盒|份|件|个)")


def _activity_items_as_events(
    activities: Sequence[Mapping[str, Any]] | None,
    *,
    event_quotes: Sequence[str] = (),
    rules_from: str | None = None,
    event_quote_amounts: Mapping[str, Decimal] | None = None,
) -> list[dict[str, Any]]:
    out = _activity_items_as_existing_events(activities, event_quotes=event_quotes, rules_from=rules_from,
                                             event_quote_amounts=event_quote_amounts)
    existing_ids = {event["event_id"] for event in out}
    for activity in activities or []:
        if not isinstance(activity, Mapping) or activity.get("suppressed"):
            continue
        if activity.get("lifecycle") == "media" or str(activity.get("category") or "").startswith("浏览"):
            continue
        activity_id = str(activity.get("activity_id") or activity.get("activityId") or "")
        if re.search(r"优惠|促销|券", str(activity.get("title") or "")):
            continue
        for index, item in enumerate(activity.get("items") or []):
            if not isinstance(item, Mapping) or item.get("item_lifecycle") == "media" or _is_v3_fact(activity, item):
                continue
            event_id = f"{activity_id}#it{index + 1}"
            spoken = item.get("spoken_at") or activity.get("occurrence_start") or activity.get("reported_at")
            parsed = _parse_dt(spoken)
            if parsed is None or parsed.date().isoformat() < _ACTIVITY_GAP_FROM:
                continue
            # 规则五：现场有本人说话时听到的收款播报，很可能是自己付的，进待确认。
            broadcast = next((str(e.get("quote") or "") for e in item.get("evidence") or []
                              if isinstance(e, Mapping) and _ACTIVITY_GAP_BROADCAST_RE.search(str(e.get("quote") or ""))), "")
            if broadcast and not any(_quote_overlaps(broadcast, q) for q in event_quotes) and _decimal(item.get("amount")) and any(
                    has_self_evidence(other) for other in activity.get("items") or [] if isinstance(other, Mapping)):
                out = [event for event in out if event["event_id"] != event_id]
                out.append({
                    "event_id": event_id, "title": broadcast, "categories": ["expense"],
                    "evidence": {"quote": broadcast, "source": "activity_track"},
                    "data": {"expense": {"amount": _money_float(_decimal(item.get("amount"))),
                                         "currency": item.get("currency"), "item": item.get("action") or activity.get("title")}},
                    "time": {"absolute": spoken}, "source": "activity_track", "activity_id": activity_id,
                    "activity_pending_reason": "activity_payment_broadcast_unconfirmed",
                })
                continue
            if event_id in existing_ids:
                continue
            # 只用本人证据；他人的数字不能补足本人的消费语境。
            quotes = list(dict.fromkeys(str(e.get("quote") or e.get("text") or "").strip()
                for e in item.get("evidence") or [] if isinstance(e, Mapping)
                and e.get("source_label") == "self" and (e.get("quote") or e.get("text"))))
            text = " ".join(quotes)
            if (not text or _ACTIVITY_INTENT_RE.search(text) or _ACTIVITY_GAP_OTHER_RE.search(text)
                    or _ACTIVITY_GAP_PRICE_OFFER_RE.search(text)
                    or _ACTIVITY_GAP_AUDIT_RE.search(f"{text} {item.get('action') or ''}")):
                continue
            if event_quotes and any(_quote_overlaps(text, q) or any(_quote_overlaps(s, q) for s in quotes)
                                    for q in event_quotes):
                continue
            number_spoken = any(not _ACTIVITY_GAP_QUANTITY_RE.match(text[m.end():])
                                for m in _ACTIVITY_GAP_NUMBER_RE.finditer(text))
            has_money = bool(number_spoken and _ACTIVITY_GAP_SPEND_RE.search(text))
            # 数量/体重不是金额；没有金额的购买仍可作为规则四的候选。
            amount = _decimal(item.get("amount"))
            if item.get("amount_kind") in {"weight", "quantity"}:
                has_money = False
            proposed = amount if has_money and amount is not None and amount > 0 else None
            if has_money and proposed is None and _has_money_token(text):
                proposed = _decimal(_extract_amount(text))
            if has_money:
                reason = "activity_spend_unconfirmed"
            elif _ACTIVITY_GAP_PURCHASE_RE.search(text) and not _has_money_token(text):
                reason = "activity_purchase_amount_unspoken"
                proposed = None
            elif (item.get("fact_type") == "expense"
                    and amount is not None and amount > 0
                    and not _ACTIVITY_NOT_DONE_RE.search(str(item.get("status") or ""))):
                # [2026-11-07] 金额只出现在**店员/对方**的引文里，本人只回一句确认
                # （11-07 act_store_order_chaofen_01#it1：「十三块四。」在 unknown，
                # 本人是「嗯，没有。」）——以前会被静默丢弃。现在进待确认，
                # 绝不直接计入已付金额。
                other_quotes = [str(e.get("quote") or e.get("text") or "").strip()
                                for e in item.get("evidence") or [] if isinstance(e, Mapping)
                                and e.get("source_label") != "self"
                                and (e.get("quote") or e.get("text"))]
                if not any(_has_money_token(q) or _ACTIVITY_GAP_NUMBER_RE.search(q) for q in other_quotes):
                    continue
                reason = "amount_from_other_speaker_unconfirmed"
                proposed = amount          # Decimal，下游 _money_float() 再转 float
            else:
                continue
            out.append({
                "event_id": event_id, "title": text, "categories": ["expense"],
                "evidence": {**next(e for e in item["evidence"] if isinstance(e, Mapping)
                                      and e.get("source_label") == "self"),
                             "quote": text, "quotes": quotes, "source": "activity_track"},
                "data": {"expense": {"amount": _money_float(proposed), "currency": item.get("currency"),
                                       "item": item.get("action") or activity.get("title")}},
                "time": {"absolute": spoken}, "source": "activity_track", "activity_id": activity_id,
                "activity_pending_reason": reason,
            })
    return out


def _apply_confirmed(event: dict[str, Any], decision: float | Mapping[str, Any]) -> dict[str, Any]:
    """用户确认的金额直接入账：去掉待确认标记，金额、币种和名称以用户为准。"""
    amount = decision["amount"] if isinstance(decision, Mapping) else decision
    label = decision.get("label") if isinstance(decision, Mapping) else None
    event = {key: value for key, value in event.items() if key != "activity_pending_reason"}
    data = dict(event.get("data") or {})
    data["expense"] = {**(data.get("expense") or {}), "amount": amount, "currency": "CNY"}
    if label:
        data["expense"]["item"] = label
        event["title"] = label
    event["data"] = data
    event["user_confirmed"] = True
    return event


def build_expense_ledger(
    events: Sequence[Mapping[str, Any]],
    *,
    activities: Sequence[Mapping[str, Any]] | None = None,
    review: Mapping[str, str] | None = None,
    activity_rules_from: str | None = None,
    confirmed: Mapping[str, float | Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a conservative expense ledger from one decision branch.

    ``confirmed`` 是用户确认过的金额（人民币）：{event_id: 金额}，优先于任何规则。

    Stable fields for callers (statistics / UI):

    ``transactions``
        Counted financial transactions.  Amounts are finite positive floats;
        ``currency`` is explicit per transaction.
    ``totals_by_currency`` / ``category_totals_by_currency``
        Payment totals (consumption + verified transfer), never units mixed
        across currencies.  Withdrawals/refunds are separate.
    ``total_amount`` / ``currency`` / ``category_breakdown``
        Legacy CNY-only aliases.  Do not use them for USD/EUR etc.
    ``pending_transactions`` / ``pending_count`` / ``pending_by_reason``
        Unknown, unverified, non-positive or invalid amounts.  These are never
        silently converted to zero and never included in totals.
    ``activities`` (关键字参数，opt-in)
        [2026-11-03 Q809] 活动层（activity-timeline）的列表。当事件轨缺失或不全时
        （实例：2026-11-01 事件轨 0 条，但活动里有「买了一包烟，十块钱，又买了一瓶水，六块九」），
        把其中**状态已完成且带金额**的 item 当成事件一起算。不传 = 行为与旧版逐位一致。
    ``aggregate_reconciliations``
        Summary rows that were matched to nearby detail rows and removed from
        totals (provenance preserved).
    ``duplicates_removed``
        Same-quote / same-reported-minute duplicates collapsed inside this
        ledger.  The original event ids stay in the relation history.
    """
    excluded: list[dict[str, Any]] = []
    records: list[_Record] = []

    source_events: list[Mapping[str, Any]] = [
        event for event in events if isinstance(event, Mapping)
    ]
    if activities:
        source_events.extend(
            _activity_items_as_events(
                activities,
                event_quotes=[
                    str((e.get("evidence") or {}).get("quote") or "")
                    for e in source_events
                    if isinstance(e.get("evidence"), Mapping)
                ],
                rules_from=activity_rules_from,
                event_quote_amounts=_event_quote_amounts(source_events),
            )
        )
    if confirmed:
        source_events = [
            _apply_confirmed(dict(event), confirmed[str(event.get("event_id") or "")])
            if str(event.get("event_id") or "") in confirmed else event
            for event in source_events
        ]

    for event in source_events:
        if not isinstance(event, Mapping):
            continue
        # [2026-10-22] 被跨分段合并掉的事件（suppressed=True）不再进账本：
        # 它们的金额信息已经被 anchor 事件承载，重复计入会翻倍。
        if event.get("suppressed"):
            continue
        data = event.get("data") if isinstance(event.get("data"), Mapping) else {}
        expense = (
            data.get("expense") if isinstance(data.get("expense"), Mapping) else {}
        )
        evidence = (
            event.get("evidence") if isinstance(event.get("evidence"), Mapping) else {}
        )
        categories = event.get("categories") or []
        if not isinstance(categories, list):
            categories = [categories]
        title = str(event.get("title") or "")
        quote = str(evidence.get("quote") or "")
        text = f"{title} {quote}"

        is_expense = (
            bool(expense)
            or "expense" in categories
            or "消费" in title
            or "支出" in title
            or "购买" in title
        )
        if not is_expense:
            continue

        review_action = (review or {}).get(str(event.get("event_id") or ""))
        if review_action == "exclude":
            excluded.append(
                {
                    "event_id": event.get("event_id"),
                    "title": title,
                    "quote": quote,
                    "reason": "user_review_excluded",
                    "provenance": _provenance(event, evidence),
                }
            )
            continue

        # [2026-10-23 P-50 三档] A=本人自述（记账）／B=播报类（待确认，不进总额）／
        # C=non_self 非播报（丢弃，已有 non_fact 规则兜底）。
        # 播报类不能只看 speaker：10-18 有 `self` 的「像那个播报收款收款二十二元」，
        # 那是在**描述播报现象**，不是自己花钱。
        has_order_evidence, order_pending_reason, order_evidence_info = (
            _evaluate_order_price_evidence(event, evidence, data, text)
        )
        is_broadcast = _is_broadcast_event(event, evidence, text)
        is_pos_broadcast_for_bucket = False
        if has_order_evidence:
            # 具有明确分角色的订单+店员报价证据，背景 POS 仅为伴随环境音，不把整单当播报
            is_broadcast = False
            pos_pending_reason = None
        else:
            # [P08] 归类和原因分开：播报一律进播报待确认，具体原因（金额不符、未归属等）照常保留。
            is_pos_broadcast_for_bucket = is_broadcast
            if is_broadcast and order_pending_reason:
                pos_pending_reason = order_pending_reason
            else:
                pos_pending_reason = (
                    "pos_broadcast_pending_confirmation"
                    if is_broadcast
                    else _pos_broadcast_pending_reason(event, evidence, text)
                )
        if review_action == "pending":
            is_broadcast = False
            is_pos_broadcast_for_bucket = False
            pos_pending_reason = "user_review_pending"
        elif event.get("user_confirmed"):
            is_broadcast = False
            is_pos_broadcast_for_bucket = False
            pos_pending_reason = None
        elif event.get("activity_pending_reason"):
            pos_pending_reason = str(event["activity_pending_reason"])

        states = _collect_states(event, evidence, data)
        confirmed_order = _confirmed_self_order_context(event, evidence, data)
        reason = _non_fact_reason(states, text, confirmed_order=confirmed_order)
        if reason:
            excluded.append(
                {
                    "event_id": event.get("event_id"),
                    "title": title,
                    "quote": quote,
                    "reason": reason,
                    "provenance": _provenance(event, evidence),
                }
            )
            continue

        activity_type = str(event.get("activity_type") or "").strip().lower()
        item = str(expense.get("item") or title).strip()
        kind = _detect_kind(activity_type, title, quote, item, expense)
        category = _detect_category(kind, item, title, quote)

        declared = str(expense.get("currency") or "").strip() or None
        currency, currency_source, currency_conflict, currency_note = _detect_currency(
            declared, quote
        )

        raw_amount = expense.get("amount")
        l2_blocked, l2_reason, l2_source, l2_contract = _l2_expense_review_signal(
            evidence
        )
        l2_accepted = bool(
            isinstance(l2_contract, Mapping)
            and str(l2_contract.get("status") or "").strip().lower() == "accepted"
        )
        proposed = _decimal(evidence.get("proposed_expense_amount"))
        if proposed is not None and proposed <= 0:
            proposed = None
        amount = _decimal(raw_amount)
        amount_source = "expense_amount"
        pending_reason: str | None = None
        explanation: list[str] = []
        if is_pos_broadcast_for_bucket:
            explanation.append("pos_broadcast_bucket")
        if pos_pending_reason:
            # 保留原金额作为「候选值」：不计数，但用户要能在待确认区看到播报说了多少
            if amount is not None and amount > 0:
                proposed = amount
            amount = None
            amount_source = pos_pending_reason
            pending_reason = pos_pending_reason
            explanation.append(f"{pos_pending_reason}_not_counted")
            explanation.append(f"pos_direction:{_pos_broadcast_direction(text)}")
        elif l2_blocked:
            # An explicit pending/rejected L2 result is authoritative.  Never
            # let the legacy quote parser resurrect a rejected amount, and do
            # not count a raw amount that leaked through despite rejection.
            amount = None
            amount_source = "l2_review_pending"
            pending_reason = l2_reason or "unverified_expense_data"
            proposed = _l2_candidate_amount(evidence, expense, l2_contract) or proposed
            explanation.append("amount_not_recovered_for_explicit_l2_review")
            explanation.append(f"l2_review_source:{l2_source}")
        elif amount is None or amount <= 0:
            if l2_accepted:
                # An accepted contract is the model-amount authority.  A
                # missing amount in that state is a contract inconsistency,
                # not an invitation to re-parse money grammar.
                amount = None
                pending_reason = "amount_unknown"
                explanation.append("accepted_contract_amount_missing_not_reparsed")
            else:
                # [2026-11-07] 媒体旁白 / 量词用法不补金额（保持 None → 待确认）。
                media_blocked = _is_media_source(event, evidence, data)
                measure_blocked = _kuai_is_measure_word(
                    " ".join([title, quote] + [str(t) for t in _evidence_quote_texts(evidence)]))
                if media_blocked or measure_blocked:
                    recovered = None
                    amount = None
                    pending_reason = "amount_unknown"
                    explanation.append(
                        "amount_not_recovered_from_media_narration" if media_blocked
                        else "amount_not_recovered_kuai_is_measure_word"
                    )
                else:
                    recovered = _recover_amount_from_evidence(evidence, quote, proposed)
                    if recovered is not None and recovered > 0:
                        amount = recovered
                        amount_source = "recovered_quote"
                        explanation.append("amount_missing_recovered_from_validated_quote")
                    else:
                        amount = None
                        if raw_amount is None:
                            pending_reason = "amount_unknown"
                        elif _decimal(raw_amount) is None:
                            pending_reason = "invalid_amount"
                        else:
                            pending_reason = "non_positive_amount"
                        explanation.append("amount_kept_pending_not_zero")
        if amount is not None and currency == "UNKNOWN":
            pending_reason = "currency_unknown"
            explanation.append(currency_note or "currency_not_verified")
        if currency_conflict and currency_note:
            explanation.append(currency_note)
        if kind == "withdrawal":
            explanation.append("withdrawal_tracked_separately")
        elif kind == "transfer":
            explanation.append("transfer_treated_as_real_payment")
        elif kind == "refund":
            explanation.append("refund_tracked_separately")

        # [2026-11-07] 视频/直播音轨里的第三人称金额：不进账、进待确认。
        # 只在还没有更重要的 pending 原因时生效（不覆盖 amount_unknown / currency_unknown）。
        if pending_reason is None:
            media_reason = media_audio_pending_reason(event)
            if media_reason:
                pending_reason = media_reason
                explanation.append(f"{media_reason}_not_counted")

        reported_raw = evidence.get("reported_at") or (event.get("time") or {}).get(
            "absolute"
        )
        reported_dt = _parse_dt(reported_raw)
        clock = None
        time_info = event.get("time") if isinstance(event.get("time"), Mapping) else {}
        if time_info.get("clock"):
            clock = str(time_info.get("clock"))
        elif reported_dt is not None:
            clock = reported_dt.strftime("%H:%M")

        record_provenance = _provenance(event, evidence)
        if l2_blocked:
            record_provenance["l2_review"] = {
                "source": l2_source,
                "status": "pending",
                "reason": pending_reason,
                "contract_version": (
                    l2_contract.get("version")
                    if isinstance(l2_contract, Mapping)
                    else None
                ),
            }

        record = _Record(
            event_id=str(event.get("event_id")) if event.get("event_id") else None,
            title=title,
            quote=quote,
            item=item,
            amount=_money(amount) if amount is not None else None,
            currency=currency,
            amount_source=amount_source,
            currency_source=currency_source,
            currency_conflict=currency_conflict,
            category=category,
            kind=kind,
            payment_context=_payment_context(expense, quote),
            explanation=explanation,
            provenance=record_provenance,
            reported_at=str(reported_raw) if reported_raw else None,
            reported_dt=reported_dt,
            clock=clock,
            pending_reason=pending_reason,
            proposed_amount=_money(proposed) if proposed is not None else None,
            review_status=str(event.get("review_status"))
            if event.get("review_status")
            else None,
        )
        records.append(record)

    # --- Same-evidence duplicate defence (read-level, non-destructive) ---
    kept: list[_Record] = []
    duplicate_index: dict[tuple[Any, ...], _Record] = {}
    duplicates_removed: list[dict[str, Any]] = []
    for record in sorted(records, key=_sort_key):
        quote_key = _normalise_quote(record.quote)
        minute = record.minute_key
        if not quote_key or minute is None:
            kept.append(record)
            continue
        key = (
            minute,
            record.currency,
            record.amount,
            record.pending_reason,
            record.proposed_amount if record.amount is None else None,
        )
        existing = duplicate_index.get(key)
        if (
            existing is not None
            and existing.event_id != record.event_id
            and _quotes_compatible(existing.quote, record.quote)
            and _items_compatible(existing, record)
            and _same_evidence_anchor(existing, record)
        ):
            if record.event_id:
                existing.duplicate_ids.append(record.event_id)
            existing.provenance.setdefault("duplicate_event_ids", []).append(
                record.event_id
            )
            duplicates_removed.append(
                {
                    "kept_event_id": existing.event_id,
                    "removed_event_id": record.event_id,
                    "reason": "same_quote_amount_and_evidence_anchor",
                    "quote": record.quote,
                    "reported_at": record.reported_at,
                    "item": record.item,
                }
            )
            continue
        # A same-minute but different timestamp is a genuine repeat purchase;
        # keep both and allow a later, separate duplicate to match this record.
        duplicate_index[key] = record
        kept.append(record)

    # --- 同一次录音里「点单」与「付款」两条通道各抽一遍 → 只留一条 ---
    channel_kept, channel_removed = _suppress_cross_channel_order_duplicates(kept)
    if channel_removed:
        kept = channel_kept
        duplicates_removed.extend(channel_removed)

    # --- 同一段音频被两套抽取器各抽一遍：合计 + 明细 → 只留明细 ---
    itemized_kept, itemized_removed = _suppress_itemized_duplicates(kept)
    if itemized_removed:
        kept = itemized_kept
        duplicates_removed.extend(itemized_removed)

    # --- Aggregate-vs-detail reconciliation ---
    valid = [r for r in kept if r.amount is not None and r.pending_reason is None]
    aggregates = [r for r in valid if _is_aggregate(r)]
    details = [r for r in valid if not _is_aggregate(r)]
    reconciled_ids: set[str] = set()
    reconciliations: list[dict[str, Any]] = []
    unreconciled_aggregates: list[dict[str, Any]] = []
    reconciliation_conflicts: list[dict[str, Any]] = []

    for aggregate in aggregates:
        aggregate.aggregate = True
        nearby_details = [
            detail for detail in details if _can_reconcile_nearby(aggregate, detail)
        ]
        matched = _find_reconciliation(aggregate, details)
        if matched:
            reconciled_ids.add(aggregate.event_id or "")
            reconciliations.append(
                {
                    "aggregate_event_id": aggregate.event_id,
                    "detail_event_ids": [r.event_id for r in matched],
                    "amount": _money_float(aggregate.amount),
                    "currency": aggregate.currency,
                    "reported_at": aggregate.reported_at,
                    "quote": aggregate.quote,
                    "title": aggregate.title,
                    "reason": "nearby_detail_sum_matches_aggregate",
                    "provenance": {
                        "aggregate": dict(aggregate.provenance),
                        "details": [dict(r.provenance) for r in matched],
                    },
                }
            )
            aggregate.explanation.append("aggregate_reconciled_with_nearby_details")
        elif nearby_details:
            # Nearby details may already contain the summary's components.  Do
            # not add the summary on top of an ambiguous subset.
            aggregate.pending_reason = "aggregate_reconciliation_ambiguous"
            aggregate.explanation.append(
                "aggregate_withheld_nearby_details_could_double_count"
            )
            reconciliation_conflicts.append(
                {
                    "aggregate_event_id": aggregate.event_id,
                    "amount": _money_float(aggregate.amount),
                    "currency": aggregate.currency,
                    "reported_at": aggregate.reported_at,
                    "quote": aggregate.quote,
                    "title": aggregate.title,
                    "nearby_detail_event_ids": [
                        detail.event_id for detail in nearby_details
                    ],
                    "reason": "aggregate_reconciliation_ambiguous",
                }
            )
        else:
            aggregate.explanation.append(
                "aggregate_kept_no_unambiguous_nearby_detail_match"
            )
            unreconciled_aggregates.append(_record_to_transaction(aggregate))

    transactions: list[_Record] = []
    pending: list[_Record] = []
    pending_broadcast: list[_Record] = []
    for record in kept:
        if (
            record.pending_reason == "pos_broadcast_pending_confirmation"
            or "pos_broadcast_bucket" in record.explanation
        ):
            # B 档：播报只在「待确认」里出现，绝不进 total_amount。
            pending_broadcast.append(record)
            continue
        if record.amount is None or record.pending_reason is not None:
            pending.append(record)
            continue
        if record.event_id and record.event_id in reconciled_ids:
            continue
        transactions.append(record)

    # --- Totals: payments are consumption + verified transfer. ---
    totals_by_currency: dict[str, Decimal] = defaultdict(lambda: Decimal(0))
    category_totals: dict[str, dict[str, Decimal]] = defaultdict(
        lambda: defaultdict(lambda: Decimal(0))
    )
    kind_totals: dict[str, dict[str, Decimal]] = defaultdict(
        lambda: defaultdict(lambda: Decimal(0))
    )
    for record in transactions:
        amount = record.amount or Decimal(0)
        kind_totals[record.kind][record.currency] += amount
        if record.kind in _PAYMENT_KINDS:
            totals_by_currency[record.currency] += amount
            category_totals[record.currency][record.category] += amount

    def _rounded_map(values: Mapping[str, Decimal]) -> dict[str, float]:
        return {
            key: _money_float(value)
            for key, value in sorted(values.items())
            if value != 0
        }

    category_totals_out = {
        currency: _rounded_map(categories)
        for currency, categories in sorted(category_totals.items())
    }
    kind_totals_out = {
        kind: _rounded_map(currencies)
        for kind, currencies in sorted(kind_totals.items())
    }
    pending_by_reason = Counter(
        record.pending_reason or "unknown" for record in pending
    )
    pending_by_currency = Counter(record.currency for record in pending)

    cny_total = totals_by_currency.get("CNY", Decimal(0))
    cny_categories = category_totals.get("CNY", {})
    payment_transactions = [r for r in transactions if r.kind in _PAYMENT_KINDS]
    non_payment_transactions = [r for r in transactions if r.kind not in _PAYMENT_KINDS]
    nonpayment_totals: dict[str, Decimal] = defaultdict(lambda: Decimal(0))
    for record in non_payment_transactions:
        nonpayment_totals[record.currency] += record.amount or Decimal(0)
    transaction_count = len(payment_transactions)
    all_transaction_count = len(transactions)
    payment_count = len(payment_transactions)
    withdrawal_count = sum(1 for r in transactions if r.kind == "withdrawal")

    return {
        "policy_version": POLICY_VERSION,
        "total_amount": _money_float(cny_total),
        "currency": "CNY",
        "category_breakdown": {
            key: _money_float(value) for key, value in sorted(cny_categories.items())
        },
        "totals_by_currency": _rounded_map(totals_by_currency),
        "category_totals_by_currency": category_totals_out,
        "kind_totals_by_currency": kind_totals_out,
        "withdrawal_total_by_currency": _rounded_map(kind_totals.get("withdrawal", {})),
        "refund_total_by_currency": _rounded_map(kind_totals.get("refund", {})),
        "transfer_total_by_currency": _rounded_map(kind_totals.get("transfer", {})),
        "topup_total_by_currency": _rounded_map(kind_totals.get("topup", {})),
        "account_transfer_total_by_currency": _rounded_map(
            kind_totals.get("account_transfer", {})
        ),
        "person_transfer_total_by_currency": _rounded_map(
            kind_totals.get("person_transfer", {})
        ),
        "payment_kind_totals_by_currency": {
            kind: _rounded_map(currencies)
            for kind, currencies in sorted(kind_totals.items())
            if kind in _PAYMENT_KINDS
        },
        "nonpayment_kind_totals_by_currency": {
            kind: _rounded_map(currencies)
            for kind, currencies in sorted(kind_totals.items())
            if kind not in _PAYMENT_KINDS
        },
        "nonpayment_total_by_currency": _rounded_map(nonpayment_totals),
        # ``transactions`` is the legacy expense list and contains payments
        # only, so summing it is safe.  Withdrawals/refunds are exposed
        # separately and ``all_transactions`` keeps the full provenance view.
        "transactions": [
            _record_to_transaction(record) for record in payment_transactions
        ],
        "payment_transactions": [
            _record_to_transaction(record) for record in payment_transactions
        ],
        "non_payment_transactions": [
            _record_to_transaction(record) for record in non_payment_transactions
        ],
        "nonpayment_transactions": [
            _record_to_transaction(record) for record in non_payment_transactions
        ],
        "nonpayment_transaction_count": len(non_payment_transactions),
        "all_transactions": [_record_to_transaction(record) for record in transactions],
        "transaction_count": transaction_count,
        "all_transaction_count": all_transaction_count,
        "payment_transaction_count": payment_count,
        "payment_transactions_count": payment_count,
        "withdrawal_count": withdrawal_count,
        "pending_transactions": [_record_to_pending(record) for record in pending],
        # [2026-10-23 P-50] B 档：收款播报（「支付宝到账十五元」等）。
        # 不进 total_amount，单独列出让用户一键确认；确认后由人工/前端写回确认事件。
        "pending_broadcast": [
            {**_record_to_pending(record), "kind_hint": "broadcast"}
            for record in pending_broadcast
        ],
        "pending_broadcast_count": len(pending_broadcast),
        "pending_broadcast_total_cny": _money_float(
            sum(
                (r.proposed_amount for r in pending_broadcast
                 if r.currency == "CNY" and r.proposed_amount is not None),
                Decimal(0),
            )
        ),
        "pending_count": len(pending),
        "pending_by_reason": dict(sorted(pending_by_reason.items())),
        "pending_by_currency": dict(sorted(pending_by_currency.items())),
        "totals_partial": bool(pending),
        "duplicates_removed": duplicates_removed,
        "duplicates_removed_count": len(duplicates_removed),
        "aggregate_reconciliations": reconciliations,
        "aggregate_reconciliation_conflicts": reconciliation_conflicts,
        "unreconciled_aggregates": unreconciled_aggregates,
        "excluded_events": excluded,
        "excluded_count": len(excluded),
        "unmatched_edits": [],
        "has_records": bool(kept),
    }
