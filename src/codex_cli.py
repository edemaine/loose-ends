#!/usr/bin/env python3
"""Shared non-interactive Codex CLI execution helpers."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path
import signal
import shutil
import subprocess
import sys
import threading
import time


REASONING_EFFORTS = ("low", "medium", "high", "xhigh", "max", "ultra")
WEB_SEARCH_MODES = ("disabled", "indexed", "live")
DEFAULT_MODEL = "gpt-5.6-sol"
DEFAULT_REASONING_EFFORT = "xhigh"
CODEX_LAUNCH_INTERVAL_SECONDS = 1.0
MAX_CODEX_START_ATTEMPTS = 3
CODEX_POLL_INTERVAL_SECONDS = 0.5
CODEX_STOP_GRACE_SECONDS = 10.0
_CODEX_LAUNCH_LOCK = threading.Lock()
_next_codex_launch_at = 0.0
_WINDOWS_RESERVED_DEVICE_NAMES = {
    "aux",
    "con",
    "nul",
    "prn",
    *(f"com{number}" for number in range(1, 10)),
    *(f"lpt{number}" for number in range(1, 10)),
}


class CodexError(RuntimeError):
    """A local Codex invocation or its workspace could not be used."""


@dataclass(frozen=True)
class ModelOptions:
    model: str | None = None
    reasoning_effort: str | None = None
    fast: bool = False


def configure_utf8_stdio() -> None:
    """Keep research titles and author names printable on Windows consoles."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="backslashreplace")


def report_error(parser: argparse.ArgumentParser, error: BaseException) -> int:
    """Report a post-parse failure without printing irrelevant CLI usage."""
    print(f"{parser.prog}: error: {error}", file=sys.stderr)
    return 1


def positive_integer(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return number


def positive_number(value: str) -> float:
    number = float(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be greater than 0")
    return number


def add_prompt_arguments(
    parser: argparse.ArgumentParser,
    *,
    default_template: Path,
    task: str,
    prefix: str = "",
) -> None:
    """Add consistent user-direction and low-level prompt-template flags."""
    option_prefix = f"{prefix}-" if prefix else ""
    destination_prefix = f"{prefix.replace('-', '_')}_" if prefix else ""
    parser.add_argument(
        f"--{option_prefix}prompt",
        dest=f"{destination_prefix}prompt",
        metavar="TEXT",
        help=f"additional instruction for the {task}",
    )
    parser.add_argument(
        f"--{option_prefix}prompt-template",
        dest=f"{destination_prefix}prompt_template",
        type=Path,
        default=default_template,
        metavar="FILE",
        help=(
            f"replace the complete low-level {task} prompt template "
            f"(default: {default_template})"
        ),
    )


def with_user_prompt(
    template: str,
    instruction: str | None,
    *,
    task: str,
    option_name: str = "--prompt",
) -> str:
    """Append an explicit user direction without replacing core safeguards."""
    if instruction is None:
        return template
    instruction = instruction.strip()
    if not instruction:
        raise CodexError(f"{option_name} must be nonempty")
    return (
        template.rstrip()
        + "\n\n# Additional user direction\n\n"
        + f"The user explicitly requested this direction for the {task}:\n\n"
        + f"<user_instruction>\n{instruction}\n</user_instruction>\n\n"
        + "Follow it throughout this run while preserving the task's output "
        + "contract, validation requirements, and mathematical accuracy.\n"
    )


def add_model_arguments(
    parser: argparse.ArgumentParser,
    *,
    prefix: str = "",
) -> None:
    """Add consistent model, reasoning, Fast mode, and executable options."""
    option_prefix = f"{prefix}-" if prefix else ""
    destination_prefix = f"{prefix.replace('-', '_')}_" if prefix else ""
    model_default = None if prefix else DEFAULT_MODEL
    reasoning_default = None if prefix else DEFAULT_REASONING_EFFORT
    model_default_help = (
        "inherit the primary run" if prefix else DEFAULT_MODEL
    )
    reasoning_default_help = (
        "inherit the primary run" if prefix else DEFAULT_REASONING_EFFORT
    )
    parser.add_argument(
        f"--{option_prefix}model",
        dest=f"{destination_prefix}model",
        default=model_default,
        metavar="MODEL",
        help=(
            "Codex model ID; for example, gpt-5.6-sol "
            f"(default: {model_default_help})"
        ),
    )
    parser.add_argument(
        f"--{option_prefix}reasoning-effort",
        dest=f"{destination_prefix}reasoning_effort",
        default=reasoning_default,
        choices=REASONING_EFFORTS,
        metavar="LEVEL",
        help=(
            "reasoning depth: low, medium, high, xhigh, max, or ultra; "
            f"extra-high is xhigh (default: {reasoning_default_help})"
        ),
    )
    parser.add_argument(
        f"--{option_prefix}fast",
        dest=f"{destination_prefix}fast",
        action="store_true",
        help=(
            "request Codex Fast mode for this run; this uses more credits "
            + (
                "(default: inherit the primary run)"
                if prefix
                else "(default: use the CLI configuration)"
            )
        ),
    )


def add_web_search_argument(
    parser: argparse.ArgumentParser,
    *,
    default: str,
    prefix: str = "",
) -> None:
    """Add a scoped Codex first-party web-search option."""
    if default not in WEB_SEARCH_MODES:
        raise ValueError(f"invalid default web-search mode: {default}")
    option_prefix = f"{prefix}-" if prefix else ""
    destination_prefix = f"{prefix.replace('-', '_')}_" if prefix else ""
    parser.add_argument(
        f"--{option_prefix}web-search",
        dest=f"{destination_prefix}web_search",
        choices=WEB_SEARCH_MODES,
        default=None if prefix else default,
        metavar="MODE",
        help=(
            "Codex first-party web search: disabled, indexed, or live "
            f"(default: {'inherit the primary run' if prefix else default}); "
            "this does not enable shell network access or MCP/plugin apps"
        ),
    )


def model_options_from_args(
    args: argparse.Namespace,
    *,
    prefix: str = "",
) -> ModelOptions:
    destination_prefix = f"{prefix.replace('-', '_')}_" if prefix else ""
    return ModelOptions(
        model=getattr(args, f"{destination_prefix}model"),
        reasoning_effort=getattr(
            args,
            f"{destination_prefix}reasoning_effort",
        ),
        fast=getattr(args, f"{destination_prefix}fast"),
    )


def semantic_config_digest(
    prompt: str,
    schema_text: str,
    options: ModelOptions,
    *,
    web_search: str = "disabled",
) -> str:
    payload = {
        "fast": options.fast,
        "model": options.model,
        "prompt": prompt,
        "reasoning_effort": options.reasoning_effort,
        "schema": schema_text,
    }
    # Preserve existing disabled-search digests for analysis and triage.
    if web_search != "disabled":
        payload["web_search"] = web_search
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def resolve_codex_executable(value: str) -> str:
    executable = shutil.which(value)
    if executable is None:
        raise CodexError(
            f"could not find the Codex CLI executable {value!r} on PATH"
        )
    return executable


def read_codex_version(codex: str) -> str:
    try:
        completed = subprocess.run(
            [codex, "--version"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise CodexError(f"could not run {codex!r}: {exc}") from exc
    if completed.returncode != 0:
        detail = completed.stderr.strip() or f"exit status {completed.returncode}"
        raise CodexError(f"could not query the Codex CLI version: {detail}")
    return completed.stdout.strip()


def is_windows_host() -> bool:
    return os.name == "nt" or sys.platform == "cygwin"


def codex_subprocess_environment() -> dict[str, str]:
    """Avoid Windows Store command aliases inaccessible to sandbox users."""
    environment = os.environ.copy()
    if not is_windows_host():
        return environment
    path = environment.get("PATH")
    if path is None:
        return environment
    entries = path.split(os.pathsep)
    environment["PATH"] = os.pathsep.join(
        entry
        for entry in entries
        if not entry.rstrip("\\/").replace("\\", "/").casefold().endswith(
            "/microsoft/windowsapps"
        )
    )
    return environment


def _run_local_command(command: list[str], description: str) -> str:
    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        stdout, stderr = process.communicate()
    except OSError as exc:
        raise CodexError(f"{description}: {exc}") from exc
    if process.returncode != 0:
        detail = stderr.strip() or stdout.strip() or (
            f"exit status {process.returncode}"
        )
        raise CodexError(f"{description}: {detail}")
    return stdout


def path_for_codex(path: Path) -> str:
    """Return a path the Windows-native Codex CLI can understand."""
    resolved = path.resolve()
    if sys.platform != "cygwin":
        return str(resolved)
    try:
        process = subprocess.Popen(
            ["cygpath", "-w", str(resolved)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        stdout, stderr = process.communicate()
    except OSError as exc:
        raise CodexError(f"could not run cygpath for {resolved}: {exc}") from exc
    if process.returncode != 0:
        detail = stderr.strip() or f"exit status {process.returncode}"
        raise CodexError(f"could not convert Cygwin path {resolved}: {detail}")
    converted = stdout.strip()
    if not converted:
        raise CodexError(f"cygpath returned an empty path for {resolved}")
    return converted


@lru_cache(maxsize=1)
def windows_identity() -> str:
    domain = os.environ.get("USERDOMAIN", "").strip()
    username = os.environ.get("USERNAME", "").strip()
    if domain and username:
        return f"{domain}\\{username}"
    executable = shutil.which("whoami.exe")
    if executable is None:
        raise CodexError("could not find whoami.exe for Windows ACL setup")
    identity = _run_local_command(
        [executable],
        "could not determine the current Windows identity",
    ).strip()
    if not identity or "\\" not in identity:
        raise CodexError(
            f"whoami.exe returned an unexpected identity: {identity!r}"
        )
    return identity


@lru_cache(maxsize=1)
def windows_icacls() -> str:
    executable = shutil.which("icacls.exe") or shutil.which("icacls")
    if executable is None:
        raise CodexError("could not find icacls.exe for Windows ACL setup")
    return executable


def windows_icacls_for_sandbox() -> str:
    executable = windows_icacls()
    if sys.platform == "cygwin":
        return path_for_codex(Path(executable))
    return executable


def grant_workspace_owner_inheritance(workspace: Path) -> None:
    """Make future sandbox-created children readable by the invoking user."""
    if not is_windows_host():
        return
    identity = windows_identity()
    _run_local_command(
        [
            windows_icacls(),
            path_for_codex(workspace),
            "/grant",
            f"{identity}:(OI)(CI)(F)",
        ],
        f"could not grant {identity} access to {workspace}",
    )


def grant_sandbox_read_access(path: Path) -> None:
    """Let the restricted Windows Codex sandbox read staged local inputs."""
    if not is_windows_host():
        return
    domain = windows_identity().split("\\", 1)[0]
    sandbox_group = f"{domain}\\CodexSandboxUsers"
    staged_path = path_for_codex(path)
    _run_local_command(
        [
            windows_icacls(),
            staged_path,
            "/remove:d",
            sandbox_group,
            "/T",
            "/C",
        ],
        f"could not remove inherited sandbox read denials from {path}",
    )
    _run_local_command(
        [
            windows_icacls(),
            staged_path,
            "/grant",
            f"{sandbox_group}:(OI)(CI)(RX)",
            "/T",
            "/C",
        ],
        f"could not grant the Codex sandbox read access to {path}",
    )


def _is_windows_reserved_device_name(name: str) -> bool:
    """Recognize names that Win32 resolves as devices instead of files."""
    basename = name.rstrip(" .").split(".", 1)[0].rstrip(" ").casefold()
    return basename in _WINDOWS_RESERVED_DEVICE_NAMES


def workspace_is_user_accessible(workspace: Path) -> bool:
    """Return whether the invoking user can traverse and read a workspace."""
    try:
        def raise_walk_error(error: OSError) -> None:
            raise error

        windows_host = is_windows_host()
        for root, directories, filenames in os.walk(
            workspace,
            onerror=raise_walk_error,
        ):
            if windows_host:
                # Cygwin can create names such as NUL via shell redirection,
                # but Win32 cannot open them and no pipeline can consume them.
                directories[:] = [
                    name
                    for name in directories
                    if not _is_windows_reserved_device_name(name)
                ]
                filenames = [
                    name
                    for name in filenames
                    if not _is_windows_reserved_device_name(name)
                ]
            root_path = Path(root)
            for name in directories:
                (root_path / name).stat()
            for name in filenames:
                path = root_path / name
                path.stat()
                with path.open("rb") as source:
                    source.read(1)
    except OSError:
        return False
    return True


def normalize_workspace_access(workspace: Path, codex: str) -> None:
    """Remove sandbox-created deny ACLs, then grant recursive user access."""
    if not is_windows_host() or workspace_is_user_accessible(workspace):
        return
    identity = windows_identity()
    command_prefix = [
        codex,
        "sandbox",
        "-P",
        ":workspace",
        "-C",
        path_for_codex(workspace),
        windows_icacls_for_sandbox(),
        ".",
    ]
    for action in (
        ["/remove:d", identity, "/T", "/C"],
        ["/grant", f"{identity}:(OI)(CI)(F)", "/T", "/C"],
    ):
        _run_local_command(
            [*command_prefix, *action],
            f"could not normalize sandbox-owned files in {workspace}",
        )
    if not workspace_is_user_accessible(workspace):
        raise CodexError(
            f"Windows ACL repair did not make {workspace} accessible"
        )


def wait_for_codex_launch_slot(interval: float) -> None:
    """Space process startups while allowing already-started runs to overlap."""
    if interval <= 0:
        return
    global _next_codex_launch_at
    with _CODEX_LAUNCH_LOCK:
        now = time.monotonic()
        delay = max(0.0, _next_codex_launch_at - now)
        if delay:
            time.sleep(delay)
        _next_codex_launch_at = time.monotonic() + interval


def is_transient_startup_failure(
    completed: subprocess.CompletedProcess,
    events_path: Path,
    log_path: Path,
) -> bool:
    """Recognize the Windows startup race seen before a thread is created."""
    if completed.returncode == 0 or events_path.stat().st_size:
        return False
    try:
        log = log_path.read_text(encoding="utf-8").lower()
    except (OSError, UnicodeError):
        return False
    return (
        "the system cannot find the path specified" in log
        or "os error 3" in log
    )


def structured_turn_is_complete(events_path: Path, result_path: Path) -> bool:
    """Return whether Codex wrote both its final event and valid JSON result."""
    try:
        with result_path.open(encoding="utf-8") as source:
            json.load(source)
        with events_path.open(encoding="utf-8") as source:
            return any(
                isinstance(event, dict)
                and event.get("type") == "turn.completed"
                for line in source
                if line.strip()
                for event in (json.loads(line),)
            )
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False


def _stop_codex_process(process: subprocess.Popen) -> None:
    """Ask a Codex process group to stop, then escalate if necessary."""
    if process.poll() is not None:
        return
    try:
        if os.name == "nt" and hasattr(signal, "CTRL_BREAK_EVENT"):
            process.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            os.killpg(process.pid, signal.SIGINT)
    except (OSError, ProcessLookupError):
        pass
    try:
        process.wait(timeout=CODEX_STOP_GRACE_SECONDS)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        process.terminate()
    except OSError:
        pass
    try:
        process.wait(timeout=CODEX_STOP_GRACE_SECONDS)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        process.kill()
    except OSError:
        pass
    try:
        process.wait(timeout=CODEX_STOP_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        pass


def _run_codex_process(
    command: list[str],
    *,
    workspace: Path,
    environment: dict[str, str],
    events,
    log,
    events_path: Path,
    result_path: Path,
    timeout_seconds: float | None,
    completion_grace_seconds: float | None,
) -> tuple[subprocess.CompletedProcess, bool, bool]:
    """Run Codex while detecting completed turns with lingering tools."""
    popen_options: dict = {
        "cwd": workspace,
        "env": environment,
        "stdout": events,
        "stderr": log,
        "text": True,
    }
    if os.name == "nt":
        popen_options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        popen_options["start_new_session"] = True
    process = subprocess.Popen(command, **popen_options)
    started_at = time.monotonic()
    completed_at: float | None = None
    structured_result_complete = False
    timed_out = False
    try:
        while process.poll() is None:
            now = time.monotonic()
            if structured_turn_is_complete(events_path, result_path):
                if completed_at is None:
                    completed_at = now
                elif (
                    completion_grace_seconds is not None
                    and now - completed_at >= completion_grace_seconds
                ):
                    log.write(
                        "\nDriver: structured turn completed but Codex did "
                        "not exit within the grace period; stopping its "
                        "process group.\n"
                    )
                    log.flush()
                    structured_result_complete = True
                    _stop_codex_process(process)
                    break
            if (
                timeout_seconds is not None
                and completed_at is None
                and now - started_at >= timeout_seconds
            ):
                log.write(
                    f"\nDriver: Codex exceeded the {timeout_seconds:g}-second "
                    "wall-clock timeout; stopping its process group.\n"
                )
                log.flush()
                timed_out = True
                _stop_codex_process(process)
                break
            time.sleep(CODEX_POLL_INTERVAL_SECONDS)
    except BaseException:
        _stop_codex_process(process)
        raise
    if structured_turn_is_complete(events_path, result_path):
        # Some launchers, notably the Cygwin `codex` shell wrapper, can return
        # a nonzero status after the CLI has already fulfilled the structured
        # output contract.  The validated result and final event are the
        # authoritative success signal in that case.
        structured_result_complete = True
    return (
        subprocess.CompletedProcess(command, process.returncode),
        structured_result_complete,
        timed_out,
    )


def build_exec_command(
    *,
    codex: str,
    workspace: Path,
    prompt: str,
    schema_path: Path,
    result_path: Path,
    options: ModelOptions,
    web_search: str = "disabled",
) -> list[str]:
    if web_search not in WEB_SEARCH_MODES:
        raise CodexError(
            "web search must be one of " + ", ".join(WEB_SEARCH_MODES)
        )
    command = [
        codex,
        "exec",
        "--ephemeral",
        "--ignore-user-config",
        "--disable",
        "shell_snapshot",
        "--disable",
        "skill_mcp_dependency_install",
        "--config",
        f'web_search="{web_search}"',
        "--config",
        "apps._default.enabled=false",
        "--config",
        "agents.enabled=false",
        "--config",
        "sandbox_workspace_write.network_access=false",
        "--config",
        'windows.sandbox="elevated"',
        "--skip-git-repo-check",
        "--sandbox",
        "workspace-write",
        "--json",
        "--color",
        "never",
        "-C",
        path_for_codex(workspace),
        "--output-schema",
        path_for_codex(schema_path),
        "-o",
        path_for_codex(result_path),
    ]
    if options.model is not None:
        command.extend(("--model", options.model))
    if options.reasoning_effort is not None:
        command.extend(
            (
                "--config",
                f'model_reasoning_effort="{options.reasoning_effort}"',
            )
        )
    if options.fast:
        command.extend(
            (
                "--config",
                "features.fast_mode=true",
                "--config",
                'service_tier="fast"',
            )
        )
    command.append(prompt)
    return command


def run_structured_codex(
    *,
    codex: str,
    workspace: Path,
    prompt: str,
    schema_path: Path,
    result_filename: str = "agent-result.json",
    events_filename: str = "events.jsonl",
    log_filename: str = "run.log",
    options: ModelOptions = ModelOptions(),
    web_search: str = "disabled",
    launch_interval: float = CODEX_LAUNCH_INTERVAL_SECONDS,
    timeout_seconds: float | None = None,
    completion_grace_seconds: float | None = None,
) -> Path:
    """Run one structured Codex turn and return its final-response path."""
    workspace = workspace.resolve()
    grant_workspace_owner_inheritance(workspace)
    result_path = workspace / result_filename
    events_path = workspace / events_filename
    log_path = workspace / log_filename
    events_path.write_text("", encoding="utf-8")
    log_path.write_text("", encoding="utf-8")
    command = build_exec_command(
        codex=codex,
        workspace=workspace,
        prompt=prompt,
        schema_path=schema_path,
        result_path=result_path,
        options=options,
        web_search=web_search,
    )
    environment = codex_subprocess_environment()

    completed: subprocess.CompletedProcess | None = None
    structured_result_complete = False
    timed_out = False
    for attempt in range(1, MAX_CODEX_START_ATTEMPTS + 1):
        wait_for_codex_launch_slot(launch_interval)
        try:
            with (
                events_path.open("a", encoding="utf-8") as events,
                log_path.open("a", encoding="utf-8") as log,
            ):
                if attempt > 1:
                    log.write(
                        f"\n--- Codex startup retry {attempt}/"
                        f"{MAX_CODEX_START_ATTEMPTS} ---\n"
                    )
                    log.flush()
                if (
                    timeout_seconds is None
                    and completion_grace_seconds is None
                ):
                    completed = subprocess.run(
                        command,
                        cwd=workspace,
                        env=environment,
                        stdout=events,
                        stderr=log,
                        text=True,
                        check=False,
                    )
                else:
                    (
                        completed,
                        structured_result_complete,
                        timed_out,
                    ) = _run_codex_process(
                        command,
                        workspace=workspace,
                        environment=environment,
                        events=events,
                        log=log,
                        events_path=events_path,
                        result_path=result_path,
                        timeout_seconds=timeout_seconds,
                        completion_grace_seconds=completion_grace_seconds,
                    )
        except OSError as exc:
            if (
                attempt < MAX_CODEX_START_ATTEMPTS
                and getattr(exc, "winerror", None) in {2, 3}
            ):
                with log_path.open("a", encoding="utf-8") as log:
                    log.write(f"Codex startup error: {exc}\n")
                continue
            raise CodexError(
                f"could not start Codex; workspace preserved at "
                f"{workspace}: {exc}"
            ) from exc

        if completed.returncode == 0 or structured_result_complete or timed_out:
            break
        if (
            attempt == MAX_CODEX_START_ATTEMPTS
            or not is_transient_startup_failure(
                completed,
                events_path,
                log_path,
            )
        ):
            break

    normalize_workspace_access(workspace, codex)
    if timed_out:
        raise CodexError(
            f"Codex exceeded the {timeout_seconds:g}-second wall-clock "
            f"timeout; workspace preserved at {workspace}"
        )
    if (
        completed is None
        or (completed.returncode != 0 and not structured_result_complete)
    ):
        returncode = completed.returncode if completed is not None else "unknown"
        raise CodexError(
            f"Codex exited with status {returncode}; "
            f"workspace preserved at {workspace}"
        )
    return result_path
