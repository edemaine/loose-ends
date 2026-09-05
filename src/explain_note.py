#!/usr/bin/env python3
"""Answer one reader note quickly with a small, unreviewed Codex turn.

The full visualization pipeline stages the whole paper and audits its
output; that takes many minutes. This script instead sends only the noted
passage, its enclosing proof or statement, the glossary, and the reader's
question, at low reasoning effort, and stores the answer as an inline
explanation marked `provenance: "quick"` so a later full run can verify or
replace it.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import shutil
import tempfile

import codex_cli
import open_problem_common as common
import visualize_paper
import visualizations


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SCHEMA_PATH = PROJECT_ROOT / "schemas" / "quick-explanation.schema.json"
DEFAULT_REASONING_EFFORT = "low"
QUICK_PROVENANCE = "quick"
QUICK_RUN_NAME = "quick-answer"
MAX_GLOSSARY_CHARS = 6000
TIMEOUT_SECONDS = 240.0


def _container_for(document: dict, anchor: str) -> dict | None:
    for proof in document.get("proofs", []):
        if anchor == proof["id"] or anchor in proof.get("paragraphs", []):
            return {"kind": "proof", "id": proof["id"], "label": proof.get("title", "Proof"), "of": proof.get("of"), "paragraphs": proof.get("paragraphs", [])}
    for statement in document.get("statements", []):
        if anchor == statement["id"] or anchor in statement.get("paragraphs", []):
            return {"kind": statement.get("kind", "statement"), "id": statement["id"], "label": statement.get("label", ""), "paragraphs": statement.get("paragraphs", [])}
    return None


def build_context(document: dict, annotations: dict | None, note: dict) -> dict:
    """Collect the small amount of paper context one note needs."""
    text = {paragraph["id"]: paragraph.get("text", "") for paragraph in document.get("paragraphs", [])}
    anchor = note["anchor"]
    container = _container_for(document, anchor)
    passage = text.get(anchor, "")
    if not passage and container:
        passage = " ".join(text.get(identifier, "") for identifier in container["paragraphs"])
    surrounding = []
    if container:
        surrounding = [{"id": identifier, "text": text.get(identifier, "")} for identifier in container["paragraphs"]]
        statement_id = container.get("of") if container["kind"] == "proof" else container["id"]
        statement = next((item for item in document.get("statements", []) if item["id"] == statement_id), None)
    else:
        statement = None
    previous = None
    if note.get("revises"):
        previous = next((entry for entry in (annotations or {}).get("explanations", []) if isinstance(entry, dict) and entry.get("id") == note["revises"]), None)
    glossary_lines = []
    total = 0
    for entry in (annotations or {}).get("glossary", []):
        line = f"- {entry.get('term')}: {entry.get('gloss')}"
        total += len(line)
        if total > MAX_GLOSSARY_CHARS:
            break
        glossary_lines.append(line)
    return {
        "title": document.get("title", ""),
        "anchor": anchor,
        "passage": passage,
        "quote": note.get("quote", ""),
        "message": note.get("message", ""),
        "latex": note.get("latex", ""),
        "previous_answer": {"title": previous.get("title", ""), "text": previous.get("text", "")} if previous else None,
        "container": container,
        "surrounding": surrounding,
        "statement": {"label": statement.get("label", ""), "title": statement.get("title", ""), "text": statement.get("text", "")} if statement else None,
        "glossary": glossary_lines,
        "macros": document.get("macros", {}),
    }


def render_prompt(context: dict) -> str:
    lines = [
        "# Explain one passage of a mathematical paper to its reader",
        "",
        "A mathematician reading the paper below marked a passage they do not "
        "follow. Answer that specific difficulty in the paper's own notation, "
        "briefly and rigorously: state the missing step, computation, case "
        "check, or definition, in two to six sentences, with LaTeX math in "
        "single `$...$` delimiters only (never `$$`, `\\(`, or `\\[`). Do not restate the surrounding text and do not lecture. If "
        "the difficulty genuinely needs a picture or a running example, say "
        "so in one sentence and set `needs_picture` to true; still give the "
        "best textual answer. If the difficulty is a term the paper never "
        "defines, give its standard definition in the text and name the "
        "source. Reply only through the structured output; do not run "
        "commands or read files.",
        "",
        f"Paper: {context['title']}",
        "",
    ]
    if context.get("statement"):
        statement = context["statement"]
        title = f" ({statement['title']})" if statement.get("title") else ""
        lines += [f"Statement under discussion: {statement['label']}{title}", "", statement["text"], ""]
    if context.get("container") and context["container"]["kind"] == "proof":
        lines += [f"The passage lies in the {context['container']['label']} of that statement. Its paragraphs, in order:", ""]
        for paragraph in context["surrounding"]:
            marker = " (contains the marked passage)" if paragraph["id"] == context["anchor"] else ""
            lines += [f"[{paragraph['id']}]{marker} {paragraph['text']}", ""]
    else:
        lines += [f"The paragraph containing the passage [{context['anchor']}]:", "", context["passage"], ""]
    if context["glossary"]:
        lines += ["Definitions the paper fixes elsewhere:", *context["glossary"], ""]
    if context.get("macros"):
        lines += ["LaTeX macros used by the paper: " + ", ".join(f"{name} = {body}" for name, body in list(context["macros"].items())[:30]), ""]
    lines += [f"Marked passage: \"{context['quote']}\""]
    if context.get("latex"):
        lines += [f"The marked passage is the formula ${context['latex']}$; explain its notation and meaning."]
    if context.get("previous_answer"):
        lines += ["", "A previous quick answer was given and the reader still does not follow it:", "",
                  f"Previous answer ({context['previous_answer']['title']}): {context['previous_answer']['text']}", "",
                  "Revise it: address what the reader now asks, keep what was right, and do not repeat the previous wording."]
    if context.get("message"):
        lines += [f"Reader's question: \"{context['message']}\""]
    lines += [
        "",
        "In `phrase`, return a short fragment (three to eight words) of the "
        "marked passage, copied verbatim and containing no formulas, at "
        "which the explanation bubble should be attached. In `title`, give a heading of at most eight "
        "words, such as \"Why the turn is a multiple of $\\pi/q$\".",
    ]
    return "\n".join(lines)


CONTROL_CHARACTERS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def sanitize_math_text(text: str) -> str:
    """Clean model prose with `$...$` math: drop control characters, normalise
    `\\(...\\)` delimiters to `$...$`, and repair an odd dollar sign."""
    text = CONTROL_CHARACTERS_RE.sub("", str(text or ""))
    text = re.sub(r"\\\((.*?)\\\)", lambda m: f"${m.group(1)}$", text, flags=re.S)
    text = re.sub(r"\\\[(.*?)\\\]", lambda m: f"$${m.group(1)}$$", text, flags=re.S)
    if text.count("$") % 2 == 1:
        # A spurious empty pair ("$$" with nothing inside) shifts every later
        # delimiter; collapsing the first one restores the pairing.
        text = text.replace("$$", "$", 1)
    return text.strip()


def _phrase_for(result: dict, note: dict, passage: str) -> str:
    from validation.visualization import phrase_found

    candidate = re.sub(r"\$\$.*?\$\$|\$[^$]*\$", " ", str(result.get("phrase") or ""))
    candidate = re.sub(r"\s+", " ", candidate).strip()
    if len(candidate.split()) >= 2 and phrase_found(candidate, passage):
        return candidate
    words = note.get("quote", "").split()
    for length in (6, 4, 3, 2):
        fragment = " ".join(words[:length])
        if fragment and phrase_found(fragment, passage):
            return fragment
    return note.get("quote", "")[:80]


def apply_answer(package: Path, note: dict, result: dict, passage: str) -> dict:
    """Store the quick answer in annotations.json and mark the note addressed."""
    annotations = common.load_json(package / visualizations.ANNOTATIONS_NAME)
    if not isinstance(annotations, dict):
        annotations = {"glossary": [], "proof_outlines": {}}
    explanations = annotations.setdefault("explanations", [])
    if not isinstance(explanations, list):
        explanations = annotations["explanations"] = []
    explanation = {
        "id": f"quick-{note['id']}",
        "anchor": note["anchor"],
        "phrase": _phrase_for(result, note, passage),
        "title": sanitize_math_text(result.get("title") or "Explanation"),
        "text": sanitize_math_text(result.get("text") or ""),
        "provenance": QUICK_PROVENANCE,
        "note": note["id"],
    }
    if note.get("latex"):
        explanation["latex"] = note["latex"]
    if result.get("needs_picture"):
        explanation["text"] += "\n\nA full visualization run may add a picture for this step."
    superseded = {explanation["id"], note.get("revises") or ""}
    explanations[:] = [entry for entry in explanations if entry.get("id") not in superseded]
    explanations.append(explanation)
    common.write_json(package / visualizations.ANNOTATIONS_NAME, annotations)
    manifest = visualizations.load_manifest(package)
    if manifest is not None and not manifest.get("annotations"):
        manifest["annotations"] = visualizations.ANNOTATIONS_NAME
        visualizations.write_manifest(package, manifest)
    visualizations.mark_notes_addressed(package, [note["id"]], QUICK_RUN_NAME, outcome=explanation["title"])
    return explanation


def quick_answer(
    source: visualize_paper.SourceRef,
    note: dict,
    *,
    codex: str,
    options: codex_cli.ModelOptions,
    schema_path: Path = DEFAULT_SCHEMA_PATH,
) -> dict:
    package = source.package
    document = visualizations.load_document(package)
    if document is None:
        raise common.CodexError("the reader document has not been built; run visualize_paper.py first")
    annotations = common.load_json(package / visualizations.ANNOTATIONS_NAME)
    context = build_context(document, annotations if isinstance(annotations, dict) else None, note)
    workspace = Path(tempfile.mkdtemp(prefix=".explain-run-", dir=source.directory)).resolve()
    try:
        result_path = codex_cli.run_structured_codex(
            codex=codex,
            workspace=workspace,
            prompt=render_prompt(context),
            schema_path=schema_path,
            options=options,
            web_search="disabled",
            launch_interval=0.0,
            timeout_seconds=TIMEOUT_SECONDS,
        )
        result = common.read_json(result_path, description="quick explanation")
    except (common.CodexError, OSError, ValueError) as exc:
        raise common.CodexError(common.preserved_workspace_message(exc, workspace)) from exc
    shutil.rmtree(workspace, ignore_errors=True)
    for field in ("title", "text", "phrase"):
        if not isinstance(result.get(field), str) or not result[field].strip():
            raise common.CodexError(f"quick explanation is missing {field}")
    return apply_answer(package, note, result, context["passage"] or " ".join(item["text"] for item in context["surrounding"]))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="answer one reader note with a quick, unreviewed explanation")
    parser.add_argument("source", type=Path, help="manuscript draft or paper directory")
    parser.add_argument("--note-id", help="id of a note stored in the package's notes.json")
    parser.add_argument("--anchor", help="element id of the passage (when not using --note-id)")
    parser.add_argument("--quote", help="the marked passage")
    parser.add_argument("--message", default="", help="the reader's question")
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
        if args.note_id:
            note = next((item for item in visualizations.load_notes(source.package) if item["id"] == args.note_id), None)
            if note is None:
                raise common.CodexError(f"unknown note {args.note_id}")
        else:
            if not args.anchor or not args.quote:
                raise common.CodexError("--anchor and --quote are required without --note-id")
            note = visualizations.add_note(source.package, {"anchor": args.anchor, "quote": args.quote, "message": args.message})
        options = codex_cli.model_options_from_args(args)
        if not options.reasoning_effort:
            options = codex_cli.ModelOptions(options.model, DEFAULT_REASONING_EFFORT, options.fast)
        codex = codex_cli.resolve_codex_executable(args.codex)
        explanation = quick_answer(source, note, codex=codex, options=options, schema_path=args.schema.expanduser().resolve())
    except common.CodexError as exc:
        return codex_cli.report_error(parser, exc)
    print(json.dumps({"note": note["id"], "explanation": explanation}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
