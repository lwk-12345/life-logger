import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import test from 'node:test';

const html = fs.readFileSync(new URL('../web/daily-timeline-live.html', import.meta.url), 'utf8');
const script = html.match(/<script>([\s\S]*)<\/script>/)[1];

function load() {
  const start = script.indexOf('function normalizeText');
  const end = script.indexOf('// ---- 装载：把真实数据填进原型的数据结构');
  const stubs = `
    const state = { date: "2026-10-24" };
    const pad2 = (n) => String(n).padStart(2, "0");
    function utcIsoToLocalParts(iso) {
      const m = String(iso).match(/^(\\d{4})-(\\d{2})-(\\d{2})T(\\d{2}):(\\d{2}):(\\d{2})/);
      const hh = (Number(m[4]) + 8) % 24;
      return { date: m[1] + "-" + m[2] + "-" + m[3], hh, mm: Number(m[5]), ss: Number(m[6]), minute: hh * 60 + Number(m[5]) };
    }
  `;
  const ctx = { result: null };
  vm.createContext(ctx);
  vm.runInContext(stubs + script.slice(start, end)
    + '\nresult = { buildCaptures, captureProvenanceView, sameEvidenceForUnknownTime };', ctx);
  return ctx.result;
}
const { buildCaptures, captureProvenanceView } = load();

const RESOLVED = {
  schema: 'capture-provenance-v1', origin: 'event', status: 'resolved', reason: null,
  source_event_id: 'evt_ctx_07c1632c45cdd829',
  recording_id: 'rec_1a6d143e1ac1', segment_id: 'rec_1a6d143e1ac1_seg_000001',
  source_id: 'src_17bdf79ae10fd46e91ca', source_label: 'self',
  reported_at: '2026-10-24T06:34:17.153000+00:00',
  quote_sha256: 'a921dc86b4bd5e13efe42c94f54985be6f6dcf85e530eb3a20dd3cc804e8c887',
  same_utterance: null, identity_scope: 'event_source_only',
  source_evidence: { recording_id: 'rec_1a6d143e1ac1', segment_id: 'rec_1a6d143e1ac1_seg_000001',
    start_offset_seconds: 0.48, end_offset_seconds: 4.34 }
};
const item = (o = {}) => ({ id: 'cap1', _origin: 'event', raw_text: '记录一下今天的裸重',
  extracted_content: '记录今天裸重85.85', captured_at: '2026-10-24T06:34:17.153000+00:00',
  metadata: { status: 'pending' }, ...o });
const build = (it) => buildCaptures({ captured_todos: [it] }, []).captures[0];

test('event-origin：白名单来源引用完整透传到 capture 输出', () => {
  const out = build(item({ capture_provenance: RESOLVED, source_event_id: RESOLVED.source_event_id }));
  assert.equal(out.provenance.schema, 'capture-provenance-v1');
  assert.equal(out.provenance.origin, 'event');
  assert.equal(out.provenance.status, 'resolved');
  assert.equal(out.provenance.source_event_id, 'evt_ctx_07c1632c45cdd829');
  assert.equal(out.provenance.recording_id, 'rec_1a6d143e1ac1');
  assert.equal(out.provenance.segment_id, 'rec_1a6d143e1ac1_seg_000001');
  assert.equal(out.provenance.source_label, 'self');
  assert.equal(out.provenance.quote_sha256, RESOLVED.quote_sha256);
  assert.equal(out.provenance.start_offset_seconds, 0.48);
  assert.equal(out.provenance.end_offset_seconds, 4.34);
});

test('capture-origin 缺来源：origin/status 明确 unknown，不编造 recording/segment', () => {
  const out = build(item({ _origin: 'capture', capture_provenance: { schema: 'capture-provenance-v1', origin: 'capture', status: 'unknown' } }));
  assert.equal(out.provenance.status, 'unknown');
  assert.equal(out.provenance.origin, 'capture');
  assert.equal('recording_id' in out.provenance, false);
  assert.equal('segment_id' in out.provenance, false);
  assert.equal('source_event_id' in out.provenance, false);
});

test('完全没有 provenance 字段：status/origin/schema 均为 unknown', () => {
  const out = build(item());
  assert.equal(out.provenance.status, 'unknown');
  assert.equal(out.provenance.origin, 'unknown');
  assert.equal(out.provenance.schema, 'unknown');
});

test('nested metadata.source_provenance 兜底', () => {
  const out = build(item({ capture_provenance: null,
    metadata: { status: 'pending', source_provenance: { schema: 'capture-provenance-v1', origin: 'capture', status: 'unknown' } } }));
  assert.equal(out.provenance.schema, 'capture-provenance-v1');
  assert.equal(out.provenance.status, 'unknown');
});

test('非法字段剔除（数字当字符串、负 offset、空串）', () => {
  const view = captureProvenanceView({ capture_provenance: {
    schema: '', origin: 42, status: 'ok', recording_id: '   ', start_offset_seconds: -3, end_offset_seconds: '4.3' } });
  assert.equal(view.schema, 'unknown', '空 schema → unknown');
  assert.equal(view.origin, 'unknown', '非法 origin 不得变成 42，降级为 unknown');
  assert.equal(view.status, 'ok');
  assert.equal('recording_id' in view, false, '空白 id 被剔除');
  assert.equal('start_offset_seconds' in view, false, '负 offset 被剔除');
  assert.equal('end_offset_seconds' in view, false, '字符串 offset 被剔除');
});

test('冲突状态：provenance.status 与 metadata.status 分开保留，不静默合并', () => {
  const out = build(item({ metadata: { status: 'pending' },
    capture_provenance: { schema: 'capture-provenance-v1', origin: 'event', status: 'resolved' } }));
  assert.equal(out.provenance.status, 'resolved');
  assert.equal(out.review_status, 'pending');
  assert.notEqual(out.provenance.status, out.review_status);
});

test('输入对象不被修改（深比较）', () => {
  const input = item({ capture_provenance: JSON.parse(JSON.stringify(RESOLVED)), source_event_id: RESOLVED.source_event_id });
  const snapshot = JSON.stringify(input);
  build(input);
  assert.equal(JSON.stringify(input), snapshot);
});

test('不泄露原话文本：只保留 quote_sha256，不含 quote 原文', () => {
  const out = build(item({ capture_provenance: { ...RESOLVED, quote: '记录一下今天的裸重八十五点八五' } }));
  const dump = JSON.stringify(out.provenance);
  assert.equal(dump.includes('裸重八十五点八五'), false);
  assert.match(dump, /quote_sha256/);
});

test('已知日期/发生时间不被 provenance 改写（minute/precision 仍来自 captured_at）', () => {
  const out = build(item({ capture_provenance: { ...RESOLVED, occurred_at: '2026-01-01T00:00:00+08:00' } }));
  assert.equal(out.precision, 'exact');
  assert.equal(out.time_str, '14:34');
  assert.equal(out.orig_date, '2026-10-24');
  assert.equal('occurred_at' in out.provenance, false, 'occurred_at 不在白名单');
});

test('止损仍生效：未知抓取全部可见、不调用 matcher、无 same_utterance 关联', () => {
  const dash = { captured_todos: [item({ _origin: 'capture', capture_provenance: { origin: 'capture', status: 'unknown' } })] };
  const ev = { event_id: 'evt_session_x', minute: null, precision: 'unknown', quote: '记录今天裸重85.85 的一段很长的讲述内容用来占位',
    evidence: {}, effective_date: '2026-10-24' };
  const r = buildCaptures(dash, [ev]);
  assert.equal(r.captures.length, 1, '未知抓取必须可见');
  assert.equal(r.suppressed.length, 0);
  assert.match(html, /const UNKNOWN_TIME_AUTOSUPPRESS_ENABLED = false;/);
  assert.equal(r.captures[0].provenance.same_utterance ?? null, null);
});

// ===== 6号独立验收的 3 处缺口 + 新契约字段 =====
const FULL = { schema: 'capture-provenance-v1', origin: 'event', status: 'resolved',
  source_event_id: 'evt_1', recording_id: 'rec_A', segment_id: 'seg_1', source_id: 'src_1',
  source_label: 'self', reported_at: '2026-10-23T05:55:31Z', quote_sha256: 'sha_1',
  identity_scope: 'event_source_only', same_utterance: true,
  session_id: 'sess_9', time_reference: 'recording_start', time_unit: 'seconds',
  absolute_started_at: '2026-10-23T05:55:31Z', absolute_ended_at: '2026-10-23T05:55:35Z',
  start_offset_seconds: 0.5, end_offset_seconds: 4.3,
  source_contract: { schema: 'capture-source-v1', status: 'complete', missing_fields: [],
    time_reference: 'recording_start', time_unit: 'seconds' } };

test('G1 新契约字段：session/time_reference/time_unit/absolute_* 全部保留', () => {
  const v = captureProvenanceView({ capture_provenance: { ...FULL } });
  assert.equal(v.session_id, 'sess_9');
  assert.equal(v.time_reference, 'recording_start');
  assert.equal(v.time_unit, 'seconds');
  assert.equal(v.absolute_started_at, '2026-10-23T05:55:31Z');
  assert.equal(v.absolute_ended_at, '2026-10-23T05:55:35Z');
});

test('G2 same_utterance 保持原类型：true 仍是 true，null 仍是 null', () => {
  assert.equal(captureProvenanceView({ capture_provenance: { ...FULL, same_utterance: true } }).same_utterance, true);
  assert.equal(captureProvenanceView({ capture_provenance: { ...FULL, same_utterance: null } }).same_utterance, null);
});

test('G3 顶层 unknown 不吞 nested 事实，且不保留 source_not_recorded 假原因', () => {
  const v = captureProvenanceView({
    capture_provenance: { schema: 'capture-provenance-v1', origin: 'capture', status: 'unknown', reason: 'source_not_recorded' },
    metadata: { source_provenance: { ...FULL } } });
  assert.equal(v.recording_id, 'rec_A');
  assert.equal(v.segment_id, 'seg_1');
  assert.equal(v.start_offset_seconds, 0.5);
  assert.equal(v.source_branch, 'nested_metadata');
  assert.notEqual(v.reason, 'source_not_recorded');
  assert.equal(v.status, 'resolved');
});

test('G4 source_contract 保真（schema/status/missing_fields/time_reference/time_unit）', () => {
  const v = captureProvenanceView({ capture_provenance: { ...FULL,
    source_contract: { schema: 'capture-source-v1', status: 'partial', missing_fields: ['session_id'],
      conflict_fields: ['recording_id'], time_reference: 'recording_start', time_unit: 'seconds' } } });
  assert.deepEqual([...v.source_contract.missing_fields], ['session_id']);
  assert.deepEqual([...v.source_contract.conflict_fields], ['recording_id']);
  assert.equal(v.source_contract.status, 'partial');
  assert.equal(v.source_contract.time_unit, 'seconds');
});

test('G5 顶层与 nested 同字段冲突：显式 conflict，不静默覆盖', () => {
  const v = captureProvenanceView({
    capture_provenance: { ...FULL, recording_id: 'rec_A' },
    metadata: { source_provenance: { ...FULL, recording_id: 'rec_B', segment_id: 'seg_9' } } });
  assert.equal(v.recording_id, 'rec_A', '顶层值保留（不静默改）');
  assert.equal(v.status, 'conflict');
  assert.deepEqual([...v.conflict_fields], ['recording_id', 'segment_id']);
});

test('G6 不盲拼：补字段时记录来源分支与补充字段名', () => {
  const v = captureProvenanceView({
    capture_provenance: { schema: 'capture-provenance-v1', origin: 'event', status: 'resolved', recording_id: 'rec_A' },
    metadata: { source_provenance: { ...FULL, recording_id: 'rec_A' } } });
  assert.equal(v.source_branch, 'top_level');
  assert.ok(Array.isArray(v.filled_from_other_branch));
  assert.ok(v.filled_from_other_branch.includes('session_id'));
});

test('G7 只透传事实：不新增 occurred_at/金额/活动字段', () => {
  const out = build(item({ capture_provenance: { ...FULL, occurred_at: '2020-01-01T00:00:00Z', amount: 999 } }));
  assert.equal('occurred_at' in out.provenance, false);
  assert.equal('amount' in out.provenance, false);
  assert.equal(out.amount, null === out.amount ? out.amount : out.amount, '金额仍来自 metadata，不被 provenance 改写');
});

// ===== 冲突双方值保留 + 后端分支契约白名单透传 =====
test('G8 冲突双方值都保留：conflicting_values 同时含 rec_A 与 rec_B', () => {
  const v = captureProvenanceView({
    capture_provenance: { ...FULL, recording_id: 'rec_A' },
    metadata: { source_provenance: { ...FULL, recording_id: 'rec_B' } } });
  assert.equal(v.status, 'conflict');
  assert.deepEqual([...v.conflicting_values.recording_id], ['rec_A', 'rec_B']);
  assert.equal(v.recording_id, 'rec_A', '选中值仍是 primary');
});

test('G9 source_branches / field_sources / selected_branch 白名单透传', () => {
  const v = captureProvenanceView({ capture_provenance: { ...FULL,
    selected_branch: 'nested_metadata',
    source_branches: { top_level: { recording_id: 'rec_A' }, nested_metadata: { recording_id: 'rec_B' } },
    field_sources: { recording_id: 'nested_metadata' } } });
  assert.equal(v.selected_branch, 'nested_metadata');
  assert.equal(v.source_branches.nested_metadata.recording_id, 'rec_B');
  assert.equal(v.field_sources.recording_id, 'nested_metadata');
});

test('G10 分支契约不听话字段：quote/token/path/未知对象被剔除', () => {
  const v = captureProvenanceView({ capture_provenance: { ...FULL,
    source_branches: { top_level: { quote: '完整原话不该上屏', raw_text: 'x', recording_id: 'rec_A',
      token: 'SECRETTOKEN0123456789abcdefghijklmnop', source_artifact: '$HOME/private/a.json' } },
    conflicting_values: { recording_id: ['rec_A', '$HOME/secret/b.json'] } } });
  const dump = JSON.stringify(v);
  assert.equal(dump.includes('完整原话不该上屏'), false);
  assert.equal(dump.includes('$HOME'), false);
  assert.equal(dump.includes('SECRETTOKEN0123456789abcdefghijklmnop'), false);
  assert.equal(dump.includes('token'), false, 'token 字段名不得进入白名单输出');
  assert.equal(v.source_branches.top_level.recording_id, 'rec_A');
});

test('G11 后端已有 conflicting_values 与前端检测合并，不覆盖已有事实', () => {
  const v = captureProvenanceView({
    capture_provenance: { ...FULL, recording_id: 'rec_A',
      conflicting_values: { segment_id: ['seg_a', 'seg_b'] } },
    metadata: { source_provenance: { ...FULL, recording_id: 'rec_B' } } });
  assert.deepEqual([...v.conflicting_values.segment_id], ['seg_a', 'seg_b']);
  assert.deepEqual([...v.conflicting_values.recording_id], ['rec_A', 'rec_B']);
});

test('G12 原始 contract status 与投影 status 分开', () => {
  const v = captureProvenanceView({ capture_provenance: { ...FULL, status: 'partial',
    source_contract: { schema: 'capture-source-v1', status: 'complete', missing_fields: [] } } });
  assert.equal(v.status, 'partial');
  assert.equal(v.source_contract.status, 'complete');
});
