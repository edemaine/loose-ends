from pathlib import Path
from io import BytesIO
import json
import os
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


def fake_plan(
    argv: list[str],
    *,
    resources: list[str] | None = None,
    unit_count: int = 1,
    priority_level: int = 0,
) -> dict:
    return {
        "action": "solve",
        "title": "Fake task",
        "priorityLevel": priority_level,
        "units": [
            {
                "label": f"Fake run {index + 1}",
                "argv": argv,
                "cwd": str(PROJECT_ROOT),
                "targets": [],
                "resources": resources or [],
            }
            for index in range(unit_count)
        ],
    }


class WorkbenchPlanningTests(unittest.TestCase):
    def test_catalog_change_does_not_invalidate_a_confirmed_plan(self):
        app = object.__new__(workbench.WorkbenchApplication)
        app.plan_lock = threading.Lock()
        app.plans = {
            "plan": {
                "created": time.time(),
                "request": {"action": "solve"},
                "plan": {"catalogVersion": 1},
            }
        }
        app.catalog = SimpleNamespace(version=2)
        app.store = Mock()
        app.store.create_job.return_value = {"id": "job"}
        app.scheduler = Mock()
        app.hub = Mock()

        self.assertEqual(app.confirm_plan("plan"), {"id": "job"})
        app.store.create_job.assert_called_once()

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

            def _send_file(self, value, *, raw=False):
                self.sent_file = (value, raw)

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

        handler = object.__new__(RecordingHandler)
        handler.path = "/view?path=result.md"
        handler.do_GET()
        self.assertEqual(handler.sent_asset, "viewer.html")

        handler = object.__new__(RecordingHandler)
        handler.path = "/api/file?path=result.md&raw=1"
        handler.do_GET()
        self.assertEqual(handler.sent_file, ("result.md", True))

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
        self.assertIn('const tabUrlStoragePrefix = "loose-ends-workbench:tab-url:"', app)
        self.assertIn("function rememberTabUrl(tab, url)", app)
        self.assertIn("function rememberedTabUrl(tab)", app)
        self.assertIn("localStorage.setItem(", app)
        self.assertIn("localStorage.getItem(", app)
        self.assertIn("rememberTabUrl(state.tab, canonicalUrl)", app)
        self.assertIn("function restoreTab(tab)", app)
        self.assertIn("restoreTab(value.dataset.tab)", app)
        self.assertIn('value.searchParams.delete("detail")', app)
        self.assertIn("pageScrollPositions.get(scrollPositionKey(url))", app)
        self.assertIn("identityFromSearchParams(parameters)", app)
        self.assertIn("reviewModel.groupProblemsByPaper(", app)
        self.assertIn('parameters.set("sort", state.paperSort)', app)
        self.assertIn("reviewModel.paperSortOptions", app)
        self.assertIn("reviewModel.sortPapers(", app)
        self.assertIn('node("div", "paper-list-controls")', app)
        self.assertIn("reviewModel.paperTitleWithYear(", app)
        self.assertIn("`${item.problemId}: ${item.problemTitle} · ${item.attemptName}`", app)
        self.assertIn("function targetDisplayLabel(value)", app)
        self.assertIn('target.kind === "draft"', app)
        self.assertIn("relation.manuscriptWide", app)
        self.assertIn("relation.paperDescendant", app)
        self.assertIn("function syncRelatedTasks()", app)
        self.assertIn('["research", "papers", "manuscripts"].includes(state.tab)', app)
        self.assertIn("function historicalAttemptTarget(value)", app)
        self.assertIn("function taskTargetsForRequest(task)", app)
        self.assertIn('"Pin to attempt"', app)
        self.assertIn("targets: taskTargetsForRequest(task)", app)
        self.assertIn('action === "review" && attempts.length === 1', app)
        self.assertIn("researchFiltersOpen: false", app)
        self.assertIn("revealSidebarSelection: false", app)
        self.assertIn("sidebarScroll: { research: 0, papers: 0, manuscripts: 0, activity: 0 }", app)
        self.assertIn('sidebar.querySelector(".research-filters")', app)
        self.assertIn("details.open = state.researchFiltersOpen", app)
        self.assertIn("state.researchFiltersOpen = details.open", app)
        self.assertIn("function rememberSidebarScroll()", app)
        self.assertIn("function restoreSidebarScroll(tab)", app)
        self.assertIn("state.revealSidebarSelection = Boolean(requested)", app)
        self.assertIn("state.revealSidebarSelection = Boolean(paper)", app)
        self.assertIn("state.revealSidebarSelection = Boolean(manuscript)", app)
        self.assertIn("state.revealSidebarSelection = state.jobs.some(", app)
        self.assertIn('scrollingElement?.querySelector(".side-card.active")', app)
        self.assertIn("const centered = scrollingElement.scrollTop", app)
        self.assertIn("scrollingElement.scrollHeight - scrollingElement.clientHeight", app)
        self.assertIn("Math.max(0, Math.min(maximum, centered))", app)
        self.assertIn("state.sidebarScroll[tab] = scrollingElement.scrollTop", app)
        self.assertIn('manuscripts: ".manuscript-scroll"', app)
        self.assertIn('manuscripts: ".draft-list"', app)
        self.assertIn("sidebarSecondaryScroll: { research: 0, manuscripts: 0 }", app)
        self.assertIn("state.revealSidebarSecondarySelection = true", app)
        self.assertIn("state.manuscriptDraftSelections.get(value.key)", app)
        self.assertIn('node("div", "draft-switcher")', app)
        self.assertIn('`Drafts · ${manuscript.drafts.length}`', app)
        self.assertNotIn('node("h2", "", "Draft history")', app)
        self.assertIn('restoreSidebarScroll("activity")', app)
        self.assertIn("const sidebarControlNodes = new Map()", app)
        self.assertIn("function persistentSidebarControls(tab, create)", app)
        self.assertIn("sidebarControlNodes.get(tab)", app)
        self.assertIn("while (controls.nextSibling) controls.nextSibling.remove()", app)
        self.assertIn("function syncSidebarControls(tab, controls)", app)
        self.assertIn('select.dataset.filterKey = key', app)
        self.assertIn('controls.querySelector("input.search")', app)
        self.assertNotIn("const selectionStart = input.selectionStart", app)
        self.assertIn("reviewModel.summaryCards(item)", app)
        self.assertIn("reviewModel.createMarkdownRenderer(window)", app)
        self.assertIn("function visibleProblemSelectionControl", app)
        self.assertIn("function awaitingReviewAttemptsForTargets", app)
        self.assertIn("appendAwaitingReviewAction(values)", app)
        self.assertIn("awaiting-review attempt", app)
        self.assertIn("input.indeterminate", app)
        self.assertIn("jobDetails: new Map()", app)
        self.assertIn("function outputRoute(path)", app)
        self.assertIn('detail: critiqueFiles.has(filename) ? "critique" : "attempt"', app)
        self.assertIn('if (filename.startsWith("triage")) detail = "triage"', app)
        self.assertIn('else if (filename.startsWith("literature")) detail = "literature"', app)
        self.assertIn('node("a", "artifact-action", "View")', app)
        self.assertIn("link.href = artifactViewUrl(path)", app)
        self.assertIn("open.href = artifactViewUrl(pdf)", app)
        self.assertIn('node("a", "artifact-action", "Raw")', app)
        self.assertIn("function taskScopeSummary(job)", app)
        self.assertIn("function problemRunPresentation(action, run)", app)
        self.assertIn('`${targets.length} selected problem', app)
        self.assertIn("title: paperTitle", app)
        self.assertIn('summary: `${count} · ${labels.join(" · ")}`', app)
        self.assertIn('node("span", "run-target-summary"', app)
        self.assertIn('node("h3", "", "Selected problems")', app)
        self.assertIn("function taskSidebarMeta(job)", app)
        self.assertIn('`${done}/${total} done`', app)
        self.assertIn('pieces.push(`${active} running`)', app)
        self.assertIn("function latestJobRuns(job)", app)
        self.assertIn("function appendRunCountBadge(parent, count, status)", app)
        self.assertIn("const mixedTerminal = terminal && outcomeKinds > 1", app)
        self.assertIn("function runAttentionPanel(job)", app)
        self.assertIn('node("h2", "", "Needs attention")', app)
        self.assertIn('button("Show run", () => focusRun(job, run)', app)
        self.assertIn("section.dataset.runId = run.id", app)
        self.assertIn("expandedRuns: new Set()", app)
        self.assertIn('node("div", "run-expanded")', app)
        self.assertIn("preserveActivityDetail", app)
        self.assertIn("function updateActivityCount()", app)
        self.assertIn("!schedulerControl.contains(event.target)", app)
        self.assertIn("function adjustWorkerLimit(delta)", app)
        self.assertIn("workerLimitTimer = setTimeout", app)
        self.assertIn("${counts.active}/${configuredLimit}", app)
        self.assertNotIn("workerApply", app)
        self.assertIn('if (!navigationReady || state.tab !== "activity") return', app)
        refresh_jobs = app[app.index("async function refreshJobs"):app.index("function connectEvents")]
        self.assertNotIn("syncNavigation", refresh_jobs)
        self.assertIn("refreshVisibleRunLogs", app)
        self.assertIn("Running dry-run previews", app)
        self.assertIn("preview?.output", app)
        self.assertIn("function formatDuration", app)
        self.assertIn("function runConsoleStatus", app)
        self.assertIn("tags: taskBadges(job)", app)
        self.assertIn('taskIsPaused(job) ? badge("Paused", "paused")', app)
        self.assertIn(
            'values.append(badge(`Weight ${priorityMultiplier(job.priority_level)}`, "neutral"))',
            app,
        )
        self.assertIn('job.scheduling_paused ? "Resume" : "Pause"', app)
        self.assertIn(
            'node("small", "", "Proportion of workers assigned to this task")',
            app,
        )
        self.assertIn("meta: taskSidebarMeta(job)", app)
        self.assertIn("row.append(node(\"span\", \"run-timing-separator\", \"·\"), taskStatusBadge(run.status))", app)
        self.assertNotIn('Exit ${run.exit_code ?? "—"}', app)
        viewer = (
            PROJECT_ROOT / "src" / "workbench_web" / "viewer.js"
        ).read_text(encoding="utf-8")
        viewer_html = (
            PROJECT_ROOT / "src" / "workbench_web" / "viewer.html"
        ).read_text(encoding="utf-8")
        styles = (
            PROJECT_ROOT / "src" / "workbench_web" / "styles.css"
        ).read_text(encoding="utf-8")
        self.assertIn("createMarkdownRenderer(window)", viewer)
        self.assertIn('query.set("raw", "1")', viewer)
        self.assertIn('extension === "pdf"', viewer)
        self.assertIn("function setPdfDarkMode(frame, enabled)", viewer)
        self.assertIn('frame.className = "viewer-frame pdf-inverted"', viewer)
        self.assertIn('id="viewer-pdf-theme"', viewer_html)
        self.assertIn(".viewer-frame.pdf-inverted", styles)
        self.assertIn("invert(1) hue-rotate(180deg)", styles)
        self.assertIn("function appendHighlightedJson(pre, value)", viewer)
        self.assertIn('span.className = `json-${kind}`', viewer)
        self.assertIn("function filtersFromSearchParams", model)
        self.assertIn("function createDefaultFilters(initialPriorities = priorityLevels)", model)
        self.assertIn("const initialPriorities = [...reviewModel.priorityLevels]", app)
        self.assertIn('["attempt", "Attempt (current)"]', model)
        self.assertIn('triage: "triage"', model)
        self.assertIn('const triage = filters.triage || "all"', model)
        self.assertIn('item.triageClassification !== triage || !item.triageCurrent', model)
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
        self.assertIn(".badge.paused", styles)
        self.assertIn(".badge.neutral { background: #332f35; color: #fff; }", styles)
        self.assertNotIn(".badge.weight", styles)

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
                        "attempt_path": str(paper / "OP-001" / "attempt-001"),
                        "attempt_name": "attempt-001",
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
            self.assertTrue(sources["problems"][0]["pinned"])
            self.assertEqual(sources["pinning"], {"pinned": 1, "tracking": 0})

            manifest["input_selectors"] = [
                {"kind": "problem", "path": str(paper / "OP-001")}
            ]
            tracking = workbench._manuscript_sources(
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
            self.assertFalse(tracking["problems"][0]["pinned"])
            self.assertEqual(
                tracking["problems"][0]["selectorKind"],
                "problem",
            )
            self.assertEqual(tracking["pinning"], {"pinned": 0, "tracking": 1})

            manifest["input_selectors"] = [
                {"kind": "paper", "path": str(paper)},
                {
                    "kind": "pin",
                    "path": str(paper / "OP-001" / "attempt-001"),
                },
            ]
            overridden = workbench._manuscript_sources(
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
            self.assertTrue(overridden["problems"][0]["pinned"])
            self.assertEqual(overridden["problems"][0]["selectorKind"], "pin")

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
                        "generated_at": f"2026-08-0{number}T12:00:00+00:00",
                        "input_selectors": [
                            {"kind": "paper", "path": str(paper)}
                        ],
                    },
                )
                (draft / "main.tex").write_text(
                    "\\begin{abstract}\n"
                    "A \\emph{concise} abstract with \\textbf{strong} "
                    "$O(n^2)$ work by Gourv\\`es, Erd\\H{o}s, "
                    "Fran\\c{c}ois, and a na\\\"ive coauthor. % hidden\n"
                    "\\end{abstract}\n",
                    encoding="utf-8",
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
            self.assertEqual(
                records[0]["latest"]["abstract"],
                "A *concise* abstract with **strong** $O(n^2)$ work by "
                "Gourvès, Erdős, François, and a naïve coauthor.",
            )
            self.assertGreater(
                records[0]["latest"]["createdTimestamp"],
                records[0]["drafts"][0]["createdTimestamp"],
            )
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
                    self.assertFalse(snapshot["loading"])
                    self.assertEqual(len(snapshot["papers"]), 1)
                    self.assertNotIn("progress", snapshot)
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
                        "priorityLevel": 2,
                    },
                },
                project_root=PROJECT_ROOT,
                allowed_roots=[root],
                manuscripts=root / "manuscripts",
                catalog_version=7,
            )

            self.assertEqual(plan["catalogVersion"], 7)
            self.assertEqual(plan["priorityLevel"], 2)
            self.assertEqual(len(plan["units"]), 1)
            argv = plan["units"][0]["argv"]
            self.assertEqual(argv[argv.index("--max-rounds") + 1], "3")
            self.assertEqual(argv[argv.index("--review") + 1], "all")
            self.assertIn("Try small cases.", argv)
            self.assertIn("Check the boundary case.", argv)

    def test_planner_rejects_priority_outside_displayed_range(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            paper = make_paper(root)
            with self.assertRaisesRegex(PlanError, "priorityLevel"):
                build_plan(
                    {
                        "action": "solve",
                        "targets": [
                            {
                                "kind": "problem",
                                "path": str(paper / "OP-001"),
                            }
                        ],
                        "options": {"priorityLevel": 4},
                    },
                    project_root=PROJECT_ROOT,
                    allowed_roots=[root],
                    manuscripts=root / "manuscripts",
                    catalog_version=1,
                )

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

    def test_problem_plan_locks_only_its_problem(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            paper = make_paper(root)
            problem = (paper / "OP-001").resolve()
            plan = build_plan(
                {
                    "action": "solve",
                    "targets": [{"kind": "problem", "path": str(problem)}],
                    "options": {},
                },
                project_root=PROJECT_ROOT,
                allowed_roots=[root],
                manuscripts=root / "manuscripts",
                catalog_version=1,
            )

            self.assertIn(f"problem:{problem}", plan["units"][0]["resources"])
            self.assertNotIn(
                f"paper:{problem.parent}", plan["units"][0]["resources"]
            )

    def test_write_plan_allows_artifacts_under_manuscript_root(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            paper = make_paper(root)
            manuscripts = root / "manuscripts"
            plan = build_plan(
                {
                    "action": "write",
                    "targets": [{"kind": "paper", "path": str(paper)}],
                    "options": {},
                },
                project_root=PROJECT_ROOT,
                allowed_roots=[root],
                manuscripts=manuscripts,
                catalog_version=1,
            )
            self.assertIn(
                f"manuscript:{manuscripts.resolve()}",
                plan["units"][0]["resources"],
            )

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
    @staticmethod
    def _targeted_plan(
        path: Path, *, kind: str, resource: str
    ) -> dict:
        plan = fake_plan(
            [sys.executable, "-c", "pass"], resources=[resource]
        )
        plan["units"][0]["targets"] = [{"kind": kind, "path": str(path)}]
        return plan

    def test_different_problems_in_one_paper_can_run_together(self):
        with TemporaryDirectory() as temporary:
            state = Path(temporary)
            paper = state / "paper"
            first_problem = paper / "OP-001"
            second_problem = paper / "OP-002"
            store = WorkbenchStore(state / "workbench.sqlite3", state)
            # Paper locks here simulate queued jobs created before problem locks
            # were introduced; target metadata should refine them on read.
            store.create_job(
                {"action": "solve"},
                self._targeted_plan(
                    first_problem,
                    kind="problem",
                    resource=f"paper:{paper}",
                ),
            )
            store.create_job(
                {"action": "solve"},
                self._targeted_plan(
                    second_problem,
                    kind="problem",
                    resource=f"paper:{paper}",
                ),
            )

            first = store.claim_next_run(set())
            store.update_run(
                first["id"], status="running", heartbeat_at=time.time()
            )

            self.assertIsNotNone(store.claim_next_run(store.active_resources()))

    def test_same_problem_runs_conflict(self):
        with TemporaryDirectory() as temporary:
            state = Path(temporary)
            problem = state / "paper" / "OP-001"
            store = WorkbenchStore(state / "workbench.sqlite3", state)
            for _ in range(2):
                store.create_job(
                    {"action": "solve"},
                    self._targeted_plan(
                        problem,
                        kind="problem",
                        resource=f"problem:{problem}",
                    ),
                )

            first = store.claim_next_run(set())
            store.update_run(
                first["id"], status="running", heartbeat_at=time.time()
            )

            self.assertIsNone(store.claim_next_run(store.active_resources()))

    def test_paper_run_conflicts_with_problem_run(self):
        with TemporaryDirectory() as temporary:
            state = Path(temporary)
            paper = state / "paper"
            problem = paper / "OP-001"
            store = WorkbenchStore(state / "workbench.sqlite3", state)
            store.create_job(
                {"action": "analyze"},
                self._targeted_plan(
                    paper,
                    kind="paper",
                    resource=f"paper:{paper}",
                ),
            )
            store.create_job(
                {"action": "solve"},
                self._targeted_plan(
                    problem,
                    kind="problem",
                    resource=f"problem:{problem}",
                ),
            )

            first = store.claim_next_run(set())
            store.update_run(
                first["id"], status="running", heartbeat_at=time.time()
            )

            self.assertIsNone(store.claim_next_run(store.active_resources()))

    def test_weighted_scheduler_shares_starts_proportionally(self):
        with TemporaryDirectory() as temporary:
            state = Path(temporary)
            store = WorkbenchStore(state / "workbench.sqlite3", state)
            regular = store.create_job(
                {"action": "solve"},
                fake_plan([sys.executable, "-c", "pass"], unit_count=12),
            )
            double = store.create_job(
                {"action": "solve"},
                fake_plan(
                    [sys.executable, "-c", "pass"],
                    unit_count=12,
                    priority_level=1,
                ),
            )
            starts = {regular["id"]: 0, double["id"]: 0}

            for _ in range(12):
                run = store.claim_next_run(set())
                self.assertIsNotNone(run)
                starts[run["job_id"]] += 1
                store.update_run(
                    run["id"], status="succeeded", finished_at=time.time()
                )

            self.assertEqual(starts[regular["id"]], 4)
            self.assertEqual(starts[double["id"]], 8)

    def test_task_pause_preserves_active_runs_and_blocks_new_starts(self):
        with TemporaryDirectory() as temporary:
            state = Path(temporary)
            store = WorkbenchStore(state / "workbench.sqlite3", state)
            job = store.create_job(
                {"action": "solve"},
                fake_plan([sys.executable, "-c", "pass"], unit_count=2),
            )
            active = store.claim_next_run(set())
            store.update_run(active["id"], status="running", heartbeat_at=time.time())
            saved = store.update_job_scheduling(job["id"], paused=True)

            self.assertTrue(saved["scheduling_paused"])
            self.assertEqual(store.get_run(active["id"])["status"], "running")
            self.assertIsNone(store.claim_next_run(set()))

            store.update_job_scheduling(job["id"], paused=False)
            self.assertIsNotNone(store.claim_next_run(set()))

    def test_scheduler_settings_persist(self):
        with TemporaryDirectory() as temporary:
            state = Path(temporary)
            database = state / "workbench.sqlite3"
            store = WorkbenchStore(database, state)
            settings = store.update_scheduler_settings(
                worker_limit=7, queue_paused=True
            )
            self.assertEqual(settings["workerLimit"], 7)
            self.assertTrue(settings["queuePaused"])

            reopened = WorkbenchStore(database, state)
            self.assertEqual(reopened.scheduler_settings()["workerLimit"], 7)
            self.assertTrue(reopened.scheduler_settings()["queuePaused"])

    def test_job_counts_only_latest_retry_for_each_part(self):
        with TemporaryDirectory() as temporary:
            state = Path(temporary)
            store = WorkbenchStore(state / "workbench.sqlite3", state)
            job = store.create_job(
                {"action": "solve"},
                fake_plan([sys.executable, "-c", "pass"]),
            )
            original = job["runs"][0]
            store.update_run(
                original["id"],
                status="failed",
                finished_at=time.time(),
                error="test failure",
            )
            retry = store.retry_run(original["id"])

            listed = store.list_jobs()[0]
            self.assertEqual(retry["status"], "queued")
            self.assertEqual(
                listed["counts"],
                {
                    "job_id": job["id"],
                    "queued": 1,
                    "active": 0,
                    "succeeded": 0,
                    "partial": 0,
                    "failed": 0,
                    "canceled": 0,
                    "interrupted": 0,
                    "unsuccessful": 0,
                    "total": 1,
                },
            )

    def test_job_list_includes_only_latest_nonterminal_runs(self):
        with TemporaryDirectory() as temporary:
            state = Path(temporary)
            first_problem = state / "paper" / "OP-001"
            second_problem = state / "paper" / "OP-002"
            plan = fake_plan(
                [sys.executable, "-c", "pass"], unit_count=2
            )
            plan["units"][0]["targets"] = [
                {"kind": "problem", "path": str(first_problem)}
            ]
            plan["units"][1]["targets"] = [
                {"kind": "problem", "path": str(second_problem)}
            ]
            store = WorkbenchStore(state / "workbench.sqlite3", state)
            job = store.create_job({"action": "solve"}, plan)
            first, second = job["runs"]
            store.update_run(
                first["id"], status="succeeded", finished_at=time.time()
            )

            listed = store.list_jobs()[0]

            self.assertEqual(
                listed["liveRuns"],
                [
                    {
                        "id": second["id"],
                        "job_id": job["id"],
                        "unit_index": 1,
                        "label": "Fake run 2",
                        "status": "queued",
                        "targets": [
                            {"kind": "problem", "path": str(second_problem)}
                        ],
                        "created_at": second["created_at"],
                        "started_at": None,
                    }
                ],
            )

    def test_mixed_success_and_failure_is_partial_with_detailed_counts(self):
        with TemporaryDirectory() as temporary:
            state = Path(temporary)
            store = WorkbenchStore(state / "workbench.sqlite3", state)
            job = store.create_job(
                {"action": "solve"},
                fake_plan([sys.executable, "-c", "pass"], unit_count=2),
            )
            first, second = job["runs"]
            store.update_run(
                first["id"], status="succeeded", finished_at=time.time()
            )
            store.update_run(
                second["id"], status="failed", finished_at=time.time()
            )

            self.assertEqual(store.get_job(job["id"])["status"], "partial")
            counts = store.list_jobs()[0]["counts"]
            self.assertEqual(counts["succeeded"], 1)
            self.assertEqual(counts["failed"], 1)
            self.assertEqual(counts["partial"], 0)
            self.assertEqual(counts["unsuccessful"], 1)
            with store.connect() as connection:
                connection.execute(
                    "UPDATE jobs SET status = 'failed' WHERE id = ?", (job["id"],)
                )
            self.assertEqual(store.get_job(job["id"])["status"], "partial")

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

    def test_reported_artifact_makes_failed_run_partial_and_not_retryable(self):
        with TemporaryDirectory() as temporary:
            state = Path(temporary)
            problem = state / "OP-001"
            problem.mkdir()
            code = (
                "from pathlib import Path; import os; "
                f"p=Path({str(problem)!r})/'attempt-001'; p.mkdir(); "
                "f=p/'solver-result.json'; f.write_text('{}'); "
                "Path(os.environ['LOOSE_ENDS_ARTIFACT_LOG']).open('a').write(str(f.resolve())+'\\n'); "
                "raise SystemExit(2)"
            )
            store = WorkbenchStore(state / "workbench.sqlite3", state)
            plan = fake_plan(
                [sys.executable, "-u", "-c", code],
                resources=[f"paper:{state}"],
            )
            job = store.create_job({"action": "solve"}, plan)
            run = job["runs"][0]
            store.mark_starting(run["id"])
            workbench_worker.run_worker(store.database, run["id"])

            saved = store.get_run(run["id"])
            self.assertEqual(saved["status"], "partial")
            self.assertEqual(
                saved["outputs"],
                [str((problem / "attempt-001" / "solver-result.json").resolve())],
            )
            with self.assertRaisesRegex(ValueError, "installed output"):
                store.retry_run(run["id"])

    def test_worker_persists_artifacts_while_process_is_still_running(self):
        with TemporaryDirectory() as temporary:
            state = Path(temporary)
            first = state / "first.md"
            second = state / "second.md"
            code = (
                "from pathlib import Path; import os, time; "
                f"first=Path({str(first)!r}); second=Path({str(second)!r}); "
                "log=Path(os.environ['LOOSE_ENDS_ARTIFACT_LOG']); "
                "first.write_text('first'); log.open('a').write(str(first.resolve())+'\\n'); "
                "time.sleep(1.0); second.write_text('second'); "
                "log.open('a').write(str(second.resolve())+'\\n'); time.sleep(0.5)"
            )
            store = WorkbenchStore(state / "workbench.sqlite3", state)
            job = store.create_job(
                {"action": "solve"},
                fake_plan(
                    [sys.executable, "-u", "-c", code],
                    resources=[f"paper:{state}"],
                ),
            )
            run = job["runs"][0]
            store.mark_starting(run["id"])
            worker = threading.Thread(
                target=workbench_worker.run_worker,
                args=(store.database, run["id"]),
            )
            worker.start()
            deadline = time.time() + 4
            while not store.get_run(run["id"])["outputs"] and time.time() < deadline:
                time.sleep(0.05)
            during = store.get_run(run["id"])
            self.assertEqual(during["outputs"], [str(first.resolve())])
            self.assertEqual(during["status"], "running")
            worker.join(10)
            self.assertFalse(worker.is_alive())
            saved = store.get_run(run["id"])
            self.assertEqual(saved["outputs"], [str(first.resolve()), str(second.resolve())])
            self.assertEqual(saved["status"], "succeeded")

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

    def test_complete_artifact_lines_are_recovered_after_worker_interruption(self):
        with TemporaryDirectory() as temporary:
            state = Path(temporary)
            artifact = state / "result.md"
            artifact.write_text("result", encoding="utf-8")
            store = WorkbenchStore(state / "workbench.sqlite3", state)
            job = store.create_job(
                {"action": "solve"},
                fake_plan(
                    [sys.executable, "-c", "pass"],
                    resources=[f"paper:{state}"],
                ),
            )
            run = job["runs"][0]
            artifact_log = Path(run["log_path"]).parent / "artifacts.txt"
            artifact_log.write_text(
                f"{artifact.resolve()}\nignored-incomplete",
                encoding="utf-8",
            )

            workbench_worker.recover_run_artifacts(store, run)
            self.assertEqual(store.get_run(run["id"])["outputs"], [str(artifact.resolve())])

    def test_artifact_reporter_is_disabled_without_managed_environment(self):
        with TemporaryDirectory() as temporary:
            artifact = Path(temporary) / "result.md"
            artifact.write_text("result", encoding="utf-8")
            with patch.dict(os.environ, {}, clear=True):
                common.report_artifacts([artifact])

    def test_artifact_reporter_appends_absolute_paths(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            log = root / "artifacts.txt"
            first = root / "first.md"
            second = root / "second.json"
            with patch.dict(os.environ, {common.ARTIFACT_LOG_ENV: str(log)}):
                common.report_artifacts([first, second])
                common.report_artifacts([first])
            self.assertEqual(
                log.read_text(encoding="utf-8").splitlines(),
                [str(first.resolve()), str(second.resolve()), str(first.resolve())],
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

    def test_watchdog_ignores_files_outside_the_catalog_model(self):
        catalog = Mock()
        handler = ChangeHandler(catalog)

        handler.on_any_event(
            SimpleNamespace(
                event_type="modified",
                is_directory=False,
                src_path=str(PROJECT_ROOT / "papers" / "agent-run.log"),
            )
        )

        catalog.schedule.assert_not_called()

    def test_watchdog_ignores_duplicate_file_notifications(self):
        catalog = Mock()
        handler = ChangeHandler(catalog)
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "metadata.json"
            path.write_text("{}", encoding="utf-8")
            event = SimpleNamespace(
                event_type="modified",
                is_directory=False,
                src_path=str(path),
            )
            handler.on_any_event(event)
            handler.on_any_event(event)

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
        store.scheduler_settings.return_value = {
            "workerLimit": 1,
            "queuePaused": False,
        }
        store.active_resources.return_value = set()
        store.claim_next_run.return_value = None
        scheduler = workbench.Scheduler(
            store,
            Mock(),
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
                version = manager.version
                manager.refresh()
                self.assertFalse(manager.snapshot()["loading"])
                self.assertEqual(manager.version, version)
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
