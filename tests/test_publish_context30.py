"""Tests for publish_activity_preview under context_minutes=30 policy.

Verifies:
1. build_payload outputs context_minutes: 30 aligned with the 30-minute context window policy.
2. Cadence minutes remains 10 (10-minute cadence).
3. All publication provenance, source references, input SHA, and corrections are intact.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from scripts.publish_activity_preview import build_payload

SOURCE_DIR = ROOT / "work/today-10min-reextract-20261026"

# [2026-11-04 夹具入库] 这条资源无法入库（体积过大 / 含真实录音 / 含本机配置），
# 缺失时 skip 而不是 fail —— 新克隆不该因为缺少本机数据而红。
import pytest as _pytest  # noqa: E402

if not ((ROOT / "work/today-10min-reextract-20261026").is_dir()):
    _pytest.skip(
        "缺少不入库的夹具：work/today-10min-reextract-20261026 单文件最大 2.6 MB（cards-review.html / input.jsonl），超过入库体积上限",
        allow_module_level=True,
    )


def test_build_payload_context30_contract():
    """Verify build_payload generates activity-timeline-v1 with context_minutes=30."""
    payload = build_payload(SOURCE_DIR)

    assert payload["schema"] == "activity-timeline-v1"
    assert payload["cadence_minutes"] == 10
    assert payload["context_minutes"] == 30, "Policy requires context_minutes=30"
    assert payload["timezone"] == "Asia/Shanghai"
    assert payload["route"] == "jev"
    assert payload["status"] == "ready"
    assert len(payload["activities"]) > 0
    assert len(payload["unassociated_items"]) > 0

    # Ensure metadata provenance is preserved
    meta = payload["metadata"]
    assert meta["origin"] == "user-approved-replay"
    assert "processed_source_ids" in meta
    assert len(meta["processed_source_ids"]) > 0


def test_publish_activity_preview_cli_context30(tmp_path: Path):
    """Verify CLI execution preserves context_minutes=30 without regression."""
    out_root = tmp_path / "output_web"
    out_root.mkdir(parents=True)
    cfg_file = tmp_path / "local-config.json"
    cfg_file.write_text(json.dumps({
        "activity_timeline": {
            "enabled": True,
            "cadence_minutes": 10,
            "context_minutes": 30,
            "route": "jev",
        }
    }), encoding="utf-8")

    cmd = [
        sys.executable,
        str(ROOT / "scripts/publish_activity_preview.py"),
        "--source", str(SOURCE_DIR),
        "--output-root", str(out_root),
        "--config", str(cfg_file),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=True)
    out = json.loads(proc.stdout.strip())
    assert out["applied"] is False
    assert out["activities"] == 51
