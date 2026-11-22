import {createRequire} from "node:module";

const require = createRequire(import.meta.url);
const test = require("node:test");
const assert = require("node:assert/strict");
const journalLogic = require("../web/journal_logic.js");

test("recording coverage keeps real gaps between audio intervals", () => {
  const intervals = [
    {
      started_at: "2026-09-27T09:00:00+08:00",
      ended_at: "2026-09-27T09:10:00+08:00",
      session_id: "session-a",
      sequence: 0,
    },
    {
      started_at: "2026-09-27T09:11:00+08:00",
      ended_at: "2026-09-27T09:20:00+08:00",
      session_id: "session-a",
      sequence: 1,
    },
  ];

  const coverage = journalLogic.recordingCoverageForDate(intervals, "2026-09-27");

  assert.equal(coverage.length, 2);
  assert.equal(coverage[0].startMinute, 540);
  assert.equal(coverage[0].endMinute, 550);
  assert.equal(coverage[1].startMinute, 551);
  assert.equal(coverage[1].endMinute, 560);
  assert.ok(coverage[0].endPercent < coverage[1].startPercent);
});

test("recording coverage clips a cross-midnight interval to each day", () => {
  const interval = {
    started_at: "2026-09-27T23:30:00+08:00",
    ended_at: "2026-09-28T00:30:00+08:00",
  };

  const firstDay = journalLogic.recordingCoverageForDate([interval], "2026-09-27");
  const secondDay = journalLogic.recordingCoverageForDate([interval], "2026-09-28");

  assert.equal(firstDay.length, 1);
  assert.equal(firstDay[0].startMinute, 1410);
  assert.equal(firstDay[0].endMinute, 1440);
  assert.equal(secondDay.length, 1);
  assert.equal(secondDay[0].startMinute, 0);
  assert.equal(secondDay[0].endMinute, 30);
  assert.deepEqual(
    journalLogic.recordingIntervalDates([interval]),
    ["2026-09-27", "2026-09-28"]
  );
});

test("recording manifest duration can provide an end when only start is present", () => {
  const coverage = journalLogic.recordingCoverageForDate([
    {
      started_at: "2026-09-27T12:00:00+08:00",
      duration_seconds: 90,
    },
  ], "2026-09-27");

  assert.equal(coverage.length, 1);
  assert.equal(coverage[0].startMinute, 720);
  assert.equal(coverage[0].endMinute, 721.5);
});

test("coverage summary uses the latest end even for overlapping recordings", () => {
  const coverage = journalLogic.recordingCoverageForDate([
    {
      started_at: "2026-09-27T09:00:00+08:00",
      ended_at: "2026-09-27T12:00:00+08:00",
    },
    {
      started_at: "2026-09-27T10:00:00+08:00",
      ended_at: "2026-09-27T10:10:00+08:00",
    },
  ], "2026-09-27");

  assert.deepEqual(journalLogic.recordingCoverageSummary(coverage), {
    startLabel: "09:00",
    endLabel: "12:00",
    intervalCount: 2,
  });
});

test("unavailable Watch coverage is not presented as no recording", () => {
  assert.equal(
    journalLogic.recordingCoverageStatus(false, 0),
    "录音覆盖数据暂不可用"
  );
  assert.equal(
    journalLogic.recordingCoverageStatus(true, 0),
    "没有可验证的录音时间段"
  );
});

test("a failed coverage refresh retains last-known audio-only dates as stale", () => {
  const lastKnown = [{
    session_id: "audio-only-session",
    sequence: 0,
    start_at: "2026-09-27T09:00:00+08:00",
    end_at: "2026-09-27T09:01:00+08:00",
  }];

  const state = journalLogic.watchCoverageState(lastKnown, {
    available: false,
    intervals: [],
  });

  assert.equal(state.available, false);
  assert.deepEqual(state.intervals, lastKnown);
  assert.deepEqual(journalLogic.recordingIntervalDates(state.intervals), ["2026-09-27"]);
});

test("recording intervals without reliable absolute times are omitted", () => {
  const coverage = journalLogic.recordingCoverageForDate([
    {duration_seconds: 60},
    {started_at: "not-a-timestamp", duration_seconds: 60},
    {
      started_at: "2026-09-27T12:00:00+08:00",
      ended_at: "2026-09-27T11:59:00+08:00",
    },
  ], "2026-09-27");

  assert.deepEqual(coverage, []);
  assert.deepEqual(journalLogic.recordingIntervalDates([]), []);
});

test("recording coverage forwards battery snapshot and includes battery range in summary", () => {
  const intervals = [
    {
      started_at: "2026-09-27T09:00:00+08:00",
      ended_at: "2026-09-27T09:01:00+08:00",
      battery_snapshot: {level_percent: 85, state: "unplugged"},
    },
    {
      started_at: "2026-09-27T09:02:00+08:00",
      ended_at: "2026-09-27T09:03:00+08:00",
      battery_snapshot: {level_percent: 84, state: "unplugged"},
    },
  ];

  const coverage = journalLogic.recordingCoverageForDate(intervals, "2026-09-27");
  assert.equal(coverage.length, 2);
  assert.deepEqual(coverage[0].battery_snapshot, {level_percent: 85, state: "unplugged"});
  assert.deepEqual(coverage[1].battery_snapshot, {level_percent: 84, state: "unplugged"});

  const summary = journalLogic.recordingCoverageSummary(coverage);
  assert.equal(summary.startBattery, 85);
  assert.equal(summary.endBattery, 84);
});

