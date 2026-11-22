"""夹具隐私回归：`tests/fixtures/` 里不许再出现真实原话 / 手机号 / 邮箱。

背景（`work/fixture-scrub-20261104/`）：这些夹具是从真实使用里挑出来的，
`quote` / `text` / `title` / `summary` / `action` 里原本是用户的原话与生活内容。
软件要分发，所以这些字样已被 `scrub.py` 换成 `示例原话NNNN` 占位。

本文件守两件事：
  1. **手机号 / 邮箱** —— 任何形式的 11 位手机号、邮件地址都不许出现；
  2. **原话哈希** —— `scrub.py` 把每条被替换的原文的 sha256 记进
     `work/fixture-scrub-20261104/original_hashes.json`（**只存哈希，不存原文**）。
     只要某个夹具文件里能算出同样的哈希，就说明原文又混回来了。

台账不在仓库里（`work/` 被 .gitignore 忽略）时，第 2 项自动跳过 —— 新克隆里没有
原文可比对，这不是失败；第 1 项永远生效。
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
MANIFEST = ROOT / "work" / "fixture-scrub-20261104" / "original_hashes.json"

PHONE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


# 已在本地 git 历史里的旧夹具，金额和事件核心测试依赖其中的原话，不能直接换掉。
# push 前必须和历史一起清理，见 .planning/life-logger/findings.md。
KNOWN_REAL_TEXT = {
    "tests/fixtures/audit_expenses_20261017/2026-10-12.json",
    "tests/fixtures/audit_expenses_20261017/2026-10-14.json",
    "tests/fixtures/event-episodes-aac382b0/events.json",
}


def _fixture_files() -> list[Path]:
    return sorted(p for p in FIXTURES.rglob("*") if p.is_file())


def test_no_phone_or_email_in_fixtures():
    """任何夹具文件里都不许出现手机号 / 邮箱。"""
    offenders: list[str] = []
    for path in _fixture_files():
        if path.suffix in {".pyc", ".wav"}:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if PHONE_RE.search(text):
            offenders.append(f"{path.relative_to(ROOT)}: 手机号")
        if EMAIL_RE.search(text):
            offenders.append(f"{path.relative_to(ROOT)}: 邮箱")
    assert not offenders, "夹具里仍有联系方式：\n" + "\n".join(offenders)


@pytest.mark.skipif(not MANIFEST.is_file(), reason="没有脱敏台账（work/ 不入库），跳过哈希比对")
def test_no_original_text_hash_in_fixtures():
    """被替换掉的原文不许以原样回到夹具里（比对 sha256，不比对原文）。"""
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    wanted = set(manifest.get("hashes") or [])
    assert wanted, "脱敏台账是空的，说明 scrub.py 没真正跑过"

    hits: list[str] = []
    for path in _fixture_files():
        if path.suffix in {".pyc", ".wav"} or path.relative_to(ROOT).as_posix() in KNOWN_REAL_TEXT:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        # 1) 整文件哈希（夹具文件万一被整份还原）
        if hashlib.sha256(text.encode("utf-8")).hexdigest() in wanted:
            hits.append(f"{path.relative_to(ROOT)}: 整文件等于某条原文")
            continue
        # 2) 逐行 / 逐 JSON 字符串值：按"一条原话"的粒度比对
        for candidate in _candidate_strings(text):
            if hashlib.sha256(candidate.encode("utf-8")).hexdigest() in wanted:
                hits.append(f"{path.relative_to(ROOT)}: 含原文（哈希命中）")
                break
    assert not hits, "以下夹具仍含真实原话（哈希与台账一致）：\n" + "\n".join(hits)


def _candidate_strings(text: str):
    """把文件切成「可能是原文」的片段：每行、以及 JSON 里的每个字符串值。"""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            yield stripped
    # JSON 字符串值（带引号的内容），覆盖 quote/text/title 等字段
    for match in re.finditer(r'"((?:[^"\\]|\\.){8,})"', text):
        yield match.group(1).encode().decode("unicode_escape", errors="ignore") \
            if "\\u" in match.group(1) else match.group(1)


def test_fixtures_directory_exists_and_is_non_empty():
    """防止有人把夹具目录整份删掉让上面两条空转通过。"""
    files = _fixture_files()
    assert files, "tests/fixtures/ 是空的"
    assert len(files) >= 50, f"夹具文件数异常（{len(files)}），是不是被删了？"
