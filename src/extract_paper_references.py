#!/usr/bin/env python3
"""Use one bounded Codex turn to extract an installed paper's reference list."""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

import analyze_papers
import artifact_reporting
import codex_cli


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PROMPT_PATH = PROJECT_ROOT / "prompts" / "extract-paper-references.md"
DEFAULT_SCHEMA_PATH = PROJECT_ROOT / "schemas" / "paper-references.schema.json"
DEFAULT_REASONING_EFFORT = "medium"
REFERENCES_DIRECTORY = "references"
REFERENCES_FILE = "references.json"
SOURCE_TEXT_FILE = "source.txt"
RUN_FILES = ("events.jsonl", "run.log")
SCHEMA_VERSION = 1
MAX_REFERENCE_TEXT_CHARACTERS = 400_000
PDF_TAIL_FRACTION = 0.4

REFERENCE_HEADING_RE = re.compile(
    r"^\s*(?:\d+\.?\s*)?(references|bibliography|literature cited|works cited)"
    r"\s*\.?\s*$",
    re.IGNORECASE,
)
THEBIBLIOGRAPHY_RE = re.compile(
    r"\\begin\{thebibliography\}.*?\\end\{thebibliography\}",
    re.DOTALL,
)
CITE_RE = re.compile(r"\\(?:no)?cite[a-zA-Z*]*(?:\[[^\]]*\]){0,2}\{([^}]*)\}")
DOCUMENTCLASS_RE = re.compile(r"\\documentclass")


class ReferenceExtractionError(codex_cli.CodexError):
    pass


@dataclass(frozen=True)
class ReferenceSource:
    """Where a paper's bibliography text came from and what it contains."""

    kind: str
    files: tuple[str, ...]
    text: str = ""
    cited_keys: tuple[str, ...] = field(default_factory=tuple)

    @property
    def uses_pdf(self) -> bool:
        return self.kind == "pdf"

    def staged_text(self) -> str:
        header = {
            "bbl": "LaTeX .bbl bibliography",
            "thebibliography": "thebibliography environment embedded in LaTeX source",
            "bib": "BibTeX .bib database",
            "pdf-text": "reference section of the rendered PDF text",
        }[self.kind]
        lines = [f"# Source: {header} ({', '.join(self.files)})"]
        if self.kind == "bib":
            keys = ", ".join(self.cited_keys) if self.cited_keys else "(none found)"
            lines.append(f"# Cited keys: {keys}")
        lines.append("")
        lines.append(self.text)
        return "\n".join(lines)


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise ReferenceExtractionError(f"could not read {path}: {exc}") from exc


def _source_files(paper: Path, suffix: str) -> list[Path]:
    source = paper / "source"
    if not source.is_dir():
        return []
    return sorted(
        (path for path in source.rglob(f"*{suffix}") if path.is_file()),
        key=lambda path: path.as_posix(),
    )


def _relative(paper: Path, path: Path) -> str:
    try:
        return path.relative_to(paper).as_posix()
    except ValueError:
        return path.name


def _main_tex_stems(paper: Path) -> set[str]:
    stems = set()
    for path in _source_files(paper, ".tex"):
        try:
            head = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if DOCUMENTCLASS_RE.search(head):
            stems.add(path.stem)
    return stems


def _choose_bbl(paper: Path) -> Path | None:
    candidates = [
        path for path in _source_files(paper, ".bbl")
        if path.stat().st_size > 0
    ]
    if not candidates:
        return None
    main_stems = _main_tex_stems(paper)
    preferred = [path for path in candidates if path.stem in main_stems]
    pool = preferred or candidates
    return max(pool, key=lambda path: (path.stat().st_size, path.as_posix()))


def _embedded_bibliographies(paper: Path) -> list[tuple[Path, str]]:
    found = []
    for path in _source_files(paper, ".tex"):
        text = _read_text(path)
        for match in THEBIBLIOGRAPHY_RE.finditer(text):
            found.append((path, match.group(0)))
    return found


def _cited_keys(paper: Path) -> tuple[str, ...]:
    keys: dict[str, None] = {}
    for path in _source_files(paper, ".tex"):
        for match in CITE_RE.finditer(_read_text(path)):
            for key in match.group(1).split(","):
                key = key.strip()
                if key:
                    keys.setdefault(key, None)
    return tuple(keys)


def pdftotext_executable() -> str | None:
    return shutil.which("pdftotext")


def pdf_text(pdf: Path) -> str:
    executable = pdftotext_executable()
    if executable is None:
        raise ReferenceExtractionError("pdftotext is not installed")
    try:
        completed = subprocess.run(
            [executable, "-enc", "UTF-8", str(pdf), "-"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=300,
            **codex_cli.windowless_popen_options(new_process_group=False),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ReferenceExtractionError(f"pdftotext failed for {pdf}: {exc}") from exc
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise ReferenceExtractionError(
            f"pdftotext failed for {pdf}: {detail or completed.returncode}"
        )
    return completed.stdout.decode("utf-8", errors="replace")


def reference_section(text: str) -> str:
    """Return the text from the last reference heading onward."""
    lines = text.splitlines()
    heading = None
    for index, line in enumerate(lines):
        if REFERENCE_HEADING_RE.match(line):
            heading = index
    if heading is None:
        start = int(len(lines) * (1 - PDF_TAIL_FRACTION))
        return "\n".join(lines[start:]).strip()
    return "\n".join(lines[heading:]).strip()


def locate_reference_source(paper: Path) -> ReferenceSource:
    """Find the best available bibliography text for ``paper``."""
    bbl = _choose_bbl(paper)
    if bbl is not None:
        return ReferenceSource(
            kind="bbl",
            files=(_relative(paper, bbl),),
            text=_read_text(bbl),
        )
    embedded = _embedded_bibliographies(paper)
    if embedded:
        files = tuple(dict.fromkeys(_relative(paper, path) for path, _ in embedded))
        text = "\n\n".join(chunk for _, chunk in embedded)
        return ReferenceSource(kind="thebibliography", files=files, text=text)
    bibs = [path for path in _source_files(paper, ".bib") if path.stat().st_size > 0]
    if bibs:
        text = "\n\n".join(
            f"% {_relative(paper, path)}\n{_read_text(path)}" for path in bibs
        )
        return ReferenceSource(
            kind="bib",
            files=tuple(_relative(paper, path) for path in bibs),
            text=text,
            cited_keys=_cited_keys(paper),
        )
    pdf = paper / "paper.pdf"
    if not pdf.is_file():
        raise ReferenceExtractionError(
            f"paper has no bibliography source and no paper.pdf: {paper}"
        )
    if pdftotext_executable() is not None:
        section = reference_section(pdf_text(pdf))
        if section:
            return ReferenceSource(kind="pdf-text", files=("paper.pdf",), text=section)
    return ReferenceSource(kind="pdf", files=("paper.pdf",))


def _string(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise ReferenceExtractionError(f"Codex returned a non-string reference {name}")
    return " ".join(value.split())


def validate_result(value: object) -> list[dict]:
    if not isinstance(value, dict):
        raise ReferenceExtractionError("Codex references result is not an object")
    references = value.get("references")
    if not isinstance(references, list):
        raise ReferenceExtractionError("Codex returned no reference list")
    normalized = []
    for index, entry in enumerate(references):
        if not isinstance(entry, dict):
            raise ReferenceExtractionError(f"reference {index + 1} is not an object")
        authors = entry.get("authors", [])
        if not isinstance(authors, list) or not all(
            isinstance(author, str) for author in authors
        ):
            raise ReferenceExtractionError(
                f"reference {index + 1} has invalid authors"
            )
        record = {
            "index": index + 1,
            "key": _string(entry.get("key", ""), "key"),
            "title": _string(entry.get("title", ""), "title"),
            "authors": [
                " ".join(author.split()) for author in authors if author.strip()
            ],
            "year": _string(entry.get("year", ""), "year"),
            "venue": _string(entry.get("venue", ""), "venue"),
            "arxiv_id": _string(entry.get("arxiv_id", ""), "arxiv_id"),
            "doi": _string(entry.get("doi", ""), "doi"),
            "url": _string(entry.get("url", ""), "url"),
            "raw": _string(entry.get("raw", ""), "raw"),
        }
        if not (record["title"] or record["raw"]):
            raise ReferenceExtractionError(
                f"reference {index + 1} has neither a title nor raw text"
            )
        normalized.append(record)
    return normalized


def references_directory(paper: Path) -> Path:
    return paper / REFERENCES_DIRECTORY


def load_manifest(paper: Path) -> dict:
    path = references_directory(paper) / REFERENCES_FILE
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReferenceExtractionError(f"could not read {path}: {exc}") from exc
    return value if isinstance(value, dict) else {}


def references_are_current(paper: Path, manifest: dict, digest: str) -> bool:
    return bool(manifest) and manifest.get("source_digest") == digest


def install_references(
    paper: Path,
    *,
    references: list[dict],
    source: ReferenceSource,
    digest: str,
    options: codex_cli.ModelOptions,
    codex_version: str,
    workspace: Path | None = None,
) -> Path:
    directory = references_directory(paper)
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source_digest": digest,
        "source_kind": source.kind,
        "source_files": list(source.files),
        "codex_version": codex_version,
        "requested_model": options.model or "",
        "requested_reasoning_effort": options.reasoning_effort or "",
        "requested_fast_mode": bool(options.fast),
        "references": references,
    }
    try:
        directory.mkdir(parents=True, exist_ok=True)
        installed = [directory / REFERENCES_FILE]
        if source.text:
            (directory / SOURCE_TEXT_FILE).write_text(
                source.staged_text(), encoding="utf-8"
            )
            installed.append(directory / SOURCE_TEXT_FILE)
        if workspace is not None:
            for filename in RUN_FILES:
                staged = workspace / filename
                if staged.is_file():
                    shutil.copyfile(staged, directory / filename)
        temporary = directory / f"{REFERENCES_FILE}.tmp"
        temporary.write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, directory / REFERENCES_FILE)
    except OSError as exc:
        raise ReferenceExtractionError(
            f"could not install references at {directory}: {exc}"
        ) from exc
    artifact_reporting.report_artifacts(installed)
    return directory / REFERENCES_FILE


def extract_references(
    paper: Path,
    *,
    codex: str,
    prompt: str,
    schema_path: Path,
    options: codex_cli.ModelOptions,
    force: bool = False,
) -> Path | None:
    paper = paper.expanduser().resolve()
    if not analyze_papers.is_paper_directory(paper):
        raise ReferenceExtractionError(f"not an installed paper directory: {paper}")
    digest = analyze_papers.source_digest(paper)
    if not force and references_are_current(paper, load_manifest(paper), digest):
        print(f"References already extracted: {paper}")
        return None
    source = locate_reference_source(paper)
    if len(source.text) > MAX_REFERENCE_TEXT_CHARACTERS:
        source = ReferenceSource(
            kind=source.kind,
            files=source.files,
            text=source.text[-MAX_REFERENCE_TEXT_CHARACTERS:],
            cited_keys=source.cited_keys,
        )
    workspace = Path(
        tempfile.mkdtemp(prefix=".references-run-", dir=paper)
    ).resolve()
    try:
        if source.uses_pdf:
            shutil.copyfile(paper / "paper.pdf", workspace / "paper.pdf")
        else:
            (workspace / "references.txt").write_text(
                source.staged_text(), encoding="utf-8"
            )
        staged_schema = workspace / DEFAULT_SCHEMA_PATH.name
        shutil.copyfile(schema_path, staged_schema)
        codex_cli.grant_sandbox_read_access(workspace)
        result_path = codex_cli.run_structured_codex(
            codex=codex,
            workspace=workspace,
            prompt=prompt,
            schema_path=staged_schema,
            options=options,
        )
        try:
            result = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ReferenceExtractionError(
                f"could not read Codex references result: {exc}"
            ) from exc
        references = validate_result(result)
        installed = install_references(
            paper,
            references=references,
            source=source,
            digest=digest,
            options=options,
            codex_version=codex_cli.read_codex_version(codex),
            workspace=workspace,
        )
    except (OSError, shutil.Error) as exc:
        raise ReferenceExtractionError(
            f"could not stage reference extraction for {paper}: {exc}"
        ) from exc
    finally:
        shutil.rmtree(workspace, ignore_errors=True)
    print(
        f"Installed {len(references)} references from {source.kind}: {installed}"
    )
    return installed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="extract the reference list of installed papers with Codex"
    )
    parser.add_argument(
        "papers", nargs="+", type=Path, help="installed paper directories"
    )
    parser.add_argument(
        "--force", action="store_true", help="replace current references"
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--codex", default="codex")
    codex_cli.add_prompt_arguments(
        parser,
        default_template=DEFAULT_PROMPT_PATH,
        task="paper reference extractor",
    )
    codex_cli.add_model_arguments(
        parser,
        default_reasoning_effort=DEFAULT_REASONING_EFFORT,
    )
    parser.add_argument("--schema", type=Path, default=DEFAULT_SCHEMA_PATH)
    return parser


def _describe_dry_run(paper: Path, *, force: bool) -> None:
    digest = analyze_papers.source_digest(paper)
    if not force and references_are_current(paper, load_manifest(paper), digest):
        print(f"Would skip {paper}: references are current.")
        return
    source = locate_reference_source(paper)
    print(f"Would ask Codex to extract references for {paper}")
    print(f"  Source: {source.kind} ({', '.join(source.files)})")
    if source.text:
        print(f"  Text: {len(source.text):,} characters")
    if source.kind == "bib":
        print(f"  Cited keys: {len(source.cited_keys)}")


def main(argv: list[str] | None = None) -> int:
    codex_cli.configure_utf8_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        papers = [paper.expanduser().resolve() for paper in args.papers]
        for paper in papers:
            if not analyze_papers.is_paper_directory(paper):
                raise ReferenceExtractionError(
                    f"not an installed paper directory: {paper}"
                )
        if args.dry_run:
            for paper in papers:
                _describe_dry_run(paper, force=args.force)
            return 0
        prompt_template = args.prompt_template.expanduser().resolve().read_text(
            encoding="utf-8"
        )
        prompt = codex_cli.with_user_prompt(
            prompt_template,
            args.prompt,
            task="paper reference extraction",
        )
        codex = codex_cli.resolve_codex_executable(args.codex)
        options = codex_cli.model_options_from_args(args)
        for paper in papers:
            extract_references(
                paper,
                codex=codex,
                prompt=prompt,
                schema_path=args.schema.expanduser().resolve(),
                options=options,
                force=args.force,
            )
    except (OSError, analyze_papers.AnalysisError, ReferenceExtractionError) as exc:
        return codex_cli.report_error(parser, exc)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
