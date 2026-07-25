#!/usr/bin/env python3
"""Analyze downloaded papers with independent non-interactive Codex runs."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from typing import Iterable


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROMPT_PATH = PROJECT_ROOT / "prompts" / "analyze-paper.md"
DEFAULT_SCHEMA_PATH = (
    PROJECT_ROOT / "schemas" / "paper-analysis-result.schema.json"
)
ANALYSIS_DIRECTORY = "analysis"
CONTENT_FILES = ("summary.md", "results.md", "open-problems.md")
RUN_FILES = ("events.jsonl", "run.log")
MANIFEST_FILE = "manifest.json"
MANIFEST_SCHEMA_VERSION = 2
STAGED_PAPER_DIRECTORY = "paper-input"
OPEN_PROBLEM_ID_RE = re.compile(r"^OP-[0-9]{3,}$")
RESULT_HEADING_RE = re.compile(
    r"^#{1,6}[ \t]+(?:`|\*\*)?(R-[0-9]{3,})\b",
    re.MULTILINE,
)
REASONING_EFFORTS = ("low", "medium", "high", "xhigh", "max", "ultra")
CODEX_LAUNCH_INTERVAL_SECONDS = 1.0
MAX_CODEX_START_ATTEMPTS = 3
_CODEX_LAUNCH_LOCK = threading.Lock()
_next_codex_launch_at = 0.0


class AnalysisError(RuntimeError):
    """A paper could not be analyzed or its output was invalid."""


@dataclass(frozen=True)
class AnalysisOutcome:
    paper_directory: Path
    status: str
    message: str
    result_count: int | None = None
    open_problem_count: int | None = None


def is_paper_directory(path: Path) -> bool:
    """Return whether ``path`` looks like one downloaded paper directory."""
    return path.is_dir() and (
        (path / "paper.pdf").is_file() or (path / "source").exists()
    )


def discover_paper_directories(paths: Iterable[Path]) -> list[Path]:
    """Resolve paper directories from direct paths or parent directories."""
    papers: dict[str, Path] = {}
    for supplied in paths:
        path = supplied.expanduser().resolve()
        if not path.exists():
            raise AnalysisError(f"path does not exist: {path}")

        if is_paper_directory(path):
            papers[os.path.normcase(str(path))] = path
            continue

        if not path.is_dir():
            raise AnalysisError(f"not a directory: {path}")

        for candidate in sorted(path.rglob("arXiv-*")):
            if is_paper_directory(candidate):
                resolved = candidate.resolve()
                papers[os.path.normcase(str(resolved))] = resolved

    if not papers:
        raise AnalysisError("no downloaded paper directories were found")
    return sorted(papers.values(), key=lambda item: os.path.normcase(str(item)))


def _hash_file(digest, path: Path, relative: str) -> None:
    digest.update(relative.encode("utf-8", errors="surrogateescape"))
    digest.update(b"\0")
    if path.is_symlink():
        digest.update(b"link\0")
        digest.update(os.readlink(path).encode("utf-8", errors="surrogateescape"))
        digest.update(b"\0")
        return

    digest.update(b"file\0")
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    digest.update(b"\0")


def source_digest(paper_directory: Path) -> str:
    """Hash the PDF and submitted source used as analysis inputs."""
    digest = hashlib.sha256()
    found_input = False
    for name in ("paper.pdf", "source"):
        root = paper_directory / name
        if not root.exists() and not root.is_symlink():
            continue
        found_input = True
        if root.is_file() or root.is_symlink():
            _hash_file(digest, root, name)
            continue

        digest.update(f"directory\0{name}\0".encode())
        for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
            if path.is_dir() and not path.is_symlink():
                continue
            relative = path.relative_to(paper_directory).as_posix()
            _hash_file(digest, path, relative)

    if not found_input:
        raise AnalysisError(
            f"paper has neither paper.pdf nor source/: {paper_directory}"
        )
    return digest.hexdigest()


def analysis_config_digest(
    prompt: str,
    schema_text: str,
    model: str | None,
    reasoning_effort: str | None,
    fast: bool,
) -> str:
    """Hash settings that affect the semantic analysis."""
    payload = {
        "fast": fast,
        "model": model,
        "prompt": prompt,
        "reasoning_effort": reasoning_effort,
        "schema": schema_text,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def load_manifest(path: Path) -> dict | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def valid_paper_authors(value: object) -> bool:
    return (
        isinstance(value, list)
        and bool(value)
        and all(
            isinstance(author, str) and bool(author.strip())
            for author in value
        )
        and len(set(value)) == len(value)
    )


def result_count(analysis_directory: Path) -> int:
    """Count uniquely identified catalog entries in ``results.md``."""
    contents = (analysis_directory / "results.md").read_text(encoding="utf-8")
    return len(set(RESULT_HEADING_RE.findall(contents)))


def analysis_counts(analysis_directory: Path) -> tuple[int, int]:
    """Read result and open-problem counts from an installed analysis."""
    manifest = load_manifest(analysis_directory / MANIFEST_FILE)
    if manifest is None or not isinstance(manifest.get("open_problems"), list):
        raise AnalysisError(
            f"analysis manifest has no open-problem list: "
            f"{analysis_directory / MANIFEST_FILE}"
        )
    return result_count(analysis_directory), len(manifest["open_problems"])


def format_count(count: int, singular: str) -> str:
    suffix = "" if count == 1 else "s"
    return f"{count} {singular}{suffix}"


def format_analysis_counts(results: int, open_problems: int) -> str:
    return (
        f"{format_count(results, 'result')}, "
        f"{format_count(open_problems, 'open problem')}"
    )


def analysis_is_current(
    analysis_directory: Path,
    *,
    paper_digest: str,
    config_digest: str,
) -> bool:
    """Return whether a complete, matching analysis is already installed."""
    manifest = load_manifest(analysis_directory / MANIFEST_FILE)
    if manifest is None:
        return False
    if manifest.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        return False
    if not valid_paper_authors(manifest.get("paper_authors")):
        return False
    if manifest.get("source_digest") != paper_digest:
        return False
    if (
        manifest.get("analysis_config_digest") != config_digest
        and manifest.get("recovered_without_config") is not True
    ):
        return False
    return all(
        (analysis_directory / filename).is_file()
        and (analysis_directory / filename).stat().st_size > 0
        for filename in CONTENT_FILES
    )


def validate_agent_result(result_path: Path, workspace: Path) -> dict:
    """Validate the structured final response and three Markdown artifacts."""
    try:
        result = json.loads(result_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise AnalysisError(
            "Codex did not write its structured final response"
        ) from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AnalysisError(
            f"could not read Codex's structured response: {exc}"
        ) from exc

    if not isinstance(result, dict):
        raise AnalysisError("Codex's structured response is not a JSON object")
    if result.get("status") not in {"complete", "partial"}:
        raise AnalysisError("Codex's structured response has an invalid status")
    paper_title = result.get("paper_title")
    if not isinstance(paper_title, str) or not paper_title.strip():
        raise AnalysisError("Codex's structured response has no paper title")
    paper_authors = result.get("paper_authors")
    if not valid_paper_authors(paper_authors):
        raise AnalysisError(
            "Codex's structured response has invalid paper authors"
        )
    if not isinstance(result.get("warnings"), list) or not all(
        isinstance(item, str) for item in result["warnings"]
    ):
        raise AnalysisError("Codex's structured response has invalid warnings")

    problems = result.get("open_problems")
    if not isinstance(problems, list):
        raise AnalysisError("Codex's structured response has no open-problem list")

    problem_ids: set[str] = set()
    for problem in problems:
        if not isinstance(problem, dict):
            raise AnalysisError("an open-problem manifest entry is not an object")
        problem_id = problem.get("id")
        if not isinstance(problem_id, str) or not OPEN_PROBLEM_ID_RE.fullmatch(
            problem_id
        ):
            raise AnalysisError(f"invalid open-problem ID: {problem_id!r}")
        if problem_id in problem_ids:
            raise AnalysisError(f"duplicate open-problem ID: {problem_id}")
        problem_ids.add(problem_id)
        title = problem.get("title")
        if not isinstance(title, str) or not title.strip():
            raise AnalysisError(f"open problem {problem_id} has no title")
        if problem.get("explicitness") not in {
            "explicit",
            "inferred",
            "uncertain",
        }:
            raise AnalysisError(
                f"open problem {problem_id} has invalid explicitness"
            )

    markdown: dict[str, str] = {}
    for filename in CONTENT_FILES:
        path = workspace / filename
        try:
            contents = path.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise AnalysisError(f"Codex did not create {filename}") from exc
        except (OSError, UnicodeError) as exc:
            raise AnalysisError(
                f"could not read generated {filename}: {exc}"
            ) from exc
        if not contents.strip():
            raise AnalysisError(f"Codex created an empty {filename}")
        if not contents.lstrip().startswith("#"):
            raise AnalysisError(
                f"generated {filename} does not start with a heading"
            )
        markdown[filename] = contents

    open_problem_text = markdown["open-problems.md"]
    for problem_id in problem_ids:
        if problem_id not in open_problem_text:
            raise AnalysisError(
                f"{problem_id} is in the manifest but missing from open-problems.md"
            )
    return result


def build_manifest(
    agent_result: dict,
    *,
    paper_digest: str,
    config_digest: str | None,
    codex_version: str,
    model: str | None,
    reasoning_effort: str | None,
    fast: bool,
) -> dict:
    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_digest": paper_digest,
        "analysis_config_digest": config_digest,
        "codex_version": codex_version,
        "requested_fast_mode": fast,
        "requested_model": model,
        "requested_reasoning_effort": reasoning_effort,
        "status": agent_result["status"],
        "paper_title": agent_result["paper_title"],
        "paper_authors": agent_result["paper_authors"],
        "files": {
            "summary": "summary.md",
            "results": "results.md",
            "open_problems": "open-problems.md",
        },
        "open_problems": agent_result["open_problems"],
        "warnings": agent_result["warnings"],
    }


def is_windows_host() -> bool:
    return os.name == "nt" or sys.platform == "cygwin"


def _run_local_command(command: list[str], description: str) -> str:
    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        stdout, stderr = process.communicate()
    except OSError as exc:
        raise AnalysisError(f"{description}: {exc}") from exc
    if process.returncode != 0:
        detail = stderr.strip() or stdout.strip() or (
            f"exit status {process.returncode}"
        )
        raise AnalysisError(f"{description}: {detail}")
    return stdout


@lru_cache(maxsize=1)
def windows_identity() -> str:
    domain = os.environ.get("USERDOMAIN", "").strip()
    username = os.environ.get("USERNAME", "").strip()
    if domain and username:
        return f"{domain}\\{username}"

    executable = shutil.which("whoami.exe")
    if executable is None:
        raise AnalysisError("could not find whoami.exe for Windows ACL setup")
    identity = _run_local_command(
        [executable],
        "could not determine the current Windows identity",
    ).strip()
    if not identity or "\\" not in identity:
        raise AnalysisError(
            f"whoami.exe returned an unexpected identity: {identity!r}"
        )
    return identity


@lru_cache(maxsize=1)
def windows_icacls() -> str:
    executable = shutil.which("icacls.exe") or shutil.which("icacls")
    if executable is None:
        raise AnalysisError("could not find icacls.exe for Windows ACL setup")
    return executable


def windows_icacls_for_sandbox() -> str:
    executable = windows_icacls()
    if sys.platform == "cygwin":
        return path_for_codex(Path(executable))
    return executable


def grant_workspace_owner_inheritance(workspace: Path) -> None:
    """Make future sandbox-created children readable by the invoking user."""
    if not is_windows_host():
        return
    identity = windows_identity()
    _run_local_command(
        [
            windows_icacls(),
            path_for_codex(workspace),
            "/grant",
            f"{identity}:(OI)(CI)(F)",
        ],
        f"could not grant {identity} access to {workspace}",
    )


def grant_staged_paper_read_access(staged_paper: Path) -> None:
    """Let the Windows Codex sandbox read user-created staged inputs."""
    if not is_windows_host():
        return
    domain = windows_identity().split("\\", 1)[0]
    sandbox_group = f"{domain}\\CodexSandboxUsers"
    staged_path = path_for_codex(staged_paper)
    _run_local_command(
        [
            windows_icacls(),
            staged_path,
            "/remove:d",
            sandbox_group,
            "/T",
            "/C",
        ],
        f"could not remove inherited sandbox read denials from {staged_paper}",
    )
    _run_local_command(
        [
            windows_icacls(),
            staged_path,
            "/grant",
            f"{sandbox_group}:(OI)(CI)(RX)",
            "/T",
            "/C",
        ],
        f"could not grant the Codex sandbox read access to {staged_paper}",
    )


def stage_paper_inputs(paper_directory: Path, workspace: Path) -> Path:
    """Copy primary inputs into the sandbox without copying restrictive ACLs."""
    staged_paper = workspace / STAGED_PAPER_DIRECTORY
    staged_paper.mkdir()

    pdf = paper_directory / "paper.pdf"
    if pdf.is_file():
        shutil.copyfile(pdf, staged_paper / "paper.pdf")

    source = paper_directory / "source"
    if source.is_dir():
        shutil.copytree(
            source,
            staged_paper / "source",
            copy_function=shutil.copyfile,
            symlinks=False,
        )
    elif source.is_file():
        shutil.copyfile(source, staged_paper / "source")

    metadata = paper_directory / "metadata.json"
    if metadata.is_file():
        shutil.copyfile(metadata, staged_paper / "metadata.json")

    grant_staged_paper_read_access(staged_paper)
    return staged_paper


def path_for_codex(path: Path) -> str:
    """Return a path the Windows-native Codex CLI can understand."""
    resolved = path.resolve()
    if sys.platform != "cygwin":
        return str(resolved)

    try:
        process = subprocess.Popen(
            ["cygpath", "-w", str(resolved)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        stdout, stderr = process.communicate()
    except OSError as exc:
        raise AnalysisError(f"could not run cygpath for {resolved}: {exc}") from exc
    if process.returncode != 0:
        detail = stderr.strip() or f"exit status {process.returncode}"
        raise AnalysisError(f"could not convert Cygwin path {resolved}: {detail}")
    converted = stdout.strip()
    if not converted:
        raise AnalysisError(f"cygpath returned an empty path for {resolved}")
    return converted


def render_prompt(template: str, paper_directory: Path) -> str:
    return template.replace(
        "{{PAPER_DIRECTORY}}",
        path_for_codex(paper_directory),
    )


def wait_for_codex_launch_slot(interval: float) -> None:
    """Space process startups while allowing already-started runs to overlap."""
    if interval <= 0:
        return

    global _next_codex_launch_at
    with _CODEX_LAUNCH_LOCK:
        now = time.monotonic()
        delay = max(0.0, _next_codex_launch_at - now)
        if delay:
            time.sleep(delay)
        _next_codex_launch_at = time.monotonic() + interval


def is_transient_startup_failure(
    completed: subprocess.CompletedProcess,
    events_path: Path,
    log_path: Path,
) -> bool:
    """Recognize the Windows startup race seen before a thread is created."""
    if completed.returncode == 0 or events_path.stat().st_size:
        return False
    try:
        log = log_path.read_text(encoding="utf-8").lower()
    except (OSError, UnicodeError):
        return False
    return (
        "the system cannot find the path specified" in log
        or "os error 3" in log
    )


def analyze_paper(
    paper_directory: Path,
    *,
    codex: str,
    codex_version: str,
    prompt_template: str,
    schema_path: Path,
    schema_text: str,
    model: str | None = None,
    reasoning_effort: str | None = None,
    fast: bool = False,
    force: bool = False,
    launch_interval: float = CODEX_LAUNCH_INTERVAL_SECONDS,
) -> AnalysisOutcome:
    """Run Codex for one paper and atomically install validated artifacts."""
    paper_directory = paper_directory.resolve()
    analysis_directory = paper_directory / ANALYSIS_DIRECTORY
    analysis_directory.mkdir(parents=True, exist_ok=True)

    paper_digest = source_digest(paper_directory)
    config_digest = analysis_config_digest(
        prompt_template,
        schema_text,
        model,
        reasoning_effort,
        fast,
    )
    if not force and analysis_is_current(
        analysis_directory,
        paper_digest=paper_digest,
        config_digest=config_digest,
    ):
        results, open_problems = analysis_counts(analysis_directory)
        return AnalysisOutcome(
            paper_directory,
            "current",
            (
                f"{format_analysis_counts(results, open_problems)}; "
                "analysis matches the paper and prompt"
            ),
            result_count=results,
            open_problem_count=open_problems,
        )

    workspace = Path(
        tempfile.mkdtemp(prefix=".run-", dir=analysis_directory)
    ).resolve()
    grant_workspace_owner_inheritance(workspace)
    staged_paper = stage_paper_inputs(paper_directory, workspace)
    result_path = workspace / "agent-result.json"
    prompt = render_prompt(prompt_template, staged_paper)
    command = [
        codex,
        "exec",
        "--ephemeral",
        "--disable",
        "shell_snapshot",
        "--skip-git-repo-check",
        "--sandbox",
        "workspace-write",
        "--json",
        "--color",
        "never",
        "-C",
        path_for_codex(workspace),
        "--output-schema",
        path_for_codex(schema_path),
        "-o",
        path_for_codex(result_path),
    ]
    if model is not None:
        command.extend(("--model", model))
    if reasoning_effort is not None:
        command.extend(
            (
                "--config",
                f'model_reasoning_effort="{reasoning_effort}"',
            )
        )
    if fast:
        command.extend(
            (
                "--config",
                "features.fast_mode=true",
                "--config",
                'service_tier="fast"',
            )
        )
    command.append(prompt)

    events_path = workspace / "events.jsonl"
    log_path = workspace / "run.log"
    events_path.write_text("", encoding="utf-8")
    log_path.write_text("", encoding="utf-8")
    completed: subprocess.CompletedProcess | None = None
    for attempt in range(1, MAX_CODEX_START_ATTEMPTS + 1):
        wait_for_codex_launch_slot(launch_interval)
        try:
            with (
                events_path.open("a", encoding="utf-8") as events,
                log_path.open("a", encoding="utf-8") as log,
            ):
                if attempt > 1:
                    log.write(
                        f"\n--- Codex startup retry {attempt}/"
                        f"{MAX_CODEX_START_ATTEMPTS} ---\n"
                    )
                    log.flush()
                completed = subprocess.run(
                    command,
                    cwd=workspace,
                    stdout=events,
                    stderr=log,
                    text=True,
                    check=False,
                )
        except OSError as exc:
            if (
                attempt < MAX_CODEX_START_ATTEMPTS
                and getattr(exc, "winerror", None) in {2, 3}
            ):
                with log_path.open("a", encoding="utf-8") as log:
                    log.write(f"Codex startup error: {exc}\n")
                continue
            raise AnalysisError(
                f"could not start Codex; workspace preserved at "
                f"{workspace}: {exc}"
            ) from exc

        if completed.returncode == 0:
            break
        if (
            attempt == MAX_CODEX_START_ATTEMPTS
            or not is_transient_startup_failure(
                completed,
                events_path,
                log_path,
            )
        ):
            break

    if completed is None or completed.returncode != 0:
        returncode = completed.returncode if completed is not None else "unknown"
        raise AnalysisError(
            f"Codex exited with status {returncode}; "
            f"workspace preserved at {workspace}"
        )

    grant_recovery_access(workspace, codex)

    try:
        agent_result = validate_agent_result(result_path, workspace)
        results = result_count(workspace)
        open_problems = len(agent_result["open_problems"])
        manifest = build_manifest(
            agent_result,
            paper_digest=paper_digest,
            config_digest=config_digest,
            codex_version=codex_version,
            model=model,
            reasoning_effort=reasoning_effort,
            fast=fast,
        )
        staged_manifest = workspace / MANIFEST_FILE
        staged_manifest.write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    except (AnalysisError, OSError) as exc:
        if isinstance(exc, AnalysisError):
            message = str(exc)
        else:
            message = f"could not stage the analysis manifest: {exc}"
        raise AnalysisError(
            f"{message}; workspace preserved at {workspace}"
        ) from exc

    final_manifest = analysis_directory / MANIFEST_FILE
    try:
        final_manifest.unlink(missing_ok=True)
        for filename in (*CONTENT_FILES, *RUN_FILES, MANIFEST_FILE):
            os.replace(workspace / filename, analysis_directory / filename)
    except OSError as exc:
        raise AnalysisError(
            f"could not install the analysis; workspace preserved at "
            f"{workspace}: {exc}"
        ) from exc

    cleanup_warning: str | None = None
    try:
        shutil.rmtree(workspace)
    except OSError as exc:
        cleanup_warning = (
            f"analysis installed, but could not remove temporary workspace "
            f"{workspace}: {exc}"
        )
        try:
            with (analysis_directory / "run.log").open(
                "a",
                encoding="utf-8",
            ) as log:
                log.write(f"\nDriver cleanup warning: {cleanup_warning}\n")
        except OSError:
            pass

    detail = (
        f"{format_analysis_counts(results, open_problems)}, "
        f"status {agent_result['status']}"
    )
    if cleanup_warning is not None:
        detail += "; temporary workspace preserved"
    return AnalysisOutcome(
        paper_directory,
        "analyzed",
        detail,
        result_count=results,
        open_problem_count=open_problems,
    )


def workspace_is_user_accessible(workspace: Path) -> bool:
    """Return whether validation and recursive cleanup can traverse the tree."""
    try:
        for filename in CONTENT_FILES:
            path = workspace / filename
            if path.exists():
                path.read_bytes()

        def raise_walk_error(error: OSError) -> None:
            raise error

        for root, directories, filenames in os.walk(
            workspace,
            onerror=raise_walk_error,
        ):
            root_path = Path(root)
            for name in (*directories, *filenames):
                (root_path / name).stat()
    except OSError:
        return False
    return True


def grant_recovery_access(workspace: Path, codex: str) -> None:
    """Normalize sandbox-owned ACLs for validation and recursive cleanup."""
    if not is_windows_host():
        return
    if workspace_is_user_accessible(workspace):
        return

    identity = windows_identity()
    command_prefix = [
        codex,
        "sandbox",
        "-P",
        ":workspace",
        "-C",
        path_for_codex(workspace),
        windows_icacls_for_sandbox(),
        ".",
    ]
    for action in (
        ["/remove:d", identity, "/T", "/C"],
        ["/grant", f"{identity}:(OI)(CI)(F)", "/T", "/C"],
    ):
        _run_local_command(
            [*command_prefix, *action],
            f"could not normalize sandbox-owned files in {workspace}",
        )
    if not workspace_is_user_accessible(workspace):
        raise AnalysisError(
            f"Windows ACL repair did not make {workspace} accessible"
        )


def find_complete_run(analysis_directory: Path) -> Path | None:
    """Return the newest preserved run whose structured status is complete."""
    candidates = sorted(
        analysis_directory.glob(".run-*"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for workspace in candidates:
        result = load_manifest(workspace / "agent-result.json")
        if result is not None and result.get("status") == "complete":
            return workspace
    return None


def recover_complete_analysis(
    paper_directory: Path,
    *,
    codex: str,
    codex_version: str,
) -> AnalysisOutcome:
    """Install a preserved complete run without spending another model turn."""
    paper_directory = paper_directory.resolve()
    analysis_directory = paper_directory / ANALYSIS_DIRECTORY
    workspace = find_complete_run(analysis_directory)
    if workspace is None:
        return AnalysisOutcome(
            paper_directory,
            "unrecovered",
            "no preserved run has structured status complete",
        )

    grant_recovery_access(workspace, codex)
    agent_result = validate_agent_result(
        workspace / "agent-result.json",
        workspace,
    )
    results = result_count(workspace)
    open_problems = len(agent_result["open_problems"])
    manifest = build_manifest(
        agent_result,
        paper_digest=source_digest(paper_directory),
        config_digest=None,
        codex_version=codex_version,
        model=None,
        reasoning_effort=None,
        fast=False,
    )
    manifest.update(
        {
            "requested_fast_mode": None,
            "recovered_without_config": True,
            "recovered_from_run": workspace.name,
            "original_run_preserved": True,
        }
    )

    staging = Path(
        tempfile.mkdtemp(prefix=".recover-", dir=analysis_directory)
    ).resolve()
    try:
        for filename in (*CONTENT_FILES, *RUN_FILES):
            shutil.copyfile(workspace / filename, staging / filename)
        (staging / MANIFEST_FILE).write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        for filename in (*CONTENT_FILES, *RUN_FILES, MANIFEST_FILE):
            os.replace(staging / filename, analysis_directory / filename)
    except (OSError, AnalysisError) as exc:
        raise AnalysisError(
            f"could not install recovered analysis; staging preserved at "
            f"{staging}: {exc}"
        ) from exc
    shutil.rmtree(staging)
    return AnalysisOutcome(
        paper_directory,
        "recovered",
        (
            f"{format_analysis_counts(results, open_problems)}; "
            f"preserved {workspace.name}"
        ),
        result_count=results,
        open_problem_count=open_problems,
    )


def positive_integer(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return number


def resolve_codex_executable(value: str) -> str:
    executable = shutil.which(value)
    if executable is None:
        raise AnalysisError(
            f"could not find the Codex CLI executable {value!r} on PATH"
        )
    return executable


def read_codex_version(codex: str) -> str:
    try:
        completed = subprocess.run(
            [codex, "--version"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise AnalysisError(f"could not run {codex!r}: {exc}") from exc
    if completed.returncode != 0:
        detail = completed.stderr.strip() or f"exit status {completed.returncode}"
        raise AnalysisError(f"could not query the Codex CLI version: {detail}")
    return completed.stdout.strip()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "use independent Codex CLI runs to analyze downloaded research papers"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  Analyze one paper with the configured Codex defaults:
    python src/analyze_papers.py papers/author/arXiv-1706.03762

  Use the current frontier model with extra-high reasoning and Fast mode:
    python src/analyze_papers.py papers/author \\
      --model gpt-5.6-sol --reasoning-effort xhigh --fast
""",
    )
    parser.add_argument(
        "paths",
        nargs="+",
        type=Path,
        help=(
            "paper directories, or parent directories to search recursively for "
            "arXiv-* paper directories"
        ),
    )
    parser.add_argument(
        "-j",
        "--jobs",
        type=positive_integer,
        default=1,
        help="maximum concurrent Codex runs (default: 1)",
    )
    parser.add_argument(
        "-f",
        "--force",
        action="store_true",
        help="reanalyze papers even when their inputs and prompt are unchanged",
    )
    parser.add_argument(
        "--recover-complete",
        action="store_true",
        help=(
            "install preserved .run-* outputs whose structured status is "
            "complete, without starting new model turns"
        ),
    )
    parser.add_argument(
        "--model",
        metavar="MODEL",
        help=(
            "Codex model ID; for example, gpt-5.6-sol "
            "(default: use the CLI configuration)"
        ),
    )
    parser.add_argument(
        "--reasoning-effort",
        choices=REASONING_EFFORTS,
        metavar="LEVEL",
        help=(
            "reasoning depth: low, medium, high, xhigh, max, or ultra; "
            "extra-high is xhigh (default: use the CLI configuration)"
        ),
    )
    parser.add_argument(
        "--fast",
        action="store_true",
        help=(
            "request Codex Fast mode for this run; this uses more credits "
            "(default: use the CLI configuration)"
        ),
    )
    parser.add_argument(
        "--codex",
        default="codex",
        help="Codex CLI executable or command name (default: codex)",
    )
    parser.add_argument(
        "--prompt",
        type=Path,
        default=DEFAULT_PROMPT_PATH,
        help=f"analysis prompt template (default: {DEFAULT_PROMPT_PATH})",
    )
    parser.add_argument(
        "--schema",
        type=Path,
        default=DEFAULT_SCHEMA_PATH,
        help=f"final-response JSON schema (default: {DEFAULT_SCHEMA_PATH})",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        papers = discover_paper_directories(args.paths)
        prompt_path = args.prompt.expanduser().resolve()
        schema_path = args.schema.expanduser().resolve()
        prompt_template = prompt_path.read_text(encoding="utf-8")
        schema_text = schema_path.read_text(encoding="utf-8")
        json.loads(schema_text)
        codex = resolve_codex_executable(args.codex)
        codex_version = read_codex_version(codex)
    except (
        AnalysisError,
        OSError,
        UnicodeError,
        json.JSONDecodeError,
    ) as exc:
        parser.error(str(exc))

    if args.recover_complete:
        print(
            f"Found {len(papers)} paper(s); recovering with up to "
            f"{min(args.jobs, len(papers))} local worker(s)."
        )
    else:
        print(
            f"Found {len(papers)} paper(s); running up to "
            f"{min(args.jobs, len(papers))} Codex agent(s) at once."
        )

    outcomes: list[AnalysisOutcome] = []
    failures: list[tuple[Path, str]] = []
    with ThreadPoolExecutor(max_workers=min(args.jobs, len(papers))) as executor:
        if args.recover_complete:
            future_to_paper = {
                executor.submit(
                    recover_complete_analysis,
                    paper,
                    codex=codex,
                    codex_version=codex_version,
                ): paper
                for paper in papers
            }
        else:
            future_to_paper = {
                executor.submit(
                    analyze_paper,
                    paper,
                    codex=codex,
                    codex_version=codex_version,
                    prompt_template=prompt_template,
                    schema_path=schema_path,
                    schema_text=schema_text,
                    model=args.model,
                    reasoning_effort=args.reasoning_effort,
                    fast=args.fast,
                    force=args.force,
                ): paper
                for paper in papers
            }
        for future in as_completed(future_to_paper):
            paper = future_to_paper[future]
            try:
                outcome = future.result()
            except (AnalysisError, OSError) as exc:
                failures.append((paper, str(exc)))
                print(f"Failed: {paper}: {exc}", file=sys.stderr)
                continue
            outcomes.append(outcome)
            if outcome.status == "current":
                label = "Current"
            elif outcome.status == "recovered":
                label = "Recovered"
            elif outcome.status == "unrecovered":
                label = "Skipped"
            else:
                label = "Analyzed"
            print(f"{label}: {paper} ({outcome.message})")

    analyzed = sum(outcome.status == "analyzed" for outcome in outcomes)
    current = sum(outcome.status == "current" for outcome in outcomes)
    recovered = sum(outcome.status == "recovered" for outcome in outcomes)
    unrecovered = sum(outcome.status == "unrecovered" for outcome in outcomes)
    total_results = sum(outcome.result_count or 0 for outcome in outcomes)
    total_open_problems = sum(
        outcome.open_problem_count or 0 for outcome in outcomes
    )
    print(
        f"Completed {analyzed} analysis run(s); recovered {recovered}; "
        f"{current} already current; {unrecovered} had no complete run"
        + (f"; {len(failures)} failed" if failures else "")
        + f". Totals: "
        f"{format_analysis_counts(total_results, total_open_problems)}."
    )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
