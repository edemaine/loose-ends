# Loose Ends

Loose Ends is a software tool to use Large Language Models (LLMs)
to solve open problems from existing research papers, and thereby
"tie up loose ends".

The goal is to discover which open problems are actually low-hanging fruit.
LLMs can read and attempt many more problems, much faster, than a human
researcher could, making it possible to search broadly for overlooked results.
The hope is that these inspire the humans to pose more interesting follow-up
questions, and focus on the harder problems where human insight is required.
Eventually, the idea is for humans and LLMs to work together on open problems.

## Overview

Loose Ends enables the following research workflow:

1. **Fetch papers.** Download PDFs, source, and metadata from arXiv, either by
   paper ID or author.
2. **Analyze papers.** Build a technical summary and extract the important
   results, techniques, and explicit or inferred open problems.
3. **Triage problems.** Decide which problems and approaches are promising
   enough to attempt now, which need exploration, and which should be skipped.
4. **Literature search.** Check whether subsequent work resolved or advanced
   each problem, and prepare relevant results and techniques for the solver.
5. **Attempt solutions.** Give selected problems to adaptive LLM research
   agents with the original paper, analysis, literature, prior attempts, and
   human directions.
6. **Review attempts.** Have independent critics assess correctness, coverage,
   importance, and remaining gaps, while preserving the full research history
   for later attempts and human inspection.
7. **Write and revise papers.** Turn selected results into traced, cited LaTeX
   manuscripts, compile them, critique them independently, and iterate toward
   expert human review.

This workflow can be followed through two interfaces:

* A local web app called the **workbench**.
  This is the main interactive interface: it organizes papers, open problems,
  solution attempts, reviews, and manuscripts; launches and manages these
  tasks; and shows their progress and results.
* A collection of composable **command-line tools**.
  The workbench uses these to do the actual work.
  You can also use them for scripting, batch processing, and direct control.

The core that makes this all possible is the
[Codex CLI](https://learn.chatgpt.com/docs/codex/cli).
You need this installed, along with an OpenAI account
(and ideally subscription).
Loose Ends calls Codex to do all the LLM work.

Loose Ends itself is written in Python, plus JavaScript in the web app.
The research CLI tools use only the Python standard library.
The workbench additionally uses `watchdog` for efficient filesystem updates.

## Prerequisites

To use Loose Ends, install the following prerequisites if you haven't already:

* [Python](https://www.python.org/)
* Python dependencies: `python -m pip install -r requirements.txt`
* [Codex CLI](https://learn.chatgpt.com/docs/codex/cli), and run
  `codex login` to authenticate your account
* LaTeX, such as [TeX Live](https://www.tug.org/texlive/), including `latexmk`,
  to write manuscripts

If you're using Windows + Cygwin, you will get better performance out of
Windows Python instead of Cygwin Python.  To do so, replace `python` with
`py -3` in all instructions.

## Quickstart

Start the workbench web server by running the following command
from the repository root:

```sh
python src/workbench.py papers
```

This command creates `papers/` if necessary, starts the workbench at
`http://localhost:35007/`, and opens it in your browser. Paper data is stored
under `papers/`, generated manuscripts under `manuscripts/`, and private task
state under `.loose-ends/`. If you prefer, you can make multiple `papers`
subdirectories like `papers/edemaine` and `papers/other` and start the
workbench with both (`python src/workbench.py papers/edemaine papers/other`).

Your web browser should automatically open the server at
`http://localhost:35007/`. Then follow this workflow:

1. **Add papers in Papers.** Switch to **Papers** and choose either:

   * **Add from arXiv**. You can enter one or more arXiv IDs or URLs;
     or change **Find papers by** to **Author name**, search, and
     select papers from the results.
   * **Add from files**. Supply local PDFs, source archives
     (`.zip` or `.tar.gz`), or paper directories.

   After the download or import finishes in **Activity**, return to **Papers**
   and select a new paper. ArXiv papers should already have their metadata
   (title and authors), but for local files you will need to supply it:
   use **Extract metadata** to automate or **Edit metadata** to do it manually.
2. **Analyze papers (extract open problems).**
   In **Papers**, select all papers by choosing **Select visible**
   (after optionally searching to filter down to a subset of papers),
   or select (click on) an individual paper,
   or select multiple papers via checkboxes.
   Then choose **Analyze**. This produces a technical summary and extracts the
   open problems from the papers. After the task finishes, switch from
   **Activity** to **Research** to browse the extracted problems.
3. **Triage the open problems.**
   In **Research**, select all problems by choosing **Select visible**
   (after optionally searching or otherwise filtering down to a subset of
   papers), or select papers via checkboxes, 
   and then choose **Triage** in the selection bar.
   Alternatively, select **Triage** from an individual problem in **Research**
   or **Triage problems** from an individual paper in **Papers**.
   Afterward, return to **Research** and use **Problem filters** dropdown
   (specifically the **Triage** field) to filter according to `attempt`,
   `maybe`, and `skip` recommendations.
4. **Search the literature.** This stage is optional but helps avoid solving a
   problem that later work has already resolved. In **Research**, select the
   promising problems and choose **Literature**, or open one problem and choose
   **Search literature**. After the task finishes, return to **Research** and
   open its **Literature** detail tab. The problem filters can narrow the list
   to current `attempt` or `maybe` triages before selecting a batch.
5. **Attempt and review solutions.** Select problems in **Research** and choose
   **Solve**. The default review policy sends promising attempts to an
   independent critic automatically. When the task finishes, return to
   **Research** and inspect the **Attempt**, **Critique**, and **Files** detail
   tabs; the attempt list in the sidebar retains earlier rounds. To handle
   attempts still awaiting a critic, filter for **Awaiting review**, select
   them, and choose **Review**.
6. **Write and revise a paper.** (This requires LaTeX including `latexmk`.)
   Select one or more results in **Research** and choose **Write paper**,
   or open an attempt and choose **Write this result**.
   You can request multiple automatic rounds of writing, reviewing, and
   revising.
   After the writing task finishes, switch to **Manuscripts** to open or
   download the PDF or source files, inspect the critic verdict, and
   choose **Revise** for another author-review round.

Every managed task has the same two-step launch process: configure its options,
review the exact commands and dry-run previews, and then start the runs. Once a
task starts, the workbench switches automatically to **Activity**, where you
can follow its status and console output; or use the back button to return.
Use **Activity** at any time to revisit task history, logs, failures, and output
paths. The **Workers** control in the top bar sets how many queued CLI runs may
execute concurrently.

## Download arXiv papers

`src/download_arxiv.py` downloads both the rendered PDF and the authors' submitted
source package. It accepts one or many IDs or URLs:

```sh
python src/download_arxiv.py 1706.03762
python src/download_arxiv.py https://arxiv.org/abs/1706.03762v7
python src/download_arxiv.py 1706.03762 2401.12345 hep-th/9901001 --output-dir papers
```

The first example creates:

```text
arXiv-1706.03762/
├── paper.pdf
├── metadata.json
└── source/
    ├── main.tex
    └── ...
```

Source tarballs and zip files are safely extracted under `source/` and removed
after successful extraction. A non-archive source submission is placed in that
directory as-is. `metadata.json` records the resolved arXiv ID, title, ordered
authors, and publication/update timestamps from the arXiv API. Existing
downloads are left untouched; pass `--force` to replace them. If an earlier run
left a source archive beside `paper.pdf`, the next run extracts and removes it
without downloading it again. `--dry-run` inspects the output directory and
distinguishes new or partial downloads from papers whose PDF and source (or
PDF-only marker) are already present and would be skipped, then summarizes both
counts. A batch continues
after an invalid ID or failed download and exits with a failure status after
reporting the final counts. When
anything fails, the last output line lists the failed IDs separated by spaces
so they can be pasted into a retry command:

```text
Failed IDs: 1706.03762 2401.12345v2
```

If an explicitly requested version is unavailable—for example, because the
latest version is a withdrawal—the downloader tries earlier versions in order.
The resolved version is used in the directory name and reported in the summary:

```text
Version fallback IDs: 2505.07147v2->2505.07147v1
```

PDF-only submissions are successful downloads rather than failures. They contain
`paper.pdf` and a `PDF_ONLY` marker instead of `source/`. The summary reports
both their count and IDs:

```text
PDF-only papers: 1.
PDF-only IDs: 1201.1650v1
```

Both current IDs and pre-2007 IDs are accepted. Because legacy IDs contain a
slash, the slash is replaced with an underscore in the directory name (for
example, `hep-th/9901001` is saved under `arXiv-hep-th_9901001/`). ArXiv
directories start with `arXiv-`; papers acquired elsewhere may use any safe,
unique directory name.

Every network request—API, PDF, or source—is started at least three seconds
after the previous request. Already-downloaded files do not cause a delay.

The downloader can also be used as a Python module:

```python
from pathlib import Path

from src import download_arxiv

downloads, failures = download_arxiv.fetch_papers(
    ["1706.03762", "2401.12345"],
    Path("papers"),
)
```

## Download papers by author

Author search uses the arXiv metadata API and passes the resulting IDs and
metadata to the same `download_arxiv` module, avoiding a duplicate metadata
request.

Inspect the results before downloading:

```sh
python src/download_arxiv_author.py "Adrian Del Maestro" --list
python src/download_arxiv_author.py "Adrian Del Maestro" --list --limit 10
```

Download all matches, or just the first few:

```sh
python src/download_arxiv_author.py "Adrian Del Maestro" --output-dir papers
python src/download_arxiv_author.py "Adrian Del Maestro" --limit 10 --output-dir papers
```

Name-based author matching is approximate. Common names can refer to multiple
people, and arXiv may match initials or alternate forms of a name. The listing
includes every paper's full author list so the result set can be checked before
downloading.

## Ingest arbitrary papers

All acquisition paths meet at one source-neutral on-disk contract. An installed
paper is any directory containing `paper.pdf` and/or `source/`:

```text
papers/<collection>/<stable-name>/
├── paper.pdf              # recommended; rendered paper
├── metadata.json          # recommended; title/authors before analysis
└── source/                # optional TeX, figures, data, or other source files
```

Use `src/ingest_paper.py` for a publisher PDF, scan, proceedings copy, private
draft, or any other local paper. It validates the PDF, copies inputs through a
temporary directory, writes normalized metadata and provenance, and refuses to
overwrite an existing paper:

```sh
python src/ingest_paper.py ~/Downloads/folding-paper.pdf \
  --output-dir papers/edemaine \
  --source ~/src/folding-paper
```

For the common metadata-later workflow, pass any number of PDF files, ZIP or
tar.gz archives, and/or source directories. Each input becomes one paper with
blank title and authors:

```sh
python src/ingest_paper.py ~/Downloads/first.pdf ~/Downloads/second.zip \
  ~/src/third-paper --output-dir papers/edemaine
```

A PDF input is installed under a name derived from its filename. A directory
input is copied wholesale under `source/` and uses its directory name. ZIP,
`.tar.gz`, and `.tgz` inputs behave like directories and use the archive name;
the maximal directory prefix shared by all archived files is discarded first.
Archive extraction rejects absolute and `..` paths, backslash paths, links,
special files, duplicate paths, excessive entry counts, and excessive expanded
size. The compiled PDF is selected by looking for root `paper.pdf`, then root
`main.pdf`, then exactly one root PDF whose filename stem matches a root `.tex`
file. Nested files are never candidates. No match or multiple matches is an
error; the importer does not guess. Metadata, `--name`, and an explicit
`--source` remain available in the detailed single-PDF mode shown below.

Title and author flags are optional. When omitted, the paper is installed
immediately with blank `title` and `authors` fields. In the workbench Papers
tab, select the new paper and choose **Extract metadata** to run a separate,
managed Codex task. The extractor reads `paper.pdf` plus `source/` when present,
returns schema-constrained title/authors/dates, and preserves local provenance
and source-specific identifiers. Its dry run, logs, retry behavior, model
settings, and optional extra prompt are visible like any other managed task.

For metadata that needs correction—or when no model call is desirable—choose
**Edit metadata** in the Papers tab. The editor updates title, ordered authors,
publication/update dates, arXiv ID, canonical URL, and DOI while preserving
unknown fields such as `provenance`.

Metadata can still be supplied at ingestion time:

```sh
python src/ingest_paper.py ~/Downloads/folding-paper.pdf \
  --output-dir papers/edemaine \
  --name folding-paper-2025 \
  --title "A Folding Paper" \
  --author "Ada Lovelace" \
  --author "Alan Turing" \
  --published 2025-06-01 \
  --url https://example.org/folding-paper \
  --doi 10.1234/example.42 \
  --source ~/src/folding-paper
```

`--name` defaults to the PDF filename stem. `--source` may be one file or a
directory; omit it for a PDF-only paper. Use `--dry-run` to inspect the target
and normalized metadata without writing. Once installed below a paper root,
the live workbench discovers the paper automatically and offers the same
analysis and downstream actions as it does for an arXiv paper.

For a hand-built or programmatic importer, create the same directory directly.
`metadata.json` uses this minimal interoperable shape:

```json
{
  "schema_version": 1,
  "title": "A Folding Paper",
  "authors": ["Ada Lovelace", "Alan Turing"],
  "published": "2025-06-01",
  "updated": "2025-06-01"
}
```

`title` and ordered `authors` are strongly recommended so the paper is useful
in the catalog before analysis. `published` and `updated` are optional ISO 8601
dates or timestamps. Importers may add source-specific identifiers and
provenance (for example `arxiv_id`, `doi`, `url`, or `provenance`); consumers
ignore unknown fields. Directory names do not encode semantics and need only be
unique within their collection.

## Analyze papers with Codex

`src/analyze_papers.py` starts one non-interactive Codex CLI agent per paper.
Each agent reads the rendered PDF and submitted source, then writes three
human-readable artifacts:

```text
arXiv-.../
├── paper.pdf
├── metadata.json
├── source/
└── analysis/
    ├── summary.md
    ├── results.md
    ├── open-problems.md
    ├── manifest.json
    ├── events.jsonl
    └── run.log
```

`summary.md` gives a technical orientation, `results.md` catalogs the important
theorems, lemmas, and techniques, and `open-problems.md` records explicit and
carefully labeled inferred problems. The compact `manifest.json` contains
provenance, the ordered paper-author list, and a machine-readable index of the
`OP-###` entries. The JSONL event stream and stderr log are retained for
diagnostics.

Install and authenticate the Codex CLI first, then analyze one paper:

```sh
codex login
python src/analyze_papers.py papers/edemaine/arXiv-0705.4085v1
```

A parent directory is searched recursively for installed paper directories.
Run several independent paper agents concurrently with `--jobs`:

```sh
python src/analyze_papers.py papers/edemaine --jobs 4
```

The default is one agent at a time. Start with modest concurrency because every
job consumes Codex capacity independently. All Codex-backed scripts default to
`--model gpt-5.6-sol --reasoning-effort xhigh`; use either option to override
that choice, or `--force` to regenerate a current analysis.
Parallel jobs are started one second apart to avoid Windows CLI startup races;
after startup, their paper analyses run concurrently. Pre-thread Windows
path-startup failures are retried up to twice.

The runner also supports Cygwin Python. It keeps local filesystem operations in
Cygwin path form while converting `-C`, output, schema, and prompt paths to
Windows form before invoking the Windows-native Codex CLI. On Windows, it also
omits the per-user Microsoft Store command-alias directory from the Codex
child's `PATH`; dedicated sandbox users cannot execute aliases such as that
directory's `pwsh.exe`, while ordinary PowerShell installations elsewhere on
`PATH` remain available. It also
adds an inheritable ACL entry for the invoking account so that Cygwin Python can
validate files created by the restricted Codex sandbox account. After Codex
finishes, it removes any explicit deny entry for that account and reapplies
recursive access before validating and cleaning the workspace. If older
installed outputs still carry those sandbox deny entries, triage
automatically repairs the selected papers' `analysis/`, `OP-*`, and `.runs/`
trees before reading them; this is a local ACL operation and starts no model
turn.
If only temporary-workspace cleanup fails after installation, the run remains
successful and the path is recorded as a warning in `run.log`.

Every newly launched Codex job writes its structured output directly to
`agent-result.json`. The driver stages only that task's checker as
`validation/validate.py`, together with the small shared
`validation/common.py` and `validation/expectations.json`, which contains the
result schema and invocation-specific expected IDs or other dynamic facts.
The shared agent instructions live in `prompts/validate-output.md` and are
appended to every task prompt at runtime, including custom prompt templates.
Before finishing, the agent runs:

```sh
python -m validation.validate
```

The host reruns the same task-specific `validate()` function with its
authoritative in-memory expectations before installing anything. If that check
finds only repairable contract errors, the driver allows one additional Codex
turn in the preserved workspace with the exact diagnostics. New runs require
canonical IDs such as `C-001` and canonical structured artifact paths such as
`artifacts/verifier.py`; permissive normalization remains limited to recovery
of older preserved workspaces. Changes to validation code participate in job
configuration digests, so cached output is not silently reused under a new
validation policy.

For example, the current frontier model with extra-high reasoning is:

```sh
python src/analyze_papers.py papers/edemaine \
  --model gpt-5.6-sol --reasoning-effort xhigh --fast
```

Reasoning effort is separate from the model ID. Supported levels include
`low`, `medium`, `high`, `xhigh`, `max`, and `ultra`; a selected model may
support only a subset. `--fast` requests Codex's faster service tier for these
runs without changing the global CLI configuration; Fast mode consumes credits
at a higher rate.

Codex writes in a temporary workspace beneath the paper's `analysis/`
directory. The runner stages disposable copies of `paper.pdf` and `source/`
inside that workspace so the Windows sandbox can read the local primary
sources while the originals remain protected. Generated files are validated
and installed only after a successful run; a failed workspace is preserved as
`analysis/.run-*` for inspection. A successful analysis is skipped when the
paper content, prompt, schema, and requested model have not changed. Each
analyzed, recovered, or current status line reports its result and open-problem
counts, and the final status line totals both catalogs across all successful
outcomes.

To install previously preserved runs that reached structured status
`complete`, without spending another model turn, use:

```sh
python src/analyze_papers.py papers/edemaine \
  --recover-complete --jobs 8
```

Recovery retains the original `.run-*` directory. Because old event logs did
not record the original model flags, the recovered manifest marks that
configuration as unknown; the recovered result is treated as current until an
explicit `--force` run replaces it.

## Triage open problems

`src/triage_open_problems.py` reads each paper's analysis and any previous
attempts and critiques. It assigns every selected problem one of three
classifications:

- `attempt`: there is a concrete, worthwhile Codex line of attack now;
- `maybe`: exploratory work or a prerequisite may be useful first;
- `skip`: the problem is currently a poor fit or the supplied history has
  exhausted the plausible approaches.

There is no per-paper quota. A triage can recommend zero or more distinct
approaches—for example, proof search, counterexample search, computation, or a
reformulation. These are advisory research ideas, not a sequence or dependency
graph.

Preview the stale work without spending credits, then run it:

```sh
python src/triage_open_problems.py papers/edemaine --dry-run
python src/triage_open_problems.py papers/edemaine --jobs 4
```

Narrow by problem ID or by explicitness:

```sh
python src/triage_open_problems.py papers/edemaine/arXiv-... \
  --problem OP-001 --problem OP-004
python src/triage_open_problems.py \
  papers/edemaine/arXiv-.../OP-00{1,4}
python src/triage_open_problems.py papers/edemaine \
  --explicitness explicit
```

One Codex turn triages all selected stale problems from the same paper. This is
substantially cheaper than starting one turn per problem. The Markdown and
compact structured record are stored at the paper root:

```text
arXiv-.../
├── analysis/
└── OP-001/
    ├── triage.md
    ├── triage.json
    ├── triage-manifest.json
    ├── triage-events.jsonl
    └── triage-run.log
```

Failed or interrupted paper-level triage and literature batch workspaces are
kept under the paper's hidden `.runs/` directory. Per-problem solver and critic
workspaces remain inside the corresponding `OP-*` directory.

A triage is current only while the paper analysis, problem record, prompt,
model settings, and complete attempt/review history still match. Installing a
new attempt or critique therefore makes that problem's triage stale
automatically; the next triage pass can decide whether a different direction
is worthwhile. Triages from the previous step/dependency format are
deliberately stale and must be regenerated.

To triage and immediately feed the `attempt` recommendations into the solver,
use the composition shortcut:

```sh
python src/triage_open_problems.py papers/edemaine \
  --jobs 4 --solve attempt
```

Use `--solve attempt,maybe` to include exploratory recommendations, or
`--solve-review none` to suppress the downstream critics. The same model
options are used throughout this shortcut. Run the scripts separately when
different solver and reviewer models are desired.

## Search later literature

`src/literature_review.py` is the optional Internet-enabled phase
between triage and solving. By default it selects current `attempt` and `maybe`
triages, groups them by paper, and uses one Codex turn per paper to search all
selected problems together:

```sh
python src/literature_review.py papers/edemaine --dry-run
python src/literature_review.py papers/edemaine --jobs 4
```

Use `--from-triage attempt` to narrow the triage classes, or bypass triage with
an explicit selection:

```sh
python src/literature_review.py papers/edemaine/arXiv-... \
  --problem OP-002
python src/literature_review.py \
  papers/edemaine/arXiv-.../OP-002
python src/literature_review.py papers/edemaine/arXiv-... \
  --all-problems
python src/literature_review.py papers/edemaine --attempted
```

The per-paper run produces independent records under each problem:

```text
OP-001/
├── literature.md
├── literature.json
├── literature-manifest.json
├── literature-events.jsonl
└── literature-run.log
```

Each record distinguishes `resolved`, `partially_resolved`,
`no_resolution_found`, and `uncertain`. The driver accepts `resolved` only with
high confidence and an inspected primary source explicitly identified as the
resolution. `no_resolution_found` means only that this search found none; it
does not certify that the problem remains open. The report also ranks later
papers and summarizes their exact results, techniques, relevance, and
limitations as a self-contained solver briefing.

The literature agent receives all prior attempts, artifacts, and critiques, so
it can search terminology and leads discovered during earlier work. Literature
currentness remains independent of attempt history: otherwise a solver would
immediately stale the report it just used before its critic runs. Use
`--attempted --force` when a literature review should incorporate newer attempt
history. Live first-party Codex web search is the default; use
`--web-search indexed` or `--web-search disabled` to reduce or remove live
access.

If validation or installation preserves a completed `.literature-run-*`
workspace, the next matching command recovers it before launching another
Codex turn. `--force` intentionally bypasses both current results and recovery.

## Attempt solutions

`src/solve_open_problems.py` runs one or more adaptive Codex research turns per
selected problem. Unlike triage, every solver receives a disposable copy of
the entire paper—PDF, submitted source, and metadata when present—together with
the technical analysis, all triage suggestions, and all previous attempts,
artifacts, and critiques. A current literature report is also staged with its
residual problem, source links, and solver briefing. It is therefore not trying
to solve a problem from its short description alone.

Solve all fresh `attempt` recommendations:

```sh
python src/solve_open_problems.py papers/edemaine \
  --from-triage attempt --jobs 4
```

No solve-all behavior is implicit. `--from-triage` requires a current triage;
problems whose new history has made triage stale are skipped. To deliberately
try a specific problem without current triage, or every extracted problem, use
an explicit selector:

```sh
python src/solve_open_problems.py papers/edemaine/arXiv-... \
  --problem OP-003
python src/solve_open_problems.py \
  papers/edemaine/arXiv-.../OP-00{1,4}
python src/solve_open_problems.py papers/edemaine/arXiv-... \
  --all-problems
```

A problem whose current literature record says `resolved` is skipped even when
selected explicitly. Use `--include-literature-resolved` to reconstruct or
audit the published resolution deliberately. Partial resolutions are not
skipped: the solver receives the precise residual problem. Literature search
is otherwise optional, and problems with no report continue normally.

Add a human research direction with `--prompt TEXT`; it is appended to the
standard solver instructions and applies to every selected problem and every
round in that invocation. It does not replace the paper context, output
contract, or validation rules:

```sh
python src/solve_open_problems.py \
  papers/edemaine/arXiv-.../OP-001 \
  --prompt "Try small computational cases before choosing proof or counterexample"
```

Use `--review-prompt TEXT` for a direction to the composed critic.
`--prompt-template FILE` and `--review-prompt-template FILE` are the low-level
options for replacing the complete prompt templates.

The same `--prompt TEXT` versus `--prompt-template FILE` distinction is used by
`analyze_papers.py`, `triage_open_problems.py`, `literature_review.py`, and
`review_solutions.py`. Custom directions are included in currentness and
recovery digests, so changing a direction cannot silently reuse output from a
different instruction.

`--dry-run` shows one future adaptive attempt number for each selected problem
and the number of triage suggestions it will receive, plus the maximum total
number of attempts requested. All suggestions go into the same solver prompt.
The solver is explicitly free to combine, reorder, abandon, or replace them as
evidence develops, and to try proof, counterexample, computation, and
verification within the same turn.

`-r N` or `--max-rounds N` requests up to `N` attempts per problem in the same
invocation. Each active problem gets one attempt, then its new attempt is
reviewed before the next round begins. A problem leaves the active set when the
critic reports plausible or well-supported correctness, complete coverage, and
resolution-level importance. Solver failures also stop later rounds for that
problem, while other problems continue. Otherwise, the next solver sees the
new attempt and critique in its history and can repair it or change direction:

```sh
python src/solve_open_problems.py papers/edemaine \
  --from-triage attempt --max-rounds 3 --jobs 4
```

The original triage is selection and advisory guidance for the whole
invocation; it does not need to be regenerated between these internal rounds.
With `--review none`, there is no independent early-stop signal, so every
successfully continuing problem runs for all requested rounds.

Attempts are append-only:

```text
OP-001/
├── triage.md
└── attempt-001/
    ├── attempt.md
    ├── solver-result.json
    ├── manifest.json
    ├── events.jsonl
    ├── run.log
    └── artifacts/
```

`attempt.md` is the human-readable research record. Each new attempt is a
cumulative snapshot of the problem's research stream rather than a delta: it
reconciles the history, carries forward still-supported material results, and
may narrow, supersede, or refute earlier claims. The structured result records
the solver-owned cumulative `claimed_result_type` (`none`, `obstruction`,
`partial_result`, `solution`, or `counterexample`), indexes the active exact
`C-###` claims so a critic can check them, and records material historical
claim lineage in `prior_claim_dispositions`. Claim type is independent of
novelty: a reconstruction of published work is still mathematically a
`solution`. External sources on which the active snapshot relies are recorded,
while literature provenance is owned by `literature_review.py`. Code, data,
and auxiliary derivations can be retained under `artifacts/`.
If a completed Codex turn is preserved because driver validation or
installation fails, retrying the same solve command recovers the matching
`.solve-run-*` workspace before starting another model turn.

By default, newly installed attempts with at least one checkable claim are
passed to `review_solutions.py`; `none` attempts do not spend a
critic turn. Control this with `--review promising`, `--review all`, or
`--review none`. Reviewer model flags inherit the solver flags unless
overridden:

```sh
python src/solve_open_problems.py papers/edemaine \
  --from-triage attempt \
  --model gpt-5.6-sol --reasoning-effort xhigh --fast \
  --review-model gpt-5.6-sol --review-reasoning-effort ultra
```

The solver prints a final list of reviews with medium or high derived human
priority, along with any unreviewed solution or counterexample claims.
Solver and critic runs default to live first-party web search. Disable it with
`--web-search disabled`, and control a composed critic independently with
`--review-web-search disabled`.

## Review attempts

Critics can also be run or rerun independently:

```sh
python src/review_solutions.py papers/edemaine --jobs 4
python src/review_solutions.py papers/edemaine/arXiv-... \
  --problem OP-001 --mode all
python src/review_solutions.py \
  papers/edemaine/arXiv-.../OP-001 --mode all
python src/review_solutions.py \
  papers/edemaine/arXiv-.../OP-001/attempt-003
```

The default `promising` mode scans pending attempts but selects only those with
checkable progress. `--mode all` also reviews honest no-progress reports.
Current reviews are skipped unless the solver-owned attempt content, critic
prompt, schema, or model settings changed; use `--force` to replace one
deliberately.

Reviews add these files to the attempt directory:

```text
attempt-001/
├── critique.md
├── review-result.json
├── review-manifest.json
├── review-events.jsonl
└── review-run.log
```

The structured review independently records mathematical correctness, coverage
of the original problem, importance relative to that problem, verification
confidence, claim-by-claim assessments, and blocking gaps. Its top-level axes
assess the cumulative snapshot after reconciling the complete attempt history,
so they can improve or worsen when a later attempt strengthens or overturns an
earlier result. Human priority is derived deterministically from those axes
instead of being a free critic judgment. The critic does not assess novelty;
it may use live web search only to verify an external theorem invoked by the
attempt. Literature changes therefore do not invalidate mathematical reviews.

## Write a paper

`src/write_paper.py` composes one deliberate research-paper manuscript from
explicitly selected papers, open problems, or solver attempts. Explicit
selection is enough to request a manuscript: partial results, incomplete
coverage, review gaps, and literature concerns are reported as warnings rather
than selection failures.
The writer and independent paper critic receive those warnings and must reflect
every substantive surviving limitation in the manuscript. Structural input
errors, unsafe generated paths, invalid traceability, citation failures, and
LaTeX build failures remain hard errors.

The input directories are positional inputs to the same paper:

```sh
python src/write_paper.py \
  papers/edemaine/arXiv-.../OP-001/attempt-003 \
  --author "A. Author"

python src/write_paper.py \
  papers/edemaine/arXiv-.../OP-001/attempt-003 \
  papers/edemaine/arXiv-.../OP-004/attempt-002 \
  --name combined-result \
  --author "A. Author" --author "B. Author"
```

An analyzed paper directory selects the latest attempt for every open problem
that has at least one attempt:

```sh
python src/write_paper.py papers/edemaine/arXiv-2207.07229v1
```

An `OP-NNN` directory similarly selects that problem's latest attempt. Earlier
attempts are not separate manuscript results: they and their independent
reviews are staged as history beneath the latest result, so the writer can use
still-valid earlier constructions on which a later synthesis depends. Open
problems with no solver attempt produce a warning and remain open-problem
context rather than result inputs. Supplying an exact `attempt-NNN` path
continues to pin that attempt while staging only its earlier history.

The selector policy is stored in every draft. On revision, a paper selector
automatically picks up newly attempted problems and newer attempts, while an
`OP-NNN` selector follows newer attempts for that problem. Exact attempt
selectors remain pinned. Existing result IDs stay stable, and new problem
streams receive new IDs.

Add a human direction to either a first draft or a revision with `--prompt`:

```sh
python src/write_paper.py \
  papers/edemaine/arXiv-.../OP-001/attempt-003 \
  --prompt "Lead with the constructive interpretation of the result"
```

Without `--name`, a single-source manuscript is named from the paper directory
and sorted problem IDs, such as
`manuscripts/arXiv-1406.6576v2_OP-001_OP-004/`. Cross-paper names join those
components with `__`; unusually long names are truncated with a stable digest.
The script never infers authors from an originating paper and emits
`\author{}` when no `--author` is supplied. Use `--dry-run` to inspect the
selection, destination, and upstream warnings without starting Codex. The
legacy `--allow-not-ready` option is accepted as a no-op; this is now the
default behavior. Warnings do not guarantee that the critic will accept the
result or that the workflow will reach human review.

The writer receives every originating PDF and source tree, analysis, selected
attempt and artifacts, solution critique, and literature report. It writes a
self-contained LaTeX article with a short result-oriented title and abstract,
an introduction with `Related Work` and `Our Results`, detailed technical
sections and proofs, and a conclusion recording remaining open problems. This
outline is a guideline when the mathematics needs a different organization.
Every originating problem and borrowed result must be cited to a verified
source. Install `latexmk` as well as Codex: the agent is asked to compile its
draft, and the driver independently rebuilds it and rejects missing citations,
undefined references, unsafe output paths, or broken traceability.
The writer may create supporting files beneath `figures/`. SVG sources are
allowed when a same-stem PDF is also generated and listed (for example,
`figures/construction.svg` and `figures/construction.pdf`); the manuscript
includes the PDF. The writer can run command-line converters such as Inkscape
when they are installed, and the driver rejects an unpaired SVG.
On Windows, the driver can use the Cygwin `latexmk` installation through
`C:\cygwin64\bin\bash.exe` when no native executable is on `PATH`.

Every completed draft is independently reviewed. Ordinary major or minor
writing findings are passed into another complete writing round; mathematical
or novelty gaps stop with `needs_research`, an unsupported central result stops
with `invalid`, and `ready_for_expert_review` stops early for manual inspection.
`-r`/`--max-rounds` caps the number of new author-review rounds in one
invocation and defaults to one:

```sh
python src/write_paper.py \
  papers/edemaine/arXiv-.../OP-001/attempt-003 \
  -r 2
```

Drafts are append-only:

```text
manuscripts/arXiv-..._OP-001/
├── draft-001/
│   ├── main.tex
│   ├── references.bib
│   ├── main.pdf
│   ├── readiness.md
│   ├── paper-result.json
│   ├── manifest.json
│   ├── paper-critique.md
│   ├── paper-review.json
│   └── ... logs and optional figures ...
└── draft-002/
```

`readiness.md` maps manuscript theorems back to `R-###` manuscript inputs and
`C-###` solver claims. The critic's `paper-critique.md` and structured
`P-###` findings remain beside every draft. Each revision must account for all
findings and writes a new draft directory instead of overwriting its parent.
Continue a reviewed draft explicitly with:

```sh
python src/write_paper.py \
  --revise manuscripts/arXiv-..._OP-001/draft-001 \
  --max-rounds 2
```

Use `--refresh-results` to promote pinned selectors—including legacy drafts
created before selector policies were recorded—to their originating paper
scope. The revision then uses the latest attempt for every attempted problem
in those papers and includes newly attempted problems. A changed selection
starts with an author round before critique:

```sh
python src/write_paper.py \
  --revise manuscripts/arXiv-..._OP-001/draft-001 \
  --refresh-results
```

Combine it with `--dry-run` to inspect the refreshed result IDs and attempt
paths without starting Codex.

Add a human-directed revision goal with `--prompt`. This starts an author round
immediately, even if the selected draft has no paper review yet, and retains
the direction across all automatic revision rounds in that invocation:

```sh
python src/write_paper.py \
  --revise manuscripts/arXiv-..._OP-001/draft-001 \
  --prompt "Add figures illustrating the construction"
```

Use `--prompt-template FILE` to replace the complete low-level writer prompt
template. `--review-prompt TEXT` gives the paper critic an additional direction,
while `--review-prompt-template FILE` replaces its low-level template.

An interrupted review can be resumed through the same `--revise` command: the
script reviews the installed unreviewed draft before deciding whether another
writing round is appropriate. A successful final verdict is only readiness for
human expert review, never an assertion that the paper is publication-ready.

## Human review

`src/human_review.py` turns the extracted open problems, solver attempts, and
critic reviews into a local HTML dashboard:

```sh
python src/human_review.py papers/edemaine
```

The command writes `human-review.html` and opens it in a browser. Every
extracted problem appears, including problems with no solver attempt and
attempts still awaiting a critic. The Attempt status filter switches among
unattempted, attempted/unreviewed, and reviewed problems; all three are shown
by default. Unattempted views still include the full problem statement,
triage and literature context, paper analysis links, and relevant files.

Independent filters also cover solver claim type, critic correctness, reviewed
coverage, importance, verification confidence, derived human priority,
literature status, and current versus stale/legacy review state. They can, for
example, show well-supported solution claims of major importance while
excluding problems already resolved in the literature. Literature filters distinguish
full and partial resolutions, no resolution found, uncertainty, and missing
literature review. Current and stale review toggles are both enabled by default;
stale and legacy assessments are visibly labeled. An always-visible
problem list is grouped under
paper titles, with the selected problem's attempts listed below. Each view
starts with the paper title and full extracted open-problem statement, followed
by Markdown-rendered literature, solver, and critic summaries, claim
assessments, blocking gaps, and recommended next steps. The solution summary
and rendered `attempt.md` come first; tabs then show the rendered `critique.md`,
the complete literature report when available, and links to the paper analysis,
structured records, and artifacts. The browser loads
Markdown-it and its KaTeX plugin to render Markdown and math in one parsing
pass, supporting `\(...\)`, `\[...\]`, `$...$`, and `$$...$$` delimiters.
Navigation is encoded in the URL: `q` stores search, review filters use named
parameters such as `status`, `claim`, `priority`, and `freshness`, and the
selected review uses the stable `paper`, `problem`, and optional `attempt`
identity. `detail` selects the evidence tab. URLs therefore survive catalog
reordering and reloads, while browser Back and Forward restore selection,
filters, tabs, and scroll position. Old positional `#review-N` links are still
accepted and are replaced with the stable form when opened.
Papers are alphabetical by title, problems are numeric by ID, and attempts are
newest first. Each problem card shows its attempt status and total number of
attempts. Reviewed cards additionally show claim type, correctness, coverage,
and importance; unreviewed attempts show their solver claim type. A `known`
tag marks problems whose current literature review reports a full resolution.

Use `--latest-per-problem` to suppress older selected attempts,
`--summary-only` for a compact index, `--priority high` to narrow the queue,
`--current-only` to omit stale reviews before building the dashboard,
`--output FILE` to choose the dashboard location, or `--no-open` to avoid
launching the browser. `--terminal` retains the paged Markdown presentation.
The command only writes its HTML output and never starts a Codex agent.

## Live research workbench

`src/workbench.py` serves the human-review data as a live local dashboard and
manages persistent CLI tasks. Install its filesystem-watcher dependency, then
start it on one or more paper roots:

```sh
python -m pip install -r requirements.txt
python src/workbench.py papers/edemaine
```

The workbench opens at `http://localhost:35007/` by default. It watches paper
and manuscript files with native `watchdog` events, refreshes the selected view
after external or managed changes, and provides actions for analysis, triage,
literature search, solving, independent review, paper writing, and revision.
Initial discovery runs in the background with live phase and row progress.
Manuscript details link back to the source paper and open-problem titles recorded
by their input selectors and attempts.
Each paper root caches its own completed paper/review inventory in
`.loose-ends/workbench-papers.json`; the manuscript root similarly uses
`.loose-ends/workbench-manuscripts.json`. After the first successful scan, a
server restart renders the merged cached catalog immediately and validates
lightweight filesystem signatures in the background. Only new or changed
roots are rebuilt, so adding an empty root does not rescan existing roots.
Cache directories are ignored by both Git and the filesystem watcher. Changes
to catalog code, prompts, or schemas invalidate the affected caches, and
unchanged freshness digests are reused within the running server.

The Papers view has **Add from arXiv** and **Add from files** actions. The file
importer accepts multiple dragged PDF files, ZIP/tar.gz archives, and/or folders, shows the selected
inputs and destination paper collection, and uses the same compiled-PDF rules
as `src/ingest_paper.py`. Imported papers are installed with blank metadata,
then selected in the Papers view so **Extract metadata** can populate them.

For arXiv, enter IDs or arXiv URLs (one
per line), or search by author with a result limit. Author search runs
immediately and presents checked-by-default results so unwanted papers can be
deselected. The final selection is converted to concrete arXiv IDs before the
normal two-step task confirmation. Choose one of the configured paper roots,
review the exact downloader command and dry-run output, and start it as a
persistent managed task. Its logs, retry controls, and scheduler behavior match
the analysis tasks. For local files, the browser importer or
`src/ingest_paper.py` installs the same documented directory contract; the
filesystem watcher adds the paper to the catalog without a workbench restart.
Blank title/author metadata
can then be populated with the managed **Extract metadata** action, and all
paper metadata can be corrected directly with **Edit metadata**.
This editor includes the arXiv ID and canonical URL as separate fields, so
arXiv provenance can be corrected without preventing a journal or project URL.
After a paper is analyzed, **Add open problem** accepts a title and a
Markdown/LaTeX statement. Manually entered problems default to `additional`,
distinguishing them from problems explicitly stated in or inferred from the
paper, and are included in the normal triage, literature, and solving flows.

The four top-level views have reloadable paths: `/research`, `/papers`,
`/manuscripts`, and `/activity`. Their query strings capture the complete
navigation state: `q` for search; the same review/filter parameters as the
human-review dashboard; `paper` for a source paper; `manuscript` plus optional
`draft` for a manuscript; and `job` for managed task output. Normal navigation
creates history entries, while search and filter edits replace the current
entry, so Back and Forward move between meaningful selections without replaying
each keystroke.

Every task has two launch steps. The first dialog collects optional human
directions, maximum rounds, review policy, model settings, authors, and other
task-specific choices. The second shows the exact targets, prompt messages,
commands, and replacement warnings. No CLI process starts until the final
confirmation.

Task intent, commands, status, output paths, and console logs are stored under
the ignored `.loose-ends/` directory. Detached workers continue if only the web
server restarts. After a machine restart, abandoned heartbeats are marked
interrupted and runs that installed no output can be retried. A run that
installed output before a later phase failed is marked partial so the next
action can operate on that output without accidentally duplicating it.

Use the worker control in the top bar to change concurrent CLI invocations or
pause new starts. Task scheduling weights and per-task pause/resume controls are
available when configuring or viewing a task. Weights are displayed as ⅛×
through 8× (with 1× as the default), and eligible tasks receive worker starts
in roughly those proportions. Pausing or lowering the limit never stops an
already active run. Use `--no-open` to
avoid opening a browser, `--port` to select another local port, or
`--state-dir` to place the private task database and logs elsewhere. The
default binding is local-only. To use the workbench from another machine on a
trusted network, pass `--host 0.0.0.0` and browse to this machine's IP address.
IP-address access and the machine's own hostnames are accepted; use repeatable
`--allowed-host NAME` for an additional trusted DNS name. Network clients can
view project data and launch CLI tasks, so do not expose the port to an
untrusted network or the public internet.

All Codex-backed commands share the analyzer's `--model`,
`--reasoning-effort`, `--fast`, `--codex`, Cygwin path conversion, Windows ACL
repair, and transient startup retry behavior. Batch commands also expose
concurrency controls.
Automated Codex runs ignore the invoking user's Codex configuration and
explicitly disable MCP/plugin app tools, shell network access, automatic MCP
dependency installation, and nested agent spawning. Paper analysis and triage
also disable web search; literature, solver, paper writer, and critic runs
default to live first-party web search, independently of shell network access.
Saved Codex authentication is still used, as documented for
`--ignore-user-config`. On Windows, the launcher explicitly restores the
elevated sandbox implementation so `workspace-write` remains effective even
though user configuration is ignored.

## Development

Code lives in `src/` and tests live in `test/`. Run:

```sh
python -m unittest discover -s test -v
```

The download script batches arXiv metadata API lookups, then downloads content
from the PDF and source links. Author lookup uses the API's `au:` query field
and reuses the metadata returned by that search. For repository-scale
harvesting, use arXiv's bulk-data facilities instead.
