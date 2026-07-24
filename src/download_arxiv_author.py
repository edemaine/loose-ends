#!/usr/bin/env python3
"""Find and download all arXiv papers matching an author name."""

from __future__ import annotations

import argparse
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlencode

import download_arxiv


API_URL = "https://export.arxiv.org/api/query"
ATOM_NAMESPACE = "http://www.w3.org/2005/Atom"
OPENSEARCH_NAMESPACE = "http://a9.com/-/spec/opensearch/1.1/"
NAMESPACES = {"atom": ATOM_NAMESPACE, "opensearch": OPENSEARCH_NAMESPACE}
DEFAULT_PAGE_SIZE = 100


@dataclass(frozen=True)
class ArxivPaper:
    arxiv_id: str
    title: str
    authors: tuple[str, ...]
    published: str
    updated: str


@dataclass(frozen=True)
class AuthorSearchResult:
    author: str
    total_results: int
    papers: tuple[ArxivPaper, ...]


def normalized_text(value: str | None) -> str:
    return " ".join((value or "").split())


def author_query(author: str) -> str:
    """Build an arXiv author-field phrase query."""
    author = normalized_text(author)
    if not author:
        raise ValueError("author name cannot be empty")
    if '"' in author:
        raise ValueError('author name cannot contain a double quote (")')
    return f'au:"{author}"'


def parse_atom_feed(payload: bytes) -> tuple[int, list[ArxivPaper]]:
    """Parse one page of an arXiv API Atom response."""
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as exc:
        raise download_arxiv.DownloadError(
            f"arXiv returned invalid Atom XML: {exc}"
        ) from exc

    total_element = root.find("opensearch:totalResults", NAMESPACES)
    try:
        total_results = int(total_element.text) if total_element is not None else 0
    except (TypeError, ValueError) as exc:
        raise download_arxiv.DownloadError(
            "arXiv returned an invalid result count"
        ) from exc

    papers = []
    for entry in root.findall("atom:entry", NAMESPACES):
        id_element = entry.find("atom:id", NAMESPACES)
        if id_element is None or not id_element.text:
            raise download_arxiv.DownloadError(
                "arXiv returned an Atom entry without an ID"
            )
        try:
            arxiv_id = download_arxiv.parse_arxiv_id(id_element.text)
        except ValueError as exc:
            title = normalized_text(entry.findtext("atom:title", "", NAMESPACES))
            raise download_arxiv.DownloadError(
                f"arXiv API returned an error entry: {title or id_element.text}"
            ) from exc

        authors = tuple(
            normalized_text(author.findtext("atom:name", "", NAMESPACES))
            for author in entry.findall("atom:author", NAMESPACES)
        )
        papers.append(
            ArxivPaper(
                arxiv_id=arxiv_id,
                title=normalized_text(
                    entry.findtext("atom:title", "", NAMESPACES)
                ),
                authors=tuple(author for author in authors if author),
                published=normalized_text(
                    entry.findtext("atom:published", "", NAMESPACES)
                ),
                updated=normalized_text(
                    entry.findtext("atom:updated", "", NAMESPACES)
                ),
            )
        )

    return total_results, papers


def search_author(
    author: str,
    *,
    limit: int | None = None,
    page_size: int = DEFAULT_PAGE_SIZE,
    pacer: download_arxiv.RequestPacer | None = None,
) -> AuthorSearchResult:
    """Return all (or ``limit``) papers matching an arXiv author query."""
    query = author_query(author)
    author = normalized_text(author)
    if limit is not None and limit < 1:
        raise ValueError("limit must be at least 1")
    if not 1 <= page_size <= 2000:
        raise ValueError("page size must be between 1 and 2000")

    papers: list[ArxivPaper] = []
    seen: set[str] = set()
    total_results = 0
    start = 0

    while limit is None or len(papers) < limit:
        request_size = page_size
        if limit is not None:
            request_size = min(request_size, limit - len(papers))
        parameters = urlencode(
            {
                "search_query": query,
                "start": start,
                "max_results": request_size,
                "sortBy": "submittedDate",
                "sortOrder": "ascending",
            }
        )
        url = f"{API_URL}?{parameters}"
        with download_arxiv.open_arxiv_url(url, pacer=pacer) as response:
            payload = response.read()

        total_results, page = parse_atom_feed(payload)
        if not page:
            break
        for paper in page:
            if paper.arxiv_id not in seen:
                seen.add(paper.arxiv_id)
                papers.append(paper)
                if limit is not None and len(papers) >= limit:
                    break

        start += len(page)
        if start >= total_results:
            break

    return AuthorSearchResult(author, total_results, tuple(papers))


def print_search_results(result: AuthorSearchResult) -> None:
    displayed = len(result.papers)
    if displayed == result.total_results:
        print(f"Found {displayed} paper(s) for {result.author}:")
    else:
        print(
            f"Showing {displayed} of {result.total_results} paper(s) "
            f"for {result.author}:"
        )

    for index, paper in enumerate(result.papers, start=1):
        date = paper.published[:10] or "unknown date"
        print(f"{index:4}. {date}  arXiv:{paper.arxiv_id}")
        print(f"      {paper.title}")
        if paper.authors:
            print(f"      {', '.join(paper.authors)}")


def positive_integer(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if number < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return number


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Find and download arXiv papers matching an author name."
    )
    parser.add_argument("author", help="author name to search for")
    parser.add_argument(
        "--list",
        action="store_true",
        help="list matching papers without downloading them",
    )
    parser.add_argument(
        "--limit",
        type=positive_integer,
        help="use only the first N results (useful for checking a query)",
    )
    parser.add_argument(
        "-o",
        "--output-dir",
        type=Path,
        default=Path.cwd(),
        help="parent directory for paper directories (default: current directory)",
    )
    parser.add_argument(
        "-f",
        "--force",
        action="store_true",
        help="replace files that have already been downloaded",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    pacer = download_arxiv.RequestPacer()

    try:
        result = search_author(args.author, limit=args.limit, pacer=pacer)
    except (ValueError, download_arxiv.DownloadError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print_search_results(result)
    if args.list or not result.papers:
        return 0

    print()
    downloads, failures = download_arxiv.fetch_papers(
        (paper.arxiv_id for paper in result.papers),
        args.output_dir.expanduser(),
        force=args.force,
        pacer=pacer,
    )
    print(
        f"Completed {len(downloads)} paper(s)"
        + (f"; {len(failures)} failed" if failures else "")
        + "."
    )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
