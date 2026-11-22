import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import test from 'node:test';

const app = fs.readFileSync(new URL('../web/app.js', import.meta.url), 'utf8');

function takeFn(name) {
  const start = app.indexOf(`function ${name}(`);
  assert.notEqual(start, -1, `missing ${name}`);
  let depth = 0;
  for (let i = app.indexOf('{', start); i < app.length; i++) {
    if (app[i] === '{') depth++;
    else if (app[i] === '}' && --depth === 0) return app.slice(start, i + 1);
  }
  throw new Error('unclosed ' + name);
}

const ctx = { result: null, Set, Map, String, Array, Math, JSON, Object };
vm.createContext(ctx);
vm.runInContext(`
  const RESULT_DEFINITIONS = { "daily_summary.json": { title: "当日总结与时间轴", badge: "主结果", description: "总结", order: 1 } };
  function escapeHtml(s) { return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }
  function artifactFileName(p) { return String(p).split("/").pop(); }
  function artifactSourceFile() { return null; }
  function artifactDate() { return ""; }
  function viewerLink(jobId, p) { return "/viewer.html?job=" + encodeURIComponent(jobId) + "&path=" + encodeURIComponent(p); }
  function resultLink(jobId, p) { return "/api/jobs/" + encodeURIComponent(jobId) + "/artifact?path=" + encodeURIComponent(p); }
  ${takeFn('renderResults')}
  result = { renderResults };
`, ctx);
const { renderResults } = ctx.result;

const DEGRADED = { job_id: 'job-20261009-231933-50b43361', status: 'degraded', results: [] };

test('degraded results=[] 但磁盘有产物 → 渲染产物入口（复用 viewer 链接）', () => {
  const html = renderResults(DEGRADED, ['result/2026-10-09/daily_summary.json', 'asr/checkpoint.json']);
  assert.match(html, /任务产物（2）/);
  assert.match(html, /\/viewer\.html\?job=job-20261009-231933-50b43361/);
  assert.match(html, /daily_summary\.json/);
  assert.doesNotMatch(html, /结果会在处理完成后出现在这里/);
});

test('产物入口数量等于实际存在的产物，不虚报文件数', () => {
  const html = renderResults(DEGRADED, ['a/1.json', 'a/2.json', 'a/3.json']);
  assert.equal((html.match(/<a /g) || []).length, 3);
  assert.match(html, /全部产物（3）/);
});

test('既有正式结果优先：有 results 时不被磁盘产物覆盖', () => {
  const html = renderResults({ ...DEGRADED, results: ['result/2026-10-09/daily_summary.json'] },
    ['asr/checkpoint.json', 'asr/other.json']);
  assert.match(html, /处理结果/);
  assert.doesNotMatch(html, /任务产物（2）/);
  assert.match(html, /技术文件/);
});

test('没有任何产物 → 诚实空态，不伪装成功', () => {
  const html = renderResults(DEGRADED, []);
  assert.match(html, /结果会在处理完成后出现在这里/);
  assert.doesNotMatch(html, /任务产物/);
});

test('渲染后接扫描入口（复用 scanJobArtifactPaths，不新增 retry/模型调用）', () => {
  assert.match(app, /hydrateJobArtifactResults\(job\);/, 'renderJob 末尾需触发补入口');
  const fn = takeFn('hydrateJobArtifactResults');
  assert.match(fn, /scanJobArtifactPaths\(job\)/, '复用既有产物扫描');
  assert.doesNotMatch(fn, /restart|\/restart|model|reprocess/i, '不得触发重试/模型调用');
  assert.match(fn, /job\.job_id !== currentJobId/, '切走后不得写回旧任务');
});

test('无可查看产物时写明确空态而非空白（渲染函数内）', () => {
  assert.match(takeFn('renderJobArtifactSlot'), /这个任务没有可查看的产物/);
});

test('不把 degraded 当全部成功：不新增“已完成”字样', () => {
  const html = renderResults(DEGRADED, ['a/1.json']);
  assert.doesNotMatch(html, /已完成|全部成功/);
});

// ===== 重入 / 失败恢复（5cffa9d8 缺陷）=====
function loadHydrate(scanImpl) {
  const start = app.indexOf('const jobArtifactCache = new Map();');
  const end = app.indexOf('function jobContentSignature(job) {');
  assert.notEqual(start, -1); assert.notEqual(end, -1);
  const slot = { innerHTML: '', dataset: { jobId: '' } };
  const state = { currentJobId: '', scanCalls: 0, worker: null };
  const ctx = {
    result: null, Set, Map, String, Array, Math, JSON, Object, Promise,
    currentJobId: '',
    $: () => slot,
    scanJobArtifactPaths: (job) => { state.scanCalls++; return scanImpl(job, state); },
    renderResults: (job, arts) => `RESULTS:${job.job_id}:${arts.length}`,
    console,
  };
  vm.createContext(ctx);
  vm.runInContext(app.slice(start, end) + '\nresult = { hydrateJobArtifactResults, jobArtifactCache, jobArtifactInflight, renderJobArtifactSlot };', ctx);
  return { ctx, slot, state };
}
const job = (id = 'jobA', results = []) => ({ job_id: id, status: 'degraded', results });

test('重入：同一 job 重新渲染后立即用缓存恢复入口，不重复请求', async () => {
  let calls = 0;
  const { ctx, slot, state } = loadHydrate(async () => { calls++; return ['a/1.json']; });
  ctx.currentJobId = 'jobA'; slot.dataset.jobId = 'jobA';
  await ctx.result.hydrateJobArtifactResults(job());
  assert.match(slot.innerHTML, /RESULTS:jobA:1/);
  // 模拟切走再回来：DOM 重建（innerHTML 清空），缓存仍在
  slot.innerHTML = '<div class="result-empty">结果会在处理完成后出现在这里</div>';
  ctx.currentJobId = 'jobA'; slot.dataset.jobId = 'jobA';
  await ctx.result.hydrateJobArtifactResults(job());
  assert.match(slot.innerHTML, /RESULTS:jobA:1/, '重入应从缓存恢复入口');
  assert.equal(calls, 1, '不得重复请求 artifacts');
});

test('A→B→A：两个 job 各自缓存，互不覆盖', async () => {
  const byJob = { jobA: ['a/1.json'], jobB: ['b/1.json', 'b/2.json'] };
  const { ctx, slot } = loadHydrate(async (j) => byJob[j.job_id]);
  for (const id of ['jobA', 'jobB', 'jobA']) {
    ctx.currentJobId = id; slot.dataset.jobId = id; slot.innerHTML = '';
    await ctx.result.hydrateJobArtifactResults(job(id));
  }
  assert.match(slot.innerHTML, /RESULTS:jobA:1/);
});

test('503 失败不当成“成功空数组”：显示失败提示，且下次可恢复', async () => {
  let fail = true;
  const { ctx, slot } = loadHydrate(async () => {
    if (fail) { const e = []; e.__failed = true; return e; }
    return ['a/1.json'];
  });
  ctx.currentJobId = 'jobA'; slot.dataset.jobId = 'jobA';
  await ctx.result.hydrateJobArtifactResults(job());
  assert.match(slot.innerHTML, /data-artifacts-source="error"/);
  assert.equal(ctx.result.jobArtifactCache.has('jobA'), false, '失败不得写入成功缓存');
  fail = false;
  slot.innerHTML = '';
  ctx.currentJobId = 'jobA'; slot.dataset.jobId = 'jobA';
  await ctx.result.hydrateJobArtifactResults(job());
  assert.match(slot.innerHTML, /RESULTS:jobA:1/, '恢复后重进应拿到入口');
});

test('成功空数组是“没有产物”而不是失败，且被缓存', async () => {
  const { ctx, slot } = loadHydrate(async () => []);
  ctx.currentJobId = 'jobA'; slot.dataset.jobId = 'jobA';
  await ctx.result.hydrateJobArtifactResults(job());
  assert.match(slot.innerHTML, /data-artifacts-source="none"/);
  assert.match(slot.innerHTML, /这个任务没有可查看的产物/);
  assert.equal(ctx.result.jobArtifactCache.has('jobA'), true);
});

test('迟到响应不串任务：A 的响应晚到时 B 的 slot 不被覆盖', async () => {
  let release;
  const gate = new Promise((r) => { release = r; });
  const { ctx, slot } = loadHydrate(async (j) => { if (j.job_id === 'jobA') await gate; return [j.job_id + '/1.json']; });
  ctx.currentJobId = 'jobA'; slot.dataset.jobId = 'jobA';
  const p = ctx.result.hydrateJobArtifactResults(job('jobA'));
  // 切到 B 并完成 B
  ctx.currentJobId = 'jobB'; slot.dataset.jobId = 'jobB'; slot.innerHTML = 'B-CONTENT';
  await ctx.result.hydrateJobArtifactResults(job('jobB'));
  release();
  await p;
  assert.match(slot.innerHTML, /RESULTS:jobB:1/, 'B 的 DOM 不得被 A 的迟到响应覆盖');
});

test('静态：slot 带 job id，渲染前校验归属', () => {
  assert.match(app, /id="job-results-slot" data-job-id="\$\{escapeHtml\(job\.job_id \|\| ""\)\}"/);
  assert.match(app, /still\.dataset\.jobId !== job\.job_id/, '写 DOM 前必须校验 slot 归属');
  assert.doesNotMatch(app, /hydratedArtifactJobId/, '旧的永久一次性守卫必须移除');
});
