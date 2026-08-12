#!/usr/bin/env python3
"""Present solver attempts selected for human review."""

from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from functools import lru_cache
import json
import os
from pathlib import Path, PureWindowsPath
import pydoc
import re
import subprocess
import sys
from typing import Callable, Iterable, Sequence
import webbrowser

import codex_cli
import open_problem_common as common
import review_solutions


PROJECT_ROOT = Path(__file__).resolve().parents[1]
REVIEW_MODEL_PATH = (
    Path(__file__).resolve().parent / "workbench_web" / "review_model.js"
)
REVIEW_TOKENS_PATH = (
    Path(__file__).resolve().parent / "workbench_web" / "review_tokens.css"
)
DEFAULT_PRIORITY = "high,medium"
PRIORITY_RANK = {
    "high": 0,
    "medium": 1,
    "low": 2,
    "none": 3,
}
CATALOG_IO_WORKERS = 4


@dataclass(frozen=True)
class HumanReviewItem:
    attempt: review_solutions.AttemptRef
    review_result: dict
    current: bool

    @property
    def priority(self) -> str:
        value = self.review_result.get("human_priority")
        if value in review_solutions.HUMAN_PRIORITY_LEVELS:
            return value
        return self.review_result["attention"]


def _attempt_number(attempt: review_solutions.AttemptRef) -> int:
    match = common.ATTEMPT_DIRECTORY_RE.fullmatch(attempt.name)
    return int(match.group(1)) if match else -1


def discover_human_reviews(
    problems: Iterable[common.ProblemRef],
    *,
    priority: set[str],
    attempt_names: set[str] | None = None,
    include_stale: bool = True,
    latest_per_problem: bool = False,
    allow_empty: bool = False,
    progress: Callable[[int, int], None] | None = None,
) -> list[HumanReviewItem]:
    """Find reviewed attempts selected for human inspection."""
    attempts: list[review_solutions.AttemptRef] = []
    for problem in problems:
        for directory in common.attempt_directories(problem):
            if (
                attempt_names is not None
                and directory.name not in attempt_names
            ):
                continue
            result = common.read_json(
                directory / "solver-result.json",
                description=(
                    f"solver result for {problem.id}/{directory.name}"
                ),
            )
            attempts.append(
                review_solutions.AttemptRef(problem, directory, result)
            )
    candidates: list[tuple[review_solutions.AttemptRef, dict]] = []
    for attempt in attempts:
        result_path = attempt.directory / "review-result.json"
        result = common.load_json(result_path)
        if result is None:
            continue
        level = result.get("human_priority", result.get("attention"))
        if level not in review_solutions.HUMAN_PRIORITY_LEVELS:
            raise common.CodexError(
                f"review has invalid human priority: {result_path}"
            )
        if level not in priority:
            continue
        candidates.append((attempt, result))

    def currentness(
        candidate: tuple[review_solutions.AttemptRef, dict],
    ) -> bool:
        attempt, result = candidate
        return review_solutions.review_is_current(attempt, result=result)

    if progress is not None:
        progress(0, len(candidates))
    current_values = []
    if len(candidates) > 1:
        with ThreadPoolExecutor(max_workers=CATALOG_IO_WORKERS) as executor:
            values = executor.map(currentness, candidates)
            for index, current in enumerate(values, 1):
                current_values.append(current)
                if progress is not None:
                    progress(index, len(candidates))
    else:
        for index, candidate in enumerate(candidates, 1):
            current_values.append(currentness(candidate))
            if progress is not None:
                progress(index, len(candidates))

    items: list[HumanReviewItem] = []
    for (attempt, result), current in zip(candidates, current_values):
        if not current and not include_stale:
            pass
        else:
            items.append(HumanReviewItem(attempt, result, current))

    if latest_per_problem:
        latest: dict[tuple[Path, str], HumanReviewItem] = {}
        for item in items:
            key = (
                item.attempt.problem.paper_directory,
                item.attempt.problem.id,
            )
            previous = latest.get(key)
            if (
                previous is None
                or _attempt_number(item.attempt)
                > _attempt_number(previous.attempt)
            ):
                latest[key] = item
        items = list(latest.values())

    items.sort(
        key=lambda item: (
            item.attempt.problem.paper_title.casefold(),
            os.path.normcase(
                str(item.attempt.problem.paper_directory)
            ),
            item.attempt.problem.id,
            -_attempt_number(item.attempt),
        )
    )
    if not items and not allow_empty:
        levels = ", ".join(
            sorted(priority, key=PRIORITY_RANK.__getitem__)
        )
        qualifier = "current " if not include_stale else ""
        raise common.CodexError(
            f"no matching {qualifier}reviews have priority {levels}"
        )
    return items


def _append_list(
    lines: list[str],
    heading: str,
    values: object,
) -> None:
    if not isinstance(values, list) or not values:
        return
    lines.extend((f"### {heading}", ""))
    lines.extend(f"- {value}" for value in values)
    lines.append("")


def _relevant_problem_paths(
    problem: common.ProblemRef,
    attempt: review_solutions.AttemptRef | None = None,
) -> list[Path]:
    paper = problem.paper_directory
    paths = [
        paper / "paper.pdf",
        paper / "analysis" / "summary.md",
        paper / "analysis" / "results.md",
        paper / "analysis" / "open-problems.md",
        problem.directory / common.TRIAGE_MARKDOWN,
        problem.directory / common.TRIAGE_RESULT,
        problem.directory / common.LITERATURE_MARKDOWN,
        problem.directory / common.LITERATURE_RESULT,
    ]
    if attempt is not None:
        paths.extend(
            (
                attempt.directory / "attempt.md",
                attempt.directory / "solver-result.json",
                attempt.directory / "critique.md",
                attempt.directory / "review-result.json",
            )
        )
        artifacts = attempt.directory / "artifacts"
        if artifacts.is_dir():
            paths.extend(
                sorted(
                    (
                        path
                        for path in artifacts.rglob("*")
                        if path.is_file()
                    ),
                    key=lambda path: path.as_posix(),
                )
            )
    return [path for path in paths if path.is_file()]


def _review_file_records(
    paper: Path,
    problem_directory: Path,
    attempt_directory: Path | None,
    *,
    include_browser_uris: bool,
) -> list[dict]:
    paths = [
        paper / "paper.pdf",
        paper / "analysis" / "summary.md",
        paper / "analysis" / "results.md",
        paper / "analysis" / "open-problems.md",
        problem_directory / common.TRIAGE_MARKDOWN,
        problem_directory / common.TRIAGE_RESULT,
        problem_directory / common.LITERATURE_MARKDOWN,
        problem_directory / common.LITERATURE_RESULT,
    ]
    artifact_paths: set[Path] = set()
    if attempt_directory is not None:
        paths.extend(
            attempt_directory / name
            for name in (
                "attempt.md",
                "solver-result.json",
                "critique.md",
                "review-result.json",
            )
        )
        artifacts = attempt_directory / "artifacts"
        if artifacts.is_dir():
            artifact_paths.update(
                path for path in artifacts.rglob("*") if path.is_file()
            )
            paths.extend(sorted(artifact_paths, key=lambda path: path.as_posix()))
    files = []
    for path in paths:
        if not path.is_file():
            continue
        file = {
            "label": os.path.relpath(path, paper).replace(os.sep, "/"),
            "path": str(path),
            "artifact": path in artifact_paths,
        }
        if include_browser_uris:
            file["uri"] = _browser_file_uri(path)
        files.append(file)
    return files


def load_review_files(
    item: dict,
    *,
    include_browser_uris: bool = False,
) -> list[dict]:
    """Load file links for one review only when its detail is requested."""
    paper = Path(item["paperDirectory"])
    attempt_value = item.get("attemptDirectory")
    return _review_file_records(
        paper,
        paper / item["problemId"],
        Path(attempt_value) if attempt_value else None,
        include_browser_uris=include_browser_uris,
    )


def _relevant_paths(item: HumanReviewItem) -> list[Path]:
    return _relevant_problem_paths(item.attempt.problem, item.attempt)


def _append_file_contents(
    lines: list[str],
    *,
    heading: str,
    path: Path,
) -> None:
    lines.extend((f"### {heading}", "", f"File: `{path.resolve()}`", ""))
    try:
        contents = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError) as exc:
        lines.extend((f"_Could not read this file: {exc}_", ""))
        return
    lines.extend((contents or "_File is empty._", ""))


@lru_cache(maxsize=1)
def _native_project_root() -> str:
    """Convert the project root once instead of spawning cygpath per link."""
    return codex_cli.path_for_codex(PROJECT_ROOT)


@lru_cache(maxsize=65536)
def _browser_file_uri(path: Path) -> str:
    resolved = path if path.is_absolute() else path.resolve()
    try:
        relative = resolved.relative_to(PROJECT_ROOT)
    except ValueError:
        native = codex_cli.path_for_codex(resolved)
    else:
        native_root = _native_project_root()
        if len(native_root) >= 3 and native_root[1:3] in {":\\", ":/"}:
            native = str(PureWindowsPath(native_root).joinpath(*relative.parts))
        else:
            return resolved.as_uri()
    if len(native) >= 3 and native[1:3] in {":\\", ":/"}:
        return PureWindowsPath(native).as_uri()
    return resolved.as_uri()


def open_in_browser(target: Path | str) -> bool:
    """Open a local file or URL in the graphical system browser."""
    if sys.platform == "cygwin":
        argument = (
            codex_cli.path_for_codex(target)
            if isinstance(target, Path)
            else target
        )
        subprocess.Popen(
            ["cygstart", argument],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True
    url = _browser_file_uri(target) if isinstance(target, Path) else target
    return webbrowser.open(url)


@lru_cache(maxsize=1)
def _review_model_script() -> str:
    """Load the browser review model shared with the live workbench."""
    return REVIEW_MODEL_PATH.read_text(encoding="utf-8")


@lru_cache(maxsize=1)
def _review_tokens_css() -> str:
    """Load visual tokens shared by the report and live workbench."""
    return REVIEW_TOKENS_PATH.read_text(encoding="utf-8")


@lru_cache(maxsize=65536)
def project_display_path(path: Path) -> str:
    """Format a path relative to this project, with portable separators."""
    resolved = path if path.is_absolute() else path.resolve()
    try:
        return resolved.relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return resolved.as_posix()


_project_display_path = project_display_path


def _read_optional_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError, UnicodeError):
        return ""


_OPEN_PROBLEM_HEADING_RE = re.compile(
    r"^(?P<marks>#{1,6})[ \t]+(?P<id>OP-[0-9]+)\b.*$",
    re.IGNORECASE,
)
_MARKDOWN_HEADING_RE = re.compile(r"^(?P<marks>#{1,6})[ \t]+")


def _extract_open_problem_markdown(path: Path, problem_id: str) -> str:
    """Return one problem's Markdown body from open-problems.md."""
    lines = _read_optional_text(path).replace("\r\n", "\n").splitlines()
    start = None
    heading_level = None
    for index, line in enumerate(lines):
        match = _OPEN_PROBLEM_HEADING_RE.match(line)
        if match and match.group("id").upper() == problem_id.upper():
            start = index + 1
            heading_level = len(match.group("marks"))
            break
    if start is None or heading_level is None:
        return ""

    end = len(lines)
    for index in range(start, len(lines)):
        match = _MARKDOWN_HEADING_RE.match(lines[index])
        if match and len(match.group("marks")) <= heading_level:
            end = index
            break
    return "\n".join(lines[start:end]).strip()


def build_review_catalog(
    items: Sequence[HumanReviewItem],
    *,
    include_contents: bool,
    problems: Sequence[common.ProblemRef] | None = None,
    attempt_names: set[str] | None = None,
    latest_per_problem: bool = False,
    progress: Callable[[int, int], None] | None = None,
    freshness_progress: Callable[[int, int], None] | None = None,
    include_browser_uris: bool = True,
    include_files: bool = True,
) -> list[dict]:
    data: list[dict] = []
    problem_statements: dict[tuple[Path, str], str] = {}
    problem_attempt_counts: dict[tuple[Path, str], int] = {}
    problem_attempt_directories: dict[tuple[Path, str], list[Path]] = {}
    problem_contexts: dict[
        tuple[Path, str], tuple[dict | None, bool, dict | None]
    ] = {}
    paper_analysis_digests: dict[Path, str] = {}
    resolved_papers: dict[Path, Path] = {}
    reviewed = {
        item.attempt.directory: item
        for item in items
    }
    rows: list[
        tuple[
            common.ProblemRef,
            review_solutions.AttemptRef | None,
            HumanReviewItem | None,
        ]
    ] = []
    if problems is None:
        rows.extend((item.attempt.problem, item.attempt, item) for item in items)
    else:
        for problem in problems:
            all_attempts = common.attempt_directories(problem)
            problem_attempt_counts[
                (problem.paper_directory, problem.id)
            ] = len(all_attempts)
            problem_attempt_directories[
                (problem.paper_directory, problem.id)
            ] = all_attempts
            attempts = [
                directory
                for directory in all_attempts
                if attempt_names is None or directory.name in attempt_names
            ]
            if not attempts:
                if attempt_names is None and not all_attempts:
                    rows.append((problem, None, None))
                continue
            added = False
            for directory in attempts:
                item = reviewed.get(directory)
                if item is not None:
                    rows.append((problem, item.attempt, item))
                    added = True
                    continue
                if (directory / "review-result.json").is_file():
                    continue
                solver_result = common.read_json(
                    directory / "solver-result.json",
                    description=(
                        f"solver result for {problem.id}/{directory.name}"
                    ),
                )
                rows.append(
                    (
                        problem,
                        review_solutions.AttemptRef(
                            problem,
                            directory,
                            solver_result,
                        ),
                        None,
                    )
                )
                added = True
            if not added:
                # Reviews filtered by freshness or explicit attempt selection
                # are effectively awaiting an applicable review.
                directory = attempts[-1]
                solver_result = common.read_json(
                    directory / "solver-result.json",
                    description=(
                        f"solver result for {problem.id}/{directory.name}"
                    ),
                )
                rows.append(
                    (
                        problem,
                        review_solutions.AttemptRef(
                            problem,
                            directory,
                            solver_result,
                        ),
                        None,
                    )
                )

    if latest_per_problem:
        latest_rows: dict[
            tuple[Path, str],
            tuple[
                common.ProblemRef,
                review_solutions.AttemptRef | None,
                HumanReviewItem | None,
            ],
        ] = {}
        for row in rows:
            problem, attempt, _ = row
            key = (problem.paper_directory, problem.id)
            previous = latest_rows.get(key)
            previous_attempt = previous[1] if previous is not None else None
            if previous is None or (
                attempt is not None
                and (
                    previous_attempt is None
                    or _attempt_number(attempt)
                    > _attempt_number(previous_attempt)
                )
            ):
                latest_rows[key] = row
        rows = list(latest_rows.values())

    unique_problems = {
        (problem.paper_directory, problem.id): problem
        for problem, _, _ in rows
    }
    context_papers = {
        problem.paper_directory
        for problem in unique_problems.values()
        if common.triage_result(problem) is not None
        or common.literature_result(problem) is not None
    }
    paper_analysis_digests.update(
        (paper, common.analysis_digest(paper))
        for paper in context_papers
    )

    def load_problem_context(
        problem: common.ProblemRef,
    ) -> tuple[dict | None, bool, dict | None]:
        triage_value = common.triage_result(problem)
        literature_candidate = common.literature_result(problem)
        digest = (
            common.problem_digest(
                problem,
                paper_analysis_digest=paper_analysis_digests[
                    problem.paper_directory
                ],
            )
            if problem.paper_directory in paper_analysis_digests
            else None
        )
        triage_current = common.triage_is_current(
            problem,
            problem_digest_value=digest,
            attempts=problem_attempt_directories.get(
                (problem.paper_directory, problem.id)
            ),
        )
        literature_value = (
            literature_candidate
            if literature_candidate is not None
            and common.literature_is_current(
                problem,
                problem_digest_value=digest,
            )
            else None
        )
        return triage_value, triage_current, literature_value

    problem_values = list(unique_problems.items())
    if freshness_progress is not None:
        freshness_progress(0, len(problem_values))
    if len(problem_values) > 1:
        with ThreadPoolExecutor(max_workers=CATALOG_IO_WORKERS) as executor:
            contexts = executor.map(
                load_problem_context,
                (problem for _, problem in problem_values),
            )
            for index, ((key, _), context) in enumerate(
                zip(problem_values, contexts),
                1,
            ):
                problem_contexts[key] = context
                if freshness_progress is not None:
                    freshness_progress(index, len(problem_values))
    else:
        problem_contexts.update(
            (key, load_problem_context(problem))
            for key, problem in problem_values
        )
        if freshness_progress is not None:
            freshness_progress(len(problem_values), len(problem_values))

    total_rows = len(rows)
    if progress is not None:
        progress(0, total_rows)
    for index, (problem, attempt, item) in enumerate(rows):
        paper = problem.paper_directory
        problem_key = (paper, problem.id)
        if problem_key not in problem_attempt_counts:
            problem_attempt_counts[problem_key] = len(
                common.attempt_directories(problem)
            )
        attempt_status = (
            "reviewed"
            if item is not None
            else "unreviewed"
            if attempt is not None
            else "unattempted"
        )
        review_result = item.review_result if item is not None else {}
        solver_result = attempt.solver_result if attempt is not None else {}
        triage, triage_current, literature = problem_contexts[problem_key]
        if paper not in resolved_papers:
            resolved_papers[paper] = paper.resolve()
        if include_contents and problem_key not in problem_statements:
            problem_statements[problem_key] = (
                _extract_open_problem_markdown(
                    paper / "analysis" / "open-problems.md",
                    problem.id,
                )
            )
        files = (
            _review_file_records(
                paper,
                problem.directory,
                attempt.directory if attempt is not None else None,
                include_browser_uris=include_browser_uris,
            )
            if include_files
            else []
        )
        data.append(
            {
                "id": f"review-{index + 1}",
                "itemKey": (
                    f"{resolved_papers[paper]}::"
                    f"{problem.id}::"
                    f"{attempt.name if attempt is not None else ''}"
                ),
                "problemKey": f"{paper}::{problem.id}",
                "paperUrlKey": project_display_path(paper),
                "paperTitle": problem.paper_title,
                "paperAuthors": list(problem.paper_authors),
                "paperDirectory": str(paper),
                "problemId": problem.id,
                "problemTitle": problem.title,
                "problemStatement": (
                    problem_statements.get(problem_key, "")
                    if include_contents
                    else ""
                ),
                "explicitness": problem.explicitness,
                "attemptStatus": attempt_status,
                "attemptName": attempt.name if attempt is not None else "",
                "attemptDirectory": (
                    str(attempt.directory) if attempt is not None else ""
                ),
                "attemptDisplayPath": _project_display_path(
                    attempt.directory
                    if attempt is not None
                    else problem.directory
                ),
                "attemptNumber": (
                    _attempt_number(attempt) if attempt is not None else -1
                ),
                "totalAttemptCount": problem_attempt_counts[problem_key],
                "priority": item.priority if item is not None else "",
                "current": item.current if item is not None else None,
                "legacyVerdict": review_result.get("verdict", ""),
                "reviewSchema": (
                    "current"
                    if review_result.get("correctness")
                    in review_solutions.CORRECTNESS_LEVELS
                    else "legacy"
                    if item is not None
                    else "none"
                ),
                "correctness": review_result.get(
                    "correctness", "legacy" if item is not None else ""
                ),
                "reviewedCoverage": review_result.get(
                    "reviewed_coverage", "legacy" if item is not None else ""
                ),
                "importance": review_result.get(
                    "importance", "legacy" if item is not None else ""
                ),
                "verificationConfidence": review_result.get(
                    "verification_confidence",
                    "legacy" if item is not None else "",
                ),
                "criticSummary": review_result.get("summary", ""),
                "claimedResultType": (
                    common.claimed_result_type(solver_result)
                    if attempt is not None
                    else ""
                ),
                "solverSummary": solver_result.get("summary", ""),
                "externalSources": solver_result.get(
                    "external_sources", []
                ),
                "triageClassification": (
                    triage.get("classification", "")
                    if triage is not None
                    else ""
                ),
                "triageCurrent": triage_current,
                "triageSummary": (
                    triage.get("rationale", "")
                    if triage is not None
                    else ""
                ),
                "triageReport": (
                    _read_optional_text(
                        problem.directory / common.TRIAGE_MARKDOWN
                    )
                    if include_contents and triage is not None
                    else ""
                ),
                "hasTriageReport": (
                    triage is not None
                    and (problem.directory / common.TRIAGE_MARKDOWN).is_file()
                ),
                "literatureStatus": (
                    literature.get("resolution_status", "")
                    if literature is not None
                    else ""
                ),
                "literatureConfidence": (
                    literature.get("confidence", "")
                    if literature is not None
                    else ""
                ),
                "literatureSummary": (
                    literature.get("status_summary", "")
                    if literature is not None
                    else ""
                ),
                "literatureReport": (
                    _read_optional_text(
                        problem.directory / common.LITERATURE_MARKDOWN
                    )
                    if include_contents and literature is not None
                    else ""
                ),
                "hasLiteratureReport": (
                    literature is not None
                    and (problem.directory / common.LITERATURE_MARKDOWN).is_file()
                ),
                "claimReviews": review_result.get(
                    "claim_reviews", []
                ),
                "blockingGaps": review_result.get(
                    "blocking_gaps", []
                ),
                "recommendedNextSteps": review_result.get(
                    "recommended_next_steps", []
                ),
                "warnings": review_result.get("warnings", []),
                "files": files,
                "fileCount": len(files),
                "critique": (
                    _read_optional_text(
                        attempt.directory / "critique.md"
                    )
                    if include_contents and attempt is not None
                    else ""
                ),
                "solverAttempt": (
                    _read_optional_text(
                        attempt.directory / "attempt.md"
                    )
                    if include_contents and attempt is not None
                    else ""
                ),
            }
        )
        if progress is not None:
            progress(index + 1, total_rows)
    return data


# Kept for callers of the original private helper.  New code should use the
# public name so the standalone report and the live workbench share one data
# model.
_html_data = build_review_catalog


def load_review_contents(item: dict) -> dict[str, str]:
    """Load the long Markdown fields for one catalog item on demand."""
    paper = Path(item["paperDirectory"])
    problem_id = item["problemId"]
    attempt_value = item.get("attemptDirectory")
    attempt = Path(attempt_value) if attempt_value else None
    problem = paper / problem_id
    return {
        "problemStatement": _extract_open_problem_markdown(
            paper / "analysis" / "open-problems.md",
            problem_id,
        ),
        "triageReport": _read_optional_text(
            problem / common.TRIAGE_MARKDOWN
        ),
        "literatureReport": _read_optional_text(
            problem / common.LITERATURE_MARKDOWN
        ),
        "critique": (
            _read_optional_text(attempt / "critique.md")
            if attempt is not None
            else ""
        ),
        "solverAttempt": (
            _read_optional_text(attempt / "attempt.md")
            if attempt is not None
            else ""
        ),
    }


def render_human_review_html(
    items: Sequence[HumanReviewItem],
    *,
    include_contents: bool = True,
    problems: Sequence[common.ProblemRef] | None = None,
    initial_priorities: set[str] | None = None,
    attempt_names: set[str] | None = None,
    latest_per_problem: bool = False,
) -> str:
    """Build a local SPA for navigating human reviews."""
    payload = json.dumps(
        build_review_catalog(
            items,
            include_contents=include_contents,
            problems=problems,
            attempt_names=attempt_names,
            latest_per_problem=latest_per_problem,
        ),
        ensure_ascii=False,
    ).replace("</", "<\\/")
    settings_payload = json.dumps(
        {
            "initialPriorities": sorted(
                initial_priorities
                if initial_priorities is not None
                else {"high", "medium"}
            )
        }
    )
    review_model_script = _review_model_script().replace("</", "<\\/")
    review_tokens_css = _review_tokens_css().replace("</", "<\\/")
    return """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Loose Ends — Human Review</title>
  <link rel="stylesheet"
    href="https://cdn.jsdelivr.net/npm/katex@0.17.0/dist/katex.min.css"
    integrity="sha384-vlBdW0r3AcZO/HboRPznQNowvexd3fY8qHOWkBi5q7KGgqJ+F48+DceybYmrVbmB"
    crossorigin="anonymous">
  <script
    src="https://cdn.jsdelivr.net/npm/katex@0.17.0/dist/katex.min.js"
    integrity="sha384-AtrdNsnxl/75rvBneBVH7DtOvCxSVahR2zWqle1coBKd8DEmLoviqNeJSx64gNAs"
    crossorigin="anonymous"></script>
  <script
    src="https://cdn.jsdelivr.net/npm/markdown-it@14.3.0/dist/markdown-it.min.js"
    crossorigin="anonymous"></script>
  <script
    src="https://cdn.jsdelivr.net/npm/@mdit/plugin-katex@1.0.1/dist/cdn.umd.js"
    crossorigin="anonymous"></script>
  <style>""" + review_tokens_css + """</style>
  <style>
    * { box-sizing: border-box; }
    html, body { min-height: 100%; }
    body {
      margin: 0;
      background:
        radial-gradient(circle at 8% 0%, var(--glow) 0, transparent 24rem),
        var(--paper);
      color: var(--ink);
      font: 15px/1.55 ui-sans-serif, system-ui, -apple-system,
        "Segoe UI", sans-serif;
    }
    button, input, select { font: inherit; }
    .shell {
      min-height: 100vh;
      display: grid;
      grid-template-columns: clamp(290px, 25vw, 370px) minmax(0, 1fr);
    }
    .rail {
      min-height: 100vh;
      padding: 28px 22px;
      border-right: 1px solid var(--line);
      background: var(--rail-bg);
      backdrop-filter: blur(12px);
      position: sticky;
      top: 0;
      height: 100vh;
      overflow: auto;
    }
    .brand {
      color: var(--heading);
      font: 750 25px/1.05 ui-serif, Georgia, serif;
      letter-spacing: -0.025em;
    }
    .eyebrow {
      margin-top: 8px;
      color: var(--accent);
      font-size: 11px;
      font-weight: 800;
      letter-spacing: 0.16em;
      text-transform: uppercase;
    }
    .queue-count {
      margin: 20px 0 14px;
      color: var(--muted);
    }
    .controls { display: grid; gap: 11px; }
    .advanced-filters {
      border: 1px solid var(--line);
      border-radius: 10px;
      background: var(--surface);
      padding: 8px 10px;
    }
    .advanced-filters summary {
      color: var(--muted);
      cursor: pointer;
      font-size: 12px;
      font-weight: 800;
    }
    .advanced-filter-grid {
      display: grid;
      gap: 10px;
      margin-top: 10px;
    }
    label.control {
      display: grid;
      gap: 5px;
      color: var(--muted);
      font-size: 12px;
      font-weight: 700;
    }
    input[type="search"] {
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 9px;
      background: var(--surface);
      color: var(--ink);
      padding: 9px 10px;
      outline: none;
    }
    input[type="search"]:focus {
      border-color: var(--accent);
      box-shadow: 0 0 0 3px var(--accent-ring);
    }
    select {
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 9px;
      background: var(--surface);
      color: var(--ink);
      padding: 9px 10px;
      outline: none;
    }
    select:focus {
      border-color: var(--accent);
      box-shadow: 0 0 0 3px var(--accent-ring);
    }
    .filters { display: flex; gap: 8px; }
    .filter {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 6px 9px;
      border: 1px solid var(--line);
      border-radius: 999px;
      background: var(--surface);
      font-size: 12px;
      font-weight: 750;
      cursor: pointer;
    }
    .problem-label, .attempt-label {
      margin: 22px 0 8px;
      color: var(--muted);
      font-size: 11px;
      font-weight: 800;
      letter-spacing: 0.12em;
      text-transform: uppercase;
    }
    .problem-list {
      display: grid;
      gap: 15px;
      max-height: min(52vh, 610px);
      overflow: auto;
      padding: 2px 5px 3px 2px;
      scrollbar-gutter: stable;
    }
    .paper-group { display: grid; gap: 6px; }
    .paper-title {
      color: var(--accent);
      font-size: 11px;
      font-weight: 850;
      letter-spacing: 0.035em;
      line-height: 1.35;
    }
    .problem-card {
      width: 100%;
      border: 1px solid transparent;
      border-left: 3px solid var(--line);
      border-radius: 8px;
      background: transparent;
      color: var(--ink);
      padding: 8px 9px 8px 11px;
      text-align: left;
      cursor: pointer;
      transition: 120ms ease;
    }
    .problem-card:hover {
      border-color: var(--line);
      border-left-color: var(--accent);
      background: var(--surface);
    }
    .problem-card.active {
      border-color: var(--accent);
      background: var(--surface);
      box-shadow: 0 0 0 2px var(--accent-ring);
    }
    .problem-card strong {
      display: block;
      color: var(--heading);
      font-size: 13px;
      line-height: 1.4;
    }
    .problem-meta {
      display: block;
      margin-top: 6px;
      color: var(--muted);
      font-size: 11px;
    }
    .attempt-list { display: grid; gap: 8px; }
    .attempt-card {
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 11px;
      background: var(--surface);
      padding: 11px 12px;
      text-align: left;
      cursor: pointer;
      transition: 120ms ease;
    }
    .attempt-card:hover { border-color: var(--accent-border); transform: translateY(-1px); }
    .attempt-card.active {
      border-color: var(--accent);
      box-shadow: 0 0 0 2px var(--accent-ring);
    }
    .attempt-card strong { display: block; color: var(--heading); }
    .attempt-tags {
      display: flex;
      flex-wrap: wrap;
      gap: 4px;
      margin-top: 6px;
    }
    .attempt-tag {
      display: inline-flex;
      border: 1px solid var(--line);
      border-radius: 999px;
      background: var(--paper);
      color: var(--muted);
      padding: 2px 6px;
      font-size: 9px;
      font-weight: 800;
      line-height: 1.35;
      text-transform: uppercase;
    }
    .attempt-tag.claim { color: var(--accent); }
    .attempt-tag.status { color: var(--medium); }
    .attempt-tag.known { color: var(--known); background: var(--known-bg); }
    .main {
      min-width: 0;
      padding: 40px clamp(18px, 4vw, 78px) 80px;
    }
    .empty {
      max-width: 720px;
      margin: 15vh auto;
      color: var(--muted);
      text-align: center;
    }
    .review { min-width: 0; max-width: 1080px; margin: 0 auto; }
    .topline {
      display: flex;
      align-items: center;
      gap: 9px;
      flex-wrap: wrap;
    }
    .badge {
      display: inline-flex;
      align-items: center;
      border-radius: 999px;
      padding: 5px 9px;
      font-size: 11px;
      font-weight: 850;
      letter-spacing: 0.09em;
      text-transform: uppercase;
    }
    .badge.high { color: var(--high); background: var(--high-bg); }
    .badge.medium { color: var(--medium); background: var(--medium-bg); }
    .badge.low, .badge.none { color: var(--low); background: var(--low-bg); }
    .badge.unattempted { color: var(--low); background: var(--low-bg); }
    .badge.unreviewed { color: var(--medium); background: var(--medium-bg); }
    .badge.solution {
      color: var(--success);
      background: var(--success-soft);
    }
    .badge.counterexample {
      color: var(--counterexample);
      background: var(--counterexample-soft);
    }
    .badge.known {
      color: var(--known);
      background: var(--known-bg);
    }
    .badge.literature {
      color: var(--accent);
      background: var(--glow);
    }
    .verdict {
      color: var(--muted);
      font-size: 13px;
      font-weight: 700;
    }
    h1 {
      max-width: 880px;
      margin: 17px 0 8px;
      color: var(--heading);
      font: 760 clamp(30px, 4vw, 49px)/1.07 ui-serif, Georgia, serif;
      letter-spacing: -0.035em;
    }
    .problem-heading {
      max-width: 900px;
      margin: 12px 0 6px;
      color: var(--ink);
      font: 680 clamp(20px, 2.4vw, 29px)/1.25 ui-sans-serif,
        system-ui, sans-serif;
      letter-spacing: -0.02em;
    }
    .metadata { color: var(--muted); }
    .summary-grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 15px;
      margin: 28px 0;
    }
    .summary {
      min-width: 0;
      border: 1px solid var(--line);
      border-radius: 14px;
      background: var(--panel);
      padding: 18px 19px;
      box-shadow: var(--shadow);
    }
    .summary.literature-summary { grid-column: 1 / -1; }
    .summary h2, .section h2 {
      margin: 0 0 9px;
      color: var(--heading);
      font-size: 14px;
      letter-spacing: 0.025em;
    }
    .summary p { margin: 0; }
    .problem-statement {
      margin-top: 26px;
    }
    .problem-statement > h2 {
      margin: 0 0 9px;
      color: var(--heading);
      font-size: 14px;
      letter-spacing: 0.025em;
    }
    .section {
      margin-top: 17px;
      border-top: 1px solid var(--line);
      padding-top: 20px;
    }
    .claim-list, .plain-list { margin: 0; padding-left: 21px; }
    .claim-list li, .plain-list li { margin: 7px 0; }
    .claim-list strong { color: var(--heading); }
    .tabs {
      display: flex;
      gap: 3px;
      margin-top: 30px;
      border-bottom: 1px solid var(--line);
    }
    .tab {
      border: 0;
      border-bottom: 3px solid transparent;
      background: transparent;
      color: var(--muted);
      padding: 10px 13px 9px;
      font-weight: 750;
      cursor: pointer;
    }
    .tab.active { color: var(--heading); border-bottom-color: var(--accent); }
    .tab-panel { padding-top: 18px; }
    .markdown-body {
      margin: 0;
      border: 1px solid var(--line);
      border-radius: 13px;
      background: var(--document);
      padding: 22px;
      overflow: auto;
      overflow-wrap: anywhere;
      font: 15px/1.65 ui-sans-serif, system-ui, -apple-system,
        "Segoe UI", sans-serif;
    }
    .markdown-body > :first-child { margin-top: 0; }
    .markdown-body > :last-child { margin-bottom: 0; }
    .markdown-body h1, .markdown-body h2, .markdown-body h3,
    .markdown-body h4, .markdown-body h5, .markdown-body h6 {
      max-width: none;
      margin: 1.45em 0 0.55em;
      color: var(--heading);
      font-family: ui-serif, Georgia, serif;
      font-weight: 750;
      line-height: 1.22;
      letter-spacing: -0.015em;
    }
    .markdown-body h1 { font-size: 28px; }
    .markdown-body h2 {
      padding-bottom: 6px;
      border-bottom: 1px solid var(--line);
      font-size: 22px;
    }
    .markdown-body h3 { font-size: 18px; }
    .markdown-body h4, .markdown-body h5, .markdown-body h6 {
      font-size: 15px;
    }
    .markdown-body p { margin: 0.75em 0; }
    .markdown-body ul, .markdown-body ol {
      margin: 0.75em 0;
      padding-left: 25px;
    }
    .markdown-body li { margin: 0.28em 0; }
    .markdown-body blockquote {
      margin: 1em 0;
      border-left: 4px solid var(--accent);
      padding: 1px 0 1px 15px;
      color: var(--muted);
    }
    .markdown-body pre {
      overflow: auto;
      margin: 1em 0;
      border: 1px solid var(--line);
      border-radius: 9px;
      background: var(--paper);
      padding: 14px;
      white-space: pre;
      font: 13px/1.55 ui-monospace, "Cascadia Code", Consolas, monospace;
      tab-size: 2;
    }
    .markdown-body code {
      border-radius: 4px;
      background: var(--paper);
      padding: 0.12em 0.32em;
      font: 0.9em ui-monospace, "Cascadia Code", Consolas, monospace;
    }
    .markdown-body pre code { background: transparent; padding: 0; }
    .markdown-body a { color: var(--accent); }
    .markdown-body hr {
      margin: 1.5em 0;
      border: 0;
      border-top: 1px solid var(--line);
    }
    .markdown-body .table-wrap { overflow-x: auto; margin: 1em 0; }
    .markdown-body table {
      width: 100%;
      border-collapse: collapse;
      font-size: 14px;
    }
    .markdown-body th, .markdown-body td {
      border: 1px solid var(--line);
      padding: 8px 10px;
      text-align: left;
      vertical-align: top;
    }
    .markdown-body th { background: var(--panel); color: var(--heading); }
    .markdown-body del { color: var(--muted); }
    .markdown-body .katex-display {
      overflow-x: auto;
      overflow-y: hidden;
      padding: 4px 0;
    }
    .summary .markdown-body {
      border: 0;
      border-radius: 0;
      background: transparent;
      padding: 0;
      overflow: auto;
      font: inherit;
    }
    .summary .markdown-body p { margin: 0.65em 0; }
    .summary .markdown-body h1, .summary .markdown-body h2,
    .summary .markdown-body h3, .summary .markdown-body h4,
    .summary .markdown-body h5, .summary .markdown-body h6 {
      font-size: 1em;
    }
    .file-list { display: grid; gap: 8px; }
    .file {
      display: block;
      border: 1px solid var(--line);
      border-radius: 10px;
      background: var(--surface);
      padding: 10px 12px;
      color: var(--heading);
      text-decoration: none;
    }
    .file:hover { border-color: var(--accent); }
    .file strong { display: block; }
    .file span {
      display: block;
      margin-top: 2px;
      color: var(--muted);
      font: 11px/1.35 ui-monospace, Consolas, monospace;
      overflow-wrap: anywhere;
    }
    .attempt-path {
      display: block;
      margin-top: 8px;
      color: var(--muted);
      font: 12px/1.4 ui-monospace, Consolas, monospace;
      overflow-wrap: anywhere;
    }
    .stale { color: var(--high); font-weight: 750; }
    [hidden] { display: none !important; }
    @media (max-width: 1200px) {
      .summary-grid { grid-template-columns: 1fr; }
    }
    @media (max-width: 850px) {
      .shell { display: block; }
      .rail {
        position: static;
        min-height: 0;
        height: auto;
        border-right: 0;
        border-bottom: 1px solid var(--line);
      }
      .main { padding-top: 30px; }
    }
  </style>
</head>
<body>
  <div class="shell">
    <aside class="rail">
      <div class="brand">Loose Ends</div>
      <div class="eyebrow">Human review</div>
      <div class="queue-count" id="queue-count"></div>
      <div class="controls">
        <label class="control">Search
          <input id="search" type="search"
            placeholder="Paper title/id, problem, attempt…"
            autocomplete="off">
        </label>
        <label class="control">Attempt status
          <select id="attempt-status-filter"></select>
        </label>
        <label class="control">Claim type
          <select id="claim-filter"></select>
        </label>
        <details class="advanced-filters">
          <summary>Advanced review filters</summary>
          <div class="advanced-filter-grid">
        <label class="control">Correctness
          <select id="correctness-filter"></select>
        </label>
        <label class="control">Coverage
          <select id="coverage-filter"></select>
        </label>
        <label class="control">Importance
          <select id="importance-filter"></select>
        </label>
        <label class="control">Verification confidence
          <select id="confidence-filter"></select>
        </label>
          </div>
        </details>
        <label class="control">Literature status
          <select id="literature-filter"></select>
        </label>
        <div class="filters" aria-label="Human priority filters">
          <label class="filter" id="filter-high-wrap"><input id="filter-high" type="checkbox" checked>
            High</label>
          <label class="filter" id="filter-medium-wrap"><input id="filter-medium" type="checkbox" checked>
            Medium</label>
          <label class="filter" id="filter-low-wrap" hidden>
            <input id="filter-low" type="checkbox" checked> Low</label>
          <label class="filter" id="filter-none-wrap" hidden>
            <input id="filter-none" type="checkbox" checked> None</label>
        </div>
        <div class="filters" aria-label="Review status filters">
          <label class="filter" id="filter-current-wrap">
            <input id="filter-current" type="checkbox" checked> Current</label>
          <label class="filter" id="filter-stale-wrap">
            <input id="filter-stale" type="checkbox" checked> Stale</label>
        </div>
      </div>
      <div class="problem-label">Papers and open problems</div>
      <div class="problem-list" id="problem-list"
        role="listbox" aria-label="Select an open problem"></div>
      <div class="attempt-label">Attempts</div>
      <div class="attempt-list" id="attempt-list"></div>
    </aside>
    <main class="main">
      <div class="empty" id="empty" hidden>
        <h1>No matching open problems</h1>
        <p>Change the search, mathematical assessment, literature, review
          status, or human-priority filters.</p>
      </div>
      <article class="review" id="review"></article>
    </main>
  </div>
  <script id="review-data" type="application/json">""" + payload + """</script>
  <script id="review-settings" type="application/json">""" + settings_payload + """
  </script>
  <script>""" + review_model_script + """</script>
  <script>
    const allItems = JSON.parse(
      document.getElementById("review-data").textContent
    );
    const settings = JSON.parse(
      document.getElementById("review-settings").textContent
    );
    const state = { selectedProblem: "", selectedItem: "", tab: "attempt" };
    const pageScrollPositions = new Map();
    let renderedItemId = "";
    let scrollUpdateFrame = null;
    if ("scrollRestoration" in history) {
      history.scrollRestoration = "manual";
    }
    const search = document.getElementById("search");
    const attemptStatusFilter = document.getElementById(
      "attempt-status-filter"
    );
    const claimFilter = document.getElementById("claim-filter");
    const correctnessFilter = document.getElementById("correctness-filter");
    const coverageFilter = document.getElementById("coverage-filter");
    const importanceFilter = document.getElementById("importance-filter");
    const confidenceFilter = document.getElementById("confidence-filter");
    const literatureFilter = document.getElementById("literature-filter");
    function populateFilter(select, options) {
      options.forEach(([value, label]) => {
        const option = document.createElement("option");
        option.value = value;
        option.textContent = label;
        select.append(option);
      });
    }
    [
      [attemptStatusFilter, "attemptStatus"],
      [claimFilter, "claim"],
      [correctnessFilter, "correctness"],
      [coverageFilter, "coverage"],
      [importanceFilter, "importance"],
      [confidenceFilter, "confidence"],
      [literatureFilter, "literature"]
    ].forEach(([select, key]) => {
      populateFilter(select, LooseEndsReviewModel.filterOptions[key]);
    });
    const high = document.getElementById("filter-high");
    const medium = document.getElementById("filter-medium");
    const low = document.getElementById("filter-low");
    const none = document.getElementById("filter-none");
    const priorityFilters = { high, medium, low, none };
    Object.entries(priorityFilters).forEach(([level, input]) => {
      input.checked = settings.initialPriorities.includes(level);
    });
    const current = document.getElementById("filter-current");
    const stale = document.getElementById("filter-stale");
    const problemList = document.getElementById("problem-list");
    const attemptList = document.getElementById("attempt-list");
    const review = document.getElementById("review");
    const empty = document.getElementById("empty");
    const queueCount = document.getElementById("queue-count");
    let markdownRenderer = null;
    try {
      markdownRenderer = LooseEndsReviewModel.createMarkdownRenderer(window);
    } catch (error) {
      console.warn("Markdown rendering unavailable", error);
    }

    function node(tag, className = "", text = "") {
      const element = document.createElement(tag);
      if (className) element.className = className;
      if (text !== "") element.textContent = text;
      return element;
    }

    function activeReviewFilters() {
      return {
        attemptStatus: attemptStatusFilter.value,
        claim: claimFilter.value,
        correctness: correctnessFilter.value,
        coverage: coverageFilter.value,
        importance: importanceFilter.value,
        confidence: confidenceFilter.value,
        literature: literatureFilter.value,
        priorities: new Set(
          Object.entries(priorityFilters)
            .filter(([, input]) => input.checked)
            .map(([level]) => level)
        ),
        current: current.checked,
        stale: stale.checked
      };
    }

    function filteredItems() {
      return LooseEndsReviewModel.filterItems(
        allItems,
        activeReviewFilters(),
        search.value
      );
    }

    const {
      attemptsForProblem,
      detailBadges,
      detailTabs,
      groupProblemsByPaper,
      humanize,
      latestProblems,
      queueSummary,
      statusLabel,
      summaryCards
    } = LooseEndsReviewModel;

    function appendAttemptTags(parent, item, { includeKnown = false } = {}) {
      const tags = node("div", "attempt-tags");
      LooseEndsReviewModel.attemptTags(item, { includeKnown }).forEach(value => {
        const tag = node(
          "span",
          `attempt-tag${value.className ? ` ${value.className}` : ""}`,
          value.label
        );
        tag.title = value.title;
        tags.append(tag);
      });
      parent.append(tags);
    }

    function addList(parent, title, values) {
      if (!Array.isArray(values) || values.length === 0) return;
      const section = node("section", "section");
      section.append(node("h2", "", title));
      const list = node("ul", "plain-list");
      values.forEach(value => list.append(node("li", "", String(value))));
      section.append(list);
      parent.append(section);
    }

    function markdownBody(markdown, missingText) {
      const body = node("div", "markdown-body");
      if (!markdown) {
        body.append(node("p", "", missingText));
      } else if (markdownRenderer) {
        body.innerHTML = markdownRenderer.render(markdown);
      } else {
        body.append(node("pre", "markdown-source", markdown));
      }
      return body;
    }

    function historyPayload(scrollY = window.scrollY) {
      return {
        humanReview: true,
        selectedProblem: state.selectedProblem,
        selectedItem: state.selectedItem,
        tab: state.tab,
        scrollY
      };
    }

    function itemUrl(itemId) {
      const parameters = new URLSearchParams();
      const query = search.value.trim();
      if (query) {
        parameters.set("q", query);
      }
      LooseEndsReviewModel.filtersToSearchParams(
        parameters,
        activeReviewFilters(),
        settings.initialPriorities
      );
      const item = allItems.find(value => value.id === itemId);
      LooseEndsReviewModel.identityToSearchParams(parameters, item);
      if (item && state.tab && state.tab !== "attempt") {
        parameters.set("detail", state.tab);
      }
      const encodedParameters = parameters.toString();
      return `${location.pathname}` +
        `${encodedParameters ? `?${encodedParameters}` : ""}`;
    }

    function applyControlsFromLocation() {
      const parameters = new URLSearchParams(location.search);
      search.value = parameters.get("q") || "";
      const filters = LooseEndsReviewModel.filtersFromSearchParams(
        parameters,
        settings.initialPriorities
      );
      attemptStatusFilter.value = filters.attemptStatus;
      claimFilter.value = filters.claim;
      correctnessFilter.value = filters.correctness;
      coverageFilter.value = filters.coverage;
      importanceFilter.value = filters.importance;
      confidenceFilter.value = filters.confidence;
      literatureFilter.value = filters.literature;
      Object.entries(priorityFilters).forEach(([level, input]) => {
        input.checked = filters.priorities.has(level);
      });
      current.checked = filters.current;
      stale.checked = filters.stale;
    }

    function requestedItemFromLocation() {
      const parameters = new URLSearchParams(location.search);
      const identity = LooseEndsReviewModel.identityFromSearchParams(parameters);
      const legacyId = decodeURIComponent(location.hash.slice(1));
      return LooseEndsReviewModel.findReviewItem(allItems, identity) ||
        allItems.find(item => item.id === legacyId);
    }

    function rememberCurrentScroll({ updateHistory = true } = {}) {
      if (!renderedItemId) return;
      const scrollY = window.scrollY;
      pageScrollPositions.set(renderedItemId, scrollY);
      if (
        updateHistory &&
        history.state?.humanReview &&
        history.state.selectedItem === renderedItemId
      ) {
        history.replaceState(
          historyPayload(scrollY),
          "",
          itemUrl(renderedItemId)
        );
      }
    }

    function restorePageScroll(itemId, preferredScroll) {
      const scrollY = Number.isFinite(preferredScroll)
        ? preferredScroll
        : pageScrollPositions.get(itemId) ?? 0;
      pageScrollPositions.set(itemId, scrollY);
      requestAnimationFrame(() => {
        window.scrollTo({ top: scrollY, left: 0, behavior: "auto" });
      });
      return scrollY;
    }

    function navigateToItem(item) {
      if (!item) return;
      rememberCurrentScroll();
      state.selectedProblem = item.problemKey;
      state.selectedItem = item.id;
      state.tab = "attempt";
      render();
      renderedItemId = state.selectedItem;
      const scrollY = pageScrollPositions.get(renderedItemId) ?? 0;
      const historyMethod =
        history.state?.humanReview &&
        history.state.selectedItem === renderedItemId
          ? "replaceState"
          : "pushState";
      history[historyMethod](
        historyPayload(scrollY),
        "",
        itemUrl(renderedItemId)
      );
      restorePageScroll(renderedItemId, scrollY);
    }

    function renderProblemControls(items) {
      const problems = latestProblems(items);
      if (!problems.some(item => item.problemKey === state.selectedProblem)) {
        state.selectedProblem = problems[0]?.problemKey || "";
      }
      problemList.replaceChildren();
      groupProblemsByPaper(items).forEach(group => {
        const section = node("section", "paper-group");
        section.append(node("div", "paper-title", group.paperTitle));
        group.problems.forEach(item => {
          const problemAttempts = attemptsForProblem(
            items, item.problemKey
          );
          const button = node(
            "button",
            `problem-card${
              item.problemKey === state.selectedProblem ? " active" : ""
            }`
          );
          button.type = "button";
          button.setAttribute("role", "option");
          button.setAttribute(
            "aria-selected",
            item.problemKey === state.selectedProblem ? "true" : "false"
          );
          button.append(
            node("strong", "", `${item.problemId} — ${item.problemTitle}`)
          );
          appendAttemptTags(button, item, { includeKnown: true });
          button.append(
            node(
              "span",
              "problem-meta",
              `${statusLabel(item.attemptStatus)} · ` +
              `${item.totalAttemptCount} total attempt${
                item.totalAttemptCount === 1 ? "" : "s"
              }`
            )
          );
          button.addEventListener("click", () => {
            navigateToItem(problemAttempts[0]);
          });
          section.append(button);
        });
        problemList.append(section);
      });

      const attempts = attemptsForProblem(
        items, state.selectedProblem
      );
      if (!attempts.some(item => item.id === state.selectedItem)) {
        state.selectedItem = attempts[0]?.id || "";
      }
      attemptList.replaceChildren();
      attempts.forEach(item => {
        const button = node(
          "button",
          `attempt-card${item.id === state.selectedItem ? " active" : ""}`
        );
        button.type = "button";
        button.append(
          node(
            "strong",
            "",
            item.attemptName || "No attempts yet"
          )
        );
        appendAttemptTags(button, item);
        button.addEventListener("click", () => {
          navigateToItem(item);
        });
        attemptList.append(button);
      });
    }

    function renderReview(item) {
      review.replaceChildren();
      const top = node("div", "topline");
      detailBadges(item).forEach(value => {
        if (value.dimension === "warning") {
          top.append(node("span", "stale", value.label.toUpperCase()));
        } else {
          const className = value.dimension === "priority"
            ? `badge ${value.value}`
            : value.dimension === "status"
              ? `badge ${value.value}`
              : value.dimension === "literature"
                ? "badge literature"
                : "badge";
          top.append(node("span", className, humanize(value.label)));
        }
      });
      top.append(
        node(
          "span",
          "verdict",
          item.attemptStatus === "reviewed"
            ? `${humanize(item.claimedResultType)} · ${item.attemptName}`
            : item.attemptName || "No solver attempt"
        )
      );
      review.append(top);
      review.append(node("h1", "", item.paperTitle));
      review.append(
        node(
          "div",
          "problem-heading",
          `${item.problemId} — ${item.problemTitle}`
        )
      );
      const authors = item.paperAuthors.length
        ? item.paperAuthors.join(", ")
        : "";
      review.append(
        node("div", "metadata",
          `${authors}${authors ? " · " : ""}${item.explicitness}`)
      );
      review.append(
        node("code", "attempt-path", item.attemptDisplayPath)
      );

      const problemStatement = node("section", "problem-statement");
      problemStatement.append(
        node("h2", "", "Open problem statement"),
        markdownBody(
          item.problemStatement,
          "The full statement was not found in open-problems.md."
        )
      );
      review.append(problemStatement);

      const summaries = node("div", "summary-grid");
      summaryCards(item).forEach(card => {
        const summary = node(
          "section",
          `summary${card.key === "literature" ? " literature-summary" : ""}`
        );
        summary.append(
          node("h2", "", card.title),
          markdownBody(card.value, card.missing || "No summary available.")
        );
        summaries.append(summary);
      });
      if (summaries.childElementCount) review.append(summaries);

      if (Array.isArray(item.claimReviews) && item.claimReviews.length) {
        const section = node("section", "section");
        section.append(node("h2", "", "Claim assessments"));
        const list = node("ul", "claim-list");
        item.claimReviews.forEach(claim => {
          const row = node("li");
          row.append(
            node("strong", "",
              `${claim.claim_id || "?"} — ${claim.assessment || "unknown"}: `),
            document.createTextNode(claim.explanation || "")
          );
          list.append(row);
        });
        section.append(list);
        review.append(section);
      }
      addList(review, "Blocking gaps", item.blockingGaps);
      addList(review, "Recommended next steps", item.recommendedNextSteps);
      addList(review, "Warnings", item.warnings);

      const tabs = node("div", "tabs");
      const panels = {
        critique: item.critique,
        attempt: item.solverAttempt,
        triage: item.triageReport,
        literature: item.literatureReport,
        files: item.files
      };
      const tabEntries = detailTabs(item);
      if (!tabEntries.some(([key]) => key === state.tab)) {
        state.tab = tabEntries[0][0];
      }
      tabEntries.forEach(([key, label]) => {
        const button = node(
          "button", `tab${state.tab === key ? " active" : ""}`, label
        );
        button.type = "button";
        button.addEventListener("click", () => {
          rememberCurrentScroll();
          state.tab = key;
          renderReview(item);
          history.pushState(
            historyPayload(window.scrollY),
            "",
            itemUrl(state.selectedItem)
          );
        });
        tabs.append(button);
      });
      review.append(tabs);

      const panel = node("div", "tab-panel");
      if (state.tab === "files") {
        const list = node("div", "file-list");
        panels.files.forEach(file => {
          const link = node("a", "file");
          link.href = file.uri;
          link.target = "_blank";
          link.rel = "noopener noreferrer";
          link.append(
            node("strong", "", `${file.artifact ? "Artifact · " : ""}${file.label}`),
            node("span", "", file.path)
          );
          list.append(link);
        });
        panel.append(list);
      } else {
        const markdown = panels[state.tab];
        panel.append(
          markdownBody(markdown, "Content was omitted from this report.")
        );
      }
      review.append(panel);
    }

    function render() {
      const items = filteredItems();
      queueCount.textContent = queueSummary(items, activeReviewFilters());
      renderProblemControls(items);
      const selected = items.find(item => item.id === state.selectedItem);
      empty.hidden = Boolean(selected);
      review.hidden = !selected;
      if (selected) renderReview(selected);
    }

    function updateFilters() {
      rememberCurrentScroll();
      state.selectedProblem = "";
      state.selectedItem = "";
      state.tab = "attempt";
      render();
      renderedItemId = state.selectedItem;
      if (renderedItemId) {
        const scrollY = pageScrollPositions.get(renderedItemId) ?? 0;
        history.replaceState(
          historyPayload(scrollY),
          "",
          itemUrl(renderedItemId)
        );
        restorePageScroll(renderedItemId, scrollY);
      } else {
        history.replaceState(historyPayload(0), "", itemUrl(""));
      }
    }
    search.addEventListener("input", updateFilters);
    attemptStatusFilter.addEventListener("change", updateFilters);
    claimFilter.addEventListener("change", updateFilters);
    correctnessFilter.addEventListener("change", updateFilters);
    coverageFilter.addEventListener("change", updateFilters);
    importanceFilter.addEventListener("change", updateFilters);
    confidenceFilter.addEventListener("change", updateFilters);
    literatureFilter.addEventListener("change", updateFilters);
    high.addEventListener("change", updateFilters);
    medium.addEventListener("change", updateFilters);
    low.addEventListener("change", updateFilters);
    none.addEventListener("change", updateFilters);
    current.addEventListener("change", updateFilters);
    stale.addEventListener("change", updateFilters);
    window.addEventListener("scroll", () => {
      if (scrollUpdateFrame !== null) return;
      scrollUpdateFrame = requestAnimationFrame(() => {
        scrollUpdateFrame = null;
        rememberCurrentScroll();
      });
    }, { passive: true });

    window.addEventListener("popstate", event => {
      rememberCurrentScroll({ updateHistory: false });
      applyControlsFromLocation();
      const requestedItem = requestedItemFromLocation();
      state.selectedProblem = requestedItem?.problemKey || "";
      state.selectedItem = requestedItem?.id || "";
      state.tab = new URLSearchParams(location.search).get("detail") ||
        (event.state?.humanReview ? event.state.tab : "attempt") || "attempt";
      render();
      renderedItemId = state.selectedItem;
      const storedScroll =
        event.state?.humanReview &&
        event.state.selectedItem === renderedItemId &&
        Number.isFinite(event.state.scrollY)
          ? event.state.scrollY
          : pageScrollPositions.get(renderedItemId);
      restorePageScroll(renderedItemId, storedScroll);
    });

    applyControlsFromLocation();
    const requestedItem = requestedItemFromLocation();
    if (requestedItem) {
      state.selectedProblem = requestedItem.problemKey;
      state.selectedItem = requestedItem.id;
    }
    state.tab = new URLSearchParams(location.search).get("detail") || "attempt";
    const availableFilters = LooseEndsReviewModel.availableFilters(allItems);
    ["high", "medium", "low", "none"].forEach(level => {
      document.getElementById(`filter-${level}-wrap`).hidden =
        !availableFilters.priorities.has(level);
    });
    document.getElementById("filter-current-wrap").hidden =
      !availableFilters.current;
    document.getElementById("filter-stale-wrap").hidden =
      !availableFilters.stale;
    render();
    renderedItemId = state.selectedItem;
    const initialScroll =
      history.state?.humanReview &&
      history.state.selectedItem === renderedItemId &&
      Number.isFinite(history.state.scrollY)
        ? history.state.scrollY
        : 0;
    if (renderedItemId) {
      history.replaceState(
        historyPayload(initialScroll),
        "",
        itemUrl(renderedItemId)
      );
      restorePageScroll(renderedItemId, initialScroll);
    }
  </script>
</body>
</html>
"""


def render_human_review_report(
    items: Sequence[HumanReviewItem],
    *,
    include_contents: bool = True,
) -> str:
    """Build a Markdown review queue with critic and solver evidence."""
    counts = Counter(item.priority for item in items)
    count_text = ", ".join(
        f"{counts[level]} {level}"
        for level in ("high", "medium", "low", "none")
        if counts[level]
    )
    lines = [
        "# Human review queue",
        "",
        f"{len(items)} attempt(s): {count_text}.",
        "",
        "Items are ordered by paper title, problem, and newest attempt "
        "first.",
        "",
    ]
    for index, item in enumerate(items, 1):
        attempt = item.attempt
        problem = attempt.problem
        review = item.review_result
        solver = attempt.solver_result
        literature = (
            common.literature_result(problem)
            if common.literature_is_current(problem)
            else None
        )
        current_label = "" if item.current else " — STALE REVIEW"
        lines.extend(
            (
                "---",
                "",
                f"## {index}. {item.priority.upper()} — "
                f"{problem.id}/{attempt.name}{current_label}",
                "",
                f"**Attempt path:** `{attempt.directory}`",
                "",
                f"**Paper:** {problem.paper_title}",
                "",
                f"**Problem:** {problem.title} "
                f"(`{problem.explicitness}`)",
                "",
                f"**Claimed result type:** "
                f"`{common.claimed_result_type(solver)}`",
                "",
                f"**Correctness:** "
                f"`{review.get('correctness', 'legacy')}`",
                "",
                f"**Reviewed coverage:** "
                f"`{review.get('reviewed_coverage', 'legacy')}`",
                "",
                f"**Importance:** "
                f"`{review.get('importance', 'legacy')}`",
                "",
                f"**Verification confidence:** "
                f"`{review.get('verification_confidence', 'legacy')}`",
                "",
                f"**Critic summary:** {review.get('summary', '')}",
                "",
            )
        )
        if literature is not None:
            lines.extend(
                (
                    f"**Literature status:** "
                    f"`{literature.get('resolution_status', 'unknown')}` "
                    f"({literature.get('confidence', 'unknown')} confidence)",
                    "",
                    f"**Literature summary:** "
                    f"{literature.get('status_summary', '')}",
                    "",
                )
            )
        solver_summary = solver.get("summary")
        if isinstance(solver_summary, str) and solver_summary.strip():
            lines.extend(
                (f"**Solver summary:** {solver_summary.strip()}", "")
            )

        claim_reviews = review.get("claim_reviews")
        if isinstance(claim_reviews, list) and claim_reviews:
            lines.extend(("### Claim assessments", ""))
            for claim in claim_reviews:
                if not isinstance(claim, dict):
                    continue
                lines.append(
                    f"- **{claim.get('claim_id', '?')} — "
                    f"{claim.get('assessment', 'unknown')}:** "
                    f"{claim.get('explanation', '')}"
                )
            lines.append("")

        _append_list(lines, "Blocking gaps", review.get("blocking_gaps"))
        _append_list(
            lines,
            "Recommended next steps",
            review.get("recommended_next_steps"),
        )
        _append_list(lines, "Warnings", review.get("warnings"))

        lines.extend(("### Relevant files", ""))
        lines.extend(f"- `{path}`" for path in _relevant_paths(item))
        lines.append("")

        if include_contents:
            literature_path = (
                problem.directory / common.LITERATURE_MARKDOWN
            )
            if literature is not None and literature_path.is_file():
                _append_file_contents(
                    lines,
                    heading="Literature search",
                    path=literature_path,
                )
            _append_file_contents(
                lines,
                heading="Solution attempt",
                path=attempt.directory / "attempt.md",
            )
            _append_file_contents(
                lines,
                heading="Critique",
                path=attempt.directory / "critique.md",
            )
    return "\n".join(lines).rstrip() + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "browse open problems, solver attempts, and critic reviews"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  Build and open the high- and medium-priority dashboard:
    python src/human_review.py papers/edemaine

  Show only the newest selected attempt for each problem:
    python src/human_review.py papers/edemaine --latest-per-problem

  Write a compact dashboard without embedding long Markdown files:
    python src/human_review.py papers/edemaine \\
      --summary-only --output human-review.html --no-open

  Use the original terminal report instead:
    python src/human_review.py papers/edemaine --terminal
""",
    )
    parser.add_argument(
        "paths",
        nargs="+",
        type=Path,
        help="paper directories or parent directories containing papers",
    )
    parser.add_argument(
        "--priority",
        "--attention",
        dest="priority",
        default=DEFAULT_PRIORITY,
        metavar="LEVELS",
        help=(
            "comma-separated reviewed-attempt priority levels initially "
            "shown in HTML, or included in terminal output "
            "(default: high,medium)"
        ),
    )
    parser.add_argument(
        "--problem",
        action="append",
        dest="problem_ids",
        metavar="OP-ID",
        help="only show this problem ID; may be repeated",
    )
    parser.add_argument(
        "--attempt",
        action="append",
        dest="attempt_names",
        metavar="ATTEMPT-NNN",
        help="only show this attempt directory name; may be repeated",
    )
    parser.add_argument(
        "--latest-per-problem",
        action="store_true",
        help="show only the newest selected attempt for each problem",
    )
    freshness = parser.add_mutually_exclusive_group()
    freshness.add_argument(
        "--current-only",
        action="store_true",
        help="exclude reviews invalidated by a newer attempt or review schema",
    )
    freshness.add_argument(
        "--include-stale",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--summary-only",
        action="store_true",
        help="show summaries and paths without embedding Markdown contents",
    )
    parser.add_argument(
        "--output",
        type=Path,
        metavar="FILE",
        help="HTML dashboard path (default: ./human-review.html)",
    )
    parser.add_argument(
        "--terminal",
        action="store_true",
        help="show the Markdown report in the terminal instead of HTML",
    )
    parser.add_argument(
        "--no-open",
        action="store_true",
        help="write the HTML dashboard without opening a browser",
    )
    parser.add_argument(
        "--no-pager",
        action="store_true",
        help="with --terminal, print directly instead of using a pager",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    codex_cli.configure_utf8_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        priority = common.parse_csv_values(
            args.priority,
            allowed=review_solutions.HUMAN_PRIORITY_LEVELS,
            label="--priority",
        )
        problems = common.discover_problem_refs(
            args.paths,
            problem_ids=set(args.problem_ids) if args.problem_ids else None,
        )
        review_priority = (
            priority
            if args.terminal
            else set(review_solutions.HUMAN_PRIORITY_LEVELS)
        )
        items = discover_human_reviews(
            problems,
            priority=review_priority,
            attempt_names=(
                set(args.attempt_names) if args.attempt_names else None
            ),
            include_stale=not args.current_only,
            latest_per_problem=(
                args.latest_per_problem if args.terminal else False
            ),
            allow_empty=not args.terminal,
        )
        if args.terminal:
            if args.output is not None:
                raise common.CodexError(
                    "--output cannot be combined with --terminal"
                )
            report = render_human_review_report(
                items,
                include_contents=not args.summary_only,
            )
            if args.no_pager or not sys.stdout.isatty():
                print(report, end="")
            else:
                pydoc.pager(report)
        else:
            output = (
                args.output
                if args.output is not None
                else Path("human-review.html")
            ).expanduser().resolve()
            dashboard = render_human_review_html(
                items,
                include_contents=not args.summary_only,
                problems=problems,
                initial_priorities=priority,
                attempt_names=(
                    set(args.attempt_names) if args.attempt_names else None
                ),
                latest_per_problem=args.latest_per_problem,
            )
            output.write_text(dashboard, encoding="utf-8")
            print(
                f"Wrote human-review dashboard for {len(problems)} open "
                f"problem(s), including {len(items)} reviewed attempt(s), "
                f"to {output}."
            )
            if not args.no_open:
                try:
                    open_in_browser(output)
                except OSError as exc:
                    print(
                        f"Could not open the browser automatically: {exc}",
                        file=sys.stderr,
                    )
    except (common.CodexError, OSError, UnicodeError) as exc:
        return codex_cli.report_error(parser, exc)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
