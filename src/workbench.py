#!/usr/bin/env python3
"""Live local dashboard and persistent task manager for Loose Ends."""

from __future__ import annotations

import argparse
from collections import deque
from datetime import datetime
import hashlib
import ipaddress
import json
import mimetypes
import os
from pathlib import Path
import re
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time
from typing import Iterable
import unicodedata
from urllib.parse import parse_qs, unquote, urlsplit
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import analyze_papers
import codex_cli
import human_review
import open_problem_common as common
import review_solutions
import write_paper
from workbench_store import ACTIVE_STATUSES, WorkbenchStore
from workbench_worker import recover_run_artifacts
from workbench_tasks import (
    PlanError,
    build_plan,
    populate_dry_run_previews,
)

try:
    from watchdog.events import FileSystemEvent, FileSystemEventHandler
    from watchdog.observers import Observer
except ImportError:  # pragma: no cover - reported cleanly from main
    FileSystemEvent = object  # type: ignore[assignment,misc]
    FileSystemEventHandler = object  # type: ignore[assignment,misc]
    Observer = None  # type: ignore[assignment]


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ASSET_DIRECTORY = Path(__file__).resolve().parent / "workbench_web"
DEFAULT_STATE_DIRECTORY = PROJECT_ROOT / ".loose-ends"
DEFAULT_MANUSCRIPTS = PROJECT_ROOT / "manuscripts"
IGNORED_PARTS = {".git", ".loose-ends", "__pycache__", ".runs"}
IGNORED_PREFIXES = (
    ".run-",
    ".recover-",
    ".solve-run-",
    ".review-run-",
    ".write-run-",
    ".paper-review-run-",
    ".literature-run-",
    ".attempt-install-",
    ".review-install-",
    ".literature-install-",
    ".draft-install-",
    ".paper-review-install-",
    ".triage-install-",
)
DRAFT_RE = re.compile(r"^draft-([0-9]{3,})$")
CATALOG_CACHE_SCHEMA_VERSION = 2
CATALOG_FILE_NAMES = {
    "metadata.json",
    "paper.pdf",
    *common.ANALYSIS_FILES,
    common.TRIAGE_MARKDOWN,
    common.TRIAGE_RESULT,
    common.TRIAGE_MANIFEST,
    common.LITERATURE_MARKDOWN,
    common.LITERATURE_RESULT,
    common.LITERATURE_MANIFEST,
    *common.ATTEMPT_HISTORY_FILES,
    "paper-result.json",
    "paper-review.json",
    "main.tex",
    "main.pdf",
    "readiness.md",
    "paper-critique.md",
}


def _read_json(path: Path) -> dict:
    value = common.load_json(path)
    return value if isinstance(value, dict) else {}


def _manuscript_abstract(path: Path) -> str:
    """Extract a display-friendly abstract from a draft's LaTeX source."""
    try:
        tex = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return ""
    match = re.search(
        r"\\begin\s*\{abstract\}(.*?)\\end\s*\{abstract\}",
        tex,
        flags=re.DOTALL,
    )
    if match is None:
        return ""
    lines = [
        re.sub(r"(?<!\\)%.*$", "", line).strip()
        for line in match.group(1).splitlines()
    ]
    paragraphs = [
        re.sub(r"\s+", " ", paragraph).strip()
        for paragraph in re.split(r"\n\s*\n", "\n".join(lines))
    ]
    value = "\n\n".join(paragraph for paragraph in paragraphs if paragraph)
    latex_letters = {
        "ae": "æ", "AE": "Æ", "oe": "œ", "OE": "Œ",
        "o": "ø", "O": "Ø", "aa": "å", "AA": "Å",
        "ss": "ß", "l": "ł", "L": "Ł", "i": "i", "j": "j",
    }
    value = re.sub(
        r"\\(ae|AE|oe|OE|aa|AA|ss|o|O|l|L|i|j)(?![A-Za-z])",
        lambda match: latex_letters[match.group(1)],
        value,
    )
    accent_marks = {
        "'": "\N{COMBINING ACUTE ACCENT}",
        "`": "\N{COMBINING GRAVE ACCENT}",
        '"': "\N{COMBINING DIAERESIS}",
        "^": "\N{COMBINING CIRCUMFLEX ACCENT}",
        "~": "\N{COMBINING TILDE}",
        "=": "\N{COMBINING MACRON}",
        ".": "\N{COMBINING DOT ABOVE}",
        "c": "\N{COMBINING CEDILLA}",
        "k": "\N{COMBINING OGONEK}",
        "u": "\N{COMBINING BREVE}",
        "v": "\N{COMBINING CARON}",
        "H": "\N{COMBINING DOUBLE ACUTE ACCENT}",
        "r": "\N{COMBINING RING ABOVE}",
        "d": "\N{COMBINING DOT BELOW}",
    }

    def unicode_accent(match: re.Match) -> str:
        return unicodedata.normalize(
            "NFC", f"{match.group(2) or match.group(3)}{accent_marks[match.group(1)]}"
        )

    value = re.sub(
        r"\\(['\"`^~=.]|[ckuvHrd])\s*(?:\{([A-Za-z])\}|([A-Za-z]))",
        unicode_accent,
        value,
    )
    # Preserve math for the Markdown/KaTeX renderer, while cleaning the most
    # common prose-only LaTeX conventions.
    formatting = {
        "emph": ("*", "*"),
        "textit": ("*", "*"),
        "textbf": ("**", "**"),
        "texttt": ("`", "`"),
        "textrm": ("", ""),
        "textsf": ("", ""),
        "mbox": ("", ""),
    }

    def markdown_format(match: re.Match) -> str:
        opening, closing = formatting[match.group(1)]
        return f"{opening}{match.group(2)}{closing}"

    for _ in range(8):
        cleaned = re.sub(
            r"\\(emph|textit|textbf|textrm|textsf|texttt|mbox)\s*\{([^{}]*)\}",
            markdown_format,
            value,
        )
        if cleaned == value:
            break
        value = cleaned
    return (
        value.replace("``", "“")
        .replace("''", "”")
        .replace("~", " ")
        .replace(r"\%", "%")
        .replace(r"\&", "&")
        .replace(r"\_", "_")
        .replace(r"\#", "#")
    )


def _paper_metadata(
    paper: Path,
    *,
    analysis: dict | None = None,
    metadata: dict | None = None,
) -> tuple[str, list[str]]:
    if analysis is None:
        analysis = _read_json(paper / "analysis" / "manifest.json")
    if metadata is None:
        metadata = _read_json(paper / "metadata.json")
    title = analysis.get("paper_title") or metadata.get("title") or paper.name
    authors = analysis.get("paper_authors") or metadata.get("authors") or []
    if not isinstance(authors, list):
        authors = []
    return str(title), [str(author) for author in authors]


def _url_key(path: Path) -> str:
    """Return the same portable project-relative identity used by reviews."""
    return human_review.project_display_path(path)


def _paper_inventory(paths: Iterable[Path]) -> list[dict]:
    papers = analyze_papers.discover_paper_directories(paths)
    records = []
    for paper in papers:
        manifest = _read_json(paper / "analysis" / "manifest.json")
        metadata = _read_json(paper / "metadata.json")
        title, authors = _paper_metadata(
            paper,
            analysis=manifest,
            metadata=metadata,
        )
        timeline = human_review.paper_timeline(paper, metadata=metadata)
        resolved = paper.resolve()
        records.append(
            {
                "key": str(resolved),
                "urlKey": _url_key(paper),
                "path": str(resolved),
                "name": paper.name,
                "title": title,
                "authors": authors,
                "published": timeline["published"],
                "updated": timeline["updated"],
                "activityTimestamp": timeline["activityTimestamp"],
                "analyzed": bool(manifest),
                "problemCount": len(manifest.get("open_problems", [])),
                "analysisStatus": manifest.get("status", ""),
                "files": [
                    str(path)
                    for path in (
                        paper / "paper.pdf",
                        paper / "metadata.json",
                        paper / "analysis" / "summary.md",
                        paper / "analysis" / "results.md",
                        paper / "analysis" / "open-problems.md",
                    )
                    if path.is_file()
                ],
            }
        )
    records.sort(key=lambda item: (item["title"].casefold(), item["path"]))
    return records


def _review_inventory(
    paths: Iterable[Path],
    progress=None,
    stage_progress=None,
) -> list[dict]:
    try:
        problems = common.discover_problem_refs(paths)
    except common.CodexError as exc:
        if "none of the discovered papers has" in str(exc) or "no open problems" in str(exc):
            return []
        raise
    selection_progress = None
    freshness_progress = None
    if stage_progress is not None:
        selection_progress = lambda current, total: stage_progress(
            "Checking solution reviews…",
            current,
            total,
        )
        freshness_progress = lambda current, total: stage_progress(
            "Checking triage and literature…",
            current,
            total,
        )
    items = human_review.discover_human_reviews(
        problems,
        priority=set(review_solutions.HUMAN_PRIORITY_LEVELS),
        include_stale=True,
        allow_empty=True,
        progress=selection_progress,
    )
    return human_review.build_review_catalog(
        items,
        include_contents=False,
        problems=problems,
        progress=progress,
        freshness_progress=freshness_progress,
        include_browser_uris=False,
        include_files=False,
    )


def _manuscript_sources(
    manifest: dict,
    papers: list[dict],
    reviews: list[dict],
    *,
    paper_lookup: dict[str, dict] | None = None,
    problem_lookup: dict[tuple[str, str], dict] | None = None,
) -> dict:
    if paper_lookup is None:
        paper_lookup = {
            os.path.normcase(str(Path(item["path"]))): item
            for item in papers
        }
    if problem_lookup is None:
        problem_lookup = {
            (
                os.path.normcase(str(Path(item["paperDirectory"]))),
                item["problemId"],
            ): item
            for item in reviews
        }
    source_papers: dict[str, dict] = {}
    source_problems: dict[tuple[str, str], dict] = {}
    paper_selectors: set[str] = set()

    def add(
        paper_value: object,
        problem_id: object = None,
        *,
        selector_kind: str | None = None,
        attempt_name: object = None,
    ) -> None:
        if not isinstance(paper_value, str) or not paper_value:
            return
        try:
            paper = write_paper.resolve_manifest_path(paper_value)
        except (OSError, ValueError):
            return
        paper_key = os.path.normcase(str(paper))
        paper_record = paper_lookup.get(paper_key, {})
        paper_title = str(paper_record.get("title") or paper.name)
        source_papers[paper_key] = {
            "key": paper_key,
            "urlKey": _url_key(paper),
            "path": str(paper),
            "title": paper_title,
        }
        if not isinstance(problem_id, str) or not problem_id:
            return
        review = problem_lookup.get((paper_key, problem_id), {})
        problem_key = (paper_key, problem_id)
        record = source_problems.setdefault(
            problem_key,
            {
                "key": f"{paper_key}::{problem_id}",
                "paperPath": str(paper.resolve()),
                "paperTitle": paper_title,
                "id": problem_id,
                "title": str(review.get("problemTitle") or problem_id),
            },
        )
        if selector_kind is not None:
            record["selectorKind"] = selector_kind
            record["pinned"] = selector_kind in {"attempt", "pin"}
        if isinstance(attempt_name, str) and attempt_name:
            record["attemptName"] = attempt_name

    selectors = manifest.get("input_selectors", [])
    if isinstance(selectors, list):
        for selector in selectors:
            if not isinstance(selector, dict):
                continue
            value = selector.get("path")
            kind = selector.get("kind")
            if not isinstance(value, str) or kind not in {
                "paper", "problem", "attempt", "pin"
            }:
                continue
            try:
                path = write_paper.resolve_manifest_path(value)
            except (OSError, ValueError):
                continue
            if kind == "paper":
                add(str(path))
                paper_selectors.add(os.path.normcase(str(path)))
            elif kind == "problem":
                add(str(path.parent), path.name, selector_kind="problem")
            else:
                add(
                    str(path.parent.parent),
                    path.parent.name,
                    selector_kind=kind,
                    attempt_name=path.name,
                )

    attempts = manifest.get("input_attempts", [])
    if isinstance(attempts, list):
        for attempt in attempts:
            if isinstance(attempt, dict):
                attempt_name = attempt.get("attempt_name")
                if not isinstance(attempt_name, str):
                    attempt_path = attempt.get("attempt_path")
                    if isinstance(attempt_path, str):
                        try:
                            attempt_name = write_paper.resolve_manifest_path(
                                attempt_path
                            ).name
                        except (OSError, ValueError):
                            attempt_name = None
                add(
                    attempt.get("paper_directory"),
                    attempt.get("problem_id"),
                    attempt_name=attempt_name,
                )

    for (paper_key, _problem_id), record in source_problems.items():
        if "selectorKind" in record:
            continue
        if paper_key in paper_selectors:
            record["selectorKind"] = "paper"
            record["pinned"] = False
        else:
            # Legacy manifests without input_selectors reconstruct exact
            # attempt selectors during revision, so they are pinned.
            record["selectorKind"] = "attempt"
            record["pinned"] = True

    pinned = sum(bool(item.get("pinned")) for item in source_problems.values())
    tracking = len(source_problems) - pinned

    return {
        "papers": sorted(
            source_papers.values(),
            key=lambda item: (item["title"].casefold(), item["path"]),
        ),
        "problems": sorted(
            source_problems.values(),
            key=lambda item: (
                item["paperTitle"].casefold(),
                item["id"],
            ),
        ),
        "pinning": {
            "pinned": pinned,
            "tracking": tracking,
        },
    }


def _manuscript_inventory(
    directory: Path,
    papers: list[dict] | None = None,
    reviews: list[dict] | None = None,
    progress=None,
) -> list[dict]:
    if not directory.is_dir():
        return []
    papers = papers or []
    reviews = reviews or []
    paper_lookup = {
        os.path.normcase(str(Path(item["path"]))): item
        for item in papers
    }
    problem_lookup = {
        (
            os.path.normcase(str(Path(item["paperDirectory"]))),
            item["problemId"],
        ): item
        for item in reviews
    }
    draft_total = sum(
        1
        for manuscript in directory.iterdir()
        if manuscript.is_dir()
        for draft in manuscript.iterdir()
        if draft.is_dir() and DRAFT_RE.fullmatch(draft.name)
    )
    draft_current = 0
    if progress is not None:
        progress(draft_current, draft_total)
    manuscripts = []
    for manuscript in sorted(path for path in directory.iterdir() if path.is_dir()):
        drafts = []
        for draft in manuscript.iterdir():
            match = DRAFT_RE.fullmatch(draft.name)
            if not draft.is_dir() or match is None:
                continue
            result = _read_json(draft / "paper-result.json")
            review = _read_json(draft / "paper-review.json")
            manifest = _read_json(draft / "manifest.json")
            generated_at = manifest.get("generated_at")
            try:
                created_timestamp = datetime.fromisoformat(
                    str(generated_at).replace("Z", "+00:00")
                ).timestamp()
            except (TypeError, ValueError):
                created_timestamp = draft.stat().st_mtime
            draft_files = [
                path
                for path in (
                    draft / "main.pdf",
                    draft / "main.tex",
                    draft / "readiness.md",
                    draft / "paper-critique.md",
                    draft / "paper-review.json",
                )
                if path.is_file()
            ]
            for directory_name in ("figures", "code"):
                generated_directory = draft / directory_name
                if generated_directory.is_dir():
                    draft_files.extend(
                        sorted(
                            path
                            for path in generated_directory.rglob("*")
                            if path.is_file()
                        )
                    )
            drafts.append(
                {
                    "key": str(draft.resolve()),
                    "urlKey": _url_key(draft),
                    "path": str(draft.resolve()),
                    "name": draft.name,
                    "number": int(match.group(1)),
                    "title": result.get("title") or manifest.get("title") or manuscript.name,
                    "createdTimestamp": created_timestamp,
                    "abstract": _manuscript_abstract(draft / "main.tex"),
                    "status": result.get("status") or manifest.get("status") or "",
                    "verdict": review.get("verdict", "unreviewed"),
                    "summary": review.get("summary", ""),
                    "sources": _manuscript_sources(
                        manifest,
                        papers,
                        reviews,
                        paper_lookup=paper_lookup,
                        problem_lookup=problem_lookup,
                    ),
                    "files": [str(path.resolve()) for path in draft_files],
                }
            )
            draft_current += 1
            if progress is not None:
                progress(draft_current, draft_total)
        if drafts:
            drafts.sort(key=lambda item: item["number"])
            manuscripts.append(
                {
                    "key": str(manuscript.resolve()),
                    "urlKey": _url_key(manuscript),
                    "path": str(manuscript.resolve()),
                    "name": manuscript.name,
                    "latest": drafts[-1],
                    "drafts": drafts,
                }
            )
    manuscripts.sort(key=lambda item: item["name"].casefold())
    return manuscripts


class EventHub:
    def __init__(self):
        self.condition = threading.Condition()
        self.sequence = 0
        self.events: deque[tuple[int, dict]] = deque(maxlen=1000)

    def publish(self, event_type: str, **data: object) -> None:
        with self.condition:
            self.sequence += 1
            self.events.append(
                (self.sequence, {"type": event_type, **data})
            )
            self.condition.notify_all()

    def current_sequence(self) -> int:
        with self.condition:
            return self.sequence

    def wait(self, sequence: int, timeout: float = 15) -> tuple[int, dict | None]:
        with self.condition:
            if self.sequence <= sequence:
                self.condition.wait(timeout)
            if self.sequence <= sequence:
                return sequence, None
            for event_sequence, event in self.events:
                if event_sequence > sequence:
                    return event_sequence, dict(event)
            # The client fell beyond the retained history; the newest event
            # will make it reconcile from the authoritative APIs.
            event_sequence, event = self.events[-1]
            return event_sequence, dict(event)


class CatalogManager:
    def __init__(
        self,
        paths: list[Path],
        manuscripts: Path,
        hub: EventHub,
        cache_path: Path | None = None,
    ):
        self.paths = paths
        self.manuscripts = manuscripts
        self.hub = hub
        self.cache_path = cache_path
        self.cache_key = {
            "paths": sorted(str(path.resolve()) for path in paths),
            "manuscripts": str(manuscripts.resolve()),
        }
        self.lock = threading.RLock()
        self.version = 0
        self.error = ""
        self.catalog: dict = {
            "papers": [],
            "reviews": [],
            "manuscripts": [],
            "counts": {
                "papers": 0,
                "problems": 0,
                "attempts": 0,
                "manuscripts": 0,
            },
            "version": 0,
            "error": "",
            "loading": True,
        }
        self.fingerprint = ""
        self._load_cache()
        self.pending = threading.Event()
        self.force_refresh = False
        self.stopping = threading.Event()
        self.ready = threading.Event()
        self.thread = threading.Thread(
            target=self._refresh_loop,
            name="catalog-refresh",
            daemon=True,
        )
        self.thread.start()
        self.pending.set()

    def _load_cache(self) -> None:
        if self.cache_path is None:
            return
        cached = common.load_json(self.cache_path)
        if (
            cached is None
            or cached.get("schemaVersion") != CATALOG_CACHE_SCHEMA_VERSION
            or cached.get("key") != self.cache_key
            or not isinstance(cached.get("catalog"), dict)
            or not isinstance(cached.get("fingerprint"), str)
        ):
            return
        catalog = cached["catalog"]
        if not all(
            isinstance(catalog.get(name), list)
            for name in ("papers", "reviews", "manuscripts")
        ):
            return
        self.catalog = catalog
        # Cached data is immediately usable; validation happens silently in
        # the background rather than becoming another initial load.
        self.catalog["loading"] = False
        self.catalog["error"] = ""
        self.catalog.pop("progress", None)
        self.version = int(catalog.get("version", 0))
        self.fingerprint = cached["fingerprint"]

    def _save_cache(self) -> None:
        if self.cache_path is None:
            return
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.cache_path.with_suffix(
            self.cache_path.suffix + ".tmp"
        )
        common.write_json(
            temporary,
            {
                "schemaVersion": CATALOG_CACHE_SCHEMA_VERSION,
                "key": self.cache_key,
                "fingerprint": self.fingerprint,
                "catalog": self.catalog,
            },
        )
        temporary.replace(self.cache_path)

    def refresh(self, *, force: bool = False) -> None:
        try:
            self._set_progress("papers", "Scanning papers…")
            papers = _paper_inventory(self.paths)
            self._set_progress("problems", "Discovering open problems…")

            last_progress = -25
            last_stage_progress: dict[str, int] = {}

            def review_progress(current: int, total: int) -> None:
                nonlocal last_progress
                if current == 0 or current == total or current - last_progress >= 25:
                    last_progress = current
                    self._set_progress(
                        "reviews",
                        "Building review catalog…",
                        current=current,
                        total=total,
                    )

            def review_stage_progress(
                label: str,
                current: int,
                total: int,
            ) -> None:
                previous = last_stage_progress.get(label, -25)
                if current == 0 or current == total or current - previous >= 25:
                    last_stage_progress[label] = current
                    self._set_progress(
                        "reviews",
                        label,
                        current=current,
                        total=total,
                    )

            reviews = _review_inventory(
                self.paths,
                progress=review_progress,
                stage_progress=review_stage_progress,
            )
            def manuscript_progress(current: int, total: int) -> None:
                self._set_progress(
                    "manuscripts",
                    "Reading manuscripts…",
                    current=current,
                    total=total,
                )

            manuscripts = _manuscript_inventory(
                self.manuscripts,
                papers,
                reviews,
                progress=manuscript_progress,
            )
            self._set_progress("finalizing", "Preparing catalog…")
            value = {
                "papers": papers,
                "reviews": reviews,
                "manuscripts": manuscripts,
                "counts": {
                    "papers": len(papers),
                    "problems": len({item["problemKey"] for item in reviews}),
                    "attempts": sum(item["attemptStatus"] != "unattempted" for item in reviews),
                    "manuscripts": len(manuscripts),
                },
                "loading": False,
            }
        except (OSError, UnicodeError, common.CodexError, json.JSONDecodeError) as exc:
            with self.lock:
                self.error = str(exc)
                self.catalog["error"] = self.error
                self.catalog["loading"] = False
            self.ready.set()
            self.hub.publish("catalog.error", message=str(exc))
            return
        fingerprint = hashlib.sha256(
            json.dumps(value, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()
        changed = fingerprint != self.fingerprint or force
        with self.lock:
            if not changed:
                self.error = ""
                self.catalog["error"] = ""
                self.catalog["loading"] = False
                self.catalog.pop("progress", None)
            else:
                self.fingerprint = fingerprint
                self.version += 1
                value["version"] = self.version
                value["error"] = ""
                self.catalog = value
                self.error = ""
        try:
            self._save_cache()
        except (OSError, UnicodeError):
            pass
        self.ready.set()
        if changed:
            self.hub.publish("catalog.changed", version=self.version)

    def _set_progress(
        self,
        phase: str,
        label: str,
        *,
        current: int | None = None,
        total: int | None = None,
    ) -> None:
        progress = {"phase": phase, "label": label}
        if current is not None and total is not None:
            progress.update({"current": current, "total": total})
        initial_load = not self.version
        with self.lock:
            if initial_load:
                self.catalog["loading"] = True
                self.catalog["progress"] = progress
        if initial_load:
            self.hub.publish("catalog.progress", **progress)

    def schedule(self) -> None:
        self.force_refresh = True
        self.pending.set()

    def wait_until_ready(self, timeout: float | None = None) -> bool:
        return self.ready.wait(timeout)

    def _refresh_loop(self) -> None:
        while not self.stopping.is_set():
            if not self.pending.wait(0.5):
                continue
            self.pending.clear()
            # Coalesce installation bursts produced by atomic directory moves.
            if self.stopping.wait(0.3):
                break
            self.pending.clear()
            force = self.force_refresh
            self.force_refresh = False
            self.refresh(force=force)

    def snapshot(self) -> dict:
        with self.lock:
            value = json.loads(json.dumps(self.catalog, ensure_ascii=False))
            value["error"] = self.error
            for item in value.get("reviews", []):
                for field in (
                    "externalSources",
                    "claimReviews",
                    "blockingGaps",
                    "recommendedNextSteps",
                    "warnings",
                    "files",
                ):
                    item.pop(field, None)
            return value

    def review_detail(self, key: str) -> dict:
        with self.lock:
            item = next(
                (row for row in self.catalog.get("reviews", []) if row["itemKey"] == key),
                None,
            )
            if item is None:
                raise KeyError(key)
            value = dict(item)
        value.update(human_review.load_review_contents(value))
        value["files"] = human_review.load_review_files(value)
        value["fileCount"] = len(value["files"])
        return value

    def close(self) -> None:
        self.stopping.set()
        self.pending.set()
        self.thread.join(timeout=5)


class ChangeHandler(FileSystemEventHandler):
    def __init__(self, catalog: CatalogManager):
        self.catalog = catalog
        self.signatures: dict[str, tuple[int, int] | None] = {}

    @staticmethod
    def ignored(path: str) -> bool:
        parts = Path(path).parts
        return any(
            part in IGNORED_PARTS or part.startswith(IGNORED_PREFIXES)
            for part in parts
        )

    @staticmethod
    def relevant_file(path: str) -> bool:
        value = Path(path)
        parts = {part.casefold() for part in value.parts}
        return (
            value.name.casefold() in CATALOG_FILE_NAMES
            or "artifacts" in parts
        )

    @staticmethod
    def signature(path: str) -> tuple[int, int] | None:
        try:
            status = Path(path).stat()
        except OSError:
            return None
        return status.st_mtime_ns, status.st_size

    def on_any_event(self, event: FileSystemEvent) -> None:
        if getattr(event, "event_type", "") in {
            "opened",
            "closed",
            "closed_no_write",
        }:
            return
        # File changes already carry the useful event.  The extra directory
        # mtime notification is noisy (and can be emitted independently by
        # filesystem services) without identifying changed catalog content.
        if (
            getattr(event, "event_type", "") == "modified"
            and getattr(event, "is_directory", False)
        ):
            return
        paths = [event.src_path]
        destination = getattr(event, "dest_path", "")
        if destination:
            paths.append(destination)
        paths = [path for path in paths if not self.ignored(path)]
        if not paths:
            return
        if not getattr(event, "is_directory", False):
            paths = [path for path in paths if self.relevant_file(path)]
            if not paths:
                return
        if getattr(event, "event_type", "") == "modified":
            changed = False
            for path in paths:
                key = os.path.normcase(os.path.abspath(path))
                signature = self.signature(path)
                if self.signatures.get(key, object()) != signature:
                    changed = True
                self.signatures[key] = signature
            if not changed:
                return
        self.catalog.schedule()


class TaskChangeHandler(FileSystemEventHandler):
    """Wake the task scheduler when SQLite or its WAL records a commit."""

    def __init__(self, scheduler: "Scheduler", database: Path):
        self.scheduler = scheduler
        database_name = database.name.casefold()
        self.database_names = {
            database_name,
            f"{database_name}-wal",
            f"{database_name}-journal",
        }

    def on_any_event(self, event: FileSystemEvent) -> None:
        if getattr(event, "event_type", "") in {
            "opened",
            "closed",
            "closed_no_write",
        } or getattr(event, "is_directory", False):
            return
        paths = [event.src_path]
        destination = getattr(event, "dest_path", "")
        if destination:
            paths.append(destination)
        for path in paths:
            name = Path(path).name.casefold()
            if name in self.database_names:
                self.scheduler.schedule()
                return


class Scheduler:
    def __init__(
        self,
        store: WorkbenchStore,
        hub: EventHub,
        *,
        state_directory: Path,
    ):
        self.store = store
        self.hub = hub
        self.state_directory = state_directory
        self.stopping = threading.Event()
        self.pending = threading.Event()
        self.last_launch = 0.0
        self.revision = self.store.revision()
        self.pending.set()
        self.thread = threading.Thread(
            target=self._loop,
            name="workbench-scheduler",
            daemon=True,
        )
        self.thread.start()

    def _launch(self, run: dict) -> None:
        command = [
            sys.executable,
            "-u",
            str(PROJECT_ROOT / "src" / "workbench_worker.py"),
            "--database",
            str(self.store.database),
            "--run",
            run["id"],
        ]
        environment = os.environ.copy()
        environment["LOOSE_ENDS_CODEX_LAUNCH_GATE"] = str(
            self.state_directory / "codex-launch.lock"
        )
        options: dict = {
            "cwd": PROJECT_ROOT,
            "env": environment,
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
        }
        options.update(codex_cli.windowless_popen_options())
        try:
            subprocess.Popen(command, **options)
        except OSError as exc:
            self.store.update_run(
                run["id"],
                status="failed",
                finished_at=time.time(),
                error=f"could not start worker: {exc}",
            )
        self.last_launch = time.monotonic()
        self.revision = self.store.revision()
        self.hub.publish("tasks.changed", revision=self.revision)

    def schedule(self) -> None:
        self.pending.set()

    def _check(self) -> float | None:
        stale = self.store.mark_stale_runs(older_than=12)
        for run_id in stale:
            run = self.store.get_run(run_id)
            recover_run_artifacts(self.store, run)
            run = self.store.get_run(run_id)
            if run["outputs"]:
                self.store.update_run(
                    run_id,
                    status="partial",
                    error="worker stopped after reporting installed output",
                )
        current_revision = self.store.revision()
        if current_revision != self.revision:
            self.revision = current_revision
            self.hub.publish("tasks.changed", revision=self.revision)

        settings = self.store.scheduler_settings()
        active_count = self.store.active_count()
        slots = settings["workerLimit"] - active_count
        if slots > 0 and not settings["queuePaused"]:
            launch_delay = max(
                0.0,
                1.05 - (time.monotonic() - self.last_launch),
            )
            if launch_delay:
                return launch_delay
            run = self.store.claim_next_run(self.store.active_resources())
            if run is not None:
                self._launch(run)
                return 1.05
        # An active worker normally wakes us through SQLite WAL events.  This
        # timeout only detects a worker that died without another commit.
        if active_count:
            return 12.0
        return None

    def _loop(self) -> None:
        wake_after: float | None = None
        while True:
            signaled = self.pending.wait(wake_after)
            if self.stopping.is_set():
                break
            if signaled:
                self.pending.clear()
                # A SQLite commit can modify the WAL several times.
                if self.stopping.wait(0.05):
                    break
                self.pending.clear()
            wake_after = self._check()

    def close(self) -> None:
        self.stopping.set()
        self.pending.set()
        self.thread.join(timeout=5)


class WorkbenchApplication:
    def __init__(
        self,
        *,
        paths: list[Path],
        manuscripts: Path,
        state_directory: Path,
    ):
        self.paths = paths
        self.manuscripts = manuscripts
        self.state_directory = state_directory
        self.allowed_roots = [*paths, manuscripts]
        self.hub = EventHub()
        self.store = WorkbenchStore(
            state_directory / "workbench.sqlite3",
            state_directory,
        )
        self.catalog = CatalogManager(
            paths,
            manuscripts,
            self.hub,
            cache_path=state_directory / "catalog-cache.json",
        )
        self.scheduler = Scheduler(
            self.store,
            self.hub,
            state_directory=state_directory,
        )
        self.csrf = secrets.token_urlsafe(24)
        self.plans: dict[str, dict] = {}
        self.plan_lock = threading.Lock()
        self.manuscript_lock = threading.Lock()
        self.observer = None

    def start_watching(self) -> None:
        if Observer is None:
            raise PlanError(
                "watchdog is required; install dependencies with "
                "python -m pip install -r requirements.txt"
            )
        observer = Observer()
        handler = ChangeHandler(self.catalog)
        roots: list[Path] = []
        for candidate in [*self.paths, self.manuscripts]:
            candidate = candidate.resolve()
            if not candidate.exists():
                candidate.mkdir(parents=True, exist_ok=True)
            if any(
                candidate == existing or existing in candidate.parents
                for existing in roots
            ):
                continue
            roots = [root for root in roots if candidate not in root.parents]
            roots.append(candidate)
        for root in roots:
            observer.schedule(handler, str(root), recursive=True)
        observer.schedule(
            TaskChangeHandler(self.scheduler, self.store.database),
            str(self.state_directory.resolve()),
            recursive=False,
        )
        observer.start()
        self.observer = observer

    def create_plan(self, request: dict) -> dict:
        plan = build_plan(
            request,
            project_root=PROJECT_ROOT,
            allowed_roots=self.allowed_roots,
            manuscripts=self.manuscripts,
            catalog_version=self.catalog.version,
        )
        populate_dry_run_previews(plan)
        with self.plan_lock:
            self.plans[plan["id"]] = {
                "created": time.time(),
                "request": request,
                "plan": plan,
            }
            expired = [
                key
                for key, value in self.plans.items()
                if value["created"] < time.time() - 1800
            ]
            for key in expired:
                self.plans.pop(key, None)
        return plan

    def confirm_plan(self, plan_id: str) -> dict:
        with self.plan_lock:
            saved = self.plans.pop(plan_id, None)
        if saved is None:
            raise PlanError("this plan expired; review the task again")
        job = self.store.create_job(saved["request"], saved["plan"])
        self.scheduler.schedule()
        self.hub.publish("tasks.changed")
        return job

    def set_manuscript_pinning(self, request: dict) -> dict:
        draft_value = request.get("draft")
        pinned = request.get("pinned")
        problem_value = request.get("problem")
        if not isinstance(draft_value, str) or not draft_value:
            raise PlanError("draft is required")
        if not isinstance(pinned, bool):
            raise PlanError("pinned must be true or false")
        if not isinstance(problem_value, str) or not problem_value:
            raise PlanError("problem is required")
        draft = Path(draft_value).expanduser().resolve()
        problem = Path(problem_value).expanduser().resolve()
        if not _relative_to(draft, self.manuscripts.resolve()):
            raise PlanError("draft is outside the manuscript directory")
        if not any(_relative_to(problem, root.resolve()) for root in self.paths):
            raise PlanError("problem is outside the configured paper directories")
        try:
            with self.manuscript_lock:
                result = write_paper.set_input_pinning(
                    draft,
                    pinned=pinned,
                    problem_directory=problem,
                )
                manifest = common.read_json(
                    draft / "manifest.json",
                    description=f"draft manifest for {draft}",
                )
        except common.CodexError as exc:
            raise PlanError(str(exc)) from exc
        catalog = self.catalog.snapshot()
        result["sources"] = _manuscript_sources(
            manifest,
            catalog.get("papers", []),
            catalog.get("reviews", []),
        )
        self.catalog.schedule()
        return result

    def close(self) -> None:
        if self.observer is not None:
            self.observer.stop()
            self.observer.join(timeout=5)
        self.scheduler.close()
        self.catalog.close()


def _is_allowed_file(path: Path, roots: Iterable[Path]) -> bool:
    resolved = path.resolve()
    return any(
        _relative_to(resolved, root.resolve())
        for root in roots
    )


def _relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _download_filename(value: str, fallback: str) -> str:
    """Return an ASCII, header-safe basename for a downloaded artifact."""
    name = re.sub(r'[\x00-\x1f\x7f"\\/]+', "_", value).strip(" .")
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return name or fallback


LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}
WILDCARD_HOSTS = {"0.0.0.0", "::"}


def _request_hostname_allowed(
    hostname: str | None,
    *,
    network_enabled: bool,
    allowed_hostnames: set[str],
) -> bool:
    if not hostname:
        return False
    normalized = hostname.casefold().rstrip(".")
    if normalized in LOOPBACK_HOSTS or normalized in allowed_hostnames:
        return True
    if not network_enabled:
        return False
    try:
        ipaddress.ip_address(normalized)
    except ValueError:
        return False
    return True


def _same_request_origin(host_header: str, origin: str) -> bool:
    try:
        host = urlsplit(f"//{host_header}")
        source = urlsplit(origin)
        host_port = host.port
        source_port = source.port
    except ValueError:
        return False
    if (
        source.scheme not in {"http", "https"}
        or not host.hostname
        or not source.hostname
    ):
        return False
    if source.hostname.casefold().rstrip(".") != host.hostname.casefold().rstrip("."):
        return False
    if host_port is None:
        return True
    expected_source_port = source_port or (443 if source.scheme == "https" else 80)
    return expected_source_port == host_port


class WorkbenchHandler(BaseHTTPRequestHandler):
    server_version = "LooseEndsWorkbench/1"
    protocol_version = "HTTP/1.1"

    @property
    def app(self) -> WorkbenchApplication:
        return self.server.app  # type: ignore[attr-defined]

    def log_message(self, format: str, *args: object) -> None:
        print(f"{self.address_string()} - {format % args}", file=sys.stderr)

    def _host_is_allowed(self) -> bool:
        host = self.headers.get("Host", "")
        try:
            hostname = urlsplit(f"//{host}").hostname
        except ValueError:
            return False
        return _request_hostname_allowed(
            hostname,
            network_enabled=self.server.network_enabled,  # type: ignore[attr-defined]
            allowed_hostnames=self.server.allowed_hostnames,  # type: ignore[attr-defined]
        )

    def _origin_is_allowed(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return True
        return self._host_is_allowed() and _same_request_origin(
            self.headers.get("Host", ""),
            origin,
        )

    def _headers(self, status: int, content_type: str, length: int | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        if self.close_connection:
            self.send_header("Connection", "close")
        if length is not None:
            self.send_header("Content-Length", str(length))
        self.end_headers()

    def send_json(self, value: object, status: int = 200) -> None:
        data = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self._headers(status, "application/json; charset=utf-8", len(data))
        self.wfile.write(data)

    def send_error_json(
        self,
        status: int,
        message: str,
        *,
        code: str | None = None,
    ) -> None:
        value = {"error": message}
        if code is not None:
            value["code"] = code
        self.send_json(value, status)

    def read_json(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            self.close_connection = True
            raise PlanError("invalid content length") from exc
        if length <= 0 or length > 2_000_000:
            self.close_connection = True
            raise PlanError("invalid request body")
        data = self.rfile.read(length)
        if len(data) != length:
            self.close_connection = True
            raise PlanError("incomplete request body")
        try:
            value = json.loads(data)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise PlanError("request body must be valid JSON") from exc
        if not isinstance(value, dict):
            raise PlanError("request body must be an object")
        return value

    def require_mutation_auth(self) -> bool:
        if not self._origin_is_allowed():
            self.close_connection = True
            self.send_error_json(403, "untrusted request origin")
            return False
        if self.headers.get("X-Workbench-CSRF") != self.app.csrf:
            self.close_connection = True
            self.send_error_json(
                403,
                "browser session expired; refresh and try again",
                code="invalid_confirmation_token",
            )
            return False
        return True

    def do_GET(self) -> None:
        if not self._host_is_allowed():
            self.send_error_json(403, "untrusted host")
            return
        parsed = urlsplit(self.path)
        path = parsed.path
        try:
            if path == "/api/bootstrap":
                event_sequence = self.app.hub.current_sequence()
                self.send_json(
                    {
                        "eventSequence": event_sequence,
                        "csrf": self.app.csrf,
                        "catalog": self.app.catalog.snapshot(),
                        "jobs": self.app.store.list_jobs(),
                        "settings": {
                            **self.app.store.scheduler_settings(),
                            "projectRoot": str(PROJECT_ROOT),
                        },
                    }
                )
            elif path == "/api/catalog":
                self.send_json(self.app.catalog.snapshot())
            elif path == "/api/review-detail":
                key = parse_qs(parsed.query).get("key", [""])[0]
                self.send_json(self.app.catalog.review_detail(key))
            elif path == "/api/jobs":
                self.send_json({"jobs": self.app.store.list_jobs()})
            elif match := re.fullmatch(r"/api/jobs/([0-9a-f-]+)", path):
                self.send_json(self.app.store.get_job(match.group(1)))
            elif match := re.fullmatch(r"/api/runs/([0-9a-f-]+)/log", path):
                self._send_log(match.group(1), parsed.query)
            elif path == "/api/events":
                self._send_events(parsed.query)
            elif path == "/api/file":
                values = parse_qs(parsed.query)
                self._send_file(
                    values.get("path", [""])[0],
                    raw=values.get("raw", [""])[0] == "1",
                    download=values.get("download", [""])[0] == "1",
                    filename=values.get("name", [""])[0] or None,
                )
            elif path == "/api/manuscript-zip":
                values = parse_qs(parsed.query)
                self._send_manuscript_zip(values.get("path", [""])[0])
            elif path == "/view":
                self._send_asset("viewer.html")
            elif path in {
                "/", "/index.html", "/research", "/papers",
                "/manuscripts", "/activity",
            }:
                self._send_asset("index.html")
            elif path in {
                "/app.js", "/review_model.js", "/review_tokens.css",
                "/styles.css", "/viewer.js",
            }:
                self._send_asset(path.removeprefix("/"))
            else:
                self.send_error_json(404, "not found")
        except KeyError:
            self.send_error_json(404, "record not found")
        except (OSError, UnicodeError) as exc:
            self.send_error_json(500, str(exc))

    def do_POST(self) -> None:
        if not self._host_is_allowed():
            self.close_connection = True
            self.send_error_json(403, "untrusted host")
            return
        parsed = urlsplit(self.path)
        try:
            body = self.read_json()
            if not self.require_mutation_auth():
                return
            if parsed.path == "/api/plans":
                self.send_json(self.app.create_plan(body), 201)
            elif parsed.path == "/api/jobs":
                plan_id = body.get("planId")
                if not isinstance(plan_id, str):
                    raise PlanError("planId is required")
                self.send_json(self.app.confirm_plan(plan_id), 201)
            elif parsed.path == "/api/scheduler":
                changes = {}
                if "workerLimit" in body:
                    changes["worker_limit"] = body["workerLimit"]
                if "queuePaused" in body:
                    changes["queue_paused"] = body["queuePaused"]
                settings = self.app.store.update_scheduler_settings(**changes)
                self.send_json(settings)
                self.app.scheduler.schedule()
                self.app.hub.publish("settings.changed", **settings)
            elif parsed.path == "/api/manuscripts/pinning":
                self.send_json(self.app.set_manuscript_pinning(body))
            elif match := re.fullmatch(
                r"/api/jobs/([0-9a-f-]+)/scheduling", parsed.path
            ):
                changes = {}
                if "priorityLevel" in body:
                    changes["priority_level"] = body["priorityLevel"]
                if "paused" in body:
                    changes["paused"] = body["paused"]
                job = self.app.store.update_job_scheduling(
                    match.group(1), **changes
                )
                self.send_json(job)
                self.app.scheduler.schedule()
                self.app.hub.publish("tasks.changed")
            elif match := re.fullmatch(r"/api/runs/([0-9a-f-]+)/cancel", parsed.path):
                self.send_json(self.app.store.request_cancel(match.group(1)))
                self.app.scheduler.schedule()
                self.app.hub.publish("tasks.changed")
            elif match := re.fullmatch(r"/api/runs/([0-9a-f-]+)/retry", parsed.path):
                self.send_json(self.app.store.retry_run(match.group(1)), 201)
                self.app.scheduler.schedule()
                self.app.hub.publish("tasks.changed")
            else:
                self.send_error_json(404, "not found")
        except KeyError:
            self.send_error_json(404, "record not found")
        except PlanError as exc:
            self.send_error_json(400, str(exc))
        except ValueError as exc:
            self.send_error_json(409, str(exc))
        except RuntimeError as exc:
            self.send_error_json(409, str(exc))

    def _send_asset(self, name: str) -> None:
        path = ASSET_DIRECTORY / name
        if not path.is_file():
            self.send_error_json(404, "asset not found")
            return
        data = path.read_bytes()
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if path.suffix in {".js", ".css", ".html"}:
            content_type += "; charset=utf-8"
        self._headers(200, content_type, len(data))
        self.wfile.write(data)

    def _send_file(
        self,
        value: str,
        *,
        raw: bool = False,
        download: bool = False,
        filename: str | None = None,
    ) -> None:
        if not value:
            self.send_error_json(400, "path is required")
            return
        path = Path(unquote(value)).expanduser().resolve()
        if not _is_allowed_file(path, self.app.allowed_roots) or not path.is_file():
            self.send_error_json(404, "file is unavailable")
            return
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if raw:
            content_type = "text/plain; charset=utf-8"
            disposition = "inline"
        elif download:
            disposition = "attachment"
        elif content_type in {"text/html", "image/svg+xml"}:
            content_type = "application/octet-stream"
            disposition = "attachment"
        else:
            disposition = "inline"
        size = path.stat().st_size
        start = 0
        end = size - 1
        status = 200
        range_header = self.headers.get("Range")
        if range_header and (match := re.fullmatch(r"bytes=(\d*)-(\d*)", range_header)):
            if match.group(1):
                start = int(match.group(1))
            if match.group(2):
                end = min(int(match.group(2)), end)
            if start > end or start >= size:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
            status = 206
        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        response_name = _download_filename(filename or path.name, path.name)
        self.send_header(
            "Content-Disposition", f'{disposition}; filename="{response_name}"'
        )
        self.send_header("X-Content-Type-Options", "nosniff")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        with path.open("rb") as source:
            source.seek(start)
            remaining = length
            while remaining:
                chunk = source.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def _send_manuscript_zip(self, value: str) -> None:
        if not value:
            self.send_error_json(400, "path is required")
            return
        draft = Path(unquote(value)).expanduser().resolve()
        manuscripts = self.app.manuscripts.resolve()
        if (
            not _relative_to(draft, manuscripts)
            or not draft.is_dir()
            or DRAFT_RE.fullmatch(draft.name) is None
        ):
            self.send_error_json(404, "manuscript draft is unavailable")
            return

        archive_root = f"{draft.parent.name}-{draft.name}"
        with tempfile.TemporaryFile() as temporary:
            with zipfile.ZipFile(
                temporary, "w", compression=zipfile.ZIP_DEFLATED
            ) as archive:
                for candidate in sorted(draft.rglob("*")):
                    if candidate.is_symlink() or not candidate.is_file():
                        continue
                    resolved = candidate.resolve()
                    if not _relative_to(resolved, draft):
                        continue
                    relative = candidate.relative_to(draft)
                    archive.write(
                        resolved,
                        arcname=(Path(archive_root) / relative).as_posix(),
                    )
            size = temporary.tell()
            temporary.seek(0)
            filename = _download_filename(f"{archive_root}.zip", "manuscript.zip")
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Length", str(size))
            self.send_header(
                "Content-Disposition", f'attachment; filename="{filename}"'
            )
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            while chunk := temporary.read(1024 * 1024):
                self.wfile.write(chunk)

    def _send_log(self, run_id: str, query: str) -> None:
        run = self.app.store.get_run(run_id)
        values = parse_qs(query)
        try:
            offset = max(0, int(values.get("offset", ["0"])[0]))
        except ValueError:
            offset = 0
        path = Path(run["log_path"])
        if not path.is_file():
            text = ""
            next_offset = 0
        else:
            size = path.stat().st_size
            offset = min(offset, size)
            with path.open("rb") as source:
                source.seek(offset)
                data = source.read(512_000)
            text = data.decode("utf-8", errors="replace")
            next_offset = offset + len(data)
        self.send_json(
            {
                "text": text,
                "nextOffset": next_offset,
                "complete": run["status"] not in ACTIVE_STATUSES and run["status"] != "queued",
            }
        )

    def _send_events(self, query: str = "") -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        values = parse_qs(query)
        initial_sequence = values.get("since", ["0"])[0]
        try:
            sequence = int(
                self.headers.get("Last-Event-ID", initial_sequence)
            )
        except ValueError:
            sequence = 0
        try:
            while True:
                sequence, event = self.app.hub.wait(sequence)
                if event is None:
                    self.wfile.write(b": keepalive\n\n")
                else:
                    payload = json.dumps(event, ensure_ascii=False)
                    self.wfile.write(
                        f"id: {sequence}\nevent: update\ndata: {payload}\n\n".encode("utf-8")
                    )
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            self.close_connection = True
            return


class WorkbenchHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        app: WorkbenchApplication,
        *,
        allowed_hostnames: Iterable[str] = (),
    ):
        super().__init__(address, WorkbenchHandler)
        self.app = app
        bind_host = address[0].casefold().rstrip(".")
        self.network_enabled = bind_host not in LOOPBACK_HOSTS
        machine_names = {
            socket.gethostname().casefold().rstrip("."),
            socket.getfqdn().casefold().rstrip("."),
        }
        self.allowed_hostnames = {
            value.casefold().rstrip(".")
            for value in {*machine_names, *allowed_hostnames, bind_host}
            if value and value not in WILDCARD_HOSTS
        }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="serve a live research dashboard and manage CLI runs"
    )
    parser.add_argument(
        "paths",
        nargs="+",
        type=Path,
        help="paper directories or parent directories containing papers",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help=(
            "address to bind (default: 127.0.0.1); use 0.0.0.0 to expose "
            "the workbench on the local network"
        ),
    )
    parser.add_argument(
        "--allowed-host",
        action="append",
        default=[],
        metavar="NAME",
        help="additional trusted hostname for network access; may be repeated",
    )
    parser.add_argument("--port", type=int, default=35007)
    parser.add_argument(
        "--manuscripts",
        type=Path,
        default=DEFAULT_MANUSCRIPTS,
    )
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=DEFAULT_STATE_DIRECTORY,
    )
    parser.add_argument("--no-open", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    codex_cli.configure_utf8_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)
    if Observer is None:
        return codex_cli.report_error(
            parser,
            PlanError(
                "watchdog is required; run python -m pip install -r requirements.txt"
            ),
        )
    paths = [path.expanduser().resolve() for path in args.paths]
    manuscripts = args.manuscripts.expanduser().resolve()
    state_directory = args.state_dir.expanduser().resolve()
    try:
        app = WorkbenchApplication(
            paths=paths,
            manuscripts=manuscripts,
            state_directory=state_directory,
        )
        app.start_watching()
        server = WorkbenchHTTPServer(
            (args.host, args.port),
            app,
            allowed_hostnames=args.allowed_host,
        )
    except (OSError, common.CodexError) as exc:
        return codex_cli.report_error(parser, exc)
    host, port = server.server_address[:2]
    url_host = "localhost" if host in LOOPBACK_HOSTS | WILDCARD_HOSTS else host
    if ":" in url_host and not url_host.startswith("["):
        url_host = f"[{url_host}]"
    url = f"http://{url_host}:{port}/"
    print(f"Loose Ends workbench: {url}", flush=True)
    if server.network_enabled:
        print(
            f"Network access enabled on {args.host}:{port}; anyone who can "
            "reach this port can view project data and start tasks.",
            flush=True,
        )
    if not args.no_open:
        try:
            human_review.open_in_browser(url)
        except OSError as exc:
            print(
                f"Could not open the browser automatically: {exc}",
                file=sys.stderr,
            )
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("\nStopping workbench.")
    finally:
        server.server_close()
        app.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
