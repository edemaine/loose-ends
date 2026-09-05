#!/usr/bin/env python3
"""Build reading aids for a paper: converted document, annotations, widgets.

Usage examples:

    python src/visualize_paper.py manuscripts/NAME/draft-002
        Convert the draft (if needed), then ask Codex for the default aids:
        definition popovers, the main-result widget, and proof outlines.

    python src/visualize_paper.py manuscripts/NAME/draft-002 \\
        --anchor lem:tiling-completion --anchor proof-2
        Add a statement widget and a step-by-step proof widget.

    python src/visualize_paper.py manuscripts/NAME/draft-002 --document-only
        Only (re)build the converted document; no Codex run.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
import tempfile

import codex_cli
import open_problem_common as common
import paper_document
from validation import visualization as visualization_validation
from validation import visualization_review as review_validation
import visualizations
import write_paper


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROMPT_PATH = PROJECT_ROOT / "prompts" / "visualize-paper.md"
DEFAULT_SCHEMA_PATH = PROJECT_ROOT / "schemas" / "visualization-result.schema.json"
DEFAULT_REVIEW_PROMPT_PATH = PROJECT_ROOT / "prompts" / "review-visualization.md"
DEFAULT_REVIEW_SCHEMA_PATH = PROJECT_ROOT / "schemas" / "visualization-review.schema.json"
WIDGET_API_PATH = PROJECT_ROOT / "prompts" / "visualization-widget-api.md"
READER_DIRECTORY = PROJECT_ROOT / "src" / "workbench_web" / "reader"
DEFAULT_ANCHOR = visualizations.DEFAULT_ANCHOR
NOTES_ANCHOR = visualizations.NOTES_ANCHOR
PSEUDO_ANCHORS = {DEFAULT_ANCHOR, NOTES_ANCHOR}
TRANSIENT_FAILURE_MARKERS = ("at capacity", "rate limit", "overloaded", "temporarily unavailable")
MAX_TRANSIENT_RETRIES = 2
DEFAULT_REPAIR_ROUNDS = 1
NOTES_ONLY_REASONING_EFFORT = "medium"


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
        return visualizations.package_directory(self.directory)


@dataclass(frozen=True)
class RunOutcome:
    source: SourceRef
    run_directory: Path
    widgets: list[str]
    annotations_updated: bool
    review_summary: str


def source_from_path(value: Path) -> SourceRef:
    directory = value.expanduser().resolve()
    if not directory.is_dir():
        raise common.CodexError(f"visualization source must be a directory: {value}")
    if write_paper.DRAFT_RE.fullmatch(directory.name) and (directory / "main.tex").is_file():
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
    manifest = visualizations.load_manifest(package)
    document = visualizations.load_document(package)
    if manifest is not None and document is not None and not rebuild:
        return document, manifest
    if document is not None and not rebuild:
        manifest = visualizations.new_manifest(document, source=_source_record(source))
        visualizations.write_manifest(package, manifest)
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
        manifest = visualizations.new_manifest(document, source=_source_record(source))
    else:
        previous = manifest.get("document", {}).get("digest")
        manifest["document"] = {
            "digest": document["source"]["digest"],
            "built_at": common.utc_now(),
            "warnings": document.get("warnings", []),
        }
        if previous and previous != document["source"]["digest"]:
            manifest["stale_annotations"] = True
    visualizations.write_manifest(package, manifest)
    return document, manifest


def _source_record(source: SourceRef) -> dict:
    return {
        "kind": source.kind,
        "path": str(source.directory),
        "label": source.label,
        "title": source.title,
    }


def resolve_anchors(document: dict, anchors: list[str]) -> list[str]:
    described = visualizations.anchor_descriptions(document)
    resolved: list[str] = []
    for anchor in anchors:
        if anchor in PSEUDO_ANCHORS:
            resolved.append(anchor)
            continue
        if anchor not in described:
            raise common.CodexError(
                f"anchor {anchor!r} is not a statement or proof of the document; "
                f"see document.json for valid ids (or use `{NOTES_ANCHOR}` to address reader notes)"
            )
        resolved.append(anchor)
    return list(dict.fromkeys(resolved)) or [DEFAULT_ANCHOR]


def _stage_common_inputs(workspace: Path, source: SourceRef, document: dict) -> Path:
    inputs = workspace / "inputs"
    inputs.mkdir(parents=True, exist_ok=False)
    package = source.package
    document_input = inputs / "document"
    document_input.mkdir()
    for name in (paper_document.DOCUMENT_HTML, paper_document.DOCUMENT_JSON):
        shutil.copyfile(package / name, document_input / name)
    figures = package / paper_document.FIGURES_DIRECTORY
    if figures.is_dir():
        shutil.copytree(figures, document_input / paper_document.FIGURES_DIRECTORY)
    source_input = inputs / "source"
    source_input.mkdir()
    for path in source.latex_directory.iterdir():
        if path.name.startswith(".") or path.name == visualizations.DIRECTORY_NAME:
            continue
        if path.is_dir():
            if path.name in {"figures", "code"} or source.kind == "paper":
                shutil.copytree(path, source_input / path.name)
        elif path.suffix.lower() in {".tex", ".bib", ".bbl", ".sty", ".cls", ".pdf"} or path.name == "main.pdf":
            shutil.copyfile(path, source_input / path.name)
    reader_input = inputs / "reader"
    reader_input.mkdir()
    for name in sorted(visualizations.READER_FILES):
        shutil.copyfile(READER_DIRECTORY / name, reader_input / name)
    shutil.copyfile(WIDGET_API_PATH, reader_input / "WIDGET-API.md")
    return inputs


def _stage_existing(inputs: Path, source: SourceRef, manifest: dict) -> None:
    package = source.package
    existing = inputs / "existing"
    annotations = package / visualizations.ANNOTATIONS_NAME
    widgets = package / visualizations.WIDGETS_DIRECTORY
    if not annotations.is_file() and not widgets.is_dir():
        return
    existing.mkdir()
    if annotations.is_file():
        shutil.copyfile(annotations, existing / visualizations.ANNOTATIONS_NAME)
    if widgets.is_dir():
        shutil.copytree(widgets, existing / visualizations.WIDGETS_DIRECTORY)
    common.write_json(existing / visualizations.MANIFEST_NAME, manifest)


def _reader_notes(source: SourceRef, document: dict) -> list[dict]:
    """Open reader notes with the text of the paragraph they point at."""
    text = {paragraph["id"]: paragraph.get("text", "") for paragraph in document.get("paragraphs", [])}
    described = visualizations.anchor_descriptions(document)
    notes = []
    for note in visualizations.open_notes(source.package):
        anchor = note.get("anchor", "")
        container = ""
        for proof in document.get("proofs", []):
            if anchor in proof.get("paragraphs", []):
                container = proof["id"]
        for statement in document.get("statements", []):
            if anchor in statement.get("paragraphs", []) or anchor == statement["id"]:
                container = statement["id"]
        if note.get("widget"):
            container = anchor
        previous = None
        if note.get("revises"):
            previous = next((entry for entry in visualizations.load_explanations(source.package) if isinstance(entry, dict) and entry.get("id") == note["revises"]), None)
        notes.append({
            "id": note["id"],
            "anchor": anchor,
            "container": container,
            "container_label": described.get(container, {}).get("label", ""),
            "quote": note.get("quote", ""),
            "latex": note.get("latex", ""),
            "message": note.get("message", ""),
            "revises": note.get("revises", ""),
            "widget": note.get("widget", ""),
            "step": note.get("step"),
            "step_title": note.get("step_title", ""),
            "follows": note.get("follows", ""),
            "previous_answer": {"title": previous.get("title", ""), "text": previous.get("text", "")} if previous else None,
            "paragraph_text": text.get(anchor, ""),
        })
    return notes


def _request(document: dict, anchors: list[str], manifest: dict, notes: list[dict] | None = None) -> dict:
    described = visualizations.anchor_descriptions(document)
    wants_default = DEFAULT_ANCHOR in anchors
    wants_notes = NOTES_ANCHOR in anchors
    return {
        "reader_notes": notes or [],
        "notes_only": wants_notes and not wants_default and not [a for a in anchors if a not in PSEUDO_ANCHORS],
        "anchors": anchors,
        "annotations": wants_default,
        "main_result_widget": wants_default,
        "proof_outlines": wants_default,
        "widgets": [
            {**described[anchor], "widget_id": visualizations.widget_id(anchor)}
            for anchor in anchors if anchor not in PSEUDO_ANCHORS
        ],
        "existing_widgets": [
            {"id": widget.get("id"), "anchor": widget.get("anchor"), "kind": widget.get("kind"), "title": widget.get("title")}
            for widget in manifest.get("widgets", []) if isinstance(widget, dict)
        ],
        "existing_annotations": manifest.get("annotations") is not None,
    }


def _render_prompt(template: str, document: dict, request: dict) -> str:
    lines = [template.rstrip(), "", "# This run", ""]
    if request["annotations"]:
        lines.append(
            "- Write `output/annotations.json`: the glossary, `main_result`, and "
            "proof outlines (2 to 5 steps) for the proof of the main result and "
            "for the proofs of the statements that proof directly cites."
        )
        lines.append(
            "- Write the main-result widget: a statement widget anchored to the "
            "main result you choose, with a playground when the mathematics allows."
        )
    for widget in request["widgets"]:
        if widget["kind"] == "proof":
            lines.append(
                f"- Write a proof widget for `{widget['id']}` ({widget['label']}, "
                f"proof of `{widget.get('of')}`), directory `output/widgets/{widget['widget_id']}/`, "
                f"with steps over paragraphs {', '.join(widget['paragraphs'])}."
            )
        else:
            lines.append(
                f"- Write a statement widget for `{widget['id']}` ({widget['label']}"
                f"{': ' + widget['title'] if widget.get('title') else ''}), "
                f"directory `output/widgets/{widget['widget_id']}/`."
            )
    if request.get("reader_notes"):
        lines.append("")
        lines.append(
            "The reader marked these passages as unclear (also in "
            "`inputs/reader-notes.json`). Address every one: add a "
            "phrase-level proof step whose picture explains the passage, a "
            "glossary entry, or a short clarifying note in the widget, and "
            "list the ids you addressed in `notes_addressed`. A note inside a "
            "proof that has no widget yet asks for a proof widget on that proof."
        )
        for note in request["reader_notes"]:
            where = f" in {note['container_label']} (`{note['container']}`)" if note.get("container") else ""
            message = f' Reader says: "{note["message"]}"' if note.get("message") else ""
            follow_up = ""
            if note.get("previous_answer"):
                follow_up = f" This follows up an earlier explanation (`{note['revises']}`, \"{note['previous_answer']['title']}\") that did not satisfy the reader; replace it."
            if note.get("widget"):
                step = f" at step {note['step'] + 1} (\"{note.get('step_title', '')}\")" if isinstance(note.get("step"), int) else ""
                lines.append(f"- `{note['id']}` about widget `{note['widget']}`{step} (anchored at `{note['anchor']}`): \"{note['message'] or note['quote']}\". Fix the widget accordingly when regenerating it.")
                continue
            lines.append(f"- `{note['id']}` at `{note['anchor']}`{where}: \"{note['quote']}\".{message}{follow_up}")
    if request["existing_widgets"]:
        lines.append("")
        lines.append("Existing widgets (under `inputs/existing/`): " + ", ".join(
            f"`{item['id']}` for `{item['anchor']}`" for item in request["existing_widgets"]
        ) + ". Do not recreate them unless requested above.")
    lines.append("")
    lines.append(f"The paper is *{document.get('title', '')}*. Its statements are:")
    for statement in document.get("statements", []):
        proofs = ", ".join(statement.get("proofs", [])) or "no proof environment"
        lines.append(f"- `{statement['id']}`: {statement['label']}{' (' + statement['title'] + ')' if statement.get('title') else ''}; proofs: {proofs}")
    lines.append("")
    return "\n".join(lines) + "\n"


def _expectations(document: dict, anchors: list[str], notes: list[dict] | None = None) -> dict:
    return {
        "anchors": anchors,
        "annotations_required": DEFAULT_ANCHOR in anchors or NOTES_ANCHOR in anchors,
        "document_ids": paper_document.anchor_ids(document),
        "proof_paragraphs": {proof["id"]: list(proof.get("paragraphs", [])) for proof in document.get("proofs", [])},
        "paragraph_text": {paragraph["id"]: paragraph.get("text", "") for paragraph in document.get("paragraphs", [])},
        "note_ids": [note["id"] for note in notes or []],
        "note_containers": sorted({note["container"] for note in notes or [] if note.get("container")}),
    }


def _review_generated(
    source: SourceRef,
    document: dict,
    generated_workspace: Path,
    generated_result: dict,
    *,
    codex: str,
    prompt: str,
    schema_path: Path,
    options: codex_cli.ModelOptions,
    web_search: str,
) -> tuple[Path, dict]:
    workspace = Path(tempfile.mkdtemp(prefix=".visualization-review-run-", dir=source.directory)).resolve()
    try:
        inputs = _stage_common_inputs(workspace, source, document)
        generated = inputs / "generated"
        shutil.copytree(generated_workspace / visualization_validation.OUTPUT_DIRECTORY, generated)
        shutil.copyfile(generated_workspace / "agent-result.json", generated / "agent-result.json")
        if (generated_workspace / "inputs" / "reader-notes.json").is_file():
            shutil.copyfile(generated_workspace / "inputs" / "reader-notes.json", inputs / "reader-notes.json")
        codex_cli.grant_sandbox_read_access(inputs)
        widget_ids = [widget["id"] for widget in generated_result.get("widgets", []) if isinstance(widget, dict)]
        report = codex_cli.run_validated_codex(
            codex=codex,
            workspace=workspace,
            prompt=prompt,
            schema_path=schema_path,
            validator=codex_cli.OutputValidator(
                Path(review_validation.__file__).resolve(),
                review_validation.validate,
                {"widget_ids": widget_ids, "annotations_present": bool(generated_result.get("annotations_updated"))},
            ),
            options=options,
            web_search=web_search,
        )
        return workspace, codex_cli.validated_result(report)
    except (common.CodexError, OSError, ValueError) as exc:
        raise common.CodexError(common.preserved_workspace_message(exc, workspace)) from exc


def _archive_review(workspace: Path, review_workspace: Path, review_result: dict, round_number: int) -> None:
    """Keep a superseded review beside the generated files for provenance."""
    archive = workspace / f"review-before-repair-{round_number}"
    archive.mkdir(exist_ok=True)
    common.write_json(archive / "review-result.json", review_result)
    for name in (review_validation.CRITIQUE_FILENAME, "events.jsonl", "run.log"):
        if (review_workspace / name).is_file():
            shutil.copyfile(review_workspace / name, archive / name)
    common.cleanup_workspace(review_workspace, installed_log=archive / "run.log")


def _install(
    source: SourceRef,
    manifest: dict,
    anchors: list[str],
    generated_workspace: Path,
    generated_result: dict,
    review_workspace: Path | None,
    review_result: dict | None,
    *,
    options: codex_cli.ModelOptions,
    review_options: codex_cli.ModelOptions,
    config_digest: str,
    review_config_digest: str,
    codex_version: str,
) -> Path:
    package = source.package
    number = visualizations.next_run_number(package)
    run_name = f"run-{number:03d}"
    runs = package / visualizations.RUNS_DIRECTORY
    runs.mkdir(exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".visualization-install-", dir=package))
    now = common.utc_now()
    try:
        run_directory = staging / run_name
        run_directory.mkdir()
        output = generated_workspace / visualization_validation.OUTPUT_DIRECTORY
        shutil.copyfile(generated_workspace / "agent-result.json", run_directory / "agent-result.json")
        for name in ("events.jsonl", "run.log"):
            if (generated_workspace / name).is_file():
                shutil.copyfile(generated_workspace / name, run_directory / name)
        for archive in sorted(generated_workspace.glob("review-before-repair-*")):
            shutil.copytree(archive, run_directory / archive.name)
        widget_reviews: dict[str, dict] = {}
        if review_workspace is not None and review_result is not None:
            common.write_json(run_directory / "review-result.json", review_result)
            shutil.copyfile(review_workspace / review_validation.CRITIQUE_FILENAME, run_directory / "critique.md")
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
            target = staging / visualizations.WIDGETS_DIRECTORY / widget_id
            shutil.copytree(output / visualizations.WIDGETS_DIRECTORY / widget_id, target)
            if widget_id in widget_reviews:
                common.write_json(target / visualizations.WIDGET_REVIEW_NAME, widget_reviews[widget_id])
            new_widgets.append({
                "id": widget_id,
                "anchor": widget["anchor"],
                "kind": widget["kind"],
                "title": widget["title"],
                "summary": widget["summary"],
                "limitations": widget.get("limitations", []),
                "entry": visualizations.WIDGET_ENTRY_NAME,
                "steps": (common.load_json(target / visualizations.WIDGET_MANIFEST_NAME) or {}).get("steps", []),
                "examples": (common.load_json(target / visualizations.WIDGET_MANIFEST_NAME) or {}).get("examples", []),
                "run": run_name,
                "generated_at": now,
                "model": options.model,
            })
        annotations_source = output / visualizations.ANNOTATIONS_NAME
        # Move everything into place.
        os.replace(run_directory, runs / run_name)
        replaced = runs / run_name / "replaced"
        for widget in new_widgets:
            destination = package / visualizations.WIDGETS_DIRECTORY / widget["id"]
            destination.parent.mkdir(exist_ok=True)
            if destination.exists():
                replaced.mkdir(exist_ok=True)
                os.replace(destination, replaced / widget["id"])
            os.replace(staging / visualizations.WIDGETS_DIRECTORY / widget["id"], destination)
            visualizations.stamp_widget_files(destination, document.get("source", {}).get("digest", ""), run_name)
        if annotations_source.is_file():
            destination = package / visualizations.ANNOTATIONS_NAME
            live = common.load_json(destination) if destination.exists() else None
            if destination.exists():
                replaced.mkdir(exist_ok=True)
                shutil.copyfile(destination, replaced / visualizations.ANNOTATIONS_NAME)
            generated = common.read_json(annotations_source, description="generated annotations")
            merged = visualizations.merge_live_annotations(
                live if isinstance(live, dict) else None,
                generated,
                addressed=[n for n in generated_result.get("notes_addressed", []) if isinstance(n, str)],
            )
            merged["schema_version"] = visualizations.ANNOTATIONS_SCHEMA_VERSION
            merged["document_digest"] = document.get("source", {}).get("digest", "")
            common.write_json(destination, merged)
            manifest["annotations"] = visualizations.ANNOTATIONS_NAME
            manifest.pop("stale_annotations", None)
            if review_result is not None:
                manifest["annotations_review"] = review_result.get("annotations_review")
        kept = [w for w in manifest.get("widgets", []) if isinstance(w, dict) and w.get("id") not in {n["id"] for n in new_widgets}]
        manifest["widgets"] = kept + new_widgets
        manifest.setdefault("runs", []).append({
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
            "config_digest": config_digest,
            "review_config_digest": review_config_digest,
            "codex_version": codex_version,
            "requested_model": options.model,
            "requested_reasoning_effort": options.reasoning_effort,
            "requested_fast_mode": options.fast,
            "review_model": review_options.model,
            "review_reasoning_effort": review_options.reasoning_effort,
            "review_fast_mode": review_options.fast,
        })
        addressed = [note_id for note_id in generated_result.get("notes_addressed", []) if isinstance(note_id, str)]
        if addressed:
            visualizations.mark_notes_addressed(package, addressed, run_name)
            manifest["runs"][-1]["notes_addressed"] = addressed
        manifest["generated_at"] = now
        visualizations.write_manifest(package, manifest)
    except (OSError, ValueError, KeyError) as exc:
        raise common.CodexError(f"could not install visualization run; staging preserved at {staging}: {exc}") from exc
    shutil.rmtree(staging, ignore_errors=True)
    installed = runs / run_name
    common.report_artifacts(path for path in package.rglob("*") if path.is_file() and visualizations.RUNS_DIRECTORY not in path.relative_to(package).parts[:1])
    common.report_artifacts(path for path in installed.rglob("*") if path.is_file())
    return installed


def transient_failure(workspace: Path) -> str | None:
    """Return the provider error when a Codex turn failed for a transient reason."""
    events = workspace / "events.jsonl"
    try:
        lines = events.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    for line in reversed(lines[-5:]):
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") not in {"error", "turn.failed"}:
            continue
        message = event.get("message") or (event.get("error") or {}).get("message") or ""
        if any(marker in message.lower() for marker in TRANSIENT_FAILURE_MARKERS):
            return str(message)
    return None


def needs_repair(review_result: dict) -> bool:
    """Return whether the critic found gaps a designer repair round should fix."""
    for review in review_result.get("widget_reviews", []):
        if not isinstance(review, dict):
            continue
        if review.get("fidelity") in {"major_gaps", "incorrect"}:
            return True
        if review.get("interaction_quality") in {"major_issues", "unusable"}:
            return True
        if review.get("blocking_gaps"):
            return True
    annotations = review_result.get("annotations_review") or {}
    return annotations.get("accuracy") == "major_issues"


def _repair_prompt(rendered: str, review_result: dict, critique: str) -> str:
    lines = [
        rendered.rstrip(),
        "",
        "# Repair round",
        "",
        "Your previous turn produced the files now under `output/` and "
        "`agent-result.json`. An independent reviewer audited them; its full "
        "critique follows. Fix every blocking gap and major finding in place, "
        "preserving everything that was judged correct, then update "
        "`agent-result.json`, rerun your scripted interaction test, and "
        "validate again. Prefer removing a fragile feature over keeping a "
        "broken one.",
        "",
    ]
    annotations = review_result.get("annotations_review") or {}
    if annotations.get("accuracy") not in {None, "not_applicable", "accurate"}:
        lines.append(f"Annotations ({annotations.get('accuracy')}):")
        lines.extend(f"- {item}" for item in annotations.get("findings", []))
        lines.append("")
    for review in review_result.get("widget_reviews", []):
        if not isinstance(review, dict):
            continue
        lines.append(
            f"Widget `{review.get('id')}`: fidelity {review.get('fidelity')}, "
            f"interaction {review.get('interaction_quality')}."
        )
        for gap in review.get("blocking_gaps", []):
            lines.append(f"- BLOCKING: {gap}")
        for finding in review.get("findings", []):
            lines.append(f"- {finding}")
        lines.append("")
    lines.append("Full critique:")
    lines.append("")
    lines.append(critique.strip())
    lines.append("")
    return "\n".join(lines)


def _resume_prompt(rendered: str) -> str:
    return (
        rendered.rstrip()
        + "\n\n# Resumed run\n\n"
        + "A previous turn on this task was interrupted by a provider error. "
        + "Files it already wrote may exist under `output/`. Inspect them, keep "
        + "what is correct, complete the remaining work, and finish normally.\n"
    )


def visualize(
    source: SourceRef,
    anchors: list[str],
    *,
    codex: str,
    codex_version: str,
    prompt: str,
    schema_path: Path,
    config_digest: str,
    options: codex_cli.ModelOptions,
    web_search: str,
    review: bool,
    review_prompt: str,
    review_schema_path: Path,
    review_config_digest: str,
    review_options: codex_cli.ModelOptions,
    review_web_search: str,
    rebuild_document: bool = False,
    repair_rounds: int | None = None,
) -> RunOutcome:
    document, manifest = ensure_document(source, rebuild=rebuild_document)
    anchors = resolve_anchors(document, anchors)
    notes_only = anchors == [NOTES_ANCHOR]
    if notes_only and not options.reasoning_effort:
        # Notes-only runs produce annotations, not widgets: medium effort is enough.
        options = codex_cli.ModelOptions(options.model, NOTES_ONLY_REASONING_EFFORT, options.fast)
    if repair_rounds is None:
        repair_rounds = 0 if notes_only else DEFAULT_REPAIR_ROUNDS
    workspace = Path(tempfile.mkdtemp(prefix=".visualize-run-", dir=source.directory)).resolve()
    review_workspace: Path | None = None
    review_result: dict | None = None
    try:
        inputs = _stage_common_inputs(workspace, source, document)
        _stage_existing(inputs, source, manifest)
        notes = _reader_notes(source, document)
        request = _request(document, anchors, manifest, notes)
        common.write_json(inputs / "request.json", request)
        common.write_json(inputs / "reader-notes.json", {"notes": notes})
        codex_cli.grant_sandbox_read_access(inputs)
        rendered_prompt = _render_prompt(prompt, document, request)
        validator = codex_cli.OutputValidator(
            Path(visualization_validation.__file__).resolve(),
            visualization_validation.validate,
            _expectations(document, anchors, notes),
        )
        retries = 0
        while True:
            try:
                report = codex_cli.run_validated_codex(
                    codex=codex,
                    workspace=workspace,
                    prompt=rendered_prompt,
                    schema_path=schema_path,
                    validator=validator,
                    options=options,
                    web_search=web_search,
                )
                break
            except common.CodexError as exc:
                reason = transient_failure(workspace)
                if reason is None or retries >= MAX_TRANSIENT_RETRIES:
                    raise
                retries += 1
                print(f"Codex failed transiently ({reason}); resuming, retry {retries}/{MAX_TRANSIENT_RETRIES}...")
                rendered_prompt = _resume_prompt(_render_prompt(prompt, document, request))
        generated_result = codex_cli.validated_result(report)
        repairs = 0
        while review and (generated_result.get("widgets") or generated_result.get("annotations_updated")):
            review_workspace, review_result = _review_generated(
                source, document, workspace, generated_result,
                codex=codex, prompt=review_prompt, schema_path=review_schema_path,
                options=review_options, web_search=review_web_search,
            )
            if repairs >= repair_rounds or not needs_repair(review_result):
                break
            repairs += 1
            print(f"Reviewer found blocking gaps; designer repair round {repairs}/{repair_rounds}...")
            critique = (review_workspace / review_validation.CRITIQUE_FILENAME).read_text(encoding="utf-8")
            _archive_review(workspace, review_workspace, review_result, repairs)
            report = codex_cli.run_validated_codex(
                codex=codex,
                workspace=workspace,
                prompt=_repair_prompt(_render_prompt(prompt, document, request), review_result, critique),
                schema_path=schema_path,
                validator=validator,
                options=options,
                web_search=web_search,
            )
            generated_result = codex_cli.validated_result(report)
            review_workspace, review_result = None, None
        generated_result["repair_rounds"] = repairs
        installed = _install(
            source, manifest, anchors, workspace, generated_result, review_workspace, review_result,
            options=options, review_options=review_options,
            config_digest=config_digest, review_config_digest=review_config_digest,
            codex_version=codex_version,
        )
    except (common.CodexError, OSError, ValueError) as exc:
        raise common.CodexError(common.preserved_workspace_message(exc, workspace)) from exc
    common.cleanup_workspace(workspace, installed_log=installed / "run.log")
    if review_workspace is not None:
        common.cleanup_workspace(review_workspace, installed_log=installed / "review-run.log")
    return RunOutcome(
        source, installed,
        [widget["id"] for widget in generated_result.get("widgets", [])],
        bool(generated_result.get("annotations_updated")),
        (review_result or {}).get("summary", ""),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="build reading aids (document, definitions, widgets) for a paper or manuscript draft",
    )
    parser.add_argument("sources", nargs="+", type=Path, help="manuscript draft-NNN or paper directories")
    parser.add_argument(
        "--anchor", action="append", default=[], metavar="ID",
        help="statement or proof id to visualize (repeatable); 'default' requests the glossary, main-result widget, and proof outlines",
    )
    parser.add_argument("--document-only", action="store_true", help="only convert the source; do not run Codex")
    parser.add_argument("--rebuild-document", action="store_true", help="reconvert the source even if a document exists")
    parser.add_argument("--skip-review", action="store_true", help="do not run the independent fidelity review")
    parser.add_argument(
        "--repair-rounds", type=int, default=None, metavar="N",
        help=f"designer repair rounds after a review with blocking gaps (default: {DEFAULT_REPAIR_ROUNDS}, or 0 for notes-only runs)",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--codex", default="codex")
    codex_cli.add_prompt_arguments(parser, default_template=DEFAULT_PROMPT_PATH, task="visualization designer")
    parser.add_argument("--schema", type=Path, default=DEFAULT_SCHEMA_PATH)
    codex_cli.add_model_arguments(parser)
    codex_cli.add_web_search_argument(parser, default="disabled")
    codex_cli.add_prompt_arguments(parser, default_template=DEFAULT_REVIEW_PROMPT_PATH, task="visualization reviewer", prefix="review")
    parser.add_argument("--review-schema", type=Path, default=DEFAULT_REVIEW_SCHEMA_PATH)
    codex_cli.add_model_arguments(parser, prefix="review")
    codex_cli.add_web_search_argument(parser, default="disabled", prefix="review")
    return parser


def _inherit_review_options(primary: codex_cli.ModelOptions, review: codex_cli.ModelOptions) -> codex_cli.ModelOptions:
    return codex_cli.ModelOptions(
        review.model or primary.model,
        review.reasoning_effort or primary.reasoning_effort,
        review.fast or primary.fast,
    )


def main(argv: list[str] | None = None) -> int:
    codex_cli.configure_utf8_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        sources = [source_from_path(path) for path in args.sources]
        anchors = list(dict.fromkeys(args.anchor)) or [DEFAULT_ANCHOR]
        if args.document_only:
            for source in sources:
                document, _manifest = ensure_document(source, rebuild=True)
                print(
                    f"Converted {source.label}: {len(document['sections'])} sections, "
                    f"{len(document['statements'])} statements, {len(document['proofs'])} proofs, "
                    f"{len(document['figures'])} figures, {len(document['warnings'])} warnings."
                )
                for warning in document["warnings"]:
                    print(f"  warning: {warning}")
            return 0
        prompt = codex_cli.with_user_prompt(
            args.prompt_template.expanduser().resolve().read_text(encoding="utf-8"),
            args.prompt, task="visualization designer",
        )
        review_prompt = codex_cli.with_user_prompt(
            args.review_prompt_template.expanduser().resolve().read_text(encoding="utf-8"),
            args.review_prompt, task="visualization reviewer", option_name="--review-prompt",
        )
        schema_path = args.schema.expanduser().resolve()
        review_schema_path = args.review_schema.expanduser().resolve()
        schema_text = schema_path.read_text(encoding="utf-8")
        review_schema_text = review_schema_path.read_text(encoding="utf-8")
        json.loads(schema_text)
        json.loads(review_schema_text)
        options = codex_cli.model_options_from_args(args)
        review_options = _inherit_review_options(options, codex_cli.model_options_from_args(args, prefix="review"))
        config_digest = codex_cli.semantic_config_digest(
            prompt, schema_text, options, web_search=args.web_search,
            validation_source=Path(visualization_validation.__file__).resolve(),
        )
        review_config_digest = codex_cli.semantic_config_digest(
            review_prompt, review_schema_text, review_options,
            web_search=args.review_web_search or args.web_search,
            validation_source=Path(review_validation.__file__).resolve(),
        )
    except (common.CodexError, OSError, UnicodeError, json.JSONDecodeError) as exc:
        return codex_cli.report_error(parser, exc)

    if args.dry_run:
        for source in sources:
            number = visualizations.next_run_number(source.package)
            print(
                f"Would visualize {source.label} ({', '.join(anchors)}) as run-{number:03d}"
                + ("" if args.skip_review else " and run an independent review")
                + "."
            )
        return 0

    try:
        codex = codex_cli.resolve_codex_executable(args.codex)
        codex_version = codex_cli.read_codex_version(codex)
        for source in sources:
            print(f"Visualizing {source.label} ({', '.join(anchors)})...")
            outcome = visualize(
                source, anchors,
                codex=codex, codex_version=codex_version,
                prompt=prompt, schema_path=schema_path, config_digest=config_digest,
                options=options, web_search=args.web_search,
                review=not args.skip_review,
                review_prompt=review_prompt, review_schema_path=review_schema_path,
                review_config_digest=review_config_digest, review_options=review_options,
                review_web_search=args.review_web_search or args.web_search,
                rebuild_document=args.rebuild_document,
                repair_rounds=None if args.repair_rounds is None else max(0, args.repair_rounds),
            )
            print(
                f"Installed {outcome.run_directory}: widgets {', '.join(outcome.widgets) or 'none'}; "
                f"annotations {'updated' if outcome.annotations_updated else 'unchanged'}."
            )
            if outcome.review_summary:
                print(f"Review: {outcome.review_summary}")
    except common.CodexError as exc:
        return codex_cli.report_error(parser, exc)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
