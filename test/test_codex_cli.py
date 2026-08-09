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


if __name__ == "__main__":
    unittest.main()
