import argparse
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import codex_cli


class CodexCliTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "win32", "Windows process flags")
    def test_windowless_process_group_hides_console(self):
        options = codex_cli.windowless_popen_options()

        self.assertTrue(
            options["creationflags"] & subprocess.CREATE_NO_WINDOW
        )
        self.assertTrue(
            options["creationflags"] & subprocess.CREATE_NEW_PROCESS_GROUP
        )
        self.assertTrue(
            options["startupinfo"].dwFlags
            & subprocess.STARTF_USESHOWWINDOW
        )
        self.assertEqual(options["startupinfo"].wShowWindow, subprocess.SW_HIDE)

    def test_reports_nul_filled_windows_sandbox_acl_state_before_launch(self):
        with TemporaryDirectory() as temporary:
            state = Path(temporary) / "deny_read_acl_state.json"
            state.write_bytes(b"\0" * 22)
            with (
                patch.object(codex_cli, "is_windows_host", return_value=True),
                patch.object(
                    codex_cli,
                    "_windows_sandbox_acl_state_path",
                    return_value=state,
                ),
                patch.object(
                    codex_cli,
                    "_windows_sandbox_probe_error",
                    None,
                ),
                patch.object(
                    codex_cli,
                    "_windows_sandbox_probe_succeeded",
                    False,
                ),
                patch.object(subprocess, "run") as run,
            ):
                with self.assertRaisesRegex(
                    codex_cli.CodexError,
                    "contains only NUL bytes",
                ) as caught:
                    codex_cli.require_secure_windows_sandbox(
                        "codex",
                        Path("."),
                    )

            run.assert_not_called()
            self.assertIn(str(state), str(caught.exception))
            self.assertIn("Move that file aside", str(caught.exception))

    def test_secure_windows_sandbox_preflight_fails_once_and_is_cached(self):
        completed = subprocess.CompletedProcess(
            ["codex", "sandbox"],
            1,
            stdout="",
            stderr=(
                "windows sandbox failed: helper_unknown_error: "
                "apply deny-read ACLs"
            ),
        )
        with (
            patch.object(codex_cli, "is_windows_host", return_value=True),
            patch.object(
                codex_cli,
                "_windows_sandbox_probe_command",
                return_value=["codex", "sandbox"],
            ),
            patch.object(
                codex_cli,
                "_windows_sandbox_probe_error",
                None,
            ),
            patch.object(
                codex_cli,
                "_windows_sandbox_probe_succeeded",
                False,
            ),
            patch.object(subprocess, "run", return_value=completed) as run,
        ):
            for _ in range(2):
                with self.assertRaisesRegex(
                    codex_cli.CodexError,
                    "before the agent was started",
                ):
                    codex_cli.require_secure_windows_sandbox(
                        "codex",
                        Path("."),
                    )
        run.assert_called_once()

    def test_shared_prompt_arguments_separate_direction_from_template(self):
        parser = argparse.ArgumentParser()
        default_template = Path("default-prompt.md")
        codex_cli.add_prompt_arguments(
            parser,
            default_template=default_template,
            task="test agent",
        )

        defaults = parser.parse_args([])
        self.assertIsNone(defaults.prompt)
        self.assertEqual(defaults.prompt_template, default_template)
        custom = parser.parse_args(
            [
                "--prompt",
                "Try the dual formulation",
                "--prompt-template",
                "custom.md",
            ]
        )
        self.assertEqual(custom.prompt, "Try the dual formulation")
        self.assertEqual(custom.prompt_template, Path("custom.md"))

        rendered = codex_cli.with_user_prompt(
            "Core instructions.\n",
            custom.prompt,
            task="test agent",
        )
        self.assertTrue(rendered.startswith("Core instructions.\n"))
        self.assertIn("<user_instruction>", rendered)
        self.assertIn("Try the dual formulation", rendered)
        options = codex_cli.ModelOptions()
        self.assertNotEqual(
            codex_cli.semantic_config_digest(
                "Core instructions.",
                "{}",
                options,
            ),
            codex_cli.semantic_config_digest(rendered, "{}", options),
        )
        with self.assertRaisesRegex(
            codex_cli.CodexError,
            "--prompt must be nonempty",
        ):
            codex_cli.with_user_prompt(
                "Core instructions.",
                "  ",
                task="test agent",
            )

    def test_reports_post_parse_error_without_usage(self):
        parser = argparse.ArgumentParser(prog="test-tool.py")
        error_output = StringIO()

        with redirect_stderr(error_output):
            returncode = codex_cli.report_error(
                parser,
                codex_cli.CodexError("the operation failed"),
            )

        self.assertEqual(returncode, 1)
        self.assertEqual(
            error_output.getvalue(),
            "test-tool.py: error: the operation failed\n",
        )

    def test_windows_reserved_device_names(self):
        for name in ("NUL", "nul.txt", "COM1", "LPT9.log", "aux. "):
            with self.subTest(name=name):
                self.assertTrue(
                    codex_cli._is_windows_reserved_device_name(name)
                )
        for name in ("NULL", "COM10", "NUL-file", "results.txt"):
            with self.subTest(name=name):
                self.assertFalse(
                    codex_cli._is_windows_reserved_device_name(name)
                )

    def test_access_check_skips_windows_reserved_device_entries(self):
        with (
            TemporaryDirectory() as temporary,
            patch.object(codex_cli, "is_windows_host", return_value=True),
            patch.object(
                codex_cli.os,
                "walk",
                return_value=[(temporary, [], ["NUL"])],
            ),
            patch.object(
                codex_cli.Path,
                "stat",
                side_effect=AssertionError("reserved entry was inspected"),
            ),
        ):
            self.assertTrue(
                codex_cli.workspace_is_user_accessible(Path(temporary))
            )

    def test_recognizes_completed_structured_turn(self):
        with TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            events = workspace / "events.jsonl"
            result = workspace / "agent-result.json"
            events.write_text(
                '{"type":"item.completed"}\n'
                '{"type":"turn.completed"}\n',
                encoding="utf-8",
            )
            result.write_text('{"status":"done"}\n', encoding="utf-8")
            self.assertTrue(
                codex_cli.structured_turn_is_complete(events, result)
            )

    def test_stops_lingering_process_after_completed_turn(self):
        process = Mock()
        process.poll.return_value = None
        process.returncode = -1
        events = StringIO()
        log = StringIO()
        with (
            patch.object(subprocess, "Popen", return_value=process),
            patch.object(
                codex_cli,
                "structured_turn_is_complete",
                return_value=True,
            ),
            patch.object(codex_cli, "_stop_codex_process") as stop,
            patch.object(codex_cli.time, "monotonic", side_effect=[0, 0, 2]),
            patch.object(codex_cli.time, "sleep"),
        ):
            completed, stopped, timed_out = codex_cli._run_codex_process(
                ["codex"],
                prompt="test prompt",
                workspace=Path("."),
                environment={},
                events=events,
                log=log,
                events_path=Path("events.jsonl"),
                result_path=Path("agent-result.json"),
                timeout_seconds=10,
                completion_grace_seconds=1,
            )
        stop.assert_called_once_with(process)
        self.assertTrue(stopped)
        self.assertFalse(timed_out)
        self.assertEqual(completed.returncode, -1)
        self.assertIn("structured turn completed", log.getvalue())

    def test_stops_process_at_wall_clock_timeout(self):
        process = Mock()
        process.poll.return_value = None
        process.returncode = -1
        with (
            patch.object(subprocess, "Popen", return_value=process),
            patch.object(
                codex_cli,
                "structured_turn_is_complete",
                return_value=False,
            ),
            patch.object(codex_cli, "_stop_codex_process") as stop,
            patch.object(codex_cli.time, "monotonic", side_effect=[0, 2]),
        ):
            _, stopped, timed_out = codex_cli._run_codex_process(
                ["codex"],
                prompt="test prompt",
                workspace=Path("."),
                environment={},
                events=StringIO(),
                log=StringIO(),
                events_path=Path("events.jsonl"),
                result_path=Path("agent-result.json"),
                timeout_seconds=1,
                completion_grace_seconds=1,
            )
        stop.assert_called_once_with(process)
        self.assertFalse(stopped)
        self.assertTrue(timed_out)

    def test_accepts_completed_result_after_nonzero_launcher_exit(self):
        process = Mock()
        process.poll.side_effect = [None, 126]
        process.returncode = 126
        with (
            patch.object(subprocess, "Popen", return_value=process),
            patch.object(
                codex_cli,
                "structured_turn_is_complete",
                return_value=True,
            ),
            patch.object(codex_cli.time, "monotonic", side_effect=[0, 0]),
            patch.object(codex_cli.time, "sleep"),
        ):
            completed, structured, timed_out = (
                codex_cli._run_codex_process(
                    ["codex"],
                    prompt="test prompt",
                    workspace=Path("."),
                    environment={},
                    events=StringIO(),
                    log=StringIO(),
                    events_path=Path("events.jsonl"),
                    result_path=Path("agent-result.json"),
                    timeout_seconds=10,
                    completion_grace_seconds=1,
                )
            )
        self.assertEqual(completed.returncode, 126)
        self.assertTrue(structured)
        self.assertFalse(timed_out)


if __name__ == "__main__":
    unittest.main()
