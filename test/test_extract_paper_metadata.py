from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import codex_cli
import extract_paper_metadata


class ExtractPaperMetadataTests(unittest.TestCase):
    def test_dry_run_needs_no_codex_executable(self):
        with TemporaryDirectory() as temporary:
            paper = Path(temporary) / "paper"
            paper.mkdir()
            (paper / "paper.pdf").write_bytes(b"%PDF-test")
            (paper / "source").mkdir()
            (paper / "source" / "main.tex").write_text("paper", encoding="utf-8")
            (paper / "metadata.json").write_text(
                '{"schema_version": 1, "title": "", "authors": []}\n',
                encoding="utf-8",
            )
            output = StringIO()
            with redirect_stdout(output):
                status = extract_paper_metadata.main([str(paper), "--dry-run"])

        self.assertEqual(status, 0)
        self.assertIn("Would ask Codex", output.getvalue())
        self.assertIn(f"PDF: {paper / 'paper.pdf'}", output.getvalue())
        self.assertIn(f"Source: {paper / 'source'}", output.getvalue())

    def test_extraction_preserves_provenance_and_installs_fields(self):
        with TemporaryDirectory() as temporary:
            paper = Path(temporary) / "paper"
            paper.mkdir()
            (paper / "paper.pdf").write_bytes(b"%PDF-test")
            (paper / "source").mkdir()
            (paper / "source" / "main.tex").write_text(
                r"\title{A Paper}", encoding="utf-8"
            )
            (paper / "metadata.json").write_text(
                json.dumps({
                    "schema_version": 1,
                    "title": "",
                    "authors": [],
                    "provenance": {"kind": "local"},
                }),
                encoding="utf-8",
            )
            schema = Path(temporary) / "schema.json"
            schema.write_text("{}", encoding="utf-8")

            def run_codex(**kwargs):
                self.assertTrue((kwargs["workspace"] / "source" / "main.tex").is_file())
                result = kwargs["workspace"] / "agent-result.json"
                result.write_text(json.dumps({
                    "title": "A Paper",
                    "authors": ["Ada Lovelace", "Alan Turing"],
                    "published": "2025-06-01",
                    "updated": "",
                }), encoding="utf-8")
                return result

            with (
                patch.object(codex_cli, "grant_sandbox_read_access"),
                patch.object(codex_cli, "run_structured_codex", side_effect=run_codex),
                redirect_stdout(StringIO()),
            ):
                result = extract_paper_metadata.extract_metadata(
                    paper,
                    codex="codex",
                    prompt="Extract metadata",
                    schema_path=schema,
                    options=codex_cli.ModelOptions(),
                )

            metadata = json.loads((paper / "metadata.json").read_text())

        self.assertEqual(result, paper / "metadata.json")
        self.assertEqual(metadata["title"], "A Paper")
        self.assertEqual(metadata["authors"], ["Ada Lovelace", "Alan Turing"])
        self.assertEqual(metadata["provenance"], {"kind": "local"})

    def test_validation_rejects_missing_authors(self):
        with self.assertRaisesRegex(
            extract_paper_metadata.MetadataExtractionError,
            "authors",
        ):
            extract_paper_metadata.validate_result({
                "title": "A Paper",
                "authors": [],
                "published": "",
                "updated": "",
            })

    def test_validation_preserves_reduced_precision_iso_dates(self):
        result = extract_paper_metadata.validate_result({
            "title": "A Paper",
            "authors": ["Ada Lovelace"],
            "published": "2025-12",
            "updated": "2026",
        })

        self.assertEqual(result["published"], "2025-12")
        self.assertEqual(result["updated"], "2026")


if __name__ == "__main__":
    unittest.main()
