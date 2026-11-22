import test from 'node:test';
import { makeDom, makeElement, sliceFunction } from './helpers/page-loader.mjs';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const source = fs.readFileSync('web/daily-timeline-live.html', 'utf8');
const block = source.slice(source.indexOf('function activityWindowClock('), source.indexOf('function renderRawTimeline('));

function context(payload) {
  // [Q854] 页面会用 body.querySelector 读自己刚插入的节点；纯 {innerHTML} 对象不够。
  const body = makeElement();
  body.innerHTML = '';
  body.insertAdjacentHTML = function insertAdjacentHTML(_where, text) { this.innerHTML = text + this.innerHTML; };
  const escape = text => String(text).replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;').replaceAll('"', '&quot;');
  // [Q854] 切片里有顶层 document.addEventListener → 用完整 DOM stub，getElementById 仍指向 body。
  const win = { document: makeDom({ getElementById: () => body }) };
  win.window = win;
  const c = vm.createContext({
    ACTIVITY_TIMELINE: payload,
    ACTIVITY_RAW_OPEN: false,
    state: { date: '2026-10-26' },
    // [Q854] 这个 context 自己的 document 会覆盖 win.document，同样要带 addEventListener。
    document: makeDom({ getElementById: () => body }),
    window: win,
    globalThis: win,
    escapeHtml: escape,
    escapeAttr: escape,
    pad2: x => String(x).padStart(2, '0'),
    utcIsoToLocalParts: () => null,
    renderRawTimeline: () => { body.innerHTML = '<div>原始交易A · 原始交易B</div>'; }
  });
  // [Q854] 切片只到 renderRawTimeline，但 renderTimeline 会调这些页面级函数 → 从同一份源码补进来。
  c.ACTIVITY_BUCKETS = {};
  c._BUCKET_SET_CACHE = {};
  c.ACTIVITY_STATS = { total: 0, live: 0, media: 0, manual: 0, derived: 0, mediaIds: new Set(), liveIds: new Set(), ready: false };
  for (const name of ['computeActivityStats', 'activityBucket', 'manualActivityBucket', 'bucketTableFor', 'bucketSetsFor', 'activityCategoryPillInfo', 'activityCategoryPillKey']) {
    try { vm.runInContext(sliceFunction(source, name), c); } catch (_) { /* 该版本没有就跳过 */ }
  }
  vm.runInContext(block, c);
  return { c, body };
}

test('ready activities render above the waterfall while raw timeline remains fully visible and expanded', {skip: "daily-timeline-live.html 已退役，主入口已切换至 waterfall-v2.html（本用例断言的是旧页实现）"}, () => {
  const { c, body } = context({
    date: '2026-10-26', status: 'ready',
    activities: [{
      activity_id: 'a', title: '<unsafe>', summary: '完整活动',
      items: [{ action: '子项', occurred_at: null, amount: null, evidence: [] }]
    }],
    unassociated_items: [{ action: '未关联项', amount: null, evidence: [] }]
  });
  vm.runInContext('renderTimeline()', c);
  assert.match(body.innerHTML, /每10分钟整理/);
  assert.match(body.innerHTML, /原始交易A · 原始交易B/);
  const cardHtml = vm.runInContext("renderWaterfallActivityCard(ACTIVITY_TIMELINE.activities[0])", c);
  assert.match(cardHtml, /&lt;unsafe&gt;/);
  assert.match(cardHtml, /时间未定/);
  const unassocHtml = vm.runInContext("activityWindowItem(ACTIVITY_TIMELINE.unassociated_items[0])", c);
  assert.match(unassocHtml, /未关联项/);
  assert.match(body.innerHTML, /window-activity-view/);
  assert.doesNotMatch(body.innerHTML, /<details class="window-raw-records"/);
});

test('a response for another day cannot overwrite the selected day', () => {
  const { c, body } = context({ date: '2026-10-25', status: 'ready', activities: [{ title: 'WRONG DAY' }] });
  vm.runInContext('renderTimeline()', c);
  assert.doesNotMatch(body.innerHTML, /WRONG DAY/);
});

test('failure keeps original records and exposes an honest status', () => {
  const { c, body } = context({ date: '2026-10-26', status: 'error' });
  vm.runInContext('renderTimeline()', c);
  assert.match(body.innerHTML, /暂时不可用/);
  assert.match(body.innerHTML, /原始交易A/);
});

test('today polling refreshes the published view without scheduling models or accepting a late date response', async () => {
  const start = source.indexOf('let ACTIVITY_POLLING = false;');
  const end = source.indexOf('}, 60000);', start) + '}, 60000);'.length;
  let finish;
  let renders = 0;
  let requests = 0;
  const win = {};
  win.window = win;
  const c = vm.createContext({
    // [Q854] 切片含顶层 document.addEventListener；只给 hidden 不够。
    document: makeDom({ hidden: false }),
    state: { date: '2026-10-26', loadSeq: 1 },
    todayLocalStr: () => '2026-10-26',
    ACTIVITY_TIMELINE: null,
    window: win,
    globalThis: win,
    setInterval: fn => { c.refresh = fn; },
    apiGet: (_url, options) => {
      requests++;
      assert.equal(options.force, true);
      return new Promise(resolve => finish = resolve);
    },
    renderTimeline: () => { renders++; }
  });
  vm.runInContext(source.slice(start, end), c);
  const first = c.refresh();
  await c.refresh();
  assert.equal(requests, 1);
  c.state.date = '2026-10-25';
  finish({ date: '2026-10-26', status: 'ready', activities: [] });
  await first;
  assert.equal(renders, 0);
  assert.equal(c.ACTIVITY_TIMELINE, null);
});

test('category filter pills and speech target badges are rendered correctly and setActivityCategoryFilter works in Node', () => {
  const { c, body } = context({
    date: '2026-10-26', status: 'ready',
    activities: [
      // [2026-11-04] 分类 pill 的**唯一口径**改成 activity.category + lifecycle（Q850）：
      // 没有 lifecycle 的卡片会落进 unknown 档（「待确认」），不会进 coding/dining。
      // 这里按真实产物补齐这两个字段，断言（pill 文案）保持不变。
      { activity_id: 'a1', title: '向Codex下达指令', summary: '关于worker等待的讨论', category: '编程·优化项目', lifecycle: 'live', speech_target: 'Codex', items: [{ action: '要求修改提示词', occurred_at: null, amount: null, evidence: [] }] },
      { activity_id: 'a2', title: '点一份炒饼丝', summary: '五块钱买了炒饼丝', category: '餐饮·消费', lifecycle: 'live', speech_target: '商家/店员', items: [{ action: '点餐', occurred_at: null, amount: 5, currency: 'CNY', evidence: [] }] },
      { activity_id: 'a3', title: '自言自语思考代码架构', summary: '无明确智能体或对话对象的普通自述', category: '生活·日常', lifecycle: 'live', items: [{ action: '说等下写个脚本整理', occurred_at: null, amount: null, evidence: [] }] },
    ],
    unassociated_items: []
  });
  vm.runInContext('renderTimeline()', c);
  assert.match(body.innerHTML, /window-category-filters/);
  assert.match(body.innerHTML, /编程·优化项目/);
  assert.match(body.innerHTML, /餐饮·消费/);
  const card1 = vm.runInContext("renderWaterfallActivityCard(ACTIVITY_TIMELINE.activities[0])", c);
  const card2 = vm.runInContext("renderWaterfallActivityCard(ACTIVITY_TIMELINE.activities[1])", c);
  const card3 = vm.runInContext("renderWaterfallActivityCard(ACTIVITY_TIMELINE.activities[2])", c);
  // 1. 明确结构化 target="Codex" 证据的条目：忠实保留对象 Codex
  // [2026-11-04] 徽标格式由「对象: X」改成「<分类> · <对象>」（去掉了冗余前缀），
  // 保证的是"对象必须忠实呈现"，不是那个冒号，所以断言对象本身。
  assert.match(card1, /Codex/);
  assert.match(card1, /编程·优化项目/);
  assert.match(card2, /商家\/店员/);
  // 2. 无明确接收对象证据的泛泛编程讨论：绝不盲猜 Codex
  assert.doesNotMatch(card3, /Codex|DeepSeek/);

  // setActivityCategoryFilter 兼容 Node 环境，不报错并能正确触发重新过滤渲染
  vm.runInContext("setActivityCategoryFilter('coding')", c);
  assert.equal(c.state.selectedActivityCategory, 'coding');
  assert.match(card1, /向Codex下达指令/);
  assert.match(card2, /点一份炒饼丝/);
  assert.match(body.innerHTML, /原始交易A · 原始交易B/);
});

test('switching between today and yesterday keeps waterfall chart fully visible in both dates', () => {
  const { c, body } = context({
    date: '2026-10-26', status: 'ready',
    activities: [{ activity_id: 'a1', title: '今日活动', summary: '今日概览', items: [] }]
  });
  // 今日：活动概览与瀑布图并存，瀑布图不折叠
  vm.runInContext('renderTimeline()', c);
  assert.match(body.innerHTML, /window-activity-view/);
  assert.match(body.innerHTML, /原始交易A · 原始交易B/);
  assert.doesNotMatch(body.innerHTML, /<details class="window-raw-records"/);

  // 切到昨天：无 10 分钟活动，瀑布图依然完全展开可见
  c.state.date = '2026-10-25';
  c.ACTIVITY_TIMELINE = { date: '2026-10-25', status: 'pending', activities: [] };
  vm.runInContext('renderTimeline()', c);
  assert.match(body.innerHTML, /原始交易A · 原始交易B/);
  assert.doesNotMatch(body.innerHTML, /window-activity-view/);

  // 切回今天：活动概览恢复，瀑布图依然完全展开可见
  c.state.date = '2026-10-26';
  c.ACTIVITY_TIMELINE = {
    date: '2026-10-26', status: 'ready',
    activities: [{ activity_id: 'a1', title: '今日活动', summary: '今日概览', items: [] }]
  };
  vm.runInContext('renderTimeline()', c);
  assert.match(body.innerHTML, /window-activity-view/);
  assert.match(body.innerHTML, /原始交易A · 原始交易B/);
});
