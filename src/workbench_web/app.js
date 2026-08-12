"use strict";

const state = {
  csrf: "",
  catalog: { papers: [], reviews: [], manuscripts: [], counts: {} },
  jobs: [],
  tab: "research",
  search: "",
  selectedReview: "",
  selectedPaper: "",
  selectedManuscript: "",
  selectedJob: "",
  detailTab: "problem",
  detailCache: new Map(),
  selection: new Map(),
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

let markdownRenderer = null;
try {
  if (typeof window.markdownit === "function") {
    markdownRenderer = window.markdownit({ html: false, linkify: true, breaks: false });
    if (window.mdItPluginKatex?.katex && window.katex?.renderToString) {
      markdownRenderer.use(window.mdItPluginKatex.katex, { throwOnError: false });
    }
    const originalLink = markdownRenderer.renderer.rules.link_open ||
      ((tokens, index, options, env, self) => self.renderToken(tokens, index, options));
    markdownRenderer.renderer.rules.link_open = (tokens, index, options, env, self) => {
      tokens[index].attrSet("target", "_blank");
      tokens[index].attrSet("rel", "noreferrer");
      return originalLink(tokens, index, options, env, self);
    };
  }
} catch (error) {
  console.warn("Markdown rendering unavailable", error);
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
  return node("span", `badge ${extra || value || "neutral"}`, humanize(value || "none"));
}

function humanize(value) {
  return String(value || "").replaceAll("_", " ").replace(/\b\w/g, letter => letter.toUpperCase());
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
  renderSelectionBar();
}

function selectionCheckbox(value) {
  const input = node("input");
  input.type = "checkbox";
  input.checked = state.selection.has(targetKey(value));
  input.setAttribute("aria-label", `Select ${value.label}`);
  input.addEventListener("click", event => event.stopPropagation());
  input.addEventListener("change", () => toggleSelection(value, input.checked));
  return input;
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
  document.querySelectorAll("[data-tab]").forEach(value => {
    value.classList.toggle("active", value.dataset.tab === tab);
  });
  render();
}

document.querySelectorAll("[data-tab]").forEach(value => {
  value.addEventListener("click", () => setTab(value.dataset.tab));
});

function render() {
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
    render();
    const replacement = sidebar.querySelector("input.search");
    replacement?.focus();
    replacement?.setSelectionRange(state.search.length, state.search.length);
  });
  return input;
}

function appendSideCard(parent, { title, meta, active, selectedTarget, onClick }) {
  const card = node("div", `side-card${active ? " active" : ""}`);
  card.role = "button";
  card.tabIndex = 0;
  if (selectedTarget) card.append(selectionCheckbox(selectedTarget));
  else card.append(node("span"));
  const copy = node("span");
  copy.append(node("strong", "", title));
  copy.append(node("small", "", meta));
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
  const query = state.search.trim().toLowerCase();
  if (!query) return state.catalog.reviews;
  return state.catalog.reviews.filter(item => [
    item.paperTitle, item.problemId, item.problemTitle, item.attemptName,
    item.solverSummary, item.criticSummary, item.triageSummary,
  ].join(" ").toLowerCase().includes(query));
}

function renderResearch() {
  sidebar.replaceChildren(sidebarSearch("Search problems and attempts…"));
  const reviews = filteredReviews();
  const groups = new Map();
  reviews.forEach(item => {
    if (!groups.has(item.paperTitle)) groups.set(item.paperTitle, []);
    groups.get(item.paperTitle).push(item);
  });
  for (const [paper, items] of groups) {
    sidebar.append(node("div", "sidebar-heading", paper));
    const list = node("div", "side-list");
    items.forEach(item => {
      const selectable = item.attemptDirectory ? attemptTarget(item) : problemTarget(item);
      appendSideCard(list, {
        title: `${item.problemId} · ${item.problemTitle}`,
        meta: item.attemptName ? `${item.attemptName} · ${humanize(item.attemptStatus)}` : "Unattempted",
        active: state.selectedReview === item.itemKey,
        selectedTarget: selectable,
        onClick: () => {
          state.selectedReview = item.itemKey;
          state.detailTab = item.attemptDirectory ? "attempt" : "problem";
          renderResearch();
        },
      });
    });
    sidebar.append(list);
  }
  if (!state.selectedReview || !reviews.some(item => item.itemKey === state.selectedReview)) {
    state.selectedReview = reviews[0]?.itemKey || "";
  }
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
  const hero = node("section", "hero");
  const copy = node("div");
  copy.append(node("div", "eyebrow", `${item.paperTitle} · ${item.problemId}`));
  copy.append(node("h1", "", item.problemTitle));
  copy.append(node("p", "", item.paperAuthors?.join(", ") || "Authors unavailable"));
  const badges = node("div", "badges");
  badges.append(badge(item.explicitness, "neutral"));
  badges.append(badge(item.attemptStatus));
  if (item.claimedResultType) badges.append(badge(item.claimedResultType));
  if (item.priority) badges.append(badge(`${item.priority} priority`, item.priority === "high" ? "error" : "warn"));
  if (item.current === false) badges.append(badge("stale review", "error"));
  copy.append(badges);
  hero.append(copy);
  shell.append(hero);

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

  const summaries = node("div", "summary-grid");
  if (item.triageSummary) summaries.append(summaryPanel("Triage", item.triageSummary));
  if (item.literatureSummary) summaries.append(summaryPanel("Literature", item.literatureSummary));
  if (item.solverSummary) summaries.append(summaryPanel("Solver", item.solverSummary));
  if (item.criticSummary) summaries.append(summaryPanel("Independent review", item.criticSummary));
  if (summaries.children.length) shell.append(summaries);

  if (item.blockingGaps?.length || item.recommendedNextSteps?.length) {
    const section = node("section", "section panel");
    section.append(node("h2", "", "Research follow-up"));
    const list = node("ul");
    [...(item.blockingGaps || []), ...(item.recommendedNextSteps || [])].forEach(value => list.append(node("li", "", value)));
    section.append(list);
    shell.append(section);
  }

  const tabs = [
    ["problem", "Problem"],
    ...(item.attemptDirectory ? [["attempt", "Attempt"], ["critique", "Critique"]] : []),
    ["literature", "Literature"],
    ["files", "Files"],
  ];
  if (!tabs.some(([key]) => key === state.detailTab)) state.detailTab = tabs[0][0];
  const tabbar = node("div", "detail-tabs");
  tabs.forEach(([key, label]) => {
    tabbar.append(button(label, () => {
      state.detailTab = key;
      renderReviewDetail(item);
    }, `detail-tab${state.detailTab === key ? " active" : ""}`));
  });
  shell.append(tabbar);
  const section = node("section", "section");
  if (state.detailTab === "problem") section.append(markdown(item.problemStatement, "Loading problem statement…"));
  else if (state.detailTab === "attempt") section.append(markdown(item.solverAttempt, "Loading solver attempt…"));
  else if (state.detailTab === "critique") section.append(markdown(item.critique, "No critique is installed."));
  else if (state.detailTab === "literature") section.append(markdown(item.literatureReport, "No literature report is installed."));
  else section.append(fileGrid(item.files || []));
  shell.append(section);
  main.replaceChildren(shell);
}

function summaryPanel(title, value) {
  const panel = node("section", "panel");
  panel.append(node("h2", "", title));
  panel.append(node("p", "", value));
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
    link.rel = "noreferrer";
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
    onClick: () => { state.selectedPaper = paper.key; renderPapers(); },
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
    onClick: () => { state.selectedManuscript = value.key; renderManuscripts(); },
  }));
  sidebar.append(node("div", "sidebar-heading", `${manuscripts.length} manuscripts`), list);
  if (!state.selectedManuscript || !manuscripts.some(value => value.key === state.selectedManuscript)) state.selectedManuscript = manuscripts[0]?.key || "";
  const manuscript = state.catalog.manuscripts.find(value => value.key === state.selectedManuscript);
  if (!manuscript) {
    main.replaceChildren(document.getElementById("empty-template").content.cloneNode(true));
    return;
  }
  const draft = manuscript.latest;
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
    const card = node("div", "entity-card");
    card.append(node("strong", "", value.name));
    card.append(node("small", "", `${humanize(value.status)} · ${humanize(value.verdict)}`));
    history.append(card);
  });
  shell.append(historyHeading, history);
  main.replaceChildren(shell);
}

function renderActivity() {
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
      renderActivity();
      loadJob(job.id);
    },
  }));
  sidebar.append(node("div", "sidebar-heading", `${jobs.length} tasks`), list);
  if (!state.selectedJob || !jobs.some(job => job.id === state.selectedJob)) state.selectedJob = jobs[0]?.id || "";
  if (!state.selectedJob) {
    main.replaceChildren(document.getElementById("empty-template").content.cloneNode(true));
    return;
  }
  const cached = state.jobs.find(job => job.id === state.selectedJob);
  const shell = node("div", "main-inner");
  shell.append(node("div", "loading", cached ? `Loading ${cached.title}…` : "Loading task…"));
  main.replaceChildren(shell);
  loadJob(state.selectedJob);
}

async function loadJob(id) {
  try {
    const job = await api(`/api/jobs/${id}`);
    if (state.tab === "activity" && state.selectedJob === id) renderJobDetail(job);
  } catch (error) {
    showNotice(error.message, true);
  }
}

function renderJobDetail(job) {
  const shell = node("div", "main-inner");
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
    const log = node("pre", "console", "Loading output…");
    section.append(log);
    shell.append(section);
    api(`/api/runs/${run.id}/log?offset=0`).then(value => {
      log.textContent = value.text || "No console output yet.";
      log.scrollTop = log.scrollHeight;
    }).catch(error => { log.textContent = error.message; });
  });
  main.replaceChildren(shell);
}

async function mutateRun(runId, action) {
  const message = action === "retry"
    ? "Queue a retry of this exact run?"
    : "Stop this run and its child processes?";
  if (!window.confirm(message)) return;
  try {
    await api(`/api/runs/${runId}/${action}`, { method: "POST", body: {} });
    await refreshJobs();
    if (state.selectedJob) loadJob(state.selectedJob);
  } catch (error) {
    showNotice(error.message, true);
  }
}

function openOutput(path) {
  const review = state.catalog.reviews.find(item => item.attemptDirectory === path);
  if (review) {
    state.selectedReview = review.itemKey;
    setTab("research");
    return;
  }
  const problem = state.catalog.reviews.find(item => `${item.paperDirectory}/${item.problemId}`.replaceAll("\\", "/") === path.replaceAll("\\", "/"));
  if (problem) {
    state.selectedReview = problem.itemKey;
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
  render();
}

async function refreshJobs() {
  const value = await api("/api/jobs");
  state.jobs = value.jobs;
  if (state.tab === "activity") renderActivity();
  else render();
}

function connectEvents() {
  const events = new EventSource("/api/events");
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
        refreshJobs().then(() => {
          if (state.tab === "activity" && state.selectedJob) loadJob(state.selectedJob);
        }).catch(error => showNotice(error.message, true));
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
    state.catalog = value.catalog;
    state.jobs = value.jobs;
    render();
    connectEvents();
  } catch (error) {
    main.replaceChildren(node("div", "error-box", `Could not start workbench: ${error.message}`));
  }
}

start();
