"""Evidence-only scope extraction and path privacy handling for Stage 6."""

from __future__ import annotations

from collections import Counter
import json
import posixpath
import re
from typing import Any, Iterable

from trajweave.experience.context import command_context
from trajweave.placement.models import Scope

_WINDOWS_ABSOLUTE = re.compile(r"^[A-Za-z]:[\\/]")
_HOME_PREFIX = re.compile(r"^(~[\\/]|/home/|/Users/|/private/|/var/)")

# The ordering is intentional and is also the scope tie-break documented by
# Stage 6.  Do not infer a framework label without explicit stored evidence.
_SCOPE_TIE_ORDER = {
    "directory": 0,
    "subsystem": 1,
    "command": 2,
    "framework": 3,
    "language": 4,
    "extension": 5,
}
_SPECIFICITY = {
    "directory": 0.95,
    "subsystem": 0.65,
    "command": 0.90,
    "framework": 0.85,
    "language": 0.72,
    "extension": 0.60,
}
_LANGUAGE_BY_EXTENSION = {
    ".py": "python", ".pyi": "python", ".js": "javascript", ".mjs": "javascript",
    ".cjs": "javascript", ".ts": "typescript", ".tsx": "typescript",
    ".jsx": "javascript", ".go": "go", ".rs": "rust", ".rb": "ruby",
    ".java": "java", ".kt": "kotlin", ".cs": "csharp", ".php": "php",
    ".swift": "swift", ".sql": "sql", ".sh": "shell", ".bash": "shell",
}
_VALID_SUBSYSTEMS = {
    "migration", "model", "database", "tests", "config", "build", "ci",
    "backend", "frontend", "docs",
}


def normalize_repository_path(value: object) -> str | None:
    """Return a safe normalized repository-relative path or ``None``.

    An event path has no reliable repository root in the Stage 5 contract.  It
    is therefore safer to discard absolute, home-relative, and upward-traversal
    paths than to try to make them relative.  This function is used both for
    scope values and content sanitation.
    """

    if not isinstance(value, str):
        return None
    path = value.strip().replace("\\", "/")
    if not path or path == "." or path.startswith("/"):
        return None
    if _WINDOWS_ABSOLUTE.match(path) or _HOME_PREFIX.match(path):
        return None
    normalized = posixpath.normpath(path)
    if normalized in ("", ".") or normalized == ".." or normalized.startswith("../"):
        return None
    # A URI or a control character is not a repository relative path either.
    if "://" in normalized or any(ord(char) < 32 for char in normalized):
        return None
    return normalized


def _feature_blob(record: dict[str, Any]) -> dict[str, Any]:
    raw = record.get("features")
    if isinstance(raw, dict):
        return raw
    raw = record.get("features_json")
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _strings(value: object) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, (list, tuple, set)):
        for item in value:
            if isinstance(item, str):
                yield item


def _event_slice(evidence: dict[str, Any], events_by_trajectory: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    trajectory_id = evidence.get("trajectory_id")
    if not isinstance(trajectory_id, str):
        return []
    start = evidence.get("start_sequence")
    end = evidence.get("end_sequence")
    try:
        start_number = int(start)
        end_number = int(end)
    except (TypeError, ValueError):
        return list(events_by_trajectory.get(trajectory_id, []))
    return [
        event for event in events_by_trajectory.get(trajectory_id, [])
        if _in_range(event.get("sequence"), start_number, end_number)
    ]


def _in_range(sequence: object, start: int, end: int) -> bool:
    try:
        return start <= int(sequence) <= end
    except (TypeError, ValueError):
        return False


def _command_family(value: object) -> str | None:
    """Map only clear, non-sensitive command evidence to a stable family."""

    if not isinstance(value, str):
        return None
    lower = value.lower()
    tagged = command_context(value)
    if tagged:
        return tagged
    for token, family in (
        ("pytest", "pytest"), ("jest", "jest"), ("vitest", "vitest"),
        ("ruff", "ruff"), ("eslint", "eslint"), ("flake8", "flake8"),
        ("cargo test", "cargo-test"), ("go test", "go-test"),
        ("npm test", "npm-test"), ("pnpm test", "pnpm-test"),
    ):
        if token in lower:
            return family
    return None


def _support_values(
    support: list[dict[str, Any]], events_by_trajectory: dict[str, list[dict[str, Any]]]
) -> dict[str, list[set[str]]]:
    values: dict[str, list[set[str]]] = {
        "directory": [], "extension": [], "language": [], "command": [], "subsystem": [],
    }
    for evidence in support:
        paths: set[str] = set()
        commands: set[str] = set()
        blob = _feature_blob(evidence)
        for key in ("paths", "file_paths", "changed_paths", "path"):
            paths.update(path for item in _strings(blob.get(key)) if (path := normalize_repository_path(item)))
            # The repository's Stage 6 read join exposes the same compact
            # values at top level.  Accepting both shapes keeps this package
            # decoupled from a particular serialization boundary.
            paths.update(path for item in _strings(evidence.get(key)) if (path := normalize_repository_path(item)))
        for key in ("commands", "command"):
            commands.update(item for item in _strings(blob.get(key)))
            commands.update(item for item in _strings(evidence.get(key)))
        for event in _event_slice(evidence, events_by_trajectory):
            path = normalize_repository_path(event.get("path"))
            if path:
                paths.add(path)
            command = event.get("command")
            if isinstance(command, str):
                commands.add(command)

        directories = {posixpath.dirname(path) for path in paths if posixpath.dirname(path) not in ("", ".")}
        extensions = {posixpath.splitext(path)[1].lower() for path in paths if posixpath.splitext(path)[1]}
        values["directory"].append(directories)
        values["extension"].append(extensions)
        values["language"].append({_LANGUAGE_BY_EXTENSION[e] for e in extensions if e in _LANGUAGE_BY_EXTENSION})
        values["command"].append({family for command in commands if (family := _command_family(command))})

        subsystem = evidence.get("repair_context")
        subsystems = {subsystem} if subsystem in _VALID_SUBSYSTEMS else set()
        for item in _strings(blob.get("antecedent_contexts")):
            if item in _VALID_SUBSYSTEMS:
                subsystems.add(item)
        values["subsystem"].append(subsystems)
    return values


def infer_scope(
    support: list[dict[str, Any]], events_by_trajectory: dict[str, list[dict[str, Any]]]
) -> Scope:
    """Choose the strongest scope repeated by at least two support episodes.

    A value must appear in at least two distinct supporting occurrences and in
    75 percent of all supporting occurrences.  Paths are compared exactly after
    repository-relative normalization - no basename or filename-similarity
    heuristic is used.
    """

    if not support:
        return Scope("global", None)
    candidates: list[Scope] = []
    for scope_type, occurrence_sets in _support_values(support, events_by_trajectory).items():
        counter: Counter[str] = Counter()
        for values in occurrence_sets:
            for value in values:
                counter[value] += 1
        for value, count in counter.items():
            concentration = count / len(support)
            if count >= 2 and concentration >= 0.75:
                candidates.append(Scope(
                    scope_type=scope_type,
                    scope_value=value,
                    support_count=count,
                    concentration=concentration,
                    specificity=_SPECIFICITY[scope_type],
                ))
    if not candidates:
        return Scope("global", None)
    return min(
        candidates,
        key=lambda scope: (
            -(scope.concentration * scope.specificity),
            -scope.support_count,
            _SCOPE_TIE_ORDER[scope.scope_type],
            scope.scope_value or "",
        ),
    )
