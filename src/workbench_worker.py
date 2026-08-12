#!/usr/bin/env python3
"""Detached worker that owns one persisted workbench CLI run."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import time

import codex_cli
from workbench_store import WorkbenchStore
from workbench_tasks import probe_outputs


HEARTBEAT_SECONDS = 2.0
CANCEL_GRACE_SECONDS = 8.0


def _stop_process_tree(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        try:
            process.send_signal(signal.CTRL_BREAK_EVENT)
        except (OSError, ValueError):
            process.terminate()
    else:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
    deadline = time.monotonic() + CANCEL_GRACE_SECONDS
    while process.poll() is None and time.monotonic() < deadline:
        time.sleep(0.2)
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            **codex_cli.windowless_popen_options(new_process_group=False),
        )
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def run_worker(database: Path, run_id: str) -> int:
    store = WorkbenchStore(database)
    run = store.get_run(run_id)
    if run["status"] not in {"starting", "queued"}:
        return 0
    log_path = Path(run["log_path"])
    log_path.parent.mkdir(parents=True, exist_ok=True)
    now = time.time()
    store.update_run(
        run_id,
        status="running",
        started_at=now,
        heartbeat_at=now,
        worker_pid=os.getpid(),
    )
    environment = os.environ.copy()
    environment["PYTHONUNBUFFERED"] = "1"
    popen_options = codex_cli.windowless_popen_options()

    process: subprocess.Popen | None = None
    canceled = False
    error: str | None = None
    exit_code: int | None = None
    try:
        with log_path.open("a", encoding="utf-8", buffering=1) as log:
            log.write(f"Workbench run {run_id}\n")
            log.write(f"Working directory: {run['cwd']}\n")
            log.write(f"Command: {run['argv']}\n\n")
            process = subprocess.Popen(
                run["argv"],
                cwd=run["cwd"],
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                text=True,
                **popen_options,
            )
            next_heartbeat = time.monotonic()
            while process.poll() is None:
                current = store.get_run(run_id)
                if current["cancel_requested"]:
                    canceled = True
                    log.write("\nCancellation requested; stopping process tree.\n")
                    _stop_process_tree(process)
                    break
                if time.monotonic() >= next_heartbeat:
                    store.update_run(
                        run_id,
                        heartbeat_at=time.time(),
                        worker_pid=os.getpid(),
                    )
                    next_heartbeat = time.monotonic() + HEARTBEAT_SECONDS
                time.sleep(0.25)
            exit_code = process.wait()
            log.write(f"\nProcess exited with code {exit_code}.\n")
    except BaseException as exc:
        error = f"{type(exc).__name__}: {exc}"
        if process is not None:
            _stop_process_tree(process)

    outputs = probe_outputs(run["probe"])
    if canceled:
        status = "partial" if outputs else "canceled"
        if outputs:
            error = "canceled after installing output"
    elif error is not None:
        status = "partial" if outputs else "failed"
    elif exit_code == 0:
        status = "succeeded"
    else:
        status = "partial" if outputs else "failed"
        error = f"command exited with code {exit_code}"
    finished = time.time()
    store.update_run(
        run_id,
        status=status,
        finished_at=finished,
        heartbeat_at=finished,
        exit_code=exit_code,
        error=error,
        outputs_json=outputs,
    )
    outcome_path = log_path.parent / "outcome.json"
    outcome_path.write_text(
        json.dumps(
            {
                "run_id": run_id,
                "status": status,
                "exit_code": exit_code,
                "error": error,
                "outputs": outputs,
                "finished_at": finished,
            },
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    return 0 if status == "succeeded" else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="run one workbench task unit")
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--run", dest="run_id", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    codex_cli.configure_utf8_stdio()
    args = build_parser().parse_args(argv)
    return run_worker(args.database.expanduser().resolve(), args.run_id)


if __name__ == "__main__":
    raise SystemExit(main())
