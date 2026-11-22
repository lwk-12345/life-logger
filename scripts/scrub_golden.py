#!/usr/bin/env python3
"""脱敏金丝雀（golden）快照里的令牌/密钥字段。

背景：`/api/settings` 会把 `mobile_access.token` 原样回显，而金丝雀采集（`scripts/api_golden.py`）
把整个响应体落盘到 `golden/<baseline>/bodies/*.json` ⇒ 明文令牌跟着进了 git。
本脚本把这些字段的**值**替换成 `<redacted>`，**不改文件其它任何字节**（保持原压缩格式与键序）。

用法：
    python3 scripts/scrub_golden.py                # 就地脱敏 golden/**/bodies/*settings*.json
    python3 scripts/scrub_golden.py --check        # 只检查，需要脱敏则 exit 1（给 CI/单测用）
    python3 scripts/scrub_golden.py --pattern '*'  # 换成别的文件名匹配（默认 *settings*.json）

设计要点：
  * **幂等**：已经是 `<redacted>` 的字段一律跳过；没有改动就不写文件（不动 mtime）。
  * **零误伤**：只动"键名看起来就是凭证"的字段（token/secret/password/api_key/...），
    且只替换该值对应的 JSON 字符串字面量，不做全文件重排。
  * **不打印任何值**：输出只含路径与字段路径。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Iterator

REPO_ROOT = Path(__file__).resolve().parents[1]
REDACTED = "<redacted>"

#: 键名（最后一个路径段）匹配这些就算凭证字段。大小写不敏感。
SENSITIVE_KEY_RE = re.compile(
    r"(?:^|[._\-])(?:"
    r"token|access_?token|refresh_?token|secret|app_?secret|client_?secret|"
    r"password|passwd|api_?key|apikey|access_?key|private_?key|credential|credentials|"
    r"authorization|auth_?token|bearer"
    r")$",
    re.IGNORECASE,
)
#: 兜底：键名不像凭证、但值长得像"随机长串"时，只对明确含 token/key/secret 的键生效（见上）。


def iter_strings(node: Any, path: str = "") -> Iterator[tuple[str, str]]:
    """遍历 JSON，产出 (字段路径, 字符串值)。"""
    if isinstance(node, dict):
        for key, value in node.items():
            child = f"{path}.{key}" if path else str(key)
            yield from iter_strings(value, child)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from iter_strings(value, f"{path}[{index}]")
    elif isinstance(node, str):
        yield path, node


def sensitive_values(payload: Any) -> list[tuple[str, str]]:
    """返回需要脱敏的 (字段路径, 当前值)。空串不算（没东西可藏，也不该改语义）。"""
    out: list[tuple[str, str]] = []
    for path, value in iter_strings(payload):
        if not value or value == REDACTED:
            continue
        last = re.split(r"[.\[\]]+", path)[-1] if path else ""
        if SENSITIVE_KEY_RE.search(last):
            out.append((path, value))
    return out


def scrub_text(raw: str) -> tuple[str, list[str]]:
    """对单个 golden 文件的**文本**做脱敏，返回 (新文本, 被脱敏的字段路径)。

    用文本替换而不是 `json.dump(payload)` 重写：golden 是单行压缩 + `ensure_ascii`，
    重排会产生整文件 diff，也会丢掉键序细节。
    """
    payload = json.loads(raw)
    targets = sensitive_values(payload)
    if not targets:
        return raw, []
    new_raw = raw
    changed: list[str] = []
    for path, value in targets:
        literal = json.dumps(value, ensure_ascii=True)      # 与落盘时的编码一致
        replacement = json.dumps(REDACTED, ensure_ascii=True)
        if literal not in new_raw:
            # 值被转义成别的写法（极少见）→ 不猜，报错让人看
            raise ValueError(f"{path}: 找不到该值的 JSON 字面量（拒绝盲改）")
        new_raw = new_raw.replace(literal, replacement)
        changed.append(path)
    # 改完必须是合法 JSON，且除这些字段外结构不变
    check = json.loads(new_raw)
    if sensitive_values(check):
        raise ValueError("脱敏后仍存在敏感字段（实现有 bug）")
    return new_raw, changed


def scrub_file(path: Path, *, check: bool) -> bool:
    """返回 True = 该文件**需要**脱敏（check 模式）或**已**脱敏（写模式）。"""
    raw = path.read_text(encoding="utf-8")
    new_raw, changed = scrub_text(raw)
    if not changed:
        return False
    if check:
        return True
    path.write_text(new_raw, encoding="utf-8")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="脱敏 golden 快照里的令牌/密钥字段")
    parser.add_argument("--check", action="store_true", help="只检查（需要脱敏则 exit 1）")
    parser.add_argument("--pattern", default="*settings*.json",
                        help="bodies 下的文件名匹配（默认 *settings*.json）")
    parser.add_argument("--root", default=str(REPO_ROOT / "golden"), help="golden 根目录")
    args = parser.parse_args(argv)

    root = Path(args.root)
    files = sorted(root.rglob(f"bodies/{args.pattern}")) if root.is_dir() else []
    if not files:
        print(f"没有匹配的文件：{root}/**/bodies/{args.pattern}")
        return 0

    dirty: list[Path] = []
    for path in files:
        changed = scrub_file(path, check=args.check)
        rel = path.relative_to(root.parent) if root.parent in path.parents else path
        if changed:
            dirty.append(path)
            print(f"{'需要脱敏' if args.check else '已脱敏'}: {rel}")
        else:
            print(f"干净: {rel}")

    if args.check and dirty:
        print(f"\n{len(dirty)} 个文件仍含明文字段（去掉 --check 即可就地脱敏）", file=sys.stderr)
        return 1
    print(f"\n共 {len(files)} 个文件，处理 {len(dirty)} 个")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
