"""Validate independent reviews of paper-visualization runs."""

from __future__ import annotations

from pathlib import Path
from typing import Mapping

from . import common


CRITIQUE_FILENAME = "critique.md"


def validate(*, workspace: Path, expectations: Mapping[str, object]) -> common.ValidationReport:
    reporter = common.Reporter()
    result = common.read_json_object(
        workspace / common.AGENT_RESULT_FILENAME, reporter,
        code="E_RESULT_JSON", description="visualization review result",
    )
    critique = common.read_markdown(
        workspace / CRITIQUE_FILENAME, reporter,
        code="E_CRITIQUE", description="visualization critique",
    )
    files = [workspace / CRITIQUE_FILENAME] if critique else []
    if result is None:
        return common.ValidationReport(files=files, issues=reporter.issues)
    common.validate_result_schema(result, expectations, reporter)
    if not isinstance(result.get("summary"), str) or not result["summary"].strip():
        reporter.error("E_TEXT", "summary must be nonempty", path="agent-result.json#/summary")
    expected = common.expectation_string_list(expectations, "widget_ids", reporter)
    reviews = result.get("widget_reviews")
    reviews = reviews if isinstance(reviews, list) else []
    seen: set[str] = set()
    for index, review in enumerate(reviews):
        path = f"agent-result.json#/widget_reviews/{index}"
        if not isinstance(review, dict):
            continue
        identifier = review.get("id")
        if identifier not in expected:
            reporter.error("E_WIDGET_REVIEW", f"unknown widget id {identifier!r}", path=path)
        elif identifier in seen:
            reporter.error("E_WIDGET_REVIEW", f"duplicate review for {identifier}", path=path)
        else:
            seen.add(identifier)
        if not isinstance(review.get("summary"), str) or not review["summary"].strip():
            reporter.error("E_WIDGET_REVIEW", "each widget review needs a nonempty summary", path=path)
        if review.get("fidelity") in {"major_gaps", "incorrect"} and not review.get("blocking_gaps"):
            reporter.error("E_WIDGET_REVIEW", "major_gaps or incorrect fidelity must list blocking_gaps", path=path)
    for missing in sorted(set(expected).difference(seen)):
        reporter.error("E_WIDGET_REVIEW", f"missing review for widget {missing}", path="agent-result.json#/widget_reviews")
    annotations_review = result.get("annotations_review")
    if isinstance(annotations_review, dict):
        expects_annotations = bool(expectations.get("annotations_present"))
        if expects_annotations and annotations_review.get("accuracy") == "not_applicable":
            reporter.error("E_ANNOTATIONS_REVIEW", "annotations were generated and must be reviewed", path="agent-result.json#/annotations_review")
        if not expects_annotations and annotations_review.get("accuracy") != "not_applicable":
            reporter.error("E_ANNOTATIONS_REVIEW", "no annotations were generated in this run; accuracy must be not_applicable", path="agent-result.json#/annotations_review")
    return common.ValidationReport(result=result, files=files, issues=reporter.issues)


if __name__ == "__main__":
    raise SystemExit(common.run_agent_validation(validate, validation_directory=Path(__file__).resolve().parent))
