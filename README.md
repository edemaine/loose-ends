# Loose Ends

Tooling for studying open problems in research papers with large language models.

The Python code uses only the standard library. Downloading papers needs no
additional package; analyzing them requires the Codex CLI.

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
without downloading it again. A batch continues after an invalid ID or failed
download and exits with a failure status after reporting the final counts. When
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
example, `hep-th/9901001` is saved under `arXiv-hep-th_9901001/`). Every
directory starts with `arXiv-` so papers from future downloaders can be
distinguished by source.

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

A parent directory is searched recursively for downloaded `arXiv-*`
directories. Run several independent paper agents concurrently with `--jobs`:

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
adds an inheritable ACL entry for the invoking account so that Cygwin Python can
validate files created by the restricted Codex sandbox account. After Codex
finishes, it removes any explicit deny entry for that account and reapplies
recursive access before validating and cleaning the workspace. If older
installed outputs still carry those sandbox deny entries, triage
automatically repairs the selected papers' `analysis/` and `attempts/` trees
before reading them; this is a local ACL operation and starts no model turn.
If only temporary-workspace cleanup fails after installation, the run remains
successful and the path is recorded as a warning in `run.log`.
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
python src/triage_open_problems.py papers/edemaine \
  --explicitness explicit
```

One Codex turn triages all selected stale problems from the same paper. This is
substantially cheaper than starting one turn per problem. The Markdown and
compact structured record are stored at the paper root:

```text
arXiv-.../
├── analysis/
└── attempts/
    └── OP-001/
        ├── triage.md
        ├── triage.json
        ├── triage-manifest.json
        ├── triage-events.jsonl
        └── triage-run.log
```

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

## Attempt solutions

`src/solve_open_problems.py` runs one adaptive Codex research turn per selected
problem. Unlike triage, every solver receives a disposable copy of the entire
paper—PDF, submitted source, and metadata when present—together with the
technical analysis, all triage suggestions, and all previous attempts,
artifacts, and critiques. It is therefore not trying to solve a problem from
its short description alone.

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
python src/solve_open_problems.py papers/edemaine/arXiv-... \
  --all-problems
```

`--dry-run` shows one future adaptive attempt number for each selected problem
and the number of triage suggestions it will receive. All suggestions go into
the same solver prompt. The solver is explicitly free to combine, reorder,
abandon, or replace them as evidence develops, and to try proof,
counterexample, computation, and verification within the same turn. A later
attempt happens only after the current attempt (and any critic output)
invalidates triage and a new triage pass recommends more work.

Attempts are append-only:

```text
attempts/
└── OP-001/
    ├── triage.md
    └── attempt-001/
        ├── attempt.md
        ├── solver-result.json
        ├── manifest.json
        ├── events.jsonl
        ├── run.log
        └── artifacts/
```

`attempt.md` is the human-readable research record. The structured result
classifies the outcome and indexes exact `C-###` claims so a critic can check
them. Code, data, and auxiliary derivations can be retained under `artifacts/`.
If a completed Codex turn is preserved because driver validation or
installation fails, retrying the same solve command recovers the matching
`.solve-run-*` workspace before starting another model turn.

By default, newly installed attempts with at least one checkable claim are
passed to `review_solutions.py`; `no_checkable_progress` attempts do not spend a
critic turn. Control this with `--review promising`, `--review all`, or
`--review none`. Reviewer model flags inherit the solver flags unless
overridden:

```sh
python src/solve_open_problems.py papers/edemaine \
  --from-triage attempt \
  --model gpt-5.6-sol --reasoning-effort xhigh --fast \
  --review-model gpt-5.6-sol --review-reasoning-effort ultra
```

The solver prints a final list of reviews that merit medium or high human
attention, along with any unreviewed candidate solutions or counterexamples.

## Review attempts

Critics can also be run or rerun independently:

```sh
python src/review_solutions.py papers/edemaine --jobs 4
python src/review_solutions.py papers/edemaine/arXiv-... \
  --problem OP-001 --mode all
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

The structured review records a verdict, claim-by-claim assessments, blocking
gaps, and an attention level. The critic independently receives the full paper
context and does not modify the solver's `attempt.md`.

## Human review

`src/human_review.py` turns the critic's high- and medium-attention
recommendations into a local HTML dashboard:

```sh
python src/human_review.py papers/edemaine
```

The command writes `human-review.html` and opens it in a browser. Search and
attention filters narrow the queue; an always-visible problem list is grouped
under paper titles, with the selected problem's attempts listed below. Each
view starts with the paper title and open problem, followed by solver and
critic summaries, claim assessments, blocking gaps, and recommended next
steps. The solution summary and rendered `attempt.md` come first; tabs then
show the rendered `critique.md` and links to the paper analysis, structured
records, and artifacts. The browser loads Markdown-it and its KaTeX plugin to
render Markdown and math in one parsing pass, supporting `\(...\)`, `\[...\]`,
`$...$`, and `$$...$$` delimiters. High-attention items come first.

Use `--latest-per-problem` to suppress older selected attempts,
`--summary-only` for a compact index, `--attention high` to narrow the queue,
`--output FILE` to choose the dashboard location, or `--no-open` to avoid
launching the browser. `--terminal` retains the paged Markdown presentation.
The command only writes its HTML output and never starts a Codex agent.

All Codex-backed commands share the analyzer's `--model`,
`--reasoning-effort`, `--fast`, `--codex`, concurrency, Cygwin path
conversion, Windows ACL repair, and transient startup retry behavior.
Automated Codex runs ignore the invoking user's Codex configuration and
explicitly disable MCP/plugin app tools, web search, shell network access,
automatic MCP dependency installation, and nested agent spawning. Saved Codex
authentication is still used, as documented for `--ignore-user-config`. On
Windows, the launcher explicitly restores the elevated sandbox implementation
so `workspace-write` remains effective even though user configuration is
ignored.

## Development

Code lives in `src/` and tests live in `test/`. Run:

```sh
python -m unittest discover -s test -v
```

The download script batches arXiv metadata API lookups, then downloads content
from the PDF and source links. Author lookup uses the API's `au:` query field
and reuses the metadata returned by that search. For repository-scale
harvesting, use arXiv's bulk-data facilities instead.
