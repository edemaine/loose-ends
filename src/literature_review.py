#!/usr/bin/env python3
"""Review later literature for selected paper open problems."""

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
DEFAULT_PROMPT_PATH = (
    PROJECT_ROOT / "prompts" / "search-open-problem-literature.md"
)
DEFAULT_SCHEMA_PATH = (
    PROJECT_ROOT / "schemas" / "open-problem-literature.schema.json"
)
DEFAULT_TRIAGE_CLASSES = "attempt,maybe"
WORK_RECORD_FILE = "literature-work-record.json"
STATUS_CORRECTION_PREFIX = "Driver downgraded `resolved` to `uncertain`: "
RESOLUTION_STATUSES = (
    "resolved",
    "partially_resolved",
    "no_resolution_found",
    "uncertain",
)
CONFIDENCE_LEVELS = ("low", "medium", "high")
SOURCE_ROLES = (
    "resolution",
    "partial_result",
    "special_case",
    "technique",
    "counterexample",
    "lower_bound",
    "survey",
    "terminology",
    "other",
)
SOURCE_PRIORITIES = ("high", "medium", "low")
SOURCE_TYPES = (
    "primary_source",
    "secondary_source",
    "search_result_only",
)


@dataclass(frozen=True)
class LiteratureOutcome:
    problem: common.ProblemRef
    status: str
    resolution_status: str
    source_count: int
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
        ).replace(
            "{{CONTEXT_DIRECTORY}}",
            codex_cli.path_for_codex(context_directory),
        )
    )


def render_literature_markdown(entry: dict) -> str:
    """Render a validated structured entry as a readable report."""
    lines = [
        f"# Literature search {entry['problem_id']}",
        "",
        "## Resolution status",
        "",
        f"**{entry['resolution_status']}** "
        f"(confidence: **{entry['confidence']}**)",
        "",
        entry["status_summary"],
        "",
        "## Exact formulation audit",
        "",
        entry["exact_match_analysis"],
        "",
        "## Residual problem",
        "",
        entry["residual_problem"] or "None identified.",
        "",
        "## Sources useful to a solver",
        "",
    ]
    sources = entry["sources"]
    if not sources:
        lines.extend(("No useful source was verified in this search.", ""))
    for source in sources:
        authors = ", ".join(source["authors"]) or "Unknown authors"
        year = f" ({source['publication_year']})" if source["publication_year"] else ""
        lines.extend(
            (
                f"### {source['id']} — [{source['title']}]({source['url']})",
                "",
                f"**Authors:** {authors}{year}",
                "",
                f"**Role and priority:** `{source['role']}`, "
                f"`{source['priority']}`; `{source['source_type']}`",
                "",
                f"**Result:** {source['result_statement']}",
                "",
                f"**Why useful:** {source['relevance']}",
                "",
                f"**Limitations:** {source['limitations']}",
                "",
            )
        )
    lines.extend(("## Solver briefing", "", entry["solver_briefing"], ""))
    queries = entry["search_queries"]
    lines.extend(("## Search queries", ""))
    lines.extend(
        (f"- `{query}`" for query in queries)
        if queries
        else ("- None recorded.",)
    )
    warnings = entry["warnings"]
    if warnings:
        lines.extend(("", "## Warnings", ""))
        lines.extend(f"- {warning}" for warning in warnings)
    return "\n".join(lines).rstrip() + "\n"


def _validate_string_list(value: object, description: str) -> list[str]:
    if not isinstance(value, list) or not all(
        isinstance(item, str) for item in value
    ):
        raise common.CodexError(f"literature response has invalid {description}")
    return value


def _validate_source(source: object, problem_id: str) -> dict:
    if not isinstance(source, dict):
        raise common.CodexError(
            f"a literature source for {problem_id} is not an object"
        )
    source_id = source.get("id")
    if not isinstance(source_id, str) or not source_id.strip():
        raise common.CodexError(
            f"invalid literature source ID for {problem_id}: "
            f"{source_id!r}"
        )
    source_id = source_id.strip()
    source["id"] = source_id
    if source.get("role") not in SOURCE_ROLES:
        raise common.CodexError(
            f"literature source {source_id} has invalid role"
        )
    if source.get("priority") not in SOURCE_PRIORITIES:
        raise common.CodexError(
            f"literature source {source_id} has invalid priority"
        )
    if source.get("source_type") not in SOURCE_TYPES:
        raise common.CodexError(
            f"literature source {source_id} has invalid source_type"
        )
    for field in (
        "title",
        "publication_year",
        "url",
        "result_statement",
        "relevance",
        "limitations",
    ):
        if not isinstance(source.get(field), str):
            raise common.CodexError(
                f"literature source {source_id} has invalid {field}"
            )
    if not source["title"].strip():
        raise common.CodexError(
            f"literature source {source_id} has no title"
        )
    if not source["url"].startswith(("https://", "http://")):
        raise common.CodexError(
            f"literature source {source_id} has invalid URL"
        )
    _validate_string_list(source.get("authors"), f"authors for {source_id}")
    return source


def validate_literature_result(
    result_path: Path,
    workspace: Path,
    problems: Sequence[common.ProblemRef],
) -> tuple[dict, dict[str, dict]]:
    result = common.read_json(result_path, description="literature response")
    if result.get("status") not in {"complete", "partial"}:
        raise common.CodexError("literature response has invalid run status")
    root_warnings = _validate_string_list(
        result.get("warnings"), "root warnings"
    )
    entries = result.get("literature")
    if not isinstance(entries, list):
        raise common.CodexError("literature response has no literature array")
    requested = {problem.id for problem in problems}
    by_id: dict[str, dict] = {}
    synthesized: list[str] = []
    status_repairs: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise common.CodexError("a literature entry is not an object")
        problem_id = entry.get("problem_id")
        if problem_id not in requested:
            raise common.CodexError(
                f"literature response contains unrequested problem "
                f"{problem_id!r}"
            )
        if problem_id in by_id:
            raise common.CodexError(
                f"literature response duplicates problem {problem_id}"
            )
        resolution = entry.get("resolution_status")
        confidence = entry.get("confidence")
        if resolution not in RESOLUTION_STATUSES:
            raise common.CodexError(
                f"literature response has invalid status for {problem_id}"
            )
        if confidence not in CONFIDENCE_LEVELS:
            raise common.CodexError(
                f"literature response has invalid confidence for {problem_id}"
            )
        for field in (
            "status_summary",
            "exact_match_analysis",
            "residual_problem",
            "solver_briefing",
        ):
            if not isinstance(entry.get(field), str):
                raise common.CodexError(
                    f"literature response has invalid {field} for {problem_id}"
                )
        if not entry["status_summary"].strip():
            raise common.CodexError(
                f"literature response has no status summary for {problem_id}"
            )
        if resolution == "partially_resolved" and not entry[
            "residual_problem"
        ].strip():
            raise common.CodexError(
                f"partial resolution has no residual problem for {problem_id}"
            )
        sources = entry.get("sources")
        if not isinstance(sources, list):
            raise common.CodexError(
                f"literature response has invalid sources for {problem_id}"
            )
        validated_sources = [
            _validate_source(source, problem_id) for source in sources
        ]
        _validate_string_list(
            entry.get("search_queries"),
            f"search_queries for {problem_id}",
        )
        entry_warnings = _validate_string_list(
            entry.get("warnings"), f"warnings for {problem_id}"
        )
        if resolution == "resolved":
            verified_resolution = any(
                source["role"] in {"resolution", "counterexample"}
                and source["source_type"] == "primary_source"
                for source in validated_sources
            )
            reasons = []
            if confidence != "high":
                reasons.append(f"confidence was {confidence}, not high")
            if not verified_resolution:
                reasons.append(
                    "no inspected primary resolution or counterexample "
                    "source was identified"
                )
            if reasons:
                correction = STATUS_CORRECTION_PREFIX + "; ".join(reasons)
                entry["resolution_status"] = "uncertain"
                entry["status_summary"] = (
                    "Conservatively installed as uncertain. Original agent "
                    f"assessment: {entry['status_summary']}"
                )
                entry["warnings"] = [*entry_warnings, correction]
                status_repairs.append(f"{problem_id}: {correction}")
        report = workspace / f"literature-{problem_id}.md"
        if not report.exists():
            try:
                report.write_text(
                    render_literature_markdown(entry),
                    encoding="utf-8",
                )
            except OSError as exc:
                raise common.CodexError(
                    f"Codex did not create literature report for {problem_id}, "
                    f"and the driver could not synthesize it: {exc}"
                ) from exc
            synthesized.append(problem_id)
        contents = common.validate_markdown(
            report,
            description=f"literature report for {problem_id}",
        )
        if problem_id not in contents:
            raise common.CodexError(
                f"literature report does not mention {problem_id}"
            )
        by_id[problem_id] = entry
    missing = requested.difference(by_id)
    if missing:
        raise common.CodexError(
            "literature response omitted: " + ", ".join(sorted(missing))
        )
    driver_warnings = []
    if synthesized:
        driver_warnings.append(
            "Driver synthesized missing Markdown literature report(s) "
            "from the validated structured response: "
            + ", ".join(synthesized)
        )
    if status_repairs:
        driver_warnings.extend(status_repairs)
    if driver_warnings:
        result = {
            **result,
            "warnings": [*root_warnings, *driver_warnings],
        }
    return result, by_id


def _install_literature(
    problem: common.ProblemRef,
    *,
    workspace: Path,
    root_result: dict,
    entry: dict,
    input_digest: str,
    config_digest: str,
    codex_version: str,
    options: codex_cli.ModelOptions,
    web_search: str,
    recovered_from: Path | None = None,
) -> None:
    problem.directory.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(prefix=".literature-install-", dir=problem.directory)
    )
    try:
        source_report = workspace / f"literature-{problem.id}.md"
        correction = next(
            (
                warning
                for warning in entry["warnings"]
                if warning.startswith(STATUS_CORRECTION_PREFIX)
            ),
            None,
        )
        if correction is None:
            shutil.copyfile(
                source_report,
                staging / common.LITERATURE_MARKDOWN,
            )
        else:
            original_report = source_report.read_text(encoding="utf-8")
            (staging / common.LITERATURE_MARKDOWN).write_text(
                "> **Driver status correction:** "
                + correction
                + "\n\n"
                + original_report,
                encoding="utf-8",
            )
        common.write_json(
            staging / common.LITERATURE_RESULT,
            {
                **entry,
                "paper_title": problem.paper_title,
                "searched_at": common.utc_now(),
                "run_status": root_result["status"],
                "run_warnings": root_result["warnings"],
            },
        )
        manifest = {
            "schema_version": common.LITERATURE_MANIFEST_SCHEMA_VERSION,
            "generated_at": common.utc_now(),
            "input_digest": input_digest,
            "config_digest": config_digest,
            "analysis_digest": common.analysis_digest(
                problem.paper_directory
            ),
            "codex_version": codex_version,
            "requested_model": options.model,
            "requested_reasoning_effort": options.reasoning_effort,
            "requested_fast_mode": options.fast,
            "requested_web_search": web_search,
            "resolution_status": entry["resolution_status"],
            "confidence": entry["confidence"],
            "source_count": len(entry["sources"]),
        }
        if recovered_from is not None:
            manifest.update(
                {
                    "recovered_from_workspace": recovered_from.name,
                    "original_workspace_preserved": True,
                }
            )
        common.write_json(
            staging / common.LITERATURE_MANIFEST,
            manifest,
        )
        shutil.copyfile(
            workspace / "events.jsonl",
            staging / common.LITERATURE_RUN_FILES[0],
        )
        shutil.copyfile(
            workspace / "run.log",
            staging / common.LITERATURE_RUN_FILES[1],
        )
        for name in (
            common.LITERATURE_MARKDOWN,
            common.LITERATURE_RESULT,
            common.LITERATURE_MANIFEST,
            *common.LITERATURE_RUN_FILES,
        ):
            os.replace(staging / name, problem.directory / name)
    except OSError as exc:
        raise common.CodexError(
            f"could not install literature search for {problem.id}; staging "
            f"preserved at {staging}: {exc}"
        ) from exc
    shutil.rmtree(staging)


def _write_work_record(
    workspace: Path,
    problems: Sequence[common.ProblemRef],
    *,
    input_digests: dict[str, str],
    config_digest: str,
    codex_version: str,
    options: codex_cli.ModelOptions,
    web_search: str,
) -> None:
    common.write_json(
        workspace / WORK_RECORD_FILE,
        {
            "schema_version": 1,
            "problem_ids": [problem.id for problem in problems],
            "input_digests": input_digests,
            "config_digest": config_digest,
            "codex_version": codex_version,
            "requested_model": options.model,
            "requested_reasoning_effort": options.reasoning_effort,
            "requested_fast_mode": options.fast,
            "requested_web_search": web_search,
        },
    )


def recover_paper_literature(
    problems: Sequence[common.ProblemRef],
    *,
    codex: str,
    codex_version: str,
    input_digests: dict[str, str],
    config_digest: str,
    options: codex_cli.ModelOptions,
    web_search: str,
) -> list[LiteratureOutcome] | None:
    """Install a matching completed workspace without another model turn."""
    runs_root = common.paper_runs_directory(problems[0].paper_directory)
    candidates = sorted(
        runs_root.glob(".literature-run-*"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for workspace in candidates:
        record = common.load_json(workspace / WORK_RECORD_FILE)
        if record is None or record.get("schema_version") != 1:
            continue
        if not (
            record.get("problem_ids")
            == [problem.id for problem in problems]
            and record.get("input_digests") == input_digests
            and record.get("config_digest") == config_digest
        ):
            continue
        codex_cli.normalize_workspace_access(workspace, codex)
        required = [
            workspace / "agent-result.json",
            workspace / "events.jsonl",
            workspace / "run.log",
            *(
                workspace / f"literature-{problem.id}.md"
                for problem in problems
            ),
        ]
        if not all(path.is_file() for path in required):
            continue
        root_result, entries = validate_literature_result(
            workspace / "agent-result.json",
            workspace,
            problems,
        )
        outcomes = []
        for problem in problems:
            entry = entries[problem.id]
            _install_literature(
                problem,
                workspace=workspace,
                root_result=root_result,
                entry=entry,
                input_digest=input_digests[problem.id],
                config_digest=config_digest,
                codex_version=record.get("codex_version", codex_version),
                options=options,
                web_search=record.get("requested_web_search", web_search),
                recovered_from=workspace,
            )
            outcomes.append(
                LiteratureOutcome(
                    problem,
                    "recovered",
                    entry["resolution_status"],
                    len(entry["sources"]),
                    f"recovered {entry['resolution_status']}; "
                    f"{len(entry['sources'])} source(s); preserved "
                    f"{workspace.name}",
                )
            )
        return outcomes
    return None


def search_paper_literature(
    problems: Sequence[common.ProblemRef],
    *,
    codex: str,
    codex_version: str,
    prompt_template: str,
    schema_path: Path,
    config_digest: str,
    options: codex_cli.ModelOptions,
    web_search: str = "live",
    allow_recovery: bool = True,
    launch_interval: float = codex_cli.CODEX_LAUNCH_INTERVAL_SECONDS,
) -> list[LiteratureOutcome]:
    """Search selected problems from one paper in one Codex run."""
    if not problems:
        return []
    paper = problems[0].paper_directory
    runs_root = common.paper_runs_directory(paper)
    runs_root.mkdir(parents=True, exist_ok=True)
    input_digests = {
        problem.id: common.literature_input_digest(problem)
        for problem in problems
    }
    if allow_recovery:
        recovered = recover_paper_literature(
            problems,
            codex=codex,
            codex_version=codex_version,
            input_digests=input_digests,
            config_digest=config_digest,
            options=options,
            web_search=web_search,
        )
        if recovered is not None:
            return recovered
    workspace = Path(
        tempfile.mkdtemp(prefix=".literature-run-", dir=runs_root)
    ).resolve()
    try:
        _write_work_record(
            workspace,
            problems,
            input_digests=input_digests,
            config_digest=config_digest,
            codex_version=codex_version,
            options=options,
            web_search=web_search,
        )
        context = common.stage_context(
            workspace,
            problems,
            include_paper=True,
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
            web_search=web_search,
            launch_interval=launch_interval,
        )
        root_result, entries = validate_literature_result(
            result_path,
            workspace,
            problems,
        )
        outcomes: list[LiteratureOutcome] = []
        for problem in problems:
            entry = entries[problem.id]
            _install_literature(
                problem,
                workspace=workspace,
                root_result=root_result,
                entry=entry,
                input_digest=input_digests[problem.id],
                config_digest=config_digest,
                codex_version=codex_version,
                options=options,
                web_search=web_search,
            )
            outcomes.append(
                LiteratureOutcome(
                    problem,
                    "searched",
                    entry["resolution_status"],
                    len(entry["sources"]),
                    f"{entry['resolution_status']}; "
                    f"{len(entry['sources'])} source(s)",
                )
            )
    except (common.CodexError, OSError) as exc:
        raise common.CodexError(
            common.preserved_workspace_message(exc, workspace)
        ) from exc
    warning = common.cleanup_workspace(
        workspace,
        installed_log=problems[0].directory / common.LITERATURE_RUN_FILES[1],
    )
    if warning:
        outcomes = [
            LiteratureOutcome(
                outcome.problem,
                outcome.status,
                outcome.resolution_status,
                outcome.source_count,
                outcome.message + "; temporary workspace preserved",
            )
            for outcome in outcomes
        ]
    return outcomes


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "search later literature for selected extracted open problems"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  Search current triage items classified attempt or maybe:
    python src/literature_review.py papers/edemaine --jobs 4

  Search one problem even without current triage:
    python src/literature_review.py papers/edemaine/arXiv-... \\
      --problem OP-002 --web-search live

  Search exact problems selected by their stored directories:
    python src/literature_review.py paper/OP-00{1,4}

  Search every extracted problem under one paper:
    python src/literature_review.py papers/edemaine/arXiv-... \\
      --all-problems

  Search only problems that already have solver attempts:
    python src/literature_review.py papers/edemaine --attempted
""",
    )
    parser.add_argument(
        "paths",
        nargs="+",
        type=Path,
        help=(
            "paper/parent directories, or PAPER/OP-ID paths selecting exact "
            "problems"
        ),
    )
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument(
        "--from-triage",
        metavar="CLASSES",
        help=(
            "search current triage entries in these comma-separated classes "
            f"(default: {DEFAULT_TRIAGE_CLASSES})"
        ),
    )
    selection.add_argument(
        "--problem",
        action="append",
        dest="problem_ids",
        metavar="OP-ID",
        help=(
            "search this problem ID; may be repeated and does not require "
            "current triage"
        ),
    )
    selection.add_argument(
        "--all-problems",
        action="store_true",
        help="search every extracted problem, regardless of triage",
    )
    selection.add_argument(
        "--attempted",
        action="store_true",
        help=(
            "search every problem with at least one installed attempt, "
            "regardless of triage"
        ),
    )
    parser.add_argument(
        "-j",
        "--jobs",
        type=codex_cli.positive_integer,
        default=1,
        help="maximum concurrent per-paper literature agents (default: 1)",
    )
    parser.add_argument(
        "-f",
        "--force",
        action="store_true",
        help="search even when the problem, prompt, and schema are unchanged",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report pending literature searches without starting Codex",
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
        help=f"literature prompt template (default: {DEFAULT_PROMPT_PATH})",
    )
    parser.add_argument(
        "--schema",
        type=Path,
        default=DEFAULT_SCHEMA_PATH,
        help=f"literature response schema (default: {DEFAULT_SCHEMA_PATH})",
    )
    codex_cli.add_model_arguments(parser)
    codex_cli.add_web_search_argument(parser, default="live")
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
            or args.attempted
        ):
            raise common.CodexError(
                "direct problem paths cannot be combined with "
                "--from-triage, --problem, --all-problems, or --attempted"
            )
        triage_classes = None
        if (
            not direct_exact
            and not args.problem_ids
            and not args.all_problems
            and not args.attempted
        ):
            triage_classes = common.parse_csv_values(
                args.from_triage or DEFAULT_TRIAGE_CLASSES,
                allowed=("attempt", "maybe", "skip"),
                label="--from-triage",
            )
        problems = common.discover_problem_refs(
            input_paths,
            problem_ids=set(args.problem_ids) if args.problem_ids else None,
        )
        problems = common.filter_exact_problems(problems, direct_exact)
        excluded: list[common.ProblemRef] = []
        exclusion_label = "without matching current triage"
        if args.attempted:
            selected = []
            exclusion_label = "without attempts"
            for problem in problems:
                if common.attempt_directories(problem):
                    selected.append(problem)
                else:
                    excluded.append(problem)
            problems = selected
        elif triage_classes is not None:
            selected: list[common.ProblemRef] = []
            for problem in problems:
                result = (
                    common.triage_result(problem)
                    if common.triage_is_current(problem)
                    else None
                )
                if result is not None and result.get("classification") in (
                    triage_classes
                ):
                    selected.append(problem)
                else:
                    excluded.append(problem)
            problems = selected
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

    current: list[LiteratureOutcome] = []
    stale: list[common.ProblemRef] = []
    for problem in problems:
        if not args.force and common.literature_is_current(
            problem,
            config_digest=config_digest,
        ):
            result = common.literature_result(problem)
            assert result is not None
            current.append(
                LiteratureOutcome(
                    problem,
                    "current",
                    result["resolution_status"],
                    len(result.get("sources", [])),
                    "literature search matches the problem and prompt",
                )
            )
        else:
            stale.append(problem)

    if args.dry_run:
        for problem in stale:
            print(
                f"Would search literature: {problem.paper_directory} "
                f"{problem.id}: {problem.title}"
            )
        print(
            f"Selected {len(problems)} problem(s): {len(stale)} stale; "
            f"{len(current)} current; {len(excluded)} {exclusion_label}."
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
            f"Searching literature for {len(stale)} stale problem(s) from "
            f"{len(grouped)} paper(s), with up to "
            f"{min(args.jobs, len(grouped))} Codex agent(s) at once "
            f"({args.web_search} web search)."
        )
        with ThreadPoolExecutor(
            max_workers=min(args.jobs, len(grouped))
        ) as executor:
            future_to_paper = {
                executor.submit(
                    search_paper_literature,
                    paper_problems,
                    codex=codex,
                    codex_version=codex_version,
                    prompt_template=prompt_template,
                    schema_path=schema_path,
                    config_digest=config_digest,
                    options=options,
                    web_search=args.web_search,
                    allow_recovery=not args.force,
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
                    label = (
                        "Recovered"
                        if outcome.status == "recovered"
                        else "Searched"
                    )
                    print(
                        f"{label}: {outcome.problem.paper_directory} "
                        f"{outcome.problem.id} ({outcome.message})"
                    )
    for outcome in current:
        print(
            f"Current: {outcome.problem.paper_directory} "
            f"{outcome.problem.id} ({outcome.resolution_status}; "
            f"{outcome.source_count} source(s))"
        )

    totals = {
        status: sum(
            outcome.resolution_status == status for outcome in outcomes
        )
        for status in RESOLUTION_STATUSES
    }
    print(
        f"Completed {sum(item.status == 'searched' for item in outcomes)} "
        f"literature search(es); "
        f"{sum(item.status == 'recovered' for item in outcomes)} recovered; "
        f"{len(current)} current; "
        f"{len(excluded)} {exclusion_label}; "
        f"{len(failures)} paper run(s) failed. Totals: "
        f"{totals['resolved']} resolved, "
        f"{totals['partially_resolved']} partially resolved, "
        f"{totals['no_resolution_found']} no resolution found, "
        f"{totals['uncertain']} uncertain."
    )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
