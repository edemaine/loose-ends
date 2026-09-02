#!/usr/bin/env python3
"""Generate and independently audit interactive result visualizations."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
import tempfile

import analyze_papers
import codex_cli
import open_problem_common as common
import review_solutions
from validation import visualization as visualization_validation
from validation import visualization_review as review_validation
import visualizations


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROMPT_PATH = PROJECT_ROOT / "prompts" / "visualize-result.md"
DEFAULT_SCHEMA_PATH = PROJECT_ROOT / "schemas" / "visualization-result.schema.json"
DEFAULT_REVIEW_PROMPT_PATH = PROJECT_ROOT / "prompts" / "review-visualization.md"
DEFAULT_REVIEW_SCHEMA_PATH = PROJECT_ROOT / "schemas" / "visualization-review.schema.json"


@dataclass(frozen=True)
class VisualizationOutcome:
    attempt: Path
    directory: Path
    fidelity: str
    exposition_quality: str
    interaction_quality: str


def attempt_from_path(value: Path) -> review_solutions.AttemptRef:
    directory = value.expanduser().resolve()
    if (
        not directory.is_dir()
        or common.ATTEMPT_DIRECTORY_RE.fullmatch(directory.name) is None
        or analyze_papers.OPEN_PROBLEM_ID_RE.fullmatch(directory.parent.name) is None
    ):
        raise common.CodexError(
            f"visualization input must name a PAPER/OP-NNN/attempt-NNN directory: {value}"
        )
    paper = directory.parent.parent
    problems = common.discover_problem_refs(
        [paper], problem_ids={directory.parent.name}
    )
    exact = [
        problem for problem in problems
        if problem.paper_directory == paper and problem.id == directory.parent.name
    ]
    if len(exact) != 1:
        raise common.CodexError(f"could not identify the open problem for {directory}")
    result = common.read_json(
        directory / "solver-result.json",
        description=f"solver result for {directory}",
    )
    return review_solutions.AttemptRef(exact[0], directory, result)


def _claim_ids(attempt: review_solutions.AttemptRef) -> list[str]:
    claims = attempt.solver_result.get("checkable_claims", [])
    return [
        claim["id"]
        for claim in claims
        if isinstance(claim, dict) and isinstance(claim.get("id"), str)
    ]


def _inherit_review_options(
    primary: codex_cli.ModelOptions,
    review: codex_cli.ModelOptions,
) -> codex_cli.ModelOptions:
    return codex_cli.ModelOptions(
        review.model or primary.model,
        review.reasoning_effort or primary.reasoning_effort,
        review.fast or primary.fast,
    )


def _render_prompt(
    template: str,
    attempt: review_solutions.AttemptRef,
) -> str:
    relative = f"inputs/history/{attempt.problem.id}/{attempt.name}"
    return (
        template.rstrip()
        + "\n\n# Selected source\n\n"
        + f"The selected attempt is staged at `{relative}/`. Its exact claim "
        + "IDs are: "
        + (", ".join(_claim_ids(attempt)) or "none")
        + ".\n"
    )


def _stage_context(workspace: Path, attempt: review_solutions.AttemptRef) -> None:
    common.stage_context(
        workspace,
        [attempt.problem],
        include_paper=True,
        include_history=True,
        include_triage=True,
        include_literature=True,
    )


def _review_generated(
    attempt: review_solutions.AttemptRef,
    generated_workspace: Path,
    generated_result: dict,
    *,
    codex: str,
    prompt: str,
    schema_path: Path,
    options: codex_cli.ModelOptions,
    web_search: str,
) -> tuple[Path, dict]:
    workspace = Path(
        tempfile.mkdtemp(
            prefix=".visualization-review-run-",
            dir=attempt.problem.directory,
        )
    ).resolve()
    try:
        _stage_context(workspace, attempt)
        shutil.copytree(
            generated_workspace / "visualization",
            workspace / "inputs" / "visualization",
        )
        shutil.copyfile(
            generated_workspace / "agent-result.json",
            workspace / "inputs" / "visualization-result.json",
        )
        codex_cli.grant_sandbox_read_access(workspace / "inputs")
        claim_refs = generated_result.get("claim_refs", [])
        report = codex_cli.run_validated_codex(
            codex=codex,
            workspace=workspace,
            prompt=prompt,
            schema_path=schema_path,
            validator=codex_cli.OutputValidator(
                Path(review_validation.__file__).resolve(),
                review_validation.validate,
                {"claim_ids": claim_refs},
            ),
            options=options,
            web_search=web_search,
        )
        result = codex_cli.validated_result(report)
        return workspace, result
    except (common.CodexError, OSError, ValueError) as exc:
        raise common.CodexError(
            common.preserved_workspace_message(exc, workspace)
        ) from exc


def _install(
    attempt: review_solutions.AttemptRef,
    generated_workspace: Path,
    generated_result: dict,
    review_workspace: Path,
    review_result: dict,
    *,
    config_digest: str,
    review_config_digest: str,
    codex_version: str,
    options: codex_cli.ModelOptions,
    review_options: codex_cli.ModelOptions,
) -> Path:
    root = attempt.directory / visualizations.DIRECTORY_NAME
    root.mkdir(exist_ok=True)
    number = visualizations.next_number(attempt.directory)
    destination = root / f"visualization-{number:03d}"
    staging = Path(tempfile.mkdtemp(prefix=".visualization-install-", dir=root))
    try:
        for source in (generated_workspace / "visualization").rglob("*"):
            if not source.is_file():
                continue
            relative = source.relative_to(generated_workspace / "visualization")
            target = staging / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
        manifest = {
            **generated_result,
            "schema_version": 1,
            "entry_point": Path(generated_result["entry_point"]).relative_to(
                "visualization"
            ).as_posix(),
            "files": [
                Path(value).relative_to("visualization").as_posix()
                for value in generated_result["files"]
            ],
            "generated_at": common.utc_now(),
            "source_attempt": str(attempt.directory.resolve()),
            "source_attempt_digest": common.solver_attempt_digest(attempt.directory),
            "config_digest": config_digest,
            "review_config_digest": review_config_digest,
            "codex_version": codex_version,
            "requested_model": options.model,
            "requested_reasoning_effort": options.reasoning_effort,
            "requested_fast_mode": options.fast,
            "review_model": review_options.model,
            "review_reasoning_effort": review_options.reasoning_effort,
            "review_fast_mode": review_options.fast,
        }
        common.write_json(staging / visualizations.MANIFEST_NAME, manifest)
        common.write_json(staging / visualizations.REVIEW_NAME, review_result)
        shutil.copyfile(
            review_workspace / "fidelity-critique.md",
            staging / visualizations.CRITIQUE_NAME,
        )
        for source, name in (
            (generated_workspace / "events.jsonl", "events.jsonl"),
            (generated_workspace / "run.log", "run.log"),
            (review_workspace / "events.jsonl", "review-events.jsonl"),
            (review_workspace / "run.log", "review-run.log"),
        ):
            shutil.copyfile(source, staging / name)
        os.replace(staging, destination)
    except (OSError, ValueError) as exc:
        raise common.CodexError(
            f"could not install visualization; staging preserved at {staging}: {exc}"
        ) from exc
    common.report_artifacts(
        path for path in destination.rglob("*") if path.is_file()
    )
    return destination


def visualize_attempt(
    attempt: review_solutions.AttemptRef,
    *,
    codex: str,
    codex_version: str,
    prompt: str,
    schema_path: Path,
    config_digest: str,
    options: codex_cli.ModelOptions,
    web_search: str,
    review_prompt: str,
    review_schema_path: Path,
    review_config_digest: str,
    review_options: codex_cli.ModelOptions,
    review_web_search: str,
) -> VisualizationOutcome:
    workspace = Path(
        tempfile.mkdtemp(prefix=".visualize-run-", dir=attempt.problem.directory)
    ).resolve()
    review_workspace: Path | None = None
    try:
        _stage_context(workspace, attempt)
        report = codex_cli.run_validated_codex(
            codex=codex,
            workspace=workspace,
            prompt=_render_prompt(prompt, attempt),
            schema_path=schema_path,
            validator=codex_cli.OutputValidator(
                Path(visualization_validation.__file__).resolve(),
                visualization_validation.validate,
                {"claim_ids": _claim_ids(attempt)},
            ),
            options=options,
            web_search=web_search,
        )
        generated_result = codex_cli.validated_result(report)
        review_workspace, review_result = _review_generated(
            attempt,
            workspace,
            generated_result,
            codex=codex,
            prompt=review_prompt,
            schema_path=review_schema_path,
            options=review_options,
            web_search=review_web_search,
        )
        destination = _install(
            attempt,
            workspace,
            generated_result,
            review_workspace,
            review_result,
            config_digest=config_digest,
            review_config_digest=review_config_digest,
            codex_version=codex_version,
            options=options,
            review_options=review_options,
        )
    except (common.CodexError, OSError, ValueError) as exc:
        raise common.CodexError(
            common.preserved_workspace_message(exc, workspace)
        ) from exc
    common.cleanup_workspace(workspace, installed_log=destination / "run.log")
    if review_workspace is not None:
        common.cleanup_workspace(
            review_workspace, installed_log=destination / "review-run.log"
        )
    return VisualizationOutcome(
        attempt.directory,
        destination,
        review_result["fidelity"],
        review_result["exposition_quality"],
        review_result["interaction_quality"],
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="build and independently review interactive visualizations",
    )
    parser.add_argument("attempts", nargs="+", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--codex", default="codex")
    codex_cli.add_prompt_arguments(
        parser, default_template=DEFAULT_PROMPT_PATH, task="visualization designer"
    )
    parser.add_argument("--schema", type=Path, default=DEFAULT_SCHEMA_PATH)
    codex_cli.add_model_arguments(parser)
    codex_cli.add_web_search_argument(parser, default="disabled")
    codex_cli.add_prompt_arguments(
        parser,
        default_template=DEFAULT_REVIEW_PROMPT_PATH,
        task="visualization fidelity reviewer",
        prefix="review",
    )
    parser.add_argument(
        "--review-schema", type=Path, default=DEFAULT_REVIEW_SCHEMA_PATH
    )
    codex_cli.add_model_arguments(parser, prefix="review")
    codex_cli.add_web_search_argument(parser, default="disabled", prefix="review")
    return parser


def main(argv: list[str] | None = None) -> int:
    codex_cli.configure_utf8_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        attempts = [attempt_from_path(path) for path in args.attempts]
        prompt_path = args.prompt_template.expanduser().resolve()
        review_prompt_path = args.review_prompt_template.expanduser().resolve()
        schema_path = args.schema.expanduser().resolve()
        review_schema_path = args.review_schema.expanduser().resolve()
        prompt = codex_cli.with_user_prompt(
            prompt_path.read_text(encoding="utf-8"),
            args.prompt,
            task="visualization designer",
        )
        review_prompt = codex_cli.with_user_prompt(
            review_prompt_path.read_text(encoding="utf-8"),
            args.review_prompt,
            task="visualization fidelity reviewer",
            option_name="--review-prompt",
        )
        schema_text = schema_path.read_text(encoding="utf-8")
        review_schema_text = review_schema_path.read_text(encoding="utf-8")
        json.loads(schema_text)
        json.loads(review_schema_text)
        options = codex_cli.model_options_from_args(args)
        review_options = codex_cli.model_options_from_args(args, prefix="review")
        review_options = _inherit_review_options(options, review_options)
        config_digest = codex_cli.semantic_config_digest(
            prompt,
            schema_text,
            options,
            web_search=args.web_search,
            validation_source=Path(visualization_validation.__file__).resolve(),
        )
        review_config_digest = codex_cli.semantic_config_digest(
            review_prompt,
            review_schema_text,
            review_options,
            web_search=args.review_web_search or args.web_search,
            validation_source=Path(review_validation.__file__).resolve(),
        )
    except (common.CodexError, OSError, UnicodeError, json.JSONDecodeError) as exc:
        return codex_cli.report_error(parser, exc)

    if args.dry_run:
        for attempt in attempts:
            number = visualizations.next_number(attempt.directory)
            print(
                f"Would visualize {attempt.problem.paper_directory.name}/"
                f"{attempt.problem.id}/{attempt.name} as visualization-{number:03d} "
                "and run an independent fidelity review."
            )
        return 0

    try:
        codex = codex_cli.resolve_codex_executable(args.codex)
        codex_version = codex_cli.read_codex_version(codex)
        for attempt in attempts:
            print(
                f"Visualizing {attempt.problem.paper_directory.name}/"
                f"{attempt.problem.id}/{attempt.name}..."
            )
            outcome = visualize_attempt(
                attempt,
                codex=codex,
                codex_version=codex_version,
                prompt=prompt,
                schema_path=schema_path,
                config_digest=config_digest,
                options=options,
                web_search=args.web_search,
                review_prompt=review_prompt,
                review_schema_path=review_schema_path,
                review_config_digest=review_config_digest,
                review_options=review_options,
                review_web_search=args.review_web_search or args.web_search,
            )
            print(
                f"Installed {outcome.directory}: fidelity {outcome.fidelity}; "
                f"exposition {outcome.exposition_quality}; "
                f"interaction {outcome.interaction_quality}."
            )
    except common.CodexError as exc:
        return codex_cli.report_error(parser, exc)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
