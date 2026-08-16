"""Validate open-problem literature-search output."""

from __future__ import annotations

from pathlib import Path
from typing import Mapping

from . import common


RESOLUTION_STATUSES = {"resolved", "partially_resolved", "no_resolution_found", "uncertain"}
CONFIDENCE_LEVELS = {"low", "medium", "high"}
SOURCE_ROLES = {"resolution", "partial_result", "special_case", "technique", "counterexample", "lower_bound", "survey", "terminology", "other"}
SOURCE_PRIORITIES = {"high", "medium", "low"}
SOURCE_TYPES = {"primary_source", "secondary_source", "search_result_only"}


def validate(*, workspace: Path, expectations: Mapping[str, object]) -> common.ValidationReport:
    reporter = common.Reporter()
    expected_ids = common.expectation_string_list(expectations, "problem_ids", reporter)
    requested = set(expected_ids)
    result = common.read_json_object(workspace / common.AGENT_RESULT_FILENAME, reporter, code="E_RESULT_JSON", description="literature result")
    files: list[Path] = []
    if result is None:
        return common.ValidationReport(issues=reporter.issues)
    common.validate_result_schema(result, expectations, reporter)
    if result.get("status") not in {"complete", "partial"}:
        reporter.error("E_STATUS", "status must be complete or partial", path="agent-result.json#/status")
    elif result["status"] == "partial":
        reporter.error("E_PARTIAL_RUN", "partial literature runs are preserved but cannot be installed", path="agent-result.json#/status", repairable=False)
    if common.string_list(result.get("warnings")) is None:
        reporter.error("E_WARNINGS", "warnings must be an array of strings", path="agent-result.json#/warnings")
    entries = result.get("literature")
    by_id: dict[str, dict] = {}
    if not isinstance(entries, list):
        reporter.error("E_LITERATURE", "literature must be an array", path="agent-result.json#/literature")
    else:
        for index, entry in enumerate(entries):
            base = f"agent-result.json#/literature/{index}"
            if not isinstance(entry, dict):
                reporter.error("E_LITERATURE_ENTRY", "entry must be an object", path=base)
                continue
            problem_id = entry.get("problem_id")
            if problem_id not in requested or problem_id in by_id:
                reporter.error("E_PROBLEM_ID", f"unrequested or duplicate problem {problem_id!r}", path=f"{base}/problem_id")
                continue
            by_id[problem_id] = entry
            resolution = entry.get("resolution_status")
            confidence = entry.get("confidence")
            if resolution not in RESOLUTION_STATUSES:
                reporter.error("E_RESOLUTION_STATUS", "invalid resolution_status", path=f"{base}/resolution_status")
            if confidence not in CONFIDENCE_LEVELS:
                reporter.error("E_CONFIDENCE", "invalid confidence", path=f"{base}/confidence")
            for field in ("status_summary", "exact_match_analysis", "residual_problem", "solver_briefing"):
                if not isinstance(entry.get(field), str):
                    reporter.error("E_TEXT", f"{field} must be a string", path=f"{base}/{field}")
            if isinstance(entry.get("status_summary"), str) and not entry["status_summary"].strip():
                reporter.error("E_STATUS_SUMMARY", "status_summary must be nonempty", path=f"{base}/status_summary")
            if resolution == "partially_resolved" and isinstance(entry.get("residual_problem"), str) and not entry["residual_problem"].strip():
                reporter.error("E_RESIDUAL_PROBLEM", "partial resolution requires a residual problem", path=f"{base}/residual_problem")
            sources = entry.get("sources")
            valid_sources: list[dict] = []
            source_ids: set[str] = set()
            if not isinstance(sources, list):
                reporter.error("E_SOURCES", "sources must be an array", path=f"{base}/sources")
            else:
                for source_index, source in enumerate(sources):
                    source_base = f"{base}/sources/{source_index}"
                    if not isinstance(source, dict):
                        reporter.error("E_SOURCE", "source must be an object", path=source_base)
                        continue
                    source_id = source.get("id")
                    if not isinstance(source_id, str) or not source_id.strip() or source_id in source_ids:
                        reporter.error("E_SOURCE_ID", f"invalid or duplicate source ID {source_id!r}", path=f"{source_base}/id")
                    else:
                        source_ids.add(source_id)
                    if source.get("role") not in SOURCE_ROLES:
                        reporter.error("E_SOURCE_ROLE", "invalid source role", path=f"{source_base}/role")
                    if source.get("priority") not in SOURCE_PRIORITIES:
                        reporter.error("E_SOURCE_PRIORITY", "invalid source priority", path=f"{source_base}/priority")
                    if source.get("source_type") not in SOURCE_TYPES:
                        reporter.error("E_SOURCE_TYPE", "invalid source_type", path=f"{source_base}/source_type")
                    for field in ("title", "publication_year", "url", "result_statement", "relevance", "limitations"):
                        if not isinstance(source.get(field), str):
                            reporter.error("E_SOURCE_TEXT", f"{field} must be a string", path=f"{source_base}/{field}")
                    if isinstance(source.get("title"), str) and not source["title"].strip():
                        reporter.error("E_SOURCE_TITLE", "source title must be nonempty", path=f"{source_base}/title")
                    if isinstance(source.get("url"), str) and not source["url"].startswith(("https://", "http://")):
                        reporter.error("E_SOURCE_URL", "source URL must be HTTP(S)", path=f"{source_base}/url")
                    if common.string_list(source.get("authors")) is None:
                        reporter.error("E_SOURCE_AUTHORS", "authors must be an array of strings", path=f"{source_base}/authors")
                    valid_sources.append(source)
            for field in ("search_queries", "warnings"):
                if common.string_list(entry.get(field)) is None:
                    reporter.error("E_STRING_LIST", f"{field} must be an array of strings", path=f"{base}/{field}")
            if resolution == "resolved":
                verified = any(source.get("role") in {"resolution", "counterexample"} and source.get("source_type") == "primary_source" for source in valid_sources)
                if confidence != "high" or not verified:
                    reporter.error("E_UNVERIFIED_RESOLUTION", "resolved requires high confidence and an inspected primary resolution or counterexample source", path=base)
            report_path = workspace / f"literature-{problem_id}.md"
            contents = common.read_markdown(report_path, reporter, code="E_LITERATURE_MARKDOWN", description=f"literature report for {problem_id}")
            if contents is not None:
                files.append(report_path)
                if problem_id not in contents:
                    reporter.error("E_LITERATURE_MARKDOWN", f"report omits {problem_id}", path=report_path.name)
    for problem_id in sorted(requested.difference(by_id)):
        reporter.error("E_MISSING_PROBLEM", f"literature result omits {problem_id}", path="agent-result.json#/literature")
    return common.ValidationReport(result=result, files=files, issues=reporter.issues)


if __name__ == "__main__":
    # Agent-only entry point for the staged workspace. Repository drivers
    # import validate() and pass authoritative expectations directly.
    raise SystemExit(common.run_agent_validation(validate, validation_directory=Path(__file__).resolve().parent))
