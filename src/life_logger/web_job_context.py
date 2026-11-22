"""JobStore 的 _JobContextMixin 方法，从 local_web.py 机械搬出，行为不变。"""
from __future__ import annotations

from life_logger.domain import TranscriptSegment
from life_logger.extract import RuleBasedExtractor
from life_logger.segment import split_utterances
from life_logger.timeutil import local_date as business_date
from life_logger.web_common import _atomic_json
from pathlib import Path
from typing import Any
import json


class _JobContextMixin:
    def review_path(self, job_id: str) -> Path:
        return self.job_path(job_id) / "review-decisions.json"

    def review_derived_events_path(self, job_id: str) -> Path:
        return self.job_path(job_id) / "review-derived-events.json"

    def event_corrections_path(self, job_id: str) -> Path:
        return self.job_path(job_id) / "event-corrections.json"

    def context_relation_reviews_path(self, job_id: str) -> Path:
        return self.job_path(job_id) / "context-relation-reviews.json"

    @property
    def startup_recovery_ids(self) -> tuple[str, ...]:
        return tuple(self._startup_recovery_ids)

    def _apply_event_corrections_unlocked(
        self,
        job_id: str,
        artifact_path: str,
        events: list[dict[str, Any]],
        *,
        include_rejected: bool = False,
    ) -> list[dict[str, Any]]:
        corrections = self._read_event_corrections_unlocked(job_id)
        output: list[dict[str, Any]] = []
        for source in events:
            if not isinstance(source, dict):
                continue
            event = dict(source)
            event_id = str(event.get("event_id") or "")
            correction = corrections.get(f"{artifact_path}::{event_id}")
            if isinstance(correction, dict):
                changes = correction.get("changes", {})
                if isinstance(changes, dict):
                    for key in ("title", "categories", "activity_type", "data"):
                        if key in changes:
                            event[key] = changes[key]
                    if isinstance(changes.get("time"), dict):
                        event["time"] = {
                            **(
                                event.get("time")
                                if isinstance(event.get("time"), dict)
                                else {}
                            ),
                            **changes["time"],
                        }
                    if "note" in changes:
                        evidence = (
                            dict(event.get("evidence"))
                            if isinstance(event.get("evidence"), dict)
                            else {}
                        )
                        evidence["correction_note"] = changes["note"]
                        event["evidence"] = evidence
                event["correction_status"] = (
                    "rejected" if correction.get("rejected") else "corrected"
                )
                event["corrected_at"] = correction.get("updated_at")
                if correction.get("rejected"):
                    event["review_status"] = "rejected"
                    if not include_rejected:
                        continue
                else:
                    event["review_status"] = "confirmed"
            output.append(event)
        return output

    def _rebuild_review_derived_events_unlocked(self, job_id: str) -> None:
        review_path = self.review_path(job_id)
        output_path = self.review_derived_events_path(job_id)
        if not review_path.exists():
            _atomic_json(output_path, {"schema_version": "0.1", "events": []})
            return

        payload = json.loads(review_path.read_text(encoding="utf-8"))
        decisions = list(payload.get("decisions", {}).values())
        extractor = RuleBasedExtractor()
        derived: list[dict[str, Any]] = []
        job_dir = self.job_path(job_id).resolve()
        artifact_cache: dict[str, list[dict[str, Any]]] = {}

        decision_index = {
            (
                str(item.get("artifact_path") or ""),
                str(item.get("segment_id") or ""),
            ): item
            for item in decisions
        }

        def reviewed_text_window(
            artifact_path: str, segment_id: str, records: list[dict[str, Any]]
        ) -> str:
            """Use nearby accepted transcript fragments so short ASR pieces keep
            their meaning.
            """
            positions = {
                str(item.get("segment_id")): index for index, item in enumerate(records)
            }
            current = positions.get(segment_id)
            if current is None:
                return ""
            current_record = records[current]
            try:
                current_start = float(current_record["start_offset_seconds"])
                current_end = float(current_record["end_offset_seconds"])
            except (KeyError, TypeError, ValueError):
                current_start = current_end = None
            selected: list[str] = []
            for index in range(max(0, current - 2), min(len(records), current + 3)):
                item = records[index]
                if item.get("recording_id") != records[current].get("recording_id"):
                    continue
                if current_start is not None and current_end is not None:
                    try:
                        item_start = float(item["start_offset_seconds"])
                        item_end = float(item["end_offset_seconds"])
                    except (KeyError, TypeError, ValueError):
                        continue
                    gap_seconds = max(
                        0.0,
                        current_start - item_end,
                        item_start - current_end,
                    )
                    if gap_seconds > 3.0:
                        continue
                decision = decision_index.get(
                    (artifact_path, str(item.get("segment_id") or ""))
                )
                if not decision:
                    continue
                if decision.get("source_decision") != "self":
                    continue
                corrected_text = str(decision.get("corrected_text") or "").strip()
                if (
                    decision.get("transcript_decision") == "incorrect"
                    and not corrected_text
                ):
                    continue
                text = corrected_text or str(item.get("text") or "").strip()
                if text and text not in selected:
                    selected.append(text)
            return " ".join(selected)

        for decision in decisions:
            if decision.get("source_decision") != "self":
                continue
            corrected_text = str(decision.get("corrected_text") or "").strip()
            if (
                decision.get("transcript_decision") == "incorrect"
                and not corrected_text
            ):
                continue
            artifact_path = str(decision.get("artifact_path") or "")
            segment_id = str(decision.get("segment_id") or "")
            if not artifact_path or not segment_id:
                continue
            source_path = (job_dir / artifact_path).resolve()
            if source_path != job_dir and job_dir not in source_path.parents:
                continue
            if not source_path.is_file():
                continue
            if artifact_path not in artifact_cache:
                source_records = json.loads(source_path.read_text(encoding="utf-8"))
                artifact_cache[artifact_path] = (
                    source_records if isinstance(source_records, list) else []
                )
            record = next(
                (
                    item
                    for item in artifact_cache[artifact_path]
                    if item.get("segment_id") == segment_id
                ),
                None,
            )
            if record is None:
                continue
            text = reviewed_text_window(
                artifact_path, segment_id, artifact_cache[artifact_path]
            )
            if not text:
                text = corrected_text or str(record.get("text") or "").strip()
            if not text:
                continue
            transcript = TranscriptSegment(
                segment_id=segment_id,
                recording_id=str(record.get("recording_id") or "reviewed_recording"),
                start_offset_seconds=float(record.get("start_offset_seconds") or 0.0),
                end_offset_seconds=float(record.get("end_offset_seconds") or 0.0),
                absolute_started_at=record.get("absolute_started_at"),
                absolute_ended_at=record.get("absolute_ended_at"),
                text=text,
                source="manual_source_review",
                language=str(record.get("language") or "zh"),
                transcription_confidence=None,
            )
            day = business_date(record.get("absolute_started_at"))
            if not day:
                continue
            target_path = source_path.parent / day / "events.json"
            if not target_path.is_file():
                continue
            target_relative = str(target_path.relative_to(job_dir))
            for utterance in split_utterances(transcript):
                for event in extractor.extract(utterance):
                    event_data = event.to_dict()
                    event_data["artifact_path"] = target_relative
                    event_data["evidence"] = {
                        **event_data.get("evidence", {}),
                        "review_source": "human",
                        "source_decision": "self",
                        "transcript_decision": decision.get("transcript_decision"),
                        "original_asr_text": str(record.get("text") or ""),
                        "corrected_text": corrected_text or None,
                    }
                    if (
                        decision.get("transcript_decision") == "correct"
                        or corrected_text
                    ):
                        event_data["review_status"] = "confirmed"
                    derived.append(event_data)

        _atomic_json(
            output_path,
            {"schema_version": "0.1", "events": derived},
        )
