"""Classify a raw shell command into a coarse :class:`CommandKind`.

Used to derive the ``test_*`` / ``lint_*`` / ``build_*`` event families from a
plain ``command`` event. Heuristic and intentionally shallow - when in doubt we
return :data:`CommandKind.OTHER` and emit only a generic ``command`` event.
"""

from __future__ import annotations

import re
import shlex

from trajweave.models.enums import CommandKind, EventType

_TEST_TOKENS = (
    "pytest", "py.test", "unittest", "nosetests", "tox",
    "jest", "vitest", "mocha", "ava", "playwright", "cypress",
    "go test", "cargo test", "rspec", "phpunit", "rake test",
    "npm test", "npm run test", "pnpm test", "pnpm run test",
    "yarn test", "bun test", "ctest", "gradle test", "mvn test",
    "dotnet test", "./gradlew test",
)
_LINT_TOKENS = (
    "ruff", "flake8", "pylint", "black", "isort", "mypy", "pyright",
    "eslint", "prettier", "biome", "tslint", "stylelint",
    "golangci-lint", "gofmt", "go vet", "clippy", "cargo clippy",
    "rubocop", "standardrb", "shellcheck", "hadolint",
    "pre-commit",
)
_BUILD_TOKENS = (
    "make", "cmake", "ninja", "bazel", "buck",
    "npm run build", "pnpm build", "pnpm run build", "yarn build",
    "bun run build", "tsc", "webpack", "vite build", "rollup",
    "next build", "nuxt build", "cargo build", "go build",
    "mvn package", "mvn compile", "gradle build", "./gradlew build",
    "dotnet build", "pip install", "uv build", "poetry build",
    "docker build", "hatch build", "python -m build",
)
_READ_TOKENS = ("cat ", "less ", "head ", "tail ", "sed -n", "bat ")
_SEARCH_TOKENS = ("grep", "rg ", "ripgrep", "ag ", "ack ", "find ")
_LIST_TOKENS = ("ls ", "ls\n", "tree ", "dir ")
_VCS_TOKENS = ("git ", "hg ", "svn ", "jj ")
_PACKAGE_TOKENS = (
    "npm install", "npm ci", "pnpm install", "yarn install", "yarn add",
    "pip install", "uv pip", "uv add", "uv sync", "poetry install",
    "poetry add", "cargo add", "go get", "bundle install", "apt-get",
    "apt install", "brew install",
)


def _normalize(command: str) -> str:
    return re.sub(r"\s+", " ", command.strip().lower())


def _matches(haystack: str, needles: tuple[str, ...]) -> bool:
    return any(n in haystack for n in needles)


def classify_command(command: str | None, parsed_hint: str | None = None) -> CommandKind:
    """Best-effort classification.

    ``parsed_hint`` is Codex's own ``parsed_cmd[].type`` (``read``/``search``/
    ``list_files``/``unknown``) when available - it settles the read/search/list
    buckets, but test/lint/build still come from token matching because Codex
    marks those ``unknown``.
    """

    if not command:
        return CommandKind.OTHER

    norm = _normalize(command)

    # Split on shell separators so "cd x && pytest" still classifies as test.
    segments = re.split(r"&&|\|\||;|\|", norm)
    segments = [s.strip() for s in segments if s.strip()] or [norm]

    for seg in segments:
        if _matches(seg, _TEST_TOKENS):
            return CommandKind.TEST
    for seg in segments:
        if _matches(seg, _LINT_TOKENS):
            return CommandKind.LINT
    for seg in segments:
        if _matches(seg, _BUILD_TOKENS):
            return CommandKind.BUILD
    for seg in segments:
        if _matches(seg, _PACKAGE_TOKENS):
            return CommandKind.PACKAGE

    if parsed_hint == "read":
        return CommandKind.READ
    if parsed_hint == "search":
        return CommandKind.SEARCH
    if parsed_hint in ("list_files", "list"):
        return CommandKind.LIST

    for seg in segments:
        if _matches(seg, _VCS_TOKENS):
            return CommandKind.VCS
        if seg.startswith(_READ_TOKENS) or _matches(seg, _READ_TOKENS):
            return CommandKind.READ
        if _matches(seg, _SEARCH_TOKENS):
            return CommandKind.SEARCH
        if _matches(seg, _LIST_TOKENS) or seg in ("ls", "pwd"):
            return CommandKind.LIST

    return CommandKind.OTHER


_FAMILY = {
    CommandKind.TEST: (EventType.TEST_RUN, EventType.TEST_PASS, EventType.TEST_FAIL),
    CommandKind.LINT: (EventType.LINT_RUN, EventType.LINT_PASS, EventType.LINT_FAIL),
    CommandKind.BUILD: (EventType.BUILD_RUN, EventType.BUILD_PASS, EventType.BUILD_FAIL),
}


def command_event_family(
    kind: CommandKind, exit_code: int | None
) -> EventType | None:
    """Return the outcome event (e.g. ``TEST_FAIL``) for a classified command.

    ``None`` when the command is not a test/lint/build, or when there is no exit
    code to judge the outcome (we do not invent a pass/fail).
    """

    fam = _FAMILY.get(kind)
    if fam is None:
        return None
    _run, ok, bad = fam
    if exit_code is None:
        return None
    return ok if exit_code == 0 else bad


def command_run_event(kind: CommandKind) -> EventType | None:
    fam = _FAMILY.get(kind)
    return fam[0] if fam else None


def safe_join(argv: list[str]) -> str:
    try:
        return shlex.join(argv)
    except Exception:  # pragma: no cover - defensive
        return " ".join(argv)
