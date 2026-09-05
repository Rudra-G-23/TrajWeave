from __future__ import annotations

import pytest

from trajweave.experience.context import command_context, file_context, is_vendored


@pytest.mark.parametrize(
    "path,expected",
    [
        ("migrations/0007_add_user.py", "migration"),
        ("alembic/versions/abc123_thing.py", "migration"),
        ("app/models/user.py", "model"),
        ("src/schema.py", "model"),
        ("tests/test_billing.py", "tests"),
        ("billing/test_flow.py", "tests"),
        ("pyproject.toml", "config"),
        ("frontend/tsconfig.json", "config"),
        ("Makefile", "build"),
        ("Dockerfile", "build"),
        ("docs/guide.md", "docs"),
        ("README.md", "docs"),
        ("src/components/Button.tsx", "frontend"),
        ("api/handlers/user.py", "backend"),
        ("weird/thing.xyz", "other"),
        (None, "other"),
    ],
)
def test_file_context(path, expected):
    assert file_context(path) == expected


@pytest.mark.parametrize(
    "path",
    [
        "node_modules/left-pad/index.js",
        ".venv/lib/python3.11/site-packages/x.py",
        "frontend/.next/static/chunks/main.js",
        "target/debug/build/thing",
        "__pycache__/mod.cpython-311.pyc",
    ],
)
def test_is_vendored(path):
    assert is_vendored(path) is True
    assert is_vendored("src/app/main.py") is False


@pytest.mark.parametrize(
    "cmd,expected",
    [
        ("alembic upgrade head", "migration"),
        ("python manage.py makemigrations", "migration"),
        ("npx prisma migrate dev", "migration"),
        ("mypy src", "typecheck"),
        ("npx tsc --noEmit", "typecheck"),
        ("pytest -q", None),
        ("git status", None),
        (None, None),
    ],
)
def test_command_context(cmd, expected):
    assert command_context(cmd) == expected
