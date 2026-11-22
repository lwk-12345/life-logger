#!/usr/bin/env python3
"""晚间对账推送（21:00）：把当天记账结果念一遍，走 scripts/notify.sh 发送。

为什么要有这个脚本
------------------
`src/life_logger/openclaw_bridge.py::dispatch_evening_alignment` 往
`/v1/reconciliation/evening-alignment` 发请求，而 8791 桥上**没有这条路由**
（只有 `/v1/tasks`、`/v1/projects`），所以它一直是 404 —— 见
`work/notify-root-20261105/report.md` 第 4 条。本脚本改成读本机只读接口
`/api/daily`，再交给仓库内唯一告警出口 `scripts/notify.sh`
（它会判断**真实送达**，微信失败时弹 Mac 通知）。

三条硬约束
----------
1. **金额只用接口的 `expense_ledger`**，本地不做任何求和/换算 ——
   页面、日报、推送必须是同一套口径。
2. **只读**：只发一个 GET，不写任何文件、不改数据。
3. **读不到就说读不到**：接口失败/缺字段时**绝不发「今天 0 元」**（那是把故障伪装成结论），
   打印原因并以非 0 退出。

用法
----
    python3 scripts/evening_alignment.py                 # 今天（UTC+8），真发
    python3 scripts/evening_alignment.py --dry-run       # 只打印，不发送
    python3 scripts/evening_alignment.py --date 2026-11-04
    python3 scripts/evening_alignment.py --top 5         # 最多列几笔（默认 10）

退出码
------
    0  已发送 / dry-run 正常
    1  notify.sh 返回失败（或未送达）—— 跟随它的退出码
    2  读接口失败（连不上 / 非 200 / JSON 坏 / 缺 expense_ledger）
    3  notify.sh 判定 SKIPPED（未配置通道）—— 跟随它
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BASE_URL = "http://127.0.0.1:8765"
DEFAULT_TOKEN_FILE = ROOT / "output" / "local-web" / "secrets.json"
DEFAULT_NOTIFY = ROOT / "scripts" / "notify.sh"
TOKEN_ENV = "LL_EVENING_ALIGNMENT_TOKEN"
BEIJING = timezone(timedelta(hours=8))

EXIT_OK = 0
EXIT_NOTIFY_FAILED = 1
EXIT_FETCH_FAILED = 2


class FetchError(RuntimeError):
    """读接口失败（连不上 / 非 200 / JSON 坏 / 缺字段）。绝不用它去发「0 元」。"""


def today_beijing(now: datetime | None = None) -> str:
    """默认日期 = 今天（UTC+8）。不用宿主时区，避免机器在 UTC 时跨日出错。"""
    current = now.astimezone(BEIJING) if now is not None else datetime.now(BEIJING)
    return current.strftime("%Y-%m-%d")


def load_token(token_file: Path = DEFAULT_TOKEN_FILE, env: dict[str, str] | None = None) -> str:
    """取访问令牌：优先环境变量（测试/运维用），否则读 secrets.json。

    **本函数与调用方都不打印令牌**。
    """
    source = os.environ if env is None else env
    from_env = str(source.get(TOKEN_ENV) or "").strip()
    if from_env:
        return from_env
    try:
        payload = json.loads(Path(token_file).read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise FetchError(f"读不到令牌文件（{type(error).__name__}）") from None
    token = str((payload or {}).get("mobile_access_token") or "").strip()
    if not token:
        raise FetchError("secrets.json 里没有 mobile_access_token")
    return token


def fetch_daily(date: str, *, base_url: str = DEFAULT_BASE_URL, token: str = "",
                timeout: float = 30.0, opener=None) -> dict:
    """只读 GET `/api/daily?date=<date>`；任何异常都转成 FetchError。"""
    url = f"{base_url.rstrip('/')}/api/daily?date={date}"
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    open_fn = opener or urllib.request.urlopen
    try:
        with open_fn(request, timeout=timeout) as response:
            status = getattr(response, "status", 200)
            body = response.read()
    except urllib.error.HTTPError as error:
        raise FetchError(f"/api/daily 返回 HTTP {error.code}") from None
    except (urllib.error.URLError, OSError, ValueError) as error:
        raise FetchError(f"连不上 /api/daily（{type(error).__name__}）") from None
    if int(status or 0) != 200:
        raise FetchError(f"/api/daily 返回 HTTP {status}")
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        raise FetchError(f"/api/daily 返回的不是合法 JSON（{type(error).__name__}）") from None
    if not isinstance(payload, dict):
        raise FetchError("/api/daily 返回的不是对象")
    return payload


def calories_note(payload: dict) -> str:
    """「今天有几条食物没说重量」的一句话（没有就返回空串，绝不硬凑）。

    数据来自 `/api/daily` 的 `nutrition_ledger`（2 号窗提供 `unweighed_items` /
    `unweighed_candidate_kcal`）。**字段不存在 → 0 条 → 不加这一句**，
    不在这里做任何求和，候选合计只读接口给的 `unweighed_candidate_kcal`。
    """
    nutrition = payload.get("nutrition_ledger") if isinstance(payload, dict) else None
    if not isinstance(nutrition, dict):
        return ""
    items = nutrition.get("unweighed_items")
    count = len(items) if isinstance(items, list) else 0
    if count <= 0:
        return ""
    try:
        total = float(nutrition.get("unweighed_candidate_kcal"))
    except (TypeError, ValueError):
        total = None
    hint = f"候选合计约 {total:.0f} 千卡，" if total is not None else ""
    return f"今天有 {count} 条食物没说重量，{hint}可到「需要我判断」处理。"


def ledger_of(payload: dict) -> dict:
    """取 `expense_ledger`；缺失或不是对象 → FetchError（不猜、不算）。"""
    ledger = payload.get("expense_ledger")
    if not isinstance(ledger, dict):
        raise FetchError("/api/daily 没有 expense_ledger（接口形状变了？）")
    return ledger


def _money(amount, currency: str) -> str:
    """金额只做**格式化**，不做任何运算。"""
    try:
        value = float(amount)
    except (TypeError, ValueError):
        return "金额未知"
    unit = "元" if (currency or "CNY").upper() in {"CNY", "RMB", ""} else f" {currency}"
    return f"{value:.2f}{unit}"


def _transaction_line(index: int, tx: dict, currency: str) -> str:
    clock = str(tx.get("clock") or "").strip()
    name = str(tx.get("title") or tx.get("item") or "未命名").strip()
    amount = _money(tx.get("amount"), str(tx.get("currency") or currency))
    prefix = f"{index}. "
    return f"{prefix}{clock} {name} {amount}".replace("  ", " ").strip()


def build_message(date: str, payload: dict, *, top: int = 10) -> tuple[str, str]:
    """生成 (一句话结论, 明细)。金额/笔数全部取自 `expense_ledger`，本地只做格式化。"""
    ledger = ledger_of(payload)
    currency = str(ledger.get("currency") or "CNY")
    total = ledger.get("total_amount")
    if total is None:
        raise FetchError("expense_ledger.total_amount 缺失")

    count = ledger.get("payment_transaction_count")
    if count is None:
        count = ledger.get("transaction_count")
    transactions = ledger.get("payment_transactions")
    if not isinstance(transactions, list):
        transactions = ledger.get("transactions")
    if not isinstance(transactions, list):
        transactions = []
    if count is None:
        count = len(transactions)

    pending = ledger.get("pending_count")
    if pending is None:
        pending_list = ledger.get("pending_transactions")
        pending = len(pending_list) if isinstance(pending_list, list) else ledger.get(
            "pending_broadcast_count", 0)

    headline = f"【晚间对账】{date} 合计 {_money(total, currency)}，共 {int(count)} 笔"
    lines = [headline]
    if not transactions:
        lines.append("今天没有记录到消费。")
    else:
        shown = transactions[:max(0, int(top))]
        for index, tx in enumerate(shown, 1):
            if isinstance(tx, dict):
                lines.append(_transaction_line(index, tx, currency))
        hidden = len(transactions) - len(shown)
        if hidden > 0:
            lines.append(f"……另有 {hidden} 笔未列出（共 {len(transactions)} 笔）。")
    lines.append(f"待确认 {int(pending or 0)} 笔。")
    note = calories_note(payload)
    if note:
        lines.append(note)
    return headline, "\n".join(lines[1:])


def send_via_notify(headline: str, body: str, *, notify: Path = DEFAULT_NOTIFY,
                    runner=None) -> int:
    """调 scripts/notify.sh；**退出码原样返回**（它是唯一告警出口，语义见其头注释）。"""
    run = runner or subprocess.run
    completed = run([str(notify), headline, body], capture_output=True, text=True)
    output = ((completed.stdout or "") + (completed.stderr or "")).strip()
    if output:
        print(output[-2000:])
    return int(completed.returncode)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="晚间对账推送（读 /api/daily → notify.sh）")
    parser.add_argument("--date", default=None, help="YYYY-MM-DD，默认今天（UTC+8）")
    parser.add_argument("--dry-run", action="store_true", help="只打印，不发送")
    parser.add_argument("--top", type=int, default=10, help="最多列出几笔（默认 10）")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--token-file", default=str(DEFAULT_TOKEN_FILE))
    parser.add_argument("--notify", default=str(DEFAULT_NOTIFY))
    args = parser.parse_args(argv)

    date = args.date or today_beijing()

    try:
        token = load_token(Path(args.token_file))
        payload = fetch_daily(date, base_url=args.base_url, token=token)
        headline, body = build_message(date, payload, top=args.top)
    except FetchError as error:
        # 关键：读不到就不发。绝不发「今天 0 元」把故障伪装成结论。
        print(f"[evening-alignment] 跳过发送：{error}", file=sys.stderr)
        return EXIT_FETCH_FAILED

    print(headline)
    print(body)

    if args.dry_run:
        print("[evening-alignment] --dry-run：未调用 notify.sh")
        return EXIT_OK

    code = send_via_notify(headline, body, notify=Path(args.notify))
    print(f"[evening-alignment] notify.sh 退出码={code}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
