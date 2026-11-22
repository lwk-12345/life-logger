# 决策 Provider（三条线）—— 怎么用

> **2026-10-16 最新决定**：用户已改为弃用Kev，由Jev/Clef各独立处理一份数据；本文件下方三线实验说明保留历史，不再代表当前运行目标。当前实现、配置、双路产物和接力状态以[双路架构](architecture/jev-clef-dual-pipeline.md)及`.planning/life-logger/task_plan.md`顶部为准。Kev及旧旁路LaunchAgent已停用，不要按下方旧命令恢复。

> 本文档补的是一个真实缺口：台账里有大量「当初怎么调通」的考古记录，
> 但没有一处写清**三条线分别怎么切、怎么跑对照、结果落在哪**，
> 导致接手的人（含 AI）读完台账仍不知道该怎么用。这里补上。

## 一句话区分（最容易搞混的一点）

| | 生产（线上跑的那条） | 对照（离线的评分实验） |
|---|---|---|
| **是什么** | 每天真实处理事件时，用哪个模型做「是不是生活事件 / 属于哪类 / 已发生还是计划」的判断 | 同一批样本喂给多条线，比谁判得准 |
| **怎么控制** | `local-config.json` 的 `decision_provider` | 跑 `~/apps/sv-bench/three_line_compare.py` |
| **影响生产吗** | **会** | **不会**（纯离线，只读样本、只写成绩文件） |
| **结果的验收口径** | 网页上事件分类对不对 | `three_line_60.json` 的 9 维准确率 |

**⚠️ 对照脚本不会改生产配置**，反之亦然。想「换生产用的模型」只需改 `decision_provider`；
想「再比一轮」是跑对照脚本。两件事不要混。

## 三条线分别是什么

| 线 | 位置 | 端点 | 凭据在哪 |
|---|---|---|---|
| **Kev-0.8B** | **本地** | `http://127.0.0.1:8000/v1/systemone` | 无需凭据（本机服务） |
| **Jev** | **云端** Command Code | `https://api.commandcode.ai/provider/v1/systemone` | `output/local-web/secrets.json` 的 **`deepseek_api_key`** |
| **Clef-flash** | **云端** Cloudflare Workers AI | `https://api.cloudflare.com/client/v4/accounts/<ACCOUNT>/ai/run/@cf/cloudflare/clef-flash` | **环境变量** `CLOUDFLARE_ACCOUNT_ID` + `CLOUDFLARE_AUTH_TOKEN`（`~/.hermes/.env`） |

**凭据命名的坑**：Jev 的 key 存在名为 `deepseek_api_key` 的字段里 —— **那是历史命名，不是 DeepSeek**。
改它等于改 Jev 的凭据，别误以为在动 DeepSeek。

## 一、切换「生产」用哪条线

**唯一开关**：`local-config.json` 的 `decision_provider`。

```jsonc
{
  "decision_provider": "kev",        // 当前值。可选："kev" | "jev"
  "use_jev": false,                  // 旧开关，仅对 jev 生效
  "jev": { "enabled_by_default": false, "min_confidence": 0.72 },
  "kev": { "base_url": "http://127.0.0.1:8000", "model": "jaredpalmer/kev-0.8b", ... }
}
```

- 代码侧的合法值：`src/life_logger/decision_provider.py` 的
  `SUPPORTED_DECISION_PROVIDERS = ("jev", "kev")`。
  **传别的值会直接报错**（不会静默退回）。
- **网页端也能切**：设置 → 决策增强 → 决策 Provider（下拉框）。
- 改完 `local-config.json` **要重启 8765 才生效**：
  ```bash
  launchctl kickstart -k gui/501/com.life-logger.local-web
  ```

**Kev 是本地服务，必须先起来它**，否则线上推理会失败：

```bash
launchctl list | grep kev                       # 看有没有在跑
curl -s http://127.0.0.1:8000/v1/models         # 期望看到 model=kev-latest
launchctl kickstart -k gui/501/com.life-logger.kev-service   # 起/重启
```

## 二、跑一轮「对照」

**⚠️ 前置**：三条线必须**同题同问法**才可比（脚本里已强制都用 V15 提示词）。
不要把 V10/V11 时代的旧答案拿来直接比。

```bash
cd ~/apps/sv-bench
set -a; . ~/.hermes/.env; set +a     # 载入 Cloudflare 凭据（Clef 用）

python3 three_line_compare.py 10     # 先试 10 条，确认三条线都通
python3 three_line_compare.py        # 全量 60 条
```

**脚本做的事**：同一批 60 条样本 × 同一套 V15 提示词（`kev_prompts_v15.py`）
→ 分别打 Kev / Jev / Clef → 与金标逐维比对 → 写成绩文件。

**成绩文件**：
- `three_line_60.json` —— 三线结果（每条含 9 维判定 + 耗时）
- `clef_answers_60.json` —— **金标答案**（名字带 clef，但它是**答案**不是模型输出，别搞混）
- `three_line_compare.py` / `four_way_compare.py` —— 对照脚本
  （`four_way` 是加一路老 Kev-4B、且只补跑 Clef 的变体）

**为什么是 9 维不是 11 维**：金标 11 维、模型 10 维，
公共可比的是 9 维 —— `media / self / novelty / people / goal / health / expense / amount / quantity`。
（`location` / `timeline` 金标有、模型无；`action` 模型有、金标无。）

**跑的姿势**：脚本内部用线程池并发，三条线同时打。
本机 swap 很紧（曾 9.58/10.24GB），**跑之前确认没有别的重活在抢内存**。

## 三、当前状态（2026-10-15）

| 线 | 角色 | 状态 |
|---|---|---|
| **Kev-0.8B** | **生产主线**（`decision_provider = "kev"`） | ✅ 在跑（服务已起，`model=kev-latest`） |
| **Jev** | 旁线对照 | 配置在、`use_jev=false`（不参与线上），按需跑对照 |
| **Clef-flash** | 旁线对照 | 脚本可用；**已知坑：Cloudflare token 权限错会 401**，需重建带 Workers AI 权限的 token |

**最近一轮成绩（2026-10-15，60 条 × 9 维）**：

| 维度 | Kev-0.8B | Jev | Clef |
|---|---|---|---|
| media | 38% | 90% | **93%** |
| self | 80% | 88% | **92%** |
| novelty | 55% | **87%** | 77% |
| people | 82% | 92% | 92% |
| goal | 77% | 92% | **95%** |
| health | 3% | 17% | 3% | ⚠️ 口径待修 |
| expense | 7% | **80%** | 78% |
| amount | 93% | **98%** | 95% |
| quantity | 67% | 83% | **88%** |
| **合计** | **55.7%** | **80.7%** | **79.3%** |

**耗时**：Kev-0.8B ≈ 18.09s/条；Jev ≈ 1.92s/条；Clef ≈ 1.74s/条。

**用户决定的处置方式**：三线**并行跑几天**再定去留（成本都不高）。
所以「三条线都在」是**刻意保留的实验状态**，不是配置混乱。

## 四、`evidence.decision_source` 为空 —— 这是正常的，别误判

产物的 `context-extraction.json` 里，事件的 `evidence.decision_source` 通常为空。
**这不代表决策增强没跑**：三条线的对照结果写在 `~/apps/sv-bench/` 的成绩文件里，
不写进事件 evidence。

**教训**：看到字段为空，先问「这个字段本来该由谁写」，
不要直接判定整条链路断了（2026-10-15 曾据此误报「决策增强可能没在跑」）。

## 日常三线旁路对照（2026-10-16 接入）

主线继续使用本地 Kev 0.8B；Jev、Clef 两条云端旁线异步处理 **Kev 实际收到的同一份 state/questions**，
用于持续积累更换主模型所需的数据，不参与正式事件裁决。
新增 `decision_shadow.enabled`、`scripts/run_decision_shadow.py` 和独立 LaunchAgent（每30秒检查队列）。
结果位于 `output/local-web/decision-shadow/`：`samples/` 保存原始输入与Kev回答，`results/` 独立保存两云端回答，
`summary.json` 汇总按问题版本分组的一致性、未知比例、延迟和失败情况。一致性不是有金标的准确率。
旧 `~/apps/sv-bench/sidecar/` 与 V15 实验保留，新旧问题定义分开比较，不覆盖旧文件。
worker已安装启动；旧Web进程仍需安全重载才能加载新版正式请求入队逻辑。
详细验证及输入输出修复见 [input-output-audit-2026-10-16.md](input-output-audit-2026-10-16.md)。

## 十一维提取提示（2026-10-16 新增）

生产抽取新增 `kev.annotation_mode=dimensions`：十一组信息、十五个子问题，
输出只作为千问/云端抽取的线索，不能过滤原文或升级声源身份。
`event_gate=false` 只表示旧过滤关闭，并不表示 Kev 不做注解。
配置、维度定义、低置信候选与验证边界见 [decision-dimensions.md](decision-dimensions.md)。
旧 V10/V14/V15 实验和成绩保留，新定义不能直接套旧金标比较。
本次没有新增自动升级云端 DeepSeek 的路由。

## 五、相关文件

- 开关与配置：`local-config.json`（`decision_provider` / `jev` / `kev`）
- 代码侧枚举：`src/life_logger/decision_provider.py`（`SUPPORTED_DECISION_PROVIDERS`）
- 调用点：`src/life_logger/contextual_extraction.py`（import decision_provider）
- 对照脚本：`~/apps/sv-bench/{three_line_compare.py, four_way_compare.py}`
- 提示词：`~/apps/sv-bench/kev_prompts_v15.py`（**当前版本；旧 v10 仅供考古**）
- 成绩与金标：`~/apps/sv-bench/{three_line_60.json, clef_answers_60.json}`
- 历史文档（Laya 时代，已过时，仅供追溯）：`docs/laya-decision-provider.md`
