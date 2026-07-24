# Loose Ends

Tooling for studying open problems in research papers with large language models.

No installation is required; the scripts use only the Python standard library.

## Download arXiv papers

`src/download_arxiv.py` downloads both the rendered PDF and the authors' submitted
source package. It accepts one or many IDs or URLs:

```sh
python src/download_arxiv.py 1706.03762
python src/download_arxiv.py https://arxiv.org/abs/1706.03762v7
python src/download_arxiv.py 1706.03762 2401.12345 hep-th/9901001 --output-dir papers
```

The first example creates:

```text
arXiv-1706.03762/
├── paper.pdf
└── source/
    ├── main.tex
    └── ...
```

Source tarballs and zip files are safely extracted under `source/` and removed
after successful extraction. A non-archive source submission is placed in that
directory as-is. Existing downloads are left untouched; pass `--force` to
replace them. If an earlier run left a source archive beside `paper.pdf`, the
next run extracts and removes it without downloading it again. A batch continues
after an invalid ID or failed download and exits with a failure status after
reporting the final counts.

Both current IDs and pre-2007 IDs are accepted. Because legacy IDs contain a
slash, the slash is replaced with an underscore in the directory name (for
example, `hep-th/9901001` is saved under `arXiv-hep-th_9901001/`). Every
directory starts with `arXiv-` so papers from future downloaders can be
distinguished by source.

Every network request—API, PDF, or source—is started at least three seconds
after the previous request. Already-downloaded files do not cause a delay.

The downloader can also be used as a Python module:

```python
from pathlib import Path

from src import download_arxiv

downloads, failures = download_arxiv.fetch_papers(
    ["1706.03762", "2401.12345"],
    Path("papers"),
)
```

## Download papers by author

Author search uses the arXiv metadata API and passes the resulting IDs to the
same `download_arxiv` module.

Inspect the results before downloading:

```sh
python src/download_arxiv_author.py "Adrian Del Maestro" --list
python src/download_arxiv_author.py "Adrian Del Maestro" --list --limit 10
```

Download all matches, or just the first few:

```sh
python src/download_arxiv_author.py "Adrian Del Maestro" --output-dir papers
python src/download_arxiv_author.py "Adrian Del Maestro" --limit 10 --output-dir papers
```

Name-based author matching is approximate. Common names can refer to multiple
people, and arXiv may match initials or alternate forms of a name. The listing
includes every paper's full author list so the result set can be checked before
downloading.

## Development

Code lives in `src/` and tests live in `test/`. Run:

```sh
python -m unittest discover -s test -v
```

The arXiv metadata API is not needed when the identifier is already known. The
script downloads content directly from arXiv's documented PDF and source links.
Author lookup uses the API's `au:` query field. For repository-scale harvesting,
use arXiv's bulk-data facilities instead.
