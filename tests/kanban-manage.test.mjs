// 任务看板：勾选、拖动、删除都要真的写状态。曾因拆分删掉写入函数，勾选点了没有反应。
import {test} from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";

const app = fs.readFileSync(new URL("../web/app.js", import.meta.url), "utf8");

test("看板写状态的函数存在", () => {
  assert.match(app, /async function setKanbanTaskStatus\(taskKey, status\)/);
});

test("三条状态泳道都能接收拖放，并各自写对状态", () => {
  for (const status of ["pending", "in_progress", "completed"]) {
    assert.ok(app.includes(`dropStatus: "${status}"`), status);
  }
  assert.match(app, /setKanbanTaskStatus\(taskKey, lane\.dataset\.kanbanDrop\)/);
});

test("删除只改状态，并提供恢复入口", () => {
  assert.match(app, /kanbanStatusButton\(taskKey, "dismissed", "删除"\)/);
  assert.match(app, /kanbanStatusButton\(taskKey, "pending", "恢复"\)/);
  const writer = app.slice(app.indexOf("async function setKanbanTaskStatus"), app.indexOf("async function toggleKanbanTodo"));
  assert.doesNotMatch(writer, /method:\s*"DELETE"/);
});

test("OpenClaw 任务并入看板，改状态走同步接口并带版本号", () => {
  assert.match(app, /kanbanOpenClawTasks\(todos\)/);
  assert.match(app, /"\/api\/openclaw\/tasks\/status"/);
  assert.match(app, /revision: task\.revision/);
  assert.match(app, /taskKey\.startsWith\("oc:"\)/);
});

test("活动里的待办进看板", () => {
  assert.match(app, /item\.fact_type !== "todo"/);
  assert.match(app, /kanbanActivityTodos\.get\(day\.date\)/);
});

test("习惯有网页入口：打卡、添加、不再跟踪", () => {
  assert.match(app, /"\/api\/habits\/checkin"/);
  assert.match(app, /"\/api\/habits\/add"/);
  assert.match(app, /"\/api\/habits\/status"/);
});
