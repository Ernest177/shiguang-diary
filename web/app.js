"use strict";

const $ = (selector, parent = document) => parent.querySelector(selector);
const $$ = (selector, parent = document) => [
  ...parent.querySelectorAll(selector),
];
const escapeHTML = (value) =>
  String(value ?? "").replace(
    /[&<>"']/g,
    (char) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        char
      ],
  );
const paths = {
  book: "M4 4h6a3 3 0 0 1 3 3v14a4 4 0 0 0-4-2H4z M13 7a3 3 0 0 1 3-3h4v15h-3a4 4 0 0 0-4 2",
  sparkles:
    "m12 3 2.4 6.6L21 12l-6.6 2.4L12 21l-2.4-6.6L3 12l6.6-2.4z M20 2v4 M18 4h4",
  check: "m5 12 4 4L19 6",
  settings:
    "m9 3 1-1h4l1 3 3 1 3 1v4l-3 2-1 3-2 3h-4l-2-3-3-1-2-2V9l3-2z M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8",
  "chevron-left": "m14 6-6 6 6 6",
  "chevron-right": "m9 6 6 6-6 6",
  search: "M16 16l5 5 M10.5 3a7.5 7.5 0 1 0 0 15 7.5 7.5 0 0 0 0-15",
  shield: "m12 3 8 3v6c0 5-8 9-8 9s-8-4-8-9V6z m-4 9 3 3 5-6",
  panel: "M3 4h18v16H3z M15 4v16",
  pen: "m4 16-1 5 5-1L20 8l-4-4z M14 6l4 4",
  calendar: "M4 5h16v16H4z M4 10h16 M8 3v4 M16 3v4",
  "arrow-up-right": "M6 18 18 6 M6 6h12v12",
  edit: "m4 16-1 5 5-1L20 8l-4-4z M14 6l4 4",
  trash: "M3 6h18 M9 6V3h6v3 M6 6l1 15h10l1-15 M10 10v7 M14 10v7",
  clock: "M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18 M12 7v5l3 2",
  leaf: "M20 3C10 3 3 7 5 15c3 6 13 4 15-12z M5 21l10-12",
  list: "M9 6h12 M9 12h12 M9 18h12 M3 6h1 M3 12h1 M3 18h1",
  download: "M12 3v12 m-5-5 5 5 5-5 M4 17v4h16v-4",
  archive: "M3 4h18v5H3z M5 9v12h14V9 M10 13h4",
  refresh:
    "M20 8a8 8 0 0 0-14-2L3 9 M3 3v6h6 M4 16a8 8 0 0 0 14 2l3-3 M21 21v-6h-6",
  lock: "M5 10h14v11H5z M8 10V6a4 4 0 0 1 8 0v4 M12 14v3",
  history: "M3 4v5h5 M3 9a9 9 0 1 1 0 7 M12 7v5l3 2",
};
const icon = (name, cls = "") =>
  `<svg class="${cls}" viewBox="0 0 24 24" aria-hidden="true"><path d="${paths[name] || paths.book}"></path></svg>`;
function fillIcons(root = document) {
  $$("[data-icon]", root).forEach((el) => {
    el.innerHTML = icon(el.dataset.icon);
  });
}
fillIcons();

const state = {
  view: "record",
  day: "",
  today: "",
  version: "—",
  month: new Date(),
  data: null,
  settings: { timezone: "Asia/Shanghai", ai_mode: "off" },
  token: "",
  query: "",
  actionTab: "task",
  edit: null,
  saving: false,
  serial: 0,
  panelCollapsed: false,
  about: null,
  aboutTab: "portrait",
  aboutMonth: "",
  aboutSerial: 0,
  usage: null,
  usageMonth: "",
  usageOffset: 0,
  usageSerial: 0,
};
let draftId = crypto.randomUUID(),
  toastTimer,
  undoCallback,
  searchTimer,
  importedBundle = null;
const modal = $("#modal");

function draftRead() {
  try {
    return JSON.parse(localStorage.getItem("shiguang-draft") || "null");
  } catch {
    return null;
  }
}
function draftWrite() {
  const payload = {
    id: draftId,
    body: $("#entry-body").value,
    day: $("#entry-day").value,
    ai: $("#entry-ai").checked,
    edit: state.edit
      ? { id: state.edit.id, revision: state.edit.revision }
      : null,
  };
  try {
    localStorage.setItem("shiguang-draft", JSON.stringify(payload));
    $("#draft-status").textContent = payload.body ? "草稿已暂存" : "只管写下来";
  } catch {
    $("#draft-status").textContent = "草稿暂存不可用，请及时保存";
  }
}
function restoreDraft() {
  const draft = draftRead();
  if (draft?.body) {
    $("#entry-body").value = draft.body;
    $("#entry-day").value = draft.day;
    $("#entry-ai").checked = draft.ai !== false;
    draftId = draft.id || crypto.randomUUID();
    state.edit = draft.edit || null;
    if (state.edit) {
      $("#composer-heading").textContent = "继续修改这个片段";
      $("#save-entry").innerHTML = `保存修改${icon("check")}`;
      $("#cancel-edit").hidden = false;
    }
    $("#draft-status").textContent = "已恢复未保存草稿";
  }
}
restoreDraft();

async function api(path, body, binary = false) {
  let response;
  try {
    response = await fetch(path, {
      method: body === undefined ? "GET" : "POST",
      cache: "no-store",
      headers: {
        "X-Notes-Token": state.token,
        ...(body === undefined ? {} : { "Content-Type": "application/json" }),
      },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    });
    $("#connection-banner").hidden = true;
  } catch {
    $("#connection-banner").hidden = false;
    throw new Error("暂时无法连接本地服务，输入内容已保留。请重新启动拾光。");
  }
  if (!response.ok) {
    let error;
    try {
      error = await response.json();
    } catch {
      error = {};
    }
    throw new Error(error.error || `操作未完成（${response.status}）`);
  }
  return binary ? response.blob() : response.json();
}

function toast(message, undo) {
  clearTimeout(toastTimer);
  $("#toast-message").textContent = message;
  $("#toast").hidden = false;
  $("#toast-undo").hidden = !undo;
  undoCallback = undo;
  toastTimer = setTimeout(
    () => {
      $("#toast").hidden = true;
    },
    undo ? 14000 : 6000,
  );
}
$("#toast-close").onclick = () => {
  $("#toast").hidden = true;
};
$("#toast-undo").onclick = async () => {
  try {
    if (undoCallback) await undoCallback();
    $("#toast").hidden = true;
  } catch (error) {
    toast(error.message);
  }
};

function dateAt(day) {
  return new Date(`${day}T12:00:00+08:00`);
}
function displayDate(day, options) {
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai",
    ...options,
  }).format(dateAt(day));
}
function timeAt(stamp, full = false) {
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: state.settings.timezone,
    hour: "2-digit",
    minute: "2-digit",
    ...(full ? { month: "2-digit", day: "2-digit" } : {}),
  }).format(new Date(stamp));
}
function statusText(entry) {
  if (!entry.ai_enabled) return "仅保留原文";
  if (entry.analysis) return "已整理";
  if (state.settings.ai_mode === "off" || entry.job_status === "waiting")
    return "AI 尚未启用";
  return (
    {
      queued: entry.analysis_error ? "等待重试" : "等待整理",
      running: "正在整理",
      failed: "整理未完成",
      done: "等待更新",
    }[entry.job_status] || "等待整理"
  );
}
function sourceButton(source, label = "查看原文") {
  return `<button class="source-link" data-source="${escapeHTML(source.entry_id)}" data-day="${escapeHTML(source.journal_day || state.day)}" title="${escapeHTML(source.quote)}">${escapeHTML(label)} ↗</button>`;
}

async function loadState({ silent = false } = {}) {
  if (!state.day) return;
  const ticket = ++state.serial;
  try {
    const data = await api(
      `/api/state?day=${encodeURIComponent(state.day)}&q=${encodeURIComponent(state.query)}`,
    );
    if (ticket !== state.serial) return;
    state.data = data;
    state.today = data.today;
    state.settings = data.settings;
    if (state.view === "about") await loadAbout();
    render();
  } catch (error) {
    if (!silent) toast(error.message);
  }
}

function renderCalendar() {
  const year = state.month.getFullYear(),
    month = state.month.getMonth();
  $("#calendar-month").textContent = `${year} 年 ${month + 1} 月`;
  const first = (new Date(year, month, 1).getDay() + 6) % 7,
    count = new Date(year, month + 1, 0).getDate();
  const days = new Set(state.data?.calendar.map((item) => item.day));
  $("#calendar-grid").innerHTML =
    '<span class="calendar-blank"></span>'.repeat(first) +
    Array.from({ length: count }, (_, i) => {
      const day = `${year}-${String(month + 1).padStart(2, "0")}-${String(i + 1).padStart(2, "0")}`;
      const cls = [
        day === state.day ? "selected" : "",
        day === state.today ? "today" : "",
        days.has(day) ? "has-entry" : "",
      ].join(" ");
      return `<button class="${cls}" data-date="${day}" aria-label="${year}年${month + 1}月${i + 1}日${days.has(day) ? "，有记录" : ""}" ${day === state.day ? 'aria-current="date"' : ""}>${i + 1}</button>`;
    }).join("");
}

function empty(title, description, name = "book") {
  return `<div class="empty-state"><div class="empty-illustration">${icon(name)}</div><h3>${title}</h3><p>${description}</p></div>`;
}

function renderEntries() {
  const records = state.data.entries;
  $("#entry-count").textContent = records.length;
  $("#timeline-heading").firstChild.textContent = state.query
    ? "找到的片段 "
    : state.day === state.today
      ? "今天的片段 "
      : "这一天的片段 ";
  if (!records.length) {
    $("#entries").innerHTML = state.query
      ? empty(
          "暂时没有找到这个片段",
          "试试更短的关键词，或换一种说法。",
          "search",
        )
      : empty(
          "这一天，等你落笔",
          "不必完整，也不必特别。<br>生活里的小片段，都可以放在这里。",
          "leaf",
        );
    return;
  }
  $("#entries").innerHTML = records
    .map(
      (
        entry,
      ) => `<article id="entry-${escapeHTML(entry.id)}" class="entry-card">
    <div class="entry-top"><span class="entry-time">${escapeHTML(timeAt(entry.created_at, state.query.length > 0))}</span>${entry.revision > 1 ? '<span class="entry-tag">已编辑</span>' : ""}${state.query ? `<span class="entry-tag">${escapeHTML(entry.journal_day)}</span>` : ""}<div class="entry-menu"><button class="icon-button" data-history="${escapeHTML(entry.id)}" aria-label="查看记录历史">${icon("history")}</button><button class="icon-button" data-edit="${escapeHTML(entry.id)}" aria-label="编辑记录">${icon("edit")}</button><button class="icon-button" data-delete="${escapeHTML(entry.id)}" aria-label="删除记录">${icon("trash")}</button></div></div>
    <p class="entry-body">${escapeHTML(entry.body)}</p>
    ${entry.analysis ? `<details class="analysis-details"><summary>AI 整理 · ${escapeHTML(entry.analysis.summary)}</summary>${entry.analysis.insights.map((item) => `<div class="insight-line">${item.type === "idea" ? "想法 · " : ""}${escapeHTML(item.text)}<div class="evidence">“${escapeHTML(item.quote)}”</div></div>`).join("")}${entry.analysis.actions.map((a) => `<div class="insight-line">${{ task: "待办", opportunity: "值得做", question: "待澄清" }[a.kind]} · ${escapeHTML(a.title)}<div class="evidence">“${escapeHTML(a.quote)}”</div></div>`).join("")}</details>` : ""}
    <div class="entry-bottom"><span class="status-label ${entry.analysis_error ? "error" : ""}" title="${escapeHTML(entry.analysis_error || "")}">${icon(entry.analysis ? "sparkles" : "lock")}${escapeHTML(statusText(entry))}</span><span>·</span><span>${entry.body.length.toLocaleString()} 字</span>${entry.ai_enabled ? `<button class="retry-button" data-memory-permission="${escapeHTML(entry.id)}">${entry.memory_enabled ? "已纳入成长记录 · 排除" : "纳入成长记录"}</button>` : ""}${entry.ai_enabled && state.settings.ai_mode !== "off" ? `<button class="retry-button" data-retry="${escapeHTML(entry.id)}">${entry.analysis ? "重新整理" : "重试整理"}</button>` : ""}</div>
    ${entry.analysis_error && state.settings.ai_mode !== "off" ? `<div class="notice error">${escapeHTML(entry.analysis_error)}</div>` : ""}
  </article>`,
    )
    .join("");
}

const sectionNames = {
  events: "今天发生的事",
  insights: "值得留住的发现",
  decisions: "做出的决定",
  actions: "行动与进展",
  opportunities: "值得做的事",
};
function summaryHTML(summary, scope = "panel") {
  const data = summary.data;
  if (!data) return "";
  const previous = $(`[data-summary-scope="${scope}"]`);
  const expanded = new Set(previous?.dataset.summaryDay === state.day
    ? $$("details[open]", previous).map((el) => el.dataset.summaryDisclosure) : []);
  const sources = new Map();
  for (const section of data.sections) for (const item of section.items) for (const source of item.sources) {
    if (!sources.has(source.entry_id)) sources.set(source.entry_id, { ...source, quotes: new Set() });
    sources.get(source.entry_id).quotes.add(source.quote);
  }
  const itemsHTML = (items) => `<ul>${items.map((item) => `<li>${escapeHTML(item.text)}</li>`).join("")}</ul>`;
  const sections = data.sections.map((section) => {
    const compact = section.category === "insights" && section.items.length > 2;
    return `<section class="summary-section"><h4>${sectionNames[section.category]}</h4>${itemsHTML(compact ? section.items.slice(0, 2) : section.items)}${compact ? `<details class="summary-more" data-summary-disclosure="insights-more" ${expanded.has("insights-more") ? "open" : ""}><summary>其余 ${section.items.length - 2} 条</summary>${itemsHTML(section.items.slice(2))}</details>` : ""}</section>`;
  }).join("");
  const evidence = sources.size ? `<details class="summary-evidence" data-summary-disclosure="sources" ${expanded.has("sources") ? "open" : ""}><summary>查看原文依据</summary>${[...sources.values()].map((source, i) => `<div class="summary-source">${sourceButton(source, sources.size === 1 ? "打开这条日记" : `打开日记 ${i + 1}`)}${[...source.quotes].map((quote) => `<blockquote>${escapeHTML(quote)}</blockquote>`).join("")}</div>`).join("")}</details>` : "";
  return `<div class="summary-content" data-summary-scope="${scope}" data-summary-day="${escapeHTML(state.day)}">${summary.stale ? '<div class="notice">记录发生了变化，以下是上次整理，正在等待更新。</div>' : ""}<p>${escapeHTML(data.overview)}</p>${sections}${evidence}<div class="summary-updated">${escapeHTML(timeAt(summary.updated_at, true))} 更新 · ${escapeHTML(summary.model)}</div></div>`;
}
function aiOffHTML() {
  return `<div class="summary-off"><div class="sparkle-orbit">${icon("sparkles")}</div><h3>让思绪，慢慢清晰</h3><p>连接你选择的 AI，帮你整理日记里的发现、待办和灵感。原文始终为你保留。</p><button class="secondary-button panel-button" data-view="settings">设置智能整理 ${icon("arrow-up-right")}</button><div class="panel-meta">${icon("shield")}由你决定使用本地或云端服务</div></div>`;
}
function summaryPending(summary) {
  if (state.settings.ai_mode === "off") return aiOffHTML();
  if (summary.status === "failed")
    return `<div class="notice error">${escapeHTML(summary.error || "整理未完成，原文已保存。")}</div><button class="secondary-button" data-retry-day="true">重新整理 ${icon("refresh")}</button>`;
  if (summary.status === "empty")
    return '<p class="panel-description">留下一些片段之后，这里会慢慢长出一天的轮廓。</p>';
  return `<div class="summary-off"><div class="sparkle-orbit">${icon("sparkles")}</div><h3>${summary.status === "running" ? "正在整理这一天" : "等待整理"}</h3><p>${escapeHTML(summary.error || (summary.status === "running" ? "正在后台整理，你可以继续记录。" : "原文已保存。连续记录会合并整理，通常在最后一条分析完成约30秒后更新。"))}</p><button class="text-button" data-retry-day="true">立即整理 ↗</button></div>`;
}
function renderPanel() {
  const data = state.data,
    summary = data.summary,
    dayRecords = state.query ? [] : data.entries;
  const active = data.actions.filter(
    (a) =>
      a.kind === "task" &&
      a.status === "open" &&
      a.sources.some((s) => s.journal_day === state.day),
  );
  const opportunities = data.actions.filter(
    (a) =>
      a.kind === "opportunity" &&
      a.status === "open" &&
      a.sources.some((s) => s.journal_day === state.day),
  );
  const dayCount =
    data.calendar.find((item) => item.day === state.day)?.count || 0;
  $("#insights-panel").innerHTML =
    `<div class="panel-heading"><span>${icon("sparkles")}这一天的整理</span><small>DAILY NOTES</small></div><p class="panel-description">从零散片段里，拾起值得留下的东西。</p>${summary.data ? summaryHTML(summary) : summaryPending(summary)}<div class="panel-divider"></div><div class="panel-label"><span>一天的足迹</span><span>${state.day.slice(5).replace("-", " / ")}</span></div><div class="panel-stat-row"><div class="panel-stat"><strong>${dayCount}</strong><span>记录片段</span></div><div class="panel-stat"><strong>${active.length}</strong><span>明确待办</span></div><div class="panel-stat"><strong>${opportunities.length}</strong><span>值得做</span></div></div>${dayRecords.length && dayRecords.every((e) => !e.ai_enabled) ? '<div class="notice">这一天的记录已排除 AI 分析。</div>' : ""}<div class="gentle-note"><div class="quote-mark">“</div><p>记录不必为了成为更好的谁，<br>也可以只是，<br>多看见自己一点。</p><small>WRITE A LITTLE, KEEP A LITTLE.</small></div>`;
}

function renderReview() {
  const count =
      state.data.calendar.find((item) => item.day === state.day)?.count || 0,
    summary = state.data.summary;
  const actions = state.data.actions.filter((a) =>
    a.sources.some((s) => s.journal_day === state.day),
  );
  $("#review-view").innerHTML =
    `<div class="view-header"><div class="eyebrow">A DAY, REVISITED</div><h1 class="view-title">${escapeHTML(displayDate(state.day, { month: "long", day: "numeric" }))}，回头看看</h1><p class="view-description">重要的经历、想法和决定，在这里连成一天的轮廓。</p></div><div class="review-stats"><div><strong>${count}</strong><span>留下的片段</span></div><div><strong>${actions.filter((a) => a.kind === "task" && a.status === "open").length}</strong><span>待跟进的行动</span></div><div><strong>${actions.filter((a) => a.kind === "opportunity" && a.status === "open").length}</strong><span>值得探索的事</span></div></div>${summary.data ? `<div class="review-box">${summaryHTML(summary, "review")}</div><div class="setting-actions"><button class="text-button" data-retry-day="true">重新整理这一天 ↗</button></div>` : count ? summaryPending(summary) : empty("还没有这一天的回顾", "先写下一个片段，日子的轮廓会逐渐清晰。", "leaf")}`;
}

function actionDeadline(a) {
  if (!a.due_date) {
    return `<span class="action-due ${a.due_text ? "uncertain" : "unset"}">${icon("calendar")}<span>${a.due_text ? "时间待确认" : "未设截止日期"}</span></span>`;
  }
  const days = Math.round(
    (Date.parse(`${a.due_date}T00:00:00Z`) - Date.parse(`${state.today}T00:00:00Z`)) / 86400000,
  );
  const active = a.status === "open";
  const status = active
    ? days < 0 ? `逾期 ${-days} 天` : ({ 0: "今天", 1: "明天", 2: "后天" })[days] || ""
    : "";
  const tone = active && days < 0 ? "overdue" : active && days <= 2 ? "soon" : "";
  return `<span class="action-due ${tone}">${icon("calendar")}<span class="action-due-label">截止</span><time datetime="${escapeHTML(a.due_date)}">${escapeHTML(a.due_date)}</time>${status ? `<span class="action-due-status">${status}</span>` : ""}</span>`;
}

function actionCard(a, expanded = false) {
  const sources = a.sources;
  const evidence = a.due_text && a.due_text !== a.due_date
    ? `<p class="action-time-quote">时间原话：${escapeHTML(a.due_text)}</p>` : "";
  return `<article class="action-card">
    ${a.kind === "task" ? `<button class="action-check ${a.status === "done" ? "done" : ""}" data-action-status="${escapeHTML(a.id)}" data-status="${a.status === "done" ? "open" : "done"}" aria-label="${a.status === "done" ? "重新打开" : "完成"}行动">${a.status === "done" ? icon("check") : ""}</button>` : `<span class="action-edit">${icon(a.kind === "opportunity" ? "sparkles" : "list")}</span>`}
    <div class="action-main">
      <div class="action-heading"><div class="action-title ${a.status === "done" ? "done" : ""}">${escapeHTML(a.title)}</div>${actionDeadline(a)}</div>
      ${sources.length ? `<details class="source-details action-sources" data-action-sources="${escapeHTML(a.id)}" ${expanded ? "open" : ""}><summary>原文依据<span class="action-source-count">${sources.length} 条</span></summary><div class="action-source-list">${evidence}${sources.map((s) => `<div class="action-source"><div class="action-source-heading"><span>记录于 ${escapeHTML(s.journal_day)}</span>${sourceButton(s, "打开这条日记")}</div><blockquote>${escapeHTML(s.quote)}</blockquote></div>`).join("")}</div></details>` : `<p class="orphan-note action-orphan">原始记录已修改或删除，保留你的手动选择</p>`}
      <div class="action-tools"><button data-action-edit="${escapeHTML(a.id)}">编辑</button>${a.kind !== "task" && a.status === "open" ? `<button data-promote="${escapeHTML(a.id)}">转为待办</button>` : ""}${a.status === "open" ? `<button data-action-status="${escapeHTML(a.id)}" data-status="${a.kind === "task" ? "cancelled" : "dismissed"}">${a.kind === "task" ? "取消行动" : "暂不采纳"}</button>` : a.status !== "done" ? `<button data-action-status="${escapeHTML(a.id)}" data-status="open">重新打开</button>` : ""}</div>
    </div></article>`;
}

function renderActions() {
  const expanded = new Set($$(".action-sources[open]", $("#actions-view")).map((el) => el.dataset.actionSources));
  const actions = state.data.actions;
  const tabs = [
    ["task", "明确待办"],
    ["opportunity", "值得做"],
    ["question", "待澄清"],
    ["done", "已完成"],
    ["dismissed", "已取消"],
  ];
  const match = (a, tab) =>
    ["done", "dismissed"].includes(tab)
      ? tab === "done"
        ? a.status === "done"
        : ["cancelled", "dismissed"].includes(a.status)
      : a.kind === tab && a.status === "open";
  const items = actions.filter((a) => match(a, state.actionTab));
  $("#actions-view").innerHTML =
    `<div class="view-header"><div class="eyebrow">THOUGHTS INTO MOTION</div><h1 class="view-title">让想法，有处落脚</h1><p class="view-description">明确的承诺变成待办，尚未决定的灵感留在「值得做」。</p></div><div class="tabbar" role="tablist" aria-label="行动类别">${tabs.map(([id, title]) => `<button role="tab" aria-selected="${state.actionTab === id}" data-tab="${id}" class="${state.actionTab === id ? "active" : ""}">${title}<small>${actions.filter((a) => match(a, id)).length}</small></button>`).join("")}</div>${items.length ? items.map((a) => actionCard(a, expanded.has(a.id))).join("") : empty("给行动留一点空间", state.settings.ai_mode === "off" ? "启用智能整理后，这里会收纳日记中的待办与灵感。" : "记录明确计划时，AI 会帮你提取行动并保留原文依据。", "list")}`;
}

function renderSettings() {
  const s = state.settings;
  $("#settings-view").innerHTML =
    `<div class="view-header"><div class="eyebrow">MAKE ROOM FOR YOURSELF</div><h1 class="view-title">按你的方式记录</h1><p class="view-description">日记留在这台电脑。智能整理的方式，由你选择。</p></div><form id="settings-form" class="settings-form"><section class="settings-section"><h2>智能整理</h2><p>支持 Chat Completions 兼容接口。本地模式只连接本机；云端模式会发送你允许分析的内容。</p><div class="form-grid"><div class="form-field"><label for="ai-mode">整理方式</label><select id="ai-mode" name="ai_mode"><option value="off" ${s.ai_mode === "off" ? "selected" : ""}>暂不启用</option><option value="local" ${s.ai_mode === "local" ? "selected" : ""}>本地模型 · 当前电脑</option><option value="cloud" ${s.ai_mode === "cloud" ? "selected" : ""}>云端服务 · 自选提供方</option></select></div><div class="form-field"><label for="ai-model">模型名称</label><input id="ai-model" name="ai_model" value="${escapeHTML(s.ai_model)}" placeholder="填写服务中已可用的模型名称" maxlength="200"></div></div><div class="form-field"><label for="ai-url">服务地址</label><input id="ai-url" name="ai_base_url" value="${escapeHTML(s.ai_base_url)}" placeholder="例如 http://127.0.0.1:11434/v1" maxlength="2000"><small>填写包含 /v1 等接口前缀的基础地址，程序会追加 /chat/completions。也支持填写完整的此接口地址。</small></div><div class="form-field"><label for="ai-key">API 密钥 ${s.key_present ? "· 已保存" : ""}</label><input id="ai-key" type="password" name="api_key" autocomplete="new-password" placeholder="${s.key_present ? "已安全保存；留空保留原密钥" : "本地服务通常可以留空"}" maxlength="4000"><small>Windows 上使用当前用户的系统保护存储，密钥不会进入日记或备份。</small></div><div id="mode-hint" class="mode-hint"></div><label id="cloud-consent-wrap" class="consent-check"><input id="cloud-consent" type="checkbox" name="cloud_consent" ${s.cloud_consent ? "checked" : ""}><span>我允许把已勾选「允许 AI 整理」的日记及整理所需的相关行动，发送到上方云端服务。可以随时关闭。</span></label><label class="consent-check"><input type="checkbox" name="clear_key"><span>删除已保存的 API 密钥</span></label><div class="setting-actions"><button type="button" id="test-ai" class="secondary-button">测试连接 ${icon("arrow-up-right")}</button><span id="ai-test-result" class="settings-feedback" role="status"></span></div></section><section class="settings-section"><h2>记录偏好</h2><div class="form-field"><label for="timezone">显示时区</label><select name="timezone" id="timezone">${[
      ["Asia/Shanghai", "上海 · UTC+8"],
      ["Asia/Singapore", "新加坡 · UTC+8"],
      ["Asia/Tokyo", "东京 · UTC+9"],
      ["UTC", "协调世界时 · UTC"],
    ]
      .map(
        ([value, label]) =>
          `<option value="${value}" ${s.timezone === value ? "selected" : ""}>${label}</option>`,
      )
      .join(
        "",
      )}</select><small>更改显示时区会调整时间的呈现，历史记录的所属日期保持原样。</small></div></section><section class="settings-section"><h2>数据与备份</h2><p>正式记录独立保存在本机数据库中。浏览器缓存可以清理，数据目录需要妥善保管。</p><div class="path-info"><strong>日记目录</strong><br>${escapeHTML(s.data_dir)}<br><strong>当前备份目录</strong><br>${escapeHTML(s.effective_backup_dir)}<br><strong>最近备份</strong> · ${s.last_backup_at ? escapeHTML(s.last_backup_at.split(" ")[0]) : "尚未创建"}</div>${s.backup_error ? `<div class="notice error">${escapeHTML(s.backup_error)}</div>` : ""}<div class="form-grid"><div class="form-field"><label for="backup-dir">备份目录</label><input id="backup-dir" name="backup_dir" value="${escapeHTML(s.backup_dir)}" placeholder="留空使用默认目录"><small>可以填写其他磁盘的完整路径，帮助应对本机磁盘故障。</small></div><div class="form-field"><label for="backup-keep">保留最近的备份数量</label><input type="number" id="backup-keep" name="backup_keep" min="2" max="100" value="${s.backup_keep}"></div></div><div class="setting-actions"><button type="button" id="backup-now" class="secondary-button">${icon("archive")}立即备份</button><button type="button" data-export="json" class="secondary-button">${icon("download")}导出完整备份</button><button type="button" data-export="markdown" class="secondary-button">导出 Markdown</button><button type="button" id="import-open" class="text-button">导入备份 ↗</button><input id="import-file" type="file" accept=".json,application/json" hidden></div><div id="backup-list"></div><p class="privacy-footnote">每天发生变更后会自动创建一份数据库快照。同一磁盘上的副本无法应对磁盘损坏，建议定期导出到其他磁盘。本版日记数据库未额外加密，请同时保护好 Windows 账户与磁盘。</p></section><div class="settings-bottom"><button type="submit" class="primary-button">保存设置 ${icon("check")}</button><small>拾光 v${escapeHTML(state.version)} · 本机运行，无遥测</small></div></form><section class="settings-section"><button id="shutdown" class="text-button">退出拾光本地服务</button><p class="privacy-footnote">关闭网页后，本地服务仍可整理已保存的日记。退出服务后，双击「启动拾光」即可重新打开。</p></section>`;
  $("#settings-form").onsubmit = saveSettings;
  $("#ai-mode").onchange = updateModeHint;
  $("#test-ai").onclick = testAI;
  $("#backup-now").onclick = backupNow;
  $("#import-open").onclick = () => $("#import-file").click();
  $("#import-file").onchange = previewImport;
  $("#shutdown").onclick = shutdown;
  updateModeHint();
  loadBackups();
}
function updateModeHint() {
  const mode = $("#ai-mode").value;
  $("#cloud-consent-wrap").hidden = mode !== "cloud";
  $("#mode-hint").textContent =
    {
      off: "关闭后不启动新的 AI 调用，已保存的整理结果仍然可以查看。",
      local:
        "需要你自行安装并运行本地模型服务。请确认所选模型在本机推理；本地代理或模型服务也可能转发到外部。",
      cloud:
        "云端模式仅接受 HTTPS 地址。请确认你信任服务提供方，并了解其内容保存政策。",
    }[mode] + " 更换服务地址会清除旧密钥，如需使用密钥请重新填写。";
}
function settingsPayload() {
  const form = $("#settings-form");
  const payload = Object.fromEntries(new FormData(form));
  payload.cloud_consent = $("#cloud-consent").checked;
  payload.clear_key = form.elements.clear_key.checked;
  payload.backup_keep = Number(payload.backup_keep);
  if (!payload.api_key) delete payload.api_key;
  return payload;
}
async function saveSettings(event) {
  event.preventDefault();
  const button = $('button[type="submit"]', event.target);
  button.disabled = true;
  try {
    const result = await api("/api/settings", settingsPayload());
    state.settings = result.settings;
    toast("设置已保存");
    renderSettings();
    await loadState();
  } catch (error) {
    toast(error.message);
  } finally {
    button.disabled = false;
  }
}
async function testAI() {
  const button = $("#test-ai"),
    result = $("#ai-test-result");
  button.disabled = true;
  result.textContent = "正在连接，不会发送日记内容…";
  result.classList.remove("error");
  try {
    await api("/api/ai/test", settingsPayload());
    result.textContent = "连接成功，模型支持结构化回复";
  } catch (error) {
    result.textContent = error.message;
    result.classList.add("error");
  } finally {
    button.disabled = false;
  }
}
async function backupNow() {
  const button = $("#backup-now");
  button.disabled = true;
  try {
    await api("/api/backup", {});
    toast("备份已创建，并通过完整性检查");
    await loadBackups();
    await loadState();
  } catch (error) {
    toast(error.message);
  } finally {
    button.disabled = false;
  }
}
async function loadBackups() {
  try {
    const data = await api("/api/backups");
    if (!$("#backup-list")) return;
    $("#backup-list").innerHTML = data.backups
      .slice(0, 8)
      .map(
        (b) =>
          `<div class="backup-row"><span>${escapeHTML(b.name)}<small>${(b.size / 1024).toFixed(0)} KB</small></span><button type="button" class="text-button" data-restore="${escapeHTML(b.name)}">恢复</button></div>`,
      )
      .join("");
  } catch (error) {
    toast(error.message);
  }
}

function render() {
  if (!state.data) return;
  renderCalendar();
  $("#action-count").textContent = state.data.actions.filter(
    (a) => a.kind === "task" && a.status === "open",
  ).length;
  $("#day-title").textContent = displayDate(state.day, {
    month: "long",
    day: "numeric",
  });
  $("#day-eyebrow").textContent =
    state.day === state.today ? "TODAY, A NEW PAGE" : "A PAGE FROM YOUR DAYS";
  $("#day-subtitle").textContent =
    displayDate(state.day, { year: "numeric", weekday: "long" }) +
    " · 让零散的日常，慢慢有迹可循。";
  const date = dateAt(state.day);
  $("#emblem-month").textContent = date
    .toLocaleDateString("en-US", { month: "short", timeZone: "Asia/Shanghai" })
    .toUpperCase();
  $("#emblem-day").textContent = String(date.getDate()).padStart(2, "0");
  $("#emblem-weekday").textContent = date
    .toLocaleDateString("en-US", { weekday: "long", timeZone: "Asia/Shanghai" })
    .toUpperCase();
  if (!$("#entry-day").value) $("#entry-day").value = state.day;
  renderEntries();
  renderPanel();
  renderReview();
  renderActions();
  applyView();
}
function applyView() {
  ["record", "review", "actions", "settings", "about", "usage"].forEach((view) => {
    $(`#${view}-view`).hidden = view !== state.view;
  });
  $$("[data-view]").forEach((button) =>
    button.classList.toggle("active", button.dataset.view === state.view),
  );
  $("#topbar-context").textContent = {
    record: "日常记录",
    review: "每日回顾",
    actions: "行动清单",
    settings: "偏好与设置",
    about: "关于我",
    usage: "用量与费用",
  }[state.view];
  $("#insights-panel").hidden = state.view !== "record";
  $("#toggle-insights").hidden = state.view !== "record";
}
function setView(view) {
  if (!["record", "review", "actions", "settings", "about", "usage"].includes(view))
    return;
  state.view = view;
  history.replaceState(null, "", `#${view}`);
  if (view === "settings") renderSettings();
  if (view === "about") {
    renderAbout();
    loadAbout().catch((error) => toast(error.message));
  }
  if (view === "usage") {
    renderUsage();
    loadUsage().catch((error) => toast(error.message));
  }
  applyView();
}
async function selectDay(day) {
  state.day = day;
  state.month = dateAt(day);
  state.query = "";
  $("#search").value = "";
  if (!$("#entry-body").value && !state.edit) $("#entry-day").value = day;
  if (["settings", "actions", "about", "usage"].includes(state.view)) setView("record");
  await loadState();
}

async function saveEntry() {
  if (state.saving) return;
  const body = $("#entry-body").value;
  if (!body.trim()) {
    toast("先写下一个小片段吧");
    $("#entry-body").focus();
    return;
  }
  const payload = {
    id: draftId,
    body,
    journal_day: $("#entry-day").value,
    ai_enabled: $("#entry-ai").checked,
    ...(state.edit
      ? { entry_id: state.edit.id, revision: state.edit.revision }
      : {}),
  };
  state.saving = true;
  $("#save-entry").disabled = true;
  $("#draft-status").textContent = "正在存入本机…";
  try {
    const result = await api("/api/entries/save", payload);
    state.day = result.entry.journal_day;
    state.month = dateAt(state.day);
    state.query = "";
    $("#search").value = "";
    if ($("#entry-body").value === body) {
      $("#entry-body").value = "";
      state.edit = null;
      draftId = crypto.randomUUID();
      $("#entry-day").value = state.day;
      resetComposer();
      draftWrite();
    } else {
      state.edit = result.entry;
      $("#composer-heading").textContent = "继续修改这个片段";
      $("#save-entry").innerHTML = `保存修改${icon("check")}`;
      $("#cancel-edit").hidden = false;
      draftWrite();
    }
    toast(result.warning || "这一刻，已经存好了");
    await loadState();
    $("#entry-body").focus();
  } catch (error) {
    draftWrite();
    toast(error.message);
  } finally {
    state.saving = false;
    $("#save-entry").disabled = false;
  }
}
function resetComposer() {
  $("#composer-heading").textContent = "这一刻，你想记下什么？";
  $("#save-entry").innerHTML = `存下这一刻${icon("arrow-up-right")}`;
  $("#cancel-edit").hidden = true;
}
async function editEntry(id) {
  const entry = state.data.entries.find((e) => e.id === id);
  if (!entry) return;
  if (
    $("#entry-body").value.trim() &&
    !(await ask(
      "切换到编辑记录？",
      "输入框中还有未保存的内容。继续会替换当前草稿。",
      "继续编辑",
    ))
  )
    return;
  state.edit = entry;
  $("#entry-body").value = entry.body;
  $("#entry-day").value = entry.journal_day;
  $("#entry-ai").checked = !!entry.ai_enabled;
  $("#composer-heading").textContent = "修改这一个片段";
  $("#cancel-edit").hidden = false;
  $("#save-entry").innerHTML = `保存修改${icon("check")}`;
  draftWrite();
  setView("record");
  $("#composer").scrollIntoView({ behavior: "smooth", block: "center" });
  $("#entry-body").focus();
}
$("#cancel-edit").onclick = () => {
  state.edit = null;
  $("#entry-body").value = "";
  draftId = crypto.randomUUID();
  resetComposer();
  draftWrite();
};
$("#save-entry").onclick = saveEntry;
$("#entry-body").addEventListener("input", draftWrite);
$("#entry-day").addEventListener("change", draftWrite);
$("#entry-ai").addEventListener("change", draftWrite);
document.addEventListener("keydown", (event) => {
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
    event.preventDefault();
    $("#search").focus();
  }
  if (
    (event.ctrlKey || event.metaKey) &&
    event.key === "Enter" &&
    !event.isComposing &&
    event.keyCode !== 229 &&
    document.activeElement === $("#entry-body")
  ) {
    event.preventDefault();
    saveEntry();
  }
});
$("#search").addEventListener("input", () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => {
    state.query = $("#search").value.trim();
    setView("record");
    loadState();
  }, 220);
});
$("#go-today").onclick = () => selectDay(state.today);
$("#month-prev").onclick = () => {
  state.month = new Date(
    state.month.getFullYear(),
    state.month.getMonth() - 1,
    1,
  );
  renderCalendar();
};
$("#month-next").onclick = () => {
  state.month = new Date(
    state.month.getFullYear(),
    state.month.getMonth() + 1,
    1,
  );
  renderCalendar();
};
$("#toggle-insights").onclick = () => {
  if (matchMedia("(max-width:1050px)").matches)
    $("#content-layout").classList.toggle("show-insights");
  else {
    $("#content-layout").classList.toggle("panel-collapsed");
  }
};

function showModal(content) {
  $("#modal-content").innerHTML = content;
  if (!modal.open) modal.showModal();
}
function ask(title, description, confirm = "确认") {
  return new Promise((resolve) => {
    showModal(
      `<h2>${escapeHTML(title)}</h2><p>${escapeHTML(description)}</p><div class="modal-actions"><button id="ask-cancel" class="secondary-button">取消</button><button id="ask-confirm" class="primary-button">${escapeHTML(confirm)}</button></div>`,
    );
    const finish = (value) => {
      modal.close();
      resolve(value);
    };
    $("#ask-cancel").onclick = () => finish(false);
    $("#ask-confirm").onclick = () => finish(true);
    modal.addEventListener("cancel", () => resolve(false), { once: true });
  });
}

async function changeAction(id, patch) {
  const result = await api("/api/actions/update", { id, ...patch });
  await loadState();
  toast("行动已更新", async () => {
    await api("/api/actions/undo", { history_id: result.history_id });
    await loadState();
  });
}
function editAction(id) {
  const a = state.data.actions.find((a) => a.id === id);
  if (!a) return;
  showModal(
    `<h2>调整这项行动</h2><form id="action-edit-form"><div class="form-field"><label for="action-title">行动内容</label><input id="action-title" name="title" value="${escapeHTML(a.title)}" maxlength="300" required></div><div class="form-grid"><div class="form-field"><label for="action-kind">类别</label><select id="action-kind" name="kind">${[
      ["task", "明确待办"],
      ["opportunity", "值得做"],
      ["question", "待澄清"],
    ]
      .map(
        ([id, t]) =>
          `<option value="${id}" ${a.kind === id ? "selected" : ""}>${t}</option>`,
      )
      .join(
        "",
      )}</select></div><div class="form-field"><label for="action-due">截止日期 · 可留空</label><input id="action-due" name="due_date" type="date" value="${escapeHTML(a.due_date || "")}"></div></div><div class="modal-actions"><button type="button" class="secondary-button" data-close-modal>取消</button><button type="submit" class="primary-button">保存修改</button></div></form>`,
  );
  $("#action-edit-form").onsubmit = async (event) => {
    event.preventDefault();
    const form = new FormData(event.target);
    try {
      const due = form.get("due_date") || null;
      await changeAction(id, {
        title: form.get("title"),
        kind: form.get("kind"),
        due_date: due,
      });
      modal.close();
    } catch (error) {
      toast(error.message);
    }
  };
}
async function showHistory(id) {
  const data = await api(`/api/history?id=${encodeURIComponent(id)}`);
  showModal(
    `<h2>原文的变化</h2><p>这里保留你每次保存的版本，AI 不会改写这些原文。</p>${data.versions.map((v) => `<div class="version-block"><p>版本 ${v.revision} · ${escapeHTML(timeAt(v.saved_at, true))} · 所属 ${v.journal_day}</p><pre>${escapeHTML(v.body)}</pre></div>`).join("")}<div class="modal-actions"><button class="secondary-button" data-close-modal>关闭</button></div>`,
  );
}
async function goSource(id, day) {
  if (day !== state.day || state.query || state.view === "usage") {
    await selectDay(day);
  }
  setView("record");
  const card = document.getElementById(`entry-${id}`);
  if (card) {
    card.scrollIntoView({ behavior: "smooth", block: "center" });
    card.classList.add("flash-highlight");
    setTimeout(() => card.classList.remove("flash-highlight"), 2000);
  } else toast("来源记录已更新，请通过日期或关键词查找");
}

async function download(format) {
  const blob = await api(`/api/export?format=${format}`, undefined, true);
  const url = URL.createObjectURL(blob),
    link = document.createElement("a");
  link.href = url;
  link.download = format === "markdown" ? "拾光日记.md" : "拾光完整备份.json";
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 3000);
  toast("导出文件已准备好");
}
async function previewImport(event) {
  const file = event.target.files[0];
  if (!file) return;
  try {
    if (file.size > 30_000_000) throw new Error("备份文件超过 30 MB");
    importedBundle = JSON.parse(await file.text());
    const preview = await api("/api/import/preview", {
      bundle: importedBundle,
    });
    showModal(
      `<h2>导入前，确认一下</h2><p>备份包含 ${preview.entries} 条日记和 ${preview.actions} 项行动。<br>可新增 ${preview.new} 条，重复 ${preview.duplicates} 条，冲突 ${preview.conflicts} 条。</p><div class="notice">合并时保留本机已有记录与行动选择。完整恢复会用备份替换现有日记；两种方式都会先自动备份。</div><div class="modal-actions"><button class="secondary-button" data-close-modal>取消</button><button id="import-replace" class="secondary-button danger-button">完整恢复</button><button id="import-merge" class="primary-button">合并导入</button></div>`,
    );
    $("#import-merge").onclick = () => commitImport("merge");
    $("#import-replace").onclick = async () => {
      if (
        await ask(
          "完整恢复此备份？",
          "现有日记与行动将被备份内容替换。系统会先创建恢复前备份。",
          "确认恢复",
        )
      )
        commitImport("replace");
    };
  } catch (error) {
    toast(error.message);
  } finally {
    event.target.value = "";
  }
}
async function commitImport(mode) {
  try {
    await api("/api/import", { bundle: importedBundle, mode });
    modal.close();
    importedBundle = null;
    toast("备份已导入");
    await loadState();
    renderSettings();
  } catch (error) {
    toast(error.message);
  }
}
async function restoreBackup(name) {
  if (
    !(await ask(
      "恢复这份本地备份？",
      "现有日记与行动将被替换，恢复前会另外创建一份备份。AI 密钥不受影响。",
      "恢复备份",
    ))
  )
    return;
  try {
    await api("/api/restore", { name });
    toast("已从本地备份恢复");
    await loadState();
    renderSettings();
  } catch (error) {
    toast(error.message);
  }
}
async function shutdown() {
  if ($("#entry-body").value.trim()) {
    toast("请先保存输入框里的记录，再退出服务");
    return;
  }
  if (
    !(await ask(
      "退出拾光本地服务？",
      "日记已经保存在电脑上。下次双击「启动拾光」即可继续。",
      "退出服务",
    ))
  )
    return;
  await api("/api/shutdown", {});
  $("#connection-banner").hidden = false;
  $("#connection-banner").textContent =
    "拾光已退出。下次双击「启动拾光」即可继续记录。";
}

document.addEventListener("click", async (event) => {
  const button = event.target.closest("button,[data-view]");
  if (!button) return;
  try {
    const d = button.dataset;
    if (d.view) {
      event.preventDefault();
      setView(d.view);
    } else if (d.date) await selectDay(d.date);
    else if (d.edit) await editEntry(d.edit);
    else if (d.delete) {
      if (
        await ask(
          "删除这个片段？",
          "原文、历史版本与相关 AI 整理将被删除。你已手动处理的行动会保留，并标记来源已失效。",
          "删除记录",
        )
      ) {
        await api("/api/entries/delete", { id: d.delete });
        await loadState();
        toast("记录已删除");
      }
    } else if (d.history) await showHistory(d.history);
    else if (d.retry) {
      await api("/api/retry", { entry_id: d.retry });
      await loadState();
      toast("已加入整理队列");
    } else if (d.retryDay) {
      await api("/api/retry", { day: state.day });
      await loadState();
      toast("已加入每日整理队列");
    } else if ("memoryPermission" in d) {
      const entry = state.data.entries.find((e) => e.id === d.memoryPermission);
      if (entry) {
        await api("/api/memory/permission", {
          id: entry.id,
          enabled: !entry.memory_enabled,
        });
        await loadState();
        toast(
          entry.memory_enabled
            ? "已排除长期记忆，相关理解已撤回"
            : "已选择资料；到「关于我」更新档案时才会读取",
        );
      }
    } else if (d.tab) {
      state.actionTab = d.tab;
      renderActions();
    } else if (d.actionStatus)
      await changeAction(d.actionStatus, { status: d.status });
    else if (d.promote)
      await changeAction(d.promote, { kind: "task", status: "open" });
    else if (d.actionEdit) editAction(d.actionEdit);
    else if (d.source) await goSource(d.source, d.day);
    else if ("closeModal" in d) modal.close();
    else if (d.export) await download(d.export);
    else if (d.restore) await restoreBackup(d.restore);
  } catch (error) {
    toast(error.message);
  }
});

async function init() {
  try {
    const boot = await api("/api/bootstrap");
    state.token = boot.token;
    state.version = boot.version;
    state.today = boot.today;
    state.aboutMonth = boot.today.slice(0, 7);
    state.usageMonth = boot.today.slice(0, 7);
    state.day = boot.today;
    state.month = dateAt(boot.today);
    state.settings = boot.settings;
    if (!$("#entry-day").value) $("#entry-day").value = boot.today;
    await loadState();
    const view = location.hash.slice(1);
    if (view) setView(view);
  } catch (error) {
    toast(error.message);
  }
}
init();
setInterval(() => {
  if (state.view === "about" && document.activeElement?.closest("#timeline-search")) return;
  if (state.view === "usage") {
    if (!document.hidden && !modal.open && !$("#usage-pricing")?.open)
      loadUsage({ silent: true });
    return;
  }
  if (
    state.data &&
    !document.hidden &&
    state.view !== "settings" &&
    !modal.open
  )
    loadState({ silent: true });
}, 7000);
