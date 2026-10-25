from pathlib import Path
import json
import os
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import analyze_papers
import codex_cli
from codex_transcripts import record_transcript, TRANSCRIPT_ENV
import solve_open_problems
import workbench
from workbench_store import WorkbenchStore
from workbench_tasks import _common_arguments, PlanError
from workbench_usage import UsageIndex, estimated_cost, path_key, token_usage
import write_paper


USAGE = dict(input_tokens=1000, cached_input_tokens=600,
             cache_write_input_tokens=100, output_tokens=100, reasoning_output_tokens=40)


def events(thread, usage=USAGE):
    result = [{"type": "thread.started", "thread_id": thread}, {"type": "turn.started"}]
    if usage is not None:
        result.append({"type": "turn.completed", "usage": usage})
    return "".join(json.dumps(event) + "\n" for event in result)


class UsageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.papers = self.root / "papers"
        self.paper = self.papers / "paper"
        self.problem = self.paper / "OP-001"
        self.attempt = self.problem / "attempt-001"
        self.attempt.mkdir(parents=True)
        (self.paper / "paper.pdf").touch()
        self.manuscripts = self.root / "manuscripts"
        self.manuscripts.mkdir()
        self.store = WorkbenchStore(self.root / "state" / "workbench.sqlite3")
        self.index = UsageIndex(self.store, [self.papers], self.manuscripts)

    def context(self, directory=None, **overrides):
        directory = directory or self.attempt
        value = {"stage": "Solve", "model": "gpt-6.1-sol", "tier": "standard",
                 "quality": 2, "scopes": sorted(self.index._scopes(directory / "events.jsonl"))}
        return {**value, **overrides}

    def source(self, name, text, context=None):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode())
        self.index.import_source(path, context or self.context())
        return path

    def total(self, *targets):
        return self.index.summary([{"kind": kind, "path": str(path)} for kind, path in
                                   (targets or [("paper", self.paper)])])

    def test_union_of_parent_child_and_duplicate_logs_counts_each_turn_once(self):
        main = events("solve") + events("repair")
        self.source("installed/events.jsonl", main)
        self.source("archive/solve/events", events("solve"))
        self.source("archive/repair/events", events("repair"), self.context(stage="Solve · Repair"))
        total = self.total(("paper", self.paper), ("problem", self.problem), ("attempt", self.attempt))
        self.assertEqual(total["invocations"], 2)
        self.assertEqual(total["tokens"]["input_tokens"], 2000)
        self.assertEqual(total["tokens"]["output_tokens"], 200)
        self.assertEqual(total["tokens"]["reasoning_output_tokens"], 80)
        self.assertAlmostEqual(total["estimatedCost"], 2 * .00191)

    def test_incremental_import_preserves_partial_lines_and_does_not_reread_unchanged_logs(self):
        path = self.source("live/events", events("live", None))
        self.assertEqual(self.total()["missingUsage"], 1)
        with patch.object(Path, "open", side_effect=AssertionError("read unchanged log")):
            self.assertEqual(self.index.import_source(path, self.context()), 0)
        completion = json.dumps({"type": "turn.completed", "usage": USAGE}).encode()
        with path.open("ab") as stream:
            stream.write(completion[:20])
        self.index.import_source(path, self.context())
        self.assertEqual(self.total()["missingUsage"], 1)
        with path.open("ab") as stream:
            stream.write(completion[20:] + b"\n")
        self.index.import_source(path, self.context())
        self.assertEqual(self.total()["invocations"], 1)
        self.assertEqual(self.total()["missingUsage"], 0)
        self.assertEqual(self.total()["tokens"], USAGE)

    def test_replacement_retains_history_and_restart_uses_database(self):
        path = self.source("events", events("first"))
        path.write_bytes(events("second").encode())
        self.index.import_source(path, self.context())
        path.unlink()
        reopened = UsageIndex(self.store, [self.papers], self.manuscripts)
        with patch.object(Path, "open", side_effect=AssertionError("summary read filesystem")):
            total = reopened.summary([{"kind": "paper", "path": str(self.paper)}])
        self.assertEqual(total["invocations"], 2)

    def test_unknown_models_and_missing_usage_remain_visible(self):
        self.source("unknown", events("unknown"), self.context(model="custom-model"))
        self.source("failed", events("failed", None))
        self.source("bad", events("bad", {**USAGE, "cached_input_tokens": 5000}))
        self.source("invalid", '{"type":"turn.completed","usage":broken}\n')
        total = self.total()
        self.assertEqual(total["unpriced"], 1)
        self.assertEqual(total["missingUsage"], 3)
        self.assertIsNone(total["estimatedCost"])
        self.assertEqual(total["tokens"], USAGE)
        unknown = next(item for item in total["pricingRates"] if item["model"] == "custom-model")
        self.assertIsNone(unknown["rates"])

    def test_managed_and_installed_backfill_fills_model_and_attempt_attribution(self):
        job = self.store.create_job({}, {"action": "solve", "title": "Solve", "units": [{
            "label": "Problem", "argv": ["python", "solve.py"], "cwd": str(self.root),
            "targets": [{"kind": "problem", "path": str(self.problem)}],
        }]})
        run = job["runs"][0]
        archive = Path(run["log_path"]).parent / "codex" / "turn-1"
        archive.mkdir(parents=True)
        (archive / "source.json").write_text(json.dumps({
            "events": str(self.problem / ".solve-run-x" / "events.jsonl"), "stage": "Solve",
        }))
        (archive / "events").write_text(events("same"))
        (self.attempt / "events.jsonl").write_text(events("same"))
        (self.attempt / "manifest.json").write_text(json.dumps({
            "requested_model": "gpt-6.1-sol", "requested_fast_mode": False,
        }))
        self.assertGreater(self.index.refresh(), 0)
        total = self.total(("job", job["id"]))
        self.assertEqual(total["invocations"], 1)
        self.assertEqual(total["unpriced"], 0)
        self.assertEqual(self.total(("attempt", self.attempt))["invocations"], 1)
        self.assertEqual(self.index.refresh(), 0)
        with self.store.connect() as db:
            db.execute("DELETE FROM runs")
            db.execute("DELETE FROM jobs")
        self.assertEqual(self.total(("paper", self.paper))["invocations"], 1)

    def test_analysis_and_joint_manuscripts_are_not_charged_to_each_source_problem(self):
        analysis = self.paper / "analysis"
        analysis.mkdir()
        self.source("analysis", events("analysis"), self.context(directory=analysis, stage="Analyze"))
        draft = self.manuscripts / "joint" / "draft-001"
        draft.mkdir(parents=True)
        self.source("draft", events("write"), self.context(directory=draft, stage="Write"))
        self.assertEqual(self.total(("problem", self.problem))["invocations"], 0)
        self.assertEqual(self.total()["invocations"], 1)
        self.assertEqual(self.total(("manuscript", draft.parent), ("draft", draft))["invocations"], 1)

    def test_metadata_change_updates_without_changing_tokens(self):
        path = self.source("events", events("known"), self.context(model=None))
        self.assertIsNone(self.total()["estimatedCost"])
        self.index.import_source(path, self.context(tier="ultrafast", quality=3))
        total = self.total()
        self.assertAlmostEqual(total["estimatedCost"], .00191 * 6)
        self.assertEqual(total["invocations"], 1)
        self.assertEqual(len(total["pricingRates"]), 1)
        pricing = total["pricingRates"][0]
        self.assertEqual((pricing["model"], pricing["tier"]), ("gpt-6.1-sol", "ultrafast"))
        for key, expected in {"input": 12, "cachedInput": .6, "cacheWrite": 15, "output": 60}.items():
            self.assertAlmostEqual(pricing["rates"][key], expected)

    def test_large_selection_uses_database_union_without_expression_depth_limit(self):
        self.source("events", events("selected"))
        targets = [{"kind": "problem", "path": str(self.paper / f"OP-{n:04d}")} for n in range(2000)]
        targets.extend([{"kind": "paper", "path": str(self.paper)}] * 3)
        self.assertEqual(self.index.summary(targets)["invocations"], 1)

    def test_stopped_import_is_retried_on_restart(self):
        path = self.root / "events"
        path.write_bytes(events("restart").encode())
        self.index.stopping.set()
        self.assertEqual(self.index.import_source(path, self.context()), 0)
        self.index.stopping.clear()
        self.index.import_source(path, self.context())
        self.assertEqual(self.total()["invocations"], 1)

    def test_usage_request_rejects_invalid_or_outside_targets(self):
        app = object.__new__(workbench.WorkbenchApplication)
        app.allowed_roots = [self.papers, self.manuscripts]
        app.usage = self.index
        for body in ({}, {"targets": []}, {"targets": [None]},
                     {"targets": [{"kind": [], "path": str(self.paper)}]},
                     {"targets": [{"kind": "paper", "path": str(self.root / "outside")}]},
                     {"targets": [{"kind": "unknown", "path": str(self.paper)}]}):
            with self.subTest(body=body), self.assertRaises(PlanError):
                app.usage_summary(body)
        self.assertEqual(app.usage_summary({"targets": [{"kind": "paper", "path": str(self.paper)}]})["invocations"], 0)

    @unittest.skipUnless(os.name == "nt", "Windows/Cygwin interoperability")
    def test_windows_and_cygwin_paths_share_a_scope_key(self):
        native = str(self.paper.resolve())
        cygwin = "/cygdrive/" + native[0].lower() + native[2:].replace("\\", "/")
        self.assertEqual(path_key(native), path_key(cygwin))


class UltrafastTests(unittest.TestCase):
    def test_rates_cache_subsets_and_tiers(self):
        self.assertAlmostEqual(estimated_cost(USAGE, "gpt-6.1-sol", "standard"), .00191)
        self.assertAlmostEqual(estimated_cost(USAGE, "gpt-6.1-sol", "fast"), .00382)
        self.assertAlmostEqual(estimated_cost(USAGE, "gpt-6.1-sol", "ultrafast"), .01146)
        self.assertIsNone(estimated_cost(USAGE, "unknown", "standard"))
        self.assertIsNone(estimated_cost(USAGE, "gpt-5.6-sol", "ultrafast"))
        self.assertIsNone(token_usage({**USAGE, "input_tokens": True}))

    def test_cli_flags_inheritance_and_digest(self):
        for module in (analyze_papers, solve_open_problems, write_paper):
            parser = module.build_parser()
            # Parser defaults/positional inputs differ; inspect the argument group.
            self.assertIn("--ultrafast", parser._option_string_actions)
            self.assertIn("--fast", parser._option_string_actions)
        normal = codex_cli.ModelOptions("gpt-6.1-sol", "high")
        ultra = codex_cli.ModelOptions("gpt-6.1-sol", "high", ultrafast=True)
        fast = codex_cli.ModelOptions(fast=True)
        self.assertNotEqual(codex_cli.semantic_config_digest("p", "{}", normal),
                            codex_cli.semantic_config_digest("p", "{}", ultra))
        for inherit in (solve_open_problems._inherit_review_options, write_paper._inherit_options):
            self.assertEqual(inherit(ultra, codex_cli.ModelOptions()).service_tier, "ultrafast")
            self.assertEqual(inherit(ultra, fast).service_tier, "fast")
            self.assertEqual(inherit(fast, ultra).service_tier, "ultrafast")
        with self.assertRaises(ValueError):
            codex_cli.ModelOptions(fast=True, ultrafast=True)
        argv = []
        _common_arguments(argv, {"serviceTier": "ultrafast"})
        self.assertEqual(argv, ["--ultrafast"])
        with self.assertRaises(PlanError):
            _common_arguments([], {"serviceTier": "typo"})
        with patch.object(codex_cli, "codex_account_config_overrides", return_value=[]):
            command = codex_cli.build_exec_command(
                codex="codex", workspace=Path.cwd(), prompt="p", schema_path=Path("schema.json"),
                result_path=Path("result.json"), options=ultra,
            )
        self.assertIn('service_tier="ultrafast"', command)
        self.assertNotIn('service_tier="fast"', command)

    def test_invocation_provenance_survives_failure(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / ".solve-run-example"
            workspace.mkdir()

            @record_transcript
            def invoke(**kwargs):
                (workspace / "events.jsonl").write_text(events("failed", None))
                raise RuntimeError("failure")

            with patch.dict(os.environ, {TRANSCRIPT_ENV: str(root / "archive")}):
                with self.assertRaises(RuntimeError):
                    invoke(workspace=workspace, prompt="p", options=codex_cli.ModelOptions("gpt-6-astra", ultrafast=True))
            folder = next((root / "archive").iterdir())
            metadata = json.loads((folder / "source.json").read_text())
            self.assertEqual(metadata["model"], "gpt-6-astra")
            self.assertEqual(metadata["service_tier"], "ultrafast")
            self.assertTrue((folder / "events").is_file())


if __name__ == "__main__":
    unittest.main()
