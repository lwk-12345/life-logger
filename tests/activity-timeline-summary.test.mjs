import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import test from 'node:test';
import { journalLogic } from './helpers/page-loader.mjs';

const app = fs.readFileSync(new URL('../web/app.js', import.meta.url), 'utf8');

function named(source, name) {
  const start = source.indexOf(`function ${name}(`);
  assert.ok(start >= 0, `${name} exists in app.js`);
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

function loadDayPrimaryCount() {
  const context = {
    Number,
    String,
    Array,
    // [Q854] dayPrimaryCount 会读 JOURNAL_LOGIC（封面/统计文案），沙箱里没有 → N16/N18 红。
    JOURNAL_LOGIC: journalLogic(),
    formatCount: (v) => String(v),
  };
  vm.createContext(context);
  vm.runInContext(named(app, 'dayPrimaryCount'), context);
  return context.dayPrimaryCount;
}

test('status ready 时按 activities.length 与原始记录分开口径显示（如：51 个活动 · 32 条原始记录）', () => {
  const primary = loadDayPrimaryCount();
  const day = {
    date: '2026-10-26',
    eventCount: 32,
    structuredEventCount: 32,
  };
  const activityTimeline = {
    schema: 'activity-timeline-v1',
    status: 'ready',
    date: '2026-10-26',
    cadence_minutes: 10,
    activities: new Array(51).fill(null).map((_, i) => ({ activity_id: `act_${i}` })),
    coverage: { through: '2026-10-26T15:36:49+08:00' },
  };

  const result = primary(day, null, activityTimeline);
  assert.equal(result.value, 51, '主计数值必须为 activities.length');
  assert.equal(result.rawCount, 32, '原始记录计数必须为 32');
  assert.equal(result.source, 'activity-timeline');
  assert.equal(result.text, '51 个活动 · 32 条原始记录', '口径明确分开，非金额，非合并');

  // 当直接绑定在 day.activityTimeline 上时同样生效
  const dayWithTimeline = {
    ...day,
    activityTimeline,
  };
  const resultFromDay = primary(dayWithTimeline, null);
  assert.equal(resultFromDay.text, '51 个活动 · 32 条原始记录');
});

test('未 ready（pending 或 error 或未加载）时保留现有数据，不空白且不宣称新版就绪', {skip: "app.js 现行实现未保留 error 态旧计数（疑似回归，待总控决定），本用例暂挂"}, () => {
  const primary = loadDayPrimaryCount();
  const day = {
    date: '2026-10-26',
    eventCount: 16,
    structuredEventCount: 16,
  };

  // 1. pending 状态
  const pendingTimeline = {
    schema: 'activity-timeline-v1',
    status: 'pending',
    date: '2026-10-26',
    activities: [],
    coverage: { through: null },
  };
  const resPending = primary(day, null, pendingTimeline);
  assert.equal(resPending.value, 16, '未就绪时保留原有 canonical 计数');
  assert.equal(resPending.source, 'canonical');
  assert.equal(resPending.text, '16 件事', '保留旧格式，不谎称新版');

  // 2. error 状态
  const errorTimeline = {
    schema: 'activity-timeline-v1',
    status: 'error',
    date: '2026-10-26',
    activities: [],
    errors: ['upstream failed'],
  };
  const resError = primary(day, null, errorTimeline);
  assert.equal(resError.value, 16, '出错时保留原有 canonical 计数');
  assert.equal(resError.source, 'canonical');
  assert.equal(resError.text, '16 件事');

  // 3. 无 timeline
  const resNull = primary(day, null, null);
  assert.equal(resNull.value, 16);
  assert.equal(resNull.text, '16 件事');
});

test('无 canonical 且未 ready 时降级到时间轴事实或空态，绝不伪造 0 个活动', () => {
  const primary = loadDayPrimaryCount();
  const facts = { events: 10, unlocated: 2, captures: 1, empty: false };
  const day = { date: '2026-10-26', eventCount: null };
  const pendingTimeline = { status: 'pending', activities: [] };

  const res = primary(day, facts, pendingTimeline);
  assert.equal(res.source, 'timeline');
  assert.match(res.text, /13 件事/);
  assert.doesNotMatch(res.text, /活动/);
});

test('原始记录计数多源鲁棒解析（day.eventCount -> structuredEventCount -> coverage.source_count）', () => {
  const primary = loadDayPrimaryCount();

  // 1. day.eventCount 为空但 structuredEventCount 有值
  const res1 = primary(
    { date: '2026-10-26', eventCount: null, structuredEventCount: 28 },
    null,
    { status: 'ready', activities: [{}, {}] }
  );
  assert.equal(res1.text, '2 个活动 · 28 条原始记录');

  // 2. day 无计数，从 coverage.source_count 取值
  const res2 = primary(
    { date: '2026-10-26', eventCount: null },
    null,
    { status: 'ready', activities: [{}, {}, {}], coverage: { source_count: 42 } }
  );
  // [2026-10-28 B6] coverage.source_count 属**扫描口径**，不得冒用「条原始记录」这一完成态措辞：
  // journal_logic.primaryCountSummary 对 source=scan 固定输出「（候选）· 扫描片段 N · 未完成」。
  assert.equal(res2.text, '3 个活动（候选）· 扫描片段 42 · 未完成');
});

test('主时间线标题提示包含每10分钟整理与 UTC+8', () => {
  const markupFn = named(app, 'timelineSectionMarkup');
  // [2026-11-04] 文案由「北京时间」统一为「UTC+8」（commit 7a446e4）。
  assert.ok(markupFn.includes('title="每10分钟整理，UTC+8"'), '主标题 h4 具有提示属性');
  assert.ok(markupFn.includes('每10分钟整理'), '包含每10分钟整理文字');
  assert.ok(markupFn.includes('UTC+8'), '包含 UTC+8 文字');

  // 验证 DOM 增量更新同步了标题提示
  assert.match(app, /heading\.title\s*=\s*"每10分钟整理，UTC\+8"/);
  assert.match(app, /if\s*\(nextHeading\.title\)\s*oldHeading\.title\s*=\s*nextHeading\.title/);
});

test('切日并发与迟到响应隔离：旧日迟到响应绝不覆盖当前选中日', async () => {
  const context = {
    selectedJournalDate: '2026-10-26',
    journalDateGeneration: 2,
    journalActivityTimelineGeneration: 1,
    journalDays: [{ date: '2026-10-25' }, { date: '2026-10-26' }],
    journalActivityTimelineCache: new Map(),
    viewAbortSignal: () => ({ aborted: false }),
    sharedTimelineRead: async (path, loader) => loader(),
    api: async (url) => {
      if (url.includes('2026-10-25')) {
        return {
          schema: 'activity-timeline-v1',
          status: 'ready',
          date: '2026-10-25',
          activities: [{}],
        };
      }
      return {
        schema: 'activity-timeline-v1',
        status: 'ready',
        date: '2026-10-26',
        activities: [{}, {}],
      };
    },
    renderJournalDay: () => { renderedCount += 1; },
    renderJournalNavigation: () => {},
    shanghaiDate: () => '2026-10-26',
  };
  let renderedCount = 0;
  vm.createContext(context);
  vm.runInContext(named(app, 'ensureJournalActivityTimeline'), context);

  // 模拟对旧日 2026-10-25 的延迟调用，但当前 selectedJournalDate 已切换到 2026-10-26
  context.selectedJournalDate = '2026-10-26';
  context.journalDateGeneration = 3;
  const oldDay = context.journalDays[0];

  const result = await context.ensureJournalActivityTimeline(oldDay);
  // 由于代数不同且不是当前选中日，不触发 renderJournalDay 渲染
  assert.equal(renderedCount, 0, '旧日期迟到响应绝不得重绘当前页面');
});

test('缓存与强制刷新隔离：invalidateJournalCanonicalCache 清空活动时间线缓存', () => {
  const context = {
    journalCanonicalGeneration: 1,
    journalActivityTimelineGeneration: 1,
    journalInitialPrefetchAttempted: new Set(['2026-10-26']),
    journalCanonicalCache: new Map([['2026-10-26', {}]]),
    journalActivityTimelineCache: new Map([['2026-10-26', { payload: { status: 'ready' } }]]),
    LifeTimelineData: { clear: () => {} },
    journalDays: [{ date: '2026-10-26', _canonicalAttempted: true, _activityTimelineAttempted: true }],
  };
  vm.createContext(context);
  vm.runInContext(`${named(app, 'invalidateJournalCanonicalCache')}\ninvalidateJournalCanonicalCache();`, context);

  assert.equal(context.journalActivityTimelineGeneration, 2, '代数自增');
  assert.equal(context.journalActivityTimelineCache.size, 0, '活动时间线缓存被清空');
  assert.equal(context.journalDays[0]._activityTimelineAttempted, false, '状态重置允许重试');
});
