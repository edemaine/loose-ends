import argparse
import json
import os
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import tomllib
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import codex_cli


class CodexCliTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.codex_home = Path(temporary.name)
        environment = patch.dict(os.environ, {
            "CODEX_HOME": str(self.codex_home),
            "CODEX_SQLITE_HOME": "",
        })
        environment.start()
        self.addCleanup(environment.stop)

    def command_config(self):
        command = codex_cli.build_exec_command(
            codex="codex", workspace=Path("."), prompt="test",
            schema_path=Path("schema.json"), result_path=Path("result.json"),
            options=codex_cli.ModelOptions(model="requested-model"),
        )
        self.assertIn("--ignore-user-config", command)
        self.assertEqual(command[command.index("--model") + 1], "requested-model")
        return tomllib.loads("\n".join(
            command[index + 1]
            for index, argument in enumerate(command)
            if argument == "--config"
        ))

    def test_preserves_only_account_settings_and_selected_provider(self):
        (self.codex_home / "config.toml").write_text('''
cli_auth_credentials_store = "keyring"
forced_login_method = "chatgpt"
forced_chatgpt_workspace_id = "00000000-0000-0000-0000-000000000001"
sqlite_home = "account state"
model_provider = "gateway.with.dot"
openai_base_url = "https://api.example.test/v1"
chatgpt_base_url = "https://chatgpt.example.test/"
model = "ignored-model"
model_reasoning_effort = "low"
web_search = "live"
notify = ["ignored-hook"]
[apps._default]
enabled = true
[agents]
enabled = true
[sandbox_workspace_write]
network_access = true
[model_providers."gateway.with.dot"]
name = 'Gateway "café"'
base_url = "https://gateway.example.test/v1"
request_max_retries = 3
supports_websockets = false
query_params = { "api.version" = "2026-01-01" }
http_headers = { "X-Example" = 'quoted "value"' }
auth = { command = "token-command", args = ["--account", "second account"], cwd = "auth dir" }
[model_providers.unselected]
name = "Ignored provider"
''', encoding="utf-8")
        os.environ["CODEX_SQLITE_HOME"] = "ignored-env-path"
        config = self.command_config()

        self.assertEqual(config["cli_auth_credentials_store"], "keyring")
        self.assertEqual(config["forced_login_method"], "chatgpt")
        self.assertEqual(config["forced_chatgpt_workspace_id"],
                         "00000000-0000-0000-0000-000000000001")
        self.assertEqual(config["sqlite_home"],
                         codex_cli.path_for_codex(self.codex_home / "account state"))
        self.assertEqual(config["openai_base_url"], "https://api.example.test/v1")
        self.assertEqual(config["chatgpt_base_url"], "https://chatgpt.example.test/")
        self.assertEqual(config["model_provider"], "gateway.with.dot")
        self.assertEqual(list(config["model_providers"]), ["gateway.with.dot"])
        provider = config["model_providers"]["gateway.with.dot"]
        self.assertEqual(provider["name"], 'Gateway "café"')
        self.assertEqual(provider["base_url"], "https://gateway.example.test/v1")
        self.assertEqual(provider["request_max_retries"], 3)
        self.assertFalse(provider["supports_websockets"])
        self.assertEqual(provider["query_params"], {"api.version": "2026-01-01"})
        self.assertEqual(provider["http_headers"], {"X-Example": 'quoted "value"'})
        self.assertEqual(provider["auth"], {
            "command": "token-command", "args": ["--account", "second account"],
            "cwd": codex_cli.path_for_codex(self.codex_home / "auth dir"),
        })
        self.assertNotIn("model", config)
        self.assertNotIn("model_reasoning_effort", config)
        self.assertNotIn("notify", config)
        self.assertEqual(config["web_search"], "disabled")
        self.assertFalse(config["apps"]["_default"]["enabled"])
        self.assertFalse(config["agents"]["enabled"])
        self.assertFalse(config["sandbox_workspace_write"]["network_access"])

    def test_account_config_uses_default_home_and_tracks_account_changes(self):
        default = self.codex_home / ".codex"
        default.mkdir()
        (default / "config.toml").write_text('forced_login_method = "api"\n')
        with (
            patch.dict(os.environ, {"CODEX_HOME": "", "USERPROFILE": ""}),
            patch.object(codex_cli.Path, "home", return_value=self.codex_home),
        ):
            self.assertEqual(self.command_config()["forced_login_method"], "api")
        (self.codex_home / "config.toml").write_text('forced_login_method = "chatgpt"\n')
        self.assertEqual(self.command_config()["forced_login_method"], "chatgpt")

    def test_account_config_reads_native_windows_home_from_cygwin(self):
        (self.codex_home / "config.toml").write_text('cli_auth_credentials_store = "file"\n')
        with (
            patch.dict(os.environ, {"CODEX_HOME": r"C:\Accounts\second"}),
            patch.object(codex_cli.sys, "platform", "cygwin"),
            patch.object(codex_cli, "_run_local_command", return_value=str(self.codex_home)) as convert,
        ):
            config = self.command_config()
        self.assertEqual(config["cli_auth_credentials_store"], "file")
        convert.assert_called_once_with(
            ["cygpath", "-u", r"C:\Accounts\second"], "could not convert Codex home"
        )

    def test_account_config_preserves_absolute_windows_sqlite_path(self):
        (self.codex_home / "config.toml").write_text(
            "sqlite_home = 'C:\\Accounts\\second account\\sqlite'\n"
        )
        self.assertEqual(self.command_config()["sqlite_home"],
                         r"C:\Accounts\second account\sqlite")

    def test_account_config_errors_are_reported_without_falling_back(self):
        for text, message in (
            ('forced_login_method = [', "could not read Codex configuration"),
            ('forced_login_method = 42', "forced_login_method must be a string"),
            ('model_providers = 42', "model_providers must be a table"),
            ('model_provider = "gateway"\nmodel_providers.gateway = 42',
             "selected provider must be a table"),
        ):
            with self.subTest(text=text):
                (self.codex_home / "config.toml").write_text(text)
                with self.assertRaisesRegex(codex_cli.CodexError, message):
                    self.command_config()
        with patch.object(codex_cli.Path, "open", side_effect=PermissionError("denied")):
            with self.assertRaisesRegex(codex_cli.CodexError, "could not read Codex configuration"):
                codex_cli.codex_account_config_overrides()

    def test_preserves_sqlite_home_while_ignoring_user_config(self):
        for sqlite_home in (
            "/accounts/second account/sqlite",
            r"C:\Accounts\second account\sqlite",
            'accounts/"quoted"/café',
            "relative/account",
            "",
            None,
        ):
            with (
                self.subTest(sqlite_home=sqlite_home),
                patch.dict(os.environ, {}),
            ):
                if sqlite_home is not None:
                    os.environ["CODEX_SQLITE_HOME"] = sqlite_home
                else:
                    os.environ.pop("CODEX_SQLITE_HOME", None)
                command = codex_cli.build_exec_command(
                    codex="codex",
                    workspace=Path("."),
                    prompt="test",
                    schema_path=Path("schema.json"),
                    result_path=Path("result.json"),
                    options=codex_cli.ModelOptions(),
                )
                config = tomllib.loads("\n".join(
                    command[index + 1]
                    for index, argument in enumerate(command)
                    if argument == "--config"
                ))

                self.assertIn("--ignore-user-config", command)
                if sqlite_home:
                    self.assertEqual(config["sqlite_home"], sqlite_home)
                else:
                    self.assertNotIn("sqlite_home", config)
                self.assertFalse(config["apps"]["_default"]["enabled"])
                self.assertFalse(config["agents"]["enabled"])
                self.assertFalse(config["sandbox_workspace_write"]["network_access"])
                self.assertEqual(command[-1], "-")

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
