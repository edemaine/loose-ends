import json
import multiprocessing
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from validation import common as validation_common
from validation import visualization as visualization_validation
from validation import visualization_review as review_validation
import codex_cli
import explain_note
import fix_widget
import open_problem_common as common
import paper_document
import visualize_paper
import visualizations
import visualization_contract


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


def sample_expectations() -> dict:
    return {
        "anchors": ["default", "proof-1"],
        "annotations_required": True,
        "document_ids": {"thm:main": "theorem", "lem:one": "lemma", "proof-1": "proof", "par-1": "paragraph", "par-2": "paragraph", "par-3": "paragraph", "sec:intro": "section"},
        "proof_paragraphs": {"proof-1": ["par-2", "par-3"]},
        "paragraph_text": {"par-2": "Choose a side $s_0$ and reflect the polygon across it.", "par-3": "Then every vertex stays in the lattice."},
        "note_ids": ["note-001"],
        "result_schema": json.loads((PROJECT_ROOT / "schemas" / "visualization-result.schema.json").read_text(encoding="utf-8")),
    }


def write_widget(workspace: Path, widget_id: str = "thm-main", anchor: str = "thm:main", kind: str = "statement", steps=None, script=None, examples=None) -> dict:
    directory = workspace / "output" / "widgets" / widget_id
    directory.mkdir(parents=True, exist_ok=True)
    manifest = {"id": widget_id, "anchor": anchor, "kind": kind, "title": "T", "summary": "S"}
    if steps is not None:
        manifest["steps"] = steps
    if examples is not None:
        manifest["examples"] = examples
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


def install(source, workspace, widgets=(), manifest=None):
    result = {"widgets": list(widgets), "annotations_updated": True, "notes_addressed": ["note-001"]}
    common.write_json(workspace / "agent-result.json", result)
    return visualizations.install_run(
        source.package, manifest or {}, [], workspace, result, None, None,
        provenance={"codex_version": "test"}, document_digest="digest-1",
    )


def paused_note_completion(package, loaded, release):
    original = visualizations.load_notes

    def read_then_pause(directory):
        snapshot = original(directory)
        loaded.set()
        if not release.wait(15):
            raise RuntimeError("test did not release note completion")
        return snapshot

    with patch.object(visualizations, "load_notes", read_then_pause):
        visualizations.mark_notes_addressed(Path(package), ["note-001"], "quick")


def add_note_worker(package, started, done):
    started.set()
    visualizations.add_note(Path(package), {"anchor": "par-2", "quote": "second"})
    done.set()


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

    def test_inputs_and_graphics_stay_inside_the_source_tree(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "secret.tex").write_text("SECRET TOP", encoding="utf-8")
            source = root / "draft-001"
            source.mkdir()
            (source / "ok.tex").write_text("Included text.", encoding="utf-8")
            (source / "main.tex").write_text(
                "\\documentclass{article}\\begin{document}\\section{A}\\input{ok}\\input{../secret}"
                "\\input{/etc/hostname}\\begin{figure}\\includegraphics{../secret}\\caption{c}\\end{figure}\\end{document}",
                encoding="utf-8",
            )
            document = paper_document.build_document(source, source / "visualization", render_figures_enabled=True, pdflatex="pdflatex-missing", pdftocairo="pdftocairo-missing")
            html = (source / "visualization" / "document.html").read_text(encoding="utf-8")
        self.assertIn("Included text.", html)
        self.assertNotIn("SECRET TOP", html)
        joined = " ".join(document["warnings"])
        self.assertIn("outside the source tree: ../secret", joined)
        self.assertIn("outside the source tree: /etc/hostname", joined)
        self.assertIn("figure graphic not found: ../secret", joined)

    def test_footnotes_and_equation_arguments_survive_conversion(self):
        with TemporaryDirectory() as temporary:
            source = Path(temporary) / "draft-001"
            source.mkdir()
            (source / "main.tex").write_text(
                "\\documentclass{article}\\usepackage{amsmath}\\begin{document}\\section{A}\n"
                "Text with a note\\footnote{The note has \\emph{emphasis} and $x^2$.} here.\n"
                "\\begin{equation}{x}+y=z\\end{equation}\n"
                "Before: \\eqref{eq:a} and \\eqref{eq:c}.\n"
                "\\begin{align}a&=b\\label{eq:a}\\\\c&=d\\label{eq:c}\\end{align} See \\eqref{eq:c}.\n"
                "\\end{document}",
                encoding="utf-8",
            )
            document = paper_document.build_document(source, source / "visualization", render_figures_enabled=False)
            html = (source / "visualization" / "document.html").read_text(encoding="utf-8")
        self.assertIn('class="footnote-ref"', html)
        self.assertIn("<em>emphasis</em>", html)
        self.assertIn("{x}+y=z", html)
        self.assertIn('href="#eq:a"', html)  # the second label of the align resolves to the block
        self.assertNotIn('ref-missing', html)
        self.assertTrue(any("numbered as one equation" in warning for warning in document["warnings"]))

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


class BibliographyConfinementTests(unittest.TestCase):
    def test_rejects_traversal_and_absolute_paths(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            secret = root / "private.bib"
            secret.write_text("@article{secret, title={SYNTHETIC PRIVATE TITLE}}", encoding="utf-8")
            for name in ("../private", "../private.bib", secret.as_posix()):
                with self.subTest(name=name):
                    pre = paper_document.Preprocessor({})
                    pre._extract_bibliography(r"\bibliography{" + name + "}", source)
                    self.assertEqual(pre.bib_items, [])
                    self.assertTrue(any("outside source tree" in warning for warning in pre.warnings))

    def test_allows_bibliography_in_source_root_from_nested_main(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            nested = root / "chapters"
            nested.mkdir()
            (root / "refs.bib").write_text("@article{ok, title={Public title}}", encoding="utf-8")
            pre = paper_document.Preprocessor({})
            pre.run(r"\bibliography{../refs}", "", nested, None, None, root=root)
            self.assertEqual(pre.bib_items[0][0], "ok")
            self.assertEqual(pre.warnings, [])

    def test_local_bbl_fallback(self):
        with TemporaryDirectory() as temporary:
            source = Path(temporary)
            (source / "other.bbl").write_text(r"\begin{thebibliography}{9}\bibitem{ok}Public title\end{thebibliography}", encoding="utf-8")
            pre = paper_document.Preprocessor({})
            pre._extract_bibliography("", source)
            self.assertEqual(pre.bib_items[0][0], "ok")

    def test_rejects_symlinked_bib_and_bbl(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            outside = root / "outside"
            for filename in ("refs.bib", "main.bbl", "other.bbl"):
                with self.subTest(filename=filename):
                    outside.write_text(
                        "@article{secret, title={Private title}}" if filename.endswith(".bib") else
                        r"\begin{thebibliography}{9}\bibitem{secret}Private title\end{thebibliography}",
                        encoding="utf-8",
                    )
                    link = source / filename
                    try:
                        link.symlink_to(outside)
                    except OSError as exc:
                        self.skipTest(f"symlinks unavailable: {exc}")
                    pre = paper_document.Preprocessor({})
                    pre._extract_bibliography(r"\bibliography{refs}", source)
                    self.assertEqual(pre.bib_items, [])
                    self.assertTrue(any("outside source tree" in warning for warning in pre.warnings))
                    link.unlink()


class EquationReferenceTests(unittest.TestCase):
    def test_all_equation_aliases_resolve_before_and_after_the_block(self):
        def reference(label):
            return {"t": "Link", "c": [["", [], [["reference", label], ["reference-type", "eqref"]]], [], ["#" + label, ""]]}

        refs = {"t": "Para", "c": [reference("eq:a"), {"t": "Space"}, reference("eq:b")]}
        ast = {"blocks": [refs, {"t": "Para", "c": [{"t": "Math", "c": [{"t": "DisplayMath"}, r"\LEeq{eq:a|eq:b}a=b"]}]}, refs]}
        pre = paper_document.Preprocessor({})
        builder = paper_document.Builder(pre, {}, {}, {}, [])
        root = builder.build(ast)
        self.assertEqual(builder.labels["eq:b"]["id"], "eq:a")
        html = paper_document.Renderer(builder, pre).blocks(root.children)
        self.assertNotIn("ref-missing", html)
        self.assertEqual(html.count('href="#eq:a"'), 4)


class QuickAnswerTests(unittest.TestCase):
    def sample_document(self):
        return {
            "title": "Flat Doubles",
            "macros": {"\\D": "\\mathcal D"},
            "statements": [{"id": "lem:one", "kind": "lemma", "label": "Lemma 2.2", "title": "Reflection", "text": "Reflection preserves the lattice.", "paragraphs": ["par-5"], "proofs": ["proof-1"]}],
            "proofs": [{"id": "proof-1", "title": "Proof", "of": "lem:one", "paragraphs": ["par-6", "par-7"]}],
            "paragraphs": [
                {"id": "par-5", "text": "Reflection preserves the lattice.", "container": "lem:one"},
                {"id": "par-6", "text": "The exterior turn at the corner is a multiple of $\\pi/q$.", "container": "proof-1"},
                {"id": "par-7", "text": "Hence every side direction is a multiple too.", "container": "proof-1"},
            ],
        }

    def test_context_and_prompt_stay_small_and_specific(self):
        document = self.sample_document()
        annotations = {"glossary": [{"term": "lattice", "gloss": "The set $\\mathbb Z^2$."}]}
        note = {"id": "note-001", "anchor": "par-6", "quote": "exterior turn at the corner", "message": "why a multiple?"}
        context = explain_note.build_context(document, annotations, note)
        self.assertEqual(context["container"]["id"], "proof-1")
        self.assertEqual(context["statement"]["label"], "Lemma 2.2")
        prompt = explain_note.render_prompt(context)
        self.assertIn("Lemma 2.2", prompt)
        self.assertIn("[par-6] (contains the marked passage)", prompt)
        self.assertIn("why a multiple?", prompt)
        self.assertIn("- lattice:", prompt)
        self.assertLess(len(prompt), 4000)

    def test_apply_answer_stores_a_quick_explanation_and_marks_the_note(self):
        with TemporaryDirectory() as temporary:
            package = Path(temporary)
            visualizations.write_manifest(package, {"annotations": None, "widgets": [], "runs": []})
            note = visualizations.add_note(package, {"anchor": "par-6", "quote": "exterior turn at the corner", "message": ""})
            result = {"title": "Why a multiple", "text": "Because $\\alpha_i = m_i\\pi/q$.", "phrase": "exterior turn", "needs_picture": False}
            explanation = explain_note.apply_answer(package, note, result, "The exterior turn at the corner is a multiple of $\\pi/q$.")
            annotations = json.loads((package / "annotations.json").read_text(encoding="utf-8"))
            manifest = visualizations.load_manifest(package)
            notes = visualizations.load_notes(package)
        self.assertEqual(explanation["id"], "quick-note-001")
        self.assertEqual(explanation["provenance"], "quick")
        self.assertEqual(annotations["explanations"][0]["phrase"], "exterior turn")
        self.assertEqual(annotations["glossary"], [])
        self.assertEqual(manifest["annotations"], "annotations.json")
        self.assertEqual(notes[0]["addressed_run"], "quick-answer")

    def test_sanitize_math_text_repairs_control_characters_and_delimiters(self):
        broken = "Here $\x00$\\mathbb Z u$ means combinations $au+bv$ with \\(a,b\\in\\mathbb Z\\)."
        self.assertEqual(
            explain_note.sanitize_math_text(broken),
            "Here $\\mathbb Z u$ means combinations $au+bv$ with $a,b\\in\\mathbb Z$.",
        )
        self.assertEqual(explain_note.sanitize_math_text("plain $x$ text"), "plain $x$ text")

    def test_revision_replaces_the_previous_quick_answer(self):
        with TemporaryDirectory() as temporary:
            package = Path(temporary)
            visualizations.write_manifest(package, {"annotations": "annotations.json", "widgets": [], "runs": []})
            first = visualizations.add_note(package, {"anchor": "par-6", "quote": "exterior turn", "message": ""})
            explain_note.apply_answer(package, first, {"title": "First try", "text": "Vague.", "phrase": "exterior turn", "needs_picture": False}, "The exterior turn at the corner.")
            follow = visualizations.add_note(package, {"anchor": "par-6", "quote": "exterior turn", "message": "still unclear", "revises": "quick-note-001"})
            self.assertEqual(follow["revises"], "quick-note-001")
            annotations = json.loads((package / "annotations.json").read_text(encoding="utf-8"))
            context = explain_note.build_context(self.sample_document(), annotations, follow)
            self.assertEqual(context["previous_answer"]["title"], "First try")
            self.assertIn("Previous answer (First try): Vague.", explain_note.render_prompt(context))
            explain_note.apply_answer(package, follow, {"title": "Second try", "text": "Precise.", "phrase": "exterior turn", "needs_picture": False}, "The exterior turn at the corner.")
            ids = [entry["id"] for entry in visualizations.load_explanations(package)]
        self.assertEqual(ids, ["quick-note-002"])

    def test_phrase_falls_back_to_the_quote_when_the_model_misquotes(self):
        note = {"id": "n", "anchor": "par-6", "quote": "exterior turn at the corner is", "message": ""}
        self.assertEqual(explain_note._phrase_for({"phrase": "not present"}, note, "The exterior turn at the corner is a multiple."), "exterior turn at the corner is")
        self.assertEqual(explain_note._phrase_for({"phrase": "Restrict $\\T_p$ to $P$ and"}, note, "Restrict $\\mathcal T_p$ to $P$ and $P^*$."), "Restrict to and")


class InstallMergeTests(unittest.TestCase):
    def test_installs_annotations_with_or_without_widgets_and_preserves_live_state(self):
        for with_widget in (False, True):
            with self.subTest(with_widget=with_widget), TemporaryDirectory() as temporary:
                root = Path(temporary)
                source = visualizations.SourceRef("draft", root, root, "Test", (), "test")
                source.package.mkdir()
                workspace = root / "generated"
                (workspace / "output").mkdir(parents=True)
                common.write_json(workspace / "output" / "annotations.json", {"glossary": []})
                note = visualizations.add_note(source.package, {"anchor": "par-1", "quote": "first"})
                second = visualizations.add_note(source.package, {"anchor": "par-2", "quote": "second"})
                explain_note.apply_answer(source.package, second, {"title": "Answer", "text": "Explanation"}, "second")
                visualizations.write_manifest(source.package, {"widgets": [], "runs": [], "live_field": "preserve"})
                widgets = []
                if with_widget:
                    widget = write_widget(workspace)
                    widgets.append(widget)
                with patch.object(common, "report_artifacts"):
                    installed = install(source, workspace, widgets, manifest={"outdated": True})
                self.assertTrue((installed / "agent-result.json").is_file())
                annotations = common.load_json(source.package / "annotations.json")
                self.assertEqual(annotations["document_digest"], "digest-1")
                self.assertEqual(annotations["schema_version"], 1)
                self.assertEqual(annotations["explanations"][0]["note"], second["id"])
                self.assertEqual(visualizations.find_note(source.package, note["id"])["addressed_run"], "run-001")
                self.assertEqual(visualizations.load_manifest(source.package)["live_field"], "preserve")
                if with_widget:
                    directory = source.package / "widgets" / "thm-main"
                    stamped = common.load_json(directory / "widget.json")
                    self.assertEqual(stamped["document_digest"], "digest-1")
                    self.assertEqual(stamped["run"], "run-001")
                    self.assertEqual(fix_widget.check_widget(directory, "thm-main", widget), [])

    def test_failed_stamping_does_not_replace_live_widgets_or_install_a_run(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = visualizations.SourceRef("draft", root, root, "Test", (), "test")
            source.package.mkdir()
            workspace = root / "generated"
            widget = write_widget(workspace)
            destination = source.package / "widgets" / "thm-main"
            destination.mkdir(parents=True)
            (destination / "widget.js").write_text("old widget", encoding="utf-8")
            with patch.object(visualizations, "stamp_widget_files", side_effect=OSError("stamp failed")):
                with self.assertRaisesRegex(common.CodexError, "stamp failed"):
                    install(source, workspace, [widget])
            self.assertEqual((destination / "widget.js").read_text(encoding="utf-8"), "old widget")
            self.assertFalse((source.package / "runs" / "run-001").exists())

    def test_invalid_annotations_fail_before_replacing_live_widget(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = visualizations.SourceRef("draft", root, root, "Test", (), "test")
            source.package.mkdir()
            workspace = root / "generated"
            widget = write_widget(workspace)
            (workspace / "output" / "annotations.json").write_text("not JSON", encoding="utf-8")
            with self.assertRaises(common.CodexError):
                install(source, workspace, [widget])
            self.assertFalse((source.package / "widgets" / "thm-main").exists())
            self.assertFalse((source.package / "runs" / "run-001").exists())

    def test_quick_answers_survive_a_full_run_unless_addressed(self):
        live = {"glossary": [], "explanations": [
            {"id": "quick-note-001", "note": "note-001", "provenance": "quick", "anchor": "par-1", "phrase": "a", "text": "t"},
            {"id": "quick-note-002", "note": "note-002", "provenance": "quick", "anchor": "par-2", "phrase": "b", "text": "t"},
            {"id": "quick-note-003", "note": "note-003", "provenance": "quick", "anchor": "par-3", "phrase": "c", "text": "t"},
            {"id": "old", "anchor": "par-4", "phrase": "d", "text": "t"},
        ]}
        generated = {"glossary": [{"id": "g", "term": "g", "anchor": "par-1", "gloss": "x"}], "explanations": [
            {"id": "turn", "note": "note-002", "anchor": "par-2", "phrase": "b", "text": "reviewed"},
        ]}
        merged = visualizations.merge_live_annotations(live, generated, addressed=["note-003"])
        self.assertEqual([entry["id"] for entry in merged["explanations"]], ["turn", "quick-note-001"])
        self.assertEqual(merged["glossary"][0]["id"], "g")
        self.assertEqual(visualizations.merge_live_annotations(None, generated, addressed=[]), generated)

    def test_stamps_and_widget_records_read_widget_json(self):
        with TemporaryDirectory() as temporary:
            package = Path(temporary)
            widget = package / "widgets" / "thm-main"
            widget.mkdir(parents=True)
            (widget / "widget.json").write_text(json.dumps({"id": "thm-main", "anchor": "thm:main", "kind": "statement", "title": "Fresh title", "summary": "s"}), encoding="utf-8")
            (widget / "widget.js").write_text("x", encoding="utf-8")
            (widget / "review.json").write_text(json.dumps({"fidelity": "well_supported", "interaction_quality": "works"}), encoding="utf-8")
            visualizations.stamp_widget_files(widget, "digest-1", "run-002")
            records = visualizations.widget_records(package, {"widgets": [{"id": "thm-main", "anchor": "thm:main", "title": "Stale title"}]})
            manifest = json.loads((widget / "widget.json").read_text(encoding="utf-8"))
            review = json.loads((widget / "review.json").read_text(encoding="utf-8"))
        self.assertEqual(records[0]["title"], "Fresh title")
        self.assertEqual(records[0]["review"]["fidelity"], "well_supported")
        self.assertEqual((manifest["schema_version"], manifest["api_version"], manifest["document_digest"], manifest["run"]), (1, 1, "digest-1", "run-002"))
        self.assertEqual((review["schema_version"], review["document_digest"]), (1, "digest-1"))


class QuickFixTests(unittest.TestCase):
    def test_package_store_installs_a_quick_fix_and_updates_provenance(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            widget = write_widget(root)
            package = root / "output"
            directory = package / "widgets" / widget["id"]
            previous_script = (directory / "widget.js").read_text(encoding="utf-8")
            previous_review = {"fidelity": "well_supported", "summary": "Reviewed original"}
            common.write_json(directory / "review.json", previous_review)
            visualizations.write_manifest(package, {"widgets": [widget], "runs": []})
            note = visualizations.add_note(package, {"anchor": "thm:main", "quote": "fix", "widget": widget["id"]})
            write_widget(root / "edit", script='LooseEnds.registerWidget("thm-main", () => ({fixed: true}));')
            edited = root / "edit" / "output" / "widgets" / widget["id"]
            metadata = common.load_json(edited / "widget.json")
            metadata["title"] = "Fixed title"
            common.write_json(edited / "widget.json", metadata)

            visualizations.install_quick_fix(
                package, widget["id"], edited, note_id=note["id"],
                summary="Fixed display", document_digest="digest-1", run_name="quick-fix",
            )

            self.assertEqual((directory / "widget.js").read_text(encoding="utf-8"), (edited / "widget.js").read_text(encoding="utf-8"))
            archives = list((package / "runs" / "quick-fixes").iterdir())
            self.assertEqual(len(archives), 1)
            self.assertEqual((archives[0] / "widget.js").read_text(encoding="utf-8"), previous_script)
            review = common.load_json(directory / "review.json")
            self.assertEqual(review["previous_review"], previous_review)
            self.assertEqual(review["fidelity"], "unreviewed")
            self.assertEqual(review["document_digest"], "digest-1")
            installed = visualizations.load_manifest(package)["widgets"][0]
            self.assertEqual((installed["title"], installed["quick_fixes"]), ("Fixed title", 1))
            self.assertEqual(visualizations.find_note(package, note["id"])["addressed_run"], "quick-fix")

    def test_widget_notes_require_an_installed_widget(self):
        with TemporaryDirectory() as temporary:
            package = Path(temporary)
            with self.assertRaises(ValueError):
                visualizations.add_note(package, {"anchor": "thm:main", "quote": "T", "message": "grid moves", "widget": "thm-main"})
            (package / "widgets" / "thm-main").mkdir(parents=True)
            (package / "widgets" / "thm-main" / "widget.json").write_text(json.dumps({"id": "thm-main", "anchor": "thm:main", "kind": "statement"}), encoding="utf-8")
            note = visualizations.add_note(package, {"anchor": "thm:main", "quote": "T", "message": "grid moves", "widget": "thm-main"})
            self.assertEqual(note["widget"], "thm-main")

    def test_check_widget_rejects_unsafe_or_renamed_widgets(self):
        with TemporaryDirectory() as temporary:
            directory = Path(temporary)
            original = {"id": "thm-main", "anchor": "thm:main", "kind": "statement"}
            (directory / "widget.json").write_text(json.dumps({**original, "anchor": "lem:one"}), encoding="utf-8")
            (directory / "widget.js").write_text('LooseEnds.registerWidget("thm-main", () => ({})); fetch("https://x");', encoding="utf-8")
            problems = fix_widget.check_widget(directory, "thm-main", original)
        self.assertTrue(any("anchor must not change" in item for item in problems))
        self.assertTrue(any("remote URL" in item for item in problems))
        # The full run validator now applies: invalid steps are rejected too.
        with TemporaryDirectory() as temporary:
            directory = Path(temporary)
            original = {"id": "proof-1", "anchor": "proof-1", "kind": "proof"}
            (directory / "widget.json").write_text(json.dumps({**original, "title": "T", "summary": "S", "examples": [{"id": "generic", "label": "G"}], "steps": [{"title": "A", "paragraphs": ["par-99"]}]}), encoding="utf-8")
            (directory / "widget.js").write_text('LooseEnds.registerWidget("proof-1", () => ({}));', encoding="utf-8")
            document = {"statements": [], "proofs": [{"id": "proof-1", "paragraphs": ["par-2", "par-3"]}], "paragraphs": [{"id": "par-2", "text": "a"}, {"id": "par-3", "text": "b"}], "sections": [], "figures": [], "equations": []}
            problems = fix_widget.check_widget(directory, "proof-1", original, document)
        self.assertTrue(any("does not belong to this proof" in item for item in problems), problems)
        prompt = fix_widget.render_prompt(original | {"title": "Tester"}, {"message": "The grid moves on click."}, "Flat Doubles")
        self.assertIn("The grid moves on click.", prompt)
        self.assertIn("smallest change", prompt)
        widget = original | {"title": "Tester", "steps": [{"title": "Reflect $P$", "paragraphs": ["par-48"], "phrase": "Put"}, {"title": "Match", "paragraphs": ["par-50"]}]}
        prompt = fix_widget.render_prompt(widget, {"message": "The label is missing.", "step": 1}, "Flat Doubles", {"message": "Add a label", "outcome": "Added P^* label"})
        self.assertIn("step 2 of 2", prompt)
        self.assertIn("\"Match\"", prompt)
        self.assertIn("Added P^* label", prompt)

    def test_notes_record_steps_follow_ups_and_outcomes(self):
        with TemporaryDirectory() as temporary:
            package = Path(temporary)
            (package / "widgets" / "proof-2").mkdir(parents=True)
            (package / "widgets" / "proof-2" / "widget.json").write_text(json.dumps({"id": "proof-2", "anchor": "proof-2", "kind": "proof"}), encoding="utf-8")
            first = visualizations.add_note(package, {"anchor": "proof-2", "quote": "W", "message": "missing label", "widget": "proof-2", "step": 3, "step_title": "Match"})
            visualizations.mark_notes_addressed(package, [first["id"]], "quick-fix", outcome="Added the label")
            second = visualizations.add_note(package, {"anchor": "proof-2", "quote": "W", "message": "still missing", "widget": "proof-2", "step": 3, "follows": first["id"]})
            stored = visualizations.load_notes(package)
        self.assertEqual((stored[0]["step"], stored[0]["outcome"]), (3, "Added the label"))
        self.assertEqual(second["follows"], "note-001")
        with TemporaryDirectory() as temporary:
            with self.assertRaises(ValueError):
                visualizations.add_note(Path(temporary), {"anchor": "p", "quote": "q", "follows": "nope"})


class PackageConcurrencyTests(unittest.TestCase):
    def test_full_install_cannot_overwrite_an_answer_finishing_during_merge(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = visualizations.SourceRef("draft", root, root, "Test", (), "test")
            package = source.package
            package.mkdir()
            workspace = root / "generated"
            (workspace / "output").mkdir(parents=True)
            common.write_json(workspace / "output" / "annotations.json", {"glossary": []})
            visualizations.add_note(package, {"anchor": "par-1", "quote": "first"})
            note = visualizations.add_note(package, {"anchor": "par-2", "quote": "second"})
            loaded, release, started, done = [threading.Event() for _ in range(4)]
            failures = []
            original_merge = visualizations.merge_live_annotations

            def pause_merge(*args, **kwargs):
                merged = original_merge(*args, **kwargs)
                loaded.set()
                if not release.wait(10):
                    raise RuntimeError("test did not release installation")
                return merged

            def full_run():
                try:
                    install(source, workspace)
                except Exception as exc:
                    failures.append(exc)

            def answer():
                try:
                    started.set()
                    explain_note.apply_answer(package=package, note=note, result={"title": "Answer", "text": "Explanation"}, passage="second")
                    done.set()
                except Exception as exc:
                    failures.append(exc)

            with patch.object(visualizations, "merge_live_annotations", pause_merge), patch.object(common, "report_artifacts"):
                first = threading.Thread(target=full_run)
                second = threading.Thread(target=answer)
                first.start()
                try:
                    self.assertTrue(loaded.wait(5))
                    second.start()
                    self.assertTrue(started.wait(5))
                    self.assertFalse(done.wait(0.25), "quick answer bypassed the installation lock")
                finally:
                    release.set()
                    first.join(10)
                    if second.ident is not None:
                        second.join(10)
            self.assertFalse(first.is_alive())
            self.assertFalse(second.is_alive())
            self.assertEqual(failures, [])
            self.assertEqual(visualizations.load_explanations(package)[0]["note"], note["id"])
            self.assertEqual(visualizations.find_note(package, note["id"])["addressed_run"], "quick-answer")
            self.assertEqual(common.load_json(package / "annotations.json")["document_digest"], "digest-1")

    def test_note_completion_cannot_overwrite_a_concurrent_add_in_another_process(self):
        context = multiprocessing.get_context("spawn")
        with TemporaryDirectory() as temporary:
            package = Path(temporary)
            visualizations.add_note(package, {"anchor": "par-1", "quote": "first"})
            loaded, release, started, done = [context.Event() for _ in range(4)]
            completing = context.Process(target=paused_note_completion, args=(temporary, loaded, release))
            adding = context.Process(target=add_note_worker, args=(temporary, started, done))
            completing.start()
            try:
                self.assertTrue(loaded.wait(10))
                adding.start()
                self.assertTrue(started.wait(10))
                self.assertFalse(done.wait(0.25), "addition bypassed the worker's package lock")
            finally:
                release.set()
                for process in (completing, adding):
                    if process.pid is not None:
                        process.join(10)
                        if process.is_alive():
                            process.terminate()
                            process.join(5)
            self.assertEqual(completing.exitcode, 0)
            self.assertEqual(adding.exitcode, 0)
            notes = visualizations.load_notes(package)
            self.assertEqual([note["quote"] for note in notes], ["first", "second"])
            self.assertEqual(notes[0]["addressed_run"], "quick")

    def test_concurrent_quick_answers_preserve_both_explanations(self):
        with TemporaryDirectory() as temporary:
            package = Path(temporary)
            notes = [visualizations.add_note(package, {"anchor": "par-1", "quote": quote}) for quote in ("first", "second")]
            loaded, release, started, done = [threading.Event() for _ in range(4)]
            failures = []
            original = common.load_json

            def read_then_pause(path):
                snapshot = original(path)
                if path.name == "annotations.json" and threading.current_thread().name == "first-answer":
                    loaded.set()
                    if not release.wait(10):
                        raise RuntimeError("test did not release answer")
                return snapshot

            def answer(note):
                try:
                    if note == notes[1]:
                        started.set()
                    explain_note.apply_answer(package, note, {"title": "Answer", "text": "Explanation"}, note["quote"])
                    if note == notes[1]:
                        done.set()
                except Exception as exc:
                    failures.append(exc)

            with patch.object(common, "load_json", read_then_pause):
                first = threading.Thread(target=answer, args=(notes[0],), name="first-answer")
                second = threading.Thread(target=answer, args=(notes[1],))
                first.start()
                try:
                    self.assertTrue(loaded.wait(5))
                    second.start()
                    self.assertTrue(started.wait(5))
                    self.assertFalse(done.wait(0.25))
                finally:
                    release.set()
                    first.join(10)
                    if second.ident is not None:
                        second.join(10)
            self.assertFalse(first.is_alive())
            self.assertFalse(second.is_alive())
            self.assertEqual(failures, [])
            self.assertEqual({entry["note"] for entry in visualizations.load_explanations(package)}, {note["id"] for note in notes})
            self.assertTrue(all(note["addressed_run"] == "quick-answer" for note in visualizations.load_notes(package)))

    def test_package_lock_is_reentrant_and_released_after_an_exception(self):
        with TemporaryDirectory() as temporary:
            package = Path(temporary)
            with self.assertRaisesRegex(ValueError, "test error"):
                with visualizations.package_lock(package):
                    visualizations.add_note(package, {"anchor": "par-1", "quote": "first"})
                    raise ValueError("test error")
            visualizations.add_note(package, {"anchor": "par-2", "quote": "second"})
            self.assertEqual(len(visualizations.load_notes(package)), 2)


class ReaderNoteTests(unittest.TestCase):
    def test_notes_are_added_removed_and_marked_addressed(self):
        with TemporaryDirectory() as temporary:
            package = Path(temporary)
            first = visualizations.add_note(package, {"anchor": "par-48", "quote": "choose a side", "message": "why this side?"})
            second = visualizations.add_note(package, {"anchor": "par-50", "quote": "Λ3", "message": "", "latex": "\\Lambda_3"})
            self.assertEqual([first["id"], second["id"]], ["note-001", "note-002"])
            self.assertEqual(second["latex"], "\\Lambda_3")
            (package / "annotations.json").write_text(json.dumps({"glossary": [], "explanations": [
                {"id": "quick-note-002", "anchor": "par-50", "phrase": "x", "text": "t", "provenance": "quick", "note": "note-002"},
                {"id": "keep", "anchor": "par-48", "phrase": "choose", "text": "t"},
            ]}), encoding="utf-8")
            self.assertEqual(len(visualizations.open_notes(package)), 2)
            visualizations.mark_notes_addressed(package, ["note-001"], "run-004")
            self.assertEqual([note["id"] for note in visualizations.open_notes(package)], ["note-002"])
            self.assertTrue(visualizations.remove_note(package, "note-002"))
            self.assertFalse(visualizations.remove_note(package, "note-002"))
            self.assertEqual([entry["id"] for entry in visualizations.load_explanations(package)], ["keep"])
            self.assertEqual(visualizations.load_notes(package)[0]["addressed_run"], "run-004")
            with self.assertRaises(ValueError):
                visualizations.add_note(package, {"anchor": "../x", "quote": "q"})
            with self.assertRaises(ValueError):
                visualizations.add_note(package, {"anchor": "par-1", "quote": "  "})


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
            try:
                (package / "link.txt").symlink_to(Path(temporary) / "secret.txt")
            except OSError as exc:  # Windows without symlink privileges
                self.skipTest(f"symbolic links unavailable: {exc}")
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


class VisualizeDriverTests(unittest.TestCase):
    def test_cli_uses_package_discovery_for_dry_run_and_document_only(self):
        with TemporaryDirectory() as temporary:
            source = Path(temporary) / "draft-001"
            source.mkdir()
            (source / "main.tex").write_text(SAMPLE_TEX, encoding="utf-8")
            with patch("builtins.print"):
                self.assertEqual(visualize_paper.main([str(source), "--dry-run"]), 0)
            document = {"sections": [], "statements": [], "proofs": [], "figures": [], "warnings": []}
            with patch.object(visualizations, "ensure_document", return_value=(document, {})) as build, patch("builtins.print"):
                self.assertEqual(visualize_paper.main([str(source), "--document-only"]), 0)
            self.assertEqual(build.call_args.args[0].directory, source.resolve())
            self.assertEqual(build.call_args.kwargs, {"rebuild": True})

    def test_source_from_path_accepts_drafts_and_papers_only(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            draft = root / "draft-003"
            draft.mkdir()
            (draft / "main.tex").write_text(SAMPLE_TEX, encoding="utf-8")
            (draft / "manifest.json").write_text(json.dumps({"title": "T", "authors": ["A"]}), encoding="utf-8")
            source = visualizations.source_from_path(draft)
            self.assertEqual((source.kind, source.title, source.authors), ("draft", "T", ("A",)))
            paper = root / "arXiv-1.2v1"
            (paper / "source").mkdir(parents=True)
            (paper / "metadata.json").write_text(json.dumps({"title": "P", "authors": ["B", "C"]}), encoding="utf-8")
            source = visualizations.source_from_path(paper)
            self.assertEqual((source.kind, source.latex_directory.name), ("paper", "source"))
            with self.assertRaises(codex_cli.CodexError):
                visualizations.source_from_path(root)

    @unittest.skipUnless(HAS_PANDOC, "pandoc is required to convert LaTeX")
    def test_ensure_document_builds_once_and_resolves_anchors(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            draft = root / "draft-001"
            draft.mkdir()
            (draft / "main.tex").write_text(SAMPLE_TEX, encoding="utf-8")
            (draft / "manifest.json").write_text("{}", encoding="utf-8")
            source = visualizations.source_from_path(draft)
            document, manifest = visualizations.ensure_document(source)
            built_at = manifest["document"]["built_at"]
            document_again, manifest_again = visualizations.ensure_document(source)
            self.assertEqual(manifest_again["document"]["built_at"], built_at)
            self.assertEqual(document_again["statements"], document["statements"])
            self.assertEqual(visualize_paper.resolve_anchors(document, ["default", "thm:main", "proof-1", "thm:main"]), ["default", "thm:main", "proof-1"])
            self.assertEqual(visualize_paper.resolve_anchors(document, ["notes"]), ["notes"])
            self.assertTrue(visualize_paper._request(document, ["notes"], manifest)["notes_only"])
            with self.assertRaises(codex_cli.CodexError):
                visualize_paper.resolve_anchors(document, ["par-1"])
            with self.assertRaises(codex_cli.CodexError):
                visualize_paper.resolve_anchors(document, ["nope"])
            visualizations.add_note(source.package, {"anchor": "par-7", "quote": "First paragraph", "message": "Why?"})
            (source.package / "annotations.json").write_text(json.dumps({"glossary": [], "explanations": [{"id": "quick-note-001", "anchor": "par-7", "phrase": "First", "title": "Old", "text": "t", "provenance": "quick", "note": "note-001"}]}), encoding="utf-8")
            visualizations.add_note(source.package, {"anchor": "par-7", "quote": "First paragraph", "message": "Still?", "revises": "quick-note-001"})
            notes = visualize_paper._reader_notes(source, document)
            self.assertEqual(notes[0]["container"], "proof-1")
            self.assertEqual(notes[1]["previous_answer"]["title"], "Old")
            prompt = visualize_paper._render_prompt("TEMPLATE", document, visualize_paper._request(document, ["default", "proof-1"], manifest, notes))
            self.assertIn("note-001", prompt)
            self.assertIn("Reader says", prompt)
            self.assertIn("follows up an earlier explanation (`quick-note-001`", prompt)
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


class VisualizationValidationTests(unittest.TestCase):
    def test_validated_runs_preserve_dependency_staging_and_permissions(self):
        with TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            schema = workspace / "result.schema.json"
            common.write_json(schema, {})
            validator = codex_cli.OutputValidator(
                Path(visualization_validation.__file__),
                lambda **kwargs: validation_common.ValidationReport(result={}), {},
                dependencies=visualize_paper.VALIDATION_DEPENDENCIES,
            )
            with patch.object(codex_cli, "run_structured_codex") as run, patch.object(codex_cli, "grant_sandbox_read_access") as grant:
                report = codex_cli.run_validated_codex(
                    codex="unused", workspace=workspace, prompt="test", schema_path=schema, validator=validator,
                )
            self.assertTrue(report.valid)
            run.assert_called_once()
            dependency = workspace / "visualization_contract.py"
            self.assertTrue(dependency.is_file())
            self.assertIn((dependency,), [call.args for call in grant.call_args_list])

    def test_staged_validators_import_the_shared_contract_without_the_repository(self):
        for validator_module in (visualization_validation, review_validation):
            with self.subTest(validator=validator_module.__name__), TemporaryDirectory() as temporary:
                workspace = Path(temporary)
                validator = codex_cli.OutputValidator(
                    Path(validator_module.__file__), validator_module.validate, {},
                    dependencies=visualize_paper.VALIDATION_DEPENDENCIES,
                )
                codex_cli.stage_output_validator(workspace, validator)
                staged = workspace / "visualization_contract.py"
                self.assertEqual(staged.read_bytes(), Path(visualization_contract.__file__).read_bytes())
                self.assertEqual({path.name for path in workspace.iterdir()}, {"validation", "visualization_contract.py"})
                completed = subprocess.run(
                    [sys.executable, "-E", "-c", "import validation.validate; import visualization_contract; assert 'run' in visualization_contract.WIDGET_FIELDS"],
                    cwd=workspace, capture_output=True, text=True, timeout=20, check=False,
                )
                self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_contract_changes_invalidate_the_configuration_digest(self):
        with TemporaryDirectory() as temporary:
            dependency = Path(temporary) / "visualization_contract.py"
            shutil.copyfile(visualization_contract.__file__, dependency)
            arguments = {
                "validation_source": Path(visualization_validation.__file__),
                "validation_dependencies": (dependency,),
            }
            before = codex_cli.semantic_config_digest("prompt", "{}", codex_cli.ModelOptions(), **arguments)
            dependency.write_text(dependency.read_text(encoding="utf-8").replace("WIDGET_API_VERSION = 1", "WIDGET_API_VERSION = 2"), encoding="utf-8")
            after = codex_cli.semantic_config_digest("prompt", "{}", codex_cli.ModelOptions(), **arguments)
            self.assertNotEqual(before, after)

    def test_stamped_widgets_pass_quick_fix_validation(self):
        with TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            manifest = write_widget(workspace)
            directory = workspace / "output" / "widgets" / manifest["id"]
            self.assertEqual(fix_widget.check_widget(directory, "thm-main", manifest), [])
            visualizations.stamp_widget_files(directory, "digest", "run-001")
            self.assertEqual(fix_widget.check_widget(directory, "thm-main", manifest), [])
            stamped = common.load_json(directory / "widget.json")
            for field, value in (("schema_version", True), ("api_version", 2), ("document_digest", 7), ("run", ""), ("typo", 1)):
                with self.subTest(field=field):
                    common.write_json(directory / "widget.json", {**stamped, field: value})
                    problems = fix_widget.check_widget(directory, "thm-main", manifest)
                    self.assertTrue(any(field in problem for problem in problems), problems)

    def test_annotation_metadata_is_optional_but_checked(self):
        for value in ({}, {"schema_version": 1, "document_digest": "digest"}):
            reporter = validation_common.Reporter()
            visualization_validation.validate_annotations(value, {}, {}, reporter)
            self.assertEqual(reporter.issues, [])
        for field, value in (("schema_version", True), ("schema_version", 2), ("document_digest", []), ("typo", "x")):
            with self.subTest(field=field, value=value):
                reporter = validation_common.Reporter()
                visualization_validation.validate_annotations({field: value}, {}, {}, reporter)
                self.assertTrue(any(field in issue.render() for issue in reporter.issues))

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
                write_widget(workspace, "proof-1", "proof-1", "proof", steps=[
                    {"title": "Choose $s_0$", "paragraphs": ["par-2"], "phrase": "choose a side"},
                    {"title": "Reflect", "paragraphs": ["par-2"], "phrase": "reflect the polygon"},
                    {"title": "Lattice", "paragraphs": ["par-3"]},
                ], examples=[{"id": "generic", "label": "Generic octagon", "note": "8 vertices"}, {"id": "parallel", "label": "Parallel sides"}]),
            ]
            result = sample_result(widgets)
            result["notes_addressed"] = ["note-001"]
            (workspace / "agent-result.json").write_text(json.dumps(result), encoding="utf-8")
            report = visualization_validation.validate(workspace=workspace, expectations=sample_expectations())
        self.assertEqual([issue.render() for issue in report.issues], [])
        self.assertTrue(report.valid)

    def test_background_terms_and_explanations_are_validated(self):
        with TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            (workspace / "output").mkdir()
            (workspace / "output" / "annotations.json").write_text(json.dumps({
                "main_result": "thm:main",
                "glossary": [
                    {"id": "monodromy", "term": "monodromy", "kind": "background", "gloss": "Standard.", "source": "Hatcher, Algebraic Topology"},
                    {"id": "bad", "term": "bad", "gloss": "no anchor and not background"},
                ],
                "explanations": [
                    {"id": "turn", "anchor": "par-2", "phrase": "reflect the polygon", "title": "Why", "text": "Because."},
                    {"id": "missing", "anchor": "par-3", "phrase": "not there", "text": "x"},
                    {"id": "turn", "anchor": "par-9", "phrase": "y", "text": "z", "extra": 1},
                ],
                "proof_outlines": {},
            }), encoding="utf-8")
            widgets = [write_widget(workspace, "thm-main", "thm:main", "statement")]
            expectations = sample_expectations()
            expectations["anchors"] = ["notes"]
            expectations["note_containers"] = ["thm:main"]
            (workspace / "agent-result.json").write_text(json.dumps(sample_result(widgets)), encoding="utf-8")
            report = visualization_validation.validate(workspace=workspace, expectations=expectations)
        rendered = "\n".join(issue.render() for issue in report.issues)
        self.assertIn("needs an anchor unless its kind is 'background'", rendered)
        self.assertIn("does not occur in par-3", rendered)
        self.assertIn("duplicate explanation id turn", rendered)
        self.assertIn("anchor 'par-9' is not an element", rendered)
        self.assertIn("unknown explanation field 'extra'", rendered)
        self.assertNotIn("monodromy", rendered)
        self.assertNotIn("was not requested", rendered)

    def test_phrase_steps_examples_and_notes_are_checked(self):
        with TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            (workspace / "output").mkdir()
            (workspace / "output" / "annotations.json").write_text(json.dumps({"main_result": "thm:main", "glossary": [], "proof_outlines": {}}), encoding="utf-8")
            widgets = [
                write_widget(workspace, "thm-main", "thm:main", "statement"),
                write_widget(workspace, "proof-1", "proof-1", "proof", steps=[
                    {"title": "A", "paragraphs": ["par-2"], "phrase": "not in the text"},
                    {"title": "B", "paragraphs": ["par-2"]},
                ], examples=[{"id": "Bad Id", "label": ""}, {"id": "x", "label": "x", "extra": 1}]),
            ]
            result = sample_result(widgets)
            result["notes_addressed"] = ["note-999"]
            (workspace / "agent-result.json").write_text(json.dumps(result), encoding="utf-8")
            report = visualization_validation.validate(workspace=workspace, expectations=sample_expectations())
        rendered = "\n".join(issue.render() for issue in report.issues)
        self.assertIn("does not occur in the step's paragraphs", rendered)
        self.assertIn("must each name a distinct phrase", rendered)
        self.assertIn("invalid example id 'Bad Id'", rendered)
        self.assertIn("example needs a nonempty label", rendered)
        self.assertIn("unknown example field 'extra'", rendered)
        self.assertIn("unknown reader note 'note-999'", rendered)

    def test_proof_widgets_must_declare_examples(self):
        with TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            (workspace / "output").mkdir()
            (workspace / "output" / "annotations.json").write_text(json.dumps({"main_result": "thm:main", "glossary": [], "proof_outlines": {}}), encoding="utf-8")
            widgets = [
                write_widget(workspace, "thm-main", "thm:main", "statement"),
                write_widget(workspace, "proof-1", "proof-1", "proof", steps=[{"title": "A", "paragraphs": ["par-2", "par-3"]}]),
            ]
            (workspace / "agent-result.json").write_text(json.dumps(sample_result(widgets)), encoding="utf-8")
            report = visualization_validation.validate(workspace=workspace, expectations=sample_expectations())
        self.assertIn("must declare their running examples", "\n".join(issue.render() for issue in report.issues))

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
