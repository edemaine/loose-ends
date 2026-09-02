"""Validate generated free-form mathematical visualization packages."""

from __future__ import annotations

from pathlib import Path
import re
from typing import Mapping

from . import common


CLAIM_ID_RE = re.compile(r"^C-[0-9]{3,}$")
MAX_FILES = 200
MAX_TOTAL_BYTES = 25 * 1024 * 1024


def _nonempty_string(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def validate(
    *, workspace: Path, expectations: Mapping[str, object]
) -> common.ValidationReport:
    reporter = common.Reporter()
    result = common.read_json_object(
        workspace / common.AGENT_RESULT_FILENAME,
        reporter,
        code="E_RESULT_JSON",
        description="visualization result",
    )
    verification = common.read_markdown(
        workspace / "visualization" / "verification.md",
        reporter,
        code="E_VERIFICATION",
        description="visualization verification report",
    )
    files: list[Path] = []
    if result is None:
        if verification:
            files.append(workspace / "visualization" / "verification.md")
        return common.ValidationReport(files=files, issues=reporter.issues)

    common.validate_result_schema(result, expectations, reporter)
    for field in ("title", "summary"):
        if not _nonempty_string(result.get(field)):
            reporter.error(
                "E_TEXT", f"{field} must be nonempty",
                path=f"agent-result.json#/{field}",
            )

    known_claims_value = expectations.get("claim_ids", [])
    known_claims = set(known_claims_value) if isinstance(known_claims_value, list) else set()
    refs = result.get("claim_refs")
    seen_refs: set[str] = set()
    if not isinstance(refs, list):
        reporter.error("E_CLAIM_REFS", "claim_refs must be an array", path="agent-result.json#/claim_refs")
    else:
        for index, value in enumerate(refs):
            path = f"agent-result.json#/claim_refs/{index}"
            if not isinstance(value, str) or not CLAIM_ID_RE.fullmatch(value):
                reporter.error("E_CLAIM_REF", f"invalid claim reference {value!r}", path=path)
            elif value in seen_refs:
                reporter.error("E_CLAIM_REF", f"duplicate claim reference {value}", path=path)
            elif value not in known_claims:
                reporter.error("E_CLAIM_REF", f"unknown claim reference {value}", path=path)
            else:
                seen_refs.add(value)

    for field in ("concepts", "limitations", "warnings"):
        if common.string_list(result.get(field)) is None:
            reporter.error("E_STRING_LIST", f"{field} must be an array of strings", path=f"agent-result.json#/{field}")

    checks = result.get("verification_checks")
    if not isinstance(checks, list) or not checks:
        reporter.error("E_CHECKS", "verification_checks must contain at least one check", path="agent-result.json#/verification_checks")
    elif any(
        not isinstance(check, dict)
        or any(not _nonempty_string(check.get(field)) for field in ("name", "method", "details"))
        for check in checks
    ):
        reporter.error("E_CHECKS", "every verification check needs nonempty name, method, and details", path="agent-result.json#/verification_checks")

    entry = common.safe_relative_path(result.get("entry_point"))
    if entry is None or entry.as_posix() != "visualization/index.html":
        reporter.error("E_ENTRY_POINT", "entry_point must be visualization/index.html", path="agent-result.json#/entry_point")

    listed = result.get("files")
    seen_files: set[str] = set()
    total_bytes = 0
    if not isinstance(listed, list):
        reporter.error("E_FILES", "files must be an array", path="agent-result.json#/files")
    else:
        if len(listed) > MAX_FILES:
            reporter.error("E_FILES", f"visualization exceeds the {MAX_FILES}-file limit", path="agent-result.json#/files")
        for index, value in enumerate(listed):
            item_path = f"agent-result.json#/files/{index}"
            relative = common.safe_relative_path(value)
            if relative is None or len(relative.parts) < 2 or relative.parts[0] != "visualization":
                reporter.error("E_FILE_PATH", f"file must be a canonical visualization/... path, not {value!r}", path=item_path)
                continue
            normalized = relative.as_posix()
            if normalized in seen_files:
                reporter.error("E_FILE_PATH", f"duplicate file {normalized}", path=item_path)
                continue
            seen_files.add(normalized)
            target = workspace.joinpath(*relative.parts)
            if target.is_symlink():
                reporter.error("E_FILE_LINK", f"symbolic links are not allowed: {normalized}", path=item_path, repairable=False)
            elif not target.is_file():
                reporter.error("E_FILE_MISSING", f"listed file does not exist: {normalized}", path=item_path)
            else:
                total_bytes += target.stat().st_size
                files.append(target)
        required = {"visualization/index.html", "visualization/verification.md"}
        for missing in sorted(required.difference(seen_files)):
            reporter.error("E_FILES", f"files omits required {missing}", path="agent-result.json#/files")
    if total_bytes > MAX_TOTAL_BYTES:
        reporter.error("E_FILES", "visualization package exceeds the 25 MiB limit", path="agent-result.json#/files")

    visualization_root = workspace / "visualization"
    if visualization_root.is_dir():
        actual = {
            path.relative_to(workspace).as_posix()
            for path in visualization_root.rglob("*")
            if path.is_file() or path.is_symlink()
        }
        for extra in sorted(actual.difference(seen_files)):
            reporter.error("E_FILES", f"unlisted visualization file: {extra}", path="agent-result.json#/files")

    return common.ValidationReport(result=result, files=files, issues=reporter.issues)


if __name__ == "__main__":
    raise SystemExit(common.run_agent_validation(validate, validation_directory=Path(__file__).resolve().parent))
