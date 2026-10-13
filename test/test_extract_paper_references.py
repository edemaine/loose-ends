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
import extract_paper_references as references


def make_paper(root: Path, name: str = "arXiv-1234.56789v1") -> Path:
    paper = root / name
    (paper / "source").mkdir(parents=True)
    (paper / "paper.pdf").write_bytes(b"%PDF-test")
    return paper


class LocateReferenceSourceTests(unittest.TestCase):
    def test_prefers_bbl_matching_the_main_tex_file(self):
        with TemporaryDirectory() as temporary:
            paper = make_paper(Path(temporary))
            source = paper / "source"
            (source / "main.tex").write_text(
                r"\documentclass{article}\begin{document}\cite{a}\end{document}",
                encoding="utf-8",
            )
            (source / "main.bbl").write_text(
                "\\begin{thebibliography}{1}\n\\bibitem{a} A. Author. Main.\n"
                "\\end{thebibliography}\n",
                encoding="utf-8",
            )
            (source / "old.bbl").write_text(
                "\\begin{thebibliography}{9}\n" + "\\bibitem{z} Old entry.\n" * 20
                + "\\end{thebibliography}\n",
                encoding="utf-8",
            )

            found = references.locate_reference_source(paper)

        self.assertEqual(found.kind, "bbl")
        self.assertEqual(found.files, ("source/main.bbl",))
        self.assertIn("Main.", found.text)
        self.assertIn("# Source: LaTeX .bbl bibliography", found.staged_text())

    def test_falls_back_to_embedded_thebibliography(self):
        with TemporaryDirectory() as temporary:
            paper = make_paper(Path(temporary))
            (paper / "source" / "paper.tex").write_text(
                "\\documentclass{article}\n\\begin{document}\nText.\n"
                "\\begin{thebibliography}{9}\n\\bibitem{k} K. Author. Title. 2001.\n"
                "\\end{thebibliography}\n\\end{document}\n",
                encoding="utf-8",
            )

            found = references.locate_reference_source(paper)

        self.assertEqual(found.kind, "thebibliography")
        self.assertEqual(found.files, ("source/paper.tex",))
        self.assertTrue(found.text.startswith("\\begin{thebibliography}"))
        self.assertTrue(found.text.endswith("\\end{thebibliography}"))

    def test_bib_database_records_cited_keys(self):
        with TemporaryDirectory() as temporary:
            paper = make_paper(Path(temporary))
            (paper / "source" / "paper.tex").write_text(
                "\\documentclass{article}\\begin{document}"
                "\\cite{first, second}\\citep[p.~3]{third}\\end{document}",
                encoding="utf-8",
            )
            (paper / "source" / "refs.bib").write_text(
                "@article{first, title={One}}\n@article{unused, title={Two}}\n",
                encoding="utf-8",
            )

            found = references.locate_reference_source(paper)

        self.assertEqual(found.kind, "bib")
        self.assertEqual(found.cited_keys, ("first", "second", "third"))
        self.assertIn("# Cited keys: first, second, third", found.staged_text())

    def test_pdf_text_is_trimmed_to_the_last_reference_heading(self):
        text = "\n".join([
            "Introduction", "References are discussed here.", "Body text",
            "REFERENCES", "[1] Old.", "Appendix", "References", "[1] A. Author.",
        ])
        self.assertEqual(
            references.reference_section(text),
            "References\n[1] A. Author.",
        )

    def test_pdf_text_without_heading_keeps_the_tail(self):
        lines = [f"line {index}" for index in range(100)]
        section = references.reference_section("\n".join(lines))
        self.assertTrue(section.startswith("line 60"))
        self.assertTrue(section.endswith("line 99"))

    def test_pdf_only_paper_uses_pdftotext_when_available(self):
        with TemporaryDirectory() as temporary:
            paper = Path(temporary) / "paper"
            paper.mkdir()
            (paper / "paper.pdf").write_bytes(b"%PDF-test")
            with patch.object(references, "pdftotext_executable", return_value="/bin/x"), \
                    patch.object(references, "pdf_text", return_value="Body\nReferences\n[1] X."):
                found = references.locate_reference_source(paper)
            self.assertEqual(found.kind, "pdf-text")
            self.assertEqual(found.text, "References\n[1] X.")
            with patch.object(references, "pdftotext_executable", return_value=None):
                found = references.locate_reference_source(paper)
            self.assertEqual(found.kind, "pdf")
            self.assertTrue(found.uses_pdf)


class ExtractionTests(unittest.TestCase):
    def test_cli_defaults_to_medium_reasoning(self):
        args = references.build_parser().parse_args(["paper"])
        self.assertEqual(args.reasoning_effort, "medium")

    def test_validate_result_normalizes_entries(self):
        result = references.validate_result({
            "references": [
                {
                    "key": " A1 ", "title": "Title\n  here", "authors": ["A. One", " "],
                    "year": "2001", "venue": "", "arxiv_id": "", "doi": "", "url": "",
                    "raw": "A. One. Title here. 2001.",
                },
            ],
        })
        self.assertEqual(result[0]["index"], 1)
        self.assertEqual(result[0]["key"], "A1")
        self.assertEqual(result[0]["title"], "Title here")
        self.assertEqual(result[0]["authors"], ["A. One"])

    def test_validate_result_rejects_entries_without_text(self):
        with self.assertRaises(references.ReferenceExtractionError):
            references.validate_result({"references": [{"title": "", "raw": ""}]})
        with self.assertRaises(references.ReferenceExtractionError):
            references.validate_result({"references": "nope"})

    def test_dry_run_needs_no_codex_executable(self):
        with TemporaryDirectory() as temporary:
            paper = make_paper(Path(temporary))
            (paper / "source" / "main.bbl").write_text("\\bibitem{a} A.", encoding="utf-8")
            output = StringIO()
            with redirect_stdout(output):
                status = references.main([str(paper), "--dry-run"])

        self.assertEqual(status, 0)
        self.assertIn("Would ask Codex to extract references", output.getvalue())
        self.assertIn("Source: bbl (source/main.bbl)", output.getvalue())

    def test_extraction_installs_manifest_and_skips_when_current(self):
        with TemporaryDirectory() as temporary:
            paper = make_paper(Path(temporary))
            (paper / "source" / "main.bbl").write_text(
                "\\bibitem{a} A. One. First. 2001.", encoding="utf-8"
            )
            schema = Path(temporary) / "schema.json"
            schema.write_text("{}", encoding="utf-8")

            def fake_run(*, workspace, **_):
                self.assertTrue((workspace / "references.txt").is_file())
                self.assertTrue((workspace / "paper-references.schema.json").is_file())
                result = workspace / "agent-result.json"
                result.write_text(json.dumps({
                    "references": [{
                        "key": "a", "title": "First", "authors": ["A. One"],
                        "year": "2001", "venue": "", "arxiv_id": "", "doi": "",
                        "url": "", "raw": "A. One. First. 2001.",
                    }],
                }), encoding="utf-8")
                (workspace / "run.log").write_text("log", encoding="utf-8")
                return result

            with patch.object(codex_cli, "run_structured_codex", side_effect=fake_run), \
                    patch.object(codex_cli, "read_codex_version", return_value="codex 1"), \
                    redirect_stdout(StringIO()):
                installed = references.extract_references(
                    paper,
                    codex="codex",
                    prompt="prompt",
                    schema_path=schema,
                    options=codex_cli.ModelOptions(model="m", reasoning_effort="low"),
                )
                manifest = json.loads(installed.read_text(encoding="utf-8"))
                self.assertEqual(manifest["source_kind"], "bbl")
                self.assertEqual(manifest["requested_model"], "m")
                self.assertEqual(manifest["references"][0]["title"], "First")
                self.assertEqual(manifest["references"][0]["index"], 1)
                self.assertTrue((paper / "references" / "source.txt").is_file())
                self.assertTrue((paper / "references" / "run.log").is_file())
                self.assertFalse(
                    [path for path in paper.iterdir() if path.name.startswith(".references-run")]
                )

                skipped = references.extract_references(
                    paper,
                    codex="codex",
                    prompt="prompt",
                    schema_path=schema,
                    options=codex_cli.ModelOptions(),
                )
                self.assertIsNone(skipped)


if __name__ == "__main__":
    unittest.main()
