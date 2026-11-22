# 活动事实结构 v3（设计稿）

五层框架第 3、4 层共用，唯一定义在 `src/life_logger/fact_schema.py`，提示词和校验都从这里生成。依据：`work/llm-audit-20261106/report.md`。活动运行时已接可选 v3 路径，默认 `activity_prompt.version=v2`、`mode=off`；先使用 shadow 留档，不自动进入正式账本或页面。开关、隔离目录和 live 边界见 [activity-v3-shadow.md](activity-v3-shadow.md)。源码接线不代表已部署。

## 活动

| 字段 | 类型 | 说明 |
|---|---|---|
| activity_id | string | 重叠窗口复用 |
| title | string，≤20 字 | 人话，不写时间和“录音中疑似” |
| summary | string | 一句话 |
| status | planned / ongoing / completed / cancelled / unknown | 只这 5 种 |
| lifecycle | live / media / unknown | 本人现场、媒体、拿不准 |
| category | 工作·项目 / 运动·健身 / 饮食 / 消费·购物 / 出行 / 社交·通话 / 健康·作息 / 家务·琐事 / 闲聊·其他 / 媒体内容 | 模型直接给，废关键词表 |
| start_at / end_at | ISO 或 null | 发生时间，不抄说话时间 |
| items | 数组 | 见下 |

## 子项

| 字段 | 类型 | 说明 |
|---|---|---|
| item_id、action | string | |
| fact_type | expense / food / exercise / smoke / todo / idea / other | other 表示无需入账的事实 |
| status | 同活动 | |
| spoken_at | ISO | 代码填，不由模型给 |
| occurred_at | ISO 或 null | 没明说钟点为 null |
| occurred_date | YYYY-MM-DD 或 null | “昨晚”能确定日期但没钟点时用 |
| correction_of | item_id 或 null | 改口时指向被改的条目，以最后说的为准 |
| restatement_of | item_id 或 null | 复述 |
| confidence | 0～1 | 不预设范围 |
| reason | string，≤20 字 | 判断依据 |
| evidence | [{source_id, quote}] | 逐字；其他坐标由代码回查 |
| expense | {amount, currency, item, payment_status: paid/unpaid/unknown} 或 null | 只放钱 |
| food | {name, meal: breakfast/lunch/dinner/snack/unknown, grams, portion} 或 null | 不让模型算营养 |
| exercise | {name, sets, reps, weight_value, weight_unit, side: per_side/total/unknown, duration_seconds} 或 null | |
| smoke | {count, brand, kind: smoked/bought/urge} 或 null | 念头和买烟不算吸烟 |
| todo | {text, due} 或 null | 进统一任务库 |

删除：unit、location 平铺、related_transaction_ids、duration_seconds（平铺）、amount_kind。

## 校验层去向

- 入账：本人、live、completed、有逐字证据、把握达标。
- 待确认：有证据但缺一项，或把握不够。附缺什么。
- 无关：媒体、他人、无信息。

没有“静默拒绝”。枚举外的值在校验层报错并记录，先用 json_schema，上游不支持再本地强校验并重试一次。

## 迁移

旧活动不改。新结构带 schema_version=3，读取方按版本分支；切换日前读旧口径。
