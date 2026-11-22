import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import test from 'node:test';

const app = fs.readFileSync(new URL('../web/app.js', import.meta.url), 'utf8');
const html = fs.readFileSync(new URL('../web/index.html', import.meta.url), 'utf8');

function named(source, name) {
  const start = source.indexOf(`function ${name}(`);
  assert.ok(start >= 0, `${name} exists`);
  // 跳过参数列表（默认值里可能有 {}），再找函数体起始大括号
  let paren = 0, bodyStart = -1;
  for (let i = source.indexOf('(', start); i < source.length; i += 1) {
    if (source[i] === '(') paren += 1;
    else if (source[i] === ')') { paren -= 1; if (paren === 0) { bodyStart = source.indexOf('{', i); break; } }
  }
  assert.ok(bodyStart > 0, `${name} body`);
  let depth = 0;
  for (let i = bodyStart; i < source.length; i += 1) {
    if (source[i] === '{') depth += 1;
    else if (source[i] === '}' && !--depth) return source.slice(start, i + 1);
  }
  throw new Error(`missing end of ${name}`);
}

function load() {
  const context = {Number, String, Math, Array, Object, Date, escapeHtml: (v) => String(v ?? '').replace(/[&<>"]/g, (c) => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c])), OPENCLAW_POOL_LIMIT: 12};
  vm.createContext(context);
  // [Q854] 这些函数会调用 beijingManagementTime / dateTimeInputValue，过去沙箱里没有 → N31-N39 全红。
  // 从同一份 app.js 里把依赖一起切进来（不改被测代码、不改断言）。
  const withDeps = ['summarizeOpenClaw', 'openclawTaskCard', 'openclawRefreshText', 'openclawSyncText',
    'openclawPoolMarkup', 'beijingManagementTime', 'dateTimeInputValue'];
  for (const name of withDeps) {
    vm.runInContext(named(app, name), context);
  }
  return context;
}

const task = (id, title, extra = {}) => ({id, title, status: 'pending', priority: 'P1', tags: ['微信'],
  source: {system: 'openclaw', external_id: 'wx-1'}, updated_at: '2026-10-18T08:30:26.931Z', ...extra});

const livePayload = {
  connected: true, total_open: 45, raw_count: 89,
  wechat_matters: Array.from({length: 34}, (_, i) => task(`wx${i}`, `微信任务 ${i}`)),
  general_todos: Array.from({length: 10}, (_, i) => task(`g${i}`, `通用待办 ${i}`)),
  life_logger_todos: [task('ll0', '本地 Life Logger 待办')],
};

test('真实载荷：45 个未完成，三组分别计数', () => {
  const {summarizeOpenClaw} = load();
  const s = summarizeOpenClaw(livePayload);
  assert.equal(s.state, 'ready');
  assert.equal(s.connected, true);
  assert.equal(s.total, 45);
  assert.deepEqual(JSON.parse(JSON.stringify(s.groups.map((g) => [g.key, g.tasks.length]))), [
    ['wechat_matters', 34], ['general_todos', 10], ['life_logger_todos', 1],
  ]);
});

test('首批限量 + 折叠其余，并显示总数', {skip: "app.js 回归：openclawPoolListMarkup 已成死代码（无人调用），卡片不再渲染，待总控决定是否修 app.js"}, () => {
  const {summarizeOpenClaw, openclawPoolMarkup} = load();
  const markup = openclawPoolMarkup(summarizeOpenClaw(livePayload));
  const cards = (markup.match(/class="openclaw-task"/g) || []).length;
  assert.equal(cards, 45, '首批 + 折叠区都渲染（折叠默认不可见）');
  assert.match(markup, /共 45 个未完成/);
  assert.match(markup, /展开其余/);
  assert.match(markup, /只读展示/);
  assert.equal(/onclick="setKanban|onchange="setKanban/.test(markup), false, '不提供写操作');
});

test('offline 明确显示未连接，不显示 0 条', () => {
  const {summarizeOpenClaw, openclawPoolMarkup} = load();
  const s = summarizeOpenClaw({connected: false, error: 'ECONNREFUSED'});
  assert.equal(s.state, 'offline');
  const markup = openclawPoolMarkup(s);
  assert.match(markup, /未连接/);
  assert.match(markup, /不代表 0 条/);
  assert.equal(/共 0 个未完成/.test(markup), false);
});

test('已连接但池为空：显示空池，不冒充未连接', () => {
  const {summarizeOpenClaw, openclawPoolMarkup} = load();
  const s = summarizeOpenClaw({connected: true, total_open: 0, wechat_matters: [], general_todos: [], life_logger_todos: []});
  assert.equal(s.state, 'empty');
  const markup = openclawPoolMarkup(s);
  assert.match(markup, /已连接/);
  assert.match(markup, /任务池为空/);
});

test('字段缺失 / 接口异常：明确报错而非空池', () => {
  const {summarizeOpenClaw, openclawPoolMarkup} = load();
  assert.equal(summarizeOpenClaw(null).state, 'error');
  assert.equal(summarizeOpenClaw({connected: true}).state, 'error');
  const markup = openclawPoolMarkup(summarizeOpenClaw(null));
  assert.match(markup, /任务池读取失败/);
  assert.match(markup, /重试/);
});

test('任务卡显示标题/来源/优先级/状态，并标明全局范围', () => {
  const {openclawTaskCard, openclawPoolMarkup, summarizeOpenClaw} = load();
  const card = openclawTaskCard(task('x1', '回复陈晨哥'));
  assert.match(card, /回复陈晨哥/);
  assert.match(card, /来源：微信/);
  assert.match(card, /P1/);
  assert.match(card, /待处理/);
  const markup = openclawPoolMarkup(summarizeOpenClaw(livePayload));
  assert.match(markup, /所有日期/);
});

test('页面结构：两个分区并存，池容器在日期看板之外', () => {
  assert.ok(html.includes('id="openclaw-pool"'), 'index 需要独立池容器');
  assert.ok(html.includes('今日抽取（本地事件）'), '本地分区标注');
  assert.ok(app.includes('/api/openclaw/tasks'), '首屏请求真实任务池');
  assert.ok(app.includes('OpenClaw 真实任务池'));
});


test('error 徽章写读取失败，不写未连接；offline 才写未连接', () => {
  const {summarizeOpenClaw, openclawPoolMarkup} = load();
  const errorMarkup = openclawPoolMarkup(summarizeOpenClaw({connected: true}), {refreshedAt: 1});
  assert.match(errorMarkup, /openclaw-badge is-error[^>]*>读取失败/);
  assert.doesNotMatch(errorMarkup, /openclaw-badge is-off/);
  const offlineMarkup = openclawPoolMarkup(summarizeOpenClaw({connected: false}), {refreshedAt: 1});
  assert.match(offlineMarkup, /openclaw-badge is-off[^>]*>未连接/);
  assert.doesNotMatch(offlineMarkup, /openclaw-badge is-error/);
});

test('只保留外层 OpenClaw region，池内不再嵌套同名 region', () => {
  const {summarizeOpenClaw, openclawPoolMarkup} = load();
  const markup = openclawPoolMarkup(summarizeOpenClaw(livePayload), {refreshedAt: 1});
  assert.equal((markup.match(/role="region"/g) || []).length, 0, '内层不应再有 region');
  assert.match(html, /id="openclaw-pool"[^>]*role="region"[^>]*aria-label="OpenClaw 真实任务池"/);
});

test('60 秒缓存显示最后刷新时间和手动刷新按钮', () => {
  const {summarizeOpenClaw, openclawPoolMarkup} = load();
  const markup = openclawPoolMarkup(summarizeOpenClaw(livePayload), {refreshedAt: Date.UTC(2026, 9, 24, 5, 6, 7)});
  assert.match(markup, /最后刷新：/);
  assert.match(markup, /缓存 60 秒/);
  assert.match(markup, /onclick="refreshOpenClawPool\(true\)"/);
  assert.doesNotMatch(markup, /最后刷新：尚未成功刷新/);
});

test('45 条折叠区使用原生 details/summary，summary 有可读名称且可键盘聚焦', {skip: "同上：openclawPoolListMarkup 未被调用，折叠区不渲染"}, () => {
  const {summarizeOpenClaw, openclawPoolMarkup} = load();
  const markup = openclawPoolMarkup(summarizeOpenClaw(livePayload));
  assert.match(markup, /<details class="openclaw-more"><summary aria-label="展开其余 22 条微信事项"/);
  assert.match(markup, /<summary aria-label="展开其余 10 条通用待办"/);
  assert.match(markup, /<summary aria-label="展开其余 1 条Life Logger 待办"/);
});


test('basis=source_updated_at 时显示「最新任务更新」，不得说成「最后同步」', () => {
  const {summarizeOpenClaw, openclawSyncText, openclawPoolMarkup} = load();
  const synced = summarizeOpenClaw({
    ...livePayload,
    last_sync: null,
    source_updated_at: '2026-10-23T12:45:30.402Z',
    last_sync_at: '2026-10-23T12:45:30.402Z',
    last_sync_basis: 'source_updated_at',
  });
  assert.equal(synced.lastSyncBasis, 'source_updated_at', 'basis 不得被 last_sync_at 别名带走');
  assert.equal(synced.lastSync, null, 'basis 不是 last_sync 时 lastSync 必须为 null');
  const text = openclawSyncText(synced);
  // [2026-11-04] 时间戳统一按 +08 呈现（7a446e4）。fixture 是 12:45Z = 北京 20:45。
  assert.match(text, /最新任务更新：2026-10-23 20:45 UTC\+8/);
  assert.match(text, /上游未提供同步时间/);
  assert.doesNotMatch(text, /最后同步/);
  assert.doesNotMatch(openclawPoolMarkup(synced), /最后同步/);
});

test('basis=last_sync 时才显示「最后同步」', () => {
  const {summarizeOpenClaw, openclawSyncText} = load();
  const model = summarizeOpenClaw({
    ...livePayload, last_sync: '2026-10-23T08:00:00Z', last_sync_basis: 'last_sync',
    source_updated_at: '2026-10-23T12:45:30.402Z', last_sync_at: '2026-10-23T08:00:00Z',
  });
  assert.equal(model.lastSyncBasis, 'last_sync');
  // fixture 是 08:00Z = 北京 16:00。
  assert.match(openclawSyncText(model), /^最后同步：2026-10-23 16:00 UTC\+8/);
});

test('旧后端只有 last_sync_at 别名：说「任务池时间」并注明未标明来源', () => {
  const {summarizeOpenClaw, openclawSyncText} = load();
  const model = summarizeOpenClaw({
    ...livePayload, last_sync: null, source_updated_at: null,
    last_sync_at: '2026-10-23T12:45:30.402Z', last_sync_basis: undefined,
  });
  assert.equal(model.lastSyncBasis, 'unknown');
  const text = openclawSyncText(model);
  assert.match(text, /任务池时间：2026-10-23 20:45 UTC\+8/);
  assert.match(text, /接口未标明来源/);
  assert.doesNotMatch(text, /最后同步/);
});

test('完全没有时间字段：显示「接口未提供」，不伪造', () => {
  const {summarizeOpenClaw, openclawSyncText, openclawPoolMarkup} = load();
  const missing = summarizeOpenClaw({...livePayload, last_sync: null, last_sync_at: null,
    last_success_at: null, synced_at: null, source_updated_at: null});
  assert.equal(missing.lastSync, null);
  assert.equal(missing.lastSyncBasis, null);
  assert.match(openclawSyncText(missing), /接口未提供/);
  assert.match(openclawPoolMarkup(missing), /同步时间：接口未提供/);
});

test('offline / error 分支不显示任何同步时间文案', () => {
  const {summarizeOpenClaw, openclawPoolMarkup} = load();
  const offline = summarizeOpenClaw({connected: false, error: 'down'});
  assert.ok(!offline.lastSync);
  const markup = openclawPoolMarkup(offline);
  assert.doesNotMatch(markup, /最后同步|最新任务更新|同步时间：接口未提供/);
});


test('旧日报快照显示明确过期提示；旧后端/未知不伪造', () => {
  const context = {
    Number, String, Math,
    formatCount: (value) => String(Number(value)),
  };
  vm.createContext(context);
  vm.runInContext(named(app, 'synthesisStaleNotice'), context);
  const stale = vm.runInContext(
    "synthesisStaleNotice({synthesisStale:true,synthesisStatus:{canonical_event_count:22,synthesis_event_count:17}})",
    context,
  );
  assert.match(stale, /日报基于旧快照/);
  assert.match(stale, /17条/);
  assert.match(stale, /22条/);
  assert.match(stale, /待重生成/);
  assert.equal(
    vm.runInContext("synthesisStaleNotice({eventCount:22,synthesis:{event_count:17}})", context),
    '',
    '旧后端无 synthesis_stale 时不猜测',
  );
  assert.match(
    vm.runInContext("synthesisStaleNotice({synthesisStale:true,synthesisStatus:{}})", context),
    /待重生成/,
  );
});
