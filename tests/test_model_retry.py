"""W4：日报生成必须「每级重试 3 次」后才允许判定失败（2026-10-22 用户纠正）。

用户原话：「Open Design 的 deepseek-v4.1-flash 是能够正常用的，只不过它可能不太稳定。
你是不是没有做过这个冗余测试啊？比如说重试两次三次的，你就直接给我发消息了？」

A0 实测反证：该模型连打 3 次 3/3 成功（1.3~1.8s），历史 270 次 `timed out` 是抖动。
所以本文件锁三条：
  1. 抖动（前 2 次超时、第 3 次成功）→ **必须正常出日报**，绝不进 fallback/pending；
  2. 真坏（3 级 × 3 次全败）→ 才落 pending + 通知；
  3. 单次 timeout 不能是 90s（降到 40s），退避 1/3/8s。
"""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest


from life_logger import daily_cognitive_session as dcs

SYNTH = {
    "executive_summary": "今天正常",
    "nutrition_analysis": {"meals_summary": "烧鸭饭"},
    "fitness_analysis": {},
    "habit_and_smoking_analysis": {},
    "expense_analysis": {"total_amount_cny": 12.0, "items_summary": "烧鸭饭"},
    "work_and_cognition_analysis": {},
    "attention_and_energy": {},
    "tomorrow_advice": "继续",
}

EVENTS = [{
    "event_id": "e1", "title": "购买烧鸭饭 (¥12.00)", "categories": ["expense", "food"],
    "time": {"absolute": "2026-10-22T12:00:00+08:00"},
    "evidence": {"quote": "要一份烧鸭饭", "reported_at": "2026-10-22T12:00:00+08:00"},
    "data": {"expense": {"amount": 12.0, "currency": "CNY", "item": "烧鸭饭"}},
}]


class _Resp:
    def __init__(self, content: str):
        self._payload = json.dumps(
            {"choices": [{"message": {"content": content}}]}, ensure_ascii=False
        ).encode()

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _flaky(fail_times: int, *, exc=TimeoutError("the read operation timed out")):
    """前 fail_times 次抛超时，之后成功（模拟服务抖动）。"""
    state = {"calls": 0}

    def _fake(*_args, **_kwargs):
        state["calls"] += 1
        if state["calls"] <= fail_times:
            raise exc
        return _Resp(json.dumps(SYNTH, ensure_ascii=False))

    return _fake, state


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setenv("LIFE_LOGGER_MODEL_BACKOFF", "0")
    monkeypatch.setenv("LIFE_LOGGER_NOTIFY_SCRIPT", "/nonexistent/notify.sh")
    yield


def test_jitter_two_failures_then_success_still_produces_report(tmp_path):
    """最关键的一条：抖动不能判死。"""
    fake, state = _flaky(2)
    with patch.object(dcs, "_get_deepseek_settings", return_value=(
        "https://unit.invalid/v1", "k", "opendesign/deepseek-v4.1-flash",
        "workbuddy-ai/deepseek-v4.1-flash", None,
    )), patch("urllib.request.urlopen", side_effect=fake):
        result = dcs.generate_daily_cognitive_synthesis("2026-10-22", EVENTS, force=True, cache_dir=tmp_path)

    assert state["calls"] == 3, "前两次超时后必须在同一级重试，而不是直接换级/进 fallback"
    assert result.get("model_tier") == "primary"
    assert result.get("pending_synthesis") is None
    assert (tmp_path / "synthesis-2026-10-22.json").is_file()
    assert not (tmp_path / "daily-synthesis" / "pending-2026-10-22.json").exists()


def test_true_failure_needs_three_tiers_times_three_attempts(tmp_path, monkeypatch):
    fake, state = _flaky(10_000)  # 永远失败
    with patch.object(dcs, "_get_deepseek_settings", return_value=(
        "https://unit.invalid/v1", "k", "opendesign/deepseek-v4.1-flash",
        "workbuddy-ai/deepseek-v4.1-flash",
        {"enabled": True, "base_url": "https://tier3.invalid/v1", "api_key": "k3", "model": "deepseek-chat"},
    )), patch("urllib.request.urlopen", side_effect=fake):
        result = dcs.generate_daily_cognitive_synthesis("2026-10-22", EVENTS, force=True, cache_dir=tmp_path)

    assert state["calls"] == 9, "3 级 × 3 次 = 9 次调用后才允许判死"
    assert result["pending_synthesis"] is True
    marker = json.loads((tmp_path / "daily-synthesis" / "pending-2026-10-22.json").read_text(encoding="utf-8"))
    tiers = [row["tier"] for row in marker["errors"]]
    assert tiers == ["primary", "tier2", "tier3"]
    assert all(row["attempts"] == 3 for row in marker["errors"]), "每级都必须用完 3 次重试"
    assert len(marker["errors"][0]["attempt_log"]) == 3
    assert marker["errors"][0]["attempt_log"][0]["ok"] is False
    assert not (tmp_path / "synthesis-2026-10-22.json").exists()


def test_timeout_and_backoff_defaults():
    assert dcs.MODEL_TIMEOUT_SECONDS == 40.0, "90s 硬等会把线程池拖死"
    assert dcs.MODEL_ATTEMPTS == 3
    assert dcs.MODEL_BACKOFF_SECONDS == (1.0, 3.0, 8.0)


def test_timeout_is_env_overridable(monkeypatch):
    monkeypatch.setenv("LIFE_LOGGER_MODEL_TIMEOUT", "12.5")
    assert dcs._model_timeout() == 12.5
    monkeypatch.setenv("LIFE_LOGGER_MODEL_ATTEMPTS", "5")
    assert dcs._model_attempts() == 5


def test_default_tier_order_is_commandcode_then_opendesign(monkeypatch, tmp_path):
    """档位顺序锁定：primary=commandcode-plan → tier2=opendesign → tier3=官方 deepseek-chat。

    2026-10-22 曾一度把 primary 换成 opendesign，用户改主意后要求撤回；
    这条测试防止将来又被"顺手优化"回去。

    [2026-11-02 A0] primary 默认值由 `workbuddy-ai/deepseek-v4.1-flash` 改为
    `commandcode-plan/deepseek/deepseek-v4.1-flash`：WorkBuddy 账号已被封，
    实测 HTTP 403 "WorkBuddy restricted this account for unusual activity"，
    而 commandcode-plan 通道实测 HTTP 200 可用。档位"顺序"契约不变，只是换掉死模型。
    """
    config = tmp_path / "local-config.json"
    config.write_text(json.dumps({"deepseek": {"api_key": "k"}}), encoding="utf-8")
    monkeypatch.setattr(dcs, "PROJECT_ROOT", tmp_path, raising=False)
    monkeypatch.setattr(dcs, "_read_config", lambda: {"deepseek": {"api_key": "k"}}, raising=False)

    _base, _key, model, tier2_model, _fallback = dcs._get_deepseek_settings()
    assert model == "commandcode-plan/deepseek/deepseek-v4.1-flash"
    assert tier2_model == "opendesign/deepseek-v4.1-flash"


def test_timeout_scales_with_prompt_and_is_capped(monkeypatch):
    monkeypatch.delenv("LIFE_LOGGER_MODEL_TIMEOUT", raising=False)
    assert dcs._model_timeout() == dcs.MODEL_TIMEOUT_SECONDS
    assert dcs._model_timeout(20000) == dcs.MODEL_TIMEOUT_SECONDS + 40
    assert dcs._model_timeout(10 ** 7) == 150.0
    monkeypatch.setenv("LIFE_LOGGER_MODEL_TIMEOUT", "12.5")
    assert dcs._model_timeout(10 ** 7) == 12.5
