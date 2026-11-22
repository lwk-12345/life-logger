# 饮食只读 API（Life Logger 唯一事实源）

**饮食只来自手表，2026-11-06 用户决定。** 手表录音上传到 Life Logger 后，由后台按现有事件、活动与营养库计算。现有普通事件/人工订正读取口径保留。OpenClaw 的饮食文字记录不进入 Life Logger，不自行估算营养；只在晚间总结中只读后台结果。

## GET /api/nutrition/day?date=YYYY-MM-DD

需要现有 mobile access token：`Authorization: Bearer <mobile-token>`，或服务已有认证 cookie。未配置令牌或缺鉴权返回401。GET不需要CSRF；无效/缺失日期返回400。日期必须为有效ISO日期，日界遵循现有UTC+8账本口径。

读取同root的 canonical event_view（provisional/display）和当日活动，交给现有 `day_facts.domain_ledgers → build_nutrition_ledger`。普通事件的餐次、回顾、邻近上下文及摄入/购买区分都沿用账本规则，没有微信陈述专用分支。

响应字段：

- `ok`、`day`、`has_records`。
- `totals`：`calories_kcal`、`protein_g`、`carbohydrates_g`、`fat_g`、`sodium_mg` 的已知合计。
- `meals`：直接返回现有账本的餐次和条目；`coverage`、`unknown_nutrition_count`、`needs_review` 同样引用账本结果。
- `estimated_calories_kcal`、`estimated_share`：去重后实际摄入记录中，按既有标准份估算的已知热量及其占当天已知热量的比例。

没有食物记录时 `has_records=false`、`totals=null`。有记录但某营养值全部未知时，该总值为null，不用0冒充已知。每条`basis`为`weighed`、`standard_portion`或`unknown`；标准份保留原有估算依据与置信说明。未知热量不进入估算占比分母；空日/全部未知时两个估算字段为null，已知总热量为0时占比为null。

本接口只读取现有品牌/成分表/USDA本地库，显式关闭会联网或写回缓存的OFF补查；查不到保持未知，不增加另一套营养算法。GET不创建手动事件、回执、幂等记录或任务目录，不改历史产物。若仍有遗留饮食手动事件文件，其JSON损坏或events格式无效时保留503且不覆盖；该只读保护不读取回执或恢复写入。

## OpenClaw 的使用边界

仅运行：

```sh
node scripts/life-logger-nutrition.js day --date YYYY-MM-DD
```

数字和缺数据项数逐字引用该命令输出。估算热量直接引用后台`estimated_calories_kcal`，不自行乘比例；读不到时说明Life Logger暂不可读，不从日记推算、补数或用0替代。

饮食写入入口已撤销；旧写入请求按未知路由返回404（仍受全局鉴权/CSRF约束）。CLI旧post命令退出2并提示“饮食只来自手表，OpenClaw 不写入”，不会读凭据、发请求或建立待补记录。总控已调整的daily-checkin Skill保持不变。

## 验证和发布

本次验收和33日只读账本对比见 `work/nutrition-watch-only-20261106/report.md`。未重启前，工作树删除不代表长驻服务已卸载旧路由；必须由总控发布后确认。此接口不进行模型推理，不重算或覆盖历史。
