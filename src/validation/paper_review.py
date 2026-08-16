"""Validate an independent review of a generated manuscript."""

from __future__ import annotations

from pathlib import Path
import re
from typing import Mapping

from . import common


FINDING_ID_RE = re.compile(r"^P-[0-9]{3,}$")
VERDICTS = {"invalid", "needs_research", "needs_major_revision", "needs_minor_revision", "ready_for_expert_review"}
REVISION_VERDICTS = {"needs_major_revision", "needs_minor_revision"}


def validate(*, workspace: Path, expectations: Mapping[str, object]) -> common.ValidationReport:
    reporter = common.Reporter()
    result_ids = set(common.expectation_string_list(expectations, "result_ids", reporter))
    all_claim_ids = set(common.expectation_string_list(expectations, "claim_ids", reporter))
    result = common.read_json_object(workspace / common.AGENT_RESULT_FILENAME, reporter, code="E_RESULT_JSON", description="paper review result")
    critique = common.read_markdown(workspace / "paper-critique.md", reporter, code="E_CRITIQUE", description="paper critique")
    files = [workspace / "paper-critique.md"] if critique is not None else []
    if result is None:
        return common.ValidationReport(files=files, issues=reporter.issues)
    common.validate_result_schema(result, expectations, reporter)
    verdict = result.get("verdict")
    if verdict not in VERDICTS:
        reporter.error("E_VERDICT", "invalid verdict", path="agent-result.json#/verdict")
    if not isinstance(result.get("summary"), str) or not result["summary"].strip():
        reporter.error("E_SUMMARY", "summary must be nonempty", path="agent-result.json#/summary")
    if common.string_list(result.get("warnings")) is None:
        reporter.error("E_WARNINGS", "warnings must be an array of strings", path="agent-result.json#/warnings")
    reviews = result.get("result_reviews")
    reviewed: set[str] = set()
    valid_reviews: list[dict] = []
    if not isinstance(reviews, list):
        reporter.error("E_RESULT_REVIEWS", "result_reviews must be an array", path="agent-result.json#/result_reviews")
    else:
        for index, review in enumerate(reviews):
            base = f"agent-result.json#/result_reviews/{index}"
            if not isinstance(review, dict):
                reporter.error("E_RESULT_REVIEW", "result review must be an object", path=base)
                continue
            result_id = review.get("result_id")
            if result_id not in result_ids or result_id in reviewed:
                reporter.error("E_RESULT_ID", f"unknown or duplicate result {result_id!r}", path=f"{base}/result_id")
                continue
            reviewed.add(result_id)
            valid_reviews.append(review)
            if review.get("assessment") not in {"supported", "partially_supported", "unsupported", "incorrect"}:
                reporter.error("E_ASSESSMENT", f"invalid assessment for {result_id}", path=f"{base}/assessment")
            if not isinstance(review.get("explanation"), str):
                reporter.error("E_EXPLANATION", "explanation must be a string", path=f"{base}/explanation")
    for result_id in sorted(result_ids.difference(reviewed)):
        reporter.error("E_MISSING_RESULT", f"paper review omits {result_id}", path="agent-result.json#/result_reviews")
    findings = result.get("findings")
    finding_ids: set[str] = set()
    valid_findings: list[dict] = []
    if not isinstance(findings, list):
        reporter.error("E_FINDINGS", "findings must be an array", path="agent-result.json#/findings")
    else:
        for index, finding in enumerate(findings):
            base = f"agent-result.json#/findings/{index}"
            if not isinstance(finding, dict):
                reporter.error("E_FINDING", "finding must be an object", path=base)
                continue
            finding_id = finding.get("id")
            if not isinstance(finding_id, str) or not FINDING_ID_RE.fullmatch(finding_id) or finding_id in finding_ids:
                reporter.error("E_FINDING_ID", f"invalid or duplicate finding ID {finding_id!r}", path=f"{base}/id")
            else:
                finding_ids.add(finding_id)
            if finding.get("severity") not in {"blocking", "major", "minor"}:
                reporter.error("E_SEVERITY", "invalid severity", path=f"{base}/severity")
            if finding.get("category") not in {"proof", "theorem_statement", "citation", "novelty", "self_containment", "exposition", "latex", "scope", "other"}:
                reporter.error("E_CATEGORY", "invalid category", path=f"{base}/category")
            referenced_results = common.string_list(finding.get("result_ids"))
            if referenced_results is None or any(value not in result_ids for value in referenced_results):
                reporter.error("E_FINDING_RESULTS", "invalid result_ids", path=f"{base}/result_ids")
            referenced_claims = common.string_list(finding.get("source_claim_ids"))
            if referenced_claims is None or any(value not in all_claim_ids for value in referenced_claims):
                reporter.error("E_FINDING_CLAIMS", "invalid source_claim_ids", path=f"{base}/source_claim_ids")
            for field in ("location", "explanation", "suggested_repair"):
                if not isinstance(finding.get(field), str):
                    reporter.error("E_FINDING_TEXT", f"{field} must be a string", path=f"{base}/{field}")
            if not isinstance(finding.get("requires_new_research"), bool):
                reporter.error("E_RESEARCH_FLAG", "requires_new_research must be boolean", path=f"{base}/requires_new_research")
            valid_findings.append(finding)
    if critique is not None:
        for finding_id in sorted(finding_ids):
            if finding_id not in critique:
                reporter.error("E_FINDING_MARKDOWN", f"paper-critique.md omits {finding_id}", path="paper-critique.md")
    action = result.get("recommended_action")
    if action not in {"revise", "new_research", "human_review"}:
        reporter.error("E_ACTION", "invalid recommended_action", path="agent-result.json#/recommended_action")
    if verdict == "ready_for_expert_review" and (
        action != "human_review"
        or any(finding.get("severity") in {"blocking", "major"} for finding in valid_findings)
        or any(review.get("assessment") in {"unsupported", "incorrect"} for review in valid_reviews)
    ):
        reporter.error("E_READY_CONSISTENCY", "ready verdict is incompatible with action or findings", path="agent-result.json")
    if verdict in REVISION_VERDICTS and (action != "revise" or not valid_findings):
        reporter.error("E_REVISION_CONSISTENCY", "revision verdict requires revise and at least one finding", path="agent-result.json")
    if verdict == "needs_research" and (action != "new_research" or not any(finding.get("requires_new_research") for finding in valid_findings)):
        reporter.error("E_RESEARCH_CONSISTENCY", "needs_research requires a new-research finding", path="agent-result.json")
    return common.ValidationReport(result=result, files=files, issues=reporter.issues)


if __name__ == "__main__":
    # Agent-only entry point for the staged workspace. Repository drivers
    # import validate() and pass authoritative expectations directly.
    raise SystemExit(common.run_agent_validation(validate, validation_directory=Path(__file__).resolve().parent))
