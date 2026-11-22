"""2026-11-09 MOSS 静音/底噪提示词泄漏幻觉修复测试（方案二-B + 方案一 + 跳过 <1s 尾片）。"""
from __future__ import annotations

import wave
from pathlib import Path
from typing import Any

import pytest

from life_logger import asr_resident as ar
from life_logger import moss_transcribe as mt


def _write_dummy_wav(path: Path, duration_seconds: float, rate: int = 16000) -> None:
    frames = int(round(duration_seconds * rate))
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(b"\x01\x00" * frames)


# =============================================================================
# 正例夹具：报告第 1 节列出的 8 段泄漏全部幻觉原句与变体（含顿号变体）
# =============================================================================
LEAK_HALLUCINATION_POSITIVES: list[str] = [
    # 变体 A（AA5AD2A35C2A seq=000150, 000152, 000157, 000174）
    "好的，那我们接下来就进行一个简单的测试。首先，我将给你的音频内容转成文字，然后根据这个标准进行高精度的语音转文处理。",
    # 变体 B（2A7B05A49567 seq=000032）
    "好的，我将根据音频内容进行高精度文字转词的精确翻译。",
    "首先，我会仔细听好音频，然后对每个发音、语调进行辨认，并准确地把每一个发音、间断和声处理，保证只输出真实的音量。",
    "此外，我会注意输入正确的语音内容，不会因为错误的发音导致偏差。",
    # 变体 B（2A7B05A49567 seq=000033，含 11-08 手工脚本漏掉的顿号变体）
    "好的，我将根据音频内容进行高精度文字转词分析。",
    "首先，我会仔细听、考虑所有条件下的每一个可能音译，然后对每个发音进行精确的辨认——“a”字部分是清晰的，但需要注意：在说话中听到的声音和语境下表达的区别（比如声音变声）是在这个时间段出现的，而不是其他地方。",
    "接下来我会仔细听音频内容，逐字逐句精读，根据其词源分析，确定准确地把每句、时、候、状态、语气等音译出来。",
    # 变体 C（2A7B05A49567 seq=000037, 000038）
    "嗯，对啊，这个我也是第一次听到这种声音，感觉好像在说一些什么。",
    "我也是第一次听到这种声音，感觉好像在说一些什么。",
    "那我们接下来就进行一个简单的测试。",
    "首先，我们需要确定这个设备的性能和精度。",
    "比如，它是否支持全频段传输？",
    "以及它的响应速度如何调整以提高准确度。",
    "然后我们再进行一个简单的测试。",
    "首先，我们要确定这个设备的性能和精度。",
    "如果我们的测试结果与标准一致，那么我们就通过了这次考核。",
    "现在我们可以开始测试新的设备了。",
    "然后，我们要确保它能够运行稳定、无故障地工作。",
    "这样子的话，我们的系统就不会出现不稳定的情况。",
    "好的，那我们开始测试一下。",
]


# =============================================================================
# 反例夹具 1：报告 4.3 的 20 段正常人声转写文字（必须 0 误伤）
# =============================================================================
NORMAL_20_SPEECH_SEGMENTS: list[tuple[str, list[str]]] = [
    (
        "8065847EEA8C_seq000085",
        [
            "啊，你稍微看一下我们Life Logger的一个项目，大概的一个数据架构。",
            "然后我现在对于我们。",
            "语音转文本就是A I S R过程嘛，我觉得它可能不是现在上。",
            "呃，最好的一个结构。",
            "然后我现在用到了。",
        ],
    ),
    (
        "AA5AD2A35C2A_seq000141",
        [
            "呃，MyPi还需要更新吗？",
            "更新吗？",
            "我通过它转到我的Pi上面，显示还是一百三十一的一个上架量。",
        ],
    ),
    (
        "AA5AD2A35C2A_seq000147",
        [
            "现在主要有一个问题是，当我手表离近说话的时候，它的一个分贝是够的。",
            "然后到本地调音的时候报错了。",
        ],
    ),
    (
        "AA5AD2A35C2A_seq000149",
        [
            "一个页面啊。",
            "就是看起来还是比较复杂，注意力比较分散，都在注意这儿，我去，我给你粘那个图片啊。",
            "我。",
            "三大营养素。",
            "一是，第一个问题是三大营养素里面数据是没有的。",
            "是不显示的。",
            "第二，像这种不重要数据，像录音时长，我的生活事件次数，这东西没什么，为什么放在这么显眼的位置，而且。",
            "又比较大。",
            "反而像消费的那个，就是我给截的图片的右边那一个，那一块儿做的比较好。",
        ],
    ),
    (
        "AA5AD2A35C2A_seq000153",
        [
            "然后我感觉现在还可以再启动一个任务，就是专门去做这个前端页面的一个优化。",
            "你看现在六个终端的模型额度都还够，处理速度也挺快。",
        ],
    ),
    (
        "AA5AD2A35C2A_seq000165",
        [
            "哦，然后我们现在还有可以进行任务呀。",
            "然后你看一下那个现在有一个掌控是Antigravity吗？",
            "他感觉他没啥任务。",
        ],
    ),
    (
        "2A7B05A49567_seq000041",
        [
            "终端现在我的API服务正常。",
            "对，就是这个工作啊。",
            "需要让他们重新接上之前的任务吗？",
            "还是说之前任务已经被别人给做好了？",
            "嗯。",
        ],
    ),
    (
        "2A7B05A49567_seq000042",
        [
            "之前就是美业它的Airflow",
            "它都是这个",
            "拍立得的后期",
        ],
    ),
    (
        "2A7B05A49567_seq000000",
        [
            "今天测试一下我们的这个系统的稳定性，看看说话能不能正常被收录进去。",
            "去买个冰杯，太远了算了，直接买瓶可乐支付宝碰一下。",
        ],
    ),
    (
        "2A7B05A49567_seq000031",
        [
            "我实测的原型里就能看到，这个主绘画就是前台，这两个是番茄钟和BMI两张工单各自的工作区。我报错以后，主绘画写的是已经同步给这张工单的OFFER，由他修复。",
            "第二块是筛消息，普通的AI要你开口它才动。它不一样，它会自己看群消息、邮件、日常提醒、代码评论，这些是真实的，名字叫TRIGGER。可这些消息大多不用它管，条条都问大模型又慢又贵。所以先过两道关。第一道是写死的代码，查来源查名单不对就跳过，不用问大模型。",
            "第二道才问大模型，它按提示词里写的规则判断要不要管。原型的面板上来源、触发、评估这几页，搂的就是这个东西。",
            "然后还有一件事情其实没有看到，这里面数据。",
            "要问的还得先在后台试着做，做成了才告诉你，做不成这个倒没发生。这个",
        ],
    ),
    (
        "3B9DC4F3F72E_seq000000",
        [
            "呃",
            "计算一下今天的一个总摄入",
            "今天的一个总摄入的话是一共",
            "五百克的鸡胸肉丸",
            "关于鸡胸肉丸的一个一个热量表的话，等会儿给你念出来",
            "然后还吃了一包",
            "呃，香辣土豆牛肉料理包",
            "先念一下这个料理包的一个营养成分表",
            "它是每一百克",
            "是四百九十五千焦",
            "蛋白质七点八克，脂肪五点八克，饱和脂肪一点九克，碳水化合物八点七克",
            "钠是",
            "五百七十三毫克",
        ],
    ),
    (
        "3B9DC4F3F72E_seq000001",
        [
            "刚才说的那个香辣土豆牛肉料理包是两百克。",
            "鸡肉丸子每一百克是四百六十千焦，蛋白质十七点五克。",
        ],
    ),
    (
        "3B9DC4F3F72E_seq000002",
        [
            "然后今天十点的时候去超，去便利店买了一包十块的烟",
            "当时是没带手表",
            "嗯",
        ],
    ),
    (
        "EEF1E11C596F_seq000000",
        [
            "你现在听得到我说话吗？",
            "刚才在楼下抽了一根烟，你顺便检查一下昨天的ASR有没有漏。",
        ],
    ),
    (
        "EEF1E11C596F_seq000001",
        [
            "是不是能够显示的合适的就只有这三条？",
            "然后，对我刚才想到，如果是对微信说话的话，是要显示出来的。",
            "嗯。",
        ],
    ),
    (
        "EEF1E11C596F_seq000004",
        [
            "补充一下昨天的三笔消费，鸡胸肉丸、料理包还有面条。",
            "面条是十五块二。",
        ],
    ),
    (
        "EEF1E11C596F_seq000005",
        [
            "五块二",
            "电话保时五十六点九",
            "这些你要记下来",
            "嗯",
        ],
    ),
    (
        "EEF1E11C596F_seq000008",
        [
            "然后我想一下",
            "要不要再加个目标的营养成分",
            "就比如说我去减肥，我的蛋白质要至少吃到我体重的一点二克",
            "一点五倍体重",
            "嗯",
        ],
    ),
    (
        "EEF1E11C596F_seq000009",
        [
            "碳水，啊，碳水还是多，操。",
            "你看下我减肥，我，我是八十五千克，然后昨天的一个碳水摄入是，",
            "碳水是一百八十一克，蛋白质一百二十六克，脂肪是二十六克。",
        ],
    ),
    (
        "28764DD5CE0A_seq000000",
        [
            "测试一下，现在是晚上十一点半，下楼丢个垃圾买包烟回来睡觉。",
        ],
    ),
]


# =============================================================================
# 反例夹具 2：报告第 0 节宽泛搜索命中的 16 段真实语音/外放候选（必须 0 误伤）
# =============================================================================
CANDIDATE_16_NON_LEAK_SEGMENTS: list[tuple[str, list[str]]] = [
    (
        "1FC556DD5E26_seq000135",
        [
            "你们C和4点1万点1期，参加时尚考核，挑战祖传bug。如果按照官方发布的这个品质来看的话，那么这一次，他可是奔着考核榜榜首的位置去的了。",
            "那么就来吧，规则很简单，先做青铜、白银、黄金九个基础bug，基础bug总共有12分，如果能够得10分，就可以挑战军。",
        ],
    ),
    (
        "1FC556DD5E26_seq000160",
        [
            "冉秋，准备去仙门考核。",
            "哥，还有点时间，我带你去集市逛逛吧，听说那里会卖考核题目。",
        ],
    ),
    (
        "1FC556DD5E26_seq000170",
        [
            "这位江行公子，也是学道宗考核的，仙人特意帮我们捎一程。",
            "告诉贾大人，前方有劫匪，注意警戒。",
        ],
    ),
    (
        "1FC556DD5E26_seq000172",
        [
            "入门前要参与三场考试，通过者入门，失败者自行离去。首场考核一周后结束。",
            "你和宋家小姐共同寻找第一场考核的线索，她对你的爱恋愈发深厚，她无你不可，无你不爱。",
        ],
    ),
    (
        "1FC556DD5E26_seq000178",
        [
            "若你能通过考核，修炼拳、掌、指法，视为最佳。",
        ],
    ),
    (
        "1FC556DD5E26_seq000180",
        [
            "他初试通过，无需测试灵根。",
            "第一关考核一周后结束。",
            "测试灵根的时候，红尘心术就已经种下了。",
            "蒋行，我给他测试灵根的时候，师妹没见他的眼神。",
        ],
    ),
    (
        "1FC556DD5E26_seq000181",
        [
            "太子殿下，这是今日参加仙门考核的名单，其中包括您要查的江行。",
        ],
    ),
    (
        "1FC556DD5E26_seq000187",
        [
            "我懂了，我懂了，这次考的是悟性，如果能从中领悟到突破的诀窍，便能通过考核。",
        ],
    ),
    (
        "1FC556DD5E26_seq000247",
        [
            "修仙客同楼上醉，烂客人谈东风起。沧海桑田，谁也别想影响我的道行。",
            "天目道友，让道友介绍，道宗，长生院，将士。",
        ],
    ),
    (
        "28479D590D05_seq000454",
        [
            "只要你成为外门弟子表现好，应该也很容易进入内门。进入内门之后，我能够让你爹爹亲自指点。",
        ],
    ),
    (
        "8065847EEA8C_seq000085_cand",
        [
            "啊，你稍微看一下我们Life Logger的一个项目，大概的一个数据架构。",
            "语音转文本就是A I S R过程嘛，我觉得它可能不是现在上。",
        ],
    ),
    (
        "86BEAD7F2B67_seq000131",
        [
            "不是考核",
            "这个我应该是知道的，这个是大厂，我们是跑的",
        ],
    ),
    (
        "901BE63F70EF_seq000054",
        [
            "面包店老板当场看呆了,前来面试的男生正在进行揉面团考核,才短短几秒,一整个面团就揉制完成,全程居然不到10秒,老板还以为是自己看花了眼,请他再揉",
        ],
    ),
    (
        "9443BA195952_seq000003",
        [
            "Mistral Small Transcribe 3",
            "在三十五种语言和五十种方言中",
            "实现了最先进的高精度语音转文字",
            "语音转文字价格",
        ],
    ),
    (
        "A700EE6DD12C_seq000152",
        [
            "你们都仔细听了，你们真以为长平地雷可以进行击倒开机，是为了他本身那个规则力量？长平看重的是他的运营气息。",
        ],
    ),
    (
        "EEF1E11C596F_seq000088",
        [
            "请仔细观察受损，销毁经费，负责调查",
            "别，你仔细听，他可能用了什么",
        ],
    ),
]


@pytest.mark.parametrize("hallucination", LEAK_HALLUCINATION_POSITIVES)
def test_scheme1_regex_matches_all_leak_variants_and_emits_warning(hallucination: str) -> None:
    """方案一：全部提示词泄漏/设备测试幻觉变体（含顿号变体）均命中正则，丢弃并产出 warning。"""
    assert mt._MOSS_META_HALLUCINATION_RE.search(hallucination)
    assert mt._looks_like_garbage(hallucination)

    rows = [
        {"start": 0.0, "end": 5.0, "text": f"[S01] {hallucination}", "speaker_id": "S01"},
        {"start": 5.0, "end": 6.5, "text": "[S01] 正常保留的一句话。", "speaker_id": "S01"},
    ]
    cleaned, warnings = ar.collapse_payload_repetition({"text": "", "segments": rows})
    assert [r["text"] for r in cleaned["segments"]] == ["[S01] 正常保留的一句话。"]
    expected_warn = f"prompt_leak_hallucination_dropped:{hallucination[:18]}"
    assert expected_warn in warnings


@pytest.mark.parametrize(
    "seg_id,lines",
    NORMAL_20_SPEECH_SEGMENTS + CANDIDATE_16_NON_LEAK_SEGMENTS,
)
def test_scheme1_regex_zero_false_positive_on_20_normal_and_16_candidates(
    seg_id: str, lines: list[str]
) -> None:
    """方案一：对报告 4.3 的 20 段正常人声 + 第 0 节的 16 段真实语音候选 0 误伤。"""
    for line in lines:
        assert not mt._MOSS_META_HALLUCINATION_RE.search(line), f"{seg_id} 误命中: {line}"
        assert not mt._looks_like_garbage(line), f"{seg_id} 被误判为 garbage: {line}"

    rows = [
        {"start": float(i), "end": float(i + 1), "text": f"[S01] {line}", "speaker_id": "S01"}
        for i, line in enumerate(lines)
    ]
    cleaned, warnings = ar.collapse_payload_repetition({"text": "", "segments": rows})
    assert len(cleaned["segments"]) == len(lines), f"{seg_id} 行数变化: {warnings}"
    assert not any(w.startswith("prompt_leak_hallucination_dropped:") for w in warnings)


def test_scheme2b_60s_first_pass_keeps_chinese_prompt_and_20s_slices_use_none(
    tmp_path: Path,
) -> None:
    """方案二-B：60s 首轮照旧带中文 moss_prompt(热词) 一字不变；切 20s 子片重转时一律 prompt=None。"""
    wav = tmp_path / "seg_60s.wav"
    _write_dummy_wav(wav, duration_seconds=60.098)

    class _FakeOutput:
        def __init__(self, text: str, segments: list[dict[str, Any]]) -> None:
            self.text = text
            self.segments = segments

    class _FakeMoss:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str | None]] = []

        def generate(
            self,
            audio_path: str,
            max_tokens: int = 4096,
            prompt: str | None = None,
            **kwargs: Any,
        ) -> _FakeOutput:
            self.calls.append((Path(audio_path).name, prompt))
            if Path(audio_path).name == "seg_60s.wav":
                # 触发复读切段回退（63 次单字重复）
                return _FakeOutput(
                    text="".join(f"[{i * 0.5:.2f}][S01]是[{(i + 1) * 0.5:.2f}]" for i in range(63)),
                    segments=[
                        {"start": i * 0.5, "end": (i + 1) * 0.5, "text": "[S01] 是", "speaker_id": "S01"}
                        for i in range(63)
                    ],
                )
            return _FakeOutput(
                text="[0.50][S01]子片正常转写[1.50]",
                segments=[{"start": 0.5, "end": 1.5, "text": "[S01] 子片正常转写", "speaker_id": "S01"}],
            )

    hotwords = "竹升面、OpenClaw、Life Logger"
    expected_60s_prompt = mt.moss_prompt(hotwords)
    fake_moss = _FakeMoss()

    out = ar._default_transcribe(
        wav=wav, outdir=tmp_path, hotwords=hotwords, moss=fake_moss, campplus=None
    )
    # 1) 60.098s 音频切 3 个 20s 子片（0.098s 尾片被跳过），总调用 1 + 3 = 4 次
    assert len(fake_moss.calls) == 4
    # 2) 60s 首轮的 prompt 与 moss_prompt(hotwords) 逐字完全一致
    assert fake_moss.calls[0] == ("seg_60s.wav", expected_60s_prompt)
    # 3) 20s 子片重转一律传 prompt=None
    for slice_name, slice_prompt in fake_moss.calls[1:]:
        assert slice_name.startswith("slice-00")
        assert slice_prompt is None
    assert "repetition_retranscribed_20s" in out["warnings"]


def test_slice_wav_file_skips_tail_shorter_than_one_second(tmp_path: Path) -> None:
    """_slice_wav_file 跳过 < 1.0 秒的极短尾片（如 60.098s 的 0.098s 尾片），保留 >= 1.0s 的尾片。"""
    wav_short_tail = tmp_path / "seg_60_098.wav"
    _write_dummy_wav(wav_short_tail, duration_seconds=60.098)
    tmpdir1, slices1 = ar._slice_wav_file(wav_short_tail, slice_seconds=20.0)
    try:
        assert [off for _, off in slices1] == [0.0, 20.0, 40.0]
    finally:
        import shutil
        shutil.rmtree(tmpdir1, ignore_errors=True)

    wav_long_tail = tmp_path / "seg_61_200.wav"
    _write_dummy_wav(wav_long_tail, duration_seconds=61.2)
    tmpdir2, slices2 = ar._slice_wav_file(wav_long_tail, slice_seconds=20.0)
    try:
        assert [off for _, off in slices2] == [0.0, 20.0, 40.0, 60.0]
    finally:
        import shutil
        shutil.rmtree(tmpdir2, ignore_errors=True)


def test_truncated_trailing_timestamp_bracket_is_dropped_as_empty() -> None:
    """当 MOSS 空时间戳死循环撞上 max_tokens=4096 在末尾截断出 `[2.0` 或单独 `[` 时，应作为空段丢弃。"""
    loop_prefix = "".join(f"[{i * 0.03:.2f}][S01][{(i + 1) * 0.03:.2f}]" for i in range(80))
    for tail in ("[2.0", "["):
        raw = loop_prefix + tail
        cleaned, warnings = ar.collapse_payload_repetition(
            {"text": raw, "segments": [{"start": 0.0, "end": 60.0, "text": raw}]}
        )
        assert cleaned["segments"] == []
        assert "empty_timestamp_segments_dropped:x1" in warnings
        text_only, _ = mt._split_text_and_speaker(raw)
        assert text_only == ""

