#!/usr/bin/env python3
"""Make independent, paper-informed attempts on selected open problems."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import sys
import tempfile
from typing import Callable, Sequence

import codex_cli
import open_problem_common as common
import review_solutions


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROMPT_PATH = PROJECT_ROOT / "prompts" / "solve-open-problem.md"
DEFAULT_SCHEMA_PATH = (
    PROJECT_ROOT / "schemas" / "open-problem-solution.schema.json"
)
DEFAULT_REVIEW_PROMPT_PATH = review_solutions.DEFAULT_PROMPT_PATH
DEFAULT_REVIEW_SCHEMA_PATH = review_solutions.DEFAULT_SCHEMA_PATH
SOLUTION_STATUSES = common.CLAIMED_RESULT_TYPES
CLAIM_ID_RE = re.compile(r"^C-[0-9]{3,}$")
LOOSE_CLAIM_ID_RE = re.compile(r"^C[-_ ]?0*([1-9][0-9]*)$")
CORE_OUTPUT_PATHS = {"attempt.md", "solution.md"}
WORK_RECORD_FILE = "work-record.json"


@dataclass(frozen=True)
class SolveWork:
    problem: common.ProblemRef
    guidance: dict
    attempt_number: int
    triage_snapshot_digest: str | None
    literature_snapshot_digest: str | None = None

    @property
    def attempt_name(self) -> str:
        return f"attempt-{self.attempt_number:03d}"


@dataclass(frozen=True)
class SolveOutcome:
    work: SolveWork
    attempt: review_solutions.AttemptRef
    claimed_result_type: str
    claim_count: int
    message: str


SolveFinishedCallback = Callable[
    [SolveWork, SolveOutcome | None, str | None],
    None,
]


def generic_guidance() -> dict:
    return {
        "source": "explicit_selection",
        "classification": None,
        "rationale": (
            "This problem was selected explicitly without a current triage."
        ),
        "promising_features": [],
        "obstacles": [],
        "suggested_approaches": [],
        "instruction": (
            "Choose and adapt the most promising research strategy after "
            "reading the full paper, analysis, and attempt history."
        ),
    }


def research_guidance(
    problem: common.ProblemRef,
    *,
    require_triage_classes: set[str] | None,
) -> tuple[dict | None, str | None]:
    """Return current triage as advisory guidance, or generic guidance."""
    current = common.triage_is_current(problem)
    result = common.triage_result(problem) if current else None
    if require_triage_classes is not None:
        if result is None or result.get("classification") not in (
            require_triage_classes
        ):
            return None, None
    if result is None:
        guidance = generic_guidance()
        triage_snapshot = None
    else:
        guidance = {
            "source": "triage",
            "classification": result.get("classification"),
            "rationale": result.get("rationale", ""),
            "promising_features": result.get("promising_features", []),
            "obstacles": result.get("obstacles", []),
            "suggested_approaches": result.get("suggested_approaches", []),
            "instruction": (
                "Treat every suggestion as advisory. Form your own strategy; "
                "combine, reorder, abandon, or replace approaches in response "
                "to evidence."
            ),
        }
        triage_snapshot = common.triage_input_digest(problem)
    literature = (
        common.literature_result(problem)
        if common.literature_is_current(problem)
        else None
    )
    if literature is not None:
        guidance["literature"] = {
            "resolution_status": literature.get("resolution_status"),
            "confidence": literature.get("confidence"),
            "status_summary": literature.get("status_summary", ""),
            "residual_problem": literature.get("residual_problem", ""),
            "solver_briefing": literature.get("solver_briefing", ""),
        }
    return guidance, triage_snapshot


def build_work(
    problems: Sequence[common.ProblemRef],
    *,
    require_triage_classes: set[str] | None,
    include_literature_resolved: bool = False,
) -> tuple[
    list[SolveWork],
    list[common.ProblemRef],
    list[common.ProblemRef],
]:
    work: list[SolveWork] = []
    without_work: list[common.ProblemRef] = []
    literature_resolved: list[common.ProblemRef] = []
    for problem in problems:
        literature = (
            common.literature_result(problem)
            if common.literature_is_current(problem)
            else None
        )
        if (
            not include_literature_resolved
            and literature is not None
            and literature.get("resolution_status") == "resolved"
        ):
            literature_resolved.append(problem)
            continue
        guidance, snapshot = research_guidance(
            problem,
            require_triage_classes=require_triage_classes,
        )
        if guidance is None:
            without_work.append(problem)
            continue
        work.append(
            SolveWork(
                problem,
                guidance,
                common.next_attempt_number(problem),
                snapshot,
                common.literature_snapshot_digest(problem),
            )
        )
    return work, without_work, literature_resolved


def render_prompt(
    template: str,
    *,
    work: SolveWork,
    context_directory: Path,
) -> str:
    return (
        template.replace("{{PROBLEM_ID}}", work.problem.id)
        .replace(
            "{{RESEARCH_GUIDANCE_JSON}}",
            json.dumps(work.guidance, indent=2, ensure_ascii=False),
        )
        .replace(
            "{{CONTEXT_DIRECTORY}}",
            codex_cli.path_for_codex(context_directory),
        )
    )


def _validated_artifact_paths(workspace: Path, values: object) -> list[Path]:
    if not isinstance(values, list) or not all(
        isinstance(value, str) for value in values
    ):
        raise common.CodexError("solver response has invalid artifacts")
    paths: list[Path] = []
    seen: set[str] = set()
    for value in values:
        relative = _artifact_relative_path(workspace, value)
        if relative is None:
            raise common.CodexError(
                f"solver response has unsafe artifact path: {value!r}"
            )
        normalized = relative.as_posix()
        if normalized in CORE_OUTPUT_PATHS:
            continue
        if (
            relative.is_absolute()
            or not relative.parts
            or ".." in relative.parts
            or normalized in seen
        ):
            raise common.CodexError(
                f"solver response has unsafe artifact path: {value!r}"
            )
        if relative.parts[0] == "artifacts":
            path = workspace.joinpath(*relative.parts)
        elif len(relative.parts) == 1:
            source = workspace / relative.name
            if not source.is_file():
                raise common.CodexError(
                    f"solver-listed artifact does not exist: {value}"
                )
            path = workspace / "artifacts" / relative.name
            path.parent.mkdir(parents=True, exist_ok=True)
            if not path.exists():
                shutil.copyfile(source, path)
            relative = PurePosixPath("artifacts", relative.name)
            normalized = relative.as_posix()
        else:
            raise common.CodexError(
                f"solver response has unsafe artifact path: {value!r}"
            )
        if normalized in seen:
            raise common.CodexError(
                f"solver response has unsafe artifact path: {value!r}"
            )
        seen.add(normalized)
        if not path.is_file():
            raise common.CodexError(
                f"solver-listed artifact does not exist: {value}"
            )
        paths.append(path)
    return paths


def _artifact_relative_path(
    workspace: Path,
    value: str,
) -> PurePosixPath | None:
    """Parse a relative path or a model-generated local Markdown link."""
    target = value
    match = re.fullmatch(r"\[([^\]]+)\]\((.+)\)", value)
    if match is not None:
        target = match.group(2)
    target = target.strip()
    if target.startswith("<") and target.endswith(">"):
        target = target[1:-1]
    target = re.sub(r":\d+(?::\d+)?$", "", target)
    normalized = target.replace("\\", "/")
    if re.match(r"^/[A-Za-z]:/", normalized):
        normalized = normalized[1:]
    roots = {
        workspace.resolve().as_posix().rstrip("/"),
        codex_cli.path_for_codex(workspace).replace("\\", "/").rstrip("/"),
    }
    folded = normalized.casefold()
    for root in roots:
        prefix = root.casefold() + "/"
        if folded.startswith(prefix):
            normalized = normalized[len(root) + 1:]
            break
    else:
        if normalized.startswith("/") or re.match(
            r"^[A-Za-z]:/", normalized
        ):
            return None
    relative = PurePosixPath(normalized)
    if not relative.parts or relative.is_absolute() or ".." in relative.parts:
        return None
    return relative


def _normalize_solver_claim_ids(result: dict) -> dict[str, str]:
    """Canonicalize unambiguous spellings such as C1 to C-001."""
    claims = result.get("checkable_claims")
    if not isinstance(claims, list):
        return {}
    changes: dict[str, str] = {}
    normalized_ids: set[str] = set()
    for claim in claims:
        if not isinstance(claim, dict):
            continue
        claim_id = claim.get("id")
        canonical = claim_id if isinstance(claim_id, str) else None
        if isinstance(claim_id, str) and not CLAIM_ID_RE.fullmatch(claim_id):
            match = LOOSE_CLAIM_ID_RE.fullmatch(claim_id)
            canonical = (
                f"C-{int(match.group(1)):03d}" if match is not None else None
            )
        if canonical is None or canonical in normalized_ids:
            continue
        normalized_ids.add(canonical)
        if canonical != claim_id:
            changes[claim_id] = canonical
            claim["id"] = canonical
    if changes:
        if isinstance(result.get("summary"), str):
            result["summary"] = _replace_claim_ids(
                result["summary"], changes
            )
        for claim in claims:
            if not isinstance(claim, dict):
                continue
            for field in ("statement", "support", "remaining_gap"):
                if isinstance(claim.get(field), str):
                    claim[field] = _replace_claim_ids(claim[field], changes)
        for source in result.get("external_sources", []):
            if not isinstance(source, dict):
                continue
            for field in ("title", "used_for", "verification"):
                if isinstance(source.get(field), str):
                    source[field] = _replace_claim_ids(
                        source[field], changes
                    )
        if isinstance(result.get("warnings"), list):
            result["warnings"] = [
                _replace_claim_ids(warning, changes)
                for warning in result["warnings"]
            ]
    if changes and isinstance(result.get("warnings"), list):
        rendered = ", ".join(
            f"{old} -> {new}" for old, new in changes.items()
        )
        result["warnings"].append(
            f"Driver normalized solver claim IDs: {rendered}."
        )
    return changes


def _replace_claim_ids(contents: str, changes: dict[str, str]) -> str:
    for old, new in changes.items():
        contents = re.sub(
            rf"(?<![A-Za-z0-9_-]){re.escape(old)}(?![A-Za-z0-9_-])",
            new,
            contents,
        )
    return contents


def validate_solver_result(
    result_path: Path,
    workspace: Path,
    *,
    require_markdown: bool = True,
) -> tuple[dict, list[Path]]:
    result = common.read_json(result_path, description="solver response")
    claimed_result_type = result.get("claimed_result_type")
    if claimed_result_type not in SOLUTION_STATUSES:
        raise common.CodexError(
            "solver response has an invalid claimed_result_type"
        )
    if not isinstance(result.get("summary"), str) or not result[
        "summary"
    ].strip():
        raise common.CodexError("solver response has no summary")
    external_sources = result.get("external_sources")
    if not isinstance(external_sources, list):
        raise common.CodexError("solver response has invalid external_sources")
    for source in external_sources:
        if not isinstance(source, dict):
            raise common.CodexError("solver external source is not an object")
        for field in ("title", "url", "used_for", "verification"):
            if not isinstance(source.get(field), str):
                raise common.CodexError(
                    f"solver external source has invalid {field}"
                )
        if not source["url"].startswith(("https://", "http://")):
            raise common.CodexError("solver external source has invalid URL")
    warnings = result.get("warnings")
    if not isinstance(warnings, list) or not all(
        isinstance(value, str) for value in warnings
    ):
        raise common.CodexError("solver response has invalid warnings")
    claim_id_changes = _normalize_solver_claim_ids(result)
    claims = result.get("checkable_claims")
    if not isinstance(claims, list):
        raise common.CodexError("solver response has no checkable_claims array")
    claim_ids: set[str] = set()
    for claim in claims:
        if not isinstance(claim, dict):
            raise common.CodexError("a solver claim is not an object")
        claim_id = claim.get("id")
        if (
            not isinstance(claim_id, str)
            or not CLAIM_ID_RE.fullmatch(claim_id)
            or claim_id in claim_ids
        ):
            raise common.CodexError(
                f"invalid or duplicate solver claim ID: {claim_id!r}"
            )
        claim_ids.add(claim_id)
        if claim.get("type") not in {
            "proof",
            "lemma",
            "counterexample",
            "computation",
            "reduction",
            "reformulation",
            "obstruction",
            "other",
        }:
            raise common.CodexError(
                f"solver claim {claim_id} has an invalid type"
            )
        for field in ("statement", "support", "remaining_gap"):
            if not isinstance(claim.get(field), str):
                raise common.CodexError(
                    f"solver claim {claim_id} has invalid {field}"
                )
    if claimed_result_type == "none" and claims:
        raise common.CodexError(
            "none response unexpectedly contains claims"
        )
    if claimed_result_type != "none" and not claims:
        raise common.CodexError(
            f"{claimed_result_type} response contains no checkable claims"
        )
    if require_markdown:
        attempt_path = workspace / "attempt.md"
        contents = common.validate_markdown(
            attempt_path,
            description="solver attempt",
        )
        normalized_contents = _replace_claim_ids(contents, claim_id_changes)
        if normalized_contents != contents:
            attempt_path.write_text(normalized_contents, encoding="utf-8")
            contents = normalized_contents
        missing_ids = [
            claim_id for claim_id in claim_ids if claim_id not in contents
        ]
        if missing_ids:
            raise common.CodexError(
                "attempt.md omits claims: "
                + ", ".join(sorted(missing_ids))
            )
    artifacts = _validated_artifact_paths(workspace, result.get("artifacts"))
    result["artifacts"] = [
        path.relative_to(workspace).as_posix()
        for path in artifacts
    ]
    if claim_id_changes or result != common.read_json(
        result_path,
        description="solver response",
    ):
        common.write_json(result_path, result)
    return result, artifacts


def recover_alternate_attempt_markdown(
    work: SolveWork,
    workspace: Path,
) -> bool:
    """Use a substantive solution.md when the solver misnamed attempt.md."""
    attempt_path = workspace / "attempt.md"
    if attempt_path.is_file():
        return False
    result_path = workspace / "agent-result.json"
    raw_result = common.read_json(
        result_path,
        description="solver response",
    )
    source_path = workspace / "solution.md"
    source_artifact: str | None = None
    if not source_path.is_file():
        candidates: list[tuple[str, Path]] = []
        for value in raw_result.get("artifacts", []):
            if not isinstance(value, str):
                continue
            relative = _artifact_relative_path(workspace, value)
            if (
                relative is not None
                and len(relative.parts) == 1
                and relative.suffix.casefold() == ".md"
                and (workspace / relative.name).is_file()
            ):
                candidates.append((value, workspace / relative.name))
        if len(candidates) != 1:
            return False
        source_artifact, source_path = candidates[0]
    if source_artifact is not None:
        raw_result["artifacts"] = [
            value
            for value in raw_result.get("artifacts", [])
            if value != source_artifact
        ]
        common.write_json(result_path, raw_result)
    result, _ = validate_solver_result(
        result_path,
        workspace,
        require_markdown=False,
    )
    contents = common.validate_markdown(
        source_path,
        description="solver solution",
    ).rstrip()
    missing_claims = [
        claim for claim in result["checkable_claims"]
        if claim["id"] not in contents
    ]
    if missing_claims:
        lines = ["", "", "## Checkable claims", ""]
        for claim in missing_claims:
            lines.extend(
                [
                    f"### {claim['id']} — {claim['type']}",
                    "",
                    f"**Statement.** {claim['statement'].strip()}",
                    "",
                    f"**Support.** {claim['support'].strip()}",
                    "",
                    f"**Remaining gap.** {claim['remaining_gap'].strip()}",
                    "",
                ]
            )
        contents += "\n".join(lines).rstrip()
    warning = (
        f"Driver recovery: used the solver-written {source_path.name} as "
        "attempt.md"
        " and supplied any missing structured claim labels."
    )
    if warning not in result["warnings"]:
        result["warnings"].append(warning)
    common.write_json(result_path, result)
    attempt_path.write_text(contents.rstrip() + "\n", encoding="utf-8")
    return True


def _recovered_attempt_markdown(work: SolveWork, result: dict) -> str:
    """Render the auditable structured result when the sandbox blocked Markdown."""
    lines = [
        f"# {work.problem.id} — {work.problem.title}",
        "",
        "> **Driver recovery notice.** The elevated Windows sandbox blocked "
        "the solver from creating `attempt.md`. This document was generated "
        "from the completed structured solver response; it may be less "
        "detailed than the intended research narrative.",
        "",
        "## Solver summary",
        "",
        result["summary"].strip(),
        "",
        "## Checkable claims",
        "",
    ]
    claims = result["checkable_claims"]
    if not claims:
        lines.extend(["No checkable claim was reported.", ""])
    for claim in claims:
        lines.extend(
            [
                f"### {claim['id']} — {claim['type']}",
                "",
                f"**Statement.** {claim['statement'].strip()}",
                "",
                f"**Support.** {claim['support'].strip()}",
                "",
                f"**Remaining gap.** {claim['remaining_gap'].strip()}",
                "",
            ]
        )
    lines.extend(["## External sources", ""])
    sources = result["external_sources"]
    if not sources:
        lines.extend(["No external source was reported.", ""])
    for source in sources:
        lines.extend(
            [
                f"- [{source['title']}]({source['url']}) — "
                f"{source['used_for']} Verification: "
                f"{source['verification']}",
                "",
            ]
        )
    lines.extend(["## Warnings and limitations", ""])
    warnings = result["warnings"]
    if not warnings:
        lines.extend(["No warning was reported.", ""])
    else:
        lines.extend(f"- {warning}" for warning in warnings)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def recover_missing_attempt_markdown(
    work: SolveWork,
    workspace: Path,
) -> bool:
    """Recover a completed result whose only core write failure was ACL setup."""
    attempt_path = workspace / "attempt.md"
    if attempt_path.is_file():
        return False
    result_path = workspace / "agent-result.json"
    events_path = workspace / "events.jsonl"
    log_path = workspace / "run.log"
    if not codex_cli.structured_turn_is_complete(events_path, result_path):
        return False
    try:
        log = log_path.read_text(encoding="utf-8", errors="replace").lower()
    except OSError:
        return False
    if codex_cli.WINDOWS_SANDBOX_ACL_FAILURE not in log:
        return False
    result, _ = validate_solver_result(
        result_path,
        workspace,
        require_markdown=False,
    )
    warning = (
        "Driver recovery: attempt.md was synthesized from the completed "
        "structured response after the elevated Windows sandbox ACL failure."
    )
    if warning not in result["warnings"]:
        result["warnings"].append(warning)
        common.write_json(result_path, result)
    attempt_path.write_text(
        _recovered_attempt_markdown(work, result),
        encoding="utf-8",
    )
    return True


def _install_attempt(
    work: SolveWork,
    *,
    workspace: Path,
    result: dict,
    artifacts: Sequence[Path],
    config_digest: str | None,
    codex_version: str,
    options: codex_cli.ModelOptions,
    prior_history_digest: str,
    web_search: str = "live",
    recovered_from: Path | None = None,
) -> Path:
    destination = work.problem.directory / work.attempt_name
    if destination.exists():
        raise common.CodexError(
            f"attempt destination already exists: {destination}"
        )
    staging = Path(
        tempfile.mkdtemp(prefix=".attempt-install-", dir=work.problem.directory)
    )
    try:
        shutil.copyfile(workspace / "attempt.md", staging / "attempt.md")
        common.write_json(staging / "solver-result.json", result)
        manifest = {
            "schema_version": 3,
            "generated_at": common.utc_now(),
            "problem_id": work.problem.id,
            "problem_digest": common.problem_digest(work.problem),
            "analysis_digest": common.analysis_digest(
                work.problem.paper_directory
            ),
            "prior_attempt_history_digest": prior_history_digest,
            "triage_snapshot_digest": work.triage_snapshot_digest,
            "literature_snapshot_digest": work.literature_snapshot_digest,
            "research_guidance": work.guidance,
            "config_digest": config_digest,
            "codex_version": codex_version,
            "requested_model": options.model,
            "requested_reasoning_effort": options.reasoning_effort,
            "requested_fast_mode": options.fast,
            "requested_web_search": web_search,
            "claimed_result_type": result["claimed_result_type"],
            "claim_count": len(result["checkable_claims"]),
        }
        if recovered_from is not None:
            manifest.update(
                {
                    "recovered_from_workspace": recovered_from.name,
                    "original_workspace_preserved": True,
                }
            )
        common.write_json(staging / "manifest.json", manifest)
        shutil.copyfile(
            workspace / "events.jsonl",
            staging / "events.jsonl",
        )
        shutil.copyfile(workspace / "run.log", staging / "run.log")
        if artifacts:
            for source in artifacts:
                relative = source.relative_to(workspace)
                target = staging / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)
        os.replace(staging, destination)
    except OSError as exc:
        raise common.CodexError(
            f"could not install solver attempt; staging preserved at "
            f"{staging}: {exc}"
        ) from exc
    common.report_artifacts(
        [
            destination / "attempt.md",
            destination / "solver-result.json",
            *(destination / source.relative_to(workspace) for source in artifacts),
        ]
    )
    return destination


def _write_work_record(
    workspace: Path,
    work: SolveWork,
    *,
    config_digest: str,
    codex_version: str,
    options: codex_cli.ModelOptions,
    prior_history_digest: str,
    web_search: str = "live",
) -> None:
    common.write_json(
        workspace / WORK_RECORD_FILE,
        {
            "schema_version": 2,
            "problem_id": work.problem.id,
            "attempt_name": work.attempt_name,
            "research_guidance": work.guidance,
            "triage_snapshot_digest": work.triage_snapshot_digest,
            "literature_snapshot_digest": work.literature_snapshot_digest,
            "prior_attempt_history_digest": prior_history_digest,
            "config_digest": config_digest,
            "codex_version": codex_version,
            "requested_model": options.model,
            "requested_reasoning_effort": options.reasoning_effort,
            "requested_fast_mode": options.fast,
            "requested_web_search": web_search,
        },
    )


def _recovery_workspace_matches(
    workspace: Path,
    work: SolveWork,
    *,
    config_digest: str,
) -> tuple[bool, dict | None]:
    record = common.load_json(workspace / WORK_RECORD_FILE)
    if record is None or record.get("schema_version") != 2:
        return False, None
    return (
        record.get("problem_id") == work.problem.id
        and record.get("attempt_name") == work.attempt_name
        and record.get("research_guidance") == work.guidance
        and record.get("config_digest") == config_digest,
        record,
    )


def recover_solver_work(
    work: SolveWork,
    *,
    codex: str,
    codex_version: str,
    config_digest: str,
    options: codex_cli.ModelOptions,
    web_search: str = "live",
) -> SolveOutcome | None:
    """Install a matching completed preserved workspace without another turn."""
    candidates = sorted(
        work.problem.directory.glob(".solve-run-*"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for workspace in candidates:
        if not all(
            (workspace / name).is_file()
            for name in (
                "agent-result.json",
                "events.jsonl",
                "run.log",
            )
        ):
            continue
        matches, record = _recovery_workspace_matches(
            workspace,
            work,
            config_digest=config_digest,
        )
        if not matches:
            continue
        assert record is not None
        codex_cli.normalize_workspace_access(workspace, codex)
        recover_alternate_attempt_markdown(work, workspace)
        recover_missing_attempt_markdown(work, workspace)
        if not (workspace / "attempt.md").is_file():
            continue
        result, artifacts = validate_solver_result(
            workspace / "agent-result.json",
            workspace,
        )
        recorded_digest = record.get("prior_attempt_history_digest")
        prior_history_digest = (
            recorded_digest
            if isinstance(recorded_digest, str)
            else common.attempt_history_digest(work.problem)
        )
        destination = _install_attempt(
            work,
            workspace=workspace,
            result=result,
            artifacts=artifacts,
            config_digest=record["config_digest"],
            codex_version=record.get("codex_version", codex_version),
            options=options,
            prior_history_digest=prior_history_digest,
            web_search=record.get("requested_web_search", web_search),
            recovered_from=workspace,
        )
        attempt = review_solutions.AttemptRef(
            work.problem,
            destination,
            result,
        )
        return SolveOutcome(
            work,
            attempt,
            result["claimed_result_type"],
            len(result["checkable_claims"]),
            (
                f"recovered {result['claimed_result_type']}; "
                f"{len(result['checkable_claims'])} claim(s); "
                f"preserved {workspace.name}"
            ),
        )
    return None


def solve_work(
    work: SolveWork,
    *,
    codex: str,
    codex_version: str,
    prompt_template: str,
    schema_path: Path,
    config_digest: str,
    options: codex_cli.ModelOptions,
    web_search: str = "live",
    launch_interval: float = codex_cli.CODEX_LAUNCH_INTERVAL_SECONDS,
) -> SolveOutcome:
    """Run and install one adaptive solver attempt."""
    work.problem.directory.mkdir(parents=True, exist_ok=True)
    recovered = recover_solver_work(
        work,
        codex=codex,
        codex_version=codex_version,
        config_digest=config_digest,
        options=options,
        web_search=web_search,
    )
    if recovered is not None:
        return recovered
    workspace = Path(
        tempfile.mkdtemp(prefix=".solve-run-", dir=work.problem.directory)
    ).resolve()
    prior_history_digest = common.attempt_history_digest(work.problem)
    _write_work_record(
        workspace,
        work,
        config_digest=config_digest,
        codex_version=codex_version,
        options=options,
        prior_history_digest=prior_history_digest,
        web_search=web_search,
    )
    try:
        context = common.stage_context(
            workspace,
            [work.problem],
            include_paper=True,
            include_history=True,
            include_triage=work.triage_snapshot_digest is not None,
            include_literature=work.literature_snapshot_digest is not None,
        )
        prompt = render_prompt(
            prompt_template,
            work=work,
            context_directory=context,
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
        recover_alternate_attempt_markdown(work, workspace)
        recover_missing_attempt_markdown(work, workspace)
        result, artifacts = validate_solver_result(result_path, workspace)
        destination = _install_attempt(
            work,
            workspace=workspace,
            result=result,
            artifacts=artifacts,
            config_digest=config_digest,
            codex_version=codex_version,
            options=options,
            prior_history_digest=prior_history_digest,
            web_search=web_search,
        )
    except (common.CodexError, OSError) as exc:
        raise common.CodexError(
            common.preserved_workspace_message(exc, workspace)
        ) from exc
    warning = common.cleanup_workspace(
        workspace,
        installed_log=destination / "run.log",
    )
    attempt = review_solutions.AttemptRef(work.problem, destination, result)
    message = (
        f"{result['claimed_result_type']}; "
        f"{len(result['checkable_claims'])} claim(s)"
    )
    if warning:
        message += "; temporary workspace preserved"
    return SolveOutcome(
        work,
        attempt,
        result["claimed_result_type"],
        len(result["checkable_claims"]),
        message,
    )


def solve_many(
    work_items: Sequence[SolveWork],
    *,
    codex: str,
    codex_version: str,
    prompt_template: str,
    schema_path: Path,
    config_digest: str,
    options: codex_cli.ModelOptions,
    jobs: int,
    web_search: str = "live",
    on_finished: SolveFinishedCallback | None = None,
) -> tuple[list[SolveOutcome], list[tuple[SolveWork, str]]]:
    outcomes: list[SolveOutcome] = []
    failures: list[tuple[SolveWork, str]] = []
    if not work_items:
        return outcomes, failures
    with ThreadPoolExecutor(
        max_workers=min(jobs, len(work_items))
    ) as executor:
        future_to_work = {
            executor.submit(
                solve_work,
                work,
                codex=codex,
                codex_version=codex_version,
                prompt_template=prompt_template,
                schema_path=schema_path,
                config_digest=config_digest,
                options=options,
                web_search=web_search,
            ): work
            for work in work_items
        }
        for future in as_completed(future_to_work):
            work = future_to_work[future]
            try:
                outcome = future.result()
            except (common.CodexError, OSError) as exc:
                message = str(exc)
                failures.append((work, message))
                if on_finished is not None:
                    on_finished(work, None, message)
            else:
                outcomes.append(outcome)
                if on_finished is not None:
                    on_finished(work, outcome, None)
    return outcomes, failures


def _inherit_review_options(
    solver_options: codex_cli.ModelOptions,
    review_options: codex_cli.ModelOptions,
) -> codex_cli.ModelOptions:
    return codex_cli.ModelOptions(
        review_options.model or solver_options.model,
        review_options.reasoning_effort or solver_options.reasoning_effort,
        review_options.fast or solver_options.fast,
    )


def review_confirms_resolution(
    outcome: review_solutions.ReviewOutcome,
) -> bool:
    """Return whether a critic has confirmed a complete problem resolution."""
    return (
        outcome.correctness in {"plausible", "well_supported"}
        and outcome.coverage
        in {"complete", "complete_under_stated_interpretation"}
        and outcome.importance == "resolution"
    )


def next_round_work(work: SolveWork) -> SolveWork:
    """Build another attempt for a problem after its prior round installed."""
    return SolveWork(
        work.problem,
        work.guidance,
        common.next_attempt_number(work.problem),
        work.triage_snapshot_digest,
        common.literature_snapshot_digest(work.problem),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "run adaptive, full-paper solver attempts on selected problems"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  Solve two exact problems selected by their stored directories:
    python src/solve_open_problems.py paper/OP-00{1,4}

  Solve every current triage item classified attempt, then review progress:
    python src/solve_open_problems.py papers/edemaine \\
      --from-triage attempt --jobs 4

  Explicitly try one problem, even without current triage:
    python src/solve_open_problems.py papers/edemaine/arXiv-... \\
      --problem OP-003 --model gpt-5.6-sol \\
      --reasoning-effort xhigh --fast

  Include maybe items, but only report solver output without critics:
    python src/solve_open_problems.py papers/edemaine \\
      --from-triage attempt,maybe --review none

  Try each unresolved problem up to three times, reviewing between rounds:
    python src/solve_open_problems.py papers/edemaine \\
      --from-triage attempt --max-rounds 3

  Give every selected solver an additional research direction:
    python src/solve_open_problems.py paper/OP-001 \\
      --prompt "Try a computational search before committing to a proof"
""",
    )
    parser.add_argument(
        "paths",
        nargs="+",
        type=Path,
        help=(
            "paper/parent directories, or PAPER/OP-ID paths to "
            "select exact problems without another selection flag"
        ),
    )
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument(
        "--from-triage",
        metavar="CLASSES",
        help=(
            "solve current triage entries in these comma-separated classes "
            "(attempt,maybe,skip)"
        ),
    )
    selection.add_argument(
        "--problem",
        action="append",
        dest="problem_ids",
        metavar="OP-ID",
        help=(
            "explicitly solve this problem ID; may be repeated and does not "
            "require current triage"
        ),
    )
    selection.add_argument(
        "--all-problems",
        action="store_true",
        help="explicitly solve all selected papers' extracted problems",
    )
    parser.add_argument(
        "--exact-problem",
        action="append",
        dest="exact_problems",
        metavar="PAPER::OP-ID",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "-j",
        "--jobs",
        type=codex_cli.positive_integer,
        default=1,
        help="maximum concurrent solver agents (default: 1)",
    )
    parser.add_argument(
        "-r",
        "--max-rounds",
        type=codex_cli.positive_integer,
        default=1,
        metavar="N",
        help=(
            "maximum solver attempts per selected problem in this run; "
            "when review is enabled, review after each round and stop after "
            "a critic-confirmed resolution (default: 1)"
        ),
    )
    parser.add_argument(
        "--review",
        choices=("promising", "all", "none"),
        default="promising",
        help=(
            "run critics for checkable progress, every new attempt, or none "
            "(default: promising)"
        ),
    )
    parser.add_argument(
        "--review-jobs",
        type=codex_cli.positive_integer,
        help="maximum concurrent critics (default: --jobs)",
    )
    parser.add_argument(
        "--review-timeout-minutes",
        type=codex_cli.positive_number,
        default=review_solutions.DEFAULT_TIMEOUT_MINUTES,
        metavar="MINUTES",
        help=(
            "maximum wall-clock time for each critic "
            f"(default: {review_solutions.DEFAULT_TIMEOUT_MINUTES:g})"
        ),
    )
    parser.add_argument(
        "--include-literature-resolved",
        action="store_true",
        help=(
            "run even when a current literature search marks the exact "
            "problem resolved"
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report adaptive solver attempts without starting Codex",
    )
    parser.add_argument(
        "--codex",
        default="codex",
        help="Codex CLI executable or command name (default: codex)",
    )
    codex_cli.add_prompt_arguments(
        parser,
        default_template=DEFAULT_PROMPT_PATH,
        task="open-problem solver",
    )
    parser.add_argument(
        "--schema",
        type=Path,
        default=DEFAULT_SCHEMA_PATH,
        help=f"solver final-response schema (default: {DEFAULT_SCHEMA_PATH})",
    )
    codex_cli.add_prompt_arguments(
        parser,
        default_template=DEFAULT_REVIEW_PROMPT_PATH,
        task="solution critic",
        prefix="review",
    )
    parser.add_argument(
        "--review-schema",
        type=Path,
        default=DEFAULT_REVIEW_SCHEMA_PATH,
        help=f"critic final-response schema (default: {DEFAULT_REVIEW_SCHEMA_PATH})",
    )
    codex_cli.add_model_arguments(parser)
    codex_cli.add_model_arguments(parser, prefix="review")
    codex_cli.add_web_search_argument(parser, default="live")
    codex_cli.add_web_search_argument(
        parser,
        default="live",
        prefix="review",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    codex_cli.configure_utf8_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        input_paths, direct_exact = common.direct_problem_inputs(args.paths)
        if direct_exact and (
            args.from_triage is not None
            or args.problem_ids
            or args.all_problems
        ):
            raise common.CodexError(
                "direct problem paths cannot be combined with "
                "--from-triage, --problem, or --all-problems"
            )
        if not direct_exact and not (
            args.from_triage is not None
            or args.problem_ids
            or args.all_problems
        ):
            raise common.CodexError(
                "select work with PAPER/OP-ID paths, "
                "--from-triage, --problem, or --all-problems"
            )
        triage_classes = (
            common.parse_csv_values(
                args.from_triage,
                allowed=("attempt", "maybe", "skip"),
                label="--from-triage",
            )
            if args.from_triage is not None
            else None
        )
        problems = common.discover_problem_refs(
            input_paths,
            problem_ids=set(args.problem_ids) if args.problem_ids else None,
        )
        exact = set(direct_exact)
        if args.exact_problems:
            for value in args.exact_problems:
                try:
                    paper_value, problem_id = value.rsplit("::", 1)
                except ValueError as exc:
                    raise common.CodexError(
                        f"invalid exact problem selector: {value!r}"
                    ) from exc
                exact.add(
                    (
                        os.path.normcase(
                            str(Path(paper_value).expanduser().resolve())
                        ),
                        problem_id,
                    )
                )
        problems = common.filter_exact_problems(problems, exact)
        prompt_path = args.prompt_template.expanduser().resolve()
        schema_path = args.schema.expanduser().resolve()
        prompt_template = prompt_path.read_text(encoding="utf-8")
        prompt_template = codex_cli.with_user_prompt(
            prompt_template,
            args.prompt,
            task="open-problem solver",
        )
        schema_text = schema_path.read_text(encoding="utf-8")
        json.loads(schema_text)
        options = codex_cli.model_options_from_args(args)
        config_digest = codex_cli.semantic_config_digest(
            prompt_template,
            schema_text,
            options,
            web_search=args.web_search,
        )
        work_items, without_work, literature_resolved = build_work(
            problems,
            require_triage_classes=triage_classes,
            include_literature_resolved=args.include_literature_resolved,
        )
        review_prompt_path = args.review_prompt_template.expanduser().resolve()
        review_schema_path = args.review_schema.expanduser().resolve()
        review_prompt = review_prompt_path.read_text(encoding="utf-8")
        review_prompt = codex_cli.with_user_prompt(
            review_prompt,
            args.review_prompt,
            task="solution critic",
            option_name="--review-prompt",
        )
        review_schema_text = review_schema_path.read_text(encoding="utf-8")
        json.loads(review_schema_text)
        review_options = _inherit_review_options(
            options,
            codex_cli.model_options_from_args(args, prefix="review"),
        )
        review_config_digest = codex_cli.semantic_config_digest(
            review_prompt,
            review_schema_text,
            review_options,
            web_search=args.review_web_search or args.web_search,
        )
    except (
        common.CodexError,
        OSError,
        UnicodeError,
        json.JSONDecodeError,
    ) as exc:
        return codex_cli.report_error(parser, exc)

    if args.dry_run:
        if args.prompt is not None:
            print(f"Solver direction: {args.prompt.strip()}")
        if args.review_prompt is not None and args.review != "none":
            print(f"Critic direction: {args.review_prompt.strip()}")
        for work in work_items:
            suggestions = work.guidance.get("suggested_approaches", [])
            print(
                f"Would solve: {work.problem.paper_directory} "
                f"{work.problem.id}/{work.attempt_name} "
                f"(adaptive; {len(suggestions)} suggested approach(es))"
            )
        for problem in literature_resolved:
            print(
                f"Would skip resolved literature: "
                f"{problem.paper_directory} {problem.id}"
            )
        round_behavior = (
            "with review between rounds and critic-confirmed early stopping"
            if args.review != "none"
            else "without review or early stopping"
        )
        print(
            f"Selected {len(problems)} problem(s): "
            f"up to {len(work_items) * args.max_rounds} adaptive solver "
            f"attempt(s) across {args.max_rounds} round(s), "
            f"{round_behavior}; "
            f"{len(without_work)} without matching current triage; "
            f"{len(literature_resolved)} resolved by current literature."
        )
        return 0

    if not work_items:
        print(
            f"No solver work selected from {len(problems)} problem(s); "
            f"{len(without_work)} had no matching current triage; "
            f"{len(literature_resolved)} resolved by current literature."
        )
        return 0
    try:
        codex = codex_cli.resolve_codex_executable(args.codex)
        codex_version = codex_cli.read_codex_version(codex)
        # Probe once before announcing or submitting the batch.  Per-turn
        # probing remains as a guard against a helper that fails later, but a
        # persistently broken Windows helper must not create one workspace and
        # failure report per selected problem.
        codex_cli.require_secure_windows_sandbox(codex, PROJECT_ROOT)
    except common.CodexError as exc:
        return codex_cli.report_error(parser, exc)
    agent_count = min(args.jobs, len(work_items))
    if agent_count == 1:
        concurrency = "sequentially with 1 Codex agent"
    else:
        concurrency = f"with up to {agent_count} concurrent Codex agents"
    round_phrase = (
        "one round"
        if args.max_rounds == 1
        else f"up to {args.max_rounds} rounds"
    )
    scope = f"Solving {len(work_items)} problem(s) for {round_phrase}"
    print(
        f"{scope}, {concurrency}.",
        flush=True,
    )
    outcomes: list[SolveOutcome] = []
    failures: list[tuple[SolveWork, str]] = []
    review_outcomes: list[review_solutions.ReviewOutcome] = []
    review_failures: list[
        tuple[review_solutions.AttemptRef, str]
    ] = []
    review_skipped = 0
    confirmed_problems: set[Path] = set()
    active_work = work_items

    for round_number in range(1, args.max_rounds + 1):
        if not active_work:
            break
        if args.max_rounds > 1:
            print(
                f"Round {round_number}/{args.max_rounds}: solving "
                f"{len(active_work)} active problem(s).",
                flush=True,
            )
        round_finished = 0

        def report_finished(
            work: SolveWork,
            outcome: SolveOutcome | None,
            error: str | None,
        ) -> None:
            nonlocal round_finished
            round_finished += 1
            prefix = f"[{round_finished}/{len(active_work)}]"
            if args.max_rounds > 1:
                prefix = (
                    f"[round {round_number} "
                    f"{round_finished}/{len(active_work)}]"
                )
            if outcome is not None:
                print(
                    f"Completed {prefix}: {work.problem.paper_directory} "
                    f"{work.problem.id}/{work.attempt_name} "
                    f"({outcome.message})",
                    flush=True,
                )
            else:
                print(
                    f"Failed {prefix}: {work.problem.paper_directory} "
                    f"{work.problem.id}/{work.attempt_name}: {error}",
                    file=sys.stderr,
                    flush=True,
                )

        round_outcomes, round_failures = solve_many(
            active_work,
            codex=codex,
            codex_version=codex_version,
            prompt_template=prompt_template,
            schema_path=schema_path,
            config_digest=config_digest,
            options=options,
            jobs=args.jobs,
            web_search=args.web_search,
            on_finished=report_finished,
        )
        outcomes.extend(round_outcomes)
        failures.extend(round_failures)

        round_review_outcomes: list[
            review_solutions.ReviewOutcome
        ] = []
        if args.review != "none" and round_outcomes:
            attempts = [outcome.attempt for outcome in round_outcomes]
            if args.review == "promising":
                selected_attempts = [
                    attempt
                    for attempt in attempts
                    if review_solutions.is_promising(attempt)
                ]
            else:
                selected_attempts = attempts
            review_skipped += len(attempts) - len(selected_attempts)
        else:
            selected_attempts = []
        if selected_attempts:
            print(
                f"Reviewing {len(selected_attempts)} new attempt(s), with up "
                f"to {min(args.review_jobs or args.jobs, len(selected_attempts))} "
                f"Codex critic(s) at once."
            )
            review_finished_count = 0

            def report_review_finished(
                attempt: review_solutions.AttemptRef,
                outcome: review_solutions.ReviewOutcome | None,
                error: str | None,
            ) -> None:
                nonlocal review_finished_count
                review_finished_count += 1
                prefix = (
                    f"[{review_finished_count}/{len(selected_attempts)}]"
                )
                target = (
                    f"{attempt.problem.paper_directory} "
                    f"{attempt.problem.id}/{attempt.name}"
                )
                if outcome is not None:
                    verb = (
                        "Recovered"
                        if outcome.status == "recovered"
                        else "Reviewed"
                    )
                    print(
                        f"{prefix} {verb}: {target} ({outcome.message})",
                        flush=True,
                    )
                else:
                    print(
                        f"{prefix} Review failed: {target}: {error}",
                        file=sys.stderr,
                        flush=True,
                    )

            (
                round_review_outcomes,
                round_review_failures,
            ) = review_solutions.review_many(
                selected_attempts,
                codex=codex,
                codex_version=codex_version,
                prompt_template=review_prompt,
                schema_path=review_schema_path,
                config_digest=review_config_digest,
                options=review_options,
                jobs=args.review_jobs or args.jobs,
                web_search=args.review_web_search or args.web_search,
                timeout_seconds=args.review_timeout_minutes * 60,
                on_finished=report_review_finished,
            )
            review_outcomes.extend(round_review_outcomes)
            review_failures.extend(round_review_failures)

        resolved_this_round = {
            outcome.attempt.problem.directory
            for outcome in round_review_outcomes
            if review_confirms_resolution(outcome)
        }
        for problem_directory in sorted(resolved_this_round):
            confirmed_problems.add(problem_directory)
            matching = next(
                outcome
                for outcome in round_review_outcomes
                if outcome.attempt.problem.directory == problem_directory
                and review_confirms_resolution(outcome)
            )
            print(
                f"Critic confirmed a complete resolution: "
                f"{matching.attempt.problem.paper_directory} "
                f"{matching.attempt.problem.id}/{matching.attempt.name}.",
                flush=True,
            )

        if round_number == args.max_rounds:
            break
        active_work = [
            next_round_work(outcome.work)
            for outcome in round_outcomes
            if outcome.work.problem.directory not in resolved_this_round
        ]
        if active_work:
            print(
                f"Continuing {len(active_work)} unresolved problem(s) to "
                f"round {round_number + 1}/{args.max_rounds}.",
                flush=True,
            )

    priority_items = [
        outcome
        for outcome in review_outcomes
        if outcome.priority in {"medium", "high"}
    ]
    if priority_items:
        print("Human review recommended:")
        for outcome in sorted(
            priority_items,
            key=lambda item: (
                item.priority != "high",
                str(item.attempt.directory),
            ),
        ):
            print(
                f"  {outcome.priority}: {outcome.attempt.directory} "
                f"({outcome.correctness}; {outcome.coverage}; "
                f"{outcome.importance})"
            )
        print("Open the human-review dashboard with:")
        print(
            "  "
            + shlex.join(
                [
                    "python",
                    "src/human_review.py",
                    *(str(path) for path in args.paths),
                ]
            )
        )
    unreviewed_candidates = [
        outcome
        for outcome in outcomes
        if outcome.claimed_result_type in {"counterexample", "solution"}
        and not (outcome.attempt.directory / "review-result.json").is_file()
    ]
    if unreviewed_candidates:
        print("Unreviewed candidate results:")
        for outcome in unreviewed_candidates:
            print(f"  {outcome.attempt.directory}")
    print(
        f"Completed {len(outcomes)} solver attempt(s); "
        f"{len(failures)} solver failure(s); "
        f"{len(review_outcomes)} review(s); {review_skipped} review(s) "
        f"skipped for no checkable progress; "
        f"{len(review_failures)} review failure(s); "
        f"{len(confirmed_problems)} critic-confirmed resolution(s); "
        f"{len(literature_resolved)} problem(s) skipped as literature-"
        f"resolved; {len(priority_items)} recommended for human review."
    )
    return 1 if failures or review_failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
