"""Stage-1 local pre-filter and multi-track candidate extractor for OpenClaw.

Preserves cognitive events (decision restraints, hypothetical reasoning, curiosity questions)
alongside concrete facts, compressing raw transcripts into high-density candidates.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import replace
from typing import Any

from life_logger.domain import EventTime, LifeEvent, TranscriptSegment, validate_event
from life_logger.extract import RuleBasedExtractor, _purchase_item, _title

_DECISION_NEGATION_MARKERS = (
    "没买",
    "不买了",
    "算了不买",
    "太贵了没买",
    "不要了",
    "放弃了",
    "取消了",
    "没去",
    "不去了",
    "算了不去",
    "先不买",
    "先不去",
    "买不起",
)

_HYPOTHETICAL_MARKERS = (
    "如果",
    "假如",
    "要是",
    "假设",
    "按这个逻辑",
    "方案讨论",
    "假如下次",
    "如果后面",
    "如果说",
    "打个比方",
    "举个例子",
)

_QUESTION_MARKERS = (
    "为什么",
    "怎么做",
    "怎么弄",
    "如何",
    "多少钱",
    "能不能",
    "可不可以",
    "行不行",
    "有没有",
    "是不是",
)


def _candidate_id(segment: TranscriptSegment, candidate_type: str, title: str) -> str:
    raw = "|".join(("candidate", segment.recording_id, segment.segment_id, candidate_type, title))
    return f"evt_cand_{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:12]}"


class CandidateExtractor:
    """Stage-1 local extractor emitting multi-track life event candidates for OpenClaw."""

    def __init__(self, rule_extractor: RuleBasedExtractor | None = None) -> None:
        self.rule_extractor = rule_extractor or RuleBasedExtractor()

    def extract(self, segment: TranscriptSegment) -> list[LifeEvent]:
        raw_text = segment.text.strip()
        if not raw_text:
            return []

        # 1. Prioritize decision restraint / canceled plans (没买 / 没去 / 算了)
        # Prevents "太贵了没买" from being falsely treated as a completed purchase fact
        text = raw_text
        if any(marker in text for marker in _DECISION_NEGATION_MARKERS):
            is_purchase = any(
                k in text for k in ("买", "花", "元", "块", "付", "消费", "烟", "耳机", "手机", "电脑", "衣服")
            )
            category = "expense" if is_purchase else "routine"
            activity_type = "decision_saving" if is_purchase else "plan_cancelled"
            title = _title(text)
            data: dict[str, Any] = {
                "decision": {
                    "action": "rejected_purchase" if is_purchase else "cancelled_plan",
                    "state": "negated",
                    "text": text,
                },
                "openclaw": {
                    "candidate_type": "decision_candidate",
                    "subtype": "saving_restraint" if is_purchase else "plan_cancelled",
                    "state": "negated",
                    "category": category,
                    "quote": text,
                    "confidence": 0.92,
                },
            }
            if is_purchase:
                item = _purchase_item(text, title)
                data["expense"] = {
                    "amount": None,
                    "currency": "CNY",
                    "item": item,
                }

            event = LifeEvent(
                event_id=_candidate_id(segment, "decision", title),
                categories=[category],  # type: ignore[list-item]
                activity_type=activity_type,
                title=title,
                time=EventTime(
                    absolute=segment.absolute_started_at,
                    relative_seconds=segment.start_offset_seconds,
                    precision="exact" if segment.absolute_started_at else "relative",
                ),
                data=data,
                capture_mode="passive",
                source_type="audio",
                confidence=0.92,
                evidence={
                    "recording_id": segment.recording_id,
                    "segment_id": segment.segment_id,
                    "start_offset_seconds": segment.start_offset_seconds,
                    "end_offset_seconds": segment.end_offset_seconds,
                    "quote": text,
                    "candidate_type": "decision_candidate",
                },
                review_status="unreviewed",
            )
            validate_event(event.to_dict())
            return [event]

        # 2. Run the baseline fact extractor for confirmed facts
        baseline_events = self.rule_extractor.extract(segment)
        if baseline_events:
            annotated: list[LifeEvent] = []
            for event in baseline_events:
                category = event.categories[0] if event.categories else "routine"
                if category == "todo":
                    candidate_type = "todo_candidate"
                    state = "planned"
                elif category == "idea":
                    if event.activity_type == "curiosity_question":
                        candidate_type = "question_candidate"
                        state = "question"
                    else:
                        candidate_type = "reasoning_candidate"
                        state = "hypothetical"
                else:
                    candidate_type = "fact_candidate"
                    state = "completed"
                data = dict(event.data)
                data["openclaw"] = {
                    "candidate_type": candidate_type,
                    "state": state,
                    "category": category,
                    "quote": segment.text,
                    "confidence": event.confidence,
                }
                annotated.append(replace(event, data=data))
            return annotated

        # 3. Check for hypothetical reasoning / brainstorming (如果 / 假如 / 方案讨论)
        if any(marker in text for marker in _HYPOTHETICAL_MARKERS):
            title = _title(text)
            data = {
                "idea": {"kind": "hypothetical_reasoning", "future": True},
                "reasoning": {"state": "hypothetical", "hypothesis": text},
                "openclaw": {
                    "candidate_type": "reasoning_candidate",
                    "state": "hypothetical",
                    "category": "idea",
                    "quote": text,
                    "confidence": 0.88,
                },
            }
            event = LifeEvent(
                event_id=_candidate_id(segment, "reasoning", title),
                categories=["idea"],
                activity_type="hypothetical_reasoning",
                title=title,
                time=EventTime(
                    absolute=segment.absolute_started_at,
                    relative_seconds=segment.start_offset_seconds,
                    precision="exact" if segment.absolute_started_at else "relative",
                ),
                data=data,
                capture_mode="passive",
                source_type="audio",
                confidence=0.90,
                evidence={
                    "recording_id": segment.recording_id,
                    "segment_id": segment.segment_id,
                    "start_offset_seconds": segment.start_offset_seconds,
                    "end_offset_seconds": segment.end_offset_seconds,
                    "quote": text,
                    "candidate_type": "reasoning_candidate",
                },
                review_status="unreviewed",
            )
            validate_event(event.to_dict())
            return [event]

        # 4. Check for open questions / curiosity (疑问句 / 探究点)
        is_question = (
            text.endswith(("？", "?", "吗", "呢"))
            or any(marker in text for marker in _QUESTION_MARKERS)
        )
        if is_question and len(text) >= 4:
            title = _title(text)
            data = {
                "idea": {"kind": "curiosity_question"},
                "question": {"state": "question", "query": text},
                "openclaw": {
                    "candidate_type": "question_candidate",
                    "state": "question",
                    "category": "idea",
                    "quote": text,
                    "confidence": 0.85,
                },
            }
            event = LifeEvent(
                event_id=_candidate_id(segment, "question", title),
                categories=["idea"],
                activity_type="curiosity_question",
                title=title,
                time=EventTime(
                    absolute=segment.absolute_started_at,
                    relative_seconds=segment.start_offset_seconds,
                    precision="exact" if segment.absolute_started_at else "relative",
                ),
                data=data,
                capture_mode="passive",
                source_type="audio",
                confidence=0.90,
                evidence={
                    "recording_id": segment.recording_id,
                    "segment_id": segment.segment_id,
                    "start_offset_seconds": segment.start_offset_seconds,
                    "end_offset_seconds": segment.end_offset_seconds,
                    "quote": text,
                    "candidate_type": "question_candidate",
                },
                review_status="unreviewed",
            )
            validate_event(event.to_dict())
            return [event]

        # Pure chitchat or background without actionable signal
        return []
