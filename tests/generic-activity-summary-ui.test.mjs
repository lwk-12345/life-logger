import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";
import test from "node:test";

const html = fs.readFileSync(new URL("../web/daily-timeline-live.html", import.meta.url), "utf8");
const script = html.match(/<script>([\s\S]*)<\/script>/)[1];

function loadTimelineContext(overrides = {}) {
  const context = {
    state: { date: "2026-10-25", density: "normal", activityGroupOpenStates: {}, selectedEventId: null },
    CATEGORY_META: {
      exercise: { label: "运动", color: "#e11d48", bg: "#fff0f3" },
      game: { label: "游戏", color: "#8b5cf6", bg: "#f5f3ff" },
      meal: { label: "饮食", color: "#eab308", bg: "#fef9c3" },
      work_communication: { label: "工作", color: "#4f46e5", bg: "#f1f0ff" }
    },
    ACTIVITY_PRESENTATION: { groups: [] },
    PURCHASE_SESSION_BY_EVENT: new Map(),
    PURCHASE_SESSION_BY_ID: new Map(),
    activityTypeLabel: (t) => t,
    precisionLabel: (p) => p,
    extractDataChips: () => [],
    duplicateBadgeHtml: () => "",
    activityFactsHtml: () => "",
    escapeHtml: (s) => s,
    escapeAttr: (s) => s,
    ...overrides
  };
  vm.createContext(context);
  const start = script.indexOf("function findMatchedActivityGroup");
  const end = script.indexOf("function renderTimeline()");
  assert.ok(start > 0 && end > start);
  vm.runInContext(script.slice(start, end) + `
    result = {
      findMatchedActivityGroup,
      activityGroupIcon,
      buildResultSummarySentence,
      buildClusterSentenceSummary,
      setGroups: (g) => { ACTIVITY_PRESENTATION.groups = g; }
    };
  `, context);
  return context;
}

test("健身通用活动：消费 result_summary 动作/地点/时长，图标为 🏋️ 绝非 🍱", () => {
  const ctx = loadTimelineContext();
  const fitnessGroup = {
    activity_ref: "activity:act_gym",
    member_event_ids: ["e1", "e2"],
    result_summary: {
      schema: "activity-result-summary-v1",
      title: "深蹲",
      actions: ["深蹲", "卧推"],
      status: "completed",
      locations: ["某健身房"],
      durations: [{ minutes: 70, event_id: "e1" }],
      state_breakdown: { completed: 2 },
      amounts_summed: false,
      durations_summed: false
    }
  };
  ctx.result.setGroups([fitnessGroup]);

  const cluster = [
    { event_id: "e1", title: "深蹲", category: "exercise" },
    { event_id: "e2", title: "卧推", category: "exercise" },
  ];

  const summary = ctx.result.buildClusterSentenceSummary(cluster, "pres:activity:act_gym");
  assert.equal(summary, "深蹲、卧推，全部已完成；地点：某健身房；时长：70分钟");

  const icon = ctx.result.activityGroupIcon(fitnessGroup, cluster, "pres:activity:act_gym");
  // 2026-11 起图标改成「种类标签」（activityKindChip），不再是单个 emoji；语义要求不变：
  // 必须落在「运动」档，绝不能落成餐饮/餐盒。
  assert.match(icon, /kind-chip kind-fitness/, "健身活动必须用运动种类标签");
  assert.match(icon, /运动/);
  assert.doesNotMatch(icon, /🍱|kind-meal|餐饮/, "健身活动绝不能落成餐盒/餐饮");
});

test("游戏活动：消费 items 局数，图标为 🎮 绝无餐饮文案或餐盒图标", () => {
  const ctx = loadTimelineContext();
  const gameGroup = {
    activity_ref: "activity:act_elden",
    member_event_ids: ["g1", "g2"],
    result_summary: {
      schema: "activity-result-summary-v1",
      title: "艾尔登法环",
      items: ["艾尔登法环", "3"],
      status: "completed",
      state_breakdown: { completed: 2 }
    }
  };
  ctx.result.setGroups([gameGroup]);

  const cluster = [
    { event_id: "g1", title: "艾尔登法环", category: "game" },
    { event_id: "g2", title: "又玩了一局", category: "game" },
  ];

  const summary = ctx.result.buildClusterSentenceSummary(cluster, "pres:activity:act_elden");
  assert.equal(summary, "艾尔登法环（3局），全部已完成");
  assert.doesNotMatch(summary, /点单|消费|鸭腿|记录金额/, "游戏绝不出现餐饮文案");

  const icon = ctx.result.activityGroupIcon(gameGroup, cluster, "pres:activity:act_elden");
  assert.match(icon, /kind-chip kind-game/, "游戏活动必须用游戏种类标签");
  assert.match(icon, /游戏/);
  assert.doesNotMatch(icon, /🍱|kind-meal|餐饮/, "游戏活动绝不能落成餐盒/餐饮");
});

test("混合/冲突状态：完成与计划分别明确列出，不把未完成冒充全部完成", () => {
  const ctx = loadTimelineContext();
  const mixedGroup = {
    activity_ref: "activity:act_mixed",
    member_event_ids: ["m1", "m2"],
    result_summary: {
      schema: "activity-result-summary-v1",
      title: "训练",
      actions: ["计划跑步", "跑了5公里"],
      status: "conflict",
      status_note: "计划与实际并存，未统一成完成",
      state_breakdown: { planned: 1, completed: 1 }
    }
  };
  ctx.result.setGroups([mixedGroup]);

  const cluster = [
    { event_id: "m1", title: "计划跑步", category: "exercise" },
    { event_id: "m2", title: "跑了5公里", category: "exercise" },
  ];

  const summary = ctx.result.buildClusterSentenceSummary(cluster, "pres:activity:act_mixed");
  assert.match(summary, /完成 1 项、计划 1 项/, "子项目完成/计划各自清楚");
  assert.match(summary, /计划与实际并存，未统一成完成/);
  assert.doesNotMatch(summary, /全部已完成/, "不得冒充全部完成");
});

test("未知不猜：无地点和时长时不编造字段", () => {
  const ctx = loadTimelineContext();
  const simpleGroup = {
    activity_ref: "activity:act_simple",
    member_event_ids: ["s1", "s2"],
    result_summary: {
      schema: "activity-result-summary-v1",
      title: "自习讨论",
      actions: ["自习", "讨论"],
      status: "in_progress",
      locations: [],
      durations: [],
      state_breakdown: { in_progress: 2 }
    }
  };
  ctx.result.setGroups([simpleGroup]);

  const cluster = [
    { event_id: "s1", title: "自习", category: "work_communication" },
    { event_id: "s2", title: "讨论", category: "work_communication" },
  ];

  const summary = ctx.result.buildClusterSentenceSummary(cluster, "pres:activity:act_simple");
  assert.equal(summary, "自习、讨论，进行中");
  assert.doesNotMatch(summary, /地点|时长/, "未知地点时长绝不编造");
});

test("原购买跨商品摘要与图标不回归", () => {
  const ctx = loadTimelineContext();
  const mockSession = {
    session_id: "ps_duck",
    member_ids: ["e1", "e2"],
    items: [
      { event_id: "e1", item: "鸭腿", amount: 10.0 },
      { event_id: "e2", item: "烤肠", amount: 3.0 }
    ],
    addon_links: [{ from_event_id: "e1", to_event_id: "e2" }],
    recording_id: "rec_1"
  };
  ctx.PURCHASE_SESSION_BY_ID.set("ps_duck", mockSession);

  const cluster = [
    { event_id: "e1", title: "购买鸭腿" },
    { event_id: "e2", title: "购买烤肠" }
  ];

  const summary = ctx.result.buildClusterSentenceSummary(cluster, "ps:ps_duck");
  assert.equal(summary, "鸭腿10元，随后追加烤肠3元", "原购买摘要保持完好");

  const icon = ctx.result.activityGroupIcon(null, cluster, "ps:ps_duck");
  // 购买会话（ps:）同样收成种类标签，不再是 🛍️ 单 emoji。
  assert.match(icon, /kind-chip kind-purchase/, "购买会话必须用购买种类标签");
  assert.match(icon, /购买/);
});
