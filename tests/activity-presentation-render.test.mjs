import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";
import test from "node:test";
import { makeDom } from './helpers/page-loader.mjs';

const html = fs.readFileSync(new URL("../web/daily-timeline-live.html", import.meta.url), "utf8");
const start = html.indexOf("function activityPresentationCount");
const end = html.indexOf("function extractDataChips", start);
assert.ok(start >= 0 && end > start, "activity presentation render helpers exist");

function build() {
  const focused = [];
  const context = {
    ACTIVITY_PRESENTATION: {groups: [], conflicts: []},
    ACTIVITY_STATE_LABELS: {done: "已完成", in_progress: "进行中", planned: "计划中", cancelled: "已取消", unknown: "状态未知"},
    formatCount: (value) => String(value),
    escapeHtml: (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c])),
    escapeAttr: (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c])),
    // [Q854] 页面顶层有 document.addEventListener，只给 querySelectorAll 不够。
    document: makeDom({querySelectorAll: () => []}),
    selectEvent: (id) => focused.push(id),
    focused,
  };
  vm.createContext(context);
  vm.runInContext(html.slice(start, end), context);
  return context;
}

const group = {
  group_id: "group:activity:g1",
  activity_ref: "activity:g1",
  basis: {label: "显式声明 · 未核实", activity_entity_verified: false},
  members: [
    {event_id: "evt-a", known: true, title: "点单：鸭腿饭", state: "done", time: {clock: "00:19", date: "2026-10-25"}, location: {name: "餐厅"}},
    {event_id: "evt-b", known: true, title: "加辣", state: "unknown", time: {clock: "00:19", date: "2026-10-25"}, location: null},
  ],
  summary: {title: "鸭腿饭", member_count: 2, states: ["done", "unknown"], clocks: ["00:19"], locations: ["餐厅"], transaction_count: 2, amounts_summed: false, duration_summed: false},
};

test("有组时渲染紧凑摘要、成员跳转入口和未核实标签", () => {
  const context = build();
  context.ACTIVITY_PRESENTATION = {groups: [group], conflicts: []};
  const rendered = vm.runInContext("activityPresentationHtml()", context);
  assert.match(rendered, /活动摘要/);
  assert.match(rendered, /显式声明 · 未核实/);
  assert.match(rendered, /2 个关联记录/);
  assert.match(rendered, /关联交易 2 笔（仅引用）/);
  assert.match(rendered, /点单：鸭腿饭/);
  assert.match(rendered, /加辣/);
  assert.match(rendered, /00:19/);
  assert.match(rendered, /餐厅/);
  assert.doesNotMatch(rendered, /已核实(?!性)/);
  assert.doesNotMatch(rendered, /合计|总和|总时长/);
});

test("无组时完全不渲染空面板；冲突友好提示展示且不破坏原事件", () => {
  const context = build();
  assert.equal(vm.runInContext("activityPresentationHtml()", context), "");
  context.ACTIVITY_PRESENTATION = {groups: [], conflicts: [{activity_ref: "activity:bad", reason: "declaration_conflict", event_ids: ["evt-a"]}]};
  const conflict = vm.runInContext("activityPresentationHtml()", context);
  assert.match(conflict, /待核对/);
  assert.match(conflict, /bad/);
  assert.match(conflict, /相关联的记录尚未在本日找到，已保留原始片段供核对/);
  assert.doesNotMatch(conflict, /class="activity-group"/);
  assert.doesNotMatch(conflict, /dangling_reference/);
  assert.doesNotMatch(conflict, /只读后端/);
});

test("成员标题/字段做 HTML 转义，点击定位原卡而不隐藏原事件", () => {
  const context = build();
  context.ACTIVITY_PRESENTATION = {groups: [{...group, members: [{...group.members[0], title: '<img src=x onerror=alert(1)>'}]}], conflicts: []};
  const rendered = vm.runInContext("activityPresentationHtml()", context);
  assert.match(rendered, /&lt;img src=x onerror=alert\(1\)&gt;/);
  assert.doesNotMatch(rendered, /<img src=x/);
  // 原时间轴事件卡仍保留；摘要只是新增索引（函数不删除 EVENTS_DATA）。
  assert.match(html, /data-event-id=/);
});

test("成员跳转优先聚焦原卡，找不到才走既有 selectEvent", () => {
  const context = build();
  const card = {dataset: {eventId: "evt-a"}, getAttribute(name) { return name === "data-event-id" ? "evt-a" : ""; }, setAttribute() {}, focus(opts) { context.focused.push({id: "evt-a", opts}); }};
  context.document.querySelectorAll = () => [card];
  vm.runInContext("focusActivityMember('evt-a')", context);
  assert.equal(JSON.stringify(context.focused[0]), JSON.stringify({id: "evt-a", opts: {preventScroll: false}}));
  vm.runInContext("focusActivityMember('evt-missing')", context);
  assert.equal(context.focused.at(-1), "evt-missing");
});
