#!/usr/bin/env python3
"""Triage extracted open problems, using past attempts as evidence."""

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
from typing import Sequence

import codex_cli
import open_problem_common as common


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROMPT_PATH = PROJECT_ROOT / "prompts" / "triage-open-problems.md"
DEFAULT_SCHEMA_PATH = (
    PROJECT_ROOT / "schemas" / "open-problem-triage.schema.json"
)
TRIAGE_CLASSES = ("attempt", "maybe", "skip")
APPROACH_MODES = (
    "proof",
    "counterexample",
    "computation",
    "reformulation",
    "special_case",
    "verification",
    "literature_check",
    "other",
)


@dataclass(frozen=True)
class TriageOutcome:
    problem: common.ProblemRef
    status: str
    classification: str
    suggestion_count: int
    message: str


def render_prompt(
    template: str,
    *,
    problem_ids: Sequence[str],
    context_directory: Path,
) -> str:
    return (
        template.replace(
            "{{PROBLEM_IDS}}",
            "\n".join(f"- {problem_id}" for problem_id in problem_ids),
        )
        .replace(
            "{{CONTEXT_DIRECTORY}}",
            codex_cli.path_for_codex(context_directory),
        )
    )


def validate_triage_result(
    result_path: Path,
    workspace: Path,
    problems: Sequence[common.ProblemRef],
) -> tuple[dict, dict[str, dict]]:
    result = common.read_json(result_path, description="triage response")
    if result.get("status") not in {"complete", "partial"}:
        raise common.CodexError("triage response has an invalid status")
    warnings = result.get("warnings")
    if not isinstance(warnings, list) or not all(
        isinstance(item, str) for item in warnings
    ):
        raise common.CodexError("triage response has invalid warnings")
    entries = result.get("triages")
    if not isinstance(entries, list):
        raise common.CodexError("triage response has no triages array")
    requested = {problem.id for problem in problems}
    by_id: dict[str, dict] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise common.CodexError("a triage entry is not an object")
        problem_id = entry.get("problem_id")
        if problem_id not in requested:
            raise common.CodexError(
                f"triage response contains unrequested problem {problem_id!r}"
            )
        if problem_id in by_id:
            raise common.CodexError(
                f"triage response duplicates problem {problem_id}"
            )
        classification = entry.get("classification")
        if classification not in TRIAGE_CLASSES:
            raise common.CodexError(
                f"triage response has invalid classification for {problem_id}"
            )
        if not isinstance(entry.get("rationale"), str) or not entry[
            "rationale"
        ].strip():
            raise common.CodexError(
                f"triage response has no rationale for {problem_id}"
            )
        for field in ("promising_features", "obstacles"):
            values = entry.get(field)
            if not isinstance(values, list) or not all(
                isinstance(value, str) for value in values
            ):
                raise common.CodexError(
                    f"triage response has invalid {field} for {problem_id}"
                )
        suggestions = entry.get("suggested_approaches")
        if not isinstance(suggestions, list):
            raise common.CodexError(
                f"triage response has invalid suggested_approaches for "
                f"{problem_id}"
            )
        suggestion_ids: set[str] = set()
        for suggestion in suggestions:
            if not isinstance(suggestion, dict):
                raise common.CodexError(
                    f"a suggested approach for {problem_id} is not an object"
                )
            suggestion_id = suggestion.get("id")
            if (
                not isinstance(suggestion_id, str)
                or not suggestion_id.strip()
                or suggestion_id in suggestion_ids
            ):
                raise common.CodexError(
                    f"invalid or duplicate suggested-approach ID for "
                    f"{problem_id}"
                )
            suggestion_ids.add(suggestion_id)
            if suggestion.get("mode") not in APPROACH_MODES:
                raise common.CodexError(
                    f"invalid mode for {problem_id} suggested approach "
                    f"{suggestion_id}"
                )
            for field in ("suggestion", "why_promising", "abandon_if"):
                if (
                    not isinstance(suggestion.get(field), str)
                    or not suggestion[field].strip()
                ):
                    raise common.CodexError(
                        f"{problem_id} suggested approach {suggestion_id} "
                        f"has no {field}"
                    )
        report = workspace / f"triage-{problem_id}.md"
        contents = common.validate_markdown(
            report,
            description=f"triage report for {problem_id}",
        )
        if problem_id not in contents:
            raise common.CodexError(
                f"triage report does not mention {problem_id}"
            )
        by_id[problem_id] = entry
    missing = requested.difference(by_id)
    if missing:
        raise common.CodexError(
            "triage response omitted: " + ", ".join(sorted(missing))
        )
    return result, by_id


def _install_triage(
    problem: common.ProblemRef,
    *,
    workspace: Path,
    root_result: dict,
    entry: dict,
    input_digest: str,
    config_digest: str,
    codex_version: str,
    options: codex_cli.ModelOptions,
) -> None:
    problem.directory.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(prefix=".triage-install-", dir=problem.directory)
    )
    try:
        shutil.copyfile(
            workspace / f"triage-{problem.id}.md",
            staging / common.TRIAGE_MARKDOWN,
        )
        common.write_json(
            staging / common.TRIAGE_RESULT,
            {
                **entry,
                "paper_title": problem.paper_title,
                "run_status": root_result["status"],
                "run_warnings": root_result["warnings"],
            },
        )
        manifest = {
            "schema_version": common.TRIAGE_MANIFEST_SCHEMA_VERSION,
            "generated_at": common.utc_now(),
            "input_digest": input_digest,
            "config_digest": config_digest,
            "analysis_digest": common.analysis_digest(
                problem.paper_directory
            ),
            "attempt_history_digest": common.attempt_history_digest(problem),
            "codex_version": codex_version,
            "requested_model": options.model,
            "requested_reasoning_effort": options.reasoning_effort,
            "requested_fast_mode": options.fast,
            "classification": entry["classification"],
            "suggestion_count": len(entry["suggested_approaches"]),
        }
        common.write_json(staging / common.TRIAGE_MANIFEST, manifest)
        shutil.copyfile(
            workspace / "events.jsonl",
            staging / common.TRIAGE_RUN_FILES[0],
        )
        shutil.copyfile(
            workspace / "run.log",
            staging / common.TRIAGE_RUN_FILES[1],
        )
        for name in (
            common.TRIAGE_MARKDOWN,
            common.TRIAGE_RESULT,
            common.TRIAGE_MANIFEST,
            *common.TRIAGE_RUN_FILES,
        ):
            os.replace(staging / name, problem.directory / name)
    except OSError as exc:
        raise common.CodexError(
            f"could not install triage for {problem.id}; staging preserved "
            f"at {staging}: {exc}"
        ) from exc
    shutil.rmtree(staging)


def triage_paper(
    problems: Sequence[common.ProblemRef],
    *,
    codex: str,
    codex_version: str,
    prompt_template: str,
    schema_path: Path,
    config_digest: str,
    options: codex_cli.ModelOptions,
    launch_interval: float = codex_cli.CODEX_LAUNCH_INTERVAL_SECONDS,
) -> list[TriageOutcome]:
    """Triage the selected stale problems from one paper in one Codex run."""
    if not problems:
        return []
    paper = problems[0].paper_directory
    attempts_root = paper / common.ATTEMPTS_DIRECTORY
    attempts_root.mkdir(parents=True, exist_ok=True)
    workspace = Path(
        tempfile.mkdtemp(prefix=".triage-run-", dir=attempts_root)
    ).resolve()
    input_digests = {
        problem.id: common.triage_input_digest(problem)
        for problem in problems
    }
    try:
        context = common.stage_context(
            workspace,
            problems,
            include_paper=False,
            include_history=True,
        )
        prompt = render_prompt(
            prompt_template,
            problem_ids=[problem.id for problem in problems],
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
        root_result, entries = validate_triage_result(
            result_path,
            workspace,
            problems,
        )
        outcomes: list[TriageOutcome] = []
        for problem in problems:
            entry = entries[problem.id]
            _install_triage(
                problem,
                workspace=workspace,
                root_result=root_result,
                entry=entry,
                input_digest=input_digests[problem.id],
                config_digest=config_digest,
                codex_version=codex_version,
                options=options,
            )
            outcomes.append(
                TriageOutcome(
                    problem,
                    "triaged",
                    entry["classification"],
                    len(entry["suggested_approaches"]),
                    f"{entry['classification']}; "
                    f"{len(entry['suggested_approaches'])} suggested "
                    "approach(es)",
                )
            )
    except (common.CodexError, OSError) as exc:
        raise common.CodexError(
            common.preserved_workspace_message(exc, workspace)
        ) from exc
    warning = common.cleanup_workspace(
        workspace,
        installed_log=problems[0].directory / common.TRIAGE_RUN_FILES[1],
    )
    if warning:
        outcomes = [
            TriageOutcome(
                outcome.problem,
                outcome.status,
                outcome.classification,
                outcome.suggestion_count,
                outcome.message + "; temporary workspace preserved",
            )
            for outcome in outcomes
        ]
    return outcomes


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "classify extracted open problems as attempt, maybe, or skip"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  Triage every stale problem under an author directory:
    python src/triage_open_problems.py papers/edemaine --jobs 4

  Triage two problem IDs with the latest model at extra-high reasoning:
    python src/triage_open_problems.py papers/edemaine/arXiv-... \\
      --problem OP-001 --problem OP-004 \\
      --model gpt-5.6-sol --reasoning-effort xhigh --fast
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
        help="only triage this problem ID; may be repeated",
    )
    parser.add_argument(
        "--explicitness",
        default="explicit,inferred,uncertain",
        metavar="KINDS",
        help=(
            "comma-separated explicitness values to include "
            "(default: explicit,inferred,uncertain)"
        ),
    )
    parser.add_argument(
        "-j",
        "--jobs",
        type=codex_cli.positive_integer,
        default=1,
        help="maximum concurrent per-paper Codex runs (default: 1)",
    )
    parser.add_argument(
        "-f",
        "--force",
        action="store_true",
        help="triage even when the analysis, history, and prompt are unchanged",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would be triaged without starting Codex",
    )
    parser.add_argument(
        "--solve",
        metavar="CLASSES",
        help=(
            "after triage, feed current selected problems in these "
            "comma-separated classes to solve_open_problems.py"
        ),
    )
    parser.add_argument(
        "--solve-review",
        choices=("promising", "all", "none"),
        default="promising",
        help=(
            "critic policy when --solve is used "
            "(default: promising)"
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
        help=f"triage prompt template (default: {DEFAULT_PROMPT_PATH})",
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
        explicitness = common.parse_csv_values(
            args.explicitness,
            allowed=common.EXPLICITNESS_VALUES,
            label="--explicitness",
        )
        problems = common.discover_problem_refs(
            args.paths,
            problem_ids=set(args.problem_ids) if args.problem_ids else None,
            explicitness=explicitness,
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
        if args.solve is not None:
            common.parse_csv_values(
                args.solve,
                allowed=TRIAGE_CLASSES,
                label="--solve",
            )
    except (
        common.CodexError,
        OSError,
        UnicodeError,
        json.JSONDecodeError,
    ) as exc:
        parser.error(str(exc))

    current: list[TriageOutcome] = []
    stale: list[common.ProblemRef] = []
    for problem in problems:
        if not args.force and common.triage_is_current(
            problem,
            config_digest=config_digest,
        ):
            result = common.triage_result(problem)
            assert result is not None
            current.append(
                TriageOutcome(
                    problem,
                    "current",
                    result["classification"],
                    len(result.get("suggested_approaches", [])),
                    "triage matches the analysis, history, and prompt",
                )
            )
        else:
            stale.append(problem)

    if args.dry_run:
        for problem in stale:
            print(
                f"Would triage: {problem.paper_directory} {problem.id}: "
                f"{problem.title}"
            )
        print(
            f"Selected {len(problems)} problem(s): {len(stale)} stale; "
            f"{len(current)} current."
        )
        return 0

    if stale:
        try:
            codex = codex_cli.resolve_codex_executable(args.codex)
            codex_version = codex_cli.read_codex_version(codex)
        except common.CodexError as exc:
            parser.error(str(exc))
    else:
        codex = args.codex
        codex_version = "not queried"

    outcomes = list(current)
    failures: list[tuple[Path, str]] = []
    grouped = common.group_by_paper(stale)
    if grouped:
        print(
            f"Triaging {len(stale)} stale problem(s) from "
            f"{len(grouped)} paper(s), with up to "
            f"{min(args.jobs, len(grouped))} Codex agent(s) at once."
        )
        with ThreadPoolExecutor(
            max_workers=min(args.jobs, len(grouped))
        ) as executor:
            future_to_paper = {
                executor.submit(
                    triage_paper,
                    paper_problems,
                    codex=codex,
                    codex_version=codex_version,
                    prompt_template=prompt_template,
                    schema_path=schema_path,
                    config_digest=config_digest,
                    options=options,
                ): paper
                for paper, paper_problems in grouped.items()
            }
            for future in as_completed(future_to_paper):
                paper = future_to_paper[future]
                try:
                    paper_outcomes = future.result()
                except (common.CodexError, OSError) as exc:
                    failures.append((paper, str(exc)))
                    print(f"Failed: {paper}: {exc}", file=sys.stderr)
                    continue
                outcomes.extend(paper_outcomes)
                for outcome in paper_outcomes:
                    print(
                        f"Triaged: {outcome.problem.paper_directory} "
                        f"{outcome.problem.id} ({outcome.message})"
                    )
    for outcome in current:
        print(
            f"Current: {outcome.problem.paper_directory} "
            f"{outcome.problem.id} ({outcome.classification}; "
            f"{outcome.suggestion_count} suggested approach(es))"
        )

    classifications = {
        value: sum(outcome.classification == value for outcome in outcomes)
        for value in TRIAGE_CLASSES
    }
    print(
        f"Completed {sum(item.status == 'triaged' for item in outcomes)} "
        f"triage(s); {len(current)} current; "
        f"{len(failures)} paper run(s) failed. Totals: "
        f"{classifications['attempt']} attempt, "
        f"{classifications['maybe']} maybe, "
        f"{classifications['skip']} skip."
    )
    if failures:
        return 1
    if args.solve is not None:
        import solve_open_problems

        selected = [outcome.problem for outcome in outcomes]
        paper_paths = sorted(
            {problem.paper_directory for problem in selected},
            key=lambda path: os.path.normcase(str(path)),
        )
        solver_arguments = [
            *(str(path) for path in paper_paths),
            "--from-triage",
            args.solve,
            "--review",
            args.solve_review,
            "--jobs",
            str(args.jobs),
            "--codex",
            args.codex,
        ]
        for problem in selected:
            solver_arguments.extend(
                (
                    "--exact-problem",
                    f"{problem.paper_directory}::{problem.id}",
                )
            )
        if options.model is not None:
            solver_arguments.extend(("--model", options.model))
        if options.reasoning_effort is not None:
            solver_arguments.extend(
                ("--reasoning-effort", options.reasoning_effort)
            )
        if options.fast:
            solver_arguments.append("--fast")
        print(
            f"Feeding {len(selected)} current triage item(s) into the solver."
        )
        return solve_open_problems.main(solver_arguments)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
