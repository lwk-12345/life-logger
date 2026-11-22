import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import test from 'node:test';

const app = fs.readFileSync(new URL('../web/app.js', import.meta.url), 'utf8');

function takeFn(name) {
  const start = app.indexOf(`function ${name}(`);
  assert.notEqual(start, -1, `missing ${name}`);
  let depth = 0, i = app.indexOf('{', start);
  for (; i < app.length; i++) {
    if (app[i] === '{') depth++;
    else if (app[i] === '}' && --depth === 0) return app.slice(start, i + 1);
  }
  throw new Error('unclosed ' + name);
}
const ctx = { result: null, Set, Map, String, Array, Math, JSON };
vm.createContext(ctx);
vm.runInContext(`
  const STAGE_LABELS = { jev: "决策增强", asr: "语音转写", attribution: "声源归属", completed: "处理完成" };
  function stageLabel(stage) { return STAGE_LABELS[stage] || stage || "等待"; }
  function escapeHtml(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]));
  }
  ${takeFn('stageErrorReasonText')}
  ${takeFn('normalizeStageErrors')}
  ${takeFn('renderStageErrorBlock')}
  const STAGE_ERROR_STATUSES = new Set(["partial", "degraded", "failed", "interrupted"]);
  result = { stageErrorReasonText, normalizeStageErrors, renderStageErrorBlock };
`, ctx);
const { normalizeStageErrors, renderStageErrorBlock, stageErrorReasonText } = ctx.result;

const entry = (o = {}) => ({ file_id: 'f1', stage: 'jev', kind: 'stage_failed', severity: 'degradable',
  resolved: false, error: 'Jev 增强失败，退出码 1', ...o });

test('partial + stage_errors：显示阶段、处数与原因（error 为 null 时）', () => {
  const html = renderStageErrorBlock({ status: 'partial', error: null, stage_errors: [entry()] });
  assert.match(html, /失败阶段 1 处/);
  assert.match(html, /决策增强/);
  assert.match(html, /Jev 增强失败，退出码 1/);
  assert.doesNotMatch(html, /未提供/);
});

test('error 优先：两者都有时只渲染 error，不重复原因', () => {
  const html = renderStageErrorBlock({ status: 'failed', error: 'attribution 失败，退出码 2', stage_errors: [entry()] });
  assert.equal(html, '');
});

test('degraded 无 error 且无 stage_errors → 明确“未提供”，不编造', () => {
  const html = renderStageErrorBlock({ status: 'degraded', error: null, stage_errors: [] });
  assert.match(html, /失败原因：未提供/);
  assert.doesNotMatch(html, /失败阶段/);
});

test('failed 无 error 无 stage_errors → 也显示“未提供”', () => {
  assert.match(renderStageErrorBlock({ status: 'failed', error: null }), /未提供/);
});

test('completed / running 无错误 → 不渲染任何失败块（不制造噪音）', () => {
  assert.equal(renderStageErrorBlock({ status: 'completed', error: null, stage_errors: [] }), '');
  assert.equal(renderStageErrorBlock({ status: 'running', error: null, stage_errors: [] }), '');
});

test('resolved=true 的历史错误不当作当前失败', () => {
  const html = renderStageErrorBlock({ status: 'partial', error: null,
    stage_errors: [entry({ stage: 'jev', resolved: true }), entry({ resolved: false })] });
  assert.match(html, /失败阶段 1 处/);
});

test('同一文件同一阶段的重复条目只算一处；文件数与处数分开', () => {
  const html = renderStageErrorBlock({ status: 'partial', error: null, stage_errors: [
    entry({ file_id: 'f1', stage: 'jev', error: '同一原因' }),
    entry({ file_id: 'f1', stage: 'jev', error: '同一原因' }),
    entry({ file_id: 'f1', stage: 'asr', error: '另一个阶段' }),
    entry({ file_id: 'f2', stage: 'jev', error: '同一原因' })] });
  assert.match(html, /失败阶段 3 处/);
  assert.match(html, /涉及 2 个文件/);
});

test('恶意文本被转义，不产生可执行标记', () => {
  const html = renderStageErrorBlock({ status: 'partial', error: null,
    stage_errors: [entry({ error: '<img src=x onerror=alert(1)>" onclick="evil()' })] });
  assert.doesNotMatch(html, /<img/);
  assert.doesNotMatch(html, /onclick="evil/);
  assert.match(html, /&lt;img/);
});

test('不新增显示内部绝对路径 / 长 token / file_id', () => {
  const html = renderStageErrorBlock({ status: 'partial', error: null, stage_errors: [entry({
    file_id: 'aa04a3ff2ddcbdf61b4009c051c7eeebf739beeba6facd465e08edea3dc732d2',
    error: 'failed at $HOME/Documents/Codex/private/secret.py with Bearer abcdefghijklmnopqrstuvwxyz0123456789' })] });
  assert.doesNotMatch(html, /\/Users\/lwk/);
  assert.doesNotMatch(html, /abcdefghijklmnopqrstuvwxyz/);
  assert.doesNotMatch(html, /aa04a3ff2ddc/, 'file_id 不得上屏');
  assert.match(html, /secret\.py/, '路径只保留末段');
});

test('原因首行截断（traceback 只展示第一行）', () => {
  const reason = stageErrorReasonText('Traceback (most recent call last):\n  File "x.py", line 1\nValueError: boom');
  assert.equal(reason, 'Traceback (most recent call last):');
});

test('长原因截断到 160 字符以内', () => {
  assert.ok(stageErrorReasonText('a'.repeat(400)).length <= 160);
});

test('产品内 STAGE_ERROR_STATUSES 与测试扫描一致（partial/degraded/failed/interrupted）', () => {
  assert.match(app, /const STAGE_ERROR_STATUSES = new Set\(\["partial", "degraded", "failed", "interrupted"\]\);/);
});

test('normalizeStageErrors 返回处数与文件数（不混淆）', () => {
  const s = normalizeStageErrors([entry({ file_id: 'f1', stage: 'jev' }), entry({ file_id: 'f2', stage: 'jev' })]);
  assert.equal(s.count, 2);
  assert.equal(s.fileCount, 2);
});
