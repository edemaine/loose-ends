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
CORE_OUTPUT_PATHS = {"attempt.md"}
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
        normalized = value.replace("\\", "/")
        if normalized in CORE_OUTPUT_PATHS:
            continue
        relative = PurePosixPath(normalized)
        if (
            relative.is_absolute()
            or not relative.parts
            or relative.parts[0] != "artifacts"
            or ".." in relative.parts
            or normalized in seen
        ):
            raise common.CodexError(
                f"solver response has unsafe artifact path: {value!r}"
            )
        seen.add(normalized)
        path = workspace.joinpath(*relative.parts)
        if not path.is_file():
            raise common.CodexError(
                f"solver-listed artifact does not exist: {value}"
            )
        paths.append(path)
    return paths


def validate_solver_result(
    result_path: Path,
    workspace: Path,
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
    contents = common.validate_markdown(
        workspace / "attempt.md",
        description="solver attempt",
    )
    missing_ids = [claim_id for claim_id in claim_ids if claim_id not in contents]
    if missing_ids:
        raise common.CodexError(
            "attempt.md omits claims: " + ", ".join(sorted(missing_ids))
        )
    artifacts = _validated_artifact_paths(workspace, result.get("artifacts"))
    result["artifacts"] = [
        path.relative_to(workspace).as_posix()
        for path in artifacts
    ]
    return result, artifacts


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
                "attempt.md",
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "run one adaptive, full-paper solver attempt per selected problem"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
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
""",
    )
    parser.add_argument(
        "paths",
        nargs="+",
        type=Path,
        help="paper directories or parent directories containing papers",
    )
    selection = parser.add_mutually_exclusive_group(required=True)
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
    parser.add_argument(
        "--prompt",
        type=Path,
        default=DEFAULT_PROMPT_PATH,
        help=f"solver prompt template (default: {DEFAULT_PROMPT_PATH})",
    )
    parser.add_argument(
        "--schema",
        type=Path,
        default=DEFAULT_SCHEMA_PATH,
        help=f"solver final-response schema (default: {DEFAULT_SCHEMA_PATH})",
    )
    parser.add_argument(
        "--review-prompt",
        type=Path,
        default=DEFAULT_REVIEW_PROMPT_PATH,
        help=f"critic prompt template (default: {DEFAULT_REVIEW_PROMPT_PATH})",
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
            args.paths,
            problem_ids=set(args.problem_ids) if args.problem_ids else None,
        )
        if args.exact_problems:
            exact: set[tuple[str, str]] = set()
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
            problems = [
                problem
                for problem in problems
                if (
                    os.path.normcase(str(problem.paper_directory)),
                    problem.id,
                )
                in exact
            ]
            if not problems:
                raise common.CodexError(
                    "no open problems matched the exact selectors"
                )
        prompt_path = args.prompt.expanduser().resolve()
        schema_path = args.schema.expanduser().resolve()
        prompt_template = prompt_path.read_text(encoding="utf-8")
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
        review_prompt_path = args.review_prompt.expanduser().resolve()
        review_schema_path = args.review_schema.expanduser().resolve()
        review_prompt = review_prompt_path.read_text(encoding="utf-8")
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
        parser.error(str(exc))

    if args.dry_run:
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
        print(
            f"Selected {len(problems)} problem(s): "
            f"{len(work_items)} adaptive solver attempt(s); "
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
    except common.CodexError as exc:
        parser.error(str(exc))
    agent_count = min(args.jobs, len(work_items))
    if agent_count == 1:
        concurrency = "sequentially with 1 Codex agent"
    else:
        concurrency = f"with up to {agent_count} concurrent Codex agents"
    if len(work_items) == 1:
        scope = "Solving 1 problem with one adaptive attempt"
    else:
        scope = (
            f"Solving {len(work_items)} problems with one adaptive attempt "
            "per problem"
        )
    print(
        f"{scope}, {concurrency}.",
        flush=True,
    )
    finished_count = 0

    def report_finished(
        work: SolveWork,
        outcome: SolveOutcome | None,
        error: str | None,
    ) -> None:
        nonlocal finished_count
        finished_count += 1
        prefix = f"[{finished_count}/{len(work_items)}]"
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

    outcomes, failures = solve_many(
        work_items,
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

    review_outcomes: list[review_solutions.ReviewOutcome] = []
    review_failures: list[
        tuple[review_solutions.AttemptRef, str]
    ] = []
    review_skipped = 0
    if args.review != "none" and outcomes:
        attempts = [outcome.attempt for outcome in outcomes]
        if args.review == "promising":
            selected_attempts = [
                attempt
                for attempt in attempts
                if review_solutions.is_promising(attempt)
            ]
        else:
            selected_attempts = attempts
        review_skipped = len(attempts) - len(selected_attempts)
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

            review_outcomes, review_failures = review_solutions.review_many(
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
        f"{len(literature_resolved)} problem(s) skipped as literature-"
        f"resolved; {len(priority_items)} recommended for human review."
    )
    return 1 if failures or review_failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
