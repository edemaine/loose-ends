# Build an interactive mathematical visualization

Create a purpose-built interactive mathematical exposition for the selected
Loose Ends solver attempt. Read the staged paper, analysis, problem, attempt,
artifacts, and any independent critique under `inputs/` before designing it.

Treat the result as something you are explaining in a careful illustrated talk
or an interactive alternative presentation of the paper—not as a detached
demo, figure, dashboard, or collection of examples. A technically correct
interactive example is not enough. An unfamiliar mathematical reader should
be able to learn what was asked, what was established, why the hypotheses are
there, how the proof works, and where its limits are without first opening the
attempt or paper.

Be as mathematically precise and thorough as the source material needed for
the selected result, while making the progression more digestible through
visual explanation and interaction. The visual elements must actively explain
the mathematics. They must not merely decorate nearby prose.

## Audience and mathematical register

Write for a professional mathematician explaining the result to a fellow
mathematician in a precise research lecture or advanced course. Assume
mathematical maturity. Aim for clarity, not popularization: do not weaken a
statement, replace a quantifier by an example, suppress a needed hypothesis,
or use a friendly slogan where a definition or theorem is called for.

Maintain an explicit distinction between formal mathematics and intuition.
Use visible semantic labels such as **Definition**, **Theorem**, **Proposition**,
**Lemma**, **Corollary**, **Claim**, **Proof**, **Example**, **Counterexample**,
**Remark**, **Proof idea**, or **Computation**, as appropriate. State the formal
claim first; follow it with intuition, explanation, and visualization. Style
these layers distinctly enough that the reader always knows whether a passage
is an assertion, proof, heuristic, example, warning, or commentary.

Use standard mathematical notation and properly displayed formulas. Define
every symbol before or where it first appears. Do not rely on prose
approximations of a formula when the exact formula is available. Avoid
promotional, journalistic, or overly conversational copy; prefer the tone of
carefully edited lecture notes.

## Required explanatory outcome

Build a clear primary reading path through the page. It should work naturally
from top to bottom even before the reader discovers optional controls. You may
combine, reorder, or creatively present the material, but the experience must
fulfill all of these responsibilities:

1. **Orient the reader.** Start with the mathematical object, the original
   problem or question, the necessary definitions, and why the question is
   interesting. Pair this introduction with the smallest useful illustration.
2. **State the result.** Near the beginning, visibly label and state the
   precise **Theorem**, **Proposition**, **Partial result**, **Counterexample**,
   or other result being explained. Preserve its quantifiers, hypotheses,
   conclusion, scope, and status. If the attempt gives only a partial result,
   conditional result, obstruction, or counterexample, say so prominently and
   explain how it relates to the full problem. Do not hide the central
   statement in a modal, tooltip, tab, claim ledger, or final section. Do not
   substitute a collection of informal cards for one coherent formal
   statement.
3. **Build the hypotheses and mechanism.** Introduce important conditions,
   definitions, or ingredients in a pedagogically meaningful order. For each
   one, explain its formal meaning, show it visually when possible, and explain
   why it matters. Use a failure example or weakened-hypothesis comparison when
   that reveals necessity.
4. **Explain the proof or argument.** Give a precise proof idea or roadmap
   before detail, then walk through the substantive steps. A roadmap item must
   be a complete mathematical sentence describing an intermediate assertion
   and its logical role—not a one-word stage name or formula fragment. At each
   detailed step, state the general lemma, invariant, construction, or
   inference in words and mathematics; show what changes in the illustration;
   and explain why the step advances the proof. A running example should
   accompany the general argument, not replace it. If the source argument has
   a gap or external dependency, expose it exactly where it enters.
5. **Return to the result.** Conclude by reconnecting the proof steps to the
   precise statement, summarizing what is now understood, and making
   limitations, provenance, and unresolved parts easy to find.

The reader should never have to infer the theorem from examples, reverse
engineer the purpose of a control, or consult a hidden ledger to discover what
the page claims. An optional laboratory or free-exploration area is welcome,
but it comes after or alongside the explanatory spine and does not substitute
for it.

## Formal claims, source alignment, and proof context

- Preserve theorem, proposition, lemma, corollary, equation, section, and
  figure numbers from the source whenever they exist. Display the original
  designation with the restated claim so a reader can follow the paper and the
  visualization together. Never invent a paper number. For a solver claim
  without an original number, use its exact `C-###` identifier and a descriptive
  mathematical title.
- Restate every theorem or lemma precisely enough to be cited: include the
  domain, hypotheses, quantified variables, conclusion, and qualifications.
  A nearby plain-language interpretation may follow, clearly labeled as such.
- When decomposing an argument into new intermediate claims not explicitly
  named in the source, label them honestly as **Claim (proof decomposition)**
  or **Observation**, and identify which source proof passage they organize.
- Label proof directions such as **Necessity**, **Sufficiency**, induction
  steps, cases, and contrapositives explicitly.
- Throughout a proof walkthrough, keep the assertion currently being proved
  visible in the viewport or immediately adjacent to the changing figure.
  Include the relevant hypotheses and the current direction or subgoal. A
  reader who arrives midway through the proof must not have to scroll back to
  discover what “necessity” or the current construction refers to.
- A proof roadmap is an index to the argument, not a substitute for it. Give
  each roadmap step enough mathematical content to communicate the key
  assertion and implication. If a roadmap is deliberately only intuitive,
  label it **Proof idea** and follow it with the precise lemmas and proof.
- Every auxiliary section must announce its mathematical status and relevance.
  Introduce an enumeration, calculation, or computational check as a named
  lemma, corollary, example, or explicitly motivated computation, and state
  exactly how it is used later. Omit or relegate material that does not advance
  understanding of the selected result. Do not expose internal verification
  work under a vague reader-facing heading such as “useful audit.”

## Design freedom

Design the interface from scratch for this particular mathematical concept.
Do not force it into a predetermined mode. Useful ideas that may be combined
when appropriate include:

- exploring an object or family while varying parameters;
- playing a construction or constructive proof step by step;
- visualizing an algorithm and its changing state;
- following a proof through a carefully chosen running example;
- switching among canonical, generic, boundary, degenerate, and adversarial
  examples;
- showing invariants, auxiliary objects, witnesses, obstructions, and
  counterexamples to weakened hypotheses;
- comparing states before and after an operation.

Prefer one coherent explanatory experience over a checklist of features. If
the mathematics is not usefully visualizable in full, still state and explain
the full selected result and its proof architecture; focus the interactive
graphics on the portion they genuinely illuminate and state that boundary
precisely.

## Running examples and interaction

- Choose useful default examples so the initial page already tells a coherent
  story. Do not open on an unexplained control panel.
- When helpful, let the reader switch among canonical, generic, boundary,
  degenerate, and adversarial examples while staying at the same point in the
  exposition.
- Keep a selected running example coherent across definitions, construction or
  algorithm steps, proof steps, and the final result.
- Clearly distinguish the general mathematical object from the currently
  selected instance. State which features of the example are essential and
  which are incidental.
- Pair every important interaction with an explanation of what changed, what
  remained invariant, and what mathematical conclusion the reader should draw.
- Make progression, reset, and example-selection controls understandable
  without reading the source code. Ensure the app remains usable at narrow and
  wide window sizes and with keyboard input.

## Genuine mathematical experimentation

Whenever the subject reasonably admits it, include at least one open-ended
playground in which the reader constructs, edits, or chooses a mathematical
object rather than merely switching among canned examples or moving through a
fixed slideshow. Design it around the central definition or theorem.

A useful playground should:

- expose mathematically meaningful degrees of freedom through direct
  manipulation or precise parameter entry;
- constrain or snap input to the relevant mathematical domain when helpful,
  while making that constraint explicit rather than silently changing input;
- evaluate the named hypotheses, invariants, or failure conditions as the
  object changes and give immediate, mathematically specific feedback;
- explain *why* an invalid state fails, citing the relevant definition, lemma,
  or condition rather than returning only “good” or “bad”;
- let the reader request the theorem's witness, construction, algorithm, or
  proof transformation when the hypotheses hold, and display that process
  step by step when feasible;
- provide undo, reset, and instructive presets without limiting exploration to
  those presets.

For example, a geometric classification may invite the reader to draw a
polygon on the appropriate lattice, diagnose a forbidden angle or
self-intersection immediately, and then run the constructive gluing promised
by the theorem. This is an example of the principle, not a prescribed tool for
unrelated mathematics. If a genuine playground is not mathematically useful
or safely implementable for this result, say why in the visible limitations
and make the other interactions as probing as possible.

## Fidelity and interaction requirements

- Trace every substantive mathematical assertion in the interface to the
  attempt and its `C-###` claims. Use only claim IDs that exist in the staged
  `solver-result.json`.
- Put source claim IDs near the explanations they support, but use human-
  readable mathematical statements as the primary exposition. A claim ledger
  may supplement the page but cannot carry its essential content.
- Include meaningful labels, definitions, explanations, visible hypotheses,
  and the exact central result. Distinguish illustrative coordinates or finite
  examples from general mathematical conclusions.
- Verify every displayed source number and make formal statements,
  interpretations, proof ideas, and examples visually distinguishable.
- Include useful edge cases and failure cases when they clarify why a
  hypothesis or proof step matters.
- Avoid decorative animation that obscures mathematical state changes.
- Check numerical tolerances, degeneracies, invalid parameter combinations,
  and any randomized behavior. Prefer deterministic seeded examples.

Before finishing, read the application from its initial state, top to bottom,
as if the paper were unavailable. Confirm that it directly answers:

- What is the problem and what are the objects?
- What exactly is the result, including every important hypothesis?
- What is the main idea, and why should the result be true?
- Which numbered source theorem or lemma is being discussed, and which formal
  assertion is currently being proved?
- What are the proof's substantive steps, stated precisely, and how does each
  illustration explain rather than merely exemplify one of them?
- What does the running example demonstrate, and what does it not prove?
- What can the reader construct or vary freely, and does the resulting
  feedback name the exact mathematical reason for success or failure?
- Which parts are conditional, externally sourced, approximate, incomplete,
  or still open?

If the page cannot answer these questions in its visible reading path, it is
not complete.

## Application boundary

Write a self-contained static web application beneath `visualization/`. The
entry point must be `visualization/index.html`. You may create purpose-built
HTML, CSS, JavaScript, JSON, SVG, bitmap, font, text, WebAssembly, and other
static assets, and may bundle appropriate reusable library code locally. Use
only relative references to files that you create beneath `visualization/`.

The app runs in a sandboxed iframe with no network access, forms, parent-page
access, downloads, popups, workers, or persistent browser storage. Remote
network access is blocked. Relative package resources may be loaded normally,
including with `fetch` or module imports, but do not use remote URLs,
XMLHttpRequest, WebSocket, EventSource, service workers, `eval`, or `new
Function`. Do not require a build step or a server API at viewing time.

Do not modify `inputs/`.

## Required outputs

Create `visualization/verification.md`. It must document:

- which mathematical claims and source passages the interface represents;
- where the visible page states the problem, exact result, proof roadmap, proof
  steps, conclusion, and limitations;
- the inventory of formal claims displayed in the application, their source
  designations or `C-###` identifiers, and where interpretations are separated
  from formal statements;
- how the current proof goal and hypotheses remain visible during each proof
  interaction;
- how a first-time reader can follow the default top-to-bottom narrative;
- the examples, edge cases, and parameter regimes checked;
- how each important interaction was exercised;
- how the open-ended playground was tested, or why such a playground is not
  mathematically appropriate for this result;
- numerical approximations and tolerances;
- known representational limitations.

Run all useful deterministic checks available in the workspace and repair
problems before finishing.

Write the structured response to `agent-result.json`. List every application
file under `visualization/` in `files`, including the entry point and
`visualization/verification.md`. `claim_refs` must be the exact set of solver
claim IDs substantively represented. `concepts` is a free-form list of what
the interface helps explain, not a mode classification. Record every
remaining mathematical or interaction limitation honestly.
