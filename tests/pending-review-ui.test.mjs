import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";
import test from "node:test";

const appSource = fs.readFileSync(new URL("../web/app.js", import.meta.url), "utf8");
const blockStart = appSource.indexOf("// ==== 待人工审核（pending_sources）");
const blockEnd = appSource.indexOf("\nfunction renderJournalDay(day) {", blockStart);
assert.notEqual(blockStart, -1, "待人工审核区应该存在于 web/app.js");
assert.notEqual(blockEnd, -1, "待人工审核区应该以 renderJournalDay 作为结束边界");
const blockSource = appSource.slice(blockStart, blockEnd);

// 真实后端 /api/pending-sources 的形状（按 reason 分组；折叠组默认不带条目）
const SAMPLE_PAYLOAD = {
  ok: true,
  schema: "pending-sources-v1",
  scope: {date: "2026-10-12", include_collapsed: false, include_resolved: false, limit: 30},
  totals: {
    all: 3668,
    needs_review: 33,
    batch_failure: 3623,
    resolved: 0,
    undated: 693,
    scoped: 1136,
    shown: 30,
    artifacts: 19,
  },
  groups: [
    {
      key: "needs_review",
      label: "需要你确认",
      hint: "模型判「无事件」但文本像你的主动指令/口述记录。补录即可变成正式记录。",
      collapsed: false,
      count: 24,
      items: [
        {
          source_id: "src_c42af133d3ed5280bb63",
          segment_id: "watch_000003_000001",
          quote: "这个声纹归曼已经有个词系统去查一下有没有更好的解决方法了。",
          reason: "model_no_event_but_directive",
          reason_label: "指令句：模型判无事件，但文本像你的主动指令",
          reason_hint: "匹配到「检查/记录/汇报/整理…」这类指令词。",
          group: "needs_review",
          state: "open",
          date: "2026-10-12",
          time: "19:42",
          artifact: {kind: "watch-session", label: "Apple Watch 会话 66F8F1FF", date: "2026-10-12"},
        },
      ],
      items_truncated: 0,
    },
    {
      key: "batch_failure",
      label: "整批 API 失败兜底",
      hint: "整批调用失败时的全批倾倒（API 挂掉，不是模型判不出来）。默认折叠，不占版面。",
      collapsed: true,
      count: 15,
      items: [],
      items_truncated: 15,
    },
  ],
};

function buildContext({apiImpl} = {}) {
  const calls = [];
  const context = {
    escapeHtml: (value) => String(value),
    selectedJournalDate: "2026-10-12",
    document: {getElementById: () => null, addEventListener() {}, querySelectorAll: () => []},
    window: {addEventListener() {}},
    URLSearchParams,
    navigator: {clipboard: {writeText: async () => {}}},
    samplePayload: JSON.parse(JSON.stringify(SAMPLE_PAYLOAD)),
    calls,
    api: async (path, options = {}) => {
      const method = String(options.method || "GET").toUpperCase();
      calls.push({path, method, body: options.body ? JSON.parse(options.body) : null});
      if (apiImpl) return apiImpl(path, method);
      return JSON.parse(JSON.stringify(SAMPLE_PAYLOAD));
    },
  };
  vm.createContext(context);
  vm.runInContext(blockSource, context);
  // These helpers live just outside the pending-review slice. Evaluate them
  // in the same VM context so they can see the lexical pendingReviewState.
  vm.runInContext(`
currentPendingReviewDate = () => selectedJournalDate;
findPendingReviewItem = (sourceId) => {
  for (const payload of [pendingReviewState.payload, pendingReviewState.expanded]) {
    for (const group of payload?.groups || []) {
      for (const item of group.items || []) {
        if (String(item?.source_id || "") === String(sourceId)) return item;
      }
    }
  }
  return null;
};
setPendingReviewState = async (sourceId, status, note) => {
  await api("/api/pending-sources/status", {
    method: "POST",
    body: JSON.stringify({source_id: sourceId, status, note}),
  });
  await refreshPendingReviewBlock(currentPendingReviewDate(), {force: true});
  return {ok: true};
};
pendingReviewBulkFallback = async () => {
  const target = currentPendingReviewDate();
  const query = new URLSearchParams();
  if (target) query.set("date", target);
  query.set("limit", "200");
  const payload = await api(
    "/api/pending-sources?" + query.toString(),
    {},
    {silent: true, operation: "批量忽略兼容读取"},
  );
  const items = (payload?.groups || [])
    .filter((group) => group?.key === "needs_review")
    .flatMap((group) => group?.items || [])
    .filter((item) => item?.state === "open");
  for (const item of items) {
    await setPendingReviewState(item.source_id, "ignored", "批量清理测试口语");
  }
  return {succeeded: items.length, failed: 0, truncated: 0};
};
`, context);
  return context;
}

test("待人工审核区把真条目铺开、把整批 API 失败折叠成计数", () => {
  const context = buildContext();
  // 模拟尚未重启的旧后端仍返回过期提示；前端必须使用已校正的本地文案。
  context.samplePayload.groups.find((group) => group.key === "needs_review").hint =
    "复制出来重说一遍才会走正常的事件提取。";
  const html = vm.runInContext(
    "pendingReviewBodyHtml('2026-10-12', {payload: samplePayload, expandedOpen: false})",
    context,
  );

  // 真条目：原文、时间、原因、四个处理动作都要出现
  assert.match(html, /这个声纹归曼已经有个词系统去查一下/, "应该展示待确认原文");
  assert.match(html, /2026-10-12 19:42/, "应该展示条目真实时间");
  assert.match(html, /is-actionable/);
  assert.match(html, /复制原文/);
  assert.match(html, /标记已处理/);
  assert.match(html, /忽略/);
  assert.doesNotMatch(html, /补录为正式记录/, "补录链路要求已存在 events.json，不能给假按钮");
  assert.doesNotMatch(html, /复制出来重说一遍/, "旧后端的过期提示不能出现在声源组");
  assert.doesNotMatch(html, /匹配到「检查/, "每个卡片的 reason_hint 不再重复展示");
  assert.doesNotMatch(html, /目前没有从来源条目自动重新抽取或晋升为正式事件的通道/, "长免责声明不再重复占版面");
  assert.match(html, /忽略本日全部抽取待确认/);

  // batch_failure 默认折叠，但组和计数仍可见，且可以展开查看。
  assert.match(html, /整批 API 失败兜底/);
  assert.match(html, /展开查看/);
  assert.match(html, /pending-review-group is-dump/);
  assert.doesNotMatch(html, /pending-review-item is-dump/);

  // 摘要保留需确认与抽取异常，但不再显示 API 失败兜底计数
  assert.match(html, /本日开放需确认 24/);
  assert.match(html, /本日开放需确认 24 · 抽取异常 0/);
  assert.doesNotMatch(html, /1,136|当前范围 2026-10-12（本日） 命中/);
  assert.match(html, /忽略本日全部抽取待确认/);
  assert.match(html, /整批 API 失败兜底/);
  assert.doesNotMatch(html, /pending-review-item is-dump/, "折叠组不应该渲染条目卡");
});

test("请求按当前日记日过滤，默认不带整批失败条目", async () => {
  const context = buildContext();
  await vm.runInContext("refreshPendingReviewBlock('2026-10-12', {force: true})", context);
  assert.equal(context.calls.length, 1);
  assert.match(context.calls[0].path, /^\/api\/pending-sources\?/);
  assert.match(context.calls[0].path, /date=2026-10-12/);
  assert.doesNotMatch(context.calls[0].path, /include_collapsed/);
  assert.match(context.calls[0].path, /limit=30/);

  // 展开折叠组时才显式申请整批失败条目
  await vm.runInContext("pendingReviewState.expandedOpen = true", context);
  await vm.runInContext("refreshPendingReviewBlock('2026-10-12', {force: true})", context);
  assert.equal(context.calls.length, 2);
  assert.match(context.calls[1].path, /include_collapsed=1/);
  assert.match(context.calls[1].path, /limit=10/);
});

test("处理动作写人工台账，并重新读取服务端开放计数", async () => {
  const updated = JSON.parse(JSON.stringify(SAMPLE_PAYLOAD));
  updated.totals.open_actionable = 23;
  updated.totals.needs_review = 23;
  updated.totals.scoped_open_actionable = 23;
  updated.groups.find((group) => group.key === "needs_review").items = [];
  updated.groups.find((group) => group.key === "needs_review").count = 23;
  let getCount = 0;
  const context = buildContext({
    apiImpl: async (_path, method) => {
      if (method === "POST") return {ok: true};
      getCount += 1;
      return getCount === 1 ? JSON.parse(JSON.stringify(SAMPLE_PAYLOAD)) : updated;
    },
  });
  await vm.runInContext("refreshPendingReviewBlock('2026-10-12', {force: true})", context);
  await vm.runInContext(
    "pendingReviewAction('ignored', encodeURIComponent('src_c42af133d3ed5280bb63'))",
    context,
  );
  const post = context.calls.find((call) => call.method === "POST");
  assert.ok(post, "应该向服务端写台账");
  assert.ok(context.calls.filter((call) => call.method === "GET").length >= 2, "处理后必须重新读取开放计数");
  assert.equal(post.path, "/api/pending-sources/status");
  assert.deepEqual(post.body, {
    source_id: "src_c42af133d3ed5280bb63",
    status: "ignored",
    note: "用户忽略",
  });
  assert.equal(
    vm.runInContext(
      "pendingReviewState.payload.groups.find((group) => group.key === 'needs_review').items.length",
      context,
    ),
    0,
    "被处理的条目应该从列表里消失",
  );
  assert.equal(
    vm.runInContext(
      "pendingReviewState.payload.groups.find((group) => group.key === 'needs_review').count",
      context,
    ),
    23,
  );
});

test("批量接口 404 时用兼容模式逐条忽略，且不碰 extraction/batch 组", async () => {
  const fallbackPayload = {
    ok: true,
    scope: {date: "2026-10-12"},
    totals: {scoped_open_actionable: 2, scoped_open_extraction_gap: 1},
    groups: [
      {
        key: "needs_review",
        count: 3,
        items_truncated: 0,
        items: [
          {source_id: "src_open_1", state: "open"},
          {source_id: "src_open_2", state: "open"},
          {source_id: "src_resolved", state: "resolved"},
        ],
      },
      {key: "extraction_gap", count: 1, items: [{source_id: "src_gap", state: "open"}]},
      {key: "batch_failure", count: 9, items: [{source_id: "src_batch", state: "open"}]},
    ],
  };
  const context = buildContext({
    apiImpl: async (path, method) => {
      if (method === "POST" && path === "/api/pending-sources/status-bulk") {
        const error = new Error("请求失败：404");
        error.status = 404;
        throw error;
      }
      if (method === "GET") return JSON.parse(JSON.stringify(fallbackPayload));
      if (method === "POST" && path === "/api/pending-sources/status") return {ok: true};
      throw new Error(`unexpected ${method} ${path}`);
    },
  });
  await vm.runInContext("refreshPendingReviewBlock('2026-10-12', {force: true})", context);
  await vm.runInContext("pendingReviewBulkIgnore()", context);

  const primary = context.calls.filter((call) => call.path === "/api/pending-sources/status-bulk");
  assert.equal(primary.length, 1, "still tries the primary bulk endpoint first");
  const fallbackRead = context.calls.find((call) => (
    call.method === "GET"
    && call.path.includes("/api/pending-sources?")
    && call.path.includes("limit=200")
  ));
  assert.ok(fallbackRead, "404 must trigger a 200-item scoped fallback read");
  assert.match(fallbackRead.path, /date=2026-10-12/);
  assert.doesNotMatch(fallbackRead.path, /include_collapsed/);

  const statusPosts = context.calls.filter((call) => (
    call.method === "POST" && call.path === "/api/pending-sources/status"
  ));
  assert.deepEqual(
    statusPosts.map((call) => call.body.source_id).sort(),
    ["src_open_1", "src_open_2"],
    "only open needs_review items are ignored",
  );
  for (const call of statusPosts) {
    assert.equal(call.body.status, "ignored");
    assert.equal(call.body.note, "批量清理测试口语");
  }
  assert.ok(!statusPosts.some((call) => ["src_gap", "src_batch", "src_resolved"].includes(call.body.source_id)));
});

test("接口还没上线时给出「重启本地服务」的诚实提示，而不是假装没有内容", async () => {
  const context = buildContext({
    apiImpl: () => {
      const error = new Error("请求失败：404");
      error.status = 404;
      throw error;
    },
  });
  await vm.runInContext("refreshPendingReviewBlock('2026-10-12', {force: true})", context);
  const html = vm.runInContext(
    "pendingReviewBodyHtml('2026-10-12', pendingReviewState)",
    context,
  );
  assert.match(html, /重启 Life Logger 本地服务/);
  assert.match(
    vm.runInContext("String(pendingReviewState.error)", context),
    /重启 Life Logger 本地服务/,
  );
  assert.equal(
    vm.runInContext("pendingReviewState.payload", context) === null,
    true,
    "读不到时不应该塞一个假 payload",
  );
});


const SPLIT_TOTALS = {
  ...SAMPLE_PAYLOAD.totals,
  resolved: 6550,
  resolved_ignored_by_user: 20,
  resolved_handled_by_user: 1,
  resolved_failed_archived: 6529,
  batch_failure_open: 0,
  batch_failure_archived: 6529,
};

test("真实拆分 fixture：人工忽略20 / 人工处理1 / 批次归档6529 分别显示", () => {
  const context = buildContext();
  context.samplePayload.totals = JSON.parse(JSON.stringify(SPLIT_TOTALS));
  const batch = context.samplePayload.groups.find((group) => group.key === "batch_failure");
  batch.count = 6529;
  batch.items_truncated = 6529;
  const html = vm.runInContext(
    "pendingReviewBodyHtml('2026-10-12', {payload: samplePayload, expandedOpen: false})",
    context,
  );

  assert.match(html, /人工忽略 20/);
  assert.match(html, /人工处理 1/);
  assert.match(html, /批次失败归档 6,529/);
  assert.doesNotMatch(html, /已处理 6,550/);
  assert.doesNotMatch(html, /旧口径/);
});

test("batch_failure 默认折叠但可展开查看，归档条目标记为系统归档", () => {
  const context = buildContext();
  const payload = context.samplePayload;
  payload.totals = JSON.parse(JSON.stringify(SPLIT_TOTALS));
  const batch = payload.groups.find((group) => group.key === "batch_failure");
  batch.count = 6529;
  batch.items = [];
  batch.items_truncated = 6529;

  const collapsed = vm.runInContext(
    "pendingReviewBodyHtml('2026-10-12', {payload: samplePayload, expandedOpen: false})",
    context,
  );
  assert.match(collapsed, /整批 API 失败兜底/);
  assert.match(collapsed, /展开查看/);
  assert.doesNotMatch(collapsed, /历史失败原文/);

  const expanded = JSON.parse(JSON.stringify(payload));
  const expandedBatch = expanded.groups.find((group) => group.key === "batch_failure");
  expandedBatch.items = [{
    source_id: "src_archived",
    quote: "历史失败原文",
    group: "batch_failure",
    state: "failed_archived",
    date: "2026-10-12",
    artifact: {label: "历史产物"},
  }];
  expandedBatch.items_truncated = 0;
  const html = vm.runInContext(
    "pendingReviewBodyHtml('2026-10-12', {payload: samplePayload, expandedOpen: true, expanded})",
    Object.assign(context, {expanded}),
  );
  assert.match(html, /历史失败原文/);
  assert.match(html, /系统归档（非人工处理）/);
  assert.match(html, /收起/);
});

test("旧后端缺拆分字段：显示旧口径并明确待升级，不伪造 20/1/6529", () => {
  const context = buildContext();
  context.samplePayload.totals = {
    ...context.samplePayload.totals,
    resolved: 6550,
  };
  delete context.samplePayload.totals.resolved_ignored_by_user;
  delete context.samplePayload.totals.resolved_handled_by_user;
  delete context.samplePayload.totals.resolved_failed_archived;
  delete context.samplePayload.totals.batch_failure_open;
  delete context.samplePayload.totals.batch_failure_archived;
  const html = vm.runInContext(
    "pendingReviewBodyHtml('2026-10-12', {payload: samplePayload, expandedOpen: false})",
    context,
  );

  assert.match(html, /旧口径：resolved 6,550/);
  assert.match(html, /无法区分人工\/批次归档/);
  assert.match(html, /待后端升级/);
  assert.doesNotMatch(html, /人工忽略 20/);
  assert.doesNotMatch(html, /人工处理 1/);
  assert.doesNotMatch(html, /批次失败归档 6,529/);
});
