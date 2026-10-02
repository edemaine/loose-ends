#!/usr/bin/env python3
"""Build a citation graph over installed papers and the works they cite.

Nodes are installed papers plus "stubs": referenced works that are not in the
collection. References are resolved to installed papers exactly (arXiv ID or
DOI) or by inferred equivalence (a journal or conference version of an
installed arXiv paper, matched by title and authors). Papers are grouped into
clusters by citation, bibliographic coupling, and co-citation using Louvain
modularity optimization.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from difflib import SequenceMatcher
import hashlib
import json
import math
from pathlib import Path
import re
import sys
from typing import Iterable
import unicodedata

import analyze_papers
import open_problem_common as common


SCHEMA_VERSION = 1
MIN_TITLE_KEY_LENGTH = 12
PAPER_TITLE_SIMILARITY = 0.85
PAPER_TITLE_SIMILARITY_WITHOUT_AUTHORS = 0.95
STUB_TITLE_SIMILARITY = 0.9
MAX_KEYWORDS = 3
MAX_TOP_AUTHORS = 3
LOUVAIN_RESOLUTION = 1.0
ISOLATED_CLUSTER_ID = "isolated"

ARXIV_ID_RE = re.compile(
    r"(?<![\w.])((?:\d{4}\.\d{4,5})|(?:[a-z][a-z\-]*(?:\.[A-Z]{2})?/\d{7}))"
    r"(?:v\d+)?(?![\w.])",
    re.IGNORECASE,
)
DOI_RE = re.compile(r"\b(10\.\d{4,9}/[^\s\"<>]+)", re.IGNORECASE)
LATEX_ACCENT_RE = re.compile(r"\\[`'^\"~=.uvHtcdbkr]\s*\{?\s*([a-zA-Z])\s*\}?")
LATEX_LIGATURE_RE = re.compile(r"\\(ae|oe|ss|aa|o|l|AE|OE|AA|O|L)\b\s*\{?\}?")
LATEX_COMMAND_RE = re.compile(r"\\[a-zA-Z]+\*?")
YEAR_RE = re.compile(r"(?<!\d)((?:19|20)\d{2})(?!\d)")
AUTHOR_SUFFIXES = {"jr", "jr.", "sr", "sr.", "ii", "iii", "iv"}
# Words too common in this corpus to distinguish one cluster of titles from
# another. Shorter words are already dropped by ``title_words``.
STOPWORDS = {
    "about", "above", "after", "again", "algorithm", "algorithms", "among",
    "analysis", "another", "applications", "approach", "based", "between",
    "bound", "bounds", "case", "cases", "characterization", "class",
    "complexity", "computing", "efficient", "every", "extension", "faster",
    "finding", "first", "from", "general", "given", "improved", "into",
    "large", "lower", "many", "more", "most", "near", "note", "optimal",
    "other", "over", "paper", "problem", "problems", "proof", "property",
    "results", "revisited", "several", "simple", "small", "some", "structure",
    "structures", "study", "than", "that", "their", "theorem", "these",
    "this", "three", "through", "time", "toward", "towards", "under", "upper",
    "using", "version", "very", "what", "when", "where", "which", "with",
    "within", "without",
}


@dataclass
class Stub:
    """One referenced work that is not installed, merged across citing papers."""

    identity: str
    titles: Counter = field(default_factory=Counter)
    authors: list[str] = field(default_factory=list)
    years: Counter = field(default_factory=Counter)
    venues: Counter = field(default_factory=Counter)
    arxiv_id: str = ""
    doi: str = ""
    url: str = ""
    raws: list[str] = field(default_factory=list)
    citations: list[tuple[str, int, str]] = field(default_factory=list)

    def absorb(self, reference: dict, paper_id: str) -> None:
        title = reference.get("title", "")
        if title:
            self.titles[title] += 1
        elif reference.get("raw"):
            self.titles[reference["raw"]] += 1
        authors = reference.get("authors", [])
        if len(authors) > len(self.authors):
            self.authors = list(authors)
        if reference.get("year"):
            self.years[reference["year"]] += 1
        if reference.get("venue"):
            self.venues[reference["venue"]] += 1
        self.arxiv_id = self.arxiv_id or reference.get("arxiv_id", "")
        self.doi = self.doi or reference.get("doi", "")
        self.url = self.url or reference.get("url", "")
        if reference.get("raw"):
            self.raws.append(reference["raw"])
        self.citations.append(
            (paper_id, int(reference.get("index", 0)), reference.get("key", ""))
        )

    def merge(self, other: "Stub") -> None:
        self.titles.update(other.titles)
        if len(other.authors) > len(self.authors):
            self.authors = list(other.authors)
        self.years.update(other.years)
        self.venues.update(other.venues)
        self.arxiv_id = self.arxiv_id or other.arxiv_id
        self.doi = self.doi or other.doi
        self.url = self.url or other.url
        self.raws.extend(other.raws)
        self.citations.extend(other.citations)

    @property
    def title(self) -> str:
        if not self.titles:
            return ""
        best = max(self.titles.items(), key=lambda item: (item[1], len(item[0])))
        return best[0]

    def most_common(self, counter: Counter) -> str:
        if not counter:
            return ""
        return max(counter.items(), key=lambda item: (item[1], item[0]))[0]


def strip_latex(value: str) -> str:
    text = LATEX_ACCENT_RE.sub(r"\1", value)
    text = LATEX_LIGATURE_RE.sub(lambda match: match.group(1).lower(), text)
    text = LATEX_COMMAND_RE.sub(" ", text)
    text = text.replace("{", "").replace("}", "").replace("~", " ")
    text = text.replace("$", "").replace("\\", " ")
    return " ".join(text.split())


def ascii_fold(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value)
    return "".join(char for char in decomposed if not unicodedata.combining(char))


def normalize_title(value: str) -> str:
    text = ascii_fold(strip_latex(value or "")).lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def title_words(value: str) -> list[str]:
    return [
        word for word in normalize_title(value).split()
        if len(word) >= 4 and word not in STOPWORDS and not word.isdigit()
    ]


def author_surname(name: str) -> str:
    text = ascii_fold(strip_latex(name or "")).strip()
    if not text:
        return ""
    if "," in text:
        text = text.split(",", 1)[0]
        parts = text.split()
    else:
        parts = text.split()
        while parts and parts[-1].lower().rstrip(".") in {s.rstrip(".") for s in AUTHOR_SUFFIXES}:
            parts.pop()
    if not parts:
        return ""
    surname = re.sub(r"[^a-z]", "", parts[-1].lower())
    return surname


def author_surnames(authors: Iterable[str]) -> set[str]:
    return {
        surname for surname in (author_surname(author) for author in authors)
        if surname
    }


def arxiv_base(value: str) -> str:
    match = ARXIV_ID_RE.search(value or "")
    if not match:
        return ""
    return match.group(1).lower()


def arxiv_from_directory_name(name: str) -> str:
    """Recover an arXiv ID from directory names such as ``arXiv-cs_0410048v4``."""
    if not name.startswith("arXiv-"):
        return ""
    value = name.removeprefix("arXiv-").replace("_", "/")
    return arxiv_base(value)


def normalize_doi(value: str) -> str:
    match = DOI_RE.search(value or "")
    if not match:
        return ""
    return match.group(1).rstrip(".,;)").lower()


def year_of(value: str) -> str:
    match = YEAR_RE.search(value or "")
    return match.group(1) if match else ""


def title_similarity(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    longest = max(len(left), len(right))
    if abs(len(left) - len(right)) > longest * 0.4:
        return 0.0
    return SequenceMatcher(None, left, right).ratio()


def _paper_id(paper: dict) -> str:
    return str(paper.get("key") or paper.get("path") or paper.get("name"))


def _stub_id(identity: str) -> str:
    digest = hashlib.sha1(identity.encode("utf-8", errors="replace")).hexdigest()
    return f"stub:{digest[:12]}"


class _PaperIndex:
    def __init__(self, papers: list[dict]):
        self.by_id: dict[str, dict] = {}
        self.by_arxiv: dict[str, str] = {}
        self.by_doi: dict[str, str] = {}
        self.by_title: dict[str, list[str]] = defaultdict(list)
        self.by_surname: dict[str, set[str]] = defaultdict(set)
        self.by_prefix: dict[str, set[str]] = defaultdict(set)
        self.titles: dict[str, str] = {}
        self.surnames: dict[str, set[str]] = {}
        for paper in papers:
            paper_id = _paper_id(paper)
            self.by_id[paper_id] = paper
            arxiv = arxiv_base(str(paper.get("arxivId") or "")) or (
                arxiv_from_directory_name(str(paper.get("name") or ""))
            )
            if arxiv:
                self.by_arxiv.setdefault(arxiv, paper_id)
            doi = normalize_doi(str(paper.get("doi") or ""))
            if doi:
                self.by_doi.setdefault(doi, paper_id)
            title = normalize_title(str(paper.get("title") or ""))
            self.titles[paper_id] = title
            if len(title) >= MIN_TITLE_KEY_LENGTH:
                self.by_title[title].append(paper_id)
            surnames = author_surnames(paper.get("authors") or [])
            self.surnames[paper_id] = surnames
            for surname in surnames:
                self.by_surname[surname].add(paper_id)
            words = title.split()
            if words:
                self.by_prefix[" ".join(words[:2])].add(paper_id)

    def resolve(self, reference: dict, citing: str) -> tuple[str, str, float] | None:
        """Return (paper id, kind, score) for a reference, if it is installed."""
        arxiv = arxiv_base(reference.get("arxiv_id", "")) or arxiv_base(
            reference.get("url", "")
        ) or arxiv_base(reference.get("raw", ""))
        if arxiv and arxiv in self.by_arxiv and self.by_arxiv[arxiv] != citing:
            return self.by_arxiv[arxiv], "exact", 1.0
        doi = normalize_doi(reference.get("doi", "")) or normalize_doi(
            reference.get("url", "")
        )
        if doi and doi in self.by_doi and self.by_doi[doi] != citing:
            return self.by_doi[doi], "exact", 1.0
        title = normalize_title(reference.get("title", ""))
        if len(title) < MIN_TITLE_KEY_LENGTH:
            return None
        surnames = author_surnames(reference.get("authors") or [])
        for paper_id in self.by_title.get(title, []):
            if paper_id == citing:
                continue
            if not surnames or surnames & self.surnames[paper_id]:
                return paper_id, "inferred", 1.0
        candidates: set[str] = set()
        for surname in surnames:
            candidates |= self.by_surname.get(surname, set())
        words = title.split()
        if words:
            candidates |= self.by_prefix.get(" ".join(words[:2]), set())
        candidates.discard(citing)
        best: tuple[float, str] | None = None
        for paper_id in sorted(candidates):
            score = title_similarity(title, self.titles[paper_id])
            shared_authors = bool(surnames & self.surnames[paper_id])
            threshold = (
                PAPER_TITLE_SIMILARITY
                if shared_authors
                else PAPER_TITLE_SIMILARITY_WITHOUT_AUTHORS
            )
            if score >= threshold and (best is None or score > best[0]):
                best = (score, paper_id)
        if best is None:
            return None
        return best[1], "inferred", round(best[0], 3)


def _stub_identity(reference: dict) -> str:
    arxiv = arxiv_base(reference.get("arxiv_id", "")) or arxiv_base(
        reference.get("url", "")
    )
    if arxiv:
        return f"arxiv:{arxiv}"
    doi = normalize_doi(reference.get("doi", ""))
    if doi:
        return f"doi:{doi}"
    title = normalize_title(reference.get("title", ""))
    if len(title) >= MIN_TITLE_KEY_LENGTH:
        return f"title:{title}"
    raw = normalize_title(reference.get("raw", "")) or title
    return f"raw:{raw}"


class _UnionFind:
    def __init__(self, items: Iterable[str]):
        self.parent = {item: item for item in items}

    def find(self, item: str) -> str:
        root = item
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[item] != root:
            self.parent[item], item = root, self.parent[item]
        return root

    def union(self, left: str, right: str) -> None:
        left, right = self.find(left), self.find(right)
        if left == right:
            return
        if left < right:
            self.parent[right] = left
        else:
            self.parent[left] = right


def _merge_stubs(stubs: dict[str, Stub]) -> dict[str, Stub]:
    """Merge stubs whose titles are near-identical (journal versus preprint)."""
    union = _UnionFind(stubs)
    blocks: dict[str, list[str]] = defaultdict(list)
    normalized: dict[str, str] = {}
    surnames: dict[str, set[str]] = {}
    for identity, stub in stubs.items():
        title = normalize_title(stub.title)
        normalized[identity] = title
        names = author_surnames(stub.authors)
        surnames[identity] = names
        if len(title) < MIN_TITLE_KEY_LENGTH:
            continue
        words = title.split()
        keys = set(names) | {" ".join(words[:2])}
        for key in keys:
            blocks[key].append(identity)
    for members in blocks.values():
        if len(members) < 2 or len(members) > 400:
            continue
        for index, left in enumerate(members):
            for right in members[index + 1:]:
                if union.find(left) == union.find(right):
                    continue
                score = title_similarity(normalized[left], normalized[right])
                if score < STUB_TITLE_SIMILARITY:
                    continue
                if (
                    surnames[left]
                    and surnames[right]
                    and not surnames[left] & surnames[right]
                ):
                    continue
                union.union(left, right)
    merged: dict[str, Stub] = {}
    for identity in sorted(stubs):
        root = union.find(identity)
        if root not in merged:
            merged[root] = Stub(identity=root)
        merged[root].merge(stubs[identity])
    return merged


def _louvain(
    nodes: list[str],
    weights: dict[tuple[str, str], float],
    *,
    resolution: float = LOUVAIN_RESOLUTION,
) -> dict[str, int]:
    """Deterministic Louvain community detection for a small weighted graph."""
    adjacency: dict[str, dict[str, float]] = {node: {} for node in nodes}
    for (left, right), weight in weights.items():
        if left == right or weight <= 0 or left not in adjacency or right not in adjacency:
            continue
        adjacency[left][right] = adjacency[left].get(right, 0.0) + weight
        adjacency[right][left] = adjacency[right].get(left, 0.0) + weight
    total = sum(sum(neighbors.values()) for neighbors in adjacency.values()) / 2
    if total <= 0:
        return {node: index for index, node in enumerate(nodes)}

    membership = {node: node for node in nodes}
    current_nodes = list(nodes)
    current_adjacency = adjacency
    while True:
        degree = {
            node: sum(neighbors.values())
            for node, neighbors in current_adjacency.items()
        }
        community = {node: node for node in current_nodes}
        community_degree = dict(degree)
        improved = False
        moved = True
        while moved:
            moved = False
            for node in current_nodes:
                own = community[node]
                links: dict[str, float] = defaultdict(float)
                for neighbor, weight in current_adjacency[node].items():
                    if neighbor == node:
                        continue  # a self-loop moves with the node
                    links[community[neighbor]] += weight
                community_degree[own] -= degree[node]
                best_community = own
                best_gain = links.get(own, 0.0) - resolution * (
                    community_degree[own] * degree[node] / (2 * total)
                )
                for candidate in sorted(links):
                    if candidate == own:
                        continue
                    gain = links[candidate] - resolution * (
                        community_degree[candidate] * degree[node] / (2 * total)
                    )
                    if gain > best_gain + 1e-12:
                        best_gain = gain
                        best_community = candidate
                community_degree[best_community] += degree[node]
                if best_community != own:
                    community[node] = best_community
                    moved = True
                    improved = True
        if not improved:
            break
        groups: dict[str, list[str]] = defaultdict(list)
        for node in current_nodes:
            groups[community[node]].append(node)
        membership = {node: community[membership[node]] for node in nodes}
        new_nodes = sorted(groups)
        new_adjacency: dict[str, dict[str, float]] = {node: {} for node in new_nodes}
        for node in current_nodes:
            source = community[node]
            for neighbor, weight in current_adjacency[node].items():
                # Internal edges become self-loops so that an aggregated
                # node keeps the full degree of its members.
                target = community[neighbor]
                new_adjacency[source][target] = (
                    new_adjacency[source].get(target, 0.0) + weight
                )
        current_nodes = new_nodes
        current_adjacency = new_adjacency

    ordered = sorted(set(membership.values()))
    index_of = {value: index for index, value in enumerate(ordered)}
    return {node: index_of[membership[node]] for node in nodes}


def _cluster_keywords(titles: list[str], document_frequency: Counter, total: int) -> list[str]:
    counts: Counter = Counter()
    for title in titles:
        counts.update(set(title_words(title)))
    scored = []
    for word, count in counts.items():
        idf = math.log((1 + total) / (1 + document_frequency.get(word, 0)))
        scored.append((count * idf, count, word))
    scored.sort(key=lambda item: (-item[0], -item[1], item[2]))
    shared = [entry for entry in scored if entry[1] >= 2]
    # Prefer words that recur across the cluster's titles; small clusters
    # without any recurring word fall back to their most distinctive words.
    chosen = shared if shared or len(titles) == 1 else scored
    return [word for _, _, word in chosen[:MAX_KEYWORDS]]


def _top_authors(papers: list[dict]) -> list[str]:
    counts: Counter = Counter()
    display: dict[str, str] = {}
    for paper in papers:
        for author in paper.get("authors") or []:
            surname = author_surname(author)
            if not surname:
                continue
            counts[surname] += 1
            display.setdefault(surname, strip_latex(author))
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return [display[surname] for surname, _ in ranked[:MAX_TOP_AUTHORS]]


def _cluster_vote_rank(item: tuple[str, int]) -> tuple[int, bool, int]:
    """Rank a stub's cluster votes: most citers, a real cluster, largest cluster."""
    cluster_id, votes = item
    number = int(cluster_id[1:]) if cluster_id[1:].isdigit() else 0
    return votes, cluster_id != ISOLATED_CLUSTER_ID, -number


def _reference_record(reference: dict) -> dict:
    return {
        "index": int(reference.get("index", 0)),
        "key": reference.get("key", ""),
        "title": reference.get("title", ""),
        "authors": list(reference.get("authors") or []),
        "year": reference.get("year", ""),
        "venue": reference.get("venue", ""),
        "arxivId": reference.get("arxiv_id", ""),
        "doi": reference.get("doi", ""),
        "url": reference.get("url", ""),
        "raw": reference.get("raw", ""),
    }


def build_graph(papers: list[dict]) -> dict:
    """Build the citation graph from catalog paper records with references."""
    papers = [paper for paper in papers if isinstance(paper, dict)]
    index = _PaperIndex(papers)
    paper_ids = [_paper_id(paper) for paper in papers]
    references_of: dict[str, list[dict]] = {}
    resolved: dict[str, dict[str, dict]] = defaultdict(dict)
    stubs: dict[str, Stub] = {}
    stub_citations: dict[str, dict[str, dict]] = defaultdict(dict)
    resolved_exact = resolved_inferred = total_references = 0

    for paper in papers:
        paper_id = _paper_id(paper)
        entries = paper.get("references") or []
        records = []
        for reference in entries:
            if not isinstance(reference, dict):
                continue
            total_references += 1
            record = _reference_record(reference)
            match = index.resolve(reference, paper_id)
            if match is not None:
                target, kind, score = match
                record.update({"target": target, "kind": kind, "score": score})
                if kind == "exact":
                    resolved_exact += 1
                else:
                    resolved_inferred += 1
                existing = resolved[paper_id].get(target)
                if existing is None or (kind == "exact" and existing["kind"] != "exact"):
                    resolved[paper_id][target] = {
                        "source": paper_id,
                        "target": target,
                        "kind": kind,
                        "score": score,
                        "refIndex": record["index"],
                        "refKey": record["key"],
                    }
            else:
                identity = _stub_identity(reference)
                stub = stubs.get(identity)
                if stub is None:
                    stub = stubs[identity] = Stub(identity=identity)
                stub.absorb(reference, paper_id)
                record.update({"target": identity, "kind": "stub", "score": 0.0})
            records.append(record)
        references_of[paper_id] = records

    merged = _merge_stubs(stubs)
    stub_nodes: dict[str, Stub] = {
        _stub_id(root): stub for root, stub in merged.items()
    }
    # Point every reference that became a stub at the merged stub node.
    stub_of_reference: dict[tuple[str, int], str] = {}
    for stub_id, stub in stub_nodes.items():
        for paper_id, ref_index, _ in stub.citations:
            stub_of_reference[(paper_id, ref_index)] = stub_id
    for paper_id, records in references_of.items():
        for record in records:
            if record.get("kind") == "stub":
                stub_id = stub_of_reference.get((paper_id, record["index"]))
                record["target"] = stub_id or ""
                if stub_id:
                    existing = stub_citations[stub_id].get(paper_id)
                    if existing is None:
                        stub_citations[stub_id][paper_id] = {
                            "source": paper_id,
                            "target": stub_id,
                            "kind": "exact",
                            "score": 1.0,
                            "refIndex": record["index"],
                            "refKey": record["key"],
                        }

    edges: list[dict] = []
    for paper_id in paper_ids:
        for target in sorted(resolved[paper_id]):
            edges.append(resolved[paper_id][target])
    for stub_id in sorted(stub_citations):
        for paper_id in sorted(stub_citations[stub_id]):
            edges.append(stub_citations[stub_id][paper_id])

    cited_by: dict[str, list[dict]] = defaultdict(list)
    for edge in edges:
        cited_by[edge["target"]].append(
            {"paper": edge["source"], "kind": edge["kind"], "score": edge["score"]}
        )

    # Clustering weights among installed papers.
    weights: dict[tuple[str, str], float] = defaultdict(float)

    def add_weight(left: str, right: str, weight: float) -> None:
        if left == right:
            return
        key = (left, right) if left < right else (right, left)
        weights[key] += weight

    for paper_id in paper_ids:
        for target in resolved[paper_id]:
            add_weight(paper_id, target, 1.0)
    citers_of: dict[str, set[str]] = defaultdict(set)
    for edge in edges:
        citers_of[edge["target"]].add(edge["source"])
    for target, citers in citers_of.items():
        if len(citers) < 2:
            continue
        ordered = sorted(citers)
        share = 1.0 / (len(ordered) - 1)
        for position, left in enumerate(ordered):
            for right in ordered[position + 1:]:
                add_weight(left, right, share)
    for paper_id in paper_ids:
        targets = sorted(resolved[paper_id])
        if len(targets) < 2:
            continue
        share = 0.5 / (len(targets) - 1)
        for position, left in enumerate(targets):
            for right in targets[position + 1:]:
                add_weight(left, right, share)

    connected = {node for pair in weights for node in pair}
    connected_ids = [paper_id for paper_id in paper_ids if paper_id in connected]
    membership = _louvain(connected_ids, dict(weights))
    cluster_members: dict[int, list[str]] = defaultdict(list)
    for paper_id in connected_ids:
        cluster_members[membership[paper_id]].append(paper_id)
    ordered_clusters = sorted(
        cluster_members.items(), key=lambda item: (-len(item[1]), item[1][0])
    )
    document_frequency: Counter = Counter()
    for paper in papers:
        document_frequency.update(set(title_words(str(paper.get("title") or ""))))
    cluster_of: dict[str, str] = {}
    clusters: list[dict] = []
    for position, (_, members) in enumerate(ordered_clusters, start=1):
        cluster_id = f"c{position}"
        member_papers = [index.by_id[paper_id] for paper_id in members]
        keywords = _cluster_keywords(
            [str(paper.get("title") or "") for paper in member_papers],
            document_frequency,
            len(papers),
        )
        for paper_id in members:
            cluster_of[paper_id] = cluster_id
        clusters.append(
            {
                "id": cluster_id,
                "label": " · ".join(word.capitalize() for word in keywords)
                or f"Cluster {position}",
                "keywords": keywords,
                "topAuthors": _top_authors(member_papers),
                "papers": sorted(members),
                "stubs": [],
                "size": len(members),
            }
        )
    isolated = [paper_id for paper_id in paper_ids if paper_id not in connected]
    if isolated:
        for paper_id in isolated:
            cluster_of[paper_id] = ISOLATED_CLUSTER_ID
        clusters.append(
            {
                "id": ISOLATED_CLUSTER_ID,
                "label": "Not connected",
                "keywords": [],
                "topAuthors": [],
                "papers": sorted(isolated),
                "stubs": [],
                "size": len(isolated),
            }
        )
    cluster_by_id = {cluster["id"]: cluster for cluster in clusters}

    nodes: list[dict] = []
    for paper in papers:
        paper_id = _paper_id(paper)
        records = references_of.get(paper_id, [])
        nodes.append(
            {
                "id": paper_id,
                "kind": "paper",
                "key": paper.get("key", paper_id),
                "path": paper.get("path", ""),
                "urlKey": paper.get("urlKey", ""),
                "name": paper.get("name", ""),
                "title": paper.get("title", ""),
                "authors": list(paper.get("authors") or []),
                "year": year_of(str(paper.get("published") or "")),
                "arxivId": paper.get("arxivId", ""),
                "doi": paper.get("doi", ""),
                "url": paper.get("url", ""),
                "clusterId": cluster_of.get(paper_id, ISOLATED_CLUSTER_ID),
                "referencesExtracted": bool(paper.get("referencesExtracted")),
                "referenceCount": len(records),
                "resolvedCount": sum(
                    1 for record in records if record.get("kind") != "stub"
                ),
                "citedByCount": len(cited_by.get(paper_id, [])),
                "references": records,
                "citedBy": cited_by.get(paper_id, []),
            }
        )
    shared_stubs = 0
    for stub_id in sorted(stub_nodes):
        stub = stub_nodes[stub_id]
        citers = cited_by.get(stub_id, [])
        if len(citers) >= 2:
            shared_stubs += 1
        votes: Counter = Counter(
            cluster_of.get(entry["paper"], ISOLATED_CLUSTER_ID) for entry in citers
        )
        cluster_id = ""
        if votes:
            cluster_id = max(votes.items(), key=_cluster_vote_rank)[0]
            cluster_by_id[cluster_id]["stubs"].append(stub_id)
        nodes.append(
            {
                "id": stub_id,
                "kind": "stub",
                "title": stub.title,
                "authors": stub.authors,
                "year": stub.most_common(stub.years),
                "venue": stub.most_common(stub.venues),
                "arxivId": stub.arxiv_id,
                "doi": stub.doi,
                "url": stub.url,
                "raw": stub.raws[0] if stub.raws else "",
                "clusterId": cluster_id,
                "citedByCount": len(citers),
                "variants": len(stub.titles),
                "citedBy": citers,
            }
        )

    return {
        "schemaVersion": SCHEMA_VERSION,
        "nodes": nodes,
        "edges": edges,
        "clusters": clusters,
        "stats": {
            "papers": len(papers),
            "papersWithReferences": sum(
                1 for paper in papers if paper.get("referencesExtracted")
            ),
            "references": total_references,
            "resolvedExact": resolved_exact,
            "resolvedInferred": resolved_inferred,
            "stubs": len(stub_nodes),
            "sharedStubs": shared_stubs,
            "paperEdges": sum(len(targets) for targets in resolved.values()),
            "clusters": len([c for c in clusters if c["id"] != ISOLATED_CLUSTER_ID]),
        },
    }


def paper_records(paths: Iterable[Path]) -> list[dict]:
    """Build minimal catalog-like paper records from installed directories."""
    records = []
    for paper in analyze_papers.discover_paper_directories(paths):
        metadata = common.load_json(paper / "metadata.json") or {}
        manifest = common.load_json(paper / "references" / "references.json") or {}
        references = manifest.get("references") if isinstance(manifest, dict) else []
        records.append(
            {
                "key": str(paper.resolve()),
                "path": str(paper.resolve()),
                "name": paper.name,
                "title": metadata.get("title") or paper.name,
                "authors": metadata.get("authors") or [],
                "published": metadata.get("published") or "",
                "arxivId": metadata.get("arxiv_id") or "",
                "doi": metadata.get("doi") or "",
                "url": metadata.get("url") or "",
                "referencesExtracted": bool(manifest),
                "references": references if isinstance(references, list) else [],
            }
        )
    return records


def summarize(graph: dict) -> str:
    stats = graph["stats"]
    lines = [
        f"{stats['papers']} papers, {stats['papersWithReferences']} with references",
        f"{stats['references']} references: {stats['resolvedExact']} exact, "
        f"{stats['resolvedInferred']} inferred, {stats['stubs']} stubs "
        f"({stats['sharedStubs']} shared by 2+ papers)",
        f"{stats['paperEdges']} paper-to-paper citations, {stats['clusters']} clusters",
    ]
    for cluster in graph["clusters"]:
        lines.append(f"  [{cluster['id']}] {cluster['label']}: {cluster['size']} papers")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="build the citation graph of installed papers"
    )
    parser.add_argument("papers", nargs="+", type=Path, help="paper directories or roots")
    parser.add_argument("--output", type=Path, help="write the graph JSON here")
    parser.add_argument(
        "--summary", action="store_true", help="print a summary instead of JSON"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        graph = build_graph(paper_records(args.papers))
    except analyze_papers.AnalysisError as exc:
        print(f"{parser.prog}: error: {exc}", file=sys.stderr)
        return 1
    if args.output:
        args.output.write_text(
            json.dumps(graph, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
    if args.summary or args.output:
        print(summarize(graph))
    else:
        print(json.dumps(graph, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
