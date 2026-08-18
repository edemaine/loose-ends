from contextlib import redirect_stdout
from io import BytesIO, StringIO
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import download_arxiv
import download_arxiv_author
from download_arxiv_author import (
    author_query,
    parse_atom_feed,
    search_author,
)


def atom_feed(total, entries):
    entry_xml = "".join(
        f"""
        <entry>
          <id>https://arxiv.org/abs/{arxiv_id}</id>
          <title>{title}</title>
          <published>{published}T00:00:00Z</published>
          <updated>{published}T00:00:00Z</updated>
          <author><name>{author}</name></author>
        </entry>
        """
        for arxiv_id, title, published, author in entries
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
    <feed xmlns="http://www.w3.org/2005/Atom"
          xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/">
      <opensearch:totalResults>{total}</opensearch:totalResults>
      {entry_xml}
    </feed>
    """.encode()


class AuthorQueryTests(unittest.TestCase):
    def test_builds_phrase_query_and_normalizes_whitespace(self):
        self.assertEqual(
            author_query("  Adrian   Del Maestro "),
            'au:"Adrian Del Maestro"',
        )

    def test_rejects_empty_name(self):
        with self.assertRaises(ValueError):
            author_query("  ")

    def test_cli_dry_run_does_not_search(self):
        output = StringIO()
        with TemporaryDirectory() as temporary, redirect_stdout(output):
            status = download_arxiv_author.main([
                "Ada Lovelace", "--limit", "5", "--output-dir", temporary,
                "--dry-run",
            ])
        self.assertEqual(status, 0)
        self.assertIn('au:"Ada Lovelace"', output.getvalue())
        self.assertIn("limited to 5 result(s)", output.getvalue())


class AtomFeedTests(unittest.TestCase):
    def test_parses_paper_metadata(self):
        payload = atom_feed(
            1,
            [
                (
                    "1706.03762v7",
                    " Attention Is All You Need ",
                    "2017-06-12",
                    "Ashish Vaswani",
                )
            ],
        )

        total, papers = parse_atom_feed(payload)

        self.assertEqual(total, 1)
        self.assertEqual(papers[0].arxiv_id, "1706.03762v7")
        self.assertEqual(papers[0].title, "Attention Is All You Need")
        self.assertEqual(papers[0].authors, ("Ashish Vaswani",))

    def test_rejects_invalid_xml(self):
        with self.assertRaises(download_arxiv.DownloadError):
            parse_atom_feed(b"<not-finished")


class AuthorSearchTests(unittest.TestCase):
    @patch.object(download_arxiv, "open_arxiv_url")
    def test_pages_until_all_results_are_collected(self, open_url):
        open_url.side_effect = [
            BytesIO(
                atom_feed(
                    3,
                    [
                        ("1706.03762v7", "First", "2017-06-12", "A. Author"),
                        ("1801.00001v1", "Second", "2018-01-01", "A. Author"),
                    ],
                )
            ),
            BytesIO(
                atom_feed(
                    3,
                    [("1901.00001v2", "Third", "2019-01-01", "A. Author")],
                )
            ),
        ]

        result = search_author(
            "A. Author",
            page_size=2,
            pacer=download_arxiv.RequestPacer(0),
        )

        self.assertEqual(result.total_results, 3)
        self.assertEqual(len(result.papers), 3)
        self.assertEqual(open_url.call_count, 2)

    @patch.object(download_arxiv, "open_arxiv_url")
    def test_limit_stops_after_requested_results(self, open_url):
        open_url.return_value = BytesIO(
            atom_feed(
                20,
                [
                    ("1706.03762v7", "First", "2017-06-12", "A. Author"),
                    ("1801.00001v1", "Second", "2018-01-01", "A. Author"),
                ],
            )
        )

        result = search_author("A. Author", limit=2)

        self.assertEqual(len(result.papers), 2)
        self.assertEqual(open_url.call_count, 1)


if __name__ == "__main__":
    unittest.main()
