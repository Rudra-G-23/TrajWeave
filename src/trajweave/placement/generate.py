"""Repository orchestration for deterministic Stage 6 proposal generation.

The scoring engine remains pure.  This module is the deliberately small seam
that rehydrates the Stage 5 evidence it needs and persists its complete,
versioned output.  It never writes to an observed project repository.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from trajweave.placement.engine import ENGINE_VERSION, build_proposals
from trajweave.storage.repository import Repository


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _fingerprint(experience: dict[str, Any], evidence: list[dict[str, Any]], events: dict[str, list[dict[str, Any]]]) -> str:
    """Hash precisely the persisted values that can influence a proposal."""

    exp_fields = (
        "id", "summary", "reusable_lesson", "pattern_type", "confidence",
        "confidence_json", "support_count", "contradiction_count", "ambiguous_count",
        "occurrence_count", "project_count", "status", "review_status",
    )
    evidence_fields = (
        "occurrence_id", "trajectory_id", "project_id", "relationship", "classification",
        "pattern_type", "start_sequence", "end_sequence", "repair_context", "features_json",
    )
    event_fields = ("sequence", "type", "path", "command", "tool_name")
    snapshot = {
        "engine_version": ENGINE_VERSION,
        "experience": {key: experience.get(key) for key in exp_fields},
        "evidence": [
            {key: item.get(key) for key in evidence_fields}
            for item in sorted(evidence, key=lambda row: str(row.get("occurrence_id", "")))
        ],
        "events": {
            trajectory_id: [
                {key: event.get(key) for key in event_fields}
                for event in sorted(rows, key=lambda row: int(row.get("sequence") or 0))
            ]
            for trajectory_id, rows in sorted(events.items())
        },
    }
    return hashlib.sha256(_canonical(snapshot).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class PlacementGenerationResult:
    eligible_experiences: int
    proposal_sets: int
    regenerated: int
    unchanged: int
    runtime_seconds: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "eligible_experiences": self.eligible_experiences,
            "proposal_sets": self.proposal_sets,
            "regenerated": self.regenerated,
            "unchanged": self.unchanged,
            "runtime_seconds": round(self.runtime_seconds, 3),
            "generator_version": ENGINE_VERSION,
        }


class PlacementGenerator:
    """Generate or retain the current proposal set for every eligible Experience."""

    def __init__(self, repo: Repository):
        self.repo = repo

    def run(self) -> PlacementGenerationResult:
        started = time.monotonic()
        started_at = datetime.now(timezone.utc)
        work: list[tuple[dict[str, Any], list[dict[str, Any]], dict[str, list[dict[str, Any]]], str, list[dict[str, Any]]]] = []
        for row in self.repo.list_placement_eligible_experiences():
            experience = dict(row)
            evidence = self.repo.get_placement_evidence(experience["id"])
            trajectory_ids = sorted({str(item["trajectory_id"]) for item in evidence if item.get("trajectory_id")})
            events = {
                trajectory_id: [dict(event) for event in self.repo.get_trajectory_events(trajectory_id)]
                for trajectory_id in trajectory_ids
            }
            fingerprint = _fingerprint(experience, evidence, events)
            proposals = build_proposals(experience, evidence, events)
            # Storage owns JSON field names; the pure engine owns only its domain
            # vocabulary.  Every alternative links back to every input occurrence.
            roles = {
                str(item.get("occurrence_id")): str(item.get("relationship") or "evidence")
                for item in evidence if item.get("occurrence_id")
            }
            stored = []
            for proposal in proposals:
                diagnostics = proposal["diagnostics"]
                stored.append({
                    "placement_type": proposal["placement_type"],
                    "scope_type": proposal["scope_type"],
                    "scope_value": proposal.get("scope_value"),
                    "proposed_content": proposal["proposed_content"],
                    "score": proposal["score"],
                    "rank": proposal["rank"],
                    "feature_values": proposal["features"],
                    "diagnostics": diagnostics,
                    "diagnostics_text": "\n".join(
                        f"{row.get('polarity', '')} {row.get('message', '')}".strip()
                        for row in diagnostics
                    ),
                    "evidence": [
                        {"occurrence_id": occurrence_id, "role": roles.get(occurrence_id, "evidence")}
                        for occurrence_id in proposal["evidence_occurrence_ids"]
                    ],
                })
            work.append((experience, evidence, events, fingerprint, stored))

        elapsed = time.monotonic() - started
        finished_at = datetime.now(timezone.utc)
        run_id = self.repo.record_placement_run({
            "started_at": started_at.isoformat(),
            "finished_at": finished_at.isoformat(),
            "generator_version": ENGINE_VERSION,
            "eligible_experiences": len(work),
            "proposal_sets_generated": len(work),
            "runtime_seconds": elapsed,
        })
        regenerated = 0
        unchanged = 0
        for experience, _evidence, _events, fingerprint, proposals in work:
            existing = self.repo.get_placement_proposal_set(experience["id"])
            existing_proposals = self.repo.get_placement_proposals(experience["id"])
            if (
                existing is not None
                and existing["source_fingerprint"] == fingerprint
                and existing["generator_version"] == ENGINE_VERSION
                and len(existing_proposals) == 5
            ):
                unchanged += 1
                continue
            self.repo.replace_placement_proposal_set(
                experience_id=experience["id"],
                source_fingerprint=fingerprint,
                generator_version=ENGINE_VERSION,
                proposals=proposals,
                run_id=run_id,
            )
            regenerated += 1
        return PlacementGenerationResult(
            eligible_experiences=len(work), proposal_sets=len(work), regenerated=regenerated,
            unchanged=unchanged, runtime_seconds=time.monotonic() - started,
        )
