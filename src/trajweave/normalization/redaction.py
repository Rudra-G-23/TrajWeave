"""Best-effort secret redaction.

Stage 0-3 is deterministic and conservative: we are not trying to be a full DLP
system, just to avoid the obvious mistake of copying an API key or a private key
out of a shell command into ``trajweave.db``. Anything matched is replaced with
``[REDACTED:<label>]`` and the surrounding record is flagged ``redacted=True``.
"""

from __future__ import annotations

import re
from typing import Any

_PLACEHOLDER = "[REDACTED:{label}]"

# (label, compiled pattern). Order matters - most specific first.
_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("private-key", re.compile(
        r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----.*?-----END (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----",
        re.DOTALL,
    )),
    ("aws-access-key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("aws-secret", re.compile(r"(?i)\baws_secret_access_key\b\s*[=:]\s*['\"]?[A-Za-z0-9/+=]{40}['\"]?")),
    ("gcp-api-key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("github-pat", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{60,}\b")),
    ("slack-token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("openai-key", re.compile(r"\bsk-(?:proj-|ant-|live-)?[A-Za-z0-9_\-]{20,}\b")),
    ("anthropic-key", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}\b")),
    ("bearer", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{16,}")),
    ("basic-auth-url", re.compile(r"\b([a-z][a-z0-9+.\-]*://)[^\s/:@]+:[^\s/@]+@")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b")),
    ("env-secret", re.compile(
        r"(?i)\b([A-Z0-9_]*(?:SECRET|TOKEN|PASSWORD|PASSWD|API[_-]?KEY|ACCESS[_-]?KEY|PRIVATE[_-]?KEY|CLIENT[_-]?SECRET)[A-Z0-9_]*)\s*[=:]\s*['\"]?([^\s'\"]{6,})['\"]?"
    )),
]


def redact(text: Any) -> tuple[Any, bool]:
    """Redact secrets from ``text``. Returns ``(clean_text, was_redacted)``."""

    if not isinstance(text, str) or not text:
        return text, False

    redacted = False
    out = text
    for label, pattern in _PATTERNS:
        if label == "basic-auth-url":
            new_out, n = pattern.subn(rf"\g<1>[REDACTED:{label}]@", out)
        elif label == "env-secret":
            new_out, n = pattern.subn(rf"\g<1>=[REDACTED:{label}]", out)
        else:
            new_out, n = pattern.subn(_PLACEHOLDER.format(label=label), out)
        if n:
            redacted = True
            out = new_out
    return out, redacted


def redact_mapping(data: Any) -> tuple[Any, bool]:
    """Recursively redact strings inside dicts/lists. Returns ``(clean, flag)``."""

    any_redacted = False
    if isinstance(data, str):
        return redact(data)
    if isinstance(data, dict):
        clean: dict[Any, Any] = {}
        for key, value in data.items():
            new_value, flag = redact_mapping(value)
            any_redacted |= flag
            clean[key] = new_value
        return clean, any_redacted
    if isinstance(data, (list, tuple)):
        items = []
        for value in data:
            new_value, flag = redact_mapping(value)
            any_redacted |= flag
            items.append(new_value)
        return (type(data)(items) if not isinstance(data, tuple) else items), any_redacted
    return data, False
