"""Validate open-problem triage output."""

from __future__ import annotations

from pathlib import Path
from typing import Mapping

from . import common


TRIAGE_CLASSES = {"attempt", "maybe", "skip"}
APPROACH_MODES = {
    "proof", "counterexample", "computation", "reformulation",
    "special_case", "verification", "literature_check", "other",
}


def validate(*, workspace: Path, expectations: Mapping[str, object]) -> common.ValidationReport:
    reporter = common.Reporter()
    expected_ids = common.expectation_string_list(expectations, "problem_ids", reporter)
    requested = set(expected_ids)
    if len(requested) != len(expected_ids):
        reporter.error("E_EXPECTATIONS", "problem_ids contains duplicates", path="expectations.json#/problem_ids", repairable=False)
    result = common.read_json_object(
        workspace / common.AGENT_RESULT_FILENAME, reporter,
        code="E_RESULT_JSON", description="triage result",
    )
    files: list[Path] = []
    if result is None:
        return common.ValidationReport(issues=reporter.issues)
    common.validate_result_schema(result, expectations, reporter)
    if result.get("status") not in {"complete", "partial"}:
        reporter.error("E_STATUS", "status must be complete or partial", path="agent-result.json#/status")
    if common.string_list(result.get("warnings")) is None:
        reporter.error("E_WARNINGS", "warnings must be an array of strings", path="agent-result.json#/warnings")
    entries = result.get("triages")
    by_id: dict[str, dict] = {}
    if not isinstance(entries, list):
        reporter.error("E_TRIAGES", "triages must be an array", path="agent-result.json#/triages")
    else:
        for index, entry in enumerate(entries):
            base = f"agent-result.json#/triages/{index}"
            if not isinstance(entry, dict):
                reporter.error("E_TRIAGE", "entry must be an object", path=base)
                continue
            problem_id = entry.get("problem_id")
            if problem_id not in requested:
                reporter.error("E_PROBLEM_ID", f"unrequested problem {problem_id!r}", path=f"{base}/problem_id")
                continue
            if problem_id in by_id:
                reporter.error("E_PROBLEM_ID", f"duplicate problem {problem_id}", path=f"{base}/problem_id")
                continue
            by_id[problem_id] = entry
            if entry.get("classification") not in TRIAGE_CLASSES:
                reporter.error("E_CLASSIFICATION", f"invalid classification for {problem_id}", path=f"{base}/classification")
            if not isinstance(entry.get("rationale"), str) or not entry["rationale"].strip():
                reporter.error("E_RATIONALE", f"missing rationale for {problem_id}", path=f"{base}/rationale")
            for field in ("promising_features", "obstacles"):
                if common.string_list(entry.get(field)) is None:
                    reporter.error("E_STRING_LIST", f"{field} must be an array of strings", path=f"{base}/{field}")
            suggestions = entry.get("suggested_approaches")
            seen_suggestions: set[str] = set()
            if not isinstance(suggestions, list):
                reporter.error("E_APPROACHES", "suggested_approaches must be an array", path=f"{base}/suggested_approaches")
            else:
                for suggestion_index, suggestion in enumerate(suggestions):
                    suggestion_base = f"{base}/suggested_approaches/{suggestion_index}"
                    if not isinstance(suggestion, dict):
                        reporter.error("E_APPROACH", "suggested approach must be an object", path=suggestion_base)
                        continue
                    suggestion_id = suggestion.get("id")
                    if not isinstance(suggestion_id, str) or not suggestion_id.strip() or suggestion_id in seen_suggestions:
                        reporter.error("E_APPROACH_ID", f"invalid or duplicate approach ID {suggestion_id!r}", path=f"{suggestion_base}/id")
                    else:
                        seen_suggestions.add(suggestion_id)
                    if suggestion.get("mode") not in APPROACH_MODES:
                        reporter.error("E_APPROACH_MODE", f"invalid approach mode {suggestion.get('mode')!r}", path=f"{suggestion_base}/mode")
                    for field in ("suggestion", "why_promising", "abandon_if"):
                        if not isinstance(suggestion.get(field), str) or not suggestion[field].strip():
                            reporter.error("E_APPROACH_TEXT", f"{field} must be nonempty", path=f"{suggestion_base}/{field}")
            report_path = workspace / f"triage-{problem_id}.md"
            contents = common.read_markdown(report_path, reporter, code="E_TRIAGE_MARKDOWN", description=f"triage report for {problem_id}")
            if contents is not None:
                files.append(report_path)
                if problem_id not in contents:
                    reporter.error("E_TRIAGE_MARKDOWN", f"report omits {problem_id}", path=report_path.name)
    for problem_id in sorted(requested.difference(by_id)):
        reporter.error("E_MISSING_PROBLEM", f"triage result omits {problem_id}", path="agent-result.json#/triages")
    return common.ValidationReport(result=result, files=files, issues=reporter.issues)


if __name__ == "__main__":
    # Agent-only entry point for the staged workspace. Repository drivers
    # import validate() and pass authoritative expectations directly.
    raise SystemExit(common.run_agent_validation(validate, validation_directory=Path(__file__).resolve().parent))
