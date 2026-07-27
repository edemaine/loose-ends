#!/usr/bin/env python3
"""Shared paper, open-problem, history, and workspace helpers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
from typing import Iterable, Sequence

import analyze_papers
import codex_cli


ATTEMPTS_DIRECTORY = "attempts"
TRIAGE_MARKDOWN = "triage.md"
TRIAGE_RESULT = "triage.json"
TRIAGE_MANIFEST = "triage-manifest.json"
TRIAGE_MANIFEST_SCHEMA_VERSION = 2
TRIAGE_RUN_FILES = ("triage-events.jsonl", "triage-run.log")
ATTEMPT_DIRECTORY_RE = re.compile(r"^attempt-([0-9]{3,})$")
ATTEMPT_HISTORY_FILES = (
    "attempt.md",
    "solver-result.json",
    "manifest.json",
    "critique.md",
    "review-result.json",
    "review-manifest.json",
)
ANALYSIS_FILES = (
    "summary.md",
    "results.md",
    "open-problems.md",
    "manifest.json",
)
EXPLICITNESS_VALUES = ("explicit", "inferred", "uncertain")
CodexError = codex_cli.CodexError


@dataclass(frozen=True)
class ProblemRef:
    paper_directory: Path
    paper_title: str
    paper_authors: tuple[str, ...]
    problem: dict

    @property
    def id(self) -> str:
        return self.problem["id"]

    @property
    def title(self) -> str:
        return self.problem["title"]

    @property
    def explicitness(self) -> str:
        return self.problem["explicitness"]

    @property
    def directory(self) -> Path:
        return (
            self.paper_directory
            / ATTEMPTS_DIRECTORY
            / self.id
        )


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_json(path: Path, *, description: str | None = None) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        label = description or str(path)
        raise CodexError(f"missing {label}: {path}") from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        label = description or str(path)
        raise CodexError(f"could not read {label}: {exc}") from exc
    if not isinstance(value, dict):
        label = description or str(path)
        raise CodexError(f"{label} is not a JSON object: {path}")
    return value


def write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def load_json(path: Path) -> dict | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def load_analysis_manifest(paper_directory: Path) -> dict:
    manifest_path = (
        paper_directory / analyze_papers.ANALYSIS_DIRECTORY / "manifest.json"
    )
    manifest = read_json(manifest_path, description="paper analysis manifest")
    problems = manifest.get("open_problems")
    if not isinstance(problems, list):
        raise CodexError(
            f"analysis manifest has no open-problem list: {manifest_path}"
        )
    return manifest


def discover_problem_refs(
    paths: Iterable[Path],
    *,
    problem_ids: set[str] | None = None,
    explicitness: set[str] | None = None,
) -> list[ProblemRef]:
    """Discover analyzed open problems under direct papers or parent paths."""
    papers = analyze_papers.discover_paper_directories(paths)
    selected: list[ProblemRef] = []
    analyzed_papers = 0
    for paper in papers:
        manifest_path = paper / "analysis" / "manifest.json"
        if not manifest_path.is_file():
            continue
        manifest = load_analysis_manifest(paper)
        analyzed_papers += 1
        title = manifest.get("paper_title")
        authors = manifest.get("paper_authors")
        if not isinstance(title, str) or not title.strip():
            raise CodexError(f"analysis manifest has no paper title: {manifest_path}")
        if not analyze_papers.valid_paper_authors(authors):
            raise CodexError(
                f"analysis manifest has invalid paper authors: {manifest_path}"
            )
        seen: set[str] = set()
        for raw_problem in manifest["open_problems"]:
            if not isinstance(raw_problem, dict):
                raise CodexError(
                    f"invalid open-problem entry in {manifest_path}"
                )
            problem_id = raw_problem.get("id")
            if (
                not isinstance(problem_id, str)
                or not analyze_papers.OPEN_PROBLEM_ID_RE.fullmatch(problem_id)
                or problem_id in seen
            ):
                raise CodexError(
                    f"invalid or duplicate open-problem ID in "
                    f"{manifest_path}: {problem_id!r}"
                )
            seen.add(problem_id)
            problem_title = raw_problem.get("title")
            problem_explicitness = raw_problem.get("explicitness")
            if not isinstance(problem_title, str) or not problem_title.strip():
                raise CodexError(
                    f"open problem {problem_id} has no title in {manifest_path}"
                )
            if problem_explicitness not in EXPLICITNESS_VALUES:
                raise CodexError(
                    f"open problem {problem_id} has invalid explicitness in "
                    f"{manifest_path}"
                )
            if problem_ids is not None and problem_id not in problem_ids:
                continue
            if (
                explicitness is not None
                and raw_problem.get("explicitness") not in explicitness
            ):
                continue
            selected.append(
                ProblemRef(
                    paper.resolve(),
                    title.strip(),
                    tuple(authors),
                    dict(raw_problem),
                )
            )
    if not selected:
        if analyzed_papers == 0:
            raise CodexError(
                "none of the discovered papers has an analysis/manifest.json"
            )
        qualifier = " matching the requested filters" if problem_ids or explicitness else ""
        raise CodexError(f"no open problems were found{qualifier}")
    return selected


def group_by_paper(
    problems: Iterable[ProblemRef],
) -> dict[Path, list[ProblemRef]]:
    grouped: dict[Path, list[ProblemRef]] = {}
    for problem in problems:
        grouped.setdefault(problem.paper_directory, []).append(problem)
    for values in grouped.values():
        values.sort(key=lambda item: item.id)
    return dict(sorted(grouped.items(), key=lambda item: os.path.normcase(str(item[0]))))


def _hash_file(digest, path: Path, relative: str) -> None:
    digest.update(relative.encode("utf-8", errors="surrogateescape"))
    digest.update(b"\0")
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    digest.update(b"\0")


def files_digest(files: Sequence[tuple[str, Path]]) -> str:
    digest = hashlib.sha256()
    for relative, path in sorted(files, key=lambda item: item[0]):
        if not path.is_file():
            digest.update(relative.encode("utf-8", errors="surrogateescape"))
            digest.update(b"\0missing\0")
            continue
        _hash_file(digest, path, relative)
    return digest.hexdigest()


def stable_value_digest(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def analysis_digest(paper_directory: Path) -> str:
    analysis = paper_directory / "analysis"
    return files_digest([(name, analysis / name) for name in ANALYSIS_FILES])


def problem_digest(problem: ProblemRef) -> str:
    return stable_value_digest(
        {
            "analysis_digest": analysis_digest(problem.paper_directory),
            "paper_title": problem.paper_title,
            "paper_authors": problem.paper_authors,
            "problem": problem.problem,
        }
    )


def attempt_directories(problem: ProblemRef) -> list[Path]:
    if not problem.directory.is_dir():
        return []
    attempts: list[tuple[int, Path]] = []
    for path in problem.directory.iterdir():
        match = ATTEMPT_DIRECTORY_RE.fullmatch(path.name)
        if path.is_dir() and match:
            attempts.append((int(match.group(1)), path))
    return [path for _, path in sorted(attempts)]


def next_attempt_number(problem: ProblemRef) -> int:
    numbers = [
        int(match.group(1))
        for path in attempt_directories(problem)
        if (match := ATTEMPT_DIRECTORY_RE.fullmatch(path.name))
    ]
    return max(numbers, default=0) + 1


def _attempt_history_paths(problem: ProblemRef) -> list[tuple[str, Path]]:
    files: list[tuple[str, Path]] = []
    for attempt in attempt_directories(problem):
        for name in ATTEMPT_HISTORY_FILES:
            path = attempt / name
            if path.is_file():
                files.append((f"{attempt.name}/{name}", path))
        artifacts = attempt / "artifacts"
        if artifacts.is_dir():
            for path in artifacts.rglob("*"):
                if path.is_file():
                    relative = path.relative_to(problem.directory).as_posix()
                    files.append((relative, path))
    return files


def attempt_history_digest(problem: ProblemRef) -> str:
    return files_digest(_attempt_history_paths(problem))


def solver_attempt_digest(attempt_directory: Path) -> str:
    """Hash solver-owned content while excluding any later review."""
    files: list[tuple[str, Path]] = []
    for name in ("attempt.md", "solver-result.json", "manifest.json"):
        files.append((name, attempt_directory / name))
    artifacts = attempt_directory / "artifacts"
    if artifacts.is_dir():
        for path in artifacts.rglob("*"):
            if path.is_file():
                files.append(
                    (path.relative_to(attempt_directory).as_posix(), path)
                )
    return files_digest(files)


def triage_input_digest(problem: ProblemRef) -> str:
    return stable_value_digest(
        {
            "problem_digest": problem_digest(problem),
            "attempt_history_digest": attempt_history_digest(problem),
        }
    )


def triage_manifest(problem: ProblemRef) -> dict | None:
    return load_json(problem.directory / TRIAGE_MANIFEST)


def triage_result(problem: ProblemRef) -> dict | None:
    return load_json(problem.directory / TRIAGE_RESULT)


def triage_is_current(
    problem: ProblemRef,
    *,
    config_digest: str | None = None,
) -> bool:
    manifest = triage_manifest(problem)
    result = triage_result(problem)
    if manifest is None or result is None:
        return False
    if manifest.get("schema_version") != TRIAGE_MANIFEST_SCHEMA_VERSION:
        return False
    if manifest.get("input_digest") != triage_input_digest(problem):
        return False
    if (
        config_digest is not None
        and manifest.get("config_digest") != config_digest
    ):
        return False
    return result.get("problem_id") == problem.id


def _copy_file(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)


def _copy_attempt_history(
    problem: ProblemRef,
    destination: Path,
    *,
    exclude_review_for: Path | None = None,
) -> None:
    for relative, source in _attempt_history_paths(problem):
        if (
            exclude_review_for is not None
            and source.parent == exclude_review_for
            and source.name
            in {
                "critique.md",
                "review-result.json",
                "review-manifest.json",
            }
        ):
            continue
        _copy_file(source, destination / relative)


def stage_context(
    workspace: Path,
    problems: Sequence[ProblemRef],
    *,
    include_paper: bool,
    include_history: bool = True,
    include_triage: bool = False,
    exclude_review_for: Path | None = None,
) -> Path:
    """Stage disposable, sandbox-readable context for one paper."""
    if not problems:
        raise CodexError("cannot stage an empty open-problem set")
    paper = problems[0].paper_directory
    if any(problem.paper_directory != paper for problem in problems):
        raise CodexError("one workspace cannot mix open problems from different papers")
    inputs = workspace / "inputs"
    inputs.mkdir(parents=True, exist_ok=False)

    if include_paper:
        paper_input = inputs / "paper"
        paper_input.mkdir()
        for name in ("paper.pdf", "source", "metadata.json", "PDF_ONLY"):
            source = paper / name
            destination = paper_input / name
            if source.is_dir():
                shutil.copytree(source, destination)
            elif source.is_file():
                shutil.copyfile(source, destination)

    analysis_input = inputs / "analysis"
    analysis_input.mkdir()
    for name in ANALYSIS_FILES:
        source = paper / "analysis" / name
        if not source.is_file():
            raise CodexError(f"analysis input is missing: {source}")
        shutil.copyfile(source, analysis_input / name)

    write_json(
        inputs / "problems.json",
        {
            "paper_directory_name": paper.name,
            "paper_title": problems[0].paper_title,
            "paper_authors": list(problems[0].paper_authors),
            "problems": [problem.problem for problem in problems],
        },
    )

    if include_history:
        for problem in problems:
            destination = inputs / "history" / problem.id
            _copy_attempt_history(
                problem,
                destination,
                exclude_review_for=exclude_review_for,
            )

    if include_triage:
        for problem in problems:
            destination = inputs / "triage" / problem.id
            for name in (TRIAGE_MARKDOWN, TRIAGE_RESULT, TRIAGE_MANIFEST):
                source = problem.directory / name
                if source.is_file():
                    _copy_file(source, destination / name)

    codex_cli.grant_sandbox_read_access(inputs)
    return inputs


def validate_markdown(path: Path, *, description: str) -> str:
    try:
        contents = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise CodexError(f"Codex did not create {description}: {path.name}") from exc
    except (OSError, UnicodeError) as exc:
        raise CodexError(f"could not read generated {description}: {exc}") from exc
    if not contents.strip():
        raise CodexError(f"Codex created an empty {description}")
    if not contents.lstrip().startswith("#"):
        raise CodexError(f"generated {description} does not start with a heading")
    return contents


def cleanup_workspace(
    workspace: Path,
    *,
    installed_log: Path | None = None,
) -> str | None:
    try:
        shutil.rmtree(workspace)
    except OSError as exc:
        warning = f"could not remove temporary workspace {workspace}: {exc}"
        if installed_log is not None:
            try:
                with installed_log.open("a", encoding="utf-8") as log:
                    log.write(f"\nDriver cleanup warning: {warning}\n")
            except OSError:
                pass
        return warning
    return None


def preserved_workspace_message(exc: BaseException, workspace: Path) -> str:
    message = str(exc)
    if str(workspace) in message:
        return message
    return f"{message}; workspace preserved at {workspace}"


def parse_csv_values(
    value: str,
    *,
    allowed: Sequence[str],
    label: str,
) -> set[str]:
    values = {item.strip() for item in value.split(",") if item.strip()}
    invalid = values.difference(allowed)
    if not values or invalid:
        choices = ", ".join(allowed)
        detail = f"; invalid: {', '.join(sorted(invalid))}" if invalid else ""
        raise CodexError(f"{label} must contain one or more of {choices}{detail}")
    return values
