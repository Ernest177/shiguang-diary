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
  renderAbout();
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
    .filter((j) => j.status !== "done")
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
    ["month", "月度回顾"],
  ];
  const busy = a.jobs.some(
    (j) =>
      ["queued", "running"].includes(j.status) &&
      ["memory", "portrait"].includes(j.kind),
  );
  root.innerHTML = `<div class="about-hero"><div class="about-hero-copy"><div class="eyebrow">一份正在生长的个人档案</div><h1>慢慢认识，<br>正在变化的你<span>。</span></h1><p>把经历留给时间，把理解留给你确认。<br>这里收藏来路，也为新的自己留白。</p><div class="about-hero-actions"><button class="primary-button" data-experience-new>${icon("pen")}补录一段经历</button><button class="secondary-button" data-about-generate="memory" ${busy || !a.source_count ? "disabled" : ""}>${icon("sparkles")}${busy ? "正在更新档案" : "更新我的档案"}</button></div></div><div class="about-emblem" aria-hidden="true"><div class="about-emblem-ring"></div><span class="about-emblem-word">我</span><span class="about-emblem-caption">来路 · 此刻 · 可能</span><i></i><b></b></div></div><div class="about-overview"><span><strong>${a.source_count}</strong> 份已授权资料</span><span><strong>${a.memories.length}</strong> 张记忆卡片</span><span><strong>${confirmed}</strong> 项你的确认与修正</span><button class="text-button" data-about-sources>管理日记来源 ↗</button></div>${aboutJobNotice()}<div class="tabbar about-tabs" role="tablist" aria-label="个人档案栏目">${tabs.map(([id, title]) => `<button role="tab" aria-selected="${state.aboutTab === id}" class="${state.aboutTab === id ? "active" : ""}" data-about-tab="${id}">${title}${id === "memories" ? `<small>${a.memories.length}</small>` : ""}</button>`).join("")}</div><div id="about-content">${state.aboutTab === "portrait" ? portraitHTML() : state.aboutTab === "memories" ? memoriesHTML() : state.aboutTab === "experiences" ? experiencesHTML() : monthHTML()}</div><p class="about-footnote">${icon("shield")}资料保存在本机。只有你主动授权并点击整理后，才会交给已配置的 AI。新的理解始终可以修正。</p>`;
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

function portraitHTML() {
  const report = state.about.reports.portrait;
  if (!report || !report.data.items.length)
    return `<div class="about-intro"><div class="about-intro-mark">${icon("leaf")}</div><h2>你有自己的来路，<br>也有尚未写完的故事。</h2><p>先补录一段重要经历，或选择几条日记。<br>模型会整理值得留下的线索，每一项都由你确认。</p><div class="about-steps"><div><span>01</span><strong>留下经历</strong><small>一个转折、一段坚持，或一次新的选择。</small></div><div><span>02</span><strong>选择资料</strong><small>决定哪些内容可以进入长期记忆。</small></div><div><span>03</span><strong>确认理解</strong><small>认可、修正，让这份档案更贴近你。</small></div></div>${state.about.memories.length ? '<button class="secondary-button" data-about-generate="portrait">根据当前记忆生成画像 ↗</button>' : ""}</div>`;
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
    )}</div><p class="report-meta">${escapeHTML(timeAt(report.updated_at, true))} 更新 · ${escapeHTML(report.model)} · 每项理解均可通过记忆卡片修正</p>`;
}

function memoriesHTML() {
  const memories = state.about.memories;
  if (!memories.length)
    return empty(
      "记忆从一条有依据的理解开始",
      "选择资料后，点击「更新我的档案」。新卡片会先等待你确认。",
      "book",
    );
  return `<div class="about-section-heading"><div><h2>值得留下的线索</h2><p>修正过的表述会优先保留。删除记忆会同时撤回相关画像。</p></div><span class="about-count">${memories.length} 张卡片</span></div><div class="memory-grid">${memories.map((m) => `<article class="memory-card ${m.status}" id="memory-${escapeHTML(m.id)}"><div class="memory-card-top"><span>${escapeHTML(memoryNames[m.category])}</span><span class="memory-badge ${m.status}">${memoryStatusNames[m.status]}</span></div><h3>${escapeHTML(m.title)}</h3><p>${escapeHTML(m.status === "corrected" ? m.correction : m.detail)}</p><div class="memory-period">${m.horizon === "recent" ? "近期状态" : m.horizon === "long_term" ? "长期线索" : "时间范围待确认"}${m.period ? " · " + escapeHTML(m.period) : ""}</div><details class="source-details"><summary>查看原文依据 · ${m.sources.length} 处</summary>${m.sources.map((s) => `<blockquote>${escapeHTML(s.quote)}</blockquote>${aboutSource(s)}`).join("")}</details><div class="memory-card-actions">${m.status === "pending" ? `<button data-memory-confirm="${escapeHTML(m.id)}">${icon("check")}认可</button>` : ""}<button data-memory-correct="${escapeHTML(m.id)}">修正理解</button><button data-memory-forget="${escapeHTML(m.id)}" class="quiet-danger">删除记忆</button></div></article>`).join("")}</div>`;
}

function experiencesHTML() {
  const items = state.about.experiences;
  return `<div class="about-section-heading"><div><h2>那些塑造了你的片段</h2><p>不必从出生开始。先写下此刻愿意回看的经历，按补录时间排列。</p></div><button class="text-button" data-experience-new>补录经历 ＋</button></div>${items.length ? `<div class="experience-list">${items.map((e) => `<article class="experience-card"><div class="experience-date"><span>${escapeHTML(e.chapter)}</span><strong>${escapeHTML(e.period || "时间未注明")}</strong></div><div class="experience-body"><h3>${escapeHTML(e.title)}</h3><p>${escapeHTML(e.body)}</p><div class="experience-bottom"><span>${e.ai_enabled ? "已允许用于长期记忆" : "仅保存在本机"} · 版本 ${e.revision}</span><div><button class="text-button" data-experience-edit="${escapeHTML(e.id)}">编辑</button><button class="text-button quiet-danger" data-experience-delete="${escapeHTML(e.id)}">删除</button></div></div></div></article>`).join("")}</div>` : `<div class="experience-prompts"><span class="eyebrow">从一个小问题开始</span><h3>哪一次选择，让你更接近自己？</h3><p>也可以写：你曾经坚持过什么，谁影响过你，或者最近第一次做到的事。</p><button class="secondary-button" data-experience-new>写下这一段 ${icon("pen")}</button></div>`}`;
}

function monthHTML() {
  const report = state.about.reports.month;
  return `<div class="about-section-heading"><div><h2>变化，常常藏在回看里</h2><p>有上月资料时进行对比；资料不足时，保留本月的真实线索。</p></div><div class="month-controls"><input id="about-month" type="month" aria-label="月度回顾月份" value="${escapeHTML(state.aboutMonth)}"><button class="secondary-button" data-about-generate="month">整理这个月</button></div></div>${report ? `<div class="month-items">${report.data.items.length ? report.data.items.map((item) => `<article class="month-item"><span class="memory-badge">${item.comparison ? "与上月对照 · 待确认" : { events: "本月经历", growth: "成长线索 · 待确认", questions: "仍在探索" }[item.category]}</span><p>${escapeHTML(item.text)}</p><details class="source-details"><summary>查看依据</summary>${item.sources.map((s) => `<blockquote>${escapeHTML(s.quote)}</blockquote>${aboutSource(s)}`).join("")}</details></article>`).join("") : '<p class="about-empty-note">这个月暂未整理出足够明确的线索，可以继续记录后再回看。</p>'}</div><p class="report-meta">${escapeHTML(timeAt(report.updated_at, true))} 更新 · ${escapeHTML(report.model)}</p>` : empty("给变化留一点时间", "选择月份，整理这个月的经历与线索。每一处变化都应有前后的记录作依据。", "history")}<p class="about-footnote">月度回顾使用已授权且日期明确到月份的资料。模糊时间的成长经历会保留在记忆与画像中，不自动猜测所属月份。</p>`;
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
    `<h2>${existing ? "修改这一段经历" : "补录一段成长经历"}</h2><p>写你愿意留下的部分。时间可以模糊，意义也可以以后再理解。</p><form id="experience-form"><div class="form-field"><label for="experience-title">这一段的标题</label><input id="experience-title" name="title" maxlength="160" required value="${escapeHTML(e.title || "")}" placeholder="例如：第一次主动表达自己的想法"></div><div class="form-grid"><div class="form-field"><label for="experience-period">发生时间 · 可以不确定</label><input id="experience-period" name="period" maxlength="80" value="${escapeHTML(e.period || "")}" placeholder="2026-10、大学期间、大约2019年"></div><div class="form-field"><label for="experience-chapter">人生章节</label><select id="experience-chapter" name="chapter">${["成长", "学习", "工作", "关系", "生活", "转折"].map((c) => `<option ${c === (e.chapter || "成长") ? "selected" : ""}>${c}</option>`).join("")}</select></div></div><div class="form-field"><label for="experience-body">发生了什么，对你有什么意义？</label><textarea id="experience-body" name="body" rows="7" maxlength="20000" required placeholder="当时发生了什么，你怎么选择，现在如何看待？">${escapeHTML(e.body || "")}</textarea></div><label class="consent-check"><input name="ai_enabled" type="checkbox" ${e.ai_enabled ? "checked" : ""}><span>允许用于长期记忆。点击更新档案时，可发送至我配置的 AI 服务。</span></label><p class="experience-draft-hint">${draft && e === draft ? "已恢复未保存的经历草稿。" : "填写内容会暂存在当前浏览器，保存后正式写入本机。"}</p><div class="modal-actions"><button type="button" class="secondary-button" data-close-modal>稍后再写</button><button type="submit" class="primary-button">保存经历</button></div></form>`,
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
    `<h2>哪些日记，可以帮你认识自己？</h2><p>长期记忆单独授权。选择后，只有点击更新档案才会读取；排除后相关记忆与画像会立即撤回。</p><div class="source-picker">${entries.length ? entries.map((e) => `<label class="source-picker-row"><input type="checkbox" data-source-permission="${escapeHTML(e.id)}" ${e.memory_enabled ? "checked" : ""} ${e.ai_enabled ? "" : "disabled"}><span><strong>${escapeHTML(e.journal_day)}${e.ai_enabled ? "" : " · 已排除 AI"}</strong><small>${escapeHTML(e.preview)}</small></span></label>`).join("") : "<p>先写下一条日记，再来选择资料。</p>"}</div><p class="experience-draft-hint">这里列出最近200条。更早的记录可以在「日常记录」中逐条选择；已排除 AI 的记录需先编辑并允许 AI 整理。</p><div class="modal-actions"><button class="primary-button" data-close-modal>完成选择</button></div>`,
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
      renderAbout();
    } else if ("experienceNew" in d) editExperience();
    else if (d.experienceEdit) editExperience(d.experienceEdit);
    else if ("aboutSources" in d) chooseAboutSources();
    else if (d.aboutGenerate) await generateAbout(d.aboutGenerate, d.target);
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
