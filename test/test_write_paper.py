from pathlib import Path
import json
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import codex_cli
import open_problem_common as common
import write_paper


def make_paper(root: Path, name: str = "arXiv-1234.56789v1") -> Path:
    paper = root / name
    (paper / "source").mkdir(parents=True)
    (paper / "paper.pdf").write_bytes(b"%PDF-test")
    (paper / "source" / "main.tex").write_text(
        "\\section{Results}\nAn open problem.\n",
        encoding="utf-8",
    )
    common.write_json(
        paper / "metadata.json",
        {
            "id": name.removeprefix("arXiv-"),
            "title": "Originating Paper",
            "authors": ["Original Author"],
        },
    )
    analysis = paper / "analysis"
    analysis.mkdir()
    (analysis / "summary.md").write_text("# Summary\n\nSummary.\n", encoding="utf-8")
    (analysis / "results.md").write_text("# Results\n\nLemma.\n", encoding="utf-8")
    (analysis / "open-problems.md").write_text(
        "# Open Problems\n\n## OP-001\n\nProve it.\n\n"
        "## OP-002\n\nDisprove it.\n",
        encoding="utf-8",
    )
    common.write_json(
        analysis / "manifest.json",
        {
            "schema_version": 2,
            "paper_title": "Originating Paper",
            "paper_authors": ["Original Author"],
            "open_problems": [
                {
                    "id": "OP-001",
                    "title": "First problem",
                    "explicitness": "explicit",
                },
                {
                    "id": "OP-002",
                    "title": "Second problem",
                    "explicitness": "explicit",
                },
            ],
        },
    )
    return paper


def make_ready_attempt(paper: Path, problem_id: str, attempt_number: int = 1) -> Path:
    problem = common.discover_problem_refs(
        [paper],
        problem_ids={problem_id},
    )[0]
    problem.directory.mkdir(parents=True, exist_ok=True)
    (problem.directory / common.LITERATURE_MARKDOWN).write_text(
        f"# Literature {problem_id}\n\nNo resolution found.\n",
        encoding="utf-8",
    )
    common.write_json(
        problem.directory / common.LITERATURE_RESULT,
        {
            "problem_id": problem_id,
            "resolution_status": "no_resolution_found",
            "confidence": "high",
            "status_summary": "No resolution found.",
            "exact_match_analysis": "Checked the exact statement.",
            "residual_problem": "The full problem.",
            "solver_briefing": "Use the originating lemma.",
            "sources": [],
            "search_queries": [],
            "warnings": [],
        },
    )
    common.write_json(
        problem.directory / common.LITERATURE_MANIFEST,
        {
            "schema_version": common.LITERATURE_MANIFEST_SCHEMA_VERSION,
            "input_digest": common.literature_input_digest(problem),
        },
    )
    attempt = problem.directory / f"attempt-{attempt_number:03d}"
    attempt.mkdir()
    (attempt / "attempt.md").write_text(
        "# Attempt\n\n## Checkable claims\n\n### C-001\n\nThe problem is solved.\n",
        encoding="utf-8",
    )
    solver_result = {
        "claimed_result_type": "solution",
        "summary": "A complete proof.",
        "external_sources": [],
        "checkable_claims": [
            {
                "id": "C-001",
                "type": "proof",
                "statement": "The problem is solved.",
                "support": "A detailed derivation.",
                "remaining_gap": "",
            }
        ],
        "artifacts": [],
        "warnings": [],
    }
    common.write_json(attempt / "solver-result.json", solver_result)
    common.write_json(attempt / "manifest.json", {"schema_version": 2})
    (attempt / "critique.md").write_text(
        "# Critique\n\nC-001 is supported.\n",
        encoding="utf-8",
    )
    common.write_json(
        attempt / "review-result.json",
        {
            "correctness": "well_supported",
            "reviewed_coverage": "complete",
            "importance": "resolution",
            "verification_confidence": "high",
            "human_priority": "high",
            "summary": "The proof is supported.",
            "claim_reviews": [
                {
                    "claim_id": "C-001",
                    "assessment": "supported",
                    "explanation": "The derivation checks out.",
                }
            ],
            "blocking_gaps": [],
            "recommended_next_steps": ["Write the paper."],
            "warnings": [],
        },
    )
    common.write_json(
        attempt / "review-manifest.json",
        {
            "schema_version": 2,
            "attempt_digest": common.solver_attempt_digest(attempt),
        },
    )
    return attempt


def write_run_files(workspace: Path) -> None:
    (workspace / "events.jsonl").write_text(
        '{"type":"turn.completed"}\n',
        encoding="utf-8",
    )
    (workspace / "run.log").write_text("", encoding="utf-8")


def paper_result(
    result_ids: list[str],
    *,
    addressed: list[dict] | None = None,
) -> dict:
    return {
        "status": "draft_complete",
        "title": "A Short Result",
        "summary": "We solve the selected open problem.",
        "results": [
            {
                "result_id": result_id,
                "disposition": "included_main",
                "source_claim_ids": ["C-001"],
                "manuscript_labels": [f"thm:{result_id.lower()}"],
                "explanation": "The theorem resolves the problem.",
            }
            for result_id in result_ids
        ],
        "citations": [
            {
                "bib_key": "origin",
                "title": "Originating Paper",
                "url": "https://arxiv.org/abs/1234.56789",
                "verification": "The primary source was inspected.",
                "role": "original_problem",
                "result_ids": result_ids,
            }
        ],
        "addressed_findings": addressed or [],
        "unresolved_issues": [],
        "generated_files": [],
        "warnings": [],
    }


def write_manuscript_files(workspace: Path, result_ids: list[str]) -> None:
    labels = "\n".join(
        f"\\begin{{theorem}}\\label{{thm:{result_id.lower()}}}Result."
        "\\end{theorem}"
        for result_id in result_ids
    )
    (workspace / "main.tex").write_text(
        "\\documentclass{article}\n"
        "\\usepackage{amsthm}\n"
        "\\title{A Short Result}\n"
        "\\author{}\n"
        "\\begin{document}\n\\maketitle\n"
        "\\begin{abstract}We solve an open problem.\\end{abstract}\n"
        "\\section{Introduction}The problem was posed in \\cite{origin}.\n"
        f"{labels}\n"
        "\\bibliographystyle{plain}\n\\bibliography{references}\n"
        "\\end{document}\n",
        encoding="utf-8",
    )
    (workspace / "references.bib").write_text(
        "@article{origin, title={Originating Paper}, author={Author}, year={2020}}\n",
        encoding="utf-8",
    )
    (workspace / "readiness.md").write_text(
        "# Readiness\n\n"
        + "\n".join(f"## {result_id}\n\nSupported by C-001.\n" for result_id in result_ids),
        encoding="utf-8",
    )


class WritePaperTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(codex_cli, "grant_sandbox_read_access")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_loads_explicit_attempts_and_derives_readable_name(self):
        with TemporaryDirectory() as temporary:
            paper = make_paper(Path(temporary))
            second = make_ready_attempt(paper, "OP-002")
            first = make_ready_attempt(paper, "OP-001")

            inputs = write_paper.load_paper_inputs([second, first])

            self.assertEqual([item.result_id for item in inputs], ["R-001", "R-002"])
            self.assertEqual(
                [item.attempt.problem.id for item in inputs],
                ["OP-001", "OP-002"],
            )
            self.assertEqual(
                write_paper.derive_manuscript_name(inputs),
                "arXiv-1234.56789v1_OP-001_OP-002",
            )

    def test_cli_defaults_to_three_live_frontier_rounds(self):
        parsed = write_paper.build_parser().parse_args(["attempt-001"])
        self.assertEqual(parsed.model, "gpt-5.6-sol")
        self.assertEqual(parsed.reasoning_effort, "xhigh")
        self.assertEqual(parsed.web_search, "live")
        self.assertEqual(parsed.max_rounds, 3)
        self.assertIsNone(parsed.authors)
        self.assertEqual(write_paper._metadata([], None)["authors"], [])
        revision = write_paper.build_parser().parse_args(
            ["--revise", "draft-001", "--prompt", "Add figures"]
        )
        self.assertEqual(revision.revision_instruction, "Add figures")
        self.assertEqual(
            revision.prompt_template,
            write_paper.DEFAULT_PROMPT_PATH,
        )

    def test_writer_prompt_includes_explicit_revision_direction(self):
        rendered = write_paper.render_writer_prompt(
            "{{MODE_INSTRUCTION}}\n{{CONTEXT_DIRECTORY}}\n"
            "{{MANUSCRIPT_METADATA_JSON}}",
            context=Path.cwd(),
            authors=[],
            title_hint=None,
            previous=write_paper.DraftRef(
                Path("draft-001"),
                1,
                {"status": "draft_complete"},
            ),
            revision_instruction="Add figures explaining the construction.",
        )

        self.assertIn("<revision_instruction>", rendered)
        self.assertIn("Add figures explaining the construction.", rendered)

    def test_prompts_keep_internal_review_language_out_of_manuscript(self):
        writer_prompt = write_paper.DEFAULT_PROMPT_PATH.read_text(encoding="utf-8")
        reviewer_prompt = write_paper.DEFAULT_REVIEW_PROMPT_PATH.read_text(
            encoding="utf-8"
        )

        self.assertIn(
            "Keep the internal research and review workflow completely out of `main.tex`",
            writer_prompt,
        )
        self.assertIn(
            "Treat leaked workflow language as an exposition finding",
            reviewer_prompt,
        )

    def test_readiness_gate_can_only_be_overridden_explicitly(self):
        with TemporaryDirectory() as temporary:
            paper = make_paper(Path(temporary))
            attempt = make_ready_attempt(paper, "OP-001")
            review = common.read_json(attempt / "review-result.json")
            review["correctness"] = "minor_gaps"
            common.write_json(attempt / "review-result.json", review)
            manifest = common.read_json(attempt / "review-manifest.json")
            manifest["attempt_digest"] = common.solver_attempt_digest(attempt)
            common.write_json(attempt / "review-manifest.json", manifest)

            with self.assertRaisesRegex(common.CodexError, "not paper-ready"):
                write_paper.load_paper_inputs([attempt])
            inputs = write_paper.load_paper_inputs(
                [attempt],
                allow_not_ready=True,
            )
            self.assertTrue(inputs[0].readiness_issues)
            workspace = Path(temporary) / "override-workspace"
            workspace.mkdir()
            write_manuscript_files(workspace, ["R-001"])
            common.write_json(
                workspace / "agent-result.json",
                paper_result(["R-001"]),
            )

            result, _ = write_paper.validate_paper_result(
                workspace / "agent-result.json",
                workspace,
                inputs,
            )

            self.assertEqual(result["status"], "draft_complete")

    def test_validates_traceability_and_bibliography(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            paper = make_paper(root)
            attempt = make_ready_attempt(paper, "OP-001")
            inputs = write_paper.load_paper_inputs([attempt])
            workspace = root / "workspace"
            workspace.mkdir()
            write_manuscript_files(workspace, ["R-001"])
            (workspace / "main.pdf").write_bytes(b"%PDF-agent")
            figures = workspace / "figures"
            figures.mkdir()
            svg = figures / "overview.svg"
            svg.write_text(
                "<svg xmlns='http://www.w3.org/2000/svg'/>",
                encoding="utf-8",
            )
            figure = figures / "overview.pdf"
            figure.write_bytes(b"%PDF-figure")
            response = paper_result(["R-001"])
            response["generated_files"] = [
                "main.tex",
                "references.bib",
                "readiness.md",
                "main.pdf",
                "figures/overview.svg",
                "figures/overview.pdf",
            ]
            common.write_json(
                workspace / "agent-result.json",
                response,
            )

            result, files = write_paper.validate_paper_result(
                workspace / "agent-result.json",
                workspace,
                inputs,
                authors=[],
            )

            self.assertEqual(result["status"], "draft_complete")
            self.assertEqual(files, [svg, figure])
            self.assertEqual(
                result["generated_files"],
                ["figures/overview.svg", "figures/overview.pdf"],
            )
            unpaired_response = paper_result(["R-001"])
            unpaired_response["generated_files"] = ["figures/overview.svg"]
            common.write_json(
                workspace / "agent-result.json",
                unpaired_response,
            )
            with self.assertRaisesRegex(common.CodexError, "matching PDF"):
                write_paper.validate_paper_result(
                    workspace / "agent-result.json",
                    workspace,
                    inputs,
                )
            unsafe_response = paper_result(["R-001"])
            unsafe_response["generated_files"] = ["notes.txt"]
            (workspace / "notes.txt").write_text("unsafe", encoding="utf-8")
            common.write_json(
                workspace / "agent-result.json",
                unsafe_response,
            )
            with self.assertRaisesRegex(common.CodexError, "unsafe generated"):
                write_paper.validate_paper_result(
                    workspace / "agent-result.json",
                    workspace,
                    inputs,
                )
            common.write_json(
                workspace / "agent-result.json",
                response,
            )
            (workspace / "references.bib").write_text(
                "@article{different, title={Other}}\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(common.CodexError, "missing bibliography"):
                write_paper.validate_paper_result(
                    workspace / "agent-result.json",
                    workspace,
                    inputs,
                )

    def test_compile_latex_builds_nonempty_paper_pdf(self):
        with TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            (workspace / "main.tex").write_text(
                "\\documentclass{article}\n"
                "\\begin{document}\nCompile smoke test.\n"
                "\\end{document}\n",
                encoding="utf-8",
            )
            try:
                latexmk = write_paper.resolve_latexmk("latexmk")
            except common.CodexError as exc:
                self.skipTest(str(exc))

            try:
                pdf = write_paper.compile_latex(workspace, latexmk)
            except common.CodexError as exc:
                self.fail(
                    f"{exc}\n"
                    + (workspace / "build.log").read_text(
                        encoding="utf-8", errors="replace"
                    )
                )

            self.assertTrue(pdf.is_file())
            self.assertGreater(pdf.stat().st_size, 0)
            self.assertTrue((workspace / "build.log").is_file())

    def test_max_rounds_runs_critic_guided_revision_and_stops_early(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            paper = make_paper(root)
            attempt = make_ready_attempt(paper, "OP-001")
            inputs = write_paper.load_paper_inputs([attempt])
            manuscript = root / "manuscripts" / "test"
            calls = {"writer": 0, "reviewer": 0}

            def fake_codex(**kwargs):
                workspace = kwargs["workspace"]
                if kwargs["schema_path"].name == "open-problem-paper.schema.json":
                    calls["writer"] += 1
                    write_manuscript_files(workspace, ["R-001"])
                    addressed = []
                    if calls["writer"] == 2:
                        self.assertTrue(
                            (
                                workspace
                                / "inputs"
                                / "manuscript"
                                / "paper-review.json"
                            ).is_file()
                        )
                        with (workspace / "readiness.md").open(
                            "a", encoding="utf-8"
                        ) as report:
                            report.write("\n## P-001\n\nResolved in the revision.\n")
                        addressed = [
                            {
                                "finding_id": "P-001",
                                "disposition": "resolved",
                                "explanation": "Expanded the proof.",
                            }
                        ]
                    common.write_json(
                        workspace / "agent-result.json",
                        paper_result(["R-001"], addressed=addressed),
                    )
                else:
                    calls["reviewer"] += 1
                    if calls["reviewer"] == 1:
                        critique = "# Paper critique\n\n## P-001\n\nExpand the proof.\n"
                        review_result = {
                            "verdict": "needs_minor_revision",
                            "summary": "One detail should be expanded.",
                            "result_reviews": [
                                {
                                    "result_id": "R-001",
                                    "assessment": "supported",
                                    "explanation": "The result is correct.",
                                }
                            ],
                            "findings": [
                                {
                                    "id": "P-001",
                                    "severity": "minor",
                                    "category": "exposition",
                                    "location": "Proof of the main theorem",
                                    "result_ids": ["R-001"],
                                    "source_claim_ids": ["C-001"],
                                    "explanation": "One step is terse.",
                                    "suggested_repair": "Expand the step.",
                                    "requires_new_research": False,
                                }
                            ],
                            "recommended_action": "revise",
                            "warnings": [],
                        }
                    else:
                        critique = "# Paper critique\n\nReady for expert review.\n"
                        review_result = {
                            "verdict": "ready_for_expert_review",
                            "summary": "No major issue remains.",
                            "result_reviews": [
                                {
                                    "result_id": "R-001",
                                    "assessment": "supported",
                                    "explanation": "The result is supported.",
                                }
                            ],
                            "findings": [],
                            "recommended_action": "human_review",
                            "warnings": [],
                        }
                    (workspace / "paper-critique.md").write_text(
                        critique,
                        encoding="utf-8",
                    )
                    common.write_json(workspace / "agent-result.json", review_result)
                write_run_files(workspace)
                return workspace / "agent-result.json"

            def fake_compile(workspace, latexmk):
                (workspace / "build.log").write_text(
                    "Build complete.\n",
                    encoding="utf-8",
                )
                (workspace / "main.pdf").write_bytes(b"%PDF-paper")
                return workspace / "main.pdf"

            options = codex_cli.ModelOptions("test-model", "high", False)
            with (
                patch.object(
                    codex_cli,
                    "run_structured_codex",
                    side_effect=fake_codex,
                ),
                patch.object(
                    write_paper,
                    "compile_latex",
                    side_effect=fake_compile,
                ),
            ):
                outcome = write_paper.run_pipeline(
                    manuscript,
                    inputs,
                    previous=None,
                    authors=["Test Author"],
                    title_hint=None,
                    max_rounds=3,
                    codex="codex",
                    codex_version="test",
                    latexmk="latexmk",
                    prompt_template=write_paper.DEFAULT_PROMPT_PATH.read_text(
                        encoding="utf-8"
                    ),
                    schema_path=write_paper.DEFAULT_SCHEMA_PATH,
                    config_digest="writer-config",
                    options=options,
                    review_prompt_template=(
                        write_paper.DEFAULT_REVIEW_PROMPT_PATH.read_text(
                            encoding="utf-8"
                        )
                    ),
                    review_schema_path=write_paper.DEFAULT_REVIEW_SCHEMA_PATH,
                    review_config_digest="review-config",
                    review_options=options,
                )

            self.assertTrue(outcome.ready)
            self.assertEqual(len(outcome.drafts), 2)
            self.assertEqual(calls, {"writer": 2, "reviewer": 2})
            self.assertEqual(
                common.read_json(manuscript / "draft-002" / "manifest.json")[
                    "previous_draft"
                ],
                "draft-001",
            )
            self.assertTrue(
                (manuscript / "draft-002" / "paper-critique.md").is_file()
            )
            self.assertTrue(
                (manuscript / "draft-002" / "main.pdf").is_file()
            )
            self.assertFalse(
                (manuscript / "draft-002" / "paper.pdf").exists()
            )
            self.assertTrue(
                write_paper.paper_review_is_current(
                    outcome.drafts[-1],
                    inputs,
                    config_digest="review-config",
                )
            )

    def test_max_rounds_is_a_hard_cap(self):
        draft = write_paper.DraftRef(
            Path("draft-001"),
            1,
            {"status": "draft_complete"},
        )
        review = write_paper.PaperReview(
            draft,
            {"verdict": "needs_minor_revision"},
        )
        options = codex_cli.ModelOptions()
        with (
            patch.object(write_paper, "run_author_round", return_value=draft) as author,
            patch.object(write_paper, "run_paper_review", return_value=review) as critic,
        ):
            outcome = write_paper.run_pipeline(
                Path("manuscript"),
                [],
                previous=None,
                authors=["Anonymous"],
                title_hint=None,
                max_rounds=1,
                codex="codex",
                codex_version="test",
                latexmk="latexmk",
                prompt_template="prompt",
                schema_path=Path("paper-schema.json"),
                config_digest="writer",
                options=options,
                review_prompt_template="review prompt",
                review_schema_path=Path("review-schema.json"),
                review_config_digest="reviewer",
                review_options=options,
            )

        self.assertEqual(author.call_count, 1)
        self.assertEqual(critic.call_count, 1)
        self.assertEqual(outcome.reason, "maximum rounds reached")
        self.assertFalse(outcome.ready)

    def test_readiness_override_sends_blocked_draft_to_critic(self):
        draft = write_paper.DraftRef(
            Path("draft-001"),
            1,
            {"status": "blocked"},
        )
        review = write_paper.PaperReview(
            draft,
            {"verdict": "needs_research"},
        )
        options = codex_cli.ModelOptions()
        with (
            patch.object(write_paper, "run_author_round", return_value=draft),
            patch.object(
                write_paper,
                "run_paper_review",
                return_value=review,
            ) as critic,
        ):
            outcome = write_paper.run_pipeline(
                Path("manuscript"),
                [],
                previous=None,
                authors=["Anonymous"],
                title_hint=None,
                max_rounds=1,
                codex="codex",
                codex_version="test",
                latexmk="latexmk",
                prompt_template="prompt",
                schema_path=Path("paper-schema.json"),
                config_digest="writer",
                options=options,
                review_prompt_template="review prompt",
                review_schema_path=Path("review-schema.json"),
                review_config_digest="reviewer",
                review_options=options,
                allow_not_ready=True,
            )

        self.assertEqual(critic.call_count, 1)
        self.assertEqual(outcome.reason, "needs_research")

    def test_revision_prompt_starts_author_round_before_critic(self):
        previous = write_paper.DraftRef(
            Path("draft-001"),
            1,
            {"status": "draft_complete"},
        )
        revised = write_paper.DraftRef(
            Path("draft-002"),
            2,
            {"status": "draft_complete"},
        )
        review = write_paper.PaperReview(
            revised,
            {"verdict": "needs_research"},
        )
        options = codex_cli.ModelOptions()
        calls: list[str] = []

        def author_round(*args, **kwargs):
            calls.append("author")
            self.assertEqual(kwargs["previous"], previous)
            self.assertEqual(kwargs["revision_instruction"], "Add figures")
            return revised

        def paper_review(*args, **kwargs):
            calls.append("critic")
            return review

        with (
            patch.object(
                write_paper,
                "run_author_round",
                side_effect=author_round,
            ),
            patch.object(
                write_paper,
                "run_paper_review",
                side_effect=paper_review,
            ),
        ):
            outcome = write_paper.run_pipeline(
                Path("manuscript"),
                [],
                previous=previous,
                authors=[],
                title_hint=None,
                revision_instruction="Add figures",
                max_rounds=1,
                codex="codex",
                codex_version="test",
                latexmk="latexmk",
                prompt_template="prompt",
                schema_path=Path("paper-schema.json"),
                config_digest="writer",
                options=options,
                review_prompt_template="review prompt",
                review_schema_path=Path("review-schema.json"),
                review_config_digest="reviewer",
                review_options=options,
            )

        self.assertEqual(calls, ["author", "critic"])
        self.assertEqual(outcome.reason, "needs_research")


if __name__ == "__main__":
    unittest.main()
