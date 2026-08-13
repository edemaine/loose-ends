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

import artifact_reporting
import codex_cli
from workbench_store import WorkbenchStore


HEARTBEAT_SECONDS = 2.0
CANCEL_GRACE_SECONDS = 8.0


def _artifact_roots(run: dict) -> list[Path]:
    roots = []
    for resource in run.get("resources", []):
        kind, separator, value = resource.partition(":")
        if separator and kind in {"paper", "manuscript"} and value:
            roots.append(Path(value).resolve())
    return roots


def _under_roots(path: Path, roots: list[Path]) -> bool:
    for root in roots:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def _consume_artifact_log(
    store: WorkbenchStore,
    run: dict,
    artifact_log: Path,
    offset: int,
    remainder: bytes,
    console,
) -> tuple[int, bytes]:
    try:
        with artifact_log.open("rb") as source:
            source.seek(offset)
            chunk = source.read()
    except FileNotFoundError:
        return offset, remainder
    if not chunk:
        return offset, remainder
    offset += len(chunk)
    pieces = (remainder + chunk).split(b"\n")
    remainder = pieces.pop()
    roots = _artifact_roots(run)
    for raw in pieces:
        try:
            value = raw.removesuffix(b"\r").decode("utf-8")
        except UnicodeDecodeError:
            console.write("\nIgnored a non-UTF-8 artifact path.\n")
            continue
        if not value:
            continue
        path = Path(value).resolve()
        if not path.is_file() or not _under_roots(path, roots):
            console.write(f"\nIgnored unavailable or out-of-scope artifact: {value}\n")
            continue
        if store.append_run_output(run["id"], path):
            console.write(f"\nInstalled artifact: {path}\n")
    return offset, remainder


def recover_run_artifacts(store: WorkbenchStore, run: dict) -> None:
    """Recover complete artifact lines that preceded a worker interruption."""
    console_path = Path(run["log_path"])
    artifact_log = console_path.parent / "artifacts.txt"
    console_path.parent.mkdir(parents=True, exist_ok=True)
    with console_path.open("a", encoding="utf-8", buffering=1) as console:
        _consume_artifact_log(store, run, artifact_log, 0, b"", console)


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
    artifact_log = log_path.parent / "artifacts.txt"
    artifact_log.touch(exist_ok=True)
    environment[artifact_reporting.ARTIFACT_LOG_ENV] = str(artifact_log)
    popen_options = codex_cli.windowless_popen_options()

    process: subprocess.Popen | None = None
    canceled = False
    error: str | None = None
    exit_code: int | None = None
    artifact_offset = 0
    artifact_remainder = b""
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
                artifact_offset, artifact_remainder = _consume_artifact_log(
                    store,
                    run,
                    artifact_log,
                    artifact_offset,
                    artifact_remainder,
                    log,
                )
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
            artifact_offset, artifact_remainder = _consume_artifact_log(
                store,
                run,
                artifact_log,
                artifact_offset,
                artifact_remainder,
                log,
            )
            if artifact_remainder:
                log.write("\nIgnored an incomplete artifact-log line.\n")
            log.write(f"\nProcess exited with code {exit_code}.\n")
    except BaseException as exc:
        error = f"{type(exc).__name__}: {exc}"
        if process is not None:
            _stop_process_tree(process)

    outputs = store.get_run(run_id)["outputs"]
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
