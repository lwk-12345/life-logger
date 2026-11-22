import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import test from 'node:test';

// 2026-10-25 止损后的契约：
//  - 未知时刻“同证据自动抑制”已在产品调用点停用（开关 false），页面不得隐藏任何抓取记录；
//  - 候选算法（sameEvidenceForUnknownTime / evidenceIdentityMatch / collectUtteranceRefs）仍保留，
//    仅作为**未启用**的 helper 单测对象，不以产品行为断言；
//  - 已知时间旧路径（同签名 + 同分钟重合）保持不变。

const html = fs.readFileSync(new URL('../web/daily-timeline-live.html', import.meta.url), 'utf8');
const script = html.match(/<script>([\s\S]*)<\/script>/)[1];

function loadBlock() {
  const start = script.indexOf('function normalizeText');
  const end = script.indexOf('// ---- 装载：把真实数据填进原型的数据结构');
  assert.notEqual(start, -1);
  assert.notEqual(end, -1);
  const stubs = `
    const state = { date: "2026-10-23" };
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
    + '\nresult = { buildCaptures, sameEvidenceForUnknownTime, evidenceIdentityMatch, collectUtteranceRefs };', ctx);
  return ctx.result;
}
const { buildCaptures, sameEvidenceForUnknownTime, evidenceIdentityMatch } = loadBlock();

const SENT = '我们Lackback项目适不适合用这个X度的插件';
const QUOTE = '检查一下现在DeepSick上面的一个自动化流程……' + SENT + '……检查一下我们的Life Vlogger';
const cap = (o = {}) => ({ id: 'capX', raw_text: SENT, extracted_content: SENT,
  captured_at: '2026-10-23T05:57:49.479000+00:00', metadata: {}, ...o });
const ev = (o = {}) => ({ event_id: 'evt_session_2e326b6ebed12d7e', title: '会话', minute: null,
  precision: 'unknown', date_is_unknown: true, effective_date: '2026-10-23', quote: QUOTE,
  evidence: {}, ...o });
const build = (c, e) => buildCaptures({ captured_todos: [c] }, [e]);

// ============ helper 级：候选算法仍在，但**未启用**（不删除、不 skip） ============
test('［未启用 helper 候选］同 utterance_id → helper 判定 same=true', () => {
  const verdict = sameEvidenceForUnknownTime(cap({ metadata: { utterance_id: 'u1' } }),
    ev({ evidence: { utterance_id: 'u1' } }), '2026-10-23', '2026-10-23');
  assert.equal(verdict.same, true);
  assert.equal(verdict.reason, 'same_utterance_ref');
  assert.match(html, /const UNKNOWN_TIME_AUTOSUPPRESS_ENABLED = false;/, '候选仍处于未启用状态');
});

test('［未启用 helper 候选］capture.source_event_id === event_id → helper same=true', () => {
  const verdict = sameEvidenceForUnknownTime(cap({ metadata: { source_event_id: 'evt_session_2e326b6ebed12d7e' } }),
    ev(), '2026-10-23', '2026-10-23');
  assert.equal(verdict.same, true);
});

test('［未启用 helper 候选］同 segment 不同 utterance → helper 判冲突（候选边界仍有效）', () => {
  const verdict = evidenceIdentityMatch(cap({ metadata: { segment_id: 'seg7', utterance_id: 'u2' } }),
    ev({ evidence: { segment_id: 'seg7', utterance_id: 'u1' } }));
  assert.equal(verdict.same, false);
  assert.equal(verdict.conflict, true);
});

test('［未启用 helper 候选］仅共享 recording → helper 只给候选，不去重', () => {
  const verdict = evidenceIdentityMatch(cap({ metadata: { recording_id: 'rec_A' } }),
    ev({ evidence: { recording_id: 'rec_A' } }));
  assert.equal(verdict.same, false);
  assert.match(String(verdict.reason), /candidate_only/);
});

// ============ 产品级：止损后不得隐藏记录（可见性契约） ============
test('产品止损：未知时刻 + 同 utterance 引用 → 仍显示抓取，且不进 suppressed', () => {
  const r = build(cap({ metadata: { utterance_id: 'u1' } }), ev({ evidence: { utterance_id: 'u1' } }));
  assert.equal(r.captures.length, 1, '同句原记录必须可见');
  assert.equal(r.captures[0].event_id, 'cap_todo_capX');
  assert.equal(r.suppressed.length, 0, '不得进入 suppressed（避免误隐藏）');
  assert.equal(r.stats.deduped, 0);
});

test('产品止损：真实 payload 无来源（仅 status/query_text）→ 保留重复且可见', () => {
  const r = build(cap({ metadata: { status: 'pending', query_text: SENT } }), ev());
  assert.equal(r.captures.length, 1);
  assert.equal(r.suppressed.length, 0);
});

test('产品止损：主体/utterance 冲突也不隐藏', () => {
  const r = build(cap({ metadata: { utterance_id: 'u2', person: '张三' } }),
    ev({ evidence: { utterance_id: 'u1', source_label: '李四' } }));
  assert.equal(r.captures.length, 1);
  assert.equal(r.suppressed.length, 0);
});

test('产品止损：同 recording 不同 segment / 仅共享 recording 都保留', () => {
  assert.equal(build(cap({ metadata: { recording_id: 'rec_A', segment_id: 's2' } }),
    ev({ evidence: { recording_id: 'rec_A', segment_id: 's1' } })).captures.length, 1);
  assert.equal(build(cap({ metadata: { recording_id: 'rec_A' } }),
    ev({ evidence: { recording_id: 'rec_A' } })).captures.length, 1);
});

// ============ 已知时间旧路径：必须保持不变 ============
test('已知时间：同分钟 + 原话重合仍只留事件卡（旧路径未变）', () => {
  const known = { event_id: 'evt_known', title: '检查自动化流程', minute: 837, precision: 'minute',
    quote: '检查一下现在DeepSick上面的一个自动化流程。', evidence: { recording_id: 'rec_K' } };
  const c = { id: 'k1', raw_text: '检查一下现在DeepSick上面的一个自动化流程。', extracted_content: '检查 DeepSick 上的自动化流程',
    captured_at: '2026-10-23T05:55:31.178000+00:00', metadata: {} };
  const r = buildCaptures({ captured_todos: [c] }, [known]);
  assert.equal(r.captures.length, 0, '同分钟重合去重保持原行为');
  assert.equal(r.suppressed.length, 0, '旧路径不使用 suppressed');
});

test('已知时间：同一抓取重复出现一次（同签名）仍去重', () => {
  const c = cap({ id: 'dup1', extracted_content: '买咖啡', raw_text: '买咖啡' });
  const r = buildCaptures({ captured_todos: [c, { ...c }] }, []);
  assert.equal(r.captures.length, 1);
});

test('已知时间：同文本不同分钟（两次购买）→ 两张都保留', () => {
  const r = buildCaptures({ captured_expenses: [
    { id: 'b1', extracted_content: '买咖啡', captured_at: '2026-10-23T01:00:00.000000+00:00', metadata: { amount: 18 } },
    { id: 'b2', extracted_content: '买咖啡', captured_at: '2026-10-23T06:00:00.000000+00:00', metadata: { amount: 20 } }] }, []);
  assert.equal(r.captures.length, 2);
});

// ============ helper 级边界（候选保留，未启用） ============
test('［未启用 helper 候选］已知时刻不进入未知去重', () => {
  const verdict = sameEvidenceForUnknownTime(cap({ metadata: { utterance_id: 'u1' } }),
    ev({ minute: 837, precision: 'minute', date_is_unknown: false, evidence: { utterance_id: 'u1' } }), '2026-10-23', '2026-10-23');
  assert.equal(verdict.same, false);
  assert.equal(verdict.reason, 'known_time');
});

test('［未启用 helper 候选］文本过短 → 不足为证', () => {
  const verdict = sameEvidenceForUnknownTime(cap({ extracted_content: '买咖啡' }),
    ev({ quote: '买咖啡买咖啡买咖啡买咖啡买咖啡', evidence: { utterance_id: 'u1' } }), '2026-10-23', '2026-10-23');
  assert.equal(verdict.same, false);
  assert.equal(verdict.reason, 'text_too_short');
});

test('［未启用 helper 候选］原话不在 quote 里 → 不认证据', () => {
  const verdict = sameEvidenceForUnknownTime(cap({ metadata: { utterance_id: 'u1' } }),
    ev({ quote: '完全无关的一段很长的讲述内容，用来凑够二十个字符以上', evidence: { utterance_id: 'u1' } }),
    '2026-10-23', '2026-10-23');
  assert.equal(verdict.same, false);
  assert.equal(verdict.reason, 'text_not_in_quote');
});

test('［未启用 helper 候选］同 utterance 但跨日 → 日期不符', () => {
  const verdict = sameEvidenceForUnknownTime(cap({ metadata: { utterance_id: 'u1' } }),
    ev({ evidence: { utterance_id: 'u1' } }), '2026-10-22', '2026-10-23');
  assert.equal(verdict.same, false);
  assert.equal(verdict.reason, 'date_mismatch');
});

test('［未启用 helper 候选］主体冲突优先于引用相同', () => {
  const verdict = sameEvidenceForUnknownTime(cap({ metadata: { utterance_id: 'u1', person: '张三' } }),
    ev({ evidence: { utterance_id: 'u1', source_label: '李四' } }), '2026-10-23', '2026-10-23');
  assert.equal(verdict.same, false);
  assert.equal(verdict.reason, 'subject_conflict');
});

test('［未启用 helper 候选］缺一侧对象 → 直接拒绝', () => {
  assert.equal(sameEvidenceForUnknownTime(cap(), null, '2026-10-23', '2026-10-23').reason, 'missing_side');
  assert.equal(sameEvidenceForUnknownTime(null, ev(), '2026-10-23', '2026-10-23').reason, 'missing_side');
});

test('［未启用 helper 候选］只共享 segment（无原句引用）→ 候选但不判同证据', () => {
  const verdict = evidenceIdentityMatch(cap({ metadata: { segment_id: 'seg7' } }),
    ev({ evidence: { segment_id: 'seg7' } }));
  assert.equal(verdict.same, false);
  assert.match(String(verdict.reason), /candidate_only/);
});
