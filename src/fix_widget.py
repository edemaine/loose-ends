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
from validation import common as validation_common
from validation import visualization as visualization_validation
import visualize_paper
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


def check_widget(directory: Path, widget_id: str, original_manifest: dict) -> list[str]:
    """Return problems with the edited widget, using the pipeline's checks."""
    reporter = validation_common.Reporter()
    entry = directory / visualizations.WIDGET_ENTRY_NAME
    if not entry.is_file():
        return ["widget.js is missing"]
    visualization_validation.validate_widget_script(entry, widget_id, reporter, "widget/widget.js")
    manifest = common.load_json(directory / visualizations.WIDGET_MANIFEST_NAME)
    if not isinstance(manifest, dict):
        reporter.error("E_WIDGET_MANIFEST", "widget.json is missing or invalid", path="widget/widget.json")
    else:
        for field in ("id", "anchor", "kind"):
            if manifest.get(field) != original_manifest.get(field):
                reporter.error("E_WIDGET_MANIFEST", f"widget.json {field} must not change", path="widget/widget.json")
    for path in directory.rglob("*"):
        if path.is_symlink():
            reporter.error("E_WIDGET_FILES", f"symbolic links are not allowed: {path.name}", path="widget")
        elif path.is_file() and path.suffix.lower() == ".html":
            reporter.error("E_WIDGET_FILES", "widgets must not contain HTML documents", path="widget")
    return [issue.render() for issue in reporter.issues]


def quick_fix(
    source: visualize_paper.SourceRef,
    note: dict,
    *,
    codex: str,
    options: codex_cli.ModelOptions,
    schema_path: Path = DEFAULT_SCHEMA_PATH,
) -> dict:
    package = source.package
    widget_id = note.get("widget") or ""
    widget_directory = package / visualizations.WIDGETS_DIRECTORY / widget_id
    manifest = common.load_json(widget_directory / visualizations.WIDGET_MANIFEST_NAME)
    if not widget_id or not isinstance(manifest, dict):
        raise common.CodexError(f"unknown widget {widget_id!r}")
    document = visualizations.load_document(package) or {}
    previous = visualizations.find_note(package, note["follows"]) if note.get("follows") else None
    workspace = Path(tempfile.mkdtemp(prefix=".fix-run-", dir=source.directory)).resolve()
    try:
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
        problems = check_widget(workspace / "widget", widget_id, manifest)
        if problems:
            raise common.CodexError("quick fix rejected: " + "; ".join(problems))
        archive = package / visualizations.RUNS_DIRECTORY / "quick-fixes" / f"{widget_id}-{common.utc_now().replace(':', '').replace('+', 'Z')}"
        archive.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(widget_directory, archive)
        previous_review = common.load_json(widget_directory / visualizations.WIDGET_REVIEW_NAME)
        for path in workspace.joinpath("widget").rglob("*"):
            if path.is_file():
                target = widget_directory / path.relative_to(workspace / "widget")
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, target)
        common.write_json(widget_directory / visualizations.WIDGET_REVIEW_NAME, {
            "fidelity": "unreviewed",
            "interaction_quality": "unreviewed",
            "summary": f"Quick fix applied without review: {result.get('summary', '')}",
            "findings": [],
            "blocking_gaps": [],
            "provenance": "quick",
            "previous_review": previous_review if isinstance(previous_review, dict) else None,
        })
    except (common.CodexError, OSError, ValueError) as exc:
        raise common.CodexError(common.preserved_workspace_message(exc, workspace)) from exc
    shutil.rmtree(workspace, ignore_errors=True)
    visualizations.mark_notes_addressed(package, [note["id"]], QUICK_RUN_NAME, outcome=str(result.get("summary") or ""))
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
        source = visualize_paper.source_from_path(args.source)
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
