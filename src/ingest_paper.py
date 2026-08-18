#!/usr/bin/env python3
"""Install an arbitrary local paper in the Loose Ends directory format."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import tarfile
import tempfile
import zipfile

import artifact_reporting


class IngestError(RuntimeError):
    pass


ARCHIVE_MAX_FILES = 20_000
ARCHIVE_MAX_UNCOMPRESSED_BYTES = 4 * 1024 * 1024 * 1024
REDUCED_ISO_DATE_RE = re.compile(r"^(?P<year>[0-9]{4})(?:-(?P<month>[0-9]{2}))?$")


@dataclass(frozen=True)
class PaperInput:
    supplied: Path
    pdf: Path
    source: Path | None
    name: str


def directory_slug(value: str) -> str:
    """Return a conservative, portable paper-directory name."""
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip())
    slug = re.sub(r"-+", "-", slug).strip(".-_")
    if not slug:
        raise IngestError("paper name must contain a letter or number")
    device_name = slug.partition(".")[0]
    if device_name.lower() in {
        "con", "prn", "aux", "nul", "clock$", "conin$", "conout$"
    } or re.fullmatch(
        r"(?:com|lpt)[1-9]", device_name, flags=re.IGNORECASE
    ):
        slug = f"paper-{slug}"
    return slug[:120].rstrip(".-_")


def validate_pdf(path: Path) -> Path:
    path = path.expanduser().resolve()
    if not path.is_file():
        raise IngestError(f"PDF does not exist: {path}")
    try:
        with path.open("rb") as source:
            header = source.read(5)
    except OSError as exc:
        raise IngestError(f"could not read PDF {path}: {exc}") from exc
    if header != b"%PDF-":
        raise IngestError(f"file is not a PDF: {path}")
    return path


def validate_source(path: Path | None) -> Path | None:
    if path is None:
        return None
    resolved = path.expanduser().resolve()
    if not resolved.exists():
        raise IngestError(f"source path does not exist: {resolved}")
    if resolved.is_symlink():
        raise IngestError(f"source path may not be a symbolic link: {resolved}")
    if not resolved.is_file() and not resolved.is_dir():
        raise IngestError(f"source path is not a file or directory: {resolved}")
    if resolved.is_dir():
        for directory, child_directories, filenames in os.walk(
            resolved, followlinks=False
        ):
            for name in [*child_directories, *filenames]:
                child = Path(directory) / name
                if child.is_symlink():
                    raise IngestError(
                        f"source tree contains a symbolic link: {child}"
                    )
    return resolved


def select_compiled_pdf(directory: Path) -> Path:
    """Identify the rendered paper in a source directory without guessing."""
    source = validate_source(directory)
    if source is None or not source.is_dir():
        raise IngestError(f"paper input is not a directory: {directory}")

    root_file_list = sorted(
        (child for child in source.iterdir() if child.is_file()),
        key=lambda child: child.name.casefold(),
    )
    root_files = {child.name.casefold(): child for child in root_file_list}
    for preferred in ("paper.pdf", "main.pdf"):
        if candidate := root_files.get(preferred):
            return validate_pdf(candidate)

    tex_stems = {
        path.stem.casefold()
        for path in root_file_list
        if path.suffix.casefold() == ".tex"
    }
    matches = [
        path
        for path in root_file_list
        if path.suffix.casefold() == ".pdf"
        and path.stem.casefold() in tex_stems
    ]

    if not matches:
        raise IngestError(
            f"could not identify the compiled PDF in {source}; expected "
            "root paper.pdf, root main.pdf, or one root PDF matching a root TeX filename"
        )
    if len(matches) > 1:
        relative = ", ".join(str(path.relative_to(source)) for path in matches)
        raise IngestError(
            f"multiple possible compiled PDFs in {source}: {relative}"
        )
    return validate_pdf(matches[0])


def archive_kind(path: Path) -> str:
    name = path.name.casefold()
    if name.endswith(".zip"):
        return "zip"
    if name.endswith((".tar.gz", ".tgz")):
        return "tar.gz"
    return ""


def archive_paper_name(path: Path) -> str:
    name = path.name
    for suffix in (".tar.gz", ".tgz", ".zip"):
        if name.casefold().endswith(suffix):
            return name[:-len(suffix)]
    return path.stem


def _archive_relative_path(value: str) -> PurePosixPath:
    if not value or "\\" in value or "\0" in value or ":" in value:
        raise IngestError(f"unsafe archive path: {value!r}")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part == ".." for part in path.parts):
        raise IngestError(f"unsafe archive path: {value!r}")
    if not path.parts:
        raise IngestError(f"unsafe archive path: {value!r}")
    return path


def _common_directory_prefix(paths: list[PurePosixPath]) -> tuple[str, ...]:
    parents = [path.parts[:-1] for path in paths]
    if not parents:
        return ()
    prefix: list[str] = []
    for values in zip(*parents):
        if len(set(values)) != 1:
            break
        prefix.append(values[0])
    return tuple(prefix)


def _archive_destination(
    root: Path,
    relative: PurePosixPath,
    prefix: tuple[str, ...],
) -> Path:
    parts = relative.parts[len(prefix):]
    if not parts:
        raise IngestError(f"archive entry has no filename: {relative}")
    return root.joinpath(*parts)


def _extract_zip(path: Path, destination: Path) -> None:
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            if len(infos) > ARCHIVE_MAX_FILES:
                raise IngestError("archive contains too many entries")
            files: list[tuple[zipfile.ZipInfo, PurePosixPath]] = []
            total = 0
            for info in infos:
                relative = _archive_relative_path(info.filename)
                mode = (info.external_attr >> 16) & 0xFFFF
                kind = stat.S_IFMT(mode)
                if kind not in {0, stat.S_IFREG, stat.S_IFDIR}:
                    raise IngestError(f"archive contains a link or special file: {info.filename}")
                if info.is_dir():
                    continue
                total += info.file_size
                if total > ARCHIVE_MAX_UNCOMPRESSED_BYTES:
                    raise IngestError("archive expands beyond the size limit")
                files.append((info, relative))
            if not files:
                raise IngestError("archive contains no files")
            prefix = _common_directory_prefix([relative for _, relative in files])
            seen: set[str] = set()
            for info, relative in files:
                target = _archive_destination(destination, relative, prefix)
                key = os.path.normcase(str(target))
                if key in seen or target.exists():
                    raise IngestError(f"archive contains duplicate path: {relative}")
                seen.add(key)
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as source, target.open("xb") as output:
                    shutil.copyfileobj(source, output, length=1024 * 1024)
    except IngestError:
        raise
    except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
        raise IngestError(f"could not extract ZIP archive {path}: {exc}") from exc


def _extract_tar_gz(path: Path, destination: Path) -> None:
    try:
        with tarfile.open(path, mode="r:gz") as archive:
            members = archive.getmembers()
            if len(members) > ARCHIVE_MAX_FILES:
                raise IngestError("archive contains too many entries")
            files: list[tuple[tarfile.TarInfo, PurePosixPath]] = []
            total = 0
            for member in members:
                relative = _archive_relative_path(member.name)
                if member.isdir():
                    continue
                if not member.isfile():
                    raise IngestError(
                        f"archive contains a link or special file: {member.name}"
                    )
                total += member.size
                if total > ARCHIVE_MAX_UNCOMPRESSED_BYTES:
                    raise IngestError("archive expands beyond the size limit")
                files.append((member, relative))
            if not files:
                raise IngestError("archive contains no files")
            prefix = _common_directory_prefix([relative for _, relative in files])
            seen: set[str] = set()
            for member, relative in files:
                target = _archive_destination(destination, relative, prefix)
                key = os.path.normcase(str(target))
                if key in seen or target.exists():
                    raise IngestError(f"archive contains duplicate path: {relative}")
                seen.add(key)
                target.parent.mkdir(parents=True, exist_ok=True)
                source = archive.extractfile(member)
                if source is None:
                    raise IngestError(f"could not read archive entry: {member.name}")
                with source, target.open("xb") as output:
                    shutil.copyfileobj(source, output, length=1024 * 1024)
    except IngestError:
        raise
    except (OSError, tarfile.TarError) as exc:
        raise IngestError(f"could not extract tar.gz archive {path}: {exc}") from exc


def extract_paper_archive(path: Path, destination: Path) -> Path:
    """Safely extract a supported paper archive, stripping its shared prefix."""
    kind = archive_kind(path)
    if not kind:
        raise IngestError(f"unsupported paper archive: {path}")
    destination.mkdir(parents=True)
    if kind == "zip":
        _extract_zip(path, destination)
    else:
        _extract_tar_gz(path, destination)
    return destination


def prepare_paper_inputs(
    inputs: list[Path],
    output_dir: Path,
    *,
    archive_directory: Path | None = None,
) -> list[PaperInput]:
    """Resolve and preflight the metadata-free batch-ingestion shorthand."""
    output_dir = output_dir.expanduser().resolve()
    prepared: list[PaperInput] = []
    targets: dict[str, Path] = {}
    for index, supplied in enumerate(inputs):
        path = supplied.expanduser().resolve()
        kind = archive_kind(path) if path.is_file() else ""
        if kind:
            if archive_directory is None:
                raise IngestError("archive staging directory is unavailable")
            source = extract_paper_archive(
                path,
                archive_directory / f"archive-{index}",
            )
            pdf = select_compiled_pdf(source)
            name = archive_paper_name(path)
        elif path.is_file():
            pdf = validate_pdf(path)
            source = None
            name = path.stem
        elif path.is_dir():
            pdf = select_compiled_pdf(path)
            source = path
            name = path.name
        else:
            raise IngestError(f"paper input does not exist: {path}")
        slug = directory_slug(name)
        key = os.path.normcase(slug)
        if key in targets:
            raise IngestError(
                f"multiple inputs would use paper directory {slug}: "
                f"{targets[key]} and {path}"
            )
        target = output_dir / slug
        if target.exists():
            raise IngestError(f"paper directory already exists: {target}")
        targets[key] = path
        prepared.append(PaperInput(path, pdf, source, name))
    return prepared


def ingest_inputs(
    inputs: list[Path], output_dir: Path, *, dry_run: bool = False
) -> list[Path]:
    """Install PDF files, archives, and/or source directories with blank metadata."""
    if not inputs:
        raise IngestError("at least one PDF file, archive, or directory is required")
    with tempfile.TemporaryDirectory(prefix="paper-archives-") as temporary:
        prepared = prepare_paper_inputs(
            inputs,
            output_dir,
            archive_directory=Path(temporary),
        )
        return [
            ingest_paper(
                item.pdf,
                output_dir,
                name=item.name,
                source=item.source,
                dry_run=dry_run,
            )
            for item in prepared
        ]


def is_iso8601_date_or_timestamp(value: str) -> bool:
    """Accept full Python ISO dates/timestamps plus ISO year/year-month precision."""
    match = REDUCED_ISO_DATE_RE.fullmatch(value)
    if match:
        year = int(match.group("year"))
        month = match.group("month")
        return year >= 1 and (month is None or 1 <= int(month) <= 12)
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def _iso_timestamp(value: str | None, name: str) -> str:
    if not value:
        return ""
    normalized = value.strip()
    if not is_iso8601_date_or_timestamp(normalized):
        raise IngestError(f"{name} must be an ISO 8601 date or timestamp")
    return normalized


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def metadata_payload(
    *,
    pdf: Path,
    title: str = "",
    authors: list[str] | None = None,
    published: str = "",
    updated: str = "",
    url: str = "",
    doi: str = "",
) -> dict:
    title = title.strip()
    authors = [author.strip() for author in (authors or []) if author.strip()]
    if len(set(authors)) != len(authors):
        raise IngestError("authors must not contain duplicates")
    payload: dict[str, object] = {
        "schema_version": 1,
        "title": title,
        "authors": authors,
        "published": _iso_timestamp(published, "published"),
        "updated": _iso_timestamp(updated, "updated"),
        "provenance": {
            "kind": "local",
            "original_filename": pdf.name,
            "sha256": _sha256(pdf),
            "ingested_at": datetime.now(timezone.utc).isoformat().replace(
                "+00:00", "Z"
            ),
        },
    }
    if url.strip():
        payload["url"] = url.strip()
    if doi.strip():
        payload["doi"] = doi.strip()
    return payload


def _copy_source(source: Path, destination: Path) -> None:
    destination.mkdir()
    if source.is_file():
        shutil.copy2(source, destination / source.name)
        return
    for child in source.iterdir():
        target = destination / child.name
        if child.is_symlink():
            raise IngestError(f"source tree contains a symbolic link: {child}")
        if child.is_dir():
            shutil.copytree(child, target, symlinks=False)
        elif child.is_file():
            shutil.copy2(child, target)


def ingest_paper(
    pdf: Path,
    output_dir: Path,
    *,
    name: str,
    title: str = "",
    authors: list[str] | None = None,
    source: Path | None = None,
    published: str = "",
    updated: str = "",
    url: str = "",
    doi: str = "",
    dry_run: bool = False,
) -> Path:
    pdf = validate_pdf(pdf)
    source = validate_source(source)
    output_dir = output_dir.expanduser().resolve()
    target = output_dir / directory_slug(name)
    payload = metadata_payload(
        pdf=pdf,
        title=title,
        authors=authors,
        published=published,
        updated=updated,
        url=url,
        doi=doi,
    )
    if target.exists():
        raise IngestError(
            f"paper directory already exists: {target}; choose another --name"
        )
    if source is not None and source.is_dir() and (
        source == target or source in target.parents or target in source.parents
    ):
        raise IngestError("output directory and supplied source may not overlap")
    if dry_run:
        print(f"Would install paper: {target}")
        print(f"  PDF: {pdf}")
        print(f"  Source: {source if source is not None else 'none'}")
        print(f"  Title: {payload['title'] or '(blank)'}")
        print(f"  Authors: {', '.join(payload['authors']) or '(blank)'}")
        return target

    output_dir.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(prefix=f".paper-install-{target.name}-", dir=output_dir)
    )
    try:
        shutil.copy2(pdf, staging / "paper.pdf")
        if source is not None:
            _copy_source(source, staging / "source")
        (staging / "metadata.json").write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        os.replace(staging, target)
    except (OSError, shutil.Error) as exc:
        raise IngestError(f"could not install {target}: {exc}") from exc
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)

    artifact_reporting.report_artifacts(
        [target / "paper.pdf", target / "metadata.json"]
    )
    print(f"Installed paper: {target}")
    return target


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Install an arbitrary PDF in the Loose Ends paper format."
    )
    parser.add_argument(
        "inputs", nargs="+", type=Path,
        help="rendered PDFs, ZIP/tar.gz archives, and/or source directories",
    )
    parser.add_argument(
        "--title", default="",
        help="paper title (optional; may be filled in later)",
    )
    parser.add_argument(
        "--author", action="append", default=[],
        help="optional author in display order; repeat for every author",
    )
    parser.add_argument(
        "--name", help="paper directory name (single-PDF mode only)"
    )
    parser.add_argument(
        "--source", type=Path,
        help="optional source file or directory copied under source/",
    )
    parser.add_argument("--published", default="", help="ISO 8601 publication date")
    parser.add_argument("--updated", default="", help="ISO 8601 update date")
    parser.add_argument("--url", default="", help="canonical paper URL")
    parser.add_argument("--doi", default="", help="DOI")
    parser.add_argument(
        "-o", "--output-dir", type=Path, default=Path.cwd(),
        help="parent directory for the installed paper (default: current directory)",
    )
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        detailed = bool(
            args.name or args.title or args.author or args.source
            or args.published or args.updated or args.url or args.doi
        )
        if detailed:
            if len(args.inputs) != 1 or not args.inputs[0].is_file():
                raise IngestError(
                    "metadata, --name, and --source options require exactly one PDF input"
                )
            pdf = args.inputs[0]
            ingest_paper(
                pdf,
                args.output_dir,
                name=args.name or pdf.stem,
                title=args.title,
                authors=args.author,
                source=args.source,
                published=args.published,
                updated=args.updated,
                url=args.url,
                doi=args.doi,
                dry_run=args.dry_run,
            )
        else:
            ingest_inputs(args.inputs, args.output_dir, dry_run=args.dry_run)
    except IngestError as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
