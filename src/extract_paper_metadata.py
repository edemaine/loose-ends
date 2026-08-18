#!/usr/bin/env python3
"""Use one bounded Codex turn to populate an installed paper's metadata."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import tempfile

import artifact_reporting
import codex_cli
from ingest_paper import is_iso8601_date_or_timestamp


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PROMPT_PATH = PROJECT_ROOT / "prompts" / "extract-paper-metadata.md"
DEFAULT_SCHEMA_PATH = PROJECT_ROOT / "schemas" / "paper-metadata.schema.json"


class MetadataExtractionError(codex_cli.CodexError):
    pass


def load_metadata(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise MetadataExtractionError(f"could not read {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise MetadataExtractionError(f"metadata is not a JSON object: {path}")
    return value


def _date(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise MetadataExtractionError(f"Codex returned a non-string {field}")
    normalized = value.strip()
    if normalized and not is_iso8601_date_or_timestamp(normalized):
        raise MetadataExtractionError(
            f"Codex returned an invalid ISO 8601 {field}: {normalized!r}"
        )
    return normalized


def validate_result(value: object) -> dict:
    if not isinstance(value, dict):
        raise MetadataExtractionError("Codex metadata result is not an object")
    title = value.get("title")
    authors = value.get("authors")
    if not isinstance(title, str) or not title.strip():
        raise MetadataExtractionError("Codex returned no paper title")
    if not isinstance(authors, list) or not authors or not all(
        isinstance(author, str) and author.strip() for author in authors
    ):
        raise MetadataExtractionError("Codex returned no valid paper authors")
    normalized_authors = [author.strip() for author in authors]
    if len(set(normalized_authors)) != len(normalized_authors):
        raise MetadataExtractionError("Codex returned duplicate paper authors")
    return {
        "title": title.strip(),
        "authors": normalized_authors,
        "published": _date(value.get("published"), "published"),
        "updated": _date(value.get("updated"), "updated"),
    }


def metadata_is_present(metadata: dict) -> bool:
    return bool(
        isinstance(metadata.get("title"), str)
        and metadata["title"].strip()
        and isinstance(metadata.get("authors"), list)
        and metadata["authors"]
    )


def install_metadata(path: Path, extracted: dict) -> Path:
    metadata_path = path / "metadata.json"
    merged = load_metadata(metadata_path)
    merged.setdefault("schema_version", 1)
    merged["title"] = extracted["title"]
    merged["authors"] = extracted["authors"]
    for field in ("published", "updated"):
        if extracted[field] or not merged.get(field):
            merged[field] = extracted[field]
    temporary = metadata_path.with_name("metadata.json.tmp")
    try:
        temporary.write_text(
            json.dumps(merged, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, metadata_path)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise MetadataExtractionError(
            f"could not install extracted metadata at {metadata_path}: {exc}"
        ) from exc
    artifact_reporting.report_artifacts([metadata_path])
    return metadata_path


def extract_metadata(
    paper: Path,
    *,
    codex: str,
    prompt: str,
    schema_path: Path,
    options: codex_cli.ModelOptions,
    force: bool = False,
) -> Path | None:
    paper = paper.expanduser().resolve()
    pdf = paper / "paper.pdf"
    if not paper.is_dir() or not pdf.is_file():
        raise MetadataExtractionError(
            f"paper directory has no paper.pdf: {paper}"
        )
    metadata = load_metadata(paper / "metadata.json")
    if metadata_is_present(metadata) and not force:
        print(f"Metadata already present: {paper}")
        return None

    workspace = Path(tempfile.mkdtemp(prefix=".metadata-run-", dir=paper)).resolve()
    try:
        shutil.copy2(pdf, workspace / "paper.pdf")
        source = paper / "source"
        if source.is_dir():
            shutil.copytree(
                source,
                workspace / "source",
                copy_function=shutil.copyfile,
                symlinks=False,
            )
        elif source.is_file():
            shutil.copy2(source, workspace / "source")
        staged_schema = workspace / "paper-metadata.schema.json"
        shutil.copy2(schema_path, staged_schema)
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
            raise MetadataExtractionError(
                f"could not read Codex metadata result: {exc}"
            ) from exc
        metadata_path = install_metadata(paper, validate_result(result))
    except (OSError, shutil.Error) as exc:
        raise MetadataExtractionError(
            f"could not stage metadata extraction for {paper}: {exc}"
        ) from exc
    finally:
        shutil.rmtree(workspace, ignore_errors=True)
    print(f"Installed paper metadata: {metadata_path}")
    return metadata_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="extract title and authors from an installed paper with Codex"
    )
    parser.add_argument("paper", type=Path, help="installed paper directory")
    parser.add_argument("--force", action="store_true", help="replace populated metadata")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--codex", default="codex")
    codex_cli.add_prompt_arguments(
        parser,
        default_template=DEFAULT_PROMPT_PATH,
        task="paper metadata extractor",
    )
    codex_cli.add_model_arguments(parser)
    parser.add_argument("--schema", type=Path, default=DEFAULT_SCHEMA_PATH)
    return parser


def main(argv: list[str] | None = None) -> int:
    codex_cli.configure_utf8_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)
    paper = args.paper.expanduser().resolve()
    try:
        metadata = load_metadata(paper / "metadata.json")
        if not paper.is_dir() or not (paper / "paper.pdf").is_file():
            raise MetadataExtractionError(
                f"paper directory has no paper.pdf: {paper}"
            )
        if metadata_is_present(metadata) and not args.force:
            print(f"Would skip {paper}: title and authors are already present.")
            return 0
        if args.dry_run:
            print("Would ask Codex to extract title and authors from:")
            print(f"  PDF: {paper / 'paper.pdf'}")
            source = paper / "source"
            print(f"  Source: {source if source.exists() else 'none'}")
            return 0
        prompt_template = args.prompt_template.expanduser().resolve().read_text(
            encoding="utf-8"
        )
        prompt = codex_cli.with_user_prompt(
            prompt_template,
            args.prompt,
            task="paper metadata extraction",
        )
        extract_metadata(
            paper,
            codex=codex_cli.resolve_codex_executable(args.codex),
            prompt=prompt,
            schema_path=args.schema.expanduser().resolve(),
            options=codex_cli.model_options_from_args(args),
            force=args.force,
        )
    except (OSError, MetadataExtractionError) as exc:
        return codex_cli.report_error(parser, exc)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
