import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";
import test from "node:test";

const app = fs.readFileSync(new URL("../web/app.js", import.meta.url), "utf8");
const start = app.indexOf("function preserveJournalDayTransientState");
const end = app.indexOf("function renderJournalNavigation()", start);
assert.ok(start >= 0 && end > start, "initial preload helpers exist");

function build({ensureImpl} = {}) {
  const context = {
    journalDays: [],
    journalInitialPrefetchStarted: false,
    journalInitialPrefetchAttempted: new Set(),
    journalWatchCoverageAvailable: true,
    selectedJournalDate: "2026-10-25",
    shanghaiDate: (offset = 0) => (offset === 0 ? "2026-10-25" : "2026-10-24"),
    journalCanonicalCache: new Map(),
    hydrateJournalDayFromCanonical(day, payload) {
      day.canonical = true;
      day.eventCount = payload.event_count;
      day._canonicalLoadedAt = Date.now();
    },
    ensureJournalDaySynthesis(day) {
      if (ensureImpl) return ensureImpl(day);
      return Promise.resolve();
    },
    // [Q854] 页面里 journalActivityTimelineCache 是模块级 Map，沙箱里没有 → N23 红。
    journalActivityTimelineCache: new Map(),
    journalActivityTimelineGeneration: 0,
    calls: [],
    hydrated: [],
  };
  vm.createContext(context);
  vm.runInContext(`${app.slice(start, end)}\nthis.ensureInitialJournalDays=ensureInitialJournalDays; this.startInitialJournalPrefetch=startInitialJournalPrefetch; this.hydrateJournalDaysFromCache=hydrateJournalDaysFromCache; this.preserveJournalDayTransientState=preserveJournalDayTransientState;`, context);
  return context;
}

test("冷启动先建立今天/昨天壳且不伪造 0", () => {
  const c = build();
  const days = vm.runInContext("ensureInitialJournalDays()", c);
  assert.equal(days.today.date, "2026-10-25");
  assert.equal(days.yesterday.date, "2026-10-24");
  assert.equal(days.today.canonical, false);
  assert.equal(days.today.eventCount, null);
  assert.equal(c.journalDays.length, 2);
});

test("今天请求完成后才有界预取昨天，且只预取一次", async () => {
  let resolveToday;
  const calls = [];
  const c = build({
    ensureImpl: (day) => {
      calls.push(day.date);
      if (day.date === "2026-10-25") return new Promise((resolve) => { resolveToday = resolve; });
      return Promise.resolve();
    },
  });
  vm.runInContext("startInitialJournalPrefetch()", c);
  assert.deepEqual(calls, ["2026-10-25"]);
  assert.equal(c.journalDays.find((d) => d.date === "2026-10-24")._initialPrefetchQueued, true);
  resolveToday();
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.deepEqual(calls, ["2026-10-25", "2026-10-24"]);
  vm.runInContext("startInitialJournalPrefetch()", c);
  assert.deepEqual(calls, ["2026-10-25", "2026-10-24"]);
});

test("重新分组后从 canonical cache 恢复计数，不退回未加载", () => {
  const c = build();
  c.journalDays = [{date: "2026-10-24", canonical: false, eventCount: null}];
  c.journalCanonicalCache.set("2026-10-24", {payload: {event_count: 7}});
  vm.runInContext("hydrateJournalDaysFromCache()", c);
  assert.equal(c.journalDays[0].canonical, true);
  assert.equal(c.journalDays[0].eventCount, 7);
});

test("预取不会切换当前选日", () => {
  const c = build();
  vm.runInContext("selectedJournalDate='2026-10-22'; startInitialJournalPrefetch()", c);
  assert.equal(c.selectedJournalDate, "2026-10-22");
});


test("重新分组保留预取中/失败/待重试状态，不退成未加载", () => {
  const c = build();
  const old = {date: "2026-10-24", canonical: false, canonicalError: "read_busy", _initialPrefetchQueued: true, _canonicalAttempted: true};
  const next = {date: "2026-10-24", canonical: false, canonicalError: null, canonicalPending: null};
  c.previousDays = [old]; c.nextDays = [next];
  vm.runInContext("preserveJournalDayTransientState(previousDays, nextDays)", c);
  assert.equal(next.canonicalError, "read_busy");
  assert.equal(next._initialPrefetchQueued, true);
  assert.equal(next._canonicalAttempted, true);
});
