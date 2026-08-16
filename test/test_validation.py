import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import call, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import codex_cli
from validation import solver as solver_validation


def solver_result(claim_id: str = "C-001", artifacts=None) -> dict:
    return {
        "claimed_result_type": "partial_result",
        "summary": "A checkable lemma.",
        "external_sources": [],
        "checkable_claims": [
            {
                "id": claim_id,
                "type": "lemma",
                "statement": "The lemma holds.",
                "support": "A derivation is supplied.",
                "remaining_gap": "The main theorem remains.",
            }
        ],
        "artifacts": artifacts or [],
        "warnings": [],
    }


class ValidationTests(unittest.TestCase):
    def test_shared_validation_prompt_is_appended(self):
        instructions = codex_cli.DEFAULT_VALIDATION_PROMPT_PATH.read_text(
            encoding="utf-8"
        ).strip()

        combined = codex_cli.with_validation_instructions("Do the task.\n")

        self.assertEqual(combined, f"Do the task.\n\n{instructions}\n")
        self.assertIn("python -m validation.validate", combined)

    def test_stages_only_selected_checker_and_runs_without_arguments(self):
        with TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            (workspace / "attempt.md").write_text(
                "# Attempt\n\n## C-001\n\nA derivation.\n",
                encoding="utf-8",
            )
            (workspace / "agent-result.json").write_text(
                json.dumps(solver_result()),
                encoding="utf-8",
            )
            validator = codex_cli.OutputValidator(
                Path(solver_validation.__file__).resolve(),
                solver_validation.validate,
                {},
            )
            directory = codex_cli.stage_output_validator(workspace, validator)

            self.assertEqual(
                {path.name for path in directory.iterdir()},
                {"__init__.py", "common.py", "validate.py", "expectations.json"},
            )
            completed = subprocess.run(
                [sys.executable, "-m", "validation.validate"],
                cwd=workspace,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            self.assertEqual(completed.stdout, "Validation passed.\n")

    def test_solver_rejects_loose_ids_and_markdown_artifact_links(self):
        with TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            (workspace / "attempt.md").write_text(
                "# Attempt\n\n## C1\n\nA derivation.\n",
                encoding="utf-8",
            )
            (workspace / "agent-result.json").write_text(
                json.dumps(
                    solver_result(
                        "C1",
                        ["[Verifier](artifacts/verifier.py)"],
                    )
                ),
                encoding="utf-8",
            )

            report = solver_validation.validate(
                workspace=workspace,
                expectations={},
            )

            self.assertFalse(report.valid)
            self.assertIn("E_CLAIM_ID", report.failure_message())
            self.assertIn("E_ARTIFACT_PATH", report.failure_message())

    def test_staged_schema_rejects_unknown_result_fields(self):
        with TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            (workspace / "attempt.md").write_text(
                "# Attempt\n\n## C-001\n\nA derivation.\n",
                encoding="utf-8",
            )
            result = solver_result()
            result["unexpected"] = True
            (workspace / "agent-result.json").write_text(
                json.dumps(result),
                encoding="utf-8",
            )
            schema = json.loads(
                (
                    Path(__file__).resolve().parents[1]
                    / "schemas"
                    / "open-problem-solution.schema.json"
                ).read_text(encoding="utf-8")
            )

            report = solver_validation.validate(
                workspace=workspace,
                expectations={"result_schema": schema},
            )

            self.assertFalse(report.valid)
            self.assertIn("E_SCHEMA_PROPERTY", report.failure_message())

    def test_model_owned_result_command_omits_final_output_redirection(self):
        command = codex_cli.build_exec_command(
            codex="codex",
            workspace=Path("."),
            prompt="test",
            schema_path=Path("schema.json"),
            result_path=Path("agent-result.json"),
            options=codex_cli.ModelOptions(),
            model_writes_result=True,
        )

        self.assertNotIn("--output-schema", command)
        self.assertNotIn("-o", command)

    def test_authoritative_failure_gets_one_repair_turn(self):
        with TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            calls = []
            schema = workspace / "schema.json"
            schema.write_text('{"type":"object"}\n', encoding="utf-8")

            def fake_run(**kwargs):
                calls.append(kwargs)
                claim_id = "C1" if len(calls) == 1 else "C-001"
                (workspace / "attempt.md").write_text(
                    f"# Attempt\n\n## {claim_id}\n\nA derivation.\n",
                    encoding="utf-8",
                )
                (workspace / "agent-result.json").write_text(
                    json.dumps(solver_result(claim_id)),
                    encoding="utf-8",
                )
                return workspace / "agent-result.json"

            validator = codex_cli.OutputValidator(
                Path(solver_validation.__file__).resolve(),
                solver_validation.validate,
                {},
            )
            with patch.object(
                codex_cli,
                "run_structured_codex",
                side_effect=fake_run,
            ):
                report = codex_cli.run_validated_codex(
                    codex="codex",
                    workspace=workspace,
                    prompt="Do the task.",
                    schema_path=schema,
                    validator=validator,
                    launch_interval=0,
                )

            self.assertTrue(report.valid)
            self.assertEqual(len(calls), 2)
            self.assertIn("E_CLAIM_ID", calls[1]["prompt"])
            self.assertTrue(calls[0]["model_writes_result"])

    def test_validated_run_grants_read_access_to_staged_validation(self):
        with TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            schema = workspace / "schema.json"
            schema.write_text('{"type":"object"}\n', encoding="utf-8")

            def fake_run(**kwargs):
                (workspace / "attempt.md").write_text(
                    "# Attempt\n\n## C-001\n\nA derivation.\n",
                    encoding="utf-8",
                )
                (workspace / "agent-result.json").write_text(
                    json.dumps(solver_result()),
                    encoding="utf-8",
                )
                return workspace / "agent-result.json"

            validator = codex_cli.OutputValidator(
                Path(solver_validation.__file__).resolve(),
                solver_validation.validate,
                {},
            )
            with (
                patch.object(
                    codex_cli,
                    "run_structured_codex",
                    side_effect=fake_run,
                ),
                patch.object(
                    codex_cli,
                    "grant_sandbox_read_access",
                ) as grant_read_access,
            ):
                report = codex_cli.run_validated_codex(
                    codex="codex",
                    workspace=workspace,
                    prompt="Do the task.",
                    schema_path=schema,
                    validator=validator,
                    launch_interval=0,
                )

            self.assertTrue(report.valid)
            self.assertEqual(
                grant_read_access.call_args_list,
                [
                    call(workspace / "validation"),
                    call(schema),
                ],
            )


if __name__ == "__main__":
    unittest.main()
