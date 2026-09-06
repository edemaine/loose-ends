// Local-only browser fixture, served by ReaderBrowserTests. No model/backend calls.
"use strict";

if (window.parent !== window) {
  window.revision = 1;
  window.calls = [];
  window.captureMode = "ok";
  window.restoreMode = "ok";
  window.scriptFailure = false;
  window.fixtureListeners = { scroll: new Set(), resize: new Set() };
  const addListener = window.addEventListener.bind(window), removeListener = window.removeEventListener.bind(window);
  window.addEventListener = (type, listener, options) => {
    window.fixtureListeners[type]?.add(listener);
    addListener(type, listener, options);
  };
  window.removeEventListener = (type, listener, options) => {
    window.fixtureListeners[type]?.delete(listener);
    removeListener(type, listener, options);
  };
  const metadata = () => {
    const value = {
    id: "proof-widget", anchor: "proof-1", kind: "proof",
    title: `Revision ${window.revision}`, summary: `Summary ${window.revision}`,
    document_digest: window.revision === 1 ? "old" : "current",
    limitations: [`Limitation ${window.revision}`],
    examples: window.revision < 3
      ? [{ id: "generic", label: "Generic" }, { id: "special", label: `Special ${window.revision}`, note: `Note ${window.revision}` }]
      : [{ id: "replacement", label: "Replacement" }],
    steps: window.revision === 1
      ? [{ title: "First", paragraphs: ["par-1"] }, { title: "Second", paragraphs: ["par-2"] }]
      : [{ title: "Second revised", paragraphs: ["par-2"] }, { title: "First revised", paragraphs: ["par-1"] }, { title: "Third", paragraphs: ["par-3"] }],
    };
    if (window.omitControls) { delete value.examples; delete value.steps; delete value.limitations; }
    return value;
  };
  const documentData = {
    title: "Fixture", source: { digest: "current" }, warnings: ["Unsupported fixture command"],
    sections: [], statements: [], figures: [], equations: [],
    proofs: [{ id: "proof-1", paragraphs: ["par-1", "par-2", "par-3"] }],
    paragraphs: [1, 2, 3].map(i => ({ id: `par-${i}`, text: `Paragraph ${i}` })),
  };
  window.fetch = async path => {
    const name = String(path).split("?")[0];
    const values = {
      "visualization.json": { widgets: [{ id: "proof-widget", anchor: "proof-1" }], annotations: "annotations.json" },
      "document.json": documentData,
      "document.html": '<section id="proof-1" class="proof"><div class="proof-head">Proof</div><div class="proof-body"><p class="par" id="par-1">Paragraph 1</p><p class="par" id="par-2">Paragraph 2</p><p class="par" id="par-3">Paragraph 3</p></div></section>',
      "annotations.json": { document_digest: "old", glossary: [], proof_outlines: {} },
      "notes.json": { notes: [] },
      "widgets/proof-widget/widget.json": metadata(),
      "widgets/proof-widget/review.json": { fidelity: "unreviewed", summary: `Review ${window.revision}` },
    };
    return new Response(typeof values[name] === "string" ? values[name] : JSON.stringify(values[name] || {}));
  };
  window.installFixtureWidget = () => {
    if (window.scriptFailure) return; // Script loads but fails to register.
    LooseEnds.registerWidget("proof-widget", (container, api) => {
      const revision = window.revision;
      let example = "", step = -1, destroyed = false;
      const input = document.createElement("input");
      input.className = "fixture-input";
      container.append(input);
      const log = action => {
        window.calls.push({ revision, action, example, step, value: input.value });
        if (destroyed) throw new Error("Disposed instance called");
        container.dataset.step = String(step);
        container.dataset.example = example;
      };
      const instance = {
        setExample(id) { example = id; input.value = id === "special" ? "20" : "10"; log("example"); },
        setStep(index) { step = index; log("step"); },
        getState() {
          if (window.captureMode === "throw") throw new Error("fixture capture failed");
          if (window.captureMode === "large") return { text: "x".repeat(32768) };
          if (window.captureMode === "nan") return { value: NaN };
          return { version: 1, value: Number(input.value) };
        },
        setState(snapshot) {
          if (window.restoreMode === "reject") return false;
          input.value = String(snapshot.value); log("state"); return true;
        },
        destroy() { log("destroy"); destroyed = true; },
      };
      if (window.legacyWidget) { delete instance.getState; delete instance.setState; }
      return instance;
    });
  };
} else {
  const run = async () => {
    const result = document.createElement("pre");
    result.id = "result";
    document.body.append(result);
    const frame = document.createElement("iframe");
    frame.style.cssText = "width:1200px;height:900px";
    frame.src = "/reader.html";
    document.body.append(frame);
    const pause = () => new Promise(resolve => setTimeout(resolve, 20));
    const until = async (predicate, description) => {
      for (let i = 0; i < 200; i++) { if (predicate()) return; await pause(); }
      throw new Error(`Timed out: ${description}`);
    };
    const assert = (condition, message) => { if (!condition) throw new Error(message); };
    try {
      await until(() => frame.contentDocument.querySelector(".fixture-input") && frame.contentDocument.querySelector("#notice").textContent.includes("conversion warning"), "reader startup");
      const win = frame.contentWindow, doc = frame.contentDocument;
      const listenerCounts = () => Object.values(win.fixtureListeners).map(listeners => listeners.size).join();
      const initialListeners = listenerCounts();
      const card = () => doc.querySelector(".widget-card");
      const notice = () => doc.querySelector("#notice").textContent;
      const click = (root, label) => {
        const button = [...root.querySelectorAll("button")].find(b => b.textContent === label);
        assert(button, `Missing button ${label}`); button.click();
      };
      const select = value => {
        const element = card().querySelector("select");
        element.value = value; element.dispatchEvent(new win.Event("change"));
      };
      let request = null;
      window.addEventListener("message", event => {
        if (event.data.type === "loose-ends:fix-widget") request = event.data;
      });
      const submit = async (whileFormOpen = () => {}) => {
        request = null;
        click(card().querySelector(".widget-head"), "Improve…");
        const form = doc.querySelector(".note-form:not([hidden])");
        whileFormOpen();
        form.querySelector("textarea").value = "Fix fixture";
        click(form, "Fix now");
        await until(() => request, "fix request");
        return request;
      };
      const finish = async (revision, whileFormOpen) => {
        const pending = await submit(whileFormOpen);
        win.revision = revision;
        win.postMessage({ type: "loose-ends:widget-fixed", token: pending.token, ok: true, summary: "Fixed" }, "*");
        await until(() => card().querySelector(".widget-title").textContent === `Revision ${revision}`, "widget replacement");
        return pending.note;
      };
      assert(notice().includes("earlier version") && notice().includes("conversion warning"), "Persistent warnings coexist");
      assert(card().querySelector(".widget-stale"), "Stale widget marked locally");
      select("special");
      card().querySelector(".fixture-input").value = "42";
      click(card(), "▶");
      click(card(), "Details");
      const note = await finish(2, () => { card().querySelector(".fixture-input").value = "43"; });
      assert(note.example === "special" && note.step === 1 && note.widget_state.value === 42, "Feedback captures example, step, and edits");
      assert(card().querySelector("select").value === "special", "Selected example restored");
      assert(card().querySelector(".fixture-input").value === "43", "Reload preserves ongoing edits; feedback keeps its original snapshot");
      assert(card().querySelector(".step-counter").textContent === "Step 1 of 3", "Reordered step matched by target");
      assert(card().querySelector(".step-current").textContent.includes("Second revised"), "New step text used");
      assert(card().classList.contains("details-open") && card().querySelector(".widget-details").textContent.includes("Limitation 2"), "Details rebuilt and open state preserved");
      assert(card().querySelector(".widget-details").textContent.includes("Review 2") && !card().querySelector(".widget-details").textContent.includes("Limitation 1"), "Review and limitations refreshed");
      assert(card().querySelector("select").options[1].textContent === "Special 2" && card().querySelector(".widget-example-note").textContent === "Note 2", "Example choices and note rebuilt");
      assert(!card().querySelector(".widget-stale") && notice().includes("annotations") && !notice().includes("widget proof-widget"), "Only refreshed artifact loses its stale marker");
      const restored = win.calls.filter(call => call.revision === 2).map(call => call.action);
      assert(restored.slice(0, 3).join() === "example,state,step", "Restore order");
      assert(listenerCounts() === initialListeners, "Reload replaces rather than accumulates scroll/resize handlers");
      const oldCalls = win.calls.filter(call => call.revision === 1).length;
      win.dispatchEvent(new win.Event("resize")); win.dispatchEvent(new win.Event("scroll"));
      await pause();
      assert(win.calls.filter(call => call.revision === 1).length === oldCalls, "Disposed controller does not drive old widget");
      select("generic");
      assert(card().querySelector(".fixture-input").value === "10", "New selector drives new instance");
      select("special");
      await finish(3);
      assert(card().querySelector("select").value === "replacement" && card().querySelector(".fixture-input").value === "10", "Removed example resets safely");
      assert(notice().includes("previous example is no longer available") && notice().includes("conversion warning") && notice().includes("earlier version"), "Transient and persistent warnings coexist");
      win.restoreMode = "reject";
      card().querySelector(".fixture-input").value = "99";
      await finish(4);
      assert(card().querySelector(".fixture-input").value === "10" && notice().includes("Edited inputs were not restored"), "Incompatible state resets visibly");
      for (const mode of ["throw", "large", "nan"]) {
        win.captureMode = mode;
        const pending = await submit();
        assert(pending.note.widget_state === undefined && pending.note.widget_state_error, `Invalid capture rejected: ${mode}`);
        win.postMessage({ type: "loose-ends:widget-fixed", token: pending.token, ok: false }, "*");
        await pause();
      }
      win.captureMode = "ok";
      const pending = await submit();
      win.scriptFailure = true; win.revision = 5;
      win.postMessage({ type: "loose-ends:widget-fixed", token: pending.token, ok: true }, "*");
      await until(() => notice().includes("could not be displayed"), "failed script notice");
      assert(card().querySelector(".widget-title").textContent === "Revision 4", "Load failure keeps working old view");
      select("replacement");
      assert(card().querySelector(".fixture-input").value === "10", "Old instance still usable");
      win.scriptFailure = false; win.restoreMode = "ok"; win.omitControls = true;
      await finish(6);
      assert(!card().querySelector("select") && !card().querySelector(".step-counter"), "Removed examples and steps do not survive reload");
      assert(!card().querySelector(".widget-details").textContent.includes("Limitation"), "Removed limitations do not survive reload");
      win.omitControls = false; win.legacyWidget = true;
      await finish(7);
      assert(notice().includes("Edited inputs were not restored"), "Missing restoration hook warns");
      const legacy = await submit();
      assert(!legacy.note.widget_state && legacy.note.widget_state_error.includes("does not export"), "Legacy widget reports capture limitation");
      result.dataset.status = "passed"; result.textContent = "Reader regressions passed";
    } catch (error) {
      result.dataset.status = "failed"; result.textContent = error.stack || String(error);
    }
  };
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", run);
  else run();
}
