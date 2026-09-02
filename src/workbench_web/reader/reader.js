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
  };
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
    if (!glossary.length) return;
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
      }
      if (entry.source) popover.append(node("div", "popover-source", entry.source));
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
    head.append(node("strong", "", widget.title || "Visualization"));
    const review = widget.review || null;
    if (review) {
      head.append(badge(`fidelity: ${humanize(review.fidelity || "unreviewed")}`, verdictClass(review.fidelity)));
      if (review.interaction_quality) head.append(badge(`interaction: ${humanize(review.interaction_quality)}`, verdictClass(review.interaction_quality)));
    } else {
      head.append(badge("unreviewed", "neutral"));
    }
    head.append(node("span", "spacer"));
    head.append(button("Details", () => card.classList.toggle("details-open")));
    card.append(head);
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
    return { card, body };
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
    const controller = {
      setStep(index, { scroll = false, fromWidget = false } = {}) {
        if (index < 0 || index >= steps.length) return;
        active = index;
        items.forEach((item, position) => item.classList.toggle("active", position === index));
        const ids = new Set(steps[index].paragraphs || []);
        paragraphs.forEach(paragraph => paragraph.classList.toggle("step-active", ids.has(paragraph.id)));
        if (scroll) {
          const first = (steps[index].paragraphs || []).map(id => document.getElementById(id)).find(Boolean);
          if (first) {
            // Ignore scroll-following while the smooth scroll is in flight.
            lockUntil = Date.now() + 1200;
            reveal(first.id, { flash: false });
          }
        }
        if (instance && instance.setStep && !fromWidget) {
          try { instance.setStep(index, steps[index]); } catch (error) { console.error(error); }
        }
      },
      attach(value) { instance = value; },
      get active() { return active; },
    };
    if (steps.length) {
      const nav = node("div", "step-nav");
      nav.append(
        button("◀ Previous", () => controller.setStep(Math.max(0, active - 1), { scroll: true })),
        button("Next ▶", () => controller.setStep(Math.min(steps.length - 1, active + 1), { scroll: true })),
      );
      body.append(list, nav);
    }
    // Follow the reader's scroll position through the proof: the current
    // step is the one containing the last paragraph whose top has passed
    // the reading line. A step revealed by a click lands at SCROLL_OFFSET,
    // above the line, so the highlight agrees with the click.
    if (steps.length) {
      const lookup = new Map();
      steps.forEach((step, index) => (step.paragraphs || []).forEach(id => lookup.set(id, index)));
      const tracked = paragraphs.filter(paragraph => lookup.has(paragraph.id));
      let scheduled = false;
      const sync = () => {
        scheduled = false;
        if (Date.now() < lockUntil) return;
        if (!proof.isConnected || proof.classList.contains("collapsed")) return;
        const proofRect = proof.getBoundingClientRect();
        if (proofRect.bottom < 0 || proofRect.top > window.innerHeight) return;
        let index = lookup.get(tracked[0]?.id) ?? 0;
        for (const paragraph of tracked) {
          if (paragraph.getBoundingClientRect().top <= READING_LINE) index = lookup.get(paragraph.id);
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
      const { card, body } = widgetCard(widget);
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
      const mount = () => {
        const instance = mountWidget(widget, body, controller);
        if (controller && instance) {
          controller.attach(instance);
          controller.setStep(0);
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
    });
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
    document.title = `${state.doc.title || "Paper"} · Loose Ends reader`;
    typeset(content);
    makeCollapsible();
    wireReferences();
    buildOutline();
    addStatementActions();
    buildToolbar();
    applyGlossary();
    await mountWidgets();
    const warnings = (state.doc.warnings || []).length;
    if (warnings) showNotice(`${warnings} conversion warning${warnings === 1 ? "" : "s"}: ${state.doc.warnings.join(" · ")}`);
    if (location.hash) reveal(decodeURIComponent(location.hash.slice(1)));
  }

  window.addEventListener("message", event => {
    const data = event.data || {};
    if (data.type === "loose-ends:reveal" && data.id) reveal(String(data.id));
  });

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
