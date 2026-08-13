#!/usr/bin/env python3
"""Shared paper, open-problem, history, and workspace helpers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
from typing import Iterable, Sequence

import analyze_papers
import codex_cli


RUNS_DIRECTORY = ".runs"
TRIAGE_MARKDOWN = "triage.md"
TRIAGE_RESULT = "triage.json"
TRIAGE_MANIFEST = "triage-manifest.json"
TRIAGE_MANIFEST_SCHEMA_VERSION = 2
TRIAGE_RUN_FILES = ("triage-events.jsonl", "triage-run.log")
LITERATURE_MARKDOWN = "literature.md"
LITERATURE_RESULT = "literature.json"
LITERATURE_MANIFEST = "literature-manifest.json"
LITERATURE_MANIFEST_SCHEMA_VERSION = 1
LITERATURE_RUN_FILES = ("literature-events.jsonl", "literature-run.log")
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
CLAIMED_RESULT_TYPES = (
    "none",
    "obstruction",
    "partial_result",
    "solution",
    "counterexample",
)
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
        return self.paper_directory / self.id


def direct_problem_inputs(
    paths: Sequence[Path],
) -> tuple[list[Path], set[tuple[str, str]]]:
    """Translate PAPER/OP-NNN inputs into paper roots and exact selectors."""
    discovery_paths: list[Path] = []
    selectors: set[tuple[str, str]] = set()
    for raw_path in paths:
        path = raw_path.expanduser().resolve()
        if (
            analyze_papers.OPEN_PROBLEM_ID_RE.fullmatch(path.name)
            and (path.parent / "analysis" / "manifest.json").is_file()
        ):
            paper = path.parent
            discovery_paths.append(paper)
            selectors.add((os.path.normcase(str(paper)), path.name))
        else:
            discovery_paths.append(path)
    return list(dict.fromkeys(discovery_paths)), selectors


def direct_review_inputs(
    paths: Sequence[Path],
) -> tuple[
    list[Path],
    set[tuple[str, str]],
    set[tuple[str, str, str]],
]:
    """Resolve direct problem and attempt paths for the review command."""
    discovery_paths: list[Path] = []
    whole_problems: set[tuple[str, str]] = set()
    exact_attempts: set[tuple[str, str, str]] = set()
    for raw_path in paths:
        path = raw_path.expanduser().resolve()
        if (
            ATTEMPT_DIRECTORY_RE.fullmatch(path.name)
            and analyze_papers.OPEN_PROBLEM_ID_RE.fullmatch(path.parent.name)
            and (path.parent.parent / "analysis" / "manifest.json").is_file()
        ):
            paper = path.parent.parent
            paper_key = os.path.normcase(str(paper))
            discovery_paths.append(paper)
            exact_attempts.add((paper_key, path.parent.name, path.name))
            continue
        normalized, selectors = direct_problem_inputs([path])
        discovery_paths.extend(normalized)
        whole_problems.update(selectors)
    return (
        list(dict.fromkeys(discovery_paths)),
        whole_problems,
        exact_attempts,
    )


def filter_exact_problems(
    problems: Iterable[ProblemRef],
    selectors: set[tuple[str, str]],
) -> list[ProblemRef]:
    """Restrict discovered problems to exact paper/problem selectors."""
    if not selectors:
        return list(problems)
    selected = [
        problem
        for problem in problems
        if (
            os.path.normcase(str(problem.paper_directory)),
            problem.id,
        ) in selectors
    ]
    if len(selected) != len(selectors):
        found = {
            (os.path.normcase(str(problem.paper_directory)), problem.id)
            for problem in selected
        }
        missing = sorted(
            f"{paper}/{problem_id}"
            for paper, problem_id in selectors - found
        )
        raise CodexError(
            "direct problem path does not match an extracted problem: "
            + ", ".join(missing)
        )
    return selected


def paper_runs_directory(paper_directory: Path) -> Path:
    """Return the home for preserved paper-level batch workspaces."""
    return paper_directory / RUNS_DIRECTORY


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def claimed_result_type(result: dict) -> str:
    """Return a current solver result's mathematical claim type."""
    claimed = result.get("claimed_result_type")
    if claimed in CLAIMED_RESULT_TYPES:
        return claimed
    return "unknown"


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


def repair_problem_data_access(
    paper_directories: Iterable[Path],
    *,
    codex_command: str,
) -> tuple[str | None, int]:
    """Repair restrictive sandbox ACLs in generated paper data on Windows."""
    if not codex_cli.is_windows_host():
        return None, 0
    inaccessible: list[Path] = []
    for paper in paper_directories:
        directories = [paper / "analysis", paper_runs_directory(paper)]
        try:
            directories.extend(
                child
                for child in paper.iterdir()
                if child.is_dir()
                and analyze_papers.OPEN_PROBLEM_ID_RE.fullmatch(child.name)
            )
        except OSError:
            directories.append(paper)
        for directory in directories:
            if (
                directory.is_dir()
                and not codex_cli.workspace_is_user_accessible(directory)
            ):
                inaccessible.append(directory)
    if not inaccessible:
        return None, 0
    codex = codex_cli.resolve_codex_executable(codex_command)
    for directory in inaccessible:
        codex_cli.normalize_workspace_access(directory, codex)
    return codex, len(inaccessible)


def _hash_file(digest, path: Path, relative: str) -> None:
    digest.update(relative.encode("utf-8", errors="surrogateescape"))
    digest.update(b"\0")
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    digest.update(b"\0")


@lru_cache(maxsize=4096)
def _files_digest_from_signature(
    signature: tuple[tuple[str, str, int, int] | tuple[str, None, int, int], ...],
) -> str:
    digest = hashlib.sha256()
    for relative, path_value, _, _ in signature:
        if path_value is None:
            digest.update(relative.encode("utf-8", errors="surrogateescape"))
            digest.update(b"\0missing\0")
            continue
        _hash_file(digest, Path(path_value), relative)
    return digest.hexdigest()


def files_digest(files: Sequence[tuple[str, Path]]) -> str:
    signature = []
    for relative, path in sorted(files, key=lambda item: item[0]):
        try:
            status = path.stat()
        except OSError:
            status = None
        if status is None or not stat.S_ISREG(status.st_mode):
            signature.append((relative, None, 0, 0))
        else:
            signature.append(
                (
                    relative,
                    str(path),
                    status.st_mtime_ns,
                    status.st_size,
                )
            )
    return _files_digest_from_signature(tuple(signature))


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


def problem_digest(
    problem: ProblemRef,
    *,
    paper_analysis_digest: str | None = None,
) -> str:
    return stable_value_digest(
        {
            "analysis_digest": (
                paper_analysis_digest
                if paper_analysis_digest is not None
                else analysis_digest(problem.paper_directory)
            ),
            "paper_title": problem.paper_title,
            "paper_authors": problem.paper_authors,
            "problem": problem.problem,
        }
    )


def attempt_directories(problem: ProblemRef) -> list[Path]:
    attempts: list[tuple[int, Path]] = []
    try:
        with os.scandir(problem.directory) as entries:
            for entry in entries:
                match = ATTEMPT_DIRECTORY_RE.fullmatch(entry.name)
                if match and entry.is_dir():
                    attempts.append((int(match.group(1)), Path(entry.path)))
    except FileNotFoundError:
        return []
    return [path for _, path in sorted(attempts)]


def next_attempt_number(problem: ProblemRef) -> int:
    numbers = [
        int(match.group(1))
        for path in attempt_directories(problem)
        if (match := ATTEMPT_DIRECTORY_RE.fullmatch(path.name))
    ]
    return max(numbers, default=0) + 1


def _attempt_history_paths(
    problem: ProblemRef,
    *,
    attempts: Sequence[Path] | None = None,
) -> list[tuple[str, Path]]:
    files: list[tuple[str, Path]] = []
    for attempt in attempts if attempts is not None else attempt_directories(problem):
        try:
            with os.scandir(attempt) as entries:
                entry_lookup = {
                    entry.name: entry
                    for entry in entries
                    if entry.name in {*ATTEMPT_HISTORY_FILES, "artifacts"}
                }
        except FileNotFoundError:
            continue
        for name in ATTEMPT_HISTORY_FILES:
            entry = entry_lookup.get(name)
            if entry is not None and entry.is_file():
                files.append((f"{attempt.name}/{name}", Path(entry.path)))
        artifacts = attempt / "artifacts"
        artifact_entry = entry_lookup.get("artifacts")
        if artifact_entry is not None and artifact_entry.is_dir():
            for directory, _, names in os.walk(artifacts):
                for name in names:
                    path = Path(directory) / name
                    relative = os.path.relpath(
                        path,
                        problem.directory,
                    ).replace(os.sep, "/")
                    files.append((relative, path))
    return files


def attempt_history_digest(
    problem: ProblemRef,
    *,
    attempts: Sequence[Path] | None = None,
) -> str:
    return files_digest(_attempt_history_paths(problem, attempts=attempts))


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


def triage_input_digest(
    problem: ProblemRef,
    *,
    problem_digest_value: str | None = None,
    attempts: Sequence[Path] | None = None,
) -> str:
    return stable_value_digest(
        {
            "problem_digest": (
                problem_digest_value
                if problem_digest_value is not None
                else problem_digest(problem)
            ),
            "attempt_history_digest": attempt_history_digest(
                problem,
                attempts=attempts,
            ),
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
    problem_digest_value: str | None = None,
    attempts: Sequence[Path] | None = None,
) -> bool:
    manifest = triage_manifest(problem)
    result = triage_result(problem)
    if manifest is None or result is None:
        return False
    if manifest.get("schema_version") != TRIAGE_MANIFEST_SCHEMA_VERSION:
        return False
    if manifest.get("input_digest") != triage_input_digest(
        problem,
        problem_digest_value=problem_digest_value,
        attempts=attempts,
    ):
        return False
    if (
        config_digest is not None
        and manifest.get("config_digest") != config_digest
    ):
        return False
    return result.get("problem_id") == problem.id


def literature_input_digest(
    problem: ProblemRef,
    *,
    problem_digest_value: str | None = None,
) -> str:
    """Hash the analyzed problem, independent of attempts and triage."""
    return (
        problem_digest_value
        if problem_digest_value is not None
        else problem_digest(problem)
    )


def literature_manifest(problem: ProblemRef) -> dict | None:
    return load_json(problem.directory / LITERATURE_MANIFEST)


def literature_result(problem: ProblemRef) -> dict | None:
    return load_json(problem.directory / LITERATURE_RESULT)


def literature_is_current(
    problem: ProblemRef,
    *,
    config_digest: str | None = None,
    problem_digest_value: str | None = None,
) -> bool:
    manifest = literature_manifest(problem)
    result = literature_result(problem)
    if manifest is None or result is None:
        return False
    if manifest.get("schema_version") != LITERATURE_MANIFEST_SCHEMA_VERSION:
        return False
    if manifest.get("input_digest") != literature_input_digest(
        problem,
        problem_digest_value=problem_digest_value,
    ):
        return False
    if (
        config_digest is not None
        and manifest.get("config_digest") != config_digest
    ):
        return False
    return (
        result.get("problem_id") == problem.id
        and result.get("run_status") == "complete"
    )


def literature_snapshot_digest(problem: ProblemRef) -> str | None:
    """Hash the current literature packet, or return None when unavailable."""
    if not literature_is_current(problem):
        return None
    return files_digest(
        [
            (name, problem.directory / name)
            for name in (
                LITERATURE_MARKDOWN,
                LITERATURE_RESULT,
                LITERATURE_MANIFEST,
            )
        ]
    )


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
    include_literature: bool = False,
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

    if include_literature:
        for problem in problems:
            destination = inputs / "literature" / problem.id
            for name in (
                LITERATURE_MARKDOWN,
                LITERATURE_RESULT,
                LITERATURE_MANIFEST,
            ):
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
