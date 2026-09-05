"""Classify a raw shell command into a coarse :class:`CommandKind`.

Used to derive the ``test_*`` / ``lint_*`` / ``build_*`` event families from a
plain ``command`` event. Heuristic and intentionally shallow - when in doubt we
return :data:`CommandKind.OTHER` and emit only a generic ``command`` event.

Token matching is deliberately conservative (Stage 4 finding): a single-word
tool name (``pytest``, ``make``, ``tsc`` ...) must appear as a real argv token,
not as a substring of a path or inside a quoted string, and a segment that only
*mentions* a tool (``echo``, ``which``, ``--version``) never counts as a run.
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

#: Leading words that only inspect/print - a segment starting with one of these
#: is never a run of whatever it names (``echo "run pytest"``, ``which ruff``).
_INSPECT_ONLY = frozenset(
    {"echo", "printf", "which", "type", "true", "false", ":", "command", "man", "help"}
)

_ENV_ASSIGN = re.compile(r"[a-z_][a-z0-9_]*=.*")


def _needle_re(needle: str) -> re.Pattern[str]:
    """Multi-word needles match as a phrase; single-word needles must be a whole
    argv token (not a path/identifier fragment)."""

    if " " in needle:
        return re.compile(re.escape(needle))
    return re.compile(r"(?<![\w./-])" + re.escape(needle) + r"(?![\w-])")


def _compile(tokens: tuple[str, ...]) -> tuple[re.Pattern[str], ...]:
    return tuple(_needle_re(t) for t in tokens)


_TEST_RE = _compile(_TEST_TOKENS)
_LINT_RE = _compile(_LINT_TOKENS)
_BUILD_RE = _compile(_BUILD_TOKENS)
_PACKAGE_RE = _compile(_PACKAGE_TOKENS)


def _normalize(command: str) -> str:
    return re.sub(r"\s+", " ", command.strip().lower())


def _strip_literals(seg: str) -> str:
    """Blank out quoted spans so a tool named inside a string is not a run."""

    seg = re.sub(r"\"[^\"]*\"", " ", seg)
    seg = re.sub(r"'[^']*'", " ", seg)
    return seg


def _matches(haystack: str, needles: tuple[str, ...]) -> bool:
    return any(n in haystack for n in needles)


def _matches_re(haystack: str, needle_res: tuple[re.Pattern[str], ...]) -> bool:
    return any(r.search(haystack) for r in needle_res)


def _runnable_segments(segments: list[str]) -> list[str]:
    """Segments that actually execute a tool, with quoted text removed and
    inspect-only / ``--version`` / ``--help`` segments dropped."""

    out: list[str] = []
    for seg in segments:
        toks = seg.split()
        while toks and _ENV_ASSIGN.fullmatch(toks[0]):
            toks = toks[1:]
        first = toks[0].strip("()") if toks else ""
        if first in _INSPECT_ONLY:
            continue
        if "--version" in seg or "--help" in seg:
            continue
        out.append(_strip_literals(seg))
    return out


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

    runnable = _runnable_segments(segments)
    for seg in runnable:
        if _matches_re(seg, _TEST_RE):
            return CommandKind.TEST
    for seg in runnable:
        if _matches_re(seg, _LINT_RE):
            return CommandKind.LINT
    for seg in runnable:
        if _matches_re(seg, _BUILD_RE):
            return CommandKind.BUILD
    for seg in runnable:
        if _matches_re(seg, _PACKAGE_RE):
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
