#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`scripts/check_refuted_approaches.py` 的单测。

为什么要有：这个脚本是「别再把已证伪方向加回来」的闸门。
闸门自己坏了（漏报/误报）比没有闸门更糟，所以每条规则都要有一个**故意的坏样本**
和一个**故意的相似但合法样本**（防误报）。

对应报告：`work/do-not-retry-20261103/REPORT.md`
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check_refuted_approaches.py"
sys.path.insert(0, str(ROOT / "scripts"))

import check_refuted_approaches as chk  # noqa: E402


# --------------------------------------------------------------------------
# 夹具
# --------------------------------------------------------------------------
def make_tree(tmp_path: Path, files: dict[str, str]) -> Path:
    """在 tmp_path 下造一个假仓库。"""
    for rel, text in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return tmp_path


def run(root: Path, *extra: str) -> tuple[int, dict]:
    """跑一次扫描，返回 (exit_code, json结果)。用 import 直调，避免子进程开销。"""
    import io
    from contextlib import redirect_stdout

    argv = ["--root", str(root), "--json", "--baseline", "", *extra]
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = chk.main(argv)
    out = buf.getvalue().strip()
    return code, (json.loads(out) if out.startswith("{") else {})


def rules_hit(res: dict) -> set[str]:
    return {f["rule"] for f in res.get("findings", []) if f["severity"] in ("fail", "warn")}


# --------------------------------------------------------------------------
# 1) 基线：干净的树必须通过
# --------------------------------------------------------------------------
def test_clean_tree_passes(tmp_path):
    root = make_tree(tmp_path, {
        "src/life_logger/source_attribution.py": (
            "CAMPPLUS_VOICEPRINT_POLICY = VoiceprintPolicy(\n"
            "    attribution=AttributionConfig(self_threshold=0.40, unknown_threshold=0.30),\n"
            ")\n"
        ),
    })
    code, res = run(root)
    assert code == 0, res
    assert res["counts"]["fail"] == 0
    assert res["counts"]["warn"] == 0


# --------------------------------------------------------------------------
# 2) 核心用例：把阈值改成 0.22 必须报错
# --------------------------------------------------------------------------
def test_threshold_022_is_rejected(tmp_path):
    root = make_tree(tmp_path, {
        "src/life_logger/source_attribution.py": (
            "CAMPPLUS_VOICEPRINT_POLICY = VoiceprintPolicy(\n"
            "    attribution=AttributionConfig(self_threshold=0.22, unknown_threshold=0.10),\n"
            ")\n"
        ),
    })
    code, res = run(root)
    assert code == 1, res
    assert "R1" in rules_hit(res)
    f = [x for x in res["findings"] if x["rule"] == "R1"][0]
    assert f["file"] == "src/life_logger/source_attribution.py"
    assert f["line"] == 2
    # 警告必须带失败数字，否则闸门只会说"不行"却没说为什么
    assert "68%" in f["evidence"]
    assert "far-threshold-20261103" in f["report"]


def test_threshold_022_via_json_config_is_rejected(tmp_path):
    root = make_tree(tmp_path, {
        "local-config.json": json.dumps({"voiceprint": {"self_threshold": 0.22}}, ensure_ascii=False),
    })
    code, res = run(root)
    assert code == 1, res
    assert "R1" in rules_hit(res)


def test_far_only_threshold_below_030_is_rejected(tmp_path):
    root = make_tree(tmp_path, {
        "src/life_logger/source_attribution.py": "far_self_threshold = 0.22\n",
    })
    code, res = run(root)
    assert code == 1, res
    assert "R1" in rules_hit(res)


# --------------------------------------------------------------------------
# 3) 防误报：钳位与注释里的 0.22 不算
# --------------------------------------------------------------------------
def test_min_clamp_is_not_flagged(tmp_path):
    """`min(x, 0.22)` 是钳位不是阈值 —— 生产里真有这行，不能误报。"""
    root = make_tree(tmp_path, {
        "scripts/attribute_funasr_transcript.py": (
            'item["voice_similarity"] = min(float(item.get("voice_similarity") or 0.22), 0.22)\n'
        ),
    })
    code, res = run(root)
    assert code == 0, res
    assert res["findings"] == []


def test_comment_mentioning_022_is_not_flagged(tmp_path):
    root = make_tree(tmp_path, {
        "src/life_logger/source_attribution.py": (
            "# 曾经试过 self_threshold=0.22，误收 68%，已证伪\n"
            "self_threshold = 0.40\n"
        ),
    })
    code, res = run(root)
    assert code == 0, res


def test_css_decimals_are_not_flagged(tmp_path):
    root = make_tree(tmp_path, {
        "web/page.html": "box-shadow: 0 2px 8px rgba(30, 33, 31, 0.22);\n",
    })
    code, res = run(root)
    assert code == 0, res


def test_backup_and_prototype_files_are_skipped(tmp_path):
    """备份/原型页不是生产逻辑，扫它们只会造噪音。"""
    root = make_tree(tmp_path, {
        "web/daily-timeline-live.html.orig": "box-shadow: rgba(0,0,0,0.22);\nself_threshold=0.22\n",
        "web/daily-timeline-prototype.html": "var x = 1;\nself_threshold=0.22\n",
        "scripts/x.py.bak-20260101": "self_threshold=0.22\n",
    })
    code, res = run(root)
    assert code == 0, res


# --------------------------------------------------------------------------
# 4) 其余三条已证伪方向
# --------------------------------------------------------------------------
@pytest.mark.parametrize("rel,text,rule", [
    ("src/life_logger/voiceprint.py",
     "def cluster_split_by_cosine(items):\n    return items\n", "R2"),
    ("src/life_logger/voiceprint.py",
     "second_split_enabled = True\n", "R2"),
    ("src/life_logger/voiceprint.py",
     'MODEL = "iic/speech_eres2netv2_sv_zh-cn_16k-common"\n', "R3"),
    ("src/life_logger/voiceprint.py",
     "ERes2NetV2 = load_model()\n", "R3"),
    ("src/life_logger/attribution.py",
     "def session_vote(rows):\n    return rows\n", "R4"),
])
def test_other_rules(tmp_path, rel, text, rule):
    root = make_tree(tmp_path, {rel: text})
    code, res = run(root)
    assert code == 1, res
    assert rule in rules_hit(res)


@pytest.mark.parametrize("rel,text,rule", [
    ("web/daily-timeline-live.html", 'var MEDIA_IDS = new Set(["act_001"]);\n', "R5"),
    ("src/life_logger/denoise.py", "clean = spectral_subtraction(audio, sr)\n", "R6"),
])
def test_warn_rules_do_not_fail_by_default(tmp_path, rel, text, rule):
    root = make_tree(tmp_path, {rel: text})
    code, res = run(root)
    assert code == 0, res                       # WARN 默认不判失败
    assert rule in rules_hit(res)
    code_strict, _ = run(root, "--strict")
    assert code_strict == 1, " --strict 下 WARN 必须判失败"


# --------------------------------------------------------------------------
# 5) 豁免与基线
# --------------------------------------------------------------------------
def test_exempt_marker_skips(tmp_path):
    root = make_tree(tmp_path, {
        "src/life_logger/source_attribution.py":
            "self_threshold = 0.22  # refuted-ok: 实验分支，见 work/xxx\n",
    })
    code, res = run(root)
    assert code == 0, res
    assert res["counts"]["exempt"] == 1


def test_baseline_downgrades_known_hit(tmp_path):
    root = make_tree(tmp_path, {
        "src/life_logger/daily_dashboard.py":
            "if rows_st and self_ratio < 0.2 and len(txt) > 40:\n    scene = 'media'\n",
    })
    code, res = run(root)
    assert code == 1, res                       # 没有 baseline 时是 FAIL
    base = tmp_path / "baseline.json"
    base.write_text(json.dumps({"known": [{
        "rule": "R4", "file": "src/life_logger/daily_dashboard.py",
        "pattern": "self 占比判媒体", "why": "存量债务",
    }]}, ensure_ascii=False), encoding="utf-8")
    import io
    from contextlib import redirect_stdout
    buf = io.StringIO()
    with redirect_stdout(buf):
        code2 = chk.main(["--root", str(root), "--json", "--baseline", str(base)])
    res2 = json.loads(buf.getvalue())
    assert code2 == 0, res2
    assert res2["counts"]["known"] == 1


# --------------------------------------------------------------------------
# 6) 真 CLI：退出码与结论行一致
# --------------------------------------------------------------------------
def test_cli_exit_code_and_message(tmp_path):
    root = make_tree(tmp_path, {
        "src/life_logger/source_attribution.py":
            "attribution=AttributionConfig(self_threshold=0.22)\n",
    })
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(root), "--baseline", ""],
        capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "结论: FAIL" in proc.stdout
    assert "R1" in proc.stdout


def test_repo_itself_passes():
    """真仓库当前必须是 PASS —— 否则这个闸门一装上就是红的，没人会守它。"""
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--quiet"],
        capture_output=True, text=True, timeout=180,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "fail=0" in proc.stdout
