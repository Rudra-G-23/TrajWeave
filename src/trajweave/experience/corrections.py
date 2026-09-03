"""Filter trivial ``human_correction`` turns (Stage 5 brief section 8).

The adapters emit ``human_correction`` for *any* user turn that follows assistant
activity once the task is set. In the real data a large fraction of those are
bare affirmations (``yes``, ``ok``, ``c``, ``continue``) - noise that would
otherwise dominate a "candidate lesson per correction" step.

This module derives ``is_meaningful_human_correction`` from the event summary.
It does **not** mutate the stored event - the original ``human_correction``
record is kept intact; Stage 5 just skips the non-meaningful ones when detecting
``human_correction_repair`` occurrences.

Heuristic (not a giant blacklist): a turn is *not* a meaningful correction when,
after trimming, it is short (<= ``min_tokens`` tokens) **and** carries none of
the signals that a real redirection carries - an imperative/directive verb, a
negation, a question, a path or filename, code, an error reference, or a
quantifier like "instead" / "should" / "must".
"""

from __future__ import annotations

import re

# Directive / redirection vocabulary. Short and defensible; matched as whole
# words. Presence of any one of these in a short turn makes it "meaningful".
_DIRECTIVE_WORDS = frozenset(
    {
        "use", "dont", "don", "not", "never", "instead", "should", "must",
        "need", "add", "remove", "delete", "revert", "undo", "rename", "move",
        "stop", "avoid", "fix", "change", "update", "rerun", "install",
        "ensure", "wrong", "actually", "wait", "prefer", "rather", "without",
        "missing", "broke", "broken", "failing", "fails", "again",
    }
)

# Bare acknowledgements - explicitly non-meaningful even if they sneak a couple
# of tokens in ("ok thanks", "yes please"). Used as a fast path and as the
# canonical test fixture; the token+signal test below would catch these anyway.
_ACK_ONLY = frozenset(
    {
        "y", "n", "yes", "no", "ok", "okay", "k", "kk", "sure", "yep", "yeah",
        "yup", "nope", "c", "go", "cont", "continue", "proceed", "next", "done",
        "thanks", "thank", "thankyou", "ty", "please", "pls", "good", "great",
        "perfect", "nice", "cool", "fine", "correct", "right", "exactly",
        "do it", "go ahead", "sounds good", "looks good", "lgtm", "ship it",
        "yes please", "ok thanks", "thank you", "that works", "makes sense",
    }
)

_TOKEN_RE = re.compile(r"[a-z0-9]+(?:['-][a-z0-9]+)*")
_PATHISH_RE = re.compile(r"[\w./-]+/[\w./-]+|\b\w+\.[a-z]{1,5}\b")
_CODEISH_RE = re.compile(r"`|\bdef \b|\bclass \b|[(){};]|=>|::|--\w")
_ERRORISH_RE = re.compile(
    r"\b(error|exception|traceback|failed|failure|assert|stack ?trace|"
    r"undefined|null|nan|segfault|timeout|\d{3}\b)",
    re.IGNORECASE,
)


def _normalize(summary: str) -> str:
    return re.sub(r"\s+", " ", summary or "").strip()


def is_meaningful_human_correction(summary: str | None, *, min_tokens: int = 3) -> bool:
    """Return ``True`` when ``summary`` reads like a genuine redirection.

    ``summary`` is the stored (whitespace-collapsed, <=500 char) event summary.
    """

    text = _normalize(summary)
    if not text:
        return False

    low = text.lower().rstrip(" .!")
    if low in _ACK_ONLY:
        return False

    tokens = _TOKEN_RE.findall(low)
    if not tokens:
        return False

    # Long turns almost always carry real content.
    if len(tokens) > min_tokens:
        return True

    # Short turn: keep only if it carries an actionable signal.
    if "?" in text:
        return True
    if _PATHISH_RE.search(text) or _CODEISH_RE.search(text) or _ERRORISH_RE.search(text):
        return True
    if any(tok in _DIRECTIVE_WORDS for tok in tokens):
        return True
    return False
