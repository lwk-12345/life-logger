"""`items[].amount` 的语义标注：这个数字到底是钱、是量、还是说不清（Q839 / P0-D3）。

问题
----
活动子项的 `amount` 是一个**裸数字**，同一天里可以是：

    {"amount": 84.35}                          ← 体重（kg）
    {"amount": 500}                            ← 条数
    {"amount": 8}                              ← 车价「8 万」（其实要 ×10000）
    {"amount": 400, "currency": "CNY"}         ← 真实报销

区分全靠 `currency` / `unit` 有没有值，而 `84.35` 三项全空。
前端、导出、任何新消费方读到的都是同一个字段，**"钱"与"不是钱"混在一起**。

做法
----
**只加标注，不改数值**：给每个带 amount 的 item 补一个确定性推导的
``amount_kind ∈ {money, quantity, weight, unknown}``。

为什么不让模型输出这个字段：模型已经输出过 `unit: null`（prompt 里明确要求"不知道就不许猜"），
再让它判断"这是不是钱"只会引入新的幻觉。分类规则完全由 `currency` / `unit` 的字面值决定，
是**可复现、可审计**的纯函数 —— 同一输入永远同一结果。

为什么不动 `amount` 本身：把它收紧成"只允许带 currency 的值"会让 `84.35`、`500`
这些**原话里真实出现过的数字**从产物里消失，属于丢证据；而它们对下游的伤害
是"被当成钱"，不是"存在"。加标注就能拦住误读，同时保留原文。

兜底哲学
--------
``currency`` 与 ``unit`` 全空 → ``unknown``，**不猜**。
（`84.35` 到底是 kg 还是元，只有原话知道；猜错比不猜更糟。）
"""

from __future__ import annotations

from typing import Any, Mapping

#: 四种语义
AMOUNT_KINDS = ("money", "quantity", "weight", "unknown")

#: 能当**货币**用的 unit 字面（注意："块" 是口语元，"角/分" 是辅币）
_MONEY_UNITS = frozenset({
    "元", "元钱", "块钱", "块", "圆", "人民币", "rmb", "cny", "角", "毛", "分",
    "美元", "美金", "usd", "欧元", "eur", "英镑", "gbp", "日元", "jpy",
    "里拉", "try", "港币", "hkd", "韩元", "krw",
})

#: 能当**重量/体积**用的 unit 字面
_WEIGHT_UNITS = frozenset({
    "克", "g", "gram", "千克", "kg", "公斤", "斤", "两", "吨", "t",
    "毫克", "mg", "毫升", "ml", "升", "l", "litre", "liter",
})

#: **倍数/量级**词（"8 万"）：它只说明量级，不说明量纲 —— 8 万可以是 8 万块，也可以是 8 万条。
#: 所以带这种 unit 时判 ``unknown``，**不判 quantity**（判成数量会让人以为"8 条"）。
_SCALE_UNITS = frozenset({"万", "亿", "千", "百", "十", "w", "k"})

#: 明确是**计数/频率/比例**的 unit（其余既非钱也非重量的 unit 也归 quantity）
_QUANTITY_UNITS = frozenset({
    "个", "只", "条", "包", "件", "抽", "次", "份", "碗", "瓶", "盒", "袋", "张",
    "台", "辆", "本", "年", "月", "天", "小时", "分钟", "秒", "人", "步", "楼",
    "点", "percent", "%", "倍", "层", "号", "名字", "潜能点",
})


def _norm(value: Any) -> str:
    return str(value or "").strip().lower()


def classify_amount_kind(item: Mapping[str, Any]) -> str | None:
    """返回 ``money|quantity|weight|unknown``；``amount`` 不是数字时返回 ``None``。

    **只读 item，不写**。判据顺序（先 currency，再 unit —— 因为 currency 是
    "这笔钱"的强声明，unit 更像"数量单位"）：

      1. 有 currency：
         * currency 本身是重量单位（极少数脏数据）→ weight
         * 其它 → money
      2. 无 currency、有 unit：
         * 货币类 unit（含"万元"）→ money
         * 重量类 unit（含"千克/毫升"）→ weight
         * 量级词（万/亿/千/百/十）→ unknown（量纲未知，**不当数量**）
         * 其余（个/抽/次/点…）→ quantity
      3. 两者皆无 → unknown（**不猜**）

    注意 `unit="元"` + `currency="TRY"` 这种：currency 优先 → money（币种可能是里拉，
    金额仍然"是钱"，只是不是人民币 —— 那是另一个维度，交给 currency 字段表达）。
    """
    raw = item.get("amount")
    if raw is None or isinstance(raw, bool):
        return None
    if not isinstance(raw, (int, float)):
        try:
            float(raw)
        except (TypeError, ValueError):
            return None

    currency = _norm(item.get("currency"))
    unit = _norm(item.get("unit"))

    if currency:
        return "weight" if currency in _WEIGHT_UNITS else "money"
    if unit:
        if unit in _MONEY_UNITS or (len(unit) > 1 and unit[-1] in {"元", "块"}):
            return "money"                 # "万元" / "千元" / "万块"
        if unit in _WEIGHT_UNITS or (len(unit) > 1 and unit[-1] in {"克", "斤", "升", "吨"}):
            return "weight"                # "千克" / "公斤" / "毫升"
        if unit in _SCALE_UNITS:
            return "unknown"               # "8 万"：量级已知、量纲未知 → 不猜
        return "quantity"
    return "unknown"


def tag_item(item: Any) -> Any:
    """就地给一个 item 补 ``amount_kind``（没有 amount 时不加字段），返回同一对象。"""
    if not isinstance(item, dict):
        return item
    kind = classify_amount_kind(item)
    if kind is None:
        item.pop("amount_kind", None)
    else:
        item["amount_kind"] = kind
    return item


def tag_payload(payload: Any) -> Any:
    """就地给 payload 里所有 ``activities[].items[]`` / ``unassociated_items[]`` 补标注。

    幂等：重复调用结果相同；不改变任何既有字段的值。
    """
    if not isinstance(payload, Mapping):
        return payload
    for collection in ("activities", "unassociated_items"):
        for activity in payload.get(collection) or []:
            if not isinstance(activity, Mapping):
                continue
            for item in activity.get("items") or []:
                tag_item(item)
    return payload


__all__ = ["AMOUNT_KINDS", "classify_amount_kind", "tag_item", "tag_payload"]
