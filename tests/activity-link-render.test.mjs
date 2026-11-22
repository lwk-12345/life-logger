import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import test from 'node:test';
import { makeDom } from './helpers/page-loader.mjs';

const html = fs.readFileSync(new URL('../web/daily-timeline-live.html', import.meta.url), 'utf8');
const script = html.match(/<script>([\s\S]*)<\/script>/)[1];

function load() {
  const start = script.indexOf('const ACTIVITY_LINK_LABELS');
  const end = script.indexOf('function activityFactsHtml(ev)');
  // [Q854] 切片含顶层 document.addEventListener，补 DOM stub。
  const ctx = { result: null, Set, Map, Array, String, Math, JSON, Number, Object, document: makeDom() };
  vm.createContext(ctx);
  vm.runInContext(`
    function escapeHtml(s) { return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }
    function escapeAttr(s) { return escapeHtml(s); }
    let EVENTS_DATA = [], UNLOCATED_EVENTS = [], ACTIVITY_LINKS_EDGES = [];
    ${script.slice(start, end)}
    result = { activityLinksHtml, set: (edges, events) => { ACTIVITY_LINKS_EDGES = edges; EVENTS_DATA = events || []; } };
  `, ctx);
  return ctx.result;
}
const api = load();
const EDGE = (o = {}) => ({ edge_id: 'e1', relation: 'child_of', namespace: 'event', from_id: 'evt_child',
  to_id: 'evt_parent', activity_membership: false, verified: true, ...o });

test('父子关系渲染为可点击定位按钮（调用既有 selectEvent）', () => {
  api.set([EDGE()], [{ event_id: 'evt_parent' }, { event_id: 'evt_child' }]);
  const out = api.activityLinksHtml({ event_id: 'evt_parent' });
  assert.match(out, /定位 evt_child/);
  assert.match(out, /onclick="event\.stopPropagation\(\);selectEvent\('evt_child'\)"/);
});

test('显式 membership 标“声明，未核实”，不冒充已核实活动', () => {
  api.set([EDGE({ relation: 'member_of', namespace: 'event', activity_membership: true,
    from_id: 'evt_parent', to_id: 'activity:act_1' })], [{ event_id: 'evt_parent' }]);
  const out = api.activityLinksHtml({ event_id: 'evt_parent' });
  assert.match(out, /声明，未核实/);
  assert.match(out, /act_1/);
  assert.doesNotMatch(out, /已核实/);
});

test('recording/order/session 不作为活动，只标“同事件来源”', () => {
  api.set([EDGE({ relation: 'same_as', namespace: 'order', from_id: 'evt_parent', to_id: 'order:88' })],
    [{ event_id: 'evt_parent' }]);
  const out = api.activityLinksHtml({ event_id: 'evt_parent' });
  assert.match(out, /同事件来源/);
  assert.doesNotMatch(out, /定位 order:88/);
});

test('目标不在本日 → 安全回退文案，不产生假跳转', () => {
  api.set([EDGE({ from_id: 'evt_parent', to_id: 'evt_other_day' })], [{ event_id: 'evt_parent' }]);
  const out = api.activityLinksHtml({ event_id: 'evt_parent' });
  assert.match(out, /目标不在本日/);
  assert.doesNotMatch(out, /selectEvent\('evt_other_day'\)/);
});

test('无 membership / 无关系 → 不渲染块（不伪造活动完成）', () => {
  api.set([], [{ event_id: 'evt_parent' }]);
  assert.equal(api.activityLinksHtml({ event_id: 'evt_parent' }), '');
});

test('XSS：目标 id 与标签被转义', () => {
  api.set([EDGE({ from_id: 'evt_parent', to_id: "evt_x' onclick='alert(1)" })], [{ event_id: 'evt_parent' }]);
  const out = api.activityLinksHtml({ event_id: 'evt_parent' });
  assert.doesNotMatch(out, /onclick='alert/);
  assert.match(out, /目标不在本日|&#39;/, '非法目标走安全回退或转义');
});

test('装载：activity_links 支持顶层与 daily_summary 内层', () => {
  assert.match(html, /const links = daily\.activity_links \|\| \(daily\.daily_summary && daily\.daily_summary\.activity_links\) \|\| \{\}/);
  assert.match(html, /ACTIVITY_LINKS_EDGES = Array\.isArray\(links\.edges\) \? links\.edges : \[\]/);
  assert.match(html, /applyData\(dashboard, daily, coverage\)/);
});
