"""Small primitives shared by every task-specific output validator."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path, PurePosixPath
import re
from typing import Callable, Mapping


AGENT_RESULT_FILENAME = "agent-result.json"
EXPECTATIONS_FILENAME = "expectations.json"


@dataclass(frozen=True)
class ValidationIssue:
    code: str
    message: str
    path: str | None = None
    repairable: bool = True

    def render(self) -> str:
        location = f" [{self.path}]" if self.path else ""
        return f"{self.code}{location}: {self.message}"


@dataclass
class ValidationReport:
    result: dict | None = None
    files: list[Path] = field(default_factory=list)
    issues: list[ValidationIssue] = field(default_factory=list)

    @property
    def valid(self) -> bool:
        return not self.issues

    @property
    def repairable(self) -> bool:
        return bool(self.issues) and all(issue.repairable for issue in self.issues)

    def failure_message(self) -> str:
        return "\n".join(issue.render() for issue in self.issues)


class Reporter:
    def __init__(self) -> None:
        self.issues: list[ValidationIssue] = []

    def error(
        self,
        code: str,
        message: str,
        *,
        path: str | None = None,
        repairable: bool = True,
    ) -> None:
        self.issues.append(
            ValidationIssue(code, message, path=path, repairable=repairable)
        )


def read_json_object(
    path: Path,
    reporter: Reporter,
    *,
    code: str,
    description: str,
) -> dict | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        reporter.error(code, f"missing {description}", path=path.name)
        return None
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        reporter.error(
            code,
            f"could not read {description}: {exc}",
            path=path.name,
        )
        return None
    if not isinstance(value, dict):
        reporter.error(code, f"{description} is not a JSON object", path=path.name)
        return None
    return value


def read_markdown(
    path: Path,
    reporter: Reporter,
    *,
    code: str,
    description: str,
) -> str | None:
    try:
        contents = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        reporter.error(code, f"missing {description}", path=path.name)
        return None
    except (OSError, UnicodeError) as exc:
        reporter.error(
            code,
            f"could not read {description}: {exc}",
            path=path.name,
        )
        return None
    if not contents.strip():
        reporter.error(code, f"{description} is empty", path=path.name)
        return None
    if not contents.lstrip().startswith("#"):
        reporter.error(
            code,
            f"{description} does not start with a Markdown heading",
            path=path.name,
        )
        return None
    return contents


def string_list(value: object) -> list[str] | None:
    if not isinstance(value, list) or not all(
        isinstance(item, str) for item in value
    ):
        return None
    return value


def expectation_string_list(
    expectations: Mapping[str, object],
    field: str,
    reporter: Reporter,
) -> list[str]:
    value = string_list(expectations.get(field))
    if value is None:
        reporter.error(
            "E_EXPECTATIONS",
            f"expectations field {field!r} is not an array of strings",
            path=EXPECTATIONS_FILENAME,
            repairable=False,
        )
        return []
    return value


def safe_relative_path(value: object) -> PurePosixPath | None:
    if not isinstance(value, str) or not value or "\\" in value:
        return None
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or ".." in path.parts:
        return None
    return path


def validate_result_schema(
    result: object,
    expectations: Mapping[str, object],
    reporter: Reporter,
) -> None:
    """Validate the JSON-Schema subset used by this project's result schemas."""
    schema = expectations.get("result_schema")
    if schema is None:
        return
    if not isinstance(schema, dict):
        reporter.error(
            "E_EXPECTATIONS",
            "result_schema must be an object",
            path="expectations.json#/result_schema",
            repairable=False,
        )
        return
    _validate_schema_value(result, schema, reporter, "agent-result.json#")


def _validate_schema_value(
    value: object,
    schema: Mapping[str, object],
    reporter: Reporter,
    path: str,
) -> None:
    expected_type = schema.get("type")
    type_checks = {
        "object": lambda item: isinstance(item, dict),
        "array": lambda item: isinstance(item, list),
        "string": lambda item: isinstance(item, str),
        "boolean": lambda item: isinstance(item, bool),
        "integer": lambda item: isinstance(item, int) and not isinstance(item, bool),
        "number": lambda item: isinstance(item, (int, float)) and not isinstance(item, bool),
        "null": lambda item: item is None,
    }
    if isinstance(expected_type, str) and expected_type in type_checks:
        if not type_checks[expected_type](value):
            reporter.error(
                "E_SCHEMA_TYPE",
                f"expected {expected_type}",
                path=path,
            )
            return
    enum = schema.get("enum")
    if isinstance(enum, list) and value not in enum:
        reporter.error("E_SCHEMA_ENUM", f"value {value!r} is not allowed", path=path)
    pattern = schema.get("pattern")
    if isinstance(pattern, str) and isinstance(value, str):
        try:
            matches = re.search(pattern, value) is not None
        except re.error as exc:
            reporter.error(
                "E_EXPECTATIONS",
                f"invalid schema pattern: {exc}",
                path="expectations.json#/result_schema",
                repairable=False,
            )
        else:
            if not matches:
                reporter.error("E_SCHEMA_PATTERN", f"value {value!r} does not match {pattern}", path=path)
    if isinstance(value, dict):
        properties = schema.get("properties")
        properties = properties if isinstance(properties, dict) else {}
        required = schema.get("required")
        if isinstance(required, list):
            for field in required:
                if isinstance(field, str) and field not in value:
                    reporter.error("E_SCHEMA_REQUIRED", f"missing required field {field!r}", path=path)
        if schema.get("additionalProperties") is False:
            for field in sorted(set(value).difference(properties)):
                reporter.error("E_SCHEMA_PROPERTY", f"unknown field {field!r}", path=path)
        for field, item in value.items():
            child_schema = properties.get(field)
            if isinstance(child_schema, dict):
                _validate_schema_value(item, child_schema, reporter, f"{path}/{field}")
    if isinstance(value, list):
        min_items = schema.get("minItems")
        max_items = schema.get("maxItems")
        if isinstance(min_items, int) and len(value) < min_items:
            reporter.error("E_SCHEMA_LENGTH", f"array requires at least {min_items} item(s)", path=path)
        if isinstance(max_items, int) and len(value) > max_items:
            reporter.error("E_SCHEMA_LENGTH", f"array allows at most {max_items} item(s)", path=path)
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(value):
                _validate_schema_value(item, item_schema, reporter, f"{path}/{index}")


def run_agent_validation(
    validate: Callable[..., ValidationReport],
    *,
    validation_directory: Path,
) -> int:
    """Run a staged validator for a Codex agent with no CLI arguments."""
    reporter = Reporter()
    expectations = read_json_object(
        validation_directory / EXPECTATIONS_FILENAME,
        reporter,
        code="E_EXPECTATIONS",
        description="validation expectations",
    )
    if expectations is None:
        report = ValidationReport(issues=reporter.issues)
    else:
        try:
            report = validate(
                workspace=validation_directory.parent,
                expectations=expectations,
            )
        except Exception as exc:  # Agent-facing diagnostics must stay readable.
            report = ValidationReport(
                issues=[
                    ValidationIssue(
                        "E_VALIDATOR_FAILURE",
                        f"validator could not run: {exc}",
                        repairable=False,
                    )
                ]
            )
    if report.valid:
        print("Validation passed.")
        return 0
    print(f"Validation failed with {len(report.issues)} issue(s):")
    for issue in report.issues:
        print(f"- {issue.render()}")
    return 1
