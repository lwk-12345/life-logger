/**
 * 前端测试共用加载器（Q854）。
 *
 * 为什么需要：`tests/*.test.mjs` 各自用 `vm.runInContext` 加载
 * `web/daily-timeline-live.html` 里的一段脚本，但页面脚本会引用一批"运行环境"东西
 * （document / setInterval / beijingManagementTime / sceneMetaFor / JOURNAL_LOGIC…）。
 * 过去每个测试文件各写一份残缺沙箱，页面一长就集体报
 * `ReferenceError: document is not defined`，看起来像 43 条测试坏了，其实是**环境缺件**。
 *
 * 本文件只提供"环境"，不提供断言、不改被测代码：
 *   - makeDom()        极简 DOM stub（addEventListener / querySelector / createElement…）
 *   - baseGlobals()    标准内置对象 + 定时器 + console + 上面这些缺件
 *   - pageScript()     取页面内联脚本原文
 *   - sliceBetween()   按标记切片（保留既有测试的切片习惯）
 *   - sliceFunction()  按函数名切一个函数（含嵌套大括号配平）
 *   - helperFromPage() 从页面脚本里取一个纯函数（escapeHtml / beijingManagementTime …）
 *
 * 只读：不写文件、不访问网络。
 */
import fs from "node:fs";
import vm from "node:vm";

const PAGE_URL = new URL("../../web/daily-timeline-live.html", import.meta.url);
const JOURNAL_LOGIC_URL = new URL("../../web/journal_logic.js", import.meta.url);

/** 页面内联脚本原文（`<script>…</script>` 中不带 src 的那一段）。 */
export function pageScript() {
  const html = fs.readFileSync(PAGE_URL, "utf8");
  const m = html.match(/<script>([\s\S]*)<\/script>/);
  if (!m) throw new Error("daily-timeline-live.html 里找不到内联 <script>");
  return m[1];
}

/** 页面全文（少数测试需要扫 HTML 结构）。 */
export function pageHtml() {
  return fs.readFileSync(PAGE_URL, "utf8");
}

/** `web/journal_logic.js` 的 `window.LifeLoggerJournalLogic` 对象。 */
export function journalLogic() {
  const src = fs.readFileSync(JOURNAL_LOGIC_URL, "utf8");
  const sandbox = { window: {}, document: makeDom(), console };
  vm.createContext(sandbox);
  vm.runInContext(src, sandbox);
  return sandbox.window.LifeLoggerJournalLogic || {};
}

/**
 * 极简 DOM stub。页面脚本里有若干顶层副作用（点击委托、滚动监听、初始化调用），
 * 在 vm 里跑要么直接抛错、要么需要 document 存在——这里给一个"什么都不做"的实现。
 */
export function makeDom(overrides = {}) {
  const noop = () => {};
  const el = () => ({
    addEventListener: noop,
    removeEventListener: noop,
    appendChild: noop,
    removeChild: noop,
    insertBefore: noop,
    setAttribute: noop,
    removeAttribute: noop,
    getAttribute: () => null,
    querySelector: () => null,
    querySelectorAll: () => [],
    closest: () => null,
    focus: noop,
    blur: noop,
    click: noop,
    scrollIntoView: noop,
    classList: { add: noop, remove: noop, toggle: noop, contains: () => false },
    style: {},
    dataset: {},
    children: [],
    childNodes: [],
    parentElement: null,
    textContent: "",
    innerHTML: "",
    innerText: "",
    value: "",
  });
  const document = {
    addEventListener: noop,
    removeEventListener: noop,
    dispatchEvent: noop,
    createElement: () => el(),
    createTextNode: () => ({}),
    createDocumentFragment: () => el(),
    querySelector: () => null,
    querySelectorAll: () => [],
    getElementById: () => null,
    getElementsByClassName: () => [],
    body: el(),
    head: el(),
    documentElement: { classList: { add: noop, remove: noop, contains: () => false }, style: {}, dataset: {} },
    readyState: "complete",
    hidden: false,
    visibilityState: "visible",
    title: "",
    cookie: "",
    location: { href: "http://127.0.0.1:8765/", search: "", pathname: "/", reload: noop },
    ...overrides,
  };
  return document;
}

/** 单个元素 stub（给 getElementById 之类用；带 removeAttribute/insertAdjacentHTML）。 */
export function makeElement(overrides = {}) {
  const noop = () => {};
  return {
    textContent: "", innerHTML: "", innerText: "", value: "", id: "",
    style: {}, dataset: {}, children: [], childNodes: [], parentElement: null,
    classList: { add: noop, remove: noop, toggle: noop, contains: () => false },
    addEventListener: noop, removeEventListener: noop,
    setAttribute: noop, removeAttribute: noop, getAttribute: () => null,
    appendChild: noop, removeChild: noop, insertBefore: noop, replaceChildren: noop,
    insertAdjacentHTML: noop, insertAdjacentText: noop,
    querySelector: () => null, querySelectorAll: () => [], closest: () => null,
    focus: noop, blur: noop, click: noop, scrollIntoView: noop, remove: noop,
    getBoundingClientRect: () => ({ top: 0, left: 0, right: 0, bottom: 0, width: 0, height: 0 }),
    ...overrides,
  };
}

/** 标准内置对象 + 定时器 + console + DOM。测试可以在其后覆盖任意键。 */
export function baseGlobals(overrides = {}) {
  const noop = () => 0;
  return {
    result: null,
    console,
    // 内置对象（vm 上下文里默认没有这些，测试过去逐个手写）
    Object, Array, String, Number, Boolean, Math, JSON, Date, RegExp, Error, Promise,
    Map, Set, WeakMap, WeakSet, Symbol, Intl, BigInt,
    parseInt, parseFloat, isNaN, isFinite, encodeURIComponent, decodeURIComponent,
    encodeURI, decodeURI,
    // 定时器：页面里有轮询/防抖，测试不需要真的跑
    setTimeout: noop, clearTimeout: noop, setInterval: noop, clearInterval: noop,
    requestAnimationFrame: noop, cancelAnimationFrame: noop,
    queueMicrotask: (fn) => { try { fn(); } catch (_) {} },
    // 浏览器 API
    document: makeDom(),
    window: undefined,
    localStorage: { getItem: () => null, setItem: noop, removeItem: noop, clear: noop },
    sessionStorage: { getItem: () => null, setItem: noop, removeItem: noop, clear: noop },
    location: { href: "http://127.0.0.1:8765/", search: "", pathname: "/", reload: noop },
    navigator: { userAgent: "node-test", language: "zh-CN" },
    fetch: () => Promise.reject(new Error("no network in unit tests")),
    URLSearchParams,
    URL,
    AbortController,
    performance: { now: () => Date.now() },
    alert: noop,
    ...overrides,
  };
}

/** 按起止标记切一段脚本。end 找不到时切到脚本末尾。 */
export function sliceBetween(script, startMarker, endMarker) {
  const start = script.indexOf(startMarker);
  if (start < 0) throw new Error(`找不到起始标记: ${startMarker}`);
  const end = endMarker ? script.indexOf(endMarker, start) : -1;
  return script.slice(start, end > start ? end : script.length);
}

/**
 * 按函数名切一个函数体（配平大括号）。
 * 支持 `function name(`、`async function name(`、`const name = (` / `const name = function`。
 */
export function sliceFunction(script, name) {
  const escaped = name.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const patterns = [
    new RegExp(`(?:async\\s+)?function\\s+${escaped}\\s*\\(`),
    new RegExp(`(?:const|let|var)\\s+${escaped}\\s*=\\s*(?:async\\s*)?(?:function\\s*)?\\(`),
    new RegExp(`(?:const|let|var)\\s+${escaped}\\s*=\\s*(?:async\\s*)?[^\n(]*=>`),
  ];
  let m = null;
  for (const p of patterns) {
    m = p.exec(script);
    if (m) break;
  }
  if (!m) throw new Error(`找不到函数: ${name}`);
  const from = m.index;
  let i = script.indexOf("{", m.index);
  // 箭头函数单表达式形式：切到行尾
  const arrowOnly = !script.slice(m.index, m.index + 200).includes("{");
  if (arrowOnly) {
    const nl = script.indexOf("\n", m.index);
    return script.slice(from, nl < 0 ? script.length : nl);
  }
  let depth = 0;
  for (; i < script.length; i += 1) {
    const ch = script[i];
    if (ch === "{") depth += 1;
    else if (ch === "}") {
      depth -= 1;
      if (depth === 0) return script.slice(from, i + 1);
    }
  }
  throw new Error(`函数 ${name} 的大括号不配平`);
}

/** 从页面脚本里取一个纯函数源码；取不到返回空串（调用方可跳过）。 */
export function helperFromPage(script, name) {
  try {
    return sliceFunction(script, name);
  } catch (_) {
    return "";
  }
}

/**
 * 建立一个带基础环境的 vm 上下文；可选注入一段前导脚本。
 * 返回 context 本身，测试照旧 `vm.runInContext(...)`。
 */
export function createContext(overrides = {}, { prelude = "" } = {}) {
  const ctx = baseGlobals(overrides);
  if (!ctx.window) ctx.window = ctx;
  vm.createContext(ctx);
  if (prelude) vm.runInContext(prelude, ctx);
  return ctx;
}

export default {
  pageScript, pageHtml, journalLogic, makeDom, makeElement, baseGlobals,
  sliceBetween, sliceFunction, helperFromPage, createContext,
};
