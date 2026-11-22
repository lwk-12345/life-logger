from scripts.downstream_benchmark import build_prompt, chunk_segments, ollama_options


def test_chunk_segments_preserves_order_and_respects_character_limit() -> None:
    segments = [
        {"index": 0, "start_s": 0.0, "end_s": 1.0, "text": "第一段"},
        {"index": 1, "start_s": 2.0, "end_s": 3.0, "text": "第二段"},
        {"index": 2, "start_s": 4.0, "end_s": 5.0, "text": "第三段"},
    ]

    batches = chunk_segments(segments, max_chars=90)

    assert [item["index"] for batch in batches for item in batch] == [0, 1, 2]
    assert all(len(str(batch)) <= 90 for batch in batches)


def test_build_prompt_requires_evidence_backed_seven_category_json() -> None:
    prompt = build_prompt(
        model_name="sensevoice",
        segments=[{"index": 0, "start_s": 1.0, "end_s": 2.0, "text": "我刚起床。"}],
    )

    assert "routine" in prompt
    assert "work_communication" in prompt
    assert "evidence_quote" in prompt
    assert "只使用 ASR 文本" in prompt


def test_ollama_options_bound_generation_length() -> None:
    options = ollama_options(max_output_tokens=512)

    assert options == {
        "temperature": 0,
        "num_ctx": 16_384,
        "num_predict": 512,
    }
