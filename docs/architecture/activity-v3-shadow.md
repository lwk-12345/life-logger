# 活动提示词 v3：默认关闭与影子运行

代码接线不等于已加载。当前默认 `activity_prompt.version=v2`、`mode=off`；没有修改 local-config.json。上线和配置切换由总控操作。

配置示例（仅文档，不是当前配置）：

```json
{
  "activity_prompt": {
    "version": "v3",
    "mode": "shadow",
    "user_profile_path": "config/user-profile.md"
  }
}
```

| version | mode | 正式窗口结果 | 额外输出 |
|---|---|---|---|
| v2 / 缺省 | 任意合法 mode | 原 v2 | 无 |
| v3 | off / 缺省 | 原 v2 | 无 |
| v3 | shadow | 原 v2 | 同窗口 v3 独立档案 |
| v3 | live | 过 schema 与现有逐字证据门的 v3 | 留存 v3 原始结果及拒绝原因 |

非法枚举保守回退为 v2/off，不会自动启用 v3。关闭只需 mode=off；历史不重算。

模板为 `config/activity-prompt-v3.md`，与原 `work/prompt-v3-ab-20261106/prompt_v3_template.md` 字节相同，没有使用 round3。背景默认读 `config/user-profile.md`，可以通过 `activity_prompt.user_profile_path` 更换文件（相对仓库根，亦可绝对路径），或通过 `activity_prompt.user_profile` 配置内联文字。该文件不放手机号或密钥。

v3 使用 `fact_schema.json_schema()` 生成 response_format=json_schema。HTTP 400/422/501 明确拒绝 schema，或正常响应明确拒绝 schema 时，才回退 json_object 一次，记录 `schema_fallbacks`。配额、认证、网络异常不会作为 schema 不支持而重试。结构校验失败不做静默过滤：原响应、原内容、全部条目和结构化错误都留在：

```
output/local-web/activity-shadow-v3/YYYY-MM-DD/<窗口时间>-<输入摘要>-<唯一标识>.json
```

日期按 UTC+8。此目录不参与活动读取或账本输入，不新增页面入口。shadow 在原 v2 尝试结束后执行，包括 v2 请求或证据校验失败的窗口；原 v2 的结果或异常保持不变，避免影子样本漏掉 v2 的失败窗口。v3 请求或档案写入失败不能改变 v2 返回值。当前实现是同步影子调用，会在正式窗口返回前增加模型请求等待时间；复用原提取并发闸门与超时配置。schema 不被支持时最多有两次额外请求。

用量分别记作 `activity_v3_shadow` / `activity_v3_live`，通过 `model_usage.record`，只含模型、token、耗时和状态，不含提示词、原话、密钥或完整 URL。影子档案保留模型原始输出及证据，schema 合格不等于业务事实完全正确。

显式 live 保留嵌套的 v3 事实字段与活动 `fact_schema_version=3` 标记，代码回查 source_id 补齐 segment_id/row_id 和 spoken_at，再用现有联合坐标及逐字引用门校验。只把 expense 中已有 amount/currency/payment_status 镜像给旧读路径；不将克数或重量当金额，不推断已付款或发生时间。源 ID 未知/歧义、引用不符或时间越界会拒绝该窗口，原文和 `publication_validation` 原因仍留档。旧活动不重写。

live 当前按本窗口可唯一回查的 source_id 接受证据。包含仅存在于历史上下文的引用会保守拒绝；三本账、旧页面和 v3 全字段的完整呈现尚未做真实生产验证。应先检查影子档案，再由总控决定是否另行启用 live。

验证命令、实际退出码、全量测试与成本估计见 `work/activity-v3-shadow-20261106/report.md`。本轮只使用假网络，未调用真实模型。
