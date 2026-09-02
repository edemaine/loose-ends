#!/usr/bin/env python3
"""Convert a LaTeX paper or manuscript into a structured reader document.

The conversion is deterministic: the paper's own text, formulas, figures, and
numbering are preserved, so that later LLM passes only add annotations and
widgets anchored to stable identifiers.  Pandoc handles inline LaTeX; this
module handles document structure (sections, theorem-like environments,
proofs, figures, numbered equations, bibliographies) that pandoc 2.x drops.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import html
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from typing import Sequence


SCHEMA_VERSION = 1
DOCUMENT_HTML = "document.html"
DOCUMENT_JSON = "document.json"
FIGURES_DIRECTORY = "figures"


class DocumentError(RuntimeError):
    """A paper could not be converted into a reader document."""


DEFAULT_THEOREM_ENVIRONMENTS = {
    "theorem": "Theorem",
    "lemma": "Lemma",
    "proposition": "Proposition",
    "corollary": "Corollary",
    "definition": "Definition",
    "remark": "Remark",
    "example": "Example",
    "claim": "Claim",
    "conjecture": "Conjecture",
    "question": "Question",
    "problem": "Problem",
    "openproblem": "Open Problem",
    "fact": "Fact",
    "observation": "Observation",
    "notation": "Notation",
    "assumption": "Assumption",
    "property": "Property",
    "exercise": "Exercise",
}
# Environment kinds that make an "assertion" a reader may want visualized.
STATEMENT_KINDS = {
    "theorem", "lemma", "proposition", "corollary", "claim", "conjecture",
    "fact", "observation", "definition", "question", "problem", "openproblem",
    "property",
}
PROOF_ENVIRONMENT = "proof"
MARKER_RE = re.compile(
    r"^LE(ENVBEGIN|ENVEND|TITLE|FIGURE|BIBITEM|DOCTITLE|AUTHOR)(\d{4})$"
)
CREF_MARK = "LECREFMARK"
GRAPHICS_EXTENSIONS = (".pdf", ".png", ".jpg", ".jpeg", ".svg", ".eps", ".gif")


@dataclass
class TheoremEnvironment:
    name: str
    display: str
    counter: str
    parent: str | None = None  # "section" resets by section
    numbered: bool = True


@dataclass
class FigureSpec:
    index: int
    label: str | None
    caption: str
    graphics: list[str] = field(default_factory=list)
    tikz: list[str] = field(default_factory=list)
    kind: str = "figure"


@dataclass
class EnvironmentSpec:
    index: int
    name: str
    label: str | None
    title: str | None


@dataclass
class Node:
    kind: str
    id: str = ""
    attrs: dict = field(default_factory=dict)
    children: list = field(default_factory=list)


# --------------------------------------------------------------------------
# LaTeX source preparation
# --------------------------------------------------------------------------


def strip_comments(text: str) -> str:
    out = []
    for line in text.split("\n"):
        result = []
        index = 0
        while index < len(line):
            char = line[index]
            if char == "\\":
                result.append(line[index:index + 2])
                index += 2
                continue
            if char == "%":
                break
            result.append(char)
            index += 1
        out.append("".join(result))
    return "\n".join(out)


def find_main_file(source_dir: Path, main_file: str | None = None) -> Path:
    if main_file:
        candidate = source_dir / main_file
        if not candidate.is_file():
            raise DocumentError(f"main LaTeX file not found: {candidate}")
        return candidate
    candidates = sorted(
        path
        for path in source_dir.rglob("*.tex")
        if not any(part.startswith(".") for part in path.relative_to(source_dir).parts)
    )
    with_class = []
    for path in candidates:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if re.search(r"^\s*\\documentclass", text, re.MULTILINE):
            with_class.append(path)
    if not with_class:
        raise DocumentError(f"no LaTeX file with \\documentclass under {source_dir}")
    with_class.sort(key=lambda path: (path.name != "main.tex", len(path.parts), path.name))
    return with_class[0]


def expand_inputs(text: str, base: Path, depth: int = 0) -> str:
    if depth > 8:
        return text

    def replace(match: re.Match) -> str:
        name = match.group(2).strip()
        for candidate in (base / name, base / f"{name}.tex"):
            if candidate.is_file():
                try:
                    body = candidate.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    return ""
                return "\n" + expand_inputs(strip_comments(body), candidate.parent, depth + 1) + "\n"
        return ""

    return re.sub(r"\\(input|include)\{([^}]*)\}", replace, text)


def _balanced_group(text: str, start: int, open_char: str = "{", close_char: str = "}") -> tuple[str, int] | None:
    """Return the content of the group starting at text[start] and the end index."""
    if start >= len(text) or text[start] != open_char:
        return None
    depth = 0
    index = start
    while index < len(text):
        char = text[index]
        if char == "\\":
            index += 2
            continue
        if char == open_char:
            depth += 1
        elif char == close_char:
            depth -= 1
            if depth == 0:
                return text[start + 1:index], index + 1
        index += 1
    return None


def _command_argument(text: str, command: str) -> str | None:
    match = re.search(r"\\" + command + r"\*?\s*(\[[^\]]*\])?\s*\{", text)
    if match is None:
        return None
    group = _balanced_group(text, match.end() - 1)
    return group[0] if group else None


def parse_theorem_environments(preamble: str) -> dict[str, TheoremEnvironment]:
    environments: dict[str, TheoremEnvironment] = {}
    pattern = re.compile(
        r"\\newtheorem(\*?)\{([^}]+)\}(?:\[([^\]]+)\])?\{([^}]+)\}(?:\[([^\]]+)\])?"
    )
    for match in pattern.finditer(preamble):
        starred, name, shared, display, parent = match.groups()
        display = re.sub(r"\\[a-zA-Z]+\s*", "", display).strip("{} ")
        environments[name] = TheoremEnvironment(
            name=name,
            display=display,
            counter=shared or name,
            parent=parent if parent in {"section", "subsection", "chapter"} else None,
            numbered=not starred,
        )
    # Shared counters inherit their parent from the counter's owner.
    for env in environments.values():
        owner = environments.get(env.counter)
        if owner is not None and env.parent is None:
            env.parent = owner.parent
    if not environments:
        for name, display in DEFAULT_THEOREM_ENVIRONMENTS.items():
            environments[name] = TheoremEnvironment(name, display, "theorem")
    else:
        for name, display in DEFAULT_THEOREM_ENVIRONMENTS.items():
            environments.setdefault(name, TheoremEnvironment(name, display, name))
    return environments


def parse_macros(preamble: str) -> dict[str, str]:
    """Collect simple macros so KaTeX can expand them in the browser."""
    macros: dict[str, str] = {}
    pattern = re.compile(r"\\(?:re)?newcommand\*?\s*\{?\\([a-zA-Z]+)\}?\s*(?:\[(\d)\])?\s*(?:\[[^\]]*\])?\s*\{")
    for match in pattern.finditer(preamble):
        group = _balanced_group(preamble, match.end() - 1)
        if group is None:
            continue
        body = group[0]
        if "\\ensuremath" in body:
            body = re.sub(r"\\ensuremath\{(.*)\}", r"\1", body, flags=re.DOTALL)
        macros["\\" + match.group(1)] = body.strip()
    for match in re.finditer(r"\\DeclareMathOperator(\*?)\{\\([a-zA-Z]+)\}\{", preamble):
        group = _balanced_group(preamble, match.end() - 1)
        if group is None:
            continue
        star = "*" if match.group(1) else ""
        macros["\\" + match.group(2)] = "\\operatorname" + star + "{" + group[0] + "}"
    for match in re.finditer(r"\\def\s*\\([a-zA-Z]+)\s*\{", preamble):
        group = _balanced_group(preamble, match.end() - 1)
        if group is not None and "#" not in group[0]:
            macros.setdefault("\\" + match.group(1), group[0].strip())
    return macros


def _clean_author_fragment(text: str) -> str:
    text = re.sub(r"\\(inst|thanks|footnote|fnmsep|orcidID|orcid)\s*\{[^}]*\}", "", text)
    text = re.sub(r"\\(inst|thanks|footnote)\b", "", text)
    text = text.replace("\\\\", " ")
    text = re.sub(r"\$[^$]*\$", "", text)
    text = re.sub(r"\\[a-zA-Z]+\*?", " ", text)
    text = re.sub(r"[{}~]", " ", text)
    text = re.sub(r"\s+", " ", text).strip(" ,;")
    return text


def parse_authors(preamble: str) -> list[str]:
    author = _command_argument(preamble, "author")
    if not author:
        return []
    parts = re.split(r"\\and\b|\\And\b|\\AND\b", author)
    authors = []
    for part in parts:
        for piece in re.split(r",\s*(?=[A-Z])", part) if "\\\\" not in part else [part]:
            cleaned = _clean_author_fragment(piece)
            if cleaned and len(cleaned) < 80:
                authors.append(cleaned)
    return authors


class Preprocessor:
    """Rewrite structural LaTeX into plain-word markers pandoc keeps."""

    def __init__(self, environments: dict[str, TheoremEnvironment]) -> None:
        self.environments = environments
        self.env_specs: list[EnvironmentSpec] = []
        self.figures: list[FigureSpec] = []
        self.bib_files: list[str] = []
        self.bib_items: list[tuple[str, str | None, str]] = []  # key, label, latex
        self.doc_title: str | None = None
        self.doc_authors: list[str] = []

    # -- environments -----------------------------------------------------

    def _env_marker(self, name: str, label: str | None, title: str | None) -> str:
        index = len(self.env_specs)
        self.env_specs.append(EnvironmentSpec(index, name, label, title))
        marker = f"\n\nLEENVBEGIN{index:04d}\n\n"
        if title:
            marker += f"LETITLE{index:04d} {title}\n\n"
        return marker

    def _rewrite_environments(self, body: str) -> str:
        names = set(self.environments) | {PROOF_ENVIRONMENT, "abstract"}
        result = []
        index = 0
        begin_re = re.compile(r"\\begin\{([a-zA-Z*]+)\}")
        while True:
            match = begin_re.search(body, index)
            if match is None:
                result.append(body[index:])
                break
            name = match.group(1)
            if name not in names:
                result.append(body[index:match.end()])
                index = match.end()
                continue
            result.append(body[index:match.start()])
            position = match.end()
            title = None
            while position < len(body) and body[position] in " \t":
                position += 1
            if position < len(body) and body[position] == "[":
                group = _balanced_group(body, position, "[", "]")
                if group is not None:
                    title, position = group
            label = None
            label_match = re.match(r"\s*\\label\{([^}]*)\}", body[position:])
            if label_match:
                label = label_match.group(1).strip()
                position += label_match.end()
            result.append(self._env_marker(name, label, title))
            index = position
        text = "".join(result)

        def end_marker(match: re.Match) -> str:
            return "\n\nLEENVEND0000\n\n"

        text = re.sub(
            r"\\end\{(" + "|".join(re.escape(name) for name in names) + r")\}",
            end_marker,
            text,
        )
        return text

    # -- figures ------------------------------------------------------------

    def _extract_figures(self, body: str) -> str:
        pattern = re.compile(
            r"\\begin\{(figure\*?|wrapfigure|SCfigure|table\*?)\}(?:\[[^\]]*\])?(?:\{[^}]*\}){0,2}"
            r"(.*?)\\end\{\1\}",
            re.DOTALL,
        )

        def replace(match: re.Match) -> str:
            env, content = match.group(1), match.group(2)
            if env.startswith("table"):
                # Keep tables for pandoc, but pull the caption in front.
                caption = _command_argument(content, "caption")
                label = re.search(r"\\label\{([^}]*)\}", content)
                spec = FigureSpec(len(self.figures), label.group(1).strip() if label else None, caption or "", kind="table")
                self.figures.append(spec)
                inner = _strip_caption_and_label(content)
                return f"\n\nLEFIGURE{spec.index:04d}\n\n{inner}\n\n"
            caption = _command_argument(content, "caption") or ""
            label = re.search(r"\\label\{([^}]*)\}", _strip_caption_and_label(content) + " " + caption)
            if label is None:
                label = re.search(r"\\label\{([^}]*)\}", content)
            spec = FigureSpec(len(self.figures), label.group(1).strip() if label else None, caption)
            for graphic in re.finditer(r"\\includegraphics\*?\s*(?:\[[^\]]*\])?\s*\{([^}]*)\}", content):
                spec.graphics.append(graphic.group(1).strip())
            for tikz in re.finditer(r"\\begin\{tikzpicture\}.*?\\end\{tikzpicture\}", content, re.DOTALL):
                spec.tikz.append(tikz.group(0))
            self.figures.append(spec)
            return f"\n\nLEFIGURE{spec.index:04d}\n\n"

        return pattern.sub(replace, body)

    # -- equations ------------------------------------------------------------

    def _rewrite_equations(self, body: str) -> str:
        numbered = {"equation": None, "align": "aligned", "gather": "gathered", "multline": "gathered", "eqnarray": "aligned", "flalign": "aligned", "alignat": "aligned"}
        unnumbered = {f"{name}*": inner for name, inner in numbered.items()} | {"displaymath": None}

        def wrap(name: str, content: str, is_numbered: bool) -> str:
            labels = re.findall(r"\\label\{([^}]*)\}", content)
            content = re.sub(r"\\label\{[^}]*\}", "", content)
            content = re.sub(r"\\(nonumber|notag)\b", "", content)
            inner = numbered.get(name.rstrip("*"))
            if name == "alignat":
                content = re.sub(r"^\{[^}]*\}", "", content.strip())
            if inner:
                content = f"\\begin{{{inner}}}{content}\\end{{{inner}}}"
            tag = ""
            if is_numbered:
                tag = "\\LEeq{" + (labels[0].strip() if labels else "") + "}"
            return f"\n\\[{tag}{content}\\]\n"

        for name in list(unnumbered) + list(numbered):
            pattern = re.compile(
                r"\\begin\{" + re.escape(name) + r"\}(?:\{[^}]*\})?(.*?)\\end\{" + re.escape(name) + r"\}",
                re.DOTALL,
            )
            is_numbered = name in numbered
            body = pattern.sub(lambda match, n=name, k=is_numbered: wrap(n, match.group(1), k), body)
        return body

    # -- bibliography -------------------------------------------------------------

    def _extract_bibliography(self, body: str, source_dir: Path) -> str:
        for match in re.finditer(r"\\bibliography\{([^}]*)\}", body):
            self.bib_files.extend(name.strip() for name in match.group(1).split(","))
        body = re.sub(r"\\bibliography\{[^}]*\}", "", body)
        body = re.sub(r"\\bibliographystyle\{[^}]*\}", "", body)
        thebib = re.search(r"\\begin\{thebibliography\}\{[^}]*\}(.*?)\\end\{thebibliography\}", body, re.DOTALL)
        if thebib:
            self._parse_bibitems(thebib.group(1))
            body = body[:thebib.start()] + body[thebib.end():]
        else:
            for name in self.bib_files:
                for candidate in (source_dir / name, source_dir / f"{name}.bib"):
                    if candidate.is_file():
                        self._parse_bibtex(candidate.read_text(encoding="utf-8", errors="replace"))
                        break
            if not self.bib_items:
                bbl = source_dir / "main.bbl"
                bbls = [bbl] if bbl.is_file() else sorted(source_dir.glob("*.bbl"))
                for path in bbls:
                    text = path.read_text(encoding="utf-8", errors="replace")
                    inner = re.search(r"\\begin\{thebibliography\}\{[^}]*\}(.*?)\\end\{thebibliography\}", text, re.DOTALL)
                    if inner:
                        self._parse_bibitems(strip_comments(inner.group(1)))
                        break
        return body

    def _parse_bibitems(self, text: str) -> None:
        for match in re.finditer(r"\\bibitem\s*(?:\[((?:[^\[\]]|\[[^\]]*\])*)\])?\s*\{([^}]*)\}(.*?)(?=\\bibitem|\Z)", text, re.DOTALL):
            label, key, entry = match.groups()
            entry = re.sub(r"\\newblock\b", " ", entry)
            entry = re.sub(r"\\providecommand\{[^}]*\}\{[^}]*\}", "", entry)
            self.bib_items.append((key.strip(), label.strip() if label else None, entry.strip()))

    def _parse_bibtex(self, text: str) -> None:
        text = strip_comments(text)
        for match in re.finditer(r"@(\w+)\s*\{", text):
            kind = match.group(1).lower()
            if kind in {"comment", "preamble", "string"}:
                continue
            group = _balanced_group(text, match.end() - 1)
            if group is None:
                continue
            content = group[0]
            if "," not in content:
                continue
            key, fields_text = content.split(",", 1)
            key = key.strip()
            if not key:
                continue
            fields = _parse_bibtex_fields(fields_text)
            self.bib_items.append((key, None, _format_bibtex_entry(kind, fields)))

    # -- driver ------------------------------------------------------------------

    def run(self, body: str, preamble: str, source_dir: Path, title: str | None, authors: Sequence[str] | None) -> str:
        body = re.sub(r"\\(maketitle|tableofcontents|listoffigures|listoftables|linenumbers|nolinenumbers|newpage|clearpage|cleardoublepage|frontmatter|mainmatter|backmatter|appendix|qed|qedhere|noindent|centering|raggedright|raggedleft|small|footnotesize|scriptsize|normalsize|large|Large|LARGE|medskip|smallskip|bigskip)\b\*?", " ", body)
        body = re.sub(r"\\(vspace|hspace|vskip|hskip)\*?\s*\{[^}]*\}", " ", body)
        body = re.sub(r"\\(cref|Cref|autoref|nameref|vref)\{([^}]*)\}", lambda m: f"{CREF_MARK}\\ref{{{m.group(2)}}}", body)
        body = self._extract_bibliography(body, source_dir)
        body = self._extract_figures(body)
        body = self._rewrite_equations(body)
        body = self._rewrite_environments(body)
        head = ""
        doc_title = title or _command_argument(preamble + body, "title")
        if doc_title:
            doc_title = re.sub(r"\\(thanks|footnote)\{[^}]*\}", "", doc_title).replace("\\\\", " ")
            head += f"LEDOCTITLE0000 {doc_title}\n\n"
        self.doc_authors = list(authors) if authors else parse_authors(preamble + body)
        for index, author in enumerate(self.doc_authors):
            head += f"LEAUTHOR{index:04d} {author}\n\n"
        tail = ""
        for index, (key, _label, entry) in enumerate(self.bib_items):
            tail += f"\n\nLEBIBITEM{index:04d} {entry}\n\n"
        return head + body + tail


def _strip_caption_and_label(text: str) -> str:
    result = []
    index = 0
    while True:
        match = re.search(r"\\(caption\*?|label)\s*(\[[^\]]*\])?\s*\{", text[index:])
        if match is None:
            result.append(text[index:])
            break
        start = index + match.start()
        result.append(text[index:start])
        group = _balanced_group(text, index + match.end() - 1)
        if group is None:
            index = index + match.end()
            continue
        index = group[1]
    return "".join(result)


def _parse_bibtex_fields(text: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    index = 0
    while index < len(text):
        match = re.compile(r"\s*(\w+)\s*=\s*").match(text, index)
        if match is None:
            break
        name = match.group(1).lower()
        index = match.end()
        if index >= len(text):
            break
        if text[index] == "{":
            group = _balanced_group(text, index)
            if group is None:
                break
            value, index = group
        elif text[index] == '"':
            end = text.find('"', index + 1)
            if end < 0:
                break
            value, index = text[index + 1:end], end + 1
        else:
            end_match = re.compile(r"[^,]*").match(text, index)
            value, index = end_match.group(0).strip(), end_match.end()
        fields[name] = re.sub(r"\s+", " ", value).strip()
        comma = text.find(",", index)
        index = len(text) if comma < 0 else comma + 1
    return fields


def _format_bibtex_entry(kind: str, fields: dict[str, str]) -> str:
    parts = []
    authors = fields.get("author") or fields.get("editor")
    if authors:
        names = [name.strip() for name in re.split(r"\s+and\s+", authors)]
        formatted = []
        for name in names:
            if "," in name:
                last, first = name.split(",", 1)
                formatted.append(f"{first.strip()} {last.strip()}".strip())
            else:
                formatted.append(name)
        parts.append(", ".join(formatted) + ".")
    if fields.get("title"):
        title = fields["title"]
        parts.append(f"\\emph{{{title}}}." if kind in {"book", "phdthesis", "mastersthesis"} else f"{title}.")
    venue = fields.get("journal") or fields.get("booktitle") or fields.get("publisher") or fields.get("school") or fields.get("howpublished") or fields.get("note")
    if venue:
        detail = venue
        if fields.get("volume"):
            detail += f" {fields['volume']}"
        if fields.get("number"):
            detail += f"({fields['number']})"
        if fields.get("pages"):
            detail += f":{fields['pages']}"
        parts.append(f"\\emph{{{detail}}}," if fields.get("journal") else f"{detail},")
    if fields.get("year"):
        parts.append(f"{fields['year']}.")
    for name in ("doi", "url", "eprint"):
        value = fields.get(name)
        if value:
            if name == "doi" and not value.startswith("http"):
                value = f"https://doi.org/{value}"
            elif name == "eprint" and not value.startswith("http"):
                value = f"https://arxiv.org/abs/{value}"
            parts.append(f"\\url{{{value}}}")
            break
    return " ".join(parts)


# --------------------------------------------------------------------------
# Pandoc
# --------------------------------------------------------------------------


def run_pandoc(pandoc: str, latex: str, timeout: float = 300.0) -> dict:
    executable = shutil.which(pandoc)
    if executable is None:
        raise DocumentError(f"pandoc is required to build reader documents but was not found: {pandoc}")
    try:
        completed = subprocess.run(
            [executable, "--from", "latex", "--to", "json"],
            input=latex,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DocumentError(f"pandoc failed: {exc}") from exc
    if completed.returncode != 0:
        raise DocumentError(f"pandoc failed with status {completed.returncode}: {completed.stderr.strip()[:2000]}")
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise DocumentError(f"pandoc produced invalid JSON: {exc}") from exc


# --------------------------------------------------------------------------
# AST to document tree
# --------------------------------------------------------------------------


class Builder:
    def __init__(
        self,
        pre: Preprocessor,
        environments: dict[str, TheoremEnvironment],
        macros: dict[str, str],
        figure_files: dict[int, list[str]],
        warnings: list[str],
    ) -> None:
        self.pre = pre
        self.environments = environments
        self.macros = macros
        self.figure_files = figure_files
        self.warnings = warnings
        self.root = Node("root")
        self.stack: list[Node] = [self.root]
        self.section_counters = [0, 0, 0]
        self.counters: dict[str, int] = {}
        self.equation_counter = 0
        self.figure_counter = 0
        self.table_counter = 0
        self.paragraph_counter = 0
        self.env_counter = 0
        self.proof_counter = 0
        self.labels: dict[str, dict] = {}
        self.sections: list[dict] = []
        self.statements: list[dict] = []
        self.proofs: list[dict] = []
        self.paragraphs: list[dict] = []
        self.figures: list[dict] = []
        self.equations: list[dict] = []
        self.footnotes: list[str] = []
        self.citation_order: list[str] = []
        self.title_inlines: list | None = None
        self.author_inlines: list[list] = []
        self.bib_entries: dict[int, list] = {}
        self.used_ids: set[str] = set()
        self.pending_env_title: dict[int, list] = {}
        self.last_statement: Node | None = None

    # -- helpers ----------------------------------------------------------------

    def _unique_id(self, preferred: str) -> str:
        candidate = preferred
        suffix = 2
        while candidate in self.used_ids:
            candidate = f"{preferred}-{suffix}"
            suffix += 1
        self.used_ids.add(candidate)
        return candidate

    def _container(self) -> Node:
        return self.stack[-1]

    def _container_id(self) -> str:
        for node in reversed(self.stack):
            if node.id:
                return node.id
        return ""

    def _section_number(self) -> str:
        return ".".join(str(value) for value in self.section_counters if value) if self.section_counters[0] else ""

    def _current_section(self) -> str:
        for node in reversed(self.stack):
            if node.kind == "section":
                return node.id
        return ""

    def _new_paragraph(self, inlines: list, kind: str = "paragraph") -> Node:
        self.paragraph_counter += 1
        node = Node(kind, id=f"par-{self.paragraph_counter}", attrs={"inlines": inlines, "container": self._container_id()})
        self.used_ids.add(node.id)
        return node

    # -- block walking ---------------------------------------------------------------

    def build(self, ast: dict) -> Node:
        self._register_span_labels(ast["blocks"])
        self._walk_blocks(ast["blocks"], top_level=True)
        while len(self.stack) > 1:
            self.stack.pop()
        return self.root

    def _register_span_labels(self, blocks: list, item_number: str | None = None) -> None:
        """Record `\\label` anchors (pandoc Spans) so forward references resolve."""
        for block in blocks:
            kind = block.get("t")
            content = block.get("c")
            if kind in {"Para", "Plain", "Header"}:
                inlines = content[2] if kind == "Header" else content
                for inline in _iter_inlines(inlines):
                    if inline.get("t") == "Math" and inline["c"][0]["t"] == "DisplayMath":
                        match = re.match(r"\s*\\LEeq\{([^}]*)\}", inline["c"][1])
                        if match:
                            self.equation_counter += 1
                            label = match.group(1).strip()
                            if label:
                                self.labels[label] = {"kind": "equation", "number": str(self.equation_counter), "id": label, "display": "Equation"}
                    if inline.get("t") == "Span" and inline["c"][0][0]:
                        identifier = inline["c"][0][0]
                        self.labels.setdefault(identifier, {
                            "kind": "item" if item_number else "anchor",
                            "number": item_number or "", "id": identifier, "display": "",
                        })
            elif kind == "BulletList":
                for item in content:
                    self._register_span_labels(item)
            elif kind == "OrderedList":
                start = content[0][0]
                for index, item in enumerate(content[1]):
                    self._register_span_labels(item, str(start + index))
            elif kind == "BlockQuote":
                self._register_span_labels(content)
            elif kind == "Div":
                self._register_span_labels(content[1])

    def _walk_blocks(self, blocks: list, top_level: bool = False) -> list[Node]:
        """Walk blocks in the current container; return nodes for nested use."""
        nested: list[Node] = []
        for block in blocks:
            for node in self._convert_block(block, top_level):
                if top_level:
                    self._container().children.append(node)
                else:
                    nested.append(node)
        return nested

    def _convert_block(self, block: dict, top_level: bool) -> list[Node]:
        kind = block["t"]
        content = block.get("c")
        if kind in {"Para", "Plain"}:
            marker = self._marker(content)
            if marker is not None:
                return self._handle_marker(marker, content, top_level)
            if not content:
                return []
            return [self._new_paragraph(content, "paragraph" if kind == "Para" else "plain")]
        if kind == "Header":
            level, (identifier, classes, _attrs), inlines = content
            if not top_level or level > 3:
                return [Node("heading", attrs={"level": level, "inlines": inlines})]
            self._open_section(level, identifier, "unnumbered" not in classes, inlines)
            return []
        if kind == "BulletList":
            return [Node("list", attrs={"ordered": False, "items": [self._walk_blocks(item) for item in content]})]
        if kind == "OrderedList":
            (start, _style, _delim), items = content
            return [Node("list", attrs={"ordered": True, "start": start, "items": [self._walk_blocks(item) for item in items]})]
        if kind == "DefinitionList":
            items = [
                (term, [self._walk_blocks(definition) for definition in definitions])
                for term, definitions in content
            ]
            return [Node("deflist", attrs={"items": items})]
        if kind == "BlockQuote":
            return [Node("quote", children=self._walk_blocks(content))]
        if kind == "Div":
            (identifier, classes, _attrs), blocks = content
            return [Node("div", attrs={"classes": classes, "identifier": identifier}, children=self._walk_blocks(blocks))]
        if kind == "CodeBlock":
            return [Node("code", attrs={"text": content[1]})]
        if kind == "Table":
            return [self._convert_table(content)]
        if kind == "HorizontalRule":
            return [Node("rule")]
        if kind == "LineBlock":
            return [Node("paragraph", attrs={"inlines": [inline for line in content for inline in (line + [{"t": "LineBreak"}])], "container": self._container_id()})]
        if kind == "RawBlock":
            return []
        if kind == "Null":
            return []
        return []

    def _convert_table(self, content: list) -> Node:
        # pandoc 2.9 (API 1.20) or newer (API 1.22+) table shapes.
        if len(content) == 5:
            caption, alignments, _widths, headers, rows = content
            header_cells = [self._walk_blocks(cell) for cell in headers]
            body_rows = [[self._walk_blocks(cell) for cell in row] for row in rows]
            caption_inlines = caption
        else:
            _attrs, caption, _colspecs, head, bodies, _foot = content
            caption_inlines = []
            if caption and caption[1]:
                caption_inlines = caption[1][0]["c"] if caption[1] and caption[1][0].get("t") in {"Plain", "Para"} else []
            header_cells = []
            if head and head[1]:
                header_cells = [self._walk_blocks(cell[4]) for cell in head[1][0][1]]
            body_rows = []
            for body in bodies:
                for row in body[3]:
                    body_rows.append([self._walk_blocks(cell[4]) for cell in row[1]])
        return Node("table", attrs={"caption": caption_inlines, "headers": header_cells, "rows": body_rows})

    def _marker(self, inlines: list) -> tuple[str, int] | None:
        if not inlines or inlines[0].get("t") != "Str":
            return None
        match = MARKER_RE.match(inlines[0]["c"])
        if match is None:
            return None
        return match.group(1), int(match.group(2))

    def _handle_marker(self, marker: tuple[str, int], inlines: list, top_level: bool) -> list[Node]:
        name, index = marker
        rest = inlines[1:]
        while rest and rest[0].get("t") in {"Space", "SoftBreak"}:
            rest = rest[1:]
        if name == "DOCTITLE":
            self.title_inlines = rest
            return []
        if name == "AUTHOR":
            self.author_inlines.append(rest)
            return []
        if name == "BIBITEM":
            self.bib_entries[index] = rest
            return []
        if name == "TITLE":
            self.pending_env_title[index] = rest
            node = self._find_env_node(index)
            if node is not None:
                node.attrs["title_inlines"] = rest
            return []
        if name == "FIGURE":
            return [self._figure_node(index)]
        if name == "ENVBEGIN":
            spec = self.pre.env_specs[index]
            node = self._open_environment(spec)
            if not top_level:
                # Environments nested inside lists are rare; flatten them.
                self.stack.pop()
                return [node]
            return []
        if name == "ENVEND":
            if top_level and len(self.stack) > 1 and self.stack[-1].kind in {"environment", "proof", "abstract"}:
                closed = self.stack.pop()
                self._finish_environment(closed)
            return []
        return []

    def _find_env_node(self, index: int) -> Node | None:
        for node in reversed(self.stack):
            if node.attrs.get("env_index") == index:
                return node
        return None

    # -- sections and environments ------------------------------------------------------

    def _open_section(self, level: int, identifier: str, numbered: bool, inlines: list) -> None:
        while len(self.stack) > 1 and (self.stack[-1].kind != "section" or self.stack[-1].attrs["level"] >= level):
            closed = self.stack.pop()
            if closed.kind in {"environment", "proof", "abstract"}:
                self._finish_environment(closed)
        number = ""
        if numbered:
            self.section_counters[level - 1] += 1
            for deeper in range(level, 3):
                self.section_counters[deeper] = 0
            if level == 1:
                for env in self.environments.values():
                    if env.parent == "section":
                        self.counters[env.counter] = 0
            number = ".".join(str(value) for value in self.section_counters[:level])
        preferred = identifier or f"sec-{number or len(self.sections) + 1}"
        node_id = self._unique_id(preferred)
        node = Node("section", id=node_id, attrs={"level": level, "number": number, "inlines": inlines, "parent": self._current_section()})
        self.sections.append({"id": node_id, "level": level, "number": number, "title": inlines_to_text(inlines), "parent": node.attrs["parent"]})
        if identifier:
            self.labels[identifier] = {"kind": "section", "number": number, "id": node_id, "display": "Section"}
        self._container().children.append(node)
        self.stack.append(node)

    def _open_environment(self, spec: EnvironmentSpec) -> Node:
        if spec.name == "abstract":
            node = Node("abstract", id=self._unique_id("abstract"), attrs={"env_index": spec.index})
        elif spec.name == PROOF_ENVIRONMENT:
            self.proof_counter += 1
            node = Node("proof", id=self._unique_id(f"proof-{self.proof_counter}"), attrs={"env_index": spec.index, "label": spec.label, "title_latex": spec.title})
            if spec.label:
                self.labels[spec.label] = {"kind": "proof", "number": "", "id": node.id, "display": "Proof"}
        else:
            env = self.environments[spec.name]
            number = ""
            if env.numbered:
                self.counters[env.counter] = self.counters.get(env.counter, 0) + 1
                count = self.counters[env.counter]
                prefix = str(self.section_counters[0]) if env.parent == "section" and self.section_counters[0] else ""
                number = f"{prefix}.{count}" if prefix else str(count)
            self.env_counter += 1
            preferred = spec.label or f"{spec.name}-{self.env_counter}"
            node = Node("environment", id=self._unique_id(preferred), attrs={
                "env_index": spec.index, "name": spec.name, "display": env.display,
                "number": number, "label": spec.label, "title_latex": spec.title,
                "section": self._current_section(),
            })
            if spec.label:
                self.labels[spec.label] = {"kind": spec.name, "number": number, "id": node.id, "display": env.display}
        self._container().children.append(node)
        self.stack.append(node)
        return node

    def _finish_environment(self, node: Node) -> None:
        if node.kind == "environment":
            self.last_statement = node

    # -- figures ------------------------------------------------------------------------

    def _figure_node(self, index: int) -> Node:
        spec = self.pre.figures[index]
        if spec.kind == "table":
            self.table_counter += 1
            number = str(self.table_counter)
            display = "Table"
        else:
            self.figure_counter += 1
            number = str(self.figure_counter)
            display = "Figure"
        preferred = spec.label or f"{spec.kind}-{number}"
        node_id = self._unique_id(preferred)
        if spec.label:
            self.labels[spec.label] = {"kind": spec.kind, "number": number, "id": node_id, "display": display}
        node = Node("figure", id=node_id, attrs={
            "figure_index": index, "number": number, "display": display,
            "files": self.figure_files.get(index, []), "table": spec.kind == "table",
        })
        return node


# --------------------------------------------------------------------------
# Inline rendering
# --------------------------------------------------------------------------


def inlines_to_text(inlines: list, labels: dict | None = None) -> str:
    parts: list[str] = []
    for inline in inlines:
        kind = inline.get("t")
        content = inline.get("c")
        if kind == "Str":
            parts.append(content)
        elif kind in {"Space", "SoftBreak"}:
            parts.append(" ")
        elif kind == "LineBreak":
            parts.append("\n")
        elif kind in {"Emph", "Strong", "Strikeout", "Superscript", "Subscript", "SmallCaps", "Underline"}:
            parts.append(inlines_to_text(content, labels))
        elif kind == "Quoted":
            parts.append('"' + inlines_to_text(content[1], labels) + '"')
        elif kind == "Cite":
            parts.append("[" + ", ".join(c["citationId"] for c in content[0]) + "]")
        elif kind == "Code":
            parts.append(content[1])
        elif kind == "Math":
            latex = content[1]
            parts.append(f"${latex}$" if content[0]["t"] == "InlineMath" else f"$${latex}$$")
        elif kind == "Link":
            attrs = dict(content[0][2])
            reference = attrs.get("reference")
            if reference:
                info = (labels or {}).get(reference)
                if info is None:
                    parts.append(f"[{reference}]")
                elif attrs.get("reference-type") == "eqref":
                    parts.append(f"({info.get('number') or reference})")
                else:
                    parts.append(info.get("number") or info.get("display") or reference)
            else:
                parts.append(inlines_to_text(content[1], labels))
        elif kind == "Image":
            parts.append(inlines_to_text(content[1]))
        elif kind == "Note":
            parts.append(" (" + " ".join(blocks_to_text(content)) + ")")
        elif kind == "Span":
            parts.append(inlines_to_text(content[1], labels))
        elif kind == "RawInline":
            pass
    text = "".join(parts)
    text = text.replace(CREF_MARK, "").replace("\xa0", " ")
    return re.sub(r"[ \t]+", " ", text).strip()


def blocks_to_text(blocks: list) -> list[str]:
    result = []
    for block in blocks:
        kind = block.get("t")
        content = block.get("c")
        if kind in {"Para", "Plain"}:
            result.append(inlines_to_text(content))
        elif kind in {"BulletList"}:
            for item in content:
                result.extend(blocks_to_text(item))
        elif kind == "OrderedList":
            for item in content[1]:
                result.extend(blocks_to_text(item))
        elif kind in {"BlockQuote", "Div"}:
            result.extend(blocks_to_text(content if kind == "BlockQuote" else content[1]))
    return result


class Renderer:
    def __init__(self, builder: Builder, pre: Preprocessor) -> None:
        self.builder = builder
        self.pre = pre
        self.labels = builder.labels
        self.cite_numbers: dict[str, int] = {}
        self.bib_keys = [key for key, _label, _entry in pre.bib_items]
        self.footnotes: list[str] = []
        self.equation_counter = 0
        self.statement_paragraphs: dict[str, list[str]] = {}

    # -- inlines ---------------------------------------------------------------

    def inlines(self, inlines: list) -> str:
        out: list[str] = []
        cref_pending = False
        for inline in inlines:
            kind = inline.get("t")
            content = inline.get("c")
            if kind == "Str":
                if content == CREF_MARK:
                    cref_pending = True
                    continue
                if content.startswith(CREF_MARK):
                    cref_pending = True
                    content = content[len(CREF_MARK):]
                out.append(html.escape(content))
            elif kind == "Space":
                out.append(" ")
            elif kind == "SoftBreak":
                out.append("\n")
            elif kind == "LineBreak":
                out.append("<br>")
            elif kind == "Emph":
                out.append(f"<em>{self.inlines(content)}</em>")
            elif kind == "Strong":
                out.append(f"<strong>{self.inlines(content)}</strong>")
            elif kind == "Strikeout":
                out.append(f"<s>{self.inlines(content)}</s>")
            elif kind == "Superscript":
                out.append(f"<sup>{self.inlines(content)}</sup>")
            elif kind == "Subscript":
                out.append(f"<sub>{self.inlines(content)}</sub>")
            elif kind == "SmallCaps":
                out.append(f'<span class="smallcaps">{self.inlines(content)}</span>')
            elif kind == "Underline":
                out.append(f"<u>{self.inlines(content)}</u>")
            elif kind == "Quoted":
                quote = ("‘", "’") if content[0]["t"] == "SingleQuote" else ("“", "”")
                out.append(quote[0] + self.inlines(content[1]) + quote[1])
            elif kind == "Cite":
                out.append(self._cite(content[0]))
            elif kind == "Code":
                out.append(f"<code>{html.escape(content[1])}</code>")
            elif kind == "Math":
                out.append(self._math(content[0]["t"] == "DisplayMath", content[1]))
            elif kind == "Link":
                out.append(self._link(content, cref_pending))
                cref_pending = False
            elif kind == "Image":
                out.append(f'<span class="inline-image">{self.inlines(content[1])}</span>')
            elif kind == "Note":
                self.footnotes.append(self.blocks(content, inline_only=True))
                number = len(self.footnotes)
                out.append(f'<sup class="footnote-ref"><a href="#footnote-{number}" id="footnote-ref-{number}">{number}</a></sup>')
            elif kind == "Span":
                (identifier, classes, _attrs), spans = content
                if identifier:
                    self.labels.setdefault(identifier, {"kind": "anchor", "number": "", "id": identifier, "display": ""})
                    out.append(f'<span class="anchor" id="{html.escape(identifier)}"></span>')
                else:
                    out.append(self.inlines(spans))
            elif kind == "RawInline":
                pass
        return "".join(out)

    def _math(self, display: bool, latex: str) -> str:
        latex = latex.strip()
        if display:
            number = ""
            label = None
            match = re.match(r"\\LEeq\{([^}]*)\}", latex)
            if match:
                self.equation_counter += 1
                number = str(self.equation_counter)
                label = match.group(1).strip() or None
                latex = latex[match.end():].strip()
            attributes = ""
            if label:
                self.labels[label] = {"kind": "equation", "number": number, "id": label, "display": "Equation"}
                self.builder.equations.append({"id": label, "number": number, "latex": latex})
                attributes += f' id="{html.escape(label)}"'
            elif number:
                self.builder.equations.append({"id": "", "number": number, "latex": latex})
            if number:
                attributes += f' data-number="{number}"'
            return f'<div class="math-block"{attributes}><span class="math" data-display="1">{html.escape(latex)}</span></div>'
        return f'<span class="math" data-display="0">{html.escape(latex)}</span>'

    def _link(self, content: list, cref: bool) -> str:
        (_identifier, _classes, attrs), inlines, (target, _title) = content
        attributes = dict(attrs)
        reference = attributes.get("reference")
        if reference is None and target.startswith("#"):
            reference = target[1:]
        if reference is not None:
            reference_type = attributes.get("reference-type", "ref")
            info = self.labels.get(reference)
            if info is None:
                return f'<span class="ref ref-missing" title="unresolved reference">[{html.escape(reference)}]</span>'
            number = info.get("number") or ""
            if reference_type == "eqref":
                text = f"({number})"
            elif cref and info.get("display"):
                text = f"{info['display']} {number}".strip()
            else:
                text = number or info.get("display") or reference
            return f'<a class="ref" href="#{html.escape(info["id"])}" data-ref-kind="{html.escape(info.get("kind") or "")}">{html.escape(text)}</a>'
        text = self.inlines(inlines) or html.escape(target)
        return f'<a class="external" href="{html.escape(target)}" rel="noopener noreferrer" target="_blank">{text}</a>'

    def _cite(self, citations: list) -> str:
        pieces = []
        for citation in citations:
            key = citation["citationId"]
            if key not in self.cite_numbers:
                self.cite_numbers[key] = len(self.cite_numbers) + 1
            number = self.cite_numbers[key]
            if key in self.bib_keys:
                pieces.append(f'<a href="#bib-{html.escape(key)}" class="cite-link">{number}</a>')
            else:
                pieces.append(f'<span class="cite-missing" title="{html.escape(key)}">{number}</span>')
        return '<span class="cite">[' + ", ".join(pieces) + "]</span>"

    # -- blocks -----------------------------------------------------------------

    def blocks(self, nodes: list[Node], inline_only: bool = False) -> str:
        return "".join(self.block(node) for node in nodes)

    def block(self, node: Node) -> str:
        kind = node.kind
        attrs = node.attrs
        if kind in {"paragraph", "plain"}:
            text = self.inlines(attrs["inlines"])
            plain = inlines_to_text(attrs["inlines"], self.labels)
            if node.id:
                self.builder.paragraphs.append({"id": node.id, "container": attrs.get("container", ""), "text": plain})
            if kind == "plain" and not node.id:
                return text
            identifier = f' id="{node.id}"' if node.id else ""
            tag = "div" if any(inline.get("t") == "Math" and inline["c"][0]["t"] == "DisplayMath" for inline in _iter_inlines(attrs["inlines"])) else "p"
            return f'<{tag} class="par"{identifier}>{text}</{tag}>\n'
        if kind == "section":
            return self._section(node)
        if kind == "environment":
            return self._environment(node)
        if kind == "proof":
            return self._proof(node)
        if kind == "abstract":
            return f'<section class="abstract" id="{node.id}"><h2 class="abstract-title">Abstract</h2>{self.blocks(node.children)}</section>\n'
        if kind == "heading":
            return f'<h{min(attrs["level"] + 1, 6)} class="run-in">{self.inlines(attrs["inlines"])}</h{min(attrs["level"] + 1, 6)}>\n'
        if kind == "list":
            tag = "ol" if attrs["ordered"] else "ul"
            start = f' start="{attrs["start"]}"' if attrs.get("ordered") and attrs.get("start", 1) != 1 else ""
            items = "".join(f"<li>{self.blocks(item)}</li>" for item in attrs["items"])
            return f"<{tag}{start}>{items}</{tag}>\n"
        if kind == "deflist":
            items = "".join(
                f"<dt>{self.inlines(term)}</dt>" + "".join(f"<dd>{self.blocks(definition)}</dd>" for definition in definitions)
                for term, definitions in attrs["items"]
            )
            return f"<dl>{items}</dl>\n"
        if kind == "quote":
            return f"<blockquote>{self.blocks(node.children)}</blockquote>\n"
        if kind == "div":
            classes = " ".join(attrs.get("classes") or [])
            return f'<div class="{html.escape(classes)}">{self.blocks(node.children)}</div>\n'
        if kind == "code":
            return f"<pre><code>{html.escape(attrs['text'])}</code></pre>\n"
        if kind == "table":
            return self._table(node)
        if kind == "rule":
            return "<hr>\n"
        if kind == "figure":
            return self._figure(node)
        return ""

    def _section(self, node: Node) -> str:
        attrs = node.attrs
        level = attrs["level"]
        number = attrs["number"]
        heading_tag = f"h{min(level + 1, 6)}"
        number_html = f'<span class="secnum">{number}</span> ' if number else ""
        return (
            f'<section class="sec sec-level-{level}" id="{html.escape(node.id)}" data-number="{number}">\n'
            f'<{heading_tag} class="sec-title">{number_html}{self.inlines(attrs["inlines"])}</{heading_tag}>\n'
            f'<div class="sec-body">\n{self.blocks(node.children)}</div>\n</section>\n'
        )

    def _environment(self, node: Node) -> str:
        attrs = node.attrs
        display = attrs["display"]
        number = attrs["number"]
        head = f"{display} {number}".strip()
        title_html = ""
        title_text = ""
        if attrs.get("title_inlines") is not None:
            title_html = f' <span class="env-title">({self.inlines(attrs["title_inlines"])})</span>'
            title_text = inlines_to_text(attrs["title_inlines"])
        before = len(self.builder.paragraphs)
        body = self.blocks(node.children)
        paragraph_ids = [item["id"] for item in self.builder.paragraphs[before:]]
        text = " ".join(item["text"] for item in self.builder.paragraphs[before:])
        record = {
            "id": node.id, "kind": attrs["name"], "display": display, "number": number,
            "label": head, "title": title_text, "section": attrs.get("section", ""),
            "paragraphs": paragraph_ids, "text": text, "proofs": [],
            "statement": attrs["name"] in STATEMENT_KINDS,
        }
        self.builder.statements.append(record)
        return (
            f'<div class="env env-{html.escape(attrs["name"])}" id="{html.escape(node.id)}" data-kind="{html.escape(attrs["name"])}" data-number="{number}">\n'
            f'<div class="env-head"><span class="env-label">{html.escape(head)}</span>{title_html}</div>\n'
            f'<div class="env-body">\n{body}</div>\n</div>\n'
        )

    def _proof(self, node: Node) -> str:
        attrs = node.attrs
        title_html = "Proof."
        title_text = "Proof"
        of = None
        if attrs.get("title_inlines") is not None:
            title_html = self.inlines(attrs["title_inlines"]) + "."
            title_text = inlines_to_text(attrs["title_inlines"], self.labels)
            for inline in _iter_inlines(attrs["title_inlines"]):
                if inline.get("t") == "Link":
                    reference = dict(inline["c"][0][2]).get("reference")
                    if reference and reference in self.labels and self.labels[reference]["kind"] not in {"section", "equation"}:
                        of = self.labels[reference]["id"]
                        break
        if of is None:
            for record in reversed(self.builder.statements):
                if record["statement"] and not record["proofs"]:
                    of = record["id"]
                    break
            if of is None and self.builder.statements:
                of = self.builder.statements[-1]["id"]
        before = len(self.builder.paragraphs)
        body = self.blocks(node.children)
        paragraph_ids = [item["id"] for item in self.builder.paragraphs[before:]]
        record = {"id": node.id, "title": title_text, "of": of, "paragraphs": paragraph_ids, "section": self.builder._current_section()}
        self.builder.proofs.append(record)
        for statement in self.builder.statements:
            if statement["id"] == of:
                statement["proofs"].append(node.id)
        return (
            f'<div class="proof" id="{html.escape(node.id)}" data-of="{html.escape(of or "")}">\n'
            f'<div class="proof-head"><span class="proof-label">{title_html}</span></div>\n'
            f'<div class="proof-body">\n{body}<span class="qed">∎</span></div>\n</div>\n'
        )

    def _figure(self, node: Node) -> str:
        attrs = node.attrs
        spec = self.pre.figures[attrs["figure_index"]]
        caption_html = self.inlines(self._pandoc_inlines_cache(spec.caption)) if spec.caption else ""
        caption_text = inlines_to_text(self._pandoc_inlines_cache(spec.caption), self.labels) if spec.caption else ""
        label = f"{attrs['display']} {attrs['number']}"
        record = {"id": node.id, "kind": spec.kind, "number": attrs["number"], "label": label, "caption": caption_text, "files": attrs["files"]}
        self.builder.figures.append(record)
        if attrs["table"]:
            return f'<div class="table-caption" id="{html.escape(node.id)}"><span class="fig-label">{html.escape(label)}.</span> {caption_html}</div>\n'
        images = "".join(
            f'<img src="{html.escape(file)}" alt="{html.escape(caption_text[:120])}" loading="lazy">'
            for file in attrs["files"]
        )
        if not images:
            images = '<div class="figure-missing">Figure could not be rendered.</div>'
        return (
            f'<figure class="figure" id="{html.escape(node.id)}" data-number="{attrs["number"]}">\n'
            f'<div class="figure-images">{images}</div>\n'
            f'<figcaption><span class="fig-label">{html.escape(label)}.</span> {caption_html}</figcaption>\n</figure>\n'
        )

    def _table(self, node: Node) -> str:
        attrs = node.attrs
        head = ""
        if attrs["headers"] and any(cell for cell in attrs["headers"]):
            head = "<thead><tr>" + "".join(f"<th>{self.blocks(cell)}</th>" for cell in attrs["headers"]) + "</tr></thead>"
        rows = "".join("<tr>" + "".join(f"<td>{self.blocks(cell)}</td>" for cell in row) + "</tr>" for row in attrs["rows"])
        caption = f"<caption>{self.inlines(attrs['caption'])}</caption>" if attrs["caption"] else ""
        return f'<div class="table-wrap"><table>{caption}{head}<tbody>{rows}</tbody></table></div>\n'

    _inline_cache: dict[str, list] = {}

    def _pandoc_inlines_cache(self, latex: str) -> list:
        return self._inline_cache.get(latex, [{"t": "Str", "c": latex}])


def _iter_inlines(inlines: list):
    for inline in inlines:
        yield inline
        content = inline.get("c")
        if inline.get("t") in {"Emph", "Strong", "Span", "Link"}:
            nested = content[1] if inline.get("t") in {"Span", "Link"} else content
            if isinstance(nested, list):
                yield from _iter_inlines(nested)


# --------------------------------------------------------------------------
# Figures
# --------------------------------------------------------------------------


def _find_graphic(source_dir: Path, name: str, graphics_paths: Sequence[str]) -> Path | None:
    bases = [source_dir] + [source_dir / path for path in graphics_paths]
    for base in bases:
        for candidate in [base / name] + [base / f"{name}{ext}" for ext in GRAPHICS_EXTENSIONS]:
            if candidate.is_file():
                return candidate
    return None


def _convert_graphic(path: Path, destination: Path, pdftocairo: str | None, warnings: list[str]) -> str | None:
    suffix = path.suffix.lower()
    if suffix in {".png", ".jpg", ".jpeg", ".gif", ".svg"}:
        target = destination.with_suffix(suffix)
        shutil.copyfile(path, target)
        return target.name
    if suffix == ".eps":
        epstopdf = shutil.which("epstopdf")
        if epstopdf is None:
            warnings.append(f"cannot convert EPS figure without epstopdf: {path.name}")
            return None
        pdf = destination.with_suffix(".pdf")
        try:
            subprocess.run([epstopdf, str(path), f"--outfile={pdf}"], check=True, capture_output=True, timeout=120)
        except (OSError, subprocess.SubprocessError) as exc:
            warnings.append(f"epstopdf failed for {path.name}: {exc}")
            return None
        path = pdf
        suffix = ".pdf"
    if suffix == ".pdf":
        if pdftocairo is None:
            warnings.append(f"cannot convert PDF figure without pdftocairo: {path.name}")
            return None
        target = destination.with_suffix(".svg")
        try:
            subprocess.run(
                [pdftocairo, "-svg", "-f", "1", "-l", "1", str(path), str(target)],
                check=True, capture_output=True, timeout=120,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            warnings.append(f"pdftocairo failed for {path.name}: {exc}")
            return None
        return target.name if target.is_file() else None
    warnings.append(f"unsupported figure format: {path.name}")
    return None


def _figure_preamble(preamble: str) -> str:
    kept = []
    for line in preamble.split("\n"):
        stripped = line.strip()
        if not stripped or stripped.startswith("\\documentclass"):
            continue
        if re.match(r"\\(title|author|date|maketitle|linenumbers|pagestyle|thispagestyle|setcounter|newtheorem|theoremstyle|bibliographystyle|numberwithin|linespread|setlength|addtolength|usepackage(\[[^\]]*\])?\{(geometry|lineno|hyperref|fullpage|microtype|llncs|times|multicol|fancyhdr|showkeys|cite|natbib|biblatex|authblk|titlesec|todonotes|refcheck|lipsum)\})", stripped):
            continue
        kept.append(line)
    return "\n".join(kept)


def _compile_tikz(tikz: str, preamble: str, destination: Path, pdflatex: str, pdftocairo: str | None, warnings: list[str], timeout: float) -> str | None:
    if pdftocairo is None:
        warnings.append("cannot render TikZ figures without pdftocairo")
        return None
    with tempfile.TemporaryDirectory(prefix="le-figure-") as temporary:
        workspace = Path(temporary)
        source = (
            "\\documentclass[11pt]{article}\n"
            + _figure_preamble(preamble)
            + "\n\\usepackage{tikz}\n\\usepackage[active,tightpage]{preview}\n"
            "\\PreviewEnvironment{tikzpicture}\n\\setlength{\\PreviewBorder}{4pt}\n"
            "\\pagestyle{empty}\n\\begin{document}\n" + tikz + "\n\\end{document}\n"
        )
        (workspace / "figure.tex").write_text(source, encoding="utf-8")
        try:
            completed = subprocess.run(
                [pdflatex, "-interaction=nonstopmode", "-halt-on-error", "figure.tex"],
                cwd=workspace, capture_output=True, text=True, timeout=timeout, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            warnings.append(f"pdflatex failed for a TikZ figure: {exc}")
            return None
        pdf = workspace / "figure.pdf"
        if completed.returncode != 0 or not pdf.is_file():
            log = (workspace / "figure.log")
            detail = ""
            if log.is_file():
                lines = [line for line in log.read_text(encoding="utf-8", errors="replace").splitlines() if line.startswith("!")]
                detail = "; ".join(lines[:3])
            warnings.append(f"TikZ figure did not compile: {detail or 'see pdflatex output'}")
            return None
        return _convert_graphic(pdf, destination, pdftocairo, warnings)


def render_figures(
    figures: list[FigureSpec],
    source_dir: Path,
    preamble: str,
    output_dir: Path,
    warnings: list[str],
    *,
    pdflatex: str = "pdflatex",
    pdftocairo: str = "pdftocairo",
    figure_timeout: float = 180.0,
) -> dict[int, list[str]]:
    figures_dir = output_dir / FIGURES_DIRECTORY
    if figures_dir.exists():
        shutil.rmtree(figures_dir)
    figures_dir.mkdir(parents=True)
    pdftocairo_path = shutil.which(pdftocairo)
    pdflatex_path = shutil.which(pdflatex)
    graphics_paths = []
    for match in re.finditer(r"\\graphicspath\{((?:\{[^}]*\})+)\}", preamble):
        graphics_paths.extend(re.findall(r"\{([^}]*)\}", match.group(1)))
    files: dict[int, list[str]] = {}
    for spec in figures:
        if spec.kind == "table":
            continue
        produced: list[str] = []
        counter = 0
        for name in spec.graphics:
            counter += 1
            path = _find_graphic(source_dir, name, graphics_paths)
            destination = figures_dir / f"fig-{spec.index + 1}-{counter}"
            if path is None:
                warnings.append(f"figure graphic not found: {name}")
                continue
            file = _convert_graphic(path, destination, pdftocairo_path, warnings)
            if file:
                produced.append(f"{FIGURES_DIRECTORY}/{file}")
        for tikz in spec.tikz:
            counter += 1
            destination = figures_dir / f"fig-{spec.index + 1}-{counter}"
            if pdflatex_path is None:
                warnings.append("cannot render TikZ figures without pdflatex")
                continue
            file = _compile_tikz(tikz, preamble, destination, pdflatex_path, pdftocairo_path, warnings, figure_timeout)
            if file:
                produced.append(f"{FIGURES_DIRECTORY}/{file}")
        files[spec.index] = produced
    return files


# --------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------


def source_digest(source_dir: Path, main: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(p for p in source_dir.rglob("*") if p.is_file() and p.suffix.lower() in {".tex", ".bib", ".bbl", ".sty", ".cls"} | set(GRAPHICS_EXTENSIONS)):
        digest.update(path.relative_to(source_dir).as_posix().encode("utf-8"))
        digest.update(b"\0")
        try:
            digest.update(path.read_bytes())
        except OSError:
            digest.update(b"<unreadable>")
        digest.update(b"\0")
    digest.update(main.name.encode("utf-8"))
    return digest.hexdigest()


def build_document(
    source_dir: Path,
    output_dir: Path,
    *,
    main_file: str | None = None,
    title: str | None = None,
    authors: Sequence[str] | None = None,
    source_kind: str = "latex",
    source_path: str | None = None,
    pandoc: str = "pandoc",
    pdflatex: str = "pdflatex",
    pdftocairo: str = "pdftocairo",
    figure_timeout: float = 180.0,
    render_figures_enabled: bool = True,
) -> dict:
    """Convert the LaTeX source in `source_dir` into `output_dir`.

    Writes `document.html`, `document.json`, and `figures/`, and returns the
    parsed document record.
    """
    source_dir = source_dir.resolve()
    main = find_main_file(source_dir, main_file)
    try:
        raw = main.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise DocumentError(f"could not read {main}: {exc}") from exc
    text = expand_inputs(strip_comments(raw), main.parent)
    split = re.search(r"\\begin\{document\}", text)
    if split is None:
        raise DocumentError(f"{main.name} has no \\begin{{document}}")
    preamble, body = text[:split.start()], text[split.end():]
    end = re.search(r"\\end\{document\}", body)
    if end:
        body = body[:end.start()]
    environments = parse_theorem_environments(preamble)
    macros = parse_macros(preamble)
    warnings: list[str] = []
    pre = Preprocessor(environments)
    prepared = pre.run(body, preamble, main.parent, title, authors)
    # Figure captions are converted in the same pandoc pass as marker paragraphs.
    caption_markers = ""
    for spec in pre.figures:
        if spec.caption:
            caption_markers += f"\n\nLECAPTION{spec.index:04d} {spec.caption}\n\n"
    ast = run_pandoc(pandoc, prepared + caption_markers)
    caption_inlines: dict[str, list] = {}
    remaining_blocks = []
    for block in ast["blocks"]:
        if block["t"] in {"Para", "Plain"} and block["c"] and block["c"][0].get("t") == "Str":
            match = re.match(r"^LECAPTION(\d{4})$", block["c"][0]["c"])
            if match:
                rest = block["c"][1:]
                while rest and rest[0].get("t") in {"Space", "SoftBreak"}:
                    rest = rest[1:]
                caption_inlines[pre.figures[int(match.group(1))].caption] = rest
                continue
        remaining_blocks.append(block)
    ast["blocks"] = remaining_blocks
    output_dir.mkdir(parents=True, exist_ok=True)
    figure_files = (
        render_figures(pre.figures, main.parent, preamble, output_dir, warnings, pdflatex=pdflatex, pdftocairo=pdftocairo, figure_timeout=figure_timeout)
        if render_figures_enabled
        else {}
    )
    builder = Builder(pre, environments, macros, figure_files, warnings)
    root = builder.build(ast)
    renderer = Renderer(builder, pre)
    renderer._inline_cache = caption_inlines
    body_html = renderer.blocks(root.children)
    title_html = renderer.inlines(builder.title_inlines) if builder.title_inlines else html.escape(title or main.stem)
    title_text = inlines_to_text(builder.title_inlines) if builder.title_inlines else (title or main.stem)
    author_names = [inlines_to_text(inlines) for inlines in builder.author_inlines] or list(authors or pre.doc_authors)
    authors_html = ", ".join(html.escape(name) for name in author_names)
    bibliography_html = ""
    bibliography: list[dict] = []
    if pre.bib_items:
        entries = []
        ordered = sorted(
            range(len(pre.bib_items)),
            key=lambda index: renderer.cite_numbers.get(pre.bib_items[index][0], len(renderer.cite_numbers) + index + 1),
        )
        for index in ordered:
            key, _label, entry_latex = pre.bib_items[index]
            inlines = builder.bib_entries.get(index, [{"t": "Str", "c": entry_latex}])
            number = renderer.cite_numbers.get(key)
            if number is None:
                number = len(renderer.cite_numbers) + 1
                renderer.cite_numbers[key] = number
            entry_html = renderer.inlines(inlines)
            entries.append(f'<li id="bib-{html.escape(key)}" value="{number}">{entry_html}</li>')
            bibliography.append({"key": key, "number": number, "text": inlines_to_text(inlines)})
        entries.sort(key=lambda item: int(re.search(r'value="(\d+)"', item).group(1)))
        bibliography_html = '<section class="bibliography" id="bibliography"><h2 class="sec-title">References</h2><ol class="bib-list">' + "".join(entries) + "</ol></section>\n"
    footnotes_html = ""
    if renderer.footnotes:
        footnotes_html = '<section class="footnotes" id="footnotes"><ol>' + "".join(
            f'<li id="footnote-{index + 1}">{text} <a href="#footnote-ref-{index + 1}" class="footnote-back">↩</a></li>'
            for index, text in enumerate(renderer.footnotes)
        ) + "</ol></section>\n"
    document_html = (
        '<article class="paper">\n<header class="paper-header">'
        f'<h1 class="paper-title">{title_html}</h1>'
        + (f'<div class="paper-authors">{authors_html}</div>' if authors_html else "")
        + "</header>\n" + body_html + footnotes_html + bibliography_html + "</article>\n"
    )
    for statement in builder.statements:
        statement.pop("statement", None)
    record = {
        "schema_version": SCHEMA_VERSION,
        "source": {
            "kind": source_kind,
            "path": source_path or str(source_dir),
            "main_file": main.relative_to(source_dir).as_posix(),
            "digest": source_digest(source_dir, main),
        },
        "title": title_text,
        "authors": author_names,
        "html": DOCUMENT_HTML,
        "macros": macros,
        "sections": builder.sections,
        "statements": builder.statements,
        "proofs": builder.proofs,
        "paragraphs": builder.paragraphs,
        "figures": builder.figures,
        "equations": builder.equations,
        "bibliography": bibliography,
        "warnings": warnings,
    }
    (output_dir / DOCUMENT_HTML).write_text(document_html, encoding="utf-8")
    (output_dir / DOCUMENT_JSON).write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return record


def anchor_ids(document: dict) -> dict[str, str]:
    """Map every anchorable identifier to its kind."""
    ids: dict[str, str] = {}
    for section in document.get("sections", []):
        ids[section["id"]] = "section"
    for statement in document.get("statements", []):
        ids[statement["id"]] = statement.get("kind", "environment")
    for proof in document.get("proofs", []):
        ids[proof["id"]] = "proof"
    for paragraph in document.get("paragraphs", []):
        ids[paragraph["id"]] = "paragraph"
    for figure in document.get("figures", []):
        ids[figure["id"]] = figure.get("kind", "figure")
    for equation in document.get("equations", []):
        if equation.get("id"):
            ids[equation["id"]] = "equation"
    return ids


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="convert a LaTeX paper into a reader document")
    parser.add_argument("source", type=Path, help="directory containing the LaTeX source")
    parser.add_argument("output", type=Path, help="directory receiving document.html, document.json, figures/")
    parser.add_argument("--main", help="main .tex file relative to the source directory")
    parser.add_argument("--title")
    parser.add_argument("--author", action="append", default=[])
    parser.add_argument("--no-figures", action="store_true")
    args = parser.parse_args(argv)
    try:
        record = build_document(
            args.source, args.output, main_file=args.main, title=args.title,
            authors=args.author or None, render_figures_enabled=not args.no_figures,
        )
    except DocumentError as exc:
        parser.exit(1, f"error: {exc}\n")
    print(
        f"Wrote {args.output / DOCUMENT_HTML}: {len(record['sections'])} sections, "
        f"{len(record['statements'])} statements, {len(record['proofs'])} proofs, "
        f"{len(record['figures'])} figures, {len(record['warnings'])} warnings."
    )
    for warning in record["warnings"]:
        print(f"warning: {warning}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
