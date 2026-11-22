"""Unit and integration tests for natural language correction and hotword elevation."""

from __future__ import annotations

import json
import threading
import urllib.request
from pathlib import Path
from urllib.error import HTTPError

from life_logger.intent_pool import ActiveIntentPool
from life_logger.local_llm_extract import _deepseek_amount_is_grounded
from life_logger.local_web import JobStore, create_server
from life_logger.natural_correction import (
    DeepSeekTriIntentParser,
    _eval_hotword,
    classify_correction_action,
    extract_correction_fields,
    parse_spoken_amount,
    parse_spoken_time,
    process_natural_correction,
)


def test_hotword_detection_variants() -> None:
    """Test hotword detection with all canonical hotwords."""
    # 记一笔
    is_hw, hw, clean = _eval_hotword("记一笔买咖啡15元")
    assert is_hw is True
    assert hw == "记一笔"
    assert clean == "买咖啡15元"

    # 记录一下
    is_hw, hw, clean = _eval_hotword("记录一下今天打车花了28元")
    assert is_hw is True
    assert hw == "记录一下"
    assert "28元" in clean

    # 记账
    is_hw, hw, clean = _eval_hotword("记账超市买生活用品80块")
    assert is_hw is True
    assert hw == "记账"
    assert "80块" in clean

    # 记个待办
    is_hw, hw, clean = _eval_hotword("记个待办明天下午三点提交周报")
    assert is_hw is True
    assert hw == "记个待办"
    assert "周报" in clean

    # Standard speech without hotwords
    is_hw, hw, clean = _eval_hotword("今天下午天气挺好的")
    assert is_hw is False
    assert hw is None
    assert clean == "今天下午天气挺好的"

    # Hotword embedded inside sentence
    is_hw, hw, clean = _eval_hotword("今天下午记一笔买水4.9元")
    assert is_hw is True
    assert hw == "记一笔"
    assert "买水4.9元" in clean


def test_classify_action() -> None:
    """Test intent classification into ADD, UPDATE, and DELETE."""
    # ADD
    assert classify_correction_action("今天下午我还花了4.9元买了一瓶水") == "ADD"
    assert classify_correction_action("记一笔买烟9元") == "ADD"
    assert classify_correction_action("补充一下中午吃了炸鸡") == "ADD"
    assert classify_correction_action("记个待办明天早上九点开会") == "ADD"

    # UPDATE
    assert classify_correction_action("刚刚那笔超市不是140是150") == "UPDATE"
    assert classify_correction_action("把刚才炸鸡改成18元") == "UPDATE"
    assert classify_correction_action("修改为20元") == "UPDATE"
    assert classify_correction_action("更正为30块") == "UPDATE"
    assert classify_correction_action("金额应该是25元") == "UPDATE"

    # DELETE
    assert classify_correction_action("删除刚才买烟的记录") == "DELETE"
    assert classify_correction_action("刚才记错了，取消这笔") == "DELETE"
    assert classify_correction_action("别记这笔消费") == "DELETE"
    assert classify_correction_action("撤销刚才的记录") == "DELETE"
    assert classify_correction_action("删掉这一条") == "DELETE"


def test_parse_spoken_amount() -> None:
    """Test parsing amounts from text with grounding check."""
    assert parse_spoken_amount("今天下午我还花了4.9元买了一瓶水") == 4.90
    assert parse_spoken_amount("花了140元在超市") == 140.0
    assert parse_spoken_amount("一百四买的超市") == 140.0
    assert parse_spoken_amount("九块钱买了一包烟") == 9.0
    assert parse_spoken_amount("总共十六块钱的炸鸡") == 16.0
    assert parse_spoken_amount("超市买生活用品80块") == 80.0
    assert parse_spoken_amount("去散步没有花钱") is None


def test_parse_spoken_amount_update_patterns() -> None:
    """Test parsing updated amounts from update sentences."""
    assert parse_spoken_amount("刚刚那笔超市不是140是150") == 150.0
    assert parse_spoken_amount("把刚才炸鸡改成18元") == 18.0
    assert parse_spoken_amount("修改为20元") == 20.0
    assert parse_spoken_amount("更正为35块") == 35.0


def test_parse_spoken_time() -> None:
    """Test extracting clock times and relative offsets."""
    abs_t, rel_s, prec = parse_spoken_time("今天下午三点喝咖啡", base_date="2026-10-02")
    assert abs_t == "2026-10-02T15:00:00+08:00"
    assert prec == "exact"
    assert rel_s is None

    abs_t, rel_s, prec = parse_spoken_time("早上9点开会", base_date="2026-10-02")
    assert abs_t == "2026-10-02T09:00:00+08:00"
    assert prec == "exact"

    abs_t, rel_s, prec = parse_spoken_time("18:17买烟", base_date="2026-10-02")
    assert abs_t == "2026-10-02T18:17:00+08:00"
    assert prec == "exact"

    abs_t, rel_s, prec = parse_spoken_time("5分钟前买了一瓶水")
    assert rel_s == 300.0
    assert prec == "relative"

    abs_t, rel_s, prec = parse_spoken_time("刚才去超市")
    assert rel_s == 60.0
    assert prec == "relative"

    abs_t, rel_s, prec = parse_spoken_time("昨天去散步")
    assert "12:00:00" in (abs_t or "")
    assert prec == "inferred"


def test_extract_correction_fields() -> None:
    """Test structured field extraction from natural language."""
    fields = extract_correction_fields("记一笔买咖啡15元")
    assert fields["action"] == "ADD"
    assert fields["category"] == "expense"
    assert fields["amount"] == 15.0
    assert fields["data"]["expense"]["amount"] == 15.0
    assert "咖啡" in fields["title"]

    todo_fields = extract_correction_fields("记个待办明天下午三点提交周报")
    assert todo_fields["action"] == "ADD"
    assert todo_fields["category"] == "todo"
    assert todo_fields["activity_type"] == "action"

    update_fields = extract_correction_fields("刚刚那笔超市不是140是150")
    assert update_fields["action"] == "UPDATE"
    assert update_fields["amount"] == 150.0
    assert update_fields["old_amount"] == 140.0


def test_strict_amount_grounding_rejects_hallucination() -> None:
    """Test that amounts not grounded in utterance are strictly rejected."""
    assert _deepseek_amount_is_grounded("花了4.9元买水", 4.90) is True
    assert _deepseek_amount_is_grounded("一百四买的超市", 140.0) is True
    # Hallucinated amount not matching tokens in text
    assert _deepseek_amount_is_grounded("花了4.9元买水", 50.0) is False
    assert _deepseek_amount_is_grounded("一百四买的超市", 99.0) is False
    assert _deepseek_amount_is_grounded("去散步没有花钱", 10.0) is False


def test_deepseek_tri_intent_parser_with_mock_opener() -> None:
    """Test DeepSeekTriIntentParser with mock opener and amount grounding filter."""
    mock_payload = {
        "choices": [
            {
                "message": {
                    "content": json.dumps(
                        {
                            "action": "ADD",
                            "category": "expense",
                            "activity_type": "purchase",
                            "title": "咖啡",
                            "target_entity": "咖啡",
                            "amount": 15.0,
                            "event_state": "completed",
                            "claim_type": "event",
                            "amount_evidence": "15元",
                            "transaction_evidence": "买咖啡15元",
                            "relative_seconds": None,
                            "absolute_time": None,
                            "time_precision": "unknown",
                        }
                    )
                }
            }
        ]
    }

    class MockResponse:
        def __init__(self, data: dict) -> None:
            self._data = json.dumps(data).encode("utf-8")

        def read(self) -> bytes:
            return self._data

        def __enter__(self) -> MockResponse:
            return self

        def __exit__(self, *args: object) -> None:
            pass

    def mock_opener(req: urllib.request.Request, **kwargs: object) -> MockResponse:
        return MockResponse(mock_payload)

    parser = DeepSeekTriIntentParser(
        api_key="sk-test-mock-key",
        opener=mock_opener,
    )
    result = parser.parse("记一笔买咖啡15元")
    assert result["action"] == "ADD"
    assert result["amount"] == 15.0
    assert result["category"] == "expense"

    # Test that hallucinated amount in model output is rejected by grounding filter
    mock_payload_hallucinated = {
        "choices": [
            {
                "message": {
                    "content": json.dumps(
                        {
                            "action": "ADD",
                            "category": "expense",
                            "amount": 999.0,  # Hallucinated
                            "target_entity": "咖啡",
                            "title": "咖啡",
                        }
                    )
                }
            }
        ]
    }

    def mock_opener_hallucinated(
        req: urllib.request.Request, **kwargs: object
    ) -> MockResponse:
        return MockResponse(mock_payload_hallucinated)

    parser_hallucinated = DeepSeekTriIntentParser(
        api_key="sk-test-mock-key",
        opener=mock_opener_hallucinated,
    )
    # The utterance says 15元, model returned 999.0 without the required
    # evidence.  A rejected L2 answer must NOT be re-derived by the spoken-money
    # parser; keep it for review with the model's proposed value.
    rejected = parser_hallucinated.parse("记一笔买咖啡15元")
    assert rejected["amount"] is None
    assert rejected["data"] == {}
    assert rejected["proposed_amount"] == 999.0
    assert rejected["amount_review_reason"] == "transaction_state_missing"
    assert rejected["expense_contract"]["status"] == "pending"


def test_deepseek_model_evidence_accepts_normalized_amount_without_grammar_reparse(
    monkeypatch,
) -> None:
    """The real Chinese 38.8 utterance survives with grammar forced to raise.

    The old acceptance path re-parsed spoken money and rejected model answers
    the grammar did not understand.  A raising guard proves the model path
    neither accepts nor falls back through the monetary grammar.
    """

    def raising_grammar(*_args: object, **_kwargs: object) -> bool:
        raise AssertionError("L2 correction path must not call spoken-money grammar")

    monkeypatch.setattr(
        "life_logger.natural_correction._deepseek_amount_is_grounded",
        raising_grammar,
    )
    monkeypatch.setattr(
        "life_logger.local_llm_extract._deepseek_amount_is_grounded",
        raising_grammar,
    )

    payload = {
        "choices": [
            {
                "message": {
                    "content": json.dumps(
                        {
                            "action": "ADD",
                            "category": "expense",
                            "activity_type": "purchase",
                            "title": "黑椒鸡排",
                            "target_entity": "黑椒鸡排",
                            "amount": 38.8,
                            "event_state": "completed",
                            "claim_type": "event",
                            "amount_evidence": "三十八点八",
                            "transaction_evidence": "买了四斤半成品的黑椒鸡排",
                        },
                        ensure_ascii=False,
                    )
                }
            }
        ]
    }

    class MockResponse:
        def read(self) -> bytes:
            return json.dumps(payload, ensure_ascii=False).encode("utf-8")

        def __enter__(self) -> "MockResponse":
            return self

        def __exit__(self, *args: object) -> None:
            pass

    parser = DeepSeekTriIntentParser(
        api_key="sk-test-mock-key",
        opener=lambda req, **kwargs: MockResponse(),
    )
    result = parser.parse("然后又买了四斤半成品的黑椒鸡排，是三十八点八。")

    assert result["amount"] == 38.8
    assert result["data"]["expense"]["amount"] == 38.8
    assert result["data"]["expense"]["amount_evidence"] == "三十八点八"
    assert result["data"]["expense"]["transaction_evidence"] == "买了四斤半成品的黑椒鸡排"
    assert result["amount_review_reason"] is None


def test_rejected_model_amount_is_not_published_as_amountless_correction(
    tmp_path: Path,
) -> None:
    """A failed L2 money answer returns review metadata, never a regex fallback."""

    class RejectingParser:
        def parse(self, text: str, *, base_date: str | None = None) -> dict:
            return {
                "action": "UPDATE",
                "title": "咖啡",
                "category": "expense",
                "activity_type": "purchase",
                "amount": None,
                "proposed_amount": 999.0,
                "amount_review_reason": "transaction_state_missing",
                "expense_contract": {"status": "pending"},
                "old_amount": None,
                "target_entity": "咖啡",
                "relative_seconds": None,
                "absolute_time": None,
                "time_precision": "unknown",
                "data": {},
            }

    store = JobStore(tmp_path / "web", recover=False)
    result = process_natural_correction(
        store,
        text="把咖啡改成999元",
        target_date="2026-10-02",
        parser=RejectingParser(),
    )

    assert result["status"] == "needs_review"
    assert result["action"] == "UPDATE"
    assert result["event"] is None
    assert result["correction"] is None
    assert result["review_reason"] == "transaction_state_missing"
    assert result["proposed_amount"] == 999.0


def test_deepseek_parser_offline_fallback() -> None:
    """Test that DeepSeekTriIntentParser falls back offline when no API key or error."""
    # No API key -> offline fallback
    parser = DeepSeekTriIntentParser(api_key="")
    res = parser.parse("今天下午我还花了4.9元买了一瓶水")
    assert res["action"] == "ADD"
    assert res["amount"] == 4.90

    # With API key but broken opener -> falls back to offline
    def broken_opener(*args: object, **kwargs: object) -> None:
        raise OSError("Network connection refused")

    parser_broken = DeepSeekTriIntentParser(
        api_key="sk-test-key",
        opener=broken_opener,
    )
    res_fallback = parser_broken.parse("记账超市买生活用品80块")
    assert res_fallback["action"] == "ADD"
    assert res_fallback["amount"] == 80.0


def test_process_natural_correction_add_and_resolve_intent(
    tmp_path: Path,
) -> None:
    """Test process_natural_correction supplement and intent resolution."""
    store = JobStore(tmp_path / "web", recover=False)
    intent_pool = ActiveIntentPool(tmp_path / "active-intents.json")

    # 1. Register pending intent from earlier in the day
    intent = intent_pool.register_intent(
        text="要去买瓶水",
        domain="Finance",
        target_entity="水",
        timestamp="2026-10-02T20:35:37+08:00",
    )
    assert intent["status"] == "pending"

    # 2. Process user supplement in the evening: "今天下午我还花了4.9元买了一瓶水"
    # 【2026-10-15 修】必须显式传**离线** parser。
    # 原因：process_natural_correction 里是 `intent_parser = parser or DeepSeekTriIntentParser()`，
    # 不传 parser 时会现造一个会捡环境变量 DEEPSEEK_API_KEY 的联网实例 —— 于是本用例
    # 真的去打线上 LLM，返回的 title 变成模型爱写的「买了一瓶水」而不是离线正则的「水」，
    # 结果随机红绿（实测同一句三次跑出三个值），还偷偷花钱。api_key="" 现已是明确的
    # 「离线」信号（__init__ 里 None 才回落环境变量）。
    res = process_natural_correction(
        store,
        text="今天下午我还花了4.9元买了一瓶水",
        target_date="2026-10-02",
        intent_pool=intent_pool,
        parser=DeepSeekTriIntentParser(api_key=""),
    )
    assert res["status"] == "ok"
    assert res["action"] == "ADD"
    event = res["event"]
    assert event["title"] == "水"
    assert event["data"]["expense"]["amount"] == 4.90
    assert event["source_type"] in {"user_supplement", "manual"}
    assert event["review_status"] == "confirmed"

    # Verify pending intent was resolved
    resolved_intents = res["intent_resolution"]
    assert len(resolved_intents) == 1
    assert resolved_intents[0]["intent_id"] == intent["intent_id"]
    assert resolved_intents[0]["status"] == "resolved"

    # Verify daily summary on disk was recomputed
    job_id = res["job_id"]
    summary_path = store.job_path(job_id) / "2026-10-02" / "daily_summary.json"
    assert summary_path.is_file()
    summary_data = json.loads(summary_path.read_text(encoding="utf-8"))
    assert summary_data["expense_total_cny"] == 4.90
    assert "audit_trail" in summary_data
    assert len(summary_data["audit_trail"]) >= 1


def test_process_natural_correction_hotword_elevation(tmp_path: Path) -> None:
    """Test hotword elevation to active_hotword capture_mode."""
    store = JobStore(tmp_path / "web", recover=False)
    res = process_natural_correction(
        store,
        text="记一笔买咖啡15元",
        target_date="2026-10-02",
    )
    assert res["status"] == "ok"
    assert res["capture_mode"] == "active_hotword"
    assert res["event"]["data"]["expense"]["amount"] == 15.0


def test_process_natural_correction_update_and_summary_recompute(
    tmp_path: Path,
) -> None:
    """Test updating existing event amount and verifying summary recomputation."""
    store = JobStore(tmp_path / "web", recover=False)
    job_id = store.create_job()
    artifact_path = "2026-10-02/events.json"

    # Seed base event: supermarket 140 CNY
    job_dir = store.job_path(job_id)
    target = job_dir / artifact_path
    target.parent.mkdir(parents=True, exist_ok=True)
    base_event = {
        "event_id": "evt_market_140",
        "artifact_path": artifact_path,
        "categories": ["expense"],
        "activity_type": "purchase",
        "title": "超市",
        "time": {"absolute": "2026-10-02T18:17:15+08:00", "precision": "exact"},
        "data": {"expense": {"amount": 140.0, "currency": "CNY", "item": "超市"}},
        "capture_mode": "passive",
        "source_type": "audio",
        "confidence": 0.95,
        "review_status": "confirmed",
    }
    target.write_text(json.dumps([base_event], ensure_ascii=False), encoding="utf-8")

    # Update event: "刚刚那笔超市不是140是150"
    res = process_natural_correction(
        store,
        text="刚刚那笔超市不是140是150",
        job_id=job_id,
        target_date="2026-10-02",
        artifact_path=artifact_path,
    )
    assert res["status"] == "ok"
    assert res["action"] == "UPDATE"
    assert res["correction"] is not None
    assert res["event"]["data"]["expense"]["amount"] == 150.0

    # Verify daily summary on disk recomputed to 150.0 CNY
    summary_path = target.parent / "daily_summary.json"
    assert summary_path.is_file()
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert summary["expense_total_cny"] == 150.0
    assert any(a["action"] == "UPDATE" for a in summary.get("audit_trail", []))


def test_process_natural_correction_delete_and_summary_recompute(
    tmp_path: Path,
) -> None:
    """Test rejecting/deleting event and verifying summary exclusion."""
    store = JobStore(tmp_path / "web", recover=False)
    job_id = store.create_job()
    artifact_path = "2026-10-02/events.json"

    # Seed base events: supermarket 140 CNY, smoke 9 CNY
    job_dir = store.job_path(job_id)
    target = job_dir / artifact_path
    target.parent.mkdir(parents=True, exist_ok=True)
    events = [
        {
            "event_id": "evt_market_140",
            "artifact_path": artifact_path,
            "categories": ["expense"],
            "activity_type": "purchase",
            "title": "超市",
            "time": {"absolute": "2026-10-02T18:17:15+08:00", "precision": "exact"},
            "data": {"expense": {"amount": 140.0, "currency": "CNY", "item": "超市"}},
            "capture_mode": "passive",
            "source_type": "audio",
            "confidence": 0.95,
            "review_status": "confirmed",
        },
        {
            "event_id": "evt_smoke_9",
            "artifact_path": artifact_path,
            "categories": ["expense"],
            "activity_type": "purchase",
            "title": "买烟",
            "time": {"absolute": "2026-10-02T20:35:31+08:00", "precision": "exact"},
            "data": {"expense": {"amount": 9.0, "currency": "CNY", "item": "烟"}},
            "capture_mode": "passive",
            "source_type": "audio",
            "confidence": 0.95,
            "review_status": "confirmed",
        },
    ]
    target.write_text(json.dumps(events, ensure_ascii=False), encoding="utf-8")

    # Delete smoke purchase: "删除刚才买烟的记录"
    res = process_natural_correction(
        store,
        text="删除刚才买烟的记录",
        job_id=job_id,
        target_date="2026-10-02",
        artifact_path=artifact_path,
    )
    assert res["status"] == "ok"
    assert res["action"] == "DELETE"
    assert res["correction"]["rejected"] is True

    # Verify daily summary on disk recomputed to exclude 9 CNY (total: 140.0 CNY)
    summary_path = target.parent / "daily_summary.json"
    assert summary_path.is_file()
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert summary["expense_total_cny"] == 140.0
    assert any(a["action"] == "DELETE" for a in summary.get("audit_trail", []))


def test_natural_correction_http_api_endpoints(tmp_path: Path, monkeypatch) -> None:
    """Test POST /api/events/natural-correction endpoint via local web server."""
    # Keep this focused correction test offline/hermetic: the production server
    # may have a live model key, but this test covers endpoint wiring, not L2.
    monkeypatch.setattr(
        "life_logger.local_pipeline.local_pipeline_env", lambda _config=None: {}
    )
    store = JobStore(tmp_path / "web", recover=False)
    server = create_server(host="127.0.0.1", port=0, store=store)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"

    try:
        # 1. Test ADD supplement via HTTP API
        add_payload = json.dumps(
            {
                "text": "记一笔买咖啡15元",
                "date": "2026-10-02",
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            f"{base_url}/api/events/natural-correction",
            data=add_payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req) as resp:
            assert resp.status == 200
            data = json.loads(resp.read().decode("utf-8"))
            assert data["status"] == "ok"
            assert data["action"] == "ADD"
            assert data["capture_mode"] == "active_hotword"
            assert data["event"]["data"]["expense"]["amount"] == 15.0

        # 2. Test UPDATE correction via HTTP API
        job_id = data["job_id"]
        update_payload = json.dumps(
            {
                "text": "把刚才咖啡改成18元",
                "job_id": job_id,
                "date": "2026-10-02",
                "target_event_id": data["event"]["event_id"],
            }
        ).encode("utf-8")
        req_update = urllib.request.Request(
            f"{base_url}/api/events/natural-correction",
            data=update_payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req_update) as resp:
            assert resp.status == 200
            data_up = json.loads(resp.read().decode("utf-8"))
            assert data_up["status"] == "ok"
            assert data_up["action"] == "UPDATE"
            assert data_up["event"]["data"]["expense"]["amount"] == 18.0

        # 3. Test DELETE correction via HTTP API
        delete_payload = json.dumps(
            {
                "text": "取消刚才这笔咖啡",
                "job_id": job_id,
                "date": "2026-10-02",
                "target_event_id": data["event"]["event_id"],
            }
        ).encode("utf-8")
        req_del = urllib.request.Request(
            f"{base_url}/api/events/natural-correction",
            data=delete_payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req_del) as resp:
            assert resp.status == 200
            data_del = json.loads(resp.read().decode("utf-8"))
            assert data_del["status"] == "ok"
            assert data_del["action"] == "DELETE"
            assert data_del["correction"]["rejected"] is True

        # 4. Test invalid empty input returns HTTP 400
        req_empty = urllib.request.Request(
            f"{base_url}/api/events/natural-correction",
            data=json.dumps({"text": ""}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            urllib.request.urlopen(req_empty)
            assert False, "Should have raised HTTPError"
        except HTTPError as err:
            assert err.code == 400

    finally:
        server.shutdown()
        server.server_close()


def test_deepseek_request_sends_browser_like_user_agent() -> None:
    """回归：请求必须带非 urllib 默认的 User-Agent，否则被 Cloudflare 403 秒拒。

    【2026-10-14 实测事故】`_call_deepseek()` 原来只带 Authorization + Content-Type，
    Command Code 端点前置的 Cloudflare 会用 `403 error code: 1010` **在几毫秒内**拒绝
    Python urllib 的默认 UA。异常被 `parse()` 吞掉后静默退回离线正则，表现为
    「纠错结果不对」而没有任何报错：实测「把咖啡改成18元」被抽成实体「把咖啡改成」，
    金额也抽不到。加上 `User-Agent: curl/8.7.1` 后同一句话正确解析为
    `{"action":"UPDATE","title":"咖啡","amount":18.0}`。

    这个断言防的是「有人为了好看把头删掉」——它看起来冗余，实际是整条纠错链的开关。
    """
    captured: dict[str, object] = {}

    class _Resp:
        def __enter__(self) -> "_Resp":
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def read(self) -> bytes:
            return json.dumps(
                {"choices": [{"message": {"content": json.dumps({"action": "ADD", "title": "咖啡"})}}]}
            ).encode("utf-8")

    def capturing_opener(req: object, timeout: float = 0.0) -> "_Resp":
        # urllib 的 Request.headers 会把头名规范化成 "User-agent"
        captured["headers"] = dict(getattr(req, "headers", {}) or {})
        captured["url"] = getattr(req, "full_url", "")
        return _Resp()

    parser = DeepSeekTriIntentParser(
        api_key="test-key",
        base_url="https://example.invalid/provider/v1",
        model="deepseek/deepseek-v4-flash",
        opener=capturing_opener,
    )
    parser.parse("记一笔买咖啡15元")

    headers = {str(k).lower(): str(v) for k, v in dict(captured["headers"]).items()}
    assert "user-agent" in headers, f"缺少 User-Agent，会被 Cloudflare 403 拦掉：{headers}"
    assert "urllib" not in headers["user-agent"].lower(), headers["user-agent"]
    assert headers["user-agent"] == "curl/8.7.1"
    assert headers.get("accept") == "application/json"
    assert headers.get("authorization") == "Bearer test-key"
    assert str(captured["url"]).endswith("/provider/v1/chat/completions"), captured["url"]
