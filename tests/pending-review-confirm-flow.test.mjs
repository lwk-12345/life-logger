import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";
import test from "node:test";

const source = fs.readFileSync(new URL("../web/app.js", import.meta.url), "utf8");
const start = source.indexOf("// ==== 待人工审核（pending_sources）");
const end = source.indexOf("\nfunction renderJournalDay(day) {", start);
assert.notEqual(start, -1);
assert.notEqual(end, -1);
const block = source.slice(start, end);
const SOURCE_ID = "pending_event:jev:evt_ctx_confirm";
const ENCODED = encodeURIComponent(SOURCE_ID).replace(/'/g, "%27");
const JOB = "job-confirm-1";
const EVENTS_PATH = "files/hash/result/2026-10-25/events.json";

const item = {
  source_id: SOURCE_ID,
  kind: "pending_event",
  event_id: "evt_ctx_confirm",
  quote: "买了南方黑芝麻两桶，花费100元。",
  proposal_title: "购买南方黑芝麻两桶，花费100元",
  proposed_amount: 100,
  proposed_currency: "CNY",
  proposed_item: "南方黑芝麻",
  reason_label: "待确认消费",
  group: "needs_review",
  state: "open",
  date: "2026-10-25",
  time: "02:05",
  occurrence_time: null,
  artifact: {kind: "job", label: "任务 job-confirm-1", date: "2026-10-25", job_id: JOB, path: "jobs/job-confirm-1/files/hash/result/context-extraction.json"},
};

function buildContext({apiImpl} = {}) {
  const calls = [];
  const values = Object.create(null);
  const nodes = new Map();
  const node = (id) => {
    if (!nodes.has(id)) nodes.set(id, {
      id, value: values[id] ?? "", textContent: "", className: "", focus() {},
      scrollIntoView() {}, setAttribute() {}, removeAttribute() {},
    });
    return nodes.get(id);
  };
  const context = {
    console,
    URLSearchParams,
    selectedJournalDate: "2026-10-25",
    journalDays: [{date: "2026-10-25", entries: [{jobId: JOB, date: "2026-10-25", eventsPath: EVENTS_PATH}]}],
    pendingReviewState: null,
    item,
    api: async (path, options = {}) => {
      calls.push({path, method: options.method || "GET", body: options.body ? JSON.parse(options.body) : null});
      if (apiImpl) return apiImpl(path, options);
      if (path === "/api/jobs/job-confirm-1/artifacts") return {artifacts: [EVENTS_PATH]};
      if (path === "/api/jobs/job-confirm-1/manual-event") return {event: {event_id: "manual_evt_1"}};
      if (path.startsWith("/api/jobs/job-confirm-1/manual-events")) return {events: [{event_id: "manual_evt_1"}]};
      return {groups: [], totals: {}};
    },
    document: {
      getElementById: (id) => id === "pending-review-action-status" ? node(id) : node(id),
      querySelectorAll: () => [],
      addEventListener() {},
    },
    window: {addEventListener() {}},
    navigator: {clipboard: {writeText: async () => {}}},
    escapeHtml: (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c])),
    currentPendingReviewDate: () => "2026-10-25",
    refreshPendingReviewBlock: async () => {},
    setPendingReviewState: async (sourceId, kind, note) => {
      calls.push({path: "/api/pending-sources/status", method: "POST", body: {source_id: sourceId, status: kind, note}});
      return true;
    },
    calls,
    nodes,
    values,
  };
  vm.createContext(context);
  vm.runInContext(block, context);
  vm.runInContext(`
findPendingReviewItem = (sourceId) => {
  for (const payload of [pendingReviewState.payload, pendingReviewState.expanded]) {
    for (const group of payload?.groups || []) {
      for (const current of group.items || []) {
        if (String(current?.source_id || "") === String(sourceId)) return current;
      }
    }
  }
  return null;
};
`, context);
  return context;
}

test("待确认事件提供查看证据与人工确认表单，不再只有复制/假处理", () => {
  const context = buildContext();
  vm.runInContext("pendingReviewState.payload = {groups: [{key: 'needs_review', items: [item]}], totals: {}}", context);
  const html = vm.runInContext("pendingReviewItemHtml(item)", context);
  assert.match(html, /查看证据/);
  assert.match(html, /查看并确认/);
  assert.match(html, /标记已看过/);
  assert.doesNotMatch(html, />标记已处理</);
  assert.match(html, /关闭提醒/);
  assert.match(html, /不自动入账/);
});

test("确认事件只调用现有 manual-event，金额/类型/未知时间进入明确请求体", async () => {
  const context = buildContext();
  vm.runInContext("pendingReviewState.payload = {groups: [{key: 'needs_review', items: [item]}], totals: {}}", context);
  await vm.runInContext(`pendingReviewAction('confirm', '${ENCODED}')`, context);
  assert.equal(vm.runInContext("pendingReviewState.confirmId", context), SOURCE_ID);
  const html = vm.runInContext("pendingReviewItemHtml(item)", context);
  assert.match(html, /人工确认这一条/);
  assert.match(html, /manual-events\.json/);
  context.values[`pending-confirm-title-${ENCODED}`] = "购买南方黑芝麻两桶";
  context.values[`pending-confirm-category-${ENCODED}`] = "expense";
  context.values[`pending-confirm-amount-${ENCODED}`] = "100";
  context.values[`pending-confirm-time-${ENCODED}`] = "";
  context.values[`pending-confirm-activity-${ENCODED}`] = "purchase";
  await vm.runInContext(`pendingReviewConfirmSubmit(null, '${ENCODED}')`, context);
  const post = context.calls.find((call) => call.method === "POST");
  assert.ok(post);
  assert.equal(post.path, `/api/jobs/${JOB}/manual-event`);
  assert.equal(post.body.path, EVENTS_PATH);
  assert.deepEqual(post.body.categories, ["expense"]);
  assert.equal(post.body.data.expense.amount, 100);
  assert.equal(post.body.time_precision, "unknown");
  assert.equal(post.body.absolute_time, null);
  assert.match(post.body.note, /用户确认待确认来源/);
  assert.doesNotMatch(JSON.stringify(post.body), /event-correction|natural-correction|0\.00/);
});

test("金额留空不伪造 0，转待办只填已有入口不自动发请求", async () => {
  const context = buildContext();
  vm.runInContext("pendingReviewState.payload = {groups: [{key: 'needs_review', items: [item]}], totals: {}}", context);
  await vm.runInContext(`pendingReviewAction('confirm', '${ENCODED}')`, context);
  context.values[`pending-confirm-title-${ENCODED}`] = "购买南方黑芝麻两桶";
  context.values[`pending-confirm-category-${ENCODED}`] = "expense";
  context.values[`pending-confirm-amount-${ENCODED}`] = "";
  await vm.runInContext(`pendingReviewConfirmSubmit(null, '${ENCODED}')`, context);
  const post = context.calls.find((call) => call.path.endsWith("/manual-event"));
  assert.ok(post);
  assert.deepEqual(post.body.data, {});
  assert.equal(post.body.time_precision, "unknown");

  const instructionItem = {...item, kind: "pending_source", source_id: "src_instruction", event_id: null, proposal_title: null};
  context.instructionItem = instructionItem;
  vm.runInContext("pendingReviewState.payload = {groups: [{key: 'needs_review', items: [instructionItem]}], totals: {}}", context);
  const before = context.calls.length;
  await vm.runInContext("pendingReviewAction('instruction', 'src_instruction')", context);
  assert.equal(context.calls.length, before, "转入口不应自动发送或写账");
  assert.match(context.document.getElementById("natural-correction-input").value, /记一个待办/);
});

test("标记已看过只写审核台账并明确仅关闭提醒", async () => {
  const context = buildContext();
  vm.runInContext("pendingReviewState.payload = {groups: [{key: 'needs_review', items: [item]}], totals: {}}", context);
  await vm.runInContext(`pendingReviewAction('handled', '${ENCODED}')`, context);
  const post = context.calls.find((call) => call.path === "/api/pending-sources/status");
  assert.ok(post);
  assert.equal(post.body.status, "handled");
  assert.match(post.body.note, /仅关闭提醒/);
  assert.doesNotMatch(post.body.note, /确认/);
});


test("artifact path already containing date is not doubled in events.json target", () => {
  const context = buildContext();
  vm.runInContext("journalDays = []", context);
  const item = {
    ...context.item,
    artifact: {
      ...context.item.artifact,
      path: "jobs/job-confirm-1/files/hash/result/2026-10-25/context-extraction.json",
    },
  };
  context.item = item;
  const target = vm.runInContext("pendingManualTarget(item)", context);
  assert.equal(target.artifactPath, "files/hash/result/2026-10-25/events.json");
  assert.doesNotMatch(target.artifactPath, /2026-10-25\/2026-10-25/);
});

test("读回失败不宣称成功、不关闭提醒", async () => {
  const context = buildContext({
    apiImpl: async (path) => {
      if (path.endsWith("/artifacts")) return {artifacts: [EVENTS_PATH]};
      if (path.endsWith("/manual-event")) return {event: {event_id: "manual_readback_missing"}};
      if (path.includes("/manual-events?")) return {events: []};
      return {groups: [], totals: {}};
    },
  });
  vm.runInContext("pendingReviewState.payload = {groups: [{key: 'needs_review', items: [item]}], totals: {}}", context);
  await vm.runInContext(`pendingReviewAction('confirm', '${ENCODED}')`, context);
  context.values[`pending-confirm-title-${ENCODED}`] = "购买南方黑芝麻两桶";
  context.values[`pending-confirm-category-${ENCODED}`] = "expense";
  context.values[`pending-confirm-amount-${ENCODED}`] = "";
  await vm.runInContext(`pendingReviewConfirmSubmit(null, '${ENCODED}')`, context);
  const status = context.document.getElementById("pending-review-action-status").textContent;
  assert.match(status, /读回不到/);
  assert.notEqual(vm.runInContext("pendingReviewState.confirmId", context), null);
});

test("双击确认只发一次 manual-event 写入", async () => {
  let manualPosts = 0;
  let release;
  const gate = new Promise((resolve) => { release = resolve; });
  const context = buildContext({
    apiImpl: async (path) => {
      if (path.endsWith("/artifacts")) return {artifacts: [EVENTS_PATH]};
      if (path.endsWith("/manual-event")) {
        manualPosts += 1;
        await gate;
        return {event: {event_id: "manual_double_click"}};
      }
      if (path.includes("/manual-events?")) return {events: [{event_id: "manual_double_click"}]};
      return {groups: [], totals: {}};
    },
  });
  vm.runInContext("pendingReviewState.payload = {groups: [{key: 'needs_review', items: [item]}], totals: {}}", context);
  await vm.runInContext(`pendingReviewAction('confirm', '${ENCODED}')`, context);
  context.values[`pending-confirm-title-${ENCODED}`] = "购买南方黑芝麻两桶";
  context.values[`pending-confirm-category-${ENCODED}`] = "expense";
  context.values[`pending-confirm-amount-${ENCODED}`] = "";
  const first = vm.runInContext(`pendingReviewConfirmSubmit(null, '${ENCODED}')`, context);
  const second = vm.runInContext(`pendingReviewConfirmSubmit(null, '${ENCODED}')`, context);
  release();
  await Promise.all([first, second]);
  assert.equal(manualPosts, 1);
});
