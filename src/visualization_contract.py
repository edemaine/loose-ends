"""Shared on-disk visualization contract; no command or storage dependencies."""

import re


DIRECTORY_NAME = "visualization"
OUTPUT_DIRECTORY = "output"
MANIFEST_NAME = "visualization.json"
DOCUMENT_HTML = "document.html"
DOCUMENT_JSON = "document.json"
FIGURES_DIRECTORY = "figures"
ANNOTATIONS_NAME = "annotations.json"
WIDGETS_DIRECTORY = "widgets"
RUNS_DIRECTORY = "runs"
WIDGET_MANIFEST_NAME = "widget.json"
WIDGET_ENTRY_NAME = "widget.js"
WIDGET_REVIEW_NAME = "review.json"
NOTES_NAME = "notes.json"
CRITIQUE_FILENAME = "critique.md"
READER_FILES = {"reader.html", "reader.js", "reader.css"}

DOCUMENT_SCHEMA_VERSION = 1
MANIFEST_SCHEMA_VERSION = 2
ANNOTATIONS_SCHEMA_VERSION = 1
WIDGET_SCHEMA_VERSION = 1
REVIEW_SCHEMA_VERSION = 1
NOTES_SCHEMA_VERSION = 1
WIDGET_API_VERSION = 1

NOTE_ID_RE = re.compile(r"^note-[0-9]{3,}$")
RUN_RE = re.compile(r"^run-([0-9]{3,})$")
WIDGET_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,80}$")
MAX_NOTES = 200
MAX_NOTE_TEXT = 2000
DEFAULT_ANCHOR = "default"
NOTES_ANCHOR = "notes"

ANNOTATION_VERSIONS = {"schema_version": ANNOTATIONS_SCHEMA_VERSION}
WIDGET_VERSIONS = {"schema_version": WIDGET_SCHEMA_VERSION, "api_version": WIDGET_API_VERSION}
ANNOTATION_FIELDS = frozenset({
    "main_result", "glossary", "proof_outlines", "explanations", "notes",
    *ANNOTATION_VERSIONS, "document_digest",
})
WIDGET_FIELDS = frozenset({
    "id", "anchor", "kind", "title", "summary", "limitations", "steps", "examples",
    *WIDGET_VERSIONS, "document_digest", "run",
})


def widget_id(anchor: str) -> str:
    """Return the canonical widget directory name for an anchor."""
    slug = re.sub(r"[^a-z0-9]+", "-", anchor.lower()).strip("-")
    return slug or "widget"
