import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import test from 'node:test';

const source = fs.readFileSync(new URL('../web/app.js', import.meta.url), 'utf8');
const block = source.slice(source.indexOf('function renderDeepSeekSynthesisBlock(day)'), source.indexOf('\nfunction sendQuickDeepSeekQuestion('));
function render(synthesis) {
  const context = {day: {date: '2000-01-01', synthesis}, escapeHtml: value => String(value ?? '').replaceAll('<', '&lt;')};
  return vm.runInNewContext(`${block}\nrenderDeepSeekSynthesisBlock(day)`, context);
}

test('facts-only report shows degradation, retains manual retry and hides invented analysis', () => {
  const html = render({generation: {status: 'facts_only'}, narrative: {status: 'unavailable'},
    executive_summary: 'unsafe stale alias', attention_and_energy: {health_score: 82}});
  assert.match(html, /模型降级，本日仅展示已有记录与账本统计。/);
  assert.doesNotMatch(html, /unsafe stale alias|82\/100|class="insight-subcard"|id="deepseek-synthesize-trigger-btn"/);
  assert.match(html, /id="deepseek-resynthesize-btn"/);
});

test('old explicit fallback is degraded even with a nonempty summary', () => {
  const html = render({model_tier: 'local-fallback', executive_summary: 'old unsupported story'});
  assert.match(html, /模型降级/);
  assert.doesNotMatch(html, /old unsupported story/);
});

test('v2 rendering uses narrative fields over stale legacy aliases', () => {
  const html = render({executive_summary: 'obsolete root summary', narrative: {status: 'generated', sections: {
    executive_summary: 'fresh analysis', work_and_cognition_analysis: {architecture_decisions: null}}}});
  assert.match(html, /fresh analysis/);
  assert.doesNotMatch(html, /obsolete root summary/);
});

test('unverified historical analysis stays readable and null arrays do not crash', () => {
  const html = render({narrative: {status: 'legacy_unverified', sections: {
    executive_summary: 'historical analysis', work_and_cognition_analysis: {architecture_decisions: null}}}});
  assert.match(html, /historical analysis/);
  assert.match(html, /历史模型分析/);
});

test('missing report still offers explicit generation', () => {
  assert.match(render(null), /id="deepseek-synthesize-trigger-btn"/);
});
