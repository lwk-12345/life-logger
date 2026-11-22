import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import test from 'node:test';

const app = fs.readFileSync(new URL('../web/app.js', import.meta.url), 'utf8');

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

function loadPrimary() {
  const context = {Number, String, formatCount: (v) => String(v)};
  vm.createContext(context);
  vm.runInContext(named(app, 'dayPrimaryCount'), context);
  return context.dayPrimaryCount;
}

test('同屏只保留一个主口径：canonical 优先，时间轴只在未就绪时顶上并标注', () => {
  const primary = loadPrimary();
  const facts = {events: 11, unlocated: 7, captures: 0, empty: false};
  const canonical = primary({date: '2026-10-22', eventCount: 12}, facts);
  assert.equal(canonical.value, 12, 'canonical 在时必须用 canonical');
  assert.equal(canonical.source, 'canonical');
  assert.equal(canonical.text, '12 件事', '主口径不附带第二个数字');

  const timeline = primary({date: '2026-10-22', eventCount: null}, facts);
  assert.equal(timeline.value, 18);
  assert.equal(timeline.source, 'timeline');
  assert.match(timeline.text, /18 件事/);
  assert.match(timeline.text, /含时间未定位 7/, '时间轴口径必须说明含未定位');
  assert.doesNotMatch(timeline.text, /12 件事/);
});

test('没有任何数据时不编造 0 件事', () => {
  const primary = loadPrimary();
  const unknown = primary({date: '2026-10-24', eventCount: null}, null);
  assert.equal(unknown.value, null);
  assert.equal(unknown.text, '');
});

test('有内容就不许说“没有记录”，读取失败只显示未读取成功+重试', () => {
  const source = named(app, 'renderJournalDay');
  // 有内容分支
  assert.match(source, /时间轴已显示/);
  assert.match(source, /统计口径（分类与账本）仍在读取/);
  // 失败分支
  assert.match(source, /未读取成功（统计口径）/);
  assert.match(source, /这不等于“没有记录”/);
  assert.match(source, /尚未读取到这一天的记录/);
  // 旧的自相矛盾文案不得存在
  assert.doesNotMatch(app, /这一天没有录音或事件证据/);
});

test('事件轴 metric 与主摘要使用同一个数字', () => {
  assert.match(app, /事件轴（主口径）/);
  // 「同一个数字」的约束仍然成立：主摘要与指标卡都走同一次 dayPrimaryCount(day, timelineFactsFor(day.date))，
  // 只是主摘要现在先绑局部变量 primaryCount 再取 .text（原来直接链式取 .value）。
  assert.match(app, /const primaryCount = dayPrimaryCount\(day, timelineFactsFor\(day\.date\)\);/);
  assert.match(app, /primaryCount\.text/);
  assert.match(app, /primaryCountMetricHtml\(day, timelineFactsFor\(day\.date\)\)/);
});
