from contextlib import redirect_stderr, redirect_stdout
from io import BytesIO, StringIO
from pathlib import Path
import sys
import tarfile
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import download_arxiv
from download_arxiv import (
    RequestPacer,
    directory_name,
    extract_source,
    fetch_paper,
    fetch_papers,
    parse_arxiv_id,
)


class ParseArxivIdTests(unittest.TestCase):
    def test_modern_id(self):
        self.assertEqual(parse_arxiv_id("1706.03762"), "1706.03762")

    def test_versioned_citation(self):
        self.assertEqual(parse_arxiv_id("arXiv:1706.03762v7"), "1706.03762v7")

    def test_abstract_url(self):
        self.assertEqual(
            parse_arxiv_id("https://arxiv.org/abs/1706.03762v7"),
            "1706.03762v7",
        )

    def test_pdf_url(self):
        self.assertEqual(
            parse_arxiv_id("https://www.arxiv.org/pdf/1706.03762.pdf#page=3"),
            "1706.03762",
        )

    def test_html_url(self):
        self.assertEqual(
            parse_arxiv_id("arxiv.org/html/2401.12345v2"),
            "2401.12345v2",
        )

    def test_legacy_id_and_url(self):
        self.assertEqual(parse_arxiv_id("hep-th/9901001v2"), "hep-th/9901001v2")
        self.assertEqual(
            parse_arxiv_id("https://export.arxiv.org/src/hep-th/9901001v2"),
            "hep-th/9901001v2",
        )

    def test_legacy_id_uses_portable_directory_name(self):
        self.assertEqual(
            directory_name("hep-th/9901001v2"),
            "arXiv-hep-th_9901001v2",
        )

    def test_modern_id_directory_is_prefixed(self):
        self.assertEqual(directory_name("1706.03762"), "arXiv-1706.03762")

    def test_invalid_host(self):
        with self.assertRaises(ValueError):
            parse_arxiv_id("https://example.com/abs/1706.03762")

    def test_invalid_month(self):
        with self.assertRaises(ValueError):
            parse_arxiv_id("1713.03762")

    def test_invalid_text(self):
        with self.assertRaises(ValueError):
            parse_arxiv_id("attention is all you need")


class RequestPacerTests(unittest.TestCase):
    def test_waits_between_requests_but_not_before_first(self):
        now = [10.0]
        sleeps = []

        def sleep(seconds):
            sleeps.append(seconds)
            now[0] += seconds

        pacer = RequestPacer(clock=lambda: now[0], sleep=sleep)
        pacer.wait()
        pacer.wait()
        pacer.wait()

        self.assertEqual(sleeps, [3.0, 3.0])

    def test_rejects_negative_interval(self):
        with self.assertRaises(ValueError):
            RequestPacer(-1)


class SourceExtractionTests(unittest.TestCase):
    def test_extracts_tarball_and_removes_it(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = root / "source.tar.gz"
            with tarfile.open(package, "w:gz") as archive:
                contents = b"\\documentclass{article}\n"
                entry = tarfile.TarInfo("paper/main.tex")
                entry.size = len(contents)
                archive.addfile(entry, BytesIO(contents))

            source_dir = extract_source(package, root / "source")

            self.assertEqual(
                (source_dir / "paper" / "main.tex").read_bytes(),
                contents,
            )
            self.assertFalse(package.exists())

    def test_extracts_zip_and_removes_it(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = root / "source.zip"
            with zipfile.ZipFile(package, "w") as archive:
                archive.writestr("main.tex", "hello")

            source_dir = extract_source(package, root / "source")

            self.assertEqual((source_dir / "main.tex").read_text(), "hello")
            self.assertFalse(package.exists())

    def test_moves_plain_source_into_source_directory(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = root / "source.tex"
            package.write_text("hello")

            source_dir = extract_source(package, root / "source")

            self.assertEqual((source_dir / "source.tex").read_text(), "hello")
            self.assertFalse(package.exists())

    def test_rejects_path_traversal_and_keeps_tarball(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = root / "source.tar.gz"
            with tarfile.open(package, "w:gz") as archive:
                contents = b"escape"
                entry = tarfile.TarInfo("../escape.tex")
                entry.size = len(contents)
                archive.addfile(entry, BytesIO(contents))

            with self.assertRaises(download_arxiv.DownloadError):
                extract_source(package, root / "source")

            self.assertTrue(package.exists())
            self.assertFalse((root / "escape.tex").exists())
            self.assertFalse((root / "source").exists())

    def test_existing_tarball_is_migrated_without_a_download(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            paper_dir = root / "arXiv-1706.03762"
            paper_dir.mkdir()
            (paper_dir / "paper.pdf").write_bytes(b"%PDF-test")
            package = paper_dir / "source.tar.gz"
            with tarfile.open(package, "w:gz") as archive:
                contents = b"hello"
                entry = tarfile.TarInfo("main.tex")
                entry.size = len(contents)
                archive.addfile(entry, BytesIO(contents))

            with redirect_stdout(StringIO()):
                _, source_dir = fetch_paper("1706.03762", root)

            self.assertEqual((source_dir / "main.tex").read_bytes(), contents)
            self.assertFalse(package.exists())


class BatchDownloadTests(unittest.TestCase):
    @patch.object(download_arxiv, "fetch_paper")
    def test_batch_continues_after_bad_id_and_skips_duplicate(self, fetch_paper):
        fetch_paper.return_value = (Path("paper.pdf"), Path("source"))

        with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            downloads, failures = fetch_papers(
                ["1706.03762", "not-an-id", "1706.03762"],
                Path("papers"),
                pacer=RequestPacer(0),
            )

        self.assertEqual([download.arxiv_id for download in downloads], ["1706.03762"])
        self.assertEqual([failure.paper for failure in failures], ["not-an-id"])
        fetch_paper.assert_called_once()


if __name__ == "__main__":
    unittest.main()
