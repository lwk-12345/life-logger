import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import test from 'node:test';

const html = fs.readFileSync(new URL('../web/daily-timeline-live.html', import.meta.url), 'utf8');
const script = html.match(/<script>([\s\S]*)<\/script>/)[1];

function named(source, name) {
  const start = source.indexOf(`function ${name}(`);
  assert.ok(start >= 0, `${name} exists`);
  let i = source.indexOf('(', start), paren = 0, body = -1;
  for (; i < source.length; i += 1) {
    if (source[i] === '(') paren += 1;
    else if (source[i] === ')' && --paren === 0) { body = source.indexOf('{', i); break; }
  }
  let depth = 0;
  for (let j = body; j < source.length; j += 1) {
    if (source[j] === '{') depth += 1;
    else if (source[j] === '}' && --depth === 0) return source.slice(start, j + 1);
  }
  throw new Error(`missing end of ${name}`);
}

function load() {
  const context = {Date, Math, Number, String, TZ_OFFSET_MIN: 480, pad2: (n) => String(n).padStart(2, "0")};
  vm.createContext(context);
  vm.runInContext(named(script, 'utcIsoToLocalParts'), context);
  for (const name of ['relativeAgeLabel', 'hhmmOf', 'lastRecordingTruthText']) {
    vm.runInContext(named(script, name), context);
  }
  return context;
}

const NOW = Date.parse('2026-10-24T10:00:00+08:00');

test('有真实接收时间：显示“最后录音 HH:MM（X前）”且按 Asia/Shanghai', () => {
  const {lastRecordingTruthText} = load();
  const out = lastRecordingTruthText({lastRecordingTruth: {
    receivedAt: '2026-10-24T09:30:00+08:00', receivedSource: 'segment_file_mtime',
    recordingEndAt: '2026-10-24T09:31:00+08:00', recordingEndSource: 'coverage_tail',
  }}, NOW);
  assert.equal(out.kind, 'received');
  assert.equal(out.text, '最后录音 09:30（30 分钟前）');
});

test('UTC 输入也按上海时区显示', () => {
  const {lastRecordingTruthText} = load();
  const out = lastRecordingTruthText({lastRecordingTruth: {
    receivedAt: '2026-10-24T01:30:00Z', receivedSource: 'segment_file_mtime',
  }}, NOW);
  assert.equal(out.text, '最后录音 09:30（30 分钟前）');
});

test('只有录音覆盖尾端：明确标注“录音尾端”，不得说成接收时间', () => {
  const {lastRecordingTruthText} = load();
  const out = lastRecordingTruthText({lastRecordingTruth: {
    receivedAt: '2026-10-24T09:30:00+08:00', receivedSource: 'coverage_tail',
    recordingEndAt: '2026-10-24T09:30:00+08:00', recordingEndSource: 'coverage_tail',
  }}, NOW);
  assert.equal(out.kind, 'coverage');
  assert.match(out.text, /^录音尾端 09:30/);
  assert.match(out.text, /非接收时间/);
  assert.doesNotMatch(out.text, /^最后录音/);
});

test('字段缺失或两者都无：显示原因或“未收到本日录音”，不显示空白 —', () => {
  const {lastRecordingTruthText} = load();
  assert.equal(lastRecordingTruthText({lastRecordingTruth: {}}, NOW).text, '未收到本日录音');
  assert.match(lastRecordingTruthText({lastRecordingTruth: {receivedReason: '没有可用的 Watch segment mtime'}}, NOW).text,
    /没有可用的 Watch segment mtime/);
});

test('旧后端缺字段时不报错（全部 undefined）', () => {
  const {lastRecordingTruthText} = load();
  assert.doesNotThrow(() => lastRecordingTruthText({}, NOW));
  assert.equal(lastRecordingTruthText({}, NOW).kind, 'none');
});

test('汇总行接入真值，不再用空白 — 或处理水位冒充最后录音', () => {
  const summary = named(script, 'renderLiveSummaryLine');
  assert.match(summary, /lastRecordingTruthText\(m\)/);
  assert.match(summary, /last-recording-truth/);
  assert.doesNotMatch(summary, /最后录音止于/);
});
