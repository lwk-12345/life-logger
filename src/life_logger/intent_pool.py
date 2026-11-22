"""Persistent active intent tracking pool and cross-batch resolution.

Manages active-intents.json: tracking unresolved planned intents (e.g. '要去买瓶水')
and transitioning them to resolved/dismissed/expired across ingestion chunks.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
import unicodedata
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Mapping

from life_logger.domain import ALLOWED_DOMAINS, DomainType, map_category_to_domain

IntentStatus = Literal["pending", "resolved", "dismissed", "expired"]
ALLOWED_INTENT_STATUSES: set[str] = {"pending", "resolved", "dismissed", "expired"}

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_INTENT_POOL_PATH = PROJECT_ROOT / "output" / "active-intents.json"
DEFAULT_INTENT_TTL_HOURS = 48.0


def _parse_iso(ts: str) -> datetime | None:
    if not ts or not isinstance(ts, str):
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def get_default_intent_pool_path() -> Path:
    """Resolve the default persistent active-intents.json path."""
    env_path = os.environ.get("LIFE_LOGGER_INTENT_POOL_PATH")
    if env_path:
        return Path(env_path)
    return DEFAULT_INTENT_POOL_PATH


@dataclass
class Intent:
    """Schema model for an active intent item in active-intents.json."""

    intent_id: str
    text: str
    quote: str
    domain: str
    target_entity: str
    status: IntentStatus = "pending"
    created_at: str = ""
    resolved_at: str | None = None
    resolved_by_event_id: str | None = None
    resolution_note: str | None = None
    extracted_intent: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.extracted_intent:
            self.extracted_intent = self.text

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        if not d.get("extracted_intent"):
            d["extracted_intent"] = d["text"]
        return d


def validate_intent(intent: Mapping[str, Any]) -> None:
    """Validate that intent record complies with the active-intents schema."""
    required_fields = {
        "intent_id",
        "domain",
        "target_entity",
        "status",
        "created_at",
        "resolved_at",
        "resolved_by_event_id",
        "metadata",
    }
    missing = required_fields.difference(intent)
    if missing:
        raise ValueError(f"intent record missing required fields: {sorted(missing)}")

    # At least one of text or extracted_intent must be present
    if "text" not in intent and "extracted_intent" not in intent:
        raise ValueError("intent record missing required field: text")

    status = intent.get("status")
    if status not in ALLOWED_INTENT_STATUSES:
        allowed = sorted(ALLOWED_INTENT_STATUSES)
        raise ValueError(f"invalid intent status '{status}', must be one of {allowed}")

    domain = str(intent.get("domain") or "").capitalize()
    if domain not in ALLOWED_DOMAINS:
        allowed_domains = sorted(ALLOWED_DOMAINS)
        raise ValueError(
            f"invalid intent domain '{intent.get('domain')}', "
            f"must be in {allowed_domains}"
        )

    if not isinstance(intent.get("metadata"), dict):
        raise ValueError("intent metadata must be a dictionary")


def extract_target_entity(text: str, domain: str = "Finance") -> str:
    """Extract candidate target entity from an utterance or supplement text.

    Examples:
      - '要去买瓶水' -> '水'
      - '今天下午我还花了4.9元买了一瓶水' -> '水'
      - '打算买咖啡' -> '咖啡'
      - '记个待办核对报价' -> '核对报价'
    """
    cleaned = text.strip()
    if not cleaned:
        return "未指定"

    # Strip conversational prefixes and relative time indicators
    stripped = re.sub(
        r"^(?:今天(?:下午|晚上|早上|中午)?|明天|昨天|刚才|待会儿|一会儿|等下|稍后|现在|\d{1,2}[:：]\d{2})\s*",
        "",
        cleaned,
    )
    # Strip hotword and todo prefixes
    stripped = re.sub(
        r"^(?:记个待办|记待办|待办(?:事项)?|记一笔|记录一下|记账|提醒我|需要)\s*",
        "",
        stripped,
    )
    # Strip monetary amounts anywhere in the text
    # (both Arabic digits and Chinese numbers)
    stripped = re.sub(
        r"(?:(?:[零一二两三四五六七八九十百千\d.]+|\d+(?:\.\d+)?)\s*(?:元|块钱?|角|分|rmb|cny))\s*",
        "",
        stripped,
        flags=re.IGNORECASE,
    )
    # Strip pronouns, spend indicators, and common filler words
    stripped = re.sub(r"^(?:我(?:还|又)?(?:花了|用了|买了)?(?:去)?)\s*", "", stripped)
    stripped = re.sub(r"^(?:总共|一共|大概|大约|差不多|只|也就是)\s*", "", stripped)

    # Heuristic pattern matching for action + aspect + quantifier + target noun
    match = re.search(
        r"(?:要去|准备去|打算去|想去|计划去|去|准备|打算|想|计划|可能要|需要|记个待办|记待办|待办)?(?:买|喝|吃|购|点|消费|付款)?(?:了|过)?(?:的)?(?:一瓶|瓶|包|盒|个|杯|份|碗|斤|罐|支|件|张|一包|一个|一杯|一份|一碗|一斤|一罐)?([^\s，。！？,.!?]{1,20})",
        stripped,
    )
    if match and match.group(1):
        candidate = match.group(1).strip()
        candidate = re.sub(r"[，。！？,.!?]+$", "", candidate)
        candidate = re.sub(r"^的", "", candidate).strip()
        if candidate and candidate not in {
            "买",
            "去",
            "吃",
            "喝",
            "做",
            "了",
            "的",
            "一个",
            "一份",
            "一包",
            "一瓶",
        }:
            return candidate

    return cleaned


_PLANNED_KEYWORDS = (
    "要去",
    "打算去",
    "准备去",
    "想去",
    "计划去",
    "待会儿去",
    "一会儿去",
    "等下去",
    "准备买",
    "打算买",
    "要去买",
    "想买",
    "计划买",
    "可能要买",
    "准备吃",
    "打算吃",
    "要去吃",
    "想吃",
    "准备做",
    "打算做",
    "计划做",
    "记个待办",
    "待办",
)


def is_planned_utterance(text: str) -> bool:
    """Check if an utterance contains planned/intended action indicators."""
    cleaned = text.strip()
    return any(kw in cleaned for kw in _PLANNED_KEYWORDS)


def extract_planned_clause(text: str) -> str | None:
    """Extract planned clause from text if present.

    Example:
      '就就我外甥买了一包烟，要去买瓶水。' -> '要去买瓶水'
      '打算去买咖啡' -> '打算去买咖啡'
    """
    cleaned = text.strip()
    if not cleaned:
        return None
    clauses = re.split(r"[，。！？,.!?；;]+", cleaned)
    for clause in clauses:
        clause = clause.strip()
        if clause and is_planned_utterance(clause):
            return clause
    if is_planned_utterance(cleaned):
        return cleaned
    return None


def infer_intent_domain(text: str) -> DomainType:
    """Infer target domain from utterance text."""
    low = text.lower()
    if any(
        k in low
        for k in ("买", "花", "购", "付", "消费", "金额", "元", "费用", "转账", "结账")
    ):
        return "Finance"
    if any(
        k in low
        for k in ("吃", "喝", "餐", "饭", "外卖", "点心", "茶", "咖啡", "饮品", "奶茶")
    ):
        return "Diet"
    if any(
        k in low
        for k in (
            "待办",
            "做",
            "办",
            "开会",
            "核对",
            "整理",
            "提醒",
            "联系",
            "发邮件",
            "写",
            "提交",
        )
    ):
        return "Todos"
    if any(
        k in low
        for k in (
            "想",
            "点子",
            "灵感",
            "思路",
            "思考",
            "反思",
            "感悟",
            "笔记",
            "记录一下",
        )
    ):
        return "Insights"
    return "Finance"


def normalize_domain_name(domain: str) -> str:
    """Normalize domain string into canonical Capitalized domain name."""
    norm = domain.strip().lower()
    if norm in {"finance", "expense"}:
        return "Finance"
    if norm in {"diet", "food"}:
        return "Diet"
    if norm in {"todos", "todo"}:
        return "Todos"
    if norm in {"insights", "insight", "idea"}:
        return "Insights"
    cap = domain.strip().capitalize()
    return cap if cap in ALLOWED_DOMAINS else map_category_to_domain(norm)


def stable_intent_id(
    source_id: str, domain: str, target_entity: str, quote_or_text: str
) -> str:
    """Return a deterministic ID for one planned intent from one source.

    Whitespace and case differences in the domain/entity/quote do not change the
    identity; distinct utterance quotes from the same source remain distinct.
    """
    normalize = lambda value: " ".join(
        unicodedata.normalize("NFKC", str(value or "")).split()
    ).casefold()
    identity = {
        "source_id": normalize(source_id),
        "domain": normalize_domain_name(domain),
        "target_entity": normalize(target_entity),
        "quote_or_text": normalize(quote_or_text),
    }
    digest = hashlib.sha256(
        json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:24]
    return f"intent_{digest}"


class ActiveIntentPool:
    """Manages active-intents.json pool for cross-batch intent tracking."""

    def __init__(self, storage_path: Path | str | None = None) -> None:
        if storage_path is None:
            self.storage_path = get_default_intent_pool_path()
        else:
            path = Path(storage_path)
            if path.is_dir():
                path = path / "active-intents.json"
            self.storage_path = path

        self._lock = threading.Lock()
        self._intents: dict[str, dict[str, Any]] = {}
        self.load()

    def load(self) -> None:
        """Load state from storage_path if it exists."""
        with self._lock:
            if not self.storage_path.is_file():
                self._intents = {}
                return
            try:
                raw = json.loads(self.storage_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                self._intents = {}
                return

            items: list[dict[str, Any]] = []
            if isinstance(raw, list):
                items = [x for x in raw if isinstance(x, dict)]
            elif isinstance(raw, dict):
                intents_list = raw.get("intents")
                if isinstance(intents_list, list):
                    items = [x for x in intents_list if isinstance(x, dict)]

            loaded: dict[str, dict[str, Any]] = {}
            for item in items:
                intent_id = item.get("intent_id")
                if intent_id:
                    loaded[str(intent_id)] = item
            self._intents = loaded

    def save(self) -> None:
        """Atomically persist current intent pool state to storage_path."""
        with self._lock:
            self.storage_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "schema_version": "active-intents-v1",
                "updated_at": datetime.now(timezone.utc).astimezone().isoformat(),
                "intents": list(self._intents.values()),
            }
            tmp = self.storage_path.with_name(
                f".{self.storage_path.name}.{next(tempfile._get_candidate_names())}.tmp"
            )
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            tmp.replace(self.storage_path)

    def register_intent(
        self,
        text: str,
        domain: str,
        target_entity: str,
        timestamp: str,
        source_segment_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        quote: str | None = None,
        intent_id: str | None = None,
    ) -> dict[str, Any]:
        """Register intent with status 'pending' into active-intents.json.

        Adheres strictly to ADR 0004: records unconfirmed planned intentions
        without promoting them to formal expense events.
        """
        canonical_domain = normalize_domain_name(domain)
        meta = dict(metadata or {})
        if source_segment_id and "source_segment_id" not in meta:
            meta["source_segment_id"] = source_segment_id

        actual_quote = quote if quote is not None else text
        actual_entity = target_entity.strip() or extract_target_entity(
            text, domain=canonical_domain
        )
        created_time = timestamp or datetime.now(timezone.utc).astimezone().isoformat()
        existing_result: dict[str, Any] | None = None
        metadata_changed = False
        intent_data: dict[str, Any] | None = None

        # Check existing pending intent with identical text and timestamp
        # or segment to prevent duplicate
        with self._lock:
            if intent_id and intent_id in self._intents:
                existing = self._intents[intent_id]
                same_content = (
                    existing.get("text") == text
                    and existing.get("quote", existing.get("text")) == actual_quote
                    and normalize_domain_name(existing.get("domain", ""))
                    == canonical_domain
                    and (existing.get("target_entity") or "").strip()
                    == actual_entity
                    and existing.get("source_segment_id") == source_segment_id
                )
                if not same_content:
                    raise ValueError(
                        f"intent ID {intent_id!r} conflicts with existing intent content"
                    )
                existing_metadata = dict(existing.get("metadata") or {})
                for key, value in meta.items():
                    if value not in (None, "") and existing_metadata.get(key) in (None, ""):
                        existing_metadata[key] = value
                        metadata_changed = True
                if metadata_changed:
                    existing["metadata"] = existing_metadata
                    self._intents[intent_id] = existing
                existing_result = dict(existing)

            if existing_result is None:
                for existing in self._intents.values():
                    if existing.get("status") == "pending":
                        if (
                            source_segment_id
                            and existing.get("metadata", {}).get("source_segment_id")
                            == source_segment_id
                        ):
                            # Explicit stable IDs distinguish separate plans emitted
                            # from one transcript segment. Legacy callers without an
                            # ID retain the original segment-level deduplication.
                            if not intent_id or existing.get("intent_id") == intent_id:
                                existing_result = dict(existing)
                                break
                            continue
                        if (
                            existing.get("text") == text
                            and existing.get("created_at") == created_time
                            and existing.get("target_entity") == actual_entity
                        ):
                            existing_result = dict(existing)
                            break

            if existing_result is None:
                if not intent_id:
                    count = len(self._intents) + 1
                    digest_input = (
                        f"{canonical_domain}:{actual_entity}:{actual_quote}:{created_time}:{count}:{uuid.uuid4().hex[:6]}"
                    )
                    intent_hash = hashlib.sha256(digest_input.encode("utf-8")).hexdigest()[
                        :12
                    ]
                    intent_id = f"intent_{intent_hash}"

                intent_data = {
                    "intent_id": intent_id,
                    "text": text,
                    "quote": actual_quote,
                    "domain": canonical_domain,
                    "target_entity": actual_entity,
                    "status": "pending",
                    "created_at": created_time,
                    "resolved_at": None,
                    "resolved_by_event_id": None,
                    "resolution_note": None,
                    "extracted_intent": text,
                    "source_segment_id": source_segment_id,
                    "metadata": meta,
                }
                validate_intent(intent_data)
                self._intents[intent_id] = intent_data

        if existing_result is not None:
            if metadata_changed:
                self.save()
            return existing_result
        self.save()
        assert intent_data is not None
        return dict(intent_data)

    def find_matching_intents(
        self,
        domain: str,
        target_entity: str | None = None,
        status: str | None = "pending",
    ) -> list[dict[str, Any]]:
        """Find matching intents in the pool, filtered by domain, status, and entity."""
        canonical_domain = normalize_domain_name(domain)
        query_entity = (target_entity or "").strip().casefold()

        with self._lock:
            matches: list[dict[str, Any]] = []
            for item in self._intents.values():
                if status is not None and item.get("status") != status:
                    continue
                if normalize_domain_name(item.get("domain", "")) != canonical_domain:
                    continue

                if query_entity:
                    item_entity = (
                        str(item.get("target_entity") or "").strip().casefold()
                    )
                    item_text = str(item.get("text") or "").casefold()
                    item_quote = str(item.get("quote") or "").casefold()

                    is_match = (
                        query_entity == item_entity
                        or query_entity in item_entity
                        or item_entity in query_entity
                        or query_entity in item_quote
                        or query_entity in item_text
                    )
                    if not is_match:
                        continue

                matches.append(dict(item))
            return matches

    def expire_stale_intents(
        self,
        max_age_hours: float = DEFAULT_INTENT_TTL_HOURS,
        now_dt: datetime | None = None,
    ) -> list[str]:
        """Auto-expire pending intents older than max_age_hours to prevent unbounded zombie accumulation."""
        now = now_dt or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        expired_ids = []
        with self._lock:
            for intent_id, item in list(self._intents.items()):
                if item.get("status") != "pending":
                    continue
                meta = item.get("metadata") or {}
                future_date = meta.get("due_date") or meta.get("target_date")
                if future_date:
                    f_dt = _parse_iso(str(future_date))
                    if f_dt and f_dt > now:
                        continue
                created_dt = _parse_iso(str(item.get("created_at") or ""))
                if not created_dt:
                    continue
                age_seconds = (now - created_dt).total_seconds()
                if age_seconds > max_age_hours * 3600.0:
                    item["status"] = "expired"
                    item["resolved_at"] = now.isoformat()
                    item["resolution_note"] = f"auto_expired_ttl_{int(max_age_hours)}h"
                    expired_ids.append(intent_id)
        if expired_ids:
            self.save()
        return expired_ids

    def list_pending_intents(
        self,
        domain: str | None = None,
        auto_expire: bool = False,
    ) -> list[dict[str, Any]]:
        """List all pending intents in the pool, optionally filtered by domain."""
        if auto_expire:
            self.expire_stale_intents()
        with self._lock:
            intents = [
                dict(item)
                for item in self._intents.values()
                if item.get("status") == "pending"
            ]
            if domain:
                canonical = normalize_domain_name(domain)
                intents = [
                    item
                    for item in intents
                    if normalize_domain_name(item.get("domain", "")) == canonical
                ]
            return intents

    def dismiss_intent(
        self,
        intent_id: str,
        note: str | None = None,
        reason: str | None = None,
        dismissed_at: str | None = None,
    ) -> dict[str, Any]:
        """Transition intent status from 'pending' to 'dismissed'."""
        actual_note = note or reason
        with self._lock:
            if intent_id not in self._intents:
                raise KeyError(f"Intent ID not found: {intent_id}")
            intent = self._intents[intent_id]
            intent["status"] = "dismissed"
            intent["resolved_at"] = (
                dismissed_at or datetime.now(timezone.utc).astimezone().isoformat()
            )
            if actual_note:
                intent["dismiss_note"] = actual_note
                meta = intent.setdefault("metadata", {})
                meta["dismiss_note"] = actual_note
                meta["dismiss_reason"] = actual_note
            validate_intent(intent)
            res = dict(intent)
        self.save()
        return res

    def resolve_intent(
        self,
        intent_id: str,
        resolved_by_event_id: str,
        resolution_note: str | None = None,
        resolved_at: str | None = None,
    ) -> dict[str, Any]:
        """Transition intent status from 'pending' to 'resolved'."""
        if not resolved_by_event_id or not str(resolved_by_event_id).strip():
            raise ValueError("resolved_by_event_id cannot be empty")
        with self._lock:
            if intent_id not in self._intents:
                raise KeyError(f"Intent ID not found: {intent_id}")

            intent = self._intents[intent_id]
            if intent.get("status") == "resolved":
                return dict(intent)
            if intent.get("status") != "pending":
                raise ValueError(
                    f"intent is not in pending state (current: {intent.get('status')})"
                )
            intent["status"] = "resolved"
            intent["resolved_by_event_id"] = str(resolved_by_event_id).strip()
            intent["resolved_at"] = (
                resolved_at or datetime.now(timezone.utc).astimezone().isoformat()
            )
            intent["resolution_note"] = resolution_note
            if resolution_note:
                intent.setdefault("metadata", {})["resolution_note"] = resolution_note

            validate_intent(intent)
            res = dict(intent)

        self.save()
        return res

    def resolve_matching_intents(
        self,
        domain: str,
        target_entity: str,
        resolved_by_event_id: str,
        resolution_note: str | None = None,
    ) -> list[dict[str, Any]]:
        """Find and resolve all pending matching intents in one call."""
        pending = self.find_matching_intents(
            domain=domain, target_entity=target_entity, status="pending"
        )
        resolved: list[dict[str, Any]] = []
        for item in pending:
            resolved.append(
                self.resolve_intent(
                    item["intent_id"],
                    resolved_by_event_id=resolved_by_event_id,
                    resolution_note=resolution_note,
                )
            )
        return resolved

    def resolve_by_supplement(
        self,
        supplement: str | Mapping[str, Any],
        resolved_by_event_id: str | None = None,
        resolution_note: str | None = None,
        domain: str = "Finance",
    ) -> list[dict[str, Any]]:
        """Automatically resolve pending intents matching an evening/user supplement.

        Supports string inputs (e.g. '今天下午我还花了4.9元买了一瓶水')
        or full LifeEvent dictionaries.
        """
        if isinstance(supplement, str):
            text = supplement
            entity = extract_target_entity(text, domain=domain)
            event_id = (
                resolved_by_event_id
                or f"evt_supplement_{hashlib.sha256(text.encode()).hexdigest()[:8]}"
            )
            note = resolution_note or text
            return self.resolve_matching_intents(
                domain=domain,
                target_entity=entity,
                resolved_by_event_id=event_id,
                resolution_note=note,
            )

        # Dictionary / LifeEvent input
        event_dict = dict(supplement)
        event_id = resolved_by_event_id or str(event_dict.get("event_id") or "")
        cats = event_dict.get("categories") or ["expense"]
        event_domain = map_category_to_domain(cats[0])

        # Extract item entity from event payload
        data = event_dict.get("data") or {}
        cat_data = data.get(cats[0], {}) if isinstance(data, dict) else {}
        entity = cat_data.get("item") or event_dict.get("title") or ""
        if not entity:
            quote = (
                event_dict.get("evidence", {}).get("quote")
                or event_dict.get("title")
                or ""
            )
            entity = extract_target_entity(quote, domain=event_domain)

        note = resolution_note or f"Resolved by event {event_id}"
        return self.resolve_matching_intents(
            domain=event_domain,
            target_entity=entity,
            resolved_by_event_id=event_id,
            resolution_note=note,
        )

    def expire_intent(
        self,
        intent_id: str,
        note: str | None = None,
        expired_at: str | None = None,
    ) -> dict[str, Any]:
        """Expire a single intent by ID."""
        with self._lock:
            if intent_id not in self._intents:
                raise KeyError(f"Intent ID not found: {intent_id}")

            intent = self._intents[intent_id]
            intent["status"] = "expired"
            intent["resolved_at"] = (
                expired_at or datetime.now(timezone.utc).astimezone().isoformat()
            )
            intent["expire_note"] = note
            if note:
                intent.setdefault("metadata", {})["expire_note"] = note

            validate_intent(intent)
            res = dict(intent)

        self.save()
        return res

    def expire_intents(
        self,
        max_age_seconds: float = 7 * 86400,
        reference_time: str | None = None,
    ) -> list[dict[str, Any]]:
        """Expire pending intents older than max_age_seconds."""
        ref = (
            datetime.fromisoformat(reference_time)
            if reference_time
            else datetime.now(timezone.utc)
        )
        expired_list: list[dict[str, Any]] = []

        with self._lock:
            for item in self._intents.values():
                if item.get("status") == "pending":
                    created = item.get("created_at")
                    if created:
                        try:
                            dt = datetime.fromisoformat(created)
                            if dt.tzinfo is None:
                                dt = dt.replace(tzinfo=timezone.utc)
                            if ref.tzinfo is None:
                                ref = ref.replace(tzinfo=timezone.utc)
                            if (ref - dt).total_seconds() > max_age_seconds:
                                item["status"] = "expired"
                                item["resolved_at"] = ref.isoformat()
                                expired_list.append(dict(item))
                        except (ValueError, TypeError):
                            pass

        if expired_list:
            self.save()
        return expired_list

    def query_unresolved(self, domain: str | None = None) -> list[dict[str, Any]]:
        """Query all pending unresolved intents, optionally filtered by domain."""
        status = "pending"
        with self._lock:
            results = [
                dict(x) for x in self._intents.values() if x.get("status") == status
            ]
            if domain is not None:
                canonical = normalize_domain_name(domain)
                results = [
                    x
                    for x in results
                    if normalize_domain_name(x.get("domain", "")) == canonical
                ]
            return results

    def get_intent(self, intent_id: str) -> dict[str, Any] | None:
        """Fetch single intent by ID."""
        with self._lock:
            item = self._intents.get(intent_id)
            return dict(item) if item else None

    def list_intents(
        self,
        domain: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        """List intents matching domain and status filters."""
        with self._lock:
            results = list(self._intents.values())
            if status is not None:
                results = [x for x in results if x.get("status") == status]
            if domain is not None:
                canonical = normalize_domain_name(domain)
                results = [
                    x
                    for x in results
                    if normalize_domain_name(x.get("domain", "")) == canonical
                ]
            return [dict(x) for x in results]

    def clear(self) -> None:
        """Clear all intents from pool and save empty state."""
        with self._lock:
            self._intents.clear()
        self.save()

    def __len__(self) -> int:
        with self._lock:
            return len(self._intents)
