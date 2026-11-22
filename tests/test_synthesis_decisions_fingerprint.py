"""账本级更正（broadcast-decisions）必须参与日报缓存判据。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from life_logger import daily_cognitive_session as dcs  # noqa: E402

DATE = "2026-10-30"
EV = [{"event_id": "e0", "title": "事件", "time": {"absolute": "2026-10-30T01:00:00+00:00", "date": DATE},
       "categories": ["生活·日常"], "data": {}, "evidence": {"quote": "q"}}]


@pytest.fixture()
def root(tmp_path):
    (tmp_path / "daily-synthesis").mkdir()
    (tmp_path / "broadcast-decisions").mkdir()
    return tmp_path


def _write(root, decisions=None, raw=None):
    p = root / "broadcast-decisions" / f"{DATE}.json"
    p.write_bytes(raw if raw is not None else json.dumps({"decisions": decisions or []}).encode())
    return p


def _cache(root):
    return root / "daily-synthesis" / f"synthesis-{DATE}.json"


def test_digest_sentinels_are_distinct(root):
    assert dcs.decisions_digest(_cache(root), DATE) == dcs.DECISIONS_DIGEST_ABSENT
    _write(root, raw=b"")
    assert dcs.decisions_digest(_cache(root), DATE) == dcs.DECISIONS_DIGEST_INVALID
    _write(root, raw=b'{"decisions": {"x": 1}}')
    assert dcs.decisions_digest(_cache(root), DATE) == dcs.DECISIONS_DIGEST_INVALID
    _write(root, [])
    assert dcs.decisions_digest(_cache(root), DATE) == dcs.DECISIONS_DIGEST_EMPTY
    _write(root, [{"event_id": "a", "action": "accept"}])
    digest = dcs.decisions_digest(_cache(root), DATE)
    assert len(digest) == 64 and digest not in {
        dcs.DECISIONS_DIGEST_ABSENT, dcs.DECISIONS_DIGEST_INVALID, dcs.DECISIONS_DIGEST_EMPTY}


def test_digest_is_content_not_size(root):
    a = _write(root, [{"event_id": "a", "action": "accept"}, {"event_id": "b", "action": "reject"}])
    first, size = dcs.decisions_digest(_cache(root), DATE), a.stat().st_size
    _write(root, [{"event_id": "a", "action": "reject"}, {"event_id": "b", "action": "accept"}])
    assert a.stat().st_size == size
    assert dcs.decisions_digest(_cache(root), DATE) != first


def test_merged_fingerprint_depends_on_both_axes():
    assert dcs.merged_cache_fingerprint("aaa", "absent") != dcs.merged_cache_fingerprint("aaa", "empty")
    assert dcs.merged_cache_fingerprint("aaa", "absent") != dcs.merged_cache_fingerprint("bbb", "absent")
    assert dcs.merged_cache_fingerprint("aaa", "absent") == dcs.merged_cache_fingerprint("aaa", "absent")


def test_generate_stamps_decisions_digest_and_cache_fingerprint(root, monkeypatch):
    monkeypatch.setattr(dcs, "_get_deepseek_settings", lambda: ("http://fake/v1", "k", "m", None, None))
    calls = {"n": 0}

    def fake(url, key, body, *, tier):
        calls["n"] += 1
        return ({"narrative": {"sections": []}, "executive_summary": "ok",
                 "expense_analysis": {"model_reported_total": None}}, [{"tier": tier, "attempt": 1}])

    monkeypatch.setattr(dcs, "_call_model", fake)
    _write(root, [{"event_id": "a", "action": "accept", "amount": 10.0}])
    first = dcs.generate_daily_cognitive_synthesis(DATE, EV, cache_dir=root / "daily-synthesis")
    assert calls["n"] == 1 and first["cache_state"] == "miss"
    saved = json.loads(_cache(root).read_text())
    assert saved[dcs.DECISIONS_DIGEST_FIELD] == dcs.decisions_digest(_cache(root), DATE)
    assert saved[dcs.CACHE_FINGERPRINT_FIELD] == dcs.merged_cache_fingerprint(
        dcs.events_fingerprint(EV), saved[dcs.DECISIONS_DIGEST_FIELD])
    assert dcs.generate_daily_cognitive_synthesis(DATE, EV, cache_dir=root / "daily-synthesis")["cache_state"] == "hit"
    assert calls["n"] == 1
    _write(root, [{"event_id": "a", "action": "reject"}])
    assert dcs.generate_daily_cognitive_synthesis(DATE, EV, cache_dir=root / "daily-synthesis")["cache_state"] == "stale_rebuilt"
    assert calls["n"] == 2


def test_decision_change_releases_pending_cooldown(root, monkeypatch):
    monkeypatch.setattr(dcs, "_get_deepseek_settings", lambda: ("http://fake/v1", "k", "m", None, None))
    calls = {"n": 0}

    def fake(url, key, body, *, tier):
        calls["n"] += 1
        return ({"narrative": {"sections": []}, "executive_summary": "ok",
                 "expense_analysis": {"model_reported_total": None}}, [{"tier": tier, "attempt": 1}])

    monkeypatch.setattr(dcs, "_call_model", fake)
    from life_logger import synthesis_pause as sp
    monkeypatch.setattr(sp, "notify", lambda *a, **k: {"status": "sent"})
    _write(root, [{"event_id": "a", "action": "accept", "amount": 1.0}])
    pending, _ = sp.record_pending(root, DATE, errors=[{"tier": "primary", "error": "x"}], event_count=1), None
    payload = json.loads(Path(pending).read_text())
    payload[dcs.CACHE_FINGERPRINT_FIELD] = dcs.merged_cache_fingerprint(
        dcs.events_fingerprint(EV), dcs.decisions_digest(_cache(root), DATE))
    Path(pending).write_text(json.dumps(payload))
    assert dcs.generate_daily_cognitive_synthesis(DATE, EV, cache_dir=root / "daily-synthesis")["cache_state"] == "pending_cooldown"
    assert calls["n"] == 0
    _write(root, [{"event_id": "a", "action": "reject"}])
    assert dcs.generate_daily_cognitive_synthesis(DATE, EV, cache_dir=root / "daily-synthesis")["cache_state"] in {"miss", "stale_rebuilt"}
    assert calls["n"] == 1
