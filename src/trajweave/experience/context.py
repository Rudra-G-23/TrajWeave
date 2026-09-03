"""Lightweight, deterministic file/command context tagging (Stage 5 brief 19-20).

We only use signals that are cheap and hard to get wrong: path segments, well
known directory names, and file extensions. When nothing matches we return
``"other"`` rather than guessing.

The core :class:`~trajweave.models.enums.CommandKind` classifier is intentionally
left untouched; ``command_context`` derives the two Stage-5-only tags
(``migration`` / ``typecheck``) here.
"""

from __future__ import annotations

import posixpath
import re

# Ordered: first match wins, most specific first.
_CONTEXT_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("migration", re.compile(r"(^|/)(migrations?|alembic|versions)(/|$)|_migration|/migrate")),
    ("model", re.compile(r"(^|/)(models?|entities|schema|schemas)(/|$)|(^|/)model\.py$|(^|/)schema\.(py|rb|sql)$")),
    ("tests", re.compile(r"(^|/)(tests?|spec|specs|__tests__|e2e|integration)(/|$)|(^|/)test_[^/]+$|_test\.[a-z]+$|\.spec\.[a-z]+$|\.test\.[a-z]+$")),
    ("config", re.compile(
        r"(^|/)(pyproject\.toml|setup\.cfg|setup\.py|tox\.ini|pytest\.ini|"
        r"package\.json|package-lock\.json|pnpm-lock\.yaml|yarn\.lock|"
        r"tsconfig[^/]*\.json|\.eslintrc[^/]*|\.prettierrc[^/]*|babel\.config[^/]*|"
        r"jest\.config[^/]*|vite\.config[^/]*|webpack\.config[^/]*|"
        r"Cargo\.toml|go\.mod|Gemfile|requirements[^/]*\.txt|"
        r"\.pre-commit-config\.yaml|ruff\.toml|mypy\.ini|\.env[^/]*)$"
        r"|(^|/)(config|conf|settings)(/|$)"
    )),
    ("ci", re.compile(r"(^|/)(\.github/workflows|\.gitlab-ci|\.circleci|Jenkinsfile)")),
    ("build", re.compile(
        r"(^|/)(Makefile|CMakeLists\.txt|Dockerfile|docker-compose[^/]*\.ya?ml|"
        r"BUILD|WORKSPACE|Rakefile|build\.gradle[^/]*)$|(^|/)(build|dist|scripts)(/|$)"
    )),
    ("docs", re.compile(r"(^|/)(docs?|documentation)(/|$)|\.(md|rst|adoc|txt)$|(^|/)(README|CHANGELOG|LICENSE)")),
    ("database", re.compile(r"(^|/)(db|database|sql|queries|repositories?)(/|$)|\.sql$|(^|/)(dao|orm)(/|$)")),
    ("frontend", re.compile(
        r"(^|/)(src/)?(components?|pages|views|ui|client|frontend|web|styles?|assets)(/|$)"
        r"|\.(tsx|jsx|vue|svelte|css|scss|sass|less|html)$"
    )),
    ("backend", re.compile(
        r"(^|/)(api|server|backend|app|services?|handlers?|controllers?|routes?|lib|core|internal|cmd|pkg)(/|$)"
        r"|\.(py|go|rs|rb|java|kt|php|cs|ts|js|c|cc|cpp|h|hpp)$"
    )),
)

_VENDORED_RE = re.compile(
    r"(^|/)(node_modules|\.venv|venv|site-packages|vendor|dist|build|"
    r"\.next|\.nuxt|target|__pycache__|\.tox|\.mypy_cache|\.pytest_cache|"
    r"coverage|\.git)(/|$)"
)


def is_vendored(path: str | None) -> bool:
    """True for generated / third-party / build-output paths (Stage 5 brief 19/5)."""

    if not path:
        return False
    return bool(_VENDORED_RE.search(_clean(path)))


def _clean(path: str) -> str:
    p = str(path).strip().replace("\\", "/")
    return posixpath.normpath(p) if p else p


def file_context(path: str | None) -> str:
    """Map a repo-relative path to one coarse context bucket."""

    if not path:
        return "other"
    p = _clean(path)
    for name, rx in _CONTEXT_RULES:
        if rx.search(p):
            return name
    return "other"


_MIGRATION_CMD_RE = re.compile(
    r"\b(alembic|flask db|django-admin|manage\.py)\b.*\b(migrat|upgrade|downgrade|revision|makemigrations|stamp)"
    r"|\bmakemigrations\b|\bmigrate\b|\bprisma migrate\b|\bknex migrate\b|\brails db:migrate\b|\bsequelize db:migrate\b|"
    r"\bdbmate\b|\bgoose\b|\bsqlx migrate\b|\bdiesel migration\b|\byoyo\b",
    re.I,
)
_TYPECHECK_CMD_RE = re.compile(
    r"\b(mypy|pyright|pyre|tsc\b|tsc$|type-?check|typecheck|flow check|"
    r"cargo check|go vet)\b",
    re.I,
)


def command_context(command: str | None) -> str | None:
    """Stage-5-only command tags on top of the core CommandKind classifier.

    Returns ``"migration"`` / ``"typecheck"`` when the raw command clearly runs a
    schema migration or a type checker, else ``None``.
    """

    if not command:
        return None
    if _MIGRATION_CMD_RE.search(command):
        return "migration"
    if _TYPECHECK_CMD_RE.search(command):
        return "typecheck"
    return None
