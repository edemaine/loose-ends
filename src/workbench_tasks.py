#!/usr/bin/env python3
"""Typed task planning and output discovery for the workbench."""

from __future__ import annotations

from collections import defaultdict
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
import open_problem_common as common


ACTIONS = {
    "analyze",
    "triage",
    "literature",
    "solve",
    "review",
    "write",
    "revise",
}
PROBLEM_RE = analyze_papers.OPEN_PROBLEM_ID_RE
ATTEMPT_RE = common.ATTEMPT_DIRECTORY_RE
DRAFT_RE = re.compile(r"^draft-([0-9]{3,})$")
MAX_PROMPT_LENGTH = 16_000


class PlanError(codex_cli.CodexError):
    pass


def command_display(argv: list[str]) -> str:
    if os.name == "nt":
        return subprocess.list2cmdline(argv)
    return shlex.join(argv)


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
    probe: dict,
) -> dict:
    resources: set[str] = set()
    for target in targets:
        path = Path(target["path"]).resolve()
        if target["kind"] == "paper":
            resources.add(f"paper:{path}")
        elif target["kind"] == "problem":
            resources.add(f"paper:{path.parent}")
        elif target["kind"] == "attempt":
            resources.add(f"paper:{path.parent.parent}")
        elif target["kind"] == "draft":
            resources.add(f"manuscript:{path.parent}")
    return {
        "label": label,
        "argv": argv,
        "command": command_display(argv),
        "cwd": str(project_root),
        "targets": targets,
        "resources": sorted(resources),
        "probe": probe,
    }


def _attempt_names(problem: Path) -> list[str]:
    if not problem.is_dir():
        return []
    return sorted(
        path.name
        for path in problem.iterdir()
        if path.is_dir() and ATTEMPT_RE.fullmatch(path.name)
    )


def _draft_paths(manuscripts: Path) -> list[str]:
    if not manuscripts.is_dir():
        return []
    return sorted(
        str(path.resolve())
        for path in manuscripts.glob("*/draft-*")
        if path.is_dir() and DRAFT_RE.fullmatch(path.name)
    )


def _file_marker(path: Path) -> list[int] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return [stat.st_mtime_ns, stat.st_size]


def _require(targets: list[dict], allowed: set[str], action: str) -> None:
    invalid = sorted({item["kind"] for item in targets} - allowed)
    if invalid:
        raise PlanError(
            f"{action} does not accept {', '.join(invalid)} targets"
        )


def build_plan(
    request: dict,
    *,
    project_root: Path,
    allowed_roots: Iterable[Path],
    manuscripts: Path,
    catalog_version: int,
) -> dict:
    if not isinstance(request, dict):
        raise PlanError("invalid task request")
    action = request.get("action")
    if action not in ACTIONS:
        raise PlanError("unknown workbench action")
    options = request.get("options", {})
    if not isinstance(options, dict):
        raise PlanError("task options must be an object")
    targets = normalize_targets(
        request.get("targets"),
        project_root=project_root,
        allowed_roots=allowed_roots,
    )
    python = sys.executable
    script = project_root / "src"
    units: list[dict] = []
    warnings: list[str] = []

    if action == "analyze":
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
                    probe={
                        "kind": "analysis",
                        "paper": str(path),
                        "before": _file_marker(path / "analysis" / "manifest.json"),
                    },
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
                    probe={
                        "kind": action,
                        "problems": [item["path"] for item in paper_targets],
                        "before": {
                            item["path"]: _file_marker(
                                Path(item["path"])
                                / (
                                    common.TRIAGE_RESULT
                                    if action == "triage"
                                    else common.LITERATURE_RESULT
                                )
                            )
                            for item in paper_targets
                        },
                    },
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
            before = _attempt_names(problem)
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
                    probe={
                        "kind": "solve",
                        "problem": str(problem),
                        "before": before,
                    },
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
                    probe={
                        "kind": "review",
                        "attempt": str(attempt),
                        "before": _file_marker(attempt / "review-result.json"),
                    },
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
                probe={
                    "kind": "write",
                    "manuscripts": str(manuscripts),
                    "before": _draft_paths(manuscripts),
                },
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
                probe={
                    "kind": "write",
                    "manuscripts": str(manuscripts),
                    "before": _draft_paths(manuscripts),
                },
            )
        )

    prompts = {
        key: value
        for key in ("prompt", "reviewPrompt")
        if (value := _text(options, key)) is not None
    }
    labels = ", ".join(target["label"] for target in targets[:3])
    if len(targets) > 3:
        labels += f" and {len(targets) - 3} more"
    return {
        "id": str(uuid.uuid4()),
        "action": action,
        "title": f"{action.title()}: {labels}",
        "catalogVersion": catalog_version,
        "targets": targets,
        "options": options,
        "prompts": prompts,
        "warnings": list(dict.fromkeys(warnings)),
        "units": units,
    }


def probe_outputs(probe: dict) -> list[str]:
    kind = probe.get("kind")
    outputs: list[Path] = []
    if kind == "analysis":
        path = Path(probe["paper"]) / "analysis"
        after = _file_marker(path / "manifest.json")
        if after is not None and after != probe.get("before"):
            outputs.append(path)
    elif kind == "triage":
        for value in probe.get("problems", []):
            path = Path(value)
            after = _file_marker(path / common.TRIAGE_RESULT)
            if after is not None and after != probe.get("before", {}).get(value):
                outputs.append(path)
    elif kind == "literature":
        for value in probe.get("problems", []):
            path = Path(value)
            after = _file_marker(path / common.LITERATURE_RESULT)
            if after is not None and after != probe.get("before", {}).get(value):
                outputs.append(path)
    elif kind == "solve":
        problem = Path(probe["problem"])
        before = set(probe.get("before", []))
        if problem.is_dir():
            outputs.extend(
                path
                for path in problem.iterdir()
                if path.is_dir()
                and ATTEMPT_RE.fullmatch(path.name)
                and path.name not in before
                and (path / "solver-result.json").is_file()
            )
    elif kind == "review":
        attempt = Path(probe["attempt"])
        after = _file_marker(attempt / "review-result.json")
        if after is not None and after != probe.get("before"):
            outputs.append(attempt)
    elif kind == "write":
        manuscripts = Path(probe["manuscripts"])
        before = set(probe.get("before", []))
        outputs.extend(
            Path(value)
            for value in _draft_paths(manuscripts)
            if value not in before
        )
    return sorted({str(path.resolve()) for path in outputs})
