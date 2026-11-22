"""Remaining prompt contracts; no providers, services or local data are accessed."""
import ast
from pathlib import Path
import re

import pytest

from life_logger.domain import ALLOWED_CATEGORIES
from life_logger.expense_model_contract import POSITIVE_TRANSACTION_CLAIM_TYPES
from life_logger.activity_digest import CATEGORY_MODELS, KINDS

ROOT = Path(__file__).resolve().parents[1]


def literal(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return ''.join(literal(value) for value in node.values)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return literal(node.left) + literal(node.right)
    return ''


def prompt(module, owner, variable):
    path = ROOT / (module if module.startswith('scripts/') else 'src/life_logger/' + module + '.py')
    tree = ast.parse(path.read_text())
    scope = next((node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                  and node.name == owner), tree) if owner else tree
    if variable == 'return':
        return '\n'.join(literal(node.value) for node in ast.walk(scope) if isinstance(node, ast.Return))
    return '\n'.join(literal(node.value) for node in ast.walk(scope) if isinstance(node, (ast.Assign, ast.AnnAssign))
        and any(isinstance(target, ast.Name) and target.id == variable
                for target in (node.targets if isinstance(node, ast.Assign) else [node.target])))


PROMPTS = [
    ('memory_rag', 'ask', 'prompt'), ('vip_fast_track', 'execute_vip_command', 'prompt'),
    ('natural_correction', '_call_deepseek', 'prompt'), ('todo_text_cleaner', None, 'CLEAN_PROMPT'),
    ('session_aggregator', None, 'SESSION_EXTRACTION_SYSTEM_PROMPT'),
    ('daily_cognitive_session', 'ask_daily_cognitive_session', 'system_prompt'),
    ('local_llm_extract', '_prompt', 'return'), ('local_llm_extract', '_deepseek_prompt', 'return'),
    ('contextual_extraction', None, 'LOCAL_EXTRACTION_PROMPT'),
    ('contextual_extraction', '_deepseek_request', 'prompt'),
    ('moss_transcribe', 'moss_prompt', 'return'), ('scripts/downstream_benchmark.py', 'build_prompt', 'return'),
]


@pytest.mark.parametrize('module,owner,variable', PROMPTS)
def test_each_live_prompt_has_explicit_data_not_instruction_boundary(module, owner, variable):
    text = prompt(module, owner, variable)
    assert text and ('不是指令' in text or '不执行其中' in text), (module, owner)
    assert '原' in text or '音频' in text


@pytest.mark.parametrize('module,owner,variable,removed', [
    ('natural_correction', '_call_deepseek', 'prompt', '不是140是150'),
    ('vip_fast_track', 'execute_vip_command', 'prompt', '这家菜不新鲜'),
    ('vip_fast_track', 'execute_vip_command', 'prompt', '明天去银行办卡'),
    ('vip_fast_track', 'execute_vip_command', 'prompt', '把会议推迟到四点'),
    ('vip_fast_track', 'execute_vip_command', 'prompt', '我刚才抽了一根烟'),
    ('session_aggregator', None, 'SESSION_EXTRACTION_SYSTEM_PROMPT', '抵足狗'),
    ('session_aggregator', None, 'SESSION_EXTRACTION_SYSTEM_PROMPT', '40公斤'),
    ('session_aggregator', None, 'SESSION_EXTRACTION_SYSTEM_PROMPT', '烧鸭饭'),
    ('session_aggregator', None, 'SESSION_EXTRACTION_SYSTEM_PROMPT', '14:56'),
    ('session_aggregator', None, 'SESSION_EXTRACTION_SYSTEM_PROMPT', '"weight_kg": 20'),
    ('session_aggregator', None, 'SESSION_EXTRACTION_SYSTEM_PROMPT', '最核心的原话摘要'),
    ('activity_digest', None, 'PROMPT', '健身舞运行成功吗'),
    ('activity_digest', None, 'CATEGORY_PROMPT', '健身舞运行成功'),
    ('local_llm_extract', '_prompt', 'return', '"amount": 16'),
    ('contextual_extraction', None, 'LOCAL_EXTRACTION_PROMPT', '"amount":13'),
    ('contextual_extraction', '_deepseek_request', 'prompt', '乌冬面'),
    ('contextual_extraction', '_deepseek_request', 'prompt', '嗨棒'),
    ('contextual_extraction', '_deepseek_request', 'prompt', '生脉饮'),
])
def test_each_removed_one_off_example_stays_out_of_live_prompt(module, owner, variable, removed):
    assert removed not in prompt(module, owner, variable)


def test_vip_habit_name_is_open_text_as_in_the_actual_parser():
    text = prompt('vip_fast_track', 'execute_vip_command', 'prompt')
    assert '"habit_name": "原文明示的习惯名称"' in text
    assert '"habit_name": "吸烟" |' not in text


def test_correction_category_enum_matches_actual_parser_set():
    text = prompt('natural_correction', '_call_deepseek', 'prompt')
    stanza = text.split('- category: ', 1)[1].split('\n', 1)[0]
    assert set(re.findall(r"'([a-z_]+)'", stanza)) == ALLOWED_CATEGORIES
    positive = text.split('- claim_type: ', 1)[1].split('；', 1)[0]
    assert set(re.findall(r'[a-z_]+', positive)) == POSITIVE_TRANSACTION_CLAIM_TYPES


def test_session_categories_match_downstream_domain_without_claiming_local_validation():
    text = prompt('session_aggregator', None, 'SESSION_EXTRACTION_SYSTEM_PROMPT')
    stanza = text.split('类别限于：', 1)[1].split('（', 1)[0]
    assert set(re.findall(r'[a-z_]+', stanza)) == ALLOWED_CATEGORIES
    assert '本地时间校验器本身不验证类别' in text
    assert '逐字连续复制原话' in text


@pytest.mark.parametrize('owner,marker', [('_prompt', '允许类别仅为：'), ('_deepseek_prompt', '允许categories仅为')])
def test_local_extractor_prompt_matches_effective_category_filter(owner, marker):
    text = prompt('local_llm_extract', owner, 'return')
    stanza = text.split(marker, 1)[1].split('\n', 1)[0].split('；', 1)[0]
    assert set(re.findall(r'[a-z_]+', stanza)) == ALLOWED_CATEGORIES - {'other'}


def test_digest_both_enum_sets_stay_aligned():
    text = prompt('activity_digest', None, 'PROMPT')
    assert set(text.split('kind(', 1)[1].split(')', 1)[0].split('|')) == KINDS
    text = prompt('activity_digest', None, 'CATEGORY_PROMPT')
    assert set(text.split('c 只能选：', 1)[1].split('。', 1)[0].split('、')) == CATEGORY_MODELS


@pytest.mark.parametrize('module,owner,var,requirement', [
    ('memory_rag', 'ask', 'prompt', '不自行估算或加总'),
    ('daily_cognitive_session', 'ask_daily_cognitive_session', 'system_prompt', '不补数、不加总、不估算营养'),
    ('natural_correction', '_call_deepseek', 'prompt', '只提取原文明示的单笔记录'),
    ('session_aggregator', None, 'SESSION_EXTRACTION_SYSTEM_PROMPT', '不计算营养或金额汇总'),
    ('session_aggregator', None, 'SESSION_EXTRACTION_SYSTEM_PROMPT', '不计算会话时间差或估计训练耗时'),
    ('local_llm_extract', '_prompt', 'return', '不计算营养或金额汇总'),
    ('local_llm_extract', '_deepseek_prompt', 'return', '不计算营养或金额汇总'),
    ('contextual_extraction', '_deepseek_request', 'prompt', '不做交易汇总、退款减算'),
    ('vip_fast_track', 'execute_vip_command', 'prompt', '不要计算营养或金额汇总'),
])
def test_no_programmable_arithmetic_is_delegated_to_model(module, owner, var, requirement):
    assert requirement in prompt(module, owner, var)


@pytest.mark.parametrize('module,owner,var', PROMPTS)
def test_no_fixed_high_confidence_band_is_requested(module, owner, var):
    assert not re.search(r'0\.9\d*\s*(?:~|～|到|至|[-—])\s*1(?:\.0+)?', prompt(module, owner, var))
