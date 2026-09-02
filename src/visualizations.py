"""Discovery and path safety for installed interactive visualizations."""

from __future__ import annotations

import hashlib
from pathlib import Path
import re

import open_problem_common as common


DIRECTORY_NAME = "visualizations"
VISUALIZATION_RE = re.compile(r"^visualization-([0-9]{3,})$")
MANIFEST_NAME = "visualization.json"
REVIEW_NAME = "fidelity-review.json"
CRITIQUE_NAME = "fidelity-critique.md"


def package_key(directory: Path) -> str:
    """Return a URL-safe opaque identity for one installed package."""
    return hashlib.sha256(str(directory.resolve()).encode("utf-8")).hexdigest()[:24]


def next_number(attempt_directory: Path) -> int:
    root = attempt_directory / DIRECTORY_NAME
    numbers = (
        [
            int(match.group(1))
            for path in root.iterdir()
            if path.is_dir()
            and (match := VISUALIZATION_RE.fullmatch(path.name))
        ]
        if root.is_dir()
        else []
    )
    return max(numbers, default=0) + 1


def discover(attempt_directory: Path) -> list[dict]:
    """Return display-ready records for valid installed packages."""
    root = attempt_directory / DIRECTORY_NAME
    if not root.is_dir():
        return []
    records: list[dict] = []
    for directory in root.iterdir():
        match = VISUALIZATION_RE.fullmatch(directory.name)
        if not directory.is_dir() or match is None:
            continue
        manifest = common.load_json(directory / MANIFEST_NAME)
        if not isinstance(manifest, dict):
            continue
        entry_value = manifest.get("entry_point")
        if not isinstance(entry_value, str):
            continue
        try:
            entry = (directory / entry_value).resolve()
            entry.relative_to(directory.resolve())
        except (OSError, ValueError):
            continue
        if not entry.is_file():
            continue
        review = common.load_json(directory / REVIEW_NAME)
        review = review if isinstance(review, dict) else {}
        records.append(
            {
                "key": package_key(directory),
                "directory": str(directory.resolve()),
                "name": directory.name,
                "number": int(match.group(1)),
                "title": str(manifest.get("title") or directory.name),
                "summary": str(manifest.get("summary") or ""),
                "status": str(manifest.get("status") or ""),
                "entryPoint": entry_value,
                "claimRefs": manifest.get("claim_refs", []),
                "concepts": manifest.get("concepts", []),
                "limitations": manifest.get("limitations", []),
                "warnings": manifest.get("warnings", []),
                "fidelity": str(review.get("fidelity") or "unreviewed"),
                "expositionQuality": str(
                    review.get("exposition_quality") or "unreviewed"
                ),
                "interactionQuality": str(
                    review.get("interaction_quality") or "unreviewed"
                ),
                "reviewSummary": str(review.get("summary") or ""),
                "blockingGaps": review.get("blocking_gaps", []),
                "mathematicalFindings": review.get(
                    "mathematical_findings", []
                ),
                "interactionFindings": review.get(
                    "interaction_findings", []
                ),
                "expositionFindings": review.get(
                    "exposition_findings", []
                ),
                "critiquePath": str(directory / CRITIQUE_NAME)
                if (directory / CRITIQUE_NAME).is_file()
                else "",
            }
        )
    return sorted(records, key=lambda item: item["number"], reverse=True)


def resolve_file(directory: Path, relative_value: str) -> Path:
    """Resolve one package resource without allowing traversal or symlinks."""
    if not relative_value or "\\" in relative_value:
        raise ValueError("invalid visualization resource path")
    relative = Path(relative_value)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("invalid visualization resource path")
    root = directory.resolve()
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError("visualization resource leaves its package") from exc
    if not candidate.is_file():
        raise FileNotFoundError(candidate)
    unresolved = root
    for part in relative.parts:
        unresolved = unresolved / part
        if unresolved.is_symlink():
            raise ValueError("visualization resources cannot use symbolic links")
    return candidate
