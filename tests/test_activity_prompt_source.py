"""线上提示词只有代码里一份：不能依赖未入库的 work/ 文件，且内容经过显式登记。"""
import hashlib
from pathlib import Path

from life_logger.activity_timeline_runtime import PROMPT_V2_TEXT

ROOT = Path(__file__).resolve().parents[1]


def test_prompt_never_reads_the_work_directory():
    source = (ROOT / "src/life_logger/activity_timeline_runtime.py").read_text(encoding="utf-8")
    assert "window-cadence-20261026" not in source


def test_prompt_uses_the_live_15_minute_window():
    assert "最近15分钟ASR" in PROMPT_V2_TEXT
    assert "最近30分钟ASR" not in PROMPT_V2_TEXT


def test_prompt_asks_for_a_fact_type_on_each_item():
    assert "fact_type" in PROMPT_V2_TEXT
    for kind in ("expense", "food", "exercise", "smoke", "todo", "idea", "none"):
        assert kind in PROMPT_V2_TEXT


def test_prompt_change_is_a_deliberate_edit():
    digest = hashlib.sha256(PROMPT_V2_TEXT.strip().encode()).hexdigest()
    assert digest == "3e59bc2a56267329f8609362885ab358a39fd3ce3e3ddc84a99ad8744306a86f"
