"""v3.1 pre-enable gates; real responses are replayed separately, no network."""
import copy
import json

import pytest

from life_logger import activity_prompt_v3, fact_schema
from test_activity_v3_shadow import RECORD, ROOT, response, run, setup as setup, v3_body


def test_lengths_share_schema_validator_and_rendered_prompt(setup, monkeypatch):
    schema = fact_schema.json_schema()
    activity = schema['properties']['activities']['items']
    assert activity['properties']['title']['maxLength'] == 20
    assert activity['properties']['items']['items']['properties']['action']['maxLength'] == 40
    monkeypatch.setitem(fact_schema.STRING_MAX_LENGTHS, 'activity', {'title': 3})
    monkeypatch.setitem(fact_schema.STRING_MAX_LENGTHS, 'item', {'action': 4})
    runtime = setup({'version': 'v3', 'mode': 'shadow', 'user_profile': ''})
    from test_activity_v3_shadow import CUTOFF, LOOKBACK
    prompt = activity_prompt_v3.build_prompt(runtime.load_config(), CUTOFF, LOOKBACK, [RECORD], [])
    assert '标题≤3 字、动作≤4 字' in prompt
    result = fact_schema.validate_payload(v3_body())
    assert result['ok'] and result['non_confirmed'][0]['reason'] == 'field_too_long'
    assert result['non_confirmed'][0]['fields'][0]['maximum'] == 3
    assert fact_schema.json_schema()['properties']['activities']['items']['properties']['title']['maxLength'] == 3


@pytest.mark.parametrize('field,limit', [('title', 20), ('action', 40)])
def test_length_boundary_holds_whole_original_item_only(field, limit):
    body = v3_body()
    second = copy.deepcopy(body['activities'][0]); second['activity_id'] = 'kept'
    body['activities'].append(second)
    target = body['activities'][0] if field == 'title' else body['activities'][0]['items'][0]
    target[field] = '字' * limit
    assert fact_schema.validate_payload(body)['non_confirmed'] == []
    target[field] += '多'
    before = copy.deepcopy(body)
    result = fact_schema.validate_payload(body)
    assert result['ok'] and result['errors'] == []
    assert result['counts']['non_confirmed'] == 1
    assert result['non_confirmed'][0]['reason'] == 'field_too_long'
    assert result['non_confirmed'][0]['item'] == before['activities'][0]['items'][0]
    assert [a['activity_id'] for a in result['confirmed_payload']['activities']] == ['kept']
    assert body == before


def test_date_length_is_also_shared_and_overlong_date_is_held():
    body = v3_body()
    item = body['activities'][0]['items'][0]
    item['occurred_date'] = '2026-11-05'
    assert fact_schema.validate_payload(body)['non_confirmed'] == []
    item['occurred_date'] += '1'
    result = fact_schema.validate_payload(body)
    assert result['ok'] and result['non_confirmed'][0]['reason'] == 'field_too_long'
    assert result['non_confirmed'][0]['fields'][0]['field'] == 'occurred_date'
    schema = fact_schema.json_schema()['properties']['activities']['items']['properties']['items']['items']
    assert schema['properties']['occurred_date']['maxLength'] == fact_schema.DATE_LENGTH


def test_overlong_sibling_does_not_remove_other_items():
    body = v3_body(); items = body['activities'][0]['items']
    held = copy.deepcopy(items[0]); held['item_id'] = 'held'; held['action'] = '长' * 50
    items.append(held)
    result = fact_schema.validate_payload(body)
    assert result['ok'] and result['confirmed_payload']['activities'][0]['items'] == [items[0]]
    assert result['non_confirmed'][0]['item']['action'] == '长' * 50
    items[0]['unexpected'] = True
    assert not fact_schema.validate_payload(body)['ok']


@pytest.mark.parametrize('score', [float('nan'), float('inf'), -float('inf'), True, '0.8', None, -0.01, 1.01])
def test_bad_confidence_is_invalid_without_clamping(score):
    body = v3_body(); body['activities'][0]['items'][0]['confidence'] = score
    result = fact_schema.validate_payload(body)
    assert not result['ok']
    assert result['confirmed_payload'] is None
    assert any(e['field'] == 'confidence' for e in result['errors'])


@pytest.mark.parametrize('score', [0, 1])
def test_confidence_boundaries_and_no_invented_default(score):
    body = v3_body(); body['activities'][0]['items'][0]['confidence'] = score
    result = fact_schema.validate_payload(body)
    assert result['ok'] and result['non_confirmed'] == []
    assert result['confidence_gate'] == {'configured': False, 'threshold': None}
    schema = fact_schema.json_schema()['properties']['activities']['items']['properties']['items']['items']
    assert schema['properties']['confidence']['minimum'] == 0
    assert schema['properties']['confidence']['maximum'] == 1


@pytest.mark.parametrize('number', [float('nan'), float('inf'), -float('inf')])
def test_nonfinite_expense_rejected(number):
    body = v3_body(); body['activities'][0]['items'][0]['expense']['amount'] = number
    assert not fact_schema.validate_payload(body)['ok']


def test_root_extra_field_rejected_for_json_object_fallback():
    body = v3_body(); body['extra'] = 'do not accept'
    result = fact_schema.validate_payload(body)
    assert not result['ok']
    assert {'where': '$', 'field': 'extra', 'error': 'unknown_field', 'value': 'do not accept'} in result['errors']
    assert fact_schema.json_schema()['additionalProperties'] is False


@pytest.mark.parametrize('bad', [-0.1, 1.1, True, '0.8', float('nan')])
def test_explicit_invalid_threshold_is_configuration_error(bad):
    result = fact_schema.validate_payload(v3_body(), confidence_threshold=bad)
    assert not result['ok'] and any(e['where'] == '$config' for e in result['errors'])


def test_explicit_threshold_holds_below_and_keeps_equal_without_mutation():
    body = v3_body(); items = body['activities'][0]['items']
    items[0]['confidence'] = 0.79
    equal = copy.deepcopy(items[0]); equal.update(item_id='equal', confidence=0.8); items.append(equal)
    before = copy.deepcopy(body)
    result = fact_schema.validate_payload(body, confidence_threshold=0.8)
    assert result['ok'] and result['counts']['non_confirmed'] == 1
    assert result['non_confirmed'][0]['reason'] == 'low_confidence'
    assert result['non_confirmed'][0]['item']['confidence'] == 0.79
    assert result['confirmed_payload']['activities'][0]['items'] == [equal]
    assert body == before


@pytest.mark.parametrize('reason', ['field_too_long', 'low_confidence'])
def test_live_holds_items_in_evidence_checked_unassociated_lane(setup, reason):
    body = v3_body()
    if reason == 'field_too_long': body['activities'][0]['items'][0]['action'] = '字' * 50
    else: body['activities'][0]['items'][0]['confidence'] = 0.79
    runtime = setup({'version': 'v3', 'mode': 'live', 'confidence_threshold': 0.8}, lambda _: response(body))
    result = run(runtime)
    assert result['activities'] == []
    item = result['unassociated_items'][0]
    assert item['pending_reason'] == reason and item['review_status'] == 'non_confirmed'
    assert item['evidence'][0]['row_id'] == RECORD['row_id']
    runtime.publish_day('2026-11-05', result)
    saved = runtime.read_day('2026-11-05', for_display=False)
    assert saved['unassociated_items'][0]['pending_reason'] == reason
    from life_logger import judgment_queue, pending_review
    def hashes():
        import hashlib
        return {str(p.relative_to(runtime.output_root)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in runtime.output_root.rglob('*') if p.is_file()}
    before = hashes()
    records, _, _ = pending_review._collect_pending_items(runtime.output_root, {})
    candidate = next(r for r in records.values() if r.get('proposed_item'))
    assert candidate['reason'] == reason and candidate['state'] == 'open'
    assert candidate['proposed_item']['confidence'] == body['activities'][0]['items'][0]['confidence']
    assert candidate['quote'] == RECORD['text']
    assert any(r['source_id'] == candidate['source_id'] for r in judgment_queue._open_items(runtime.output_root))
    # Review resolution is stable, and reading does not write a second candidate store.
    resolved, _, _ = pending_review._collect_pending_items(runtime.output_root, {candidate['source_id']: {'status': 'handled'}})
    assert resolved[candidate['source_id']]['state'] == 'handled'
    assert hashes() == before
    from life_logger.expense_policy import build_expense_ledger
    assert build_expense_ledger([], activities=saved['activities'])['total_amount'] == 0


def test_shadow_uses_versioned_template_and_keeps_raw_body(setup):
    body = v3_body(); body['activities'][0]['items'][0]['action'] = '长' * 49
    seen = []
    def sender(payload):
        seen.append(payload['messages'][0]['content'])
        return response(body)
    runtime = setup({'version': 'v3', 'mode': 'shadow', 'user_profile': ''}, sender)
    run(runtime)
    archive = json.loads(next((runtime.output_root/'activity-shadow-v3').rglob('*.json')).read_text())
    assert archive['template_version'] == 'v3.3' and archive['status'] == 'valid'
    assert archive['result'] == body
    assert archive['attempts'][0]['body'] == json.loads(response(body)['choices'][0]['message']['content'])
    assert archive['non_confirmed'][0]['item']['action'] == '长' * 49
    assert archive['confirmed_result']['activities'] == []
    assert archive['confidence_gate']['configured'] is False
    assert '标题≤20 字、动作≤40 字' in seen[0]
    assert '七十，他给我减了十块' not in seen[0] and 'amount 填实付 60' not in seen[0]
    assert (ROOT/'config/activity-prompt-v3.md').read_bytes() == (ROOT/'work/prompt-v3-ab-20261106/prompt_v3_template.md').read_bytes()
    assert not (runtime.output_root/'activity-timeline').exists()
    from life_logger.pending_review import _collect_pending_items
    assert _collect_pending_items(runtime.output_root, {})[0] == {}
