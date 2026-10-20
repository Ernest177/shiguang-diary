"use strict";
// Pure rendering checks with synthetic state; this does not replace browser QA.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const root = { innerHTML: "" };
const handlers = [];
let dialogHTML = "";
const quote = "一次主动表达";
const memory = {
  id: "memory-1",
  title: "<img src=x onerror=alert(1)>",
  detail: "已被用户修正的旧理解",
  correction: "只在准备充分时主动表达。",
  category: "fact",
  horizon: "recent",
  period: "2026-10",
  status: "corrected",
  sources: [
    {
      record_id: "entry:old-source",
      quote,
      period: "2026-10-01",
      title: "较早的日记",
    },
  ],
};
const state = {
  aboutTab: "portrait",
  aboutMonth: "2026-10",
  aboutSerial: 0,
  settings: { ai_mode: "off" },
  about: {
    memories: [memory],
    experiences: [],
    entries: [
      {
        id: "private",
        journal_day: "2026-10-01",
        preview: "<script>private</script>",
        ai_enabled: 0,
        memory_enabled: 0,
      },
    ],
    jobs: [],
    source_count: 1,
    reports: {
      portrait: {
        updated_at: "2026-10-20",
        model: "合成替身",
        data: {
          items: [
            {
              text: "只在准备充分时主动表达。",
              category: "strengths",
              certainty: "hypothesis",
              memory_ids: [memory.id],
              horizon: "recent",
              period: "2026-10",
            },
          ],
        },
      },
      month: {
        updated_at: "2026-10-20",
        model: "合成替身",
        data: {
          items: [
            {
              text: "【合成】有出处的回顾",
              category: "growth",
              comparison: true,
              sources: memory.sources,
            },
          ],
        },
      },
    },
  },
};
const escapeHTML = (value) =>
  String(value ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
const sandbox = {
  state,
  escapeHTML,
  icon: () => "<svg></svg>",
  empty: (title, body) => `<div>${escapeHTML(title)} ${escapeHTML(body)}</div>`,
  timeAt: () => "合成时间",
  $: (selector) => (selector === "#about-view" ? root : null),
  $$: () => [],
  modal: {},
  showModal: (value) => {
    dialogHTML = value;
  },
  document: { addEventListener: (_, fn) => handlers.push(fn) },
  console,
};
vm.createContext(sandbox);
vm.runInContext(
  fs.readFileSync(path.join(__dirname, "../web/about.js"), "utf8"),
  sandbox,
);
vm.runInContext("renderAbout()", sandbox);
assert.match(root.innerHTML, /待你确认的理解/);
assert.match(root.innerHTML, /关于|个人档案/);
state.aboutTab = "memories";
vm.runInContext("renderAbout()", sandbox);
assert.match(root.innerHTML, /只在准备充分时主动表达/);
assert.doesNotMatch(root.innerHTML, /已被用户修正的旧理解/);
assert.doesNotMatch(root.innerHTML, /<img src=x/);
assert.match(root.innerHTML, /&lt;img/);
assert.match(root.innerHTML, /data-about-day="2026-10-01"/);
state.aboutTab = "month";
vm.runInContext("renderAbout()", sandbox);
assert.match(root.innerHTML, /与上月对照 · 待确认/);
vm.runInContext("chooseAboutSources()", sandbox);
assert.match(dialogHTML, /disabled/);
assert.doesNotMatch(dialogHTML, /<script>private/);
state.about.memories = [];
state.about.reports = {};
state.about.source_count = 0;
state.aboutTab = "portrait";
vm.runInContext("renderAbout()", sandbox);
assert.match(root.innerHTML, /disabled/);
assert.match(root.innerHTML, /你的档案|你有自己的来路/);
state.about.organization = { mode: "auto", ready: false, records: 1, periods: 1 };
state.about.original_preview = [
  { text: "<img>合成的原话", label: "原文", source: memory.sources[0] },
  { text: "<img>合成的原话", label: "原文", source: { ...memory.sources[0], record_id: "entry:repeat-2", period: "2026-10-02" } },
];
vm.runInContext("renderAbout()", sandbox);
assert.match(root.innerHTML, /先留住你的原话/);
assert.match(root.innerHTML, /&lt;img&gt;合成的原话/);
assert.equal(root.innerHTML.split("&lt;img&gt;合成的原话").length - 1, 2);
assert.match(root.innerHTML, /data-about-source="entry:repeat-2" data-about-day="2026-10-02"/);
assert.doesNotMatch(root.innerHTML, /从记录中，读到的你/);
assert.doesNotMatch(root.innerHTML, /待你确认的理解/);
console.log(
  "Personal archive rendering checks passed (synthetic DOM stubs; no browser).",
);
