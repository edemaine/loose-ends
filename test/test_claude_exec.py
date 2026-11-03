import json
import os
from pathlib import Path
import stat
import sys
from tempfile import TemporaryDirectory
import textwrap
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import claude_exec
import codex_cli


SESSION = "11111111-2222-3333-4444-555555555555"


def stream(*, structured=None, result_overrides=None):
    """A short Claude Code stream-json transcript."""
    result = {
        "type": "result", "subtype": "success", "is_error": False,
        "session_id": SESSION, "duration_ms": 1234, "num_turns": 3,
        "result": "done", "total_cost_usd": 0.0125,
        "usage": {"input_tokens": 5, "cache_read_input_tokens": 100,
                  "cache_creation_input_tokens": 20, "output_tokens": 40,
                  "output_tokens_details": {"thinking_tokens": 10}},
        "modelUsage": {
            "claude-opus-5-5": {"inputTokens": 5, "cacheReadInputTokens": 100,
                                "cacheCreationInputTokens": 20, "outputTokens": 40,
                                "thinkingTokens": 10, "costUSD": 0.012},
            "claude-haiku-5-5": {"inputTokens": 7, "cacheReadInputTokens": 0,
                                 "cacheCreationInputTokens": 0, "outputTokens": 3,
                                 "costUSD": 0.0005},
        },
    }
    if structured is not None:
        result["structured_output"] = structured
    result.update(result_overrides or {})
    return [
        {"type": "system", "subtype": "init", "session_id": SESSION,
         "model": "claude-opus-5-5", "claude_code_version": "9.9.9"},
        {"type": "assistant", "session_id": SESSION, "message": {"content": [
            {"type": "thinking", "thinking": ""},
            {"type": "text", "text": "Checking the input."},
        ]}},
        {"type": "assistant", "session_id": SESSION, "message": {"content": [
            {"type": "tool_use", "id": "toolu_1", "name": "Bash",
             "input": {"command": "wc -l input.txt"}},
        ]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "toolu_1",
             "content": "1 input.txt", "is_error": False},
        ]}},
        {"type": "assistant", "session_id": SESSION, "message": {"content": [
            {"type": "tool_use", "id": "toolu_2", "name": "Write",
             "input": {"file_path": "/work/out.txt", "content": "x" * 5000}},
        ]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "toolu_2",
             "content": [{"type": "text", "text": "File created"}]},
        ]}},
        {"type": "system", "subtype": "task_summary", "detail": "ignored"},
        result,
    ]


def translate(events, result_path=None):
    emitted = []
    translator = claude_exec.Translator(emitted.append, result_path=result_path)
    for event in events:
        translator.handle(event)
    return emitted, translator


class TranslatorTests(unittest.TestCase):
    def test_translates_turn_items_and_usage_to_codex_events(self):
        emitted, _ = translate(stream())
        self.assertEqual([event["type"] for event in emitted], [
            "thread.started", "turn.started", "item.completed",
            "item.started", "item.completed", "item.started", "item.completed",
            "turn.completed",
        ])
        # The usage index requires "type" to be the first key of each event.
        self.assertTrue(all(next(iter(event)) == "type" for event in emitted))
        self.assertEqual(emitted[0]["thread_id"], SESSION)
        self.assertEqual(emitted[0]["model"], "claude-opus-5-5")
        self.assertEqual(emitted[2]["item"]["type"], "agent_message")
        bash = emitted[4]["item"]
        self.assertEqual((bash["id"], bash["type"], bash["command"], bash["status"]),
                         ("toolu_1", "command_execution", "wc -l input.txt", "completed"))
        self.assertEqual(bash["aggregated_output"], "1 input.txt")
        write = emitted[6]["item"]
        self.assertEqual((write["type"], write["tool"]), ("tool_call", "Write"))
        self.assertEqual(write["command"], "Write /work/out.txt")
        self.assertEqual(write["aggregated_output"], "File created")
        self.assertLess(len(write["input"]["content"]), 2100)
        completed = emitted[-1]
        # All models' tokens, with Codex's convention that input includes cache.
        self.assertEqual(completed["usage"], {
            "input_tokens": 132, "cached_input_tokens": 100,
            "cache_write_input_tokens": 20, "output_tokens": 43,
            "reasoning_output_tokens": 10, "cost_usd": 0.0125,
        })
        self.assertEqual(completed["models"], ["claude-haiku-5-5", "claude-opus-5-5"])

    def test_structured_output_is_written_before_turn_completes(self):
        with TemporaryDirectory() as temporary:
            result_path = Path(temporary) / "result.json"
            written = []

            def emit(event):
                if event["type"] == "turn.completed":
                    written.append(json.loads(result_path.read_text(encoding="utf-8")))

            translator = claude_exec.Translator(emit, result_path=result_path)
            for event in stream(structured={"title": "A Paper"}):
                translator.handle(event)
            self.assertEqual(written, [{"title": "A Paper"}])

            emitted, _ = translate(stream(), result_path=Path(temporary) / "missing.json")
            self.assertEqual(emitted[-1]["type"], "turn.failed")
            self.assertIn("no structured output", emitted[-1]["error"]["message"])

    def test_usage_limit_failure_becomes_a_credit_error(self):
        events = stream(result_overrides={
            "subtype": "success", "is_error": True,
            "result": "Claude AI usage limit reached|1793737200",
        })
        emitted, _ = translate(events)
        self.assertEqual(emitted[-2], {
            "type": "error", "code": "usage_limit_reached",
            "message": "Claude AI usage limit reached|1793737200",
        })
        self.assertEqual(emitted[-1]["type"], "turn.failed")
        self.assertIn("usage", emitted[-1])
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "events.jsonl"
            path.write_text("".join(json.dumps(event) + "\n" for event in emitted))
            self.assertIsNotNone(codex_cli.codex_credit_error(path))

        rejected = [
            stream()[0],
            {"type": "rate_limit_event", "rate_limit_info": {
                "status": "rejected", "rateLimitType": "five_hour"}},
            {"type": "result", "subtype": "error_during_execution", "is_error": True,
             "result": "API Error: 429"},
        ]
        emitted, _ = translate(rejected)
        self.assertEqual(emitted[-2]["code"], "usage_limit_reached")
        self.assertIn("five_hour", emitted[-2]["message"])

    def test_ordinary_failures_are_not_credit_errors(self):
        emitted, _ = translate(stream(result_overrides={
            "subtype": "error_max_turns", "is_error": True, "result": "",
        }))
        self.assertEqual([event["type"] for event in emitted[-1:]], ["turn.failed"])
        self.assertIn("error_max_turns", emitted[-1]["error"]["message"])
        self.assertFalse(any(event["type"] == "error" for event in emitted))

    def test_missing_result_is_reported_only_after_the_turn_started(self):
        emitted, translator = translate(stream()[:3])
        self.assertTrue(translator.finish(1))
        self.assertEqual(emitted[-1]["type"], "turn.failed")
        self.assertIn("status 1", emitted[-1]["error"]["message"])
        emitted, translator = translate([])
        self.assertFalse(translator.finish(1))
        self.assertEqual(emitted, [])


FAKE_CLAUDE = """\
#!{python}
import json, os, sys
from pathlib import Path
arguments = sys.argv[1:]
Path("claude-argv.json").write_text(json.dumps(arguments))
prompt = sys.stdin.read()
Path("claude-stdin.txt").write_text(prompt)
mode = os.environ.get("FAKE_CLAUDE_MODE", "ok")
def emit(event):
    print(json.dumps(event), flush=True)
emit({{"type": "system", "subtype": "init", "session_id": "s-1", "model": "fake"}})
if mode == "limit":
    emit({{"type": "result", "subtype": "success", "is_error": True,
          "result": "You've hit your limit · resets 5pm"}})
    sys.exit(1)
emit({{"type": "assistant", "message": {{"content": [
    {{"type": "text", "text": "Working."}}]}}}})
structured = None
if "--json-schema" in arguments:
    schema = json.loads(arguments[arguments.index("--json-schema") + 1])
    structured = {{"echo": sorted(schema["properties"])}}
else:
    Path("agent-result.json").write_text(json.dumps({{"written": "by model"}}))
event = {{"type": "result", "subtype": "success", "is_error": False,
         "usage": {{"input_tokens": 1, "output_tokens": 2}}, "total_cost_usd": 0.01}}
if structured is not None:
    event["structured_output"] = structured
emit(event)
"""


@unittest.skipIf(os.name == "nt", "uses an executable script as a fake Claude CLI")
class EndToEndTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.claude = self.root / "claude"
        self.claude.write_text(FAKE_CLAUDE.format(python=sys.executable), encoding="utf-8")
        self.claude.chmod(self.claude.stat().st_mode | stat.S_IEXEC)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.schema = self.root / "schema.json"
        self.schema.write_text(json.dumps({
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object", "properties": {"title": {}, "authors": {}},
        }), encoding="utf-8")
        environment = patch.dict(os.environ, {codex_cli.WORKBENCH_DATABASE_ENV: ""})
        environment.start()
        self.addCleanup(environment.stop)

    def run_turn(self, **overrides):
        arguments = dict(
            codex=str(self.claude), workspace=self.workspace,
            prompt="Read the paper.\n" * 2000, schema_path=self.schema,
            options=codex_cli.ModelOptions("claude-haiku-5-5", "low"),
            launch_interval=0,
        )
        arguments.update(overrides)
        return codex_cli.run_structured_codex(**arguments)

    def events(self):
        path = self.workspace / "events.jsonl"
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def test_structured_turn_writes_events_and_result(self):
        for timeout in (None, 60):
            with self.subTest(timeout=timeout):
                result = self.run_turn(timeout_seconds=timeout, completion_grace_seconds=timeout)
                self.assertEqual(json.loads(result.read_text()), {"echo": ["authors", "title"]})
                self.assertEqual([event["type"] for event in self.events()],
                                 ["thread.started", "turn.started", "item.completed", "turn.completed"])
                self.assertTrue(codex_cli.structured_turn_is_complete(
                    self.workspace / "events.jsonl", result))
                arguments = json.loads((self.workspace / "claude-argv.json").read_text())
                self.assertEqual(arguments[arguments.index("--model") + 1], "claude-haiku-5-5")
                self.assertEqual(arguments[arguments.index("--effort") + 1], "low")
                # Claude Code rejects meta-schema URIs it does not bundle.
                passed = json.loads(arguments[arguments.index("--json-schema") + 1])
                self.assertNotIn("$schema", passed)
                self.assertEqual(passed["type"], "object")
                self.assertEqual((self.workspace / "claude-stdin.txt").read_text(),
                                 "Read the paper.\n" * 2000)

    def test_model_owned_result_is_left_to_the_agent(self):
        result = self.run_turn(model_writes_result=True)
        self.assertEqual(json.loads(result.read_text()), {"written": "by model"})
        arguments = json.loads((self.workspace / "claude-argv.json").read_text())
        self.assertNotIn("--json-schema", arguments)

    def test_usage_limit_stops_with_a_credit_error(self):
        with patch.dict(os.environ, {"FAKE_CLAUDE_MODE": "limit"}):
            with self.assertRaisesRegex(codex_cli.CodexError, "usage limit"):
                self.run_turn()
        self.assertEqual(self.events()[-2]["code"], "usage_limit_reached")


if __name__ == "__main__":
    unittest.main()
