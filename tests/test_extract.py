import pytest

from life_logger.aggregate import build_daily_summary
from life_logger.domain import TranscriptSegment
from life_logger.extract import RuleBasedExtractor


def transcript(text: str, segment_id: str = "seg_001") -> TranscriptSegment:
    return TranscriptSegment(
        segment_id=segment_id,
        recording_id="rec_001",
        start_offset_seconds=120.0,
        end_offset_seconds=130.0,
        absolute_started_at="2026-09-02T08:02:00+08:00",
        absolute_ended_at="2026-09-02T08:02:10+08:00",
        text=text,
        source="demo",
        language="zh",
        transcription_confidence=None,
    )


def test_explicit_purchase_creates_expense_and_food_event() -> None:
    events = RuleBasedExtractor().extract(transcript("记录一下，花4块9买了一瓶饮料。"))

    assert len(events) == 1
    assert events[0].categories == ["expense", "food"]
    assert events[0].data["expense"]["amount"] == 4.9
    assert events[0].data["food"]["quantity"] == 1
    assert events[0].capture_mode == "active"
    assert events[0].confidence >= 0.9
    assert events[0].evidence["quote"] == "记录一下，花4块9买了一瓶饮料。"
    assert events[0].evidence["absolute_started_at"] == "2026-09-02T08:02:00+08:00"
    assert events[0].evidence["absolute_ended_at"] == "2026-09-02T08:02:10+08:00"


def test_purchase_without_explicit_amount_stays_transcript_only() -> None:
    events = RuleBasedExtractor().extract(transcript("刚买烟回来"))

    assert events == []


def test_sep26_watch_purchase_extracts_trailing_spoken_amount() -> None:
    events = RuleBasedExtractor().extract(
        transcript(
            "买了一个智慧充电口，268，然后去超市准备消费。出门忘带钥匙了。",
            "seg_sep26_watch",
        )
    )

    assert len(events) == 1
    assert "expense" in events[0].categories
    assert events[0].data["expense"]["amount"] == 268.0
    assert "智慧充电口" in events[0].data["expense"]["item"]


def test_purchase_amount_formats_cover_currency_decimal_and_thousands() -> None:
    cases = [
        ("买了一个充电头，¥268.50，然后回家。", 268.5),
        ("花了1,268元买了一个耳机。", 1268.0),
        ("付款￥99买了数据线。", 99.0),
        ("支付了￥268。", 268.0),
        ("支付了268.50元。", 268.5),
        ("买了个充电器，268块5毛。", 268.5),
        ("买了个充电器，二百六十八块五。", 268.5),
        ("买了个充电器，268元5角。", 268.5),
        ("买了个充电器，268元5角6分。", 268.56),
        ("支付了268，买了一个充电器。", 268.0),
        ("花了四块九买了瓶水。", 4.9),
        ("优惠券减了20元，实付248元。", 248.0),
        ("单价20元，买了3个，总共60元。", 60.0),
        ("原价300元，折后价268元。", 268.0),
        ("付了十五块五。", 15.5),
        ("扫了二十元打车。", 20.0),
        ("微信扫了十八块八吃饭。", 18.8),
        ("给司机付了十五块五。", 15.5),
        ("转了三十二元房租。", 32.0),
        ("坐地铁刷了六块。", 6.0),
        ("打车扣了二十三块六。", 23.6),
        ("支付宝扣了九块九买水。", 9.9),
        ("今天午饭花了十二块八。", 12.8),
        ("早餐付了八块五。", 8.5),
        ("停车扫了十二块。", 12.0),
        ("咖啡我付了十六块八。", 16.8),
        ("高铁站扫了三十五块买饭。", 35.0),
        ("打车我微信付了二十六块三。", 26.3),
        ("买早餐刷了七块五。", 7.5),
        ("夜宵我扫了十九块八。", 19.8),
        ("便利店付了十一块二。", 11.2),
        ("晚饭微信付了二十一块六。", 21.6),
        ("停车费扣了八块。", 8.0),
        ("奶茶我扫了十三块九。", 13.9),
        ("共享单车扣了一块五。", 1.5),
        ("下午茶微信扫了十七块三。", 17.3),
        ("地铁进站刷了五块。", 5.0),
        ("我刚刚给便利店付了十四块六买水。", 14.6),
        ("下楼取快递顺手扫了九块买饮料。", 9.0),
        ("我刚在楼下买咖啡，微信付了十八块五。", 18.5),
        ("回家路上打了个车，最后扣了二十九块四。", 29.4),
        ("菜市场买了点菜，给老板付了二十七块八。", 27.8),
        ("骑完共享单车以后自动扣了两块五。", 2.5),
        ("停车场出来的时候自动扣了十二块。", 12.0),
        ("公司楼下吃午饭，最后微信付了二十四块七。", 24.7),
        ("刚开完会去买了杯咖啡，扫码付了十五块九。", 15.9),
        ("从超市回来的路上打车，平台最后扣了三十一块二。", 31.2),
        ("散步回来顺路买了瓶水，微信扫了六块八。", 6.8),
        ("看完电影出来打车回家，最后自动扣了四十二块五。", 42.5),
        ("中午跟同事吃完饭，我最后微信付了三十六块八。", 36.8),
        ("去医院回来坐地铁，进站的时候刷了四块五。", 4.5),
        ("买完药顺便在楼下便利店拿了瓶水，一共付了二十三块六。", 23.6),
        ("晚上加完班打车回家，到家以后平台扣了三十八块九。", 38.9),
        ("下班去超市买了点水果，结账的时候微信付了四十七块三。", 47.3),
        ("健身回来顺路买了瓶饮料，扫码一共花了八块六。", 8.6),
        ("早餐八块五。", 8.5),
        ("今天打车二十六块三。", 26.3),
        ("午饭十二块八。", 12.8),
        ("停车六块。", 6.0),
        ("奶茶十五块五。", 15.5),
        ("电影票四十八块。", 48.0),
        ("咖啡九块九。", 9.9),
        ("公交两块。", 2.0),
        ("早餐十块。", 10.0),
        ("打车三十三块五。", 33.5),
        ("矿泉水三块五。", 3.5),
        ("地铁五块五。", 5.5),
        ("豆浆四块。", 4.0),
        ("早餐包七块五。", 7.5),
        ("早餐十五块五毛三分。", 15.53),
        ("停车十二块三分。", 12.03),
        ("公交五毛。", 0.5),
        ("买糖三毛五分。", 0.35),
        ("买糖三毛钱五分。", 0.35),
        ("公交五角钱。", 0.5),
        ("买菜一块两毛三分。", 1.23),
        ("矿泉水两块零五分。", 2.05),
    ]

    for index, (text, expected) in enumerate(cases):
        events = RuleBasedExtractor().extract(transcript(text, f"seg_money_{index}"))
        assert len(events) == 1
        assert events[0].data["expense"]["amount"] == expected


def test_purchase_amount_supports_chinese_hundreds_and_digit_sequence() -> None:
    written = RuleBasedExtractor().extract(
        transcript("买了一个充电口，二百六十八，然后回家。", "seg_cn_hundreds")
    )
    spoken_digits = RuleBasedExtractor().extract(
        transcript("买了一个充电口，二六八，然后回家。", "seg_cn_digits")
    )

    assert written[0].data["expense"]["amount"] == 268.0
    assert spoken_digits[0].data["expense"]["amount"] == 268.0


def test_trailing_year_is_not_misread_as_purchase_amount() -> None:
    events = RuleBasedExtractor().extract(
        transcript("买了一个旧充电器，2026年出的型号。", "seg_year_not_amount")
    )

    assert events == []


def test_duration_number_near_purchase_word_is_not_misread_as_money() -> None:
    events = RuleBasedExtractor().extract(
        transcript("花了30分钟买完东西。", "seg_duration_not_amount")
    )

    assert events == []


@pytest.mark.parametrize(
    "text",
    [
        "买了3个268型号的接头。",
        "买了一个型号268的充电器。",
        "花了2个小时买东西。",
        "支付订单号268买家已确认。",
    ],
)
def test_purchase_parser_ignores_quantity_model_and_order_numbers(text: str) -> None:
    assert RuleBasedExtractor().extract(transcript(text, "seg_not_money")) == []


@pytest.mark.parametrize(
    "text",
    [
        "买了一个电池，3.7V。",
        "买了一个硬盘，512GB。",
        "买了一个屏幕，120Hz。",
    ],
)
def test_purchase_parser_ignores_product_specs_after_comma(text: str) -> None:
    assert RuleBasedExtractor().extract(transcript(text, "seg_spec_not_money")) == []


@pytest.mark.parametrize(
    "text",
    [
        "买了一个东西，优惠券减了20元。",
        "买了一个充电器，订单编号￥268。",
        "买了一个适配器，版本号268。",
    ],
)
def test_purchase_parser_does_not_promote_discount_or_identifier_to_price(text: str) -> None:
    events = RuleBasedExtractor().extract(transcript(text, "seg_identifier_not_price"))
    assert all(
        event.data.get("expense", {}).get("amount") is None
        for event in events
        if "expense" in event.categories
    )


def test_routine_is_passive_without_active_marker() -> None:
    events = RuleBasedExtractor().extract(transcript("今天早上八点十分起床了。"))

    assert events[0].categories == ["routine"]
    assert events[0].capture_mode == "passive"
    assert events[0].activity_type == "waking_up"
    assert events[0].confidence >= 0.9


def test_exercise_parses_chinese_duration() -> None:
    events = RuleBasedExtractor().extract(transcript("下午跑步三十分钟。"))

    assert events[0].categories == ["exercise"]
    assert events[0].data["exercise"]["duration_minutes"] == 30


def test_food_keeps_multiple_items_from_one_meal() -> None:
    events = RuleBasedExtractor().extract(transcript("早餐吃了两个鸡蛋和一碗粥。"))

    items = events[0].data["food"]["items"]

    assert {item["name"] for item in items} == {"鸡蛋", "粥"}
    assert {item["name"]: item["quantity"] for item in items} == {
        "鸡蛋": 2,
        "粥": 1,
    }


def test_spoken_time_refines_event_time() -> None:
    events = RuleBasedExtractor().extract(transcript("今天早上八点十分起床了。"))

    assert events[0].time.absolute == "2026-09-02T08:10:00+08:00"
    assert events[0].data["routine"]["reported_time"] == "08:10"


def test_reminder_command_is_active() -> None:
    events = RuleBasedExtractor().extract(transcript("明天提醒我把录音文件导出来。"))

    assert events[0].capture_mode == "active"
    assert events[0].confidence >= 0.9


def test_conversation_stays_transcript_only_but_clear_todo_survives() -> None:
    extractor = RuleBasedExtractor()

    work = extractor.extract(transcript("上午和同事沟通了项目方向。", "seg_work"))
    idea = extractor.extract(transcript("我觉得应该先做好音频主链路。", "seg_idea"))
    todo = extractor.extract(transcript("明天提醒我把录音文件导出来。", "seg_todo"))

    assert work == []
    assert idea == []
    assert todo[0].categories == ["todo"]
    assert todo[0].confidence >= 0.9


def test_unsupported_text_is_not_invented_as_an_event() -> None:
    events = RuleBasedExtractor().extract(transcript("今天风很大。"))

    assert events == []


def test_food_intention_is_not_counted_as_food_intake() -> None:
    events = RuleBasedExtractor().extract(
        transcript("我想一下，吃啥？是直接做菜呢？还是先吃一个呀？")
    )

    assert events == []


def test_chinese_money_and_food_are_extracted_from_one_purchase() -> None:
    events = RuleBasedExtractor().extract(
        transcript("花了三十块四毛，点了一份炸鸡，然后返现应该会返十四块钱。")
    )

    assert len(events) == 1
    assert events[0].categories == ["expense", "food"]
    assert events[0].data["expense"]["amount"] == 30.4
    assert events[0].data["food"]["item"] == "炸鸡"


def test_ambiguous_work_discussion_stays_transcript_only() -> None:
    events = RuleBasedExtractor().extract(
        transcript(
            "其实今天中午应该是有一次汇报的。你看一下，是因为断网原因还是其他什么原因。"
        )
    )

    assert events == []


def test_recording_quality_comment_is_not_work_activity() -> None:
    events = RuleBasedExtractor().extract(transcript("边充电边录没问题。"))

    assert events == []


def test_event_evidence_preserves_audio_offsets_for_merging() -> None:
    events = RuleBasedExtractor().extract(transcript("今天早上八点十分起床了。"))

    assert events[0].evidence["start_offset_seconds"] == 120.0
    assert events[0].evidence["end_offset_seconds"] == 130.0


def test_personal_strategy_without_save_command_stays_transcript_only() -> None:
    events = RuleBasedExtractor().extract(
        transcript("看直播的话，我都会戴耳机啊，这样能避免一些语言的误触。")
    )

    assert events == []


def test_mentioning_reduced_workload_is_not_work_activity() -> None:
    events = RuleBasedExtractor().extract(
        transcript("为了减少工作量，我日常的这个游戏呀、直播呀。")
    )

    assert events == []



def test_generic_project_chatter_is_not_promoted_to_work_event() -> None:
    events = RuleBasedExtractor().extract(
        transcript("我这两年一直把这个项目给忘记了，就是这个 AI 硬件这块一直没录音。")
    )

    assert events == []

def test_decimal_money_is_not_parsed_as_a_spoken_clock_time() -> None:
    events = RuleBasedExtractor().extract(
        transcript("花了二十二块三点了，两份清补凉，一份椰奶的，一份椰子水的。")
    )

    assert len(events) == 1
    assert events[0].data["expense"]["amount"] == 22.3
    assert events[0].time.absolute == "2026-09-02T08:02:00+08:00"


def test_sep21_cigarette_asr_correction_extracts_16_yuan_purchase() -> None:
    raw = (
        "我刚才花16买了一包短剧的乐土，我希望你这里面已经幽静，"
        "去提示这个购买消费行为。"
    )
    events = RuleBasedExtractor().extract(transcript(raw, "seg_sep21_smoke"))

    assert len(events) == 1
    event = events[0]
    assert event.categories == ["expense"]
    assert event.data["expense"]["amount"] == 16.0
    assert event.data["expense"]["item"] == "短支的乐途"
    assert event.title == "花了16元，买了一包「短支的乐途」"
    assert event.evidence["raw_quote"] == raw
    assert event.evidence["quote"].startswith("我刚才花16买了一包短支的乐途")
    assert event.evidence["text_corrections"] == ["cigarette_term:短支的乐途"]


def test_abandoned_ten_yuan_cigarette_choice_is_not_counted_as_expense() -> None:
    events = RuleBasedExtractor().extract(
        transcript("拿包十块的那个，哦，算了，换口味吧。", "seg_abandoned")
    )

    assert events == []


def test_sep21_spending_total_is_16_not_abandoned_10() -> None:
    extractor = RuleBasedExtractor()
    events = []
    events.extend(
        extractor.extract(
            transcript("拿包十块的那个，哦，算了，换口味吧。", "seg_choice")
        )
    )
    events.extend(
        extractor.extract(
            transcript("我刚才花16买了一包短剧的乐土。", "seg_purchase")
        )
    )

    summary = build_daily_summary(
        events,
        date="2026-09-21",
        timezone_name="Asia/Shanghai",
        warnings=[],
    )

    assert len(events) == 1
    assert summary["expense_total_cny"] == 16.0


def test_need_statements_are_ideas_not_todos_after_todo_lockdown() -> None:
    """[2026-10-13 收口] 「需要+动词」不再升为待办 —— 待办的唯一入口是口令词。

    旧设计把「我需要去提交周报」直接判 todo，实测这类随口意向每天堆出几十条
    无人管理的待办（用户 2026-10-12 单日 253 条）。现在降级为 idea：
    内容仍被记录，但不进待办列表、不推 OpenClaw、不占徽章的 4 个显示槽。
    """
    extractor = RuleBasedExtractor()

    # 问句 / 闲聊里的「需要」照旧不成事件
    assert extractor.extract(transcript("难道说需要我重新打开一下VPN？", "seg_q")) == []
    assert extractor.extract(
        transcript(
            "我只看它的一个形态，谁更便携，然后谁可以满足这个录音一整天的一个需要。",
            "seg_need",
        )
    ) == []

    # 带动作的「需要…」现在是想法，不是待办
    need_statement = extractor.extract(transcript("我需要去提交周报。", "seg_action"))
    assert need_statement[0].categories == ["idea"]

    # 只有口令词才升为待办
    explicit = extractor.extract(transcript("提醒我提交周报。", "seg_cmd"))
    assert explicit[0].categories == ["todo"]


def test_recollections_and_negations_are_not_todos() -> None:
    extractor = RuleBasedExtractor()
    non_todos = [
        "智能检索，我记得当时是做了",
        "然后你里面记得一些各种详细信息也可以，好不好",
        "我只看它的一个形态，谁更便携，然后谁可以满足这个录音一整天的一个需要",
        "难道说需要我重新打开一下VPN",
        "你先把你能做的能够做好，然后需要我的时候咱们再配合",
        "我记得我是一直开着的，不知道为啥容易退出",
        "你要记得，就是每个方向的都要严格按照那个",
        "我记得那个Windows上面可以不需要去做签名授权",
        "我记得那个小米的遥控器就是",
        "我记得你之前说过，你之前说过他的一个算了，他的休眠时间改为五分钟吧",
        "呃，不需要去介入专门的一个软件，就映射快映射按键就可以了",
    ]
    for text in non_todos:
        events = extractor.extract(transcript(text, "seg_recall"))
        todo_events = [e for e in events if "todo" in e.categories]
        assert todo_events == [], f"Should not extract todo from: {text}"


def test_implausible_low_confidence_volume_stays_transcript_only() -> None:
    events = RuleBasedExtractor().extract(
        transcript("今天喝了九百五十九百升的一个牛奶。", "seg_bad_volume")
    )

    assert events == []


def test_narrative_past_purchases_are_suppressed_from_daily_expense() -> None:
    extractor = RuleBasedExtractor()

    # 1. User's exact sentence about Turkish Lira gift card:
    events = extractor.extract(
        transcript("然后我用的买的是礼品卡，我是七十二块钱买了四百九十九里拉的", "seg_gift_card")
    )
    assert len(events) == 1
    assert events[0].categories == ["idea"]
    assert events[0].activity_type == "experience_sharing"
    assert "expense" not in events[0].categories
    assert "expense" not in events[0].data

    # 2. Historical recounting with "当时花了":
    events_past = extractor.extract(
        transcript("我当时花了一百块买了个耳机", "seg_past_headphone")
    )
    assert len(events_past) == 1
    assert events_past[0].categories == ["idea"]
    assert events_past[0].activity_type == "experience_sharing"

    # 3. Price comparison inquiry with "划算吗":
    events_inquiry = extractor.extract(
        transcript("七十二块钱买了四百九十九里拉划算吗？", "seg_inquiry")
    )
    assert len(events_inquiry) == 1
    assert events_inquiry[0].categories == ["idea"]
    assert events_inquiry[0].activity_type == "curiosity_question"

    # 4. Ensure these do not inflate expense_total_cny:
    summary = build_daily_summary(
        events + events_past + events_inquiry,
        date="2026-10-04",
        timezone_name="Asia/Shanghai",
        warnings=[],
    )
    assert summary["expense_total_cny"] == 0.0


def test_spatial_and_together_piece_expressions_are_not_one_yuan_expense() -> None:
    extractor = RuleBasedExtractor()
    assert extractor.extract(transcript("这一块是上中下三个键", "seg_keys")) == []
    assert extractor.extract(transcript("那一块屏幕坏了", "seg_screen")) == []
    assert extractor.extract(transcript("大家一块去吃饭", "seg_together")) == []


def test_active_explicit_accounting_commands_bypass_to_confirmed_expense() -> None:
    extractor = RuleBasedExtractor()
    events = extractor.extract(transcript("记一笔，七十二块钱买了礼品卡", "seg_active_1"))
    assert len(events) == 1
    assert events[0].categories == ["expense"]
    assert events[0].data["expense"]["amount"] == 72.0
    assert events[0].capture_mode == "active"
    assert events[0].evidence.get("active_direct") is True

    events_2 = extractor.extract(transcript("记录一下 15元咖啡", "seg_active_2"))
    assert len(events_2) == 1
    assert events_2[0].data["expense"]["amount"] == 15.0
    assert events_2[0].capture_mode == "active"


def test_ambient_cashier_and_pos_prompts_are_filtered() -> None:
    extractor = RuleBasedExtractor()
    assert extractor.extract(transcript("三块二可以付了，这四个菊花茶谁的？我的。", "seg_tea")) == []
    assert extractor.extract(transcript("请解锁手机，碰一下会员支付。", "seg_pos1")) == []
    assert extractor.extract(transcript("该商品请解锁手机，碰一下会员支付。", "seg_pos2")) == []


def test_returned_change_does_not_create_fake_expense() -> None:
    extractor = RuleBasedExtractor()
    assert extractor.extract(transcript("这。没有。然后两块钱零钱。", "seg_change1")) == []
    assert extractor.extract(transcript("找您五块钱零钱", "seg_change2")) == []


def test_oral_convenience_store_purchase_with_habit_price() -> None:
    extractor = RuleBasedExtractor()
    events = extractor.extract(transcript("老板，拿一包那个芒果，有吗？", "seg_smoke"))
    assert len(events) == 1
    assert events[0].categories == ["expense"]
    assert events[0].data["expense"]["amount"] == 10.0
    assert events[0].data["expense"]["item"] == "芒果香烟（10元档）"


