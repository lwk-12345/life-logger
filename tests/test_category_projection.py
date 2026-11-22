#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Q850：category 必须由 lifecycle 定大类 —— 回归测试（跑在 patched 副本上）。

    .venv/bin/python -m pytest work/fix-category-lifecycle-20261104/test_category_projection.py -q

守 5 条：
  T1 lifecycle=media   ⇒ category 必须是「浏览·视频」
  T2 lifecycle=unknown ⇒ category 必须是「待确认」（**不许落进任何生活分类**）
  T3 lifecycle=live    ⇒ 保留原有细分（不被改写）
  T4 user_confirmed 的 lifecycle 优先（用 monkeypatch 模拟 ⓪ 档返回）
  T5 金额字段零变化（amount/currency/unit 逐条相同）
外加一个**变异自检**：人为制造违规，断言必须报错（否则测试是假的）。
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
W = ROOT / "work/fix-category-lifecycle-20261104"
sys.path.insert(0, str(ROOT / "src"))

MOD = SourceFileLoader("rt_q850", str(W / "patched/activity_timeline_runtime.py")).load_module()

DAYS = ["2026-10-27", "2026-10-28", "2026-10-29",
        "2026-11-01", "2026-11-02", "2026-11-03", "2026-11-04"]
MEDIA_CAT, PENDING_CAT = "浏览·视频", "待确认"
LIFE_LIKE = {"生活·日常", "餐饮·消费", "运动·健身", "支付·播报", "编程·项目优化", "与助手对话"}


def load(day):
    path = ROOT / f"output/local-web/activity-timeline/jev/{day}.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None


def projected(day) -> dict[str, tuple[str, str]]:
    payload = load(day)
    out = MOD._tag_lifecycle(payload)
    return {str(a.get("activity_id")): (str(a.get("lifecycle")), str(a.get("category") or ""))
            for a in out.get("activities") or []}


def violations(rows) -> list[str]:
    return [f"{aid}: lifecycle={lc} category={cat}"
            for aid, (lc, cat) in rows.items()
            if (lc == "media" and cat in LIFE_LIKE) or (lc == "unknown" and cat in LIFE_LIKE)]


@pytest.mark.parametrize("day", DAYS)
def test_no_contradiction(day):
    rows = projected(day)
    assert rows, f"{day} 没有活动？"
    assert not violations(rows), "\n".join(violations(rows)[:8])


@pytest.mark.parametrize("day", DAYS)
def test_media_and_unknown_use_fixed_labels(day):
    for aid, (lc, cat) in projected(day).items():
        if lc == "media":
            assert cat == MEDIA_CAT, f"{day}/{aid}: media 却没有归到「{MEDIA_CAT}」→ {cat!r}"
        elif lc == "unknown":
            assert cat == PENDING_CAT, f"{day}/{aid}: unknown 却没有标「{PENDING_CAT}」→ {cat!r}"


@pytest.mark.parametrize("day", DAYS)
def test_live_keeps_subcategory(day):
    """live 的细分不许被抹平：除了原本就空/视频档的，其它必须原样保留。"""
    payload = load(day)
    before = {str(a.get("activity_id")): str(a.get("category") or "")
              for a in payload.get("activities") or []}
    for aid, (lc, cat) in projected(day).items():
        if lc != "live":
            continue
        assert cat == (before[aid] or "生活·日常"), f"{day}/{aid}: live 细分被改写 {before[aid]!r}→{cat!r}"


def test_user_confirmed_lifecycle_wins(monkeypatch):
    """⓪ 档给出的结论优先：用户说 media，即使卡片像生活也必须归到浏览·视频。"""
    monkeypatch.setattr(MOD, "_feedback_lifecycle", lambda day, aid: "media")
    payload = {"date": "2026-11-02", "activities": [
        {"activity_id": "act_x", "title": "现场讨论退货", "category": "生活·日常", "items": []}]}
    got = MOD._tag_lifecycle(payload)["activities"][0]
    assert got["lifecycle"] == "media" and got["category"] == MEDIA_CAT
    assert got["category_original"] == "生活·日常", "原文必须留痕"


def test_amount_fields_untouched():
    """T5：amount / currency / unit 逐条相同（只改 category）。"""
    for day in DAYS:
        payload = load(day)
        after = MOD._tag_lifecycle(json.loads(json.dumps(payload)))
        a = Counter(repr((i.get("amount"), i.get("currency"), i.get("unit")))
                    for act in payload["activities"] for i in act.get("items") or [])
        b = Counter(repr((i.get("amount"), i.get("currency"), i.get("unit")))
                    for act in after["activities"] for i in act.get("items") or [])
        assert a == b, f"{day}: 金额字段被改动"


def test_mutation_is_detected():
    """自检：把一张 media 卡的 category 改回「生活·日常」，违规检查必须报错。"""
    rows = projected("2026-11-03")
    media_ids = [aid for aid, (lc, _) in rows.items() if lc == "media"]
    assert media_ids, "这天没有 media 卡，换一天做变异"
    broken = dict(rows)
    broken[media_ids[0]] = ("media", "生活·日常")
    assert violations(broken), "改坏了却检查不出来 → 断言无效"
