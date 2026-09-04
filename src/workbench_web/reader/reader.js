"use strict";

/*
 * Loose Ends paper reader.
 *
 * Renders a converted paper (document.html + document.json) with collapsible
 * sections and proofs, an outline, definition popovers, and mounted widgets.
 * Runs inside a sandboxed iframe; widget code is untrusted and limited to
 * the package it was generated with.
 */

(function () {
  const state = {
    manifest: null,
    doc: null,
    annotations: null,
    widgets: [],
    factories: new Map(),
    instances: new Map(),
    proofsVisible: false,
    labels: new Map(),
    stepControllers: new Map(),
    notes: [],
    noteHighlight: null,
  };
  const HAS_HIGHLIGHT_API = typeof CSS !== "undefined" && "highlights" in CSS && typeof Highlight !== "undefined";
  // One shared highlight for the active proof step; panels clear and refill it.
  const stepHighlight = HAS_HIGHLIGHT_API ? new Highlight() : null;
  if (stepHighlight) CSS.highlights.set("step-active", stepHighlight);
  const embedded = window.parent !== window;
  const content = document.getElementById("content");
  const outlineList = document.getElementById("outline-list");
  const popover = document.getElementById("popover");
  const readerRoot = document.getElementById("reader");
  const toolbarActions = document.getElementById("toolbar-actions");
  const toolbarStatus = document.getElementById("toolbar-status");
  const notice = document.getElementById("notice");

  // ------------------------------------------------------------------
  // Public widget API
  // ------------------------------------------------------------------

  window.LooseEnds = {
    registerWidget(id, factory) {
      if (typeof id !== "string" || !factory) return;
      state.factories.set(id, factory);
      const pending = state.instances.get(id);
      if (pending && pending.mount) pending.mount();
    },
  };

  // ------------------------------------------------------------------
  // Utilities
  // ------------------------------------------------------------------

  function node(tag, className = "", text = "") {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (text) element.textContent = text;
    return element;
  }

  function button(label, onClick, className = "mini") {
    const element = node("button", className, label);
    element.type = "button";
    element.addEventListener("click", onClick);
    return element;
  }

  async function fetchJson(path, optional = false) {
    try {
      const response = await fetch(path, { cache: "no-store" });
      if (!response.ok) {
        if (optional) return null;
        throw new Error(`${path}: HTTP ${response.status}`);
      }
      return await response.json();
    } catch (error) {
      if (optional) return null;
      throw error;
    }
  }

  function showNotice(message) {
    notice.textContent = message;
    notice.hidden = false;
  }

  function humanize(value) {
    return String(value || "").replace(/_/g, " ");
  }

  function verdictClass(value) {
    if (["well_supported", "works", "complete", "accurate"].includes(value)) return "success";
    if (["minor_gaps", "minor_issues"].includes(value)) return "warn";
    if (["major_gaps", "major_issues", "incorrect", "unusable", "misleading"].includes(value)) return "error";
    return "neutral";
  }

  function badge(text, kind) {
    return node("span", `badge ${kind}`, text);
  }

  // ------------------------------------------------------------------
  // Math
  // ------------------------------------------------------------------

  function macros() {
    return Object.assign({}, (state.doc && state.doc.macros) || {});
  }

  function renderLatex(latex, element, display) {
    if (!window.katex) {
      element.textContent = latex;
      element.classList.add("math-fallback");
      return;
    }
    try {
      window.katex.render(latex, element, {
        displayMode: Boolean(display),
        throwOnError: false,
        trust: false,
        strict: "ignore",
        macros: macros(),
      });
    } catch (error) {
      element.textContent = latex;
      element.classList.add("math-fallback");
      element.title = String(error && error.message || error);
    }
  }

  function typeset(root) {
    root.querySelectorAll(".math").forEach(element => {
      if (element.dataset.rendered) return;
      const latexSource = element.textContent;
      const display = element.dataset.display === "1";
      let latex = latexSource;
      const block = element.closest(".math-block");
      if (display && block && block.dataset.number) latex += `\\tag{${block.dataset.number}}`;
      element.dataset.latex = latexSource;
      element.dataset.rendered = "1";
      renderLatex(latex, element, display);
    });
  }

  /** Render text containing $...$ and $$...$$ spans into the element. */
  function renderRichText(text, element) {
    element.replaceChildren();
    text = String(text || "")
      .replace(/[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]/g, "")
      .replace(/\\\((.*?)\\\)/gs, (_, inner) => `$${inner}$`)
      .replace(/\\\[(.*?)\\\]/gs, (_, inner) => `$$${inner}$$`);
    const pattern = /\$\$([\s\S]+?)\$\$|\$([^$]+?)\$/g;
    let index = 0;
    let match;
    while ((match = pattern.exec(text)) !== null) {
      if (match.index > index) element.append(document.createTextNode(text.slice(index, match.index)));
      const span = node("span", "math");
      renderLatex(match[1] || match[2], span, Boolean(match[1]));
      element.append(span);
      index = pattern.lastIndex;
    }
    if (index < text.length) element.append(document.createTextNode(text.slice(index)));
  }

  // ------------------------------------------------------------------
  // Structure: collapsing, outline, navigation
  // ------------------------------------------------------------------

  function makeToggle(target, bodyClass) {
    const toggle = node("button", "toggle", "▾");
    toggle.type = "button";
    toggle.setAttribute("aria-label", "Toggle");
    toggle.addEventListener("click", event => {
      event.stopPropagation();
      target.classList.toggle("collapsed");
      toggle.setAttribute("aria-expanded", String(!target.classList.contains("collapsed")));
    });
    return toggle;
  }

  function makeCollapsible() {
    content.querySelectorAll("section.sec").forEach(section => {
      const heading = section.querySelector(":scope > .sec-title");
      if (heading) heading.prepend(makeToggle(section));
    });
    content.querySelectorAll(".proof").forEach(proof => {
      const head = proof.querySelector(":scope > .proof-head");
      if (head) head.prepend(makeToggle(proof));
      if (!state.proofsVisible) proof.classList.add("collapsed");
    });
  }

  function setAllSections(collapsed) {
    content.querySelectorAll("section.sec").forEach(section => section.classList.toggle("collapsed", collapsed));
  }

  function setAllProofs(visible) {
    state.proofsVisible = visible;
    content.querySelectorAll(".proof").forEach(proof => proof.classList.toggle("collapsed", !visible));
    document.getElementById("toggle-proofs").textContent = visible ? "Hide proofs" : "Show proofs";
  }

  /** Find `phrase` (whitespace- and case-insensitive) in the prose of `element`; returns a Range or null. */
  function findPhraseRange(element, phrase) {
    // Phrases are matched against prose only, so drop any $...$ formulas.
    const needle = String(phrase || "").replace(/\$\$[\s\S]*?\$\$|\$[^$]*\$/g, " ").replace(/\s+/g, " ").trim().toLowerCase();
    if (!needle || !element) return null;
    const walker = document.createTreeWalker(element, NodeFilter.SHOW_TEXT, {
      acceptNode(text) {
        return text.parentElement && text.parentElement.closest(".math, .env-actions, .proof-actions, button") ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT;
      },
    });
    let haystack = "";
    const map = [];
    let lastSpace = true;
    while (walker.nextNode()) {
      const node = walker.currentNode;
      const value = node.nodeValue;
      for (let index = 0; index < value.length; index += 1) {
        const char = value[index];
        if (/\s/.test(char)) {
          if (lastSpace) continue;
          haystack += " ";
          map.push([node, index]);
          lastSpace = true;
        } else {
          haystack += char.toLowerCase();
          map.push([node, index]);
          lastSpace = false;
        }
      }
    }
    // Exact match first; then the longest prefix of words that occurs, which
    // tolerates quotes copied from rendered math ("the ith corner").
    const words = needle.split(" ");
    const candidates = [needle];
    for (let count = words.length - 1; count >= 2; count -= 1) candidates.push(words.slice(0, count).join(" "));
    for (const candidate of candidates) {
      const start = haystack.indexOf(candidate);
      if (start < 0) continue;
      const end = start + candidate.length - 1;
      const range = document.createRange();
      range.setStart(map[start][0], map[start][1]);
      range.setEnd(map[end][0], map[end][1] + 1);
      return range;
    }
    return null;
  }

  function scrollToRectTop(top, smooth = true) {
    window.scrollTo({ top: top + window.scrollY - SCROLL_OFFSET, behavior: smooth ? "smooth" : "auto" });
  }

  const SCROLL_OFFSET = 60;      // where a revealed element's top lands
  const READING_LINE = 120;      // paragraphs above this line count as "read"

  function reveal(id, { flash = true, block = "start" } = {}) {
    const target = document.getElementById(id);
    if (!target) return false;
    let ancestor = target.parentElement;
    while (ancestor) {
      if (ancestor.classList && (ancestor.classList.contains("sec") || ancestor.classList.contains("proof"))) {
        ancestor.classList.remove("collapsed");
      }
      ancestor = ancestor.parentElement;
    }
    if (target.classList.contains("proof")) target.classList.remove("collapsed");
    if (target.classList.contains("sec")) target.classList.remove("collapsed");
    const top = target.getBoundingClientRect().top + window.scrollY - SCROLL_OFFSET;
    window.scrollTo({ top, behavior: "smooth" });
    if (flash) {
      target.classList.remove("flash");
      void target.offsetWidth;
      target.classList.add("flash");
    }
    return true;
  }

  function wireReferences() {
    content.addEventListener("click", event => {
      const link = event.target.closest("a[href^='#']");
      if (!link) return;
      const id = decodeURIComponent(link.getAttribute("href").slice(1));
      if (reveal(id)) event.preventDefault();
    });
  }

  function mainResultId() {
    if (state.annotations && state.annotations.main_result) return state.annotations.main_result;
    const statements = (state.doc && state.doc.statements) || [];
    const theorem = statements.find(item => item.kind === "theorem");
    return (theorem || statements[0] || {}).id || null;
  }

  function buildOutline() {
    outlineList.replaceChildren();
    const sections = (state.doc && state.doc.sections) || [];
    const statements = (state.doc && state.doc.statements) || [];
    const main = mainResultId();
    const widgetAnchors = new Set(state.widgets.map(widget => widget.anchor));
    const bySection = new Map();
    statements.forEach(statement => {
      const key = statement.section || "";
      if (!bySection.has(key)) bySection.set(key, []);
      bySection.get(key).push(statement);
    });
    const addStatement = (statement, level) => {
      const item = node("button", `outline-item statement level-${level}`);
      item.type = "button";
      if (statement.id === main) item.classList.add("main-result");
      if (widgetAnchors.has(statement.id)) item.classList.add("has-widget");
      item.append(node("span", "outline-label", statement.label));
      item.append(node("span", "outline-title", statement.title || ""));
      item.addEventListener("click", () => reveal(statement.id));
      outlineList.append(item);
    };
    const abstract = document.getElementById("abstract");
    if (abstract) {
      const item = node("button", "outline-item level-1");
      item.type = "button";
      item.append(node("span", "outline-num", ""), node("span", "outline-title", "Abstract"));
      item.addEventListener("click", () => reveal("abstract", { flash: false }));
      outlineList.append(item);
    }
    (bySection.get("") || []).forEach(statement => addStatement(statement, 1));
    sections.forEach(section => {
      const item = node("button", `outline-item level-${section.level}`);
      item.type = "button";
      item.append(node("span", "outline-num", section.number), node("span", "outline-title", section.title));
      item.addEventListener("click", () => reveal(section.id, { flash: false }));
      outlineList.append(item);
      (bySection.get(section.id) || []).forEach(statement => addStatement(statement, section.level));
    });
    if (document.getElementById("bibliography")) {
      const item = node("button", "outline-item level-1");
      item.type = "button";
      item.append(node("span", "outline-num", ""), node("span", "outline-title", "References"));
      item.addEventListener("click", () => reveal("bibliography", { flash: false }));
      outlineList.append(item);
    }
  }

  // ------------------------------------------------------------------
  // Visualize requests to the workbench
  // ------------------------------------------------------------------

  function requestVisualization(anchors, label) {
    if (!embedded) {
      showNotice("Open this reader inside the workbench to generate visualizations.");
      return;
    }
    window.parent.postMessage({ type: "loose-ends:visualize", anchors, label }, "*");
  }

  function addStatementActions() {
    const widgetAnchors = new Set(state.widgets.map(widget => widget.anchor));
    const main = mainResultId();
    content.querySelectorAll(".env").forEach(env => {
      const head = env.querySelector(":scope > .env-head");
      if (!head) return;
      const actions = node("span", "env-actions");
      if (env.id === main) {
        env.classList.add("main-result");
        actions.append(node("span", "tag", "Main result"));
      }
      const label = env.querySelector(".env-label")?.textContent || env.id;
      const existing = widgetAnchors.has(env.id);
      actions.append(button(existing ? "Visualize again" : "Visualize", () => requestVisualization([env.id], label)));
      head.append(actions);
    });
    content.querySelectorAll(".proof").forEach(proof => {
      const head = proof.querySelector(":scope > .proof-head");
      if (!head) return;
      const actions = node("span", "proof-actions");
      const existing = widgetAnchors.has(proof.id);
      const label = proof.querySelector(".proof-label")?.textContent || proof.id;
      actions.append(button(existing ? "Visualize proof again" : "Visualize proof", () => requestVisualization([proof.id], label)));
      head.append(actions);
    });
  }

  function buildToolbar() {
    document.getElementById("expand-all").addEventListener("click", () => { setAllSections(false); setAllProofs(true); });
    document.getElementById("collapse-all").addEventListener("click", () => { setAllSections(true); setAllProofs(false); });
    document.getElementById("toggle-proofs").addEventListener("click", () => setAllProofs(!state.proofsVisible));
    document.getElementById("outline-close").addEventListener("click", () => readerRoot.classList.add("outline-hidden"));
    document.getElementById("outline-open").addEventListener("click", () => readerRoot.classList.remove("outline-hidden"));
    if (window.innerWidth < 1000) readerRoot.classList.add("outline-hidden");
    const main = mainResultId();
    if (main) {
      toolbarActions.append(button("Main result", () => reveal(main), "tool"));
    }
    if (!state.annotations) {
      toolbarActions.append(button(embedded ? "Visualize" : "Visualize (workbench only)", () => requestVisualization(["default"], "default"), "tool primary"));
    } else {
      const widgetCount = state.widgets.length;
      const glossaryCount = (state.annotations.glossary || []).length;
      toolbarStatus.textContent = `${glossaryCount} definitions · ${widgetCount} widget${widgetCount === 1 ? "" : "s"}`;
    }
  }

  // ------------------------------------------------------------------
  // Glossary popovers
  // ------------------------------------------------------------------

  function escapeRegExp(value) {
    return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  }

  /** Expand parameterless paper macros, then strip whitespace, for comparisons. */
  function normalizeLatex(value) {
    let text = String(value || "");
    const table = macros();
    const names = Object.keys(table).filter(name => !/#/.test(table[name])).sort((a, b) => b.length - a.length);
    for (let round = 0; round < 3; round += 1) {
      let changed = false;
      names.forEach(name => {
        const pattern = new RegExp(escapeRegExp(name) + "(?![a-zA-Z])", "g");
        if (pattern.test(text)) {
          text = text.replace(pattern, table[name] + " ");
          changed = true;
        }
      });
      if (!changed) break;
    }
    return text.replace(/\s+/g, "");
  }

  function applyGlossary() {
    const glossary = (state.annotations && state.annotations.glossary) || [];
    if (!glossary.length) {
      state.glossary = new Map();
      wirePopover();
      return;
    }
    const entries = glossary.map((entry, index) => ({
      ...entry,
      key: entry.id || `term-${index}`,
      forms: [...new Set([entry.term, ...(entry.forms || [])].filter(Boolean))].sort((a, b) => b.length - a.length),
      latexForms: (entry.latex_forms || []).map(normalizeLatex).filter(Boolean),
    }));
    const byKey = new Map(entries.map(entry => [entry.key, entry]));
    state.glossary = byKey;
    const skipSelector = ".math, .env-head, .proof-head, a, .popover, .widget-card, .sec-title, .paper-header, .bibliography, code, .term";
    const allForms = entries.flatMap(entry => entry.forms.map(form => ({ form, key: entry.key, anchor: entry.anchor })));
    if (allForms.length) {
      allForms.sort((a, b) => b.form.length - a.form.length);
      const pattern = new RegExp(`(^|[^\\p{L}\\p{N}])(${allForms.map(item => escapeRegExp(item.form)).join("|")})(?![\\p{L}\\p{N}])`, "giu");
      const formLookup = new Map(allForms.map(item => [item.form.toLowerCase(), item]));
      const walker = document.createTreeWalker(content, NodeFilter.SHOW_TEXT, {
        acceptNode(text) {
          if (!text.nodeValue.trim()) return NodeFilter.FILTER_REJECT;
          const parent = text.parentElement;
          if (!parent || parent.closest(skipSelector)) return NodeFilter.FILTER_REJECT;
          return NodeFilter.FILTER_ACCEPT;
        },
      });
      const textNodes = [];
      while (walker.nextNode()) textNodes.push(walker.currentNode);
      // Decorate only the first occurrence of each term within a paragraph.
      const decorated = new Set();
      textNodes.forEach(text => {
        const value = text.nodeValue;
        pattern.lastIndex = 0;
        if (!pattern.test(value)) return;
        pattern.lastIndex = 0;
        const fragment = document.createDocumentFragment();
        let index = 0;
        let match;
        while ((match = pattern.exec(value)) !== null) {
          const start = match.index + match[1].length;
          const item = formLookup.get(match[2].toLowerCase());
          if (!item) continue;
          const container = text.parentElement.closest("[id]");
          // Do not decorate a term inside the element that defines it.
          if (item.anchor && container && (container.id === item.anchor || text.parentElement.closest(`#${CSS.escape(item.anchor)}`))) continue;
          const paragraph = text.parentElement.closest(".par, li, figcaption, td, th");
          const seenKey = `${paragraph ? paragraph.id || paragraph.textContent.length : "x"}::${item.key}`;
          if (decorated.has(seenKey)) continue;
          decorated.add(seenKey);
          fragment.append(document.createTextNode(value.slice(index, start)));
          const span = node("span", "term", match[2]);
          span.dataset.term = item.key;
          span.tabIndex = 0;
          fragment.append(span);
          index = start + match[2].length;
        }
        if (index === 0) return;
        fragment.append(document.createTextNode(value.slice(index)));
        text.replaceWith(fragment);
      });
    }
    const latexEntries = entries.filter(entry => entry.latexForms.length);
    if (latexEntries.length) {
      content.querySelectorAll(".math[data-display='0']").forEach(element => {
        if (element.closest(".widget-card, .env-head, .popover")) return;
        const latex = normalizeLatex(element.dataset.latex || element.textContent);
        const entry = latexEntries.find(item => item.latexForms.some(form => latex === form || (form.length >= 6 && latex.includes(form))));
        if (!entry) return;
        if (entry.anchor && element.closest(`#${CSS.escape(entry.anchor)}`)) return;
        element.classList.add("term", "math-term");
        element.dataset.term = entry.key;
        element.tabIndex = 0;
      });
    }
    wirePopover();
  }

  /** Mark phrases that have an inline explanation with a small bubble marker. */
  function attachExplanation(entry, index = 0) {
    if (!state.glossary) state.glossary = new Map();
    const key = `explain-${entry.id || index}`;
    const existing = content.querySelector(`.explain-mark[data-term="${CSS.escape(key)}"]`);
    if (existing) existing.remove();
    const quick = entry.provenance === "quick";
    state.glossary.set(key, {
      term: entry.title || "Why?",
      kind: quick ? "quick answer, unreviewed" : "explanation",
      gloss: entry.text,
      anchor: null,
      explanation: entry,
    });
    const marker = node("button", `explain-mark${quick ? " quick" : ""}`, "?");
    marker.type = "button";
    marker.dataset.term = key;
    marker.classList.add("term");
    marker.title = quick ? "Quick answer (unreviewed)" : "Explanation";
    const placed = placeMarker(marker, entry.anchor, { latex: entry.latex, phrase: entry.phrase });
    if (!placed) return null;
    if (placed.range && HAS_HIGHLIGHT_API) {
      if (!state.explainHighlight) {
        state.explainHighlight = new Highlight();
        CSS.highlights.set("explained", state.explainHighlight);
      }
      state.explainHighlight.add(placed.range);
    }
    if (placed.math) placed.math.classList.add("explained-math");
    return marker;
  }

  /** Replace every explanation bubble with the given list. */
  function resyncExplanations(list) {
    content.querySelectorAll(".explain-mark:not(.pending)").forEach(element => element.remove());
    content.querySelectorAll(".explained-math").forEach(element => element.classList.remove("explained-math"));
    if (state.explainHighlight) state.explainHighlight.clear();
    if (!state.annotations) state.annotations = { glossary: [], proof_outlines: {}, explanations: [] };
    state.annotations.explanations = Array.isArray(list) ? list : [];
    state.annotations.explanations.forEach((entry, index) => attachExplanation(entry, index));
  }

  function applyExplanations() {
    const explanations = (state.annotations && state.annotations.explanations) || [];
    explanations.forEach((entry, index) => attachExplanation(entry, index));
  }

  function wirePopover() {
    let hideTimer = null;
    let pinned = false;
    let currentTerm = null;

    function hide(force = false) {
      if (pinned && !force) return;
      pinned = false;
      popover.hidden = true;
      currentTerm = null;
    }

    function show(termElement) {
      const entry = state.glossary && state.glossary.get(termElement.dataset.term);
      if (!entry) return;
      if (currentTerm === termElement && !popover.hidden) return;
      currentTerm = termElement;
      popover.replaceChildren();
      const head = node("div", "popover-term");
      renderRichText(entry.term, head);
      if (entry.kind) head.append(node("span", "popover-kind", entry.kind));
      popover.append(head);
      const gloss = node("div", "popover-gloss");
      renderRichText(entry.gloss || "", gloss);
      popover.append(gloss);
      if (entry.anchor && document.getElementById(entry.anchor)) {
        const jump = node("a", "popover-jump", "Go to definition ↓");
        jump.href = `#${entry.anchor}`;
        jump.addEventListener("click", event => {
          event.preventDefault();
          hide(true);
          reveal(entry.anchor);
        });
        popover.append(jump);
      } else if (entry.kind === "background") {
        popover.append(node("div", "popover-source", "Not defined in the paper."));
      }
      if (entry.source) popover.append(node("div", "popover-source", entry.source));
      if (entry.explanation) {
        const revise = button("Unclear?", () => {
          hide(true);
          openReviseForm(entry.explanation, termElement);
        }, "mini");
        const actions = node("div", "popover-actions");
        actions.append(revise);
        popover.append(actions);
      }
      popover.hidden = false;
      const rect = termElement.getBoundingClientRect();
      const width = popover.offsetWidth;
      let left = rect.left + window.scrollX;
      if (left + width > window.scrollX + window.innerWidth - 12) left = Math.max(12, window.scrollX + window.innerWidth - width - 12);
      let top = rect.bottom + window.scrollY + 6;
      if (rect.bottom + popover.offsetHeight + 12 > window.innerHeight) top = rect.top + window.scrollY - popover.offsetHeight - 6;
      popover.style.left = `${left}px`;
      popover.style.top = `${top}px`;
    }

    content.addEventListener("mouseover", event => {
      const term = event.target.closest(".term");
      if (!term) return;
      clearTimeout(hideTimer);
      show(term);
    });
    content.addEventListener("mouseout", event => {
      const term = event.target.closest(".term");
      if (!term) return;
      clearTimeout(hideTimer);
      hideTimer = setTimeout(() => hide(), 250);
    });
    content.addEventListener("click", event => {
      const term = event.target.closest(".term");
      if (!term) {
        if (!popover.contains(event.target)) hide(true);
        return;
      }
      clearTimeout(hideTimer);
      show(term);
      pinned = true;
    });
    content.addEventListener("focusin", event => {
      const term = event.target.closest(".term");
      if (term) show(term);
    });
    popover.addEventListener("mouseenter", () => clearTimeout(hideTimer));
    popover.addEventListener("mouseleave", () => { hideTimer = setTimeout(() => hide(), 250); });
    document.addEventListener("keydown", event => { if (event.key === "Escape") hide(true); });
    window.addEventListener("scroll", () => { if (!pinned) hide(); }, { passive: true });
  }

  // ------------------------------------------------------------------
  // Widgets and proof panels
  // ------------------------------------------------------------------

  function anchorRecord(anchor) {
    const doc = state.doc || {};
    return (doc.statements || []).find(item => item.id === anchor)
      || (doc.proofs || []).find(item => item.id === anchor)
      || (doc.figures || []).find(item => item.id === anchor)
      || (doc.paragraphs || []).find(item => item.id === anchor)
      || null;
  }

  function widgetCard(widget) {
    const card = node("section", "widget-card");
    card.dataset.widget = widget.id;
    const head = node("div", "widget-head");
    head.append(node("strong", "widget-title", widget.title || "Visualization"));
    const meta = node("div", "widget-meta");
    const review = widget.review || null;
    if (review && review.fidelity && review.fidelity !== "unreviewed") {
      meta.append(badge(humanize(review.fidelity), verdictClass(review.fidelity)));
      if (review.interaction_quality) meta.append(badge(humanize(review.interaction_quality), verdictClass(review.interaction_quality)));
    } else {
      meta.append(badge("unreviewed", "neutral"));
    }
    meta.append(node("span", "spacer"));
    const improve = button("Improve…", () => openImproveForm(widget, card));
    improve.title = "Report something off in this visualization as a whole";
    meta.append(improve);
    meta.append(button("Details", () => card.classList.toggle("details-open")));
    head.append(meta);
    card.append(head);
    let exampleSelect = null;
    if (Array.isArray(widget.examples) && widget.examples.length) {
      const bar = node("div", "widget-examples");
      const label = node("label");
      label.append(node("span", "", "Running example"));
      exampleSelect = node("select");
      widget.examples.forEach(example => {
        const option = node("option", "", example.label || example.id);
        option.value = example.id;
        if (example.note) option.title = example.note;
        exampleSelect.append(option);
      });
      label.append(exampleSelect);
      bar.append(label);
      const note = node("span", "widget-example-note");
      const updateNote = () => { const chosen = widget.examples.find(item => item.id === exampleSelect.value); note.textContent = chosen && chosen.note ? chosen.note : ""; };
      updateNote();
      exampleSelect.addEventListener("change", updateNote);
      bar.append(note);
      card.append(bar);
    }
    const body = node("div", "widget-body");
    card.append(body);
    const details = node("div", "widget-details");
    if (widget.summary) details.append(node("p", "", widget.summary));
    if (review && review.summary) {
      details.append(node("h4", "", "Independent review"));
      details.append(node("p", "", review.summary));
    }
    const lists = [
      ["Limitations", widget.limitations],
      ["Review findings", review && review.findings],
      ["Blocking gaps", review && review.blocking_gaps],
    ];
    lists.forEach(([title, items]) => {
      if (!items || !items.length) return;
      details.append(node("h4", "", title));
      const list = node("ul");
      items.forEach(item => list.append(node("li", "", item)));
      details.append(list);
    });
    if (widget.generated_at) details.append(node("p", "", `Generated ${widget.generated_at}${widget.model ? ` · ${widget.model}` : ""}`));
    card.append(details);
    return { card, body, exampleSelect };
  }

  /** Map a pointer event to the SVG's viewBox coordinates, independent of CSS scaling. */
  function svgPoint(svg, event) {
    const matrix = svg.getScreenCTM && svg.getScreenCTM();
    if (!matrix) return null;
    const point = new DOMPoint(event.clientX, event.clientY).matrixTransform(matrix.inverse());
    return { x: point.x, y: point.y };
  }

  function widgetApi(widget, body, controller) {
    const base = `widgets/${encodeURIComponent(widget.id)}/`;
    return {
      id: widget.id,
      anchor: anchorRecord(widget.anchor),
      anchorId: widget.anchor,
      document: state.doc,
      widget,
      steps: widget.steps || [],
      container: body,
      katex: { render: (latex, element, display = false) => renderLatex(latex, element, display) },
      svgPoint: (svg, event) => svgPoint(svg, event),
      renderText: (text, element) => renderRichText(text, element),
      typeset: element => typeset(element),
      assetUrl: name => base + String(name).split("/").map(encodeURIComponent).join("/"),
      goTo: id => reveal(id),
      requestStep: index => { if (controller) controller.setStep(index, { fromWidget: true }); },
      macros: macros(),
    };
  }

  function mountWidget(widget, body, controller) {
    const factory = state.factories.get(widget.id);
    if (!factory) return null;
    try {
      const api = widgetApi(widget, body, controller);
      const instance = typeof factory === "function" ? factory(body, api) : factory.mount(body, api);
      return instance || {};
    } catch (error) {
      body.replaceChildren(node("div", "widget-error", `Widget failed to start: ${error && error.message || error}`));
      return null;
    }
  }

  function loadWidgetScript(widget) {
    return new Promise(resolve => {
      const script = document.createElement("script");
      script.src = `widgets/${encodeURIComponent(widget.id)}/${widget.entry || "widget.js"}`;
      script.async = false;
      script.addEventListener("load", () => resolve(true));
      script.addEventListener("error", () => resolve(false));
      document.head.append(script);
    });
  }

  function stepController(proof, steps, body) {
    const paragraphs = Array.from(proof.querySelectorAll(":scope > .proof-body .par"));
    const list = node("ol", "steps");
    let active = -1;
    let instance = null;
    // Each step targets either a phrase range inside one of its paragraphs or its first paragraph.
    const targets = steps.map(step => {
      const elements = (step.paragraphs || []).map(id => document.getElementById(id)).filter(Boolean);
      let range = null;
      if (step.phrase) {
        for (const element of elements) {
          range = findPhraseRange(element, step.phrase);
          if (range) break;
        }
      }
      return { elements, range };
    });
    const targetTop = index => {
      const target = targets[index];
      if (!target) return Infinity;
      if (target.range) return target.range.getBoundingClientRect().top;
      return target.elements[0] ? target.elements[0].getBoundingClientRect().top : Infinity;
    };
    const items = steps.map((step, index) => {
      const item = node("li", "step");
      item.append(node("span", "step-index", String(index + 1)));
      const text = node("span", "step-text");
      const title = node("span", "step-title");
      renderRichText(step.title || `Step ${index + 1}`, title);
      text.append(title);
      if (step.note || step.summary) {
        const note = node("span", "step-note");
        renderRichText(step.note || step.summary, note);
        text.append(note);
      }
      item.append(text);
      item.addEventListener("click", () => controller.setStep(index, { scroll: true }));
      list.append(item);
      return item;
    });
    let lockUntil = 0;
    // Keep the sticky panel on screen while the last steps reach the reading
    // line: pad the text column so the proof does not end above the panel.
    const ensureRunway = () => {
      const body = proof.querySelector(":scope > .proof-body");
      const text = body && body.querySelector(":scope > .proof-text");
      const panel = body && body.querySelector(":scope > .proof-panel");
      if (!text || !panel || !steps.length) return;
      const last = targets[steps.length - 1];
      const lastTop = last.range ? last.range.getBoundingClientRect().top : (last.elements[0] ? last.elements[0].getBoundingClientRect().top : null);
      if (lastTop === null) return;
      text.style.paddingBottom = "0px";
      const remaining = text.getBoundingClientRect().bottom - lastTop;
      const needed = panel.offsetHeight + 52 - READING_LINE;
      text.style.paddingBottom = `${Math.max(0, Math.ceil(needed - remaining))}px`;
    };
    window.addEventListener("resize", ensureRunway);
    const controller = {
      setStep(index, { scroll = false, fromWidget = false } = {}) {
        if (index < 0 || index >= steps.length) return;
        active = index;
        items.forEach((item, position) => item.classList.toggle("active", position === index));
        if (controller.onChange) controller.onChange(index);
        const target = targets[index];
        const usePhrase = Boolean(target.range && stepHighlight);
        const ids = new Set(usePhrase ? [] : (steps[index].paragraphs || []));
        paragraphs.forEach(paragraph => paragraph.classList.toggle("step-active", ids.has(paragraph.id)));
        if (stepHighlight) {
          stepHighlight.clear();
          if (usePhrase) stepHighlight.add(target.range);
        }
        if (scroll) {
          const first = target.elements[0];
          if (first) {
            // Ignore scroll-following while the smooth scroll is in flight.
            lockUntil = Date.now() + 1200;
            let ancestor = first.parentElement;
            while (ancestor) {
              if (ancestor.classList && (ancestor.classList.contains("sec") || ancestor.classList.contains("proof"))) ancestor.classList.remove("collapsed");
              ancestor = ancestor.parentElement;
            }
            scrollToRectTop(target.range ? target.range.getBoundingClientRect().top : first.getBoundingClientRect().top);
          }
        }
        if (instance && instance.setStep && !fromWidget) {
          try { instance.setStep(index, steps[index]); } catch (error) { console.error(error); }
        }
      },
      attach(value) { instance = value; requestAnimationFrame(ensureRunway); },
      get active() { return active; },
      get instance() { return instance; },
      refresh() { if (active >= 0) controller.setStep(active); requestAnimationFrame(ensureRunway); },
      ensureRunway,
    };
    if (steps.length) {
      // Current step stays adjacent to the picture; the full list scrolls.
      const current = node("div", "step-current");
      const nav = node("div", "step-nav");
      const counter = node("span", "step-counter");
      const previous = button("◀", () => controller.setStep(Math.max(0, active - 1), { scroll: true }));
      const next = button("▶", () => controller.setStep(Math.min(steps.length - 1, active + 1), { scroll: true }));
      const expand = button("All steps", () => {
        list.classList.toggle("expanded");
        expand.textContent = list.classList.contains("expanded") ? "Fewer" : "All steps";
      });
      nav.append(previous, counter, next, node("span", "spacer"), expand);
      const currentText = node("div", "step-current-text");
      body.append(current, nav, list);
      current.append(currentText);
      const stepImprove = button("Improve…", () => {
        const card = proof.querySelector(".proof-panel .widget-card");
        const widget = card && card.dataset.widget ? state.widgets.find(item => item.id === card.dataset.widget) : null;
        if (widget) openImproveForm(widget, card, { step: active, stepTitle: steps[active] && steps[active].title });
      }, "mini step-improve");
      stepImprove.title = "Report something off in this step's picture";
      current.append(stepImprove);
      controller.stepImprove = stepImprove;
      list.classList.add("compact");
      controller.onChange = index => {
        counter.textContent = `Step ${index + 1} of ${steps.length}`;
        currentText.replaceChildren();
        const title = node("div", "step-title");
        renderRichText(steps[index].title || `Step ${index + 1}`, title);
        currentText.append(title);
        if (steps[index].note || steps[index].summary) {
          const note = node("div", "step-note");
          renderRichText(steps[index].note || steps[index].summary, note);
          currentText.append(note);
        }
        previous.disabled = index === 0;
        next.disabled = index === steps.length - 1;
        const item = items[index];
        if (item && !list.classList.contains("expanded")) {
          const offset = item.getBoundingClientRect().top - list.getBoundingClientRect().top + list.scrollTop;
          list.scrollTop = Math.max(0, offset - list.clientHeight / 2 + item.offsetHeight / 2);
        }
      };
    }
    // Follow the reader's scroll position through the proof: the current
    // step is the one containing the last paragraph whose top has passed
    // the reading line. A step revealed by a click lands at SCROLL_OFFSET,
    // above the line, so the highlight agrees with the click.
    if (steps.length) {
      const lookup = new Map();
      steps.forEach((step, index) => (step.paragraphs || []).forEach(id => lookup.set(id, index)));
      let scheduled = false;
      const sync = () => {
        scheduled = false;
        if (Date.now() < lockUntil) return;
        if (!proof.isConnected || proof.classList.contains("collapsed")) return;
        const proofRect = proof.getBoundingClientRect();
        if (proofRect.bottom < 0 || proofRect.top > window.innerHeight) return;
        let index = 0;
        for (let position = 0; position < steps.length; position += 1) {
          if (targetTop(position) <= READING_LINE) index = position;
          else break;
        }
        if (index !== active) controller.setStep(index);
      };
      window.addEventListener("scroll", () => {
        if (scheduled) return;
        scheduled = true;
        requestAnimationFrame(sync);
      }, { passive: true });
    }
    return controller;
  }

  function proofPanel(proof) {
    let panel = proof.querySelector(":scope > .proof-body > .proof-panel");
    if (panel) return panel;
    const body = proof.querySelector(":scope > .proof-body");
    const text = node("div", "proof-text");
    while (body.firstChild) text.append(body.firstChild);
    panel = node("aside", "proof-panel");
    body.append(text, panel);
    proof.classList.add("with-panel");
    return panel;
  }

  function outlineSteps(proofId) {
    const outlines = (state.annotations && state.annotations.proof_outlines) || {};
    const outline = outlines[proofId];
    return Array.isArray(outline) ? outline : (outline && outline.steps) || [];
  }

  async function mountWidgets() {
    const handledProofs = new Set();
    for (const widget of state.widgets) {
      const target = document.getElementById(widget.anchor);
      if (!target) {
        showNotice(`Widget "${widget.title || widget.id}" targets a missing anchor ${widget.anchor}.`);
        continue;
      }
      const { card, body, exampleSelect } = widgetCard(widget);
      let controller = null;
      if (target.classList.contains("proof")) {
        handledProofs.add(target.id);
        const panel = proofPanel(target);
        const steps = (widget.steps && widget.steps.length) ? widget.steps : outlineSteps(target.id);
        const stepsHost = node("div");
        controller = stepController(target, steps, stepsHost);
        card.append(stepsHost);
        panel.append(card);
        if (!state.proofsVisible) target.classList.remove("collapsed");
      } else {
        // Statement widgets sit beside the statement on wide screens and
        // below it otherwise (see reader.css .statement-row).
        const row = node("div", "statement-row");
        target.replaceWith(row);
        row.append(target, card);
      }
      const loaded = await loadWidgetScript(widget);
      if (!loaded) {
        body.replaceChildren(node("div", "widget-error", "Widget script could not be loaded."));
        continue;
      }
      if (controller) state.stepControllers.set(widget.id, controller);
      const mount = () => {
        const instance = mountWidget(widget, body, controller);
        state.instances.set(widget.id, { mount: () => {}, instance });
        if (controller && instance) {
          controller.attach(instance);
          controller.setStep(0);
        }
        if (exampleSelect && instance) {
          exampleSelect.addEventListener("change", () => {
            try { if (instance.setExample) instance.setExample(exampleSelect.value); } catch (error) { console.error(error); }
            if (controller) controller.refresh();
          });
        }
      };
      if (state.factories.has(widget.id)) mount();
      else {
        state.instances.set(widget.id, { mount });
        setTimeout(() => {
          if (!state.factories.has(widget.id)) body.replaceChildren(node("div", "widget-error", `Widget did not register itself as "${widget.id}".`));
        }, 3000);
      }
    }
    // Proof outlines from annotations, for proofs without a widget.
    const outlines = (state.annotations && state.annotations.proof_outlines) || {};
    Object.keys(outlines).forEach(proofId => {
      if (handledProofs.has(proofId)) return;
      const proof = document.getElementById(proofId);
      if (!proof || !proof.classList.contains("proof")) return;
      const steps = outlineSteps(proofId);
      if (!steps.length) return;
      const panel = proofPanel(proof);
      const card = node("section", "widget-card");
      card.append(node("div", "panel-title", "Proof outline"));
      const host = node("div");
      const controller = stepController(proof, steps, host);
      card.append(host);
      panel.append(card);
      controller.setStep(0);
      requestAnimationFrame(controller.ensureRunway);
    });
  }

  // ------------------------------------------------------------------
  // Reader notes ("I don't get this")
  // ------------------------------------------------------------------

  const noteButton = node("button", "note-button", "I don't get this");
  noteButton.type = "button";
  noteButton.hidden = true;
  document.body.append(noteButton);
  const noteForm = node("div", "note-form");
  noteForm.hidden = true;
  document.body.append(noteForm);
  const notesPanel = node("div", "notes-panel");
  notesPanel.hidden = true;
  document.body.append(notesPanel);
  let notesButton = null;
  let pendingSelection = null;

  function selectionAnchor(range) {
    let element = range.startContainer.nodeType === Node.TEXT_NODE ? range.startContainer.parentElement : range.startContainer;
    if (!element || !content.contains(element)) return null;
    const paragraph = element.closest(".par[id], figcaption, li");
    if (paragraph && paragraph.id) return paragraph.id;
    const holder = element.closest("[id]");
    return holder && content.contains(holder) ? holder.id : null;
  }

  function hideNoteUi() {
    noteButton.hidden = true;
    noteForm.hidden = true;
    pendingSelection = null;
  }

  function onSelectionChange() {
    if (!noteForm.hidden) return;
    const selection = window.getSelection();
    if (!selection || selection.isCollapsed || !selection.rangeCount) { noteButton.hidden = true; return; }
    const range = selection.getRangeAt(0);
    const quote = selection.toString().replace(/\s+/g, " ").trim();
    const anchor = selectionAnchor(range);
    if (!anchor || quote.length < 1 || quote.length > 1500) { noteButton.hidden = true; return; }
    const startElement = range.startContainer.nodeType === Node.TEXT_NODE ? range.startContainer.parentElement : range.startContainer;
    const mathElement = startElement && startElement.closest(".math");
    const latex = mathElement ? (mathElement.dataset.latex || "") : "";
    if (!latex && quote.length < 3) { noteButton.hidden = true; return; }
    pendingSelection = { anchor, quote, latex };
    const rect = range.getBoundingClientRect();
    noteButton.style.left = `${Math.min(rect.right + window.scrollX + 6, window.scrollX + window.innerWidth - 150)}px`;
    noteButton.style.top = `${rect.bottom + window.scrollY + 6}px`;
    noteButton.hidden = false;
  }

  function openNoteForm() {
    if (!pendingSelection) return;
    showNoteForm(pendingSelection, { left: noteButton.style.left, top: noteButton.style.top });
  }

  /** Reopen the form on an existing explanation so the reader can ask again. */
  function openReviseForm(explanation, marker) {
    const rect = marker.getBoundingClientRect();
    showNoteForm({
      anchor: explanation.anchor,
      quote: explanation.phrase || explanation.title || "",
      latex: explanation.latex || "",
      revises: explanation.id,
      previousTitle: explanation.title || "",
    }, { left: `${Math.min(rect.left + window.scrollX, window.scrollX + window.innerWidth - 340)}px`, top: `${rect.bottom + window.scrollY + 6}px` });
  }

  function showNoteForm(selection, position) {
    const { anchor, quote, latex, revises } = selection;
    noteForm.replaceChildren();
    if (revises || selection.follows) {
      noteForm.append(node("div", "note-form-title", `Ask again about “${selection.previousTitle || "the explanation"}”`));
    } else {
      const quoteNode = node("div", "note-form-quote");
      if (latex) renderLatex(latex, quoteNode, false);
      else quoteNode.textContent = `“${quote.length > 160 ? quote.slice(0, 157) + "…" : quote}”`;
      noteForm.append(quoteNode);
    }
    const textarea = node("textarea");
    textarea.placeholder = revises ? "What is still unclear, or what should change?" : "What is unclear? (optional)";
    textarea.rows = 3;
    noteForm.append(textarea);
    const actions = node("div", "note-form-actions");
    const noteFor = () => ({ anchor, quote, latex, revises: revises || "", follows: selection.follows || "", message: textarea.value.trim() });
    actions.append(
      button("Answer now", () => { askNow(noteFor()); hideNoteUi(); window.getSelection()?.removeAllRanges(); }, "mini accent"),
      button("Add to list", () => { addNote(noteFor()); hideNoteUi(); window.getSelection()?.removeAllRanges(); }, "mini"),
      button("Cancel", () => hideNoteUi()),
    );
    noteForm.append(actions);
    noteForm.style.left = position.left;
    noteForm.style.top = position.top;
    noteButton.hidden = true;
    noteForm.hidden = false;
    textarea.focus();
  }

  function addNote(note) {
    if (embedded) {
      window.parent.postMessage({ type: "loose-ends:note", action: "add", note }, "*");
      return;
    }
    state.notes.push({ ...note, id: `local-${state.notes.length + 1}`, created_at: new Date().toISOString(), addressed_run: null, local: true });
    showNotice("Notes are only kept for this page outside the workbench.");
    renderNotes();
  }

  /** Find the rendered formula in `element` whose LaTeX matches. */
  function findMathElement(element, latex) {
    const wanted = normalizeLatex(latex);
    if (!wanted || !element) return null;
    return [...element.querySelectorAll(".math")].find(item => normalizeLatex(item.dataset.latex || item.textContent) === wanted) || null;
  }

  /** Insert `marker` after the formula, after the phrase, or at the end of the anchor. */
  function placeMarker(marker, anchorId, { latex, phrase }) {
    const element = document.getElementById(anchorId);
    if (!element) return null;
    const math = latex ? findMathElement(element, latex) : null;
    if (math) {
      math.insertAdjacentElement("afterend", marker);
      return { math };
    }
    const range = phrase ? findPhraseRange(element, phrase) : null;
    if (range) {
      const end = document.createRange();
      end.setStart(range.endContainer, range.endOffset);
      end.collapse(true);
      end.insertNode(range ? marker : marker);
      return { range };
    }
    element.append(marker);
    return { fallback: true };
  }

  /** Report a problem with a widget: fix it now or queue it for the next run. */
  function openImproveForm(widget, card, { step = null, stepTitle = "", follows = "" } = {}) {
    const rect = (step !== null && card.querySelector(".step-current") ? card.querySelector(".step-current") : card.querySelector(".widget-head")).getBoundingClientRect();
    noteForm.replaceChildren();
    const titleNode = node("div", "note-form-title");
    if (step !== null) renderRichText(`Improve step ${step + 1}: ${stepTitle || ""}`, titleNode);
    else titleNode.textContent = `Improve “${widget.title || widget.id}”`;
    noteForm.append(titleNode);
    const textarea = node("textarea");
    textarea.placeholder = follows ? "What is still wrong?" : "What is off, or what should it do instead?";
    textarea.rows = 3;
    noteForm.append(textarea);
    const actions = node("div", "note-form-actions");
    const noteFor = () => ({
      anchor: widget.anchor, widget: widget.id, quote: step !== null ? `${widget.title || widget.id}, step ${step + 1}` : (widget.title || widget.id),
      step: step === null ? undefined : step, step_title: stepTitle ? String(stepTitle) : "", follows: follows || "",
      message: textarea.value.trim(),
    });
    actions.append(
      button("Fix now", () => {
        const note = noteFor();
        if (!note.message) { textarea.focus(); return; }
        hideNoteUi();
        fixWidgetNow(widget, card, note);
      }, "mini accent"),
      button("Add to list", () => { const note = noteFor(); if (!note.message) { textarea.focus(); return; } addNote(note); hideNoteUi(); }, "mini"),
      button("Cancel", () => hideNoteUi()),
    );
    noteForm.append(actions);
    noteForm.style.left = `${Math.min(rect.left + window.scrollX, window.scrollX + window.innerWidth - 340)}px`;
    noteForm.style.top = `${rect.bottom + window.scrollY + 6}px`;
    noteButton.hidden = true;
    noteForm.hidden = false;
    textarea.focus();
  }

  function fixWidgetNow(widget, card, note) {
    if (!embedded) {
      showNotice("Fix now needs the workbench; the request was kept as a note instead.");
      addNote(note);
      return;
    }
    const status = node("span", "badge warn widget-fixing", "fixing…");
    card.querySelector(".widget-head .spacer").after(status);
    const token = `fix-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
    state.pendingFixes = state.pendingFixes || new Map();
    state.pendingFixes.set(token, { widget, card });
    addPendingRequest(token, { ...note, status: "fixing…" });
    window.parent.postMessage({ type: "loose-ends:fix-widget", token, note }, "*");
  }

  async function reloadWidget(widget, card, summary) {
    const body = card.querySelector(".widget-body");
    const registered = state.instances.get(widget.id);
    try { if (registered && registered.instance && registered.instance.destroy) registered.instance.destroy(); } catch (_) { /* ignore */ }
    body.replaceChildren(node("div", "loading", "Reloading widget…"));
    const fresh = await fetchJson(`widgets/${encodeURIComponent(widget.id)}/widget.json?v=${Date.now()}`, true);
    if (fresh) Object.assign(widget, { title: fresh.title, summary: fresh.summary, steps: fresh.steps || widget.steps, examples: fresh.examples || widget.examples, limitations: fresh.limitations });
    widget.review = { fidelity: "unreviewed", interaction_quality: "unreviewed", summary: `Quick fix applied without review: ${summary || ""}` };
    state.factories.delete(widget.id);
    await new Promise(resolve => {
      const script = document.createElement("script");
      script.src = `widgets/${encodeURIComponent(widget.id)}/${widget.entry || "widget.js"}?v=${Date.now()}`;
      script.addEventListener("load", () => resolve(true));
      script.addEventListener("error", () => resolve(false));
      document.head.append(script);
    });
    body.replaceChildren();
    const controller = state.stepControllers.get(widget.id) || null;
    const instance = mountWidget(widget, body, controller);
    if (controller && instance) { controller.attach(instance); controller.refresh(); }
    state.instances.set(widget.id, { mount: () => {}, instance });
    const head = card.querySelector(".widget-head");
    head.querySelectorAll(".badge").forEach(element => element.remove());
    const title = head.querySelector("strong");
    title.textContent = widget.title || "Visualization";
    title.after(badge("fixed, unreviewed", "warn"));
  }

  function showFixResult(token, data) {
    clearPendingRequest(token);
    const pending = state.pendingFixes && state.pendingFixes.get(token);
    if (state.pendingFixes) state.pendingFixes.delete(token);
    if (!pending) return;
    pending.card.querySelectorAll(".widget-fixing").forEach(element => element.remove());
    if (Array.isArray(data.notes)) { state.notes = data.notes; renderNotes(); }
    if (!data.ok) {
      showNotice(`Quick fix failed: ${data.error || "unknown error"}. The request was kept in the notes list.`);
      return;
    }
    reloadWidget(pending.widget, pending.card, data.summary);
  }

  /** Ask the workbench for an immediate explanation of the selected passage. */
  function askNow(note) {
    if (!embedded) {
      showNotice("Answer now needs the workbench; the note was kept locally instead.");
      addNote(note);
      return;
    }
    const pending = node("span", "explain-mark pending", "…");
    pending.title = "Thinking…";
    const old = note.revises ? content.querySelector(`.explain-mark[data-term="${CSS.escape(`explain-${note.revises}`)}"]`) : null;
    if (old) old.replaceWith(pending);
    else placeMarker(pending, note.anchor, { latex: note.latex, phrase: note.quote });
    const token = `ask-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
    state.pendingAsks = state.pendingAsks || new Map();
    state.pendingAsks.set(token, pending);
    addPendingRequest(token, { ...note, status: "answering…" });
    window.parent.postMessage({ type: "loose-ends:explain", token, note }, "*");
  }

  function addPendingRequest(token, note) {
    state.pendingRequests = state.pendingRequests || new Map();
    state.pendingRequests.set(token, note);
    renderNotes();
  }

  function clearPendingRequest(token) {
    if (state.pendingRequests) state.pendingRequests.delete(token);
  }

  function showQuickAnswer(token, data) {
    clearPendingRequest(token);
    const pending = state.pendingAsks && state.pendingAsks.get(token);
    if (state.pendingAsks) state.pendingAsks.delete(token);
    if (!data.ok) {
      if (pending) pending.remove();
      showNotice(`Quick answer failed: ${data.error || "unknown error"}. The note was kept in the list.`);
      if (Array.isArray(data.notes)) { state.notes = data.notes; renderNotes(); }
      return;
    }
    if (Array.isArray(data.notes)) state.notes = data.notes;
    if (Array.isArray(data.glossary) && state.annotations) state.annotations.glossary = data.glossary;
    if (pending) pending.remove();
    resyncExplanations(Array.isArray(data.explanations) ? data.explanations : ((state.annotations && state.annotations.explanations) || []));
    renderNotes();
    const entry = data.explanation;
    if (entry) {
      const key = `explain-${entry.id}`;
      const marker = content.querySelector(`.explain-mark[data-term="${CSS.escape(key)}"]`);
      if (marker) marker.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    }
  }

  function removeNote(noteId) {
    if (embedded && !String(noteId).startsWith("local-")) {
      window.parent.postMessage({ type: "loose-ends:note", action: "remove", id: noteId }, "*");
      return;
    }
    state.notes = state.notes.filter(note => note.id !== noteId);
    if (state.annotations && Array.isArray(state.annotations.explanations)) {
      resyncExplanations(state.annotations.explanations.filter(entry => entry.note !== noteId));
    }
    renderNotes();
  }

  function renderNotes() {
    const open = state.notes.filter(note => !note.addressed_run);
    if (notesButton) notesButton.textContent = `Notes (${open.length}${state.notes.length > open.length ? ` of ${state.notes.length}` : ""})`;
    // Highlight the quoted passages.
    if (HAS_HIGHLIGHT_API) {
      if (!state.noteHighlight) {
        state.noteHighlight = new Highlight();
        CSS.highlights.set("reader-note", state.noteHighlight);
      }
      state.noteHighlight.clear();
      content.querySelectorAll(".noted-math").forEach(element => element.classList.remove("noted-math"));
      state.notes.forEach(note => {
        if (note.addressed_run) return;
        const element = document.getElementById(note.anchor);
        if (!element) return;
        const math = note.latex ? findMathElement(element, note.latex) : null;
        if (math) { math.classList.add("noted-math"); return; }
        const range = findPhraseRange(element, note.quote);
        if (range) state.noteHighlight.add(range);
      });
    }
    // Panel contents.
    notesPanel.replaceChildren();
    const head = node("div", "notes-panel-head");
    head.append(node("strong", "", "Reader notes"));
    head.append(node("span", "spacer"));
    head.append(button("Close", () => { notesPanel.hidden = true; }));
    notesPanel.append(head);
    if (!state.notes.length) {
      notesPanel.append(node("p", "notes-empty", "Select any passage you do not follow and choose “I don't get this”. The next visualization run explains the noted passages."));
    }
    const list = node("ul", "notes-list");
    (state.pendingRequests ? [...state.pendingRequests.values()] : []).forEach(request => {
      const item = node("li", "note-item pending");
      item.append(node("div", "note-quote", request.widget ? `Widget: ${request.quote}` : `“${request.quote.slice(0, 140)}”`));
      if (request.message) item.append(node("div", "note-message", request.message));
      const meta = node("div", "note-meta");
      meta.append(node("span", "note-status", request.status));
      item.append(meta);
      list.append(item);
    });
    state.notes.forEach(note => {
      const item = node("li", `note-item${note.addressed_run ? " addressed" : ""}`);
      const quoteNode = node("div", "note-quote");
      if (note.widget) quoteNode.textContent = `Widget: ${note.quote}`;
      else if (note.latex) renderLatex(note.latex, quoteNode, false);
      else quoteNode.textContent = `“${note.quote.length > 140 ? note.quote.slice(0, 137) + "…" : note.quote}”`;
      item.append(quoteNode);
      if (note.message) item.append(node("div", "note-message", note.message));
      if (note.outcome) item.append(node("div", "note-outcome", `→ ${note.outcome}`));
      const meta = node("div", "note-meta");
      meta.append(node("span", "", note.addressed_run ? `addressed in ${note.addressed_run}` : "open"));
      meta.append(button("Go to", () => {
        reveal(note.anchor, { flash: true });
      }));
      meta.append(button("Unclear?", () => followUp(note)));
      meta.append(button("Remove", () => removeNote(note.id)));
      item.append(meta);
      list.append(item);
    });
    notesPanel.append(list);
    if (open.length) {
      const containers = [...new Set(open.map(note => {
        const element = document.getElementById(note.anchor);
        const holder = element && element.closest(".proof, .env");
        return holder ? holder.id : null;
      }).filter(Boolean))];
      notesPanel.append(button(`Explain ${open.length} open note${open.length === 1 ? "" : "s"}`, () => requestVisualization(["notes", ...containers], "notes"), "tool primary"));
    }
  }

  /** Ask again about a note: revise its quick answer, or file a follow-up request. */
  function followUp(note) {
    notesPanel.hidden = true;
    if (note.widget) {
      const widget = state.widgets.find(item => item.id === note.widget);
      const card = widget ? content.querySelector(`.widget-card[data-widget="${CSS.escape(widget.id)}"]`) : null;
      if (!widget || !card) { showNotice("That widget is not mounted on this page."); return; }
      openImproveForm(widget, card, { step: Number.isInteger(note.step) ? note.step : null, stepTitle: note.step_title || "", follows: note.id });
      return;
    }
    const explanation = ((state.annotations && state.annotations.explanations) || []).find(entry => entry.note === note.id);
    const element = document.getElementById(note.anchor);
    const rect = element ? element.getBoundingClientRect() : { left: 40, bottom: 60 };
    showNoteForm({
      anchor: note.anchor, quote: note.quote, latex: note.latex || "",
      revises: explanation ? explanation.id : "", follows: explanation ? "" : note.id,
      previousTitle: explanation ? explanation.title : note.message || note.quote,
    }, { left: `${Math.min(rect.left + window.scrollX, window.scrollX + window.innerWidth - 340)}px`, top: `${rect.bottom + window.scrollY + 6}px` });
  }

  function wireNotes() {
    notesButton = button("Notes (0)", () => {
      notesPanel.hidden = !notesPanel.hidden;
      if (!notesPanel.hidden) renderNotes();
    }, "tool");
    toolbarActions.prepend(notesButton);
    document.addEventListener("selectionchange", () => { clearTimeout(wireNotes.timer); wireNotes.timer = setTimeout(onSelectionChange, 200); });
    noteButton.addEventListener("mousedown", event => event.preventDefault());
    noteButton.addEventListener("click", openNoteForm);
    document.addEventListener("keydown", event => { if (event.key === "Escape") hideNoteUi(); });
    renderNotes();
  }

  // ------------------------------------------------------------------
  // Boot
  // ------------------------------------------------------------------

  async function init() {
    try {
      state.manifest = await fetchJson("visualization.json", true);
      state.doc = await fetchJson("document.json");
      const response = await fetch("document.html", { cache: "no-store" });
      if (!response.ok) throw new Error(`document.html: HTTP ${response.status}`);
      content.innerHTML = await response.text();
    } catch (error) {
      content.replaceChildren(node("div", "widget-error", `Could not load the paper: ${error && error.message || error}`));
      return;
    }
    if (state.manifest) {
      state.widgets = (state.manifest.widgets || []).filter(widget => widget && widget.id && widget.anchor);
      if (state.manifest.annotations) state.annotations = await fetchJson(state.manifest.annotations, true);
    }
    const notes = await fetchJson("notes.json", true);
    state.notes = notes && Array.isArray(notes.notes) ? notes.notes : [];
    document.title = `${state.doc.title || "Paper"} · Loose Ends reader`;
    typeset(content);
    makeCollapsible();
    wireReferences();
    buildOutline();
    addStatementActions();
    buildToolbar();
    applyGlossary();
    applyExplanations();
    if (!state.glossary || !state.popoverWired) { /* popover wiring happens in applyGlossary when entries exist */ }
    await mountWidgets();
    wireNotes();
    const warnings = (state.doc.warnings || []).length;
    if (warnings) showNotice(`${warnings} conversion warning${warnings === 1 ? "" : "s"}: ${state.doc.warnings.join(" · ")}`);
    if (location.hash) reveal(decodeURIComponent(location.hash.slice(1)));
  }

  window.addEventListener("message", event => {
    const data = event.data || {};
    if (data.type === "loose-ends:reveal" && data.id) reveal(String(data.id));
    if (data.type === "loose-ends:notes" && Array.isArray(data.notes)) {
      state.notes = data.notes;
      if (Array.isArray(data.explanations)) resyncExplanations(data.explanations);
      renderNotes();
    }
    if (data.type === "loose-ends:explained" && data.token) showQuickAnswer(data.token, data);
    if (data.type === "loose-ends:widget-fixed" && data.token) showFixResult(data.token, data);
  });

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
