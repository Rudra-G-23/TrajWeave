"""Length-bounding and summarizing of free text before it lands in SQLite.

Raw prompts / command output can be huge. We store a bounded, single-purpose
summary rather than the whole thing (the original transcript stays in
``~/.codex`` / ``~/.claude``).
"""

from __future__ import annotations

import re

_WS = re.compile(r"\s+")

DEFAULT_SUMMARY_LIMIT = 500
DEFAULT_COMMAND_LIMIT = 2000
DEFAULT_TASK_LIMIT = 4000


def bound_text(text: str | None, limit: int) -> str | None:
    if text is None:
        return None
    text = text.strip()
    if not text:
        return None
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def summarize(text: str | None, limit: int = DEFAULT_SUMMARY_LIMIT) -> str | None:
    """Collapse whitespace and truncate to a one-line-ish summary."""

    if text is None:
        return None
    collapsed = _WS.sub(" ", text).strip()
    return bound_text(collapsed, limit)


# Wrapper blocks that harnesses inject around/instead of a real human prompt.
_INJECTED_MARKERS = (
    "<environment_context>",
    "<recommended_plugins>",
    "<skills_instructions>",
    "<user_instructions>",
    "<system-reminder>",
    "<available-skills>",
    "# AGENTS.md instructions",
    "# CLAUDE.md",
    "Caveat: The messages below",
    "<command-name>",
    "<local-command-",
    "<user-prompt-submit-hook>",
)

_STRIP_TAG = re.compile(
    r"<(environment_context|recommended_plugins|skills_instructions|user_instructions|"
    r"available-skills|system-reminder)>.*?</\1>",
    re.DOTALL | re.IGNORECASE,
)


def strip_injected_context(text: str | None) -> str | None:
    """Remove known injected wrapper blocks; return the residual human text.

    Returns ``None`` when nothing human-authored remains (the whole message was
    harness scaffolding).
    """

    if not text:
        return None
    cleaned = _STRIP_TAG.sub("", text).strip()
    if not cleaned:
        return None
    lowered = cleaned.lstrip()
    for marker in _INJECTED_MARKERS:
        if lowered.startswith(marker):
            # Drop everything up to the end of that block if we can find a blank
            # line, otherwise treat the whole thing as non-human.
            after = cleaned.split("\n\n", 1)
            if len(after) == 2 and after[1].strip():
                return strip_injected_context(after[1])
            return None
    # A message that is still nothing but an XML-ish tag is not human text.
    if lowered.startswith("<") and lowered.endswith(">") and "\n" not in lowered:
        return None
    return cleaned
