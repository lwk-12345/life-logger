"""Coverage repair must preserve answered sources and retry only omitted IDs."""

import json

from life_logger.contextual_extraction import run_contextual_extraction


class Response:
    def __init__(self, records):
        self.body = json.dumps(
            {"choices": [{"message": {"content": json.dumps({"records": records})}}]}
        ).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self.body


def rows():
    return [
        {
            "segment_id": str(i),
            "recording_id": "rec",
            "source_label": "self",
            "text": f"无事件测试{i}",
            "absolute_started_at": "2026-10-16T12:00:00+08:00",
            "start_offset_seconds": i,
            "end_offset_seconds": i + 1,
        }
        for i in range(2)
    ]


def packet(request):
    body = json.loads(request.data)
    return json.loads(body["messages"][0]["content"].split("输入记录：\n", 1)[1])


def no_event(sid):
    return {"source_id": sid, "is_event": False, "events": []}


def run(opener, directory):
    return run_contextual_extraction(
        rows(),
        deepseek_api_key="offline",
        deepseek_base_url="https://example.invalid/v1",
        deepseek_opener=opener,
        output_dir=directory / "result",
        repair_missing_sources=True,
    )


def test_missing_answer_is_repaired_without_reasking_answered_source(tmp_path):
    calls = []

    def opener(request, timeout):
        records = packet(request)
        calls.append([r["source_id"] for r in records])
        return Response([no_event(records[0]["source_id"])])

    result = run(opener, tmp_path)
    assert len(calls) == 2
    assert len(calls[0]) == 2 and calls[1] == [calls[0][1]]
    assert result["pending_sources"] == []
    assert result["status"] == "completed"
    assert result["batches"][0]["coverage_repair"]["recovered_count"] == 1


def test_partial_cache_repairs_only_missing_source_and_keeps_original_response(
    tmp_path,
):
    first_calls = []

    def first(request, timeout):
        records = packet(request)
        first_calls.append([r["source_id"] for r in records])
        if len(records) == 2:
            return Response([no_event(records[0]["source_id"])])
        # A valid but irrelevant response must not falsely satisfy coverage.
        return Response([no_event("unrelated-id")])

    initial = run(first, tmp_path)
    assert len(initial["pending_sources"]) == 1
    assert initial["batches"][0]["original_raw_response"]
    second_calls = []

    def second(request, timeout):
        records = packet(request)
        second_calls.append([r["source_id"] for r in records])
        return Response([no_event(records[0]["source_id"])])

    recovered = run(second, tmp_path)
    assert second_calls == [[first_calls[0][1]]]
    assert recovered["pending_sources"] == []
    assert recovered["status"] == "completed"


def test_missing_display_title_does_not_discard_grounded_expense(tmp_path):
    text = "午饭付了十三元。"
    row = {**rows()[0], "text": text}

    def opener(request, timeout):
        current = packet(request)[0]
        return Response(
            [
                {
                    "source_id": current["source_id"],
                    "is_event": True,
                    "events": [
                        {
                            "category": "expense",
                            "event_state": "completed",
                            "title": None,
                            "activity_type": None,
                            "quote": text,
                            "confidence": 0.95,
                            "claim_type": "event",
                            "data": {
                                "expense": {
                                    "amount": 13,
                                    "currency": "CNY",
                                    "amount_evidence": "十三元",
                                    "transaction_evidence": "付了十三元",
                                }
                            },
                        }
                    ],
                }
            ]
        )

    result = run_contextual_extraction(
        [row],
        deepseek_api_key="offline",
        deepseek_opener=opener,
        output_dir=tmp_path / "result",
    )
    assert result["formal_events"][0]["title"] == text
    assert result["formal_events"][0]["data"]["expense"]["amount"] == 13
    assert result["formal_events"][0]["evidence"]["title_source"] == "quote_fallback"
