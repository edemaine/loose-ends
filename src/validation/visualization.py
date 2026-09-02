"""Validate generated paper-visualization runs: annotations and widgets."""

from __future__ import annotations

import json
from pathlib import Path
import re
import shutil
import subprocess
from typing import Mapping

from . import common


OUTPUT_DIRECTORY = "output"
ANNOTATIONS_NAME = "annotations.json"
WIDGETS_DIRECTORY = "widgets"
WIDGET_MANIFEST_NAME = "widget.json"
WIDGET_ENTRY_NAME = "widget.js"
WIDGET_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,80}$")
MAX_WIDGET_FILES = 40
MAX_WIDGET_BYTES = 4 * 1024 * 1024
MAX_GLOSSARY = 200
MAX_STEPS = 12
FORBIDDEN_PATTERNS = (
    (re.compile(r"https?://", re.IGNORECASE), "remote URL"),
    (re.compile(r"\bimport\s*\("), "dynamic import"),
    (re.compile(r"^\s*import\s+[\w{*]", re.MULTILINE), "ES module import"),
    (re.compile(r"^\s*export\s+", re.MULTILINE), "ES module export"),
    (re.compile(r"\bXMLHttpRequest\b"), "XMLHttpRequest"),
    (re.compile(r"\bWebSocket\b"), "WebSocket"),
    (re.compile(r"\bEventSource\b"), "EventSource"),
    (re.compile(r"\b(localStorage|sessionStorage|indexedDB)\b"), "browser storage"),
    (re.compile(r"\beval\s*\("), "eval"),
    (re.compile(r"new\s+Function\s*\("), "Function constructor"),
    (re.compile(r"new\s+Worker\s*\("), "web worker"),
    (re.compile(r"\bdocument\.cookie\b"), "cookies"),
    (re.compile(r"\bwindow\.(parent|top|open)\b"), "parent window access"),
)


def _nonempty_string(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _widget_id_for(anchor: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", anchor.lower()).strip("-")
    return slug or "widget"


def validate_annotations(
    annotations: object,
    ids: Mapping[str, str],
    proofs: Mapping[str, list],
    reporter: common.Reporter,
) -> None:
    path = f"{OUTPUT_DIRECTORY}/{ANNOTATIONS_NAME}"
    if not isinstance(annotations, dict):
        reporter.error("E_ANNOTATIONS", "annotations must be a JSON object", path=path)
        return
    allowed = {"main_result", "glossary", "proof_outlines", "notes"}
    for key in sorted(set(annotations).difference(allowed)):
        reporter.error("E_ANNOTATIONS", f"unknown annotations field {key!r}", path=f"{path}#/{key}")
    main = annotations.get("main_result")
    if main is not None and (not isinstance(main, str) or ids.get(main) in {None, "paragraph", "section", "proof"}):
        reporter.error("E_MAIN_RESULT", f"main_result must name a statement id, not {main!r}", path=f"{path}#/main_result")
    glossary = annotations.get("glossary", [])
    if not isinstance(glossary, list):
        reporter.error("E_GLOSSARY", "glossary must be an array", path=f"{path}#/glossary")
        glossary = []
    if len(glossary) > MAX_GLOSSARY:
        reporter.error("E_GLOSSARY", f"glossary exceeds {MAX_GLOSSARY} entries", path=f"{path}#/glossary")
    seen_ids: set[str] = set()
    for index, entry in enumerate(glossary):
        entry_path = f"{path}#/glossary/{index}"
        if not isinstance(entry, dict):
            reporter.error("E_GLOSSARY", "glossary entry must be an object", path=entry_path)
            continue
        for field in ("id", "term", "gloss", "anchor"):
            if not _nonempty_string(entry.get(field)):
                reporter.error("E_GLOSSARY", f"glossary entry needs nonempty {field}", path=entry_path)
        identifier = entry.get("id")
        if isinstance(identifier, str):
            if identifier in seen_ids:
                reporter.error("E_GLOSSARY", f"duplicate glossary id {identifier}", path=entry_path)
            seen_ids.add(identifier)
        anchor = entry.get("anchor")
        if isinstance(anchor, str) and anchor not in ids:
            reporter.error("E_GLOSSARY", f"glossary anchor {anchor!r} is not an element of the document", path=entry_path)
        for field in ("forms", "latex_forms"):
            if field in entry and common.string_list(entry.get(field)) is None:
                reporter.error("E_GLOSSARY", f"{field} must be an array of strings", path=entry_path)
        if "kind" in entry and not isinstance(entry.get("kind"), str):
            reporter.error("E_GLOSSARY", "kind must be a string", path=entry_path)
        if "source" in entry and not isinstance(entry.get("source"), str):
            reporter.error("E_GLOSSARY", "source must be a string", path=entry_path)
        extra = set(entry).difference({"id", "term", "forms", "latex_forms", "kind", "anchor", "gloss", "source"})
        for key in sorted(extra):
            reporter.error("E_GLOSSARY", f"unknown glossary field {key!r}", path=entry_path)
    outlines = annotations.get("proof_outlines", {})
    if not isinstance(outlines, dict):
        reporter.error("E_OUTLINE", "proof_outlines must map proof ids to step arrays", path=f"{path}#/proof_outlines")
        outlines = {}
    for proof_id, steps in outlines.items():
        outline_path = f"{path}#/proof_outlines/{proof_id}"
        if proof_id not in proofs:
            reporter.error("E_OUTLINE", f"{proof_id!r} is not a proof id", path=outline_path)
            continue
        validate_steps(steps, proofs[proof_id], reporter, outline_path, require_paragraphs=True)


def validate_steps(
    steps: object,
    paragraphs: list,
    reporter: common.Reporter,
    path: str,
    *,
    require_paragraphs: bool,
) -> None:
    if not isinstance(steps, list) or not steps:
        reporter.error("E_STEPS", "steps must be a nonempty array", path=path)
        return
    if len(steps) > MAX_STEPS:
        reporter.error("E_STEPS", f"at most {MAX_STEPS} steps are allowed", path=path)
    order = {identifier: index for index, identifier in enumerate(paragraphs)}
    last = -1
    used: set[str] = set()
    for index, step in enumerate(steps):
        step_path = f"{path}/{index}"
        if not isinstance(step, dict):
            reporter.error("E_STEPS", "step must be an object", path=step_path)
            continue
        if not _nonempty_string(step.get("title")):
            reporter.error("E_STEPS", "step needs a nonempty title", path=step_path)
        for key in sorted(set(step).difference({"title", "paragraphs", "note", "summary"})):
            reporter.error("E_STEPS", f"unknown step field {key!r}", path=step_path)
        listed = step.get("paragraphs", [])
        if common.string_list(listed) is None or (require_paragraphs and not listed):
            reporter.error("E_STEPS", "step paragraphs must be a nonempty array of paragraph ids", path=step_path)
            continue
        for identifier in listed:
            if identifier not in order:
                reporter.error("E_STEPS", f"paragraph {identifier!r} does not belong to this proof", path=step_path)
                continue
            if identifier in used:
                reporter.error("E_STEPS", f"paragraph {identifier!r} appears in more than one step", path=step_path)
            used.add(identifier)
            if order[identifier] < last:
                reporter.error("E_STEPS", f"paragraph {identifier!r} is out of reading order", path=step_path)
            last = max(last, order[identifier])


def validate_widget_script(path: Path, widget_id: str, reporter: common.Reporter, relative: str) -> None:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        reporter.error("E_WIDGET_JS", f"could not read widget script: {exc}", path=relative)
        return
    if f'registerWidget("{widget_id}"' not in text and f"registerWidget('{widget_id}'" not in text:
        reporter.error("E_WIDGET_JS", f'widget.js must call LooseEnds.registerWidget("{widget_id}", ...)', path=relative)
    for pattern, description in FORBIDDEN_PATTERNS:
        if pattern.search(text):
            reporter.error("E_WIDGET_JS", f"widget.js must not use {description}", path=relative)
    node = shutil.which("node")
    if node is not None:
        try:
            completed = subprocess.run([node, "--check", str(path)], capture_output=True, text=True, timeout=60, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            reporter.error("E_WIDGET_JS", f"could not run node --check: {exc}", path=relative, repairable=False)
            return
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip().splitlines()
            reporter.error("E_WIDGET_JS", "widget.js has a syntax error: " + " | ".join(detail[:4]), path=relative)


def validate(*, workspace: Path, expectations: Mapping[str, object]) -> common.ValidationReport:
    reporter = common.Reporter()
    result = common.read_json_object(
        workspace / common.AGENT_RESULT_FILENAME, reporter,
        code="E_RESULT_JSON", description="visualization result",
    )
    files: list[Path] = []
    if result is None:
        return common.ValidationReport(files=files, issues=reporter.issues)
    common.validate_result_schema(result, expectations, reporter)
    if not _nonempty_string(result.get("summary")):
        reporter.error("E_TEXT", "summary must be nonempty", path="agent-result.json#/summary")

    ids_value = expectations.get("document_ids")
    ids: dict[str, str] = ids_value if isinstance(ids_value, dict) else {}
    proofs_value = expectations.get("proof_paragraphs")
    proofs: dict[str, list] = proofs_value if isinstance(proofs_value, dict) else {}
    requested = common.expectation_string_list(expectations, "anchors", reporter)
    annotations_required = bool(expectations.get("annotations_required"))

    output = workspace / OUTPUT_DIRECTORY
    annotations_path = output / ANNOTATIONS_NAME
    updated = result.get("annotations_updated")
    if annotations_path.is_file():
        annotations = common.read_json_object(annotations_path, reporter, code="E_ANNOTATIONS", description="annotations")
        if annotations is not None:
            validate_annotations(annotations, ids, proofs, reporter)
            files.append(annotations_path)
        if updated is False:
            reporter.error("E_ANNOTATIONS", "annotations_updated must be true when output/annotations.json is written", path="agent-result.json#/annotations_updated")
    else:
        if annotations_required:
            reporter.error("E_ANNOTATIONS", "this run must write output/annotations.json", path=f"{OUTPUT_DIRECTORY}/{ANNOTATIONS_NAME}")
        if updated is True:
            reporter.error("E_ANNOTATIONS", "annotations_updated is true but output/annotations.json is missing", path="agent-result.json#/annotations_updated")

    widgets = result.get("widgets")
    widgets = widgets if isinstance(widgets, list) else []
    expected_widget_anchors = [anchor for anchor in requested if anchor != "default"]
    declared_anchors: set[str] = set()
    declared_ids: set[str] = set()
    listed_files: set[str] = set()
    for index, widget in enumerate(widgets):
        widget_path = f"agent-result.json#/widgets/{index}"
        if not isinstance(widget, dict):
            continue
        widget_id = widget.get("id")
        anchor = widget.get("anchor")
        if not isinstance(widget_id, str) or not WIDGET_ID_RE.fullmatch(widget_id):
            reporter.error("E_WIDGET", f"invalid widget id {widget_id!r}", path=widget_path)
            continue
        if widget_id in declared_ids:
            reporter.error("E_WIDGET", f"duplicate widget id {widget_id}", path=widget_path)
            continue
        declared_ids.add(widget_id)
        if not isinstance(anchor, str) or anchor not in ids:
            reporter.error("E_WIDGET", f"widget anchor {anchor!r} is not an element of the document", path=widget_path)
            continue
        declared_anchors.add(anchor)
        if widget_id != _widget_id_for(anchor):
            reporter.error("E_WIDGET", f"widget id for anchor {anchor} must be {_widget_id_for(anchor)!r}", path=widget_path)
        kind = widget.get("kind")
        anchor_kind = ids.get(anchor)
        if anchor_kind == "proof" and kind != "proof":
            reporter.error("E_WIDGET", "a widget anchored to a proof must have kind 'proof'", path=widget_path)
        elif anchor_kind != "proof" and kind == "proof":
            reporter.error("E_WIDGET", "a proof widget must be anchored to a proof id", path=widget_path)
        if anchor_kind in {"paragraph", "section", "equation", "figure", "table"}:
            reporter.error("E_WIDGET", f"widgets must anchor to a statement or proof, not a {anchor_kind}", path=widget_path)
        directory = output / WIDGETS_DIRECTORY / widget_id
        relative_directory = f"{OUTPUT_DIRECTORY}/{WIDGETS_DIRECTORY}/{widget_id}"
        manifest = common.read_json_object(directory / WIDGET_MANIFEST_NAME, reporter, code="E_WIDGET_MANIFEST", description=f"{relative_directory}/{WIDGET_MANIFEST_NAME}")
        if manifest is not None:
            files.append(directory / WIDGET_MANIFEST_NAME)
            if manifest.get("id") != widget_id or manifest.get("anchor") != anchor or manifest.get("kind") != kind:
                reporter.error("E_WIDGET_MANIFEST", "widget.json id, anchor, and kind must match agent-result.json", path=f"{relative_directory}/{WIDGET_MANIFEST_NAME}")
            for field in ("title", "summary"):
                if not _nonempty_string(manifest.get(field)):
                    reporter.error("E_WIDGET_MANIFEST", f"widget.json needs nonempty {field}", path=f"{relative_directory}/{WIDGET_MANIFEST_NAME}")
            for key in sorted(set(manifest).difference({"id", "anchor", "kind", "title", "summary", "limitations", "steps"})):
                reporter.error("E_WIDGET_MANIFEST", f"unknown widget.json field {key!r}", path=f"{relative_directory}/{WIDGET_MANIFEST_NAME}")
            steps = manifest.get("steps")
            if kind == "proof":
                validate_steps(steps, proofs.get(anchor, []), reporter, f"{relative_directory}/{WIDGET_MANIFEST_NAME}#/steps", require_paragraphs=True)
            elif steps:
                reporter.error("E_WIDGET_MANIFEST", "statement widgets must not define steps", path=f"{relative_directory}/{WIDGET_MANIFEST_NAME}")
        entry = directory / WIDGET_ENTRY_NAME
        if not entry.is_file():
            reporter.error("E_WIDGET_JS", f"missing {relative_directory}/{WIDGET_ENTRY_NAME}", path=relative_directory)
        else:
            validate_widget_script(entry, widget_id, reporter, f"{relative_directory}/{WIDGET_ENTRY_NAME}")
            files.append(entry)
        listed = widget.get("files")
        listed = listed if isinstance(listed, list) else []
        normalized: set[str] = set()
        for value in listed:
            relative = common.safe_relative_path(value)
            if relative is None or relative.parts[:3] != (OUTPUT_DIRECTORY, WIDGETS_DIRECTORY, widget_id):
                reporter.error("E_WIDGET_FILES", f"file must be a canonical {relative_directory}/... path, not {value!r}", path=widget_path)
                continue
            normalized.add(relative.as_posix())
        for required in (f"{relative_directory}/{WIDGET_MANIFEST_NAME}", f"{relative_directory}/{WIDGET_ENTRY_NAME}"):
            if required not in normalized:
                reporter.error("E_WIDGET_FILES", f"files must list {required}", path=widget_path)
        if directory.is_dir():
            actual = {
                f"{relative_directory}/{path.relative_to(directory).as_posix()}"
                for path in directory.rglob("*") if path.is_file() or path.is_symlink()
            }
            for extra in sorted(actual.difference(normalized)):
                reporter.error("E_WIDGET_FILES", f"unlisted widget file: {extra}", path=widget_path)
            for missing in sorted(normalized.difference(actual)):
                reporter.error("E_WIDGET_FILES", f"listed file does not exist: {missing}", path=widget_path)
            if len(actual) > MAX_WIDGET_FILES:
                reporter.error("E_WIDGET_FILES", f"widget exceeds {MAX_WIDGET_FILES} files", path=widget_path)
            total = sum(path.stat().st_size for path in directory.rglob("*") if path.is_file())
            if total > MAX_WIDGET_BYTES:
                reporter.error("E_WIDGET_FILES", "widget exceeds the 4 MiB limit", path=widget_path)
            for path in directory.rglob("*"):
                if path.is_symlink():
                    reporter.error("E_WIDGET_FILES", f"symbolic links are not allowed: {path.name}", path=widget_path, repairable=False)
                elif path.is_file() and path.suffix.lower() == ".html":
                    reporter.error("E_WIDGET_FILES", "widgets must not contain HTML documents", path=widget_path)
        listed_files.update(normalized)
    for anchor in expected_widget_anchors:
        if anchor not in declared_anchors:
            reporter.error("E_WIDGET", f"the requested anchor {anchor} has no widget in agent-result.json", path="agent-result.json#/widgets")
    main_result = None
    if annotations_path.is_file():
        try:
            loaded = json.loads(annotations_path.read_text(encoding="utf-8"))
            main_result = loaded.get("main_result") if isinstance(loaded, dict) else None
        except (OSError, UnicodeError, json.JSONDecodeError):
            main_result = None
    for anchor in sorted(declared_anchors.difference(expected_widget_anchors)):
        if "default" in requested and anchor == main_result:
            continue
        reporter.error(
            "E_WIDGET",
            f"widget for {anchor} was not requested"
            + (" (a default run may add only the main-result widget named in annotations.json)" if "default" in requested else ""),
            path="agent-result.json#/widgets",
        )
    if "default" in requested and main_result is not None and main_result not in declared_anchors:
        reporter.error("E_WIDGET", f"a default run must include a statement widget for the main result {main_result}", path="agent-result.json#/widgets")
    widgets_root = output / WIDGETS_DIRECTORY
    if widgets_root.is_dir():
        for path in widgets_root.iterdir():
            if path.name not in declared_ids:
                reporter.error("E_WIDGET", f"undeclared widget directory {path.name}", path=f"{OUTPUT_DIRECTORY}/{WIDGETS_DIRECTORY}")
    if output.is_dir():
        for path in output.iterdir():
            if path.name not in {ANNOTATIONS_NAME, WIDGETS_DIRECTORY}:
                reporter.error("E_OUTPUT", f"unexpected output entry {path.name}", path=OUTPUT_DIRECTORY)
    if "default" in requested and not annotations_path.is_file():
        pass  # already reported through annotations_required
    checks = result.get("verification_checks")
    if not isinstance(checks, list) or not checks:
        reporter.error("E_CHECKS", "verification_checks must contain at least one check", path="agent-result.json#/verification_checks")
    return common.ValidationReport(result=result, files=files, issues=reporter.issues)


if __name__ == "__main__":
    raise SystemExit(common.run_agent_validation(validate, validation_directory=Path(__file__).resolve().parent))
