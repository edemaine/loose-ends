from io import BytesIO
from pathlib import Path
import json
import sys
import tarfile
from tempfile import TemporaryDirectory
import unittest
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ingest_paper import (
    IngestError,
    directory_slug,
    ingest_inputs,
    ingest_paper,
    select_compiled_pdf,
)


class IngestPaperTests(unittest.TestCase):
    def test_installs_with_blank_metadata_for_later_extraction(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            pdf = root / "paper.pdf"
            pdf.write_bytes(b"%PDF-test")

            target = ingest_paper(pdf, root / "papers", name="paper")

            metadata = json.loads((target / "metadata.json").read_text())
            self.assertEqual(metadata["title"], "")
            self.assertEqual(metadata["authors"], [])

    def test_installs_pdf_source_and_normalized_metadata(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            pdf = root / "input paper.pdf"
            pdf.write_bytes(b"%PDF-test")
            source = root / "input-source"
            source.mkdir()
            (source / "main.tex").write_text("paper", encoding="utf-8")

            target = ingest_paper(
                pdf,
                root / "papers",
                name="A Paper: 2025",
                title="A Paper",
                authors=["Ada Lovelace", "Alan Turing"],
                source=source,
                published="2025-06-01",
                doi="10.1234/example",
            )

            self.assertEqual(target.name, "A-Paper-2025")
            self.assertEqual((target / "paper.pdf").read_bytes(), b"%PDF-test")
            self.assertEqual(
                (target / "source" / "main.tex").read_text(encoding="utf-8"),
                "paper",
            )
            metadata = json.loads((target / "metadata.json").read_text())
            self.assertEqual(metadata["title"], "A Paper")
            self.assertEqual(metadata["authors"], ["Ada Lovelace", "Alan Turing"])
            self.assertEqual(metadata["published"], "2025-06-01")
            self.assertEqual(metadata["doi"], "10.1234/example")
            self.assertEqual(metadata["provenance"]["kind"], "local")
            self.assertEqual(len(metadata["provenance"]["sha256"]), 64)

    def test_accepts_reduced_precision_iso_dates(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            pdf = root / "paper.pdf"
            pdf.write_bytes(b"%PDF-test")

            target = ingest_paper(
                pdf,
                root / "papers",
                name="paper",
                published="2025-12",
                updated="2026",
            )
            metadata = json.loads((target / "metadata.json").read_text())

        self.assertEqual(metadata["published"], "2025-12")
        self.assertEqual(metadata["updated"], "2026")

    def test_dry_run_writes_nothing(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            pdf = root / "paper.pdf"
            pdf.write_bytes(b"%PDF-test")
            target = ingest_paper(
                pdf,
                root / "papers",
                name="paper",
                title="Paper",
                authors=["Ada Lovelace"],
                dry_run=True,
            )
            self.assertFalse(target.exists())

    def test_refuses_invalid_pdf_and_existing_target(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            pdf = root / "paper.pdf"
            pdf.write_bytes(b"not a PDF")
            with self.assertRaisesRegex(IngestError, "not a PDF"):
                ingest_paper(
                    pdf, root, name="paper", title="Paper",
                    authors=["Ada Lovelace"],
                )
            pdf.write_bytes(b"%PDF-test")
            (root / "paper").mkdir()
            with self.assertRaisesRegex(IngestError, "already exists"):
                ingest_paper(
                    pdf, root, name="paper", title="Paper",
                    authors=["Ada Lovelace"],
                )

    def test_portable_slug(self):
        self.assertEqual(directory_slug("CON"), "paper-CON")
        self.assertEqual(directory_slug("con.txt"), "paper-con.txt")
        self.assertEqual(directory_slug("  one / two  "), "one-two")

    def test_selects_compiled_pdf_by_preference_then_tex_match(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "article.tex").write_text("paper", encoding="utf-8")
            (root / "article.pdf").write_bytes(b"%PDF-article")
            self.assertEqual(select_compiled_pdf(root), root / "article.pdf")

            (root / "main.pdf").write_bytes(b"%PDF-main")
            self.assertEqual(select_compiled_pdf(root), root / "main.pdf")

            (root / "paper.pdf").write_bytes(b"%PDF-paper")
            self.assertEqual(select_compiled_pdf(root), root / "paper.pdf")

    def test_refuses_ambiguous_or_unresolved_directory_pdf(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            for stem in ("first", "second"):
                (root / f"{stem}.tex").write_text("paper", encoding="utf-8")
                (root / f"{stem}.pdf").write_bytes(b"%PDF-test")
            with self.assertRaisesRegex(IngestError, "multiple possible"):
                select_compiled_pdf(root)

            for path in root.iterdir():
                path.unlink()
            nested = root / "nested"
            nested.mkdir()
            (nested / "article.tex").write_text("paper", encoding="utf-8")
            (nested / "article.pdf").write_bytes(b"%PDF-test")
            with self.assertRaisesRegex(IngestError, "could not identify"):
                select_compiled_pdf(root)

    def test_batch_ingests_pdf_and_directory_with_blank_metadata(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            loose_pdf = root / "Loose Paper.pdf"
            loose_pdf.write_bytes(b"%PDF-loose")
            source = root / "Source Paper"
            source.mkdir()
            (source / "main.tex").write_text("paper", encoding="utf-8")
            (source / "main.pdf").write_bytes(b"%PDF-source")

            targets = ingest_inputs([loose_pdf, source], root / "papers")

            self.assertEqual(
                [target.name for target in targets],
                ["Loose-Paper", "Source-Paper"],
            )
            self.assertEqual((targets[1] / "paper.pdf").read_bytes(), b"%PDF-source")
            self.assertTrue((targets[1] / "source" / "main.tex").is_file())
            metadata = json.loads((targets[1] / "metadata.json").read_text())
            self.assertEqual(metadata["title"], "")
            self.assertEqual(metadata["authors"], [])

    def test_ingests_zip_and_tar_gz_after_stripping_shared_prefix(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            zip_path = root / "Zipped Paper.zip"
            with zipfile.ZipFile(zip_path, "w") as archive:
                archive.writestr("repository-prefix/main.tex", "paper")
                archive.writestr("repository-prefix/main.pdf", b"%PDF-zip")

            tar_path = root / "Tarred Paper.tar.gz"
            with tarfile.open(tar_path, "w:gz") as archive:
                for name, contents in (
                    ("release/paper/main.tex", b"paper"),
                    ("release/paper/main.pdf", b"%PDF-tar"),
                ):
                    entry = tarfile.TarInfo(name)
                    entry.size = len(contents)
                    archive.addfile(entry, BytesIO(contents))

            targets = ingest_inputs([zip_path, tar_path], root / "papers")

            self.assertEqual(
                [target.name for target in targets],
                ["Zipped-Paper", "Tarred-Paper"],
            )
            self.assertEqual((targets[0] / "paper.pdf").read_bytes(), b"%PDF-zip")
            self.assertTrue((targets[0] / "source" / "main.tex").is_file())
            self.assertFalse((targets[0] / "source" / "repository-prefix").exists())
            self.assertEqual((targets[1] / "paper.pdf").read_bytes(), b"%PDF-tar")
            self.assertFalse((targets[1] / "source" / "release").exists())

    def test_archives_reject_parent_paths(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            zip_path = root / "unsafe.zip"
            with zipfile.ZipFile(zip_path, "w") as archive:
                archive.writestr("../outside.pdf", b"%PDF-test")
            with self.assertRaisesRegex(IngestError, "unsafe archive path"):
                ingest_inputs([zip_path], root / "zip-papers")

            tar_path = root / "unsafe.tar.gz"
            with tarfile.open(tar_path, "w:gz") as archive:
                contents = b"%PDF-test"
                entry = tarfile.TarInfo("prefix/../../outside.pdf")
                entry.size = len(contents)
                archive.addfile(entry, BytesIO(contents))
            with self.assertRaisesRegex(IngestError, "unsafe archive path"):
                ingest_inputs([tar_path], root / "tar-papers")

            self.assertFalse((root / "outside.pdf").exists())


if __name__ == "__main__":
    unittest.main()
