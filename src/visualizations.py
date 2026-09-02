"""Discovery, layout, and path safety for paper visualization packages.

A visualization package lives beside its source (for example inside a
manuscript draft directory) and contains the converted reader document plus
LLM-generated annotations and widgets:

    visualization/
    ├── visualization.json      manifest
    ├── document.html           converted paper (deterministic)
    ├── document.json           structure: sections, statements, proofs, ...
    ├── figures/                rendered figures
    ├── annotations.json        glossary, main result, proof outlines
    ├── widgets/<id>/           widget.js, widget.json, review.json, ...
    └── runs/run-NNN/           logs and structured results of each run
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re

import open_problem_common as common
import paper_document


DIRECTORY_NAME = "visualization"
MANIFEST_NAME = "visualization.json"
ANNOTATIONS_NAME = "annotations.json"
WIDGETS_DIRECTORY = "widgets"
RUNS_DIRECTORY = "runs"
WIDGET_MANIFEST_NAME = "widget.json"
WIDGET_ENTRY_NAME = "widget.js"
WIDGET_REVIEW_NAME = "review.json"
RUN_RE = re.compile(r"^run-([0-9]{3,})$")
WIDGET_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,80}$")
READER_FILES = {"reader.html", "reader.js", "reader.css"}
MANIFEST_SCHEMA_VERSION = 2
DEFAULT_ANCHOR = "default"


def package_key(directory: Path) -> str:
    """Return a URL-safe opaque identity for one package."""
    return hashlib.sha256(str(directory.resolve()).encode("utf-8")).hexdigest()[:24]


def package_directory(source_directory: Path) -> Path:
    return source_directory / DIRECTORY_NAME


def widget_id(anchor: str) -> str:
    """Return the canonical widget directory name for an anchor."""
    slug = re.sub(r"[^a-z0-9]+", "-", anchor.lower()).strip("-")
    return slug or "widget"


def load_manifest(directory: Path) -> dict | None:
    manifest = common.load_json(directory / MANIFEST_NAME)
    return manifest if isinstance(manifest, dict) else None


def load_document(directory: Path) -> dict | None:
    document = common.load_json(directory / paper_document.DOCUMENT_JSON)
    return document if isinstance(document, dict) else None


def next_run_number(directory: Path) -> int:
    runs = directory / RUNS_DIRECTORY
    numbers = (
        [int(match.group(1)) for path in runs.iterdir() if path.is_dir() and (match := RUN_RE.fullmatch(path.name))]
        if runs.is_dir()
        else []
    )
    return max(numbers, default=0) + 1


def widget_records(directory: Path, manifest: dict) -> list[dict]:
    records = []
    for entry in manifest.get("widgets", []):
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), str):
            continue
        widget_directory = directory / WIDGETS_DIRECTORY / entry["id"]
        if not (widget_directory / (entry.get("entry") or WIDGET_ENTRY_NAME)).is_file():
            continue
        review = common.load_json(widget_directory / WIDGET_REVIEW_NAME)
        record = dict(entry)
        record["review"] = review if isinstance(review, dict) else None
        records.append(record)
    return records


def discover(source_directory: Path) -> dict | None:
    """Return a display-ready record for the package of one source, if any."""
    directory = package_directory(source_directory)
    manifest = load_manifest(directory)
    if manifest is None:
        return None
    document = load_document(directory)
    if document is None or not (directory / paper_document.DOCUMENT_HTML).is_file():
        return None
    widgets = widget_records(directory, manifest)
    annotations = common.load_json(directory / ANNOTATIONS_NAME)
    annotations = annotations if isinstance(annotations, dict) else None
    runs = manifest.get("runs", []) if isinstance(manifest.get("runs"), list) else []
    latest = runs[-1] if runs else None
    return {
        "key": package_key(directory),
        "directory": str(directory.resolve()),
        "title": str(document.get("title") or source_directory.name),
        "generatedAt": str(manifest.get("generated_at") or ""),
        "documentDigest": str(document.get("source", {}).get("digest") or ""),
        "statementCount": len(document.get("statements", [])),
        "proofCount": len(document.get("proofs", [])),
        "warnings": list(document.get("warnings", [])),
        "hasAnnotations": annotations is not None,
        "glossaryCount": len(annotations.get("glossary", [])) if annotations else 0,
        "mainResult": (annotations or {}).get("main_result") or "",
        "widgets": [
            {
                "id": widget["id"],
                "anchor": str(widget.get("anchor") or ""),
                "kind": str(widget.get("kind") or ""),
                "title": str(widget.get("title") or widget["id"]),
                "summary": str(widget.get("summary") or ""),
                "fidelity": str((widget.get("review") or {}).get("fidelity") or "unreviewed"),
                "interactionQuality": str((widget.get("review") or {}).get("interaction_quality") or "unreviewed"),
                "run": widget.get("run"),
            }
            for widget in widgets
        ],
        "widgetCount": len(widgets),
        "runCount": len(runs),
        "latestRun": latest,
    }


def resolve_file(directory: Path, relative_value: str) -> Path:
    """Resolve one package resource without allowing traversal or symlinks."""
    if not relative_value or "\\" in relative_value:
        raise ValueError("invalid visualization resource path")
    relative = Path(relative_value)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("invalid visualization resource path")
    root = directory.resolve()
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError("visualization resource leaves its package") from exc
    if not candidate.is_file():
        raise FileNotFoundError(candidate)
    unresolved = root
    for part in relative.parts:
        unresolved = unresolved / part
        if unresolved.is_symlink():
            raise ValueError("visualization resources cannot use symbolic links")
    return candidate


def write_manifest(directory: Path, manifest: dict) -> None:
    manifest = {**manifest, "schema_version": MANIFEST_SCHEMA_VERSION}
    common.write_json(directory / MANIFEST_NAME, manifest)


def new_manifest(document: dict, *, source: dict) -> dict:
    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "generated_at": common.utc_now(),
        "source": source,
        "document": {
            "digest": document.get("source", {}).get("digest", ""),
            "built_at": common.utc_now(),
            "warnings": document.get("warnings", []),
        },
        "annotations": None,
        "widgets": [],
        "runs": [],
    }


def anchor_descriptions(document: dict) -> dict[str, dict]:
    """Describe every statement and proof anchor for prompts and validation."""
    described: dict[str, dict] = {}
    for statement in document.get("statements", []):
        described[statement["id"]] = {
            "id": statement["id"],
            "kind": statement.get("kind", "statement"),
            "label": statement.get("label", ""),
            "title": statement.get("title", ""),
            "text": statement.get("text", ""),
            "paragraphs": statement.get("paragraphs", []),
            "proofs": statement.get("proofs", []),
        }
    for proof in document.get("proofs", []):
        described[proof["id"]] = {
            "id": proof["id"],
            "kind": "proof",
            "label": proof.get("title", "Proof"),
            "of": proof.get("of"),
            "paragraphs": proof.get("paragraphs", []),
        }
    return described


def dump(value: object) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True)
