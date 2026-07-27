from pathlib import Path
from contextlib import redirect_stdout
from io import StringIO
import json
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import codex_cli
import analyze_papers
import open_problem_common as common
import review_solutions
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
            work, missing = solve_open_problems.build_work(
                [problem],
                require_triage_classes={"attempt"},
            )
            self.assertEqual(missing, [])
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
                        "status": "partial_progress",
                        "summary": "Proved a special case.",
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
                        "verdict": "plausible_progress",
                        "attention": "medium",
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

            self.assertEqual(review_outcome.attention, "medium")
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
            stale_work, skipped = solve_open_problems.build_work(
                [problem],
                require_triage_classes={"attempt"},
            )
            self.assertEqual(stale_work, [])
            self.assertEqual(skipped, [problem])

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
                "status": "no_checkable_progress",
                "summary": "No concrete advance.",
                "checkable_claims": [],
                "artifacts": [],
                "warnings": [],
            }
            attempt = review_solutions.AttemptRef(problem, directory, result)
            self.assertFalse(review_solutions.is_promising(attempt))

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
                    "status": "partial_progress",
                    "summary": "A lemma was proved.",
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
            work_items, missing = solve_open_problems.build_work(
                [problem],
                require_triage_classes={"attempt"},
            )

            self.assertEqual(missing, [])
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
                        "status": "partial_progress",
                        "checkable_claims": [{"id": "C-001"}],
                    },
                )
                return solve_open_problems.SolveOutcome(
                    work,
                    attempt,
                    "partial_progress",
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
        )
        arguments = (
            ["paper"],
            ["paper"],
            ["paper", "--from-triage", "attempt"],
            ["paper"],
        )
        for parser, argv in zip(parsers, arguments):
            with self.subTest(program=parser.prog):
                parsed = parser.parse_args(argv)
                self.assertEqual(parsed.model, "gpt-5.6-sol")
                self.assertEqual(parsed.reasoning_effort, "xhigh")


if __name__ == "__main__":
    unittest.main()
