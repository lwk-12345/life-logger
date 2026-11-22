"""读时标题降级（activity_timeline_runtime.read_day(for_display=True)）回归测试。

新文件（不修改任何既有测试）。覆盖：
  * 真实 10-28 数据：4 条「用户本人…」被降级，改后 0 条确定断言
  * 幂等：连续两次 read 结果完全相同
  * 零写盘：调用前后 jev 文件 sha256 不变
  * 字段保真：除 title/title_original/title_source/title_hedged 外逐键相等
  * 反向：identity_safe.identity == "self" 一律不改
  * 反例：空/None/非字符串标题、缺 identity_safe 但有 source_label、损坏 JSON —— 不抛异常
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import life_logger.activity_timeline_runtime as R  # noqa: E402

JEVDIR = ROOT / "output/local-web/activity-timeline/jev"
ASSERTION = re.compile(r"用户本人|本人(在|说|问|买|看|听|刷)")
# 读时降级**允许**改动的键（2026-10-29 扩到 summary/uncertainties/items.action）
HEDGE_KEYS = {"category", "category_original", "category_projected_by",  # [Q850] 读时投影允许改大类
              "title", "title_original", "title_source", "title_hedged",
              "summary", "summary_original", "uncertainties", "uncertainties_original",
              "items", "lifecycle"}
# `lifecycle`：[2026-11-03] 生活/媒体分档，由 `_tag_lifecycle()` 在 **read_day 投影之后**
# 才打上的纯读时字段（实测真实 jev 文件 0/180、0/91 不带该键）。它和 title_hedged 同类，
# 属于「读时投影」而非「非标题字段被改动」，所以计入本白名单，不影响本用例守住的原意。
CONFIG = ROOT / "output/local-web/config.json"
TARGETS = {
    "act_20261028_rec_10c8cc27e1eb_agent_task_query": "录音中疑似本人询问",
    "act_20261028_rec_039d6e025d01_prompt_optimize": "录音中疑似本人询问",
    "act_20261028_rec_78ca7aabec5d_ui_check_query": "录音中疑似本人询问",
    "act_20261028_rec_19c1942f1477_li_xiaolu_talk": "录音中提及",
}


def runtime():
    return R.ActivityTimelineRuntime(ROOT / "output/local-web", CONFIG)


def _require_real(day: str = "2026-10-28") -> None:
    if not (JEVDIR / f"{day}.json").is_file():
        pytest.skip(f"真实数据缺失：{day}")


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def test_real_20261028_titles_hedged():
    _require_real()
    data = runtime().read_day("2026-10-28")
    by_id = {a.get("activity_id"): a for a in (data.get("activities") or []) if isinstance(a, dict)}
    for aid, prefix in TARGETS.items():
        assert aid in by_id, f"缺少活动 {aid}"
        title = str(by_id[aid].get("title") or "")
        assert title.startswith(prefix), f"{aid} 标题未降级：{title!r}"
        assert "用户本人" not in title
    remaining = [a.get("activity_id") for a in by_id.values()
                 if ASSERTION.search(str(a.get("title") or ""))]
    assert remaining == []


# [2026-10-29 红线补丁] 「无锚定具体行为断言」也会被降级。这里把**已确认**的新增项写死，
# 其余活动仍必须与磁盘逐字节相同（测试强度只增不减：新增项断言了确切期望值）。
UNANCHORED_KNOWN = {
    ("2026-10-26", "act_order_fries_live"): "录音中疑似：现场点一份中份薯条并得到店员复述",
    # [2026-10-30 返工] 「点+汉字宾语」形态补齐（1 号窗 §8 的两条漏网）
    ("2026-10-26", "act_order_lean_meat_noodles_live"): "录音中疑似：现场点瘦肉粉并询问价格，后在店内吃一碗",
    ("2026-10-27", "act_20261027_037"): "录音中疑似：傍晚现场点芝麻酱并称赞饭菜好吃",
    ("2026-10-27", "act_20261027_026"): "录音中疑似：傍晚店内点单与现场用餐交流",
    ("2026-10-29", "act_20261029_071750_zhuzatangfan"): "录音中疑似：购买寿司并点猪杂汤饭",
    ("2026-10-29", "act_20261029_010844_store_smokes_water"): "录音中疑似：在店内现场要一包十元红塔山和一桶三块钱的水",
}


def test_other_days_untouched():
    for day in ("2026-10-26", "2026-10-27", "2026-10-29"):
        _require_real(day)
        raw = json.loads((JEVDIR / f"{day}.json").read_text(encoding="utf-8"))
        raw_by_id = {a.get("activity_id"): a for a in (raw.get("activities") or []) if isinstance(a, dict)}
        data = runtime().read_day(day)
        for a in data.get("activities") or []:
            aid = a.get("activity_id")
            got = str(a.get("title") or "")
            expected = UNANCHORED_KNOWN.get((day, aid))
            if expected is not None:
                assert got == expected, f"{day}/{aid} 无锚定断言未按预期降级：{got!r}"
                continue
            assert got == str((raw_by_id.get(aid) or {}).get("title") or ""), \
                f"{day}/{aid} 被改动（本应不动）"


def test_idempotent_and_no_disk_write():
    _require_real()
    jev = JEVDIR / "2026-10-28.json"
    corr = JEVDIR / "2026-10-28.corrections.json"
    before = {p.name: (_sha(p), p.stat().st_mtime_ns) for p in (jev, corr) if p.is_file()}
    rt = runtime()
    first = rt.read_day("2026-10-28")
    second = rt.read_day("2026-10-28")
    assert json.dumps(first, sort_keys=True, ensure_ascii=False) == \
           json.dumps(second, sort_keys=True, ensure_ascii=False)
    after = {p.name: (_sha(p), p.stat().st_mtime_ns) for p in (jev, corr) if p.is_file()}
    assert before == after, "read_day 不应写盘"


def test_field_fidelity():
    _require_real()
    raw = json.loads((JEVDIR / "2026-10-28.json").read_text(encoding="utf-8"))
    raw_by_id = {a.get("activity_id"): a for a in (raw.get("activities") or []) if isinstance(a, dict)}
    data = runtime().read_day("2026-10-28")
    checked = 0
    for a in data.get("activities") or []:
        src = raw_by_id.get(a.get("activity_id"))
        if not src:
            continue
        checked += 1
        src_rest = {k: v for k, v in src.items() if k not in HEDGE_KEYS}
        out_rest = {k: v for k, v in a.items() if k not in HEDGE_KEYS}
        assert json.dumps(src_rest, sort_keys=True, ensure_ascii=False) == \
               json.dumps(out_rest, sort_keys=True, ensure_ascii=False), \
               f"{a.get('activity_id')} 的非标题字段被改动"
    assert checked >= 30


def _isolated(tmp_path: Path, activities: list) -> dict:
    out = tmp_path / "local-web"
    (out / "activity-timeline" / "jev").mkdir(parents=True)
    payload = {"schema": R.SCHEMA, "date": "2026-10-28", "timezone": "Asia/Shanghai",
               "cadence_minutes": 10, "status": "ready", "activities": activities,
               "unassociated_items": [], "conflicts": [], "window_history": [],
               "coverage": {"through": None, "source_count": 0}, "errors": [], "metadata": {}}
    (out / "activity-timeline" / "jev" / "2026-10-28.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    cfg = tmp_path / "local-config.json"
    cfg.write_text(json.dumps({"activity_timeline": {"enabled": False}}), encoding="utf-8")
    return R.ActivityTimelineRuntime(out, cfg).read_day("2026-10-28")


def _act(aid: str, title, identity, **item_extra) -> dict:
    item = {"item_id": "i1", "evidence": [{"quote": "q"}]}
    if identity is not None:
        item["identity_safe"] = {"identity": identity, "candidate_only": True}
    item.update(item_extra)
    return {"activity_id": aid, "title": title, "items": [item]}


def test_self_identity_never_hedged(tmp_path):
    data = _isolated(tmp_path, [_act("a_self", "用户本人询问明天的安排", "self")])
    a = data["activities"][0]
    assert a["title"] == "用户本人询问明天的安排"
    assert "title_original" not in a and "title_hedged" not in a


def test_unknown_with_source_label_is_hedged(tmp_path):
    data = _isolated(tmp_path, [_act("a_sl", "用户本人询问明天的安排", None, source_label="self")])
    a = data["activities"][0]
    assert a["title"].startswith("录音中疑似本人询问")
    assert a["title_original"] == "用户本人询问明天的安排"


@pytest.mark.parametrize("bad_title", ["", None, 12345, []])
def test_bad_titles_are_safe(tmp_path, bad_title):
    data = _isolated(tmp_path, [_act("a_bad", bad_title, "unknown")])
    a = data["activities"][0]
    assert a["title"] == bad_title


def test_corrupt_json_raises_instead_of_empty(tmp_path):
    """[2026-11-03 行为变更] 损坏文件不再"优雅降级"成空日。

    旧行为：JSON 解析失败 → 返回 status=pending + activities=[]。
    后果：2026-11-03 11:39 的 `/api/activity-timeline` 返回 0 条，而文件其实有 91 条
    （那一刻文件还没生成；但同样的吞异常路径也会把「损坏」伪装成「没有活动」）。
    新契约：文件存在但不可用 → 抛 TimelineFileCorrupt，由 API 层回 500 + 原因。
    """
    out = tmp_path / "local-web"
    (out / "activity-timeline" / "jev").mkdir(parents=True)
    jev = out / "activity-timeline" / "jev" / "2026-10-28.json"
    jev.write_text(
        '{"schema": "activity-timeline-v1", "activities": [', encoding="utf-8")
    cfg = tmp_path / "local-config.json"
    cfg.write_text("{}", encoding="utf-8")
    with pytest.raises(R.TimelineFileCorrupt) as excinfo:
        R.ActivityTimelineRuntime(out, cfg).read_day("2026-10-28")
    assert excinfo.value.reason == "invalid_json"

    # 文件**不存在**仍然是可用的 pending，但必须显式标注 read_state
    missing = R.ActivityTimelineRuntime(out, cfg).read_day("2026-10-29")
    assert missing["status"] == "pending"
    assert missing["activities"] == []
    assert missing["read_state"] == "no_file"
    assert missing["errors"] == []


# ── 写盘护栏（2026-10-29 补）：展示投影绝不允许 publish ──────────────────────
def test_publish_day_rejects_display_projection(tmp_path):
    out = tmp_path / "local-web"
    (out / "activity-timeline" / "jev").mkdir(parents=True)
    payload = {"schema": R.SCHEMA, "date": "2026-10-28", "route": "jev",
               "timezone": "Asia/Shanghai", "cadence_minutes": 10, "status": "ready",
               "activities": [_act("a1", "用户本人询问明天的安排", "unknown")],
               "unassociated_items": [], "conflicts": [], "window_history": [],
               "coverage": {"through": None, "source_count": 0}, "errors": [], "metadata": {}}
    jev = out / "activity-timeline" / "jev" / "2026-10-28.json"
    jev.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    cfg = tmp_path / "local-config.json"
    cfg.write_text("{}", encoding="utf-8")
    rt = R.ActivityTimelineRuntime(out, cfg)
    display = rt.read_day("2026-10-28")
    assert type(display).__name__ == "_DisplayProjection"
    before = jev.read_bytes()
    with pytest.raises(ValueError):
        rt.publish_day("2026-10-28", display)
    assert jev.read_bytes() == before          # 被拒绝，磁盘未动


def test_read_day_for_display_false_is_plain_dict(tmp_path):
    out = tmp_path / "local-web"
    (out / "activity-timeline" / "jev").mkdir(parents=True)
    payload = {"schema": R.SCHEMA, "date": "2026-10-28", "route": "jev",
               "timezone": "Asia/Shanghai", "cadence_minutes": 10, "status": "ready",
               "activities": [_act("a1", "用户本人询问明天的安排", "unknown")],
               "unassociated_items": [], "conflicts": [], "window_history": [],
               "coverage": {"through": None, "source_count": 0}, "errors": [], "metadata": {}}
    (out / "activity-timeline" / "jev" / "2026-10-28.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    cfg = tmp_path / "local-config.json"
    cfg.write_text("{}", encoding="utf-8")
    rt = R.ActivityTimelineRuntime(out, cfg)
    raw = rt.read_day("2026-10-28", for_display=False)
    assert type(raw) is dict                    # 写路径拿到的必须是普通 dict
    assert raw["activities"][0]["title"] == "用户本人询问明天的安排"   # 原始标题（不降级）


# ── 2026-10-29 扩展：summary / uncertainties / items[].action 也降级，evidence 不动 ──
def _full_activity(identity="unknown"):
    return {
        "activity_id": "a_full",
        "title": "用户本人询问明天的安排",
        "summary": "现场录音里用户本人询问了明天的安排，语气自然。",
        "uncertainties": ["与 rec_x 同为用户本人询问且时间接近，暂独立保留", "已带疑似语气的一条"],
        "items": [{
            "item_id": "i1",
            "identity_safe": {"identity": identity, "candidate_only": True},
            "action": "用户本人询问明天的安排",
            "evidence": [{"quote": "用户本人询问明天的安排", "segment_id": "s", "row_id": 1}],
        }],
    }


def test_text_fields_hedged_and_originals_kept(tmp_path):
    act = _full_activity()
    got = _isolated(tmp_path, [act])["activities"][0]
    assert got["title"].startswith("录音中疑似本人询问")
    assert got["summary"].startswith("现场录音里疑似本人询问")
    assert got["summary_original"] == act["summary"]
    assert got["uncertainties"][0].startswith("与 rec_x 同为疑似本人询问")
    assert got["uncertainties"][1] == "已带疑似语气的一条"          # 已 hedge 的不叠加
    assert got["uncertainties_original"] == act["uncertainties"]
    assert got["items"][0]["action"] == "疑似本人询问明天的安排"
    assert got["items"][0]["action_original"] == act["items"][0]["action"]
    # 证据原文一个字节都不许动
    assert got["items"][0]["evidence"][0]["quote"] == "用户本人询问明天的安排"


def test_text_fields_untouched_when_self(tmp_path):
    act = _full_activity(identity="self")
    got = _isolated(tmp_path, [act])["activities"][0]
    assert got["title"] == act["title"]
    assert got["summary"] == act["summary"]
    assert got["uncertainties"] == act["uncertainties"]
    assert got["items"][0]["action"] == act["items"][0]["action"]
    assert "summary_original" not in got and "uncertainties_original" not in got
    assert "action_original" not in got["items"][0]
