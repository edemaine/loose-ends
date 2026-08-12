"use strict";

const reviewModel = window.LooseEndsReviewModel;
if (!reviewModel) throw new Error("Shared review model failed to load");

const state = {
  csrf: "",
  eventSequence: 0,
  catalog: { papers: [], reviews: [], manuscripts: [], counts: {} },
  jobs: [],
  tab: "research",
  search: "",
  selectedReview: "",
  selectedProblem: "",
  researchFilters: reviewModel.createDefaultFilters(),
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
  dialog: null,
};

const sidebar = document.getElementById("sidebar");
const main = document.getElementById("main");
const notice = document.getElementById("notice");
const selectionBar = document.getElementById("selection-bar");
const activityCount = document.getElementById("activity-count");
const connection = document.getElementById("connection");
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
const initialPriorities = ["high", "medium"];
const pageScrollPositions = new Map();
let renderedUrl = "";
let scrollUpdateFrame = null;
let navigationReady = false;

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

function currentUrl() {
  const parameters = new URLSearchParams();
  if (state.search.trim()) parameters.set("q", state.search.trim());
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

function rememberCurrentScroll({ updateHistory = true } = {}) {
  if (!renderedUrl) return;
  const scrollY = window.scrollY;
  pageScrollPositions.set(renderedUrl, scrollY);
  if (updateHistory && history.state?.looseEndsWorkbench) {
    history.replaceState(historyPayload(scrollY), "", renderedUrl);
  }
}

function restorePageScroll(url, preferredScroll) {
  const scrollY = Number.isFinite(preferredScroll)
    ? preferredScroll
    : pageScrollPositions.get(url) ?? 0;
  pageScrollPositions.set(url, scrollY);
  requestAnimationFrame(() => window.scrollTo({ top: scrollY, left: 0, behavior: "auto" }));
}

function syncNavigation({ replace = false, preserveScroll = false } = {}) {
  if (!navigationReady) return;
  rememberCurrentScroll();
  render();
  const url = currentUrl();
  const scrollY = preserveScroll ? window.scrollY : pageScrollPositions.get(url) ?? 0;
  const method = replace || url === renderedUrl ? "replaceState" : "pushState";
  history[method](historyPayload(scrollY), "", url);
  renderedUrl = url;
  restorePageScroll(url, scrollY);
}

function applyLocation({ scrollY } = {}) {
  const parameters = new URLSearchParams(location.search);
  state.tab = tabFromPath(location.pathname);
  state.search = parameters.get("q") || "";
  if (state.tab === "research") {
    state.researchFilters = reviewModel.filtersFromSearchParams(parameters, initialPriorities);
    const identity = reviewModel.identityFromSearchParams(parameters);
    const legacy = decodeURIComponent(location.hash.slice(1));
    const requested = reviewModel.findReviewItem(state.catalog.reviews, identity) ||
      state.catalog.reviews.find(item => item.id === legacy || item.itemKey === legacy);
    state.selectedReview = requested?.itemKey || "";
    state.selectedProblem = requested?.problemKey || "";
    state.detailTab = parameters.get("detail") || "attempt";
  } else if (state.tab === "papers") {
    const requested = parameters.get("paper");
    state.selectedPaper = state.catalog.papers.find(
      item => item.urlKey === requested || item.path === requested,
    )?.key || "";
  } else if (state.tab === "manuscripts") {
    const requested = parameters.get("manuscript");
    const manuscript = state.catalog.manuscripts.find(
      item => item.urlKey === requested || item.path === requested,
    );
    state.selectedManuscript = manuscript?.key || "";
    const requestedDraft = parameters.get("draft");
    state.selectedDraft = manuscript?.drafts.find(
      draft => draft.name === requestedDraft || draft.urlKey === requestedDraft,
    )?.key || manuscript?.latest.key || "";
  } else {
    state.selectedJob = parameters.get("job") || "";
  }
  render();
  const canonicalUrl = currentUrl();
  const restoredScroll = Number.isFinite(scrollY) ? scrollY : pageScrollPositions.get(canonicalUrl) ?? 0;
  history.replaceState(historyPayload(restoredScroll), "", canonicalUrl);
  renderedUrl = canonicalUrl;
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

function fileUrl(path) {
  return `/api/file?${new URLSearchParams({ path })}`;
}

async function api(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (options.body && typeof options.body !== "string") {
    headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(options.body);
  }
  if (options.method && options.method !== "GET") {
    headers["X-Workbench-CSRF"] = state.csrf;
  }
  const response = await fetch(path, { ...options, headers });
  const result = await response.json().catch(() => ({ error: response.statusText }));
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
  return target("attempt", item.attemptDirectory, `${item.problemId}/${item.attemptName}`);
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
  }
  if ([...kinds].every(kind => kind === "problem")) {
    selectionBar.append(button("Triage", () => openTask("triage", values)));
    selectionBar.append(button("Literature", () => openTask("literature", values)));
    selectionBar.append(button("Solve", () => openTask("solve", values), "button primary"));
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

document.querySelectorAll("[data-tab]").forEach(value => {
  value.addEventListener("click", () => setTab(value.dataset.tab));
});

function render() {
  document.querySelectorAll("[data-tab]").forEach(value => {
    value.classList.toggle("active", value.dataset.tab === state.tab);
  });
  const active = state.jobs.filter(job => ["queued", "running"].includes(job.status)).length;
  activityCount.textContent = active ? String(active) : "";
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
  renderSelectionBar();
}

function sidebarSearch(placeholder) {
  const input = node("input", "search");
  input.type = "search";
  input.placeholder = placeholder;
  input.value = state.search;
  input.addEventListener("input", () => {
    state.search = input.value;
    syncNavigation({ replace: true, preserveScroll: true });
    const replacement = sidebar.querySelector("input.search");
    replacement?.focus();
    replacement?.setSelectionRange(state.search.length, state.search.length);
  });
  return input;
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

function filterToggle(label, checked, handler) {
  const wrapper = node("label", "filter-toggle");
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
  const summary = node("summary");
  summary.append(node("span", "", "Problem filters"));
  summary.append(visibleProblemSelectionControl());
  details.append(summary);
  const controls = node("div", "research-filter-grid");
  controls.append(
    filterControl("Attempt status", "attemptStatus", reviewModel.filterOptions.attemptStatus),
    filterControl("Claim type", "claim", reviewModel.filterOptions.claim),
    filterControl("Correctness", "correctness", reviewModel.filterOptions.correctness),
    filterControl("Coverage", "coverage", reviewModel.filterOptions.coverage),
    filterControl("Importance", "importance", reviewModel.filterOptions.importance),
    filterControl("Verification", "confidence", reviewModel.filterOptions.confidence),
    filterControl("Literature", "literature", reviewModel.filterOptions.literature),
  );
  const toggles = node("div", "filter-toggles");
  const available = reviewModel.availableFilters(state.catalog.reviews);
  reviewModel.priorityLevels.forEach(priority => {
    if (!available.priorities.has(priority)) return;
    toggles.append(filterToggle(reviewModel.titleize(priority), state.researchFilters.priorities.has(priority), checked => {
      if (checked) state.researchFilters.priorities.add(priority);
      else state.researchFilters.priorities.delete(priority);
    }));
  });
  if (available.current) {
    toggles.append(filterToggle("Current", state.researchFilters.current, checked => { state.researchFilters.current = checked; }));
  }
  if (available.stale) {
    toggles.append(filterToggle("Stale", state.researchFilters.stale, checked => { state.researchFilters.stale = checked; }));
  }
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

function renderResearch() {
  sidebar.replaceChildren();
  const controls = node("div", "research-controls");
  controls.append(sidebarSearch("Search open problems…"), renderResearchFilters());
  sidebar.append(controls);
  const reviews = filteredReviews();
  const problems = reviewModel.latestProblems(reviews);
  const requested = state.catalog.reviews.find(item => item.itemKey === state.selectedReview);
  if (requested) state.selectedProblem = requested.problemKey;
  if (!problems.some(item => item.problemKey === state.selectedProblem)) {
    state.selectedProblem = problems[0]?.problemKey || "";
  }
  const listScroll = node("div", "problem-scroll");
  listScroll.append(node("div", "sidebar-heading queue-summary", reviewModel.queueSummary(reviews, state.researchFilters)));
  for (const group of reviewModel.groupProblemsByPaper(reviews)) {
    listScroll.append(node("div", "sidebar-heading paper-heading", group.paperTitle));
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
  sidebar.replaceChildren(sidebarSearch("Search source papers…"));
  const query = state.search.trim().toLowerCase();
  const papers = state.catalog.papers.filter(paper => !query || `${paper.title} ${paper.name} ${paper.authors.join(" ")}`.toLowerCase().includes(query));
  const list = node("div", "side-list");
  papers.forEach(paper => appendSideCard(list, {
    title: paper.title,
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
  sidebar.replaceChildren(sidebarSearch("Search manuscripts…"));
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
  sidebar.replaceChildren(sidebarSearch("Search tasks…"));
  const query = state.search.trim().toLowerCase();
  const jobs = state.jobs.filter(job => !query || `${job.title} ${job.action} ${job.status}`.toLowerCase().includes(query));
  const list = node("div", "side-list");
  jobs.forEach(job => appendSideCard(list, {
    title: job.title,
    meta: `${humanize(job.status)} · ${formatTime(job.created_at)}`,
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
  copy.append(node("div", "eyebrow", `${humanize(job.action)} task`));
  copy.append(node("h1", "", job.title));
  copy.append(node("p", "", `Created ${formatTime(job.created_at)}`));
  const badges = node("div", "badges");
  badges.append(badge(job.status));
  copy.append(badges);
  hero.append(copy);
  shell.append(hero);
  job.runs.forEach((run, index) => {
    const section = node("section", "section panel");
    const heading = node("div", "section-title");
    const title = node("h2", "", `${index + 1}. ${run.label}`);
    heading.append(title, badge(run.status));
    section.append(heading);
    const meta = node("p", "", `Started ${formatTime(run.started_at)} · Exit ${run.exit_code ?? "—"}`);
    section.append(meta);
    const actions = node("div", "actions");
    if (["queued", "starting", "running", "cancel_requested"].includes(run.status)) {
      actions.append(button("Cancel", () => mutateRun(run.id, "cancel"), "button danger"));
    }
    if (["failed", "canceled", "interrupted"].includes(run.status) && !(run.outputs || []).length) {
      actions.append(button("Retry", () => mutateRun(run.id, "retry"), "button primary"));
    }
    section.append(actions);
    if (run.error) section.append(node("div", "error-box", run.error));
    if (run.outputs?.length) {
      const output = node("div", "confirm-block");
      output.append(node("h3", "", "Installed output"));
      run.outputs.forEach(path => {
        const row = node("div", "file-link");
        row.append(node("strong", "", path.split(/[\\/]/).pop()));
        row.append(node("small", "", path));
        row.addEventListener("click", () => openOutput(path));
        output.append(row);
      });
      section.append(output);
    }
    const command = node("div", "confirm-block");
    command.append(node("h3", "", "Command"), node("pre", "command", run.argv.join(" ")));
    section.append(command);
    const cachedLog = state.runLogs.get(run.id);
    const log = node("pre", "console", runLogText(cachedLog));
    log.dataset.runLog = run.id;
    section.append(log);
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

function openOutput(path) {
  const review = state.catalog.reviews.find(item => item.attemptDirectory === path);
  if (review) {
    state.selectedReview = review.itemKey;
    state.selectedProblem = review.problemKey;
    setTab("research");
    return;
  }
  const problem = state.catalog.reviews.find(item => `${item.paperDirectory}/${item.problemId}`.replaceAll("\\", "/") === path.replaceAll("\\", "/"));
  if (problem) {
    state.selectedReview = problem.itemKey;
    state.selectedProblem = problem.problemKey;
    setTab("research");
    return;
  }
  const paperPath = path.replace(/[\\/]analysis$/, "");
  const paper = state.catalog.papers.find(item => item.path === path || item.path === paperPath);
  if (paper) {
    state.selectedPaper = paper.key;
    setTab("papers");
    return;
  }
  for (const manuscript of state.catalog.manuscripts) {
    if (manuscript.drafts.some(draft => draft.path === path)) {
      state.selectedManuscript = manuscript.key;
      state.selectedDraft = manuscript.drafts.find(draft => draft.path === path)?.key || manuscript.latest.key;
      setTab("manuscripts");
      return;
    }
  }
}

const actionNames = {
  analyze: "Analyze papers", triage: "Triage problems", literature: "Search literature",
  solve: "Solve problems", review: "Review attempts", write: "Write paper", revise: "Revise manuscript",
};

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
  try {
    task.plan = await api("/api/plans", {
      method: "POST",
      body: { action: task.action, targets: task.targets, options: task.options },
    });
    renderTaskConfirmation();
  } catch (error) {
    renderTaskConfiguration(error.message);
  }
}

function renderTaskConfirmation() {
  const task = state.dialog;
  const plan = task.plan;
  dialogEyebrow.textContent = "Step 2 of 2 · Confirm";
  dialogTitle.textContent = plan.title;
  dialogBody.replaceChildren();
  const intro = node("p", "", `This will queue ${plan.units.length} managed run${plan.units.length === 1 ? "" : "s"}. Nothing has started yet.`);
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
    block.append(node("h3", "", `${index + 1}. ${unit.label}`));
    block.append(node("pre", "command", unit.command));
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
  if (
    navigationReady &&
    preserveActivityDetail &&
    state.tab === "activity"
  ) {
    const active = state.jobs.filter(job => ["queued", "running"].includes(job.status)).length;
    activityCount.textContent = active ? String(active) : "";
    renderActivity({ preserveDetail: true });
    const url = currentUrl();
    history.replaceState(historyPayload(window.scrollY), "", url);
    renderedUrl = url;
  } else if (navigationReady) {
    syncNavigation({ replace: true, preserveScroll: true });
  } else {
    render();
  }
}

function connectEvents() {
  const events = new EventSource(`/api/events?${new URLSearchParams({
    since: String(state.eventSequence || 0),
  })}`);
  events.addEventListener("open", () => {
    connection.textContent = "Live";
    connection.className = "connection live";
  });
  events.addEventListener("error", () => {
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
