# Life Logger × OpenClaw × Hermes：统一 Todo / Project 方案

> 状态：设计已落定，尚未开始跨系统大改。  
> 调研时间：2026-09-22。  
> 原则：OpenClaw 作为逻辑单一事实源（SoT）；Life Logger 提供生活事件与待办候选；Hermes 作为可选执行器/项目运行时。不得让三边各维护一套独立“最终状态”。

## 1. 本机现状

### 1.1 结论先行

当前真正有历史待办数据的是 OpenClaw 的 `workspace/memory/tasks/`，并由
`workspace/scripts/todo.js` 读写。

OpenClaw 的 `OPEN_LOOPS.md` 已明确声明自己“不是日期与完成状态的唯一事实源”，
准确状态以 `scripts/todo.js`、`memory/tasks/` 和实际工具结果为准。

OpenClaw 的 Workboard 插件虽然已启用，但本机 `workboard.sqlite` 当前是
**0 board / 0 card**；它是可用能力，不是当前事实源。

Hermes 本机已有成熟的 `kanban.db` 和 `projects.db` schema，但当前同样是
**0 task / 0 project**，因此它现在也不是用户实际待办的事实源。

Life Logger 有 `/api/tasks` 与 `/api/tasks/status`，底层是
`output/local-web/task-states.json`，但当前工作区中该文件不存在，即当前没有
显式保存过的 task-state 行。Life Logger 的 NEXT 主要仍可由 `todo` 事件与本地状态
派生。

### 1.2 数据源对照表

| 系统 / 数据源 | 本机真实状态 | 读 | 写 | 适合承担的角色 |
| --- | --- | --- | --- | --- |
| OpenClaw `workspace/memory/tasks/today-YYYY-MM-DD.md` | 有大量历史文件；2026-09-22 今日文件为空 | `todo.js list` / 文件 | `todo.js add/done/progress/rm` | **当前任务 SoT 后端** |
| OpenClaw `workspace/memory/tasks/backlog.md` | 有真实 backlog；含 `[ ]`、`[x]`、`[~]` 及 `[state:*]` 元数据 | `todo.js list --backlog` / 文件 | 主要经 `todo.js`，另有其他自动化写入痕迹 | **当前任务 SoT 后端的一部分** |
| OpenClaw `scripts/todo.js` | 正在使用；支持 add/list/done/progress/rm；Asia/Shanghai | CLI | CLI | **当前规范写入口** |
| OpenClaw `.state/todo.md` | 仍有旧待办 | 文件 | 人工/旧流程 | 旧快照；应逐步只读/退役 |
| OpenClaw `OPEN_LOOPS.md` | 有跨领域未闭环事项；文件自己声明不是唯一事实源 | 文件 | 人工/Agent 汇总 | 派生“开放回路”视图，不保存最终状态 |
| OpenClaw Workboard `plugins/workboard/workboard.sqlite` | 插件 enabled；0 board / 0 card；schema 很丰富（boards/cards/comments/runs/artifacts 等） | OpenClaw 内部 | OpenClaw 内部 | 后续项目看板/投影视图；**当前不直接当 SoT** |
| Life Logger `todo` 事件 | 来自录音事件提取；带 evidence/event_id | `daily_summary`、SQLite、`/api/daily`、`/api/search` | 由本地处理管线生成 | 生活证据与“待办候选” |
| Life Logger `task-states.json` | 能力存在；当前文件缺失 | `GET /api/tasks` | `POST /api/tasks/status` | 兼容状态缓存；不应继续成为全局 SoT |
| Life Logger NEXT / Daily Brief | NEXT 由事件 + task state 派生；brief 会输出 open todos | Web / `GET /api/daily-brief` / outbox / webhook | UI 可改本地 task state | OpenClaw 全局任务的消费视图 + Life Logger 来源视图 |
| Hermes `kanban.db` | schema 成熟；0 task | CLI / Hermes 内部 | CLI / dispatcher | 可选执行队列、运行状态、依赖/阻塞/审阅 |
| Hermes `projects.db` | schema 成熟；0 project | `hermes project list/show` | `hermes project create/.../bind-board` | 可选项目运行时投影 |
| Hermes cron / dispatcher | 已有任务派发基础设施 | Hermes | Hermes | 执行、调度、运行证据，不应拥有用户待办最终状态 |

本机抽样计数：OpenClaw `memory/tasks` 当前可见约 15 条 open、14 条 done、
17 条 `[~]` 其他状态记录，顶层任务文件 55 个。这个数量说明“迁移已有真实数据”
比重新从 Life Logger/Hermes 建一份更合理。

### 1.3 一个必须先解决的兼容问题

`todo.js` 的 `TASK_RE` 目前只识别：

- `- [ ] ...`
- `- [x] ...`

而 backlog 里实际已经存在 `- [~] ... [state:dismissed|closed]` 等记录。
因此 **Life Logger/Hermes 不得直接解析/修改这些 Markdown 作为跨系统同步协议**。
否则会出现“OpenClaw 自己不同写入链路都不完全一致，外部系统又各写一遍”的状态漂移。

同理，也不应让 Life Logger 或 Hermes 直接写 Workboard/Hermes SQLite 内部表。

---

## 2. 推荐 SoT

## 2.1 选择：OpenClaw 是逻辑单一事实源

推荐把 **OpenClaw Task Service** 定义为逻辑 SoT。

第一阶段它的实际存储仍是当前已经在使用的：

`scripts/todo.js -> workspace/memory/tasks/*.md`

但 Life Logger 与 Hermes **只依赖稳定的 OpenClaw Task API / bridge contract**，
不依赖 Markdown 路径、正则或 SQLite schema。

这样以后 OpenClaw 内部可以从 Markdown 升级成 Workboard，而 Life Logger/Hermes
不需要跟着重写。

### 为什么不选 Life Logger

Life Logger 的任务状态目前只有：

- pending
- in_progress
- completed

并且核心身份是生活日志：任务来自录音事件，必须保留 `event_id`、证据、时间与来源。
它缺少全局项目、优先级、阻塞、负责人、执行运行、评论等模型。

把它升级成总任务管理器会把 Life Logger 做成另一个 OpenClaw/Hermes。

### 为什么不选 Hermes

Hermes 的 Kanban/Projects 模型最强，但当前本机没有真实 task/project 数据，而且 Hermes
属于“接入时可用”的执行器。如果把 Hermes 设为 SoT，会导致“不启动/不用 Hermes 时，
Life Logger 与 OpenClaw 的普通待办也失去主库”，不符合本地日常使用方式。

### 为什么暂时不直接选 Workboard SQLite

Workboard 插件已启用，schema 足够强，但当前完全空，且本次本机调研没有确认到一个应当
让外部进程依赖的稳定公开 HTTP/CLI 写接口。

所以：

- **逻辑 SoT：OpenClaw**
- **阶段 1 物理后端：memory/tasks + todo.js**
- **未来可替换后端：Workboard**
- **禁止外部系统直连 Workboard SQLite**

## 2.2 写权限矩阵

| 系统 | 读统一任务 | 创建任务 | 改状态/标题/项目 | 写执行进展 |
| --- | --- | --- | --- | --- |
| OpenClaw | 是 | **是，主写端** | **是，主写端** | 是 |
| Life Logger | 是 | 仅“用户明确确认创建”后，经 OpenClaw bridge | 仅用户明确操作后，经 bridge | 只写来源/evidence，不自行宣布全局完成 |
| Hermes | 是 | 原则上先在 OpenClaw 建主任务，再镜像执行任务 | 只通过 OpenClaw bridge 回写 | **是，负责 run/progress/result** |
| Workboard | 是 | 若未来接成 OpenClaw 主 UI，可写 | 经 OpenClaw 内部 Task Service | 展示/编排 |

用户在任意入口明确完成一个任务，最终都必须落到 OpenClaw SoT；其他本地副本只做缓存或投影。

---

## 3. 统一数据模型

对外不要暴露 Markdown 行格式，定义一个稳定的 `unified-task-v1`。

建议字段：

```json
{
  "schema_version": "unified-task-v1",
  "id": "oc_task_01...",
  "title": "提交 Life Logger 周报",
  "status": "pending",
  "priority": "P2",
  "project_id": "life-logger",
  "tags": ["life-logger"],
  "assignee": null,
  "source": {
    "system": "life_logger",
    "event_id": "evt_xxx",
    "job_id": "job_xxx",
    "artifact_path": ".../events.json"
  },
  "created_at": "2026-09-22T20:00:00+08:00",
  "updated_at": "2026-09-22T20:10:00+08:00",
  "revision": 4
}
```

建议统一状态：

- `pending`
- `in_progress`
- `blocked`
- `completed`
- `dismissed`

Life Logger 初期 UI 仍可只提供三态按钮：

- pending -> pending
- in_progress -> in_progress
- completed -> completed

读取到 blocked/dismissed 时只展示 badge，不擅自映射回 pending。

### 3.1 稳定 ID

这是第一批实现的前置条件。

OpenClaw 当前普通 `todo.js add` 不保证给每条任务稳定 ID，虽然微信自动化条目已经出现
`[id:wx-...]`。

推荐：

1. 所有新任务由 OpenClaw Task Service 生成 `oc_task_<uuid/ulid>`；
2. Markdown 后端可暂时编码为 `[id:oc_task_...]`；
3. 老任务在第一次被 bridge 枚举时做一次可审计的 ID backfill；
4. 外部系统永远使用 `id`，不用“行号、标题关键词、文件路径”当主键。

### 3.2 Project

统一协议从第一天就保留 `project_id`，即使当前 Markdown 后端项目能力较弱。

阶段 1 可以把项目元数据编码在 OpenClaw 内部任务记录中；阶段 3 若迁移到 Workboard，
`project_id` 可映射到 board/project，不改变外部协议。

Hermes 的 `projects.db` 只存 OpenClaw project 的运行时投影，例如仓库路径、worktree、
board 绑定；不反过来成为项目名称/归属的最终事实源。

---

## 4. 同步方向与协议

## 4.1 总体拓扑

```text
Life Logger todo event
        |
        | 用户明确确认 / 自动规则允许创建
        v
OpenClaw Task Bridge / Task Service  <---->  OpenClaw UI / todo.js / future Workboard
        ^
        |
        +---- Life Logger NEXT / daily-brief 只读任务状态
        |
        +---- Hermes 领取任务 -> 执行 -> 回写 progress/complete
```

### 核心原则

**同步不是“文件 A 复制到文件 B”。**

只有 OpenClaw 保存最终任务状态；Life Logger/Hermes 保存各自领域必须保留的东西：

- Life Logger：事件、证据、录音来源、`event_id -> external_task_id` 链接；
- Hermes：run、worker、session、执行日志、结果、失败原因；
- OpenClaw：任务标题、状态、优先级、项目、负责人、生命周期。

## 4.2 推荐协议：localhost HTTP

不推荐把共享 Markdown/SQLite 当协议。

建议 OpenClaw 提供 loopback-only bridge：

- `GET /v1/tasks`
- `GET /v1/tasks/{id}`
- `POST /v1/tasks`
- `PATCH /v1/tasks/{id}`
- `POST /v1/tasks/{id}/progress`
- `GET /v1/projects`
- `GET /v1/changes?cursor=...`

Phase 1 可以由轻量 Node bridge 包装现有 `todo.js`/任务后端；未来切 Workboard 时接口不变。

### 创建幂等

Life Logger 从一个 todo 事件创建任务时：

`Idempotency-Key: life-logger:event:<event_id>`

OpenClaw bridge 保存 `source.system + source.event_id` 唯一约束；同一录音事件重复处理不能刷出多条任务。

Hermes 创建执行镜像：

`Idempotency-Key: hermes:mirror:<openclaw_task_id>`

## 4.3 拉取 + 事件通知

首版采用：

1. 页面打开 / Agent 查询时立即 GET；
2. Life Logger Web 运行时每 30–60 秒轻量拉取 open task revisions；
3. 后续再加 OpenClaw `task.changed` webhook 降低延迟。

无需一开始做双向实时 websocket。

Life Logger 已有 daily-brief webhook，但它的职责是“简报出站”，不是任务数据库同步；
不要复用它来偷偷改任务状态。

## 4.4 冲突处理

由于只有 OpenClaw 是状态 SoT，冲突规则简单：

1. `revision` 单调递增；
2. PATCH 必须带 `If-Match: <revision>` 或请求体 `expected_revision`；
3. revision 不一致返回 409，客户端重新 GET 后让用户/Agent基于最新状态决定；
4. 用户在 OpenClaw 的手工修改优先于旧的 Life Logger/Hermes 缓存；
5. Hermes 只能提交 progress/result 或合法状态转换，不能用旧 run 覆盖用户刚完成/取消的任务；
6. Life Logger 的事件证据不可被 OpenClaw/Hermes 改写；只允许更新“这个事件关联了哪个 task id”。

### 字段所有权

- `title/status/priority/project_id/tags`：OpenClaw
- `source.life_logger.* / evidence`：Life Logger
- `execution.* / run_id / result / progress`：Hermes
- `revision/updated_at`：OpenClaw Task Service

## 4.5 删除

外部客户端不做 hard delete。

统一使用 `dismissed` / archive tombstone。这样 Life Logger 的历史日记不会因任务被“删除”
而失去来源链。

---

## 5. 鉴权与安全

### 5.1 OpenClaw Task Bridge

- 默认只监听 `127.0.0.1`；
- 独立 Bearer token，文件权限 0600；
- token 不写进 Life Logger 仓库、Hermes 仓库、Markdown 或日志；
- 至少区分：
  - `tasks:read`
  - `tasks:write`
  - `projects:read`
  - `projects:write`（后续）

Life Logger 与 Hermes 各自拿独立 client token，便于撤销和审计。

### 5.2 Life Logger 当前 API

Life Logger 已有 Bearer token，并且浏览器写操作还要求：

- 同源 `Origin`
- `X-Life-Logger-CSRF: 1`

这套保护适合浏览器 UI，不适合把 `/api/tasks/status` 当作 OpenClaw/Hermes 的跨进程主写接口。

因此推荐方向是：

**Life Logger -> OpenClaw bridge**

而不是 OpenClaw/Hermes 持续 POST Life Logger `/api/tasks/status`。

未来 Life Logger 若确实需要 server-to-server 写 API，应单独增加 scoped service token，而不是绕过现有 CSRF。

---

## 6. Life Logger 如何改

Life Logger 的七类事件契约不动，`todo` 仍然是事件类别。

变化的是“事件”和“任务”的边界：

### 现在

```text
录音 -> todo LifeEvent -> NEXT
                    -> task-states.json 改状态
```

### 目标

```text
录音 -> todo LifeEvent（证据/候选）
                    |
                    | 创建成功后保存 external_task_id
                    v
             OpenClaw canonical task
                    |
                    +-> Life Logger NEXT 读取状态
                    +-> daily-brief 引用状态
                    +-> Hermes 可领取执行
```

因此 Life Logger 的 `task-states.json` 后续降级为：

- 兼容旧版本；
- 缓存最近一次远端状态；
- 保存 `event_id <-> openclaw_task_id` 迁移信息；

但不再决定“这个任务最终是 pending 还是 completed”。

### 候选创建策略

录音里识别出 todo **不等于立即创建全局任务**。

推荐：

- 明确主动命令：“提醒我明天提交报告” -> 可自动提交 OpenClaw；
- 被动/低置信提取 -> 先作为 Life Logger todo candidate；
- 用户在 NEXT 点击“加入待办”或明确确认后 -> POST OpenClaw；
- 已有 `event_id` 的重复处理 -> 幂等返回原 task。

这可以避免把背景对话、误识别重新污染全局任务列表。

---

## 7. Hermes 接入规则

Hermes 保留自己的 `kanban.db`，因为它需要运行时状态：

- ready/running/blocked/done；
- worker PID / heartbeat；
- task_runs；
- session / result / failure；
- dependency / review。

但这些字段是**执行状态**，不是用户任务 SoT。

### Hermes 接入流程

1. 从 OpenClaw GET task/project；
2. 用户或 OpenClaw 把某 task 标成“交给 Hermes”；
3. Hermes 用 `openclaw_task_id` 作为 idempotency/source key 创建 kanban mirror；
4. Hermes 执行期间 POST progress 到 OpenClaw；
5. 成功后请求 OpenClaw 把 canonical task -> completed；
6. 若用户已在 OpenClaw dismiss/complete，则 Hermes 停止用旧 run 覆盖它；
7. Hermes project 只镜像 OpenClaw project，并附加本地 repo/worktree 配置。

Hermes 不运行时，OpenClaw/Life Logger 的 todo 仍完全可用。

---

## 8. OpenClaw Workboard 的定位

Workboard 很适合最终承载：

- Project / board；
- card；
- priority/status；
- comments；
- attempts/runs；
- artifacts；
- links；
- notifications。

但当前数据库为空，而且外部稳定接口未确认。

所以迁移顺序必须是：

`外部统一 contract -> OpenClaw Task Service -> 内部 backend 可替换`

而不是：

`Life Logger -> 直接写 workboard.sqlite`

未来若 Workboard 成熟，OpenClaw Task Service 内部从 Markdown backend 切换到 Workboard backend；
Life Logger 与 Hermes 无感。

---

## 9. 最小实现：2–3 个小批次

## Batch 1 — OpenClaw Task Bridge 骨架 + 稳定 ID

目标：先做唯一写入口，不改 Life Logger UI 逻辑。

OpenClaw 侧：

1. 给 Task Service/bridge 定义 `unified-task-v1`；
2. 为新任务生成稳定 `oc_task_*` ID；
3. 对现有 `memory/tasks` 做只读 export + 可审计 ID backfill；
4. 提供 `GET /v1/tasks` 与 `GET /v1/projects`；
5. 再提供最小 `POST/PATCH task`，内部只经 OpenClaw 自己的 mutation 层；
6. 对当前 `[~]` / `[state:*]` 历史格式做兼容测试；
7. localhost + scoped token。

**本批次（当前）不写这段代码。**

原因：这些改动的正确落点是 OpenClaw workspace/plugin，而不是 Life Logger 仓库；在 OpenClaw 稳定接口出现前，在 Life Logger 里写 Markdown/SQLite adapter 会形成错误耦合。本批只把 contract 和迁移边界落定。

## Batch 2 — Life Logger 接 OpenClaw

1. 增加可选 `openclaw_tasks` 本地配置；
2. server-side client 拉取 `/v1/tasks`；
3. NEXT 用 OpenClaw canonical status，LifeEvent 只提供来源；
4. 用户改状态 -> Life Logger server 代理 PATCH OpenClaw；
5. 创建采用 `Idempotency-Key: life-logger:event:<event_id>`；
6. `task-states.json` 保留兼容缓存，不再是 SoT；
7. daily-brief 的 todos 取 unified task 状态；
8. OpenClaw 不可用时显示“任务服务离线”，不静默回到另一套可写主库。

验收：同一任务在 OpenClaw 完成后，Life Logger NEXT 下一次拉取立即消失/显示完成，不需要再在 Life Logger 点一次。

## Batch 3 — Hermes + Workboard 项目投影

1. OpenClaw project -> Hermes project mirror；
2. OpenClaw task -> Hermes kanban execution mirror；
3. Hermes progress/result -> OpenClaw；
4. Workboard 作为 OpenClaw 内部 project/task UI；
5. 完成 Markdown -> Workboard backend 迁移后，再把 `memory/tasks` 降级成导出/备份。

验收：OpenClaw、Life Logger、Hermes 三处看到同一 task id；状态只需改一次。

---

## 10. 明确不做

本方案不做以下事情：

- 不重做已经完成的 P4/P5；
- 不修改七类 LifeEvent 契约；
- 不要求云端 ASR 或云端 LLM 才能运行；
- 不让 Life Logger/Hermes 直接写 OpenClaw Markdown；
- 不让 Life Logger/Hermes 直接写 Workboard SQLite；
- 不让 OpenClaw 直接把 Life Logger `task-states.json` 当主库；
- 不把 daily-brief webhook 冒充任务双向同步；
- 不启动、重启、停止或修改 Hermes 进程；
- 不修改 Hermes `kanban.db` / `projects.db`；
- 不新开 ChatGPT 对话。

---

## 11. 最终推荐

当前最稳妥的统一路径是：

**OpenClaw 负责“我到底有哪些任务/项目、现在是什么状态”；  
Life Logger 负责“这个任务从哪段生活证据里来”；  
Hermes 负责“这个任务如果交给 Agent 执行，具体跑到了哪一步”。**

因此不要做“两边同步两份 todo list”，而要做：

**一份 OpenClaw canonical task + 两个领域投影。**

这个边界同时满足：

- 现有 OpenClaw 历史待办不丢；
- Life Logger 不膨胀成项目管理器；
- Hermes 可插拔；
- 未来 OpenClaw 从 Markdown 换 Workboard 时，外部协议不变。
