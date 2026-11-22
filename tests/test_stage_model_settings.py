"""测试按阶段配置模型与降级链管理功能 (Stage Model Settings)."""
from __future__ import annotations
import json
from pathlib import Path
import pytest

from life_logger.model_settings import (
    resolve_stage_tiers,
    update_stage_config,
    VALID_STAGES,
)


@pytest.fixture
def mock_config(tmp_path: Path):
    cfg_file = tmp_path / "local-config.json"
    content = {
        "deepseek": {
            "enabled": True,
            "base_url": "http://127.0.0.1:3425/v1",
            "api_key": "global_key",
            "model": "global-model-v1",
            "tier2_model": "global-tier2-v1",
            "fallback": {
                "enabled": True,
                "base_url": "https://api.deepseek.com/v1",
                "api_key": "fallback_key",
                "model": "deepseek-chat"
            }
        }
    }
    cfg_file.write_text(json.dumps(content), encoding="utf-8")
    return cfg_file


def test_legacy_config_backward_compatibility(mock_config: Path):
    """【兼容性】：当没有 stages 段时，所有阶段 100% 回退到全局 deepseek 降级链。"""
    for stage in VALID_STAGES:
        tiers = resolve_stage_tiers(stage, config_path=mock_config)
        assert len(tiers) == 3
        assert tiers[0]["name"] == "primary"
        assert tiers[0]["model"] == "global-model-v1"
        assert tiers[0]["base_url"] == "http://127.0.0.1:3425/v1"
        assert tiers[1]["name"] == "tier2"
        assert tiers[1]["model"] == "global-tier2-v1"
        assert tiers[2]["name"] == "fallback"
        assert tiers[2]["model"] == "deepseek-chat"


def test_stage_override_and_partial_inheritance(mock_config: Path):
    """【阶段独立性】：覆盖特定阶段的 primary 模型，tier2 和 fallback 自动继承全局。"""
    # 覆盖 extract 阶段的 primary 为 custom-extractor
    update_stage_config(
        "extract",
        {"primary": {"model": "custom-extractor-v4", "base_url": "http://127.0.0.1:9999/v1"}},
        config_path=mock_config,
    )
    
    extract_tiers = resolve_stage_tiers("extract", config_path=mock_config)
    assert len(extract_tiers) == 3
    # primary 变更为阶段独立模型
    assert extract_tiers[0]["model"] == "custom-extractor-v4"
    assert extract_tiers[0]["base_url"] == "http://127.0.0.1:9999/v1"
    assert extract_tiers[0]["api_key"] == "global_key"  # 自动继承全局 key
    # tier2 与 fallback 保持继承
    assert extract_tiers[1]["model"] == "global-tier2-v1"
    assert extract_tiers[2]["model"] == "deepseek-chat"

    # daily 阶段完全不受 extract 阶段影响，继续保持全局配置
    daily_tiers = resolve_stage_tiers("daily", config_path=mock_config)
    assert daily_tiers[0]["model"] == "global-model-v1"


def test_disable_tier_in_stage(mock_config: Path):
    """【禁用特定 tier】：在某阶段可显式禁用 fallback 档。"""
    update_stage_config(
        "daily",
        {"fallback": {"enabled": False}},
        config_path=mock_config,
    )
    daily_tiers = resolve_stage_tiers("daily", config_path=mock_config)
    tier_names = [t["name"] for t in daily_tiers]
    assert "primary" in tier_names
    assert "tier2" in tier_names
    assert "fallback" not in tier_names


def test_invalid_stage_rejected(mock_config: Path):
    """非法阶段名称显式抛出 ValueError。"""
    with pytest.raises(ValueError, match="Invalid stage"):
        resolve_stage_tiers("unknown_stage", config_path=mock_config)
