"""Deterministic pattern-occurrence detection over one normalized trajectory.

No LLM, no network, no randomness. Given a trajectory row + its ordered events,
emit zero or more :class:`Occurrence` records. Ordering is authoritative (the
transcripts are append-only logs); timestamps are only used for recency.

Supported pattern types (Stage 5 brief 17, a conservative starting set):

* ``failure_repair_success``     - ``*_fail`` ... edits ... ``*_pass`` (same family)
* ``human_correction_repair``    - a *meaningful* correction, then a good resolution
* ``repeated_failure``           - an error whose normalized signature is terminal here
* ``file_change_pattern``        - change in context A directly followed by context B

Contradiction evidence is **not** produced here - it needs the aggregated group
(see :mod:`trajweave.experience.grouping`).
"""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass
from typing import Any, Iterable

from trajweave.experience.config import ExperienceConfig
from trajweave.experience.context import command_context, file_context, is_vendored
from trajweave.experience.corrections import _DIRECTIVE_WORDS, _TOKEN_RE, is_meaningful_human_correction
from trajweave.experience.models import (
    PATTERN_FAILURE_REPAIR,
    PATTERN_FILE_CHANGE,
    PATTERN_HUMAN_CORRECTION_REPAIR,
    PATTERN_REPEATED_FAILURE,
    AMBIGUOUS,
    SUPPORT,
    Occurrence,
)
from trajweave.experience.signatures import error_signature

_FAIL_TO_FAMILY = {"test_fail": "test", "lint_fail": "lint", "build_fail": "build"}
_PASS_TO_FAMILY = {"test_pass": "test", "lint_pass": "lint", "build_pass": "build"}
_EDIT_TYPES = frozenset({"file_edit", "file_create", "file_delete"})
_CHANGE_TYPES = frozenset({"file_edit", "file_create"})

# file_change_pattern only fires when the *second* change lands in one of these
# "did you also update ..." contexts - keeps precision high.
_FILE_CHANGE_SINKS = frozenset({"migration", "tests", "build", "config"})

# repeated_failure: ignore signatures that carry no diagnostic value. A
# ``repeated_failure`` experience claims "this exact error keeps coming back
# unresolved" - it is only useful if the signature actually names the error.
_GENERIC_SIGNATURES = frozenset(
    {
        "", "error", "errors", "failed", "failure", "failures", "command failed",
        "exit n", "exit code n", "n failed", "n passed", "n error", "n errors",
        "assertionerror", "assertion failed", "traceback", "non-zero exit",
        "process completed with exit code n", "process exited with code n",
        "tests failed", "test failed", "lint failed", "build failed",
        "check failed", "api error message", "an error occurred",
        "something went wrong", "unknown error", "internal error",
    }
)

# ... and reject whole families of generic templates (``test exit n``,
# ``lint exited n``, ``N passed, N failed``, ``step failed with code n`` ...).
_GENERIC_SIGNATURE_RE = re.compile(
    r"^(?:"
    r"(?:the )?(?:test|tests|lint|linter|build|type ?check|command|process|step|job|task|"
    r"script|npm|yarn|pnpm|pytest|make|cargo|go|check)s?"
    r"(?: run| suite)?"
    r"(?: (?:has |have )?(?:exit(?:ed|s)?|fail(?:ed|s|ing)?|return(?:ed)?|"
    r"complet(?:ed|es)?|finish(?:ed|es)?|error(?:ed|s)?))+"
    r"(?: with)?(?: (?:code|status|a non-?zero (?:exit )?(?:code|status)))?"
    r"[ n:=-]*"
    r"|n (?:passed|failed|errors?|skipped|warnings?)(?:[ ,;n]+(?:passed|failed|errors?|skipped|warnings?))*"
    r"|exit(?:ed)?(?: code| status)? n"
    r")$"
)

# A signature only counts as diagnostic if it names *what* went wrong: an
# exception/error class, a quoted symbol, or a recognizable specific phrase.
_DIAGNOSTIC_TOKEN_RE = re.compile(
    r"[a-z_][a-z_.]*(?:error|exception|warning|fault|denied|refused|timeout|"
    r"notfound|nomethod|keyerror)"
    r"|['\"`][^'\"`]{3,}['\"`]"
    r"|\b(?:no such (?:file|module|table|column)|cannot find|can'?t find|"
    r"command not found|not (?:found|defined|callable|a function|iterable|"
    r"serializable|installed|recognized|permitted|allowed|available)|"
    r"could not (?:find|acquire|open|resolve|load|connect|create|import)|"
    r"unable to (?:find|open|resolve|load|locate|import|access)|"
    r"failed to (?:fetch|resolve|compile|build|connect|load|import|parse)|"
    r"undefined (?:reference|method|variable|name|symbol)|unresolved|"
    r"missing (?:module|dependency|argument|required|import|attribute)|"
    r"unexpected (?:token|keyword|indent|eof)|"
    r"permission denied|read-only file system|connection (?:refused|reset|timed out)|"
    r"module not found|cannot read propert|is not a function|"
    r"expected .* (?:but )?(?:got|received)|"
    r"segmentation fault|out of memory|stack overflow|"
    r"already exists|does not exist|foreign key|null value|"
    r"syntax|deprecat|incompatible|version mismatch)\b"
)


def _looks_diagnostic(sig: str) -> bool:
    """True only if ``sig`` names a specific, recurring error worth surfacing."""

    if not sig or len(sig) < 12 or sig in _GENERIC_SIGNATURES:
        return False
    if _GENERIC_SIGNATURE_RE.match(sig):
        return False
    return bool(_DIAGNOSTIC_TOKEN_RE.search(sig))


# The Claude/Codex adapters synthesize the failure-outcome event's summary as
# ``"<kind> exit <code>"`` and keep the real command output on the *preceding*
# ``command`` event (adapters/claude.py, adapters/codex.py). Recover it so
# repeated_failure can still name what broke.
_SYNTH_OUTCOME_RE = re.compile(r"^\s*\w[\w -]* exit -?\d+\s*$", re.I)


def _diagnostic_text(evs: list[_Ev], i: int) -> str | None:
    own = (evs[i].summary or "").strip()
    if own and not _SYNTH_OUTCOME_RE.match(own):
        return own
    for k in (i - 1, i - 2):
        if k < 0:
            break
        prev = evs[k]
        if prev.type in ("command", "tool_call") and (prev.summary or "").strip():
            return prev.summary.strip()
    return own or None


@dataclass
class _Ev:
    seq: int
    type: str
    path: str | None
    command: str | None
    exit_code: int | None
    summary: str | None
    context: str  # file_context(path) or "" for non-file events
    cmd_context: str | None


def _meta(raw: Any) -> dict:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            v = json.loads(raw)
            return v if isinstance(v, dict) else {}
        except (TypeError, ValueError):
            return {}
    return {}


def _view(events: Iterable[dict]) -> list[_Ev]:
    out: list[_Ev] = []
    for e in events:
        path = e.get("path")
        out.append(
            _Ev(
                seq=int(e.get("sequence") or 0),
                type=str(e.get("type") or "unknown"),
                path=path,
                command=e.get("command"),
                exit_code=e.get("exit_code"),
                summary=e.get("summary"),
                context=file_context(path) if path else "",
                cmd_context=command_context(e.get("command")),
            )
        )
    return out


def _dominant_context(contexts: list[str]) -> tuple[str, bool]:
    """Return ``(context, ambiguous)``. Empty -> ('other', True); tie -> ('mixed', True)."""

    real = [c for c in contexts if c and c != "other"]
    if not real:
        return "other", True
    counts = Counter(real).most_common()
    if len(counts) > 1 and counts[0][1] == counts[1][1]:
        return "mixed", True
    return counts[0][0], False


def _is_directive(summary: str | None) -> bool:
    """A correction that redirects (verb / negation / 'instead'), not one that
    merely elaborates or asks a question."""

    if not summary:
        return False
    toks = _TOKEN_RE.findall(summary.lower())
    return any(t in _DIRECTIVE_WORDS for t in toks)


def _antecedent_contexts(evs: list[_Ev], lo: int, hi: int) -> list[str]:
    seen: list[str] = []
    for ev in evs:
        if lo <= ev.seq < hi and ev.type in _CHANGE_TYPES and ev.path and not is_vendored(ev.path):
            if ev.context not in seen:
                seen.append(ev.context)
    return seen


def detect_occurrences(
    traj: dict, events: Iterable[dict], cfg: ExperienceConfig | None = None
) -> list[Occurrence]:
    cfg = (cfg or ExperienceConfig()).validated()
    evs = _view(events)
    if not evs:
        return []

    tid = str(traj["id"])
    pid = traj.get("project_id")
    final_status = str(traj.get("final_status") or "unknown")
    gap = cfg.max_event_gap

    found: list[Occurrence] = []
    # One occurrence per (group_key, classification) per trajectory: repeated
    # episodes of the same pattern inside one session are not *independent*
    # evidence, and counting them inflates confidence on chatty transcripts.
    seen: set[tuple[str, str]] = set()

    def emit(occs: list[Occurrence], *, skip_groups: set[str] | None = None) -> None:
        for occ in occs:
            if skip_groups is not None and occ.group_key in skip_groups:
                continue
            key = (occ.group_key, occ.classification)
            if key not in seen:
                seen.add(key)
                found.append(occ)

    emit(_failure_repair(evs, tid, pid, gap))
    emit(_human_correction_repair(evs, tid, pid, final_status, cfg))
    emit(_repeated_failure(evs, tid, pid, final_status))
    # A proactive "A -> B -> pass" is redundant when this same trajectory already
    # has an explicit failure->repair->pass for the same (repair_context, family).
    claimed = {o.group_key for o in found}
    emit(_file_change_pattern(evs, tid, pid, final_status, gap), skip_groups=claimed)
    return found


# ---------------------------------------------------------------------------
def _failure_repair(
    evs: list[_Ev], tid: str, pid: str | None, gap: int
) -> list[Occurrence]:
    out: list[Occurrence] = []
    n = len(evs)
    for i, ev in enumerate(evs):
        family = _FAIL_TO_FAMILY.get(ev.type)
        if family is None:
            continue
        want_pass = f"{family}_pass"
        j = None
        for k in range(i + 1, min(n, i + 1 + gap)):
            if evs[k].type == want_pass:
                j = k
                break
        if j is None:
            continue

        between = evs[i + 1 : j]
        edit_ctxs = [
            b.context for b in between
            if b.type in _EDIT_TYPES and b.path and not is_vendored(b.path)
        ]
        has_migration_cmd = any(b.cmd_context == "migration" for b in between)
        if not edit_ctxs and not has_migration_cmd:
            continue  # a pass that just happened again, no repair work in between

        if has_migration_cmd:
            repair_ctx, ambiguous = "migration", False
        else:
            repair_ctx, ambiguous = _dominant_context(edit_ctxs)

        ante = _antecedent_contexts(evs, max(0, ev.seq - gap), ev.seq)
        classification = AMBIGUOUS if ambiguous or repair_ctx in ("other", "mixed") else SUPPORT
        out.append(
            Occurrence(
                trajectory_id=tid,
                project_id=pid,
                pattern_type=PATTERN_FAILURE_REPAIR,
                group_key=f"fr::{repair_ctx}::{family}",
                start_sequence=ev.seq,
                end_sequence=evs[j].seq,
                failure_family=family,
                resolution_family=family,
                repair_context=repair_ctx,
                classification=classification,
                features={
                    "failure_signature": error_signature(_diagnostic_text(evs, i)),
                    "repair_edit_contexts": sorted(set(edit_ctxs)),
                    "antecedent_contexts": ante,
                    "migration_command": has_migration_cmd,
                    "file_changes_between": len(
                        [b for b in between if b.type in _EDIT_TYPES]
                    ),
                },
            )
        )
    return out


def _human_correction_repair(
    evs: list[_Ev], tid: str, pid: str | None, final_status: str, cfg: ExperienceConfig
) -> list[Occurrence]:
    """A *meaningful, directive* correction that is followed by a failing check
    turning green - i.e. the user's redirection demonstrably fixed something.

    Deliberately strict: a long user turn that merely precedes eventual success
    is not evidence of a reusable lesson (that describes almost every multi-turn
    session). We require (1) the correction to redirect, not just elaborate, and
    (2) a ``*_fail`` -> ``*_pass`` of the same family inside the window after it.
    """

    out: list[Occurrence] = []
    n = len(evs)
    gap = cfg.max_event_gap
    for i, ev in enumerate(evs):
        if ev.type != "human_correction":
            continue
        if not is_meaningful_human_correction(
            ev.summary, min_tokens=cfg.meaningful_correction_min_tokens
        ):
            continue
        if not _is_directive(ev.summary):
            continue

        window = evs[i + 1 : min(n, i + 1 + gap)]
        fail_off = next(
            (o for o, w in enumerate(window) if w.type in _FAIL_TO_FAMILY), None
        )
        if fail_off is None:
            continue
        fail_family = _FAIL_TO_FAMILY[window[fail_off].type]
        res_idx = next(
            (
                o for o, w in enumerate(window)
                if o > fail_off and w.type == f"{fail_family}_pass"
            ),
            None,
        )
        if res_idx is None:
            continue
        res_family = fail_family

        end = window[res_idx]
        between = window[fail_off + 1 : res_idx + 1]
        edits = [
            w for w in between
            if w.type in _EDIT_TYPES and w.path and not is_vendored(w.path)
        ]
        cmds = [w for w in between if w.type == "command"]
        if not edits and not cmds:
            continue

        if edits:
            repair_ctx, ambiguous = _dominant_context([w.context for w in edits])
        elif any(w.cmd_context == "migration" for w in cmds):
            repair_ctx, ambiguous = "migration", False
        else:
            repair_ctx, ambiguous = "other", True

        classification = AMBIGUOUS if ambiguous or repair_ctx in ("other", "mixed") else SUPPORT
        out.append(
            Occurrence(
                trajectory_id=tid,
                project_id=pid,
                pattern_type=PATTERN_HUMAN_CORRECTION_REPAIR,
                group_key=f"hc::{repair_ctx}::{res_family or '-'}",
                start_sequence=ev.seq,
                end_sequence=end.seq,
                failure_family=fail_family,
                resolution_family=res_family,
                repair_context=repair_ctx,
                classification=classification,
                features={
                    "correction_summary": (ev.summary or "")[:280],
                    "repair_edit_contexts": sorted({w.context for w in edits}),
                    "resolution": res_family,
                },
            )
        )
    return out


def _repeated_failure(
    evs: list[_Ev], tid: str, pid: str | None, final_status: str
) -> list[Occurrence]:
    fail_types = {"test_fail", "lint_fail", "build_fail", "error"}
    by_sig: dict[str, list[_Ev]] = {}
    for i, ev in enumerate(evs):
        if ev.type not in fail_types:
            continue
        # Only a real diagnostic message qualifies - a bare command line or an
        # exit-code blob tells us nothing reusable about *which* error recurs.
        sig = error_signature(_diagnostic_text(evs, i))
        if not _looks_diagnostic(sig):
            continue
        by_sig.setdefault(sig, []).append(ev)

    out: list[Occurrence] = []
    for sig, hits in by_sig.items():
        first, last = hits[0], hits[-1]
        family = _FAIL_TO_FAMILY.get(first.type)
        resolved_later = False
        if family:
            wanted = f"{family}_pass"
            resolved_later = any(
                e.type == wanted and e.seq > last.seq for e in evs
            )
        bad_ending = final_status in ("partial", "aborted", "failure")
        terminal = (not resolved_later) or bad_ending
        if not terminal:
            continue
        recurs_here = len(hits) >= 2
        # A lone hit in a session that still finished OK is only *ambiguous*
        # evidence that this signature is a recurring problem; it needs another
        # trajectory (or a bad ending / in-session recurrence) to count as
        # support.
        ambiguous = not recurs_here and not bad_ending
        out.append(
            Occurrence(
                trajectory_id=tid,
                project_id=pid,
                pattern_type=PATTERN_REPEATED_FAILURE,
                group_key=f"rf::{sig}",
                start_sequence=first.seq,
                end_sequence=last.seq,
                failure_family=family,
                resolution_family=None,
                repair_context=None,
                error_signature=sig,
                classification=AMBIGUOUS if ambiguous else SUPPORT,
                features={
                    "occurrences_in_trajectory": len(hits),
                    "resolved_later": resolved_later,
                    "final_status": final_status,
                },
            )
        )
    return out


#: Ordered (from -> to) context transitions that carry a real "did you also
#: update ..." lesson. Kept tight on purpose: broader pairs (backend->tests,
#: frontend->config, ...) fire on almost every long successful session and
#: drown the signal. The mechanism generalizes; Stage 5 ships the schema pair.
_FILE_CHANGE_TRANSITIONS = frozenset(
    {("model", "migration"), ("model", "database"), ("database", "migration")}
)


def _file_change_pattern(
    evs: list[_Ev], tid: str, pid: str | None, final_status: str, gap: int
) -> list[Occurrence]:
    if final_status != "success":
        return []
    changes = [
        e for e in evs
        if e.type in _CHANGE_TYPES and e.path and not is_vendored(e.path)
        and e.context in ("model", "database", "migration")
    ]
    out: list[Occurrence] = []
    seen_pairs: set[tuple[str, str]] = set()
    for idx, a in enumerate(changes):
        for b in changes[idx + 1 :]:
            if b.seq - a.seq > gap:
                break
            pair = (a.context, b.context)
            if pair not in _FILE_CHANGE_TRANSITIONS or pair in seen_pairs:
                continue

            res_family = next(
                (
                    _PASS_TO_FAMILY[e.type] for e in evs
                    if b.seq < e.seq <= b.seq + gap and e.type in _PASS_TO_FAMILY
                ),
                None,
            )
            if res_family is None:
                continue  # need an actual check turning green after the change
            seen_pairs.add(pair)
            out.append(
                Occurrence(
                    trajectory_id=tid,
                    project_id=pid,
                    pattern_type=PATTERN_FILE_CHANGE,
                    # same namespace as failure_repair so a proactive
                    # "model -> migration -> tests pass" clusters with the
                    # "model, tests fail, fix migration, tests pass" repairs.
                    group_key=f"fr::{b.context}::{res_family}",
                    start_sequence=a.seq,
                    end_sequence=b.seq,
                    failure_family=None,
                    resolution_family=res_family,
                    repair_context=b.context,
                    classification=SUPPORT,
                    features={
                        "from_context": a.context,
                        "to_context": b.context,
                        "antecedent_contexts": [a.context],
                        "resolution": res_family,
                    },
                )
            )
            break
    return out
