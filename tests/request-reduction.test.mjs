import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import {createRequire} from 'node:module';
import test from 'node:test';
const require = createRequire(import.meta.url);
const {createTimelineCache} = require('../web/timeline-data.js');
const source = name => process.env.REQUEST_REDUCTION_WEB
  ? `${process.env.REQUEST_REDUCTION_WEB}/${name}` : new URL(`../web/${name}`, import.meta.url);
const app = fs.readFileSync(source('app.js'), 'utf8');
const waterfall = fs.readFileSync(source('waterfall-v2.html'), 'utf8');
const tick = () => new Promise(resolve => setImmediate(resolve));

function apiHarness() {
  // Execute the actual request entry and retry loop, replacing only the transport.
  const start = app.indexOf('const apiGetInflight');
  const end = app.indexOf('async function apiOnce(');
  assert.ok(start >= 0 && end > start, 'GET entry must own an in-flight registry');
  let calls = 0, release, fail;
  const cache = createTimelineCache();
  const context = {Map, Promise, Headers, AbortController, DOMException, URL, accessToken: 'test',
    API_READ_RETRY_DELAYS_MS: [], isRetryableApiError: () => false, reportError: () => {},
    apiOnce: () => { calls++; return new Promise((r, reject) => { release = r; fail = reject; }); },
    LifeTimelineData: cache,
    sharedTimelineRead: (path, loader, options) => cache.read(path, loader, options),
  };
  vm.createContext(context);
  vm.runInContext(app.slice(start, end) + '\nglobalThis.callApi = api;', context);
  return {api: context.callApi, calls: () => calls, release: value => release(value), fail: error => fail(error)};
}

test('concurrent jobs GETs use one transport; settled GETs can be refreshed', async () => {
  const h = apiHarness();
  const a = h.api('/api/jobs'), b = h.api('/api/jobs');
  await tick(); assert.equal(h.calls(), 1);
  h.release({jobs: [1]}); assert.deepEqual(await a, await b);
  const c = h.api('/api/jobs', {}, {force: true});
  await tick(); assert.equal(h.calls(), 2); h.release({jobs: [2]}); await c;
});

test('one canceled subscriber cannot abort the shared GET for another subscriber', async () => {
  const h = apiHarness(), controller = new AbortController();
  const first = h.api('/api/jobs', {signal: controller.signal});
  const second = h.api('/api/jobs');
  controller.abort();
  await assert.rejects(first, e => e.name === 'AbortError');
  await tick(); assert.equal(h.calls(), 1);
  h.release({jobs: []}); await second;
});

test('already canceled reads do not start a network request', async () => {
  const h = apiHarness(), controller = new AbortController();
  controller.abort();
  await assert.rejects(h.api('/api/jobs', {signal: controller.signal}), e => e.name === 'AbortError');
  await tick(); assert.equal(h.calls(), 0);
});

test('failed GET is evicted and a later read can recover', async () => {
  const h = apiHarness();
  const first = h.api('/api/jobs');
  await tick(); h.fail(new Error('offline'));
  await assert.rejects(first, /offline/);
  const next = h.api('/api/jobs');
  await tick(); assert.equal(h.calls(), 2); h.release({jobs: []}); await next;
});

test('date-independent jobs reuse their snapshot until a write or explicit refresh', async () => {
  const h = apiHarness();
  const first = h.api('/api/jobs');
  await tick(); h.release({jobs: [1]}); await first;
  assert.equal((await h.api('/api/jobs')).jobs[0], 1);
  assert.equal(h.calls(), 1);
  const write = h.api('/api/jobs', {method: 'POST'});
  await tick(); h.release({ok: true}); await write;
  const next = h.api('/api/jobs');
  await tick(); assert.equal(h.calls(), 3); h.release({jobs: [2]});
  assert.equal((await next).jobs[0], 2);
});

test('POST mutations are never coalesced', async () => {
  const h = apiHarness();
  h.api('/api/jobs', {method: 'POST'}); h.api('/api/jobs', {method: 'POST'});
  await tick(); assert.equal(h.calls(), 2);
});

function waterfallReadHarness(cache, transport) {
  const start = waterfall.indexOf('function getJSON(');
  const end = waterfall.indexOf('function postToParent(');
  assert.ok(start >= 0 && end > start, 'waterfall must expose the shared cache read');
  const context = {LifeTimelineData: cache, fetch: transport, authHeaders: () => ({}),
    shToday: () => '2026-11-05', URL, Date, Promise};
  vm.createContext(context);
  vm.runInContext(waterfall.slice(start, end) + '\nglobalThis.read = getJSON;', context);
  return context.read;
}

test('parent/iframe share one pending activity read and preserve the complete payload', async () => {
  const cache = createTimelineCache(); let release, calls = 0;
  const payload = {date: '2026-11-02', activities: [{activity_id: 'a'}], unassociated_items: [{item_id: 'u'}]};
  const parent = cache.read('/api/activity-timeline?date=2026-11-02', () => {calls++; return new Promise(r => {release = r;});});
  const read = waterfallReadHarness(cache, async () => {calls++; throw Error('duplicate');});
  const child = read('/api/activity-timeline?date=2026-11-02');
  await tick(); assert.equal(calls, 1); release(payload);
  assert.equal(await child, payload); assert.equal(await parent, payload);
});

test('date-independent coverage is reused across dates; daily requests remain date-scoped', async () => {
  const cache = createTimelineCache(); let calls = 0;
  const read = waterfallReadHarness(cache, async path => {calls++; return {ok: true, status: 200, json: async () => ({path})};});
  await read('/api/watch-coverage');
  await read('/api/activity-timeline?date=2026-11-02');
  await read('/api/watch-coverage');
  await read('/api/activity-timeline?date=2026-11-03');
  assert.equal(calls, 3);
});

test('embedded waterfall loads the parent-compatible timeline cache before its own script', () => {
  assert.ok(waterfall.indexOf('src="/timeline-data.js"') >= 0);
  assert.ok(waterfall.indexOf('src="/timeline-data.js"') < waterfall.indexOf('function getJSON('));
});
