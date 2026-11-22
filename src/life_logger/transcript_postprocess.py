"""Conservative local post-processing for high-impact ASR confusions."""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class TranscriptPostprocessResult:
    text: str
    corrections: tuple[str, ...] = ()
    review_reasons: tuple[str, ...] = ()

    @property
    def needs_review(self) -> bool:
        return bool(self.review_reasons)


_CIGARETTE_CONTEXT = re.compile(
    r"(?:买|花\s*\d|花了|拿(?:一|个|包|那)|一包|包烟|香烟|换口味|烟)"
)
_CIGARETTE_CONFUSION = re.compile(r"短(?:剧|致|支)的?乐土")
_CIGARETTE_PARTIAL_CONFUSION = re.compile(r"短(?:剧|致)的?乐途")
_IMPLAUSIBLE_DRINK_LITERS = re.compile(
    r"(?:\d{2,4}|[零一二两三四五六七八九十百千万]{3,})\s*升"
)
_DRINK_CONTEXT = re.compile(r"(?:喝|牛奶|饮料|水|咖啡|茶|奶)")


def postprocess_transcript_text(text: str) -> TranscriptPostprocessResult:
    """Fix high-confidence local confusions; flag unsafe quantity guesses."""

    raw = str(text or "")
    corrected = raw
    corrections: list[str] = []
    review_reasons: list[str] = []

    if _CIGARETTE_CONTEXT.search(corrected):
        updated = _CIGARETTE_CONFUSION.sub("短支的乐途", corrected)
        updated = _CIGARETTE_PARTIAL_CONFUSION.sub("短支的乐途", updated)
        if updated != corrected:
            corrections.append("cigarette_term:短支的乐途")
            corrected = updated

    volume_probe = corrected.replace("毫升", "")
    if _DRINK_CONTEXT.search(corrected) and _IMPLAUSIBLE_DRINK_LITERS.search(
        volume_probe
    ):
        review_reasons.append("implausible_drink_volume_unit")

    return TranscriptPostprocessResult(
        text=corrected,
        corrections=tuple(corrections),
        review_reasons=tuple(review_reasons),
    )
