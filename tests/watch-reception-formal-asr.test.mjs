import assert from "node:assert/strict";
import {readFile} from "node:fs/promises";
import test from "node:test";
import vm from "node:vm";

const source = await readFile(new URL("../web/app.js", import.meta.url), "utf8");
const renderStart = source.indexOf("function renderFormalWatchJob(job, sessionAsr = null) {");
const renderEnd = source.indexOf("\nasync function refreshWatchReception()", renderStart);
const escapeStart = source.indexOf("function escapeHtml(value) {");
const escapeEnd = source.indexOf("\nfunction highlightSearchText", escapeStart);
assert.ok(renderStart >= 0 && renderEnd > renderStart, "formal Watch ASR renderer should be extractable");

const context = {
  compactDurationLabel: (value) => `${Math.floor(Number(value))}ms`,
  stageLabel: (stage) => ({asr: "正式ASR", speaker: "声源归属", extraction: "事件提取"}[stage] || stage),
  viewerLink: (jobId, path) => `/viewer.html?job=${encodeURIComponent(jobId)}&path=${encodeURIComponent(path)}`,
};
vm.createContext(context);
vm.runInContext(
  `${source.slice(escapeStart, escapeEnd)}\n${source.slice(renderStart, renderEnd)}\nglobalThis.render = renderFormalWatchJob;`,
  context,
);

test("Watch reception uses formal ASR progress and never renders provisional previews", () => {
  const html = context.render(null, {
    status: "processing",
    counts: {completed: 1, no_speech: 1, processing: 1, pending: 1, failed: 0},
    transcript_count: 1,
    latest_transcript_rows: [{text: "formal sentence", start_offset_seconds: 2, end_offset_seconds: 4}],
  });

  assert.match(html.label, /正式ASR处理中 · 2\/4 段已完成/);
  assert.match(html.details, /正式ASR分段 2\/4/);
  assert.match(html.details, /已生成 1 句/);
  assert.match(html.transcript, /正式转写句子/);
  assert.match(html.transcript, /formal sentence/);
  assert.doesNotMatch(source.slice(renderStart, renderEnd), /临时预览|processing_segments/i);
});

test("formal ASR retry card shows failed segment count and next retry delay", () => {
  const html = context.render(null, {
    status: "processing",
    counts: {completed: 1, no_speech: 0, processing: 0, pending: 0, failed: 1},
    transcript_count: 0,
    automatic_retry: {
      failed_segments: 1,
      waiting_segments: 1,
      next_retry_in_seconds: 47,
    },
  });

  assert.equal(html.label, "正式ASR有 1 段失败 · 已安排自动重试");
  assert.match(html.details, /下次重试不足 1 分钟/);
});

test("due formal ASR retries are not shown with a future delay", () => {
  const html = context.render(null, {
    status: "processing",
    counts: {completed: 0, no_speech: 0, processing: 0, pending: 0, failed: 1},
    transcript_count: 0,
    automatic_retry: {
      failed_segments: 1,
      waiting_segments: 1,
      next_retry_in_seconds: 0,
    },
  });

  assert.equal(html.label, "正式ASR有 1 段失败 · 已安排自动重试");
  assert.match(html.details, /下次重试即将执行/);
  assert.doesNotMatch(html.details, /分钟后/);
});

test("due retries with no future backoff continue through background scans", () => {
  const html = context.render(null, {
    status: "processing",
    counts: {completed: 0, no_speech: 0, processing: 0, pending: 0, failed: 1},
    transcript_count: 0,
    automatic_retry: {
      failed_segments: 1,
      waiting_segments: 0,
      next_retry_in_seconds: null,
    },
  });

  assert.equal(html.label, "正式ASR有 1 段失败 · 等待后台继续处理");
  assert.match(html.details, /后台后续扫描会继续尝试重试/);
  assert.doesNotMatch(html.details, /后会继续处理|分钟后/);
});

test("reserved formal job shows segment retry instead of claiming ASR has not started", () => {
  const html = context.render(
    {status: "draft", stage: "waiting_for_files", job_id: "job-reserved-1234"},
    {
      status: "processing",
      counts: {completed: 0, no_speech: 0, processing: 0, pending: 0, failed: 1},
      transcript_count: 0,
      automatic_retry: {
        failed_segments: 1,
        waiting_segments: 1,
        next_retry_in_seconds: 125,
      },
    },
  );

  assert.equal(html.label, "正式ASR有 1 段失败 · 已安排自动重试");
  assert.match(html.details, /下次重试约 3 分钟后/);
  assert.doesNotMatch(html.label, /等待音频进入ASR/);
});

test("reserved draft formal job truthfully waits for audio before ASR", () => {
  const html = context.render({status: "draft", stage: "waiting_for_files", job_id: "job-reserved-1234"});

  assert.equal(html.label, "正式任务已建立 · 等待音频进入ASR");
  assert.doesNotMatch(html.label, /ASR已完成|转写完成/);
  assert.match(source, /const formalJobId = item\.job_id \|\| item\.completion_job_id/);
  assert.match(source, /data-watch-job-id=\"\$\{escapeHtml\(formalJobId\)\}\"/);
});

test("reserved task remains visibly unavailable when its Job cannot be read", () => {
  const html = context.render({status: "unavailable"});

  assert.equal(html.label, "整场任务状态暂不可用");
  assert.doesNotMatch(html.label, /尚未建立正式转写任务/);
  assert.match(source, /item\.formal_job \|\| \(formalJobId \? \{status: "unavailable"\} : null\)/);
  assert.match(source, /data-watch-job-id=\"\$\{escapeHtml\(formalJobId\)\}\"/);
});

test("linked whole-session jobs keep the formal ASR sentence trail visible", () => {
  const html = context.render({status: "running", stage: "speaker"}, {
    status: "completed",
    counts: {completed: 4, no_speech: 1, processing: 0, pending: 0, failed: 0},
    transcript_count: 5,
    latest_transcript_rows: [{text: "last persisted sentence", start_offset_seconds: 12, end_offset_seconds: 15}],
  });

  assert.equal(html.label, "整场处理中");
  assert.match(html.details, /声源归属/);
  assert.match(html.details, /正式ASR分段 5\/5/);
  assert.match(html.details, /已生成 5 句/);
  assert.match(html.transcript, /last persisted sentence/);
});

test("whole-session completion is not mislabeled as ASR-only completion", () => {
  const html = context.render({status: "completed", stage: "completed"}, {
    status: "completed",
    counts: {completed: 2, no_speech: 0, processing: 0, pending: 0, failed: 0},
    transcript_count: 2,
    latest_transcript_rows: [{text: "stored sentence", start_offset_seconds: 0, end_offset_seconds: 1}],
  });

  assert.equal(html.label, "整场处理已完成");
  assert.match(html.details, /正式ASR分段 2\/2/);
  assert.match(html.transcript, /stored sentence/);
});

test("completed Watch job links directly to its full formal transcript artifact", () => {
  const html = context.render({
    job_id: "job-watch-1",
    status: "completed",
    transcript_artifact_path: "files/abc/result/2026-10-06/transcript.json",
  });

  assert.match(html.fullTranscriptLink, /查看完整ASR转写/);
  assert.match(html.fullTranscriptLink, /job=job-watch-1&amp;path=files%2Fabc%2Fresult%2F2026-10-06%2Ftranscript.json/);
  assert.match(source, /\$\{job\.fullTranscriptLink \|\| ""\}/);
  assert.match(source, /\$\{taskLink\}/);
});

test("Watch transcript link escapes its href and stays absent without an artifact", () => {
  const linked = context.render({
    job_id: `job-\"<img>`,
    transcript_artifact_path: `files/abc/result/\"<img>/transcript.json`,
  });
  assert.match(linked.fullTranscriptLink, /%3Cimg%3E/);
  assert.doesNotMatch(linked.fullTranscriptLink, /<img/);
  assert.equal(context.render({job_id: "job-watch-2"}).fullTranscriptLink, "");
});

test("completed no-speech job explains that audio was kept and downstream work skipped", () => {
  const html = context.render({
    status: "completed",
    stage: "completed",
    no_speech_count: 1,
    no_speech_reason: "ASR 已完成且无语音；原音频已保留，跳过声源归属与事件提取。",
  });

  assert.equal(html.label, "整场处理已完成 · 1 个文件无语音");
  assert.match(html.details, /原音频已保留/);
  assert.match(html.details, /跳过声源归属与事件提取/);
});

test("completed whole-session job surfaces degraded event extraction", () => {
  const html = context.render({
    status: "completed",
    stage: "completed",
    context_extraction: {status: "degraded", laya_status: "degraded"},
  });

  assert.equal(html.label, "整场处理已完成 · 事件提取部分完成");
  assert.match(html.details, /事件提取未覆盖全部内容/);
});

test("failed event extraction is visible after a completed whole-session job", () => {
  const html = context.render({
    status: "completed",
    stage: "completed",
    context_extraction: {status: "failed"},
  });

  assert.equal(html.label, "整场处理已完成 · 事件提取失败");
  assert.match(html.details, /事件提取失败/);
});

test("Watch task card shows bounded contextual extraction auto-recovery", () => {
  const waiting = context.renderFormalWatchJob({
    job_id: "job-watch-auto-context",
    status: "completed",
    context_extraction: {status: "failed"},
    context_auto_retry: {status: "scheduled", attempts: 0, max_attempts: 1},
  });
  assert.equal(waiting.label, "整场处理已完成 · 事件提取等待自动恢复");
  assert.match(waiting.details, /已安排后台自动恢复/);

  const running = context.renderFormalWatchJob({
    job_id: "job-watch-auto-context",
    status: "completed",
    context_extraction: {status: "failed"},
    context_auto_retry: {status: "running", attempts: 1, max_attempts: 1},
  });
  assert.equal(running.label, "整场处理已完成 · 事件提取自动恢复中");
  assert.match(running.details, /成功批次会复用/);
});

test("formal Watch transcript text is escaped before it reaches the page", () => {
  const html = context.render(null, {
    status: "completed",
    counts: {completed: 1, no_speech: 0, processing: 0, pending: 0, failed: 0},
    transcript_count: 1,
    latest_transcript_rows: [{text: `<img src=x onerror=alert(1)>`, start_offset_seconds: 0, end_offset_seconds: 1}],
  });

  assert.match(html.transcript, /&lt;img src=x onerror=alert\(1\)&gt;/);
  assert.doesNotMatch(html.transcript, /<img/);
});

const contextStart = source.indexOf("function contextExtractionPaths(job) {");
const contextEnd = source.indexOf("\nfunction timeSourceLabel", contextStart);
assert.ok(contextStart >= 0 && contextEnd > contextStart, "context recovery renderer should be extractable");
const contextUi = {
  document: {querySelectorAll: () => []},
  api: async () => ({
    status: "degraded",
    counts: {formal_events: 1, pending_events: 0, pending_sources: 2},
    laya: {status: "degraded"},
    batches: [{status: "completed"}, {status: "failed"}],
  }),
  resultLink: (_job, path) => `/artifact?path=${encodeURIComponent(path)}`,
  viewerLink: (_job, path) => `/viewer?path=${encodeURIComponent(path)}`,
  $: () => null,
  renderJob: () => {},
  beginPolling: () => {},
};
vm.createContext(contextUi);
vm.runInContext(
  `${source.slice(escapeStart, escapeEnd)}\n${source.slice(contextStart, contextEnd)}\nglobalThis.renderRecoveryStatus = contextRecoveryStatusMarkup; globalThis.renderAutoRetryStatus = contextAutoRetryStatusMarkup; globalThis.loadContextSummary = loadContextExtractionSummaries;`,
  contextUi,
);

const retryStart = source.indexOf("async function retryContextExtraction(button) {");
const retryEnd = source.indexOf("\nfunction setJobsLiveStatus", retryStart);
assert.ok(retryStart >= 0 && retryEnd > retryStart, "context retry action should be extractable");
vm.runInContext(
  `${source.slice(retryStart, retryEnd)}\nglobalThis.retryContext = retryContextExtraction;`,
  contextUi,
);

test("degraded context extraction offers same-job downstream recovery and archived results", async () => {
  const body = {innerHTML: "", textContent: ""};
  const retryButton = {
    dataset: {contextRecoveryJob: "job-1"},
    disabled: false,
    setAttribute() {},
    addEventListener(eventName, handler) {
      this.eventName = eventName;
      this.handler = handler;
    },
  };
  body.querySelector = () => retryButton;
  const panel = {
    dataset: {contextJob: "job-1"},
    isConnected: true,
    querySelector: () => body,
  };
  contextUi.document.querySelectorAll = () => [panel];
  await contextUi.loadContextSummary({
    job_id: "job-1",
    results: ["files/abc/result/context-extraction.json"],
    context_recovery: {
      status: "completed",
      extraction_status: "degraded",
      target_files: 1,
      completed_files: 1,
      archived_artifacts: ["recovery/context-extraction/r1/abc/result/context-extraction.json"],
    },
  });

  assert.match(body.innerHTML, /重试事件提取/);
  assert.match(body.innerHTML, /复用ASR和声源归属结果/);
  assert.match(body.innerHTML, /重试前的事件提取记录/);
  assert.match(body.innerHTML, /仍有内容待确认/);
  assert.equal(retryButton.eventName, "click");
  assert.match(body.innerHTML, /data-context-recovery-job="job-1"/);
  assert.doesNotMatch(body.innerHTML, /onclick=/);
});

test("active context recovery disables the rendered retry control", async () => {
  const body = {innerHTML: "", textContent: "", querySelector: () => null};
  const panel = {
    dataset: {contextJob: "job-2"},
    isConnected: true,
    querySelector: () => body,
  };
  contextUi.document.querySelectorAll = () => [panel];
  await contextUi.loadContextSummary({
    job_id: "job-2",
    results: ["files/def/result/context-extraction.json"],
    context_recovery: {status: "running"},
  });
  assert.match(body.innerHTML, /context-recovery-button[^>]*disabled aria-busy="true"/);
});

test("scheduled context auto-retry is explained and disables manual duplicate", async () => {
  const body = {innerHTML: "", textContent: ""};
  const retryButton = {disabled: false, setAttribute() {}, addEventListener() {}};
  body.querySelector = () => retryButton;
  const panel = {
    dataset: {contextJob: "job-auto-scheduled"},
    isConnected: true,
    querySelector: () => body,
  };
  contextUi.document.querySelectorAll = () => [panel];
  await contextUi.loadContextSummary({
    job_id: "job-auto-scheduled",
    results: ["files/abc/result/context-extraction.json"],
    context_auto_retry: {status: "scheduled", attempts: 0, max_attempts: 1},
  });
  assert.match(body.innerHTML, /后台将在短暂等待后自动恢复/);
  assert.match(body.innerHTML, /context-recovery-button[^>]*disabled aria-busy="true"/);
});

test("automatic retry terminal statuses have clear recovery guidance", () => {
  assert.match(
    contextUi.renderAutoRetryStatus({context_auto_retry: {status: "completed", attempts: 1, max_attempts: 1}}),
    /自动恢复完成/,
  );
  assert.match(
    contextUi.renderAutoRetryStatus({context_auto_retry: {status: "needs_review", attempts: 1, max_attempts: 1}}),
    /仍有内容需要确认/,
  );
  assert.match(
    contextUi.renderAutoRetryStatus({context_auto_retry: {status: "stopped", attempts: 1, max_attempts: 1}}),
    /自动恢复已停止/,
  );
});

test("context recovery request starts polling and keeps the button busy", async () => {
  const calls = [];
  const button = {
    dataset: {contextRecoveryJob: "job-retry"},
    disabled: false,
    attributes: new Map(),
    setAttribute(name, value) { this.attributes.set(name, value); },
    removeAttribute(name) { this.attributes.delete(name); },
  };
  contextUi.api = async (path, options) => {
    calls.push([path, options]);
    return {job_id: "job-retry", status: "completed"};
  };
  let rendered = null;
  let polled = 0;
  contextUi.renderJob = (job) => { rendered = job; };
  contextUi.beginPolling = () => { polled += 1; };

  await contextUi.retryContext(button);

  assert.equal(calls[0][0], "/api/jobs/job-retry/retry-context-extraction");
  assert.equal(calls[0][1].method, "POST");
  assert.equal(rendered.job_id, "job-retry");
  assert.equal(polled, 1);
  assert.equal(button.disabled, true);
  assert.equal(button.attributes.get("aria-busy"), "true");
});

test("failed context recovery request re-enables its button", async () => {
  const button = {
    dataset: {contextRecoveryJob: "job-failed"},
    disabled: false,
    attributes: new Map(),
    setAttribute(name, value) { this.attributes.set(name, value); },
    removeAttribute(name) { this.attributes.delete(name); },
  };
  contextUi.api = async () => { throw new Error("offline"); };

  await contextUi.retryContext(button);

  assert.equal(button.disabled, false);
  assert.equal(button.attributes.has("aria-busy"), false);
});

test("active context recovery disables duplicate action and reports batch progress", () => {
  const html = contextUi.renderRecoveryStatus({
    context_recovery: {
      status: "running",
      target_files: 2,
      completed_files: 1,
      batch_progress: {completed: 3, total: 5},
    },
  });
  assert.match(html, /正在重试事件提取/);
  assert.match(html, /录音 1\/2/);
  assert.match(html, /提取批次 3\/5/);
});

test("context recovery error text is escaped before rendering", () => {
  const html = contextUi.renderRecoveryStatus({
    context_recovery: {status: "failed", error: "<img src=x onerror=alert(1)>"},
  });
  assert.match(html, /&lt;img src=x onerror=alert\(1\)&gt;/);
  assert.doesNotMatch(html, /<img/);
});
