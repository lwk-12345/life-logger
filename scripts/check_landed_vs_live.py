#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""「已落盘 ≠ 已生效」检查器（Q841s）。

为什么需要
----------
本仓库反复出现同一个事故形态：**改了代码 / 跑了测试 / 写了报告，但对外行为一个字没变**。
两次因为"以为生效"而误判交付（Q839、Q840 的报告都在「剩余问题」里写了"需要重启"）。

本脚本只回答一个问题：**某个符号/字段，现在是「源码里有」还是「进程里有」还是「对外真的能看到」？**

四态判定
--------
  MISSING         源码里根本没有 → 交付没落盘
  PENDING_RESTART 源码有、**运行进程加载的字节码里没有** → 必须重启
  PENDING_REPUBLISH 进程里有，但产物文件是**加载之前**写的 → 必须重跑一次（不是重启）
  LIVE            进程里有，且对外探测真的看到

关键区分：`amount_kind` 是**读时投影**（read surface），查询即生效、磁盘永远没有；
`occurred_at_audit` 是**写时产物**（write surface），只在 publish 时写入，历史文件不补。
两者"没看到"的原因完全不同，混在一起就会得出"都要重启"的错误结论。

只读保证
--------
* 不重启、不写 output/、不改 src/、不触发模型（只发一个 GET）
* token 从 `output/local-web/secrets.json` 内部读取，**绝不打印、绝不写进输出**
* `--json` 输出里也不含 token

用法
----
    python3 scripts/check_landed_vs_live.py                 # 人类可读
    python3 scripts/check_landed_vs_live.py --json          # 机器可读
    python3 scripts/check_landed_vs_live.py --date 2026-11-04
    python3 scripts/check_landed_vs_live.py --no-http       # 离线：跳过 API 探测
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import struct
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
SECRETS = ROOT / "output" / "local-web" / "secrets.json"
JEV = ROOT / "output" / "local-web" / "activity-timeline" / "jev"
BASE_URL = "http://127.0.0.1:8765"

MISSING = "MISSING"
PENDING_RESTART = "PENDING_RESTART"
PENDING_REPUBLISH = "PENDING_REPUBLISH"
LIVE = "LIVE"
LIVE_UNVERIFIED = "LIVE_UNVERIFIED"   # 代码进进程了，但该日期没有可用来验证的数据形状

# ---------------------------------------------------------------- 规格表
# 每条：文件、符号、表面（read=读时投影 / write=写时产物）、探测方式
SPECS: list[dict[str, Any]] = [
    {
        "id": "Q840-amount_kind",
        "title": "items[].amount_kind（读时投影）",
        "file": "src/life_logger/activity_timeline_runtime.py",
        "symbol": "tag_payload",
        "surface": "read",
        "probe": "api_item_field",
        "probe_arg": "amount_kind",
        "note": "只在 read_day 里补算 → API 可见、磁盘永远没有；判 LIVE 必须看 API 不能看文件",
    },
    {
        "id": "Q839-occurred_at_audit",
        "title": "items[].occurred_at_audit + occurred_at_audit_summary（写时产物）",
        "file": "src/life_logger/activity_timeline_runtime.py",
        "symbol": "audit_payload",
        "surface": "write",
        "probe": "file_top_key",
        "probe_arg": "occurred_at_audit_summary",
        "note": "只在 publish_day 里写 → 看文件；历史日期不重跑就永远是 0，这**不是**重启能解决的",
    },
    {
        "id": "SELFTEST-enrich_evidence_source",
        "title": "自检项：evidence.source_label（已知已生效，用来证明检查器本身没坏）",
        "file": "src/life_logger/activity_timeline_runtime.py",
        "symbol": "enrich_evidence_source",
        "surface": "read",
        "probe": "api_evidence_field",
        "probe_arg": "source_label",
        "note": "若此项不是 LIVE，说明检查器/环境有问题，其它结论一律不可信",
    },
]


# ---------------------------------------------------------------- 基础设施
def sh(args: list[str]) -> str:
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=20).stdout.strip()
    except Exception:  # noqa: BLE001
        return ""


def running_pid() -> tuple[int | None, dt.datetime | None]:
    """返回 8765 监听进程 pid 与启动时间（不依赖 bash 解析，安全）。"""
    out = sh(["bash", "-lc", "lsof -tiTCP:8765 -sTCP:LISTEN 2>/dev/null | head -1"])
    if not out.isdigit():
        return None, None
    pid = int(out)
    started = sh(["bash", "-lc", f"ps -o lstart= -p {pid} 2>/dev/null"])
    try:
        when = dt.datetime.strptime(started.strip(), "%a %b %d %H:%M:%S %Y")
    except ValueError:
        when = None
    return pid, when


def interpreter_tag() -> str:
    """本脚本自身解释器的 tag（仅作展示；**不能**用它选 pyc，见 service_tag）。"""
    return f"cpython-{sys.version_info[0]}{sys.version_info[1]}"


def service_executable(pid: int | None) -> str | None:
    if pid is None:
        return None
    cmd = sh(["bash", "-lc", f"ps -o command= -p {pid} 2>/dev/null"]).strip()
    return cmd.split()[0] if cmd else None


def service_tag(pid: int | None) -> str | None:
    """探测**服务进程**用的解释器版本。

    为什么不能用 interpreter_tag()：检查器可能跑在 3.13，而服务跑在 3.11。
    实测踩坑：用自带版本去选 pyc，选中的是**检查器自己 import 时新生成的 3.13 pyc**
    （embedded mtime 恰好等于当前源码 mtime），于是把「已生效」误判成「需要重启」。
    正确做法是从服务进程的命令行里拿解释器路径，问它自己是哪个版本。
    """
    if pid is None:
        return None
    cmd = sh(["bash", "-lc", f"ps -o command= -p {pid} 2>/dev/null"]).strip()
    if not cmd:
        return None
    exe = cmd.split()[0]
    ver = sh(["bash", "-lc", f"'{exe}' -V 2>&1"]).strip()
    match = re.search(r"Python (\d+)\.(\d+)", ver)
    return f"cpython-{match.group(1)}{match.group(2)}" if match else None


def pyc_candidates(src_path: Path) -> list[Path]:
    return sorted((src_path.parent / "__pycache__").glob(f"{src_path.stem}.*.pyc"))


def pick_loaded_pyc(src_path: Path, pref_tag: str | None,
                    proc_start: dt.datetime | None) -> tuple[Path | None, str]:
    """选出**最可能是运行进程加载的那个 pyc**，并返回选择理由。

    优先级：
      1. 服务解释器 tag 完全匹配的 pyc（最可靠）
      2. embedded mtime ≤ 进程启动时间 **且最晚** 的那个（进程只可能加载启动前编好的）
      3. 都没有 → 返回最晚的一个，并在理由里标 `SUSPECT`
    """
    cands = pyc_candidates(src_path)
    if not cands:
        return None, "无任何 pyc"
    if pref_tag:
        for cand in cands:
            if cand.name == f"{src_path.stem}.{pref_tag}.pyc":
                return cand, f"按服务解释器 {pref_tag} 匹配"
    if proc_start is not None:
        eligible = [(pyc_embedded_mtime(c), c) for c in cands]
        eligible = [(t, c) for t, c in eligible if t is not None and t <= proc_start]
        if eligible:
            eligible.sort()
            return eligible[-1][1], "按(embedded ≤ 进程启动)取最晚"
    newest = max(cands, key=lambda c: (pyc_embedded_mtime(c) or dt.datetime.min))
    return newest, "SUSPECT: 没有早于进程启动的 pyc，取最晚的一个（结论不可信）"


def pyc_embedded_mtime(pyc: Path) -> dt.datetime | None:
    try:
        raw = pyc.read_bytes()[:16]
        return dt.datetime.fromtimestamp(struct.unpack("<I", raw[8:12])[0])
    except Exception:  # noqa: BLE001
        return None


def pyc_symbols(pyc: Path) -> set[str]:
    """从 .pyc 里取出全部**名字**（co_names / co_varnames / 字符串常量）。

    为什么不用 `strings | grep`：marshalled 字符串是**长度前缀**存储的，相邻对象紧挨着，
    `strings` 会把尾巴一起带出来（实测得到 `tag_payloadu/`、`enrich_evidence_sourcer`），
    按 token 精确匹配就会**假阴性** —— 我第一次就是这样把"已生效"误判成"需要重启"的。
    这里直接 marshal 反序列化，递归走 code object，拿到的名字是干净的。
    """
    import marshal
    import types as _types

    try:
        blob = pyc.read_bytes()
    except OSError:
        return set()
    try:
        code = marshal.loads(blob[16:])          # 3.7+ 头部固定 16 字节
    except (ValueError, EOFError, TypeError):
        return set()

    found: set[str] = set()
    stack = [code]
    seen = 0
    while stack and seen < 20000:               # 防御性上限
        obj = stack.pop()
        seen += 1
        if isinstance(obj, _types.CodeType):
            found.update(obj.co_names)
            found.update(obj.co_varnames)
            found.update(obj.co_freevars)
            found.update(obj.co_cellvars)
            for const in obj.co_consts:
                if isinstance(const, str):
                    found.add(const)
                elif isinstance(const, _types.CodeType):
                    stack.append(const)
    return found


_token_cache: str | None = None


def _token() -> str | None:
    global _token_cache
    if _token_cache is not None:
        return _token_cache
    try:
        data = json.loads(SECRETS.read_text(encoding="utf-8"))
        _token_cache = str(data.get("mobile_access_token") or "") or None
    except (OSError, ValueError):
        _token_cache = None
    return _token_cache


def api_json(path: str, timeout: float = 20.0) -> dict[str, Any] | None:
    tok = _token()
    if not tok:
        return None
    req = urllib.request.Request(f"{BASE_URL}{path}", headers={"Authorization": f"Bearer {tok}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return None



_BODY_DIFF_SNIPPET = r"""
import marshal, pathlib, sys, types
pyc = pathlib.Path(sys.argv[1]); srcp = pathlib.Path(sys.argv[2])
loaded = marshal.loads(pyc.read_bytes()[16:])
cur = compile(srcp.read_text(), str(srcp), "exec")
def walk(c):
    yield c
    for k in c.co_consts:
        if isinstance(k, types.CodeType):
            yield from walk(k)
def index(code):
    return {c.co_name: c for c in walk(code) if c.co_name != "<module>"}
L, C = index(loaded), index(cur)
only_src = sorted(set(C) - set(L)); only_loaded = sorted(set(L) - set(C))
diff = [n for n in sorted(set(L) & set(C))
        if L[n].co_code != C[n].co_code or L[n].co_names != C[n].co_names
        or len(L[n].co_consts) != len(C[n].co_consts)]
import json; print(json.dumps({"only_in_source": only_src,
                               "only_in_loaded": only_loaded, "body_differs": diff}))
"""


def body_diff(src_path: Path, pyc: Path, svc_executable: str | None) -> dict[str, list[str]] | None:
    """用**服务解释器**重编译当前源码，与该 pyc 逐函数比 co_code。

    这是"到底哪个函数还没生效"的唯一可靠答案 —— 符号级检查会漏掉"同一函数里改了行为"。
    """
    if not svc_executable:
        return None
    try:
        proc = subprocess.run([svc_executable, "-c", _BODY_DIFF_SNIPPET, str(pyc), str(src_path)],
                              capture_output=True, text=True, timeout=60)
        if proc.returncode != 0:
            return None
        return json.loads(proc.stdout.strip().splitlines()[-1])
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------- 探测
def _candidate_days(day: str, extra: int = 3) -> list[str]:
    """先试指定日期，再补几个最近有数据的日子（避免"当天没数据"把结论判死）。"""
    days = [day]
    try:
        recent = sorted((p.stem for p in JEV.glob("20*.json") if p.name[0].isdigit()), reverse=True)
    except OSError:
        recent = []
    for d in recent[:extra]:
        if d not in days:
            days.append(d)
    return days


def probe_api_item_field(day: str, field: str) -> tuple[str, str]:
    """读时字段：有'相关 population'（amount 非空）才算可判；否则 inconclusive。"""
    notes = []
    total_pop = total_hit = 0
    for d in _candidate_days(day):
        payload = api_json(f"/api/activity-timeline?date={d}")
        if not payload:
            continue
        items = [i for a in payload.get("activities") or [] for i in (a.get("items") or [])]
        pop = [i for i in items if i.get("amount") is not None]
        hit = sum(1 for i in pop if field in i)
        total_pop += len(pop); total_hit += hit
        if pop:
            notes.append(f"{d}: {hit}/{len(pop)}")
        if hit:
            return "ok", f"API {', '.join(notes)} 带 {field}"
    if total_pop == 0:
        return "inconclusive", "最近几天都没有 amount 非空的 item（无法用数据验证，仅凭字节码判定）"
    return "fail", f"API {', '.join(notes)} 带 {field}（有 amount 但无该字段）"


def probe_api_evidence_field(day: str, field: str) -> tuple[str, str]:
    for d in _candidate_days(day):
        payload = api_json(f"/api/activity-timeline?date={d}")
        if not payload:
            continue
        evs = [e for a in payload.get("activities") or [] for i in (a.get("items") or [])
               for e in (i.get("evidence") or [])]
        if not evs:
            continue
        hit = sum(1 for e in evs if field in e)
        if hit:
            return "ok", f"API evidence {hit}/{len(evs)} 带 {field}（{d}）"
        return "fail", f"API evidence 0/{len(evs)} 带 {field}（{d}）"
    return "inconclusive", "最近几天都没有 evidence"


def probe_file_top_key(day: str, key: str) -> tuple[str, str]:
    """写时产物：看目标日期的文件；文件比进程旧 → 说明"没重跑过"，不是"没生效"。"""
    path = JEV / f"{day}.json"
    if not path.is_file():
        return "inconclusive", f"{path.name} 不存在"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return "fail", f"{path.name} 读取失败: {type(exc).__name__}"
    mtime = dt.datetime.fromtimestamp(path.stat().st_mtime)
    stamp = mtime.strftime("%m-%d %H:%M:%S")
    if key in data:
        return "ok", f"{path.name}({stamp}) 有 {key}"
    probe_state = os.environ.get("_LVL_STARTED_TS")
    started = dt.datetime.fromtimestamp(float(probe_state)) if probe_state else None
    if started and mtime < started:
        return "fail_stale", (f"{path.name}({stamp}) 没有 {key}，且文件早于进程启动 "
                              f"({started.strftime('%m-%d %H:%M:%S')}) → 该日期从未用新代码重跑")
    return "fail", f"{path.name}({stamp}) 没有 {key}"


PROBES = {
    "api_item_field": probe_api_item_field,
    "api_evidence_field": probe_api_evidence_field,
    "file_top_key": probe_file_top_key,
}


# ---------------------------------------------------------------- 判定
def evaluate(spec: dict[str, Any], day: str, use_http: bool,
             pref_tag: str | None = None,
             proc_start: dt.datetime | None = None) -> dict[str, Any]:
    src_path = ROOT / spec["file"]
    symbol = spec["symbol"]
    row: dict[str, Any] = {"id": spec["id"], "title": spec["title"], "file": spec["file"],
                           "symbol": symbol, "surface": spec["surface"], "note": spec["note"]}

    src_exists = src_path.is_file()
    src_text = src_path.read_text(encoding="utf-8", errors="replace") if src_exists else ""
    row["in_source"] = symbol in src_text
    row["source_mtime"] = (dt.datetime.fromtimestamp(src_path.stat().st_mtime)
                           .strftime("%Y-%m-%d %H:%M:%S")) if src_exists else None

    pyc, pick_reason = pick_loaded_pyc(src_path, pref_tag, proc_start) if src_exists else (None, "")
    row["pyc"] = str(pyc.relative_to(ROOT)) if pyc else None
    row["pyc_pick"] = pick_reason
    row["pyc_embedded"] = (pyc_embedded_mtime(pyc).strftime("%Y-%m-%d %H:%M:%S")
                           if pyc else None)
    row["in_loaded_bytecode"] = bool(pyc and symbol in pyc_symbols(pyc))

    if proc_start is not None:
        os.environ["_LVL_STARTED_TS"] = str(proc_start.timestamp())
    probe = PROBES.get(spec["probe"])
    if probe and use_http:
        try:
            state, detail = probe(day, spec["probe_arg"])
        except Exception as exc:  # noqa: BLE001
            state, detail = "fail", f"探测异常: {type(exc).__name__}: {exc}"
    else:
        state, detail = "inconclusive", "跳过（--no-http）"
    row["probe_state"] = state
    row["live_probe_ok"] = state == "ok"
    row["live_detail"] = detail

    if not row["in_source"]:
        verdict = MISSING
    elif not row["in_loaded_bytecode"]:
        verdict = PENDING_RESTART
    elif state == "ok":
        verdict = LIVE
    elif state == "inconclusive":
        verdict = LIVE_UNVERIFIED
    elif state == "fail_stale":
        verdict = PENDING_REPUBLISH
    else:
        verdict = PENDING_REPUBLISH if spec["surface"] == "write" else PENDING_RESTART
    row["verdict"] = verdict
    return row


def source_newer_than_process(pid_start: dt.datetime | None) -> list[dict[str, str]]:
    """列出现在磁盘上比进程启动更新的 src/ 文件（= 下次重启才会加载的东西）。"""
    if pid_start is None:
        return []
    out = []
    cutoff = pid_start.timestamp()
    for path in sorted((SRC / "life_logger").glob("*.py")):
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if mtime > cutoff:
            out.append({
                "file": str(path.relative_to(ROOT)),
                "mtime": dt.datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S"),
            })
    return out


def default_probe_date() -> str:
    """探测默认日期：**北京时间今天**。

    [2026-11-04 UTC+8] 原来是 `dt.date.today()`，跟随系统时区。机器在 UTC-8 时，
    傍晚之后北京已经跨日 → 默认去探前一天，报告会假阴性。
    """
    return dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).date().isoformat()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="已落盘 vs 已生效 检查器（只读）")
    ap.add_argument("--date", default=default_probe_date(), help="探测用日期（默认北京时间今天）")
    ap.add_argument("--json", action="store_true", help="机器可读输出")
    ap.add_argument("--no-http", action="store_true", help="不访问 API（离线模式）")
    ap.add_argument("--body-diff", action="store_true",
                    help="用服务解释器重编译源码，逐函数比对字节码，回答'到底哪个函数没生效'")
    args = ap.parse_args(argv)

    pid, started = running_pid()
    newer = source_newer_than_process(started)

    svc_tag = service_tag(pid)
    rows = [evaluate(spec, args.date, not args.no_http, svc_tag, started) for spec in SPECS]

    diffs: dict[str, dict[str, list[str]]] = {}
    if args.body_diff:
        exe = service_executable(pid)
        for path in sorted((SRC / "life_logger").glob("*.py")):
            if started is None or path.stat().st_mtime <= started.timestamp():
                continue
            pyc, _why = pick_loaded_pyc(path, svc_tag, started)
            if not pyc:
                continue
            got = body_diff(path, pyc, exe)
            if got:
                diffs[str(path.relative_to(ROOT))] = got

    if args.json:
        print(json.dumps({
            "date": args.date,
            "pid": pid,
            "process_started": started.isoformat() if started else None,
            "interpreter_checker": interpreter_tag(),
            "interpreter_service": svc_tag,
            "checks": rows,
            "source_newer_than_process": newer,
            "body_diff": diffs,
            "verdict_counts": {v: sum(1 for r in rows if r["verdict"] == v)
                               for v in (LIVE, LIVE_UNVERIFIED, PENDING_RESTART, PENDING_REPUBLISH, MISSING)},
        }, ensure_ascii=False, indent=2))
        return 0 if all(r["verdict"] in (LIVE, LIVE_UNVERIFIED) for r in rows) else 1

    print("=" * 84)
    print("已落盘 vs 已生效")
    print("=" * 84)
    print(f"  探测日期      : {args.date}")
    print(f"  监听进程      : pid={pid or '-'}  启动于 {started.strftime('%Y-%m-%d %H:%M:%S') if started else '-'}")
    print(f"  解释器        : 服务={svc_tag or '未探测'}  检查器={interpreter_tag()}")
    if newer:
        print(f"  ⚠ 磁盘上有 {len(newer)} 个 src 文件比进程新（下次重启才加载）：")
        for item in newer[:8]:
            print(f"      {item['mtime']}  {item['file']}")
        if len(newer) > 8:
            print(f"      … 还有 {len(newer) - 8} 个")
    else:
        print("  磁盘源码均不晚于进程启动时间")
    if diffs:
        print()
        print("  逐函数字节码比对（服务解释器重编译 vs 已加载 pyc）：")
        for f, d in diffs.items():
            od = d.get("body_differs") or []
            ns = d.get("only_in_source") or []
            if od or ns:
                print(f"    {f}")
                if ns:
                    print(f"      只在源码里（新函数）: {ns}")
                if od:
                    print(f"      函数体变了（必须重启才生效）: {od}")
            else:
                print(f"    {f}: 无差异")
    print()

    head = f"{'结论':18s} {'条目':32s} {'源文件':10s} {'进程字节码':10s} 探测"
    print(head)
    print("-" * len(head) + "-" * 40)
    for row in rows:
        print(f"{row['verdict']:18s} {row['id']:32s} "
              f"{'有' if row['in_source'] else '无':10s} "
              f"{'有' if row['in_loaded_bytecode'] else '无':10s} {row['live_detail']}")
    print()

    for row in rows:
        if row["verdict"] != LIVE:
            print(f"[{row['id']}] {row['title']}")
            print(f"   源码 mtime : {row['source_mtime']}")
            print(f"   加载字节码 : {row['pyc_embedded']}  ({row['pyc']})  [{row.get('pyc_pick')}]")
            print(f"   表面       : {row['surface']}（{row['note']}）")
            if row["verdict"] == PENDING_RESTART:
                print("   → **需要重启**（进程里的字节码不含该符号）")
            elif row["verdict"] == PENDING_REPUBLISH:
                print("   → **需要重跑该日期**（写时产物，重启也不会补历史）")
            elif row["verdict"] == MISSING:
                print("   → **源码里没有**，交付未落盘")
            print()

    counts = {v: sum(1 for r in rows if r["verdict"] == v)
              for v in (LIVE, LIVE_UNVERIFIED, PENDING_RESTART, PENDING_REPUBLISH, MISSING)}
    print("汇总：" + "  ".join(f"{k}={v}" for k, v in counts.items() if v))
    blocked = [r for r in rows if r["verdict"] == PENDING_RESTART]
    if blocked:
        print("\n建议重启前先跑门禁：")
        print("  python3 scripts/pre_restart_gate.sh" if (ROOT / "scripts/pre_restart_gate.sh").is_file()
              else "  （pre_restart_gate.sh 不存在，跳过）")
        print("重启后自检：")
        print("  python3 scripts/check_landed_vs_live.py")
        print("  # 或：launchctl kickstart -k gui/$(id -u)/com.life-logger.local-web")
    return 0 if not blocked and not any(r["verdict"] == MISSING for r in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
