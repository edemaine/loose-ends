(function (global) {
  "use strict";

  const filterOptions = Object.freeze({
    attemptStatus: [
      ["all", "All open problems"],
      ["unattempted", "Unattempted"],
      ["unreviewed", "Awaiting review"],
      ["reviewed", "Reviewed"],
    ],
    triage: [
      ["all", "Any triage recommendation"],
      ["attempt", "Attempt (current)"],
      ["maybe", "Maybe (current)"],
      ["skip", "Skip (current)"],
      ["stale", "Stale triage"],
      ["missing", "No triage"],
    ],
    claim: [
      ["all", "Any claim type"],
      ["resolution", "Any resolution claim"],
      ["solution", "Solution"],
      ["counterexample", "Counterexample"],
      ["partial_result", "Partial result"],
      ["obstruction", "Obstruction"],
      ["none", "No result"],
    ],
    correctness: [
      ["all", "Any correctness"],
      ["credible", "Plausible or well supported"],
      ["no_major_error", "Minor gaps or better"],
      ["well_supported", "Well supported"],
      ["plausible", "Plausible"],
      ["minor_gaps", "Minor gaps"],
      ["major_gaps", "Major gaps"],
      ["incorrect", "Incorrect"],
      ["not_applicable", "Not applicable"],
      ["legacy", "Legacy review"],
    ],
    coverage: [
      ["all", "Any coverage"],
      ["complete_any", "Any complete coverage"],
      ["substantial", "Near complete or complete"],
      ["complete", "Complete"],
      ["complete_under_stated_interpretation", "Complete under stated interpretation"],
      ["near_complete", "Near complete"],
      ["partial", "Partial"],
      ["special_case", "Special case"],
      ["auxiliary", "Auxiliary"],
      ["none", "None"],
      ["legacy", "Legacy review"],
    ],
    importance: [
      ["all", "Any importance"],
      ["major_or_resolution", "Major or resolution"],
      ["resolution", "Resolution"],
      ["major", "Major"],
      ["moderate", "Moderate"],
      ["minor", "Minor"],
      ["none", "None"],
      ["legacy", "Legacy review"],
    ],
    confidence: [
      ["all", "Any confidence"],
      ["high", "High"],
      ["medium", "Medium"],
      ["low", "Low"],
      ["legacy", "Legacy review"],
    ],
    literature: [
      ["all", "Any literature status"],
      ["exclude-resolved", "Exclude known full resolutions"],
      ["resolved", "Known full resolution"],
      ["partially_resolved", "Partially resolved"],
      ["no_resolution_found", "No resolution found"],
      ["uncertain", "Uncertain"],
      ["missing", "No literature review"],
    ],
  });

  const filterParameters = Object.freeze({
    attemptStatus: "status",
    triage: "triage",
    claim: "claim",
    correctness: "correctness",
    coverage: "coverage",
    importance: "importance",
    confidence: "confidence",
    literature: "literature",
  });
  const priorityLevels = Object.freeze(["high", "medium", "low", "none"]);
  const freshnessLevels = Object.freeze(["current", "stale"]);
  const paperSortOptions = Object.freeze([
    ["alphabetical", "Alphabetical"],
    ["publication", "Publication date (newest)"],
    ["activity", "Latest activity"],
    ["results", "Most results (weighted)"],
    ["problems", "Most open problems"],
  ]);

  function humanize(value, fallback = "unknown") {
    return String(value || fallback).replaceAll("_", " ");
  }

  function titleize(value, fallback = "Unknown") {
    return humanize(value, fallback).replace(/\b\w/g, letter => letter.toUpperCase());
  }

  function statusLabel(value) {
    return value === "unreviewed" ? "attempted, awaiting review" : humanize(value);
  }

  function createDefaultFilters(initialPriorities = priorityLevels) {
    return {
      attemptStatus: "all",
      triage: "all",
      claim: "all",
      correctness: "all",
      coverage: "all",
      importance: "all",
      confidence: "all",
      literature: "all",
      priorities: new Set(initialPriorities),
      current: true,
      stale: true,
    };
  }

  function matches(item, filters) {
    if (filters.attemptStatus !== "all" && item.attemptStatus !== filters.attemptStatus) return false;
    const triage = filters.triage || "all";
    if (triage === "missing" && item.triageClassification) return false;
    if (triage === "stale" && (!item.triageClassification || item.triageCurrent)) return false;
    if (!["all", "missing", "stale"].includes(triage) && (
      item.triageClassification !== triage || !item.triageCurrent
    )) return false;
    if (item.attemptStatus === "reviewed") {
      if (!filters.priorities.has(item.priority)) return false;
      if (item.current && !filters.current) return false;
      if (!item.current && !filters.stale) return false;
    } else if ([filters.correctness, filters.coverage, filters.importance, filters.confidence].some(value => value !== "all")) {
      return false;
    }
    if (filters.claim === "resolution" && !["solution", "counterexample"].includes(item.claimedResultType)) return false;
    if (!["all", "resolution"].includes(filters.claim) && item.claimedResultType !== filters.claim) return false;
    if (filters.correctness === "credible" && !["plausible", "well_supported"].includes(item.correctness)) return false;
    if (filters.correctness === "no_major_error" && !["minor_gaps", "plausible", "well_supported"].includes(item.correctness)) return false;
    if (!["all", "credible", "no_major_error"].includes(filters.correctness) && item.correctness !== filters.correctness) return false;
    const completeCoverage = ["complete_under_stated_interpretation", "complete"];
    if (filters.coverage === "complete_any" && !completeCoverage.includes(item.reviewedCoverage)) return false;
    if (filters.coverage === "substantial" && !["near_complete", ...completeCoverage].includes(item.reviewedCoverage)) return false;
    if (!["all", "complete_any", "substantial"].includes(filters.coverage) && item.reviewedCoverage !== filters.coverage) return false;
    if (filters.importance === "major_or_resolution" && !["major", "resolution"].includes(item.importance)) return false;
    if (!["all", "major_or_resolution"].includes(filters.importance) && item.importance !== filters.importance) return false;
    if (filters.confidence !== "all" && item.verificationConfidence !== filters.confidence) return false;
    if (filters.literature === "exclude-resolved" && item.literatureStatus === "resolved") return false;
    if (filters.literature === "missing" && item.literatureStatus) return false;
    if (!["all", "exclude-resolved", "missing"].includes(filters.literature) && item.literatureStatus !== filters.literature) return false;
    return true;
  }

  function filterItems(items, filters, query = "") {
    const needle = query.trim().toLowerCase();
    return items.filter(item => {
      if (!matches(item, filters)) return false;
      if (!needle) return true;
      return [
        item.paperTitle, item.paperDirectory, item.paperAuthors,
        item.problemId, item.problemTitle, item.problemStatement,
        item.attemptName, item.criticSummary, item.solverSummary,
        item.attemptStatus, item.triageClassification, item.triageSummary,
        item.claimedResultType, item.correctness, item.reviewedCoverage,
        item.importance, item.verificationConfidence,
        item.literatureStatus, item.literatureSummary, item.legacyVerdict,
      ].join(" ").toLowerCase().includes(needle);
    });
  }

  function compareProblems(left, right) {
    return left.paperTitle.localeCompare(
      right.paperTitle,
      undefined,
      { sensitivity: "base", numeric: true },
    ) || left.paperDirectory.localeCompare(right.paperDirectory) ||
      left.problemId.localeCompare(right.problemId, undefined, { numeric: true });
  }

  function latestProblems(items) {
    const latest = new Map();
    items.forEach(item => {
      const previous = latest.get(item.problemKey);
      if (!previous || item.attemptNumber > previous.attemptNumber) latest.set(item.problemKey, item);
    });
    return [...latest.values()].sort(compareProblems);
  }

  function attemptsForProblem(items, problemKey) {
    return items.filter(item => item.problemKey === problemKey).sort((left, right) =>
      right.attemptNumber - left.attemptNumber ||
      String(right.itemKey || right.id).localeCompare(String(left.itemKey || left.id)),
    );
  }

  function normalizePaperSort(value) {
    return paperSortOptions.some(([key]) => key === value)
      ? value
      : "alphabetical";
  }

  function timestamp(value) {
    if (Number.isFinite(value)) return Number(value);
    const parsed = Date.parse(value || "");
    return Number.isFinite(parsed) ? parsed / 1000 : 0;
  }

  function paperTitleWithYear(title, published) {
    const paperTitle = String(title || "");
    const value = timestamp(published);
    if (!value) return paperTitle;
    const year = new Date(value * 1000).getUTCFullYear();
    return Number.isFinite(year) ? `${paperTitle} (${year})` : paperTitle;
  }

  function paperResultWeight(item) {
    if (item.attemptStatus === "reviewed" && item.correctness === "incorrect") return 0;
    if (["solution", "counterexample"].includes(item.claimedResultType)) return 1;
    if (["partial_result", "obstruction"].includes(item.claimedResultType)) return 0.1;
    return 0;
  }

  function paperMetrics(items) {
    const metrics = new Map();
    const bestResultByProblem = new Map();
    items.forEach(item => {
      const key = item.paperUrlKey || item.paperDirectory;
      if (!metrics.has(key)) {
        metrics.set(key, {
          activityTimestamp: 0,
          publicationTimestamp: 0,
          problemCount: 0,
          resultScore: 0,
        });
      }
      const metric = metrics.get(key);
      metric.activityTimestamp = Math.max(
        metric.activityTimestamp,
        timestamp(item.paperActivityTimestamp),
      );
      metric.publicationTimestamp = Math.max(
        metric.publicationTimestamp,
        timestamp(item.paperPublished),
      );
      metric.problemCount = Math.max(
        metric.problemCount,
        Number(item.paperProblemCount) || 0,
      );
      const resultKey = `${key}::${item.problemKey}`;
      const previousResult = bestResultByProblem.get(resultKey);
      bestResultByProblem.set(resultKey, {
        key,
        weight: Math.max(previousResult?.weight || 0, paperResultWeight(item)),
      });
    });
    bestResultByProblem.forEach(({ key, weight }) => {
      if (metrics.has(key)) metrics.get(key).resultScore += weight;
    });
    return metrics;
  }

  function comparePaperIdentity(left, right) {
    return String(left.paperTitle || left.title || "").localeCompare(
      String(right.paperTitle || right.title || ""),
      undefined,
      { sensitivity: "base", numeric: true },
    ) || String(left.paperDirectory || left.path || "").localeCompare(
      String(right.paperDirectory || right.path || ""),
    );
  }

  function comparePaperEntries(left, right, requestedSort) {
    const sort = normalizePaperSort(requestedSort);
    if (sort === "publication") {
      return right.publicationTimestamp - left.publicationTimestamp || comparePaperIdentity(left, right);
    }
    if (sort === "activity") {
      return right.activityTimestamp - left.activityTimestamp || comparePaperIdentity(left, right);
    }
    if (sort === "results") {
      return right.resultScore - left.resultScore || comparePaperIdentity(left, right);
    }
    if (sort === "problems") {
      return right.problemCount - left.problemCount || comparePaperIdentity(left, right);
    }
    return comparePaperIdentity(left, right);
  }

  function groupProblemsByPaper(items, sort = "alphabetical", referenceItems = items) {
    const groups = new Map();
    latestProblems(items).forEach(item => {
      const key = item.paperUrlKey || item.paperDirectory;
      if (!groups.has(key)) {
        groups.set(key, { key, paperTitle: item.paperTitle, paperDirectory: item.paperDirectory, problems: [] });
      }
      groups.get(key).problems.push(item);
    });
    const metrics = paperMetrics(referenceItems);
    return [...groups.values()].map(group => ({
      ...group,
      ...(metrics.get(group.key) || {
        activityTimestamp: 0,
        publicationTimestamp: 0,
        problemCount: group.problems.length,
        resultScore: 0,
      }),
    })).sort((left, right) => comparePaperEntries(left, right, sort));
  }

  function sortPapers(papers, sort = "alphabetical", referenceItems = []) {
    const metrics = paperMetrics(referenceItems);
    return papers.map(paper => {
      const key = paper.urlKey || paper.path;
      const metric = metrics.get(key) || {};
      return {
        ...paper,
        activityTimestamp: Math.max(
          timestamp(paper.activityTimestamp),
          metric.activityTimestamp || 0,
        ),
        publicationTimestamp: Math.max(
          timestamp(paper.published),
          metric.publicationTimestamp || 0,
        ),
        problemCount: Math.max(
          Number(paper.problemCount) || 0,
          metric.problemCount || 0,
        ),
        resultScore: metric.resultScore || 0,
      };
    }).sort((left, right) => comparePaperEntries(left, right, sort));
  }

  function attemptTags(item, { includeKnown = false } = {}) {
    const values = [];
    if (includeKnown && item.literatureStatus === "resolved") values.push(["known", "known"]);
    if (item.claimedResultType) values.push(["claim", item.claimedResultType]);
    if (item.attemptStatus === "reviewed") {
      values.push(
        ["correctness", item.correctness],
        ["coverage", item.reviewedCoverage],
        ["importance", item.importance],
      );
    } else {
      values.push(["status", statusLabel(item.attemptStatus)]);
    }
    return values.filter(([, value]) => value).map(([dimension, value]) => ({
      dimension,
      value,
      label: humanize(value),
      title: `${dimension}: ${humanize(value)}`,
      className: ["claim", "status", "known"].includes(dimension) ? dimension : "",
    }));
  }

  function detailBadges(item) {
    const values = [];
    if (item.attemptStatus === "reviewed") {
      values.push(
        ["priority", item.priority, item.priority],
        ["claim", item.claimedResultType, item.claimedResultType],
        ["correctness", item.correctness, item.correctness],
        ["coverage", item.reviewedCoverage, `coverage: ${humanize(item.reviewedCoverage)}`],
        ["importance", item.importance, `importance: ${humanize(item.importance)}`],
        ["confidence", item.verificationConfidence, `verification: ${humanize(item.verificationConfidence)}`],
      );
    } else {
      values.push(["status", item.attemptStatus, statusLabel(item.attemptStatus)]);
      if (item.claimedResultType) values.push(["claim", item.claimedResultType, item.claimedResultType]);
    }
    if (item.literatureStatus) {
      values.push(["literature", item.literatureStatus, `literature: ${humanize(item.literatureStatus)}`]);
    }
    if (item.attemptStatus === "reviewed" && !item.current) values.push(["warning", "stale", "stale review"]);
    if (item.reviewSchema === "legacy") values.push(["warning", "legacy", "legacy assessment"]);
    return values.filter(([, value]) => value).map(([dimension, value, label]) => ({ dimension, value, label }));
  }

  function summaryCards(item) {
    const cards = [];
    if (item.triageSummary) {
      cards.push({
        key: "triage",
        title: `Triage · ${item.triageClassification || "unclassified"}${item.triageCurrent ? "" : " · stale"}`,
        value: item.triageSummary,
      });
    }
    if (item.literatureSummary) {
      cards.push({
        key: "literature",
        title: `Literature · ${humanize(item.literatureStatus)}${item.literatureConfidence ? ` · ${humanize(item.literatureConfidence)} confidence` : ""}`,
        value: item.literatureSummary,
      });
    }
    if (item.attemptStatus !== "unattempted") {
      cards.push({
        key: "solver",
        title: `Solver claim · ${humanize(item.claimedResultType)}`,
        value: item.solverSummary,
        missing: "No solver summary.",
      });
    }
    if (item.attemptStatus === "reviewed") {
      cards.push({
        key: "critic",
        title: item.reviewSchema === "legacy"
          ? `Critic · legacy ${humanize(item.legacyVerdict)}`
          : `Critic · ${humanize(item.correctness)} · ${humanize(item.reviewedCoverage)}`,
        value: item.criticSummary,
        missing: "No critic summary.",
      });
    }
    return cards;
  }

  function detailTabs(item) {
    const tabs = [];
    if (item.attemptStatus !== "unattempted") tabs.push(["attempt", "Solution attempt"]);
    if (item.attemptStatus === "reviewed") tabs.push(["critique", "Critique"]);
    if (item.triageReport || item.hasTriageReport) tabs.push(["triage", "Triage"]);
    if (item.literatureReport || item.hasLiteratureReport) tabs.push(["literature", "Literature"]);
    tabs.push(["files", `Files (${item.fileCount ?? (item.files || []).length})`]);
    return tabs;
  }

  function availableFilters(items) {
    return {
      priorities: new Set(priorityLevels.filter(level => items.some(
        item => item.attemptStatus === "reviewed" && item.priority === level,
      ))),
      current: items.some(item => item.attemptStatus === "reviewed" && item.current),
      stale: items.some(item => item.attemptStatus === "reviewed" && !item.current),
    };
  }

  function queueSummary(items, filters) {
    const problemCount = new Set(items.map(item => item.problemKey)).size;
    const parts = [`${problemCount} problem${problemCount === 1 ? "" : "s"} shown`];
    const statuses = ["unattempted", "unreviewed", "reviewed"].map(status => {
      const count = new Set(items.filter(item => item.attemptStatus === status).map(item => item.problemKey)).size;
      return count ? `${count} ${statusLabel(status)}` : "";
    }).filter(Boolean);
    if (statuses.length) parts.push(statuses.join(" · "));
    const priorities = priorityLevels.map(level => {
      const count = items.filter(item => item.priority === level).length;
      return count ? `${count} ${level}` : "";
    }).filter(Boolean);
    if (priorities.length) parts.push(priorities.join(" · "));
    const staleCount = items.filter(item => item.attemptStatus === "reviewed" && !item.current).length;
    if (staleCount) parts.push(`${staleCount} stale`);
    const focusLabels = {
      resolution: "resolution claims", solution: "solution claims", counterexample: "counterexample claims",
      partial_result: "partial results", obstruction: "obstructions", none: "no result claim",
    };
    const literatureLabels = {
      "exclude-resolved": "excluding known full resolutions", resolved: "known full resolutions",
      partially_resolved: "partially resolved in literature", no_resolution_found: "no literature resolution found",
      uncertain: "uncertain literature status", missing: "no literature review",
    };
    const triageLabels = {
      attempt: "current triage: attempt", maybe: "current triage: maybe",
      skip: "current triage: skip", stale: "stale triage", missing: "no triage",
    };
    if (triageLabels[filters.triage]) parts.push(triageLabels[filters.triage]);
    if (focusLabels[filters.claim]) parts.push(focusLabels[filters.claim]);
    if (literatureLabels[filters.literature]) parts.push(literatureLabels[filters.literature]);
    return parts.join(" · ");
  }

  function filtersFromSearchParams(parameters, initialPriorities = priorityLevels) {
    const filters = createDefaultFilters(initialPriorities);
    Object.entries(filterParameters).forEach(([key, parameter]) => {
      const requested = parameters.get(parameter);
      const allowed = new Set(filterOptions[key].map(([value]) => value));
      if (requested && allowed.has(requested)) filters[key] = requested;
    });
    if (parameters.has("priority")) {
      filters.priorities = new Set(
        parameters.get("priority").split(",").filter(value => priorityLevels.includes(value)),
      );
    }
    if (parameters.has("freshness")) {
      const values = new Set(parameters.get("freshness").split(","));
      filters.current = values.has("current");
      filters.stale = values.has("stale");
    }
    return filters;
  }

  function filtersToSearchParams(parameters, filters, initialPriorities = priorityLevels) {
    const defaults = createDefaultFilters(initialPriorities);
    Object.entries(filterParameters).forEach(([key, parameter]) => {
      parameters.delete(parameter);
      if (filters[key] !== defaults[key]) parameters.set(parameter, filters[key]);
    });
    parameters.delete("priority");
    const selectedPriorities = priorityLevels.filter(level => filters.priorities.has(level));
    const defaultPriorities = priorityLevels.filter(level => defaults.priorities.has(level));
    if (selectedPriorities.join(",") !== defaultPriorities.join(",")) {
      parameters.set("priority", selectedPriorities.join(","));
    }
    parameters.delete("freshness");
    const selectedFreshness = freshnessLevels.filter(level => filters[level]);
    if (selectedFreshness.length !== freshnessLevels.length) {
      parameters.set("freshness", selectedFreshness.join(","));
    }
    return parameters;
  }

  function itemIdentity(item) {
    return {
      paper: item?.paperUrlKey || item?.paperDirectory || "",
      problem: item?.problemId || "",
      attempt: item?.attemptName || "",
    };
  }

  function findReviewItem(items, identity) {
    if (!identity?.paper || !identity.problem) return null;
    const candidates = items.filter(item =>
      (item.paperUrlKey === identity.paper || item.paperDirectory === identity.paper) &&
      item.problemId === identity.problem,
    );
    if (!candidates.length) return null;
    if (identity.attempt !== null && identity.attempt !== undefined) {
      const exact = candidates.find(item => (item.attemptName || "") === identity.attempt);
      if (exact) return exact;
    }
    return attemptsForProblem(candidates, candidates[0].problemKey)[0] || null;
  }

  function identityFromSearchParams(parameters) {
    if (!parameters.has("paper") || !parameters.has("problem")) return null;
    return {
      paper: parameters.get("paper") || "",
      problem: parameters.get("problem") || "",
      attempt: parameters.has("attempt") ? parameters.get("attempt") || "" : null,
    };
  }

  function identityToSearchParams(parameters, item) {
    ["paper", "problem", "attempt"].forEach(key => parameters.delete(key));
    if (!item) return parameters;
    const identity = itemIdentity(item);
    parameters.set("paper", identity.paper);
    parameters.set("problem", identity.problem);
    if (identity.attempt) parameters.set("attempt", identity.attempt);
    return parameters;
  }

  function createMarkdownRenderer(scope = global) {
    if (typeof scope.markdownit !== "function") return null;
    let renderer = scope.markdownit({ html: false, linkify: true, typographer: false });
    if (typeof scope.mdItPluginKatex?.katex === "function" && typeof scope.katex?.renderToString === "function") {
      renderer = renderer.use(scope.mdItPluginKatex.katex, {
        delimiters: "all", throwOnError: false, logger: () => "ignore",
      });
    }
    const defaultLinkOpen = renderer.renderer.rules.link_open ||
      ((tokens, index, options, env, self) => self.renderToken(tokens, index, options));
    renderer.renderer.rules.link_open = (tokens, index, options, env, self) => {
      tokens[index].attrSet("target", "_blank");
      tokens[index].attrSet("rel", "noopener noreferrer");
      return defaultLinkOpen(tokens, index, options, env, self);
    };
    return renderer;
  }

  global.LooseEndsReviewModel = Object.freeze({
    filterOptions,
    priorityLevels,
    freshnessLevels,
    paperSortOptions,
    humanize,
    titleize,
    statusLabel,
    createDefaultFilters,
    matches,
    filterItems,
    compareProblems,
    latestProblems,
    attemptsForProblem,
    normalizePaperSort,
    paperTitleWithYear,
    paperResultWeight,
    paperMetrics,
    groupProblemsByPaper,
    sortPapers,
    attemptTags,
    detailBadges,
    summaryCards,
    detailTabs,
    availableFilters,
    queueSummary,
    filtersFromSearchParams,
    filtersToSearchParams,
    itemIdentity,
    findReviewItem,
    identityFromSearchParams,
    identityToSearchParams,
    createMarkdownRenderer,
  });
})(globalThis);
