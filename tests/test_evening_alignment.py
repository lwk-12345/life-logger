"""晚间对账推送（scripts/evening_alignment.py）的行为测试。

守五件事（派单指定的五条）：
  1. 正常：金额/笔数/明细/待确认都来自 `expense_ledger`，并调用 notify 一次；
  2. 零消费：0 笔也照实说（不说假话、不隐藏）；
  3. 接口失败：**绝不发送**（不发「0 元」），退出码 2；
  4. 超过 10 笔：只列 10 笔 + 明确写出「另有 N 笔」；
  5. --dry-run：只打印，不调用 notify。

假 notify 是一个临时 shell 脚本：把收到的参数写进文件、按 FAKE_NOTIFY_RC 退出。
这样既不碰真微信通道，又能验证「退出码跟随 notify.sh」。
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "evening_alignment.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("evening_alignment", SCRIPT)
    assert spec and spec.loader, f"找不到脚本：{SCRIPT}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def ea():
    return _load_module()


@pytest.fixture()
def fake_notify(tmp_path: Path):
    """假 notify.sh：把 headline/body 落到文件，退出码由 FAKE_NOTIFY_RC 控制。"""
    record = tmp_path / "notify-args.txt"

    def make(rc: int = 0) -> Path:
        script = tmp_path / "fake-notify.sh"
        script.write_text(
            "#!/bin/bash\n"
            f'printf "%s\\n" "$@" > "{record}"\n'
            f"exit {rc}\n",
            encoding="utf-8",
        )
        script.chmod(0o755)
        return script

    make.record = record  # type: ignore[attr-defined]
    return make


def _ledger(*, total=0.0, currency="CNY", transactions=(), pending=0, count=None):
    return {
        "total_amount": total,
        "currency": currency,
        "payment_transaction_count": len(transactions) if count is None else count,
        "payment_transactions": list(transactions),
        "pending_count": pending,
    }


def _tx(clock: str, title: str, amount: float) -> dict:
    return {"clock": clock, "title": title, "amount": amount, "currency": "CNY"}


def _daily(ledger: dict) -> dict:
    return {"date": "2026-11-04", "expense_ledger": ledger}


# ───────────────────────────────────────────────── 1) 正常
def test_normal_message_and_single_notify_call(ea, fake_notify, monkeypatch):
    notify = fake_notify(rc=0)
    daily = _daily(_ledger(total=678.6, pending=2, transactions=[
        _tx("09:54", "Apple 礼品卡", 60.0),
        _tx("12:30", "猪杂汤饭", 18.6),
    ]))
    monkeypatch.setattr(ea, "fetch_daily", lambda date, **kw: daily)

    rc = ea.main(["--date", "2026-11-04", "--notify", str(notify)])
    assert rc == 0

    sent = fake_notify.record.read_text(encoding="utf-8")
    assert "678.60元" in sent
    assert "共 2 笔" in sent
    assert "Apple 礼品卡" in sent and "60.00元" in sent
    assert "猪杂汤饭" in sent and "18.60元" in sent
    assert "待确认 2 笔" in sent


def test_notify_exit_code_is_propagated(ea, fake_notify, monkeypatch):
    """退出码必须跟随 notify.sh（1=发送失败、3=SKIPPED），不能自己改写成 0。"""
    for code in (1, 3):
        notify = fake_notify(rc=code)
        monkeypatch.setattr(ea, "fetch_daily",
                            lambda date, **kw: _daily(_ledger(total=1.0, transactions=[_tx("10:00", "水", 1.0)])))
        assert ea.main(["--date", "2026-11-04", "--notify", str(notify)]) == code


# ───────────────────────────────────────────────── 2) 零消费
def test_zero_spending_still_sends_honest_message(ea, fake_notify, monkeypatch):
    notify = fake_notify(rc=0)
    monkeypatch.setattr(ea, "fetch_daily", lambda date, **kw: _daily(_ledger(total=0.0)))

    rc = ea.main(["--date", "2026-11-04", "--notify", str(notify)])
    assert rc == 0
    sent = fake_notify.record.read_text(encoding="utf-8")
    assert "0.00元" in sent
    assert "共 0 笔" in sent
    assert "今天没有记录到消费" in sent
    assert "待确认 0 笔" in sent


# ───────────────────────────────────────────────── 3) 接口失败 → 不发送
def test_fetch_failure_sends_nothing_and_exits_nonzero(ea, fake_notify, monkeypatch):
    notify = fake_notify(rc=0)

    def boom(date, **kw):
        raise ea.FetchError("/api/daily 返回 HTTP 500")

    monkeypatch.setattr(ea, "fetch_daily", boom)
    rc = ea.main(["--date", "2026-11-04", "--notify", str(notify)])

    assert rc == ea.EXIT_FETCH_FAILED == 2
    assert not fake_notify.record.exists(), "接口失败时绝不能调用 notify（更不许发 0 元）"


def test_missing_expense_ledger_is_a_failure_not_zero(ea, fake_notify, monkeypatch):
    """接口通了但没有 expense_ledger（形状变了）→ 同样不发、退出 2。"""
    notify = fake_notify(rc=0)
    monkeypatch.setattr(ea, "fetch_daily", lambda date, **kw: {"date": "2026-11-04"})

    rc = ea.main(["--date", "2026-11-04", "--notify", str(notify)])
    assert rc == 2
    assert not fake_notify.record.exists()


# ───────────────────────────────────────────────── 4) 超过 10 笔只列 10 笔
def test_more_than_ten_transactions_lists_only_ten(ea, fake_notify, monkeypatch):
    notify = fake_notify(rc=0)
    txs = [_tx(f"1{i % 10}:00", f"项目{i}", float(i + 1)) for i in range(13)]
    monkeypatch.setattr(ea, "fetch_daily",
                        lambda date, **kw: _daily(_ledger(total=91.0, transactions=txs)))

    rc = ea.main(["--date", "2026-11-04", "--notify", str(notify)])
    assert rc == 0
    sent = fake_notify.record.read_text(encoding="utf-8")
    listed = [line for line in sent.splitlines() if line.strip().startswith(tuple(f"{i}." for i in range(1, 20)))]
    assert len(listed) == 10, f"应只列 10 笔，实际 {len(listed)}"
    assert "另有 3 笔未列出" in sent
    assert "项目12" not in sent, "第 11 笔之后不应出现在明细里"


def test_custom_top_limits_lines(ea, fake_notify, monkeypatch):
    notify = fake_notify(rc=0)
    txs = [_tx("10:00", f"项目{i}", 1.0) for i in range(5)]
    monkeypatch.setattr(ea, "fetch_daily",
                        lambda date, **kw: _daily(_ledger(total=5.0, transactions=txs)))
    assert ea.main(["--date", "2026-11-04", "--top", "2", "--notify", str(notify)]) == 0
    sent = fake_notify.record.read_text(encoding="utf-8")
    assert "另有 3 笔未列出" in sent


# ───────────────────────────────────────────────── 5) dry-run 不发送
def test_dry_run_does_not_call_notify(ea, fake_notify, monkeypatch, capsys):
    notify = fake_notify(rc=0)
    monkeypatch.setattr(ea, "fetch_daily",
                        lambda date, **kw: _daily(_ledger(total=9.9, transactions=[_tx("10:00", "水", 9.9)])))

    rc = ea.main(["--date", "2026-11-04", "--dry-run", "--notify", str(notify)])
    assert rc == 0
    assert not fake_notify.record.exists(), "dry-run 不得调用 notify"
    out = capsys.readouterr().out
    assert "9.90元" in out and "未调用 notify.sh" in out


# ───────────────────────────────────────────────── 附加：口径与安全
def test_message_uses_ledger_amounts_verbatim(ea):
    """金额只格式化、不重算：接口给多少就写多少（含非整分）。"""
    headline, body = ea.build_message("2026-11-04", _daily(_ledger(total=1234.56, transactions=[
        _tx("08:00", "甲", 1234.56)])))
    assert "1234.56元" in headline
    assert "1234.56元" in body


def test_token_is_never_printed(ea, monkeypatch, capsys, tmp_path):
    secret = "SECRET-TOKEN-SHOULD-NOT-APPEAR-0123456789"
    secrets_file = tmp_path / "secrets.json"
    secrets_file.write_text(json.dumps({"mobile_access_token": secret}), encoding="utf-8")
    monkeypatch.delenv(ea.TOKEN_ENV, raising=False)
    monkeypatch.setattr(ea, "fetch_daily",
                        lambda date, **kw: _daily(_ledger(total=0.0)))

    rc = ea.main(["--date", "2026-11-04", "--dry-run", "--token-file", str(secrets_file)])
    captured = capsys.readouterr()
    assert rc == 0
    assert secret not in (captured.out + captured.err)


def test_default_date_is_beijing_today(ea):
    from datetime import datetime, timezone

    # UTC 的 16:30 已经是北京次日 00:30 —— 必须按 UTC+8 算，不能按宿主时区
    utc_moment = datetime(2026, 11, 4, 16, 30, tzinfo=timezone.utc)
    assert ea.today_beijing(utc_moment) == "2026-11-05"
    utc_moment2 = datetime(2026, 11, 4, 15, 59, tzinfo=timezone.utc)
    assert ea.today_beijing(utc_moment2) == "2026-11-04"
