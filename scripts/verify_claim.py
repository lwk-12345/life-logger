#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""verify_claim.py —— 交付取证器：把窗口自述变成可核对的断言。

设计契约见 `work/automation-20261023/DESIGN.md` 第 2.1 节与第 6 节 T2 行：

* 输入 JSON：`window` / `claim`（自述全文）/ `dispatched_at` / `expected`（可选断言列表）。
* 输出 JSON：每条断言 `{id, claim, type, check, verdict, evidence}`，外加四值 summary 与 exit_code。
* 四值判定，**永不合并**：`PASS` / `FAIL` / `UNVERIFIABLE` / `BLOCKED_ENV`。
* 退出码：有 `FAIL` → 1；无 FAIL 但有 `UNVERIFIABLE` → 2；全为 `BLOCKED_ENV` → 3；其余（全 PASS）→ 0。
  唯一不允许的行为是「有 FAIL 却退 0」，那是 todo 里 `dispatch.sh` 与 `api_golden.py` 的病。
* 离线可跑：只用标准库，不联网、不调模型；唯一可能被调用的外部进程是补跑的测试命令（可关）。
* 幂等：同一磁盘状态 + 同一输入 → 完全相同的 stdout（默认不写墙钟时间，`--stamp` 才写）。
* 自述被截断（`…` 收尾 / `(TIMEOUT after` / U+FFFD）→ 全部断言 `UNVERIFIABLE`，绝不对半句话判真假。
* 取证器自身异常 → 该条 `UNVERIFIABLE`，绝不记 PASS；**不许 `except: pass`**。

自述是**不可信数据**：其中的路径、命令、金额一律当字符串处理，命令只有通过白名单形状
（可选环境变量前缀 + python/pytest 可执行体 + 无 shell 元字符）才会被 `shlex.split` 执行，
且 `shell=False`。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

# --------------------------------------------------------------------------- #
# 四值判定与退出码（契约层，改动前先读 DESIGN.md 2.1）
# --------------------------------------------------------------------------- #

PASS = "PASS"
FAIL = "FAIL"
UNVERIFIABLE = "UNVERIFIABLE"
BLOCKED_ENV = "BLOCKED_ENV"
VERDICTS = (PASS, FAIL, UNVERIFIABLE, BLOCKED_ENV)

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_UNVERIFIABLE = 2
EXIT_BLOCKED_ENV = 3

BUDGET_FS = 5.0        # 文件类断言：DESIGN.md 2.1「超时阈值按类型给（文件类 5s）」
BUDGET_TEST = 300.0    # 测试类断言：300s

SCHEMA_VERSION = "claim-verdict-v1"

# 文件名索引要剪掉的目录：体积大、与「自述引用的源文件/产物」无关。
# 刻意**不剪 work/**：自述最常引用 `work/<task>/preflight.cjs`、`pytest.txt` 这类同目录文件名，
# 剪掉就会把真交付判成「文件不存在」（误报），误报比漏报更能杀死工具（DESIGN 4.2 闸 3）。
INDEX_PRUNE = {
    ".git", ".venv", "node_modules", "__pycache__", ".pytest_cache", ".ruff_cache",
    ".mypy_cache", ".models", ".kev", "recordings_test", "watch-crashlogs",
    ".build", "DerivedData", "dist", "backups", "output", "input",
    "watch-rescue", "watch-rescue2", ".agents", ".codex", ".hermes",
}
INDEX_MAX_FILES = 60000


class CheckError(Exception):
    """检查实现体自己崩了——一律 UNVERIFIABLE，不许变成 PASS。"""


# --------------------------------------------------------------------------- #
# 通用小工具
# --------------------------------------------------------------------------- #

def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def _clip(text: str, limit: int = 400) -> str:
    text = re.sub(r"\s+", " ", str(text)).strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def parse_ts(value):
    """解析派单时间：ISO（含 +0800 / Z / 空格分隔）、epoch 秒。失败返回 None。"""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value)).astimezone()
    text = str(value).strip()
    if not text:
        return None
    if re.fullmatch(r"\d{9,11}(\.\d+)?", text):
        return datetime.fromtimestamp(float(text)).astimezone()
    candidate = text.replace("Z", "+00:00")
    candidate = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", candidate)
    try:
        dt = datetime.fromisoformat(candidate)
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S%z", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
            try:
                dt = datetime.strptime(text, fmt)
                break
            except ValueError:
                dt = None
        if dt is None:
            return None
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt


def default_root() -> Path:
    """仓库根 = scripts/ 的父目录；跑在别处时用 --root 覆盖。"""
    return Path(__file__).resolve().parents[1]


def root_validation_error(root: Path) -> str | None:
    """--root 必须像一个仓库根；传错时不能继续验证错误证据。"""
    root = Path(root).expanduser().resolve()
    script = root / "scripts" / "verify_claim.py"
    source = root / "src" / "life_logger"
    missing = []
    if not script.is_file():
        missing.append("scripts/verify_claim.py")
    if not source.is_dir():
        missing.append("src/life_logger/")
    if missing:
        return f"root={root} 缺少 {'、'.join(missing)}"
    return None


class FileIndex:
    """按 basename 反查仓内文件，供「只说文件名，例如 local_web.py:1234」的自述使用。

    懒加载：只有在直接解析失败时才建索引，且剪掉大目录、限上限，避免把 99MB 会话目录
    和 output/ 全走一遍。
    """

    PREFER = ("src/life_logger/", "scripts/", "tests/", "web/", "desktop/")

    def __init__(self, root: Path):
        self.root = root
        self._by_name: dict[str, list[str]] | None = None

    def _build(self) -> dict[str, list[str]]:
        mapping: dict[str, list[str]] = {}
        count = 0
        root = str(self.root)
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames if d not in INDEX_PRUNE)
            for name in sorted(filenames):
                count += 1
                if count > INDEX_MAX_FILES:
                    break
                rel = os.path.relpath(os.path.join(dirpath, name), root)
                mapping.setdefault(name, []).append(rel)
            if count > INDEX_MAX_FILES:
                break
        return mapping

    def lookup(self, name: str) -> list[str]:
        if self._by_name is None:
            self._by_name = self._build()
        hits = self._by_name.get(Path(name).name, [])
        if len(hits) <= 1:
            return hits

        def rank(rel: str) -> tuple[int, int, str]:
            pref = next(
                (i for i, p in enumerate(self.PREFER) if rel.startswith(p)),
                len(self.PREFER),
            )
            return (pref, rel.count("/"), rel)

        return sorted(hits, key=rank)


@dataclass
class Context:
    root: Path
    claim: str
    dispatched_at_raw: object = None
    run_commands: bool = True
    _index: FileIndex | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def index(self) -> FileIndex:
        if self._index is None:
            self._index = FileIndex(self.root)
        return self._index

    @property
    def dispatched_at(self):
        return parse_ts(self.dispatched_at_raw)

    def resolve(self, raw: str) -> Path:
        """把自述里的路径解析成磁盘路径（~ 展开；相对路径先试 root，再试 basename 索引）。"""
        text = str(raw).strip().strip("`'\"")
        text = text.rstrip("，。；、,.;:)）]】>》")
        if not text:
            return self.root / "\x00empty"
        expanded = Path(os.path.expanduser(text))
        if expanded.is_absolute():
            return expanded
        direct = (self.root / expanded)
        if direct.exists():
            return direct
        if "/" not in text:
            hits = self.index.lookup(text)
            if hits:
                return self.root / hits[0]
        return direct


# --------------------------------------------------------------------------- #
# 自述解析：把「它说了什么」抽成候选断言（全部可核对，不做语义判断）
# --------------------------------------------------------------------------- #

TRUNCATION_MARKERS = ("(TIMEOUT after", "(timeout after", "\ufffd")

PATH_EXT = (
    "py pyi md json jsonl js cjs mjs sh bash zsh ts tsx html css txt log tgz tar gz "
    "db sqlite sqlite3 yaml yml toml cfg ini csv png jpg jpeg webp svg pdf lock"
).split()

# 反引号内的内容、以及裸的路径 token
RE_BACKTICK = re.compile(r"`([^`\n]{1,200})`")
RE_PATH = re.compile(
    r"(?:^|[\s(\[（'\"：:,、`「」『』])"
    r"((?:~|\.{1,2}/|/)[^\s`'\"()\[\]（），。；、]+"
    r"|[A-Za-z0-9_.\-]+(?:/[A-Za-z0-9_.\-]+)+"
    r"|[A-Za-z0-9_.\-]+\.(?:" + "|".join(PATH_EXT) + r"))"
)
RE_FILE_LINE = re.compile(
    r"([A-Za-z0-9_./\-]+\.(?:py|md|js|cjs|mjs|sh|ts|html|json))[：:](\d{1,6})(?:\s*[-–]\s*(\d{1,6}))?"
)
RE_TEST_COUNT = re.compile(
    r"(\d{1,6})\s*(?:个)?\s*(?:测试|test[s]?|用例)?\s*"
    r"(?:全部)?\s*(?:通过|全绿|passed|pass|绿)"
    r"|(\d{1,6})\s*passed"
    r"|passed\s*[:：=]?\s*(\d{1,6})",
    re.IGNORECASE,
)
RE_AMOUNT = re.compile(
    r"(?:账本|金额|总额|合计|花了|支出|消费|花费)[^0-9\n]{0,14}(\d+(?:\.\d+)?)"
    r"|(\d+(?:\.\d+)?)\s*(?:元|块|CNY|RMB)"
)
RE_DATE = re.compile(r"(?:(20\d{2})-)?(\d{1,2})-(\d{1,2})")
RE_COUNT_NEAR = re.compile(r"(\d{1,4})\s*(?:套|个|份|张|条|组|files?|items?)")
RE_COMMAND_HINT = re.compile(r"\b(?:PYTHONPATH=\S+\s+)?(?:\.venv/bin/)?(?:python[0-9.]*|pytest)\b[^\n`]*")
RE_CODE_LINE = re.compile(
    r"^\s*(?:"
    r"\d+[-:]\s"            # grep -n / 文件:行: 输出
    r"|```"
    r"|\$ "
    r">>> "
    r"\[dispatch\]"
    r"|[-*]\s*\d+[-:]"
    r")"
)

CJK_RE = re.compile(r"[\u4e00-\u9fff]")
WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{2,}")

# T5 造假 #4：`file.py:1234 的锁` 这种自述没有 `name()` 形状，抽不出符号名，
# 于是 symbol_at 退化成「第 1234 行存在」——任何行号范围内的整数都过，造假白得 PASS。
# 补救不是把整句当关键词（「已改」「的」「并加了」全是噪音），而是取
# **被声称改动的那个名词**：`xxx 的 YYY` / `改动 YYY` 里的 YYY。
# 只认 2 字以上的中文名词块和标识符，噪音动词/虚词在 STOP_TOPIC 里挡掉。
TOPIC_RE = re.compile(r"(?:的|对|在|把|给|修改|改动|改)\s*([A-Za-z_][A-Za-z0-9_]{2,}|[\u4e00-\u9fff]{2,8})")
STOP_TOPIC = {
    "锁外", "锁内", "这样", "这里", "那里", "上面", "下面", "之后", "之前", "现在",
    "问题", "代码", "地方", "位置", "东西", "内容", "情况", "结果", "时候", "地方",
}
# 只在「没抽到 symbol」时才用的每行邻域：声称改的东西应该就落在该行附近。
TOPIC_WINDOW = 3

STOP_KEYS = {
    "的", "了", "是", "在", "和", "与", "或", "这", "那", "就", "都", "要", "不", "我",
    "你", "他", "有", "没", "也", "会", "对", "上", "下", "里", "时", "个", "请", "问",
    "什么", "怎么", "是否", "一下", "这个", "那个", "the", "and", "for", "with", "that",
    "this", "does", "did", "was", "were", "you", "your",
}

# 「我交付了」类动词：只有这类动词才让一条路径成为**可核对时效的产物**
DELIVERY_VERBS = (
    "已创建", "已新增", "已写入", "已改", "已修改", "已修好", "已修", "已提交", "已交付",
    "已产出", "已生成", "已落地", "已补", "已加", "新增了", "改好了", "写好了", "生成了",
    "交付", "提交", "产出", "落盘", "保存到", "输出到", "写进", "改了", "新建",
)
# 「我只是说到/看过」类动词：路径只作为引用，不据此判时效
REFERENCE_VERBS = ("参照", "参考", "依据", "见 ", "见了", "读了", "读的是", "相关", "已有", "沿用", "复用", "看的是")
IN_PROGRESS_VERBS = ("正在", "在做", "进行中", "尚未", "还没", "还在", "将在", "准备", "计划")


def detect_truncation(claim: str) -> tuple[bool, str]:
    """输入完整性：只对**断言边界**判截断，避免把派单前缀里的省略号误判。"""
    if not claim.strip():
        return False, ""
    for marker in TRUNCATION_MARKERS:
        if marker in claim:
            return True, f"自述含截断标记 {marker!r}：输入不完整，不对半句话判真假"
    lines = [ln for ln in claim.splitlines() if ln.strip()]
    if lines:
        tail = lines[-1].rstrip()
        if tail.endswith("…") or tail.endswith("...") or tail.endswith("（后略") or tail.endswith("(后略"):
            return True, "自述末行以省略号收尾：视为被截断，整份自述不予判定"
    return False, ""


class LineIndex:
    """按字符偏移取所属行——把「找这句话在哪一行」从 O(n) 降到 O(log n)。

    原实现对每个抽出的 token 都重扫全文（`for line in claim.splitlines()`），
    16 MB 自述 × 5 万个 token = 数十亿次字符比较。实测 2 MB 自述就要 60 s，
    而派单给的现实输入正是「6 个窗口 16 MB 会话」——这个量级下工具会先超时，
    再被人判定为「跑不动，还是手工看吧」，等于没交付。
    """

    __slots__ = ("text", "starts")

    def __init__(self, text: str) -> None:
        self.text = text
        starts = [0]
        find = text.find
        pos = find("\n")
        while pos != -1:
            starts.append(pos + 1)
            pos = find("\n", pos + 1)
        self.starts = starts

    def at(self, offset: int, limit: int = 240) -> str:
        """返回 offset 所在行的一个**有界**片段。

        必须截断：日志类自述常常整份只有一行（没有换行），
        此时 `找行尾 → 切整行` 会切出 16 MB 字符串，420k 个 token 逐个这么切
        就是 O(n²)。实测这正是 16 MB 自述跑到 92 s 的原因，
        而工具要处理的恰恰是「6 个窗口 16 MB 会话」。
        """
        import bisect

        idx = bisect.bisect_right(self.starts, offset) - 1
        start = self.starts[idx]
        # 只在 start 起的一段上限内找行尾：整份自述只有一行时，
        # `find` 会从 0 一路扫到 16 MB 末尾，79k 次就是几十秒。
        # 找不到换行（返回 -1）等价于「行太长」，走下面的定窗分支。
        end = self.text.find("\n", start, start + limit * 4)
        if end == -1:
            # 行太长（或没有换行）就只取 offset 附近一个窗口，够判断语境即可
            start = max(start, offset - limit)
            end = min(len(self.text), offset + limit)
        return _squeeze(self.text, start, end, limit)


def _squeeze(text: str, start: int, end: int, limit: int) -> str:
    """取 [start,end) 并压缩空白、截到 limit —— 不复制整行再 strip。

    `text[start:end].strip()` 会先复制一份完整的行；日志自述常常一行十几 MB，
    42 万个 token 各复制一次就是几十秒。这里先按 limit 定窗，只在窗口内压空白。
    """
    if end - start > limit * 2:
        end = start + limit * 2
    raw = text[start:end]
    return re.sub(r"\s+", " ", raw).strip()[:limit]


_INDEX_CACHE: dict[int, tuple[str, LineIndex]] = {}


def _line_index(claim: str) -> LineIndex:
    """同一个 claim 只建一次索引（按 id + 长度做键，避免持有旧对象）。"""
    cached = _INDEX_CACHE.get(id(claim))
    if cached and cached[0] is claim:
        return cached[1]
    index = LineIndex(claim)
    if len(_INDEX_CACHE) > 8:
        _INDEX_CACHE.clear()
    _INDEX_CACHE[id(claim)] = (claim, index)
    return index


def _line_of(claim: str, needle: str, limit: int = 240) -> str:
    """取 needle 所在的行；找不到就退回 needle 本身。"""
    if not needle:
        return ""
    index = _line_index(claim)
    offset = query_offset(claim, needle)
    if offset < 0:
        return needle[:limit]
    return index.at(offset, limit)


def query_offset(claim: str, needle: str) -> int:
    return claim.find(needle)


def _line_of_at(claim: str, offset: int, limit: int = 240) -> str:
    """已知字符偏移时直接取行——不留全文搜索。"""
    if offset < 0:
        return ""
    return _line_index(claim).at(offset, limit)


def _has_any(text: str, words) -> str | None:
    for word in words:
        if word in text:
            return word
    return None


def local_clause(line: str, token: str) -> str:
    """取 token 前面那一小段「就近语境」，用来判断它属于哪个动词。

    一行里常同时出现两种声称：
    「交付 `scripts/a.py` 与 `scripts/b.py`，报告在 `work/c/report.md`」
    前两个是真交付，第三个只是**指向一份早先就存在的文件**。
    整行一起看会把 report.md 也按「已交付」要求 mtime —— 这是实测出来的误报，
    而误报比漏报更能杀死工具（DESIGN 4.2 闸 3）。
    所以按「上一个路径 token 之后」切分句，只看本句。
    """
    idx = line.find(token)
    if idx < 0:
        return line
    head = line[:idx]
    # 切在最近的一个路径/分隔符之后：逗号、分号、和/与/及、括号
    cut = max(
        head.rfind("，"), head.rfind(","), head.rfind("；"), head.rfind(";"),
        head.rfind("和"), head.rfind("与"), head.rfind("及"), head.rfind("（"),
        head.rfind("("),
    )
    clause = head[cut + 1:] if cut >= 0 else head
    # 切完只剩一个反引号/引号时不算「有归属的语境」，否则 `scripts/b.py` 前面
    # 那个「与 `」会被当成独立分句、动词丢失，整条交付证据被静默跳过。
    cleaned = clause.strip().strip("`'\" \t")
    return cleaned or line


def evidence_tokens(claim: str) -> dict:
    """自述里「可以拿去核对」的材料有多少：路径 / 文件:行号 / 测试数字 / 命令 / 金额。"""
    return {
        "paths": len(extract_paths(claim)),
        "symbols": len(RE_FILE_LINE.findall(claim)),
        "test_numbers": len(extract_test_claims(claim)),
        "commands": 1 if derive_test_command(claim) else 0,
        "amounts": len(RE_AMOUNT.findall(claim)),
    }


def is_artifact_path(token: str) -> bool:
    """这条 token 是否值得当「产物路径」去 test -e。

    绝对路径只在已知根下才算产物：`~`、仓库根、`/tmp`、`/private/tmp`、`/Users`。
    像 `/api/daily` 这种 HTTP 路由**不是**磁盘路径——拿它去 `test -e` 必然不存在，
    会把真交付判成「目录不存在」（误报），所以它只能进 artifact_ref 的可选引用，
    不进 path_exists。
    """
    token = token.strip()
    if token.startswith("~"):
        return True
    if token.startswith("./") or token.startswith("../"):
        return True
    if token.startswith("/"):
        return token.startswith(("/tmp/", "/private/tmp/", "/Users/", "/Volumes/", "/opt/"))
    return True


def extract_paths(claim: str) -> list[tuple[str, str]]:
    """(原样 token, 出现它的那一行)；保序去重。"""
    found: list[tuple[str, str]] = []
    seen: set[str] = set()

    def push(token: str, source: str) -> None:
        token = token.strip().rstrip("。，；、,.;:)）]】>》")
        if not token or token in seen:
            return
        if "://" in token or token.startswith("#"):
            return
        # 通配符/占位符不可核对
        if any(ch in token for ch in "*?<>{}%$"):
            return
        if token.lower() in {"e.g.", "etc.", "i.e."}:
            return
        seen.add(token)
        found.append((token, source))

    for match in RE_BACKTICK.finditer(claim):
        inner = match.group(1).strip()
        # 反引号里可能是「命令」或「路径」，只取看起来是路径的
        if re.match(r"^(?:~|\.{1,2}/|/)", inner) or re.search(
            r"\.(?:" + "|".join(PATH_EXT) + r")$", inner
        ):
            push(inner, _line_of_at(claim, match.start()))
    for match in RE_PATH.finditer(claim):
        token = match.group(1)
        if re.match(r"^\d+\.\d+", token) and "/" not in token:
            continue  # 版本号 v0.10.0 之类
        push(token, _line_of_at(claim, match.start(1)))
    return found


def extract_test_claims(claim: str) -> list[dict]:
    """抽「N 个测试全绿 / N passed」+ 附近能补跑的命令。"""
    out: list[dict] = []
    for match in RE_TEST_COUNT.finditer(claim):
        number = next((g for g in match.groups() if g), None)
        if not number:
            continue
        out.append(
            {
                "claimed_passed": int(number),
                "line": _line_of(claim, match.group(0)),
                "command": derive_test_command(claim),
            }
        )
    return out


def derive_test_command(claim: str) -> str | None:
    """从自述里找可以补跑的 pytest 命令；形状不安全一律返回 None（→ UNVERIFIABLE）。"""
    for line in claim.splitlines():
        for match in RE_COMMAND_HINT.finditer(line):
            candidate = match.group(0).strip().strip("`")
            if is_safe_command(candidate):
                return candidate
    for match in RE_BACKTICK.finditer(claim):
        candidate = match.group(1).strip()
        if is_safe_command(candidate):
            return candidate
    return None


SAFE_EXE = {"python", "python3", "pytest", "pytest-3", "py.test"}
SAFE_META = set(";|&><$`\\\n\r")


def is_safe_command(command: str) -> bool:
    """命令白名单形状：可选 ENV=VAL 前缀 + python/pytest 可执行体 + 参数里无 shell 元字符。

    自述是不可信输入；这里宁可判 UNVERIFIABLE 也不把自述里的字符串交给 shell。
    """
    command = command.strip()
    if not command or any(ch in command for ch in SAFE_META):
        return False
    if "(" in command or ")" in command:
        return False
    try:
        tokens = shlex.split(command)
    except ValueError:
        return False
    if not tokens:
        return False
    idx = 0
    while idx < len(tokens) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=\S*", tokens[idx]):
        idx += 1
    if idx >= len(tokens):
        return False
    exe = tokens[idx]
    base = os.path.basename(exe)
    if base not in SAFE_EXE and not re.fullmatch(r"python[0-9.]+", base):
        return False
    if base == "python" or re.fullmatch(r"python[0-9.]+", base):
        rest = tokens[idx + 1:]
        if not rest or rest[0] != "-m" or len(rest) < 2 or rest[1] != "pytest":
            return False
    return True


def extract_amount_claims(claim: str, dispatched_at) -> list[dict]:
    out: list[dict] = []
    default_date = None
    if dispatched_at is not None:
        default_date = dispatched_at.date().isoformat()
    for match in RE_AMOUNT.finditer(claim):
        raw = next((g for g in match.groups() if g), None)
        if raw is None:
            continue
        try:
            amount = float(raw)
        except ValueError:
            continue
        line = _line_of(claim, match.group(0))
        date = None
        date_match = RE_DATE.search(line) or RE_DATE.search(claim)
        if date_match:
            year = date_match.group(1) or (str(dispatched_at.year) if dispatched_at else None)
            if year:
                date = f"{int(year):04d}-{int(date_match.group(2)):02d}-{int(date_match.group(3)):02d}"
        out.append(
            {
                "amount": amount,
                "date": date or default_date,
                "line": line,
                # scope 刻意不推断：DESIGN 2.1 要求显式 scope，缺了就是 UNVERIFIABLE
                "scope": None,
            }
        )
    return out


def _topic_tokens(line_text: str, file_token: str) -> list[str]:
    """从「已改 X:1234 的锁：…」里取出被声称改动的名词（这里就是「锁」）。

    symbol_at 抽不到 `name()` 时会退化成「行号存在」，任何文件都能过。
    这个函数提供第二判据：自述说改了「锁」，那这一行附近必须真的出现「锁」。
    刻意宽松（只要求落在 ±TOPIC_WINDOW 行内），因为它是**兜底**判据，
    误报的代价比漏报高（DESIGN 4.2 闸 3）。
    """
    body = line_text.replace(file_token, " ")
    out: list[str] = []
    for match in TOPIC_RE.finditer(body):
        # 先切再截断：名词短语常长成「锁内深拷贝移到锁外」，
        # 先截 6 字会把「移到」这个切点切掉，剩下的前缀比中率极低。
        for piece in re.split(r"移到|改成|换成|改为|放到|搬到|之后|之前", match.group(1)):
            piece = piece.strip()
            # 标识符不截断（fetch_all 截成 fetch_ 就再也比不中）；
            # 中文名词核截 6 字，去掉「锁内深拷贝移到锁」这种长前缀。
            tok = piece if WORD_RE.fullmatch(piece) else piece[:6]
            if len(tok) < 2 or tok in STOP_TOPIC or tok in STOP_KEYS or tok.isdigit():
                continue
            if len(CJK_RE.findall(tok)) < 2 and not WORD_RE.fullmatch(tok):
                continue
            if tok not in out:
                out.append(tok)
    return out[:4]


def _claimed_count(line: str, token: str) -> int | None:
    """从同句里取出「40 套」这类数量，作为目录断言的最小文件数。

    刻意保守（误报比漏报更能杀死工具）：
    * 只认同句、且数量出现在路径 token 之前；
    * 「个」必须写成「个文件/个产物」，因为「改了 3 个脚本，产物在 X/」
      说的是脚本数，不是 X 里有几个文件；
    * 中间不允许再出现另一个路径 token。
    """
    idx = line.find(token)
    if idx <= 0:
        return None
    head = line[:idx]
    matches = [m for m in RE_COUNT_NEAR.finditer(head)]
    if not matches:
        return None
    last = matches[-1]
    unit = re.sub(r"^\d+\s*", "", last.group(0))
    if unit.startswith("个") and "个文件" not in head[last.start():]:
        return None
    # 数量和 token 之间不能再有别的路径（否则它数的是另一个东西）
    between = head[last.end():]
    if extract_paths(between):
        return None
    try:
        value = int(last.group(1))
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _claimed_symbol_count(line: str) -> int | None:
    """Extract "改了 A 文件 3 处" from the same line, if present."""
    match = re.search(r"(\d{1,6})\s*处(?:改动|修改|改动点)?", line)
    if not match:
        return None
    try:
        value = int(match.group(1))
    except ValueError:
        return None
    return value if value > 0 else None


def derive_assertions(ctx: Context) -> list[dict]:
    """没有 expected 时，从自述里抽「它声称的每一条」。

    顺序固定（先时效、后存在、再行号、测试数、金额），保证幂等输出。
    """
    assertions: list[dict] = []
    claim = ctx.claim
    counter = iter(range(1, 10_000))

    def nid() -> str:
        return f"D{next(counter):02d}"

    paths = extract_paths(claim)

    # 0) 交付时效：自述说「已改/已创建/已交付」的路径，必须在派单后被动过。
    #    这是 ③3号「在做基线、无产出」那条漏报的唯一抓手——只说「我在做」时，
    #    路径存在与否毫无意义（文件本来就在），只有 mtime 能证伪。
    #    优先级写死：引用动词（参照/读了）→ 纯引用，跳过；
    #    否则只要有交付动词就查时效，**不被「正在」中和**（「我在做 X，已改 Y」仍然是声称改了 Y）。
    if ctx.dispatched_at is not None:
        for token, line in paths:
            clause = local_clause(line, token)
            if _has_any(clause, REFERENCE_VERBS):
                continue
            verb = _has_any(clause, DELIVERY_VERBS)
            if not verb:
                continue
            assertions.append(
                {
                    "id": nid(),
                    "type": "mtime_after",
                    "path": token,
                    "claim": line,
                    "source": "derived",
                    "trigger": verb,
                }
            )

    # 0b) 全篇只声明「在做/进行中」，一条交付证据都没有 → 不许当成通过。
    #     这是 ③3号 的另一种写法（连路径都不给），只能判 UNVERIFIABLE。
    if ctx.dispatched_at is not None and not any(
        a["type"] == "mtime_after" for a in assertions
    ):
        progress = next(
            (ln.strip() for ln in claim.splitlines() if _has_any(ln, IN_PROGRESS_VERBS)), None
        )
        if progress:
            assertions.append(
                {
                    "id": nid(),
                    "type": "artifact_ref",
                    "paths": [],
                    "claim": progress,
                    "source": "derived",
                    "reason": "自称任务进行中，但全篇没有任何「派单后产出」的交付证据",
                }
            )

    # 1) 路径类：产物/文件是否存在，非空
    for token, line in paths:
        is_file_line = bool(RE_FILE_LINE.fullmatch(token.strip("`")))
        if is_file_line:
            continue
        assertions.append(
            {
                "id": nid(),
                "type": "path_exists",
                "path": token,
                "claim": line,
                "source": "derived",
                # 「已创建 40 套设计，产物在 X/」——只查「X 存在」等于放行：
                # ⑥6号 那次就是这么漏的，目录存在但里面只有 3 个文件。
                # 声称了数量，就必须拿实际文件数去对。
                "min_files": _claimed_count(line, token),
                "min_bytes": 1,
            }
        )

    # 2) 文件:行号 / 函数名
    for match in RE_FILE_LINE.finditer(claim):
        file_token, line_no = match.group(1), int(match.group(2))
        line_text = _line_of(claim, match.group(0))
        symbol = None
        sym_match = re.search(r"`?([A-Za-z_][A-Za-z0-9_]{2,})`?\s*\(\)", line_text)
        if sym_match:
            symbol = sym_match.group(1)
        else:
            # 真实报告里更常见的是 `web/app.js:4186` | `derivedExpenseDiagnostic` 这一形状：
            # 符号名带反引号但**没有括号**（是表格里的一句话，不是调用）。
            # 只认 `name()` 会把它漏成 symbol=None，检查又退化回「行存在」。
            bare = [
                m.group(1) for m in re.finditer(r"`([A-Za-z_][A-Za-z0-9_]{2,})`", line_text)
                if m.group(1) and m.group(1) != file_token.split("/")[-1].split(":")[0]
            ]
            bare = [b for b in bare if b not in STOP_KEYS and not b.endswith(".py")]
            if bare:
                symbol = bare[0]
        assertions.append(
            {
                "id": nid(),
                "type": "symbol_at",
                "file": file_token,
                "line": line_no,
                "symbol": symbol,
                # T5 造假 #4：自述只写「已改 local_web.py:1234 的锁：…加了 inflight 等待」，
                # 没有 `name()` 形状 → symbol 抽出来是 None，检查退化成「第 1234 行存在」，
                # 而**任何**文件在行号范围内都「存在某一行」，于是造假白送 PASS。
                # 所以：没符号名时，改问「自述声称改的那个东西，在这一行吗」。
                "topic": _topic_tokens(line_text, file_token),
                "claim": line_text,
                "source": "derived",
                "strict_span": False,
                "claimed_count": _claimed_symbol_count(line_text),
            }
        )

    # 2b) 声称「改了 N 处」时，单独核对出现次数（组合欺骗不能只靠行号/mtime）。
    for assertion in list(assertions):
        if assertion.get("type") != "symbol_at":
            continue
        claimed_count = assertion.get("claimed_count")
        if assertion.get("symbol") and claimed_count:
            assertions.append(
                {
                    "id": nid(),
                    "type": "symbol_count",
                    "file": assertion["file"],
                    "symbol": assertion["symbol"],
                    "count": claimed_count,
                    "claim": assertion["claim"],
                    "source": "derived",
                }
            )

    # 3) 测试数字
    for item in extract_test_claims(claim):
        assertions.append(
            {
                "id": nid(),
                "type": "test_count",
                "claimed_passed": item["claimed_passed"],
                "command": item["command"],
                "claim": item["line"],
                "source": "derived",
                "timeout": BUDGET_TEST,
            }
        )

    # 4) 金额（缺 scope 就是 UNVERIFIABLE，这是刻意的）
    for item in extract_amount_claims(claim, ctx.dispatched_at):
        assertions.append(
            {
                "id": nid(),
                "type": "amount_equals",
                "amount": item["amount"],
                "date": item["date"],
                "scope": item["scope"],
                "view": None,
                "claim": item["line"],
                "source": "derived",
            }
        )

    # 5) 「40 套设计」这类「数量 + 已创建」但自述里根本没给路径
    if not paths and RE_COUNT_NEAR.search(claim):
        assertions.append(
            {
                "id": nid(),
                "type": "path_exists",
                "path": None,
                "claim": _line_of(claim, RE_COUNT_NEAR.search(claim).group(0)),
                "source": "derived",
                "min_files": None,
                "min_bytes": 1,
            }
        )

    return assertions


# --------------------------------------------------------------------------- #
# 检查实现体：每条都是「一个可执行动作 + 可复述的证据」
# --------------------------------------------------------------------------- #

def _elapsed(start: float) -> float:
    return round(time.monotonic() - start, 3)


def check_path_exists(ctx: Context, a: dict) -> tuple[str, str, str, dict]:
    raw = a.get("path")
    if not raw:
        return (
            UNVERIFIABLE,
            f"path_exists(path=None); root={ctx.root}",
            "自述声称有产物，但没有可解析的路径 → 没有可执行检查",
            {},
        )
    target = ctx.resolve(str(raw))
    check = f"test -e {target}; find {target} -type f | wc -l"
    min_files = a.get("min_files")
    min_bytes = a.get("min_bytes")
    if not target.exists():
        return FAIL, check, f"test -e → 不存在：{target}", {"path": str(target)}
    if target.is_file():
        size = target.stat().st_size
        detail = {"path": str(target), "kind": "file", "bytes": size}
        if min_bytes is not None and size < int(min_bytes):
            return FAIL, check, f"文件存在但 {size} B < 要求 {min_bytes} B", detail
        verdict = PASS
        return verdict, check, f"文件存在，{size} B", detail
    files = [p for p in target.rglob("*") if p.is_file()]
    non_empty = [p for p in files if p.stat().st_size > 0]
    detail = {
        "path": str(target),
        "kind": "dir",
        "files": len(files),
        "non_empty_files": len(non_empty),
    }
    if min_files is not None and len(files) < int(min_files):
        return (
            FAIL,
            check,
            f"目录存在但只有 {len(files)} 个文件，自述声称 ≥{min_files}；空文件 {len(files) - len(non_empty)} 个",
            detail,
        )
    if min_files is None and not files:
        return FAIL, check, "目录存在但为空（0 个文件）", detail
    if not non_empty:
        return FAIL, check, f"目录有 {len(files)} 个文件但全部为空", detail
    return PASS, check, f"目录存在，{len(files)} 个文件（非空 {len(non_empty)}）", detail


def check_artifact_ref(ctx: Context, a: dict) -> tuple[str, str, str, dict]:
    raw_paths = a.get("paths")
    if not raw_paths:
        raw_paths = [p for p, _ in extract_paths(ctx.claim)]
    if not raw_paths:
        reason = a.get("reason") or "自述里没有任何可解析的产物路径 → 无可核对对象"
        return (
            UNVERIFIABLE,
            "claim → 路径抽取（反引号 / 绝对路径 / 带扩展名 token）；"
            "无路径时查「是否有任何派单后产出」",
            reason,
            {},
        )
    check = "for p in <paths>: test -e $p && [ -s $p ]"
    missing, empty, ok = [], [], []
    for item in raw_paths:
        target = ctx.resolve(str(item))
        if not target.exists():
            missing.append(str(item))
        elif target.is_file() and target.stat().st_size == 0:
            empty.append(str(item))
        else:
            ok.append(str(item))
    detail = {"ok": ok, "missing": missing, "empty": empty, "total": len(raw_paths)}
    summary = f"共 {len(raw_paths)} 条：存在 {len(ok)} / 不存在 {len(missing)} / 空 {len(empty)}"
    if missing or empty:
        return FAIL, check, summary + "；缺失=" + ", ".join(missing[:3]), detail
    return PASS, check, summary, detail


def check_mtime_after(ctx: Context, a: dict) -> tuple[str, str, str, dict]:
    raw = a.get("path")
    after_raw = a.get("after", ctx.dispatched_at_raw)
    after = parse_ts(after_raw)
    if after is None:
        return (
            UNVERIFIABLE,
            f"mtime_after(path={raw}, after={after_raw!r})",
            "派单时间无法解析 → 没有时间基准，无从判断「派单后有没有产出」",
            {},
        )
    if not raw:
        return (
            UNVERIFIABLE,
            f"mtime_after(path=None, after={after.isoformat()})",
            "没有给出要检查的路径 → 没有可执行检查",
            {},
        )
    target = ctx.resolve(str(raw))
    check = f"stat -f %m {target}  # 与 {after.isoformat()} 比较"
    if not target.exists():
        return FAIL, check, f"路径不存在：{target}（派单后无产出）", {"path": str(target)}
    if target.is_file():
        candidates = [target]
    else:
        candidates = sorted(p for p in target.rglob("*") if p.is_file())
    if not candidates:
        return FAIL, check, f"{target} 下没有任何文件（派单后无产出）", {"path": str(target)}
    newest = max(candidates, key=lambda p: p.stat().st_mtime)
    newest_dt = datetime.fromtimestamp(newest.stat().st_mtime).astimezone()
    detail = {
        "path": str(target),
        "newest": str(newest),
        "newest_mtime": newest_dt.isoformat(timespec="seconds"),
        "after": after.isoformat(timespec="seconds"),
        "newer_count": sum(
            1 for p in candidates
            if datetime.fromtimestamp(p.stat().st_mtime).astimezone() > after
        ),
        "total": len(candidates),
    }
    if newest_dt <= after:
        return (
            FAIL,
            check,
            f"最新文件 mtime {detail['newest_mtime']} 不晚于派单时间 {detail['after']}"
            f"（{len(candidates)} 个文件全是旧的）",
            detail,
        )
    return (
        PASS,
        check,
        f"最新文件 mtime {detail['newest_mtime']} > 派单 {detail['after']}"
        f"（{detail['newer_count']}/{len(candidates)} 个文件是派单后写的）",
        detail,
    )


PYTEST_SUMMARY = re.compile(
    # 曾经写成 `(?:^|=\s*)(\d+)\s+passed`，只认行首或 "=" 后面的 passed。
    # pytest 的常规汇总长这样：`3 failed, 3 passed in 0.02s` —— passed 在句中，
    # 于是 passed 永远解析成 None，一次**真的跑过**的失败被报成「无 passed 汇总」。
    # 结论碰巧还是 FAIL，但理由是错的；下次换成 `1377 passed` 之外的行形态就会翻。
    r"(\d+)\s+passed|(\d+)\s+failed|(\d+)\s+error", re.IGNORECASE
)


# 展示类开关：把汇总行吃掉，等于让「跑过了」变成「看不出跑没跑」。
_DISPLAY_FLAGS = ("-q", "--quiet", "-v", "--verbose", "--tb")


def _normalize_pytest_tokens(tokens: list[str]) -> list[str]:
    """把 `-q -q` 归一到单个 `-q`（`-qq` 同理）。

    自述里的命令常常自带 `-q`，而 pyproject 的 addopts 也写了 `-q`；
    两者叠成 `-qq` 后 pytest 直接不打印 `N passed, M failed` 汇总行，
    取证器就只能报 UNVERIFIABLE —— 一条真跑过的结果被判「看不出」。
    证据优先：这里保留至多一个静默级别，并追加 --no-header 减少噪音。
    """
    out: list[str] = []
    quiet = 0
    for tok in tokens:
        if tok.startswith("--quiet"):
            quiet += 1
            continue
        if tok.startswith("-") and not tok.startswith("--") and set(tok[1:]) == {"q"}:
            quiet += 1
            continue
        out.append(tok)
    if quiet:
        out.append("-q")
    return out


def _parse_pytest_counts(output: str) -> dict:
    counts = {"passed": None, "failed": 0, "error": 0}
    for match in PYTEST_SUMMARY.finditer(output):
        passed, failed, error = match.groups()
        if passed is not None:
            counts["passed"] = int(passed)
        if failed is not None:
            counts["failed"] = max(counts["failed"], int(failed))
        if error is not None:
            counts["error"] = max(counts["error"], int(error))
    return counts


def check_test_count(ctx: Context, a: dict) -> tuple[str, str, str, dict]:
    claimed = a.get("claimed_passed")
    command = a.get("command")
    timeout = float(a.get("timeout") or BUDGET_TEST)
    if claimed is None:
        return UNVERIFIABLE, "test_count(claimed=None)", "没有可核对的测试数字", {}
    if not command:
        return (
            UNVERIFIABLE,
            f"test_count(claimed={claimed}, command=None)",
            "自述给了数字但没给可复现的命令（或命令形状不安全）→ 无法实跑复现；"
            "绝不用常量比对（本仓真实出现过 83/126/966/55/69 多个数字）",
            {"claimed_passed": claimed},
        )
    if not is_safe_command(str(command)):
        return (
            UNVERIFIABLE,
            f"test_count(claimed={claimed}, command={command!r})",
            "命令未通过白名单形状检查（自述是不可信输入，不交给 shell）",
            {"claimed_passed": claimed},
        )
    if not ctx.run_commands:
        return (
            BLOCKED_ENV,
            f"{command}  (cwd={ctx.root})",
            "--no-run-commands：本次不实跑，测试数字无法复现（不是通过）",
            {"claimed_passed": claimed},
        )
    tokens = _normalize_pytest_tokens(shlex.split(str(command)))
    try:
        proc = subprocess.run(
            tokens,
            cwd=str(ctx.root),
            capture_output=True,
            text=True,
            timeout=timeout,
            shell=False,
        )
    except subprocess.TimeoutExpired:
        return (
            BLOCKED_ENV,
            f"{command}  (timeout={timeout}s)",
            f"实跑超时 {timeout}s → 环境问题，不算造假也不算通过",
            {"claimed_passed": claimed},
        )
    except OSError as exc:
        return (
            BLOCKED_ENV,
            f"{command}",
            f"命令无法启动：{exc}",
            {"claimed_passed": claimed},
        )
    tail = (proc.stdout or "") + (proc.stderr or "")
    counts = _parse_pytest_counts(tail)
    detail = {
        "claimed_passed": claimed,
        "actual_passed": counts["passed"],
        "actual_failed": counts["failed"],
        "actual_error": counts["error"],
        "exit_code": proc.returncode,
        "tail": _clip(tail.strip().splitlines()[-1] if tail.strip() else "", 200),
    }
    check = f"{command}  (cwd={ctx.root}, shell=False, timeout={timeout}s)"
    if counts["passed"] is None and proc.returncode != 0:
        return FAIL, check, f"实跑退出码 {proc.returncode}，无 passed 汇总；尾部={detail['tail']}", detail
    if counts["passed"] is None:
        return (
            UNVERIFIABLE,
            check,
            f"实跑退出码 {proc.returncode} 但解析不出测试汇总；尾部={detail['tail']}",
            detail,
        )
    if proc.returncode != 0 or counts["failed"] or counts["error"]:
        return (
            FAIL,
            check,
            f"自述 {claimed} 全绿，实跑 {counts['passed']} passed / "
            f"{counts['failed']} failed / {counts['error']} error，退出码 {proc.returncode}",
            detail,
        )
    if counts["passed"] != int(claimed):
        return (
            FAIL,
            check,
            f"数字对不上：自述 {claimed}，实跑 {counts['passed']}（退出码 0）",
            detail,
        )
    return (
        PASS,
        check,
        f"实跑复现：{counts['passed']} passed, 0 failed, 退出码 0（与自述一致）",
        detail,
    )


DEF_RE = r"(?:def|class|function|async def|const|let|var)\s+{sym}\b"


def _symbol_spans(lines: list[str], symbol: str) -> dict:
    """符号的三类坐标：定义行、用法行、跨行（定义行 → 下一个顶层定义前一行）。

    只认「词边界出现」——`self._read_inflight` 也算 `_read_inflight` 的用法，
    因为自述常常引的是一个属性而不是一个函数。
    """
    word = re.compile(rf"(?<![\w]){re.escape(symbol)}(?![\w])")
    def_line = re.compile(DEF_RE.format(sym=re.escape(symbol)))
    defined: list[int] = []
    used: list[int] = []
    for idx, text in enumerate(lines, 1):
        if word.search(text):
            used.append(idx)
            if def_line.search(text) or re.match(
                rf"^\s*(?:self\.)?{re.escape(symbol)}\s*[:=]", text
            ):
                defined.append(idx)
    spans: list[list[int]] = []
    for start in defined:
        end = len(lines)
        for jdx in range(start, len(lines)):
            if jdx > start and re.match(r"^(?:def |class |async def )", lines[jdx]):
                end = jdx
                break
        spans.append([start, end])
    return {"defined": defined, "used": used, "spans": spans}


def check_symbol_at(ctx: Context, a: dict) -> tuple[str, str, str, dict]:
    raw = a.get("file")
    line_no = a.get("line")
    symbol = a.get("symbol")
    if not raw:
        return UNVERIFIABLE, "symbol_at(file=None)", "没有文件路径", {}
    target = ctx.resolve(str(raw))
    check = f"sed -n '{line_no}p' {target}  # + grep -n '{symbol or ''}' {target}"
    if not target.exists() or not target.is_file():
        return FAIL, check, f"文件不存在：{target}", {"file": str(target)}
    lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
    if not line_no and not symbol:
        return UNVERIFIABLE, check, "既没给行号也没给函数名 → 没有可核对对象", {}
    detail = {"file": str(target), "line": int(line_no) if line_no else None, "file_lines": len(lines)}
    if symbol:
        info = _symbol_spans(lines, symbol)
        detail.update({"symbol": symbol, "defined_at": info["defined"][:6],
                       "used_at": info["used"][:6], "spans": info["spans"][:6]})
        if not info["used"]:
            return (
                FAIL,
                check,
                f"{target.name} 全文（{len(lines)} 行）没有出现符号 {symbol!r}"
                f"（定义与用法都没有）→ 自述声称改过它不可信",
                detail,
            )
        if line_no:
            line_no = int(line_no)
            if line_no < 1 or line_no > len(lines):
                return (
                    FAIL,
                    check,
                    f"{target.name} 只有 {len(lines)} 行，自述声称的第 {line_no} 行不存在",
                    detail,
                )
            in_span = any(s <= line_no <= e for s, e in info["spans"])
            on_use = line_no in info["used"] or in_span
            detail["line_text"] = _clip(lines[line_no - 1], 160)
            if not on_use:
                where = (
                    f"定义行 {info['defined'][:3]}" if info["defined"]
                    else f"用法行 {info['used'][:3]}"
                )
                return (
                    FAIL,
                    check,
                    f"{symbol!r} 确实在 {target.name} 里（{where}），"
                    f"但不在自述声称的第 {line_no} 行：该行是 "
                    f"{detail['line_text'][:80]!r}",
                    detail,
                )
            return (
                PASS,
                check,
                f"{symbol!r} 命中第 {line_no} 行（{len(lines)} 行文件）："
                f"{detail['line_text'][:120]}",
                detail,
            )
        first = info["defined"][0] if info["defined"] else info["used"][0]
        detail["line"] = first
        detail["line_text"] = _clip(lines[first - 1], 160)
        return (
            PASS,
            check,
            f"符号 {symbol!r} 存在：定义于第 {first} 行（共 {len(info['used'])} 处出现）",
            detail,
        )
    line_no = int(line_no)
    if line_no < 1 or line_no > len(lines):
        return (
            FAIL,
            check,
            f"{target.name} 只有 {len(lines)} 行，自述声称的第 {line_no} 行不存在",
            detail,
        )
    detail["line_text"] = _clip(lines[line_no - 1], 160)
    # 没有符号名（自述写的是「已改 X:1234 的锁」而不是「已改 X:1234 的 yyy()」）时，
    # 只验「第 1234 行存在」等于给造假发通行证：任何行号范围内的整数都过。
    # 补第二判据：自述声称改的那个名词必须落在该行 ±TOPIC_WINDOW 内。
    topic = a.get("topic") or []
    if topic:
        lo = max(1, line_no - TOPIC_WINDOW)
        hi = min(len(lines), line_no + TOPIC_WINDOW)
        near = "\n".join(lines[lo - 1: hi])
        hits = [t for t in topic if t in near]
        detail.update({"topic": topic, "topic_hits": hits,
                       "topic_window": [lo, hi]})
        if not hits:
            return (
                FAIL,
                check,
                f"自述声称改了 {topic}，但第 {line_no} 行附近（{lo}-{hi} 行）"
                f"一处都没出现；该行实际是 {detail['line_text'][:80]!r} → 行号对不上改动",
                detail,
            )
        return (
            PASS,
            check,
            f"第 {line_no} 行附近（{lo}-{hi}）命中自述改动对象 {hits}"
            f"（{len(lines)} 行文件）",
            detail,
        )
    return (
        UNVERIFIABLE,
        check,
        f"第 {line_no} 行非空（{len(lines)} 行文件），但自述没写符号名/改动对象；"
        f"只验到该行非空，无法确认要改的东西在这里：{detail['line_text'][:120]}",
        {"unscoped": True, "only_nonempty": True, **detail},
    )


def check_symbol_count(ctx: Context, a: dict) -> tuple[str, str, str, dict]:
    """Count claimed occurrences of a symbol, e.g. "改了 A 文件 3 处"."""
    raw = a.get("file")
    symbol = str(a.get("symbol") or "").strip()
    claimed = a.get("count", a.get("claimed_count"))
    check = f"grep -c '\\b{symbol}\\b' {raw}"
    if not raw or not symbol:
        return UNVERIFIABLE, "symbol_count(file/symbol=None)", "缺少文件或符号名", {}
    try:
        expected = int(claimed)
    except (TypeError, ValueError):
        return UNVERIFIABLE, check, "缺少可核对的出现次数", {}
    target = ctx.resolve(str(raw))
    if not target.is_file():
        return FAIL, check, f"文件不存在：{target}", {"file": str(target)}
    lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
    info = _symbol_spans(lines, symbol)
    actual = len(info["used"])
    detail = {"file": str(target), "symbol": symbol, "claimed": expected,
              "actual": actual, "used_at": info["used"][:20]}
    if actual != expected:
        return (
            FAIL,
            check,
            f"自述声称 {symbol!r} 改了 {expected} 处，实际全文出现 {actual} 处"
            f"（定义 {len(info['defined'])} 处）→ 改动数量对不上",
            detail,
        )
    return PASS, check, f"{symbol!r} 实际出现 {actual} 处，与自述一致", detail


def _ledger_path(ctx: Context, date: str, scope: str) -> Path | None:
    base = ctx.root / "output" / "local-web"
    if scope == "settled":
        return base / "daily-synthesis" / f"synthesis-{date}.json"
    return None


def _provisional_snapshot(ctx: Context, date: str) -> tuple[Path | None, list[dict]]:
    """离线读 provisional 账本：output/local-web/cache 下的接口快照。

    t2 当初把 provisional 判成「只由在线接口提供、离线无产物 → BLOCKED_ENV」。
    T5 演练发现这个前提是错的：磁盘上躺着一批
    expense-day-index-provisional-display-*.json，每个都带 generated_at。
    既然有产物，就不能再拿「环境缺失」当挡箭牌——那不是环境缺失，是没去看。

    返回 (最新快照路径, 该日期所有出现过不同取值的分布)。
    同一份数据在不同时刻有两个值（26.8 / 53.6），这本身就是最该暴露给 A0 的事实。
    """
    cache = ctx.root / "output" / "local-web" / "cache"
    if not cache.is_dir():
        return None, []
    rows: list[tuple[str, Path, float]] = []
    for path in sorted(cache.glob("expense-day-index-provisional-display-*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if data.get("scope") != "provisional":
            continue
        day = (data.get("days") or {}).get(date) or {}
        value = day.get("total_cny")
        if value is None:
            continue
        rows.append((str(data.get("generated_at") or ""), path, value))
    if not rows:
        return None, []
    rows.sort(key=lambda item: item[0])
    seen: dict[float, str] = {}
    for generated_at, path, value in rows:
        seen.setdefault(float(value), generated_at)
    dist = [
        {"value": value, "first_seen": seen[value], "occurrences": sum(
            1 for _, _, v in rows if float(v) == value)}
        for value in sorted(seen)
    ]
    return rows[-1][1], dist


def check_amount_equals(ctx: Context, a: dict) -> tuple[str, str, str, dict]:
    claimed = a.get("amount")
    date = a.get("date")
    scope = a.get("scope")
    view = a.get("view")
    if claimed is None:
        return UNVERIFIABLE, "amount_equals(amount=None)", "没有可核对的金额", {}
    if not scope:
        return (
            UNVERIFIABLE,
            f"amount_equals(date={date}, scope=None)",
            "断言没写 scope：/api/daily 的 settled 与 provisional 都叫账本，"
            "不写 scope 等于让「随时会变的数」冒充「定下来的数」→ 不做判定",
            {"claimed": claimed, "date": date},
        )
    if not date:
        return (
            UNVERIFIABLE,
            f"amount_equals(scope={scope}, date=None)",
            "没有日期 → 无法定位那一天的账本",
            {"claimed": claimed},
        )
    ledger = Path(a["ledger"]) if a.get("ledger") else _ledger_path(ctx, str(date), scope)
    check = f"python -c 'json.load(open({ledger}))[\"expense_ledger\"][\"total_amount\"]'"
    if scope == "provisional" and not a.get("ledger"):
        # T5 造假 #5：t2 把 provisional 判成「离线无产物」，实测磁盘上有 41 份快照。
        # 有产物就必须读；读出来的分布还要一并交代（26.8 与 53.6 都出现过）。
        snap, dist = _provisional_snapshot(ctx, str(date))
        check = (
            f"ls {ctx.root}/output/local-web/cache/expense-day-index-provisional-display-*.json"
            f" | sort by generated_at  # 取最新一份的 days.{date}.total_cny"
        )
        if snap is None:
            return (
                BLOCKED_ENV,
                check,
                f"cache 下没有覆盖 {date} 的 provisional 快照 → 环境性缺失，"
                f"既不是通过也不是造假",
                {"claimed": claimed, "date": date, "scope": scope},
            )
        snap_data = json.loads(snap.read_text(encoding="utf-8"))
        day = (snap_data.get("days") or {}).get(str(date)) or {}
        actual = day.get("total_cny")
        detail = {
            "claimed": claimed, "actual": actual, "date": date, "scope": scope,
            "view": view, "snapshot": str(snap),
            "generated_at": snap_data.get("generated_at"),
            "distinct_values": dist,
        }
        if actual is None:
            return (
                UNVERIFIABLE, check,
                f"最新 provisional 快照 {snap.name} 里没有 {date} 的 total_cny",
                detail,
            )
        if abs(float(claimed) - float(actual)) <= 0.005:
            extra = ""
            if len(dist) > 1:
                extra = (
                    f"；注意同一天历史上有 {len(dist)} 个取值 {dist}，"
                    f"自述取的是最新那份"
                )
            return (
                PASS, check,
                f"{date} provisional 最新快照 total_cny={actual}"
                f"（generated_at={detail['generated_at']}），与自述 {claimed} 一致{extra}",
                detail,
            )
        return (
            FAIL, check,
            f"{date} provisional 最新快照 total_cny={actual}"
            f"（generated_at={detail['generated_at']}），自述 {claimed} 对不上"
            f"（差 {round(abs(float(actual) - float(claimed)), 2)}）",
            detail,
        )
    if ledger is None or not Path(ledger).exists():
        return (
            BLOCKED_ENV,
            check,
            f"账本产物不存在：{ledger}",
            {"claimed": claimed, "date": date, "scope": scope},
        )
    try:
        data = json.loads(Path(ledger).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return (
            BLOCKED_ENV,
            check,
            f"账本产物无法解析：{_clip(exc, 160)}",
            {"ledger": str(ledger)},
        )
    ledger_block = data.get("expense_ledger") or {}
    actual = ledger_block.get("total_amount")
    detail = {
        "claimed": claimed,
        "actual": actual,
        "date": date,
        "scope": scope,
        "view": view,
        "policy_version": ledger_block.get("policy_version"),
        "event_count": data.get("event_count"),
        "ledger": str(ledger),
    }
    # 同一份 synthesis 里还有 expense_analysis.total_amount_cny，口径不同却都叫「金额」，
    # 一并带出来供人判断，不作为本断言的判据。
    other = (data.get("expense_analysis") or {}).get("total_amount_cny")
    if other is not None:
        detail["expense_analysis_total_amount_cny"] = other
    if actual is None:
        return (
            UNVERIFIABLE,
            check,
            f"{Path(ledger).name} 里没有 expense_ledger.total_amount 字段",
            detail,
        )
    try:
        actual_f = float(actual)
    except (TypeError, ValueError):
        return (
            UNVERIFIABLE,
            check,
            f"账本 total_amount={actual!r} 不是数字",
            detail,
        )
    if abs(float(claimed) - actual_f) <= 0.005:
        return (
            PASS,
            check,
            f"{date} {scope} 账本 total_amount={actual_f}，与自述 {claimed} 一致"
            f"（policy_version={detail['policy_version']}）",
            detail,
        )
    return (
        FAIL,
        check,
        f"{date} {scope} 账本 total_amount={actual_f}，自述 {claimed} 对不上"
        f"（差 {round(abs(actual_f - float(claimed)), 2)}）",
        detail,
    )


def _keywords(text: str) -> set[str]:
    keys = {w.lower() for w in WORD_RE.findall(text) if len(w) >= 4}
    cjk = "".join(CJK_RE.findall(text))
    for i in range(len(cjk) - 1):
        keys.add(cjk[i: i + 2])
    return {k for k in keys if k not in STOP_KEYS}


def _split_prose(claim: str) -> tuple[list[str], int, int]:
    """返回（散文行，代码行数，总行数）。代码行/输出行不算「回答」。"""
    prose: list[str] = []
    code_lines = 0
    total = 0
    in_fence = False
    for line in claim.splitlines():
        if not line.strip():
            continue
        total += 1
        if line.strip().startswith("```"):
            in_fence = not in_fence
            code_lines += 1
            continue
        if in_fence:
            code_lines += 1
            continue
        if RE_CODE_LINE.match(line) or line.lstrip().startswith(("def ", "import ", "from ", "class ")):
            code_lines += 1
            continue
        stripped = line.strip()
        # 纯符号/纯标点/纯列表符号
        if not re.search(r"[A-Za-z\u4e00-\u9fff]", stripped):
            code_lines += 1
            continue
        prose.append(stripped)
    return prose, code_lines, total


def check_answers_question(ctx: Context, a: dict) -> tuple[str, str, str, dict]:
    question = a.get("question")
    if not question:
        return (
            UNVERIFIABLE,
            "answers_question(question=None)",
            "没有给出被回答的问句 → 无法判断「有没有回答问题」",
            {},
        )
    prose, code_lines, total = _split_prose(ctx.claim)
    prose_text = " ".join(prose)
    cjk = len(CJK_RE.findall(prose_text))
    words = len(WORD_RE.findall(prose_text))
    qkeys = _keywords(str(question))
    hits = {k for k in qkeys if k in prose_text or k in prose_text.lower()}
    ratio = round(len(hits) / len(qkeys), 3) if qkeys else 0.0
    code_ratio = round(code_lines / total, 3) if total else 1.0
    detail = {
        "question": _clip(question, 160),
        "prose_lines": len(prose),
        "prose_cjk_chars": cjk,
        "prose_words": words,
        "code_lines": code_lines,
        "total_lines": total,
        "code_ratio": code_ratio,
        "keyword_hits": sorted(hits)[:12],
        "keyword_ratio": ratio,
    }
    check = (
        "claim → 剔除代码块/命令行输出后统计散文内容（CJK 字数、英文词数）、"
        "与问句关键词重合率、代码行占比"
    )
    substantive = cjk >= 60 or words >= 25
    if not substantive:
        return (
            FAIL,
            check,
            f"没有实质回答：散文仅 {len(prose)} 行 / {cjk} 个汉字 / {words} 个英文词，"
            f"代码与输出行占 {code_ratio:.0%}（典型「只回 grep 行号」）",
            detail,
        )
    if code_ratio > 0.5:
        return (
            FAIL,
            check,
            f"回答被输出淹没：代码/命令行输出 {code_lines}/{total} 行（{code_ratio:.0%}），"
            f"散文 {cjk} 字",
            detail,
        )
    if qkeys and len(hits) == 0:
        return (
            FAIL,
            check,
            f"散文 {cjk} 字但没有命中问句的任何关键词（问句 {len(qkeys)} 个键）→ 疑似答非所问",
            detail,
        )
    # 0.25 这条阈值实测误杀过真回答：一份 60+ 字的正经回答与问句关键词重合 23%，
    # 差 2 个百分点被判「疑似跑题」。误报比漏报更能杀死工具（DESIGN 4.2 闸 3），
    # 所以降到 0.2；「只回 grep 行号」那种仍由上面的实质性与上面的代码占比拦下，
    # 不依赖这条阈值。
    if qkeys and ratio < 0.2:
        return (
            FAIL,
            check,
            f"与问句关键词重合 {ratio:.0%}（{len(hits)}/{len(qkeys)}），低于 20% → 疑似跑题",
            detail,
        )
    return (
        PASS,
        check,
        f"有实质回答：散文 {len(prose)} 行 / {cjk} 字，关键词重合 {ratio:.0%}，"
        f"代码占 {code_ratio:.0%}",
        detail,
    )


CHECKS = {
    "path_exists": check_path_exists,
    "artifact_ref": check_artifact_ref,
    "mtime_after": check_mtime_after,
    "test_count": check_test_count,
    "symbol_at": check_symbol_at,
    "symbol_count": check_symbol_count,
    "amount_equals": check_amount_equals,
    "answers_question": check_answers_question,
}

BUDGETS = {
    "path_exists": BUDGET_FS,
    "artifact_ref": BUDGET_FS,
    "mtime_after": BUDGET_FS,
    "symbol_at": BUDGET_FS,
    "symbol_count": BUDGET_FS,
    "amount_equals": BUDGET_FS,
    "answers_question": BUDGET_FS,
    "test_count": BUDGET_TEST,
}


# --------------------------------------------------------------------------- #
# 断言规范化 + 调度
# --------------------------------------------------------------------------- #

def normalize_expected(entry: dict, position: int) -> dict:
    if not isinstance(entry, dict):
        return {
            "id": f"A{position:02d}",
            "type": str(entry),
            "claim": str(entry),
            "source": "expected",
            "_invalid": "断言不是对象",
        }
    out = dict(entry)
    out.setdefault("id", f"A{position:02d}")
    out.setdefault("type", "")
    out.setdefault("claim", out.get("path") or out.get("file") or "")
    out.setdefault("source", "expected")
    # 允许 expected 里只写 type + 参数，claim 原话缺省时退回自述里含该路径的行
    return out


def _claim_original(ctx: Context, assertion: dict) -> str:
    """输出契约要求每条断言都带「原话」。

    expected 里通常只写 type + 参数，没有原话；这时不能留空——空的 claim 等于
    让下游看到一个没有出处、却自称有证据的判定。按参数在自述里找回对应那句，
    真的找不到才退回整段自述（截断），绝不留空字符串。
    """
    explicit = assertion.get("claim")
    if explicit:
        return _clip(str(explicit), 300)
    for key in ("path", "file", "question"):
        value = assertion.get(key)
        if not value:
            continue
        for line in ctx.claim.splitlines():
            if str(value) in line:
                return _clip(line.strip(), 300)
    amount = assertion.get("amount")
    if amount is not None:
        for line in ctx.claim.splitlines():
            if str(amount) in line:
                return _clip(line.strip(), 300)
    return _clip(ctx.claim.strip(), 300)


def run_assertion(ctx: Context, assertion: dict, truncated: bool, trunc_reason: str) -> dict:
    base = {
        "id": assertion.get("id"),
        "claim": _claim_original(ctx, assertion),
        "type": assertion.get("type"),
        "source": assertion.get("source", "expected"),
    }
    if truncated:
        base.update(
            {
                "check": "input_integrity: 检查自述完整性（尾部/标记）",
                "verdict": UNVERIFIABLE,
                "evidence": trunc_reason,
            }
        )
        return base
    if assertion.get("_invalid"):
        base.update(
            {
                "check": "normalize_expected",
                "verdict": UNVERIFIABLE,
                "evidence": assertion["_invalid"],
            }
        )
        return base
    atype = assertion.get("type")
    if atype not in CHECKS:
        base.update(
            {
                "check": f"registry lookup: {atype!r}",
                "verdict": UNVERIFIABLE,
                "evidence": f"未知断言类型 {atype!r}，本取证器没有对应的可执行检查",
            }
        )
        return base
    start = time.monotonic()
    try:
        verdict, check, evidence, detail = CHECKS[atype](ctx, assertion)
    except Exception:  # 取证器自己崩了 → UNVERIFIABLE，绝不 PASS
        base.update(
            {
                "check": f"{atype}(...)",
                "verdict": UNVERIFIABLE,
                "evidence": "取证器异常：" + _clip(traceback.format_exc().strip().splitlines()[-1], 240),
            }
        )
        return base
    elapsed = _elapsed(start)
    budget = float(assertion.get("budget") or BUDGETS.get(atype, BUDGET_FS))
    if elapsed > budget:
        base.update(
            {
                "check": check,
                "verdict": BLOCKED_ENV,
                "evidence": f"超时：耗时 {elapsed}s > 预算 {budget}s（环境问题，不算造假也不算通过）",
            }
        )
        return base
    if verdict not in VERDICTS:
        verdict = UNVERIFIABLE
        evidence = f"检查实现体返回了非法判定 {verdict!r}"
    # DESIGN 4.1：没有 check 或没有 evidence 的 PASS 一律无效
    if verdict == PASS and (not check.strip() or not evidence.strip()):
        verdict = UNVERIFIABLE
        evidence = "缺 check/evidence 字段的 PASS 视为无效"
    base.update({"check": check, "verdict": verdict, "evidence": _clip(evidence, 600)})
    if detail:
        base["detail"] = detail
    return base


def summarize(results: list[dict]) -> dict:
    counts = {v: 0 for v in VERDICTS}
    for item in results:
        counts[item["verdict"]] = counts.get(item["verdict"], 0) + 1
    return {
        "PASS": counts[PASS],
        "FAIL": counts[FAIL],
        "UNVERIFIABLE": counts[UNVERIFIABLE],
        "BLOCKED_ENV": counts[BLOCKED_ENV],
        "total": len(results),
    }


def exit_code_for(summary: dict) -> int:
    """把汇总计数翻译成进程退出码。

    优先级（高 → 低）：**FAIL(1) > UNVERIFIABLE(2) > BLOCKED_ENV(3) > PASS(0)**。

    【2026-10-23 R2 修复】原来的判定是
    `total > 0 and BLOCKED_ENV == total → 3`，于是
    `{PASS: 1, BLOCKED_ENV: 1}` 这种**混判**会一路掉到 `return EXIT_OK`，
    等于"一条通过 + 一条因为环境缺失根本没验"被判成全部通过 ——
    最省钱的检查带着最典型的假通过（t6 的 mixed.json 实测退 0，期望 3）。

    现在的规则：只要有 FAIL 就退 1；否则只要有 UNVERIFIABLE 就退 2；
    否则**只要还有 BLOCKED_ENV（哪怕只有一条）就退 3**；全部 PASS 才退 0。
    "零断言" 仍然是"什么都没核对"，退 2，绝不退 0。
    """
    if summary["FAIL"] > 0:
        return EXIT_FAIL
    if summary["UNVERIFIABLE"] > 0:
        return EXIT_UNVERIFIABLE
    if summary["total"] == 0:
        # 一条断言都没有 = 什么都没核对。这不是通过。
        return EXIT_UNVERIFIABLE
    if summary["BLOCKED_ENV"] > 0:
        return EXIT_BLOCKED_ENV
    return EXIT_OK


def verify(doc: dict, root=None, run_commands: bool = True, stamp: bool = False) -> dict:
    if not isinstance(doc, dict):
        doc = {"claim": str(doc)}
    root_path = Path(root).expanduser().resolve() if root else default_root()
    claim = doc.get("claim") or ""
    if not isinstance(claim, str):
        claim = json.dumps(claim, ensure_ascii=False)
    ctx = Context(
        root=root_path,
        claim=claim,
        dispatched_at_raw=doc.get("dispatched_at"),
        run_commands=run_commands,
    )
    expected = doc.get("expected") or []
    assertions: list[dict] = []
    if expected:
        if not isinstance(expected, list):
            assertions = [
                {"id": "A01", "type": "invalid", "claim": str(expected), "source": "expected",
                 "_invalid": "expected 必须是数组"}
            ]
        else:
            for i, entry in enumerate(expected, 1):
                assertions.append(normalize_expected(entry, i))
    else:
        assertions = derive_assertions(ctx)

    truncated, trunc_reason = detect_truncation(claim)
    if truncated:
        ctx.notes.append(trunc_reason)
    results = [run_assertion(ctx, a, truncated, trunc_reason) for a in assertions]

    if not results:
        results = [
            {
                "id": "D00",
                "claim": _clip(claim, 200),
                "type": "(none)",
                "source": "derived",
                "check": "claim → 断言抽取（路径 / 文件:行号 / 测试数字 / 金额 / 问句）",
                "verdict": UNVERIFIABLE,
                "evidence": "自述里抽不出任何可执行的断言 → 没有核对过任何东西，"
                            "这不是通过（UNVERIFIABLE ≠ PASS）",
            }
        ]

    summary = summarize(results)
    report = {
        "schema_version": SCHEMA_VERSION,
        "window": doc.get("window"),
        "root": str(root_path),
        "dispatched_at": doc.get("dispatched_at"),
        "claim_sha256": _sha256(claim),
        "summary": summary,
        "exit_code": exit_code_for(summary),
        "assertions": results,
    }
    if ctx.notes:
        report["notes"] = ctx.notes
    if stamp:
        report["checked_at"] = _now_iso()
    return report


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def _render_human(report: dict) -> str:
    lines = [
        f"window={report.get('window')}  root={report.get('root')}",
        f"claim_sha256={report['claim_sha256'][:16]}  exit={report['exit_code']}",
        "verdicts: " + "  ".join(f"{k}={v}" for k, v in report["summary"].items()),
        "",
    ]
    for item in report["assertions"]:
        lines.append(f"[{item['verdict']:<12}] {item.get('id')} {item.get('type')}")
        lines.append(f"    claim:  {item.get('claim')}")
        lines.append(f"    check:  {item.get('check')}")
        lines.append(f"    evidence: {item.get('evidence')}")
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="交付取证器：把窗口自述变成可核对的断言（离线、无模型）。"
    )
    parser.add_argument("--input", "-i", default="-", help="输入 JSON 路径，- 表示 stdin")
    parser.add_argument("--out", "-o", default=None, help="把结果 JSON 另存到这里")
    parser.add_argument("--root", default=None, help="仓库根（默认取 scripts/ 的父目录）")
    parser.add_argument("--human", action="store_true", help="输出人类可读表而不是 JSON")
    parser.add_argument("--no-run-commands", action="store_true",
                        help="不实跑测试命令（test_count 会判 BLOCKED_ENV，不是 PASS）")
    parser.add_argument("--stamp", action="store_true", help="输出里带 checked_at（默认不带，保证幂等）")
    args = parser.parse_args(argv)

    root_path = Path(args.root).expanduser() if args.root else default_root()
    root_error = root_validation_error(root_path)
    if root_error:
        report = {
            "schema_version": SCHEMA_VERSION,
            "window": None,
            "root": str(root_path.resolve()),
            "claim_sha256": _sha256(""),
            "summary": {"PASS": 0, "FAIL": 0, "UNVERIFIABLE": 0, "BLOCKED_ENV": 1, "total": 1},
            "exit_code": EXIT_BLOCKED_ENV,
            "assertions": [
                {
                    "id": "A00",
                    "claim": "",
                    "type": "root",
                    "source": "cli",
                    "check": f"test -f {root_path}/scripts/verify_claim.py && test -d {root_path}/src/life_logger",
                    "verdict": BLOCKED_ENV,
                    "evidence": root_error,
                }
            ],
        }
        payload = _render_human(report) if args.human else json.dumps(
            report, ensure_ascii=False, indent=2
        )
        print(payload)
        if args.out:
            try:
                Path(args.out).write_text(payload + "\n", encoding="utf-8")
            except (OSError, TypeError, ValueError) as exc:
                print(f"[verify_claim] 结果无法落盘到 {args.out}：{exc}", file=sys.stderr)
                return EXIT_BLOCKED_ENV
        return EXIT_BLOCKED_ENV

    try:
        if args.input == "-":
            raw = sys.stdin.read()
        else:
            raw = Path(args.input).read_text(encoding="utf-8")
        doc = json.loads(raw) if raw.strip() else {}
    except (OSError, json.JSONDecodeError) as exc:
        # 读不到输入 = 环境/输入问题，不是「通过」
        report = {
            "schema_version": SCHEMA_VERSION,
            "window": None,
            "root": str(args.root or default_root()),
            "claim_sha256": _sha256(""),
            "summary": {"PASS": 0, "FAIL": 0, "UNVERIFIABLE": 0, "BLOCKED_ENV": 1, "total": 1},
            "exit_code": EXIT_BLOCKED_ENV,
            "assertions": [
                {
                    "id": "A00",
                    "claim": "",
                    "type": "input",
                    "source": "cli",
                    "check": f"读取输入 {args.input!r} 并 json.loads",
                    "verdict": BLOCKED_ENV,
                    "evidence": f"输入无法读取/解析：{_clip(exc, 200)}",
                }
            ],
        }
        code = EXIT_BLOCKED_ENV
    else:
        report = verify(doc, root=args.root, run_commands=not args.no_run_commands, stamp=args.stamp)
        code = report["exit_code"]

    payload = _render_human(report) if args.human else json.dumps(
        report, ensure_ascii=False, indent=2
    )
    print(payload)
    if args.out:
        # `verify_all.sh --json` 的教训：落盘用 `|| true` 会把「序列化/写盘失败」
        # 变成静默 PASS。这里既不吞异常也不崩栈——大声报 BLOCKED_ENV 并回 3。
        try:
            Path(args.out).write_text(
                json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
        except (OSError, TypeError, ValueError) as exc:
            print(f"[verify_claim] 结果无法落盘到 {args.out}：{exc}", file=sys.stderr)
            return EXIT_BLOCKED_ENV
    return code


if __name__ == "__main__":
    sys.exit(main())
