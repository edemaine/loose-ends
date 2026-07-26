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
from typing import Iterable, Sequence

import codex_cli
import open_problem_common as common


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROMPT_PATH = (
    PROJECT_ROOT / "prompts" / "review-open-problem-attempt.md"
)
DEFAULT_SCHEMA_PATH = (
    PROJECT_ROOT / "schemas" / "open-problem-review.schema.json"
)
VERDICTS = (
    "invalid",
    "no_progress",
    "useful_but_flawed",
    "plausible_progress",
    "strong_candidate",
)
ATTENTION_LEVELS = ("none", "low", "medium", "high")
REVIEW_MODES = ("promising", "all")


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
    verdict: str
    attention: str
    message: str


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
        attempt.solver_result.get("status") != "no_checkable_progress"
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
    return result.get("verdict") in VERDICTS


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
    if result.get("verdict") not in VERDICTS:
        raise common.CodexError("review response has an invalid verdict")
    if result.get("attention") not in ATTENTION_LEVELS:
        raise common.CodexError("review response has invalid attention")
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
        common.write_json(
            staging / "review-manifest.json",
            {
                "schema_version": 1,
                "generated_at": common.utc_now(),
                "attempt_digest": attempt_digest,
                "config_digest": config_digest,
                "codex_version": codex_version,
                "requested_model": options.model,
                "requested_reasoning_effort": options.reasoning_effort,
                "requested_fast_mode": options.fast,
                "verdict": result["verdict"],
                "attention": result["attention"],
            },
        )
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


def review_attempt(
    attempt: AttemptRef,
    *,
    codex: str,
    codex_version: str,
    prompt_template: str,
    schema_path: Path,
    config_digest: str,
    options: codex_cli.ModelOptions,
    launch_interval: float = codex_cli.CODEX_LAUNCH_INTERVAL_SECONDS,
) -> ReviewOutcome:
    """Run and install one independent review."""
    workspace = Path(
        tempfile.mkdtemp(prefix=".review-run-", dir=attempt.problem.directory)
    ).resolve()
    attempt_digest = common.solver_attempt_digest(attempt.directory)
    try:
        context = common.stage_context(
            workspace,
            [attempt.problem],
            include_paper=True,
            include_history=True,
            include_triage=True,
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
            launch_interval=launch_interval,
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
        )
    except (common.CodexError, OSError) as exc:
        raise common.CodexError(
            common.preserved_workspace_message(exc, workspace)
        ) from exc
    warning = common.cleanup_workspace(
        workspace,
        installed_log=attempt.directory / "review-run.log",
    )
    message = f"{result['verdict']}; attention {result['attention']}"
    if warning:
        message += "; temporary workspace preserved"
    return ReviewOutcome(
        attempt,
        "reviewed",
        result["verdict"],
        result["attention"],
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
            ): attempt
            for attempt in attempts
        }
        for future in as_completed(future_to_attempt):
            attempt = future_to_attempt[future]
            try:
                outcomes.append(future.result())
            except (common.CodexError, OSError) as exc:
                failures.append((attempt, str(exc)))
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
                    result["verdict"],
                    result["attention"],
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
        outcomes, failures = review_many(
            pending,
            codex=codex,
            codex_version=codex_version,
            prompt_template=prompt_template,
            schema_path=schema_path,
            config_digest=config_digest,
            options=options,
            jobs=args.jobs,
        )
    else:
        outcomes, failures = [], []

    for outcome in outcomes:
        print(
            f"Reviewed: {outcome.attempt.problem.paper_directory} "
            f"{outcome.attempt.problem.id}/{outcome.attempt.name} "
            f"({outcome.message})"
        )
    for outcome in current:
        print(
            f"Current: {outcome.attempt.problem.paper_directory} "
            f"{outcome.attempt.problem.id}/{outcome.attempt.name} "
            f"({outcome.verdict}; attention {outcome.attention})"
        )
    for attempt, message in failures:
        print(
            f"Failed: {attempt.problem.paper_directory} "
            f"{attempt.problem.id}/{attempt.name}: {message}",
            file=sys.stderr,
        )
    combined = outcomes + current
    attention = [
        outcome
        for outcome in combined
        if outcome.attention in {"medium", "high"}
    ]
    if attention:
        print("Human attention recommended:")
        for outcome in sorted(
            attention,
            key=lambda item: (
                item.attention != "high",
                str(item.attempt.directory),
            ),
        ):
            print(
                f"  {outcome.attention}: {outcome.attempt.directory} "
                f"({outcome.verdict})"
            )
    print(
        f"Completed {len(outcomes)} review(s); {len(current)} current; "
        f"{skipped} skipped by mode; {len(failures)} failed. "
        f"Human-attention total: {len(attention)} medium/high."
    )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
