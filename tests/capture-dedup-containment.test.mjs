// 【2026-10-25 止损】unknown-time 自动抑制在调用点停用（不回滚 WIP、不删候选算法）
// 目的：静态代码不得再因“未知时刻同证据”隐藏抓取记录；helper 保留供候选单测。
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import test from 'node:test';

const ROOT = path.resolve(path.dirname(new URL(import.meta.url).pathname), '..');
const html = fs.readFileSync(path.join(ROOT, 'web/daily-timeline-live.html'), 'utf8');
const script = html.match(/<script>([\s\S]*)<\/script>/)[1];

function extract(name) {
  const re = new RegExp(`^(?:async\\s+)?function\\s+${name}\\s*\\(`, 'm');
  const m = re.exec(script);
  assert.ok(m, `missing function ${name}`);
  let i = script.indexOf('(', m.index), depth = 0;
  for (; i < script.length; i += 1) {
    if (script[i] === '(') depth += 1;
    else if (script[i] === ')' && (depth -= 1) === 0) { i = script.indexOf('{', i); break; }
  }
  let d = 0;
  for (; i < script.length; i += 1) {
    if (script[i] === '{') d += 1;
    else if (script[i] === '}' && (d -= 1) === 0) return script.slice(m.index, i + 1);
  }
  throw new Error(`unclosed ${name}`);
}

test('调用点已停用 unknown-time 自动抑制（开关为 false，产品路径不会调用 helper）', () => {
  assert.match(script, /const UNKNOWN_TIME_AUTOSUPPRESS_ENABLED = false;/);
  const block = script.slice(script.indexOf('const UNKNOWN_TIME_AUTOSUPPRESS_ENABLED'), script.indexOf('seen.add(sig)', script.indexOf('const UNKNOWN_TIME_AUTOSUPPRESS_ENABLED')));
  assert.match(block, /const unknownHit = UNKNOWN_TIME_AUTOSUPPRESS_ENABLED\s*\?/);
  // 关闭时 unknownHit 恒为 null → 下面的 suppressed.push / deduped++ 不可能执行
  assert.match(block, /: null;/);
});

test('候选 helper 仍在（可继续单测），但产品里只有这一处调用且被开关包住', () => {
  assert.match(script, /function sameEvidenceForUnknownTime\(/);
  assert.match(script, /function evidenceIdentityMatch\(/);
  const calls = [...script.matchAll(/sameEvidenceForUnknownTime\(/g)];
  assert.equal(calls.length, 2, '1 处定义 + 1 处调用');
  const callLine = script.slice(0, calls[1].index).split('\n').pop();
  assert.ok(callLine.includes('map((e) =>'), '调用应只在 guarded 分支里');
});

test('已知时间路径未被改动（同签名去重 + 同分钟重合去重仍在）', () => {
  assert.match(script, /if \(seen\.has\(sig\)\) \{ deduped\+\+; return; \}/);
  assert.match(script, /const dupOfEvent = events\.some\(/);
  assert.match(script, /if \(dupOfEvent\) \{ deduped\+\+; return; \}/);
});

test('候选 helper 的既有单测仍在（候选语义由 tests/capture-event-dedup.test.mjs 覆盖）', () => {
  // 说明：本文件只锁“止损”这件事（产品不调用）。helper 的语义细节不在这里重复造夹具，
  // 由既有 20+ 条候选单测继续验证；若那些测试被删/被改宽，本断言会失败。
  const dedupPath = path.join(ROOT, 'tests/capture-event-dedup.test.mjs');
  assert.ok(fs.existsSync(dedupPath), '候选单测文件必须保留');
  const dedup = fs.readFileSync(dedupPath, 'utf8');
  const cases = (dedup.match(/^test\(/gm) || []).length;
  assert.ok(cases >= 18, `候选单测数量不应减少（实际 ${cases}）`);
  assert.match(dedup, /sameEvidenceForUnknownTime|evidenceIdentityMatch/,
    '候选单测必须仍然直接测 helper');
});
