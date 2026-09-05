#!/usr/bin/env python3
"""Typed task planning and output discovery for the workbench."""

from __future__ import annotations

from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import uuid
from typing import Iterable

import analyze_papers
import codex_cli
import download_arxiv
import open_problem_common as common


ANCHOR_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:._-]{0,120}$")
ACTIONS = {
    "download",
    "metadata",
    "analyze",
    "triage",
    "literature",
    "solve",
    "review",
    "visualize",
    "write",
    "revise",
}
PROBLEM_RE = analyze_papers.OPEN_PROBLEM_ID_RE
ATTEMPT_RE = common.ATTEMPT_DIRECTORY_RE
DRAFT_RE = re.compile(r"^draft-([0-9]{3,})$")
MAX_PROMPT_LENGTH = 16_000
DRY_RUN_TIMEOUT_SECONDS = 30
MAX_DRY_RUN_OUTPUT = 100_000
MAX_DRY_RUN_WORKERS = 4
MIN_PRIORITY_LEVEL = -3
MAX_PRIORITY_LEVEL = 3


def task_cli_defaults() -> dict[str, dict[str, str]]:
    """Read workbench-visible defaults from each action's actual CLI parser."""
    import extract_paper_metadata
    import literature_review
    import review_solutions
    import solve_open_problems
    import triage_open_problems
    import visualize_paper
    import write_paper

    modules = {
        "metadata": extract_paper_metadata,
        "analyze": analyze_papers,
        "triage": triage_open_problems,
        "literature": literature_review,
        "solve": solve_open_problems,
        "review": review_solutions,
        "visualize": visualize_paper,
        "write": write_paper,
        "revise": write_paper,
    }
    defaults: dict[str, dict[str, str]] = {}
    for action, module in modules.items():
        parser = module.build_parser()
        destinations = {argument.dest for argument in parser._actions}
        values = {
            "model": parser.get_default("model"),
            "reasoningEffort": parser.get_default("reasoning_effort"),
        }
        if "web_search" in destinations:
            values["webSearch"] = parser.get_default("web_search")
        defaults[action] = {
            name: value for name, value in values.items() if isinstance(value, str)
        }
    return defaults


class PlanError(codex_cli.CodexError):
    pass


def command_display(argv: list[str]) -> str:
    if os.name == "nt":
        return subprocess.list2cmdline(argv)
    return shlex.join(argv)


def _preview_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _limit_preview_output(output: str) -> tuple[str, bool]:
    if len(output) <= MAX_DRY_RUN_OUTPUT:
        return output, False
    first = MAX_DRY_RUN_OUTPUT * 3 // 5
    last = MAX_DRY_RUN_OUTPUT - first
    return (
        output[:first]
        + "\n\n… dry-run output truncated by the workbench …\n\n"
        + output[-last:],
        True,
    )


def _dry_run_preview(unit: dict) -> dict:
    argv = [*unit["argv"], "--dry-run"]
    preview = {
        "command": command_display(argv),
        "status": "ok",
        "exitCode": None,
        "output": "",
        "truncated": False,
    }
    try:
        completed = subprocess.run(
            argv,
            cwd=unit["cwd"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=DRY_RUN_TIMEOUT_SECONDS,
            check=False,
            **codex_cli.windowless_popen_options(new_process_group=False),
        )
        preview["exitCode"] = completed.returncode
        if completed.returncode != 0:
            preview["status"] = "failed"
        stdout = _preview_text(completed.stdout).rstrip()
        stderr = _preview_text(completed.stderr).rstrip()
    except subprocess.TimeoutExpired as exc:
        preview["status"] = "timeout"
        stdout = _preview_text(exc.stdout).rstrip()
        stderr = _preview_text(exc.stderr).rstrip()
        stderr = "\n".join(
            value
            for value in (
                stderr,
                f"Dry run exceeded the {DRY_RUN_TIMEOUT_SECONDS}-second limit.",
            )
            if value
        )
    except OSError as exc:
        preview["status"] = "error"
        stdout = ""
        stderr = f"Could not start the dry run: {exc}"

    pieces = []
    if stdout:
        pieces.append(stdout)
    if stderr:
        pieces.append(f"Standard error:\n{stderr}")
    output = "\n\n".join(pieces)
    if not output:
        output = "The dry run completed without producing output."
    preview["output"], preview["truncated"] = _limit_preview_output(output)
    return preview


def populate_dry_run_previews(plan: dict) -> dict:
    """Run each planned command in preview mode and attach captured output."""
    units = plan.get("units", [])
    if not units:
        return plan
    with ThreadPoolExecutor(
        max_workers=min(MAX_DRY_RUN_WORKERS, len(units))
    ) as executor:
        futures = {
            executor.submit(_dry_run_preview, unit): index
            for index, unit in enumerate(units)
        }
        for future in as_completed(futures):
            units[futures[future]]["dryRun"] = future.result()
    return plan


def _under(path: Path, roots: Iterable[Path]) -> bool:
    resolved = path.resolve()
    for root in roots:
        try:
            resolved.relative_to(root.resolve())
            return True
        except ValueError:
            continue
    return False


def _problem_exists(path: Path) -> bool:
    manifest = common.load_json(path.parent / "analysis" / "manifest.json")
    if manifest is None:
        return False
    return any(
        isinstance(item, dict) and item.get("id") == path.name
        for item in manifest.get("open_problems", [])
    )


def infer_target(path: Path) -> str | None:
    if DRAFT_RE.fullmatch(path.name) and (path / "manifest.json").is_file():
        return "draft"
    if (
        ATTEMPT_RE.fullmatch(path.name)
        and PROBLEM_RE.fullmatch(path.parent.name)
        and (path / "solver-result.json").is_file()
    ):
        return "attempt"
    if PROBLEM_RE.fullmatch(path.name) and _problem_exists(path):
        return "problem"
    if analyze_papers.is_paper_directory(path):
        return "paper"
    return None


def normalize_targets(
    raw_targets: object,
    *,
    project_root: Path,
    allowed_roots: Iterable[Path],
) -> list[dict]:
    if not isinstance(raw_targets, list) or not raw_targets:
        raise PlanError("select at least one target")
    targets: list[dict] = []
    seen: set[Path] = set()
    for raw in raw_targets:
        if not isinstance(raw, dict) or not isinstance(raw.get("path"), str):
            raise PlanError("every target must contain a path")
        path = Path(raw["path"]).expanduser()
        if not path.is_absolute():
            path = project_root / path
        path = path.resolve()
        if path in seen:
            continue
        if not _under(path, allowed_roots):
            raise PlanError(f"target is outside the configured roots: {path}")
        kind = infer_target(path)
        requested_kind = raw.get("kind")
        if kind is None:
            raise PlanError(f"target is not a recognized workbench entity: {path}")
        if requested_kind and requested_kind != kind:
            raise PlanError(
                f"target type changed from {requested_kind} to {kind}: {path}"
            )
        seen.add(path)
        targets.append(
            {
                "kind": kind,
                "path": str(path),
                "label": str(raw.get("label") or path.name),
            }
        )
    return targets


def _text(options: dict, name: str) -> str | None:
    value = options.get(name)
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise PlanError(f"{name} must be text")
    value = value.strip()
    if not value:
        return None
    if len(value) > MAX_PROMPT_LENGTH:
        raise PlanError(f"{name} is too long (maximum {MAX_PROMPT_LENGTH} characters)")
    return value


def _positive_integer(options: dict, name: str, default: int) -> int:
    value = options.get(name, default)
    if isinstance(value, bool):
        raise PlanError(f"{name} must be a positive integer")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise PlanError(f"{name} must be a positive integer") from exc
    if number < 1:
        raise PlanError(f"{name} must be a positive integer")
    return number


def _priority_level(options: dict) -> int:
    value = options.get("priorityLevel", 0)
    if isinstance(value, bool):
        raise PlanError("priorityLevel must be an integer")
    try:
        level = int(value)
    except (TypeError, ValueError) as exc:
        raise PlanError("priorityLevel must be an integer") from exc
    if not MIN_PRIORITY_LEVEL <= level <= MAX_PRIORITY_LEVEL:
        raise PlanError(
            f"priorityLevel must be between {MIN_PRIORITY_LEVEL} and "
            f"{MAX_PRIORITY_LEVEL}"
        )
    return level


def _positive_number(options: dict, name: str, default: float) -> float:
    value = options.get(name, default)
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise PlanError(f"{name} must be positive") from exc
    if number <= 0:
        raise PlanError(f"{name} must be positive")
    return number


def _choice(options: dict, name: str, allowed: tuple[str, ...], default: str) -> str:
    value = options.get(name, default)
    if value not in allowed:
        raise PlanError(f"{name} must be one of {', '.join(allowed)}")
    return str(value)


def _common_arguments(
    argv: list[str],
    options: dict,
    *,
    prompt: bool = True,
    web_search: bool = False,
) -> None:
    if prompt and (value := _text(options, "prompt")) is not None:
        argv.extend(("--prompt", value))
    if value := _text(options, "model"):
        argv.extend(("--model", value))
    effort = options.get("reasoningEffort")
    if effort:
        if effort not in codex_cli.REASONING_EFFORTS:
            raise PlanError("invalid reasoning effort")
        argv.extend(("--reasoning-effort", effort))
    if options.get("fast") is True:
        argv.append("--fast")
    if web_search:
        mode = options.get("webSearch")
        if mode:
            if mode not in codex_cli.WEB_SEARCH_MODES:
                raise PlanError("invalid web-search mode")
            argv.extend(("--web-search", mode))


def _review_arguments(argv: list[str], options: dict) -> None:
    if value := _text(options, "reviewPrompt"):
        argv.extend(("--review-prompt", value))
    if value := _text(options, "reviewModel"):
        argv.extend(("--review-model", value))
    effort = options.get("reviewReasoningEffort")
    if effort:
        if effort not in codex_cli.REASONING_EFFORTS:
            raise PlanError("invalid review reasoning effort")
        argv.extend(("--review-reasoning-effort", effort))
    mode = options.get("reviewWebSearch")
    if mode:
        if mode not in codex_cli.WEB_SEARCH_MODES:
            raise PlanError("invalid review web-search mode")
        argv.extend(("--review-web-search", mode))


def _unit(
    *,
    label: str,
    argv: list[str],
    project_root: Path,
    targets: list[dict],
    additional_resources: Iterable[str] = (),
) -> dict:
    resources: set[str] = set(additional_resources)
    for target in targets:
        path = Path(target["path"]).resolve()
        if target["kind"] == "paper":
            resources.add(f"paper:{path}")
        elif target["kind"] == "problem":
            resources.add(f"problem:{path}")
        elif target["kind"] == "attempt":
            resources.add(f"problem:{path.parent}")
        elif target["kind"] == "draft":
            resources.add(f"manuscript:{path.parent}")
    return {
        "label": label,
        "argv": argv,
        "command": command_display(argv),
        "cwd": str(project_root),
        "targets": targets,
        "resources": sorted(resources),
    }


def _require(targets: list[dict], allowed: set[str], action: str) -> None:
    invalid = sorted({item["kind"] for item in targets} - allowed)
    if invalid:
        raise PlanError(
            f"{action} does not accept {', '.join(invalid)} targets"
        )


def _single_paper_problem_title(targets: list[dict]) -> str | None:
    """Return the paper title for a multi-problem, single-paper selection."""
    if len(targets) < 2 or any(
        target["kind"] != "problem" for target in targets
    ):
        return None
    papers = {Path(target["path"]).parent for target in targets}
    if len(papers) != 1:
        return None
    paper = next(iter(papers))
    analysis = common.load_json(paper / "analysis" / "manifest.json") or {}
    metadata = common.load_json(paper / "metadata.json") or {}
    for value in (analysis.get("paper_title"), metadata.get("title")):
        if isinstance(value, str) and value.strip():
            return value.strip()
    return paper.name


def build_plan(
    request: dict,
    *,
    project_root: Path,
    allowed_roots: Iterable[Path],
    manuscripts: Path,
    catalog_version: int,
    paper_roots: Iterable[Path] | None = None,
) -> dict:
    if not isinstance(request, dict):
        raise PlanError("invalid task request")
    action = request.get("action")
    if action not in ACTIONS:
        raise PlanError("unknown workbench action")
    options = request.get("options", {})
    if not isinstance(options, dict):
        raise PlanError("task options must be an object")
    allowed_roots = list(allowed_roots)
    configured_paper_roots = list(
        allowed_roots if paper_roots is None else paper_roots
    )
    targets = [] if action == "download" else normalize_targets(
        request.get("targets"),
        project_root=project_root,
        allowed_roots=allowed_roots,
    )
    python = sys.executable
    script = project_root / "src"
    units: list[dict] = []
    warnings: list[str] = []
    priority_level = _priority_level(options)

    if action == "download":
        raw_papers = options.get("papers")
        if isinstance(raw_papers, str):
            papers = [line.strip() for line in raw_papers.splitlines() if line.strip()]
        elif isinstance(raw_papers, list):
            papers = [
                value.strip()
                for value in raw_papers
                if isinstance(value, str) and value.strip()
            ]
        else:
            papers = []
        if not papers:
            raise PlanError("enter at least one arXiv ID or URL")
        if len(papers) > 100:
            raise PlanError("a workbench download is limited to 100 papers")
        canonical_papers = []
        seen_papers = set()
        for paper in papers:
            try:
                arxiv_id = download_arxiv.parse_arxiv_id(paper)
            except ValueError as exc:
                raise PlanError(f"invalid arXiv paper {paper!r}: {exc}") from exc
            if arxiv_id not in seen_papers:
                canonical_papers.append(arxiv_id)
                seen_papers.add(arxiv_id)
        papers = canonical_papers
        output_value = options.get("outputDirectory")
        if not isinstance(output_value, str) or not output_value.strip():
            if not configured_paper_roots:
                raise PlanError("no paper output directory is configured")
            output = configured_paper_roots[0].resolve()
        else:
            output = Path(output_value).expanduser()
            if not output.is_absolute():
                output = project_root / output
            output = output.resolve()
        if not _under(output, configured_paper_roots):
            raise PlanError(
                f"output directory is outside the configured paper roots: {output}"
            )
        targets = [
            {
                "kind": "paper",
                "path": str(
                    (output / download_arxiv.directory_name(paper)).resolve()
                ),
                "label": f"arXiv:{paper}",
            }
            for paper in papers
        ]
        argv = [
            python,
            "-u",
            str(script / "download_arxiv.py"),
            *papers,
            "--output-dir",
            str(output),
        ]
        paper_label = f"{len(papers)} paper{'s' if len(papers) != 1 else ''}"
        label = f"Download {paper_label} from arXiv"
        paper_resources = {
            f"paper:{output / download_arxiv.directory_name(candidate)}"
            for paper in papers
            for candidate in download_arxiv.version_candidates(paper)
        }
        if options.get("force") is True:
            argv.append("--force")
            warnings.append(
                "Forced downloads replace matching arXiv PDF, source, and metadata files."
            )
        units.append(
            _unit(
                label=label,
                argv=argv,
                project_root=project_root,
                # Keep fallback-version resources intact. Plan-level targets
                # provide navigation once the downloaded papers enter the catalog.
                targets=[],
                additional_resources=paper_resources,
            )
        )

    elif action == "metadata":
        _require(targets, {"paper"}, action)
        for target in targets:
            argv = [
                python,
                "-u",
                str(script / "extract_paper_metadata.py"),
                target["path"],
            ]
            _common_arguments(argv, options)
            if options.get("force") is True:
                argv.append("--force")
                warnings.append(
                    "Forced extraction replaces title, authors, and extracted dates."
                )
            units.append(
                _unit(
                    label=f"Extract metadata for {target['label']}",
                    argv=argv,
                    project_root=project_root,
                    targets=[target],
                )
            )

    elif action == "analyze":
        _require(targets, {"paper"}, action)
        for target in targets:
            path = Path(target["path"])
            argv = [python, "-u", str(script / "analyze_papers.py"), str(path)]
            if options.get("force") is True:
                argv.append("--force")
                warnings.append("A forced analysis replaces the installed analysis.")
            if options.get("recoverComplete") is True:
                argv.append("--recover-complete")
            _common_arguments(argv, options)
            units.append(
                _unit(
                    label=f"Analyze {target['label']}",
                    argv=argv,
                    project_root=project_root,
                    targets=[target],
                )
            )

    elif action in {"triage", "literature"}:
        _require(targets, {"problem"}, action)
        grouped: dict[Path, list[dict]] = defaultdict(list)
        for target in targets:
            grouped[Path(target["path"]).parent].append(target)
        filename = (
            "triage_open_problems.py"
            if action == "triage"
            else "literature_review.py"
        )
        for paper, paper_targets in grouped.items():
            argv = [
                python,
                "-u",
                str(script / filename),
                *(item["path"] for item in paper_targets),
                "--jobs",
                "1",
            ]
            if options.get("force") is True:
                argv.append("--force")
                warnings.append(f"Forced {action} replaces current matching output.")
            _common_arguments(
                argv,
                options,
                web_search=action == "literature",
            )
            units.append(
                _unit(
                    label=f"{action.title()} {paper.name}",
                    argv=argv,
                    project_root=project_root,
                    targets=paper_targets,
                )
            )

    elif action == "solve":
        _require(targets, {"problem"}, action)
        rounds = _positive_integer(options, "maxRounds", 1)
        review = _choice(
            options, "review", ("promising", "all", "none"), "promising"
        )
        for target in targets:
            problem = Path(target["path"])
            argv = [
                python,
                "-u",
                str(script / "solve_open_problems.py"),
                str(problem),
                "--jobs",
                "1",
                "--max-rounds",
                str(rounds),
                "--review",
                review,
            ]
            if options.get("includeLiteratureResolved") is True:
                argv.append("--include-literature-resolved")
            timeout = _positive_number(options, "reviewTimeoutMinutes", 120)
            argv.extend(("--review-timeout-minutes", f"{timeout:g}"))
            _common_arguments(argv, options, web_search=True)
            _review_arguments(argv, options)
            units.append(
                _unit(
                    label=f"Solve {target['label']}",
                    argv=argv,
                    project_root=project_root,
                    targets=[target],
                )
            )

    elif action == "review":
        _require(targets, {"attempt"}, action)
        mode = _choice(options, "mode", ("promising", "all"), "promising")
        timeout = _positive_number(options, "timeoutMinutes", 120)
        for target in targets:
            attempt = Path(target["path"])
            argv = [
                python,
                "-u",
                str(script / "review_solutions.py"),
                str(attempt),
                "--jobs",
                "1",
                "--mode",
                mode,
                "--timeout-minutes",
                f"{timeout:g}",
            ]
            if options.get("force") is True:
                argv.append("--force")
                warnings.append("A forced review replaces the current review.")
            _common_arguments(argv, options, web_search=True)
            units.append(
                _unit(
                    label=f"Review {target['label']}",
                    argv=argv,
                    project_root=project_root,
                    targets=[target],
                )
            )

    elif action == "visualize":
        _require(targets, {"draft", "paper"}, action)
        anchors = options.get("anchors", [])
        if isinstance(anchors, str):
            anchors = anchors.replace(",", " ").split()
        if not isinstance(anchors, list) or not all(
            isinstance(anchor, str) and ANCHOR_RE.fullmatch(anchor) for anchor in anchors
        ):
            raise PlanError("anchors must be statement or proof identifiers")
        for target in targets:
            source = Path(target["path"])
            argv = [
                python,
                "-u",
                str(script / "visualize_paper.py"),
                str(source),
            ]
            for anchor in anchors:
                argv.extend(("--anchor", anchor))
            if options.get("skipReview") is True:
                argv.append("--skip-review")
            rounds = options.get("repairRounds")
            if rounds not in (None, ""):
                try:
                    rounds = int(rounds)
                except (TypeError, ValueError) as exc:
                    raise PlanError("repairRounds must be an integer") from exc
                if not 0 <= rounds <= 3:
                    raise PlanError("repairRounds must be between 0 and 3")
                argv.extend(("--repair-rounds", str(rounds)))
            _common_arguments(argv, options, web_search=True)
            _review_arguments(argv, options)
            units.append(
                _unit(
                    label=f"Visualize {target['label']}",
                    argv=argv,
                    project_root=project_root,
                    targets=[target],
                )
            )

    elif action == "write":
        _require(targets, {"paper", "problem", "attempt"}, action)
        argv = [
            python,
            "-u",
            str(script / "write_paper.py"),
            *(target["path"] for target in targets),
            "--max-rounds",
            str(_positive_integer(options, "maxRounds", 1)),
        ]
        if value := _text(options, "name"):
            argv.extend(("--name", value))
        if value := _text(options, "title"):
            argv.extend(("--title", value))
        authors = options.get("authors", [])
        if isinstance(authors, str):
            authors = [line.strip() for line in authors.splitlines() if line.strip()]
        if not isinstance(authors, list) or not all(
            isinstance(author, str) and author.strip() for author in authors
        ):
            raise PlanError("authors must be a list of nonempty names")
        for author in authors:
            argv.extend(("--author", author.strip()))
        _common_arguments(argv, options, web_search=True)
        _review_arguments(argv, options)
        units.append(
            _unit(
                label=f"Write one manuscript from {len(targets)} selection(s)",
                argv=argv,
                project_root=project_root,
                targets=targets,
                additional_resources=(f"manuscript:{manuscripts.resolve()}",),
            )
        )

    elif action == "revise":
        _require(targets, {"draft"}, action)
        if len(targets) != 1:
            raise PlanError("revise accepts exactly one draft")
        target = targets[0]
        draft = Path(target["path"])
        argv = [
            python,
            "-u",
            str(script / "write_paper.py"),
            "--revise",
            str(draft),
            "--max-rounds",
            str(_positive_integer(options, "maxRounds", 1)),
        ]
        if options.get("refreshResults") is True:
            argv.append("--refresh-results")
        if value := _text(options, "title"):
            argv.extend(("--title", value))
        authors = options.get("authors", [])
        if isinstance(authors, str):
            authors = [line.strip() for line in authors.splitlines() if line.strip()]
        for author in authors if isinstance(authors, list) else []:
            if not isinstance(author, str) or not author.strip():
                raise PlanError("authors must be nonempty names")
            argv.extend(("--author", author.strip()))
        _common_arguments(argv, options, web_search=True)
        _review_arguments(argv, options)
        units.append(
            _unit(
                label=f"Revise {target['label']}",
                argv=argv,
                project_root=project_root,
                targets=targets,
            )
        )

    prompts = {
        key: value
        for key in ("prompt", "reviewPrompt")
        if (value := _text(options, key)) is not None
    }
    single_paper_title = _single_paper_problem_title(targets)
    if single_paper_title:
        scope_title = f"{len(targets)} problems in {single_paper_title}"
    elif action == "download":
        scope_title = units[0]["label"].removeprefix("Download ")
    else:
        scope_title = ", ".join(target["label"] for target in targets[:3])
        if len(targets) > 3:
            scope_title += f" and {len(targets) - 3} more"
    return {
        "id": str(uuid.uuid4()),
        "action": action,
        "title": f"{action.title()}: {scope_title}",
        "singlePaperTitle": single_paper_title,
        "catalogVersion": catalog_version,
        "priorityLevel": priority_level,
        "targets": targets,
        "options": options,
        "prompts": prompts,
        "warnings": list(dict.fromkeys(warnings)),
        "units": units,
    }
