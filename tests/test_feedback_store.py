"""标注回灌（数据飞轮）的回归测试：存得对、读得对、能压过后端规则、坏数据不崩。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from life_logger import feedback_store as fs  # noqa: E402


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(fs, "FEEDBACK_DIR", tmp_path / "feedback")
    fs._CACHE.clear()
    yield tmp_path / "feedback"
    fs._CACHE.clear()


def test_record_and_read_roundtrip(store):
    rec = fs.record({"day": "2026-11-03", "target": "lifecycle", "activity_id": "act_001",
                     "after": "media", "basis": "凌晨在放英文赛事解说", "source": "test"})
    assert rec["schema"] == fs.SCHEMA and rec["after"] == "media"
    rows = list(fs.iter_records("2026-11-03"))
    assert len(rows) == 1 and rows[0]["activity_id"] == "act_001"
    assert fs.overrides("2026-11-03") == {"act_001": "media"}


def test_conflicting_records_resolve_to_unknown(store):
    """[Q826] 同一 id 既有 live 又有 media、且都没有用户确认 → 显式 unknown，不再静默取最后一条。"""
    fs.record({"day": "2026-11-03", "target": "lifecycle", "activity_id": "act_009", "after": "media", "basis": "a"})
    fs.record({"day": "2026-11-03", "target": "lifecycle", "activity_id": "act_009", "after": "live", "basis": "b"})
    assert fs.overrides("2026-11-03")["act_009"] == fs.CONFLICT_VALUE
    info = fs.resolve("2026-11-03")["act_009"]
    assert info["conflict"] is True and info["records"] == 2
    assert fs.resolution_stats("2026-11-03")["conflict"] == 1
    assert len(list(fs.iter_records("2026-11-03"))) == 2, "历史必须留痕，不能覆盖"


def test_dedupe_is_idempotent(store):
    a = fs.record({"day": "2026-11-03", "target": "lifecycle", "activity_id": "act_002", "after": "live", "basis": "x"})
    b = fs.record({"day": "2026-11-03", "target": "lifecycle", "activity_id": "act_002", "after": "live", "basis": "x"})
    assert "_skipped" not in a and b.get("_skipped") == "duplicate"
    assert len(list(fs.iter_records("2026-11-03"))) == 1


@pytest.mark.parametrize("bad", [
    {"day": "2026-11-03", "target": "nope", "activity_id": "act_1", "after": "live"},
    {"day": "20261103", "target": "lifecycle", "activity_id": "act_1", "after": "live"},
    {"day": "2026-11-03", "target": "lifecycle", "activity_id": "act_1", "after": "maybe"},
    {"day": "2026-11-03", "target": "lifecycle", "activity_id": "", "after": "live"},
])
def test_invalid_rejected(store, bad):
    with pytest.raises(fs.FeedbackError):
        fs.record(bad)


def test_days_are_partitioned(store):
    fs.record({"day": "2026-11-02", "target": "lifecycle", "activity_id": "act_007", "after": "media", "basis": "a"})
    fs.record({"day": "2026-11-03", "target": "lifecycle", "activity_id": "act_007", "after": "live", "basis": "b"})
    assert fs.overrides("2026-11-02")["act_007"] == "media"
    assert fs.overrides("2026-11-03")["act_007"] == "live"
    assert fs.overrides("2026-11-04") == {}, "没有表的日期不得套用别人"


def test_corrupt_line_is_skipped(store):
    store.mkdir(parents=True, exist_ok=True)
    (store / "2026-11-03.jsonl").write_text('{"schema":"feedback-v1","day":"2026-11-03","target":"lifecycle","activity_id":"act_3","after":"live"}\nNOT JSON\n', encoding="utf-8")
    assert fs.overrides("2026-11-03") == {"act_3": "live"}


def test_lifecycle_override_beats_rules_both_ways(store):
    """**用户确认过**的判定仍然一票否决（只有这一档有这个权力）。"""
    from life_logger.activity_timeline_runtime import activity_lifecycle
    fs.record({"day": "2026-11-03", "target": "lifecycle", "activity_id": "act_a", "after": "media",
               "basis": "x", "provenance": "user_confirmed"})
    fs.record({"day": "2026-11-03", "target": "lifecycle", "activity_id": "act_b", "after": "live",
               "basis": "y", "provenance": "user_confirmed"})
    # declared + 标题都说 live → 人工 media 必须赢
    assert activity_lifecycle({"activity_id": "act_a", "occurrence_start": "2026-11-03T01:00:00+08:00",
                               "title": "现场讨论退货", "lifecycle": "live"}, "2026-11-03") == "media"
    # declared + 标题都说 media → 人工 live 必须赢
    assert activity_lifecycle({"activity_id": "act_b", "occurrence_start": "2026-11-03T01:00:00+08:00",
                               "title": "背景播放演唱会解说", "lifecycle": "media"}, "2026-11-03") == "live"


def test_few_shot_examples_skip_empty_basis(store):
    fs.record({"day": "2026-11-03", "target": "lifecycle", "activity_id": "act_1", "after": "media",
               "basis": "", "provenance": "user_confirmed"})
    fs.record({"day": "2026-11-03", "target": "lifecycle", "activity_id": "act_2", "after": "live",
               "basis": "凌晨你说的是自己唱的一句", "provenance": "user_confirmed"})
    ex = fs.few_shot_examples(limit=5)
    assert [e["activity_id"] for e in ex] == ["act_2"]
    block = fs.render_few_shot_block(ex)
    assert "act_2" in block and "历史人工判定样例" in block


def test_migrate_legacy_dry_run_and_real(tmp_path, store, monkeypatch):
    web = tmp_path / "web"
    web.mkdir()
    (web / "activity-buckets-2026-11-02.js").write_text(
        'window.ACTIVITY_BUCKETS["2026-11-02"] = { live: ["act_001"], media: ["act_007"] };\n', encoding="utf-8")
    (web / "activity-category-override.js").write_text(
        'window.ACTIVITY_CATEGORY_OVERRIDE = {\n  "2026-11-02": {\n    "assistant": ["act_001"]\n  },\n};\n', encoding="utf-8")
    dry = fs.migrate_legacy(tmp_path, dry_run=True)
    assert dry["lifecycle"] == 2 and dry["category"] == 1
    assert fs.days() == [], "dry-run 不得落盘"
    real = fs.migrate_legacy(tmp_path)
    assert real["lifecycle"] == 2 and real["category"] == 1
    assert fs.overrides("2026-11-02") == {"act_001": "live", "act_007": "media"}
    again = fs.migrate_legacy(tmp_path)
    assert again["skipped"] == 3, "重复迁移必须全部跳过"


# ---------------------------------------------------------------------------
# [Q826] provenance 分层：自动推断不得压过声纹证据
# ---------------------------------------------------------------------------
def _labelled(labels: list[str], title: str = "现场讨论退货") -> dict:
    # 注意：**不带** `lifecycle` 字段 —— 真实 payload 里这个键是读时投影才加的，
    # 带上它会先命中 ①，根本走不到声纹门禁（第一版测试就错在这里）。
    return {"activity_id": "act_x", "occurrence_start": "2026-11-03T01:00:00+08:00",
            "title": title,
            "items": [{"evidence": [{"quote": "q", "source_label": l} for l in labels]}]}


def test_missing_provenance_defaults_to_migrated_guess(store):
    """旧行（没有 provenance 字段）读取时一律降级，**不改文件**。"""
    store.mkdir(parents=True, exist_ok=True)
    (store / "2026-11-03.jsonl").write_text(
        '{"schema":"feedback-v1","day":"2026-11-03","target":"lifecycle",'
        '"activity_id":"act_old","after":"media","basis":"旧行"}\n', encoding="utf-8")
    rec = next(iter(fs.iter_records("2026-11-03")))
    assert fs.provenance_of(rec) == "migrated_guess"
    assert fs.lifecycle_override("2026-11-03", "act_old") is None, "迁移猜测不得进 ⓪"


def test_migrated_guess_cannot_override_voiceprint_self(store):
    """核心回归：迁移来的 media 判定，不得压过 5 条 self 证据。"""
    from life_logger.activity_timeline_runtime import activity_lifecycle
    fs.record({"day": "2026-11-03", "target": "lifecycle", "activity_id": "act_x",
               "after": "media", "basis": "迁移来的", "source": "migrate:xxx.js"})
    assert fs.lifecycle_override("2026-11-03", "act_x") is None
    assert activity_lifecycle(_labelled(["self", "self", "non_self"]), "2026-11-03") == "live"


def test_user_confirmed_still_wins_over_voiceprint(store):
    from life_logger.activity_timeline_runtime import activity_lifecycle
    fs.record({"day": "2026-11-03", "target": "lifecycle", "activity_id": "act_x",
               "after": "media", "basis": "用户确认过不是他", "provenance": "user_confirmed"})
    assert activity_lifecycle(_labelled(["self", "self"]), "2026-11-03") == "media"


def test_conflict_does_not_override_evidence(store):
    """冲突（live+media 并存且无人工裁决）不得替证据做决定。"""
    from life_logger.activity_timeline_runtime import activity_lifecycle
    fs.record({"day": "2026-11-03", "target": "lifecycle", "activity_id": "act_x",
               "after": "media", "basis": "a", "source": "migrate:a.js"})
    fs.record({"day": "2026-11-03", "target": "lifecycle", "activity_id": "act_x",
               "after": "live", "basis": "b", "source": "migrate:b.js"})
    assert fs.overrides("2026-11-03")["act_x"] == fs.CONFLICT_VALUE
    assert fs.lifecycle_override("2026-11-03", "act_x") is None
    assert activity_lifecycle(_labelled(["self"]), "2026-11-03") == "live"


def test_weak_guess_is_only_a_tiebreaker(store):
    """迁移猜测只在规则链末尾投票：无证据、无标题线索时才会生效。"""
    from life_logger.activity_timeline_runtime import activity_lifecycle
    fs.record({"day": "2026-11-03", "target": "lifecycle", "activity_id": "act_w",
               "after": "media", "basis": "迁移来的", "source": "migrate:y.js"})
    bare = {"activity_id": "act_w", "occurrence_start": "2026-11-03T01:00:00+08:00", "title": ""}
    assert activity_lifecycle(bare, "2026-11-03") == "media", "空卡才会采纳弱信号"
    # 一旦有强线索（主动通信），弱信号立刻让位
    talk = {**bare, "title": "打电话给物业"}
    assert activity_lifecycle(talk, "2026-11-03") == "live"


def test_few_shot_only_accepts_user_confirmed(store):
    fs.record({"day": "2026-11-03", "target": "lifecycle", "activity_id": "act_m",
               "after": "media", "basis": "迁移来的猜测理由够长了", "source": "migrate:z.js"})
    fs.record({"day": "2026-11-03", "target": "lifecycle", "activity_id": "act_u",
               "after": "live", "basis": "用户明确说是自己唱的", "provenance": "user_confirmed"})
    ex = fs.few_shot_examples(limit=5)
    assert [e["activity_id"] for e in ex] == ["act_u"]


def test_audit_provenance_reports_levels_and_conflicts(store):
    fs.record({"day": "2026-11-03", "target": "lifecycle", "activity_id": "act_a",
               "after": "media", "basis": "a", "source": "migrate:a.js"})
    fs.record({"day": "2026-11-03", "target": "lifecycle", "activity_id": "act_a",
               "after": "live", "basis": "b", "source": "migrate:b.js"})
    fs.record({"day": "2026-11-03", "target": "lifecycle", "activity_id": "act_b",
               "after": "live", "basis": "c", "provenance": "user_confirmed"})
    stats = fs.audit_provenance()
    assert stats["records"] == 3
    assert stats["by_provenance"] == {"migrated_guess": 2, "user_confirmed": 1}
    assert stats["conflict_total"] == 1
