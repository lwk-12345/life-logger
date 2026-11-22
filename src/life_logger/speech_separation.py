"""Targeted two-speaker separation for difficult source-attribution segments."""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from math import gcd, isfinite
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence

from life_logger.source_attribution import (
    AttributionConfig,
    classify_similarity,
    cosine_similarity,
)

SEPARATION_POLICY_VERSION = "targeted-two-speaker-v1"


@dataclass(frozen=True)
class SpeechSeparationConfig:
    """Limits for expensive separation attempts."""

    min_duration_seconds: float = 0.8
    max_duration_seconds: float = 20.0
    skip_noise_probability: float = 0.85

    def __post_init__(self) -> None:
        if (
            not isfinite(float(self.min_duration_seconds))
            or self.min_duration_seconds <= 0
        ):
            raise ValueError("min_duration_seconds must be positive")
        if (
            not isfinite(float(self.max_duration_seconds))
            or self.max_duration_seconds < self.min_duration_seconds
        ):
            raise ValueError("max_duration_seconds must cover minimum")
        if not 0.0 <= self.skip_noise_probability <= 1.0:
            raise ValueError("skip_noise_probability must be between 0 and 1")


class TwoSpeakerSeparator(Protocol):
    name: str

    def separate(
        self,
        waveform: Any,
        *,
        sample_rate: int,
    ) -> list[Any]:
        """Return two isolated speaker waveforms."""


def is_separation_candidate(
    record: Mapping[str, Any],
    config: SpeechSeparationConfig | None = None,
) -> bool:
    """Only final unknown/review-band speech is eligible for separation."""

    config = config or SpeechSeparationConfig()
    if record.get("source_label") != "unknown":
        return False
    if record.get("review_required") is False:
        return False
    start = float(record.get("start_offset_seconds", 0.0))
    end = float(record.get("end_offset_seconds", start))
    duration = max(0.0, end - start)
    if not config.min_duration_seconds <= duration <= config.max_duration_seconds:
        return False

    scores = record.get("audio_content_scores")
    if isinstance(scores, Mapping):
        try:
            noise = float(scores.get("noise", 0.0))
            speech = float(scores.get("speech", 0.0))
            stream = float(scores.get("stream", 0.0))
        except (TypeError, ValueError):
            noise = speech = stream = 0.0
        if (
            noise >= config.skip_noise_probability
            and speech + stream < 0.15
        ):
            return False
    return True


def _resample(samples: Any, input_rate: int, output_rate: int) -> Any:
    import numpy as np

    waveform = np.asarray(samples, dtype=np.float32).reshape(-1)
    if input_rate == output_rate:
        return waveform
    try:
        from scipy.signal import resample_poly
    except ImportError as error:  # pragma: no cover - worker dependency
        raise RuntimeError("speech separation resampling requires scipy") from error
    factor = gcd(int(input_rate), int(output_rate))
    return resample_poly(
        waveform,
        output_rate // factor,
        input_rate // factor,
    ).astype(np.float32)


class ModelScopeTwoSpeakerSeparator:
    """Run a local ModelScope MossFormer-style two-speaker separation model."""

    name = "modelscope_speech_separation"

    def __init__(self, model_path: Path | str):
        path = Path(model_path).expanduser().resolve()
        if not path.is_dir():
            raise RuntimeError(f"speech separation model not found: {path}")
        self.model_path = path
        self._pipeline: Any | None = None

    def _load_pipeline(self) -> Any:
        if self._pipeline is None:
            try:
                from modelscope.pipelines import pipeline
                from modelscope.utils.constant import Tasks
            except ImportError as error:  # pragma: no cover - worker dependency
                raise RuntimeError(
                    "speech separation requires the ModelScope worker environment"
                ) from error
            self._pipeline = pipeline(
                task=Tasks.speech_separation,
                model=str(self.model_path),
                device="cpu",
            )
        return self._pipeline

    def separate(
        self,
        waveform: Any,
        *,
        sample_rate: int,
    ) -> list[Any]:
        import numpy as np

        separated_rate = 8_000
        mixture = _resample(waveform, sample_rate, separated_rate)
        if mixture.size == 0:
            return []
        peak = float(np.max(np.abs(mixture)))
        if peak > 1.0:
            mixture = mixture / peak

        try:
            import soundfile as sf
        except ImportError as error:  # pragma: no cover - worker dependency
            raise RuntimeError("speech separation requires soundfile") from error

        with tempfile.TemporaryDirectory(prefix="life-logger-separation-") as temp:
            path = Path(temp) / "mixture.wav"
            sf.write(path, mixture, separated_rate, subtype="PCM_16")
            result = self._load_pipeline()(str(path))

        if not isinstance(result, Mapping):
            raise RuntimeError("speech separation returned an invalid result")
        payload = result.get("output_pcm_list")
        if not isinstance(payload, Sequence) or isinstance(
            payload, (bytes, bytearray, str)
        ):
            raise RuntimeError("speech separation returned no speaker sources")

        sources: list[Any] = []
        for item in payload:
            if isinstance(item, (bytes, bytearray)):
                source = np.frombuffer(item, dtype=np.int16).astype(np.float32)
                source /= 32768.0
            else:
                source = np.asarray(item, dtype=np.float32).reshape(-1)
            if source.size:
                sources.append(_resample(source, separated_rate, sample_rate))
        return sources[:2]


def apply_difficult_speech_separation(
    records: Sequence[Mapping[str, Any]],
    *,
    audio_provider: Callable[[Mapping[str, Any]], Any],
    separator: TwoSpeakerSeparator,
    embedding_provider: Callable[[Sequence[Any]], Sequence[Sequence[float]]],
    template_embedding: Sequence[float],
    attribution_config: AttributionConfig,
    sample_rate: int = 16_000,
    config: SpeechSeparationConfig | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Rescue final unknown segments by separating two speakers then rerunning CAM++."""

    config = config or SpeechSeparationConfig()
    output = [dict(record) for record in records]
    summary = {
        "policy_version": SEPARATION_POLICY_VERSION,
        "backend": getattr(separator, "name", type(separator).__name__),
        "eligible": 0,
        "attempted": 0,
        "rescued_self": 0,
        "retained_unknown": 0,
        "errors": 0,
    }

    for record in output:
        record["speech_separation_candidate"] = is_separation_candidate(
            record, config
        )
        record["speech_separation_attempted"] = False
        if not record["speech_separation_candidate"]:
            continue
        summary["eligible"] += 1
        record["speech_separation_attempted"] = True
        record["speech_separation_policy"] = SEPARATION_POLICY_VERSION
        record["speech_separation_backend"] = summary["backend"]
        summary["attempted"] += 1

        try:
            mixture = audio_provider(record)
            sources = separator.separate(mixture, sample_rate=sample_rate)
            if len(sources) < 2:
                record["speech_separation_decision_reason"] = (
                    "fewer_than_two_sources"
                )
                summary["retained_unknown"] += 1
                continue

            embeddings = list(embedding_provider(sources[:2]))
            if len(embeddings) != 2:
                raise RuntimeError(
                    "speaker model returned an unexpected separated-source count"
                )

            rows: list[dict[str, Any]] = []
            for index, embedding in enumerate(embeddings):
                score = cosine_similarity(template_embedding, embedding)
                decision = classify_similarity(score, attribution_config)
                rows.append(
                    {
                        "source": "A" if index == 0 else "B",
                        "voice_similarity": round(score, 6),
                        "source_label": decision.label,
                        "decision_reason": decision.reason,
                    }
                )
            record["speech_separation_sources"] = rows
            record["speech_separation_source_count"] = len(rows)

            self_indexes = [
                index
                for index, row in enumerate(rows)
                if row["source_label"] == "self"
            ]
            if self_indexes:
                selected = max(
                    self_indexes,
                    key=lambda index: float(rows[index]["voice_similarity"]),
                )
                record["separation_original_voice_similarity"] = record.get(
                    "voice_similarity"
                )
                record["source_label"] = "self"
                record["review_required"] = False
                record["voice_similarity"] = rows[selected]["voice_similarity"]
                record["decision_reason"] = "speech_separation_self"
                record["speech_separation_decision_reason"] = (
                    "separated_source_self_match"
                )
                record["speech_separation_selected_source"] = rows[selected][
                    "source"
                ]
                record["_speaker_embedding"] = list(embeddings[selected])
                summary["rescued_self"] += 1
            else:
                record["speech_separation_decision_reason"] = (
                    "no_separated_self_match"
                )
                summary["retained_unknown"] += 1
        except Exception as error:  # one difficult fragment must not fail the day
            record["speech_separation_decision_reason"] = (
                f"separation_error:{type(error).__name__}"
            )
            record["speech_separation_error"] = str(error)[:500]
            summary["errors"] += 1
            summary["retained_unknown"] += 1

    return output, summary
