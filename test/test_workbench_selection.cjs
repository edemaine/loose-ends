const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { test } = require("node:test");
const vm = require("node:vm");
require("../src/workbench_web/review_model.js");
const reviewModel = globalThis.LooseEndsReviewModel;

const source = readFileSync(`${__dirname}/../src/workbench_web/app.js`, "utf8");
const functions = [
  "filterControl", "filterToggle", "renderResearchFilters", "filteredReviews", "renderResearch",
  "paperFilterControl", "renderPaperFilters", "filteredPapers", "renderPapers",
  "manuscriptFilterControl", "renderManuscriptFilters", "filteredManuscripts", "renderManuscripts",
  "sidebarSearch", "renderActivity",
  "syncListNavigation", "restoreSidebarScroll", "revealIfHidden", "revealCentered",
  "paperSortControl", "manuscriptSortControl", "normalizeManuscriptSort",
].map(name => source.match(new RegExp(`^function ${name}[(][^]*?^}`, "m"))[0]).join("\n");

test("nearest matching selection keeps matches, measures distance, and prefers following ties", () => {
  const order = ["a", "b", "c", "d", "e"];
  const nearest = (selected, matches) => reviewModel.nearestMatchingKey(selected, matches, order);
  assert.equal(nearest("c", ["a", "c", "e"]), "c");
  assert.equal(nearest("c", ["b", "e"]), "b");
  assert.equal(nearest("c", ["a", "d"]), "d");
  assert.equal(nearest("c", ["a", "e"]), "e");
  assert.equal(nearest("a", ["e"]), "e");
  assert.equal(nearest("e", ["a"]), "a");
  assert.equal(nearest("missing", ["b", "d"]), "b");
  assert.equal(nearest("", ["b", "d"]), "b");
  assert.equal(nearest("c", []), "");
});

function harness(view, sort = "alphabetical") {
  const keys = ["a", "b", "c", "d", "e"];
  const title = key => `${key} ${key === "c" ? "excluded" : "match"}`;
  const reviews = keys.map((key, index) => ({
    itemKey: `${key}-1`, problemKey: key, problemId: "OP-001", problemTitle: title(key),
    paperTitle: title(key), paperDirectory: key, paperActivityTimestamp: index + 1,
    attemptNumber: 1, attemptStatus: "reviewed", priority: "high", current: true,
    claimedResultType: key === "c" ? "none" : "solution",
  }));
  const papers = keys.map((key, index) => ({
    key, path: key, title: title(key), activityTimestamp: index + 1,
    metadataComplete: key !== "c",
  }));
  const manuscripts = keys.map((key, index) => {
    const latest = { key: `${key}-draft`, title: title(key), createdTimestamp: index + 1,
      status: key === "c" ? "blocked" : "draft_complete" };
    return { key, name: key, latest, drafts: [{ key: `${key}-old` }, latest] };
  });
  const state = {
    catalog: { reviews, papers, manuscripts }, search: "", paperSort: sort, manuscriptSort: sort,
    selectedProblem: "c", selectedReview: "c-1", selectedPaper: "c",
    selectedManuscript: "c", selectedDraft: "c-old", manuscriptDraftSelections: new Map(),
    researchFilters: reviewModel.createDefaultFilters(), paperFilters: reviewModel.createDefaultPaperFilters(),
    manuscriptFilters: reviewModel.createDefaultManuscriptFilters(), detailCache: new Map(),
    jobs: keys.map(id => ({ id, title: title(id) })), selectedJob: "c", jobDetails: new Map(),
  };
  const cards = [];
  const detailReached = Symbol("detail reached");
  function node(tag, className, text) {
    // Selection and sidebar rendering finish before the paper/manuscript detail panel.
    if (className === "main-inner") throw detailReached;
    return { tag, text, dataset: {}, children: [], handlers: {},
      append(...children) { this.children.push(...children); },
      addEventListener(event, handler) { this.handlers[event] = handler; },
    };
  }
  const context = vm.createContext({
    state, reviewModel, node, renderScrollTarget: null,
    manuscriptSortOptions: [["latest", "Latest drafts"], ["alphabetical", "Alphabetical"]],
    document: { createTextNode: text => text, getElementById: () => ({ content: { cloneNode() {} } }) },
    button: (text, handler) => ({ text, handler }),
    persistentSidebarControls() {}, visibleProblemSelectionControl() {}, visiblePaperSelectionControl() {},
    relatedTaskHost() {}, attemptTagsNode() {}, problemTarget() {}, paperTarget() {}, attemptTarget() {},
    humanize: reviewModel.humanize,
    appendSideCard: (_list, card) => cards.push(card),
    sidebar: node("aside"), main: { replaceChildren() {}, querySelector() {} }, renderReviewDetail() {}, loadReviewDetail() {},
    taskSidebarTitle: job => job.title, taskSidebarMeta() {}, taskBadges() {},
    syncNavigation: options => {
      assert.equal(options.replace, true);
      assert.equal(options.preserveScroll, true);
      assert.equal(state.keepSidebarSelectionVisible, true);
      render();
    },
  });
  vm.runInContext(functions, context);
  function render() {
    cards.length = 0;
    try { context[`render${view}`](); } catch (error) { if (error !== detailReached) throw error; }
  }
  function change(key, value) {
    const name = { Research: "filterControl", Papers: "paperFilterControl", Manuscripts: "manuscriptFilterControl" }[view];
    const select = context[name](key, key, []).children[1];
    select.value = value;
    select.handlers.change();
  }
  function reset() {
    const controls = context[`render${view === "Papers" ? "Paper" : view === "Manuscripts" ? "Manuscript" : "Research"}Filters`]();
    function findReset(element) {
      return element?.text === "Reset" ? element : element?.children?.map(findReset).find(Boolean);
    }
    findReset(controls).handler();
  }
  function search(value) {
    const input = context.sidebarSearch("Search");
    input.value = value;
    input.handlers.input();
  }
  return { state, cards, context, render, change, reset, search };
}

for (const [view, selectedKey, filter, value] of [
  ["Research", "selectedProblem", "claim", "solution"],
  ["Papers", "selectedPaper", "metadata", "complete"],
  ["Manuscripts", "selectedManuscript", "status", "draft_complete"],
]) {
  test(`${view}: filters select the nearest match and keep it on reset`, () => {
    const h = harness(view);
    h.change(filter, value);
    assert.equal(h.state[selectedKey], "d");
    assert.ok(h.cards.some(card => card.active && card.title.includes("d match")));
    h.reset();
    assert.equal(h.state[selectedKey], "d");
    h.change(filter, value);
    assert.equal(h.state[selectedKey], "d");
  });

  test(`${view}: nearest follows the selected sort order`, () => {
    const h = harness(view, view === "Manuscripts" ? "latest" : "activity");
    h.change(filter, value);
    assert.equal(h.state[selectedKey], "b");
  });

  test(`${view}: search selects nearby matches and handles empty results`, () => {
    const h = harness(view);
    h.search("match");
    assert.equal(h.state[selectedKey], "d");
    h.search("");
    assert.equal(h.state[selectedKey], "d");
    h.search("no such item");
    assert.equal(h.state[selectedKey], "");
    if (view === "Research") assert.equal(h.state.selectedReview, "");
    if (view === "Manuscripts") assert.equal(h.state.selectedDraft, "");
    h.search("");
    assert.equal(h.state[selectedKey], "a");
  });
}

test("Research: filtered attempts move to the nearest attempt within the same problem", () => {
  const h = harness("Research");
  const original = h.state.catalog.reviews.find(item => item.problemKey === "c");
  h.state.catalog.reviews.push(...[2, 3, 4, 5].map(attemptNumber => ({
    ...original, attemptNumber, itemKey: `c-${attemptNumber}`,
    claimedResultType: [2, 5].includes(attemptNumber) ? "solution" : "none",
  })));
  h.state.selectedReview = "c-3";
  h.change("claim", "solution");
  assert.equal(h.state.selectedProblem, "c");
  assert.equal(h.state.selectedReview, "c-2");
  h.reset();
  assert.equal(h.state.selectedReview, "c-2");
  const toggle = h.context.filterToggle("Stale", true, checked => { h.state.researchFilters.stale = checked; });
  toggle.children[0].checked = false;
  toggle.children[0].handlers.change();
  assert.equal(h.state.selectedReview, "c-2");
});

test("Activity: search selects the nearest task and highlights it immediately", () => {
  const h = harness("Activity");
  h.search("match");
  assert.equal(h.state.selectedJob, "d");
  assert.ok(h.cards.some(card => card.active && card.title === "d match"));
  h.search("");
  assert.equal(h.state.selectedJob, "d");
  h.search("no such task");
  assert.equal(h.state.selectedJob, "");
});

test("Manuscripts: keep the selected draft when matching and restore the next manuscript's remembered draft", () => {
  const h = harness("Manuscripts");
  h.change("status", "blocked");
  assert.equal(h.state.selectedDraft, "c-old");
  h.reset();
  assert.equal(h.state.selectedDraft, "c-old");
  h.state.manuscriptDraftSelections.set("d", "d-old");
  h.change("status", "draft_complete");
  assert.equal(h.state.selectedManuscript, "d");
  assert.equal(h.state.selectedDraft, "d-old");
});

function scrollContainer(cardTop, cardHeight = 40) {
  return {
    scrollTop: 100, scrollHeight: 1000, clientHeight: 200, dataset: {},
    getBoundingClientRect: () => ({ top: 0, bottom: 200 }),
    querySelector() {
      return cardTop === null ? null : {
        getBoundingClientRect: () => ({ top: cardTop - this.scrollTop, bottom: cardTop + cardHeight - this.scrollTop }),
      };
    },
  };
}

test("revealing the selection scrolls only as far as necessary", () => {
  const { context } = harness("Research");
  for (const [top, height, expected] of [
    [150, 40, 100], // Fully visible: stay put.
    [80, 40, 80], // Partially above the viewport.
    [280, 40, 120], // Partially below the viewport.
    [500, 40, 340], // Far below the viewport.
    [50, 300, 100], // Tall card already spans the viewport.
    [500, 300, 500], // Reveal the top of a tall card.
    [null, 40, 100], // Empty results.
  ]) {
    const container = scrollContainer(top, height);
    context.revealIfHidden(container);
    assert.equal(container.scrollTop, expected);
  }
});

for (const tab of ["research", "papers", "manuscripts", "activity"]) {
  test(`${tab}: filter scrolling reveals primary and secondary selections once`, () => {
    const { context, state } = harness("Research");
    const primary = scrollContainer(500);
    const secondary = scrollContainer(80);
    const split = ["research", "manuscripts"].includes(tab);
    context.sidebar = split ? {
      dataset: {}, scrollTop: 0,
      querySelector: selector => selector.endsWith("-scroll") ? primary : secondary,
    } : primary;
    state.sidebarScroll = { [tab]: 100 };
    state.sidebarSecondaryScroll = { [tab]: 100 };
    state.keepSidebarSelectionVisible = true;
    context.restoreSidebarScroll(tab);
    assert.equal(primary.scrollTop, 340);
    assert.equal(state.sidebarScroll[tab], 340);
    if (split) {
      assert.equal(secondary.scrollTop, 80);
      assert.equal(state.sidebarSecondaryScroll[tab], 80);
    }
    assert.equal(state.keepSidebarSelectionVisible, false);
    // A subsequent background refresh preserves a user's scroll away from the selection.
    state.sidebarScroll[tab] = 100;
    context.restoreSidebarScroll(tab);
    assert.equal(primary.scrollTop, 100);
  });
}

test("sort controls also request selection visibility and preserve the detail scroll", () => {
  for (const view of ["Research", "Papers", "Manuscripts"]) {
    const { context } = harness(view);
    const control = view === "Manuscripts" ? context.manuscriptSortControl() : context.paperSortControl();
    const select = control.children[1];
    select.value = view === "Manuscripts" ? "latest" : "activity";
    select.handlers.change();
  }
});
