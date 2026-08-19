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

Treat the target attempt as the proposed current snapshot of the entire
research stream, not merely as the newest increment. Reconcile it with every
relevant prior attempt and review. Determine which earlier results remain
supported, which have been strengthened or narrowed, and which are superseded
or refuted. Do not preserve a historical high-water mark when a later argument
invalidates it, and do not downgrade the cumulative assessment merely because
the newest valid increment is auxiliary while an earlier major result survives.

Evaluate only mathematical correctness, the scope actually established, and
its importance relative to the stated open problem. Do not assess originality,
publication priority, or whether the result already appears in the literature.
A previously known proof and an apparently new proof receive the same
mathematical assessment. You may inspect an external source to verify a theorem
that the attempt invokes, but uncertainty about prior art must never lower
correctness, coverage, importance, verification confidence, or the derived
human priority. Literature provenance is handled by `literature_review.py`.

The target attempt's complete claim-ID list is:

{{CLAIM_IDS}}

Return exactly one claim review for each ID in that list, in that order. Do not
invent IDs for external sources, auxiliary observations, or your own claims;
put such material in the prose critique or warnings instead. Try to falsify
each claim before accepting it. Re-run small computations or inspect artifacts
when practical. Do not silently repair the solver's argument: clearly separate
what it actually establishes from a possible repair.

Before reviewing the paper's analysis or provenance, read the target attempt's
`solver-result.json` and `attempt.md`. The `claim_reviews` entries assess those
current solver claims—not the open-problem extraction, analysis entries, or
paper results that happen to have other identifiers. Check the structured
`prior_claim_dispositions` against the historical attempts when that field is
present. Even for a legacy target without that field, perform the cumulative
reconciliation yourself.

All top-level review fields describe the cumulative mathematical state after
that reconciliation. They may improve or worsen relative to an earlier review.
Base them on surviving results, not on the importance of only the newest claims
and not on the best label ever assigned in the history.
`blocking_gaps` lists only gaps that block or limit the cumulative result now;
omit historical gaps that have been resolved. `recommended_next_steps`
describes the current research frontier. `warnings` records surviving caveats
and consequential retractions or scope changes.

Classify the attempt on four independent axes:

- `correctness` concerns support for the cumulative active mathematical
  argument: use `incorrect` for a decisive error, `major_gaps` when a central
  step is missing, `minor_gaps` for local plausibly repairable omissions, `plausible`
  when no error or specific gap is identified, and `well_supported` only when
  the key reasoning or computations were independently checked. Use
  `not_applicable` only when there is no mathematical result to assess.
- `reviewed_coverage` measures what fraction of the original open problem is
  currently addressed by all surviving results.
  `complete_under_stated_interpretation` is appropriate when the cumulative
  record resolves a precise reasonable interpretation but the source problem
  is materially ambiguous.
- `importance` measures the cumulative mathematical consequence of all
  surviving results relative to the original problem. A correct but narrow
  surviving result is `minor`; a useful special case or meaningful theorem is
  normally `moderate`; `major` is reserved for a surviving result that
  substantially changes the state of the problem; and `resolution` means the
  original problem is currently fully answered under the assigned coverage.
- `verification_confidence` records how much independent checking supports the
  cumulative assessment, not novelty confidence. Credit applicable earlier
  independent checks, but lower confidence when a material inherited claim was
  not adequately checked or no longer matches its reviewed formulation.

In every Markdown file, delimit all mathematical notation explicitly. Use
`\(...\)` for inline mathematics and `\[...\]` for display mathematics.

Write `critique.md` in the current working directory with:

- a concise reconstruction of the approach;
- a `History reconciliation` section describing the current cumulative result
  and every material retention, strengthening, narrowing, supersession, or
  refutation in the prior record;
- claim-by-claim verification;
- decisive errors or missing steps;
- the exact cumulative portion of the open problem currently established;
- an explanation of the surviving results' cumulative importance relative to
  the problem;
- concrete next steps for mathematical verification or repair.

Do not include a novelty or provenance assessment. Do not modify `inputs/`.
Write the structured response to `agent-result.json`.
