const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { test } = require("node:test");
const vm = require("node:vm");

const source = readFileSync(`${__dirname}/../src/workbench_web/app.js`, "utf8");
const functions = [
  "latestJobRuns", "retryJob",
  "separateWriteTasks", "taskRequests", "taskRequestOptions", "taskTargetsForRequest",
  "targetKey", "targetCountLabel", "reviewTask", "confirmTask", "collectDialogOptions",
].map(name => source.match(new RegExp(`^(?:async )?function ${name}\\([^]*?^}`, "m"))[0]).join("\n");

test("bulk retry confirms latest failed/partial runs and selects the new task", async () => {
  const control = { disabled: false };
  const requests = [];
  let confirmation;
  let refreshed = false;
  const context = vm.createContext({
    state: { selectedJob: "original" },
    window: { confirm(message) { confirmation = message; return true; } },
    api: async (path, options) => {
      assert.equal(control.disabled, true);
      requests.push({ path, options });
      return { id: "retried" };
    },
    refreshJobs: async () => { refreshed = true; },
    showNotice: message => assert.fail(message),
  });
  vm.runInContext(functions, context);
  await context.retryJob({ id: "original", runs: [
    { id: "old", unit_index: 0, created_at: 1, status: "failed" },
    { id: "recovered", unit_index: 0, created_at: 2, status: "succeeded" },
    { id: "failed", unit_index: 1, created_at: 1, status: "failed" },
    { id: "partial", unit_index: 2, created_at: 1, status: "partial" },
    { id: "canceled", unit_index: 3, created_at: 1, status: "canceled" },
  ] }, control);
  assert.match(confirmation, /2 failed\/partial runs/);
  assert.match(confirmation, /may repeat completed work/);
  assert.equal(requests.length, 1);
  assert.equal(requests[0].path, "/api/jobs/original/retry");
  assert.equal(requests[0].options.method, "POST");
  assert.equal(context.state.selectedJob, "retried");
  assert.equal(refreshed, true);
  assert.equal(control.disabled, false);
});

test("bulk retry cancellation makes no request", async () => {
  const context = vm.createContext({
    window: { confirm: () => false },
    api: () => assert.fail("canceled retry must not submit"),
  });
  vm.runInContext(functions, context);
  const control = { disabled: false };
  await context.retryJob({ id: "original", runs: [] }, control);
  assert.equal(control.disabled, false);
});

function harness(options = {}, targets = [
  { kind: "problem", path: "/paper/OP-001" },
  { kind: "problem", path: "/paper/OP-002" },
]) {
  const requests = [];
  const task = { action: "write", options, targets };
  const context = vm.createContext({
    state: { dialog: task, selection: new Map(targets.map(t => [`${t.kind}:${t.path}`, t])) },
    dialogFooter: { querySelectorAll: () => [], querySelector: () => null },
    dialog: { setAttribute() {}, removeAttribute() {}, close() {} },
    sessionStorage: { removeItem() {} },
    saveDialogOptions() {}, renderTaskConfirmation() {}, renderSelectionBar() {},
    renderTaskConfiguration(message) { context.error = message; },
    refreshJobs: async () => {}, setTab() {},
    problemTargetForAttempt: t => ({ kind: "problem", path: t.path.replace(/\/attempt-\d+$/, "") }),
    api: async (path, { body }) => {
      requests.push({ path, body });
      return { id: String(requests.length), units: [{ targets: body.targets }], warnings: [] };
    },
  });
  vm.runInContext(functions, context);
  return { context, task, requests };
}

test("separate writing previews and starts one independent job per selected item", async () => {
  const { context, task, requests } = harness({ prompt: "Explain clearly", name: "shared-name" });
  await context.reviewTask();
  assert.equal(requests.length, 2);
  requests.forEach(({ path, body }, index) => {
    assert.equal(path, "/api/plans");
    assert.equal(body.action, "write");
    assert.deepEqual(Array.from(body.targets), [task.targets[index]]);
    assert.equal(body.options.prompt, "Explain clearly");
    assert.equal(body.options.name, undefined);
    assert.equal(body.options.writeMode, undefined);
  });
  assert.equal(task.plan.title, "Write 2 separate papers");
  assert.equal(task.plan.units.length, 2);
  await context.confirmTask();
  assert.deepEqual(requests.slice(2).map(r => [r.path, r.body.planId]), [
    ["/api/jobs", "1"], ["/api/jobs", "2"],
  ]);
  assert.equal(context.state.dialog, null);
});

test("combined writing keeps all selections in a single job", async () => {
  const { context, task, requests } = harness({ writeMode: "combined", name: "combined" });
  await context.reviewTask();
  assert.equal(requests.length, 1);
  assert.deepEqual(Array.from(requests[0].body.targets), task.targets);
  assert.equal(requests[0].body.options.name, "combined");
  assert.equal(requests[0].body.options.writeMode, undefined);
  await context.confirmTask();
  assert.equal(requests.length, 2);
});

test("separate writing preserves each selection when attempts resolve to the same problem", () => {
  const targets = [1, 2].map(n => ({ kind: "attempt", path: `/paper/OP-001/attempt-00${n}` }));
  const { context, task } = harness({}, targets);
  const requests = context.taskRequests(task);
  assert.equal(requests.length, 2);
  assert.equal(requests[0].targets[0].path, "/paper/OP-001");
  assert.equal(requests[1].targets[0].path, "/paper/OP-001");
  task.options.pinAttempts = true;
  context.taskRequests(task).forEach((request, index) => {
    assert.equal(request.targets[0], targets[index]);
  });
});

test("a single selection keeps the normal Write configuration", () => {
  const { context, task } = harness({ name: "single" }, [{ kind: "paper", path: "/paper" }]);
  assert.equal(context.separateWriteTasks(task), false);
  assert.equal(context.taskRequests(task)[0].options.name, "single");
});

test("retry after partial submission includes only selections whose jobs did not start", async () => {
  const { context, task, requests } = harness();
  const remaining = task.targets[1];
  await context.reviewTask();
  const api = context.api;
  context.api = (path, request) => {
    if (path === "/api/jobs" && request.body.planId === "2") throw new Error("Unavailable");
    return api(path, request);
  };
  await context.confirmTask();
  assert.match(context.error, /1 Write job started/);
  assert.deepEqual(task.targets, [remaining]);
  assert.equal(context.state.selection.size, 1);
  context.api = api;
  await context.reviewTask();
  assert.deepEqual(Array.from(requests.at(-1).body.targets), [remaining]);
  await context.confirmTask();
  assert.equal(requests.filter(r => r.path === "/api/jobs").length, 2);
});

test("only the checked writing mode is collected", () => {
  const { context } = harness();
  context.dialogBody = { querySelectorAll: () => [
    { name: "writeMode", type: "radio", value: "separate", checked: true },
    { name: "writeMode", type: "radio", value: "combined", checked: false },
  ] };
  assert.equal(context.collectDialogOptions().writeMode, "separate");
});
