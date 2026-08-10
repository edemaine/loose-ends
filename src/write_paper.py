#!/usr/bin/env python3
"""Compose one reviewed open-problem manuscript and iteratively critique it."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Sequence

import analyze_papers
import codex_cli
import open_problem_common as common
import review_solutions


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIRECTORY = PROJECT_ROOT / "manuscripts"
DEFAULT_PROMPT_PATH = PROJECT_ROOT / "prompts" / "write-open-problem-paper.md"
DEFAULT_SCHEMA_PATH = (
    PROJECT_ROOT / "schemas" / "open-problem-paper.schema.json"
)
DEFAULT_REVIEW_PROMPT_PATH = (
    PROJECT_ROOT / "prompts" / "review-open-problem-paper.md"
)
DEFAULT_REVIEW_SCHEMA_PATH = (
    PROJECT_ROOT / "schemas" / "open-problem-paper-review.schema.json"
)
ATTEMPT_RE = re.compile(r"^attempt-[0-9]{3,}$")
DRAFT_RE = re.compile(r"^draft-([0-9]{3,})$")
RESULT_ID_RE = re.compile(r"^R-[0-9]{3,}$")
CLAIM_ID_RE = re.compile(r"^C-[0-9]{3,}$")
FINDING_ID_RE = re.compile(r"^P-[0-9]{3,}$")
SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
PAPER_STATUSES = ("draft_complete", "blocked")
PAPER_VERDICTS = (
    "invalid",
    "needs_research",
    "needs_major_revision",
    "needs_minor_revision",
    "ready_for_expert_review",
)
REVISION_VERDICTS = {"needs_major_revision", "needs_minor_revision"}
MAX_DERIVED_NAME_LENGTH = 160


@dataclass(frozen=True)
class PaperInput:
    result_id: str
    attempt: review_solutions.AttemptRef
    review_result: dict
    literature_result: dict
    readiness_issues: tuple[str, ...] = ()


@dataclass(frozen=True)
class DraftRef:
    directory: Path
    number: int
    result: dict


@dataclass(frozen=True)
class PaperReview:
    draft: DraftRef
    result: dict


@dataclass(frozen=True)
class PipelineOutcome:
    drafts: tuple[DraftRef, ...]
    final_review: PaperReview | None
    reason: str

    @property
    def ready(self) -> bool:
        return (
            self.final_review is not None
            and self.final_review.result.get("verdict")
            == "ready_for_expert_review"
        )


def _read_text(path: Path, description: str) -> str:
    try:
        value = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise common.CodexError(f"missing {description}: {path}") from exc
    except (OSError, UnicodeError) as exc:
        raise common.CodexError(f"could not read {description}: {exc}") from exc
    if not value.strip():
        raise common.CodexError(f"{description} is empty: {path}")
    return value


def _attempt_from_path(path: Path) -> review_solutions.AttemptRef:
    directory = path.expanduser().resolve()
    if not directory.is_dir() or not ATTEMPT_RE.fullmatch(directory.name):
        raise common.CodexError(
            f"attempt path must name an attempt-NNN directory: {path}"
        )
    problem_directory = directory.parent
    if not analyze_papers.OPEN_PROBLEM_ID_RE.fullmatch(problem_directory.name):
        raise common.CodexError(
            f"attempt path is not under PAPER/OP-NNN: {directory}"
        )
    paper_directory = problem_directory.parent
    problems = common.discover_problem_refs(
        [paper_directory],
        problem_ids={problem_directory.name},
    )
    exact = [
        problem
        for problem in problems
        if problem.paper_directory == paper_directory
        and problem.id == problem_directory.name
    ]
    if len(exact) != 1:
        raise common.CodexError(
            f"could not identify the open problem for {directory}"
        )
    solver_result = common.read_json(
        directory / "solver-result.json",
        description=f"solver result for {directory}",
    )
    return review_solutions.AttemptRef(exact[0], directory, solver_result)


def expand_attempt_inputs(paths: Sequence[Path]) -> tuple[list[Path], list[str]]:
    """Expand paper/problem inputs to their latest available attempts."""
    attempts: list[Path] = []
    warnings: list[str] = []
    for raw_path in paths:
        path = raw_path.expanduser().resolve()
        if ATTEMPT_RE.fullmatch(path.name):
            attempts.append(path)
            continue
        if (
            analyze_papers.OPEN_PROBLEM_ID_RE.fullmatch(path.name)
            and (path.parent / "analysis" / "manifest.json").is_file()
        ):
            problems = common.discover_problem_refs(
                [path.parent],
                problem_ids={path.name},
            )
        elif (path / "analysis" / "manifest.json").is_file():
            problems = common.discover_problem_refs([path])
        else:
            raise common.CodexError(
                "paper input must name an analyzed paper, an OP-NNN "
                f"directory, or an attempt-NNN directory: {raw_path}"
            )
        if not problems:
            raise common.CodexError(f"paper input has no open problems: {raw_path}")
        for problem in problems:
            available = common.attempt_directories(problem)
            if available:
                attempts.append(available[-1])
            else:
                warnings.append(
                    f"{problem.paper_directory.name}/{problem.id}: "
                    "no solver attempts; retained only as an open problem, "
                    "not a result input"
                )
    return attempts, warnings


def _prior_attempt_directories(
    attempt: review_solutions.AttemptRef,
) -> list[Path]:
    available = common.attempt_directories(attempt.problem)
    try:
        index = available.index(attempt.directory)
    except ValueError as exc:
        raise common.CodexError(
            f"selected attempt is not in its problem history: {attempt.directory}"
        ) from exc
    return available[:index]


def _prior_attempt_history_digest(
    attempt: review_solutions.AttemptRef,
) -> str:
    records = []
    for directory in _prior_attempt_directories(attempt):
        records.append(
            {
                "attempt_name": directory.name,
                "attempt_digest": common.solver_attempt_digest(directory),
                "review_digest": common.files_digest(
                    [
                        (name, directory / name)
                        for name in (
                            "critique.md",
                            "review-result.json",
                            "review-manifest.json",
                        )
                    ]
                ),
            }
        )
    return common.stable_value_digest(records)


def _readiness_issues(attempt: review_solutions.AttemptRef) -> list[str]:
    issues: list[str] = []
    claimed = common.claimed_result_type(attempt.solver_result)
    if claimed not in {"solution", "counterexample"}:
        issues.append(f"solver classifies the work as {claimed}")
    if not review_solutions.review_is_current(attempt):
        issues.append("the independent solution review is missing or stale")
        review = common.load_json(attempt.directory / "review-result.json") or {}
    else:
        review = common.read_json(
            attempt.directory / "review-result.json",
            description=f"solution review for {attempt.directory}",
        )
    correctness = review.get("correctness")
    if correctness != "well_supported":
        issues.append(f"solution review correctness is {correctness}")
    coverage = review.get("reviewed_coverage")
    if coverage not in {
        "complete",
        "complete_under_stated_interpretation",
    }:
        issues.append(f"solution review coverage is {coverage}")
    importance = review.get("importance")
    if importance not in {"major", "resolution"}:
        issues.append(f"solution review importance is {importance}")
    gaps = review.get("blocking_gaps")
    if not isinstance(gaps, list):
        issues.append("solution review has invalid gap metadata")
    else:
        issues.extend(
            f"solution review records a remaining issue: {gap}"
            for gap in gaps
            if isinstance(gap, str) and gap.strip()
        )
    claim_reviews = review.get("claim_reviews")
    if not isinstance(claim_reviews, list) or not any(
        isinstance(item, dict) and item.get("assessment") == "supported"
        for item in claim_reviews
    ):
        issues.append("solution review supports no checkable claim")
    if not common.literature_is_current(attempt.problem):
        issues.append("the literature review is missing or stale")
    else:
        literature = common.literature_result(attempt.problem) or {}
        if literature.get("resolution_status") == "resolved":
            issues.append("the literature review marks the problem resolved")
    return issues


def load_paper_inputs(attempt_paths: Sequence[Path]) -> list[PaperInput]:
    if not attempt_paths:
        raise common.CodexError("select at least one solver attempt")
    attempts = [_attempt_from_path(path) for path in attempt_paths]
    resolved_paths = [attempt.directory for attempt in attempts]
    if len(set(resolved_paths)) != len(resolved_paths):
        raise common.CodexError("the same solver attempt was selected more than once")
    attempts.sort(
        key=lambda attempt: (
            os.path.normcase(str(attempt.problem.paper_directory)),
            attempt.problem.id,
            attempt.name,
        )
    )
    inputs: list[PaperInput] = []
    for index, attempt in enumerate(attempts, 1):
        issues = _readiness_issues(attempt)
        review = common.load_json(attempt.directory / "review-result.json") or {}
        literature = common.literature_result(attempt.problem) or {}
        inputs.append(
            PaperInput(
                f"R-{index:03d}",
                attempt,
                review,
                literature,
                tuple(issues),
            )
        )
    return inputs


def derive_manuscript_name(inputs: Sequence[PaperInput]) -> str:
    grouped: dict[str, set[str]] = {}
    for item in inputs:
        grouped.setdefault(
            item.attempt.problem.paper_directory.name,
            set(),
        ).add(item.attempt.problem.id)
    pieces = [
        "_".join([paper_name, *sorted(problem_ids)])
        for paper_name, problem_ids in sorted(grouped.items())
    ]
    name = "__".join(pieces)
    if len(name) > MAX_DERIVED_NAME_LENGTH:
        suffix = hashlib.sha256(name.encode("utf-8")).hexdigest()[:8]
        name = f"{name[: MAX_DERIVED_NAME_LENGTH - 9]}_{suffix}"
    return name


def validate_manuscript_name(value: str) -> str:
    if not SAFE_NAME_RE.fullmatch(value) or value in {".", ".."}:
        raise common.CodexError(
            "--name must contain only letters, digits, dots, underscores, "
            "and hyphens, and must start with a letter or digit"
        )
    return value


def _input_digest(inputs: Sequence[PaperInput]) -> str:
    records = []
    for item in inputs:
        attempt = item.attempt
        records.append(
            {
                "result_id": item.result_id,
                "attempt_path": str(attempt.directory),
                "problem_digest": common.problem_digest(attempt.problem),
                "attempt_digest": common.solver_attempt_digest(attempt.directory),
                "solution_review_digest": common.files_digest(
                    [
                        (name, attempt.directory / name)
                        for name in (
                            "critique.md",
                            "review-result.json",
                            "review-manifest.json",
                        )
                    ]
                ),
                "prior_attempt_history_digest": _prior_attempt_history_digest(
                    attempt
                ),
                "literature_digest": common.literature_snapshot_digest(
                    attempt.problem
                ),
                "readiness_issues": item.readiness_issues,
            }
        )
    return common.stable_value_digest(records)


def _copy_path(source: Path, destination: Path) -> None:
    if source.is_dir():
        shutil.copytree(source, destination)
    elif source.is_file():
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)


def _copy_attempt_history(source: Path, destination: Path) -> None:
    destination.mkdir(parents=True)
    for name in common.ATTEMPT_HISTORY_FILES:
        _copy_path(source / name, destination / name)
    _copy_path(source / "artifacts", destination / "artifacts")


def _copy_draft(
    source: Path,
    destination: Path,
    *,
    include_review: bool,
) -> None:
    destination.mkdir(parents=True)
    names = [
        "main.tex",
        "references.bib",
        "main.pdf",
        "readiness.md",
        "paper-result.json",
        "manifest.json",
    ]
    if include_review:
        names.extend(
            (
                "paper-critique.md",
                "paper-review.json",
                "paper-review-manifest.json",
            )
        )
    for name in names:
        _copy_path(source / name, destination / name)
    _copy_path(source / "figures", destination / "figures")


def stage_paper_context(
    workspace: Path,
    inputs: Sequence[PaperInput],
    *,
    manuscript: DraftRef | None = None,
    include_manuscript_review: bool = True,
) -> Path:
    root = workspace / "inputs"
    root.mkdir(parents=True, exist_ok=False)
    papers = sorted(
        {item.attempt.problem.paper_directory for item in inputs},
        key=lambda path: os.path.normcase(str(path)),
    )
    paper_ids = {paper: f"P-{index:03d}" for index, paper in enumerate(papers, 1)}
    paper_entries = []
    for paper in papers:
        paper_id = paper_ids[paper]
        destination = root / "papers" / paper_id
        paper_input = destination / "paper"
        paper_input.mkdir(parents=True)
        for name in ("paper.pdf", "source", "metadata.json", "PDF_ONLY"):
            _copy_path(paper / name, paper_input / name)
        analysis = destination / "analysis"
        analysis.mkdir()
        for name in common.ANALYSIS_FILES:
            source = paper / "analysis" / name
            if not source.is_file():
                raise common.CodexError(f"analysis input is missing: {source}")
            shutil.copyfile(source, analysis / name)
        representative = next(
            item.attempt.problem
            for item in inputs
            if item.attempt.problem.paper_directory == paper
        )
        paper_entries.append(
            {
                "paper_id": paper_id,
                "directory_name": paper.name,
                "title": representative.paper_title,
                "authors": list(representative.paper_authors),
                "path": f"papers/{paper_id}",
            }
        )
    result_entries = []
    for item in inputs:
        attempt = item.attempt
        destination = root / "results" / item.result_id
        prior_attempt_entries = []
        for prior in _prior_attempt_directories(attempt):
            prior_path = destination / "history" / prior.name
            _copy_attempt_history(prior, prior_path)
            prior_attempt_entries.append(
                {
                    "attempt_name": prior.name,
                    "path": f"results/{item.result_id}/history/{prior.name}",
                }
            )
        attempt_input = destination / "attempt"
        attempt_input.mkdir(parents=True)
        for name in ("attempt.md", "solver-result.json", "manifest.json"):
            _copy_path(attempt.directory / name, attempt_input / name)
        _copy_path(attempt.directory / "artifacts", attempt_input / "artifacts")
        review_input = destination / "solution-review"
        for name in (
            "critique.md",
            "review-result.json",
            "review-manifest.json",
        ):
            _copy_path(attempt.directory / name, review_input / name)
        literature_input = destination / "literature"
        for name in (
            common.LITERATURE_MARKDOWN,
            common.LITERATURE_RESULT,
            common.LITERATURE_MANIFEST,
        ):
            _copy_path(attempt.problem.directory / name, literature_input / name)
        problem_record = {
            "result_id": item.result_id,
            "paper_id": paper_ids[attempt.problem.paper_directory],
            "problem": attempt.problem.problem,
            "attempt_name": attempt.name,
            "prior_attempts": prior_attempt_entries,
            "claimed_result_type": common.claimed_result_type(
                attempt.solver_result
            ),
            "solution_review_correctness": item.review_result.get(
                "correctness"
            ),
            "solution_review_coverage": item.review_result.get(
                "reviewed_coverage"
            ),
            "solution_review_importance": item.review_result.get(
                "importance"
            ),
            "literature_status": item.literature_result.get(
                "resolution_status"
            ),
            "readiness_issues": list(item.readiness_issues),
        }
        common.write_json(destination / "result.json", problem_record)
        result_entries.append(
            {
                **problem_record,
                "path": f"results/{item.result_id}",
            }
        )
    if manuscript is not None:
        _copy_draft(
            manuscript.directory,
            root / "manuscript",
            include_review=include_manuscript_review,
        )
    common.write_json(
        root / "index.json",
        {
            "papers": paper_entries,
            "results": result_entries,
            "manuscript_path": "manuscript" if manuscript is not None else None,
        },
    )
    codex_cli.grant_sandbox_read_access(root)
    return root


def _metadata(authors: Sequence[str], title_hint: str | None) -> dict:
    return {
        "authors": list(authors),
        "title_hint": title_hint or "",
    }


def render_writer_prompt(
    template: str,
    *,
    context: Path,
    authors: Sequence[str],
    title_hint: str | None,
    previous: DraftRef | None,
    revision_instruction: str | None = None,
) -> str:
    if previous is None:
        mode = (
            "Create the first draft. There are no previous paper-critic "
            "findings, so `addressed_findings` must be empty."
        )
    else:
        mode = (
            "Revise the manuscript in `inputs/manuscript/`. Read its "
            "`paper-critique.md` and `paper-review.json` when present, "
            "preserve correct material, and address every P-### finding "
            "explicitly in "
            "`readiness.md` and `addressed_findings`. Write a complete "
            "replacement manuscript in the current directory; do not edit "
            "the staged prior draft."
        )
    if revision_instruction is not None:
        mode += (
            "\n\nThe user explicitly requested this revision direction:\n\n"
            f"<revision_instruction>\n{revision_instruction}\n"
            "</revision_instruction>\n\n"
            "Follow it throughout this author-review invocation while "
            "preserving mathematical correctness, evidence requirements, "
            "and all independently verified material."
        )
    return (
        template.replace("{{MODE_INSTRUCTION}}", mode)
        .replace("{{CONTEXT_DIRECTORY}}", codex_cli.path_for_codex(context))
        .replace(
            "{{MANUSCRIPT_METADATA_JSON}}",
            json.dumps(
                _metadata(authors, title_hint),
                indent=2,
                ensure_ascii=False,
            ),
        )
    )


def render_reviewer_prompt(
    template: str,
    *,
    context: Path,
) -> str:
    manuscript = context / "manuscript"
    return (
        template.replace(
            "{{MANUSCRIPT_DIRECTORY}}",
            codex_cli.path_for_codex(manuscript),
        ).replace(
            "{{CONTEXT_DIRECTORY}}",
            codex_cli.path_for_codex(context),
        )
    )


def _string_list(result: dict, field: str) -> list[str]:
    value = result.get(field)
    if not isinstance(value, list) or not all(
        isinstance(item, str) for item in value
    ):
        raise common.CodexError(f"paper response has invalid {field}")
    return value


def _tex_citation_keys(tex: str) -> set[str]:
    keys: set[str] = set()
    pattern = re.compile(
        r"\\(?:cite|citep|citet|autocite|parencite|textcite)[A-Za-z]*"
        r"\s*(?:\[[^\]]*\]\s*)*\{([^}]*)\}"
    )
    for match in pattern.finditer(tex):
        keys.update(key.strip() for key in match.group(1).split(",") if key.strip())
    return keys


def _bib_keys(bib: str) -> set[str]:
    return {
        match.group(1).strip()
        for match in re.finditer(r"@[A-Za-z]+\s*\{\s*([^,\s]+)", bib)
    }


def _generated_files(workspace: Path, values: object) -> list[Path]:
    if not isinstance(values, list) or not all(
        isinstance(value, str) for value in values
    ):
        raise common.CodexError("paper response has invalid generated_files")
    paths: list[Path] = []
    seen: set[str] = set()
    standard_outputs = {
        "main.tex",
        "references.bib",
        "readiness.md",
        "main.pdf",
    }
    for value in values:
        normalized = value.replace("\\", "/")
        relative = PurePosixPath(normalized)
        if (
            relative.is_absolute()
            or not relative.parts
            or ".." in relative.parts
            or normalized in seen
            or (
                normalized not in standard_outputs
                and relative.parts[0] != "figures"
            )
        ):
            raise common.CodexError(
                f"paper response has unsafe generated file: {value!r}"
            )
        seen.add(normalized)
        path = workspace.joinpath(*relative.parts)
        if not path.is_file():
            raise common.CodexError(
                f"paper-listed generated file does not exist: {value}"
            )
        if normalized not in standard_outputs:
            paths.append(path)
    for normalized in seen:
        relative = PurePosixPath(normalized)
        if relative.suffix.casefold() != ".svg":
            continue
        pdf = relative.with_suffix(".pdf").as_posix()
        if pdf not in seen:
            raise common.CodexError(
                "generated SVG must include a matching PDF: "
                f"{normalized} -> {pdf}"
            )
    return paths


def _previous_finding_ids(previous: DraftRef | None) -> set[str]:
    if previous is None:
        return set()
    review_path = previous.directory / "paper-review.json"
    if not review_path.is_file():
        return set()
    review = common.read_json(
        review_path,
        description=f"paper review for {previous.directory}",
    )
    findings = review.get("findings")
    if not isinstance(findings, list):
        raise common.CodexError("previous paper review has invalid findings")
    ids = {
        finding.get("id")
        for finding in findings
        if isinstance(finding, dict) and isinstance(finding.get("id"), str)
    }
    if len(ids) != len(findings):
        raise common.CodexError("previous paper review has invalid finding IDs")
    return ids


def validate_paper_result(
    result_path: Path,
    workspace: Path,
    inputs: Sequence[PaperInput],
    *,
    previous: DraftRef | None = None,
    authors: Sequence[str] | None = None,
) -> tuple[dict, list[Path]]:
    result = common.read_json(result_path, description="paper response")
    status = result.get("status")
    if status not in PAPER_STATUSES:
        raise common.CodexError("paper response has invalid status")
    for field in ("title", "summary"):
        if not isinstance(result.get(field), str) or not result[field].strip():
            raise common.CodexError(f"paper response has invalid {field}")
    _string_list(result, "unresolved_issues")
    _string_list(result, "warnings")
    expected_results = {item.result_id: item for item in inputs}
    rows = result.get("results")
    if not isinstance(rows, list):
        raise common.CodexError("paper response has invalid results")
    seen_results: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise common.CodexError("paper result entry is not an object")
        result_id = row.get("result_id")
        if result_id not in expected_results or result_id in seen_results:
            raise common.CodexError(
                f"paper response has invalid or duplicate result {result_id!r}"
            )
        seen_results.add(result_id)
        if row.get("disposition") not in {
            "included_main",
            "included_supporting",
            "excluded",
        }:
            raise common.CodexError(
                f"paper result {result_id} has invalid disposition"
            )
        claims = row.get("source_claim_ids")
        if not isinstance(claims, list) or not all(
            isinstance(claim, str) for claim in claims
        ) or len(set(claims)) != len(claims):
            raise common.CodexError(
                f"paper result {result_id} has invalid source_claim_ids"
            )
        available_claims = {
            claim.get("id")
            for claim in expected_results[result_id].attempt.solver_result.get(
                "checkable_claims", []
            )
            if isinstance(claim, dict)
        }
        if any(
            not CLAIM_ID_RE.fullmatch(claim) or claim not in available_claims
            for claim in claims
        ):
            raise common.CodexError(
                f"paper result {result_id} references an unknown solver claim"
            )
        labels = row.get("manuscript_labels")
        if not isinstance(labels, list) or not all(
            isinstance(label, str) and label.strip() for label in labels
        ):
            raise common.CodexError(
                f"paper result {result_id} has invalid manuscript_labels"
            )
        if row["disposition"] != "excluded" and (not claims or not labels):
            raise common.CodexError(
                f"included paper result {result_id} has no claims or labels"
            )
        if not isinstance(row.get("explanation"), str):
            raise common.CodexError(
                f"paper result {result_id} has invalid explanation"
            )
    if seen_results != set(expected_results):
        missing = set(expected_results).difference(seen_results)
        raise common.CodexError(
            "paper response omitted selected results: " + ", ".join(sorted(missing))
        )
    unresolved = result["unresolved_issues"]
    if status == "draft_complete" and (
        unresolved or any(row["disposition"] == "excluded" for row in rows)
    ):
        raise common.CodexError(
            "draft_complete paper has unresolved issues or excluded results"
        )
    if status == "blocked" and not unresolved:
        raise common.CodexError("blocked paper has no unresolved issues")
    previous_ids = _previous_finding_ids(previous)
    addressed = result.get("addressed_findings")
    if not isinstance(addressed, list):
        raise common.CodexError("paper response has invalid addressed_findings")
    addressed_ids: set[str] = set()
    for item in addressed:
        if not isinstance(item, dict):
            raise common.CodexError("addressed finding is not an object")
        finding_id = item.get("finding_id")
        if finding_id not in previous_ids or finding_id in addressed_ids:
            raise common.CodexError(
                f"paper response has invalid addressed finding {finding_id!r}"
            )
        addressed_ids.add(finding_id)
        if item.get("disposition") not in {
            "resolved",
            "not_resolved",
            "rejected",
        } or not isinstance(item.get("explanation"), str):
            raise common.CodexError(
                f"paper response has invalid disposition for {finding_id}"
            )
    if addressed_ids != previous_ids:
        raise common.CodexError(
            "paper response did not address every previous critic finding"
        )

    tex = _read_text(workspace / "main.tex", "generated main.tex")
    bib = _read_text(workspace / "references.bib", "generated references.bib")
    readiness = common.validate_markdown(
        workspace / "readiness.md",
        description="paper readiness report",
    )
    for required in (r"\documentclass", r"\begin{document}", r"\end{document}"):
        if required not in tex:
            raise common.CodexError(f"main.tex omits {required}")
    if not re.search(r"\\title\s*\{", tex):
        raise common.CodexError("main.tex has no title")
    if authors is not None and not authors and not re.search(
        r"\\author\s*\{\s*\}", tex
    ):
        raise common.CodexError("main.tex must use \\author{} when no author is supplied")
    if not re.search(r"\\begin\s*\{abstract\}", tex):
        raise common.CodexError("main.tex has no abstract")
    citation_keys = _tex_citation_keys(tex)
    bibliography_keys = _bib_keys(bib)
    if not citation_keys:
        raise common.CodexError("main.tex contains no citations")
    missing_bib = citation_keys.difference(bibliography_keys)
    if missing_bib:
        raise common.CodexError(
            "main.tex cites missing bibliography keys: "
            + ", ".join(sorted(missing_bib))
        )
    citations = result.get("citations")
    if not isinstance(citations, list):
        raise common.CodexError("paper response has invalid citations")
    origin_coverage: set[str] = set()
    structured_keys: set[str] = set()
    for citation in citations:
        if not isinstance(citation, dict):
            raise common.CodexError("paper citation entry is not an object")
        key = citation.get("bib_key")
        if (
            key in structured_keys
            or key not in bibliography_keys
            or key not in citation_keys
        ):
            raise common.CodexError(
                f"structured citation is duplicate, unused, or undefined: {key!r}"
            )
        structured_keys.add(key)
        if not isinstance(citation.get("url"), str) or not citation[
            "url"
        ].startswith(("https://", "http://")):
            raise common.CodexError(f"paper citation {key} has invalid URL")
        for field in ("title", "verification"):
            if not isinstance(citation.get(field), str) or not citation[field].strip():
                raise common.CodexError(
                    f"paper citation {key} has invalid {field}"
                )
        if citation.get("role") not in {
            "original_problem",
            "related_work",
            "technique",
            "other",
        }:
            raise common.CodexError(f"paper citation {key} has invalid role")
        result_ids = citation.get("result_ids")
        if not isinstance(result_ids, list) or any(
            result_id not in expected_results for result_id in result_ids
        ):
            raise common.CodexError(
                f"paper citation {key} has invalid result_ids"
            )
        if citation["role"] == "original_problem":
            origin_coverage.update(result_ids)
    if origin_coverage != set(expected_results):
        missing = set(expected_results).difference(origin_coverage)
        raise common.CodexError(
            "paper response lacks originating-paper citations for: "
            + ", ".join(sorted(missing))
        )
    if structured_keys != citation_keys or bibliography_keys != citation_keys:
        raise common.CodexError(
            "main.tex, references.bib, and structured citation keys do not match"
        )
    for result_id in expected_results:
        if result_id not in readiness:
            raise common.CodexError(f"readiness.md omits {result_id}")
    for row in rows:
        for claim_id in row["source_claim_ids"]:
            if claim_id not in readiness:
                raise common.CodexError(f"readiness.md omits {claim_id}")
        for label in row["manuscript_labels"]:
            if f"\\label{{{label}}}" not in tex:
                raise common.CodexError(
                    f"main.tex omits structured manuscript label {label}"
                )
    for finding_id in previous_ids:
        if finding_id not in readiness:
            raise common.CodexError(f"readiness.md omits {finding_id}")
    generated = _generated_files(workspace, result.get("generated_files"))
    result["generated_files"] = [
        path.relative_to(workspace).as_posix() for path in generated
    ]
    return result, generated


def resolve_latexmk(value: str) -> tuple[str, ...]:
    executable = shutil.which(value)
    if executable is not None:
        return (executable,)
    cygwin_bash = Path("C:/cygwin64/bin/bash.exe")
    if codex_cli.is_windows_host() and value == "latexmk" and cygwin_bash.is_file():
        try:
            available = subprocess.run(
                [str(cygwin_bash), "-c", "command -v latexmk"],
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError:
            available = None
        if available is not None and available.returncode == 0:
            return (
                str(cygwin_bash),
                "-c",
                'exec latexmk "$@"',
                "latexmk",
            )
    raise common.CodexError(
        f"could not find the LaTeX build executable {value!r} on PATH"
    )


def compile_latex(
    workspace: Path,
    latexmk: str | Sequence[str],
) -> Path:
    log_path = workspace / "build.log"
    command_prefix = [latexmk] if isinstance(latexmk, str) else list(latexmk)
    try:
        with log_path.open("w", encoding="utf-8") as log:
            completed = subprocess.run(
                [
                    *command_prefix,
                    "-pdf",
                    "-interaction=nonstopmode",
                    "-halt-on-error",
                    "-file-line-error",
                    "main.tex",
                ],
                cwd=workspace,
                stdout=log,
                stderr=subprocess.STDOUT,
                text=True,
                check=False,
            )
    except OSError as exc:
        raise common.CodexError(f"could not run latexmk: {exc}") from exc
    if completed.returncode != 0:
        raise common.CodexError(
            f"LaTeX build failed with status {completed.returncode}; "
            f"see {log_path}"
        )
    pdf = workspace / "main.pdf"
    if not pdf.is_file() or pdf.stat().st_size == 0:
        raise common.CodexError("LaTeX build did not create a nonempty main.pdf")
    final_tex_log = workspace / "main.log"
    inspected_log = final_tex_log if final_tex_log.is_file() else log_path
    log_text = _read_text(inspected_log, "final LaTeX log").lower()
    bad_patterns = (
        "there were undefined references",
        "undefined citations",
        "citation `",
    )
    if any(pattern in log_text for pattern in bad_patterns) and "undefined" in log_text:
        raise common.CodexError("LaTeX build log reports undefined citations or references")
    return pdf


def _draft_digest(draft: DraftRef) -> str:
    files = [
        (name, draft.directory / name)
        for name in (
            "main.tex",
            "references.bib",
            "readiness.md",
            "paper-result.json",
            "manifest.json",
        )
    ]
    main_pdf = draft.directory / "main.pdf"
    files.append(("main.pdf", main_pdf))
    figures = draft.directory / "figures"
    if figures.is_dir():
        files.extend(
            (
                path.relative_to(draft.directory).as_posix(),
                path,
            )
            for path in figures.rglob("*")
            if path.is_file()
        )
    return common.files_digest(files)


def paper_review_is_current(
    draft: DraftRef,
    inputs: Sequence[PaperInput],
    *,
    config_digest: str,
) -> bool:
    manifest = common.load_json(draft.directory / "paper-review-manifest.json")
    result = common.load_json(draft.directory / "paper-review.json")
    if manifest is None or result is None:
        return False
    return (
        manifest.get("schema_version") == 1
        and manifest.get("draft_digest") == _draft_digest(draft)
        and manifest.get("input_digest") == _input_digest(inputs)
        and manifest.get("config_digest") == config_digest
        and result.get("verdict") in PAPER_VERDICTS
    )


def next_draft_number(manuscript_directory: Path) -> int:
    numbers = []
    if manuscript_directory.is_dir():
        for path in manuscript_directory.iterdir():
            match = DRAFT_RE.fullmatch(path.name)
            if path.is_dir() and match:
                numbers.append(int(match.group(1)))
    return max(numbers, default=0) + 1


def _install_draft(
    manuscript_directory: Path,
    *,
    workspace: Path,
    result: dict,
    generated: Sequence[Path],
    draft_number: int,
    inputs: Sequence[PaperInput],
    previous: DraftRef | None,
    authors: Sequence[str],
    title_hint: str | None,
    revision_instruction: str | None,
    config_digest: str,
    codex_version: str,
    options: codex_cli.ModelOptions,
    web_search: str,
) -> DraftRef:
    destination = manuscript_directory / f"draft-{draft_number:03d}"
    if destination.exists():
        raise common.CodexError(f"draft destination already exists: {destination}")
    staging = Path(
        tempfile.mkdtemp(prefix=".draft-install-", dir=manuscript_directory)
    )
    try:
        for name in (
            "main.tex",
            "references.bib",
            "main.pdf",
            "readiness.md",
            "build.log",
            "events.jsonl",
            "run.log",
        ):
            shutil.copyfile(workspace / name, staging / name)
        for source in generated:
            relative = source.relative_to(workspace)
            target = staging / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
        common.write_json(staging / "paper-result.json", result)
        common.write_json(
            staging / "manifest.json",
            {
                "schema_version": 1,
                "generated_at": common.utc_now(),
                "draft_number": draft_number,
                "input_digest": _input_digest(inputs),
                "input_attempts": [
                    {
                        "result_id": item.result_id,
                        "attempt_path": str(item.attempt.directory),
                        "paper_directory": str(
                            item.attempt.problem.paper_directory
                        ),
                        "problem_id": item.attempt.problem.id,
                        "attempt_name": item.attempt.name,
                        "readiness_issues": list(item.readiness_issues),
                    }
                    for item in inputs
                ],
                "previous_draft": previous.directory.name if previous else None,
                "previous_draft_digest": (
                    _draft_digest(previous) if previous else None
                ),
                "authors": list(authors),
                "title_hint": title_hint,
                "revision_instruction": revision_instruction,
                "config_digest": config_digest,
                "codex_version": codex_version,
                "requested_model": options.model,
                "requested_reasoning_effort": options.reasoning_effort,
                "requested_fast_mode": options.fast,
                "requested_web_search": web_search,
                "status": result["status"],
                "title": result["title"],
            },
        )
        os.replace(staging, destination)
    except OSError as exc:
        raise common.CodexError(
            f"could not install paper draft; staging preserved at {staging}: {exc}"
        ) from exc
    return DraftRef(destination, draft_number, result)


def run_author_round(
    manuscript_directory: Path,
    inputs: Sequence[PaperInput],
    *,
    previous: DraftRef | None,
    authors: Sequence[str],
    title_hint: str | None,
    revision_instruction: str | None,
    codex: str,
    codex_version: str,
    latexmk: str | Sequence[str],
    prompt_template: str,
    schema_path: Path,
    config_digest: str,
    options: codex_cli.ModelOptions,
    web_search: str = "live",
    launch_interval: float = codex_cli.CODEX_LAUNCH_INTERVAL_SECONDS,
) -> DraftRef:
    manuscript_directory.mkdir(parents=True, exist_ok=True)
    workspace = Path(
        tempfile.mkdtemp(prefix=".write-run-", dir=manuscript_directory)
    ).resolve()
    draft_number = next_draft_number(manuscript_directory)
    try:
        context = stage_paper_context(workspace, inputs, manuscript=previous)
        prompt = render_writer_prompt(
            prompt_template,
            context=context,
            authors=authors,
            title_hint=title_hint,
            previous=previous,
            revision_instruction=revision_instruction,
        )
        result_path = codex_cli.run_structured_codex(
            codex=codex,
            workspace=workspace,
            prompt=prompt,
            schema_path=schema_path,
            options=options,
            web_search=web_search,
            launch_interval=launch_interval,
        )
        result, generated = validate_paper_result(
            result_path,
            workspace,
            inputs,
            previous=previous,
            authors=authors,
        )
        compile_latex(workspace, latexmk)
        draft = _install_draft(
            manuscript_directory,
            workspace=workspace,
            result=result,
            generated=generated,
            draft_number=draft_number,
            inputs=inputs,
            previous=previous,
            authors=authors,
            title_hint=title_hint,
            revision_instruction=revision_instruction,
            config_digest=config_digest,
            codex_version=codex_version,
            options=options,
            web_search=web_search,
        )
    except (common.CodexError, OSError) as exc:
        raise common.CodexError(
            common.preserved_workspace_message(exc, workspace)
        ) from exc
    common.cleanup_workspace(workspace, installed_log=draft.directory / "run.log")
    return draft


def validate_paper_review(
    result_path: Path,
    workspace: Path,
    inputs: Sequence[PaperInput],
) -> dict:
    result = common.read_json(result_path, description="paper-review response")
    verdict = result.get("verdict")
    if verdict not in PAPER_VERDICTS:
        raise common.CodexError("paper-review response has invalid verdict")
    if not isinstance(result.get("summary"), str) or not result["summary"].strip():
        raise common.CodexError("paper-review response has invalid summary")
    _string_list(result, "warnings")
    expected_results = {item.result_id: item for item in inputs}
    reviews = result.get("result_reviews")
    if not isinstance(reviews, list):
        raise common.CodexError("paper-review response has invalid result_reviews")
    reviewed: set[str] = set()
    for review in reviews:
        if not isinstance(review, dict):
            raise common.CodexError("paper result review is not an object")
        result_id = review.get("result_id")
        if result_id not in expected_results or result_id in reviewed:
            raise common.CodexError(
                f"paper review has invalid or duplicate result {result_id!r}"
            )
        reviewed.add(result_id)
        if review.get("assessment") not in {
            "supported",
            "partially_supported",
            "unsupported",
            "incorrect",
        } or not isinstance(review.get("explanation"), str):
            raise common.CodexError(
                f"paper review has invalid assessment for {result_id}"
            )
    if reviewed != set(expected_results):
        raise common.CodexError("paper review omitted selected results")
    findings = result.get("findings")
    if not isinstance(findings, list):
        raise common.CodexError("paper-review response has invalid findings")
    finding_ids: set[str] = set()
    all_claims = {
        claim.get("id")
        for item in inputs
        for claim in item.attempt.solver_result.get("checkable_claims", [])
        if isinstance(claim, dict)
    }
    for finding in findings:
        if not isinstance(finding, dict):
            raise common.CodexError("paper-review finding is not an object")
        finding_id = finding.get("id")
        if (
            not isinstance(finding_id, str)
            or not FINDING_ID_RE.fullmatch(finding_id)
            or finding_id in finding_ids
        ):
            raise common.CodexError(
                f"paper review has invalid or duplicate finding {finding_id!r}"
            )
        finding_ids.add(finding_id)
        if finding.get("severity") not in {"blocking", "major", "minor"}:
            raise common.CodexError(f"paper finding {finding_id} has invalid severity")
        if finding.get("category") not in {
            "proof",
            "theorem_statement",
            "citation",
            "novelty",
            "self_containment",
            "exposition",
            "latex",
            "scope",
            "other",
        }:
            raise common.CodexError(f"paper finding {finding_id} has invalid category")
        result_ids = finding.get("result_ids")
        claim_ids = finding.get("source_claim_ids")
        if not isinstance(result_ids, list) or any(
            result_id not in expected_results for result_id in result_ids
        ):
            raise common.CodexError(
                f"paper finding {finding_id} has invalid result_ids"
            )
        if not isinstance(claim_ids, list) or any(
            claim_id not in all_claims for claim_id in claim_ids
        ):
            raise common.CodexError(
                f"paper finding {finding_id} has invalid source_claim_ids"
            )
        for field in ("location", "explanation", "suggested_repair"):
            if not isinstance(finding.get(field), str):
                raise common.CodexError(
                    f"paper finding {finding_id} has invalid {field}"
                )
        if not isinstance(finding.get("requires_new_research"), bool):
            raise common.CodexError(
                f"paper finding {finding_id} has invalid requires_new_research"
            )
    action = result.get("recommended_action")
    if action not in {"revise", "new_research", "human_review"}:
        raise common.CodexError("paper-review response has invalid recommended_action")
    if verdict == "ready_for_expert_review":
        if action != "human_review" or any(
            finding["severity"] in {"blocking", "major"} for finding in findings
        ) or any(review["assessment"] != "supported" for review in reviews):
            raise common.CodexError(
                "ready_for_expert_review has major findings or wrong action"
            )
    if verdict in REVISION_VERDICTS and (action != "revise" or not findings):
        raise common.CodexError(
            "revision verdict must recommend revise and identify findings"
        )
    if verdict == "needs_research" and (
        action != "new_research"
        or not any(finding["requires_new_research"] for finding in findings)
    ):
        raise common.CodexError(
            "needs_research must identify a new-research finding"
        )
    critique = common.validate_markdown(
        workspace / "paper-critique.md",
        description="paper critique",
    )
    for finding_id in finding_ids:
        if finding_id not in critique:
            raise common.CodexError(f"paper-critique.md omits {finding_id}")
    return result


def run_paper_review(
    manuscript_directory: Path,
    draft: DraftRef,
    inputs: Sequence[PaperInput],
    *,
    codex: str,
    codex_version: str,
    prompt_template: str,
    schema_path: Path,
    config_digest: str,
    options: codex_cli.ModelOptions,
    web_search: str = "live",
    launch_interval: float = codex_cli.CODEX_LAUNCH_INTERVAL_SECONDS,
) -> PaperReview:
    workspace = Path(
        tempfile.mkdtemp(prefix=".paper-review-run-", dir=manuscript_directory)
    ).resolve()
    try:
        context = stage_paper_context(
            workspace,
            inputs,
            manuscript=draft,
            include_manuscript_review=False,
        )
        prompt = render_reviewer_prompt(prompt_template, context=context)
        result_path = codex_cli.run_structured_codex(
            codex=codex,
            workspace=workspace,
            prompt=prompt,
            schema_path=schema_path,
            options=options,
            web_search=web_search,
            launch_interval=launch_interval,
        )
        result = validate_paper_review(result_path, workspace, inputs)
        staging = Path(
            tempfile.mkdtemp(prefix=".paper-review-install-", dir=draft.directory)
        )
        try:
            shutil.copyfile(
                workspace / "paper-critique.md",
                staging / "paper-critique.md",
            )
            common.write_json(staging / "paper-review.json", result)
            common.write_json(
                staging / "paper-review-manifest.json",
                {
                    "schema_version": 1,
                    "generated_at": common.utc_now(),
                    "draft_digest": _draft_digest(draft),
                    "input_digest": _input_digest(inputs),
                    "config_digest": config_digest,
                    "codex_version": codex_version,
                    "requested_model": options.model,
                    "requested_reasoning_effort": options.reasoning_effort,
                    "requested_fast_mode": options.fast,
                    "requested_web_search": web_search,
                    "verdict": result["verdict"],
                },
            )
            shutil.copyfile(
                workspace / "events.jsonl",
                staging / "review-events.jsonl",
            )
            shutil.copyfile(workspace / "run.log", staging / "review-run.log")
            for name in (
                "paper-critique.md",
                "paper-review.json",
                "paper-review-manifest.json",
                "review-events.jsonl",
                "review-run.log",
            ):
                os.replace(staging / name, draft.directory / name)
            shutil.rmtree(staging)
        except OSError as exc:
            raise common.CodexError(
                f"could not install paper review; staging preserved at {staging}: {exc}"
            ) from exc
    except (common.CodexError, OSError) as exc:
        raise common.CodexError(
            common.preserved_workspace_message(exc, workspace)
        ) from exc
    common.cleanup_workspace(
        workspace,
        installed_log=draft.directory / "review-run.log",
    )
    return PaperReview(draft, result)


def run_pipeline(
    manuscript_directory: Path,
    inputs: Sequence[PaperInput],
    *,
    previous: DraftRef | None,
    authors: Sequence[str],
    title_hint: str | None,
    revision_instruction: str | None = None,
    max_rounds: int,
    codex: str,
    codex_version: str,
    latexmk: str | Sequence[str],
    prompt_template: str,
    schema_path: Path,
    config_digest: str,
    options: codex_cli.ModelOptions,
    review_prompt_template: str,
    review_schema_path: Path,
    review_config_digest: str,
    review_options: codex_cli.ModelOptions,
    web_search: str = "live",
    review_web_search: str = "live",
) -> PipelineOutcome:
    drafts: list[DraftRef] = []
    final_review: PaperReview | None = None
    current_previous = previous
    if current_previous is not None and revision_instruction is None:
        reviewed_now = False
        saved_review = (
            common.load_json(current_previous.directory / "paper-review.json")
            if paper_review_is_current(
                current_previous,
                inputs,
                config_digest=review_config_digest,
            )
            else None
        )
        if saved_review is None:
            final_review = run_paper_review(
                manuscript_directory,
                current_previous,
                inputs,
                codex=codex,
                codex_version=codex_version,
                prompt_template=review_prompt_template,
                schema_path=review_schema_path,
                config_digest=review_config_digest,
                options=review_options,
                web_search=review_web_search,
            )
            saved_review = final_review.result
            reviewed_now = True
        if reviewed_now and saved_review.get("verdict") == "ready_for_expert_review":
            return PipelineOutcome(
                tuple(drafts),
                final_review,
                "ready_for_expert_review",
            )
        if saved_review.get("verdict") in {"invalid", "needs_research"}:
            return PipelineOutcome(
                tuple(drafts),
                final_review or PaperReview(current_previous, saved_review),
                saved_review["verdict"],
            )
    for _ in range(max_rounds):
        draft = run_author_round(
            manuscript_directory,
            inputs,
            previous=current_previous,
            authors=authors,
            title_hint=title_hint,
            revision_instruction=revision_instruction,
            codex=codex,
            codex_version=codex_version,
            latexmk=latexmk,
            prompt_template=prompt_template,
            schema_path=schema_path,
            config_digest=config_digest,
            options=options,
            web_search=web_search,
        )
        drafts.append(draft)
        final_review = run_paper_review(
            manuscript_directory,
            draft,
            inputs,
            codex=codex,
            codex_version=codex_version,
            prompt_template=review_prompt_template,
            schema_path=review_schema_path,
            config_digest=review_config_digest,
            options=review_options,
            web_search=review_web_search,
        )
        verdict = final_review.result["verdict"]
        if verdict == "ready_for_expert_review":
            return PipelineOutcome(tuple(drafts), final_review, verdict)
        if verdict not in REVISION_VERDICTS:
            return PipelineOutcome(tuple(drafts), final_review, verdict)
        current_previous = draft
    return PipelineOutcome(tuple(drafts), final_review, "maximum rounds reached")


def _inherit_options(
    primary: codex_cli.ModelOptions,
    secondary: codex_cli.ModelOptions,
) -> codex_cli.ModelOptions:
    return codex_cli.ModelOptions(
        secondary.model or primary.model,
        secondary.reasoning_effort or primary.reasoning_effort,
        secondary.fast or primary.fast,
    )


def _load_draft(path: Path) -> DraftRef:
    directory = path.expanduser().resolve()
    match = DRAFT_RE.fullmatch(directory.name)
    if not directory.is_dir() or match is None:
        raise common.CodexError("--revise must name a draft-NNN directory")
    result = common.read_json(
        directory / "paper-result.json",
        description=f"paper result for {directory}",
    )
    return DraftRef(directory, int(match.group(1)), result)


def _revision_inputs(draft: DraftRef) -> tuple[list[PaperInput], dict]:
    manifest = common.read_json(
        draft.directory / "manifest.json",
        description=f"draft manifest for {draft.directory}",
    )
    records = manifest.get("input_attempts")
    if not isinstance(records, list) or not all(
        isinstance(record, dict) and isinstance(record.get("attempt_path"), str)
        for record in records
    ):
        raise common.CodexError("draft manifest has invalid input_attempts")
    inputs = load_paper_inputs(
        [Path(record["attempt_path"]) for record in records],
    )
    return inputs, manifest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "write one paper from explicitly selected papers, open problems, "
            "or attempts"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  Write and independently review one paper, revising up to three rounds:
    python src/write_paper.py \\
      papers/edemaine/arXiv-.../OP-001/attempt-003

  Use the latest attempt for every attempted problem in one paper:
    python src/write_paper.py papers/edemaine/arXiv-...

  Combine two solved problems in one manuscript:
    python src/write_paper.py \\
      papers/edemaine/arXiv-.../OP-001/attempt-003 \\
      papers/edemaine/arXiv-.../OP-004/attempt-002 \\
      --name combined-result --author "A. Author"

  Continue from an independently reviewed draft:
    python src/write_paper.py --revise manuscripts/.../draft-001 \\
      --max-rounds 2
""",
    )
    parser.add_argument(
        "attempts",
        nargs="*",
        type=Path,
        help=(
            "analyzed paper, OP-NNN, or attempt-NNN directories; papers and "
            "problems select their latest attempts"
        ),
    )
    parser.add_argument(
        "--revise",
        type=Path,
        help="reviewed draft-NNN to revise instead of starting a manuscript",
    )
    parser.add_argument(
        "--prompt",
        dest="revision_instruction",
        metavar="TEXT",
        help="explicit revision direction; requires --revise",
    )
    parser.add_argument(
        "--name",
        help="manuscript directory name (default: paper dirname plus OP IDs)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIRECTORY,
        help=f"parent for new manuscripts (default: {DEFAULT_OUTPUT_DIRECTORY})",
    )
    parser.add_argument(
        "--author",
        action="append",
        dest="authors",
        metavar="NAME",
        help="manuscript author; may be repeated (default: empty author)",
    )
    parser.add_argument(
        "--title",
        dest="title_hint",
        help="optional short title to use or refine",
    )
    parser.add_argument(
        "--max-rounds",
        type=codex_cli.positive_integer,
        default=3,
        help=(
            "maximum new author-review rounds in this invocation, including "
            "the first draft or revision (default: 3)"
        ),
    )
    parser.add_argument(
        "--allow-not-ready",
        action="store_true",
        help="deprecated no-op; readiness findings are always warnings",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="validate and report the manuscript plan without starting Codex",
    )
    parser.add_argument(
        "--codex",
        default="codex",
        help="Codex CLI executable or command name (default: codex)",
    )
    parser.add_argument(
        "--latexmk",
        default="latexmk",
        help="latexmk executable or command name (default: latexmk)",
    )
    parser.add_argument(
        "--prompt-template",
        type=Path,
        default=DEFAULT_PROMPT_PATH,
        help=f"writer prompt-template path (default: {DEFAULT_PROMPT_PATH})",
    )
    parser.add_argument("--schema", type=Path, default=DEFAULT_SCHEMA_PATH)
    parser.add_argument(
        "--review-prompt",
        type=Path,
        default=DEFAULT_REVIEW_PROMPT_PATH,
    )
    parser.add_argument(
        "--review-schema",
        type=Path,
        default=DEFAULT_REVIEW_SCHEMA_PATH,
    )
    codex_cli.add_model_arguments(parser)
    codex_cli.add_model_arguments(parser, prefix="review")
    codex_cli.add_web_search_argument(parser, default="live")
    codex_cli.add_web_search_argument(parser, default="live", prefix="review")
    return parser


def main(argv: list[str] | None = None) -> int:
    codex_cli.configure_utf8_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if bool(args.attempts) == bool(args.revise):
            raise common.CodexError(
                "provide one or more paper/problem/attempt directories, or "
                "--revise, but not both"
            )
        if args.revision_instruction is not None:
            if not args.revision_instruction.strip():
                raise common.CodexError("--prompt must be nonempty")
            if not args.revise:
                raise common.CodexError("--prompt requires --revise")
        if args.revise:
            selection_warnings: list[str] = []
            previous = _load_draft(args.revise)
            inputs, prior_manifest = _revision_inputs(previous)
            manuscript_directory = previous.directory.parent
            if args.name:
                raise common.CodexError("--name cannot be used with --revise")
            authors = args.authors or prior_manifest.get("authors") or []
            title_hint = (
                args.title_hint
                if args.title_hint is not None
                else prior_manifest.get("title_hint")
            )
        else:
            previous = None
            attempt_paths, selection_warnings = expand_attempt_inputs(args.attempts)
            inputs = load_paper_inputs(attempt_paths)
            name = validate_manuscript_name(
                args.name or derive_manuscript_name(inputs)
            )
            manuscript_directory = args.output_dir.expanduser().resolve() / name
            if manuscript_directory.exists() and any(
                DRAFT_RE.fullmatch(path.name)
                for path in manuscript_directory.iterdir()
                if path.is_dir()
            ):
                raise common.CodexError(
                    f"manuscript already has drafts: {manuscript_directory}; "
                    "use --revise on a reviewed draft or choose --name"
                )
            authors = args.authors or []
            title_hint = args.title_hint
        if not all(isinstance(author, str) and author.strip() for author in authors):
            raise common.CodexError("every --author must be nonempty")

        prompt_path = args.prompt_template.expanduser().resolve()
        schema_path = args.schema.expanduser().resolve()
        prompt_template = _read_text(prompt_path, "paper prompt")
        schema_text = _read_text(schema_path, "paper schema")
        json.loads(schema_text)
        options = codex_cli.model_options_from_args(args)
        config_digest = codex_cli.semantic_config_digest(
            prompt_template,
            schema_text,
            options,
            web_search=args.web_search,
        )
        review_prompt_path = args.review_prompt.expanduser().resolve()
        review_schema_path = args.review_schema.expanduser().resolve()
        review_prompt = _read_text(review_prompt_path, "paper-review prompt")
        review_schema_text = _read_text(
            review_schema_path,
            "paper-review schema",
        )
        json.loads(review_schema_text)
        review_options = _inherit_options(
            options,
            codex_cli.model_options_from_args(args, prefix="review"),
        )
        review_web_search = args.review_web_search or args.web_search
        review_config_digest = codex_cli.semantic_config_digest(
            review_prompt,
            review_schema_text,
            review_options,
            web_search=review_web_search,
        )
    except (
        common.CodexError,
        OSError,
        UnicodeError,
        json.JSONDecodeError,
    ) as exc:
        parser.error(str(exc))

    if args.dry_run:
        print(f"Would write one manuscript: {manuscript_directory}")
        for warning in selection_warnings:
            print(f"  warning: {warning}")
        for item in inputs:
            print(
                f"  {item.result_id}: {item.attempt.problem.paper_directory.name} "
                f"{item.attempt.problem.id}/{item.attempt.name}"
            )
            for issue in item.readiness_issues:
                print(f"    warning: {issue}")
        print(
            f"Would create and independently review at most "
            f"{args.max_rounds} new draft(s)."
        )
        if args.revision_instruction is not None:
            print(f"Revision direction: {args.revision_instruction}")
        return 0

    try:
        codex = codex_cli.resolve_codex_executable(args.codex)
        codex_version = codex_cli.read_codex_version(codex)
        latexmk = resolve_latexmk(args.latexmk)
        manuscript_directory.mkdir(parents=True, exist_ok=True)
        print(
            f"Writing one manuscript from {len(inputs)} selected result(s), "
            f"with at most {args.max_rounds} author-review round(s).",
            flush=True,
        )
        warning_rows = [*selection_warnings]
        warning_rows.extend(
            f"{item.attempt.problem.paper_directory.name}/"
            f"{item.attempt.problem.id}/{item.attempt.name}: {issue}"
            for item in inputs
            for issue in item.readiness_issues
        )
        if warning_rows:
            print("Upstream readiness warnings (continuing):", file=sys.stderr)
            for warning in warning_rows:
                print(f"  {warning}", file=sys.stderr)
        outcome = run_pipeline(
            manuscript_directory,
            inputs,
            previous=previous,
            authors=authors,
            title_hint=title_hint,
            revision_instruction=args.revision_instruction,
            max_rounds=args.max_rounds,
            codex=codex,
            codex_version=codex_version,
            latexmk=latexmk,
            prompt_template=prompt_template,
            schema_path=schema_path,
            config_digest=config_digest,
            options=options,
            review_prompt_template=review_prompt,
            review_schema_path=review_schema_path,
            review_config_digest=review_config_digest,
            review_options=review_options,
            web_search=args.web_search,
            review_web_search=review_web_search,
        )
    except (common.CodexError, OSError) as exc:
        print(f"Paper writing failed: {exc}", file=sys.stderr)
        return 1

    for draft in outcome.drafts:
        review = common.load_json(draft.directory / "paper-review.json")
        if review is None:
            detail = draft.result.get("status", "unknown")
        else:
            detail = review.get("verdict", "unknown")
        print(f"Draft: {draft.directory} ({detail})")
    if outcome.ready and outcome.final_review is not None:
        print("Ready for human expert review:")
        print(f"  {outcome.final_review.draft.directory / 'main.pdf'}")
        print(f"  {outcome.final_review.draft.directory / 'paper-critique.md'}")
        return 0
    print(f"Stopped: {outcome.reason}", file=sys.stderr)
    if outcome.final_review is not None:
        print(
            f"Inspect: {outcome.final_review.draft.directory / 'paper-critique.md'}",
            file=sys.stderr,
        )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
