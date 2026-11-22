"use strict";
const results = document.querySelector("#results");
const status = document.querySelector("#status");
const refresh = document.querySelector("#refresh");
const download = document.querySelector("#download");
let latest = null;
let loading = false;
const stateLabels = { running: "分析中", completed: "处理完成", degraded: "有内容待确认", failed: "处理失败", pending: "等待处理" };
function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined && text !== null) node.textContent = String(text);
  if (className) node.className = className;
  return node;
}
function renderEvent(event, pending) {
  const row = element("article", null, "event");
  row.append(element("span", pending ? "待确认" : "已提取", `badge${pending ? " pending" : ""}`));
  row.append(element("h3", event.title || "未命名记录"));
  const amount = event.data?.expense?.amount;
  if (typeof amount === "number") row.append(element("p", `¥ ${amount.toFixed(2)}`, "amount"));
  const time = event.time?.absolute;
  row.append(element("p", time ? `发生时间：${new Date(time).toLocaleString("zh-CN", { timeZone: "Asia/Shanghai" })}` : "发生时间待确认", "meta"));
  if (event.evidence?.quote) row.append(element("p", `原话：${event.evidence.quote}`, "quote"));
  return row;
}
function render(payload) {
  results.replaceChildren();
  for (const item of payload.items || []) {
    const run = element("section", null, "run");
    run.append(element("p", item.artifact.startsWith("jobs/") ? "正式录音任务" : "实时录音预览"));
    run.append(element("p", item.recorded_at ? `录音时间：${new Date(item.recorded_at).toLocaleString("zh-CN", { timeZone: "Asia/Shanghai" })}` : "同一份录音的独立分析", "scope"));
    const columns = element("div", null, "columns");
    for (const provider of ["jev", "clef"]) {
      const data = item.variants?.[provider];
      const card = element("section", null, `model ${provider}`);
      card.append(element("h2", provider === "jev" ? "Jev" : "Clef"));
      card.append(element("p", stateLabels[data?.status || "pending"] || "等待确认"));
      const formal = data?.formal_events || [];
      const pending = data?.pending_events || [];
      const unresolved = data?.pending_sources || [];
      card.append(element("p", `${formal.length} 条记录 · ${pending.length} 条待确认 · ${unresolved.length} 段待补充`, "meta"));
      if (data?.decision_elapsed_seconds !== undefined) card.append(element("p", `决策 ${data.decision_elapsed_seconds.toFixed(1)} 秒 · 提取 ${(data.extraction_elapsed_seconds || 0).toFixed(1)} 秒`, "meta"));
      for (const event of formal) card.append(renderEvent(event, false));
      for (const event of pending) card.append(renderEvent(event, true));
      for (const source of unresolved) {
        const entry = element("article", null, "event");
        entry.append(element("span", "待补充", "badge pending"));
        entry.append(element("p", source.quote || "此段尚未得到完整结果"));
        card.append(entry);
      }
      if (!formal.length && !pending.length && !unresolved.length) card.append(element("p", data?.status === "completed" ? "这一份分析没有提取到事件。" : "历史结果未完成。", "empty"));
      columns.append(card);
    }
    run.append(columns);
    results.append(run);
  }
  if (!payload.items?.length) results.append(element("p", "暂无历史对照。决策增强已停用，新录音不再生成对照结果。", "empty"));
}
async function load() {
  if (loading) return;
  loading = true;
  refresh.disabled = true;
  try {
    const response = await fetch("/api/decision-comparisons", { cache: "no-store" });
    if (!response.ok) throw new Error(response.status === 401 ? "请先返回工作台登录。" : "暂时无法读取结果，请稍后刷新。");
    latest = await response.json();
    render(latest);
    download.disabled = !latest.items?.length;
    status.textContent = `已停用 · 已读取 ${latest.items?.length || 0} 组历史对照 · 仅供查看`;
  } catch (error) {
    status.textContent = error.message;
  } finally {
    loading = false;
    refresh.disabled = false;
  }
}
refresh.addEventListener("click", load);
download.addEventListener("click", () => {
  if (!latest) return;
  const url = URL.createObjectURL(new Blob([JSON.stringify(latest, null, 2)], { type: "application/json" }));
  const link = element("a");
  link.href = url;
  link.download = "life-logger-jev-clef.json";
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
});
load();
