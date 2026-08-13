#!/usr/bin/env python3
"""Minimal append-only artifact reporting for managed CLI runs."""

from __future__ import annotations

import os
from pathlib import Path
import threading
from typing import Iterable


ARTIFACT_LOG_ENV = "LOOSE_ENDS_ARTIFACT_LOG"
_artifact_log_lock = threading.Lock()


def report_artifacts(paths: Iterable[Path]) -> None:
    """Append installed project artifacts to the managed-run artifact log."""
    log_value = os.environ.get(ARTIFACT_LOG_ENV)
    if not log_value:
        return
    values = [str(Path(path).resolve()) for path in paths]
    if not values:
        return
    try:
        with _artifact_log_lock, Path(log_value).open(
            "a", encoding="utf-8", newline="\n"
        ) as log:
            for value in values:
                if "\n" in value or "\r" in value:
                    raise OSError("artifact paths cannot contain newlines")
                log.write(value + "\n")
            log.flush()
    except OSError as exc:
        raise OSError(f"could not report installed artifact: {exc}") from exc
