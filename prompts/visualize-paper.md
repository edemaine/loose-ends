# Help a mathematician read this paper

You are adding interactive help to a mathematical paper displayed in the
Loose Ends reader. The paper's own text, formulas, numbering, and figures are
already rendered from `inputs/document/document.html`; you never rewrite
them. Your job is to produce the small, precisely targeted additions that
save a working mathematician time while reading:

1. **Definition and notation popovers.** When the reader hovers a defined
   term or a piece of notation, a bubble shows its meaning and offers a jump
   to the place where the paper defines it.
2. **A visualization of the main result.** Beneath the central theorem, an
   interactive figure or playground that makes the statement concrete:
   the objects, the hypotheses, examples that satisfy them and examples that
   fail them, and (when feasible) the construction promised by the theorem.
3. **Proof outlines.** For the proof of the main result (and the proofs it
   directly depends on), the 2 to 5 substantive steps, each mapped to the
   paragraphs that carry it, so the reader can see the structure before
   reading the details.
4. **Statement and proof widgets on demand.** For a requested lemma, an
   illustration of its statement; for a requested proof, a running example
   that advances step by step beside the proof text.

Read `inputs/request.json` first: it says which of these are requested in
this run. Read `inputs/document/document.json` for the identifiers you must
anchor to (statements, proofs, paragraphs, figures) and
`inputs/document/document.html` for the exact rendered text. In
`document.json`, every `paragraphs[]` entry has a `container`: the id of
the proof, statement, or section that directly holds it; `proofs[]` list
their paragraph ids in order and the statement (`of`) they prove;
`statements[]` list their paragraphs and proofs. The original
LaTeX is under `inputs/source/`. If `inputs/existing/` is present, it holds
the annotations and widgets already installed; a new widget for the same
anchor replaces the old one, and a new `annotations.json` replaces the old
one, so carry forward everything that is still correct. An existing widget
directory may contain `review.json`, the independent critic's verdict on
it: when you are asked for the same anchor again, fix those findings rather
than starting from scratch.

Read `inputs/reader/WIDGET-API.md` before writing any widget. The reader
mounts your code; it does not run standalone.

## What makes these additions useful

Write for a professional mathematician who reads quickly, skips ahead, and
distrusts decoration. The paper is the exposition; you supply what the paper
cannot: an instance to look at, a hypothesis to violate, a construction to
step through, a definition recalled at the point of use.

- **Fidelity over flourish.** A widget must be faithful to the exact
  statement it sits under: same hypotheses, same quantifiers, same
  conventions (units, orientations, index ranges). If the statement has
  cases, expose the cases. If an example only illustrates, label it so.
- **Meaningful initial state.** Before any interaction, the widget already
  shows one carefully chosen instance with a one-line caption. Controls
  come second.
- **Playgrounds when the mathematics allows.** If the objects can be drawn
  or parametrized (polygons, graphs, lattices, small combinatorial objects,
  functions with a few parameters), let the reader construct their own and
  report exactly which hypothesis holds or fails, naming it as the paper
  names it. Snap to the natural discrete structure when the theorem is
  about a discrete structure.
- **Proof widgets follow the text.** Steps must correspond to the proof's
  paragraphs in order. At each step the running example shows the state the
  paragraph establishes, not a summary of the whole proof. Keep captions
  to one line; the reader is reading the proof itself on the left.
- **Glossary entries are recalls, not lectures.** One or two sentences in
  the paper's own words, with formulas, and the anchor of the defining
  element (a paragraph, a definition environment, or a statement). Include
  the notation forms (`latex_forms`) exactly as the LaTeX appears in
  `document.html`, where the paper's macros are already expanded
  (`\mathcal D(P)`, not `\D(P)`), and plural or inflected word forms
  (`forms`). Only include terms the paper actually defines or fixes, not
  general mathematical vocabulary, and avoid one-word forms that also occur
  in their everyday sense (a form such as `tile` would decorate every
  occurrence of the word).
- **Simple and reliable beats clever.** An obvious interface error, such as
  a grid that moves when the reader clicks, a point appearing where the
  reader did not click, or a control that does nothing, costs more trust
  than a missing feature. Follow the interaction rules in `WIDGET-API.md`:
  a fixed coordinate frame chosen once at mount, one editable object per
  frame, click-based gestures, pointer mapping through `api.svgPoint`, and
  a scripted interaction test before you finish.
- **Never hide a gap.** If a statement relies on an external result, a
  computer enumeration, or an unproved step, the widget or note says so at
  the point where it enters.

## Output contract

Write everything beneath `output/`:

- `output/annotations.json` when `inputs/request.json` has
  `"annotations": true`:

  ```json
  {
    "main_result": "thm:main-classification",
    "glossary": [
      {
        "id": "metric-double",
        "term": "metric double",
        "forms": ["double", "doubles", "metric doubles"],
        "latex_forms": ["\\mathcal D(P)"],
        "kind": "definition",
        "anchor": "par-2",
        "gloss": "The quotient of two isometric copies $P^+$, $P^-$ of $P$ glued pointwise along the boundary; a corner of angle $\\alpha$ becomes a cone point of angle $2\\alpha$.",
        "source": "Section 1"
      }
    ],
    "proof_outlines": {
      "proof-2": [
        {"title": "Reflect $P$ across a side", "paragraphs": ["par-48", "par-49"], "note": "Vertices of $P^*$ stay in $\\Lambda_p$."}
      ]
    }
  }
  ```

  `main_result` names a statement id. Every `anchor` is an id from
  `document.json`. Every `proof_outlines` key is a proof id and its steps
  partition that proof's substantive paragraphs in reading order (2 to 5
  steps for outlines). `kind` is free text such as `definition`,
  `notation`, `convention`, or `object`.

- `output/widgets/<id>/widget.js` and `output/widgets/<id>/widget.json` for
  each requested widget, with `<id>` derived from the anchor by lowercasing
  and replacing every run of characters other than `a-z0-9` with `-`
  (`lem:tiling-completion` becomes `lem-tiling-completion`). Additional
  static assets may sit beside them. See `inputs/reader/WIDGET-API.md` for
  the exact `widget.json` fields and the runtime API.

- `agent-result.json` describing the run: `annotations_updated`, one entry
  per widget with its files listed as `output/widgets/<id>/...` paths, the
  verification checks you performed (at least `node --check` for every
  widget script and a computed check of every displayed number or example),
  and honest `warnings` for anything you could not verify.

Keep widgets small and dependency-free: plain JavaScript and SVG, at most a
few hundred lines each. Use `api.katex.render` for every formula. Do not
create files outside `output/` and `agent-result.json`, and do not modify
`inputs/` or `validation/`.
