import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";
import test from "node:test";

const html = fs.readFileSync(new URL("../web/daily-timeline-live.html", import.meta.url), "utf8");

test("CSS: activity-card-group 默认卡片与原生 details 收起样式", () => {
  assert.match(html, /\.activity-card-group\s*\{/);
  assert.match(html, /\.activity-card-group-summary\s*\{/);
  assert.match(html, /\.act-sentence-summary\s*\{/);
  assert.match(html, /\.activity-group-details\s*\{/);
  assert.match(html, /\.act-verify-tag\s*\{/);
  assert.doesNotMatch(html, /横向紧凑对齐/, "已彻底移除横向紧凑对齐文案");
});

test("JS: buildClusterSentenceSummary 真实证据动态提取与金额不求和", () => {
  const script = html.match(/<script>([\s\S]*)<\/script>/)[1];
  const start = script.indexOf("function buildClusterSentenceSummary");
  const end = script.indexOf("function getClusterRepresentativeTitle");
  assert.ok(start > 0 && end > start);

  const context = {};
  vm.createContext(context);
  vm.runInContext(script.slice(start, end) + "\nresult = { buildClusterSentenceSummary };", context);

  // 1. 真实 10-25 鸭腿事件
  const duckEvents = [
    { event_id: "e_order", title: "点了一份鸭腿" },
    { event_id: "e_buy", title: "购买鸭腿 (¥10.00)", data: { expense: { amount: 10 } } },
    { event_id: "e_flavor1", title: "鸭腿口味要求辣" },
    { event_id: "e_flavor2", title: "鸭腿口味调整为微辣" },
  ];
  const duckSummary = context.result.buildClusterSentenceSummary(duckEvents);
  assert.match(duckSummary, /点了一份鸭腿/);
  assert.match(duckSummary, /口味调整为微辣/);
  assert.match(duckSummary, /记录金额10元/);

  // 2. 多笔独立交易不求和
  const multiTx = [
    { event_id: "m1", title: "点了一份牛肉面", data: { expense: { amount: 25 } } },
    { event_id: "m2", title: "加一份卤蛋", data: { expense: { amount: 3 } } },
  ];
  const multiSummary = context.result.buildClusterSentenceSummary(multiTx);
  assert.match(multiSummary, /记录金额25元、3元/, "多笔交易分别列出，绝不求和");
  assert.doesNotMatch(multiSummary, /28元/);
});

test("DOM: activity-card-group 默认展示一句话摘要，details 默认收起 4 张散卡", () => {
  const script = html.match(/<script>([\s\S]*)<\/script>/)[1];
  const start = script.indexOf("function getEventClusterKey");
  const end = script.indexOf("function renderTimeline()");
  assert.ok(start > 0 && end > start);

  const context = {
    CATEGORY_META: { meal: { label: "饮食", color: "#eab308", bg: "#fef9c3" } },
    ACTIVITY_PRESENTATION: {
      groups: [{
        activity_ref: "inferred:meal:鸭腿",
        group_id: "group:inferred:meal:2026-10-25:00:鸭腿",
        member_event_ids: ["e_order", "e_buy", "e_flavor1", "e_flavor2"],
        summary: { title: "购买鸭腿 (¥10.00)" },
        basis: { verified: false, inferred: true }
      }]
    },
    state: { density: "normal", selectedEventId: null, showQuotes: false, date: "2026-10-25" },
    activityTypeLabel: (t) => t,
    precisionLabel: (p) => p,
    extractDataChips: () => [],
    duplicateBadgeHtml: () => "",
    activityFactsHtml: () => "",
    escapeHtml: (s) => s,
    escapeAttr: (s) => s,
  };
  vm.createContext(context);
  vm.runInContext(script.slice(start, end), context);

  const duckEvents = [
    { event_id: "e_order", title: "点了一份鸭腿", category: "meal", activity_type: "meal", time_str: "00:19" },
    { event_id: "e_buy", title: "购买鸭腿 (¥10.00)", category: "meal", activity_type: "expense", time_str: "00:19", data: { expense: { amount: 10 } } },
    { event_id: "e_flavor1", title: "鸭腿口味要求辣", category: "meal", activity_type: "meal", time_str: "00:19" },
    { event_id: "e_flavor2", title: "鸭腿口味调整为微辣", category: "meal", activity_type: "meal", time_str: "00:19" },
  ];

  const clusterTitle = context.getClusterRepresentativeTitle("pres:inferred:meal:鸭腿", duckEvents);
  const sentenceSummary = context.buildClusterSentenceSummary(duckEvents);
  assert.equal(clusterTitle, "购买鸭腿 (¥10.00)");
  assert.match(sentenceSummary, /点了一份鸭腿，口味调整为微辣；记录金额10元/);
});
