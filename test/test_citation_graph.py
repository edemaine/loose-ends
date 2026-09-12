from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import citation_graph


def reference(title, authors=(), **fields):
    record = {
        "index": fields.pop("index", 1),
        "key": "", "title": title, "authors": list(authors), "year": "",
        "venue": "", "arxiv_id": "", "doi": "", "url": "", "raw": "",
    }
    record.update(fields)
    return record


def paper(key, title, authors, references=None, **fields):
    record = {
        "key": key, "path": key, "name": key, "title": title, "authors": authors,
        "published": "2010-01-01", "arxivId": "", "doi": "", "url": "",
        "referencesExtracted": references is not None,
        "references": references or [],
    }
    record.update(fields)
    return record


class NormalizationTests(unittest.TestCase):
    def test_normalize_title_strips_latex_and_accents(self):
        self.assertEqual(
            citation_graph.normalize_title(r"The {V}oronoi Diagram\,of Sacrist\'an's points"),
            "the voronoi diagram of sacristan s points",
        )
        self.assertEqual(
            citation_graph.normalize_title("Dynamic algorithms in computational geometry."),
            "dynamic algorithms in computational geometry",
        )

    def test_author_surname_handles_common_forms(self):
        self.assertEqual(citation_graph.author_surname("Stefan Langerman"), "langerman")
        self.assertEqual(citation_graph.author_surname("S.~Langerman"), "langerman")
        self.assertEqual(citation_graph.author_surname("Langerman, Stefan"), "langerman")
        self.assertEqual(citation_graph.author_surname("E. D. Demaine, Jr."), "demaine")
        self.assertEqual(citation_graph.author_surname(r"V. Sacrist\'an"), "sacristan")

    def test_arxiv_identifiers(self):
        self.assertEqual(citation_graph.arxiv_base("arXiv:1603.08485v2"), "1603.08485")
        self.assertEqual(citation_graph.arxiv_base("https://arxiv.org/abs/cs/0410048v4"), "cs/0410048")
        self.assertEqual(citation_graph.arxiv_base("volume 12, pages 34"), "")
        self.assertEqual(
            citation_graph.arxiv_from_directory_name("arXiv-cs_0410048v4"), "cs/0410048"
        )
        self.assertEqual(citation_graph.arxiv_from_directory_name("main"), "")

    def test_doi_normalization(self):
        self.assertEqual(
            citation_graph.normalize_doi("https://doi.org/10.1007/S00454-016-9788-1."),
            "10.1007/s00454-016-9788-1",
        )


class GraphTests(unittest.TestCase):
    def build(self):
        papers = [
            paper(
                "A", "Incremental Voronoi Diagrams", ["S. Allen", "S. Langerman"],
                arxivId="1603.08485v1",
                references=[
                    reference(
                        "Data structures for halfplane proximity queries",
                        ["B. Aronov", "S. Langerman"], index=1, arxiv_id="cs/0512091",
                    ),
                    reference(
                        "Circle separability queries in logarithmic time.",
                        ["G. Aloupis", "L. Barba", "S. Langerman"], index=2, year="2012",
                    ),
                    reference(
                        "A data structure for dynamic trees.",
                        ["D. D. Sleator", "R. E. Tarjan"], index=3, year="1983",
                    ),
                    reference("Some unrelated book", ["X. Person"], index=4),
                ],
            ),
            paper(
                "B", "Data Structures for Halfplane Proximity Queries",
                ["B. Aronov", "S. Langerman"], name="arXiv-cs_0512091v3",
                references=[
                    reference(
                        "A data structure for dynamic trees",
                        ["Daniel D. Sleator", "Robert Endre Tarjan"], index=1, year="1983",
                    ),
                ],
            ),
            paper(
                "C", "Circle separability queries in logarithmic time",
                ["G. Aloupis", "L. Barba", "S. Langerman"], arxivId="1203.6266v1",
            ),
            paper("D", "Something Else Entirely", ["Z. Other"]),
        ]
        return citation_graph.build_graph(papers)

    def test_resolves_exact_and_inferred_citations(self):
        graph = self.build()
        edges = {
            (edge["source"], edge["target"]): edge
            for edge in graph["edges"] if not edge["target"].startswith("stub:")
        }
        self.assertEqual(edges[("A", "B")]["kind"], "exact")
        self.assertEqual(edges[("A", "C")]["kind"], "inferred")
        self.assertEqual(edges[("A", "C")]["refIndex"], 2)
        self.assertEqual(graph["stats"]["resolvedExact"], 1)
        self.assertEqual(graph["stats"]["resolvedInferred"], 1)
        self.assertEqual(graph["stats"]["paperEdges"], 2)

    def test_merges_shared_stubs_across_papers(self):
        graph = self.build()
        stubs = [node for node in graph["nodes"] if node["kind"] == "stub"]
        self.assertEqual(len(stubs), 2)
        shared = next(node for node in stubs if node["citedByCount"] == 2)
        self.assertIn("dynamic trees", shared["title"].lower())
        self.assertEqual({entry["paper"] for entry in shared["citedBy"]}, {"A", "B"})
        self.assertEqual(shared["year"], "1983")
        self.assertEqual(graph["stats"]["sharedStubs"], 1)
        # Every stub-directed reference points at its merged stub node.
        node_a = next(node for node in graph["nodes"] if node["id"] == "A")
        targets = {record["index"]: record["target"] for record in node_a["references"]}
        self.assertEqual(targets[3], shared["id"])
        self.assertTrue(targets[4].startswith("stub:"))

    def test_clusters_connected_papers_and_isolates_the_rest(self):
        graph = self.build()
        cluster_of = {node["id"]: node["clusterId"] for node in graph["nodes"] if node["kind"] == "paper"}
        self.assertEqual(cluster_of["A"], cluster_of["B"])
        self.assertEqual(cluster_of["A"], cluster_of["C"])
        self.assertEqual(cluster_of["D"], citation_graph.ISOLATED_CLUSTER_ID)
        first = graph["clusters"][0]
        self.assertEqual(sorted(first["papers"]), ["A", "B", "C"])
        self.assertTrue(first["label"])
        self.assertEqual(graph["clusters"][-1]["id"], citation_graph.ISOLATED_CLUSTER_ID)
        self.assertEqual(graph["stats"]["clusters"], 1)

    def test_cited_by_lists_citing_papers(self):
        graph = self.build()
        node_c = next(node for node in graph["nodes"] if node["id"] == "C")
        self.assertEqual(node_c["citedBy"], [{"paper": "A", "kind": "inferred", "score": 1.0}])
        self.assertEqual(node_c["citedByCount"], 1)
        self.assertFalse(node_c["referencesExtracted"])

    def test_louvain_separates_weakly_joined_triangles(self):
        nodes = ["a", "b", "c", "d", "e", "f"]
        weights = {
            ("a", "b"): 1.0, ("a", "c"): 1.0, ("b", "c"): 1.0,
            ("d", "e"): 1.0, ("d", "f"): 1.0, ("e", "f"): 1.0,
            ("c", "d"): 0.1,
        }
        membership = citation_graph._louvain(nodes, weights)
        self.assertEqual(membership["a"], membership["b"])
        self.assertEqual(membership["a"], membership["c"])
        self.assertEqual(membership["d"], membership["e"])
        self.assertEqual(membership["d"], membership["f"])
        self.assertNotEqual(membership["a"], membership["d"])

    def test_self_citations_are_ignored(self):
        graph = citation_graph.build_graph([
            paper("A", "Alpha Paper Title Here", ["Q. Author"], arxivId="1111.11111",
                  references=[reference("Alpha Paper Title Here", ["Q. Author"], arxiv_id="1111.11111")]),
        ])
        self.assertEqual(graph["stats"]["paperEdges"], 0)
        self.assertEqual(graph["stats"]["stubs"], 1)


if __name__ == "__main__":
    unittest.main()
