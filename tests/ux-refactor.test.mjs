import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";
import test from "node:test";

const appSource = fs.readFileSync(new URL("../web/app.js", import.meta.url), "utf8");
const indexSource = fs.readFileSync(new URL("../web/index.html", import.meta.url), "utf8");

function slice(source, startNeedle, endNeedle) {
  const start = source.indexOf(startNeedle);
  const end = source.indexOf(endNeedle, start);
  assert.notEqual(start, -1, `missing start: ${startNeedle}`);
  assert.notEqual(end, -1, `missing end: ${endNeedle}`);
  return source.slice(start, end);
}

const escapeHtml = (value) => String(value)
  .replace(/&/g, "&amp;")
  .replace(/</g, "&lt;")
  .replace(/>/g, "&gt;")
  .replace(/"/g, "&quot;")
  .replace(/'/g, "&#39;");

test("Watch preview segments render real HTML and never the DocumentFragment literal", () => {
  const source = slice(
    appSource,
    "function renderWatchPreviewSegments(preview) {",
    "function watchPreviewCount(preview, key) {",
  );
  const context = {
    console,
    Array,
    Number,
    escapeHtml,
    watchRecordingDetailTime: () => "2026-10-17 12:00",
    compactDurationLabel: () => "1 分 05 秒",
    formatPercent: (value) => `${Math.round(value * 100)}%`,
  };
  vm.createContext(context);
  vm.runInContext(source, context);
  context.__preview = {
    segments: [
      {
        sequence: 3,
        status: "completed",
        start_at: "2026-10-17T04:00:00Z",
        duration_ms: 65000,
        rows: [
          {text: "<script>alert(1)</script>", source_label: "self", voice_similarity: 0.93},
          {text: "第二行", source_label: "unknown"},
        ],
      },
      {sequence: 4, status: "no_speech"},
      {sequence: 5, status: "failed", error: "ASR <failed>"},
    ],
  };
  const html = vm.runInContext("renderWatchPreviewSegments(__preview)", context);
  assert.equal(typeof html, "string", "render helper must return HTML text, not a DOM fragment");
  assert.doesNotMatch(html, /\[object DocumentFragment\]/);
  assert.match(html, /<article class="watch-preview-segment watch-preview-completed" role="listitem">/);
  assert.match(html, /第 3 段 · 已完成/);
  assert.match(html, /&lt;script&gt;alert\(1\)&lt;\/script&gt;/);
  assert.doesNotMatch(html, /<script>alert/);
  assert.match(html, /来源：本人/);
  assert.match(html, /相似度：93%/);
  assert.match(html, /本段无语音/);
  assert.match(html, /ASR &lt;failed&gt;/);
});

function reviewContext() {
  const root = {innerHTML: "", setAttribute() {}};
  const badge = {textContent: "", title: "", classList: {toggle() {}}};
  let refreshCalls = 0;
  const context = {
    console,
    Date,
    Number,
    Array,
    JSON,
    journalDays: [{
      date: "2026-10-17",
      pendingReviewCount: 5,
      reviewCount: 223,
      reviewPartialCount: 0,
      reviewCompletedCount: 4,
      reviewTargets: [{kind: "unknown", count: 223, jobId: "job-1", path: "events.json"}],
    }],
    selectedJournalDate: "2026-10-17",
    pendingReviewState: {
      payload: {
        ok: true,
        scope: {date: "2026-10-17"},
        totals: {open_actionable: 48, scoped_open_actionable: 48},
        groups: [{key: "needs_review", count: 48, items: []}],
      },
      expanded: null,
      expandedOpen: false,
      allDates: false,
      error: null,
      loadedAt: Date.now(),
      inflight: null,
    },
    escapeHtml,
    formatCount: (value) => String(value),
    pendingReviewNumber: (value) => String(value),
    friendlyDate: (date) => date,
    viewerLink: (jobId, path) => `/viewer.html?job=${jobId}&path=${path}`,
    shanghaiDate: () => "2026-10-17",
    pendingOpenCounts: (totals) => ({
      globalActionable: totals.open_actionable,
      scopedActionable: totals.scoped_open_actionable,
      globalGap: 0,
      scopedGap: 0,
      openBatch: 0,
    }),
    renderPendingReviewBlock: () => '<section id="pending-review-block">SOURCE_QUEUE</section>',
    refreshPendingReviewBlock: () => { refreshCalls += 1; },
    window: {addEventListener() {}, removeEventListener() {}},
    document: {
      getElementById: (id) => {
        if (id === "review-dashboard") return root;
        if (id === "review-nav-count") return badge;
        return null;
      },
      querySelectorAll: () => [],
    },
    $: (id) => (id === "review-dashboard" ? root : (id === "review-nav-count" ? badge : null)),
    __root: root,
    __badge: badge,
    __refreshCalls: () => refreshCalls,
  };
  const source = slice(appSource, 'let activeReviewTab = "voice";', "async function refreshJournal(");
  vm.createContext(context);
  vm.runInContext(source, context);
  return context;
}

test("need-to-judge page has two reachable tabs, separate counts and no contradictory claim", () => {
  const context = reviewContext();
  vm.runInContext("renderReviewDashboard()", context);
  const voiceHtml = context.__root.innerHTML;
  assert.match(voiceHtml, /role="tablist"/);
  assert.match(voiceHtml, /声源待确认 <span[^>]*>223<\/span>/);
  assert.match(voiceHtml, /抽取待确认 <span[^>]*>48<\/span>/);
  assert.match(voiceHtml, /id="review-voice-panel"/);
  assert.doesNotMatch(voiceHtml, /SOURCE_QUEUE/, "source queue stays behind its own tab");
  assert.ok(voiceHtml.indexOf("review-tab-voice") < voiceHtml.indexOf("review-tab-source"));
  assert.doesNotMatch(voiceHtml, /统一复核中心/);
  assert.doesNotMatch(voiceHtml, /重说一遍/);

  vm.runInContext("setReviewTab('source')", context);
  const sourceHtml = context.__root.innerHTML;
  assert.match(sourceHtml, /id="review-source-panel"/);
  assert.match(sourceHtml, /SOURCE_QUEUE/);
  assert.ok(sourceHtml.indexOf("review-tab-source") < sourceHtml.indexOf("SOURCE_QUEUE"), "source tab is above the queue");
  assert.equal(context.__refreshCalls(), 0, "fresh scoped payload must not trigger an extra request");

  vm.runInContext("setReviewTab('voice')", context);
  assert.match(context.__root.innerHTML, /review-voice-panel/);
  assert.doesNotMatch(context.__root.innerHTML, /SOURCE_QUEUE/);
  assert.match(context.__badge.title, /声源待确认 223 \/ 抽取待确认 48/);
  assert.match(context.__badge.title, /未相加/);
});

test("page title, nav labels and source action copy are honest", () => {
  const reviewPanel = slice(indexSource, '<section class="view-panel" data-panel="review">', '<section class="view-panel" data-panel="history">');
  assert.doesNotMatch(reviewPanel, /view-intro/, "review page keeps only the shared topbar header");
  assert.doesNotMatch(reviewPanel, /review-heading/);
  assert.match(reviewPanel, /aria-label="需要我判断"/);
  assert.doesNotMatch(indexSource, /统一复核中心/);
  assert.doesNotMatch(appSource, /统一复核中心/);
  assert.doesNotMatch(appSource, /重说一遍/);
  assert.match(appSource, /const PENDING_REVIEW_SCOPE_HINT = "模型判「无事件」但像主动指令，或模型给了待确认建议；标记只影响列表，不会入账、不会自动重跑模型。"/);
  assert.doesNotMatch(appSource, /reason_hint/);
  assert.doesNotMatch(appSource, /API 失败兜底/);
  assert.match(appSource, /忽略本日全部抽取待确认/);
  assert.match(appSource, /\/api\/pending-sources\/status-bulk/);
  assert.match(appSource, /group: "needs_review"/);
  assert.match(appSource, /status: "ignored"/);
  assert.doesNotMatch(indexSource, /local-reminders-card/);
  assert.doesNotMatch(indexSource, /本地待办提醒/);
  assert.doesNotMatch(slice(appSource, "function renderJournalDay(day) {", "async function refreshJournal"), /renderDailyWorkspace\(day\)/);
  assert.doesNotMatch(appSource, /当日工作台/);
});

function navigationContext(days, selectedDate = "2026-10-17") {
  const root = {innerHTML: "", querySelector: () => null, querySelectorAll: () => [],
    appendChild() {}, insertBefore() {}, addEventListener() {}};
  const context = {
    console,
    Date,
    Map,
    Set,
    journalDays: days,
    selectedJournalDate: selectedDate,
    window: {addEventListener() {}, removeEventListener() {}, localStorage: {setItem() {}}},
    document: {
      createElement: () => ({className: "", style: {}, dataset: {}, innerHTML: "",
        appendChild() {}, setAttribute() {}, addEventListener() {}, remove() {},
        querySelector: () => null, querySelectorAll: () => []}),
      body: {appendChild() {}, classList: {add() {}, remove() {}, toggle() {}}},
      querySelector: () => null, querySelectorAll: () => [],
      getElementById: () => null, addEventListener() {},
    },
    journalSelectionInitialized: true,
    journalWatchCoverageAvailable: true,
    localStorage: {setItem() {}},
    $: (id) => (id === "journal-dashboard" ? root : null),
    setJournalLoading() {},
    journalInitialDate: () => selectedDate,
    shanghaiDate: (offset = 0) => (offset === 0 ? "2026-10-18" : "2026-10-17"),
    friendlyDate: (date) => date,
    formatCount: (value) => String(value),
    escapeHtml,
    JOURNAL_LOGIC: {recordingCoverageStatus: () => "暂无录音"},
    renderJournalDay() {},
    renderReviewDashboard: () => 0,
    renderDailyHealthBar: () => {},
    refreshDailyHealth: async () => {},
    renderMultiDomainLedgersGrid: () => "",
    renderDailyWorkspace: () => "",
    renderSaidMetric: () => "",
    scheduleTimelineCollisionResolution: () => {},
    fitDayTimelineFrame: () => {},
    ensurePendingReviewStatsForTabs: () => {},
    updateReviewTabCounts: () => {},
    reviewPendingDays: () => [],
    renderPendingReviewBlock: () => "",
    __root: root,
  };
  const source = slice(appSource, "function renderJournalNavigation() {", 'let activeReviewTab = "voice";');
  vm.createContext(context);
  vm.runInContext(source, context);
  return context;
}

function journalDay(date, extra = {}) {
  return {
    date,
    canonical: false,
    eventCount: null,
    categories: {},
    entries: [],
    timeline: [],
    workspaceEvents: [],
    recordingIntervals: [],
    watchCoverageAvailable: true,
    ...extra,
  };
}

test("date chips say reading / failed / not loaded and never permanent 计数加载中", () => {
  const selectedDay = journalDay("2026-10-17");
  const otherDay = journalDay("2026-10-16");
  const context = navigationContext([selectedDay, otherDay]);
  vm.runInContext("renderJournalNavigation()", context);
  let html = context.__root.innerHTML;
  assert.match(html, /读取中/, "selected date is the only one being hydrated");
  assert.match(html, /选择查看/, "unselected dates must not claim a count");
  assert.doesNotMatch(html, /计数加载中/);
  const selectedChip = html.match(/<button class="day-chip[^"]*active[^"]*"[^>]*aria-label="([^"]+)"/);
  assert.ok(selectedChip, "selected chip should be present");
  assert.match(selectedChip[1], /读取中/);

  selectedDay.canonicalError = "network down";
  vm.runInContext("renderJournalNavigation()", context);
  html = context.__root.innerHTML;
  assert.match(html, /读取失败/);
  assert.doesNotMatch(html, /计数加载中/);

  selectedDay.canonical = true;
  selectedDay.canonicalError = null;
  selectedDay.eventCount = 0;
  otherDay.canonical = true;
  otherDay.eventCount = 5;
  vm.runInContext("renderJournalNavigation()", context);
  html = context.__root.innerHTML;
  assert.match(html, /5 条事件/);
  assert.doesNotMatch(html, /计数加载中/);
});

test("primary nav includes kanban and the review entry is active-state compatible", () => {
  const nav = slice(indexSource, '<nav class="app-nav"', "</nav>");
  const views = [...nav.matchAll(/data-view="([^"]+)"/g)].map((match) => match[1]);
  assert.deepEqual(views, ["today", "kanban", "stats", "review", "more"]);
  assert.match(nav, /data-view="kanban"[\s\S]*?任务看板/);
  assert.match(nav, /data-view="review"[\s\S]*?需要我判断/);
  assert.doesNotMatch(indexSource, /data-view="dashboard"/);

  const mobile = slice(indexSource, '<nav id="mobile-bottom-nav"', "</nav>");
  const mobileViews = [...mobile.matchAll(/data-mobile-view="([^"]+)"/g)].map((match) => match[1]);
  assert.deepEqual(mobileViews, ["today", "stats", "review", "more"]);
  assert.match(mobile, /aria-label="需要我判断"/);

  assert.match(appSource, /review: \["NEEDS REVIEW", "需要我判断"\]/);
  assert.match(appSource, /button\.setAttribute\("aria-current", "page"\)/);
  assert.match(appSource, /button\.removeAttribute\("aria-current"\)/);
});
