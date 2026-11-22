import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";
import test from "node:test";

const appSource = fs.readFileSync(new URL("../web/app.js", import.meta.url), "utf8");
const functionStart = appSource.indexOf("async function refreshJournal(");
const functionEnd = appSource.indexOf("\nconst RESULT_DEFINITIONS", functionStart);
assert.notEqual(functionStart, -1, "refreshJournal should exist");
assert.notEqual(functionEnd, -1, "refreshJournal extraction boundary should exist");

test("journal refresh passes fetched Watch intervals into day grouping", async () => {
  const coverage = [{
    device_id: "watch",
    session_id: "session-1",
    sequence: 0,
    start_at: "2026-09-27T10:00:00.000Z",
    end_at: "2026-09-27T10:01:00.000Z",
  }];
  const grouped = [];
  const reviewRoot = {
    children: [{}],
    setAttribute() {},
  };
  const reviewStatus = {};
  const context = {
    journalSwitchActive: () => false,
    JOURNAL_LOGIC: {watchCoverageState: (_previous, result) => result},
    journalWatchCoverageIntervals: [],
    journalWatchCoverageAvailable: false,
    journalFingerprint: "old-fingerprint",
    journalDays: [],
    $: (id) => id === "review-dashboard" ? reviewRoot : reviewStatus,
    loadWatchCoverageIntervals: async () => ({available: true, intervals: coverage}),
    loadJournalEntries: async () => [],
    groupJournalDays: (...args) => {
      grouped.push(args);
      return [];
    },
    renderJournalNavigation() {},
    renderReviewDashboard: () => 0,
    markJournalRefreshSuccess() {},
    invalidateJournalCanonicalCache() {},
    escapeHtml: (value) => String(value),
  };
  vm.createContext(context);
  vm.runInContext(appSource.slice(functionStart, functionEnd), context);

  await vm.runInContext("refreshJournal([])", context);

  assert.equal(grouped.length, 1);
  assert.deepEqual(JSON.parse(JSON.stringify(grouped[0][1])), coverage);
  assert.equal(grouped[0][2], true);
});
