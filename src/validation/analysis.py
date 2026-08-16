"""Validate paper-analysis output."""

from __future__ import annotations

from pathlib import Path
import re
from typing import Mapping

from . import common


CONTENT_FILES = ("summary.md", "results.md", "open-problems.md")
OPEN_PROBLEM_ID_RE = re.compile(r"^OP-[0-9]{3,}$")


def _valid_authors(value: object) -> bool:
    return (
        isinstance(value, list)
        and bool(value)
        and all(isinstance(author, str) and bool(author.strip()) for author in value)
        and len(set(value)) == len(value)
    )


def validate(
    *,
    workspace: Path,
    expectations: Mapping[str, object],
) -> common.ValidationReport:
    reporter = common.Reporter()
    result = common.read_json_object(
        workspace / common.AGENT_RESULT_FILENAME,
        reporter,
        code="E_RESULT_JSON",
        description="analysis result",
    )
    markdown: dict[str, str] = {}
    for filename in CONTENT_FILES:
        contents = common.read_markdown(
            workspace / filename,
            reporter,
            code="E_MARKDOWN",
            description=filename,
        )
        if contents is not None:
            markdown[filename] = contents
    if result is None:
        return common.ValidationReport(issues=reporter.issues)
    common.validate_result_schema(result, expectations, reporter)
    if result.get("status") not in {"complete", "partial"}:
        reporter.error("E_STATUS", "status must be complete or partial", path="agent-result.json#/status")
    if not isinstance(result.get("paper_title"), str) or not result["paper_title"].strip():
        reporter.error("E_PAPER_TITLE", "paper_title must be nonempty", path="agent-result.json#/paper_title")
    if not _valid_authors(result.get("paper_authors")):
        reporter.error("E_PAPER_AUTHORS", "paper_authors must be a nonempty array of unique nonempty strings", path="agent-result.json#/paper_authors")
    if common.string_list(result.get("warnings")) is None:
        reporter.error("E_WARNINGS", "warnings must be an array of strings", path="agent-result.json#/warnings")
    problems = result.get("open_problems")
    problem_ids: set[str] = set()
    if not isinstance(problems, list):
        reporter.error("E_OPEN_PROBLEMS", "open_problems must be an array", path="agent-result.json#/open_problems")
    else:
        for index, problem in enumerate(problems):
            base = f"agent-result.json#/open_problems/{index}"
            if not isinstance(problem, dict):
                reporter.error("E_OPEN_PROBLEM", "entry must be an object", path=base)
                continue
            problem_id = problem.get("id")
            if not isinstance(problem_id, str) or not OPEN_PROBLEM_ID_RE.fullmatch(problem_id):
                reporter.error("E_PROBLEM_ID", f"invalid open-problem ID {problem_id!r}", path=f"{base}/id")
            elif problem_id in problem_ids:
                reporter.error("E_PROBLEM_ID", f"duplicate open-problem ID {problem_id}", path=f"{base}/id")
            else:
                problem_ids.add(problem_id)
            if not isinstance(problem.get("title"), str) or not problem["title"].strip():
                reporter.error("E_PROBLEM_TITLE", "title must be nonempty", path=f"{base}/title")
            if problem.get("explicitness") not in {"explicit", "inferred", "uncertain"}:
                reporter.error("E_EXPLICITNESS", "invalid explicitness", path=f"{base}/explicitness")
    open_problem_text = markdown.get("open-problems.md", "")
    for problem_id in sorted(problem_ids):
        if problem_id not in open_problem_text:
            reporter.error("E_PROBLEM_MARKDOWN", f"open-problems.md omits {problem_id}", path="open-problems.md")
    return common.ValidationReport(
        result=result,
        files=[workspace / filename for filename in CONTENT_FILES],
        issues=reporter.issues,
    )


if __name__ == "__main__":
    # Agent-only entry point for the staged workspace. Repository drivers
    # import validate() and pass authoritative expectations directly.
    raise SystemExit(
        common.run_agent_validation(
            validate,
            validation_directory=Path(__file__).resolve().parent,
        )
    )
