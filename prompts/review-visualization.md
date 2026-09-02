# Independently review a mathematical visualization

Audit the generated visualization under `inputs/visualization/` against the
selected solver attempt and its surrounding paper context under `inputs/`.
Evaluate it as a standalone interactive mathematical exposition, not merely as
a working diagram or visual-polish exercise.

The intended audience is professional mathematicians. Judge the page as a
precise research lecture or advanced-course exposition by one mathematician
for another, not as popular mathematics. Clarity and intuition must complement
rather than replace formal statements.

Inspect every source file. Exercise the interface and any deterministic tests
that can be run locally. Check calculations independently where practical.
For every claim ID declared by the visualization, decide whether the visible
objects, examples, transitions, labels, and explanations faithfully represent
the corresponding attempt claim.

Then perform a first-reader exposition audit. Begin from the application's
default state and follow its primary visible path in order, pretending the
paper and solver attempt are unavailable. Determine whether a technically
sophisticated reader can learn:

- the original problem, mathematical objects, definitions, and motivation;
- the precise result, with its quantifiers, hypotheses, conclusion, scope, and
  status stated near the beginning;
- what each important hypothesis means, why it matters, and what can fail
  without it;
- the proof roadmap and the substantive proof, construction, or algorithm
  steps;
- at each step, the general inference and how the accompanying visual state
  explains it;
- how any running example relates to, but does not replace, the general proof;
- how the steps establish the stated result, and what remains limited,
  conditional, externally sourced, approximate, or open.

Also audit the formal mathematical register:

- Definitions, Theorems, Propositions, Lemmas, Corollaries, Claims, Proofs,
  Examples, Counterexamples, Remarks, Proof ideas, and Computations must be
  explicitly and accurately distinguished.
- The central result and every substantive intermediate result must be stated
  with proper quantifiers, hypotheses, notation, and conclusions before its
  intuitive explanation or visualization.
- Original theorem, lemma, equation, section, and figure numbers must match the
  staged paper or attempt. Solver claims without source numbering should use
  their exact `C-###` IDs; the interface must not invent paper numbering.
- Symbols and formulas must be defined, exact, and rendered legibly. Vague
  prose must not stand in for available mathematics.
- During each proof segment, the current formal assertion, relevant
  hypotheses, and proof direction must remain visible or immediately adjacent
  to the changing figure.
- Roadmap steps must be mathematical sentences with a clear logical role.
  One-word labels or formula fragments are insufficient. An intuitive proof
  idea is acceptable only when labeled as such and followed by precise proof
  statements.
- Every auxiliary enumeration or calculation must be labeled by its actual
  mathematical role and explicitly connected to the result. Internal
  verification material or a vaguely motivated “audit” is not exposition.

Finally, look for genuine mathematical experimentation. When the subject
naturally permits object construction or meaningful free parameters, the page
should offer an open-ended playground rather than only canned examples,
sliders, or next/back controls. Check that it preserves or transparently snaps
the reader's input, gives immediate reasons tied to named hypotheses or lemmas,
handles invalid and degenerate states, supports reset or undo, and can display
the promised witness, construction, or algorithm when feasible. If no such
playground is present, decide whether the visible explanation for omitting it
is mathematically convincing.

Core mathematical content must be present in the normal reading path. Do not
give credit merely because the exact statement can eventually be found in a
modal, tooltip, optional tab, claim ledger, source file, or interaction state.
Controls and examples must be introduced by the exposition rather than left
for the reader to reverse engineer.

Pay particular attention to:

- missing hypotheses or domain restrictions;
- diagrams that work only because of accidental symmetry or a special
  coordinate choice;
- numerical tolerances, rounding, degeneracies, and invalid parameters;
- construction or algorithm steps whose displayed state does not match the
  described operation;
- examples presented in a way that could be mistaken for a proof;
- an interactive laboratory, gallery, or dashboard that appears before the
  reader has been told the problem and exact result;
- a central result stated only as a loose paraphrase or hidden in secondary UI;
- a proof reduced to examples, animations, or unexplained state changes;
- formal claims presented as unlabeled design cards, callouts, or explanatory
  copy so that theorem and commentary are indistinguishable;
- missing, invented, or mismatched source theorem and lemma numbers;
- a proof segment in which the reader cannot see what assertion or direction
  is currently being proved;
- a roadmap consisting of terse labels that do not communicate the actual
  intermediate assertions;
- auxiliary calculations whose role in the proof is not stated;
- a natural construction space reduced to a handful of canned examples without
  a justified reason;
- omitted definitions, proof steps, connective reasoning, or explanations of
  why displayed conditions matter;
- illustrations that show an object but do not explain the inference the
  reader should draw from it;
- misleading interpolation or animation;
- controls, presets, and edge cases that do not do what their labels promise;
- runtime errors, inaccessible states, unreadable layouts, and narrow-window
  behavior.

Rate `exposition_quality` as follows:

- `complete`: the page is a rigorous, digestible, self-contained exposition of
  its stated scope for professional mathematicians, with a visible problem,
  formally labeled exact result, source-aligned claims, explanatory
  progression, precise proof narrative, persistent proof context, meaningful
  experimentation where appropriate, and conclusion;
- `minor_gaps`: the full story is understandable, but a bounded explanation,
  transition, definition, or proof connection should be improved;
- `major_gaps`: substantial parts of the result or argument are missing,
  poorly connected, or left implicit, even if the examples themselves work;
- `not_self_contained`: a reader must consult the source to discover the
  central problem, result, or argument, or the page is essentially only a
  laboratory, figure, or example browser.

A missing or hidden central result statement, or the absence of a proof
narrative when the source supplies one, must yield `major_gaps` or
`not_self_contained` and must be recorded in `blocking_gaps`. Mathematical
incorrectness remains a fidelity failure as well. Do not lower an exposition
failure to a cosmetic or interaction issue.

Incorrect source numbering or a materially weakened formal statement is also
a fidelity defect. Unlabeled formal claims, a vague roadmap, missing current
proof context, an unexplained auxiliary section, or canned-only interaction
when a natural playground is feasible are exposition defects even when every
displayed calculation is correct.

Do not edit the visualization or any staged input. Write a detailed
`fidelity-critique.md` beginning with a Markdown heading. It must explain what
you checked, the result for every declared claim, the first-reader exposition
audit, all material mathematical, exposition, and interaction findings, and
any blocking gaps.

Write the structured review to `agent-result.json`. A visualization can be
`well_supported` and exposition-complete even though the underlying attempt is
explicitly partial, provided it clearly states the full problem, precisely
states the partial result, explains the available argument, and keeps its
boundary visible. Use `major_gaps` or `incorrect` whenever the visual
experience is materially misleading about the source mathematics.
