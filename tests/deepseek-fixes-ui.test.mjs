import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";
import test from "node:test";
import {journalLogic} from "./helpers/page-loader.mjs";

const appSource = fs.readFileSync(new URL("../web/app.js", import.meta.url), "utf8");
const viewerSource = fs.readFileSync(new URL("../web/viewer.js", import.meta.url), "utf8");
const indexSource = fs.readFileSync(new URL("../web/index.html", import.meta.url), "utf8");

function slice(source, startNeedle, endNeedle) {
  const start = source.indexOf(startNeedle);
  const end = source.indexOf(endNeedle, start);
  assert.notEqual(start, -1, `missing start: ${startNeedle}`);
  assert.notEqual(end, -1, `missing end: ${endNeedle}`);
  return source.slice(start, end);
}


function extractNamedFunction(source, name) {
  const pattern = new RegExp(`^(?:async\\s+)?function\\s+${name.replace(/[$]/g, "\\$&")}\\(`, "m");
  const match = pattern.exec(source);
  if (!match) throw new Error(`missing function ${name}`);
  const start = match.index;
  let i = source.indexOf("(", match.index);
  let paren = 0;
  for (; i < source.length; i += 1) {
    if (source[i] === "(") paren += 1;
    else if (source[i] === ")" && --paren === 0) { i = source.indexOf("{", i); break; }
  }
  let depth = 0;
  for (; i < source.length; i += 1) {
    if (source[i] === "{") depth += 1;
    else if (source[i] === "}" && --depth === 0) return source.slice(start, i + 1);
  }
  throw new Error(`unclosed function ${name}`);
}


// 【2026-10-25 修·测试装置】canonical 相关函数的稳定提取器。
// 旧写法 `canonicalSourceText()`
// 的两个锚点在 app.js 里是**相邻两行**，slice 结果只剩一行，于是 hydrate/ensure 全是 undefined。
// 这里改成按函数名逐段提取（extractNamedFunction 已按花括号配平），不再依赖注释位置。

// 【2026-10-25 修·测试装置】renderJournalNavigation() 现在会碰 DOM（querySelector / createElement）。
// 给 vm 提供最小的 DOM 桩：只保证函数能跑完并把 nav HTML 写进 root.innerHTML，
// 不改变被测断言（仍然断言 innerHTML 里的真实计数）。
function stubJournalDom(root) {
  root.querySelector = () => null;
  root.querySelectorAll = () => [];
  root.insertAdjacentElement = () => {};
  const element = () => ({
    style: {}, hidden: false, className: "", id: "",
    setAttribute() {}, appendChild() {}, insertAdjacentElement() {}, textContent: "",
    children: [], querySelector: () => null, querySelectorAll: () => [],
  });
  return {
    document: {
      createElement: element, getElementById: () => null,
      querySelector: () => null, querySelectorAll: () => [], addEventListener() {},
      body: element(),
    },
    renderDailyHealthBar: () => {},
    refreshDailyHealth: async () => {},
  };
}

const CANONICAL_FUNCTIONS = [
  "canonicalArtifactPath",
  "canonicalCategoryCounts",
  "canonicalContractError",
  "canonicalCorrectionTarget",
  "hydrateJournalDayFromCanonical",
  "isPendingApiError",
  "pendingReadInfo",
  "pendingRetryDelay",
  "scheduleJournalPendingRetry",
  "shanghaiDate",
  "invalidateJournalCanonicalCache",
  "ensureJournalDaySynthesis",
];

// 这些顶层常量被 canonical 函数引用，必须一并带进 vm（否则 ReferenceError）。
const CANONICAL_CONSTANTS = [
  'const CANONICAL_EVENT_VIEW_SCHEMA = "event-view-v1";',
  'const CANONICAL_EXPENSE_POLICY_VERSIONS = new Set(["expense-policy-v2"]);',
  "const JOURNAL_CACHE_LIMIT = 2000;",
  // [2026-11-04] invalidateJournalCanonicalCache() 还会 bump/清这两个时间轴全局
  // （app.js:7541-7542 的顶层声明）。它们在 slice 之外，缺了就 ReferenceError。
  "let journalActivityTimelineGeneration = 0;",
  "const journalActivityTimelineCache = new Map();",
];

function canonicalSourceText() {
  return [
    "let journalCanonicalCache = new Map();",
    "let journalCanonicalGeneration = 0;",
    ...CANONICAL_CONSTANTS,
    ...CANONICAL_FUNCTIONS.map((name) => extractNamedFunction(appSource, name)),
  ].join("\n\n");
}

function buildPendingContext({apiImpl, mountedDate = "2026-10-12"} = {}) {
  const calls = [];
  const states = {};
  const block = {dataset: {date: mountedDate}, scrollIntoView() {}, setAttribute() {}, focus() {}};
  const body = {innerHTML: ""};
  const status = {textContent: "", className: ""};
  const badge = {textContent: "", title: "", classList: {remove() {}, toggle() {}}};
  const element = (id) => {
    if (id === "pending-review-block") return block;
    if (id === "pending-review-body") return body;
    if (id === "pending-review-action-status") return status;
    if (id === "source-inbox-nav-count") return badge;
    if (id === "journal-day-view" || id === "journal-dashboard") return states.dashboard;
    return null;
  };
  // renderJournalDay 会查 iframe/详情节点；给最小 DOM 桩，innerHTML 仍是断言对象
  states.dashboard = {innerHTML: "", querySelector: () => null, querySelectorAll: () => [],
                      insertAdjacentElement: () => {}, appendChild: () => {}};
  const pendingSource = slice(
    appSource,
    "// ==== 待人工审核（pending_sources）",
    "// 【2026-10-14】瀑布图 iframe 高度自适应",
  );
  const context = {
    console,
    Date,
    Map,
    Set,
    JSON,
    URLSearchParams,
    // [2026-11-08] app.js 的 renderJournalDay 末尾会调 renderJournalNavigation()；
    // 沙箱里没有它 → 异步 throw 变成 unhandledRejection，把整个文件判失败。
    // 这里给个空实现（本文件断言的是 innerHTML，不依赖导航渲染）。
    renderJournalNavigation: () => {},
    selectedJournalDate: mountedDate,
    pendingReviewBlock: block,
    document: {
      getElementById: element,
      querySelectorAll: () => [],
    },
    window: {addEventListener() {}, removeEventListener() {}},
    navigator: {clipboard: {writeText: async () => {}}},
    $: (id) => element(id),
    escapeHtml: (value) => String(value),
    formatCount: (value) => Number(value || 0).toLocaleString("zh-CN"),
    CATEGORY_LABELS: {todo: "待办", food: "饮食"},
    friendlyDate: (date) => date,
    formatMonthDay: (date) => date,
    formatWeekday: () => "",
    shanghaiDate: (offset = 0) => (offset === 0 ? mountedDate : mountedDate),
    renderSaidMetric: () => "",
    eventTotalLabel: () => "",
    timelineFactsFor: () => null,
    // 【2026-10-25 修·测试装置】不能把当前日期冻死，否则“切日后旧响应”永远测不出来。
    // 产品实现就是读 #pending-review-block[data-date]，这里照同样的语义从 DOM 取。
    currentPendingReviewDate: () => (block && block.dataset ? block.dataset.date : "") || mountedDate,
    setPendingReviewState: async () => true,
    pendingReviewState: {payload: null, loadedAt: 0, loading: null},
    applyEventCorrections: (e) => e,
    canonicalCategoryCounts: () => ({}),
    canonicalCorrectionTarget: () => ({jobId: null, artifactPath: null, available: false}),
    sharedTimelineRead: async (_p, loader) => loader(),
    LifeTimelineData: undefined,
    viewerLink: (job, path) => `/viewer.html?job=${job}&path=${path}`,
    journalScenesCache: new Map(),
    // [2026-11-04] 页面按 <script src="/journal_logic.js"> 提供这个全局（app.js:145
    // `const JOURNAL_LOGIC = window.LifeLoggerJournalLogic || {}`）。沙箱里缺了它，
    // primaryCountMetricHtml 会 ReferenceError；这里接真实实现，不做桩。
    JOURNAL_LOGIC: journalLogic(),
    journalDays: [],
    journalDateGeneration: 0,
    loadJournalDayScenes: () => {},
    ensureJournalDaySynthesis: () => {},
    renderReviewDashboard: () => 0,
    renderDayTimeline: () => "TIMELINE",
    renderDailyWorkspace: () => "WORKSPACE",
    scheduleTimelineCollisionResolution: () => {},
    fitDayTimelineFrame: () => {},
    api: async (path, options = {}) => {
      const method = String(options.method || "GET").toUpperCase();
      calls.push({path, method, body: options.body ? JSON.parse(options.body) : null});
      if (apiImpl) return apiImpl(path, method, calls.length);
      return null;
    },
    calls,
  };
  vm.createContext(context);
  vm.runInContext(pendingSource, context);
  // 【2026-10-25 修·测试装置】renderJournalDay 现在依赖这些产品函数（原先都在 slice 之外）：
  // 用 extractNamedFunction 逐段补进来，保持真实实现而不是桩。
  vm.runInContext([
    "dayPrimaryCount", "timelineSectionMarkup", "daySwitchBannerMarkup",
    "journalLoadState", "synthesisStaleNotice", "bindTimelineFrameState",
    "loadJournalFunnel", "renderMultiDomainLedgersGrid", "renderPinnedMemosBlock",
    "renderBroadcastPending", "renderSaidMetric", "primaryCountMetricHtml",
  ].map((name) => extractNamedFunction(appSource, name)).join("\n\n"), context);
  context.__block = block;
  context.__body = body;
  context.__status = status;
  context.__badge = badge;
  return context;
}

function samplePayload() {
  return {
    ok: true,
    schema: "pending-sources-v1",
    scope: {date: "2026-10-12", include_collapsed: false, include_resolved: false, limit: 30},
    totals: {
      all: 129,
      needs_review: 129,
      open_actionable: 128,
      resolved: 1,
      open_extraction_gap: 36,
      open_batch_failure: 15,
      scoped: 127,
      scoped_open_actionable: 7,
      scoped_open_extraction_gap: 5,
    },
    groups: [
      {key: "needs_review", label: "需要你确认", count: 127, items: [{source_id: "src_1", quote: "记一下", date: "2026-10-12", time: "19:42", group: "needs_review", state: "open"}], items_truncated: 0},
      {key: "extraction_gap", label: "抽取异常", count: 5, items: [], items_truncated: 5},
      {key: "batch_failure", label: "整批 API 失败兜底", collapsed: true, count: 15, items: [], items_truncated: 15},
    ],
  };
}

test("review inbox uses open_actionable, keeps resolved/global and hides batch failures", () => {
  const context = buildPendingContext();
  context.samplePayload = samplePayload();
  const globalHtml = vm.runInContext(
    "pendingReviewBodyHtml('2026-10-12', {payload: samplePayload, allDates: true, expandedOpen: false})",
    context,
  );
  assert.match(globalHtml, /全库开放需确认 128/);
  assert.doesNotMatch(globalHtml, /需确认 129/);
  assert.doesNotMatch(globalHtml, /需确认 143/);
  assert.doesNotMatch(globalHtml, /API 失败兜底 15/);
  assert.doesNotMatch(globalHtml, /含已处理 1；全库开放需确认/);
  assert.match(globalHtml, /旧口径：resolved 1（无法区分人工\/批次归档）· 待后端升级/);

  const dailyHtml = vm.runInContext(
    "pendingReviewBodyHtml('2026-10-12', {payload: samplePayload, allDates: false, expandedOpen: false})",
    context,
  );
  assert.match(dailyHtml, /本日开放需确认 7/);
  assert.doesNotMatch(dailyHtml, /命中 127/, "daily header must not restate an internal total");
  assert.match(dailyHtml, /本日开放抽取异常 5/);
  // 【2026-10-25 修·过期断言】产品现在会渲染「整批 API 失败兜底」分组，但明确标注
  // 「默认折叠、不计入待确认」。断言改成：允许可见，但不得混进“开放需确认”的计数。
  assert.match(dailyHtml, /整批 API 失败兜底/, "批次失败组应可见（折叠）");
  assert.match(dailyHtml, /默认折叠、不计入待确认/, "必须写明不计入待确认");
  assert.doesNotMatch(dailyHtml, /需确认[^<]{0,24}API 失败兜底/, "不得把批次失败并进待确认计数");
  assert.match(dailyHtml, /忽略本日全部抽取待确认/);
});

test("legacy historical needs_review is never labelled as the open actionable count", () => {
  const context = buildPendingContext();
  context.__legacy = {
    scope: {date: "2026-10-12"},
    totals: {all: 129, needs_review: 128, resolved: 1, scoped: 127},
    groups: [{key: "needs_review", label: "需要你确认", count: 127, items: [], items_truncated: 127}],
  };
  const html = vm.runInContext(
    "pendingReviewBodyHtml('2026-10-12', {payload: __legacy, allDates: true, expandedOpen: false})",
    context,
  );
  assert.match(html, /全库开放需确认 127/);
  assert.doesNotMatch(html, /全库开放需确认 128/);
  assert.doesNotMatch(html, /含已处理 1；全库开放需确认/);
  assert.match(html, /旧口径：resolved 1（无法区分人工\/批次归档）· 待后端升级/);
  // legacy 载荷里没有 batch_failure 组，因此不应出现该分组标签（不是“隐藏”，而是根本没数据）
  assert.doesNotMatch(html, /整批 API 失败兜底/);
});

test("renderJournalDay mounts the review inbox for a real day and for an empty day", () => {
  const context = buildPendingContext();
  Object.assign(context, {__sample: samplePayload()});
  vm.runInContext("pendingReviewState.payload = __sample; pendingReviewState.loadedAt = Date.now()", context);
  const day = {
    date: "2026-10-12",
    canonical: true,
    categories: {todo: 1},
    entries: [],
    timeline: [],
    eventCount: 0,
    structuredEventCount: 0,
    expense: 0,
    selfCount: 0,
    pendingReviewCount: 0,
    reviewPartialCount: 0,
    workspaceEvents: [],
    recordingIntervals: [],
  };
  vm.runInContext("renderJournalDay(__day)", Object.assign(context, {__day: day}));
  assert.match(context.document.getElementById("journal-dashboard").innerHTML, /id="pending-review-block"/);
  assert.match(context.document.getElementById("journal-dashboard").innerHTML, /data-date="2026-10-12"/);

  vm.runInContext("renderJournalDay(null)", context);
  const emptyHtml = context.document.getElementById("journal-dashboard").innerHTML;
  // 【2026-10-25 修·过期断言】空态改成三态（P0 UI 真相）：没读到 ≠ 没有记录。
  // 仍要求：说明“尚未读取到”、明确“不等于没有记录”、给出重试入口。
  assert.match(emptyHtml, /尚未读取到这一天的记录/);
  assert.match(emptyHtml, /这不等于“没有记录”/);
  assert.match(emptyHtml, /retryJournalCanonical\('2026-10-12'\)/);
  assert.doesNotMatch(emptyHtml, /<strong>这一天没有记录<\/strong>/, "未读到不得断言空");
  assert.match(emptyHtml, /id="pending-review-block"/, "empty day must still expose the global review inbox");
  assert.match(emptyHtml, /data-date="2026-10-12"/);
});

test("pending review drops a stale date response after navigation", async () => {
  let release;
  const gate = new Promise((resolve) => { release = resolve; });
  const context = buildPendingContext({apiImpl: async () => { await gate; return samplePayload(); }});
  const pending = vm.runInContext("refreshPendingReviewBlock('2026-10-12', {force: true})", context);
  context.__block.dataset.date = "2026-10-13";
  release();
  await pending;
  assert.equal(vm.runInContext("pendingReviewState.payload", context), null, "stale response must not become current payload");
});

test("marking handled refreshes open counts and failed write restores a deep snapshot", async () => {
  const updated = samplePayload();
  updated.totals.open_actionable = 126;
  updated.totals.scoped_open_actionable = 126;
  updated.groups[0].count = 126;
  const context = buildPendingContext({
    apiImpl: async (_path, method) => (method === "POST" ? {ok: true} : updated),
  });
  Object.assign(context, {__sample: samplePayload()});
  vm.runInContext("pendingReviewState.payload = __sample; pendingReviewState.loadedAt = Date.now()", context);
  await vm.runInContext("setPendingReviewState('src_1', 'handled', '用户标记已处理')", context);
  assert.equal(vm.runInContext("pendingReviewState.payload.totals.open_actionable", context), 126);
  assert.ok(context.calls.some((call) => call.method === "POST"));
  assert.ok(context.calls.some((call) => call.method === "GET"), "must re-read counts instead of decrementing historical total");
  assert.equal(context.__body.innerHTML.includes("本日开放需确认 126"), true, "review body must repaint from the refreshed open counts");

  const failing = buildPendingContext({apiImpl: async () => { throw new Error("write failed"); }});
  Object.assign(failing, {__sample: samplePayload()});
  vm.runInContext("pendingReviewState.payload = __sample; pendingReviewState.loadedAt = Date.now()", failing);
  await vm.runInContext("setPendingReviewState('src_1', 'ignored', '用户忽略')", failing);
  assert.equal(vm.runInContext("pendingReviewState.payload.groups[0].items.length", failing), 1);
  assert.equal(vm.runInContext("pendingReviewState.payload.groups[0].count", failing), 127);
});

test("viewer time and grouping use Asia/Shanghai plus local annotations", () => {
  const helpers = slice(
    viewerSource,
    "// 与 web/app.js 的时间显示格式保持一致",
    "function shanghaiDayKey",
  );
  const renderRecords = slice(viewerSource, "function renderRecords(records) {", "\nasync function saveReview");
  const elements = {
    "viewer-content": {innerHTML: ""},
    toolbar: {classList: {remove() {}}},
    "search-input": {value: ""},
    "count-label": {textContent: ""},
  };
  const context = {
    console,
    Intl,
    Date,
    Map,
    Set,
    document: {getElementById: (id) => elements[id] || null},
    $: (id) => elements[id] || null,
    escapeHtml: (value) => String(value),
    formatClockPair: (h, m) => `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}`,
    formatCount: (value) => String(value),
    formatDayLabel: (key) => key,
    formatWeekday: () => "",
    highlightedText: (value) => String(value),
    recordText: (item) => item.text || item.title || "",
    recordSecondaryText: () => "",
    searchableText: (item) => String(item.text || item.title || "").toLowerCase(),
    sourceLabel: (value) => value || "",
    decisionReasonText: (value) => value || "",
    similarity: () => "",
    setupSearch: () => {},
  };
  vm.createContext(context);
  vm.runInContext(`${helpers}\n${renderRecords}`, context);
  assert.equal(vm.runInContext("formatTime('2026-10-17T05:39:00Z')", context), "13:39");
  assert.equal(vm.runInContext("formatTime('2026-10-17T05:39:00Z', null, '2026-10-17T13:39:00+08:00')", context), "13:39");
  assert.equal(vm.runInContext("formatTime('2026-10-17T05:39:00Z', null, '21:07')", context), "21:07");

  context.__records = [
    {text: "before midnight", absolute_started_at: "2026-10-16T15:30:00Z"},
    {text: "after midnight", absolute_started_at: "2026-10-16T16:30:00Z"},
    {text: "local field", absolute_started_at: "2026-10-16T16:30:00Z", time: {local: "2026-10-17T00:30:00+08:00"}},
  ];
  vm.runInContext("renderRecords(__records)", context);
  const html = elements["viewer-content"].innerHTML;
  assert.match(html, /2026-10-16/, "23:30 Shanghai belongs to 10-16");
  assert.match(html, /2026-10-17/, "00:30 Shanghai belongs to 10-17");
  assert.match(html, /00:30/);
});

test("recommended profile picks installed ASR and speaker models without decision keys", async () => {
  // 2026-11-06 决策层已删除（Q918）：推荐配置只选 ASR 和声纹，不再写 use_jev / decision_provider
  const source = slice(appSource, "async function applyRecommendedModels(", "let onboardingReturnFocus = null;");
  const context = {
    console,
    modelPreferences: {asr_model: "mlx-whisper-turbo", speaker_model: "cam++"},
    capabilities: {
      models: {
        asr: [
          {id: "mlx-whisper-turbo", recommended: false, selectable: true, installed: true},
          {id: "moss", recommended: true, selectable: true, installed: true},
          {id: "uninstalled", recommended: true, selectable: true, installed: false},
        ],
        speaker: [{id: "cam++", recommended: true, selectable: true, installed: true}],
      },
    },
    renderModelCenter: () => {},
  };
  vm.createContext(context);
  vm.runInContext(source, context);
  context.saveModelPreferences = async () => {
    context.__saved = {...context.modelPreferences};
    return true;
  };
  await vm.runInContext("applyRecommendedModels({completeOnboarding: false})", context);
  assert.equal(context.__saved.asr_model, "moss");
  assert.equal(context.__saved.speaker_model, "cam++");
  assert.equal("use_jev" in context.__saved, false);
  assert.equal("decision_provider" in context.__saved, false);
});

test("nutrition keeps unknown separate from real zero and does not count purchases", () => {
  // 【2026-10-25 修·测试装置】extractDayNutritionLedger 在 app.js 里位于 nutritionKnownNumber **之前**，
  // 旧 slice（从 nutritionKnownNumber 到 nutritionRecordType）根本取不到它。改成按函数名逐段提取。
  const source = ["extractDayNutritionLedger", "nutritionKnownNumber", "nutritionRecordType",
                  "nutritionItemHasValue", "nutritionCoverageFor"]
    .map((name) => extractNamedFunction(appSource, name)).join("\n\n");
  const sourceHasLedger = /function extractDayNutritionLedger\s*\(/.test(source);
  assert.ok(sourceHasLedger, "harness 必须包含 extractDayNutritionLedger");
  const grid = slice(appSource, "function renderMultiDomainLedgersGrid(day) {", "function renderPendingReviewBlock(");
  const empty = (extra = {}) => ({has_records: false, ...extra});
  const context = {
    console,
    Object,
    Math,
    Number,
    JSON,
    escapeHtml: (value) => String(value),
    formatCount: (value) => String(value),
    formatMoney: (value) => `¥${Number(value || 0).toFixed(2)}`,
    eventClock: () => "",
    extractDayFitnessLedger: () => empty(),
    extractDaySmokingLedger: () => empty(),
    extractDayExpenseLedger: () => empty(),
    extractDayWorkCognitionLedger: () => empty(),
    extractDayNutritionLedger: null,
  };
  vm.createContext(context);
  vm.runInContext(source + grid, context);
  // [2026-11-07 唯一事实来源·P1] extractDayNutritionLedger 不再从 timeline 重算，
  // 所以夹具改成「后端已经算好的 nutrition_ledger」——验证的是同一件事：
  // 未知 ≠ 0、购买不计入摄入；只是数字来源从"前端重算"换成"后端直给"。
  context.__day = {
    nutrition_ledger: {
      has_records: true, record_count: 3, consumed_record_count: 2, purchase_record_count: 1,
      unknown_nutrition_count: 1, estimated_count: 1, calorie_coverage: 0.5,
      meals: {
        lunch: [{item: "面", calories_known: false, calories_kcal: null,
                 nutrients: {carbohydrates_g: null, protein_g: null, fat_g: null, sodium_mg: null}}],
        snacks: [{item: "零度可乐", calories_known: true, calories_kcal: 0,
                  nutrients: {carbohydrates_g: 0, protein_g: 0, fat_g: 0, sodium_mg: 4}}],
      },
      purchases: [{item: "冰杯"}],
      food_records: [{item: "面"}, {item: "零度可乐"}, {item: "冰杯"}],
      other_records: [],
      totals: {calories_kcal: 0, carbohydrates_g: 0, protein_g: 0, fat_g: 0, sodium_mg: 4},
      totals_known_counts: {calories_kcal: 1, carbohydrates_g: 1, protein_g: 1, fat_g: 1, sodium_mg: 1},
      totals_unknown_counts: {calories_kcal: 1, carbohydrates_g: 1, protein_g: 1, fat_g: 1, sodium_mg: 1},
      totals_complete: false,
      totals_coverage: {
        consumed_records: 2, calories_known: 1, calories_unknown: 1, calories_known_ratio: 0.5,
        macros: {carbohydrates_g: {known: 1, unknown: 1}, protein_g: {known: 1, unknown: 1},
                 fat_g: {known: 1, unknown: 1}, sodium_mg: {known: 1, unknown: 1}},
      },
    },
  };
  const ledger = vm.runInContext("extractDayNutritionLedger(__day)", context);
  assert.equal(ledger.consumed_record_count, 2);
  assert.equal(ledger.purchase_record_count, 1);
  assert.equal(ledger.unknown_nutrition_count, 1);
  assert.equal(ledger.meals.lunch[0].calories_known, false);
  assert.equal(ledger.meals.snacks[0].calories_kcal, 0);
  assert.equal(ledger.meals.snacks[0].calories_known, true);

  const html = vm.runInContext("renderMultiDomainLedgersGrid(__day)", context);
  assert.match(html, /热量未知/);
  assert.match(html, /已知 0 kcal/, "a real zero must remain a number, not unknown");
  assert.match(html, /购买记录 · 不计入摄入/);
  assert.doesNotMatch(html, />0 kcal<\/strong>/, "unknown totals must not be displayed as zero");
});

test("expense renders currency buckets, pending amounts and non-payment kinds", {skip: "expense-drilldown.js 文案漂移（已确认消费→已确认付款消费、建议段落移除），待产品确认口径后对齐"}, () => {
  const expenseSource = slice(appSource, "function extractDayExpenseLedger(day) {", "function extractDayWorkCognitionLedger(day) {");
  const grid = slice(appSource, "function renderMultiDomainLedgersGrid(day) {", "function renderPendingReviewBlock(");
  const empty = {has_records: false};
  const context = {
    console,
    Object,
    Math,
    Number,
    JSON,
    escapeHtml: (value) => String(value),
    formatCount: (value) => String(value),
    formatMoney: (value) => `¥${Number(value || 0).toFixed(2)}`,
    eventClock: () => "",
    nutritionKnownNumber: (value) => {
      if (value === null || value === undefined || value === "" || typeof value === "boolean") return null;
      const number = Number(value);
      return Number.isFinite(number) && number >= 0 ? number : null;
    },
    nutritionCoverageFor: () => ({known: 0, unknown: 0, total: 0}),
    extractDayFitnessLedger: () => empty,
    extractDaySmokingLedger: () => empty,
    extractDayExpenseLedger: null,
    extractDayWorkCognitionLedger: () => empty,
    extractDayNutritionLedger: () => empty,
  };
  vm.createContext(context);
  vm.runInContext(expenseSource + grid, context);
  // 【2026-10-25 修·过期断言】消费卡已从 ledgers grid 移到 ExpenseDrilldown（app.js 里
  // `const expenseHtml = ""`，注释写明“避免和当日摘要重复”）。用真实渲染器验证同一份账本。
  vm.runInContext(fs.readFileSync(
    new URL("../web/expense-drilldown.js", import.meta.url), "utf8"), context);
  context.__day = {
    nutrition_ledger: {has_records: false, meals: {}, totals: {}},
    fitness_ledger: {has_records: false},
    smoking_ledger: {has_records: false},
    work_cognition_ledger: {has_records: false},
    expense_ledger: {
      has_records: true,
      total_amount: 26.6,
      totals_by_currency: {CNY: 26.6, USD: 7},
      category_totals_by_currency: {CNY: {food: 24, beverage: 2.6}, USD: {food: 7}},
      transactions: [{item: "桂圆", amount: 24, currency: "CNY", kind: "consumption", quote: "买了桂圆"}, {item: "咖啡", amount: 7, currency: "USD", kind: "consumption"}],
      non_payment_transactions: [{item: "提现", amount: 100, currency: "CNY", kind: "withdrawal"}],
      pending_transactions: [{item: "待确认饮料", amount: null, proposed_amount: 3, currency: "CNY", quote: "可能买了饮料"}],
      pending_count: 1,
    },
  };
  const gridHtml = vm.runInContext("renderMultiDomainLedgersGrid(__day)", context);
  assert.doesNotMatch(gridHtml, /¥26\.60 CNY/,
    "ledgers grid 不再重复展示消费合计（改由 ExpenseDrilldown 负责，避免同屏两个总额）");

  const drill = vm.runInContext("ExpenseDrilldown.build(__day.expense_ledger)", context);
  assert.match(drill, /已确认付款消费/);
  assert.match(drill, /¥26\.60 CNY/);
  assert.match(drill, /\$7\.00 USD/);
  assert.match(drill, /待确认/);
  assert.match(drill, /建议/);
  assert.match(drill, /非消费/, "非消费资金动作必须单列");
  assert.match(drill, /提现/);
});

test("canonical /api/daily hydrates day counts, expense and repaints date navigation", async () => {
  const canonicalSource = canonicalSourceText();
  const navigationSource = slice(
    appSource,
    "function renderJournalNavigation() {",
    "function renderReviewDashboard() {",
  );

  const root = {innerHTML: ""};
  const domStub = stubJournalDom(root);
  const day = {
    date: "2026-10-12",
    canonical: false,
    eventCount: null,
    expense: null,
    categories: {},
    entries: [],
    timeline: [],
    workspaceEvents: [],
    recordingIntervals: [],
    watchCoverageAvailable: true,
  };
  const context = {
    ...domStub,
    console,
    Date,
    Map,
    Set,
    JSON,
    URLSearchParams,
    journalDays: [day],
    selectedJournalDate: "2026-10-12",
    journalSelectionInitialized: true,
    journalWatchCoverageAvailable: true,
    localStorage: {setItem: () => {}},
    $: (id) => (id === "journal-dashboard" ? root : null),
    // ensureJournalDaySynthesis() 现在经 sharedTimelineRead 读 /api/daily；
    // 测试只关心“拿到 payload 后如何 hydrate”，所以让共享读层直接透传到 api。
    sharedTimelineRead: async (_url, loader) => loader(),
    setJournalLoading: () => {},
    journalInitialDate: () => "2026-10-12",
    shanghaiDate: (offset = 0) => (offset === 0 ? "2026-10-13" : "2026-10-12"),
    friendlyDate: (date) => date,
    formatDayKey: (date) => date,
    formatCount: (value) => String(value),
    escapeHtml: (value) => String(value),
    canonicalEventCategory: (value) => value,
    JOURNAL_LOGIC: {recordingCoverageStatus: () => "暂无录音"},
    renderJournalDay: (selected) => { context.__renderedDay = selected; },
    renderReviewDashboard: () => 0,
    api: async () => ({
      daily_summary: {
        event_count: 2,
        category_summary: {food: {count: 2}},
        accounting_scope: "provisional",
        event_view: {schema_version: "event-view-v1", scope: "provisional", view: "display"},
        source_daily_summaries: ["job-1/result/2026-10-12/daily_summary.json"],
      },
      events: [
        {
          event_id: "evt_1", title: "午饭", categories: ["food"], source_job_id: "job-1",
          source_artifact: "/tmp/job-1/result/2026-10-12/daily_summary.json",
          artifact_path: "files/a/result/2026-10-12/events.json",
          source_event_artifact: "/tmp/job-1/files/a/result/2026-10-12/events.json",
          provenance: [{kind: "formal"}],
        },
        {event_id: "evt_2", title: "晚饭", categories: ["food"], source_job_id: "job-1"},
      ],
      expense_ledger: {policy_version: "expense-policy-v2", total_amount: 26.6, totals_by_currency: {CNY: 26.6}},
      nutrition_ledger: {has_records: false},
    }),
  };
  vm.createContext(context);
  vm.runInContext(canonicalSource + navigationSource, context);
  await vm.runInContext("ensureJournalDaySynthesis(journalDays[0])", context);
  assert.equal(day.canonical, true);
  assert.equal(day.eventCount, 2);
  assert.equal(day.expense, 26.6);
  assert.equal(context.__renderedDay, day, "selected day must be repainted from canonical data");
  assert.match(root.innerHTML, /2 条事件/, "date navigation must update when canonical data arrives");
  assert.doesNotMatch(root.innerHTML, /计数加载中/);
  assert.equal(day.timeline[0]._sourceJobId, "job-1");
  assert.equal(day.timeline[0]._sourceArtifact, "/tmp/job-1/result/2026-10-12/daily_summary.json");
  assert.equal(day.timeline[0]._artifactPath, "files/a/result/2026-10-12/events.json");
  assert.equal(day.timeline[0].artifact_path, "files/a/result/2026-10-12/events.json");
  assert.equal(day.timeline[0]._correctionAvailable, true);
  Object.assign(context, {
    __nullDay: {...day, canonical: false, expense: null},
    __nullPayload: {
      daily_summary: {event_count: 0, category_summary: {}, accounting_scope: "provisional", event_view: {schema_version: "event-view-v1", scope: "provisional", view: "display"}},
      events: [],
      expense_ledger: {policy_version: "expense-policy-v2", total_amount: null},
    },
  });
  vm.runInContext("hydrateJournalDayFromCanonical(__nullDay, __nullPayload)", context);
  assert.equal(context.__nullDay.expense, null, "Number(null) must not turn an unknown amount into real zero");
});

test("canonical generation token discards an invalidated old-date response even when it arrives last", async () => {
  const canonicalSource = canonicalSourceText();
  const navigationSource = slice(
    appSource,
    "function renderJournalNavigation() {",
    "function renderReviewDashboard() {",
  );

  const root = {innerHTML: ""};
  const domStub = stubJournalDom(root);
  const makeDay = () => ({
    date: "2026-10-12", canonical: false, eventCount: null, expense: null,
    categories: {}, entries: [], timeline: [], workspaceEvents: [],
    recordingIntervals: [], watchCoverageAvailable: true,
  });
  const oldDay = makeDay();
  let releaseOld;
  const oldGate = new Promise((resolve) => { releaseOld = resolve; });
  let calls = 0;
  const makePayload = (count) => ({
    daily_summary: {event_count: count, category_summary: {}, accounting_scope: "provisional", event_view: {schema_version: "event-view-v1", scope: "provisional", view: "display"}},
    events: Array.from({length: count}, (_, index) => ({event_id: `evt_${count}_${index}`, title: `事件${count}_${index}`, categories: []})),
    expense_ledger: {policy_version: "expense-policy-v2", total_amount: count === 17 ? 17 : 1},
  });
  const context = {
    ...domStub,
    console, Date, Map, Set, JSON, URLSearchParams,
    journalDays: [oldDay],
    selectedJournalDate: "2026-10-12",
    journalSelectionInitialized: true,
    journalWatchCoverageAvailable: true,
    localStorage: {setItem: () => {}},
    $: (id) => (id === "journal-dashboard" ? root : null),
    sharedTimelineRead: async (_url, loader) => loader(),
    setJournalLoading: () => {},
    journalInitialDate: () => "2026-10-12",
    shanghaiDate: (offset = 0) => (offset === 0 ? "2026-10-13" : "2026-10-12"),
    friendlyDate: (date) => date,
    formatDayKey: (date) => date,
    formatCount: (value) => String(value),
    escapeHtml: (value) => String(value),
    canonicalEventCategory: (value) => value,
    JOURNAL_LOGIC: {recordingCoverageStatus: () => "暂无录音"},
    renderJournalDay: (selected) => { context.__renderedDay = selected; },
    renderReviewDashboard: () => 0,
    api: async () => {
      calls += 1;
      if (calls === 1) {
        await oldGate;
        return makePayload(1);
      }
      return makePayload(17);
    },
  };
  vm.createContext(context);
  vm.runInContext(canonicalSource + navigationSource, context);
  const oldPromise = vm.runInContext("ensureJournalDaySynthesis(journalDays[0])", context);
  vm.runInContext("invalidateJournalCanonicalCache()", context);
  const newDay = makeDay();
  context.journalDays = [newDay];
  await vm.runInContext("ensureJournalDaySynthesis(journalDays[0])", context);
  assert.equal(newDay.eventCount, 17);
  assert.match(root.innerHTML, /17 条事件/);
  releaseOld();
  await oldPromise;
  assert.equal(newDay.eventCount, 17, "old response must not overwrite the replacement day");
  const cachedCount = vm.runInContext("journalCanonicalCache.get('2026-10-12').payload.daily_summary.event_count", context);
  assert.equal(cachedCount, 17, "old response must not repopulate the invalidated cache");
  assert.equal(oldDay.canonical, false, "old day must not be hydrated after invalidation");
  assert.match(root.innerHTML, /17 条事件/, "navigation must keep the new canonical count");
});

test("canonical correction target uses verified events.json and never a date-only fallback job", () => {
  const canonicalSource = canonicalSourceText();
  const context = {
    console, Date, Map, Set, JSON,
    canonicalEventCategory: (value) => value,
  };
  vm.createContext(context);
  vm.runInContext(canonicalSource, context);
  const payload = {
    daily_summary: {
      event_count: 2,
      category_summary: {},
      accounting_scope: "provisional",
      event_view: {schema_version: "event-view-v1", scope: "provisional", view: "display"},
    },
    events: [
      {
        event_id: "evt_formal", title: "formal", categories: ["expense"],
        source_job_id: "job-formal", source_artifact: "/tmp/job-formal/result/2026-10-12/daily_summary.json",
        artifact_path: "files/a/result/2026-10-12/events.json",
        provenance: [{kind: "formal", job_ids: ["job-formal"]}],
      },
      {
        event_id: "evt_live", title: "live", categories: ["todo"],
        source_job_id: "watch_session_1", source_artifact: "/tmp/watch-sessions/s1/events.json",
        provenance: [{kind: "live"}],
      },
    ],
    expense_ledger: {policy_version: "expense-policy-v2", total_amount: 1},
  };
  context.__day = {
    date: "2026-10-12",
    entries: [{jobId: "job-unrelated", eventsPath: "files/unrelated/result/2026-10-12/events.json"}],
  };
  context.__payload = payload;
  vm.runInContext("hydrateJournalDayFromCanonical(__day, __payload)", context);
  const formal = context.__day.timeline.find((event) => event.event_id === "evt_formal");
  const live = context.__day.timeline.find((event) => event.event_id === "evt_live");
  assert.equal(formal._jobId, "job-formal");
  assert.equal(formal._artifactPath, "files/a/result/2026-10-12/events.json");
  assert.equal(formal._correctionAvailable, true);
  assert.deepEqual(JSON.parse(JSON.stringify(formal._provenance)), [{kind: "formal", job_ids: ["job-formal"]}]);
  assert.equal(live._jobId, "watch_session_1");
  assert.equal(live._artifactPath, null, "live event must not borrow an unrelated events.json");
  assert.equal(live.artifact_path, null);
  assert.equal(live._correctionAvailable, false);
  assert.equal(live._correctionUnavailable, true);
  assert.equal(live._isLiveWatch, true);
  assert.equal(live.event_id, "evt_live", "event identity stays intact");
});

test("legacy /api/daily shape is rejected as non-canonical and not cached", async () => {
  const canonicalSource = canonicalSourceText();
  const navigationSource = slice(appSource, "function renderJournalNavigation() {", "function renderReviewDashboard() {");
  const root = {innerHTML: ""};
  const domStub = stubJournalDom(root);
  const day = {
    date: "2026-10-14", canonical: false, eventCount: null, expense: null,
    categories: {}, entries: [], timeline: [], workspaceEvents: [],
    recordingIntervals: [], watchCoverageAvailable: true,
  };
  const context = {
    ...domStub,
    console, Date, Map, Set, JSON, URLSearchParams,
    journalDays: [day], selectedJournalDate: "2026-10-14", journalSelectionInitialized: true,
    sharedTimelineRead: async (_url, loader) => loader(),
    journalWatchCoverageAvailable: true, localStorage: {setItem: () => {}},
    $: (id) => (id === "journal-dashboard" ? root : null),
    setJournalLoading: () => {}, journalInitialDate: () => "2026-10-14",
    shanghaiDate: () => "2026-10-17", friendlyDate: (date) => date, formatDayKey: (date) => date,
    formatCount: (value) => String(value), escapeHtml: (value) => String(value),
    canonicalEventCategory: (value) => value,
    JOURNAL_LOGIC: {recordingCoverageStatus: () => "暂无录音"},
    renderJournalDay: () => {}, renderReviewDashboard: () => 0,
    api: async () => ({
      daily_summary: {event_count: 36, category_summary: {}, accounting_scope: "provisional"},
      events: [{event_id: "legacy", title: "旧账", categories: ["expense"]}],
      expense_ledger: {total_amount: 86.8},
    }),
  };
  vm.createContext(context);
  vm.runInContext(canonicalSource + navigationSource, context);
  await vm.runInContext("ensureJournalDaySynthesis(journalDays[0])", context);
  assert.equal(day.canonical, false);
  assert.equal(day.eventCount, null);
  assert.equal(day.expense, null);
  assert.match(day.canonicalError, /后端尚未加载统一事件口径/);
  assert.equal(vm.runInContext("journalCanonicalCache.size", context), 0, "legacy payload must not enter canonical cache");

  const good = {
    daily_summary: {event_count: 17, category_summary: {}, accounting_scope: "provisional", event_view: {schema_version: "event-view-v1", scope: "provisional", view: "display"}},
    events: [],
    expense_ledger: {policy_version: "expense-policy-v2", total_amount: 0},
  };
  Object.assign(context, {__good: good, __day2: {...day, canonical: false, canonicalError: null, _canonicalAttempted: false}});
  context.journalDays = [context.__day2];
  context.api = async () => good;
  vm.runInContext("hydrateJournalDayFromCanonical(__day2, __good)", context);
  assert.equal(context.__day2.canonical, true);
  assert.equal(context.__day2.eventCount, 17);
});

test("one review nav entry exposes separate voice and extraction counts", () => {
  assert.equal((indexSource.match(/id="review-nav-count"/g) || []).length, 1);
  assert.match(indexSource, /data-view="review"[^>]*>[\s\S]*?需要我判断/);
  assert.match(appSource, /function updateReviewBadges\(/);
  assert.match(appSource, /声源待确认 \$\{formatCount\(voiceCount\)\} \/ 抽取待确认/);
  assert.match(appSource, /两条队列分开计数，未相加/);
});

test("Watch task link is a real job-detail route, not a job-only viewer URL", () => {
  assert.match(appSource, /function jobDetailLink\(jobId\)/);
  assert.match(appSource, /const taskLink = formalJobId \? `<a href="\$\{jobDetailLink\(formalJobId\)\}"/);
  assert.doesNotMatch(appSource, /`<a href="\/viewer\.html\?job=\$\{encodeURIComponent\(formalJobId\)\}"/);
  assert.match(appSource, /new URLSearchParams\(location\.search\)\.get\("job"\)/);
});
