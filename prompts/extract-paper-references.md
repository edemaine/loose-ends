# Extract paper references

Extract the complete reference list (bibliography) of one research paper as
structured data. The workspace contains exactly one of:

- `references.txt`: the paper's bibliography, copied from its LaTeX `.bbl`
  file, an embedded `thebibliography` environment, its `.bib` database
  (together with the list of citation keys the paper actually cites), or the
  reference section of the rendered PDF text. The first line of the file says
  which of these it is.
- `paper.pdf`: the rendered paper. Read its reference section directly.

Return one entry per cited work, in the order the paper lists them. Do not
skip entries, do not merge distinct entries, and do not invent entries.

For each entry:

- `key`: the citation label used by the paper, such as the BibTeX key
  (`AHIK03`), the bracketed number (`12`), or an author-year label. Use an
  empty string when there is none.
- `title`: the work's title as plain text. Remove LaTeX commands, braces, and
  math markup; keep the original capitalization; join line breaks with spaces.
- `authors`: every author in order as plain-text names in "First Last" or
  "F. Last" form, exactly as printed. Use an empty array for works without
  authors (standards, software, web pages).
- `year`: the four-digit publication year, or an empty string.
- `venue`: the journal, conference, book, thesis, report series, or publisher
  as printed, without volume, page, or year details. Empty string if none.
- `arxiv_id`: the arXiv identifier when the entry cites arXiv, such as
  `1234.56789`, `1234.56789v2`, or `cs/0410048`. Include the version suffix
  only when printed. Empty string otherwise.
- `doi`: the DOI (`10.xxxx/...`) when printed, without a URL prefix. Empty
  string otherwise.
- `url`: a URL when printed (excluding DOI and arXiv URLs already captured).
  Empty string otherwise.
- `raw`: the whole entry as a single line of plain text, with LaTeX markup
  removed and whitespace collapsed.

When a `.bib` database is supplied, include only entries whose keys appear in
the cited-keys list; when that list is empty, include every entry.
Do not use the paper's own filename or directory name as evidence.
Return only the structured result requested by the output schema.
