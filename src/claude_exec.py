#!/usr/bin/env python3
"""Run one non-interactive Claude Code turn and emit Codex-style JSONL events.

The rest of Loose Ends (turn-completion checks, credit detection, the usage
index, and the workbench transcript viewer) reads the event schema that
``codex exec --json`` produces: ``thread.started``, ``turn.started``,
``item.started``/``item.completed``, ``turn.completed`` with token usage,
``turn.failed``, and ``error``.  This shim runs
``claude -p --output-format stream-json``, translates each event as it arrives,
and prints the translation to standard output, so the driver can keep treating
the agent as one process that streams events into ``events.jsonl``.

Usage::

    claude_exec.py [--result PATH] [--schema PATH] -- CLAUDE [ARGS...]

The prompt is read from standard input by Claude itself.  With ``--schema``,
the schema file is passed to Claude as ``--json-schema`` and the validated
``structured_output`` of a successful turn is written to ``--result`` before
``turn.completed`` is emitted.  Claude's standard error is inherited (the
driver sends it to ``run.log``).
"""

from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
from typing import Callable


MAX_OUTPUT_CHARACTERS = 100_000
MAX_INPUT_STRING_CHARACTERS = 2_000
USAGE_LIMIT_PATTERN = re.compile(
    r"usage limit|hit your (?:usage )?limit|limit reached|credit balance is too low"
    r"|out of (?:extra )?usage|out of credits|insufficient (?:credits|quota)",
    re.IGNORECASE,
)


def _count(value: object) -> int:
    return value if type(value) is int and value >= 0 else 0


def codex_usage(result: dict) -> dict | None:
    """Convert Claude usage to the Codex counters used by the usage index.

    Codex reports ``input_tokens`` including cached input, with cache reads
    and writes as subsets; Claude reports uncached input separately.  Claude
    also reports an API-list-price total, which is kept as ``cost_usd``.
    """
    model_usage = result.get("modelUsage")
    if isinstance(model_usage, dict) and model_usage:
        rows = [row for row in model_usage.values() if isinstance(row, dict)]
        uncached = sum(_count(row.get("inputTokens")) for row in rows)
        read = sum(_count(row.get("cacheReadInputTokens")) for row in rows)
        write = sum(_count(row.get("cacheCreationInputTokens")) for row in rows)
        output = sum(_count(row.get("outputTokens")) for row in rows)
        thinking = sum(_count(row.get("thinkingTokens")) for row in rows)
    else:
        usage = result.get("usage")
        if not isinstance(usage, dict):
            return None
        uncached = _count(usage.get("input_tokens"))
        read = _count(usage.get("cache_read_input_tokens"))
        write = _count(usage.get("cache_creation_input_tokens"))
        output = _count(usage.get("output_tokens"))
        details = usage.get("output_tokens_details")
        thinking = _count(details.get("thinking_tokens")) if isinstance(details, dict) else 0
    converted = {
        "input_tokens": uncached + read + write,
        "cached_input_tokens": read,
        "cache_write_input_tokens": write,
        "output_tokens": output,
        "reasoning_output_tokens": min(thinking, output),
    }
    cost = result.get("total_cost_usd")
    if (
        isinstance(cost, (int, float))
        and not isinstance(cost, bool)
        and math.isfinite(cost)
        and cost >= 0
    ):
        converted["cost_usd"] = float(cost)
    return converted


def _text_of(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
            elif isinstance(block, dict) and block.get("type") == "image":
                parts.append("[image]")
            else:
                parts.append(json.dumps(block, ensure_ascii=False))
        return "\n".join(parts)
    if content is None:
        return ""
    return json.dumps(content, ensure_ascii=False)


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n… [{len(text) - limit} more characters truncated]"


def _compact_input(value: object) -> object:
    """Keep tool inputs readable without copying whole files into the log."""
    if isinstance(value, str):
        return _truncate(value, MAX_INPUT_STRING_CHARACTERS)
    if isinstance(value, dict):
        return {key: _compact_input(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_compact_input(item) for item in value]
    return value


def tool_item(block: dict) -> dict:
    name = str(block.get("name") or "tool")
    arguments = block.get("input") if isinstance(block.get("input"), dict) else {}
    item: dict = {"id": str(block.get("id") or ""), "status": "in_progress"}
    if name == "Bash":
        item.update(type="command_execution", command=str(arguments.get("command", "")))
    elif name == "WebSearch":
        item.update(type="web_search", query=str(arguments.get("query", "")))
    elif name == "StructuredOutput":
        item.update(
            type="structured_output",
            text=json.dumps(arguments, indent=2, ensure_ascii=False),
        )
    else:
        target = next(
            (
                str(arguments[key])
                for key in ("file_path", "notebook_path", "path", "pattern", "url")
                if arguments.get(key)
            ),
            "",
        )
        item.update(
            type="tool_call",
            tool=name,
            command=f"{name} {target}".strip(),
            input=_compact_input(arguments),
        )
    return item


class Translator:
    """Stateful Claude stream-json to Codex JSONL event translation."""

    def __init__(
        self,
        emit: Callable[[dict], None],
        *,
        result_path: Path | None = None,
    ):
        self.emit = emit
        self.result_path = result_path
        self.started = False
        self.finished = False
        self.pending: dict[str, dict] = {}
        self.sequence = 0
        self.limit_message: str | None = None

    def _next_id(self, prefix: str) -> str:
        self.sequence += 1
        return f"{prefix}-{self.sequence}"

    def _start(self, session_id: object = None, **details) -> None:
        if self.started:
            return
        self.started = True
        self.emit({
            "type": "thread.started",
            "thread_id": str(session_id or "claude"),
            **{key: value for key, value in details.items() if value is not None},
        })
        self.emit({"type": "turn.started"})

    def handle(self, event: dict) -> None:
        kind = event.get("type")
        if kind == "system" and event.get("subtype") == "init":
            self._start(
                event.get("session_id"),
                model=event.get("model"),
                claude_code_version=event.get("claude_code_version"),
            )
        elif kind == "assistant":
            self._start(event.get("session_id"))
            self._assistant(event)
        elif kind == "user":
            self._tool_results(event)
        elif kind == "rate_limit_event":
            info = event.get("rate_limit_info")
            if isinstance(info, dict) and info.get("status") == "rejected":
                window = info.get("rateLimitType") or "usage"
                self.limit_message = f"Claude {window} usage limit reached"
        elif kind == "result":
            self._start(event.get("session_id"))
            self._result(event)

    def _assistant(self, event: dict) -> None:
        message = event.get("message")
        if not isinstance(message, dict):
            return
        content = message.get("content")
        if not isinstance(content, list):
            return
        for block in content:
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")
            if block_type == "text" and str(block.get("text", "")).strip():
                self.emit({"type": "item.completed", "item": {
                    "id": self._next_id("message"),
                    "type": "agent_message",
                    "text": block["text"],
                }})
            elif block_type == "thinking" and str(block.get("thinking", "")).strip():
                self.emit({"type": "item.completed", "item": {
                    "id": self._next_id("reasoning"),
                    "type": "reasoning",
                    "text": block["thinking"],
                }})
            elif block_type == "tool_use":
                item = tool_item(block)
                if not item["id"]:
                    item["id"] = self._next_id("tool")
                self.pending[item["id"]] = item
                self.emit({"type": "item.started", "item": dict(item)})

    def _tool_results(self, event: dict) -> None:
        message = event.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            return
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            identity = str(block.get("tool_use_id") or "")
            item = self.pending.pop(identity, None) or {
                "id": identity or self._next_id("tool"),
                "type": "tool_call",
            }
            item["status"] = "failed" if block.get("is_error") else "completed"
            if item.get("type") != "structured_output" or block.get("is_error"):
                item["aggregated_output"] = _truncate(
                    _text_of(block.get("content")), MAX_OUTPUT_CHARACTERS
                )
            self.emit({"type": "item.completed", "item": item})

    def _write_result(self, value: object) -> None:
        assert self.result_path is not None
        temporary = self.result_path.with_name(self.result_path.name + ".tmp")
        temporary.write_text(
            json.dumps(value, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, self.result_path)

    def _result(self, event: dict) -> None:
        if self.finished:
            return
        self.finished = True
        usage = codex_usage(event)
        details = {
            "duration_ms": event.get("duration_ms"),
            "num_turns": event.get("num_turns"),
            "models": sorted(event["modelUsage"]) if isinstance(event.get("modelUsage"), dict) else None,
        }
        details = {key: value for key, value in details.items() if value is not None}
        succeeded = event.get("subtype") == "success" and not event.get("is_error")
        message = None
        if succeeded and self.result_path is not None:
            structured = event.get("structured_output")
            if structured is None:
                succeeded = False
                message = "Claude Code returned no structured output for the required schema"
            else:
                self._write_result(structured)
        if succeeded:
            self.emit({"type": "turn.completed", "usage": usage, **details})
            return
        if message is None:
            errors = event.get("errors")
            message = (
                str(event.get("result") or "").strip()
                or (", ".join(map(str, errors)) if isinstance(errors, list) else "")
                or f"Claude Code turn ended with {event.get('subtype') or 'an error'}"
            )
        if self.limit_message or USAGE_LIMIT_PATTERN.search(message):
            self.emit({
                "type": "error",
                "code": "usage_limit_reached",
                "message": message if USAGE_LIMIT_PATTERN.search(message) else (
                    f"{self.limit_message}: {message}"
                ),
            })
        error = {"message": message}
        if event.get("api_error_status") is not None:
            error["status"] = event["api_error_status"]
        self.emit({"type": "turn.failed", "error": error, "usage": usage, **details})

    def finish(self, returncode: int) -> bool:
        """Report a process that ended without a final result event."""
        if self.finished or not self.started:
            return False
        self.finished = True
        message = f"Claude Code exited with status {returncode} before reporting a result"
        if self.limit_message:
            self.emit({"type": "error", "code": "usage_limit_reached",
                       "message": f"{self.limit_message}; {message}"})
        self.emit({"type": "turn.failed", "error": {"message": message}})
        return True


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--result", type=Path, help="where to write structured output")
    parser.add_argument("--schema", type=Path, help="JSON Schema for structured output")
    parser.add_argument("command", nargs=argparse.REMAINDER, help="-- CLAUDE [ARGS...]")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    command = list(args.command)
    if command[:1] == ["--"]:
        command = command[1:]
    if not command:
        print("claude_exec.py: missing Claude command", file=sys.stderr)
        return 2
    if (args.result is None) != (args.schema is None):
        print("claude_exec.py: --result and --schema go together", file=sys.stderr)
        return 2
    if args.schema is not None:
        try:
            schema = json.loads(args.schema.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            print(f"claude_exec.py: could not read schema {args.schema}: {exc}", file=sys.stderr)
            return 2
        if isinstance(schema, dict):
            # Claude Code's validator rejects meta-schema URIs it does not
            # bundle (such as draft 2020-12); the host validates separately.
            schema = {key: value for key, value in schema.items() if key != "$schema"}
        command.extend(("--json-schema", json.dumps(schema, separators=(",", ":"))))

    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="backslashreplace")

    def emit(event: dict) -> None:
        sys.stdout.write(json.dumps(event, ensure_ascii=False) + "\n")
        sys.stdout.flush()

    translator = Translator(emit, result_path=args.result)
    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError as exc:
        print(f"claude_exec.py: could not start {command[0]!r}: {exc}", file=sys.stderr)
        return 127

    def forward(signum, _frame):
        try:
            process.send_signal(signum)
        except OSError:
            pass

    if os.name != "nt":
        signal.signal(signal.SIGTERM, forward)
    try:
        assert process.stdout is not None
        for line in process.stdout:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                event = json.loads(stripped)
            except json.JSONDecodeError:
                print(f"Claude output: {stripped}", file=sys.stderr, flush=True)
                continue
            if isinstance(event, dict):
                translator.handle(event)
        returncode = process.wait()
    except KeyboardInterrupt:
        try:
            process.send_signal(signal.SIGINT)
        except OSError:
            pass
        returncode = process.wait()
    if translator.finish(returncode) and returncode == 0:
        return 1
    return returncode


if __name__ == "__main__":
    sys.exit(main())
