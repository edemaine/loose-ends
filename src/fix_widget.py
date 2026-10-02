#!/usr/bin/env python3
"""Apply a reader's fix request to one widget with a single, unreviewed Codex turn.

The full pipeline regenerates and audits widgets; that takes many minutes.
This script instead hands Codex the widget's own files, the reader API, and
the reader's complaint, asks for the smallest change that fixes it, checks
the result with the same deterministic widget checks the pipeline uses, and
installs it with its review marked as superseded by an unreviewed quick fix.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import tempfile

import codex_cli
import open_problem_common as common
import paper_document
from validation import common as validation_common
from validation import visualization as visualization_validation
import visualizations


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SCHEMA_PATH = PROJECT_ROOT / "schemas" / "quick-fix.schema.json"
DEFAULT_REASONING_EFFORT = "medium"
QUICK_RUN_NAME = "quick-fix"
TIMEOUT_SECONDS = 900.0
READER_DIRECTORY = PROJECT_ROOT / "src" / "workbench_web" / "reader"


def _step_context(widget: dict, note: dict) -> list[str]:
    step = note.get("step")
    steps = widget.get("steps") or []
    if step is None or not isinstance(step, int) or step >= len(steps):
        return []
    current = steps[step]
    lines = [
        "",
        f"The reader was looking at step {step + 1} of {len(steps)}, "
        f"\"{current.get('title', '')}\" (paragraphs {', '.join(current.get('paragraphs', []))}"
        + (f", phrase \"{current['phrase']}\"" if current.get("phrase") else "") + "), when reporting. "
        "Unless the report clearly concerns the whole widget, fix that step's state: the "
        "picture and caption shown when `setStep` is called with that index.",
    ]
    return lines


def _widget_state_context(note: dict) -> list[str]:
    lines = []
    if note.get("example"):
        lines.append(f"Selected running example: `{note['example']}`.")
    if note.get("widget_state") is not None:
        lines.extend([
            "Captured edited inputs (JSON):",
            json.dumps(note["widget_state"], ensure_ascii=False, allow_nan=False),
            "Reproduce with setExample(id), then setState(snapshot), then setStep(index). "
            "Preserve support for this state format where possible; test this exact input.",
        ])
    if note.get("widget_state_error"):
        lines.append(f"Input capture was incomplete: {note['widget_state_error']}")
    return ["", *lines] if lines else []


def render_prompt(widget: dict, note: dict, document_title: str, previous: dict | None = None) -> str:
    lines = [
        "# Fix one reader-reported problem in a paper-reader widget",
        "",
        f"The widget `{widget['id']}` (\"{widget.get('title', '')}\") is mounted beside "
        f"`{widget.get('anchor', '')}` of the paper \"{document_title}\" in the Loose Ends "
        "reader. Its files are under `widget/` in this workspace: `widget.js`, "
        "`widget.json`, and any assets. The reader API and the rules the widget "
        "must follow are in `reader/WIDGET-API.md`; `reader/reader.js` shows "
        "exactly how the widget is mounted and driven.",
        "",
        "A reader using the widget reported:",
        "",
        f"> {note.get('message') or note.get('quote') or 'Something is wrong with this widget.'}",
        *_step_context(widget, note),
        *_widget_state_context(note),
        *(["", f"This follows an earlier request, \"{previous.get('message', '')}\", which was applied as: {previous.get('outcome') or 'a change without a recorded summary'}. The reader is still not satisfied; do not repeat that change, address what is still wrong."] if previous else []),
        "",
        "Make the smallest change to the files under `widget/` that fixes what "
        "the reader reports, keeping everything else exactly as it is: the "
        "widget id, its anchor, its examples and steps unless the report is "
        "about them, and the mathematics. Do not add features. Keep the "
        "coordinate frame fixed and the interaction rules of the API. Run "
        "`node --check widget/widget.js` and any scripted interaction test "
        "the widget ships with. If the report cannot be fixed safely in a "
        "small change (for example it needs a different running example or "
        "new mathematics), set `fixed` to false and explain why in `summary`; "
        "do not make a partial change.",
        "",
        "Reply through the structured output: `fixed`, a one-sentence "
        "`summary` of the change, and `files_changed`.",
    ]
    return "\n".join(lines)


def check_widget(directory: Path, widget_id: str, original_manifest: dict, document: dict | None = None) -> list[str]:
    """Return problems with the edited widget, using the pipeline's full validator."""
    manifest = common.load_json(directory / visualizations.WIDGET_MANIFEST_NAME)
    if not isinstance(manifest, dict):
        return ["widget.json is missing or invalid"]
    problems = []
    for field in ("id", "anchor", "kind"):
        if manifest.get(field) != original_manifest.get(field):
            problems.append(f"widget.json {field} must not change")
    anchor = str(original_manifest.get("anchor") or "")
    document = document or {}
    # Stage the widget the way a run's output looks and reuse the run validator.
    with tempfile.TemporaryDirectory(prefix="le-check-") as temporary:
        workspace = Path(temporary)
        staged = workspace / visualization_validation.OUTPUT_DIRECTORY / visualization_validation.WIDGETS_DIRECTORY / widget_id
        shutil.copytree(directory, staged, ignore=shutil.ignore_patterns(visualizations.WIDGET_REVIEW_NAME))
        files = sorted(
            f"{visualization_validation.OUTPUT_DIRECTORY}/{visualization_validation.WIDGETS_DIRECTORY}/{widget_id}/{path.relative_to(staged).as_posix()}"
            for path in staged.rglob("*") if path.is_file() or path.is_symlink()
        )
        common.write_json(workspace / validation_common.AGENT_RESULT_FILENAME, {
            "status": "complete", "summary": "quick fix", "annotations_updated": False,
            "widgets": [{
                "id": widget_id, "anchor": anchor, "kind": str(manifest.get("kind") or "statement"),
                "title": str(manifest.get("title") or widget_id), "summary": str(manifest.get("summary") or "-"),
                "limitations": list(manifest.get("limitations") or []), "files": files,
            }],
            "verification_checks": [{"name": "quick fix", "method": "node --check", "result": "passed", "details": "-"}],
            "warnings": [],
        })
        expectations = {
            "anchors": [anchor],
            "annotations_required": False,
            "document_ids": paper_document.anchor_ids(document) if document else {anchor: str(manifest.get("kind") or "statement")},
            "proof_paragraphs": {proof["id"]: list(proof.get("paragraphs", [])) for proof in document.get("proofs", [])},
            "paragraph_text": {paragraph["id"]: paragraph.get("text", "") for paragraph in document.get("paragraphs", [])},
            "note_ids": [],
            "result_schema": json.loads((PROJECT_ROOT / "schemas" / "visualization-result.schema.json").read_text(encoding="utf-8")),
        }
        report = visualization_validation.validate(workspace=workspace, expectations=expectations)
    problems.extend(issue.render() for issue in report.issues)
    return problems


def quick_fix(
    source: visualizations.SourceRef,
    note: dict,
    *,
    codex: str,
    options: codex_cli.ModelOptions,
    schema_path: Path = DEFAULT_SCHEMA_PATH,
) -> dict:
    package = source.package
    widget_id = note.get("widget") or ""
    widget_directory = package / visualizations.WIDGETS_DIRECTORY / widget_id
    workspace = Path(tempfile.mkdtemp(prefix=".fix-run-", dir=source.directory)).resolve()
    try:
        with visualizations.package_lock(package):
            manifest = common.load_json(widget_directory / visualizations.WIDGET_MANIFEST_NAME)
            if not widget_id or not isinstance(manifest, dict):
                raise common.CodexError(f"unknown widget {widget_id!r}")
            document = visualizations.load_document(package) or {}
            previous = visualizations.find_note(package, note["follows"]) if note.get("follows") else None
            shutil.copytree(widget_directory, workspace / "widget", ignore=shutil.ignore_patterns(visualizations.WIDGET_REVIEW_NAME))
        reader = workspace / "reader"
        reader.mkdir()
        shutil.copyfile(PROJECT_ROOT / "prompts" / "visualization-widget-api.md", reader / "WIDGET-API.md")
        for name in ("reader.js", "reader.css"):
            shutil.copyfile(READER_DIRECTORY / name, reader / name)
        result_path = codex_cli.run_structured_codex(
            codex=codex,
            workspace=workspace,
            prompt=render_prompt(manifest, note, document.get("title", ""), previous),
            schema_path=schema_path,
            options=options,
            web_search="disabled",
            launch_interval=0.0,
            timeout_seconds=TIMEOUT_SECONDS,
        )
        result = common.read_json(result_path, description="quick fix result")
        if not result.get("fixed"):
            raise common.CodexError("quick fix declined: " + str(result.get("summary") or "no reason given"))
        problems = check_widget(workspace / "widget", widget_id, manifest, document)
        if problems:
            raise common.CodexError("quick fix rejected: " + "; ".join(problems))
        visualizations.install_quick_fix(
            package, widget_id, workspace / "widget",
            note_id=note["id"], summary=str(result.get("summary") or ""),
            document_digest=document.get("source", {}).get("digest", ""), run_name=QUICK_RUN_NAME,
        )
    except (common.CodexError, OSError, ValueError) as exc:
        raise common.CodexError(common.preserved_workspace_message(exc, workspace)) from exc
    shutil.rmtree(workspace, ignore_errors=True)
    return {"widget": widget_id, "summary": str(result.get("summary") or ""), "files_changed": result.get("files_changed", [])}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="apply a reader-reported fix to one widget with a quick, unreviewed Codex turn")
    parser.add_argument("source", type=Path, help="manuscript draft or paper directory")
    parser.add_argument("--note-id", required=True, help="id of a widget note stored in the package's notes.json")
    parser.add_argument("--codex", default="codex")
    parser.add_argument("--schema", type=Path, default=DEFAULT_SCHEMA_PATH)
    codex_cli.add_model_arguments(parser)
    return parser


def main(argv: list[str] | None = None) -> int:
    codex_cli.configure_utf8_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        source = visualizations.source_from_path(args.source)
        note = next((item for item in visualizations.load_notes(source.package) if item["id"] == args.note_id), None)
        if note is None or not note.get("widget"):
            raise common.CodexError(f"note {args.note_id} is not a widget note")
        options = codex_cli.model_options_from_args(args)
        if not options.reasoning_effort:
            options = codex_cli.ModelOptions(options.model, DEFAULT_REASONING_EFFORT, options.fast)
        codex = codex_cli.resolve_codex_executable(args.codex)
        outcome = quick_fix(source, note, codex=codex, options=options, schema_path=args.schema.expanduser().resolve())
    except common.CodexError as exc:
        return codex_cli.report_error(parser, exc)
    print(json.dumps(outcome, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
