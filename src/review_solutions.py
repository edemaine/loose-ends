#!/usr/bin/env python3
"""Independently review checkable open-problem solver attempts."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from typing import Callable, Iterable, Sequence

import codex_cli
import open_problem_common as common


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROMPT_PATH = (
    PROJECT_ROOT / "prompts" / "review-open-problem-attempt.md"
)
DEFAULT_SCHEMA_PATH = (
    PROJECT_ROOT / "schemas" / "open-problem-review.schema.json"
)
CORRECTNESS_LEVELS = (
    "not_applicable",
    "incorrect",
    "major_gaps",
    "minor_gaps",
    "plausible",
    "well_supported",
)
REVIEWED_COVERAGE_LEVELS = (
    "none",
    "auxiliary",
    "special_case",
    "partial",
    "near_complete",
    "complete_under_stated_interpretation",
    "complete",
)
IMPORTANCE_LEVELS = ("none", "minor", "moderate", "major", "resolution")
VERIFICATION_CONFIDENCE_LEVELS = ("low", "medium", "high")
HUMAN_PRIORITY_LEVELS = ("none", "low", "medium", "high")
REVIEW_MODES = ("promising", "all")
WORK_RECORD_FILE = "review-work-record.json"
DEFAULT_TIMEOUT_MINUTES = 120.0
COMPLETION_GRACE_SECONDS = 30.0


@dataclass(frozen=True)
class AttemptRef:
    problem: common.ProblemRef
    directory: Path
    solver_result: dict

    @property
    def name(self) -> str:
        return self.directory.name


@dataclass(frozen=True)
class ReviewOutcome:
    attempt: AttemptRef
    status: str
    correctness: str
    coverage: str
    importance: str
    priority: str
    message: str


ReviewFinishedCallback = Callable[
    [AttemptRef, ReviewOutcome | None, str | None],
    None,
]


def derive_human_priority(result: dict) -> str:
    """Derive human-review priority from mathematical review dimensions."""
    correctness = result.get("correctness")
    coverage = result.get("reviewed_coverage")
    importance = result.get("importance")
    if (
        correctness in {"not_applicable", "incorrect"}
        or coverage == "none"
        or importance == "none"
    ):
        return "none"
    if correctness == "major_gaps":
        return "medium" if importance in {"major", "resolution"} else "low"
    if correctness == "minor_gaps":
        return (
            "medium"
            if importance in {"moderate", "major", "resolution"}
            else "low"
        )
    if correctness in {"plausible", "well_supported"}:
        if importance in {"major", "resolution"}:
            return "high"
        if importance == "moderate":
            return "medium"
        if importance == "minor":
            return "low"
    return "none"


def discover_attempt_refs(
    problems: Iterable[common.ProblemRef],
    *,
    attempt_names: set[str] | None = None,
) -> list[AttemptRef]:
    attempts: list[AttemptRef] = []
    for problem in problems:
        for directory in common.attempt_directories(problem):
            if attempt_names is not None and directory.name not in attempt_names:
                continue
            result = common.read_json(
                directory / "solver-result.json",
                description=f"solver result for {problem.id}/{directory.name}",
            )
            attempts.append(AttemptRef(problem, directory, result))
    if not attempts:
        raise common.CodexError("no matching solver attempts were found")
    return attempts


def is_promising(attempt: AttemptRef) -> bool:
    claims = attempt.solver_result.get("checkable_claims")
    return (
        common.claimed_result_type(attempt.solver_result) != "none"
        and isinstance(claims, list)
        and bool(claims)
    )


def review_is_current(
    attempt: AttemptRef,
    *,
    config_digest: str | None = None,
) -> bool:
    manifest = common.load_json(attempt.directory / "review-manifest.json")
    result = common.load_json(attempt.directory / "review-result.json")
    if manifest is None or result is None:
        return False
    if manifest.get("attempt_digest") != common.solver_attempt_digest(
        attempt.directory
    ):
        return False
    if (
        config_digest is not None
        and manifest.get("config_digest") != config_digest
    ):
        return False
    if result.get("correctness") not in CORRECTNESS_LEVELS:
        return False
    if result.get("reviewed_coverage") not in REVIEWED_COVERAGE_LEVELS:
        return False
    if result.get("importance") not in IMPORTANCE_LEVELS:
        return False
    if (
        result.get("verification_confidence")
        not in VERIFICATION_CONFIDENCE_LEVELS
    ):
        return False
    return result.get("human_priority") == derive_human_priority(result)


def render_prompt(
    template: str,
    *,
    attempt: AttemptRef,
    context_directory: Path,
) -> str:
    staged_attempt = (
        context_directory
        / "history"
        / attempt.problem.id
        / attempt.name
    )
    return (
        template.replace("{{PROBLEM_ID}}", attempt.problem.id)
        .replace(
            "{{ATTEMPT_DIRECTORY}}",
            codex_cli.path_for_codex(staged_attempt),
        )
        .replace(
            "{{CONTEXT_DIRECTORY}}",
            codex_cli.path_for_codex(context_directory),
        )
    )


def validate_review_result(
    result_path: Path,
    workspace: Path,
    attempt: AttemptRef,
) -> dict:
    result = common.read_json(result_path, description="review response")
    if result.get("correctness") not in CORRECTNESS_LEVELS:
        raise common.CodexError("review response has invalid correctness")
    if result.get("reviewed_coverage") not in REVIEWED_COVERAGE_LEVELS:
        raise common.CodexError("review response has invalid coverage")
    if result.get("importance") not in IMPORTANCE_LEVELS:
        raise common.CodexError("review response has invalid importance")
    if (
        result.get("verification_confidence")
        not in VERIFICATION_CONFIDENCE_LEVELS
    ):
        raise common.CodexError(
            "review response has invalid verification confidence"
        )
    if not isinstance(result.get("summary"), str) or not result[
        "summary"
    ].strip():
        raise common.CodexError("review response has no summary")
    for field in ("blocking_gaps", "recommended_next_steps", "warnings"):
        values = result.get(field)
        if not isinstance(values, list) or not all(
            isinstance(value, str) for value in values
        ):
            raise common.CodexError(f"review response has invalid {field}")
    expected_claims = attempt.solver_result.get("checkable_claims")
    if not isinstance(expected_claims, list):
        raise common.CodexError(
            f"{attempt.directory}/solver-result.json has invalid claims"
        )
    expected_ids = {
        claim.get("id")
        for claim in expected_claims
        if isinstance(claim, dict) and isinstance(claim.get("id"), str)
    }
    if len(expected_ids) != len(expected_claims):
        raise common.CodexError(
            f"{attempt.directory}/solver-result.json has invalid claim IDs"
        )
    reviews = result.get("claim_reviews")
    if not isinstance(reviews, list):
        raise common.CodexError("review response has no claim_reviews array")
    reviewed_ids: set[str] = set()
    for review in reviews:
        if not isinstance(review, dict):
            raise common.CodexError("a claim review is not an object")
        claim_id = review.get("claim_id")
        if claim_id not in expected_ids or claim_id in reviewed_ids:
            raise common.CodexError(
                f"review response has invalid or duplicate claim {claim_id!r}"
            )
        reviewed_ids.add(claim_id)
        if review.get("assessment") not in {
            "supported",
            "partially_supported",
            "unsupported",
            "incorrect",
        }:
            raise common.CodexError(
                f"review response has invalid assessment for {claim_id}"
            )
        if not isinstance(review.get("explanation"), str) or not review[
            "explanation"
        ].strip():
            raise common.CodexError(
                f"review response has no explanation for {claim_id}"
            )
    if reviewed_ids != expected_ids:
        missing = expected_ids.difference(reviewed_ids)
        raise common.CodexError(
            "review response omitted claims: " + ", ".join(sorted(missing))
        )
    claimed = common.claimed_result_type(attempt.solver_result)
    if claimed == "none" and (
        result["correctness"] != "not_applicable"
        or result["reviewed_coverage"] != "none"
        or result["importance"] != "none"
    ):
        raise common.CodexError(
            "a no-result attempt requires not_applicable correctness and "
            "none coverage/importance"
        )
    if result["importance"] == "resolution" and result[
        "reviewed_coverage"
    ] not in {"complete", "complete_under_stated_interpretation"}:
        raise common.CodexError(
            "resolution importance requires complete coverage"
        )
    result["human_priority"] = derive_human_priority(result)
    common.validate_markdown(
        workspace / "critique.md",
        description="review critique",
    )
    return result


def _install_review(
    attempt: AttemptRef,
    *,
    workspace: Path,
    result: dict,
    attempt_digest: str,
    config_digest: str,
    codex_version: str,
    options: codex_cli.ModelOptions,
    web_search: str,
    recovered_from: Path | None = None,
) -> None:
    staging = Path(
        tempfile.mkdtemp(prefix=".review-install-", dir=attempt.directory)
    )
    try:
        shutil.copyfile(
            workspace / "critique.md",
            staging / "critique.md",
        )
        common.write_json(staging / "review-result.json", result)
        manifest = {
                "schema_version": 2,
                "generated_at": common.utc_now(),
                "attempt_digest": attempt_digest,
                "config_digest": config_digest,
                "codex_version": codex_version,
                "requested_model": options.model,
                "requested_reasoning_effort": options.reasoning_effort,
                "requested_fast_mode": options.fast,
                "requested_web_search": web_search,
                "correctness": result["correctness"],
                "reviewed_coverage": result["reviewed_coverage"],
                "importance": result["importance"],
                "verification_confidence": result[
                    "verification_confidence"
                ],
                "human_priority": result["human_priority"],
            }
        if recovered_from is not None:
            manifest.update(
                {
                    "recovered_from_workspace": recovered_from.name,
                    "original_workspace_preserved": True,
                }
            )
        common.write_json(staging / "review-manifest.json", manifest)
        shutil.copyfile(
            workspace / "events.jsonl",
            staging / "review-events.jsonl",
        )
        shutil.copyfile(
            workspace / "run.log",
            staging / "review-run.log",
        )
        for name in (
            "critique.md",
            "review-result.json",
            "review-manifest.json",
            "review-events.jsonl",
            "review-run.log",
        ):
            os.replace(staging / name, attempt.directory / name)
    except OSError as exc:
        raise common.CodexError(
            f"could not install review; staging preserved at {staging}: {exc}"
        ) from exc
    shutil.rmtree(staging)


def _write_work_record(
    workspace: Path,
    attempt: AttemptRef,
    *,
    attempt_digest: str,
    config_digest: str,
    codex_version: str,
    options: codex_cli.ModelOptions,
    web_search: str,
) -> None:
    common.write_json(
        workspace / WORK_RECORD_FILE,
        {
            "schema_version": 1,
            "problem_id": attempt.problem.id,
            "attempt_name": attempt.name,
            "attempt_digest": attempt_digest,
            "config_digest": config_digest,
            "codex_version": codex_version,
            "requested_model": options.model,
            "requested_reasoning_effort": options.reasoning_effort,
            "requested_fast_mode": options.fast,
            "requested_web_search": web_search,
        },
    )


def recover_review(
    attempt: AttemptRef,
    *,
    codex: str,
    codex_version: str,
    config_digest: str,
    options: codex_cli.ModelOptions,
    web_search: str,
) -> ReviewOutcome | None:
    """Install a matching completed review workspace without another turn."""
    attempt_digest = common.solver_attempt_digest(attempt.directory)
    candidates = sorted(
        attempt.problem.directory.glob(".review-run-*"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for workspace in candidates:
        record = common.load_json(workspace / WORK_RECORD_FILE)
        if record is None or record.get("schema_version") != 1:
            continue
        if not (
            record.get("problem_id") == attempt.problem.id
            and record.get("attempt_name") == attempt.name
            and record.get("attempt_digest") == attempt_digest
            and record.get("config_digest") == config_digest
        ):
            continue
        codex_cli.normalize_workspace_access(workspace, codex)
        if not all(
            (workspace / name).is_file()
            for name in (
                "agent-result.json",
                "critique.md",
                "events.jsonl",
                "run.log",
            )
        ):
            continue
        result = validate_review_result(
            workspace / "agent-result.json",
            workspace,
            attempt,
        )
        _install_review(
            attempt,
            workspace=workspace,
            result=result,
            attempt_digest=attempt_digest,
            config_digest=config_digest,
            codex_version=record.get("codex_version", codex_version),
            options=options,
            web_search=record.get("requested_web_search", web_search),
            recovered_from=workspace,
        )
        return ReviewOutcome(
            attempt,
            "recovered",
            result["correctness"],
            result["reviewed_coverage"],
            result["importance"],
            result["human_priority"],
            f"recovered {result['correctness']}; coverage "
            f"{result['reviewed_coverage']}; importance "
            f"{result['importance']}; priority "
            f"{result['human_priority']}; preserved {workspace.name}",
        )
    return None


def review_attempt(
    attempt: AttemptRef,
    *,
    codex: str,
    codex_version: str,
    prompt_template: str,
    schema_path: Path,
    config_digest: str,
    options: codex_cli.ModelOptions,
    web_search: str = "live",
    launch_interval: float = codex_cli.CODEX_LAUNCH_INTERVAL_SECONDS,
    timeout_seconds: float = DEFAULT_TIMEOUT_MINUTES * 60,
    allow_recovery: bool = True,
) -> ReviewOutcome:
    """Run and install one independent review."""
    if allow_recovery:
        recovered = recover_review(
            attempt,
            codex=codex,
            codex_version=codex_version,
            config_digest=config_digest,
            options=options,
            web_search=web_search,
        )
        if recovered is not None:
            return recovered
    workspace = Path(
        tempfile.mkdtemp(prefix=".review-run-", dir=attempt.problem.directory)
    ).resolve()
    attempt_digest = common.solver_attempt_digest(attempt.directory)
    try:
        _write_work_record(
            workspace,
            attempt,
            attempt_digest=attempt_digest,
            config_digest=config_digest,
            codex_version=codex_version,
            options=options,
            web_search=web_search,
        )
        context = common.stage_context(
            workspace,
            [attempt.problem],
            include_paper=True,
            include_history=True,
            include_triage=True,
            include_literature=False,
            exclude_review_for=attempt.directory,
        )
        prompt = render_prompt(
            prompt_template,
            attempt=attempt,
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
            timeout_seconds=timeout_seconds,
            completion_grace_seconds=COMPLETION_GRACE_SECONDS,
        )
        result = validate_review_result(result_path, workspace, attempt)
        _install_review(
            attempt,
            workspace=workspace,
            result=result,
            attempt_digest=attempt_digest,
            config_digest=config_digest,
            codex_version=codex_version,
            options=options,
            web_search=web_search,
        )
    except (common.CodexError, OSError) as exc:
        raise common.CodexError(
            common.preserved_workspace_message(exc, workspace)
        ) from exc
    warning = common.cleanup_workspace(
        workspace,
        installed_log=attempt.directory / "review-run.log",
    )
    message = (
        f"{result['correctness']}; coverage {result['reviewed_coverage']}; "
        f"importance {result['importance']}; priority "
        f"{result['human_priority']}"
    )
    if warning:
        message += "; temporary workspace preserved"
    return ReviewOutcome(
        attempt,
        "reviewed",
        result["correctness"],
        result["reviewed_coverage"],
        result["importance"],
        result["human_priority"],
        message,
    )


def review_many(
    attempts: Sequence[AttemptRef],
    *,
    codex: str,
    codex_version: str,
    prompt_template: str,
    schema_path: Path,
    config_digest: str,
    options: codex_cli.ModelOptions,
    jobs: int,
    web_search: str = "live",
    timeout_seconds: float = DEFAULT_TIMEOUT_MINUTES * 60,
    allow_recovery: bool = True,
    on_finished: ReviewFinishedCallback | None = None,
) -> tuple[list[ReviewOutcome], list[tuple[AttemptRef, str]]]:
    outcomes: list[ReviewOutcome] = []
    failures: list[tuple[AttemptRef, str]] = []
    if not attempts:
        return outcomes, failures
    with ThreadPoolExecutor(max_workers=min(jobs, len(attempts))) as executor:
        future_to_attempt = {
            executor.submit(
                review_attempt,
                attempt,
                codex=codex,
                codex_version=codex_version,
                prompt_template=prompt_template,
                schema_path=schema_path,
                config_digest=config_digest,
                options=options,
                web_search=web_search,
                timeout_seconds=timeout_seconds,
                allow_recovery=allow_recovery,
            ): attempt
            for attempt in attempts
        }
        for future in as_completed(future_to_attempt):
            attempt = future_to_attempt[future]
            try:
                outcome = future.result()
            except (common.CodexError, OSError) as exc:
                message = str(exc)
                failures.append((attempt, message))
                if on_finished is not None:
                    on_finished(attempt, None, message)
            else:
                outcomes.append(outcome)
                if on_finished is not None:
                    on_finished(attempt, outcome, None)
    return outcomes, failures


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="independently critique solver attempts worth checking",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  Review pending attempts that contain checkable progress:
    python src/review_solutions.py papers/edemaine --jobs 4

  Review every attempt for one problem with extra-high reasoning:
    python src/review_solutions.py papers/edemaine/arXiv-... \\
      --problem OP-001 --mode all \\
      --model gpt-5.6-sol --reasoning-effort xhigh
""",
    )
    parser.add_argument(
        "paths",
        nargs="+",
        type=Path,
        help="paper directories or parent directories containing papers",
    )
    parser.add_argument(
        "--problem",
        action="append",
        dest="problem_ids",
        metavar="OP-ID",
        help="only review attempts for this problem ID; may be repeated",
    )
    parser.add_argument(
        "--attempt",
        action="append",
        dest="attempt_names",
        metavar="ATTEMPT-NNN",
        help="only review this attempt directory name; may be repeated",
    )
    parser.add_argument(
        "--mode",
        choices=REVIEW_MODES,
        default="promising",
        help=(
            "review attempts with checkable progress, or all attempts "
            "(default: promising)"
        ),
    )
    parser.add_argument(
        "-j",
        "--jobs",
        type=codex_cli.positive_integer,
        default=1,
        help="maximum concurrent review agents (default: 1)",
    )
    parser.add_argument(
        "-f",
        "--force",
        action="store_true",
        help="replace a current review",
    )
    parser.add_argument(
        "--timeout-minutes",
        type=codex_cli.positive_number,
        default=DEFAULT_TIMEOUT_MINUTES,
        metavar="MINUTES",
        help=(
            "maximum wall-clock time for each critic "
            f"(default: {DEFAULT_TIMEOUT_MINUTES:g})"
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report pending reviews without starting Codex",
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
        help=f"review prompt template (default: {DEFAULT_PROMPT_PATH})",
    )
    parser.add_argument(
        "--schema",
        type=Path,
        default=DEFAULT_SCHEMA_PATH,
        help=f"final-response JSON schema (default: {DEFAULT_SCHEMA_PATH})",
    )
    codex_cli.add_model_arguments(parser)
    codex_cli.add_web_search_argument(parser, default="live")
    return parser


def main(argv: list[str] | None = None) -> int:
    codex_cli.configure_utf8_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        problems = common.discover_problem_refs(
            args.paths,
            problem_ids=set(args.problem_ids) if args.problem_ids else None,
        )
        attempts = discover_attempt_refs(
            problems,
            attempt_names=(
                set(args.attempt_names) if args.attempt_names else None
            ),
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
    except (
        common.CodexError,
        OSError,
        UnicodeError,
        json.JSONDecodeError,
    ) as exc:
        parser.error(str(exc))

    eligible = [
        attempt
        for attempt in attempts
        if args.mode == "all" or is_promising(attempt)
    ]
    skipped = len(attempts) - len(eligible)
    current: list[ReviewOutcome] = []
    pending: list[AttemptRef] = []
    for attempt in eligible:
        if not args.force and review_is_current(
            attempt,
            config_digest=config_digest,
        ):
            result = common.read_json(
                attempt.directory / "review-result.json",
                description="review result",
            )
            current.append(
                ReviewOutcome(
                    attempt,
                    "current",
                    result["correctness"],
                    result["reviewed_coverage"],
                    result["importance"],
                    result["human_priority"],
                    "review matches the attempt and prompt",
                )
            )
        else:
            pending.append(attempt)

    if args.dry_run:
        for attempt in pending:
            print(
                f"Would review: {attempt.problem.paper_directory} "
                f"{attempt.problem.id}/{attempt.name}"
            )
        print(
            f"Selected {len(attempts)} attempt(s): {len(pending)} pending; "
            f"{len(current)} current; {skipped} skipped by mode."
        )
        return 0

    if pending:
        try:
            codex = codex_cli.resolve_codex_executable(args.codex)
            codex_version = codex_cli.read_codex_version(codex)
        except common.CodexError as exc:
            parser.error(str(exc))
        print(
            f"Reviewing {len(pending)} attempt(s), with up to "
            f"{min(args.jobs, len(pending))} Codex critic(s) at once."
        )
        finished_count = 0

        def report_finished(
            attempt: AttemptRef,
            outcome: ReviewOutcome | None,
            error: str | None,
        ) -> None:
            nonlocal finished_count
            finished_count += 1
            prefix = f"[{finished_count}/{len(pending)}]"
            target = (
                f"{attempt.problem.paper_directory} "
                f"{attempt.problem.id}/{attempt.name}"
            )
            if error is not None:
                print(f"{prefix} Failed: {target}: {error}", file=sys.stderr)
            else:
                assert outcome is not None
                verb = (
                    "Recovered"
                    if outcome.status == "recovered"
                    else "Reviewed"
                )
                print(f"{prefix} {verb}: {target} ({outcome.message})")
            sys.stdout.flush()
            sys.stderr.flush()

        outcomes, failures = review_many(
            pending,
            codex=codex,
            codex_version=codex_version,
            prompt_template=prompt_template,
            schema_path=schema_path,
            config_digest=config_digest,
            options=options,
            jobs=args.jobs,
            web_search=args.web_search,
            timeout_seconds=args.timeout_minutes * 60,
            allow_recovery=not args.force,
            on_finished=report_finished,
        )
    else:
        outcomes, failures = [], []

    for outcome in current:
        print(
            f"Current: {outcome.attempt.problem.paper_directory} "
            f"{outcome.attempt.problem.id}/{outcome.attempt.name} "
            f"({outcome.correctness}; coverage {outcome.coverage}; "
            f"importance {outcome.importance}; priority "
            f"{outcome.priority})"
        )
    combined = outcomes + current
    priority_items = [
        outcome
        for outcome in combined
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
    print(
        f"Completed {len(outcomes)} review(s); {len(current)} current; "
        f"{skipped} skipped by mode; {len(failures)} failed. "
        f"Human-priority total: {len(priority_items)} medium/high."
    )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
