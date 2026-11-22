"""习惯（habit）的唯一事实源：本地 `habits.json`（schema `local-habits-v1`）。

## 为什么不放进 OpenClaw

OpenClaw 的 task 是一次性语义（`pending → completed/dismissed`，做完即终态），
**没有「周期 / 重复」概念**（`~/.openclaw/workspace/scripts/openclaw-task-store.js`
的字段里没有 recurrence）。而习惯的本质是「今天做了没」，同一件事每天重新计一次：
- 放进 task → 每天都要新造一条，`memory/tasks/*.md` 里堆垃圾，还污染真待办视图；
- 想靠 patch 记打卡日期 → `patch_task` 只改 status/priority/title，没地方存日期历史。

所以习惯单独一本账：**一个文件既是唯一写入点，也是所有只读投影的唯一数据源**
（网页看板 / 徽章第 3 屏 / 微信汇报）。待办仍归 OpenClaw，两条线互不影响。

## 三条语义（别改，下游依赖）

1. `habits[].checkins` 是**以北京日期为键**的字典（`{"2026-10-13": "...ISO..."}`）：
   同一天重复打卡天然幂等；「天天重置」= 判断今天有没有键，而不是清空数据。
2. 删除 = `status="dismissed"`（保留打卡历史，可追溯），不做物理删除。
3. `streak` / `checked_today` 每次读取现算，不落盘 —— 不会因为漏跑同步而失真。

## 形状

```json
{
  "schema_version": "local-habits-v1",
  "backend": "local",
  "habits": {
    "habit-8f3c1a2b4d5e": {
      "habit_key": "habit-8f3c1a2b4d5e",
      "title": "冥想 10 分钟",
      "status": "active",
      "source": "voice",
      "review_status": "confirmed",
      "source_type": "manual",
      "created_at": "2026-10-13T09:00:00+08:00",
      "updated_at": "2026-10-13T09:00:00+08:00",
      "checkins": {"2026-10-13": "2026-10-13T22:10:00+08:00"},
      "last_checkin_date": "2026-10-13",
      "total_checkins": 1
    }
  }
}
```
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

logger = logging.getLogger(__name__)

HABIT_SCHEMA_VERSION = "local-habits-v1"
BEIJING_TZ = timezone(timedelta(hours=8))

VALID_HABIT_STATUSES: tuple[str, ...] = ("active", "paused", "dismissed")
HABIT_TRIGGER_TYPES: tuple[str, ...] = ("habit", "habit_done", "habit_delete")

TITLE_MAX_CHARS = 60
# 徽章每格上限 60 字节 ≈ 20 个汉字（固件 0x04 帧的 title_len 是单字节长度）
BADGE_TITLE_MAX_CHARS = 20
# 徽章第 3 屏槽位数（与固件 PASSPORT_TODO_MAX_ITEMS = 4 同源）
BADGE_SLOT_LIMIT = 4

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_INDEX_RE = re.compile(r"^#?(\d{1,3})$")


class HabitNotFoundError(LookupError):
    """目标习惯不存在（或指向不明确）。

    `candidates` 让调用方能在微信/网页里回一句「你要的是不是这几条？」，而不是
    静默失败 —— 口径同 `docs/passport-todo-loop-design.md` §3.2。
    """

    def __init__(self, target: str, candidates: Iterable[str] = ()) -> None:
        super().__init__(f"找不到习惯：{target}")
        self.target = target
        self.candidates: list[str] = list(candidates)


def beijing_now() -> datetime:
    return datetime.now(BEIJING_TZ)


def beijing_date(value: datetime | None = None) -> str:
    """北京时区（UTC+8）的 YYYY-MM-DD。全项目唯一的「今天」口径。"""
    return (value or beijing_now()).astimezone(BEIJING_TZ).strftime("%Y-%m-%d")


def _normalize_title(value: str) -> str:
    return re.sub(r"\s+", "", str(value or "")).strip("，,。.：:；;、 　")


def _now_iso(clock: Callable[[], datetime]) -> str:
    return clock().astimezone(BEIJING_TZ).isoformat()


def _streak_from(checkins: Mapping[str, Any], today: str) -> int:
    """连续打卡天数：从今天（今天没打就从昨天）往前数，断了就停。"""
    if not checkins:
        return 0
    try:
        cursor = datetime.strptime(today, "%Y-%m-%d")
    except ValueError:
        return 0
    if today not in checkins:
        cursor -= timedelta(days=1)
    days = 0
    while cursor.strftime("%Y-%m-%d") in checkins:
        days += 1
        cursor -= timedelta(days=1)
    return days


class LocalHabitStore:
    """习惯台账。锁 + 原子写，形状与 `LocalReminderStore` 一致（同一套运维经验）。"""

    def __init__(
        self,
        root: Path,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.root = Path(root)
        self.path = self.root / "habits.json"
        self._clock = clock or beijing_now
        from life_logger.runtime_queue import ProcessRLock
        self._lock = ProcessRLock(self.root / ".habits.lock")

    # ------------------------------------------------------------------ 持久化
    def _read_unlocked(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = {}
        habits = payload.get("habits") if isinstance(payload, dict) else None
        return {
            "schema_version": HABIT_SCHEMA_VERSION,
            "backend": "local",
            "habits": habits if isinstance(habits, dict) else {},
        }

    def _write_unlocked(self, payload: Mapping[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temporary, self.path)

    # ------------------------------------------------------------------ 读投影
    def _project(self, record: Mapping[str, Any], *, today: str) -> dict[str, Any]:
        checkins = record.get("checkins")
        checkins = checkins if isinstance(checkins, dict) else {}
        return {
            "habit_key": record.get("habit_key"),
            "title": record.get("title"),
            "status": record.get("status") or "active",
            "source": record.get("source") or "manual",
            "review_status": record.get("review_status") or "confirmed",
            "source_type": record.get("source_type") or "manual",
            "created_at": record.get("created_at"),
            "updated_at": record.get("updated_at"),
            "last_checkin_date": record.get("last_checkin_date"),
            "total_checkins": len(checkins),
            "date": today,
            # 以下是现算字段，不落盘：徽章/看板/微信都按它判断「今天做了没」
            "checked_today": today in checkins,
            "streak": _streak_from(checkins, today),
        }

    def _sorted_records(
        self, habits: Mapping[str, Any], *, include_inactive: bool = False
    ) -> list[dict[str, Any]]:
        rows = [
            dict(item)
            for item in habits.values()
            if isinstance(item, dict) and item.get("habit_key")
        ]
        if not include_inactive:
            rows = [r for r in rows if r.get("status") == "active"]
        rows.sort(
            key=lambda r: (
                str(r.get("created_at") or ""),
                str(r.get("habit_key") or ""),
            )
        )
        return rows

    def _match(self, habits: Mapping[str, Any], target: str) -> dict[str, Any]:
        """把用户说出的东西落到一条习惯上。

        匹配顺序（可预期、可解释，不做编辑距离）：
        1. `habit_key` 精确；2. `#2` / `2` 编号（按创建顺序，徽章上的编号）；
        3. 标题精确（忽略空白与标点）；4. 标题包含 / 被包含 —— 唯一才认，多条算歧义。
        """
        wanted = str(target or "").strip()
        if not wanted:
            raise HabitNotFoundError(target)
        if wanted in habits and isinstance(habits[wanted], dict):
            return {"habit_key": wanted, "ambiguous": False, "candidates": []}

        active = self._sorted_records(habits)
        index_match = _INDEX_RE.match(wanted)
        if index_match:
            position = int(index_match.group(1))
            if 1 <= position <= len(active):
                return {
                    "habit_key": active[position - 1].get("habit_key"),
                    "ambiguous": False,
                    "candidates": [],
                }

        normalized = _normalize_title(wanted)
        exact = [
            r
            for r in active
            if _normalize_title(str(r.get("title") or "")) == normalized
        ]
        if len(exact) == 1:
            return {
                "habit_key": exact[0].get("habit_key"),
                "ambiguous": False,
                "candidates": [],
            }

        pool = exact or [
            r
            for r in active
            if normalized
            and (
                normalized in _normalize_title(str(r.get("title") or ""))
                or _normalize_title(str(r.get("title") or "")) in normalized
            )
        ]
        if len(pool) == 1:
            return {
                "habit_key": pool[0].get("habit_key"),
                "ambiguous": False,
                "candidates": [],
            }
        names = [str(r.get("title") or "") for r in pool]
        if pool:
            # 歧义：认第一条（创建最早的），把其他候选带回去让人一句话纠正
            return {
                "habit_key": pool[0].get("habit_key"),
                "ambiguous": True,
                "candidates": names,
            }
        raise HabitNotFoundError(target, [str(r.get("title") or "") for r in active])

    # ------------------------------------------------------------------ 公开 API
    def get(self, target: str) -> dict[str, Any] | None:
        with self._lock:
            payload = self._read_unlocked()
            try:
                matched = self._match(payload["habits"], target)
            except HabitNotFoundError:
                return None
            record = payload["habits"].get(matched["habit_key"])
        if not isinstance(record, dict):
            return None
        return self._project(record, today=beijing_date(self._clock()))

    def list_habits(
        self,
        *,
        include_inactive: bool = False,
        today: str | None = None,
    ) -> list[dict[str, Any]]:
        stamp = today or beijing_date(self._clock())
        with self._lock:
            payload = self._read_unlocked()
            rows = self._sorted_records(
                payload["habits"], include_inactive=include_inactive
            )
        return [self._project(row, today=stamp) for row in rows]

    def add(
        self,
        title: str,
        *,
        habit_key: str | None = None,
        source: str = "voice",
    ) -> dict[str, Any]:
        """新建习惯；同名（去空白/标点）已存在则原样返回，不重复造条目。"""
        clean = str(title or "").strip()[:TITLE_MAX_CHARS]
        if not clean:
            raise ValueError("缺少习惯名称")
        stamp = _now_iso(self._clock)
        with self._lock:
            payload = self._read_unlocked()
            habits = payload["habits"]
            normalized = _normalize_title(clean)
            for existing in habits.values():
                if (
                    isinstance(existing, dict)
                    and _normalize_title(str(existing.get("title") or "")) == normalized
                    and existing.get("status") != "dismissed"
                ):
                    return {
                        "habit": self._project(
                            existing, today=beijing_date(self._clock())
                        ),
                        "created": False,
                    }
            key = str(habit_key or "").strip() or f"habit-{uuid.uuid4().hex[:12]}"
            record = {
                "habit_key": key,
                "title": clean,
                "status": "active",
                "source": str(source or "voice"),
                # 人自己说的口令 = 已确认，下游（徽章严格模式）不必再点一次确认
                "review_status": "confirmed",
                "source_type": "manual",
                "created_at": stamp,
                "updated_at": stamp,
                "checkins": {},
                "last_checkin_date": None,
            }
            habits[key] = record
            self._write_unlocked(payload)
            return {
                "habit": self._project(record, today=beijing_date(self._clock())),
                "created": True,
            }

    def set_status(self, target: str, status: str) -> dict[str, Any]:
        """`active` / `paused` / `dismissed`。删除即 dismissed（历史保留）。"""
        if status not in VALID_HABIT_STATUSES:
            raise ValueError(f"状态无效：{status}")
        with self._lock:
            payload = self._read_unlocked()
            matched = self._match(payload["habits"], target)
            record = dict(payload["habits"][matched["habit_key"]])
            record["status"] = status
            record["updated_at"] = _now_iso(self._clock)
            payload["habits"][matched["habit_key"]] = record
            self._write_unlocked(payload)
            return {
                "habit": self._project(record, today=beijing_date(self._clock())),
                "matched": matched,
            }

    def checkin(
        self,
        target: str,
        *,
        date: str | None = None,
        undo: bool = False,
    ) -> dict[str, Any]:
        """打卡（默认今天，北京时区）。同一天重复打卡是幂等的：`changed=False`。

        `date` 只用于补录/补正；格式 YYYY-MM-DD。`undo=True` 撤销该日打卡。
        """
        day = str(date or beijing_date(self._clock())).strip()
        if not _DATE_RE.match(day):
            raise ValueError("打卡日期必须是 YYYY-MM-DD")
        with self._lock:
            payload = self._read_unlocked()
            matched = self._match(payload["habits"], target)
            record = dict(payload["habits"][matched["habit_key"]])
            checkins = record.get("checkins")
            checkins = dict(checkins) if isinstance(checkins, dict) else {}
            changed = False
            if undo:
                if day in checkins:
                    checkins.pop(day, None)
                    changed = True
            elif day not in checkins:
                checkins[day] = _now_iso(self._clock)
                changed = True
            record["checkins"] = checkins
            dates = sorted(checkins)
            record["last_checkin_date"] = dates[-1] if dates else None
            record["updated_at"] = _now_iso(self._clock)
            payload["habits"][matched["habit_key"]] = record
            if changed:
                self._write_unlocked(payload)
            return {
                "habit": self._project(record, today=beijing_date(self._clock())),
                "date": day,
                "changed": changed,
                "matched": matched,
            }

    def snapshot(
        self,
        *,
        today: str | None = None,
        include_inactive: bool = False,
    ) -> dict[str, Any]:
        """给网页看板 / 徽章 / 微信的统一只读投影。"""
        stamp = today or beijing_date(self._clock())
        rows = self.list_habits(include_inactive=include_inactive, today=stamp)
        return {
            "schema_version": HABIT_SCHEMA_VERSION,
            "backend": "local",
            "date": stamp,
            "total": len(rows),
            "checked_count": len([r for r in rows if r["checked_today"]]),
            "habits": rows,
        }

    def habits_for_badge(self, *, today: str | None = None) -> list[dict[str, Any]]:
        """徽章第 3 屏（0x04 帧）的数据投影：4 槽、带编号与打卡标志位。

        `[0x04][index][total][flags][title_len][title]`，flags bit0 = 已打卡。
        编号是「当次看着徽章说」用的（`#2`），下次重新看，所以这里按创建顺序现算。
        """
        rows = self.list_habits(today=today)[:BADGE_SLOT_LIMIT]
        slots: list[dict[str, Any]] = []
        for index, row in enumerate(rows, 1):
            title = str(row.get("title") or "")
            slots.append(
                {
                    "index": index,
                    "total": len(rows),
                    "flags": 0x01 if row["checked_today"] else 0x00,
                    "title": title[:BADGE_TITLE_MAX_CHARS],
                    "habit_key": row["habit_key"],
                    "checked_today": row["checked_today"],
                    "streak": row["streak"],
                }
            )
        return slots


def apply_habit_captures(
    store: LocalHabitStore,
    captures: Iterable[Any],
    *,
    date: str | None = None,
) -> list[dict[str, Any]]:
    """把口语口令（`habit` / `habit_done` / `habit_delete`）落到台账上。

    只处理这三种 trigger；其余原样跳过 —— 调用方（Watch ASR 实时扫描、管道脚本、
    网页）不需要知道习惯的存储细节。返回逐条结果，供微信/网页回话。

    不抛异常：单条失败只记 `applied=False`，不能因为一个没匹配上的口令把整段转写搞挂。
    """
    outcomes: list[dict[str, Any]] = []
    for capture in captures:
        raw: Any = capture
        if not isinstance(raw, Mapping):
            to_dict = getattr(capture, "to_dict", None)
            if callable(to_dict):
                try:
                    raw = to_dict()
                except Exception:  # pragma: no cover - 防御性
                    raw = None
        data: Mapping[str, Any] | None = raw if isinstance(raw, Mapping) else None
        if not isinstance(data, Mapping):
            continue
        trigger_type = str(data.get("trigger_type") or "").strip()
        if trigger_type not in HABIT_TRIGGER_TYPES:
            continue
        target = str(
            data.get("extracted_content")
            or data.get("target")
            or data.get("title")
            or ""
        ).strip()
        outcome: dict[str, Any] = {
            "trigger_type": trigger_type,
            "target": target,
            "capture_id": data.get("id"),
            "captured_at": data.get("captured_at"),
            "applied": False,
            "action": None,
            "habit_key": None,
            "habit_title": None,
            "changed": False,
            "ambiguous": False,
            "candidates": [],
            "error": None,
        }
        try:
            if trigger_type == "habit":
                result = store.add(target, source="voice")
                outcome.update(
                    applied=True,
                    action="add",
                    created=bool(result.get("created")),
                    habit_key=result["habit"]["habit_key"],
                    habit_title=result["habit"]["title"],
                )
            elif trigger_type == "habit_done":
                result = store.checkin(target, date=date)
                outcome.update(
                    applied=True,
                    action="checkin",
                    changed=bool(result.get("changed")),
                    ambiguous=bool(result["matched"].get("ambiguous")),
                    candidates=list(result["matched"].get("candidates") or []),
                    habit_key=result["habit"]["habit_key"],
                    habit_title=result["habit"]["title"],
                )
            else:  # habit_delete
                result = store.set_status(target, "dismissed")
                outcome.update(
                    applied=True,
                    action="dismiss",
                    ambiguous=bool(result["matched"].get("ambiguous")),
                    candidates=list(result["matched"].get("candidates") or []),
                    habit_key=result["habit"]["habit_key"],
                    habit_title=result["habit"]["title"],
                )
        except HabitNotFoundError as error:
            outcome["error"] = str(error)
            outcome["candidates"] = list(error.candidates)
        except (ValueError, OSError) as error:
            outcome["error"] = str(error)
        outcomes.append(outcome)
    return outcomes
