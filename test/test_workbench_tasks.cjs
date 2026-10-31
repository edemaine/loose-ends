const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { test } = require("node:test");
const vm = require("node:vm");

const source = readFileSync(`${__dirname}/../src/workbench_web/app.js`, "utf8");
const functions = [
  "latestJobRuns", "retryJob", "codexItems", "refreshCodexLogs",
  "separateWriteTasks", "taskRequests", "taskRequestOptions", "taskTargetsForRequest",
  "targetKey", "targetCountLabel", "reviewTask", "confirmTask", "collectDialogOptions",
  "saveProblemEditor", "configuredMemoryLimit", "renderMemoryControl",
].map(name => source.match(new RegExp(`^(?:async )?function ${name}\\([^]*?^}`, "m"))[0]).join("\n");

test("task steps remember independent scroll positions only until the dialog closes", async () => {
  const element = () => ({
    children: [], scrollTop: 0,
    append(...children) { this.children.push(...children); },
    replaceChildren(...children) { this.children = children; this.scrollTop = 0; },
    querySelectorAll() { return []; },
    querySelector() { return null; },
  });
  const body = element();
  let open = false;
  let scrollTop = 900;
  // Like a hidden dialog, retain the old offset but ignore scroll writes while closed.
  Object.defineProperty(body, "scrollTop", {
    get: () => open ? scrollTop : 0,
    set: value => { if (open) scrollTop = value; },
  });
  const footer = element();
  const plan = { title: "Download", units: [], warnings: [], prompts: {}, priorityLevel: 0 };
  const context = vm.createContext({
    state: { settings: { paperRoots: [] } },
    dialogBody: body, dialogFooter: footer, dialogEyebrow: {}, dialogTitle: {},
    dialog: {
      showModal() { open = true; }, close() { open = false; },
      setAttribute() {}, removeAttribute() {},
    },
    sessionStorage: { getItem: () => null },
    dialogStorageKey: () => "draft", actionNames: { download: "Download" },
    node: element, field: element, checkbox: element, priorityOptions: () => [],
    button: (label, onClick) => ({ label, onClick }),
    renderTaskTargetChips() {}, saveDialogOptions() {},
    taskRequests: () => [{}], api: async () => plan,
    priorityMultiplier: () => "1×", confirmTask() {},
  });
  const renderers = [
    "showTaskDialog", "openTask", "beginTaskDialogStep", "renderTaskConfiguration", "renderTaskConfirmation", "reviewTask",
  ].map(name => source.match(new RegExp(`^(?:async )?function ${name}\\([^]*?^}`, "m"))[0]).join("\n");
  vm.runInContext(renderers, context);

  body.scrollTop = 900;
  context.openTask("download", []);
  assert.equal(body.scrollTop, 0);
  body.scrollTop = 240;
  context.renderTaskConfiguration();
  assert.equal(body.scrollTop, 240);
  await context.reviewTask();
  assert.equal(body.scrollTop, 0);

  body.scrollTop = 420;
  footer.children.find(button => button.label === "Back").onClick();
  assert.equal(body.scrollTop, 240);
  body.scrollTop = 360;
  await context.reviewTask();
  assert.equal(body.scrollTop, 420);
  footer.children.find(button => button.label === "Back").onClick();
  assert.equal(body.scrollTop, 360);

  context.dialog.close();
  context.openTask("download", []);
  assert.equal(body.scrollTop, 0);
  await context.reviewTask();
  assert.equal(body.scrollTop, 0);
});

test("memory control distinguishes unsupported-platform warnings from enforcement failures", () => {
  const warning = "Worker memory limits are unavailable on macOS; workers will run without a memory limit.";
  for (const mode of ["percent", "gb", "unlimited"]) {
    const context = vm.createContext({
      state: { settings: { memoryLimit: { mode, value: 50 }, memory: { available: false, warning } } },
      memoryLimitDraft: null, memoryLimitSaving: false,
      memoryMode: { querySelectorAll: () => [] },
      memoryValueLabel: {}, memoryStepper: {}, memoryValue: {},
      memoryDecrease: {}, memoryIncrease: {}, memoryStatus: {},
      document: { activeElement: null },
    });
    vm.runInContext(functions, context);
    context.renderMemoryControl(1);
    assert.equal(context.memoryStatus.textContent, `Warning: ${warning}`);

    context.state.settings.memoryLimitPending = true;
    context.state.settings.memory = { available: false, error: "delegation unavailable" };
    context.renderMemoryControl(1);
    assert.equal(context.memoryStatus.textContent,
      "Queue paused because the memory limit could not be enforced: delegation unavailable");
  }
});

test("scheduler changes preserve task defaults and configured paper roots", async () => {
  const schedulerFunctions = ["updateScheduler", "adjustWorkerLimit", "scheduleMemoryLimit"].map(name =>
    source.match(new RegExp(`^(?:async )?function ${name}\\([^]*?^}`, "m"))[0],
  ).join("\n");
  for (const change of ["queue", "workers", "memory"]) {
    const taskDefaults = { analyze: { model: "default-model", reasoningEffort: "high" } };
    const paperRoots = ["/papers"];
    let scheduled;
    const requests = [];
    const context = vm.createContext({
      state: { settings: { taskDefaults, paperRoots, projectRoot: "/project", workerLimit: 2 } },
      workerLimitDraft: null, workerLimitSaving: false, workerLimitTimer: null,
      memoryLimitDraft: null, memoryLimitSaving: false, memoryLimitTimer: null,
      renderSchedulerControl() {}, showNotice: message => assert.fail(message),
      clearTimeout() {}, setTimeout: callback => { scheduled = callback; return 1; },
      api: async (path, { body }) => {
        assert.equal(path, "/api/scheduler");
        requests.push(JSON.parse(JSON.stringify(body)));
        return { workerLimit: 3, queuePaused: true, memoryLimit: { mode: "gb", value: 8 } };
      },
    });
    vm.runInContext(schedulerFunctions, context);
    if (change === "queue") await context.updateScheduler({ queuePaused: true });
    else {
      if (change === "workers") context.adjustWorkerLimit(1);
      else context.scheduleMemoryLimit({ mode: "gb", value: 8 });
      await scheduled();
    }
    assert.deepEqual(requests, [change === "queue" ? { queuePaused: true }
      : change === "workers" ? { workerLimit: 3 } : { memoryLimit: { mode: "gb", value: 8 } }]);
    assert.equal(context.state.settings.taskDefaults, taskDefaults);
    assert.equal(context.state.settings.paperRoots, paperRoots);
    assert.equal(context.state.settings.projectRoot, "/project");
    assert.equal(context.state.settings.workerLimit, 3);
    assert.equal(context.state.settings.queuePaused, true);
    assert.equal(context.state.settings.memoryLimit.value, 8);
  }
});

function problemEditorHarness(api, refreshCatalog = async () => {}) {
  const controls = [{ disabled: false }, { disabled: false, textContent: "Add problem" }];
  const attributes = new Map();
  const listeners = new Map();
  const statusNodes = [];
  const context = vm.createContext({
    state: { dialog: { kind: "open-problem", paper: { path: "/paper", title: "Paper", problemCount: 1 } } },
    dialogFooter: {
      querySelectorAll: () => controls,
      querySelector: () => controls[1],
    },
    dialogBody: {
      querySelectorAll: () => [
        { name: "title", value: "Question" }, { name: "statement", value: "What happens?" },
      ],
      querySelector: () => null,
      append: value => statusNodes.push(value),
      prepend: value => { context.error = value.textContent; },
    },
    dialog: {
      setAttribute: (key, value) => attributes.set(key, value),
      removeAttribute: key => attributes.delete(key),
      addEventListener: (key, value) => listeners.set(key, value),
      removeEventListener: key => listeners.delete(key),
      close: () => { context.closed = true; },
    },
    node: (_tag, _className, textContent) => ({
      textContent, setAttribute(key, value) { this[key] = value; },
      remove() { this.removed = true; },
    }),
    api, refreshCatalog,
    showNotice: (message, error) => { context.notices.push({ message, error }); },
    notices: [],
  });
  vm.runInContext(functions, context);
  return { context, controls, attributes, listeners, statusNodes };
}

test("adding a problem shows progress and submits once until Research is refreshed", async () => {
  let finishSave, finishRefresh, startRefresh;
  let requests = 0;
  const saved = new Promise(resolve => { finishSave = resolve; });
  const refreshed = new Promise(resolve => { finishRefresh = resolve; });
  const refreshStarted = new Promise(resolve => { startRefresh = resolve; });
  const { context, controls, attributes, listeners, statusNodes } = problemEditorHarness(
    () => { requests += 1; return saved; }, () => { startRefresh(); return refreshed; },
  );
  const pending = context.saveProblemEditor();
  await context.saveProblemEditor();
  assert.equal(requests, 1);
  assert.ok(controls.every(value => value.disabled));
  assert.equal(controls[1].textContent, "Adding problem…");
  assert.equal(attributes.get("aria-busy"), "true");
  assert.equal(statusNodes[0].role, "status");
  let prevented = false;
  listeners.get("cancel")({ preventDefault: () => { prevented = true; } });
  assert.equal(prevented, true);
  finishSave({ id: "OP-002" });
  await refreshStarted;
  assert.equal(controls[1].textContent, "Updating Research…");
  assert.equal(context.closed, undefined);
  finishRefresh();
  await pending;
  assert.equal(context.closed, true);
  assert.equal(context.state.dialog, null);
  assert.equal(attributes.has("aria-busy"), false);
  assert.equal(listeners.has("cancel"), false);
  assert.equal(statusNodes[0].removed, true);
  assert.match(context.notices[0].message, /OP-002 was added/);
});

test("failed problem save restores controls and retains entered text", async () => {
  const { context, controls, attributes, statusNodes } = problemEditorHarness(
    async () => { throw new Error("Title is required"); },
  );
  await context.saveProblemEditor();
  assert.equal(context.error, "Title is required");
  assert.equal(context.state.dialog.saving, false);
  assert.equal(context.closed, undefined);
  assert.ok(controls.every(value => !value.disabled));
  assert.equal(controls[1].textContent, "Add problem");
  assert.equal(attributes.has("aria-busy"), false);
  assert.equal(statusNodes[0].removed, true);
});

test("failed catalog fetch after saving does not invite another submission", async () => {
  const { context } = problemEditorHarness(
    async () => ({ id: "OP-002" }),
    async () => { throw new Error("Refresh failed"); },
  );
  await context.saveProblemEditor();
  assert.equal(context.closed, true);
  assert.equal(context.state.dialog, null);
  assert.equal(context.error, undefined);
  assert.equal(context.notices[0].error, true);
  assert.match(context.notices[1].message, /OP-002 was added/);
});

test("Codex transcript updates items and keeps repeated IDs in later turns", () => {
  const context = vm.createContext({});
  vm.runInContext(functions, context);
  const events = [
    { type: "turn.started" },
    { type: "item.started", item: { id: "item_0", type: "command_execution", status: "in_progress" } },
    { type: "item.completed", item: { id: "item_0", type: "command_execution", status: "completed", aggregated_output: "done" } },
    { type: "turn.started" },
    { type: "item.completed", item: { id: "item_0", type: "agent_message", text: "Hello" } },
    { type: "turn.failed", error: { message: "failure" } },
  ].map(value => JSON.stringify(value)).join("\n");
  const items = context.codexItems(events + "\n--- repair ---\nnull\n{");
  assert.equal(items.length, 3);
  assert.equal(items[0].aggregated_output, "done");
  assert.equal(items[1].text, "Hello");
  assert.equal(items[2].text, "failure");
});

test("shared transcript loader preserves report queries and pages both sources", async () => {
  for (const source of ["/api/runs/run-id/codex", "/api/review-codex?key=attempt&version=2"]) {
    const requests = [];
    const pages = [
      { text: "first\n", nextOffset: 6, complete: false },
      { text: "second\n", nextOffset: 13, complete: true },
    ];
    const context = vm.createContext({
      URLSearchParams,
      state: { codexLogs: new Map(), codexLoads: new Set(), codexTimers: new Map() },
      main: { querySelectorAll: () => [] },
      clearTimeout() {}, setTimeout() { return 1; },
      api: async url => {
        requests.push(url);
        if (url === source) return { complete: true, transcripts: [{ id: "saved-log", legacy: true }] };
        const query = new URL(url, "http://localhost").searchParams;
        if (source.includes("?")) assert.equal(query.get("key"), "attempt");
        if (query.get("kind") === "diagnostics") return { text: "", nextOffset: 0, complete: true };
        assert.equal(query.get("offset"), pages.length === 2 ? "0" : "6");
        return pages.shift();
      },
    });
    vm.runInContext(functions, context);
    const element = { dataset: { rendered: "true" }, isConnected: true };
    await context.refreshCodexLogs(source, element);
    await context.refreshCodexLogs(source, element);
    assert.equal(context.state.codexLogs.get(source)[0].events, "first\nsecond\n");
    assert.equal(pages.length, 0);
    assert.equal(requests.filter(url => url.includes("kind=prompt")).length, 0);
  }
});

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

test("editing existing Write settings reviews one fixed task and keeps pinned targets", async () => {
  const { context, task, requests } = harness({ model: "new-model", codexHome: "/accounts/work" }, [
    { kind: "attempt", path: "/paper/OP-001/attempt-001" },
    { kind: "problem", path: "/paper/OP-002" },
  ]);
  task.editJob = { id: "existing", runIds: ["pending"], settingsVersion: 4 };
  await context.reviewTask();
  assert.equal(requests.length, 1);
  assert.equal(requests[0].path, "/api/jobs/existing/edit-plan");
  assert.deepEqual(JSON.parse(JSON.stringify(requests[0].body)), {
    id: "existing", runIds: ["pending"], settingsVersion: 4,
    options: { model: "new-model", codexHome: "/accounts/work" },
  });
  assert.equal(context.separateWriteTasks(task), false);
  assert.equal(context.taskTargetsForRequest(task)[0].kind, "attempt");
});

test("saving a conflicting edit retains input and never starts a replacement task", async () => {
  const { context, task } = harness({ model: "new-model" });
  task.editJob = { id: "existing", runIds: ["pending"], settingsVersion: 0 };
  task.plan = { id: "reviewed-edit" };
  const editFunctions = ["confirmJobEdit", "shellQuote", "runCommandDisplay"].map(name =>
    source.match(new RegExp(`^(?:async )?function ${name}\\([^]*?^}`, "m"))[0],
  ).join("\n");
  vm.runInContext(editFunctions, context);
  context.api = async (path) => {
    assert.equal(path, "/api/jobs/existing/settings");
    throw new Error("Pending runs or settings changed; reopen Edit settings and review again.");
  };
  await context.confirmJobEdit();
  assert.equal(context.state.dialog, task);
  assert.equal(task.options.model, "new-model");
  assert.equal(task.plan, null);
  assert.match(context.error, /Pending runs or settings changed/);

  context.state.jobDetails = new Map();
  context.loadJob = async id => { context.loaded = id; };
  context.api = async () => ({ id: "existing" });
  task.plan = { id: "newly-reviewed-edit" };
  await context.confirmJobEdit();
  assert.equal(context.state.dialog, null);
  assert.equal(context.loaded, "existing");
  assert.equal(context.state.selection.size, 2);
  const job = { plan: { options: { codexHome: "/accounts/new" } } };
  assert.equal(context.runCommandDisplay(job, {
    argv: ["codex"], options: { codexHome: "/accounts/old account" },
  }), "CODEX_HOME='/accounts/old account' codex");
  assert.equal(context.runCommandDisplay(job, { argv: ["codex"], options: {} }), "codex");
});

function jobsHarness(api) {
  const timers = new Map();
  let nextTimer = 0;
  const context = vm.createContext({
    state: { jobs: [], tab: "activity", jobDetails: new Map(), selectedJob: "job" },
    jobsRefresh: null, jobsRefreshPending: false, jobsRefreshPreserveDetail: true,
    jobsRefreshTimer: null, navigationReady: true, api,
    clearTimeout: id => timers.delete(id),
    setTimeout: callback => { timers.set(++nextTimer, callback); return nextTimer; },
    updateActivityCount() {}, syncRelatedTasks() {}, rememberSidebarScroll() {}, restoreSidebarScroll() {},
    renderActivity: options => context.renders.push(options.preserveDetail), renders: [],
    currentUrl: () => "/activity", historyPayload: () => ({}), history: { replaceState() {} },
    window: { scrollY: 0 }, document: { hidden: false }, renderedUrl: "",
    main: { querySelectorAll: () => [] },
    showNotice: message => context.notices.push(message), notices: [],
    refreshVisibleRunElapsed: job => context.elapsed.push(job.id), elapsed: [],
    refreshRunLog: id => context.logs.push(id), logs: [],
  });
  const names = ["refreshJobs", "fetchJobs", "scheduleJobsRefresh", "refreshLiveActivity", "taskScopeSummary"];
  vm.runInContext(names.map(name => source.match(
    new RegExp(`^(?:async )?function ${name}\\([^]*?^}`, "m"),
  )[0]).join("\n"), context);
  return { context, timers };
}

test("a burst of task events schedules one jobs request", async () => {
  let requests = 0;
  const { context, timers } = jobsHarness(async path => {
    assert.equal(path, "/api/jobs");
    requests += 1;
    return { jobs: [] };
  });
  for (let i = 0; i < 100; i++) context.scheduleJobsRefresh();
  assert.equal(timers.size, 1);
  [...timers.values()][0]();
  await context.jobsRefresh;
  assert.equal(requests, 1);
  assert.deepEqual(context.renders, [true]);
});

test("changes during a jobs fetch coalesce into one trailing fetch with no overlap", async () => {
  const finishes = [];
  let active = 0, maximum = 0;
  const { context } = jobsHarness(() => {
    active += 1;
    maximum = Math.max(maximum, active);
    return new Promise(resolve => finishes.push(value => { active -= 1; resolve(value); }));
  });
  const first = context.refreshJobs({ preserveActivityDetail: true });
  const pending = Array.from({ length: 50 }, () => context.refreshJobs({ preserveActivityDetail: true }));
  pending.push(context.refreshJobs());
  assert.equal(finishes.length, 1);
  finishes[0]({ jobs: [{ id: "old" }] });
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(finishes.length, 2);
  finishes[1]({ jobs: [{ id: "new" }] });
  await Promise.all([first, ...pending]);
  assert.equal(maximum, 1);
  assert.equal(finishes.length, 2);
  assert.equal(context.state.jobs[0].id, "new");
  assert.deepEqual(context.renders, [true, false]);
});

test("a failed jobs refresh releases the request so later changes can load", async () => {
  let requests = 0;
  const { context } = jobsHarness(async () => {
    if (++requests === 1) throw new Error("Unavailable");
    return { jobs: [{ id: "recovered" }] };
  });
  await assert.rejects(context.refreshJobs(), /Unavailable/);
  await context.refreshJobs();
  assert.equal(context.state.jobs[0].id, "recovered");
  assert.equal(requests, 2);
});

test("live elapsed times and displayed logs update without fetching jobs", () => {
  const { context } = jobsHarness(() => { throw new Error("Jobs must not be fetched"); });
  context.refreshVisibleUsage = () => {};
  context.state.jobDetails.set("job", { id: "job" });
  context.main.querySelectorAll = () => [{ dataset: { runLog: "run" } }];
  context.refreshLiveActivity();
  assert.deepEqual(context.elapsed, ["job"]);
  assert.deepEqual(context.logs, ["run"]);
  context.document.hidden = true;
  context.refreshLiveActivity();
  assert.equal(context.logs.length, 1);
  context.document.hidden = false;
  context.state.tab = "research";
  context.main.querySelectorAll = () => [];
  context.refreshLiveActivity();
  assert.equal(context.elapsed.length, 1);
});

test("compact job summaries retain run counts in their scope", () => {
  const { context } = jobsHarness();
  context.taskTargets = job => job.plan.targets;
  context.singlePaperProblemScope = () => "";
  context.targetCountLabel = () => "2 problems";
  context.targetPaperCount = () => 1;
  assert.equal(context.taskScopeSummary({
    plan: { targets: [{ kind: "problem" }, { kind: "problem" }] }, counts: { total: 3 },
  }), "2 problems · 1 paper · 3 runs");
});
