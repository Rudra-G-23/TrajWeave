"""Pure, deterministic Stage 6 placement feature extraction and ranking.

``build_proposals(experience, evidence, events_by_trajectory)`` is the public
integration seam.  All inputs are Stage 5 data represented as dictionaries and
all outputs are JSON-serializable dictionaries.  The engine neither reads nor
writes a database or a user repository.

Scoring is intentionally arithmetic, not semantic classification.  Each
alternative starts with its documented weighted sum below, is clipped to
``[0, 1]``, and then ranked by score followed by the fixed safety-first type
order.  Global has an explicit evidence gate: at least two projects, each with
two support occurrences, and no project owning more than 75 percent of support.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from typing import Any

from trajweave.experience.models import (
    PATTERN_FAILURE_REPAIR,
    PATTERN_FILE_CHANGE,
    PATTERN_HUMAN_CORRECTION_REPAIR,
    PATTERN_REPEATED_FAILURE,
)
from trajweave.placement.models import PlacementFeatures, PlacementType, Proposal, Scope
from trajweave.placement.scope import infer_scope

ENGINE_VERSION = "stage6-deterministic-v1"

_TIE_ORDER = {
    PlacementType.IGNORE: 0,
    PlacementType.GLOBAL_RULE: 1,
    PlacementType.PROJECT_RULE: 2,
    PlacementType.SCOPED_RULE: 3,
    PlacementType.SKILL: 4,
}
_ABSOLUTE_TEXT_PATH = re.compile(
    r"(?:(?:[A-Za-z]:[\\/])|(?:~[\\/])|(?<![A-Za-z0-9_.-])/)[^\s,;:()\[\]{}<>\"']+"
)


def _json_object(value: object) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return decoded if isinstance(decoded, dict) else {}
    return {}


def _relationship(record: dict[str, Any]) -> str:
    return str(record.get("relationship") or record.get("classification") or "support")


def _bounded(value: object, default: float = 0.0) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return default


def _confidence_components(experience: dict[str, Any]) -> tuple[float, float]:
    confidence = _bounded(experience.get("confidence"))
    blob = _json_object(experience.get("confidence_json"))
    components = _json_object(blob.get("components"))
    if confidence == 0.0 and "score" in blob:
        confidence = _bounded(blob.get("score"))
    return confidence, _bounded(components.get("recency"), default=0.3)


def _actionability(pattern_type: object, support_count: int) -> float:
    if support_count < 2:
        return 0.25
    if pattern_type == PATTERN_REPEATED_FAILURE:
        return 0.35
    if pattern_type in {
        PATTERN_FAILURE_REPAIR, PATTERN_FILE_CHANGE, PATTERN_HUMAN_CORRECTION_REPAIR,
    }:
        return 1.0
    return 0.50


def _procedure_complexity(
    support: list[dict[str, Any]], events_by_trajectory: dict[str, list[dict[str, Any]]]
) -> float:
    """Derive multi-step complexity from ordered stored event operations only."""

    if not support:
        return 0.0
    multi_step = 0
    for occurrence in support:
        tid = occurrence.get("trajectory_id")
        try:
            start, end = int(occurrence.get("start_sequence")), int(occurrence.get("end_sequence"))
        except (TypeError, ValueError):
            start, end = -1, -1
        operations: list[tuple[int, str]] = []
        if isinstance(tid, str):
            for event in events_by_trajectory.get(tid, []):
                try:
                    sequence = int(event.get("sequence"))
                except (TypeError, ValueError):
                    continue
                if not (start <= sequence <= end):
                    continue
                # A distinct operation is a stored tool event with a path,
                # command, or explicit tool name.  Raw contents never matter.
                if event.get("path") or event.get("command") or event.get("tool_name"):
                    operations.append((sequence, str(event.get("type") or event.get("tool_name") or "operation")))
        if len({sequence for sequence, _ in operations}) >= 3 and len({kind for _, kind in operations}) >= 2:
            multi_step += 1
    # Repetition is required: one elaborate trajectory cannot create a Skill.
    return min(1.0, multi_step / 2.0)


def extract_features(
    experience: dict[str, Any],
    evidence: list[dict[str, Any]],
    events_by_trajectory: dict[str, list[dict[str, Any]]],
) -> tuple[PlacementFeatures, tuple[str, ...]]:
    """Extract the complete persisted score input snapshot from Stage 5 facts."""

    support = [item for item in evidence if _relationship(item) == "support"]
    contradiction = [item for item in evidence if _relationship(item) == "contradiction"]
    ambiguous = [item for item in evidence if _relationship(item) == "ambiguous"]
    # Older Stage 5 databases can lack evidence rows for a partial experience.
    # This deliberately stays conservative rather than trusting aggregate counts.
    support_count, contradiction_count, ambiguous_count = map(len, (support, contradiction, ambiguous))
    decisive = support_count + contradiction_count
    support_ratio = support_count / decisive if decisive else 0.0
    recurrence = min(1.0, (support_count + ambiguous_count) / 6.0)
    trajectory_count = len({item.get("trajectory_id") for item in support if item.get("trajectory_id")})
    projects = Counter(str(item["project_id"]) for item in support if item.get("project_id"))
    project_count = len(projects)
    project_concentration = (max(projects.values()) / support_count) if projects and support_count else 0.0
    dominant_project_id = (
        sorted(projects.items(), key=lambda item: (-item[1], item[0]))[0][0]
        if projects else None
    )
    cross_project_support = min(1.0, max(0, project_count - 1) / 2.0)

    contexts = Counter(
        str(item["repair_context"]) for item in support
        if item.get("repair_context") not in (None, "", "other", "mixed")
    )
    context_consistency = (max(contexts.values()) / support_count) if contexts and support_count else 0.0
    scope = infer_scope(support, events_by_trajectory)
    confidence, recency = _confidence_components(experience)
    global_eligible = (
        project_count >= 2
        and min(projects.values(), default=0) >= 2
        and project_concentration <= 0.75
    )
    feature = PlacementFeatures(
        support_count=support_count,
        contradiction_count=contradiction_count,
        ambiguous_count=ambiguous_count,
        trajectory_count=trajectory_count,
        project_count=project_count,
        dominant_project_id=dominant_project_id,
        support_ratio=support_ratio,
        recurrence=recurrence,
        cross_project_support=cross_project_support,
        project_concentration=project_concentration,
        context_consistency=context_consistency,
        actionability=_actionability(experience.get("pattern_type"), support_count),
        procedural_complexity=_procedure_complexity(support, events_by_trajectory),
        confidence=confidence,
        recency=recency,
        scope_type=scope.scope_type,
        scope_value=scope.scope_value,
        scope_support_count=scope.support_count,
        scope_concentration=scope.concentration,
        scope_specificity=scope.specificity,
        global_eligible=global_eligible,
    )
    occurrence_ids = tuple(sorted(
        str(item["occurrence_id"] if item.get("occurrence_id") else item["id"])
        for item in evidence if item.get("occurrence_id") or item.get("id")
    ))
    return feature, occurrence_ids


def _clip(value: float) -> float:
    return round(max(0.0, min(1.0, value)), 4)


def _score(placement_type: PlacementType, f: PlacementFeatures) -> float:
    contradiction_rate = f.contradiction_count / max(1, f.support_count + f.contradiction_count)
    scope_signal = f.scope_concentration * f.scope_specificity
    if placement_type is PlacementType.IGNORE:
        return _clip(
            0.10 + 0.35 * (1 - f.support_ratio) + 0.40 * contradiction_rate
            + 0.15 * (1 - f.actionability) + 0.10 * (1 - f.confidence)
            + (0.10 if f.support_count < 2 else 0.0)
        )
    if placement_type is PlacementType.GLOBAL_RULE:
        raw = (
            0.05 + 0.24 * f.support_ratio + 0.22 * f.recurrence
            + 0.35 * f.cross_project_support + 0.14 * f.actionability
            + 0.10 * f.confidence - 0.10 * f.project_concentration
            - 0.15 * scope_signal - 0.20 * contradiction_rate
        )
        return _clip(raw if f.global_eligible else min(raw, 0.24))
    if placement_type is PlacementType.PROJECT_RULE:
        return _clip(
            0.10 + 0.20 * f.support_ratio + 0.20 * f.recurrence
            + 0.25 * f.project_concentration + 0.15 * f.actionability
            + 0.10 * f.confidence - 0.12 * scope_signal
            - 0.25 * f.cross_project_support - 0.40 * contradiction_rate
        )
    if placement_type is PlacementType.SCOPED_RULE:
        return _clip(
            0.05 + 0.16 * f.support_ratio + 0.10 * f.recurrence
            + 0.40 * scope_signal + 0.12 * f.actionability + 0.07 * f.confidence
            + 0.05 * f.context_consistency - 0.35 * contradiction_rate
        )
    if placement_type is PlacementType.SKILL:
        return _clip(
            0.03 + 0.15 * f.support_ratio + 0.10 * f.recurrence
            + 0.50 * f.procedural_complexity + 0.12 * f.actionability
            + 0.08 * f.confidence - 0.10 * contradiction_rate
        )
    raise ValueError(f"unknown placement type: {placement_type}")  # pragma: no cover


def _safe_content(experience: dict[str, Any]) -> str:
    """Use concise Stage 5 knowledge while removing private absolute paths."""

    raw = experience.get("reusable_lesson") or experience.get("summary") or experience.get("title") or ""
    text = " ".join(str(raw).replace("\x00", " ").split())
    text = _ABSOLUTE_TEXT_PATH.sub("[repository path omitted]", text)
    # This is canonical knowledge, not an unbounded transcript or terminal log.
    return text[:500].rstrip()


def _diagnostics(placement_type: PlacementType, f: PlacementFeatures) -> tuple[dict[str, Any], ...]:
    """One machine-readable diagnostic list, rendered directly by CLI/UI later."""

    rows: list[dict[str, Any]] = []

    def add(code: str, polarity: str, message: str, **values: Any) -> None:
        rows.append({"code": code, "polarity": polarity, "message": message, "values": values})

    add("support", "+" if f.support_count else "-", f"{f.support_count} supporting occurrence(s)", count=f.support_count)
    if f.contradiction_count:
        add("contradictions", "-", f"{f.contradiction_count} contradicting occurrence(s)", count=f.contradiction_count)
    if placement_type is PlacementType.GLOBAL_RULE:
        if f.global_eligible:
            add("global_gate", "+", f"balanced repeated support across {f.project_count} projects", projects=f.project_count)
        else:
            add("global_gate", "-", "insufficient balanced repeated independent-project support", projects=f.project_count)
        if f.scope_value:
            add("specific_scope", "-", f"evidence concentrates in {f.scope_type}: {f.scope_value}", scope_type=f.scope_type, scope_value=f.scope_value)
    elif placement_type is PlacementType.PROJECT_RULE:
        add("project_concentration", "+" if f.project_concentration >= 0.75 else "-", f"largest project supplies {f.project_concentration:.0%} of support", concentration=round(f.project_concentration, 4))
        if f.scope_value:
            add("scope_alternative", "-", f"a repeated {f.scope_type} scope is also available", scope_type=f.scope_type, scope_value=f.scope_value)
    elif placement_type is PlacementType.SCOPED_RULE:
        if f.scope_value:
            add("scope_evidence", "+", f"{f.scope_support_count} supports concentrate in {f.scope_type}: {f.scope_value}", scope_type=f.scope_type, scope_value=f.scope_value, concentration=round(f.scope_concentration, 4))
        else:
            add("scope_evidence", "-", "no repeated evidence-backed technical scope", scope_type="global")
    elif placement_type is PlacementType.SKILL:
        if f.procedural_complexity >= 0.5:
            add("procedure", "+", "repeated evidence contains multi-operation ordered workflows", complexity=round(f.procedural_complexity, 4))
        else:
            add("procedure", "-", "evidence does not show repeated multi-operation workflows", complexity=round(f.procedural_complexity, 4))
    else:
        if f.support_ratio < 0.75:
            add("weak_or_mixed", "+", "mixed or weak support raises defer preference", support_ratio=round(f.support_ratio, 4))
        if f.actionability < 0.5:
            add("actionability", "+", "pattern is not yet a concrete reusable instruction", actionability=round(f.actionability, 4))
        if f.support_ratio >= 0.75 and f.actionability >= 0.5:
            add("actionability", "-", "evidence is actionable enough for a placement alternative", actionability=round(f.actionability, 4))
    return tuple(rows)


def _scope_for(placement_type: PlacementType, inferred: PlacementFeatures) -> Scope:
    if placement_type is PlacementType.GLOBAL_RULE:
        return Scope("global", None)
    if placement_type is PlacementType.PROJECT_RULE:
        # A project identifier is intentionally not repeated into generated
        # policy text.  Persistence associates the proposal to its Experience.
        return Scope("project", inferred.dominant_project_id)
    if placement_type is PlacementType.SCOPED_RULE:
        return Scope(inferred.scope_type, inferred.scope_value, inferred.scope_support_count, inferred.scope_concentration, inferred.scope_specificity)
    return Scope("global", None)


def build_proposals(
    experience: dict[str, Any],
    evidence: list[dict[str, Any]],
    events_by_trajectory: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """Build all five ranked placement alternatives for one Stage 5 Experience.

    Args:
        experience: ``Repository.get_experience`` row converted to ``dict``.
        evidence: joined evidence rows, normally
            ``Repository.get_experience_evidence`` converted to ``dict``.
        events_by_trajectory: event rows keyed by trajectory id.  Only events
            inside each evidence occurrence's sequence range affect scope and
            procedural-complexity features.

    Returns:
        Five JSON-serializable alternatives with ``placement_type``, canonical
        content, scope, score, rank, diagnostics, feature snapshot, and every
        linked occurrence id.  The caller persists the result as a proposal set.
    """

    features, occurrence_ids = extract_features(experience, evidence, events_by_trajectory)
    content = _safe_content(experience)
    unranked = [
        Proposal(
            placement_type=placement_type,
            scope=_scope_for(placement_type, features),
            proposed_content=content,
            score=_score(placement_type, features),
            rank=0,
            diagnostics=_diagnostics(placement_type, features),
            features=features,
            evidence_occurrence_ids=occurrence_ids,
        )
        for placement_type in PlacementType
    ]
    ordered = sorted(unranked, key=lambda item: (-item.score, _TIE_ORDER[item.placement_type]))
    return [
        Proposal(
            placement_type=item.placement_type,
            scope=item.scope,
            proposed_content=item.proposed_content,
            score=item.score,
            rank=index,
            diagnostics=item.diagnostics,
            features=item.features,
            evidence_occurrence_ids=item.evidence_occurrence_ids,
        ).as_dict()
        for index, item in enumerate(ordered, start=1)
    ]
