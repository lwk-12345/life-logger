"""给活动子项标事实类型（docs/adr/0005 P1）。

模型在新窗口给的 ``fact_type`` 优先；旧数据用规则兜底。规则偏保守，只用于影子对比，不决定金额入账。
判断不了就返回 ``none``，不猜。媒体内容和计划不算已发生的事实。
"""
from __future__ import annotations

import re
from typing import Any, Mapping

FACT_TYPES = ("expense", "food", "exercise", "smoke", "todo", "idea", "none")

_PLAN_STATUS = re.compile(r"planned|计划|已提出|requested|待办", re.I)
_INTENT = re.compile(r"想买|要买|打算|准备买|等会|明天|后天|下次|多少钱|吗[？?]?$|能不能|要不要|假如|如果")
_NOT_DOING = re.compile(r"询问|提问|要求|告知|讨论|建议|认为|不想|没有|没吃|没练|不吃|不喝|不练|说话人二|回应|应答")
_PAID = re.compile(r"买了|买过|刚买|已经买|购入|购买了|花了|花费|付了|付款|支付|实付|消费了|下了单|下单了|买单|买到")
_SMOKE = re.compile(r"抽烟|抽了|一根烟|戒烟|吸烟")
_EXERCISE = re.compile(r"练了|练完|练腿|练胸|练背|卧推|深蹲|硬拉|跑了|撸铁|做了.{0,4}组|\d+组|[一二三四五六七八九十]组")
_FOOD = re.compile(r"吃了|喝了|早餐吃|午餐吃|晚餐吃|夜宵吃|点了.{0,6}(饭|面|粉|粥|汤)")
_TODO = re.compile(r"提醒|记得|别忘|待办|要去|得去")
_IDEA = re.compile(r"想法|灵感|点子|构思")


def _quotes(item: Mapping[str, Any]) -> str:
    return " ".join(
        str(entry.get("quote") or "")
        for entry in item.get("evidence") or []
        if isinstance(entry, Mapping)
    )


def has_self_evidence(item: Mapping[str, Any]) -> bool:
    return any(
        isinstance(entry, Mapping) and entry.get("source_label") == "self"
        for entry in item.get("evidence") or []
    )


def item_fact_type(item: Mapping[str, Any], activity: Mapping[str, Any] | None = None) -> str:
    activity = activity or {}
    if activity.get("lifecycle") == "media" or item.get("item_lifecycle") == "media":
        return "none"
    declared = str(item.get("fact_type") or "")
    if declared in FACT_TYPES:
        return declared
    action = str(item.get("action") or "")
    text = f"{action} {_quotes(item)}"
    status = str(item.get("status") or "")
    if _PLAN_STATUS.search(status) or _TODO.search(action):
        return "todo" if _TODO.search(text) else "none"
    if _INTENT.search(text):
        return "none"
    if _PAID.search(text) and (item.get("amount") is not None or item.get("currency")):
        return "expense"
    # 饮食、运动、抽烟误报代价高：必须是本人说的动作，分类标签单独不算证据。
    if not has_self_evidence(item) or _NOT_DOING.search(action):
        return "none"
    if _SMOKE.search(text):
        return "smoke"
    if _EXERCISE.search(text):
        return "exercise"
    if _FOOD.search(text):
        return "food"
    if _IDEA.search(action):
        return "idea"
    return "none"
