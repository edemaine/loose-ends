"""Validate a generated open-problem manuscript and its structured result."""

from __future__ import annotations

from pathlib import Path
from pathlib import PurePosixPath
import re
from typing import Mapping

from . import common


CLAIM_ID_RE = re.compile(r"^C-[0-9]{3,}$")
LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:._/-]*$")
PAPER_STATUSES = {"draft_complete", "blocked"}
STANDARD_OUTPUTS = {"main.tex", "references.bib", "readiness.md", "main.pdf"}


def _read_text(path: Path, reporter: common.Reporter, description: str) -> str | None:
    try:
        contents = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        reporter.error("E_FILE_MISSING", f"missing {description}", path=path.name)
        return None
    except (OSError, UnicodeError) as exc:
        reporter.error("E_FILE_READ", f"could not read {description}: {exc}", path=path.name)
        return None
    if not contents.strip():
        reporter.error("E_FILE_EMPTY", f"{description} is empty", path=path.name)
        return None
    return contents


def _tex_citation_keys(tex: str) -> set[str]:
    keys: set[str] = set()
    pattern = re.compile(
        r"\\(?:cite|citep|citet|autocite|parencite|textcite)[A-Za-z]*"
        r"\s*(?:\[[^\]]*\]\s*)*\{([^}]*)\}"
    )
    for match in pattern.finditer(tex):
        keys.update(key.strip() for key in match.group(1).split(",") if key.strip())
    return keys


def _bib_keys(bib: str) -> set[str]:
    return {match.group(1).strip() for match in re.finditer(r"@[A-Za-z]+\s*\{\s*([^,\s]+)", bib)}


def _claim_map(expectations: Mapping[str, object], reporter: common.Reporter) -> dict[str, set[str]]:
    raw = expectations.get("claim_ids_by_result")
    if not isinstance(raw, dict):
        reporter.error("E_EXPECTATIONS", "claim_ids_by_result must be an object", path="expectations.json#/claim_ids_by_result", repairable=False)
        return {}
    output: dict[str, set[str]] = {}
    for result_id, values in raw.items():
        parsed = common.string_list(values)
        if not isinstance(result_id, str) or parsed is None:
            reporter.error("E_EXPECTATIONS", "claim_ids_by_result has invalid entries", path="expectations.json#/claim_ids_by_result", repairable=False)
            continue
        output[result_id] = set(parsed)
    return output


def validate(*, workspace: Path, expectations: Mapping[str, object]) -> common.ValidationReport:
    reporter = common.Reporter()
    result_ids = set(common.expectation_string_list(expectations, "result_ids", reporter))
    claims_by_result = _claim_map(expectations, reporter)
    previous_findings = set(common.expectation_string_list(expectations, "previous_finding_ids", reporter))
    historical_findings = set(common.expectation_string_list(expectations, "historical_finding_ids", reporter))
    authors = common.string_list(expectations.get("authors"))
    if authors is None:
        reporter.error("E_EXPECTATIONS", "authors must be an array of strings", path="expectations.json#/authors", repairable=False)
        authors = []
    result = common.read_json_object(workspace / common.AGENT_RESULT_FILENAME, reporter, code="E_RESULT_JSON", description="paper result")
    tex = _read_text(workspace / "main.tex", reporter, "main.tex")
    bib = _read_text(workspace / "references.bib", reporter, "references.bib")
    readiness = common.read_markdown(workspace / "readiness.md", reporter, code="E_READINESS", description="paper readiness report")
    files = [path for path in (workspace / "main.tex", workspace / "references.bib", workspace / "readiness.md") if path.is_file()]
    pdf = workspace / "main.pdf"
    if not pdf.is_file() or pdf.stat().st_size == 0:
        reporter.error(
            "E_PDF_MISSING",
            "main.pdf is missing or empty; compile main.tex before validating",
            path="main.pdf",
        )
    else:
        files.append(pdf)
    tex_log = workspace / "main.log"
    build_log = workspace / "build.log"
    inspected_log = tex_log if tex_log.is_file() else build_log
    if not inspected_log.is_file() or inspected_log.stat().st_size == 0:
        reporter.error(
            "E_LATEX_LOG",
            "main.log or build.log is required after compiling main.tex",
            path=inspected_log.name,
        )
    else:
        log_text = inspected_log.read_bytes().decode(
            "utf-8", errors="replace"
        ).lower()
        bad_patterns = (
            "there were undefined references",
            "undefined citations",
            "citation `",
        )
        if any(pattern in log_text for pattern in bad_patterns) and "undefined" in log_text:
            reporter.error(
                "E_LATEX_UNDEFINED",
                "LaTeX log reports undefined citations or references",
                path=inspected_log.name,
            )
    if result is None:
        return common.ValidationReport(files=files, issues=reporter.issues)
    common.validate_result_schema(result, expectations, reporter)
    status = result.get("status")
    if status not in PAPER_STATUSES:
        reporter.error("E_STATUS", "status must be draft_complete or blocked", path="agent-result.json#/status")
    for field in ("title", "summary"):
        if not isinstance(result.get(field), str) or not result[field].strip():
            reporter.error("E_TEXT", f"{field} must be nonempty", path=f"agent-result.json#/{field}")
    for field in ("unresolved_issues", "warnings"):
        if common.string_list(result.get(field)) is None:
            reporter.error("E_STRING_LIST", f"{field} must be an array of strings", path=f"agent-result.json#/{field}")
    rows = result.get("results")
    seen_results: set[str] = set()
    valid_rows: list[dict] = []
    if not isinstance(rows, list):
        reporter.error("E_RESULTS", "results must be an array", path="agent-result.json#/results")
        rows = []
    for index, row in enumerate(rows):
        base = f"agent-result.json#/results/{index}"
        if not isinstance(row, dict):
            reporter.error("E_RESULT_ENTRY", "result entry must be an object", path=base)
            continue
        result_id = row.get("result_id")
        if result_id not in result_ids or result_id in seen_results:
            reporter.error("E_RESULT_ID", f"unknown or duplicate result {result_id!r}", path=f"{base}/result_id")
            continue
        seen_results.add(result_id)
        valid_rows.append(row)
        disposition = row.get("disposition")
        if disposition not in {"included_main", "included_supporting", "excluded"}:
            reporter.error("E_DISPOSITION", "invalid disposition", path=f"{base}/disposition")
        claims = common.string_list(row.get("source_claim_ids"))
        if claims is None or len(set(claims)) != len(claims):
            reporter.error("E_SOURCE_CLAIMS", "source_claim_ids must be a unique string array", path=f"{base}/source_claim_ids")
            claims = []
        for claim_id in claims:
            if not CLAIM_ID_RE.fullmatch(claim_id) or claim_id not in claims_by_result.get(result_id, set()):
                reporter.error("E_SOURCE_CLAIM", f"unknown solver claim {claim_id!r} for {result_id}", path=f"{base}/source_claim_ids")
        labels = common.string_list(row.get("manuscript_labels"))
        if labels is None or any(not LABEL_RE.fullmatch(label) for label in labels) or len(set(labels or [])) != len(labels or []):
            reporter.error("E_LABELS", "manuscript_labels must be unique nonempty literal label keys", path=f"{base}/manuscript_labels")
            labels = []
        if disposition != "excluded" and (not claims or not labels):
            reporter.error("E_INCLUDED_RESULT", "included result requires claims and manuscript labels", path=base)
        if not isinstance(row.get("explanation"), str):
            reporter.error("E_EXPLANATION", "explanation must be a string", path=f"{base}/explanation")
    for result_id in sorted(result_ids.difference(seen_results)):
        reporter.error("E_MISSING_RESULT", f"paper result omits {result_id}", path="agent-result.json#/results")
    unresolved = result.get("unresolved_issues") if isinstance(result.get("unresolved_issues"), list) else []
    if status == "draft_complete" and (unresolved or any(row.get("disposition") == "excluded" for row in valid_rows)):
        reporter.error("E_DRAFT_COMPLETE", "draft_complete cannot have unresolved issues or excluded results", path="agent-result.json#/status")
    if status == "blocked" and not unresolved:
        reporter.error("E_BLOCKED", "blocked status requires unresolved issues", path="agent-result.json#/unresolved_issues")
    addressed = result.get("addressed_findings")
    addressed_ids: set[str] = set()
    if not isinstance(addressed, list):
        reporter.error("E_ADDRESSED_FINDINGS", "addressed_findings must be an array", path="agent-result.json#/addressed_findings")
    else:
        for index, item in enumerate(addressed):
            base = f"agent-result.json#/addressed_findings/{index}"
            if not isinstance(item, dict):
                reporter.error("E_ADDRESSED_FINDING", "entry must be an object", path=base)
                continue
            finding_id = item.get("finding_id")
            if finding_id not in previous_findings or finding_id in addressed_ids:
                detail = "historical finding must not be repeated" if finding_id in historical_findings else "unknown or duplicate finding"
                reporter.error("E_FINDING_ID", f"{detail}: {finding_id!r}", path=f"{base}/finding_id")
            else:
                addressed_ids.add(finding_id)
            if item.get("disposition") not in {"resolved", "not_resolved", "rejected"} or not isinstance(item.get("explanation"), str):
                reporter.error("E_FINDING_DISPOSITION", "invalid disposition or explanation", path=base)
    for finding_id in sorted(previous_findings.difference(addressed_ids)):
        reporter.error("E_MISSING_FINDING", f"paper result does not address {finding_id}", path="agent-result.json#/addressed_findings")
    citation_keys: set[str] = set()
    bibliography_keys: set[str] = set()
    if tex is not None:
        for required in (r"\documentclass", r"\begin{document}", r"\end{document}"):
            if required not in tex:
                reporter.error("E_TEX_STRUCTURE", f"main.tex omits {required}", path="main.tex")
        if not re.search(r"\\title\s*\{", tex):
            reporter.error("E_TEX_TITLE", "main.tex has no title", path="main.tex")
        if not re.search(r"\\date\s*\{\s*\}", tex):
            reporter.error("E_TEX_DATE", r"main.tex must use \date{}", path="main.tex")
        if not authors and not re.search(r"\\author\s*\{\s*\}", tex):
            reporter.error("E_TEX_AUTHOR", r"main.tex must use \author{} when no author is supplied", path="main.tex")
        if not re.search(r"\\begin\s*\{abstract\}", tex):
            reporter.error("E_TEX_ABSTRACT", "main.tex has no abstract", path="main.tex")
        citation_keys = _tex_citation_keys(tex)
        if not citation_keys:
            reporter.error("E_TEX_CITATIONS", "main.tex contains no citations", path="main.tex")
    if bib is not None:
        bibliography_keys = _bib_keys(bib)
    missing_bib = citation_keys.difference(bibliography_keys)
    if missing_bib:
        reporter.error("E_BIB_MISSING", "missing bibliography keys: " + ", ".join(sorted(missing_bib)), path="references.bib")
    citations = result.get("citations")
    structured_keys: set[str] = set()
    origin_coverage: set[str] = set()
    if not isinstance(citations, list):
        reporter.error("E_CITATIONS", "citations must be an array", path="agent-result.json#/citations")
    else:
        for index, citation in enumerate(citations):
            base = f"agent-result.json#/citations/{index}"
            if not isinstance(citation, dict):
                reporter.error("E_CITATION", "citation must be an object", path=base)
                continue
            key = citation.get("bib_key")
            if not isinstance(key, str) or key in structured_keys or key not in bibliography_keys or key not in citation_keys:
                reporter.error("E_CITATION_KEY", f"duplicate, unused, or undefined citation {key!r}", path=f"{base}/bib_key")
            else:
                structured_keys.add(key)
            url = citation.get("url")
            if not isinstance(url, str) or (url and not url.startswith(("https://", "http://"))):
                reporter.error("E_CITATION_URL", "URL must be empty or HTTP(S)", path=f"{base}/url")
            for field in ("title", "verification"):
                if not isinstance(citation.get(field), str) or not citation[field].strip():
                    reporter.error("E_CITATION_TEXT", f"{field} must be nonempty", path=f"{base}/{field}")
            if citation.get("role") not in {"original_problem", "related_work", "technique", "other"}:
                reporter.error("E_CITATION_ROLE", "invalid role", path=f"{base}/role")
            referenced = common.string_list(citation.get("result_ids"))
            if referenced is None or any(value not in result_ids for value in referenced):
                reporter.error("E_CITATION_RESULTS", "invalid result_ids", path=f"{base}/result_ids")
                referenced = []
            if citation.get("role") == "original_problem":
                origin_coverage.update(referenced)
    for result_id in sorted(result_ids.difference(origin_coverage)):
        reporter.error("E_ORIGIN_CITATION", f"no originating-paper citation for {result_id}", path="agent-result.json#/citations")
    if structured_keys != citation_keys or bibliography_keys != citation_keys:
        reporter.error("E_CITATION_SET", "main.tex, references.bib, and structured citation keys must match exactly", path="agent-result.json#/citations")
    if readiness is not None:
        for result_id in sorted(result_ids):
            if result_id not in readiness:
                reporter.error("E_READINESS_RESULT", f"readiness.md omits {result_id}", path="readiness.md")
        for row in valid_rows:
            for claim_id in row.get("source_claim_ids", []):
                if claim_id not in readiness:
                    reporter.error("E_READINESS_CLAIM", f"readiness.md omits {claim_id}", path="readiness.md")
            if tex is not None:
                for label in row.get("manuscript_labels", []):
                    if f"\\label{{{label}}}" not in tex:
                        reporter.error("E_TEX_LABEL", f"main.tex omits literal label {label}", path="main.tex")
        for finding_id in sorted(previous_findings):
            if finding_id not in readiness:
                reporter.error("E_READINESS_FINDING", f"readiness.md omits {finding_id}", path="readiness.md")
    generated = result.get("generated_files")
    seen_generated: set[str] = set()
    if not isinstance(generated, list):
        reporter.error("E_GENERATED_FILES", "generated_files must be an array", path="agent-result.json#/generated_files")
    else:
        for index, value in enumerate(generated):
            relative = common.safe_relative_path(value)
            item_path = f"agent-result.json#/generated_files/{index}"
            normalized = relative.as_posix() if relative is not None else ""
            if relative is None or normalized in seen_generated or (
                normalized not in STANDARD_OUTPUTS
                and (
                    not relative.parts
                    or relative.parts[0] not in {"figures", "code"}
                )
            ):
                reporter.error("E_GENERATED_PATH", f"invalid or duplicate generated file {value!r}", path=item_path)
                continue
            seen_generated.add(normalized)
            path = workspace.joinpath(*relative.parts)
            if not path.is_file():
                reporter.error("E_GENERATED_MISSING", f"listed generated file does not exist: {normalized}", path=item_path)
            elif normalized not in STANDARD_OUTPUTS:
                files.append(path)
        for normalized in sorted(seen_generated):
            relative = PurePosixPath(normalized)
            if (
                relative.parts[0] == "figures"
                and relative.suffix.casefold() == ".svg"
                and relative.with_suffix(".pdf").as_posix()
                not in seen_generated
            ):
                reporter.error("E_SVG_PDF", f"generated SVG requires matching PDF: {normalized}", path="agent-result.json#/generated_files")
    return common.ValidationReport(result=result, files=files, issues=reporter.issues)


if __name__ == "__main__":
    # Agent-only entry point for the staged workspace. Repository drivers
    # import validate() and pass authoritative expectations directly.
    raise SystemExit(common.run_agent_validation(validate, validation_directory=Path(__file__).resolve().parent))
