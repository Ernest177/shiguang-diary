"use strict";

const memoryNames = {
  fact: "经历线索",
  value: "重视的事",
  preference: "个人偏好",
  goal: "当前目标",
  pattern: "反复出现的模式",
};
const memoryStatusNames = {
  pending: "待你确认",
  confirmed: "你已确认",
  corrected: "以你的修正为准",
};
const portraitNames = {
  focus: "此刻关注",
  values: "重视的事",
  preferences: "适合你的方式",
  strengths: "已经展现的力量",
  questions: "仍在探索",
};

async function loadAbout() {
  const ticket = ++state.aboutSerial;
  const data = await api(
    `/api/about?month=${encodeURIComponent(state.aboutMonth || state.today.slice(0, 7))}`,
  );
  if (ticket !== state.aboutSerial) return;
  state.about = data;
  if (state.aboutTab === "timeline") await loadGrowthTimeline(false);
  renderAbout();
}

function growthControlHTML() {
  const g = state.about.growth;
  if (!g) return "";
  if (!g.enabled)
    return `<div class="growth-strip"><div><strong>让记录，慢慢连成你的故事</strong><p>开启后自动积累，不必逐条选择日记。原文始终留在本机。</p></div><button class="secondary-button" data-growth-settings>开启自动积累 ↗</button></div>`;
  return `<div class="growth-strip" role="status"><div><strong>${g.paused ? "自动积累已暂停" : g.pending ? "正在积累你的成长记录" : "成长记录已跟上你的脚步"}</strong><p>已整理 ${g.complete} / ${g.total} 份记录${g.pending ? ` · 还有 ${g.remaining_chunks} 个片段` : ""}${g.failed ? ` · ${g.failed} 份未完成` : ""}。${escapeHTML(g.notice || "新日记随整理积累，旧日记分批接续。")}</p></div><div class="growth-strip-actions"><button class="text-button" data-growth-toggle>${g.paused ? "继续积累" : "暂停"}</button><button class="text-button" data-growth-settings>积累设置 ↗</button></div></div>`;
}

function growthSettings() {
  const g = state.about.growth || {};
  showModal(`<h2>让日记，持续积累</h2><p>开启一次，已有和以后的 AI 日记会自动纳入。已排除 AI 的日记、仅本机保存的经历仍保持私密。你也可以随时排除某条记录。</p><form id="growth-policy-form"><label class="consent-check"><input name="enabled" type="checkbox" ${g.enabled ? "checked" : ""}><span>自动积累成长线索，交给我配置的 AI 整理</span></label><div class="form-grid"><div class="form-field"><label for="growth-calls">每日额外请求上限</label><input id="growth-calls" name="calls" type="number" min="1" max="100" value="${g.daily_calls || 12}" required></div><div class="form-field"><label for="growth-budget">每月参考预算 · 元</label><input id="growth-budget" name="budget" type="number" min="0.01" max="10000" step="0.01" value="${escapeHTML(g.monthly_budget || "1.00")}" required></div></div><p class="experience-draft-hint">上限用于成长片段、画像和月度回顾的额外请求。日记原有整理另计。预算依据模型返回用量估算，到达后暂停后续请求；单次调用可能使金额超过预算，缺失用量及平台账单差异也会影响估算。</p>${g.usage ? `<p class="report-meta">今日额外请求 ${g.usage.today_calls} 次 · 本月已知估算 ¥${escapeHTML(g.usage.month_cost)}${g.usage.unknown_cost ? ` · ${g.usage.unknown_cost} 次费用未知` : ""}</p>` : ""}<details><summary>原文、缓存与隐私</summary><p>新日记复用单条整理请求。历史日记逐片段处理一次，结果保存在本机；更新画像只使用节选线索。云端模式下，参与处理的原文片段仍会发送至你设置的服务。暂停不会阻止日记原有的 AI 整理。</p><button type="button" class="text-button" data-growth-sources>查看或排除单条来源 ↗</button></details><div class="modal-actions"><button type="button" class="secondary-button" data-close-modal>取消</button><button type="submit" class="primary-button">保存设置</button></div></form>`);
  $("#growth-policy-form").onsubmit = async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    try {
      await api("/api/growth/policy", { growth_enabled: form.elements.enabled.checked, growth_paused: false,
        growth_daily_calls: Number(form.elements.calls.value), growth_monthly_budget: form.elements.budget.value });
      modal.close();
      await loadAbout();
      toast("积累设置已保存");
    } catch (error) { toast(error.message); }
  };
}

async function loadGrowthTimeline(render = true) {
  const serial = (state.timelineSerial || 0) + 1;
  state.timelineSerial = serial;
  const result = await api(`/api/growth/timeline?q=${encodeURIComponent(state.timelineQuery || "")}&kind=${encodeURIComponent(state.timelineKind || "all")}&offset=${state.timelineOffset || 0}`);
  if (serial !== state.timelineSerial) return;
  state.timeline = result;
  if (render && state.aboutTab === "timeline") renderAbout();
}

function timelineHTML() {
  const t = state.timeline || { items: [], total: 0, offset: 0 };
  const names = { milestone: "重要经历", turning_point: "人生转折", moment: "日常感受" };
  return `<div class="about-section-heading"><div><h2>生活的细节，也有自己的位置</h2><p>全部原文都可在本机回看。重要经历与日常感受会逐渐浮现。</p></div><span class="about-count">${t.total} 份记录</span></div><form id="timeline-search" class="timeline-search"><input name="query" maxlength="100" aria-label="搜索成长记录" placeholder="找一段经历、一个人，或当时的感受…" value="${escapeHTML(state.timelineQuery || "")}"><select name="kind" aria-label="成长片段类型">${Object.entries({ all: "全部记录", ...names }).map(([key, label]) => `<option value="${key}" ${key === (state.timelineKind || "all") ? "selected" : ""}>${label}</option>`).join("")}</select><button class="secondary-button" type="submit">查找</button></form><div class="growth-timeline">${t.items.length ? t.items.map((item) => `<article class="growth-moment"><div class="growth-moment-date">${escapeHTML(item.period || "时间未注明")}</div><div><h3>${escapeHTML(item.title)}</h3><p>${escapeHTML(item.preview)}</p>${item.events.map((e) => `<div class="growth-event"><span>${names[e.kind]}</span><strong>${escapeHTML(e.title)}</strong><p>${escapeHTML(e.detail)}</p></div>`).join("")}${aboutSource(item)}</div></article>`).join("") : empty("每个片段，都可以留下", "直接写日记即可。搜索只在本机进行，不消耗模型 Token。", "leaf")}</div>${t.total > 30 ? `<div class="timeline-pagination"><button class="text-button" data-timeline-page="${Math.max(0, t.offset - 30)}" ${t.offset ? "" : "disabled"}>上一页</button><span>${Math.floor(t.offset / 30) + 1} / ${Math.ceil(t.total / 30)}</span><button class="text-button" data-timeline-page="${t.offset + 30}" ${t.offset + 30 < t.total ? "" : "disabled"}>下一页</button></div>` : ""}<p class="report-meta">关键词检索 · 不调用模型；模糊时间保留原样，暂按补录时间排列。</p>`;
}

function aboutSource(source) {
  const label =
    source.title ||
    (source.record_id.startsWith("entry:") ? "日记依据" : "经历依据");
  return `<button class="source-link" data-about-source="${escapeHTML(source.record_id)}" data-about-day="${escapeHTML(source.period || "")}">${escapeHTML(label)} ↗</button>`;
}

function aboutJobNotice() {
  const jobs = state.about.jobs;
  const labels = {
    memory: "整理长期记忆",
    portrait: "生成个人画像",
    month: "整理月度回顾",
  };
  return jobs
    .filter((j) => j.status !== "done" && !(state.about.growth?.enabled && j.status === "deferred"))
    .map(
      (j) =>
        `<div class="about-job ${j.status === "failed" ? "error" : ""}" role="status"><span>${icon(j.status === "failed" ? "list" : "sparkles")}</span><div><strong>${escapeHTML(labels[j.kind])} · ${j.status === "failed" ? "未完成" : j.status === "running" ? "正在进行" : "等待处理"}</strong><p>${escapeHTML(j.error || "你可以继续记录，完成后会在这里更新。")}</p></div>${j.status === "failed" ? `<button class="text-button" data-about-generate="${j.kind}" data-target="${escapeHTML(j.target)}">重试 ↗</button>` : ""}</div>`,
    )
    .join("");
}

function renderAbout() {
  const root = $("#about-view");
  if (!state.about) {
    root.innerHTML = empty("正在翻开你的档案", "成长不必急着下结论。", "leaf");
    return;
  }
  const a = state.about;
  const confirmed = a.memories.filter((m) => m.status !== "pending").length;
  const tabs = [
    ["portrait", "我的画像"],
    ["memories", "记忆卡片"],
    ["experiences", "成长经历"],
    ["timeline", "成长足迹"],
    ["month", "月度回顾"],
  ];
  const busy = a.jobs.some(
    (j) =>
      ["queued", "running"].includes(j.status) &&
      ["memory", "portrait"].includes(j.kind),
  );
  root.innerHTML = `<div class="about-hero"><div class="about-hero-copy"><div class="eyebrow">一份正在生长的个人档案</div><h1>慢慢认识，<br>正在变化的你<span>。</span></h1><p>把经历留给时间，把理解留给你确认。<br>这里收藏来路，也为新的自己留白。</p><div class="about-hero-actions"><button class="primary-button" data-experience-new>${icon("pen")}补录一段经历</button><button class="secondary-button" data-about-generate="memory" ${busy || !a.source_count ? "disabled" : ""}>${icon("sparkles")}${busy ? "正在更新档案" : "更新我的档案"}</button></div></div><div class="about-emblem" aria-hidden="true"><div class="about-emblem-ring"></div><span class="about-emblem-word">我</span><span class="about-emblem-caption">来路 · 此刻 · 可能</span><i></i><b></b></div></div><div class="about-overview"><span><strong>${a.source_count}</strong> ${a.growth?.enabled ? "份自动积累资料" : "份已授权资料"}</span><span><strong>${a.memories.length}</strong> 张记忆卡片</span><span><strong>${confirmed}</strong> 项你的确认与修正</span><button class="text-button" data-growth-settings>积累设置 ↗</button></div>${growthControlHTML()}${aboutJobNotice()}<div class="tabbar about-tabs" role="tablist" aria-label="个人档案栏目">${tabs.map(([id, title]) => `<button role="tab" aria-selected="${state.aboutTab === id}" class="${state.aboutTab === id ? "active" : ""}" data-about-tab="${id}">${title}${id === "memories" ? `<small>${a.memories.length}</small>` : ""}</button>`).join("")}</div><div id="about-content">${state.aboutTab === "portrait" ? portraitHTML() : state.aboutTab === "memories" ? memoriesHTML() : state.aboutTab === "experiences" ? experiencesHTML() : state.aboutTab === "timeline" ? timelineHTML() : monthHTML()}</div><p class="about-footnote">${icon("shield")}原文保存在本机。${a.growth?.enabled ? "参与 AI 的记录会自动积累，云端整理会发送必要片段。" : "开启自动积累，或手动选择资料后整理。"}新的理解始终可以修正。</p>`;
  const search = $("#timeline-search");
  if (search) search.onsubmit = async (event) => {
    event.preventDefault();
    state.timelineQuery = search.elements.query.value;
    state.timelineKind = search.elements.kind.value;
    state.timelineOffset = 0;
    try { await loadGrowthTimeline(); } catch (error) { toast(error.message); }
  };
  const month = $("#about-month");
  if (month)
    month.onchange = async () => {
      state.aboutMonth = month.value;
      try {
        await loadAbout();
      } catch (error) {
        toast(error.message);
      }
    };
}

function portraitScopeHTML(scope) {
  if (!scope?.selection_version) return "";
  const period = scope.period_start && scope.period_end ? `${scope.period_start} 至 ${scope.period_end}` : "部分线索没有明确日期";
  return `<details class="source-details portrait-scope"><summary>这次画像参考了哪些线索</summary><p>使用 ${escapeHTML(scope.included)} / ${escapeHTML(scope.available)} 条线索，来自 ${escapeHTML(scope.source_count)} 份记录。包含 ${escapeHTML(scope.decisions)} 条你的确认与修正、${escapeHTML(scope.long_term)} 条长期线索。</p><p>已知时间范围：${escapeHTML(period)}。兼顾近期与早期经历；未选入的记忆仍完整保留，可在记忆卡片与成长足迹中查看。</p></details>`;
}

function portraitHTML() {
  const report = state.about.reports.portrait;
  if (state.about.organization?.mode === "auto" && (!state.about.organization.ready || !report?.data.items.length)) {
    const preview = state.about.original_preview || [];
    return `<div class="about-section-heading"><div><h2>先留住你的原话</h2><p>${state.about.organization.ready ? "暂时没有需要额外概括的理解，先回看这些已记录的线索。" : "资料还少，先积累，不急着为你下结论。"}</p></div></div>${preview.length ? `<div class="original-portrait">${preview.map((item) => `<article><span class="memory-period">${escapeHTML(item.label)} · ${escapeHTML(item.source.period || "时间未注明")}</span><blockquote class="original-prose">${escapeHTML(item.text)}</blockquote>${aboutSource(item.source)}</article>`).join("")}</div>` : empty("生活会慢慢连成故事", "继续写日记即可；行动与成长线索照常提取。", "leaf")}<p class="report-meta">来自至少3份记录、跨越2个日期或阶段的线索后，再尝试总结变化。这是开始整理的条件，不代表已经完整了解你。</p>`;
  }
  if (!report || !report.data.items.length)
    return `<div class="about-intro"><div class="about-intro-mark">${icon("leaf")}</div><h2>你有自己的来路，<br>也有尚未写完的故事。</h2><p>${state.about.growth?.enabled ? "继续写日记，生活的细节会慢慢积累。<br>有依据的理解会在这里出现，你随时可以修正。" : "开启自动积累，日记会自然连成你的成长记录。<br>也可以先补录一段对你重要的经历。"}</p><div class="about-steps"><div><span>01</span><strong>留下经历</strong><small>一个转折、一段坚持，或一次新的选择。</small></div><div><span>02</span><strong>自动积累</strong><small>新内容顺手整理，旧记录分批接续。</small></div><div><span>03</span><strong>回看与修正</strong><small>不急着定义自己，允许理解不断变化。</small></div></div>${state.about.memories.length ? '<button class="secondary-button" data-about-generate="portrait">根据当前记忆生成画像 ↗</button>' : ""}</div>`;
  return `<div class="about-section-heading"><div><h2>从记录中，读到的你</h2><p>原文线索与模型的解释分开呈现；这些理解不定义你。</p></div><button class="text-button" data-about-generate="portrait">仅更新画像 ↗</button></div><div class="portrait-grid">${Object.entries(
    portraitNames,
  )
    .map(([key, title]) => {
      const items = report.data.items.filter((i) => i.category === key);
      return items.length
        ? `<section class="portrait-section"><div class="portrait-section-label">${icon(key === "strengths" ? "leaf" : "sparkles")}<h3>${title}</h3></div>${items.map((item) => `<div class="portrait-item"><span class="memory-badge ${item.certainty === "observation" ? "confirmed" : ""}">${item.certainty === "observation" ? "你已确认的原话" : "待你确认的理解"}</span>${item.horizon === "recent" ? '<span class="memory-period">近期线索</span>' : ""}<p>${escapeHTML(item.text)}</p>${item.period ? `<small class="memory-period">${escapeHTML(item.period)}</small>` : ""}<div>${item.memory_ids.map((id) => `<button class="source-link" data-memory-focus="${escapeHTML(id)}">查看记忆依据 ↗</button>`).join("")}</div></div>`).join("")}</section>`
        : "";
    })
    .join(
      "",
    )}</div><p class="report-meta">${escapeHTML(timeAt(report.updated_at, true))} 更新 · ${escapeHTML(report.model)} · 每项理解均可通过记忆卡片修正${report.data.scope ? ` · 本次使用 ${report.data.scope.included} / ${report.data.scope.available} 条线索节选` : ""}</p>${portraitScopeHTML(report.data.scope)}`;
}

function memoriesHTML() {
  const all = state.about.memories;
  const memories = all.slice(0, state.memoryLimit || 24);
  if (!memories.length)
    return empty(
      "记忆从一条有依据的理解开始",
      "开启自动积累后，线索会随日记出现。你可以认可，也可以随时修正。",
      "book",
    );
  return `<div class="about-section-heading"><div><h2>值得留下的线索</h2><p>修正过的表述会优先保留。删除记忆会同时撤回相关画像。</p></div><span class="about-count">${all.length} 张卡片</span></div><div class="memory-grid">${memories.map((m) => `<article class="memory-card ${m.status}" id="memory-${escapeHTML(m.id)}"><div class="memory-card-top"><span>${escapeHTML(memoryNames[m.category])}</span><span class="memory-badge ${m.status}">${m.status === "pending" && state.about.growth?.enabled ? "可修正的线索" : memoryStatusNames[m.status]}</span></div><h3>${escapeHTML(m.title)}</h3><p>${escapeHTML(m.status === "corrected" ? m.correction : m.detail)}</p><div class="memory-period">${m.horizon === "recent" ? "近期状态" : m.horizon === "long_term" ? "长期线索" : "时间范围待确认"}${m.period ? " · " + escapeHTML(m.period) : ""}</div><details class="source-details"><summary>查看原文依据 · ${m.sources.length} 处</summary>${m.sources.map((s) => `<blockquote>${escapeHTML(s.quote)}</blockquote>${aboutSource(s)}`).join("")}</details><div class="memory-card-actions">${m.status === "pending" ? `<button data-memory-confirm="${escapeHTML(m.id)}">${icon("check")}认可</button>` : ""}<button data-memory-correct="${escapeHTML(m.id)}">修正理解</button><button data-memory-forget="${escapeHTML(m.id)}" class="quiet-danger">删除记忆</button></div></article>`).join("")}</div>${memories.length < all.length ? '<button class="secondary-button" data-memory-more>查看更多线索</button>' : ""}`;
}

function experiencesHTML() {
  const items = state.about.experiences;
  return `<div class="about-section-heading"><div><h2>那些塑造了你的片段</h2><p>不必从出生开始。先写下此刻愿意回看的经历，按补录时间排列。</p></div><button class="text-button" data-experience-new>补录经历 ＋</button></div>${items.length ? `<div class="experience-list">${items.map((e) => `<article class="experience-card"><div class="experience-date"><span>${escapeHTML(e.chapter)}</span><strong>${escapeHTML(e.period || "时间未注明")}</strong></div><div class="experience-body"><h3>${escapeHTML(e.title)}</h3><p>${escapeHTML(e.body)}</p><div class="experience-bottom"><span>${e.ai_enabled ? "已允许用于长期记忆" : "仅保存在本机"} · 版本 ${e.revision}</span><div><button class="text-button" data-experience-edit="${escapeHTML(e.id)}">编辑</button><button class="text-button quiet-danger" data-experience-delete="${escapeHTML(e.id)}">删除</button></div></div></div></article>`).join("")}</div>` : `<div class="experience-prompts"><span class="eyebrow">从一个小问题开始</span><h3>哪一次选择，让你更接近自己？</h3><p>也可以写：你曾经坚持过什么，谁影响过你，或者最近第一次做到的事。</p><button class="secondary-button" data-experience-new>写下这一段 ${icon("pen")}</button></div>`}`;
}

function monthHTML() {
  const report = state.about.reports.month;
  return `<div class="about-section-heading"><div><h2>变化，常常藏在回看里</h2><p>有上月资料时进行对比；资料不足时，保留本月的真实线索。</p></div><div class="month-controls"><input id="about-month" type="month" aria-label="月度回顾月份" value="${escapeHTML(state.aboutMonth)}"><button class="secondary-button" data-about-generate="month">整理这个月</button></div></div>${report ? `<div class="month-items">${report.data.items.length ? report.data.items.map((item) => `<article class="month-item"><span class="memory-badge">${item.comparison ? "与上月对照 · 待确认" : { events: "本月经历", growth: "成长线索 · 待确认", questions: "仍在探索" }[item.category]}</span><p>${escapeHTML(item.text)}</p><details class="source-details"><summary>查看依据</summary>${item.sources.map((s) => `<blockquote>${escapeHTML(s.quote)}</blockquote>${aboutSource(s)}`).join("")}</details></article>`).join("") : '<p class="about-empty-note">这个月暂未整理出足够明确的线索，可以继续记录后再回看。</p>'}</div><p class="report-meta">${escapeHTML(timeAt(report.updated_at, true))} 更新 · ${escapeHTML(report.model)}</p>` : empty("给变化留一点时间", "选择月份，整理这个月的经历与线索。每一处变化都应有前后的记录作依据。", "history")}<p class="about-footnote">${state.about.growth?.enabled ? "月度回顾使用本月与上月已积累的线索节选，最多各10份记录，不代表完整月份。" : "月度回顾使用已授权且日期明确到月份的资料。"}模糊时间保留原样，不猜测所属月份。</p>`;
}

function editExperience(id) {
  const existing = state.about?.experiences.find((e) => e.id === id);
  let draft = null;
  try {
    draft = JSON.parse(
      localStorage.getItem("shiguang-experience-draft") || "null",
    );
  } catch {}
  const e =
    draft && (draft.id || null) === (id || null) ? draft : existing || {};
  showModal(
    `<h2>${existing ? "修改这一段经历" : "补录一段成长经历"}</h2><p>写你愿意留下的部分。时间可以模糊，意义也可以以后再理解。</p><form id="experience-form"><div class="form-field"><label for="experience-title">这一段的标题</label><input id="experience-title" name="title" maxlength="160" required value="${escapeHTML(e.title || "")}" placeholder="例如：第一次主动表达自己的想法"></div><div class="form-grid"><div class="form-field"><label for="experience-period">发生时间 · 可以不确定</label><input id="experience-period" name="period" maxlength="80" value="${escapeHTML(e.period || "")}" placeholder="2026-10、大学期间、大约2019年"></div><div class="form-field"><label for="experience-chapter">人生章节</label><select id="experience-chapter" name="chapter">${["成长", "学习", "工作", "关系", "生活", "转折"].map((c) => `<option ${c === (e.chapter || "成长") ? "selected" : ""}>${c}</option>`).join("")}</select></div></div><div class="form-field"><label for="experience-body">发生了什么，对你有什么意义？</label><textarea id="experience-body" name="body" rows="7" maxlength="20000" required placeholder="当时发生了什么，你怎么选择，现在如何看待？">${escapeHTML(e.body || "")}</textarea></div><label class="consent-check"><input name="ai_enabled" type="checkbox" ${(e.ai_enabled ?? (!existing && state.about.growth?.enabled)) ? "checked" : ""}><span>允许 AI 整理这段经历。开启自动积累后，会发送至我配置的服务。</span></label><p class="experience-draft-hint">${draft && e === draft ? "已恢复未保存的经历草稿。" : "填写内容会暂存在当前浏览器，保存后正式写入本机。"}</p><div class="modal-actions"><button type="button" class="secondary-button" data-close-modal>稍后再写</button><button type="submit" class="primary-button">保存经历</button></div></form>`,
  );
  const form = $("#experience-form");
  const clientId = e.client_id || crypto.randomUUID();
  const payload = () => ({
    ...(existing
      ? { id: existing.id, revision: e.revision || existing.revision }
      : { client_id: clientId }),
    title: form.elements.title.value,
    period: form.elements.period.value,
    chapter: form.elements.chapter.value,
    body: form.elements.body.value,
    ai_enabled: form.elements.ai_enabled.checked,
  });
  form.oninput = () => {
    try {
      localStorage.setItem(
        "shiguang-experience-draft",
        JSON.stringify(payload()),
      );
    } catch {}
  };
  form.onsubmit = async (event) => {
    event.preventDefault();
    const button = form.querySelector("[type=submit]");
    button.disabled = true;
    try {
      await api("/api/experiences/save", payload());
      try {
        localStorage.removeItem("shiguang-experience-draft");
      } catch {}
      modal.close();
      state.aboutTab = "experiences";
      await loadAbout();
      toast("经历已保存在本机");
    } catch (error) {
      toast(error.message);
    } finally {
      button.disabled = false;
    }
  };
}

function chooseAboutSources() {
  const entries = state.about.entries;
  showModal(
    `<h2>日记来源</h2><p>${state.about.growth?.enabled ? "AI 日记默认参与自动积累，无需逐条选择。取消勾选可排除个别记录，并撤回相关记忆。" : "你可以手动选择来源，也可以在积累设置中开启自动处理。"}</p><div class="source-picker">${entries.length ? entries.map((e) => `<label class="source-picker-row"><input type="checkbox" data-source-permission="${escapeHTML(e.id)}" ${e.memory_enabled ? "checked" : ""} ${e.ai_enabled ? "" : "disabled"}><span><strong>${escapeHTML(e.journal_day)}${e.ai_enabled ? "" : " · 已排除 AI"}</strong><small>${escapeHTML(e.preview)}</small></span></label>`).join("") : "<p>先写下一条日记，再来选择资料。</p>"}</div><p class="experience-draft-hint">这里显示最近200条。自动积累覆盖全部符合设置的记录；更早的记录也能在日常记录中排除。</p><div class="modal-actions"><button class="primary-button" data-close-modal>完成选择</button></div>`,
  );
  $$("[data-source-permission]", modal).forEach(
    (input) =>
      (input.onchange = async () => {
        input.disabled = true;
        try {
          await api("/api/memory/permission", {
            id: input.dataset.sourcePermission,
            enabled: input.checked,
          });
          await loadAbout();
        } catch (error) {
          input.checked = !input.checked;
          toast(error.message);
        } finally {
          input.disabled = false;
        }
      }),
  );
}

async function generateAbout(kind, target) {
  if (state.settings.ai_mode === "off") {
    toast("先到「偏好与设置」启用 AI，再整理你的档案");
    return;
  }
  target = target || (kind === "month" ? state.aboutMonth : "current");
  if (state.about.growth?.enabled) {
    await api("/api/about/generate", { kind, target });
    await loadAbout();
    toast("已检查更新；内容未变化时直接复用本机结果");
    return;
  }
  const scope =
    kind === "memory"
      ? `已授权的 ${state.about.source_count} 份资料，先整理记忆，再生成画像，最多两次模型请求`
      : kind === "portrait"
        ? "当前记忆卡片与有效来源，进行一次模型请求"
        : `${target} 及其上个月的已授权资料，进行一次模型请求`;
  if (
    !(await ask(
      "更新这份个人档案？",
      `本次使用${scope}。${state.settings.ai_mode === "cloud" ? "内容会发送至设置中的云端服务，并按提供方规则计费。" : "内容将交给你配置的本机模型。"}失败后由你决定是否重试。`,
      "开始整理",
    ))
  )
    return;
  await api("/api/about/generate", { kind, target });
  await loadAbout();
  toast("已加入整理队列");
}

async function memoryDecision(id, status) {
  const memory = state.about.memories.find((m) => m.id === id);
  if (!memory) return;
  await api("/api/memory/update", {
    id,
    status,
    updated_at: memory.updated_at,
  });
  await loadAbout();
  toast("已记录你的确认；下次生成画像会采用你的选择");
}

function correctMemory(id) {
  const memory = state.about.memories.find((m) => m.id === id);
  if (!memory) return;
  showModal(
    `<h2>让理解，更贴近你</h2><p>你可以补充适用情境、例外，或直接改写这项理解。后续整理会优先保留你的表述。</p><form id="memory-correction-form"><div class="form-field"><label for="memory-correction">我的实际情况是</label><textarea id="memory-correction" rows="6" maxlength="3000" required>${escapeHTML(memory.correction || memory.detail)}</textarea></div><div class="modal-actions"><button type="button" class="secondary-button" data-close-modal>取消</button><button type="submit" class="primary-button">保存我的修正</button></div></form>`,
  );
  $("#memory-correction-form").onsubmit = async (event) => {
    event.preventDefault();
    try {
      await api("/api/memory/update", {
        id,
        status: "corrected",
        correction: $("#memory-correction").value,
        updated_at: memory.updated_at,
      });
      modal.close();
      await loadAbout();
      toast("修正已保存，相关旧画像已撤回");
    } catch (error) {
      toast(error.message);
    }
  };
}

document.addEventListener("click", async (event) => {
  const button = event.target.closest("button");
  if (!button) return;
  const d = button.dataset;
  try {
    if (d.aboutTab) {
      state.aboutTab = d.aboutTab;
      if (d.aboutTab === "timeline") await loadGrowthTimeline(false);
      renderAbout();
    } else if ("experienceNew" in d) editExperience();
    else if (d.experienceEdit) editExperience(d.experienceEdit);
    else if ("growthSettings" in d) growthSettings();
    else if ("growthToggle" in d) {
      await api("/api/growth/policy", { growth_paused: !state.about.growth.paused });
      await loadAbout();
    }
    else if ("timelinePage" in d) {
      state.timelineOffset = Number(d.timelinePage);
      await loadGrowthTimeline();
    }
    else if ("growthSources" in d) chooseAboutSources();
    else if ("aboutSources" in d) chooseAboutSources();
    else if (d.aboutGenerate) await generateAbout(d.aboutGenerate, d.target);
    else if ("memoryMore" in d) { state.memoryLimit = (state.memoryLimit || 24) + 24; renderAbout(); }
    else if (d.memoryConfirm)
      await memoryDecision(d.memoryConfirm, "confirmed");
    else if (d.memoryCorrect) correctMemory(d.memoryCorrect);
    else if (d.memoryForget) {
      if (
        await ask(
          "删除这条长期记忆？",
          "相关画像会撤回，并阻止它从同一依据再次出现。原始记录与历史备份仍保留。",
          "删除记忆",
        )
      ) {
        await api("/api/memory/forget", { id: d.memoryForget });
        await loadAbout();
        toast("记忆已删除，相关画像已撤回");
      }
    } else if (d.experienceDelete) {
      if (
        await ask(
          "删除这段经历？",
          "经历原文与依赖它的记忆将撤回。历史备份仍按保留规则管理。",
          "删除经历",
        )
      ) {
        await api("/api/experiences/delete", { id: d.experienceDelete });
        await loadAbout();
        toast("经历已删除");
      }
    } else if (d.memoryFocus) {
      state.aboutTab = "memories";
      state.memoryLimit = Math.max(state.memoryLimit || 24, state.about.memories.findIndex((m) => m.id === d.memoryFocus) + 1);
      renderAbout();
      const card = document.getElementById("memory-" + d.memoryFocus);
      if (card) {
        card.scrollIntoView({ behavior: "smooth", block: "center" });
        card.classList.add("memory-highlight");
      }
    } else if (d.aboutSource) {
      const [kind, id] = d.aboutSource.split(":");
      if (kind === "experience") {
        const e = state.about.experiences.find((e) => e.id === id);
        if (e)
          showModal(
            `<h2>${escapeHTML(e.title)}</h2><p>${escapeHTML(e.period || "时间未注明")} · ${escapeHTML(e.chapter)}</p><pre class="experience-original">${escapeHTML(e.body)}</pre><div class="modal-actions"><button class="primary-button" data-close-modal>关闭</button></div>`,
          );
      } else {
        const e = state.about.entries.find((e) => e.id === id);
        if (e || /^\d{4}-\d{2}-\d{2}$/.test(d.aboutDay || ""))
          await goSource(id, e?.journal_day || d.aboutDay);
        else {
          state.query = "";
          setView("record");
          toast("请通过日期或关键词查找这条较早的来源记录");
        }
      }
    }
  } catch (error) {
    toast(error.message);
  }
});
