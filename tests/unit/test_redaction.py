from __future__ import annotations

import pytest

from trajweave.normalization.redaction import redact, redact_mapping


@pytest.mark.parametrize(
    "secret",
    [
        "export OPENAI_API_KEY=sk-proj-abcdefghijklmnopqrstuvwxyz012345",
        "AWS key AKIAIOSFODNN7EXAMPLE here",
        "token: ghp_1234567890abcdefghijklmnopqrstuvwxyzAB",
        "Authorization: Bearer abcdef1234567890abcdef",
        "PGPASSWORD=hunter2supersecret psql",
        "https://user:s3cr3tpass@example.com/repo.git",
    ],
)
def test_redact_catches_common_secrets(secret):
    clean, flag = redact(secret)
    assert flag is True
    assert "REDACTED" in clean


def test_redact_leaves_clean_text_untouched():
    text = "pytest tests/test_billing.py -q"
    clean, flag = redact(text)
    assert clean == text
    assert flag is False


def test_redact_mapping_recurses():
    data = {"cmd": "curl -H 'x: Bearer abcdef1234567890abcdef'", "nested": ["ok", {"k": "AKIAIOSFODNN7EXAMPLE"}]}
    clean, flag = redact_mapping(data)
    assert flag is True
    assert "REDACTED" in clean["cmd"]
    assert "REDACTED" in clean["nested"][1]["k"]
    assert clean["nested"][0] == "ok"
