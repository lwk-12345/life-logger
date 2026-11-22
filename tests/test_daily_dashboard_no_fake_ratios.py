"""回归：每日看板不得编造比例数字。

无真实数据时 social_ratio / emotion_distribution 必须为空（前端显示「--」空态），
绝不允许出现 50/50 或 20/55/25 这类写死的默认值。
"""

from __future__ import annotations

import json
from pathlib import Path

from life_logger.daily_dashboard import DailyDashboardBuilder

ROOT = Path(__file__).resolve().parents[1]


def test_empty_day_has_no_fabricated_ratios(tmp_path: Path) -> None:
    job_root = tmp_path / "job_root"
    job_root.mkdir()
    dash = DailyDashboardBuilder(job_root).build_for_date("2026-10-11")
    # 当天没有任何真实语音/情绪记录 → 两个分布都必须为空，而不是 50/50、20/55/25
    assert dash.social_ratio == {}
    assert dash.emotion_distribution == {}


def test_real_records_still_compute_ratios(tmp_path: Path) -> None:
    job_root = tmp_path / "job_root"
    attr_dir = job_root / "jobs" / "job-1" / "files" / "rec-1" / "attribution"
    date_dir = job_root / "jobs" / "job-1" / "files" / "rec-1" / "result" / "2026-10-11"
    attr_dir.mkdir(parents=True)
    date_dir.mkdir(parents=True)
    records = [
        {
            "recording_id": "r1",
            "segment_id": "s1",
            "absolute_started_at": "2026-10-11T09:00:00",
            "start_offset_seconds": 0.0,
            "end_offset_seconds": 60.0,
            "source_label": "self",
            "speaker": "self",
            "text": "早上好",
            "emotion": "HAPPY",
        },
        {
            "recording_id": "r1",
            "segment_id": "s2",
            "absolute_started_at": "2026-10-11T09:01:00",
            "start_offset_seconds": 60.0,
            "end_offset_seconds": 180.0,
            "source_label": "non_self",
            "speaker": "others",
            "text": "嗯",
            "emotion": "NEUTRAL",
        },
    ]
    (attr_dir / "source-attribution.json").write_text(
        json.dumps({"records": records}, ensure_ascii=False), encoding="utf-8"
    )
    dash = DailyDashboardBuilder(job_root).build_for_date("2026-10-11")
    assert set(dash.social_ratio) == {"self_speech_percent", "others_speech_percent"}
    assert abs(
        dash.social_ratio["self_speech_percent"]
        + dash.social_ratio["others_speech_percent"]
        - 100.0
    ) < 0.2
    assert dash.social_ratio["self_speech_percent"] == 33.3
    assert dash.emotion_distribution["happy"] == 33.3


def test_web_dashboard_removed_hardcoded_defaults() -> None:
    index = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    app = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    # 静态起始值不得回到写死的 50，JS 兜底不得再出现 "|| 50"
    assert 'id="dashboard-self-pct">50<' not in index
    assert 'id="dashboard-others-pct">50<' not in index
    assert "sr.self_speech_percent || 50" not in app
    assert "sr.others_speech_percent || 50" not in app
