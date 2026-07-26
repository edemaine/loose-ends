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

This is a research-planning task, not a quota. Classify every requested problem:

- `attempt`: a Codex solver has a concrete, worthwhile line of attack now;
- `maybe`: progress seems possible, but the proposed work is exploratory,
  underspecified, or likely needs a prerequisite;
- `skip`: the problem is currently a poor fit, too broad, requires unavailable
  evidence or experiments, is probably only an inferred limitation, or has
  already exhausted the plausible approaches in the supplied history.

Do not favor a fixed number of problems. It is valid to classify all, some, or
none as `attempt`.

For each problem, propose zero or more bounded `next_steps`. A step should be
specific enough to give directly to an independent solver. Include multiple
steps when proof and counterexample searches should be tried independently, or
when computation should inform a later proof. Use `relationship` to explain
whether steps are independent, alternatives, or sequential prerequisites. Use
`depends_on` for the IDs of steps that must finish first; independent first
steps have an empty array. Keep the dependency graph acyclic.
`literature_check` may be recommended, but the downstream solver will not have
internet access unless separately arranged.

Write one substantive Markdown report named `triage-OP-NNN.md` in the current
working directory for every requested ID. Each report should contain:

- classification and rationale;
- evidence from the paper analysis and prior attempts;
- obstacles and warning signs;
- the proposed steps, their relationships, and what would count as progress;
- a short explanation if there are no proposed steps.

Do not modify `inputs/`. Your final response must be only the JSON object
required by the supplied schema. The `triages` array must contain every
requested problem exactly once and no other problem.
