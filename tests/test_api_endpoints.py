"""Integration tests for new Life Logger HTTP API endpoints."""

from __future__ import annotations

from pathlib import Path


def test_api_endpoints_integration(tmp_path: Path) -> None:
    # 模拟 Job Root 与配置文件
    job_root = tmp_path / "output" / "local-web"
    job_root.mkdir(parents=True)
    config_file = tmp_path / "local-config.json"
    config_file.write_text("{}", encoding="utf-8")

    # 1. 验证 API Features 中包含新特性
    from life_logger.local_web import API_FEATURES
    assert "health_sync_v1" in API_FEATURES
    assert "memory_rag_v1" in API_FEATURES
    assert "daily_dashboard_v1" in API_FEATURES
    # 【2026-10-14 修正】原断言 `"scene_classifier_v1" in API_FEATURES` 锁定的是一个**虚假宣称**：
    # src/life_logger/scene_classifier.py 是完整的声学场景分类器（ASC/SED，6 类场景），但全仓
    # 生产路径对它引用 0 次（只有 API_FEATURES 那一行字符串 + 本测试）。看板显示的场景实际来自
    # daily_dashboard.build_timeline_scenes() 的另一套关键词/事件实现。
    # `/api/health` 对外自述 ≠ 实际接通属于信任问题，故摘除该项。
    # 这里改为**反向锁死**：将来若有人接通 scene_classifier 并恢复该宣称，必须同时改掉本断言
    # （即被迫显式承认这次行为变更），不会静默回归。
    assert "scene_classifier_v1" not in API_FEATURES, (
        "scene_classifier_v1 已被摘除（该能力未接通）。若已真正接通 scene_classifier，"
        "请恢复 API_FEATURES 中的该项并更新本断言与说明。"
    )
    assert "voice_triggers_v1" in API_FEATURES

    # 2. 模拟 POST /api/health/sync 同步健康体征
    from life_logger.health_store import HealthDataStore
    hstore = HealthDataStore(job_root / "health")
    hstore.record_heart_rates([
        {"timestamp": "2026-10-11T11:00:00", "bpm": 76.5, "source": "test-sync", "context": "resting"}
    ])
    hstore.record_steps("2026-10-11", 5200)

    summary = hstore.get_daily_summary("2026-10-11")
    assert summary.avg_heart_rate == 76.5
    assert summary.total_steps == 5200

    # 3. 模拟随手记写入与 /api/quick-captures 读取
    from life_logger.voice_triggers import QuickCaptureStore, scan_voice_triggers
    cstore = QuickCaptureStore(job_root / "quick_captures.json")
    cstore.append(scan_voice_triggers("提醒我明天上午十点复盘", timestamp="2026-10-11T09:30:00"))
    cstore.append(scan_voice_triggers("买午餐花了35元", timestamp="2026-10-11T12:00:00"))

    captures = cstore.list_all()
    assert len(captures) == 2
    assert captures[0]["trigger_type"] == "todo"
    assert captures[1]["trigger_type"] == "expense"

    # 4. 模拟 Daily Dashboard 聚合
    from life_logger.daily_dashboard import DailyDashboardBuilder
    builder = DailyDashboardBuilder(job_root)
    dash = builder.build_for_date("2026-10-11")
    assert dash.health_summary["total_steps"] == 5200
    assert len(dash.captured_todos) == 1
    assert len(dash.captured_expenses) == 1

    # 5. 模拟生活记忆检索问答
    from life_logger.memory_rag import MemoryRAGService
    rag = MemoryRAGService(job_root)
    ans = rag.ask("明天有什么提醒事项？")
    assert ans.confidence > 0
    assert "复盘" in ans.answer
