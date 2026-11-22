"""verify_ledger_consistency 的「查不了 ≠ 数据错」回归测试（P1 归因）。

背景（2026-10-23 实测）：服务卡死 / 半死时，`/api/daily` 返回 503 或连接超时，
旧实现把「读不到」当成「读到了但金额不对」——17 天日报被全线误判成数据错。
那会让数据门禁变成「服务健康度的回声」，而不是数据正确性的判据。

这里钉住三件事：
1. 接口不可达 / 超时 → rule2/3/4 一律 SLOW，**绝不进 failed_days**；
2. 接口健康但金额真的对不上 → 仍然 FAIL（阳性对照，防止修成放水）；
3. 超时与 503 的原因要能落到 slow_days 的 reason 里，便于归因。

全部离线：`_get` / `_synthesis` / `_event_view_counts` / 规则5 / 规则6 全部打桩。
"""

from __future__ import annotations


import scripts.verify_ledger_consistency as vlc


DAY = "2026-10-17"


def _stub_env(monkeypatch, *, get_result, daily_events: int = 47) -> None:
    """把网络与磁盘依赖都换成确定性桩。"""
    monkeypatch.setattr(vlc, "_get", lambda path, token, timeout=15.0: get_result(path))
    monkeypatch.setattr(vlc, "_token", lambda: "test-token")
    monkeypatch.setattr(vlc, "_event_view_counts", lambda days: {day: daily_events for day in days})
    monkeypatch.setattr(
        vlc,
        "_synthesis",
        lambda day, inject=None: {
            "event_count": 47,
            "expense_analysis": {"total_amount_cny": 26.8},
            "expense_ledger": {"total_amount": 26.8},
        },
    )
    monkeypatch.setattr(vlc, "check_frontend_money", lambda: (vlc.PASS, [], []))
    monkeypatch.setattr(
        vlc,
        "check_duplicate_purchase_windows",
        lambda date_from, date_to: {"status": vlc.PASS, "cluster_count": 0, "clusters": []},
    )


def test_unreachable_api_is_slow_not_failure(monkeypatch):
    """服务半死（HTTP 503）：判 SLOW，失败天数必须为 0。"""
    _stub_env(
        monkeypatch,
        get_result=lambda path: (None, "unavailable", "接口返回 HTTP 503"),
    )

    result = vlc.verify(DAY, DAY)

    row = result["rows"][0]
    assert row["rule2"] == vlc.SLOW
    assert row["rule3"] == vlc.SLOW
    assert row["rule4"] == vlc.SLOW
    assert result["failed_days"] == [], "接口不可达被误判成了数据不一致"
    assert result["verdict"] == vlc.SLOW

    interface = result["interface"]
    assert interface["expense_range_status"] == "unavailable"
    assert interface["daily_unreachable_count"] == 1
    assert interface["daily_unreachable_days"] == [DAY]


def test_timeout_is_slow_and_keeps_reason(monkeypatch):
    """接口超时：同样只许 SLOW，且 reason 里要点明是超时。"""
    _stub_env(
        monkeypatch,
        get_result=lambda path: (None, "timeout", "接口超时未返回（>15s）"),
    )

    result = vlc.verify(DAY, DAY)

    assert result["failed_days"] == []
    assert result["verdict"] == vlc.SLOW
    reasons = " ".join(item.get("reason", "") for item in result["slow_days"])
    assert "超时" in reasons


def test_real_amount_mismatch_is_still_failure(monkeypatch):
    """阳性对照：接口健康、金额真的不一致时，必须判 FAIL（修完不许放水）。"""

    def fake_get(path: str):
        if path.startswith("/api/expense-range"):
            return (
                {"by_day": [{"date": DAY, "total_cny": 26.8, "source": "ledger"}]},
                "ok",
                "",
            )
        return (
            {
                "expense_ledger": {"total_amount": 14.0},
                "events": [{}] * 47,
            },
            "ok",
            "",
        )

    _stub_env(monkeypatch, get_result=fake_get)

    result = vlc.verify(DAY, DAY)

    row = result["rows"][0]
    assert row["rule2"] == vlc.FAIL
    assert result["failed_days"] == [DAY]
    assert result["verdict"] == vlc.FAIL
    assert "26.8" in row["rule2_note"] and "14.0" in row["rule2_note"]


def test_missing_synthesis_file_stays_not_applicable(monkeypatch):
    """没有日报文件的日子仍记 N/A，不能被接口故障改写成 SLOW 或 FAIL。"""
    _stub_env(
        monkeypatch,
        get_result=lambda path: (None, "unavailable", "接口不可达（连接被拒）"),
    )
    monkeypatch.setattr(vlc, "_synthesis", lambda day, inject=None: None)

    result = vlc.verify(DAY, DAY)

    row = result["rows"][0]
    assert row["rule1"] == vlc.NA
    assert result["failed_days"] == []
    assert result["verdict"] == vlc.SLOW


# ---------------------------------------------------------------------------
# HTTP 202 computing：异步租约把「还在算」回成 202，
# 若当成 ok，expense_ledger 缺失 → 金额读成 None → 误判「账本不一致」。
# ---------------------------------------------------------------------------
import io
import json as _json
import urllib.error as _urlerror


class _FakeResponse:
    def __init__(self, body, status=200, headers=None):
        self._body = body if isinstance(body, (bytes, bytearray)) else _json.dumps(body).encode("utf-8")
        self.status = status
        self.headers = headers or {}

    def read(self):
        return self._body

    def getcode(self):
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_get_202_computing_is_not_ok(monkeypatch):
    """202 + status=computing 必须单独成态，且保留 retry_after。"""
    body = {"status": "computing", "error": "read_busy", "retry_after": 4}
    monkeypatch.setattr(vlc, "urlopen", lambda request, timeout=15.0: _FakeResponse(body, status=202))

    payload, status, reason = vlc._get("/api/daily?date=2026-10-17", "tok")

    assert status == "computing", f"202 computing 被当成了 {status}"
    assert "retry_after=4s" in reason
    assert payload["_retry_after"] == 4


def test_get_202_without_retry_after_still_computing(monkeypatch):
    """202 即使不带 retry_after，也不能回落成 ok。"""
    monkeypatch.setattr(
        vlc, "urlopen", lambda request, timeout=15.0: _FakeResponse({"status": "computing"}, status=202)
    )
    _, status, _ = vlc._get("/api/daily", "tok")
    assert status == "computing"


def test_get_503_is_unavailable(monkeypatch):
    """HTTP 503 是环境问题，不能进「数据不一致」。"""

    def raise_503(request, timeout=15.0):
        raise _urlerror.HTTPError(request.full_url, 503, "Service Unavailable", {}, io.BytesIO(b"{}"))

    monkeypatch.setattr(vlc, "urlopen", raise_503)
    _, status, reason = vlc._get("/api/daily", "tok")
    assert status == "unavailable"
    assert "503" in reason


def test_get_connection_refused_is_unavailable(monkeypatch):
    """连接被拒（服务没起）同样是环境问题。"""

    def raise_refused(request, timeout=15.0):
        raise _urlerror.URLError(ConnectionRefusedError(61, "Connection refused"))

    monkeypatch.setattr(vlc, "urlopen", raise_refused)
    _, status, reason = vlc._get("/api/daily", "tok")
    assert status == "unavailable"
    assert "Connection refused" in reason or "不可达" in reason


def test_get_200_non_json_is_unavailable(monkeypatch):
    """200 但响应不是 JSON（代理页 / HTML 错误页）：不能当数据。"""
    monkeypatch.setattr(
        vlc, "urlopen", lambda request, timeout=15.0: _FakeResponse(b"<html>gateway</html>", status=200)
    )
    _, status, reason = vlc._get("/api/daily", "tok")
    assert status == "unavailable"
    assert "不可解析" in reason


def test_computing_marks_rules_slow_and_keeps_retry_after(monkeypatch):
    """computing 下 rule2/3/4 全是 SLOW，失败天数为 0，且 slow_days 带 retry_after。"""
    _stub_env(
        monkeypatch,
        get_result=lambda path: ({"_retry_after": 4}, "computing", "仍在计算 retry_after=4s"),
    )

    result = vlc.verify(DAY, DAY)

    row = result["rows"][0]
    assert row["rule2"] == vlc.SLOW
    assert row["rule3"] == vlc.SLOW
    assert row["rule4"] == vlc.SLOW
    assert result["failed_days"] == [], "202 computing 被误判成数据不一致"
    assert result["verdict"] == vlc.SLOW

    assert result["interface"]["daily_computing_count"] == 1
    assert result["interface"]["daily_computing_days"] == [DAY]
    assert result["slow_days"][0]["retry_after"] == 4


def test_day_without_events_counts_as_zero_not_mismatch(monkeypatch):
    """当天 0 事件时 event_view 里没有这一天，应按 0 算，不能判成三方不一致。"""
    _stub_env(
        monkeypatch,
        get_result=lambda path: (None, "unavailable", "接口返回 HTTP 503"),
    )
    monkeypatch.setattr(vlc, "_event_view_counts", lambda days: {})
    daily = {"event_count": 0, "expense_analysis": {"total_amount_cny": 0}, "expense_ledger": {"total_amount": 0}}
    monkeypatch.setattr(vlc, "_synthesis", lambda day, inject=None: {"event_count": 0, **daily})
    monkeypatch.setattr(
        vlc,
        "_get",
        lambda path, token, timeout=15.0: (
            {"events": [], "expense_ledger": {"total_amount": 0}, "by_day": [], "total_amount": 0},
            "ok",
            "",
        ),
    )

    row = vlc.verify(DAY, DAY)["rows"][0]

    assert row["rule4"] != vlc.FAIL, row["rule4_note"]
