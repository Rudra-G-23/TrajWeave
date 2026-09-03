"""Stage 5 orchestrator: normalized trajectories -> candidate experiences.

Idempotent and incremental (Stage 5 brief 27-28):

* occurrence *detection* only runs on trajectories that are new or whose source
  hash changed since the last run (``--rebuild`` forces all);
* experience *grouping* is always a full recompute from the persisted
  occurrence table - it is cheap (occurrences are rare) and keeps the aggregate
  counts, contradictions and confidence trivially consistent.

Nothing here mutates a repository or decides placement.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from trajweave.experience.config import ExperienceConfig
from trajweave.experience.context import file_context, is_vendored
from trajweave.experience.detect import detect_occurrences
from trajweave.experience.grouping import TrajectoryProfile, build_experiences
from trajweave.experience.models import STATUS_CANDIDATE, STATUS_NEEDS_MORE, Occurrence
from trajweave.experience.summarize import get_summarizer
from trajweave.storage.repository import Repository
from trajweave.utils.logging import get_logger

log = get_logger("experience.extract")

_CHANGE_TYPES = frozenset({"file_edit", "file_create"})
_FAIL_TO_FAMILY = {"test_fail": "test", "lint_fail": "lint", "build_fail": "build"}
_PASS_TO_FAMILY = {"test_pass": "test", "lint_pass": "lint", "build_pass": "build"}


@dataclass
class ExtractionResult:
    trajectories_considered: int = 0
    trajectories_analyzed: int = 0
    occurrences_found: int = 0
    clusters_formed: int = 0
    candidates_created: int = 0
    needs_more_evidence: int = 0
    llm_used: bool = False
    llm_tokens: int = 0
    runtime_seconds: float = 0.0
    rebuild: bool = False
    project_filter: str | None = None
    top_candidates: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "trajectories_considered": self.trajectories_considered,
            "trajectories_analyzed": self.trajectories_analyzed,
            "occurrences_found": self.occurrences_found,
            "clusters_formed": self.clusters_formed,
            "candidates_created": self.candidates_created,
            "needs_more_evidence": self.needs_more_evidence,
            "llm_used": self.llm_used,
            "llm_tokens": self.llm_tokens,
            "runtime_seconds": round(self.runtime_seconds, 3),
            "rebuild": self.rebuild,
            "project_filter": self.project_filter,
            "top_candidates": self.top_candidates,
        }


def _row_features(raw: Any) -> dict:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            v = json.loads(raw)
            return v if isinstance(v, dict) else {}
        except (TypeError, ValueError):
            return {}
    return {}


def _occ_from_row(r: Any) -> Occurrence:
    o = Occurrence(
        trajectory_id=r["trajectory_id"],
        project_id=r["project_id"],
        pattern_type=r["pattern_type"],
        group_key=r["group_key"],
        start_sequence=int(r["start_sequence"] or 0),
        end_sequence=int(r["end_sequence"] or 0),
        failure_family=r["failure_family"],
        resolution_family=r["resolution_family"],
        repair_context=r["repair_context"],
        error_signature=r["error_signature"],
        classification=r["classification"],
        features=_row_features(r["features_json"]),
    )
    o.id = r["id"]
    return o


def _build_profile(traj: Any, events: list[Any]) -> TrajectoryProfile:
    prof = TrajectoryProfile(
        trajectory_id=traj["id"],
        project_id=traj["project_id"],
        final_status=str(traj["final_status"] or "unknown"),
        last_activity=traj["ended_at"] or traj["started_at"],
    )
    seen_fail: set[str] = set()
    for e in events:
        etype = str(e["type"] or "")
        seq = int(e["sequence"] or 0)
        prof.max_sequence = max(prof.max_sequence, seq)
        path = e["path"]
        if etype in _CHANGE_TYPES and path and not is_vendored(path):
            ctx = file_context(path)
            if ctx != "other":
                prof.edit_contexts.add(ctx)
        fam = _FAIL_TO_FAMILY.get(etype)
        if fam:
            seen_fail.add(fam)
            prof.fail_families.add(fam)
        pfam = _PASS_TO_FAMILY.get(etype)
        if pfam and pfam not in seen_fail:
            prof.clean_pass_families.add(pfam)
    return prof


class ExperienceExtractor:
    def __init__(self, repo: Repository, cfg: ExperienceConfig | None = None):
        self.repo = repo
        self.cfg = (cfg or ExperienceConfig()).validated()
        self.summarizer = get_summarizer(self.cfg)

    def run(
        self,
        *,
        project_id: str | None = None,
        rebuild: bool = False,
        now: datetime | None = None,
    ) -> ExtractionResult:
        started = time.time()
        now = now or datetime.now(timezone.utc)
        res = ExtractionResult(rebuild=rebuild, project_filter=project_id)

        if rebuild:
            self.repo.clear_experience_data()

        state = {} if rebuild else self.repo.experience_extraction_state()
        traj_rows = self.repo.list_trajectories_for_extraction(project_id)
        res.trajectories_considered = len(traj_rows)

        profiles: dict[str, TrajectoryProfile] = {}
        for row in traj_rows:
            events = [dict(e) for e in self.repo.get_trajectory_events(row["id"])]
            profiles[row["id"]] = _build_profile(dict(row), events)

            changed = rebuild or row["id"] not in state or state[row["id"]] != row["source_hash"]
            if not changed:
                continue
            occs = detect_occurrences(dict(row), events, self.cfg)
            self.repo.replace_trajectory_occurrences(row["id"], occs)
            self.repo.set_extraction_state(row["id"], row["source_hash"])
            res.trajectories_analyzed += 1
            if occs:
                log.debug(
                    "%s: %d occurrence(s) %s",
                    row["id"], len(occs), [o.group_key for o in occs],
                )

        # Full recompute of experiences from every persisted real occurrence.
        real_rows = self.repo.load_occurrences(classifications=("support", "ambiguous"))
        real_occ = [_occ_from_row(r) for r in real_rows]
        grouped, all_occ = build_experiences(
            real_occ, profiles, cfg=self.cfg, summarizer=self.summarizer, now=now
        )
        contradictions = [o for o in all_occ if o.classification == "contradiction"]
        self.repo.rebuild_experiences(grouped, contradictions)
        log.debug(
            "grouped %d cluster(s) -> %d experience row(s), %d synthesized contradiction(s)",
            len({o.group_key for o in real_occ}), len(grouped), len(contradictions),
        )

        res.occurrences_found = len(real_occ) + len(contradictions)
        res.clusters_formed = len({o.group_key for o in real_occ})
        res.candidates_created = sum(1 for g in grouped if g.status == STATUS_CANDIDATE)
        res.needs_more_evidence = sum(1 for g in grouped if g.status == STATUS_NEEDS_MORE)
        res.llm_used = any(g.summary.source == "llm" for g in grouped)
        res.llm_tokens = sum(g.summary.tokens for g in grouped)
        res.runtime_seconds = time.time() - started
        res.top_candidates = [
            {
                "group_key": g.group_key,
                "title": g.summary.title,
                "confidence": g.confidence.score,
                "occurrences": g.occurrence_count,
                "support": g.support_count,
                "contradictions": g.contradiction_count,
                "projects": g.project_count,
                "status": g.status,
            }
            for g in grouped
            if g.status == STATUS_CANDIDATE
        ][:10]

        self.repo.record_experience_run(
            {
                "started_at": datetime.fromtimestamp(started, tz=timezone.utc).isoformat(),
                "finished_at": now.isoformat(),
                "rebuild": 1 if rebuild else 0,
                "project_filter": project_id,
                "trajectories_considered": res.trajectories_considered,
                "trajectories_analyzed": res.trajectories_analyzed,
                "occurrences_found": res.occurrences_found,
                "clusters_formed": res.clusters_formed,
                "candidates_created": res.candidates_created,
                "needs_more_evidence": res.needs_more_evidence,
                "llm_used": 1 if res.llm_used else 0,
                "llm_tokens": res.llm_tokens,
                "runtime_seconds": res.runtime_seconds,
            }
        )
        return res
