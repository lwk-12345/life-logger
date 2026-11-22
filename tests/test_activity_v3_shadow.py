"""Activity v3 is opt-in; every network call and output directory is a sandbox."""
from __future__ import annotations

import copy
import importlib.util
import io
import json
import urllib.error
from pathlib import Path

import pytest

from life_logger.activity_timeline_runtime import ActivityTimelineRuntime, parse_aware_dt

ROOT = Path(__file__).resolve().parents[1]
CUTOFF = parse_aware_dt("2026-11-05T13:00:00+08:00")
LOOKBACK = parse_aware_dt("2026-11-05T12:50:00+08:00")
RECORD = {
    "source_id": "source-a", "segment_id": "segment-a", "row_id": "row-a",
    "timestamp": "2026-11-05T12:55:00+08:00", "source_label": "self",
    "text": "买了东西花了十六元", "speaker": "speaker-a",
}


def v3_body():
    return {
        "schema_version": 3,
        "activities": [{
            "activity_id": "v3-a", "title": "购买物品", "summary": "记录消费",
            "status": "completed", "lifecycle": "live", "category": "消费·购物",
            "start_at": None, "end_at": None,
            "items": [{
                "item_id": "v3-i", "action": "购买物品", "fact_type": "expense",
                "status": "completed", "spoken_at": None, "occurred_at": None,
                "occurred_date": None, "correction_of": None, "restatement_of": None,
                "confidence": 0.8, "reason": "有原话", "evidence": [{
                    "source_id": "source-a", "quote": RECORD["text"],
                }],
                "expense": {"amount": 16, "currency": "CNY", "item": "物品", "payment_status": "unknown"},
                "food": None, "exercise": None, "smoke": None, "todo": None,
            }],
        }],
    }


def response(body):
    """Serialize canonical fixture facts through the v3.2 model wire contract."""
    wire = copy.deepcopy(body)
    for activity in wire.get("activities", []):
        for item in activity.get("items", []):
            item["evidence"] = [{"line_id": "L001" if e.get("source_id") == RECORD["source_id"] else "L999"}
                                for e in item.get("evidence", [])]
    return {"choices": [{"message": {"content": json.dumps(wire, ensure_ascii=False)}}],
            "usage": {"prompt_tokens": 50, "completion_tokens": 25, "total_tokens": 75}}


def v2_body():
    return {"activities": [{
        "activity_id": "v2-a", "title": "购买物品", "summary": "原 v2 结果",
        "status": "completed", "items": [{
            "item_id": "v2-i", "action": "购买物品", "status": "completed",
            "spoken_at": RECORD["timestamp"], "occurred_at": None,
            "evidence": [{key: RECORD[key] for key in ("source_id", "segment_id", "row_id")} | {"quote": RECORD["text"]}],
        }],
    }]}


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("LL_MODEL_USAGE_DIR", str(tmp_path / "usage"))
    def make(options=None, sender=None):
        config = tmp_path / "local-config.json"
        data = {"deepseek": {"api_key": "sandbox-key", "base_url": "https://model.invalid", "model": "fake-extractor"}}
        if options is not None:
            data["activity_prompt"] = options
        config.write_text(json.dumps(data))
        return ActivityTimelineRuntime(tmp_path / "output", config, v3_extractor=sender)
    return make


def run(runtime, extractor=None):
    return runtime._process_window_model(CUTOFF, LOOKBACK, [copy.deepcopy(RECORD)],
                                         {"activities": []}, extractor or (lambda _: v2_body()))


@pytest.mark.parametrize("options,expected", [
    (None, "v2"), ({"version": "v3", "mode": "off"}, "v2"),
    ({"version": "v2", "mode": "shadow"}, "v2"),
    ({"version": "v2", "mode": "live"}, "v2"),
    ({"version": "v3", "mode": "shadow"}, "both"),
    ({"version": "v3", "mode": "live", "confidence_threshold": 0.0}, "v3"),
    ({"version": "v3", "mode": "bogus"}, "v2"),
    ({"version": "bogus", "mode": "live"}, "v2"),
])
def test_mode_selection(setup, options, expected):
    calls = []
    def v2(prompt):
        calls.append("v2")
        return v2_body()
    def v3(payload):
        calls.append("v3")
        assert payload["response_format"]["type"] == "json_schema"
        assert payload["response_format"]["json_schema"]["schema"]["properties"]["schema_version"]["enum"] == [3]
        return response(v3_body())
    runtime = setup(options, v3)
    result = run(runtime, v2)
    assert calls == {"v2": ["v2"], "v3": ["v3"], "both": ["v2", "v3"]}[expected]
    assert result["activities"][0]["activity_id"] == ("v3-a" if expected == "v3" else "v2-a")
    if expected == "v2":
        assert not (runtime.output_root / "activity-shadow-v3").exists()


def test_shadow_published_v2_bytes_are_identical_and_reads_ignore_archive(setup):
    shadow_body = v3_body()
    shadow_body["activities"][0]["items"][0]["expense"]["amount"] = 99
    runtime = setup({"version": "v3", "mode": "off"}, lambda _: response(shadow_body))
    before_inputs = copy.deepcopy(RECORD)
    v2_result = run(runtime)
    path = runtime.publish_day("2026-11-05", copy.deepcopy(v2_result))
    original = path.read_bytes()
    runtime.config_path.write_text(json.dumps({"activity_prompt": {"version": "v3", "mode": "shadow"}}))
    shadow_result = run(runtime)
    assert json.dumps(v2_result, ensure_ascii=False, sort_keys=True).encode() == json.dumps(shadow_result, ensure_ascii=False, sort_keys=True).encode()
    runtime.publish_day("2026-11-05", shadow_result)
    assert path.read_bytes() == original
    assert RECORD == before_inputs
    archives = list((runtime.output_root / "activity-shadow-v3" / "2026-11-05").glob("*.json"))
    assert len(archives) == 1
    archived = json.loads(archives[0].read_text())["result"]["activities"][0]
    assert archived["activity_id"] == "v3-a"
    assert archived["items"][0]["expense"]["amount"] == 99
    published = runtime.read_day("2026-11-05", for_display=False)
    assert [a["activity_id"] for a in published["activities"]] == ["v2-a"]
    from life_logger.expense_policy import build_expense_ledger
    before_ledger = build_expense_ledger([], activities=json.loads(original)["activities"])
    after_ledger = build_expense_ledger([], activities=published["activities"])
    assert before_ledger["total_amount"] == 16
    assert after_ledger == before_ledger
    assert after_ledger["total_amount"] == 16


def test_schema_capability_rejection_falls_back_and_records_both_attempts(setup, tmp_path):
    formats = []
    def sender(payload):
        formats.append(payload["response_format"]["type"])
        if len(formats) == 1:
            raise urllib.error.HTTPError("https://model.invalid", 400, "bad request", {},
                                         io.BytesIO(b'{"error":"json_schema response_format not supported"}'))
        return response(v3_body())
    runtime = setup({"version": "v3", "mode": "shadow"}, sender)
    assert run(runtime)["activities"][0]["activity_id"] == "v2-a"
    assert formats == ["json_schema", "json_object"]
    archive = json.loads(next((runtime.output_root / "activity-shadow-v3").rglob("*.json")).read_text())
    assert archive["schema_fallbacks"] == 1
    assert archive["status"] == "valid"
    usage = [json.loads(line) for p in (tmp_path / "usage").glob("*.jsonl") for line in p.read_text().splitlines()]
    assert len(usage) == 2
    assert all(row["step"] == "activity_v3_shadow" for row in usage)
    assert usage[0]["ok"] is False and usage[0]["schema_fallback"] is True
    assert usage[1]["total_tokens"] == 75
    assert all("sandbox-key" not in json.dumps(row) and "messages" not in row for row in usage)


@pytest.mark.parametrize("status", [401, 429, 500])
def test_unrelated_http_error_does_not_retry_schema(setup, status):
    calls = []
    def sender(payload):
        calls.append(payload)
        raise urllib.error.HTTPError("https://model.invalid", status, "error", {}, io.BytesIO(b'json_schema'))
    runtime = setup({"version": "v3", "mode": "shadow"}, sender)
    assert run(runtime)["activities"][0]["activity_id"] == "v2-a"
    assert len(calls) == 1


def test_invalid_schema_items_and_reasons_are_preserved(setup):
    invalid = v3_body()
    invalid["activities"][0]["items"][0]["expense"]["amount"] = "十六元"
    runtime = setup({"version": "v3", "mode": "shadow"}, lambda _: response(invalid))
    assert run(runtime)["activities"][0]["activity_id"] == "v2-a"
    stored = json.loads(next((runtime.output_root / "activity-shadow-v3").rglob("*.json")).read_text())
    assert stored["status"] == "invalid"
    assert stored["result"] == invalid
    assert stored["attempts"][0]["body"]["activities"][0]["items"][0]["expense"] == invalid["activities"][0]["items"][0]["expense"]
    assert stored["attempts"][0]["body"]["activities"][0]["items"][0]["evidence"] == [{"line_id": "L001"}]
    assert any("amount" in json.dumps(error) for error in stored["validation"]["errors"])
    runtime.config_path.write_text(json.dumps({"activity_prompt": {"version": "v3", "mode": "live", "confidence_threshold": 0.0}}))
    with pytest.raises(ValueError, match="not valid"):
        run(runtime)


def test_shadow_network_failure_cannot_change_v2_or_leak_exception_key(setup):
    def failing(payload):
        raise RuntimeError("sandbox-key Bearer private-credential")
    runtime = setup({"version": "v3", "mode": "shadow"}, failing)
    assert run(runtime)["activities"][0]["activity_id"] == "v2-a"
    data = next((runtime.output_root / "activity-shadow-v3").rglob("*.json")).read_text()
    assert "sandbox-key" not in data and "private-credential" not in data
    assert json.loads(data)["attempts"][0]["error"]["kind"] == "RuntimeError"


def test_live_hydrates_sources_and_keeps_typed_expense_without_inventing_time(setup):
    body = v3_body()
    body["activities"][0]["items"][0]["spoken_at"] = "2099-01-01T00:00:00+08:00"
    runtime = setup({"version": "v3", "mode": "live", "confidence_threshold": 0.0}, lambda _: response(body))
    result = run(runtime, lambda _: pytest.fail("live must not call v2"))
    item = result["activities"][0]["items"][0]
    assert item["spoken_at"] == RECORD["timestamp"]
    assert item["occurred_at"] is None
    assert item["expense"]["amount"] == item["amount"] == 16
    assert item["payment_status"] == "unknown"
    assert item["evidence"][0]["segment_id"] == RECORD["segment_id"]
    assert item["evidence"][0]["row_id"] == RECORD["row_id"]
    assert result["activities"][0]["fact_schema_version"] == 3


def test_legacy_archive_does_not_publish_hallucinated_quote(setup):
    from life_logger import activity_prompt_v3
    body = v3_body()
    body["activities"][0]["items"][0]["evidence"][0]["quote"] = "不存在的原话"
    runtime = setup({"version": "v3", "mode": "live", "confidence_threshold": 0.0})
    report = {"status": "valid", "result": body}
    with pytest.raises(ValueError, match="quote mismatch"):
        runtime._verify_and_sanitize_v2_response(
            activity_prompt_v3.project_live(report, [RECORD]), [RECORD], CUTOFF, {"activities": []},
        )
    assert not (runtime.output_root / "activity-timeline").exists()


def test_profile_from_configuration_and_original_template(setup):
    seen = []
    def sender(payload):
        seen.append(payload["messages"][0]["content"])
        return response(v3_body())
    runtime = setup({"version": "v3", "mode": "shadow", "user_profile": "测试背景：从配置注入"}, sender)
    run(runtime)
    assert "测试背景：从配置注入" in seen[0]
    assert "{user_profile}" not in seen[0]
    assert (ROOT / "config/activity-prompt-v3.md").read_bytes() == (ROOT / "work/prompt-v3-ab-20261106/prompt_v3_template.md").read_bytes()


def test_real_transport_uses_schema_and_configured_profile_file(setup, tmp_path, monkeypatch):
    from life_logger import activity_prompt_v3

    profile = tmp_path / "profile.md"
    profile.write_text("配置文件里的背景")
    seen = []
    def opener(request, timeout):
        seen.append(json.loads(request.data))
        assert request.get_method() == "POST"
        assert timeout > 0
        return io.BytesIO(json.dumps(response(v3_body())).encode())
    monkeypatch.setattr(activity_prompt_v3.urllib.request, "urlopen", opener)
    runtime = setup({"version": "v3", "mode": "shadow", "user_profile_path": str(profile)})
    assert run(runtime)["activities"][0]["activity_id"] == "v2-a"
    assert len(seen) == 1
    assert seen[0]["response_format"]["type"] == "json_schema"
    assert "配置文件里的背景" in seen[0]["messages"][0]["content"]


def test_shadow_archive_failure_keeps_v2(setup, monkeypatch):
    runtime = setup({"version": "v3", "mode": "shadow"}, lambda _: response(v3_body()))
    def fail(*args, **kwargs):
        raise OSError("sandbox disk failure")
    monkeypatch.setattr("life_logger.activity_prompt_v3.write_json_atomic", fail)
    assert run(runtime)["activities"][0]["activity_id"] == "v2-a"


@pytest.mark.parametrize("failure", ["transport", "evidence"])
def test_shadow_runs_when_v2_fails_and_preserves_primary_error(setup, failure):
    calls = []
    def sender(payload):
        calls.append(payload)
        return response(v3_body())
    def primary(prompt):
        if failure == "transport":
            raise RuntimeError("primary failure")
        body = v2_body()
        body["activities"][0]["items"][0]["evidence"][0]["quote"] = "不存在的引用"
        return body
    runtime = setup({"version": "v3", "mode": "shadow"}, sender)
    with pytest.raises((RuntimeError, ValueError), match="primary failure|quote mismatch"):
        run(runtime, primary)
    assert len(calls) == 1
    stored = json.loads(next((runtime.output_root / "activity-shadow-v3").rglob("*.json")).read_text())
    assert stored["status"] == "valid"
    assert not (runtime.output_root / "activity-timeline").exists()


def test_scorer_uses_all_evidence_and_recovers_baseline_sixteen():
    path = ROOT / "work/prompt-v3-ab-20261106/score_ab.py"
    spec = importlib.util.spec_from_file_location("score_all_evidence", path)
    scorer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(scorer)
    assert scorer.quote_of({"evidence": [None, {"quote": "第一句"}, {"quote": "第二句"}]}) == "第一句 第二句"
    raw = json.loads((path.parent / "raw-1793910113.json").read_text())
    row = next(r for r in scorer.score_a(raw, "v3") if r["label"] == "买菜")
    assert row["found"] is True and row["amount"] == 16
