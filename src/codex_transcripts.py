"""Keep per-invocation transcripts for managed runs, including failed turns."""

from functools import wraps
import json
import os
from pathlib import Path
import shutil
import tempfile
import time


TRANSCRIPT_ENV = "LOOSE_ENDS_CODEX_TRANSCRIPTS"


def transcript_stage(events: Path) -> str:
    """Identify the pipeline stage from its workspace or installed log name."""
    directory = events.parent.name
    stage = ""
    for prefix, label in (
        (".paper-review-run-", "Paper review"),
        (".review-run-", "Review"),
        (".solve-run-", "Solve"),
        (".write-run-", "Write"),
        (".literature-run-", "Literature review"),
        (".triage-run-", "Triage"),
        (".metadata-run-", "Extract metadata"),
        (".run-", "Analyze"),
    ):
        if directory.startswith(prefix):
            stage = label
            break
    if not stage:
        if events.name.startswith("review-"):
            stage = "Paper review" if directory.startswith("draft-") else "Review"
        elif events.name.startswith("literature-"):
            stage = "Literature review"
        elif events.name.startswith("triage-"):
            stage = "Triage"
        elif directory.startswith("attempt-"):
            stage = "Solve"
        elif directory.startswith("draft-"):
            stage = "Write"
        elif directory == "analysis":
            stage = "Analyze"
    if events.name.startswith("repair-"):
        return f"{stage} · Repair" if stage else "Repair"
    return stage


def artifact_transcripts(directories):
    """Find installed logs in the requested directories, without scanning history."""
    transcripts = []
    stages = {}
    for directory in dict.fromkeys(directories):
        for events in sorted(directory.glob("*events.jsonl")):
            if events.name.startswith("repair-"):
                continue  # Repairs are merged into the primary event stream.
            prefix = events.name.removesuffix("events.jsonl")
            stages[str(events)] = transcript_stage(events)
            transcripts.append((str(events), {
                "events": events,
                "diagnostics": directory / f"{prefix}run.log",
            }))
    return transcripts, stages


def record_transcript(function):
    @wraps(function)
    def recorded(**kwargs):
        destination = os.environ.get(TRANSCRIPT_ENV)
        if not destination:
            return function(**kwargs)
        root = Path(destination)
        root.mkdir(parents=True, exist_ok=True)
        folder = Path(tempfile.mkdtemp(prefix=f"turn-{time.time_ns()}-", dir=root))
        workspace = Path(kwargs["workspace"]).resolve()
        sources = {
            "events": str(workspace / kwargs.get("events_filename", "events.jsonl")),
            "diagnostics": str(workspace / kwargs.get("log_filename", "run.log")),
        }
        sources["stage"] = transcript_stage(Path(sources["events"]))
        (folder / "prompt.txt").write_text(kwargs["prompt"], encoding="utf-8")
        (folder / "source.json").write_text(json.dumps(sources), encoding="utf-8")
        try:
            return function(**kwargs)
        finally:
            # Snapshot before callers merge repair logs or move/delete workspaces.
            for name in ("events", "diagnostics"):
                source = sources[name]
                if Path(source).is_file():
                    temporary = folder / f"{name}.tmp"
                    shutil.copyfile(source, temporary)
                    os.replace(temporary, folder / name)
    return recorded
