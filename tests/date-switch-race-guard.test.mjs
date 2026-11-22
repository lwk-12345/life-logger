import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import test from 'node:test';

const app = fs.readFileSync(new URL('../web/app.js', import.meta.url), 'utf8');

function named(source, name) {
  const start = source.indexOf(`function ${name}(`);
  assert.ok(start >= 0, `${name} exists`);
  let paren = 0, bodyStart = -1;
  for (let i = source.indexOf('(', start); i < source.length; i += 1) {
    if (source[i] === '(') paren += 1;
    if (source[i] === ')' && !--paren) { bodyStart = source.indexOf('{', i); break; }
  }
  assert.ok(bodyStart >= 0, `${name} body exists`);
  let depth = 0;
  for (let i = bodyStart; i < source.length; i += 1) {
    if (source[i] === '{') depth += 1;
    if (source[i] === '}' && !--depth) return source.slice(start, i + 1);
  }
  throw new Error(`missing end of ${name}`);
}
function loadHelper(name) {
  const context = vm.createContext({});
  vm.runInContext(`${named(app, name)}\nthis.helper = ${name};`, context);
  return context.helper;
}

test('显式选日真实写入权威日期并递增 generation', () => {
  const context = vm.createContext({
    LifeTimelineData: {selectDate() {}},
    document: {querySelectorAll() { return []; }},
    writeStorageVersioned() {},
    renderJournalNavigation() {},
    freezeJournalSwitchHeight() {},
    journalSwitchingUntil: 0,
    journalSelectionInitialized: false,
    journalUserSelectedDate: '',
    journalDateGeneration: 7,
    selectedJournalDate: '2026-10-25',
  });
  vm.runInContext(`${named(app, 'selectJournalDate')}\nthis.select = selectJournalDate;`, context);
  context.select('2026-10-23');
  assert.equal(context.journalUserSelectedDate, '2026-10-23');
  assert.equal(context.selectedJournalDate, '2026-10-23');
  assert.equal(context.journalDateGeneration, 8);
});

test('迟到 today 响应不能通过当前选中日的守卫', () => {
  const allowed = loadHelper('journalDayAllowedForSelection');
  assert.equal(allowed({date: '2026-10-25'}, '2026-10-24'), false);
  assert.equal(allowed({date: '2026-10-24'}, '2026-10-24'), true);
  assert.equal(allowed({date: '2026-10-25'}, ''), true);
  assert.equal(allowed(null, '2026-10-24'), true);
});

test('日期导航优先显式选日，未选日时才使用 latest', () => {
  const target = loadHelper('journalNavigationTarget');
  const initial = (dates, persisted) => (dates.includes(persisted) ? persisted : dates[0] || null);
  assert.equal(target({
    selectedDate: '2026-10-25', explicitDate: '2026-10-23', selectionInitialized: true,
    dates: ['2026-10-25'], latest: '2026-10-25', initialDate: initial,
  }), '2026-10-23');
  assert.equal(target({
    selectedDate: '', explicitDate: '', selectionInitialized: true,
    dates: ['2026-10-25', '2026-10-24'], latest: '2026-10-25', initialDate: initial,
  }), '2026-10-25');
  assert.equal(target({
    selectedDate: '2026-10-22', explicitDate: '', selectionInitialized: false,
    dates: ['2026-10-25'], latest: '2026-10-25', initialDate: () => '2026-10-25',
  }), '2026-10-25');
});

test('切日后旧 bulk refresh 的 generation 已失效', () => {
  const current = loadHelper('journalRefreshStillCurrent');
  assert.equal(current(2, 2), true);
  assert.equal(current(2, 3), false);
  assert.equal(current(undefined, 0), true);
});


test('iframe 报 0 条但未确认 empty 时不得画成 0 件事', () => {
  const context = vm.createContext({Number, String, formatCount: (v) => String(v)});
  vm.runInContext(`${named(app, 'dayPrimaryCount')}\nthis.primary = dayPrimaryCount;`, context);
  assert.equal(context.primary({date: '2026-10-24', eventCount: null}, {events: 0, unlocated: 0, captures: 0, empty: false}).value, null);
  assert.equal(context.primary({date: '2026-10-24', eventCount: null}, {events: 0, unlocated: 0, captures: 0, empty: true}).value, 0);
});


test('已渲染日期以 iframe 画完的日期为准', () => {
  const rendered = loadHelper('timelineRenderedDate');
  assert.equal(rendered({contentDocument: {documentElement: {dataset: {renderedDate: '2026-10-23'}}}}), '2026-10-23');
  assert.equal(rendered({contentDocument: {documentElement: {dataset: {}}}}), '', '没有完成态日期时不得认为已在目标日');
  assert.equal(rendered(null), '');
});
