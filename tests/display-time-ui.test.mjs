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
  const context = {String};
  vm.createContext(context);
  for (const name of ['timeBasisLabel', 'eventDisplayTime']) vm.runInContext(named(script, name), context);
  return context;
}

test('三类时间依据都有中文标签，且约时/推断不会被当成精确', () => {
  const {timeBasisLabel} = load();
  assert.equal(timeBasisLabel('capture_statement'), '按讲述时间（约）');
  assert.equal(timeBasisLabel('inferred_occurrence'), '按段转录时间推断');
  assert.equal(timeBasisLabel('exact_occurrence'), '精确时刻');
  assert.equal(timeBasisLabel('reported_local'), '只有讲述时间（发生未知）');
  assert.equal(timeBasisLabel(undefined), '时间未知');
});

test('卡片优先使用 display_time.label；缺字段时回退 null（由调用方用 HH:MM）', () => {
  const {eventDisplayTime} = load();
  const capture = eventDisplayTime({event_time_basis: 'capture_statement', display_time: {label: '约14:34（按讲述时间）', basis: 'capture_statement'}});
  assert.equal(capture.label, '约14:34（按讲述时间）');
  const inferred = eventDisplayTime({event_time_basis: 'inferred_occurrence', display_time: {label: '09:10（按段转录时间）'}});
  assert.equal(inferred.label, '09:10（按段转录时间）');
  assert.equal(inferred.basis, 'inferred_occurrence');
  assert.equal(eventDisplayTime({event_time_basis: 'exact_occurrence', display_time: {label: '14:34'}}).label, '14:34');
  assert.equal(eventDisplayTime({}).label, null, '旧后端无字段时不编造');
});

test('reported_local / artifact_directory 归入时刻未知带', () => {
  const unknown = named(script, 'eventOccurrenceUnknown');
  assert.match(unknown, /event_time_basis/);
  for (const basis of ['reported_local', 'artifact_directory', 'unknown']) {
    assert.ok(unknown.includes(`"${basis}"`), `${basis} 必须触发未定位`);
  }
});

test('卡片与抽屉都使用 display_time 标签，不再直接裸显示 hh:mm', () => {
  assert.match(script, /time_str: display\.label/);
  assert.match(script, /时间依据/);
  assert.match(script, /timeBasisLabel/);
});


test('capture_statement/inferred 进入时间位置，reported/artifact 留在未知', () => {
  const source = named(script, 'eventOccurrenceUnknown');
  assert.match(source, /capture_statement/);
  assert.match(source, /inferred_occurrence/);
  assert.match(source, /reported_local/);
  assert.match(source, /artifact_directory/);
  assert.match(script, /ev-time\$\{.*is-approximate/);
  assert.match(script, /aria-label=/);
});
