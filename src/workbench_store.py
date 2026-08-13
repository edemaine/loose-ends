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

    @staticmethod
    def _decode(row: sqlite3.Row, fields: Iterable[str]) -> dict:
        value = dict(row)
        for field in fields:
            value[field.removesuffix("_json")] = json.loads(value.pop(field))
        if "cancel_requested" in value:
            value["cancel_requested"] = bool(value["cancel_requested"])
        return value

    def create_job(self, request: dict, plan: dict) -> dict:
        now = time.time()
        job_id = str(uuid.uuid4())
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO jobs (
                    id, action, title, status, request_json, plan_json,
                    created_at, updated_at
                ) VALUES (?, ?, ?, 'queued', ?, ?, ?, ?)
                """,
                (
                    job_id,
                    plan["action"],
                    plan["title"],
                    json.dumps(request, ensure_ascii=False),
                    json.dumps(plan, ensure_ascii=False),
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
                    SELECT job_id,
                        SUM(status = 'queued') AS queued,
                        SUM(status IN ('starting', 'running', 'cancel_requested')) AS active,
                        SUM(status = 'succeeded') AS succeeded,
                        SUM(status IN ('partial', 'failed', 'canceled', 'interrupted')) AS unsuccessful,
                        COUNT(*) AS total
                    FROM runs GROUP BY job_id
                    """
                )
            }
        jobs = []
        for row in rows:
            job = self._decode(row, ("request_json", "plan_json"))
            job["counts"] = counts.get(job["id"], {})
            jobs.append(job)
        return jobs

    def queued_runs(self, *, limit: int = 100) -> list[dict]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM runs WHERE status = 'queued'
                ORDER BY created_at, unit_index LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [
            self._decode(
                row,
                (
                    "argv_json",
                    "targets_json",
                    "resources_json",
                    "probe_json",
                    "outputs_json",
                ),
            )
            for row in rows
        ]

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
                f"SELECT resources_json FROM runs WHERE status IN ({placeholders})",
                tuple(ACTIVE_STATUSES),
            ).fetchall()
        resources: set[str] = set()
        for row in rows:
            resources.update(json.loads(row["resources_json"]))
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
            latest: dict[int, sqlite3.Row] = {}
            for row in rows:
                latest[row["unit_index"]] = row
            statuses = [row["status"] for row in latest.values()]
            if not statuses:
                status = "failed"
            elif any(value in ACTIVE_STATUSES for value in statuses):
                status = "running"
            elif any(value == "queued" for value in statuses):
                status = "queued"
            elif all(value == "succeeded" for value in statuses):
                status = "succeeded"
            elif any(value == "partial" for value in statuses):
                status = "partial"
            elif any(value == "failed" for value in statuses):
                status = "failed"
            elif any(value == "interrupted" for value in statuses):
                status = "interrupted"
            else:
                status = "canceled"
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
        return max(float(job), float(run))
