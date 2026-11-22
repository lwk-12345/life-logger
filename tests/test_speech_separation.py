import numpy as np

from life_logger.source_attribution import AttributionConfig
from life_logger.speech_separation import (
    apply_difficult_speech_separation,
    is_separation_candidate,
)


class FakeSeparator:
    name = "fake_separator"

    def __init__(self) -> None:
        self.calls = 0

    def separate(self, waveform, *, sample_rate: int):
        self.calls += 1
        samples = np.asarray(waveform, dtype=np.float32)
        return [samples * 0.8, samples * 0.2]


def _record(
    segment_id: str,
    label: str,
    *,
    review_required: bool,
    start: float = 0.0,
    end: float = 3.0,
    scores=None,
):
    return {
        "segment_id": segment_id,
        "start_offset_seconds": start,
        "end_offset_seconds": end,
        "source_label": label,
        "review_required": review_required,
        "voice_similarity": 0.36 if label == "unknown" else 0.8,
        "audio_content_scores": scores
        or {"speech": 0.7, "music": 0.1, "noise": 0.1, "stream": 0.1},
    }


def test_only_final_unknown_review_segments_are_separation_candidates():
    assert is_separation_candidate(
        _record("unknown", "unknown", review_required=True)
    )
    assert not is_separation_candidate(
        _record("self", "self", review_required=False)
    )
    assert not is_separation_candidate(
        _record("non-self", "non_self", review_required=False)
    )
    assert not is_separation_candidate(
        _record(
            "noise",
            "unknown",
            review_required=True,
            scores={"speech": 0.02, "music": 0.02, "noise": 0.94, "stream": 0.02},
        )
    )


def test_separation_rescues_unknown_when_one_source_matches_self():
    separator = FakeSeparator()
    audio_requests: list[str] = []

    def audio_provider(record):
        audio_requests.append(record["segment_id"])
        return np.ones(16_000 * 2, dtype=np.float32)

    def embedding_provider(sources):
        assert len(sources) == 2
        return [[1.0, 0.0], [0.0, 1.0]]

    records = [
        _record("already-self", "self", review_required=False),
        _record("mixed", "unknown", review_required=True),
    ]
    output, summary = apply_difficult_speech_separation(
        records,
        audio_provider=audio_provider,
        separator=separator,
        embedding_provider=embedding_provider,
        template_embedding=[1.0, 0.0],
        attribution_config=AttributionConfig(
            self_threshold=0.50,
            unknown_threshold=0.25,
        ),
    )

    assert audio_requests == ["mixed"]
    assert separator.calls == 1
    assert output[0]["source_label"] == "self"
    rescued = output[1]
    assert rescued["source_label"] == "self"
    assert rescued["review_required"] is False
    assert rescued["decision_reason"] == "speech_separation_self"
    assert rescued["speech_separation_selected_source"] == "A"
    assert rescued["voice_similarity"] == 1.0
    assert rescued["_speaker_embedding"] == [1.0, 0.0]
    assert summary["attempted"] == 1
    assert summary["rescued_self"] == 1
    assert summary["retained_unknown"] == 0


def test_separation_keeps_unknown_when_neither_source_is_confident_self():
    separator = FakeSeparator()

    output, summary = apply_difficult_speech_separation(
        [_record("mixed", "unknown", review_required=True)],
        audio_provider=lambda _record: np.ones(16_000, dtype=np.float32),
        separator=separator,
        embedding_provider=lambda _sources: [[0.35, 0.94], [0.2, 0.98]],
        template_embedding=[1.0, 0.0],
        attribution_config=AttributionConfig(
            self_threshold=0.50,
            unknown_threshold=0.25,
        ),
    )

    record = output[0]
    assert record["source_label"] == "unknown"
    assert record["review_required"] is True
    assert record["speech_separation_attempted"] is True
    assert record["speech_separation_decision_reason"] == "no_separated_self_match"
    assert len(record["speech_separation_sources"]) == 2
    assert summary["rescued_self"] == 0
    assert summary["retained_unknown"] == 1


def test_separation_error_does_not_drop_or_finalize_fragment():
    class BrokenSeparator:
        name = "broken"

        def separate(self, waveform, *, sample_rate: int):
            raise RuntimeError("separation failed")

    output, summary = apply_difficult_speech_separation(
        [_record("mixed", "unknown", review_required=True)],
        audio_provider=lambda _record: np.ones(16_000, dtype=np.float32),
        separator=BrokenSeparator(),
        embedding_provider=lambda _sources: [],
        template_embedding=[1.0, 0.0],
        attribution_config=AttributionConfig(
            self_threshold=0.50,
            unknown_threshold=0.25,
        ),
    )

    record = output[0]
    assert record["source_label"] == "unknown"
    assert record["review_required"] is True
    assert record["speech_separation_decision_reason"] == (
        "separation_error:RuntimeError"
    )
    assert summary["errors"] == 1
    assert summary["retained_unknown"] == 1
