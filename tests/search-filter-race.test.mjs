import assert from "node:assert/strict";
import {readFile} from "node:fs/promises";
import test from "node:test";
import vm from "node:vm";

test("clearing a source filter discards its still-pending response", async () => {
  const source = await readFile(new URL("../web/app.js", import.meta.url), "utf8");
  const start = source.indexOf("async function runGlobalSearch() {");
  const end = source.indexOf("\nfunction renderPassportReceiverHealth", start);
  assert.ok(start >= 0 && end > start, "search function should remain extractable");

  let criteria = {
    query: "",
    dateFrom: "",
    dateTo: "",
    category: "",
    importSource: "apple-watch",
    amountMin: "",
    amountMax: "",
  };
  let resolvePending;
  const rendered = [];
  const busyStates = [];
  const summary = {textContent: ""};
  const context = {
    URLSearchParams,
    currentSearchCriteria: () => criteria,
    document: {querySelector: () => ({dataset: {searchType: "all"}})},
    $(id) {
      return id === "search-summary" ? summary : null;
    },
    viewAbortSignal: () => undefined,
    setSearchBusy: (value) => busyStates.push(value),
    renderSearchEmpty: (title) => rendered.push(`empty:${title}`),
    renderSearchLoading: () => rendered.push("loading"),
    renderSearchResults: () => rendered.push("results"),
    api(url) {
      assert.match(url, /^\/api\/events\/search\?/);
      assert.match(url, /import_source=apple-watch/);
      assert.match(url, /scope=provisional/);
      assert.match(url, /view=display/);
      return new Promise((resolve) => { resolvePending = resolve; });
    },
    importSourceLabel: (value) => value,
    CATEGORY_LABELS: {},
    highlightSearchText: (value) => value,
    escapeHtml: (value) => value,
    searchResultTime: () => "time",
  };
  vm.createContext(context);
  vm.runInContext(
    `let searchRequestGeneration = 0;\n${source.slice(start, end)}\nglobalThis.runSearch = runGlobalSearch;`,
    context,
  );

  const oldRequest = context.runSearch();
  criteria = {...criteria, importSource: "all"};
  await context.runSearch();
  resolvePending({results: [{text: "stale Watch result"}]});
  await oldRequest;

  assert.deepEqual(rendered, ["loading", "empty:输入关键词或使用筛选"]);
  assert.deepEqual(busyStates, [true, false]);
});


test("canonical search uses /api/events/search with scoped contract labels and explicit service-update failure", async () => {
  const source = await readFile(new URL("../web/app.js", import.meta.url), "utf8");
  const start = source.indexOf("async function runGlobalSearch() {");
  const end = source.indexOf("\nfunction renderPassportReceiverHealth", start);
  let criteria = {
    query: "咖啡",
    dateFrom: "2026-10-01",
    dateTo: "2026-10-17",
    category: "expense",
    importSource: "all",
    amountMin: "7",
    amountMax: "7",
  };
  const rendered = [];
  const calls = [];
  const summary = {textContent: ""};
  const context = {
    URLSearchParams,
    currentSearchCriteria: () => criteria,
    document: {querySelector: () => ({dataset: {searchType: "event"}})},
    $(id) {
      return id === "search-summary" ? summary : null;
    },
    viewAbortSignal: () => undefined,
    setSearchBusy: () => {},
    renderSearchEmpty: (title, body) => rendered.push({title, body}),
    renderSearchLoading: () => rendered.push({loading: true}),
    renderSearchResults: (results) => rendered.push({results}),
    api(url) {
      calls.push(url);
      return Promise.resolve({
        results: [{result_type: "event", event_id: "evt_1", date: "2026-10-17", amount: 7, currency: "CNY", title: "咖啡"}],
        read_contract: {accounting_scope: "provisional", view: "display", endpoint: "/api/events/search"},
      });
    },
    importSourceLabel: (value) => value,
    CATEGORY_LABELS: {expense: "消费"},
    formatDayKey: (value) => value,
    formatCount: (value) => String(value),
    formatMoney: (value) => `¥${value}`,
    escapeHtml: (value) => value,
    highlightSearchText: (value) => value,
    searchResultTime: () => "time",
  };
  vm.createContext(context);
  vm.runInContext(
    `let searchRequestGeneration = 0;\n${source.slice(start, end)}\nglobalThis.runSearch = runGlobalSearch;`,
    context,
  );
  await context.runSearch();
  assert.equal(calls.length, 1);
  assert.match(calls[0], /^\/api\/events\/search\?/);
  assert.match(calls[0], /type=event/);
  assert.match(calls[0], /category=expense/);
  assert.match(calls[0], /amount_min=7/);
  assert.match(calls[0], /amount_max=7/);
  assert.match(calls[0], /scope=provisional/);
  assert.match(calls[0], /view=display/);
  assert.match(summary.textContent, /统一生活事件（canonical event_view）/);
  assert.match(summary.textContent, /口径 provisional/);
  assert.match(summary.textContent, /视图 display/);
  assert.equal(rendered.at(-1).results[0].event_id, "evt_1");

  context.api = async (url) => {
    calls.push(url);
    const error = new Error("请求失败：404");
    error.status = 404;
    throw error;
  };
  await context.runSearch();
  assert.match(summary.textContent, /后端尚未加载统一事件搜索，请更新服务/);
  assert.equal(rendered.at(-1).title, "需要更新本地服务");
  assert.match(rendered.at(-1).body, /不会退回旧的 \/api\/search/);
  assert.doesNotMatch(calls.at(-1), /^\/api\/search\?/);
});
