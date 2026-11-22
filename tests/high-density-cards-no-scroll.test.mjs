import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";
import test from "node:test";

const html = fs.readFileSync(new URL("../web/daily-timeline-live.html", import.meta.url), "utf8");

test("CSS: .hour-band 移除硬高度限制，保留 min-height 刻度基准", () => {
  const match = html.match(/\.hour-band\s*\{([^}]+)\}/);
  assert.ok(match, "找到 .hour-band 规则");
  const body = match[1];
  assert.match(body, /min-height:\s*var\(--hour-h,\s*72px\)/, "保留基准 min-height");
  assert.doesNotMatch(body, /^\s*height:\s*var\(--hour-h,\s*72px\);/m, "不得强制固定 height");
});

test("CSS: .col-events-track 移除内部强制纵向滚动条", () => {
  const match = html.match(/\.col-events-track\s*\{([^}]+)\}/);
  assert.ok(match, "找到 .col-events-track 规则");
  const body = match[1];
  assert.doesNotMatch(body, /overflow-y:\s*auto/, "移除了 overflow-y: auto");
  assert.doesNotMatch(body, /scrollbar-width/, "移除了小框滚动条设置");
});

test("CSS: 移动端媒体查询不引入内部滚动", () => {
  const mediaIdx = html.indexOf("@media (max-width: 620px)");
  assert.ok(mediaIdx > 0, "存在移动端媒体查询");
  const mediaBlock = html.slice(mediaIdx, html.indexOf("</style>", mediaIdx));
  const colMatch = mediaBlock.match(/\.col-events-track\s*\{([^}]+)\}/);
  if (colMatch) {
    assert.doesNotMatch(colMatch[1], /overflow-y:\s*auto/);
    assert.doesNotMatch(colMatch[1], /overflow:\s*hidden/);
  }
});

test("CSS: 包含 .activity-card-group 横向紧凑分组样式", () => {
  assert.match(html, /\.activity-card-group\s*\{/);
  assert.match(html, /\.activity-card-group-list\s*\{/);
});

test("JS: 同活动/点单记录识别并聚合成横向紧凑卡片组，原卡结构完整", () => {
  const script = html.match(/<script>([\s\S]*)<\/script>/)[1];
  const start = script.indexOf("function getEventClusterKey");
  const end = script.indexOf("function renderTimeline()");
  assert.ok(start > 0 && end > start);

  const context = {
    CATEGORY_META: { meal: { label: "饮食", color: "#eab308", bg: "#fef9c3" }, routine: { label: "作息", color: "#22c55e", bg: "#f0fdf4" } },
    ACTIVITY_PRESENTATION: {
      groups: [{
        activity_ref: "act:order_duck",
        member_event_ids: ["ev_meal", "ev_flavor", "ev_pay"]
      }]
    },
    state: { density: "normal", selectedEventId: null, showQuotes: false, date: "2026-10-25" },
    activityTypeLabel: (t) => t,
    precisionLabel: (p) => p,
    extractDataChips: () => [],
    duplicateBadgeHtml: () => "",
    activityFactsHtml: () => "",
    // v23 契约更新：union 卡片渲染链新增 activityInlineHtml 依赖（内联摘要），
    // 仅补齐该桩函数，不改动任何聚类/分组断言。
    activityInlineHtml: () => "",
    escapeHtml: (s) => s,
    escapeAttr: (s) => s,
  };
  vm.createContext(context);
  vm.runInContext(script.slice(start, end), context);

  const ev1 = { event_id: "ev_meal", title: "鸭腿饭", category: "meal", activity_type: "meal", time_str: "12:00" };
  const ev2 = { event_id: "ev_flavor", title: "微辣口味", category: "meal", activity_type: "meal", time_str: "12:00" };
  const ev3 = { event_id: "ev_pay", title: "支付播报 28元", category: "meal", activity_type: "pos_payment_broadcast", time_str: "12:01" };
  const evOther = { event_id: "ev_other", title: "其他无关联事件", category: "routine", activity_type: "routine", time_str: "12:30" };

  assert.equal(context.getEventClusterKey(ev1), "pres:act:order_duck");
  assert.equal(context.getEventClusterKey(ev2), "pres:act:order_duck");
  assert.equal(context.getEventClusterKey(ev3), "pres:act:order_duck");
  assert.equal(context.getEventClusterKey(evOther), null);

  const cardHtml = context.renderSingleEventCard(ev1);
  assert.match(cardHtml, /data-event-id="ev_meal"/);
  assert.match(cardHtml, /鸭腿饭/);
});


test("JS: getClusterRepresentativeTitle 优先消费后端 summary.title，兜底按金额/核心动作加权", () => {
  const script = html.match(/<script>([\s\S]*)<\/script>/)[1];
  const start = script.indexOf("function getEventClusterKey");
  const end = script.indexOf("function renderTimeline()");
  assert.ok(start > 0 && end > start);

  const context = {
    ACTIVITY_PRESENTATION: {
      groups: [{
        activity_ref: "inferred:meal:鸭腿",
        member_event_ids: ["e_order", "e_buy", "e_flavor1", "e_flavor2"],
        summary: { title: "购买鸭腿 (¥10.00)" }
      }]
    },
    state: {},
    escapeHtml: (s) => s,
    escapeAttr: (s) => s,
  };
  vm.createContext(context);
  vm.runInContext(script.slice(start, end), context);

  const cluster = [
    { event_id: "e_order", title: "点了一份鸭腿" },
    { event_id: "e_buy", title: "购买鸭腿 (¥10.00)", data: { expense: { amount: 10 } } },
    { event_id: "e_flavor1", title: "鸭腿口味要求辣" },
    { event_id: "e_flavor2", title: "鸭腿口味调整为微辣" },
  ];

  // 1. 优先命中后端 summary.title
  const titleWithSummary = context.getClusterRepresentativeTitle("pres:inferred:meal:鸭腿", cluster);
  assert.equal(titleWithSummary, "购买鸭腿 (¥10.00)");

  // 2. 无后端 groups 时的备用加权兜底
  context.ACTIVITY_PRESENTATION.groups = [];
  const titleFallback = context.getClusterRepresentativeTitle("some_k", cluster);
  assert.equal(titleFallback, "购买鸭腿 (¥10.00)", "有金额卡片优先当选代表标题");

  // 3. 无金额卡片时，第二优先选行为动作卡
  const clusterNoAmount = [
    { event_id: "e_order", title: "点了一份鸭腿" },
    { event_id: "e_flavor1", title: "鸭腿口味要求辣" },
    { event_id: "e_flavor2", title: "鸭腿口味调整为微辣" },
  ];
  const titleAction = context.getClusterRepresentativeTitle("some_k", clusterNoAmount);
  assert.equal(titleAction, "点了一份鸭腿", "核心动作卡优先于纯口味修饰卡");
});
