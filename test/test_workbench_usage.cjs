const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { test } = require("node:test");
const vm = require("node:vm");

const source = readFileSync(`${__dirname}/../src/workbench_web/app.js`, "utf8");
const functions = ["formatDuration", "requestUsage", "usageCostLabel", "usageElapsedLabel", "usageTable", "usagePricingTable", "setUsageModelIncluded", "usageRunFilter", "refreshUsageFilters", "refreshUsageElement", "openCostDialog"]
  .map(name => source.match(new RegExp(`^(?:async )?function ${name}\\([^]*?^}`, "m"))[0]).join("\n");

test("usage queries share in-flight requests, reuse cached totals, and can be refreshed", async () => {
  let finish;
  let requests = 0;
  const context = vm.createContext({
    state: { usageCache: new Map(), usageLoads: new Map() },
    api: async (path, request) => {
      assert.equal(path, "/api/usage");
      assert.equal(request.body.targets[0].kind, "problem");
      requests++;
      return new Promise(resolve => { finish = resolve; });
    },
  });
  vm.runInContext(functions, context);
  const targets = [{ kind: "problem", path: "/paper/OP-001" }];
  const first = context.requestUsage(targets);
  const second = context.requestUsage(targets);
  assert.equal(requests, 1);
  finish({ estimatedCost: 1.5 });
  assert.equal((await first).estimatedCost, 1.5);
  await second;
  assert.equal((await context.requestUsage(targets)).estimatedCost, 1.5);
  assert.equal(requests, 1);
  const refreshed = context.requestUsage(targets, true);
  finish({ estimatedCost: 2 });
  assert.equal((await refreshed).estimatedCost, 2);
  assert.equal(requests, 2);
});

test("failed usage requests release their slot for retry", async () => {
  let requests = 0;
  const context = vm.createContext({
    state: { usageCache: new Map(), usageLoads: new Map() },
    api: async () => { if (++requests === 1) throw new Error("Offline"); return { invocations: 2 }; },
  });
  vm.runInContext(functions, context);
  await assert.rejects(context.requestUsage([]), /Offline/);
  assert.equal(context.state.usageLoads.size, 0);
  assert.equal((await context.requestUsage([])).invocations, 2);
});

test("model filters use separate canonical cache entries from unfiltered page totals", async () => {
  const requests = [];
  const context = vm.createContext({
    state: { usageCache: new Map(), usageLoads: new Map() },
    api: async (_path, request) => {
      requests.push(JSON.parse(JSON.stringify(request.body)));
      return { estimatedCost: request.body.excludeModels ? 1 : 3 };
    },
  });
  vm.runInContext(functions, context);
  const targets = [{ kind: "paper", path: "/paper" }];
  assert.equal((await context.requestUsage(targets)).estimatedCost, 3);
  assert.equal((await context.requestUsage(targets, false, new Set(["b", null, "a"]))).estimatedCost, 1);
  assert.equal((await context.requestUsage(targets, false, ["a", "b", null])).estimatedCost, 1);
  assert.equal((await context.requestUsage(targets)).estimatedCost, 3);
  assert.equal(requests.length, 2);
  assert.deepEqual(requests[1].excludeModels, [null, "a", "b"]);
});

test("model checkbox changes update all popup scopes while preserving page totals", () => {
  const modelFilter = { excludedModels: new Set() };
  const problem = { usageModelFilter: modelFilter, usageValue: {} };
  const attempt = { usageModelFilter: modelFilter, usageValue: {} };
  const pageButton = { usageValue: { estimatedCost: 3 } };
  const calls = [];
  const context = vm.createContext({
    document: { querySelectorAll: () => [problem, attempt, pageButton] },
  });
  vm.runInContext(functions, context);
  context.refreshUsageElement = element => calls.push(element);
  context.setUsageModelIncluded(modelFilter, "gpt-6.1-sol", false);
  assert.equal(modelFilter.excludedModels.has("gpt-6.1-sol"), true);
  assert.deepEqual(calls, [problem, attempt]);
  assert.equal(problem.usageValue, null);
  assert.equal(attempt.usageValue, null);
  assert.equal(pageButton.usageValue.estimatedCost, 3);
  context.setUsageModelIncluded(modelFilter, "gpt-6.1-sol", true);
  assert.equal(modelFilter.excludedModels.size, 0);
});

test("successful-run queries compose with model filters and keep page cache totals separate", async () => {
  const requests = [];
  const context = vm.createContext({
    state: { usageCache: new Map(), usageLoads: new Map() },
    api: async (_path, { body }) => {
      requests.push(JSON.parse(JSON.stringify(body)));
      return { estimatedCost: body.successfulRunsOnly ? 1 : 3 };
    },
  });
  vm.runInContext(functions, context);
  const targets = [{ kind: "problem", path: "/problem" }];
  await context.requestUsage(targets);
  await context.requestUsage(targets, false, [], true);
  await context.requestUsage(targets, false, ["b", "a"], true);
  await context.requestUsage(targets, false, ["a", "b"], true);
  assert.equal((await context.requestUsage(targets)).estimatedCost, 3);
  assert.equal(requests.length, 3);
  assert.equal(requests[1].successfulRunsOnly, true);
  assert.deepEqual(requests[2].excludeModels, ["a", "b"]);
  assert.equal(requests[2].successfulRunsOnly, true);
  assert.equal(requests[0].successfulRunsOnly, undefined);
});

test("successful-run checkbox synchronizes all scopes while preserving model selections and page totals", () => {
  const filter = { excludedModels: new Set(["gpt-6-astra"]), successfulRunsOnly: false };
  const problem = { usageModelFilter: filter, usageValue: {} };
  const attempt = { usageModelFilter: filter, usageValue: {} };
  const page = { usageValue: { estimatedCost: 3 } };
  const calls = [];
  const context = vm.createContext({
    node: (tag, className, text) => ({
      tag, text, dataset: {}, children: [], handlers: {},
      append(...children) { this.children.push(...children); },
      addEventListener(event, handler) { this.handlers[event] = handler; },
    }),
    document: { querySelectorAll: () => [problem, attempt, page] },
  });
  vm.runInContext(functions, context);
  context.refreshUsageElement = element => calls.push(element);
  const label = context.usageRunFilter(filter);
  const input = label.children[0];
  assert.equal(label.children[1].text, "Successful runs only");
  assert.equal(input.checked, false);
  input.checked = true;
  input.handlers.change();
  assert.equal(filter.successfulRunsOnly, true);
  assert.deepEqual(calls, [problem, attempt]);
  assert.equal(page.usageValue.estimatedCost, 3);
  assert.ok(filter.excludedModels.has("gpt-6-astra"));
  assert.equal(context.usageRunFilter(filter).children[0].checked, true);
  input.checked = false;
  input.handlers.change();
  assert.equal(filter.successfulRunsOnly, false);
});

test("missing prices and usage never appear as a zero dollar estimate", () => {
  const context = vm.createContext({});
  vm.runInContext(functions, context);
  assert.equal(context.usageCostLabel({ estimatedCost: null, invocations: 1 }), "Cost unavailable");
  assert.equal(context.usageCostLabel({ estimatedCost: null, invocations: 0 }), "No recorded usage");
  assert.equal(context.usageCostLabel({ estimatedCost: .001, invocations: 1 }), "<$0.01");
  assert.equal(context.usageCostLabel({ estimatedCost: 1.5, missingUsage: 2 }), "≈$1.50 (partial)");
  assert.equal(context.usageCostLabel({ estimatedCost: 1.5, unpriced: 1 }), "≈$1.50 (partial)");
});

test("elapsed labels distinguish precise, fallback, live, partial, and missing timing", () => {
  const context = vm.createContext({});
  vm.runInContext(functions, context);
  assert.equal(context.usageElapsedLabel({ elapsedSeconds: 3661 }), "1h 1m 1s");
  assert.equal(context.usageElapsedLabel({ elapsedSeconds: 90, elapsedFallbackRuns: 1, runningElapsed: 1, missingElapsed: 2 }), "≈1m 30s+*");
  assert.equal(context.usageElapsedLabel({ elapsedSeconds: null }), "—");
  assert.equal(context.usageElapsedLabel({ elapsedSeconds: 0 }), "0s");
});

test("usage table shows an elapsed total and unassigned time without inventing token counts", () => {
  const context = vm.createContext({
    node: (tag, className, text) => ({ tag, text, children: [], append(...children) { this.children.push(...children); } }),
  });
  vm.runInContext(functions, context);
  const fields = ["input_tokens", "cached_input_tokens", "cache_write_input_tokens", "output_tokens", "reasoning_output_tokens"];
  const row = {
    invocations: 1, tokens: Object.fromEntries(fields.map(key => [key, 100])),
    reported: Object.fromEntries(fields.map(key => [key, 1])),
    missingUsage: 0, estimatedCost: 1, elapsedSeconds: 20, stage: "Solve",
  };
  const remainder = { ...row, stage: "Run time · stage unavailable", invocations: 0,
    reported: Object.fromEntries(fields.map(key => [key, 0])),
    timingOnly: true, elapsedSeconds: 80, elapsedFallbackRuns: 1 };
  const other = { ...remainder, stage: "Other run time", elapsedSeconds: 10 };
  const table = context.usageTable({ ...row, elapsedSeconds: 110, elapsedFallbackRuns: 1,
    stages: [remainder, row, other] }, "Total").children[0];
  const body = table.children[2].children;
  assert.equal(body.length, 2);
  assert.equal(body[0].children[0].text, "Solve");
  assert.equal(body[0].children[2].text, "20s");
  assert.equal(body[1].children[0].text, "Unknown");
  assert.equal(body[1].children[2].text, "≈1m 30s");
  assert.equal(body[1].children[1].text, "—");
  assert.ok(body[1].children.slice(3).every(cell => cell.text === "—"));
  assert.equal(table.children[3].children[0].children[2].text, "≈1m 50s");
});

test("pricing table preserves fractional rates and distinguishes unknown prices", () => {
  const context = vm.createContext({
    node: (tag, className, text) => ({ tag, text, children: [], append(...children) { this.children.push(...children); } }),
  });
  vm.runInContext(functions, context);
  const table = context.usagePricingTable([
    { model: "gpt-6.1-sol", tier: "ultrafast", rates: { input: 12, cachedInput: .6, cacheWrite: 15, output: 60 } },
    { model: "gpt-6-luna", tier: "standard", rates: { input: .1, cachedInput: .01, cacheWrite: .125, output: .5 } },
    { model: "unknown", tier: "standard", rates: null },
  ]).children[0];
  assert.match(table.children[0].text, /USD per 1M tokens/);
  const rows = table.children[2].children;
  const cells = row => Array.from(row.children, cell => cell.text);
  assert.deepEqual(cells(rows[0]), ["gpt-6.1-sol", "ultrafast", "$12.00", "$0.60", "$15.00", "$60.00"]);
  assert.deepEqual(cells(rows[1]), ["gpt-6-luna", "standard", "$0.10", "$0.01", "$0.125", "$0.50"]);
  assert.deepEqual(cells(rows[2]), ["unknown", "standard", "—", "—", "—", "—"]);
});

test("pricing checkboxes select a model across its tiers and include unknown models", () => {
  const context = vm.createContext({
    node: (tag, className, text) => ({
      tag, text, children: [], dataset: {}, handlers: {},
      append(...children) { this.children.push(...children); },
      replaceChildren(...children) { this.children = children; },
      setAttribute(name, value) { this[name] = value; },
      addEventListener(name, handler) { this.handlers[name] = handler; },
      classList: { toggle() {} },
    }),
    document: { querySelectorAll: () => [] },
  });
  vm.runInContext(functions, context);
  const filter = { excludedModels: new Set() };
  const pricing = [
    { model: "gpt-6.1-sol", tier: "standard", rates: null },
    { model: "gpt-6.1-sol", tier: "fast", rates: null },
    { model: null, tier: null, rates: null },
  ];
  const inputs = element => element.tag === "input" ? [element] : element.children.flatMap(inputs);
  let controls = inputs(context.usagePricingTable(pricing, filter));
  assert.equal(controls.length, 3);
  assert.ok(controls.every(input => input.checked));
  assert.match(controls[0]["aria-label"], /Include gpt-6.1-sol/);
  controls[0].checked = false;
  controls[0].handlers.change();
  controls = inputs(context.usagePricingTable(pricing, filter));
  assert.deepEqual(controls.map(input => input.checked), [false, false, true]);
  controls[2].checked = false;
  controls[2].handlers.change();
  assert.equal(filter.excludedModels.has(null), true);
  controls[1].checked = true;
  controls[1].handlers.change();
  assert.equal(filter.excludedModels.has("gpt-6.1-sol"), false);
});

test("late usage responses do not overwrite an abandoned detail panel", async () => {
  let finish;
  const element = {
    isConnected: true, dataset: { usageTargets: "[]" },
    replaceChildren() { throw new Error("Detached panel must not change"); },
  };
  const context = vm.createContext({ requestUsage: () => new Promise(resolve => { finish = resolve; }) });
  vm.runInContext(functions.replace(/^async function requestUsage\([^]*?^}\n/m, ""), context);
  const pending = context.refreshUsageElement(element);
  element.isConnected = false;
  finish({});
  await pending;
});

test("older filter responses cannot overwrite the latest model and successful-run selection", async () => {
  const finishes = [];
  const selections = [];
  const element = {
    isConnected: true, dataset: { usageTargets: "[]", usageDisplay: "button", usageLabel: "Test" },
    usageModelFilter: { excludedModels: new Set(), successfulRunsOnly: false },
  };
  const context = vm.createContext({
    requestUsage: (_targets, _force, excluded, successfulOnly) => {
      selections.push([[...excluded], successfulOnly]);
      return new Promise(resolve => finishes.push(resolve));
    },
  });
  vm.runInContext(functions.replace(/^async function requestUsage\([^]*?^}\n/m, ""), context);
  const first = context.refreshUsageElement(element);
  element.usageModelFilter.excludedModels.add("gpt-6.1-sol");
  const second = context.refreshUsageElement(element);
  element.usageModelFilter.successfulRunsOnly = true;
  const third = context.refreshUsageElement(element);
  finishes[2]({ estimatedCost: .5 });
  await third;
  finishes[1]({ estimatedCost: 1 });
  await second;
  finishes[0]({ estimatedCost: 3 });
  await first;
  assert.equal(element.textContent, "Cost: ≈$0.50");
  assert.deepEqual(selections, [[[], false], [["gpt-6.1-sol"], false], [["gpt-6.1-sol"], true]]);
});

test("Cost opens a read-only dialog using the selection without clearing it", () => {
  const selection = new Map([["paper", { kind: "paper", path: "/paper" }]]);
  const context = vm.createContext({
    state: { selection }, dialogEyebrow: {}, dialogTitle: {},
    dialogBody: { replaceChildren(...children) { this.children = children; } },
    dialogFooter: { replaceChildren(...children) { this.children = children; } },
    node: (tag, className, text) => ({ tag, text }),
    usageSummary: (targets, label, modelFilter) => ({ targets, label, modelFilter }),
    button: label => ({ label }), showTaskDialog() {},
  });
  vm.runInContext(functions, context);
  context.openCostDialog([...selection.values()]);
  assert.equal(context.state.dialog.kind, "cost");
  assert.equal(selection.size, 1);
  assert.equal(context.dialogBody.children[1].targets[0].path, "/paper");
  assert.deepEqual(Array.from(context.dialogFooter.children, child => child.label), ["Refresh", "Close"]);
  const firstFilter = context.state.dialog;
  assert.equal(context.dialogBody.children[1].modelFilter, firstFilter);
  firstFilter.excludedModels.add("gpt-6.1-sol");
  firstFilter.successfulRunsOnly = true;
  context.openCostDialog([...selection.values()]);
  assert.notEqual(context.state.dialog, firstFilter);
  assert.equal(context.state.dialog.excludedModels.size, 0);
  assert.equal(context.state.dialog.successfulRunsOnly, false);
});
