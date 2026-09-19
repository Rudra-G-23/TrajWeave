"""Small deterministic constants describing the Stage 9 state machine.

Kept separate from ``service.py`` so the allowed transitions and reason codes
are easy to find, review, and lock down with tests independently of the
orchestration logic that uses them.
"""

from __future__ import annotations

POLICY_STATUSES = ("active", "disabled", "pruned")
VERSION_STATUSES = ("active", "superseded", "rolled_back")

# "Broader" scope is listed after "narrower" scope. Promote moves right,
# demote moves left. `skill` is an on-demand alternative to `scoped_rule`,
# not a strictly broader/narrower point on this same line - it is reachable
# only as an explicit demote target from project_rule/scoped_rule and an
# explicit promote source back to scoped_rule/project_rule.
PLACEMENT_ORDER = ("scoped_rule", "project_rule", "global_rule")

# {current_placement_type: {allowed target placement types}}
PROMOTE_TARGETS: dict[str, tuple[str, ...]] = {
    "skill": ("scoped_rule", "project_rule"),
    "scoped_rule": ("project_rule",),
    "project_rule": ("global_rule",),
    "global_rule": (),
}

DEMOTE_TARGETS: dict[str, tuple[str, ...]] = {
    "global_rule": ("project_rule",),
    "project_rule": ("scoped_rule", "skill"),
    "scoped_rule": ("skill",),
    "skill": (),
}

REASON_CODES = (
    "CONSISTENT_CROSS_SCOPE_BENEFIT",
    "REPEATED_REGRESSION",
    "HIGH_WRONG_SCOPE_COST",
    "DUPLICATE_POLICY",
    "STALE_TARGET",
    "NEW_VERSION_REGRESSION",
    "INSUFFICIENT_EVIDENCE",
)

RECOMMENDATION_OPERATIONS = (
    "retain", "promote", "demote", "rewrite", "merge", "split", "disable", "prune", "rollback",
)
