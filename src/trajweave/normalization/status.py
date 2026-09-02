"""Infer a conservative :class:`FinalStatus` from a normalized event list.

Guiding rule from the spec: *do not invent success*. Only claim ``success`` when
there is reliable positive evidence (an explicit completion signal, or a passing
test/build as the last verifiable action). Otherwise prefer ``partial`` /
``unknown``.
"""

from __future__ import annotations

from trajweave.models.enums import EventType, FinalStatus
from trajweave.models.events import NormalizedEvent

_VERIFY_PASS = {EventType.TEST_PASS, EventType.LINT_PASS, EventType.BUILD_PASS}
_VERIFY_FAIL = {EventType.TEST_FAIL, EventType.LINT_FAIL, EventType.BUILD_FAIL}


def infer_final_status(
    events: list[NormalizedEvent],
    *,
    aborted: bool = False,
    explicit_completion: bool = False,
    completion_message: str | None = None,
) -> tuple[FinalStatus, str]:
    if aborted:
        return FinalStatus.ABORTED, "session was aborted/interrupted"

    # Last verifiable (test/lint/build) outcome, scanning from the end.
    last_verify: NormalizedEvent | None = None
    for event in reversed(events):
        if event.type in _VERIFY_PASS or event.type in _VERIFY_FAIL:
            last_verify = event
            break

    # Tail command failure (non test/lint/build) without any later recovery.
    trailing_cmd_fail = False
    for event in reversed(events):
        if event.type == EventType.COMMAND and event.exit_code not in (None, 0):
            trailing_cmd_fail = True
            break
        if event.type in (EventType.COMMAND,) and event.exit_code == 0:
            break
        if event.type in _VERIFY_PASS or event.type in _VERIFY_FAIL:
            break

    if last_verify is not None and last_verify.type in _VERIFY_FAIL:
        return FinalStatus.PARTIAL, f"last {last_verify.type} was failing"

    if last_verify is not None and last_verify.type in _VERIFY_PASS:
        if explicit_completion:
            return FinalStatus.SUCCESS, "explicit completion + passing verification"
        return FinalStatus.SUCCESS, f"last verification ({last_verify.type}) passed"

    if explicit_completion and (completion_message or "").strip():
        if trailing_cmd_fail:
            return FinalStatus.PARTIAL, "completion signalled but a trailing command failed"
        return FinalStatus.SUCCESS, "explicit completion signal with final answer"

    if trailing_cmd_fail:
        return FinalStatus.PARTIAL, "session ended on a failing command"

    return FinalStatus.UNKNOWN, "no reliable completion evidence"
