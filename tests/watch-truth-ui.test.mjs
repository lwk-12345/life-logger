import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import test from 'node:test';

const app = fs.readFileSync(new URL('../web/app.js', import.meta.url), 'utf8');

function named(source, name) {
  const start = source.indexOf(`function ${name}(`);
  assert.ok(start >= 0, `${name} exists`);
  let depth = 0, opened = false;
  for (let i = source.indexOf('{', start); i < source.length; i += 1) {
    if (source[i] === '{') { depth += 1; opened = true; }
    if (source[i] === '}' && !--depth && opened) return source.slice(start, i + 1);
  }
  throw new Error(`missing end of ${name}`);
}

function load() {
  const context = {Number, String, Math, Object};
  vm.createContext(context);
  // 停录原因用到的常量表与计数格式化
  const labels = app.match(/const WATCH_ENDED_BY_LABELS = \{[\s\S]*?\n\};/);
  assert.ok(labels, 'WATCH_ENDED_BY_LABELS exists');
  vm.runInContext(labels[0], context);
  vm.runInContext('function formatCount(v){ return String(v); }', context);
  for (const name of ['watchSnapshotAgeLabel', 'watchTruthModel', 'watchTruthHtml', 'escapeHtml',
                      'watchStopReasonModel', 'watchStopClockText', 'watchStopDiagnosticsHtml']) {
    vm.runInContext(named(app, name), context);
  }
  return context;
}

const base = {device_id: 'watch-A', pending_segment_count: 20, reported_age_seconds: 30};

test('pending_is_real=true 显示待补传 N 段（红）', () => {
  const {watchTruthModel} = load();
  const model = watchTruthModel({...base, pending_is_real: true, missing_segments: 3,
    reported_vs_manifest: {verdict: 'server_confirms_missing', server_missing_segments: 3}});
  assert.equal(model.tone, 'error');
  assert.match(model.headline, /待补传 3 段/);
  assert.match(model.action, /等待设备补传/);
});

test('reported_only_stale 显示设备自报、未证实、快照时间（黄），不出现“立即重传”', () => {
  const {watchTruthModel} = load();
  const model = watchTruthModel({...base, reported_age_seconds: 12 * 60, stale: true,
    pending_is_real: false, missing_segments: 0,
    reported_vs_manifest: {verdict: 'reported_only_stale', server_missing_segments: 0}});
  assert.equal(model.tone, 'warning');
  assert.match(model.headline, /设备自报 20 段，服务端未证实/);
  assert.match(model.detail, /快照 12 分钟前/);
  assert.match(model.action, /等待设备上报/);
  assert.equal(/立即重传/.test(model.headline + model.detail + model.action), false);
});

test('reported_only 显示待确认（黄）', () => {
  const {watchTruthModel} = load();
  const model = watchTruthModel({...base, pending_is_real: false, missing_segments: 0,
    reported_vs_manifest: {verdict: 'reported_only', server_missing_segments: 0}});
  assert.equal(model.tone, 'warning');
  assert.match(model.headline, /设备自报 20 段待确认/);
  assert.match(model.action, /等待设备上报/);
});

test('clean 显示已同步（绿）', () => {
  const {watchTruthModel} = load();
  const model = watchTruthModel({...base, pending_segment_count: 0, pending_is_real: false,
    missing_segments: 0, reported_vs_manifest: {verdict: 'clean', server_missing_segments: 0}});
  assert.equal(model.tone, 'ready');
  assert.match(model.headline, /已同步/);
});

test('extra_segments 单独提示，不与缺失合并', () => {
  const {watchTruthModel} = load();
  const model = watchTruthModel({...base, pending_is_real: true, missing_segments: 2, extra_segments: 4,
    reported_vs_manifest: {verdict: 'server_confirms_missing', server_missing_segments: 2, server_extra_segments: 4}});
  assert.match(model.headline, /待补传 2 段/);
  assert.match(model.extra, /超出预期 4 段/);
  assert.equal(/待补传 6 段/.test(model.headline), false, '多收段不能抵缺口');
});

test('旧后端没有新字段时给谨慎文案且不崩', () => {
  const {watchTruthModel} = load();
  const legacy = watchTruthModel({device_id: 'watch-old', pending_segment_count: 20, reported_at: '2026-10-23T00:00:00+08:00'});
  assert.equal(legacy.tone, 'warning');
  assert.match(legacy.headline, /设备自报 20 段/);
  assert.match(legacy.detail, /旧服务端未校验|未校验/);
  assert.match(legacy.action, /等待设备上报/);
  assert.equal(watchTruthModel(null), null);
});

test('空列表不渲染任何块', () => {
  const {watchTruthHtml} = load();
  assert.equal(watchTruthHtml([]), '');
  assert.equal(watchTruthHtml(null), '');
  const html = watchTruthHtml([{...base, pending_is_real: true, missing_segments: 1,
    reported_vs_manifest: {verdict: 'server_confirms_missing', server_missing_segments: 1}}]);
  assert.match(html, /status-copy/);
  assert.match(html, /待补传 1 段/);
  assert.equal(/pending_is_real|reported_vs_manifest/.test(html), false, '不得把原始字段名画到页面');
});

// ---------------------------------------------------------------------------
// 停录原因（ended_by）三态 + 未确认 completion
// ---------------------------------------------------------------------------
const stopPayload = (overrides = {}) => ({
  ok: true,
  recent_stops: [{
    session_id: 'S1', ended_at: '2026-10-23T17:08:40+08:00',
    ended_by_source: 'client_reported', ended_by: 'user', end_reason: 'user_stop',
  }],
  counts: {complete_sessions: 1, unacked_completions: 0},
  ...overrides,
});

test('client_reported：显示设备上报的真实原因，并标注来源', () => {
  const {watchStopDiagnosticsHtml} = load();
  const html = watchStopDiagnosticsHtml(stopPayload());
  assert.match(html, /最近停录原因/);
  assert.match(html, /本人手动停止/);
  assert.match(html, /设备上报/);
  assert.match(html, /17:08/);
  assert.equal(/原因未知|旧数据无记录/.test(html), false);
});

test('client_reported 但枚举未知：原样显示，不编一个已知原因', () => {
  const {watchStopReasonModel, watchStopDiagnosticsHtml} = load();
  const model = watchStopReasonModel({ended_by_source: 'client_reported', ended_by: 'martian_landing'});
  assert.equal(model.tone, 'ready');
  assert.match(model.label, /martian_landing/);
  assert.match(watchStopDiagnosticsHtml(stopPayload({recent_stops: [
    {ended_at: '2026-10-23T17:08:40+08:00', ended_by_source: 'client_reported', ended_by: 'martian_landing'},
  ]})), /martian_landing/);
});

test('inferred：明确写“设备未上报，原因未知”，不得推断成用户停止', () => {
  const {watchStopReasonModel, watchStopDiagnosticsHtml} = load();
  const model = watchStopReasonModel({ended_by_source: 'inferred', ended_by: 'watch_client'});
  assert.equal(model.tone, 'warning');
  assert.equal(model.label, '设备未上报，原因未知');
  const html = watchStopDiagnosticsHtml(stopPayload({recent_stops: [
    {ended_at: '2026-10-23T17:08:40+08:00', ended_by_source: 'inferred', ended_by: 'watch_client'},
  ]}));
  assert.match(html, /设备未上报，原因未知/);
  assert.equal(/本人手动停止|你停止了录音/.test(html), false);
});

test('missing：旧数据明确写“旧数据无记录”', () => {
  const {watchStopReasonModel, watchStopDiagnosticsHtml} = load();
  assert.equal(watchStopReasonModel({}).label, '旧数据无记录');
  assert.equal(watchStopReasonModel({ended_by_source: 'missing'}).label, '旧数据无记录');
  const html = watchStopDiagnosticsHtml(stopPayload({recent_stops: [
    {ended_at: '2026-10-23T17:08:40+08:00', ended_by_source: 'missing'},
  ]}));
  assert.match(html, /旧数据无记录/);
  assert.match(html, /无记录/);
});

test('旧后端 404 / 缺字段：整块隐藏，不显示 0 条也不编原因', () => {
  const {watchStopDiagnosticsHtml} = load();
  assert.equal(watchStopDiagnosticsHtml(null), '');
  assert.equal(watchStopDiagnosticsHtml(undefined), '');
  assert.equal(watchStopDiagnosticsHtml({}), '');
  assert.equal(watchStopDiagnosticsHtml({ok: false, error: 'not found'}), '');
  assert.equal(watchStopDiagnosticsHtml({ok: true, recent_stops: [], counts: {}}), '');
  assert.equal(watchStopDiagnosticsHtml({ok: true, recent_stops: [], counts: {unacked_completions: 0}}), '');
});

test('未确认 completion：有数量才显示，且明确“不代表你停止了录音”', () => {
  const {watchStopDiagnosticsHtml} = load();
  const shown = watchStopDiagnosticsHtml(stopPayload({recent_stops: [], counts: {unacked_completions: 3}}));
  assert.match(shown, /3 场录音已结束/);
  assert.match(shown, /不代表你停止了录音/);
  // counts 缺字段 → 不显示这一行（不把缺失当 0）
  const missingCounts = watchStopDiagnosticsHtml({
    ok: true, recent_stops: [{ended_at: '2026-10-23T17:08:40+08:00', ended_by_source: 'missing'}], counts: {},
  });
  assert.equal(/场录音已结束/.test(missingCounts), false);
});

test('最近停录最多显示 5 条，且按服务端顺序', () => {
  const {watchStopDiagnosticsHtml} = load();
  const rows = Array.from({length: 8}, (_, i) => ({
    ended_at: `2026-10-23T1${i}:00:00+08:00`, ended_by_source: 'client_reported', ended_by: 'user',
  }));
  const html = watchStopDiagnosticsHtml({ok: true, recent_stops: rows, counts: {}});
  assert.equal((html.match(/watch-stop-row/g) || []).length, 5);
  assert.match(html, /10:00/);
  assert.equal(/18:00/.test(html), false);
});
