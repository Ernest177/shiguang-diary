"use strict";
// Synthetic rendering/interaction contracts; not a replacement for browser QA.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const nodes = new Map();
const expanded = [{ dataset: { actionSources: "task-1" } }];
function node(selector) {
  if (!nodes.has(selector)) nodes.set(selector, {
    innerHTML: "", value: "", dataset: {}, open: false,
    addEventListener() {}, close() { this.open = false; }, showModal() { this.open = true; },
    querySelectorAll: (q) => q === ".action-sources[open]" ? expanded : [],
  });
  return nodes.get(selector);
}
const context = vm.createContext({
  console, Intl, Date, Set,
  crypto: { randomUUID: () => "synthetic-id" },
  document: { querySelector: node, querySelectorAll: () => [], addEventListener() {} },
  localStorage: { getItem: () => null },
  setTimeout: () => 0, clearTimeout() {},
  FormData: class { constructor(form) { this.form = form; } get(key) { return this.form[key]; } },
});
const script = fs.readFileSync(path.join(__dirname, "../web/app.js"), "utf8");
vm.runInContext(script.slice(0, script.lastIndexOf("\ninit();")), context);
vm.runInContext(`
  state.today = "2026-10-20";
  state.day = "2026-10-20";
  state.settings.ai_mode = "local";
`, context);
context.action = {
  id: "task-1", title: "整理项目思路", kind: "task", status: "open",
  due_date: "2026-10-22", due_text: "2026-10-22",
  sources: [{ entry_id: "first", journal_day: "2026-10-20", quote: "花两天时间整理项目思路" },
            { entry_id: "second", journal_day: "2026-10-19", quote: "<script>合成的引用</script>" }],
};
const card = () => vm.runInContext("actionCard(action)", context);
const visibleText = (html) => html.replace(/<[^>]*>/g, "");

let html = card();
assert.equal(visibleText(html).split("2026-10-22").length - 1, 1, "legacy duplicate date is displayed once");
assert.match(html, /截止<\/span><time[^>]*>2026-10-22<\/time>/);
assert.match(html, /后天/);
const details = html.slice(html.indexOf("<details"), html.indexOf("</details>"));
assert.match(details, /记录于 2026-10-20/);
assert.match(details, /data-source="first" data-day="2026-10-20"/);
assert.match(details, /data-source="second" data-day="2026-10-19"/);
assert.match(details, /打开这条日记/);
assert.ok(!html.slice(0, html.indexOf("<details")).includes("2026-10-20"), "source dates stay out of deadline heading");
assert.ok(!html.includes("<script>"));

context.action.due_text = "花两天时间";
assert.match(card(), /时间原话：花两天时间/);
context.action.due_text = null;
assert.match(card(), /<time[^>]*>2026-10-22<\/time>/, "a manual date needs no original-language field");
context.action.due_date = null;
context.action.due_text = "晚点";
assert.match(card(), /时间待确认/);
assert.match(card(), /时间原话：晚点/);
context.action.due_text = null;
assert.match(card(), /未设截止日期/);
context.action.due_date = "2026-10-19";
assert.match(card(), /action-due overdue/);
assert.match(card(), /逾期 1 天/);
context.action.status = "done";
assert.ok(!card().includes("逾期"), "completed tasks aren't marked overdue");
context.action.status = "open";
context.action.due_date = "2026-10-22";
vm.runInContext("state.data = { actions: [action] }; renderActions();", context);
assert.match(node("#actions-view").innerHTML, /data-action-sources="task-1" open/,
  "periodic rendering preserves expanded evidence");
context.action.sources = [];
assert.match(card(), /原始记录已修改或删除/);
assert.ok(!card().includes("data-source="));

(async () => {
  vm.runInContext(`
    let submitted;
    changeAction = async (id, patch) => { submitted = { id, patch }; };
    showModal = () => {};
    editAction("task-1");
  `, context);
  await node("#action-edit-form").onsubmit({ preventDefault() {}, target: {
    title: "整理项目思路", kind: "task", due_date: "2026-10-25",
  } });
  const submitted = vm.runInContext("submitted", context);
  assert.equal(submitted.patch.due_date, "2026-10-25");
  assert.ok(!("due_text" in submitted.patch), "manual date must not fabricate a time quotation");
  console.log("Action deadline, evidence placement, manual editing and refresh contracts passed.");
})().catch((error) => { console.error(error); process.exitCode = 1; });
