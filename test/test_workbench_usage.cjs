const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { test } = require("node:test");
const vm = require("node:vm");

const source = readFileSync(`${__dirname}/../src/workbench_web/app.js`, "utf8");
const functions = ["requestUsage", "usageCostLabel", "usagePricingTable", "refreshUsageElement", "openCostDialog"]
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

test("missing prices and usage never appear as a zero dollar estimate", () => {
  const context = vm.createContext({});
  vm.runInContext(functions, context);
  assert.equal(context.usageCostLabel({ estimatedCost: null, invocations: 1 }), "Cost unavailable");
  assert.equal(context.usageCostLabel({ estimatedCost: null, invocations: 0 }), "No recorded usage");
  assert.equal(context.usageCostLabel({ estimatedCost: .001, invocations: 1 }), "<$0.01");
  assert.equal(context.usageCostLabel({ estimatedCost: 1.5, missingUsage: 2 }), "≈$1.50 (partial)");
  assert.equal(context.usageCostLabel({ estimatedCost: 1.5, unpriced: 1 }), "≈$1.50 (partial)");
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

test("Cost opens a read-only dialog using the selection without clearing it", () => {
  const selection = new Map([["paper", { kind: "paper", path: "/paper" }]]);
  const context = vm.createContext({
    state: { selection }, dialogEyebrow: {}, dialogTitle: {},
    dialogBody: { replaceChildren(...children) { this.children = children; } },
    dialogFooter: { replaceChildren(...children) { this.children = children; } },
    node: (tag, className, text) => ({ tag, text }),
    usageSummary: (targets, label) => ({ targets, label }),
    button: label => ({ label }), showTaskDialog() {},
  });
  vm.runInContext(functions, context);
  context.openCostDialog([...selection.values()]);
  assert.equal(context.state.dialog.kind, "cost");
  assert.equal(selection.size, 1);
  assert.equal(context.dialogBody.children[1].targets[0].path, "/paper");
  assert.deepEqual(Array.from(context.dialogFooter.children, child => child.label), ["Refresh", "Close"]);
});
