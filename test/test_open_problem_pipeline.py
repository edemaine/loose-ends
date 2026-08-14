from pathlib import Path
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import codex_cli
import analyze_papers
import human_review
import open_problem_common as common
import review_solutions
import literature_review
import solve_open_problems
import triage_open_problems


def make_analyzed_paper(root: Path) -> Path:
    paper = root / "arXiv-1234.56789v1"
    (paper / "source").mkdir(parents=True)
    (paper / "paper.pdf").write_bytes(b"%PDF-test")
    (paper / "source" / "main.tex").write_text(
        "\\section{Results}\nA theorem and an open problem.\n",
        encoding="utf-8",
    )
    common.write_json(
        paper / "metadata.json",
        {
            "title": "Test Paper",
            "authors": ["Ada Lovelace"],
            "published": "2024-01-02T00:00:00Z",
            "updated": "2024-01-03T00:00:00Z",
        },
    )
    analysis = paper / "analysis"
    analysis.mkdir()
    (analysis / "summary.md").write_text(
        "# Summary\n\nTest paper by Ada Lovelace.\n",
        encoding="utf-8",
    )
    (analysis / "results.md").write_text(
        "# Results\n\n## R-001\n\nA useful lemma.\n",
        encoding="utf-8",
    )
    (analysis / "open-problems.md").write_text(
        "# Open Problems\n\n## OP-001: Test conjecture\n\nProve it.\n"
        "\n## OP-002: Test construction\n\nConstruct it.\n",
        encoding="utf-8",
    )
    common.write_json(
        analysis / "manifest.json",
        {
            "schema_version": 2,
            "source_digest": "source-test",
            "paper_title": "Test Paper",
            "paper_authors": ["Ada Lovelace"],
            "open_problems": [
                {
                    "id": "OP-001",
                    "title": "Test conjecture",
                    "explicitness": "explicit",
                },
                {
                    "id": "OP-002",
                    "title": "Test construction",
                    "explicitness": "inferred",
                },
            ],
        },
    )
    return paper


def write_run_files(workspace: Path) -> None:
    (workspace / "events.jsonl").write_text(
        '{"type":"turn.completed"}\n',
        encoding="utf-8",
    )
    (workspace / "run.log").write_text("", encoding="utf-8")


class OpenProblemPipelineTests(unittest.TestCase):
    def test_human_priority_is_derived_from_merit_axes(self):
        base = {
            "correctness": "plausible",
            "reviewed_coverage": "partial",
            "importance": "minor",
        }
        self.assertEqual(
            review_solutions.derive_human_priority(base), "low"
        )
        self.assertEqual(
            review_solutions.derive_human_priority(
                {**base, "importance": "moderate"}
            ),
            "medium",
        )
        self.assertEqual(
            review_solutions.derive_human_priority(
                {**base, "importance": "major"}
            ),
            "high",
        )
        self.assertEqual(
            review_solutions.derive_human_priority(
                {
                    **base,
                    "correctness": "major_gaps",
                    "importance": "resolution",
                }
            ),
            "medium",
        )

    def test_review_many_reports_each_completion(self):
        attempt = review_solutions.AttemptRef(
            problem=None,  # type: ignore[arg-type]
            directory=Path("attempt-001"),
            solver_result={},
        )
        outcome = review_solutions.ReviewOutcome(
            attempt,
            "reviewed",
            "plausible",
            "partial",
            "moderate",
            "medium",
            "reviewed",
        )
        finished = []
        with patch.object(
            review_solutions,
            "review_attempt",
            return_value=outcome,
        ):
            outcomes, failures = review_solutions.review_many(
                [attempt],
                codex="codex",
                codex_version="test",
                prompt_template="prompt",
                schema_path=Path("schema"),
                config_digest="config",
                options=codex_cli.ModelOptions(),
                jobs=1,
                on_finished=lambda item, result, error: finished.append(
                    (item, result, error)
                ),
            )
        self.assertEqual(outcomes, [outcome])
        self.assertEqual(failures, [])
        self.assertEqual(finished, [(attempt, outcome, None)])

    def test_review_recovers_matching_completed_workspace(self):
        with TemporaryDirectory() as temporary:
            paper = make_analyzed_paper(Path(temporary))
            problem = common.discover_problem_refs(
                [paper], problem_ids={"OP-001"}
            )[0]
            problem.directory.mkdir(parents=True)
            directory = problem.directory / "attempt-001"
            directory.mkdir()
            (directory / "attempt.md").write_text(
                "# Attempt\n\n## C-001\n\nA special case holds.\n",
                encoding="utf-8",
            )
            solver_result = {
                "claimed_result_type": "partial_result",
                "checkable_claims": [{"id": "C-001"}],
            }
            common.write_json(directory / "solver-result.json", solver_result)
            common.write_json(directory / "manifest.json", {"schema_version": 3})
            attempt = review_solutions.AttemptRef(
                problem,
                directory,
                solver_result,
            )
            workspace = problem.directory / ".review-run-finished"
            workspace.mkdir()
            options = codex_cli.ModelOptions("test-model", "xhigh", False)
            review_solutions._write_work_record(
                workspace,
                attempt,
                attempt_digest=common.solver_attempt_digest(directory),
                config_digest="config",
                codex_version="test",
                options=options,
                web_search="live",
            )
            (workspace / "critique.md").write_text(
                "# Critique\n\nThe special case is supported.\n",
                encoding="utf-8",
            )
            common.write_json(
                workspace / "agent-result.json",
                {
                    "correctness": "well_supported",
                    "reviewed_coverage": "special_case",
                    "importance": "moderate",
                    "verification_confidence": "high",
                    "summary": "The special case is valid.",
                    "claim_reviews": [
                        {
                            "claim_id": "C-001",
                            "assessment": "supported",
                            "explanation": "Independently checked.",
                        }
                    ],
                    "blocking_gaps": ["The general case remains."],
                    "recommended_next_steps": ["Generalize it."],
                    "warnings": [],
                },
            )
            write_run_files(workspace)

            outcome = review_solutions.recover_review(
                attempt,
                codex="codex",
                codex_version="test",
                config_digest="config",
                options=options,
                web_search="live",
            )

            self.assertIsNotNone(outcome)
            assert outcome is not None
            self.assertEqual(outcome.status, "recovered")
            self.assertTrue((directory / "review-result.json").is_file())
            manifest = common.read_json(directory / "review-manifest.json")
            self.assertEqual(
                manifest["recovered_from_workspace"], workspace.name
            )
            self.assertTrue(workspace.is_dir())

    def test_review_recovers_missing_critique_and_ignores_unknown_claim(self):
        with TemporaryDirectory() as temporary:
            paper = make_analyzed_paper(Path(temporary))
            problem = common.discover_problem_refs(
                [paper], problem_ids={"OP-001"}
            )[0]
            problem.directory.mkdir(parents=True)
            directory = problem.directory / "attempt-001"
            directory.mkdir()
            solver_result = {
                "claimed_result_type": "partial_result",
                "checkable_claims": [{"id": "C-001"}],
            }
            common.write_json(directory / "solver-result.json", solver_result)
            attempt = review_solutions.AttemptRef(
                problem,
                directory,
                solver_result,
            )
            workspace = problem.directory / ".review-run-finished"
            workspace.mkdir()
            common.write_json(
                workspace / "agent-result.json",
                {
                    "correctness": "well_supported",
                    "reviewed_coverage": "special_case",
                    "importance": "moderate",
                    "verification_confidence": "high",
                    "summary": "The special case is valid.",
                    "claim_reviews": [
                        {
                            "claim_id": "C-001",
                            "assessment": "supported",
                            "explanation": "Independently checked.",
                        },
                        {
                            "claim_id": "EXT-001",
                            "assessment": "supported",
                            "explanation": "An external source agrees.",
                        },
                    ],
                    "blocking_gaps": ["The general case remains."],
                    "recommended_next_steps": ["Generalize it."],
                    "warnings": [],
                },
            )
            write_run_files(workspace)

            self.assertTrue(
                review_solutions.recover_missing_critique(
                    attempt,
                    workspace,
                )
            )
            result = common.read_json(workspace / "agent-result.json")
            self.assertEqual(
                [review["claim_id"] for review in result["claim_reviews"]],
                ["C-001"],
            )
            self.assertTrue(
                any("EXT-001" in warning for warning in result["warnings"])
            )
            critique = (workspace / "critique.md").read_text(
                encoding="utf-8"
            )
            self.assertIn("Driver recovery notice", critique)
            self.assertIn("C-001", critique)
            self.assertNotIn("external source agrees", critique.lower())

            rendered = review_solutions.render_prompt(
                "Claims: {{CLAIM_IDS}}",
                attempt=attempt,
                context_directory=workspace / "inputs",
            )
            self.assertEqual(rendered, "Claims: `C-001`")
            constrained = review_solutions.write_attempt_review_schema(
                review_solutions.DEFAULT_SCHEMA_PATH,
                workspace,
                attempt,
            )
            schema = common.read_json(constrained)
            reviews_schema = schema["properties"]["claim_reviews"]
            self.assertEqual(reviews_schema["minItems"], 1)
            self.assertEqual(reviews_schema["maxItems"], 1)
            self.assertEqual(
                reviews_schema["items"]["properties"]["claim_id"]["enum"],
                ["C-001"],
            )

    def setUp(self):
        patcher = patch.object(codex_cli, "grant_sandbox_read_access")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_triage_is_batched_and_attempt_history_invalidates_it(self):
        with TemporaryDirectory() as temporary:
            paper = make_analyzed_paper(Path(temporary))
            problems = common.discover_problem_refs([paper])
            prompt = triage_open_problems.DEFAULT_PROMPT_PATH.read_text(
                encoding="utf-8"
            )
            schema_text = (
                triage_open_problems.DEFAULT_SCHEMA_PATH.read_text(
                    encoding="utf-8"
                )
            )
            options = codex_cli.ModelOptions("test-model", "xhigh", True)
            config_digest = codex_cli.semantic_config_digest(
                prompt,
                schema_text,
                options,
            )

            def fake_triage(**kwargs):
                workspace = kwargs["workspace"]
                staged = json.loads(
                    (workspace / "inputs" / "problems.json").read_text(
                        encoding="utf-8"
                    )
                )
                entries = []
                for index, problem in enumerate(staged["problems"], 1):
                    problem_id = problem["id"]
                    classification = "attempt" if index == 1 else "maybe"
                    suggestion = {
                        "id": f"S-{index:03d}",
                        "mode": "proof",
                        "suggestion": "Try the central lemma.",
                        "why_promising": "The paper supplies its hypotheses.",
                        "abandon_if": "The hypotheses cannot be established.",
                    }
                    entries.append(
                        {
                            "problem_id": problem_id,
                            "classification": classification,
                            "rationale": "The paper supplies relevant machinery.",
                            "promising_features": ["R-001"],
                            "obstacles": ["A missing estimate"],
                            "suggested_approaches": [suggestion],
                        }
                    )
                    (workspace / f"triage-{problem_id}.md").write_text(
                        f"# Triage for {problem_id}\n\n{classification}.\n",
                        encoding="utf-8",
                    )
                common.write_json(
                    workspace / "agent-result.json",
                    {
                        "status": "complete",
                        "triages": entries,
                        "warnings": [],
                    },
                )
                write_run_files(workspace)
                return workspace / "agent-result.json"

            with patch.object(
                codex_cli,
                "run_structured_codex",
                side_effect=fake_triage,
            ) as run:
                outcomes = triage_open_problems.triage_paper(
                    problems,
                    codex="codex",
                    codex_version="test",
                    prompt_template=prompt,
                    schema_path=triage_open_problems.DEFAULT_SCHEMA_PATH,
                    config_digest=config_digest,
                    options=options,
                    launch_interval=0,
                )

            self.assertEqual(run.call_count, 1)
            self.assertEqual(len(outcomes), 2)
            self.assertTrue(
                common.triage_is_current(
                    problems[0],
                    config_digest=config_digest,
                )
            )
            self.assertEqual(
                common.triage_result(problems[0])["classification"],
                "attempt",
            )

            attempt = problems[0].directory / "attempt-001"
            attempt.mkdir()
            (attempt / "attempt.md").write_text(
                "# Attempt\n\nNew work.\n",
                encoding="utf-8",
            )
            self.assertFalse(common.triage_is_current(problems[0]))

    def test_repairs_inaccessible_generated_data_before_triage(self):
        with TemporaryDirectory() as temporary:
            paper = make_analyzed_paper(Path(temporary))
            with (
                patch.object(
                    codex_cli,
                    "is_windows_host",
                    return_value=True,
                ),
                patch.object(
                    codex_cli,
                    "workspace_is_user_accessible",
                    side_effect=lambda path: path != paper / "analysis",
                ),
                patch.object(
                    codex_cli,
                    "resolve_codex_executable",
                    return_value="resolved-codex",
                ) as resolve,
                patch.object(
                    codex_cli,
                    "normalize_workspace_access",
                ) as normalize,
            ):
                codex, repaired = common.repair_problem_data_access(
                    [paper],
                    codex_command="codex",
                )

            self.assertEqual(codex, "resolved-codex")
            self.assertEqual(repaired, 1)
            resolve.assert_called_once_with("codex")
            normalize.assert_called_once_with(
                paper / "analysis",
                "resolved-codex",
            )

    def test_synthesizes_missing_triage_markdown_from_valid_json(self):
        with TemporaryDirectory() as temporary:
            paper = make_analyzed_paper(Path(temporary))
            problem = common.discover_problem_refs(
                [paper],
                problem_ids={"OP-001"},
            )[0]
            workspace = Path(temporary) / "workspace"
            workspace.mkdir()
            common.write_json(
                workspace / "agent-result.json",
                {
                    "status": "partial",
                    "triages": [
                        {
                            "problem_id": problem.id,
                            "classification": "attempt",
                            "rationale": "The paper supplies a useful lemma.",
                            "promising_features": ["R-001 is close."],
                            "obstacles": ["One estimate is missing."],
                            "suggested_approaches": [
                                {
                                    "id": "proof",
                                    "mode": "proof",
                                    "suggestion": "Prove the estimate.",
                                    "why_promising": "It would close the gap.",
                                    "abandon_if": "A small case refutes it.",
                                }
                            ],
                        }
                    ],
                    "warnings": [],
                },
            )

            result, entries = triage_open_problems.validate_triage_result(
                workspace / "agent-result.json",
                workspace,
                [problem],
            )

            report = (
                workspace
                / f"triage-{problem.id}.md"
            ).read_text(encoding="utf-8")
            self.assertIn(f"# Triage {problem.id}", report)
            self.assertIn("**attempt**", report)
            self.assertIn("### proof — proof", report)
            self.assertEqual(entries[problem.id]["classification"], "attempt")
            self.assertIn("Driver synthesized", result["warnings"][-1])

    def test_solver_uses_full_context_then_reviewer_triages_progress(self):
        with TemporaryDirectory() as temporary:
            paper = make_analyzed_paper(Path(temporary))
            problem = common.discover_problem_refs(
                [paper],
                problem_ids={"OP-001"},
            )[0]
            problem.directory.mkdir(parents=True)
            triage_entry = {
                "problem_id": "OP-001",
                "classification": "attempt",
                "rationale": "Promising.",
                "promising_features": ["R-001"],
                "obstacles": [],
                "suggested_approaches": [
                    {
                        "id": "proof-search",
                        "mode": "proof",
                        "suggestion": "Prove a key lemma.",
                        "why_promising": "R-001 nearly implies it.",
                        "abandon_if": "A required estimate is false.",
                    }
                ],
            }
            common.write_json(problem.directory / "triage.json", triage_entry)
            (problem.directory / "triage.md").write_text(
                "# Triage OP-001\n\nAttempt.\n",
                encoding="utf-8",
            )
            common.write_json(
                problem.directory / "triage-manifest.json",
                {
                    "schema_version": common.TRIAGE_MANIFEST_SCHEMA_VERSION,
                    "input_digest": common.triage_input_digest(problem),
                    "config_digest": "triage-config",
                },
            )
            work, missing, resolved = solve_open_problems.build_work(
                [problem],
                require_triage_classes={"attempt"},
            )
            self.assertEqual(missing, [])
            self.assertEqual(resolved, [])
            self.assertEqual(len(work), 1)
            self.assertEqual(
                work[0].guidance["suggested_approaches"][0]["id"],
                "proof-search",
            )

            solver_prompt = (
                solve_open_problems.DEFAULT_PROMPT_PATH.read_text(
                    encoding="utf-8"
                )
            )
            solver_schema = (
                solve_open_problems.DEFAULT_SCHEMA_PATH.read_text(
                    encoding="utf-8"
                )
            )
            solver_options = codex_cli.ModelOptions()
            solver_config = codex_cli.semantic_config_digest(
                solver_prompt,
                solver_schema,
                solver_options,
            )

            def fake_solver(**kwargs):
                workspace = kwargs["workspace"]
                self.assertTrue(
                    (workspace / "inputs" / "paper" / "paper.pdf").is_file()
                )
                self.assertTrue(
                    (
                        workspace
                        / "inputs"
                        / "paper"
                        / "source"
                        / "main.tex"
                    ).is_file()
                )
                self.assertTrue(
                    (
                        workspace
                        / "inputs"
                        / "analysis"
                        / "results.md"
                    ).is_file()
                )
                self.assertTrue(
                    (
                        workspace
                        / "inputs"
                        / "triage"
                        / "OP-001"
                        / "triage.json"
                    ).is_file()
                )
                (workspace / "attempt.md").write_text(
                    "# Attempt\n\n## Checkable claims\n\n"
                    "### C-001\n\nA special case follows from R-001.\n",
                    encoding="utf-8",
                )
                common.write_json(
                    workspace / "agent-result.json",
                    {
                        "claimed_result_type": "partial_result",
                        "summary": "Proved a special case.",
                        "external_sources": [],
                        "checkable_claims": [
                            {
                                "id": "C-001",
                                "type": "lemma",
                                "statement": "The special case holds.",
                                "support": "Apply R-001.",
                                "remaining_gap": "The general case remains.",
                            }
                        ],
                        "artifacts": [],
                        "warnings": [],
                    },
                )
                write_run_files(workspace)
                return workspace / "agent-result.json"

            with patch.object(
                codex_cli,
                "run_structured_codex",
                side_effect=fake_solver,
            ):
                solve_outcome = solve_open_problems.solve_work(
                    work[0],
                    codex="codex",
                    codex_version="test",
                    prompt_template=solver_prompt,
                    schema_path=solve_open_problems.DEFAULT_SCHEMA_PATH,
                    config_digest=solver_config,
                    options=solver_options,
                    launch_interval=0,
                )

            attempt = solve_outcome.attempt
            self.assertTrue((attempt.directory / "attempt.md").is_file())
            self.assertTrue(review_solutions.is_promising(attempt))
            self.assertFalse(common.triage_is_current(problem))
            history_before_review = common.attempt_history_digest(problem)

            review_prompt = review_solutions.DEFAULT_PROMPT_PATH.read_text(
                encoding="utf-8"
            )
            review_schema = review_solutions.DEFAULT_SCHEMA_PATH.read_text(
                encoding="utf-8"
            )
            review_options = codex_cli.ModelOptions()
            review_config = codex_cli.semantic_config_digest(
                review_prompt,
                review_schema,
                review_options,
            )

            def fake_review(**kwargs):
                workspace = kwargs["workspace"]
                target = (
                    workspace
                    / "inputs"
                    / "history"
                    / "OP-001"
                    / "attempt-001"
                    / "attempt.md"
                )
                self.assertTrue(target.is_file())
                (workspace / "critique.md").write_text(
                    "# Critique\n\nC-001 is plausible but incomplete.\n",
                    encoding="utf-8",
                )
                common.write_json(
                    workspace / "agent-result.json",
                    {
                        "correctness": "plausible",
                        "reviewed_coverage": "special_case",
                        "importance": "moderate",
                        "verification_confidence": "medium",
                        "summary": "The special case appears valid.",
                        "claim_reviews": [
                            {
                                "claim_id": "C-001",
                                "assessment": "supported",
                                "explanation": "R-001 applies as stated.",
                            }
                        ],
                        "blocking_gaps": ["The general case remains."],
                        "recommended_next_steps": ["Generalize the estimate."],
                        "warnings": [],
                    },
                )
                write_run_files(workspace)
                return workspace / "agent-result.json"

            with patch.object(
                codex_cli,
                "run_structured_codex",
                side_effect=fake_review,
            ):
                review_outcome = review_solutions.review_attempt(
                    attempt,
                    codex="codex",
                    codex_version="test",
                    prompt_template=review_prompt,
                    schema_path=review_solutions.DEFAULT_SCHEMA_PATH,
                    config_digest=review_config,
                    options=review_options,
                    launch_interval=0,
                )

            self.assertEqual(review_outcome.priority, "medium")
            self.assertTrue(
                review_solutions.review_is_current(
                    attempt,
                    config_digest=review_config,
                )
            )
            self.assertNotEqual(
                history_before_review,
                common.attempt_history_digest(problem),
            )
            stale_work, skipped, resolved = solve_open_problems.build_work(
                [problem],
                require_triage_classes={"attempt"},
            )
            self.assertEqual(stale_work, [])
            self.assertEqual(skipped, [problem])
            self.assertEqual(resolved, [])

    def test_literature_search_batches_paper_and_skips_resolved_solver_work(self):
        with TemporaryDirectory() as temporary:
            paper = make_analyzed_paper(Path(temporary))
            problems = common.discover_problem_refs([paper])
            for problem in problems:
                problem.directory.mkdir(parents=True, exist_ok=True)
                if problem.id == "OP-001":
                    prior = problem.directory / "attempt-001"
                    prior.mkdir()
                    (prior / "attempt.md").write_text(
                        "# Prior attempt\n\nTry the hinge terminology.\n",
                        encoding="utf-8",
                    )
                    (prior / "critique.md").write_text(
                        "# Critique\n\nSearch the rigidity literature.\n",
                        encoding="utf-8",
                    )
                common.write_json(
                    problem.directory / common.TRIAGE_RESULT,
                    {
                        "problem_id": problem.id,
                        "classification": "attempt",
                        "rationale": "Worth searching.",
                        "promising_features": [],
                        "obstacles": [],
                        "suggested_approaches": [],
                    },
                )
                common.write_json(
                    problem.directory / common.TRIAGE_MANIFEST,
                    {
                        "schema_version": (
                            common.TRIAGE_MANIFEST_SCHEMA_VERSION
                        ),
                        "input_digest": common.triage_input_digest(problem),
                    },
                )

            prompt = (
                literature_review.DEFAULT_PROMPT_PATH.read_text(
                    encoding="utf-8"
                )
            )
            schema_text = (
                literature_review.DEFAULT_SCHEMA_PATH.read_text(
                    encoding="utf-8"
                )
            )
            options = codex_cli.ModelOptions("test-model", "xhigh")
            config_digest = codex_cli.semantic_config_digest(
                prompt,
                schema_text,
                options,
                web_search="live",
            )

            def source(role):
                return {
                    "id": "S0",
                    "role": role,
                    "priority": "high",
                    "title": "Later Paper",
                    "authors": ["Grace Hopper"],
                    "publication_year": "2025",
                    "url": "https://example.org/later-paper",
                    "source_type": "primary_source",
                    "result_statement": "A matching theorem is proved.",
                    "relevance": "It addresses the extracted question.",
                    "limitations": "Check the hypotheses carefully.",
                }

            def fake_literature(**kwargs):
                self.assertEqual(kwargs["web_search"], "live")
                workspace = kwargs["workspace"]
                self.assertTrue(
                    (workspace / "inputs" / "paper" / "paper.pdf").is_file()
                )
                self.assertTrue(
                    (
                        workspace
                        / "inputs"
                        / "history"
                        / "OP-001"
                        / "attempt-001"
                        / "attempt.md"
                    ).is_file()
                )
                self.assertTrue(
                    (
                        workspace
                        / "inputs"
                        / "history"
                        / "OP-001"
                        / "attempt-001"
                        / "critique.md"
                    ).is_file()
                )
                entries = [
                    {
                        "problem_id": "OP-001",
                        "resolution_status": "resolved",
                        "confidence": "high",
                        "status_summary": "A later theorem resolves OP-001.",
                        "exact_match_analysis": "All quantifiers match.",
                        "residual_problem": "",
                        "solver_briefing": "No original problem remains.",
                        "sources": [source("resolution")],
                        "search_queries": ["Test conjecture later theorem"],
                        "warnings": [],
                    },
                    {
                        "problem_id": "OP-002",
                        "resolution_status": "partially_resolved",
                        "confidence": "high",
                        "status_summary": "A special case is known.",
                        "exact_match_analysis": "The general case is absent.",
                        "residual_problem": "Construct the general case.",
                        "solver_briefing": "Adapt the special-case gadget.",
                        "sources": [source("partial_result")],
                        "search_queries": ["Test construction special case"],
                        "warnings": [],
                    },
                ]
                common.write_json(
                    workspace / "agent-result.json",
                    {
                        "status": "complete",
                        "literature": entries,
                        "warnings": [],
                    },
                )
                constrained = common.read_json(kwargs["schema_path"])
                literature_schema = constrained["properties"]["literature"]
                self.assertEqual(literature_schema["minItems"], 2)
                self.assertEqual(literature_schema["maxItems"], 2)
                self.assertEqual(
                    literature_schema["items"]["properties"][
                        "problem_id"
                    ]["enum"],
                    [problem.id for problem in problems],
                )
                self.assertIn(
                    "- ID: OP-001\n  Title: Test conjecture",
                    kwargs["prompt"],
                )
                write_run_files(workspace)
                return workspace / "agent-result.json"

            with patch.object(
                codex_cli,
                "run_structured_codex",
                side_effect=fake_literature,
            ) as run:
                outcomes = (
                    literature_review.search_paper_literature(
                        problems,
                        codex="codex",
                        codex_version="test",
                        prompt_template=prompt,
                        schema_path=(
                            literature_review.DEFAULT_SCHEMA_PATH
                        ),
                        config_digest=config_digest,
                        options=options,
                        web_search="live",
                        launch_interval=0,
                    )
                )

            self.assertEqual(run.call_count, 1)
            self.assertEqual(len(outcomes), 2)
            self.assertTrue(common.literature_is_current(problems[0]))
            self.assertTrue(common.triage_is_current(problems[0]))
            self.assertEqual(
                common.literature_result(problems[0])["resolution_status"],
                "resolved",
            )
            work, missing, resolved = solve_open_problems.build_work(
                problems,
                require_triage_classes={"attempt"},
            )
            self.assertEqual([item.problem.id for item in work], ["OP-002"])
            self.assertEqual(missing, [])
            self.assertEqual(resolved, [problems[0]])
            self.assertEqual(
                work[0].guidance["literature"]["residual_problem"],
                "Construct the general case.",
            )
            self.assertIsNotNone(work[0].literature_snapshot_digest)

            included, missing, resolved = solve_open_problems.build_work(
                problems,
                require_triage_classes={"attempt"},
                include_literature_resolved=True,
            )
            self.assertEqual(len(included), 2)
            self.assertEqual(missing, [])
            self.assertEqual(resolved, [])

            attempt = problems[0].directory / "attempt-002"
            attempt.mkdir()
            (attempt / "attempt.md").write_text(
                "# Attempt\n\nLater work.\n",
                encoding="utf-8",
            )
            self.assertFalse(common.triage_is_current(problems[0]))
            self.assertTrue(common.literature_is_current(problems[0]))
            partial = common.literature_result(problems[0])
            partial["run_status"] = "partial"
            common.write_json(
                problems[0].directory / common.LITERATURE_RESULT,
                partial,
            )
            self.assertFalse(common.literature_is_current(problems[0]))

    def test_literature_recovers_matching_completed_workspace(self):
        with TemporaryDirectory() as temporary:
            paper = make_analyzed_paper(Path(temporary))
            problem = common.discover_problem_refs(
                [paper], problem_ids={"OP-001"}
            )[0]
            runs = common.paper_runs_directory(paper)
            runs.mkdir()
            workspace = runs / ".literature-run-preserved"
            workspace.mkdir()
            prompt = literature_review.DEFAULT_PROMPT_PATH.read_text(
                encoding="utf-8"
            )
            schema_text = (
                literature_review.DEFAULT_SCHEMA_PATH.read_text(
                    encoding="utf-8"
                )
            )
            options = codex_cli.ModelOptions("test-model", "xhigh")
            config_digest = codex_cli.semantic_config_digest(
                prompt,
                schema_text,
                options,
                web_search="live",
            )
            input_digests = {
                problem.id: common.literature_input_digest(problem)
            }
            literature_review._write_work_record(
                workspace,
                [problem],
                input_digests=input_digests,
                config_digest=config_digest,
                codex_version="test",
                options=options,
                web_search="live",
            )
            entry = {
                "problem_id": problem.id,
                "resolution_status": "no_resolution_found",
                "confidence": "medium",
                "status_summary": "No exact resolution was located.",
                "exact_match_analysis": "Nearby results do not match.",
                "residual_problem": "The original problem remains.",
                "solver_briefing": "Try the nearby technique.",
                "sources": [
                    {
                        "id": "S0",
                        "role": "technique",
                        "priority": "medium",
                        "title": "Nearby Work",
                        "authors": ["Grace Hopper"],
                        "publication_year": "2025",
                        "url": "https://example.org/nearby",
                        "source_type": "primary_source",
                        "result_statement": "A special technique is proved.",
                        "relevance": "The technique may transfer.",
                        "limitations": "It does not resolve the problem.",
                    }
                ],
                "search_queries": ["test conjecture later work"],
                "warnings": [],
            }
            common.write_json(
                workspace / "agent-result.json",
                {
                    "status": "complete",
                    "literature": [entry],
                    "warnings": [],
                },
            )
            write_run_files(workspace)

            with patch.object(codex_cli, "run_structured_codex") as run:
                outcomes = literature_review.search_paper_literature(
                    [problem],
                    codex="codex",
                    codex_version="test",
                    prompt_template=prompt,
                    schema_path=literature_review.DEFAULT_SCHEMA_PATH,
                    config_digest=config_digest,
                    options=options,
                    web_search="live",
                    launch_interval=0,
                )

            run.assert_not_called()
            self.assertEqual(outcomes[0].status, "recovered")
            self.assertTrue(workspace.is_dir())
            manifest = common.literature_manifest(problem)
            self.assertEqual(
                manifest["recovered_from_workspace"], workspace.name
            )
            self.assertEqual(
                common.literature_result(problem)["sources"][0]["id"],
                "S0",
            )
            self.assertTrue(
                (problem.directory / common.LITERATURE_MARKDOWN).is_file()
            )

    def test_literature_repairs_unsupported_resolved_status(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            paper = make_analyzed_paper(root)
            problems = common.discover_problem_refs([paper])
            workspace = root / "literature-workspace"
            workspace.mkdir()

            def entry(problem_id, confidence, role):
                problem = next(
                    problem
                    for problem in problems
                    if problem.id == problem_id
                )
                return {
                    "problem_id": problem_id,
                    "resolution_status": "resolved",
                    "confidence": confidence,
                    "status_summary": "A later source resolves the problem.",
                    "exact_match_analysis": "The formulations match.",
                    "residual_problem": "",
                    "solver_briefing": "Audit the published argument.",
                    "sources": [
                        {
                            "id": "S0",
                            "role": role,
                            "priority": "high",
                            "title": "Later Result",
                            "authors": ["Grace Hopper"],
                            "publication_year": "2025",
                            "url": "https://example.org/result",
                            "source_type": "primary_source",
                            "result_statement": "The exact claim is settled.",
                            "relevance": "It matches the open problem.",
                            "limitations": "The mapping should be checked.",
                        }
                    ],
                    "search_queries": ["later exact result"],
                    "warnings": [],
                }

            common.write_json(
                workspace / "agent-result.json",
                {
                    "status": "complete",
                    "literature": [
                        entry("OP-001", "medium", "resolution"),
                        entry("OP-002", "high", "counterexample"),
                    ],
                    "warnings": [],
                },
            )
            for problem in problems:
                (workspace / f"literature-{problem.id}.md").write_text(
                    f"# Literature {problem.id}\n\nAgent says resolved.\n",
                    encoding="utf-8",
                )
            write_run_files(workspace)

            result, by_id = literature_review.validate_literature_result(
                workspace / "agent-result.json",
                workspace,
                problems,
            )

            repaired = by_id["OP-001"]
            self.assertEqual(repaired["resolution_status"], "uncertain")
            self.assertTrue(
                repaired["warnings"][-1].startswith(
                    literature_review.STATUS_CORRECTION_PREFIX
                )
            )
            self.assertEqual(
                by_id["OP-002"]["resolution_status"], "resolved"
            )
            self.assertIn("OP-001: Driver downgraded", result["warnings"][0])

            literature_review._install_literature(
                problems[0],
                workspace=workspace,
                root_result=result,
                entry=repaired,
                input_digest=common.literature_input_digest(problems[0]),
                config_digest="test-config",
                codex_version="test",
                options=codex_cli.ModelOptions("test-model", "xhigh"),
                web_search="live",
            )
            installed = (
                problems[0].directory / common.LITERATURE_MARKDOWN
            ).read_text(encoding="utf-8")
            self.assertTrue(
                installed.startswith("> **Driver status correction:**")
            )

    def test_literature_rejects_partial_run(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            paper = make_analyzed_paper(root)
            problem = common.discover_problem_refs(
                [paper], problem_ids={"OP-001"}
            )[0]
            workspace = root / "literature-workspace"
            workspace.mkdir()
            response = workspace / "agent-result.json"
            common.write_json(
                response,
                {"status": "partial", "literature": [], "warnings": []},
            )
            with self.assertRaisesRegex(
                common.CodexError,
                "partial run; no reports were installed",
            ):
                literature_review.validate_literature_result(
                    response,
                    workspace,
                    [problem],
                )

    def test_literature_attempted_selector_bypasses_triage(self):
        with TemporaryDirectory() as temporary:
            paper = make_analyzed_paper(Path(temporary))
            problems = common.discover_problem_refs([paper])
            attempted = problems[1].directory / "attempt-001"
            attempted.mkdir(parents=True)
            (attempted / "attempt.md").write_text(
                "# Attempt\n\nA prior construction.\n",
                encoding="utf-8",
            )

            output = StringIO()
            with redirect_stdout(output):
                returncode = literature_review.main(
                    [str(paper), "--attempted", "--dry-run"]
                )

            contents = output.getvalue()
            self.assertEqual(returncode, 0)
            self.assertIn("OP-002: Test construction", contents)
            self.assertNotIn("OP-001: Test conjecture", contents)
            self.assertIn("1 without attempts", contents)

    def test_no_progress_is_not_promising_for_review(self):
        with TemporaryDirectory() as temporary:
            paper = make_analyzed_paper(Path(temporary))
            problem = common.discover_problem_refs(
                [paper],
                problem_ids={"OP-001"},
            )[0]
            directory = problem.directory / "attempt-001"
            directory.mkdir(parents=True)
            result = {
                "claimed_result_type": "none",
                "summary": "No concrete advance.",
                "checkable_claims": [],
                "artifacts": [],
                "warnings": [],
            }
            attempt = review_solutions.AttemptRef(problem, directory, result)
            self.assertFalse(review_solutions.is_promising(attempt))

    def test_human_review_prioritizes_attention_and_shows_files(self):
        with TemporaryDirectory() as temporary:
            paper = make_analyzed_paper(Path(temporary))
            problems = common.discover_problem_refs([paper])
            levels = ("medium", "high")
            bodies = ("MEDIUM BODY", "HIGH BODY")
            for problem, level, body in zip(problems, levels, bodies):
                directory = problem.directory / "attempt-001"
                directory.mkdir(parents=True)
                (directory / "attempt.md").write_text(
                    f"# Attempt\n\n{body}\n",
                    encoding="utf-8",
                )
                solver_result = {
                    "claimed_result_type": "partial_result",
                    "summary": f"{level} solver summary",
                    "checkable_claims": [],
                    "artifacts": [],
                    "warnings": [],
                }
                common.write_json(
                    directory / "solver-result.json",
                    solver_result,
                )
                common.write_json(directory / "manifest.json", {})
                (directory / "critique.md").write_text(
                    f"# Critique\n\n{level} critique.\n",
                    encoding="utf-8",
                )
                common.write_json(
                    directory / "review-result.json",
                    {
                        "correctness": "plausible",
                        "reviewed_coverage": (
                            "partial" if level == "high" else "special_case"
                        ),
                        "importance": "major" if level == "high" else "moderate",
                        "verification_confidence": "medium",
                        "human_priority": level,
                        "summary": f"{level} critic summary",
                        "claim_reviews": [],
                        "blocking_gaps": ["One gap."],
                        "recommended_next_steps": ["Check it."],
                        "warnings": [],
                    },
                )
                common.write_json(
                    directory / "review-manifest.json",
                    {
                        "attempt_digest": common.solver_attempt_digest(
                            directory
                        )
                    },
                )

            unreviewed = problems[0].directory / "attempt-002"
            unreviewed.mkdir()
            common.write_json(
                unreviewed / "solver-result.json",
                {
                    "claimed_result_type": "none",
                    "summary": "A later attempt without a review.",
                    "checkable_claims": [],
                    "artifacts": [],
                    "warnings": [],
                },
            )

            items = human_review.discover_human_reviews(
                problems,
                priority={"high", "medium"},
            )
            with patch.object(
                review_solutions,
                "review_is_current",
                return_value=False,
            ):
                stale_items = human_review.discover_human_reviews(
                    problems,
                    priority={"high", "medium"},
                )
                with self.assertRaises(common.CodexError):
                    human_review.discover_human_reviews(
                        problems,
                        priority={"high", "medium"},
                        include_stale=False,
                    )
            self.assertEqual(len(stale_items), 2)
            self.assertTrue(all(not item.current for item in stale_items))
            report = human_review.render_human_review_report(items)

            self.assertEqual(
                [item.priority for item in items],
                ["medium", "high"],
            )
            self.assertLess(
                report.index("MEDIUM BODY"),
                report.index("HIGH BODY"),
            )
            self.assertIn("critique.md", report)
            self.assertIn("open-problems.md", report)
            self.assertIn(
                f"**Attempt path:** `{items[0].attempt.directory}`",
                report,
            )

            compact = human_review.render_human_review_report(
                items,
                include_contents=False,
            )
            self.assertNotIn("HIGH BODY", compact)

            dashboard = human_review.render_human_review_html(items)
            self.assertIn(human_review._review_model_script(), dashboard)
            self.assertIn('id="triage-filter"', dashboard)
            self.assertIn('[triageFilter, "triage"]', dashboard)
            dashboard_data = human_review._html_data(
                items,
                include_contents=True,
            )
            self.assertEqual(
                dashboard_data[0]["attemptDirectory"],
                str(items[0].attempt.directory),
            )
            self.assertEqual(
                dashboard_data[0]["attemptDisplayPath"],
                human_review._project_display_path(
                    items[0].attempt.directory
                ),
            )
            self.assertEqual(dashboard_data[0]["totalAttemptCount"], 2)
            self.assertEqual(
                dashboard_data[0]["paperPublished"],
                "2024-01-02T00:00:00Z",
            )
            self.assertEqual(dashboard_data[0]["paperProblemCount"], 2)
            self.assertGreater(dashboard_data[0]["paperActivityTimestamp"], 0)
            self.assertEqual(
                human_review._project_display_path(
                    human_review.PROJECT_ROOT / "papers" / "example"
                ),
                "papers/example",
            )
            self.assertIn("item.attemptDisplayPath", dashboard)
            self.assertEqual(
                human_review._extract_open_problem_markdown(
                    paper / "analysis" / "open-problems.md",
                    "OP-001",
                ),
                "Prove it.",
            )
            self.assertIn('id="problem-list"', dashboard)
            self.assertIn(
                "item.paperTitle, item.paperDirectory, item.paperAuthors",
                dashboard,
            )
            self.assertIn("Paper title/id, problem, attempt…", dashboard)
            self.assertNotIn('id="problem-select"', dashboard)
            self.assertIn('id="attempt-list"', dashboard)
            self.assertIn("function latestProblems(items)", dashboard)
            self.assertIn("function attemptsForProblem(items, problemKey)", dashboard)
            self.assertIn("right.attemptNumber - left.attemptNumber", dashboard)
            self.assertIn("item.totalAttemptCount", dashboard)
            self.assertIn("appendAttemptTags(button, item)", dashboard)
            self.assertNotIn("showing ${item.attemptName}", dashboard)
            self.assertIn('id="claim-filter"', dashboard)
            self.assertIn('id="paper-sort"', dashboard)
            self.assertIn("LooseEndsReviewModel.paperSortOptions", dashboard)
            self.assertIn("LooseEndsReviewModel.paperTitleWithYear", dashboard)
            self.assertIn('parameters.set("sort", paperSort.value)', dashboard)
            self.assertIn('id="correctness-filter"', dashboard)
            self.assertIn('id="coverage-filter"', dashboard)
            self.assertIn('id="importance-filter"', dashboard)
            self.assertIn('id="confidence-filter"', dashboard)
            self.assertIn('id="literature-filter"', dashboard)
            self.assertIn('id="filter-current"', dashboard)
            self.assertIn('id="filter-stale"', dashboard)
            self.assertIn("LooseEndsReviewModel.filterItems", dashboard)
            self.assertIn("LooseEndsReviewModel.filterOptions", dashboard)
            self.assertIn(
                '["resolution", "Any resolution claim"]',
                dashboard,
            )
            self.assertIn(
                '["partial_result", "Partial result"]',
                dashboard,
            )
            self.assertIn(
                "Exclude known full resolutions",
                dashboard,
            )
            self.assertIn(
                'filters.literature === "exclude-resolved"',
                dashboard,
            )
            self.assertIn(
                "if (!item.current && !filters.stale) return false;",
                dashboard,
            )
            self.assertIn(
                '["solution", "counterexample"].includes(item.claimedResultType)',
                dashboard,
            )
            self.assertIn(
                'filters.correctness === "credible"',
                dashboard,
            )
            self.assertIn(
                'filters.coverage === "complete_any"',
                dashboard,
            )
            self.assertIn(
                '["major_or_resolution", "Major or resolution"]',
                dashboard,
            )
            self.assertIn("HIGH BODY", dashboard)
            self.assertIn('"problemStatement": "Construct it."', dashboard)
            self.assertIn("Open problem statement", dashboard)
            self.assertIn(
                "summaryCards(item).forEach(card =>",
                dashboard,
            )
            self.assertIn(
                "repeat(2, minmax(0, 1fr))",
                dashboard,
            )
            self.assertIn("file:///", dashboard)
            self.assertIn('tab: "attempt"', dashboard)
            self.assertIn('history.scrollRestoration = "manual"', dashboard)
            self.assertIn("const pageScrollPositions = new Map()", dashboard)
            self.assertIn("function navigateToItem(item)", dashboard)
            self.assertIn('window.addEventListener("popstate"', dashboard)
            self.assertIn("restorePageScroll(renderedItemId", dashboard)
            self.assertIn('parameters.set("q", query)', dashboard)
            self.assertIn(
                'parameters.get("q") || ""',
                dashboard,
            )
            self.assertIn("applyControlsFromLocation()", dashboard)
            self.assertIn("identityFromSearchParams(parameters)", dashboard)
            self.assertIn("identityToSearchParams(parameters, item)", dashboard)
            self.assertIn("filtersFromSearchParams(", dashboard)
            self.assertIn("filtersToSearchParams(", dashboard)
            self.assertIn(
                human_review.REVIEW_TOKENS_PATH.read_text(encoding="utf-8"),
                dashboard,
            )
            self.assertIn("katex@0.17.0", dashboard)
            self.assertIn("markdown-it@14.3.0", dashboard)
            self.assertIn("@mdit/plugin-katex@1.0.1", dashboard)
            self.assertIn('delimiters: "all"', dashboard)
            self.assertIn("html: false", dashboard)
            self.assertNotIn("renderMathInElement", dashboard)
            self.assertNotIn("auto-render.min.js", dashboard)
            self.assertLess(
                dashboard.index('["attempt", "Solution attempt"]'),
                dashboard.index('["critique", "Critique"]'),
            )

            empty_paper = make_analyzed_paper(Path(temporary) / "empty")
            empty_problems = common.discover_problem_refs([empty_paper])
            coverage_problems = [*problems, *empty_problems]
            coverage_data = human_review._html_data(
                items,
                include_contents=True,
                problems=coverage_problems,
            )
            self.assertEqual(
                [
                    entry["attemptStatus"]
                    for entry in coverage_data
                ].count("reviewed"),
                2,
            )
            self.assertEqual(
                [
                    entry["attemptStatus"]
                    for entry in coverage_data
                ].count("unreviewed"),
                1,
            )
            self.assertEqual(
                [
                    entry["attemptStatus"]
                    for entry in coverage_data
                ].count("unattempted"),
                2,
            )
            unattempted = next(
                entry
                for entry in coverage_data
                if entry["attemptStatus"] == "unattempted"
            )
            self.assertEqual(unattempted["attemptName"], "")
            self.assertEqual(unattempted["totalAttemptCount"], 0)
            self.assertIn("analysis/open-problems.md", {
                file["label"] for file in unattempted["files"]
            })
            coverage_dashboard = human_review.render_human_review_html(
                items,
                problems=coverage_problems,
                initial_priorities={"high", "medium"},
            )
            self.assertIn('id="attempt-status-filter"', coverage_dashboard)
            self.assertIn(
                '["unattempted", "Unattempted"]',
                coverage_dashboard,
            )
            self.assertIn(
                "attempted, awaiting review",
                coverage_dashboard,
            )
            self.assertIn(
                'item.literatureStatus === "resolved"',
                coverage_dashboard,
            )
            self.assertIn(
                "appendAttemptTags(button, item, { includeKnown: true })",
                coverage_dashboard,
            )
            self.assertIn('"attemptStatus": "unattempted"', coverage_dashboard)
            self.assertIn('"initialPriorities": ["high", "medium"]', coverage_dashboard)

            empty_output = Path(temporary) / "empty-review.html"
            with redirect_stdout(StringIO()):
                returncode = human_review.main(
                    [
                        str(empty_paper),
                        "--summary-only",
                        "--no-open",
                        "--output",
                        str(empty_output),
                    ]
                )
            self.assertEqual(returncode, 0)
            empty_dashboard = empty_output.read_text(encoding="utf-8")
            self.assertIn(
                '"attemptStatus": "unattempted"',
                empty_dashboard,
            )
            self.assertIn(
                '"initialPriorities": ["high", "low", "medium", "none"]',
                empty_dashboard,
            )

    def test_human_review_converts_project_root_only_once(self):
        human_review._native_project_root.cache_clear()
        self.addCleanup(human_review._native_project_root.cache_clear)
        with patch.object(
            codex_cli,
            "path_for_codex",
            return_value=r"C:\Project\loose-ends",
        ) as convert, patch.object(
            Path,
            "resolve",
            side_effect=AssertionError("absolute links must not be resolved"),
        ):
            first = human_review._browser_file_uri(
                human_review.PROJECT_ROOT / "papers" / "first paper.pdf"
            )
            second = human_review._browser_file_uri(
                human_review.PROJECT_ROOT / "papers" / "second.pdf"
            )
            display = human_review._project_display_path(
                human_review.PROJECT_ROOT / "papers" / "second.pdf"
            )

        convert.assert_called_once_with(human_review.PROJECT_ROOT)
        self.assertEqual(
            first,
            "file:///C:/Project/loose-ends/papers/first%20paper.pdf",
        )
        self.assertEqual(
            second,
            "file:///C:/Project/loose-ends/papers/second.pdf",
        )
        self.assertEqual(display, "papers/second.pdf")

    def test_cygwin_browser_opener_uses_graphical_system_browser(self):
        url = "http://localhost:35007/"
        with patch.object(
            human_review.sys,
            "platform",
            "cygwin",
        ), patch.object(human_review.subprocess, "Popen") as popen:
            opened = human_review.open_in_browser(url)

        self.assertTrue(opened)
        popen.assert_called_once_with(
            ["cygstart", url],
            stdout=human_review.subprocess.DEVNULL,
            stderr=human_review.subprocess.DEVNULL,
        )

    def test_solver_recovers_core_artifact_mislisting_without_new_turn(self):
        with TemporaryDirectory() as temporary:
            paper = make_analyzed_paper(Path(temporary))
            problem = common.discover_problem_refs(
                [paper],
                problem_ids={"OP-001"},
            )[0]
            problem.directory.mkdir(parents=True)
            workspace = problem.directory / ".solve-run-preserved"
            workspace.mkdir()
            (workspace / "attempt.md").write_text(
                "# OP-001 adaptive attempt\n\n## Checkable claims\n\n"
                "C-001 is a lemma.\n",
                encoding="utf-8",
            )
            common.write_json(
                workspace / "agent-result.json",
                {
                    "claimed_result_type": "partial_result",
                    "summary": "A lemma was proved.",
                    "external_sources": [],
                    "checkable_claims": [
                        {
                            "id": "C-001",
                            "type": "lemma",
                            "statement": "The lemma.",
                            "support": "A proof.",
                            "remaining_gap": "The theorem remains.",
                        }
                    ],
                    "artifacts": ["attempt.md"],
                    "warnings": [],
                },
            )
            write_run_files(workspace)
            guidance = {
                **solve_open_problems.generic_guidance(),
                "instruction": "Prove a lemma, adapting as needed.",
            }
            work = solve_open_problems.SolveWork(
                problem,
                guidance,
                1,
                None,
            )
            options = codex_cli.ModelOptions()
            solve_open_problems._write_work_record(
                workspace,
                work,
                config_digest="config",
                codex_version="test",
                options=options,
                prior_history_digest=common.attempt_history_digest(problem),
            )

            with (
                patch.object(
                    codex_cli,
                    "run_structured_codex",
                ) as run,
                patch.object(
                    codex_cli,
                    "normalize_workspace_access",
                ) as normalize,
            ):
                outcome = solve_open_problems.solve_work(
                    work,
                    codex="codex",
                    codex_version="test",
                    prompt_template="prompt",
                    schema_path=Path("schema"),
                    config_digest="config",
                    options=options,
                    launch_interval=0,
                )

            run.assert_not_called()
            normalize.assert_called_once_with(workspace, "codex")
            self.assertIn("recovered", outcome.message)
            self.assertTrue(workspace.is_dir())
            result = common.read_json(
                outcome.attempt.directory / "solver-result.json"
            )
            self.assertEqual(result["artifacts"], [])
            manifest = common.read_json(
                outcome.attempt.directory / "manifest.json"
            )
            self.assertEqual(
                manifest["recovered_from_workspace"],
                ".solve-run-preserved",
            )

    def test_solver_recovers_loose_claim_ids_and_solution_filename(self):
        with TemporaryDirectory() as temporary:
            paper = make_analyzed_paper(Path(temporary))
            problem = common.discover_problem_refs(
                [paper], problem_ids={"OP-001"}
            )[0]
            problem.directory.mkdir(parents=True)
            workspace = problem.directory / ".solve-run-preserved"
            workspace.mkdir()
            (workspace / "solution.md").write_text(
                "# A substantive solution note\n\nThe detailed proof is here.\n",
                encoding="utf-8",
            )
            (workspace / "certificate.txt").write_text(
                "Checked independently.\n",
                encoding="utf-8",
            )
            common.write_json(
                workspace / "agent-result.json",
                {
                    "claimed_result_type": "partial_result",
                    "summary": "A lemma was proved.",
                    "external_sources": [],
                    "checkable_claims": [
                        {
                            "id": "C1",
                            "type": "lemma",
                            "statement": "The lemma.",
                            "support": "C1 supplies a proof.",
                            "remaining_gap": "The theorem remains.",
                        }
                    ],
                    "artifacts": [
                        f"[Proof write-up]({workspace.as_posix()}/solution.md:1)",
                        f"[Certificate]({workspace.as_posix()}/certificate.txt:1)",
                    ],
                    "warnings": [],
                },
            )
            write_run_files(workspace)
            work = solve_open_problems.SolveWork(
                problem,
                solve_open_problems.generic_guidance(),
                1,
                None,
            )
            options = codex_cli.ModelOptions()
            solve_open_problems._write_work_record(
                workspace,
                work,
                config_digest="config",
                codex_version="test",
                options=options,
                prior_history_digest=common.attempt_history_digest(problem),
            )

            with patch.object(
                codex_cli,
                "run_structured_codex",
            ) as run:
                outcome = solve_open_problems.solve_work(
                    work,
                    codex="codex",
                    codex_version="test",
                    prompt_template="prompt",
                    schema_path=Path("schema"),
                    config_digest="config",
                    options=options,
                    launch_interval=0,
                )

            run.assert_not_called()
            result = common.read_json(
                outcome.attempt.directory / "solver-result.json"
            )
            self.assertEqual(result["checkable_claims"][0]["id"], "C-001")
            self.assertEqual(
                result["checkable_claims"][0]["support"],
                "C-001 supplies a proof.",
            )
            self.assertEqual(result["artifacts"], ["artifacts/certificate.txt"])
            self.assertTrue(
                (
                    outcome.attempt.directory
                    / "artifacts"
                    / "certificate.txt"
                ).is_file()
            )
            attempt = (outcome.attempt.directory / "attempt.md").read_text(
                encoding="utf-8"
            )
            self.assertIn("The detailed proof is here.", attempt)
            self.assertIn("### C-001", attempt)
            self.assertTrue(
                any("solution.md" in warning for warning in result["warnings"])
            )

    def test_solver_recovers_markdown_blocked_by_windows_sandbox(self):
        with TemporaryDirectory() as temporary:
            paper = make_analyzed_paper(Path(temporary))
            problem = common.discover_problem_refs(
                [paper],
                problem_ids={"OP-001"},
            )[0]
            work = solve_open_problems.SolveWork(
                problem,
                solve_open_problems.generic_guidance(),
                1,
                None,
            )
            workspace = problem.directory / ".solve-run-blocked"
            workspace.mkdir(parents=True)
            common.write_json(
                workspace / "agent-result.json",
                {
                    "claimed_result_type": "partial_result",
                    "summary": "A lemma was proved.",
                    "external_sources": [],
                    "checkable_claims": [
                        {
                            "id": "C-001",
                            "type": "lemma",
                            "statement": "The lemma holds.",
                            "support": "Here is its proof.",
                            "remaining_gap": "The theorem remains.",
                        }
                    ],
                    "artifacts": [],
                    "warnings": [],
                },
            )
            (workspace / "events.jsonl").write_text(
                '{"type":"turn.completed"}\n',
                encoding="utf-8",
            )
            (workspace / "run.log").write_text(
                "windows sandbox: helper_unknown_error: "
                "apply deny-read ACLs\n",
                encoding="utf-8",
            )

            self.assertTrue(
                solve_open_problems.recover_missing_attempt_markdown(
                    work,
                    workspace,
                )
            )
            attempt = (workspace / "attempt.md").read_text(encoding="utf-8")
            self.assertIn("Driver recovery notice", attempt)
            self.assertIn("C-001", attempt)
            result, artifacts = solve_open_problems.validate_solver_result(
                workspace / "agent-result.json",
                workspace,
            )
            self.assertEqual(result["claimed_result_type"], "partial_result")
            self.assertEqual(artifacts, [])

    def test_response_schemas_avoid_unsupported_unique_items(self):
        schemas = Path(__file__).resolve().parents[1] / "schemas"
        for path in schemas.glob("*.json"):
            with self.subTest(schema=path.name):
                self.assertNotIn(
                    '"uniqueItems"',
                    path.read_text(encoding="utf-8"),
                )

    def test_triage_can_handoff_its_exact_selection_to_solver(self):
        with TemporaryDirectory() as temporary:
            paper = make_analyzed_paper(Path(temporary))
            problems = common.discover_problem_refs([paper])
            prompt = triage_open_problems.DEFAULT_PROMPT_PATH.read_text(
                encoding="utf-8"
            )
            schema_text = (
                triage_open_problems.DEFAULT_SCHEMA_PATH.read_text(
                    encoding="utf-8"
                )
            )
            config_digest = codex_cli.semantic_config_digest(
                prompt,
                schema_text,
                codex_cli.ModelOptions(
                    codex_cli.DEFAULT_MODEL,
                    codex_cli.DEFAULT_REASONING_EFFORT,
                ),
            )
            for problem in problems:
                problem.directory.mkdir(parents=True, exist_ok=True)
                result = {
                    "problem_id": problem.id,
                    "classification": "attempt",
                    "rationale": "Promising.",
                    "promising_features": [],
                    "obstacles": [],
                    "suggested_approaches": [
                        {
                            "id": "proof",
                            "mode": "proof",
                            "suggestion": "Try a proof.",
                            "why_promising": "The paper has useful machinery.",
                            "abandon_if": "A necessary lemma is false.",
                        }
                    ],
                }
                common.write_json(
                    problem.directory / "triage.json",
                    result,
                )
                (problem.directory / "triage.md").write_text(
                    f"# Triage {problem.id}\n",
                    encoding="utf-8",
                )
                common.write_json(
                    problem.directory / "triage-manifest.json",
                    {
                        "schema_version": (
                            common.TRIAGE_MANIFEST_SCHEMA_VERSION
                        ),
                        "input_digest": common.triage_input_digest(problem),
                        "config_digest": config_digest,
                    },
                )
            output = StringIO()
            with (
                patch.object(
                    solve_open_problems,
                    "main",
                    return_value=0,
                ) as solve_main,
                redirect_stdout(output),
            ):
                returncode = triage_open_problems.main(
                    [str(paper), "--solve", "attempt"]
                )

            self.assertEqual(returncode, 0)
            arguments = solve_main.call_args.args[0]
            self.assertIn("--from-triage", arguments)
            self.assertEqual(arguments.count("--exact-problem"), 2)
            self.assertIn("Feeding 2 current triage item(s)", output.getvalue())

    def test_solver_combines_all_triage_suggestions_in_one_attempt(self):
        with TemporaryDirectory() as temporary:
            paper = make_analyzed_paper(Path(temporary))
            problem = common.discover_problem_refs(
                [paper],
                problem_ids={"OP-001"},
            )[0]
            problem.directory.mkdir(parents=True)
            suggestions = [
                {
                    "id": "compute",
                    "mode": "computation",
                    "suggestion": "Compute examples.",
                    "why_promising": "Small cases may expose structure.",
                    "abandon_if": "The data contradicts the pattern.",
                },
                {
                    "id": "prove",
                    "mode": "proof",
                    "suggestion": "Try the structural lemma.",
                    "why_promising": "It would settle the conjecture.",
                    "abandon_if": "A small case refutes the lemma.",
                },
                {
                    "id": "counterexample",
                    "mode": "counterexample",
                    "suggestion": "Search the boundary cases.",
                    "why_promising": "The hypotheses may be too weak.",
                    "abandon_if": "The paper's invariant rules them out.",
                },
            ]
            common.write_json(
                problem.directory / common.TRIAGE_RESULT,
                {
                    "problem_id": problem.id,
                    "classification": "attempt",
                    "rationale": "Several related avenues look useful.",
                    "promising_features": ["R-001"],
                    "obstacles": ["A missing estimate"],
                    "suggested_approaches": suggestions,
                },
            )
            common.write_json(
                problem.directory / common.TRIAGE_MANIFEST,
                {
                    "schema_version": (
                        common.TRIAGE_MANIFEST_SCHEMA_VERSION
                    ),
                    "input_digest": common.triage_input_digest(problem),
                },
            )
            work_items, missing, resolved = solve_open_problems.build_work(
                [problem],
                require_triage_classes={"attempt"},
            )

            self.assertEqual(missing, [])
            self.assertEqual(resolved, [])
            self.assertEqual(len(work_items), 1)
            self.assertEqual(
                work_items[0].guidance["suggested_approaches"],
                suggestions,
            )

            calls: list[solve_open_problems.SolveWork] = []
            finished: list[
                tuple[
                    solve_open_problems.SolveWork,
                    solve_open_problems.SolveOutcome | None,
                    str | None,
                ]
            ] = []

            def fake_solve(work, **kwargs):
                calls.append(work)
                directory = problem.directory / work.attempt_name
                attempt = review_solutions.AttemptRef(
                    problem,
                    directory,
                    {
                        "claimed_result_type": "partial_result",
                        "checkable_claims": [{"id": "C-001"}],
                    },
                )
                return solve_open_problems.SolveOutcome(
                    work,
                    attempt,
                    "partial_result",
                    1,
                    "partial progress",
                )

            with patch.object(
                solve_open_problems,
                "solve_work",
                side_effect=fake_solve,
            ):
                outcomes, failures = solve_open_problems.solve_many(
                    work_items,
                    codex="codex",
                    codex_version="test",
                    prompt_template="prompt",
                    schema_path=Path("schema"),
                    config_digest="config",
                    options=codex_cli.ModelOptions(),
                    jobs=2,
                    on_finished=lambda work, outcome, error: finished.append(
                        (work, outcome, error)
                    ),
                )

            self.assertEqual(failures, [])
            self.assertEqual(len(outcomes), 1)
            self.assertEqual(calls, work_items)
            self.assertEqual(len(finished), 1)
            self.assertIs(finished[0][0], work_items[0])
            self.assertIs(finished[0][1], outcomes[0])
            self.assertIsNone(finished[0][2])

    def test_solver_accepts_problem_directories_as_exact_selection(self):
        with TemporaryDirectory() as temporary:
            paper = make_analyzed_paper(Path(temporary))
            paths = [
                paper / "OP-001",
                paper / "OP-002",
            ]
            output = StringIO()
            with redirect_stdout(output):
                returncode = solve_open_problems.main(
                    [*(str(path) for path in paths), "--dry-run"]
                )

            self.assertEqual(returncode, 0)
            text = output.getvalue()
            self.assertIn("OP-001/attempt-001", text)
            self.assertIn("OP-002/attempt-001", text)
            self.assertIn("Selected 2 problem(s)", text)

    def test_solver_aborts_batch_when_secure_sandbox_preflight_fails(self):
        with TemporaryDirectory() as temporary:
            paper = make_analyzed_paper(Path(temporary))
            error = StringIO()
            with (
                patch.object(
                    codex_cli,
                    "resolve_codex_executable",
                    return_value="codex",
                ),
                patch.object(
                    codex_cli,
                    "read_codex_version",
                    return_value="test",
                ),
                patch.object(
                    codex_cli,
                    "require_secure_windows_sandbox",
                    side_effect=common.CodexError("sandbox unavailable"),
                ) as preflight,
                patch.object(solve_open_problems, "solve_many") as solve_many,
                redirect_stderr(error),
            ):
                returncode = solve_open_problems.main(
                    [str(paper / "OP-001")]
                )

            self.assertEqual(returncode, 1)
            preflight.assert_called_once_with(
                "codex",
                solve_open_problems.PROJECT_ROOT,
            )
            solve_many.assert_not_called()
            self.assertEqual(
                error.getvalue().count("sandbox unavailable"),
                1,
            )
            self.assertFalse(any((paper / "OP-001").glob(".solve-run-*")))

    def test_solver_repeats_rounds_until_critic_confirms_resolution(self):
        with TemporaryDirectory() as temporary:
            paper = make_analyzed_paper(Path(temporary))
            events: list[str] = []
            solve_round = 0

            def fake_solve_many(work_items, **kwargs):
                nonlocal solve_round
                solve_round += 1
                events.append(f"solve-{solve_round}")
                work = work_items[0]
                directory = work.problem.directory / work.attempt_name
                directory.mkdir(parents=True)
                claimed = (
                    "partial_result" if solve_round == 1 else "solution"
                )
                solver_result = {
                    "claimed_result_type": claimed,
                    "checkable_claims": [{"id": "C-001"}],
                }
                common.write_json(
                    directory / "solver-result.json",
                    solver_result,
                )
                attempt = review_solutions.AttemptRef(
                    work.problem,
                    directory,
                    solver_result,
                )
                outcome = solve_open_problems.SolveOutcome(
                    work,
                    attempt,
                    claimed,
                    1,
                    claimed,
                )
                if kwargs.get("on_finished") is not None:
                    kwargs["on_finished"](work, outcome, None)
                return [outcome], []

            def fake_review_many(attempts, **kwargs):
                events.append(f"review-{solve_round}")
                attempt = attempts[0]
                if solve_round == 1:
                    coverage = "partial"
                    importance = "major"
                else:
                    coverage = "complete"
                    importance = "resolution"
                outcome = review_solutions.ReviewOutcome(
                    attempt,
                    "reviewed",
                    "well_supported",
                    coverage,
                    importance,
                    "high",
                    "reviewed",
                )
                common.write_json(
                    attempt.directory / "review-result.json",
                    {"reviewed_coverage": coverage},
                )
                if kwargs.get("on_finished") is not None:
                    kwargs["on_finished"](attempt, outcome, None)
                return [outcome], []

            output = StringIO()
            with (
                patch.object(
                    solve_open_problems,
                    "solve_many",
                    side_effect=fake_solve_many,
                ),
                patch.object(
                    review_solutions,
                    "review_many",
                    side_effect=fake_review_many,
                ),
                patch.object(
                    codex_cli,
                    "resolve_codex_executable",
                    return_value="codex",
                ),
                patch.object(
                    codex_cli,
                    "read_codex_version",
                    return_value="test",
                ),
                patch.object(
                    codex_cli,
                    "require_secure_windows_sandbox",
                ) as preflight,
                redirect_stdout(output),
            ):
                returncode = solve_open_problems.main(
                    [
                        str(paper / "OP-001"),
                        "--max-rounds",
                        "3",
                        "--review",
                        "all",
                    ]
                )

            self.assertEqual(returncode, 0)
            preflight.assert_called_once_with(
                "codex",
                solve_open_problems.PROJECT_ROOT,
            )
            self.assertEqual(
                events,
                ["solve-1", "review-1", "solve-2", "review-2"],
            )
            self.assertTrue((paper / "OP-001" / "attempt-001").is_dir())
            self.assertTrue((paper / "OP-001" / "attempt-002").is_dir())
            self.assertFalse((paper / "OP-001" / "attempt-003").exists())
            self.assertIn(
                "Critic confirmed a complete resolution",
                output.getvalue(),
            )
            self.assertIn(
                "1 critic-confirmed resolution(s)",
                output.getvalue(),
            )

    def test_other_problem_commands_accept_exact_problem_directories(self):
        with TemporaryDirectory() as temporary:
            paper = make_analyzed_paper(Path(temporary))
            problem = common.discover_problem_refs(
                [paper], problem_ids={"OP-001"}
            )[0]
            attempt = problem.directory / "attempt-001"
            attempt.mkdir(parents=True)
            common.write_json(
                attempt / "solver-result.json",
                {
                    "claimed_result_type": "none",
                    "checkable_claims": [],
                },
            )

            commands = (
                (triage_open_problems.main, [paper / "OP-001"], "1 problem(s)"),
                (literature_review.main, [paper / "OP-002"], "1 problem(s)"),
                (
                    review_solutions.main,
                    [attempt, "--mode", "all"],
                    "1 attempt(s)",
                ),
            )
            for command, arguments, expected in commands:
                output = StringIO()
                with redirect_stdout(output):
                    returncode = command(
                        [*(str(argument) for argument in arguments), "--dry-run"]
                    )
                self.assertEqual(returncode, 0)
                self.assertIn(expected, output.getvalue())

    def test_step_based_triage_manifest_is_deliberately_stale(self):
        with TemporaryDirectory() as temporary:
            paper = make_analyzed_paper(Path(temporary))
            problem = common.discover_problem_refs(
                [paper],
                problem_ids={"OP-001"},
            )[0]
            problem.directory.mkdir(parents=True)
            common.write_json(
                problem.directory / common.TRIAGE_RESULT,
                {
                    "problem_id": problem.id,
                    "classification": "attempt",
                    "rationale": "Old format.",
                    "promising_features": [],
                    "obstacles": [],
                    "next_steps": [],
                },
            )
            common.write_json(
                problem.directory / common.TRIAGE_MANIFEST,
                {
                    "schema_version": 1,
                    "input_digest": common.triage_input_digest(problem),
                },
            )

            self.assertFalse(common.triage_is_current(problem))

    def test_all_cli_entry_points_share_frontier_model_defaults(self):
        parsers = (
            analyze_papers.build_parser(),
            triage_open_problems.build_parser(),
            solve_open_problems.build_parser(),
            review_solutions.build_parser(),
            literature_review.build_parser(),
        )
        arguments = (
            ["paper"],
            ["paper"],
            ["paper", "--from-triage", "attempt"],
            ["paper"],
            ["paper", "--all-problems"],
        )
        for parser, argv in zip(parsers, arguments):
            with self.subTest(program=parser.prog):
                parsed = parser.parse_args(argv)
                self.assertEqual(parsed.model, "gpt-5.6-sol")
                self.assertEqual(parsed.reasoning_effort, "xhigh")
                self.assertIsNone(parsed.prompt)
                self.assertIsInstance(parsed.prompt_template, Path)

        solver = solve_open_problems.build_parser().parse_args(
            ["paper", "--all-problems"]
        )
        reviewer = review_solutions.build_parser().parse_args(["paper"])
        literature = literature_review.build_parser().parse_args(
            ["paper", "--all-problems"]
        )
        self.assertEqual(solver.web_search, "live")
        self.assertEqual(solver.max_rounds, 1)
        self.assertIsNone(solver.prompt)
        self.assertEqual(
            solver.prompt_template,
            solve_open_problems.DEFAULT_PROMPT_PATH,
        )
        directed_solver = solve_open_problems.build_parser().parse_args(
            [
                "paper",
                "--all-problems",
                "--prompt",
                "Prioritize a counterexample search",
                "--review-prompt",
                "Check the boundary case independently",
            ]
        )
        self.assertEqual(
            directed_solver.prompt,
            "Prioritize a counterexample search",
        )
        self.assertEqual(
            directed_solver.review_prompt,
            "Check the boundary case independently",
        )
        self.assertEqual(
            directed_solver.review_prompt_template,
            review_solutions.DEFAULT_PROMPT_PATH,
        )
        self.assertEqual(
            solve_open_problems.build_parser()
            .parse_args(["paper", "--all-problems", "-r", "3"])
            .max_rounds,
            3,
        )
        self.assertEqual(reviewer.web_search, "live")
        self.assertEqual(literature.web_search, "live")

    def test_live_web_search_does_not_enable_shell_network_or_plugins(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            command = codex_cli.build_exec_command(
                codex="codex",
                workspace=root,
                prompt="prompt",
                schema_path=root / "schema.json",
                result_path=root / "result.json",
                options=codex_cli.ModelOptions(),
                web_search="live",
            )

        self.assertIn('web_search="live"', command)
        self.assertIn("sandbox_workspace_write.network_access=false", command)
        self.assertIn("apps._default.enabled=false", command)
        self.assertIn("agents.enabled=false", command)
        self.assertNotIn("prompt", command)
        self.assertEqual(command[-1], "-")


if __name__ == "__main__":
    unittest.main()
