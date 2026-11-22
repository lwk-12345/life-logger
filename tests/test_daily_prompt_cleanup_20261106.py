"""夜间日报提示词清理：无写死内容、cognitive_insight 不编结论、用量被记录。"""
from __future__ import annotations

import io
import json
import re
from pathlib import Path

import pytest

from life_logger import daily_cognitive_session as dcs

SOURCE = Path(dcs.__file__).read_text(encoding="utf-8")
BANNED = ["芒果香烟10元档", "戒烟三天", "前夜缺觉", "CAM显存溢出",
          "¥10.00", "多巴胺代偿", "精力综合健康分", "大肉粒烤肠", "生脉饮党参方"]


@pytest.mark.parametrize("phrase", BANNED)
def test_prompt_has_no_hardcoded_day_content(phrase):
    """系统提示里不许再出现那一天的具体内容（示例值）。"""
    assert phrase not in SOURCE, f"提示词里仍有写死内容：{phrase}"


def test_prompt_keeps_rules_but_no_examples():
    """要求还在（不写例子版），且明确禁止编造/打分。"""
    assert "不举例、不补全" in SOURCE
    assert "不要给出分数" in SOURCE
    assert "只引用记录里的值" in SOURCE


def test_cognitive_insight_is_none():
    """有吸烟记录时也不再输出心理结论文本。"""
    events = [
        {"event_id": "e1", "title": "买烟", "category": "expense",
         "data": {"expense": {"item": "红塔山", "amount": 10}},
         "evidence": {"quote": "红塔山十块"}, "occurred_at": "2026-11-04T21:58:00+08:00"},
        {"event_id": "e2", "title": "抽了一根", "category": "smoking",
         "data": {"smoke": {"count": 1, "brand": "红塔山", "kind": "smoked"}},
         "evidence": {"quote": "抽了一根"}, "occurred_at": "2026-11-04T22:10:00+08:00"},
    ]
    ledger = dcs.build_smoking_ledger(events, day="2026-11-04")
    assert ledger.get("cognitive_insight") is None
    text = json.dumps(ledger, ensure_ascii=False)
    assert "多巴胺" not in text and "承诺戒烟3天" not in text


def test_usage_recorded_on_success(monkeypatch):
    calls = []
    import life_logger.model_usage as mu

    monkeypatch.setattr(mu, "record", lambda *a, **kw: calls.append((a, kw)) or {})

    class FakeResp(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *exc): return False

    body = {"model": "deepseek-test", "messages": [{"role": "user", "content": "x"}]}
    payload = {"choices": [{"message": {"content": "{\"ok\": 1}"}}],
               "usage": {"total_tokens": 12}}

    monkeypatch.setattr(dcs.urllib.request, "urlopen",
                        lambda *a, **kw: FakeResp(json.dumps(payload).encode()))
    parsed, log = dcs._call_model("http://example.invalid/x", "k", body, tier="primary")
    assert parsed == {"ok": 1}
    assert calls, "没有记录用量"
    step = calls[0][0][0]
    assert step == "daily_synthesis"
    assert calls[0][1]["endpoint_kind"] == "primary"
    assert calls[0][1]["model"] == "deepseek-test"


def test_usage_recorded_on_failure(monkeypatch):
    calls = []
    import life_logger.model_usage as mu

    monkeypatch.setattr(mu, "record", lambda *a, **kw: calls.append((a, kw)) or {})

    def boom(*a, **kw):
        raise TimeoutError("timeout")

    monkeypatch.setattr(dcs.urllib.request, "urlopen", boom)
    monkeypatch.setattr(dcs, "_model_attempts", lambda: 1)
    body = {"model": "m", "messages": []}
    with pytest.raises(Exception):
        dcs._call_model("http://example.invalid/x", "k", body, tier="tier2")
    assert calls and calls[0][1]["ok"] is False and calls[0][1]["endpoint_kind"] == "tier2"


def test_no_local_import_of_time():
    """Q923 的坑：函数内 import time 会让 time 变局部变量。"""
    assert not re.search(r"^\s+import time\s*$", SOURCE, re.MULTILINE)
