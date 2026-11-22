from life_logger.transcript_postprocess import postprocess_transcript_text


def test_cigarette_domain_terms_are_corrected_only_in_purchase_context() -> None:
    result = postprocess_transcript_text("我刚才花16买了一包短剧的乐土。")

    assert result.text == "我刚才花16买了一包短支的乐途。"
    assert result.corrections == ("cigarette_term:短支的乐途",)
    assert result.needs_review is False


def test_implausible_drink_liters_are_flagged_without_guessing_quantity() -> None:
    raw = "今天喝了九百五十九百升的一个牛奶。"
    result = postprocess_transcript_text(raw)

    assert result.text == raw
    assert result.review_reasons == ("implausible_drink_volume_unit",)
    assert result.needs_review is True


def test_valid_milliliter_volume_is_not_flagged() -> None:
    result = postprocess_transcript_text("今天喝了九百毫升的纯牛奶。")

    assert result.review_reasons == ()
