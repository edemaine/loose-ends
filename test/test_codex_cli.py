import argparse
import json
import os
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
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.claude_home = Path(temporary.name)
        environment = patch.dict(os.environ, {
            "CLAUDE_CONFIG_DIR": str(self.claude_home),
            codex_cli.CLAUDE_SANDBOX_ENV: "",
        })
        environment.start()
        self.addCleanup(environment.stop)

    def command(self, **overrides):
        arguments = dict(
            codex="claude", workspace=Path("."), prompt="secret prompt text",
            schema_path=Path("schema.json"), result_path=Path("result.json"),
            options=codex_cli.ModelOptions(model="requested-model"),
        )
        arguments.update(overrides)
        return codex_cli.build_exec_command(**arguments)

    @staticmethod
    def claude_arguments(command):
        return command[command.index("--") + 1:]

    @staticmethod
    def value(command, flag):
        return command[command.index(flag) + 1]

    def test_runs_claude_print_mode_through_translating_shim(self):
        command = self.command()
        self.assertEqual(command[0], sys.executable)
        self.assertEqual(Path(command[1]), codex_cli.CLAUDE_EXEC_PATH)
        self.assertEqual(self.value(command, "--result"), str(Path("result.json").resolve()))
        self.assertEqual(self.value(command, "--schema"), str(Path("schema.json").resolve()))
        claude = self.claude_arguments(command)
        self.assertEqual(claude[:2], ["claude", "-p"])
        self.assertEqual(self.value(claude, "--output-format"), "stream-json")
        for flag in ("--verbose", "--no-session-persistence", "--safe-mode",
                     "--disable-slash-commands", "--strict-mcp-config"):
            self.assertIn(flag, claude)
        self.assertEqual(self.value(claude, "--tools"), "Bash,Read,Write,Edit,Glob,Grep")
        self.assertEqual(self.value(claude, "--allowedTools"), self.value(claude, "--tools"))
        self.assertEqual(self.value(claude, "--permission-mode"), "acceptEdits")
        self.assertEqual(self.value(claude, "--permission-prompts"), "none")
        self.assertEqual(self.value(claude, "--model"), "requested-model")
        self.assertNotIn("--effort", claude)
        # The prompt travels on standard input, never on the command line.
        self.assertFalse(any("secret prompt text" in argument for argument in command))

    def test_model_owned_result_needs_no_structured_output_flags(self):
        command = self.command(model_writes_result=True)
        self.assertNotIn("--result", command)
        self.assertNotIn("--schema", command)
        self.assertNotIn("--json-schema", command)
        self.assertEqual(command[2], "--")

    def test_web_search_modes_choose_claude_web_tools(self):
        for mode, tools in (
            ("disabled", "Bash,Read,Write,Edit,Glob,Grep"),
            ("indexed", "Bash,Read,Write,Edit,Glob,Grep,WebSearch"),
            ("live", "Bash,Read,Write,Edit,Glob,Grep,WebSearch,WebFetch"),
        ):
            with self.subTest(mode=mode):
                command = self.command(web_search=mode)
                self.assertEqual(self.value(command, "--tools"), tools)
                self.assertEqual(self.value(command, "--allowedTools"), tools)
        with self.assertRaisesRegex(codex_cli.CodexError, "web search must be one of"):
            self.command(web_search="everything")

    def test_effort_fast_mode_and_sandbox_settings(self):
        options = codex_cli.ModelOptions(reasoning_effort="xhigh", fast=True)
        with patch.dict(os.environ, {codex_cli.CLAUDE_SANDBOX_ENV: "1"}):
            command = self.command(options=options)
        self.assertEqual(self.value(command, "--effort"), "xhigh")
        self.assertNotIn("--model", command)
        settings = json.loads(self.value(command, "--settings"))
        self.assertTrue(settings["fastMode"])
        self.assertEqual(settings["sandbox"], {
            "enabled": True,
            "autoAllowBashIfSandboxed": True,
            "allowUnsandboxedCommands": False,
        })
        with patch.dict(os.environ, {codex_cli.CLAUDE_SANDBOX_ENV: "0"}):
            self.assertNotIn("--settings", self.command(options=codex_cli.ModelOptions()))

    def test_sandbox_defaults_to_supported_platforms(self):
        for platform, expected in (("darwin", True), ("linux", True), ("win32", False), ("cygwin", False)):
            with self.subTest(platform=platform), patch.object(codex_cli.sys, "platform", platform):
                self.assertEqual(codex_cli.claude_sandbox_enabled({}), expected)
        self.assertFalse(codex_cli.claude_sandbox_enabled({codex_cli.CLAUDE_SANDBOX_ENV: "off"}))
        with patch.object(codex_cli.sys, "platform", "win32"):
            self.assertTrue(codex_cli.claude_sandbox_enabled({codex_cli.CLAUDE_SANDBOX_ENV: "yes"}))

    def test_ultrafast_is_rejected_for_claude(self):
        with self.assertRaisesRegex(codex_cli.CodexError, "Ultrafast is a Codex service tier"):
            self.command(options=codex_cli.ModelOptions(ultrafast=True))
        parser = argparse.ArgumentParser()
        codex_cli.add_model_arguments(parser)
        self.assertFalse(parser.parse_args([]).ultrafast)
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
            parser.parse_args(["--ultrafast"])
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
            parser.parse_args(["--reasoning-effort", "ultra"])
        defaults = parser.parse_args([])
        self.assertEqual(defaults.model, codex_cli.DEFAULT_MODEL)
        self.assertEqual(defaults.reasoning_effort, codex_cli.DEFAULT_REASONING_EFFORT)

    def test_config_dir_reads_native_windows_path_from_cygwin(self):
        with (
            patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": r"C:\Accounts\second"}),
            patch.object(codex_cli.sys, "platform", "cygwin"),
            patch.object(codex_cli, "_run_local_command", return_value=str(self.claude_home)) as convert,
        ):
            self.assertEqual(codex_cli.codex_home_path(), self.claude_home.resolve())
        convert.assert_called_once_with(
            ["cygpath", "-u", r"C:\Accounts\second"], "could not convert CLAUDE_CONFIG_DIR"
        )

    def test_home_warnings_only_report_missing_directories(self):
        default = self.claude_home / ".claude"
        default.mkdir()
        with patch.object(codex_cli.Path, "home", return_value=self.claude_home):
            for value in (None, ""):
                with self.subTest(value=value):
                    environment = {} if value is None else {"CLAUDE_CONFIG_DIR": value}
                    warnings = codex_cli.codex_home_warnings(environment)
                    self.assertEqual(warnings, [])
            default.rmdir()
            warnings = codex_cli.codex_home_warnings({})
            self.assertEqual(len(warnings), 1)
            self.assertIn("not an existing directory", warnings[0])
            self.assertIn(str(default.resolve()), warnings[0])
            self.assertFalse(default.exists())

        self.assertEqual(codex_cli.codex_home_warnings(), [])
        for path in (self.claude_home / "missing", self.claude_home / "file"):
            with self.subTest(path=path):
                if path.name == "file":
                    path.write_text("not a directory")
                warnings = codex_cli.codex_home_warnings({"CLAUDE_CONFIG_DIR": str(path)})
                self.assertEqual(len(warnings), 1)
                self.assertIn("not an existing directory", warnings[0])
                self.assertIn(str(path.resolve()), warnings[0])

    def test_credit_exhaustion_pauses_queue_and_preserves_pending_work(self):
        from workbench_store import WorkbenchStore

        message = "You've hit your usage limit. Purchase more credits or try again later."
        with TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            store = WorkbenchStore(workspace / "workbench.sqlite3")

            def fail_codex(*args, **kwargs):
                kwargs["stdout"].write(json.dumps({"type": "turn.failed", "error": {"message": message}}) + "\n")
                return subprocess.CompletedProcess(args[0], 1)

            with (
                patch.dict(os.environ, {codex_cli.WORKBENCH_DATABASE_ENV: str(store.database)}),
                patch.object(codex_cli, "grant_workspace_owner_inheritance"),
                patch.object(codex_cli, "require_secure_windows_sandbox"),
                patch.object(codex_cli, "normalize_workspace_access"),
                patch.object(codex_cli, "build_exec_command", return_value=["codex"]),
                patch.object(codex_cli, "wait_for_codex_launch_slot"),
                patch.object(subprocess, "run", side_effect=fail_codex) as run,
            ):
                with self.assertRaisesRegex(codex_cli.CodexError, "credits exhausted"):
                    codex_cli.run_structured_codex(
                        codex="codex", workspace=workspace, prompt="test",
                        schema_path=workspace / "schema.json",
                    )
            run.assert_called_once()
            reopened = WorkbenchStore(store.database)
            self.assertEqual(reopened.scheduler_settings()["queuePauseReason"], "codex_credits")
            self.assertTrue(reopened.scheduler_settings()["queuePaused"])
            self.assertEqual(reopened.scheduler_settings()["codexCreditError"], message)
            self.assertIsNone(reopened.claim_next_run(set()))
            reopened.update_scheduler_settings(queue_paused=False)
            self.assertFalse(reopened.scheduler_settings()["queuePaused"])
            self.assertIsNone(reopened.scheduler_settings()["codexCreditError"])

    def test_credit_detection_ignores_agent_content_and_unrelated_errors(self):
        with TemporaryDirectory() as temporary:
            events = Path(temporary) / "events.jsonl"
            for event, expected in (
                ({"type": "error", "message": "You've hit your usage limit."}, True),
                ({"type": "turn.failed", "error": {"code": "insufficient_quota"}}, True),
                ({"type": "error", "message": "Out of credits"}, True),
                ({"type": "turn.failed", "error": {"message": "Credit balance is too low"}}, True),
                ({"type": "error", "code": "usage_limit_reached",
                  "message": "Claude five_hour usage limit reached"}, True),
                ({"type": "turn.failed", "error": {"message": "You've hit your limit · resets 5pm"}}, True),
                ({"type": "error", "message": "Rate limit exceeded; retry shortly"}, False),
                ({"type": "item.completed", "message": "out of credits"}, False),
                ({"type": "turn.failed", "error": None}, False),
                ([], False),
            ):
                with self.subTest(event=event):
                    events.write_text("not json\n" + json.dumps(event) + "\n", encoding="utf-8")
                    self.assertEqual(codex_cli.codex_credit_error(events) is not None, expected)

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
