import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import test from 'node:test';
import { makeDom } from './helpers/page-loader.mjs';

const html = fs.readFileSync(new URL('../web/daily-timeline-live.html', import.meta.url), 'utf8');
const script = html.match(/<script>([\s\S]*)<\/script>/)[1];

function load() {
  const start = script.indexOf('const ACTIVITY_STATE_LABELS');
  const end = script.indexOf('function extractDataChips(ev)');
  assert.ok(end > start);
  assert.ok(start > 0 && end > start);
  // [Q854] 切片里有顶层 document.addEventListener（页面点击委托），沙箱必须提供 DOM。
  const ctx = { result: null, Map, Set, Array, String, Math, JSON, Number, Object, document: makeDom() };
  vm.createContext(ctx);
  vm.runInContext(`
    let ACTIVITY_CARDS_BY_EVENT = new Map();
    let ACTIVITY_LINKS_EDGES = [], EVENTS_DATA = [], UNLOCATED_EVENTS = [];
    function escapeHtml(s) { return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }
    function escapeAttr(s) { return escapeHtml(s); }
    ${script.slice(start, end)}
    result = { activityFactsHtml, activityDurationText, activityCardFor,
      setCards: (list) => { ACTIVITY_CARDS_BY_EVENT = new Map(list.map((c) => [c.source_event_id, c])); } };
  `, ctx);
  return ctx.result;
}
const api = load();
const degrade = (o = {}) => ({ card_id: 'card_1', source_event_id: 'evt_1', kind: 'fitness', title: '练胸',
  state: 'done', duration: { minutes: 45, unit: 'minutes', path: 'data.duration_minutes' }, location: { name: '健身房' },
  facts: [{ path: 'data.exercise.body_part', value: '胸' }], transactions: [], unknown_fields: [],
  evidence: { recording_id: 'rec_1', segment_id: 'seg_1', source_label: 'self' }, ...o });

test('按 source_event_id 附到既有事件卡，不新增独立卡片容器', () => {
  api.setCards([degrade()]);
  const out = api.activityFactsHtml({ event_id: 'evt_1' });
  assert.match(out, /act-facts/);
  assert.doesNotMatch(out, /class="event-card"/, '不得自造事件卡');
  assert.equal(api.activityFactsHtml({ event_id: 'evt_missing' }), '', '没有卡就不渲染');
});

test('状态未知不强显完成；未知不编造状态文案', () => {
  // [2026-11-04 Q851] unknown 不再给文案：无状态就不显示标签（不编造、不堆「状态未知」）。
  // 本用例真正要守的是「绝不把未知说成已完成」，这一条保持不变。
  api.setCards([degrade({ state: 'unknown' })]);
  const out = api.activityFactsHtml({ event_id: 'evt_1' });
  assert.doesNotMatch(out, /状态未知/);
  assert.doesNotMatch(out, /已完成/);
});

test('duration null 与 0 分开：null 不显示时长，0 显示 0 分钟', () => {
  api.setCards([degrade({ duration: null })]);
  assert.doesNotMatch(api.activityFactsHtml({ event_id: 'evt_1' }), /⏱/);
  api.setCards([degrade({ duration: { minutes: 0, unit: 'minutes' } })]);
  assert.match(api.activityFactsHtml({ event_id: 'evt_1' }), /0 分钟/);
});

test('展开区给出完整事实、支付引用（仅引用不吞并）与证据', () => {
  api.setCards([degrade({ transactions: [{ event_id: 'evt_pay', amount: 25, currency: 'CNY', role: 'expense' }] })]);
  const out = api.activityFactsHtml({ event_id: 'evt_1' });
  assert.match(out, /data\.exercise\.body_part/);
  assert.match(out, /支付引用 evt_pay · 25 CNY（仅引用，不计入活动）/);
  assert.match(out, /录音 rec_1/);
  assert.match(out, /片段 seg_1/);
});

test('未知字段只在展开区列出，不塞满卡片概要', () => {
  api.setCards([degrade({ unknown_fields: ['data.meal.consumed'] })]);
  const out = api.activityFactsHtml({ event_id: 'evt_1' });
  const summary = out.slice(0, out.indexOf('<details'));
  assert.doesNotMatch(summary, /data\.meal\.consumed/, '概要不塞未知字段');
  assert.match(out, /未提供：[\s\S]*data\.meal\.consumed/);
});

test('恶意文本被转义', () => {
  api.setCards([degrade({ facts: [{ path: 'data.x', value: '<img src=x onerror=alert(1)>' }] })]);
  const out = api.activityFactsHtml({ event_id: 'evt_1' });
  assert.doesNotMatch(out, /<img/);
  assert.match(out, /&lt;img/);
});

test('时长单位 seconds 正确显示', () => {
  assert.equal(api.activityDurationText({ duration: { minutes: 90, unit: 'seconds' } }), '90 秒');
  assert.equal(api.activityDurationText({ duration: null }), '');
});

test('事件页装载 activity_cards 并建立映射', () => {
  assert.match(html, /ACTIVITY_CARDS_BY_EVENT = new Map\(activityCardsFromDaily\(daily\)/);
  assert.match(html, /\$\{activityFactsHtml\(ev\)\}/, '卡内渲染');
});

// ===== 真实 wrapper：/api/daily → daily_summary.activity_cards =====
function loadDailyShape() {
  const start = script.indexOf('function activityCardsFromDaily(daily)');
  const end = script.indexOf('function applyData(dashboard, daily, coverage)');
  assert.ok(start > 0 && end > start);
  const ctx = { result: null, Array, Object };
  vm.createContext(ctx);
  vm.runInContext(script.slice(start, end) + '\nresult = { activityCardsFromDaily };', ctx);
  return ctx.result;
}
const shape = loadDailyShape();
const cardA = { card_id: 'card_A', source_event_id: 'evt_a', facts: [] };

test('真实 wrapper 形状 daily_summary.activity_cards 被读取', () => {
  assert.equal(JSON.stringify(shape.activityCardsFromDaily({ daily_summary: { activity_cards: [cardA] } }).map((c) => c.card_id)), '["card_A"]');
});

test('顶层 activity_cards 仅作旧 fixture 兼容', () => {
  assert.equal(JSON.stringify(shape.activityCardsFromDaily({ activity_cards: [cardA] }).map((c) => c.card_id)), '["card_A"]');
});

test('嵌套优先于顶层（不盲拼、不双份）', () => {
  const out = shape.activityCardsFromDaily({ daily_summary: { activity_cards: [cardA] }, activity_cards: [{ card_id: 'card_top' }] });
  assert.equal(JSON.stringify(out.map((c) => c.card_id)), '["card_A"]', '有嵌套时以契约形状为准');
});

test('无 activity_cards / 非数组 → 空数组，不报错', () => {
  assert.equal(JSON.stringify(shape.activityCardsFromDaily({})), '[]');
  assert.equal(JSON.stringify(shape.activityCardsFromDaily({ daily_summary: { activity_cards: null } })), '[]');
  assert.equal(JSON.stringify(shape.activityCardsFromDaily(null)), '[]');
});

test('applyData 使用 activityCardsFromDaily（不再只读顶层）', () => {
  assert.match(html, /ACTIVITY_CARDS_BY_EVENT = new Map\(activityCardsFromDaily\(daily\)/);
  assert.doesNotMatch(html, /const activityCards = Array\.isArray\(daily\.activity_cards\)/);
});
