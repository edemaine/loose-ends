from pathlib import Path
from io import BytesIO
import json
import subprocess
import sys
from tempfile import TemporaryDirectory
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import open_problem_common as common
import human_review
from watchdog.observers import Observer
from workbench import (
    CatalogManager,
    ChangeHandler,
    EventHub,
    _request_hostname_allowed,
    _same_request_origin,
    build_parser,
)
import workbench
from workbench_store import WorkbenchStore
from workbench_tasks import (
    PlanError,
    build_plan,
    populate_dry_run_previews,
    probe_outputs,
)
import workbench_worker


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def make_paper(root: Path) -> Path:
    paper = root / "arXiv-1234.56789v1"
    (paper / "source").mkdir(parents=True)
    (paper / "paper.pdf").write_bytes(b"%PDF-test")
    (paper / "source" / "main.tex").write_text("Test", encoding="utf-8")
    analysis = paper / "analysis"
    analysis.mkdir()
    (analysis / "summary.md").write_text("# Summary", encoding="utf-8")
    (analysis / "results.md").write_text("# Results", encoding="utf-8")
    (analysis / "open-problems.md").write_text(
        "# Problems\n\n## OP-001: Test\n\nSolve it.\n",
        encoding="utf-8",
    )
    common.write_json(
        analysis / "manifest.json",
        {
            "schema_version": 2,
            "paper_title": "Test Paper",
            "paper_authors": ["Ada Lovelace"],
            "open_problems": [
                {"id": "OP-001", "title": "Test", "explicitness": "explicit"}
            ],
        },
    )
    return paper


def fake_plan(argv: list[str], *, probe: dict | None = None) -> dict:
    return {
        "action": "solve",
        "title": "Fake task",
        "units": [
            {
                "label": "Fake run",
                "argv": argv,
                "cwd": str(PROJECT_ROOT),
                "targets": [],
                "probe": probe or {},
            }
        ],
    }


class WorkbenchPlanningTests(unittest.TestCase):
    def test_request_json_consumes_exact_body_before_next_request(self):
        payload = b'{"action":"literature"}'
        trailing = b"GET /research HTTP/1.1\r\n"
        handler = object.__new__(workbench.WorkbenchHandler)
        handler.headers = {"Content-Length": str(len(payload))}
        handler.rfile = BytesIO(payload + trailing)
        handler.close_connection = False

        self.assertEqual(handler.read_json(), {"action": "literature"})
        self.assertEqual(handler.rfile.read(), trailing)
        self.assertFalse(handler.close_connection)

    def test_incomplete_request_body_forces_connection_close(self):
        handler = object.__new__(workbench.WorkbenchHandler)
        handler.headers = {"Content-Length": "20"}
        handler.rfile = BytesIO(b"{}")
        handler.close_connection = False

        with self.assertRaisesRegex(PlanError, "incomplete request body"):
            handler.read_json()
        self.assertTrue(handler.close_connection)

    def test_spa_routes_and_shared_assets_are_served(self):
        class RecordingHandler(workbench.WorkbenchHandler):
            def _host_is_allowed(self):
                return True

            def _send_asset(self, name):
                self.sent_asset = name

            def send_error_json(self, status, message):
                self.error = (status, message)

        for path in ("/", "/research", "/papers", "/manuscripts", "/activity"):
            handler = object.__new__(RecordingHandler)
            handler.path = f"{path}?q=test"
            handler.do_GET()
            self.assertEqual(handler.sent_asset, "index.html")

        handler = object.__new__(RecordingHandler)
        handler.path = "/review_tokens.css"
        handler.do_GET()
        self.assertEqual(handler.sent_asset, "review_tokens.css")

    def test_workbench_assets_use_stable_history_routes_and_shared_model(self):
        app = (PROJECT_ROOT / "src" / "workbench_web" / "app.js").read_text(
            encoding="utf-8"
        )
        model = (
            PROJECT_ROOT / "src" / "workbench_web" / "review_model.js"
        ).read_text(encoding="utf-8")
        self.assertIn('research: "/research"', app)
        self.assertIn('window.addEventListener("popstate"', app)
        self.assertIn('since: String(state.eventSequence || 0)', app)
        self.assertIn('result.code === "invalid_confirmation_token"', app)
        self.assertIn('await api("/api/bootstrap", {}, false)', app)
        self.assertIn("previousConnection?.close()", app)
        self.assertIn("eventReconnectNeedsRefresh = true", app)
        self.assertIn("if (eventReconnectNeedsRefresh)", app)
        self.assertIn("if (eventConnection !== events) return", app)
        self.assertIn("return api(path, options, false)", app)
        server = (PROJECT_ROOT / "src" / "workbench.py").read_text(
            encoding="utf-8"
        )
        self.assertIn('code="invalid_confirmation_token"', server)
        self.assertIn('self.send_header("Connection", "close")', server)
        self.assertLess(
            server.index("body = self.read_json()"),
            server.index("if not self.require_mutation_auth()"),
        )
        self.assertIn('raise PlanError("incomplete request body")', server)
        self.assertIn("except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):", server)
        self.assertIn('history[method](historyPayload(scrollY), "", url)', app)
        self.assertIn('value.searchParams.delete("detail")', app)
        self.assertIn("pageScrollPositions.get(scrollPositionKey(url))", app)
        self.assertIn("identityFromSearchParams(parameters)", app)
        self.assertIn("reviewModel.groupProblemsByPaper(", app)
        self.assertIn('parameters.set("sort", state.paperSort)', app)
        self.assertIn("reviewModel.paperSortOptions", app)
        self.assertIn("reviewModel.sortPapers(", app)
        self.assertIn('node("div", "paper-list-controls")', app)
        self.assertIn("reviewModel.paperTitleWithYear(", app)
        self.assertIn("`${paperTitle} · ${item.problemId}/${item.attemptName}`", app)
        self.assertIn("researchFiltersOpen: false", app)
        self.assertIn("sidebarScroll: { research: 0, papers: 0, manuscripts: 0, activity: 0 }", app)
        self.assertIn('sidebar.querySelector(".research-filters")', app)
        self.assertIn("details.open = state.researchFiltersOpen", app)
        self.assertIn("state.researchFiltersOpen = details.open", app)
        self.assertIn("function rememberSidebarScroll()", app)
        self.assertIn("function restoreSidebarScroll(tab)", app)
        self.assertIn('renderedTab === "research"', app)
        self.assertIn('restoreSidebarScroll("activity")', app)
        self.assertIn("const selectionStart = input.selectionStart", app)
        self.assertIn("selectionDirection", app)
        self.assertIn("focus({ preventScroll: true })", app)
        self.assertNotIn("setSelectionRange(state.search.length", app)
        self.assertIn("reviewModel.summaryCards(item)", app)
        self.assertIn("reviewModel.createMarkdownRenderer(window)", app)
        self.assertIn("function visibleProblemSelectionControl", app)
        self.assertIn("function awaitingReviewAttemptsForTargets", app)
        self.assertIn("appendAwaitingReviewAction(values)", app)
        self.assertIn("awaiting-review attempt", app)
        self.assertIn("input.indeterminate", app)
        self.assertIn("jobDetails: new Map()", app)
        self.assertIn("preserveActivityDetail", app)
        self.assertIn("refreshVisibleRunLogs", app)
        self.assertIn("Running dry-run previews", app)
        self.assertIn("preview?.output", app)
        self.assertIn("function formatDuration", app)
        self.assertIn("function runConsoleStatus", app)
        self.assertIn("tags: taskStatusBadge(job.status)", app)
        self.assertIn('meta: `Created ${formatTime(job.created_at)}`', app)
        self.assertIn("row.append(node(\"span\", \"run-timing-separator\", \"·\"), taskStatusBadge(run.status))", app)
        self.assertNotIn('Exit ${run.exit_code ?? "—"}', app)
        self.assertIn("function filtersFromSearchParams", model)
        self.assertIn("function identityToSearchParams", model)
        self.assertIn("function queueSummary", model)
        self.assertIn("function paperResultWeight", model)
        self.assertIn("function paperTitleWithYear", model)
        self.assertIn("function groupProblemsByPaper(items, sort", model)
        self.assertIn('["results", "Most results (weighted)"]', model)
        styles = (
            PROJECT_ROOT / "src" / "workbench_web" / "styles.css"
        ).read_text(encoding="utf-8")
        self.assertIn(".selection-bar[hidden] { display: none; }", styles)
        self.assertIn(".notice { position: fixed; z-index: 25;", styles)
        self.assertIn(".selection-bar { position: fixed; z-index: 30;", styles)
        self.assertIn("bottom: 12px", styles)
        self.assertIn(".selection-bar .button:hover, .selection-bar .button:focus-visible", styles)
        self.assertIn(".selection-bar .button:active", styles)
        self.assertIn(".side-card > span > .badge", styles)
        self.assertIn(".paper-list-controls { display: grid; gap: 6px; }", styles)
        self.assertNotIn(".research-controls > .search", styles)
        self.assertIn(".console-cursor", styles)
        self.assertIn("prefers-reduced-motion: reduce", styles)

    def test_network_bind_and_request_host_security(self):
        args = build_parser().parse_args(
            ["--host", "0.0.0.0", "--allowed-host", "research.local", "."]
        )
        self.assertEqual(args.host, "0.0.0.0")
        self.assertEqual(args.allowed_host, ["research.local"])
        self.assertTrue(
            _request_hostname_allowed(
                "192.168.1.20",
                network_enabled=True,
                allowed_hostnames=set(),
            )
        )
        self.assertTrue(
            _request_hostname_allowed(
                "research.local",
                network_enabled=True,
                allowed_hostnames={"research.local"},
            )
        )
        self.assertFalse(
            _request_hostname_allowed(
                "attacker.example",
                network_enabled=True,
                allowed_hostnames=set(),
            )
        )
        self.assertFalse(
            _request_hostname_allowed(
                "192.168.1.20",
                network_enabled=False,
                allowed_hostnames=set(),
            )
        )
        self.assertTrue(
            _same_request_origin(
                "192.168.1.20:35007",
                "http://192.168.1.20:35007",
            )
        )
        self.assertFalse(
            _same_request_origin(
                "192.168.1.20:35007",
                "http://attacker.example:35007",
            )
        )
        self.assertFalse(
            _same_request_origin(
                "192.168.1.20:35007",
                "http://192.168.1.20:35008",
            )
        )

    def test_review_inventory_reports_row_progress(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            paper = make_paper(root)
            progress = []

            records = workbench._review_inventory(
                [root],
                progress=lambda current, total: progress.append((current, total)),
            )

            self.assertEqual(len(records), 1)
            self.assertEqual(progress, [(0, 1), (1, 1)])
            self.assertEqual(records[0]["paperUrlKey"], paper.as_posix())
            self.assertEqual(records[0]["files"], [])
            self.assertTrue(human_review.load_review_files(records[0]))

    def test_review_inventory_reports_freshness_stages(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            make_paper(root)
            stages = []

            workbench._review_inventory(
                [root],
                stage_progress=lambda label, current, total: stages.append(
                    (label, current, total)
                ),
            )

            self.assertIn(("Checking solution reviews…", 0, 0), stages)
            self.assertIn(("Checking triage and literature…", 0, 1), stages)
            self.assertIn(("Checking triage and literature…", 1, 1), stages)

    def test_file_digest_cache_reuses_unchanged_inputs(self):
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "value.txt"
            path.write_text("first", encoding="utf-8")
            files = [("value.txt", path)]
            before = common._files_digest_from_signature.cache_info()

            first = common.files_digest(files)
            second = common.files_digest(files)
            reused = common._files_digest_from_signature.cache_info()
            path.write_text("second value", encoding="utf-8")
            changed = common.files_digest(files)
            after = common._files_digest_from_signature.cache_info()

            self.assertEqual(first, second)
            self.assertNotEqual(first, changed)
            self.assertGreater(reused.hits, before.hits)
            self.assertGreater(after.misses, reused.misses)

    def test_manuscript_sources_include_legacy_paper_and_problem_titles(self):
        with TemporaryDirectory() as temporary:
            paper = make_paper(Path(temporary))
            manifest = {
                "input_attempts": [
                    {
                        "paper_directory": str(paper),
                        "problem_id": "OP-001",
                    }
                ]
            }

            sources = workbench._manuscript_sources(
                manifest,
                [{"path": str(paper), "title": "Test Paper"}],
                [
                    {
                        "paperDirectory": str(paper),
                        "problemId": "OP-001",
                        "problemTitle": "Test conjecture",
                    }
                ],
            )

            self.assertEqual(
                [source["title"] for source in sources["papers"]],
                ["Test Paper"],
            )
            self.assertEqual(
                [source["title"] for source in sources["problems"]],
                ["Test conjecture"],
            )

    def test_manuscript_inventory_reports_draft_progress(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            paper = make_paper(root / "papers")
            manuscript = root / "manuscripts" / "test"
            for number in (1, 2):
                draft = manuscript / f"draft-{number:03d}"
                draft.mkdir(parents=True)
                common.write_json(
                    draft / "manifest.json",
                    {
                        "title": "Test manuscript",
                        "input_selectors": [
                            {"kind": "paper", "path": str(paper)}
                        ],
                    },
                )
            papers = [{"path": str(paper), "title": "Test Paper"}]
            progress = []

            records = workbench._manuscript_inventory(
                manuscript.parent,
                papers,
                [],
                progress=lambda current, total: progress.append(
                    (current, total)
                ),
            )

            self.assertEqual(len(records), 1)
            self.assertEqual(progress, [(0, 2), (1, 2), (2, 2)])

    def test_event_hub_exposes_bootstrap_sequence(self):
        hub = EventHub()
        self.assertEqual(hub.current_sequence(), 0)
        hub.publish("catalog.progress", phase="papers")
        hub.publish("catalog.changed", version=1)
        self.assertEqual(hub.current_sequence(), 2)

    def test_catalog_cache_makes_restart_immediately_browsable(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            paper = make_paper(root / "papers")
            manuscripts = root / "manuscripts"
            cache = root / "state" / "catalog-cache.json"
            first = CatalogManager(
                [paper.parent],
                manuscripts,
                EventHub(),
                cache_path=cache,
            )
            try:
                self.assertTrue(first.wait_until_ready(8))
                self.assertTrue(cache.is_file())
                version = first.version
            finally:
                first.close()

            scan_allowed = threading.Event()

            def slow_paper_inventory(paths):
                scan_allowed.wait(5)
                return []

            with patch(
                "workbench._paper_inventory",
                side_effect=slow_paper_inventory,
            ):
                second = CatalogManager(
                    [paper.parent],
                    manuscripts,
                    EventHub(),
                    cache_path=cache,
                )
                try:
                    snapshot = second.snapshot()
                    self.assertEqual(snapshot["version"], version)
                    self.assertTrue(snapshot["loading"])
                    self.assertEqual(len(snapshot["papers"]), 1)
                    self.assertEqual(
                        snapshot["progress"]["phase"], "refreshing"
                    )
                finally:
                    scan_allowed.set()
                    second.close()

    def test_solver_plan_preserves_prompt_round_and_critic_settings(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            paper = make_paper(root)
            problem = paper / "OP-001"
            plan = build_plan(
                {
                    "action": "solve",
                    "targets": [
                        {"kind": "problem", "path": str(problem), "label": "OP-001"}
                    ],
                    "options": {
                        "prompt": "Try small cases.",
                        "reviewPrompt": "Check the boundary case.",
                        "maxRounds": 3,
                        "review": "all",
                    },
                },
                project_root=PROJECT_ROOT,
                allowed_roots=[root],
                manuscripts=root / "manuscripts",
                catalog_version=7,
            )

            self.assertEqual(plan["catalogVersion"], 7)
            self.assertEqual(len(plan["units"]), 1)
            argv = plan["units"][0]["argv"]
            self.assertEqual(argv[argv.index("--max-rounds") + 1], "3")
            self.assertEqual(argv[argv.index("--review") + 1], "all")
            self.assertIn("Try small cases.", argv)
            self.assertIn("Check the boundary case.", argv)

    def test_triage_groups_problem_targets_by_paper(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = make_paper(root / "first")
            second = make_paper(root / "second")
            targets = [
                {"kind": "problem", "path": str(first / "OP-001")},
                {"kind": "problem", "path": str(second / "OP-001")},
            ]
            plan = build_plan(
                {"action": "triage", "targets": targets, "options": {}},
                project_root=PROJECT_ROOT,
                allowed_roots=[root],
                manuscripts=root / "manuscripts",
                catalog_version=1,
            )
            self.assertEqual(len(plan["units"]), 2)

    def test_planner_rejects_targets_outside_configured_roots(self):
        with TemporaryDirectory() as temporary, TemporaryDirectory() as outside:
            root = Path(temporary)
            paper = make_paper(Path(outside))
            with self.assertRaisesRegex(PlanError, "outside the configured roots"):
                build_plan(
                    {
                        "action": "analyze",
                        "targets": [{"kind": "paper", "path": str(paper)}],
                        "options": {},
                    },
                    project_root=PROJECT_ROOT,
                    allowed_roots=[root],
                    manuscripts=root / "manuscripts",
                    catalog_version=1,
                )

    @patch("workbench_tasks.subprocess.run")
    def test_plan_preview_runs_exact_command_in_dry_run_mode(self, run):
        run.return_value = Mock(
            returncode=0,
            stdout="Would solve: OP-001\nSelected 1 problem.\n",
            stderr="",
        )
        plan = fake_plan([sys.executable, "tool.py", "OP-001"])

        returned = populate_dry_run_previews(plan)

        self.assertIs(returned, plan)
        self.assertEqual(
            run.call_args.args[0],
            [sys.executable, "tool.py", "OP-001", "--dry-run"],
        )
        self.assertEqual(plan["units"][0]["argv"][-1], "OP-001")
        self.assertEqual(plan["units"][0]["dryRun"]["status"], "ok")
        self.assertIn("Would solve", plan["units"][0]["dryRun"]["output"])

    @patch("workbench_tasks.subprocess.run")
    def test_plan_preview_preserves_failed_dry_run_output(self, run):
        run.return_value = Mock(
            returncode=2,
            stdout="",
            stderr="invalid target\n",
        )
        plan = fake_plan([sys.executable, "tool.py", "missing"])

        populate_dry_run_previews(plan)

        preview = plan["units"][0]["dryRun"]
        self.assertEqual(preview["status"], "failed")
        self.assertEqual(preview["exitCode"], 2)
        self.assertIn("Standard error:\ninvalid target", preview["output"])


class WorkbenchStoreTests(unittest.TestCase):
    def test_worker_persists_console_and_success(self):
        with TemporaryDirectory() as temporary:
            state = Path(temporary)
            store = WorkbenchStore(state / "workbench.sqlite3", state)
            job = store.create_job(
                {"action": "solve"},
                fake_plan([sys.executable, "-u", "-c", "print('hello workbench')"]),
            )
            run = job["runs"][0]
            store.mark_starting(run["id"])

            self.assertEqual(
                workbench_worker.run_worker(store.database, run["id"]),
                0,
            )

            saved = store.get_run(run["id"])
            self.assertEqual(saved["status"], "succeeded")
            self.assertEqual(saved["exit_code"], 0)
            self.assertIn(
                "hello workbench",
                Path(saved["log_path"]).read_text(encoding="utf-8"),
            )
            self.assertEqual(store.get_job(job["id"])["status"], "succeeded")

    def test_partial_solver_output_is_discovered_and_not_blindly_retried(self):
        with TemporaryDirectory() as temporary:
            state = Path(temporary)
            problem = state / "OP-001"
            problem.mkdir()
            code = (
                "from pathlib import Path; import json; "
                f"p=Path({str(problem)!r})/'attempt-001'; p.mkdir(); "
                "(p/'solver-result.json').write_text('{}'); raise SystemExit(2)"
            )
            store = WorkbenchStore(state / "workbench.sqlite3", state)
            plan = fake_plan(
                [sys.executable, "-u", "-c", code],
                probe={"kind": "solve", "problem": str(problem), "before": []},
            )
            job = store.create_job({"action": "solve"}, plan)
            run = job["runs"][0]
            store.mark_starting(run["id"])
            workbench_worker.run_worker(store.database, run["id"])

            saved = store.get_run(run["id"])
            self.assertEqual(saved["status"], "partial")
            self.assertEqual(saved["outputs"], [str((problem / "attempt-001").resolve())])
            with self.assertRaisesRegex(ValueError, "installed output"):
                store.retry_run(run["id"])

    def test_stale_heartbeat_becomes_interrupted_and_can_retry(self):
        with TemporaryDirectory() as temporary:
            state = Path(temporary)
            store = WorkbenchStore(state / "workbench.sqlite3", state)
            job = store.create_job(
                {"action": "solve"},
                fake_plan([sys.executable, "-c", "pass"]),
            )
            run = job["runs"][0]
            store.mark_starting(run["id"])
            store.update_run(run["id"], heartbeat_at=time.time() - 100)

            self.assertEqual(store.mark_stale_runs(older_than=10), [run["id"]])
            self.assertEqual(store.get_run(run["id"])["status"], "interrupted")
            retry = store.retry_run(run["id"])
            self.assertEqual(retry["status"], "queued")
            self.assertEqual(retry["retry_of"], run["id"])

    def test_probe_handles_missing_problem_directory(self):
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "missing"
            self.assertEqual(
                probe_outputs({"kind": "solve", "problem": str(path), "before": []}),
                [],
            )


class WorkbenchWatchTests(unittest.TestCase):
    def test_watchdog_ignores_directory_metadata_changes(self):
        catalog = Mock()
        handler = ChangeHandler(catalog)

        handler.on_any_event(
            SimpleNamespace(
                event_type="modified",
                is_directory=True,
                src_path=str(PROJECT_ROOT / "papers"),
            )
        )

        catalog.schedule.assert_not_called()

    def test_watchdog_schedules_file_content_changes(self):
        catalog = Mock()
        handler = ChangeHandler(catalog)

        handler.on_any_event(
            SimpleNamespace(
                event_type="modified",
                is_directory=False,
                src_path=str(PROJECT_ROOT / "papers" / "metadata.json"),
            )
        )

        catalog.schedule.assert_called_once_with()

    def test_task_watchdog_wakes_only_for_sqlite_files(self):
        scheduler = Mock()
        database = PROJECT_ROOT / ".loose-ends" / "workbench.sqlite3"
        handler = workbench.TaskChangeHandler(scheduler, database)

        handler.on_any_event(
            SimpleNamespace(
                event_type="modified",
                is_directory=False,
                src_path=str(database) + "-wal",
            )
        )
        handler.on_any_event(
            SimpleNamespace(
                event_type="modified",
                is_directory=False,
                src_path=str(database.parent / "console.log"),
            )
        )

        scheduler.schedule.assert_called_once_with()

    def test_idle_scheduler_waits_until_explicitly_woken(self):
        store = Mock()
        store.revision.return_value = 0.0
        store.mark_stale_runs.return_value = []
        store.active_count.return_value = 0
        store.queued_runs.return_value = []
        scheduler = workbench.Scheduler(
            store,
            Mock(),
            max_workers=1,
            state_directory=PROJECT_ROOT / ".loose-ends",
        )
        try:
            deadline = time.time() + 2
            while (
                store.mark_stale_runs.call_count == 0
                and time.time() < deadline
            ):
                time.sleep(0.01)
            checks = store.mark_stale_runs.call_count
            self.assertGreater(checks, 0)
            time.sleep(0.55)
            self.assertEqual(store.mark_stale_runs.call_count, checks)

            scheduler.schedule()
            deadline = time.time() + 2
            while (
                store.mark_stale_runs.call_count == checks
                and time.time() < deadline
            ):
                time.sleep(0.01)
            self.assertGreater(store.mark_stale_runs.call_count, checks)
        finally:
            scheduler.close()

    def test_sqlite_wal_change_wakes_scheduler(self):
        with TemporaryDirectory() as temporary:
            state = Path(temporary)
            store = WorkbenchStore(state / "workbench.sqlite3", state)
            job = store.create_job(
                {"action": "solve"},
                fake_plan([sys.executable, "-c", "pass"]),
            )
            run = job["runs"][0]
            store.mark_starting(run["id"])
            store.update_run(
                run["id"],
                status="running",
                heartbeat_at=time.time(),
            )
            hub = EventHub()
            scheduler = workbench.Scheduler(
                store,
                hub,
                max_workers=1,
                state_directory=state,
            )
            observer = Observer()
            observer.schedule(
                workbench.TaskChangeHandler(scheduler, store.database),
                str(state),
                recursive=False,
            )
            observer.start()
            try:
                time.sleep(0.15)
                sequence = hub.current_sequence()
                store.update_run(run["id"], heartbeat_at=time.time())
                deadline = time.time() + 5
                while (
                    hub.current_sequence() == sequence
                    and time.time() < deadline
                ):
                    time.sleep(0.05)
                self.assertGreater(hub.current_sequence(), sequence)
            finally:
                observer.stop()
                observer.join(5)
                scheduler.close()

    @unittest.skipUnless(sys.platform == "win32", "Windows process flags")
    def test_scheduler_launches_worker_without_a_console_window(self):
        scheduler = object.__new__(workbench.Scheduler)
        scheduler.store = Mock()
        scheduler.store.mark_starting.return_value = True
        scheduler.hub = Mock()
        scheduler.state_directory = PROJECT_ROOT / ".loose-ends"
        scheduler.last_launch = 0.0

        with patch.object(workbench.subprocess, "Popen") as popen:
            scheduler._launch({"id": "test-run"})

        options = popen.call_args.kwargs
        self.assertTrue(
            options["creationflags"] & subprocess.CREATE_NO_WINDOW
        )
        self.assertTrue(
            options["startupinfo"].dwFlags
            & subprocess.STARTF_USESHOWWINDOW
        )

    def test_initial_catalog_scan_does_not_block_server_startup(self):
        scan_allowed = threading.Event()

        def slow_paper_inventory(paths):
            scan_allowed.wait(5)
            return []

        with TemporaryDirectory() as temporary, patch(
            "workbench._paper_inventory",
            side_effect=slow_paper_inventory,
        ), patch(
            "workbench._review_inventory",
            return_value=[],
        ), patch(
            "workbench._manuscript_inventory",
            return_value=[],
        ):
            started = time.monotonic()
            manager = CatalogManager(
                [Path(temporary)],
                Path(temporary) / "manuscripts",
                EventHub(),
            )
            try:
                self.assertLess(time.monotonic() - started, 0.5)
                self.assertTrue(manager.snapshot()["loading"])
                scan_allowed.set()
                self.assertTrue(manager.wait_until_ready(2))
                self.assertFalse(manager.snapshot()["loading"])
                manager.refresh()
                self.assertFalse(manager.snapshot()["loading"])
            finally:
                scan_allowed.set()
                manager.close()

    def test_watchdog_change_invalidates_lazy_review_details(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            paper = make_paper(root)
            manager = CatalogManager([root], root / "manuscripts", EventHub())
            observer = Observer()
            observer.schedule(ChangeHandler(manager), str(root), recursive=True)
            observer.start()
            self.addCleanup(manager.close)
            self.addCleanup(observer.join, 5)
            self.addCleanup(observer.stop)
            self.assertTrue(manager.wait_until_ready(8))
            initial = manager.version

            (paper / "analysis" / "open-problems.md").write_text(
                "# Problems\n\n## OP-001: Test\n\nUpdated statement.\n",
                encoding="utf-8",
            )
            deadline = time.time() + 8
            while manager.version == initial and time.time() < deadline:
                time.sleep(0.1)

            self.assertGreater(manager.version, initial)


if __name__ == "__main__":
    unittest.main()
