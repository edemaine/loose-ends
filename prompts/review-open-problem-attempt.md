# Independent mathematical review task

Critically review this solver attempt:

{{ATTEMPT_DIRECTORY}}

for open problem:

{{PROBLEM_ID}}

All available input is staged read-only under:

{{CONTEXT_DIRECTORY}}

Read the original paper and analysis, the target `attempt.md`, its structured
claims and artifacts, and relevant prior attempts. Independently verify the
argument against the original problem statement. Look especially for changed
quantifiers, missing hypotheses, circular dependence on the desired result,
unproved regularity or finiteness assumptions, invalid computations, and
uncleared state in reversible constructions.

Evaluate only mathematical correctness, the scope actually established, and
its importance relative to the stated open problem. Do not assess originality,
publication priority, or whether the result already appears in the literature.
A previously known proof and an apparently new proof receive the same
mathematical assessment. You may inspect an external source to verify a theorem
that the attempt invokes, but uncertainty about prior art must never lower
correctness, coverage, importance, verification confidence, or the derived
human priority. Literature provenance is handled by `literature_review.py`.

For every structured claim ID in the target attempt, return one claim review.
Try to falsify the claim before accepting it. Re-run small computations or
inspect artifacts when practical. Do not silently repair the solver's argument:
clearly separate what it actually establishes from a possible repair.

Classify the attempt on four independent axes:

- `correctness` concerns support for the mathematical argument: use
  `incorrect` for a decisive error, `major_gaps` when a central step is
  missing, `minor_gaps` for local plausibly repairable omissions, `plausible`
  when no error or specific gap is identified, and `well_supported` only when
  the key reasoning or computations were independently checked. Use
  `not_applicable` only when there is no mathematical result to assess.
- `reviewed_coverage` measures what fraction of the original open problem is
  actually addressed. `complete_under_stated_interpretation` is appropriate
  when the attempt resolves a precise reasonable interpretation but the source
  problem is materially ambiguous.
- `importance` measures mathematical consequence relative to the original
  problem. A correct but narrow lemma is `minor`; a useful special case or
  meaningful theorem is normally `moderate`; `major` is reserved for a result
  that substantially changes the state of the problem; and `resolution` means
  the original problem is fully answered under the assigned coverage.
- `verification_confidence` records how much independent checking the review
  managed to perform, not novelty confidence.

Write `critique.md` in the current working directory with:

- a concise reconstruction of the approach;
- claim-by-claim verification;
- decisive errors or missing steps;
- the exact portion of the open problem established;
- an explanation of the result's importance relative to the problem;
- concrete next steps for mathematical verification or repair.

Do not include a novelty or provenance assessment. Do not modify `inputs/`.
Your final response must be only the JSON object required by the supplied
schema.
