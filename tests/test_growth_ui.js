"use strict";
// Synthetic rendering and actions; not browser or model quality verification.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const root = { innerHTML: "" }, handlers = [], calls = [];
let dialog = "", form = {};
const state = { aboutTab: "portrait", aboutMonth: "2026-10", settings: { ai_mode: "local" },
  about: { memories: [], experiences: [], entries: [], reports: {}, jobs: [], source_count: 4,
    growth: { enabled: true, paused: false, complete: 2, total: 4, pending: 2, remaining_chunks: 3,
      daily_calls: 12, monthly_budget: "1.00", failed: 1, notice: "", usage: { today_calls: 2, month_cost: "0.001", unknown_cost: 1 } } } };
const escapeHTML = (s) => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const sandbox = { state, escapeHTML, icon: () => "", timeAt: () => "合成时间", empty: (a,b) => a+b,
  $: s => s === "#about-view" ? root : s === "#growth-policy-form" ? form : null, $$: () => [],
  showModal: html => {dialog=html;}, modal: {close(){}}, document: { addEventListener: (_, fn) => handlers.push(fn) },
  toast() {}, api: async (url, data) => { calls.push({url,data});
    if (url.startsWith("/api/about?")) return state.about;
    if (url.startsWith("/api/growth/timeline")) return { items: [], total: 0, offset: 0 };
    return {}; }, console };
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(require("node:path").join(__dirname,"../web/about.js"),"utf8"),sandbox);
(async () => {
  vm.runInContext("renderAbout()",sandbox);
  assert.match(root.innerHTML,/已整理 2 \/ 4/);
  assert.match(root.innerHTML,/成长足迹/);
  assert.match(root.innerHTML,/可修正|随时可以修正/);
  assert.doesNotMatch(root.innerHTML,/选择几条日记/);
  assert.equal(calls.length,0,"rendering itself must not request a model");
  vm.runInContext("growthSettings()",sandbox);
  assert.match(dialog,/每日额外请求上限/);
  assert.match(dialog,/每月参考预算/);
  assert.match(dialog,/费用未知/);
  assert.match(dialog,/仍会发送/);
  assert.doesNotMatch(dialog,/data-source-permission/);
  state.about.growth.paused = true;
  vm.runInContext("renderAbout()",sandbox);
  assert.match(root.innerHTML,/自动积累已暂停/);
  state.aboutTab = "timeline";
  state.timeline = { offset: 0, total: 35, items: [{record_id:"entry:synthetic", period:"2026-10-20", title:"<script>标题</script>", preview:"<img onerror=alert(1)>",events:[{kind:"moment",title:"细小感受",detail:"<svg onload=alert(1)>"}]}] };
  vm.runInContext("renderAbout()",sandbox);
  assert.match(root.innerHTML,/data-timeline-page="30"/);
  assert.doesNotMatch(root.innerHTML,/<script>|<img|<svg onload/);
  assert.match(root.innerHTML,/data-about-day="2026-10-20"/);
  await handlers[0]({target:{closest:()=>({dataset:{timelinePage:"30"}})}});
  assert.match(calls.at(-1).url,/offset=30/);
  state.aboutTab = "portrait";
  await vm.runInContext('generateAbout("memory")',sandbox);
  assert.ok(calls.some(c => c.url === "/api/about/generate" && c.data.kind === "memory"));
  // No ask() stub exists: automatic mode must not demand repeated selection or confirmation.
  state.about.growth.enabled = false;
  vm.runInContext("renderAbout()",sandbox);
  assert.match(root.innerHTML,/开启自动积累/);
  const scopeHTML = vm.runInContext('portraitScopeHTML({selection_version:"memory-selection.v1", included:24, available:40, source_count:18, decisions:3, long_term:5, period_start:"2018", period_end:"<img onerror=alert(1)>"})', sandbox);
  assert.match(scopeHTML,/这次画像参考了哪些线索/);
  assert.match(scopeHTML,/24 \/ 40/);
  assert.match(scopeHTML,/未选入的记忆仍完整保留/);
  assert.doesNotMatch(scopeHTML,/<img/);
  console.log("Growth progress, controls, local timeline, escaping and automatic update contracts passed.");
})().catch(error=>{console.error(error);process.exitCode=1;});
