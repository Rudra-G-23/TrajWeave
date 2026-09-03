from __future__ import annotations

from trajweave.experience.detect import detect_occurrences
from trajweave.experience.models import (
    PATTERN_FAILURE_REPAIR,
    PATTERN_FILE_CHANGE,
    PATTERN_HUMAN_CORRECTION_REPAIR,
    PATTERN_REPEATED_FAILURE,
)


def _ev(seq, type, **kw):
    row = {"sequence": seq, "type": type, "path": None, "command": None,
           "exit_code": None, "summary": None, "tool_name": None, "metadata": None}
    row.update(kw)
    return row


def _traj(events, *, id="TW-1", project_id="p1", final_status="success"):
    return dict(id=id, project_id=project_id, final_status=final_status), events


def test_failure_repair_success_basic():
    t, evs = _traj([
        _ev(1, "user_prompt", summary="change schema"),
        _ev(2, "file_edit", path="app/models/user.py"),
        _ev(3, "test_fail", summary="E assert 1 == 2"),
        _ev(4, "file_create", path="migrations/0007_x.py"),
        _ev(5, "test_pass"),
    ])
    occs = detect_occurrences(t, evs)
    fr = [o for o in occs if o.pattern_type == PATTERN_FAILURE_REPAIR]
    assert len(fr) == 1
    o = fr[0]
    assert o.failure_family == "test" and o.resolution_family == "test"
    assert o.repair_context == "migration"
    assert o.group_key == "fr::migration::test"
    assert o.start_sequence == 3 and o.end_sequence == 5
    assert o.features["antecedent_contexts"] == ["model"]
    # the redundant proactive file_change_pattern for the same group is suppressed
    assert not [
        o for o in occs
        if o.pattern_type == PATTERN_FILE_CHANGE and o.group_key == "fr::migration::test"
    ]


def test_failure_then_pass_with_no_edits_is_not_a_repair():
    t, evs = _traj([
        _ev(1, "test_fail", summary="flaky"),
        _ev(2, "test_pass"),
    ])
    assert detect_occurrences(t, evs) == []


def test_repair_outside_window_is_ignored():
    events = [_ev(1, "test_fail", summary="boom")]
    events += [_ev(i, "assistant_message") for i in range(2, 30)]
    events += [_ev(30, "file_edit", path="app/x.py"), _ev(31, "test_pass")]
    t, evs = _traj(events)
    assert [o for o in detect_occurrences(t, evs) if o.pattern_type == PATTERN_FAILURE_REPAIR] == []


def test_human_correction_repair_only_on_meaningful_turns():
    base = [
        _ev(1, "user_prompt", summary="do the thing"),
        _ev(2, "assistant_message"),
    ]
    trivial = base + [
        _ev(3, "human_correction", summary="yes"),
        _ev(4, "file_edit", path="app/api/user.py"),
        _ev(5, "test_pass"),
    ]
    t, evs = _traj(trivial)
    assert [o for o in detect_occurrences(t, evs)
            if o.pattern_type == PATTERN_HUMAN_CORRECTION_REPAIR] == []

    # a directive correction, then a failing check that the follow-up turns green
    real = base + [
        _ev(3, "human_correction", summary="don't edit the lockfile, run npm install"),
        _ev(4, "test_fail", summary="E   ModuleNotFoundError: left-pad"),
        _ev(5, "command", command="npm install"),
        _ev(6, "file_edit", path="app/api/user.py"),
        _ev(7, "test_pass"),
    ]
    t, evs = _traj(real)
    hc = [o for o in detect_occurrences(t, evs)
          if o.pattern_type == PATTERN_HUMAN_CORRECTION_REPAIR]
    assert len(hc) == 1
    assert hc[0].resolution_family == "test"
    assert hc[0].start_sequence == 3

    # same shape but a bare acknowledgement instead of a directive -> nothing
    ack = base + [
        _ev(3, "human_correction", summary="yes do that"),
        _ev(4, "test_fail", summary="E   ModuleNotFoundError: left-pad"),
        _ev(5, "command", command="npm install"),
        _ev(6, "file_edit", path="app/api/user.py"),
        _ev(7, "test_pass"),
    ]
    t, evs = _traj(ack)
    assert [o for o in detect_occurrences(t, evs)
            if o.pattern_type == PATTERN_HUMAN_CORRECTION_REPAIR] == []


def test_repeated_failure_terminal_signature():
    t, evs = _traj([
        _ev(1, "build_fail", summary="ld: symbol not found for _foo in libbar"),
        _ev(2, "file_edit", path="src/bar.c"),
        _ev(3, "build_fail", summary="ld: symbol not found for _foo in libbar"),
    ], final_status="aborted")
    rf = [o for o in detect_occurrences(t, evs) if o.pattern_type == PATTERN_REPEATED_FAILURE]
    assert len(rf) == 1
    assert rf[0].error_signature and "symbol not found" in rf[0].error_signature
    assert rf[0].group_key.startswith("rf::")


def test_repeated_failure_skipped_when_resolved_and_status_ok():
    t, evs = _traj([
        _ev(1, "test_fail", summary="ImportError: cannot import name frobnicate from mod"),
        _ev(2, "file_edit", path="mod.py"),
        _ev(3, "test_pass"),
    ], final_status="success")
    assert [o for o in detect_occurrences(t, evs)
            if o.pattern_type == PATTERN_REPEATED_FAILURE] == []


def test_file_change_pattern_model_then_migration():
    t, evs = _traj([
        _ev(1, "file_edit", path="app/models/order.py"),
        _ev(2, "file_create", path="migrations/0009_order.py"),
        _ev(3, "test_pass"),
    ])
    fc = [o for o in detect_occurrences(t, evs) if o.pattern_type == PATTERN_FILE_CHANGE]
    assert len(fc) == 1
    assert fc[0].group_key == "fr::migration::test"
    assert fc[0].features["from_context"] == "model"


def test_vendored_edits_do_not_count_as_repair_context():
    t, evs = _traj([
        _ev(1, "test_fail", summary="boom"),
        _ev(2, "file_edit", path="node_modules/pkg/index.js"),
        _ev(3, "test_pass"),
    ])
    assert [o for o in detect_occurrences(t, evs)
            if o.pattern_type == PATTERN_FAILURE_REPAIR] == []


def test_empty_events():
    assert detect_occurrences({"id": "TW-1", "project_id": None, "final_status": "unknown"}, []) == []
