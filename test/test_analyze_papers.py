from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
import json
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import analyze_papers
import codex_cli
from analyze_papers import (
    AnalysisError,
    analyze_paper,
    discover_paper_directories,
    recover_complete_analysis,
    source_digest,
)


PROMPT_TEMPLATE = analyze_papers.DEFAULT_PROMPT_PATH.read_text(encoding="utf-8")
SCHEMA_TEXT = analyze_papers.DEFAULT_SCHEMA_PATH.read_text(encoding="utf-8")


def make_paper(root: Path, name: str = "arXiv-1706.03762") -> Path:
    paper = root / name
    source = paper / "source"
    source.mkdir(parents=True)
    (paper / "paper.pdf").write_bytes(b"%PDF-test")
    (source / "main.tex").write_text(
        "\\section{Introduction}\nAn open problem remains.\n",
        encoding="utf-8",
    )
    return paper


def successful_codex_run(
    commands: list[list[str]],
    *,
    require_staged_inputs: bool = True,
):
    def run(command, **kwargs):
        commands.append(command)
        workspace = Path(kwargs["cwd"])
        staged_paper = workspace / analyze_papers.STAGED_PAPER_DIRECTORY
        if (
            require_staged_inputs
            and not (staged_paper / "paper.pdf").is_file()
        ):
            raise AssertionError("paper.pdf was not staged inside the workspace")
        if (
            require_staged_inputs
            and not (staged_paper / "source" / "main.tex").is_file()
        ):
            raise AssertionError("source/main.tex was not staged inside the workspace")
        (workspace / "summary.md").write_text(
            "# Summary\n\nA summary.\n",
            encoding="utf-8",
        )
        (workspace / "results.md").write_text(
            "# Results\n\n## R-001\n\nA result.\n",
            encoding="utf-8",
        )
        (workspace / "open-problems.md").write_text(
            "# Open Problems\n\n## OP-001: A question\n\nUnresolved.\n",
            encoding="utf-8",
        )
        result_path = workspace / "agent-result.json"
        result_path.write_text(
            json.dumps(
                {
                    "status": "complete",
                    "paper_title": "Test Paper",
                    "paper_authors": ["Ada Lovelace", "Alan Turing"],
                    "open_problems": [
                        {
                            "id": "OP-001",
                            "title": "A question",
                            "explicitness": "explicit",
                        }
                    ],
                    "warnings": [],
                }
            ),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(command, 0)

    return run


class PaperDiscoveryTests(unittest.TestCase):
    def test_accepts_a_direct_paper_and_discovers_nested_papers(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = make_paper(root / "author-a", "arXiv-1706.03762")
            second = make_paper(root / "author-b", "arXiv-cs_9910024v2")

            direct = discover_paper_directories([first])
            nested = discover_paper_directories([root])

            self.assertEqual(direct, [first.resolve()])
            self.assertEqual(nested, sorted([first.resolve(), second.resolve()]))

    def test_rejects_a_directory_without_downloaded_papers(self):
        with TemporaryDirectory() as temporary:
            with self.assertRaises(AnalysisError):
                discover_paper_directories([Path(temporary)])


class PaperDigestTests(unittest.TestCase):
    def test_digest_changes_with_source_but_not_analysis(self):
        with TemporaryDirectory() as temporary:
            paper = make_paper(Path(temporary))
            initial = source_digest(paper)

            analysis = paper / "analysis"
            analysis.mkdir()
            (analysis / "summary.md").write_text("generated", encoding="utf-8")
            self.assertEqual(source_digest(paper), initial)

            (paper / "source" / "main.tex").write_text(
                "changed",
                encoding="utf-8",
            )
            self.assertNotEqual(source_digest(paper), initial)


class CodexPathTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "cygwin", "requires Cygwin Python")
    def test_converts_cygwin_path_for_windows_codex(self):
        converted = analyze_papers.path_for_codex(Path.cwd())

        self.assertRegex(converted, r"^[A-Za-z]:\\")
        self.assertNotIn("/cygdrive/", converted)

    def test_acl_repair_removes_explicit_deny_before_granting_access(self):
        with (
            TemporaryDirectory() as temporary,
            patch.object(codex_cli, "is_windows_host", return_value=True),
            patch.object(
                codex_cli,
                "workspace_is_user_accessible",
                side_effect=[False, True],
            ),
            patch.object(
                codex_cli,
                "windows_identity",
                return_value=r"DOMAIN\user",
            ),
            patch.object(
                codex_cli,
                "windows_icacls_for_sandbox",
                return_value="icacls.exe",
            ),
            patch.object(codex_cli, "_run_local_command") as run_local,
        ):
            workspace = Path(temporary)
            codex_cli.normalize_workspace_access(workspace, "codex")

        self.assertEqual(run_local.call_count, 2)
        remove_command = run_local.call_args_list[0].args[0]
        grant_command = run_local.call_args_list[1].args[0]
        self.assertIn("/remove:d", remove_command)
        self.assertIn(r"DOMAIN\user", remove_command)
        self.assertIn("/grant", grant_command)
        self.assertIn(r"DOMAIN\user:(OI)(CI)(F)", grant_command)


class AnalyzePaperTests(unittest.TestCase):
    @patch.object(analyze_papers.subprocess, "run")
    def test_writes_three_markdown_files_and_manifest(self, run):
        with TemporaryDirectory() as temporary:
            paper = make_paper(Path(temporary))
            commands: list[list[str]] = []
            run.side_effect = successful_codex_run(commands)

            outcome = analyze_paper(
                paper,
                codex="codex",
                codex_version="codex-cli test",
                prompt_template=PROMPT_TEMPLATE,
                schema_path=analyze_papers.DEFAULT_SCHEMA_PATH,
                schema_text=SCHEMA_TEXT,
                launch_interval=0,
            )

            self.assertEqual(outcome.status, "analyzed")
            self.assertEqual(outcome.result_count, 1)
            self.assertEqual(outcome.open_problem_count, 1)
            self.assertIn("1 result, 1 open problem", outcome.message)
            analysis = paper / "analysis"
            for filename in analyze_papers.CONTENT_FILES:
                self.assertTrue((analysis / filename).is_file())
            self.assertTrue((analysis / "events.jsonl").is_file())
            self.assertTrue((analysis / "run.log").is_file())
            self.assertEqual(list(analysis.glob(".run-*")), [])

            manifest = json.loads(
                (analysis / "manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["paper_title"], "Test Paper")
            self.assertEqual(
                manifest["paper_authors"],
                ["Ada Lovelace", "Alan Turing"],
            )
            self.assertEqual(manifest["open_problems"][0]["id"], "OP-001")
            self.assertEqual(manifest["codex_version"], "codex-cli test")
            self.assertFalse(manifest["requested_fast_mode"])
            self.assertIsNone(manifest["requested_reasoning_effort"])

            command = commands[0]
            self.assertIn("--ephemeral", command)
            self.assertIn("shell_snapshot", command)
            self.assertIn("--json", command)
            self.assertIn("workspace-write", command)
            self.assertNotIn("--add-dir", command)
            self.assertIn(
                analyze_papers.STAGED_PAPER_DIRECTORY,
                command[-1],
            )

    @patch.object(analyze_papers.subprocess, "run")
    def test_passes_model_and_reasoning_effort_to_codex(self, run):
        with TemporaryDirectory() as temporary:
            paper = make_paper(Path(temporary))
            commands: list[list[str]] = []
            run.side_effect = successful_codex_run(commands)

            analyze_paper(
                paper,
                codex="codex",
                codex_version="codex-cli test",
                prompt_template=PROMPT_TEMPLATE,
                schema_path=analyze_papers.DEFAULT_SCHEMA_PATH,
                schema_text=SCHEMA_TEXT,
                model="gpt-5.6-sol",
                reasoning_effort="xhigh",
                fast=True,
                launch_interval=0,
            )

            command = commands[0]
            self.assertIn("gpt-5.6-sol", command)
            self.assertIn('model_reasoning_effort="xhigh"', command)
            self.assertIn("features.fast_mode=true", command)
            self.assertIn('service_tier="fast"', command)
            manifest = json.loads(
                (paper / "analysis" / "manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(manifest["requested_model"], "gpt-5.6-sol")
            self.assertEqual(manifest["requested_reasoning_effort"], "xhigh")
            self.assertTrue(manifest["requested_fast_mode"])

    @patch.object(analyze_papers.subprocess, "run")
    def test_skips_a_current_analysis_and_reruns_after_source_change(self, run):
        with TemporaryDirectory() as temporary:
            paper = make_paper(Path(temporary))
            commands: list[list[str]] = []
            run.side_effect = successful_codex_run(commands)
            arguments = {
                "codex": "codex",
                "codex_version": "codex-cli test",
                "prompt_template": PROMPT_TEMPLATE,
                "schema_path": analyze_papers.DEFAULT_SCHEMA_PATH,
                "schema_text": SCHEMA_TEXT,
                "launch_interval": 0,
            }

            first = analyze_paper(paper, **arguments)
            second = analyze_paper(paper, **arguments)
            (paper / "source" / "main.tex").write_text(
                "new version",
                encoding="utf-8",
            )
            third = analyze_paper(paper, **arguments)

            self.assertEqual(first.status, "analyzed")
            self.assertEqual(second.status, "current")
            self.assertEqual(second.result_count, 1)
            self.assertEqual(second.open_problem_count, 1)
            self.assertIn("1 result, 1 open problem", second.message)
            self.assertEqual(third.status, "analyzed")
            self.assertEqual(run.call_count, 2)

    @patch.object(analyze_papers.subprocess, "run")
    def test_failed_rerun_preserves_the_previous_analysis(self, run):
        with TemporaryDirectory() as temporary:
            paper = make_paper(Path(temporary))
            commands: list[list[str]] = []
            run.side_effect = successful_codex_run(commands)
            arguments = {
                "codex": "codex",
                "codex_version": "codex-cli test",
                "prompt_template": PROMPT_TEMPLATE,
                "schema_path": analyze_papers.DEFAULT_SCHEMA_PATH,
                "schema_text": SCHEMA_TEXT,
                "launch_interval": 0,
            }
            analyze_paper(paper, **arguments)
            old_summary = (paper / "analysis" / "summary.md").read_text(
                encoding="utf-8"
            )
            old_manifest = (paper / "analysis" / "manifest.json").read_text(
                encoding="utf-8"
            )

            (paper / "source" / "main.tex").write_text(
                "changed",
                encoding="utf-8",
            )
            run.side_effect = lambda command, **kwargs: subprocess.CompletedProcess(
                command,
                2,
            )
            with self.assertRaises(AnalysisError):
                analyze_paper(paper, **arguments)

            self.assertEqual(
                (paper / "analysis" / "summary.md").read_text(encoding="utf-8"),
                old_summary,
            )
            self.assertEqual(
                (paper / "analysis" / "manifest.json").read_text(
                    encoding="utf-8"
                ),
                old_manifest,
            )
            self.assertEqual(len(list((paper / "analysis").glob(".run-*"))), 1)

    @patch.object(analyze_papers.subprocess, "run")
    def test_cleanup_failure_does_not_reclassify_installed_analysis(self, run):
        with TemporaryDirectory() as temporary:
            paper = make_paper(Path(temporary))
            commands: list[list[str]] = []
            run.side_effect = successful_codex_run(commands)
            real_rmtree = analyze_papers.shutil.rmtree

            def fail_workspace_cleanup(path, *args, **kwargs):
                if Path(path).name.startswith(".run-"):
                    raise PermissionError("sandbox-owned temporary directory")
                return real_rmtree(path, *args, **kwargs)

            with patch.object(
                analyze_papers.shutil,
                "rmtree",
                side_effect=fail_workspace_cleanup,
            ):
                outcome = analyze_paper(
                    paper,
                    codex="codex",
                    codex_version="codex-cli test",
                    prompt_template=PROMPT_TEMPLATE,
                    schema_path=analyze_papers.DEFAULT_SCHEMA_PATH,
                    schema_text=SCHEMA_TEXT,
                    launch_interval=0,
                )

            analysis = paper / "analysis"
            self.assertEqual(outcome.status, "analyzed")
            self.assertIn("temporary workspace preserved", outcome.message)
            self.assertTrue((analysis / "manifest.json").is_file())
            self.assertEqual(len(list(analysis.glob(".run-*"))), 1)
            self.assertIn(
                "Driver cleanup warning:",
                (analysis / "run.log").read_text(encoding="utf-8"),
            )
            real_rmtree(next(analysis.glob(".run-*")))

    @patch.object(analyze_papers.subprocess, "run")
    def test_retries_windows_path_failure_before_thread_start(self, run):
        with TemporaryDirectory() as temporary:
            paper = make_paper(Path(temporary))
            commands: list[list[str]] = []
            success = successful_codex_run(commands)
            attempts = 0

            def fail_once(command, **kwargs):
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    kwargs["stderr"].write(
                        "Error: The system cannot find the path specified. "
                        "(os error 3)\n"
                    )
                    kwargs["stderr"].flush()
                    return subprocess.CompletedProcess(command, 1)
                return success(command, **kwargs)

            run.side_effect = fail_once
            outcome = analyze_paper(
                paper,
                codex="codex",
                codex_version="codex-cli test",
                prompt_template=PROMPT_TEMPLATE,
                schema_path=analyze_papers.DEFAULT_SCHEMA_PATH,
                schema_text=SCHEMA_TEXT,
                launch_interval=0,
            )

            self.assertEqual(outcome.status, "analyzed")
            self.assertEqual(run.call_count, 2)
            self.assertIn(
                "startup retry 2/3",
                (paper / "analysis" / "run.log").read_text(encoding="utf-8"),
            )

    @patch.object(analyze_papers, "grant_recovery_access")
    def test_recovers_a_complete_run_without_removing_it(self, grant_access):
        with TemporaryDirectory() as temporary:
            paper = make_paper(Path(temporary))
            analysis = paper / "analysis"
            workspace = analysis / ".run-preserved"
            workspace.mkdir(parents=True)
            commands: list[list[str]] = []
            successful_codex_run(
                commands,
                require_staged_inputs=False,
            )(
                ["codex", "exec"],
                cwd=workspace,
            )
            (workspace / "events.jsonl").write_text(
                '{"type":"turn.completed"}\n',
                encoding="utf-8",
            )
            (workspace / "run.log").write_text("", encoding="utf-8")

            outcome = recover_complete_analysis(
                paper,
                codex="codex",
                codex_version="codex-cli test",
            )

            self.assertEqual(outcome.status, "recovered")
            self.assertEqual(outcome.result_count, 1)
            self.assertEqual(outcome.open_problem_count, 1)
            self.assertIn("1 result, 1 open problem", outcome.message)
            self.assertTrue(workspace.is_dir())
            grant_access.assert_called_once_with(workspace, "codex")
            manifest = json.loads(
                (analysis / "manifest.json").read_text(encoding="utf-8")
            )
            self.assertTrue(manifest["recovered_without_config"])
            self.assertTrue(manifest["original_run_preserved"])
            self.assertEqual(
                manifest["recovered_from_run"],
                ".run-preserved",
            )
            for filename in analyze_papers.CONTENT_FILES:
                self.assertTrue((analysis / filename).is_file())


class MainOutputTests(unittest.TestCase):
    def test_summary_includes_result_and_open_problem_totals(self):
        with (
            TemporaryDirectory() as temporary,
            patch.object(
                analyze_papers,
                "resolve_codex_executable",
                return_value="codex",
            ),
            patch.object(
                analyze_papers,
                "read_codex_version",
                return_value="codex-cli test",
            ),
            patch.object(analyze_papers, "analyze_paper") as analyze,
        ):
            paper = make_paper(Path(temporary))
            analyze.return_value = analyze_papers.AnalysisOutcome(
                paper,
                "current",
                "3 results, 2 open problems; analysis matches",
                result_count=3,
                open_problem_count=2,
            )
            output = StringIO()

            with redirect_stdout(output):
                returncode = analyze_papers.main([str(paper)])

        self.assertEqual(returncode, 0)
        self.assertIn(
            "Totals: 3 results, 2 open problems.",
            output.getvalue(),
        )


if __name__ == "__main__":
    unittest.main()
