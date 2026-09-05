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
NOTES_NAME = "notes.json"
NOTE_ID_RE = re.compile(r"^note-[0-9]{3,}$")
MAX_NOTES = 200
MAX_NOTE_TEXT = 2000
RUN_RE = re.compile(r"^run-([0-9]{3,})$")
WIDGET_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,80}$")
READER_FILES = {"reader.html", "reader.js", "reader.css"}
MANIFEST_SCHEMA_VERSION = 2
ANNOTATIONS_SCHEMA_VERSION = 1
WIDGET_SCHEMA_VERSION = 1
REVIEW_SCHEMA_VERSION = 1
WIDGET_API_VERSION = 1
DEFAULT_ANCHOR = "default"
NOTES_ANCHOR = "notes"


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
    """Widget records for display: `widget.json` is the source of metadata,
    the package manifest only lists ids, anchors, and provenance."""
    records = []
    for entry in manifest.get("widgets", []):
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), str):
            continue
        widget_directory = directory / WIDGETS_DIRECTORY / entry["id"]
        if not (widget_directory / (entry.get("entry") or WIDGET_ENTRY_NAME)).is_file():
            continue
        record = dict(entry)
        metadata = common.load_json(widget_directory / WIDGET_MANIFEST_NAME)
        if isinstance(metadata, dict):
            for field in ("title", "summary", "kind", "anchor", "steps", "examples", "limitations", "api_version", "document_digest"):
                if field in metadata:
                    record[field] = metadata[field]
        review = common.load_json(widget_directory / WIDGET_REVIEW_NAME)
        record["review"] = review if isinstance(review, dict) else None
        records.append(record)
    return records


def stamp_widget_files(widget_directory: Path, document_digest: str, run_name: str) -> None:
    """Record schema versions and the source digest on a widget's files."""
    manifest = common.load_json(widget_directory / WIDGET_MANIFEST_NAME)
    if isinstance(manifest, dict):
        manifest.setdefault("schema_version", WIDGET_SCHEMA_VERSION)
        manifest.setdefault("api_version", WIDGET_API_VERSION)
        manifest["document_digest"] = document_digest
        manifest["run"] = run_name
        common.write_json(widget_directory / WIDGET_MANIFEST_NAME, manifest)
    review = common.load_json(widget_directory / WIDGET_REVIEW_NAME)
    if isinstance(review, dict):
        review.setdefault("schema_version", REVIEW_SCHEMA_VERSION)
        review["document_digest"] = document_digest
        common.write_json(widget_directory / WIDGET_REVIEW_NAME, review)


def merge_live_annotations(live: dict | None, generated: dict, *, addressed: list[str]) -> dict:
    """Carry quick answers created while a run was in flight into its output.

    A quick explanation survives unless the run addressed its note, replaced
    its id, or the run itself now explains the same note.
    """
    merged = dict(generated)
    if not live:
        return merged
    existing = merged.get("explanations")
    existing = existing if isinstance(existing, list) else []
    generated_ids = {entry.get("id") for entry in existing if isinstance(entry, dict)}
    generated_notes = {entry.get("note") for entry in existing if isinstance(entry, dict) and entry.get("note")}
    carried = []
    for entry in live.get("explanations", []) if isinstance(live.get("explanations"), list) else []:
        if not isinstance(entry, dict) or entry.get("provenance") != "quick":
            continue
        if entry.get("id") in generated_ids or entry.get("note") in addressed or entry.get("note") in generated_notes:
            continue
        carried.append(entry)
    if carried:
        merged["explanations"] = existing + carried
    return merged


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
        "noteCount": len(load_notes(directory)),
        "openNoteCount": len(open_notes(directory)),
        "runCount": len(runs),
        "latestRun": latest,
    }


def load_notes(directory: Path) -> list[dict]:
    """Return the reader's notes for one package (oldest first)."""
    value = common.load_json(directory / NOTES_NAME)
    notes = value.get("notes") if isinstance(value, dict) else None
    return [note for note in notes if isinstance(note, dict) and isinstance(note.get("id"), str)] if isinstance(notes, list) else []


def write_notes(directory: Path, notes: list[dict]) -> None:
    common.write_json(directory / NOTES_NAME, {"schema_version": 1, "notes": notes})


def add_note(directory: Path, note: dict) -> dict:
    """Validate and append one reader note; returns the stored note."""
    anchor = note.get("anchor")
    quote = note.get("quote")
    message = note.get("message", "")
    if not isinstance(anchor, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9:._-]{0,120}", anchor):
        raise ValueError("note anchor must be an element id")
    if not isinstance(quote, str) or not quote.strip() or len(quote) > MAX_NOTE_TEXT:
        raise ValueError("note quote must be nonempty text")
    if not isinstance(message, str) or len(message) > MAX_NOTE_TEXT:
        raise ValueError("note message must be text")
    latex = note.get("latex", "")
    if latex is None:
        latex = ""
    if not isinstance(latex, str) or len(latex) > MAX_NOTE_TEXT:
        raise ValueError("note latex must be text")
    revises = note.get("revises") or ""
    if not isinstance(revises, str) or len(revises) > 200:
        raise ValueError("revises must name an explanation id")
    widget = note.get("widget") or ""
    if not isinstance(widget, str) or (widget and not WIDGET_ID_RE.fullmatch(widget)):
        raise ValueError("widget must be a widget id")
    if widget and not (directory / WIDGETS_DIRECTORY / widget / WIDGET_MANIFEST_NAME).is_file():
        raise ValueError(f"unknown widget {widget}")
    step = note.get("step")
    if step is not None and (isinstance(step, bool) or not isinstance(step, int) or step < 0 or step > 200):
        raise ValueError("step must be a step index")
    step_title = note.get("step_title") or ""
    if not isinstance(step_title, str) or len(step_title) > 300:
        raise ValueError("step_title must be short text")
    follows = note.get("follows") or ""
    if not isinstance(follows, str) or (follows and not NOTE_ID_RE.fullmatch(follows)):
        raise ValueError("follows must name a note id")
    notes = load_notes(directory)
    if len(notes) >= MAX_NOTES:
        raise ValueError(f"at most {MAX_NOTES} notes are kept per package")
    numbers = [int(match.group(0)[5:]) for item in notes if (match := NOTE_ID_RE.fullmatch(item["id"]))]
    stored = {
        "id": f"note-{max(numbers, default=0) + 1:03d}",
        "anchor": anchor,
        "quote": quote.strip(),
        "message": message.strip(),
        "latex": latex.strip(),
        "revises": revises.strip(),
        "widget": widget,
        "step": step,
        "step_title": step_title.strip(),
        "follows": follows,
        "created_at": common.utc_now(),
        "addressed_run": None,
        "outcome": "",
    }
    notes.append(stored)
    write_notes(directory, notes)
    return stored


def remove_note(directory: Path, note_id: str) -> bool:
    """Remove a note and any quick explanation that answered it."""
    notes = load_notes(directory)
    kept = [note for note in notes if note["id"] != note_id]
    if len(kept) == len(notes):
        return False
    write_notes(directory, kept)
    annotations = common.load_json(directory / ANNOTATIONS_NAME)
    if isinstance(annotations, dict) and isinstance(annotations.get("explanations"), list):
        remaining = [entry for entry in annotations["explanations"] if not (isinstance(entry, dict) and entry.get("note") == note_id)]
        if len(remaining) != len(annotations["explanations"]):
            annotations["explanations"] = remaining
            common.write_json(directory / ANNOTATIONS_NAME, annotations)
    return True


def load_explanations(directory: Path) -> list:
    annotations = common.load_json(directory / ANNOTATIONS_NAME)
    if not isinstance(annotations, dict):
        return []
    explanations = annotations.get("explanations")
    return explanations if isinstance(explanations, list) else []


def mark_notes_addressed(directory: Path, note_ids: list[str], run_name: str, outcome: str = "") -> None:
    notes = load_notes(directory)
    wanted = set(note_ids)
    for note in notes:
        if note["id"] in wanted:
            note["addressed_run"] = run_name
            if outcome:
                note["outcome"] = outcome[:500]
    write_notes(directory, notes)


def find_note(directory: Path, note_id: str) -> dict | None:
    return next((note for note in load_notes(directory) if note["id"] == note_id), None)


def open_notes(directory: Path) -> list[dict]:
    return [note for note in load_notes(directory) if not note.get("addressed_run")]


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
