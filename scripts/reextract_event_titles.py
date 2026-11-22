#!/usr/bin/env python3
"""Selective historical title re-extraction (dry-run by default).

This tool never calls a model or re-runs ASR. It reads the already-written
transcript, applies the current title-grounding rules, and shows before/after.
Only an explicit ``--apply`` writes, after taking a timestamped backup.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Iterable, Mapping

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from life_logger.contextual_extraction import (  # noqa: E402
    ground_title,
    title_grounding_violations,
)

SKIP_PARTS = {
    "decision-variants", "comparison-history", "jev", "clef", "recovery",
    "preview", "backup", ".tmp", "__pycache__",
}
FACTUAL_LATIN = {"A级", "UI", "AI", "WinDisplay"}
FACTUAL_TERMS = {"接入"}


class UnsafeTargetError(RuntimeError):
    """--apply 没有明确的改写目标（会变成整树重写）。"""


class UnsafeRollbackError(RuntimeError):
    """回滚清单不可信（绝对路径 / .. / sha256 不符 / root 不一致）。"""


class PlanDriftError(RuntimeError):
    """plan 之后文件被人改过，拒绝拿旧计划覆盖新内容。"""


def clean_event_ids(values: Iterable[Any] | None) -> list[str]:
    """把 `--event-id` 归一化：去空白、丢空串。

    为什么必须 strip：`--event-id ''` / `--event-id '   '` 在 Python 里是 truthy，
    旧逻辑会把它当成“显式指定了目标”，于是仍然建 backup/manifest 并发起 apply。
    """
    cleaned: list[str] = []
    for value in values or []:
        text = str(value if value is not None else "").strip()
        if text:
            cleaned.append(text)
    return cleaned


def _norm(text: str) -> str:
    return re.sub(r"[\W_]+", "", str(text or "").casefold())


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _relative_parts(path: Path, root: Path) -> tuple[str, ...] | None:
    """只拿 **root 相对路径** 的 parts。

    为什么不能用 `path.parts`：那是绝对路径，root 只要叫 `preview` / `backup` /
    `recovery` 之类，整棵树会被误判成“全是要跳过的目录”，dry-run 静默返回 0 条。
    """
    try:
        relative = path.relative_to(root)
    except ValueError:
        return None
    return relative.parts


def _event_files(root: Path) -> Iterable[Path]:
    for path in sorted(root.glob("jobs/job-*/files/*/result/*/events.json")):
        parts = _relative_parts(path, root)
        if parts is not None and not any(part in SKIP_PARTS for part in parts):
            yield path
    for path in sorted(root.glob("watch-sessions/*/*/events.json")):
        parts = _relative_parts(path, root)
        if parts is not None and not any(part in SKIP_PARTS for part in parts):
            yield path


def _load_events(path: Path) -> tuple[Any, list[dict[str, Any]]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, list):
        return payload, [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict) and isinstance(payload.get("events"), list):
        return payload, [item for item in payload["events"] if isinstance(item, dict)]
    return payload, []


def _iter_json(path: Path) -> list[Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        rows = payload.get("rows")
        if isinstance(rows, list):
            return rows
        return [payload]
    return []


def _find_transcript(event_file: Path, event: dict[str, Any]) -> dict[str, Any]:
    evidence = event.get("evidence") if isinstance(event.get("evidence"), dict) else {}
    segment_id = str(evidence.get("segment_id") or "").strip()
    quote = str(evidence.get("quote") or "").strip()
    candidates: list[Path] = []
    if "watch-sessions" in event_file.parts:
        session_root = event_file.parent
        if segment_id:
            # watch_000223_000002 -> formal-asr/segments/000223/... (best effort)
            match = re.match(r"watch_(\d+)_\d+", segment_id)
            if match:
                seq = match.group(1)
                candidates.extend(session_root.glob(f"formal-asr/segments/{seq}/**/transcript.json"))
                candidates.extend(session_root.glob(f"formal-asr/segments/{seq}/**/state.json"))
        candidates.extend(session_root.glob("formal-asr/segments/*/*/transcript.json"))
    else:
        result_dir = event_file.parent
        candidates.extend([
            result_dir / "transcript.json",
            result_dir / "asr" / "transcript.json",
            result_dir.parent.parent / "asr" / "transcript.json",
            result_dir.parent.parent / "attribution" / "source-attribution.json",
        ])
    for path in candidates:
        rows = _iter_json(path)
        for row in rows:
            if not isinstance(row, dict):
                continue
            if segment_id and str(row.get("segment_id") or "") != segment_id:
                continue
            text = str(row.get("text") or "")
            if quote and _norm(quote) not in _norm(text):
                # A nearby transcript may contain the quote even if segment id
                # is absent from legacy artifacts; only accept an exact
                # normalized quote, never a fuzzy substitute.
                continue
            if not text:
                continue
            return {
                "transcript_path": str(path),
                "segment_id": segment_id or row.get("segment_id"),
                "text": text,
                "quote_verified": True,
            }
    return {
        "transcript_path": None,
        "segment_id": segment_id or None,
        "text": "",
        "quote_verified": False,
    }


def _is_protected(event: dict[str, Any]) -> bool:
    evidence = event.get("evidence") if isinstance(event.get("evidence"), dict) else {}
    return bool(
        event.get("correction_status")
        or event.get("reviewed_at")
        or event.get("review_status") in {"reviewed", "confirmed"}
        or evidence.get("review_status") in {"reviewed", "confirmed"}
        or evidence.get("correction_note")
    )


def _classify(violations: list[str], event_id: str) -> str:
    tokens = " ".join(violations)
    if any(token in tokens for token in FACTUAL_LATIN):
        return "factual_error"
    if any(f"ungrounded_term:{term}" in tokens for term in FACTUAL_TERMS):
        return "factual_error"
    # The four known factual errors are stable event ids even if a future
    # language rule changes how the violation is named.
    if event_id in {
        "evt_ctx_6a87a251d6225c68", "evt_ctx_65eab9c9da9cb1a6",
        "evt_ctx_e4d84e6d17e4a348", "evt_ctx_2d871a9b8b0b6bba",
    }:
        return "factual_error"
    return "minor_summary"


def plan(root: Path, event_ids: set[str], session_id: str | None) -> list[dict[str, Any]]:
    changes: list[dict[str, Any]] = []
    for path in _event_files(root):
        if session_id and session_id not in path.parts:
            continue
        _payload, events = _load_events(path)
        for event in events:
            event_id = str(event.get("event_id") or "")
            if event_ids and event_id not in event_ids:
                continue
            evidence = event.get("evidence") if isinstance(event.get("evidence"), dict) else {}
            if str(evidence.get("title_source") or "model") != "model":
                continue
            title = str(event.get("title") or "").strip()
            quote = str(evidence.get("quote") or "").strip()
            if not title or not quote:
                continue
            transcript = _find_transcript(path, event)
            context = transcript["text"] if transcript["text"] else quote
            violations = title_grounding_violations(title, quote, context)
            if not violations:
                continue
            item = {
                "event_id": event_id,
                "title_before": title,
                "quote": quote,
                "event_file": str(path),
                # plan→apply 一致性：记下计划时刻的标题与文件指纹，
                # apply 前一比就知道中间有没有人改过。
                "file_sha256": _sha256(path),
                "title_sha256": hashlib.sha256(title.encode("utf-8")).hexdigest(),
                "segment_id": evidence.get("segment_id"),
                "transcript_path": transcript["transcript_path"],
                "quote_verified": transcript["quote_verified"],
                "violations": violations,
                "classification": _classify(violations, event_id),
            }
            if _is_protected(event):
                item.update(action="skip", reason="protected_reviewed_or_corrected")
            elif not transcript["quote_verified"]:
                item.update(action="needs_audio_rerun", reason="quote_not_found_in_existing_transcript")
            else:
                after, source, _ = ground_title(title, quote, context)
                item.update(action="change", title_after=after, title_source_after=source,
                            reason="existing_transcript+current_grounding")
            changes.append(item)
    return changes


def _context_extraction_paths(event_file: Path) -> list[Path]:
    """Nearby context-extraction artifacts that must be backed up with the event."""
    names = ("formal-asr/live-context/context-extraction.json", "live-context/context-extraction.json", "context-extraction.json")
    found: list[Path] = []
    for parent in [event_file.parent, *event_file.parents[1:5]]:
        for name in names:
            candidate = parent / name
            if candidate.is_file() and candidate not in found:
                found.append(candidate)
    return found


def _backup(root: Path, backup_dir: Path, changes: list[dict[str, Any]]) -> dict[str, Any]:
    backup_dir.mkdir(parents=True, exist_ok=True)
    files: list[dict[str, Any]] = []
    for item in changes:
        if item.get("action") != "change":
            continue
        sources = [Path(item["event_file"])]
        if item.get("transcript_path"):
            sources.append(Path(item["transcript_path"]))
        sources.extend(_context_extraction_paths(Path(item["event_file"])))
        for source in sources:
            if not source.is_file():
                continue
            rel = source.relative_to(root) if source.is_relative_to(root) else Path(source.name)
            target = backup_dir / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                shutil.copy2(source, target)
            files.append({"path": str(rel), "sha256": _sha256(source)})
    return {"files": files, "backup_dir": str(backup_dir)}


def apply(
    root: Path,
    backup_dir: Path,
    changes: list[dict[str, Any]],
    *,
    scope: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """把 plan 里的 `change` 落到 events.json。

    `scope` 是防御层：即使有人绕过 CLI 直接调函数，没有明确目标也拒绝执行。
    理由：一个不带 `--event-id` 的 apply 等于把全库所有 “标题不合规” 的事件全部改写，
    且无法撤销到“只改了一半”的中间态。
    """
    scope = dict(scope or {})
    event_ids = clean_event_ids(scope.get("event_ids"))
    if not scope_is_explicit(event_ids, scope.get("session_id"),
                             bool(scope.get("confirm_session"))):
        raise UnsafeTargetError(
            "拒绝执行：--apply 必须显式指定至少一个 --event-id，"
            "或 --session-id 配合 --confirm-session；不允许整树重写"
        )
    scope = {**scope, "event_ids": event_ids}
    pending = [item for item in changes if item.get("action") == "change"]
    if not pending:
        # 没有任何要写的：不建 backup 目录、不写 manifest，避免留下“看起来执行过”的痕迹。
        return {
            "schema_version": "title-reextract-v1",
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "root": str(root),
            "backup": {"files": [], "backup_dir": str(backup_dir)},
            "changes": changes,
            "written": 0,
            "rollback": None,
        }

    # ---------- plan→apply 一致性校验（先全量比，再动一个字节）----------
    drift: list[str] = []
    for item in pending:
        path = Path(item["event_file"])
        expected = str(item.get("file_sha256") or "")
        if not expected:
            drift.append(f"{path}: 计划里没有记录 sha256（旧版 plan？）")
            continue
        if not path.is_file():
            drift.append(f"{path}: 目标文件已不存在")
            continue
        actual = _sha256(path)
        if actual != expected:
            drift.append(f"{path}: 内容已变化（plan={expected[:12]}… now={actual[:12]}…）")
    if drift:
        raise PlanDriftError(
            "拒绝执行：计划生成后目标文件被修改过，整批中止（未写入任何文件）：\n"
            + "\n".join(f"  - {line}" for line in drift)
        )

    backup_dir.mkdir(parents=True, exist_ok=True)
    backup = _backup(root, backup_dir, changes)
    touched: dict[Path, Any] = {}
    for item in changes:
        if item.get("action") != "change":
            continue
        path = Path(item["event_file"])
        if path not in touched:
            touched[path] = json.loads(path.read_text(encoding="utf-8"))
        payload = touched[path]
        events = payload if isinstance(payload, list) else payload.get("events", [])
        for event in events:
            if not isinstance(event, dict) or str(event.get("event_id") or "") != item["event_id"]:
                continue
            evidence = event.get("evidence") if isinstance(event.get("evidence"), dict) else {}
            evidence["title_source"] = item["title_source_after"]
            evidence["title_grounding"] = {
                "status": "rejected" if item["violations"] else "accepted",
                "violations": item["violations"],
                "fallback": "quote_prefix" if item["title_source_after"] == "quote_fallback" else None,
                "basis": item["reason"],
                "segment_id": item.get("segment_id"),
                "transcript_path": item.get("transcript_path"),
            }
            event["title"] = item["title_after"]
            event["evidence"] = evidence
            event["review_status"] = "unreviewed"
            item["applied"] = True
    for path, payload in touched.items():
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    manifest = {
        "schema_version": "title-reextract-v1",
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "root": str(Path(root).resolve()),
        "backup": backup,
        "changes": changes,
        "written": len(pending),
        "scope": scope,
        "rollback": (
            "python scripts/reextract_event_titles.py "
            f"--root {Path(root).resolve()} --rollback {backup_dir}"
        ),
    }
    (backup_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest


def scope_is_explicit(event_ids: Any, session_id: Any, confirm_session: bool = False) -> bool:
    """改写目标是否足够明确。空 event_ids 不等于“全部”，而是“没指定”。"""
    if event_ids:
        return True
    return bool(str(session_id or "").strip()) and bool(confirm_session)


def _safe_relative(raw: Any) -> Path:
    """回滚清单里的路径必须是干净的相对路径。

    不信 manifest 的原因：`--rollback <dir>` 指向的是任意目录，一份构造过的
    manifest（`"path": "../../x"`）就能把内容写到 root 之外。
    """
    text = str(raw or "").strip()
    if not text:
        raise UnsafeRollbackError("回滚清单里有空路径")
    relative = Path(text)
    if relative.is_absolute():
        raise UnsafeRollbackError(f"拒绝绝对路径: {text}")
    if any(part == ".." for part in relative.parts):
        raise UnsafeRollbackError(f"拒绝包含 .. 的路径: {text}")
    return relative


def rollback(backup_dir: Path, root: Path | None = None) -> int:
    """从备份恢复。先全部校验（路径 + root + sha256），全部通过后才开始写。

    `root` 必填且要和 manifest 里记录的一致：manifest 是普通 JSON，被篡改后若直接
    信任里面的 `root`，一份 `{"root": "/Users/xxx"}` 就能把文件写到任意目录。
    """
    if root is None:
        raise UnsafeRollbackError("回滚必须显式传 --root，并与 manifest 记录一致")
    manifest_path = backup_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    recorded = Path(str(manifest.get("root") or ""))
    if not str(manifest.get("root") or "").strip():
        raise UnsafeRollbackError("manifest 里没有记录 root，拒绝回滚")
    if recorded.resolve() != Path(root).resolve():
        raise UnsafeRollbackError(
            f"root 不一致：命令行 {Path(root).resolve()} vs manifest {recorded.resolve()}；拒绝回滚"
        )
    root = Path(root).resolve()
    entries = list((manifest.get("backup") or {}).get("files") or [])

    verified: list[tuple[Path, Path]] = []
    for item in entries:
        relative = _safe_relative(item.get("path"))
        source = backup_dir / relative
        if not source.is_file():
            raise UnsafeRollbackError(f"备份文件缺失: {relative}")
        expected = str(item.get("sha256") or "")
        if expected:
            actual = _sha256(source)
            if actual != expected:
                raise UnsafeRollbackError(
                    f"备份 {relative} 的 sha256 不匹配"
                    f"（期望 {expected[:12]}… 实际 {actual[:12]}…）；已中止，未写入任何文件"
                )
        verified.append((relative, source))

    for relative, source in verified:
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.{os.getpid()}.rollback.tmp")
        shutil.copy2(source, temporary)
        os.replace(temporary, target)
    print(f"rolled back {len(verified)} files from {backup_dir}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=None,
                        help="数据根目录；--rollback 时必须显式给出并与 manifest 一致")
    parser.add_argument("--event-id", action="append", default=[])
    parser.add_argument("--session-id")
    parser.add_argument("--confirm-session", action="store_true",
                        help="配合 --session-id 使用；没有它时 --apply 拒绝执行")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--include-minor", action="store_true",
                        help="允许改写 minor_summary（默认只改 factual_error）")
    parser.add_argument("--only-factual", action="store_true",
                        help="只展示 factual_error 候选（dry-run 用）")
    parser.add_argument("--backup-dir", type=Path)
    parser.add_argument("--rollback", type=Path)
    parser.add_argument("--format", choices=("json", "markdown"), default="json")
    args = parser.parse_args(argv)
    event_ids = clean_event_ids(args.event_id)
    if args.rollback:
        if args.root is None:
            print("回滚被拒绝: 必须显式传 --root，并与 manifest 记录的 root 一致", file=sys.stderr)
            return 3
        try:
            return rollback(args.rollback, args.root)
        except UnsafeRollbackError as error:
            print(f"回滚被拒绝: {error}", file=sys.stderr)
            return 3
    root = args.root if args.root is not None else ROOT / "output" / "local-web"
    changes = plan(root, set(event_ids), args.session_id)
    if args.only_factual:
        changes = [item for item in changes if item["classification"] == "factual_error"]
    result = {
        "dry_run": not args.apply,
        "summary": {
            "candidates": len(changes),
            "factual_error": sum(1 for item in changes if item["classification"] == "factual_error"),
            "minor_summary": sum(1 for item in changes if item["classification"] == "minor_summary"),
            "needs_audio_rerun": sum(1 for item in changes if item["action"] == "needs_audio_rerun"),
            "protected": sum(1 for item in changes if item["action"] == "skip"),
            "would_change": sum(1 for item in changes if item["action"] == "change"),
        },
        "changes": changes,
    }
    if args.apply:
        scope = {"event_ids": event_ids, "session_id": args.session_id,
                 "confirm_session": args.confirm_session}
        if not scope_is_explicit(event_ids, args.session_id, args.confirm_session):
            print(
                "拒绝执行 --apply：必须显式指定至少一个 --event-id，"
                "或 --session-id 配合 --confirm-session。"
                "不带目标的 apply 会重写全库所有标题，本工具不做这件事。",
                file=sys.stderr,
            )
            return 2
        minors = [item for item in changes
                  if item["action"] == "change" and item["classification"] != "factual_error"]
        if minors and not args.include_minor:
            print(
                "拒绝执行 --apply：目标里有 %d 条 minor_summary（%s）。"
                "默认只改写 factual_error；确实要改 minor 请显式加 --include-minor。"
                % (len(minors), ", ".join(item["event_id"] for item in minors[:5])),
                file=sys.stderr,
            )
            return 2
        backup_dir = args.backup_dir or (ROOT / "work" / "reextract-plan-20261023" / f"apply-{time.strftime('%Y%m%d-%H%M%S')}")
        try:
            manifest = apply(root, backup_dir, changes, scope=scope)
        except PlanDriftError as error:
            print(f"{error}", file=sys.stderr)
            return 4
        if manifest.get("written", 0):
            result["manifest"] = str(backup_dir / "manifest.json")
            result["rollback_command"] = manifest["rollback"]
        else:
            result["written"] = 0
    print(json.dumps(result, ensure_ascii=False, indent=2) if args.format == "json" else _markdown(result))
    return 0


def _markdown(result: dict[str, Any]) -> str:
    lines = ["# Re-extraction preview", "", f"dry_run: {result['dry_run']}", "", "| classification | action | event_id | before | after | violations |", "|---|---|---|---|---|---|"]
    for item in result["changes"]:
        lines.append(
            f"| {item['classification']} | {item['action']} | {item['event_id']} | "
            f"{item['title_before']} | {item.get('title_after', '—')} | {', '.join(item['violations'])} |"
        )
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
