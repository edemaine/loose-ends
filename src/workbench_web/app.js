"use strict";

const reviewModel = window.LooseEndsReviewModel;
if (!reviewModel) throw new Error("Shared review model failed to load");

const state = {
  csrf: "",
  eventSequence: 0,
  catalog: { papers: [], reviews: [], manuscripts: [], counts: {} },
  jobs: [],
  settings: { workerLimit: 2, queuePaused: false },
  tab: "research",
  search: "",
  selectedReview: "",
  selectedProblem: "",
  researchFilters: reviewModel.createDefaultFilters(),
  researchFiltersOpen: false,
  revealSidebarSelection: false,
  sidebarScroll: { research: 0, papers: 0, manuscripts: 0, activity: 0 },
  paperSort: "alphabetical",
  selectedPaper: "",
  selectedManuscript: "",
  selectedDraft: "",
  selectedJob: "",
  detailTab: "attempt",
  detailCache: new Map(),
  selection: new Map(),
  jobDetails: new Map(),
  runLogs: new Map(),
  runLogLoads: new Map(),
  expandedRuns: new Set(),
  dialog: null,
};

const sidebar = document.getElementById("sidebar");
const main = document.getElementById("main");
const notice = document.getElementById("notice");
const selectionBar = document.getElementById("selection-bar");
const activityCount = document.getElementById("activity-count");
const connection = document.getElementById("connection");
const schedulerControl = document.getElementById("scheduler-control");
const workerSummary = document.getElementById("worker-summary");
const workerLimit = document.getElementById("worker-limit");
const workerStatus = document.getElementById("worker-status");
const workerApply = document.getElementById("worker-apply");
const queueToggle = document.getElementById("queue-toggle");
const dialog = document.getElementById("task-dialog");
const dialogBody = document.getElementById("dialog-body");
const dialogFooter = document.getElementById("dialog-footer");
const dialogTitle = document.getElementById("dialog-title");
const dialogEyebrow = document.getElementById("dialog-eyebrow");

const viewPaths = Object.freeze({
  research: "/research",
  papers: "/papers",
  manuscripts: "/manuscripts",
  activity: "/activity",
});
const tabUrlStoragePrefix = "loose-ends-workbench:tab-url:";
const initialPriorities = ["high", "medium"];
const pageScrollPositions = new Map();
const sidebarControlNodes = new Map();
let renderedUrl = "";
let scrollUpdateFrame = null;
let navigationReady = false;
let eventConnection = null;
let eventReconnectNeedsRefresh = false;
let sessionRefresh = null;
const priorityLevels = [
  [-3, "⅛×"],
  [-2, "¼×"],
  [-1, "½×"],
  [0, "1×"],
  [1, "2×"],
  [2, "4×"],
  [3, "8×"],
];

if ("scrollRestoration" in history) history.scrollRestoration = "manual";

let markdownRenderer = null;
try {
  markdownRenderer = reviewModel.createMarkdownRenderer(window);
} catch (error) {
  console.warn("Markdown rendering unavailable", error);
}

function tabFromPath(pathname) {
  return Object.entries(viewPaths).find(([, path]) => path === pathname)?.[0] || "research";
}

function rememberTabUrl(tab, url) {
  try {
    const value = new URL(url, location.origin);
    if (value.origin !== location.origin || value.pathname !== viewPaths[tab]) return;
    localStorage.setItem(
      `${tabUrlStoragePrefix}${tab}`,
      `${value.pathname}${value.search}${value.hash}`,
    );
  } catch (error) {
    console.warn("Unable to remember tab state", error);
  }
}

function rememberedTabUrl(tab) {
  try {
    const stored = localStorage.getItem(`${tabUrlStoragePrefix}${tab}`);
    if (!stored) return "";
    const value = new URL(stored, location.origin);
    if (value.origin !== location.origin || value.pathname !== viewPaths[tab]) return "";
    return `${value.pathname}${value.search}${value.hash}`;
  } catch (error) {
    console.warn("Unable to restore tab state", error);
    return "";
  }
}

function currentUrl() {
  const parameters = new URLSearchParams();
  if (state.search.trim()) parameters.set("q", state.search.trim());
  if (["research", "papers"].includes(state.tab) && state.paperSort !== "alphabetical") {
    parameters.set("sort", state.paperSort);
  }
  if (state.tab === "research") {
    reviewModel.filtersToSearchParams(parameters, state.researchFilters, initialPriorities);
    const item = state.catalog.reviews.find(value => value.itemKey === state.selectedReview);
    reviewModel.identityToSearchParams(parameters, item);
    if (item && state.detailTab && state.detailTab !== "attempt") parameters.set("detail", state.detailTab);
  } else if (state.tab === "papers") {
    const paper = state.catalog.papers.find(value => value.key === state.selectedPaper);
    if (paper) parameters.set("paper", paper.urlKey || paper.path);
  } else if (state.tab === "manuscripts") {
    const manuscript = state.catalog.manuscripts.find(value => value.key === state.selectedManuscript);
    if (manuscript) {
      parameters.set("manuscript", manuscript.urlKey || manuscript.path);
      const draft = manuscript.drafts.find(value => value.key === state.selectedDraft);
      if (draft && draft.key !== manuscript.latest.key) parameters.set("draft", draft.name);
    }
  } else if (state.tab === "activity" && state.selectedJob) {
    parameters.set("job", state.selectedJob);
  }
  const query = parameters.toString();
  return `${viewPaths[state.tab]}${query ? `?${query}` : ""}`;
}

function historyPayload(scrollY = window.scrollY) {
  return { looseEndsWorkbench: true, scrollY };
}

function scrollPositionKey(url) {
  const value = new URL(url, location.origin);
  if (value.pathname === viewPaths.research) value.searchParams.delete("detail");
  return `${value.pathname}${value.search}${value.hash}`;
}

function rememberCurrentScroll({ updateHistory = true } = {}) {
  if (!renderedUrl) return;
  const scrollY = window.scrollY;
  pageScrollPositions.set(scrollPositionKey(renderedUrl), scrollY);
  if (updateHistory && history.state?.looseEndsWorkbench) {
    history.replaceState(historyPayload(scrollY), "", renderedUrl);
  }
}

function restorePageScroll(url, preferredScroll) {
  const positionKey = scrollPositionKey(url);
  const scrollY = Number.isFinite(preferredScroll)
    ? preferredScroll
    : pageScrollPositions.get(positionKey) ?? 0;
  pageScrollPositions.set(positionKey, scrollY);
  requestAnimationFrame(() => window.scrollTo({ top: scrollY, left: 0, behavior: "auto" }));
}

function syncNavigation({ replace = false, preserveScroll = false } = {}) {
  if (!navigationReady) return;
  rememberCurrentScroll();
  render();
  const url = currentUrl();
  const scrollY = preserveScroll
    ? window.scrollY
    : pageScrollPositions.get(scrollPositionKey(url)) ?? 0;
  const method = replace || url === renderedUrl ? "replaceState" : "pushState";
  history[method](historyPayload(scrollY), "", url);
  renderedUrl = url;
  rememberTabUrl(state.tab, url);
  restorePageScroll(url, scrollY);
}

function applyLocation({ scrollY } = {}) {
  const parameters = new URLSearchParams(location.search);
  state.tab = tabFromPath(location.pathname);
  state.search = parameters.get("q") || "";
  state.paperSort = reviewModel.normalizePaperSort(parameters.get("sort"));
  if (state.tab === "research") {
    state.researchFilters = reviewModel.filtersFromSearchParams(parameters, initialPriorities);
    const identity = reviewModel.identityFromSearchParams(parameters);
    const legacy = decodeURIComponent(location.hash.slice(1));
    const requested = reviewModel.findReviewItem(state.catalog.reviews, identity) ||
      state.catalog.reviews.find(item => item.id === legacy || item.itemKey === legacy);
    state.selectedReview = requested?.itemKey || "";
    state.selectedProblem = requested?.problemKey || "";
    state.revealSidebarSelection = Boolean(requested);
    state.detailTab = parameters.get("detail") || "attempt";
  } else if (state.tab === "papers") {
    const requested = parameters.get("paper");
    const paper = state.catalog.papers.find(
      item => item.urlKey === requested || item.path === requested,
    );
    state.selectedPaper = paper?.key || "";
    state.revealSidebarSelection = Boolean(paper);
  } else if (state.tab === "manuscripts") {
    const requested = parameters.get("manuscript");
    const manuscript = state.catalog.manuscripts.find(
      item => item.urlKey === requested || item.path === requested,
    );
    state.selectedManuscript = manuscript?.key || "";
    state.revealSidebarSelection = Boolean(manuscript);
    const requestedDraft = parameters.get("draft");
    state.selectedDraft = manuscript?.drafts.find(
      draft => draft.name === requestedDraft || draft.urlKey === requestedDraft,
    )?.key || manuscript?.latest.key || "";
  } else {
    state.selectedJob = parameters.get("job") || "";
    state.revealSidebarSelection = state.jobs.some(
      job => job.id === state.selectedJob,
    );
  }
  render();
  const canonicalUrl = currentUrl();
  const restoredScroll = Number.isFinite(scrollY)
    ? scrollY
    : pageScrollPositions.get(scrollPositionKey(canonicalUrl)) ?? 0;
  history.replaceState(historyPayload(restoredScroll), "", canonicalUrl);
  renderedUrl = canonicalUrl;
  rememberTabUrl(state.tab, canonicalUrl);
  restorePageScroll(canonicalUrl, restoredScroll);
}

function node(tag, className = "", text = "") {
  const value = document.createElement(tag);
  if (className) value.className = className;
  if (text) value.textContent = text;
  return value;
}

function button(label, handler, className = "button") {
  const value = node("button", className, label);
  value.type = "button";
  value.addEventListener("click", handler);
  return value;
}

function badge(value, extra = "") {
  return node("span", `badge ${extra || value || "neutral"}`, reviewModel.titleize(value || "none"));
}

function humanize(value) {
  return reviewModel.titleize(value, "");
}

function formatTime(value) {
  return value ? new Date(value * 1000).toLocaleString() : "—";
}

function formatDuration(startedAt, finishedAt) {
  if (!Number.isFinite(startedAt) || !Number.isFinite(finishedAt)) return "";
  let seconds = Math.max(0, Math.round(finishedAt - startedAt));
  if (seconds < 1) return "<1s";
  const days = Math.floor(seconds / 86400);
  seconds %= 86400;
  const hours = Math.floor(seconds / 3600);
  seconds %= 3600;
  const minutes = Math.floor(seconds / 60);
  seconds %= 60;
  const pieces = [];
  if (days) pieces.push(`${days}d`);
  if (hours) pieces.push(`${hours}h`);
  if (minutes) pieces.push(`${minutes}m`);
  if (seconds || !pieces.length) pieces.push(`${seconds}s`);
  return pieces.join(" ");
}

function priorityMultiplier(level) {
  return priorityLevels.find(([value]) => value === Number(level))?.[1] || "1×";
}

function priorityOptions(defaultLabel = true) {
  return priorityLevels.map(([value, label]) => [
    String(value),
    `${label}${defaultLabel && value === 0 ? " (default)" : ""}`,
  ]);
}

function taskStatus(status) {
  const values = {
    queued: ["Queued", "queued", false],
    starting: ["Starting", "running", true],
    running: ["Running", "running", true],
    cancel_requested: ["Stopping", "running", true],
    succeeded: ["Finished", "succeeded", false],
    partial: ["Partial", "partial", false],
    failed: ["Failed", "failed", false],
    canceled: ["Canceled", "canceled", false],
    interrupted: ["Interrupted", "interrupted", false],
  };
  const [label, tone, active] = values[status] || [humanize(status), "neutral", false];
  return { label, tone, active };
}

function taskStatusBadge(status) {
  const value = taskStatus(status);
  return node("span", `badge ${value.tone}`, value.label);
}

function taskIsPaused(job) {
  return job.scheduling_paused && ![
    "succeeded", "partial", "failed", "canceled", "interrupted",
  ].includes(job.status);
}

function taskBadges(job) {
  const values = node("span", "task-badges");
  values.append(taskIsPaused(job) ? badge("Paused", "paused") : taskStatusBadge(job.status));
  if (!["succeeded", "partial", "failed", "canceled", "interrupted"].includes(job.status)) {
    values.append(badge(`Share ${priorityMultiplier(job.priority_level)}`, "neutral"));
  }
  return values;
}

function runTiming(run) {
  const status = taskStatus(run.status);
  const row = node("div", "run-timing");
  row.append(node(
    "span",
    "",
    run.started_at
      ? `Started ${formatTime(run.started_at)}`
      : `Created ${formatTime(run.created_at)}`,
  ));
  row.append(node("span", "run-timing-separator", "·"), taskStatusBadge(run.status));
  if (run.finished_at) {
    row.append(node("span", "", `at ${formatTime(run.finished_at)}`));
    const duration = formatDuration(run.started_at, run.finished_at);
    if (duration) row.append(node("span", "run-timing-separator", "·"), node("span", "", `Runtime ${duration}`));
    if (run.exit_code != null && run.exit_code !== 0) {
      row.append(node("span", "run-timing-separator", "·"), node("span", "", `Exit code ${run.exit_code}`));
    }
  }
  return row;
}

function runConsoleStatus(run) {
  const status = taskStatus(run.status);
  const footer = node("div", `console-status ${status.tone}${status.active ? " active" : ""}`);
  footer.setAttribute("role", "status");
  footer.setAttribute("aria-live", "polite");
  footer.append(node("span", "console-status-dot"), node("strong", "", status.label));
  if (run.finished_at) {
    const duration = formatDuration(run.started_at, run.finished_at);
    footer.append(node("span", "console-status-detail", `${formatTime(run.finished_at)}${duration ? ` · ${duration}` : ""}`));
  }
  if (status.active) {
    const cursor = node("span", "console-cursor");
    cursor.setAttribute("aria-hidden", "true");
    footer.append(cursor);
  }
  return footer;
}

function fileUrl(path) {
  return `/api/file?${new URLSearchParams({ path })}`;
}

function rawFileUrl(path) {
  return `/api/file?${new URLSearchParams({ path, raw: "1" })}`;
}

function artifactViewUrl(path) {
  return `/view?${new URLSearchParams({ path })}`;
}

async function refreshSession() {
  if (sessionRefresh) return sessionRefresh;
  sessionRefresh = (async () => {
    const value = await api("/api/bootstrap", {}, false);
    state.csrf = value.csrf;
    state.eventSequence = value.eventSequence || 0;
    state.catalog = value.catalog;
    state.jobs = value.jobs;
    state.settings = value.settings || state.settings;
    state.detailCache.clear();
    if (navigationReady) {
      syncNavigation({ replace: true, preserveScroll: true });
      connectEvents();
    }
    return value;
  })();
  try {
    return await sessionRefresh;
  } finally {
    sessionRefresh = null;
  }
}

async function api(path, options = {}, retryConfirmation = true) {
  const requestOptions = { ...options };
  const headers = { ...(options.headers || {}) };
  if (options.body && typeof options.body !== "string") {
    headers["Content-Type"] = "application/json";
    requestOptions.body = JSON.stringify(options.body);
  }
  const method = String(options.method || "GET").toUpperCase();
  if (method !== "GET") {
    headers["X-Workbench-CSRF"] = state.csrf;
  }
  const response = await fetch(path, { ...requestOptions, headers });
  const result = await response.json().catch(() => ({ error: response.statusText }));
  if (
    !response.ok &&
    retryConfirmation &&
    method !== "GET" &&
    response.status === 403 &&
    result.code === "invalid_confirmation_token"
  ) {
    await refreshSession();
    return api(path, options, false);
  }
  if (!response.ok) throw new Error(result.error || response.statusText);
  return result;
}

function showNotice(message, error = false) {
  notice.hidden = !message;
  notice.textContent = message || "";
  notice.className = `notice${error ? " error-box" : ""}`;
}

function catalogProgressNode(large = false) {
  const progress = state.catalog.progress || {};
  const wrapper = node("div", `catalog-progress${large ? " large" : ""}`);
  const line = node("div", "progress-line");
  line.append(node("strong", "", progress.label || "Loading the research catalog…"));
  if (Number.isFinite(progress.current) && Number.isFinite(progress.total)) {
    line.append(node("span", "", `${progress.current.toLocaleString()} / ${progress.total.toLocaleString()}`));
  }
  const track = node("div", "progress-track");
  const fill = node("div", "progress-fill");
  if (Number.isFinite(progress.current) && progress.total > 0) {
    fill.style.width = `${Math.min(100, 100 * progress.current / progress.total)}%`;
  } else {
    fill.classList.add("indeterminate");
  }
  track.append(fill);
  wrapper.append(line, track);
  return wrapper;
}

function renderCatalogLoading() {
  notice.hidden = false;
  notice.className = "notice loading-notice";
  notice.replaceChildren(catalogProgressNode());
}

function renderInitialLoading() {
  sidebar.replaceChildren();
  delete sidebar.dataset.controlsTab;
  const side = node("div", "loading-shell");
  side.append(node("div", "loading-pulse"), node("div", "loading-pulse short"));
  sidebar.append(side);
  const shell = node("section", "initial-loading panel");
  shell.append(
    node("div", "eyebrow", "Preparing workbench"),
    node("h1", "", "Loading your research catalog"),
    node("p", "", "The server is scanning papers, open problems, reviews, and manuscripts. You can leave this page open; it will update automatically."),
    catalogProgressNode(true),
  );
  main.replaceChildren(shell);
}

function target(kind, path, label) {
  return { kind, path, label };
}

function paperTarget(paper) {
  return target("paper", paper.path, paper.title);
}

function problemTarget(item) {
  return target("problem", `${item.paperDirectory}/${item.problemId}`, `${item.problemId}: ${item.problemTitle}`);
}

function attemptTarget(item) {
  const paperTitle = reviewModel.paperTitleWithYear(
    item.paperTitle,
    item.paperPublished,
  ) || item.paperDirectory;
  return target(
    "attempt",
    item.attemptDirectory,
    `${paperTitle} · ${item.problemId}/${item.attemptName}`,
  );
}

function draftTarget(draft) {
  return target("draft", draft.path, `${draft.name}: ${draft.title}`);
}

function targetKey(value) {
  return `${value.kind}:${value.path}`;
}

function toggleSelection(value, checked) {
  const key = targetKey(value);
  if (checked) state.selection.set(key, value);
  else state.selection.delete(key);
  syncSelectionControls();
  renderSelectionBar();
}

function selectionCheckbox(value) {
  const input = node("input");
  input.type = "checkbox";
  input.dataset.selectionKey = targetKey(value);
  input.checked = state.selection.has(input.dataset.selectionKey);
  input.setAttribute("aria-label", `Select ${value.label}`);
  input.addEventListener("click", event => event.stopPropagation());
  input.addEventListener("change", () => toggleSelection(value, input.checked));
  return input;
}

function visibleProblemTargets() {
  return reviewModel.latestProblems(filteredReviews()).map(problemTarget);
}

function updateVisibleProblemSelectionControl(input) {
  if (!input) return;
  const targets = visibleProblemTargets();
  const selected = targets.filter(value => state.selection.has(targetKey(value))).length;
  input.checked = targets.length > 0 && selected === targets.length;
  input.indeterminate = selected > 0 && selected < targets.length;
  input.disabled = targets.length === 0;
  input.setAttribute("aria-label", `${input.checked ? "Clear" : "Select"} all ${targets.length} visible problems`);
  const label = input.closest("label");
  label?.classList.toggle("disabled", input.disabled);
  const copy = label?.querySelector("span");
  if (copy) copy.textContent = `Select visible (${targets.length.toLocaleString()})`;
}

function syncSelectionControls() {
  document.querySelectorAll("input[data-selection-key]").forEach(input => {
    input.checked = state.selection.has(input.dataset.selectionKey);
  });
  updateVisibleProblemSelectionControl(
    document.querySelector("input[data-select-visible-problems]"),
  );
}

function visibleProblemSelectionControl() {
  const label = node("label", "select-visible");
  const input = node("input");
  input.type = "checkbox";
  input.dataset.selectVisibleProblems = "";
  input.addEventListener("click", event => event.stopPropagation());
  input.addEventListener("change", () => {
    visibleProblemTargets().forEach(value => {
      const key = targetKey(value);
      if (input.checked) state.selection.set(key, value);
      else state.selection.delete(key);
    });
    syncSelectionControls();
    renderSelectionBar();
  });
  label.addEventListener("click", event => event.stopPropagation());
  label.append(input, node("span"));
  updateVisibleProblemSelectionControl(input);
  return label;
}

function awaitingReviewAttemptsForTargets(values) {
  const paperPaths = new Set(
    values.filter(value => value.kind === "paper").map(value => value.path),
  );
  const problemKeys = new Set(
    values.filter(value => value.kind === "problem").map(targetKey),
  );
  const latest = new Map();
  state.catalog.reviews.forEach(item => {
    if (item.attemptStatus !== "unreviewed" || !item.attemptDirectory) return;
    if (
      !paperPaths.has(item.paperDirectory) &&
      !problemKeys.has(targetKey(problemTarget(item)))
    ) return;
    const previous = latest.get(item.problemKey);
    if (!previous || item.attemptNumber > previous.attemptNumber) {
      latest.set(item.problemKey, item);
    }
  });
  return [...latest.values()]
    .sort(reviewModel.compareProblems)
    .map(attemptTarget);
}

function appendAwaitingReviewAction(values) {
  const attempts = awaitingReviewAttemptsForTargets(values);
  if (!attempts.length) return;
  const action = button(
    `Review (${attempts.length.toLocaleString()})`,
    () => openTask("review", attempts),
  );
  action.title = `${attempts.length.toLocaleString()} awaiting-review attempt${attempts.length === 1 ? "" : "s"}`;
  selectionBar.append(action);
}

function renderSelectionBar() {
  selectionBar.replaceChildren();
  const values = [...state.selection.values()];
  selectionBar.hidden = !values.length;
  if (!values.length) return;
  selectionBar.append(node("strong", "", `${values.length} selected`));
  const kinds = new Set(values.map(item => item.kind));
  if ([...kinds].every(kind => kind === "paper")) {
    selectionBar.append(button("Analyze", () => openTask("analyze", values)));
    const problems = problemsForPapers(values);
    if (problems.length) {
      selectionBar.append(button("Triage", () => openTask("triage", problems)));
      selectionBar.append(button("Literature", () => openTask("literature", problems)));
      selectionBar.append(button("Solve", () => openTask("solve", problems), "button primary"));
    }
    appendAwaitingReviewAction(values);
  }
  if ([...kinds].every(kind => kind === "problem")) {
    selectionBar.append(button("Triage", () => openTask("triage", values)));
    selectionBar.append(button("Literature", () => openTask("literature", values)));
    selectionBar.append(button("Solve", () => openTask("solve", values), "button primary"));
    appendAwaitingReviewAction(values);
  }
  if ([...kinds].every(kind => kind === "attempt")) {
    selectionBar.append(button("Review", () => openTask("review", values)));
  }
  if ([...kinds].every(kind => ["paper", "problem", "attempt"].includes(kind))) {
    selectionBar.append(button("Write paper", () => openTask("write", values)));
  }
  selectionBar.append(button("Clear", () => {
    state.selection.clear();
    render();
  }));
}

function problemsForPapers(papers) {
  const paths = new Set(papers.map(value => value.path));
  const problems = new Map();
  state.catalog.reviews.forEach(item => {
    if (paths.has(item.paperDirectory)) {
      const value = problemTarget(item);
      problems.set(targetKey(value), value);
    }
  });
  return [...problems.values()];
}

function setTab(tab) {
  state.tab = tab;
  state.search = "";
  syncNavigation();
}

function restoreTab(tab) {
  const url = rememberedTabUrl(tab) || viewPaths[tab];
  if (url === renderedUrl) return;
  rememberCurrentScroll();
  const scrollY = pageScrollPositions.get(scrollPositionKey(url)) ?? 0;
  history.pushState(historyPayload(scrollY), "", url);
  applyLocation({ scrollY });
}

document.querySelectorAll("[data-tab]").forEach(value => {
  value.addEventListener("click", () => restoreTab(value.dataset.tab));
});

function render() {
  rememberSidebarScroll();
  const renderedResearchFilters = sidebar.querySelector(".research-filters");
  if (renderedResearchFilters) {
    state.researchFiltersOpen = renderedResearchFilters.open;
  }
  document.querySelectorAll("[data-tab]").forEach(value => {
    value.classList.toggle("active", value.dataset.tab === state.tab);
  });
  updateActivityCount();
  if (state.catalog.error) showNotice(`Catalog update delayed: ${state.catalog.error}`, true);
  else if (state.catalog.loading) renderCatalogLoading();
  else showNotice("");
  if (state.catalog.loading && !state.catalog.version) {
    renderInitialLoading();
    renderSelectionBar();
    return;
  }
  sidebar.classList.toggle("research-sidebar", state.tab === "research");
  if (state.tab === "research") renderResearch();
  else if (state.tab === "papers") renderPapers();
  else if (state.tab === "manuscripts") renderManuscripts();
  else renderActivity();
  restoreSidebarScroll(state.tab);
  renderSelectionBar();
}

function updateActivityCount() {
  const active = state.jobs.filter(job => ["queued", "running"].includes(job.status)).length;
  activityCount.textContent = active ? String(active) : "";
  renderSchedulerControl();
}

function schedulerCounts() {
  return state.jobs.reduce((counts, job) => {
    counts.active += Number(job.counts?.active) || 0;
    counts.queued += Number(job.counts?.queued) || 0;
    return counts;
  }, { active: 0, queued: 0 });
}

function renderSchedulerControl() {
  const limit = Number(state.settings.workerLimit) || 2;
  const counts = schedulerCounts();
  workerSummary.textContent = `${state.settings.queuePaused ? "Queue paused" : "Workers"} ${counts.active}/${limit}`;
  if (document.activeElement !== workerLimit) workerLimit.value = String(limit);
  queueToggle.textContent = state.settings.queuePaused ? "Resume queue" : "Pause queue";
  if (state.settings.queuePaused) {
    workerStatus.textContent = `${counts.active} active; ${counts.queued} waiting. Active runs continue.`;
  } else if (counts.active > limit) {
    workerStatus.textContent = `Draining ${counts.active} active runs to the new limit of ${limit}.`;
  } else {
    workerStatus.textContent = `${counts.active} active; ${counts.queued} waiting.`;
  }
}

async function updateScheduler(changes) {
  try {
    state.settings = await api("/api/scheduler", { method: "POST", body: changes });
    renderSchedulerControl();
  } catch (error) {
    showNotice(error.message, true);
  }
}

workerApply.addEventListener("click", () => updateScheduler({ workerLimit: Number(workerLimit.value) }));
queueToggle.addEventListener("click", () => updateScheduler({ queuePaused: !state.settings.queuePaused }));
document.addEventListener("pointerdown", event => {
  if (schedulerControl.open && !schedulerControl.contains(event.target)) {
    schedulerControl.open = false;
  }
});

function rememberSidebarScroll() {
  const renderedTab = sidebar.dataset.tab;
  if (!(renderedTab in state.sidebarScroll)) return;
  const scrollingElement = renderedTab === "research"
    ? sidebar.querySelector(".problem-scroll")
    : sidebar;
  if (scrollingElement) state.sidebarScroll[renderedTab] = scrollingElement.scrollTop;
}

function restoreSidebarScroll(tab) {
  sidebar.dataset.tab = tab;
  if (tab === "research") sidebar.scrollTop = 0;
  const scrollingElement = tab === "research"
    ? sidebar.querySelector(".problem-scroll")
    : sidebar;
  if (!scrollingElement) return;
  scrollingElement.scrollTop = state.sidebarScroll[tab] || 0;
  if (!state.revealSidebarSelection) return;
  state.revealSidebarSelection = false;
  const selected = scrollingElement.querySelector(".side-card.active");
  if (!selected) return;
  const viewport = scrollingElement.getBoundingClientRect();
  const card = selected.getBoundingClientRect();
  const centered = scrollingElement.scrollTop +
    (card.top + card.bottom - viewport.top - viewport.bottom) / 2;
  const maximum = scrollingElement.scrollHeight - scrollingElement.clientHeight;
  scrollingElement.scrollTop = Math.max(0, Math.min(maximum, centered));
  state.sidebarScroll[tab] = scrollingElement.scrollTop;
}

function sidebarSearch(placeholder) {
  const input = node("input", "search");
  input.type = "search";
  input.placeholder = placeholder;
  input.value = state.search;
  input.addEventListener("input", () => {
    state.search = input.value;
    syncNavigation({ replace: true, preserveScroll: true });
  });
  return input;
}

function paperSortControl() {
  const wrapper = node("label", "paper-sort-control");
  wrapper.append(node("span", "", "Sort papers"));
  const select = node("select");
  select.dataset.paperSort = "";
  select.title = "Most results uses the best result per problem: solutions and counterexamples 1.0, partial results and obstructions 0.1, reviewed incorrect results 0.";
  reviewModel.paperSortOptions.forEach(([value, label]) => {
    const option = node("option", "", label);
    option.value = value;
    option.selected = state.paperSort === value;
    select.append(option);
  });
  select.addEventListener("change", () => {
    state.paperSort = reviewModel.normalizePaperSort(select.value);
    syncNavigation({ replace: true, preserveScroll: true });
  });
  wrapper.append(select);
  return wrapper;
}

function attemptTagsNode(item, options = {}) {
  const tags = node("span", "attempt-tags");
  reviewModel.attemptTags(item, options).forEach(value => {
    const tag = node("span", `attempt-tag${value.className ? ` ${value.className}` : ""}`, value.label);
    tag.title = value.title;
    tags.append(tag);
  });
  return tags;
}

function appendSideCard(parent, { title, meta, tags, active, selectedTarget, onClick }) {
  const card = node("div", `side-card${active ? " active" : ""}`);
  card.role = "button";
  card.tabIndex = 0;
  if (selectedTarget) card.append(selectionCheckbox(selectedTarget));
  else card.append(node("span"));
  const copy = node("span");
  const titleNode = node("strong", "", title);
  titleNode.title = title;
  const metaNode = node("small", "", meta);
  metaNode.title = meta;
  copy.append(titleNode);
  if (tags) copy.append(tags);
  copy.append(metaNode);
  card.append(copy);
  card.addEventListener("click", onClick);
  card.addEventListener("keydown", event => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      onClick();
    }
  });
  parent.append(card);
}

function filteredReviews() {
  return reviewModel.filterItems(
    state.catalog.reviews,
    state.researchFilters,
    state.search,
  );
}

function filterControl(label, key, options) {
  const wrapper = node("label", "filter-control");
  wrapper.append(node("span", "", label));
  const select = node("select");
  select.dataset.filterKey = key;
  options.forEach(([value, text]) => {
    const option = node("option", "", text);
    option.value = value;
    option.selected = state.researchFilters[key] === value;
    select.append(option);
  });
  select.addEventListener("change", () => {
    state.researchFilters[key] = select.value;
    state.selectedProblem = "";
    state.selectedReview = "";
    syncNavigation({ replace: true });
  });
  wrapper.append(select);
  return wrapper;
}

function filterToggle(label, checked, handler, { priority = "", availability = "" } = {}) {
  const wrapper = node("label", "filter-toggle");
  if (priority) wrapper.dataset.filterPriority = priority;
  if (availability) wrapper.dataset.filterAvailability = availability;
  const input = node("input");
  input.type = "checkbox";
  input.checked = checked;
  input.addEventListener("change", () => {
    handler(input.checked);
    state.selectedProblem = "";
    state.selectedReview = "";
    syncNavigation({ replace: true });
  });
  wrapper.append(input, document.createTextNode(label));
  return wrapper;
}

function renderResearchFilters() {
  const details = node("details", "research-filters");
  details.open = state.researchFiltersOpen;
  details.addEventListener("toggle", () => {
    state.researchFiltersOpen = details.open;
  });
  const summary = node("summary");
  summary.append(node("span", "", "Problem filters"));
  summary.append(visibleProblemSelectionControl());
  details.append(summary);
  const controls = node("div", "research-filter-grid");
  controls.append(
    filterControl("Attempt status", "attemptStatus", reviewModel.filterOptions.attemptStatus),
    filterControl("Triage recommendation", "triage", reviewModel.filterOptions.triage),
    filterControl("Claim type", "claim", reviewModel.filterOptions.claim),
    filterControl("Correctness", "correctness", reviewModel.filterOptions.correctness),
    filterControl("Coverage", "coverage", reviewModel.filterOptions.coverage),
    filterControl("Importance", "importance", reviewModel.filterOptions.importance),
    filterControl("Verification", "confidence", reviewModel.filterOptions.confidence),
    filterControl("Literature", "literature", reviewModel.filterOptions.literature),
  );
  const toggles = node("div", "filter-toggles");
  reviewModel.priorityLevels.forEach(priority => {
    toggles.append(filterToggle(reviewModel.titleize(priority), state.researchFilters.priorities.has(priority), checked => {
      if (checked) state.researchFilters.priorities.add(priority);
      else state.researchFilters.priorities.delete(priority);
    }, { priority }));
  });
  toggles.append(filterToggle("Current", state.researchFilters.current, checked => {
    state.researchFilters.current = checked;
  }, { availability: "current" }));
  toggles.append(filterToggle("Stale", state.researchFilters.stale, checked => {
    state.researchFilters.stale = checked;
  }, { availability: "stale" }));
  const footer = node("div", "filter-footer");
  footer.append(node("span", "", "Human priority and review freshness"));
  footer.append(button("Reset", () => {
    state.researchFilters = reviewModel.createDefaultFilters();
    state.selectedProblem = "";
    state.selectedReview = "";
    syncNavigation({ replace: true });
  }, "filter-reset"));
  controls.append(toggles, footer);
  details.append(controls);
  return details;
}

function syncSidebarControls(tab, controls) {
  const search = controls.querySelector("input.search");
  if (search && search.value !== state.search) search.value = state.search;
  const sort = controls.querySelector("select[data-paper-sort]");
  if (sort && sort.value !== state.paperSort) sort.value = state.paperSort;
  if (tab !== "research") return;

  const details = controls.querySelector(".research-filters");
  if (details && details.open !== state.researchFiltersOpen) {
    details.open = state.researchFiltersOpen;
  }
  controls.querySelectorAll("select[data-filter-key]").forEach(select => {
    const value = state.researchFilters[select.dataset.filterKey];
    if (select.value !== value) select.value = value;
  });
  const available = reviewModel.availableFilters(state.catalog.reviews);
  controls.querySelectorAll("[data-filter-priority]").forEach(wrapper => {
    const priority = wrapper.dataset.filterPriority;
    wrapper.hidden = !available.priorities.has(priority);
    wrapper.querySelector("input").checked = state.researchFilters.priorities.has(priority);
  });
  controls.querySelectorAll("[data-filter-availability]").forEach(wrapper => {
    const key = wrapper.dataset.filterAvailability;
    wrapper.hidden = !available[key];
    wrapper.querySelector("input").checked = Boolean(state.researchFilters[key]);
  });
  updateVisibleProblemSelectionControl(
    controls.querySelector("input[data-select-visible-problems]"),
  );
}

function persistentSidebarControls(tab, create) {
  let controls = sidebarControlNodes.get(tab);
  if (!controls) {
    controls = create();
    sidebarControlNodes.set(tab, controls);
  }
  if (sidebar.dataset.controlsTab !== tab || controls.parentNode !== sidebar) {
    sidebar.replaceChildren(controls);
    sidebar.dataset.controlsTab = tab;
  } else {
    while (controls.nextSibling) controls.nextSibling.remove();
  }
  syncSidebarControls(tab, controls);
  return controls;
}

function renderResearch() {
  persistentSidebarControls("research", () => {
    const controls = node("div", "paper-list-controls research-controls");
    controls.append(
      sidebarSearch("Search open problems…"),
      paperSortControl(),
      renderResearchFilters(),
    );
    return controls;
  });
  const reviews = filteredReviews();
  const paperGroups = reviewModel.groupProblemsByPaper(
    reviews,
    state.paperSort,
    state.catalog.reviews,
  );
  const problems = paperGroups.flatMap(group => group.problems);
  const requested = state.catalog.reviews.find(item => item.itemKey === state.selectedReview);
  if (requested) state.selectedProblem = requested.problemKey;
  if (!problems.some(item => item.problemKey === state.selectedProblem)) {
    state.selectedProblem = problems[0]?.problemKey || "";
  }
  const listScroll = node("div", "problem-scroll");
  listScroll.append(node("div", "sidebar-heading queue-summary", reviewModel.queueSummary(reviews, state.researchFilters)));
  for (const group of paperGroups) {
    listScroll.append(node(
      "div",
      "sidebar-heading paper-heading",
      reviewModel.paperTitleWithYear(group.paperTitle, group.publicationTimestamp),
    ));
    const list = node("div", "side-list");
    group.problems.forEach(item => {
      appendSideCard(list, {
        title: `${item.problemId} · ${item.problemTitle}`,
        meta: item.attemptName
          ? `${reviewModel.statusLabel(item.attemptStatus)} · ${item.totalAttemptCount} total attempt${item.totalAttemptCount === 1 ? "" : "s"}`
          : "Unattempted",
        tags: attemptTagsNode(item, { includeKnown: true }),
        active: state.selectedProblem === item.problemKey,
        selectedTarget: problemTarget(item),
        onClick: () => {
          state.selectedProblem = item.problemKey;
          state.selectedReview = reviewModel.attemptsForProblem(reviews, item.problemKey)[0]?.itemKey || item.itemKey;
          state.detailTab = "attempt";
          syncNavigation();
        },
      });
    });
    listScroll.append(list);
  }
  sidebar.append(listScroll);

  const attempts = reviewModel.attemptsForProblem(reviews, state.selectedProblem);
  if (!attempts.some(item => item.itemKey === state.selectedReview)) {
    state.selectedReview = attempts[0]?.itemKey || "";
  }
  const attemptSwitcher = node("div", "attempt-switcher");
  attemptSwitcher.append(node("div", "sidebar-heading", `Attempts${attempts.length ? ` · ${attempts.length}` : ""}`));
  const attemptList = node("div", "attempt-list");
  attempts.forEach(item => {
    appendSideCard(attemptList, {
      title: item.attemptName || "No attempts yet",
      meta: item.attemptDirectory
        ? reviewModel.statusLabel(item.attemptStatus)
        : "Open problem",
      tags: attemptTagsNode(item),
      active: state.selectedReview === item.itemKey,
      selectedTarget: item.attemptDirectory ? attemptTarget(item) : null,
      onClick: () => {
        state.selectedReview = item.itemKey;
        state.detailTab = "attempt";
        syncNavigation();
      },
    });
  });
  attemptSwitcher.append(attemptList);
  sidebar.append(attemptSwitcher);

  const summary = state.catalog.reviews.find(item => item.itemKey === state.selectedReview);
  if (!summary) {
    main.replaceChildren(document.getElementById("empty-template").content.cloneNode(true));
    return;
  }
  renderReviewDetail(state.detailCache.get(summary.itemKey) || summary);
  if (!state.detailCache.has(summary.itemKey)) {
    api(`/api/review-detail?${new URLSearchParams({ key: summary.itemKey })}`)
      .then(detail => {
        state.detailCache.set(summary.itemKey, detail);
        if (state.selectedReview === summary.itemKey && state.tab === "research") renderReviewDetail(detail);
      })
      .catch(error => showNotice(error.message, true));
  }
}

function markdown(value, missing = "No content available.") {
  const body = node("div", "markdown");
  if (!value) {
    body.append(node("p", "", missing));
  } else if (markdownRenderer) {
    body.innerHTML = markdownRenderer.render(value);
  } else {
    const pre = node("pre", "", value);
    body.append(pre);
  }
  return body;
}

function addAction(parent, label, action, targets, primary = false) {
  parent.append(button(label, () => openTask(action, targets), `button${primary ? " primary" : ""}`));
}

function renderReviewDetail(item) {
  const shell = node("div", "main-inner");
  const selectedSummary = state.catalog.reviews.find(value => value.itemKey === item.itemKey);
  if (selectedSummary && item !== selectedSummary) {
    item = { ...selectedSummary, ...item };
  }
  const hero = node("section", "hero");
  const copy = node("div");
  copy.append(node("div", "eyebrow", `${item.paperTitle} · ${item.problemId}`));
  copy.append(node("h1", "", item.problemTitle));
  copy.append(node("p", "", item.paperAuthors?.join(", ") || "Authors unavailable"));
  const badges = node("div", "badges");
  badges.append(badge(item.explicitness, "neutral"));
  reviewModel.detailBadges(item).forEach(value => {
    let className = value.value;
    if (value.dimension === "priority") className = value.value === "high" ? "error" : value.value === "medium" ? "warn" : "neutral";
    else if (value.dimension === "warning") className = "error";
    else if (["coverage", "importance", "confidence", "literature"].includes(value.dimension)) className = "neutral";
    badges.append(node("span", `badge ${className || "neutral"}`, value.label));
  });
  copy.append(badges);
  hero.append(copy);
  shell.append(hero);

  if (item.attemptDisplayPath) shell.append(node("code", "attempt-path", item.attemptDisplayPath));

  const actions = node("div", "actions");
  const problem = problemTarget(item);
  const attempt = item.attemptDirectory ? attemptTarget(item) : null;
  addAction(actions, item.triageCurrent ? "Triage again" : "Triage", "triage", [problem]);
  addAction(actions, item.literatureStatus ? "Search literature again" : "Search literature", "literature", [problem]);
  addAction(actions, attempt ? "Solve again" : "Solve", "solve", [problem], true);
  if (attempt) {
    addAction(actions, item.attemptStatus === "reviewed" ? "Review again" : "Review", "review", [attempt]);
    addAction(actions, "Write this result", "write", [attempt]);
  }
  shell.append(actions);

  const problemStatement = node("section", "problem-statement panel");
  problemStatement.append(
    node("h2", "", "Open problem statement"),
    markdown(item.problemStatement, "Loading problem statement…"),
  );
  shell.append(problemStatement);

  const summaries = node("div", "summary-grid");
  reviewModel.summaryCards(item).forEach(card => {
    summaries.append(summaryPanel(card.title, card.value, card.missing));
  });
  if (summaries.children.length) shell.append(summaries);

  if (item.claimReviews?.length) {
    const section = node("section", "section panel");
    section.append(node("h2", "", "Claim assessments"));
    const list = node("ul", "claim-list");
    item.claimReviews.forEach(claim => {
      const row = node("li");
      row.append(
        node("strong", "", `${claim.claim_id || "?"} — ${claim.assessment || "unknown"}: `),
        document.createTextNode(claim.explanation || ""),
      );
      list.append(row);
    });
    section.append(list);
    shell.append(section);
  }
  appendStringList(shell, "Blocking gaps", item.blockingGaps);
  appendStringList(shell, "Recommended next steps", item.recommendedNextSteps);
  appendStringList(shell, "Warnings", item.warnings);

  const tabs = reviewModel.detailTabs(item);
  if (!tabs.some(([key]) => key === state.detailTab)) state.detailTab = tabs[0][0];
  const tabbar = node("div", "detail-tabs");
  tabs.forEach(([key, label]) => {
    tabbar.append(button(label, () => {
      state.detailTab = key;
      syncNavigation();
    }, `detail-tab${state.detailTab === key ? " active" : ""}`));
  });
  shell.append(tabbar);
  const section = node("section", "section");
  if (state.detailTab === "attempt") section.append(markdown(item.solverAttempt, "Loading solver attempt…"));
  else if (state.detailTab === "critique") section.append(markdown(item.critique, "No critique is installed."));
  else if (state.detailTab === "triage") section.append(markdown(item.triageReport, "Loading triage report…"));
  else if (state.detailTab === "literature") section.append(markdown(item.literatureReport, "No literature report is installed."));
  else section.append(fileGrid(item.files || []));
  shell.append(section);
  main.replaceChildren(shell);
}

function appendStringList(parent, title, values) {
  if (!Array.isArray(values) || !values.length) return;
  const section = node("section", "section panel");
  section.append(node("h2", "", title));
  const list = node("ul", "plain-list");
  values.forEach(value => list.append(node("li", "", String(value))));
  section.append(list);
  parent.append(section);
}

function summaryPanel(title, value, missing = "No summary available.") {
  const panel = node("section", "panel");
  panel.append(node("h2", "", title));
  panel.append(markdown(value, missing));
  return panel;
}

function fileGrid(files) {
  const grid = node("div", "file-grid");
  if (!files.length) grid.append(node("p", "", "No files are available."));
  files.forEach(value => {
    const path = typeof value === "string" ? value : value.path;
    const label = typeof value === "string" ? path.split(/[\\/]/).pop() : value.label;
    const link = node("a", "file-link");
    link.href = fileUrl(path);
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.append(node("strong", "", label));
    link.append(node("small", "", path));
    grid.append(link);
  });
  return grid;
}

function renderPapers() {
  persistentSidebarControls("papers", () => {
    const controls = node("div", "paper-list-controls");
    controls.append(
      sidebarSearch("Search source papers…"),
      paperSortControl(),
    );
    return controls;
  });
  const query = state.search.trim().toLowerCase();
  const papers = reviewModel.sortPapers(
    state.catalog.papers.filter(paper => !query || `${paper.title} ${paper.name} ${paper.authors.join(" ")}`.toLowerCase().includes(query)),
    state.paperSort,
    state.catalog.reviews,
  );
  const list = node("div", "side-list");
  papers.forEach(paper => appendSideCard(list, {
    title: reviewModel.paperTitleWithYear(
      paper.title,
      paper.publicationTimestamp,
    ),
    meta: paper.analyzed ? `${paper.problemCount} open problems` : "Not analyzed",
    active: state.selectedPaper === paper.key,
    selectedTarget: paperTarget(paper),
    onClick: () => { state.selectedPaper = paper.key; syncNavigation(); },
  }));
  sidebar.append(node("div", "sidebar-heading", `${papers.length} papers`), list);
  if (!state.selectedPaper || !papers.some(paper => paper.key === state.selectedPaper)) state.selectedPaper = papers[0]?.key || "";
  const paper = state.catalog.papers.find(value => value.key === state.selectedPaper);
  if (!paper) {
    main.replaceChildren(document.getElementById("empty-template").content.cloneNode(true));
    return;
  }
  const shell = node("div", "main-inner");
  const hero = node("section", "hero");
  const copy = node("div");
  copy.append(node("div", "eyebrow", paper.name));
  copy.append(node("h1", "", paper.title));
  copy.append(node("p", "", paper.authors.join(", ") || "Authors unavailable"));
  const badges = node("div", "badges");
  badges.append(badge(paper.analyzed ? "analyzed" : "not analyzed", paper.analyzed ? "succeeded" : "warn"));
  if (paper.analyzed) badges.append(badge(`${paper.problemCount} problems`, "neutral"));
  copy.append(badges);
  hero.append(copy);
  shell.append(hero);
  const actions = node("div", "actions");
  addAction(actions, paper.analyzed ? "Analyze again" : "Analyze", "analyze", [paperTarget(paper)], !paper.analyzed);
  const problems = uniqueProblemTargets(paper.path);
  if (problems.length) {
    addAction(actions, "Triage problems", "triage", problems);
    addAction(actions, "Search literature", "literature", problems);
    addAction(actions, "Solve problems", "solve", problems, paper.analyzed);
    addAction(actions, "Write from latest results", "write", [paperTarget(paper)]);
  }
  shell.append(actions);
  shell.append(node("section", "section-title", "Files"));
  shell.append(fileGrid(paper.files));
  main.replaceChildren(shell);
}

function uniqueProblemTargets(paperPath) {
  const values = new Map();
  state.catalog.reviews.filter(item => item.paperDirectory === paperPath).forEach(item => {
    values.set(item.problemId, problemTarget(item));
  });
  return [...values.values()];
}

function renderManuscripts() {
  persistentSidebarControls("manuscripts", () => {
    const controls = node("div", "paper-list-controls");
    controls.append(sidebarSearch("Search manuscripts…"));
    return controls;
  });
  const query = state.search.trim().toLowerCase();
  const manuscripts = state.catalog.manuscripts.filter(value => !query || `${value.name} ${value.latest.title}`.toLowerCase().includes(query));
  const list = node("div", "side-list");
  manuscripts.forEach(value => appendSideCard(list, {
    title: value.latest.title,
    meta: `${value.drafts.length} draft${value.drafts.length === 1 ? "" : "s"} · ${humanize(value.latest.verdict)}`,
    active: state.selectedManuscript === value.key,
    onClick: () => {
      state.selectedManuscript = value.key;
      state.selectedDraft = value.latest.key;
      syncNavigation();
    },
  }));
  sidebar.append(node("div", "sidebar-heading", `${manuscripts.length} manuscripts`), list);
  if (!state.selectedManuscript || !manuscripts.some(value => value.key === state.selectedManuscript)) state.selectedManuscript = manuscripts[0]?.key || "";
  const manuscript = state.catalog.manuscripts.find(value => value.key === state.selectedManuscript);
  if (!manuscript) {
    main.replaceChildren(document.getElementById("empty-template").content.cloneNode(true));
    return;
  }
  if (!manuscript.drafts.some(value => value.key === state.selectedDraft)) {
    state.selectedDraft = manuscript.latest.key;
  }
  const draft = manuscript.drafts.find(value => value.key === state.selectedDraft) || manuscript.latest;
  const shell = node("div", "main-inner");
  const hero = node("section", "hero");
  const copy = node("div");
  copy.append(node("div", "eyebrow", `${manuscript.name} · ${draft.name}`));
  copy.append(node("h1", "", draft.title));
  const badges = node("div", "badges");
  badges.append(badge(draft.status || "draft", "neutral"));
  badges.append(badge(draft.verdict, draft.verdict === "ready_for_expert_review" ? "succeeded" : "warn"));
  copy.append(badges);
  hero.append(copy);
  shell.append(hero);
  const actions = node("div", "actions");
  addAction(actions, draft.verdict === "unreviewed" ? "Resume review" : "Revise", "revise", [draftTarget(draft)], true);
  const pdf = draft.files.find(path => path.endsWith("main.pdf"));
  if (pdf) {
    const open = node("a", "button", "Open PDF");
    open.href = fileUrl(pdf);
    open.target = "_blank";
    shell.append(actions);
    actions.append(open);
  } else shell.append(actions);
  if (draft.summary) shell.append(summaryPanel("Paper critic", draft.summary));
  const sources = draft.sources || { papers: [], problems: [] };
  if (sources.papers.length || sources.problems.length) {
    const sourcesHeading = node("div", "section-title");
    sourcesHeading.append(node("h2", "", "Based on"));
    const sourceGrid = node("div", "source-grid");
    if (sources.papers.length) {
      const panel = node("section", "panel source-panel");
      panel.append(node("h2", "", `Source paper${sources.papers.length === 1 ? "" : "s"}`));
      const list = node("ul", "source-list");
      sources.papers.forEach(source => {
        const item = node("li");
        const link = node("button", "source-link", source.title);
        link.type = "button";
        link.addEventListener("click", () => {
          state.selectedPaper = source.path;
          setTab("papers");
        });
        item.append(link);
        list.append(item);
      });
      panel.append(list);
      sourceGrid.append(panel);
    }
    if (sources.problems.length) {
      const panel = node("section", "panel source-panel");
      panel.append(node("h2", "", `Open problem${sources.problems.length === 1 ? "" : "s"}`));
      const list = node("ul", "source-list");
      sources.problems.forEach(source => {
        const item = node("li");
        const label = node("button", "source-link", `${source.id}: ${source.title}`);
        label.type = "button";
        label.addEventListener("click", () => {
          const review = state.catalog.reviews.find(value =>
            value.paperDirectory === source.paperPath && value.problemId === source.id
          );
          if (review) {
            state.selectedReview = review.itemKey;
            state.selectedProblem = review.problemKey;
            setTab("research");
          }
        });
        item.append(label, node("small", "", source.paperTitle));
        list.append(item);
      });
      panel.append(list);
      sourceGrid.append(panel);
    }
    shell.append(sourcesHeading, sourceGrid);
  }
  const heading = node("div", "section-title");
  heading.append(node("h2", "", "Latest draft files"));
  shell.append(heading, fileGrid(draft.files));
  const historyHeading = node("div", "section-title");
  historyHeading.append(node("h2", "", "Draft history"));
  const history = node("div", "card-grid");
  [...manuscript.drafts].reverse().forEach(value => {
    const card = node("button", `entity-card draft-card${value.key === draft.key ? " active" : ""}`);
    card.type = "button";
    card.append(node("strong", "", value.name));
    card.append(node("small", "", `${humanize(value.status)} · ${humanize(value.verdict)}`));
    card.addEventListener("click", () => {
      state.selectedDraft = value.key;
      syncNavigation();
    });
    history.append(card);
  });
  shell.append(historyHeading, history);
  main.replaceChildren(shell);
}

function renderActivity({ preserveDetail = false } = {}) {
  persistentSidebarControls("activity", () => {
    const controls = node("div", "paper-list-controls");
    controls.append(sidebarSearch("Search tasks…"));
    return controls;
  });
  const query = state.search.trim().toLowerCase();
  const jobs = state.jobs.filter(job => !query || `${job.title} ${job.action} ${job.status}`.toLowerCase().includes(query));
  const list = node("div", "side-list");
  jobs.forEach(job => appendSideCard(list, {
    title: taskSidebarTitle(job),
    meta: taskSidebarMeta(job),
    tags: taskBadges(job),
    active: state.selectedJob === job.id,
    onClick: () => {
      state.selectedJob = job.id;
      syncNavigation();
    },
  }));
  sidebar.append(node("div", "sidebar-heading", `${jobs.length} tasks`), list);
  if (!state.selectedJob || !jobs.some(job => job.id === state.selectedJob)) state.selectedJob = jobs[0]?.id || "";
  if (!state.selectedJob) {
    main.replaceChildren(document.getElementById("empty-template").content.cloneNode(true));
    return;
  }
  const summary = state.jobs.find(job => job.id === state.selectedJob);
  const cached = state.jobDetails.get(state.selectedJob);
  const visibleJob = main.querySelector("[data-job-detail]")?.dataset.jobDetail;
  if (cached) {
    if (!preserveDetail || visibleJob !== state.selectedJob) renderJobDetail(cached);
    else refreshVisibleRunLogs();
  } else {
    const shell = node("div", "main-inner");
    shell.append(node("div", "loading", summary ? `Loading ${summary.title}…` : "Loading task…"));
    main.replaceChildren(shell);
  }
  loadJob(state.selectedJob);
}

function jobRenderFingerprint(job) {
  return JSON.stringify({
    id: job.id,
    action: job.action,
    title: job.title,
    status: job.status,
    priority_level: job.priority_level,
    scheduling_paused: job.scheduling_paused,
    created_at: job.created_at,
    finished_at: job.finished_at,
    runs: job.runs.map(run => ({
      id: run.id,
      label: run.label,
      status: run.status,
      started_at: run.started_at,
      finished_at: run.finished_at,
      exit_code: run.exit_code,
      error: run.error,
      outputs: run.outputs,
      argv: run.argv,
      cancel_requested: run.cancel_requested,
    })),
  });
}

function problemRunPresentation(action, run) {
  const targets = (run.targets || []).filter(target => target.kind === "problem");
  if (action !== "literature" || !targets.length) {
    return { title: run.label, targets: [] };
  }
  const paperPaths = new Set(targets.map(target =>
    normalizedPath(target.path).split("/").slice(0, -1).join("/"),
  ));
  if (paperPaths.size !== 1) return { title: run.label, targets: [] };
  const paperPath = paperPaths.values().next().value;
  const paper = state.catalog.papers.find(value => normalizedPath(value.path) === paperPath);
  const review = state.catalog.reviews.find(value => normalizedPath(value.paperDirectory) === paperPath);
  const fallback = targets[0].path.split(/[\\/]/).slice(-2, -1)[0] || run.label;
  const paperTitle = reviewModel.paperTitleWithYear(
    paper?.title || review?.paperTitle || fallback,
    paper?.published || review?.paperPublished,
  );
  const labels = targets.map(target => target.label || target.path.split(/[\\/]/).pop());
  const count = `${targets.length} selected problem${targets.length === 1 ? "" : "s"}`;
  return {
    title: paperTitle,
    summary: `${count} · ${labels.join(" · ")}`,
    targets: labels,
  };
}

function appendRunTargetSummary(parent, presentation) {
  if (!presentation.targets.length) return;
  const summary = node("span", "run-target-summary", presentation.summary);
  summary.title = presentation.summary;
  parent.append(summary);
}

async function loadJob(id) {
  try {
    const job = await api(`/api/jobs/${id}`);
    const previous = state.jobDetails.get(id);
    state.jobDetails.set(id, job);
    if (state.tab !== "activity" || state.selectedJob !== id) return;
    const visibleJob = main.querySelector("[data-job-detail]")?.dataset.jobDetail;
    if (
      !previous ||
      visibleJob !== id ||
      jobRenderFingerprint(previous) !== jobRenderFingerprint(job)
    ) {
      renderJobDetail(job);
    } else {
      refreshVisibleRunLogs();
    }
  } catch (error) {
    showNotice(error.message, true);
  }
}

function renderJobDetail(job) {
  const shell = node("div", "main-inner");
  shell.dataset.jobDetail = job.id;
  const hero = node("section", "hero");
  const copy = node("div");
  copy.append(node("div", "eyebrow", "Managed task"));
  copy.append(node("h1", "", taskActionTitle(job.action)));
  const scope = node("details", "task-scope");
  scope.append(node("summary", "", taskScopeSummary(job)));
  const targets = job.plan?.targets || job.request?.targets || [];
  if (targets.length) {
    const targetList = node("div", "target-list task-targets");
    targets.forEach(value => targetList.append(node("span", "target-chip", value.label || value.path)));
    scope.append(targetList);
  }
  copy.append(scope, node("p", "", `Created ${formatTime(job.created_at)}`));
  const badges = node("div", "badges");
  badges.append(taskIsPaused(job) ? badge("Paused", "paused") : taskStatusBadge(job.status));
  badges.append(badge(`Share ${priorityMultiplier(job.priority_level)}`, "neutral"));
  copy.append(badges);
  hero.append(copy, jobSchedulingControls(job));
  shell.append(hero);
  job.runs.forEach((run, index) => {
    const section = node("section", "run-card panel");
    const expanded = state.expandedRuns.has(run.id);
    const presentation = problemRunPresentation(job.action, run);
    const heading = node("div", "run-summary");
    const toggle = button("", () => {
      if (state.expandedRuns.has(run.id)) state.expandedRuns.delete(run.id);
      else state.expandedRuns.add(run.id);
      renderJobDetail(job);
    }, "run-toggle");
    toggle.setAttribute("aria-expanded", String(expanded));
    toggle.setAttribute("aria-label", `${expanded ? "Hide" : "Show"} details for ${presentation.title}`);
    toggle.append(node("span", "run-chevron", "›"));
    const headingCopy = node("span", "run-heading-copy");
    const title = node("strong", "", `${index + 1}. ${presentation.title}`);
    title.title = presentation.title;
    headingCopy.append(title);
    appendRunTargetSummary(headingCopy, presentation);
    headingCopy.append(runTiming(run));
    toggle.append(headingCopy);
    const actions = node("div", "run-actions");
    if (["queued", "starting", "running", "cancel_requested"].includes(run.status)) {
      actions.append(button("Cancel", () => mutateRun(run.id, "cancel"), "button danger"));
    }
    if (["failed", "canceled", "interrupted"].includes(run.status) && !(run.outputs || []).length) {
      actions.append(button("Retry", () => mutateRun(run.id, "retry"), "button primary"));
    }
    heading.append(toggle, actions);
    section.append(heading);
    if (run.error) section.append(node("div", "error-box", run.error));
    if (run.outputs?.length) {
      const output = node("div", "run-artifacts");
      run.outputs.forEach(path => {
        const row = node("div", "artifact-row");
        const route = outputRoute(path);
        const artifactCopy = node("div", "artifact-copy");
        artifactCopy.title = path;
        artifactCopy.append(
          node("strong", "", path.split(/[\\/]/).pop()),
          node("small", "", path),
        );
        const artifactActions = node("div", "artifact-actions");
        if (route) {
          artifactActions.append(button(
            outputRouteLabel(route),
            () => openOutput(path),
            "artifact-action route",
          ));
        }
        const view = node("a", "artifact-action", "View");
        view.href = artifactViewUrl(path);
        view.target = "_blank";
        view.rel = "noopener noreferrer";
        const raw = node("a", "artifact-action", "Raw");
        raw.href = rawFileUrl(path);
        raw.target = "_blank";
        raw.rel = "noopener noreferrer";
        artifactActions.append(view, raw);
        row.append(artifactCopy, artifactActions);
        output.append(row);
      });
      section.append(output);
    }
    if (expanded) {
      const details = node("div", "run-expanded");
      if (presentation.targets.length) {
        const selected = node("div", "confirm-block");
        selected.append(node("h3", "", "Selected problems"));
        const targetList = node("div", "target-list run-targets");
        presentation.targets.forEach(label => targetList.append(node("span", "target-chip", label)));
        selected.append(targetList);
        details.append(selected);
      }
      const command = node("div", "confirm-block");
      command.append(node("h3", "", "Command"), node("pre", "command", run.argv.join(" ")));
      details.append(command);
      const cachedLog = state.runLogs.get(run.id);
      const log = node("pre", "console", runLogText(cachedLog));
      log.dataset.runLog = run.id;
      const consoleShell = node("div", "console-shell");
      consoleShell.append(log, runConsoleStatus(run));
      details.append(consoleShell);
      section.append(details);
    }
    shell.append(section);
  });
  main.replaceChildren(shell);
  refreshVisibleRunLogs();
}

function runLogText(value) {
  if (!value) return "Loading output…";
  return value.text || "No console output yet.";
}

function updateRunLogNodes(runId, value) {
  main.querySelectorAll("[data-run-log]").forEach(log => {
    if (log.dataset.runLog !== runId) return;
    log.textContent = runLogText(value);
    log.scrollTop = log.scrollHeight;
  });
}

async function refreshRunLog(runId) {
  const cached = state.runLogs.get(runId);
  if (cached?.complete) {
    updateRunLogNodes(runId, cached);
    return;
  }
  let request = state.runLogLoads.get(runId);
  if (!request) {
    const offset = cached?.nextOffset || 0;
    request = api(`/api/runs/${runId}/log?${new URLSearchParams({ offset: String(offset) })}`)
      .then(value => {
        const previous = state.runLogs.get(runId) || { text: "", nextOffset: 0 };
        const result = {
          text: previous.text + (value.text || ""),
          nextOffset: value.nextOffset,
          complete: value.complete,
        };
        state.runLogs.set(runId, result);
        return result;
      })
      .finally(() => state.runLogLoads.delete(runId));
    state.runLogLoads.set(runId, request);
  }
  try {
    updateRunLogNodes(runId, await request);
  } catch (error) {
    if (!state.runLogs.has(runId)) {
      updateRunLogNodes(runId, { text: error.message, complete: false });
    } else {
      showNotice(error.message, true);
    }
  }
}

function refreshVisibleRunLogs() {
  main.querySelectorAll("[data-run-log]").forEach(log => {
    refreshRunLog(log.dataset.runLog);
  });
}

async function mutateRun(runId, action) {
  const message = action === "retry"
    ? "Queue a retry of this exact run?"
    : "Stop this run and its child processes?";
  if (!window.confirm(message)) return;
  try {
    await api(`/api/runs/${runId}/${action}`, { method: "POST", body: {} });
    await refreshJobs({ preserveActivityDetail: true });
  } catch (error) {
    showNotice(error.message, true);
  }
}

function jobSchedulingControls(job) {
  const controls = node("div", "job-scheduling");
  const row = node("div", "job-scheduling-row");
  const label = node("label");
  label.append(node("span", "", "Scheduling share"));
  const select = node("select");
  priorityOptions(false).forEach(([value, text]) => {
    const option = node("option", "", text);
    option.value = value;
    select.append(option);
  });
  select.value = String(job.priority_level ?? 0);
  select.disabled = ["succeeded", "partial", "failed", "canceled", "interrupted"].includes(job.status);
  select.addEventListener("change", () => mutateJobScheduling(job.id, {
    priorityLevel: Number(select.value),
  }));
  label.append(select);
  row.append(label);
  if (!select.disabled) {
    row.append(button(
      job.scheduling_paused ? "Resume" : "Pause",
      () => mutateJobScheduling(job.id, { paused: !job.scheduling_paused }),
      "button",
    ));
  }
  controls.append(row, node("small", "", "Proportion of workers assigned to this task"));
  return controls;
}

async function mutateJobScheduling(jobId, changes) {
  try {
    const job = await api(`/api/jobs/${jobId}/scheduling`, {
      method: "POST",
      body: changes,
    });
    state.jobDetails.set(jobId, job);
    await refreshJobs({ preserveActivityDetail: true });
    if (state.tab === "activity" && state.selectedJob === jobId) renderJobDetail(job);
  } catch (error) {
    showNotice(error.message, true);
    const job = state.jobDetails.get(jobId);
    if (job && state.tab === "activity" && state.selectedJob === jobId) renderJobDetail(job);
  }
}

function normalizedPath(path) {
  return path.replaceAll("\\", "/").replace(/\/$/, "").toLowerCase();
}

function pathContains(parent, child) {
  const root = normalizedPath(parent);
  const value = normalizedPath(child);
  return value === root || value.startsWith(`${root}/`);
}

function outputRoute(path) {
  const filename = path.split(/[\\/]/).pop()?.toLowerCase() || "";
  const review = state.catalog.reviews.find(
    item => item.attemptDirectory && pathContains(item.attemptDirectory, path),
  );
  if (review) {
    const critiqueFiles = new Set([
      "critique.md", "review-result.json", "review-manifest.json",
      "review-events.jsonl", "review-run.log",
    ]);
    return {
      tab: "research",
      review,
      detail: critiqueFiles.has(filename) ? "critique" : "attempt",
    };
  }
  const problem = state.catalog.reviews.find(
    item => pathContains(`${item.paperDirectory}/${item.problemId}`, path),
  );
  if (problem) {
    let detail = "attempt";
    if (filename.startsWith("triage")) detail = "triage";
    else if (filename.startsWith("literature")) detail = "literature";
    return { tab: "research", review: problem, detail };
  }
  const paper = state.catalog.papers.find(
    item => normalizedPath(item.path) === normalizedPath(path) ||
      pathContains(`${item.path}/analysis`, path),
  );
  if (paper) return { tab: "papers", paper };
  for (const manuscript of state.catalog.manuscripts) {
    const draft = manuscript.drafts.find(value => pathContains(value.path, path));
    if (draft) return { tab: "manuscripts", manuscript, draft };
  }
  return null;
}

function outputRouteLabel(route) {
  if (route.tab === "papers") return "Go to paper";
  if (route.tab === "manuscripts") return "Go to manuscript";
  if (route.review?.attemptDirectory && route.detail !== "literature" && route.detail !== "triage") {
    return "Go to attempt";
  }
  return "Go to problem";
}

function openOutput(path) {
  const route = outputRoute(path);
  if (!route) return false;
  if (route.tab === "research") {
    state.selectedReview = route.review.itemKey;
    state.selectedProblem = route.review.problemKey;
    state.detailTab = route.detail;
  } else if (route.tab === "papers") {
    state.selectedPaper = route.paper.key;
  } else {
    state.selectedManuscript = route.manuscript.key;
    state.selectedDraft = route.draft.key;
  }
  setTab(route.tab);
  return true;
}

const actionNames = {
  analyze: "Analyze papers", triage: "Triage problems", literature: "Search literature",
  solve: "Solve problems", review: "Review attempts", write: "Write paper", revise: "Revise manuscript",
};

const taskActionTitles = {
  analyze: "Paper analysis",
  triage: "Problem triage",
  literature: "Literature review",
  solve: "Problem solving",
  review: "Solution review",
  write: "Paper writing",
  revise: "Manuscript revision",
};

function taskActionTitle(action) {
  return taskActionTitles[action] || humanize(action);
}

function taskTargets(job) {
  return job.plan?.targets || job.request?.targets || [];
}

function targetCountLabel(targets) {
  const nouns = {
    paper: ["paper", "papers"],
    problem: ["problem", "problems"],
    attempt: ["attempt", "attempts"],
    draft: ["draft", "drafts"],
  };
  const kinds = new Set(targets.map(value => value.kind));
  if (kinds.size === 1 && nouns[kinds.values().next().value]) {
    const [singular, plural] = nouns[kinds.values().next().value];
    return `${targets.length} ${targets.length === 1 ? singular : plural}`;
  }
  return `${targets.length} selection${targets.length === 1 ? "" : "s"}`;
}

function targetPaperCount(targets) {
  const papers = new Set();
  targets.forEach(value => {
    const parts = normalizedPath(value.path).split("/");
    if (value.kind === "paper") papers.add(parts.join("/"));
    else if (value.kind === "problem") papers.add(parts.slice(0, -1).join("/"));
    else if (value.kind === "attempt") papers.add(parts.slice(0, -2).join("/"));
  });
  return papers.size;
}

function taskScopeSummary(job) {
  const targets = taskTargets(job);
  if (!targets.length) return job.title;
  const pieces = [targetCountLabel(targets)];
  const papers = targetPaperCount(targets);
  if (papers) pieces.push(`${papers} paper${papers === 1 ? "" : "s"}`);
  const units = job.plan?.units?.length || new Set(job.runs.map(run => run.unit_index)).size;
  if (units) pieces.push(`${units} run${units === 1 ? "" : "s"}`);
  return pieces.join(" · ");
}

function taskSidebarTitle(job) {
  const targets = taskTargets(job);
  const scope = targets.length === 1
    ? targets[0].label || targetCountLabel(targets)
    : targets.length ? targetCountLabel(targets) : job.title;
  return `${humanize(job.action)} · ${scope}`;
}

function taskSidebarMeta(job) {
  if (taskIsPaused(job)) {
    const active = Number(job.counts?.active) || 0;
    const queued = Number(job.counts?.queued) || 0;
    return `${active ? `${active} finishing · ` : ""}${queued} waiting · ${priorityMultiplier(job.priority_level)}`;
  }
  if (job.status !== "running") return `Created ${formatTime(job.created_at)}`;
  const counts = job.counts || {};
  const total = Number(counts.total) || 0;
  const done = (Number(counts.succeeded) || 0) + (Number(counts.unsuccessful) || 0);
  const active = Number(counts.active) || 0;
  const queued = Number(counts.queued) || 0;
  if (!total) return `Created ${formatTime(job.created_at)}`;
  const pieces = [`${done}/${total} done`];
  if (active) pieces.push(`${active} running`);
  if (queued) pieces.push(`${queued} queued`);
  return pieces.join(" · ");
}

function field(name, label, { type = "text", value = "", help = "", full = false, options = [] } = {}) {
  const wrapper = node("label", `field${full ? " full" : ""}`);
  wrapper.append(node("span", "", label));
  let input;
  if (type === "textarea") input = node("textarea");
  else if (type === "select") {
    input = node("select");
    options.forEach(([optionValue, optionLabel]) => {
      const option = node("option", "", optionLabel);
      option.value = optionValue;
      input.append(option);
    });
  } else {
    input = node("input");
    input.type = type;
  }
  input.name = name;
  input.value = value ?? "";
  wrapper.append(input);
  if (help) wrapper.append(node("small", "", help));
  return wrapper;
}

function checkbox(name, label, help = "", checked = false) {
  const wrapper = node("label", "field checkbox");
  const input = node("input");
  input.type = "checkbox";
  input.name = name;
  input.checked = checked;
  const copy = node("span");
  copy.append(node("span", "", label));
  if (help) copy.append(node("small", "", help));
  wrapper.append(input, copy);
  return wrapper;
}

function promptLabel(action) {
  return {
    analyze: "Analyzer direction", triage: "Triage direction", literature: "Search direction",
    solve: "Solver direction", review: "Critic direction", write: "Writer direction", revise: "Revision direction",
  }[action];
}

function dialogStorageKey(action, targets) {
  return `loose-ends-task-draft:${action}:${targets.map(value => value.path).sort().join("|")}`;
}

function openTask(action, targets) {
  const storageKey = dialogStorageKey(action, targets);
  let saved = {};
  try { saved = JSON.parse(sessionStorage.getItem(storageKey) || "{}"); } catch (_) { saved = {}; }
  state.dialog = { action, targets, options: saved, storageKey, plan: null };
  renderTaskConfiguration();
  dialog.showModal();
}

function renderTaskConfiguration(errorMessage = "") {
  const task = state.dialog;
  dialogEyebrow.textContent = "Step 1 of 2 · Configure";
  dialogTitle.textContent = actionNames[task.action];
  dialogBody.replaceChildren();
  const targets = node("div", "target-list");
  task.targets.forEach(value => targets.append(node("span", "target-chip", value.label)));
  dialogBody.append(targets);
  if (errorMessage) dialogBody.append(node("div", "error-box", errorMessage));
  const grid = node("div", "form-grid");
  const options = task.options;
  grid.append(field("prompt", promptLabel(task.action), {
    type: "textarea", value: options.prompt || "", full: true,
    help: "Added to the standard task instructions without replacing validation safeguards.",
  }));
  if (["solve", "write", "revise"].includes(task.action)) {
    grid.append(field("reviewPrompt", task.action === "solve" ? "Solution-critic direction" : "Paper-critic direction", {
      type: "textarea", value: options.reviewPrompt || "", full: true,
    }));
  }
  if (["solve", "write", "revise"].includes(task.action)) {
    grid.append(field("maxRounds", "Maximum rounds", {
      type: "number", value: options.maxRounds || 1,
      help: "The pipeline may stop early when its critic confirms completion.",
    }));
  }
  if (task.action === "solve") {
    grid.append(field("review", "Review policy", {
      type: "select", value: options.review || "promising",
      options: [["promising", "Promising attempts"], ["all", "Every attempt"], ["none", "No critic"]],
    }));
    grid.append(field("reviewTimeoutMinutes", "Critic timeout (minutes)", { type: "number", value: options.reviewTimeoutMinutes || 120 }));
    grid.append(checkbox("includeLiteratureResolved", "Solve literature-resolved problems", "Useful for reconstructing or auditing a known resolution.", options.includeLiteratureResolved));
  }
  if (task.action === "review") {
    grid.append(field("mode", "Review mode", {
      type: "select", value: options.mode || "promising",
      options: [["promising", "Checkable progress only"], ["all", "Every selected attempt"]],
    }));
    grid.append(field("timeoutMinutes", "Timeout (minutes)", { type: "number", value: options.timeoutMinutes || 120 }));
  }
  if (["analyze", "triage", "literature", "review"].includes(task.action)) {
    grid.append(checkbox("force", "Force replacement", "Run even if matching current output exists.", options.force));
  }
  if (task.action === "analyze") {
    grid.append(checkbox("recoverComplete", "Recover completed workspace", "Install a preserved completed analysis without a new model turn.", options.recoverComplete));
  }
  if (task.action === "write") {
    grid.append(field("authors", "Authors", { type: "textarea", value: Array.isArray(options.authors) ? options.authors.join("\n") : options.authors || "", help: "One author per line." }));
    grid.append(field("title", "Title direction", { value: options.title || "" }));
    grid.append(field("name", "Manuscript directory name", { value: options.name || "", help: "Leave blank for the derived name." }));
  }
  if (task.action === "revise") {
    grid.append(field("authors", "Override authors", { type: "textarea", value: Array.isArray(options.authors) ? options.authors.join("\n") : options.authors || "", help: "Leave blank to inherit." }));
    grid.append(field("title", "Override title direction", { value: options.title || "" }));
    grid.append(checkbox("refreshResults", "Refresh result selection", "Promote stored selectors to paper scope and use latest attempts.", options.refreshResults));
  }
  grid.append(field("priorityLevel", "Scheduling share", {
    type: "select",
    value: options.priorityLevel ?? "0",
    options: priorityOptions(),
    help: "Relative share of worker starts when this task competes with other eligible tasks.",
  }));

  const advanced = node("details", "advanced");
  advanced.append(node("summary", "", "Model and web-search settings"));
  const advancedGrid = node("div", "form-grid");
  advancedGrid.append(field("model", "Model", { value: options.model || "", help: "Blank uses the CLI default." }));
  advancedGrid.append(field("reasoningEffort", "Reasoning effort", {
    type: "select", value: options.reasoningEffort || "",
    options: [["", "CLI default"], ...["low", "medium", "high", "xhigh", "max", "ultra"].map(value => [value, value])],
  }));
  advancedGrid.append(checkbox("fast", "Fast service tier", "Uses additional credits.", options.fast));
  if (["literature", "solve", "review", "write", "revise"].includes(task.action)) {
    advancedGrid.append(field("webSearch", "Web search", {
      type: "select", value: options.webSearch || "",
      options: [["", "CLI default"], ["live", "Live"], ["indexed", "Indexed"], ["disabled", "Disabled"]],
    }));
  }
  if (["solve", "write", "revise"].includes(task.action)) {
    advancedGrid.append(field("reviewModel", "Critic model", { value: options.reviewModel || "", help: "Blank inherits the primary model." }));
    advancedGrid.append(field("reviewReasoningEffort", "Critic reasoning", {
      type: "select", value: options.reviewReasoningEffort || "",
      options: [["", "Inherit"], ...["low", "medium", "high", "xhigh", "max", "ultra"].map(value => [value, value])],
    }));
    advancedGrid.append(field("reviewWebSearch", "Critic web search", {
      type: "select", value: options.reviewWebSearch || "",
      options: [["", "Inherit"], ["live", "Live"], ["indexed", "Indexed"], ["disabled", "Disabled"]],
    }));
  }
  advanced.append(advancedGrid);
  grid.append(advanced);
  dialogBody.append(grid);
  grid.querySelectorAll("input, textarea, select").forEach(input => input.addEventListener("input", saveDialogOptions));
  dialogFooter.replaceChildren(
    button("Cancel", () => dialog.close()),
    button("Review task", reviewTask, "button primary"),
  );
}

function collectDialogOptions() {
  const options = {};
  dialogBody.querySelectorAll("[name]").forEach(input => {
    if (input.type === "checkbox") options[input.name] = input.checked;
    else if (input.name === "authors") options.authors = input.value.split("\n").map(value => value.trim()).filter(Boolean);
    else if (input.value !== "") options[input.name] = input.value;
  });
  return options;
}

function saveDialogOptions() {
  if (!state.dialog) return;
  state.dialog.options = collectDialogOptions();
  sessionStorage.setItem(state.dialog.storageKey, JSON.stringify(state.dialog.options));
}

async function reviewTask() {
  const task = state.dialog;
  saveDialogOptions();
  dialogFooter.querySelectorAll("button").forEach(value => value.disabled = true);
  const reviewButton = dialogFooter.querySelector(".primary");
  if (reviewButton) reviewButton.textContent = "Running dry-run previews…";
  dialog.setAttribute("aria-busy", "true");
  try {
    task.plan = await api("/api/plans", {
      method: "POST",
      body: { action: task.action, targets: task.targets, options: task.options },
    });
    renderTaskConfirmation();
  } catch (error) {
    renderTaskConfiguration(error.message);
  } finally {
    dialog.removeAttribute("aria-busy");
  }
}

function renderTaskConfirmation() {
  const task = state.dialog;
  const plan = task.plan;
  dialogEyebrow.textContent = "Step 2 of 2 · Confirm";
  dialogTitle.textContent = plan.title;
  dialogBody.replaceChildren();
  const intro = node("p", "", `This will queue ${plan.units.length} managed run${plan.units.length === 1 ? "" : "s"} with a ${priorityMultiplier(plan.priorityLevel)} scheduling share. Nothing has started yet.`);
  dialogBody.append(intro);
  if (plan.warnings.length) plan.warnings.forEach(value => dialogBody.append(node("div", "warning", value)));
  if (Object.keys(plan.prompts).length) {
    const block = node("section", "confirm-block");
    block.append(node("h3", "", "Prompt messages"));
    Object.entries(plan.prompts).forEach(([name, value]) => {
      block.append(node("div", "eyebrow", name === "prompt" ? promptLabel(task.action) : "Critic direction"));
      block.append(node("pre", "command", value));
    });
    dialogBody.append(block);
  }
  plan.units.forEach((unit, index) => {
    const block = node("section", "confirm-block");
    const presentation = problemRunPresentation(plan.action, unit);
    block.append(node("h3", "", `${index + 1}. ${presentation.title}`));
    if (presentation.targets.length) {
      block.append(node("div", "confirm-target-summary", presentation.summary));
    }
    block.append(node("div", "eyebrow", "Command"));
    block.append(node("pre", "command", unit.command));
    const preview = unit.dryRun;
    const previewHeading = node("div", "dry-run-heading");
    previewHeading.append(node("span", "eyebrow", "Dry-run preview"));
    if (preview) {
      const statusLabels = {
        ok: "Succeeded",
        failed: `Exited ${preview.exitCode}`,
        timeout: "Timed out",
        error: "Could not start",
      };
      previewHeading.append(node("span", `dry-run-status ${preview.status}`, statusLabels[preview.status] || humanize(preview.status)));
    }
    block.append(previewHeading);
    block.append(node(
      "pre",
      `command dry-run-output${preview && preview.status !== "ok" ? " dry-run-error" : ""}`,
      preview?.output || "No dry-run preview was returned.",
    ));
    dialogBody.append(block);
  });
  dialogFooter.replaceChildren(
    button("Back", () => renderTaskConfiguration()),
    button(`Start ${plan.units.length} run${plan.units.length === 1 ? "" : "s"}`, confirmTask, "button primary"),
  );
}

async function confirmTask() {
  const task = state.dialog;
  dialogFooter.querySelectorAll("button").forEach(value => value.disabled = true);
  try {
    const job = await api("/api/jobs", { method: "POST", body: { planId: task.plan.id } });
    sessionStorage.removeItem(task.storageKey);
    dialog.close();
    state.dialog = null;
    state.selectedJob = job.id;
    state.selection.clear();
    await refreshJobs();
    setTab("activity");
  } catch (error) {
    task.plan = null;
    renderTaskConfiguration(error.message);
  }
}

async function refreshCatalog() {
  state.catalog = await api("/api/catalog");
  state.detailCache.clear();
  if (navigationReady) syncNavigation({ replace: true, preserveScroll: true });
  else render();
}

async function refreshJobs({ preserveActivityDetail = false } = {}) {
  const value = await api("/api/jobs");
  state.jobs = value.jobs;
  updateActivityCount();
  if (!navigationReady || state.tab !== "activity") return;
  rememberSidebarScroll();
  renderActivity({ preserveDetail: preserveActivityDetail });
  restoreSidebarScroll("activity");
  const url = currentUrl();
  history.replaceState(historyPayload(window.scrollY), "", url);
  renderedUrl = url;
}

function connectEvents() {
  const previousConnection = eventConnection;
  eventConnection = null;
  previousConnection?.close();
  const events = new EventSource(`/api/events?${new URLSearchParams({
    since: String(state.eventSequence || 0),
  })}`);
  eventConnection = events;
  events.addEventListener("open", () => {
    if (eventConnection !== events) return;
    connection.textContent = "Live";
    connection.className = "connection live";
    if (eventReconnectNeedsRefresh) {
      eventReconnectNeedsRefresh = false;
      refreshSession().catch(error => showNotice(error.message, true));
    }
  });
  events.addEventListener("error", () => {
    if (eventConnection !== events) return;
    eventReconnectNeedsRefresh = true;
    connection.textContent = "Reconnecting…";
    connection.className = "connection";
  });
  events.addEventListener("update", event => {
    try {
      const value = JSON.parse(event.data);
      if (value.type === "catalog.progress") {
        state.catalog.loading = true;
        state.catalog.progress = value;
        if (state.catalog.version) renderCatalogLoading();
        else render();
      } else if (value.type.startsWith("catalog.")) {
        refreshCatalog().catch(error => showNotice(error.message, true));
      }
      if (value.type === "tasks.changed") {
        refreshJobs({ preserveActivityDetail: true })
          .catch(error => showNotice(error.message, true));
      }
      if (value.type === "settings.changed") {
        state.settings = { ...state.settings, ...value };
        renderSchedulerControl();
      }
    } catch (error) {
      console.warn("Invalid live event", error);
    }
  });
}

async function start() {
  try {
    const value = await api("/api/bootstrap");
    state.csrf = value.csrf;
    state.eventSequence = value.eventSequence || 0;
    state.catalog = value.catalog;
    state.jobs = value.jobs;
    state.settings = value.settings || state.settings;
    navigationReady = true;
    applyLocation({
      scrollY: history.state?.looseEndsWorkbench ? history.state.scrollY : 0,
    });
    connectEvents();
  } catch (error) {
    main.replaceChildren(node("div", "error-box", `Could not start workbench: ${error.message}`));
  }
}

window.addEventListener("scroll", () => {
  if (scrollUpdateFrame !== null) return;
  scrollUpdateFrame = requestAnimationFrame(() => {
    scrollUpdateFrame = null;
    rememberCurrentScroll();
  });
}, { passive: true });

window.addEventListener("popstate", event => {
  if (!navigationReady) return;
  rememberCurrentScroll({ updateHistory: false });
  applyLocation({
    scrollY: event.state?.looseEndsWorkbench ? event.state.scrollY : undefined,
  });
});

start();
