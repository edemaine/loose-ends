#!/usr/bin/env python3
"""Live local dashboard and persistent task manager for Loose Ends."""

from __future__ import annotations

import argparse
from collections import deque
from datetime import datetime
from functools import lru_cache
import hashlib
import ipaddress
import json
import mimetypes
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import shutil
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
import download_arxiv
import download_arxiv_author
import human_review
import ingest_paper
import open_problem_common as common
import review_solutions
import write_paper
import visualizations
from workbench_store import ACTIVE_STATUSES, WorkbenchStore
from workbench_memory import QUEUE_JOB_ENV, QueueMemoryController
from workbench_worker import recover_run_artifacts
from workbench_tasks import (
    PlanError,
    build_plan,
    populate_dry_run_previews,
    task_cli_defaults,
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
    ".visualization-install-",
    ".visualization-review-run-",
    ".visualize-run-",
    ".paper-install-",
    ".triage-install-",
)
DRAFT_RE = re.compile(r"^draft-([0-9]{3,})$")
CATALOG_CACHE_SCHEMA_VERSION = 3
ROOT_CACHE_DIRECTORY = ".loose-ends"
PAPER_CACHE_FILENAME = "workbench-papers.json"
MANUSCRIPT_CACHE_FILENAME = "workbench-manuscripts.json"
PAPER_IMPORT_MAX_FILE_BYTES = 2 * 1024 * 1024 * 1024
PAPER_IMPORT_MAX_TOTAL_BYTES = 4 * 1024 * 1024 * 1024
PAPER_IMPORT_MAX_FILES = 20_000
PAPER_IMPORT_TTL_SECONDS = 60 * 60
WORKER_HEARTBEAT_STALE_SECONDS = 5 * 60
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
    visualizations.MANIFEST_NAME,
    visualizations.REVIEW_NAME,
    visualizations.CRITIQUE_NAME,
    "index.html",
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
    title = metadata.get("title") or analysis.get("paper_title") or paper.name
    authors = metadata.get("authors") or analysis.get("paper_authors") or []
    if not isinstance(authors, list):
        authors = []
    return str(title), [str(author) for author in authors]


def _url_key(path: Path) -> str:
    """Return the same portable project-relative identity used by reviews."""
    return human_review.project_display_path(path)


def _paper_url(metadata: dict) -> str:
    url = metadata.get("url", "")
    if isinstance(url, str) and url.strip():
        return url
    arxiv_id = metadata.get("arxiv_id", "")
    if isinstance(arxiv_id, str) and arxiv_id.strip():
        try:
            return download_arxiv.arxiv_abs_url(arxiv_id)
        except ValueError:
            pass
    return ""


def _paper_inventory(paths: Iterable[Path]) -> list[dict]:
    try:
        papers = analyze_papers.discover_paper_directories(paths)
    except analyze_papers.AnalysisError as exc:
        if "no installed paper directories" in str(exc):
            return []
        raise
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
                "arxivId": metadata.get("arxiv_id", "")
                if isinstance(metadata.get("arxiv_id", ""), str) else "",
                "url": _paper_url(metadata),
                "doi": metadata.get("doi", "")
                if isinstance(metadata.get("doi", ""), str) else "",
                "activityTimestamp": timeline["activityTimestamp"],
                "analyzed": bool(manifest),
                "problemCount": len(manifest.get("open_problems", [])),
                "analysisStatus": manifest.get("status", ""),
                "metadataComplete": bool(
                    isinstance(metadata.get("title"), str)
                    and metadata["title"].strip()
                    and isinstance(metadata.get("authors"), list)
                    and metadata["authors"]
                ),
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


def _catalog_cache_path(root: Path, filename: str) -> Path:
    return root / ROOT_CACHE_DIRECTORY / filename


@lru_cache(maxsize=1)
def _catalog_implementation_signature() -> str:
    """Invalidate root caches when catalog semantics or prompts change."""
    digest = hashlib.sha256()
    files = [
        *sorted((PROJECT_ROOT / "src").glob("*.py")),
        *sorted((PROJECT_ROOT / "prompts").rglob("*")),
        *sorted((PROJECT_ROOT / "schemas").rglob("*")),
    ]
    for path in files:
        if not path.is_file():
            continue
        relative = path.relative_to(PROJECT_ROOT).as_posix()
        digest.update(relative.encode("utf-8", errors="surrogateescape"))
        digest.update(b"\0")
        try:
            digest.update(path.read_bytes())
        except OSError:
            digest.update(b"<unavailable>")
        digest.update(b"\0")
    return digest.hexdigest()


def _catalog_root_signature(root: Path) -> str:
    """Fingerprint catalog-relevant filesystem state without parsing it."""
    digest = hashlib.sha256()
    if not root.is_dir():
        return digest.hexdigest()
    for directory, child_names, file_names in os.walk(root):
        child_names[:] = sorted(
            name for name in child_names
            if name not in IGNORED_PARTS
            and not name.startswith(IGNORED_PREFIXES)
        )
        current = Path(directory)
        for name in child_names:
            relative = (current / name).relative_to(root).as_posix()
            digest.update(f"D\0{relative}\0".encode("utf-8", errors="surrogateescape"))
        relative_parts = {
            part.casefold() for part in current.relative_to(root).parts
        }
        include_directory_files = bool(
            relative_parts.intersection(
                {"artifacts", "figures", "code", visualizations.DIRECTORY_NAME}
            )
        )
        for name in sorted(file_names):
            if (
                name.casefold() not in CATALOG_FILE_NAMES
                and not include_directory_files
            ):
                continue
            path = current / name
            try:
                status = path.stat()
            except OSError:
                continue
            relative = path.relative_to(root).as_posix()
            digest.update(
                f"F\0{relative}\0{status.st_mtime_ns}\0{status.st_size}\0".encode(
                    "utf-8", errors="surrogateescape"
                )
            )
    return digest.hexdigest()


def _review_inventory(
    paths: Iterable[Path],
    progress=None,
    stage_progress=None,
) -> list[dict]:
    try:
        problems = common.discover_problem_refs(paths)
    except common.CodexError as exc:
        if any(
            message in str(exc)
            for message in (
                "no installed paper directories",
                "none of the discovered papers has",
                "no open problems",
            )
        ):
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
    ):
        self.paths = list(dict.fromkeys(path.resolve() for path in paths))
        self.manuscripts = manuscripts.resolve()
        self.manuscripts.mkdir(parents=True, exist_ok=True)
        self.hub = hub
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
        self.paper_caches: dict[str, dict] = {}
        self.manuscript_cache: dict | None = None
        self._load_caches()
        self.pending = threading.Event()
        self.pending_lock = threading.Lock()
        self.force_refresh = False
        self.validate_all = True
        self.dirty_roots: set[str] = set()
        self.stopping = threading.Event()
        self.ready = threading.Event()
        self.thread = threading.Thread(
            target=self._refresh_loop,
            name="catalog-refresh",
            daemon=True,
        )
        self.thread.start()
        self.pending.set()

    @staticmethod
    def _root_key(root: Path) -> str:
        return os.path.normcase(str(root.resolve()))

    @staticmethod
    def _load_cache_file(path: Path, kind: str, root: Path) -> dict | None:
        cached = common.load_json(path)
        if (
            cached is None
            or cached.get("schemaVersion") != CATALOG_CACHE_SCHEMA_VERSION
            or cached.get("kind") != kind
            or cached.get("root") != str(root.resolve())
            or not isinstance(cached.get("signature"), str)
            or cached.get("implementationSignature")
            != _catalog_implementation_signature()
        ):
            return None
        if kind == "papers" and not all(
            isinstance(cached.get(name), list) for name in ("papers", "reviews")
        ):
            return None
        if kind == "manuscripts" and (
            not isinstance(cached.get("manuscripts"), list)
            or not isinstance(cached.get("dependencyFingerprint"), str)
        ):
            return None
        return cached

    @staticmethod
    def _save_cache_file(path: Path, value: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        ignore = path.parent / ".gitignore"
        if not ignore.exists():
            ignore.write_text("*\n", encoding="utf-8")
        temporary = path.with_suffix(path.suffix + ".tmp")
        common.write_json(temporary, value)
        temporary.replace(path)

    @staticmethod
    def _source_fingerprint(papers: list[dict], reviews: list[dict]) -> str:
        problems = {
            (
                str(item.get("paperDirectory", "")),
                str(item.get("problemId", "")),
            ): {
                "paperDirectory": item.get("paperDirectory"),
                "problemId": item.get("problemId"),
                "problemTitle": item.get("problemTitle"),
            }
            for item in reviews
        }
        dependencies = {
            "papers": [
                {"path": item.get("path"), "title": item.get("title")}
                for item in papers
            ],
            "problems": [problems[key] for key in sorted(problems)],
        }
        return hashlib.sha256(
            json.dumps(dependencies, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()

    @staticmethod
    def _merge_paper_caches(entries: Iterable[dict]) -> tuple[list[dict], list[dict]]:
        papers_by_key: dict[str, dict] = {}
        reviews_by_key: dict[str, dict] = {}
        for entry in entries:
            for item in entry.get("papers", []):
                papers_by_key[str(item.get("key") or item.get("path"))] = item
            for item in entry.get("reviews", []):
                reviews_by_key[str(item.get("itemKey") or item.get("id"))] = item
        papers = list(papers_by_key.values())
        papers.sort(key=lambda item: (str(item.get("title", "")).casefold(), item["path"]))
        reviews = list(reviews_by_key.values())
        reviews.sort(
            key=lambda item: (
                str(item.get("paperTitle", "")).casefold(),
                os.path.normcase(str(item.get("paperDirectory", ""))),
                str(item.get("problemId", "")),
                -int(item.get("attemptNumber", 0) or 0),
                str(item.get("itemKey", "")),
            )
        )
        return papers, reviews

    @staticmethod
    def _catalog_value(
        papers: list[dict],
        reviews: list[dict],
        manuscripts: list[dict],
        *,
        loading: bool,
    ) -> dict:
        return {
            "papers": papers,
            "reviews": reviews,
            "manuscripts": manuscripts,
            "counts": {
                "papers": len(papers),
                "problems": len({item["problemKey"] for item in reviews}),
                "attempts": sum(
                    item["attemptStatus"] != "unattempted" for item in reviews
                ),
                "manuscripts": len(manuscripts),
            },
            "loading": loading,
        }

    @staticmethod
    def _catalog_fingerprint(value: dict) -> str:
        comparable = {
            key: value[key]
            for key in ("papers", "reviews", "manuscripts", "counts")
        }
        return hashlib.sha256(
            json.dumps(comparable, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()

    def _load_caches(self) -> None:
        for root in self.paths:
            cached = self._load_cache_file(
                _catalog_cache_path(root, PAPER_CACHE_FILENAME),
                "papers",
                root,
            )
            if cached is not None:
                self.paper_caches[self._root_key(root)] = cached
        papers, reviews = self._merge_paper_caches(self.paper_caches.values())
        dependency = self._source_fingerprint(papers, reviews)
        manuscript_cache = self._load_cache_file(
            _catalog_cache_path(self.manuscripts, MANUSCRIPT_CACHE_FILENAME),
            "manuscripts",
            self.manuscripts,
        )
        if (
            manuscript_cache is not None
            and manuscript_cache["dependencyFingerprint"] == dependency
        ):
            self.manuscript_cache = manuscript_cache
        cached_anything = bool(self.paper_caches) or self.manuscript_cache is not None
        manuscripts = (
            self.manuscript_cache.get("manuscripts", [])
            if self.manuscript_cache is not None
            else []
        )
        self.catalog = self._catalog_value(
            papers,
            reviews,
            manuscripts,
            loading=not cached_anything,
        )
        if cached_anything:
            # Cached roots are immediately browsable. Their lightweight
            # signatures are validated by the background refresh.
            self.version = 1
            self.catalog["version"] = self.version
            self.catalog["error"] = ""
            self.fingerprint = self._catalog_fingerprint(self.catalog)

    def _scan_paper_root(self, root: Path, signature: str) -> dict:
        self._set_progress("papers", f"Scanning papers in {root.name}…")
        papers = _paper_inventory([root])
        self._set_progress("problems", f"Discovering open problems in {root.name}…")
        last_progress = -25
        last_stage_progress: dict[str, int] = {}

        def review_progress(current: int, total: int) -> None:
            nonlocal last_progress
            if current == 0 or current == total or current - last_progress >= 25:
                last_progress = current
                self._set_progress(
                    "reviews",
                    f"Building review catalog for {root.name}…",
                    current=current,
                    total=total,
                )

        def review_stage_progress(label: str, current: int, total: int) -> None:
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
            [root],
            progress=review_progress,
            stage_progress=review_stage_progress,
        )
        return {
            "schemaVersion": CATALOG_CACHE_SCHEMA_VERSION,
            "kind": "papers",
            "root": str(root.resolve()),
            "signature": signature,
            "implementationSignature": _catalog_implementation_signature(),
            "papers": papers,
            "reviews": reviews,
        }

    def refresh(
        self,
        *,
        force: bool = False,
        dirty_roots: set[str] | None = None,
    ) -> None:
        rescanned = False
        try:
            entries: dict[str, dict] = {}
            for root in self.paths:
                key = self._root_key(root)
                cached = self.paper_caches.get(key)
                validate = force or dirty_roots is None or key in dirty_roots
                if not validate and cached is not None:
                    entries[key] = cached
                    continue
                signature = _catalog_root_signature(root)
                if not force and cached is not None and cached["signature"] == signature:
                    entries[key] = cached
                    continue
                entry = self._scan_paper_root(root, signature)
                rescanned = True
                entries[key] = entry
                try:
                    self._save_cache_file(
                        _catalog_cache_path(root, PAPER_CACHE_FILENAME),
                        entry,
                    )
                except (OSError, UnicodeError):
                    pass
            self.paper_caches = entries
            papers, reviews = self._merge_paper_caches(entries.values())
            dependency = self._source_fingerprint(papers, reviews)

            def manuscript_progress(current: int, total: int) -> None:
                self._set_progress(
                    "manuscripts",
                    "Reading manuscripts…",
                    current=current,
                    total=total,
                )
            manuscript_key = self._root_key(self.manuscripts)
            cached_manuscripts = self.manuscript_cache
            validate_manuscripts = (
                force
                or dirty_roots is None
                or manuscript_key in dirty_roots
                or cached_manuscripts is None
                or cached_manuscripts["dependencyFingerprint"] != dependency
            )
            if validate_manuscripts:
                manuscript_signature = _catalog_root_signature(self.manuscripts)
            else:
                manuscript_signature = cached_manuscripts["signature"]
            if (
                not force
                and cached_manuscripts is not None
                and cached_manuscripts["signature"] == manuscript_signature
                and cached_manuscripts["dependencyFingerprint"] == dependency
            ):
                manuscripts = cached_manuscripts["manuscripts"]
            else:
                manuscripts = _manuscript_inventory(
                    self.manuscripts,
                    papers,
                    reviews,
                    progress=manuscript_progress,
                )
                rescanned = True
                cached_manuscripts = {
                    "schemaVersion": CATALOG_CACHE_SCHEMA_VERSION,
                    "kind": "manuscripts",
                    "root": str(self.manuscripts.resolve()),
                    "signature": manuscript_signature,
                    "implementationSignature": _catalog_implementation_signature(),
                    "dependencyFingerprint": dependency,
                    "manuscripts": manuscripts,
                }
                try:
                    self._save_cache_file(
                        _catalog_cache_path(
                            self.manuscripts,
                            MANUSCRIPT_CACHE_FILENAME,
                        ),
                        cached_manuscripts,
                    )
                except (OSError, UnicodeError):
                    pass
            self.manuscript_cache = cached_manuscripts
            self._set_progress("finalizing", "Preparing catalog…")
            value = self._catalog_value(
                papers,
                reviews,
                manuscripts,
                loading=False,
            )
        except (OSError, UnicodeError, common.CodexError, json.JSONDecodeError) as exc:
            with self.lock:
                self.error = str(exc)
                self.catalog["error"] = self.error
                self.catalog["loading"] = False
            self.ready.set()
            self.hub.publish("catalog.error", message=str(exc))
            return
        fingerprint = self._catalog_fingerprint(value)
        # A relevant file can affect lazily loaded detail without changing
        # the summary catalog, so a real root rescan still advances version.
        changed = (
            fingerprint != self.fingerprint
            or force
            or (rescanned and dirty_roots is not None)
        )
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

    def schedule(self, paths: Iterable[str | Path] | None = None) -> None:
        with self.pending_lock:
            if paths is None:
                self.force_refresh = True
            else:
                roots = [*self.paths, self.manuscripts]
                for value in paths:
                    path = Path(value).expanduser().resolve()
                    for root in roots:
                        if path == root or _relative_to(path, root):
                            self.dirty_roots.add(self._root_key(root))
        self.pending.set()

    def wait_until_ready(self, timeout: float | None = None) -> bool:
        return self.ready.wait(timeout)

    def _refresh_loop(self) -> None:
        while not self.stopping.is_set():
            if not self.pending.wait(0.5):
                continue
            # Coalesce installation bursts produced by atomic directory moves.
            if self.stopping.wait(0.3):
                break
            with self.pending_lock:
                force = self.force_refresh
                validate_all = self.validate_all
                dirty_roots = set(self.dirty_roots)
                self.force_refresh = False
                self.validate_all = False
                self.dirty_roots.clear()
                self.pending.clear()
            self.refresh(
                force=force,
                dirty_roots=None if force or validate_all else dirty_roots,
            )

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

    def visualization_file(self, key: str, relative: str) -> Path:
        """Resolve a resource from a catalogued visualization package."""
        with self.lock:
            packages = [
                package
                for item in self.catalog.get("reviews", [])
                for package in item.get("visualizations", [])
                if isinstance(package, dict) and package.get("key") == key
            ]
        if len(packages) != 1:
            raise KeyError(key)
        directory = Path(packages[0]["directory"]).resolve()
        if not any(_relative_to(directory, root.resolve()) for root in self.paths):
            raise KeyError(key)
        try:
            return visualizations.resolve_file(directory, relative)
        except (FileNotFoundError, ValueError) as exc:
            raise KeyError(key) from exc

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
            or bool(
                parts.intersection(
                    {"artifacts", "figures", "code", visualizations.DIRECTORY_NAME}
                )
            )
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
        self.catalog.schedule(paths)


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
        self.memory = QueueMemoryController(self.store.database)
        self.memory_lock = threading.Lock()
        self.last_memory_report: tuple | None = None
        self.settings_snapshot()
        self.pending.set()
        self.thread = threading.Thread(
            target=self._loop,
            name="workbench-scheduler",
            daemon=True,
        )
        self.thread.start()

    def _launch(self, run: dict, settings: dict | None = None) -> None:
        if settings is None:
            settings = self.store.scheduler_settings()
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
        memory = getattr(self, "memory", None)
        try:
            if memory is not None:
                with self.memory_lock:
                    container = memory.prepare_run(run["id"], settings)
                if container:
                    environment[QUEUE_JOB_ENV] = container
        except OSError as exc:
            self.store.update_run(
                run["id"],
                status="failed",
                finished_at=time.time(),
                error=f"could not prepare worker memory limit: {exc}",
            )
            self.last_launch = time.monotonic()
            self.revision = self.store.revision()
            self.hub.publish("tasks.changed", revision=self.revision)
            return
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

    def settings_snapshot(self) -> dict:
        """Return persisted settings enriched with live queue-memory state."""
        with self.memory_lock:
            settings = self.store.scheduler_settings()
            active_run_ids = self.store.active_run_ids()
            if not isinstance(active_run_ids, (set, list, tuple)):
                active_run_ids = set()
            memory = self.memory.reconcile(
                settings,
                set(active_run_ids),
            )
            changed = self.store.update_memory_limit_runtime(
                pending=bool(memory["pending"]),
                applied_bytes=memory["appliedBytes"],
            )
            if changed is True:
                settings = self.store.scheduler_settings()
            settings["memory"] = memory
            return settings

    def _publish_memory_if_changed(self, settings: dict) -> None:
        memory = settings["memory"]
        bucket_size = 128 * 1024 * 1024
        report = (
            settings.get("memoryLimitPending", False),
            memory["appliedBytes"],
            (memory["currentBytes"] or 0) // bucket_size,
            (memory["peakBytes"] or 0) // bucket_size,
            memory["error"],
        )
        if report == self.last_memory_report:
            return
        self.last_memory_report = report
        self.hub.publish("settings.changed", **settings)

    def _interrupt_terminated_workers(self) -> list[str]:
        interrupted = []
        candidates = self.store.stale_run_ids(
            older_than=WORKER_HEARTBEAT_STALE_SECONDS
        )
        for run_id in candidates:
            with self.memory_lock:
                has_processes = self.memory.run_has_processes(run_id)
            if has_processes is not False:
                continue
            if not self.store.mark_run_interrupted_if_stale(
                run_id,
                older_than=WORKER_HEARTBEAT_STALE_SECONDS,
            ):
                continue
            interrupted.append(run_id)
            run = self.store.get_run(run_id)
            recover_run_artifacts(self.store, run)
            run = self.store.get_run(run_id)
            if run["outputs"]:
                self.store.update_run(
                    run_id,
                    status="partial",
                    error="worker stopped after reporting installed output",
                )
        return interrupted

    def _check(self) -> float | None:
        self._interrupt_terminated_workers()
        current_revision = self.store.revision()
        if current_revision != self.revision:
            self.revision = current_revision
            self.hub.publish("tasks.changed", revision=self.revision)

        settings = self.settings_snapshot()
        self._publish_memory_if_changed(settings)
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
                self._launch(run, settings)
                return 1.05
        # An active worker normally wakes us through SQLite WAL events.  This
        # timeout only detects a worker that died without another commit.
        if settings.get("memoryLimitPending", False):
            return 2.0
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
        self.memory.close()


def _paper_import_relative_path(value: str) -> Path:
    if not value or len(value) > 2048 or "\\" in value or "\0" in value:
        raise PlanError("invalid uploaded file path")
    relative = PurePosixPath(value)
    if (
        relative.is_absolute()
        or len(relative.parts) > 64
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise PlanError("invalid uploaded file path")
    return Path(*relative.parts)


class WorkbenchApplication:
    def __init__(
        self,
        *,
        paths: list[Path],
        manuscripts: Path,
        state_directory: Path,
    ):
        self.paths = paths
        self.paper_output_roots = [
            path for path in paths
            if not analyze_papers.is_paper_directory(path)
        ]
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
        )
        self.scheduler = Scheduler(
            self.store,
            self.hub,
            state_directory=state_directory,
        )
        self.csrf = secrets.token_urlsafe(24)
        self.task_defaults = task_cli_defaults()
        self.plans: dict[str, dict] = {}
        self.plan_lock = threading.Lock()
        self.manuscript_lock = threading.Lock()
        self.metadata_lock = threading.Lock()
        self.analysis_lock = threading.Lock()
        self.paper_import_lock = threading.Lock()
        self.paper_imports: dict[str, dict] = {}
        self.arxiv_search_lock = threading.Lock()
        self.arxiv_pacer = download_arxiv.RequestPacer()
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
            paper_roots=self.paper_output_roots,
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

    def search_arxiv_author(self, request: dict) -> dict:
        author = request.get("author")
        if not isinstance(author, str) or not author.strip():
            raise PlanError("author is required")
        raw_limit = request.get("limit", 100)
        if isinstance(raw_limit, bool):
            raise PlanError("limit must be an integer from 1 to 100")
        try:
            limit = int(raw_limit)
        except (TypeError, ValueError) as exc:
            raise PlanError("limit must be an integer from 1 to 100") from exc
        if not 1 <= limit <= 100:
            raise PlanError("limit must be an integer from 1 to 100")
        try:
            with self.arxiv_search_lock:
                result = download_arxiv_author.search_author(
                    author,
                    limit=limit,
                    pacer=self.arxiv_pacer,
                )
        except (ValueError, download_arxiv.DownloadError) as exc:
            raise PlanError(str(exc)) from exc
        return {
            "author": result.author,
            "totalResults": result.total_results,
            "papers": [
                {
                    "id": paper.arxiv_id,
                    "title": paper.title,
                    "authors": list(paper.authors),
                    "published": paper.published,
                    "updated": paper.updated,
                }
                for paper in result.papers
            ],
        }

    def create_paper_import(self) -> dict:
        now = time.time()
        expired: list[Path] = []
        with self.paper_import_lock:
            for key, value in list(self.paper_imports.items()):
                if value["created"] < now - PAPER_IMPORT_TTL_SECONDS:
                    expired.append(value["path"])
                    self.paper_imports.pop(key, None)
            self.state_directory.mkdir(parents=True, exist_ok=True)
            import_id = secrets.token_urlsafe(18)
            staging = Path(tempfile.mkdtemp(
                prefix=".paper-upload-",
                dir=self.state_directory,
            ))
            self.paper_imports[import_id] = {
                "path": staging,
                "created": now,
                "bytes": 0,
                "files": 0,
            }
        for path in expired:
            shutil.rmtree(path, ignore_errors=True)
        return {"id": import_id}

    def upload_paper_import_file(
        self,
        import_id: str,
        relative_value: str,
        source,
        length: int,
    ) -> dict:
        if length < 0 or length > PAPER_IMPORT_MAX_FILE_BYTES:
            raise PlanError("uploaded file is too large")
        relative = _paper_import_relative_path(relative_value)
        with self.paper_import_lock:
            session = self.paper_imports.get(import_id)
            if session is None:
                raise PlanError("paper import expired; choose the files again")
            if session["files"] >= PAPER_IMPORT_MAX_FILES:
                raise PlanError("paper import contains too many files")
            if session["bytes"] + length > PAPER_IMPORT_MAX_TOTAL_BYTES:
                raise PlanError("paper import is too large")
            destination = session["path"] / relative
            if destination.exists():
                raise PlanError(f"duplicate uploaded path: {relative_value}")
            session["files"] += 1
            session["bytes"] += length

        temporary = destination.with_name(destination.name + ".part")
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            remaining = length
            with temporary.open("xb") as output:
                while remaining:
                    chunk = source.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise PlanError("incomplete uploaded file")
                    output.write(chunk)
                    remaining -= len(chunk)
            os.replace(temporary, destination)
        except Exception as exc:
            temporary.unlink(missing_ok=True)
            with self.paper_import_lock:
                current = self.paper_imports.get(import_id)
                if current is session:
                    session["files"] -= 1
                    session["bytes"] -= length
            if isinstance(exc, PlanError):
                raise
            raise PlanError(f"could not store uploaded file: {exc}") from exc
        return {"path": relative_value, "size": length}

    def commit_paper_import(self, import_id: str, request: dict) -> dict:
        output_value = request.get("outputDirectory")
        if not isinstance(output_value, str) or not output_value:
            raise PlanError("paper collection is required")
        output = Path(output_value).expanduser().resolve()
        if not any(output == root.resolve() for root in self.paper_output_roots):
            raise PlanError("paper collection is not a configured paper root")
        input_values = request.get("inputs")
        if not isinstance(input_values, list) or not input_values:
            raise PlanError("at least one PDF, archive, or directory is required")
        if not all(isinstance(value, str) for value in input_values):
            raise PlanError("paper import inputs must be paths")

        with self.paper_import_lock:
            session = self.paper_imports.pop(import_id, None)
        if session is None:
            raise PlanError("paper import expired; choose the files again")
        staging: Path = session["path"]
        try:
            inputs = [staging / _paper_import_relative_path(value) for value in input_values]
            if any(not path.exists() for path in inputs):
                raise PlanError("paper import is missing an uploaded input")
            try:
                targets = ingest_paper.ingest_inputs(inputs, output)
            except ingest_paper.IngestError as exc:
                raise PlanError(str(exc)) from exc
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        self.catalog.schedule(targets)
        return {
            "papers": [
                {"path": str(path), "urlKey": _url_key(path)}
                for path in targets
            ]
        }

    def discard_paper_import(self, import_id: str) -> dict:
        with self.paper_import_lock:
            session = self.paper_imports.pop(import_id, None)
        if session is not None:
            shutil.rmtree(session["path"], ignore_errors=True)
        return {"discarded": session is not None}

    def update_paper_metadata(self, request: dict) -> dict:
        value = request.get("path")
        if not isinstance(value, str) or not value:
            raise PlanError("paper path is required")
        paper = Path(value).expanduser().resolve()
        if not any(_relative_to(paper, root.resolve()) for root in self.paths):
            raise PlanError("paper is outside the configured paper directories")
        if not analyze_papers.is_paper_directory(paper):
            raise PlanError("paper directory is unavailable")

        title = request.get("title", "")
        authors = request.get("authors", [])
        if not isinstance(title, str):
            raise PlanError("title must be text")
        if not isinstance(authors, list) or not all(
            isinstance(author, str) for author in authors
        ):
            raise PlanError("authors must be an array of names")
        authors = [author.strip() for author in authors if author.strip()]
        if len(set(authors)) != len(authors):
            raise PlanError("authors must not contain duplicates")

        arxiv_id = request.get("arxivId", "")
        if not isinstance(arxiv_id, str):
            raise PlanError("arXiv ID must be text")
        arxiv_id = arxiv_id.strip()
        if arxiv_id:
            try:
                arxiv_id = download_arxiv.parse_arxiv_id(arxiv_id)
            except ValueError as exc:
                raise PlanError(str(exc)) from exc

        fields = {
            "title": title.strip(),
            "authors": authors,
            "arxiv_id": arxiv_id,
        }
        for name in ("published", "updated"):
            raw = request.get(name, "")
            if not isinstance(raw, str):
                raise PlanError(f"{name} must be text")
            normalized = raw.strip()
            if normalized and not ingest_paper.is_iso8601_date_or_timestamp(normalized):
                raise PlanError(
                    f"{name} must be an ISO 8601 date or timestamp"
                )
            fields[name] = normalized
        for name in ("url", "doi"):
            raw = request.get(name, "")
            if not isinstance(raw, str):
                raise PlanError(f"{name} must be text")
            fields[name] = raw.strip()

        metadata_path = paper / "metadata.json"
        temporary = metadata_path.with_name("metadata.json.tmp")
        try:
            with self.metadata_lock:
                try:
                    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                except FileNotFoundError:
                    metadata = {}
                if not isinstance(metadata, dict):
                    raise PlanError("paper metadata is not a JSON object")
                metadata.setdefault("schema_version", 1)
                metadata.update(fields)
                temporary.write_text(
                    json.dumps(metadata, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8",
                )
                os.replace(temporary, metadata_path)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            temporary.unlink(missing_ok=True)
            raise PlanError(f"could not update paper metadata: {exc}") from exc
        self.catalog.schedule([metadata_path])
        result = {"path": str(paper), **fields}
        result["arxivId"] = result.pop("arxiv_id")
        return result

    def add_open_problem(self, request: dict) -> dict:
        """Add one manually entered problem to an analyzed paper."""
        value = request.get("path")
        if not isinstance(value, str) or not value:
            raise PlanError("paper path is required")
        paper = Path(value).expanduser().resolve()
        if not any(_relative_to(paper, root.resolve()) for root in self.paths):
            raise PlanError("paper is outside the configured paper directories")
        if not analyze_papers.is_paper_directory(paper):
            raise PlanError("paper directory is unavailable")

        title = request.get("title", "")
        statement = request.get("statement", "")
        explicitness = request.get("explicitness", "additional")
        if not isinstance(title, str) or not title.strip():
            raise PlanError("problem title is required")
        if "\n" in title or "\r" in title:
            raise PlanError("problem title must be a single line")
        if not isinstance(statement, str) or not statement.strip():
            raise PlanError("problem statement is required")
        if explicitness not in common.EXPLICITNESS_VALUES:
            raise PlanError(
                "problem relation must be explicit, inferred, uncertain, or additional"
            )
        title = title.strip()
        statement = statement.strip()

        analysis = paper / "analysis"
        manifest_path = analysis / "manifest.json"
        problems_path = analysis / "open-problems.md"
        manifest_temporary = analysis / ".manifest.manual-problem.tmp"
        problems_temporary = analysis / ".open-problems.manual-problem.tmp"
        with self.analysis_lock:
            try:
                manifest = common.read_json(
                    manifest_path,
                    description="paper analysis manifest",
                )
            except common.CodexError as exc:
                raise PlanError(
                    "analyze the paper before adding an open problem"
                ) from exc
            problems = manifest.get("open_problems")
            if not isinstance(problems, list):
                raise PlanError("paper analysis manifest has no open-problem list")

            used_numbers = []
            for problem in problems:
                problem_id = problem.get("id") if isinstance(problem, dict) else None
                if (
                    not isinstance(problem_id, str)
                    or not analyze_papers.OPEN_PROBLEM_ID_RE.fullmatch(problem_id)
                ):
                    raise PlanError("paper analysis manifest has an invalid open problem")
                used_numbers.append(int(problem_id.removeprefix("OP-")))
            problem_id = f"OP-{max(used_numbers, default=0) + 1:03d}"
            manifest["open_problems"] = [
                *problems,
                {
                    "id": problem_id,
                    "title": title,
                    "explicitness": explicitness,
                },
            ]

            try:
                original_markdown = problems_path.read_text(encoding="utf-8")
            except FileNotFoundError:
                original_markdown = "# Open Problems\n"
            except (OSError, UnicodeError) as exc:
                raise PlanError(f"could not read open problems: {exc}") from exc
            updated_markdown = (
                original_markdown.rstrip()
                + f"\n\n## {problem_id}: {title}\n\n{statement}\n"
            )
            try:
                common.write_json(manifest_temporary, manifest)
                problems_temporary.write_text(updated_markdown, encoding="utf-8")
                os.replace(problems_temporary, problems_path)
                try:
                    os.replace(manifest_temporary, manifest_path)
                except Exception:
                    problems_path.write_text(original_markdown, encoding="utf-8")
                    raise
            except (OSError, UnicodeError) as exc:
                raise PlanError(f"could not add open problem: {exc}") from exc
            finally:
                manifest_temporary.unlink(missing_ok=True)
                problems_temporary.unlink(missing_ok=True)

        self.catalog.schedule([manifest_path, problems_path])
        return {
            "path": str(paper / problem_id),
            "id": problem_id,
            "title": title,
            "statement": statement,
            "explicitness": explicitness,
        }

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
        self.catalog.schedule([draft])
        return result

    def close(self) -> None:
        if self.observer is not None:
            self.observer.stop()
            self.observer.join(timeout=5)
        with self.paper_import_lock:
            imports = [value["path"] for value in self.paper_imports.values()]
            self.paper_imports.clear()
        for path in imports:
            shutil.rmtree(path, ignore_errors=True)
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
                            **self.app.scheduler.settings_snapshot(),
                            "projectRoot": str(PROJECT_ROOT),
                            "paperRoots": [
                                str(path) for path in self.app.paper_output_roots
                            ],
                            "taskDefaults": self.app.task_defaults,
                        },
                    }
                )
            elif path == "/api/catalog":
                self.send_json(self.app.catalog.snapshot())
            elif path == "/api/review-detail":
                key = parse_qs(parsed.query).get("key", [""])[0]
                self.send_json(self.app.catalog.review_detail(key))
            elif match := re.fullmatch(
                r"/api/visualizations/([0-9a-f]{24})/(.+)", path
            ):
                resource = self.app.catalog.visualization_file(
                    match.group(1), unquote(match.group(2))
                )
                self._send_visualization_file(resource)
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
            upload_match = re.fullmatch(
                r"/api/paper-imports/([A-Za-z0-9_-]+)/files",
                parsed.path,
            )
            if upload_match:
                if not self.require_mutation_auth():
                    return
                try:
                    length = int(self.headers.get("Content-Length", "-1"))
                except ValueError as exc:
                    self.close_connection = True
                    raise PlanError("invalid content length") from exc
                relative = parse_qs(parsed.query).get("path", [""])[0]
                try:
                    result = self.app.upload_paper_import_file(
                        upload_match.group(1),
                        relative,
                        self.rfile,
                        length,
                    )
                except Exception:
                    self.close_connection = True
                    raise
                self.send_json(result, 201)
                return
            body = self.read_json()
            if not self.require_mutation_auth():
                return
            if parsed.path == "/api/plans":
                self.send_json(self.app.create_plan(body), 201)
            elif parsed.path == "/api/paper-imports":
                self.send_json(self.app.create_paper_import(), 201)
            elif match := re.fullmatch(
                r"/api/paper-imports/([A-Za-z0-9_-]+)/commit",
                parsed.path,
            ):
                self.send_json(self.app.commit_paper_import(match.group(1), body), 201)
            elif match := re.fullmatch(
                r"/api/paper-imports/([A-Za-z0-9_-]+)/cancel",
                parsed.path,
            ):
                self.send_json(self.app.discard_paper_import(match.group(1)))
            elif parsed.path == "/api/arxiv/author-search":
                self.send_json(self.app.search_arxiv_author(body))
            elif parsed.path == "/api/papers/metadata":
                self.send_json(self.app.update_paper_metadata(body))
            elif parsed.path == "/api/papers/open-problems":
                self.send_json(self.app.add_open_problem(body), 201)
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
                if "memoryLimit" in body:
                    changes["memory_limit"] = body["memoryLimit"]
                self.app.store.update_scheduler_settings(**changes)
                settings = self.app.scheduler.settings_snapshot()
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

    def _send_visualization_file(self, path: Path) -> None:
        """Serve one sandboxed app resource with a network-denying policy."""
        data = path.read_bytes()
        content_type = (
            mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        )
        if path.suffix.casefold() in {
            ".html", ".css", ".js", ".json", ".md", ".svg", ".txt"
        }:
            content_type += "; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        # A sandboxed iframe without allow-same-origin has the opaque `null`
        # origin. Scope this CORS exception to package resources so local
        # fetch/module/WASM loads work without exposing the Workbench API.
        self.send_header("Access-Control-Allow-Origin", "null")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; "
            "script-src 'self' 'unsafe-inline' 'wasm-unsafe-eval'; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: blob:; "
            "font-src 'self' data:; "
            "media-src 'self' data: blob:; "
            "connect-src 'self'; object-src 'none'; frame-src 'none'; "
            "worker-src 'none'; base-uri 'none'; form-action 'none'; "
            "navigate-to 'none'; "
            "frame-ancestors 'self'",
        )
        self.end_headers()
        self.wfile.write(data)

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
