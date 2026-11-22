import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";
import test from "node:test";

const html = fs.readFileSync(new URL("../web/daily-timeline-live.html", import.meta.url), "utf8");

test("JS: 状态持久化键规范化并绑定日期，切日天然隔离不串日", () => {
  const script = html.match(/<script>([\s\S]*)<\/script>/)[1];
  const start = script.indexOf("function activityGroupKey");
  const end = script.indexOf("function buildClusterSentenceSummary");
  assert.ok(start > 0 && end > start);

  const context = {
    state: { date: "2026-10-25", activityGroupOpenStates: {} }
  };
  vm.createContext(context);
  vm.runInContext(script.slice(start, end) + "\nresult = { activityGroupKey, isActivityGroupOpen, toggleActivityGroupOpen };", context);

  const key1 = context.result.activityGroupKey("pres:inferred:meal:鸭腿");
  const key2 = context.result.activityGroupKey("inferred:meal:鸭腿");
  assert.equal(key1, "2026-10-25:inferred:meal:鸭腿");
  assert.equal(key2, "2026-10-25:inferred:meal:鸭腿", "兼容带与不带 pres: 前缀");

  // 在 10-25 展开
  context.result.toggleActivityGroupOpen("pres:inferred:meal:鸭腿", true);
  assert.equal(context.result.isActivityGroupOpen("inferred:meal:鸭腿"), true);

  // 切到 10-24
  context.state.date = "2026-10-24";
  assert.equal(context.result.isActivityGroupOpen("inferred:meal:鸭腿"), false, "切日后绝不串到新日期");
});

test("JS: 展开状态被重渲染/轮询持久化保留", () => {
  const script = html.match(/<script>([\s\S]*)<\/script>/)[1];
  const start = script.indexOf("function activityGroupKey");
  const end = script.indexOf("function renderTimeline()");
  assert.ok(start > 0 && end > start);

  const context = {
    CATEGORY_META: { meal: { label: "饮食", color: "#eab308", bg: "#fef9c3" } },
    ACTIVITY_PRESENTATION: { groups: [] },
    state: { date: "2026-10-25", density: "normal", selectedEventId: null, showQuotes: false, activityGroupOpenStates: {} },
    activityTypeLabel: (t) => t,
    precisionLabel: (p) => p,
    extractDataChips: () => [],
    duplicateBadgeHtml: () => "",
    activityFactsHtml: () => "",
    escapeHtml: (s) => s,
    escapeAttr: (s) => s,
  };
  vm.createContext(context);
  vm.runInContext(script.slice(start, end) + "\nresult = { toggleActivityGroupOpen, isActivityGroupOpen };", context);

  // 用户点击展开
  context.result.toggleActivityGroupOpen("group:order_1", true);
  assert.equal(context.result.isActivityGroupOpen("group:order_1"), true);
});

test("DOM: revealEventCardIfCollapsed 在卡片处于折叠 details 时自动展开并持久化", () => {
  const script = html.match(/<script>([\s\S]*)<\/script>/)[1];
  const start = script.indexOf("function revealEventCardIfCollapsed");
  const end = script.indexOf("function buildClusterSentenceSummary");
  assert.ok(start > 0 && end > start);

  const mockDetails = { open: false };
  const mockGroup = { getAttribute: (attr) => attr === "data-activity-group" ? "pres:inferred:meal:鸭腿" : null };
  const mockTarget = {
    closest: (sel) => sel === ".activity-group-details" ? mockDetails : (sel === ".activity-card-group" ? mockGroup : null)
  };

  const mockState = { date: "2026-10-25", activityGroupOpenStates: {} };
  const context = {
    state: mockState,
    activityGroupKey: (g) => `2026-10-25:${String(g).replace(/^pres:/, "")}`,
    toggleActivityGroupOpen: (groupId, force) => {
      mockState.activityGroupOpenStates[`2026-10-25:${String(groupId).replace(/^pres:/, "")}`] = force;
    }
  };
  vm.createContext(context);
  vm.runInContext(script.slice(start, end) + "\nresult = { revealEventCardIfCollapsed };", context);

  context.result.revealEventCardIfCollapsed(mockTarget);
  assert.equal(mockDetails.open, true, "details 必须被展开");
  assert.equal(mockState.activityGroupOpenStates["2026-10-25:inferred:meal:鸭腿"], true, "展开状态必须被持久化");
});
