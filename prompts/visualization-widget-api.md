# Widget API for the Loose Ends paper reader

A visualization package augments a converted paper with *widgets*: small,
self-contained interactive figures anchored to one statement or one proof.
The reader (`reader.html`, `reader.js`, `reader.css`) is trusted code that
renders the paper, mounts your widget next to its anchor, and drives proof
steps. Your widget is a single classic script, `widgets/<id>/widget.js`,
plus optional static assets in the same directory.

## Registration

```js
LooseEnds.registerWidget("<id>", function (container, api) {
  // Build the widget inside `container` (an empty <div>) using DOM or SVG.
  return {
    setStep(index, step) { /* proof widgets: show step `index` */ },
    destroy() { /* optional cleanup */ },
  };
});
```

`<id>` must equal the widget directory name. Register exactly once, at load
time. Do not use ES module syntax, `import`, `eval`, `new Function`, workers,
network access (`fetch` to remote hosts, `XMLHttpRequest`, `WebSocket`),
browser storage, or external scripts and stylesheets. Everything you need must
be in `widget.js` or in files beside it, referenced through `api.assetUrl`.

## The `api` object

| Member | Meaning |
| --- | --- |
| `api.id` | the widget id |
| `api.anchorId` | the anchor (statement or proof id in `document.json`) |
| `api.anchor` | the statement or proof record from `document.json` |
| `api.document` | the whole `document.json` (sections, statements, proofs, paragraphs, macros) |
| `api.widget` | your `widget.json` |
| `api.steps` | the `steps` array from `widget.json` (proof widgets) |
| `api.container` | the mount element |
| `api.katex.render(latex, element, display)` | render LaTeX math into an element with the paper's macros |
| `api.svgPoint(svg, event)` | map a pointer event to the SVG's viewBox coordinates (`{x, y}`), correct under any CSS scaling; use this for every click or drag on an SVG |
| `api.renderText(text, element)` | render prose containing `$...$` and `$$...$$` into an element |
| `api.typeset(element)` | render every `<span class="math">` (text = LaTeX) inside `element` |
| `api.assetUrl(name)` | URL for a file beside `widget.js` |
| `api.goTo(id)` | expand and scroll the paper to an element id |
| `api.requestStep(index)` | proof widgets: ask the reader to select a step (highlights the paragraphs) |
| `api.macros` | KaTeX macro table extracted from the paper preamble |

## `widget.json`

```json
{
  "id": "lem-tiling-completion",
  "anchor": "lem:tiling-completion",
  "kind": "statement",
  "title": "Reflecting a lattice polygon",
  "summary": "One sentence saying what the reader can see or do.",
  "limitations": ["Optional list of honest limitations."],
  "steps": [
    {"title": "Reflect $P$ across a side", "paragraphs": ["par-48", "par-49"], "note": "optional one-line note"}
  ]
}
```

`kind` is `statement` or `proof`. `steps` is required for proof widgets and
must be omitted (or empty) for statement widgets. Every paragraph id in a step
must belong to the anchored proof, in reading order, covering the proof's
substantive paragraphs without overlap. Titles and notes may contain `$...$`.

## Layout and behaviour

- A statement widget is mounted beside its statement on wide screens (a
  column between 380px and 640px wide, sticky while the reader scrolls the
  statement) and below it on narrow screens (up to 780px). Design for a
  fluid width with a 380px minimum: one SVG with a fixed `viewBox` and
  `width: 100%`, controls that wrap. Prefer SVG. Keep the initial state
  meaningful: the reader should understand the statement better at a
  glance, before touching any control.
- A proof widget is mounted in a sticky side panel beside the proof text.
  The panel is about 360px wide; design for that width and at most ~420px
  of height above the step list. The reader calls `setStep(index, step)`
  when the reader clicks a step or scrolls to its paragraphs; show the state
  of the running example after that step. Step 0 is called on mount.
- Use `api.katex.render` for any formula; never hand-write formula text.
- Keep everything deterministic. No timers except for explicit animation the
  reader starts, and no randomness without a fixed seed.
- Use only the fonts and colors of the page (inherit), plus a small palette
  for geometry. The page is light; keep contrast high.
- Handle degenerate inputs in playgrounds (self-intersection, collinear
  points, empty state) with a visible message that names the violated
  hypothesis.
- Do not print the full statement or proof again in the widget; the paper
  text is right next to it. Short labels and one-line captions only.

## Interaction rules (obvious interface errors are the most common failure)

- **Fixed frame.** Choose the `viewBox` and the world-to-screen mapping
  once, when the widget mounts. Never rescale, recenter, or refit the view
  in response to a click, drag, preset, or step change; a moving grid makes
  every interaction feel broken. If a construction would leave the frame,
  clip it and say so in the caption.
- **One editable object per frame.** Derived objects (reflections, duals,
  completions) are drawn only when the reader asks for them, in a clearly
  distinct style, and never intercept pointer events.
- **Simplest possible gestures.** Click to add a vertex, click an existing
  vertex to remove it, a Clear button. Avoid drag handles unless the
  mathematics needs continuous parameters; then use a slider.
- **Pointer mapping.** Use `api.svgPoint(svg, event)`, then snap in world
  coordinates. Do not compute positions from `getBoundingClientRect`
  ratios.
- **Prove it works before finishing.** Write a Node harness with a small
  DOM stub, mount the widget, simulate the exact sequence a reader will try
  (add three points, remove one, clear, load each preset, switch each tab)
  and assert the visible state after each action, including that the frame
  mapping did not change. Record this in `verification_checks`.

## Testing without a browser

`node --check widget.js` verifies syntax. The reader's `reader.js` shows the
exact call sequence; you may execute pure geometry helpers under Node with
a tiny DOM stub to check computations.
