import json
from pathlib import Path
import shutil
import sys
from tempfile import TemporaryDirectory
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from validation import visualization as visualization_validation
from validation import visualization_review as review_validation
import codex_cli
import paper_document
import visualize_paper
import visualizations


PROJECT_ROOT = Path(__file__).resolve().parents[1]
HAS_PANDOC = shutil.which("pandoc") is not None

SAMPLE_TEX = r"""
\documentclass{article}
\usepackage{amsmath,amsthm}
\newtheorem{theorem}{Theorem}[section]
\newtheorem{lemma}[theorem]{Lemma}
\theoremstyle{definition}
\newtheorem{definition}[theorem]{Definition}
\newcommand{\D}{\mathcal D}
\DeclareMathOperator{\area}{area}
\title{Flat Doubles}
\author{Ada Lovelace \and Emmy Noether}
\begin{document}
\maketitle
\begin{abstract}
We study the double $\D(P)$. % a comment
\end{abstract}
\section{Introduction}\label{sec:intro}
Let $P$ be a polygon.  Its \emph{double} $\D(P)$ has area $2\area(P)$, see
\eqref{eq:area} and Lemma~\ref{lem:one}; also \cref{thm:main} and \cite{knuth}.
\begin{equation}\label{eq:area}
  \area(\D(P)) = 2\area(P).
\end{equation}
\begin{definition}[Lattice polygon]\label{def:lattice}
A polygon is \emph{lattice-drawn} if its vertices lie in $\mathbb Z^2$.
\end{definition}
\section{Results}
\begin{theorem}[Main]\label{thm:main}
Every lattice-drawn polygon $P$ satisfies $\area(P) \in \tfrac12\mathbb Z$.
\end{theorem}
\begin{lemma}\label{lem:one}
Reflection preserves the lattice.
\end{lemma}
\begin{proof}
First paragraph of the lemma proof.

Second paragraph.
\end{proof}
\begin{proof}[Proof of Theorem~\ref{thm:main}]
Use Lemma~\ref{lem:one}.
\begin{itemize}
\item one \item two
\end{itemize}
Done.
\end{proof}
\begin{figure}
\centering
\includegraphics{missing-figure}
\caption{A figure about $P$.}
\label{fig:one}
\end{figure}
Figure~\ref{fig:one} shows it.
\begin{thebibliography}{9}
\bibitem{knuth} D. Knuth. \newblock The Art of Computer Programming. 1968.
\end{thebibliography}
\end{document}
"""


def build_sample(root: Path) -> dict:
    source = root / "draft-001"
    source.mkdir()
    (source / "main.tex").write_text(SAMPLE_TEX, encoding="utf-8")
    return paper_document.build_document(source, source / "visualization", render_figures_enabled=False)


@unittest.skipUnless(HAS_PANDOC, "pandoc is required to convert LaTeX")
class PaperDocumentTests(unittest.TestCase):
    def test_converts_structure_numbering_and_references(self):
        with TemporaryDirectory() as temporary:
            document = build_sample(Path(temporary))
            html = (Path(temporary) / "draft-001" / "visualization" / "document.html").read_text(encoding="utf-8")

        self.assertEqual(document["title"], "Flat Doubles")
        self.assertEqual(document["authors"], ["Ada Lovelace", "Emmy Noether"])
        self.assertEqual([s["number"] for s in document["sections"]], ["1", "2"])
        self.assertEqual(document["sections"][0]["id"], "sec:intro")
        labels = {s["id"]: s["label"] for s in document["statements"]}
        self.assertEqual(labels, {"def:lattice": "Definition 1.1", "thm:main": "Theorem 2.1", "lem:one": "Lemma 2.2"})
        theorem = next(s for s in document["statements"] if s["id"] == "thm:main")
        self.assertEqual(theorem["title"], "Main")
        self.assertIn("$\\operatorname{area}(P)", theorem["text"].replace("\\area", "\\operatorname{area}"))
        self.assertEqual(theorem["proofs"], ["proof-2"])
        proofs = {p["id"]: p for p in document["proofs"]}
        self.assertEqual(proofs["proof-1"]["of"], "lem:one")
        self.assertEqual(len(proofs["proof-1"]["paragraphs"]), 2)
        self.assertEqual(proofs["proof-2"]["title"], "Proof of Theorem 2.1")
        self.assertEqual(document["equations"][0], {"id": "eq:area", "number": "1", "latex": "\\area(\\D(P)) = 2\\area(P)."} | {"latex": document["equations"][0]["latex"]})
        self.assertEqual(document["figures"][0]["label"], "Figure 1")
        self.assertEqual(document["bibliography"][0]["key"], "knuth")
        self.assertEqual(document["macros"]["\\area"], "\\operatorname{area}")
        self.assertIn('<a class="ref" href="#lem:one" data-ref-kind="lemma">2.2</a>', html)
        self.assertIn('>Theorem 2.1</a>', html)  # \cref renders kind + number
        self.assertIn('>(1)</a>', html)
        self.assertIn('id="eq:area" data-number="1"', html)
        self.assertIn('<div class="env env-theorem" id="thm:main"', html)
        self.assertIn('<div class="proof" id="proof-2" data-of="thm:main"', html)
        self.assertIn('href="#bib-knuth"', html)
        self.assertIn("Figure could not be rendered", html)
        self.assertNotIn("a comment", html)

    def test_anchor_ids_cover_every_addressable_element(self):
        with TemporaryDirectory() as temporary:
            document = build_sample(Path(temporary))
        ids = paper_document.anchor_ids(document)
        self.assertEqual(ids["thm:main"], "theorem")
        self.assertEqual(ids["proof-1"], "proof")
        self.assertEqual(ids["sec:intro"], "section")
        self.assertEqual(ids["fig:one"], "figure")
        self.assertEqual(ids["eq:area"], "equation")
        self.assertTrue(any(kind == "paragraph" for kind in ids.values()))


class PackageTests(unittest.TestCase):
    def test_widget_id_is_a_stable_slug_of_the_anchor(self):
        self.assertEqual(visualizations.widget_id("lem:tiling-completion"), "lem-tiling-completion")
        self.assertEqual(visualizations.widget_id("Proof_2"), "proof-2")
        self.assertEqual(visualizations.widget_id("::"), "widget")

    def test_resolve_file_rejects_traversal_and_symlinks(self):
        with TemporaryDirectory() as temporary:
            package = Path(temporary) / "visualization"
            (package / "widgets" / "w").mkdir(parents=True)
            (package / "widgets" / "w" / "widget.js").write_text("x", encoding="utf-8")
            (Path(temporary) / "secret.txt").write_text("s", encoding="utf-8")
            (package / "link.txt").symlink_to(Path(temporary) / "secret.txt")
            self.assertEqual(
                visualizations.resolve_file(package, "widgets/w/widget.js"),
                (package / "widgets" / "w" / "widget.js").resolve(),
            )
            with self.assertRaises(ValueError):
                visualizations.resolve_file(package, "../secret.txt")
            with self.assertRaises(ValueError):
                visualizations.resolve_file(package, "link.txt")
            with self.assertRaises(FileNotFoundError):
                visualizations.resolve_file(package, "widgets/w/missing.js")

    @unittest.skipUnless(HAS_PANDOC, "pandoc is required to convert LaTeX")
    def test_discover_reports_widgets_and_annotations(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            document = build_sample(root)
            source = root / "draft-001"
            package = source / "visualization"
            self.assertIsNone(visualizations.discover(source))
            manifest = visualizations.new_manifest(document, source={"kind": "draft"})
            widget = package / "widgets" / "thm-main"
            widget.mkdir(parents=True)
            (widget / "widget.js").write_text('LooseEnds.registerWidget("thm-main", () => ({}));', encoding="utf-8")
            (widget / "review.json").write_text(json.dumps({"fidelity": "minor_gaps", "interaction_quality": "works"}), encoding="utf-8")
            manifest["widgets"] = [{"id": "thm-main", "anchor": "thm:main", "kind": "statement", "title": "Areas"}]
            manifest["widgets"].append({"id": "ghost", "anchor": "lem:one", "kind": "statement", "title": "Missing files"})
            manifest["annotations"] = "annotations.json"
            (package / "annotations.json").write_text(json.dumps({"main_result": "thm:main", "glossary": [{"id": "d", "term": "double", "anchor": "par-1", "gloss": "g"}]}), encoding="utf-8")
            visualizations.write_manifest(package, manifest)

            record = visualizations.discover(source)

        self.assertEqual(record["title"], "Flat Doubles")
        self.assertEqual(record["widgetCount"], 1)
        self.assertEqual(record["widgets"][0]["fidelity"], "minor_gaps")
        self.assertEqual(record["glossaryCount"], 1)
        self.assertEqual(record["mainResult"], "thm:main")
        self.assertEqual(record["statementCount"], 3)


@unittest.skipUnless(HAS_PANDOC, "pandoc is required to convert LaTeX")
class VisualizeDriverTests(unittest.TestCase):
    def test_source_from_path_accepts_drafts_and_papers_only(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            draft = root / "draft-003"
            draft.mkdir()
            (draft / "main.tex").write_text(SAMPLE_TEX, encoding="utf-8")
            (draft / "manifest.json").write_text(json.dumps({"title": "T", "authors": ["A"]}), encoding="utf-8")
            source = visualize_paper.source_from_path(draft)
            self.assertEqual((source.kind, source.title, source.authors), ("draft", "T", ("A",)))
            paper = root / "arXiv-1.2v1"
            (paper / "source").mkdir(parents=True)
            (paper / "metadata.json").write_text(json.dumps({"title": "P", "authors": ["B", "C"]}), encoding="utf-8")
            source = visualize_paper.source_from_path(paper)
            self.assertEqual((source.kind, source.latex_directory.name), ("paper", "source"))
            with self.assertRaises(codex_cli.CodexError):
                visualize_paper.source_from_path(root)

    def test_ensure_document_builds_once_and_resolves_anchors(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            draft = root / "draft-001"
            draft.mkdir()
            (draft / "main.tex").write_text(SAMPLE_TEX, encoding="utf-8")
            (draft / "manifest.json").write_text("{}", encoding="utf-8")
            source = visualize_paper.source_from_path(draft)
            document, manifest = visualize_paper.ensure_document(source)
            built_at = manifest["document"]["built_at"]
            document_again, manifest_again = visualize_paper.ensure_document(source)
            self.assertEqual(manifest_again["document"]["built_at"], built_at)
            self.assertEqual(document_again["statements"], document["statements"])
            self.assertEqual(visualize_paper.resolve_anchors(document, ["default", "thm:main", "proof-1", "thm:main"]), ["default", "thm:main", "proof-1"])
            with self.assertRaises(codex_cli.CodexError):
                visualize_paper.resolve_anchors(document, ["par-1"])
            with self.assertRaises(codex_cli.CodexError):
                visualize_paper.resolve_anchors(document, ["nope"])
            prompt = visualize_paper._render_prompt("TEMPLATE", document, visualize_paper._request(document, ["default", "proof-1"], manifest))
            self.assertIn("output/widgets/proof-1/", prompt)
            self.assertIn("`thm:main`: Theorem 2.1 (Main)", prompt)
            self.assertIn("Write `output/annotations.json`", prompt)

    def test_transient_provider_failures_are_detected_from_events(self):
        with TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            events = workspace / "events.jsonl"
            events.write_text(
                '{"type":"item.completed"}\n'
                '{"type":"error","message":"Selected model is at capacity. Please try a different model."}\n'
                '{"type":"turn.failed","error":{"message":"Selected model is at capacity."}}\n',
                encoding="utf-8",
            )
            self.assertIn("at capacity", visualize_paper.transient_failure(workspace))
            events.write_text('{"type":"turn.failed","error":{"message":"validation failed"}}\n', encoding="utf-8")
            self.assertIsNone(visualize_paper.transient_failure(workspace))
            self.assertIn("# Resumed run", visualize_paper._resume_prompt("BASE"))

    def test_repair_round_is_triggered_by_blocking_reviews(self):
        clean = {"annotations_review": {"accuracy": "accurate", "findings": []}, "widget_reviews": [
            {"id": "w", "fidelity": "minor_gaps", "interaction_quality": "works", "summary": "s", "findings": ["f"], "blocking_gaps": []},
        ]}
        self.assertFalse(visualize_paper.needs_repair(clean))
        blocked = {"annotations_review": {"accuracy": "not_applicable", "findings": []}, "widget_reviews": [
            {"id": "w", "fidelity": "well_supported", "interaction_quality": "major_issues", "summary": "s", "findings": [], "blocking_gaps": ["Add undo."]},
        ]}
        self.assertTrue(visualize_paper.needs_repair(blocked))
        self.assertTrue(visualize_paper.needs_repair({"annotations_review": {"accuracy": "major_issues", "findings": []}, "widget_reviews": []}))
        prompt = visualize_paper._repair_prompt("BASE", blocked, "# Critique\n\nDetails.")
        self.assertIn("# Repair round", prompt)
        self.assertIn("BLOCKING: Add undo.", prompt)
        self.assertIn("Details.", prompt)

    def test_reviewer_inherits_each_unspecified_model_setting(self):
        inherited = visualize_paper._inherit_review_options(
            codex_cli.ModelOptions("primary", "xhigh", True),
            codex_cli.ModelOptions("critic", None, False),
        )
        self.assertEqual((inherited.model, inherited.reasoning_effort, inherited.fast), ("critic", "xhigh", True))


def sample_expectations() -> dict:
    return {
        "anchors": ["default", "proof-1"],
        "annotations_required": True,
        "document_ids": {"thm:main": "theorem", "lem:one": "lemma", "proof-1": "proof", "par-1": "paragraph", "par-2": "paragraph", "par-3": "paragraph", "sec:intro": "section"},
        "proof_paragraphs": {"proof-1": ["par-2", "par-3"]},
        "result_schema": json.loads((PROJECT_ROOT / "schemas" / "visualization-result.schema.json").read_text(encoding="utf-8")),
    }


def write_widget(workspace: Path, widget_id: str, anchor: str, kind: str, steps=None, script=None) -> dict:
    directory = workspace / "output" / "widgets" / widget_id
    directory.mkdir(parents=True, exist_ok=True)
    manifest = {"id": widget_id, "anchor": anchor, "kind": kind, "title": "T", "summary": "S"}
    if steps is not None:
        manifest["steps"] = steps
    (directory / "widget.json").write_text(json.dumps(manifest), encoding="utf-8")
    (directory / "widget.js").write_text(
        script if script is not None else f'LooseEnds.registerWidget("{widget_id}", function (c, api) {{ return {{ setStep() {{}} }}; }});',
        encoding="utf-8",
    )
    return {
        "id": widget_id, "anchor": anchor, "kind": kind, "title": "T", "summary": "S", "limitations": [],
        "files": [f"output/widgets/{widget_id}/widget.json", f"output/widgets/{widget_id}/widget.js"],
    }


def sample_result(widgets: list) -> dict:
    return {
        "status": "complete", "summary": "Done.", "annotations_updated": True, "widgets": widgets,
        "verification_checks": [{"name": "syntax", "method": "node --check", "result": "passed", "details": "ok"}],
        "warnings": [],
    }


class VisualizationValidationTests(unittest.TestCase):
    def test_accepts_annotations_and_widgets_matching_the_request(self):
        with TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            (workspace / "output").mkdir()
            (workspace / "output" / "annotations.json").write_text(json.dumps({
                "main_result": "thm:main",
                "glossary": [{"id": "double", "term": "double", "forms": ["doubles"], "latex_forms": ["\\mathcal D(P)"], "kind": "definition", "anchor": "par-1", "gloss": "The double."}],
                "proof_outlines": {"proof-1": [{"title": "Step", "paragraphs": ["par-2"]}, {"title": "Step 2", "paragraphs": ["par-3"], "note": "n"}]},
            }), encoding="utf-8")
            widgets = [
                write_widget(workspace, "thm-main", "thm:main", "statement"),
                write_widget(workspace, "proof-1", "proof-1", "proof", steps=[{"title": "A", "paragraphs": ["par-2", "par-3"]}]),
            ]
            (workspace / "agent-result.json").write_text(json.dumps(sample_result(widgets)), encoding="utf-8")
            report = visualization_validation.validate(workspace=workspace, expectations=sample_expectations())
        self.assertEqual([issue.render() for issue in report.issues], [])
        self.assertTrue(report.valid)

    def test_rejects_bad_anchors_ids_steps_and_unsafe_scripts(self):
        with TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            (workspace / "output").mkdir()
            (workspace / "output" / "annotations.json").write_text(json.dumps({
                "main_result": "par-1",
                "glossary": [{"id": "x", "term": "x", "anchor": "nowhere", "gloss": "g", "extra": 1}],
                "proof_outlines": {"proof-1": [{"title": "A", "paragraphs": ["par-3"]}, {"title": "B", "paragraphs": ["par-2", "par-9"]}]},
            }), encoding="utf-8")
            widgets = [
                write_widget(workspace, "wrong-id", "thm:main", "statement"),
                write_widget(workspace, "proof-1", "proof-1", "statement", script='LooseEnds.registerWidget("proof-1", () => { fetch("https://example.com"); });'),
                write_widget(workspace, "lem-one", "lem:one", "statement"),
            ]
            (workspace / "agent-result.json").write_text(json.dumps(sample_result(widgets)), encoding="utf-8")
            report = visualization_validation.validate(workspace=workspace, expectations=sample_expectations())
        rendered = "\n".join(issue.render() for issue in report.issues)
        self.assertIn("main_result must name a statement id", rendered)
        self.assertIn("glossary anchor 'nowhere'", rendered)
        self.assertIn("unknown glossary field 'extra'", rendered)
        self.assertIn("out of reading order", rendered)
        self.assertIn("does not belong to this proof", rendered)
        self.assertIn("must be 'thm-main'", rendered)
        self.assertIn("anchored to a proof must have kind 'proof'", rendered)
        self.assertIn("must not use remote URL", rendered)
        self.assertIn("widget for lem:one was not requested", rendered)

    def test_missing_annotations_are_an_error_when_requested(self):
        with TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            result = sample_result([])
            result["annotations_updated"] = False
            (workspace / "agent-result.json").write_text(json.dumps(result), encoding="utf-8")
            report = visualization_validation.validate(workspace=workspace, expectations=sample_expectations())
        rendered = "\n".join(issue.render() for issue in report.issues)
        self.assertIn("must write output/annotations.json", rendered)
        self.assertIn("requested anchor proof-1 has no widget", rendered)

    def test_review_requires_every_widget_and_blocking_gaps(self):
        schema = json.loads((PROJECT_ROOT / "schemas" / "visualization-review.schema.json").read_text(encoding="utf-8"))
        with TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            (workspace / "critique.md").write_text("# Review\n\nText.", encoding="utf-8")
            (workspace / "agent-result.json").write_text(json.dumps({
                "summary": "ok",
                "annotations_review": {"accuracy": "not_applicable", "findings": []},
                "widget_reviews": [{"id": "thm-main", "fidelity": "major_gaps", "interaction_quality": "works", "summary": "s", "findings": [], "blocking_gaps": []}],
                "warnings": [],
            }), encoding="utf-8")
            report = review_validation.validate(
                workspace=workspace,
                expectations={"widget_ids": ["thm-main", "proof-1"], "annotations_present": True, "result_schema": schema},
            )
        rendered = "\n".join(issue.render() for issue in report.issues)
        self.assertIn("missing review for widget proof-1", rendered)
        self.assertIn("must list blocking_gaps", rendered)
        self.assertIn("annotations were generated and must be reviewed", rendered)

    def test_prompts_describe_the_reader_contract(self):
        author = (PROJECT_ROOT / "prompts" / "visualize-paper.md").read_text(encoding="utf-8")
        reviewer = (PROJECT_ROOT / "prompts" / "review-visualization.md").read_text(encoding="utf-8")
        api = (PROJECT_ROOT / "prompts" / "visualization-widget-api.md").read_text(encoding="utf-8")
        for text in ("inputs/request.json", "output/annotations.json", "WIDGET-API.md", "main_result", "proof_outlines"):
            self.assertIn(text, author)
        for text in ("inputs/generated", "blocking_gaps", "not_applicable", "critique.md"):
            self.assertIn(text, reviewer)
        for text in ("LooseEnds.registerWidget", "setStep", "api.katex.render", "widget.json"):
            self.assertIn(text, api)


if __name__ == "__main__":
    unittest.main()
