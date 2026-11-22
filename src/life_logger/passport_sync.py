"""AI Passport 240x280 LVGL screen display and synchronization subsystem.

Implements the Stage 7 roadmap requirements for Life Logger / OpenClaw -> AI Passport:
- Card Schema contracts: TODO_CARD, SUMMARY_CARD, STATUS_CARD.
- Card extraction from daily summaries, task states, and OpenClaw unified tasks.
- JSON serialization and compact binary packet framing protocol (MTU 185 safe for BLE/Wi-Fi).
- Middle-button (中键) paging navigation and atomic offline caching.
"""

from __future__ import annotations

import hashlib
import json
import re
import struct
import zlib
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

# ---------------------------------------------------------------------------
# Constants & Specifications
# ---------------------------------------------------------------------------

CARD_SCHEMA_VERSION = "passport-card-v1"
BINARY_PROTOCOL_MAGIC = b"APC1"  # AirPassport Card v1
BINARY_HEADER_FORMAT = ">4sBHHHI"  # magic(4s), flags(B), seq(H), total(H), length(H), crc32(I)
BINARY_HEADER_SIZE = struct.calcsize(BINARY_HEADER_FORMAT)  # 15 bytes
MAX_BLE_CHUNK_SIZE = 160  # fits comfortably within MTU 185 (15B header + 160B payload = 175B)

# Screen constraints for AirPassport hardware
SCREEN_WIDTH = 240
SCREEN_HEIGHT = 280
LVGL_MAX_TITLE_CHARS = 24
LVGL_MAX_KEY_EVENTS = 4

# Flags
FLAG_IS_LAST_CHUNK = 0x01
FLAG_IS_COMPRESSED = 0x02

SAFE_ID_RE = re.compile(r"[^A-Za-z0-9_.-]+")


class CardType(str, Enum):
    TODO = "TODO_CARD"
    SUMMARY = "SUMMARY_CARD"
    STATUS = "STATUS_CARD"


VALID_CARD_TYPES = {CardType.TODO.value, CardType.SUMMARY.value, CardType.STATUS.value}
VALID_TODO_STATUSES = {"pending", "in_progress", "completed", "dismissed", "blocked"}
VALID_PRIORITIES = {"P0", "P1", "P2", "P3"}
VALID_CONNECTION_STATUSES = {"ready", "connected", "syncing", "disconnected"}


def safe_id(value: str, fallback: str = "card") -> str:
    cleaned = SAFE_ID_RE.sub("_", str(value or "")).strip("._")
    return cleaned[:64] or fallback


def _iso_now(timezone_name: str = "Asia/Shanghai") -> str:
    try:
        return datetime.now(tz=ZoneInfo(timezone_name)).isoformat()
    except Exception:
        return datetime.now().astimezone().isoformat()


def format_duration(seconds: float) -> str:
    """Format duration in seconds to a compact readable string."""
    sec = max(0, int(round(seconds)))
    hours = sec // 3600
    minutes = (sec % 3600) // 60
    remaining_sec = sec % 60
    if hours > 0:
        return f"{hours}小时{minutes}分"
    if minutes > 0:
        if remaining_sec > 0:
            return f"{minutes}分{remaining_sec}秒"
        return f"{minutes}分"
    return f"{remaining_sec}秒"


def format_currency(amount: float) -> str:
    """Format CNY amount with 2 decimal places."""
    return f"¥{amount:.2f}"


# ---------------------------------------------------------------------------
# Card Data Models
# ---------------------------------------------------------------------------


@dataclass
class PassportCard:
    """Base class for all AI Passport screen cards."""

    card_id: str
    title: str = ""
    card_type: str = ""
    updated_at: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def validate(self) -> None:
        if not self.card_id or not isinstance(self.card_id, str):
            raise ValueError("card_id must be a non-empty string")
        if self.card_type not in VALID_CARD_TYPES:
            raise ValueError(f"invalid card_type: {self.card_type}")
        if not self.title or not isinstance(self.title, str):
            raise ValueError("title must be a non-empty string")
        if not self.updated_at or not isinstance(self.updated_at, str):
            raise ValueError("updated_at must be a non-empty ISO timestamp string")

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)

    def to_lvgl_dict(self) -> dict[str, Any]:
        """Convert card data into an LVGL 240x280 display-ready structure."""
        return {
            "card_id": self.card_id,
            "card_type": self.card_type,
            "title": self.title[:LVGL_MAX_TITLE_CHARS],
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PassportCard:
        card_type = str(data.get("card_type") or "")
        if card_type == CardType.TODO.value:
            return TodoCard.from_dict(data)
        if card_type == CardType.SUMMARY.value:
            return SummaryCard.from_dict(data)
        if card_type == CardType.STATUS.value:
            return StatusCard.from_dict(data)
        raise ValueError(f"Unknown card_type: {card_type}")


@dataclass
class TodoCard(PassportCard):
    """Card representing a task / todo item for LVGL screen display."""

    task_key: str = ""
    status: str = "pending"
    priority: str = "P2"
    due_time: str | None = None
    project: str | None = None
    tags: list[str] = field(default_factory=list)
    source_event_id: str | None = None

    def __post_init__(self) -> None:
        if not self.card_type:
            self.card_type = CardType.TODO.value
        if not self.updated_at:
            self.updated_at = _iso_now()

    def validate(self) -> None:
        super().validate()
        if not self.task_key or not isinstance(self.task_key, str):
            raise ValueError("task_key must be a non-empty string")
        if self.status not in VALID_TODO_STATUSES:
            raise ValueError(f"status must be one of {sorted(VALID_TODO_STATUSES)}")
        if self.priority not in VALID_PRIORITIES:
            raise ValueError(f"priority must be one of {sorted(VALID_PRIORITIES)}")

    def to_openclaw_task(self) -> dict[str, Any]:
        """Export to OpenClaw unified-task-v1 contract."""
        return {
            "schema_version": "unified-task-v1",
            "id": self.task_key,
            "title": self.title,
            "status": self.status,
            "priority": self.priority,
            "project_id": self.project,
            "tags": list(self.tags),
            "assignee": None,
            "source": {
                "system": "life_logger",
                "event_id": self.source_event_id,
            }
            if self.source_event_id
            else None,
            "created_at": self.updated_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_openclaw_task(cls, task: Mapping[str, Any]) -> TodoCard:
        """Construct a TodoCard from an OpenClaw unified-task-v1 dictionary."""
        task_id = str(task.get("id") or "").strip()
        if not task_id:
            raise ValueError("OpenClaw task must contain an 'id'")
        title = str(task.get("title") or "未命名待办").strip()
        status = str(task.get("status") or "pending").strip()
        if status not in VALID_TODO_STATUSES:
            status = "pending"
        priority = str(task.get("priority") or "P2").strip()
        if priority not in VALID_PRIORITIES:
            priority = "P2"
        updated_at = str(task.get("updated_at") or task.get("created_at") or _iso_now())
        project = task.get("project_id")
        tags = [str(t) for t in task.get("tags") or []]
        source = task.get("source") if isinstance(task.get("source"), dict) else {}
        source_event_id = source.get("event_id")

        return cls(
            card_id=f"card_todo_{safe_id(task_id)}",
            card_type=CardType.TODO.value,
            title=title,
            updated_at=updated_at,
            task_key=task_id,
            status=status,
            priority=priority,
            due_time=str(task.get("due_time")) if task.get("due_time") else None,
            project=str(project) if project else None,
            tags=tags,
            source_event_id=str(source_event_id) if source_event_id else None,
            metadata={"source_system": "openclaw"},
        )

    def to_lvgl_dict(self) -> dict[str, Any]:
        base = super().to_lvgl_dict()
        priority_colors = {
            "P0": "#FF4D4F",  # Red
            "P1": "#FA8C16",  # Orange
            "P2": "#1890FF",  # Blue
            "P3": "#8C8C8C",  # Gray
        }
        status_labels = {
            "pending": "待办",
            "in_progress": "进行中",
            "completed": "已完成",
            "dismissed": "已取消",
            "blocked": "已阻塞",
        }
        base.update(
            {
                "task_key": self.task_key,
                "status": self.status,
                "status_label": status_labels.get(self.status, self.status),
                "priority": self.priority,
                "priority_color": priority_colors.get(self.priority, "#1890FF"),
                "due_time": self.due_time or "",
                "project": self.project or "",
                "tags": self.tags,
                "badge_text": f"[{self.priority} {status_labels.get(self.status, self.status)}]",
                "action_hint": "[中键长按] 标记完成" if self.status != "completed" else "[中键长按] 重启任务",
            }
        )
        return base

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TodoCard:
        card = cls(
            card_id=str(data.get("card_id") or ""),
            card_type=CardType.TODO.value,
            title=str(data.get("title") or ""),
            updated_at=str(data.get("updated_at") or _iso_now()),
            metadata=dict(data.get("metadata") or {}),
            task_key=str(data.get("task_key") or ""),
            status=str(data.get("status") or "pending"),
            priority=str(data.get("priority") or "P2"),
            due_time=str(data.get("due_time")) if data.get("due_time") is not None else None,
            project=str(data.get("project")) if data.get("project") is not None else None,
            tags=[str(t) for t in data.get("tags") or []],
            source_event_id=str(data.get("source_event_id")) if data.get("source_event_id") is not None else None,
        )
        card.validate()
        return card


@dataclass
class SummaryCard(PassportCard):
    """Card representing daily life summary (recording duration, expenses, highlights)."""

    date: str = ""
    recording_duration_seconds: float = 0.0
    recording_duration_formatted: str = ""
    expense_count: int = 0
    expense_total_cny: float = 0.0
    key_events: list[dict[str, Any]] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.card_type:
            self.card_type = CardType.SUMMARY.value
        if not self.updated_at:
            self.updated_at = _iso_now()
        if not self.recording_duration_formatted:
            self.recording_duration_formatted = format_duration(self.recording_duration_seconds)

    def validate(self) -> None:
        super().validate()
        if not self.date or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", self.date):
            raise ValueError(f"date must follow YYYY-MM-DD format, got: {self.date}")
        if self.recording_duration_seconds < 0:
            raise ValueError("recording_duration_seconds cannot be negative")
        if self.expense_count < 0:
            raise ValueError("expense_count cannot be negative")
        if self.expense_total_cny < 0:
            raise ValueError("expense_total_cny cannot be negative")

    def to_lvgl_dict(self) -> dict[str, Any]:
        base = super().to_lvgl_dict()
        event_lines: list[str] = []
        for ev in self.key_events[:LVGL_MAX_KEY_EVENTS]:
            ev_time = str(ev.get("time") or "")
            ev_title = str(ev.get("title") or "")[:14]
            event_lines.append(f"{ev_time} {ev_title}".strip())

        base.update(
            {
                "date": self.date,
                "duration_text": f"🎙 录音 {self.recording_duration_formatted}",
                "expense_text": f"💰 支出 {format_currency(self.expense_total_cny)} ({self.expense_count}笔)",
                "event_lines": event_lines,
                "action_hint": "[中键] 浏览待办",
            }
        )
        return base

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SummaryCard:
        card = cls(
            card_id=str(data.get("card_id") or ""),
            title=str(data.get("title") or ""),
            card_type=CardType.SUMMARY.value,
            updated_at=str(data.get("updated_at") or _iso_now()),
            metadata=dict(data.get("metadata") or {}),
            date=str(data.get("date") or ""),
            recording_duration_seconds=float(data.get("recording_duration_seconds") or 0.0),
            recording_duration_formatted=str(data.get("recording_duration_formatted") or ""),
            expense_count=int(data.get("expense_count") or 0),
            expense_total_cny=float(data.get("expense_total_cny") or 0.0),
            key_events=list(data.get("key_events") or []),
            stats=dict(data.get("stats") or {}),
        )
        card.validate()
        return card


@dataclass
class StatusCard(PassportCard):
    """Card representing hardware, connection, and sync status."""

    device_name: str = "AirPassport"
    battery_level: int | None = None  # 0-100 percentage
    is_charging: bool = False
    connection_status: str = "ready"
    sync_time: str = ""
    ble_encrypted: bool = True
    transport: str = "ble"  # "ble", "wifi", "offline"
    storage_free_kb: int | None = None

    def __post_init__(self) -> None:
        if not self.card_type:
            self.card_type = CardType.STATUS.value
        if not self.updated_at:
            self.updated_at = _iso_now()

    def validate(self) -> None:
        super().validate()
        if self.connection_status not in VALID_CONNECTION_STATUSES:
            raise ValueError(
                f"connection_status must be one of {sorted(VALID_CONNECTION_STATUSES)}, got: {self.connection_status}"
            )
        if self.battery_level is not None and not (0 <= self.battery_level <= 100):
            raise ValueError(f"battery_level must be between 0 and 100, got: {self.battery_level}")

    def to_lvgl_dict(self) -> dict[str, Any]:
        base = super().to_lvgl_dict()
        bat_icon = "⚡" if self.is_charging else "🔋"
        bat_str = f"{bat_icon} {self.battery_level}%" if self.battery_level is not None else "🔋 --%"
        status_icons = {
            "ready": "🟢 就绪",
            "connected": "🟢 已连接",
            "syncing": "🔵 同步中",
            "disconnected": "⚪ 未连接",
        }
        base.update(
            {
                "device_name": self.device_name,
                "battery_text": bat_str,
                "is_charging": self.is_charging,
                "connection_text": status_icons.get(self.connection_status, self.connection_status),
                "sync_time_text": f"同步: {self.sync_time}" if self.sync_time else "未同步",
                "security_text": "链路: BLE加密 (MTU 185)" if self.ble_encrypted else "链路: 普通",
                "action_hint": "[中键] 查看今日摘要",
            }
        )
        return base

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> StatusCard:
        card = cls(
            card_id=str(data.get("card_id") or ""),
            card_type=CardType.STATUS.value,
            title=str(data.get("title") or ""),
            updated_at=str(data.get("updated_at") or _iso_now()),
            metadata=dict(data.get("metadata") or {}),
            device_name=str(data.get("device_name") or "AirPassport"),
            battery_level=int(data["battery_level"]) if data.get("battery_level") is not None else None,
            is_charging=bool(data.get("is_charging") or False),
            connection_status=str(data.get("connection_status") or "ready"),
            sync_time=str(data.get("sync_time") or ""),
            ble_encrypted=bool(data.get("ble_encrypted", True)),
            transport=str(data.get("transport") or "ble"),
            storage_free_kb=int(data["storage_free_kb"]) if data.get("storage_free_kb") is not None else None,
        )
        card.validate()
        return card


# ---------------------------------------------------------------------------
# Card Deck Container
# ---------------------------------------------------------------------------


@dataclass
class PassportCardDeck:
    """A collection of cards with page management, metadata, and differential hashing."""

    deck_id: str
    cards: list[PassportCard] = field(default_factory=list)
    generated_at: str = ""
    version_hash: str = ""
    schema_version: str = CARD_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not self.generated_at:
            self.generated_at = _iso_now()
        if not self.version_hash:
            self.version_hash = self.compute_version_hash()

    def compute_version_hash(self) -> str:
        """Compute deterministic SHA-256 hash of all card contents for dirty checking."""
        content = json.dumps([c.to_dict() for c in self.cards], sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]

    def validate(self) -> None:
        if not self.deck_id:
            raise ValueError("deck_id must be non-empty")
        for card in self.cards:
            card.validate()

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "schema_version": self.schema_version,
            "deck_id": self.deck_id,
            "card_count": len(self.cards),
            "generated_at": self.generated_at,
            "version_hash": self.version_hash,
            "cards": [c.to_dict() for c in self.cards],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)

    def to_lvgl_deck(self) -> dict[str, Any]:
        """Generate full display deck preformatted for LVGL screen pagination."""
        total = len(self.cards)
        pages = []
        for idx, card in enumerate(self.cards):
            c_dict = card.to_lvgl_dict()
            c_dict["page_index"] = idx + 1
            c_dict["total_pages"] = total
            c_dict["page_header"] = f"{card.title[:12]} ({idx + 1}/{total})"
            pages.append(c_dict)
        return {
            "deck_id": self.deck_id,
            "version_hash": self.version_hash,
            "total_pages": total,
            "pages": pages,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PassportCardDeck:
        raw_cards = data.get("cards", [])
        cards: list[PassportCard] = []
        for item in raw_cards:
            if isinstance(item, dict):
                cards.append(PassportCard.from_dict(item))
        deck = cls(
            deck_id=str(data.get("deck_id") or f"deck_{safe_id(_iso_now())}"),
            cards=cards,
            generated_at=str(data.get("generated_at") or _iso_now()),
            version_hash=str(data.get("version_hash") or ""),
            schema_version=str(data.get("schema_version") or CARD_SCHEMA_VERSION),
        )
        if not deck.version_hash:
            deck.version_hash = deck.compute_version_hash()
        deck.validate()
        return deck

    def get_card(self, card_id: str) -> PassportCard | None:
        for card in self.cards:
            if card.card_id == card_id:
                return card
        return None

    def filter_cards(self, card_type: str | None = None) -> list[PassportCard]:
        if card_type is None:
            return list(self.cards)
        return [c for c in self.cards if c.card_type == card_type]


# ---------------------------------------------------------------------------
# Data Extraction: Database & Event -> Card Deck
# ---------------------------------------------------------------------------


def _daily_summary_paths(job_root: Path, date: str) -> list[Path]:
    root = Path(job_root)
    return sorted(root.glob(f"jobs/job-*/files/*/result/{date}/daily_summary.json"))


def _read_task_states_file(job_root: Path) -> list[dict[str, Any]]:
    path = Path(job_root) / "task-states.json"
    if not path.is_file():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    tasks = payload.get("tasks", {}) if isinstance(payload, dict) else {}
    if not isinstance(tasks, dict):
        return []
    return [item for item in tasks.values() if isinstance(item, dict)]


def _infer_todo_priority(title: str) -> str:
    """Infer priority from task text if not explicitly assigned."""
    title_lower = title.lower()
    if any(k in title_lower for k in ("紧急", "立刻", "马上", "p0", "urgent", "asap")):
        return "P0"
    if any(k in title_lower for k in ("重要", "优先", "尽快", "p1", "high")):
        return "P1"
    if any(k in title_lower for k in ("低优先", "闲时", "顺便", "p3", "low")):
        return "P3"
    return "P2"


def _format_event_time(time_field: Any) -> str:
    if not isinstance(time_field, dict):
        return ""
    absolute = str(time_field.get("absolute") or "").strip()
    if absolute:
        try:
            dt = datetime.fromisoformat(absolute)
            return dt.strftime("%H:%M")
        except Exception:
            if "T" in absolute:
                parts = absolute.split("T")
                if len(parts) > 1:
                    return parts[1][:5]
    rel = time_field.get("relative_seconds")
    if rel is not None:
        try:
            sec = int(float(rel))
            return f"+{sec // 60:02d}:{sec % 60:02d}"
        except Exception:
            pass
    return ""


def extract_passport_cards(
    job_root: Path | str,
    *,
    date: str | None = None,
    task_states: Sequence[Mapping[str, Any]] | None = None,
    device_status: Mapping[str, Any] | None = None,
    openclaw_tasks: Sequence[Mapping[str, Any]] | None = None,
    timezone_name: str = "Asia/Shanghai",
) -> PassportCardDeck:
    """Extract today's tasks, daily summary, and device status into a structured PassportCardDeck."""
    root = Path(job_root)
    target_date = date or datetime.now(tz=ZoneInfo(timezone_name)).strftime("%Y-%m-%d")
    now_iso = _iso_now(timezone_name)

    # 1. Status Card (Header of the deck)
    dev_status = dict(device_status or {})
    status_card = StatusCard(
        card_id=f"card_status_{safe_id(dev_status.get('device_name', 'AirPassport'))}",
        card_type=CardType.STATUS.value,
        title="系统状态",
        updated_at=now_iso,
        device_name=str(dev_status.get("device_name") or "AirPassport"),
        battery_level=int(dev_status["battery_level"]) if dev_status.get("battery_level") is not None else None,
        is_charging=bool(dev_status.get("is_charging") or False),
        connection_status=str(dev_status.get("connection_status") or "ready"),
        sync_time=str(dev_status.get("sync_time") or datetime.now(tz=ZoneInfo(timezone_name)).strftime("%H:%M:%S")),
        ble_encrypted=bool(dev_status.get("ble_encrypted", True)),
        transport=str(dev_status.get("transport") or "ble"),
        storage_free_kb=int(dev_status["storage_free_kb"]) if dev_status.get("storage_free_kb") is not None else None,
        metadata={"source": "device_telemetry"},
    )

    # 2. Summary Card: Aggregate recording duration, expenses, and key events
    summary_paths = _daily_summary_paths(root, target_date)
    total_recording_sec = 0.0
    expense_count = 0
    expense_total_cny = 0.0
    key_events_pool: list[dict[str, Any]] = []
    category_counts: dict[str, int] = {}
    found_events: list[dict[str, Any]] = []

    # Also compute duration from recording files or manifests if available
    for spath in summary_paths:
        try:
            payload = json.loads(spath.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue

        # Expenses from daily_summary
        expense_total_cny += float(payload.get("expense_total_cny") or 0.0)

        # Timeline events
        timeline = payload.get("timeline", [])
        if isinstance(timeline, list):
            for raw_ev in timeline:
                if not isinstance(raw_ev, dict):
                    continue
                found_events.append(raw_ev)
                cats = [str(c) for c in raw_ev.get("categories", [])]
                for cat in cats:
                    category_counts[cat] = category_counts.get(cat, 0) + 1
                if "expense" in cats:
                    expense_count += 1
                # Key highlight candidates
                title = str(raw_ev.get("title") or "").strip()
                if title:
                    t_str = _format_event_time(raw_ev.get("time"))
                    primary_cat = cats[0] if cats else "routine"
                    key_events_pool.append(
                        {
                            "time": t_str,
                            "title": title,
                            "category": primary_cat,
                        }
                    )

        # Try to infer recording duration from file manifest or audio in parent directories
        manifest_path = spath.parents[2] / "manifest.json"
        if manifest_path.is_file():
            try:
                m_payload = json.loads(manifest_path.read_text(encoding="utf-8"))
                for seg in m_payload.get("segments", []):
                    dur = seg.get("duration_seconds")
                    if isinstance(dur, (int, float)):
                        total_recording_sec += float(dur)
            except Exception:
                pass

    # Fallback duration estimation from timeline span if no manifests
    if total_recording_sec == 0.0 and found_events:
        rel_secs = [
            float(ev["time"]["relative_seconds"])
            for ev in found_events
            if isinstance(ev.get("time"), dict) and ev["time"].get("relative_seconds") is not None
        ]
        if rel_secs:
            total_recording_sec = max(rel_secs)

    summary_card = SummaryCard(
        card_id=f"card_summary_{safe_id(target_date)}",
        card_type=CardType.SUMMARY.value,
        title=f"今日概况 ({target_date[5:]})",
        updated_at=now_iso,
        date=target_date,
        recording_duration_seconds=total_recording_sec,
        recording_duration_formatted=format_duration(total_recording_sec),
        expense_count=expense_count,
        expense_total_cny=round(expense_total_cny, 2),
        key_events=key_events_pool[:LVGL_MAX_KEY_EVENTS],
        stats=category_counts,
        metadata={"summary_count": len(summary_paths)},
    )

    # 3. Todo Cards: Extract from OpenClaw unified tasks, task-states.json, and todo events
    todo_cards: list[TodoCard] = []
    seen_task_keys: set[str] = set()

    # (a) From OpenClaw unified tasks (highest priority SoT)
    if openclaw_tasks:
        for otask in openclaw_tasks:
            try:
                card = TodoCard.from_openclaw_task(otask)
                if card.status == "dismissed":
                    continue
                if card.task_key not in seen_task_keys:
                    seen_task_keys.add(card.task_key)
                    todo_cards.append(card)
            except Exception:
                continue

    # (b) From Life Logger task-states.json
    raw_states = list(task_states) if task_states is not None else _read_task_states_file(root)
    for st in raw_states:
        key = str(st.get("task_key") or st.get("id") or "").strip()
        if not key or key in seen_task_keys:
            continue
        title = str(st.get("title") or "未命名待办").strip()
        status = str(st.get("status") or "pending").strip()
        if status == "dismissed":
            continue
        if status not in VALID_TODO_STATUSES:
            status = "pending"
        priority = str(st.get("priority") or _infer_todo_priority(title))
        if priority not in VALID_PRIORITIES:
            priority = _infer_todo_priority(title)

        seen_task_keys.add(key)
        todo_cards.append(
            TodoCard(
                card_id=f"card_todo_{safe_id(key)}",
                card_type=CardType.TODO.value,
                title=title,
                updated_at=str(st.get("updated_at") or now_iso),
                task_key=key,
                status=status,
                priority=priority,
                due_time=str(st.get("due_time")) if st.get("due_time") else None,
                project=str(st.get("project_id") or st.get("project") or ""),
                tags=[str(t) for t in st.get("tags") or []],
                source_event_id=str(st.get("event_id")) if st.get("event_id") else None,
                metadata={"source_system": "life_logger_store"},
            )
        )

    # (c) From today's events if not in task states
    for ev in found_events:
        cats = [str(c) for c in ev.get("categories", [])]
        if "todo" not in cats:
            continue
        ev_id = str(ev.get("event_id") or "")
        key = f"evt_{ev_id}" if ev_id else f"todo_{len(seen_task_keys)}"
        if key in seen_task_keys or ev_id in seen_task_keys:
            continue
        seen_task_keys.add(key)
        title = str(ev.get("title") or "未命名待办").strip()
        t_str = _format_event_time(ev.get("time"))
        todo_cards.append(
            TodoCard(
                card_id=f"card_todo_{safe_id(key)}",
                card_type=CardType.TODO.value,
                title=title,
                updated_at=now_iso,
                task_key=key,
                status="pending",
                priority=_infer_todo_priority(title),
                due_time=t_str or None,
                source_event_id=ev_id or None,
                metadata={"source_system": "life_event"},
            )
        )

    # Sort todo cards: P0 -> P1 -> P2 -> P3; pending first, completed last
    priority_order = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
    status_order = {"pending": 0, "in_progress": 1, "blocked": 2, "completed": 3, "dismissed": 4}
    todo_cards.sort(
        key=lambda c: (
            status_order.get(c.status, 9),
            priority_order.get(c.priority, 9),
            c.title,
        )
    )

    # Combine into deck: StatusCard -> SummaryCard -> TodoCards
    deck_cards: list[PassportCard] = [status_card, summary_card]
    deck_cards.extend(todo_cards)

    deck_id = f"deck_{target_date}_{safe_id(status_card.device_name)}"
    return PassportCardDeck(deck_id=deck_id, cards=deck_cards, generated_at=now_iso)


def fetch_live_passport_deck(
    job_root: Path | str,
    *,
    date: str | None = None,
    task_states: Sequence[Mapping[str, Any]] | None = None,
    device_status: Mapping[str, Any] | None = None,
    timezone_name: str = "Asia/Shanghai",
) -> PassportCardDeck:
    """Extract passport cards, automatically fetching live OpenClaw tasks if available."""
    openclaw_tasks = None
    try:
        from life_logger.openclaw_bridge import OpenClawBridgeClient

        oc_client = OpenClawBridgeClient()
        if oc_client.is_healthy():
            openclaw_tasks = oc_client.list_tasks()
    except Exception:
        openclaw_tasks = None

    return extract_passport_cards(
        job_root=job_root,
        date=date,
        task_states=task_states,
        device_status=device_status,
        openclaw_tasks=openclaw_tasks,
        timezone_name=timezone_name,
    )



# ---------------------------------------------------------------------------
# Compact Binary Serialization & Packet Framing Protocol
# ---------------------------------------------------------------------------


def pack_packet(
    seq: int,
    total: int,
    payload: bytes,
    *,
    is_last: bool,
    is_compressed: bool,
) -> bytes:
    """Pack a single chunk into a binary framed packet with CRC32 verification.

    Packet layout (15B header + payload):
    - 4B: Magic (b"APC1")
    - 1B: Flags (bit0: is_last, bit1: is_compressed)
    - 2B: Sequence number (UInt16 BE)
    - 2B: Total chunks (UInt16 BE)
    - 2B: Payload length (UInt16 BE)
    - 4B: CRC32 of (Header[0..11] + Payload)
    - NB: Payload bytes
    """
    if seq >= total or seq < 0:
        raise ValueError(f"Invalid sequence {seq} for total {total}")
    if len(payload) > 65535:
        raise ValueError("Payload exceeds maximum 64KB for UInt16")

    flags = 0
    if is_last:
        flags |= FLAG_IS_LAST_CHUNK
    if is_compressed:
        flags |= FLAG_IS_COMPRESSED

    partial_header = struct.pack(
        ">4sBHHH",
        BINARY_PROTOCOL_MAGIC,
        flags,
        seq,
        total,
        len(payload),
    )
    # Checksum over partial_header + payload
    crc = zlib.crc32(partial_header + payload) & 0xFFFFFFFF
    full_header = partial_header + struct.pack(">I", crc)
    return full_header + payload


def unpack_packet(packet: bytes) -> tuple[int, int, bool, bool, bytes]:
    """Unpack and verify a framed packet.

    Returns (seq, total, is_last, is_compressed, payload).
    Raises ValueError on corruption or protocol violation.
    """
    if len(packet) < BINARY_HEADER_SIZE:
        raise ValueError(f"Packet too short: {len(packet)} < {BINARY_HEADER_SIZE}")

    magic, flags, seq, total, payload_len, packet_crc = struct.unpack(
        BINARY_HEADER_FORMAT, packet[:BINARY_HEADER_SIZE]
    )
    if magic != BINARY_PROTOCOL_MAGIC:
        raise ValueError(f"Invalid magic: {magic!r}, expected {BINARY_PROTOCOL_MAGIC!r}")

    payload = packet[BINARY_HEADER_SIZE:]
    if len(payload) != payload_len:
        raise ValueError(f"Payload length mismatch: expected {payload_len}, got {len(payload)}")

    partial_header = packet[:11]
    expected_crc = zlib.crc32(partial_header + payload) & 0xFFFFFFFF
    if packet_crc != expected_crc:
        raise ValueError(f"CRC32 mismatch: packet has {packet_crc:08x}, computed {expected_crc:08x}")

    is_last = bool(flags & FLAG_IS_LAST_CHUNK)
    is_compressed = bool(flags & FLAG_IS_COMPRESSED)
    return seq, total, is_last, is_compressed, payload


def chunk_deck(
    deck: PassportCardDeck,
    *,
    max_chunk_size: int = MAX_BLE_CHUNK_SIZE,
    compress: bool = True,
) -> list[bytes]:
    """Serialize and chunk a PassportCardDeck into binary framed packets for BLE/Wi-Fi streaming."""
    raw_json = deck.to_json().encode("utf-8")
    if compress:
        payload_data = zlib.compress(raw_json, level=9)
    else:
        payload_data = raw_json

    total_len = len(payload_data)
    if total_len == 0:
        return [pack_packet(0, 1, b"", is_last=True, is_compressed=compress)]

    chunk_size = max(16, max_chunk_size)
    chunks_list: list[bytes] = []
    total_chunks = (total_len + chunk_size - 1) // chunk_size

    for seq in range(total_chunks):
        start = seq * chunk_size
        end = min(total_len, start + chunk_size)
        chunk_data = payload_data[start:end]
        is_last = seq == total_chunks - 1
        packet = pack_packet(
            seq,
            total_chunks,
            chunk_data,
            is_last=is_last,
            is_compressed=compress,
        )
        chunks_list.append(packet)

    return chunks_list


def deck_to_packets_hex(
    deck: PassportCardDeck,
    *,
    max_chunk_size: int = MAX_BLE_CHUNK_SIZE,
    compress: bool = True,
) -> list[str]:
    """Helper to produce hex-encoded packets for JSON REST responses."""
    packets = chunk_deck(deck, max_chunk_size=max_chunk_size, compress=compress)
    return [p.hex() for p in packets]


class PacketAssembler:
    """Assembles and verifies stream packets into a PassportCardDeck."""

    def __init__(self) -> None:
        self._chunks: dict[int, bytes] = {}
        self._total_chunks: int | None = None
        self._is_compressed: bool = False

    def reset(self) -> None:
        self._chunks.clear()
        self._total_chunks = None
        self._is_compressed = False

    def add_packet(self, packet: bytes) -> bool:
        """Add a packet. Returns True if packet was valid and recorded."""
        seq, total, _, is_compressed, payload = unpack_packet(packet)
        if self._total_chunks is not None and self._total_chunks != total:
            raise ValueError(f"Total chunk count changed mid-stream from {self._total_chunks} to {total}")
        self._total_chunks = total
        self._is_compressed = is_compressed
        self._chunks[seq] = payload
        return True

    def missing_chunks(self) -> list[int]:
        if self._total_chunks is None:
            return []
        return [i for i in range(self._total_chunks) if i not in self._chunks]

    def is_complete(self) -> bool:
        if self._total_chunks is None:
            return False
        return len(self._chunks) == self._total_chunks

    @property
    def progress(self) -> float:
        if self._total_chunks is None or self._total_chunks == 0:
            return 0.0
        return min(1.0, len(self._chunks) / self._total_chunks)

    def assemble(self) -> PassportCardDeck:
        """Reconstruct the PassportCardDeck from assembled packets."""
        if not self.is_complete():
            missing = self.missing_chunks()
            raise ValueError(f"Cannot assemble incomplete deck, missing chunks: {missing}")

        assert self._total_chunks is not None
        ordered_data = b"".join(self._chunks[i] for i in range(self._total_chunks))

        if self._is_compressed:
            try:
                raw_json = zlib.decompress(ordered_data)
            except zlib.error as err:
                raise ValueError(f"Decompression failed: {err}") from err
        else:
            raw_json = ordered_data

        try:
            deck_dict = json.loads(raw_json.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as err:
            raise ValueError(f"JSON deserialization failed: {err}") from err

        return PassportCardDeck.from_dict(deck_dict)


# ---------------------------------------------------------------------------
# Navigation & Pager (中键翻页与交互)
# ---------------------------------------------------------------------------


class PassportPager:
    """Manages middle-button paging and action dispatch for the 240x280 display."""

    def __init__(self, deck: PassportCardDeck, *, loop: bool = True) -> None:
        self.deck = deck
        self.loop = loop
        self.current_page: int = 0  # 0-indexed

    @property
    def total_pages(self) -> int:
        return len(self.deck.cards)

    def current_card(self) -> PassportCard | None:
        if 0 <= self.current_page < len(self.deck.cards):
            return self.deck.cards[self.current_page]
        return None

    def next_page(self) -> PassportCard | None:
        """Simulate middle button short press: advance to next card."""
        if self.total_pages == 0:
            return None
        if self.current_page + 1 < self.total_pages:
            self.current_page += 1
        elif self.loop:
            self.current_page = 0
        return self.current_card()

    def prev_page(self) -> PassportCard | None:
        """Simulate previous button press: retreat to previous card."""
        if self.total_pages == 0:
            return None
        if self.current_page > 0:
            self.current_page -= 1
        elif self.loop:
            self.current_page = self.total_pages - 1
        return self.current_card()

    def jump_to(self, page_index: int) -> PassportCard | None:
        """Jump to specific 0-based page index."""
        if 0 <= page_index < self.total_pages:
            self.current_page = page_index
            return self.current_card()
        raise IndexError(f"Page index {page_index} out of range (0..{self.total_pages - 1})")

    def action_click(self) -> dict[str, Any]:
        """Simulate middle button long press / action click on the current card.

        If current card is TodoCard: toggles between pending and completed.
        If current card is SummaryCard / StatusCard: returns refresh request.
        """
        card = self.current_card()
        if card is None:
            return {"action": "none", "reason": "empty_deck"}

        now_iso = _iso_now()
        if isinstance(card, TodoCard):
            old_status = card.status
            new_status = "completed" if old_status != "completed" else "pending"
            card.status = new_status
            card.updated_at = now_iso
            self.deck.version_hash = self.deck.compute_version_hash()
            return {
                "action": "toggle_todo",
                "card_id": card.card_id,
                "task_key": card.task_key,
                "old_status": old_status,
                "new_status": new_status,
                "updated_at": now_iso,
            }

        return {
            "action": "refresh",
            "card_id": card.card_id,
            "card_type": card.card_type,
            "timestamp": now_iso,
        }

    def render_screen(self) -> dict[str, Any]:
        """Render the complete LVGL display state for the current page."""
        card = self.current_card()
        total = self.total_pages
        if card is None:
            return {
                "page_index": 0,
                "total_pages": 0,
                "header": "AI Passport (0/0)",
                "card": None,
                "footer": "暂无卡片数据",
            }

        card_lvgl = card.to_lvgl_dict()
        return {
            "page_index": self.current_page + 1,
            "total_pages": total,
            "header": f"AI Passport ({self.current_page + 1}/{total})",
            "card": card_lvgl,
            "footer": card_lvgl.get("action_hint", "[中键] 翻页"),
        }


# ---------------------------------------------------------------------------
# Offline Cache (离线分页缓存)
# ---------------------------------------------------------------------------


class OfflineCardCache:
    """Manages atomic caching of PassportCardDecks on disk."""

    @staticmethod
    def cache_path(cache_dir: Path | str, filename: str = "passport_cards_cache.json") -> Path:
        return Path(cache_dir) / filename

    @classmethod
    def save(
        cls,
        deck: PassportCardDeck,
        cache_dir: Path | str,
        filename: str = "passport_cards_cache.json",
    ) -> Path:
        """Atomically persist a card deck to disk."""
        target = cls.cache_path(cache_dir, filename)
        target.parent.mkdir(parents=True, exist_ok=True)
        from life_logger.atomic import write_bytes_atomic

        write_bytes_atomic(target, deck.to_json().encode("utf-8"))
        return target

    @classmethod
    def load(
        cls,
        cache_dir: Path | str,
        filename: str = "passport_cards_cache.json",
    ) -> PassportCardDeck | None:
        """Load a card deck from disk. Returns None if absent or invalid."""
        target = cls.cache_path(cache_dir, filename)
        if not target.is_file():
            return None
        try:
            payload = json.loads(target.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                return None
            return PassportCardDeck.from_dict(payload)
        except Exception:
            return None

    @classmethod
    def is_stale(
        cls,
        cache_dir: Path | str,
        max_age_seconds: float = 3600.0,
        filename: str = "passport_cards_cache.json",
    ) -> bool:
        """Check whether the cache is absent or older than max_age_seconds."""
        target = cls.cache_path(cache_dir, filename)
        if not target.is_file():
            return True
        try:
            mtime = target.stat().st_mtime
            age = datetime.now().timestamp() - mtime
            return age > max_age_seconds
        except OSError:
            return True

    @classmethod
    def clear(
        cls,
        cache_dir: Path | str,
        filename: str = "passport_cards_cache.json",
    ) -> bool:
        """Delete cache file if it exists."""
        target = cls.cache_path(cache_dir, filename)
        try:
            target.unlink(missing_ok=True)
            return True
        except OSError:
            return False
