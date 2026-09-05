"""Cross-trajectory aggregation: occurrences -> candidate experiences.

Grouping is deterministic and interpretable (Stage 5 brief 21): occurrences are
bucketed by their ``group_key`` (already computed at detection time from
pattern-type + repair-context + resolution-family, or the error signature).

For ``fr::`` clusters (a repair-context change before a check passed) we also
*synthesize* contradiction evidence: trajectories that made the same kind of
antecedent change, reached the same check cleanly, and never touched the repair
context. Everything uncertain is left ``ambiguous`` rather than forced.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from trajweave.experience.confidence import compute_confidence
from trajweave.experience.config import ExperienceConfig
from trajweave.experience.models import (
    PATTERN_FAILURE_REPAIR,
    PATTERN_RANK,
    AMBIGUOUS,
    CONTRADICTION,
    STATUS_CANDIDATE,
    STATUS_NEEDS_MORE,
    SUPPORT,
    ConfidenceBreakdown,
    Occurrence,
)
from trajweave.experience.summarize import DeterministicSummarizer, SummaryInput, Summary


@dataclass
class TrajectoryProfile:
    trajectory_id: str
    project_id: str | None
    final_status: str
    last_activity: str | None
    edit_contexts: set[str] = field(default_factory=set)
    clean_pass_families: set[str] = field(default_factory=set)
    fail_families: set[str] = field(default_factory=set)
    max_sequence: int = 0


@dataclass
class GroupedExperience:
    group_key: str
    pattern_type: str
    occurrences: list[Occurrence]
    support_count: int
    contradiction_count: int
    ambiguous_count: int
    occurrence_count: int
    project_count: int
    first_seen_at: str | None
    last_seen_at: str | None
    confidence: ConfidenceBreakdown
    status: str
    summary: Summary


def _parse_fr_key(group_key: str) -> tuple[str, str] | None:
    parts = group_key.split("::")
    if len(parts) == 3 and parts[0] == "fr":
        return parts[1], parts[2]
    return None


def _headline_pattern(occs: list[Occurrence]) -> str:
    present = {o.pattern_type for o in occs}
    for p in PATTERN_RANK:
        if p in present:
            return p
    return PATTERN_FAILURE_REPAIR


def _antecedent_union(occs: list[Occurrence]) -> set[str]:
    out: set[str] = set()
    for o in occs:
        for c in o.features.get("antecedent_contexts", []) or []:
            if c and c != "other":
                out.add(c)
        frm = o.features.get("from_context")
        if frm and frm != "other":
            out.add(frm)
    return out


def _synthesize_contradictions(
    group_key: str,
    positive: list[Occurrence],
    profiles: dict[str, TrajectoryProfile],
) -> list[Occurrence]:
    parsed = _parse_fr_key(group_key)
    if parsed is None:
        return []
    repair_ctx, res_family = parsed
    if res_family not in ("test", "lint", "build") or repair_ctx in ("other", "mixed", "-"):
        return []

    antecedents = _antecedent_union(positive)
    if not antecedents:
        return []
    positive_tids = {o.trajectory_id for o in positive}

    out: list[Occurrence] = []
    for p in profiles.values():
        if p.trajectory_id in positive_tids:
            continue
        if res_family not in p.clean_pass_families:
            continue
        if not (p.edit_contexts & antecedents):
            continue
        if repair_ctx in p.edit_contexts:
            continue
        out.append(
            Occurrence(
                trajectory_id=p.trajectory_id,
                project_id=p.project_id,
                pattern_type=PATTERN_FAILURE_REPAIR,
                group_key=group_key,
                start_sequence=1,
                end_sequence=max(1, p.max_sequence),
                failure_family=None,
                resolution_family=res_family,
                repair_context=repair_ctx,
                classification=CONTRADICTION,
                features={
                    "reason": (
                        f"changed {sorted(p.edit_contexts & antecedents)} and reached "
                        f"{res_family} success without touching {repair_ctx}"
                    ),
                },
            )
        )
    return out


def build_experiences(
    occurrences: list[Occurrence],
    profiles: dict[str, TrajectoryProfile],
    *,
    cfg: ExperienceConfig | None = None,
    summarizer=None,
    now: datetime | None = None,
) -> tuple[list[GroupedExperience], list[Occurrence]]:
    """Return ``(experiences, all_occurrences_incl_synthesized_contradictions)``."""

    cfg = (cfg or ExperienceConfig()).validated()
    summarizer = summarizer or DeterministicSummarizer()

    buckets: dict[str, list[Occurrence]] = {}
    for o in occurrences:
        buckets.setdefault(o.group_key, []).append(o)

    experiences: list[GroupedExperience] = []
    all_occ: list[Occurrence] = list(occurrences)

    for group_key, occs in buckets.items():
        positive = [o for o in occs if o.classification in (SUPPORT, AMBIGUOUS)]
        support = [o for o in occs if o.classification == SUPPORT]
        ambiguous = [o for o in occs if o.classification == AMBIGUOUS]
        if not support:
            continue  # only ambiguous evidence -> not an experience yet

        contradictions: list[Occurrence] = []
        if len(positive) >= max(2, cfg.min_occurrences - 1):
            contradictions = _synthesize_contradictions(group_key, positive, profiles)
            all_occ.extend(contradictions)

        evidence = positive + contradictions
        evidence_for = len(positive)
        if evidence_for < cfg.min_occurrences - 1:
            continue  # single occurrence -> occurrence only, no experience

        all_projects = {o.project_id for o in evidence if o.project_id}
        project_count = len(all_projects)
        # Recurrence and cross-project strength measure how well the pattern is
        # *supported*; contradictions only ever subtract, via support_ratio.
        # Per the confidence spec, cross-project breadth counts projects with
        # genuine SUPPORT evidence (ambiguous occurrences do not widen it).
        support_projects = len({o.project_id for o in support if o.project_id})

        stamps = sorted(
            s for s in (
                profiles[o.trajectory_id].last_activity
                for o in evidence
                if o.trajectory_id in profiles
            ) if s
        )
        first_seen = stamps[0] if stamps else None
        last_seen = stamps[-1] if stamps else None

        occurrence_count = len(evidence)
        conf = compute_confidence(
            support=len(support),
            contradiction=len(contradictions),
            ambiguous=len(ambiguous),
            occurrences=len(positive),
            projects=support_projects,
            last_seen_at=last_seen,
            now=now,
        )

        if evidence_for >= cfg.min_occurrences and project_count >= cfg.min_projects:
            status = STATUS_CANDIDATE
        else:
            status = STATUS_NEEDS_MORE

        headline = _headline_pattern(positive)
        contexts = sorted(
            {o.repair_context for o in positive if o.repair_context and o.repair_context != "other"}
            | _antecedent_union(positive)
        )
        sample_corr = next(
            (o.features.get("correction_summary") for o in positive if o.features.get("correction_summary")),
            None,
        )
        summary = summarizer.summarize(
            SummaryInput(
                group_key=group_key,
                pattern_type=headline,
                occurrence_count=occurrence_count,
                support_count=len(support),
                contradiction_count=len(contradictions),
                project_count=project_count,
                repair_context=positive[0].repair_context,
                resolution_family=positive[0].resolution_family,
                error_signature=positive[0].error_signature,
                contexts=contexts,
                sample_correction=sample_corr,
            )
        )

        experiences.append(
            GroupedExperience(
                group_key=group_key,
                pattern_type=headline,
                occurrences=evidence,
                support_count=len(support),
                contradiction_count=len(contradictions),
                ambiguous_count=len(ambiguous),
                occurrence_count=occurrence_count,
                project_count=project_count,
                first_seen_at=first_seen,
                last_seen_at=last_seen,
                confidence=conf,
                status=status,
                summary=summary,
            )
        )

    experiences.sort(key=lambda e: (-e.confidence.score, e.group_key))
    return experiences, all_occ
