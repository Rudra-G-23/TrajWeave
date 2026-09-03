"""Turn an aggregated cluster into a human-readable title / summary / lesson.

Two implementations behind one tiny protocol:

* :class:`DeterministicSummarizer` - the default. Pure string templating over the
  deterministic features. No network, no key, fully reproducible.
* :class:`LlmSummarizer` - a seam for a later stage. Not wired to a provider in
  Stage 5 (the brief forbids requiring paid APIs); it raises unless a callable
  transport is injected, and callers fall back to the deterministic one.

The summarizer is only ever given compact, sanitized aggregate facts - never raw
transcripts, code, or secrets (Stage 5 brief 44).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from trajweave.experience.models import (
    PATTERN_FAILURE_REPAIR,
    PATTERN_FILE_CHANGE,
    PATTERN_HUMAN_CORRECTION_REPAIR,
    PATTERN_REPEATED_FAILURE,
)

_CONTEXT_PHRASE = {
    "migration": "a database migration",
    "model": "a persistent model / schema",
    "database": "the database layer",
    "tests": "the test suite",
    "config": "project configuration",
    "build": "the build setup",
    "ci": "CI configuration",
    "backend": "backend code",
    "frontend": "frontend code",
    "docs": "documentation",
    "other": "project files",
    "mixed": "several areas",
}
_FAMILY_PHRASE = {"test": "tests", "lint": "the linter", "build": "the build"}


@dataclass
class SummaryInput:
    """Compact, sanitized facts about one cluster."""

    group_key: str
    pattern_type: str
    occurrence_count: int
    support_count: int
    contradiction_count: int
    project_count: int
    repair_context: str | None
    resolution_family: str | None
    error_signature: str | None
    contexts: list[str]
    sample_correction: str | None = None


@dataclass
class Summary:
    title: str
    summary: str
    reusable_lesson: str
    context: list[str]
    source: str  # "deterministic" | "llm"
    tokens: int = 0


@runtime_checkable
class Summarizer(Protocol):
    def summarize(self, data: SummaryInput) -> Summary:  # pragma: no cover - protocol
        ...


class DeterministicSummarizer:
    """Template-based. Always available, always reproducible."""

    source = "deterministic"

    def summarize(self, d: SummaryInput) -> Summary:
        ctx = d.repair_context or (d.contexts[0] if d.contexts else "other")
        ctx_phrase = _CONTEXT_PHRASE.get(ctx, f"{ctx} files")
        fam_phrase = _FAMILY_PHRASE.get(d.resolution_family or "", "the checks")
        n = d.occurrence_count

        if d.pattern_type == PATTERN_FAILURE_REPAIR or d.pattern_type == PATTERN_FILE_CHANGE:
            title = f"{ctx.capitalize()} change needed before {fam_phrase} passed"
            summary = (
                f"Across {n} trajectories, work that changed related code was repeatedly "
                f"followed by a change to {ctx_phrase} before {fam_phrase} passed "
                f"({d.support_count} supporting, {d.contradiction_count} contradicting, "
                f"{d.project_count} project(s))."
            )
            lesson = (
                f"When making changes of this kind, check whether {ctx_phrase} also needs "
                f"to be updated before relying on {fam_phrase}."
            )
        elif d.pattern_type == PATTERN_HUMAN_CORRECTION_REPAIR:
            title = f"Recurring human correction about {ctx_phrase}"
            summary = (
                f"In {n} trajectories the user had to redirect the agent, after which a "
                f"change to {ctx_phrase} resolved the task "
                f"({d.project_count} project(s))."
            )
            lesson = (
                f"Proactively consider {ctx_phrase} for this kind of task instead of "
                f"waiting for the user to point it out."
            )
            if d.sample_correction:
                summary += f' Example correction: "{d.sample_correction.strip()[:160]}".'
        elif d.pattern_type == PATTERN_REPEATED_FAILURE:
            sig = (d.error_signature or "the same failure").strip()
            title = f"Recurring unresolved failure: {sig[:60]}"
            summary = (
                f"The failure signature \"{sig[:120]}\" appeared and was left unresolved "
                f"in {n} trajectories across {d.project_count} project(s)."
            )
            lesson = (
                f"This failure recurs and is not trivially fixed - when it appears, expect "
                f"it to need real investigation rather than a quick retry."
            )
        else:  # pragma: no cover - defensive
            title = f"Recurring pattern in {ctx_phrase}"
            summary = f"A pattern recurred in {n} trajectories."
            lesson = "Review the linked evidence."

        return Summary(
            title=title,
            summary=summary,
            reusable_lesson=lesson,
            context=sorted({c for c in [ctx, *d.contexts] if c and c != "other"}),
            source=self.source,
        )


class LlmSummarizer:
    """Optional semantic summarizer. Provider wiring is deferred (Stage 6+).

    ``transport`` must be ``Callable[[str], dict]`` returning a dict with
    ``title`` / ``summary`` / ``reusable_lesson`` / ``context`` / ``tokens``.
    Any failure falls back to the deterministic summarizer.
    """

    source = "llm"

    def __init__(self, transport: Any = None, *, model: str | None = None):
        self._transport = transport
        self._model = model
        self._fallback = DeterministicSummarizer()

    def summarize(self, d: SummaryInput) -> Summary:
        if self._transport is None:
            return self._fallback.summarize(d)
        try:
            raw = self._transport(_render_prompt(d, self._model))
            return Summary(
                title=str(raw["title"]).strip()[:200],
                summary=str(raw["summary"]).strip()[:1000],
                reusable_lesson=str(raw["reusable_lesson"]).strip()[:1000],
                context=[str(c) for c in raw.get("context", [])][:6],
                source="llm",
                tokens=int(raw.get("tokens", 0)),
            )
        except Exception:  # noqa: BLE001 - never let summarization break extraction
            return self._fallback.summarize(d)


def _render_prompt(d: SummaryInput, model: str | None) -> str:
    return (
        "Summarize this recurring coding-agent pattern as JSON with keys "
        "title, summary, reusable_lesson, context (list). Do NOT invent counts "
        "or decide where the rule should live.\n"
        f"pattern_type: {d.pattern_type}\n"
        f"occurrences: {d.occurrence_count} (support {d.support_count}, "
        f"contradiction {d.contradiction_count}) across {d.project_count} project(s)\n"
        f"repair_context: {d.repair_context}\n"
        f"resolution: {d.resolution_family}\n"
        f"error_signature: {d.error_signature}\n"
        f"contexts: {', '.join(d.contexts)}\n"
    )


def get_summarizer(cfg: Any) -> Summarizer:
    """Pick a summarizer from config. Deterministic unless LLM is explicitly on
    *and* a transport is available (it is not, in Stage 5)."""

    if getattr(cfg, "use_llm_summary", False):
        return LlmSummarizer(transport=None)  # falls back deterministically
    return DeterministicSummarizer()
