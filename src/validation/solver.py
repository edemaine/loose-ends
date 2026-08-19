"""Validate open-problem solver output."""

from __future__ import annotations

from pathlib import Path
import re
from typing import Mapping

from . import common


CLAIM_ID_RE = re.compile(r"^C-[0-9]{3,}$")
SOLUTION_STATUSES = {"none", "obstruction", "partial_result", "solution", "counterexample"}
CLAIM_TYPES = {"proof", "lemma", "counterexample", "computation", "reduction", "reformulation", "obstruction", "other"}
PRIOR_DISPOSITIONS = {"retained", "strengthened", "narrowed", "superseded", "refuted"}
ATTEMPT_NAME_RE = re.compile(r"^attempt-[0-9]+$")


def validate(*, workspace: Path, expectations: Mapping[str, object]) -> common.ValidationReport:
    reporter = common.Reporter()
    result = common.read_json_object(workspace / common.AGENT_RESULT_FILENAME, reporter, code="E_RESULT_JSON", description="solver result")
    attempt = common.read_markdown(workspace / "attempt.md", reporter, code="E_ATTEMPT_MARKDOWN", description="solver attempt")
    files = [workspace / "attempt.md"] if attempt is not None else []
    if result is None:
        return common.ValidationReport(files=files, issues=reporter.issues)
    common.validate_result_schema(result, expectations, reporter)
    claimed = result.get("claimed_result_type")
    if claimed not in SOLUTION_STATUSES:
        reporter.error("E_RESULT_TYPE", "invalid claimed_result_type", path="agent-result.json#/claimed_result_type")
    if not isinstance(result.get("summary"), str) or not result["summary"].strip():
        reporter.error("E_SUMMARY", "summary must be nonempty", path="agent-result.json#/summary")
    sources = result.get("external_sources")
    if not isinstance(sources, list):
        reporter.error("E_EXTERNAL_SOURCES", "external_sources must be an array", path="agent-result.json#/external_sources")
    else:
        for index, source in enumerate(sources):
            base = f"agent-result.json#/external_sources/{index}"
            if not isinstance(source, dict):
                reporter.error("E_EXTERNAL_SOURCE", "source must be an object", path=base)
                continue
            for field in ("title", "url", "used_for", "verification"):
                if not isinstance(source.get(field), str):
                    reporter.error("E_SOURCE_TEXT", f"{field} must be a string", path=f"{base}/{field}")
            if isinstance(source.get("url"), str) and not source["url"].startswith(("https://", "http://")):
                reporter.error("E_SOURCE_URL", "URL must be HTTP(S)", path=f"{base}/url")
    if common.string_list(result.get("warnings")) is None:
        reporter.error("E_WARNINGS", "warnings must be an array of strings", path="agent-result.json#/warnings")
    claims = result.get("checkable_claims")
    claim_ids: set[str] = set()
    if not isinstance(claims, list):
        reporter.error("E_CLAIMS", "checkable_claims must be an array", path="agent-result.json#/checkable_claims")
        claims = []
    for index, claim in enumerate(claims):
        base = f"agent-result.json#/checkable_claims/{index}"
        if not isinstance(claim, dict):
            reporter.error("E_CLAIM", "claim must be an object", path=base)
            continue
        claim_id = claim.get("id")
        if not isinstance(claim_id, str) or not CLAIM_ID_RE.fullmatch(claim_id) or claim_id in claim_ids:
            reporter.error("E_CLAIM_ID", f"invalid or duplicate claim ID {claim_id!r}; use C-001 syntax", path=f"{base}/id")
        else:
            claim_ids.add(claim_id)
        if claim.get("type") not in CLAIM_TYPES:
            reporter.error("E_CLAIM_TYPE", "invalid claim type", path=f"{base}/type")
        for field in ("statement", "support", "remaining_gap"):
            if not isinstance(claim.get(field), str):
                reporter.error("E_CLAIM_TEXT", f"{field} must be a string", path=f"{base}/{field}")
    if claimed == "none" and claims:
        reporter.error("E_CLAIM_CONSISTENCY", "none result must not contain claims", path="agent-result.json#/checkable_claims")
    if claimed in SOLUTION_STATUSES - {"none"} and not claims:
        reporter.error("E_CLAIM_CONSISTENCY", f"{claimed} result must contain a claim", path="agent-result.json#/checkable_claims")
    prior_claim_refs_value = expectations.get("prior_claim_refs")
    prior_claim_refs = (
        set(prior_claim_refs_value)
        if common.string_list(prior_claim_refs_value) is not None
        else None
    )
    dispositions = result.get("prior_claim_dispositions")
    seen_prior_claims: set[str] = set()
    if not isinstance(dispositions, list):
        reporter.error(
            "E_PRIOR_DISPOSITIONS",
            "prior_claim_dispositions must be an array",
            path="agent-result.json#/prior_claim_dispositions",
        )
    else:
        for index, disposition in enumerate(dispositions):
            base = f"agent-result.json#/prior_claim_dispositions/{index}"
            if not isinstance(disposition, dict):
                reporter.error("E_PRIOR_DISPOSITION", "disposition must be an object", path=base)
                continue
            source_attempt = disposition.get("source_attempt")
            source_claim_id = disposition.get("source_claim_id")
            prior_ref = f"{source_attempt}/{source_claim_id}"
            if not isinstance(source_attempt, str) or not ATTEMPT_NAME_RE.fullmatch(source_attempt):
                reporter.error("E_PRIOR_ATTEMPT", "invalid source_attempt", path=f"{base}/source_attempt")
            if not isinstance(source_claim_id, str) or not CLAIM_ID_RE.fullmatch(source_claim_id):
                reporter.error("E_PRIOR_CLAIM", "invalid source_claim_id", path=f"{base}/source_claim_id")
            elif prior_ref in seen_prior_claims:
                reporter.error("E_PRIOR_CLAIM", f"duplicate prior claim {prior_ref}", path=f"{base}/source_claim_id")
            else:
                seen_prior_claims.add(prior_ref)
                if prior_claim_refs is not None and prior_ref not in prior_claim_refs:
                    reporter.error("E_PRIOR_CLAIM", f"unknown prior claim {prior_ref}", path=f"{base}/source_claim_id")
            value = disposition.get("disposition")
            if value not in PRIOR_DISPOSITIONS:
                reporter.error("E_PRIOR_DISPOSITION", "invalid disposition", path=f"{base}/disposition")
            explanation = disposition.get("explanation")
            if not isinstance(explanation, str) or not explanation.strip():
                reporter.error("E_PRIOR_EXPLANATION", "explanation must be nonempty", path=f"{base}/explanation")
            current_claim_id = disposition.get("current_claim_id")
            if value == "refuted":
                if current_claim_id != "":
                    reporter.error("E_PRIOR_CURRENT_CLAIM", "refuted claims require an empty current_claim_id", path=f"{base}/current_claim_id")
            elif current_claim_id not in claim_ids:
                reporter.error("E_PRIOR_CURRENT_CLAIM", "active dispositions must reference a current claim", path=f"{base}/current_claim_id")
    if attempt is not None:
        for claim_id in sorted(claim_ids):
            if claim_id not in attempt:
                reporter.error("E_CLAIM_MARKDOWN", f"attempt.md omits {claim_id}", path="attempt.md")
    artifacts = result.get("artifacts")
    seen: set[str] = set()
    if not isinstance(artifacts, list):
        reporter.error("E_ARTIFACTS", "artifacts must be an array", path="agent-result.json#/artifacts")
    else:
        for index, value in enumerate(artifacts):
            relative = common.safe_relative_path(value)
            item_path = f"agent-result.json#/artifacts/{index}"
            if relative is None or not relative.parts or relative.parts[0] != "artifacts" or len(relative.parts) < 2:
                reporter.error("E_ARTIFACT_PATH", f"artifact must be a canonical artifacts/... path, not {value!r}", path=item_path)
                continue
            normalized = relative.as_posix()
            if normalized in seen:
                reporter.error("E_ARTIFACT_PATH", f"duplicate artifact {normalized}", path=item_path)
                continue
            seen.add(normalized)
            artifact_path = workspace.joinpath(*relative.parts)
            if not artifact_path.is_file():
                reporter.error("E_ARTIFACT_MISSING", f"listed artifact does not exist: {normalized}", path=item_path)
            else:
                files.append(artifact_path)
    return common.ValidationReport(result=result, files=files, issues=reporter.issues)


if __name__ == "__main__":
    # Agent-only entry point for the staged workspace. Repository drivers
    # import validate() and pass authoritative expectations directly.
    raise SystemExit(common.run_agent_validation(validate, validation_directory=Path(__file__).resolve().parent))
