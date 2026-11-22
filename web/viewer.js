const params = new URLSearchParams(location.search);
const jobId = params.get("job") || "";
const artifactPath = params.get("path") || "";
const fileName = artifactPath.split("/").pop() || "结果";
const viewMode = params.get("mode") || "";
const embeddedMode = params.get("embedded") === "1";
const $ = (id) => document.getElementById(id);
let globalAuditionHandler = null;

// —— localStorage 数据结构版本化 ——
// 持久化数据统一封装为 {"v": 版本号, "value": 业务值} 写入。读取时：
//   1) 旧版未封版本的裸值（字符串 / 旧 JSON，如联系人数组）按当前版本直接兼容，老数据不失效；
//   2) 版本号不符或结构损坏时返回 null，由调用方走默认值，
//      避免以后调整存储结构时旧数据解析报错。
// 变更存储结构时递增 STORAGE_SCHEMA_VERSION，并在此补充对应兼容分支。
const STORAGE_SCHEMA_VERSION = 1;

function readVersionedValue(key) {
  let raw = null;
  try {
    raw = localStorage.getItem(key);
  } catch (_) {
    return null;
  }
  if (raw === null) return null;
  try {
    const parsed = JSON.parse(raw);
    if (parsed && typeof parsed === "object" && !Array.isArray(parsed) && "v" in parsed) {
      if (parsed.v !== STORAGE_SCHEMA_VERSION || !("value" in parsed)) return null;
      const value = parsed.value;
      return typeof value === "string" ? value : JSON.stringify(value);
    }
    // 旧版裸 JSON 值（如联系人数组）：返回原文，调用方自行解析。
    return raw;
  } catch (_) {
    // 旧版裸字符串值（token / 速率等）：直接按当前版本兼容。
    return raw;
  }
}

function writeVersionedValue(key, value) {
  try {
    localStorage.setItem(key, JSON.stringify({ v: STORAGE_SCHEMA_VERSION, value }));
  } catch (_) {
    // 存储不可用时放弃持久化，不影响当前会话内的内存状态。
  }
}

// —— 数字与日期统一走 Intl 格式化，不再各处手拼字符串 ——
const INT_COUNT = new Intl.NumberFormat("zh-CN");
const INT_MONEY = new Intl.NumberFormat("zh-CN", {style: "currency", currency: "CNY"});
const INT_PERCENT = new Intl.NumberFormat("zh-CN", {style: "percent"});
const INT_SECONDS = new Intl.NumberFormat("zh-CN", {style: "unit", unit: "second", unitDisplay: "narrow", minimumFractionDigits: 1, maximumFractionDigits: 1});
const INT_CLOCK = new Intl.NumberFormat("zh-CN", {minimumIntegerDigits: 2, useGrouping: false});
const INT_DAY_KEY = new Intl.DateTimeFormat("en-CA", {timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit"});
const INT_MONTH_DAY = new Intl.DateTimeFormat("en-CA", {timeZone: "Asia/Shanghai", month: "2-digit", day: "2-digit"});
const INT_WEEKDAY = new Intl.DateTimeFormat("zh-CN", {timeZone: "Asia/Shanghai", weekday: "short"});

// 日期键是北京时间日期，不经过宿主本地午夜；拒绝会被 Date 自动进位的无效日。
function localDayDate(dateKey) {
  const key = String(dateKey ?? "");
  if (!/^\d{4}-\d{2}-\d{2}$/.test(key)) return null;
  const date = new Date(`${key}T00:00:00+08:00`);
  if (Number.isNaN(date.getTime()) || INT_DAY_KEY.format(date) !== key) return null;
  return date;
}

function formatCount(value) {
  return INT_COUNT.format(Number(value) || 0);
}

function formatMoney(value) {
  return INT_MONEY.format(Number(value) || 0);
}

function formatPercent(ratio) {
  return INT_PERCENT.format(Number(ratio) || 0);
}

function formatSeconds(value) {
  return INT_SECONDS.format(Math.max(0, Number(value) || 0));
}

function formatClockPair(hours, minutes) {
  return `${INT_CLOCK.format(hours)}:${INT_CLOCK.format(minutes)}`;
}

function formatDayKey(dateKey) {
  const date = localDayDate(dateKey);
  return date ? INT_DAY_KEY.format(date) : String(dateKey || "");
}

function formatMonthDay(dateKey) {
  const date = localDayDate(dateKey);
  return date ? INT_MONTH_DAY.format(date) : String(dateKey || "");
}

function formatWeekday(dateKey) {
  const date = localDayDate(dateKey);
  return date ? INT_WEEKDAY.format(date) : "";
}

// 试听按钮图标：内联 SVG 替代 ▶ / ❚❚ 文本符号，避免各平台 emoji 字体渲染不一致。
// 宽高用 1em 继承按钮字号、填充用 currentColor 继承按钮文字色，不新造尺寸/颜色常量；
// aria-hidden 是因为语义已由外层 button 的 aria-label 承担。
const PLAY_ICON_SVG = '<svg class="audio-icon" width="1em" height="1em" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true" focusable="false"><path d="M8 5.14v13.72a1 1 0 0 0 1.54.84l10.28-6.86a1 1 0 0 0 0-1.68L9.54 4.3A1 1 0 0 0 8 5.14z"/></svg>';
const PAUSE_ICON_SVG = '<svg class="audio-icon" width="1em" height="1em" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true" focusable="false"><rect x="6.5" y="5" width="4" height="14" rx="1"/><rect x="13.5" y="5" width="4" height="14" rx="1"/></svg>';

// 通用 UI 图标：同样以 1em 字号 + currentColor 取色，不新造尺寸/颜色常量。
// ⋯ / ✕ 所在按钮已是 inline-flex 居中布局，图标作为 flex item 自动对齐，无需额外样式；
// ✓ 用于「图标 + 文字」按钮，故用 em 相对的基线微调与相邻空格做间隔，避免硬编码 px。
const MORE_ICON_SVG = '<svg class="ui-icon" width="1em" height="1em" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true" focusable="false"><circle cx="6" cy="12" r="2"/><circle cx="12" cy="12" r="2"/><circle cx="18" cy="12" r="2"/></svg>';
const CLOSE_ICON_SVG = '<svg class="ui-icon" width="1em" height="1em" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true" focusable="false"><path d="M19 6.41 17.59 5 12 10.59 6.41 5 5 6.41 10.59 12 5 17.59 6.41 19 12 13.41 17.59 19 19 17.59 13.41 12z"/></svg>';
const CHECK_ICON_SVG = '<svg class="ui-icon" width="1em" height="1em" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true" focusable="false" style="vertical-align:-.125em"><path d="M9 16.17 4.83 12l-1.42 1.41L9 19 21 7l-1.41-1.41z"/></svg>';

// 无障碍：为承载动态文案（加载 / 空态 / 错误 / 计数）的容器补充 live region，
// 使屏幕阅读器在内容更新时以 polite 方式播报，而不打断当前朗读。
// viewer-content 是整块内容容器，只加 aria-live；count-label / edit-error 是纯状态提示，
// 再补 role="status" + aria-atomic，让屏幕阅读器把整句当作状态消息读完。
const LIVE_REGION_IDS = ["viewer-content", "count-label", "edit-error"];
const STATUS_ROLE_IDS = new Set(["count-label", "edit-error"]);
for (const id of LIVE_REGION_IDS) {
  const el = document.getElementById(id);
  if (!el) continue;
  el.setAttribute("aria-live", "polite");
  if (!STATUS_ROLE_IDS.has(id)) continue;
  el.setAttribute("role", "status");
  el.setAttribute("aria-atomic", "true");
}

const queryToken = String(params.get("token") || "").trim();
if (queryToken) {
  try { sessionStorage.setItem("life-logger-access-token", queryToken); } catch (_) {}
  try { writeVersionedValue("life-logger-access-token", queryToken); } catch (_) {}
}

function getStoredAccessToken() {
  try {
    return String(sessionStorage.getItem("life-logger-access-token") || "").trim() ||
           String(readVersionedValue("life-logger-access-token") || "").trim();
  } catch (_) {
    return "";
  }
}

function secureWriteHeaders() {
  const headers = {
    "Content-Type": "application/json",
    "X-Life-Logger-CSRF": "1",
  };
  const token = getStoredAccessToken();
  if (token) headers.Authorization = `Bearer ${token}`;
  return headers;
}

function showToast(message, type = "info") {
  let toast = document.getElementById("viewer-toast");
  if (!toast) {
    toast = document.createElement("div");
    toast.id = "viewer-toast";
    toast.className = "viewer-toast";
    toast.setAttribute("aria-live", "polite");
    toast.setAttribute("role", "status");
    document.body.appendChild(toast);
  }
  toast.textContent = message;
  toast.className = `viewer-toast show ${type}`;
  clearTimeout(toast._timer);
  toast._timer = setTimeout(() => {
    toast.className = "viewer-toast";
  }, 2500);
}

function preferredScrollBehavior() {
  return window.matchMedia?.("(prefers-reduced-motion: reduce)")?.matches ? "auto" : "smooth";
}

const TITLES = {
  "daily.md": ["原始日报快照", "处理完成时生成的文本快照"],
  "daily_summary.json": ["当日总结与时间轴", "自动合并生成事件与人工确认事件"],
  "events.json": ["生活事件明细", "从本人语音中提取出的结构化生活事件"],
  "unknown-review.json": ["待人工复核", "声源暂时无法确定，需要人工确认的语音"],
  "self-transcript.json": ["我的发言", "声纹判断为本人说话的转写片段"],
  "non-self-transcript.json": ["非本人 / 媒体语音", "被系统排除为非本人或疑似媒体的语音"],
  "transcript.json": ["完整转写", "这份录音的全部 ASR 转写"],
  "source-attribution.json": ["声源归属结果", "每段语音的声纹判断结果"],
};

const CATEGORY_LABELS = { routine: "作息", food: "饮食", diet: "饮食", expense: "消费", exercise: "运动", work_communication: "工作/沟通", work: "工作/沟通", communication: "工作/沟通", idea: "想法", todo: "待办", other: "其他" };

const CATEGORY_ALIASES = { diet: "food", work: "work_communication", communication: "work_communication" };

function canonicalViewerCategory(value) {
  const category = String(value || "");
  return CATEGORY_ALIASES[category] || category;
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({"&":"&amp;", "<":"&lt;", ">":"&gt;", "\"":"&quot;", "'":"&#39;"}[char]));
}

function artifactUrl() {
  return `/api/jobs/${encodeURIComponent(jobId)}/artifact?path=${encodeURIComponent(artifactPath)}`;
}

function artifactUrlFor(path) {
  return `/api/jobs/${encodeURIComponent(jobId)}/artifact?path=${encodeURIComponent(path)}`;
}

function audioClipUrl(start, end) {
  const query = new URLSearchParams({
    path: artifactPath,
    start: String(start),
    end: String(end),
    clip_version: "watch-segment-aligned-1",
  });
  return `/api/jobs/${encodeURIComponent(jobId)}/audio-clip?${query.toString()}`;
}

function siblingArtifactPath(name) {
  const parts = artifactPath.split("/");
  parts[parts.length - 1] = name;
  return parts.join("/");
}

function reviewsUrl() {
  return `/api/jobs/${encodeURIComponent(jobId)}/reviews?path=${encodeURIComponent(artifactPath)}`;
}

function reviewUrl() {
  return `/api/jobs/${encodeURIComponent(jobId)}/review`;
}

function manualEventsUrl({includeRejected = false} = {}) {
  const query = new URLSearchParams({path: artifactPath});
  if (includeRejected) query.set("include_rejected", "1");
  return `/api/jobs/${encodeURIComponent(jobId)}/manual-events?${query.toString()}`;
}

function manualEventsUrlFor(path) {
  return `/api/jobs/${encodeURIComponent(jobId)}/manual-events?path=${encodeURIComponent(path)}`;
}

function eventCorrectionsUrl(path = artifactPath) {
  return `/api/jobs/${encodeURIComponent(jobId)}/event-corrections?path=${encodeURIComponent(path)}`;
}

function eventCorrectionUrl() {
  return `/api/jobs/${encodeURIComponent(jobId)}/event-correction`;
}

function notifyParentDataChanged(kind) {
  if (!embeddedMode || window.parent === window) return;
  window.parent.postMessage({
    type: "life-logger-data-changed",
    kind,
    job_id: jobId,
    artifact_path: artifactPath,
  }, location.origin);
}

async function saveEventCorrection(eventId, patch) {
  const response = await fetch(eventCorrectionUrl(), {
    method: "POST",
    headers: secureWriteHeaders(),
    body: JSON.stringify({path: artifactPath, event_id: eventId, ...patch}),
  });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error || `保存失败：${response.status}`);
  notifyParentDataChanged("event-correction");
  return payload.correction;
}

function applyEventCorrections(events, corrections, {includeRejected = false} = {}) {
  const byId = new Map((corrections || []).map((item) => [item.event_id, item]));
  const output = [];
  for (const source of events || []) {
    const event = {...source};
    const correction = byId.get(event.event_id);
    if (correction) {
      const changes = correction.changes || {};
      for (const key of ["title", "categories", "activity_type", "data"]) {
        if (Object.prototype.hasOwnProperty.call(changes, key)) event[key] = changes[key];
      }
      if (changes.time && typeof changes.time === "object") event.time = {...(event.time || {}), ...changes.time};
      if (Object.prototype.hasOwnProperty.call(changes, "note")) {
        event.evidence = {...(event.evidence || {}), correction_note: changes.note};
      }
      event.correction_status = correction.rejected ? "rejected" : "corrected";
      event.review_status = correction.rejected ? "rejected" : "confirmed";
      if (correction.rejected && !includeRejected) continue;
    }
    output.push(event);
  }
  return output;
}

const EDITABLE_EVENT_CATEGORIES = ["routine", "food", "expense", "exercise", "work_communication", "idea", "todo"];
let activeEditor = null;

function editorCategoryOptions(selected = []) {
  const selectedSet = new Set(selected);
  return EDITABLE_EVENT_CATEGORIES.map((key) => `<label class="edit-category-option"><input type="checkbox" value="${escapeHtml(key)}" ${selectedSet.has(key) ? "checked" : ""}><span>${escapeHtml(CATEGORY_LABELS[key] || key)}</span></label>`).join("");
}

function closeCorrectionEditor(result = null) {
  const dialog = $("edit-dialog");
  if (dialog?.open) dialog.close();
  const resolve = activeEditor?.resolve;
  activeEditor = null;
  if (resolve) resolve(result);
}

function openCorrectionEditor(mode, context = {}) {
  const dialog = $("edit-dialog");
  const eventFields = $("event-edit-fields");
  const transcriptFields = $("transcript-edit-fields");
  const error = $("edit-error");
  if (!dialog || !eventFields || !transcriptFields || !error) return Promise.resolve(null);
  if (activeEditor?.resolve) activeEditor.resolve(null);
  error.classList.add("hidden");
  error.textContent = "";
  eventFields.classList.toggle("hidden", mode !== "event");
  transcriptFields.classList.toggle("hidden", mode !== "transcript");
  if (mode === "event") {
    const item = context.event || {};
    $("edit-dialog-title").textContent = "编辑生活事件";
    $("edit-dialog-help").textContent = "修正会作为人工 overlay 保存，原始 events.json 不会被覆盖。";
    $("edit-event-title").value = item.title || "";
    $("edit-event-categories").innerHTML = editorCategoryOptions(item.categories || []);
    $("edit-event-time").value = item?.time?.absolute || "";
    $("edit-event-amount").value = Number.isFinite(Number(item?.data?.expense?.amount)) ? String(Number(item.data.expense.amount)) : "";
    $("edit-event-note").value = item?.evidence?.correction_note || "";
  } else {
    $("edit-dialog-title").textContent = "修正语音转写";
    $("edit-dialog-help").textContent = "原始 ASR 永久保留；正确文本会单独保存，并用于后续人工确认事件提取。";
    $("edit-original-asr").textContent = context.originalText || "（无原始文本）";
    $("edit-corrected-text").value = context.correctedText || context.originalText || "";
  }
  return new Promise((resolve) => {
    activeEditor = {mode, context, resolve};
    dialog.showModal();
    queueMicrotask(() => {
      const target = mode === "event" ? $("edit-event-title") : $("edit-corrected-text");
      target?.focus();
      target?.select?.();
    });
  });
}

function submitCorrectionEditor(event) {
  event.preventDefault();
  if (!activeEditor) return;
  const error = $("edit-error");
  try {
    if (activeEditor.mode === "transcript") {
      const corrected = String($("edit-corrected-text").value || "").trim();
      if (!corrected) throw new Error("修正文本不能为空。");
      closeCorrectionEditor({corrected_text: corrected});
      return;
    }
    const current = activeEditor.context.event || {};
    const title = String($("edit-event-title").value || "").trim();
    if (!title) throw new Error("事件标题不能为空。");
    const categories = [...$("edit-event-categories").querySelectorAll("input:checked")].map((input) => input.value);
    if (!categories.length) throw new Error("至少选择一个分类。");
    const changes = {
      title,
      categories,
      note: String($("edit-event-note").value || "").trim(),
    };
    const absolute = String($("edit-event-time").value || "").trim();
    if (absolute) changes.time = {absolute, precision: "exact"};
    const amountText = String($("edit-event-amount").value || "").trim();
    if (categories.includes("expense") && amountText) {
      const amount = Number(amountText);
      if (!Number.isFinite(amount) || amount < 0) throw new Error("消费金额格式无效。");
      changes.data = {...(current.data || {}), expense: {...(current?.data?.expense || {}), amount}};
    }
    closeCorrectionEditor({changes});
  } catch (editorError) {
    if (!error) return;
    error.textContent = editorError.message;
    error.classList.remove("hidden");
  }
}

$("edit-form")?.addEventListener("submit", submitCorrectionEditor);
$("edit-cancel")?.addEventListener("click", () => closeCorrectionEditor(null));
$("edit-dialog-close")?.addEventListener("click", () => closeCorrectionEditor(null));
$("edit-dialog")?.addEventListener("cancel", (event) => { event.preventDefault(); closeCorrectionEditor(null); });

// 与 web/app.js 的时间显示格式保持一致：HH:MM（不显示秒），无值时用 "--:--"。
// 所有时间统一按 Asia/Shanghai 解读；优先使用产物写入的 clock_local /
// started_at_local / time.local，避免浏览器或旧 UTC 字符串把 05:39Z 显示成 05:39。
function localClockText(value) {
  const text = String(value || "").trim();
  if (!text) return null;
  const match = text.match(/(?:^|[T\s])(\d{2}):(\d{2})(?::\d{2})?/);
  if (!match) return null;
  return formatClockPair(Number(match[1]), Number(match[2]));
}

function shanghaiClockText(value) {
  const text = String(value || "").trim();
  if (!text) return null;
  const zoned = /(?:Z|[+-]\d{2}:?\d{2})$/i.test(text);
  if (text.includes("T") && !zoned) {
    // Legacy naive timestamps are already local wall-clock values.
    return localClockText(text);
  }
  const millis = Date.parse(text);
  if (!Number.isFinite(millis)) return localClockText(text);
  const parts = new Intl.DateTimeFormat("en-GB", {
    timeZone: "Asia/Shanghai",
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
  }).formatToParts(new Date(millis)).reduce((acc, part) => {
    acc[part.type] = part.value;
    return acc;
  }, {});
  return parts.hour && parts.minute ? formatClockPair(Number(parts.hour), Number(parts.minute)) : null;
}

function shanghaiDateKey(value) {
  const text = String(value || "").trim();
  if (!text) return "";
  const direct = text.match(/^(\d{4}-\d{2}-\d{2})/);
  const hasZone = /(?:Z|[+-]\d{2}:?\d{2})$/i.test(text);
  if (direct && (!text.includes("T") || !hasZone)) return direct[1];
  const millis = Date.parse(text);
  if (!Number.isFinite(millis)) return direct ? direct[1] : "";
  return new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(new Date(millis));
}

function itemLocalClock(item) {
  return item?.clock_local || item?.started_at_local || item?.time?.local || item?.local_time || "";
}

function itemLocalDateKey(item) {
  const local = String(item?.started_at_local || item?.time?.local || "").trim();
  if (local) {
    const key = shanghaiDateKey(local);
    if (key) return key;
  }
  return shanghaiDateKey(item?.absolute_started_at || item?.time?.absolute || item?.evidence?.reported_at || "");
}

function formatTime(value, fallbackSeconds, localValue) {
  const localClock = localClockText(localValue);
  if (localClock) return localClock;
  const clock = shanghaiClockText(value);
  if (clock) return clock;
  if (Number.isFinite(Number(fallbackSeconds))) {
    const seconds = Math.max(0, Number(fallbackSeconds));
    const h = Math.floor(seconds / 3600), m = Math.floor((seconds % 3600) / 60);
    return formatClockPair(h, m);
  }
  return "--:--";
}

function shanghaiDayKey(offsetDays = 0) {
  return new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(new Date(Date.now() + offsetDays * 86400000));
}

// 与 web/app.js 的日期显示格式保持一致：今天/昨天/前天 · YYYY-MM-DD (周X)。
function formatDayLabel(dateKey) {
  if (!dateKey) return "未知日期";
  let prefix = "";
  if (dateKey === shanghaiDayKey(0)) prefix = "今天";
  else if (dateKey === shanghaiDayKey(-1)) prefix = "昨天";
  else if (dateKey === shanghaiDayKey(-2)) prefix = "前天";
  const weekday = formatWeekday(dateKey);
  const dayKey = formatDayKey(dateKey);
  if (!weekday) return prefix ? `${prefix} · ${dayKey}` : dayKey;
  return prefix ? `${prefix} · ${dayKey} (${weekday})` : `${dayKey} (${weekday})`;
}

// 与 web/app.js 的相似度显示保持一致：0–1 比例取整为整数百分比（Intl.NumberFormat percent），不保留小数。
function similarity(value) {
  const number = Number(value);
  return Number.isFinite(number) ? formatPercent(number) : "—";
}

function sourceLabel(value) {
  return ({self: "本人", unknown: "待确认", non_self: "现场其他人", other_person: "现场其他人", media: "噪音/媒体", non_speech: "噪音/媒体", manual: "人工确认"})[value] || value || "未分类";
}

function decisionReasonText(value) {
  return ({
    segment_too_short: "片段太短，声纹不稳定",
    short_segment_low_similarity_review: "短句声源不确定，建议人工确认",
    similarity_in_review_band: "声纹相似度处于待确认区间",
    below_non_self_threshold: "声纹相似度很低",
    above_self_threshold: "声纹与本人模板较接近",
    audio_slice_empty: "音频片段不可用",
    multi_evidence_far_low_similarity: "声纹偏低、声学远场且缺少本人上下文支持",
  })[value] || value || "待确认";
}

function proximityText(value) {
  return ({near: "近场", far: "远场", uncertain: "近远场不确定"})[value] || "近远场无数据";
}

function priorityText(value) {
  return ({high: "高", medium: "中", low: "低"})[value] || "中";
}

function recordText(item) {
  return item.text || item.title || item.content || item.evidence?.quote || "";
}

function recordSecondaryText(item) {
  const primary = recordText(item);
  const quote = item.evidence?.quote || "";
  return quote && quote !== primary ? quote : "";
}

function searchableText(item) {
  return [
    recordText(item),
    recordSecondaryText(item),
    item.segment_id,
    item.source_label,
    item.decision_reason,
  ].filter(Boolean).join("\n").toLowerCase();
}

function highlightedText(value, query) {
  const text = String(value || "");
  const needle = String(query || "").trim();
  if (!needle) return escapeHtml(text);
  const lower = text.toLowerCase();
  const lowerNeedle = needle.toLowerCase();
  let cursor = 0;
  let html = "";
  while (cursor < text.length) {
    const index = lower.indexOf(lowerNeedle, cursor);
    if (index < 0) {
      html += escapeHtml(text.slice(cursor));
      break;
    }
    html += escapeHtml(text.slice(cursor, index));
    html += `<mark>${escapeHtml(text.slice(index, index + needle.length))}</mark>`;
    cursor = index + needle.length;
  }
  return html || escapeHtml(text);
}

function setupSearch(onSearch) {
  const search = $("search-input");
  const button = $("search-button");
  const clear = $("search-clear");
  if (!search || !button || !clear) return () => {};
  const run = () => {
    const query = search.value.trim();
    clear.classList.toggle("hidden", !query);
    onSearch(query);
  };
  button.onclick = run;
  clear.onclick = () => {
    search.value = "";
    clear.classList.add("hidden");
    onSearch("");
    search.focus();
  };
  search.onkeydown = (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      run();
    }
  };
  return run;
}

function renderRecords(records) {
  const content = $("viewer-content"), toolbar = $("toolbar"), search = $("search-input");
  toolbar.classList.remove("hidden");

  function renderCard(item, query) {
    const label = item.source_label || "";
    const time = formatTime(item.absolute_started_at || item.time?.absolute, item.start_offset_seconds || item.time?.relative_seconds, itemLocalClock(item));
    const reason = item.decision_reason ? `判定：${decisionReasonText(item.decision_reason)}` : "";
    const sim = item.voice_similarity != null ? `声纹相似度 ${similarity(item.voice_similarity)}` : "";
    const secondary = recordSecondaryText(item);
    return `<article class="record-card"><div class="record-meta"><span class="record-time">${escapeHtml(time)}</span>${label ? `<span class="tag ${escapeHtml(label)}">${escapeHtml(sourceLabel(label))}</span>` : ""}</div><p class="record-text">${highlightedText(recordText(item) || "（无文字内容）", query)}</p>${secondary ? `<div class="record-quote"><span>原始发言</span><p>${highlightedText(secondary, query)}</p></div>` : ""}<div class="record-foot">${sim ? `<span>${escapeHtml(sim)}</span>` : ""}${reason ? `<span>${escapeHtml(reason)}</span>` : ""}</div></article>`;
  }

  function draw(query = "") {
    const normalized = query.trim().toLowerCase();
    const filtered = normalized ? records.filter((item) => searchableText(item).includes(normalized)) : records;
    const countLabel = $("count-label");
    if (countLabel) countLabel.textContent = `${formatCount(filtered.length)} / ${formatCount(records.length)} 条`;
    if (!filtered.length) { content.innerHTML = `<div class="empty">没有找到包含“${escapeHtml(query)}”的发言。</div>`; return; }

    const dayGroups = new Map();
    filtered.forEach((item) => {
      const dateKey = itemLocalDateKey(item);
      const groupKey = dateKey || "全部记录";
      if (!dayGroups.has(groupKey)) dayGroups.set(groupKey, []);
      dayGroups.get(groupKey).push(item);
    });

    if (dayGroups.size > 1 && !dayGroups.has("全部记录")) {
      content.innerHTML = `<div class="record-day-groups">${[...dayGroups.entries()].map(([dKey, items]) => {
        return `<section class="record-day-block"><div class="record-day-header">📅 <strong>${escapeHtml(formatDayLabel(dKey))}</strong><span>共 ${formatCount(items.length)} 条发言</span></div><div class="record-list">${items.map((it) => renderCard(it, query)).join("")}</div></section>`;
      }).join("")}</div>`;
    } else {
      content.innerHTML = `<div class="record-list">${filtered.map((it) => renderCard(it, query)).join("")}</div>`;
    }
  }
  search.value = "";
  setupSearch(draw);
  draw();
}

async function saveReview(segmentId, patch) {
  const response = await fetch(reviewUrl(), {
    method: "POST",
    headers: secureWriteHeaders(),
    body: JSON.stringify({path: artifactPath, segment_id: segmentId, ...patch}),
  });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error || `保存失败：${response.status}`);
  notifyParentDataChanged("review");
  return payload.review;
}

function renderReviewRecords(records, savedReviews, contextRecords = records) {
  const content = $("viewer-content"), toolbar = $("toolbar"), search = $("search-input");
  const decisions = new Map(savedReviews.map((item) => [item.segment_id, item]));
  const contextIndex = new Map(contextRecords.map((item, index) => [item.segment_id, index]));
  let onlyPending = true;
  let showBackground = false;
  let sortBy = "time";
  let keyboardSegmentId = null;
  let query = "";
  const selectedSegmentIds = new Set();
  const groupInterlocutorDrafts = new Map();
  let currentSceneGroups = [];
  let currentPlaybackRate = 1.0;
  try {
    currentPlaybackRate = Number(readVersionedValue("life-logger-review-rate")) || 1.0;
  } catch (_) {}
  toolbar.classList.remove("hidden");

  const DEFAULT_CONTACTS = [
    { name: "妈妈", relation: "family", icon: "👩" },
    { name: "姐姐", relation: "family", icon: "👧" },
    { name: "领导", relation: "colleague", icon: "👔" },
    { name: "同事", relation: "colleague", icon: "💼" },
    { name: "朋友", relation: "friend", icon: "🤝" },
  ];

  function getSavedContacts() {
    try {
      const stored = readVersionedValue("life-logger-saved-contacts");
      if (stored) {
        const parsed = JSON.parse(stored);
        if (Array.isArray(parsed) && parsed.length) return parsed;
      }
    } catch (_) {}
    return DEFAULT_CONTACTS;
  }

  function saveContact(name, relation) {
    if (!name || !name.trim()) return;
    const cleanName = name.trim();
    const cleanRel = relation || "other";
    const current = getSavedContacts();
    const existingIndex = current.findIndex((c) => c.name === cleanName);
    const icon = cleanRel === "family"
      ? (cleanName.includes("姐") ? "👧" : cleanName.includes("妈") ? "👩" : "🏠")
      : cleanRel === "colleague"
      ? (cleanName.includes("领导") ? "👔" : "💼")
      : cleanRel === "friend"
      ? "🤝"
      : "👤";
    if (existingIndex >= 0) {
      current[existingIndex].relation = cleanRel;
    } else {
      current.unshift({ name: cleanName, relation: cleanRel, icon });
    }
    try {
      writeVersionedValue("life-logger-saved-contacts", current.slice(0, 15));
    } catch (_) {}
  }

  // Pre-seed any contacts already saved in decisions
  for (const item of records) {
    const dec = decisions.get(item.segment_id);
    if (dec?.interlocutor_name) {
      saveContact(dec.interlocutor_name, dec.interlocutor_relation || "other");
    }
  }

  let activeGlobalInterlocutor = null;
  for (const item of records) {
    const dec = decisions.get(item.segment_id);
    if (dec?.interlocutor_name) {
      activeGlobalInterlocutor = { name: dec.interlocutor_name, relation: dec.interlocutor_relation || "other" };
      break;
    }
  }
  const recordingInterlocutors = new Map();

  function resolveInterlocutor(groupIdx, recId) {
    if (groupIdx !== undefined && groupIdx >= 0 && currentSceneGroups[groupIdx]) {
      const group = currentSceneGroups[groupIdx];
      const groupKey = group[0].segment_id;
      const draft = groupInterlocutorDrafts.get(groupKey);
      if (draft && draft.name) return { name: draft.name, relation: draft.relation || "other" };
      const existingRel = group.map((it) => (decisions.get(it.segment_id) || {}).interlocutor_relation).find(Boolean) || "";
      const existingName = group.map((it) => (decisions.get(it.segment_id) || {}).interlocutor_name).find(Boolean) || "";
      if (existingName) return { name: existingName, relation: existingRel || "other" };
    }
    if (recId && recordingInterlocutors.has(recId)) {
      return recordingInterlocutors.get(recId);
    }
    if (activeGlobalInterlocutor && activeGlobalInterlocutor.name) {
      return activeGlobalInterlocutor;
    }
    return null;
  }

  function groupRecordsByScene(items, maxGapSeconds = 240) {
    if (!items.length) return [];
    const groups = [];
    let currentGroup = [items[0]];

    for (let i = 1; i < items.length; i++) {
      const prev = items[i - 1];
      const curr = items[i];
      let gap = 0;
      if (prev.absolute_started_at && curr.absolute_started_at) {
        const pTime = new Date(prev.absolute_started_at).getTime() / 1000;
        const cTime = new Date(curr.absolute_started_at).getTime() / 1000;
        const pDur = (Number(prev.end_offset_seconds) || 0) - (Number(prev.start_offset_seconds) || 0);
        gap = Math.abs(cTime - (pTime + pDur));
      } else {
        const prevStart = Number(prev.start_offset_seconds) || 0;
        const prevEnd = Number(prev.end_offset_seconds) || prevStart;
        const currStart = Number(curr.start_offset_seconds) || 0;
        gap = Math.abs(currStart - prevEnd);
      }

      const isDiffRecording = Boolean(curr.recording_id && prev.recording_id && curr.recording_id !== prev.recording_id);
      if (isDiffRecording || gap > maxGapSeconds) {
        groups.push(currentGroup);
        currentGroup = [curr];
      } else {
        currentGroup.push(curr);
      }
    }
    groups.push(currentGroup);
    return groups;
  }

  function reviewLayer(item) {
    if (item.review_layer) return item.review_layer;
    if (item.review_priority === "high") return "focus";
    if (item.review_priority === "medium") return "standard";
    if (item.review_priority === "low") return "background";
    return "standard";
  }

  function completionCount(segmentId) {
    const item = decisions.get(segmentId);
    return Number(Boolean(item?.source_decision)) + Number(Boolean(item?.transcript_decision));
  }

  function isReviewed(segmentId) {
    return completionCount(segmentId) === 2;
  }

  function contextWindow(item) {
    const index = contextIndex.get(item.segment_id);
    if (index == null) return {before: [], after: []};
    const before = [];
    const after = [];
    const seen = new Set([String(recordText(item) || "").trim()]);
    function canAdd(candidate) {
      const text = String(recordText(candidate) || "").trim();
      if (!text || seen.has(text)) return false;
      if (text.length <= 3) return false;
      seen.add(text);
      return true;
    }
    const targetStart = Number(item.start_offset_seconds);
    const targetEnd = Number(item.end_offset_seconds);
    for (let i = index - 1; i >= 0 && before.length < 3; i -= 1) {
      const candidate = contextRecords[i];
      if (candidate.recording_id !== item.recording_id) break;
      const gap = Number.isFinite(targetStart) ? targetStart - Number(candidate.end_offset_seconds) : 0;
      if (Number.isFinite(gap) && gap > 45) break;
      if (canAdd(candidate)) before.unshift(candidate);
    }
    for (let i = index + 1; i < contextRecords.length && after.length < 3; i += 1) {
      const candidate = contextRecords[i];
      if (candidate.recording_id !== item.recording_id) break;
      const gap = Number.isFinite(targetEnd) ? Number(candidate.start_offset_seconds) - targetEnd : 0;
      if (Number.isFinite(gap) && gap > 45) break;
      if (canAdd(candidate)) after.push(candidate);
    }
    return {before, after};
  }

  function contextLine(item, position) {
    const time = formatTime(item.absolute_started_at, item.start_offset_seconds, itemLocalClock(item));
    return `<div class="review-context-line ${position}"><span>${escapeHtml(time)}</span><p>${highlightedText(recordText(item) || "（无文字内容）", query)}</p></div>`;
  }

  function relationText(value) {
    return ({
      friend: "朋友",
      colleague: "同事",
      family: "家人",
      business: "商务",
      stranger: "陌生人",
      other: "其他",
    })[value] || value || "";
  }

  function reviewSearchableText(item) {
    const context = contextWindow(item);
    const decision = decisions.get(item.segment_id) || {};
    return [
      searchableText(item),
      decision.interlocutor_name || "",
      relationText(decision.interlocutor_relation) || "",
      ...context.before.map((candidate) => recordText(candidate)),
      ...context.after.map((candidate) => recordText(candidate)),
    ].filter(Boolean).join("\n").toLowerCase();
  }

  function sourceText(value) {
    return ({self: "我本人", non_self: "现场其他人", other_person: "现场其他人", media: "噪音/媒体", non_speech: "噪音/媒体", unclear: "无法判断"})[value] || "";
  }

  function transcriptText(value) {
    return ({correct: "转写正确", incorrect: "转写有误", unclear: "无法判断"})[value] || "";
  }

  function button(label, segmentId, kind, value, active) {
    return `<button class="review-button ${active ? "active" : ""}" data-segment="${escapeHtml(segmentId)}" data-kind="${kind}" data-value="${value}">${label}</button>`;
  }

  async function executeBatchReview(segmentIds, action, extraPatch = {}) {
    if (!segmentIds || !segmentIds.length) return;
    const actionNames = {
      other_person: "现场其他人",
      media: "噪音 / 媒体",
      non_speech: "噪音 / 媒体",
      self: "我本人（正确）",
      apply_interlocutor: "对谈人信息",
    };
    const actionName = actionNames[action] || action;
    const resolvedSource = (action === "media" || action === "non_speech") ? "non_speech" : action;
    showToast(`正在批量处理 ${segmentIds.length} 条【${actionName}】…`, "info");

    for (const id of segmentIds) {
      const prev = decisions.get(id) || {};
      const itemPatch = action === "self"
        ? { source_decision: "self", transcript_decision: "correct", ...extraPatch }
        : action === "apply_interlocutor"
        ? {
            source_decision: prev.source_decision || "other_person",
            transcript_decision: prev.transcript_decision || "correct",
            ...extraPatch,
          }
        : { source_decision: resolvedSource, transcript_decision: "correct", ...extraPatch };
      decisions.set(id, { ...prev, ...itemPatch, segment_id: id });
      selectedSegmentIds.delete(id);
    }

    const scrollTop = window.scrollY;
    draw();
    try {
      const nextGroupCard = content.querySelector(".review-card:not(.reviewed)");
      if (nextGroupCard) {
        nextGroupCard.scrollIntoView({behavior: preferredScrollBehavior(), block: "center"});
        activateKeyboardCard(nextGroupCard, {scroll: false});
      } else {
        window.scrollTo({top: scrollTop, behavior: "instant"});
        currentKeyboardCard();
      }
    } catch (_) {}

    // First try atomic batch endpoint
    try {
      const batchItems = segmentIds.map((id) => {
        const item = decisions.get(id) || {};
        const itemPatch = action === "self"
          ? { source_decision: "self", transcript_decision: "correct", ...extraPatch }
          : action === "apply_interlocutor"
          ? {
              source_decision: item.source_decision || "other_person",
              transcript_decision: item.transcript_decision || "correct",
              ...extraPatch,
            }
          : { source_decision: resolvedSource, transcript_decision: "correct", ...extraPatch };
        return {
          segment_id: id,
          ...itemPatch,
        };
      });
      const res = await fetch(`/api/jobs/${encodeURIComponent(jobId)}/reviews-batch`, {
        method: "POST",
        headers: secureWriteHeaders(),
        body: JSON.stringify({path: artifactPath, items: batchItems}),
      });
      if (res.ok) {
        const payload = await res.json();
        (payload.reviews || []).forEach((saved) => {
          if (saved.segment_id) decisions.set(saved.segment_id, saved);
        });
        showToast(`已成功将 ${segmentIds.length} 条标为【${actionName}】！`, "success");
        notifyParentDataChanged("review");
        return;
      }
    } catch (e) {
      console.warn("批量接口失败，尝试逐条保存:", e);
    }

    // Fallback: chunked save
    const CHUNK_SIZE = 6;
    let failCount = 0;
    for (let i = 0; i < segmentIds.length; i += CHUNK_SIZE) {
      const chunk = segmentIds.slice(i, i + CHUNK_SIZE);
      await Promise.all(chunk.map(async (id) => {
        try {
          const item = decisions.get(id) || {};
          const itemPatch = action === "self"
            ? { source_decision: "self", transcript_decision: "correct", ...extraPatch }
            : action === "apply_interlocutor"
            ? {
                source_decision: item.source_decision || "other_person",
                transcript_decision: item.transcript_decision || "correct",
                ...extraPatch,
              }
            : { source_decision: resolvedSource, transcript_decision: "correct", ...extraPatch };
          const saved = await saveReview(id, itemPatch);
          decisions.set(id, saved);
        } catch (e) {
          failCount += 1;
          console.error(`保存片段 ${id} 失败:`, e);
        }
      }));
    }
    if (failCount === 0) {
      showToast(`已成功将 ${segmentIds.length} 条标为【${actionName}】！`, "success");
    } else {
      showToast(`批量保存完成，但有 ${failCount} 条失败，请重试`, "error");
    }
  }

  async function applyGroupInterlocutor(groupIdx) {
    const group = currentSceneGroups[groupIdx];
    if (!group || !group.length) return;
    const groupKey = group[0].segment_id;
    const draft = groupInterlocutorDrafts.get(groupKey) || {};
    const existingRel = group.map((it) => (decisions.get(it.segment_id) || {}).interlocutor_relation).find(Boolean) || "";
    const existingName = group.map((it) => (decisions.get(it.segment_id) || {}).interlocutor_name).find(Boolean) || "";
    const rel = draft.relation !== undefined ? draft.relation : existingRel;
    const name = draft.name !== undefined ? draft.name : existingName;

    const checkedInGroup = group.filter((it) => selectedSegmentIds.has(it.segment_id));
    const targetItems = checkedInGroup.length > 0 ? checkedInGroup : group;
    const targetIds = targetItems.map((it) => it.segment_id);

    const extraPatch = {
      interlocutor_relation: rel || "",
      interlocutor_name: name || "",
    };
    await executeBatchReview(targetIds, "apply_interlocutor", extraPatch);
  }

  function draw() {
    const normalized = query.trim().toLowerCase();
    const visibleRecords = showBackground
      ? records
      : records.filter((item) => reviewLayer(item) !== "background");
    const searched = normalized
      ? visibleRecords.filter((item) => reviewSearchableText(item).includes(normalized))
      : visibleRecords;
    const filtered = (onlyPending ? searched.filter((item) => !isReviewed(item.segment_id)) : searched).sort((left, right) => {
      if (sortBy === "priority") {
        const leftPriority = Number(left.review_priority_score);
        const rightPriority = Number(right.review_priority_score);
        if (Number.isFinite(leftPriority) || Number.isFinite(rightPriority)) {
          const leftValue = Number.isFinite(leftPriority) ? leftPriority : 0;
          const rightValue = Number.isFinite(rightPriority) ? rightPriority : 0;
          if (leftValue !== rightValue) return rightValue - leftValue;
        }
        const leftSimilarity = Number(left.voice_similarity);
        const rightSimilarity = Number(right.voice_similarity);
        const leftNeeds = Number.isFinite(leftSimilarity) ? Math.abs(leftSimilarity - 0.5) : 1;
        const rightNeeds = Number.isFinite(rightSimilarity) ? Math.abs(rightSimilarity - 0.5) : 1;
        if (leftNeeds !== rightNeeds) return leftNeeds - rightNeeds;
        const leftDuration = Number(left.end_offset_seconds) - Number(left.start_offset_seconds);
        const rightDuration = Number(right.end_offset_seconds) - Number(right.start_offset_seconds);
        return (Number.isFinite(rightDuration) ? rightDuration : 0) - (Number.isFinite(leftDuration) ? leftDuration : 0);
      }
      const leftStart = Number(left.start_offset_seconds) || 0;
      const rightStart = Number(right.start_offset_seconds) || 0;
      return leftStart - rightStart;
    });

    const reviewedCount = visibleRecords.filter((item) => isReviewed(item.segment_id)).length;
    const pendingCount = visibleRecords.filter((item) => !isReviewed(item.segment_id)).length;
    const partialCount = visibleRecords.filter((item) => completionCount(item.segment_id) === 1).length;

    currentSceneGroups = sortBy === "priority"
      ? filtered.map((item) => [item])
      : groupRecordsByScene(filtered);

    let lastRenderedRecId = null;
    const isMultiRecording = new Set(records.map((r) => r.recording_id).filter(Boolean)).size > 1;

    const listHtml = currentSceneGroups.map((group, groupIdx) => {
      const first = group[0];
      const last = group[group.length - 1];
      const groupKey = first.segment_id;
      const recId = first.recording_id;
      const startStr = formatTime(first.absolute_started_at, first.start_offset_seconds, itemLocalClock(first));
      const endStr = formatTime(last.absolute_started_at, last.end_offset_seconds, itemLocalClock(last));
      const isMulti = group.length > 1;
      const groupTitle = `💬 连续对话场景 · ${startStr} ~ ${endStr}`;

      let recBannerHtml = "";
      if (isMultiRecording && recId && recId !== lastRenderedRecId) {
        lastRenderedRecId = recId;
        const recInterlocutor = recordingInterlocutors.get(recId) || activeGlobalInterlocutor;
        recBannerHtml = `
        <div class="recording-separator-banner">
          <div class="rec-banner-info">
            <span class="rec-banner-icon" aria-hidden="true"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" focusable="false"><path d="M20 20a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.69-.9L9.6 3.9A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2Z"/></svg></span>
            <span class="rec-banner-title">独立录音：${escapeHtml(recId)} · 起始 ${escapeHtml(startStr)}</span>
          </div>
          <div class="rec-banner-quick-interlocutor">
            <span class="rec-banner-label">此录音主对谈人:</span>
            ${getSavedContacts().slice(0, 5).map((c) => `
              <button type="button" class="rec-contact-btn ${recInterlocutor?.name === c.name ? "active" : ""}" data-rec-id="${escapeHtml(recId)}" data-contact-name="${escapeHtml(c.name)}" data-contact-rel="${escapeHtml(c.relation)}" title="为此录音指定对谈人: ${escapeHtml(c.name)}" aria-label="为此录音指定对谈人：${escapeHtml(c.name)}">
                ${escapeHtml(c.name)}
              </button>
            `).join("")}
          </div>
        </div>`;
      }

      const effectiveGroupInterlocutor = resolveInterlocutor(groupIdx, recId);
      const existingRel = group.map((it) => (decisions.get(it.segment_id) || {}).interlocutor_relation).find(Boolean) || "";
      const existingName = group.map((it) => (decisions.get(it.segment_id) || {}).interlocutor_name).find(Boolean) || "";
      const draft = groupInterlocutorDrafts.get(groupKey);
      const currentRel = draft?.relation !== undefined ? draft.relation : (existingRel || effectiveGroupInterlocutor?.relation || "");
      const currentName = draft?.name !== undefined ? draft.name : (existingName || effectiveGroupInterlocutor?.name || "");

      const displayInterlocutorName = currentName || effectiveGroupInterlocutor?.name || "";
      const displayInterlocutorRel = currentRel || effectiveGroupInterlocutor?.relation || "";
      const groupInterlocutorText = [relationText(displayInterlocutorRel), displayInterlocutorName].filter(Boolean).join(" · ");
      const interlocutorBadge = groupInterlocutorText
        ? `<span class="scene-interlocutor-tag">👤 ${escapeHtml(groupInterlocutorText)}</span>`
        : "";

      const rowsHtml = group.map((item) => {
        const decision = decisions.get(item.segment_id) || {};
        const time = formatTime(item.absolute_started_at, item.start_offset_seconds, itemLocalClock(item));
        const reviewed = isReviewed(item.segment_id);
        const completed = completionCount(item.segment_id);
        const isSelected = selectedSegmentIds.has(item.segment_id);
        const audioStart = Math.max(0, Number(item.start_offset_seconds) || 0);
        const audioEnd = Number(item.end_offset_seconds);
        const hasAudio = Number.isFinite(audioEnd) && audioEnd > audioStart;
        const audioDuration = hasAudio ? formatSeconds(audioEnd - audioStart) : formatSeconds(0);
        const sim = item.voice_similarity != null ? `声纹 ${similarity(item.voice_similarity)}` : "";
        const proximity = proximityText(item.acoustic_proximity);
        const priority = priorityText(item.review_priority);
        const text = recordText(item) || "（无文字内容）";

        const effectiveRowInterlocutor = (decision.interlocutor_name || decision.interlocutor_relation)
          ? { name: decision.interlocutor_name, relation: decision.interlocutor_relation }
          : (["other_person", "non_self"].includes(decision.source_decision) ? resolveInterlocutor(groupIdx, item.recording_id) : null);

        const rowInterlocutorText = effectiveRowInterlocutor
          ? [relationText(effectiveRowInterlocutor.relation), effectiveRowInterlocutor.name].filter(Boolean).join(" · ")
          : "";
        const rowInterlocutorBadge = rowInterlocutorText
          ? `<span class="dialogue-interlocutor-pill" title="对谈人: ${escapeHtml(rowInterlocutorText)}">👤 ${escapeHtml(rowInterlocutorText)}</span>`
          : "";

        let statusTag = `<span class="tag unknown">待确认</span>`;
        if (reviewed) {
          const srcLbl = sourceText(decision.source_decision) || "已完成";
          statusTag = `<span class="tag reviewed-tag">${CHECK_ICON_SVG} ${escapeHtml(srcLbl)}</span>`;
        } else if (completed === 1) {
          statusTag = `<span class="tag partial-tag">进行中</span>`;
        }

        return `
        <div class="scene-dialogue-row ${!isMulti ? "standalone" : ""} review-card ${reviewed ? "reviewed" : ""}" data-card="${escapeHtml(item.segment_id)}">
          <div class="dialogue-row-main">
            <div class="dialogue-row-prefix">
              <input type="checkbox" class="review-item-checkbox" data-checkbox-segment="${escapeHtml(item.segment_id)}" ${isSelected ? "checked" : ""}>
              <span class="dialogue-time">${escapeHtml(time)}</span>
              ${hasAudio ? `
              <div class="dialogue-audio-controls">
                <button type="button" class="nudge-audio-btn nudge-prev" data-nudge="-1" title="向前扩展1秒听上文" aria-label="向前扩展 1 秒听上文 [-1s]">[-1s]</button>
                <button type="button" class="dialogue-play-btn review-audio-button" data-start="${audioStart}" data-end="${audioEnd}" title="试听本段 ${audioDuration}" aria-label="试听本段，${audioDuration}">${PLAY_ICON_SVG} <span class="audio-dur-text">${audioDuration}</span></button>
                <button type="button" class="nudge-audio-btn nudge-next" data-nudge="+1" title="向后扩展1秒听下文" aria-label="向后扩展 1 秒听下文 [+1s]">[+1s]</button>
                <audio class="review-audio" preload="none" src="${escapeHtml(audioClipUrl(audioStart, audioEnd))}"></audio>
              </div>
              ` : ""}
            </div>
            <div class="dialogue-text-wrap">
              <span class="dialogue-text">${highlightedText(text, query)}</span>
              ${decision.corrected_text ? `<span class="dialogue-correction">（修正：${escapeHtml(decision.corrected_text)}）</span>` : ""}
            </div>
            <div class="dialogue-quick-actions">
              ${rowInterlocutorBadge}
              ${statusTag}
              <button type="button" class="quick-tag-btn self-btn ${decision.source_decision === "self" ? "active" : ""}" data-row-action="self" data-segment="${escapeHtml(item.segment_id)}" title="[1] 标为本人发言">1 我本人</button>
              <button type="button" class="quick-tag-btn other-btn ${["other_person", "non_self"].includes(decision.source_decision) ? "active" : ""}" data-row-action="other_person" data-group-index="${groupIdx}" data-segment="${escapeHtml(item.segment_id)}" title="[2] 标为现场其他人（自动带入本组/本篇对谈人）">2 他人/对话</button>
              <button type="button" class="quick-tag-btn noise-btn ${["non_speech", "media"].includes(decision.source_decision) ? "active" : ""}" data-row-action="non_speech" data-segment="${escapeHtml(item.segment_id)}" title="[3] 标为噪音或媒体播放">3 噪音/媒体</button>
              <button type="button" class="dialogue-toggle-detail" data-toggle-detail="${escapeHtml(item.segment_id)}" title="查看声学指标与修正文本" aria-label="查看声学指标与修正文本">${MORE_ICON_SVG}</button>
            </div>
          </div>
          <div class="dialogue-detail-panel hidden" id="detail-${escapeHtml(item.segment_id)}">
            <div class="dialogue-detail-meta">
              ${sim ? `<span>${escapeHtml(sim)}</span>` : ""}
              <span>${escapeHtml(proximity)}</span>
              <span>优先级 ${priority}</span>
              <span>原因：${escapeHtml(decisionReasonText(item.decision_reason))}</span>
            </div>
            <div class="dialogue-detail-interlocutor">
              <span class="detail-label">对谈人:</span>
              <div class="relation-chips mini">
                ${["friend", "colleague", "family", "business", "stranger", "other"].map((relKey) => `
                  <button type="button" class="relation-chip mini ${decision.interlocutor_relation === relKey ? "active" : ""}" data-row-interlocutor-rel="${relKey}" data-segment="${escapeHtml(item.segment_id)}">
                    ${relationText(relKey)}
                  </button>
                `).join("")}
              </div>
              <input type="text" class="row-interlocutor-name-input" data-segment="${escapeHtml(item.segment_id)}" placeholder="姓名/称呼" value="${escapeHtml(decision.interlocutor_name || "")}" maxlength="50">
              <button type="button" class="row-interlocutor-save-btn" data-segment="${escapeHtml(item.segment_id)}">保存对谈人</button>
            </div>
            <div class="dialogue-detail-actions">
              <button type="button" class="review-button ${decision.transcript_decision === "correct" ? "active" : ""}" data-segment="${escapeHtml(item.segment_id)}" data-kind="transcript" data-value="correct">转写正确</button>
              <button type="button" class="review-button ${decision.transcript_decision === "incorrect" ? "active" : ""}" data-segment="${escapeHtml(item.segment_id)}" data-kind="transcript" data-value="incorrect">编辑修正文本</button>
              <button type="button" class="review-button ${decision.source_decision === "unclear" ? "active" : ""}" data-segment="${escapeHtml(item.segment_id)}" data-kind="source" data-value="unclear">无法判断</button>
            </div>
          </div>
        </div>`;
      }).join("");

      if (!isMulti) {
        return recBannerHtml + rowsHtml;
      }

      const isGroupAllReviewed = group.every((it) => isReviewed(it.segment_id));

      return `
      ${recBannerHtml}
      <section class="scene-group-card ${isGroupAllReviewed ? "all-reviewed" : ""}" data-group-index="${groupIdx}">
        <div class="scene-group-header">
          <div class="scene-group-title">
            <span class="scene-title-text">${escapeHtml(groupTitle)}</span>
            <span class="scene-count-badge">${group.length} 句连续对话</span>
            ${interlocutorBadge}
          </div>
          <div class="scene-group-actions">
            <button type="button" class="scene-action-btn select-all" data-group-action="select" data-group-index="${groupIdx}" title="勾选本组所有句子">勾选本组</button>
            <button type="button" class="scene-action-btn primary" data-group-action="other_person" data-group-index="${groupIdx}" title="[Shift+2] 整组一键标为现场其他人（自动带入所选对谈人）">👥 整组他人 (Shift+2)</button>
            <button type="button" class="scene-action-btn danger" data-group-action="non_speech" data-group-index="${groupIdx}" title="[Shift+3] 整组一键标为噪音或媒体播放">🔇 整组噪音/媒体 (Shift+3)</button>
            <button type="button" class="scene-action-btn success" data-group-action="self" data-group-index="${groupIdx}" title="[Shift+1] 整组一键标为本人发言">🙋 整组本人 (Shift+1)</button>
          </div>
        </div>
        <div class="scene-interlocutor-bar" data-group-index="${groupIdx}">
          <div class="interlocutor-bar-left">
            <span class="interlocutor-bar-label">👥 本组对谈人:</span>
            <div class="group-quick-contacts">
              ${getSavedContacts().slice(0, 4).map((c) => `
                <button type="button" class="group-quick-contact-pill ${currentName === c.name ? "active" : ""}" data-group-quick-name="${escapeHtml(c.name)}" data-group-quick-rel="${escapeHtml(c.relation)}" data-group-index="${groupIdx}" title="设为本组对谈人: ${escapeHtml(c.name)}" aria-label="设为本组对谈人：${escapeHtml(c.name)}">
                  ${escapeHtml(c.name)}
                </button>
              `).join("")}
            </div>
            <div class="relation-chips">
              ${["friend", "colleague", "family", "business", "stranger"].map((relKey) => `
                <button type="button" class="relation-chip ${currentRel === relKey ? "active" : ""}" data-group-action="set-relation" data-group-index="${groupIdx}" data-rel="${relKey}">
                  ${relationText(relKey)}
                </button>
              `).join("")}
            </div>
            <div class="interlocutor-input-wrap">
              <input type="text" class="group-interlocutor-name-input" data-group-index="${groupIdx}" data-group-key="${escapeHtml(groupKey)}" placeholder="姓名/称呼 (选填，自动存)" value="${escapeHtml(currentName)}" maxlength="50">
            </div>
          </div>
        </div>
        <div class="scene-group-body">
          ${rowsHtml}
        </div>
      </section>`;
    }).join("");

    const allVisibleSelected = filtered.length > 0 && filtered.every((it) => selectedSegmentIds.has(it.segment_id));

    const countLabel = $("count-label");
    if (countLabel) countLabel.textContent = `${pendingCount} 待确认 · ${reviewedCount}/${visibleRecords.length} 已完成${partialCount ? ` · ${partialCount} 条进行中` : ""}`;
    content.innerHTML = `<section class="review-overview">
      <div>
        <span class="review-kicker">人工复核工作台</span>
        <strong>${pendingCount} 条待确认 · ${reviewedCount}/${visibleRecords.length} 已完成</strong>
        <p>沉浸式单焦点键盘流：主角标只统计当前默认可见队列；按 1 本人、2 他人、3 噪音/媒体自动平滑滚入下一条；连贯场景支持 Shift+1/2/3 整组秒级归档；对谈人选填并自动保存。</p>
      </div>
      <div class="review-filter-group">
        <label class="pending-toggle"><input id="pending-toggle" type="checkbox" ${onlyPending ? "checked" : ""}> 只看未完成</label>
        <label class="pending-toggle"><input id="background-toggle" type="checkbox" ${showBackground ? "checked" : ""}> 显示背景/碎片</label>
        <div class="review-speed-selector" role="group" aria-label="试听倍速">
          <span class="speed-label">倍速:</span>
          <button type="button" class="speed-chip ${currentPlaybackRate === 1.0 ? "active" : ""}" data-speed="1.0">1.0x</button>
          <button type="button" class="speed-chip ${currentPlaybackRate === 1.25 ? "active" : ""}" data-speed="1.25">1.25x</button>
          <button type="button" class="speed-chip ${currentPlaybackRate === 1.5 ? "active" : ""}" data-speed="1.5">1.5x</button>
          <button type="button" class="speed-chip ${currentPlaybackRate === 2.0 ? "active" : ""}" data-speed="2.0">2.0x</button>
        </div>
        <select id="sort-select" class="review-sort-select">
          <option value="time" ${sortBy === "time" ? "selected" : ""}>按时间顺序</option>
          <option value="priority" ${sortBy === "priority" ? "selected" : ""}>按优先级排序</option>
        </select>
      </div>
    </section>
    <section class="global-interlocutor-bar">
      <div class="global-interlocutor-header">
        <div class="global-interlocutor-title-wrap">
          <span class="global-interlocutor-title">🎯 本篇录音主对谈人:</span>
          ${activeGlobalInterlocutor?.name ? `
            <span class="global-interlocutor-current-tag">
              👤 ${escapeHtml(activeGlobalInterlocutor.name)}
              <span class="rel-text">(${relationText(activeGlobalInterlocutor.relation)})</span>
              <button type="button" class="btn-clear-global-contact" aria-label="取消主对谈人设定" title="取消主对谈人设定">${CLOSE_ICON_SVG}</button>
            </span>
          ` : `<span class="global-interlocutor-none">未指定（点击下方常用人物，整篇录音所有“他人”自动归属此人）</span>`}
        </div>
        <div class="global-interlocutor-actions">
          ${activeGlobalInterlocutor?.name ? `
            <button type="button" class="btn-apply-all-other" title="将当前所有现场其他人发言一键同步为此人">⚡ 全篇他人同步为【${escapeHtml(activeGlobalInterlocutor.name)}】</button>
          ` : ""}
        </div>
      </div>
      <div class="quick-contacts-container">
        <span class="quick-contacts-label">常用人物一键切换:</span>
        <div class="quick-contacts-list">
          ${getSavedContacts().map((c) => {
            const isCurrent = activeGlobalInterlocutor?.name === c.name;
            return `
            <button type="button" class="quick-contact-pill ${isCurrent ? "active" : ""}" data-global-contact-name="${escapeHtml(c.name)}" data-global-contact-rel="${escapeHtml(c.relation)}" title="设为本篇对谈人：${escapeHtml(c.name)} (${relationText(c.relation)})" aria-label="设为本篇对谈人：${escapeHtml(c.name)}（${relationText(c.relation)}）">
              <span class="contact-icon">${c.icon || "👤"}</span>
              <span class="contact-name">${escapeHtml(c.name)}</span>
              <span class="contact-rel">${relationText(c.relation)}</span>
            </button>
            `;
          }).join("")}
          <button type="button" class="add-contact-pill" id="btn-add-quick-contact" title="添加新的常用对谈人">➕ 添加常用人物</button>
        </div>
      </div>
    </section>
    <div class="review-shortcuts" aria-label="快速复核快捷键">
      <span><kbd>1</kbd> 我本人</span>
      <span><kbd>2</kbd> 他人/对话</span>
      <span><kbd>3</kbd> 噪音/媒体</span>
      <span><kbd>Space</kbd> 试听</span>
      <span><kbd>J</kbd> / <kbd>K</kbd> 上下条</span>
      <span><kbd>Shift</kbd>+<kbd>1/2/3</kbd> 整组归类</span>
    </div>
    ${filtered.length ? `
    <section class="review-batch-bar">
      <div class="batch-select-wrap">
        <label class="batch-checkbox-label">
          <input type="checkbox" id="batch-select-all" ${allVisibleSelected ? "checked" : ""}>
          <span aria-live="polite" aria-atomic="true">全选当前筛选 (已选 <strong id="batch-selected-count">${selectedSegmentIds.size}</strong> / ${filtered.length} 条)</span>
        </label>
      </div>
      <div class="batch-action-buttons">
        <button type="button" class="batch-action-btn primary" data-bulk-action="other_person" title="选中的条目全部标为现场其他人（自动带入当前主对谈人）">👥 批量标为他人</button>
        <button type="button" class="batch-action-btn danger" data-bulk-action="non_speech" title="选中的条目全部标为噪音或媒体播放">🔇 批量标为噪音/媒体</button>
        <button type="button" class="batch-action-btn success" data-bulk-action="self" title="选中的条目全部标为本人 (转写正确)">${CHECK_ICON_SVG} 批量标为本人</button>
      </div>
    </section>
    <div class="record-list">${listHtml}</div>
    ` : `<div class="empty">${onlyPending ? "🎉 太棒了！当前筛选下所有待办条目均已完成复核。" : "没有找到匹配内容。"}</div>`}`;

    const toggle = document.getElementById("pending-toggle");
    if (toggle) toggle.addEventListener("change", () => { onlyPending = toggle.checked; draw(); });
    const backgroundToggle = document.getElementById("background-toggle");
    if (backgroundToggle) backgroundToggle.addEventListener("change", () => { showBackground = backgroundToggle.checked; draw(); });
    const sortSelect = document.getElementById("sort-select");
    if (sortSelect) sortSelect.addEventListener("change", () => { sortBy = sortSelect.value; draw(); });
  }

  function keyboardCards() {
    return [...content.querySelectorAll(".review-card")].filter((card) => !card.classList.contains("reviewed"));
  }

  function activateKeyboardCard(card, {scroll = true} = {}) {
    content.querySelectorAll(".review-card.keyboard-active").forEach((item) => item.classList.remove("keyboard-active"));
    if (!card) { keyboardSegmentId = null; return null; }
    card.classList.add("keyboard-active");
    keyboardSegmentId = card.dataset.card || null;
    if (scroll) card.scrollIntoView({behavior: preferredScrollBehavior(), block: "center"});
    window.focus();
    return card;
  }

  function currentKeyboardCard() {
    if (keyboardSegmentId) {
      const exact = [...content.querySelectorAll(".review-card")].find((card) => card.dataset.card === keyboardSegmentId);
      if (exact && !exact.classList.contains("reviewed")) return activateKeyboardCard(exact, {scroll: false});
    }
    return activateKeyboardCard(keyboardCards()[0], {scroll: false});
  }

  function moveKeyboardCard(delta) {
    const cards = keyboardCards();
    if (!cards.length) return activateKeyboardCard(null);
    const current = currentKeyboardCard();
    let index = cards.indexOf(current);
    if (index < 0) index = 0;
    activateKeyboardCard(cards[Math.max(0, Math.min(cards.length - 1, index + delta))]);
  }

  function triggerAudioAudition() {
    window.focus();
    let card = currentKeyboardCard();
    if (!card || !content.contains(card)) {
      const firstPending = content.querySelector(".review-card:not(.reviewed)");
      if (firstPending) {
        card = activateKeyboardCard(firstPending, {scroll: true});
      } else {
        const anyCard = content.querySelector(".review-card");
        if (anyCard) card = activateKeyboardCard(anyCard, {scroll: true});
      }
    }
    if (card) {
      const playBtn = card.querySelector(".review-audio-button");
      if (playBtn) {
        playBtn.click();
        return true;
      }
    }
    return false;
  }
  globalAuditionHandler = triggerAudioAudition;

  function isTypingTarget(target) {
    const tag = String(target?.tagName || "").toLowerCase();
    return ["input", "textarea", "select"].includes(tag) || Boolean(target?.isContentEditable);
  }

  function handleReviewShortcut(event) {
    if (isTypingTarget(event.target) || event.metaKey || event.ctrlKey || event.altKey) return;
    const key = event.key.toLowerCase();
    
    const reviewShortcutActions = {
    "1": "self",
    "2": "other_person",
    "3": "media",
    "4": "non_speech",
    "5": "unclear",
  };

  const isDigit1 = event.code === "Digit1" || key === "1" || key === "!";
    const isDigit2 = event.code === "Digit2" || key === "2" || key === "@";
    const isDigit3 = event.code === "Digit3" || key === "3" || key === "#";
    const isDigit4 = event.code === "Digit4" || key === "4" || key === "$";
    const isSpace = event.code === "Space" || key === " " || event.keyCode === 32;
    const isDown = event.code === "ArrowDown" || key === "j";
    const isUp = event.code === "ArrowUp" || key === "k";

    if (!isDigit1 && !isDigit2 && !isDigit3 && !isDigit4 && !isSpace && !isDown && !isUp) return;

    // Immediately stop propagation and prevent default!
    // This stops Space from scrolling the page or clicking any focused button/link in the browser.
    event.preventDefault();
    event.stopPropagation();

    if (isSpace) {
      triggerAudioAudition();
      return;
    }

    const card = currentKeyboardCard();
    if (!card) return;

    if (isDown || isUp) {
      moveKeyboardCard(isDown ? 1 : -1);
      return;
    }

    let targetAction = null;
    if (isDigit1) targetAction = "self";
    else if (isDigit2) targetAction = "other_person";
    else if (isDigit3 || isDigit4) targetAction = "non_speech";

    if (!targetAction) return;

    if (event.shiftKey) {
      const groupCard = card.closest(".scene-group-card");
      if (groupCard) {
        const groupBtn = groupCard.querySelector(`.scene-action-btn[data-group-action="${targetAction}"]`);
        if (groupBtn && !groupBtn.disabled) {
          groupBtn.click();
          return;
        }
      }
    }

    const btn = card.querySelector(`[data-row-action="${targetAction}"]`);
    if (btn && !btn.disabled) {
      btn.click();
    }
  }

  content.addEventListener("click", async (event) => {
    const clickedCard = event.target.closest(".review-card");
    if (clickedCard) {
      activateKeyboardCard(clickedCard, {scroll: false});
    }
    if (document.activeElement instanceof HTMLElement && document.activeElement.tagName === "BUTTON") {
      document.activeElement.blur();
    }
    const clearGlobalBtn = event.target.closest(".btn-clear-global-contact");
    if (clearGlobalBtn) {
      activeGlobalInterlocutor = null;
      draw();
      showToast("已清除全局主对谈人", "info");
      return;
    }

    const applyAllOtherBtn = event.target.closest(".btn-apply-all-other");
    if (applyAllOtherBtn && activeGlobalInterlocutor) {
      const otherItems = records.filter((it) => {
        const dec = decisions.get(it.segment_id) || {};
        return dec.source_decision === "other_person";
      });
      if (!otherItems.length) {
        showToast("当前没有已标记为他人的发言", "info");
        return;
      }
      const ids = otherItems.map((it) => it.segment_id);
      ids.forEach((id) => {
        const dec = decisions.get(id) || {};
        decisions.set(id, { ...dec, interlocutor_name: activeGlobalInterlocutor.name, interlocutor_relation: activeGlobalInterlocutor.relation });
      });
      await executeBatchReview(ids, "apply_interlocutor", {
        interlocutor_name: activeGlobalInterlocutor.name,
        interlocutor_relation: activeGlobalInterlocutor.relation,
      });
      showToast(`已将全篇 ${ids.length} 处他人发言统一同步为【${activeGlobalInterlocutor.name}】！`, "success");
      return;
    }

    const addContactBtn = event.target.closest("#btn-add-quick-contact");
    if (addContactBtn) {
      const name = prompt("请输入常用对谈人姓名/称呼 (例如: 妈妈、姐姐、张总、老婆):");
      if (name && name.trim()) {
        const relInput = prompt("请选择关系类别:\n1: 家人\n2: 同事\n3: 朋友\n4: 商务\n5: 其他", "1");
        const relMap = { "1": "family", "2": "colleague", "3": "friend", "4": "business", "5": "other" };
        const relation = relMap[relInput] || "family";
        saveContact(name.trim(), relation);
        activeGlobalInterlocutor = { name: name.trim(), relation };
        draw();
        showToast(`已添加常用人物【${name.trim()} (${relationText(relation)})】并设为主对谈人！`, "success");
      }
      return;
    }

    const quickContactPill = event.target.closest(".quick-contact-pill");
    if (quickContactPill) {
      const name = quickContactPill.dataset.globalContactName;
      const rel = quickContactPill.dataset.globalContactRel;
      activeGlobalInterlocutor = { name, relation: rel };
      saveContact(name, rel);
      
      const otherItems = records.filter((it) => {
        const dec = decisions.get(it.segment_id) || {};
        return dec.source_decision === "other_person";
      });
      if (otherItems.length > 0) {
        const ids = otherItems.map((it) => it.segment_id);
        ids.forEach((id) => {
          const dec = decisions.get(id) || {};
          decisions.set(id, { ...dec, interlocutor_name: name, interlocutor_relation: rel });
        });
        await executeBatchReview(ids, "apply_interlocutor", { interlocutor_name: name, interlocutor_relation: rel });
        showToast(`已设为主对谈人：【${name} (${relationText(rel)})】，已自动同步全篇 ${ids.length} 处他人发言！`, "success");
      } else {
        showToast(`已设为主对谈人：【${name} (${relationText(rel)})】，后续标为他人时自动关联！`, "info");
        draw();
      }
      return;
    }

    const recContactBtn = event.target.closest(".rec-contact-btn");
    if (recContactBtn) {
      const recId = recContactBtn.dataset.recId;
      const name = recContactBtn.dataset.contactName;
      const rel = recContactBtn.dataset.contactRel;
      recordingInterlocutors.set(recId, { name, relation: rel });
      saveContact(name, rel);

      const recOtherItems = records.filter((it) => {
        if (it.recording_id !== recId) return false;
        const dec = decisions.get(it.segment_id) || {};
        return dec.source_decision === "other_person";
      });
      if (recOtherItems.length > 0) {
        const ids = recOtherItems.map((it) => it.segment_id);
        ids.forEach((id) => {
          const dec = decisions.get(id) || {};
          decisions.set(id, { ...dec, interlocutor_name: name, interlocutor_relation: rel });
        });
        await executeBatchReview(ids, "apply_interlocutor", { interlocutor_name: name, interlocutor_relation: rel });
        showToast(`录音【${recId}】对谈人已设为【${name}】并同步此录音所有他人发言！`, "success");
      } else {
        showToast(`录音【${recId}】主对谈人已设为【${name}】！`, "info");
        draw();
      }
      return;
    }

    const groupQuickPill = event.target.closest(".group-quick-contact-pill");
    if (groupQuickPill) {
      const groupIdx = Number(groupQuickPill.dataset.groupIndex);
      const group = currentSceneGroups[groupIdx];
      if (!group || !group.length) return;
      const name = groupQuickPill.dataset.groupQuickName;
      const rel = groupQuickPill.dataset.groupQuickRel;
      const groupKey = group[0].segment_id;
      groupInterlocutorDrafts.set(groupKey, { name, relation: rel });
      saveContact(name, rel);
      
      const otherItems = group.filter((it) => {
        const dec = decisions.get(it.segment_id) || {};
        return dec.source_decision === "other_person";
      });
      if (otherItems.length > 0) {
        const ids = otherItems.map((it) => it.segment_id);
        ids.forEach((id) => {
          const dec = decisions.get(id) || {};
          decisions.set(id, { ...dec, interlocutor_name: name, interlocutor_relation: rel });
        });
        await executeBatchReview(ids, "apply_interlocutor", { interlocutor_name: name, interlocutor_relation: rel });
      } else {
        showToast(`已将本组对谈人设为【${name} (${relationText(rel)})】！`, "info");
        draw();
      }
      return;
    }

    const selectAllInput = event.target.closest("#batch-select-all");
    if (selectAllInput) {
      const currentCards = [...content.querySelectorAll(".review-item-checkbox")];
      if (selectAllInput.checked) {
        currentCards.forEach((cb) => {
          cb.checked = true;
          selectedSegmentIds.add(cb.dataset.checkboxSegment);
        });
      } else {
        currentCards.forEach((cb) => {
          cb.checked = false;
          selectedSegmentIds.delete(cb.dataset.checkboxSegment);
        });
      }
      const countEl = document.getElementById("batch-selected-count");
      if (countEl) countEl.textContent = selectedSegmentIds.size;
      return;
    }

    const itemCheckbox = event.target.closest(".review-item-checkbox");
    if (itemCheckbox) {
      const segId = itemCheckbox.dataset.checkboxSegment;
      if (itemCheckbox.checked) {
        selectedSegmentIds.add(segId);
      } else {
        selectedSegmentIds.delete(segId);
      }
      const countEl = document.getElementById("batch-selected-count");
      if (countEl) countEl.textContent = selectedSegmentIds.size;
      const selectAllEl = document.getElementById("batch-select-all");
      const allBoxes = [...content.querySelectorAll(".review-item-checkbox")];
      if (selectAllEl && allBoxes.length) {
        selectAllEl.checked = allBoxes.every((cb) => cb.checked);
      }
      return;
    }

    const groupActionBtn = event.target.closest("[data-group-action]");
    if (groupActionBtn) {
      const groupIdx = Number(groupActionBtn.dataset.groupIndex);
      const action = groupActionBtn.dataset.groupAction;
      const group = currentSceneGroups[groupIdx];
      if (!group || !group.length) return;
      if (action === "select") {
        group.forEach((it) => selectedSegmentIds.add(it.segment_id));
        draw();
        return;
      }
      if (action === "set-relation") {
        const rel = groupActionBtn.dataset.rel;
        const groupKey = group[0].segment_id;
        const currentDraft = groupInterlocutorDrafts.get(groupKey) || {};
        const existingRel = group.map((it) => (decisions.get(it.segment_id) || {}).interlocutor_relation).find(Boolean) || "";
        const baseRel = currentDraft.relation !== undefined ? currentDraft.relation : existingRel;
        const newRel = baseRel === rel ? "" : rel;
        groupInterlocutorDrafts.set(groupKey, { ...currentDraft, relation: newRel });
        
        // Auto-sync to any other_person items already in this group
        const existingOtherItems = group.filter((it) => {
          const dec = decisions.get(it.segment_id) || {};
          return dec.source_decision === "other_person";
        });
        if (existingOtherItems.length > 0) {
          const ids = existingOtherItems.map((it) => it.segment_id);
          ids.forEach((id) => {
            const dec = decisions.get(id) || {};
            decisions.set(id, { ...dec, interlocutor_relation: newRel });
          });
          executeBatchReview(ids, "apply_interlocutor", { interlocutor_relation: newRel });
        } else {
          showToast(newRel ? `对谈人关系已设为【${relationText(newRel)}】` : "已取消对谈人关系", "info");
        }

        const scrollTop = window.scrollY;
        draw();
        window.scrollTo({top: scrollTop, behavior: "instant"});
        return;
      }
      if (action === "apply-interlocutor") {
        groupActionBtn.disabled = true;
        groupActionBtn.textContent = "保存中…";
        await applyGroupInterlocutor(groupIdx);
        return;
      }
      const ids = group.map((it) => it.segment_id);
      groupActionBtn.disabled = true;
      groupActionBtn.textContent = "保存中…";
      const extraPatch = {};
      if (action === "other_person") {
        const targetInterlocutor = resolveInterlocutor(groupIdx, group[0]?.recording_id);
        if (targetInterlocutor) {
          if (targetInterlocutor.relation) extraPatch.interlocutor_relation = targetInterlocutor.relation;
          if (targetInterlocutor.name) extraPatch.interlocutor_name = targetInterlocutor.name;
        }
      }
      await executeBatchReview(ids, action, extraPatch);
      return;
    }

    const rowActionBtn = event.target.closest("[data-row-action]");
    if (rowActionBtn) {
      const segId = rowActionBtn.dataset.segment;
      const action = rowActionBtn.dataset.rowAction;
      const patch = action === "self"
        ? { source_decision: "self", transcript_decision: "correct" }
        : { source_decision: action, transcript_decision: "correct" };
      if (action === "other_person") {
        const groupIdx = rowActionBtn.dataset.groupIndex !== undefined ? Number(rowActionBtn.dataset.groupIndex) : -1;
        const targetItem = records.find((it) => it.segment_id === segId);
        const targetInterlocutor = resolveInterlocutor(groupIdx, targetItem?.recording_id);
        if (targetInterlocutor) {
          if (targetInterlocutor.relation) patch.interlocutor_relation = targetInterlocutor.relation;
          if (targetInterlocutor.name) patch.interlocutor_name = targetInterlocutor.name;
        }
      }

      // Calculate next unreviewed target ID BEFORE updating decisions & calling draw()
      const currentPending = [...content.querySelectorAll(".review-card:not(.reviewed)")];
      const currentIdx = currentPending.findIndex((c) => c.dataset.card === segId);
      let nextTargetId = null;
      if (currentIdx >= 0) {
        if (currentIdx + 1 < currentPending.length) {
          nextTargetId = currentPending[currentIdx + 1].dataset.card;
        } else if (currentIdx > 0) {
          nextTargetId = currentPending[0].dataset.card;
        }
      }

      const prev = decisions.get(segId) || {};
      decisions.set(segId, { ...prev, ...patch, segment_id: segId });
      selectedSegmentIds.delete(segId);
      const scrollTop = window.scrollY;
      draw();
      try {
        if (nextTargetId) {
          const nextCard = content.querySelector(`.review-card[data-card="${nextTargetId}"]`);
          if (nextCard && !nextCard.classList.contains("reviewed")) {
            nextCard.scrollIntoView({behavior: preferredScrollBehavior(), block: "center"});
            activateKeyboardCard(nextCard, {scroll: false});
          } else {
            const firstPending = content.querySelector(".review-card:not(.reviewed)");
            if (firstPending) {
              firstPending.scrollIntoView({behavior: preferredScrollBehavior(), block: "center"});
              activateKeyboardCard(firstPending, {scroll: false});
            } else {
              activateKeyboardCard(null);
            }
          }
        } else {
          const firstPending = content.querySelector(".review-card:not(.reviewed)");
          if (firstPending) {
            firstPending.scrollIntoView({behavior: preferredScrollBehavior(), block: "center"});
            activateKeyboardCard(firstPending, {scroll: false});
          } else {
            window.scrollTo({top: scrollTop, behavior: "instant"});
            activateKeyboardCard(null);
          }
        }
      } catch (_) {}
      try {
        const saved = await saveReview(segId, patch);
        decisions.set(segId, saved);
        showToast("已保存", "success");
      } catch (err) {
        showToast(`保存失败：${err.message}`, "error");
        draw();
      }
      return;
    }

    const rowRelBtn = event.target.closest("[data-row-interlocutor-rel]");
    if (rowRelBtn) {
      const segId = rowRelBtn.dataset.segment;
      const rel = rowRelBtn.dataset.rowInterlocutorRel;
      const existing = decisions.get(segId) || {};
      const newRel = existing.interlocutor_relation === rel ? "" : rel;
      const nameInput = content.querySelector(`.row-interlocutor-name-input[data-segment="${segId}"]`);
      const name = nameInput ? nameInput.value.trim() : (existing.interlocutor_name || "");
      const patch = {
        interlocutor_relation: newRel,
        interlocutor_name: name,
        source_decision: existing.source_decision || "other_person",
        transcript_decision: existing.transcript_decision || "correct",
      };
      const optimistic = { ...existing, ...patch, segment_id: segId };
      decisions.set(segId, optimistic);
      const scrollTop = window.scrollY;
      draw();
      window.scrollTo({top: scrollTop, behavior: "instant"});
      try {
        const saved = await saveReview(segId, patch);
        decisions.set(segId, saved);
        showToast(`已更新对谈人：${relationText(newRel) || "未指定"}`, "success");
      } catch (err) {
        showToast(`保存失败: ${err.message}`, "error");
        draw();
      }
      return;
    }

    const rowSaveBtn = event.target.closest(".row-interlocutor-save-btn");
    if (rowSaveBtn) {
      const segId = rowSaveBtn.dataset.segment;
      const existing = decisions.get(segId) || {};
      const nameInput = content.querySelector(`.row-interlocutor-name-input[data-segment="${segId}"]`);
      const name = nameInput ? nameInput.value.trim() : "";
      const patch = {
        interlocutor_name: name,
        interlocutor_relation: existing.interlocutor_relation || "friend",
        source_decision: existing.source_decision || "other_person",
        transcript_decision: existing.transcript_decision || "correct",
      };
      const optimistic = { ...existing, ...patch, segment_id: segId };
      decisions.set(segId, optimistic);
      const scrollTop = window.scrollY;
      draw();
      window.scrollTo({top: scrollTop, behavior: "instant"});
      try {
        const saved = await saveReview(segId, patch);
        decisions.set(segId, saved);
        showToast("已保存对谈人信息", "success");
      } catch (err) {
        showToast(`保存失败: ${err.message}`, "error");
        draw();
      }
      return;
    }

    const toggleDetailBtn = event.target.closest("[data-toggle-detail]");
    if (toggleDetailBtn) {
      const segId = toggleDetailBtn.dataset.toggleDetail;
      const panel = document.getElementById(`detail-${segId}`);
      if (panel) panel.classList.toggle("hidden");
      return;
    }

    const bulkBtn = event.target.closest("[data-bulk-action]");
    if (bulkBtn) {
      const action = bulkBtn.dataset.bulkAction;
      const visibleCheckboxes = [...content.querySelectorAll(".review-item-checkbox")];
      const visibleIds = visibleCheckboxes.map((cb) => cb.dataset.checkboxSegment);
      let targets = [];
      if (selectedSegmentIds.size > 0) {
        targets = visibleIds.filter((id) => selectedSegmentIds.has(id));
      } else {
        targets = visibleIds;
      }
      if (!targets.length) {
        alert("当前没有可操作的条目。");
        return;
      }
      if (selectedSegmentIds.size === 0) {
        const actionNames = {
          other_person: "现场其他人",
          media: "噪音 / 媒体",
          non_speech: "噪音 / 媒体",
          self: "本人（且转写正确）",
        };
        const actionName = actionNames[action] || action;
        if (!confirm(`未勾选单个条目。确定将当前可见的全部 ${targets.length} 条批量标为【${actionName}】吗？`)) {
          return;
        }
      }
      bulkBtn.disabled = true;
      bulkBtn.textContent = "批量保存中…";
      const extraPatch = {};
      if (action === "other_person") {
        const targetInterlocutor = resolveInterlocutor(-1, null);
        if (targetInterlocutor) {
          if (targetInterlocutor.relation) extraPatch.interlocutor_relation = targetInterlocutor.relation;
          if (targetInterlocutor.name) extraPatch.interlocutor_name = targetInterlocutor.name;
        }
      }
      await executeBatchReview(targets, action, extraPatch);
      return;
    }

    const speedChip = event.target.closest(".speed-chip");
    if (speedChip) {
      currentPlaybackRate = parseFloat(speedChip.dataset.speed || "1.0");
      try { writeVersionedValue("life-logger-review-rate", String(currentPlaybackRate)); } catch (_) {}
      content.querySelectorAll(".speed-chip").forEach((btn) => {
        btn.classList.toggle("active", btn === speedChip);
      });
      content.querySelectorAll(".review-audio").forEach((a) => {
        if (!a.paused) a.playbackRate = currentPlaybackRate;
      });
      return;
    }

    const nudgeBtn = event.target.closest(".nudge-audio-btn");
    if (nudgeBtn) {
      event.stopPropagation();
      const container = nudgeBtn.closest(".dialogue-audio-controls");
      const playBtn = container?.querySelector(".review-audio-button");
      const audio = container?.querySelector(".review-audio");
      if (!playBtn || !audio) return;
      let start = parseFloat(playBtn.dataset.start || "0");
      let end = parseFloat(playBtn.dataset.end || "0");
      const delta = parseFloat(nudgeBtn.dataset.nudge || "0");
      if (delta < 0) {
        start = Math.max(0, start - 1.0);
      } else {
        end = end + 1.0;
      }
      playBtn.dataset.start = String(start);
      playBtn.dataset.end = String(end);
      const durSpan = playBtn.querySelector(".audio-dur-text");
      if (durSpan) durSpan.textContent = formatSeconds(end - start);
      audio.src = audioClipUrl(start, end);
      audio.currentTime = 0;
      content.querySelectorAll(".review-audio").forEach((c) => { if (c !== audio) c.pause(); });
      content.querySelectorAll(".review-audio-button").forEach((c) => { if (c !== playBtn) c.classList.remove("playing"); });
      try {
        audio.playbackRate = currentPlaybackRate;
        await audio.play();
        playBtn.classList.add("playing");
        playBtn.innerHTML = `${PAUSE_ICON_SVG} <span class="audio-dur-text">${formatSeconds(end - start)}</span>`;
        audio.onended = () => {
          playBtn.classList.remove("playing");
          playBtn.innerHTML = `${PLAY_ICON_SVG} <span class="audio-dur-text">${formatSeconds(end - start)}</span>`;
        };
      } catch (e) {
        console.error(e);
      }
      return;
    }

    const playButton = event.target.closest(".review-audio-button");
    if (playButton) {
      const card = playButton.closest(".review-card");
      const audio = card?.querySelector(".review-audio");
      if (!audio) return;
      const start = Number(playButton.dataset.start) || 0;
      const end = Number(playButton.dataset.end) || start;
      const durLabel = formatSeconds(end > start ? end - start : 0);
      if (!audio.paused) {
        audio.pause();
        playButton.classList.remove("playing");
        playButton.innerHTML = `${PLAY_ICON_SVG} <span class="audio-dur-text">${durLabel}</span>`;
        return;
      }
      content.querySelectorAll(".review-audio").forEach((candidate) => {
        if (candidate !== audio) candidate.pause();
      });
      content.querySelectorAll(".review-audio-button").forEach((candidate) => {
        if (candidate !== playButton) {
          candidate.classList.remove("playing");
          const cStart = Number(candidate.dataset.start) || 0;
          const cEnd = Number(candidate.dataset.end) || cStart;
          const cDur = formatSeconds(cEnd > cStart ? cEnd - cStart : 0);
          candidate.innerHTML = `${PLAY_ICON_SVG} <span class="audio-dur-text">${cDur}</span>`;
        }
      });
      try {
        const finish = () => {
          audio.pause();
          audio.currentTime = 0;
          playButton.classList.remove("playing");
          playButton.innerHTML = `${PLAY_ICON_SVG} <span class="audio-dur-text">${durLabel}</span>`;
        };
        audio.onerror = () => {
          playButton.classList.remove("playing");
          playButton.innerHTML = `${PLAY_ICON_SVG} <span class="audio-dur-text">${durLabel}</span>`;
        };
        audio.onended = finish;
        if (audio.ended) audio.currentTime = 0;
        audio.playbackRate = currentPlaybackRate;
        await audio.play();
        playButton.classList.add("playing");
        playButton.innerHTML = `${PAUSE_ICON_SVG} <span class="audio-dur-text">${durLabel}</span>`;
      } catch (error) {
        playButton.classList.remove("playing");
        playButton.innerHTML = `${PLAY_ICON_SVG} <span class="audio-dur-text">${durLabel}</span>`;
        alert(`音频播放失败：${error.message}`);
      }
      return;
    }

    const buttonEl = event.target.closest(".review-button");
    if (!buttonEl || buttonEl.disabled) return;
    const segmentId = buttonEl.dataset.segment;
    const kind = buttonEl.dataset.kind;
    const value = buttonEl.dataset.value;
    const existing = decisions.get(segmentId) || {};
    let patch;
    if (kind === "source") {
      if (value === "other_person") {
        patch = { source_decision: "other_person", transcript_decision: existing.transcript_decision || "correct" };
      } else if (value === "media") {
        patch = { source_decision: "media", transcript_decision: existing.transcript_decision || "correct" };
      } else if (value === "non_speech") {
        patch = { source_decision: "non_speech", transcript_decision: existing.transcript_decision || "correct" };
      } else if (value === "unclear") {
        patch = { source_decision: "unclear", transcript_decision: existing.transcript_decision || "unclear" };
      } else if (value === "self") {
        patch = { source_decision: "self", transcript_decision: existing.transcript_decision || "correct" };
      }
    } else if (kind === "transcript") {
      if (value === "correct") {
        patch = { transcript_decision: "correct", source_decision: existing.source_decision || "self" };
      } else if (value === "incorrect") {
        const sourceItem = records.find((item) => item.segment_id === segmentId);
        const originalText = recordText(sourceItem || {});
        const existingText = existing?.corrected_text || originalText;
        const correction = await openCorrectionEditor("transcript", {originalText, correctedText: existingText});
        if (!correction) return;
        patch = {
          transcript_decision: "incorrect",
          corrected_text: correction.corrected_text,
          source_decision: existing.source_decision || "self",
        };
      } else if (value === "unclear") {
        patch = { transcript_decision: "unclear", source_decision: existing.source_decision || "unclear" };
      }
    }
    if (!patch) return;

    const optimistic = { ...existing, ...patch, segment_id: segmentId };
    decisions.set(segmentId, optimistic);
    selectedSegmentIds.delete(segmentId);

    const scrollTop = window.scrollY;
    draw();
    window.scrollTo({top: scrollTop, behavior: "instant"});
    currentKeyboardCard();

    try {
      const saved = await saveReview(segmentId, patch);
      decisions.set(segmentId, saved);
    } catch (error) {
      alert(`复核保存失败：${error.message}`);
      draw();
    }
  });

  content.addEventListener("input", (event) => {
    const input = event.target.closest(".group-interlocutor-name-input");
    if (input) {
      const groupKey = input.dataset.groupKey;
      const currentDraft = groupInterlocutorDrafts.get(groupKey) || {};
      currentDraft.name = input.value;
      groupInterlocutorDrafts.set(groupKey, currentDraft);
    }
  });

  content.addEventListener("change", async (event) => {
    const input = event.target.closest(".group-interlocutor-name-input");
    if (input) {
      const groupIdx = Number(input.dataset.groupIndex);
      const group = currentSceneGroups[groupIdx];
      if (!group || !group.length) return;
      const groupKey = input.dataset.groupKey;
      const val = input.value.trim();
      const currentDraft = groupInterlocutorDrafts.get(groupKey) || {};
      groupInterlocutorDrafts.set(groupKey, { ...currentDraft, name: val });
      if (val) saveContact(val, currentDraft.relation || "other");

      const otherPersonItems = group.filter((it) => {
        const dec = decisions.get(it.segment_id) || {};
        return dec.source_decision === "other_person";
      });
      if (otherPersonItems.length > 0) {
        const idsToUpdate = otherPersonItems.map((it) => it.segment_id);
        idsToUpdate.forEach((id) => {
          const dec = decisions.get(id) || {};
          decisions.set(id, { ...dec, interlocutor_name: val });
        });
        await executeBatchReview(idsToUpdate, "apply_interlocutor", { interlocutor_name: val });
      }
      if (val) {
        showToast(`对谈人姓名已保存：${val}`, "info");
      }
    }
  });

  content.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      const input = event.target.closest(".group-interlocutor-name-input");
      if (input) {
        event.preventDefault();
        input.blur();
        return;
      }
      const rowInput = event.target.closest(".row-interlocutor-name-input");
      if (rowInput) {
        event.preventDefault();
        const segId = rowInput.dataset.segment;
        const rowSave = content.querySelector(`.row-interlocutor-save-btn[data-segment="${segId}"]`);
        if (rowSave) rowSave.click();
        return;
      }
    }
  });

  document.addEventListener("keydown", handleReviewShortcut, true);
  search.value = "";
  setupSearch((value) => { query = value; draw(); currentKeyboardCard(); });
  draw();
  currentKeyboardCard();
}

// 与 web/app.js 一致：0–1 比例取整为整数百分比（Intl.NumberFormat percent）。
function percent(value) {
  return value == null || !Number.isFinite(Number(value)) ? "—" : formatPercent(Number(value));
}

function viewerEventSeconds(event) {
  const absolute = Date.parse(event?.time?.absolute || "");
  if (Number.isFinite(absolute)) return absolute / 1000;
  const relative = Number(event?.time?.relative_seconds);
  return Number.isFinite(relative) ? relative : null;
}

function normalizedViewerEventText(event) {
  let value = String(event?.title || event?.evidence?.quote || "").trim().toLowerCase();
  let changed = true;
  while (changed) {
    changed = false;
    const stripped = value.replace(/^[\s，,。！？!?；;：:]+/u, "");
    for (const prefix of ["嗯", "呃", "啊", "这个", "那个", "然后", "就是", "我觉得", "我想"]) {
      if (stripped.startsWith(prefix)) {
        value = stripped.slice(prefix.length);
        changed = true;
        break;
      }
    }
    if (!changed) value = stripped;
  }
  return value.replace(/[^\p{L}\p{N}]+/gu, "");
}

function viewerEventNgrams(value, size = 3) {
  if (!value) return new Set();
  if (value.length <= size) return new Set([value]);
  const result = new Set();
  for (let index = 0; index <= value.length - size; index += 1) result.add(value.slice(index, index + size));
  return result;
}

function viewerEventsAreDuplicates(left, right) {
  const leftCategories = [...(left.categories || [])].map(canonicalViewerCategory).sort().join("|");
  const rightCategories = [...(right.categories || [])].map(canonicalViewerCategory).sort().join("|");
  if (leftCategories !== rightCategories) return false;
  if (left?.activity_type && right?.activity_type && left.activity_type !== right.activity_type) return false;
  const leftText = normalizedViewerEventText(left);
  const rightText = normalizedViewerEventText(right);
  if (!leftText || !rightText) return false;
  if (leftText === rightText) return true;
  const [shorter, longer] = [leftText, rightText].sort((a, b) => a.length - b.length);
  if (shorter.length >= 8 && longer.includes(shorter) && shorter.length / longer.length >= 0.62) return true;
  const leftGrams = viewerEventNgrams(leftText);
  const rightGrams = viewerEventNgrams(rightText);
  const union = new Set([...leftGrams, ...rightGrams]);
  if (!union.size) return false;
  let overlap = 0;
  leftGrams.forEach((gram) => { if (rightGrams.has(gram)) overlap += 1; });
  return overlap / union.size >= 0.64;
}

function viewerEventIsMeaningful(event) {
  if (event?.review_status === "rejected") return false;
  if (event?.source_type === "manual" || event?.review_status === "confirmed") return true;
  const categories = new Set((event.categories || []).map(canonicalViewerCategory));
  if ([...categories].some((category) => ["routine", "expense", "food", "exercise", "todo"].includes(category))) return true;
  if (categories.has("idea")) return normalizedViewerEventText(event).length >= 6;
  if ([...categories].some((category) => ["work_communication", "work", "communication"].includes(category))) {
    const text = `${event?.title || ""} ${event?.evidence?.quote || ""}`;
    return ["开会", "会议", "沟通", "电话", "汇报", "修复", "提交", "完成", "测试", "开发", "修改", "处理", "上线", "部署", "面试", "评审", "排查"].some((marker) => text.includes(marker));
  }
  return false;
}

function viewerTimelineWorthy(event) {
  if (event?.review_status === "rejected") return false;
  if (event?.source_type === "manual" || event?.review_status === "confirmed") return true;
  const categories = new Set((event.categories || []).map(canonicalViewerCategory));
  if ([...categories].some((category) => ["routine", "expense", "food", "exercise"].includes(category))) return true;
  if (!categories.has("work_communication")) return false;
  if (event?.capture_mode === "active") return true;
  const text = `${event?.title || ""} ${event?.evidence?.quote || ""}`;
  return ["开会", "会议", "汇报", "修复", "断网", "切换工作", "提交", "完成", "测试", "开发", "修改", "处理", "上线", "部署", "面试", "电话沟通", "和同事沟通", "跟同事沟通"].some((marker) => text.includes(marker));
}

function curateViewerEvents(events) {
  const ordered = [...(events || [])]
    .filter(viewerEventIsMeaningful)
    .sort((a, b) => (viewerEventSeconds(a) ?? 0) - (viewerEventSeconds(b) ?? 0));
  const result = [];
  for (const event of ordered) {
    const seconds = viewerEventSeconds(event);
    const duplicate = [...result].reverse().find((previous) => {
      const previousSeconds = viewerEventSeconds(previous);
      if (seconds !== null && previousSeconds !== null) {
        const delta = seconds - previousSeconds;
        if (delta < 0 || delta > 180) return false;
      }
      return viewerEventsAreDuplicates(previous, event);
    });
    if (!duplicate) result.push(event);
  }
  return result;
}
function viewerSummaryDate(summary) {
  const direct = String(summary?.date || "").trim();
  if (/^\d{4}-\d{2}-\d{2}$/.test(direct)) return direct;
  const match = String(artifactPath || "").match(/result\/(\d{4}-\d{2}-\d{2})\//);
  return match ? match[1] : "";
}

async function fetchDailyExpenseTotal(date) {
  if (!date) return null;
  try {
    const response = await fetch(
      `/api/daily?date=${encodeURIComponent(date)}&scope=provisional&view=display`,
      {credentials: "same-origin"},
    );
    if (!response.ok) return null;
    const payload = await response.json();
    const ledger = payload?.expense_ledger;
    if (!ledger || typeof ledger !== "object") return null;
    const raw = ledger.totals_by_currency?.CNY ?? ledger.total_amount;
    if (raw === null || raw === undefined || raw === "") return null;
    const total = Number(raw);
    return Number.isFinite(total) ? total : null;
  } catch (_) {
    return null;
  }
}

async function renderDailySummary(summary) {
  const total = await fetchDailyExpenseTotal(viewerSummaryDate(summary));
  const totalHtml = total === null ? "—" : formatMoney(total);
  const categories = Object.entries(summary.category_summary || {}).map(([key, value]) => `<span class="chip">${escapeHtml(CATEGORY_LABELS[key] || key)} ${Number(value?.count || 0)}</span>`).join("");
  const timeline = (summary.timeline || []).map((event) => `<div class="timeline-item"><div class="timeline-time">${escapeHtml(formatTime(event?.time?.absolute, event?.time?.relative_seconds, itemLocalClock(event)))}</div><div><div class="timeline-title">${escapeHtml(event.title || "未命名事件")}</div>${event.evidence?.quote ? `<div class="timeline-evidence">证据：${escapeHtml(event.evidence.quote)}</div>` : ""}</div></div>`).join("");
  $("viewer-content").innerHTML = `<div class="summary-grid"><div class="metric"><span>生活事件</span><strong>${Number(summary.event_count || 0)}</strong></div><div class="metric"><span>消费</span><strong>${totalHtml}</strong></div><div class="metric"><span>日期</span><strong style="font-size:16px">${escapeHtml(summary.date || "—")}</strong></div><div class="metric"><span>时区</span><strong style="font-size:16px">${escapeHtml(summary.timezone || "—")}</strong></div></div><section class="section"><h2>事件分类</h2><div class="chips">${categories || `<span class="subtitle">暂无分类事件</span>`}</div></section><section class="section"><h2>时间轴</h2><div class="timeline">${timeline || `<div class="empty">暂无生活事件。</div>`}</div></section>`;
}

function mergeDailySummary(summary, extraEvents) {
  const generated = Array.isArray(summary?.timeline) ? summary.timeline : [];
  const unique = new Map();
  for (const event of [...generated, ...(Array.isArray(extraEvents) ? extraEvents : [])]) {
    const key = event?.event_id || [
      event?.time?.absolute || event?.time?.relative_seconds || "",
      event?.title || "",
    ].join("::");
    unique.set(key, event);
  }
  const workspaceEvents = curateViewerEvents([...unique.values()]);
  const timeline = workspaceEvents.filter(viewerTimelineWorthy);
  const categorySummary = {};
  for (const event of workspaceEvents) {
    const categories = Array.isArray(event?.categories) && event.categories.length
      ? event.categories
      : [event?.activity_type || "other"];
    for (const category of categories) {
      const item = categorySummary[category] || {count: 0, highlights: []};
      item.count += 1;
      if (event?.title && item.highlights.length < 5) item.highlights.push(event.title);
      categorySummary[category] = item;
    }
  }
  return {
    ...summary,
    event_count: workspaceEvents.length,
    timeline,
    category_summary: categorySummary,
    // 消费总额不在这里求和；renderDailySummary 统一读 /api/daily 的 expense_ledger。
    expense_total_cny: null,
  };
}

function renderEvents(events) {
  const content = $("viewer-content"), toolbar = $("toolbar"), search = $("search-input");
  toolbar.classList.remove("hidden");
  function draw(query = "") {
    const normalized = query.trim().toLowerCase();
    const filtered = normalized ? events.filter((item) => searchableText({...item, text: item.title}).includes(normalized)) : events;
    const countLabel = $("count-label");
    if (countLabel) countLabel.textContent = `${formatCount(filtered.length)} / ${formatCount(events.length)} 条`;
    content.innerHTML = filtered.length ? `<div class="record-list">${filtered.map((event) => {
      const rejected = event.review_status === "rejected";
      const time = formatTime(event?.time?.absolute, event?.time?.relative_seconds, itemLocalClock(event));
      const categories = (event.categories || []).map((key) => CATEGORY_LABELS[key] || key).join(" · ");
      const amount = Number(event?.data?.expense?.amount);
      const quote = event?.evidence?.quote || event?.evidence?.note || "";
      return `<article class="record-card ${rejected ? "reviewed" : ""}" data-event-id="${escapeHtml(event.event_id || "")}">
        <div class="record-meta"><span class="record-time">${escapeHtml(time)}</span><span class="tag">${escapeHtml(categories || "未分类")}</span>${event.correction_status === "corrected" ? `<span class="tag reviewed-tag">已人工修正</span>` : ""}${rejected ? `<span class="tag unknown">已排除</span>` : ""}</div>
        <p class="record-text">${highlightedText(event.title || "未命名事件", query)}</p>
        ${quote ? `<div class="record-quote"><span>原始证据</span><p>${highlightedText(quote, query)}</p></div>` : ""}
        <div class="record-foot">${Number.isFinite(amount) ? `<span>金额 ${formatMoney(amount)}</span>` : ""}${event?.evidence?.correction_note ? `<span>修正备注：${escapeHtml(event.evidence.correction_note)}</span>` : ""}</div>
        <div class="review-actions">
          <button type="button" class="review-button event-edit" data-event-id="${escapeHtml(event.event_id || "")}" ${rejected ? "disabled" : ""}>编辑事件</button>
          <button type="button" class="review-button event-reject" data-event-id="${escapeHtml(event.event_id || "")}" data-rejected="${rejected ? "0" : "1"}">${rejected ? "恢复事件" : "这不是事件"}</button>
        </div>
      </article>`;
    }).join("")}</div>` : `<div class="empty">没有找到包含“${escapeHtml(query)}”的事件。</div>`;
  }
  search.value = "";
  setupSearch(draw);
  draw();
  content.addEventListener("click", async (event) => {
    const button = event.target.closest(".event-edit, .event-reject");
    if (!button || button.disabled) return;
    const eventId = button.dataset.eventId;
    const current = events.find((item) => item.event_id === eventId);
    if (!current) return;
    button.disabled = true;
    try {
      if (button.classList.contains("event-reject")) {
        await saveEventCorrection(eventId, {rejected: button.dataset.rejected === "1"});
        location.reload();
        return;
      }
      const edit = await openCorrectionEditor("event", {event: current});
      if (!edit) {
        button.disabled = false;
        return;
      }
      await saveEventCorrection(eventId, {changes: edit.changes, rejected: false});
      location.reload();
    } catch (error) {
      button.disabled = false;
      alert(`事件保存失败：${error.message}`);
    }
  });
}

function inlineMarkdown(value) {
  let html = escapeHtml(value);
  html = html.replace(/`([^`]+)`/g, "<code>$1</code>");
  html = html.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  return html;
}

function renderMarkdown(text) {
  const lines = text.replace(/\r\n/g, "\n").split("\n");
  let html = "", inList = false;
  for (const line of lines) {
    if (line.startsWith("# ")) { if (inList) { html += "</ul>"; inList = false; } html += `<h1>${inlineMarkdown(line.slice(2))}</h1>`; }
    else if (line.startsWith("## ")) { if (inList) { html += "</ul>"; inList = false; } html += `<h2>${inlineMarkdown(line.slice(3))}</h2>`; }
    else if (line.startsWith("- ")) { if (!inList) { html += "<ul>"; inList = true; } html += `<li>${inlineMarkdown(line.slice(2))}</li>`; }
    else if (!line.trim()) { if (inList) { html += "</ul>"; inList = false; } }
    else { if (inList) { html += "</ul>"; inList = false; } html += `<p>${inlineMarkdown(line)}</p>`; }
  }
  if (inList) html += "</ul>";
  $("viewer-content").innerHTML = `<article class="markdown">${html}</article>`;
}

function setupEmbeddedViewer() {
  if (!embeddedMode) return;
  document.body.classList.add("embedded-viewer");
  const backLink = document.querySelector(".back-link");
  if (backLink) {
    backLink.href = "#";
    backLink.setAttribute("tabindex", "-1");
    backLink.textContent = "← 返回 Life Logger";
    backLink.addEventListener("click", (event) => {
      event.preventDefault();
      window.parent.postMessage({type: "life-logger-close-viewer"}, location.origin);
    });
    backLink.addEventListener("keydown", (e) => {
      if (e.code === "Space" || e.key === " " || e.keyCode === 32) {
        e.preventDefault();
        e.stopPropagation();
        if (typeof globalAuditionHandler === "function") globalAuditionHandler();
      }
    });
  }
  window.addEventListener("message", (event) => {
    if (event.origin !== location.origin) return;
    if (event.data?.type === "life-logger-shortcut-space") {
      if (typeof globalAuditionHandler === "function") {
        globalAuditionHandler();
      } else {
        const card = document.querySelector(".review-card.keyboard-active")
          || document.querySelector(".review-card:not(.reviewed)")
          || document.querySelector(".review-card");
        card?.querySelector(".review-audio-button")?.click();
      }
    }
  });
}

async function init() {
  setupEmbeddedViewer();
  const meta = TITLES[fileName] || [fileName, "处理结果"];
  $("viewer-title").textContent = meta[0];
  $("viewer-subtitle").textContent = `${meta[1]} · UTC+8`;
  $("raw-link").href = artifactUrl();
  if (!jobId || !artifactPath) throw new Error("缺少结果文件参数");
  const response = await fetch(artifactUrl());
  if (!response.ok) throw new Error(`结果读取失败：${response.status}`);
  if (fileName.endsWith(".md")) { renderMarkdown(await response.text()); return; }
  const data = await response.json();
  if (fileName === "daily_summary.json") {
    let manualEvents = [];
    let generatedEvents = Array.isArray(data.timeline) ? data.timeline : [];
    let corrections = [];
    const eventsPath = siblingArtifactPath("events.json");
    try {
      const [manualResponse, eventsResponse, correctionsResponse] = await Promise.all([
        fetch(manualEventsUrlFor(eventsPath)),
        fetch(artifactUrlFor(eventsPath)),
        fetch(eventCorrectionsUrl(eventsPath)),
      ]);
      if (manualResponse.ok) {
        const manualPayload = await manualResponse.json();
        if (Array.isArray(manualPayload.events)) manualEvents = manualPayload.events;
      }
      if (eventsResponse.ok) {
        const eventsPayload = await eventsResponse.json();
        if (Array.isArray(eventsPayload)) generatedEvents = eventsPayload;
      }
      if (correctionsResponse.ok) {
        const correctionsPayload = await correctionsResponse.json();
        if (Array.isArray(correctionsPayload.corrections)) corrections = correctionsPayload.corrections;
      }
    } catch (_) {
      // The generated summary remains usable when optional correction overlays are missing.
    }
    const corrected = applyEventCorrections(generatedEvents, corrections);
    renderDailySummary(mergeDailySummary({...data, timeline: corrected}, manualEvents));
  }
  else if (fileName === "events.json") {
    const [manualResponse, correctionsResponse] = await Promise.all([
      fetch(manualEventsUrl({includeRejected: true})),
      fetch(eventCorrectionsUrl()),
    ]);
    const manualPayload = manualResponse.ok ? await manualResponse.json() : {events: []};
    const correctionsPayload = correctionsResponse.ok ? await correctionsResponse.json() : {corrections: []};
    const generated = applyEventCorrections(
      Array.isArray(data) ? data : [],
      Array.isArray(correctionsPayload.corrections) ? correctionsPayload.corrections : [],
      {includeRejected: true},
    );
    renderEvents([
      ...generated,
      ...(Array.isArray(manualPayload.events) ? manualPayload.events : []),
    ]);
  }
  else if (fileName === "source-attribution.json") renderRecords(Array.isArray(data) ? data : []);
  else if (fileName === "unknown-review.json") {
    const reviewResponse = await fetch(reviewsUrl());
    const reviewPayload = reviewResponse.ok ? await reviewResponse.json() : {reviews: []};
    let contextRecords = Array.isArray(data) ? data : [];
    try {
      const contextResponse = await fetch(artifactUrlFor(siblingArtifactPath("source-attribution.json")));
      if (contextResponse.ok) {
        const contextPayload = await contextResponse.json();
        if (Array.isArray(contextPayload) && contextPayload.length) contextRecords = contextPayload;
      }
    } catch (_) {
      // Fallback to the review list itself when a legacy job has no full attribution file.
    }
    renderReviewRecords(Array.isArray(data) ? data : [], reviewPayload.reviews || [], contextRecords);
  }
  else if (["self-transcript.json", "non-self-transcript.json", "transcript.json"].includes(fileName)) renderRecords(Array.isArray(data) ? data : []);
  else if (Array.isArray(data)) renderRecords(data);
  else if (data && Array.isArray(data.timeline) && "event_count" in data) renderDailySummary(data);
  else $("viewer-content").innerHTML = `<pre class="generic-json">${escapeHtml(JSON.stringify(data, null, 2))}</pre>`;
}

init().catch((error) => {
  const target = $("viewer-content");
  if (target) target.innerHTML = `<div class="error">${escapeHtml(error.message)}</div>`;
});
