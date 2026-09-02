# Independently review paper-reading aids

A designer added reading aids to a mathematical paper: possibly an
`annotations.json` (definition popovers, the choice of main result, proof
outlines) and one or more widgets (interactive figures anchored to a
statement or a proof). Audit them against the paper itself.

Staged read-only inputs:

- `inputs/document/document.html` and `document.json`: the rendered paper and
  its structure (statement ids, proof ids, paragraph ids and text);
- `inputs/source/`: the original LaTeX;
- `inputs/generated/annotations.json` (if this run produced annotations) and
  `inputs/generated/widgets/<id>/` (the widgets produced in this run);
- `inputs/generated/agent-result.json`: what the designer claims;
- `inputs/reader/WIDGET-API.md` and the reader source, which define how the
  widgets are mounted and driven.

Inspect every generated file. Exercise the widgets' logic in a deterministic
way where you can (Node with a small DOM stub for pure computations;
`node --check` for syntax) and recompute every number, coordinate, angle,
count, or example a widget displays or relies on.

## Annotations

For each glossary entry: is the term actually defined or fixed by the paper,
does the gloss restate that definition faithfully (hypotheses, quantifiers,
notation), and does the anchor point at the defining element? Are important
definitions or notations missing? For `main_result`: is it the paper's
central statement? For each proof outline: do the steps partition the proof's
paragraphs in order, and do their titles state the intermediate assertions
(not vague stage names)?

Rate `annotations_review.accuracy` as `accurate`, `minor_issues`, or
`major_issues`; use `not_applicable` only if this run produced no
annotations.

## Widgets

For each widget, judge:

- **Fidelity.** Does the initial state and every reachable state represent
  the anchored statement or proof step exactly: same objects, hypotheses,
  quantifiers, conventions, and numbering? Are illustrative instances marked
  as such? Are failing examples correctly diagnosed with the hypothesis the
  paper names? For proof widgets: does the state at each step match what the
  corresponding paragraphs establish, no more and no less?
- **Interaction.** Simulate the reader's actual gestures in a DOM stub:
  clicks at known viewBox coordinates (through the widget's own mapping),
  presets, tabs, undo and clear. After each action, check that the
  coordinate frame and grid did not move, that exactly the expected object
  changed, and that no derived object appeared unasked. Does it work
  without errors in the reader's API contract
  (`registerWidget`, `setStep`, no network, no storage, no globals beyond
  `LooseEnds`)? Are degenerate inputs handled with a visible reason? Is it
  legible at the panel width the API describes?

Rate `fidelity` as `well_supported`, `minor_gaps`, `major_gaps`, or
`incorrect`, and `interaction_quality` as `works`, `minor_issues`,
`major_issues`, or `unusable`. Anything mathematically misleading is at
least `major_gaps` and must be recorded in `blocking_gaps`.

Do not edit generated files or staged inputs. Write `critique.md` beginning
with a Markdown heading: what you checked, the result per glossary entry
group and per widget, every material finding, and the blocking gaps. Then
write the structured review to `agent-result.json`.
