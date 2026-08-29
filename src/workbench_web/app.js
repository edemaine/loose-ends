"use strict";

const reviewModel = window.LooseEndsReviewModel;
if (!reviewModel) throw new Error("Shared review model failed to load");

const state = {
  csrf: "",
  eventSequence: 0,
  catalog: { papers: [], reviews: [], manuscripts: [], counts: {} },
  jobs: [],
  settings: {
    workerLimit: 2,
    queuePaused: false,
    queueManuallyPaused: false,
    memoryLimit: { mode: "percent", value: 50 },
    paperRoots: [],
    taskDefaults: {},
  },
  tab: "research",
  search: "",
  selectedReview: "",
  selectedProblem: "",
  researchFilters: reviewModel.createDefaultFilters(),
  researchFiltersOpen: false,
  revealSidebarSelection: false,
  revealSidebarSecondarySelection: false,
  sidebarScroll: { research: 0, papers: 0, manuscripts: 0, activity: 0 },
  sidebarSecondaryScroll: { research: 0, manuscripts: 0 },
  paperSort: "alphabetical",
  manuscriptSort: "latest",
  selectedPaper: "",
  selectedManuscript: "",
  selectedDraft: "",
  manuscriptDraftSelections: new Map(),
  selectedJob: "",
  detailTab: "attempt",
  detailCache: new Map(),
  detailCacheVersions: new Map(),
  detailLoads: new Map(),
  selection: new Map(),
  jobDetails: new Map(),
  runLogs: new Map(),
  runLogLoads: new Map(),
  expandedRuns: new Set(),
  expandedJobScopes: new Set(),
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
const workerDecrease = document.getElementById("worker-decrease");
const workerIncrease = document.getElementById("worker-increase");
const workerStatus = document.getElementById("worker-status");
const queueToggle = document.getElementById("queue-toggle");
const memoryMode = document.getElementById("memory-mode");
const memoryValueLabel = document.getElementById("memory-value-label");
const memoryStepper = document.getElementById("memory-stepper");
const memoryValue = document.getElementById("memory-value");
const memoryDecrease = document.getElementById("memory-decrease");
const memoryIncrease = document.getElementById("memory-increase");
const memoryStatus = document.getElementById("memory-status");
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
const initialPriorities = [...reviewModel.priorityLevels];
const pageScrollPositions = new Map();
const sidebarControlNodes = new Map();
let renderedUrl = "";
let scrollUpdateFrame = null;
let navigationReady = false;
let eventConnection = null;
let eventReconnectNeedsRefresh = false;
let sessionRefresh = null;
let workerLimitDraft = null;
let workerLimitTimer = null;
let workerLimitSaving = false;
let memoryLimitDraft = null;
let memoryLimitTimer = null;
let memoryLimitSaving = false;
let renderScrollTarget = null;
const priorityLevels = [
  [-3, "⅛×"],
  [-2, "¼×"],
  [-1, "½×"],
  [0, "1×"],
  [1, "2×"],
  [2, "4×"],
  [3, "8×"],
];
const manuscriptSortOptions = [
  ["latest", "Latest drafts"],
  ["alphabetical", "Alphabetical"],
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
  if (state.tab === "manuscripts" && state.manuscriptSort !== "latest") {
    parameters.set("sort", state.manuscriptSort);
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
  const preservedScrollY = window.scrollY;
  rememberCurrentScroll();
  const previousUrl = currentUrl();
  renderScrollTarget = preserveScroll
    ? preservedScrollY
    : pageScrollPositions.get(scrollPositionKey(previousUrl)) ?? 0;
  render();
  renderScrollTarget = null;
  const url = currentUrl();
  const scrollY = preserveScroll
    ? preservedScrollY
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
  state.revealSidebarSecondarySelection = false;
  state.search = parameters.get("q") || "";
  state.paperSort = reviewModel.normalizePaperSort(parameters.get("sort"));
  state.manuscriptSort = normalizeManuscriptSort(parameters.get("sort"));
  if (state.tab === "research") {
    state.researchFilters = reviewModel.filtersFromSearchParams(parameters, initialPriorities);
    const identity = reviewModel.identityFromSearchParams(parameters);
    const legacy = decodeURIComponent(location.hash.slice(1));
    const requested = reviewModel.findReviewItem(state.catalog.reviews, identity) ||
      state.catalog.reviews.find(item => item.id === legacy || item.itemKey === legacy);
    state.selectedReview = requested?.itemKey || "";
    state.selectedProblem = requested?.problemKey || "";
    state.revealSidebarSelection = Boolean(requested);
    state.revealSidebarSecondarySelection = Boolean(requested);
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
    if (manuscript && state.selectedDraft) {
      state.manuscriptDraftSelections.set(manuscript.key, state.selectedDraft);
      state.revealSidebarSecondarySelection = true;
    }
  } else {
    state.selectedJob = parameters.get("job") || "";
    state.revealSidebarSelection = state.jobs.some(
      job => job.id === state.selectedJob,
    );
  }
  const requestedScroll = Number.isFinite(scrollY)
    ? scrollY
    : pageScrollPositions.get(scrollPositionKey(location.href)) ?? 0;
  renderScrollTarget = requestedScroll;
  render();
  renderScrollTarget = null;
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

const unsuccessfulRunStatuses = ["partial", "failed", "canceled", "interrupted"];

function latestJobRuns(job) {
  const latest = new Map();
  (job.runs || []).forEach(run => {
    const key = Number.isInteger(run.unit_index) ? `unit:${run.unit_index}` : `run:${run.id}`;
    const previous = latest.get(key);
    if (!previous || Number(run.created_at) >= Number(previous.created_at)) latest.set(key, run);
  });
  return [...latest.values()].sort((left, right) =>
    (Number(left.unit_index) - Number(right.unit_index)) ||
    (Number(left.created_at) - Number(right.created_at))
  );
}

function jobOutcomeCounts(job) {
  if (Number(job.counts?.total)) {
    return {
      succeeded: Number(job.counts.succeeded) || 0,
      partial: Number(job.counts.partial) || 0,
      failed: Number(job.counts.failed) || 0,
      canceled: Number(job.counts.canceled) || 0,
      interrupted: Number(job.counts.interrupted) || 0,
    };
  }
  const counts = { succeeded: 0, partial: 0, failed: 0, canceled: 0, interrupted: 0 };
  latestJobRuns(job).forEach(run => {
    if (run.status in counts) counts[run.status] += 1;
  });
  return counts;
}

function appendRunCountBadge(parent, count, status) {
  if (!count) return;
  const value = taskStatus(status);
  const result = badge(`${count} ${value.label.toLowerCase()}`, value.tone);
  result.title = `${count} ${value.label.toLowerCase()} run${count === 1 ? "" : "s"}`;
  parent.append(result);
}

function taskBadges(job) {
  const values = node("span", "task-badges");
  const terminal = ["succeeded", "partial", "failed", "canceled", "interrupted"].includes(job.status);
  const counts = jobOutcomeCounts(job);
  const outcomeKinds = [counts.succeeded, ...unsuccessfulRunStatuses.map(status => counts[status])]
    .filter(Boolean).length;
  const mixedTerminal = terminal && outcomeKinds > 1;
  if (taskIsPaused(job)) values.append(badge("Paused", "paused"));
  else if (mixedTerminal) {
    unsuccessfulRunStatuses.forEach(status => appendRunCountBadge(values, counts[status], status));
    appendRunCountBadge(values, counts.succeeded, "succeeded");
  } else {
    values.append(taskStatusBadge(job.status));
    if (!terminal) unsuccessfulRunStatuses.forEach(status => appendRunCountBadge(values, counts[status], status));
  }
  if (!terminal) {
    values.append(badge(`Weight ${priorityMultiplier(job.priority_level)}`, "neutral"));
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
  } else if (status.active && run.started_at) {
    const elapsed = node(
      "span",
      "run-elapsed",
      `Elapsed ${formatDuration(run.started_at, Date.now() / 1000)}`,
    );
    elapsed.dataset.runElapsed = run.id;
    row.append(node("span", "run-timing-separator", "·"), elapsed);
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
  } else if (status.active && run.started_at) {
    const elapsed = node(
      "span",
      "console-status-detail run-elapsed",
      `Elapsed ${formatDuration(run.started_at, Date.now() / 1000)}`,
    );
    elapsed.dataset.runElapsed = run.id;
    footer.append(elapsed);
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

function downloadFileUrl(path, name) {
  return `/api/file?${new URLSearchParams({ path, download: "1", name })}`;
}

function manuscriptZipUrl(path) {
  return `/api/manuscript-zip?${new URLSearchParams({ path })}`;
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
    invalidateReviewDetails();
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
  if (options.body && typeof options.body !== "string" && !(options.body instanceof Blob)) {
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
  return target(
    "attempt",
    item.attemptDirectory,
    `${item.problemId}: ${item.problemTitle} · ${item.attemptName}`,
  );
}

function catalogAttemptForTarget(value) {
  if (value.kind !== "attempt") return null;
  const path = normalizedPath(value.path);
  return state.catalog.reviews.find(item =>
    item.attemptDirectory && normalizedPath(item.attemptDirectory) === path
  ) || null;
}

function historicalAttemptTarget(value) {
  const selected = catalogAttemptForTarget(value);
  if (!selected) return false;
  const selectedNumber = Number(selected.attemptNumber) || 0;
  return state.catalog.reviews.some(item =>
    item.problemKey === selected.problemKey &&
    (Number(item.attemptNumber) || 0) > selectedNumber
  );
}

function problemTargetForAttempt(value) {
  const item = catalogAttemptForTarget(value);
  if (item) return problemTarget(item);
  const path = value.path.replace(/[\\/]+$/, "");
  const separator = Math.max(path.lastIndexOf("/"), path.lastIndexOf("\\"));
  return target(
    "problem",
    separator >= 0 ? path.slice(0, separator) : path,
    (value.label || "Problem").replace(/\s+·\s+attempt-\d+.*$/i, ""),
  );
}

function taskTargetsForRequest(task) {
  if (task.action !== "write" || task.options.pinAttempts === true) {
    return task.targets;
  }
  const targets = task.targets.map(value =>
    value.kind === "attempt" ? problemTargetForAttempt(value) : value
  );
  return [...new Map(targets.map(value => [targetKey(value), value])).values()];
}

function targetDisplayLabel(value) {
  if (value.kind === "attempt") {
    const attemptPath = normalizedPath(value.path);
    const item = state.catalog.reviews.find(review =>
      review.attemptDirectory && normalizedPath(review.attemptDirectory) === attemptPath
    );
    if (item) return `${item.problemId}: ${item.problemTitle} · ${item.attemptName}`;
  }
  if (value.kind === "paper") {
    const path = normalizedPath(value.path);
    const paper = state.catalog.papers.find(item => normalizedPath(item.path) === path);
    if (paper) {
      return reviewModel.paperTitleWithYear(paper.title, paper.published);
    }
  }
  return value.label || value.path;
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

function visiblePaperTargets() {
  const query = state.search.trim().toLowerCase();
  return state.catalog.papers
    .filter(paper => !query || `${paper.title} ${paper.name} ${paper.authors.join(" ")}`.toLowerCase().includes(query))
    .map(paperTarget);
}

function updateVisibleSelectionControl(input, targets, noun) {
  if (!input) return;
  const selected = targets.filter(value => state.selection.has(targetKey(value))).length;
  input.checked = targets.length > 0 && selected === targets.length;
  input.indeterminate = selected > 0 && selected < targets.length;
  input.disabled = targets.length === 0;
  input.setAttribute("aria-label", `${input.checked ? "Clear" : "Select"} all ${targets.length} visible ${noun}`);
  const label = input.closest("label");
  label?.classList.toggle("disabled", input.disabled);
  const copy = label?.querySelector("span");
  if (copy) copy.textContent = `Select visible (${targets.length.toLocaleString()})`;
}

function updateVisibleProblemSelectionControl(input) {
  updateVisibleSelectionControl(input, visibleProblemTargets(), "problems");
}

function updateVisiblePaperSelectionControl(input) {
  updateVisibleSelectionControl(input, visiblePaperTargets(), "papers");
}

function syncSelectionControls() {
  document.querySelectorAll("input[data-selection-key]").forEach(input => {
    input.checked = state.selection.has(input.dataset.selectionKey);
  });
  updateVisibleProblemSelectionControl(
    document.querySelector("input[data-select-visible-problems]"),
  );
  updateVisiblePaperSelectionControl(
    document.querySelector("input[data-select-visible-papers]"),
  );
}

function visibleSelectionControl(targets, datasetKey, update) {
  const label = node("label", "select-visible");
  const input = node("input");
  input.type = "checkbox";
  input.dataset[datasetKey] = "";
  input.addEventListener("click", event => event.stopPropagation());
  input.addEventListener("change", () => {
    targets().forEach(value => {
      const key = targetKey(value);
      if (input.checked) state.selection.set(key, value);
      else state.selection.delete(key);
    });
    syncSelectionControls();
    renderSelectionBar();
  });
  label.addEventListener("click", event => event.stopPropagation());
  label.append(input, node("span"));
  update(input);
  return label;
}

function visibleProblemSelectionControl() {
  return visibleSelectionControl(
    visibleProblemTargets,
    "selectVisibleProblems",
    updateVisibleProblemSelectionControl,
  );
}

function visiblePaperSelectionControl() {
  return visibleSelectionControl(
    visiblePaperTargets,
    "selectVisiblePapers",
    updateVisiblePaperSelectionControl,
  );
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

function missingMetadataPaperTargets(values) {
  const selectedPaths = new Set(
    values.filter(value => value.kind === "paper").map(value => normalizedPath(value.path)),
  );
  return state.catalog.papers
    .filter(paper => selectedPaths.has(normalizedPath(paper.path)) && !paper.metadataComplete)
    .map(paperTarget);
}

function appendMissingMetadataAction(values) {
  const papers = missingMetadataPaperTargets(values);
  if (!papers.length) return;
  const action = button(
    `Extract metadata (${papers.length.toLocaleString()})`,
    () => openTask("metadata", papers),
  );
  action.title = `${papers.length.toLocaleString()} selected paper${papers.length === 1 ? "" : "s"} missing a title or authors`;
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
    appendMissingMetadataAction(values);
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
  sidebar.classList.toggle("split-sidebar", ["research", "manuscripts"].includes(state.tab));
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

function formatMemoryGB(bytes) {
  if (bytes == null || !Number.isFinite(Number(bytes))) return "unknown";
  const value = Number(bytes) / (1024 ** 3);
  const digits = value >= 100 ? 0 : value >= 10 ? 1 : 2;
  return `${Number(value.toFixed(digits))} GB`;
}

function formatMemoryUsage(bytes) {
  if (bytes == null || !Number.isFinite(Number(bytes))) return "unknown";
  const value = Number(bytes);
  if (value < 1024 ** 3) {
    const mb = value / (1024 ** 2);
    const digits = mb >= 100 ? 0 : mb >= 10 ? 1 : 2;
    return `${Number(mb.toFixed(digits))} MB`;
  }
  return formatMemoryGB(value);
}

function configuredMemoryLimit() {
  const memory = state.settings.memoryLimit;
  if (!memory || !["percent", "gb", "unlimited"].includes(memory.mode)) {
    return { mode: "percent", value: 50 };
  }
  return {
    mode: memory.mode,
    value: memory.mode === "unlimited" ? null : Number(memory.value),
  };
}

function renderMemoryControl(maximumWorkers) {
  const configured = configuredMemoryLimit();
  const displayed = memoryLimitDraft || configured;
  memoryMode.querySelectorAll("[data-memory-mode]").forEach(control => {
    const active = control.dataset.memoryMode === displayed.mode;
    control.classList.toggle("active", active);
    control.setAttribute("aria-pressed", String(active));
    control.disabled = memoryLimitSaving;
  });
  const unlimited = displayed.mode === "unlimited";
  memoryValueLabel.hidden = !unlimited;
  memoryStepper.hidden = unlimited;
  memoryValue.step = displayed.mode === "percent" ? "5" : "1";
  memoryValue.max = displayed.mode === "percent" ? "10000" : "100000";
  if (!unlimited && document.activeElement !== memoryValue) {
    memoryValue.value = String(displayed.value);
  }
  const minimum = 0.1;
  memoryValue.disabled = memoryLimitSaving || unlimited;
  memoryDecrease.disabled = memoryLimitSaving || unlimited || displayed.value <= minimum;
  memoryIncrease.disabled = memoryLimitSaving || unlimited;

  const telemetry = state.settings.memory || {};
  if (telemetry.error && state.settings.memoryLimitPending) {
    memoryStatus.textContent = `Queue paused because the memory limit could not be enforced: ${telemetry.error}`;
    return;
  }
  if (!telemetry.available) {
    const policy = unlimited
      ? "Configured total allocation limit: unlimited."
      : displayed.mode === "percent"
        ? `Configured total allocation limit: ${displayed.value}% of installed RAM.`
        : `Configured total allocation limit: ${displayed.value} GB.`;
    memoryStatus.textContent = `${policy} ${telemetry.error || "Enforcement status unavailable."}`;
    return;
  }
  if (unlimited) {
    memoryStatus.textContent = telemetry.currentBytes == null
      ? "Total allocation limit: unlimited.\nPer-worker limit: unlimited."
      : `Total allocation limit: unlimited.\nPer-worker limit: unlimited.\nCurrent actual usage: ${formatMemoryUsage(telemetry.currentBytes)}.`;
    return;
  }
  const displayedAllocationBytes = displayed.mode === "percent"
    ? Number(telemetry.physicalBytes) * displayed.value / 100
    : displayed.value * (1024 ** 3);
  const allocation = formatMemoryGB(displayedAllocationBytes);
  const perWorker = formatMemoryUsage(displayedAllocationBytes / maximumWorkers);
  const allocationBasis = displayed.mode === "percent"
    ? `${displayed.value}% of ${formatMemoryGB(telemetry.physicalBytes)} installed = ${allocation}`
    : allocation;
  const workerBasis = `${perWorker} (${allocation} ÷ ${maximumWorkers} maximum workers)`;
  const activeWorkers = schedulerCounts().active;
  const managedWorkers = Number(telemetry.managedWorkers);
  let coverageNote = "";
  if (telemetry.allocationBytes === undefined) {
    coverageNote = "\nRestart the workbench to enforce per-worker limits.";
  } else if (Number.isFinite(managedWorkers) && managedWorkers < activeWorkers) {
    const existing = activeWorkers - managedWorkers;
    coverageNote = existing === 1
      ? "\n1 existing active worker predates per-worker enforcement and is not included in current actual usage."
      : `\n${existing} existing active workers predate per-worker enforcement and are not included in current actual usage.`;
  }
  if (state.settings.memoryLimitPending) {
    memoryStatus.textContent = `Requested total allocation limit: ${allocationBasis}.\nRequested per-worker limit: ${workerBasis}.\nCurrent actual usage: ${formatMemoryUsage(telemetry.currentBytes)}; waiting for every worker to fall below its new limit.${coverageNote}`;
  } else if (telemetry.currentBytes != null) {
    memoryStatus.textContent = `Total allocation limit: ${allocationBasis}.\nPer-worker limit: ${workerBasis}.\nCurrent actual usage: ${formatMemoryUsage(telemetry.currentBytes)}.${coverageNote}`;
  } else {
    memoryStatus.textContent = `Total allocation limit: ${allocationBasis}.\nPer-worker limit: ${workerBasis}.${coverageNote}`;
  }
}

function renderSchedulerControl() {
  const configuredLimit = Number(state.settings.workerLimit) || 2;
  const displayedLimit = workerLimitDraft ?? configuredLimit;
  const counts = schedulerCounts();
  workerSummary.textContent = `${state.settings.queuePaused ? "Queue paused" : "Workers"} ${counts.active}/${configuredLimit}`;
  workerLimit.textContent = String(displayedLimit);
  workerDecrease.disabled = workerLimitSaving || displayedLimit <= 1;
  workerIncrease.disabled = workerLimitSaving || displayedLimit >= 64;
  const manuallyPaused = state.settings.queueManuallyPaused ?? state.settings.queuePaused;
  queueToggle.textContent = manuallyPaused
    ? "Resume queue"
    : state.settings.memoryLimitPending
      ? "Pause manually"
      : "Pause queue";
  if (state.settings.memoryLimitPending && state.settings.memory?.error) {
    workerStatus.textContent = `${counts.active} active; ${counts.queued} waiting. Queue paused because memory enforcement failed.`;
  } else if (state.settings.memoryLimitPending) {
    workerStatus.textContent = `${counts.active} active; ${counts.queued} waiting. Queue paused until the lower memory limit can be applied.`;
  } else if (state.settings.queuePaused) {
    workerStatus.textContent = `${counts.active} active; ${counts.queued} waiting. Active runs continue.`;
  } else if (counts.active > configuredLimit) {
    workerStatus.textContent = `Draining ${counts.active} active runs to the new limit of ${configuredLimit}.`;
  } else {
    workerStatus.textContent = `${counts.active} active; ${counts.queued} waiting.`;
  }
  renderMemoryControl(displayedLimit);
}

async function updateScheduler(changes) {
  try {
    state.settings = await api("/api/scheduler", { method: "POST", body: changes });
    renderSchedulerControl();
  } catch (error) {
    showNotice(error.message, true);
  }
}

function adjustWorkerLimit(delta) {
  if (workerLimitSaving) return;
  const current = (workerLimitDraft ?? Number(state.settings.workerLimit)) || 2;
  const next = Math.max(1, Math.min(64, current + delta));
  if (next === current) return;
  workerLimitDraft = next;
  renderSchedulerControl();
  clearTimeout(workerLimitTimer);
  workerLimitTimer = setTimeout(async () => {
    const limit = workerLimitDraft;
    workerLimitSaving = true;
    renderSchedulerControl();
    try {
      state.settings = await api("/api/scheduler", {
        method: "POST",
        body: { workerLimit: limit },
      });
      workerLimitDraft = null;
    } catch (error) {
      workerLimitDraft = null;
      showNotice(error.message, true);
    } finally {
      workerLimitSaving = false;
      renderSchedulerControl();
    }
  }, 2000);
}

function equivalentMemoryValue(mode, current) {
  if (current.mode === mode && Number.isFinite(current.value)) return current.value;
  const physical = Number(state.settings.memory?.physicalBytes);
  const allocationValue = state.settings.memory?.allocationBytes;
  const allocation = allocationValue == null ? NaN : Number(allocationValue);
  if (mode === "gb" && Number.isFinite(allocation) && allocation > 0) {
    return Math.max(0.1, Number((allocation / (1024 ** 3)).toFixed(1)));
  }
  if (mode === "percent" && physical > 0 && Number.isFinite(allocation) && allocation > 0) {
    return Math.max(0.1, Number((allocation * 100 / physical).toFixed(1)));
  }
  return mode === "percent" ? 50 : 8;
}

function scheduleMemoryLimit(memory) {
  memoryLimitDraft = memory;
  renderSchedulerControl();
  clearTimeout(memoryLimitTimer);
  memoryLimitTimer = setTimeout(async () => {
    const limit = memoryLimitDraft;
    memoryLimitSaving = true;
    renderSchedulerControl();
    try {
      state.settings = await api("/api/scheduler", {
        method: "POST",
        body: { memoryLimit: limit },
      });
      memoryLimitDraft = null;
    } catch (error) {
      memoryLimitDraft = null;
      showNotice(error.message, true);
    } finally {
      memoryLimitSaving = false;
      renderSchedulerControl();
    }
  }, 2000);
}

function setMemoryMode(mode) {
  if (memoryLimitSaving) return;
  const current = memoryLimitDraft || configuredMemoryLimit();
  if (current.mode === mode) return;
  scheduleMemoryLimit({
    mode,
    value: mode === "unlimited" ? null : equivalentMemoryValue(mode, current),
  });
}

function setMemoryValue(rawValue) {
  if (memoryLimitSaving) return;
  if (String(rawValue).trim() === "") return;
  const current = memoryLimitDraft || configuredMemoryLimit();
  if (current.mode === "unlimited") return;
  const maximum = current.mode === "percent" ? 10000 : 100000;
  const value = Math.max(0.1, Math.min(maximum, Number(rawValue)));
  if (!Number.isFinite(value)) return;
  scheduleMemoryLimit({ mode: current.mode, value });
}

function adjustMemoryLimit(direction) {
  const current = memoryLimitDraft || configuredMemoryLimit();
  if (current.mode === "unlimited") return;
  const step = current.mode === "percent" ? 5 : 1;
  setMemoryValue(Number((current.value + direction * step).toFixed(1)));
}

workerDecrease.addEventListener("click", () => adjustWorkerLimit(-1));
workerIncrease.addEventListener("click", () => adjustWorkerLimit(1));
memoryDecrease.addEventListener("click", () => adjustMemoryLimit(-1));
memoryIncrease.addEventListener("click", () => adjustMemoryLimit(1));
memoryValue.addEventListener("input", () => setMemoryValue(memoryValue.value));
memoryMode.addEventListener("click", event => {
  const control = event.target.closest("[data-memory-mode]");
  if (control) setMemoryMode(control.dataset.memoryMode);
});
queueToggle.addEventListener("click", () => updateScheduler({
  queuePaused: !(state.settings.queueManuallyPaused ?? state.settings.queuePaused),
}));
document.addEventListener("pointerdown", event => {
  if (schedulerControl.open && !schedulerControl.contains(event.target)) {
    schedulerControl.open = false;
  }
});

function rememberSidebarScroll() {
  const renderedTab = sidebar.dataset.tab;
  if (!(renderedTab in state.sidebarScroll)) return;
  const primarySelector = {
    research: ".problem-scroll",
    manuscripts: ".manuscript-scroll",
  }[renderedTab];
  const scrollingElement = primarySelector ? sidebar.querySelector(primarySelector) : sidebar;
  if (scrollingElement) state.sidebarScroll[renderedTab] = scrollingElement.scrollTop;
  const secondarySelector = {
    research: ".attempt-list",
    manuscripts: ".draft-list",
  }[renderedTab];
  const secondary = secondarySelector ? sidebar.querySelector(secondarySelector) : null;
  if (secondary) state.sidebarSecondaryScroll[renderedTab] = secondary.scrollTop;
}

function revealCentered(scrollingElement) {
  const selected = scrollingElement?.querySelector(".side-card.active");
  if (!selected) return;
  const viewport = scrollingElement.getBoundingClientRect();
  const card = selected.getBoundingClientRect();
  const centered = scrollingElement.scrollTop +
    (card.top + card.bottom - viewport.top - viewport.bottom) / 2;
  const maximum = scrollingElement.scrollHeight - scrollingElement.clientHeight;
  scrollingElement.scrollTop = Math.max(0, Math.min(maximum, centered));
}

function restoreSidebarScroll(tab) {
  sidebar.dataset.tab = tab;
  const primarySelector = {
    research: ".problem-scroll",
    manuscripts: ".manuscript-scroll",
  }[tab];
  if (primarySelector) sidebar.scrollTop = 0;
  const scrollingElement = primarySelector ? sidebar.querySelector(primarySelector) : sidebar;
  if (!scrollingElement) return;
  scrollingElement.scrollTop = state.sidebarScroll[tab] || 0;
  if (state.revealSidebarSelection) revealCentered(scrollingElement);
  state.revealSidebarSelection = false;
  state.sidebarScroll[tab] = scrollingElement.scrollTop;

  const secondarySelector = {
    research: ".attempt-list",
    manuscripts: ".draft-list",
  }[tab];
  const secondary = secondarySelector ? sidebar.querySelector(secondarySelector) : null;
  if (!secondary) return;
  secondary.scrollTop = state.sidebarSecondaryScroll[tab] || 0;
  if (state.revealSidebarSecondarySelection) revealCentered(secondary);
  state.revealSidebarSecondarySelection = false;
  state.sidebarSecondaryScroll[tab] = secondary.scrollTop;
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

function normalizeManuscriptSort(value) {
  return manuscriptSortOptions.some(([key]) => key === value)
    ? value
    : "latest";
}

function manuscriptSortControl() {
  const wrapper = node("label", "paper-sort-control");
  wrapper.append(node("span", "", "Sort manuscripts"));
  const select = node("select");
  select.dataset.manuscriptSort = "";
  manuscriptSortOptions.forEach(([value, label]) => {
    const option = node("option", "", label);
    option.value = value;
    option.selected = state.manuscriptSort === value;
    select.append(option);
  });
  select.addEventListener("change", () => {
    state.manuscriptSort = normalizeManuscriptSort(select.value);
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

function appendSideCard(parent, {
  title, meta, tags, active, selectedTarget, onClick, relatedProblem,
  relatedTask,
}) {
  const card = node("div", `side-card${active ? " active" : ""}`);
  card.role = "button";
  card.tabIndex = 0;
  if (selectedTarget) card.append(selectionCheckbox(selectedTarget));
  else card.append(node("span"));
  const copy = node("span");
  const titleRow = node("span", "side-card-title-row");
  const titleNode = node("strong", "", title);
  titleNode.title = title;
  const metaNode = node("small", "", meta);
  metaNode.title = meta;
  titleRow.append(titleNode);
  if (relatedTask || relatedProblem) {
    titleRow.append(relatedTaskHost(relatedTask || {
      paperPath: relatedProblem.paperDirectory,
      problemPath: `${relatedProblem.paperDirectory}/${relatedProblem.problemId}`,
      includePaper: false,
    }));
  }
  copy.append(titleRow);
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
  return card;
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
  const manuscriptSort = controls.querySelector("select[data-manuscript-sort]");
  if (manuscriptSort && manuscriptSort.value !== state.manuscriptSort) {
    manuscriptSort.value = state.manuscriptSort;
  }
  if (tab === "papers") {
    updateVisiblePaperSelectionControl(
      controls.querySelector("input[data-select-visible-papers]"),
    );
  }
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
    const paperHeading = node("div", "sidebar-heading paper-heading");
    const paperHeadingLabel = reviewModel.paperTitleWithYear(
      group.paperTitle,
      group.publicationTimestamp,
    );
    const paperHeadingTitle = node("span", "paper-heading-title", paperHeadingLabel);
    paperHeadingTitle.title = paperHeadingLabel;
    paperHeading.append(
      paperHeadingTitle,
      relatedTaskHost({ paperPath: group.paperDirectory }),
    );
    listScroll.append(paperHeading);
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
        relatedProblem: item,
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
  const cachedDetail = state.detailCache.get(summary.itemKey);
  renderReviewDetail(cachedDetail || summary);
  if (!cachedDetail && Number.isFinite(renderScrollTarget) && renderScrollTarget > 0) {
    const shell = main.querySelector(".main-inner");
    if (shell) shell.style.minHeight = `${renderScrollTarget + window.innerHeight}px`;
  }
  loadReviewDetail(summary);
}

function invalidateReviewDetails() {
  const valid = new Set(state.catalog.reviews.map(item => item.itemKey));
  state.detailCacheVersions.clear();
  state.detailLoads.clear();
  for (const key of state.detailCache.keys()) {
    if (!valid.has(key)) state.detailCache.delete(key);
  }
}

function renderReviewDetailPreservingScroll(detail) {
  const scrollY = window.scrollY;
  rememberCurrentScroll();
  renderReviewDetail(detail);
  restorePageScroll(renderedUrl || currentUrl(), scrollY);
}

function loadReviewDetail(summary) {
  const version = Number(state.catalog.version) || 0;
  if (
    state.detailCache.has(summary.itemKey) &&
    state.detailCacheVersions.get(summary.itemKey) === version
  ) return;
  const existing = state.detailLoads.get(summary.itemKey);
  if (existing?.version === version) return;

  const load = { version };
  state.detailLoads.set(summary.itemKey, load);
  load.promise = api(`/api/review-detail?${new URLSearchParams({ key: summary.itemKey })}`)
    .then(detail => {
      if (state.detailLoads.get(summary.itemKey) !== load) return;
      state.detailCache.set(summary.itemKey, detail);
      state.detailCacheVersions.set(summary.itemKey, version);
      if (state.selectedReview === summary.itemKey && state.tab === "research") {
        renderReviewDetailPreservingScroll(detail);
      }
    })
    .catch(error => {
      if (state.detailLoads.get(summary.itemKey) === load) {
        showNotice(error.message, true);
      }
    })
    .finally(() => {
      if (state.detailLoads.get(summary.itemKey) === load) {
        state.detailLoads.delete(summary.itemKey);
      }
    });
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

function olderVersionWarning(kind, currentName, latestName, route, selectLatest) {
  const warning = node("aside", "version-warning");
  warning.setAttribute("aria-label", `Older ${kind}`);
  const mark = node("span", "version-warning-mark", "!");
  mark.setAttribute("aria-hidden", "true");
  const copy = node("span", "version-warning-copy");
  copy.append(
    node("strong", "", `You’re viewing an older ${kind}.`),
    node("small", "", `${currentName} is selected; the latest is ${latestName}.`),
  );
  const link = node("a", "button version-warning-link", `View latest ${kind}`);
  link.href = routeHref(route);
  link.addEventListener("click", event => {
    if (
      event.defaultPrevented || event.button !== 0 || event.metaKey ||
      event.ctrlKey || event.shiftKey || event.altKey
    ) return;
    event.preventDefault();
    selectLatest();
  });
  warning.append(mark, copy, link);
  return warning;
}

function manuscriptsForProblem(item) {
  const paperPath = normalizedPath(item.paperDirectory);
  return state.catalog.manuscripts.flatMap(manuscript => {
    const drafts = (manuscript.drafts || []).filter(draft =>
      (draft.sources?.problems || []).some(source =>
        source.id === item.problemId &&
        normalizedPath(source.paperPath) === paperPath
      )
    );
    if (!drafts.length) return [];
    const draft = [...drafts].sort((left, right) =>
      Number(right.number || 0) - Number(left.number || 0)
    )[0];
    return [{ manuscript, draft, matchingDraftCount: drafts.length }];
  }).sort((left, right) =>
    left.draft.title.localeCompare(
      right.draft.title,
      undefined,
      { sensitivity: "base", numeric: true },
    )
  );
}

function problemManuscriptsPanel(item) {
  const relations = manuscriptsForProblem(item);
  if (!relations.length) return null;
  const panel = node("section", "problem-manuscripts panel");
  const heading = node("div", "related-tasks-heading");
  heading.append(
    node("h2", "", "Manuscripts about this problem"),
    badge(`${relations.length} manuscript${relations.length === 1 ? "" : "s"}`, "neutral"),
  );
  const grid = node("div", "card-grid");
  relations.forEach(({ manuscript, draft, matchingDraftCount }) => {
    const link = routeLink(
      { tab: "manuscripts", manuscript, draft },
      "",
      "entity-card",
    );
    const draftSummary = matchingDraftCount === 1
      ? draft.name
      : `${matchingDraftCount} matching drafts · latest match ${draft.name}`;
    link.append(
      node("strong", "", draft.title),
      node("small", "", `${manuscript.name} · ${draftSummary}`),
    );
    grid.append(link);
  });
  panel.append(heading, grid);
  return panel;
}

function sourcePaperForProblem(item) {
  const paperPath = normalizedPath(item.paperDirectory);
  return state.catalog.papers.find(
    paper => normalizedPath(paper.path) === paperPath,
  );
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

  const attempts = reviewModel.attemptsForProblem(
    state.catalog.reviews,
    item.problemKey,
  );
  const latestAttempt = attempts[0];
  if (latestAttempt && latestAttempt.itemKey !== item.itemKey) {
    shell.append(olderVersionWarning(
      "attempt",
      item.attemptName || "This attempt",
      latestAttempt.attemptName || "the latest attempt",
      { tab: "research", review: latestAttempt, detail: state.detailTab },
      () => {
        state.selectedReview = latestAttempt.itemKey;
        state.selectedProblem = latestAttempt.problemKey;
        state.revealSidebarSecondarySelection = true;
        syncNavigation();
      },
    ));
  }

  if (item.attemptDisplayPath) shell.append(node("code", "attempt-path", item.attemptDisplayPath));

  const actions = node("div", "actions");
  const problem = problemTarget(item);
  const attempt = item.attemptDirectory ? attemptTarget(item) : null;
  const sourcePaper = sourcePaperForProblem(item);
  if (sourcePaper) {
    actions.append(routeLink(
      { tab: "papers", paper: sourcePaper },
      "View source paper",
      "button",
    ));
  }
  addAction(actions, item.triageCurrent ? "Triage again" : "Triage", "triage", [problem]);
  addAction(actions, item.literatureStatus ? "Search literature again" : "Search literature", "literature", [problem]);
  addAction(actions, attempt ? "Solve again" : "Solve", "solve", [problem], true);
  if (attempt) {
    addAction(actions, item.attemptStatus === "reviewed" ? "Review again" : "Review", "review", [attempt]);
    addAction(actions, "Write this result", "write", [attempt]);
  }
  shell.append(actions);

  shell.append(relatedTasksPanel({
    paperPath: item.paperDirectory,
    problemPath: `${item.paperDirectory}/${item.problemId}`,
  }));
  const manuscriptPanel = problemManuscriptsPanel(item);
  if (manuscriptPanel) shell.append(manuscriptPanel);

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
    link.href = artifactViewUrl(path);
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
    const addFromArxiv = button(
      "Add from arXiv", () => openTask("download", []), "button",
    );
    const addFromFiles = button(
      "Add from files", openFileImport, "button",
    );
    addFromArxiv.disabled = !(state.settings.paperRoots || []).length;
    addFromFiles.disabled = addFromArxiv.disabled;
    if (addFromArxiv.disabled) {
      addFromArxiv.title = "Start the workbench with a parent paper directory to enable downloads.";
      addFromFiles.title = addFromArxiv.title;
    }
    const addActions = node("div", "paper-add-actions");
    addActions.append(addFromArxiv, addFromFiles);
    controls.append(
      sidebarSearch("Search source papers…"),
      paperSortControl(),
      visiblePaperSelectionControl(),
      addActions,
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
    relatedTask: {
      paperPath: paper.path,
      includePaperDescendants: true,
    },
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
  if (paper.published || (paper.updated && paper.updated !== paper.published)) {
    const dates = node("div", "paper-dates");
    if (paper.published) {
      const published = node("time", "", paper.published);
      published.dateTime = paper.published;
      dates.append(node("strong", "", "Published"), published);
    }
    if (paper.updated && paper.updated !== paper.published) {
      const updated = node("time", "", paper.updated);
      updated.dateTime = paper.updated;
      dates.append(node("strong", "", "Revised"), updated);
    }
    copy.append(dates);
  }
  if (paper.arxivId) copy.append(node("div", "paper-identifier", `arXiv:${paper.arxivId}`));
  if (paper.url) {
    const sourceLink = node("a", "paper-source-link", paper.url);
    sourceLink.href = paper.url;
    sourceLink.target = "_blank";
    sourceLink.rel = "noopener noreferrer";
    copy.append(sourceLink);
  }
  const badges = node("div", "badges");
  badges.append(badge(paper.analyzed ? "analyzed" : "not analyzed", paper.analyzed ? "succeeded" : "warn"));
  if (paper.analyzed) badges.append(badge(`${paper.problemCount} problems`, "neutral"));
  copy.append(badges);
  hero.append(copy);
  shell.append(hero);
  const actions = node("div", "actions");
  addAction(
    actions,
    paper.metadataComplete ? "Extract metadata again" : "Extract metadata",
    "metadata",
    [paperTarget(paper)],
    !paper.metadataComplete,
  );
  actions.append(button("Edit metadata", () => openMetadataEditor(paper), "button"));
  addAction(actions, paper.analyzed ? "Analyze again" : "Analyze", "analyze", [paperTarget(paper)], !paper.analyzed);
  const addProblem = button(
    "Add open problem",
    () => openProblemEditor(paper),
    "button",
  );
  addProblem.disabled = !paper.analyzed;
  if (!paper.analyzed) addProblem.title = "Analyze the paper first.";
  actions.append(addProblem);
  const problems = uniqueProblemTargets(paper.path);
  if (problems.length) {
    addAction(actions, "Triage problems", "triage", problems);
    addAction(actions, "Search literature", "literature", problems);
    addAction(actions, "Solve problems", "solve", problems, paper.analyzed);
    addAction(actions, "Write from latest results", "write", [paperTarget(paper)]);
  }
  shell.append(actions);
  shell.append(relatedTasksPanel({
    paperPath: paper.path,
    includePaperDescendants: true,
  }));
  const problemPanel = paperProblemsPanel(paper);
  if (problemPanel) shell.append(problemPanel);
  shell.append(node("section", "section-title", "Files"));
  shell.append(fileGrid(paper.files));
  main.replaceChildren(shell);
}

function paperProblemReviews(paperPath) {
  const normalizedPaper = normalizedPath(paperPath);
  const problems = new Map();
  state.catalog.reviews.forEach(item => {
    if (normalizedPath(item.paperDirectory) !== normalizedPaper) return;
    const previous = problems.get(item.problemKey);
    if (
      !previous ||
      Number(item.attemptNumber || 0) > Number(previous.attemptNumber || 0)
    ) {
      problems.set(item.problemKey, item);
    }
  });
  return [...problems.values()].sort((left, right) =>
    left.problemId.localeCompare(
      right.problemId,
      undefined,
      { numeric: true, sensitivity: "base" },
    )
  );
}

function paperProblemsPanel(paper) {
  const problems = paperProblemReviews(paper.path);
  if (!problems.length) return null;
  const panel = node("section", "paper-problems panel");
  const heading = node("div", "related-tasks-heading");
  heading.append(
    node("h2", "", "Open problems"),
    badge(`${problems.length} problem${problems.length === 1 ? "" : "s"}`, "neutral"),
  );
  const grid = node("div", "card-grid");
  problems.forEach(problem => {
    const link = routeLink(
      { tab: "research", review: problem, detail: "attempt" },
      "",
      "entity-card",
    );
    const attemptStatus = problem.attemptDirectory
      ? reviewModel.statusLabel(problem.attemptStatus)
      : "Unattempted";
    link.append(
      node("strong", "", `${problem.problemId} · ${problem.problemTitle}`),
      node("small", "", `${humanize(problem.explicitness)} · ${attemptStatus}`),
    );
    grid.append(link);
  });
  panel.append(heading, grid);
  return panel;
}

function uniqueProblemTargets(paperPath) {
  const values = new Map();
  state.catalog.reviews.filter(item => item.paperDirectory === paperPath).forEach(item => {
    values.set(item.problemId, problemTarget(item));
  });
  return [...values.values()];
}

function paperImportSize(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(1)} GB`;
}

function loosePaperImportItems(files) {
  return [...files].map(file => {
    const name = file.name.toLowerCase();
    if (!name.endsWith(".pdf") && !name.endsWith(".zip")
        && !name.endsWith(".tar.gz") && !name.endsWith(".tgz")) {
      throw new Error(`File is not a PDF, ZIP, or tar.gz archive: ${file.name}`);
    }
    return { kind: "file", name: file.name, files: [{ file, path: file.name }] };
  });
}

function directoryPickerImportItems(files) {
  const groups = new Map();
  [...files].forEach(file => {
    const parts = String(file.webkitRelativePath || file.name).split("/").filter(Boolean);
    if (parts.length < 2) return;
    const name = parts.shift();
    if (!groups.has(name)) groups.set(name, []);
    groups.get(name).push({ file, path: parts.join("/") });
  });
  return [...groups].map(([name, values]) => ({ kind: "directory", name, files: values }));
}

function fileListPaperImportItems(files) {
  const values = [...files];
  const loose = values.filter(file => !String(file.webkitRelativePath || "").includes("/"));
  const directoryFiles = values.filter(file => String(file.webkitRelativePath || "").includes("/"));
  return [...loosePaperImportItems(loose), ...directoryPickerImportItems(directoryFiles)];
}

function fileForEntry(entry) {
  return new Promise((resolve, reject) => entry.file(resolve, reject));
}

function entriesForDirectory(reader) {
  return new Promise((resolve, reject) => {
    const values = [];
    const read = () => reader.readEntries(entries => {
      if (!entries.length) resolve(values);
      else {
        values.push(...entries);
        read();
      }
    }, reject);
    read();
  });
}

async function filesForDirectoryEntry(directory, prefix = "") {
  const values = [];
  const entries = await entriesForDirectory(directory.createReader());
  for (const entry of entries) {
    const path = prefix ? `${prefix}/${entry.name}` : entry.name;
    if (entry.isDirectory) values.push(...await filesForDirectoryEntry(entry, path));
    else if (entry.isFile) values.push({ file: await fileForEntry(entry), path });
  }
  return values;
}

async function droppedPaperImportItems(transfer) {
  const entries = [...(transfer.items || [])]
    .map(item => item.webkitGetAsEntry?.())
    .filter(Boolean);
  if (!entries.length) return fileListPaperImportItems(transfer.files || []);
  const values = [];
  for (const entry of entries) {
    if (entry.isDirectory) {
      values.push({
        kind: "directory",
        name: entry.name,
        files: await filesForDirectoryEntry(entry),
      });
    } else if (entry.isFile) {
      values.push(...loosePaperImportItems([await fileForEntry(entry)]));
    }
  }
  return values;
}

function appendPaperImportItems(items) {
  const task = state.dialog;
  const names = new Set(task.items.map(item => item.name.toLowerCase()));
  for (const item of items) {
    if (!item.files.length) throw new Error(`Directory is empty: ${item.name}`);
    if (names.has(item.name.toLowerCase())) {
      throw new Error(`An input named ${item.name} is already selected.`);
    }
    names.add(item.name.toLowerCase());
    task.items.push(item);
  }
}

function openFileImport() {
  state.dialog = {
    kind: "fileImport",
    items: [],
    outputDirectory: (state.settings.paperRoots || [])[0] || "",
  };
  renderFileImport();
  dialog.showModal();
}

function renderFileImport(errorMessage = "") {
  const task = state.dialog;
  if (!task || task.kind !== "fileImport") return;
  dialogEyebrow.textContent = "Source papers";
  dialogTitle.textContent = "Add from files";
  dialogBody.replaceChildren();
  if (errorMessage) dialogBody.append(node("div", "error-box", errorMessage));

  const dropzone = node("div", "paper-dropzone");
  dropzone.tabIndex = 0;
  dropzone.append(
    node("strong", "", "Drop PDFs, ZIP/tar.gz archives, and/or folders here"),
    node("small", "", "Archives discard a shared directory prefix. At the resulting root: paper.pdf, then main.pdf, then one PDF matching a TeX filename."),
  );
  const chooser = node("div", "paper-dropzone-actions");
  const pdfInput = node("input");
  pdfInput.type = "file";
  pdfInput.multiple = true;
  pdfInput.accept = ".pdf,.zip,.tar.gz,.tgz,application/pdf,application/zip,application/gzip";
  pdfInput.hidden = true;
  const directoryInput = node("input");
  directoryInput.type = "file";
  directoryInput.multiple = true;
  directoryInput.setAttribute("webkitdirectory", "");
  directoryInput.hidden = true;
  chooser.append(
    button("Choose files", () => pdfInput.click()),
    button("Choose folders", () => directoryInput.click()),
    pdfInput,
    directoryInput,
  );
  dropzone.append(chooser);
  const addItems = values => {
    try {
      appendPaperImportItems(values);
      renderFileImport();
    } catch (error) {
      renderFileImport(error.message);
    }
  };
  pdfInput.addEventListener("change", () => {
    try { addItems(loosePaperImportItems(pdfInput.files)); }
    catch (error) { renderFileImport(error.message); }
  });
  directoryInput.addEventListener("change", () => {
    try { addItems(directoryPickerImportItems(directoryInput.files)); }
    catch (error) { renderFileImport(error.message); }
  });
  ["dragenter", "dragover"].forEach(type => dropzone.addEventListener(type, event => {
    event.preventDefault();
    dropzone.classList.add("dragging");
  }));
  ["dragleave", "drop"].forEach(type => dropzone.addEventListener(type, event => {
    event.preventDefault();
    dropzone.classList.remove("dragging");
  }));
  dropzone.addEventListener("drop", async event => {
    try {
      addItems(await droppedPaperImportItems(event.dataTransfer));
    } catch (error) {
      renderFileImport(error.message);
    }
  });
  dialogBody.append(dropzone);

  if (task.items.length) {
    const list = node("div", "paper-import-list");
    task.items.forEach((item, index) => {
      const size = item.files.reduce((sum, value) => sum + value.file.size, 0);
      const row = node("div", "paper-import-item");
      const copy = node("span");
      copy.append(
        node("strong", "", item.name),
        node("small", "", item.kind === "file"
          ? paperImportSize(size)
          : `${item.files.length} files · ${paperImportSize(size)}`),
      );
      row.append(copy, button("Remove", () => {
        task.items.splice(index, 1);
        renderFileImport();
      }, "button"));
      list.append(row);
    });
    dialogBody.append(list);
  }

  const roots = state.settings.paperRoots || [];
  const grid = node("div", "form-grid paper-import-options");
  grid.append(field("outputDirectory", "Paper collection", {
    type: "select", value: task.outputDirectory,
    options: roots.map(value => [value, value]),
  }));
  grid.querySelector("select").addEventListener("change", event => {
    task.outputDirectory = event.target.value;
  });
  dialogBody.append(grid);
  const add = button(
    `Add ${task.items.length || ""} paper${task.items.length === 1 ? "" : "s"}`.replace("  ", " "),
    uploadPaperImports,
    "button primary",
  );
  add.disabled = !task.items.length;
  dialogFooter.replaceChildren(button("Cancel", () => dialog.close()), add);
}

async function uploadPaperImports() {
  const task = state.dialog;
  if (!task || task.kind !== "fileImport" || !task.items.length) return;
  dialog.setAttribute("aria-busy", "true");
  dialogFooter.querySelectorAll("button").forEach(value => { value.disabled = true; });
  let session = null;
  try {
    session = await api("/api/paper-imports", { method: "POST", body: {} });
    const inputs = [];
    const total = task.items.reduce((sum, item) => sum + item.files.length, 0);
    let uploaded = 0;
    for (const [index, item] of task.items.entries()) {
      const root = `item-${index}/${item.name}`;
      inputs.push(root);
      for (const value of item.files) {
        const path = item.kind === "file" ? root : `${root}/${value.path}`;
        dialogEyebrow.textContent = `Uploading ${uploaded + 1} of ${total}`;
        await api(`/api/paper-imports/${session.id}/files?${new URLSearchParams({ path })}`, {
          method: "POST",
          body: value.file,
        });
        uploaded += 1;
      }
    }
    dialogEyebrow.textContent = "Installing papers";
    const result = await api(`/api/paper-imports/${session.id}/commit`, {
      method: "POST",
      body: { inputs, outputDirectory: task.outputDirectory },
    });
    dialog.close();
    state.dialog = null;
    showNotice(`Added ${result.papers.length} paper${result.papers.length === 1 ? "" : "s"}.`);
    const paths = new Set(result.papers.map(value => value.path));
    for (let attempt = 0; attempt < 30; attempt += 1) {
      await refreshCatalog();
      const imported = state.catalog.papers.find(paper => paths.has(paper.path));
      if (imported) {
        state.selectedPaper = imported.key;
        syncNavigation({ replace: true });
        break;
      }
      await new Promise(resolve => setTimeout(resolve, 250));
    }
  } catch (error) {
    if (session) {
      try {
        await api(`/api/paper-imports/${session.id}/cancel`, {
          method: "POST", body: {},
        });
      } catch (_) {}
    }
    renderFileImport(error.message);
  } finally {
    dialog.removeAttribute("aria-busy");
  }
}

function openMetadataEditor(paper) {
  state.dialog = { kind: "metadata", paper };
  dialogEyebrow.textContent = "Paper metadata";
  dialogTitle.textContent = "Edit metadata";
  dialogBody.replaceChildren();
  const grid = node("div", "form-grid");
  grid.append(
    field("title", "Title", {
      value: paper.metadataComplete || paper.title !== paper.name ? paper.title : "",
      full: true,
    }),
    field("authors", "Authors", {
      type: "textarea", value: paper.authors.join("\n"), full: true,
      help: "One author per line, in display order.",
    }),
    field("published", "Published", {
      value: paper.published || "",
      help: "The paper's original publication or first-submission date. Format: YYYY, YYYY-MM, or YYYY-MM-DD.",
    }),
    field("updated", "Revised", {
      value: paper.updated || "",
      help: "The paper's latest revision date, such as its newest arXiv version. Format: YYYY, YYYY-MM, or YYYY-MM-DD.",
    }),
    field("arxivId", "arXiv ID", {
      value: paper.arxivId || "", help: "Optional; for example 2608.04410v1.",
    }),
    field("doi", "DOI", { value: paper.doi || "" }),
    field("url", "Canonical URL", { value: paper.url || "", full: true }),
  );
  dialogBody.append(grid);
  dialogFooter.replaceChildren(
    button("Cancel", () => dialog.close()),
    button("Save metadata", saveMetadataEditor, "button primary"),
  );
  dialog.showModal();
}

async function saveMetadataEditor() {
  const editor = state.dialog;
  if (!editor || editor.kind !== "metadata") return;
  const values = {};
  dialogBody.querySelectorAll("[name]").forEach(input => {
    values[input.name] = input.value;
  });
  values.authors = String(values.authors || "")
    .split("\n").map(value => value.trim()).filter(Boolean);
  dialogFooter.querySelectorAll("button").forEach(value => {
    value.disabled = true;
  });
  try {
    const updated = await api("/api/papers/metadata", {
      method: "POST",
      body: { path: editor.paper.path, ...values },
    });
    Object.assign(editor.paper, updated, {
      metadataComplete: Boolean(updated.title && updated.authors.length),
    });
    dialog.close();
    state.dialog = null;
    renderPapers();
  } catch (error) {
    dialogFooter.querySelectorAll("button").forEach(value => {
      value.disabled = false;
    });
    dialogBody.querySelector(".error-box")?.remove();
    dialogBody.prepend(node("div", "error-box", error.message));
  }
}

function openProblemEditor(paper) {
  state.dialog = { kind: "open-problem", paper };
  dialogEyebrow.textContent = paper.title;
  dialogTitle.textContent = "Add open problem";
  dialogBody.replaceChildren();
  const grid = node("div", "form-grid");
  grid.append(
    field("title", "Title", {
      full: true,
      help: "A short name for the problem.",
    }),
    field("statement", "Problem statement", {
      type: "textarea",
      full: true,
      help: "Markdown and LaTeX math are supported.",
    }),
    field("explicitness", "Relation to the paper", {
      type: "select",
      value: "additional",
      options: [
        ["additional", "Additional problem related to the paper"],
        ["explicit", "Explicitly stated in the paper"],
        ["inferred", "Inferred from the paper"],
        ["uncertain", "Uncertain"],
      ],
      full: true,
    }),
  );
  dialogBody.append(grid);
  dialogFooter.replaceChildren(
    button("Cancel", () => dialog.close()),
    button("Add problem", saveProblemEditor, "button primary"),
  );
  dialog.showModal();
  dialogBody.querySelector('[name="title"]')?.focus();
}

async function saveProblemEditor() {
  const editor = state.dialog;
  if (!editor || editor.kind !== "open-problem") return;
  const values = {};
  dialogBody.querySelectorAll("[name]").forEach(input => {
    values[input.name] = input.value;
  });
  dialogFooter.querySelectorAll("button").forEach(value => {
    value.disabled = true;
  });
  try {
    const problem = await api("/api/papers/open-problems", {
      method: "POST",
      body: { path: editor.paper.path, ...values },
    });
    editor.paper.problemCount += 1;
    dialog.close();
    state.dialog = null;
    renderPapers();
    showNotice(`${problem.id} was added to ${editor.paper.title}.`);
  } catch (error) {
    dialogFooter.querySelectorAll("button").forEach(value => {
      value.disabled = false;
    });
    dialogBody.querySelector(".error-box")?.remove();
    dialogBody.prepend(node("div", "error-box", error.message));
  }
}

async function setManuscriptPinning(draft, source, pinned, control) {
  const message = pinned
    ? `Pin ${source.id} in future revisions of ${draft.name} to ${source.attemptName || "its current attempt"}?`
    : `Unpin ${source.id} in ${draft.name}? Future revisions will use this problem's latest attempt.`;
  if (!window.confirm(message)) return;
  control.disabled = true;
  try {
    const result = await api("/api/manuscripts/pinning", {
      method: "POST",
      body: {
        draft: draft.path,
        problem: `${source.paperPath}/${source.id}`,
        pinned,
      },
    });
    draft.sources = result.sources;
    renderManuscripts();
    showNotice(
      pinned
        ? `${source.id} is now pinned to ${source.attemptName || "its current attempt"}.`
        : `${source.id} will now track its latest attempt.`,
    );
  } catch (error) {
    control.disabled = false;
    showNotice(error.message, true);
  }
}

function renderManuscripts() {
  persistentSidebarControls("manuscripts", () => {
    const controls = node("div", "paper-list-controls");
    controls.append(
      sidebarSearch("Search manuscripts…"),
      manuscriptSortControl(),
    );
    return controls;
  });
  const query = state.search.trim().toLowerCase();
  const manuscripts = state.catalog.manuscripts
    .filter(value => !query || `${value.name} ${value.latest.title}`.toLowerCase().includes(query))
    .sort((left, right) => {
      const alphabetical = String(left.latest.title).localeCompare(
        String(right.latest.title),
        undefined,
        { sensitivity: "base", numeric: true },
      ) || left.name.localeCompare(right.name, undefined, { sensitivity: "base", numeric: true });
      if (state.manuscriptSort === "alphabetical") return alphabetical;
      return (Number(right.latest.createdTimestamp) || 0) -
        (Number(left.latest.createdTimestamp) || 0) || alphabetical;
    });
  if (!state.selectedManuscript || !manuscripts.some(value => value.key === state.selectedManuscript)) {
    state.selectedManuscript = manuscripts[0]?.key || "";
  }
  const manuscriptScroll = node("div", "manuscript-scroll");
  const list = node("div", "side-list");
  manuscripts.forEach(value => appendSideCard(list, {
    title: value.latest.title,
    meta: `${value.drafts.length} draft${value.drafts.length === 1 ? "" : "s"} · ${humanize(value.latest.verdict)}`,
    active: state.selectedManuscript === value.key,
    relatedTask: { manuscriptPath: value.path },
    onClick: () => {
      state.selectedManuscript = value.key;
      const rememberedDraft = state.manuscriptDraftSelections.get(value.key);
      state.selectedDraft = value.drafts.some(draft => draft.key === rememberedDraft)
        ? rememberedDraft
        : value.latest.key;
      state.revealSidebarSecondarySelection = true;
      syncNavigation();
    },
  }));
  manuscriptScroll.append(node("div", "sidebar-heading", `${manuscripts.length} manuscripts`), list);
  sidebar.append(manuscriptScroll);
  const manuscript = state.catalog.manuscripts.find(value => value.key === state.selectedManuscript);
  if (!manuscript) {
    main.replaceChildren(document.getElementById("empty-template").content.cloneNode(true));
    return;
  }
  if (!manuscript.drafts.some(value => value.key === state.selectedDraft)) {
    const rememberedDraft = state.manuscriptDraftSelections.get(manuscript.key);
    state.selectedDraft = manuscript.drafts.some(value => value.key === rememberedDraft)
      ? rememberedDraft
      : manuscript.latest.key;
  }
  state.manuscriptDraftSelections.set(manuscript.key, state.selectedDraft);
  const draft = manuscript.drafts.find(value => value.key === state.selectedDraft) || manuscript.latest;

  const draftSwitcher = node("div", "draft-switcher");
  draftSwitcher.append(node("div", "sidebar-heading", `Drafts · ${manuscript.drafts.length}`));
  const draftList = node("div", "draft-list");
  [...manuscript.drafts].reverse().forEach(value => {
    const latest = value.key === manuscript.latest.key;
    appendSideCard(draftList, {
      title: value.name,
      meta: `${latest ? "Latest · " : ""}${humanize(value.status)} · ${humanize(value.verdict)}`,
      active: value.key === draft.key,
      relatedTask: {
        manuscriptPath: manuscript.path,
        draftPath: value.path,
      },
      onClick: () => {
        state.selectedDraft = value.key;
        state.manuscriptDraftSelections.set(manuscript.key, value.key);
        syncNavigation();
      },
    });
  });
  draftSwitcher.append(draftList);
  sidebar.append(draftSwitcher);

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
  if (draft.key !== manuscript.latest.key) {
    shell.append(olderVersionWarning(
      "draft",
      draft.name,
      manuscript.latest.name,
      { tab: "manuscripts", manuscript, draft: manuscript.latest },
      () => {
        state.selectedDraft = manuscript.latest.key;
        state.manuscriptDraftSelections.set(manuscript.key, manuscript.latest.key);
        state.revealSidebarSecondarySelection = true;
        syncNavigation();
      },
    ));
  }
  const actions = node("div", "actions");
  addAction(actions, draft.verdict === "unreviewed" ? "Resume review" : "Revise", "revise", [draftTarget(draft)], true);
  const pdf = draft.files.find(path => path.endsWith("main.pdf"));
  if (pdf) {
    const open = node("a", "button", "Open PDF");
    open.href = artifactViewUrl(pdf);
    open.target = "_blank";
    const downloadPdf = node("a", "button", "Download PDF");
    downloadPdf.href = downloadFileUrl(pdf, `${manuscript.name}-${draft.name}.pdf`);
    actions.append(open, downloadPdf);
  }
  const downloadZip = node("a", "button", "Download ZIP");
  downloadZip.href = manuscriptZipUrl(draft.path);
  actions.append(downloadZip);
  shell.append(actions);
  shell.append(relatedTasksPanel({
    manuscriptPath: manuscript.path,
    draftPath: draft.path,
  }));
  if (draft.abstract) shell.append(summaryPanel("Abstract", draft.abstract));
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
        const tracking = source.pinned
          ? `Pinned to ${source.attemptName || "the recorded attempt"}`
          : `${source.selectorKind === "paper" ? "Tracks latest through paper selection" : "Tracks latest attempt"}${source.attemptName ? ` · currently ${source.attemptName}` : ""}`;
        const trackingRow = node("div", "source-tracking-row");
        trackingRow.append(
          node("small", `source-tracking${source.pinned ? " pinned" : ""}`, tracking),
        );
        const toggle = button(
          source.pinned ? "Unpin" : "Pin",
          () => setManuscriptPinning(draft, source, !source.pinned, toggle),
          "button source-pin-toggle",
        );
        toggle.title = source.pinned
          ? "Follow this problem's latest attempt in future revisions"
          : "Pin future revisions to this problem's current attempt";
        trackingRow.append(toggle);
        item.append(
          label,
          node("small", "", source.paperTitle),
          trackingRow,
        );
        list.append(item);
      });
      panel.append(list);
      sourceGrid.append(panel);
    }
    shell.append(sourcesHeading, sourceGrid);
  }
  const heading = node("div", "section-title");
  heading.append(node("h2", "", "Draft files"));
  shell.append(heading, fileGrid(draft.files));
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
    else {
      refreshVisibleRunElapsed(cached);
      refreshVisibleRunLogs();
    }
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
  const attempts = (run.targets || []).filter(target => target.kind === "attempt");
  if (action === "review" && attempts.length === 1) {
    return { title: targetDisplayLabel(attempts[0]), targets: [], targetValues: [] };
  }
  const targets = (run.targets || []).filter(target => target.kind === "problem");
  if (action !== "literature" || !targets.length) {
    return { title: run.label, targets: [], targetValues: [] };
  }
  const paperPaths = new Set(targets.map(target =>
    normalizedPath(target.path).split("/").slice(0, -1).join("/"),
  ));
  if (paperPaths.size !== 1) {
    return { title: run.label, targets: [], targetValues: [] };
  }
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
    targetValues: targets,
  };
}

function appendRunTargetSummary(parent, presentation) {
  if (!presentation.targets.length) return;
  const summary = node("span", "run-target-summary", presentation.summary);
  summary.title = presentation.summary;
  parent.append(summary);
}

function focusRun(job, run) {
  state.expandedRuns.add(run.id);
  renderJobDetail(job);
  requestAnimationFrame(() => {
    document.getElementById(`run-${run.id}`)?.scrollIntoView({ block: "center", behavior: "auto" });
  });
}

function runAttentionPanel(job) {
  const runs = latestJobRuns(job).filter(run => unsuccessfulRunStatuses.includes(run.status));
  if (!runs.length) return null;
  const panel = node("section", "run-attention panel");
  const heading = node("div", "run-attention-heading");
  heading.append(
    node("h2", "", "Needs attention"),
    badge(`${runs.length} run${runs.length === 1 ? "" : "s"}`, "failed"),
  );
  panel.append(heading);
  const list = node("div", "run-attention-list");
  runs.forEach(run => {
    const presentation = problemRunPresentation(job.action, run);
    const row = node("div", `run-attention-row status-${taskStatus(run.status).tone}`);
    const copy = node("div", "run-attention-copy");
    const title = node("strong", "", presentation.title);
    title.title = presentation.title;
    const details = [];
    if (presentation.targets.length) {
      details.push(`${presentation.targets.length} selected problem${presentation.targets.length === 1 ? "" : "s"}`);
    }
    if (run.exit_code != null && run.exit_code !== 0) details.push(`Exit code ${run.exit_code}`);
    if (run.error) details.push(run.error);
    copy.append(title, node("small", "", details.join(" · ") || taskStatus(run.status).label));
    const actions = node("div", "run-attention-actions");
    actions.append(button("Show run", () => focusRun(job, run), "button"));
    if (["failed", "canceled", "interrupted"].includes(run.status) && !(run.outputs || []).length) {
      actions.append(button("Retry", () => mutateRun(run.id, "retry"), "button primary"));
    }
    row.append(taskStatusBadge(run.status), copy, actions);
    list.append(row);
  });
  panel.append(list);
  return panel;
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
      refreshVisibleRunElapsed(job);
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
  const copy = node("div", "hero-copy");
  copy.append(node("div", "eyebrow", "Managed task"));
  copy.append(node("h1", "", taskActionTitle(job.action)));
  const scope = node("div", "task-scope");
  scope.append(node("div", "task-scope-summary", taskScopeSummary(job)));
  const targets = taskTargets(job);
  if (targets.length) {
    const targetBox = node("div", "task-targets-box");
    const targetList = node("div", "target-list task-targets");
    targetList.id = `job-targets-${job.id}`;
    targets.forEach(value => targetList.append(targetChip(value)));
    // Start clamped so overflow can be measured even when this scope was
    // previously expanded. The requested state is restored after layout.
    targetList.classList.add("collapsed");
    const expanded = state.expandedJobScopes.has(job.id);
    const setExpanded = nextExpanded => {
      targetList.classList.toggle("collapsed", !nextExpanded);
      if (nextExpanded) state.expandedJobScopes.add(job.id);
      else state.expandedJobScopes.delete(job.id);
      topToggle.hidden = !nextExpanded;
      topToggle.setAttribute("aria-expanded", String(nextExpanded));
      bottomToggle.textContent = nextExpanded ? "Show less" : "Show all";
      bottomToggle.setAttribute("aria-expanded", String(nextExpanded));
    };
    const topToggle = button("Show less", () => setExpanded(false), "task-targets-toggle top");
    const bottomToggle = button(
      expanded ? "Show less" : "Show all",
      () => setExpanded(targetList.classList.contains("collapsed")),
      "task-targets-toggle bottom",
    );
    topToggle.hidden = true;
    bottomToggle.hidden = true;
    [topToggle, bottomToggle].forEach(toggle => {
      toggle.setAttribute("aria-controls", targetList.id);
      toggle.setAttribute("aria-expanded", String(expanded));
    });
    targetBox.append(topToggle, targetList, bottomToggle);
    scope.append(targetBox);
    requestAnimationFrame(() => {
      if (!targetList.isConnected) return;
      const overflows = targetList.scrollHeight > targetList.clientHeight + 1;
      if (!overflows) {
        state.expandedJobScopes.delete(job.id);
        targetList.classList.remove("collapsed");
        topToggle.remove();
        bottomToggle.remove();
        return;
      }
      if (expanded) {
        targetList.classList.remove("collapsed");
        topToggle.hidden = false;
      }
      bottomToggle.hidden = false;
    });
  }
  copy.append(node("p", "", `Created ${formatTime(job.created_at)}`));
  const badges = node("div", "badges");
  badges.append(taskIsPaused(job) ? badge("Paused", "paused") : taskStatusBadge(job.status));
  badges.append(badge(`Weight ${priorityMultiplier(job.priority_level)}`, "neutral"));
  copy.append(badges);
  hero.append(copy, jobSchedulingControls(job), scope);
  shell.append(hero);
  const attention = runAttentionPanel(job);
  if (attention) shell.append(attention);
  job.runs.forEach((run, index) => {
    const section = node("section", `run-card panel status-${taskStatus(run.status).tone}`);
    section.id = `run-${run.id}`;
    section.dataset.runId = run.id;
    const expanded = state.expandedRuns.has(run.id);
    const presentation = problemRunPresentation(job.action, run);
    const heading = node("div", "run-summary");
    const headingCopy = node("span", "run-heading-copy");
    const title = node("strong", "", `${index + 1}. ${presentation.title}`);
    title.title = presentation.title;
    headingCopy.append(title);
    appendRunTargetSummary(headingCopy, presentation);
    headingCopy.append(runTiming(run));
    const actions = node("div", "run-actions");
    if (["queued", "starting", "running", "cancel_requested"].includes(run.status)) {
      actions.append(button("Cancel", () => mutateRun(run.id, "cancel"), "button danger"));
    }
    if (["failed", "canceled", "interrupted"].includes(run.status) && !(run.outputs || []).length) {
      actions.append(button("Retry", () => mutateRun(run.id, "retry"), "button primary"));
    }
    heading.append(headingCopy, actions);
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
    const detailFooter = node("div", "run-detail-footer");
    const toggle = button(`${expanded ? "Hide" : "Show"} command & output`, () => {
      if (state.expandedRuns.has(run.id)) state.expandedRuns.delete(run.id);
      else state.expandedRuns.add(run.id);
      renderJobDetail(job);
    }, "run-detail-toggle");
    toggle.setAttribute("aria-expanded", String(expanded));
    toggle.setAttribute("aria-label", `${expanded ? "Hide" : "Show"} command and output for ${presentation.title}`);
    toggle.prepend(node("span", "run-chevron", "›"));
    detailFooter.append(toggle);
    section.append(detailFooter);
    if (expanded) {
      const details = node("div", "run-expanded");
      if (presentation.targets.length) {
        const selected = node("div", "confirm-block");
        selected.append(node("h3", "", "Selected problems"));
        const targetList = node("div", "target-list run-targets");
        presentation.targetValues.forEach(value => targetList.append(targetChip(value)));
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
    const nextText = runLogText(value);
    const previousText = log.textContent || "";
    if (previousText === nextText) return;
    const wasLoading = previousText === "Loading output…";
    const wasNearBottom = log.scrollHeight - log.scrollTop - log.clientHeight <= 24;
    if (!wasLoading && previousText && nextText.startsWith(previousText)) {
      log.append(document.createTextNode(nextText.slice(previousText.length)));
    } else {
      log.textContent = nextText;
    }
    if (wasLoading || wasNearBottom) log.scrollTop = log.scrollHeight;
  });
}

async function refreshRunLog(runId) {
  const cached = state.runLogs.get(runId);
  if (cached?.complete) return;
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

function refreshVisibleRunElapsed(job) {
  const runs = new Map((job.runs || []).map(run => [run.id, run]));
  const now = Date.now() / 1000;
  main.querySelectorAll("[data-run-elapsed]").forEach(label => {
    const run = runs.get(label.dataset.runElapsed);
    if (!run || !taskStatus(run.status).active || !run.started_at) return;
    label.textContent = `Elapsed ${formatDuration(run.started_at, now)}`;
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
  label.append(node("span", "", "Scheduling weight"));
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

function liveRunRelation(
  run,
  {
    paperPath = "",
    problemPath = "",
    draftPath = "",
    manuscriptPath = "",
  },
) {
  const paper = normalizedPath(paperPath);
  const problem = normalizedPath(problemPath);
  const draft = normalizedPath(draftPath);
  const manuscript = normalizedPath(manuscriptPath);
  let paperWide = false;
  let paperDescendant = false;
  let manuscriptWide = false;
  let direct = false;
  (run.targets || []).forEach(target => {
    if (!target || typeof target.path !== "string") return;
    const path = normalizedPath(target.path);
    if (target.kind === "paper" && path === paper) paperWide = true;
    else if (target.kind === "problem") {
      if (problem && path === problem) direct = true;
      if (paper && path.split("/").slice(0, -1).join("/") === paper) {
        paperDescendant = true;
      }
    } else if (target.kind === "attempt") {
      const parent = path.split("/").slice(0, -1).join("/");
      if (problem && parent === problem) direct = true;
      if (paper && parent.split("/").slice(0, -1).join("/") === paper) {
        paperDescendant = true;
      }
    } else if (target.kind === "draft") {
      if (draft && path === draft) direct = true;
      if (manuscript && path.split("/").slice(0, -1).join("/") === manuscript) {
        manuscriptWide = true;
      }
    }
  });
  return { paperWide, paperDescendant, manuscriptWide, direct };
}

function relatedEntryStatus(entry) {
  const statuses = new Set(entry.runs.map(run => run.status));
  for (const status of ["running", "starting", "cancel_requested"]) {
    if (statuses.has(status)) return { ...taskStatus(status), status };
  }
  if (statuses.has("queued") && taskIsPaused(entry.job)) {
    return { label: "Paused", tone: "paused", active: false, status: "paused" };
  }
  return { ...taskStatus("queued"), status: "queued" };
}

function relatedTaskEntries({
  paperPath = "",
  problemPath = "",
  draftPath = "",
  manuscriptPath = "",
  includePaper = true,
  includePaperDescendants = false,
  includeManuscript = true,
}) {
  const entries = [];
  state.jobs.forEach(job => {
    const runs = (job.liveRuns || []).filter(run => {
      const relation = liveRunRelation(run, {
        paperPath, problemPath, draftPath, manuscriptPath,
      });
      return relation.direct ||
        (includePaper && relation.paperWide) ||
        (includePaperDescendants && relation.paperDescendant) ||
        (includeManuscript && relation.manuscriptWide);
    });
    if (runs.length) entries.push({ job, runs });
  });
  const rank = { running: 0, starting: 1, cancel_requested: 2, queued: 3, paused: 4 };
  return entries.sort((left, right) => {
    const leftStatus = relatedEntryStatus(left);
    const rightStatus = relatedEntryStatus(right);
    const leftRank = rank[leftStatus.status] ?? 5;
    const rightRank = rank[rightStatus.status] ?? 5;
    return leftRank - rightRank || Number(right.job.created_at) - Number(left.job.created_at);
  });
}

function openRelatedTask(jobId) {
  state.selectedJob = jobId;
  setTab("activity");
}

function fillRelatedTaskHost(host) {
  const entries = relatedTaskEntries({
    paperPath: host.dataset.relatedPaper || "",
    problemPath: host.dataset.relatedProblem || "",
    draftPath: host.dataset.relatedDraft || "",
    manuscriptPath: host.dataset.relatedManuscript || "",
    includePaper: host.dataset.includePaper !== "0",
    includePaperDescendants: host.dataset.includePaperDescendants === "1",
    includeManuscript: host.dataset.includeManuscript !== "0",
  });
  host.replaceChildren();
  host.hidden = !entries.length;
  if (!entries.length) return;
  const status = relatedEntryStatus(entries[0]);
  const label = `${status.label}${entries.length > 1 ? ` · ${entries.length}` : ""}`;
  const pill = button(label, event => {
    event.stopPropagation();
    openRelatedTask(entries[0].job.id);
  }, `badge sidebar-task-pill ${status.tone}`);
  pill.title = entries.map(entry => {
    const value = relatedEntryStatus(entry);
    return `${value.label}: ${taskActionTitle(entry.job.action)}`;
  }).join("\n");
  host.append(pill);
}

function relatedTaskHost({
  paperPath = "",
  problemPath = "",
  draftPath = "",
  manuscriptPath = "",
  includePaper = true,
  includePaperDescendants = false,
  includeManuscript = true,
}) {
  const host = node("span", "sidebar-related-tasks");
  if (paperPath) host.dataset.relatedPaper = normalizedPath(paperPath);
  if (problemPath) host.dataset.relatedProblem = normalizedPath(problemPath);
  if (draftPath) host.dataset.relatedDraft = normalizedPath(draftPath);
  if (manuscriptPath) host.dataset.relatedManuscript = normalizedPath(manuscriptPath);
  host.dataset.includePaper = includePaper ? "1" : "0";
  host.dataset.includePaperDescendants = includePaperDescendants ? "1" : "0";
  host.dataset.includeManuscript = includeManuscript ? "1" : "0";
  fillRelatedTaskHost(host);
  return host;
}

function fillRelatedTasksPanel(panel) {
  const entries = relatedTaskEntries({
    paperPath: panel.dataset.relatedPaper || "",
    problemPath: panel.dataset.relatedProblem || "",
    draftPath: panel.dataset.relatedDraft || "",
    manuscriptPath: panel.dataset.relatedManuscript || "",
    includePaper: panel.dataset.includePaper !== "0",
    includePaperDescendants: panel.dataset.includePaperDescendants === "1",
    includeManuscript: panel.dataset.includeManuscript !== "0",
  });
  panel.replaceChildren();
  panel.hidden = !entries.length;
  if (!entries.length) return;
  const heading = node("div", "related-tasks-heading");
  heading.append(
    node("h2", "", "Related tasks"),
    badge(`${entries.length} task${entries.length === 1 ? "" : "s"}`, "neutral"),
  );
  const list = node("div", "related-task-list");
  entries.forEach(entry => {
    const status = relatedEntryStatus(entry);
    const row = button("", () => openRelatedTask(entry.job.id), "related-task-row");
    const copy = node("span", "related-task-copy");
    copy.append(
      node("strong", "", taskActionTitle(entry.job.action)),
      node("small", "", `${taskScopeSummary(entry.job)} · ${taskSidebarMeta(entry.job)}`),
    );
    row.append(node("span", `badge ${status.tone}`, status.label), copy, node("span", "related-task-arrow", "›"));
    row.setAttribute("aria-label", `Open ${taskActionTitle(entry.job.action)} task`);
    list.append(row);
  });
  panel.append(heading, list);
}

function relatedTasksPanel({
  paperPath = "",
  problemPath = "",
  draftPath = "",
  manuscriptPath = "",
  includePaper = true,
  includePaperDescendants = false,
  includeManuscript = true,
}) {
  const panel = node("section", "related-tasks panel");
  if (paperPath) panel.dataset.relatedPaper = normalizedPath(paperPath);
  if (problemPath) panel.dataset.relatedProblem = normalizedPath(problemPath);
  if (draftPath) panel.dataset.relatedDraft = normalizedPath(draftPath);
  if (manuscriptPath) panel.dataset.relatedManuscript = normalizedPath(manuscriptPath);
  panel.dataset.includePaper = includePaper ? "1" : "0";
  panel.dataset.includePaperDescendants = includePaperDescendants ? "1" : "0";
  panel.dataset.includeManuscript = includeManuscript ? "1" : "0";
  fillRelatedTasksPanel(panel);
  return panel;
}

function syncRelatedTasks() {
  if (!["research", "papers", "manuscripts"].includes(state.tab)) return;
  sidebar.querySelectorAll(".sidebar-related-tasks").forEach(fillRelatedTaskHost);
  main.querySelectorAll(".related-tasks").forEach(fillRelatedTasksPanel);
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
      pathContains(item.path, path),
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

function targetRoute(target) {
  if (!target || typeof target.path !== "string") return null;
  const path = normalizedPath(target.path);
  if (target.kind === "attempt") {
    const review = state.catalog.reviews.find(item =>
      item.attemptDirectory && normalizedPath(item.attemptDirectory) === path
    );
    return review ? { tab: "research", review, detail: "attempt" } : null;
  }
  if (target.kind === "problem") {
    const problem = state.catalog.reviews.find(item =>
      normalizedPath(`${item.paperDirectory}/${item.problemId}`) === path
    );
    if (!problem) return null;
    const review = reviewModel.attemptsForProblem(
      state.catalog.reviews,
      problem.problemKey,
    )[0] || problem;
    return { tab: "research", review, detail: "attempt" };
  }
  if (target.kind === "paper") {
    const paper = state.catalog.papers.find(item => normalizedPath(item.path) === path);
    return paper ? { tab: "papers", paper } : null;
  }
  if (target.kind === "draft") {
    for (const manuscript of state.catalog.manuscripts) {
      const draft = manuscript.drafts.find(item => normalizedPath(item.path) === path);
      if (draft) return { tab: "manuscripts", manuscript, draft };
    }
  }
  return null;
}

function routeHref(route) {
  const parameters = new URLSearchParams();
  if (route.tab === "research") {
    reviewModel.identityToSearchParams(parameters, route.review);
    if (route.detail && route.detail !== "attempt") parameters.set("detail", route.detail);
  } else if (route.tab === "papers") {
    parameters.set("paper", route.paper.urlKey || route.paper.path);
  } else if (route.tab === "manuscripts") {
    parameters.set("manuscript", route.manuscript.urlKey || route.manuscript.path);
    if (route.draft && route.draft.key !== route.manuscript.latest.key) {
      parameters.set("draft", route.draft.name);
    }
  }
  const query = parameters.toString();
  return `${viewPaths[route.tab]}${query ? `?${query}` : ""}`;
}

function openRoute(route) {
  if (route.tab === "research") {
    state.search = "";
    state.paperSort = "alphabetical";
    state.researchFilters = reviewModel.createDefaultFilters();
    state.selectedReview = route.review.itemKey;
    state.selectedProblem = route.review.problemKey;
    state.detailTab = route.detail;
    state.revealSidebarSelection = true;
    state.revealSidebarSecondarySelection = true;
  } else if (route.tab === "papers") {
    state.search = "";
    state.paperSort = "alphabetical";
    state.selectedPaper = route.paper.key;
    state.revealSidebarSelection = true;
  } else {
    state.search = "";
    state.manuscriptSort = "latest";
    state.selectedManuscript = route.manuscript.key;
    state.selectedDraft = route.draft.key;
    state.manuscriptDraftSelections.set(route.manuscript.key, route.draft.key);
    state.revealSidebarSelection = true;
    state.revealSidebarSecondarySelection = true;
  }
  setTab(route.tab);
  return true;
}

function routeLink(route, label, className) {
  const link = node("a", className, label);
  link.href = routeHref(route);
  link.addEventListener("click", event => {
    if (
      event.defaultPrevented || event.button !== 0 || event.metaKey ||
      event.ctrlKey || event.shiftKey || event.altKey
    ) return;
    event.preventDefault();
    openRoute(route);
  });
  return link;
}

function targetChip(target) {
  const route = targetRoute(target);
  if (!route) return node("span", "target-chip", targetDisplayLabel(target));
  const link = routeLink(route, targetDisplayLabel(target), "target-chip target-link");
  link.title = `Open ${target.kind}`;
  return link;
}

function openOutput(path) {
  const route = outputRoute(path);
  return route ? openRoute(route) : false;
}

const actionNames = {
  download: "Download from arXiv",
  metadata: "Extract paper metadata",
  analyze: "Analyze papers", triage: "Triage problems", literature: "Search literature",
  solve: "Solve problems", review: "Review attempts", write: "Write paper", revise: "Revise manuscript",
};

const taskActionTitles = {
  download: "arXiv download",
  metadata: "Paper metadata",
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
  const planned = job.plan?.targets || job.request?.targets || [];
  if (planned.length || job.action !== "download") return planned;

  // Older download tasks predate plan-level paper targets. Recover their
  // scope from reported artifacts so their completed papers remain navigable.
  const papers = new Map();
  (job.runs || []).flatMap(run => run.outputs || []).forEach(path => {
    const paper = state.catalog.papers.find(item => pathContains(item.path, path));
    if (!paper) return;
    papers.set(normalizedPath(paper.path), target(
      "paper",
      paper.path,
      reviewModel.paperTitleWithYear(paper.title, paper.published),
    ));
  });
  return [...papers.values()];
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

function singlePaperProblemScope(job) {
  const targets = taskTargets(job);
  const paperTitle = job.plan?.singlePaperTitle;
  if (
    targets.length < 2 ||
    !targets.every(value => value.kind === "problem") ||
    typeof paperTitle !== "string" ||
    !paperTitle.trim()
  ) return "";
  return `${targetCountLabel(targets)} in ${paperTitle}`;
}

function taskScopeSummary(job) {
  const targets = taskTargets(job);
  if (!targets.length) return job.title;
  const paperScope = singlePaperProblemScope(job);
  const pieces = [paperScope || targetCountLabel(targets)];
  const papers = targetPaperCount(targets);
  if (papers && !paperScope) pieces.push(`${papers} paper${papers === 1 ? "" : "s"}`);
  const units = job.plan?.units?.length || new Set((job.runs || []).map(run => run.unit_index)).size;
  if (units) pieces.push(`${units} run${units === 1 ? "" : "s"}`);
  return pieces.join(" · ");
}

function taskSidebarTitle(job) {
  const targets = taskTargets(job);
  const scope = singlePaperProblemScope(job) || (targets.length === 1
    ? targetDisplayLabel(targets[0]) || targetCountLabel(targets)
    : targets.length ? targetCountLabel(targets) : job.title);
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
  if (help) copy.append(document.createTextNode(" "), node("small", "", help));
  wrapper.append(input, copy);
  return wrapper;
}

function promptLabel(action) {
  return {
    metadata: "Metadata-extractor direction",
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
  if (
    action === "write" &&
    targets.some(value => value.kind === "attempt") &&
    !Object.hasOwn(saved, "pinAttempts")
  ) {
    saved.pinAttempts = targets.some(historicalAttemptTarget);
  }
  state.dialog = {
    action, targets, options: saved, storageKey, plan: null,
    authorSearch: null, authorSelection: new Set(),
  };
  renderTaskConfiguration();
  dialog.showModal();
}

function renderTaskTargetChips(task, container) {
  container.replaceChildren();
  taskTargetsForRequest(task).forEach(value => {
    container.append(node("span", "target-chip", targetDisplayLabel(value)));
  });
}

function selectedAuthorPaperIds(task) {
  if (!task.authorSearch) return [];
  return task.authorSearch.papers
    .map(paper => paper.id)
    .filter(id => task.authorSelection.has(id));
}

function updateAuthorSelectionAction(task) {
  const selected = selectedAuthorPaperIds(task).length;
  const action = dialogFooter.querySelector(".primary");
  if (!action) return;
  action.disabled = selected === 0;
  action.textContent = `Review ${selected} download${selected === 1 ? "" : "s"}`;
}

function arxivAuthorResults(task) {
  const result = task.authorSearch;
  const section = node("section", "arxiv-results full");
  const heading = node("div", "arxiv-results-heading");
  const displayed = result.papers.length;
  const summary = displayed === result.totalResults
    ? `${displayed} paper${displayed === 1 ? "" : "s"}`
    : `${displayed} of ${result.totalResults} papers`;
  heading.append(
    node("div", "", "Search results"),
    node("small", "", `${summary} for ${result.author}`),
  );
  const controls = node("div", "arxiv-results-actions");
  controls.append(
    button("Select all", () => {
      result.papers.forEach(paper => task.authorSelection.add(paper.id));
      section.querySelectorAll('input[type="checkbox"]').forEach(input => { input.checked = true; });
      updateAuthorSelectionAction(task);
    }, "button"),
    button("Select none", () => {
      task.authorSelection.clear();
      section.querySelectorAll('input[type="checkbox"]').forEach(input => { input.checked = false; });
      updateAuthorSelectionAction(task);
    }, "button"),
  );
  const list = node("div", "arxiv-paper-list");
  result.papers.forEach(paper => {
    const row = node("label", "arxiv-paper-choice");
    const input = node("input");
    input.type = "checkbox";
    input.checked = task.authorSelection.has(paper.id);
    input.addEventListener("change", () => {
      if (input.checked) task.authorSelection.add(paper.id);
      else task.authorSelection.delete(paper.id);
      updateAuthorSelectionAction(task);
    });
    const copy = node("span", "arxiv-paper-copy");
    const date = String(paper.published || "").slice(0, 10);
    copy.append(
      node("strong", "", paper.title || paper.id),
      node("small", "", `${date ? `${date} · ` : ""}arXiv:${paper.id}`),
      node("small", "", (paper.authors || []).join(", ") || "Authors unavailable"),
    );
    row.append(input, copy);
    list.append(row);
  });
  if (!result.papers.length) {
    list.append(node("p", "", "No papers matched this author search."));
  }
  section.append(heading, controls, list);
  return section;
}

async function fetchArxivAuthorPapers() {
  const task = state.dialog;
  saveDialogOptions();
  dialogFooter.querySelectorAll("button").forEach(value => { value.disabled = true; });
  const fetchButton = dialogFooter.querySelector(".primary");
  if (fetchButton) fetchButton.textContent = "Searching arXiv…";
  dialog.setAttribute("aria-busy", "true");
  try {
    const result = await api("/api/arxiv/author-search", {
      method: "POST",
      body: {
        author: task.options.author || "",
        limit: task.options.authorLimit || 100,
      },
    });
    task.authorSearch = result;
    task.authorSelection = new Set(result.papers.map(paper => paper.id));
    renderTaskConfiguration();
  } catch (error) {
    renderTaskConfiguration(error.message);
  } finally {
    dialog.removeAttribute("aria-busy");
  }
}

function renderTaskConfiguration(errorMessage = "") {
  const task = state.dialog;
  dialogEyebrow.textContent = "Step 1 of 2 · Configure";
  dialogTitle.textContent = actionNames[task.action];
  dialogBody.replaceChildren();
  const targets = node("div", "target-list");
  renderTaskTargetChips(task, targets);
  if (task.targets.length) dialogBody.append(targets);
  if (errorMessage) dialogBody.append(node("div", "error-box", errorMessage));
  const grid = node("div", "form-grid");
  const options = task.options;
  if (task.action === "download") {
    const roots = state.settings.paperRoots || [];
    const acquisition = options.acquisition || "ids";
    if (!task.authorSearch) {
      grid.append(field("acquisition", "Find papers by", {
        type: "select", value: acquisition,
        options: [["ids", "IDs or URLs"], ["author", "Author name"]],
      }));
    }
    if (acquisition === "ids" && !task.authorSearch) {
      grid.append(field("papers", "arXiv IDs or URLs", {
        type: "textarea", value: options.papers || "", full: true,
        help: "Enter one paper per line. IDs, citations, and arXiv abs/pdf/src/html URLs are accepted.",
      }));
    } else if (!task.authorSearch) {
      grid.append(field("author", "Author name", {
        value: options.author || "",
        help: "Author matching is approximate. Search first, then choose individual papers.",
      }));
      grid.append(field("authorLimit", "Maximum papers", {
        type: "number", value: options.authorLimit || 100,
        help: "Fetch between 1 and 100 results for selection.",
      }));
    } else {
      grid.append(arxivAuthorResults(task));
    }
    grid.append(field("outputDirectory", "Paper collection", {
      type: "select", value: options.outputDirectory || roots[0] || "",
      options: roots.map(value => [value, value]),
      help: "The paper directories will be created below this configured root.",
    }));
    grid.append(checkbox(
      "force", "Replace matching downloads",
      "Re-download files for arXiv directories that already exist.",
      options.force,
    ));
    grid.append(field("priorityLevel", "Scheduling weight", {
      type: "select", value: options.priorityLevel ?? "0", options: priorityOptions(),
    }));
    dialogBody.append(grid);
    grid.querySelectorAll("input, textarea, select").forEach(input => input.addEventListener("input", saveDialogOptions));
    const acquisitionInput = grid.querySelector('[name="acquisition"]');
    acquisitionInput?.addEventListener("change", () => {
      saveDialogOptions();
      task.authorSearch = null;
      task.authorSelection.clear();
      renderTaskConfiguration();
    });
    if (task.authorSearch) {
      dialogFooter.replaceChildren(
        button("Search again", () => {
          task.authorSearch = null;
          task.authorSelection.clear();
          renderTaskConfiguration();
        }),
        button("Review downloads", reviewTask, "button primary"),
      );
      updateAuthorSelectionAction(task);
    } else if (acquisition === "author") {
      dialogFooter.replaceChildren(
        button("Cancel", () => dialog.close()),
        button("Fetch papers", fetchArxivAuthorPapers, "button primary"),
      );
    } else {
      dialogFooter.replaceChildren(
        button("Cancel", () => dialog.close()),
        button("Review download", reviewTask, "button primary"),
      );
    }
    return;
  }
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
  if (["metadata", "analyze", "triage", "literature", "review"].includes(task.action)) {
    grid.append(checkbox("force", "Force replacement", "Run even if matching current output exists.", options.force));
  }
  if (task.action === "analyze") {
    grid.append(checkbox("recoverComplete", "Recover completed workspace", "Install a preserved completed analysis without a new model turn.", options.recoverComplete));
  }
  if (task.action === "write") {
    if (task.targets.some(value => value.kind === "attempt")) {
      const pin = checkbox(
        "pinAttempts",
        "Pin to attempt",
        "Otherwise the manuscript tracks each problem and uses its latest attempt when writing or revising.",
        options.pinAttempts === true,
      );
      pin.querySelector("input").addEventListener("change", () => {
        saveDialogOptions();
        renderTaskTargetChips(task, targets);
      });
      grid.append(pin);
    }
    grid.append(field("authors", "Authors", { type: "textarea", value: Array.isArray(options.authors) ? options.authors.join("\n") : options.authors || "", help: "One author per line." }));
    grid.append(field("title", "Title direction", { value: options.title || "" }));
    grid.append(field("name", "Manuscript directory name", { value: options.name || "", help: "Leave blank for the derived name." }));
  }
  if (task.action === "revise") {
    grid.append(field("authors", "Override authors", { type: "textarea", value: Array.isArray(options.authors) ? options.authors.join("\n") : options.authors || "", help: "Leave blank to inherit." }));
    grid.append(field("title", "Override title direction", { value: options.title || "" }));
    grid.append(checkbox("refreshResults", "Refresh result selection", "Promote stored selectors to paper scope and use latest attempts.", options.refreshResults));
  }
  grid.append(field("priorityLevel", "Scheduling weight", {
    type: "select",
    value: options.priorityLevel ?? "0",
    options: priorityOptions(),
    help: "Relative weight of worker starts when this task competes with other eligible tasks.",
  }));

  const modelSettings = node("section", "model-settings");
  modelSettings.append(node("h3", "", "Model and web-search settings"));
  const advancedGrid = node("div", "form-grid");
  const taskDefaults = state.settings.taskDefaults?.[task.action] || {};
  advancedGrid.append(field("model", "Model", {
    value: options.model || "",
    help: `Default: ${taskDefaults.model || "unavailable"}.`,
  }));
  advancedGrid.append(field("reasoningEffort", "Reasoning effort", {
    type: "select", value: options.reasoningEffort || "",
    options: [["", `Default (${taskDefaults.reasoningEffort || "unavailable"})`], ...["low", "medium", "high", "xhigh", "max", "ultra"].map(value => [value, value])],
  }));
  advancedGrid.append(checkbox("fast", "Fast service tier", "Uses additional credits.", options.fast));
  if (["literature", "solve", "review", "write", "revise"].includes(task.action)) {
    advancedGrid.append(field("webSearch", "Web search", {
      type: "select", value: options.webSearch || "",
      options: [["", `Default (${reviewModel.titleize(taskDefaults.webSearch || "unavailable")})`], ["live", "Live"], ["indexed", "Indexed"], ["disabled", "Disabled"]],
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
  modelSettings.append(advancedGrid);
  grid.append(modelSettings);
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
  const collected = collectDialogOptions();
  if (state.dialog.action === "download" && state.dialog.authorSearch) {
    for (const name of ["acquisition", "author", "authorLimit"]) {
      if (Object.hasOwn(state.dialog.options, name)) {
        collected[name] = state.dialog.options[name];
      }
    }
  }
  state.dialog.options = collected;
  sessionStorage.setItem(state.dialog.storageKey, JSON.stringify(state.dialog.options));
}

function taskRequestOptions(task) {
  const options = { ...task.options };
  if (task.action !== "download") return options;
  if (task.authorSearch) {
    options.papers = selectedAuthorPaperIds(task).join("\n");
  }
  delete options.acquisition;
  delete options.author;
  delete options.authorLimit;
  return options;
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
      body: {
        action: task.action,
        targets: taskTargetsForRequest(task),
        options: taskRequestOptions(task),
      },
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
  const intro = node("p", "", `This will queue ${plan.units.length} managed run${plan.units.length === 1 ? "" : "s"} with a ${priorityMultiplier(plan.priorityLevel)} scheduling weight. Nothing has started yet.`);
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
  invalidateReviewDetails();
  if (navigationReady) syncNavigation({ replace: true, preserveScroll: true });
  else render();
}

async function refreshJobs({ preserveActivityDetail = false } = {}) {
  const value = await api("/api/jobs");
  state.jobs = value.jobs;
  updateActivityCount();
  if (navigationReady && ["research", "papers", "manuscripts"].includes(state.tab)) {
    syncRelatedTasks();
    return;
  }
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
        if (!state.catalog.version) {
          state.catalog.loading = true;
          state.catalog.progress = value;
          render();
        }
      } else if (["catalog.changed", "catalog.error"].includes(value.type)) {
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
