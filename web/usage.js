"use strict";

const usageKindNames = {
  entry: "单条整理", day: "每日回顾", memory: "长期记忆",
  portrait: "个人画像", month: "月度回顾", test: "连接测试", other: "其他请求",
};
const usageStatusNames = { running: "进行中", success: "已返回", error: "请求异常", interrupted: "中断 · 用量未知" };
const tokenText = (value) => value === null || value === undefined ? "—" : Number(value).toLocaleString("zh-CN");
const moneyText = (value) => value === null || value === undefined ? "—" : "¥" + Number(value).toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 8 });

function usageTime(stamp) {
  const parts = Object.fromEntries(new Intl.DateTimeFormat("zh-CN", {
    timeZone: state.settings.timezone, year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23",
  }).formatToParts(new Date(stamp)).map((p) => [p.type, p.value]));
  return `<time datetime="${escapeHTML(stamp)}"><span>${parts.year}-${parts.month}-${parts.day}</span><small>${parts.hour}:${parts.minute}:${parts.second}</small></time>`;
}

async function loadUsage({ silent = false } = {}) {
  const ticket = ++state.usageSerial;
  try {
    const data = await api(`/api/usage?month=${encodeURIComponent(state.usageMonth || state.today.slice(0, 7))}&offset=${state.usageOffset}`);
    if (ticket !== state.usageSerial) return;
    state.usage = data;
    if (state.view === "usage") renderUsage();
  } catch (error) {
    if (!silent) toast(error.message);
  }
}

function usageRecord(row, open) {
  const rateText = row.input_rate === null || row.output_rate === null ? "当次未配置单价" :
    `当次单价：输入 ${row.input_rate} / 输出 ${row.output_rate} 元每百万 Token${row.cached_rate !== null ? "；缓存输入 " + row.cached_rate : ""}`;
  const details = [rateText, row.cached_tokens !== null ? `缓存命中：${tokenText(row.cached_tokens)}（已包含在输入中）` : "缓存用量未提供",
    row.reasoning_tokens !== null ? `思考：${tokenText(row.reasoning_tokens)}（已包含在输出中）` : "",
    row.cost_basis === "standard_rates" ? "按标准输入与输出价估算，未计缓存优惠" : "",
    row.total_source === "sum" ? "总 Token 由输入与输出相加" : "", row.usage_note || "",
    row.error_code ? `状态说明：${({ network_or_timeout: "连接异常或超时，服务商可能已产生费用", invalid_response: "响应格式异常", service_restarted: "服务重启前请求未完成" })[row.error_code] || (/^http_\d{3}$/.test(row.error_code) ? row.error_code.replace("http_", "HTTP ") : "请求未完成")}` : "",
    row.returned_model && row.returned_model !== row.model ? `服务实际返回模型：${row.returned_model}` : "",
  ].filter(Boolean);
  const source = row.kind === "entry" && row.source_day
    ? sourceButton({ entry_id: row.target, journal_day: row.source_day, quote: "" }, "查看对应日记")
    : row.kind === "day" ? `<button class="source-link" data-date="${escapeHTML(row.target)}">查看这一天 ↗</button>` : "";
  return `<tr><td class="usage-time">${usageTime(row.started_at)}</td>
    <td><details class="usage-call-info" data-usage-id="${escapeHTML(row.id)}" ${open ? "open" : ""}><summary>${escapeHTML(usageKindNames[row.kind] || "其他请求")}</summary>
      <div class="usage-call-detail">${details.map((d) => `<p>${escapeHTML(d)}</p>`).join("")}${source}</div></details>
      ${row.attempt > 1 ? `<small>第 ${escapeHTML(row.attempt)} 次尝试</small>` : ""}</td>
    <td class="usage-model">${escapeHTML(row.model)}</td>
    <td class="usage-number">${tokenText(row.input_tokens)}</td><td class="usage-number">${tokenText(row.output_tokens)}</td>
    <td class="usage-number">${tokenText(row.total_tokens)}</td><td class="usage-number usage-cost">${moneyText(row.estimated_cost)}</td>
    <td><span class="usage-status ${escapeHTML(row.status)}">${escapeHTML(usageStatusNames[row.status] || "未知")}</span></td></tr>`;
}

function renderUsage() {
  const root = $("#usage-view");
  const openRows = new Set($$(".usage-call-info[open]", root).map((el) => el.dataset.usageId));
  const priceOpen = $("#usage-pricing")?.open;
  const data = state.usage;
  if (!data) {
    root.innerHTML = `<div class="view-header"><div class="eyebrow">A LITTLE CLARITY</div><h1 class="view-title">每一次整理，都看得清</h1><p class="view-description">正在读取本机的用量记录…</p></div>`;
    return;
  }
  const s = data.summary;
  const price = data.price || {};
  const all = data.month === "all";
  const tokenMetric = (value, missing) => s.requests && missing === s.requests ? "—" : tokenText(value);
  root.innerHTML = `<div class="view-header usage-header"><div><div class="eyebrow">A LITTLE CLARITY</div><h1 class="view-title">每一次整理，都看得清</h1><p class="view-description">记录模型的每次请求，让思考的成本也有迹可循。</p></div>
    <div class="usage-header-actions"><button id="usage-refresh" class="secondary-button">${icon("refresh")}刷新</button><button id="usage-export" class="secondary-button">${icon("download")}导出明细</button></div></div>
    <div class="usage-filters"><label for="usage-month">查看月份</label><input id="usage-month" type="month" value="${escapeHTML(all ? state.today.slice(0, 7) : data.month)}"><button id="usage-all" class="${all ? "active" : ""}">全部记录</button><span>${all ? "所有月份" : escapeHTML(data.month)} · ${tokenText(s.requests)} 次请求</span></div>
    <div class="usage-metrics"><div><span>输入 Token</span><strong>${tokenMetric(s.input_tokens, s.missing_input)}</strong><small>${s.missing_input ? `另有 ${s.missing_input} 次用量未知` : "包含提示词与发送的上下文"}</small></div>
      <div><span>输出 Token</span><strong>${tokenMetric(s.output_tokens, s.missing_output)}</strong><small>${s.missing_output ? `另有 ${s.missing_output} 次用量未知` : "包含服务商返回的思考用量"}</small></div>
      <div><span>总 Token</span><strong>${tokenMetric(s.total_tokens, s.missing_total)}</strong><small>${s.missing_total ? `已知部分 · ${s.missing_total} 次未知` : "输入与输出的合计"}</small></div>
      <div class="usage-fee-metric"><span>已知费用估算</span><strong>${s.requests && s.unknown_cost === s.requests ? "—" : moneyText(s.known_cost)}</strong><small>${s.unknown_cost ? `${s.unknown_cost} 次费用未知，未计入` : "按各次请求保存的单价计算"}</small></div></div>
    ${data.warning ? `<div class="notice error" role="status">${escapeHTML(data.warning)}</div>` : ""}
    <details id="usage-pricing" class="usage-pricing" ${priceOpen ? "open" : ""}><summary><span>计费参考 · ${escapeHTML(data.model || "尚未配置模型")}</span><span>${data.price ? `输入 ¥${escapeHTML(price.input_rate)} / 输出 ¥${escapeHTML(price.output_rate)} · 每百万 Token` : "设置单价后估算费用"}</span></summary>
      <form id="usage-price-form"><p>仅修改当前服务与模型的后续请求，历史记录保留当时的单价。单位：人民币元 / 百万 Token。</p><div class="usage-price-fields">
        <div class="form-field"><label for="usage-input-rate">输入单价</label><input id="usage-input-rate" name="input_rate" type="number" min="0" max="1000000" step="0.00000001" required value="${escapeHTML(price.input_rate ?? "")}"></div>
        <div class="form-field"><label for="usage-output-rate">输出单价</label><input id="usage-output-rate" name="output_rate" type="number" min="0" max="1000000" step="0.00000001" required value="${escapeHTML(price.output_rate ?? "")}"></div>
        <div class="form-field"><label for="usage-cached-rate">缓存输入单价 · 可留空</label><input id="usage-cached-rate" name="cached_rate" type="number" min="0" max="1000000" step="0.00000001" value="${escapeHTML(price.cached_rate ?? "")}"></div></div>
        <p>缓存单价不清楚时留空，缓存部分按标准输入价估算。${price.source === "user" ? "当前使用你填写的单价。" : data.price ? `预填价格核对于 ${escapeHTML(price.checked_at)}，参考<a href="https://help.aliyun.com/zh/model-studio/model-pricing" target="_blank" rel="noopener noreferrer">阿里云官方价格 ↗</a>。` : "当前服务与模型尚未设置单价。"}</p>
        <button class="primary-button" type="submit" ${data.model ? "" : "disabled"}>保存单价</button></form></details>
    <div class="usage-ledger-heading"><h2>请求明细</h2><p>每次模型调用单独记一行；一次整理可能包含多次调用，重试也会记录。</p></div>
    ${data.records.length ? `<div class="usage-table-wrap"><table class="usage-table"><thead><tr><th>时间</th><th>用途 · 展开详情</th><th>模型</th><th>输入</th><th>输出</th><th>总 Token</th><th>费用估算</th><th>请求状态</th></tr></thead><tbody>${data.records.map((row) => usageRecord(row, openRows.has(row.id))).join("")}</tbody></table></div>` : `<div class="usage-empty">${icon("leaf")}<h2>让每一份用量，都有记录</h2><p>这里会自动收录之后的整理与连接测试请求。暂时无需操作。</p></div>`}
    <div class="usage-pagination"><span>${data.records.length ? `${data.offset + 1}—${data.offset + data.records.length} / ${s.requests}` : "暂无记录"}</span><div><button id="usage-prev" class="secondary-button" ${data.offset ? "" : "disabled"}>上一页</button><button id="usage-next" class="secondary-button" ${data.offset + data.records.length < s.requests ? "" : "disabled"}>下一页</button></div></div>
    <p class="usage-footnote">从本版本开始记录，仅统计本应用的调用，升级前的历史用量无法追补。“—”表示未知，不代表零。估算未包含免费额度、活动折扣及未配置的缓存优惠，实际费用以服务商账单为准。账目有独立的每日快照，也可导出明细；日记 JSON 备份不包含账目，恢复日记不会重置已发生的消耗。</p>`;
  $("#usage-refresh").onclick = () => loadUsage();
  $("#usage-month").onchange = (event) => { if (!event.target.value) return; state.usageMonth = event.target.value; state.usageOffset = 0; loadUsage(); };
  $("#usage-all").onclick = () => { state.usageMonth = "all"; state.usageOffset = 0; loadUsage(); };
  $("#usage-prev").onclick = () => { state.usageOffset = Math.max(0, state.usageOffset - data.limit); loadUsage(); };
  $("#usage-next").onclick = () => { state.usageOffset += data.limit; loadUsage(); };
  $("#usage-export").onclick = async () => {
    try {
      const blob = await api(`/api/usage/export?month=${encodeURIComponent(data.month)}`, undefined, true);
      const url = URL.createObjectURL(blob), link = document.createElement("a");
      link.href = url; link.download = `拾光用量-${data.month}.csv`; link.click();
      setTimeout(() => URL.revokeObjectURL(url), 3000);
    } catch (error) { toast(error.message); }
  };
  $("#usage-price-form").onsubmit = async (event) => {
    event.preventDefault();
    const button = event.target.querySelector('button[type="submit"]');
    button.disabled = true;
    try {
      const form = new FormData(event.target);
      await api("/api/usage/price", { model: data.model, provider: data.provider,
        input_rate: form.get("input_rate"), output_rate: form.get("output_rate"), cached_rate: form.get("cached_rate") });
      await loadUsage(); toast("单价已保存，后续请求会使用新单价");
    } catch (error) { toast(error.message); }
    finally { button.disabled = false; }
  };
}
