#!/usr/bin/env python3
"""Download arXiv papers' PDFs and submitted source packages.

The public ``fetch_paper`` and ``fetch_papers`` functions are also used by the
author-search downloader.
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import re
import shutil
import stat
import sys
import tarfile
import time
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable, Iterable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlencode, urlparse
from urllib.request import Request, urlopen


ARXIV_HOSTS = {"arxiv.org", "www.arxiv.org", "export.arxiv.org"}
ARXIV_PATH_PREFIXES = ("abs/", "pdf/", "src/", "format/", "html/")
MODERN_ID_RE = re.compile(r"(?P<id>\d{4}\.\d{4,5}(?:v[1-9]\d*)?)", re.IGNORECASE)
LEGACY_ID_RE = re.compile(
    r"(?P<id>[a-z][a-z0-9.-]*/\d{7}(?:v[1-9]\d*)?)", re.IGNORECASE
)
SOURCE_FILENAMES = (
    "source.tar.gz",
    "source.tar",
    "source.zip",
    "source.gz",
    "source.pdf",
    "source.ps",
    "source.tex",
    "source.bin",
)
PDF_ONLY_MARKER = "PDF_ONLY"
USER_AGENT = "loose-ends-arxiv-downloader/0.1"
DEFAULT_REQUEST_INTERVAL = 3.0
API_URL = "https://export.arxiv.org/api/query"
ATOM_NAMESPACE = "http://www.w3.org/2005/Atom"
OPENSEARCH_NAMESPACE = "http://a9.com/-/spec/opensearch/1.1/"
NAMESPACES = {"atom": ATOM_NAMESPACE, "opensearch": OPENSEARCH_NAMESPACE}
DEFAULT_METADATA_BATCH_SIZE = 100
METADATA_FILE = "metadata.json"
METADATA_SCHEMA_VERSION = 1


class DownloadError(RuntimeError):
    """A problem fetching or validating a file from arXiv."""


class ArxivHTTPError(DownloadError):
    """An HTTP error response from arXiv."""

    def __init__(self, status: int, url: str, reason: str) -> None:
        self.status = status
        self.url = url
        super().__init__(f"arXiv returned HTTP {status} for {url}: {reason}")


class RequestPacer:
    """Keep request start times at least ``interval`` seconds apart."""

    def __init__(
        self,
        interval: float = DEFAULT_REQUEST_INTERVAL,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if interval < 0:
            raise ValueError("request interval cannot be negative")
        self.interval = interval
        self._clock = clock
        self._sleep = sleep
        self._next_request_at: float | None = None

    def wait(self) -> None:
        now = self._clock()
        if self._next_request_at is not None and now < self._next_request_at:
            self._sleep(self._next_request_at - now)
            now = max(self._clock(), self._next_request_at)
        self._next_request_at = now + self.interval


DEFAULT_PACER = RequestPacer()


@dataclass(frozen=True)
class DownloadedFile:
    content_type: str
    server_filename: str | None


@dataclass(frozen=True)
class PaperDownload:
    arxiv_id: str
    pdf_path: Path
    source_path: Path | None
    requested_id: str | None = None

    @property
    def pdf_only(self) -> bool:
        return self.source_path is None

    @property
    def fell_back(self) -> bool:
        return self.requested_id is not None and self.requested_id != self.arxiv_id


@dataclass(frozen=True)
class PaperFailure:
    paper: str
    error: str


@dataclass(frozen=True)
class PaperMetadata:
    arxiv_id: str
    title: str
    authors: tuple[str, ...]
    published: str
    updated: str


def parse_arxiv_id(value: str) -> str:
    """Return an arXiv ID from a bare ID, arXiv citation, or arXiv URL."""
    candidate = value.strip().strip("<>")
    if not candidate:
        raise ValueError("the arXiv ID or URL is empty")

    if candidate.lower().startswith("arxiv:"):
        candidate = candidate[6:].strip()
    elif candidate.lower().startswith(
        ("arxiv.org/", "www.arxiv.org/", "export.arxiv.org/")
    ):
        candidate = "https://" + candidate

    parsed = urlparse(candidate)
    if parsed.scheme or parsed.netloc:
        if parsed.scheme not in {"http", "https"} or parsed.hostname not in ARXIV_HOSTS:
            raise ValueError(f"not an arXiv URL: {value!r}")
        candidate = unquote(parsed.path).lstrip("/")
        for prefix in ARXIV_PATH_PREFIXES:
            if candidate.lower().startswith(prefix):
                candidate = candidate[len(prefix) :]
                break

    candidate = candidate.strip().rstrip("/")
    if candidate.lower().endswith(".pdf"):
        candidate = candidate[:-4]

    match = MODERN_ID_RE.fullmatch(candidate) or LEGACY_ID_RE.fullmatch(candidate)
    if not match:
        raise ValueError(f"could not parse a valid arXiv ID from {value!r}")

    arxiv_id = match.group("id")
    if MODERN_ID_RE.fullmatch(arxiv_id):
        month = int(arxiv_id[2:4])
        if not 1 <= month <= 12:
            raise ValueError(f"invalid month in arXiv ID: {arxiv_id!r}")
    return arxiv_id


def directory_name(arxiv_id: str) -> str:
    """Map an ID to one portable directory name."""
    return f"arXiv-{arxiv_id.replace('/', '_')}"


def open_arxiv_url(url: str, *, pacer: RequestPacer | None = None):
    """Open an arXiv URL after applying the shared request-rate limit."""
    (pacer or DEFAULT_PACER).wait()
    request = Request(url, headers={"User-Agent": USER_AGENT})
    try:
        return urlopen(request, timeout=60)
    except HTTPError as exc:
        raise ArxivHTTPError(exc.code, url, str(exc.reason)) from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise DownloadError(f"could not download {url}: {exc}") from exc


def normalized_text(value: str | None) -> str:
    return " ".join((value or "").split())


def parse_atom_feed(payload: bytes) -> tuple[int, list[PaperMetadata]]:
    """Parse paper metadata from one arXiv API Atom response."""
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as exc:
        raise DownloadError(f"arXiv returned invalid Atom XML: {exc}") from exc

    total_element = root.find("opensearch:totalResults", NAMESPACES)
    try:
        total_results = int(total_element.text) if total_element is not None else 0
    except (TypeError, ValueError) as exc:
        raise DownloadError("arXiv returned an invalid result count") from exc

    papers = []
    for entry in root.findall("atom:entry", NAMESPACES):
        id_element = entry.find("atom:id", NAMESPACES)
        if id_element is None or not id_element.text:
            raise DownloadError("arXiv returned an Atom entry without an ID")
        try:
            arxiv_id = parse_arxiv_id(id_element.text)
        except ValueError as exc:
            title = normalized_text(entry.findtext("atom:title", "", NAMESPACES))
            raise DownloadError(
                f"arXiv API returned an error entry: {title or id_element.text}"
            ) from exc

        authors = tuple(
            normalized_text(author.findtext("atom:name", "", NAMESPACES))
            for author in entry.findall("atom:author", NAMESPACES)
        )
        papers.append(
            PaperMetadata(
                arxiv_id=arxiv_id,
                title=normalized_text(
                    entry.findtext("atom:title", "", NAMESPACES)
                ),
                authors=tuple(author for author in authors if author),
                published=normalized_text(
                    entry.findtext("atom:published", "", NAMESPACES)
                ),
                updated=normalized_text(
                    entry.findtext("atom:updated", "", NAMESPACES)
                ),
            )
        )

    return total_results, papers


def fetch_arxiv_metadata(
    arxiv_ids: Iterable[str],
    *,
    batch_size: int = DEFAULT_METADATA_BATCH_SIZE,
    pacer: RequestPacer | None = None,
) -> dict[str, PaperMetadata]:
    """Fetch metadata for exact arXiv IDs in batched API requests."""
    if batch_size < 1:
        raise ValueError("metadata batch size must be at least 1")
    ids = list(dict.fromkeys(parse_arxiv_id(value) for value in arxiv_ids))
    requested_by_base: dict[str, list[str]] = {}
    for arxiv_id in ids:
        base_id = re.sub(r"v[1-9]\d*$", "", arxiv_id, flags=re.IGNORECASE)
        requested_by_base.setdefault(base_id, []).append(arxiv_id)
    metadata: dict[str, PaperMetadata] = {}
    for start in range(0, len(ids), batch_size):
        batch = ids[start : start + batch_size]
        query = urlencode(
            {
                "id_list": ",".join(batch),
                "start": 0,
                "max_results": len(batch),
            }
        )
        with open_arxiv_url(f"{API_URL}?{query}", pacer=pacer) as response:
            _, papers = parse_atom_feed(response.read())
        for paper in papers:
            if paper.arxiv_id in ids:
                metadata[paper.arxiv_id] = paper
                continue
            base_id = re.sub(
                r"v[1-9]\d*$",
                "",
                paper.arxiv_id,
                flags=re.IGNORECASE,
            )
            requested = requested_by_base.get(base_id, [])
            if len(requested) == 1:
                metadata[requested[0]] = paper

    missing = [arxiv_id for arxiv_id in ids if arxiv_id not in metadata]
    if missing:
        raise DownloadError(
            "arXiv returned no metadata for: " + ", ".join(missing)
        )
    return metadata


def write_paper_metadata(target_dir: Path, metadata: PaperMetadata) -> Path:
    """Atomically save normalized arXiv metadata beside a downloaded paper."""
    if not metadata.title or not metadata.authors:
        raise DownloadError(f"arXiv returned incomplete metadata for {metadata.arxiv_id}")
    path = target_dir / METADATA_FILE
    temporary = path.with_name(path.name + ".part")
    payload = {
        "schema_version": METADATA_SCHEMA_VERSION,
        "arxiv_id": metadata.arxiv_id,
        "title": metadata.title,
        "authors": list(metadata.authors),
        "published": metadata.published,
        "updated": metadata.updated,
    }
    temporary.unlink(missing_ok=True)
    try:
        temporary.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    except OSError as exc:
        raise DownloadError(f"could not write {path}: {exc}") from exc
    finally:
        temporary.unlink(missing_ok=True)
    return path


def download_to(
    url: str,
    destination: Path,
    *,
    pacer: RequestPacer | None = None,
) -> DownloadedFile:
    """Stream a URL to a temporary file and atomically install it."""
    temporary = destination.with_name(destination.name + ".part")
    temporary.unlink(missing_ok=True)

    try:
        with open_arxiv_url(url, pacer=pacer) as response, temporary.open(
            "wb"
        ) as output:
            content_type = response.headers.get_content_type().lower()
            server_filename = response.headers.get_filename()
            shutil.copyfileobj(response, output, length=1024 * 1024)
    except DownloadError:
        temporary.unlink(missing_ok=True)
        raise
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise DownloadError(f"could not save {destination}: {exc}") from exc

    if temporary.stat().st_size == 0:
        temporary.unlink(missing_ok=True)
        raise DownloadError(f"arXiv returned an empty file for {url}")

    os.replace(temporary, destination)
    return DownloadedFile(content_type, server_filename)


def file_header(path: Path, size: int = 512) -> bytes:
    with path.open("rb") as file:
        return file.read(size)


def validate_pdf(path: Path) -> None:
    if not file_header(path).startswith(b"%PDF-"):
        path.unlink(missing_ok=True)
        raise DownloadError("arXiv's PDF endpoint did not return a PDF")


def source_filename(download: DownloadedFile, header: bytes) -> str:
    """Choose a stable filename without assuming every source is a tarball."""
    if header.startswith(b"%PDF-"):
        return "source.pdf"
    if header.startswith(b"PK\x03\x04"):
        return "source.zip"
    if len(header) >= 262 and header[257:262] == b"ustar":
        return "source.tar"
    if header.startswith(b"\x1f\x8b"):
        if download.server_filename and download.server_filename.lower().endswith(
            ".tar.gz"
        ):
            return "source.tar.gz"
        return "source.gz"
    if header.startswith(b"%!PS"):
        return "source.ps"

    server_name = (download.server_filename or "").lower()
    if server_name.endswith((".tex", ".latex")):
        return "source.tex"
    if download.content_type.startswith("text/") and not header.lstrip().startswith(
        b"<"
    ):
        return "source.tex"
    return "source.bin"


def existing_source_file(target_dir: Path) -> Path | None:
    return next(
        (target_dir / name for name in SOURCE_FILENAMES if (target_dir / name).exists()),
        None,
    )


def safe_archive_destination(root: Path, member_name: str) -> Path:
    """Resolve an archive member beneath ``root`` or reject it."""
    normalized = member_name.replace("\\", "/")
    member_path = PurePosixPath(normalized)
    if (
        not normalized
        or normalized.startswith("/")
        or "\0" in normalized
        or (member_path.parts and ":" in member_path.parts[0])
        or any(part == ".." for part in member_path.parts)
    ):
        raise DownloadError(f"unsafe path in source archive: {member_name!r}")

    parts = tuple(part for part in member_path.parts if part not in {"", "."})
    destination = root.joinpath(*parts)
    root_resolved = root.resolve()
    destination_resolved = destination.resolve(strict=False)
    if os.path.commonpath((root_resolved, destination_resolved)) != str(root_resolved):
        raise DownloadError(f"unsafe path in source archive: {member_name!r}")
    return destination


def extract_tar(source_file: Path, destination: Path) -> None:
    with tarfile.open(source_file, "r:*") as archive:
        members = archive.getmembers()
        for member in members:
            safe_archive_destination(destination, member.name)
            if member.issym() or member.islnk():
                raise DownloadError(
                    f"links are not allowed in source archives: {member.name!r}"
                )
            if not (member.isfile() or member.isdir()):
                raise DownloadError(
                    f"unsupported entry in source archive: {member.name!r}"
                )

        for member in members:
            target = safe_archive_destination(destination, member.name)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            extracted = archive.extractfile(member)
            if extracted is None:
                raise DownloadError(
                    f"could not read source archive entry: {member.name!r}"
                )
            with extracted, target.open("wb") as output:
                shutil.copyfileobj(extracted, output, length=1024 * 1024)


def extract_zip(source_file: Path, destination: Path) -> None:
    with zipfile.ZipFile(source_file) as archive:
        entries = archive.infolist()
        for entry in entries:
            safe_archive_destination(destination, entry.filename)
            entry_type = stat.S_IFMT(entry.external_attr >> 16)
            if entry_type == stat.S_IFLNK:
                raise DownloadError(
                    f"links are not allowed in source archives: {entry.filename!r}"
                )

        for entry in entries:
            target = safe_archive_destination(destination, entry.filename)
            if entry.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(entry) as extracted, target.open("wb") as output:
                shutil.copyfileobj(extracted, output, length=1024 * 1024)


def extract_gzip(source_file: Path, destination: Path) -> None:
    with gzip.open(source_file, "rb") as compressed:
        header = compressed.read(512)
        if header.startswith(b"%PDF-"):
            filename = "source.pdf"
        elif header.startswith(b"%!PS"):
            filename = "source.ps"
        else:
            filename = "source.tex"
        with (destination / filename).open("wb") as output:
            output.write(header)
            shutil.copyfileobj(compressed, output, length=1024 * 1024)


def remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def extract_source(
    source_file: Path,
    source_dir: Path,
    *,
    force: bool = False,
) -> Path:
    """Install submitted source under ``source_dir`` and remove its package."""
    staging_dir = source_dir.with_name(source_dir.name + ".part")
    remove_path(staging_dir)
    staging_dir.mkdir(parents=True)

    try:
        if tarfile.is_tarfile(source_file):
            extract_tar(source_file, staging_dir)
        elif zipfile.is_zipfile(source_file):
            extract_zip(source_file, staging_dir)
        elif file_header(source_file, 2) == b"\x1f\x8b":
            extract_gzip(source_file, staging_dir)
        else:
            shutil.copy2(source_file, staging_dir / source_file.name)

        if source_dir.exists() or source_dir.is_symlink():
            if not force:
                raise DownloadError(f"source directory already exists: {source_dir}")
            remove_path(source_dir)
        os.replace(staging_dir, source_dir)
        source_file.unlink()
    except DownloadError:
        remove_path(staging_dir)
        raise
    except (OSError, tarfile.TarError, zipfile.BadZipFile, gzip.BadGzipFile) as exc:
        remove_path(staging_dir)
        raise DownloadError(f"could not extract {source_file}: {exc}") from exc

    return source_dir


def version_candidates(arxiv_id: str) -> list[str]:
    """Return an explicitly versioned ID followed by its earlier versions."""
    match = re.fullmatch(r"(.+)v([1-9]\d*)", arxiv_id, re.IGNORECASE)
    if match is None:
        return [arxiv_id]
    base_id, version_text = match.groups()
    return [
        f"{base_id}v{version}"
        for version in range(int(version_text), 0, -1)
    ]


def metadata_matches_id(metadata: PaperMetadata, arxiv_id: str) -> bool:
    if metadata.arxiv_id == arxiv_id:
        return True
    if re.search(r"v[1-9]\d*$", arxiv_id, flags=re.IGNORECASE):
        return False
    metadata_base = re.sub(
        r"v[1-9]\d*$",
        "",
        metadata.arxiv_id,
        flags=re.IGNORECASE,
    )
    return metadata_base == arxiv_id


def mark_pdf_only(target_dir: Path) -> None:
    marker = target_dir / PDF_ONLY_MARKER
    try:
        marker.write_text(
            "arXiv did not provide separate source files for this version.\n",
            encoding="utf-8",
        )
    except OSError as exc:
        raise DownloadError(f"could not write {marker}: {exc}") from exc


def fetch_paper_version(
    requested_id: str,
    arxiv_id: str,
    output_dir: Path,
    *,
    force: bool = False,
    pacer: RequestPacer | None = None,
) -> PaperDownload:
    """Fetch one known version of a paper."""
    target_dir = output_dir / directory_name(arxiv_id)
    target_dir.mkdir(parents=True, exist_ok=True)

    quoted_id = arxiv_id  # Every accepted ID character is safe in an URL path.
    pdf_path = target_dir / "paper.pdf"
    if pdf_path.exists() and not force:
        print(f"Already exists: {pdf_path}")
    else:
        print(f"Downloading PDF for {arxiv_id}...")
        download_to(
            f"https://arxiv.org/pdf/{quoted_id}", pdf_path, pacer=pacer
        )
        validate_pdf(pdf_path)
        print(f"Saved: {pdf_path}")

    source_dir = target_dir / "source"
    pdf_only_marker = target_dir / PDF_ONLY_MARKER
    old_source = existing_source_file(target_dir)
    if source_dir.is_dir() and not force:
        source_path = source_dir
        pdf_only_marker.unlink(missing_ok=True)
        print(f"Already exists: {source_path}")
    elif pdf_only_marker.exists() and not force:
        source_path = None
        print(f"Already identified as PDF-only: arXiv:{arxiv_id}")
    elif source_dir.exists() and not force:
        raise DownloadError(f"source path is not a directory: {source_dir}")
    elif old_source is not None and not force:
        if file_header(old_source).startswith(b"%PDF-"):
            old_source.unlink()
            mark_pdf_only(target_dir)
            source_path = None
            print(f"Identified as PDF-only: arXiv:{arxiv_id}")
        else:
            print(f"Extracting existing source package: {old_source}")
            source_path = extract_source(old_source, source_dir)
            pdf_only_marker.unlink(missing_ok=True)
            print(f"Saved source files: {source_path}")
    else:
        if force:
            pdf_only_marker.unlink(missing_ok=True)
        print(f"Downloading source for {arxiv_id}...")
        source_download = target_dir / "source.download"
        try:
            result = download_to(
                f"https://arxiv.org/src/{quoted_id}",
                source_download,
                pacer=pacer,
            )
        except ArxivHTTPError as exc:
            if exc.status not in {403, 404}:
                raise
            remove_path(source_dir)
            for name in SOURCE_FILENAMES:
                (target_dir / name).unlink(missing_ok=True)
            mark_pdf_only(target_dir)
            source_path = None
            print(f"No separate source available: arXiv:{arxiv_id}")
            return PaperDownload(
                arxiv_id,
                pdf_path,
                source_path,
                requested_id=requested_id,
            )

        header = file_header(source_download)
        if result.content_type == "text/html" or header.lstrip().lower().startswith(
            (b"<!doctype html", b"<html")
        ):
            source_download.unlink(missing_ok=True)
            raise DownloadError("arXiv's source endpoint returned HTML instead of source")

        if header.startswith(b"%PDF-"):
            source_download.unlink()
            remove_path(source_dir)
            mark_pdf_only(target_dir)
            source_path = None
            print(f"No separate source available: arXiv:{arxiv_id}")
        else:
            source_package = target_dir / source_filename(result, header)
            os.replace(source_download, source_package)
            source_path = extract_source(source_package, source_dir, force=force)
            pdf_only_marker.unlink(missing_ok=True)
            if force:
                for name in SOURCE_FILENAMES:
                    stale_path = target_dir / name
                    stale_path.unlink(missing_ok=True)
            print(f"Saved source files: {source_path}")

    return PaperDownload(
        arxiv_id,
        pdf_path,
        source_path,
        requested_id=requested_id,
    )


def fetch_paper(
    arxiv_id: str,
    output_dir: Path,
    *,
    force: bool = False,
    pacer: RequestPacer | None = None,
    metadata: PaperMetadata | None = None,
    save_metadata: bool = True,
) -> PaperDownload:
    """Fetch a paper and its metadata, with explicit-version fallback."""
    candidates = version_candidates(arxiv_id)
    last_error: ArxivHTTPError | None = None

    for index, candidate in enumerate(candidates):
        try:
            download = fetch_paper_version(
                arxiv_id,
                candidate,
                output_dir,
                force=force,
                pacer=pacer,
            )
        except ArxivHTTPError as exc:
            if exc.status != 404:
                raise
            last_error = exc
            target_dir = output_dir / directory_name(candidate)
            try:
                target_dir.rmdir()
            except OSError:
                pass
            if index + 1 < len(candidates):
                print(
                    f"arXiv:{candidate} is unavailable; "
                    f"trying arXiv:{candidates[index + 1]}."
                )
            continue

        if save_metadata:
            if metadata is None or not metadata_matches_id(
                metadata,
                download.arxiv_id,
            ):
                metadata = fetch_arxiv_metadata(
                    [download.arxiv_id],
                    pacer=pacer,
                )[download.arxiv_id]
            metadata_path = write_paper_metadata(
                download.pdf_path.parent,
                metadata,
            )
            print(f"Saved metadata: {metadata_path}")
        return download

    assert last_error is not None
    raise last_error


def fetch_papers(
    papers: Iterable[str],
    output_dir: Path,
    *,
    force: bool = False,
    pacer: RequestPacer | None = None,
    metadata_by_id: Mapping[str, PaperMetadata] | None = None,
) -> tuple[list[PaperDownload], list[PaperFailure]]:
    """Download a batch and save metadata, continuing after failures."""
    content_downloads: list[PaperDownload] = []
    failures: list[PaperFailure] = []
    seen: set[str] = set()

    for paper in papers:
        failure_name = paper
        try:
            arxiv_id = parse_arxiv_id(paper)
            failure_name = arxiv_id
            if arxiv_id in seen:
                print(f"Skipping duplicate: arXiv:{arxiv_id}")
                continue
            seen.add(arxiv_id)
            download = fetch_paper(
                arxiv_id,
                output_dir,
                force=force,
                pacer=pacer,
                save_metadata=False,
            )
            content_downloads.append(download)
        except (ValueError, DownloadError) as exc:
            failures.append(PaperFailure(failure_name, str(exc)))
            print(f"error: {paper}: {exc}", file=sys.stderr)

    metadata = dict(metadata_by_id or {})
    missing_ids = [
        download.arxiv_id
        for download in content_downloads
        if download.arxiv_id not in metadata
    ]
    metadata_error: DownloadError | None = None
    if missing_ids:
        try:
            metadata.update(
                fetch_arxiv_metadata(missing_ids, pacer=pacer)
            )
        except DownloadError as exc:
            metadata_error = exc

    downloads: list[PaperDownload] = []
    for download in content_downloads:
        paper_metadata = metadata.get(download.arxiv_id)
        try:
            if paper_metadata is None:
                assert metadata_error is not None
                raise metadata_error
            metadata_path = write_paper_metadata(
                download.pdf_path.parent,
                paper_metadata,
            )
            print(f"Saved metadata: {metadata_path}")
            downloads.append(download)
        except DownloadError as exc:
            failures.append(PaperFailure(download.arxiv_id, str(exc)))
            print(
                f"error: arXiv:{download.arxiv_id}: {exc}",
                file=sys.stderr,
            )

    return downloads, failures


def print_completion_summary(
    downloads: list[PaperDownload],
    failures: list[PaperFailure],
) -> None:
    print(
        f"Completed {len(downloads)} paper(s)"
        + (f"; {len(failures)} failed" if failures else "")
        + "."
    )
    fallbacks = [download for download in downloads if download.fell_back]
    if fallbacks:
        print(f"Version fallbacks: {len(fallbacks)} paper(s).")
        print(
            "Version fallback IDs: "
            + " ".join(
                f"{download.requested_id}->{download.arxiv_id}"
                for download in fallbacks
            )
        )
    pdf_only = [download for download in downloads if download.pdf_only]
    if pdf_only:
        print(f"PDF-only papers: {len(pdf_only)}.")
        print(
            "PDF-only IDs: "
            + " ".join(download.arxiv_id for download in pdf_only)
        )
    if failures:
        print("Failed IDs: " + " ".join(failure.paper for failure in failures))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Download arXiv papers' PDFs and submitted source packages."
    )
    parser.add_argument(
        "papers",
        nargs="+",
        metavar="PAPER",
        help="one or more arXiv IDs, citations, or abs/pdf/src/html URLs",
    )
    parser.add_argument(
        "-o",
        "--output-dir",
        type=Path,
        default=Path.cwd(),
        help="parent directory for the paper directory (default: current directory)",
    )
    parser.add_argument(
        "-f",
        "--force",
        action="store_true",
        help="replace files that have already been downloaded",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    downloads, failures = fetch_papers(
        args.papers,
        args.output_dir.expanduser(),
        force=args.force,
        pacer=RequestPacer(),
    )

    print_completion_summary(downloads, failures)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
