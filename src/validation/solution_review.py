"""Validate an independent review of one solver attempt."""

from __future__ import annotations

from pathlib import Path
from typing import Mapping

from . import common


CORRECTNESS = {"not_applicable", "incorrect", "major_gaps", "minor_gaps", "plausible", "well_supported"}
COVERAGE = {"none", "auxiliary", "special_case", "partial", "near_complete", "complete_under_stated_interpretation", "complete"}
IMPORTANCE = {"none", "minor", "moderate", "major", "resolution"}
CONFIDENCE = {"low", "medium", "high"}
ASSESSMENTS = {"supported", "partially_supported", "unsupported", "incorrect"}


def validate(*, workspace: Path, expectations: Mapping[str, object]) -> common.ValidationReport:
    reporter = common.Reporter()
    expected_ids = common.expectation_string_list(expectations, "claim_ids", reporter)
    expected = set(expected_ids)
    claimed_result_type = expectations.get("claimed_result_type")
    if not isinstance(claimed_result_type, str):
        reporter.error("E_EXPECTATIONS", "claimed_result_type must be a string", path="expectations.json#/claimed_result_type", repairable=False)
    result = common.read_json_object(workspace / common.AGENT_RESULT_FILENAME, reporter, code="E_RESULT_JSON", description="solution review result")
    critique = common.read_markdown(workspace / "critique.md", reporter, code="E_CRITIQUE", description="review critique")
    files = [workspace / "critique.md"] if critique is not None else []
    if result is None:
        return common.ValidationReport(files=files, issues=reporter.issues)
    common.validate_result_schema(result, expectations, reporter)
    for field, allowed, code in (
        ("correctness", CORRECTNESS, "E_CORRECTNESS"),
        ("reviewed_coverage", COVERAGE, "E_COVERAGE"),
        ("importance", IMPORTANCE, "E_IMPORTANCE"),
        ("verification_confidence", CONFIDENCE, "E_CONFIDENCE"),
    ):
        if result.get(field) not in allowed:
            reporter.error(code, f"invalid {field}", path=f"agent-result.json#/{field}")
    if not isinstance(result.get("summary"), str) or not result["summary"].strip():
        reporter.error("E_SUMMARY", "summary must be nonempty", path="agent-result.json#/summary")
    for field in ("blocking_gaps", "recommended_next_steps", "warnings"):
        if common.string_list(result.get(field)) is None:
            reporter.error("E_STRING_LIST", f"{field} must be an array of strings", path=f"agent-result.json#/{field}")
    reviews = result.get("claim_reviews")
    reviewed: set[str] = set()
    if not isinstance(reviews, list):
        reporter.error("E_CLAIM_REVIEWS", "claim_reviews must be an array", path="agent-result.json#/claim_reviews")
    else:
        for index, review in enumerate(reviews):
            base = f"agent-result.json#/claim_reviews/{index}"
            if not isinstance(review, dict):
                reporter.error("E_CLAIM_REVIEW", "claim review must be an object", path=base)
                continue
            claim_id = review.get("claim_id")
            if claim_id not in expected:
                reporter.error("E_CLAIM_ID", f"unknown claim ID {claim_id!r}", path=f"{base}/claim_id")
                continue
            if claim_id in reviewed:
                reporter.error("E_CLAIM_ID", f"duplicate claim ID {claim_id}", path=f"{base}/claim_id")
                continue
            reviewed.add(claim_id)
            if review.get("assessment") not in ASSESSMENTS:
                reporter.error("E_ASSESSMENT", f"invalid assessment for {claim_id}", path=f"{base}/assessment")
            if not isinstance(review.get("explanation"), str) or not review["explanation"].strip():
                reporter.error("E_EXPLANATION", f"missing explanation for {claim_id}", path=f"{base}/explanation")
    for claim_id in sorted(expected.difference(reviewed)):
        reporter.error("E_MISSING_CLAIM", f"review omits {claim_id}", path="agent-result.json#/claim_reviews")
    if claimed_result_type == "none" and (
        result.get("correctness") != "not_applicable"
        or result.get("reviewed_coverage") != "none"
        or result.get("importance") != "none"
    ):
        reporter.error("E_NO_RESULT_AXES", "a no-result attempt requires not_applicable correctness and none coverage/importance", path="agent-result.json")
    if result.get("importance") == "resolution" and result.get("reviewed_coverage") not in {"complete", "complete_under_stated_interpretation"}:
        reporter.error("E_RESOLUTION_COVERAGE", "resolution importance requires complete coverage", path="agent-result.json#/importance")
    return common.ValidationReport(result=result, files=files, issues=reporter.issues)


if __name__ == "__main__":
    # Agent-only entry point for the staged workspace. Repository drivers
    # import validate() and pass authoritative expectations directly.
    raise SystemExit(common.run_agent_validation(validate, validation_directory=Path(__file__).resolve().parent))
