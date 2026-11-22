import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";
import test from "node:test";

// 提取 web/app.js 中「日记产物加载」这一整段（pathForResultSibling →
// loadJournalEntries），在 vm 里用桩化的 api 真跑一遍 —— 验证：
//  1. partial / failed 的 job 只要磁盘有产物就能加载（不再被 status 拦掉）；
//  2. results 数组漏记（空数组 / 裸日期）时按产物目录扫描（/artifacts）补上；
//  3. 确实无产物的 job 依旧跳过，且不会白发扫描请求；
//  4. results 里没列出的兄弟产物（events.json / unknown-review.json / ...）
//     不再被静默丢弃。
const appSource = fs.readFileSync(
  process.env.LIFE_LOGGER_APP_JS
    ? new URL(`file://${process.env.LIFE_LOGGER_APP_JS}`)
    : new URL("../web/app.js", import.meta.url),
  "utf8",
);
const functionStart = appSource.indexOf("function pathForResultSibling(");
const functionEnd = appSource.indexOf("async function loadWatchLiveJournalEntries(");
assert.notEqual(functionStart, -1, "pathForResultSibling should exist");
assert.notEqual(functionEnd, -1, "loadJournalEntries extraction boundary should exist");
const slice = appSource.slice(functionStart, functionEnd);

function extractFunction(source, name) {
  const start = source.indexOf(`function ${name}(`);
  assert.notEqual(start, -1, `${name} should exist`);
  let depth = 0;
  for (let index = source.indexOf("{", start); index < source.length; index += 1) {
    if (source[index] === "{") depth += 1;
    else if (source[index] === "}") {
      depth -= 1;
      if (depth === 0) return source.slice(start, index + 1);
    }
  }
  throw new Error(`unclosed function ${name}`);
}

// artifactDate / artifactSourceFile / resultLink 定义在文件更后面（或更前面），但
// loadJournalEntries 会用到，直接取生产代码原文一起跑（不复制逻辑，避免测试与实现漂移）。
const companionSource = [
  extractFunction(appSource, "resultLink"),
  extractFunction(appSource, "artifactDate"),
  extractFunction(appSource, "artifactSourceFile"),
].join("\n");

function artifactUrl(jobId, kind, artifactPath = "") {
  const suffix = artifactPath ? `?path=${encodeURIComponent(artifactPath)}` : "";
  return `/api/jobs/${encodeURIComponent(jobId)}/${kind}${suffix}`;
}

function createContext(table, calls) {
  const context = {
    api: async (path) => {
      calls.push(path);
      if (Object.prototype.hasOwnProperty.call(table, path)) return table[path];
      const error = new Error(`stub missing: ${path}`);
      error.status = 404;
      throw error;
    },
    selectedJournalDate: "2026-10-09",
    journalSwitchActive: () => false,
    setTimeout,
    clearTimeout,
    setInterval,
    clearInterval,
    jobHistoryLocalDate: () => "2026-10-09",
    journalArtifactScanCache: new Map(),
    journalDays: [],
    journalRelativeAbsoluteTimestamp: () => null,
    journalEntryEvidenceDates: ({artifactDate}) => (artifactDate ? [artifactDate] : []),
    journalReviewCandidateVisible: () => true,
    journalSourceFileLocalDate: () => null,
    journalSegmentLocalDate: (item, fallbackDate) => fallbackDate || "",
    journalReviewCandidateKey: (kind, item) => `${kind}:${item?.segment_id || ""}`,
    dedupeWorkspaceEvents: (events) => events || [],
    isMeaningfulWorkspaceEvent: () => true,
    eventCategories: (event) => event?.categories || [],
    eventSeconds: () => null,
    workspaceDuplicateWindowSeconds: () => 900,
    areSemanticEventDuplicates: () => false,
    loadWatchLiveJournalEntries: async () => [],
    // loadJournalEntries 在补扫/读取失败时会调用这个 UI 提示函数；
    // 测试只关心加载结果，用最小桩避免 VM 里 ReferenceError。
    showSyncToast: () => {},
  };
  vm.createContext(context);
  vm.runInContext(slice, context);
  vm.runInContext(companionSource, context);
  return context;
}

test("partial job with produced artifacts is loaded instead of hidden", async () => {
  const jobId = "job-partial";
  const fileId = "f".repeat(64);
  const summaryPath = `files/${fileId}/result/2026-10-09/daily_summary.json`;
  const eventsPath = `files/${fileId}/result/2026-10-09/events.json`;
  const reviewPath = `files/${fileId}/result/unknown-review.json`;
  const selfPath = `files/${fileId}/result/self-transcript.json`;
  const table = {
    [artifactUrl(jobId, "artifact", summaryPath)]: {date: "2026-10-09", timeline: []},
    [artifactUrl(jobId, "artifact", eventsPath)]: [
      {event_id: "e1", title: "买了一瓶水", categories: ["expense"]},
      {event_id: "e2", title: "开了一个会", categories: ["work_communication"]},
    ],
    [artifactUrl(jobId, "artifact", reviewPath)]: [{segment_id: "s1"}],
    [artifactUrl(jobId, "artifact", selfPath)]: [{segment_id: "s1", text: "我"}],
    [artifactUrl(jobId, "manual-events", eventsPath)]: {events: []},
    [artifactUrl(jobId, "event-corrections", eventsPath)]: {corrections: []},
    [artifactUrl(jobId, "reviews", reviewPath)]: {reviews: []},
  };
  const calls = [];
  const context = createContext(table, calls);
  const jobs = [{
    job_id: jobId,
    status: "partial",
    results: [summaryPath, eventsPath, reviewPath, selfPath],
    files: [{file_id: fileId, effective_start_at: "2026-10-09T10:00:00+08:00"}],
  }];

  const entries = await context.loadJournalEntries(jobs);

  assert.equal(entries.length, 1, "partial job 有产物就必须出条目");
  assert.equal(entries[0].date, "2026-10-09");
  assert.equal(entries[0].events.length, 2);
  assert.equal(entries[0].eventsPath, eventsPath);
  assert.equal(entries[0].reviewCount, 1);
  assert.equal(entries[0].selfCount, 1);
  assert.ok(!calls.some((path) => path.endsWith("/artifacts")), "results 够用时不该扫描");
});

test("job whose results are empty is rescued by scanning the artifact directory", async () => {
  const jobId = "job-scanned";
  const fileId = "a".repeat(64);
  const summaryPath = `files/${fileId}/result/2026-10-09/daily_summary.json`;
  const eventsPath = `files/${fileId}/result/2026-10-09/events.json`;
  const recordingsPath = `files/${fileId}/asr/session-1/recordings.json`;
  const table = {
    [artifactUrl(jobId, "artifacts")]: {
      job_id: jobId,
      artifacts: [summaryPath, eventsPath, recordingsPath],
    },
    [artifactUrl(jobId, "artifact", summaryPath)]: {date: "2026-10-09", timeline: []},
    [artifactUrl(jobId, "artifact", eventsPath)]: [
      {event_id: "e1", title: "事件一", categories: ["routine"]},
      {event_id: "e2", title: "事件二", categories: ["routine"]},
      {event_id: "e3", title: "事件三", categories: ["routine"]},
    ],
    [artifactUrl(jobId, "artifact", recordingsPath)]: {
      recordings: [{
        recording_id: "r1",
        started_at: "2026-10-09T21:00:00+08:00",
        ended_at: "2026-10-09T21:10:00+08:00",
        duration_seconds: 600,
      }],
    },
  };
  const calls = [];
  const context = createContext(table, calls);
  const jobs = [{
    job_id: jobId,
    // 历史数据里真实出现过：results 为空数组，磁盘上却啥都有
    status: "partial",
    results: [],
    files: [{file_id: fileId, status: "failed", effective_start_at: "2026-10-09T21:00:00+08:00"}],
  }];

  const entries = await context.loadJournalEntries(jobs);

  assert.ok(calls.includes(artifactUrl(jobId, "artifacts")), "应当扫描产物目录");
  assert.equal(entries.length, 1);
  assert.equal(entries[0].events.length, 3);
  assert.equal(entries[0].eventsPath, eventsPath);
  assert.equal(entries[0].recordingIntervals.length, 1);
  assert.equal(entries[0].recordingIntervals[0].recording_id, "r1");
});

test("jobs with genuinely no artifacts are still skipped without scanning", async () => {
  const calls = [];
  const context = createContext({}, calls);
  const jobs = [
    {
      job_id: "job-no-artifacts",
      status: "failed",
      results: [],
      files: [{file_id: "b".repeat(64), status: "failed"}],
    },
    {job_id: "job-no-files", status: "draft", results: [], files: []},
  ];

  const entries = await context.loadJournalEntries(jobs);

  assert.equal(entries.length, 0);
  assert.equal(calls.filter((path) => path === artifactUrl("job-no-artifacts", "artifacts")).length, 1);
  assert.ok(
    !calls.some((path) => path.includes("job-no-files")),
    "连输入文件都没有的 job 不该发扫描请求",
  );
});

test("sibling artifacts missing from results are still fetched", async () => {
  const jobId = "job-siblings";
  const fileId = "c".repeat(64);
  const summaryPath = `files/${fileId}/result/2026-10-09/daily_summary.json`;
  const eventsPath = `files/${fileId}/result/2026-10-09/events.json`;
  const reviewPath = `files/${fileId}/result/unknown-review.json`;
  const selfPath = `files/${fileId}/result/self-transcript.json`;
  const table = {
    [artifactUrl(jobId, "artifact", summaryPath)]: {date: "2026-10-09", timeline: []},
    [artifactUrl(jobId, "artifact", eventsPath)]: [
      {event_id: "e1", title: "唯一事件", categories: ["routine"]},
    ],
    // 这两个都没被 results 列出，但磁盘上真实存在
    [artifactUrl(jobId, "artifact", reviewPath)]: [{segment_id: "s1"}],
    [artifactUrl(jobId, "artifact", selfPath)]: [{segment_id: "s1", text: "我"}],
    [artifactUrl(jobId, "manual-events", eventsPath)]: {events: []},
    [artifactUrl(jobId, "event-corrections", eventsPath)]: {corrections: []},
  };
  const calls = [];
  const context = createContext(table, calls);
  const jobs = [{
    job_id: jobId,
    status: "completed",
    // 旧逻辑只认 results 白名单，这两个兄弟产物会被静默丢掉
    results: [summaryPath, eventsPath],
    files: [{file_id: fileId}],
  }];

  const entries = await context.loadJournalEntries(jobs);

  assert.equal(entries.length, 1);
  assert.equal(entries[0].reviewCount, 1);
  assert.equal(entries[0].selfCount, 1);
  assert.ok(calls.includes(artifactUrl(jobId, "artifact", reviewPath)));
});

test("listed summary that does not exist on disk yields no entry", async () => {
  const jobId = "job-ghost";
  const summaryPath = `files/${"d".repeat(64)}/result/2026-10-09/daily_summary.json`;
  const calls = [];
  const context = createContext({}, calls);

  const entries = await context.loadJournalEntries([{
    job_id: jobId,
    status: "completed",
    results: [summaryPath],
    files: [{file_id: "d".repeat(64)}],
  }]);

  assert.equal(entries.length, 0);
});
