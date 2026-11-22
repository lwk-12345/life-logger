import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";

const app = fs.readFileSync(new URL("../web/app.js", import.meta.url), "utf8");
const css = fs.readFileSync(new URL("../web/style.css", import.meta.url), "utf8");
const timeline = fs.readFileSync(new URL("../web/daily-timeline-live.html", import.meta.url), "utf8");

test("CSS: style.css 彻底移除固定 360px/1440px/1800px 高度限制", () => {
  assert.doesNotMatch(css, /body\[data-active-view="today"\]\s+\.day-timeline-frame\s*\{\s*height:\s*360px/, "移除了 today 视图下的固定 360px 高度");
  assert.doesNotMatch(css, /\.day-timeline-frame\s*\{[^}]*height:\s*1440px/, "移除了桌面端固定 1440px 高度");
  assert.doesNotMatch(css, /\.day-timeline-frame\s*\{[^}]*height:\s*1800px/, "移除了移动端固定 1800px 高度");
  assert.match(css, /\.day-timeline-frame\s*\{[^}]*min-height:\s*400px/, "设置了合理的自适应基准 min-height");
});

test("iframe HTML/CSS: 嵌入模式下 iframe 内部禁止纵向滚动条 (overflow-y: hidden)", () => {
  // [2026-11-02 Q533 M1] 嵌入判定类由 is-embedded 改为 is-embedded-in-frame：
  // 旧类会被 ?embedded=1 在独立打开时命中，造成整页滚轮/PageDown 失效。
  assert.match(timeline, /html\.is-embedded-in-frame,\s*html\.is-embedded-in-frame\s+body,\s*body\.is-embedded-in-frame\s*\{\s*overflow-y:\s*hidden\s*!important;\s*\}/);
  assert.match(timeline, /classList\.add\("is-embedded-in-frame"\)/);
});

test("iframe JS: 包含 ResizeObserver 自动观测高度并在渲染结束时同步高度", () => {
  assert.match(timeline, /function notifyParentHeight\(\)/);
  assert.match(timeline, /new ResizeObserver/);
  assert.match(timeline, /postMessage\(\{\s*type:\s*"life-logger-timeline-height"/);
});

test("父页 JS (app.js): 全局监听高度消息并严格校验消息来源", () => {
  assert.match(app, /window\.addEventListener\("message"/);
  assert.match(app, /payload\.type !== "life-logger-timeline-height"/);
  assert.match(app, /event\.source !== frame\.contentWindow/, "必须严格校验消息来自当前 iframe contentWindow");
  assert.match(app, /!frame\.isConnected/, "必须校验 iframe 处于当前 DOM 连接状态");
  assert.match(app, /frame\.style\.height\s*=\s*`\$\{next\}px`/, "将自适应高度直接赋予 frame 元素");
});
