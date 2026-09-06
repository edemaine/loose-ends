"""Source discovery, storage, and synchronized updates for visualization packages.

Command modules prepare model inputs and results; this module owns package
updates and their locks. On-disk names and versions live in visualization_contract.

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

from contextlib import contextmanager
from dataclasses import dataclass
import errno
from functools import wraps
import hashlib
import inspect
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import threading
import time

import open_problem_common as common
import paper_document
from visualization_contract import (
    DIRECTORY_NAME,
    MANIFEST_NAME,
    ANNOTATIONS_NAME,
    WIDGETS_DIRECTORY,
    RUNS_DIRECTORY,
    WIDGET_MANIFEST_NAME,
    WIDGET_ENTRY_NAME,
    WIDGET_REVIEW_NAME,
    NOTES_NAME,
    NOTE_ID_RE,
    MAX_NOTES,
    MAX_NOTE_TEXT,
    MAX_WIDGET_STATE_BYTES,
    RUN_RE,
    WIDGET_ID_RE,
    READER_FILES,
    MANIFEST_SCHEMA_VERSION,
    ANNOTATIONS_SCHEMA_VERSION,
    WIDGET_SCHEMA_VERSION,
    REVIEW_SCHEMA_VERSION,
    WIDGET_API_VERSION,
    DEFAULT_ANCHOR,
    NOTES_ANCHOR,
    OUTPUT_DIRECTORY,
    CRITIQUE_FILENAME,
    NOTES_SCHEMA_VERSION,
    widget_id,
)


_PACKAGE_LOCKS: dict[Path, threading.RLock] = {}
_PACKAGE_LOCKS_GUARD = threading.Lock()
_HELD_PACKAGE_LOCKS = threading.local()


@contextmanager
def package_lock(directory: Path):
    """Serialize package updates across threads and processes, reentrantly.

    Keep the lock file in place: unlinking it could give concurrent writers
    different lock files. Hold this only for local updates, never Codex runs.
    """
    directory = directory.resolve()
    with _PACKAGE_LOCKS_GUARD:
        thread_lock = _PACKAGE_LOCKS.setdefault(directory, threading.RLock())
    with thread_lock:
        held = getattr(_HELD_PACKAGE_LOCKS, "directories", None)
        if held is None:
            held = _HELD_PACKAGE_LOCKS.directories = set()
        if directory in held:
            yield
            return
        directory.mkdir(parents=True, exist_ok=True)
        with (directory / ".update.lock").open("a+b") as lock:
            if os.name == "nt":
                import msvcrt

                lock.seek(0, os.SEEK_END)
                if lock.tell() == 0:
                    lock.write(b"0")
                    lock.flush()
                while True:
                    try:
                        lock.seek(0)
                        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                        break
                    except OSError as exc:
                        if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                            raise
                        time.sleep(0.05)
                def unlock():
                    lock.seek(0)
                    msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                def unlock():
                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            held.add(directory)
            try:
                yield
            finally:
                held.remove(directory)
                unlock()


def locked_package(function):
    """Protect a read-modify-write operation whose first argument is a package."""
    signature = inspect.signature(function)
    parameter = next(iter(signature.parameters))

    @wraps(function)
    def locked(*args, **kwargs):
        directory = signature.bind(*args, **kwargs).arguments[parameter]
        with package_lock(directory):
            return function(*args, **kwargs)
    return locked


@dataclass(frozen=True)
class SourceRef:
    kind: str  # "draft" or "paper"
    directory: Path  # where the package lives
    latex_directory: Path
    title: str | None
    authors: tuple[str, ...]
    label: str

    @property
    def package(self) -> Path:
        return package_directory(self.directory)


def source_from_path(value: Path) -> SourceRef:
    directory = value.expanduser().resolve()
    if not directory.is_dir():
        raise common.CodexError(f"visualization source must be a directory: {value}")
    if re.fullmatch(r"draft-([0-9]{3,})", directory.name) and (directory / "main.tex").is_file():
        manifest = common.load_json(directory / "manifest.json")
        manifest = manifest if isinstance(manifest, dict) else {}
        result = common.load_json(directory / "paper-result.json")
        result = result if isinstance(result, dict) else {}
        title = result.get("title") or manifest.get("title") or None
        authors = manifest.get("authors") if isinstance(manifest.get("authors"), list) else []
        return SourceRef(
            "draft", directory, directory,
            title if isinstance(title, str) else None,
            tuple(str(author) for author in authors),
            f"{directory.parent.name}/{directory.name}",
        )
    if (directory / "source").is_dir() and (directory / "metadata.json").is_file():
        metadata = common.load_json(directory / "metadata.json")
        metadata = metadata if isinstance(metadata, dict) else {}
        authors = metadata.get("authors") if isinstance(metadata.get("authors"), list) else []
        return SourceRef(
            "paper", directory, directory / "source",
            metadata.get("title") if isinstance(metadata.get("title"), str) else None,
            tuple(str(author) for author in authors),
            directory.name,
        )
    raise common.CodexError(
        "visualization source must be a manuscript draft-NNN directory or a "
        f"paper directory with source/ and metadata.json: {value}"
    )


def ensure_document(source: SourceRef, *, rebuild: bool = False) -> tuple[dict, dict]:
    """Return (document, manifest), converting the source when needed."""
    package = source.package
    with package_lock(package):
        manifest = load_manifest(package)
        document = load_document(package)
        if manifest is not None and document is not None and not rebuild:
            return document, manifest
        if document is not None and not rebuild:
            manifest = new_manifest(document, source=_source_record(source))
            write_manifest(package, manifest)
            return document, manifest
        try:
            document = paper_document.build_document(
                source.latex_directory, package,
                title=source.title, authors=source.authors or None,
                source_kind=source.kind, source_path=str(source.directory),
            )
        except paper_document.DocumentError as exc:
            raise common.CodexError(f"could not convert {source.label}: {exc}") from exc
        if manifest is None:
            manifest = new_manifest(document, source=_source_record(source))
        else:
            previous = manifest.get("document", {}).get("digest")
            manifest["document"] = {
                "digest": document["source"]["digest"],
                "built_at": common.utc_now(),
                "warnings": document.get("warnings", []),
            }
            if previous and previous != document["source"]["digest"]:
                manifest["stale_annotations"] = True
        write_manifest(package, manifest)
        return document, manifest


def _source_record(source: SourceRef) -> dict:
    return {
        "kind": source.kind,
        "path": str(source.directory),
        "label": source.label,
        "title": source.title,
    }




def package_key(directory: Path) -> str:
    """Return a URL-safe opaque identity for one package."""
    return hashlib.sha256(str(directory.resolve()).encode("utf-8")).hexdigest()[:24]


def package_directory(source_directory: Path) -> Path:
    return source_directory / DIRECTORY_NAME


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


def install_run(
    package: Path,
    manifest: dict,
    anchors: list[str],
    generated_workspace: Path,
    generated_result: dict,
    review_workspace: Path | None,
    review_result: dict | None,
    *,
    provenance: dict,
    document_digest: str,
) -> Path:
    """Install validated run artifacts and merge live state under the package lock."""
    with package_lock(package):
        manifest = load_manifest(package) or manifest
        number = next_run_number(package)
        run_name = f"run-{number:03d}"
        runs = package / RUNS_DIRECTORY
        runs.mkdir(exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".visualization-install-", dir=package))
        now = common.utc_now()
        try:
            run_directory = staging / run_name
            run_directory.mkdir()
            output = generated_workspace / OUTPUT_DIRECTORY
            shutil.copyfile(generated_workspace / "agent-result.json", run_directory / "agent-result.json")
            for name in ("events.jsonl", "run.log"):
                if (generated_workspace / name).is_file():
                    shutil.copyfile(generated_workspace / name, run_directory / name)
            for archive in sorted(generated_workspace.glob("review-before-repair-*")):
                shutil.copytree(archive, run_directory / archive.name)
            widget_reviews: dict[str, dict] = {}
            if review_workspace is not None and review_result is not None:
                common.write_json(run_directory / "review-result.json", review_result)
                shutil.copyfile(review_workspace / CRITIQUE_FILENAME, run_directory / "critique.md")
                for name, target in (("events.jsonl", "review-events.jsonl"), ("run.log", "review-run.log")):
                    if (review_workspace / name).is_file():
                        shutil.copyfile(review_workspace / name, run_directory / target)
                for review in review_result.get("widget_reviews", []):
                    if isinstance(review, dict) and isinstance(review.get("id"), str):
                        widget_reviews[review["id"]] = review
            # Widgets: stage new directories, remember old ones for replacement.
            new_widgets: list[dict] = []
            for widget in generated_result.get("widgets", []):
                widget_id = widget["id"]
                target = staging / WIDGETS_DIRECTORY / widget_id
                shutil.copytree(output / WIDGETS_DIRECTORY / widget_id, target)
                if widget_id in widget_reviews:
                    common.write_json(target / WIDGET_REVIEW_NAME, widget_reviews[widget_id])
                stamp_widget_files(target, document_digest, run_name)
                new_widgets.append({
                    "id": widget_id,
                    "anchor": widget["anchor"],
                    "kind": widget["kind"],
                    "title": widget["title"],
                    "summary": widget["summary"],
                    "limitations": widget.get("limitations", []),
                    "entry": WIDGET_ENTRY_NAME,
                    "steps": (common.load_json(target / WIDGET_MANIFEST_NAME) or {}).get("steps", []),
                    "examples": (common.load_json(target / WIDGET_MANIFEST_NAME) or {}).get("examples", []),
                    "run": run_name,
                    "generated_at": now,
                    "model": provenance.get("requested_model"),
                })
            annotations_source = output / ANNOTATIONS_NAME
            staged_annotations = staging / ANNOTATIONS_NAME
            if annotations_source.is_file():
                live = common.load_json(package / ANNOTATIONS_NAME)
                generated = common.read_json(annotations_source, description="generated annotations")
                merged = merge_live_annotations(
                    live if isinstance(live, dict) else None,
                    generated,
                    addressed=[n for n in generated_result.get("notes_addressed", []) if isinstance(n, str)],
                )
                merged["schema_version"] = ANNOTATIONS_SCHEMA_VERSION
                merged["document_digest"] = document_digest
                common.write_json(staged_annotations, merged)
            # Move everything into place.
            os.replace(run_directory, runs / run_name)
            replaced = runs / run_name / "replaced"
            for widget in new_widgets:
                destination = package / WIDGETS_DIRECTORY / widget["id"]
                destination.parent.mkdir(exist_ok=True)
                if destination.exists():
                    replaced.mkdir(exist_ok=True)
                    os.replace(destination, replaced / widget["id"])
                os.replace(staging / WIDGETS_DIRECTORY / widget["id"], destination)
            if staged_annotations.is_file():
                destination = package / ANNOTATIONS_NAME
                if destination.exists():
                    replaced.mkdir(exist_ok=True)
                    shutil.copyfile(destination, replaced / ANNOTATIONS_NAME)
                os.replace(staged_annotations, destination)
                manifest["annotations"] = ANNOTATIONS_NAME
                manifest.pop("stale_annotations", None)
                if review_result is not None:
                    manifest["annotations_review"] = review_result.get("annotations_review")
            kept = [w for w in manifest.get("widgets", []) if isinstance(w, dict) and w.get("id") not in {n["id"] for n in new_widgets}]
            manifest["widgets"] = kept + new_widgets
            manifest.setdefault("runs", []).append({
                **provenance,
                "name": run_name,
                "generated_at": now,
                "anchors": anchors,
                "status": generated_result.get("status"),
                "summary": generated_result.get("summary", ""),
                "widgets": [widget["id"] for widget in new_widgets],
                "annotations_updated": bool(generated_result.get("annotations_updated")),
                "repair_rounds": int(generated_result.get("repair_rounds", 0)),
                "review_summary": (review_result or {}).get("summary", ""),
                "warnings": list(generated_result.get("warnings", [])) + list((review_result or {}).get("warnings", [])),
            })
            addressed = [note_id for note_id in generated_result.get("notes_addressed", []) if isinstance(note_id, str)]
            if addressed:
                mark_notes_addressed(package, addressed, run_name)
                manifest["runs"][-1]["notes_addressed"] = addressed
            manifest["generated_at"] = now
            write_manifest(package, manifest)
        except (OSError, ValueError, KeyError) as exc:
            raise common.CodexError(f"could not install visualization run; staging preserved at {staging}: {exc}") from exc
        shutil.rmtree(staging, ignore_errors=True)
        installed = runs / run_name
        common.report_artifacts(path for path in package.rglob("*") if path.is_file() and path.name != ".update.lock" and RUNS_DIRECTORY not in path.relative_to(package).parts[:1])
        common.report_artifacts(path for path in installed.rglob("*") if path.is_file())
        return installed


@locked_package
def install_quick_fix(
    package: Path, widget_id: str, edited_directory: Path, *,
    note_id: str, summary: str, document_digest: str, run_name: str,
) -> None:
    """Archive and install a checked widget, invalidate its review, and acknowledge its note."""
    widget_directory = package / WIDGETS_DIRECTORY / widget_id
    archive = package / RUNS_DIRECTORY / "quick-fixes" / f"{widget_id}-{common.utc_now().replace(':', '').replace('+', 'Z')}"
    archive.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(widget_directory, archive)
    previous_review = common.load_json(widget_directory / WIDGET_REVIEW_NAME)
    for path in edited_directory.rglob("*"):
        if path.is_file():
            target = widget_directory / path.relative_to(edited_directory)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
    # Keep the package manifest's record of this widget in step with widget.json.
    updated_manifest = common.load_json(widget_directory / WIDGET_MANIFEST_NAME) or {}
    package_manifest = load_manifest(package)
    if package_manifest is not None:
        for entry in package_manifest.get("widgets", []):
            if isinstance(entry, dict) and entry.get("id") == widget_id:
                for field in ("title", "summary", "steps", "examples", "limitations"):
                    if field in updated_manifest:
                        entry[field] = updated_manifest[field]
                entry["quick_fixes"] = int(entry.get("quick_fixes") or 0) + 1
        write_manifest(package, package_manifest)
    common.write_json(widget_directory / WIDGET_REVIEW_NAME, {
        "schema_version": REVIEW_SCHEMA_VERSION,
        "document_digest": document_digest,
        "fidelity": "unreviewed",
        "interaction_quality": "unreviewed",
        "summary": f"Quick fix applied without review: {summary}",
        "findings": [],
        "blocking_gaps": [],
        "provenance": "quick",
        "previous_review": previous_review if isinstance(previous_review, dict) else None,
    })
    mark_notes_addressed(package, [note_id], run_name, outcome=summary)


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
    common.write_json(directory / NOTES_NAME, {"schema_version": NOTES_SCHEMA_VERSION, "notes": notes})


@locked_package
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
    example = note.get("example", "")
    if not isinstance(example, str) or (example and not WIDGET_ID_RE.fullmatch(example)):
        raise ValueError("example must be an example id")
    widget_state_error = note.get("widget_state_error", "")
    if not isinstance(widget_state_error, str) or len(widget_state_error) > 300:
        raise ValueError("widget_state_error must be short text")
    widget_state = note.get("widget_state")
    if not widget and (example or widget_state is not None or widget_state_error):
        raise ValueError("example and widget state require a widget")
    if widget_state is not None:
        if not isinstance(widget_state, dict):
            raise ValueError("widget_state must be a JSON object")
        try:
            encoded = json.dumps(widget_state, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
            if len(encoded.encode("utf-8")) > MAX_WIDGET_STATE_BYTES:
                raise ValueError("widget_state exceeds 32 KiB")
            widget_state = json.loads(encoded)
        except (TypeError, ValueError, RecursionError) as error:
            raise ValueError(f"invalid widget_state: {error}") from error
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
        "example": example,
        "widget_state": widget_state,
        "widget_state_error": widget_state_error,
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


@locked_package
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


@locked_package
def store_explanation(directory: Path, note: dict, explanation: dict, *, run_name: str) -> None:
    """Save a prepared explanation and acknowledge its note in one package update."""
    annotations = common.load_json(directory / ANNOTATIONS_NAME)
    if not isinstance(annotations, dict):
        annotations = {"glossary": [], "proof_outlines": {}}
    explanations = annotations.setdefault("explanations", [])
    if not isinstance(explanations, list):
        explanations = annotations["explanations"] = []
    superseded = {explanation["id"], note.get("revises") or ""}
    explanations[:] = [entry for entry in explanations if entry.get("id") not in superseded]
    explanations.append(explanation)
    common.write_json(directory / ANNOTATIONS_NAME, annotations)
    manifest = load_manifest(directory)
    if manifest is not None and not manifest.get("annotations"):
        manifest["annotations"] = ANNOTATIONS_NAME
        write_manifest(directory, manifest)
    mark_notes_addressed(directory, [note["id"]], run_name, outcome=explanation["title"])


def load_explanations(directory: Path) -> list:
    annotations = common.load_json(directory / ANNOTATIONS_NAME)
    if not isinstance(annotations, dict):
        return []
    explanations = annotations.get("explanations")
    return explanations if isinstance(explanations, list) else []


@locked_package
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
