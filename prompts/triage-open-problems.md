# Open-problem triage task

Triage exactly these open-problem IDs:

{{PROBLEM_IDS}}

All available input is staged read-only under:

{{CONTEXT_DIRECTORY}}

Read `inputs/problems.json` and the paper's `inputs/analysis/` files. Prior
solver attempts and critiques, when present, are under
`inputs/history/OP-.../attempt-.../`. Use that history: distinguish an
unexplored direction from one that has repeatedly failed, and identify concrete
ways to improve on earlier attempts.

This is research triage, not a quota. Classify every requested problem:

- `attempt`: a Codex solver has a concrete, worthwhile line of attack now;
- `maybe`: progress seems possible, but the work is exploratory,
  underspecified, or likely needs a prerequisite;
- `skip`: the problem is currently a poor fit, too broad, requires unavailable
  evidence or experiments, is probably only an inferred limitation, or has
  already exhausted the plausible approaches in the supplied history.

Do not favor a fixed number of problems. It is valid to classify all, some, or
none as `attempt`.

For each problem, propose zero or more nonbinding `suggested_approaches`.
Identify distinct promising ideas, useful modes such as proof,
counterexample search, computation, or reformulation, why each idea is
promising, and an observation that should cause it to be abandoned or
reconsidered. Do not turn these ideas into a sequential plan or dependency
graph. A downstream solver receives all suggestions in one adaptive research
turn and may combine, reorder, abandon, or replace them after reading the full
paper and attempt history. `literature_check` may be suggested when present-day
status is especially important, but do not classify a problem as resolved from
model memory. A separate optional literature-search phase can verify current
status and prepare later work for the downstream solver.

Write one substantive Markdown report named `triage-OP-NNN.md` in the current
working directory for every requested ID. Each report should contain:

- classification and rationale;
- evidence from the paper analysis and prior attempts;
- obstacles and warning signs;
- the suggested approaches, why they may work, and when to abandon them;
- a short explanation if there are no suggested approaches.

Do not modify `inputs/`. Write the structured response to `agent-result.json`.
The `triages` array must contain every
requested problem exactly once and no other problem.
