import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import test from 'node:test';

const html = fs.readFileSync(new URL('../web/daily-timeline-live.html', import.meta.url), 'utf8');
const script = html.match(/<script>([\s\S]*)<\/script>/)[1];

function loadLabels() {
  const start = script.indexOf('const CATEGORY_META');
  const end = script.indexOf('// =====================================================================================', start);
  assert.notEqual(start, -1, 'missing category/activity label block');
  assert.notEqual(end, -1, 'missing end of label block');
  const context = {result: null};
  vm.createContext(context);
  vm.runInContext(script.slice(start, end) + '\nresult = {ACTIVITY_LABELS, reviewStatusLabel, activityTypeLabel, CATEGORY_META, precisionLabel, captureModeLabel, extractorLabel, captureSourceLabel, drawerDebugRows};', context);
  return context.result;
}

const SNAKE = /[a-z][a-z0-9]*_[a-z0-9_]+/;

test('现场出现的细分类型都有中文标签，且不输出 snake_case', () => {
  const {activityTypeLabel} = loadLabels();
  const liveValues = ['query_task', 'work_action', 'work_information', 'work_discussion', 'project_review', 'work_presentation', 'technical_discussion', 'development'];
  for (const value of liveValues) {
    const label = activityTypeLabel(value);
    assert.ok(label && label !== value, `${value} 应有中文标签`);
    assert.equal(SNAKE.test(label), false, `${value} 的中文标签不得含 snake_case：${label}`);
  }
  assert.equal(activityTypeLabel('query_task'), '查询任务');
  assert.equal(activityTypeLabel('technical_discussion'), '技术讨论');
  assert.equal(activityTypeLabel('work_presentation'), '工作汇报');
  assert.equal(SNAKE.test(activityTypeLabel('work_presentation')), false, 'work_presentation 不得裸露 snake_case');
  assert.equal(activityTypeLabel('development'), '开发推进');
});

test('未知细分值显示“其他记录”，原始值只留在 title 调试属性', () => {
  const {activityTypeLabel} = loadLabels();
  assert.equal(activityTypeLabel('brand_new_thing'), '其他记录');
  assert.equal(activityTypeLabel('capture_todo'), '即时抓取 · 待办');
  assert.equal(activityTypeLabel(''), '一般记录');
  assert.equal(activityTypeLabel(undefined), '一般记录');
  assert.equal(SNAKE.test(activityTypeLabel('brand_new_thing')), false);
});

test('review_status 不再原样露出，reviewed 显示已审阅', () => {
  const {reviewStatusLabel} = loadLabels();
  // [Q430] 审核徽标不得用「已确认」暗示完成/核实（另一张 activity-state-badge-ui 也在锁这条）。
  assert.equal(reviewStatusLabel('reviewed'), '已审阅');
  assert.equal(reviewStatusLabel('REVIEWED'), '已审阅');
  assert.equal(reviewStatusLabel('confirmed'), '已审阅');
  assert.equal(reviewStatusLabel('unreviewed'), '待审阅');
  assert.equal(reviewStatusLabel('rejected'), '已驳回');
  assert.equal(reviewStatusLabel('some_unknown_status'), '已记录');
  assert.equal(reviewStatusLabel(''), '待确认');
  assert.equal(SNAKE.test(reviewStatusLabel('some_unknown_status')), false);
});

test('卡片渲染使用中文主分类，不再输出 catMeta.en', () => {
  assert.ok(script.includes('escapeHtml(catMeta.label)'), '卡片应显示中文主分类');
  assert.equal(/ev-cat-badge">\$\{isCapture \? "capture" : catMeta\.en\}/.test(script), false, '不得回退到 catMeta.en');
  const badgeLine = script.split('\n').find(line => line.includes('ev-cat-badge"'));
  assert.ok(badgeLine.includes('title="category:'), '主分类原始值应放在 title 调试属性');
  assert.ok(script.includes('activityTypeLabel(ev.activity_type)'), '细分类型走统一标签函数');
  assert.equal(script.includes('ACTIVITY_LABELS[ev.activity_type] || ev.activity_type'), false, '不得再把未知值原样显示');
});

test('复核状态 chip 与抽屉不再显示 reviewed 等原始枚举', () => {
  assert.ok(script.includes('reviewStatusLabel(ev.review_status)'), '状态 chip 走中文映射');
  assert.equal(script.includes('statusMap[ev.review_status] || ev.review_status'), false);
  assert.equal(script.includes('${catMeta.en} · ${catMeta.label}'), false, '抽屉主分类不得显示英文枚举');
  assert.equal(script.includes('${ev.activity_type} · ${subAct}'), false, '抽屉细分类型不得显示原始值');
});

test('高频 activity_type 全部有中文映射，未知仍回退“其他记录”', () => {
  const {activityTypeLabel} = loadLabels();
  const live = {
    pos_payment_broadcast: '支付播报',
    reminder: '提醒事项',
    strength_training: '力量训练',
    workout: '健身训练',
    work_observation: '工作观察',
    task_coordination: '任务协调',
    info_dictation: '信息口述',
    bug_report: '问题上报',
    work_issue: '工作问题',
    work_task: '工作任务',
    query_request: '查询请求',
  };
  for (const [value, expected] of Object.entries(live)) {
    const label = activityTypeLabel(value);
    assert.equal(label, expected, value);
    assert.equal(SNAKE.test(label), false, `${value} → ${label}`);
  }
  assert.equal(activityTypeLabel('totally_unknown_kind'), '其他记录');
});

test('时间精度/捕捉模式/抽取器/抓取来源都有中文标签', () => {
  const {precisionLabel, captureModeLabel, extractorLabel, captureSourceLabel} = loadLabels();
  for (const value of ['exact', 'minute', 'inferred', 'relative', 'unknown']) {
    assert.equal(SNAKE.test(precisionLabel(value)), false, value);
  }
  assert.equal(precisionLabel('unknown'), '未知');
  for (const value of ['passive', 'active', 'pending', 'manual']) {
    assert.equal(SNAKE.test(captureModeLabel(value)), false, value);
  }
  assert.equal(captureModeLabel('passive'), '被动采集');
  assert.equal(extractorLabel('deepseek_context_batch'), 'DeepSeek 语境抽取');
  assert.equal(SNAKE.test(extractorLabel('deepseek_context_batch')), false);
  assert.match(captureSourceLabel('todo'), /^即时抓取 · 待办$/);
  assert.equal(SNAKE.test(captureSourceLabel('expense')), false);
});

test('抽屉可见字段不含原始枚举，内部 id 只进调试区', () => {
  const {drawerDebugRows} = loadLabels();
  const rows = drawerDebugRows({event_id: 'evt_ctx_abc', precision: 'unknown', capture_mode: 'passive',
    evidence: {recording_id: 'watch_AAC382', segment_id: 'watch_000308_000001', extractor: 'deepseek_context_batch'}});
  const text = rows.map(([label, value]) => `${label}:${value}`).join('|');
  assert.match(text, /录音批次|音频切片|原始精度|原始抽取器/);
  assert.match(text, /watch_AAC382/);
  // 抽屉可见文案（不含 closed details）不得直接展示原始枚举/内部 id
  const visibleDrawer = script.slice(script.indexOf('if (ev.kind === "capture")'),
                                     script.indexOf('const debugList = document.getElementById("drawerDebugList")'));
  for (const needle of ['${ev.precision}', '${(ev.evidence && ev.evidence.recording_id) || "-"}',
                        '${(ev.evidence && ev.evidence.segment_id) || "-"}',
                        '${(ev.evidence && ev.evidence.extractor) || "-"}',
                        'dashboard.${ev.capture_kind']) {
    // 原始值只允许出现在 title 调试属性里，不能在可见文本中
    const offending = visibleDrawer.split('\n').filter(line => line.includes(needle) && !line.includes('title="'));
    assert.deepEqual(offending, [], `可见抽屉仍在输出：${needle}`);
  }
  assert.ok(visibleDrawer.includes('precisionLabel(ev.precision)'));
  assert.ok(visibleDrawer.includes('captureModeLabel(ev.capture_mode)'));
  assert.ok(visibleDrawer.includes('extractorLabel(ev.evidence && ev.evidence.extractor)'));
  assert.ok(visibleDrawer.includes('captureSourceLabel(ev.capture_kind)'));
  assert.ok(visibleDrawer.includes('reviewStatusLabel((ev.metadata && ev.metadata.status) || "")'));
});

test('时间未定位说明与 data 区不再裸露英文枚举', () => {
  const visible = script.slice(script.indexOf('时间未定位（'), script.indexOf('body.innerHTML = html'));
  assert.equal(/precision=unknown|date_basis=reported_local/.test(
    visible.replace(/title="[^"]*"/g, '')), false, '可见说明不得含原始枚举');
  // 2026-10-24 Q170：title 里也不许出现内部字段名，只能写人话
  assert.ok(/title="发生时间未知，只保留讲述时间"/.test(script),
    '说明文字的 title 必须是用户能懂的话');
  assert.ok(html.includes('<details class="drawer-debug" id="drawerDebug">'), 'data 与内部 id 收进折叠调试区');
  assert.ok(html.includes('id="drawerDebugList"'), '调试区包含原始字段列表');
});

test('页面可见文案不再出现 activity_type 术语', () => {
  const visible = html.replace(/<script>[\s\S]*?<\/script>/g, '').replace(/<style>[\s\S]*?<\/style>/g, '');
  assert.equal(visible.includes('activity_type'), false);
  assert.equal(visible.includes('reviewed<'), false);
});

test('瀑布图默认可见文案不出现接口路径，接口只进折叠的数据来源区', () => {
  const html = fs.readFileSync(new URL('../web/daily-timeline-live.html', import.meta.url), 'utf8');
  const visible = html.replace(/<script>[\s\S]*?<\/script>/g, '').replace(/<style>[\s\S]*?<\/style>/g, '');
  const collapsedStripped = visible.replace(/<details[\s\S]*?<\/details>/g, '');

  assert.equal(collapsedStripped.includes('/api/'), false, '折叠区之外不得出现 /api/ 路径');

  const titles = [...collapsedStripped.matchAll(/title="([^"]*)"/g)].map((m) => m[1]);
  assert.deepEqual(titles.filter((t) => t.includes('/api/')), [], 'title 属性不得变相暴露接口');

  const sub = visible.match(/<div class="brand-sub">([\s\S]*?)<\/div>/);
  assert.ok(sub, '缺少 .brand-sub');
  const subText = sub[1].replace(/<[^>]+>/g, '');
  assert.equal(subText.includes('/api/'), false, `.brand-sub 不得出现接口路径：${subText.trim()}`);
  assert.equal(subText.includes('<code>'), false, '.brand-sub 不得包含代码标签');

  const dataSource = visible.match(/<details class="brand-datasource">[\s\S]*?<\/details>/);
  assert.ok(dataSource, '接口说明应放在默认折叠的 .brand-datasource 里');
  assert.equal(/\sopen(\s|=|>)/.test(dataSource[0]), false, '数据来源 details 默认必须收起');
  assert.ok(dataSource[0].includes('/api/daily'), '折叠区里应写明数据来源接口');
});

// ---------------------------------------------------------------------------
// 2026-10-24 Q170：时间未定位的默认 DOM / title 不得泄漏内部字段名
// ---------------------------------------------------------------------------
const BANNED = /precision|date_basis|reported_local|unknown|\/api\//i;

function sliceFunction(source, name) {
  const start = source.indexOf(`function ${name}(`);
  assert.notEqual(start, -1, `missing function ${name}`);
  const rest = source.slice(start + 1);
  const next = rest.search(/\nfunction |\n\/\/ ={10,}/);
  return next === -1 ? source.slice(start) : source.slice(start, start + 1 + next);
}

const LABEL_BLOCK = (() => {
  const start = script.indexOf('const CATEGORY_META');
  const end = script.indexOf('// =====================================================================================', start);
  assert.notEqual(start, -1, 'missing label block');
  assert.notEqual(end, -1, 'missing end of label block');
  return script.slice(start, end);
})();

function renderUnlocated(item) {
  const parts = [
    'let TZ_OFFSET_MIN = 8 * 60;',
    LABEL_BLOCK,
    sliceFunction(script, 'occurrenceReasonLabel'),
    sliceFunction(script, 'pad2'),
    sliceFunction(script, 'utcIsoToLocalParts'),
    sliceFunction(script, 'formatReportedAt'),
    sliceFunction(script, 'escapeHtml'),
    sliceFunction(script, 'escapeAttr'),
    sliceFunction(script, 'unlocatedItemMarkup'),
  ].join('\n');
  const context = {result: null};
  vm.createContext(context);
  vm.runInContext(parts + '\nresult = unlocatedItemMarkup;', context);
  return context.result(item, '');
}

test('时间未定位条目：title 与可见文本都不含内部字段名', () => {
  const htmlOut = renderUnlocated({
    event_id: 'evt_ctx_demo',
    title: '讨论下周排期',
    activity_type: 'work_discussion',
    precision: 'unknown',
    date_basis: 'reported_local',
    occurrence_reason: 'date_basis_reported_local',
    reported_at: '2026-10-22T17:05:19+08:00',
    quote: '我们下周再排一次',
    pending_reason: '',
  });

  const titles = [...htmlOut.matchAll(/title="([^"]*)"/g)].map(m => m[1]);
  for (const t of titles) assert.equal(BANNED.test(t), false, `title 泄漏内部字段：${t}`);

  // 默认可见部分（去掉默认收起的调试块）也不许出现
  const visible = htmlOut.replace(/<details[\s\S]*?<\/details>/g, '');
  assert.equal(BANNED.test(visible), false, `默认可见区泄漏：${visible}`);

  // 人话判定原因要出现
  assert.match(visible, /日期只能按讲述当天归档/);
});

test('原始 precision/date_basis 只保留在默认收起的调试块里', () => {
  const htmlOut = renderUnlocated({
    event_id: 'evt_x', title: '某件事', activity_type: 'general',
    precision: 'unknown', date_basis: 'reported_local',
    occurrence_reason: 'date_basis_reported_local', reported_at: '', quote: '',
  });
  const debug = htmlOut.match(/<details class="ut-debug">[\s\S]*?<\/details>/);
  assert.ok(debug, '必须有折叠的调试块');
  assert.equal(/\sopen(\s|=|>)/.test(debug[0]), false, '调试块默认必须收起');
  assert.equal(debug[0].includes('unknown'), true, '调试块里保留原始值');
  assert.equal(debug[0].includes('reported_local'), true, '调试块里保留 date_basis');
});

test('判定原因叫法全部是人话，不含内部字段', () => {
  const {occurrenceReasonLabel} = (() => {
    const context = {result: null};
    vm.createContext(context);
    vm.runInContext(sliceFunction(script, 'occurrenceReasonLabel') + '\nresult = {occurrenceReasonLabel};', context);
    return context.result;
  })();
  for (const reason of ['no_absolute_time', 'precision_unknown', 'date_basis_reported_local',
                        'temporal_relation_unknown', 'something_else']) {
    const label = occurrenceReasonLabel(reason);
    assert.equal(BANNED.test(label), false, `${reason} → ${label}`);
    assert.ok(label.length > 4, `${reason} 应有可读说明`);
  }
});

test('静态 DOM：除默认收起的 details 外，正文与所有 title 都不含内部字段/接口路径', () => {
  // 页面后面还有 <script id="…"> 块；旧的 `<script>` 正则剥不干净，会把脚本里的内部字段当成可见 DOM。
  const markup = html.replace(/<script\b[^>]*>[\s\S]*?<\/script>/g, '').replace(/<style\b[^>]*>[\s\S]*?<\/style>/g, '');
  const details = [...markup.matchAll(/<details\b[^>]*>/g)];
  for (const tag of details) {
    assert.equal(/\sopen(\s|=|>)/.test(tag[0]), false, `details 默认必须收起：${tag[0]}`);
  }
  const visible = markup.replace(/<details[\s\S]*?<\/details>/g, '').replace(/<!--[\s\S]*?-->/g, '');
  for (const m of visible.matchAll(/title="([^"]*)"/g)) {
    assert.equal(BANNED.test(m[1]), false, `title 泄漏：${m[1]}`);
  }
  assert.equal(BANNED.test(visible), false, `默认 DOM 泄漏：${visible.match(BANNED)}`);
});
