"""真实日数据回归：11-02 与妈妈聊天（act_145–151）里对方的话不得被判成媒体。

为什么单独一个文件
------------------
`tests/test_interlocutor_not_media.py` 用**构造**的卡片守判定语义。这一份守的是
**真实产物**：直接读 `output/local-web/activity-timeline/jev/2026-11-02.json`，
走一遍 `read_day()` 的读时投影，然后断言：

  1. 用户确认过的真实对话（act_150）里，"对方称…"两项**不是 media**；
  2. 独立媒体证据（non_self 行**全部** far 场）仍然判 media —— 不许把这条也放过去；
  3. 证据不足（只有 unknown）保持 unknown —— 不许自动认成本人；
  4. **磁盘原始文件一个字节都没变**（读时投影绝不写盘）；
  5. 原话逐字保留，不被投影改写。

这五条正是任务书要求的四类覆盖（真实对话 / 独立媒体 / 未知来源 / 原始数据不变）。
真实产物不存在时自动 skip（不把"没数据"伪装成"通过"）。
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from life_logger import feedback_store as fs  # noqa: E402
from life_logger.activity_timeline_runtime import (  # noqa: E402
    ActivityTimelineRuntime,
    _tag_item_lifecycle,
)

DAY = "2026-11-02"
DAY_FILE = ROOT / "output" / "local-web" / "activity-timeline" / "jev" / f"{DAY}.json"
CONFIRMED_CHAT_IDS = tuple(f"act_{i:03d}" for i in range(145, 152))


@pytest.fixture(autouse=True)
def _isolated_feedback(tmp_path, monkeypatch):
    """隔离人工判定库：本机 real feedback 会串进来改变结论。"""
    monkeypatch.setattr(fs, "FEEDBACK_DIR", tmp_path / "feedback")
    fs._CACHE.clear()
    yield
    fs._CACHE.clear()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_day() -> dict:
    rt = ActivityTimelineRuntime(ROOT / "output" / "local-web", ROOT / "local-config.json")
    return rt.read_day(DAY)


def _acts(payload: dict) -> dict[str, dict]:
    return {str(a.get("activity_id")): a for a in payload.get("activities") or [] if isinstance(a, dict)}


def _item_state(item: dict) -> str:
    return str(item.get("item_lifecycle") or "")


# ══════════════════════════════════════════════════════════════════════════
# 1. 用户确认的真实对话：对方发言不得折叠
# ══════════════════════════════════════════════════════════════════════════
def test_confirmed_mom_chat_items_are_not_media():
    """act_145–151 是与妈妈的真实对话；**没有任何一条子项允许被折成「视频台词」**。"""
    if not DAY_FILE.is_file():
        pytest.skip(f"缺少真实产物 {DAY_FILE}")
    acts = _acts(_read_day())
    missing = [aid for aid in CONFIRMED_CHAT_IDS if aid not in acts]
    assert not missing, f"真实产物里缺这些已确认的活动：{missing}"

    offenders = []
    for aid in CONFIRMED_CHAT_IDS:
        for item in acts[aid].get("items") or []:
            if _item_state(item) == "media":
                offenders.append((aid, item.get("item_id"), str(item.get("action"))[:40]))
    assert not offenders, (
        "真实对话里的子项被折成媒体（前端会显示成「旁人/视频台词」）：\n  "
        + "\n  ".join(f"{a} / {i} / {t}" for a, i, t in offenders)
    )


def test_act_150_counterpart_items_keep_their_quotes():
    """`对方称那天减肥饿得胃疼…` 两项：不折 + 原话逐字保留（证据不许被改写）。"""
    if not DAY_FILE.is_file():
        pytest.skip("缺少真实产物")
    act = _acts(_read_day())["act_150"]
    actions = [str(i.get("action")) for i in act["items"]]
    quotes = [str(e.get("quote")) for i in act["items"] for e in (i.get("evidence") or [])]

    target = [i for i in act["items"] if "胃疼" in str(i.get("action"))]
    assert target, f"找不到目标子项，实际子项：{actions}"
    assert _item_state(target[0]) != "media", "对方发言仍被折成媒体"
    assert any("饿的胃疼" in q for q in quotes), "原话被改写或丢失"


# ══════════════════════════════════════════════════════════════════════════
# 2. 独立媒体证据仍然要判 media（否则就是把修复推成"什么都不折"）
# ══════════════════════════════════════════════════════════════════════════
def test_independent_media_evidence_still_folds():
    """non_self 行**全部** far 场 = 屏幕串音/远端音源 → 仍必须 media。"""
    payload = {"date": DAY, "activities": [{
        "activity_id": "act_synth_media",
        "title": "现场说话时电视在放",
        "items": [
            {"item_id": "i1", "action": "本人在现场说话",
             "evidence": [{"source_id": "s1", "quote": "本人在现场说话",
                           "source_label": "self", "acoustic_proximity": "near"}]},
            {"item_id": "i2", "action": "电视里的台词",
             "evidence": [{"source_id": "s2", "quote": "电视里的台词",
                           "source_label": "non_self", "acoustic_proximity": "far"},
                          {"source_id": "s3", "quote": "电视里的台词二",
                           "source_label": "non_self", "acoustic_proximity": "far"}]},
        ],
    }]}
    out = _tag_item_lifecycle(payload)
    items = {i["item_id"]: i for i in out["activities"][0]["items"]}
    assert _item_state(items["i1"]) != "media"
    assert _item_state(items["i2"]) == "media", "全 far 的 non_self 应当仍是 media"


# ══════════════════════════════════════════════════════════════════════════
# 3. 未知来源：既不认成本人，也不折成媒体
# ══════════════════════════════════════════════════════════════════════════
def test_unknown_voiceprint_stays_unknown():
    """没有任何已归属证据的子项 → unknown（不折叠、也不写成本人生活）。"""
    payload = {"date": DAY, "activities": [{
        "activity_id": "act_synth_unknown",
        "title": "现场说话 + 来源不明的条目",
        "items": [
            {"item_id": "i1", "action": "本人说",
             "evidence": [{"source_id": "s1", "quote": "本人说",
                           "source_label": "self", "acoustic_proximity": "near"}]},
            {"item_id": "i2", "action": "不知道是谁说的",
             "evidence": [{"source_id": "s2", "quote": "不知道是谁说的"}]},
            {"item_id": "i3", "action": "只识别出不是本人",
             "evidence": [{"source_id": "s3", "quote": "只识别出不是本人",
                           "source_label": "unknown", "acoustic_proximity": "uncertain"}]},
        ],
    }]}
    out = _tag_item_lifecycle(payload)
    items = {i["item_id"]: i for i in out["activities"][0]["items"]}
    assert _item_state(items["i2"]) == "unknown"
    assert _item_state(items["i3"]) == "unknown"
    assert _item_state(items["i2"]) != "live"


def test_real_day_has_no_unknown_promoted_to_live():
    """真实数据里，unknown 子项一律保持 unknown —— 只有 live/media 才允许是结论。"""
    if not DAY_FILE.is_file():
        pytest.skip("缺少真实产物")
    bad = []
    for aid, act in _acts(_read_day()).items():
        for item in act.get("items") or []:
            evs = item.get("evidence") or []
            labels = {str(e.get("source_label") or "") for e in evs}
            if evs and labels <= {"unknown", ""} and _item_state(item) == "live":
                bad.append((aid, item.get("item_id")))
    assert not bad, f"只有 unknown 证据却被标成 live：{bad[:5]}"


# ══════════════════════════════════════════════════════════════════════════
# 4. 原始数据不变：读时投影绝不写盘
# ══════════════════════════════════════════════════════════════════════════
def test_read_day_does_not_touch_the_day_file():
    """`read_day()` 投影前/后：文件 sha256 与 mtime 必须完全一致。"""
    if not DAY_FILE.is_file():
        pytest.skip("缺少真实产物")
    before_hash = _sha256(DAY_FILE)
    before_mtime = DAY_FILE.stat().st_mtime_ns
    _read_day()
    assert _sha256(DAY_FILE) == before_hash, "读时投影改写了磁盘上的活动数据（红线）"
    assert DAY_FILE.stat().st_mtime_ns == before_mtime


def test_disk_payload_has_no_projection_fields():
    """磁盘上不得出现任何**读时**字段（item_lifecycle / mixed_* / lifecycle）。"""
    if not DAY_FILE.is_file():
        pytest.skip("缺少真实产物")
    raw = json.loads(DAY_FILE.read_text(encoding="utf-8"))
    leaked = []
    for act in raw.get("activities") or []:
        if "item_lifecycle" in act:
            leaked.append((act.get("activity_id"), "item_lifecycle"))
        for key in ("mixed_media_item_count", "mixed_media_strong_count", "mixed_media_item_ids"):
            if key in act:
                leaked.append((act.get("activity_id"), key))
        for item in act.get("items") or []:
            if isinstance(item, dict) and "item_lifecycle" in item:
                leaked.append((act.get("activity_id"), f"item:{item.get('item_id')}"))
    assert not leaked, f"读时字段被写进磁盘：{leaked[:5]}"


def test_item_lifecycle_projection_is_idempotent():
    """投影两次 == 投影一次（幂等），否则反复读会漂移。"""
    if not DAY_FILE.is_file():
        pytest.skip("缺少真实产物")
    once = _read_day()
    twice = _tag_item_lifecycle(once)
    strip = lambda p: [  # noqa: E731 - 只比较判定结果
        (a.get("activity_id"), a.get("lifecycle"),
         [(i.get("item_id"), i.get("item_lifecycle")) for i in a.get("items") or []])
        for a in p.get("activities") or []
    ]
    assert strip(once) == strip(twice)
