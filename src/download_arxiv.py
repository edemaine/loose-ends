#!/usr/bin/env python3
"""Download arXiv papers' PDFs and submitted source packages.

The public ``fetch_paper`` and ``fetch_papers`` functions are also used by the
author-search downloader.
"""

from __future__ import annotations

import argparse
import gzip
import os
import re
import shutil
import stat
import sys
import tarfile
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlparse
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
USER_AGENT = "loose-ends-arxiv-downloader/0.1"
DEFAULT_REQUEST_INTERVAL = 3.0


class DownloadError(RuntimeError):
    """A problem fetching or validating a file from arXiv."""


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
    source_path: Path


@dataclass(frozen=True)
class PaperFailure:
    paper: str
    error: str


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
        raise DownloadError(
            f"arXiv returned HTTP {exc.code} for {url}: {exc.reason}"
        ) from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise DownloadError(f"could not download {url}: {exc}") from exc


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


def fetch_paper(
    arxiv_id: str,
    output_dir: Path,
    *,
    force: bool = False,
    pacer: RequestPacer | None = None,
) -> tuple[Path, Path]:
    """Fetch the PDF and source package, returning their local paths."""
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
    old_source = existing_source_file(target_dir)
    if source_dir.is_dir() and not force:
        source_path = source_dir
        print(f"Already exists: {source_path}")
    elif source_dir.exists() and not force:
        raise DownloadError(f"source path is not a directory: {source_dir}")
    elif old_source is not None and not force:
        print(f"Extracting existing source package: {old_source}")
        source_path = extract_source(old_source, source_dir)
        print(f"Saved source files: {source_path}")
    else:
        print(f"Downloading source for {arxiv_id}...")
        source_download = target_dir / "source.download"
        result = download_to(
            f"https://arxiv.org/src/{quoted_id}",
            source_download,
            pacer=pacer,
        )
        header = file_header(source_download)
        if result.content_type == "text/html" or header.lstrip().lower().startswith(
            (b"<!doctype html", b"<html")
        ):
            source_download.unlink(missing_ok=True)
            raise DownloadError("arXiv's source endpoint returned HTML instead of source")

        source_package = target_dir / source_filename(result, header)
        os.replace(source_download, source_package)
        source_path = extract_source(source_package, source_dir, force=force)
        if force:
            for name in SOURCE_FILENAMES:
                stale_path = target_dir / name
                stale_path.unlink(missing_ok=True)
        print(f"Saved source files: {source_path}")

    return pdf_path, source_path


def fetch_papers(
    papers: Iterable[str],
    output_dir: Path,
    *,
    force: bool = False,
    pacer: RequestPacer | None = None,
) -> tuple[list[PaperDownload], list[PaperFailure]]:
    """Download a batch, continuing after invalid IDs or request failures."""
    downloads: list[PaperDownload] = []
    failures: list[PaperFailure] = []
    seen: set[str] = set()

    for paper in papers:
        try:
            arxiv_id = parse_arxiv_id(paper)
            if arxiv_id in seen:
                print(f"Skipping duplicate: arXiv:{arxiv_id}")
                continue
            seen.add(arxiv_id)
            pdf_path, source_path = fetch_paper(
                arxiv_id,
                output_dir,
                force=force,
                pacer=pacer,
            )
            downloads.append(PaperDownload(arxiv_id, pdf_path, source_path))
        except (ValueError, DownloadError) as exc:
            failures.append(PaperFailure(paper, str(exc)))
            print(f"error: {paper}: {exc}", file=sys.stderr)

    return downloads, failures


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

    print(
        f"Completed {len(downloads)} paper(s)"
        + (f"; {len(failures)} failed" if failures else "")
        + "."
    )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
