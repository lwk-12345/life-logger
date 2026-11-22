import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const cssPath = resolve(here, "..", "web", "style.css");
const css = readFileSync(cssPath, "utf8");

/**
 * Count brace depth while skipping comments and quoted strings. This is the
 * cheap structural check that would have caught the stray `}` which caused the
 * final `@layer utilities` block (and therefore `.more-grid`) to be swallowed.
 */
function scanBraces(source) {
  let depth = 0;
  let minDepth = 0;
  let inComment = false;
  let quote = null;

  for (let i = 0; i < source.length; i += 1) {
    const ch = source[i];
    const pair = source.slice(i, i + 2);

    if (inComment) {
      if (pair === "*/") {
        inComment = false;
        i += 1;
      }
      continue;
    }
    if (quote) {
      if (ch === "\\") {
        i += 1;
      } else if (ch === quote) {
        quote = null;
      }
      continue;
    }
    if (pair === "/*") {
      inComment = true;
      i += 1;
      continue;
    }
    if (ch === '"' || ch === "'") {
      quote = ch;
      continue;
    }
    if (ch === "{") depth += 1;
    if (ch === "}") depth -= 1;
    if (depth < minDepth) minDepth = depth;
  }

  return { depth, minDepth };
}

test("style.css has balanced braces and never closes an outer block early", () => {
  const { depth, minDepth } = scanBraces(css);
  assert.equal(minDepth, 0, "a closing brace appears before any matching block is open");
  assert.equal(depth, 0, "brace count is unbalanced (missing or extra closing brace)");
});

test("cascade layers include utilities and the utilities block contains .more-grid", () => {
  assert.match(
    css,
    /@layer\s+reset\s*,\s*base\s*,\s*components\s*,\s*utilities\s*;/,
    "approved cascade layer order must be declared",
  );
  const moreGridIndex = css.indexOf(".more-grid {");
  assert.notEqual(moreGridIndex, -1, ".more-grid rule is missing");
  const utilitiesOpen = css.lastIndexOf("@layer utilities {", moreGridIndex);
  assert.notEqual(utilitiesOpen, -1, "expected .more-grid inside an @layer utilities block");
  const utilitiesClose = css.lastIndexOf("} /* @layer utilities 结束 */");
  assert.ok(
    utilitiesClose > moreGridIndex,
    "the utilities layer must close after .more-grid, not swallow it",
  );
});

test(".more-grid, .more-card and .kanban-board keep their intended display", () => {
  const gridIndex = css.indexOf(".more-grid {");
  const cardIndex = css.indexOf(".more-card {");
  const kanbanIndex = css.indexOf(".kanban-board {");
  assert.ok(gridIndex !== -1, ".more-grid rule is missing");
  assert.ok(cardIndex !== -1, ".more-card rule is missing");
  assert.ok(kanbanIndex !== -1, ".kanban-board rule is missing");
  assert.ok(gridIndex < cardIndex, ".more-grid must precede .more-card");

  const gridBlock = css.slice(gridIndex, css.indexOf("}", gridIndex));
  const cardBlock = css.slice(cardIndex, css.indexOf("}", cardIndex));
  const kanbanBlock = css.slice(kanbanIndex, css.indexOf("}", kanbanIndex));

  assert.match(gridBlock, /display:\s*grid\s*;/, ".more-grid should use display: grid");
  assert.match(cardBlock, /display:\s*flex\s*;/, ".more-card should use display: flex");
  assert.match(cardBlock, /border:\s*1px\s+solid\b/, ".more-card should keep its 1px border");
  assert.match(cardBlock, /padding:\s*16px\s+18px\s*;/, ".more-card should keep its padding");
  assert.match(kanbanBlock, /display:\s*grid\s*;/, ".kanban-board should use display: grid");
  assert.doesNotMatch(
    css,
    /\.local-reminder-[\w-]*\s*\{/,
    "removed local reminder UI must not keep CSS rules",
  );
});
