from datetime import datetime, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from life_logger import memory_rag


@pytest.mark.parametrize("host_zone", ["America/Los_Angeles", "Asia/Shanghai", "UTC"])
def test_health_context_uses_beijing_day_at_midnight(tmp_path, monkeypatch, host_zone):
    instant = datetime(2026, 10, 25, 16, 0, tzinfo=timezone.utc)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is not None:
                return instant.astimezone(tz)
            return instant.astimezone(ZoneInfo(host_zone)).replace(tzinfo=None)

    seen = []

    def daily_summary(day):
        seen.append(day)
        return SimpleNamespace(avg_heart_rate=0, total_steps=0, sleep_minutes=0)

    service = memory_rag.MemoryRAGService(tmp_path)
    service.health_store = SimpleNamespace(get_daily_summary=daily_summary)
    monkeypatch.setattr(memory_rag, "datetime", Clock)
    service.collect_recent_memory_context(query="health")
    assert seen == ["2026-10-26"]
