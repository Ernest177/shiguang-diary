"use strict";
// Synthetic rendering contracts; not browser layout verification.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const nodes = new Map();
const oldSummary = {
  dataset: { summaryDay: "2026-10-20" },
  querySelectorAll: () => [{ dataset: { summaryDisclosure: "sources" } }],
};
function node(selector) {
  if (selector === '[data-summary-scope="panel"]') return oldSummary;
  if (!nodes.has(selector)) nodes.set(selector, { dataset: {}, addEventListener() {}, querySelectorAll: () => [] });
  return nodes.get(selector);
}
const context = vm.createContext({
  Intl, Date, Set, Map, crypto: { randomUUID: () => "synthetic-id" },
  document: { querySelector: node, querySelectorAll: () => [], addEventListener() {} },
});
const source = fs.readFileSync(path.join(__dirname, "../web/app.js"), "utf8");
vm.runInContext(source.slice(0, source.lastIndexOf("\ninit();")), context);
context.summary = { updated_at: "2026-10-20T00:00:00+00:00", model: "合成数据", stale: false, data: {
  overview: "整理了学习方法与本周计划。", sections: [
    { category: "insights", items: Array.from({ length: 5 }, (_, i) => ({
      text: `合成发现 ${i + 1}`, sources: [{ entry_id: "entry-a", quote: i < 2 ? "相同引用" : `引用 ${i + 1}` }],
    })) },
    { category: "actions", items: Array.from({ length: 4 }, (_, i) => ({
      text: `合成行动 ${i + 1}`, sources: [{ entry_id: "entry-b", quote: "<script>合成原文</script>" }],
    })) },
  ],
} };
vm.runInContext('state.day = "2026-10-20";', context);
let html = vm.runInContext("summaryHTML(summary)", context);
const insightSection = html.slice(html.indexOf('class="summary-section"'), html.indexOf("</section>"));
const visibleInsights = insightSection.slice(0, insightSection.indexOf("<details"));
assert.equal((visibleInsights.match(/<li>/g) || []).length, 2);
assert.match(insightSection, /其余 3 条/);
assert.match(insightSection, /合成发现 5/);
assert.match(html, /合成行动 4/, "action content is not truncated");
assert.equal((html.match(/查看原文依据/g) || []).length, 1);
assert.equal((html.match(/data-source="entry-a"/g) || []).length, 1, "one link per originating entry");
assert.equal((html.match(/<blockquote>相同引用<\/blockquote>/g) || []).length, 1, "identical quotations are deduplicated");
assert.match(html, /data-summary-disclosure="sources" open/);
assert.match(html, /&lt;script&gt;合成原文&lt;\/script&gt;/);
assert.ok(!html.includes("<script>合成原文"));
html = vm.runInContext('summaryHTML(summary, "review")', context);
assert.ok(!html.includes('data-summary-disclosure="sources" open'), "panel and review disclosures are independent");
vm.runInContext('state.day = "2026-10-21";', context);
assert.ok(!vm.runInContext("summaryHTML(summary)", context).includes('data-summary-disclosure="sources" open'));
context.summary.data.sections[0].items = context.summary.data.sections[0].items.slice(0, 1);
assert.ok(!vm.runInContext("summaryHTML(summary)", context).includes('class="summary-more"'));
context.summary.data.presentation = { kind: "original", local: true, records: [{ entry_id: "entry-a", body: "<script>合成原文</script>" }] };
html = vm.runInContext('summaryHTML(summary, "review")', context);
assert.match(html, /保留你的原话/);
assert.match(html, /没有额外请求 AI/);
assert.match(html, /&lt;script&gt;合成原文&lt;\/script&gt;/);
assert.ok(!html.includes("值得留住的发现"));
html = vm.runInContext('summaryHTML(summary, "panel")', context);
assert.match(html, /保留原文即可/);
assert.match(html, /仍想梳理一下/);
assert.ok(!html.includes("合成原文"), "the side panel does not duplicate the diary body");
html = vm.runInContext('organizationSettingsHTML({})', context);
assert.match(html, /自动判断/);
assert.match(html, /value="88"/);
assert.match(html, /name="brief_entry_chars"/);
console.log("Concise insights, retained actions, unified evidence, escaping and refresh contracts passed.");
