"""Validate independent fidelity reviews of visualization packages."""

from __future__ import annotations

from pathlib import Path
from typing import Mapping

from . import common


def validate(
    *, workspace: Path, expectations: Mapping[str, object]
) -> common.ValidationReport:
    reporter = common.Reporter()
    result = common.read_json_object(
        workspace / common.AGENT_RESULT_FILENAME,
        reporter,
        code="E_RESULT_JSON",
        description="visualization fidelity review",
    )
    critique = common.read_markdown(
        workspace / "fidelity-critique.md",
        reporter,
        code="E_CRITIQUE",
        description="visualization fidelity critique",
    )
    files = [workspace / "fidelity-critique.md"] if critique else []
    if result is None:
        return common.ValidationReport(files=files, issues=reporter.issues)

    common.validate_result_schema(result, expectations, reporter)
    if not isinstance(result.get("summary"), str) or not result["summary"].strip():
        reporter.error("E_SUMMARY", "summary must be nonempty", path="agent-result.json#/summary")
    for field in (
        "mathematical_findings",
        "exposition_findings",
        "interaction_findings",
        "blocking_gaps",
        "warnings",
    ):
        if common.string_list(result.get(field)) is None:
            reporter.error("E_STRING_LIST", f"{field} must be an array of strings", path=f"agent-result.json#/{field}")

    if (
        result.get("exposition_quality") in {"major_gaps", "not_self_contained"}
        and not result.get("blocking_gaps")
    ):
        reporter.error(
            "E_EXPOSITION_GAPS",
            "major exposition failures must be recorded in blocking_gaps",
            path="agent-result.json#/blocking_gaps",
        )

    expected_value = expectations.get("claim_ids", [])
    expected = set(expected_value) if isinstance(expected_value, list) else set()
    reviews = result.get("claim_reviews")
    seen: set[str] = set()
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
                reporter.error("E_CLAIM_REVIEW", f"unexpected claim review {claim_id!r}", path=f"{base}/claim_id")
            elif claim_id in seen:
                reporter.error("E_CLAIM_REVIEW", f"duplicate claim review {claim_id}", path=f"{base}/claim_id")
            else:
                seen.add(claim_id)
            if not isinstance(review.get("explanation"), str) or not review["explanation"].strip():
                reporter.error("E_CLAIM_REVIEW", "claim explanation must be nonempty", path=f"{base}/explanation")
        for claim_id in sorted(expected.difference(seen)):
            reporter.error("E_CLAIM_REVIEW", f"missing review for {claim_id}", path="agent-result.json#/claim_reviews")
    if critique is not None:
        for claim_id in sorted(expected):
            if claim_id not in critique:
                reporter.error("E_CRITIQUE_CLAIM", f"fidelity-critique.md omits {claim_id}", path="fidelity-critique.md")
    return common.ValidationReport(result=result, files=files, issues=reporter.issues)


if __name__ == "__main__":
    raise SystemExit(common.run_agent_validation(validate, validation_directory=Path(__file__).resolve().parent))
