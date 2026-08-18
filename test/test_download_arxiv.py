from contextlib import redirect_stderr, redirect_stdout
from io import BytesIO, StringIO
from pathlib import Path
import json
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
    print_completion_summary,
)


def paper_metadata(arxiv_id: str) -> download_arxiv.PaperMetadata:
    return download_arxiv.PaperMetadata(
        arxiv_id=arxiv_id,
        title="Test Paper",
        authors=("Ada Lovelace", "Alan Turing"),
        published="2024-01-01T00:00:00Z",
        updated="2024-01-02T00:00:00Z",
    )


def metadata_feed(*papers: download_arxiv.PaperMetadata) -> bytes:
    entries = "".join(
        f"""
        <entry>
          <id>https://arxiv.org/abs/{paper.arxiv_id}</id>
          <title>{paper.title}</title>
          <published>{paper.published}</published>
          <updated>{paper.updated}</updated>
          {''.join(f'<author><name>{author}</name></author>' for author in paper.authors)}
        </entry>
        """
        for paper in papers
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
    <feed xmlns="http://www.w3.org/2005/Atom"
          xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/">
      <opensearch:totalResults>{len(papers)}</opensearch:totalResults>
      {entries}
    </feed>
    """.encode()


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

    def test_cli_dry_run_validates_without_network(self):
        output = StringIO()
        with TemporaryDirectory() as temporary, redirect_stdout(output):
            status = download_arxiv.main(
                ["1706.03762", "--output-dir", temporary, "--dry-run"]
            )
        self.assertEqual(status, 0)
        self.assertIn("Would complete arXiv:1706.03762", output.getvalue())
        self.assertIn("arXiv-1706.03762", output.getvalue())
        self.assertIn(
            "Dry-run summary: 1 paper(s) would download or complete; "
            "0 already downloaded and would be skipped.",
            output.getvalue(),
        )

    def test_cli_dry_run_reports_complete_download_would_be_skipped(self):
        output = StringIO()
        with TemporaryDirectory() as temporary:
            paper = Path(temporary) / "arXiv-1706.03762"
            (paper / "source").mkdir(parents=True)
            (paper / "paper.pdf").write_bytes(b"%PDF-test")
            with redirect_stdout(output):
                status = download_arxiv.main(
                    ["1706.03762", "--output-dir", temporary, "--dry-run"]
                )

        self.assertEqual(status, 0)
        self.assertIn(
            "Would skip content download for arXiv:1706.03762: already downloaded",
            output.getvalue(),
        )
        self.assertIn("metadata would be refreshed", output.getvalue())
        self.assertIn(
            "Dry-run summary: 0 paper(s) would download or complete; "
            "1 already downloaded and would be skipped.",
            output.getvalue(),
        )

    def test_cli_dry_run_reports_pdf_only_download_would_be_skipped(self):
        output = StringIO()
        with TemporaryDirectory() as temporary:
            paper = Path(temporary) / "arXiv-1706.03762"
            paper.mkdir()
            (paper / "paper.pdf").write_bytes(b"%PDF-test")
            (paper / "PDF_ONLY").write_text("no source\n", encoding="utf-8")
            with redirect_stdout(output):
                status = download_arxiv.main(
                    ["1706.03762", "--output-dir", temporary, "--dry-run"]
                )

        self.assertEqual(status, 0)
        self.assertIn("Would skip content download", output.getvalue())
        self.assertIn("already identified as PDF-only", output.getvalue())

    def test_cli_dry_run_reports_partial_download_would_be_completed(self):
        output = StringIO()
        with TemporaryDirectory() as temporary:
            paper = Path(temporary) / "arXiv-1706.03762"
            paper.mkdir()
            (paper / "paper.pdf").write_bytes(b"%PDF-test")
            with redirect_stdout(output):
                status = download_arxiv.main(
                    ["1706.03762", "--output-dir", temporary, "--dry-run"]
                )

        self.assertEqual(status, 0)
        self.assertIn("Would complete arXiv:1706.03762", output.getvalue())
        self.assertIn("PDF already exists", output.getvalue())
        self.assertIn("source would be downloaded", output.getvalue())

    def test_cli_dry_run_reports_force_would_replace_existing_content(self):
        output = StringIO()
        with TemporaryDirectory() as temporary:
            paper = Path(temporary) / "arXiv-1706.03762"
            paper.mkdir()
            with redirect_stdout(output):
                status = download_arxiv.main(
                    [
                        "1706.03762",
                        "--output-dir",
                        temporary,
                        "--force",
                        "--dry-run",
                    ]
                )

        self.assertEqual(status, 0)
        self.assertIn("Would re-download arXiv:1706.03762", output.getvalue())
        self.assertIn("--force replaces existing content", output.getvalue())


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


class MetadataTests(unittest.TestCase):
    @patch.object(download_arxiv, "open_arxiv_url")
    def test_fetches_exact_ids_in_batches(self, open_url):
        first = paper_metadata("1706.03762v7")
        second = paper_metadata("2401.12345v2")
        open_url.side_effect = [
            BytesIO(metadata_feed(first)),
            BytesIO(metadata_feed(second)),
        ]

        metadata = download_arxiv.fetch_arxiv_metadata(
            ["1706.03762", second.arxiv_id],
            batch_size=1,
            pacer=RequestPacer(0),
        )

        self.assertEqual(metadata["1706.03762"].arxiv_id, "1706.03762v7")
        self.assertEqual(metadata["1706.03762"].authors, first.authors)
        self.assertEqual(metadata[second.arxiv_id].title, second.title)
        self.assertEqual(open_url.call_count, 2)


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
                download = fetch_paper(
                    "1706.03762",
                    root,
                    metadata=paper_metadata("1706.03762"),
                )

            source_dir = download.source_path
            self.assertIsNotNone(source_dir)
            assert source_dir is not None
            self.assertEqual((source_dir / "main.tex").read_bytes(), contents)
            self.assertFalse(package.exists())
            metadata = json.loads(
                (paper_dir / "metadata.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                metadata["authors"],
                ["Ada Lovelace", "Alan Turing"],
            )
            self.assertEqual(
                metadata["url"],
                "https://arxiv.org/abs/1706.03762",
            )


class VersionFallbackTests(unittest.TestCase):
    @patch.object(download_arxiv, "download_to")
    def test_falls_back_to_previous_version_after_pdf_404(self, download_to):
        def fake_download(url, destination, *, pacer=None):
            if url.endswith("/pdf/2505.07147v2"):
                raise download_arxiv.ArxivHTTPError(404, url, "Not Found")
            if url.endswith("/pdf/2505.07147v1"):
                destination.write_bytes(b"%PDF-test")
                return download_arxiv.DownloadedFile(
                    "application/pdf",
                    "2505.07147v1.pdf",
                )
            if url.endswith("/src/2505.07147v1"):
                with tarfile.open(destination, "w:gz") as archive:
                    contents = b"hello"
                    entry = tarfile.TarInfo("main.tex")
                    entry.size = len(contents)
                    archive.addfile(entry, BytesIO(contents))
                return download_arxiv.DownloadedFile(
                    "application/gzip",
                    "arXiv-2505.07147v1.tar.gz",
                )
            self.fail(f"unexpected URL: {url}")

        download_to.side_effect = fake_download

        with TemporaryDirectory() as temporary, redirect_stdout(StringIO()):
            root = Path(temporary)
            download = fetch_paper(
                "2505.07147v2",
                root,
                pacer=RequestPacer(0),
                save_metadata=False,
            )

            self.assertEqual(download.arxiv_id, "2505.07147v1")
            self.assertEqual(download.requested_id, "2505.07147v2")
            self.assertTrue(download.fell_back)
            self.assertTrue(
                (root / "arXiv-2505.07147v1" / "source" / "main.tex").is_file()
            )
            self.assertFalse((root / "arXiv-2505.07147v2").exists())

    @patch.object(download_arxiv, "download_to")
    def test_source_403_is_a_successful_pdf_only_paper(self, download_to):
        def fake_download(url, destination, *, pacer=None):
            if "/pdf/" in url:
                destination.write_bytes(b"%PDF-test")
                return download_arxiv.DownloadedFile(
                    "application/pdf",
                    "1201.1650v1.pdf",
                )
            raise download_arxiv.ArxivHTTPError(403, url, "Forbidden")

        download_to.side_effect = fake_download

        with TemporaryDirectory() as temporary, redirect_stdout(StringIO()):
            root = Path(temporary)
            download = fetch_paper(
                "1201.1650v1",
                root,
                pacer=RequestPacer(0),
                metadata=paper_metadata("1201.1650v1"),
            )

            self.assertTrue(download.pdf_only)
            self.assertIsNone(download.source_path)
            self.assertTrue(
                (root / "arXiv-1201.1650v1" / "PDF_ONLY").is_file()
            )
            self.assertTrue(
                (root / "arXiv-1201.1650v1" / "metadata.json").is_file()
            )
            download_to.reset_mock()
            second_download = fetch_paper(
                "1201.1650v1",
                root,
                metadata=paper_metadata("1201.1650v1"),
            )
            self.assertTrue(second_download.pdf_only)
            download_to.assert_not_called()


class BatchDownloadTests(unittest.TestCase):
    @patch.object(download_arxiv, "fetch_arxiv_metadata")
    @patch.object(download_arxiv, "write_paper_metadata")
    @patch.object(download_arxiv, "fetch_paper")
    def test_batch_continues_after_bad_id_and_skips_duplicate(
        self,
        fetch_paper,
        write_metadata,
        fetch_metadata,
    ):
        fetch_paper.return_value = download_arxiv.PaperDownload(
            "1706.03762",
            Path("paper.pdf"),
            Path("source"),
            requested_id="1706.03762",
        )

        with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            downloads, failures = fetch_papers(
                ["1706.03762", "not-an-id", "1706.03762"],
                Path("papers"),
                pacer=RequestPacer(0),
                metadata_by_id={
                    "1706.03762": paper_metadata("1706.03762"),
                },
            )

        self.assertEqual([download.arxiv_id for download in downloads], ["1706.03762"])
        self.assertEqual([failure.paper for failure in failures], ["not-an-id"])
        fetch_paper.assert_called_once()
        write_metadata.assert_called_once()
        fetch_metadata.assert_not_called()

    def test_summary_lists_failed_ids_for_retry(self):
        failures = [
            download_arxiv.PaperFailure("1706.03762", "first error"),
            download_arxiv.PaperFailure("2401.12345v2", "second error"),
        ]

        output = StringIO()
        with redirect_stdout(output):
            print_completion_summary([], failures)

        self.assertIn(
            "Failed IDs: 1706.03762 2401.12345v2",
            output.getvalue(),
        )

    def test_summary_counts_pdf_only_papers_and_fallbacks(self):
        downloads = [
            download_arxiv.PaperDownload(
                "1201.1650v1",
                Path("paper.pdf"),
                None,
                requested_id="1201.1650v1",
            ),
            download_arxiv.PaperDownload(
                "2505.07147v1",
                Path("paper.pdf"),
                Path("source"),
                requested_id="2505.07147v2",
            ),
        ]

        output = StringIO()
        with redirect_stdout(output):
            print_completion_summary(downloads, [])

        self.assertIn("PDF-only papers: 1.", output.getvalue())
        self.assertIn("PDF-only IDs: 1201.1650v1", output.getvalue())
        self.assertIn(
            "Version fallback IDs: 2505.07147v2->2505.07147v1",
            output.getvalue(),
        )

    @patch.object(download_arxiv, "fetch_paper")
    def test_failed_url_is_summarized_as_canonical_id(self, fetch_paper):
        fetch_paper.side_effect = download_arxiv.DownloadError("network error")

        with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            _, failures = fetch_papers(
                ["https://arxiv.org/abs/1706.03762v7"],
                Path("papers"),
                pacer=RequestPacer(0),
            )

        self.assertEqual(failures[0].paper, "1706.03762v7")


if __name__ == "__main__":
    unittest.main()
