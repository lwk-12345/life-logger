import assert from 'node:assert/strict';
import fs from 'node:fs';
import test from 'node:test';

const html = fs.readFileSync(new URL('../web/daily-timeline-live.html', import.meta.url), 'utf8');

// ---- 静态契约：202 不得被当成鉴权/空数据；重试必须有界且跨 loadDay 计数 ----
test('202 有界退避：尝试次数跨 loadDay 持久、延迟指数封顶、无无限定时器', () => {
  assert.match(html, /state\.pendingAttemptsByDate\.set\(dateStr, state\.pendingAttempts\)/, '同日期尝试计数需持久化');
  assert.match(html, /state\.pendingAttemptsByDate\.get\(dateStr\) \|\| 0/, 'loadDay 不得把计数清零');
  assert.doesNotMatch(html, /state\.pendingAttempts = 0;/, '不得每次加载都重置重试计数（旧 bug：95 请求/30s）');
  assert.match(html, /Math\.min\(30000, Math\.round\(base \* Math\.pow\(1\.6, attempts\)\)\)/, '指数退避 + 30s 封顶');
  assert.match(html, /Math\.min\(5000, Math\.max\(1000, Number\(info\.retryAfterMs\) \|\| 1000\)\)/, '尊重 Retry-After');
  assert.match(html, /Number\(info\.maxAttempts \|\| 8\)/, '有界次数');
});

test('202 分支在“两主接口都没数据”之前，且不清空内容、不说鉴权失败', () => {
  const pendingIdx = html.indexOf('if (Object.keys(pending).length) {');
  const failedIdx = html.indexOf('if (!values.daily && !values.dashboard) {');
  assert.ok(pendingIdx > 0 && failedIdx > 0 && pendingIdx < failedIdx, 'pending 必须先于失败分支结算');
  assert.match(html, /后台正在准备这一天的数据/);
  assert.doesNotMatch(html, /pending[\s\S]{0,200}令牌无效/, 'pending 不得引向鉴权错误');
  assert.match(html, /if \(dateChanged\) state\.pendingAttemptsByDate\.delete\(dateStr\);/, '切日清理旧重试状态');
  assert.match(html, /if \(state\.date !== dateStr \|\| seq !== state\.loadSeq\) return;/, '定时器触发时校验日期/序号，防串日');
});
