/* Shared expense drilldown: total -> category -> transaction, inline accordion.
   Amounts always come from /api/daily expense_ledger fields; this module never reclassifies.
   [2026-10-27 P0 Update] Clear separation between:
     1. Confirmed Payments (payment_transactions / total_amount)
     2. Pending Orders / Quoted mentions (pending_transactions) with explicit quote and reasons
     3. Merchant / Inflow Broadcasts (pending_broadcast) with clear non-self attribution note
*/
(function (root) {
  "use strict";
  const CATEGORY_LABELS = {
    food: "餐饮美食", beverage: "饮品", tobacco: "烟草", other: "其他",
    withdrawal: "提现", refund: "退款", transfer: "转账/付款", topup: "充值",
    transport: "交通出行", shopping: "超市采购", daily: "日常消费", subscription: "订阅服务",
  };
  const KIND_LABELS = { consumption: "消费", transfer: "转账付款", withdrawal: "提现", refund: "退款" };
  const SYMBOLS = { CNY: "¥", USD: "$", EUR: "€", JPY: "JP¥", HKD: "HK$", GBP: "£" };

  const PENDING_REASON_LABELS = {
    user_review_pending: "人工复核：等你确认",
    activity_purchase_unconfirmed: "说了购买但没说已付款，等你确认",
    amount_unknown: "原话未提及具体金额，未核实不猜钱",
    order_action_missing: "口头点单/报价提及，缺少实际付款动作凭证",
    pos_broadcast_pending_confirmation: "商家音箱收款播报，待确认是否本人支出",
    pos_broadcast_unbound_order: "有点单意向但未见店员独立问答报价，防盲并",
    pos_broadcast_unattributed: "背景环境收款播报（非本人发声），未计入",
    negated_purchase: "用户明确否定/取消购买",
    planned_or_hypothetical: "未来计划或假设性询价，实际未发生",
    unverified_expense_data: "金额未经验证凭证支持",
    // [2026-11-07 P2] expense_policy.py 新增的待确认原因码：不收录就会把 snake_case 漏到界面上。
    media_audio_third_person: "视频或音乐音轨中的他人提及，等你确认",
    amount_from_other_speaker_unconfirmed: "店员或对方报价，尚未确认本人已付款",
    activity_spend_unconfirmed: "活动提及费用，付款状态待确认",
    activity_purchase_amount_unspoken: "提及购买但原话未说具体金额",
    activity_payment_broadcast_unconfirmed: "现场收款播报，待确认是否本人支付",
    invalid_amount: "金额格式无法解析，待人工核对",
    non_positive_amount: "金额非正数，未计入消费",
    currency_unknown: "币种未说明，待确认",
    aggregate_reconciliation_ambiguous: "合计与分项金额存在歧义，待确认",
    v3_unknown_lifecycle: "活动场景未知（现场或媒体待定），等你确认",
    v3_self_evidence_missing: "缺少本人语音证据，等你确认",
    v3_review_pending: "待人工复核确认",
    v3_payment_unconfirmed: "尚未确认完成付款",
    v3_amount_unverified: "金额未经原话证实",
  };
  const FALLBACK_PENDING_REASON = "待确认真实付款凭证";

  // 英文标识符（snake_case / camelCase 的机器码）绝不直接上界面：
  // 命中词表就用中文；没命中又看着像代码，就退回一句人话。
  function pendingReasonLabel(reason) {
    const raw = String(reason == null ? "" : reason).trim();
    if (!raw) return FALLBACK_PENDING_REASON;
    const hit = PENDING_REASON_LABELS[raw];
    if (hit) return hit;
    if (/^[A-Za-z][A-Za-z0-9_]*$/.test(raw)) return FALLBACK_PENDING_REASON;
    return raw;
  }

  function escapeHtml(value) {
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }
  function number(value) {
    if (value === null || value === undefined || value === "") return null;
    const n = Number(value);
    return Number.isFinite(n) ? n : null;
  }
  function money(value, currency) {
    const n = number(value);
    if (n === null) return "金额未知";
    if (!currency || String(currency).trim().toUpperCase() === "UNKNOWN") {
      return `${n.toFixed(2)} 未知币种`;
    }
    const cur = String(currency).trim().toUpperCase();
    return `${SYMBOLS[cur] || ""}${n.toFixed(2)} ${cur}`.trim();
  }
  function cnyTotal(expense) {
    const buckets = expense && expense.totals_by_currency;
    const direct = buckets && number(buckets.CNY);
    if (direct !== null) return direct;
    const fallback = number(expense && expense.total_amount);
    return fallback;
  }
  function categoryMap(expense, currency) {
    const byCurrency = expense && expense.category_totals_by_currency;
    const rows = byCurrency && byCurrency[currency];
    if (rows && typeof rows === "object") return rows;
    if (currency === "CNY" && expense && expense.category_breakdown && typeof expense.category_breakdown === "object") {
      return expense.category_breakdown;
    }
    return {};
  }
  function transactionsFor(expense, currency, category) {
    return (Array.isArray(expense && expense.transactions) ? expense.transactions : []).filter((tx) => {
      if (String(tx.currency || "CNY").toUpperCase() !== currency) return false;
      if (category && String(tx.category || "other") !== String(category)) return false;
      return number(tx.amount) !== null;
    });
  }
  function txRow(tx, currency) {
    const item = tx.item || tx.title || "未命名消费";
    return `<div class="expense-tx-row">
      <time>${escapeHtml(tx.clock || "时间未知")}</time>
      <div><strong>${escapeHtml(item)}</strong>${tx.quote ? `<p>“${escapeHtml(tx.quote)}”</p>` : ""}</div>
      <span>${escapeHtml(money(tx.amount, tx.currency || currency))}</span>
    </div>`;
  }
  function categoryBlock(expense, currency, cat, amount, total, prefix) {
    const pct = total > 0 ? Math.round((amount / total) * 1000) / 10 : 0;
    const txs = transactionsFor(expense, currency, cat);
    const label = CATEGORY_LABELS[String(cat)] || String(cat);
    return `<div class="expense-category-item">
      <button type="button" class="expense-category-row" data-expense-category-toggle aria-expanded="false" aria-controls="${prefix}-${currency}-${escapeHtml(String(cat))}">
        <span class="expense-category-name">${escapeHtml(label)}</span>
        <span class="expense-category-amount">${escapeHtml(money(amount, currency))}</span>
        <span class="expense-category-count">${txs.length} 笔</span>
        <span class="expense-category-pct">${pct.toFixed(1)}%</span>
        <span class="expense-category-bar" aria-hidden="true"><i style="width:${Math.max(0, Math.min(100, pct))}%"></i></span>
      </button>
      <div class="expense-category-detail" id="${prefix}-${currency}-${escapeHtml(String(cat))}" hidden>
        ${txs.length ? txs.map((tx) => txRow(tx, currency)).join("") : '<p class="expense-empty-line">该分类暂无逐笔明细。</p>'}
      </div>
    </div>`;
  }
  function currencyBlock(expense, currency, total, prefix, primary) {
    const cats = categoryMap(expense, currency);
    const entries = Object.entries(cats)
      .map(([cat, amount]) => [cat, number(amount)])
      .filter(([, amount]) => amount !== null && amount > 0)
      .sort((a, b) => b[1] - a[1]);
    if (!entries.length) return "";
    return `<section class="expense-currency-block${primary ? " is-primary" : ""}">
      <div class="expense-currency-head"><strong>${escapeHtml(currency)}</strong><span>${primary ? "已确认付款消费" : "其他币种（不计入 CNY）"}</span><em>${escapeHtml(money(total, currency))}</em></div>
      <div class="expense-category-list">${entries.map(([cat, amount]) => categoryBlock(expense, currency, cat, amount, total, prefix)).join("")}</div>
    </section>`;
  }

  function pendingBlock(expense) {
    const items = Array.isArray(expense && expense.pending_transactions) ? expense.pending_transactions : [];
    if (!items.length) return "";

    // Group sums strictly by currency; never mix different currencies into a single total
    const sumByCurrency = {};
    let unknownAmountCount = 0;
    items.forEach((tx) => {
      const amount = number(tx.amount) ?? number(tx.proposed_amount);
      if (amount === null) {
        unknownAmountCount++;
        return;
      }
      const cur = tx.currency && String(tx.currency).trim().toUpperCase() !== "UNKNOWN"
        ? String(tx.currency).trim().toUpperCase()
        : "UNKNOWN";
      sumByCurrency[cur] = (sumByCurrency[cur] || 0) + amount;
    });

    const sumSummaryParts = Object.entries(sumByCurrency).map(([cur, s]) => {
      return money(s, cur === "UNKNOWN" ? "" : cur);
    });
    if (unknownAmountCount > 0 && !sumSummaryParts.length) {
      sumSummaryParts.push("金额均未明");
    }

    const sumSummaryText = sumSummaryParts.length
      ? ` · 提及估算：${sumSummaryParts.join(" + ")}`
      : "";

    return `<section class="expense-pending-block" data-expense-pending-block>
      <div class="expense-pending-head">
        <strong>待确认口头点单 / 报价提及</strong>
        <span>共 ${items.length} 笔${escapeHtml(sumSummaryText)}（付款状态待确认，暂不计入总额）</span>
      </div>
      <div class="expense-pending-notice">以下为录音中提及的口头点餐、询价或未核实自述，未证实已完成实际付款，保留原话供核对：</div>
      <div class="expense-pending-list">
        ${items.map((tx) => {
          const amount = number(tx.amount) ?? number(tx.proposed_amount);
          const reasonLabel = pendingReasonLabel(tx.reason);
          return `<div class="expense-tx-row is-pending">
            <time>${escapeHtml(tx.clock || "时间未知")}</time>
            <div>
              <strong>${escapeHtml(tx.item || tx.title || "待确认消费")}</strong>
              <div class="expense-reason-badge">${escapeHtml(reasonLabel)}</div>
              ${tx.quote ? `<details class="expense-quote-drawer"><summary>查看对话原话</summary><p>“${escapeHtml(tx.quote)}”</p></details>` : ""}
            </div>
            <span>${amount === null ? "金额待录" : `提及 ${escapeHtml(money(amount, tx.currency))} · 付款状态待确认`}</span>
          </div>`;
        }).join("")}
      </div>
    </section>`;
  }

  function broadcastBlock(expense) {
    const items = Array.isArray(expense && expense.pending_broadcast) ? expense.pending_broadcast : [];
    if (!items.length) return "";

    const sumByCurrency = {};
    items.forEach((tx) => {
      const amount = number(tx.amount) ?? number(tx.proposed_amount);
      if (amount === null) return;
      const cur = tx.currency && String(tx.currency).trim().toUpperCase() !== "UNKNOWN"
        ? String(tx.currency).trim().toUpperCase()
        : "UNKNOWN";
      sumByCurrency[cur] = (sumByCurrency[cur] || 0) + amount;
    });

    const sumSummaryParts = Object.entries(sumByCurrency).map(([cur, s]) => {
      return money(s, cur === "UNKNOWN" ? "" : cur);
    });
    const broadcastTotalCny = number(expense.pending_broadcast_total_cny);
    let broadcastSumText = "";
    if (sumSummaryParts.length) {
      broadcastSumText = ` · 播报金额：${sumSummaryParts.join(" + ")}`;
    } else if (broadcastTotalCny !== null) {
      broadcastSumText = ` · 播报金额合计 ${money(broadcastTotalCny, "CNY")}`;
    }

    return `<section class="expense-broadcast-block" data-expense-broadcast-block>
      <div class="expense-broadcast-head">
        <strong>收款播报，付款方待确认</strong>
        <span>共 ${items.length} 笔${escapeHtml(broadcastSumText)}（暂未计入已确认消费）</span>
      </div>
      <div class="expense-broadcast-notice">麦克风录到的现场收款到账语音，付款方身份待核实，暂未计入已确认消费：</div>
      <div class="expense-broadcast-list">
        ${items.map((tx) => {
          const amount = number(tx.amount) ?? number(tx.proposed_amount);
          const hasMerchantProof = Boolean(tx.merchant || (tx.provenance && tx.provenance.merchant));
          const badgeText = hasMerchantProof
            ? "商户明确播报 · 待确认是否本人支出"
            : "收款播报，付款方待确认 · 暂未计入已确认消费";
          return `<div class="expense-tx-row is-broadcast">
            <time>${escapeHtml(tx.clock || "时间未知")}</time>
            <div>
              <strong>${escapeHtml(tx.title || tx.item || "收款播报")}</strong>
              <div class="expense-reason-badge is-broadcast-badge">${escapeHtml(badgeText)}</div>
              ${tx.quote ? `<details class="expense-quote-drawer"><summary>播报原文</summary><p>“${escapeHtml(tx.quote)}”</p></details>` : ""}
            </div>
            <span>${amount === null ? "播报金额未明" : `播报 ${escapeHtml(money(amount, tx.currency))}`}</span>
          </div>`;
        }).join("")}
      </div>
    </section>`;
  }

  function nonPaymentBlock(expense) {
    const items = Array.isArray(expense && expense.non_payment_transactions) ? expense.non_payment_transactions : [];
    if (!items.length) return "";
    return `<details class="expense-nonpayment"><summary>非消费资金动作 ${items.length} 笔（提现/转账/退款，不计入合计）</summary>
      ${items.map((tx) => `<div class="expense-tx-row"><time>${escapeHtml(tx.clock || "")}</time><div><strong>${escapeHtml(tx.item || tx.title || "资金动作")}</strong><span class="expense-payment-tag">${escapeHtml(KIND_LABELS[String(tx.kind || "")] || tx.kind || "非消费")}</span></div><span>${escapeHtml(money(tx.amount, tx.currency || "CNY"))}</span></div>`).join("")}
    </details>`;
  }

  function build(expense) {
    if (!expense || typeof expense !== "object") return "";
    const totals = expense.totals_by_currency && typeof expense.totals_by_currency === "object"
      ? expense.totals_by_currency : {};
    let currencies = Object.keys(totals).filter((cur) => number(totals[cur]) !== null);
    if (!currencies.length && number(expense.total_amount) !== null) currencies = ["CNY"];
    const pending = Array.isArray(expense.pending_transactions) ? expense.pending_transactions : [];
    const broadcast = Array.isArray(expense.pending_broadcast) ? expense.pending_broadcast : [];
    if (!currencies.length && !pending.length && !broadcast.length) return "";

    const primary = currencies.includes("CNY") ? "CNY" : (currencies[0] || "CNY");
    const others = currencies.filter((cur) => cur !== primary);
    const primaryTotal = number(totals[primary]) ?? number(expense.total_amount) ?? 0;
    const otherText = others.map((cur) => money(totals[cur], cur)).join(" · ");
    const prefix = "exp-" + Math.random().toString(36).slice(2, 8);

    const pendingCount = pending.length;
    const broadcastCount = broadcast.length;
    let auxiliaryHint = "";
    if (pendingCount > 0 || broadcastCount > 0) {
      const parts = [];
      if (pendingCount > 0) parts.push(`${pendingCount} 笔口头提及待确认`);
      if (broadcastCount > 0) parts.push(`${broadcastCount} 笔收款播报待确认`);
      auxiliaryHint = `（另有 ${parts.join("、")}，可展开查看原话）`;
    }

    return `<div class="expense-drilldown" data-expense-drilldown>
      <div class="expense-drilldown-head">
        <div class="expense-total">
          <span class="expense-total-label">已确认付款消费</span>
          <strong>${escapeHtml(money(primaryTotal, primary))}</strong>
          ${otherText ? `<em>另有 ${escapeHtml(otherText)}</em>` : ""}
          ${auxiliaryHint ? `<span class="expense-auxiliary-hint">${escapeHtml(auxiliaryHint)}</span>` : ""}
        </div>
        <button type="button" class="expense-drilldown-toggle" data-expense-toggle aria-expanded="false">消费账本与提及明细 <span aria-hidden="true">▾</span></button>
      </div>
      <div class="expense-drilldown-body" hidden>
        ${currencies.map((cur) => currencyBlock(expense, cur, number(totals[cur]) ?? (cur === primary ? primaryTotal : 0), prefix, cur === primary)).join("")}
        ${pendingBlock(expense)}
        ${broadcastBlock(expense)}
        ${nonPaymentBlock(expense)}
      </div>
    </div>`;
  }

  function attach() {
    if (root.__expenseDrilldownAttached) return;
    root.__expenseDrilldownAttached = true;
    document.addEventListener("click", (event) => {
      const toggle = event.target.closest("[data-expense-toggle]");
      if (toggle) {
        const box = toggle.closest("[data-expense-drilldown]");
        const body = box && box.querySelector(".expense-drilldown-body");
        if (!body) return;
        const open = body.hidden;
        body.hidden = !open;
        toggle.setAttribute("aria-expanded", String(open));
        toggle.innerHTML = open ? '收起明细 <span aria-hidden="true">▴</span>' : '消费账本与提及明细 <span aria-hidden="true">▾</span>';
        return;
      }
      const catToggle = event.target.closest("[data-expense-category-toggle]");
      if (catToggle) {
        const detail = catToggle.nextElementSibling;
        if (!detail) return;
        const open = detail.hidden;
        detail.hidden = !open;
        catToggle.setAttribute("aria-expanded", String(open));
      }
    });
  }

  const css = `
.expense-drilldown{border:1px solid #e7e2d6;border-radius:14px;background:#fffdf8;padding:14px 16px;margin:12px 0}
.expense-drilldown-head{display:flex;align-items:center;justify-content:space-between;gap:14px;flex-wrap:wrap}
.expense-total{display:flex;align-items:baseline;gap:8px;flex-wrap:wrap}
.expense-total-label{color:#7a7367;font-size:12px;font-weight:600}
.expense-total strong{font:650 22px/1.2 -apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif;color:#24231f}
.expense-total em{color:#8a7f6e;font-size:12px;font-style:normal}
.expense-auxiliary-hint{color:#9e6822;font-size:11.5px;background:#fef7ea;padding:2px 8px;border-radius:6px;border:1px solid #f2dfbd}
.expense-drilldown-toggle{border:1px solid #ddd6c8;background:#fff;border-radius:999px;padding:7px 12px;font-size:12px;font-weight:650;color:#5b5448;cursor:pointer}
.expense-drilldown-body{margin-top:12px;border-top:1px solid #eee8dc;padding-top:10px}
.expense-drilldown-body[hidden],.expense-category-detail[hidden]{display:none!important}
.expense-currency-block{margin:10px 0 14px}
.expense-currency-head{display:flex;align-items:baseline;gap:8px;margin-bottom:8px;color:#6d6558;font-size:12px}
.expense-currency-head strong{color:#2c2a25;font-size:13px}
.expense-currency-head em{margin-left:auto;font-style:normal;font-weight:650;color:#2c2a25}
.expense-category-list{display:flex;flex-direction:column;gap:6px}
.expense-category-row{display:grid;grid-template-columns:minmax(90px,1fr) auto auto auto;gap:10px;align-items:center;width:100%;border:0;background:#faf7ef;border-radius:10px;padding:9px 11px;font:inherit;text-align:left;cursor:pointer;position:relative;overflow:hidden}
.expense-category-row:hover{background:#f6f1e5}
.expense-category-name{font-weight:650;color:#2f2c26;font-size:13px}
.expense-category-amount{font-weight:650;color:#2f2c26;font-size:13px}
.expense-category-count,.expense-category-pct{color:#82796b;font-size:11px}
.expense-category-bar{position:absolute;left:0;bottom:0;height:2px;width:100%;background:transparent}
.expense-category-bar i{display:block;height:100%;background:#c9a86a}
.expense-category-detail{padding:4px 2px 2px 72px;display:flex;flex-direction:column;gap:6px}
.expense-tx-row{display:grid;grid-template-columns:52px minmax(0,1fr) auto;gap:10px;align-items:start;padding:6px 0;border-bottom:1px dashed #eee7da}
.expense-tx-row:last-child{border-bottom:0}
.expense-tx-row time{font:600 11px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace;color:#8a8173}
.expense-tx-row strong{display:block;font-size:12.5px;color:#2f2c26}
.expense-tx-row p{margin:2px 0 0;color:#7d7466;font-size:11.5px;line-height:1.5}
.expense-tx-row>span{font-weight:650;color:#2f2c26;font-size:12px;white-space:nowrap}
.expense-reason-badge{display:inline-block;font-size:10.5px;color:#855c1b;background:#faecd8;padding:1px 6px;border-radius:4px;margin-top:2px}
.is-broadcast-badge{color:#44618a;background:#e5edf8}
.expense-quote-drawer{margin-top:4px;font-size:11px;color:#7a7265}
.expense-quote-drawer summary{cursor:pointer;color:#85642a;outline:none}
.expense-quote-drawer p{margin:3px 0 0;padding:4px 8px;background:#f7f3ea;border-left:2px solid #cfbe9f;border-radius:3px;font-size:11.5px;line-height:1.4;color:#453f36}
.expense-pending-block{margin-top:12px;padding:12px 14px;border:1px solid #ebd9b5;background:#fffaf0;border-radius:10px}
.expense-pending-head{display:flex;gap:8px;align-items:baseline;color:#8a5e1d;font-size:12px;margin-bottom:4px;flex-wrap:wrap}
.expense-pending-head strong{font-size:13px;color:#78490a}
.expense-pending-notice{font-size:11px;color:#8a7450;margin-bottom:8px}
.expense-broadcast-block{margin-top:12px;padding:12px 14px;border:1px solid #c9d8eb;background:#f4f8fd;border-radius:10px}
.expense-broadcast-head{display:flex;gap:8px;align-items:baseline;color:#2c5282;font-size:12px;margin-bottom:4px;flex-wrap:wrap}
.expense-broadcast-head strong{font-size:13px;color:#1a365d}
.expense-broadcast-notice{font-size:11px;color:#4a6585;margin-bottom:8px}
.expense-empty-line{color:#8a8173;font-size:12px;margin:4px 0}
.expense-nonpayment{margin-top:10px;font-size:12px;color:#7d7466}
.expense-nonpayment summary{cursor:pointer;padding:6px 0}
.expense-payment-tag{display:inline-block;margin-left:6px;padding:1px 6px;border-radius:999px;background:#f1ece1;color:#7d7466;font-size:10px}
@media(max-width:620px){.expense-drilldown{padding:12px}.expense-category-row{grid-template-columns:minmax(80px,1fr) auto auto;gap:6px}.expense-category-pct{display:none}.expense-category-detail{padding-left:0}.expense-tx-row{grid-template-columns:44px minmax(0,1fr) auto;gap:7px}}
`;
  try {
    if (!document.getElementById("expense-drilldown-styles")) {
      const style = document.createElement("style");
      style.id = "expense-drilldown-styles";
      style.textContent = css;
      document.head.appendChild(style);
    }
    attach();
  } catch (_) {}
  root.ExpenseDrilldown = { build, cnyTotal, attach, categoryLabel: (key) => CATEGORY_LABELS[key] || key };
})(typeof window !== "undefined" ? window : globalThis);
