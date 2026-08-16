# Paper analysis task

Analyze the research paper in:

{{PAPER_DIRECTORY}}

The submitted source is normally under `source/`, and the rendered paper is
normally `paper.pdf`. Treat both as read-only primary sources. Prefer the TeX
source for exact structure, equations, labels, and line-level locations, while
using the PDF for theorem numbering, pagination, rendered notation, or
figures. In some cases, you may have only PDF and no source.

Do not research later literature or attempt to determine
whether a problem has been solved since publication. Ignore any existing
`analysis/` content when establishing claims about the paper; generated
artifacts from earlier runs are not primary evidence.

Write exactly these three Markdown files in the current working directory:

1. `summary.md`
2. `results.md`
3. `open-problems.md`

In every Markdown file, delimit all mathematical notation explicitly. Use
`\(...\)` for inline mathematics and `\[...\]` for display mathematics.

Do not modify the paper directory or its source files.

## `summary.md`

Give a self-contained technical orientation to the paper:

- Title, authors, and the paper's central thesis.
- Necessary background, definitions, models, and assumptions.
- Main contributions and how they fit together.
- A concise section-by-section roadmap.
- Scope limitations that matter when interpreting the results.

Keep this readable as an entry point for a technically sophisticated researcher.
Refer to stable result IDs from `results.md` where useful instead of duplicating
the detailed result catalog.

## `results.md`

Catalog the paper's mathematical or technical results and the machinery behind
them:

- Assign stable IDs `R-001`, `R-002`, and so on.
- Include the main theorems and important supporting theorems, lemmas,
  propositions, corollaries, algorithms, constructions, or lower bounds.
- For each result, record its original number or label, a faithful statement,
  its role in the paper, important dependencies, proof techniques, assumptions,
  limitations, and a precise source location.
- Distinguish a theorem proved in this paper from a previously known result that
  the paper only cites or restates.
- Include a separate synthesis of the recurring proof techniques and explain
  which result IDs use each technique.

Do not pad the catalog with routine algebraic steps. Include supporting lemmas
when they carry a reusable idea, expose a barrier, or are needed to understand a
main result.

## `open-problems.md`

Extract the paper's unresolved questions:

- Assign stable IDs `OP-001`, `OP-002`, and so on.
- Start with problems explicitly posed by the authors.
- Put implied but unstated research directions in a separate section
  and label them `inferred`.
- Do not silently turn every limitation or future-work sentence into an open
  problem.
- For each entry, give a precise statement, explicitness (`explicit`,
  `inferred`, or `uncertain`), source location, surrounding context, related
  `R-###` results, potentially relevant techniques from `results.md`, known
  barriers identified by the paper, and any ambiguity in your interpretation.
- Preserve important quantifiers, hypotheses, target bounds, and definitions.

If no open problems can be supported from the paper, say so clearly and explain
what locations you checked. Do not invent a problem merely to populate the
file.

## Evidence and locations

Every substantive entry in `results.md` and `open-problems.md` must point back
to the paper. Prefer combinations such as section plus theorem number, page
number, and `source/relative-file.tex:line`. Paraphrase faithfully and keep
short quotations only when the authors' exact wording is important.

Before finishing, cross-check all three files against one another:

- Every referenced `R-###` ID exists.
- Every `OP-###` ID is unique.
- `agent-result.json` lists exactly the open problems present in
  `open-problems.md`.
- Inferences are clearly separated from author-stated claims.

Write the structured response to `agent-result.json`. The substantive analysis
belongs in the three Markdown files.
