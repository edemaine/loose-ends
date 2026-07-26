# Open-problem solving task

Work on open problem:

{{PROBLEM_ID}}

using this planned step:

{{STEP_JSON}}

All available input is staged read-only under:

{{CONTEXT_DIRECTORY}}

Read the entire paper context, not merely the short problem description:

- `inputs/paper/` contains the PDF, submitted source, and metadata when
  available;
- `inputs/analysis/` contains the technical summary, result catalog, and open
  problems;
- `inputs/history/` contains prior attempts, their artifacts, and critiques;
- `inputs/triage/` contains the latest triage snapshot when one exists.

Use the paper's definitions, machinery, related results, and proof techniques.
Check whether a proposed claim really addresses the original quantifiers and
hypotheses. Prior attempts are evidence, not truth: repair their gaps when
possible and do not simply repeat them.

Pursue the planned step seriously. You may prove a special case, find an
obstruction, construct a candidate counterexample, perform a computation,
derive a useful reformulation, or explain precisely why the approach fails.
Never present intuition or an unchecked experiment as a proof. Make progress
auditable by stating exact claims and supplying derivations, verification
methods, or reproducible code/data.

Write `attempt.md` in the current working directory. It must include:

- the original problem and the assigned step;
- definitions and paper results actually used;
- a detailed derivation or experiment;
- explicit checks for hidden assumptions and edge cases;
- a section titled `Checkable claims`, using IDs `C-001`, `C-002`, ... that
  exactly match the structured response;
- remaining gaps and an honest bottom line.

Put useful code, data, or auxiliary derivations under `artifacts/`, and list
only those `artifacts/...` relative paths in the structured response. Do not
list the required `attempt.md` as an artifact. Do not modify `inputs/`.

Use `no_checkable_progress` when there is nothing concrete for a critic to
verify. A failed approach can still be `useful_negative_result` if it establishes
a specific obstruction or eliminates a well-defined strategy. Reserve
`candidate_solution` and `candidate_counterexample` for complete-looking
arguments that still require independent review.

Your final response must be only the JSON object required by the supplied
schema.
