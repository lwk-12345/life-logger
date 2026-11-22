// /api/settings 默认只给掩码；完整令牌只在用户点击复制时按需读取。
import {test} from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";

const source = fs.readFileSync(new URL("../web/app.js", import.meta.url), "utf8");
const html = fs.readFileSync(new URL("../web/index.html", import.meta.url), "utf8");

test("settings render uses masked token and does not build pair URL from it", () => {
  assert.match(source, /token_masked/);
  assert.match(source, /点击“复制地址”时生成/);
});

test("copy actions fetch full token on demand from dedicated endpoint", () => {
  assert.match(source, /async function revealMobileAccessToken/);
  assert.match(source, /api\("\/api\/settings\/mobile-token"\)/);
  assert.match(source, /copyMobileAccessValue[\s\S]{0,400}revealMobileAccessToken/);
});

test("settings copy hint explains masking", () => {
  assert.match(html, /默认只显示掩码/);
});
