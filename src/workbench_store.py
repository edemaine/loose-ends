#!/usr/bin/env python3
"""Persistent job and run records for the local research workbench."""

from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import time
import uuid
from typing import Iterable


ACTIVE_STATUSES = {"starting", "running", "cancel_requested"}
TERMINAL_STATUSES = {
    "succeeded",
    "partial",
    "failed",
    "canceled",
    "interrupted",
}
MIN_PRIORITY_LEVEL = -3
MAX_PRIORITY_LEVEL = 3
DEFAULT_WORKER_LIMIT = 2
MAX_WORKER_LIMIT = 64


def _resource_identity(resource: str) -> tuple[str, str]:
    """Return a comparable resource kind and path across slash conventions."""
    kind, separator, value = resource.partition(":")
    if not separator:
        return "", resource
    windows_path = "\\" in value or (
        len(value) >= 2 and value[0].isalpha() and value[1] == ":"
    )
    path = value.replace("\\", "/").rstrip("/")
    if windows_path:
        path = path.casefold()
    return kind, path


def _resources_conflict(left: Iterable[str], right: Iterable[str]) -> bool:
    """Whether two sets overlap, including paper/child-problem locks."""
    left_identities = [_resource_identity(resource) for resource in left]
    right_identities = [_resource_identity(resource) for resource in right]
    for left_kind, left_path in left_identities:
        for right_kind, right_path in right_identities:
            if left_kind == right_kind and left_path == right_path:
                return True
            if left_kind == "paper" and right_kind == "problem":
                if right_path.rpartition("/")[0] == left_path:
                    return True
            elif left_kind == "problem" and right_kind == "paper":
                if left_path.rpartition("/")[0] == right_path:
                    return True
    return False


def _run_resources(row: sqlite3.Row) -> set[str]:
    """Get precise locks, refining paper-wide locks saved by older versions."""
    stored = set(json.loads(row["resources_json"]))
    targets = json.loads(row["targets_json"])
    if not isinstance(targets, list):
        return stored

    derived: set[str] = set()
    recognized = False
    for target in targets:
        if not isinstance(target, dict) or not isinstance(target.get("path"), str):
            continue
        path = target["path"].replace("\\", "/").rstrip("/")
        if target.get("kind") == "paper":
            derived.add(f"paper:{path}")
            recognized = True
        elif target.get("kind") == "problem":
            derived.add(f"problem:{path}")
            recognized = True
        elif target.get("kind") == "attempt":
            parent, separator, _name = path.rpartition("/")
            if separator:
                derived.add(f"problem:{parent}")
                recognized = True
    if not recognized:
        return stored
    return {
        resource for resource in stored if not resource.startswith("paper:")
    } | derived


class WorkbenchStore:
    """Small SQLite-backed store safe to share with detached workers."""

    def __init__(self, database: Path, state_directory: Path | None = None):
        self.database = database.resolve()
        self.state_directory = (
            state_directory.resolve()
            if state_directory is not None
            else self.database.parent
        )
        self.state_directory.mkdir(parents=True, exist_ok=True)
        (self.state_directory / "runs").mkdir(exist_ok=True)
        self.initialize()

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.database, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        with self.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    action TEXT NOT NULL,
                    title TEXT NOT NULL,
                    status TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    plan_json TEXT NOT NULL,
                    priority_level INTEGER NOT NULL DEFAULT 0,
                    scheduling_paused INTEGER NOT NULL DEFAULT 0,
                    scheduler_credit REAL NOT NULL DEFAULT 0,
                    last_dispatched_at REAL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    finished_at REAL
                );

                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                    unit_index INTEGER NOT NULL,
                    label TEXT NOT NULL,
                    status TEXT NOT NULL,
                    argv_json TEXT NOT NULL,
                    cwd TEXT NOT NULL,
                    targets_json TEXT NOT NULL,
                    resources_json TEXT NOT NULL DEFAULT '[]',
                    probe_json TEXT NOT NULL,
                    log_path TEXT NOT NULL,
                    outputs_json TEXT NOT NULL DEFAULT '[]',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    started_at REAL,
                    finished_at REAL,
                    heartbeat_at REAL,
                    worker_pid INTEGER,
                    exit_code INTEGER,
                    error TEXT,
                    retry_of TEXT REFERENCES runs(id),
                    cancel_requested INTEGER NOT NULL DEFAULT 0
                );

                CREATE INDEX IF NOT EXISTS runs_job_index
                    ON runs(job_id, unit_index, created_at);
                CREATE INDEX IF NOT EXISTS runs_status_index
                    ON runs(status, created_at);

                CREATE TABLE IF NOT EXISTS scheduler_settings (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    worker_limit INTEGER NOT NULL,
                    queue_paused INTEGER NOT NULL DEFAULT 0,
                    updated_at REAL NOT NULL
                );
                """
            )
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(runs)")
            }
            if "resources_json" not in columns:
                connection.execute(
                    "ALTER TABLE runs ADD COLUMN resources_json TEXT NOT NULL DEFAULT '[]'"
                )
            job_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(jobs)")
            }
            job_migrations = {
                "priority_level": "INTEGER NOT NULL DEFAULT 0",
                "scheduling_paused": "INTEGER NOT NULL DEFAULT 0",
                "scheduler_credit": "REAL NOT NULL DEFAULT 0",
                "last_dispatched_at": "REAL",
            }
            for name, declaration in job_migrations.items():
                if name not in job_columns:
                    connection.execute(
                        f"ALTER TABLE jobs ADD COLUMN {name} {declaration}"
                    )
            connection.execute(
                """
                INSERT OR IGNORE INTO scheduler_settings (
                    id, worker_limit, queue_paused, updated_at
                ) VALUES (1, ?, 0, ?)
                """,
                (DEFAULT_WORKER_LIMIT, time.time()),
            )

    @staticmethod
    def _decode(row: sqlite3.Row, fields: Iterable[str]) -> dict:
        value = dict(row)
        for field in fields:
            value[field.removesuffix("_json")] = json.loads(value.pop(field))
        if "cancel_requested" in value:
            value["cancel_requested"] = bool(value["cancel_requested"])
        if "scheduling_paused" in value:
            value["scheduling_paused"] = bool(value["scheduling_paused"])
        return value

    @staticmethod
    def _job_status(run_rows: Iterable[sqlite3.Row]) -> str:
        latest: dict[int, sqlite3.Row] = {}
        for row in run_rows:
            latest[row["unit_index"]] = row
        statuses = [row["status"] for row in latest.values()]
        if not statuses:
            return "failed"
        if any(value in ACTIVE_STATUSES for value in statuses):
            return "running"
        if any(value == "queued" for value in statuses):
            return "queued"
        if all(value == "succeeded" for value in statuses):
            return "succeeded"
        if any(value in {"succeeded", "partial"} for value in statuses):
            return "partial"
        if any(value == "failed" for value in statuses):
            return "failed"
        if any(value == "interrupted" for value in statuses):
            return "interrupted"
        return "canceled"

    def create_job(self, request: dict, plan: dict) -> dict:
        now = time.time()
        job_id = str(uuid.uuid4())
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO jobs (
                    id, action, title, status, request_json, plan_json,
                    priority_level, created_at, updated_at
                ) VALUES (?, ?, ?, 'queued', ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    plan["action"],
                    plan["title"],
                    json.dumps(request, ensure_ascii=False),
                    json.dumps(plan, ensure_ascii=False),
                    plan.get("priorityLevel", 0),
                    now,
                    now,
                ),
            )
            for index, unit in enumerate(plan["units"]):
                run_id = str(uuid.uuid4())
                run_directory = self.state_directory / "runs" / run_id
                run_directory.mkdir(parents=True)
                log_path = run_directory / "console.log"
                (run_directory / "request.json").write_text(
                    json.dumps(request, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8",
                )
                (run_directory / "command.json").write_text(
                    json.dumps(unit, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8",
                )
                connection.execute(
                    """
                    INSERT INTO runs (
                        id, job_id, unit_index, label, status, argv_json, cwd,
                        targets_json, resources_json, probe_json, log_path,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        job_id,
                        index,
                        unit["label"],
                        json.dumps(unit["argv"], ensure_ascii=False),
                        unit["cwd"],
                        json.dumps(unit.get("targets", []), ensure_ascii=False),
                        json.dumps(unit.get("resources", []), ensure_ascii=False),
                        json.dumps(unit.get("probe", {}), ensure_ascii=False),
                        str(log_path),
                        now,
                        now,
                    ),
                )
        return self.get_job(job_id)

    def get_run(self, run_id: str) -> dict:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM runs WHERE id = ?", (run_id,)
            ).fetchone()
        if row is None:
            raise KeyError(run_id)
        return self._decode(
            row,
            (
                "argv_json",
                "targets_json",
                "resources_json",
                "probe_json",
                "outputs_json",
            ),
        )

    def get_job(self, job_id: str) -> dict:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            run_rows = connection.execute(
                "SELECT * FROM runs WHERE job_id = ? ORDER BY unit_index, created_at",
                (job_id,),
            ).fetchall()
        if row is None:
            raise KeyError(job_id)
        job = self._decode(row, ("request_json", "plan_json"))
        job["runs"] = [
            self._decode(
                item,
                (
                    "argv_json",
                    "targets_json",
                    "resources_json",
                    "probe_json",
                    "outputs_json",
                ),
            )
            for item in run_rows
        ]
        job["status"] = self._job_status(run_rows)
        return job

    def list_jobs(self, *, limit: int = 1000) -> list[dict]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
            counts = {
                row["job_id"]: dict(row)
                for row in connection.execute(
                    """
                    WITH latest AS (
                        SELECT job_id, status,
                            ROW_NUMBER() OVER (
                                PARTITION BY job_id, unit_index
                                ORDER BY created_at DESC
                            ) AS position
                        FROM runs
                    )
                    SELECT job_id,
                        SUM(status = 'queued') AS queued,
                        SUM(status IN ('starting', 'running', 'cancel_requested')) AS active,
                        SUM(status = 'succeeded') AS succeeded,
                        SUM(status = 'partial') AS partial,
                        SUM(status = 'failed') AS failed,
                        SUM(status = 'canceled') AS canceled,
                        SUM(status = 'interrupted') AS interrupted,
                        SUM(status IN ('partial', 'failed', 'canceled', 'interrupted')) AS unsuccessful,
                        COUNT(*) AS total
                    FROM latest WHERE position = 1 GROUP BY job_id
                    """
                )
            }
            live_runs: dict[str, list[dict]] = {}
            for run_row in connection.execute(
                """
                WITH latest AS (
                    SELECT id, job_id, unit_index, label, status, targets_json,
                        created_at, started_at,
                        ROW_NUMBER() OVER (
                            PARTITION BY job_id, unit_index
                            ORDER BY created_at DESC
                        ) AS position
                    FROM runs
                )
                SELECT id, job_id, unit_index, label, status, targets_json,
                    created_at, started_at
                FROM latest
                WHERE position = 1
                    AND status IN (
                        'queued', 'starting', 'running', 'cancel_requested'
                    )
                ORDER BY job_id, unit_index, created_at
                """
            ):
                run = dict(run_row)
                run["targets"] = json.loads(run.pop("targets_json"))
                live_runs.setdefault(run["job_id"], []).append(run)
        jobs = []
        for row in rows:
            job = self._decode(row, ("request_json", "plan_json"))
            job["counts"] = counts.get(job["id"], {})
            job["liveRuns"] = live_runs.get(job["id"], [])
            jobs.append(job)
        return jobs

    def scheduler_settings(self) -> dict:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM scheduler_settings WHERE id = 1"
            ).fetchone()
        return {
            "workerLimit": int(row["worker_limit"]),
            "queuePaused": bool(row["queue_paused"]),
            "updatedAt": float(row["updated_at"]),
        }

    def update_scheduler_settings(
        self,
        *,
        worker_limit: object | None = None,
        queue_paused: object | None = None,
    ) -> dict:
        assignments: list[str] = []
        parameters: list[object] = []
        if worker_limit is not None:
            if isinstance(worker_limit, bool):
                raise ValueError("worker limit must be an integer")
            try:
                limit = int(worker_limit)
            except (TypeError, ValueError) as exc:
                raise ValueError("worker limit must be an integer") from exc
            if not 1 <= limit <= MAX_WORKER_LIMIT:
                raise ValueError(
                    f"worker limit must be between 1 and {MAX_WORKER_LIMIT}"
                )
            assignments.append("worker_limit = ?")
            parameters.append(limit)
        if queue_paused is not None:
            if not isinstance(queue_paused, bool):
                raise ValueError("queuePaused must be true or false")
            assignments.append("queue_paused = ?")
            parameters.append(int(queue_paused))
        if not assignments:
            raise ValueError("no scheduler setting was supplied")
        assignments.append("updated_at = ?")
        parameters.append(time.time())
        with self.connect() as connection:
            connection.execute(
                f"UPDATE scheduler_settings SET {', '.join(assignments)} WHERE id = 1",
                parameters,
            )
        return self.scheduler_settings()

    def update_job_scheduling(
        self,
        job_id: str,
        *,
        priority_level: object | None = None,
        paused: object | None = None,
    ) -> dict:
        assignments: list[str] = []
        parameters: list[object] = []
        if priority_level is not None:
            if isinstance(priority_level, bool):
                raise ValueError("priority level must be an integer")
            try:
                level = int(priority_level)
            except (TypeError, ValueError) as exc:
                raise ValueError("priority level must be an integer") from exc
            if not MIN_PRIORITY_LEVEL <= level <= MAX_PRIORITY_LEVEL:
                raise ValueError(
                    f"priority level must be between {MIN_PRIORITY_LEVEL} and "
                    f"{MAX_PRIORITY_LEVEL}"
                )
            assignments.extend(("priority_level = ?", "scheduler_credit = 0"))
            parameters.append(level)
        if paused is not None:
            if not isinstance(paused, bool):
                raise ValueError("paused must be true or false")
            assignments.append("scheduling_paused = ?")
            parameters.append(int(paused))
        if not assignments:
            raise ValueError("no task scheduling change was supplied")
        now = time.time()
        assignments.append("updated_at = ?")
        parameters.extend((now, job_id))
        with self.connect() as connection:
            row = connection.execute(
                "SELECT status FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if row is None:
                raise KeyError(job_id)
            if paused is not None and row["status"] in TERMINAL_STATUSES:
                raise ValueError("finished tasks cannot be paused or resumed")
            connection.execute(
                f"UPDATE jobs SET {', '.join(assignments)} WHERE id = ?",
                parameters,
            )
        return self.get_job(job_id)

    def claim_next_run(self, active_resources: set[str]) -> dict | None:
        """Claim one resource-compatible run using smooth weighted fairness."""
        now = time.time()
        selected_row: sqlite3.Row | None = None
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT runs.*, jobs.priority_level, jobs.scheduler_credit,
                    jobs.last_dispatched_at,
                    jobs.created_at AS job_created_at
                FROM runs JOIN jobs ON jobs.id = runs.job_id
                WHERE runs.status = 'queued' AND jobs.scheduling_paused = 0
                ORDER BY jobs.created_at, runs.unit_index, runs.created_at
                """
            ).fetchall()
            active_counts = {
                row["job_id"]: int(row["active_count"])
                for row in connection.execute(
                    """
                    SELECT job_id, COUNT(*) AS active_count FROM runs
                    WHERE status IN ('starting', 'running', 'cancel_requested')
                    GROUP BY job_id
                    """
                )
            }
            eligible: dict[str, sqlite3.Row] = {}
            for row in rows:
                if row["job_id"] in eligible:
                    continue
                resources = _run_resources(row)
                if _resources_conflict(active_resources, resources):
                    continue
                eligible[row["job_id"]] = row
            if not eligible:
                return None

            candidates: list[tuple[sqlite3.Row, float, float]] = []
            total_weight = 0.0
            for row in eligible.values():
                weight = 2.0 ** int(row["priority_level"])
                credit = float(row["scheduler_credit"]) + weight
                total_weight += weight
                candidates.append((row, weight, credit))
            selected_row, _selected_weight, selected_credit = max(
                candidates,
                key=lambda candidate: (
                    candidate[2],
                    -active_counts.get(candidate[0]["job_id"], 0) / candidate[1],
                    -float(candidate[0]["last_dispatched_at"] or 0),
                    -float(candidate[0]["job_created_at"]),
                ),
            )
            for row, _weight, credit in candidates:
                if row["job_id"] == selected_row["job_id"]:
                    credit = selected_credit - total_weight
                    connection.execute(
                        """
                        UPDATE jobs SET scheduler_credit = ?, last_dispatched_at = ?
                        WHERE id = ?
                        """,
                        (credit, now, row["job_id"]),
                    )
                else:
                    connection.execute(
                        "UPDATE jobs SET scheduler_credit = ? WHERE id = ?",
                        (credit, row["job_id"]),
                    )
            cursor = connection.execute(
                """
                UPDATE runs SET status = 'starting', updated_at = ?
                WHERE id = ? AND status = 'queued'
                """,
                (now, selected_row["id"]),
            )
            if not cursor.rowcount:
                return None
        self.reconcile_job(selected_row["job_id"])
        return self.get_run(selected_row["id"])

    def active_count(self) -> int:
        placeholders = ",".join("?" for _ in ACTIVE_STATUSES)
        with self.connect() as connection:
            row = connection.execute(
                f"SELECT COUNT(*) FROM runs WHERE status IN ({placeholders})",
                tuple(ACTIVE_STATUSES),
            ).fetchone()
        return int(row[0])

    def active_resources(self) -> set[str]:
        placeholders = ",".join("?" for _ in ACTIVE_STATUSES)
        with self.connect() as connection:
            rows = connection.execute(
                f"""SELECT resources_json, targets_json FROM runs
                WHERE status IN ({placeholders})""",
                tuple(ACTIVE_STATUSES),
            ).fetchall()
        resources: set[str] = set()
        for row in rows:
            resources.update(_run_resources(row))
        return resources

    def mark_starting(self, run_id: str) -> bool:
        now = time.time()
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE runs SET status = 'starting', updated_at = ?
                WHERE id = ? AND status = 'queued'
                """,
                (now, run_id),
            )
        if cursor.rowcount:
            self.reconcile_job(self.get_run(run_id)["job_id"])
            return True
        return False

    def update_run(self, run_id: str, **values: object) -> None:
        allowed = {
            "status",
            "started_at",
            "finished_at",
            "heartbeat_at",
            "worker_pid",
            "exit_code",
            "error",
            "cancel_requested",
            "outputs_json",
        }
        unknown = set(values) - allowed
        if unknown:
            raise ValueError(f"unsupported run fields: {sorted(unknown)}")
        values["updated_at"] = time.time()
        assignments = ", ".join(f"{name} = ?" for name in values)
        parameters = [
            json.dumps(value, ensure_ascii=False)
            if name == "outputs_json"
            else value
            for name, value in values.items()
        ]
        parameters.append(run_id)
        with self.connect() as connection:
            connection.execute(
                f"UPDATE runs SET {assignments} WHERE id = ?", parameters
            )
        try:
            job_id = self.get_run(run_id)["job_id"]
        except KeyError:
            return
        self.reconcile_job(job_id)

    def append_run_output(self, run_id: str, path: Path | str) -> bool:
        """Persist one newly reported artifact, preserving first-report order."""
        value = str(Path(path).resolve())
        now = time.time()
        with self.connect() as connection:
            row = connection.execute(
                "SELECT job_id, outputs_json FROM runs WHERE id = ?", (run_id,)
            ).fetchone()
            if row is None:
                raise KeyError(run_id)
            outputs = json.loads(row["outputs_json"])
            if value in outputs:
                return False
            outputs.append(value)
            connection.execute(
                "UPDATE runs SET outputs_json = ?, updated_at = ? WHERE id = ?",
                (json.dumps(outputs, ensure_ascii=False), now, run_id),
            )
            job_id = row["job_id"]
        self.reconcile_job(job_id)
        return True

    def request_cancel(self, run_id: str) -> dict:
        run = self.get_run(run_id)
        if run["status"] == "queued":
            self.update_run(
                run_id,
                status="canceled",
                cancel_requested=1,
                finished_at=time.time(),
            )
        elif run["status"] in ACTIVE_STATUSES:
            self.update_run(
                run_id,
                status="cancel_requested",
                cancel_requested=1,
            )
        return self.get_run(run_id)

    def retry_run(self, run_id: str) -> dict:
        original = self.get_run(run_id)
        if original["outputs"]:
            raise ValueError(
                "this run installed output; use its suggested next action instead"
            )
        if original["status"] not in {
            "failed",
            "canceled",
            "interrupted",
        }:
            raise ValueError("only failed, canceled, or interrupted runs can retry")
        now = time.time()
        new_id = str(uuid.uuid4())
        run_directory = self.state_directory / "runs" / new_id
        run_directory.mkdir(parents=True)
        log_path = run_directory / "console.log"
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO runs (
                    id, job_id, unit_index, label, status, argv_json, cwd,
                    targets_json, resources_json, probe_json, log_path,
                    created_at, updated_at, retry_of
                ) VALUES (?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    new_id,
                    original["job_id"],
                    original["unit_index"],
                    original["label"],
                    json.dumps(original["argv"], ensure_ascii=False),
                    original["cwd"],
                    json.dumps(original["targets"], ensure_ascii=False),
                    json.dumps(original["resources"], ensure_ascii=False),
                    json.dumps(original["probe"], ensure_ascii=False),
                    str(log_path),
                    now,
                    now,
                    run_id,
                ),
            )
        self.reconcile_job(original["job_id"])
        return self.get_run(new_id)

    def reconcile_job(self, job_id: str) -> None:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM runs WHERE job_id = ?
                ORDER BY unit_index, created_at
                """,
                (job_id,),
            ).fetchall()
            status = self._job_status(rows)
            now = time.time()
            finished = now if status in TERMINAL_STATUSES else None
            connection.execute(
                """
                UPDATE jobs SET status = ?, updated_at = ?, finished_at = ?
                WHERE id = ?
                """,
                (status, now, finished, job_id),
            )

    def mark_stale_runs(self, *, older_than: float) -> list[str]:
        cutoff = time.time() - older_than
        placeholders = ",".join("?" for _ in ACTIVE_STATUSES)
        with self.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT id FROM runs
                WHERE status IN ({placeholders})
                  AND COALESCE(heartbeat_at, updated_at) < ?
                """,
                (*ACTIVE_STATUSES, cutoff),
            ).fetchall()
        changed = []
        for row in rows:
            self.update_run(
                row["id"],
                status="interrupted",
                finished_at=time.time(),
                error="worker heartbeat stopped; the run can be retried",
            )
            changed.append(row["id"])
        return changed

    def revision(self) -> float:
        with self.connect() as connection:
            job = connection.execute(
                "SELECT COALESCE(MAX(updated_at), 0) FROM jobs"
            ).fetchone()[0]
            run = connection.execute(
                "SELECT COALESCE(MAX(updated_at), 0) FROM runs"
            ).fetchone()[0]
            settings = connection.execute(
                "SELECT COALESCE(MAX(updated_at), 0) FROM scheduler_settings"
            ).fetchone()[0]
        return max(float(job), float(run), float(settings))
