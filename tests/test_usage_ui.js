"use strict";
// Synthetic DOM contracts only; layout still needs an actual browser review.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const nodes = new Map();
function node(selector) {
  if (!nodes.has(selector)) nodes.set(selector, {
    innerHTML: "", value: "", open: false, dataset: {},
    addEventListener() {}, querySelectorAll: () => [],
  });
  return nodes.get(selector);
}
const sandbox = vm.createContext({
  console, Intl, Date, Set, URL,
  crypto: { randomUUID: () => "synthetic-id" },
  document: { querySelector: node, querySelectorAll: () => [], addEventListener() {} },
  localStorage: { getItem: () => null }, setTimeout: () => 0, clearTimeout() {},
  FormData: class { constructor(form) { this.form = form; } get(key) { return this.form[key]; } },
});
const app = fs.readFileSync(path.join(__dirname, "../web/app.js"), "utf8");
vm.runInContext(fs.readFileSync(path.join(__dirname, "../web/usage.js"), "utf8"), sandbox);
vm.runInContext(app.slice(0, app.lastIndexOf("\ninit();")), sandbox);
vm.runInContext(`state.today = "2026-10-20"; state.view = "usage"; state.usageMonth = "2026-10";`, sandbox);
const row = {
  id: "call-1", started_at: "2026-10-19T16:30:05.000+00:00", kind: "entry", target: "entry-1", source_day: "2026-10-20",
  model: "synthetic-model", returned_model: "synthetic-model", status: "success", input_tokens: 1000,
  output_tokens: 100, total_tokens: 1100, cached_tokens: 200, reasoning_tokens: 80, total_source: "provider",
  input_rate: "0.8", output_rate: "2.7", cached_rate: null, estimated_cost: "0.00107000",
  cost_basis: "standard_rates", error_code: null, usage_note: null, attempt: 2,
};
const unknown = { ...row, id: "call-2", kind: "portrait", target: "current", model: "<script>unsafe</script>",
  status: "error", error_code: "network_or_timeout", input_tokens: null, output_tokens: null,
  total_tokens: null, cached_tokens: null, reasoning_tokens: null, estimated_cost: null, attempt: 1 };
sandbox.data = {
  month: "2026-10", offset: 0, limit: 50, provider: "synthetic-provider", model: "synthetic-model", warning: "",
  summary: { requests: 2, input_tokens: 1000, output_tokens: 100, total_tokens: 1100, missing_input: 1,
    missing_output: 1, missing_total: 1, known_cost: "0.00107000", unknown_cost: 1 },
  price: { input_rate: "0.8", output_rate: "2.7", cached_rate: null, source: "user" }, records: [row, unknown],
};
vm.runInContext("state.usage = data; renderUsage();", sandbox);
let html = node("#usage-view").innerHTML;
assert.match(html, /1 次费用未知，未计入/);
assert.match(html, /¥0\.00107/);
assert.match(html, /2026-10-20/);
assert.match(html, /00:30:05/, "request time uses the selected local timezone");
assert.match(html, /缓存命中：200（已包含在输入中）/);
assert.match(html, /思考：80（已包含在输出中）/);
assert.match(html, /第 2 次尝试/);
assert.match(html, /data-source="entry-1" data-day="2026-10-20"/);
assert.match(html, /&lt;script&gt;unsafe&lt;\/script&gt;/);
assert.ok(!html.includes("<script>unsafe"));
assert.match(html, /升级前的历史用量无法追补/);
assert.match(html, /<colgroup>/);
assert.equal((html.match(/<th scope="col" class="usage-number">/g)||[]).length,4,'numeric headers use the same alignment as their data');
assert.equal(vm.runInContext("moneyText(null)", sandbox), "—");
assert.equal(vm.runInContext('moneyText("0")', sandbox), "¥0.00");
assert.equal(vm.runInContext('moneyText("0.00000001")', sandbox), "¥0.00000001");
assert.equal(vm.runInContext("tokenText(null)", sandbox), "—");
assert.equal(vm.runInContext("tokenText(0)", sandbox), "0");

sandbox.data = { ...sandbox.data, records: [], summary: { requests: 0, input_tokens: 0, output_tokens: 0,
  total_tokens: 0, missing_input: 0, missing_output: 0, missing_total: 0, known_cost: "0", unknown_cost: 0 } };
vm.runInContext("state.usage = data; renderUsage();", sandbox);
assert.match(node("#usage-view").innerHTML, /这里会自动收录之后的整理与连接测试请求/);

(async () => {
  sandbox.requests = [];
  vm.runInContext(`api = async (url, payload) => { requests.push({ url, payload }); return data; }; toast = () => {};`, sandbox);
  const button = { disabled: false };
  const form = { input_rate: "1.2", output_rate: "3.4", cached_rate: "", querySelector: () => button };
  await node("#usage-price-form").onsubmit({ preventDefault() {}, target: form });
  const write = sandbox.requests.find((request) => request.url === "/api/usage/price");
  assert.equal(write.payload.input_rate, "1.2");
  assert.equal(write.payload.cached_rate, "");
  assert.equal(write.payload.model, "synthetic-model");
  assert.equal(write.payload.provider, "synthetic-provider");
  assert.equal(button.disabled, false);
  vm.runInContext("state.usageOffset = 50", sandbox);
  node("#usage-all").onclick();
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(vm.runInContext("state.usageOffset", sandbox), 0);
  assert.ok(sandbox.requests.some((request) => request.url.includes("month=all&offset=0")));
  console.log("Usage totals, unknown values, prices, source links, escaping and filter contracts passed.");
})().catch((error) => { console.error(error); process.exitCode = 1; });
