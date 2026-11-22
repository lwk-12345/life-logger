"""lifecycle 三态（live / media / unknown）贯通回归。

[2026-11-04] 背景：`unknown` 曾被**每一层各自丢掉**，导致「不确定」被静默改写成「是媒体」。
逐层实测过的漏点（本文件的用例逐条守住）：

  1. `feedback_store.LIFECYCLE_VALUES` 只有 ("live","media") → `lifecycle_override()` 把
     resolve() 算出的 unknown 过滤成 None，等于白算；
  2. `lifecycle_weak()` 同一处过滤；
  3. `activity_lifecycle()` 的 ⓪ 分支 `if manual in ("live","media")` 吞掉 unknown；
  4. `activity.get("lifecycle")` 的 declared 分支只认二元；
  5. ⑦ 弱证据分支 `if weak in ("live","media")`；
  6. 前端 `activityBucket()` 把 `0 self` 直接判 media；
  7. 前端兜底 `return {bucket:"media"}`。

本文件只测**判定语义**，不测 UI 像素。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from life_logger import feedback_store as fs  # noqa: E402
from life_logger.activity_timeline_runtime import activity_lifecycle  # noqa: E402


@pytest.fixture()
def store(tmp_path, monkeypatch):
    """把 FEEDBACK_DIR 指到临时目录，并清空模块级缓存（与 test_feedback_store 同构）。"""
    monkeypatch.setattr(fs, "FEEDBACK_DIR", tmp_path / "feedback")
    fs._CACHE.clear()
    yield tmp_path / "feedback"
    fs._CACHE.clear()


def _act(aid: str, *, self_n: int = 0, other_n: int = 1, title: str = "现场聊天",
         target: str = "", lifecycle: str | None = None) -> dict:
    ev = [{"source_label": "self", "quote": "我说的"}] * self_n
    ev += [{"source_label": "non_self", "quote": "别人说的"}] * other_n
    act = {
        "activity_id": aid,
        "title": title,
        "occurrence_start": "2026-11-02T12:00:00+08:00",
        "items": [{"evidence": ev}],
    }
    if target:
        act["speech_target"] = target
    if lifecycle:
        act["lifecycle"] = lifecycle
    return act


def test_unknown_is_a_legal_lifecycle_value() -> None:
    """LIFECYCLE_VALUES 必须包含 unknown，否则所有下游过滤都把它当非法值丢掉。"""
    assert "unknown" in fs.LIFECYCLE_VALUES


def test_zero_self_without_media_evidence_is_unknown_not_media() -> None:
    """0 self 且没有结构媒体证据 → unknown。这是「家人说话被当成视频」的直接修复。"""
    assert activity_lifecycle(_act("a1", self_n=0), "2026-11-02") == "unknown"


def test_zero_self_with_explicit_media_target_is_media() -> None:
    """0 self 但有明确媒体结构证据（speech_target=媒体内容）→ 仍判 media，不能一起变 unknown。"""
    assert activity_lifecycle(_act("a2", self_n=0, target="媒体内容"), "2026-11-02") == "media"


def test_any_self_evidence_wins_live() -> None:
    """只要有一条 self，就是本人的生活，不折叠。"""
    assert activity_lifecycle(_act("a3", self_n=1), "2026-11-02") == "live"


def test_declared_unknown_is_respected() -> None:
    """提取期写入的 declared=unknown 必须原样透出。"""
    assert activity_lifecycle(_act("a4", self_n=0, lifecycle="unknown"), "2026-11-02") == "unknown"


def test_feedback_conflict_resolves_to_unknown(store, monkeypatch) -> None:
    """同一条活动同时有 live 与 media 两条迁移记录 → 必须解析成 unknown，不能静默取最后一条。"""
    store.mkdir(parents=True, exist_ok=True)
    (store / "2026-11-02.jsonl").write_text(
        '{"schema":"feedback-v1","day":"2026-11-02","target":"lifecycle","activity_id":"act_c","after":"live","source":"migrate:x"}\n'
        '{"schema":"feedback-v1","day":"2026-11-02","target":"lifecycle","activity_id":"act_c","after":"media","source":"migrate:y"}\n',
        encoding="utf-8",
    )
    info = fs.resolve("2026-11-02", "lifecycle")["act_c"]
    assert info["conflict"] is True
    assert info["value"] == "unknown"


def test_unknown_survives_lifecycle_override_filter(store) -> None:
    """user_confirmed 的 unknown 不能被 LIFECYCLE_VALUES 过滤成 None（第 1 处漏点）。"""
    store.mkdir(parents=True, exist_ok=True)
    (store / "2026-11-02.jsonl").write_text(
        '{"schema":"feedback-v1","day":"2026-11-02","target":"lifecycle","activity_id":"act_u",'
        '"after":"unknown","provenance":"user_confirmed","basis":"我也分不清"}\n',
        encoding="utf-8",
    )
    assert fs.lifecycle_override("2026-11-02", "act_u") == "unknown"


def test_migrated_guess_cannot_override_self_evidence(store) -> None:
    """迁移来的媒体猜测不得压过 self 证据 —— 这是 act_072 那个反例。"""
    store.mkdir(parents=True, exist_ok=True)
    (store / "2026-11-02.jsonl").write_text(
        '{"schema":"feedback-v1","day":"2026-11-02","target":"lifecycle","activity_id":"act_m",'
        '"after":"media","source":"migrate:activity-buckets","basis":"人工逐条核定"}\n',
        encoding="utf-8",
    )
    # 弱证据可以参与，但不能在 self 证据存在时赢
    assert activity_lifecycle(_act("act_m", self_n=2), "2026-11-02") == "live"
