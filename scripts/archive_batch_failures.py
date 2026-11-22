#!/usr/bin/env python3
"""把历史 `deepseek_batch_failed` 噪声归档（标记 ignored），不删除任何数据。

为什么需要：
全库 6711 条 pending 里 6529 条是早期 `max_tokens` 截断导致的「整批 API 失败」倾倒
（实测 2026-10-17 与 10-19 两次统计同为 6529，说明早已不再增长）。
它们会把「需要你判断」的队列淹掉（真实待处理只有 143 条）。

为什么不直接删产物：
1. 输出目录里的 context-extraction.json 是历史证据，删了不可恢复；
2. 人工处理台账（pending-review-states.json）本来就是干这个的 —— 只写台账，
   产物一行不动，随时可 `--restore` 回滚。

用法：
    python3 scripts/archive_batch_failures.py            # 预演（不写盘）
    python3 scripts/archive_batch_failures.py --apply    # 真正归档
    python3 scripts/archive_batch_failures.py --restore  # 撤销归档
"""
from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from life_logger.pending_review import (  # noqa: E402
    STATES_FILENAME,
    STATES_SCHEMA,
    _collect_pending_items,
    _write_review_states,
    load_review_states,
)

BEIJING = ZoneInfo("Asia/Shanghai")
ARCHIVE_DIR = ROOT / "output" / "local-web" / "pending-archive"
NOTE = "archived: 历史整批 API 失败（max_tokens 截断时代），2026-10-19 归档"


def _targets(root: Path) -> dict[str, dict]:
    states = load_review_states(root)
    items, _, _ = _collect_pending_items(root, states["records"])
    return {
        sid: item
        for sid, item in items.items()
        if item.get("group") == "batch_failure" and item.get("state") == "open"
    }


def main(argv: list[str]) -> int:
    apply = "--apply" in argv
    restore = "--restore" in argv
    root = ROOT / "output" / "local-web"
    targets = _targets(root)
    ids = sorted(targets)
    digest = hashlib.sha256("\n".join(ids).encode()).hexdigest()[:16] if ids else ""
    stamp = datetime.now(BEIJING).isoformat(timespec="seconds")

    print(f"待归档（open + batch_failure）: {len(ids)} 条")
    print(f"source_id 集合指纹            : {digest or '（空）'}")
    for sid in ids[:3]:
        print(f"  · {sid}  {str(targets[sid].get('quote'))[:40]}")
    if ids:
        print(f"  … 其余 {max(0, len(ids) - 3)} 条同组")

    if not apply and not restore:
        print("\n预演模式，未写盘。确认无误后加 --apply")
        return 0

    states = load_review_states(root)
    records = dict(states["records"])

    if restore:
        removed = 0
        for sid in ids:
            if records.get(sid, {}).get("note") == NOTE:
                records.pop(sid, None)
                removed += 1
        if removed:
            _write_review_states(
                root,
                {"schema": STATES_SCHEMA, "updated_at": stamp, "records": records},
            )
        print(f"\n已撤销归档: {removed} 条（产物未动）")
        return 0

    if not ids:
        print("\n没有需要归档的条目。")
        return 0

    for sid in ids:
        records[sid] = {"status": "ignored", "note": NOTE, "updated_at": stamp}
    _write_review_states(root, {"schema": STATES_SCHEMA, "updated_at": stamp, "records": records})

    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    manifest = ARCHIVE_DIR / f"batch-failures-{stamp[:10]}.json"
    manifest.write_text(
        json.dumps(
            {
                "created_at": stamp,
                "count": len(ids),
                "source_id_digest": digest,
                "note": NOTE,
                "states_file": str(root / STATES_FILENAME),
                "restore_command": "python3 scripts/archive_batch_failures.py --restore",
                "source_ids": ids,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\n✅ 已归档 {len(ids)} 条 → {manifest.relative_to(ROOT)}")
    print("   产物未改动；撤销：python3 scripts/archive_batch_failures.py --restore")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
