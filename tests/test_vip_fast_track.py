from unittest.mock import MagicMock, patch
from life_logger.vip_fast_track import clean_sensevoice_tags, execute_vip_command

def test_clean_sensevoice_tags():
    raw = "<|zh|><|NEUTRAL|><|Speech|>帮我查一下附近药店发我微信"
    cleaned = clean_sensevoice_tags(raw)
    assert cleaned == "帮我查一下附近药店发我微信"

def test_remote_empty_pin_is_rejected_as_unsupported():
    # Empty pin without audio must NOT fake success: must return unsupported error
    res = execute_vip_command(text="   ", pin_id="pin_001")
    assert res["ok"] is False
    assert res["status"] == "unsupported"
    assert res["error"] == "unsupported_remote_pin"

def test_query_dispatch_pushes_to_wechat():
    mock_llm_response = {
        "intent_type": "query_dispatch",
        "title": "查询附近药店",
        "action_payload": {
            "research_query": "附近营业药店",
            "wechat_reply": "离您最近的药店是人民路同仁堂（距离500米），正常营业中。"
        }
    }
    mock_openclaw = MagicMock()
    mock_openclaw.create_task.return_value = {"task_id": "task_123", "status": "dispatched"}

    with patch("life_logger.vip_fast_track.call_magpie_deepseek", return_value=mock_llm_response):
        res = execute_vip_command(
            text="帮我查一下附近药店发我微信",
            pin_id="pin_query_001",
            openclaw_client=mock_openclaw
        )

    assert res["status"] == "completed"
    assert res["intent_type"] == "query_dispatch"
    assert res["action_taken"] == "dispatched_to_openclaw_wechat"
    mock_openclaw.create_task.assert_called_once()
    call_args = mock_openclaw.create_task.call_args[1]
    assert "附近药店" in call_args["title"]
    assert "同仁堂" in call_args["source"]["message"]

def test_todo_registers_to_active_intents(tmp_path):
    mock_llm_response = {
        "intent_type": "todo",
        "title": "明天去银行领卡",
        "action_payload": {
            "task": "明天去银行领卡",
            "priority": "P0"
        }
    }
    from life_logger.intent_pool import ActiveIntentPool
    pool = ActiveIntentPool(tmp_path / "active-intents.json")

    with patch("life_logger.vip_fast_track.call_magpie_deepseek", return_value=mock_llm_response):
        res = execute_vip_command(
            text="明天去银行领卡",
            pin_id="pin_todo_001",
            intent_pool=pool
        )

    assert res["status"] == "completed"
    assert res["intent_type"] == "todo"
    assert res["action_taken"] == "registered_to_active_intents"
    pending = pool.list_pending_intents()
    assert len(pending) == 1
    assert "银行领卡" in pending[0]["text"]
