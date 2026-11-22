// 无痕模式、禁用站点数据或配额耗尽时，storage 的访问和读写会直接抛异常；
// 未捕获会让整个脚本在加载阶段中断（页面卡死/空白）。这里统一兜底：
// 读失败返回 null、写失败静默忽略，调用方原有的默认值逻辑继续生效。
function readStorage(store, key) {
  try {
    return window[store].getItem(key);
  } catch (error) {
    return null;
  }
}

function writeStorage(store, key, value) {
  try {
    window[store].setItem(key, value);
  } catch (error) {
    // 存储不可用时放弃持久化，不影响当前会话内的内存状态。
  }
}

// —— localStorage 数据结构版本化 ——
// 持久化数据统一封装为 {"v": 版本号, "value": 业务值} 写入。读取时：
//   1) 旧版未封版本的裸值（字符串类型）按当前版本直接兼容，老数据不失效；
//   2) 版本号不符或结构损坏时返回 null，由调用方走默认值，
//      避免以后调整存储结构时旧数据解析报错。
// 变更存储结构时递增 STORAGE_SCHEMA_VERSION，并在此补充对应兼容分支。
const STORAGE_SCHEMA_VERSION = 1;

function readStorageVersioned(store, key) {
  const raw = readStorage(store, key);
  if (raw === null) return null;
  try {
    const parsed = JSON.parse(raw);
    if (parsed && typeof parsed === "object" && !Array.isArray(parsed) && "v" in parsed) {
      if (parsed.v !== STORAGE_SCHEMA_VERSION || !("value" in parsed)) return null;
      const value = parsed.value;
      return typeof value === "string" ? value : JSON.stringify(value);
    }
  } catch (error) {
    // 非 JSON 的旧版裸值（如任务 ID、日期），直接按当前版本兼容。
  }
  return raw;
}

function writeStorageVersioned(store, key, value) {
  writeStorage(store, key, JSON.stringify({ v: STORAGE_SCHEMA_VERSION, value }));
}

// 列表项模板：同一段行标记只在首次渲染时解析一次，缓存为 <template>，
// 之后每条数据克隆一个节点再填文本，避免对每个列表项重复走 innerHTML 解析。
const rowTemplates = new Map();

function rowTemplate(key, markup) {
  let template = rowTemplates.get(key);
  if (!template) {
    template = document.createElement("template");
    template.innerHTML = markup.trim();
    rowTemplates.set(key, template);
  }
  return template;
}

function cloneRow(key, markup) {
  return rowTemplate(key, markup).content.firstElementChild.cloneNode(true);
}

function setRowText(row, selector, value) {
  const node = row.querySelector(selector);
  if (node) node.textContent = String(value);
  return node;
}

function mountRows(root, rows) {
  if (!root) return root;
  const fragment = document.createDocumentFragment();
  rows.forEach((row) => fragment.appendChild(row));
  root.replaceChildren(fragment);
  return root;
}

const selected = new Map();
let currentJobId = readStorageVersioned("localStorage", "life-logger-current-job");
let pollTimer = null;
let passportPollTimer = null;
let journalDays = [];
let journalFingerprint = "";
let journalWatchCoverageIntervals = [];
let journalWatchCoverageAvailable = true;
let selectedJournalDate = readStorageVersioned("localStorage", "life-logger-journal-date");
let journalSelectionInitialized = false;
let journalInitialPrefetchStarted = false;
const journalInitialPrefetchAttempted = new Set();
// Once the user explicitly chooses a date, late background responses for
// today/latest must never move the selected date or iframe.
let journalUserSelectedDate = "";
let journalDateGeneration = 0;

// Pure guards: keep the race policy testable without constructing the whole UI.
function journalDayAllowedForSelection(day, userSelectedDate = journalUserSelectedDate) {
  const dayDate = String(day?.date || "");
  const selected = String(userSelectedDate || "");
  return !dayDate || !selected || dayDate === selected;
}

function journalNavigationTarget({selectedDate, explicitDate, selectionInitialized, dates, latest, initialDate}) {
  const explicit = String(explicitDate || "");
  let target = explicit || String(selectedDate || "");
  if (!explicit && !selectionInitialized) {
    target = initialDate(Array.from(dates || []), target) || latest;
  } else if (!target) {
    target = latest;
  }
  return target;
}

function journalRefreshStillCurrent(startedGeneration, currentGeneration = journalDateGeneration) {
  const started = typeof startedGeneration === "number" ? startedGeneration : 0;
  const current = typeof currentGeneration === "number" ? currentGeneration : 0;
  return started === current;
}
let journalLastSuccessAt = 0;
// [2026-10-30] 最近一次日数据读取是否拿到「旧快照」；由 fetchJson 设置、成功刷新时清除。
let journalReadStale = null;

function noteJournalReadStale(info, path) {
  if (!info || !info.stale) return;
  journalReadStale = {...info, path: String(path || "")};
  if (typeof renderJournalFreshness === "function") renderJournalFreshness();
}
let journalRefreshError = "";
let taskStates = new Map();
let lastJobs = [];
let capabilities = null;
let accessToken = "";
let modelPreferences = {
  onboarding_completed: false,
  asr_model: "mlx-whisper-turbo",
  speaker_model: "cam++",
};
let activeView = readStorageVersioned("localStorage", "life-logger-active-view") || "today";
let activeSettingsPage = readStorageVersioned("localStorage", "life-logger-settings-page") || "models";
const $ = (id) => document.getElementById(id);
const JOURNAL_LOGIC = window.LifeLoggerJournalLogic || {};
const journalEventLocalDate = JOURNAL_LOGIC.eventLocalDate || ((event, fallback) => fallback);
const journalEventCrossesMidnight = JOURNAL_LOGIC.eventCrossesMidnight || (() => false);
const journalActionCandidateVisible = JOURNAL_LOGIC.actionCandidateVisible || (() => true);
const journalReviewCandidateKey = JOURNAL_LOGIC.reviewCandidateKey || ((kind, item) => `${kind}:${item?.segment_id || item?.event_id || ""}`);
const collapseJournalReviewRows = JOURNAL_LOGIC.collapseReviewRows || ((rows) => ({pending: rows || [], completed: []}));
const groupJournalReviewTargets = JOURNAL_LOGIC.groupReviewTargets || ((rows) => rows || []);
const journalShanghaiDateFromTimestamp = JOURNAL_LOGIC.shanghaiDateFromTimestamp || ((_, fallback) => fallback);
const journalEntryEvidenceDates = JOURNAL_LOGIC.entryEvidenceDates || ((entry) => entry?.date ? [entry.date] : []);
const journalInitialDate = JOURNAL_LOGIC.initialJournalDate || ((dates, persisted) => (dates.includes(persisted) ? persisted : dates[0] || null));
const journalSourceFileLocalDate = JOURNAL_LOGIC.sourceFileLocalDate || ((_, fallback) => fallback);
const journalReviewCandidateVisible = JOURNAL_LOGIC.reviewCandidateVisible || (() => true);
const journalRelativeAbsoluteTimestamp = JOURNAL_LOGIC.relativeAbsoluteTimestamp || (() => null);
const journalSegmentLocalDate = JOURNAL_LOGIC.segmentLocalDate || ((_, fallback) => fallback);

function bootstrapAccessToken() {
  const url = new URL(location.href);
  const queryToken = String(url.searchParams.get("token") || "").trim();
  if (queryToken) {
    accessToken = queryToken;
    writeStorage("sessionStorage", "life-logger-access-token", queryToken);
    url.searchParams.delete("token");
    history.replaceState(null, "", `${url.pathname}${url.search}${url.hash}`);
    return;
  }
  accessToken = String(readStorage("sessionStorage", "life-logger-access-token") || "");
}

bootstrapAccessToken();

const STATUS_LABELS = {
  draft: "未开始",
  queued: "等待处理",
  running: "处理中",
  completed: "已完成",
  degraded: "已完成（有降级）",
  partial: "部分完成",
  failed: "失败",
  interrupted: "已中断",
};

const STAGE_LABELS = {
  queued: "等待处理",
  asr: "语音转写",
  attribution: "声源归属",
  jev: "决策增强",
  completed: "处理完成",
  degraded: "处理完成（有降级）",
  partial: "部分完成",
  failed: "处理失败",
};

const CATEGORY_LABELS = {
  routine: "作息",
  diet: "饮食",
  food: "饮食",
  expense: "消费",
  exercise: "运动",
  work: "工作/沟通",
  communication: "工作/沟通",
  work_communication: "工作/沟通",
  idea: "想法",
  todo: "待办",
  other: "其他",
};

const CATEGORY_ALIASES = {
  diet: "food",
  work: "work_communication",
  communication: "work_communication",
};

function canonicalEventCategory(value) {
  const category = String(value || "");
  return CATEGORY_ALIASES[category] || category;
}

const DATA_HEALTH_ISSUE_LABELS = {
  malformed_json: "JSON 损坏",
  missing_artifact: "结果文件缺失",
  invalid_reference: "引用路径异常",
  orphan_overlay: "悬空修正记录",
  auto_ingest_invalid_state: "自动导入状态异常",
  auto_ingest_missing_folder: "自动导入目录不存在",
  auto_ingest_stale_candidate: "自动导入候选卡住",
  auto_ingest_orphan_job: "自动导入任务引用失效",
};

// —— 数字与日期统一走 Intl 格式化，不再各处手拼字符串 ——
const INT_COUNT = new Intl.NumberFormat("zh-CN");
const INT_MONEY = new Intl.NumberFormat("zh-CN", {style: "currency", currency: "CNY"});
const INT_MONEY_TRIM = new Intl.NumberFormat("zh-CN", {maximumFractionDigits: 2, minimumFractionDigits: 0});
const INT_FIXED1 = new Intl.NumberFormat("zh-CN", {minimumFractionDigits: 1, maximumFractionDigits: 1});
const INT_FIXED2 = new Intl.NumberFormat("zh-CN", {minimumFractionDigits: 2, maximumFractionDigits: 2});
const INT_PERCENT = new Intl.NumberFormat("zh-CN", {style: "percent"});
const INT_SECONDS = new Intl.NumberFormat("zh-CN", {style: "unit", unit: "second", unitDisplay: "narrow", minimumFractionDigits: 1, maximumFractionDigits: 1});
const INT_BYTE = new Intl.NumberFormat("zh-CN", {style: "unit", unit: "byte", unitDisplay: "narrow", maximumFractionDigits: 0});
const INT_KILOBYTE = new Intl.NumberFormat("zh-CN", {style: "unit", unit: "kilobyte", unitDisplay: "narrow", minimumFractionDigits: 1, maximumFractionDigits: 1});
const INT_MEGABYTE = new Intl.NumberFormat("zh-CN", {style: "unit", unit: "megabyte", unitDisplay: "narrow", minimumFractionDigits: 1, maximumFractionDigits: 1});
const INT_CLOCK = new Intl.NumberFormat("zh-CN", {minimumIntegerDigits: 2, useGrouping: false});
const INT_MONTH_DAY = new Intl.DateTimeFormat("en-CA", {timeZone: "Asia/Shanghai", month: "2-digit", day: "2-digit"});
const INT_DAY_KEY = new Intl.DateTimeFormat("en-CA", {timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit"});
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

function formatMoneyTrimmed(value) {
  return INT_MONEY_TRIM.format(Number(value) || 0);
}

function formatFixed1(value) {
  return INT_FIXED1.format(Number(value) || 0);
}

function formatFixed2(value) {
  return INT_FIXED2.format(Number(value) || 0);
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

function formatDayKey(value) {
  const date = value instanceof Date && !Number.isNaN(value.getTime()) ? value : localDayDate(value);
  return date ? INT_DAY_KEY.format(date) : String(value || "");
}

function formatMonthDay(dateKey) {
  const date = localDayDate(dateKey);
  return date ? INT_MONTH_DAY.format(date) : String(dateKey || "");
}

function formatWeekday(dateKey) {
  const date = localDayDate(dateKey);
  return date ? INT_WEEKDAY.format(date) : "";
}

// datetime-local 输入值 "YYYY-MM-DDTHH:mm"，按 en-CA 字段经 Intl 取件后拼装。
function dateTimeInputValue(date) {
  const parts = Object.fromEntries(
    new Intl.DateTimeFormat("en-CA", {
      timeZone: "Asia/Shanghai",
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      hourCycle: "h23",
    }).formatToParts(date).map((part) => [part.type, part.value]),
  );
  return `${parts.year}-${parts.month}-${parts.day}T${parts.hour}:${parts.minute}`;
}

// datetime-local 没有偏移；产品输入明确解释为北京时间，外部 ISO 不走此入口。
function beijingDateTimeInputDate(value) {
  const match = /^(\d{4}-\d{2}-\d{2})T(\d{2}):(\d{2})(?::(\d{2}))?$/.exec(String(value));
  if (!match || !localDayDate(match[1]) || Number(match[2]) > 23
      || Number(match[3]) > 59 || Number(match[4] || 0) > 59) return null;
  return new Date(`${value}+08:00`);
}

let lastDiagnostic = null;
let lastDiagnosticFingerprint = "";
let lastDiagnosticCount = 0;

function diagnosticKind(error) {
  const message = String(error?.message || error || "未知错误");
  const name = String(error?.name || "Error");
  if (error?.diagnostic_kind) return error.diagnostic_kind;
  if (name === "TypeError" && /fetch|network|load failed/i.test(message)) return "network";
  if (/NetworkError|Failed to fetch|Load failed/i.test(message)) return "network";
  return "application";
}

function diagnosticTitle(kind) {
  if (kind === "network") return "本地服务连接失败";
  if (kind === "http_4xx") return "本地接口拒绝请求（4xx）";
  if (kind === "http_5xx") return "本地服务处理出错（5xx）";
  if (kind === "http") return "本地接口返回错误";
  return "Life Logger 发生错误";
}

function normalizeDiagnostic(error, context = {}) {
  const kind = diagnosticKind(error);
  const message = String(error?.message || error || "未知错误");
  const endpoint = context.endpoint || error?.endpoint || "";
  const method = context.method || error?.method || "";
  const status = context.status ?? error?.status ?? null;
  const operation = context.operation || error?.operation || "未标注操作";
  const timestamp = new Date().toISOString();
  const hint = kind === "network"
    ? "浏览器无法连接 Life Logger 本地后端。优先检查 127.0.0.1:8765 是否仍在监听、run_local_web.py 是否退出，以及服务日志。"
    : (kind === "http_4xx"
      ? "本地服务可连接，但请求被拒绝（4xx）。优先检查请求参数是否合法、访问令牌是否已配对；401 需要重新配对。"
      : (kind === "http_5xx"
        ? "本地服务可连接，但处理请求时出错（5xx）。优先查看 run_local_web.py 的服务日志，定位报错堆栈。"
        : (kind === "http"
          ? "本地服务仍可连接，但接口返回了错误。优先检查对应请求参数和后端错误信息。"
          : "这是浏览器端或业务逻辑错误。结合操作、原始错误和控制台信息排查。")));
  return {
    kind,
    title: diagnosticTitle(kind),
    message,
    raw_name: String(error?.name || "Error"),
    operation,
    endpoint,
    method,
    status,
    timestamp,
    hint,
    page: `${location.pathname}${location.search}`,
    origin: location.origin,
    online: navigator.onLine,
    user_agent: navigator.userAgent,
    stack: typeof error?.stack === "string" ? error.stack : "",
    response_body: error?.response_body || "",
  };
}

function diagnosticText(diagnostic = lastDiagnostic) {
  if (!diagnostic) return "";
  return [
    "Life Logger 错误诊断",
    `时间: ${diagnostic.timestamp}`,
    `类型: ${diagnostic.kind}`,
    `操作: ${diagnostic.operation}`,
    `错误: ${diagnostic.raw_name}: ${diagnostic.message}`,
    `请求: ${diagnostic.method || "—"} ${diagnostic.endpoint || "—"}`,
    `HTTP 状态: ${diagnostic.status ?? "—"}`,
    `页面: ${diagnostic.page}`,
    `Origin: ${diagnostic.origin}`,
    `浏览器在线状态: ${diagnostic.online}`,
    `提示: ${diagnostic.hint}`,
    diagnostic.response_body ? `后端响应: ${diagnostic.response_body}` : "",
    diagnostic.stack ? `Stack:\n${diagnostic.stack}` : "",
  ].filter(Boolean).join("\n");
}

function diagnosticAgentText(diagnostic = lastDiagnostic) {
  if (!diagnostic) return "";
  return [
    "请帮我排查并修复 Life Logger 本地网页错误。",
    "先根据下面诊断检查本地服务、对应接口和项目日志；不要猜测错误原因。",
    "如果是 127.0.0.1:8765 无法连接，先检查服务进程和端口，再恢复服务；如果是接口错误，再检查后端实现。",
    "请保留现有录音、训练样本和未提交代码改动，不要删除用户数据。",
    "",
    diagnosticText(diagnostic),
  ].join("\n");
}

function renderDiagnosticPanel() {
  const panel = $("diagnostic-error-panel");
  if (!panel || !lastDiagnostic) return;
  panel.classList.remove("hidden");
  $("diagnostic-error-title").textContent = lastDiagnostic.title;
  $("diagnostic-error-summary").textContent = lastDiagnostic.message;
  $("diagnostic-error-context").textContent = `${lastDiagnostic.operation}${lastDiagnosticCount > 1 ? ` · 重复 ${formatCount(lastDiagnosticCount)} 次` : ""}`;
  $("diagnostic-error-details").textContent = diagnosticText(lastDiagnostic);
}

function reportError(error, context = {}) {
  const diagnostic = normalizeDiagnostic(error, context);
  const fingerprint = [diagnostic.kind, diagnostic.operation, diagnostic.endpoint, diagnostic.message].join("|");
  if (fingerprint === lastDiagnosticFingerprint) lastDiagnosticCount += 1;
  else {
    lastDiagnosticFingerprint = fingerprint;
    lastDiagnosticCount = 1;
  }
  lastDiagnostic = diagnostic;
  renderDiagnosticPanel();
  return diagnostic;
}

async function copyDiagnostic(mode = "raw") {
  const value = mode === "agent" ? diagnosticAgentText() : diagnosticText();
  if (!value) return;
  await copyTextValue(value);
  const button = mode === "agent" ? $("diagnostic-copy-agent") : $("diagnostic-copy-raw");
  if (!button) return;
  const original = button.textContent;
  button.textContent = "已复制";
  setTimeout(() => { button.textContent = original; }, 1200);
}

function closeDiagnosticPanel() {
  $("diagnostic-error-panel")?.classList.add("hidden");
}

// 覆盖全屏的模态/浮层：打开期间，被遮住的背景区域需要 inert，
// 否则 Tab 焦点与点击会穿透到遮罩下方看不见的内容上。
const MODAL_OVERLAY_IDS = ["access-token-overlay", "onboarding-overlay", "inline-viewer-workspace"];
// 同屏出现的非模态浮层（错误诊断、同步提示）保持可交互，不参与惰性化。
const NON_MODAL_FLOATING_IDS = new Set(["diagnostic-error-panel", "sync-toast"]);
const NON_RENDERED_TAGS = new Set(["SCRIPT", "STYLE", "LINK", "TEMPLATE"]);

function syncBackgroundInert() {
  const overlays = MODAL_OVERLAY_IDS.map((id) => $(id)).filter(Boolean);
  const anyOpen = overlays.some((el) => !el.classList.contains("hidden"));
  for (const child of Array.from(document.body.children)) {
    if (NON_RENDERED_TAGS.has(child.tagName)) continue;
    if (NON_MODAL_FLOATING_IDS.has(child.id)) continue;
    const isVisibleOverlay = overlays.includes(child) && !child.classList.contains("hidden");
    child.inert = anyOpen && !isVisibleOverlay;
  }
}

// 焦点锁定：inert 只挡住鼠标与背景 Tab，还需要把键盘焦点收进最上层模态，
// 并在关闭时还给打开它的那个元素，否则键盘用户会"掉"到页面顶部。
const FOCUSABLE_SELECTOR = [
  "a[href]",
  "button:not([disabled])",
  "input:not([disabled])",
  "select:not([disabled])",
  "textarea:not([disabled])",
  "iframe",
  "[tabindex]:not([tabindex='-1'])",
].join(",");

const modalReturnFocus = new Map();

function modalFocusables(overlay) {
  if (!overlay) return [];
  return Array.from(overlay.querySelectorAll(FOCUSABLE_SELECTOR)).filter((el) => {
    if (el.closest("[inert]")) return false;
    if (el.getAttribute("aria-hidden") === "true") return false;
    // display:none 或折叠在隐藏祖先里的元素没有布局盒，不应参与循环。
    return el.getClientRects().length > 0;
  });
}

function activeModalOverlay() {
  const open = MODAL_OVERLAY_IDS
    .map((id) => $(id))
    .filter((el) => el && !el.classList.contains("hidden"));
  if (!open.length) return null;
  // 同屏出现多个模态时，以 DOM 顺序最靠后（绘制层级最高）的为准。
  return open.reduce((top, el) => (
    top.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING ? el : top
  ));
}

function rememberModalTrigger(overlay, trigger = null) {
  if (!overlay || modalReturnFocus.has(overlay.id)) return;
  const active = trigger || document.activeElement;
  modalReturnFocus.set(overlay.id, active instanceof HTMLElement && active !== document.body ? active : null);
}

function restoreModalTrigger(overlay) {
  if (!overlay) return;
  const target = modalReturnFocus.get(overlay.id);
  modalReturnFocus.delete(overlay.id);
  if (!target) return;
  window.requestAnimationFrame(() => {
    if (target.isConnected) {
      target.focus();
      return;
    }
    // 列表在模态关闭时可能被重渲染，原元素已脱离文档；按链接目标找回等价入口。
    if (target.tagName !== "A" || !target.getAttribute("href")) return;
    const href = target.href;
    Array.from(document.querySelectorAll("a[href]")).find((a) => a.href === href)?.focus();
  });
}

function trapModalFocus(event) {
  if (event.key !== "Tab") return;
  const overlay = activeModalOverlay();
  if (!overlay) return;
  const items = modalFocusables(overlay);
  if (!items.length) {
    event.preventDefault();
    return;
  }
  const first = items[0];
  const last = items.at(-1);
  const active = document.activeElement;
  if (!overlay.contains(active)) {
    event.preventDefault();
    (event.shiftKey ? last : first).focus();
    return;
  }
  if (event.shiftKey && active === first) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && active === last) {
    event.preventDefault();
    first.focus();
  }
}

function showAccessTokenOverlay(message = "") {
  const overlay = $("access-token-overlay");
  rememberModalTrigger(overlay);
  overlay?.classList.remove("hidden");
  syncBackgroundInert();
  const status = $("access-token-status");
  const input = $("access-token-input");
  if (status) status.textContent = message || "请输入本机生成的访问令牌。";
  window.requestAnimationFrame(() => input?.focus());
}

function hideAccessTokenOverlay() {
  const overlay = $("access-token-overlay");
  overlay?.classList.add("hidden");
  syncBackgroundInert();
  restoreModalTrigger(overlay);
}

function submitAccessToken() {
  const token = String($("access-token-input")?.value || "").trim();
  if (!token) {
    $("access-token-input")?.setAttribute("aria-invalid", "true");
    showAccessTokenOverlay("令牌不能为空。");
    return;
  }
  $("access-token-input")?.setAttribute("aria-invalid", "false");
  const url = new URL(location.href);
  url.searchParams.set("token", token);
  location.replace(url.toString());
}

const DEFAULT_API_TIMEOUT_MS = 30000;

// 读请求（GET/HEAD）遇网络类错误时的自动重试等待：第 1 次重试前等 300ms、第 2 次前等 900ms，
// 指数退避避免本地服务刚抖动就连环重试。写请求一律不自动重试，防止重复提交。
const API_READ_RETRY_DELAYS_MS = [300, 900];

function sleep(ms) {
  return new Promise((resolve) => { setTimeout(resolve, ms); });
}

// 只重试网络类错误（连接失败、请求超时）：HTTP 4xx/5xx 是后端明确应答，重试无意义；
// 切换视图等主动取消（aborted）也不算失败，直接交给调用方静默处理。
function isRetryableApiError(error) {
  return Boolean(error) && error.diagnostic_kind === "network" && !error.aborted;
}

// 【2026-10-23】external 读模型未就绪时，服务端返回 202 + read_status=pending。
// 这是“还在算”，不是“这一天没有记录”。任何把 202 当成功渲染的路径都会伪造空数据，
// 所以这里统一识别成 pending，由调用方显示“正在更新”并按 retry_after 有界轮询。
const PENDING_READ_STATUSES = new Set(["pending", "computing", "read_busy"]);
const PENDING_MAX_ATTEMPTS = 10;

function pendingReadInfo(payload, httpStatus) {
  const body = payload && typeof payload === "object" ? payload : {};
  const readStatus = String(body.read_status || body.status || "").toLowerCase();
  const pending = Number(httpStatus) === 202 || PENDING_READ_STATUSES.has(readStatus);
  if (!pending) return {pending: false};
  const raw = Number(body.retry_after_ms ?? (Number(body.retry_after) * 1000));
  const retryAfterMs = Math.min(5000, Math.max(250, Number.isFinite(raw) ? raw : 800));
  const taskId = typeof body.task_id === "string" && /^[0-9a-f]{8,64}$/.test(body.task_id) ? body.task_id : null;
  return {
    pending: true,
    readStatus: readStatus || "pending",
    retryAfterMs,
    taskId,
    maxAttempts: PENDING_MAX_ATTEMPTS,
    workerOnline: body.kind !== "ENV",
  };
}

// [2026-10-30] 旧快照（stale_busy）：服务端有旧快照可用但没拿到读槽位/重算超时，
// 返回 200 + read_status="stale_busy" + read_note。与 pending 语义相反：数据旧但可用，
// 必须照常渲染，同时让用户知道「这是旧快照、正在重算」，不能当成空/失败或静默。
const STALE_READ_STATUS = "stale_busy";

function staleReadInfo(payload) {
  const body = payload && typeof payload === "object" ? payload : {};
  if (String(body.read_status || "").toLowerCase() !== STALE_READ_STATUS) return null;
  return {stale: true, note: typeof body.read_note === "string" ? body.read_note : "", at: Date.now()};
}

function isPendingApiError(error) {
  return Boolean(error) && (error.pending === true || error.diagnostic_kind === "pending");
}

function pendingRetryDelay(info, attempt) {
  // 服务器建议值只当下限；本地再按 1.6 倍退避，且永远封顶 5s，
  // 避免慢 worker 把浏览器打成并发轮询风暴。
  const base = info && Number.isFinite(info.retryAfterMs) ? info.retryAfterMs : 800;
  const step = Math.max(0, Math.min(20, Number(attempt) || 0));
  return Math.min(5000, Math.max(250, Math.round(base * Math.pow(1.6, step))));
}

function cancelJournalPendingPolling(keepDate) {
  for (const day of journalDays || []) {
    if (keepDate && day.date === keepDate) continue;
    // 【2026-10-29 预取 202 修复】后台预取（today/yesterday）的重试计时器不属于
    // “当前视图轮询”：切日期时不能连带清掉，否则角标会永远停在 pending。
    // 它只刷新自身角标，不重绘当前视图（见 scheduleJournalPendingRetry 的 background 分支）。
    if (day._pendingBackground) continue;
    if (day._pendingTimer) clearTimeout(day._pendingTimer);
    day._pendingTimer = null;
  }
}

function scheduleJournalPendingRetry(day, info, {background = false} = {}) {
  if (!day || !day.date) return;
  if (day._pendingTimer) clearTimeout(day._pendingTimer);
  day._pendingTimer = null;
  const pending = info && info.pending ? info : pendingReadInfo({read_status: "pending"}, 202);
  const attempts = Number(day._pendingAttempts || 0);
  const maxAttempts = Number(pending.maxAttempts || PENDING_MAX_ATTEMPTS);
  if (attempts >= maxAttempts) {
    // 停止轮询，但保留“正在更新”语义和手动重试入口，绝不退化成空数据。
    day._pendingBackground = false;
    day.canonicalPending = {...pending, attempts, exhausted: true};
    renderJournalNavigation();
    return;
  }
  day._pendingAttempts = attempts + 1;
  day._pendingBackground = background;
  day.canonicalPending = {...pending, attempts: day._pendingAttempts, exhausted: false};
  day._pendingTimer = setTimeout(() => {
    day._pendingTimer = null;
    day._pendingBackground = false;
    // 前台轮询：用户已切到别的日期就放弃，旧日期不许刷新当前视图。
    // 后台预取（background）：允许继续，但 ensureJournalDaySynthesis 只在
    // selectedJournalDate === day.date 时才重绘当天视图，只刷新角标。
    if (selectedJournalDate !== day.date && !background) return;
    ensureJournalDaySynthesis(day, {force: true});
  }, pendingRetryDelay(pending, attempts));
}

const apiGetInflight = new Map();

function apiSubscriber(promise, signal) {
  if (!signal) return promise;
  return new Promise((resolve, reject) => {
    const abort = () => {
      const error = new DOMException("Request canceled", "AbortError");
      error.aborted = true;
      error.diagnostic_kind = "aborted";
      reject(error);
    };
    if (signal.aborted) { abort(); return; }
    signal.addEventListener("abort", abort, {once: true});
    promise.then(resolve, reject).finally(() => signal.removeEventListener("abort", abort));
  });
}

function api(path, options = {}, context = {}) {
  const method = String(options.method || "GET").toUpperCase();
  if (method !== "GET") {
    // Mutations must bypass snapshots; the next jobs read must see the write.
    if (!["HEAD", "OPTIONS"].includes(method) && typeof LifeTimelineData !== "undefined") {
      LifeTimelineData.clear("/api/jobs");
    }
    return apiRead(path, options, context);
  }
  if (options.signal?.aborted) return apiSubscriber(Promise.resolve(), options.signal);
  const headers = JSON.stringify([...new Headers(options.headers || {}).entries()]);
  const key = JSON.stringify([path, accessToken, headers, options.credentials, options.mode]);
  const read = () => {
    if (apiGetInflight.has(key)) return apiGetInflight.get(key);
    // A date/view subscriber can cancel without canceling other readers.
    const request = apiRead(path, {...options, signal: undefined}, context)
      .finally(() => { if (apiGetInflight.get(key) === request) apiGetInflight.delete(key); });
    apiGetInflight.set(key, request);
    return request;
  };
  // Jobs are global, not date-scoped. Polls reuse a one-minute snapshot;
  // explicit refresh and writes bypass it, without duplicating an active read.
  const promise = path === "/api/jobs"
    ? sharedTimelineRead(path, read, {maxAge: 60000, force: Boolean(context.force)})
    : read();
  return apiSubscriber(promise, options.signal);
}

async function apiRead(path, options = {}, context = {}) {
  const method = String(options.method || "GET").toUpperCase();
  const retryDelays = ["GET", "HEAD"].includes(method) ? API_READ_RETRY_DELAYS_MS : [];
  for (let attempt = 0; ; attempt += 1) {
    try {
      return await apiOnce(path, options, context, method);
    } catch (error) {
      if (attempt < retryDelays.length && isRetryableApiError(error)) {
        await sleep(retryDelays[attempt]);
        continue;
      }
      // 最终失败（或不可重试的错误）才上报诊断面板；重试期间保持静默，
      // 避免重试其实成功、错误面板却误报一次失败。
      // pending 不是失败：既不上报诊断面板，也不走 HTTP 错误提示。
      if (!context.silent && error.diagnostic_kind !== "aborted" && !error.pending) {
        reportError(error, {endpoint: path, method, status: error.status, operation: error.operation});
      }
      throw error;
    }
  }
}

async function apiOnce(path, options, context, method) {
  const requestOptions = {...options};
  const timeoutMs = Number.isFinite(options.timeoutMs)
    ? options.timeoutMs
    : (Number.isFinite(context.timeoutMs) ? context.timeoutMs : DEFAULT_API_TIMEOUT_MS);
  delete requestOptions.timeoutMs;
  const headers = new Headers(options.headers || {});
  if (accessToken) headers.set("Authorization", `Bearer ${accessToken}`);
  if (!["GET", "HEAD"].includes(method)) headers.set("X-Life-Logger-CSRF", "1");
  requestOptions.headers = headers;
  const controller = new AbortController();
  let timedOut = false;
  const timeoutId = setTimeout(() => { timedOut = true; controller.abort(); }, timeoutMs);
  const externalSignal = options.signal;
  if (externalSignal) {
    if (externalSignal.aborted) controller.abort();
    else externalSignal.addEventListener("abort", () => controller.abort(), {once: true});
  }
  requestOptions.signal = controller.signal;
  let response;
  let raw;
  try {
    response = await fetch(path, requestOptions);
    raw = await response.text();
  } catch (error) {
    if (timedOut) {
      const timeoutError = new Error(`网络错误：请求超时，${timeoutMs} 毫秒内未收到本地服务响应`);
      timeoutError.name = "TimeoutError";
      error = timeoutError;
    } else if (error && error.name !== "AbortError") {
      // 浏览器对连接失败的原始提示多为英文（Failed to fetch / Load failed），
      // 统一包上「网络错误」分类，与 4xx/5xx 提示区分开；主动取消不包，保持原样。
      error.message = `网络错误：无法连接本地服务（${error.message || "未知原因"}）`;
    }
    if (error && error.name === "AbortError") {
      // 主动取消（切换页面等）不算错误：标记后交给调用方静默忽略，也不上报诊断。
      error.aborted = true;
      error.endpoint = path;
      error.method = method;
      error.operation = context.operation || "本地 API 请求";
      error.diagnostic_kind = "aborted";
      throw error;
    }
    error.endpoint = path;
    error.method = method;
    error.operation = context.operation || "本地 API 请求";
    error.diagnostic_kind = "network";
    throw error;
  } finally {
    clearTimeout(timeoutId);
  }
  let payload = {};
  if (raw) {
    try { payload = JSON.parse(raw); }
    catch (_) { payload = {raw}; }
  }
  // [2026-10-30] 旧快照标记：只认 read_status === "stale_busy"（200 + 旧数据）。
  const staleRead = staleReadInfo(payload);
  if (staleRead) {
    try { payload.__staleRead = staleRead; } catch (_) {}
    if (/\/api\/(daily|dashboard\/daily)\b/.test(path)) noteJournalReadStale(staleRead, path);
  }
  const pending = pendingReadInfo(payload, response.status);
  if (pending.pending) {
    // 202 在 fetch 里是 ok。必须在这里拦住，否则调用方会拿到“没有 events 的 200 形状”
    // 并渲染成空记录。
    const error = new Error("正在更新这一天的数据，稍后自动刷新");
    error.pending = true;
    error.pendingInfo = pending;
    error.endpoint = path;
    error.method = method;
    error.status = response.status;
    error.operation = context.operation || "本地 API 请求";
    error.diagnostic_kind = "pending";
    throw error;
  }
  if (!response.ok) {
    if (response.status === 401) showAccessTokenOverlay("访问令牌无效或尚未配对。");
    // 4xx 是请求被拒（参数/权限问题），5xx 是服务端处理出错，排查方向不同，分类提示。
    const isServerError = response.status >= 500;
    const label = isServerError ? "服务端错误" : "客户端错误";
    const error = new Error(payload.error
      ? `${label} ${response.status}：${payload.error}`
      : `${label} ${response.status}`);
    error.endpoint = path;
    error.method = method;
    error.status = response.status;
    error.operation = context.operation || "本地 API 请求";
    error.response_body = raw.slice(0, 4000);
    error.diagnostic_kind = isServerError ? "http_5xx" : "http_4xx";
    throw error;
  }
  return payload;
}


const VIEW_META = {
  today: ["TODAY", "今天"],
  kanban: ["TASK BOARD", "任务看板"],
  dashboard: ["MULTI-MODAL DIGEST", "全天生活仪表盘"],
  "memory-qa": ["SECOND BRAIN Q&A", "记忆大脑问答"],
  search: ["MEMORY SEARCH", "搜索"],
  stats: ["LOOK BACK", "回看"],
  patterns: ["PATTERN OBSERVATIONS", "规律观察"],
  import: ["INGEST", "导入与处理"],
  review: ["NEEDS REVIEW", "需要我判断"],
  history: ["RUN HISTORY", "任务历史"],
  settings: ["PREFERENCES", "设置"],
  more: ["MORE", "更多"],
};

function updatePassportPolling(view) {
  if (passportPollTimer) {
    clearInterval(passportPollTimer);
    passportPollTimer = null;
  }
  if (view !== "import") return;
  passportPollTimer = setInterval(() => {
    if (document.hidden || activeView !== "import") return;
    refreshPassportSessions().catch(() => {});
  }, 3000);
}

function preferredScrollBehavior() {
  return window.matchMedia?.("(prefers-reduced-motion: reduce)")?.matches ? "auto" : "smooth";
}

// 视图级请求取消：每个视图共用一个 AbortController，切换视图时中止上一个视图仍在途的
// 慢请求，避免旧响应在切页后才返回、用旧数据覆盖新视图内容。
const viewAbortControllers = new Map();

function viewAbortSignal(view) {
  let controller = viewAbortControllers.get(view);
  if (!controller) {
    controller = new AbortController();
    viewAbortControllers.set(view, controller);
  }
  return controller.signal;
}

function abortViewRequests(view) {
  const controller = viewAbortControllers.get(view);
  if (!controller) return;
  viewAbortControllers.delete(view);
  controller.abort();
}

function switchView(name) {
  const view = VIEW_META[name] ? name : "today";
  if (view !== activeView) abortViewRequests(activeView);
  activeView = view;
  if (document.body) document.body.dataset.activeView = view;
  updatePassportPolling(view);
  writeStorageVersioned("localStorage", "life-logger-active-view", view);
  document.querySelectorAll(".nav-item").forEach((button) => {
    const active = button.dataset.view === view;
    button.classList.toggle("active", active);
    if (active) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  });
  const hubViews = ["import", "history", "settings", "stats", "patterns", "dashboard", "search", "memory-qa", "more"];
  document.querySelectorAll(".mobile-nav-btn").forEach((button) => {
    const mv = button.dataset.mobileView;
    const active = (mv === view) || (mv === "more" && hubViews.includes(view));
    button.classList.toggle("active", active);
    if (active) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  });
  document.querySelectorAll(".view-panel").forEach((panel) => {
    panel.classList.toggle("active", panel.dataset.panel === view);
  });
  $("page-kicker").textContent = VIEW_META[view][0];
  $("page-title").textContent = VIEW_META[view][1];
  window.scrollTo({top: 0, behavior: preferredScrollBehavior()});
  if (view === "search") setTimeout(() => $("global-search-input")?.focus(), 80);
  if (view === "today") {
    refreshMobileBrief().catch(() => {});
  }
  if (view === "kanban") {
    renderKanban();
  }
  if (view === "review") {
    refreshJournal(lastJobs, false, {loadDetails: true}).catch(() => {});
  }
  if (view === "dashboard") {
    loadDashboard().catch(() => {});
  }
  if (view === "memory-qa") {
    setTimeout(() => $("memory-qa-input")?.focus(), 80);
  }
  if (view === "stats") {
    ensureStatisticsRange();
    refreshStatistics().catch(() => {});
  }
  if (view === "patterns") {
    ensurePatternRange();
    refreshPatterns().catch(() => {});
  }
  if (view === "settings") switchSettingsPage(activeSettingsPage);
  if (view === "import") {
    refreshAutoIngest().catch(() => {});
    refreshPassportSessions().catch(() => {});
  }
}

function switchSettingsPage(name) {
  const page = ["models", "agent", "mobile", "doctor"].includes(name) ? name : "models";
  activeSettingsPage = page;
  writeStorageVersioned("localStorage", "life-logger-settings-page", page);
  document.querySelectorAll("[data-settings-page-target]").forEach((button) => {
    const active = button.dataset.settingsPageTarget === page;
    button.classList.toggle("active", active);
    button.setAttribute("aria-selected", active ? "true" : "false");
    button.setAttribute("tabindex", active ? "0" : "-1");
  });
  document.querySelectorAll("[data-settings-page]").forEach((panel) => {
    const active = panel.dataset.settingsPage === page;
    panel.classList.toggle("active", active);
    panel.hidden = !active;
  });
  if (page === "agent") {
    renderAgentIntegration();
    refreshDataHealth().catch(() => {});
  }
  if (page === "mobile") {
    renderMobileAccess();
    renderMobileCapture();
  }
  if (page === "doctor") refreshDoctor().catch(() => {});
}

function handleSettingsTabKeydown(event) {
  const tabs = [...document.querySelectorAll("[data-settings-page-target]")];
  const current = tabs.indexOf(event.currentTarget);
  if (current < 0 || !tabs.length) return;
  let next = current;
  if (["ArrowRight", "ArrowDown"].includes(event.key)) next = (current + 1) % tabs.length;
  else if (["ArrowLeft", "ArrowUp"].includes(event.key)) next = (current - 1 + tabs.length) % tabs.length;
  else if (event.key === "Home") next = 0;
  else if (event.key === "End") next = tabs.length - 1;
  else return;
  event.preventDefault();
  const target = tabs[next];
  switchSettingsPage(target.dataset.settingsPageTarget);
  target.focus();
}

const REQUIRED_BACKEND_FEATURES = ["event_corrections_v1", "corrected_transcript_v1"];

function backendMissingFeatures() {
  if (!capabilities) return [...REQUIRED_BACKEND_FEATURES];
  const features = new Set(Array.isArray(capabilities?.api_features) ? capabilities.api_features : []);
  return REQUIRED_BACKEND_FEATURES.filter((feature) => !features.has(feature));
}

function setSidebarServiceStatus(state, message = "") {
  const root = $("sidebar-service-status");
  const dot = $("sidebar-status-dot");
  const text = $("sidebar-bind-status");
  const normalized = ["checking", "ready", "warning", "error"].includes(state) ? state : "checking";
  const labels = {checking: "连接中", ready: "正常", warning: "需要处理", error: "状态未知"};
  if (dot) dot.className = `status-dot ${normalized}`;
  if (text && message) text.textContent = message;
  const detail = String(text?.textContent || message || "").trim();
  if (root) root.setAttribute("aria-label", `本地服务：${labels[normalized]}${detail ? `，${detail}` : ""}`);
}

function renderBackendCompatibility() {
  const warning = $("service-version-warning");
  if (!capabilities) {
    warning?.classList.add("hidden");
    return;
  }
  const features = new Set(Array.isArray(capabilities?.api_features) ? capabilities.api_features : []);
  const missing = backendMissingFeatures();
  if (warning) {
    warning.classList.toggle("hidden", missing.length === 0);
    warning.innerHTML = missing.length
      ? `<strong>本地服务需要重启</strong><span>网页代码已经更新，但当前后台仍缺少新接口。页面会自动降级，不影响查看已有记录；重启 Life Logger 本地服务后即可恢复事件修正等功能。</span>`
      : "";
  }
  const backup = $("data-backup-download");
  const backupStatus = $("data-backup-status");
  const backupReady = features.has("state_backup_v1");
  if (backup) {
    if (backupReady) {
      backup.href = "/api/backup";
      backup.removeAttribute("tabindex");
    } else {
      backup.removeAttribute("href");
      backup.setAttribute("tabindex", "-1");
    }
    backup.setAttribute("aria-disabled", backupReady ? "false" : "true");
    backup.classList.toggle("disabled", !backupReady);
    backup.textContent = backupReady ? "导出状态备份" : "重启服务后可导出";
  }
  if (backupStatus) {
    backupStatus.textContent = backupReady
      ? "备份包含人工修正、复核、人工事件、任务状态和 Personal Model；不会包含原始录音或 API Key。"
      : "当前后台版本还没有备份接口。重启本地服务后这里会自动启用。";
  }
}

async function refreshDataHealth() {
  const badge = $("data-health-badge");
  const status = $("data-health-status");
  const button = $("refresh-data-health");
  const card = $("data-health-card");
  const features = new Set(Array.isArray(capabilities?.api_features) ? capabilities.api_features : []);
  if (!features.has("data_health_v1")) {
    if (badge) { badge.textContent = "需重启"; badge.className = "setting-badge warning"; }
    if (status) status.textContent = "当前后台版本还没有数据健康接口。重启本地服务后自动启用。";
    if (card) card.setAttribute("aria-busy", "false");
    return;
  }
  if (card) card.setAttribute("aria-busy", "true");
  if (button) button.disabled = true;
  if (badge) {
    badge.textContent = "检测中";
    badge.className = "setting-badge";
  }
  if (status) status.textContent = "正在检查本地状态文件与引用关系……";
  try {
    const report = await api("/api/data-health", {}, {silent: true, operation: "数据健康检测"});
    const issueCount = Number(report.issue_count || 0);
    if (badge) {
      badge.textContent = issueCount ? "需处理" : "正常";
      badge.className = `setting-badge ${issueCount ? "warning" : "ready"}`;
    }
    if ($("data-health-files")) $("data-health-files").textContent = formatCount(report.checked_files || 0);
    if ($("data-health-issues")) $("data-health-issues").textContent = formatCount(issueCount);
    const autoHealth = report?.auto_ingest || {};
    if ($("data-health-auto-ingest")) {
      const missingFolders = Number(autoHealth.missing_folder_count || 0);
      const pendingFiles = Number(autoHealth.pending_count || 0);
      $("data-health-auto-ingest").textContent = !autoHealth.configured
        ? "未配置"
        : missingFolders
          ? `${formatCount(missingFolders)} 个目录缺失`
          : pendingFiles
            ? `${formatCount(pendingFiles)} 个待处理`
            : autoHealth.enabled
              ? "运行中"
              : "已暂停";
    }
    if (status) {
      const first = Array.isArray(report.issues) && report.issues.length ? report.issues[0] : null;
      if (first) {
        const issueLabel = DATA_HEALTH_ISSUE_LABELS[first.kind] || first.kind;
        const issueDetail = first.artifact_path || first.path || first.job_id || "";
        status.textContent = `发现 ${formatCount(issueCount)} 个问题；首项：${issueLabel}${issueDetail ? ` · ${issueDetail}` : ""}`;
      } else if (validationCount === 0) {
        status.textContent = "持久化数据结构正常。Validation 仍为 0，因此不会显示准确率或自动校准结果。";
      } else {
        status.textContent = "持久化数据结构正常，未发现损坏或悬空引用。";
      }
    }
  } catch (error) {
    if (badge) { badge.textContent = "检测失败"; badge.className = "setting-badge warning"; }
    if (status) status.textContent = `检测失败：${error.message}`;
  } finally {
    if (card) card.setAttribute("aria-busy", "false");
    if (button) button.disabled = false;
  }
}

function renderDoctor(report) {
  $("doctor-surface")?.setAttribute("aria-busy", "false");
  const badge = $("doctor-badge");
  const summary = $("doctor-summary");
  const checks = Array.isArray(report?.checks) ? report.checks : [];
  const ok = Boolean(report?.ok);
  if (badge) {
    badge.textContent = ok ? "全部通过" : "需处理";
    badge.className = `setting-badge ${ok ? "ready" : "warning"}`;
  }
  if (summary) {
    const failed = checks.filter((item) => !item.ok).length;
    summary.textContent = ok
      ? `已完成 ${checks.length} 项检查，全部正常。`
      : `已完成 ${checks.length} 项检查，${failed} 项需要处理。`;
  }
  const checksRoot = $("doctor-checks");
  if (checksRoot) {
    if (!checks.length) {
      checksRoot.innerHTML = '<div class="journal-empty" role="status"><strong>没有检查项</strong><span>当前自检没有返回可展示的检查结果。</span></div>';
    } else {
      mountRows(checksRoot, checks.map((item) => {
        const row = cloneRow("doctor-check", `
          <article class="doctor-check" role="listitem" aria-label="${escapeHtml(rowLabel)}">
            <span class="doctor-mark"></span>
            <div><strong></strong><p></p><small></small></div>
          </article>
        `);
        row.className = `doctor-check ${item.ok ? "ready" : "warning"}`;
        setRowText(row, ".doctor-mark", item.ok ? "✅" : "❌");
        setRowText(row, "strong", item.label || item.key || "检查项");
        setRowText(row, "p", item.detail || "");
        const fix = row.querySelector("small");
        if (item.fix) fix.textContent = `修复：${item.fix}`;
        else fix.remove();
        return row;
      }));
    }
  }
  const recent = Array.isArray(report?.job_stats?.recent) ? report.job_stats.recent : [];
  const jobsRoot = $("doctor-jobs");
  if (jobsRoot) {
    if (!recent.length) {
      jobsRoot.innerHTML = '<div class="journal-empty" role="status"><strong>当前没有历史任务</strong><span>处理第一份录音后，最近任务会出现在这里。</span></div>';
    } else {
      mountRows(jobsRoot, recent.slice(0, 5).map((job) => {
        const row = cloneRow("doctor-job", '<div class="doctor-job-row" role="listitem"><code></code><span></span><small></small></div>');
        setRowText(row, "code", job.job_id || "—");
        setRowText(row, "span", statusLabel(job.status || "unknown"));
        setRowText(row, "small", job.current_stage || "—");
        return row;
      }));
    }
  }
}

async function refreshDoctor() {
  const summary = $("doctor-summary");
  const button = $("run-doctor");
  const badge = $("doctor-badge");
  $("doctor-surface")?.setAttribute("aria-busy", "true");
  if (summary) summary.textContent = "正在检查本地运行环境……";
  if (badge) {
    badge.textContent = "检查中";
    badge.className = "setting-badge";
  }
  if (button) button.disabled = true;
  try {
    const report = await api("/api/doctor", {}, {silent: true, operation: "系统自检"});
    renderDoctor(report);
    return report;
  } catch (error) {
    if ($("doctor-badge")) {
      $("doctor-badge").textContent = "检测失败";
      $("doctor-badge").className = "setting-badge warning";
    }
    if (summary) summary.textContent = `自检失败：${error.message}。如果刚更新代码，请先重启本地服务。`;
    throw error;
  } finally {
    $("doctor-surface")?.setAttribute("aria-busy", "false");
    if (button) button.disabled = false;
  }
}

function agentQueryExamplesText(base = location.origin) {
  return [
    "所有请求头: Authorization: Bearer <TOKEN>",
    `稳定消费 API: GET ${base}/api/daily-brief?date=2026-09-21`,
    `连通/能力检查: GET ${base}/api/settings`,
    `排障: GET ${base}/api/doctor`,
  ].join("\n");
}

function agentPromptText() {
  const base = location.origin;
  return `请接入我的 Life Logger 本地服务。\n\n本地地址：${base}\n鉴权：所有 /api/* 请求必须带请求头 Authorization: Bearer <TOKEN>。TOKEN 从 Life Logger「设置 → 移动访问」复制。\n\n使用规则：\n1. 先请求 GET ${base}/api/settings 做连通性检查，并确认 api_features 包含 agent_brief_v1。\n2. 日常消费只使用 GET ${base}/api/daily-brief?date=YYYY-MM-DD。响应是 agent-brief-v1，包含 searchable_summary、todos、warnings 和 confidence >= 0.90 的 high_confidence_points。\n3. 这个消费 payload 不含原始音频、音频路径、完整 transcript、evidence/quote、recording_id、segment_id 或内部 artifact 路径。不要尝试从本机目录补读这些隐私数据。\n4. 默认只读。不要调用修改数据的 POST 接口；数据不足时明确说明，不要编造。\n5. /api/daily、/api/search、/api/jobs、/api/patterns 等是开发/排障接口，不属于小苏/微信的稳定消费契约，普通消费流程不要依赖它们。\n\n接入示例：\n${agentQueryExamplesText(base)}\n\n排查：如果接口无法连接，请求 GET ${base}/api/doctor；如果缺少 agent_brief_v1，重启 Life Logger 本地服务。\n\n接入后先告诉我：服务是否连通，以及指定日期的 agent-brief-v1 是否可读。不要修改任何数据。`;
}

function renderAgentIntegration() {
  const base = location.origin;
  if ($("agent-base-url")) $("agent-base-url").textContent = base;
  if ($("agent-connect-prompt")) $("agent-connect-prompt").value = agentPromptText();
  if ($("agent-query-examples")) $("agent-query-examples").textContent = agentQueryExamplesText(base);
  renderBackendCompatibility();
}

function renderMobileAccess() {
  const mobile = capabilities?.mobile_access || {};
  const enabled = Boolean(mobile.lan_access_enabled);
  const checkbox = $("lan-access-enabled");
  if (checkbox) checkbox.checked = enabled;
  const badge = $("mobile-access-badge");
  const trustedBind = String(mobile.trusted_network_bind || "");
  if (badge) badge.textContent = enabled ? (trustedBind ? "受信私网已允许" : "局域网已允许") : "仅本机";
  if ($("mobile-bind-host")) $("mobile-bind-host").textContent = mobile.bind_host || "127.0.0.1";
  if ($("trusted-network-bind")) $("trusted-network-bind").value = trustedBind;
  const watchDirectStatus = $("watch-direct-status");
  if (watchDirectStatus) {
    watchDirectStatus.textContent = mobile.watch_direct_access_enabled
      ? `Apple Watch 自动直连已启用：${mobile.bind_host}:${mobile.port}（仅开放 Watch 录音接口）`
      : "Apple Watch 自动直连需要开启局域网访问，并绑定一个具体的受信私网 IP。";
  }
  if ($("sidebar-bind-status")) {
    $("sidebar-bind-status").textContent = enabled
      ? `本机处理 · ${trustedBind ? `私网 ${trustedBind}` : "LAN"} ${mobile.port || 8765}`
      : "本机处理 · 127.0.0.1";
  }
  const masked = String(mobile.token_masked || mobile.token || "");
  if ($("mobile-access-token")) {
    $("mobile-access-token").textContent = masked || "当前服务尚未生成令牌";
    $("mobile-access-token").title = mobile.token_configured === false ? "尚未生成" : "已掩码；复制时读取完整值";
  }
  const host = trustedBind || (!["127.0.0.1", "localhost"].includes(location.hostname)
    ? location.hostname
    : "<电脑局域网IP/受信私网IP>");
  const pairUrl = mobile.token_configured === false ? "尚未生成"
    : `http://${host}:${mobile.port || 8765}/?token=<点击“复制地址”时生成>`;
  if ($("mobile-pair-url")) $("mobile-pair-url").textContent = pairUrl;
  const status = $("mobile-access-status");
  if (status && mobile.restart_required) status.textContent = "绑定范围已修改，需要重启本地服务后生效。";
}

function renderMobileCapture() {
  const capture = capabilities?.mobile_capture || {};
  const enabled = capture.enabled !== false;
  const autoStart = Boolean(capture.auto_start);
  if ($("mobile-capture-enabled")) $("mobile-capture-enabled").checked = enabled;
  if ($("mobile-capture-auto-start")) $("mobile-capture-auto-start").checked = autoStart;
  $("mobile-capture-card")?.classList.toggle("hidden", !enabled);
}

async function saveMobileCapture() {
  const status = $("mobile-capture-settings-status");
  const button = $("save-mobile-capture");
  const settings = $("mobile-capture-settings");
  const enabled = Boolean($("mobile-capture-enabled")?.checked);
  const autoStart = Boolean($("mobile-capture-auto-start")?.checked);
  if (settings) settings.setAttribute("aria-busy", "true");
  if (button) button.disabled = true;
  if (status) status.textContent = "正在保存……";
  try {
    const payload = await api("/api/mobile-capture", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({enabled, auto_start: autoStart}),
    }, {operation: "保存手机捕获设置"});
    capabilities = {...(capabilities || {}), mobile_capture: payload.mobile_capture || {}};
    renderMobileCapture();
    if (status) status.textContent = "已保存。原始音频仍只走当前 Life Logger 服务。";
  } catch (error) {
    if (status) status.textContent = `保存失败：${error.message}`;
  } finally {
    if (settings) settings.setAttribute("aria-busy", "false");
    if (button) button.disabled = false;
  }
}

async function handleMobileCapture(input) {
  const status = $("mobile-capture-status");
  const card = $("mobile-capture-card");
  const file = input?.files?.[0];
  if (!file) return;
  const capture = capabilities?.mobile_capture || {};
  if (capture.enabled === false) {
    if (status) status.textContent = "手机捕获入口已在设置中关闭。";
    input.value = "";
    return;
  }
  addFiles([file]);
  if (status) status.textContent = `已加入：${file.name} · ${formatBytes(file.size)}。`;
  input.value = "";
  if (capture.auto_start) {
    if (card) card.setAttribute("aria-busy", "true");
    input.disabled = true;
    if (status) status.textContent += " 正在交给现有本地处理链……";
    try {
      await createAndStart();
      if (status) status.textContent = "录音已交给导入流程；任务启动结果和进度会在下方显示。";
    } finally {
      if (card) card.setAttribute("aria-busy", "false");
      input.disabled = false;
    }
  } else if (status) {
    status.textContent += " 请确认后点击“创建任务并开始处理”。";
  }
}

async function runMobileDoctor() {
  const status = $("mobile-brief-action-status");
  if (status) status.textContent = "正在运行本地自检……";
  try {
    const report = await api("/api/doctor", {}, {silent: true, operation: "移动端系统自检"});
    const failed = (report.checks || []).filter((item) => !item.ok);
    if (!failed.length) {
      if (status) status.textContent = "系统自检通过。";
      return;
    }
    if (status) status.textContent = `系统自检发现 ${failed.length} 项问题，已打开详细结果。`;
    switchView("settings");
    switchSettingsPage("doctor");
  } catch (error) {
    if (status) status.textContent = `自检失败：${error.message}`;
  }
}

async function saveMobileAccess() {
  const enabled = Boolean($("lan-access-enabled")?.checked);
  const trustedNetworkBind = String($("trusted-network-bind")?.value || "").trim();
  const status = $("mobile-access-status");
  const button = $("save-mobile-access");
  const surface = $("mobile-access-surface");
  if (surface) surface.setAttribute("aria-busy", "true");
  if (button) button.disabled = true;
  if (status) status.textContent = "正在保存……";
  try {
    const payload = await api("/api/mobile-access", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({enabled, trusted_network_bind: trustedNetworkBind}),
    }, {operation: "保存移动访问设置"});
    capabilities = {...(capabilities || {}), mobile_access: payload.mobile_access || {}};
    renderMobileAccess();
    if (status) status.textContent = payload.mobile_access?.restart_required
      ? "已保存。请重启 Life Logger 本地服务以切换监听范围。"
      : "已保存，当前监听范围已经一致。";
  } catch (error) {
    if (status) status.textContent = `保存失败：${error.message}`;
  } finally {
    if (surface) surface.setAttribute("aria-busy", "false");
    if (button) button.disabled = false;
  }
}

async function revealMobileAccessToken() {
  const revealed = await api("/api/settings/mobile-token");
  return String(revealed?.token || "");
}

async function copyMobileAccessValue(kind) {
  const mobile = capabilities?.mobile_access || {};
  // 默认响应只给掩码；只有用户点“复制令牌/复制地址”时才取完整值。
  const token = await revealMobileAccessToken();
  const trustedBind = String(mobile.trusted_network_bind || "");
  const host = trustedBind || (!["127.0.0.1", "localhost"].includes(location.hostname)
    ? location.hostname
    : "<电脑局域网IP/受信私网IP>");
  const value = kind === "token"
    ? token
    : `http://${host}:${mobile.port || 8765}/?token=${encodeURIComponent(token)}`;
  if (value) await copyTextValue(value);
}

function setMobileBriefLoading(isBusy, message = "") {
  const root = $("mobile-brief-content");
  const status = $("mobile-brief-load-status");
  const refresh = $("mobile-brief-refresh");
  if (root) root.setAttribute("aria-busy", isBusy ? "true" : "false");
  if (status && message) status.textContent = message;
  if (refresh) refresh.disabled = Boolean(isBusy);
}

function renderMobileBrief(brief = {}) {
  const root = $("mobile-brief-content");
  if (!root) return;
  const events = Array.isArray(brief.high_confidence_points)
    ? brief.high_confidence_points
    : (Array.isArray(brief.events) ? brief.events : []);
  const todos = Array.isArray(brief.todos) ? brief.todos : [];
  const categories = Object.entries(brief.category_summary || {})
    .filter(([, value]) => Number(value?.count || 0) > 0)
    .map(([key, value]) => `<span>${escapeHtml(CATEGORY_LABELS[key] || key)} ${formatCount(value.count || 0)}</span>`)
    .join("");
  root.innerHTML = `<div class="mobile-brief-meta"><strong>${escapeHtml(brief.date || "今日")}</strong><span>${formatCount(brief.event_count || 0)} 条事件</span></div>
    <div class="mobile-brief-categories">${categories || "<span>暂无分类事件</span>"}</div>
    <div class="mobile-brief-list">${events.slice(0, 5).map((event) => `<article><span>${escapeHtml(eventClock(event) || "—")}</span><strong>${escapeHtml(event.title || "未命名事件")}</strong></article>`).join("") || "<p>今天还没有形成正式事件。</p>"}</div>
    <div class="mobile-brief-todos"><span>未完成待办</span><strong>${formatCount(todos.length)}</strong></div>`;
}

async function refreshMobileBrief() {
  const status = $("mobile-brief-action-status");
  const root = $("mobile-brief-content");
  if (status) status.textContent = "";
  if (!root) return;
  setMobileBriefLoading(true, "正在读取今日简报");
  try {
    const brief = await api("/api/daily-brief", {signal: viewAbortSignal("today")}, {silent: true, operation: "读取今日简报"});
    renderMobileBrief(brief);
    setMobileBriefLoading(false, "今日简报已更新");
  } catch (error) {
    if (error.aborted) {
      // 切视图取消在途请求时也必须归位：否则简报区会保持转圈、刷新按钮一直禁用。
      setMobileBriefLoading(false);
      return;
    }
    if (error.status === 401) {
      setMobileBriefLoading(false, "需要访问令牌才能读取今日简报");
      return;
    }
    root.innerHTML = `<div class="journal-empty" role="status"><strong>今天还没有简报</strong><span>${escapeHtml(error.message)}</span></div>`;
    setMobileBriefLoading(false, "今日简报暂时不可用");
  }
}

async function copyTextValue(value) {
  if (navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(value);
      return;
    } catch (_) {
      // Some embedded/local browsers expose Clipboard API but deny it at runtime.
      // Fall back to a temporary textarea so the copy button still works.
    }
  }
  const helper = document.createElement("textarea");
  helper.value = value;
  helper.style.position = "fixed";
  helper.style.opacity = "0";
  document.body.appendChild(helper);
  helper.select();
  const copied = document.execCommand("copy");
  helper.remove();
  if (!copied) throw new Error("浏览器未允许复制");
}

async function copyAgentValue(kind) {
  const status = $("agent-connect-status");
  const value = kind === "url" ? location.origin : agentPromptText();
  try {
    await copyTextValue(value);
    if (status) status.textContent = kind === "url" ? "本地地址已复制。" : "接入说明已复制，可以直接粘贴给你的 Agent。";
  } catch (error) {
    if (status) status.textContent = `复制失败：${error.message}`;
    reportError(error, {operation: kind === "url" ? "复制 Agent 地址" : "复制 Agent 接入说明"});
  }
}

async function testAgentConnection() {
  const badge = $("agent-connect-badge");
  const status = $("agent-connect-status");
  const button = $("test-agent-connection");
  const surface = $("agent-connect-surface");
  if (surface) surface.setAttribute("aria-busy", "true");
  if (badge) {
    badge.textContent = "检测中";
    badge.className = "setting-badge";
  }
  if (button) { button.disabled = true; button.textContent = "检测中……"; }
  if (status) status.textContent = "正在检查本地 API……";
  try {
    const [settings, jobsPayload, doctor] = await Promise.all([
      api("/api/settings"),
      api("/api/jobs"),
      api("/api/doctor"),
    ]);
    const jobs = Array.isArray(jobsPayload.jobs) ? jobsPayload.jobs : [];
    if (!settings.local_first || !doctor.ok) throw new Error("服务自检未通过");
    if (badge) { badge.textContent = "可接入"; badge.className = "setting-badge ready"; }
    if (status) status.textContent = `连接正常 · 当前可读取 ${formatCount(jobs.length)} 个任务。复制说明给 Agent 后即可使用。`;
  } catch (error) {
    if (badge) { badge.textContent = "连接失败"; badge.className = "setting-badge warning"; }
    if (status) status.textContent = `连接失败：${error.message}`;
  } finally {
    if (surface) surface.setAttribute("aria-busy", "false");
    if (button) { button.disabled = false; button.textContent = "连接自检"; }
  }
}

function decisionProviderLabel(provider) {
  return `${String(provider || "Jev").toUpperCase()}（历史·已停用）`;
}

function currentProcessingOptions() {
  return {
    asr_model: modelPreferences.asr_model || "mlx-whisper-turbo",
    speaker_model: modelPreferences.speaker_model || "cam++",
  };
}

function modelCard(model, selectedId, kind) {
  const selectedModel = model.id === selectedId;
  const disabled = !model.selectable;
  const state = model.installed === false ? "未安装" : (disabled ? "已安装 · 暂未开放" : "可用");
  const card = cloneRow("model-option", `
    <button class="model-option" type="button">
      <div class="model-option-head"><strong></strong><span></span></div>
      <p></p>
      <div class="model-option-foot"><span></span><em></em></div>
    </button>
  `);
  card.className = `model-option ${selectedModel ? "selected" : ""} ${disabled ? "disabled" : ""}`;
  card.dataset.modelKind = kind;
  card.dataset.modelId = model.id;
  card.disabled = disabled;
  setRowText(card, ".model-option-head strong", model.name);
  setRowText(card, ".model-option-head span", model.badge || state);
  setRowText(card, "p", model.description || "");
  setRowText(card, ".model-option-foot span", state);
  setRowText(card, ".model-option-foot em", selectedModel ? "当前使用" : (disabled ? "实验模型" : "选择"));
  return card;
}

function renderModelCenter() {
  const catalog = capabilities?.models || {};
  const asrRoot = $("asr-model-options");
  const speakerRoot = $("speaker-model-options");
  if (asrRoot) mountRows(asrRoot, (catalog.asr || []).map((model) => modelCard(model, modelPreferences.asr_model, "asr")));
  if (speakerRoot) mountRows(speakerRoot, (catalog.speaker || []).map((model) => modelCard(model, modelPreferences.speaker_model, "speaker")));
  const asrName = (catalog.asr || []).find((model) => model.id === modelPreferences.asr_model)?.name || modelPreferences.asr_model;
  const speakerName = (catalog.speaker || []).find((model) => model.id === modelPreferences.speaker_model)?.name || modelPreferences.speaker_model;
  const pipelineModels = $("import-pipeline-models");
  if (pipelineModels) pipelineModels.textContent = `${asrName} → ${speakerName} → 本地事件提取`;
}

function handleModelOptionClick(event) {
  const button = event.target.closest?.(".model-option");
  if (!button || button.disabled) return;
  if (button.dataset.modelKind === "asr") modelPreferences.asr_model = button.dataset.modelId;
  if (button.dataset.modelKind === "speaker") modelPreferences.speaker_model = button.dataset.modelId;
  renderModelCenter();
  const status = $("model-save-status");
  if (status) status.textContent = "有未保存的模型设置。";
}

async function saveModelPreferences({completeOnboarding = true} = {}) {
  const status = $("model-save-status");
  const surface = $("model-settings-surface");
  const saveButton = $("save-model-preferences");
  const recommendedButton = $("apply-recommended-models");
  const options = currentProcessingOptions();
  const payload = {
    onboarding_completed: completeOnboarding ? true : Boolean(modelPreferences.onboarding_completed),
    asr_model: options.asr_model,
    speaker_model: options.speaker_model,
  };
  if (surface) surface.setAttribute("aria-busy", "true");
  if (saveButton) saveButton.disabled = true;
  if (recommendedButton) recommendedButton.disabled = true;
  if (status) status.textContent = "正在保存……";
  try {
    const saved = await api("/api/settings/preferences", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(payload),
    });
    modelPreferences = {...modelPreferences, ...(saved.preferences || payload)};
    if (capabilities) capabilities.preferences = modelPreferences;
    if (status) status.textContent = "已保存。之后新任务会使用这套模型方案。";
    closeOnboarding({restoreFocus: false});
    renderModelCenter();
    return true;
  } catch (error) {
    if (status) status.textContent = `保存失败：${error.message}`;
    return false;
  } finally {
    if (surface) surface.setAttribute("aria-busy", "false");
    if (saveButton) saveButton.disabled = false;
    if (recommendedButton) recommendedButton.disabled = false;
  }
}

async function applyRecommendedModels({completeOnboarding = true} = {}) {
  const catalog = capabilities?.models || {};
  const choose = (models, currentId) => {
    const list = Array.isArray(models) ? models : [];
    const recommended = list.find((model) => model?.recommended && model.selectable !== false && model.installed !== false);
    const current = list.find((model) => model?.id === currentId && model.selectable !== false && model.installed !== false);
    return recommended?.id || current?.id || null;
  };
  const asrId = choose(catalog.asr, modelPreferences.asr_model);
  const speakerId = choose(catalog.speaker, modelPreferences.speaker_model);
  if (asrId) modelPreferences.asr_model = asrId;
  if (speakerId) modelPreferences.speaker_model = speakerId;
  renderModelCenter();
  return saveModelPreferences({completeOnboarding});
}

let onboardingReturnFocus = null;

function closeOnboarding({restoreFocus = true} = {}) {
  $("onboarding-overlay")?.classList.add("hidden");
  syncBackgroundInert();
  const target = onboardingReturnFocus;
  onboardingReturnFocus = null;
  if (restoreFocus && target?.isConnected) {
    window.requestAnimationFrame(() => target.focus());
  }
}

function openOnboarding(force = false) {
  if (!force && modelPreferences.onboarding_completed) return;
  const overlay = $("onboarding-overlay");
  if (!overlay) return;
  onboardingReturnFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
  overlay.classList.remove("hidden");
  syncBackgroundInert();
  window.requestAnimationFrame(() => $("onboarding-recommended")?.focus());
}

async function loadCapabilities() {
  const modelSurface = $("model-settings-surface");
  const loadStatus = $("model-settings-load-status");
  if (modelSurface) modelSurface.setAttribute("aria-busy", "true");
  setSidebarServiceStatus("checking", "正在连接本地服务…");
  if (loadStatus) loadStatus.textContent = "正在读取本机模型配置";
  let settingsLoaded = false;
  try {
    capabilities = await api("/api/settings");
    settingsLoaded = true;
    hideAccessTokenOverlay();
  } catch (_) {
    capabilities = {local_first: true, preferences: modelPreferences, models: {}};
  }
  modelPreferences = {...modelPreferences, ...(capabilities.preferences || {})};
  renderModelCenter();
  renderBackendCompatibility();
  refreshAutoIngest().catch(() => {});
  refreshPassportSessions().catch(() => {});
  renderMobileAccess();
  renderMobileCapture();
  if (!settingsLoaded) {
    setSidebarServiceStatus("error", "本地服务状态未知 · 已显示安全默认值");
  } else {
    const bindText = String($("sidebar-bind-status")?.textContent || "本机处理");
    const needsRestart = backendMissingFeatures().length > 0;
    setSidebarServiceStatus(
      needsRestart ? "warning" : "ready",
      needsRestart ? `${bindText} · 需要重启` : bindText,
    );
  }
  if (modelSurface) modelSurface.setAttribute("aria-busy", "false");
  if (loadStatus) {
    loadStatus.textContent = settingsLoaded
      ? "本机模型配置已读取"
      : "本机模型配置读取失败，已显示安全默认值";
  }
  openOnboarding(false);
}

function localDateString(date) {
  return formatDayKey(date);
}

function ensureStatisticsRange(days = 7) {
  const toField = $("stats-date-to");
  const fromField = $("stats-date-from");
  if (!toField || !fromField) return;
  if (fromField.value && toField.value) return;
  const journalDate = journalDays.find((item) => item?.date)?.date;
  const end = localDayDate(journalDate) || localDayDate(formatDayKey(new Date()));
  const start = new Date(end.getTime() - (Math.max(1, Number(days) || 7) - 1) * 86400000);
  toField.value = localDateString(end);
  fromField.value = localDateString(start);
}

function setStatisticsRange(days) {
  const toField = $("stats-date-to");
  const fromField = $("stats-date-from");
  if (!toField || !fromField) return;
  const journalDate = journalDays.find((item) => item?.date)?.date;
  const end = localDayDate(journalDate) || localDayDate(formatDayKey(new Date()));
  const start = new Date(end.getTime() - (Math.max(1, Number(days) || 7) - 1) * 86400000);
  toField.value = localDateString(end);
  fromField.value = localDateString(start);
}

async function sendMobileBrief() {
  const status = $("mobile-brief-action-status");
  const button = $("mobile-send-brief");
  if (status) status.textContent = "正在生成并发送……";
  if (button) button.disabled = true;
  try {
    const payload = await api("/api/daily-brief", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: "{}",
    }, {operation: "发送今日简报"});
    const delivery = payload.delivery || {};
    const webhook = delivery.webhook;
    if (webhook?.ok) {
      if (status) status.textContent = "今日简报已交给 webhook / 推送桥。";
    } else if (delivery.outbox_path) {
      if (status) status.textContent = `今日简报已写入本地 outbox：${delivery.outbox_path}`;
    } else if (webhook && !webhook.ok) {
      if (status) status.textContent = `推送失败：${webhook.error || `HTTP ${webhook.status || "未知"}`}；如配置了 outbox 可从本地重试。`;
    } else if (status) {
      status.textContent = "简报已生成；默认 outbox 应写入 output/local-web/outbox/daily-brief。";
    }
  } catch (error) {
    if (status) status.textContent = `发送失败：${error.message}`;
  } finally {
    if (button) button.disabled = false;
  }
}

function statsBarRows(items) {
  const maximum = Math.max(1, ...items.map((item) => Number(item.value || 0)));
  return items.map((item) => {
    const value = Number(item.value || 0);
    const width = Math.max(value ? 7 : 0, Math.round((value / maximum) * 100));
    const row = cloneRow("stats-bar-row", '<div class="stats-bar-row"><span></span><div><i></i></div><strong></strong></div>');
    setRowText(row, "span", item.label);
    row.querySelector("i").style.width = `${width}%`;
    setRowText(row, "strong", item.display ?? value);
    return row;
  });
}

function statisticsScopeLabel(stats = {}) {
  const scope = String(stats.accounting_scope || stats.scope || stats.event_view?.scope || "").toLowerCase();
  const view = String(stats.view || stats.event_view?.view || "").toLowerCase();
  const scopeText = ({provisional: "暂定/实时口径", settled: "已结算口径", formal: "正式口径", live: "实时口径"})[scope] || (scope ? scope : "");
  const viewText = ({display: "展示视图", all: "全部事件", raw: "原始事件"})[view] || (view ? view : "");
  const counts = stats.event_view?.selected_counts?.source_kind_counts
    || stats.source_kind_counts
    || stats.event_view?.counts
    || null;
  let sourceText = "";
  if (counts && typeof counts === "object") {
    const labels = {provisional: "实时", settled: "正式", live: "实时", formal: "正式", watch: "Watch", local: "本地"};
    sourceText = Object.entries(counts)
      .filter(([, value]) => Number.isFinite(Number(value)))
      .map(([key, value]) => `${labels[key] || key} ${formatCount(value)}`)
      .join(" / ");
  }
  return [scopeText, viewText, sourceText].filter(Boolean).join(" · ");
}

function renderStatistics(stats = {}) {
  const hasData = Number(stats.days_with_data || 0) > 0 || Number(stats.event_count || 0) > 0;
  const empty = $("stats-empty");
  if (empty) empty.classList.toggle("hidden", hasData);
  if ($("stats-event-count")) $("stats-event-count").textContent = formatCount(stats.event_count || 0);
  if ($("stats-exercise-count")) $("stats-exercise-count").textContent = formatCount(stats.exercise_count || 0);
  if ($("stats-days-count")) $("stats-days-count").textContent = formatCount(stats.days_with_data || 0);

  const categoryCounts = stats.category_counts || {};
  const categoryItems = ["routine", "food", "expense", "exercise", "work_communication", "idea", "todo"].map((category) => ({
    label: CATEGORY_LABELS[category] || category,
    value: Number(categoryCounts[category] || 0),
  }));
  if ($("stats-category-counts")) mountRows($("stats-category-counts"), statsBarRows(categoryItems));

  const routine = stats.routine_distribution || {};
  const routineItems = ["00-05", "06-11", "12-17", "18-23"].map((bucket) => ({
    label: `${bucket} 时`,
    value: Number(routine[bucket] || 0),
  }));
  if ($("stats-routine-distribution")) mountRows($("stats-routine-distribution"), statsBarRows(routineItems));

  if ($("stats-status")) {
    const scopeLabel = statisticsScopeLabel(stats);
    $("stats-status").textContent = `${formatDayKey(stats.date_from) || "—"} 至 ${formatDayKey(stats.date_to) || "—"} · ${formatCount(stats.days_with_data || 0)} 个有数据日期 · ${formatCount(stats.event_count || 0)} 个去重事件${scopeLabel ? ` · 口径：${scopeLabel}` : ""}。`;
  }
}

function setStatisticsBusy(isBusy) {
  $("stats-surface")?.setAttribute("aria-busy", isBusy ? "true" : "false");
  const button = $("refresh-stats");
  if (button) {
    button.disabled = Boolean(isBusy);
    button.textContent = isBusy ? "汇总中…" : "查看统计";
  }
}

async function refreshStatistics() {
  ensureStatisticsRange();
  const dateFrom = String($("stats-date-from")?.value || "");
  const dateTo = String($("stats-date-to")?.value || "");
  if (!dateFrom || !dateTo) return;
  if ($("stats-status")) $("stats-status").textContent = "正在汇总结构化事件……";
  setStatisticsBusy(true);
  try {
    const params = new URLSearchParams({from: dateFrom, to: dateTo});
    const stats = await api(`/api/stats?${params.toString()}`, {signal: viewAbortSignal("stats")}, {silent: true, operation: "读取统计"});
    renderStatistics(stats);
    refreshStatisticsExpenseDrilldown(selectedJournalDate || dateTo).catch(() => {});
    refreshStatisticsExpenseRange(dateFrom, dateTo).catch(() => {});
  } catch (error) {
    if (error.aborted) return;
    if ($("stats-status")) $("stats-status").textContent = `统计读取失败：${error.message}`;
    throw error;
  } finally {
    setStatisticsBusy(false);
  }
}

async function refreshStatisticsExpenseRange(from, to) {
  const totalEl = $("stats-expense-range");
  const metaEl = $("stats-expense-range-meta");
  if (!totalEl || !metaEl || !from || !to) return;
  totalEl.textContent = "…";
  metaEl.textContent = "读取中";
  try {
    const payload = await api(
      `/api/expense-range?from=${encodeURIComponent(from)}&to=${encodeURIComponent(to)}`,
      {signal: viewAbortSignal("stats")},
      {silent: true, operation: "读取区间消费账本"},
    );
    const total = Number(payload?.total_cny);
    totalEl.textContent = Number.isFinite(total) ? formatMoney(total) : "—";
    const dayCount = Array.isArray(payload?.by_day) ? payload.by_day.length : 0;
    const missing = Array.isArray(payload?.missing_days) ? payload.missing_days.map(String) : [];
    metaEl.textContent = `账本口径 · ${dayCount} 天${missing.length ? ` · 缺 ${missing.length} 天` : " · 无缺失"}`;
    metaEl.title = missing.length
      ? `缺失日报：${missing.join("、")}（缺天不计入总额）`
      : "区间内每天都有日报，全部计入总额";
  } catch (error) {
    if (error.aborted) return;
    totalEl.textContent = "—";
    metaEl.textContent = "区间账本读取失败";
    metaEl.title = String(error.message || error);
  }
}

async function refreshStatisticsExpenseDrilldown(date) {
  const host = $("stats-expense-drilldown");
  if (!host || !date) return;
  host.innerHTML = '<p class="hint">正在读取当日消费明细…</p>';
  try {
    const daily = await api(
      `/api/daily?date=${encodeURIComponent(date)}&scope=provisional&view=display`,
      {signal: viewAbortSignal("stats")},
      {silent: true, operation: "读取当日消费明细"},
    );
    const ledger = daily && daily.expense_ledger;
    const html = typeof ExpenseDrilldown !== "undefined" ? ExpenseDrilldown.build(ledger) : "";
    const total = typeof ExpenseDrilldown !== "undefined" ? ExpenseDrilldown.cnyTotal(ledger) : null;
    if ($("stats-expense-total")) $("stats-expense-total").textContent = total === null ? "—" : formatMoney(total);
    host.innerHTML = html
      ? `<div class="stats-expense-drilldown-head"><strong>${escapeHtml(date)} 当日消费</strong><span>点击金额旁“分类明细”逐级展开</span></div>${html}`
      : `<p class="hint">${escapeHtml(date)} 暂无已确认消费明细。</p>`;
  } catch (error) {
    if (!error.aborted) {
      if ($("stats-expense-total")) $("stats-expense-total").textContent = "—";
      host.innerHTML = `<p class="hint">当日消费明细读取失败：${escapeHtml(error.message)}</p>`;
    }
  }
}

function ensurePatternRange(days = 14) {
  const toField = $("patterns-date-to");
  const fromField = $("patterns-date-from");
  if (!toField || !fromField) return;
  if (fromField.value && toField.value) return;
  const journalDate = journalDays.find((item) => item?.date)?.date;
  const end = localDayDate(journalDate) || localDayDate(formatDayKey(new Date()));
  const start = new Date(end.getTime() - (Math.max(1, Number(days) || 14) - 1) * 86400000);
  toField.value = localDateString(end);
  fromField.value = localDateString(start);
}

function setPatternRange(days) {
  const toField = $("patterns-date-to");
  const fromField = $("patterns-date-from");
  if (!toField || !fromField) return;
  const journalDate = journalDays.find((item) => item?.date)?.date;
  const end = localDayDate(journalDate) || localDayDate(formatDayKey(new Date()));
  const start = new Date(end.getTime() - (Math.max(1, Number(days) || 14) - 1) * 86400000);
  toField.value = localDateString(end);
  fromField.value = localDateString(start);
}

// 证据列表同样走模板克隆：行标记只解析一次，之后每条证据克隆节点填文本。
function renderPatternEvidence(evidence = []) {
  if (!evidence.length) {
    const empty = document.createElement("div");
    empty.className = "pattern-evidence-empty";
    empty.textContent = "没有可展示证据。";
    return empty;
  }
  const details = cloneRow("pattern-evidence", '<details class="pattern-evidence"><summary></summary><div></div></details>');
  details.querySelector("summary").textContent = `查看证据 · ${formatCount(evidence.length)} 条`;
  const list = details.lastElementChild;
  evidence.forEach((item) => {
    const article = cloneRow("pattern-evidence-item", '<article><div><strong></strong><span></span></div><p></p></article>');
    setRowText(article, "strong", item.date || "—");
    setRowText(article, "span", item.absolute || "");
    setRowText(article, "p", item.title || item.quote || "未命名事件");
    if (item.quote) {
      const quote = document.createElement("blockquote");
      quote.textContent = item.quote;
      article.append(quote);
    }
    list.append(article);
  });
  return details;
}

function renderPatterns(payload = {}) {
  const hint = $("patterns-sample-hint");
  if (hint) {
    hint.textContent = payload.sample_hint || `建议至少积累 ${formatCount(payload.minimum_sample_days || 3)} 个有数据日期后再解读规律。`;
    hint.classList.toggle("ready", Boolean(payload.sample_ready));
  }
  if ($("patterns-status")) {
    $("patterns-status").textContent = `${formatDayKey(payload.date_from) || "—"} 至 ${formatDayKey(payload.date_to) || "—"} · ${formatCount(payload.days_with_data || 0)} 个有数据日期 · ${formatCount(payload.event_count || 0)} 个事件。`;
  }
  const alerts = Array.isArray(payload.alert_candidates) ? payload.alert_candidates : [];
  const alertRoot = $("pattern-alerts");
  if (alertRoot) {
    if (!alerts.length) {
      alertRoot.replaceChildren();
    } else {
      const heading = cloneRow("pattern-alert-heading", '<div class="pattern-alert-heading"><strong>预警候选</strong><span>仅观察，不是诊断</span></div>');
      const cards = alerts.map((item) => {
        const card = cloneRow("pattern-alert-card", '<article class="pattern-alert-card"><div><span></span><strong></strong></div><p></p></article>');
        setRowText(card, "span", "观察候选");
        setRowText(card, "strong", item.label || item.kind || "提醒");
        setRowText(card, "p", item.statement || "");
        card.append(renderPatternEvidence(item.evidence || []));
        return card;
      });
      alertRoot.replaceChildren(heading, ...cards);
    }
  }
  const items = Array.isArray(payload.observations) ? payload.observations : [];
  const root = $("patterns-results");
  if (!root) return;
  if (items.length) {
    root.setAttribute("role", "list");
    root.setAttribute("aria-label", "规律观察结果");
    mountRows(root, items.map((item) => {
      const card = cloneRow("pattern-card", '<article class="pattern-card" role="listitem"><div class="pattern-card-head"><div><span></span><strong></strong></div><small></small></div><p></p></article>');
      setRowText(card, ".pattern-card-head span", "观察");
      setRowText(card, ".pattern-card-head strong", item.label || item.kind || "规律");
      setRowText(card, ".pattern-card-head small", `${formatCount(item.metrics?.sample_count || item.metrics?.days_with_data || item.evidence?.length || 0)} 个样本`);
      setRowText(card, "p", item.statement || "");
      card.append(renderPatternEvidence(item.evidence || []));
      return card;
    }));
  } else {
    root.removeAttribute("role");
    root.removeAttribute("aria-label");
    root.innerHTML = '<div class="search-empty" role="status"><strong>当前范围还没有可展示规律</strong><span>继续积累结构化生活事件后再查看。</span></div>';
  }
}

function setPatternsBusy(isBusy) {
  $("patterns-surface")?.setAttribute("aria-busy", isBusy ? "true" : "false");
  $("patterns-results")?.setAttribute("aria-busy", isBusy ? "true" : "false");
  const button = $("refresh-patterns");
  if (button) {
    button.disabled = Boolean(isBusy);
    button.textContent = isBusy ? "读取中…" : "查看规律";
  }
}

function renderPatternsLoading() {
  const root = $("patterns-results");
  if (!root) return;
  root.removeAttribute("role");
  root.removeAttribute("aria-label");
  root.innerHTML = '<div class="search-empty" role="status"><strong>正在读取规律观察</strong><span>会基于当前日期范围汇总已有结构化事件。</span></div>';
}

async function refreshPatterns() {
  ensurePatternRange();
  const dateFrom = String($("patterns-date-from")?.value || "");
  const dateTo = String($("patterns-date-to")?.value || "");
  if (!dateFrom || !dateTo) return;
  if ($("patterns-status")) $("patterns-status").textContent = "正在读取多日规律观察……";
  const params = new URLSearchParams({from: dateFrom, to: dateTo});
  setPatternsBusy(true);
  renderPatternsLoading();
  try {
    const payload = await api(`/api/patterns?${params.toString()}`, {signal: viewAbortSignal("patterns")}, {silent: true, operation: "读取规律观察"});
    renderPatterns(payload);
  } catch (error) {
    if (error.aborted) return;
    if ($("patterns-status")) $("patterns-status").textContent = `规律读取失败：${error.message}`;
    throw error;
  } finally {
    setPatternsBusy(false);
  }
}

function formatBytes(size) {
  const bytes = Number(size) || 0;
  if (bytes < 1024) return INT_BYTE.format(bytes);
  if (bytes < 1024 * 1024) return INT_KILOBYTE.format(bytes / 1024);
  return INT_MEGABYTE.format(bytes / (1024 * 1024));
}

function formatElapsed(seconds) {
  const total = Math.max(0, Math.round(seconds || 0));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const secs = total % 60;
  return hours ? `${formatCount(hours)}小时 ${formatCount(minutes)}分 ${formatCount(secs)}秒` : `${formatCount(minutes)}分 ${formatCount(secs)}秒`;
}

function setImportLiveStatus(state, label, detail = "") {
  const chip = $("import-live-status");
  const surface = $("manual-import-surface");
  if (chip) {
    chip.textContent = label;
    chip.className = `setting-badge ${state || ""}`.trim();
    chip.title = detail || label;
    chip.setAttribute("aria-label", detail ? `${label}：${detail}` : label);
  }
  if (surface) surface.setAttribute("aria-busy", state === "checking" ? "true" : "false");
  const status = $("upload-status");
  if (status) status.className = `status-copy ${state || ""}`.trim();
}

function addFiles(fileList) {
  for (const file of fileList) {
    const key = `${file.webkitRelativePath || file.name}:${file.size}:${file.lastModified}`;
    selected.set(key, file);
  }
  renderSelected();
}

const IMPORT_MEDIA_EXTENSIONS = new Set([
  "wav", "m4a", "mp3", "flac", "aac", "ogg", "opus", "mp4", "mov", "webm",
]);

function supportedImportFiles(fileList) {
  return [...(fileList || [])].filter((file) => {
    const type = String(file?.type || "").toLowerCase();
    if (type.startsWith("audio/")) return true;
    const name = String(file?.name || "");
    const extension = name.includes(".") ? name.split(".").pop().toLowerCase() : "";
    return IMPORT_MEDIA_EXTENSIONS.has(extension);
  });
}

function setManualDropActive(active) {
  const hero = $("manual-drop-hero");
  if (!hero) return;
  hero.classList.toggle("drag-active", Boolean(active));
}

function handleManualImportDrop(event) {
  event.preventDefault();
  setManualDropActive(false);
  const files = supportedImportFiles(event.dataTransfer?.files);
  if (!files.length) {
    setImportLiveStatus("warning", "没有可导入的音频", "请拖入支持的音频或媒体文件");
    const status = $("upload-status");
    if (status) status.textContent = "没有找到支持的音频文件。";
    return;
  }
  addFiles(files);
  setImportLiveStatus("ready", `已拖入 ${formatCount(files.length)} 个`);
}

function setStartButtonBusy(isBusy) {
  const button = $("start-button");
  if (!button) return;
  button.setAttribute("aria-busy", isBusy ? "true" : "false");
  button.textContent = isBusy ? "正在创建 / 恢复任务…" : "创建任务并开始处理";
  button.disabled = Boolean(isBusy) || selected.size === 0;
  if (isBusy) setImportLiveStatus("checking", "处理中", "正在创建、上传或恢复任务");
}

function renderSelected({preserveUploadStatus = false} = {}) {
  const files = [...selected.values()];
  const startButton = $("start-button");
  if (startButton?.getAttribute("aria-busy") !== "true") startButton.disabled = files.length === 0;
  const box = $("selected-files");
  const status = $("selected-files-status");
  if (!files.length) {
    box.className = "selected-files empty";
    box.removeAttribute("role");
    box.removeAttribute("aria-live");
    box.removeAttribute("aria-atomic");
    box.removeAttribute("aria-label");
    box.innerHTML = '<div><strong>还没有选择录音</strong><span>拖入音频，或从文件 / 文件夹中选择。选中后会先在这里确认，再开始处理。</span></div>';
    if (status) status.textContent = "等待你选择要处理的录音";
    if (!preserveUploadStatus && startButton?.getAttribute("aria-busy") !== "true") {
      const uploadStatus = $("upload-status");
      if (uploadStatus) {
        uploadStatus.textContent = "等待你选择要处理的录音";
        uploadStatus.className = "status-copy";
      }
      setImportLiveStatus("", "等待选择");
    }
    return;
  }
  box.className = "selected-files";
  box.setAttribute("role", "list");
  box.setAttribute("aria-label", "待导入录音文件");
  box.removeAttribute("aria-live");
  box.removeAttribute("aria-atomic");
  mountRows(box, files.map((file) => {
    const row = cloneRow("file-row", '<div class="file-row" role="listitem"><span class="file-name"></span><span class="file-meta"></span></div>');
    setRowText(row, ".file-name", file.webkitRelativePath || file.name);
    setRowText(row, ".file-meta", formatBytes(file.size));
    return row;
  }));
  if (status) {
    const totalBytes = files.reduce((sum, file) => sum + Number(file.size || 0), 0);
    status.textContent = `已选择 ${formatCount(files.length)} 个文件，共 ${formatBytes(totalBytes)}`;
    if (startButton?.getAttribute("aria-busy") !== "true") setImportLiveStatus("ready", `已选 ${formatCount(files.length)} 个`);
  }
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (char) => ({"&":"&amp;", "<":"&lt;", ">":"&gt;", "\"":"&quot;", "'":"&#39;"}[char]));
}

function highlightSearchText(value, query) {
  const source = String(value || "");
  const needle = String(query || "").trim();
  if (!needle) return escapeHtml(source);
  const lower = source.toLocaleLowerCase();
  const target = needle.toLocaleLowerCase();
  let cursor = 0;
  let output = "";
  while (cursor < source.length) {
    const index = lower.indexOf(target, cursor);
    if (index < 0) {
      output += escapeHtml(source.slice(cursor));
      break;
    }
    output += escapeHtml(source.slice(cursor, index));
    output += `<mark>${escapeHtml(source.slice(index, index + needle.length))}</mark>`;
    cursor = index + needle.length;
  }
  return output || escapeHtml(source);
}

function shanghaiDateTimeLabel(value, fallback = "时间未知") {
  const timestamp = String(value || "").trim();
  if (!timestamp) return fallback;
  const millis = Date.parse(timestamp);
  if (!Number.isFinite(millis)) return fallback;
  const parts = Object.fromEntries(
    new Intl.DateTimeFormat("en-CA", {
      timeZone: "Asia/Shanghai",
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      hourCycle: "h23",
    }).formatToParts(new Date(millis)).map((part) => [part.type, part.value]),
  );
  return `${parts.year}-${parts.month}-${parts.day} · ${parts.hour}:${parts.minute}`;
}

// 统一上海时区时间字段解析，避免各处重复声明同一套 Intl.DateTimeFormat 配置。
function shanghaiTimeParts(millis, fields, locale) {
  return Object.fromEntries(
    new Intl.DateTimeFormat(locale, {
      timeZone: "Asia/Shanghai",
      ...fields,
      hourCycle: "h23",
    }).formatToParts(new Date(millis)).map((part) => [part.type, part.value]),
  );
}

function watchRecordingTitle(value) {
  const timestamp = String(value || "").trim();
  const millis = Date.parse(timestamp);
  if (!Number.isFinite(millis)) return "Apple Watch 录音";
  const parts = shanghaiTimeParts(millis, {
    month: "numeric",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }, "zh-CN");
  return `${Number(parts.month)}月${Number(parts.day)}日 ${parts.hour}:${parts.minute} 录音`;
}

function formatHumanRecordingTitle(job, file) {
  const rawPath = String(file?.relative_path || file?.filename || "").trim();
  const isWatch = rawPath.startsWith("watch-") || rawPath.includes("watch-sessions") || /^[0-9a-fA-F]{32}/.test(rawPath);

  const tsMatch = rawPath.match(/(\d{4})[-_]?(\d{2})[-_]?(\d{2})[-_T ](\d{2})[-_:]?(\d{2})/);
  if (tsMatch) {
    const [_, year, month, day, hour, min] = tsMatch;
    const prefix = isWatch ? "Apple Watch · " : "";
    return `${prefix}${parseInt(month, 10)}月${parseInt(day, 10)}日 ${hour}:${min} 录音`;
  }

  const dateStr = file?.start_at || job?.created_at || job?.started_at;
  if (dateStr) {
    const millis = Date.parse(dateStr);
    if (Number.isFinite(millis)) {
      const parts = shanghaiTimeParts(millis, {
        month: "numeric",
        day: "numeric",
        hour: "2-digit",
        minute: "2-digit",
      }, "zh-CN");
      const prefix = isWatch ? "Apple Watch · " : "";
      return `${prefix}${Number(parts.month)}月${Number(parts.day)}日 ${parts.hour}:${parts.minute} 录音`;
    }
  }

  if (/^[0-9a-fA-F-]{24,}$/.test(rawPath) || isWatch) {
    return isWatch ? "Apple Watch 录音" : "本地录音";
  }

  const basename = rawPath.split(/[/\\]/).pop() || rawPath;
  return basename || "未命名录音";
}

function watchRecordingDetailTime(value, fallback) {
  const millis = Date.parse(String(value || "").trim());
  if (!Number.isFinite(millis)) return fallback;
  if (millis > Date.now() + 5 * 60 * 1000) return "设备时间异常";
  return shanghaiDateTimeLabel(value, fallback);
}

function watchRecordingSortMillis(value) {
  const millis = Date.parse(String(value || "").trim());
  if (!Number.isFinite(millis)) return Number.NEGATIVE_INFINITY;
  if (millis > Date.now() + 5 * 60 * 1000) return Number.NEGATIVE_INFINITY;
  return millis;
}

function watchRecordingFreshnessLabel(value) {
  const millis = Date.parse(String(value || "").trim());
  if (!Number.isFinite(millis)) return "时间未知";
  const deltaSeconds = Math.round((Date.now() - millis) / 1000);
  if (deltaSeconds < -300) return "设备时间异常";
  if (deltaSeconds < 60) return "刚刚";
  if (deltaSeconds < 3600) return `${formatCount(Math.floor(deltaSeconds / 60))} 分钟前`;
  return shanghaiDateTimeLabel(value, "时间未知");
}

function compactDurationLabel(milliseconds) {
  const raw = Number(milliseconds);
  if (!Number.isFinite(raw) || raw < 0) return "时长未知";
  const totalSeconds = Math.round(raw / 1000);
  if (totalSeconds < 60) return `${formatCount(totalSeconds)} 秒`;
  if (totalSeconds >= 3600) {
    const hours = Math.floor(totalSeconds / 3600);
    const minutes = Math.floor((totalSeconds % 3600) / 60);
    return minutes ? `${formatCount(hours)} 小时 ${formatCount(minutes)} 分` : `${formatCount(hours)} 小时`;
  }
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  return seconds ? `${formatCount(minutes)} 分 ${formatCount(seconds)} 秒` : `${formatCount(minutes)} 分钟`;
}

function searchResultTime(item) {
  return shanghaiDateTimeLabel(item.timestamp, item.date || "时间未知");
}

const IMPORT_SOURCE_LABELS = {
  "apple-watch": "Apple Watch",
  "ai-passport-wifi": "AI Passport",
  "file-upload": "文件导入",
};

function importSourceLabel(source) {
  return IMPORT_SOURCE_LABELS[source] || "文件导入";
}

function setSearchResultsListMode(enabled) {
  const root = $("search-results");
  if (!root) return;
  if (enabled) {
    root.setAttribute("role", "list");
    root.setAttribute("aria-label", "搜索结果");
    return;
  }
  root.removeAttribute("role");
  root.removeAttribute("aria-label");
}

function renderSearchEmpty(title, detail) {
  const root = $("search-results");
  if (!root) return;
  setSearchResultsListMode(false);
  root.innerHTML = `<div class="search-empty" role="status"><strong>${escapeHtml(title)}</strong><span>${escapeHtml(detail)}</span></div>`;
}

function renderSearchLoading() {
  const root = $("search-results");
  if (!root) return;
  setSearchResultsListMode(false);
  root.innerHTML = '<div class="search-empty search-loading" role="status"><strong>正在搜索本机记录</strong><span>结果会在读取完成后出现在这里。</span></div>';
}

function setSearchBusy(isBusy) {
  $("search-form")?.setAttribute("aria-busy", isBusy ? "true" : "false");
  $("search-results")?.setAttribute("aria-busy", isBusy ? "true" : "false");
  const submit = $("search-submit");
  if (submit) {
    submit.disabled = Boolean(isBusy);
    submit.textContent = isBusy ? "搜索中…" : "搜索";
  }
}

function eventAudioCoordinates(event) {
  if (!event) return null;
  const jobId = event._jobId || event.job_id || event.jobId;
  const artifactPath = event._artifactPath || event.artifact_path || event.artifactPath;
  if (!jobId || !artifactPath) return null;

  let start = null;
  let end = null;
  if (typeof event.evidence?.audio_start_offset_seconds === "number") {
    start = event.evidence.audio_start_offset_seconds;
    end = typeof event.evidence?.audio_end_offset_seconds === "number" ? event.evidence.audio_end_offset_seconds : start + 5.0;
  } else if (typeof event.time?.audio_start_offset_seconds === "number") {
    start = event.time.audio_start_offset_seconds;
    end = typeof event.time?.audio_end_offset_seconds === "number" ? event.time.audio_end_offset_seconds : start + 5.0;
  } else if (typeof event.time?.start_seconds === "number") {
    start = event.time.start_seconds;
    end = typeof event.time?.end_seconds === "number" ? event.time.end_seconds : start + 5.0;
  } else if (typeof event.time?.relative_seconds === "number") {
    start = event.time.relative_seconds;
    const quoteLen = (event.evidence?.quote || event.title || "").length;
    const dur = Math.max(3.0, Math.min(15.0, quoteLen * 0.35 + 1.0));
    end = start + dur;
  } else if (typeof event.audio_start_offset_seconds === "number") {
    start = event.audio_start_offset_seconds;
    end = typeof event.audio_end_offset_seconds === "number" ? event.audio_end_offset_seconds : start + 5.0;
  } else if (typeof event.start_offset_seconds === "number") {
    start = event.start_offset_seconds;
    end = typeof event.end_offset_seconds === "number" ? event.end_offset_seconds : start + 5.0;
  } else if (typeof event.relative_seconds === "number") {
    start = event.relative_seconds;
    const quoteLen = (event.quote || event.text || "").length;
    const dur = Math.max(3.0, Math.min(15.0, quoteLen * 0.35 + 1.0));
    end = start + dur;
  }

  if (start === null || !Number.isFinite(start)) return null;
  if (end === null || !Number.isFinite(end) || end <= start) end = start + 5.0;
  return { jobId, artifactPath, start, end };
}

let activeAudioClip = null;
let activeAudioPlayButton = null;

function stopCurrentAudioClip() {
  if (activeAudioClip) {
    try {
      activeAudioClip.pause();
      activeAudioClip.currentTime = 0;
    } catch (_) {}
    activeAudioClip = null;
  }
  if (activeAudioPlayButton) {
    activeAudioPlayButton.classList.remove("playing");
    const label = activeAudioPlayButton.dataset.defaultLabel || "<span class='search-play-icon'>▶</span> 原声";
    activeAudioPlayButton.innerHTML = label;
    activeAudioPlayButton = null;
  }
}

async function toggleAudioClipPlayback(button) {
  if (activeAudioPlayButton === button) {
    stopCurrentAudioClip();
    return;
  }
  stopCurrentAudioClip();

  const jobId = button.dataset.jobId;
  const artifactPath = button.dataset.artifactPath;
  const start = parseFloat(button.dataset.start);
  const end = parseFloat(button.dataset.end);
  if (!jobId || !artifactPath || !Number.isFinite(start) || !Number.isFinite(end)) return;

  const token = typeof accessToken !== "undefined" ? accessToken : (readStorage("sessionStorage", "life-logger-access-token") || "");
  const url = `/api/jobs/${encodeURIComponent(jobId)}/audio-clip?path=${encodeURIComponent(artifactPath)}&start=${start.toFixed(2)}&end=${end.toFixed(2)}${token ? `&token=${encodeURIComponent(token)}` : ""}`;
  const audio = new Audio(url);
  activeAudioClip = audio;
  activeAudioPlayButton = button;
  button.classList.add("playing");
  button.innerHTML = `❚❚ ${formatSeconds(end - start)}`;

  audio.onended = () => {
    stopCurrentAudioClip();
  };
  audio.onerror = () => {
    stopCurrentAudioClip();
  };

  try {
    await audio.play();
  } catch (err) {
    stopCurrentAudioClip();
    console.warn("Audio clip playback error", err);
  }
}

function formatDayHeading(dateStr) {
  if (!dateStr || dateStr === "未知日期") return "未知日期";
  const today = shanghaiDate(0);
  const yesterday = shanghaiDate(-1);
  const beforeYesterday = shanghaiDate(-2);
  let prefix = "";
  if (dateStr === today) prefix = "今天";
  else if (dateStr === yesterday) prefix = "昨天";
  else if (dateStr === beforeYesterday) prefix = "前天";

  try {
    const parts = dateStr.split("-").map(Number);
    if (parts.length === 3) {
      const weekDay = formatWeekday(dateStr);
      const dayKey = formatDayKey(dateStr);
      return prefix ? `${prefix} · ${dayKey} (${weekDay})` : `${dayKey} (${weekDay})`;
    }
  } catch (_) {}
  return prefix ? `${prefix} · ${dateStr}` : dateStr;
}

function renderSearchResults(results, query) {
  const root = $("search-results");
  if (!root) return;
  if (!results.length) {
    renderSearchEmpty(
      query ? `没有找到“${query}”` : "当前筛选没有结果",
      query ? "可以换一个更短的关键词，或取消部分筛选。" : "可以放宽日期、分类或金额范围后再试。",
    );
    return;
  }
  setSearchResultsListMode(true);

  // 按天聚合检索记录：每天一条综合卡片（统一合并同日期条目）
  const dayGroups = new Map();
  for (const item of results) {
    // 【2026-10-15 恢复】今早 08:10 的 UI 合并把上海时区换算退回了 UTC 日期切片。
    // 深圳是 UTC+8，凌晨 0-8 点的记录会被算成前一天，检索结果就归进错误的日期分组。
    // 已改回 journalShanghaiDateFromTimestamp。
    let rawDate = item.date;
    if (!rawDate && item.timestamp) {
      rawDate = journalShanghaiDateFromTimestamp(item.timestamp, "");
    }
    const dayKey = (String(rawDate || "").trim().match(/^\d{4}-\d{2}-\d{2}/) || [""])[0] || (rawDate ? String(rawDate).trim() : "未知日期");
    if (!dayGroups.has(dayKey)) {
      dayGroups.set(dayKey, []);
    }
    dayGroups.get(dayKey).push(item);
  }

  // 按日期降序排列（最新在前）
  const sortedDays = [...dayGroups.keys()].sort((a, b) => {
    if (a === "未知日期") return 1;
    if (b === "未知日期") return -1;
    return b.localeCompare(a);
  });

  const totalDays = sortedDays.length;

  const toolbarHtml = `
    <div class="search-results-toolbar">
      <div class="search-results-toolbar-stats">
        <span>共 <strong>${formatCount(results.length)}</strong> 条记录</span>
        <span class="dot-sep">·</span>
        <span>已按天合成为 <strong>${formatCount(totalDays)}</strong> 条每日记录</span>
      </div>
      <div class="search-results-toolbar-actions">
        <button type="button" class="search-results-action-btn" id="search-expand-all">全部展开</button>
        <button type="button" class="search-results-action-btn" id="search-collapse-all">全部收起</button>
      </div>
    </div>
  `;

  const daysHtml = sortedDays.map((dayKey) => {
    const dayItems = dayGroups.get(dayKey);
    const dayCount = dayItems.length;
    const events = dayItems.filter((it) => it.result_type === "event");
    const utterances = dayItems.filter((it) => it.result_type === "utterance");
    // 【2026-10-19 A0 派单 · 一套口径】这里原来有一次本地金额求和：
    //   totalExpense = dayItems.reduce(sum + expense_amount_cny)
    // 它是「搜索命中项口径」而不是账本口径：同一天的消费可能没被关键词命中、
    // 也可能命中同一笔的描述句/复盘句，所以它天然不等于账本，绝对不能当展示金额。
    // 现在改为：本地不求和，展示金额由 /api/stats 的 daily_expenses（expense_policy
    // 账本，按天分桶）在渲染后回填，与 /api/daily 完全同源。

    const categoriesSet = new Set();
    dayItems.forEach((it) => {
      (it.categories || []).forEach((c) => categoriesSet.add(c));
    });
    const categoryTags = [...categoriesSet].slice(0, 4).map((c) => `<span class="search-category">${escapeHtml(CATEGORY_LABELS[c] || c)}</span>`).join("");

    const dayItemsHtml = dayItems.map((item) => {
      const isEvent = item.result_type === "event";
      const categories = (item.categories || []).map((category) => CATEGORY_LABELS[category] || category);
      const typeLabel = isEvent ? "生活事件" : "本人发言";
      const sourceLabel = item.source_type === "manual" ? "人工确认" : (isEvent ? "录音事件" : "本人声纹");
      const categoryHtml = categories.slice(0, 3).map((category) => `<span class="search-category">${escapeHtml(category)}</span>`).join("");
      const amount = Number(item.expense_amount_cny);
      const amountHtml = isEvent && Number.isFinite(amount)
        ? `<span class="search-category amount">${formatMoney(amount)}</span>`
        : "";
      const quote = isEvent && item.quote && item.quote !== item.text
        ? `<div class="search-evidence">“${highlightSearchText(item.quote, query)}”</div>`
        : "";
      const href = item.artifact_path ? viewerLink(item.job_id, item.artifact_path) : "#";
      const audioCoords = eventAudioCoordinates(item);
      const playBtnHtml = audioCoords
        ? `<button type="button" class="search-audio-play-btn" data-job-id="${escapeHtml(audioCoords.jobId)}" data-artifact-path="${escapeHtml(audioCoords.artifactPath)}" data-start="${audioCoords.start}" data-end="${audioCoords.end}" data-default-label="<span class='search-play-icon'>▶</span> 原声" title="原地试听录音原声"><span class="search-play-icon">▶</span> 原声</button>`
        : "";
      return `<a class="search-result-card" role="listitem" href="${href}" target="_blank" rel="noopener">
        <div class="search-result-head">
          <div class="search-result-badges"><span class="search-type ${isEvent ? "event" : "utterance"}">${typeLabel}</span>${categoryHtml}${amountHtml}</div>
          <div style="display:flex;align-items:center;gap:8px;">
            ${playBtnHtml}
            <time>${escapeHtml(searchResultTime(item))}</time>
          </div>
        </div>
        <div class="search-result-text">${highlightSearchText(item.text, query)}</div>
        ${quote}
        <div class="search-result-foot"><span>${escapeHtml(importSourceLabel(item.import_source))}</span><span>${escapeHtml(sourceLabel)}</span><span>${escapeHtml(item.source_file || "原始录音")}</span><strong>查看证据 →</strong></div>
      </a>`;
    }).join("");

    return `
      <section class="search-day-card" data-day="${escapeHtml(dayKey)}">
        <div class="search-day-head" data-day-toggle="${escapeHtml(dayKey)}" role="button" tabindex="0" aria-expanded="true" aria-controls="search-day-body-${escapeHtml(dayKey)}" title="点击展开/收起此日记录">
          <div class="search-day-head-left">
            <span class="search-day-icon">📅</span>
            <h3 class="search-day-title">${escapeHtml(formatDayHeading(dayKey))}</h3>
            <span class="search-day-badge count">${formatCount(dayCount)} 条</span>
            ${events.length ? `<span class="search-day-badge event">${formatCount(events.length)} 个事件</span>` : ""}
            ${utterances.length ? `<span class="search-day-badge utterance">${formatCount(utterances.length)} 句发言</span>` : ""}
            <span class="search-day-badge expense" data-search-day-expense="${escapeHtml(dayKey)}" style="display:none;"></span>
            ${categoryTags}
          </div>
          <div class="search-day-head-right">
            <span class="search-day-toggle-label">收起</span>
            <span class="search-day-toggle-icon">▾</span>
          </div>
        </div>
        <div class="search-day-body" id="search-day-body-${escapeHtml(dayKey)}">
          ${dayItemsHtml}
        </div>
      </section>
    `;
  }).join("");

  root.innerHTML = toolbarHtml + daysHtml;
  hydrateSearchDayExpenses(sortedDays);
}

// 【2026-10-19 A0 派单 P-6 · 一套口径】用**账本接口**回填搜索视图的每日消费徽标。
//
// 口径说明（写实，别再搞错）：
//   · `/api/stats.daily_expenses` 是**事件口径**（statistics.py 按 numeric_amount 逐笔累加，
//     且包含 kind=transfer 的转账类交易），**不是账本金额**，不能拿来当"这天花了多少钱"。
//   · 唯一可用的区间口径是 `GET /api/expense-range`：它由 event_view + expense_policy 产出，
//     与 `/api/daily` 的 `expense_ledger.total_amount` 同源；`by_day[].total_cny` 就是当日账本额。
//   · 某天 `source="missing"`（没日报/没账本依据）→ 徽标显示「—」，**绝不显示 0**。
const searchDayExpenseCache = new Map();   // date -> {total_cny, source}
let searchDayExpenseInflight = null;

async function hydrateSearchDayExpenses(dayKeys) {
  const nodes = [...document.querySelectorAll("[data-search-day-expense]")];
  if (!nodes.length) return;
  const days = [...new Set(nodes.map((node) => node.dataset.searchDayExpense).filter(Boolean))].sort();
  if (!days.length) return;
  const missing = days.filter((key) => !searchDayExpenseCache.has(key));

  const paint = () => {
    nodes.forEach((node) => {
      const row = searchDayExpenseCache.get(node.dataset.searchDayExpense);
      if (!row) return;
      if (row.source === "missing") {
        node.textContent = "消费 —";
        node.title = "该日没有账本依据（无日报/无消费记录），不按 0 计入";
        node.style.display = "";
        return;
      }
      node.textContent = `消费 ${formatMoney(row.total_cny || 0)}`;
      node.title = "来自账本接口 /api/expense-range 的 by_day（event_view + expense_policy）";
      node.style.display = "";
    });
  };
  if (!missing.length) { paint(); return; }

  if (!searchDayExpenseInflight) {
    const from = days[0];
    const to = days[days.length - 1];
    searchDayExpenseInflight = api(
      `/api/expense-range?from=${encodeURIComponent(from)}&to=${encodeURIComponent(to)}`,
      {},
      {operation: "读取搜索结果的每日消费账本", silent: true},
    )
      .then((payload) => {
        (payload.by_day || []).forEach((row) => {
          const key = String(row.date || "");
          if (key) searchDayExpenseCache.set(key, {total_cny: row.total_cny, source: row.source});
        });
      })
      .catch(() => {
        // 账本接口不可用（例如服务还没加载新路由）→ 徽标保持隐藏，不退回自己算钱
      })
      .finally(() => { searchDayExpenseInflight = null; });
  }
  await searchDayExpenseInflight;
  paint();
}

function currentSearchCriteria() {
  return {
    query: String($("global-search-input")?.value || "").trim(),
    dateFrom: String($("search-date-from")?.value || ""),
    dateTo: String($("search-date-to")?.value || ""),
    category: String($("search-category")?.value || ""),
    importSource: String($("search-import-source")?.value || "all"),
    amountMin: String($("search-amount-min")?.value || ""),
    amountMax: String($("search-amount-max")?.value || ""),
  };
}

function hasSearchCriteria() {
  const criteria = currentSearchCriteria();
  return Boolean(
    criteria.query
    || criteria.dateFrom
    || criteria.dateTo
    || criteria.category
    || criteria.amountMin
    || criteria.amountMax
    || criteria.importSource !== "all"
  );
}

let searchRequestGeneration = 0;

async function runGlobalSearch() {
  const requestGeneration = ++searchRequestGeneration;
  const summary = $("search-summary");
  const {query, dateFrom, dateTo, category, amountMin, amountMax, importSource} = currentSearchCriteria();
  if (!query && !dateFrom && !dateTo && !category && !amountMin && !amountMax && importSource === "all") {
    setSearchBusy(false);
    if (summary) summary.textContent = "输入关键词，或选择日期 / 分类 / 金额后开始搜索。";
    renderSearchEmpty("输入关键词或使用筛选", "可以直接查看某段日期内的消费、运动、待办等结构化事件。");
    return;
  }
  const activeFilter = document.querySelector(".search-filter.active");
  const resultType = activeFilter?.dataset.searchType || "all";
  const params = new URLSearchParams({q: query, type: resultType, limit: "100"});
  if (dateFrom) params.set("from", dateFrom);
  if (dateTo) params.set("to", dateTo);
  if (category) params.set("category", category);
  if (amountMin) params.set("amount_min", amountMin);
  if (amountMax) params.set("amount_max", amountMax);
  params.set("import_source", importSource);
  params.set("scope", "provisional");
  params.set("view", "display");
  setSearchBusy(true);
  if (summary) summary.textContent = "正在搜索统一生活事件与原始发言……";
  renderSearchLoading();
  try {
    const payload = await api(`/api/events/search?${params.toString()}`, {signal: viewAbortSignal("search")});
    if (requestGeneration !== searchRequestGeneration) return;
    const results = payload.results || [];
    const contract = payload.read_contract || {};
    const eventBranch = category || amountMin || amountMax || resultType === "event";
    const typeLabel = resultType === "utterance"
      ? "原始发言（讲述时间，不等于已确认事实）"
      : eventBranch
        ? "统一生活事件（canonical event_view）"
        : "统一生活事件 + 原始发言（讲述时间，两类不合并计数）";
    const qualifiers = [];
    if (contract.accounting_scope || contract.scope) qualifiers.push(`口径 ${contract.accounting_scope || contract.scope}`);
    if (contract.view) qualifiers.push(`视图 ${contract.view}`);
    if (category) qualifiers.push(CATEGORY_LABELS[category] || category);
    if (importSource !== "all") qualifiers.push(importSourceLabel(importSource));
    if (dateFrom || dateTo) qualifiers.push(`${dateFrom ? formatDayKey(dateFrom) : "最早"} → ${dateTo ? formatDayKey(dateTo) : "现在"}`);
    if (amountMin || amountMax) qualifiers.push(`${formatMoney(amountMin || 0)} → ${amountMax ? formatMoney(amountMax) : "不限"}`);
    if (summary) summary.textContent = `找到 ${formatCount(results.length)} 条 · ${typeLabel}${qualifiers.length ? ` · ${qualifiers.join(" · ")}` : ""}`;
    renderSearchResults(results, query);
  } catch (error) {
    if (error.aborted) return;
    if (requestGeneration !== searchRequestGeneration) return;
    if (error.status === 404) {
      if (summary) summary.textContent = "后端尚未加载统一事件搜索，请更新服务";
      renderSearchEmpty("需要更新本地服务", "当前服务没有 /api/events/search。普通搜索不会退回旧的 /api/search 原始结果。");
      return;
    }
    if (summary) summary.textContent = `搜索失败：${error.message}`;
    renderSearchEmpty("搜索没有完成", "检查本地服务或筛选条件后再试一次。");
  } finally {
    if (requestGeneration === searchRequestGeneration) setSearchBusy(false);
  }
}

function renderPassportReceiverHealth(receiver = {}) {
  const badge = $("passport-stream-badge");
  const summary = $("passport-receiver-summary");
  const httpChip = $("passport-http-health");
  const discoveryChip = $("passport-discovery-health");
  const importChip = $("passport-import-health");
  const enabled = Boolean(receiver.enabled);
  const httpRunning = Boolean(receiver.http_running ?? receiver.running);
  const discoveryRunning = Boolean(receiver.discovery_running ?? receiver.running);
  const running = enabled && httpRunning && discoveryRunning;
  const problem = receiver.degraded_reason || receiver.error || "receiver 未运行";
  const httpPort = Number(receiver.http_port || 8766);
  const discoveryPort = Number(receiver.discovery_port || 8767);
  const autoStart = receiver.auto_start;

  if (badge) {
    badge.textContent = running ? "Companion 正常" : enabled ? "Companion 异常" : "Companion 关闭";
    badge.className = `setting-badge ${running ? "ready" : enabled ? "warning" : ""}`;
    badge.setAttribute("aria-label", `AI Passport companion：${badge.textContent}`);
  }
  if (summary) {
    const importMode = autoStart === true ? "完整会话自动处理" : autoStart === false ? "完整会话仅建草稿" : "完整会话策略未知";
    summary.textContent = running
      ? `Companion 正常 · TCP ${httpPort} 收音频 · UDP ${discoveryPort} 自动发现 · ${importMode}`
      : enabled
        ? `Companion 部分异常 · ${problem}`
        : "Companion 已关闭";
  }
  if (httpChip) {
    httpChip.textContent = `TCP ${httpPort} · ${httpRunning ? "正在监听" : "未监听"}`;
    httpChip.className = `receiver-health-chip ${httpRunning ? "ready" : enabled ? "warning" : "off"}`;
    httpChip.setAttribute("aria-label", `音频接收端口 TCP ${httpPort}：${httpRunning ? "正常监听" : "未监听"}`);
  }
  if (discoveryChip) {
    discoveryChip.textContent = `UDP ${discoveryPort} · ${discoveryRunning ? "自动发现正常" : "自动发现异常"}`;
    discoveryChip.className = `receiver-health-chip ${discoveryRunning ? "ready" : enabled ? "warning" : "off"}`;
    discoveryChip.setAttribute("aria-label", `自动发现端口 UDP ${discoveryPort}：${discoveryRunning ? "正常" : "异常"}`);
  }
  if (importChip) {
    const policy = autoStart === true ? "自动处理" : autoStart === false ? "仅建草稿" : "策略未知";
    importChip.textContent = `完整会话 · ${policy}`;
    importChip.className = `receiver-health-chip ${autoStart === true ? "ready" : autoStart === false ? "off" : "checking"}`;
    importChip.setAttribute("aria-label", `完整 AI Passport 会话：${policy}`);
  }
  return {enabled, running, problem, autoStart};
}

function setPassportReceiverUnknown(message) {
  const badge = $("passport-stream-badge");
  const summary = $("passport-receiver-summary");
  const status = $("passport-stream-status");
  if (badge) {
    badge.textContent = "状态未知";
    badge.className = "setting-badge warning";
    badge.setAttribute("aria-label", "AI Passport companion：状态未知");
  }
  if (summary) summary.textContent = message;
  for (const [id, label] of [["passport-http-health", "TCP 8766"], ["passport-discovery-health", "UDP 8767"], ["passport-import-health", "完整会话处理"]]) {
    const chip = $(id);
    if (!chip) continue;
    chip.textContent = `${label} · 检查失败`;
    chip.className = "receiver-health-chip warning";
    chip.setAttribute("aria-label", `${label}：状态检查失败`);
  }
  if (status) status.textContent = message;
}

function renderPassportSessions(payload = {}) {
  const root = $("passport-stream-sessions");
  $("passport-stream-surface")?.setAttribute("aria-busy", "false");
  const status = $("passport-stream-status");
  if (!root) return;
  root.setAttribute("aria-busy", "false");
  const receiver = payload.receiver || {};
  const sessions = Array.isArray(payload.sessions) ? payload.sessions : [];
  const receiverHealth = renderPassportReceiverHealth(receiver);

  if (!sessions.length) {
    root.removeAttribute("role");
    root.removeAttribute("aria-label");
    root.innerHTML = '<div class="journal-empty" role="status"><strong>还没有 AI Passport 录音会话</strong><span>Companion 就绪后，Passport 的录音会话会自动出现在这里。</span></div>';
    if (status) {
      status.textContent = receiverHealth.running
        ? "Companion 已就绪；Passport 与本机同网后会自动发现 Life Logger，配网页 endpoint 可以留空。"
        : receiverHealth.enabled
          ? `Companion 没有完全就绪：${receiverHealth.problem}。可检查 TCP 8766 / UDP 8767 占用和防火墙。`
          : "AI Passport companion 已关闭；需要时在 local-config.json 开启。";
    }
    return;
  }

  root.setAttribute("role", "list");
  root.setAttribute("aria-label", "AI Passport 最近录音会话");
  const latest = sessions[0];
  mountRows(root, sessions.slice(0, 8).map((item) => {
    const timeLabel = shanghaiDateTimeLabel(
      item.updated_at,
      String(item.updated_at || "时间未知"),
    );
    const received = Number(item.received_unique_chunks || 0);
    const expected = item.expected_chunks === null || item.expected_chunks === undefined
      ? "?"
      : Number(item.expected_chunks);
    const missing = Array.isArray(item.missing_chunks) ? item.missing_chunks : [];
    const stateLabel = item.status === "complete"
      ? "完整"
      : item.status === "incomplete"
        ? `缺片 ${missing.join(",") || "未知"}`
        : item.status === "event"
          ? `设备事件 · ${formatCount(item.event_count || 0)} 条`
          : item.status === "stalled"
            ? "接收停滞 · 未收到结束标记"
            : "接收中";
    const job = item.job_id ? ` · ${item.job_id}` : "";
    const stateClass = ["incomplete", "stalled"].includes(item.status) ? "error" : `session-${item.status || "receiving"}`;
    const rowLabel = `AI Passport ${item.device || "device"}，会话 ${item.session || "session"}，${stateLabel}，${received}/${expected} 片`;
    const row = cloneRow("passport-session-row", '<div class="auto-ingest-history-row" role="listitem"><time></time><strong></strong><em></em></div>');
    row.classList.add(stateClass);
    row.setAttribute("aria-label", rowLabel);
    setRowText(row, ":scope > time", timeLabel);
    setRowText(row, ":scope > strong", `AI Passport · ${item.device || "device"} · ${item.session || "session"} · ${received}/${expected} 片`);
    setRowText(row, ":scope > em", `${stateLabel}${job}`);
    if (item.audio_url) {
      const player = document.createElement("audio");
      player.controls = true;
      player.preload = "metadata";
      player.src = item.audio_url;
      row.append(player);
    }
    if (item.import_error) {
      const importError = document.createElement("small");
      importError.className = "error";
      importError.textContent = `导入失败：${item.import_error}`;
      row.append(importError);
    }
    return row;
  }));

  if (status) {
    const sessionStatus = latest.status === "complete"
      ? `最近完整会话已保留原始分片并${latest.job_id ? `进入任务 ${latest.job_id}` : "等待导入"}。`
      : latest.status === "incomplete"
        ? `最近会话不完整：缺片 ${(latest.missing_chunks || []).join(", ") || "未知"}；不会伪装成完整录音。`
        : latest.status === "event"
          ? "最近收到 AI Passport 设备事件；已保留原始事件证据，不会伪造成录音。"
          : latest.status === "stalled"
            ? `最近会话已超过 ${formatCount(Math.round(Number(latest.stale_after_seconds || 30)))} 秒没有新分片且未收到结束标记；原始分片继续保留，不会导入成假完整录音。`
            : "正在等待结束标记；当前分片会保存在本机。";
    status.textContent = receiverHealth.running
      ? `${sessionStatus} Companion 仍正常监听。`
      : `${sessionStatus} Companion 当前异常：${receiverHealth.problem}。`;
  }
}

// 分段预览直接生成经过转义的 HTML 字符串。旧实现返回 DocumentFragment，
// 却被拼进模板字符串，页面会显示字面量对象字符串；
// 这里不再把 DOM 片段当字符串用，所有用户可读字段都显式转义。
function renderWatchPreviewSegments(preview) {
  const labels = {
    completed: "已完成",
    no_speech: "无语音",
    failed: "处理失败",
    processing: "处理中",
    pending: "等待处理",
  };
  const sourceLabels = {
    self: "本人",
    non_self: "非本人 / 媒体",
    unknown: "待确认",
  };
  const segments = Array.isArray(preview?.segments) ? preview.segments.slice(-8) : [];
  return segments.map((segment) => {
    const status = segment?.status in labels ? segment.status : "pending";
    const state = labels[status];
    const start = watchRecordingDetailTime(segment?.start_at, "开始时间未知");
    const duration = compactDurationLabel(segment?.duration_ms);
    const rows = Array.isArray(segment?.rows) ? segment.rows.slice(0, 8) : [];
    const transcriptRows = rows.map((row) => {
      const text = row && typeof row.text === "string" ? row.text : "";
      const metaParts = [];
      if (typeof row?.source_label === "string" && row.source_label) {
        const sourceLabel = sourceLabels[row.source_label] || row.source_label;
        metaParts.push(`<span>来源：${escapeHtml(sourceLabel)}</span>`);
      }
      const similarityValue = typeof row?.voice_similarity === "number"
        ? row.voice_similarity
        : Number.NaN;
      if (Number.isFinite(similarityValue) && similarityValue >= 0 && similarityValue <= 1) {
        metaParts.push(`<span>相似度：${escapeHtml(formatPercent(similarityValue))}</span>`);
      }
      return `<div class="watch-preview-transcript"><p>${escapeHtml(text || "（空转写）")}</p><small>${metaParts.join("")}</small></div>`;
    }).join("");
    let emptyState = "";
    if (!rows.length) {
      emptyState = segment?.status === "no_speech"
        ? "本段无语音"
        : segment?.status === "completed"
          ? "本段没有转写行"
          : "";
    }
    const empty = emptyState ? `<p class="watch-preview-empty">${escapeHtml(emptyState)}</p>` : "";
    const error = segment?.status === "failed" && segment?.error
      ? `<p class="watch-preview-error">${escapeHtml(String(segment.error).slice(0, 240))}</p>`
      : "";
    return `<article class="watch-preview-segment watch-preview-${status}" role="listitem"><div class="watch-preview-segment-head"><strong>${escapeHtml(`第 ${segment?.sequence ?? "?"} 段 · ${state}`)}</strong><time>${escapeHtml(`${start} · ${duration}`)}</time></div>${transcriptRows}${empty}${error}</article>`;
  }).join("");
}

function watchPreviewCount(preview, key) {
  const value = Number(preview?.[key]);
  return Number.isFinite(value) && value >= 0 ? Math.floor(value) : 0;
}

function renderFormalWatchJob(job, sessionAsr = null) {
  const asr = sessionAsr || job?.asr_progress || null;
  const counts = asr?.counts || {};
  const completedSegments = (counts.completed || 0) + (counts.no_speech || 0);
  const totalSegments = completedSegments + (counts.processing || 0) + (counts.pending || 0) + (counts.failed || 0);
  const autoRetry = asr?.automatic_retry || job?.context_auto_retry || null;
  const failedSegments = counts.failed || autoRetry?.failed_segments || 0;

  let label = "正式转写任务就绪";
  let details = "";

  if (autoRetry && failedSegments > 0) {
    if (autoRetry.next_retry_in_seconds === null || autoRetry.next_retry_in_seconds === undefined) {
      label = `正式ASR有 ${failedSegments} 段失败 · 等待后台继续处理`;
      details = "后台后续扫描会继续尝试重试";
    } else {
      label = `正式ASR有 ${failedSegments} 段失败 · 已安排自动重试`;
      const secs = Number(autoRetry.next_retry_in_seconds);
      if (secs <= 0) {
        details = "下次重试即将执行";
      } else if (secs < 60) {
        details = "下次重试不足 1 分钟";
      } else {
        const mins = Math.ceil(secs / 60);
        details = `下次重试约 ${mins} 分钟后`;
      }
    }
  } else if (job?.status === "unavailable") {
    label = "整场任务状态暂不可用";
    details = "无法读取任务详细状态";
  } else if (job?.status === "draft" && job?.stage === "waiting_for_files") {
    label = "正式任务已建立 · 等待音频进入ASR";
    details = "等待录音文件传输";
  } else if (job?.status === "running") {
    label = "整场处理中";
    const stageName = typeof stageLabel === "function" ? stageLabel(job.stage) : (job.stage || "");
    details = `${stageName} · 正式ASR分段 ${completedSegments}/${totalSegments} · 已生成 ${asr?.transcript_count || 0} 句`;
  } else if (job?.status === "completed") {
    const contextRetry = job.context_auto_retry;
    const contextExt = job.context_extraction;
    if (contextRetry?.status === "scheduled") {
      label = "整场处理已完成 · 事件提取等待自动恢复";
      details = "已安排后台自动恢复";
    } else if (contextRetry?.status === "running") {
      label = "整场处理已完成 · 事件提取自动恢复中";
      details = "成功批次会复用";
    } else if (contextExt?.status === "failed") {
      label = "整场处理已完成 · 事件提取失败";
      details = "事件提取失败";
    } else if (contextExt?.status === "degraded") {
      label = "整场处理已完成 · 事件提取部分完成";
      details = "事件提取未覆盖全部内容";
    } else if (job.no_speech_count) {
      label = `整场处理已完成 · ${job.no_speech_count} 个文件无语音`;
      details = job.no_speech_reason || "原音频已保留，跳过声源归属与事件提取。";
    } else {
      label = "整场处理已完成";
      details = `正式ASR分段 ${completedSegments}/${totalSegments}`;
    }
  } else if (asr?.status === "processing" || asr?.status === "running") {
    label = `正式ASR处理中 · ${completedSegments}/${totalSegments} 段已完成`;
    details = `正式ASR分段 ${completedSegments}/${totalSegments} · 已生成 ${asr?.transcript_count || 0} 句`;
  }

  const rows = asr?.latest_transcript_rows || [];
  let transcript = "";
  if (rows.length) {
    transcript = `<div class="watch-formal-transcript"><div class="watch-formal-transcript-head">正式转写句子</div><ul>` +
      rows.map((r) => `<li>${escapeHtml(r.text || "")}</li>`).join("") +
      `</ul></div>`;
  }

  let fullTranscriptLink = "";
  if (job?.transcript_artifact_path && job?.job_id) {
    const href = typeof viewerLink === "function"
      ? viewerLink(job.job_id, job.transcript_artifact_path)
      : `/viewer.html?job=${encodeURIComponent(job.job_id)}&path=${encodeURIComponent(job.transcript_artifact_path)}`;
    fullTranscriptLink = `<a class="watch-formal-link" href="${escapeHtml(href)}">查看完整ASR转写</a>`;
  }

  return { label, details, transcript, fullTranscriptLink };
}

// ---- Watch 设备自报 vs 服务端证实（2026-10-23）--------------------------------
// 设备自报“20 段待传”不等于服务端真缺 20 段。只有 manifest 算出的缺口才算积压，
// 其余按“未证实/过期快照”展示，避免把设备排队画成“需要立即重传”。
function watchSnapshotAgeLabel(status) {
  const seconds = Number(status && status.reported_age_seconds);
  if (!Number.isFinite(seconds) || seconds < 0) return "";
  const minutes = Math.floor(seconds / 60);
  if (minutes < 1) return "刚刚";
  if (minutes < 90) return `${minutes} 分钟前`;
  return `${Math.floor(minutes / 60)} 小时前`;
}

function watchTruthModel(status) {
  if (!status || typeof status !== "object") return null;
  const manifest = status.reported_vs_manifest && typeof status.reported_vs_manifest === "object"
    ? status.reported_vs_manifest : {};
  const verdict = String(manifest.verdict || "");
  const reported = Math.max(0, Number(status.pending_segment_count ?? manifest.reported_pending_segment_count ?? 0) || 0);
  const missing = Math.max(0, Number(status.missing_segments ?? manifest.server_missing_segments ?? 0) || 0);
  const extra = Math.max(0, Number(status.extra_segments ?? manifest.server_extra_segments ?? 0) || 0);
  const hasTruth = Object.prototype.hasOwnProperty.call(status, "pending_is_real") || Boolean(verdict);
  const age = watchSnapshotAgeLabel(status);
  const offline = String(status.offline || "");
  const silentSeconds = Number(status.silent_seconds);
  const silentHours = Number.isFinite(silentSeconds) ? silentSeconds / 3600 : NaN;
  let tone = "checking", headline = "", detail = "", action = "等待设备上报";

  // 设备失联优先于积压判定：旧逻辑 missing=0 && pending=0 会直接落到 clean，
  // 把“8 小时没上报”显示成“已同步”。
  if (offline === "alert" || offline === "warn") {
    const hours = Number.isFinite(silentHours) ? silentHours.toFixed(1) : "?";
    const clock = status.offline_since_clock ? `（上次 ${status.offline_since_clock}）` : "";
    tone = offline === "alert" ? "error" : "warning";
    headline = `手表已 ${hours} 小时未上报`;
    detail = `${clock}设备可能未佩戴、未充电或未连接；下面是服务端最后确认的状态。`;
    action = "检查手表佩戴与充电";
  } else if (!hasTruth) {
    // 旧后端：宁可用谨慎措辞，也不假装“已确认积压”。
    tone = "warning";
    headline = `设备自报 ${reported} 段待传`;
    detail = "旧服务端未校验设备自报，不能当成已确认的积压。";
  } else if (missing > 0 || status.pending_is_real === true) {
    tone = "error";
    headline = `待补传 ${missing || reported} 段`;
    detail = "服务端 manifest 确认缺失，需要设备补传。";
    action = "等待设备补传";
  } else if (verdict === "reported_only_stale" || status.stale === true) {
    tone = "warning";
    headline = `设备自报 ${reported} 段，服务端未证实`;
    detail = `服务端缺口为 0${age ? `；快照 ${age}` : ""}，不能当成真实积压。`;
  } else if (verdict === "reported_only") {
    tone = "warning";
    headline = `设备自报 ${reported} 段待确认`;
    detail = "服务端缺口为 0，可能是设备本地队列尚未上传。";
  } else if (verdict === "clean") {
    tone = "ready";
    headline = "已同步";
    detail = "设备自报与服务端 manifest 一致，无待传。";
    action = "";
  } else if (reported > 0) {
    headline = `设备自报 ${reported} 段待确认`;
    detail = "服务端未确认这批待传，先按未证实的自报数展示。";
  } else {
    tone = "ready";
    headline = "已同步";
    detail = "设备与服务端一致，无待传。";
    action = "";
  }

  return {
    deviceId: String(status.device_id || ""),
    tone, headline, detail, action,
    extra: extra > 0 ? `另收到超出预期 ${extra} 段（单独计数，不抵缺口）` : "",
  };
}

// ---------------------------------------------------------------------------
// 停录原因（Watch ended_by）。三种来源必须分开说，禁止推断成"用户停止"：
//   client_reported → 设备上报的真实原因
//   inferred        → 设备没上报，明确写"原因未知"
//   missing         → 旧 manifest 没这个字段，明确写"旧数据无记录"
// ---------------------------------------------------------------------------
const WATCH_ENDED_BY_LABELS = {
  user: "本人手动停止",
  battery: "电量耗尽",
  disconnect: "连接断开",
  app_crash: "App 崩溃",
  watch_client: "设备端结束",
};

function watchStopReasonModel(item) {
  const source = String((item && item.ended_by_source) || "missing");
  if (source === "client_reported") {
    const raw = String((item && item.ended_by) || "").trim();
    const known = Object.prototype.hasOwnProperty.call(WATCH_ENDED_BY_LABELS, raw)
      ? WATCH_ENDED_BY_LABELS[raw]
      : (raw ? `设备上报：${raw}` : "设备上报（未给出具体原因）");
    return {tone: "ready", label: known, sourceText: "设备上报"};
  }
  if (source === "inferred") {
    return {tone: "warning", label: "设备未上报，原因未知", sourceText: "推断"};
  }
  return {tone: "muted", label: "旧数据无记录", sourceText: "无记录"};
}

function watchStopClockText(value) {
  const text = String(value || "");
  const match = text.match(/T(\d{2}:\d{2})/);
  if (match) return match[1];
  const fallback = text.match(/(\d{2}:\d{2})/);
  return fallback ? fallback[1] : "";
}

function watchStopDiagnosticsHtml(payload) {
  // 旧后端没有这个端点时 payload 为 null / ok !== true —— 直接不渲染，
  // 绝不显示"0 条"或编一个原因出来。
  if (!payload || payload.ok !== true) return "";
  const stops = (Array.isArray(payload.recent_stops) ? payload.recent_stops : [])
    .filter((item) => item && typeof item === "object")
    .slice(0, 5);
  const counts = payload.counts && typeof payload.counts === "object" ? payload.counts : {};
  const unacked = Number(counts.unacked_completions);
  const hasUnacked = Number.isFinite(unacked);
  const showUnacked = hasUnacked && unacked > 0;
  if (!stops.length && !showUnacked) return "";

  const rows = stops.map((item) => {
    const model = watchStopReasonModel(item);
    const clock = watchStopClockText(item.ended_at);
    const device = String(item.device_id || "").replace(/^watch-/, "").slice(0, 6);
    return `<li class="watch-stop-row is-${escapeHtml(model.tone)}">`
      + `<time>${escapeHtml(clock || "时间未知")}</time>`
      + `<span class="watch-stop-reason">${escapeHtml(model.label)}</span>`
      + `<span class="watch-stop-source">${escapeHtml(model.sourceText)}${device ? ` · ${escapeHtml(device)}…` : ""}</span>`
      + `</li>`;
  }).join("");

  const unackedLine = showUnacked
    ? `<p class="watch-stop-unacked">${formatCount(unacked)} 场录音已结束，但本机没收到确认；这不代表你停止了录音。</p>`
    : "";

  return `<div class="watch-stop-diag" role="status" aria-live="polite">`
    + `<strong>最近停录原因</strong>`
    + (rows ? `<ul class="watch-stop-list">${rows}</ul>` : "")
    + unackedLine
    + `</div>`;
}

async function refreshWatchStopDiagnostics(anchorEl) {
  let html = "";
  try {
    const diagnostics = await api("/api/watch-sessions/diagnostics?recent=8", {},
      {silent: true, operation: "读取手表停录原因"});
    html = watchStopDiagnosticsHtml(diagnostics);
  } catch (error) {
    html = "";   // 404 / 超时 / 旧后端：隐藏，不显示 0 条，也不编原因
  }
  let host = $("watch-stop-diagnostics");
  if (!html) {
    host?.remove();
    return;
  }
  if (!host) {
    host = document.createElement("div");
    host.id = "watch-stop-diagnostics";
    host.setAttribute("aria-live", "polite");
    (anchorEl || $("watch-reception-status"))?.insertAdjacentElement("afterend", host);
  }
  host.innerHTML = html;
}

function watchTruthHtml(list) {
  const models = (Array.isArray(list) ? list : []).map(watchTruthModel).filter(Boolean);
  if (!models.length) return "";
  return models.map((model) => `<div class="status-copy ${model.tone}" data-watch-device="${escapeHtml(model.deviceId)}">`
    + `<strong>${escapeHtml(model.headline)}</strong>`
    + ` <span>${escapeHtml(model.detail)}</span>`
    + (model.extra ? ` <span>${escapeHtml(model.extra)}</span>` : "")
    + (model.action ? ` <small>动作：${escapeHtml(model.action)}</small>` : "")
    + `</div>`).join("");
}

async function refreshWatchReception() {
  const root = $("watch-reception-sessions");
  const status = $("watch-reception-status");
  if (!root || !status) return;
  root.setAttribute("aria-busy", "true");
  status.className = "status-copy checking";
  status.textContent = "正在读取 Apple Watch 接收记录…";
  try {
    const payload = await api("/api/watch-reception", {signal: viewAbortSignal("import")}, {silent: true, operation: "读取手表接收状态"});
    const rawSessions = Array.isArray(payload.sessions)
      ? payload.sessions.filter((item) => item && typeof item === "object")
      : [];
    const seenWatchSessions = new Set();
    const sessions = rawSessions.filter((item) => {
      const deviceID = String(item.device_id || "").trim();
      const sessionID = String(item.session_id || "").trim();
      if (!deviceID || !sessionID) return true;
      const key = `${deviceID}/${sessionID}`;
      if (seenWatchSessions.has(key)) return false;
      seenWatchSessions.add(key);
      return true;
    });
    sessions.sort(
      (left, right) =>
        watchRecordingSortMillis(right.started_at) - watchRecordingSortMillis(left.started_at),
    );
    const truthHtml = watchTruthHtml(payload.device_statuses);
    let truthHost = $("watch-device-truth");
    if (!truthHtml) {
      truthHost?.remove();
    } else {
      if (!truthHost) {
        truthHost = document.createElement("div");
        truthHost.id = "watch-device-truth";
        truthHost.setAttribute("aria-live", "polite");
        status.insertAdjacentElement("afterend", truthHost);
      }
      truthHost.innerHTML = truthHtml;
    }

    // 停录原因：不阻塞上面的列表渲染，端点不存在（旧后端 404）就整块隐藏。
    // 只认服务端给的 ended_by_source，绝不把「没确认」推断成「用户停止」。
    refreshWatchStopDiagnostics(truthHost || status).catch(() => {});
    status.textContent = sessions.length ? "以下为本机收到音频后的临时预览，不代表整场录音的正式处理结果。手表尚未上传的内容不在这里。" : "尚未收到手表切片。开始录音约一分钟后，首段才会上传。";
    status.className = "status-copy ready";
    if (!sessions.length) {
      root.innerHTML = '<div class="auto-ingest-empty" role="status">还没有收到 Apple Watch 录音。开始录音后，首个约 1 分钟切片上传成功时会出现在这里。</div>';
      return;
    }
    root.innerHTML = sessions.map(item => {
      const formalJobId = item.job_id || item.completion_job_id;
      const formalJob = item.formal_job || (formalJobId ? {status: "unavailable"} : null);
      const job = formalJob ? renderFormalWatchJob(formalJob, item.session_asr || formalJob.asr_progress) : {};
      const taskLink = formalJobId ? `<a href="${jobDetailLink(formalJobId)}" data-watch-job-id="${escapeHtml(formalJobId)}">查看任务详情</a>` : "";

      const title = watchRecordingTitle(item.started_at);
      const started = watchRecordingDetailTime(item.started_at, "开始时间未知");
      const updated = watchRecordingDetailTime(item.updated_at, "接收时间未知");
      const freshness = watchRecordingFreshnessLabel(item.updated_at || item.started_at);
      const duration = compactDurationLabel(item.received_duration_ms);
      const rawSegmentCount = Number(item.received_segments);
      const segmentCount = Number.isFinite(rawSegmentCount) && rawSegmentCount >= 0
        ? Math.floor(rawSegmentCount)
        : 0;
      const state = item.job_id
        ? "录音已收齐 · 已进入转写"
        : item.state === "receiving"
          ? segmentCount === 0
            ? "正在准备录音 · 等待首段"
            : `正在录音中 · 已录制 ${duration}（第 ${formatCount(segmentCount)} 段）`
          : item.state === "complete"
            ? "录音已收齐 · 等待处理"
            : "状态待确认";
      const preview = item.preview && typeof item.preview === "object" ? item.preview : {};
      const previewSegments = renderWatchPreviewSegments(preview);
      const completed = watchPreviewCount(preview, "completed_segments");
      const noSpeech = watchPreviewCount(preview, "no_speech_segments");
      const failed = watchPreviewCount(preview, "failed_segments");
      const processing = watchPreviewCount(preview, "processing_segments");
      const pending = watchPreviewCount(preview, "pending_segments");
      const previewSummary = `已完成 ${completed} 段 · 无语音 ${noSpeech} 段 · 处理中 ${processing} 段 · 待处理 ${pending} 段 · 失败 ${failed} 段`;
      const rowLabel = `${title}，${segmentCount} 段，时长 ${duration}，${state}，${previewSummary}`;
      return `<div class="auto-ingest-history-row watch-reception-session" role="listitem" aria-label="${escapeHtml(rowLabel)}"><time>${escapeHtml(started)}</time><strong>${escapeHtml(title)}</strong><em>${escapeHtml(state)}</em><small>Apple Watch · ${segmentCount} 段 · ${escapeHtml(duration)} · ${escapeHtml(freshness)} · 最近接收 ${escapeHtml(updated)}</small><div class="watch-preview"><div class="watch-preview-summary"><strong>临时预览</strong><span>${escapeHtml(previewSummary)}</span><small>仅显示最近 8 段；整场录音收齐后的正式处理结果以后续任务为准。</small></div><div class="watch-preview-segments" role="list">${previewSegments || '<p class="watch-preview-empty">暂时没有可显示的分段预览。</p>'}</div>${job.fullTranscriptLink || ""} ${taskLink}</div></div>`;
    }).join("");
  } catch (error) {
    if (error.aborted) return;
    status.className = "status-copy error";
    status.textContent = `接收状态读取失败：${error.message}`;
  } finally {
    root.setAttribute("aria-busy", "false");
  }
}

async function refreshPassportSessions() {
  const signal = viewAbortSignal("import");
  await refreshWatchReception();
  const features = new Set(Array.isArray(capabilities?.api_features) ? capabilities.api_features : []);
  if (!features.has("passport_stream_v1")) {
    renderPassportSessions({sessions: []});
    const status = $("passport-stream-status");
    if (status) status.textContent = "当前后台还没有 AI Passport 会话接口；重启 Life Logger Web 后再刷新。";
    return;
  }
  const root = $("passport-stream-sessions");
  $("passport-stream-surface")?.setAttribute("aria-busy", "true");
  if (root) root.setAttribute("aria-busy", "true");
  try {
    renderPassportSessions(await api("/api/passport-sessions", {signal}, {silent: true, operation: "读取 AI Passport 会话"}));
  } catch (error) {
    if (error.aborted) {
      // 取消（切走 import 视图）时归位 aria-busy，两个区域不能一直保持转圈。
      $("passport-stream-surface")?.setAttribute("aria-busy", "false");
      if (root) root.setAttribute("aria-busy", "false");
      return;
    }
    $("passport-stream-surface")?.setAttribute("aria-busy", "false");
    if (root) root.setAttribute("aria-busy", "false");
    setPassportReceiverUnknown(`AI Passport companion 状态读取失败：${error.message}`);
  }
}

function setAutoIngestLoading(isBusy, message = "") {
  $("auto-ingest-surface")?.setAttribute("aria-busy", isBusy ? "true" : "false");
  const status = $("auto-ingest-status");
  if (status && message) status.textContent = message;
  for (const id of ["enable-auto-ingest", "save-auto-ingest", "scan-auto-ingest", "choose-auto-ingest-folder"]) {
    const control = $(id);
    if (control) control.disabled = Boolean(isBusy);
  }
}

function renderAutoIngestHistory(history = []) {
  const root = $("auto-ingest-history");
  if (!root) return;
  if (!Array.isArray(history) || !history.length) {
    root.removeAttribute("role");
    root.removeAttribute("aria-label");
    root.innerHTML = '<div class="journal-empty" role="status"><strong>还没有自动导入活动</strong><span>启用监控目录后，实际导入、配置变化和错误会出现在这里。</span></div>';
    return;
  }
  root.setAttribute("role", "list");
  root.setAttribute("aria-label", "自动导入最近活动");
  mountRows(root, history.map((item) => {
    const timeLabel = shanghaiDateTimeLabel(item.at, String(item.at || "时间未知"));
    const row = cloneRow("auto-ingest-history-row", '<div class="auto-ingest-history-row" role="listitem"><time></time><strong></strong><em></em></div>');
    if (item.kind === "import") {
      const files = Array.isArray(item.files) ? item.files : [];
      const detail = files.length ? files.slice(0, 3).join("、") : "新录音";
      const strong = setRowText(row, ":scope > strong", `导入 ${formatCount(item.count || 0)} 个 · ${detail}`);
      strong.title = files.join("、");
      setRowText(row, ":scope > em", item.job_id || "");
    } else if (item.kind === "error") {
      row.classList.add("error");
      setRowText(row, ":scope > strong", item.error || "自动导入失败");
      setRowText(row, ":scope > em", "错误");
    } else {
      const folders = Array.isArray(item.folders) ? item.folders : [];
      setRowText(row, ":scope > strong", `${item.enabled ? "启用" : "暂停"}自动导入${folders.length ? ` · ${folders.join("、")}` : ""}`);
      setRowText(row, ":scope > em", "配置");
    }
    setRowText(row, ":scope > time", timeLabel);
    return row;
  }));
}

function renderAutoIngest(status = {}) {
  const featureSet = new Set(Array.isArray(capabilities?.api_features) ? capabilities.api_features : []);
  const featureReady = featureSet.has("auto_ingest_v1");
  const nativePickerReady = featureSet.has("native_folder_picker_v1");
  const folders = Array.isArray(status.folders) ? status.folders : [];
  const missingFolders = folders.filter((item) => !item.exists);
  const enabled = Boolean(status.enabled);
  const scanState = String(status.scan_state || (enabled ? "idle" : "paused"));
  const scanLabels = {
    scanning: "扫描中",
    waiting: "等待稳定",
    imported: "已导入",
    job_running: "任务处理中",
    folder_missing: "目录异常",
    error: "扫描失败",
    paused: "已暂停",
    idle: "空闲",
  };
  const badge = $("auto-ingest-badge");
  if (badge) {
    if (!featureReady) {
      badge.textContent = "需重启";
      badge.className = "setting-badge warning";
    } else if (status.last_error || scanState === "error") {
      badge.textContent = "扫描失败";
      badge.className = "setting-badge warning";
    } else if (enabled && (missingFolders.length || scanState === "folder_missing")) {
      badge.textContent = "目录异常";
      badge.className = "setting-badge warning";
    } else if (enabled) {
      badge.textContent = scanLabels[scanState] || "运行中";
      badge.className = "setting-badge ready";
    } else {
      badge.textContent = "已暂停";
      badge.className = "setting-badge";
    }
  }
  if ($("auto-ingest-enabled")) $("auto-ingest-enabled").checked = enabled;
  if ($("auto-ingest-folders")) $("auto-ingest-folders").value = folders.map((item) => item.path || item).join("\n");
  if ($("auto-ingest-stability")) $("auto-ingest-stability").value = String(status.stability_seconds || 20);
  if ($("auto-ingest-interval")) $("auto-ingest-interval").value = String(status.scan_interval_seconds || 15);
  if ($("auto-ingest-pending")) $("auto-ingest-pending").textContent = String(Number(status.pending_count || 0));
  if ($("auto-ingest-processed")) $("auto-ingest-processed").textContent = String(Number(status.processed_count || 0));
  if ($("auto-ingest-last-count")) $("auto-ingest-last-count").textContent = String(Number(status.last_import_count || 0));
  if ($("auto-ingest-last-job")) $("auto-ingest-last-job").textContent = status.last_job_id ? `${status.last_job_id}${status.last_job_status ? ` · ${status.last_job_status}` : ""}` : "—";
  if ($("auto-ingest-scan-state")) $("auto-ingest-scan-state").textContent = scanLabels[scanState] || scanState;
  const saveButton = $("save-auto-ingest");
  const enableButton = $("enable-auto-ingest");
  const scanButton = $("scan-auto-ingest");
  if (saveButton) saveButton.disabled = !featureReady;
  if (enableButton) {
    enableButton.disabled = !featureReady || enabled;
    enableButton.textContent = enabled ? "已启用" : "一键启用";
  }
  if (scanButton) scanButton.disabled = !featureReady;
  const pickerButton = $("choose-auto-ingest-folder");
  if (pickerButton) {
    pickerButton.hidden = !nativePickerReady;
    pickerButton.disabled = !featureReady;
  }
  $("auto-ingest-surface")?.setAttribute("aria-busy", "false");
  renderAutoIngestHistory(status.history || []);
  const copy = $("auto-ingest-status");
  if (copy) {
    if (!featureReady) {
      copy.textContent = "当前后台还没有自动导入接口。重启本地服务后自动启用。";
    } else if (status.last_error) {
      copy.textContent = `最近扫描失败：${status.last_error}`;
    } else if (missingFolders.length) {
      copy.textContent = `找不到目录：${missingFolders.map((item) => item.path).join("、")}。重新挂载设备后会自动继续扫描。`;
    } else if (scanState === "scanning") {
      copy.textContent = "正在扫描监控目录并检查音频文件稳定状态……";
    } else if (scanState === "job_running") {
      copy.textContent = `自动导入任务 ${status.last_job_id || ""} 正在处理；完成后继续扫描。`;
    } else if (status.last_scan_at) {
      const scannedLabel = shanghaiDateTimeLabel(
        status.last_scan_at,
        String(status.last_scan_at),
      );
      copy.textContent = `最近扫描 ${scannedLabel}。新文件需稳定 ${formatCount(status.stability_seconds || 20)} 秒后才会处理。`;
    } else {
      copy.textContent = enabled ? "等待第一次后台扫描。" : "保存目录并开启后，Life Logger 会在后台自动发现新录音。";
    }
  }
}

const H8_RECORD_PATH = "/Volumes/H8精英版/RECORD";

async function refreshAutoIngest() {
  setAutoIngestLoading(true, "正在读取自动导入状态……");
  const features = new Set(Array.isArray(capabilities?.api_features) ? capabilities.api_features : []);
  if (!features.has("auto_ingest_v1")) {
    renderAutoIngest({enabled: false, folders: []});
    setAutoIngestLoading(false, "当前后台还没有自动导入接口。重启本地服务后自动启用。");
    return;
  }
  try {
    const status = await api("/api/auto-ingest", {signal: viewAbortSignal("import")}, {silent: true, operation: "读取自动导入状态"});
    renderAutoIngest(status);
    setAutoIngestLoading(false);
  } catch (error) {
    if (error.aborted) {
      // 取消（切走 import 视图）时归位：自动导入的 4 个按钮不能卡在禁用态。
      setAutoIngestLoading(false);
      return;
    }
    const badge = $("auto-ingest-badge");
    if (badge) {
      badge.textContent = "读取失败";
      badge.className = "setting-badge warning";
    }
    setAutoIngestLoading(false, `自动导入状态读取失败：${error.message}`);
  }
}

function autoIngestFolderValues() {
  return String($("auto-ingest-folders")?.value || "")
    .split(/\r?\n/)
    .map((value) => value.trim())
    .filter(Boolean);
}

async function chooseAutoIngestFolder() {
  const button = $("choose-auto-ingest-folder");
  const copy = $("auto-ingest-status");
  if (button) button.disabled = true;
  if (copy) copy.textContent = "正在打开 Finder，请选择要长期监控的录音目录……";
  try {
    const result = await api("/api/auto-ingest/choose-folder", {method: "POST"});
    if (result.cancelled || !result.path) {
      if (copy) copy.textContent = "已取消选择，当前设置没有变化。";
      return;
    }
    const values = autoIngestFolderValues();
    if (!values.includes(result.path)) values.push(result.path);
    if ($("auto-ingest-folders")) $("auto-ingest-folders").value = values.join("\n");
    if (result.status) renderAutoIngest(result.status);
    if (copy) copy.textContent = `已选择并保存监控目录：${result.path}。需要自动处理时打开“启用自动导入”并保存。`;
  } catch (error) {
    if (copy) copy.textContent = `选择目录失败：${error.message}`;
  } finally {
    if (button) button.disabled = false;
  }
}

async function enableAutoIngest() {
  const button = $("enable-auto-ingest");
  const copy = $("auto-ingest-status");
  const folders = autoIngestFolderValues();
  if (!folders.length) {
    folders.push(H8_RECORD_PATH);
    if ($("auto-ingest-folders")) $("auto-ingest-folders").value = folders.join("\n");
  }
  if ($("auto-ingest-enabled")) $("auto-ingest-enabled").checked = true;
  if (button) button.disabled = true;
  if (copy) copy.textContent = "正在启用并保存自动导入……";
  try {
    const status = await api("/api/auto-ingest", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        enabled: true,
        folders,
        stability_seconds: Number($("auto-ingest-stability")?.value || 20),
        scan_interval_seconds: Number($("auto-ingest-interval")?.value || 15),
      }),
    });
    renderAutoIngest(status);
    const missing = Array.isArray(status.folder_errors) ? status.folder_errors : [];
    if (copy) {
      copy.textContent = missing.length
        ? `已启用并持久化；当前找不到 ${missing.map((item) => item.path).join("、")}。连接设备后会按同一路径自动恢复扫描。`
        : "已启用并持久化后台自动导入；新文件稳定后会自动建任务。";
    }
  } catch (error) {
    if (copy) copy.textContent = `启用失败：${error.message}`;
    if (button) button.disabled = false;
  }
}

async function saveAutoIngest() {
  const button = $("save-auto-ingest");
  const copy = $("auto-ingest-status");
  if (button) button.disabled = true;
  if (copy) copy.textContent = "正在保存自动导入设置……";
  try {
    const status = await api("/api/auto-ingest", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        enabled: Boolean($("auto-ingest-enabled")?.checked),
        folders: autoIngestFolderValues(),
        stability_seconds: Number($("auto-ingest-stability")?.value || 20),
        scan_interval_seconds: Number($("auto-ingest-interval")?.value || 15),
      }),
    });
    renderAutoIngest(status);
    if (copy) copy.textContent = status.enabled ? "已保存并启用后台自动导入。" : "已保存；自动导入当前暂停。";
  } catch (error) {
    if (copy) copy.textContent = `保存失败：${error.message}`;
  } finally {
    if (button) button.disabled = false;
  }
}

async function scanAutoIngestNow() {
  const button = $("scan-auto-ingest");
  const copy = $("auto-ingest-status");
  if (button) button.disabled = true;
  if (copy) copy.textContent = "正在扫描监控目录……";
  try {
    const status = await api("/api/auto-ingest/scan", {method: "POST"});
    renderAutoIngest(status);
    if (copy) copy.textContent = status.last_import_count
      ? `本次已导入 ${formatCount(status.last_import_count)} 个新文件，任务 ${status.last_job_id || ""} 已启动。`
      : status.last_error
        ? `扫描完成但发生错误：${status.last_error}`
        : `扫描完成；状态：${scanLabels[String(status.scan_state || "idle")] || status.scan_state || "空闲"}；当前有 ${formatCount(status.pending_count || 0)} 个文件等待稳定。`;
    await refreshAll(true);
  } catch (error) {
    if (copy) copy.textContent = `扫描失败：${error.message}`;
  } finally {
    if (button) button.disabled = false;
  }
}

function normalizedProcessingOptions(value = {}) {
  return {
    asr_model: String(value.asr_model || "mlx-whisper-turbo"),
    speaker_model: String(value.speaker_model || "cam++"),
  };
}

function processingOptionsMatch(job, requested) {
  const existing = normalizedProcessingOptions(job?.processing_options || {});
  const wanted = normalizedProcessingOptions(requested || {});
  return existing.asr_model === wanted.asr_model
    && existing.speaker_model === wanted.speaker_model;
}

function reusableJobForSelection(files, processingOptions) {
  if (files.length !== 1 || ($("start-at").value || "")) return null;
  const selectedFile = files[0];
  const priority = {running: 5, queued: 4, completed: 3, interrupted: 2, failed: 1};
  return lastJobs
    .filter((job) => priority[job.status])
    .filter((job) => processingOptionsMatch(job, processingOptions))
    .filter((job) => {
      const existingFiles = job.files || [];
      if (existingFiles.length !== 1) return false;
      const existing = existingFiles[0];
      return existing.original_name === selectedFile.name
        && Number(existing.size_bytes || 0) === Number(selectedFile.size || 0);
    })
    .sort((left, right) => {
      const rank = priority[right.status] - priority[left.status];
      if (rank) return rank;
      return String(right.updated_at || "").localeCompare(String(left.updated_at || ""));
    })[0] || null;
}
async function createAndStart() {
  const files = [...selected.values()];
  if (!files.length) return;
  const processingOptions = currentProcessingOptions();
  const reusable = reusableJobForSelection(files, processingOptions);
  if (reusable) {
    setStartButtonBusy(true);
    currentJobId = reusable.job_id;
    setImportLiveStatus("checking", "检查已有任务");
    writeStorageVersioned("localStorage", "life-logger-current-job", currentJobId);
    try {
      if (["failed", "interrupted"].includes(reusable.status)) {
        $("upload-status").textContent = "发现相同的中断任务，正在从原任务断点继续……";
        setImportLiveStatus("checking", "正在恢复", reusable.job_id);
        await api(`/api/jobs/${encodeURIComponent(currentJobId)}/restart`, {method: "POST"});
      } else if (reusable.status === "completed") {
        $("upload-status").textContent = "这份录音已经处理完成，已打开已有结果，不再重复上传。";
      } else {
        $("upload-status").textContent = "这份录音已有任务正在处理，已切回原任务。";
      }
      selected.clear();
      renderSelected({preserveUploadStatus: true});
      await refreshAll();
      setImportLiveStatus("ready", reusable.status === "completed" ? "已打开已有结果" : ["failed", "interrupted"].includes(reusable.status) ? "已恢复任务" : "已有任务处理中", reusable.job_id);
      if (reusable.status !== "completed") beginPolling();
      return;
    } catch (error) {
      $("upload-status").textContent = `恢复原任务失败：${error.message}`;
      setImportLiveStatus("error", "恢复失败", error.message);
      return;
    } finally {
      setStartButtonBusy(false);
    }
  }
  setStartButtonBusy(true);
  $("upload-status").textContent = "正在创建任务……";
  setImportLiveStatus("checking", "正在创建任务");
  try {
    const job = await api("/api/jobs", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        start_at: $("start-at").value || null,
        timezone: "Asia/Shanghai",
        processing_options: processingOptions,
      }),
    });
    currentJobId = job.job_id;
    writeStorageVersioned("localStorage", "life-logger-current-job", currentJobId);
    let exactReuse = null;
    for (let index = 0; index < files.length; index += 1) {
      const file = files[index];
      $("upload-status").textContent = `正在上传 ${index + 1}/${files.length}：${file.name}`;
      setImportLiveStatus("checking", `上传 ${index + 1}/${files.length}`, file.name);
      const uploaded = await api(`/api/jobs/${encodeURIComponent(currentJobId)}/files?name=${encodeURIComponent(file.name)}`, {
        method: "PUT",
        headers: {
          "Content-Type": file.type || "application/octet-stream",
          // HTTP header values must be ISO-8859-1 compatible. Encode Chinese and
          // other Unicode path characters; the server already URL-decodes this header.
          "X-Relative-Path": encodeURIComponent(file.webkitRelativePath || file.name),
          "X-File-Mtime": String(file.lastModified / 1000),
          "X-Reuse-Existing": files.length === 1 ? "1" : "0",
        },
        body: file,
        timeoutMs: 300000,
      });
      if (uploaded.reused_existing_job && uploaded.reusable_job_id) {
        exactReuse = uploaded;
        break;
      }
    }
    if (exactReuse) {
      currentJobId = exactReuse.reusable_job_id;
      writeStorageVersioned("localStorage", "life-logger-current-job", currentJobId);
      if (["failed", "interrupted"].includes(exactReuse.reusable_job_status)) {
        $("upload-status").textContent = "SHA256 确认是同一份录音，正在从原任务断点继续……";
        setImportLiveStatus("checking", "正在恢复", currentJobId);
        await api(`/api/jobs/${encodeURIComponent(currentJobId)}/restart`, {method: "POST"});
      } else if (exactReuse.reusable_job_status === "completed") {
        $("upload-status").textContent = "SHA256 确认这份录音已处理完成，已打开已有结果。";
      } else {
        $("upload-status").textContent = "SHA256 确认已有相同任务，已切回原任务。";
      }
      selected.clear();
      renderSelected({preserveUploadStatus: true});
      await refreshAll();
      setImportLiveStatus("ready", exactReuse.reusable_job_status === "completed" ? "已打开已有结果" : ["failed", "interrupted"].includes(exactReuse.reusable_job_status) ? "已恢复任务" : "已有任务处理中", currentJobId);
      if (exactReuse.reusable_job_status !== "completed") beginPolling();
      return;
    }
    await api(`/api/jobs/${encodeURIComponent(currentJobId)}/start`, {method: "POST"});
    $("upload-status").textContent = "任务已启动，页面会自动刷新状态。";
    selected.clear();
    renderSelected({preserveUploadStatus: true});
    await refreshAll();
    setImportLiveStatus("ready", "任务已启动", currentJobId);
    beginPolling();
  } catch (error) {
    $("upload-status").textContent = `启动失败：${error.message}`;
    setImportLiveStatus("error", "启动失败", error.message);
  } finally {
    setStartButtonBusy(false);
  }
}

function resultLink(jobId, path) {
  return `/api/jobs/${encodeURIComponent(jobId)}/artifact?path=${encodeURIComponent(path)}`;
}

function viewerLink(jobId, path) {
  return `/viewer.html?job=${encodeURIComponent(jobId)}&path=${encodeURIComponent(path)}`;
}

// Viewer requires both job and path. When a Watch job has no transcript yet,
// route to the existing job-detail panel instead of a viewer URL that errors.
function jobDetailLink(jobId) {
  return `/?job=${encodeURIComponent(jobId)}#journal-card`;
}

let inlineViewerUrl = "";
let inlineViewerScrollY = 0;
let inlineViewerDirty = false;

function inlineViewerTitle(url) {
  const path = new URL(url, location.href).searchParams.get("path") || "";
  const name = path.split("/").pop() || "结果与复核";
  return ({
    "unknown-review.json": "人工复核",
    "daily_summary.json": "当日总结与时间轴",
    "events.json": "生活事件明细",
    "source-attribution.json": "声源归属结果",
    "self-transcript.json": "我的发言",
    "non-self-transcript.json": "非本人 / 媒体语音",
    "transcript.json": "完整转写",
  })[name] || name;
}

function openInlineViewer(href, trigger = null) {
  const workspace = $("inline-viewer-workspace");
  const frame = $("inline-viewer-frame");
  if (!workspace || !frame) {
    location.href = href;
    return;
  }
  rememberModalTrigger(workspace, trigger);
  const target = new URL(href, location.href);
  target.searchParams.set("embedded", "1");
  inlineViewerUrl = target.href;
  inlineViewerScrollY = window.scrollY;
  inlineViewerDirty = false;
  $("inline-viewer-label").textContent = inlineViewerTitle(target.href);
  frame.src = `${target.pathname}${target.search}`;
  workspace.classList.remove("hidden");
  document.body.classList.add("inline-viewer-open");
  $("inline-viewer-close")?.blur();
  frame.focus();
  frame.onload = () => {
    try {
      frame.contentWindow?.focus();
    } catch (_) {}
  };
}

function closeInlineViewer() {
  const workspace = $("inline-viewer-workspace");
  const frame = $("inline-viewer-frame");
  if (!workspace || workspace.classList.contains("hidden")) return;
  workspace.classList.add("hidden");
  document.body.classList.remove("inline-viewer-open");
  syncBackgroundInert();
  if (frame) frame.src = "about:blank";
  window.scrollTo({top: inlineViewerScrollY, behavior: "instant"});
  if (inlineViewerDirty) {
    inlineViewerDirty = false;
    journalReviewCache.clear();
    // 列表会随刷新重渲染，等渲染完再还焦点，避免指到已移除的节点。
    refreshAll(true).catch(() => {}).finally(() => restoreModalTrigger(workspace));
  } else {
    restoreModalTrigger(workspace);
  }
}

function openInlineViewerInTab() {
  if (!inlineViewerUrl) return;
  const target = new URL(inlineViewerUrl);
  target.searchParams.delete("embedded");
  window.open(`${target.pathname}${target.search}`, "_blank", "noopener");
}

function handleInlineViewerLink(event) {
  if (event.defaultPrevented || event.button !== 0) return;
  if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
  const anchor = event.target.closest?.("a[href]");
  if (!anchor) return;
  const target = new URL(anchor.href, location.href);
  if (target.origin !== location.origin || target.pathname !== "/viewer.html") return;
  event.preventDefault();
  openInlineViewer(target.href, anchor);
}

function statusLabel(status) {
  return STATUS_LABELS[status] || status || "未知";
}

function stageLabel(stage) {
  return STAGE_LABELS[stage] || stage || "等待";
}

function shanghaiDate(offsetDays = 0) {
  const date = new Date(Date.now() + offsetDays * 86400000);
  return new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(date);
}

function friendlyDate(date) {
  if (!date) return "未知日期";
  const today = shanghaiDate(0);
  const yesterday = shanghaiDate(-1);
  if (date === today) return `今天 · ${formatDayKey(date)}`;
  if (date === yesterday) return `昨天 · ${formatDayKey(date)}`;
  return formatDayKey(date);
}

function pathForResultSibling(path, name) {
  return path.replace(/\/result\/\d{4}-\d{2}-\d{2}\/daily_summary\.json$/, `/result/${name}`);
}

async function loadOptionalJson(jobId, path, availablePaths) {
  // 【2026-10-15 恢复】2026-10-09 的修复，在今早 08:10 的 UI 合并里被覆盖掉了。
  // availablePaths（job.results）故意不再当拦截条件：它只能当「省请求」的参考，
  // 不能当唯一事实源 —— 2026-10-09 实测 partial/failed 的 job 里 results 会漏记
  // 磁盘上真实存在的 events.json / unknown-review.json（甚至整份数组为空、只剩
  // 一个裸日期）。所以不在清单里的路径也照样拉一次，取不到（404）就当没有 ——
  // 否则已产出的事件会被静默丢掉。silent 保证探测性 404 不弹成用户可见的错误。
  void availablePaths;
  try {
    return await api(resultLink(jobId, path), {}, {silent: true, operation: "读取日记产物"});
  } catch (_) {
    return null;
  }
}

const journalArtifactCache = new Map();
const journalReviewCache = new Map();
const JOURNAL_CACHE_LIMIT = 2000;

function trimJournalCache(cache) {
  while (cache.size > JOURNAL_CACHE_LIMIT) {
    cache.delete(cache.keys().next().value);
  }
}

const journalArtifactScanCache = new Map();

function isDailySummaryArtifact(path) {
  return /\/result\/\d{4}-\d{2}-\d{2}\/daily_summary\.json$/.test(String(path || ""));
}

// 【2026-10-24】产物扫描并发上限：启动时 175 个 job 一起打 /artifacts 会占满 6 个连接槽，
// 把 iframe 的 /api/daily 挤到 4s 之后。这里限 4 并发，切日窗口内直接跳过（下次刷新再补）。
const ARTIFACT_SCAN_MAX = 4;
const artifactScanState = {active: 0, waiters: []};
function acquireArtifactScanSlot() {
  if (artifactScanState.active < ARTIFACT_SCAN_MAX) {
    artifactScanState.active += 1;
    return Promise.resolve();
  }
  return new Promise((resolve) => artifactScanState.waiters.push(resolve));
}
function releaseArtifactScanSlot() {
  const next = artifactScanState.waiters.shift();
  if (next) next();
  else artifactScanState.active = Math.max(0, artifactScanState.active - 1);
}

async function scanJobArtifactPaths(job) {
  // 「有产物就加载」的兜底判据：按磁盘实况确认这个 job 到底产出了什么。
  // 2026-10-09 实测 partial/failed 的 job 里，status.json 的 results 会整片漏记
  // （空数组、或只记一个裸日期），而磁盘上 daily_summary.json + events.json 都在
  // —— 只信 results 就会把这些已产出的事件判成「尚未形成正式事件」。
  // 扫不到产物就返回空数组：确实无产物的 job 照旧跳过。
  const key = `${job.job_id}:${job.status}:${(job.results || []).join("|")}`;
  if (journalArtifactScanCache.has(key)) return journalArtifactScanCache.get(key);
  if (journalSwitchActive()) return Promise.resolve([]);
  const pending = (async () => {
    await acquireArtifactScanSlot();
    try {
      const payload = await api(
        `/api/jobs/${encodeURIComponent(job.job_id)}/artifacts`,
        {},
        {silent: true, operation: "扫描任务产物目录"},
      );
      return Array.isArray(payload.artifacts)
        ? payload.artifacts.filter((item) => typeof item === "string" && item)
        : [];
    } catch (_) {
      // 请求失败 ≠ 没有产物：标记失败并移出缓存，下一次重进任务允许重试。
      journalArtifactScanCache.delete(key);
      const failed = [];
      failed.__failed = true;
      return failed;
    } finally {
      releaseArtifactScanSlot();
    }
  })();
  journalArtifactScanCache.set(key, pending);
  trimJournalCache(journalArtifactScanCache);
  return pending;
}


function journalArtifactCacheKey(jobId, path) {
  return `${jobId}:${path}`;
}

async function loadJournalArtifact(jobId, path, availablePaths) {
  const key = journalArtifactCacheKey(jobId, path);
  if (journalArtifactCache.has(key)) return journalArtifactCache.get(key);
  const pending = loadOptionalJson(jobId, path, availablePaths);
  journalArtifactCache.set(key, pending);
  try {
    return await pending;
  } catch (error) {
    journalArtifactCache.delete(key);
    throw error;
  }
}

async function loadManualEvents(jobId, path) {
  if (!path) return [];
  const key = `${jobId}:${path}:manual`;
  if (journalReviewCache.has(key)) return journalReviewCache.get(key);
  const pending = (async () => {
    try {
      const payload = await api(`/api/jobs/${encodeURIComponent(jobId)}/manual-events?path=${encodeURIComponent(path)}`);
      return Array.isArray(payload.events) ? payload.events : [];
    } catch (_) {
      return [];
    }
  })();
  journalReviewCache.set(key, pending);
  trimJournalCache(journalReviewCache);
  return pending;
}

async function loadEventCorrections(jobId, path) {
  if (!path) return [];
  const key = `${jobId}:${path}:corrections`;
  if (journalReviewCache.has(key)) return journalReviewCache.get(key);
  const pending = (async () => {
    try {
      const payload = await api(
        `/api/jobs/${encodeURIComponent(jobId)}/event-corrections?path=${encodeURIComponent(path)}`,
        {},
        {silent: true, operation: "读取可选事件修正"},
      );
      return Array.isArray(payload.corrections) ? payload.corrections : [];
    } catch (error) {
      // 这里是 silent 请求，不会走 reportError，失败必须自己补可见提示。
      showSyncToast(`⚠️ 事件修正读取失败：${error.message}`, true);
      return [];
    }
  })();
  journalReviewCache.set(key, pending);
  trimJournalCache(journalReviewCache);
  return pending;
}

function applyEventCorrections(events, corrections, {includeRejected = false} = {}) {
  const byId = new Map((corrections || []).map((item) => [item.event_id, item]));
  const result = [];
  for (const source of events || []) {
    const event = {...source};
    const correction = byId.get(event.event_id);
    if (correction) {
      const changes = correction.changes || {};
      if (Object.prototype.hasOwnProperty.call(changes, "title")) event.title = changes.title;
      if (Object.prototype.hasOwnProperty.call(changes, "categories")) event.categories = changes.categories;
      if (Object.prototype.hasOwnProperty.call(changes, "activity_type")) event.activity_type = changes.activity_type;
      if (Object.prototype.hasOwnProperty.call(changes, "data")) event.data = changes.data;
      if (changes.time && typeof changes.time === "object") event.time = {...(event.time || {}), ...changes.time};
      if (Object.prototype.hasOwnProperty.call(changes, "note")) {
        event.evidence = {...(event.evidence || {}), correction_note: changes.note};
      }
      event.correction_status = correction.rejected ? "rejected" : "corrected";
      event.review_status = correction.rejected ? "rejected" : "confirmed";
      if (correction.rejected && !includeRejected) continue;
    }
    result.push(event);
  }
  return result;
}

async function loadSavedReviews(jobId, path) {
  if (!path) return [];
  const key = `${jobId}:${path}:reviews`;
  if (journalReviewCache.has(key)) return journalReviewCache.get(key);
  const pending = (async () => {
    try {
      const payload = await api(`/api/jobs/${encodeURIComponent(jobId)}/reviews?path=${encodeURIComponent(path)}`);
      return Array.isArray(payload.reviews) ? payload.reviews : [];
    } catch (_) {
      return [];
    }
  })();
  journalReviewCache.set(key, pending);
  trimJournalCache(journalReviewCache);
  return pending;
}

function fallbackTimelineEvents(events) {
  // 【2026-10-15 恢复】今早 08:10 的 UI 合并把 "todo" 从这个具体性集合里丢了。
  // 少了它，带 todo 的日记事件会被当成「非具体事件」而在时间轴/工作台里显示不出来。
  const concrete = new Set(["routine", "expense", "food", "exercise", "todo"]);
  const workMarkers = ["开会", "会议", "汇报", "修复", "断网", "切换工作", "提交", "完成", "测试", "开发", "修改", "处理", "上线", "部署", "面试", "电话沟通", "和同事沟通", "跟同事沟通"];
  const selected = (events || []).filter((event) => {
    if (event?.review_status === "rejected") return false;
    if (event?.pinned_memo || event?.evidence?.pinned_memo || event?.query_dispatch || event?.evidence?.query_dispatch) return true;
    if (!isMeaningfulWorkspaceEvent(event)) return false;
    if (event?.source_type === "manual" || event?.review_status === "confirmed") return true;
    const categories = new Set(eventCategories(event));
    if ([...concrete].some((category) => categories.has(category))) return true;
    if (!categories.has("work_communication")) return false;
    if (event?.capture_mode === "active") return true;
    const quote = String(event?.evidence?.quote || "");
    const text = `${event?.title || ""} ${quote}`;
    return workMarkers.some((marker) => text.includes(marker));
  }).sort((a, b) => journalEventTimestamp(a).localeCompare(journalEventTimestamp(b)));

  const result = [];
  for (const event of selected) {
    const seconds = eventSeconds(event);
    const duplicate = [...result].reverse().find((previous) => {
      const previousSeconds = eventSeconds(previous);
      if (seconds !== null && previousSeconds !== null) {
        const delta = seconds - previousSeconds;
        if (delta < 0 || delta > workspaceDuplicateWindowSeconds(previous, event)) return false;
      }
      return areSemanticEventDuplicates(previous, event);
    });
    if (duplicate) continue;
    result.push(event);
  }
  return result;
}

function buildReviewRows(kind, items, decisions, {jobId, path, fallbackDate, sourceFile}) {
  return (items || []).filter((item) => kind !== "unknown" || item?.review_required !== false).map((item) => {
    const decision = decisions.get(item.segment_id);
    const completed = kind === "unknown"
      ? Boolean(decision?.source_decision && decision?.transcript_decision)
      : Boolean(decision?.source_decision);
    const partial = kind === "unknown"
      ? Boolean(decision?.source_decision) !== Boolean(decision?.transcript_decision)
      : false;
    return {
      key: journalReviewCandidateKey(kind, item), kind, jobId, path, completed, partial,
      date: journalSegmentLocalDate(item, fallbackDate, sourceFile),
    };
  });
}

function withJournalWallClock(event, sourceFile) {
  const startOffset = event?.time?.relative_seconds ?? event?.evidence?.start_offset_seconds;
  const endOffset = event?.evidence?.end_offset_seconds;
  const absolute = journalRelativeAbsoluteTimestamp(sourceFile, startOffset);
  const ended = journalRelativeAbsoluteTimestamp(sourceFile, endOffset);
  if (!absolute && !ended) return event;
  return {
    ...event,
    _journalAbsolute: absolute || event?._journalAbsolute || null,
    _journalEndAbsolute: ended || event?._journalEndAbsolute || null,
  };
}

function journalEventTimestamp(event) {
  // 【2026-10-15 恢复】在今早 08:10 的 UI 合并里丢了两个 fallback：
  // evidence.reported_at（用户报的时间）与 evidence.occurrence_time_evidence（发生时间证据）。
  // 有些事件的时间只存在 evidence 里，少了这两级就会判成「无时间」而显示不出来。
  return String(
    event?._journalAbsolute
    || event?.time?.absolute
    || event?.evidence?.reported_at
    || event?.evidence?.occurrence_time_evidence
    || ""
  );
}

async function loadJournalEntries(jobs) {
  const focusDate = String(selectedJournalDate || shanghaiDate(0));
  // 切日窗口内让出主线程：父页面的产物扫描/大 JSON 解析会阻塞同源 iframe 的首屏事件卡。
  if (journalSwitchActive()) await new Promise((resolve) => setTimeout(resolve, 350));
  const prepared = await Promise.all(jobs.map(async (job) => {
    const results = Array.isArray(job.results) ? job.results : [];
    let availablePaths = results;
    let summaryPaths = results.filter(isDailySummaryArtifact);
    // 只对当前查看日扫产物；其他日期先放壳，避免 24 个后台扫描请求拖慢首屏。
    if (!summaryPaths.length && (job.files || []).length && jobHistoryLocalDate(job) === focusDate) {
      const scanned = await scanJobArtifactPaths(job);
      if (scanned.length) {
        availablePaths = Array.from(new Set([...results, ...scanned]));
        summaryPaths = scanned.filter(isDailySummaryArtifact);
      }
    }
    return {job, availablePaths, summaryPaths};
  }));
  const work = [];
  for (const {job, availablePaths, summaryPaths} of prepared) {
    const paths = availablePaths;
    for (const summaryPath of summaryPaths) {
      work.push((async () => {
        const date = artifactDate(summaryPath);
        // 详细产物只加载当前查看日；其余日期只取 summary + events 用于日期条和事件，
        // 避免每次打开都打 8×N 个请求把浏览器 6 个连接槽全部占满。
        const detailed = date === focusDate;
        if (!detailed) {
          return {
            date,
            jobId: job.job_id,
            summaryPath,
            events: [],
            timelineEvents: [],
            summary: {date, timeline: [], display_timeline: []},
            reviewRows: [],
            selfTranscript: [],
            recordingIntervals: [],
            evidenceDates: [date],
            evidenceFallbackDate: date,
            sourceFile: artifactSourceFile(job, summaryPath),
            reviewCount: 0,
            reviewTotalCount: 0,
            reviewCompletedCount: 0,
            reviewPartialCount: 0,
            selfCount: 0,
            _shell: true,
          };
        }
        const sourceFile = artifactSourceFile(job, summaryPath);
        const dailyPath = summaryPath.replace(/daily_summary\.json$/, "daily.md");
        const eventsPath = summaryPath.replace(/daily_summary\.json$/, "events.json");
        const reviewPath = pathForResultSibling(summaryPath, "unknown-review.json");
        const selfPath = pathForResultSibling(summaryPath, "self-transcript.json");
        const recordingPrefix = sourceFile?.file_id
          ? `files/${sourceFile.file_id}/asr/`
          : "";
        const recordingsPath = recordingPrefix
          ? paths.find((path) => path.startsWith(recordingPrefix) && path.endsWith("/recordings.json"))
          : null;
        const [
          summary,
          review,
          selfTranscript,
          events,
          manualEvents,
          eventCorrections,
          savedReviews,
          recordingsManifest,
        ] = await Promise.all([
          loadJournalArtifact(job.job_id, summaryPath, paths),
          detailed ? loadJournalArtifact(job.job_id, reviewPath, paths) : Promise.resolve([]),
          detailed ? loadJournalArtifact(job.job_id, selfPath, paths) : Promise.resolve([]),
          loadJournalArtifact(job.job_id, eventsPath, paths),
          detailed ? loadManualEvents(job.job_id, paths.includes(eventsPath) ? eventsPath : null) : Promise.resolve([]),
          detailed ? loadEventCorrections(job.job_id, paths.includes(eventsPath) ? eventsPath : null) : Promise.resolve(null),
          detailed ? loadSavedReviews(job.job_id, paths.includes(reviewPath) ? reviewPath : null) : Promise.resolve([]),
          detailed && recordingsPath
            ? loadJournalArtifact(job.job_id, recordingsPath, paths)
            : Promise.resolve(null),
        ]);
        if (!summary) return null;
        const evidenceFallbackDate = journalSourceFileLocalDate(sourceFile, date) || date;
        const recordingRows = Array.isArray(recordingsManifest)
          ? recordingsManifest
          : Array.isArray(recordingsManifest?.recordings)
            ? recordingsManifest.recordings
            : [];
        const recordingIntervals = sourceFile?.capture_source === "apple-watch"
          ? []
          : recordingRows.map((recording) => ({
            recording_id: recording.recording_id || null,
            started_at: recording.started_at || sourceFile?.effective_start_at || null,
            ended_at: recording.ended_at || null,
            duration_seconds: recording.duration_seconds ?? sourceFile?.duration_seconds ?? null,
          }));
        const selfTranscriptItems = Array.isArray(selfTranscript) ? selfTranscript : [];
        const correctedEvents = applyEventCorrections(
          Array.isArray(events) ? events : [],
          eventCorrections,
        );
        const curatedEvents = dedupeWorkspaceEvents([
          ...correctedEvents,
          ...manualEvents,
        ]).map((event) => withJournalWallClock(event, sourceFile));
        const reviewItems = (Array.isArray(review) ? review : [])
          .filter((item) => journalReviewCandidateVisible(item));
        const reviewDecisions = new Map(savedReviews.map((item) => [item.segment_id, item]));
        const reviewCompletedCount = reviewItems.filter((item) => {
          const decision = reviewDecisions.get(item.segment_id);
          return Boolean(decision?.source_decision && decision?.transcript_decision);
        }).length;
        const reviewPartialCount = reviewItems.filter((item) => {
          const decision = reviewDecisions.get(item.segment_id);
          return Boolean(decision?.source_decision) !== Boolean(decision?.transcript_decision);
        }).length;
        const reviewRows = [
          ...buildReviewRows("unknown", reviewItems, reviewDecisions, {jobId: job.job_id, path: reviewPath, fallbackDate: evidenceFallbackDate, sourceFile}),
        ];
        return {
          jobId: job.job_id,
          date,
          summary,
          summaryPath,
          dailyPath: paths.includes(dailyPath) ? dailyPath : null,
          eventsPath: paths.includes(eventsPath) ? eventsPath : null,
          reviewPath: paths.includes(reviewPath) ? reviewPath : null,
          reviewCount: Math.max(0, reviewItems.length - reviewCompletedCount),
          reviewTotalCount: reviewItems.length,
          reviewCompletedCount,
          reviewPartialCount,
          reviewRows,
          selfCount: selfTranscriptItems.length,
          selfTranscript: selfTranscriptItems,
          timelineEvents: fallbackTimelineEvents(curatedEvents).map((event) => ({
            ...event,
            _jobId: job.job_id,
            _artifactPath: eventsPath,
          })),
          events: curatedEvents.map((event) => ({
            ...event,
            _jobId: job.job_id,
            _artifactPath: eventsPath,
          })),
          recordingIntervals,
          sourceFile,
          evidenceFallbackDate,
          evidenceDates: journalEntryEvidenceDates({
            artifactDate: date,
            events: curatedEvents,
            reviewRows,
            selfTranscript: selfTranscriptItems,
            sourceFile,
          }),
        };
      })());
    }
  }
  const jobEntries = (await Promise.all(work)).filter(Boolean);
  // 先让正式记录/时间轴拿到连接槽，1.2 秒后再补实时会话。
  const livePromise = new Promise((resolve) => setTimeout(resolve, 1200))
    .then(() => loadWatchLiveJournalEntries(focusDate));
  // 实时会话扫描可能很慢（全量 manifest/ASR）；首屏先出正式记录，
  // 最多等 250ms，超时就后台合并，避免白页被一个“补充信息”接口拖住。
  const liveWatchEntries = await Promise.race([
    livePromise,
    new Promise((resolve) => setTimeout(() => resolve(null), 250)),
  ]);
  if (liveWatchEntries === null) {
    livePromise.then((entries) => {
      if (!entries?.length || activeView !== "today" || selectedJournalDate !== focusDate) return;
      // 【2026-10-29 A】重建后必须先把旧 day 的瞬时态（canonical/eventCount 等）搬过来，
      // 否则日角标会从真实计数退回“未加载 · 选择查看”。逻辑仍然只有 preserve 一处实现。
      const previousDays = journalDays;
      const nextDays = groupJournalDays(
        [...jobEntries, ...entries],
        journalWatchCoverageIntervals,
        journalWatchCoverageAvailable,
      );
      preserveJournalDayTransientState(previousDays, nextDays);
      journalDays = nextDays;
      if (typeof hydrateJournalDaysFromCache === "function") hydrateJournalDaysFromCache();
      renderJournalNavigation();
    }).catch(() => {});
    return jobEntries;
  }
  return [...jobEntries, ...liveWatchEntries];
}

async function loadWatchLiveJournalEntries(date = selectedJournalDate || shanghaiDate(0)) {
  try {
    const res = await api(`/api/watch-live-events?date=${encodeURIComponent(date)}`, {}, {silent: true, operation: "读取 Watch 当日实时会话事件"});
    if (!res || !Array.isArray(res.sessions)) return [];
    return res.sessions.map((sess) => {
      const date = sess.session_date;
      const events = Array.isArray(sess.events) ? sess.events : [];
      const selfTranscript = Array.isArray(sess.self_transcripts) ? sess.self_transcripts : [];
      const evDates = Array.isArray(sess.event_dates) && sess.event_dates.length ? sess.event_dates : [date];
      return {
        jobId: `watch_${sess.session_id}`,
        date: date,
        summary: {
          title: `Apple Watch 录音会话 (${sess.state === "receiving" ? "正在录音" : "已收口"})`,
          date: date,
          timeline: events,
        },
        events: events.map((e) => ({
          ...e,
          _jobId: `watch_${sess.session_id}`,
          _isLiveWatch: true,
        })),
        timelineEvents: events.map((e) => ({
          ...e,
          _jobId: `watch_${sess.session_id}`,
          _isLiveWatch: true,
        })),
        selfTranscript: selfTranscript,
        selfCount: selfTranscript.length,
        recordingIntervals: [],
        sourceFile: {
          capture_source: "apple-watch",
          original_name: `Apple Watch (${sess.session_id.slice(0, 8)})`,
        },
        evidenceFallbackDate: date,
        evidenceDates: evDates,
      };
    });
  } catch (_) {
    return [];
  }
}

async function loadWatchCoverageIntervals() {
  try {
    const payload = await sharedTimelineRead("/api/watch-coverage", () => api(
      "/api/watch-coverage",
      {},
      {silent: true, operation: "读取 Watch 录音覆盖区间"},
    ), {maxAge: 60000});
    return {
      available: true,
      intervals: Array.isArray(payload.intervals) ? payload.intervals : [],
    };
  } catch (_) {
    return {available: false, intervals: []};
  }
}

function groupJournalDays(entries, watchCoverageIntervals = [], watchCoverageAvailable = true) {
  // Cache parsing only within this immutable projection. A later refresh gets
  // a new cache, so updated intervals are never hidden by persistent memoization.
  const recordingRanges = new WeakMap();
  const normDate = (d) => (String(d || "").trim().match(/^\d{4}-\d{2}-\d{2}/) || [""])[0];
  const grouped = new Map();
  for (const entry of entries) {
    const rawDates = new Set(entry.evidenceDates || journalEntryEvidenceDates(entry));
    for (const date of JOURNAL_LOGIC.recordingIntervalDates(entry.recordingIntervals || [], recordingRanges)) {
      rawDates.add(date);
    }
    for (const rawDate of rawDates) {
      const date = normDate(rawDate);
      if (!date) continue;
      if (!grouped.has(date)) grouped.set(date, []);
      if (!grouped.get(date).includes(entry)) grouped.get(date).push(entry);
    }
  }
  for (const rawDate of JOURNAL_LOGIC.recordingIntervalDates(watchCoverageIntervals, recordingRanges)) {
    const date = normDate(rawDate);
    if (date && !grouped.has(date)) grouped.set(date, []);
  }
  return [...grouped.entries()]
    .map(([date, dayEntries]) => {
      const categories = {};
      const timeline = [];
      const workspaceEvents = [];
      let structuredEventCount = 0;
      let expense = 0;
      let reviewCount = 0;
      let reviewTotalCount = 0;
      let reviewCompletedCount = 0;
      let reviewPartialCount = 0;
      let selfCount = 0;
      for (const entry of dayEntries) {
        const ownershipFallback = entry.evidenceFallbackDate || entry.evidenceDates?.[0] || entry.date;
        const sourceEvents = entry.events?.length ? entry.events : (entry.summary.timeline || []);
        const entryEvents = sourceEvents.filter((event) => normDate(journalEventLocalDate(event, ownershipFallback, entry.sourceFile)) === date);
        const timelineSource = entry.timelineEvents?.length ? entry.timelineEvents : fallbackTimelineEvents(sourceEvents);
        const entryTimelineEvents = timelineSource.filter((event) => normDate(journalEventLocalDate(event, ownershipFallback, entry.sourceFile)) === date);
        structuredEventCount += entryEvents.length;
        reviewCount += entry.reviewCount;
        reviewTotalCount += entry.reviewTotalCount || 0;
        reviewCompletedCount += entry.reviewCompletedCount || 0;
        reviewPartialCount += entry.reviewPartialCount || 0;
        if (entry.selfTranscript?.length) {
          selfCount += entry.selfTranscript.filter((segment) => (
            normDate(journalSegmentLocalDate(segment, ownershipFallback, entry.sourceFile)) === date
          )).length;
        } else if ((entry.evidenceDates || []).length === 1 && normDate(entry.evidenceDates[0]) === date) {
          selfCount += entry.selfCount;
        }
        for (const event of entryEvents) {
          for (const key of eventCategories(event)) categories[key] = (categories[key] || 0) + 1;
          workspaceEvents.push(event);
        }
        timeline.push(...entryTimelineEvents);
      }
      timeline.sort((a, b) => journalEventTimestamp(a).localeCompare(journalEventTimestamp(b)));
      const displayTimeline = fallbackTimelineEvents(timeline);
      const cleanedWorkspaceEvents = dedupeWorkspaceEvents(workspaceEvents);
      const reviewRows = dayEntries.flatMap((entry) => entry.reviewRows || []).filter((row) => normDate(row.date) === date);
      const collapsedReviewRows = collapseJournalReviewRows(reviewRows);
      const pendingReviewRows = collapsedReviewRows.pending;
      const completedReviewRows = collapsedReviewRows.completed;
      const reviewTargets = groupJournalReviewTargets(pendingReviewRows);
      const recordingIntervals = JOURNAL_LOGIC.recordingCoverageForDate([
        ...dayEntries.flatMap((entry) => entry.recordingIntervals || []),
        ...watchCoverageIntervals,
      ], date, recordingRanges);
      reviewCount = pendingReviewRows.filter((row) => row.kind === "unknown").length;
      reviewCompletedCount = completedReviewRows.filter((row) => row.kind === "unknown").length;
      reviewPartialCount = pendingReviewRows.filter((row) => row.partial).length;
      // 【2026-10-19 A0 派单】这是**产物自洽诊断值**，不是展示金额：
      // 它对本 job 原始事件做求和（可能含重复/未去重/未走账本策略），
      // 仅供"产物里有没有消费记录"这类排查使用。页面展示一律用 `expense`（来自 /api/daily）。
      // 名字里带 Diagnostic 就是为了防止有人顺手拿它当金额渲染。
      const derivedExpenseDiagnostic = eventsForCategories(cleanedWorkspaceEvents, ["expense"])
        .reduce((sum, event) => sum + (eventExpenseAmount(event) || 0), 0);
      return {
        date,
        entries: dayEntries,
        _shell: dayEntries.length > 0 && dayEntries.every((entry) => entry._shell),
        // Counts are only published after /api/daily (the canonical event_view +
        // ledgers) hydrates this day. Raw artifact-derived values remain named
        // diagnostics and are never shown as accounting truth.
        canonical: false,
        eventCount: null,
        structuredEventCount: null,
        expense: null,
        derivedEventCount: displayTimeline.length,
        derivedStructuredEventCount: structuredEventCount,
        derivedExpenseDiagnostic,
        derivedCategories: categories,
        reviewCount,
        reviewTotalCount,
        reviewCompletedCount,
        reviewPartialCount,
        selfCount,
        pendingReviewCount: pendingReviewRows.length,
        reviewTargets,
        categories: {},
        timeline: displayTimeline,
        recordingIntervals,
        watchCoverageAvailable,
        workspaceEvents,
      };
    })
    .sort((a, b) => b.date.localeCompare(a.date));
}

function medianNumber(values) {
  const numbers = (values || []).filter((value) => Number.isFinite(value)).sort((a, b) => a - b);
  if (!numbers.length) return null;
  const middle = Math.floor(numbers.length / 2);
  return numbers.length % 2 ? numbers[middle] : (numbers[middle - 1] + numbers[middle]) / 2;
}

function minuteLabel(value) {
  if (!Number.isFinite(value)) return "样本不足";
  const rounded = Math.max(0, Math.min(1439, Math.round(value)));
  const hours = Math.floor(rounded / 60);
  const minutes = rounded % 60;
  return formatClockPair(hours, minutes);
}

function multiDayObservationData(days) {
  const observedDays = (days || []).slice(0, 14).filter((day) => day && day.date && day.canonical === true);
  const eventDays = observedDays;
  const wakeMinutes = [];
  const firstMealMinutes = [];
  let exerciseDays = 0;
  // 【2026-10-19 A0 派单】这里的 totalExpense 是「已 hydrate 的每日账本金额之和」：
  // 输入只有 day.expense（来自 /api/daily 的 expense_ledger.total_amount，
  // 未 hydrate 或当天无账本时为 null，按 0 处理）。它是聚合，不是第二套算法；
  // 但"未知按 0"意味着有缺口的日均值会偏低，所以下面同时暴露 missingDays 供 UI 提示。
  let expenseMissingDays = 0;
  let totalEvents = 0;
  for (const day of eventDays) {
    const events = dedupeWorkspaceEvents(day.workspaceEvents?.length ? day.workspaceEvents : day.timeline);
    if (day.expense === null || day.expense === undefined) expenseMissingDays += 1;
    totalEvents += Number(day.eventCount || 0);
    if (eventsForCategories(events, ["exercise"]).length) exerciseDays += 1;
    const routines = eventsForCategories(events, ["routine"])
      .filter((event) => event.activity_type === "waking_up" || /(起床|醒来|醒了)/u.test(String(event.title || event.evidence?.quote || "")))
      .map(minuteOfDay)
      .filter((value) => Number.isFinite(value))
      .sort((a, b) => a - b);
    if (routines.length) wakeMinutes.push(routines[0]);
    const meals = eventsForCategories(events, ["food", "diet"])
      .map(minuteOfDay)
      .filter((value) => Number.isFinite(value))
      .sort((a, b) => a - b);
    if (meals.length) firstMealMinutes.push(meals[0]);
  }
  const count = eventDays.length;
  return {
    dayCount: count,
    windowCount: observedDays.length,
    totalEvents,
    averageEvents: count ? totalEvents / count : 0,
    totalExpense: null,
    expenseMissingDays,
    averageExpense: null,
    exerciseDays,
    wakeMedian: medianNumber(wakeMinutes),
    wakeSamples: wakeMinutes.length,
    firstMealMedian: medianNumber(firstMealMinutes),
    firstMealSamples: firstMealMinutes.length,
  };
}

function renderMultiDayObservations(days) {
  const insight = multiDayObservationData(days);
  if (insight.dayCount < 3) {
    return `<section class="multi-day-observations insufficient">
      <div class="multi-day-head"><div><span>LONGITUDINAL MEMORY</span><h4>多日观察</h4></div><small>至少 3 个有记录日后启用</small></div>
      <p>当前只有 ${formatCount(insight.dayCount)} 个有记录日。这里不会用单日数据冒充长期规律。</p>
    </section>`;
  }
  const exerciseRate = Math.round((insight.exerciseDays / insight.dayCount) * 100);
  return `<section class="multi-day-observations">
    <div class="multi-day-head"><div><span>LONGITUDINAL MEMORY</span><h4>多日观察</h4></div><small>最近 ${formatCount(insight.windowCount)} 个记录日 · 仅作观察</small></div>
    <div class="multi-day-grid">
      <article><span>平均事件</span><strong>${formatFixed1(insight.averageEvents)}</strong><small>条 / 有记录日</small></article>
      <article><span>运动覆盖</span><strong>${formatCount(insight.exerciseDays)}/${formatCount(insight.dayCount)}</strong><small>${formatPercent(exerciseRate / 100)} 的记录日出现运动</small></article>
      <article><span>消费(CNY)</span><strong>${insight.totalExpense === null ? "—" : formatMoney(insight.totalExpense)}</strong><small>${insight.averageExpense === null ? "区间账本待接入" : `日均 ${formatMoney(insight.averageExpense)}`}</small></article>
      <article><span>起床中位时间</span><strong>${minuteLabel(insight.wakeMedian)}</strong><small>${formatCount(insight.wakeSamples)} 天有明确起床事件</small></article>
      <article><span>首次饮食中位</span><strong>${minuteLabel(insight.firstMealMedian)}</strong><small>${formatCount(insight.firstMealSamples)} 天有带时间饮食事件</small></article>
    </div>
    <p class="multi-day-note">统计只使用已有、已修正后的事件；没有录音的日期不会被当成“0”，有录音但没有正式事件的记录日会保留为 0。这些是记录覆盖下的观察结果，不是诊断、预测或完整生活事实。</p>
  </section>`;
}

function eventCategories(event) {
  const categories = Array.isArray(event?.categories) ? event.categories.filter(Boolean) : [];
  if (categories.length) {
    return [...new Set(categories.map(canonicalEventCategory))];
  }
  return event?.activity_type ? [event.activity_type] : ["other"];
}

function eventsForCategories(events, categories) {
  const wanted = new Set(categories);
  return (events || []).filter((event) => eventCategories(event).some((category) => wanted.has(category)));
}

function correctedWorkspaceText(value) {
  let text = String(value || "").trim();
  const purchaseContext = /(?:买|花\s*\d|花了|拿(?:一|个|包|那)|一包|包烟|香烟|换口味|烟)/u;
  if (purchaseContext.test(text)) {
    text = text
      .replace(/短(?:剧|致|支)的?乐土/gu, "短支的乐途")
      .replace(/短(?:剧|致)的?乐途/gu, "短支的乐途");
  }
  return text;
}

function workspaceEventText(event) {
  return correctedWorkspaceText(event?.title || event?.evidence?.quote || "");
}

function normalizedWorkspaceEventText(event) {
  return workspaceEventText(event)
    .toLowerCase()
    .replace(/[\s，。！？、；：,.!?;:'"“”‘’（）()【】\[\]{}<>《》—_-]+/g, "");
}

function eventSeconds(event) {
  const absolute = Date.parse(journalEventTimestamp(event));
  if (Number.isFinite(absolute)) return absolute / 1000;
  const relative = Number(event?.time?.relative_seconds);
  return Number.isFinite(relative) ? relative : null;
}

function eventNgrams(value, size = 3) {
  if (!value) return new Set();
  if (value.length <= size) return new Set([value]);
  const grams = new Set();
  for (let index = 0; index <= value.length - size; index += 1) grams.add(value.slice(index, index + size));
  return grams;
}

function workspaceTodoIntentText(event) {
  let text = normalizedWorkspaceEventText(event);
  ["提醒我", "记得", "待办", "明天要", "今天要", "我需要", "还需要", "需要我", "需要"].forEach((marker) => {
    text = text.split(marker).join("");
  });
  return text;
}

function workspaceFoodItems(event) {
  const food = event?.data?.food || {};
  const values = [];
  if (food.item) values.push(String(food.item));
  (food.items || []).forEach((item) => {
    if (item?.name) values.push(String(item.name));
  });
  const text = workspaceEventText(event);
  ["纯牛奶", "牛奶"].forEach((item) => {
    if (text.includes(item)) values.push(item);
  });
  return new Set(values.map((value) => value.replace(/纯牛奶/gu, "牛奶").trim().toLowerCase()).filter(Boolean));
}

function areStructuredWorkspaceDuplicates(left, right) {
  const leftCategories = new Set(eventCategories(left));
  const rightCategories = new Set(eventCategories(right));
  if (leftCategories.has("food") && rightCategories.has("food")) {
    const leftItems = workspaceFoodItems(left);
    const rightItems = workspaceFoodItems(right);
    if ([...leftItems].some((item) => rightItems.has(item))) return true;
  }
  if (leftCategories.has("exercise") && rightCategories.has("exercise")) {
    if (left?.activity_type && right?.activity_type && left.activity_type === right.activity_type) {
      const leftDuration = Number(left?.data?.exercise?.duration_minutes);
      const rightDuration = Number(right?.data?.exercise?.duration_minutes);
      if (!Number.isFinite(leftDuration) || !Number.isFinite(rightDuration) || leftDuration === rightDuration) return true;
    }
  }
  return false;
}

function workspaceDuplicateWindowSeconds(left, right) {
  const leftCategories = new Set(eventCategories(left));
  const rightCategories = new Set(eventCategories(right));
  if (leftCategories.has("todo") && rightCategories.has("todo")) return 900;
  if (
    (leftCategories.has("food") && rightCategories.has("food"))
    || (leftCategories.has("exercise") && rightCategories.has("exercise"))
  ) return 600;
  return 180;
}

function isActionableTodoEvent(event) {
  if (!eventCategories(event).includes("todo")) return false;
  if (event?.source_type === "manual" || event?.review_status === "confirmed") return true;
  if (event?.pinned_memo || event?.evidence?.pinned_memo || event?.query_dispatch || event?.evidence?.query_dispatch) return true;
  if (event?.review_status === "reviewed" && event?.evidence?.decision_source && event?.evidence?.decision_source !== "rule_based") return true;
  const text = workspaceEventText(event);
  if (/^(?:便签|记录|查询|检查|待办)[：:]/u.test(text)) return true;
  if (["提醒我", "记得", "待办", "明天要", "明天去", "去把", "准备去", "记录一下", "记一下", "记录下", "查询一下", "查一下", "检查一下", "我要查", "我想查"].some((marker) => text.includes(marker))) return true;
  if (!text.includes("需要")) return false;
  if (["难道", "谁", "什么", "怎么", "是否", "能不能"].some((marker) => text.includes(marker))) return false;
  if (/[吗呢？?]\s*$/u.test(text)) return false;
  return /(?:我|还|今天|明天|之后)?需要(?:我)?(?:去|把|做|买|处理|联系|提交|完成|记|更新|检查|确认|准备|安排)/u.test(text);
}

function areSemanticEventDuplicates(left, right) {
  const leftCategoryList = eventCategories(left).slice().sort();
  const rightCategoryList = eventCategories(right).slice().sort();
  const leftCategories = new Set(leftCategoryList);
  const rightCategories = new Set(rightCategoryList);
  const sameCategories = leftCategoryList.join("|") === rightCategoryList.join("|");
  const bothTodo = leftCategories.has("todo") && rightCategories.has("todo");
  if (!sameCategories && !bothTodo) return false;
  if (left?.activity_type && right?.activity_type && left.activity_type !== right.activity_type) return false;
  if (areStructuredWorkspaceDuplicates(left, right)) return true;
  let leftText = normalizedWorkspaceEventText(left);
  let rightText = normalizedWorkspaceEventText(right);
  if (bothTodo) {
    leftText = workspaceTodoIntentText(left);
    rightText = workspaceTodoIntentText(right);
  }
  if (!leftText || !rightText) return false;
  if (leftText === rightText) return true;
  const [shorter, longer] = [leftText, rightText].sort((a, b) => a.length - b.length);
  if (shorter.length >= 8 && longer.includes(shorter) && shorter.length / longer.length >= 0.62) return true;
  const leftGrams = eventNgrams(leftText);
  const rightGrams = eventNgrams(rightText);
  const union = new Set([...leftGrams, ...rightGrams]);
  if (!union.size) return false;
  let overlap = 0;
  leftGrams.forEach((gram) => { if (rightGrams.has(gram)) overlap += 1; });
  return overlap / union.size >= 0.64;
}

function isMeaningfulWorkspaceEvent(event) {
  if (event?.source_type === "manual" || event?.review_status === "confirmed") return true;
  if (event?.pinned_memo || event?.evidence?.pinned_memo || event?.query_dispatch || event?.evidence?.query_dispatch) return true;
  const categories = new Set(eventCategories(event));
  if (categories.has("expense") && isRejectedPurchaseChoice(event)) return false;
  if ([...categories].some((category) => ["routine", "expense", "food", "exercise"].includes(category))) return true;
  if (categories.has("todo")) return isActionableTodoEvent(event);
  if (categories.has("idea")) return isUsefulIdeaEvent(event);
  if (categories.has("work_communication") || categories.has("work") || categories.has("communication")) {
    const text = `${event?.title || ""} ${event?.evidence?.quote || ""}`;
    return ["开会", "会议", "沟通", "电话", "汇报", "修复", "提交", "完成", "测试", "开发", "修改", "处理", "上线", "部署", "面试", "评审", "排查"].some((marker) => text.includes(marker));
  }
  return false;
}

function dedupeWorkspaceEvents(events) {
  const result = [];
  const ordered = [...(events || [])]
    .filter(isMeaningfulWorkspaceEvent)
    .sort((a, b) => (eventSeconds(a) ?? 0) - (eventSeconds(b) ?? 0));
  for (const event of ordered) {
    const text = normalizedWorkspaceEventText(event);
    if (!text) continue;
    const seconds = eventSeconds(event);
    const duplicate = [...result].reverse().find((previous) => {
      const previousSeconds = eventSeconds(previous);
      if (seconds !== null && previousSeconds !== null) {
        const delta = seconds - previousSeconds;
        if (delta < 0 || delta > workspaceDuplicateWindowSeconds(previous, event)) return false;
      }
      return areSemanticEventDuplicates(previous, event);
    });
    if (!duplicate) result.push(event);
  }
  return result;
}

function isUsefulIdeaEvent(event) {
  const raw = String(event?.title || event?.evidence?.quote || "").trim();
  const text = raw.replace(/^(?:(?:嗯+|呃+|啊+|这个|那个|然后|就是|我觉得|我想)[，,\s]*)+/u, "").trim();
  const normalized = normalizedWorkspaceEventText({title: text});
  if (normalized.length < 6) return false;
  return !/(应该|然后|就是|觉得|可能|因为|所以|但是|如果|要不|这个|那个|怎么|什么|先)$/u.test(text);
}

function isRejectedPurchaseChoice(event) {
  const text = workspaceEventText(event);
  if (!text.includes("算了")) return false;
  if (!["拿", "换口味", "换一个", "不要"].some((marker) => text.includes(marker))) return false;
  return !["花了", "花费", "买了", "刚买", "已经买", "付款", "支付"].some((marker) => text.includes(marker));
}

function eventExpenseAmount(event) {
  if (isRejectedPurchaseChoice(event)) return null;
  const amount = Number(event?.data?.expense?.amount);
  if (Number.isFinite(amount) && amount > 0) return amount;
  const text = workspaceEventText(event);
  const match = text.match(/(?:花了?|花费|消费|支付(?:了)?|付款(?:了)?)\s*(\d+(?:\.\d+)?)(?!\s*(?:分钟|分|小时|天|公里|千米|米))(?=\s*(?:买|购|拿|入手))/u);
  const fallback = Number(match?.[1]);
  return Number.isFinite(fallback) && fallback > 0 ? fallback : null;
}

function eventLedgerAmount(event) {
  const amount = Number(event?.data?.expense?.amount);
  return Number.isFinite(amount) && amount > 0 ? amount : null;
}

function workspaceEventDisplayText(event) {
  const text = workspaceEventText(event);
  const amount = eventLedgerAmount(event);
  if (amount !== null && text.includes("短支的乐途")) {
    return `花了${formatMoneyTrimmed(amount)}元，买了一包「短支的乐途」`;
  }
  return text;
}

function renderWorkoutPills(exercise) {
  if (!exercise || typeof exercise !== "object") return "";
  const exList = Array.isArray(exercise.exercises) ? exercise.exercises : [];
  if (exList.length === 0 && !exercise.summary) return "";
  let html = `<div class="workout-detail-pills" style="margin-top: 6px; display: flex; flex-direction: column; gap: 4px; font-size: 12px; color: var(--color-ink-muted);">`;
  if (exercise.summary) {
    html += `<div class="workout-summary-badge" style="background: rgba(46, 125, 50, 0.08); color: #2e7d32; padding: 2px 6px; border-radius: 4px; font-weight: 500; display: inline-block;">${escapeHtml(exercise.summary)}</div>`;
  }
  for (const ex of exList) {
    const name = ex.name || "动作";
    const sets = Array.isArray(ex.sets) ? ex.sets.map(s => {
      let t = `${s.weight_kg ? s.weight_kg + 'kg' : ''}`;
      if (s.reps) t += `×${s.reps}`;
      if (s.note) t += ` (${s.note})`;
      return t;
    }).join(" | ") : (ex.note || (ex.weight_kg ? ex.weight_kg + 'kg' : ''));
    html += `<div class="workout-ex-row" style="padding-left: 2px;">• <strong>${escapeHtml(name)}</strong>: ${escapeHtml(sets)}</div>`;
  }
  html += `</div>`;
  return html;
}

function workspaceEventCard(event) {
  const categories = eventCategories(event);
  const primary = categories[0] || "other";
  const clock = eventClock(event) || "--:--";
  const amount = eventLedgerAmount(event);
  const title = workspaceEventDisplayText(event) || "未命名事件";
  return `<article class="workspace-event-card">
    <div class="workspace-event-meta">
      <time>${escapeHtml(clock)}</time>
      <span>${escapeHtml(CATEGORY_LABELS[primary] || primary)}</span>
      ${amount !== null ? `<strong>${formatMoney(amount)}</strong>` : ""}
    </div>
    <p>${escapeHtml(title)}</p>
    ${renderWorkoutPills(event.data?.exercise)}
  </article>`;
}

function eventTaskKey(event) {
  const eventId = String(event?.event_id || "");
  const jobId = String(event?._jobId || "");
  const artifactPath = String(event?._artifactPath || event?.artifact_path || "");
  if (!eventId) return "";
  return [jobId, artifactPath, eventId].join(":");
}

function taskStateForEvent(event) {
  const taskKey = eventTaskKey(event);
  return taskStates.get(taskKey)?.status || "pending";
}

function taskStateRecordForEvent(event) {
  return taskStates.get(eventTaskKey(event)) || null;
}

function taskOccurrenceTime(event) {
  const absolute = Date.parse(journalEventTimestamp(event));
  if (Number.isFinite(absolute)) return absolute;
  const originDate = String(event?._originDate || "");
  const fallback = Date.parse(`${originDate}T00:00:00+08:00`);
  return Number.isFinite(fallback) ? fallback : 0;
}

function openTodosForJournalDay(day) {
  const candidateDays = journalDays
    .filter((candidate) => candidate.date <= day.date)
    .sort((a, b) => a.date.localeCompare(b.date));
  const candidates = [];
  const selectedDayEnd = Date.parse(`${day.date}T23:59:59.999+08:00`);
  const selectedDayStart = Date.parse(`${day.date}T00:00:00+08:00`);
  for (const candidateDay of candidateDays) {
    const source = candidateDay.workspaceEvents?.length ? candidateDay.workspaceEvents : candidateDay.timeline;
    const cleaned = dedupeWorkspaceEvents(source);
    for (const event of eventsForCategories(cleaned, ["todo"])) {
      if (!isActionableTodoEvent(event)) continue;
      candidates.push({...event, _originDate: candidateDay.date});
    }
  }
  const groups = [];
  for (const event of candidates) {
    const group = [...groups].reverse().find((items) =>
      items.some((existing) => areSemanticEventDuplicates(existing, event))
    );
    if (group) group.push(event);
    else groups.push([event]);
  }

  const open = [];
  for (const group of groups) {
    group.sort((a, b) => taskOccurrenceTime(a) - taskOccurrenceTime(b));
    const latest = group.at(-1);
    const latestOccurrence = taskOccurrenceTime(latest);
    const stateEntries = group
      .map((event) => ({event, state: taskStateRecordForEvent(event)}))
      .filter((entry) => {
        if (!entry.state?.updated_at) return false;
        const updatedAt = Date.parse(entry.state.updated_at);
        return Number.isFinite(updatedAt) && (!Number.isFinite(selectedDayEnd) || updatedAt <= selectedDayEnd);
      })
      .sort((a, b) => Date.parse(a.state.updated_at) - Date.parse(b.state.updated_at));
    const latestState = stateEntries.at(-1) || null;
    const stateUpdatedAt = latestState ? Date.parse(latestState.state.updated_at) : NaN;
    if (
      latestState?.state?.status === "completed"
      && Number.isFinite(stateUpdatedAt)
      && stateUpdatedAt >= latestOccurrence
    ) {
      continue;
    }
    let representative = latest;
    if (
      latestState
      && latestState.state.status !== "completed"
      && Number.isFinite(stateUpdatedAt)
      && stateUpdatedAt >= latestOccurrence
    ) {
      representative = latestState.event;
    }
    const representativeStatus = taskStateForEvent(representative);
    if (!journalActionCandidateVisible(representativeStatus, latestOccurrence, selectedDayStart)) continue;
    const originDate = representative._originDate || day.date;
    open.push({
      ...representative,
      _originDate: originDate,
      _carriedFromDate: originDate !== day.date ? originDate : null,
    });
  }
  return open.sort((a, b) => {
    const rank = {in_progress: 0, pending: 1};
    const statusDelta = (rank[taskStateForEvent(a)] ?? 2) - (rank[taskStateForEvent(b)] ?? 2);
    if (statusDelta) return statusDelta;
    return String(a._originDate || "").localeCompare(String(b._originDate || ""));
  });
}


const TASK_STATUS_LABELS = {
  pending: "待处理",
  in_progress: "进行中",
  completed: "已完成",
  dismissed: "已忽略",
};

function taskWorkspaceCard(event) {
  const taskKey = eventTaskKey(event);
  const status = taskStateForEvent(event);
  const clock = eventClock(event) || "--:--";
  const title = workspaceEventText(event) || "未命名待办";
  const carryLabel = event?._carriedFromDate ? `来自 ${event._carriedFromDate.slice(5)}` : "";
  const isCandidate = Boolean(
    event?.is_candidate
    || String(event?.event_id || "").startsWith("evt_cand_")
    || String(taskKey).includes("evt_cand_")
    || String(event?.source_type || "").includes("candidate")
  );
  return `<article class="workspace-event-card task-card ${status === "completed" ? "task-completed" : ""} ${isCandidate ? "task-candidate" : ""}">
    <div class="workspace-event-meta">
      <time>${escapeHtml(clock)}</time>
      <span>${isCandidate ? "待办候选" : (carryLabel ? escapeHtml(carryLabel) : "待办")}</span>
      <strong class="task-state-label">${escapeHtml(TASK_STATUS_LABELS[status] || status)}</strong>
    </div>
    <p>${escapeHtml(title)}</p>
    ${taskKey ? (isCandidate ? `<div class="task-state-actions"><span class="task-candidate-hint" style="font-size:11px;color:var(--text-subtle, #7a756f);padding:3px 0;">候选待办 · 暂不可直接修改状态</span></div>` : `<div class="task-state-actions" data-task-key="${escapeHtml(taskKey)}">
      ${["pending", "in_progress", "completed"].map((value) => `<button type="button" class="${status === value ? "active" : ""}" onclick="setTaskStatus('${encodeURIComponent(taskKey)}','${value}')">${TASK_STATUS_LABELS[value]}</button>`).join("")}
      <button type="button" class="task-dismiss" title="从首页行动候选移除；原始日记与录音证据仍保留" onclick="setTaskStatus('${encodeURIComponent(taskKey)}','dismissed')">从首页移除</button>
    </div>`) : ""}
  </article>`;
}

function workspaceLane({title, eyebrow, events, emptyText, className = ""}) {
  return `<section class="workspace-lane ${className}">
    <div class="workspace-lane-head"><div><span>${escapeHtml(eyebrow)}</span><h5>${escapeHtml(title)}</h5></div><strong>${events.length}</strong></div>
    <div class="workspace-lane-body">
      ${events.length ? events.slice(0, 5).map(workspaceEventCard).join("") : `<div class="workspace-lane-empty">${escapeHtml(emptyText)}</div>`}
    </div>
  </section>`;
}

function taskWorkspaceLane(events) {
  const ordered = [...events].sort((a, b) => {
    const rank = {in_progress: 0, pending: 1};
    return (rank[taskStateForEvent(a)] ?? 2) - (rank[taskStateForEvent(b)] ?? 2);
  });
  const carried = ordered.filter((event) => event._carriedFromDate).length;
  return `<section class="workspace-lane todo-lane">
    <div class="workspace-lane-head"><div><span>NEXT</span><h5>行动候选</h5><small>仅保留最近 7 天可执行事项；“从首页移除”会持久忽略，但不删除日记证据</small></div><strong>${ordered.length}${carried ? ` · 跨日 ${carried}` : ""}</strong></div>
    <div class="workspace-lane-body">
      ${ordered.length ? ordered.slice(0, 8).map(taskWorkspaceCard).join("") : '<div class="workspace-lane-empty">当前没有需要继续跟进的待办。</div>'}
    </div>
  </section>`;
}

async function setTaskStatus(encodedTaskKey, status) {
  const taskKey = decodeURIComponent(encodedTaskKey);
  const actionStatus = $("journal-action-status");
  if (taskKey.includes("evt_cand_")) {
    if (actionStatus) actionStatus.textContent = "候选事项不能直接操作状态。";
    if (typeof showToast === "function") showToast("候选事项不能直接操作状态", "warning");
    return;
  }
  const event = journalDays
    .flatMap((day) => day.workspaceEvents?.length ? day.workspaceEvents : day.timeline)
    .find((item) => eventTaskKey(item) === taskKey);
  if (!event || String(event.event_id || "").startsWith("evt_cand_")) {
    if (actionStatus) actionStatus.textContent = "候选事项不能直接操作状态。";
    if (typeof showToast === "function") showToast("候选事项不能直接操作状态", "warning");
    return;
  }
  // 【2026-10-14 修】watch 实时会话事件用 _jobId = `watch_<session_id>` 当**显示用**标识，
  // 它不是真的 job 编号。此前把它塞进 payload.job_id，后端 job_path() 校验「必须以 job- 开头」
  // → 抛「任务编号无效」400，于是行动候选整排按钮全部点不了。
  // 正确修法：这类事件传 job_id=null（task_key 仍带 watch_ 前缀，台账照样能存），
  // 而不是把按钮拦掉 —— 拦截等于用户永远点不动。
  // ⚠️ 此修复曾在 2026-10-15 合并 UI 优化线时被覆盖，本次从备份恢复。
  const isLiveWatch = Boolean(event._isLiveWatch);
  if (actionStatus) actionStatus.textContent = "正在更新待办……";
  try {
    const payload = await api("/api/tasks/status", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        task_key: taskKey,
        status,
        title: event.title || "",
        event_id: event.event_id || null,
        job_id: isLiveWatch ? null : (event._jobId || null),
        artifact_path: event._artifactPath || event.artifact_path || null,
      }),
    });
    if (payload.task) taskStates.set(taskKey, payload.task);
    renderJournalDay(journalDays.find((day) => day.date === selectedJournalDate));
    const sync = payload.openclaw || null;
    if (actionStatus) {
      actionStatus.textContent = (sync && sync.synced === false)
        ? `已在本地更新，但未同步到 OpenClaw${sync.queued ? "（已排队，恢复后自动补）" : ""}。`
        : "待办状态已更新。";
    }
  } catch (error) {
    if (actionStatus) actionStatus.textContent = `更新待办失败：${error.message}`;
  }
}
window.setTaskStatus = setTaskStatus;

function extractDayPinnedMemos(day) {
  const memos = [];
  const seen = new Set();
  const addItem = (item) => {
    const key = String(item.text || item.quote || "").trim();
    if (!key || seen.has(key)) return;
    seen.add(key);
    memos.push(item);
  };
  if (Array.isArray(day.pinned_memos)) {
    day.pinned_memos.forEach((m) => addItem(m));
  }
  const events = [...(day.workspaceEvents || []), ...(day.timeline || [])];
  const memoRe = /(?:^|[，,。！？!?；;\s])(?:(?:呃|嗯|啊|那|然后|另外|还有|顺便|其[他它]的?)[，,\s]*)*(?:现在|帮我|给我)?(?:记录一下|记一下|记录下|记下来|记个备忘|备忘一下)/u;
  const queryRe = /(?:^|[，,。！？!?；;\s])(?:(?:呃|嗯|啊|那|然后|另外|还有|顺便)[，,\s]*)*(?:帮我|给我|我要|我想)?(?:查询一下|查一下|检查一下|搜一下|搜索一下|查一查|查查)/u;
  events.forEach((event) => {
    const evidence = event.evidence || {};
    const quote = String(evidence.quote || "").trim();
    const title = String(event.title || "").trim();
    const isQuery = Boolean(
      event.query_dispatch
      || evidence.query_dispatch
      || title.startsWith("查询：")
      || queryRe.test(quote)
      || queryRe.test(title)
    );
    const isPinned = Boolean(
      event.pinned_memo
      || evidence.pinned_memo
      || title.startsWith("便签：")
      || title.startsWith("记录：")
      || memoRe.test(quote)
      || memoRe.test(title)
    );
    if (!isPinned && !isQuery) return;
    let cleaned = "";
    if ((title.startsWith("便签：") || title.startsWith("查询：") || title.startsWith("记录：")) && title.length > 4) {
      cleaned = title.replace(/^(?:便签|查询|记录)[：:]\s*/u, "").trim();
    } else {
      const raw = quote || title;
      cleaned = raw.replace(/^(?:(?:呃|嗯|啊|那|然后|另外|还有|顺便)[，,\s]*)*(?:现在|帮我|给我|我要|我想)?(?:记录一下|记一下|记录下|记下来|记个备忘|备忘一下|查询一下|查一下|检查一下|搜一下|搜索一下|查一查|查查)[，,。：:\s]*/u, "").trim() || raw;
    }
    addItem({
      kind: isQuery && !isPinned ? "query" : "memo",
      text: cleaned,
      quote: quote || title,
      clock: eventClock(event) || null,
    });
  });
  return memos;
}

function renderPinnedMemosBlock(memos) {
  if (!memos || !memos.length) return "";
  const items = memos.map((m) => {
    const isQuery = m.kind === "query" || String(m.quote || "").startsWith("查询：");
    const tagLabel = isQuery ? "🔍 查询" : "📌 便签";
    const clockLabel = m.clock ? `${tagLabel} · ${m.clock}` : tagLabel;
    return `
    <div class="pinned-memo-card">
      <div class="pinned-memo-content">
        <span class="pinned-memo-clock">${escapeHtml(clockLabel)}</span>
        <span class="pinned-memo-text">${escapeHtml(m.text || m.quote || "")}</span>
      </div>
      <button type="button" class="pinned-memo-copy-btn" onclick="navigator.clipboard.writeText('${escapeHtml(m.text || m.quote || "").replace(/'/g, "\\\'")}'); this.textContent='已复制'; setTimeout(() => this.textContent='复制', 2000);">复制</button>
    </div>
  `;
  }).join("");
  return `<section class="pinned-memos-section">
    <div class="pinned-memos-head">
      <span>📌 今日置顶便签与语音查询</span>
      <small>口述“记录一下 / 记一下”高优先级备忘与“我要查一下 / 检查一下”查询事项</small>
    </div>
    <div class="pinned-memos-list">${items}</div>
  </section>`;
}

function kanbanDaySourceEvents(day) {
  const source = day?.workspaceEvents?.length ? day.workspaceEvents : (day?.timeline || []);
  return dedupeWorkspaceEvents(source);
}

function kanbanEventSourceLabel(event) {
  if (event?._isLiveWatch || /^watch_/.test(String(event?._jobId || ""))) return "手表录音";
  if (event?.source_type === "manual") return "手动补录";
  if (event?.query_dispatch || event?.evidence?.query_dispatch) return "语音查询";
  if (event?.pinned_memo || event?.evidence?.pinned_memo) return "语音便签";
  if (event?._carriedFromDate) return `来自 ${event._carriedFromDate.slice(5)}`;
  return "录音整理";
}

function isVoiceQueryEvent(event) {
  const evidence = event?.evidence || {};
  if (event?.query_dispatch || evidence.query_dispatch) return true;
  const title = String(event?.title || "").trim();
  const quote = String(evidence.quote || "").trim();
  if (/^查询[：:]/u.test(title) || /^查询[：:]/u.test(quote)) return true;
  const queryRe = /(?:^|[，,。！？!?；;\s])(?:(?:呃|嗯|啊|那|然后|另外|还有|顺便)[，,\s]*)*(?:帮我|给我|我要|我想)?(?:查询一下|查一下|检查一下|搜一下|搜索一下|查一查|查查)/u;
  return queryRe.test(quote) || queryRe.test(title);
}

function kanbanTodoEvents(day) {
  const collected = new Map();
  ensureKanbanActivityTodos(day.date);
  const add = (event) => {
    const taskKey = eventTaskKey(event);
    if (!taskKey || !isActionableTodoEvent(event)) return;
    if (isVoiceQueryEvent(event)) return;
    if (taskStateForEvent(event) === "dismissed") return;
    if (!collected.has(taskKey)) collected.set(taskKey, event);
  };
  openTodosForJournalDay(day).forEach(add);
  (kanbanActivityTodos.get(day.date) || []).forEach(add);
  const source = kanbanDaySourceEvents(day);
  eventsForCategories(source, ["todo"])
    .filter(isActionableTodoEvent)
    .filter((event) => taskStateForEvent(event) === "completed")
    .forEach(add);
  eventsForCategories(source, ["idea"])
    .filter((event) => {
      const state = taskStateRecordForEvent(event);
      return Boolean(state) && state.status !== "dismissed";
    })
    .forEach(add);
  const rank = {in_progress: 0, pending: 1, completed: 2};
  return [...collected.values()].sort((a, b) => {
    const statusDelta = (rank[taskStateForEvent(a)] ?? 3) - (rank[taskStateForEvent(b)] ?? 3);
    if (statusDelta) return statusDelta;
    return taskOccurrenceTime(a) - taskOccurrenceTime(b);
  });
}

function kanbanIdeaEvents(day) {
  const seen = new Set();
  return eventsForCategories(kanbanDaySourceEvents(day), ["idea"])
    .filter(isUsefulIdeaEvent)
    .filter((event) => !taskStateRecordForEvent(event))
    .filter((event) => {
      const taskKey = eventTaskKey(event);
      if (!taskKey || seen.has(taskKey)) return false;
      seen.add(taskKey);
      return true;
    });
}

function kanbanTodoDisplayTitle(event) {
  const raw = workspaceEventText(event) || "未命名待办";
  return raw.replace(/^(?:查询|便签|记录)[：:]\s*/u, "").trim() || raw;
}

const kanbanActivityTodos = new Map();
let kanbanSourceFilter = "all";

function ensureKanbanActivityTodos(date) {
  if (kanbanActivityTodos.has(date)) return;
  kanbanActivityTodos.set(date, []);
  api(`/api/activity-timeline?date=${encodeURIComponent(date)}`, {}, {silent: true}).then((payload) => {
    const events = [];
    (payload?.activities || []).forEach((activity) => (activity.items || []).forEach((item) => {
      if (item.fact_type !== "todo" || !item.action) return;
      events.push({
        event_id: `${activity.activity_id}#${item.item_id}`,
        _jobId: "activity",
        _artifactPath: date,
        _isLiveWatch: true,
        title: item.action,
        categories: ["todo"],
        source_type: "manual",
        time: {absolute: item.spoken_at || null},
        evidence: {quote: item.action},
      });
    }));
    kanbanActivityTodos.set(date, events);
    if (events.length && typeof activeView === "string" && activeView === "kanban") renderKanban();
  }).catch(() => {});
}

function kanbanOpenClawTasks(localEvents) {
  const payload = openclawPoolState.payload;
  if (!payload) return [];
  const localIds = new Set(localEvents.map((event) => String(event.event_id || "")));
  const label = {wechat_matters: "微信", life_logger_todos: "录音", general_todos: "OpenClaw"};
  const rank = {P0: 0, P1: 1, P2: 2, P3: 3};
  return Object.keys(label).flatMap((key) => (payload[key] || []).map((task) => ({...task, _sourceLabel: label[key]})))
    .filter((task) => !localIds.has(String(task.source?.event_id || "")))
    .sort((a, b) => (rank[a.priority] ?? 9) - (rank[b.priority] ?? 9) || String(b.updated_at || "").localeCompare(String(a.updated_at || "")));
}

function kanbanOpenClawCard(task) {
  const key = `oc:${task.id}`;
  const title = String(task.title || "未命名任务");
  const doing = task.status === "in_progress";
  const done = task.status === "completed";
  const move = done ? "" : kanbanStatusButton(key, doing ? "pending" : "in_progress", doing ? "放回待办" : "开始");
  return `<article class="kanban-card kanban-todo-card${done ? " is-done" : ""}" draggable="true" data-kanban-drag-key="${escapeHtml(key)}" data-kanban-card-key="${escapeHtml(key)}">
    <div class="kanban-card-head"><time>${escapeHtml(beijingManagementTime(task.updated_at) || "时间未知")}</time><span class="kanban-source">${escapeHtml(task._sourceLabel || "OpenClaw")} · ${escapeHtml(String(task.priority || "—"))}</span></div>
    <label class="kanban-todo-main">
      <input type="checkbox" class="kanban-todo-checkbox" data-kanban-task-key="${escapeHtml(key)}" ${done ? "checked" : ""} aria-label="${done ? "恢复" : "标记"}「${escapeHtml(title)}」${done ? "为待办" : "为已完成"}">
      <span class="kanban-card-title">${escapeHtml(title)}</span>
    </label>
    <div class="kanban-card-actions">${move}${done ? "" : kanbanStatusButton(key, "completed", "完成")}${kanbanStatusButton(key, "dismissed", "删除")}</div>
  </article>`;
}

function kanbanSourceChips() {
  const chips = [["all", "全部"], ["local", "录音"], ["微信", "微信"], ["OpenClaw", "OpenClaw"]];
  return `<div class="kanban-source-chips" role="group" aria-label="按来源筛选">${chips.map(([key, label]) =>
    `<button type="button" class="kanban-source-chip${kanbanSourceFilter === key ? " is-active" : ""}" data-kanban-source="${key}" aria-pressed="${kanbanSourceFilter === key}">${label}</button>`).join("")}</div>`;
}

function kanbanStatusButton(taskKey, status, label) {
  const danger = status === "dismissed" ? " is-danger" : "";
  return `<button type="button" class="kanban-action${danger}" data-kanban-set-key="${escapeHtml(taskKey)}" data-kanban-set-status="${status}">${label}</button>`;
}

function kanbanTodoCard(event) {
  const taskKey = eventTaskKey(event);
  const status = taskStateForEvent(event);
  const done = status === "completed";
  const title = kanbanTodoDisplayTitle(event);
  const clock = eventClock(event) || "时间未知";
  const promoted = eventCategories(event).includes("idea");
  const move = status === "in_progress"
    ? kanbanStatusButton(taskKey, "pending", "放回待办")
    : (done ? "" : kanbanStatusButton(taskKey, "in_progress", "开始"));
  return `<article class="kanban-card kanban-todo-card${done ? " is-done" : ""}" draggable="true" data-kanban-drag-key="${escapeHtml(taskKey)}" data-kanban-card-key="${escapeHtml(taskKey)}">
    <div class="kanban-card-head"><time>${escapeHtml(clock)}</time><span class="kanban-source">${escapeHtml(kanbanEventSourceLabel(event))}</span></div>
    <label class="kanban-todo-main">
      <input type="checkbox" class="kanban-todo-checkbox" data-kanban-task-key="${escapeHtml(taskKey)}" ${done ? "checked" : ""} aria-label="${done ? "恢复" : "标记"}「${escapeHtml(title)}」${done ? "为待办" : "为已完成"}">
      <span class="kanban-card-title">${escapeHtml(title)}</span>
    </label>
    ${promoted ? '<div class="kanban-card-foot"><span class="kanban-badge">由灵感转来</span></div>' : ""}
    <div class="kanban-card-actions">${move}${kanbanStatusButton(taskKey, "dismissed", "删除")}</div>
  </article>`;
}

function kanbanDeletedCard(event) {
  const taskKey = eventTaskKey(event);
  const title = kanbanTodoDisplayTitle(event);
  return `<article class="kanban-card kanban-deleted-card">
    <p class="kanban-card-title">${escapeHtml(title)}</p>
    <div class="kanban-card-actions">${kanbanStatusButton(taskKey, "pending", "恢复")}</div>
  </article>`;
}

function kanbanDeletedEvents(day) {
  const seen = new Set();
  return kanbanDaySourceEvents(day).filter((event) => {
    const taskKey = eventTaskKey(event);
    if (!taskKey || seen.has(taskKey) || taskStateForEvent(event) !== "dismissed") return false;
    seen.add(taskKey);
    return true;
  });
}

function kanbanIdeaCard(event) {
  const taskKey = eventTaskKey(event);
  const title = workspaceEventText(event) || "未命名想法";
  const clock = eventClock(event) || "时间未知";
  return `<article class="kanban-card kanban-idea-card"${taskKey ? ` draggable="true" data-kanban-drag-key="${escapeHtml(taskKey)}"` : ""} data-kanban-card-key="${escapeHtml(taskKey)}">
    <div class="kanban-card-head"><time>${escapeHtml(clock)}</time><span class="kanban-source">${escapeHtml(kanbanEventSourceLabel(event))}</span></div>
    <p class="kanban-card-title">${escapeHtml(title)}</p>
    <div class="kanban-card-actions">${taskKey ? `<button type="button" class="kanban-promote" data-kanban-promote-key="${escapeHtml(taskKey)}" aria-label="将「${escapeHtml(title)}」转为待办">转为待办</button>` : ""}${taskKey ? kanbanStatusButton(taskKey, "dismissed", "删除") : ""}</div>
  </article>`;
}

function kanbanMemoCard(memo) {
  const isQuery = memo.kind === "query" || String(memo.quote || "").startsWith("查询：");
  return `<article class="kanban-card kanban-memo-card">
    <div class="kanban-card-head"><time>${escapeHtml(memo.clock || "时间未知")}</time><span class="kanban-source">${isQuery ? "语音查询" : "语音便签"}</span></div>
    <p class="kanban-card-title">${escapeHtml(memo.text || memo.quote || "")}</p>
  </article>`;
}

function kanbanLane({title, eyebrow, events, emptyText, renderCard, tone, dropStatus}) {
  return `<section class="kanban-lane ${tone}"${dropStatus ? ` data-kanban-drop="${dropStatus}"` : ""}>
    <header class="kanban-lane-head">
      <div><span>${escapeHtml(eyebrow)}</span><h2>${escapeHtml(title)}</h2></div>
      <strong class="kanban-lane-count" aria-label="${escapeHtml(title)} ${formatCount(events.length)} 项">${formatCount(events.length)}</strong>
    </header>
    <div class="kanban-lane-body">${events.length ? events.map(renderCard).join("") : `<div class="kanban-empty">${escapeHtml(emptyText)}</div>`}</div>
  </section>`;
}


// ---- OpenClaw 真实任务池（2026-10-24）-------------------------------------
// 与本地抽取事件严格分区：OpenClaw 是**全局所有日期**的任务池，日期筛选不影响它。
const OPENCLAW_POOL_LIMIT = 12;
const openclawPoolState = { payload: null, loadedAt: 0, loading: null, error: null };

function summarizeOpenClaw(payload) {
  if (!payload || typeof payload !== "object") {
    return { state: "error", connected: false, total: null, groups: [], detail: "接口没有返回任务池数据" };
  }
  if (payload.connected === false) {
    return { state: "offline", connected: false, total: null, groups: [],
             detail: payload.error ? String(payload.error) : "OpenClaw 未连接" };
  }
  const groups = [
    ["wechat_matters", "微信事项", payload.wechat_matters],
    ["general_todos", "通用待办", payload.general_todos],
    ["life_logger_todos", "Life Logger 待办", payload.life_logger_todos],
  ].map(([key, label, rows]) => ({ key, label, tasks: Array.isArray(rows) ? rows : [] }))
   .filter((group) => group.tasks.length > 0);
  const total = Number.isFinite(Number(payload.total_open)) ? Number(payload.total_open) : null;
  if (total === 0) {
    return { state: "empty", connected: true, total: 0, groups: [], detail: "已连接，任务池为空" };
  }
  if (total === null && !groups.length) {
    return { state: "error", connected: true, total: null, groups: [], detail: "任务池字段缺失（无 total_open，也无任务数组）" };
  }
  // 时间语义三分（Q182）：last_sync 才是"同步"，source_updated_at 只是"最新任务更新"。
  // 绝不能因为 last_sync_at 兼容别名有值就一律写成"最后同步"。
  const explicitBasis = typeof payload.last_sync_basis === "string" && payload.last_sync_basis
    ? payload.last_sync_basis
    : null;
  const sourceUpdatedAt = typeof payload.source_updated_at === "string" && payload.source_updated_at
    ? payload.source_updated_at
    : null;
  const aliasLastSyncAt = payload.last_sync_at || payload.last_success_at || payload.synced_at || null;
  const lastSync = typeof payload.last_sync === "string" && payload.last_sync
    ? payload.last_sync
    : (explicitBasis === "last_sync" ? aliasLastSyncAt : null);
  const lastSyncBasis = explicitBasis
    || (lastSync ? "last_sync" : (sourceUpdatedAt ? "source_updated_at" : (aliasLastSyncAt ? "unknown" : null)));
  const lastSyncAt = lastSync
    || (lastSyncBasis === "source_updated_at" ? sourceUpdatedAt : null)
    || (lastSyncBasis === "unknown" ? aliasLastSyncAt : null);
  return { state: "ready", connected: true, total, groups, lastSync, lastSyncBasis, lastSyncAt,
           sourceUpdatedAt, detail: payload.error ? String(payload.error) : "" };
}

function openclawTaskCard(task) {
  const t = task || {};
  const source = Array.isArray(t.tags) && t.tags.length
    ? t.tags.join(" · ")
    : (t.source && (t.source.system || t.source.external_id)) || "OpenClaw";
  const updated = beijingManagementTime(t.updated_at);
  const priority = String(t.priority || "—");
  const status = String(t.status || "—");
  return `<article class="openclaw-task" data-task-id="${escapeHtml(String(t.id || ""))}">
      <div class="openclaw-task-head"><time>${escapeHtml(updated || "时间未知")}</time>
        <span class="openclaw-priority p-${escapeHtml(priority.toLowerCase())}">${escapeHtml(priority)}</span>
        <span class="openclaw-status">${escapeHtml(status === "pending" ? "待处理" : status)}</span>
      </div>
      <p class="openclaw-title">${escapeHtml(String(t.title || "未命名任务"))}</p>
      <div class="openclaw-meta"><span>来源：${escapeHtml(source)}</span>${t.project_id ? `<span>项目：${escapeHtml(String(t.project_id))}</span>` : ""}</div>
    </article>`;
}

function beijingManagementTime(value) {
  if (!value) return "";
  // 无偏移墙钟不能推断源时区。
  if (!/(?:Z|[+-]\d{2}:?\d{2})$/i.test(String(value))) return "时间时区未提供";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "时间无效" : `${dateTimeInputValue(date).replace("T", " ")} UTC+8`;
}

function openclawRefreshText(value) {
  const stamp = Number(value);
  if (!Number.isFinite(stamp) || stamp <= 0) return "尚未成功刷新";
  const date = new Date(stamp);
  return Number.isNaN(date.getTime())
    ? "尚未成功刷新"
    : date.toLocaleTimeString("zh-CN", {timeZone: "Asia/Shanghai", hour12: false}) + " UTC+8";
}

// 时间标签语义：只有 basis=last_sync 才能说"最后同步"；
// basis=source_updated_at 说"最新任务更新"；来源不明就说"接口未标明来源"；没有时间就说接口未提供。
function openclawSyncText(summary) {
  const model = summary || {};
  const stamp = (value) => escapeHtml(beijingManagementTime(value));
  const basis = model.lastSyncBasis;
  if (basis === "last_sync" && model.lastSync) {
    return `最后同步：${stamp(model.lastSync)}`;
  }
  if (basis === "source_updated_at" && model.sourceUpdatedAt) {
    return `最新任务更新：${stamp(model.sourceUpdatedAt)}（上游未提供同步时间）`;
  }
  if (model.lastSyncAt) {
    return `任务池时间：${stamp(model.lastSyncAt)}（接口未标明来源）`;
  }
  return "同步时间：接口未提供";
}

function openclawPoolMarkup(summary, options = {}) {
  const limit = Number.isFinite(options.limit) ? options.limit : OPENCLAW_POOL_LIMIT;
  const s = summary || { state: "error", groups: [], detail: "无数据" };
  const badge = s.state === "error"
    ? '<span class="openclaw-badge is-error">读取失败</span>'
    : s.state === "offline"
      ? '<span class="openclaw-badge is-off">未连接</span>'
      : '<span class="openclaw-badge is-ok">已连接</span>';
  const header = `<div class="openclaw-pool-head">
      <div><span class="openclaw-kicker">OPENCLAW · 全局任务池</span>
        <h3>OpenClaw 真实任务池</h3>
        <p class="openclaw-hint">OpenClaw 的全部未完成任务（所有日期），日期筛选不影响。</p></div>
      <div class="openclaw-badges">${badge}
        ${s.total !== null && s.total !== undefined ? `<span class="openclaw-badge">共 ${s.total} 个未完成</span>` : ""}
      </div>
    </div>`;
  const refresh = `<div class="openclaw-refresh-row">
      <span class="openclaw-freshness">最后刷新：${escapeHtml(openclawRefreshText(options.refreshedAt))} · 缓存 60 秒</span>
      <button type="button" class="openclaw-refresh" onclick="refreshOpenClawPool(true)" aria-label="立即刷新 OpenClaw 任务池">刷新</button>
    </div>`;
  if (s.state === "offline" || s.state === "error") {
    return `<div class="openclaw-pool is-${s.state}" role="status">${header}${refresh}
        <div class="openclaw-notice">${s.state === "offline" ? "OpenClaw 未连接，无法确认任务池状态（不代表 0 条）。" : "任务池读取失败：" + escapeHtml(s.detail || "未知错误")}
        <button type="button" class="kanban-retry" onclick="refreshOpenClawPool(true)">重试</button></div></div>`;
  }
  if (s.state === "empty") {
    return `<div class="openclaw-pool" role="status">${header}${refresh}<div class="openclaw-empty">${escapeHtml(s.detail || "任务池为空")}</div></div>`;
  }
  return `<div class="openclaw-pool">${header}${refresh}
      <p class="openclaw-sync">${openclawSyncText(s)}</p>
      <p class="openclaw-foot">这些任务已合并到上面的看板，在那里拖动、完成或删除都会同步回 OpenClaw。</p>
    </div>`;
}

function openclawPoolListMarkup(s, limit, header, refresh) {
  let rendered = 0;
  const body = s.groups.map((group) => {
    const shown = group.tasks.slice(0, Math.max(0, limit - rendered));
    rendered += shown.length;
    const rest = group.tasks.slice(shown.length);
    return `<section class="openclaw-group" data-group="${escapeHtml(group.key)}">
        <h4>${escapeHtml(group.label)} <small>${group.tasks.length} 条</small></h4>
        <div class="openclaw-grid">${shown.map(openclawTaskCard).join("")}</div>
        ${rest.length ? `<details class="openclaw-more"><summary aria-label="展开其余 ${rest.length} 条${escapeHtml(group.label)}">展开其余 ${rest.length} 条</summary><div class="openclaw-grid">${rest.map(openclawTaskCard).join("")}</div></details>` : ""}
      </section>`;
  }).join("");
  const hidden = s.groups.reduce((sum, group) => sum + group.tasks.length, 0) - rendered;
  return `<div class="openclaw-pool">${header}${refresh}
      <p class="openclaw-sync">${openclawSyncText(s)}</p>
      ${body}
      <p class="openclaw-foot">本区为只读展示（后端未提供写接口）；首批显示 ${Math.min(limit, rendered)} 条${hidden > 0 ? `，其余 ${hidden} 条已折叠` : ""}。</p>
    </div>`;
}

async function refreshOpenClawPool(force = false) {
  const host = $("openclaw-pool");
  if (!host) return null;
  if (!force && openclawPoolState.payload && Date.now() - openclawPoolState.loadedAt < 60000) {
    host.innerHTML = openclawPoolMarkup(
      summarizeOpenClaw(openclawPoolState.payload),
      {refreshedAt: openclawPoolState.loadedAt},
    );
    return openclawPoolState.payload;
  }
  if (openclawPoolState.loading) return openclawPoolState.loading;
  host.innerHTML = '<div class="openclaw-pool is-loading" role="status">正在读取 OpenClaw 任务池…</div>';
  openclawPoolState.loading = (async () => {
    try {
      const payload = await api("/api/openclaw/tasks", {}, {silent: true, operation: "读取 OpenClaw 任务池"});
      openclawPoolState.payload = payload;
      openclawPoolState.loadedAt = Date.now();
      openclawPoolState.error = null;
      host.innerHTML = openclawPoolMarkup(
        summarizeOpenClaw(payload),
        {refreshedAt: openclawPoolState.loadedAt},
      );
      if (typeof activeView === "string" && activeView === "kanban") renderKanban();
      return payload;
    } catch (error) {
      openclawPoolState.payload = null;
      openclawPoolState.error = error.message;
      host.innerHTML = openclawPoolMarkup(
        { state: "error", connected: false, total: null, groups: [], detail: error.message },
        {refreshedAt: openclawPoolState.loadedAt},
      );
      return null;
    } finally {
      openclawPoolState.loading = null;
    }
  })();
  return openclawPoolState.loading;
}
window.refreshOpenClawPool = refreshOpenClawPool;

function kanbanLocalHead(date) {
  return `<div class="kanban-section-head">
      <span>当前日期：${escapeHtml(date ? friendlyDate(date) : "未选择")}</span>
    </div>`;
}

const habitsState = {payload: null, notice: ""};

async function loadHabits() {
  try {
    habitsState.payload = await api(`/api/habits?date=${encodeURIComponent(shanghaiDate(0))}`, {}, {force: true, silent: true});
  } catch (error) {
    habitsState.notice = `习惯读取失败：${error.message}`;
  }
  renderHabits();
}

function renderHabits() {
  const root = $("kanban-habits");
  if (!root) return;
  const rows = habitsState.payload?.habits || [];
  root.innerHTML = `<h3>习惯（今天打卡）</h3>
    ${habitsState.notice ? `<p class="judgment-note" role="status">${escapeHtml(habitsState.notice)}</p>` : ""}
    <div class="habit-list">${rows.length ? rows.map((row) => {
      const key = escapeHtml(row.habit_key);
      const title = escapeHtml(row.title);
      return `<div class="habit-item">
        <input type="checkbox" class="kanban-todo-checkbox" data-habit-checkin="${key}" ${row.checked_today ? "checked" : ""} aria-label="今天打卡：${title}">
        <span>${title}</span>
        <small>${row.streak ? `连续 ${formatCount(row.streak)} 天` : ""}</small>
        <button type="button" class="kanban-action" data-habit-archive="${key}" aria-label="不再跟踪：${title}">不再跟踪</button>
      </div>`;
    }).join("") : '<p class="judgment-note">还没有习惯，在下面加一个。</p>'}</div>
    <div class="habit-add"><input id="habit-add-title" type="text" maxlength="60" placeholder="比如：早睡" aria-label="新习惯名称"><button type="button" class="kanban-action" data-habit-add="1">添加习惯</button></div>`;
}

async function habitAction(path, body, notice) {
  try {
    await api(path, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
    habitsState.notice = notice;
  } catch (error) {
    habitsState.notice = `保存失败：${error.message}`;
  }
  await loadHabits();
}

$("kanban-habits")?.addEventListener("change", (event) => {
  const key = event.target?.dataset?.habitCheckin;
  if (key) habitAction("/api/habits/checkin", {target: key, undo: !event.target.checked}, event.target.checked ? "已打卡。" : "已取消今天的打卡。");
});
$("kanban-habits")?.addEventListener("click", (event) => {
  const target = event.target?.closest?.("button");
  if (!target) return;
  if (target.dataset.habitArchive) habitAction("/api/habits/status", {target: target.dataset.habitArchive, status: "dismissed"}, "已不再跟踪。");
  if (target.dataset.habitAdd) {
    const input = $("habit-add-title");
    const title = input?.value.trim();
    if (!title) { habitsState.notice = "请先写习惯名称。"; renderHabits(); return; }
    habitAction("/api/habits/add", {title}, "已添加。");
  }
});

function renderKanban() {
  const root = $("kanban-dashboard");
  if (!root) return;
  if (!habitsState.payload) loadHabits();
  // 任务池是全局数据：无论本地日期是否就绪都先请求（缓存 60s）。
  refreshOpenClawPool().catch(() => {});
  const dateInput = $("kanban-date-input");
  const targetDate = selectedJournalDate || journalDays[0]?.date || shanghaiDate(0);
  if (dateInput && dateInput.value !== targetDate) dateInput.value = targetDate;
  const setStatus = (message, tone = "") => {
    const status = $("kanban-status");
    if (!status) return;
    status.textContent = message;
    status.className = `setting-status${tone ? ` ${tone}` : ""}`;
  };
  if (!journalDays.length) {
    root.setAttribute("aria-busy", "false");
    root.innerHTML = kanbanLocalHead(targetDate) + '<div class="kanban-empty kanban-empty-wide" role="status"><strong>还没有可用的日期数据</strong><span>先在“今天”读取记录；这里不会借用其他日期，也不会显示猜测的数量。</span></div>';
    return;
  }
  const day = journalDays.find((item) => item.date === targetDate);
  if (!day) {
    root.setAttribute("aria-busy", "false");
    root.innerHTML = kanbanLocalHead(targetDate) + `<div class="kanban-empty kanban-empty-wide" role="status"><strong>${escapeHtml(targetDate)} 没有记录</strong><span>这一天没有可用的事件证据；不会显示其他日期的待办或想法。</span></div>`;
    setStatus("当前日期没有可用记录。", "warning");
    return;
  }
  if (!day.canonical) {
    if (!day.canonicalError) ensureJournalDaySynthesis(day);
    root.setAttribute("aria-busy", "true");
    root.innerHTML = kanbanLocalHead(day.date) + (day.canonicalError
      ? `<div class="kanban-empty kanban-empty-wide" role="status"><strong>这一天的统一事件读取失败</strong><span${day.canonicalErrorRaw ? ` title="${escapeHtml(day.canonicalErrorRaw)}"` : ""}>${escapeHtml(day.canonicalError)}；读完之前不显示旧推导数量。</span><button type="button" class="kanban-retry" onclick="retryKanbanDay()">重试</button></div>`
      : '<div class="kanban-empty kanban-empty-wide" role="status"><strong>正在读取这一天的统一事件</strong><span>待办、灵感和语音查询会在读取完成后出现。</span></div>');
    setStatus(day.canonicalError ? "任务看板读取失败。" : "正在读取任务看板。", day.canonicalError ? "error" : "");
    return;
  }
  const todos = kanbanTodoEvents(day);
  const openclaw = kanbanOpenClawTasks(todos);
  const visibleTasks = openclaw.filter((task) => kanbanSourceFilter === "all" || task._sourceLabel === kanbanSourceFilter
    || (kanbanSourceFilter === "local" && task._sourceLabel === "录音"));
  const showLocal = kanbanSourceFilter === "all" || kanbanSourceFilter === "local";
  const laneCards = (status) => [
    ...(showLocal ? todos.filter((event) => taskStateForEvent(event) === status) : []),
    ...visibleTasks.filter((task) => task.status === status),
  ];
  const pending = [...laneCards("pending"), ...visibleTasks.filter((task) => task.status === "blocked")];
  const doing = laneCards("in_progress");
  const finished = laneCards("completed");
  const renderBoardCard = (card) => (String(card.id || "").startsWith("oc_task_") ? kanbanOpenClawCard(card) : kanbanTodoCard(card));
  const deleted = kanbanDeletedEvents(day);
  const ideas = kanbanIdeaEvents(day);
  const memos = extractDayPinnedMemos(day).filter((memo) => memo.kind === "query");
  root.innerHTML = kanbanLocalHead(day.date) + kanbanSourceChips() + `<div class="kanban-board">
    ${kanbanLane({title: "待办", eyebrow: "TODOS", events: pending, emptyText: "没有待办。可以把灵感拖到这里。", renderCard: renderBoardCard, tone: "kanban-lane-todos", dropStatus: "pending"})}
    ${kanbanLane({title: "进行中", eyebrow: "DOING", events: doing, emptyText: "把正在做的事拖到这里。", renderCard: renderBoardCard, tone: "kanban-lane-doing", dropStatus: "in_progress"})}
    ${kanbanLane({title: "已完成", eyebrow: "DONE", events: finished, emptyText: "把做完的事拖到这里。", renderCard: renderBoardCard, tone: "kanban-lane-done", dropStatus: "completed"})}
    ${kanbanLane({title: "灵感与思考", eyebrow: "IDEAS", events: ideas, emptyText: "这一天没有需要继续沉淀的想法。", renderCard: kanbanIdeaCard, tone: "kanban-lane-ideas"})}
    ${kanbanLane({title: "语音查询与指令", eyebrow: "VOICE", events: memos, emptyText: "这一天没有语音查询或指令。", renderCard: kanbanMemoCard, tone: "kanban-lane-voice"})}
  </div>
  ${deleted.length ? `<details class="kanban-deleted"><summary>已删除 ${formatCount(deleted.length)} 项</summary><div class="kanban-lane-body">${deleted.map(kanbanDeletedCard).join("")}</div></details>` : ""}`;
  root.setAttribute("aria-busy", "false");
  setStatus(openclawPoolState.payload
    ? `已读取 ${friendlyDate(day.date)} 的录音待办和 OpenClaw 的 ${formatCount(openclaw.length)} 个任务；OpenClaw 任务不随日期变化。`
    : `已读取 ${friendlyDate(day.date)} 的录音待办；OpenClaw 任务还没读到。`);
}

// 时间轴（iframe）回报的当日事实：父页面用它决定空态与唯一总数，不再自行断言“没有记录”。
const timelineFacts = new Map();
function timelineFactsFor(date) { return timelineFacts.get(date) || null; }

// 【P0 UI 真相】一屏只保留一个主口径：canonical 的 eventCount 优先；
// 时间轴（iframe）只在 canonical 未到时顶上，并明确标注“含时间未定位/即时抓取”。
// 【2026-10-28 返工】「事件轴（主口径）」指标的安全渲染：
// 计数未知时输出占位「—」，绝不把 undefined/null 写进 DOM；
// canonical 的 0 仍显示真实 0；扫描口径在 <small> 里明确标注（未完成）。
function primaryCountMetricHtml(day, facts) {
  const primary = dayPrimaryCount(day, facts);
  const display = (typeof JOURNAL_LOGIC.primaryCountDisplay === "function")
    ? JOURNAL_LOGIC.primaryCountDisplay({ value: primary.value, eventCount: day?.eventCount, formatCount })
    : { text: Number.isFinite(Number(primary.value)) ? formatCount(primary.value)
        : (Number.isFinite(Number(day?.eventCount)) ? formatCount(day.eventCount) : "—"), unknown: primary.value == null };
  const strongAttrs = display.unknown ? ' data-count-unknown="1" title="统计中"' : "";
  const small = primary.source === "activity-timeline"
    ? (primary.rawLabel ? `<small>${escapeHtml(primary.rawLabel)}</small>` : "")
    : (Number.isFinite(Number(day?.structuredEventCount)) && Number(day.structuredEventCount) > Number(day?.eventCount || 0)
        ? `<small>从 ${formatCount(day.structuredEventCount)} 条结构化记录筛选</small>` : "");
  return `<div><span>事件轴（主口径）</span><strong${strongAttrs}>${escapeHtml(display.text)}</strong>${small}</div>`;
}

function dayPrimaryCount(day, facts, explicitActivityTimeline) {
  const activityTimeline = explicitActivityTimeline || day?.activityTimeline || null;
  // 【2026-10-29】「整理失败」必须和「还在更新」分开说：
  //   status=error（或 errors 非空）且**没有任何活动** → 失败态，不能渲染成“统计中”。
  //   有活动就是成功（errors 可能来自更早的窗口），不得误判为失败。
  if (activityTimeline && typeof activityTimeline === "object") {
    const tlStatus = String(activityTimeline.status || "").toLowerCase();
    const tlActs = Array.isArray(activityTimeline.activities) ? activityTimeline.activities.length : 0;
    const tlErrors = Array.isArray(activityTimeline.errors) ? activityTimeline.errors.filter(Boolean) : [];
    if (tlActs === 0 && (tlStatus === "error" || tlErrors.length > 0)) {
      return {
        value: null,
        source: "activity-timeline-error",
        failure: { status: tlStatus || "error", errorCount: tlErrors.length },
        text: "活动整理暂时失败 · 可展开下方时间轴看原始记录",
      };
    }
  }
  if (activityTimeline && activityTimeline.status === "ready" && Array.isArray(activityTimeline.activities)) {
    const actCount = activityTimeline.activities.length;
    const isValidCount = (v) => v !== null && v !== undefined && v !== "" && Number.isFinite(Number(v));
    // 【2026-10-28 B6】原始计数必须标明口径：只有 canonical 才能说「条原始记录」。
    // 扫描/覆盖片段（如 4,377）与时间轴统计都不能冒用完成态口径，否则会被读成“数据丢了”。
    let rawCount = null;
    let rawCountSource = "unknown";
    if (isValidCount(day?.eventCount)) {
      rawCount = Number(day.eventCount);
      rawCountSource = "canonical";
    } else if (isValidCount(day?.structuredEventCount)) {
      rawCount = Number(day.structuredEventCount);
      rawCountSource = "canonical";
    } else if (isValidCount(activityTimeline?.raw_count)) {
      rawCount = Number(activityTimeline.raw_count);
      rawCountSource = "scan";
    } else if (isValidCount(activityTimeline?.raw_record_count)) {
      rawCount = Number(activityTimeline.raw_record_count);
      rawCountSource = "scan";
    } else if (isValidCount(activityTimeline?.coverage?.source_count) && Number(activityTimeline.coverage.source_count) > 0) {
      rawCount = Number(activityTimeline.coverage.source_count);
      rawCountSource = "scan";
    } else if (isValidCount(facts?.events)) {
      rawCount = Number(facts.events);
      rawCountSource = "timeline";
    }
    const summary = (typeof JOURNAL_LOGIC.primaryCountSummary === "function")
      ? JOURNAL_LOGIC.primaryCountSummary({
          activityCount: actCount,
          rawCount,
          rawCountSource,
          formatCount,
        })
      // 兜底：若逻辑文件未加载（旧缓存），保持诚实文案，不退回旧口径。
      : { text: `${formatCount(actCount)} 个活动（候选）· 原始记录待统计`, rawLabel: null, comparable: false, provisional: true };
    return {
      value: actCount,
      rawCount,
      rawCountSource,
      rawLabel: summary.rawLabel,
      comparable: summary.comparable,
      provisional: summary.provisional,
      source: "activity-timeline",
      text: summary.text,
    };
  }

  const rawCount = day ? day.eventCount : null;
  const canonical = (rawCount === null || rawCount === undefined || rawCount === "")
    ? null
    : (Number.isFinite(Number(rawCount)) ? Number(rawCount) : null);
  if (canonical !== null) return { value: canonical, source: "canonical", text: `${formatCount(canonical)} 件事` };
  if (facts) {
    const total = Number(facts.events || 0) + Number(facts.unlocated || 0) + Number(facts.captures || 0);
    const detail = [];
    if (facts.unlocated) detail.push(`含时间未定位 ${facts.unlocated}`);
    if (facts.captures) detail.push(`即时抓取 ${facts.captures}`);
    if (total > 0 || facts.empty === true) {
      return { value: total, source: "timeline",
               text: `${formatCount(total)} 件事（时间轴${facts.empty === true ? "确认" : "统计"}${detail.length ? "，" + detail.join(" · ") : ""}）` };
    }
    // A zero-count summary that has not confirmed "empty" is a loading shell,
    // not a real zero. Do not paint 0 while the read is still in flight.
    return { value: null, source: "unknown", text: "" };
  }
  return { value: null, source: "unknown", text: "" };
}

function eventTotalLabel(facts, canonicalCount) {
  if (!facts) {
    return canonicalCount === null || canonicalCount === undefined ? null : `${formatCount(canonicalCount)} 件事`;
  }
  const total = facts.events + facts.unlocated + facts.captures;
  const detail = [];
  if (facts.unlocated) detail.push(`含时间未定位 ${facts.unlocated}`);
  if (facts.captures) detail.push(`即时抓取 ${facts.captures}`);
  return detail.length ? `${formatCount(total)} 件事（${detail.join(" · ")}）` : `${formatCount(total)} 件事`;
}

// [2026-11-04 Q853] 服务端内部原因不得直接显示给用户。
// 后端会把「read producer exceeded deadline; returned previous snapshot」这类实现细节
// 塞进 error/read_note；在手机上就是一句英文糊在脸上。这里只在**展示层**翻译，
// 不改后端、不改错误对象本身，原文保留在 title 里供排查。
const CANONICAL_ERROR_HUMAN = [
  [/read producer exceeded deadline/i, "后台整理超时，先显示上一次的结果"],
  [/returned previous snapshot/i, "后台还在重算，先显示上一次的结果"],
  [/\btimed?\s?out\b|\btimeout\b/i, "后台整理超时，稍后自动重试"],
  [/connection refused|ECONNREFUSED|fetch failed/i, "连不上本地服务"],
];

function humanizeCanonicalError(raw) {
  let text = String(raw == null ? "" : raw).trim();
  if (!text) return "";
  // 只按"内部英文指纹"命中才算需要翻译；命中后连"服务端错误 500："这种技术前缀一起去掉。
  for (const [pattern, human] of CANONICAL_ERROR_HUMAN) {
    if (pattern.test(text)) return human;
  }
  // 其余已经是中文的报错是给人看的（例如"后端尚未加载统一事件口径"），原样保留。
  if (/[\u4e00-\u9fff]/.test(text)) return text;
  // 兜底：整句英文且没有中文 → 不外露原文。
  if (/[A-Za-z]{3,}/.test(text)) return "读取失败（内部原因已隐藏，悬停或看日志）";
  return text;
}

// 统一“加载 vs 空态”判定：有任何事实就不许说空；3 秒升级文案但仍不假空。
function journalLoadState({ facts, error, pending, elapsedMs }) {
  if (error) return { kind: "error", text: "统一统计口径读取失败" };
  if (pending) return { kind: "pending", text: "正在更新这一天的记录" };
  if (facts && facts.empty) return { kind: "empty", text: "这一天没有记录" };
  const seen = facts ? facts.events + facts.unlocated + facts.captures : 0;
  if (seen > 0) return { kind: "partial", text: "时间轴已就绪，分类与账本仍在读取" };
  return Number(elapsedMs) >= 3000
    ? { kind: "slow", text: "读取较慢，仍在继续" }
    : { kind: "loading", text: "正在读取这一天的事件与账目" };
}

// 时间轴 iframe 区块：任何日期（含 canonical 未到）都先挂载，父页面不再等 /api/daily。
function timelineSectionMarkup(date, dayWord) {
  return `<section class="day-timeline-section focus-timeline" aria-labelledby="day-timeline-heading">
      <div class="day-timeline-heading"><div><h4 id="day-timeline-heading" title="每10分钟整理，UTC+8">${escapeHtml(dayWord)}发生了什么</h4><small class="hint day-timeline-hint" title="每10分钟整理，UTC+8">每10分钟整理 · UTC+8</small></div><span class="day-recording-legend"><i aria-hidden="true"></i>绿色=有录音覆盖</span></div>
      <iframe class="day-timeline-frame" data-timeline-date="${escapeHtml(date)}" src="/waterfall-v2.html?date=${encodeURIComponent(date)}&embedded=1" title="当日事件轴（瀑布图）" loading="eager"></iframe>
    </section>`;
}

function daySwitchBannerMarkup(date, visible) {
  return `<div class="journal-switch-banner" id="day-switch-banner" role="status"${visible ? "" : " hidden"} style="display:${visible ? "block" : "none"};margin:8px 0;padding:6px 10px;border-radius:8px;background:#fff8eb;color:#8b6a32;font-size:11.5px;border:1px solid #e8d7b8;">正在切换到 ${escapeHtml(date)} · 时间轴先显示，统计稍后补上</div>`;
}

// 【2026-10-29 诊断】只读打点：记录父页向 iframe 下发的每条时钟/数据消息。
function llTraceSend(target, msg, why) {
  try {
    const box = (window.__llTrace = window.__llTrace || []);
    box.push({t: Math.round(performance.now()), kind: "parent-send",
              type: msg && msg.type, date: msg && msg.date, why: why || ""});
    if (box.length > 400) box.splice(0, box.length - 400);
  } catch (_) { /* 打点失败不影响业务 */ }
  target?.postMessage(msg, window.location.origin);
}

function bindTimelineFrameState(frame, date, scope) {
  if (!frame) return;
  hideEmbeddedTimelineSummary(frame);
  const banner = (scope || document).querySelector("#day-switch-banner");
  const settle = () => {
    try {
      if (timelineRenderedDate(frame) === date) {
        frame.dataset.timelineDate = date;
        if (banner) { banner.hidden = true; banner.style.display = "none"; }
      }
    } catch (_) { /* 跨文档异常时保持提示 */ }
  };
  frame.addEventListener("load", () => { hideEmbeddedTimelineSummary(frame); settle(); }, {once: true});
  let tries = 0;
  const timer = setInterval(() => {
    settle();
    if (++tries >= 40) clearInterval(timer);
  }, 100);
}

function broadcastAcceptedItems(ledger) {
  const rows = Array.isArray(ledger && ledger.transactions) ? ledger.transactions : [];
  return rows.filter((tx) => tx && typeof tx === "object"
    && tx.decision === "pending_broadcast_accept" && tx.event_id);
}

function broadcastAmountText(item, state) {
  const currency = String(item && item.currency || "CNY").toUpperCase();
  // 已计入/已排除：账本 transactions 里的 amount 是权威值，优先于模型建议金额。
  const source = item || {};
  const candidates = String(state || "") === "accepted"
    ? [source.amount, source.proposed_amount]
    : [source.proposed_amount, source.amount];
  const amount = candidates
    .map((value) => (value === null || value === undefined || value === "" ? NaN : Number(value)))
    .find((value) => Number.isFinite(value));
  // 没有任何可信金额：显示“未知”，绝不当成 0，也不编造。
  if (!Number.isFinite(amount)) return "未知";
  return currency === "CNY" ? formatMoney(amount) : `${formatFixed2(amount)} ${currency}`;
}

function broadcastArg(value) {
  return String(value == null ? "" : value).replace(/[^A-Za-z0-9_.:-]/g, "");
}

function broadcastItemTitle(item) {
  const explicit = String(item && item.item || "").trim();
  if (explicit) return explicit;
  const title = String(item && item.title || "").trim();
  if (!title) return "播报金额";
  return title.replace(/\s*[(（]¥[^)）]*[)）]\s*$/u, "").trim() || title;
}

function broadcastReasonText(item) {
  const explanation = Array.isArray(item && item.explanation)
    ? item.explanation.map((value) => String(value))
    : [];
  const directionEntry = explanation.find((value) => value.startsWith("pos_direction:")) || "";
  const direction = directionEntry.split(":")[1] || "";
  if (direction === "inflow") return "这是收款播报（钱进商家账户），不是你花的钱。";
  if (direction === "outflow") return "这是付款播报，看起来是你付的，需要你确认。";
  if (direction === "ambiguous") return "只有金额、没有「到账/扣款」方向词，判断不了谁付的。";
  const reason = String(item && item.reason || "");
  if (reason === "pos_broadcast_unattributed") return "这段声音不是本人，不能算到你头上。";
  if (reason === "pos_broadcast_ambiguous") return "只有金额没有方向，先不计入，等你确认。";
  if (reason === "unverified_expense_data") return "抽取结果没通过校验，金额暂不采信。";
  return "需要你确认这笔钱算不算你的支出。";
}

function broadcastRejectedItems(day) {
  const ledger = day && day.expense_ledger || {};
  const pendingIds = new Set((Array.isArray(ledger.pending_broadcast) ? ledger.pending_broadcast : [])
    .map((item) => String(item && item.event_id || "")));
  const countedIds = new Set((Array.isArray(ledger.transactions) ? ledger.transactions : [])
    .map((tx) => String(tx && tx.event_id || "")));
  const events = Array.isArray(day && day.timeline) ? day.timeline : [];
  return events.filter((event) => {
    const expense = event && event.data && event.data.expense;
    if (!expense || typeof expense !== "object") return false;
    const channel = String(expense.payment_channel || "");
    const title = String(event && event.title || "");
    if (channel !== "offline_qr_pos" && !title.startsWith("线下支付：")) return false;
    const eventId = String(event && event.event_id || "");
    return Boolean(eventId) && !pendingIds.has(eventId) && !countedIds.has(eventId);
  }).map((event) => ({
    event_id: event.event_id,
    clock: event.clock || "",
    item: (event.data.expense.item || "").trim(),
    title: event.title || "",
    quote: event.evidence && event.evidence.quote || "",
    proposed_amount: event.data.expense.amount,
    currency: event.data.expense.currency || "CNY",
  }));
}

function broadcastRow(day, item, state) {
  const date = String(day && day.date || "");
  const eventId = broadcastArg(item && item.event_id);
  const clock = String(item && item.clock || "").trim() || "时间未知";
  const title = broadcastItemTitle(item);
  const amountText = broadcastAmountText(item, state);
  const quote = String(item && item.quote || "").trim();
  const why = state === "pending"
    ? broadcastReasonText(item)
    : (state === "accepted" ? "已计入当日总额。" : "已排除，不计入当日总额。");
  const action = state === "pending" ? "accept" : "undo";
  const actionLabel = state === "pending" ? "计入" : "撤销";
  const secondary = state === "pending"
    ? `<button type="button" class="broadcast-action" data-broadcast-action="reject" onclick="submitBroadcastDecision(this,'${broadcastArg(date)}','${eventId}','reject')" aria-label="排除 ${escapeHtml(clock)} ${escapeHtml(title)} ${escapeHtml(amountText)}">排除</button>`
    : "";
  const statusTag = state === "pending"
    ? '<span class="broadcast-tag">建议</span>'
    : `<span class="broadcast-tag ${state === "accepted" ? "is-accepted" : "is-rejected"}">${state === "accepted" ? "已计入" : "已排除"}</span>`;
  return `<li class="broadcast-item" data-broadcast-state="${state}">
    <div class="broadcast-item-head"><time>${escapeHtml(clock)}</time><strong>${escapeHtml(amountText)}</strong>${statusTag}</div>
    <p class="broadcast-item-title">${escapeHtml(title)}</p>
    ${quote ? `<p class="broadcast-quote">「${escapeHtml(quote)}」</p>` : ""}
    <p class="broadcast-why">${escapeHtml(why)}</p>
    <div class="broadcast-actions">
      <button type="button" class="broadcast-action primary" data-broadcast-action="${action}" onclick="submitBroadcastDecision(this,'${broadcastArg(date)}','${eventId}','${action}')" aria-label="${actionLabel} ${escapeHtml(clock)} ${escapeHtml(title)} ${escapeHtml(amountText)}">${actionLabel}</button>
      ${secondary}
    </div>
  </li>`;
}

function broadcastUi(date) {
  if (!broadcastUiState.has(date)) {
    broadcastUiState.set(date, {busy: "", open: false, message: "", messageKind: ""});
  }
  return broadcastUiState.get(date);
}

function canonicalArtifactPath(value) {
  const text = String(value || "");
  for (const segment of ["files/", "result/"]) {
    if (text.startsWith(segment)) return text;
    const marker = `/${segment}`;
    const index = text.indexOf(marker);
    if (index >= 0) return text.slice(index + 1);
  }
  return text;
}

function canonicalCategoryCounts(categorySummary) {
  const counts = {};
  if (categorySummary && typeof categorySummary === "object") {
    for (const [key, value] of Object.entries(categorySummary)) {
      const count = Number(value?.count ?? value);
      if (Number.isFinite(count)) counts[canonicalEventCategory(key)] = count;
    }
  }
  return counts;
}

function canonicalContractError(payload, requestedScope = "provisional", requestedView = "display") {
  const summary = payload?.daily_summary || {};
  const eventView = summary.event_view;
  if (!eventView || typeof eventView !== "object") {
    return "后端尚未加载统一事件口径，请更新服务（daily_summary.event_view 缺少 metadata）";
  }
  if (String(eventView.schema_version || "") !== CANONICAL_EVENT_VIEW_SCHEMA) {
    return `后端事件口径版本不兼容（${eventView.schema_version || "缺少 schema_version"}），请更新服务`;
  }
  const scope = String(eventView.scope || summary.accounting_scope || payload?.accounting_scope || payload?.scope || "");
  const view = String(eventView.view || summary.view || payload?.view || "");
  if (scope !== requestedScope || view !== requestedView) {
    return `后端事件口径不一致（请求 ${requestedScope}/${requestedView}，返回 ${scope || "未知"}/${view || "未知"}），请更新服务`;
  }
  const policyVersion = String(payload?.expense_ledger?.policy_version || "");
  if (!CANONICAL_EXPENSE_POLICY_VERSIONS.has(policyVersion)) {
    return `后端账本策略版本不兼容（${policyVersion || "缺少 policy_version"}），请更新服务`;
  }
  return null;
}

function canonicalCorrectionTarget(event) {
  const explicitJob = String(event?.source_job_id || "").trim();
  const jobIds = Array.isArray(event?.source_job_ids) ? event.source_job_ids : [];
  const jobId = /^job-/.test(explicitJob)
    ? explicitJob
    : jobIds.map((value) => String(value || "").trim()).find((value) => /^job-/.test(value)) || "";
  const candidate = event?.artifact_path || event?.source_event_artifact || event?.events_artifact_path || "";
  const artifactPath = canonicalArtifactPath(candidate);
  const validEventsArtifact = Boolean(jobId) && /(?:^|\/)events\.json$/i.test(artifactPath);
  return {
    jobId: jobId || (explicitJob || null),
    artifactPath: validEventsArtifact ? artifactPath : null,
    available: validEventsArtifact,
  };
}

function currentPendingReviewDate() {
  const block = document.getElementById("pending-review-block");
  return (block && block.dataset ? block.dataset.date : "") || selectedJournalDate || "";
}

function extractDayExpenseLedger(day) {
  // 金额只认后端 /api/daily 的 expense_ledger。
  // 接口缺失时返回“不可用/空”，绝不在前端按标题/关键词重新分类或重新求和。
  const ledger = day && day.expense_ledger;
  if (ledger && typeof ledger === "object") return ledger;
  return {
    total_amount: null,
    currency: "CNY",
    totals_by_currency: {},
    category_totals_by_currency: {},
    category_breakdown: {},
    transactions: [],
    pending_transactions: [],
    pending_count: 0,
    has_records: false,
    unavailable: true,
  };
}

function extractDayFitnessLedger(day) {
  // [2026-11-07 唯一事实来源·P1] **删除回退重算**：原来 has_records=false 时从前端时间线
  // 按关键词重新数组数/动作，等于第二套事实源。现在：ledger 存在就原样用（哪怕没有记录），
  // 整个缺失（老接口）才返回"暂无数据"，两种情况下都不再由前端造数字。
  const ledger = day && day.fitness_ledger;
  if (ledger && typeof ledger === "object") return ledger;
  return {has_records: false, unavailable: true, unavailable_reason: "ledger_missing",
          workouts: [], target_body_parts: [], workout_count: 0, recovery_reflections: []};
}

function extractDayNutritionLedger(day) {
  // [2026-11-07 唯一事实来源·P1] 同上：不再从 timeline 重算热量/宏量/餐次。
  const ledger = day && day.nutrition_ledger;
  if (ledger && typeof ledger === "object") return ledger;
  return {has_records: false, unavailable: true, unavailable_reason: "ledger_missing",
          meals: {}, meal_counts: {}, purchases: [], other_records: [], food_records: [],
          record_count: 0, consumed_record_count: 0, purchase_record_count: 0,
          estimated_count: 0, unknown_nutrition_count: 0, calorie_coverage: null,
          totals: {}, totals_known_counts: {}, totals_unknown_counts: {}, totals_complete: false};
}

function extractDaySmokingLedger(day) {
  // [2026-11-07 唯一事实来源·P1] 同上：不再按标题/引文关键词判"是否吸烟"。
  const ledger = day && day.smoking_ledger;
  if (ledger && typeof ledger === "object") return ledger;
  return {has_records: false, unavailable: true, unavailable_reason: "ledger_missing",
          total_count: 0, brands: [], sessions: [],
          psychology: {commitments: [], compromises: [], has_relapse_conflict: false,
                       cutoff_rule: null, cognitive_insight: ""}};
}

function extractDayWorkCognitionLedger(day) {
  // [2026-11-07 唯一事实来源·P1] 同上：不再从事件重算条目数。
  const ledger = day && day.work_cognition_ledger;
  if (ledger && typeof ledger === "object") return ledger;
  return {has_records: false, unavailable: true, unavailable_reason: "ledger_missing",
          domains: {}, decisions: [], insights: [], items: [], item_count: 0};
}

function findPendingReviewItem(sourceId) {
  for (const payload of [pendingReviewState.payload, pendingReviewState.expanded]) {
    for (const group of payload?.groups || []) {
      for (const item of group.items || []) {
        if (String(item?.source_id || "") === String(sourceId)) return item;
      }
    }
  }
  return null;
}

function focusPendingReviewInbox() {
  const block = document.getElementById("pending-review-block");
  if (!block) {
    if (activeView !== "today") {
      switchView("today");
      window.setTimeout(() => {
        const target = document.getElementById("pending-review-block");
        target?.setAttribute("tabindex", "-1");
        target?.focus({preventScroll: true});
      }, 60);
    }
    return;
  }
  block.setAttribute("tabindex", "-1");
  block.focus({preventScroll: true});
}

function hydrateJournalDayFromCanonical(day, payload) {
  const contractError = canonicalContractError(payload);
  if (contractError) throw new Error(contractError);
  const summary = payload?.daily_summary || {};
  const events = Array.isArray(payload?.events)
    ? payload.events
    : (Array.isArray(summary.events) ? summary.events : []);
  const canonicalEvents = events.map((event) => {
    const target = canonicalCorrectionTarget(event);
    const liveJob = String(event.source_job_id || event.source_job_ids?.[0] || "");
    return {
      ...event,
      // Write overlays accept only events.json. source_artifact often points at
      // daily_summary.json and must stay provenance, never an edit target.
      artifact_path: target.artifactPath,
      _jobId: target.jobId,
      _artifactPath: target.artifactPath,
      _correctionAvailable: target.available,
      _correctionUnavailable: !target.available,
      _isLiveWatch: Boolean(event._isLiveWatch || event.source_kind === "live" || /^watch_/.test(liveJob)),
      _provenance: event.provenance || null,
      _sourceJobId: event.source_job_id || null,
      _sourceArtifact: event.source_artifact || null,
      _sourceEventArtifact: event.source_event_artifact || null,
    };
  });
  day.timeline = canonicalEvents;
  day.workspaceEvents = canonicalEvents;
  day.eventCount = Number.isFinite(Number(summary.event_count)) ? Number(summary.event_count) : canonicalEvents.length;
  day.structuredEventCount = day.eventCount;
  day.categories = canonicalCategoryCounts(summary.category_summary);
  const rawExpense = payload?.expense_ledger?.total_amount;
  // Number(null) is 0; a missing/null canonical amount is unknown, not a real
  // zero. Keep it null so the UI renders "未知" instead of inventing ¥0.
  const expenseTotal = rawExpense === null || rawExpense === undefined || rawExpense === "" ? NaN : Number(rawExpense);
  day.expense = Number.isFinite(expenseTotal) ? expenseTotal : null;
  day.accountingScope = summary.accounting_scope || payload?.accounting_scope || payload?.scope || "provisional";
  day.eventView = typeof summary.event_view === "object" ? summary.event_view : summary.event_view || payload?.view || "display";
  day.sourceDailySummaries = Array.isArray(summary.source_daily_summaries) ? summary.source_daily_summaries : [];
  day.nutrition_ledger = payload?.nutrition_ledger || day.nutrition_ledger;
  day.fitness_ledger = payload?.fitness_ledger || day.fitness_ledger;
  day.smoking_ledger = payload?.smoking_ledger || day.smoking_ledger;
  day.expense_ledger = payload?.expense_ledger || day.expense_ledger;
  day.work_cognition_ledger = payload?.work_cognition_ledger || day.work_cognition_ledger;
  day.pinned_memos = payload?.pinned_memos || day.pinned_memos;
  day.synthesis = payload?.synthesis || day.synthesis;
  day.synthesisStale = payload?.synthesis_stale;
  day.synthesisStatus = payload?.synthesis_status || null;
  day.canonical = true;
  day.canonicalError = null;
  day.canonicalErrorRaw = null;   // [Q853] 成功后不得在 title 里残留上一次的内部原因
  day._canonicalLoadedAt = Date.now();
  day._synthesisLoaded = true;
  day._canonicalAttempted = true;
}

function invalidateJournalCanonicalCache() {
  journalCanonicalGeneration += 1;
  journalActivityTimelineGeneration += 1;
  if (typeof journalInitialPrefetchAttempted !== "undefined") journalInitialPrefetchAttempted.clear();
  journalCanonicalCache.clear();
  journalActivityTimelineCache.clear();
  if (typeof LifeTimelineData !== "undefined") {
    LifeTimelineData.clear("/api/daily?");
    LifeTimelineData.clear("/api/activity-timeline?");
    LifeTimelineData.clear("/api/dashboard/daily?");
    LifeTimelineData.clear("/api/timeline-scenes?");
  }
  for (const day of journalDays) {
    day._canonicalAttempted = false;
    day._canonicalLoadedAt = 0;
    day._activityTimelineAttempted = false;
  }
}

function journalFreshnessAgeLabel(timestamp, now = Date.now()) {
  const ageMs = Math.max(0, now - Number(timestamp || 0));
  if (ageMs < 45_000) return "刚刚更新";
  const minutes = Math.max(1, Math.floor(ageMs / 60_000));
  if (minutes < 60) return `${formatCount(minutes)} 分钟前更新`;
  const hours = Math.max(1, Math.floor(minutes / 60));
  return `${formatCount(hours)} 小时前更新`;
}

function kanbanEventForKey(taskKey) {
  return journalDays
    .flatMap((day) => [...(day.workspaceEvents?.length ? day.workspaceEvents : day.timeline), ...(kanbanActivityTodos.get(day.date) || [])])
    .find((event) => eventTaskKey(event) === taskKey);
}

function loadJournalDayScenes(date) {
  if (journalScenesCache.has(date)) return;
  api(`/api/timeline-scenes?date=${encodeURIComponent(date)}`, {}, {silent: true}).then((res) => {
    if (res && Array.isArray(res.scenes)) {
      journalScenesCache.set(date, res.scenes);
      if (selectedJournalDate === date) {
        const day = journalDays.find((d) => d.date === date);
        if (day) renderJournalDay(day);
      }
    }
  }).catch(() => {});
}

function loadJournalFunnel(date) {
  if (journalFunnelCache.has(date)) return;
  api(`/api/capture-funnel?date=${encodeURIComponent(date)}`, {}, {silent: true}).then((res) => {
    const funnel = res?.capture_funnel;
    if (funnel && typeof funnel.said_sentences === "number") {
      journalFunnelCache.set(date, funnel);
      if (selectedJournalDate === date) {
        const day = journalDays.find((d) => d.date === date);
        if (day) renderJournalDay(day);
      }
    }
  }).catch(() => {});
}

function markJournalRefreshFailure(error) {
  journalRefreshError = String(error?.message || error || "日记刷新失败");
  renderJournalFreshness();
}

function markJournalRefreshSuccess() {
  journalLastSuccessAt = Date.now();
  journalRefreshError = "";
  journalReadStale = null;   // [2026-10-30] 新数据到手，旧快照标记必须消失
  renderJournalFreshness();
}

function nutritionCoverageFor(ledger, key) {
  // [2026-11-07 唯一事实来源·P1] 覆盖率只认后端 `nutrition_ledger.totals_coverage`
  // （后端在同一份 ledger 里给出 consumed_records / calories_known / calories_unknown / macros）。
  // 前端不再自己 known/total 地算——那是第二套口径。
  const coverage = ledger && ledger.totals_coverage;
  if (coverage && typeof coverage === "object") {
    if (key === "calories_kcal") {
      return {
        known: Number(coverage.calories_known ?? 0),
        unknown: Number(coverage.calories_unknown ?? 0),
        total: Number(coverage.consumed_records ?? 0),
        source: "backend",
      };
    }
    const macro = (coverage.macros || {})[key];
    if (macro && typeof macro === "object") {
      return {
        known: Number(macro.known ?? 0),
        unknown: Number(macro.unknown ?? 0),
        total: Number(macro.known ?? 0) + Number(macro.unknown ?? 0),
        source: "backend",
      };
    }
  }
  // 老接口没有 totals_coverage：只透传后端给的条数，known/unknown 一律"未知"，
  // 绝不退回"前端自己数 items"的老路。
  return {
    known: null,
    unknown: null,
    total: Number((ledger && ledger.consumed_record_count) ?? 0),
    source: "backend_partial",
  };
}

function nutritionItemHasValue(item, key) {
  if (key === "calories_kcal") {
    return item?.calories_known === true || (item?.calories_known !== false && nutritionKnownNumber(item?.calories_kcal) !== null);
  }
  if (item?.nutrient_known && typeof item.nutrient_known[key] === "boolean") return item.nutrient_known[key];
  return nutritionKnownNumber(item?.nutrients?.[key]) !== null;
}

function nutritionKnownNumber(value) {
  if (value === null || value === undefined || value === "" || typeof value === "boolean") return null;
  const number = Number(value);
  return Number.isFinite(number) && number >= 0 ? number : null;
}

function nutritionRecordType(title, quote, isSupplement) {
  const text = `${title || ""} ${quote || ""}`;
  if (/(?:吃了|喝了|吃到|喝到|摄入|尝了|饮用|食用)/u.test(text)) return "consumed";
  if (/(?:购买|购入|买了|买一|买两|买瓶|买包|买杯|买份|买点|买些|下单|\d+(?:\.\d+)?\s*(?:元|块|块钱))/u.test(text)) return "purchase";
  if (isSupplement || /(?:早餐|午餐|晚餐|加餐|夜宵|吃了|喝了)/u.test(text)) return "consumed";
  return "unknown";
}

// ==== 待人工审核（pending_sources）只读投影与人工处理动作 ====
function patchPendingReviewBody() {
  const body = document.getElementById("pending-review-body");
  if (body) body.innerHTML = pendingReviewBodyHtml(currentPendingReviewDate(), pendingReviewState);
  updateSourceInboxBadge();
  if (typeof updateReviewTabCounts === "function") updateReviewTabCounts();
}

function pendingGroupCount(groups, key) {
  const group = (groups || []).find((item) => item.key === key);
  return group ? Number(group.count || 0) : 0;
}

function pendingOpenCounts(totals = {}, groups = [], allDates = false) {
  const groupActionable = pendingGroupCount(groups, "needs_review");
  const openActionable = Number(totals.open_actionable);
  const scopedActionable = Number(totals.scoped_open_actionable);
  const groupGap = pendingGroupCount(groups, "extraction_gap");
  const openGap = Number(totals.open_extraction_gap);
  const scopedGap = Number(totals.scoped_open_extraction_gap);
  const openBatch = Number(totals.open_batch_failure);
  return {
    // Never use totals.needs_review here: it includes resolved rows on legacy
    // payloads. Only an explicit open field or the open group count is valid.
    globalActionable: Number.isFinite(openActionable)
      ? openActionable
      : (allDates ? groupActionable : null),
    scopedActionable: Number.isFinite(scopedActionable) ? scopedActionable : groupActionable,
    globalGap: Number.isFinite(openGap) ? openGap : (allDates ? groupGap : null),
    scopedGap: Number.isFinite(scopedGap) ? scopedGap : groupGap,
    openBatch: Number.isFinite(openBatch)
      ? openBatch
      : (Number.isFinite(Number(totals.batch_failure)) ? Number(totals.batch_failure) : pendingGroupCount(groups, "batch_failure")),
  };
}

function pendingResolutionChips(totals = {}) {
  const fields = [
    "resolved_ignored_by_user",
    "resolved_handled_by_user",
    "resolved_failed_archived",
  ];
  const hasSplit = fields.every((field) => Object.prototype.hasOwnProperty.call(totals, field));
  if (hasSplit) {
    const present = fields.every((field) => totals[field] !== null && totals[field] !== undefined && totals[field] !== "");
    const values = fields.map((field) => Number(totals[field]));
    if (present && values.every((value) => Number.isFinite(value) && value >= 0)) {
      return [
        `<span class="is-user-ignored">人工忽略 ${pendingReviewNumber(values[0])}</span>`,
        `<span class="is-user-handled">人工处理 ${pendingReviewNumber(values[1])}</span>`,
        `<span class="is-dump">批次失败归档 ${pendingReviewNumber(values[2])}</span>`,
      ].join("");
    }
  }
  // Old backends only provide ``resolved``. Never invent a 20/1/6529 split
  // from that aggregate; show the legacy total and say the backend needs upgrade.
  if (Object.prototype.hasOwnProperty.call(totals, "resolved")) {
    const resolved = Number(totals.resolved);
    if (Number.isFinite(resolved) && resolved >= 0) {
      return `<span class="is-legacy">旧口径：resolved ${pendingReviewNumber(resolved)}（无法区分人工/批次归档）· 待后端升级</span>`;
    }
  }
  return '<span class="is-legacy">旧后端未提供已处理拆分字段 · 待后端升级</span>';
}

function pendingReviewAmount(item) {
  const raw = item?.proposed_amount;
  if (raw === null || raw === undefined || raw === "") return "";
  const amount = Number(raw);
  if (!Number.isFinite(amount)) return "";
  const text = amount.toLocaleString("zh-CN", {maximumFractionDigits: 2});
  const currency = String(item?.proposed_currency || "").toUpperCase();
  if (!currency) return text;
  return currency === "CNY" ? `${text} 元` : `${text} ${currency}`;
}


// ── 生活记录优先的审核首版（2026-10-25）─────────────────────────────────────
// 首页只问最多 3 个人话问题；技术与异常挪到「系统健康」。不新增后端能力：
// 确认/已看过/忽略 全部复用已验证接口；撤销 = status:open（接口早已支持）。
const QUESTION_LIMIT = 3;
const HEALTH_GROUPS = new Set(["extraction_gap", "batch_failure"]);
const NO_EVENT_REASONS = new Set(["model_no_event_but_directive", "dictation_review"]);
const MEDIA_REASONS = new Set(["unknown_speaker", "non_self_review"]);


function pendingQuestionDrawerHtml(item) {
  const id = encodeURIComponent(String(item?.source_id || "")).replace(/'/g, "%27");
  const sid = String(item?.source_id || "");
  const kind = pendingQuestionKind(item);
  const hasJob = pendingQuestionHasJob(item);
  const option = (label, action, opts) => {
    const disabled = Boolean(opts && opts.disabled);
    const opArg = opts && opts.op ? `,'${opts.op}'` : "";
    const note = opts && opts.note ? `<small class="pq-opt-note">${escapeHtml(opts.note)}</small>` : "";
    return `<button type="button" class="pq-option" ${disabled ? "disabled" : ""} ${disabled ? "" : `onclick="pendingReviewAction('${action}','${id}'${opArg})"`}>${escapeHtml(label)}${note}</button>`;
  };

  const reasonLabel = String(item?.reason_label || "系统未提取出明确生活事件");
  const reasonCode = String(item?.reason || "unknown");
  const speaker = String(item?.speaker || item?.source_label || "未知");

  // 动态为什么问与具体未知项说明
  const whyAsk = kind === "query"
    ? "录音包含指令或查询语句，但未抽取到明确生活事件，需要你确认这段话是说给其他应用还是记录。"
    : (kind === "media"
      ? "录音疑似包含外部播放声音或背景人声，需要你确认是否为你本人所说。"
      : "系统未识别出明确事件行为，需要你做来源性质判断。");
  const unknownDetail = `未知项：说话人性质/意图归属（当前判断：${reasonLabel}，说话人：${speaker}）。`;

  let options = "";
  if (kind === "query") {
    options = option("是给其他助手 / 应用说的", "answer-question", {
        op: "query",
        note: "只记录来源用途，不执行、不生成事件；声源合同通过后才可用",
      })
      + option("是明确给 Life Logger 的记录", "confirm", hasJob ? {} : { disabled: true, note: "无法找到对应录音，暂不能保存" })
      + option("暂不确定，保留待回答", "keep-pending", { note: "只是先放着，不会从待回答里消失" })
      + option("复制原文", "copy");
  } else if (kind === "media") {
    options = option("是我本人说的（只记来源判断，不生成事件）", "answer-question", {
        op: "personal",
        note: "声源合同通过后才可用；现在不改记录、不算花费",
      })
      + option("是影视 / 广告", "answer-question", {
        op: "media",
        note: "声源合同通过后才可用；现在仅关闭本来源提醒（可撤销），不影响其他录音",
      })
      + option("暂不确定，保留待回答", "keep-pending", { note: "只是先放着，不会从待回答里消失" })
      + option("复制原文", "copy");
  } else {
    options = option("确认并保存为人工记录", "confirm", hasJob ? {} : { disabled: true, note: "无法找到对应录音，暂不能保存" })
      + option("稍后处理（保留在待回答）", "keep-pending", { note: "不写任何状态，条目继续留着" })
      + option("关闭这条提醒", "handled", { note: "会从待回答里移除；可撤销" });
  }

  // 撤销入口（支持1号真实扁平字段 review_question_state/operation/op_id/undoable，并保留旧嵌套兼容）
  const savedState = pendingReviewState?.answeredQuestions?.[sid]
    || (item?.review_question_state && item.review_question_state !== "unknown" && item.review_question_state !== "unanswered"
        ? {
            state: item.review_question_state,
            operation: item.review_question_operation || null,
            op_id: item.review_question_op_id || null,
            undoable: item.review_question_undoable !== false,
            answered_at: item.review_question_answered_at || null,
            prior_status: item.review_question_prior_status || null,
          }
        : null)
    || (item?.review_question && typeof item.review_question === "object"
        ? {
            state: item.review_question.state,
            operation: item.review_question.operation || null,
            op_id: item.review_question.op_id || null,
            undoable: item.review_question.undoable !== false,
          }
        : null)
    || (item?.question_state && item.question_state !== "unknown" && item.question_state !== "unanswered"
        ? { state: String(item.question_state), operation: null, undoable: true }
        : null);

  const canUndo = Boolean(savedState && savedState.state && savedState.state !== "unanswered" && savedState.state !== "unknown" && savedState.undoable !== false);
  const undoHtml = canUndo
    ? `<div class="pq-undo-bar">
        <span>已记录来源判断：<strong>${escapeHtml(String(savedState.state))}</strong>${savedState.operation ? ` (${escapeHtml(String(savedState.operation))})` : ""}</span>
        <button type="button" class="pq-undo-btn" onclick="pendingReviewAction('undo-question','${id}')">撤销上一次判断</button>
       </div>`
    : "";

  const jobNote = hasJob
    ? ""
    : `<p class="pq-note">无法找到对应录音，此来源暂不能确认写入；但仍可进行来源判断或关闭提醒。</p>`;

  return `<div class="pq-drawer" role="dialog" aria-modal="true" aria-labelledby="pq-drawer-title">
    <div class="pq-drawer-inner">
      <div class="pq-drawer-header">
        <h3 id="pq-drawer-title">${escapeHtml(pendingQuestionText(item))}</h3>
        <button type="button" class="pq-close-x" onclick="pendingReviewAction('close-question')" aria-label="关闭">✕</button>
      </div>
      <p class="pq-drawer-quote">“${escapeHtml(String(item?.quote || "（无原文）"))}”</p>

      <div class="pq-why-box">
        <div class="pq-why-row"><strong>为什么问：</strong><span>${escapeHtml(whyAsk)}</span></div>
        <div class="pq-why-row"><strong>具体未知：</strong><span>${escapeHtml(unknownDetail)}</span></div>
      </div>

      ${undoHtml}

      <div class="pq-drawer-options">${options}</div>
      ${jobNote}
      <p class="pq-drawer-foot">说明：来源判断不等于生成生活事件、不记账、不执行 Agent 命令。「关闭提醒」会从待回答中移除（可撤销）；「暂不确定／稍后处理」会继续留着。这次回答不会让模型"学会"，也不会自动改花费。</p>
      <details class="pq-tech"><summary>这条是怎么来的（技术详情）</summary>
        <p>${escapeHtml(reasonLabel)}（代码 ${escapeHtml(reasonCode)}）</p>
      </details>
      <div class="pq-drawer-actions">
        <button type="button" class="pq-ghost" onclick="pendingReviewAction('close-question')">关闭抽屉</button>
      </div>
    </div>
  </div>`;
}

function pendingFindItem(sourceId) {
  const payload = pendingReviewState?.expandedOpen && pendingReviewState?.expanded ? pendingReviewState.expanded : pendingReviewState?.payload;
  for (const group of (payload?.groups || [])) {
    for (const item of (group.items || [])) {
      if (String(item?.source_id || "") === String(sourceId)) return item;
    }
  }
  return null;
}

function pendingQuestionGroups(groups) {
  return (Array.isArray(groups) ? groups : [])
    .filter((group) => group && !HEALTH_GROUPS.has(String(group?.key || "")))
    .filter((group) => (Array.isArray(group.items) ? group.items.length : 0) > 0);
}

function pendingQuestionText(item) {
  const reason = String(item?.reason || "");
  if (NO_EVENT_REASONS.has(reason)) return "这段是你本人说的吗？";
  if (MEDIA_REASONS.has(reason)) return "这段来自影视或广告吗？";
  return "这条要你判断一下。";
}

function pendingQuestionKind(item) {
  const reason = String(item?.reason || "");
  if (NO_EVENT_REASONS.has(reason)) return "query";
  if (MEDIA_REASONS.has(reason)) return "media";
  return "general";
}

function pendingQuestionHasJob(item) {
  return Boolean(item?.job_id || item?.artifact?.job_id || item?.job);
}

function pendingQuestionCardHtml(item) {
  const id = encodeURIComponent(String(item?.source_id || "")).replace(/'/g, "%27");
  const kind = pendingQuestionKind(item);
  const reasoning = String(item?.reason_label || "系统没有给出可读原因");
  const hasJob = pendingQuestionHasJob(item);
  const primary = kind === "query"
    ? `<button type="button" class="pq-primary" onclick="pendingReviewAction('open-question','${id}')">回答</button>`
    : `<button type="button" class="pq-primary" onclick="pendingReviewAction('open-question','${id}')">回答</button>`;
  return `<article class="pq-card" data-question-id="${id}">
    <p class="pq-quote">${escapeHtml(String(item?.quote || "（无原文）"))}</p>
    <p class="pq-ask">${escapeHtml(pendingQuestionText(item))}</p>
    <div class="pq-actions">${primary}
      <button type="button" class="pq-ghost" onclick="pendingReviewAction('handled','${id}')">先不管</button>
    </div>
    ${hasJob ? "" : '<p class="pq-note">无法找到对应录音，这条暂不能确认保存；可以先关掉提醒。</p>'}
    <details class="pq-tech"><summary>这条是怎么来的（技术详情）</summary>
      <p>判定：${escapeHtml(reasoning)}（代码 ${escapeHtml(String(item?.reason || "unknown"))}）</p>
    </details>
  </article>`;
}

function pendingQuestionAreaHtml(date, state) {
  const payload = state?.expandedOpen && state?.expanded ? state.expanded : state?.payload;
  const groups = Array.isArray(payload?.groups) ? payload.groups : [];
  const items = pendingQuestionGroups(groups).flatMap((group) => group.items || []);
  if (!items.length) return "";
  const limit = state?.expandQuestions ? items.length : QUESTION_LIMIT;
  const shown = items.slice(0, limit);
  const rest = items.length - shown.length;
  return `<details class="pq-area" aria-label="需要你回答" ${pendingQuestionsOpen ? "open" : ""} ontoggle="pendingQuestionsOpen = this.open">
    <summary class="pq-head"><h3>需要你回答（${items.length}）</h3></summary>
    ${shown.map(pendingQuestionCardHtml).join("")}
    ${rest > 0 ? `<button type="button" class="pq-more" onclick="pendingReviewAction('expand-questions')">还有 ${rest} 条（含原始条目）</button>` : ""}
  </details>`;
}

let pendingQuestionsOpen = false;

function pendingHealthAreaHtml(state) {
  const payload = state?.expandedOpen && state?.expanded ? state.expanded : state?.payload;
  const groups = (Array.isArray(payload?.groups) ? payload.groups : []).filter((group) => group && HEALTH_GROUPS.has(String(group?.key || "")));
  const total = groups.reduce((sum, group) => sum + Number(group?.count || 0), 0);
  return `<details class="pq-health" ${state?.healthOpen ? "open" : ""}>
    <summary>系统健康 · 抽取异常 ${pendingReviewNumber(total)}（与"需要你回答"分开）</summary>
    <p class="pq-health-note">这些是没处理成功的原文，不需要逐条清理，系统不会自动修复；原始记录都还在。</p>
    ${groups.map((group) => pendingReviewGroupHtml(group, state)).join("")}
  </details>`;
}

function pendingReviewBodyHtml(date, state) {
  const payload = state?.expandedOpen && state?.expanded ? state.expanded : state?.payload;
  const scopeButton = `<button type="button" onclick="pendingReviewAction('scope')">${state?.allDates ? "只看本日" : "显示全部日期"}</button>`;
  const reloadButton = '<button type="button" onclick="pendingReviewAction(\'reload\')">重新读取</button>';
  if (state?.error) {
    return `<div class="pending-review-alert">抽取待确认暂时读不到：${escapeHtml(String(state.error))}</div>
      <div class="pending-review-buttons">${reloadButton}${scopeButton}</div>`;
  }
  if (!payload) {
    return '<div class="pending-review-skeleton">正在读取抽取待确认的条目……</div>';
  }
  const totals = payload.totals || {};
  const groups = Array.isArray(payload.groups) ? payload.groups : [];
  const openCounts = pendingOpenCounts(totals, groups, Boolean(state?.allDates));
  const actionableCount = state?.allDates ? openCounts.globalActionable : openCounts.scopedActionable;
  const gapCount = state?.allDates ? openCounts.globalGap : openCounts.scopedGap;
  const scopePrefix = state?.allDates ? "全库开放" : "本日开放";
  const actionableText = actionableCount === null ? "未知" : pendingReviewNumber(actionableCount);
  const gapText = gapCount === null ? "未知" : pendingReviewNumber(gapCount);
  // 【2026-10-25】把"两个数字/两个分组"讲清楚：需确认 = 要你判断，抽取异常 = 未产出可用结果。
  const help = `<p class="pending-review-help">「需确认」是抽取结果需要你来判断：先<strong>查看原文</strong>，
    确认无误后点「查看并确认」，<strong>编辑并保存</strong>才会写入人工事件（不改原 events.json、不自动入账）。
    「标记已看过」只关闭提醒，不确认内容；「忽略」同样不入账。
    <strong>缺少原始 job 的条目只能查看或忽略，不能保存确认</strong>（按钮会置灰，不当成可确认）。</p>`;
  const chips = `<div class="pending-review-chips">
    <span class="is-actionable">${scopePrefix}需确认 ${actionableText}</span>
    <span class="is-noise">${scopePrefix}抽取异常 ${gapText}</span>
    ${pendingResolutionChips(totals)}
  </div>`;
  // batch_failure 默认折叠但必须可查看；不能因为它数量大就从主界面消失。
  const visibleGroups = groups.filter(Boolean);
  const body = visibleGroups.length
    ? visibleGroups.map((group) => pendingReviewGroupHtml(group, state)).join("")
    : `<div class="workspace-lane-empty">当前范围没有需要判断的抽取条目。</div>`;
  const bulkButton = state?.allDates
    ? ""
    : `<div class="pending-review-bulk-row"><button type="button" class="pending-review-bulk" onclick="pendingReviewBulkIgnore()">忽略本日全部抽取待确认</button></div>`;
  const foot = `<p class="pending-review-foot">${state?.allDates ? "全库" : "本日"}开放需确认 ${actionableText} · 抽取异常 ${gapText}</p>`;
  const questionArea = pendingQuestionAreaHtml(date, state);
  const healthArea = pendingHealthAreaHtml(state);
  const fullList = `<details class="pq-all" ${state?.expandQuestions ? "open" : ""}>
    <summary>查看全部抽取条目（含未展开的原始记录）</summary>
    ${bulkButton}${body}
  </details>`;
  const toolsOpen = state?.healthOpen || state?.expandQuestions || state?.allDates;
  return `${questionArea}<details class="pq-tools${actionableCount || gapCount || state?.allDates ? "" : " is-empty"}" ${toolsOpen ? "open" : ""}>
    <summary>抽取详情 · 需确认 ${actionableText} · 异常 ${gapText}</summary>
    ${healthArea}${chips}${help}${fullList}${foot}
    <div class="pending-review-buttons">${reloadButton}${scopeButton}</div>
  </details>`;
}

function pendingReviewErrorText(error) {
  if (error?.status === 404) {
    return "本地服务还没有这个接口（前端代码已更新，重启 Life Logger 本地服务后生效）";
  }
  return String(error?.message || error || "未知错误");
}

function pendingReviewGroupHtml(group, state) {
  const key = String(group?.key || "other");
  const items = Array.isArray(group?.items) ? group.items : [];
  const count = Number(group?.count || 0);
  const collapsed = key === "batch_failure" || Boolean(group?.collapsed);
  const open = Boolean(state?.expandedOpen);
  const head = `<div class="pending-review-group-head"><div><span>${escapeHtml(String(group?.label || ""))}</span><strong>${pendingReviewNumber(count)}</strong></div></div>`;
  // 【2026-10-25】抽取异常的真实含义：这一组是"没产出可用结果 / 没过校验"的原文，
  // 不一定是系统故障；普通用户不必逐条清理，也不声称系统会自动修复。
  const gapHelp = key === "extraction_gap"
    ? `<p class="pending-review-gap-help">这一组是<strong>没能产出可用结果、或未通过校验</strong>的原文片段，
        原因可能是转写不清、上下文不足，也可能是抽取判断失误——<strong>未知原因就写未知</strong>，不一律归为系统故障。
        你可以<strong>查看原文与原因</strong>后自己判断；<strong>普通用户不需要逐条清理</strong>，
        系统也<strong>不会</strong>自动修复这些条目（没有后台重试）。技术细节收在可展开的详情里，旧数据保持原样。</p>`
    : "";
  if (collapsed && !open) {
    return `<section class="pending-review-group ${PENDING_REVIEW_GROUP_CLASS[key] || "is-unknown"}">${head}${gapHelp}
      <p class="pending-review-dump-note">这些是整批调用失败时的原样倾倒，与模型判断无关；原始转写仍在 <code>formal-asr/segments/*</code>，需要时重跑即可，所以默认折叠、不计入待确认。</p>
      <div class="pending-review-buttons"><button type="button" onclick="pendingReviewAction('expand')">展开查看</button></div>
    </section>`;
  }
  const truncated = Number(group?.items_truncated || 0);
  const body = items.length
    ? items.map(pendingReviewItemHtml).join("")
    : `<div class="workspace-lane-empty">这一组在当前筛选下没有可展示的条目。</div>`;
  const footer = truncated > 0 ? `<p class="pending-review-more">另有 ${pendingReviewNumber(truncated)} 条未展开。</p>` : "";
  const collapse = collapsed && open ? '<div class="pending-review-buttons"><button type="button" onclick="pendingReviewAction(\'collapse\')">收起</button></div>' : "";
  return `<section class="pending-review-group ${PENDING_REVIEW_GROUP_CLASS[key] || "is-unknown"}">${head}${gapHelp}<div class="pending-review-items">${body}</div>${footer}${collapse}</section>`;
}

const PENDING_CONFIRM_CATEGORIES = [
  ["food", "饮食"],
  ["expense", "消费"],
  ["exercise", "运动"],
  ["routine", "作息 / 日常"],
  ["todo", "待办"],
  ["idea", "想法"],
  ["work_communication", "工作 / 沟通"],
  ["other", "其他"],
];

function pendingSafeRelativePath(value) {
  const text = String(value || "").replace(/\\/g, "/");
  if (!text || text.startsWith("/") || /^[A-Za-z]:[\/]/.test(text)) return "";
  if (text.split("/").some((part) => !part || part === "." || part === "..")) return "";
  return text;
}

function pendingManualTarget(item) {
  const jobId = String(item?.artifact?.job_id || "");
  if (!jobId) return null;
  const date = String(item?.date || item?.artifact?.date || "");
  const journalMatches = [];
  for (const day of Array.isArray(journalDays) ? journalDays : []) {
    for (const entry of Array.isArray(day?.entries) ? day.entries : []) {
      const eventsPath = pendingSafeRelativePath(entry?.eventsPath);
      if (String(entry?.jobId || "") === jobId && eventsPath) {
        journalMatches.push({jobId, artifactPath: eventsPath, date: String(entry.date || date)});
      }
    }
  }
  const exact = journalMatches.find((entry) => !date || entry.date === date);
  if (exact) return exact;
  // A pending row can be dated by reported_at while the artifact belongs to the
  // previous local date. Reuse the job's sole events target rather than guessing
  // a second date directory.
  if (journalMatches.length === 1 && journalMatches[0].artifactPath.endsWith("/events.json")) {
    return journalMatches[0];
  }
  // Job-backed pending rows carry the job + context-extraction path. Derive
  // only the sibling events.json target. Never append a second date if the
  // artifact directory already ends with it.
  const raw = String(item?.artifact?.path || "").replace(/\\/g, "/");
  const prefix = `jobs/${jobId}/`;
  if (!raw.startsWith(prefix)) return null;
  const relative = pendingSafeRelativePath(raw.slice(prefix.length));
  if (!relative) return null;
  const parts = relative.split("/");
  const last = parts[parts.length - 1] || "";
  let target = "";
  if (last === "events.json") {
    target = relative;
  } else {
    const directory = parts.slice(0, -1).join("/");
    const dateSegment = /^\d{4}-\d{2}-\d{2}$/.test(parts[parts.length - 2] || "")
      ? parts[parts.length - 2]
      : date;
    if (!directory || !dateSegment) return null;
    target = /^\d{4}-\d{2}-\d{2}$/.test(parts[parts.length - 2] || "")
      ? `${directory}/events.json`
      : `${directory}/${dateSegment}/events.json`;
  }
  target = pendingSafeRelativePath(target);
  if (!target) return null;
  const targetDate = /^\d{4}-\d{2}-\d{2}$/.test(parts[parts.length - 2] || "")
    ? parts[parts.length - 2]
    : date;
  return {jobId, artifactPath: target, date: targetDate};
}

function pendingConfirmTimeValue(item) {
  const raw = String(item?.occurrence_time || "").trim();
  if (!raw) return "";
  const date = new Date(raw);
  if (Number.isNaN(date.getTime())) return "";
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", hourCycle: "h23",
  }).formatToParts(date).reduce((out, part) => ({...out, [part.type]: part.value}), {});
  return `${parts.year}-${parts.month}-${parts.day}T${parts.hour}:${parts.minute}`;
}

function pendingConfirmSourceHtml(item) {
  const artifact = item?.artifact || {};
  const pieces = [
    artifact.label ? `来源：${artifact.label}` : "",
    artifact.date ? `日期：${artifact.date}` : "",
    item?.segment_id ? `片段：${item.segment_id}` : "",
    item?.event_id ? `事件：${item.event_id}` : "",
    item?.branch ? `分支：${item.branch}` : "",
  ].filter(Boolean);
  return `<details class="pending-evidence"><summary>查看证据</summary>
    <p>${escapeHtml(String(item?.quote || "（无原文）"))}</p>
    ${pieces.length ? `<small>${pieces.map((part) => escapeHtml(part)).join(" · ")}</small>` : ""}
  </details>`;
}

function pendingConfirmFormHtml(item) {
  const encoded = encodeURIComponent(String(item?.source_id || "")).replace(/'/g, "%27");
  const target = pendingManualTarget(item);
  const amountRaw = item?.proposed_amount;
  const amountValue = amountRaw === null || amountRaw === undefined || amountRaw === ""
    ? ""
    : String(Number(amountRaw));
  const timeValue = pendingConfirmTimeValue(item);
  const categoryOptions = PENDING_CONFIRM_CATEGORIES
    .map(([value, label]) => `<option value="${escapeHtml(value)}">${escapeHtml(label)}</option>`)
    .join("");
  const unavailable = target ? "" : `<p class="pending-confirm-warning" role="note">这条来源没有可写入的 job / events.json（常见于 Watch 会话）；当前只能查看证据或复制原文，不能伪确认写入。</p>`;
  return `<form class="pending-confirm-form" onsubmit="pendingReviewConfirmSubmit(event, '${encoded}')">
    <div class="pending-confirm-head"><strong>人工确认这一条</strong><span>保存到 ${target ? escapeHtml(target.jobId) : "无可用任务"} 的 manual-events.json；不覆盖原始 events.json。</span></div>
    ${unavailable}
    <p class="pending-confirm-quote">${escapeHtml(String(item?.quote || ""))}</p>
    <label>事件标题<input id="pending-confirm-title-${encoded}" type="text" value="${escapeHtml(String(item?.proposal_title || item?.quote || ""))}" maxlength="240" ${target ? "" : "disabled"}></label>
    <label>事件类型<select id="pending-confirm-category-${encoded}" ${target ? "" : "disabled"}><option value="">请选择（不自动猜）</option>${categoryOptions}</select></label>
    <label>消费金额<input id="pending-confirm-amount-${encoded}" type="number" min="0" step="0.01" value="${escapeHtml(amountValue)}" placeholder="未识别则留空，不写 0" ${target ? "" : "disabled"}><small>仅事件类型为“消费”且由你填写时写入；留空保持未识别。</small></label>
    <label>发生时间（UTC+8）<input id="pending-confirm-time-${encoded}" type="datetime-local" value="${escapeHtml(timeValue)}" ${target ? "" : "disabled"}><small>留空 = 发生时间未知，不把讲述时间冒充发生时间。</small></label>
    <label>细分类型（可选）<input id="pending-confirm-activity-${encoded}" type="text" value="" maxlength="80" placeholder="例如 meal / workout；可留空" ${target ? "" : "disabled"}></label>
    <div class="pending-confirm-actions">
      <button type="submit" class="pending-confirm-save" ${target ? "" : "disabled"}>确认并写入人工事件</button>
      <button type="button" onclick="pendingReviewAction('cancel-confirm')">取消</button>
    </div>
    <p class="pending-confirm-scope">确认结果：只新增人工事件并记录你的确认；不会改原 events.json，不会自动改历史金额，不会调用模型。</p>
  </form>`;
}

function pendingReviewItemHtml(item) {
  const groupClass = PENDING_REVIEW_GROUP_CLASS[item?.group] || "is-unknown";
  const stateLabel = item?.state === "failed_archived"
    ? "系统归档（非人工处理）"
    : item?.state === "handled"
      ? "已看过（仅关闭提醒）"
      : item?.state === "ignored"
        ? "已忽略（仅关闭提醒）"
        : "";
  const id = encodeURIComponent(String(item?.source_id || "")).replace(/'/g, "%27");
  const reason = String(item?.reason_label || "");
  const origin = item?.original_source_id ? `来源 ${String(item.original_source_id)}` : "";
  const branch = item?.branch ? `分支 ${String(item.branch)}` : "";
  const originMeta = [origin, branch].filter(Boolean).map((part) => `<span>${escapeHtml(part)}</span>`).join("");
  return `<article class="pending-review-item ${groupClass}">
    <div class="pending-review-item-meta"><time>${escapeHtml(pendingReviewWhen(item))}</time><span>${escapeHtml(String(item?.artifact?.label || ""))}</span>${originMeta}<strong>${escapeHtml(stateLabel)}</strong></div>
    <p class="pending-review-quote">${escapeHtml(String(item?.quote || "（无原文）"))}</p>
    ${pendingReviewProposalHtml(item)}
    ${pendingConfirmSourceHtml(item)}
    <p class="pending-review-reason">${escapeHtml(reason)}</p>
    <div class="pending-review-buttons">
      <button type="button" title="查看证据并人工确认；只写当前 job 的 manual-events.json，不自动入账" onclick="pendingReviewAction('confirm','${id}')">查看并确认</button>
      ${item?.kind === "pending_event" ? "" : `<button type="button" title="填入已有口语补录入口；不会自动发送或入账" onclick="pendingReviewAction('instruction','${id}')">转入口语补录</button>`}
      <button type="button" title="复制原文，可粘贴到别处核对" onclick="pendingReviewAction('copy','${id}')">复制原文</button>
      <button type="button" title="只写审核台账：标记已看过并关闭提醒；不会标记已处理，不确认内容、不入账、不重跑模型" onclick="pendingReviewAction('handled','${id}')">标记已看过</button>
      <button type="button" title="只写审核台账：忽略并关闭提醒；不确认内容、不入账、不重跑模型" onclick="pendingReviewAction('ignored','${id}')">忽略</button>
    </div>
    ${pendingReviewState.confirmId === String(item?.source_id || "") ? pendingConfirmFormHtml(item) : ""}
  </article>`;
}

function pendingReviewNumber(value) {
  const number = Number(value);
  return Number.isFinite(number) ? number.toLocaleString("zh-CN") : "0";
}

function pendingReviewProposalHtml(item) {
  if (
    item?.kind !== "pending_event"
    && !item?.tentative
    && item?.proposed_amount == null
    && !item?.pending_reason
  ) return "";
  const amount = pendingReviewAmount(item);
  const itemName = String(item?.proposed_item || "").trim();
  const title = String(item?.proposal_title || "").trim();
  const amountText = amount ? `模型建议金额 ${amount}` : "模型建议金额未通过数值校验，暂不显示";
  const details = [amountText];
  if (itemName) details.push(`条目：${itemName}`);
  if (title) details.push(`事件：${title}`);
  return `<p class="pending-review-proposal"><strong>模型建议（待确认，非已入账消费）</strong>${details.map((part) => escapeHtml(part)).join(" · ")}</p>`;
}

function pendingReviewStatus(message, className = "") {
  const element = document.getElementById("pending-review-action-status");
  if (!element) return;
  element.textContent = message;
  element.className = `setting-status ${className}`.trim();
}

function pendingReviewWhen(item) {
  const date = String(item?.date || "").trim();
  const time = String(item?.time || "").trim();
  if (date && time) return `${date} ${time}`;
  return date || "时间未知";
}

function removePendingReviewItem(sourceId) {
  const drop = (payload) => {
    if (!payload) return;
    for (const group of payload.groups || []) {
      const before = (group.items || []).length;
      group.items = (group.items || []).filter((item) => String(item?.source_id || "") !== String(sourceId));
      group.count = Math.max(0, Number(group.count || 0) - (before - group.items.length));
      group.items_truncated = Math.max(0, Number(group.items_truncated || 0) - (before - group.items.length));
    }
  };
  drop(pendingReviewState.payload);
  drop(pendingReviewState.expanded);
}

function renderBroadcastPending(day) {
  const date = String(day && day.date || "");
  const ledger = day && day.expense_ledger;
  if (!date || !ledger || typeof ledger !== "object") return "";
  const pending = (Array.isArray(ledger.pending_broadcast) ? ledger.pending_broadcast : [])
    .filter((item) => item && item.event_id);
  if (!pending.length) return "";
  const accepted = broadcastAcceptedItems(ledger);
  const rejected = broadcastRejectedItems(day);
  const ui = broadcastUi(date);
  const decided = accepted.length + rejected.length;
  const open = ui.open;
  const statusText = ui.message
    ? `<p class="broadcast-message ${escapeHtml(ui.messageKind)}" role="status" aria-live="polite">${escapeHtml(ui.message)}</p>`
    : "";
  return `<section class="broadcast-pending" data-broadcast-strip="${escapeHtml(date)}" aria-labelledby="broadcast-heading-${escapeHtml(broadcastArg(date))}">
    <button type="button" class="broadcast-strip" aria-expanded="${open ? "true" : "false"}" aria-controls="broadcast-panel-${escapeHtml(broadcastArg(date))}" onclick="toggleBroadcastPending(this,'${broadcastArg(date)}')">
      <span class="broadcast-strip-main" id="broadcast-heading-${escapeHtml(broadcastArg(date))}">待确认 <strong>${formatCount(pending.length)} 笔</strong></span>
      <span class="broadcast-strip-meta">不计入上方总额${decided ? ` · 已处置 ${formatCount(decided)} 笔` : ""}</span>
      <span class="broadcast-strip-caret" aria-hidden="true">${open ? "▾" : "▸"}</span>
    </button>
    <div class="broadcast-panel" id="broadcast-panel-${escapeHtml(broadcastArg(date))}"${open ? "" : " hidden"}>
      ${statusText}
      <ul class="broadcast-list">
        ${pending.map((item) => broadcastRow(day, item, "pending")).join("")}
        ${accepted.map((item) => broadcastRow(day, item, "accepted")).join("")}
        ${rejected.map((item) => broadcastRow(day, item, "rejected")).join("")}
      </ul>
      <p class="broadcast-foot">「计入」会把这一笔加进当日总额；「排除」永久不计；两者都能撤销。</p>
    </div>
  </section>`;
}

function renderDailyWorkspace(day) {
  const workspaceEvents = day.workspaceEvents?.length ? day.workspaceEvents : day.timeline;
  const cleanedWorkspaceEvents = dedupeWorkspaceEvents(workspaceEvents);
  const dayTodos = eventsForCategories(cleanedWorkspaceEvents, ["todo"]);
  const todos = openTodosForJournalDay(day);
  const work = eventsForCategories(cleanedWorkspaceEvents, ["work", "communication", "work_communication"]);
  const ideas = eventsForCategories(cleanedWorkspaceEvents, ["idea"]).filter(isUsefulIdeaEvent);
  const diet = eventsForCategories(cleanedWorkspaceEvents, ["diet", "food"]);
  const exercise = eventsForCategories(cleanedWorkspaceEvents, ["exercise"]);
  const routine = eventsForCategories(cleanedWorkspaceEvents, ["routine"]);
  const expenses = eventsForCategories(cleanedWorkspaceEvents, ["expense"]);
  const firstClock = day.timeline.length ? eventClock(day.timeline[0]) : null;
  const lastClock = day.timeline.length ? eventClock(day.timeline.at(-1)) : null;
  const primaryCategory = Object.entries(day.categories)
    .filter(([, count]) => count > 0)
    .sort((a, b) => b[1] - a[1])[0];
  const primaryLabel = primaryCategory ? CATEGORY_LABELS[primaryCategory[0]] || primaryCategory[0] : "暂无";
  const completedToday = dayTodos.filter((event) => taskStateForEvent(event) === "completed").length;
  const actionCount = todos.length + work.length;
  const lifeSignals = diet.length + exercise.length + routine.length;

  const pinnedMemos = extractDayPinnedMemos(day);
  const dayExpenseTotal = typeof ExpenseDrilldown !== "undefined"
    ? ExpenseDrilldown.cnyTotal(day.expense_ledger)
    : null;

  const reviewLines = [];
  if (day.eventCount) reviewLines.push(`当日共整理出 ${formatCount(day.eventCount)} 条可用生活事件，主要集中在「${primaryLabel}」。`);
  if (firstClock && lastClock) reviewLines.push(`已记录的活动跨度从 ${firstClock} 到 ${lastClock}。`);
  if (dayTodos.length || todos.length) reviewLines.push(`当日识别到 ${formatCount(dayTodos.length)} 条待办候选；当前首页仅保留 ${formatCount(todos.length)} 条近期可执行候选${completedToday ? `，当日已完成 ${formatCount(completedToday)} 条` : ""}。`);
  if (ideas.length) reviewLines.push(`留下了 ${formatCount(ideas.length)} 条想法，可以继续交给 Agent 做项目归纳或行动拆解。`);
  if (Number.isFinite(dayExpenseTotal) && dayExpenseTotal > 0) reviewLines.push(`当前从录音中确认到的消费合计为 ${formatMoney(dayExpenseTotal)}。`);
  if (!reviewLines.length) reviewLines.push("这一天还没有足够的正式事件形成回顾；待复核内容不会被直接写进工作台。");

  return `<section class="daily-workspace">
    <div class="daily-workspace-head">
      <div><span class="journal-kicker">PERSONAL WORKSPACE</span><h4>当天工作台</h4><p>把“发生过什么”和“接下来做什么”分开。这里仅整理已形成的事件，不自动猜测任务是否完成。</p></div>
      <span class="workspace-trust-note">来自本人事件 · 可回溯到录音证据</span>
    </div>

    ${renderPinnedMemosBlock(pinnedMemos)}

    <div class="workspace-snapshot">
      <article><span>当日主线</span><strong>${escapeHtml(primaryLabel)}</strong><small>${day.eventCount ? `${formatCount(day.eventCount)} 条事件` : "尚无正式事件"}</small></article>
      <article><span>当前行动</span><strong>${formatCount(actionCount)}</strong><small>${todos.length ? `行动候选 ${formatCount(todos.length)} 条` : "近期可执行待办 + 工作/沟通"}</small></article>
      <article><span>生活信号</span><strong>${formatCount(lifeSignals)}</strong><small>作息 + 饮食 + 运动</small></article>
      <article class="${day.pendingReviewCount ? "attention" : ""}"><span>需要确认</span><strong>${formatCount(day.pendingReviewCount || 0)}</strong><small>${day.pendingReviewCount ? (day.reviewPartialCount ? `${formatCount(day.reviewPartialCount)} 条正在复核` : "与复核中心使用同一待确认队列") : "当前无需处理"}</small></article>
    </div>

    ${renderMultiDomainLedgersGrid(day)}

    <div class="workspace-board">
      ${taskWorkspaceLane(todos)}
      ${workspaceLane({title: "工作 / 沟通", eyebrow: "WORK", events: work, emptyText: "当日暂未形成工作或沟通事件。", className: "work-lane"})}
      ${workspaceLane({title: "想法池", eyebrow: "IDEAS", events: ideas, emptyText: "想到的新点子会自动沉淀到这里。", className: "idea-lane"})}
    </div>

    <div class="life-ledger">
      <section>
        <div class="life-ledger-head"><div><span>SPENDING</span><h5>消费</h5></div><strong>${Number.isFinite(dayExpenseTotal) ? formatMoney(dayExpenseTotal) : "—"}</strong></div>
        <div class="life-ledger-list">${expenses.length ? expenses.slice(0, 4).map(workspaceEventCard).join("") : '<div class="workspace-lane-empty">当日暂未识别到消费记录。</div>'}</div>
      </section>
      <section>
        <div class="life-ledger-head"><div><span>BODY</span><h5>饮食与运动</h5></div><strong>${diet.length + exercise.length}</strong></div>
        <div class="life-ledger-list">${[...diet, ...exercise].sort((a, b) => String(a?.time?.absolute || "").localeCompare(String(b?.time?.absolute || ""))).slice(0, 4).map(workspaceEventCard).join("") || '<div class="workspace-lane-empty">饮食和运动记录会集中出现在这里。</div>'}</div>
      </section>
      <section>
        <div class="life-ledger-head"><div><span>RHYTHM</span><h5>作息</h5></div><strong>${routine.length}</strong></div>
        <div class="life-ledger-list">${routine.length ? routine.slice(0, 4).map(workspaceEventCard).join("") : '<div class="workspace-lane-empty">起床、睡觉、出门等作息事件会显示在这里。</div>'}</div>
      </section>
    </div>

    ${renderDeepSeekSynthesisBlock(day)}

    <section class="daily-reflection">
      <div class="daily-reflection-head"><div><span>DAILY REVIEW</span><h5>当日回顾</h5></div><small>基于已确认事件自动生成</small></div>
      <ul>${reviewLines.map((line) => `<li>${escapeHtml(line)}</li>`).join("")}</ul>
    </section>
  </section>`;
}

function renderDeepSeekSynthesisBlock(day) {
  const stored = day.synthesis;
  const sections = stored?.narrative?.sections;
  const synthesis = stored ? {...stored, ...(sections && typeof sections === "object" ? sections : {})} : null;
  const factsOnly = stored && (stored.generation?.status === "facts_only" ||
    stored.narrative?.status === "unavailable" || stored.model_tier === "local-fallback" || stored.fallback === true);
  let reportHtml = "";
  if (factsOnly) {
    reportHtml = `<div class="deepseek-summary-box" role="status" data-synthesis-status="facts_only"><p>模型降级，本日仅展示已有记录与账本统计。</p></div>`;
  } else if (synthesis && synthesis.executive_summary) {
    const nutritionNote = synthesis.nutrition_analysis?.dietary_quality || synthesis.nutrition_analysis?.meals_summary || "";
    const fitnessNote = synthesis.fitness_analysis?.recovery_state || synthesis.fitness_analysis?.workout_highlight || "";
    const smokingNote = synthesis.habit_and_smoking_analysis?.psychological_conflict || synthesis.habit_and_smoking_analysis?.harm_reduction_guidance || "";
    const expenseNote = synthesis.expense_analysis?.spending_context || synthesis.expense_analysis?.items_summary || "";
    const work = synthesis.work_and_cognition_analysis;
    const workNote = (Array.isArray(work?.architecture_decisions) ? work.architecture_decisions : []).join("； ") ||
      (Array.isArray(work?.technical_insights) ? work.technical_insights : []).join("； ") || "";
    const energyNote = synthesis.attention_and_energy?.energy_trajectory || synthesis.attention_and_energy?.sleep_and_routine_note || "";
    const healthScore = typeof synthesis.attention_and_energy?.health_score === "number" && Number.isFinite(synthesis.attention_and_energy.health_score) ? `健康综合评分：${synthesis.attention_and_energy.health_score}/100` : "";
    const tomorrowNote = synthesis.tomorrow_advice || "";

    reportHtml = `
      <div class="deepseek-summary-box">
        ${stored.narrative?.status === "legacy_unverified" ? '<p class="hint">历史模型分析，未经事实核验。</p>' : ""}
        <strong>${escapeHtml(synthesis.executive_summary)}</strong>
      </div>
      <div class="deepseek-insights-grid">
        ${nutritionNote ? `<div class="insight-subcard"><strong>🥗 饮食营养评价</strong><p>${escapeHtml(nutritionNote)}</p></div>` : ""}
        ${fitnessNote ? `<div class="insight-subcard"><strong>🏋️ 训练与超量恢复</strong><p>${escapeHtml(fitnessNote)}</p></div>` : ""}
        ${smokingNote ? `<div class="insight-subcard"><strong style="color:#c05621;">🚬 戒烟博弈与止损指导</strong><p>${escapeHtml(smokingNote)}</p></div>` : ""}
        ${expenseNote ? `<div class="insight-subcard"><strong style="color:#b7791f;">💰 消费场景与动机剖析</strong><p>${escapeHtml(expenseNote)}</p></div>` : ""}
        ${workNote ? `<div class="insight-subcard"><strong style="color:#553c9a;">💻 架构决策与技术沉淀</strong><p>${escapeHtml(workNote)}</p></div>` : ""}
        ${energyNote || healthScore ? `<div class="insight-subcard"><strong>⚡ 精力与健康评分</strong><p>${escapeHtml([healthScore, energyNote].filter(Boolean).join(" · "))}</p></div>` : ""}
        ${tomorrowNote ? `<div class="insight-subcard"><strong>🎯 明日重点建议</strong><p>${escapeHtml(tomorrowNote)}</p></div>` : ""}
      </div>
    `;
  } else {
    reportHtml = `
      <div class="deepseek-summary-box" style="text-align:center;padding:20px;">
        <p style="margin-bottom:12px;color:#718096;font-size:13px;">当日生活事件已形成。点击下方按钮，由 DeepSeek 进行全天宏观认知复盘：</p>
        <button id="deepseek-synthesize-trigger-btn" class="setting-action primary-action" type="button" onclick="triggerDeepSeekSynthesis('${escapeHtml(day.date)}')">⚡ 生成全天宏观认知复盘</button>
      </div>
    `;
  }

  return `
    <section class="journal-deepseek-synthesis">
      <div class="deepseek-synthesis-head">
        <div>
          <span class="journal-kicker">DEEPSEEK DAY SYNTHESIS</span>
          <h4>全天宏观认知复盘</h4>
          <p class="hint">全天 Session 认知分析：餐饮营养收支、力量训练恢复、习惯博弈、消费动机与技术架构</p>
        </div>
        ${synthesis ? `<button id="deepseek-resynthesize-btn" class="quiet-button" type="button" onclick="triggerDeepSeekSynthesis('${escapeHtml(day.date)}')">🔄 重新复盘</button>` : ""}
      </div>
      ${reportHtml}
      <div class="deepseek-chat-box">
        <div class="deepseek-quick-prompts">
          <span class="quick-prompts-label">⚡ 快捷追问：</span>
          <button type="button" class="quick-prompt-pill" onclick="sendQuickDeepSeekQuestion('我今天花了多少钱？分别在什么场景下买的？', '${escapeHtml(day.date)}')">💰 今日消费明细</button>
          <button type="button" class="quick-prompt-pill" onclick="sendQuickDeepSeekQuestion('我今天抽烟了吗？抽的什么烟？戒烟博弈怎样？', '${escapeHtml(day.date)}')">🚬 抽烟与戒烟复盘</button>
          <button type="button" class="quick-prompt-pill" onclick="sendQuickDeepSeekQuestion('我今天在技术架构上有什么思考和调研？', '${escapeHtml(day.date)}')">💻 今日技术思考</button>
          <button type="button" class="quick-prompt-pill" onclick="sendQuickDeepSeekQuestion('今天的饮食热量和练腿恢复建议是什么？', '${escapeHtml(day.date)}')">🥗 营养与体能恢复</button>
        </div>
        <div id="deepseek-chat-stream" class="deepseek-chat-stream hidden"></div>
        <form id="deepseek-chat-form" class="deepseek-chat-input-row" onsubmit="handleDeepSeekChatSubmit(event, '${escapeHtml(day.date)}')">
          <input type="text" id="deepseek-chat-input" class="text-input" placeholder="💬 问问 DeepSeek：按 [/] 键快速聚焦输入…" autocomplete="off">
          <button type="submit" id="deepseek-chat-btn" class="setting-action primary-action">提问</button>
        </form>
        <span class="keyboard-shortcut-hint">提示：在页面任意位置按 <kbd>/</kbd> 键即可快速聚焦提问框，按 <kbd>Esc</kbd> 退出。支持多轮连贯追问。</span>
      </div>
    </section>
  `;
}

function renderJournalFreshness(now = Date.now()) {
  const chip = $("journal-freshness");
  if (!chip) return;
  if (!journalLastSuccessAt) {
    chip.textContent = journalRefreshError ? "尚未成功更新" : "等待首次更新";
    chip.className = `journal-freshness ${journalRefreshError ? "error" : "checking"}`;
    chip.title = journalRefreshError || "等待第一次成功读取日记";
    return;
  }
  if (journalReadStale) {
    const ageLabel = journalFreshnessAgeLabel(journalReadStale.at || now, now);
    chip.textContent = `旧快照 · 正在重算 · ${ageLabel}`;
    chip.className = "journal-freshness stale";
    chip.title = (journalReadStale.note || "服务端返回了上一次的快照，后台正在重新统计")
      + "（数据照常可用；不是失败，也不是空记录）";
    return;
  }
  const ageMs = Math.max(0, now - journalLastSuccessAt);
  const ageLabel = journalFreshnessAgeLabel(journalLastSuccessAt, now);
  if (journalRefreshError) {
    chip.textContent = `刷新失败 · ${ageLabel}`;
    chip.className = "journal-freshness warning";
    chip.title = journalRefreshError;
  } else if (ageMs >= 300_000) {
    chip.textContent = `数据较旧 · ${ageLabel}`;
    chip.className = "journal-freshness stale";
    chip.title = "距离上次成功读取已超过 5 分钟";
  } else {
    chip.textContent = ageLabel;
    chip.className = "journal-freshness ready";
    chip.title = "日记数据已成功读取";
  }
}

function renderMultiDomainLedgersGrid(day) {
  const nutrition = extractDayNutritionLedger(day);
  const fitness = extractDayFitnessLedger(day);
  const smoking = extractDaySmokingLedger(day);
  const expense = extractDayExpenseLedger(day);
  const work = extractDayWorkCognitionLedger(day);

  // 1. Nutrition Card
  const macros = nutrition.totals || {};
  const consumedCount = Number(nutrition.consumed_record_count ?? Object.values(nutrition.meals || {}).flat().length);
  const calorieCoverage = nutritionCoverageFor(nutrition, "calories_kcal");
  const caloriesComplete = nutrition.totals_complete === true
    || (consumedCount > 0 && calorieCoverage.unknown === 0);
  const caloriesLabel = consumedCount === 0
    ? "无摄入记录"
    : caloriesComplete
      ? `${macros.calories_kcal} kcal`
      : (calorieCoverage.known > 0 ? `已知 ${macros.calories_kcal} kcal` : "热量未知");
  const coverageHint = consumedCount > 0 && calorieCoverage.total > 0
    ? `热量覆盖 ${calorieCoverage.known}/${calorieCoverage.total} 项`
    : "";
  const mealsList = [];
  const mealLabels = { breakfast: "🌅 早餐", lunch: "☀️ 午餐", dinner: "🌙 晚餐", supplements: "🍵 调理与保健", snacks: "🍎 加餐/零食" };
  Object.entries(nutrition.meals || {}).forEach(([key, items]) => {
    if (!items.length) return;
    items.forEach((m) => {
      const lowConfidence = m.estimate_confidence === "low";
      const calories = nutritionKnownNumber(m.calories_kcal);
      const knownCalories = m.calories_known === true || (m.calories_known !== false && calories !== null);
      const calText = knownCalories && calories !== null
        ? `${calories} kcal${lowConfidence ? "（估算）" : ""}`
        : "热量未知";
      const weightText = m.weight_g ? `${m.weight_g}g` : (m.portion || "");
      const meta = [calText, weightText].filter(Boolean).join(" · ");
      mealsList.push(`
        <div class="meal-item-row">
          <div class="meal-item-title">
            <span>${escapeHtml(mealLabels[key] || key)}：${escapeHtml(m.item || m.title)}</span>
            <small>${escapeHtml(meta)}</small>
          </div>
          ${m.notes ? `<div class="meal-item-notes">${escapeHtml(m.notes)}</div>` : ""}
        </div>
      `);
    });
  });
  const purchaseRows = (nutrition.purchases || []).map((m) => `
    <div class="meal-item-row">
      <div class="meal-item-title"><span>🛒 ${escapeHtml(m.item || m.title)}</span><small>购买记录 · 不计入摄入</small></div>
    </div>
  `).join("");
  const otherRows = (nutrition.other_records || []).map((m) => `
    <div class="meal-item-row">
      <div class="meal-item-title"><span>🍽️ ${escapeHtml(m.item || m.title)}</span><small>备餐/摄入未确认 · 不计入营养合计</small></div>
    </div>
  `).join("");
  const macroStat = (key, label, unit) => {
    const coverage = nutritionCoverageFor(nutrition, key);
    const value = nutritionKnownNumber(macros[key]);
    if (!coverage.total) return `<div class="macro-stat"><span>${label}</span><strong>—</strong><small>无摄入记录</small></div>`;
    if (coverage.known === 0 || value === null) return `<div class="macro-stat"><span>${label}</span><strong>未知</strong><small>0/${coverage.total} 项</small></div>`;
    const prefix = coverage.unknown > 0 ? "已知 " : "";
    return `<div class="macro-stat"><span>${label}</span><strong>${prefix}${value}${unit}</strong><small>${coverage.known}/${coverage.total} 项</small></div>`;
  };

  const nutritionHtml = `
    <section class="journal-nutrition-card">
      <div class="ledger-card-head">
        <div><span class="journal-kicker">NUTRITION & MACROS</span><h4>今日餐饮与营养摄入</h4></div>
        <span class="ledger-card-badge">${escapeHtml(caloriesLabel)}</span>
      </div>
      ${coverageHint ? `<p class="hint">${escapeHtml(coverageHint)}；未知项不会被当成 0，购买记录不计入摄入。</p>` : ""}
      <div class="macros-strip">
        ${macroStat("calories_kcal", "能量", " kcal")}
        ${macroStat("carbohydrates_g", "碳水", " g")}
        ${macroStat("protein_g", "蛋白质", " g")}
        ${macroStat("fat_g", "脂肪", " g")}
        ${macroStat("sodium_mg", "钠含量", " mg")}
      </div>
      <div class="meals-list">${mealsList.length ? mealsList.join("") : '<div class="workspace-lane-empty">当日暂无已确认的摄入记录。</div>'}${purchaseRows ? `<div class="workspace-lane-empty">购买不等于摄入</div>${purchaseRows}` : ""}${otherRows}</div>
    </section>
  `;

  // 2. Fitness Card
  const workoutsList = (fitness.workouts || []).map((w) => {
    const parts = (w.body_parts || []).map((bp) => `<span class="muscle-tag">${escapeHtml(bp)}</span>`).join("");
    const feeling = w.feeling ? `<span class="feeling-tag">${escapeHtml(w.feeling)}</span>` : "";
    return `
      <div class="workout-item-row">
        <div class="workout-item-title">
          <span>${escapeHtml(w.title)}</span>
          <small>${escapeHtml(w.time_of_day || "")}</small>
        </div>
        <div style="margin: 4px 0;">${parts}${feeling}</div>
        ${w.quote ? `<div class="workout-item-notes">${escapeHtml(w.quote)}</div>` : ""}
      </div>
    `;
  }).join("");

  const sessionList = (fitness.sessions || []).map((session) => {
    const actions = (session.actions || []).map((a) => {
      const bits = [];
      if (a.sets) bits.push(`${a.sets} 组`);
      if (a.reps) bits.push(`${a.reps} 次`);
      if (a.weight_value) bits.push(`${a.weight_value}${a.weight_unit || "kg"}${a.side === "per_side" ? "/边" : ""}`);
      if (a.duration_seconds) bits.push(`${Math.round(a.duration_seconds / 60)} 分钟`);
      return `<div class="workout-item-notes">• ${escapeHtml(a.name || "未命名动作")}${bits.length ? "：" + escapeHtml(bits.join(" · ")) : ""}</div>`;
    }).join("");
    const total = session.total_duration_seconds ? `${Math.round(session.total_duration_seconds / 60)} 分钟` : "时长未知";
    const verification = session.verification || {};
    const badge = verification.status === "confirmed" ? "✅ 现场与回顾一致"
      : verification.status === "review_only" ? "🔁 仅来自回顾"
      : verification.status === "differences" ? "⚠️ 回顾与现场有差异" : "";
    const diffs = (verification.differences || []).map((d) => `<div class="workout-item-notes" style="color:#c05621;">• ${escapeHtml(d)}</div>`).join("");
    return `
      <div class="workout-item-row">
        <div class="workout-item-title">
          <span>健身会话 ${escapeHtml((session.start || "").slice(11, 16))}–${escapeHtml((session.end || "").slice(11, 16))}</span>
          <small>${escapeHtml(total)}</small>
        </div>
        <div style="margin:4px 0;">${badge ? `<span class="feeling-tag">${escapeHtml(badge)}</span>` : ""}</div>
        ${actions}
        ${diffs}
      </div>
    `;
  }).join("");

  const recoveryNotes = (fitness.recovery_reflections || []).map((r) => `
    <div class="workout-item-row" style="background:#fffaf0;border-color:#feebc8;">
      <div class="workout-item-title"><span style="color:#c05621;">🛌 恢复反馈：${escapeHtml(r.title)}</span></div>
      <div class="workout-item-notes">${escapeHtml(r.feeling || r.quote || "")}</div>
    </div>
  `).join("");

  const targetPartsText = (fitness.target_body_parts || []).join("、") || "力量/有氧";
  const fitnessHtml = `
    <section class="journal-fitness-card">
      <div class="ledger-card-head">
        <div><span class="journal-kicker">WORKOUT & RECOVERY</span><h4>健身锻炼与身体恢复</h4></div>
        <span class="ledger-card-badge">${escapeHtml(targetPartsText)}</span>
      </div>
      <div class="workout-list">${(sessionList || workoutsList) || recoveryNotes ? (sessionList || workoutsList) + recoveryNotes : '<div class="workspace-lane-empty">当日暂无健身运动记录。</div>'}</div>
    </section>
  `;

  // 3. Smoking Card (only rendered if records exist)
  let smokingHtml = "";
  if (smoking.has_records) {
    const conflictHtml = smoking.psychology?.has_relapse_conflict ? `
      <div class="smoking-conflict-banner">
        <strong>⚠️ 戒烟心理博弈与止损线：</strong>
        <div>承诺：${escapeHtml(smoking.psychology?.commitments?.join("、") || "")} ➜ 妥协：${escapeHtml(smoking.psychology?.compromises?.join("、") || "")}</div>
        ${smoking.psychology?.cutoff_rule ? `<div style="margin-top:2px;font-weight:600;color:#c53030;">🛑 止损执行：${escapeHtml(smoking.psychology.cutoff_rule)}</div>` : ""}
      </div>
    ` : "";

    const sessionRows = (smoking.sessions || []).map((s) => `
      <div class="workout-item-row" style="background:#fffaf0;border-color:#feebc8;">
        <div class="workout-item-title">
          <span>${s.brand ? `🚬 ${escapeHtml(s.brand)}` : "🚬 抽烟"} · ${escapeHtml(s.title)}</span>
          <small>${escapeHtml(s.clock || "")}</small>
        </div>
        ${s.feeling ? `<div style="margin:2px 0;"><span class="feeling-tag">${escapeHtml(s.feeling)}</span></div>` : ""}
        ${s.quote ? `<div class="workout-item-notes">${escapeHtml(s.quote)}</div>` : ""}
      </div>
    `).join("");

    smokingHtml = `
      <section class="journal-smoking-card">
        <div class="ledger-card-head">
          <div><span class="journal-kicker">HABIT & SMOKING</span><h4>戒烟习惯与心理博弈</h4></div>
          <span class="ledger-card-badge">${formatCount(smoking.total_count)} 次 · ${escapeHtml(smoking.brands.join("、") || "香烟")}</span>
        </div>
        ${conflictHtml}
        <div class="workout-list">${sessionRows}</div>
      </section>
    `;
  }

  // 4. Expense drilldown lives beside the day summary; keep this grid free of a duplicate total.
  const expenseHtml = "";

  // 5. Work Cognition Card (only rendered if records exist)
  let workHtml = "";
  if (work.has_records) {
    const decisionsHtml = (work.decisions || []).map((d) => `
      <div class="work-bullet-item">
        <strong>⚡ 架构决策：</strong>${escapeHtml(d)}
      </div>
    `).join("");

    const insightsHtml = (work.insights || []).map((ins) => `
      <div class="work-bullet-item" style="background:#f7fafc;">
        <strong style="color:#2b6cb0;">💡 技术探索：</strong>${escapeHtml(ins)}
      </div>
    `).join("");

    workHtml = `
      <section class="journal-work-card">
        <div class="ledger-card-head">
          <div><span class="journal-kicker">SYSTEM & COGNITION</span><h4>技术决策与架构复盘</h4></div>
          <span class="ledger-card-badge">${work.decisions.length + work.insights.length} 项沉淀</span>
        </div>
        <div class="meals-list">${decisionsHtml}${insightsHtml}</div>
      </section>
    `;
  }

  return `<div class="multi-ledgers-grid">${nutritionHtml}${fitnessHtml}${smokingHtml}${expenseHtml}${workHtml}</div>`;
}

function renderPendingReviewBlock(date) {
  const target = String(date || "");
  const payloadDate = String(pendingReviewState.payload?.scope?.date || "");
  const scopeMatches = pendingReviewState.allDates ? !payloadDate : payloadDate === target;
  if (!pendingReviewState.payload || Date.now() - pendingReviewState.loadedAt > 60_000 || !scopeMatches) {
    refreshPendingReviewBlock(target);
  }
  return `<section id="pending-review-block" class="pending-review" data-date="${escapeHtml(target)}" aria-label="抽取待确认">
    <p class="pending-review-scope">${escapeHtml(PENDING_REVIEW_SCOPE_HINT)}</p>
    <div id="pending-review-body" class="pending-review-body">${pendingReviewBodyHtml(target, pendingReviewState)}</div>
    <p id="pending-review-action-status" class="setting-status" aria-live="polite"></p>
  </section>`;
}

function renderSaidMetric(day) {
  const funnel = journalFunnelCache.get(day.date);
  if (!funnel) {
    loadJournalFunnel(day.date);
    return `<div id="journal-metric-said" title="这一天转写出来多少句；对比「事件轴」就是留存率"><span>说了</span><strong>…</strong></div>`;
  }
  const said = Number(funnel.said_sentences) || 0;
  const kept = Number(day.eventCount) || 0;
  const pct = said > 0 ? (kept / said) * 100 : 0;
  // 留存率低于 1% 标红：这不是"也许可以更好"，这是系统在丢东西。
  const cls = said > 0 && pct < 1 ? "needs-review" : "";
  const title = `说了 ${said.toLocaleString("zh-CN")} 句 / ${(funnel.said_chars || 0).toLocaleString("zh-CN")} 字`
    + `（${funnel.said_segments || 0} 段录音）→ 上事件轴 ${kept} 条，留存 ${pct.toFixed(2)}%`;
  return `<div id="journal-metric-said" class="${cls}" title="${escapeHtml(title)}"><span>说了</span><strong>${said.toLocaleString("zh-CN")}</strong><small>留存 ${pct.toFixed(1)}%</small></div>`;
}

function retryJournalCanonical(date) {
  const day = journalDays.find((item) => item.date === date);
  if (!day) return;
  journalCanonicalCache.delete(date);
  journalActivityTimelineCache.delete(date);
  day._canonicalAttempted = false;
  day._activityTimelineAttempted = false;
  day.activityTimeline = null;
  ensureJournalDaySynthesis(day, {force: true});
  ensureJournalActivityTimeline(day, {force: true});
}

function retryKanbanDay() {
  const day = journalDays.find((item) => item.date === selectedJournalDate);
  if (!day) return;
  day.canonical = false;
  day._canonicalAttempted = false;
  journalCanonicalCache.delete(day.date);
  renderKanban();
}

function selectKanbanDate(value) {
  const date = String(value || "").slice(0, 10);
  if (!/^\d{4}-\d{2}-\d{2}$/.test(date)) return;
  journalSelectionInitialized = true;
  journalUserSelectedDate = String(date || "");
  journalDateGeneration += 1;
  selectedJournalDate = date;
  writeStorageVersioned("localStorage", "life-logger-journal-date", date);
  renderKanban();
  // Kanban and journal share selectedJournalDate. If a timeline is mounted,
  // keep its rendered content in sync instead of leaving the iframe on the
  // previous date.
  if (document.querySelector(".day-timeline-frame")) renderJournalNavigation();
}

function sendQuickDeepSeekQuestion(question, date) {
  const input = document.getElementById("deepseek-chat-input");
  const form = document.getElementById("deepseek-chat-form");
  if (input) {
    input.value = question;
    input.focus();
  }
  if (form) {
    if (typeof form.requestSubmit === "function") {
      form.requestSubmit();
    } else {
      const submitEvent = new Event("submit", { cancelable: true, bubbles: true });
      form.dispatchEvent(submitEvent);
    }
  }
}

function setJournalLoading(isBusy, message = "") {
  const root = $("journal-dashboard");
  const status = $("journal-load-status");
  if (root) root.setAttribute("aria-busy", isBusy ? "true" : "false");
  if (status && message) status.textContent = message;
  const refresh = $("journal-refresh");
  if (refresh) refresh.disabled = Boolean(isBusy);
}

function sharedTimelineRead(path, loader, options = {}) {
  return typeof LifeTimelineData !== "undefined" ? LifeTimelineData.read(path, loader, options) : loader();
}

function toggleBroadcastPending(button, date) {
  const ui = broadcastUi(String(date));
  ui.open = !ui.open;
  const panel = document.getElementById(`broadcast-panel-${broadcastArg(date)}`);
  if (panel) panel.hidden = !ui.open;
  if (button) {
    button.setAttribute("aria-expanded", ui.open ? "true" : "false");
    const caret = button.querySelector(".broadcast-strip-caret");
    if (caret) caret.textContent = ui.open ? "▾" : "▸";
  }
}

function updateSourceInboxBadge() {
  if (typeof updateReviewTabCounts === "function") updateReviewTabCounts();
}

const CANONICAL_EVENT_VIEW_SCHEMA = "event-view-v1";
const CANONICAL_EXPENSE_POLICY_VERSIONS = new Set(["expense-policy-v2"]);
const PENDING_REVIEW_GROUP_CLASS = {
  needs_review: "is-actionable",
  extraction_gap: "is-noise",
  other: "is-unknown",
  batch_failure: "is-dump",
};
const PENDING_REVIEW_SCOPE_HINT = "模型判「无事件」但像主动指令，或模型给了待确认建议；标记只影响列表，不会入账、不会自动重跑模型。";
const broadcastUiState = new Map();
const journalCanonicalCache = new Map();
let journalCanonicalGeneration = 0;
const journalActivityTimelineCache = new Map();
let journalActivityTimelineGeneration = 0;
const journalFunnelCache = new Map();
const journalScenesCache = new Map();
const pendingReviewState = {
  payload: null,
  expanded: null,
  expandedOpen: false,
  allDates: false,
  error: null,
  loadedAt: 0,
  inflight: null,
  confirmId: null,
  confirmSaving: null,
};
const BROADCAST_ENDPOINT = "/api/expense/pending-broadcast";
const dayChatHistories = new Map();

async function setKanbanTaskStatus(taskKey, status) {
  const statusNode = $("kanban-status");
  const announce = (message, tone = "") => {
    if (!statusNode) return;
    statusNode.textContent = message;
    statusNode.className = `setting-status${tone ? ` ${tone}` : ""}`;
  };
  if (taskKey.startsWith("oc:")) return setOpenClawTaskStatus(taskKey.slice(3), status, announce);
  const event = kanbanEventForKey(taskKey);
  if (!event || String(event.event_id || "").startsWith("evt_cand_")) {
    announce("候选事项不能直接修改状态。", "warning");
    return false;
  }
  try {
    const payload = await api("/api/tasks/status", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        task_key: taskKey,
        status,
        title: event.title || "",
        event_id: event.event_id || null,
        job_id: event._isLiveWatch ? null : (event._jobId || null),
        artifact_path: event._artifactPath || event.artifact_path || null,
      }),
    });
    if (payload.task) taskStates.set(taskKey, payload.task);
    renderKanban();
    // 【2026-11-06】本地写成功 ≠ OpenClaw 也写成功。同步失败不能装作成功。
    const sync = payload.openclaw || null;
    if (sync && sync.synced === false) {
      announce(
        `已在本地保存，但未同步到 OpenClaw${sync.queued ? "（已排队，恢复后自动补）" : ""}：${sync.error || "未知原因"}`,
        "warning",
      );
      return true;
    }
    announce(status === "dismissed" ? "已删除，可以在看板下方恢复；原始录音和记录都还在。" : "状态已更新。");
    return true;
  } catch (error) {
    announce(`修改失败：${error.message}`, "error");
    return false;
  }
}

async function setOpenClawTaskStatus(taskId, status, announce) {
  const payload = openclawPoolState.payload;
  const groups = ["wechat_matters", "life_logger_todos", "general_todos"];
  const task = groups.flatMap((key) => payload?.[key] || []).find((item) => item.id === taskId);
  if (!task) {
    announce("这条任务已经不在列表里，请刷新。", "warning");
    return false;
  }
  try {
    const result = await api("/api/openclaw/tasks/status", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({task_id: taskId, status, revision: task.revision ?? null}),
    });
    Object.assign(task, result.task?.task || result.task || {}, {status});
    if (status === "dismissed") groups.forEach((key) => {
      if (payload[key]) payload[key] = payload[key].filter((item) => item.id !== taskId);
    });
    renderKanban();
    announce(status === "dismissed" ? "已删除，OpenClaw 里也标记为已删除。" : "状态已更新，并同步到 OpenClaw。");
    return true;
  } catch (error) {
    await refreshOpenClawPool(true);
    renderKanban();
    announce(`同步 OpenClaw 失败，已刷新任务列表，请再试一次：${error.message}`, "error");
    return false;
  }
}

async function toggleKanbanTodo(input) {
  const taskKey = String(input?.dataset?.kanbanTaskKey || "");
  if (!taskKey) return;
  const desired = input.checked;
  input.disabled = true;
  const ok = await setKanbanTaskStatus(taskKey, desired ? "completed" : "pending");
  if (!ok) input.checked = !desired;
  input.disabled = false;
}

async function promoteKanbanIdea(button) {
  const taskKey = String(button?.dataset?.kanbanPromoteKey || "");
  if (!taskKey) return;
  button.disabled = true;
  const ok = await setKanbanTaskStatus(taskKey, "pending");
  if (!ok) button.disabled = false;
}

async function triggerDeepSeekSynthesis(date) {
  const btn = document.getElementById("deepseek-resynthesize-btn") || document.getElementById("deepseek-synthesize-trigger-btn");
  if (btn) {
    btn.disabled = true;
    btn.textContent = "正在深度复盘中…";
  }
  try {
    const res = await api("/api/daily/synthesize", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ date, force: true }),
      timeoutMs: 180000,
    });
    if (res) {
      const day = journalDays.find((d) => d.date === date);
      if (day) {
        day.synthesis = res;
        day._synthesisLoaded = true;
        renderJournalDay(day);
      }
    }
  } catch (err) {
    reportError(err, { operation: "生成认知复盘" });
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function handleDeepSeekChatSubmit(event, date) {
  event.preventDefault();
  const input = document.getElementById("deepseek-chat-input");
  const btn = document.getElementById("deepseek-chat-btn");
  const stream = document.getElementById("deepseek-chat-stream");
  const question = String(input?.value || "").trim();
  if (!question) {
    if (input) input.setAttribute("aria-invalid", "true");
    return;
  }
  if (input) input.setAttribute("aria-invalid", "false");

  const history = dayChatHistories.get(date) || [];
  if (input) input.value = "";
  if (btn) btn.disabled = true;

  if (stream) {
    stream.classList.remove("hidden");
    const userBubble = document.createElement("div");
    userBubble.className = "chat-bubble user-bubble";
    userBubble.textContent = question;
    stream.appendChild(userBubble);

    const assistantBubble = document.createElement("div");
    assistantBubble.className = "chat-bubble assistant-bubble typing";
    assistantBubble.textContent = "DeepSeek 正在结合全天事实思考回答…";
    stream.appendChild(assistantBubble);
    stream.scrollTop = stream.scrollHeight;

    try {
      const res = await api("/api/daily/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ date, question, history }),
        timeoutMs: 180000,
      });
      assistantBubble.classList.remove("typing");
      assistantBubble.textContent = res.answer || "未返回有效回答。";
      history.push({ role: "user", content: question });
      history.push({ role: "assistant", content: res.answer || "" });
      dayChatHistories.set(date, history);
    } catch (err) {
      assistantBubble.classList.remove("typing");
      assistantBubble.textContent = "提问失败：" + err.message;
    } finally {
      if (btn) btn.disabled = false;
      stream.scrollTop = stream.scrollHeight;
      if (input) input.focus();
    }
  }
}

async function ensureJournalDaySynthesis(day, {force = false} = {}) {
  if (!day?.date) return;
  // 【2026-10-29 预取 202 修复】导航重绘会重建 day 对象：如果调用方手里是旧对象，
  // 响应会因为 !journalDays.includes(day) 被丢掉，角标就永远停在“读取中”。
  // 一律以当前 journalDays 里的对象为准（同日只可能有一个）。
  const liveDay = (journalDays || []).find((d) => d && d.date === day.date);
  if (liveDay && liveDay !== day) day = liveDay;
  if (day.canonical && !force) return;
  if (day._canonicalLoading) return day._canonicalLoading;
  if (day._canonicalAttempted && !force) return;
  day._canonicalAttempted = true;
  const generation = journalCanonicalGeneration;
  day._canonicalLoading = (async () => {
    const cached = journalCanonicalCache.get(day.date);
    try {
      const payload = cached && !force
        ? cached.payload
        : await sharedTimelineRead(
            `/api/daily?date=${encodeURIComponent(day.date)}&scope=provisional&view=display`,
            () => api(`/api/daily?date=${encodeURIComponent(day.date)}&scope=provisional&view=display`, {},
              {silent: true, operation: "读取统一日记口径"}),
            {force, maxAge: day.date === shanghaiDate(0) ? 15000 : 300000},
          );
      if (!payload) throw new Error("空响应");
      // Invalidation (force refresh / fingerprint replacement) increments this
      // token. A response started before invalidation must not repopulate the
      // cache or repaint an old day, even if it arrives last.
      if (generation !== journalCanonicalGeneration) return;
      const latestDay = (journalDays || []).find((d) => d && d.date === day.date);
      if (latestDay && latestDay !== day) day = latestDay;
      if (!journalDays.includes(day)) return;
      hydrateJournalDayFromCanonical(day, payload);
      // 【P0 修】canonical 就绪后必须清掉“读取较慢/仍在读取”的过期状态，否则父页面永久转圈。
      day._loadingSince = 0;
      day.canonicalPending = null;
      day._pendingAttempts = 0;
      // 【2026-10-29 预取 202 修复】canonical 已就绪：不再需要后台重试。
      day._backgroundPrefetch = false;
      day._initialPrefetchQueued = false;
      if (day._slowTimer) { clearTimeout(day._slowTimer); day._slowTimer = null; }
      journalCanonicalCache.set(day.date, {payload, loadedAt: Date.now()});
      // Repaint date navigation and the selected day together so a chip never
      // keeps a stale derived count after the canonical response arrives.
      if (journalDays.includes(day)) {
        renderJournalNavigation();
        // 只有“当前仍选中这一天”时才重绘当天视图，旧日期响应不得覆盖新日期。
        if (selectedJournalDate === day.date) renderJournalDay(day);
        renderReviewDashboard();
        if (typeof activeView === "string" && activeView === "kanban") renderKanban();
      }
    } catch (error) {
      if (generation === journalCanonicalGeneration && typeof LifeTimelineData !== "undefined") {
        LifeTimelineData.clear(`/api/daily?date=${encodeURIComponent(day.date)}`);
      }
      const latestErrDay = (journalDays || []).find((d) => d && d.date === day.date);
      if (latestErrDay && latestErrDay !== day) day = latestErrDay;
      if (day.canonical) {
        return;
      }
      day.canonical = false;
      if (isPendingApiError(error)) {
        // 202/read_status=pending 是“还在算”，不是失败：保留 pending 语义并安排有界重试。
        day.canonicalError = null;
        const info = error.pendingInfo || pendingReadInfo({read_status: "pending"}, 202);
        day.canonicalPending = {...info, attempts: Number(day._pendingAttempts || 0)};
        // 【2026-10-29 预取 202 修复】today/yesterday 的后台预取遇到 202 时，旧代码只在
        // “当前选中日期”才安排重试，预取日期于是永不重试，角标退化成“未加载 · 选择查看”。
        // 现在预取日期复用同一套有界退避；background 只表示“不得重绘当前视图”。
        const backgroundPrefetch = Boolean(day._initialPrefetchQueued || day._backgroundPrefetch);
        if (selectedJournalDate === day.date || backgroundPrefetch) {
          scheduleJournalPendingRetry(day, info,
            {background: backgroundPrefetch && selectedJournalDate !== day.date});
        }
      } else {
        day.canonicalPending = null;
        const rawCanonicalError = String(error?.message || error || "统一口径读取失败");
        day.canonicalErrorRaw = rawCanonicalError;
        // 兼容既有测试的源码切片（切片里可能没有 helper）：拿不到就退回原文，行为不变。
        day.canonicalError = typeof humanizeCanonicalError === "function"
          ? humanizeCanonicalError(rawCanonicalError)
          : rawCanonicalError;
      }
      if (selectedJournalDate === day.date || day._initialPrefetchQueued) {
        // 后台预取也要刷新角标（否则 pending 状态不可见），但绝不重绘当前视图。
        renderJournalNavigation();
        if (selectedJournalDate === day.date && typeof activeView === "string" && activeView === "kanban") renderKanban();
      }
    } finally {
      day._canonicalLoading = null;
      const latestFinallyDay = (journalDays || []).find((d) => d && d.date === day.date);
      if (latestFinallyDay) latestFinallyDay._canonicalLoading = null;
    }
  })();
  return day._canonicalLoading;
}

async function ensureJournalActivityTimeline(day, {force = false} = {}) {
  if (!day?.date) return null;
  if (day.activityTimeline && day.activityTimeline.status === "ready" && !force) {
    return day.activityTimeline;
  }
  if (day._activityTimelineLoading && !force) {
    return day._activityTimelineLoading;
  }
  if (day._activityTimelineAttempted && !force) {
    return day.activityTimeline || null;
  }
  day._activityTimelineAttempted = true;
  const targetDate = day.date;
  const generation = journalActivityTimelineGeneration;
  const dateGen = journalDateGeneration;

  day._activityTimelineLoading = (async () => {
    const cached = journalActivityTimelineCache.get(day.date);
    try {
      const payload = cached && !force
        ? cached.payload
        : await sharedTimelineRead(
            `/api/activity-timeline?date=${encodeURIComponent(day.date)}`,
            () => api(
              `/api/activity-timeline?date=${encodeURIComponent(day.date)}`,
              {signal: viewAbortSignal("today")},
              {silent: true, operation: "读取活动时间线"}
            ),
            {force, maxAge: day.date === shanghaiDate(0) ? 15000 : 300000}
          );
      if (!payload) return null;
      // Stale / race guard: user switched away from this date
      if (generation !== journalActivityTimelineGeneration || dateGen !== journalDateGeneration) {
        if (selectedJournalDate !== targetDate) return null;
      }
      const latestActDay = (journalDays || []).find((d) => d && d.date === targetDate);
      if (latestActDay && latestActDay !== day) day = latestActDay;
      if (!journalDays.includes(day)) return null;

      if (payload && payload.schema === "activity-timeline-v1") {
        journalActivityTimelineCache.set(day.date, {payload, loadedAt: Date.now()});
        day.activityTimeline = payload;
      }

      // [Q547 补全] 无论 ready 还是 error/partial，只要数据返回且是当前查看日，
      // 都触发视图重绘，使 dayPrimaryCount() 中的活动失败/降级横幅能够即时呈现，
      // 杜绝 503/error 时主页被误判为 0 行空态。
      if (payload && (payload.status === "ready" || payload.status === "error" || (Array.isArray(payload.errors) && payload.errors.length > 0))) {
        if (selectedJournalDate === day.date) {
          renderJournalDay(day);
        }
        renderJournalNavigation();
      }
      return payload;
    } catch (error) {
      if (error?.aborted) return null;
      if (generation === journalActivityTimelineGeneration && typeof LifeTimelineData !== "undefined") {
        LifeTimelineData.clear(`/api/activity-timeline?date=${encodeURIComponent(day.date)}`);
      }
      // [Q547 补全] 网络 503 或抛错时，显式为 day.activityTimeline 注入 error 态，
      // 避免主页因 activityTimeline 为空而降级为默认的「0 件事」假空态。
      if (!day.activityTimeline) {
        day.activityTimeline = {
          schema: "activity-timeline-v1",
          date: day.date,
          status: "error",
          activities: [],
          errors: [String(error?.message || error || "活动接口读取失败")]
        };
        if (selectedJournalDate === day.date) {
          renderJournalDay(day);
        }
        renderJournalNavigation();
      }
      return null;
    } finally {
      day._activityTimelineLoading = null;
      const latestFinallyActDay = (journalDays || []).find((d) => d && d.date === targetDate);
      if (latestFinallyActDay) latestFinallyActDay._activityTimelineLoading = null;
    }
  })();
  return day._activityTimelineLoading;
}

async function refreshPendingReviewBlock(date, options = {}) {
  const force = Boolean(options.force);
  const target = String(date || currentPendingReviewDate() || "");
  const withCollapsed = pendingReviewState.expandedOpen;
  const key = `${target}|${pendingReviewState.allDates}|${withCollapsed}`;
  if (pendingReviewState.inflight === key && !force) return;
  const requestId = (pendingReviewState.requestId || 0) + 1;
  pendingReviewState.requestId = requestId;
  pendingReviewState.inflight = key;
  const query = new URLSearchParams();
  if (!pendingReviewState.allDates && target) query.set("date", target);
  if (withCollapsed) query.set("include_collapsed", "1");
  query.set("limit", withCollapsed ? "10" : "30");
  try {
    const payload = await api(`/api/pending-sources?${query.toString()}`, {}, {silent: true, operation: "读取抽取待确认"});
    // A user can switch date/scope while an older request is still in flight.
    // Never let that response overwrite the newer block.
    if (requestId !== pendingReviewState.requestId) return;
    const currentDate = currentPendingReviewDate();
    if (!pendingReviewState.allDates && target && currentDate && target !== currentDate) return;
    if (withCollapsed) pendingReviewState.expanded = payload;
    else pendingReviewState.payload = payload;
    pendingReviewState.error = null;
    pendingReviewState.loadedAt = Date.now();
  } catch (error) {
    if (requestId !== pendingReviewState.requestId) return;
    pendingReviewState.error = pendingReviewErrorText(error);
  } finally {
    if (pendingReviewState.inflight === key) pendingReviewState.inflight = null;
    if (requestId === pendingReviewState.requestId) patchPendingReviewBody();
  }
}

async function pendingReviewBulkIgnore() {
  const target = currentPendingReviewDate();
  if (!target) {
    pendingReviewStatus("先选择日期再批量忽略。", "warning");
    return;
  }
  if (pendingReviewState.allDates) {
    pendingReviewStatus("批量忽略只针对当前日期；请切回「只看本日」。", "warning");
    return;
  }
  const payload = pendingReviewState.payload;
  const counts = pendingOpenCounts(
    payload?.totals || {},
    Array.isArray(payload?.groups) ? payload.groups : [],
    false,
  );
  const countText = counts.scopedActionable === null
    ? "未知数量"
    : `${pendingReviewNumber(counts.scopedActionable)} 条`;
  const confirmFn = typeof window !== "undefined" && typeof window.confirm === "function"
    ? window.confirm.bind(window)
    : null;
  if (confirmFn && !confirmFn(`确定忽略 ${target} 的全部 ${countText}抽取待确认？\n只写列表状态，不会重跑模型。`)) return;
  pendingReviewStatus("正在批量忽略……");
  try {
    const result = await api(
      "/api/pending-sources/status-bulk",
      {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({
          date: target,
          group: "needs_review",
          status: "ignored",
          note: "批量清理测试口语",
        }),
      },
      {silent: true, operation: "批量忽略抽取待确认"},
    );
    await refreshPendingReviewBlock(target, {force: true});
    pendingReviewStatus(`已忽略 ${pendingReviewNumber(result?.count ?? 0)} 条。`, "ready");
  } catch (error) {
    if ([404, 405, 501].includes(Number(error?.status))) {
      pendingReviewStatus("批量接口暂不可用，正在用兼容模式逐条处理……");
      try {
        const result = await pendingReviewBulkFallback(target);
        if (result.empty) {
          pendingReviewStatus("本日没有需确认来源。", "ready");
          return;
        }
        let message = `兼容模式已忽略 ${pendingReviewNumber(result.succeeded)} 条`;
        if (result.failed) message += `，${pendingReviewNumber(result.failed)} 条失败`;
        if (result.truncated) message += `；本日还有 ${pendingReviewNumber(result.truncated)} 条未纳入本次处理，请刷新后继续`;
        pendingReviewStatus(`${message}。`, result.failed ? "warning" : "ready");
      } catch (fallbackError) {
        pendingReviewStatus(`批量忽略失败：${pendingReviewErrorText(fallbackError)}`, "error");
      }
      return;
    }
    pendingReviewStatus(`批量忽略失败：${pendingReviewErrorText(error)}`, "error");
  }
}

async function pendingReviewAction(kind, encodedId = "", extra = "") {
  const sourceId = decodeURIComponent(String(encodedId || ""));
  if (kind === "answer-question") {
    if (pendingReviewState.submittingQuestion === sourceId) return;
    const operation = extra || "uncertain";
    const pendingOps = (pendingReviewState.pendingQuestionOps = pendingReviewState.pendingQuestionOps || {});
    const existingOp = pendingOps[sourceId];
    let opId;
    if (existingOp && existingOp.operation === operation && existingOp.opId) {
      // 失败重试：复用同一稳定操作标识，保证后端幂等
      opId = existingOp.opId;
    } else {
      // 首次提交或更换答案：生成新操作标识
      opId = `op_${Date.now()}_${Math.random().toString(36).slice(2, 7)}`;
      pendingOps[sourceId] = { operation, opId };
    }
    pendingReviewState.submittingQuestion = sourceId;
    pendingReviewStatus("正在记录来源判断……");
    try {
      const result = await api("/api/review-questions/answer", {
        method: "POST",
        body: JSON.stringify({
          source_id: sourceId,
          operation: operation,
          op_id: opId,
          reason: "用户在需要我回答抽屉中选择"
        })
      }, { operation: "提交来源判断", silent: true });

      if (result && result.ok) {
        if (pendingReviewState.pendingQuestionOps) {
          delete pendingReviewState.pendingQuestionOps[sourceId];
        }
        pendingReviewState.answeredQuestions = {
          ...(pendingReviewState.answeredQuestions || {}),
          [sourceId]: result
        };
        pendingReviewStatus(
          operation === "media"
            ? "已记录为影视/广告（仅关闭本来源提醒，可撤销）；未改动其他录音、未生成事件。"
            : (operation === "uncertain" ? "已保留为暂不确定；继续留待后续核对。" : "已记录来源判断；未生成事件、未记账。"),
          "ready"
        );
        document.querySelector(".pq-drawer")?.remove();
        await refreshPendingReviewBlock(currentPendingReviewDate(), { force: true });
        return;
      }
      throw new Error(result?.error || "记录失败");
    } catch (err) {
      const msg = String(err?.message || err);
      // 真实后端缺口给出明确提示，绝不写 ignored 冒充成功
      pendingReviewStatus(`提交失败：${msg}（未做冒充标记，保留原始状态）`, "error");
      const drawerInner = document.querySelector(".pq-drawer-inner");
      if (drawerInner && typeof drawerInner.querySelector === "function" && !drawerInner.querySelector(".pq-retry-box")) {
        drawerInner.insertAdjacentHTML("beforeend", `
          <div class="pq-retry-box" style="margin-top:10px;padding:8px 10px;border-radius:8px;background:#fef2f2;border:1px solid #fecaca;color:#991b1b;font-size:12px;">
            <p style="margin:0 0 6px;">提交失败：${escapeHtml(msg)}<br><small style="color:#b91c1c;">接口未就绪或请求失败，未自动标记为已忽略，条目状态已完整保留。</small></p>
            <button type="button" class="pq-opt-btn" onclick="pendingReviewAction('answer-question','${encodedId}','${extra}')">重试提交</button>
          </div>
        `);
      }
      return;
    } finally {
      if (pendingReviewState.submittingQuestion === sourceId) {
        pendingReviewState.submittingQuestion = null;
      }
    }
  }
  if (kind === "undo-question") {
    pendingReviewStatus("正在撤销来源判断……");
    try {
      const result = await api("/api/review-questions/undo", {
        method: "POST",
        body: JSON.stringify({ source_id: sourceId })
      }, { operation: "撤销来源判断", silent: true });
      if (result && result.ok) {
        if (pendingReviewState.pendingQuestionOps) {
          delete pendingReviewState.pendingQuestionOps[sourceId];
        }
        if (pendingReviewState.answeredQuestions) {
          delete pendingReviewState.answeredQuestions[sourceId];
        }
        const item = pendingFindItem(sourceId);
        if (item) {
          item.review_question_state = "unknown";
          item.review_question_operation = null;
          item.review_question_op_id = null;
          item.review_question_undoable = false;
        }
        pendingReviewStatus("已撤销上一次来源判断，已恢复原提醒状态。", "ready");
        document.querySelector(".pq-drawer")?.remove();
        await refreshPendingReviewBlock(currentPendingReviewDate(), { force: true });
        return;
      }
      throw new Error(result?.error || "撤销失败");
    } catch (err) {
      pendingReviewStatus(`撤销失败：${String(err?.message || err)}`, "error");
      return;
    }
  }
  if (kind === "keep-pending") { document.querySelector(".pq-drawer")?.remove(); return; }
  if (kind === "open-question" || kind === "close-question") {
    document.querySelector(".pq-drawer")?.remove();
    if (kind === "open-question") {
      const item = pendingFindItem(decodeURIComponent(encodedId));
      if (!item) { pendingReviewStatus("这条已经不在待回答列表里了。", "warning"); return; }
      document.body.insertAdjacentHTML("beforeend", pendingQuestionDrawerHtml(item));
    }
    return;
  }
  if (kind === "toggle-health") {
    pendingReviewState.healthOpen = !pendingReviewState.healthOpen;
    renderPendingReviewBlock(pendingReviewState.date || currentPendingReviewDate());
    return;
  }
  if (kind === "expand-questions") {
    pendingReviewState.expandQuestions = true;
    renderPendingReviewBlock(pendingReviewState.date || currentPendingReviewDate());
    return;
  }
  if (kind === "undo") {
    await setPendingReviewState(decodeURIComponent(encodedId), "open");
    return;
  }
  if (kind === "noop") return;
  if (kind === "expand" || kind === "collapse") {
    pendingReviewState.expandedOpen = kind === "expand";
    if (!pendingReviewState.expandedOpen) pendingReviewState.expanded = null;
    patchPendingReviewBody();
    if (pendingReviewState.expandedOpen && !pendingReviewState.expanded) {
      await refreshPendingReviewBlock(currentPendingReviewDate(), {force: true});
    }
    return;
  }
  if (kind === "scope") {
    pendingReviewState.allDates = !pendingReviewState.allDates;
    pendingReviewState.confirmId = null;
    pendingReviewState.payload = null;
    pendingReviewState.expanded = null;
    pendingReviewState.expandedOpen = false;
    pendingReviewState.error = null;
    patchPendingReviewBody();
    await refreshPendingReviewBlock(currentPendingReviewDate(), {force: true});
    return;
  }
  if (kind === "reload") {
    pendingReviewState.confirmId = null;
    pendingReviewStatus("正在重新读取……");
    await refreshPendingReviewBlock(currentPendingReviewDate(), {force: true});
    pendingReviewStatus(pendingReviewState.error ? "仍然读不到。" : "已重新读取。");
    return;
  }
  if (kind === "cancel-confirm") {
    pendingReviewState.confirmId = null;
    patchPendingReviewBody();
    return;
  }
  const item = findPendingReviewItem(sourceId);
  if (!item) return;
  if (kind === "copy") {
    try {
      await navigator.clipboard.writeText(String(item.quote || ""));
      pendingReviewStatus("已复制原文。", "ready");
    } catch (error) {
      pendingReviewStatus(`复制失败：${String(error?.message || error)}`, "error");
    }
    return;
  }
  if (kind === "promote") {
    // 刻意不做「自动晋升成正式事件」：唯一现成的写入口 /api/events/natural-correction
    // 要求目标关联一份**已存在**的 events.json（实测返回「事件修正必须关联现有
    // events.json」），而 pending 条目恰恰是没进 events.json 的那批 —— 这条链路走不通，
    // 所以界面上不摆晋升按钮，只给复制、标记已处理、忽略这些真实可用的动作。
    pendingReviewStatus("这些来源条目没有自动重新抽取通道。可复制原文、标记已处理或忽略；如需正式事件，请重新导入相关录音或手动补录。", "warning");
    return;
  }
  if (kind === "confirm") {
    pendingReviewState.confirmId = pendingReviewState.confirmId === sourceId ? null : sourceId;
    patchPendingReviewBody();
    return;
  }
  if (kind === "instruction") {
    const input = document.getElementById("natural-correction-input");
    if (!input) {
      pendingReviewStatus("当前页面没有口语补录入口，请刷新后重试；不会自动入账。", "warning");
      return;
    }
    input.value = `记一个待办：${String(item.quote || "").trim()}`;
    input.focus();
    pendingReviewStatus("已填入口语补录入口；请核对后由你手动发送。不会自动发送、不会生成财务记录。", "warning");
    return;
  }
  if (kind === "handled" || kind === "ignored") {
    pendingReviewStatus("正在记录审核状态……");
    const note = kind === "ignored" ? "用户忽略" : "用户标记已看过（仅关闭提醒）";
    const ok = await setPendingReviewState(sourceId, kind, note);
    if (ok) {
      pendingReviewStatus(
        kind === "ignored"
          ? "已忽略并关闭提醒；未确认内容、未入账。"
          : "已标记为已看过并关闭提醒；未确认内容、未入账。",
        "ready",
      );
    }
  }
}

async function pendingReviewConfirmSubmit(event, encodedId = "") {
  event?.preventDefault?.();
  const sourceId = decodeURIComponent(String(encodedId || ""));
  const item = findPendingReviewItem(sourceId);
  if (!item) {
    pendingReviewStatus("这条待确认已经刷新掉了，请重新读取。", "warning");
    return;
  }
  const target = pendingManualTarget(item);
  if (!target) {
    pendingReviewStatus("这条没有可写入的 job / events.json；不能伪确认写入。请查看证据或复制原文。", "warning");
    return;
  }
  const suffix = encodeURIComponent(sourceId).replace(/'/g, "%27");
  const title = String(document.getElementById(`pending-confirm-title-${suffix}`)?.value || "").trim();
  const category = String(document.getElementById(`pending-confirm-category-${suffix}`)?.value || "").trim();
  const amountRaw = String(document.getElementById(`pending-confirm-amount-${suffix}`)?.value || "").trim();
  const timeRaw = String(document.getElementById(`pending-confirm-time-${suffix}`)?.value || "").trim();
  const activityType = String(document.getElementById(`pending-confirm-activity-${suffix}`)?.value || "").trim();
  if (!title) {
    pendingReviewStatus("请先填写事件标题；不会用模型建议自动确认。", "warning");
    return;
  }
  if (!PENDING_CONFIRM_CATEGORIES.some(([value]) => value === category)) {
    pendingReviewStatus("请选择事件类型；系统不自动猜类型。", "warning");
    return;
  }
  let amount = null;
  if (amountRaw) {
    amount = Number(amountRaw);
    if (!Number.isFinite(amount) || amount < 0) {
      pendingReviewStatus("金额必须是非负数字；未识别时请留空，不能写成 0。", "warning");
      return;
    }
  }
  if (amount !== null && category !== "expense") {
    pendingReviewStatus("金额只能随“消费”事件确认；请改选消费或清空金额。", "warning");
    return;
  }
  let absoluteTime = null;
  if (timeRaw) {
    const parsed = beijingDateTimeInputDate(timeRaw);
    if (!parsed || Number.isNaN(parsed.getTime())) {
      pendingReviewStatus("发生时间格式无效；留空表示时间未知。", "warning");
      return;
    }
    absoluteTime = parsed.toISOString();
  }
  const data = {};
  if (category === "expense" && amount !== null) {
    data.expense = {
      amount,
      currency: String(item.proposed_currency || "CNY"),
      item: String(item.proposed_item || title),
    };
  } else if (category === "food") {
    data.food = {item: String(item.proposed_item || title)};
  }
  const body = {
    path: target.artifactPath,
    title,
    categories: [category],
    absolute_time: absoluteTime,
    time_precision: absoluteTime ? "exact" : "unknown",
    activity_type: activityType || null,
    data,
    note: `用户确认待确认来源：${String(item.quote || "").trim()}`,
  };
  if (pendingReviewState.confirmSaving === sourceId) return;
  pendingReviewState.confirmSaving = sourceId;
  const saveButton = document.querySelector?.(".pending-confirm-save");
  if (saveButton) saveButton.disabled = true;
  pendingReviewStatus("正在确认目标文件并写入人工事件……");
  try {
    // Do not let the backend create a fresh events.json at an arbitrary path.
    // Only an already-produced target is eligible for this UI confirmation.
    const artifactPayload = await api(
      `/api/jobs/${encodeURIComponent(target.jobId)}/artifacts`,
      {},
      {silent: true, operation: "确认人工事件目标文件"},
    );
    const artifactPaths = (Array.isArray(artifactPayload?.artifacts) ? artifactPayload.artifacts : [])
      .map(pendingSafeRelativePath)
      .filter((pathValue) => pathValue.endsWith("/events.json") || pathValue === "events.json");
    if (!artifactPaths.includes(target.artifactPath)) {
      const sameDate = artifactPaths.filter((pathValue) => pathValue.includes(`/${target.date}/`));
      const candidates = sameDate.length ? sameDate : artifactPaths;
      if (candidates.length === 1) {
        target.artifactPath = candidates[0];
      } else if (!candidates.length) {
        pendingReviewStatus("目标 events.json 不在这个 job 的已产出文件里；未创建新文件，未写入确认。", "error");
        return;
      } else {
        pendingReviewStatus("这个 job 有多个 events.json，无法确定同一目标；未写入确认。", "error");
        return;
      }
    }
    const response = await api(
      `/api/jobs/${encodeURIComponent(target.jobId)}/manual-event`,
      {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify(body),
      },
      {silent: true, operation: "人工确认待确认事件"},
    );
    const savedEventId = String(response?.event?.event_id || "");
    if (!savedEventId) {
      pendingReviewStatus("后端没有返回已保存的人工事件；未把这次操作当成成功。", "error");
      return;
    }
    // Read back the same target before closing the pending reminder.
    const readback = await api(
      `/api/jobs/${encodeURIComponent(target.jobId)}/manual-events?path=${encodeURIComponent(target.artifactPath)}`,
      {},
      {silent: true, operation: "读回人工确认事件"},
    );
    const readbackEvents = Array.isArray(readback?.events) ? readback.events : [];
    if (!readbackEvents.some((event) => String(event?.event_id || "") === savedEventId)) {
      pendingReviewStatus("已请求写入，但同一目标读回不到该事件；未关闭提醒，未宣称成功。", "error");
      return;
    }
    pendingReviewState.confirmId = null;
    pendingReviewStatus(
      `已人工确认并读回同一事件，保存到 ${target.jobId} 的 ${target.artifactPath}；未覆盖原始 events.json，未自动改历史金额。`,
      "ready",
    );
    await refreshPendingReviewBlock(currentPendingReviewDate(), {force: true});
  } catch (error) {
    pendingReviewStatus(`人工确认保存失败：${pendingReviewErrorText(error)}`, "error");
  } finally {
    pendingReviewState.confirmSaving = null;
    if (saveButton) saveButton.disabled = false;
  }
}

// 【P0 修】待确认条目的“标记已处理/忽略”原先调用了一个不存在的函数，按钮静默失败。
// 唯一写入口是 /api/pending-sources/status（只写人工台账，不改抽取产物）。
async function setPendingReviewState(sourceId, kind, note = "") {
  const id = String(sourceId || "").trim();
  const status = kind === "ignored" ? "ignored" : (kind === "open" ? "open" : "handled");
  if (!id) return false;
  try {
    const payload = await api("/api/pending-sources/status", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({source_id: id, status, note: String(note || "")}),
    }, {silent: true, operation: "记录待确认处理状态"});
    if (!payload || payload.ok === false) {
      pendingReviewStatus(`没有记录成功：${payload && payload.error ? payload.error : "服务端拒绝"}`, "error");
      return false;
    }
    await refreshPendingReviewBlock(currentPendingReviewDate(), {force: true});
    return true;
  } catch (error) {
    pendingReviewStatus(`写入失败：${String(error && error.message || error)}`, "error");
    return false;
  }
}
window.setPendingReviewState = setPendingReviewState;

async function submitBroadcastDecision(button, date, eventId, action) {
  const day = journalDays.find((item) => item && item.date === date);
  const ui = broadcastUi(String(date));
  if (ui.busy) return;
  if (!["accept", "reject", "undo"].includes(action)) return;
  const strip = button && button.closest ? button.closest("[data-broadcast-strip]") : null;
  const controls = strip ? [...strip.querySelectorAll("button[data-broadcast-action]")] : (button ? [button] : []);
  const originals = controls.map((node) => ({node, text: node.textContent, disabled: node.disabled}));
  ui.busy = `${action}:${eventId}`;
  ui.message = "";
  ui.messageKind = "";
  controls.forEach((node) => { node.disabled = true; });
  if (button) button.textContent = "处理中…";
  const doneText = action === "accept" ? "已计入当日总额。" : (action === "reject" ? "已排除，不计入当日总额。" : "已撤销，回到待确认。");
  let written = false;
  try {
    const body = {date, event_id: eventId};
    await api(`${BROADCAST_ENDPOINT}/${action}`, {
      method: "POST",
      // Origin 由浏览器同源请求自动带上；CSRF 头是服务端硬要求，缺了会 403。
      headers: {"Content-Type": "application/json", "X-Life-Logger-CSRF": "1"},
      body: JSON.stringify(body),
    }, {silent: true, operation: "处置待确认播报"});
    written = true;
    ui.message = doneText;
    ui.messageKind = "ok";
  } catch (error) {
    ui.message = `处置失败：${String(error && error.message || error)}`;
    ui.messageKind = "error";
    originals.forEach(({node, text, disabled}) => {
      if (!node.isConnected) return;
      node.textContent = text;
      node.disabled = disabled;
    });
    const host = strip ? strip.querySelector(".broadcast-panel") : null;
    if (host) {
      let status = host.querySelector(".broadcast-message");
      if (!status) {
        status = document.createElement("p");
        status.className = "broadcast-message";
        status.setAttribute("role", "status");
        status.setAttribute("aria-live", "polite");
        host.prepend(status);
      }
      status.className = "broadcast-message error";
      status.textContent = ui.message;
    }
  }
  if (written) {
    // 写已经成功；刷新失败不能反过来报「处置失败」，否则用户会重复点。
    try {
      await refreshJournalDayAfterBroadcast(date);
    } catch (refreshError) {
      ui.message = `${doneText}（刷新当日数据失败：${String(refreshError && refreshError.message || refreshError)}，可点「刷新记录」重试）`;
      ui.messageKind = "ok";
      const host = strip && strip.isConnected ? strip.querySelector(".broadcast-panel") : null;
      if (host) {
        let status = host.querySelector(".broadcast-message");
        if (!status) {
          status = document.createElement("p");
          status.className = "broadcast-message";
          status.setAttribute("role", "status");
          status.setAttribute("aria-live", "polite");
          host.prepend(status);
        }
        status.className = "broadcast-message ok";
        status.textContent = ui.message;
      }
    }
  }
  ui.busy = "";
}

function hideEmbeddedTimelineSummary(frame) {
  try {
    const doc = frame.contentDocument;
    if (!doc || doc.getElementById("embedded-hide-summary")) return;
    const style = doc.createElement("style");
    style.id = "embedded-hide-summary";
    style.textContent = "#summaryStatsText{display:none !important}";
    (doc.head || doc.documentElement).appendChild(style);
  } catch (_) { /* 跨文档异常忽略 */ }
}

window.addEventListener("message", (event) => {
  if (event.origin !== window.location.origin) return;
  const payload = event.data || {};
  if (payload.type !== "life-logger-timeline-summary" || !/^\d{4}-\d{2}-\d{2}$/.test(String(payload.date || ""))) return;
  const liveFrame = document.querySelector(".day-timeline-frame");
  if (liveFrame) hideEmbeddedTimelineSummary(liveFrame);
  timelineFacts.set(payload.date, payload);
  if (payload.date !== selectedJournalDate) return;
  const day = journalDays.find((item) => item.date === payload.date);
  renderJournalDay(day || {date: payload.date, entries: [], _shell: true, canonical: false});
  // The iframe emits an early empty/loading summary while swapping dates.
  // Release the frozen box only after it reports actual content; a genuinely
  // empty day is covered by the bounded release timeout.
  const summaryHasContent = Number(payload.events || 0) > 0
    || Number(payload.captures || 0) > 0
    || Number(payload.unlocated || 0) > 0
    || Number(payload.scenes || 0) > 0
    || Number(payload.recordings || 0) > 0;
  if (summaryHasContent) scheduleJournalSwitchHeightRelease(payload.date);
});

// 【P0 返工】增量渲染：新模板没有的节点（例如 canonical 到达后的 .journal-empty）
// 必须从旧 DOM 里移除，否则“0 件事 / 读取较慢 / 立即重试”会和真实内容并存。
const JOURNAL_DAY_INCREMENTAL_SELECTORS = [
  // 顺序必须与 markup 的 DOM 顺序一致，插入时才能找到正确 anchor
  ".journal-day-header", ".journal-switch-banner", ".journal-report-stale", ".focus-summary", ".day-expense-drilldown", ".today-details", ".journal-empty",
];
// 增量路径只在“旧 DOM 结构完整”时使用；缺节点（例如旧版只剩空态）必须整块重绘。
function journalDayIncrementalReady(dashboard) {
  return Boolean(dashboard.querySelector(".journal-day-header") && dashboard.querySelector(".focus-summary"));
}

// 缺失节点要能“从无到有”插入：按固定顺序找下一个已存在的节点做 anchor，
// 避免 .journal-report-stale 这类“后出现”的提示永远插入不了。
function insertJournalDayNode(dashboard, selector, node) {
  const index = JOURNAL_DAY_INCREMENTAL_SELECTORS.indexOf(selector);
  const later = index >= 0 ? JOURNAL_DAY_INCREMENTAL_SELECTORS.slice(index + 1) : [];
  for (const candidate of later) {
    const anchor = dashboard.querySelector(candidate);
    if (anchor && anchor.parentNode === dashboard) {
      anchor.before(node);
      return;
    }
  }
  dashboard.appendChild(node);
}

function syncJournalDayNodes(dashboard, template) {
  // 待答区不在增量节点里，但必须跟着日期走，否则新日期的响应会被当成过期丢掉。
  const nextPendingDate = template.content.querySelector("#pending-review-block")?.dataset.date;
  const pendingBlock = dashboard.querySelector("#pending-review-block");
  if (nextPendingDate && pendingBlock) pendingBlock.dataset.date = nextPendingDate;
  for (const selector of JOURNAL_DAY_INCREMENTAL_SELECTORS) {
    const oldNode = dashboard.querySelector(selector);
    const nextNode = template.content.querySelector(selector);
    if (nextNode) {
      if (!oldNode) {
        insertJournalDayNode(dashboard, selector, nextNode);
        continue;
      }
      if (oldNode.tagName === "DETAILS") nextNode.open = oldNode.open;
      oldNode.replaceWith(nextNode);
      continue;
    }
    if (oldNode) oldNode.remove();
  }
}

function synthesisStaleNotice(day) {
  if (!day || day.synthesisStale !== true) return "";
  const status = day.synthesisStatus && typeof day.synthesisStatus === "object"
    ? day.synthesisStatus : {};
  const synthesis = day.synthesis && typeof day.synthesis === "object" ? day.synthesis : {};
  const count = (value) => {
    const number = Number(value);
    return Number.isFinite(number) ? formatCount(number) : "";
  };
  const canonical = count(status.canonical_event_count ?? day.eventCount);
  const old = count(status.synthesis_event_count ?? synthesis.event_count);
  if (canonical && old) {
    return `日报基于旧快照：${old}条，当前${canonical}条，待重生成`;
  }
  return "日报基于旧快照，待重生成";
}

function renderJournalDay(day) {
  // Do not let a late response for today/another date repaint or retarget the
  // iframe after the user has explicitly selected a different day.
  if (typeof journalDayAllowedForSelection === "function" && !journalDayAllowedForSelection(day)) return;
  const dashboard = document.getElementById("journal-day-view") || $("journal-dashboard");
  const pendingDate = day?.date || selectedJournalDate || shanghaiDate(0);
  const pendingInbox = renderPendingReviewBlock(pendingDate);
  if (!day) {
    const facts = timelineFactsFor(pendingDate);
    const primary = dayPrimaryCount(null, facts);
    // 三态：时间轴有内容 / 时间轴确认空 / 尚未读取成功。任何“有内容”状态都不许说“没有记录”。
    const hasContent = facts && !facts.empty;
    const confirmedEmpty = facts && facts.empty;
    const statusBlock = hasContent
      ? `<strong>时间轴已显示 ${escapeHtml(primary.text)}</strong><span>统计口径（分类与账本）仍在读取，读完不会覆盖时间轴内容。</span>`
      : confirmedEmpty
        ? `<strong>这一天没有记录</strong><span>时间轴已确认：没有事件、场景分段或录音覆盖。不会借用其他日期的内容。</span>`
        : `<strong>尚未读取到这一天的记录</strong><span>可能仍在加载或读取失败；这不等于“没有记录”。</span><button class="journal-action" type="button" onclick="retryJournalCanonical('${escapeHtml(pendingDate)}')">重试</button>`;
    dashboard.innerHTML = `<div class="journal-day-header"><div><span class="journal-kicker">当前查看</span><h3>${escapeHtml(friendlyDate(pendingDate))}</h3></div></div>
      ${daySwitchBannerMarkup(pendingDate, false)}
      ${hasContent ? `<p class="focus-summary"${primary.failure ? ' data-timeline-error="1" title="活动整理失败：这条时间轴没能生成完整整理"' : ""}><strong>${escapeHtml(primary.text)}</strong></p>` : ""}
      <div class="journal-empty" role="status">${statusBlock}</div>
      ${timelineSectionMarkup(pendingDate, "当天")}${pendingInbox}`;
    const frame = dashboard.querySelector(".day-timeline-frame");
    if (frame) { frame.dataset.timelineDate = pendingDate; bindTimelineFrameState(frame, pendingDate, dashboard); }
    return;
  }
  ensureJournalDaySynthesis(day);
  ensureJournalActivityTimeline(day);
  if (!day.canonical) {
    const pending = day.canonicalPending;
    const pendingText = pending
      ? (pending.exhausted
        ? `<strong>这一天的记录仍在更新</strong><span>后台还在准备快照，已暂停自动刷新，避免一直占用连接；可以手动重试。</span>`
        : `<strong>正在更新这一天的记录</strong><span>后台正在整理这一天的数据，完成后会自动显示；在此之前不显示旧的或推导的数量。</span>`)
      : "";
    const facts = timelineFactsFor(day.date);
    const factsForDay = timelineFactsFor(day.date);
    const loadingSince = day._loadingSince || (day._loadingSince = Date.now());
    const loadState = journalLoadState({ facts: factsForDay, error: day.canonicalError,
      pending, elapsedMs: Date.now() - loadingSince });
    if (["loading", "slow"].includes(loadState.kind) && !day._slowTimer) {
      day._slowTimer = setTimeout(() => { day._slowTimer = null; if (!day.canonical) renderJournalDay(day); }, 3100);
    }
    const status = day.canonicalError
      ? `<strong>未读取成功（统计口径）</strong><span${day.canonicalErrorRaw ? ` title="${escapeHtml(day.canonicalErrorRaw)}"` : ""}>${escapeHtml(day.canonicalError)}；这不等于“没有记录”，也不会显示旧的派生数量。</span><button class="journal-action" type="button" onclick="retryJournalCanonical('${escapeHtml(day.date)}')">重试</button>`
      : pending
        ? `${pendingText}<button class="journal-action" type="button" onclick="retryJournalCanonical('${escapeHtml(day.date)}')">立即重试</button>`
        : `<strong>${escapeHtml(loadState.text)}</strong><span>${loadState.kind === "partial" ? "时间轴内容已经可用（上方计数来自时间轴）；分类与账本仍在读取，读完不会覆盖时间轴。" : "读完之前不会宣称“没有记录”。"}</span><button class="journal-action" type="button" onclick="retryJournalCanonical('${escapeHtml(day.date)}')">立即重试</button>`;
    const previousFrame = dashboard.querySelector?.(".day-timeline-frame");
    // Parent-only date switch: never replace the existing dashboard/iframe
    // container.  The old timeline remains at its real height until the iframe
    // has data for the new date; only surrounding nodes are updated.
    const canReuseFrame = Boolean(previousFrame && journalDayIncrementalReady(dashboard));
    const alreadyOnDate = (typeof timelineRenderedDate === "function" ? timelineRenderedDate(previousFrame) : "") === day.date;
    const dayWord = day.date === shanghaiDate(0) ? "今天" : (day.date === shanghaiDate(-1) ? "昨天" : "当天");
    // [2026-11-04 Q853] 状态块（"正在更新/读取失败" + 重试）与"正在切换到…"横幅
    // 说的是同一件事，同时出现就是两条提示两个按钮。状态块恒非空，横幅在此分支一律不渲染。
    const markup = `<div class="journal-day-header"><div><span class="journal-kicker">当前查看</span><h3>${escapeHtml(friendlyDate(day.date))}</h3></div></div>
      ${daySwitchBannerMarkup(day.date, false)}
      ${(() => { const primary = dayPrimaryCount(day, timelineFactsFor(day.date)); return `<p class="focus-summary"${primary.failure ? ' data-timeline-error="1" title="活动整理失败：这条时间轴没能生成完整整理"' : ""}><strong>${escapeHtml(primary.text || "事件统计更新中（时间轴已就绪）")}</strong></p>`; })()}
      <div class="journal-empty" role="status">${status}</div>
      ${timelineSectionMarkup(day.date, dayWord)}${pendingInbox}`;
    if (canReuseFrame) {
      const template = document.createElement("template");
      template.innerHTML = markup;
      // Keep the existing .day-timeline-section/iframe; remove only the
      // newly-built duplicate section from the staging template.
      template.content.querySelector(".day-timeline-section")?.remove();
      syncJournalDayNodes(dashboard, template);
      const heading = dashboard.querySelector("#day-timeline-heading");
      if (heading) {
        heading.textContent = `${dayWord}发生了什么`;
        heading.title = "每10分钟整理，UTC+8";
      }
      previousFrame.dataset.timelineDate = day.date;
      previousFrame.dataset.canonicalStamp = String(day._canonicalLoadedAt || "");
      previousFrame.dataset.canonicalPending = day.canonicalPending ? day.date : "";
      bindTimelineFrameState(previousFrame, day.date, dashboard);
      const sendDate = () => llTraceSend(previousFrame.contentWindow,
        {type: "life-logger-timeline-set-date", date: day.date}, "reuse-frame:first-load");
      // Same-day re-selection must reuse the already-rendered timeline. Sending
      // set-date again makes the iframe reload and clears its cards.
      if (!alreadyOnDate) {
        if (previousFrame.contentDocument?.readyState === "complete") sendDate();
        else previousFrame.addEventListener("load", sendDate, {once: true});
      }
    } else {
      dashboard.innerHTML = markup;
      const frame = dashboard.querySelector(".day-timeline-frame");
      if (frame) {
        frame.dataset.canonicalStamp = String(day._canonicalLoadedAt || "");
        frame.dataset.canonicalPending = day.canonicalPending ? day.date : "";
        bindTimelineFrameState(frame, day.date, dashboard);
      }
    }
    return;
  }

  const categoryChips = Object.entries(day.categories)
    .filter(([, count]) => count > 0)
    .sort((a, b) => b[1] - a[1])
    .map(([key, count]) => `<span class="category-chip">${escapeHtml(CATEGORY_LABELS[key] || key)} ${count}</span>`)
    .join("");
  // The iframe owns its scene/timeline projection. Do not build an unused second timeline here.
  const first = day.entries[0] || null;
  const actions = [
    first?.summaryPath ? `<a class="journal-action primary-action" href="${viewerLink(first.jobId, first.summaryPath)}" target="_blank">打开当日日报</a>` : "",
  ].filter(Boolean).join("");
  const expenseValue = typeof ExpenseDrilldown !== "undefined"
    ? ExpenseDrilldown.cnyTotal(day.expense_ledger)
    : Number(day.expense);
  const expenseText = Number.isFinite(expenseValue) && expenseValue > 0
    ? ` · <strong>¥${expenseValue.toFixed(2)}</strong>`
    : "";
  const pendingText = day.pendingReviewCount
    ? ` · <button class="focus-pending-link" type="button" onclick="switchView('review')">${formatCount(day.pendingReviewCount)} 件待确认</button>`
    : "";

  const dayKicker = day.date === shanghaiDate(0) ? "今天" : (day.date === shanghaiDate(-1) ? "昨天" : "当天");
  const dayWord = dayKicker === "当天" ? "这一天" : dayKicker;
  const dayHeading = [formatMonthDay(day.date), formatWeekday(day.date)].filter(Boolean).join(" ");
  const staleNotice = synthesisStaleNotice(day);
  const staleHtml = staleNotice
    // [2026-11-07 改版] 日报待更新是系统内部状态：只留一个小标记，原因放在悬停提示里。
    ? `<p class="hint journal-report-stale" role="status" aria-live="polite" title="${escapeHtml(staleNotice)}"><span class="stale-chip">日报待更新</span></p>`
    : "";
  const primaryCount = dayPrimaryCount(day, timelineFactsFor(day.date));
  const markup = `<div class="journal-day-header"><div><span class="journal-kicker">当前日期</span><h3>${escapeHtml(dayHeading)}</h3></div><div class="journal-actions">${actions}</div></div>
    ${staleHtml}
    <p class="focus-summary"${primaryCount.failure ? ' data-timeline-error="1" title="活动整理失败：这条时间轴没能生成完整整理"' : ""}><strong>${escapeHtml(primaryCount.text || (Number.isFinite(Number(day.eventCount)) ? `${formatCount(day.eventCount)} 件事` : "事件数统计中"))}</strong><button class="day-brief-toggle" type="button" aria-expanded="false" aria-controls="day-brief-details" onclick="toggleDayBrief(this)">${Number.isFinite(expenseValue) && expenseValue > 0 ? `¥${expenseValue.toFixed(2)} · 金额与分类 ▾` : "金额与分类 ▾"}</button>${pendingText}</p>
    <div class="day-expense-drilldown">${typeof ExpenseDrilldown !== "undefined" ? ExpenseDrilldown.build(day.expense_ledger) : ""}</div>
    ${renderBroadcastPending(day)}
    <details class="today-details">
      <summary>今天详情：待办、饮食、消费与分类</summary>
      <div class="journal-metrics">
        ${renderSaidMetric(day)}
        ${primaryCountMetricHtml(day, timelineFactsFor(day.date))}
        <div><span>本人发言段</span><strong>${day.selfCount}</strong></div>
        <div class="${day.pendingReviewCount ? "needs-review" : ""}"><span>待确认</span><strong>${day.pendingReviewCount || 0}</strong>${day.reviewPartialCount ? `<small>${day.reviewPartialCount} 条进行中</small>` : ""}</div>
        <div id="journal-metric-health" class="journal-metric-health" onclick="switchView('dashboard')" title="点击查看全天生活仪表盘"><span>生理状态</span><strong id="journal-metric-health-val">--</strong><small id="journal-metric-health-sub">Apple Watch</small></div>
      </div>
      <div class="journal-category-strip"><span>事件分类</span><div class="category-chips">${categoryChips || `<span class="muted">暂无分类事件</span>`}</div></div>
    </details>
    ${timelineSectionMarkup(day.date, dayWord)}${pendingInbox}`;
  const mountedFrame = dashboard.querySelector?.(".day-timeline-frame");
  const mountedDate = typeof timelineRenderedDate === "function" ? timelineRenderedDate(mountedFrame) : "";
  const sameDate = mountedFrame && mountedDate === day.date;
  if (sameDate && journalDayIncrementalReady(dashboard)) {
    // Supplemental data updates counters/details, without unloading an already rendered iframe.
    const template = document.createElement("template");
    template.innerHTML = markup;
    syncJournalDayNodes(dashboard, template);
    mountedFrame.dataset.canonicalPending = day.canonicalPending ? day.date : "";
    const stamp = String(day._canonicalLoadedAt || "");
    if (mountedFrame.dataset.canonicalStamp !== stamp) {
      mountedFrame.dataset.canonicalStamp = stamp;
      llTraceSend(mountedFrame.contentWindow, {type:"life-logger-timeline-data-updated",date:day.date}, "canonical-stamp-changed");
    }
  } else if (mountedFrame && journalDayIncrementalReady(dashboard)) {
    // 日期切换：保留已加载的 iframe，只换头部/摘要/详情，再让 iframe 自己换日期。
    // 这样不会把整个 100KB 时间轴页面重新解析一遍，也不再出现白屏。
    const template = document.createElement("template");
    template.innerHTML = markup;
    syncJournalDayNodes(dashboard, template);
    const nextHeading = template.content.querySelector("#day-timeline-heading");
    const oldHeading = dashboard.querySelector("#day-timeline-heading");
    if (nextHeading && oldHeading) {
      oldHeading.textContent = nextHeading.textContent;
      if (nextHeading.title) oldHeading.title = nextHeading.title;
    }
    mountedFrame.dataset.timelineDate = day.date;
    mountedFrame.dataset.canonicalStamp = String(day._canonicalLoadedAt || "");
    mountedFrame.dataset.canonicalPending = day.canonicalPending ? day.date : "";
    // 父页面拥有这一次 pending 轮询；把状态同步给 iframe，让它显示同一句话
    // 而不是各自发一整套请求。
    if (day.canonicalPending) {
      llTraceSend(mountedFrame.contentWindow, {
        type: "life-logger-timeline-pending",
        date: day.date,
        retry_after_ms: day.canonicalPending.retryAfterMs || 800,
      }, "canonical-pending");
    }
    const sendDate = () => llTraceSend(mountedFrame.contentWindow,
      {type: "life-logger-timeline-set-date", date: day.date}, "switch-date");
    if (mountedFrame.contentDocument?.readyState === "complete") sendDate();
    else mountedFrame.addEventListener("load", sendDate, {once: true});
  } else {
    dashboard.innerHTML = markup;
    const nextFrame = dashboard.querySelector?.(".day-timeline-frame");
    if (nextFrame) {
      nextFrame.dataset.canonicalStamp = String(day._canonicalLoadedAt || "");
      // 首次挂载也要让 iframe 知道“父页面正在等后台快照”，避免它自己再打一轮。
      nextFrame.dataset.canonicalPending = day.canonicalPending ? day.date : "";
    }
  }
  api(`/api/health/summary?date=${encodeURIComponent(day.date)}`, {}, {silent: true}).then((res) => {
    if (res && res.summary) {
      const s = res.summary;
      const valEl = $("journal-metric-health-val");
      const subEl = $("journal-metric-health-sub");
      if (valEl) valEl.textContent = s.avg_heart_rate ? `${s.avg_heart_rate} bpm` : (s.total_steps ? `${s.total_steps} 步` : "--");
      if (subEl) subEl.textContent = s.total_steps ? `${s.total_steps} 步 · 极值 ${s.min_heart_rate || '--'}~${s.max_heart_rate || '--'}` : "Apple Watch 同步";
    }
  }).catch(() => {});
  dashboard.querySelectorAll(".day-event-source").forEach((details) => {
    details.addEventListener("toggle", () => scheduleTimelineCollisionResolution(dashboard));
  });
  scheduleTimelineCollisionResolution(dashboard);
  fitDayTimelineFrame(dashboard);
}

// 【2026-10-14】瀑布图 iframe 高度自适应。同源（都用 127.0.0.1:8765 服务），
// 可以直接读 contentDocument 拿到真实内容高度，避免写死一个高度导致大面积留白/截断。
// 瀑布图把“已经画完的日期”写在根节点上；用它判断能否跳过 set-date。
function timelineRenderedDate(frame) {
  if (!frame) return "";
  try {
    return frame.contentDocument?.documentElement?.dataset.renderedDate || "";
  } catch (_) {
    return "";
  }
}

// 全局接收来自 iframe 的真实内容高度：来源严格限制为当前连接的 day-timeline-frame
// [2026-11-07 改版] 时间轴里饮食块的“去处理”：打开「需要我判断」并选中对应用途（如 热量待定）。
window.addEventListener("message", (event) => {
  if (event.origin !== window.location.origin) return;
  const payload = event.data || {};
  if (payload.type !== "life-logger-open-review") return;
  const frame = document.querySelector(".day-timeline-frame");
  if (!frame || event.source !== frame.contentWindow) return;
  judgmentState.purpose = String(payload.purpose || "");
  switchView("review");
  loadJudgmentQueue(true).then(() => {
    // 后端还没有这一类（例如“热量待定”尚未上线）时，退回看全部，不停在“这一类没有要判断的”。
    const purposes = (judgmentState.payload && judgmentState.payload.purposes) || [];
    if (judgmentState.purpose && !purposes.some((row) => row.key === judgmentState.purpose)) {
      judgmentState.purpose = "";
      if (typeof renderJudgmentPanel === "function") renderJudgmentPanel();
    }
  }).catch(() => {});
});

window.addEventListener("message", (event) => {
  const payload = event.data || {};
  if (payload.type !== "life-logger-timeline-height") return;
  const frame = document.querySelector(".day-timeline-frame");
  if (!frame || !frame.isConnected || event.source !== frame.contentWindow) return;
  const height = Number(payload.height);
  if (!Number.isFinite(height) || height < 100) return;
  const next = Math.round(height) + 6;
  const previous = Number(frame._lastHeight || 0);
  if (Math.abs(next - previous) < 4) return;
  frame._heightFrozen = false;
  frame._lastHeight = next;
  frame.style.height = `${next}px`;
  frame.style.minHeight = `${next}px`;
});

function fitDayTimelineFrame(scope) {
  const frame = (scope || document).querySelector(".day-timeline-frame");
  if (!frame) return;
  try {
    const doc = frame.contentDocument;
    if (!doc) return;
    const content = doc.querySelector("#app");
    const height = content ? Math.ceil(content.getBoundingClientRect().height) : (doc.body?.scrollHeight || 0);
    if (height >= 100) {
      const next = Math.round(height) + 6;
      frame._heightFrozen = false;
      frame._lastHeight = next;
      frame.style.height = `${next}px`;
      frame.style.minHeight = `${next}px`;
    }
  } catch (_) {}
}

function eventClock(event) {
  const millis = Date.parse(journalEventTimestamp(event));
  if (Number.isFinite(millis)) {
    const parts = shanghaiTimeParts(millis, {hour: "2-digit", minute: "2-digit"}, "en-GB");
    return `${parts.hour}:${parts.minute}`;
  }
  const relative = Number(event?.time?.relative_seconds);
  if (Number.isFinite(relative)) {
    const hours = Math.floor(relative / 3600) % 24;
    const minutes = Math.floor((relative % 3600) / 60);
    return formatClockPair(hours, minutes);
  }
  return null;
}

function eventEndClock(event) {
  const millis = Date.parse(String(event?._journalEndAbsolute || event?.evidence?.absolute_ended_at || ""));
  if (!Number.isFinite(millis)) return null;
  const parts = shanghaiTimeParts(millis, {hour: "2-digit", minute: "2-digit"}, "en-GB");
  return `${parts.hour}:${parts.minute}`;
}

function minuteOfDay(event) {
  const clock = eventClock(event);
  if (!clock) return null;
  const [hourText, minuteText] = clock.split(":");
  const hour = Number(hourText);
  const minute = Number(minuteText);
  if (!Number.isFinite(hour) || !Number.isFinite(minute)) return null;
  return Math.max(0, Math.min(1439, hour * 60 + minute));
}

function timelinePhase(minute) {
  if (minute < 480) return "清晨";
  if (minute < 720) return "上午";
  if (minute < 840) return "中午";
  if (minute < 1080) return "下午";
  if (minute < 1320) return "晚上";
  return "深夜";
}

let timelineLayoutFrame = 0;

// 卡片避让必须读取卡片的真实布局高度。未激活的视图面板是 display:none
// （见 style.css 的 .view-panel），此时 getBoundingClientRect() 量到的卡片高度
// 全是 0，算出的 --card-shift 会让卡片互相重叠；而且面板重新可见时没有任何东西
// 会回来补算。这里用 IntersectionObserver 判断画布是否真的可见：不可见的画布
// 先跳过测量，等它真正进入视口时再补算一次。
function handleTimelineCanvasIntersections(entries) {
  const roots = new Set();
  for (const entry of entries) {
    const canvas = entry.target;
    // 画布所属的当日视图已被重渲染：停止观察，避免保留已脱离文档的节点。
    if (!canvas.isConnected) {
      timelineCanvasObserver.unobserve(canvas);
      continue;
    }
    if (!entry.isIntersecting) continue;
    timelineCanvasObserver.unobserve(canvas);
    roots.add(canvas.parentElement || document);
  }
  roots.forEach((root) => resolveTimelineCardCollisions(root));
}

const timelineCanvasObserver = typeof IntersectionObserver === "function"
  ? new IntersectionObserver(handleTimelineCanvasIntersections)
  : null;

function timelineCanvasIsMeasurable(canvas) {
  // 有布局盒才能量到真实高度；display:none 的隐藏面板量出来全是 0。
  if (canvas.getClientRects().length > 0) return true;
  timelineCanvasObserver?.observe(canvas);
  return false;
}

function resolveTimelineCardCollisions(root = document) {
  const canvases = root.querySelectorAll?.(".day-axis-canvas") || [];
  for (const canvas of canvases) {
    if (!timelineCanvasIsMeasurable(canvas)) continue;
    const anchors = [...canvas.querySelectorAll(".day-event-anchor")];
    anchors.forEach((anchor) => anchor.style.setProperty("--card-shift", "0px"));
    const lanes = new Map();
    for (const anchor of anchors) {
      const lane = Number(anchor.dataset.lane || "0");
      if (!lanes.has(lane)) lanes.set(lane, []);
      lanes.get(lane).push(anchor);
    }
    for (const laneAnchors of lanes.values()) {
      laneAnchors.sort((left, right) => Number(left.dataset.anchorY || 0) - Number(right.dataset.anchorY || 0));
      let previousBottom = -Infinity;
      for (const anchor of laneAnchors) {
        const card = anchor.querySelector(".day-event-card");
        if (!card) continue;
        const anchorY = Number(anchor.dataset.anchorY || anchor.offsetTop || 0);
        const cardHeight = card.getBoundingClientRect().height;
        const shift = Math.max(0, previousBottom + 10 - anchorY);
        anchor.style.setProperty("--card-shift", `${shift}px`);
        previousBottom = anchorY + shift + cardHeight;
      }
    }
  }
}

function scheduleTimelineCollisionResolution(root = document) {
  if (timelineLayoutFrame) cancelAnimationFrame(timelineLayoutFrame);
  timelineLayoutFrame = requestAnimationFrame(() => {
    timelineLayoutFrame = 0;
    resolveTimelineCardCollisions(root);
  });
}

function renderDayTimeline(events, date, recordingIntervals = [], watchCoverageAvailable = true, timelineScenes = []) {
  if (!events?.length && !recordingIntervals.length) {
    if (!timelineScenes?.length) {
      const coverageStatus = JOURNAL_LOGIC.recordingCoverageStatus(watchCoverageAvailable, 0);
      return `<div class="day-axis-empty" role="status"><strong>${escapeHtml(coverageStatus)}</strong><span>有录音文件起止时间后，绿色覆盖带会标出实际有音频的时段；未确认的空白不会被涂满。当天也还没有正式事件。</span></div>`;
    }
  }

  const prepared = events
    .map((event) => ({...event, _clock: eventClock(event) || "--:--", _minuteOfDay: minuteOfDay(event)}))
    .filter((event) => event._minuteOfDay !== null)
    .sort((a, b) => a._minuteOfDay - b._minuteOfDay);
  if (!prepared.length && !recordingIntervals.length) {
    if (!timelineScenes?.length) {
      const coverageStatus = JOURNAL_LOGIC.recordingCoverageStatus(watchCoverageAvailable, 0);
      return `<div class="day-axis-empty" role="status"><strong>事件缺少可用时间 · ${escapeHtml(coverageStatus)}</strong><span>需要获得录音的绝对开始时间后，才能把事件或录音覆盖放到一天的坐标上。</span></div>`;
    }
  }

  // A day view must keep a stable 24-hour coordinate system. Do not crop the
  // axis around the first/last detected event, otherwise different days are
  // visually incomparable and the user loses the sense of a full day.
  const startMinute = 0;
  const endMinute = 24 * 60;
  const span = endMinute - startMinute;
  const axisHeight = 24 * 56;

  const guides = [];
  for (let minute = startMinute; minute <= endMinute; minute += 60) {
    const top = ((minute - startMinute) / span) * 100;
    const hour = minute === endMinute ? 24 : Math.floor(minute / 60);
    const major = minute % 120 === 0 ? " major" : "";
    guides.push(`<div class="day-time-guide${major}" style="top:${top}%"><time>${INT_CLOCK.format(hour)}:00</time><span></span></div>`);
  }
  const visualRecordingIntervals = [];
  for (const interval of recordingIntervals) {
    const prev = visualRecordingIntervals[visualRecordingIntervals.length - 1];
    if (prev && interval.startMinute <= prev.endMinute + 2.5) {
      if (interval.endMinute > prev.endMinute) {
        prev.endMinute = interval.endMinute;
        prev.endPercent = interval.endPercent;
        prev.ended_at = interval.ended_at;
        prev.endLabel = interval.endLabel;
      }
      if (interval.battery_snapshot) prev.battery_snapshot = interval.battery_snapshot;
    } else {
      visualRecordingIntervals.push({...interval});
    }
  }
  const recordingBarElements = visualRecordingIntervals.map((interval) => {
    let batteryText = "";
    if (interval.battery_snapshot && typeof interval.battery_snapshot.level_percent === "number") {
      const stateMap = {charging: "充电中", unplugged: "未充电", full: "已充满"};
      const stateLabel = stateMap[interval.battery_snapshot.state] || "";
      batteryText = ` · 手表电量 ${formatPercent((interval.battery_snapshot.level_percent || 0) / 100)}${stateLabel ? ` (${stateLabel})` : ""}`;
    }
    const label = `录音覆盖 ${interval.startLabel}–${interval.endLabel}${batteryText}`;
    const height = Math.max(0, interval.endPercent - interval.startPercent);
    const hasBattery = Boolean(interval.battery_snapshot && typeof interval.battery_snapshot.level_percent === "number");
    const batteryAttr = hasBattery ? ` data-battery-level="${interval.battery_snapshot.level_percent}"` : "";
    return `<div class="day-recording-interval${hasBattery ? " with-battery" : ""}" aria-hidden="true" title="${escapeHtml(label)}"${batteryAttr} style="top:${interval.startPercent}%;height:${height}%"></div>`;
  }).join("");
  let livePulseBarElement = "";
  if (date === shanghaiDate(0) && recordingIntervals.length > 0) {
    const latest = recordingIntervals[recordingIntervals.length - 1];
    // [2026-11-07 唯一事实来源·P1] 时间一律按北京时间格式化：用 Intl 指定
    // timeZone: "Asia/Shanghai"，不再手工 getUTCHours()+8（那等于在代码里维护第二套时区换算）。
    // 事件本身的时间来自 eventClock() → shanghaiTimeParts()，同一套口径；
    // 跨零点归属以接口给的 date 为准（下面的 date === shanghaiDate(0) 判断）。
    const nowParts = shanghaiTimeParts(Date.now(), {hour: "2-digit", minute: "2-digit"}, "en-GB");
    const beijingHour = Number(nowParts.hour);
    const beijingMinute = Number(nowParts.minute);
    const currentNowMinute = beijingHour * 60 + beijingMinute;
    if (currentNowMinute > latest.endMinute && (currentNowMinute - latest.endMinute) <= 25) {
      const pulseTop = latest.endPercent;
      const pulseHeight = Math.max(0.4, ((currentNowMinute - latest.endMinute) / span) * 100);
      const curTimeLabel = `${String(beijingHour).padStart(2, "0")}:${String(beijingMinute).padStart(2, "0")}`;
      livePulseBarElement = `<div class="day-recording-interval live-recording-pulsing" aria-hidden="true" title="手表当前正在边录边传（实时录音覆盖中：${latest.endLabel}–${curTimeLabel}）" style="top:${pulseTop}%;height:${pulseHeight}%"></div>`;
    }
  }

  const recordingRange = JOURNAL_LOGIC.recordingCoverageSummary(recordingIntervals);
  let batterySummaryText = "";
  if (recordingRange && typeof recordingRange.startBattery === "number") {
    batterySummaryText = recordingRange.startBattery === recordingRange.endBattery
      ? ` · 电量 ${formatPercent((recordingRange.startBattery || 0) / 100)}`
      : ` · 电量 ${formatPercent((recordingRange.startBattery || 0) / 100)} → ${formatPercent((recordingRange.endBattery || 0) / 100)}`;
  }
  const recordingLabel = recordingRange
    ? `录音覆盖 ${recordingRange.startLabel} 至 ${recordingRange.endLabel}，${formatCount(recordingRange.intervalCount)} 个实际区间${batterySummaryText}`
    : "";
  const recordingBars = (recordingIntervals.length || livePulseBarElement)
    ? `<div class="day-recording-layer" role="group" aria-label="${escapeHtml(recordingLabel)}">${recordingBarElements}${livePulseBarElement}</div>`
    : "";

  const sceneElements = (timelineScenes || []).map((s) => {
    const startPercent = (s.start_minute / span) * 100;
    const height = Math.max(0.4, ((s.end_minute - s.start_minute) / span) * 100);
    const title = `${s.start_time}–${s.end_time} (${s.duration_minutes}分) ${s.icon} ${s.label}${s.detail ? ' · ' + s.detail : ''}`;
    return `<div class="day-scene-interval" aria-hidden="true" title="${escapeHtml(title)}" style="top:${startPercent}%;height:${height}%;--scene-color:${s.color};"></div>`;
  }).join("");

  const capsuleElements = (timelineScenes || [])
    .filter((s) => s.scene !== "general" && s.duration_minutes >= 3)
    .map((s) => {
      const top = (s.start_minute / span) * 100;
      const shortLabel = s.label.split(" / ")[0];
      const title = `${s.start_time}–${s.end_time} ${s.label}${s.detail ? ' · ' + s.detail : ''}`;
      return `<div class="day-scene-capsule" style="top:${top}%;--scene-color:${s.color};" title="${escapeHtml(title)}"><span class="scene-icon">${s.icon}</span><span class="scene-name">${escapeHtml(shortLabel)}</span><time>${s.start_time}</time></div>`;
    }).join("");

  let sceneTrailHtml = "";
  const significantScenes = (timelineScenes || []).filter((s) => s.scene !== "general" && s.duration_minutes >= 2);
  if (significantScenes.length > 0) {
    sceneTrailHtml = `<div class="day-axis-scene-trail" role="group" aria-label="场景足迹"><strong>🏷️ 场景足迹</strong>${significantScenes.map((s) => {
      const shortName = s.label.split(" / ")[0];
      const title = `${s.start_time}–${s.end_time} (${s.duration_minutes}分) ${s.label}${s.detail ? ' · ' + s.detail : ''}`;
      return `<span class="day-scene-tag" style="--scene-color:${s.color};" title="${escapeHtml(title)}">${s.icon} ${escapeHtml(shortName)} <small>${s.start_time}</small></span>`;
    }).join("")}</div>`;
  }

  const phasePoints = [
    [0, "深夜"],
    [6 * 60, "清晨"],
    [8 * 60, "上午"],
    [12 * 60, "中午"],
    [14 * 60, "下午"],
    [18 * 60, "晚上"],
    [22 * 60, "深夜"],
  ].filter(([minute], index, all) => minute >= startMinute && minute <= endMinute && all.findIndex(([m]) => m === minute) === index);
  const phases = phasePoints.map(([minute, label]) => {
    const top = ((minute - startMinute) / span) * 100;
    return `<div class="day-phase-label" style="top:${top}%">${escapeHtml(label)}</div>`;
  }).join("");

  const laneCount = window.innerWidth < 720 ? 2 : 3;
  const estimatedCardHeight = window.innerWidth < 720 ? 88 : 98;
  const laneGap = 10;
  const laneEnds = Array(laneCount).fill(-Infinity);
  const laidOut = prepared.map((event) => {
    const y = ((event._minuteOfDay - startMinute) / span) * axisHeight;
    let lane = laneEnds.findIndex((lastBottom) => y >= lastBottom + laneGap);
    if (lane < 0) lane = laneEnds.indexOf(Math.min(...laneEnds));
    laneEnds[lane] = Math.max(y, laneEnds[lane] + laneGap) + estimatedCardHeight;
    return {...event, _y: y, _lane: lane};
  });

  const eventCards = laidOut.map((event, index) => {
    const category = (event.categories || [event.activity_type || "other"])[0];
    const confidence = Number(event.confidence);
    const confidenceText = Number.isFinite(confidence) ? ` · 置信度 ${Math.round(confidence * 100)}%` : "";
    const sourceType = event.source_type === "manual" ? "人工确认" : (event.evidence?.decision_source === "typesafe_jev" ? "Jev 增强" : (event.evidence?.decision_source === "kev_local" ? "旧 Kev 记录" : "录音事件"));
    const sourceEditHint = event._correctionUnavailable ? " · 仅源记录，不支持直接编辑" : "";
    const crossesMidnight = journalEventCrossesMidnight(event);
    const endClock = crossesMidnight ? eventEndClock(event) : null;
    const crossDayText = crossesMidnight ? `跨至次日${endClock ? ` ${endClock}` : ""} · 归属 ${formatMonthDay(date)}` : "";
    const eventLabel = `${event._clock}，${CATEGORY_LABELS[category] || category}，${event.title || "未命名事件"}${crossDayText ? `，${crossDayText}` : ""}`;
    const audioCoords = eventAudioCoordinates(event);
    const playBtnHtml = audioCoords
      ? `<button type="button" class="day-event-play-btn" data-job-id="${escapeHtml(audioCoords.jobId)}" data-artifact-path="${escapeHtml(audioCoords.artifactPath)}" data-start="${audioCoords.start}" data-end="${audioCoords.end}" data-default-label="<span class='play-icon'>▶</span> 原音" title="听取该事件现场录音"><span class="play-icon">▶</span> 原音</button>`
      : "";
    return `<div class="day-event-anchor" data-lane="${event._lane}" data-anchor-y="${event._y}" style="top:${event._y}px; --lane:${event._lane}; --lane-count:${laneCount}; --card-shift:0px; z-index:${20 + index}">
      <div class="day-event-node" aria-hidden="true"><span></span></div>
      <article class="day-event-card" aria-label="${escapeHtml(eventLabel)}">
        <div class="day-event-meta">
          <strong>${escapeHtml(event._clock)}${crossDayText ? ` · ${escapeHtml(crossDayText)}` : ""}</strong>
          <span>${escapeHtml(CATEGORY_LABELS[category] || category)}</span>
          <em>${escapeHtml(sourceType)}${escapeHtml(sourceEditHint)}</em>
          ${playBtnHtml}
        </div>
        <div class="day-event-title">${escapeHtml(event.title || "未命名事件")}</div>
        ${renderWorkoutPills(event.data?.exercise)}
        ${event.evidence?.quote ? `<details class="day-event-source"><summary>查看录音原句${escapeHtml(confidenceText)}</summary><p>${escapeHtml(event.evidence.quote)}</p></details>` : ""}
      </article>
    </div>`;
  }).join("");

  let nowMarker = "";
  if (date === shanghaiDate(0)) {
    const nowParts = new Intl.DateTimeFormat("en-GB", {timeZone: "Asia/Shanghai", hour: "2-digit", minute: "2-digit", hour12: false}).formatToParts(new Date());
    const hour = Number(nowParts.find((part) => part.type === "hour")?.value || 0);
    const minute = Number(nowParts.find((part) => part.type === "minute")?.value || 0);
    const nowMinute = hour * 60 + minute;
    if (nowMinute >= startMinute && nowMinute <= endMinute) {
      const top = ((nowMinute - startMinute) / span) * 100;
      nowMarker = `<div class="day-now-marker" style="top:${top}%"><span>现在 ${formatClockPair(hour, minute)}</span></div>`;
    }
  }

  const eventSummary = prepared.length
    ? `<span>首条事件 <strong>${escapeHtml(prepared[0]._clock)}</strong></span><span>末条事件 <strong>${escapeHtml(prepared.at(-1)._clock)}</strong></span><span>共 <strong>${formatCount(prepared.length)}</strong> 条</span>`
    : `<span class="day-axis-recording-only">这一天有录音，但尚未形成正式事件。</span>`;
  const recordingStatus = JOURNAL_LOGIC.recordingCoverageStatus(
    watchCoverageAvailable,
    recordingIntervals.length,
  );
  let liveRecordingBadge = "";
  if (date === shanghaiDate(0) && recordingIntervals.length > 0) {
    const latest = recordingIntervals[recordingIntervals.length - 1];
    const nowMillis = Date.now();
    const latestEnd = Date.parse(latest.ended_at || latest.started_at);
    if (Number.isFinite(latestEnd) && (nowMillis - latestEnd) < 10 * 60 * 1000) {
      liveRecordingBadge = ` · <span class="day-live-badge" style="display:inline-flex; align-items:center; gap:5px; background:rgba(34,197,94,0.15); color:#16a34a; font-weight:600; padding:2px 8px; border-radius:12px; font-size:12px;"><span style="width:7px; height:7px; border-radius:50%; background:#22c55e; animation:live-pulse 1.5s infinite;"></span>手表正在实时录音 (${latest.endLabel})</span>`;
    }
  }
  const recordingSummary = recordingRange
    ? `<span class="day-axis-recording-summary"><i aria-hidden="true"></i>录音覆盖 <strong>${escapeHtml(recordingRange.startLabel)}–${escapeHtml(recordingRange.endLabel)}</strong> · ${recordingRange.intervalCount} 段音频${liveRecordingBadge}</span>`
    : `<span class="day-axis-recording-unavailable">${escapeHtml(recordingStatus)}</span>`;
  const recordingAvailabilityNote = recordingRange && !watchCoverageAvailable
    ? '<span class="day-axis-recording-unavailable">录音覆盖接口未刷新；绿色为最后一次读取区间及已有文件清单</span>'
    : "";
  return `<div class="day-axis-summary">${eventSummary}${recordingSummary}${recordingAvailabilityNote}${sceneTrailHtml}</div>
    <div class="day-axis-canvas" style="height:${axisHeight}px">
      <div class="day-axis-line"></div>
      ${recordingBars}
      <div class="day-scene-layer" role="group" aria-label="场景环境轨迹">${sceneElements}</div>
      ${capsuleElements}
      ${guides.join("")}
      ${phases}
      ${nowMarker}
      ${eventCards}
    </div>`;
}

// 【2026-10-24】切日窗口：父页面的非关键刷新（全库日志重建/审核面板/轮询）不得抢占首屏，
// 否则同源 iframe 与父页面共用主线程，事件卡会被父页面重活拖到 2s+ 才出现。
let journalSwitchingUntil = 0;
let journalHeightFreeze = null;
function journalSwitchActive() { return Date.now() < journalSwitchingUntil; }

function freezeJournalSwitchHeight(date) {
  const dashboard = document.getElementById("journal-day-view") || $("journal-dashboard");
  const frame = dashboard?.querySelector(".day-timeline-frame");
  if (!dashboard || !frame) return;
  const dashboardHeight = Math.round(dashboard.getBoundingClientRect().height);
  const frameHeight = Math.round(frame.getBoundingClientRect().height);
  if (dashboardHeight <= 0) return;
  if (journalHeightFreeze) {
    clearTimeout(journalHeightFreeze.timer);
    clearTimeout(journalHeightFreeze.checkTimer);
  }
  // Hold the old visual box while the iframe swaps dates; data-ready releases
  // it once, so a switch can cause at most one parent height update.
  dashboard.style.height = `${dashboardHeight}px`;
  dashboard.style.overflow = "hidden";
  if (frameHeight >= 200) {
    frame.style.height = `${frameHeight}px`;
    frame._heightFrozen = true;
  }
  journalHeightFreeze = {
    date: String(date || ""),
    dashboard,
    frame,
    startedAt: Date.now(),
    timer: setTimeout(() => releaseJournalSwitchHeight(date), 4000),
    checkTimer: null,
  };
}

function scheduleJournalSwitchHeightRelease(date) {
  const freeze = journalHeightFreeze;
  if (!freeze || freeze.date !== String(date || "")) return;
  clearTimeout(freeze.checkTimer);
  freeze.checkTimer = setTimeout(() => {
    if (journalHeightFreeze !== freeze) return;
    let cards = 0;
    try {
      const doc = freeze.frame?.contentDocument;
      cards = doc?.querySelectorAll(".row").length || 0;
      if (doc?.documentElement?.dataset.renderedDate !== freeze.date) cards = 0;
    } catch (_) { cards = 0; }
    if (cards > 0) {
      releaseJournalSwitchHeight(freeze.date);
    } else if (Date.now() - freeze.startedAt < 4000) {
      scheduleJournalSwitchHeightRelease(freeze.date);
    }
  }, 250);
}

function releaseJournalSwitchHeight(date) {
  const freeze = journalHeightFreeze;
  if (!freeze || (date && freeze.date !== String(date || ""))) return;
  clearTimeout(freeze.timer);
  clearTimeout(freeze.checkTimer);
  const frame = freeze.frame;
  if (frame?.isConnected) {
    let next = 0;
    try {
      const doc = frame.contentDocument;
      const content = doc?.querySelector("#app");
      next = content ? Math.ceil(content.getBoundingClientRect().height) : (doc?.body?.scrollHeight || 0);
    } catch (_) { /* cross-document fallback below */ }
    frame._heightFrozen = false;
    if (next >= 200) {
      next = Math.min(next + 8, 6000);
      frame.style.height = `${next}px`;
      frame._lastHeight = next;
    }
  }
  freeze.dashboard.style.height = "";
  freeze.dashboard.style.overflow = "";
  journalHeightFreeze = null;
}

function selectJournalDate(date) {
  freezeJournalSwitchHeight(date);
  journalSwitchingUntil = Date.now() + 2500;
  if (typeof LifeTimelineData !== "undefined") LifeTimelineData.selectDate(date);
  // 切日期后，旧日期的 pending 定时器必须停掉：否则它会在新日期上触发刷新，
  // 把旧结果写回当前视图。
  if (typeof cancelJournalPendingPolling === "function") cancelJournalPendingPolling(date);
  journalSelectionInitialized = true;
  journalUserSelectedDate = String(date || "");
  journalDateGeneration += 1;
  selectedJournalDate = date;
  writeStorageVersioned("localStorage", "life-logger-journal-date", date);
  document.querySelectorAll(".day-chip, .quick-day").forEach((button) => {
    const active = button.dataset.date === date;
    button.classList.toggle("active", active);
    if (active) button.setAttribute("aria-current", "date");
    else button.removeAttribute("aria-current");
  });
  // A date selection is a read, not a source refresh. Canonical hydration owns
  // this day's request; keep other dates cached and leave artifact rebuilding
  // to the existing refresh/review flow.
  renderJournalNavigation();
}
window.selectJournalDate = selectJournalDate;
if (typeof window !== "undefined") {
  window.dayPrimaryCount = dayPrimaryCount;
  window.ensureJournalActivityTimeline = ensureJournalActivityTimeline;
}

const dailyHealthState = {payload: null, loadedAt: 0, loading: null};
const DAILY_HEALTH_MAX_AGE = 60_000;

function dailyHealthEntryStatus(entry) {
  if (!entry || entry.has_synthesis !== true) {
    return {key: "missing", label: "缺失", dot: "is-missing", detail: entry?.last_attempt?.reason || "没有日报产物"};
  }
  const rawTier = entry.model_tier === null || entry.model_tier === undefined ? "" : String(entry.model_tier).trim();
  const tier = rawTier.toLowerCase();
  const knownTiers = new Set(["primary", "tier1", "tier2", "tier3", "local-fallback"]);
  if (!tier || !knownTiers.has(tier)) {
    return {key: "unknown", label: "档位未知", dot: "is-unknown", detail: `档位未知（model_tier=${rawTier || "null"}）`};
  }
  if (tier === "primary") {
    return {key: "ok", label: "正常", dot: "is-ok", detail: rawTier};
  }
  return {key: "degraded", label: "降级", dot: "is-degraded", detail: entry.fallback_reason || `模型档位 ${rawTier}`};
}

function renderDailyHealthBar() {
  const bar = document.getElementById("daily-health-bar");
  if (!bar) return;
  const payload = dailyHealthState.payload;
  if (!payload || !Array.isArray(payload.days_detail)) { bar.hidden = true; return; }
  const entries = payload.days_detail.slice().reverse();
  const missing = Number(payload.missing_count || 0);
  const degraded = Number(payload.degraded_count || 0);
  const unknown = entries.filter((entry) => dailyHealthEntryStatus(entry).key === "unknown").length;
  const overall = missing > 0 ? "is-missing" : (degraded > 0 ? "is-degraded" : (unknown > 0 ? "is-unknown" : "is-ok"));
  const dots = entries.map((entry) => {
    const status = dailyHealthEntryStatus(entry);
    const title = `${entry.date} · ${status.label} · ${status.detail} · tier=${entry.model_tier || "—"}`;
    return `<span class="daily-health-dot ${status.dot}" title="${escapeHtml(title)}" aria-label="${escapeHtml(title)}"></span>`;
  }).join("");
  const rows = entries.map((entry) => {
    const status = dailyHealthEntryStatus(entry);
    return `<div class="daily-health-row"><time>${escapeHtml(String(entry.date).slice(5))}</time><span class="daily-health-dot ${status.dot}" aria-hidden="true"></span><strong>${status.label}</strong><span class="daily-health-tier">${escapeHtml(String(entry.model_tier || "—"))}</span><span class="daily-health-reason">${escapeHtml(status.detail)}</span></div>`;
  }).join("");
  bar.hidden = false;
  const unknownText = unknown > 0 ? ` · 未知 ${unknown} 天` : "";
  bar.innerHTML = `<button type="button" class="daily-health-summary" data-daily-health-toggle aria-expanded="false"><span class="daily-health-dot ${overall}" aria-hidden="true"></span><strong>日报健康</strong><span class="daily-health-meta">近 ${payload.days || 14} 天：缺 ${missing} 天 · 降级 ${degraded} 天${unknownText}</span><span class="daily-health-chevron" aria-hidden="true">▾</span></button><div class="daily-health-dots" aria-hidden="true">${dots}</div><div class="daily-health-list" hidden>${rows}</div>`;
}

async function refreshDailyHealth(force = false) {
  if (!force && dailyHealthState.payload && Date.now() - dailyHealthState.loadedAt < DAILY_HEALTH_MAX_AGE) {
    renderDailyHealthBar();
    return;
  }
  if (dailyHealthState.loading) return dailyHealthState.loading;
  dailyHealthState.loading = (async () => {
    try {
      const payload = await api("/api/daily-health?days=14", {signal: viewAbortSignal("today")}, {silent: true, operation: "读取日报健康"});
      if (!payload || !Array.isArray(payload.days_detail)) throw new Error("invalid daily health");
      dailyHealthState.payload = payload;
      dailyHealthState.loadedAt = Date.now();
      renderDailyHealthBar();
    } catch (error) {
      if (error.aborted) return;
      dailyHealthState.payload = null;
      dailyHealthState.loadedAt = 0;
      const bar = document.getElementById("daily-health-bar");
      if (bar) bar.hidden = true;
    } finally {
      dailyHealthState.loading = null;
    }
  })();
  return dailyHealthState.loading;
}
window.refreshDailyHealth = refreshDailyHealth;

document.addEventListener("click", (event) => {
  const toggle = event.target.closest("[data-daily-health-toggle]");
  if (!toggle) return;
  const bar = toggle.closest(".daily-health-bar");
  const list = bar?.querySelector(".daily-health-list");
  if (!list) return;
  const open = list.hidden;
  list.hidden = !open;
  toggle.setAttribute("aria-expanded", String(open));
  const chevron = toggle.querySelector(".daily-health-chevron");
  if (chevron) chevron.textContent = open ? "▴" : "▾";
});

function preserveJournalDayTransientState(previousDays, nextDays) {
  const previous = new Map((previousDays || []).filter((day) => day?.date).map((day) => [day.date, day]));
  for (const day of nextDays || []) {
    const old = previous.get(day.date);
    if (!old) continue;
    if (old._initialPrefetchQueued) day._initialPrefetchQueued = true;
    // 【2026-10-29 预取 202 修复】重试计数与后台标记必须跨 day 对象重建保留：
    // 导航重绘会重建 day 对象，若 attempts 归零，有界重试会退化成无限重试。
    if (old._pendingAttempts) day._pendingAttempts = old._pendingAttempts;
    if (old._backgroundPrefetch) day._backgroundPrefetch = true;
    if (old._pendingBackground) day._pendingBackground = true;
    if (old.canonical && !journalCanonicalCache.has(day.date)) {
      journalInitialPrefetchAttempted.delete(day.date);
    }
    if (old.canonicalError && !day.canonicalError) day.canonicalError = old.canonicalError;
    if (old.canonicalPending && !day.canonicalPending) day.canonicalPending = old.canonicalPending;
    // 【2026-10-29 A】整体重建的 day 对象（如实时会话迟到合并 app.js:4374）没有
    // canonical/eventCount；不保留就会让日角标从真实计数退回“未加载 · 选择查看”。
    // 只在**缓存仍持有该日 payload**时保留：缓存被正常失效（invalidateJournalCanonicalCache）
    // 时不得用内存里的旧值撑住计数，否则“失效→回退未加载”的语义会被堵死。
    if (old.canonical && !day.canonical && journalCanonicalCache.has(day.date)) {
      day.canonical = old.canonical;
    }
    if (journalCanonicalCache.has(day.date)
        && old.eventCount !== undefined && old.eventCount !== null
        && (day.eventCount === undefined || day.eventCount === null)) {
      day.eventCount = old.eventCount;
    }
    if (old.activityTimeline && !day.activityTimeline) day.activityTimeline = old.activityTimeline;
    if (old._activityTimelineAttempted && !day.activityTimeline) day._activityTimelineAttempted = true;
    if (old._canonicalLoading && !day._canonicalLoading) day._canonicalLoading = old._canonicalLoading;
    if (old._activityTimelineLoading && !day._activityTimelineLoading) day._activityTimelineLoading = old._activityTimelineLoading;
    // Only preserve an attempted flag for a day that never hydrated. A
    // previously canonical day whose cache was invalidated must be allowed
    // one bounded fresh prefetch instead of reverting to “未加载”.
    if (old._canonicalAttempted && !old.canonical) day._canonicalAttempted = true;
  }
}

function hydrateJournalDaysFromCache() {
  for (const day of journalDays) {
    const cached = journalCanonicalCache.get(day.date);
    if (cached?.payload) {
      try {
        hydrateJournalDayFromCanonical(day, cached.payload);
        day._canonicalAttempted = true;
      } catch (_) {
        // A malformed cached payload must not block a fresh read later.
      }
    }
    const actCached = journalActivityTimelineCache.get(day.date);
    if (actCached?.payload) {
      day.activityTimeline = actCached.payload;
      day._activityTimelineAttempted = true;
    }
  }
}

function journalInitialShellDay(date) {
  return {
    date,
    entries: [],
    _shell: true,
    canonical: false,
    eventCount: null,
    structuredEventCount: null,
    expense: null,
    derivedEventCount: 0,
    derivedStructuredEventCount: 0,
    derivedExpenseDiagnostic: 0,
    derivedCategories: {},
    categories: {},
    reviewCount: 0,
    reviewTotalCount: 0,
    reviewCompletedCount: 0,
    reviewPartialCount: 0,
    selfCount: 0,
    pendingReviewCount: 0,
    reviewTargets: [],
    timeline: [],
    recordingIntervals: [],
    watchCoverageAvailable: journalWatchCoverageAvailable,
  };
}

function ensureInitialJournalDays() {
  const today = shanghaiDate(0);
  const yesterday = shanghaiDate(-1);
  for (const date of [today, yesterday]) {
    if (!journalDays.some((day) => day.date === date)) {
      journalDays.push(journalInitialShellDay(date));
    }
  }
  journalDays.sort((a, b) => String(b.date || "").localeCompare(String(a.date || "")));
  return {
    today: journalDays.find((day) => day.date === today),
    yesterday: journalDays.find((day) => day.date === yesterday),
  };
}

function startInitialJournalPrefetch() {
  const {today, yesterday} = ensureInitialJournalDays();
  const requestDay = (day) => {
    if (!day || day.canonical || day._canonicalLoading
      || journalInitialPrefetchAttempted.has(day.date)) {
      return Promise.resolve();
    }
    journalInitialPrefetchAttempted.add(day.date);
    day._initialPrefetchQueued = true;
    // 【2026-10-29 预取 202 修复】后台预取的标志必须比单次请求活得久：
    // 202 重试是由定时器发起的，那时 _initialPrefetchQueued 已经清掉了。
    day._backgroundPrefetch = true;
    return Promise.resolve(ensureJournalDaySynthesis(day)).catch(() => {}).finally(() => {
      day._initialPrefetchQueued = false;
    });
  };
  if (!journalInitialPrefetchStarted) {
    journalInitialPrefetchStarted = true;
    if (today && !today.canonical) today._initialPrefetchQueued = true;
    if (yesterday && yesterday !== today && !yesterday.canonical) yesterday._initialPrefetchQueued = true;
    // Today is foreground; yesterday is one bounded background count prefetch.
    // No history-wide fan-out and no change of the selected day on late replies.
    requestDay(today).finally(() => requestDay(yesterday));
    return;
  }
  // A later refresh may have rebuilt day objects after cache invalidation.
  // Resume at most one bounded attempt for today/yesterday.
  requestDay(today);
  requestDay(yesterday);
}

function renderJournalNavigation() {
  const root = $("journal-dashboard");
  setJournalLoading(false, "日记已更新");
  if (typeof ensureInitialJournalDays === "function") ensureInitialJournalDays();
  if (typeof startInitialJournalPrefetch === "function") startInitialJournalPrefetch();
  if (!journalDays.length) {
    const pendingDate = selectedJournalDate || shanghaiDate(0);
    const emptyJournal = journalWatchCoverageAvailable
      ? `<div class="journal-empty"><strong>还没有生活记录</strong><span>处理第一份录音后，这里会自动形成按日期整理的记录。全库抽取待确认仍可从下方进入。</span></div>`
      : `<div class="journal-empty" role="status"><strong>录音覆盖数据暂不可用</strong><span>Watch 录音清单这次没有读取成功；这不代表设备没有录音，请稍后刷新。全库抽取待确认仍可从下方进入。</span></div>`;
    root.innerHTML = `${emptyJournal}${renderPendingReviewBlock(pendingDate)}`;
    return;
  }
  const today = shanghaiDate(0);
  const yesterday = shanghaiDate(-1);
  const latest = journalDays[0].date;
  const availableDates = new Set(journalDays.map((day) => day.date));
  // A user-selected date is authoritative even if a late refresh found a newer
  // "today" shell. Only fall back to latest before any explicit selection.
  const explicitDate = typeof journalUserSelectedDate !== "undefined" ? journalUserSelectedDate : "";
  const target = typeof journalNavigationTarget === "function"
    ? journalNavigationTarget({
      selectedDate: selectedJournalDate,
      explicitDate,
      selectionInitialized: journalSelectionInitialized,
      dates: availableDates,
      latest,
      initialDate: journalInitialDate,
    })
    : (explicitDate || selectedJournalDate || latest);
  if (!explicitDate && !journalSelectionInitialized) journalSelectionInitialized = true;
  selectedJournalDate = target;
  if (target) localStorage.setItem("life-logger-journal-date", target);
  if (typeof activeView === "string" && activeView === "today") {
    const pageTitle = $("page-title");
    const pageKicker = $("page-kicker");
    const word = target === today ? "今天" : (target === yesterday ? "昨天" : "当天");
    if (pageTitle) pageTitle.textContent = word;
    if (pageKicker) pageKicker.textContent = target === today ? "TODAY" : "PAST DAY";
  }

  // 【2026-10-14 修】日期轴统一为**一条连续倒序列表**（今天 → 昨天 → 更早…）。
  // 旧实现把「今天/昨天/最近」抽成快捷大卡、其余日期另起一条「最近记录」chips，后果有三：
  //   ① 同一批日期被切成两种组件、两套视觉语言；
  //   ② 「最近记录」这条的第一项其实是 10-12 —— 真正最近的两天被抽走了，
  //      标签名与内容自相矛盾（用户：「为什么把昨天独立出来」）；
  //   ③ 时间轴 14 → 13 →[断层]→ 12 读起来是断的。
  // 现在：一条轴、一种组件，今天/昨天只加一个角标，不再另立门户。
  const journalDayByDate = new Map(journalDays.map((day) => [day.date, day]));
  const dayBadgeByDate = new Map([[today, "今天"], [yesterday, "昨天"]]);
  const orderedJournalDates = [...new Set([today, yesterday, ...journalDays.map((day) => day.date)])]
    .filter(Boolean)
    .sort((a, b) => (a < b ? 1 : a > b ? -1 : 0));

  const dayButtons = orderedJournalDates
    .map((date) => {
      const day = journalDayByDate.get(date);
      const badge = dayBadgeByDate.get(date) || "";
      // 只有当前选中日会被主动补全 canonical 计数；其他日期不能永久伪装成
      // 正在加载。分别用读取中 / 读取失败 / 未加载表达真实状态，
      // canonical 已加载的日期才显示实际事件数。
      const actTimelineReady = day?.activityTimeline?.status === "ready" && Array.isArray(day.activityTimeline.activities);
      // 【2026-10-29 预取 202 修复】服务端还在算（202/pending）时角标只能说“读取中”；
      // 自动重试耗尽后说“读取超时 · 点击重试”；绝不能在服务端还在算时说“未加载”。
      const canonicalPending = day?.canonical ? null : day?.canonicalPending;
      // [2026-11-04 Q853] 口径统一：日角标的"活动数"只有 /api/activity-timeline 一个来源。
      // 未加载时不报活动数，不许拿事件轨的条数（11-03：事件 1 条 vs 活动 91 个）冒充活动数。
      const countLabel = actTimelineReady
        ? `${day.activityTimeline.activities.length} 个活动`
        : (day?.canonical
            ? (day.eventCount ? `${day.eventCount} 条事件` : (day.recordingIntervals?.length ? "" : "选择查看"))
            : day?.canonicalError
              ? "读取失败"
              : date === target || day?._canonicalLoading || day?._initialPrefetchQueued
                || (canonicalPending && !canonicalPending.exhausted)
                ? "读取中"
                : canonicalPending
                  ? "读取超时 · 点击重试"
                  : day
                    ? (day.recordingIntervals?.length ? "" : "选择查看")
                    : "无记录");
      const summary = day
        ? ([
            countLabel,
            day.recordingIntervals?.length ? `${day.recordingIntervals.length} 段录音` : "",
            !day.watchCoverageAvailable ? "覆盖未刷新" : "",
          ].filter(Boolean).join(" · ")
          || JOURNAL_LOGIC.recordingCoverageStatus(day.watchCoverageAvailable !== false, 0))
        : countLabel;
      // friendlyDate 已自带「今天/昨天」前缀，aria-label 不再重复角标
      const label = summary;
      const badgeHtml = badge ? `<em class="day-chip-badge">${badge}</em>` : "";
      return `<button class="day-chip${badge ? " day-chip-today" : ""} ${date === target ? "active" : ""}" data-date="${date}" aria-label="${escapeHtml(`${friendlyDate(date)}，${label}`)}" ${date === target ? 'aria-current="date"' : ""} onclick="selectJournalDate('${date}')">${date.slice(5)}${badgeHtml}<small>${escapeHtml(summary)}</small></button>`;
    })
    .join("");

  const strip = root.querySelector?.(".days-strip");
  if (strip && root.querySelector("#journal-day-view")) strip.innerHTML = dayButtons;
  else root.innerHTML = `<div class="journal-nav"><div class="days-strip" role="group" aria-label="按日期查看记录（由新到旧）">${dayButtons}</div></div><div id="daily-health-bar" class="daily-health-bar" hidden></div><div id="journal-day-view"></div>`;
  if (!root.querySelector("#daily-health-bar")) {
    const bar = document.createElement("div");
    bar.id = "daily-health-bar";
    bar.className = "daily-health-bar";
    bar.hidden = true;
    root.querySelector(".journal-nav")?.insertAdjacentElement("afterend", bar);
  }
  renderJournalDay(journalDays.find((item) => item.date === target));
  renderDailyHealthBar();
  refreshDailyHealth().catch(() => {});
}

let activeReviewTab = "voice";

function reviewPendingDays() {
  return journalDays
    .filter((day) => (day.pendingReviewCount || 0) > 0)
    .sort((a, b) => b.date.localeCompare(a.date));
}

function reviewVoiceCount() {
  return reviewPendingDays().reduce((sum, day) => sum + Number(day.reviewCount || 0), 0);
}

function reviewSourceCountInfo() {
  const payload = pendingReviewState.payload;
  if (!payload) {
    return pendingReviewState.error
      ? {value: null, text: "读取失败"}
      : {value: null, text: "…"};
  }
  const counts = pendingOpenCounts(
    payload.totals || {},
    Array.isArray(payload.groups) ? payload.groups : [],
    Boolean(pendingReviewState.allDates),
  );
  const raw = pendingReviewState.allDates ? counts.globalActionable : counts.scopedActionable;
  const value = raw === null || raw === undefined || !Number.isFinite(Number(raw)) ? null : Number(raw);
  return {value, text: value === null ? "未知" : pendingReviewNumber(value)};
}

// 抽取待确认默认只看当前日期：这一天没有，不代表全部日期都没有。
function reviewEmptyText() {
  const payload = pendingReviewState.payload;
  const counts = payload && !pendingReviewState.allDates
    ? pendingOpenCounts(payload.totals || {}, Array.isArray(payload.groups) ? payload.groups : [], false)
    : null;
  const total = Number(counts?.globalActionable);
  return total > 0
    ? `这一天没有要判断的。其他日期还有 ${pendingReviewNumber(total)} 条，可在“今天”页切到对应日期或选“显示全部日期”。`
    : "目前没有需要我判断的内容。";
}

function updateReviewBadges(voiceCount = reviewVoiceCount()) {
  const source = reviewSourceCountInfo();
  const title = `声源待确认 ${formatCount(voiceCount)} / 抽取待确认 ${source.text}（两条队列分开计数，未相加）`;
  const showBadge = voiceCount > 0 || (source.value !== null && source.value > 0);
  const badgeText = voiceCount > 0 ? formatCount(voiceCount) : (showBadge ? "!" : "0");
  const badge = document.getElementById("review-nav-count");
  if (badge) {
    badge.textContent = badgeText;
    badge.classList.toggle("hidden", !showBadge);
    badge.title = title;
  }
  document.querySelectorAll?.(".mobile-review-badge").forEach((item) => {
    item.textContent = badgeText;
    item.classList.toggle("hidden", !showBadge);
    item.title = title;
  });
}

function updateReviewTabCounts() {
  const voice = document.getElementById("review-tab-voice-count");
  if (voice) voice.textContent = formatCount(reviewVoiceCount());
  const source = reviewSourceCountInfo();
  const sourceCount = document.getElementById("review-tab-source-count");
  if (sourceCount) {
    sourceCount.textContent = source.text;
    sourceCount.title = `当前范围的抽取待确认 ${source.text} 条`;
  }
  updateReviewBadges();
}

function reviewTabsHtml(voiceCount) {
  const source = reviewSourceCountInfo();
  const voiceActive = activeReviewTab !== "source";
  return `<div class="settings-tabs review-tabs" role="tablist" aria-label="需要我判断的两类队列">
    <button id="review-tab-voice" class="settings-tab${voiceActive ? " active" : ""}" data-review-tab="voice" type="button" role="tab" aria-selected="${voiceActive ? "true" : "false"}" aria-controls="review-voice-panel" tabindex="${voiceActive ? "0" : "-1"}" onclick="setReviewTab('voice')">声源待确认 <span id="review-tab-voice-count" class="nav-badge">${formatCount(voiceCount)}</span></button>
    <button id="review-tab-source" class="settings-tab${voiceActive ? "" : " active"}" data-review-tab="source" type="button" role="tab" aria-selected="${voiceActive ? "false" : "true"}" aria-controls="review-source-panel" tabindex="${voiceActive ? "-1" : "0"}" onclick="setReviewTab('source')">抽取待确认 <span id="review-tab-source-count" class="nav-badge">${escapeHtml(source.text)}</span></button>
  </div>`;
}

function ensurePendingReviewStatsForTabs() {
  const target = String(selectedJournalDate || shanghaiDate(0));
  const payloadDate = String(pendingReviewState.payload?.scope?.date || "");
  const scopeMatches = pendingReviewState.allDates ? !payloadDate : payloadDate === target;
  if (!pendingReviewState.payload || Date.now() - pendingReviewState.loadedAt > 60_000 || !scopeMatches) {
    if (!pendingReviewState.inflight) refreshPendingReviewBlock(target);
  }
}

function setReviewTab(tab) {
  activeReviewTab = tab === "source" ? "source" : "voice";
  renderReviewDashboard();
}
if (typeof window !== "undefined") window.setReviewTab = setReviewTab;

const judgmentState = {purpose: "", older: false, payload: null, loading: false, loadedAt: 0, error: "", notice: "", undo: null};

async function loadJudgmentQueue(force = false) {
  if (judgmentState.loading || (!force && Date.now() - judgmentState.loadedAt < 60_000)) return;
  judgmentState.loading = true;
  renderJudgmentPanel();
  try {
    const query = new URLSearchParams();
    if (judgmentState.purpose) query.set("purpose", judgmentState.purpose);
    if (judgmentState.older) query.set("older", "1");
    judgmentState.payload = await api(`/api/judgment-queue?${query}`, {}, {force: true, silent: true});
    judgmentState.error = "";
    judgmentState.loadedAt = Date.now();
  } catch (error) {
    judgmentState.error = error.message;
  } finally {
    judgmentState.loading = false;
    renderJudgmentPanel();
  }
}

const JUDGMENT_MEAL_LABELS = {breakfast: "早餐", lunch: "午餐", dinner: "晚餐", snacks: "加餐", supplements: "补剂", unknown: "餐次未说明"};

function judgmentCardHtml(item) {
  const sid = escapeHtml(String(item.source_id || ""));
  const buttons = (item.options || []).map((option) =>
    `<button type="button" class="judgment-option${(option.op.startsWith("enter_") || option.op === "adopt_candidate") ? " is-primary" : ""}" data-judgment-answer="${escapeHtml(option.op)}" data-source-id="${sid}">${escapeHtml(option.label)}</button>`).join("");
  const moneyInput = item.purpose === "money"
    ? `<label class="judgment-amount">金额（元）<input type="number" inputmode="decimal" min="0" step="0.01" value="${item.suggested_amount ?? ""}" data-judgment-amount="${sid}"></label>`
    : "";
  // [2026-11-07] 热量待定：显示食物名 + 候选克数/热量；「我补重量」需要一个克数输入框。
  const candidate = item.purpose === "calories" && item.candidate ? item.candidate : null;
  const calorieMeta = item.purpose === "calories"
    ? `<p class="judgment-meta">${escapeHtml(String(item.item || "未命名食物"))} · ${candidate ? `候选 ${candidate.grams != null ? `${escapeHtml(String(candidate.grams))} 克 · ` : ""}${escapeHtml(String(candidate.calories_kcal))} 千卡` : "没有候选热量"}${item.meal ? ` · ${escapeHtml(JUDGMENT_MEAL_LABELS[String(item.meal)] || String(item.meal))}` : ""}</p>`
    : "";
  const weightInput = item.purpose === "calories"
    ? `<label class="judgment-amount">克数<input type="number" inputmode="decimal" min="0" step="1" value="" placeholder="${candidate && candidate.grams != null ? escapeHtml(String(candidate.grams)) : ""}" data-judgment-grams="${sid}"></label>`
    : "";
  return `<article class="judgment-card">
    <p class="judgment-quote">${escapeHtml(String(item.quote || "（无原文）"))}</p>
    <p class="judgment-meta">${escapeHtml(String(item.date || "日期未知"))} ${escapeHtml(String(item.time || ""))}</p>
    ${calorieMeta}
    ${moneyInput}
    ${weightInput}
    <div class="judgment-options">${buttons}</div>
  </article>`;
}

function judgmentLostHtml(rows) {
  if (!rows?.length) return "";
  return `<section class="judgment-lost"><h3>以前丢失的记录（${formatCount(rows.length)}）</h3>
    <p class="judgment-note">当时没说时间，没进账本。恢复后回到原来那一天，已冻结的账也会变，可以撤销。</p>
    ${rows.map((row) => {
      const key = `data-job="${escapeHtml(row.job_id)}" data-event="${escapeHtml(row.event_id)}"`;
      const money = row.amount === null || row.amount === undefined ? "没有金额" : `¥${Number(row.amount).toFixed(2)}`;
      return `<article class="judgment-card">
        <p class="judgment-quote">${escapeHtml(row.title || row.quote || "")}</p>
        <p class="judgment-meta">${escapeHtml(row.date)} · ${money}</p>
        <div class="judgment-options">
          <button type="button" class="judgment-option is-primary" data-judgment-manual="restore" ${key}>恢复</button>
          <button type="button" class="judgment-option" data-judgment-manual="dismiss" ${key}>不恢复</button>
        </div></article>`;
    }).join("")}</section>`;
}

function judgmentDuplicatesHtml(rows) {
  if (!rows?.length) return "";
  return `<section class="judgment-lost"><h3>可能记了两次（${formatCount(rows.length)}）</h3>
    <p class="judgment-note">你手动记了一笔，录音里也识别到同一天同金额的一笔，现在两笔都算在账里。</p>
    ${rows.map((row) => {
      const key = `data-event="${escapeHtml(row.event_id)}" data-date="${escapeHtml(row.date)}"`;
      return `<article class="judgment-card">
        <p class="judgment-quote">手动：${escapeHtml(row.title)} ¥${Number(row.amount).toFixed(2)}</p>
        <p class="judgment-meta">${escapeHtml(row.date)} ${escapeHtml(row.matched_time)} 录音：${escapeHtml(row.matched_title)}</p>
        <div class="judgment-options">
          <button type="button" class="judgment-option is-primary" data-judgment-duplicate="same" ${key}>是同一笔，只算一次</button>
          <button type="button" class="judgment-option" data-judgment-duplicate="separate" ${key}>确实是两笔</button>
        </div></article>`;
    }).join("")}</section>`;
}

function renderJudgmentPanel() {
  const root = $("judgment-panel");
  if (!root) return;
  const data = judgmentState.payload;
  if (!data) {
    root.innerHTML = judgmentState.error
      ? `<p class="judgment-note">待判断内容读取失败：${escapeHtml(judgmentState.error)}</p>`
      : '<p class="judgment-note">正在按用途整理待判断内容……</p>';
    return;
  }
  const recentTotal = data.purposes.reduce((sum, row) => sum + row.recent_open, 0);
  const olderTotal = data.purposes.reduce((sum, row) => sum + row.older_open, 0);
  const cards = data.purposes.map((row) => `<button type="button" class="judgment-purpose${judgmentState.purpose === row.key ? " is-active" : ""}" data-judgment-purpose="${escapeHtml(row.key)}" aria-pressed="${judgmentState.purpose === row.key}">
    <span class="judgment-purpose-head"><strong>${escapeHtml(row.label)}</strong><em>${formatCount(row.recent_open)}</em></span>
    <small>为什么问：${escapeHtml(row.why)}</small>
    <small>答完：${escapeHtml(row.effect)}</small>
  </button>`).join("");
  const wheel = data.flywheel || {};
  const undo = judgmentState.undo ? '<button type="button" class="judgment-undo" data-judgment-undo="1">撤销</button>' : "";
  root.innerHTML = `<h2 class="judgment-title">按用途判断</h2>
    <p class="judgment-note">最近 7 天 ${formatCount(recentTotal)} 条，更早 ${formatCount(olderTotal)} 条。数字是最近 7 天的条数。</p>
    <div class="judgment-purposes">${cards}</div>
    <p class="judgment-note">已写进数据飞轮：回答 ${formatCount(wheel.answers || 0)}，声纹样本 ${formatCount(wheel.voice_samples || 0)}，媒体负样本 ${formatCount(wheel.negatives || 0)}，更正 ${formatCount(wheel.corrections || 0)}。</p>
    ${judgmentState.notice ? `<p class="judgment-notice" role="status">${escapeHtml(judgmentState.notice)} ${undo}</p>` : ""}
    <div class="judgment-list">${data.items.length ? data.items.map(judgmentCardHtml).join("") : '<p class="judgment-note">这一类最近没有要判断的。</p>'}</div>
    ${olderTotal ? `<button type="button" class="judgment-more" data-judgment-older="1">${judgmentState.older ? "只看最近 7 天" : `也看更早的 ${formatCount(olderTotal)} 条`}</button>` : ""}
    ${judgmentDuplicatesHtml(data.possible_duplicates)}
    ${judgmentLostHtml(data.lost_manual)}`;
}

async function submitJudgment(path, body, notice, undo = null) {
  judgmentState.notice = "正在保存……";
  renderJudgmentPanel();
  try {
    await api(path, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
    judgmentState.notice = notice;
    judgmentState.undo = undo;
    await loadJudgmentQueue(true);
    return true;
  } catch (error) {
    judgmentState.notice = `保存失败：${error.message}`;
    judgmentState.undo = null;
    renderJudgmentPanel();
    return false;
  }
}

$("judgment-panel")?.addEventListener("click", (event) => {
  const target = event.target?.closest?.("button");
  if (!target) return;
  if (target.dataset.judgmentPurpose) {
    judgmentState.purpose = judgmentState.purpose === target.dataset.judgmentPurpose ? "" : target.dataset.judgmentPurpose;
    loadJudgmentQueue(true);
  } else if (target.dataset.judgmentOlder) {
    judgmentState.older = !judgmentState.older;
    loadJudgmentQueue(true);
  } else if (target.dataset.judgmentAnswer) {
    const operation = target.dataset.judgmentAnswer;
    const sourceId = target.dataset.sourceId;
    const body = {source_id: sourceId, operation, op_id: `op_${Date.now()}_${Math.random().toString(36).slice(2, 7)}`};
    if (operation === "enter_expense") {
      const input = [...document.querySelectorAll("[data-judgment-amount]")].find((node) => node.dataset.judgmentAmount === sourceId);
      body.amount = input?.value;
    }
    if (operation === "enter_weight") {
      // [2026-11-07] 「我补重量」：克数随回答一起提交，后端按候选的每克热量折算。
      const input = [...document.querySelectorAll("[data-judgment-grams]")].find((node) => node.dataset.judgmentGrams === sourceId);
      body.grams = input?.value;
    }
    const notices = {enter_expense: "已入账。", enter_todo: "已加入待办。", uncertain: "已保留，仍在待判断里。",
      adopt_candidate: "已按常规份量算进今天的饮食。", enter_weight: "已按你补的重量算进今天的饮食。", skip: "已记下：这条不算热量。"};
    const keep = operation === "uncertain";
    submitJudgment("/api/judgment-queue/answer", body, notices[operation] || "已记录，并写进数据飞轮。", keep ? null : {source_id: sourceId, op_id: body.op_id});
  } else if (target.dataset.judgmentManual) {
    const action = target.dataset.judgmentManual;
    const body = {job_id: target.dataset.job, event_id: target.dataset.event, action};
    submitJudgment("/api/judgment-queue/manual", body, action === "restore" ? "已恢复到原来那一天。" : "已标记为不恢复。", {manual: {...body, action: "undo"}});
  } else if (target.dataset.judgmentDuplicate) {
    const action = target.dataset.judgmentDuplicate;
    const body = {event_id: target.dataset.event, date: target.dataset.date, action};
    submitJudgment("/api/judgment-queue/duplicate", body, action === "same" ? "已排除手动那笔，只算一次。" : "已记下，两笔都算。", {duplicate: {...body, action: "undo"}});
  } else if (target.dataset.judgmentUndo && judgmentState.undo) {
    const undo = judgmentState.undo;
    if (undo.manual) submitJudgment("/api/judgment-queue/manual", undo.manual, "已撤销。");
    else if (undo.duplicate) submitJudgment("/api/judgment-queue/duplicate", undo.duplicate, "已撤销。");
    else submitJudgment("/api/judgment-queue/undo", undo, "已撤销。");
  }
});

function renderReviewDashboard() {
  const root = $("review-dashboard");
  if (!root) return 0;
  if (typeof loadJudgmentQueue === "function") loadJudgmentQueue();
  root.setAttribute("aria-busy", "false");
  const pendingDays = reviewPendingDays();
  const unknownCount = pendingDays.reduce((sum, day) => sum + Number(day.reviewCount || 0), 0);
  const pendingCount = unknownCount;
  ensurePendingReviewStatsForTabs();
  const tabs = reviewTabsHtml(pendingCount);
  const partialCount = pendingDays.reduce((sum, day) => sum + (day.reviewPartialCount || 0), 0);
  const completedCount = journalDays.reduce(
    (sum, day) => sum + (day.reviewCompletedCount || 0),
    0,
  );
  const reviewKindMeta = {
    unknown: ["普通声源", "开始判断 →", ""],
  };
  const cards = pendingDays.flatMap((day) => (day.reviewTargets || []).map((target) => {
    const [label, action, suffix] = reviewKindMeta[target.kind] || [target.kind, "打开 →", ""];
    return `<a class="review-day-card" role="listitem" aria-label="${escapeHtml(`${friendlyDate(day.date)}，${label}，${formatCount(target.count)} 条`)}" href="${viewerLink(target.jobId, target.path)}${suffix}">
      <div><span>${escapeHtml(friendlyDate(day.date))} · ${escapeHtml(label)}</span><strong>${formatCount(target.count)} 条</strong></div>
      <span class="review-open">${escapeHtml(action)}</span>
    </a>`;
  })).join("");
  const voicePanel = pendingDays.length
    ? `<div id="review-voice-panel" role="tabpanel" aria-labelledby="review-tab-voice"><div class="review-summary"><div><span>声源待确认</span><strong>${formatCount(pendingCount)}</strong><small>普通 ${formatCount(unknownCount)}${partialCount ? ` · ${formatCount(partialCount)} 条进行中` : ""}</small></div><p>声源判断已完成 ${formatCount(completedCount)} 条。抽取待确认是另一条队列，切到上方标签查看，计数不会自动相加。</p></div>
      <div class="review-day-list" role="list" aria-label="声源待确认日期与类型">${cards}</div></div>`
    : `<div id="review-voice-panel" role="tabpanel" aria-labelledby="review-tab-voice" class="review-empty"><div class="review-empty-icon" aria-hidden="true">✓</div><strong>目前没有待确认的声源片段</strong><span>抽取待确认在另一个标签页，单独计数；这里不把两条队列合并成“统一”数量。</span></div>`;
  const sourcePanel = `<div id="review-source-panel" role="tabpanel" aria-labelledby="review-tab-source">${renderPendingReviewBlock(selectedJournalDate || shanghaiDate(0))}</div>`;
  root.innerHTML = `${tabs}${activeReviewTab === "source" ? sourcePanel : voicePanel}`;
  updateReviewTabCounts();
  return pendingCount;
}

async function refreshJournal(jobs, force = false, {loadDetails = false} = {}) {
  // 切日进行中且不是用户显式刷新：让路给时间轴首屏。
  if (!force && journalSwitchActive()) return;
  if (force) journalReviewCache.clear();
  if (force) journalArtifactScanCache.clear();
  if (force) invalidateJournalCanonicalCache();
  const jobFingerprint = jobs.map((job) => `${job.job_id}:${job.status}:${(job.results || []).join("|")}`).join(";");
  const selectionGeneration = typeof journalDateGeneration === "number" ? journalDateGeneration : 0;
  const reviewStatus = $("review-action-status");
  const reviewRoot = $("review-dashboard");
  if (reviewRoot) {
    reviewRoot.setAttribute("aria-busy", "true");
    if (!reviewRoot.children.length) {
      reviewRoot.innerHTML = '<div class="review-empty"><div class="review-empty-icon">···</div><strong>正在整理声源待确认内容</strong><span>会优先展示真正需要你判断的片段，不会把技术日志混进来。</span></div>';
    }
  }
  if (reviewStatus) {
    reviewStatus.className = "setting-status checking";
    reviewStatus.textContent = "正在整理声源待确认内容……";
  }
  try {
    const fetchedCoverage = await loadWatchCoverageIntervals();
    const watchCoverage = JOURNAL_LOGIC.watchCoverageState(
      journalWatchCoverageIntervals,
      fetchedCoverage,
    );
    journalWatchCoverageIntervals = watchCoverage.intervals;
    journalWatchCoverageAvailable = watchCoverage.available;
    const coverageFingerprint = `${watchCoverage.available}:` + watchCoverage.intervals
      .map((item) => `${item.device_id}:${item.session_id}:${item.sequence}:${item.start_at}:${item.end_at}`)
      .join(";");
    const fingerprint = `${jobFingerprint};watch-coverage:${coverageFingerprint}`;
    const hadPreviousFingerprint = Boolean(journalFingerprint && fingerprint !== journalFingerprint);
    if (!force && fingerprint === journalFingerprint && !loadDetails) {
      const pendingCount = renderReviewDashboard();
      markJournalRefreshSuccess();
      if (reviewStatus) {
        reviewStatus.className = "setting-status ready";
        const sourceInfo = typeof reviewSourceCountInfo === "function" ? reviewSourceCountInfo() : {value: null};
        reviewStatus.textContent = pendingCount
          ? "需要我判断的内容已是最新；声源与抽取分开计数。"
          : (sourceInfo.value ? "声源待确认已清空；抽取待确认仍有条目。" : (typeof reviewEmptyText === "function" ? reviewEmptyText() : "目前没有需要我判断的内容。"));
      }
      return;
    }
    const entries = await loadJournalEntries(jobs);
    // A user date selection invalidates an older bulk refresh. Do not let its
    // snapshot repaint the newly selected day or retarget the iframe.
    const refreshStillCurrent = typeof journalRefreshStillCurrent === "function"
      ? journalRefreshStillCurrent(selectionGeneration)
      : true;
    if (!refreshStillCurrent) return;
    const previousJournalDays = journalDays;
    journalDays = groupJournalDays(
      entries,
      watchCoverage.intervals,
      watchCoverage.available,
    );
    if (typeof preserveJournalDayTransientState === "function") preserveJournalDayTransientState(previousJournalDays, journalDays);
    if (typeof hydrateJournalDaysFromCache === "function") hydrateJournalDaysFromCache();
    if (typeof ensureInitialJournalDays === "function") ensureInitialJournalDays();
    if (typeof startInitialJournalPrefetch === "function") startInitialJournalPrefetch();
    journalFingerprint = fingerprint;
    renderJournalNavigation();
    if (!force && hadPreviousFingerprint && typeof shanghaiDate === "function") {
      const todayKey = shanghaiDate(0);
      const todayDay = (journalDays || []).find((d) => d && d.date === todayKey);
      if (todayDay && typeof ensureJournalDaySynthesis === "function") {
        ensureJournalDaySynthesis(todayDay, {force: true});
      }
      if (todayDay && typeof ensureJournalActivityTimeline === "function") {
        ensureJournalActivityTimeline(todayDay, {force: true});
      }
    }
    if (typeof activeView === "string" && activeView === "kanban") renderKanban();
    const pendingCount = renderReviewDashboard();
    markJournalRefreshSuccess();
    if (reviewStatus) {
      reviewStatus.className = "setting-status ready";
      const sourceInfo = typeof reviewSourceCountInfo === "function" ? reviewSourceCountInfo() : {value: null};
      reviewStatus.textContent = pendingCount
        ? "需要我判断的内容已更新；声源与抽取分开计数。"
        : (sourceInfo.value ? "声源待确认已清空；抽取待确认仍有条目。" : (typeof reviewEmptyText === "function" ? reviewEmptyText() : "目前没有需要我判断的内容。"));
    }
  } catch (error) {
    if (reviewRoot && !journalDays.length) {
      reviewRoot.innerHTML = `<div class="review-empty"><div class="review-empty-icon">!</div><strong>声源待确认内容暂时读取失败</strong><span>${escapeHtml(error.message)}。已有录音和结果不会受影响，可以稍后重新刷新。</span></div>`;
    }
    if (reviewStatus) {
      reviewStatus.className = "setting-status error";
      reviewStatus.textContent = `声源待确认内容读取失败：${error.message}`;
    }
    throw error;
  } finally {
    if (reviewRoot) reviewRoot.setAttribute("aria-busy", "false");
  }
}

const RESULT_DEFINITIONS = {
  "daily_summary.json": {
    title: "当日总结与时间轴",
    description: "建议先看这里：自动合并已提取事件与人工确认事件，保持和当日事件视图一致。",
    badge: "首选",
    order: 10,
  },
  "self-transcript.json": {
    title: "我的发言",
    description: "声源归属判断为本人（self）的语音转写，是生活事件提取的主要依据。",
    badge: "本人语音",
    order: 20,
  },
  "events.json": {
    title: "生活事件明细",
    description: "从本人语音里提取出的结构化生活事件，包含时间、类别、证据和置信度。",
    badge: "结构化",
    order: 30,
  },
  "unknown-review.json": {
    title: "待人工复核",
    description: "声源暂时无法确定的内容。需要抽查时优先看这里。",
    badge: "待核对",
    order: 40,
  },
  "transcript.json": {
    title: "完整转写",
    description: "这份录音的完整 ASR 文本，包含本人、他人和媒体内容。",
    badge: "全文",
    order: 50,
  },
  "non-self-transcript.json": {
    title: "非本人 / 媒体语音",
    description: "被声源归属排除的语音，用来检查直播、视频、游戏或他人讲话是否被正确过滤。",
    badge: "已过滤",
    order: 60,
  },
};

function artifactFileName(path) {
  return path.split("/").pop() || path;
}

function artifactDate(path) {
  const match = path.match(/\/result\/(\d{4}-\d{2}-\d{2})\//);
  return match ? match[1] : null;
}

function artifactSourceFile(job, path) {
  const match = path.match(/^files\/([^/]+)\//);
  if (!match) return null;
  return (job.files || []).find((file) => file.file_id === match[1]) || null;
}

function renderResults(job, artifacts) {
  const paths = job.results || [];
  const extra = Array.isArray(artifacts) ? artifacts.filter((p) => typeof p === "string" && p) : [];
  if (!paths.length && extra.length) {
    // status.json 的 results 漏记但磁盘有产物（degraded/partial 常见）：只列出实际存在的产物，不伪造正式结果。
    const links = extra.map((path) => `<a href="${viewerLink(job.job_id, path)}" target="_blank" rel="noopener"><span>${escapeHtml(artifactFileName(path))}</span><small>${escapeHtml(path.includes("/result/") ? "处理产物" : "技术文件")}</small></a>`).join("");
    return `<div class="results-panel" data-artifacts-source="disk">
      <div class="results-heading"><div><h3>任务产物（${extra.length}）</h3><p>这次没有登记正式结果条目，以下是磁盘上实际存在的产物。</p></div></div>
      <details class="technical-results" open><summary>全部产物（${extra.length}）</summary><div class="technical-links">${links}</div></details>
    </div>`;
  }
  if (!paths.length) return `<div class="result-empty">结果会在处理完成后出现在这里</div>`;

  const finalPaths = paths.filter((path) => path.includes("/result/"));
  const mainResults = finalPaths
    .map((path) => ({path, definition: RESULT_DEFINITIONS[artifactFileName(path)]}))
    .filter((item) => item.definition)
    .sort((a, b) => a.definition.order - b.definition.order);
  const mainPathSet = new Set(mainResults.map((item) => item.path));
  const technicalPaths = paths.filter((path) => !mainPathSet.has(path));

  const cards = mainResults.map(({path, definition}) => {
    const sourceFile = artifactSourceFile(job, path);
    const date = artifactDate(path);
    const context = [sourceFile?.relative_path, date].filter(Boolean).join(" · ");
    return `<a class="result-card" href="${viewerLink(job.job_id, path)}">
      <div class="result-card-top"><strong>${escapeHtml(definition.title)}</strong><span class="result-badge">${escapeHtml(definition.badge)}</span></div>
      <p>${escapeHtml(definition.description)}</p>
      ${context ? `<span class="result-context">${escapeHtml(context)}</span>` : ""}
      <span class="result-open">打开查看 →</span>
    </a>`;
  }).join("");

  const technical = technicalPaths.map((path) => {
    const scope = path.includes("/asr/") ? "ASR 中间产物" : "技术文件";
    return `<a href="${resultLink(job.job_id, path)}" target="_blank" rel="noopener"><span>${escapeHtml(artifactFileName(path))}</span><small>${escapeHtml(scope)}</small></a>`;
  }).join("");

  return `<div class="results-panel">
    <div class="results-heading">
      <div><h3>处理结果</h3><p>先看“当日总结与时间轴”；需要核对识别效果时再看其他入口。</p></div>
    </div>
    <div class="result-grid">${cards || `<div class="result-empty">正式结果仍在生成中。</div>`}</div>
    ${technicalPaths.length ? `<details class="technical-results"><summary>原始与技术文件（${technicalPaths.length}）</summary><div class="technical-links">${technical}</div></details>` : ""}
  </div>`;
}

function aggregateStageState(job, stage) {
  const files = job.files || [];
  if (!files.length) return "pending";
  const states = files.map((file) => file.stages?.[stage]?.status || "pending");
  if (states.some((value) => value === "failed")) return "failed";
  if (states.some((value) => value === "running")) return "running";
  if (states.every((value) => value === "completed")) return "completed";
  return "pending";
}

function renderPipeline(job) {
  const useJev = Boolean(job.processing_options?.use_jev);
  const steps = [
    {key: "asr", label: "语音转写", state: aggregateStageState(job, "asr")},
    {key: "attribution", label: "声源归属", state: aggregateStageState(job, "attribution")},
  ];
  const providerLabel = decisionProviderLabel(job.processing_options?.decision_provider || "jev");
  if (useJev) steps.push({key: "jev", label: `${providerLabel} 判断`, state: aggregateStageState(job, "jev")});
  steps.push({
    key: "done",
    label: "完成",
    state: job.status === "completed" ? "completed" : (job.status === "failed" ? "failed" : "pending"),
  });
  const currentIndex = steps.findIndex((step) => step.state === "running");
  return `<div class="pipeline-state">
    <div class="pipeline-state-head"><span>处理链路</span><strong>${useJev ? `本地主链 + ${providerLabel} · 阈值 ${formatFixed2(Number(job.processing_options?.jev_min_confidence || .72))}` : "纯本地主链"}</strong></div>
    <div class="pipeline-steps">${steps.map((step, index) => {
      const active = step.state === "running" || (currentIndex < 0 && step.state === "pending" && steps.slice(0, index).every((value) => value.state === "completed"));
      return `<div class="pipeline-step ${step.state} ${active ? "active" : ""}">
        <span class="pipeline-dot">${step.state === "completed" ? "✓" : (step.state === "failed" ? "!" : index + 1)}</span>
        <span>${escapeHtml(step.label)}</span>
      </div>`;
    }).join("")}</div>
  </div>`;
}

function contextExtractionPaths(job) {
  return (job?.results || []).filter((p) => typeof p === "string" && p.includes("context-extraction"));
}

function contextRecoveryStatusMarkup(jobOrRecovery) {
  const rec = jobOrRecovery?.context_recovery || jobOrRecovery;
  if (!rec) return "";
  if (rec.status === "running") {
    const comp = rec.completed_files || 0;
    const tot = rec.target_files || 0;
    const bComp = rec.batch_progress?.completed || 0;
    const bTot = rec.batch_progress?.total || 0;
    return `<div class="context-recovery-status running">正在重试事件提取 · 录音 ${comp}/${tot} · 提取批次 ${bComp}/${bTot}</div>`;
  }
  if (rec.status === "failed") {
    return `<div class="context-recovery-status failed">${escapeHtml(rec.error || "提取失败")}</div>`;
  }
  return "";
}

function contextAutoRetryStatusMarkup(jobOrRetry) {
  const retry = jobOrRetry?.context_auto_retry || jobOrRetry;
  if (!retry) return "";
  if (retry.status === "completed") return "自动恢复完成";
  if (retry.status === "needs_review") return "仍有内容需要确认";
  if (retry.status === "stopped") return "自动恢复已停止";
  return "";
}

async function loadContextExtractionSummaries(job) {
  if (!job) return;
  const panels = typeof document !== "undefined" && document.querySelectorAll
    ? document.querySelectorAll(`[data-context-job="${job.job_id}"]`)
    : [];
  for (const panel of panels) {
    const body = (panel.querySelector && panel.querySelector(".context-recovery-body")) || panel;
    if (!body) continue;
    const rec = job.context_recovery || {};
    const autoRetry = job.context_auto_retry;
    const isBusy = rec.status === "running" || autoRetry?.status === "scheduled";
    let autoNotice = "";
    if (autoRetry?.status === "scheduled") {
      autoNotice = "<p class=\"context-auto-scheduled\">后台将在短暂等待后自动恢复</p>";
    }
    let archivesHtml = "";
    if (rec.archived_artifacts?.length) {
      archivesHtml = "<div class=\"context-archive-notice\">重试前的事件提取记录已归档</div>";
    }
    const disabledAttr = isBusy ? 'disabled aria-busy="true"' : "";
    body.innerHTML = `
      <div class="context-recovery-desc">复用ASR和声源归属结果</div>
      ${rec.extraction_status === "degraded" ? "<p>仍有内容待确认</p>" : ""}
      ${autoNotice}
      ${archivesHtml}
      <button type="button" class="context-recovery-button" data-context-recovery-job="${escapeHtml(job.job_id)}" ${disabledAttr}>重试事件提取</button>
    `;
    const btn = body.querySelector ? body.querySelector(".context-recovery-button") : null;
    if (btn && typeof btn.addEventListener === "function") {
      btn.addEventListener("click", () => retryContextExtraction(btn));
    }
  }
}

function timeSourceLabel(source) {
  return ({user: "用户指定", sidecar: "旁车文件", metadata: "媒体元数据", filename: "文件名", filesystem: "文件时间", unavailable: "不可用"})[source] || source || "未知";
}

function precisionLabel(precision) {
  return ({exact: "精确", inferred: "推断", relative: "相对时间", unknown: "未知"})[precision] || precision || "未知";
}

function recordingSourceLabel(file) {
  if (file?.capture_source === "ai-passport-wifi") {
    return `AI Passport · ${file.source_device || "device"} · ${file.source_session || "session"}`;
  }
  return "";
}

function decisionRuntimeLabel(job) {
  if (!job?.processing_options?.use_jev) return "关闭";
  const runtime = job.decision_runtime || {};
  const requested = String(runtime.requested_provider || job.processing_options?.decision_provider || "jev");
  const effective = String(runtime.effective_provider || requested);
  if (runtime.fallback && requested !== effective) {
    return `${decisionProviderLabel(requested)} → ${decisionProviderLabel(effective)} 回退`;
  }
  return `${decisionProviderLabel(effective)} 已启用`;
}

function setCurrentJobLiveStatus(state, label, detail = "") {
  const chip = $("current-job-live-status");
  if (!chip) return;
  chip.textContent = label;
  chip.className = `current-live-status ${state}`;
  chip.title = detail || label;
}

function renderCurrentJobLiveStatus(job) {
  const status = String(job?.status || "");
  if (["running", "queued"].includes(status)) {
    setCurrentJobLiveStatus("ready", "实时更新 · 1.5s", "正在自动轮询最新任务状态");
  } else if (status === "completed") {
    setCurrentJobLiveStatus("ready", "处理完成", "任务已完成，自动轮询已停止");
  } else if (status === "failed") {
    setCurrentJobLiveStatus("error", "处理失败", "任务处理失败，可从断点重新启动");
  } else if (status === "interrupted") {
    setCurrentJobLiveStatus("warning", "已中断", "任务已中断，可从断点重新启动");
  } else if (status === "draft") {
    setCurrentJobLiveStatus("off", "等待开始", "任务尚未开始处理");
  } else {
    setCurrentJobLiveStatus("checking", statusLabel(status || "unknown"));
  }
}

function setCurrentJobLoading(isBusy, message = "") {
  const card = $("current-card");
  const root = $("current-job");
  const status = $("current-job-load-status");
  const refresh = $("refresh-button");
  if (card) card.setAttribute("aria-busy", isBusy ? "true" : "false");
  if (status && message) status.textContent = message;
  if (refresh) refresh.disabled = Boolean(isBusy);
  if (isBusy) setCurrentJobLiveStatus("checking", "正在刷新");
  if (isBusy && root) {
    root.innerHTML = '<div class="journal-empty" role="status"><strong>正在读取任务</strong><span>加载处理状态、文件进度和结果……</span></div>';
  }
}


// 【2026-10-25】partial/degraded 的失败原因只在 stage_errors 里（error 为 null），
// 之前页面只渲染 job.error → 用户只看到“部分完成/有降级”却看不到原因。
// 这里只做展示：不改状态、不重试、不修历史任务。
const STAGE_ERROR_STATUSES = new Set(["partial", "degraded", "failed", "interrupted"]);

function stageErrorReasonText(value) {
  const raw = String(value || "").split("\n")[0].trim();
  if (!raw) return "";
  // 不新增展示内部绝对路径 / 令牌：路径只留末段，token 一律遮蔽。
  const withoutToken = raw.replace(/\b(bearer\s+)?[A-Za-z0-9_-]{24,}\b/gi, "…");
  const withoutAbsPath = withoutToken.replace(/(?:\/(?:[^\s/]+)){3,}/g, (m) => "…/" + m.split("/").filter(Boolean).pop());
  return withoutAbsPath.length > 160 ? withoutAbsPath.slice(0, 157) + "…" : withoutAbsPath;
}

function normalizeStageErrors(stageErrors) {
  const groups = new Map();
  (Array.isArray(stageErrors) ? stageErrors : []).forEach((entry) => {
    if (!entry || entry.resolved === true) return;             // 已解决的旧错误不当作当前失败
    const stage = String(entry.stage || entry.kind || "").trim();
    if (!stage) return;
    const reason = stageErrorReasonText(entry.error || entry.message || "");
    const key = stage + "|" + (entry.file_id || "") + "|" + reason;
    if (groups.has(key)) return;                               // 同一文件同一阶段的重复条目只算一处
    groups.set(key, { stage, reason, fileId: String(entry.file_id || "") });
  });
  const list = [...groups.values()];
  const files = new Set(list.map((item) => item.fileId).filter(Boolean));
  return { list, count: list.length, fileCount: files.size };
}

function renderStageErrorBlock(job) {
  if (job && job.error) return "";                             // error 优先，避免两处重复
  const summary = normalizeStageErrors(job && job.stage_errors);
  if (summary.count) {
    const rows = summary.list.map((item) => {
      const stage = escapeHtml(stageLabel(item.stage));
      const reason = item.reason ? escapeHtml(item.reason) : "未提供具体原因";
      return `<li><strong>${stage}</strong> · ${reason}</li>`;
    }).join("");
    const fileNote = summary.fileCount && summary.fileCount !== summary.count
      ? `（涉及 ${summary.fileCount} 个文件）` : "";
    return `<div class="stage-errors" data-stage-errors="present"><div class="stage-errors-title">失败阶段 ${summary.count} 处${fileNote}</div><ul>${rows}</ul></div>`;
  }
  if (job && STAGE_ERROR_STATUSES.has(String(job.status || ""))) {
    return `<div class="stage-errors muted" data-stage-errors="missing">失败原因：未提供</div>`;
  }
  return "";
}

function renderJob(job) {
  currentJobId = job.job_id || currentJobId;
  $("current-card")?.classList.remove("hidden");
  setCurrentJobLoading(false, `任务 ${job.job_id || ""} 已更新`);
  renderCurrentJobLiveStatus(job);
  const files = job.files || [];
  const progress = job.progress || {completed: 0, total: files.length};
  const ratio = progress.total ? Math.min(100, Math.max(0, progress.completed / progress.total * 100)) : 0;
  const progressScale = ratio / 100;
  const results = renderResults(job);
  const logs = (job.logs || []).slice(-30).join("\n");
  const fileRows = files.map((file) => {
    const fileProgress = file.progress || {};
    const absoluteTime = file.effective_start_at || file.inferred_start_at || "";
    const time = shanghaiDateTimeLabel(absoluteTime, "未获得绝对时间");
    const source = recordingSourceLabel(file);
    return `<div class="file-row"><div class="file-main"><div class="file-name">${escapeHtml(formatHumanRecordingTitle(job, file))}</div><div class="file-meta">${source ? `${escapeHtml(source)} · ` : ""}${escapeHtml(timeSourceLabel(file.time_source))} · ${escapeHtml(time)} · ${escapeHtml(precisionLabel(file.time_precision))}</div></div><div class="file-meta">${escapeHtml(stageLabel(file.current_stage))} · ${fileProgress.completed || 0}/${fileProgress.total || 0}</div></div>`;
  }).join("");
  const error = job.error ? `<div class="error">${escapeHtml(job.error)}</div>` : renderStageErrorBlock(job);
  const restart = ["failed", "interrupted"].includes(job.status) ? `<button class="primary" onclick="restartJob('${job.job_id}')">从断点重新启动</button>` : "";
  $("current-job").innerHTML = `<div class="status-line"><span class="status ${job.status}">${escapeHtml(statusLabel(job.status))}</span><span class="muted">已运行 ${formatElapsed(job.elapsed_seconds)}</span></div>${renderPipeline(job)}<div class="progress" role="progressbar" aria-label="任务处理进度" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${Math.round(ratio)}"><div style="transform:scaleX(${progressScale})"></div></div><div class="details"><div class="detail"><span class="muted">当前阶段</span><strong>${escapeHtml(stageLabel(job.current_stage))}</strong></div><div class="detail"><span class="muted">文件</span><strong>${progress.completed || 0}/${progress.total || 0}</strong></div><div class="detail"><span class="muted">语音模型</span><strong>${escapeHtml(job.processing_options?.asr_model || "mlx-whisper-turbo")}</strong></div><div class="detail"><span class="muted">声源模型</span><strong>${escapeHtml(job.processing_options?.speaker_model || "cam++")}</strong></div><div class="detail"><span class="muted">时间覆盖</span><strong>${escapeHtml(job.manual_start_at || "文件名时间 / 未知")}</strong></div><div class="detail"><span class="muted">决策增强</span><strong>${escapeHtml(decisionRuntimeLabel(job))}</strong></div></div>${error}<div class="selected-files">${fileRows || "还没有文件"}</div><div id="job-results-slot" data-job-id="${escapeHtml(job.job_id || "")}">${results}</div><details class="logs-details"><summary>运行日志</summary><div class="logs">${escapeHtml(logs || "暂无日志")}</div></details>${restart}`;
  hydrateJobArtifactResults(job);
}

const jobArtifactCache = new Map();      // jobId → 成功取到的产物数组（可能是空数组）
const jobArtifactInflight = new Map();   // jobId → 进行中的请求（去重，避免反复请求）

function renderJobArtifactSlot(slot, job, artifacts) {
  if (!slot || !job) return;
  if (!Array.isArray(artifacts)) {
    slot.innerHTML = `<div class="result-empty" data-artifacts-source="error">产物读取失败（服务未响应），可重新打开任务重试。</div>`;
    return;
  }
  if (!artifacts.length) {
    slot.innerHTML = `<div class="result-empty" data-artifacts-source="none">这个任务没有可查看的产物。</div>`;
    return;
  }
  slot.innerHTML = renderResults(job, artifacts);
}

async function hydrateJobArtifactResults(job) {
  if (!job || job.job_id !== currentJobId) return null;
  if ((job.results || []).length) return null;             // 有正式结果就不重复扫描
  if (!["degraded", "partial", "failed", "completed"].includes(String(job.status || ""))) return null;
  const slot = $("job-results-slot");
  if (!slot || slot.dataset.jobId !== job.job_id) return null;
  // 已成功取过：直接渲染到当前 DOM（切走再回来也能立刻恢复入口）。
  if (jobArtifactCache.has(job.job_id)) {
    const cached = jobArtifactCache.get(job.job_id);
    const fresh = $("job-results-slot");
    if (fresh && fresh.dataset.jobId === job.job_id) renderJobArtifactSlot(fresh, job, cached);
    return cached;
  }
  let pending = jobArtifactInflight.get(job.job_id);
  if (!pending) {
    pending = scanJobArtifactPaths(job);
    jobArtifactInflight.set(job.job_id, pending);
    pending.then((list) => {
      jobArtifactInflight.delete(job.job_id);
      if (Array.isArray(list) && !list.__failed) jobArtifactCache.set(job.job_id, list);  // 只缓存成功
    }).catch(() => { jobArtifactInflight.delete(job.job_id); });
  }
  const artifacts = await pending;
  const failed = Boolean(artifacts && artifacts.__failed);
  // 迟到响应：只有当当前 slot 仍属于这个 job 时才写 DOM，避免串任务。
  const still = $("job-results-slot");
  if (!still || job.job_id !== currentJobId || still.dataset.jobId !== job.job_id) return null;
  renderJobArtifactSlot(still, job, failed ? null : artifacts);
  return failed ? null : artifacts;
}

function jobContentSignature(job) {
  const hashes = (job.files || []).map((file) => file.sha256).filter(Boolean).sort();
  if (!hashes.length) return null;
  const options = normalizedProcessingOptions(job.processing_options || {});
  return [
    hashes.join(","),
    options.asr_model,
    options.speaker_model,
  ].join("|");
}

function collapseDuplicateJobs(jobs) {
  const priority = {running: 5, queued: 4, completed: 3, interrupted: 2, failed: 1, draft: 0};
  const collapsed = new Map();
  for (const job of jobs) {
    const signature = jobContentSignature(job);
    if (!signature) {
      collapsed.set(`job:${job.job_id}`, {...job, _duplicate_attempt_count: 1});
      continue;
    }
    const current = collapsed.get(signature);
    if (!current) {
      collapsed.set(signature, {...job, _duplicate_attempt_count: 1});
      continue;
    }
    const count = Number(current._duplicate_attempt_count || 1) + 1;
    const currentRank = priority[current.status] ?? 0;
    const candidateRank = priority[job.status] ?? 0;
    const candidateWins = candidateRank > currentRank
      || (candidateRank === currentRank
        && String(job.updated_at || "") > String(current.updated_at || ""));
    if (candidateWins) collapsed.set(signature, {...job, _duplicate_attempt_count: count});
    else current._duplicate_attempt_count = count;
  }
  return [...collapsed.values()];
}
async function restartJob(jobId) {
  const status = $("upload-status");
  if (status) status.textContent = "正在重新启动任务……";
  setImportLiveStatus("checking", "正在重新启动", jobId);
  try {
    await api(`/api/jobs/${encodeURIComponent(jobId)}/restart`, {method: "POST"});
    await refreshAll();
    beginPolling();
    if (status) status.textContent = "任务已重新启动，页面会继续刷新进度。";
    setImportLiveStatus("ready", "任务已重新启动", jobId);
  } catch (error) {
    if (status) status.textContent = `重新启动失败：${error.message}`;
    setImportLiveStatus("error", "重新启动失败", error.message);
  }
}
async function retryContextExtraction(button) {
  const jobId = button?.dataset?.contextRecoveryJob;
  if (!jobId) return;
  button.disabled = true;
  if (typeof button.setAttribute === "function") {
    button.setAttribute("aria-busy", "true");
  }
  try {
    const res = await api(`/api/jobs/${jobId}/retry-context-extraction`, {method: "POST"});
    if (typeof renderJob === "function") renderJob(res);
    if (typeof beginPolling === "function") beginPolling();
  } catch (err) {
    button.disabled = false;
    if (typeof button.removeAttribute === "function") {
      button.removeAttribute("aria-busy");
    }
  }
}

function setJobsLiveStatus(state, label, detail = "") {
  const chip = $("jobs-live-status");
  if (!chip) return;
  chip.textContent = label;
  chip.className = `setting-badge ${state || ""}`.trim();
  chip.title = detail || label;
  chip.setAttribute("aria-label", detail ? `${label}：${detail}` : label);
}

function setJobsLoading(isBusy, message = "") {
  const root = $("jobs-list");
  const status = $("jobs-load-status");
  const refresh = $("jobs-refresh");
  if (root) root.setAttribute("aria-busy", isBusy ? "true" : "false");
  if (status && message) status.textContent = message;
  if (refresh) refresh.disabled = Boolean(isBusy);
  if (isBusy) setJobsLiveStatus("", "读取中");
}

function jobHistoryLocalDate(job) {
  const firstFile = job?.files?.[0] || null;
  return journalSourceFileLocalDate(firstFile, null)
    || journalShanghaiDateFromTimestamp(job?.created_at, "")
    || "";
}

async function refreshAll(forceJournal = false) {
  // 切日窗口内不做全库刷新（/api/jobs 是数 MB 级，JSON 解析会卡住 iframe 渲染）。
  if (!forceJournal && journalSwitchActive()) return;
  const root = $("jobs-list");
  setJobsLoading(true, "正在读取任务历史");
  let payload;
  let taskPayload;
  try {
    [payload, taskPayload] = await Promise.all([api("/api/jobs", {}, {force: Boolean(forceJournal)}), api("/api/tasks")]);
  } catch (error) {
    if (root) {
      root.innerHTML = `<div class="journal-empty" role="status"><strong>任务读取失败</strong><span>${escapeHtml(error.message)}</span></div>`;
    }
    setJobsLoading(false, "任务历史读取失败");
    setJobsLiveStatus("error", "读取失败", error.message);
    markJournalRefreshFailure(error);
    throw error;
  }
  lastJobs = payload.jobs || [];
  taskStates = new Map((taskPayload.tasks || []).map((task) => [task.task_key, task]));
  const visibleJobs = collapseDuplicateJobs(lastJobs)
    .filter((job) => (job.files || []).length || job.status !== "draft");
  if (root) {
    if (!visibleJobs.length) {
      root.innerHTML = '<div class="journal-empty" role="status"><strong>还没有任务</strong><span>导入录音后，处理任务会出现在这里。</span></div>';
    } else {
      mountRows(root, visibleJobs.map((job) => {
        const firstFile = job.files?.[0];
        const title = formatHumanRecordingTitle(job, firstFile);
        const date = jobHistoryLocalDate(job);
        const mode = job.processing_options?.use_jev ? decisionRuntimeLabel(job) : "本地";
        const source = recordingSourceLabel(firstFile);
        const duplicateNote = Number(job._duplicate_attempt_count || 1) > 1
          ? ` · ${formatCount(job._duplicate_attempt_count)} 次相同任务已折叠`
          : "";
        const row = cloneRow("job-list-item", '<div class="job-list-item" role="listitem"><button class="job-row" type="button"><span class="job-title"><strong></strong><br><span class="muted"></span></span><span class="job-mode"></span><span class="muted"></span></button></div>');
        row.querySelector("button").addEventListener("click", () => selectJob(job.job_id));
        setRowText(row, ".job-title strong", title);
        setRowText(row, ".job-title .muted", `${source ? `${source} · ` : ""}${formatDayKey(date)} · ${formatCount(job.files?.length || 0)} 个文件 · ${statusLabel(job.status)}${duplicateNote}`);
        setRowText(row, ".job-mode", mode);
        setRowText(row, ".job-row > .muted", formatElapsed(job.elapsed_seconds));
        return row;
      }));
    }
  }
  setJobsLoading(false, `任务历史已更新，共 ${formatCount(visibleJobs.length)} 个任务`);
  setJobsLiveStatus("ready", visibleJobs.length ? `${formatCount(visibleJobs.length)} 个任务` : "无任务");
  await refreshJournal(payload.jobs || [], forceJournal);
  if (currentJobId) {
    const current = payload.jobs?.find((job) => job.job_id === currentJobId);
    if (current) renderJob(current);
  }
}

function showSyncToast(message, isError = false) {
  let toast = $("sync-toast");
  if (!toast) {
    toast = document.createElement("div");
    toast.id = "sync-toast";
    toast.className = "sync-toast";
    document.body.appendChild(toast);
  }
  toast.textContent = message;
  toast.style.background = isError ? "#c53030" : "#1e2420";
  toast.classList.add("visible");
  setTimeout(() => {
    toast?.classList.remove("visible");
  }, 2500);
}

async function handleSyncNow() {
  const btn = $("sync-now-button");
  if (btn) btn.classList.add("syncing");
  try {
    const results = await Promise.allSettled([
      refreshWatchReception(),
      refreshPassportSessions(),
      refreshAll(true),
    ]);
    // allSettled 永远不 reject，catch 分支进不来；必须逐个检查结果，否则失败会被报成成功。
    const failed = results.find((result) => result.status === "rejected");
    if (failed) {
      const reason = failed.reason;
      showSyncToast(`⚠️ 同步失败：${reason?.message || reason || "未知错误"}`, true);
    } else {
      showSyncToast("✅ 同步完成，已拉取最新录音与任务");
    }
  } catch (err) {
    showSyncToast(`⚠️ 同步失败：${err.message}`, true);
  } finally {
    if (btn) btn.classList.remove("syncing");
  }
}

async function runGlobalRefresh() {
  const button = $("global-refresh");
  const status = $("global-refresh-status");
  if (button) {
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
    button.textContent = "刷新中…";
  }
  if (status) status.textContent = "正在刷新任务和日记";
  try {
    await refreshAll(true);
    if (status) status.textContent = "任务和日记已刷新";
  } catch (error) {
    if (status) status.textContent = `刷新失败：${error.message}`;
  } finally {
    if (button) {
      button.disabled = false;
      button.setAttribute("aria-busy", "false");
      button.textContent = "刷新";
    }
  }
}

async function selectJob(jobId) {
  currentJobId = jobId;
  writeStorageVersioned("localStorage", "life-logger-current-job", currentJobId);
  if (activeView !== "import") switchView("import");
  $("current-card")?.classList.remove("hidden");
  setCurrentJobLoading(true, `正在读取任务 ${jobId}`);
  try {
    const job = await api(`/api/jobs/${encodeURIComponent(jobId)}`);
    renderJob(job);
    if (["running", "queued"].includes(job.status)) beginPolling();
  } catch (error) {
    const root = $("current-job");
    if (root) root.innerHTML = `<div class="journal-empty" role="status"><strong>任务读取失败</strong><span>${escapeHtml(error.message)}</span></div>`;
    setCurrentJobLoading(false, `任务 ${jobId} 读取失败`);
    setCurrentJobLiveStatus("error", "读取失败", error.message);
  }
}
window.selectJob = selectJob;

function beginPolling() {
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(async () => {
    if (!currentJobId) return;
    try {
      const job = await api(`/api/jobs/${encodeURIComponent(currentJobId)}`);
      renderJob(job);
      if (!["running", "queued"].includes(job.status)) { clearInterval(pollTimer); pollTimer = null; }
      await refreshAll();
    } catch (error) {
      const status = $("current-job-load-status");
      if (status) status.textContent = `任务状态刷新失败：${error.message}`;
      setCurrentJobLiveStatus("warning", "刷新失败", error.message);
    }
  }, 1500);
}

$("file-input")?.addEventListener("change", (event) => addFiles(event.target.files));
$("folder-input")?.addEventListener("change", (event) => addFiles(event.target.files));
const manualDropHero = $("manual-drop-hero");
if (manualDropHero) {
  manualDropHero.addEventListener("dragenter", (event) => {
    event.preventDefault();
    setManualDropActive(true);
  });
  manualDropHero.addEventListener("dragover", (event) => {
    event.preventDefault();
    if (event.dataTransfer) event.dataTransfer.dropEffect = "copy";
    setManualDropActive(true);
  });
  manualDropHero.addEventListener("dragleave", (event) => {
    if (!manualDropHero.contains(event.relatedTarget)) setManualDropActive(false);
  });
  manualDropHero.addEventListener("drop", handleManualImportDrop);
}
$("start-button")?.addEventListener("click", createAndStart);
$("save-auto-ingest")?.addEventListener("click", saveAutoIngest);
$("enable-auto-ingest")?.addEventListener("click", enableAutoIngest);
$("scan-auto-ingest")?.addEventListener("click", scanAutoIngestNow);
$("choose-auto-ingest-folder")?.addEventListener("click", chooseAutoIngestFolder);
$("sync-now-button")?.addEventListener("click", handleSyncNow);
$("refresh-button")?.addEventListener("click", refreshAll);
$("jobs-refresh")?.addEventListener("click", refreshAll);
$("journal-refresh")?.addEventListener("click", async () => {
  setJournalLoading(true, "正在刷新日记");
  refreshMobileBrief().catch(() => {});
  try {
    const payload = await api("/api/jobs");
    await refreshJournal(payload.jobs || [], true);
  } catch (error) {
    const root = $("journal-dashboard");
    setJournalLoading(false, "日记刷新失败");
    markJournalRefreshFailure(error);
    if (root && !journalDays.length) {
      root.innerHTML = `<div class="journal-empty" role="status"><strong>日记刷新失败</strong><span>${escapeHtml(error.message)}</span></div>`;
    }
  }
});
$("refresh-stats")?.addEventListener("click", () => refreshStatistics().catch(() => {}));
$("refresh-patterns")?.addEventListener("click", () => refreshPatterns().catch(() => {}));
document.querySelectorAll("[data-pattern-days]").forEach((button) => {
  button.addEventListener("click", () => {
    setPatternRange(Number(button.dataset.patternDays || 14));
    refreshPatterns().catch(() => {});
  });
});
document.querySelectorAll("[data-stats-days]").forEach((button) => {
  button.addEventListener("click", () => {
    setStatisticsRange(Number(button.dataset.statsDays || 7));
    refreshStatistics().catch(() => {});
  });
});
$("search-form")?.addEventListener("submit", (event) => {
  event.preventDefault();
  runGlobalSearch();
});
document.querySelectorAll(".search-filter").forEach((button) => {
  button.addEventListener("click", () => {
    document.querySelectorAll(".search-filter").forEach((item) => {
      const active = item === button;
      item.classList.toggle("active", active);
      item.setAttribute("aria-pressed", active ? "true" : "false");
    });
    if (hasSearchCriteria()) runGlobalSearch();
  });
});
$("search-import-source")?.addEventListener("change", () => runGlobalSearch());
["search-date-from", "search-date-to", "search-category", "search-amount-min", "search-amount-max"].forEach((id) => {
  $(id).addEventListener("change", () => {
    if (hasSearchCriteria()) runGlobalSearch();
  });
});

document.querySelectorAll(".nav-item[data-view]").forEach((button) => {
  button.addEventListener("click", () => switchView(button.dataset.view));
});
$("kanban-date-input")?.addEventListener("change", (event) => selectKanbanDate(event.target.value));
$("kanban-dashboard")?.addEventListener("change", (event) => {
  const checkbox = event.target?.closest?.("input[data-kanban-task-key]");
  if (checkbox) toggleKanbanTodo(checkbox);
});
$("kanban-dashboard")?.addEventListener("click", (event) => {
  const button = event.target?.closest?.("button[data-kanban-promote-key]");
  if (button) promoteKanbanIdea(button);
  const sourceChip = event.target?.closest?.("button[data-kanban-source]");
  if (sourceChip) {
    kanbanSourceFilter = sourceChip.dataset.kanbanSource;
    renderKanban();
  }
  const action = event.target?.closest?.("button[data-kanban-set-key]");
  if (action) {
    action.disabled = true;
    setKanbanTaskStatus(action.dataset.kanbanSetKey, action.dataset.kanbanSetStatus).then((ok) => { if (!ok) action.disabled = false; });
  }
});
$("kanban-dashboard")?.addEventListener("dragstart", (event) => {
  const card = event.target?.closest?.("[data-kanban-drag-key]");
  if (!card) return;
  event.dataTransfer.setData("text/plain", card.dataset.kanbanDragKey);
  event.dataTransfer.effectAllowed = "move";
  card.classList.add("is-dragging");
});
$("kanban-dashboard")?.addEventListener("dragend", (event) => {
  event.target?.closest?.("[data-kanban-drag-key]")?.classList.remove("is-dragging");
});
$("kanban-dashboard")?.addEventListener("dragover", (event) => {
  const lane = event.target?.closest?.("[data-kanban-drop]");
  if (!lane) return;
  event.preventDefault();
  lane.classList.add("is-drop-target");
});
$("kanban-dashboard")?.addEventListener("dragleave", (event) => {
  const lane = event.target?.closest?.("[data-kanban-drop]");
  if (lane && !lane.contains(event.relatedTarget)) lane.classList.remove("is-drop-target");
});
$("kanban-dashboard")?.addEventListener("drop", (event) => {
  const lane = event.target?.closest?.("[data-kanban-drop]");
  if (!lane) return;
  event.preventDefault();
  lane.classList.remove("is-drop-target");
  const taskKey = event.dataTransfer.getData("text/plain");
  if (taskKey) setKanbanTaskStatus(taskKey, lane.dataset.kanbanDrop);
});
document.querySelectorAll("[data-settings-page-target]").forEach((button) => {
  button.addEventListener("click", () => switchSettingsPage(button.dataset.settingsPageTarget));
  button.addEventListener("keydown", handleSettingsTabKeydown);
});
document.querySelectorAll("[data-open-view]").forEach((button) => {
  button.addEventListener("click", () => switchView(button.dataset.openView));
});
document.querySelectorAll(".mobile-nav-btn").forEach((button) => {
  button.addEventListener("click", () => {
    const target = button.dataset.mobileView;
    if (target === "hub") {
      const hubViews = ["import", "history", "settings", "stats", "patterns", "dashboard", "search", "memory-qa", "more"];
      if (!hubViews.includes(activeView)) {
        switchView("review");
      }
    } else {
      switchView(target);
    }
  });
});
let lastGlobalKeyDown = "";
window.addEventListener("keydown", (e) => {
  lastGlobalKeyDown = e.code || e.key || "";
}, true);

document.addEventListener("click", handleInlineViewerLink);
$("inline-viewer-close")?.setAttribute("tabindex", "-1");
$("inline-viewer-close")?.addEventListener("click", (event) => {
  if (lastGlobalKeyDown === "Space" || lastGlobalKeyDown === " " || lastGlobalKeyDown === "spacebar" || lastGlobalKeyDown === "Spacebar") {
    event.preventDefault();
    event.stopPropagation();
    return;
  }
  closeInlineViewer();
});
$("inline-viewer-close")?.addEventListener("keydown", (event) => {
  const isSpace = event.code === "Space" || event.key === " " || event.key === "Spacebar" || event.key === "spacebar" || event.keyCode === 32 || event.which === 32;
  if (isSpace) {
    event.preventDefault();
    event.stopPropagation();
    $("inline-viewer-close")?.blur();
    const frame = $("inline-viewer-frame");
    frame?.focus();
    frame?.contentWindow?.postMessage({type: "life-logger-shortcut-space"}, "*");
  }
});
$("inline-viewer-close")?.addEventListener("keyup", (event) => {
  const isSpace = event.code === "Space" || event.key === " " || event.key === "Spacebar" || event.key === "spacebar" || event.keyCode === 32 || event.which === 32;
  if (isSpace) {
    event.preventDefault();
    event.stopPropagation();
    $("inline-viewer-close")?.blur();
  }
});
$("inline-viewer-open-tab")?.addEventListener("click", openInlineViewerInTab);
window.addEventListener("message", (event) => {
  if (event.origin !== location.origin) return;
  const frameWindow = $("inline-viewer-frame")?.contentWindow;
  if (!frameWindow || event.source !== frameWindow) return;
  if (event.data?.type === "life-logger-data-changed") {
    inlineViewerDirty = true;
    return;
  }
  if (event.data?.type === "life-logger-close-viewer") closeInlineViewer();
});
window.addEventListener("keydown", (event) => {
  const workspace = $("inline-viewer-workspace");
  const isViewerOpen = workspace && !workspace.classList.contains("hidden");
  if (event.key === "Escape" && isViewerOpen) {
    closeInlineViewer();
    return;
  }
  if (!isViewerOpen) return;
  const tag = String(event.target?.tagName || "").toLowerCase();
  if (["input", "textarea", "select"].includes(tag) || event.target?.isContentEditable) return;

  const key = String(event.key || "").toLowerCase();
  const isSpace = event.code === "Space" || key === " " || key === "spacebar" || event.keyCode === 32 || event.which === 32;
  const isDigit1 = event.code === "Digit1" || key === "1" || key === "!";
  const isDigit2 = event.code === "Digit2" || key === "2" || key === "@";
  const isDigit3 = event.code === "Digit3" || key === "3" || key === "#";
  const isDigit4 = event.code === "Digit4" || key === "4" || key === "$";
  const isDown = event.code === "ArrowDown" || key === "j";
  const isUp = event.code === "ArrowUp" || key === "k";

  if (isSpace) {
    event.preventDefault();
    event.stopPropagation();
    $("inline-viewer-close")?.blur();
    const frame = $("inline-viewer-frame");
    frame?.focus();
    frame?.contentWindow?.postMessage({type: "life-logger-shortcut-space"}, "*");
    return;
  }

  if (isDigit1 || isDigit2 || isDigit3 || isDigit4 || isDown || isUp) {
    event.preventDefault();
    event.stopPropagation();
    $("inline-viewer-close")?.blur();
    const frame = $("inline-viewer-frame");
    frame?.focus();
    frame?.contentWindow?.postMessage({
      type: "life-logger-shortcut",
      key: event.key,
      code: event.code,
      shiftKey: Boolean(event.shiftKey),
    }, "*");
  }
}, true);

document.addEventListener("click", (event) => {
  const playBtn = event.target.closest(".day-event-play-btn, .search-audio-play-btn");
  if (playBtn) {
    event.preventDefault();
    event.stopPropagation();
    toggleAudioClipPlayback(playBtn);
    return;
  }
  const head = event.target.closest(".search-day-head");
  if (head) {
    const card = head.closest(".search-day-card");
    if (card) {
      const isCollapsed = card.classList.toggle("collapsed");
      head.setAttribute("aria-expanded", String(!isCollapsed));
      const label = card.querySelector(".search-day-toggle-label");
      if (label) label.textContent = isCollapsed ? "展开" : "收起";
    }
    return;
  }
  const expandAllBtn = event.target.closest("#search-expand-all");
  if (expandAllBtn) {
    document.querySelectorAll(".search-day-card").forEach((card) => {
      card.classList.remove("collapsed");
      card.querySelector(".search-day-head")?.setAttribute("aria-expanded", "true");
      const label = card.querySelector(".search-day-toggle-label");
      if (label) label.textContent = "收起";
    });
    return;
  }
  const collapseAllBtn = event.target.closest("#search-collapse-all");
  if (collapseAllBtn) {
    document.querySelectorAll(".search-day-card").forEach((card) => {
      card.classList.add("collapsed");
      card.querySelector(".search-day-head")?.setAttribute("aria-expanded", "false");
      const label = card.querySelector(".search-day-toggle-label");
      if (label) label.textContent = "展开";
    });
    return;
  }
});
// resize 回调里是重活：renderJournalDay 整块重建当日视图的 innerHTML，
// 随后还会做时间轴碰撞的布局测量。连续拖拽窗口时不能每个 resize 事件都跑一遍，
// 这里按最小间隔节流（尾随补一次，保证拖拽结束后视图一定是最新的）。
const JOURNAL_RESIZE_THROTTLE_MS = 150;
let journalResizeLastRun = 0;
let journalResizeTimer = 0;
function scheduleJournalResizeRender() {
  const wait = Math.max(0, JOURNAL_RESIZE_THROTTLE_MS - (Date.now() - journalResizeLastRun));
  window.clearTimeout(journalResizeTimer);
  journalResizeTimer = window.setTimeout(() => {
    journalResizeLastRun = Date.now();
    if (activeView !== "today") return;
    const day = journalDays.find((item) => item.date === selectedJournalDate);
    if (day) renderJournalDay(day);
  }, wait);
}
// 观察的是日记容器自身，而不是整个 window：侧栏折叠、分栏变化这类不触发
// window resize 的布局变化同样能感知，无关区域的重排也不再拖起这块重活。
const journalResizeTarget = $("journal-dashboard");
let journalResizeObservedWidth = journalResizeTarget ? journalResizeTarget.clientWidth : -1;
if (journalResizeTarget && typeof ResizeObserver === "function") {
  const journalResizeObserver = new ResizeObserver((entries) => {
    const width = Math.round(entries.at(-1).contentRect.width);
    // 只认宽度变化：重写 innerHTML 引起的高度变化不能再触发一次渲染，否则会自激。
    if (width === journalResizeObservedWidth) return;
    journalResizeObservedWidth = width;
    scheduleJournalResizeRender();
  });
  journalResizeObserver.observe(journalResizeTarget);
} else {
  window.addEventListener("resize", scheduleJournalResizeRender);
}
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) refreshAll(true).catch(() => {});
});
$("global-refresh")?.addEventListener("click", runGlobalRefresh);
$("diagnostic-copy-raw")?.addEventListener("click", () => copyDiagnostic("raw").catch((error) => reportError(error, {operation: "复制错误诊断"})));
$("diagnostic-copy-agent")?.addEventListener("click", () => copyDiagnostic("agent").catch((error) => reportError(error, {operation: "复制 Agent 排查信息"})));
$("diagnostic-close")?.addEventListener("click", closeDiagnosticPanel);
window.addEventListener("error", (event) => {
  const msg = String(event?.message || event?.error?.message || "");
  if (msg.includes("ResizeObserver loop")) return;
  reportError(event.error || new Error(event.message || "页面脚本错误"), {operation: "浏览器脚本执行"});
});
window.addEventListener("unhandledrejection", (event) => {
  const msg = String(event?.reason?.message || event?.reason || "");
  if (msg.includes("ResizeObserver loop")) return;
  reportError(event.reason instanceof Error ? event.reason : new Error(String(event.reason || "未处理 Promise 错误")), {operation: "未处理异步操作"});
});
$("copy-agent-url")?.addEventListener("click", () => copyAgentValue("url"));
$("copy-agent-prompt")?.addEventListener("click", () => copyAgentValue("prompt"));
$("test-agent-connection")?.addEventListener("click", testAgentConnection);
$("refresh-data-health")?.addEventListener("click", () => refreshDataHealth());
$("run-doctor")?.addEventListener("click", () => refreshDoctor().catch(() => {}));
$("mobile-brief-refresh")?.addEventListener("click", () => refreshMobileBrief().catch(() => {}));
$("focus-capture-button")?.addEventListener("click", () => {
  if (activeView !== "today") switchView("today");
  window.setTimeout(() => {
    $("natural-correction-input")?.focus();
  }, 60);
});
$("mobile-go-review")?.addEventListener("click", () => switchView("review"));
$("source-inbox-nav")?.addEventListener("click", () => {
  switchView("today");
  window.setTimeout(() => focusPendingReviewInbox(), 120);
});
$("mobile-send-brief")?.addEventListener("click", () => sendMobileBrief());
$("mobile-run-doctor")?.addEventListener("click", () => runMobileDoctor());
$("save-mobile-access")?.addEventListener("click", () => saveMobileAccess());
$("save-mobile-capture")?.addEventListener("click", () => saveMobileCapture());
$("mobile-capture-input")?.addEventListener("change", (event) => handleMobileCapture(event.target));
$("copy-mobile-token")?.addEventListener("click", () => copyMobileAccessValue("token"));
$("copy-mobile-pair-url")?.addEventListener("click", () => copyMobileAccessValue("pair"));
$("access-token-submit")?.addEventListener("click", submitAccessToken);
$("access-token-input")?.addEventListener("keydown", (event) => {
  if (event.key === "Enter") submitAccessToken();
});
$("save-model-preferences")?.addEventListener("click", () => saveModelPreferences());
$("apply-recommended-models")?.addEventListener("click", () => applyRecommendedModels());
$("open-onboarding")?.addEventListener("click", () => openOnboarding(true));
$("close-onboarding")?.addEventListener("click", () => closeOnboarding());
$("onboarding-custom")?.addEventListener("click", () => {
  closeOnboarding({restoreFocus: false});
  switchView("settings");
  switchSettingsPage("models");
  window.requestAnimationFrame(() => $("settings-tab-models")?.focus());
});
document.addEventListener("keydown", trapModalFocus);
document.addEventListener("keydown", (event) => {
  const overlay = $("onboarding-overlay");
  if (event.key !== "Escape" || !overlay || overlay.classList.contains("hidden")) return;
  event.preventDefault();
  closeOnboarding();
});
$("onboarding-recommended")?.addEventListener("click", async () => {
  const button = $("onboarding-recommended");
  if (button) {
    button.disabled = true;
    button.textContent = "正在应用……";
  }
  await applyRecommendedModels();
  if (button) {
    button.disabled = false;
    button.textContent = "使用推荐方案并开始";
  }
});

// === 全天生活仪表盘 (Daily Life Dashboard) ===
async function loadDashboard(dateStr) {
  const dateInput = $("dashboard-date-input");
  // [2026-10-13 体检修复] 这里原来是 `new Date().toISOString().slice(0, 10)` —— 那是 **UTC** 日期，
  // 北京时间 00:00–08:00 之间会落到前一天（实测 02:57 CST 时给出 10-12，而当天其实是 10-13）。
  // 日记页默认按上海时区取日，这里跟着统一，否则两个视图的「今天」不是同一天，
  // 会把上面那组「同一页面多个事件数」的口径混乱再放大一层。
  const shanghaiToday = new Intl.DateTimeFormat("en-CA", { timeZone: "Asia/Shanghai" }).format(new Date());
  const targetDate = dateStr || (dateInput && dateInput.value) || shanghaiToday;
  if (dateInput && !dateInput.value) {
    dateInput.value = targetDate;
  }
  const statusEl = $("dashboard-status-indicator");
  if (statusEl) {
    statusEl.textContent = "加载中…";
    statusEl.style.background = "rgba(59,130,246,0.15)";
    statusEl.style.color = "#2563eb";
  }

  try {
    const res = await api(`/api/dashboard/daily?date=${targetDate}`, {}, {operation: "加载生活仪表盘"});
    if (!res || !res.dashboard) return;
    const d = res.dashboard;

    // AI 亮点
    const highlightsEl = $("dashboard-ai-highlights");
    if (highlightsEl) {
      highlightsEl.textContent = d.ai_daily_highlights || "今日暂无亮点记录。";
    }

    // 生理体征
    const h = d.health_summary || {};
    if ($("dashboard-hr-stat")) $("dashboard-hr-stat").textContent = `${h.avg_heart_rate || '--'} bpm`;
    if ($("dashboard-hr-sub")) $("dashboard-hr-sub").textContent = `极值: ${h.min_heart_rate || '--'} ~ ${h.max_heart_rate || '--'} bpm`;
    if ($("dashboard-steps-stat")) $("dashboard-steps-stat").textContent = `${h.total_steps || 0} 步`;
    const sleepH = Math.floor((h.sleep_minutes || 0) / 60);
    const sleepM = (h.sleep_minutes || 0) % 60;
    if ($("dashboard-sleep-stat")) $("dashboard-sleep-stat").textContent = `${sleepH}小时${sleepM}分`;
    if ($("dashboard-sleep-sub")) $("dashboard-sleep-sub").textContent = `深睡: ${h.deep_sleep_minutes || 0} 分钟`;
    if ($("dashboard-stress-stat")) $("dashboard-stress-stat").textContent = `${h.stress_spikes_count || 0} 次`;

    // 声学场景条形图
    const sceneBarsEl = $("dashboard-scene-bars");
    if (sceneBarsEl) {
      const scenes = d.scene_breakdown_percentages || {};
      const sceneNames = {
        office: "💻 办公室 / 专注打字",
        transit: "🚇 通勤 / 地铁车辆",
        home_quiet: "🏠 居家安静 / 休息",
        dining: "🍽️ 就餐 / 咖啡厅",
        gym: "🏋️ 健身房 / 有氧运动",
        general: "🌐 其他环境",
      };
      const colors = {
        office: "#3b6fd4",
        transit: "#7c5cbf",
        home_quiet: "#4b7b7c",
        dining: "#d97706",
        gym: "#dc4c3e",
        general: "#78827c",
      };
      sceneBarsEl.innerHTML = Object.entries(scenes).map(([sc, pct]) => `
        <div class="dashboard-scene-row">
          <div class="dashboard-scene-header">
            <span>${escapeHtml(sceneNames[sc] || sc)}</span>
            <span>${pct}%</span>
          </div>
          <div class="dashboard-scene-track">
            <div class="dashboard-scene-fill" style="width:${pct}%; background:${colors[sc] || '#3b6fd4'};"></div>
          </div>
        </div>
      `).join("");
    }

    // 社交发言比例（仅当有真实语音时长数据时显示；无数据不显示默认数字）
    const sr = d.social_ratio || {};
    const selfPct = Number.isFinite(sr.self_speech_percent) ? sr.self_speech_percent : null;
    const othersPct = Number.isFinite(sr.others_speech_percent) ? sr.others_speech_percent : null;
    const hasSpeechRatio = selfPct !== null && othersPct !== null && (selfPct + othersPct) > 0;
    if ($("dashboard-self-pct")) $("dashboard-self-pct").textContent = hasSpeechRatio ? selfPct : "--";
    if ($("dashboard-others-pct")) $("dashboard-others-pct").textContent = hasSpeechRatio ? othersPct : "--";
    if ($("dashboard-self-bar")) $("dashboard-self-bar").style.width = hasSpeechRatio ? `${selfPct}%` : "0%";
    if ($("dashboard-others-bar")) $("dashboard-others-bar").style.width = hasSpeechRatio ? `${othersPct}%` : "0%";
    const socialEmptyEl = $("dashboard-social-empty");
    if (socialEmptyEl) socialEmptyEl.style.display = hasSpeechRatio ? "none" : "block";
    if ($("dashboard-laughter-stat")) $("dashboard-laughter-stat").textContent = `${d.laughter_count || 0} 次`;

    // 即刻抓取卡片流
    const capturesListEl = $("dashboard-captures-list");
    if (capturesListEl) {
      const todos = d.captured_todos || [];
      const expenses = d.captured_expenses || [];
      const ideas = d.captured_ideas || [];
      const formatCaptureClock = (ts) => {
        const ms = Date.parse(String(ts || ""));
        if (Number.isFinite(ms)) {
          return new Intl.DateTimeFormat("en-GB", {
            timeZone: "Asia/Shanghai",
            hour: "2-digit",
            minute: "2-digit",
            hourCycle: "h23",
          }).format(new Date(ms));
        }
        return (ts || "").slice(11, 16) || "今日";
      };
      const allCards = [
        ...todos.map(t => ({
          type: t.trigger_type === "query" ? "查询" : "便签/待办",
          icon: t.trigger_type === "query" ? "🔍" : "📌",
          color: t.trigger_type === "query" ? "rgba(139,92,246,0.1)" : "rgba(59,130,246,0.1)",
          text: t.extracted_content,
          time: t.captured_at,
        })),
        ...expenses.map(e => ({type: "记账", icon: "💰", color: "rgba(16,185,129,0.1)", text: e.extracted_content, time: e.captured_at})),
        ...ideas.map(i => ({type: "灵感", icon: "💡", color: "rgba(245,158,11,0.1)", text: i.extracted_content, time: i.captured_at})),
      ];
      if (allCards.length === 0) {
        capturesListEl.innerHTML = '<div class="dashboard-captures-loading">今日尚未检测到随口微指令（待办/记账/灵感）。随口说“记一下……”即可自动捕捉！</div>';
      } else {
        capturesListEl.innerHTML = allCards.map(c => `
          <div class="dashboard-capture-card" style="background:${c.color};">
            <div class="dashboard-capture-head">
              <span class="dashboard-capture-type">${c.icon} <b>${escapeHtml(c.type)}</b></span>
              <span class="dashboard-capture-time">${escapeHtml(formatCaptureClock(c.time))}</span>
            </div>
            <div class="dashboard-capture-body">${escapeHtml(c.text)}</div>
          </div>
        `).join("");
      }
    }

    if (statusEl) {
      statusEl.textContent = "已就绪";
      statusEl.style.background = "rgba(34,197,94,0.15)";
      statusEl.style.color = "#16a34a";
    }
  } catch (err) {
    if (statusEl) {
      statusEl.textContent = "加载失败";
      statusEl.style.background = "rgba(239,68,68,0.15)";
      statusEl.style.color = "#dc2626";
    }
  }
}

// === 第二大脑生活记忆问答 (Personal Memory Q&A) ===
async function submitMemoryQA(queryText) {
  const inputEl = $("memory-qa-input");
  const query = (queryText || (inputEl && inputEl.value) || "").trim();
  if (!query) return;
  if (inputEl) inputEl.value = query;

  const resultArea = $("memory-qa-result-area");
  if (resultArea) {
    resultArea.innerHTML = `
      <div class="memory-qa-loading">
        <span class="memory-qa-loading-icon">✦</span>
        <div class="memory-qa-loading-text">第二大脑正在检索日记、事件与体征并组织回答……</div>
      </div>
    `;
  }

  try {
    const res = await api("/api/memory/ask", {
      method: "POST",
      body: JSON.stringify({query: query}),
    }, {operation: "生活记忆问答"});

    if (!res || !res.result) {
      if (resultArea) resultArea.innerHTML = '<div class="memory-qa-error">未能获得回答。</div>';
      return;
    }

    const ans = res.result;
    const citationsHtml = (ans.citations || []).map((c, idx) => `
      <li class="memory-citation-item">
        <span class="memory-citation-head">[${idx+1}] 【${escapeHtml(c.timestamp)}】${escapeHtml(c.title)}:</span>
        <span class="memory-citation-snippet">${escapeHtml(c.snippet)}</span>
      </li>
    `).join("");

    if (resultArea) {
      resultArea.innerHTML = `
        <div class="memory-qa-card">
          <div class="memory-qa-card-head">
            <span class="memory-qa-question">✦ ${escapeHtml(ans.query)}</span>
            <span class="setting-badge memory-qa-confidence">置信度 ${Math.round(ans.confidence * 100)}%</span>
          </div>
          <div class="memory-qa-answer">${escapeHtml(ans.answer)}</div>
          ${citationsHtml ? `
            <div class="memory-qa-citations">
              <div class="memory-qa-citations-title">证据锚点与参考片段</div>
              <ul class="memory-qa-citations-list">${citationsHtml}</ul>
            </div>
          ` : ''}
        </div>
      `;
    }
  } catch (err) {
    if (resultArea) {
      resultArea.innerHTML = `<div class="memory-qa-error">查询失败: ${escapeHtml(err.message)}</div>`;
    }
  }
}

// 绑定仪表盘与记忆问答事件
$("dashboard-refresh-btn")?.addEventListener("click", () => {
  const dateVal = $("dashboard-date-input")?.value;
  loadDashboard(dateVal);
});
$("dashboard-date-input")?.addEventListener("change", (e) => {
  loadDashboard(e.target.value);
});

$("memory-qa-form")?.addEventListener("submit", (e) => {
  e.preventDefault();
  submitMemoryQA();
});
document.querySelectorAll("#memory-quick-prompts .quick-chip").forEach(btn => {
  btn.addEventListener("click", () => {
    submitMemoryQA(btn.textContent);
  });
});

// ==== 口语补录与快速订正（自然语言纠错接线） ==========================
// #natural-correction-form → POST /api/events/natural-correction。
// 后端契约（src/life_logger/local_web.py 的同名路由）：
//   {text, date?, job_id?, artifact_path?, target_event_id?}
// 两条硬约束：
// 1. UPDATE / DELETE 必须关联一份**已存在**的 events.json，否则后端报
//    「事件修正必须关联现有 events.json」——所以这里优先把请求挂到当天
//    日记里已有的 job 产物上，改动也会立刻显示在当天记录里。
// 2. 不带 job_id 时后端会新建一个任务：ADD 能成功，但产物不在日记扫描的
//    目录里（job 根目录），日记里看不到。因此只有在当天确实没有可写入的
//    产物时才走这条路，并在反馈里说明。
const NATURAL_CORRECTION_ENDPOINT = "/api/events/natural-correction";

function naturalCorrectionFeedback(message, state = "") {
  const box = $("natural-correction-feedback");
  if (!box) return;
  box.className = `natural-correction-feedback${state ? ` ${state}` : ""}`;
  box.textContent = message;
}

function naturalCorrectionShanghaiDate(epochMs) {
  return new Intl.DateTimeFormat("en-CA", {timeZone: "Asia/Shanghai"}).format(new Date(epochMs));
}

function naturalCorrectionTargetDate(text) {
  // 与后端 process_natural_correction 的日期推断同一口径（Asia/Shanghai）：
  // 句子里有「昨天」→ 昨天，其余都按今天。显式下发 date，避免两边各推一次。
  const now = Date.now();
  return String(text || "").includes("昨天")
    ? naturalCorrectionShanghaiDate(now - 86_400_000)
    : naturalCorrectionShanghaiDate(now);
}

function naturalCorrectionDayTarget(date) {
  // 找这一天日记里可直接写入的产物：job + 该天的 events.json。
  // 只认 entry.date === date 的产物，避免跨午夜条目把补录写进前一天的当日汇总。
  const day = (journalDays || []).find((item) => item.date === date);
  if (!day) return null;
  for (const entry of day.entries || []) {
    if (entry.date === date && entry.jobId && entry.eventsPath) {
      return {jobId: entry.jobId, artifactPath: entry.eventsPath};
    }
  }
  return null;
}

function naturalCorrectionMoney(value) {
  const num = Number(value);
  if (!Number.isFinite(num)) return "";
  return `¥${Number.isInteger(num) ? num : num.toFixed(2)}`;
}

function naturalCorrectionResultMessage(result) {
  const action = String(result?.action || "").toUpperCase();
  const event = result?.event || {};
  const title = String(event?.title || "").trim() || "未命名记录";
  const amount = event?.data?.expense?.amount;
  const date = String(result?.summary?.date || "");
  const total = Number(result?.summary?.expense_total_cny);
  const totalText = Number.isFinite(total) ? `，当天支出合计 ${naturalCorrectionMoney(total)}` : "";
  const dateText = date ? ` ${date}` : "";
  if (action === "UPDATE") {
    const changes = result?.correction?.changes || {};
    const detail = "data" in changes && amount !== undefined && amount !== null
      ? `金额已改为 ${naturalCorrectionMoney(amount)}`
      : "title" in changes ? `标题已改为「${title}」` : "内容已更新";
    return `✅ 已更正${dateText}的「${title}」：${detail}${totalText}。`;
  }
  if (action === "DELETE") {
    return `🗑️ 已删除${dateText}的「${title}」${totalText}。`;
  }
  const amountText = amount === undefined || amount === null ? "" : `（${naturalCorrectionMoney(amount)}）`;
  return `✅ 已补录到${dateText || "当天"}：${title}${amountText}${totalText}。`;
}

async function refreshJournalAfterNaturalCorrection() {
  try {
    const payload = await api("/api/jobs", {}, {silent: true, operation: "刷新任务列表（口语补录后）"});
    await refreshJournal(payload?.jobs || [], true);
  } catch (error) {
    const box = $("natural-correction-feedback");
    if (box) box.textContent = `${box.textContent}（日记刷新失败：${error?.message || error}，可点「刷新记录」重试）`;
  }
}

async function submitNaturalCorrection() {
  const input = $("natural-correction-input");
  const button = $("natural-correction-submit-btn");
  const text = String(input?.value || "").trim();
  if (!text) {
    naturalCorrectionFeedback("先输入或口述一句话，再点「发送处理」。", "error");
    input?.focus();
    return;
  }
  const targetDate = naturalCorrectionTargetDate(text);
  const target = naturalCorrectionDayTarget(targetDate);
  const body = {text, date: targetDate};
  if (target) {
    body.job_id = target.jobId;
    body.artifact_path = target.artifactPath;
  }
  if (button) {
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
  }
  naturalCorrectionFeedback(target
    ? "正在解析这句话并写入当天记录……"
    : "正在解析这句话……（这天还没有可写入的录音事件文件）", "checking");
  try {
    const result = await api(
      NATURAL_CORRECTION_ENDPOINT,
      {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify(body),
      },
      {silent: true, operation: "口述补录与快速订正"},
    );
    if (result?.status === "ok") {
      naturalCorrectionFeedback(naturalCorrectionResultMessage(result), "success");
      if (input) input.value = "";
      await refreshJournalAfterNaturalCorrection();
    } else {
      naturalCorrectionFeedback(`后端没有接受这句话：${String(result?.error || result?.detail || "未知错误")}`, "error");
    }
  } catch (error) {
    // 后端错误文案（空输入 / 缺少 events.json / 鉴权失败等）原样转述，不吞掉。
    naturalCorrectionFeedback(`处理失败：${String(error?.message || error)}`, "error");
  } finally {
    if (button) {
      button.disabled = false;
      button.removeAttribute("aria-busy");
    }
  }
}

function setupNaturalCorrectionMic() {
  const button = $("natural-correction-mic-btn");
  if (!button) return;
  const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (typeof Recognition !== "function") {
    // 浏览器不支持语音识别：隐藏按钮，别留一个点了没反应的死按钮。
    button.classList.add("hidden");
    return;
  }
  const input = $("natural-correction-input");
  let listening = false;
  let recognition = null;
  const stopListening = () => {
    listening = false;
    button.classList.remove("recording");
    button.title = "点击开始语音输入";
    const current = recognition;
    recognition = null;
    try { current?.stop(); } catch (_) {}
  };
  button.addEventListener("click", () => {
    if (listening) {
      stopListening();
      return;
    }
    let instance;
    try {
      instance = new Recognition();
    } catch (error) {
      naturalCorrectionFeedback(`这台设备无法启动语音输入：${error?.message || error}`, "error");
      return;
    }
    instance.lang = "zh-CN";
    instance.interimResults = true;
    instance.continuous = false;
    instance.maxAlternatives = 1;
    let finalText = "";
    instance.onresult = (event) => {
      let interim = "";
      let settled = "";
      for (let index = event.resultIndex; index < event.results.length; index += 1) {
        const item = event.results[index];
        const value = String(item?.[0]?.transcript || "");
        if (item?.isFinal) settled += value;
        else interim += value;
      }
      if (settled) finalText += settled;
      if (input) input.value = `${finalText}${interim}`.trim();
    };
    instance.onerror = (event) => {
      const reason = {
        "not-allowed": "浏览器没有麦克风权限，请在地址栏允许后重试。",
        "service-not-allowed": "浏览器拒绝了语音识别服务。",
        "audio-capture": "没有找到可用的麦克风。",
        "no-speech": "没有听到说话声，再点一次试试。",
        network: "语音识别需要联网，当前网络不可用。",
      }[event?.error] || `语音输入失败（${event?.error || "未知错误"}）。`;
      naturalCorrectionFeedback(reason, "error");
      stopListening();
    };
    instance.onend = () => {
      listening = false;
      recognition = null;
      button.classList.remove("recording");
      button.title = "点击开始语音输入";
      if (input?.value.trim()) input.focus();
    };
    try {
      recognition = instance;
      instance.start();
      listening = true;
      button.classList.add("recording");
      button.title = "正在听……点击停止";
      naturalCorrectionFeedback("正在听……说完会自动填进输入框。", "checking");
    } catch (error) {
      recognition = null;
      listening = false;
      button.classList.remove("recording");
      naturalCorrectionFeedback(`语音输入启动失败：${error?.message || error}`, "error");
    }
  });
}

window.submitNaturalCorrection = submitNaturalCorrection;

$("natural-correction-form")?.addEventListener("submit", (event) => {
  event.preventDefault();
  submitNaturalCorrection();
});
setupNaturalCorrectionMic();

switchView(activeView);
renderJournalFreshness();
window.setInterval(() => renderJournalFreshness(), 30_000);

// 【2026-10-14】Life Logger 状态条真实数据接线。
// 之前 HTML/CSS 里的 #system-status-* 是一组**静态占位文案**（「正在检查…」/「检查中」/「读取中」），
// 全仓没有任何 JS 更新它们 —— 所以永远卡在检查中。这里接三个真实接口：
//   /api/health        → 本地服务
//   /api/watch-health  → Apple Watch 可达性
//   /api/jobs          → Mac 任务 / ASR 最近作业
async function refreshSystemStatus() {
  const overall = $("system-status-overall");
  const checked = $("system-status-checked");
  const setItem = (id, text, tone) => {
    const el = $(id);
    if (!el) return;
    el.textContent = text;
    el.dataset.tone = tone || "";
  };
  if (overall) {
    overall.textContent = "正在检查…";
    overall.className = "system-status-overall checking";
  }
  let tones = [];
  const probe = async (path) => {
    try {
      return await api(path, {}, {silent: true});
    } catch (_) {
      return null;
    }
  };
  const [health, watch, jobs] = await Promise.all([
    probe("/api/health"),
    probe("/api/watch-health"),
    probe("/api/jobs"),
  ]);

  // ① 本地服务
  if (health && health.ok) {
    setItem("system-status-web", "正常", "ok");
    tones.push("ok");
  } else {
    setItem("system-status-web", health === null ? "不可达" : "异常", "bad");
    tones.push("bad");
  }

  // ② Apple Watch
  if (watch && watch.ok && watch.watch_upload_enabled) {
    setItem("system-status-watch", "可上传", "ok");
    tones.push("ok");
  } else if (watch && watch.ok) {
    setItem("system-status-watch", "Mac 可见 · 未启用", "warn");
    tones.push("warn");
  } else {
    setItem("system-status-watch", "未知", "warn");
    tones.push("warn");
  }

  // ③ Mac 任务 / ASR
  const list = Array.isArray(jobs?.jobs) ? jobs.jobs : [];
  const running = list.filter((j) => ["running", "queued", "pending"].includes(String(j.status || "")));
  const latest = list[0];
  if (running.length) {
    const stage = String(running[0].current_stage || running[0].status || "");
    setItem("system-status-jobs", `运行中 ${running.length} · ${stage || "处理中"}`, "active");
    tones.push("active");
  } else if (latest) {
    const statusMap = { completed: "已完成", failed: "失败", cancelled: "已取消" };
    const label = statusMap[String(latest.status)] || String(latest.status || "—");
    const when = beijingManagementTime(latest.completed_at || latest.updated_at);
    setItem("system-status-jobs", `${label}${when ? " " + when : ""}`, String(latest.status) === "failed" ? "bad" : "ok");
    tones.push(String(latest.status) === "failed" ? "bad" : "ok");
  } else {
    setItem("system-status-jobs", "暂无作业", "warn");
    tones.push("warn");
  }

  if (overall) {
    const hasBad = tones.includes("bad");
    const hasActive = tones.includes("active");
    const hasWarn = tones.includes("warn");
    overall.textContent = hasBad ? "有异常" : hasActive ? "作业运行中" : hasWarn ? "部分未知" : "一切正常";
    overall.className = "system-status-overall " + (hasBad ? "warning" : hasActive ? "active" : hasWarn ? "warning" : "ready");
  }
  if (checked) {
    const now = new Date();
    checked.textContent = `最近检查 ${now.toLocaleTimeString("zh-CN", {timeZone: "Asia/Shanghai", hour12: false})} UTC+8`;
  }
}

refreshSystemStatus().catch(() => {});
window.setInterval(() => { if (!document.hidden) refreshSystemStatus().catch(() => {}); }, 30_000);

let activitySummaryPolling = false;
window.setInterval(async () => {
  if (document.hidden || activeView !== "today" || activitySummaryPolling) return;
  const date = selectedJournalDate;
  const day = journalDays.find(item => item.date === date);
  if (!day || date !== shanghaiDate(0) || day._activityTimelineLoading) return;
  activitySummaryPolling = true;
  try {
    await ensureJournalActivityTimeline(day, {force: true});
    if (date === selectedJournalDate) renderJournalDay(day);
  } finally { activitySummaryPolling = false; }
}, 60000);

// 自动实时轮询 Watch 录音与实时会话事件 (每 15 秒同步一次绿条与实时生活事件)
let liveWatchPollTimer = null;
function startLiveWatchPolling() {
  if (liveWatchPollTimer) clearInterval(liveWatchPollTimer);
  liveWatchPollTimer = setInterval(async () => {
    if (document.hidden || journalSwitchActive()) return;
    try {
      const payload = await api("/api/jobs", {}, {silent: true});
      await refreshJournal(payload.jobs, false);
      if (activeView === "dashboard") {
        await loadDashboard();
      }
    } catch (_) {}
  }, 15_000);
}
startLiveWatchPolling();

renderAgentIntegration();
loadCapabilities();

// 非关键预热延后到浏览器空闲时执行：Agent 接入面板文案与 30s 鲜度计时不参与首屏，
// 避免和视图渲染、主数据请求抢主线程。带 timeout 兜底（空闲迟迟不来也会跑），
// 不支持 requestIdleCallback 的环境退化为 setTimeout。
function runDeferredWarmups() {
  renderAgentIntegration();
  window.setInterval(() => renderJournalFreshness(), 30_000);
  // 【2026-10-15 恢复】状态条查询同样不参与首屏，与鲜度计时一起延后：
  // 先立刻查一遍，之后每 30 秒刷新（页面隐藏时跳过，省电）。
  refreshSystemStatus().catch(() => {});
  window.setInterval(() => { if (!document.hidden) refreshSystemStatus().catch(() => {}); }, 30_000);
}
if (typeof window.requestIdleCallback === "function") {
  window.requestIdleCallback(runDeferredWarmups, {timeout: 2000});
} else {
  window.setTimeout(runDeferredWarmups, 1);
}
refreshAll().then(() => {
  if (currentJobId) beginPolling();
  // Deep link used by Watch cards / external links when no viewer artifact exists.
  const linkedJobId = new URLSearchParams(location.search).get("job");
  if (linkedJobId) {
    switchView("import");
    selectJob(linkedJobId).catch(() => {});
  }
}).catch((error) => {
  $("jobs-list").textContent = error.message;
  const root = $("journal-dashboard");
  setJournalLoading(false, "日记暂时不可用");
  markJournalRefreshFailure(error);
  if (root) {
    root.innerHTML = `<div class="journal-empty" role="status"><strong>日记暂时不可用</strong><span>${escapeHtml(error.message)}</span></div>`;
  }
});

/* [parent-ui-compact] 单一摘要入口：展开/收起金额口径、分类明细与待确认 */
function toggleDayBrief(btn) {
  const open = document.body.classList.toggle("day-brief-open");
  document.querySelectorAll(".day-brief-toggle").forEach((b) => b.setAttribute("aria-expanded", String(open)));
  if (btn) btn.setAttribute("aria-expanded", String(open));
}
window.toggleDayBrief = toggleDayBrief;

/* [parent-ui-compact] 手机端口语补录默认收起为一行按钮，点开再展开输入。 */
(function initMobileCorrectionToggle() {
  function mount() {
    const card = document.getElementById("natural-correction-card");
    if (!card || document.getElementById("correction-expand-btn")) return;
    const btn = document.createElement("button");
    btn.id = "correction-expand-btn";
    btn.type = "button";
    btn.className = "correction-expand-btn";
    btn.setAttribute("aria-expanded", "false");
    btn.setAttribute("aria-controls", "natural-correction-card");
    btn.innerHTML = '<span class="ceb-icon" aria-hidden="true">\u270e</span><span>\u8bb0\u4e00\u7b14 / \u8865\u5f55\u6d88\u8d39\u6216\u5f85\u529e</span><span class="ceb-chev" aria-hidden="true">\u25be</span>';
    btn.addEventListener("click", () => {
      const open = document.body.classList.toggle("correction-expanded");
      btn.setAttribute("aria-expanded", String(open));
      if (open) {
        const field = card.querySelector("input, textarea");
        if (field) setTimeout(() => field.focus({ preventScroll: true }), 30);
      }
    });
    card.parentElement.insertBefore(btn, card);
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", mount, { once: true });
  else mount();
})();
