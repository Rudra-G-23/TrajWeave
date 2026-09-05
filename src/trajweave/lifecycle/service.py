"""Application service for the Stage 9 lifecycle state machine.

Mirrors the shape of :class:`trajweave.review.service.ReviewService` and
:class:`trajweave.evaluation.service.EvaluationService`: one service class
owns the state machine and is the single read-model shared by the CLI and
the UI.

Every operation that changes what a policy *is* (rewrite/promote/demote/
merge/split/rollback) only ever inserts a new immutable ``policy_versions``
row plus explicit ``policy_lineage`` edges and repoints
``policies.current_version_id`` - it never touches a historical row and it
never writes to the filesystem. Only :meth:`LifecycleService.preview` /
:meth:`LifecycleService.apply` ever write to disk, and they do so entirely
through ``trajweave.review.targets`` (the same safety layer Stage 7 uses),
never a second, weaker writer.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from trajweave.config.paths import TrajWeavePaths
from trajweave.lifecycle.apply import (
    Preview,
    SafetyError,
    apply_lifecycle_preview,
    build_lifecycle_preview,
    resolve_lifecycle_target,
)
from trajweave.lifecycle.errors import LifecycleError
from trajweave.lifecycle.evidence import aggregate_evidence
from trajweave.lifecycle.heuristics import (
    detect_duplicate_policies,
    detect_stale,
    recommend_for_version,
)
from trajweave.lifecycle.models import DEMOTE_TARGETS, PROMOTE_TARGETS
from trajweave.review.service import ReviewError, ReviewService
from trajweave.storage.repository import Repository
from trajweave.utils.hashing import stable_short_id, text_sha256

_ELIGIBLE_REVIEW_STATUSES = {"accepted", "test_first", "applied"}


def _decode(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


class LifecycleService:
    def __init__(self, repo: Repository, paths: TrajWeavePaths):
        self.repo = repo
        self.paths = paths

    # ------------------------------------------------------------------
    # internal helpers
    # ------------------------------------------------------------------
    def _require_policy(self, policy_id: str) -> dict[str, Any]:
        row = self.repo.get_policy(policy_id)
        if row is None:
            raise LifecycleError(f"no policy {policy_id}")
        return dict(row)

    def _current_version(self, policy: dict[str, Any]) -> dict[str, Any]:
        if not policy.get("current_version_id"):
            raise LifecycleError(f"policy {policy['id']} has no current version")
        row = self.repo.get_policy_version(str(policy["current_version_id"]))
        if row is None:  # pragma: no cover - defensive, current_version_id is always kept in sync
            raise LifecycleError(f"policy {policy['id']} current version is missing")
        return dict(row)

    def _require_operable(self, policy: dict[str, Any]) -> dict[str, Any]:
        if policy["status"] != "active":
            raise LifecycleError(f"policy {policy['id']} is {policy['status']}; only an active policy can be changed")
        return self._current_version(policy)

    def _new_version(self, policy_id: str, **fields: Any) -> dict[str, Any]:
        version_number = self.repo.next_policy_version_number(policy_id)
        version_id = f"PVN-{policy_id}-{version_number}"
        content = str(fields["content"])
        row = {
            "id": version_id, "policy_id": policy_id, "version_number": version_number,
            "content": content, "content_hash": text_sha256(content),
            "placement_type": fields["placement_type"], "target_agent": fields.get("target_agent"),
            "target_override": fields.get("target_override"), "scope_type": fields.get("scope_type"),
            "scope_value": fields.get("scope_value"), "status": "active",
            "created_via": fields["created_via"], "created_from_version_id": fields.get("created_from_version_id"),
            "created_from_review_id": fields.get("created_from_review_id"), "reason": fields.get("reason"),
        }
        self.repo.create_policy_version(row)
        return dict(self.repo.get_policy_version(version_id))

    def _auto_project_root(self, version: dict[str, Any]) -> str | None:
        if version["placement_type"] == "global_rule":
            return None
        if version.get("scope_value"):
            project = self.repo.get_project(str(version["scope_value"]))
            if project:
                return str(project["root"])
        return None

    # ------------------------------------------------------------------
    # adopt - bootstrap a logical policy from an existing Stage 7 review
    # ------------------------------------------------------------------
    def adopt(self, review_id: str) -> str:
        existing = self.repo.get_policy_by_origin_review(review_id)
        if existing is not None:
            return str(existing["id"])
        review_service = ReviewService(self.repo, self.paths)
        try:
            payload = review_service.history(review_id)
        except ReviewError as exc:
            raise LifecycleError(str(exc)) from exc
        review = payload.get("review")
        if not review:
            raise LifecycleError(f"{review_id} has not been reviewed yet; run 'trajweave review accept' first")
        if review.get("computed_status") == "stale":
            raise LifecycleError(str(review.get("stale_reason")))
        if review["status"] not in _ELIGIBLE_REVIEW_STATUSES:
            raise LifecycleError(
                f"review {review['id']} has status {review['status']!r}; only "
                f"{sorted(_ELIGIBLE_REVIEW_STATUSES)} can be adopted as a lifecycle policy"
            )
        proposal = payload["proposal"]
        policy_id = stable_short_id("POL-", str(review["id"]))
        self.repo.create_policy(
            policy_id, origin_review_id=str(review["id"]), origin_experience_id=proposal.get("experience_id"),
        )
        version = self._new_version(
            policy_id, content=proposal["effective_content"], placement_type=proposal["placement_type"],
            target_agent=review.get("target_agent"), target_override=review.get("target_path"),
            scope_type=review.get("target_scope_type") or proposal.get("scope_type"),
            scope_value=review.get("target_scope_value") or proposal.get("scope_value"),
            created_via="initial", created_from_review_id=str(review["id"]),
            reason="adopted from Stage 7 review",
        )
        self.repo.set_policy_current_version(policy_id, version["id"])
        self.repo.record_lifecycle_action(
            policy_id=policy_id, version_id=version["id"], action="adopt",
            from_status=None, to_status="active", payload={"review_id": review["id"]},
        )
        return policy_id

    # ------------------------------------------------------------------
    # rewrite
    # ------------------------------------------------------------------
    def rewrite(self, policy_id: str, content: str, *, reason: str | None = None) -> str:
        policy = self._require_policy(policy_id)
        current = self._require_operable(policy)
        if not content.strip():
            raise LifecycleError("rewritten content must not be empty")
        if text_sha256(content) == current["content_hash"]:
            raise LifecycleError("rewrite content is identical to the current version; nothing to do")
        new_version = self._new_version(
            policy_id, content=content, placement_type=current["placement_type"],
            target_agent=current["target_agent"], target_override=current["target_override"],
            scope_type=current["scope_type"], scope_value=current["scope_value"],
            created_via="rewrite", created_from_version_id=current["id"], reason=reason,
        )
        self.repo.record_lineage(relation="rewrite", from_version_id=current["id"], to_version_id=new_version["id"])
        self.repo.set_policy_version_status(current["id"], "superseded")
        self.repo.set_policy_current_version(policy_id, new_version["id"])
        self.repo.record_lifecycle_action(
            policy_id=policy_id, version_id=new_version["id"], action="rewrite",
            from_status="active", to_status="active", payload={"from_version_id": current["id"], "reason": reason},
        )
        return new_version["id"]

    # ------------------------------------------------------------------
    # promote / demote
    # ------------------------------------------------------------------
    def _rescope(
        self, policy_id: str, current: dict[str, Any], *, op: str, allowed: dict[str, tuple[str, ...]],
        target_placement: str | None, scope_type: str | None, scope_value: str | None,
        target_agent: str | None, target_override: str | None, reason: str | None,
    ) -> str:
        placement = str(current["placement_type"])
        targets = allowed.get(placement, ())
        if not targets:
            raise LifecycleError(f"{placement} has no valid {op} target")
        target = target_placement or targets[0]
        if target not in targets:
            raise LifecycleError(f"cannot {op} {placement} to {target}; valid targets are {list(targets)}")
        new_scope_type = scope_type if scope_type is not None else (None if target == "global_rule" else current["scope_type"])
        new_scope_value = scope_value if scope_value is not None else (None if target == "global_rule" else current["scope_value"])
        new_version = self._new_version(
            policy_id, content=current["content"], placement_type=target,
            target_agent=target_agent or current["target_agent"], target_override=target_override,
            scope_type=new_scope_type, scope_value=new_scope_value,
            created_via=op, created_from_version_id=current["id"], reason=reason,
        )
        self.repo.record_lineage(relation=op, from_version_id=current["id"], to_version_id=new_version["id"])
        self.repo.set_policy_version_status(current["id"], "superseded")
        self.repo.set_policy_current_version(policy_id, new_version["id"])
        self.repo.record_lifecycle_action(
            policy_id=policy_id, version_id=new_version["id"], action=op, from_status="active", to_status="active",
            payload={"from_version_id": current["id"], "from_placement": placement, "to_placement": target, "reason": reason},
        )
        return new_version["id"]

    def promote(
        self, policy_id: str, *, target_placement: str | None = None, scope_type: str | None = None,
        scope_value: str | None = None, target_agent: str | None = None, target_override: str | None = None,
        confirm_global: bool = False, reason: str | None = None,
    ) -> str:
        policy = self._require_policy(policy_id)
        current = self._require_operable(policy)
        allowed = PROMOTE_TARGETS.get(str(current["placement_type"]), ())
        resolved = target_placement or (allowed[0] if allowed else None)
        if resolved == "global_rule" and not confirm_global:
            raise LifecycleError(
                "promoting to global_rule requires explicit human approval; pass confirm_global=True"
            )
        return self._rescope(
            policy_id, current, op="promote", allowed=PROMOTE_TARGETS, target_placement=target_placement,
            scope_type=scope_type, scope_value=scope_value, target_agent=target_agent,
            target_override=target_override, reason=reason,
        )

    def demote(
        self, policy_id: str, *, target_placement: str | None = None, scope_type: str | None = None,
        scope_value: str | None = None, target_agent: str | None = None, target_override: str | None = None,
        reason: str | None = None,
    ) -> str:
        policy = self._require_policy(policy_id)
        current = self._require_operable(policy)
        return self._rescope(
            policy_id, current, op="demote", allowed=DEMOTE_TARGETS, target_placement=target_placement,
            scope_type=scope_type, scope_value=scope_value, target_agent=target_agent,
            target_override=target_override, reason=reason,
        )

    # ------------------------------------------------------------------
    # merge / split
    # ------------------------------------------------------------------
    def merge(
        self, policy_a_id: str, policy_b_id: str, *, content: str | None = None,
        placement_type: str | None = None, scope_type: str | None = None, scope_value: str | None = None,
        target_agent: str | None = None, target_override: str | None = None,
        allow_cross_scope: bool = False, reason: str | None = None,
    ) -> str:
        if policy_a_id == policy_b_id:
            raise LifecycleError("cannot merge a policy with itself")
        policy_a = self._require_policy(policy_a_id)
        policy_b = self._require_policy(policy_b_id)
        va = self._require_operable(policy_a)
        vb = self._require_operable(policy_b)
        if not allow_cross_scope and (va["scope_type"], va["scope_value"]) != (vb["scope_type"], vb["scope_value"]):
            raise LifecycleError(
                f"{policy_a_id} and {policy_b_id} have incompatible scopes; pass allow_cross_scope=True to merge anyway"
            )
        merged_content = content if content is not None else f"{va['content']}\n\n{vb['content']}"
        merged_hash = text_sha256(merged_content)
        merged_placement = placement_type or str(va["placement_type"])

        existing = self.repo.db.query_one(
            "SELECT pv.policy_id AS policy_id FROM policy_lineage l1 "
            "JOIN policy_lineage l2 ON l1.to_version_id = l2.to_version_id AND l1.id != l2.id "
            "JOIN policy_versions pv ON pv.id = l1.to_version_id "
            "WHERE l1.relation = 'merge_parent' AND l2.relation = 'merge_parent' "
            "AND l1.from_version_id = ? AND l2.from_version_id = ? AND pv.content_hash = ?",
            (va["id"], vb["id"], merged_hash),
        )
        if existing is not None:
            return str(existing["policy_id"])

        new_policy_id = stable_short_id("POL-", "merge", va["id"], vb["id"], merged_hash)
        self.repo.create_policy(new_policy_id, origin_review_id=None, origin_experience_id=None)
        new_version = self._new_version(
            new_policy_id, content=merged_content, placement_type=merged_placement,
            target_agent=target_agent or va["target_agent"], target_override=target_override,
            scope_type=scope_type if scope_type is not None else va["scope_type"],
            scope_value=scope_value if scope_value is not None else va["scope_value"],
            created_via="merge", reason=reason,
        )
        self.repo.set_policy_current_version(new_policy_id, new_version["id"])
        self.repo.record_lineage(relation="merge_parent", from_version_id=va["id"], to_version_id=new_version["id"])
        self.repo.record_lineage(relation="merge_parent", from_version_id=vb["id"], to_version_id=new_version["id"])
        self.repo.record_lifecycle_action(
            policy_id=new_policy_id, version_id=new_version["id"], action="merge", from_status=None, to_status="active",
            payload={"parents": [policy_a_id, policy_b_id]},
        )
        for parent_id, parent_version in ((policy_a_id, va), (policy_b_id, vb)):
            self.repo.record_lifecycle_action(
                policy_id=parent_id, version_id=parent_version["id"], action="merged_into",
                from_status=policy_a["status"] if parent_id == policy_a_id else policy_b["status"],
                to_status=policy_a["status"] if parent_id == policy_a_id else policy_b["status"],
                payload={"new_policy_id": new_policy_id},
            )
        return new_policy_id

    def split(self, policy_id: str, children: list[dict[str, Any]], *, reason: str | None = None) -> list[str]:
        if not children:
            raise LifecycleError("split requires at least one child")
        policy = self._require_policy(policy_id)
        source = self._require_operable(policy)
        existing_children = [
            dict(row) for row in self.repo.db.query(
                "SELECT pv.* FROM policy_lineage l JOIN policy_versions pv ON pv.id = l.to_version_id "
                "WHERE l.from_version_id = ? AND l.relation = 'split_child'",
                (source["id"],),
            )
        ]
        result_ids: list[str] = []
        for child in children:
            content = str(child["content"])
            if not content.strip():
                raise LifecycleError("a split child's content must not be empty")
            placement_type = child.get("placement_type", source["placement_type"])
            scope_type = child.get("scope_type")
            scope_value = child.get("scope_value")
            content_hash = text_sha256(content)
            duplicate = next(
                (row for row in existing_children
                 if row["content_hash"] == content_hash and row["placement_type"] == placement_type
                 and row["scope_type"] == scope_type and row["scope_value"] == scope_value),
                None,
            )
            if duplicate is not None:
                result_ids.append(str(duplicate["policy_id"]))
                continue
            new_policy_id = stable_short_id(
                "POL-", "split", source["id"], content_hash, str(placement_type), str(scope_type or ""), str(scope_value or ""),
            )
            self.repo.create_policy(new_policy_id, origin_review_id=None, origin_experience_id=None)
            self.repo.set_policy_status(new_policy_id, "disabled")
            new_version = self._new_version(
                new_policy_id, content=content, placement_type=placement_type,
                target_agent=child.get("target_agent", source["target_agent"]),
                target_override=child.get("target_override"), scope_type=scope_type, scope_value=scope_value,
                created_via="split", reason=reason,
            )
            self.repo.set_policy_current_version(new_policy_id, new_version["id"])
            self.repo.record_lineage(relation="split_child", from_version_id=source["id"], to_version_id=new_version["id"])
            self.repo.record_lifecycle_action(
                policy_id=new_policy_id, version_id=new_version["id"], action="split_created",
                from_status=None, to_status="disabled", payload={"source_policy_id": policy_id},
            )
            result_ids.append(new_policy_id)
        self.repo.record_lifecycle_action(
            policy_id=policy_id, version_id=source["id"], action="split",
            from_status=policy["status"], to_status=policy["status"], payload={"children": result_ids},
        )
        return result_ids

    # ------------------------------------------------------------------
    # disable / enable / prune
    # ------------------------------------------------------------------
    def disable(self, policy_id: str, *, reason: str | None = None) -> None:
        policy = self._require_policy(policy_id)
        if policy["status"] != "active":
            raise LifecycleError(f"cannot disable policy {policy_id}: status is {policy['status']!r}, expected 'active'")
        self.repo.set_policy_status(policy_id, "disabled")
        self.repo.record_lifecycle_action(
            policy_id=policy_id, version_id=policy.get("current_version_id"), action="disable",
            from_status="active", to_status="disabled", payload={"reason": reason},
        )

    def enable(self, policy_id: str, *, reason: str | None = None) -> None:
        policy = self._require_policy(policy_id)
        if policy["status"] != "disabled":
            raise LifecycleError(f"cannot enable policy {policy_id}: status is {policy['status']!r}, expected 'disabled'")
        self.repo.set_policy_status(policy_id, "active")
        self.repo.record_lifecycle_action(
            policy_id=policy_id, version_id=policy.get("current_version_id"), action="enable",
            from_status="disabled", to_status="active", payload={"reason": reason},
        )

    def prune(self, policy_id: str, *, reason: str | None = None) -> None:
        policy = self._require_policy(policy_id)
        if policy["status"] not in {"active", "disabled"}:
            raise LifecycleError(f"policy {policy_id} is already pruned")
        self.repo.set_policy_status(policy_id, "pruned")
        self.repo.record_lifecycle_action(
            policy_id=policy_id, version_id=policy.get("current_version_id"), action="prune",
            from_status=policy["status"], to_status="pruned", payload={"reason": reason},
        )

    # ------------------------------------------------------------------
    # rollback
    # ------------------------------------------------------------------
    def rollback(self, policy_id: str, target_version_number: int, *, reason: str | None = None) -> str:
        policy = self._require_policy(policy_id)
        current = self._require_operable(policy)
        target_row = self.repo.db.query_one(
            "SELECT * FROM policy_versions WHERE policy_id = ? AND version_number = ?",
            (policy_id, target_version_number),
        )
        if target_row is None:
            raise LifecycleError(f"policy {policy_id} has no version {target_version_number}")
        target = dict(target_row)
        if target["id"] == current["id"]:
            raise LifecycleError("cannot roll back to the currently active version")
        new_version = self._new_version(
            policy_id, content=target["content"], placement_type=target["placement_type"],
            target_agent=target["target_agent"], target_override=target["target_override"],
            scope_type=target["scope_type"], scope_value=target["scope_value"],
            created_via="rollback", created_from_version_id=target["id"],
            created_from_review_id=target.get("created_from_review_id"),
            reason=reason or f"rollback to version {target_version_number}",
        )
        self.repo.record_lineage(relation="rollback_source", from_version_id=target["id"], to_version_id=new_version["id"])
        self.repo.set_policy_version_status(current["id"], "rolled_back")
        self.repo.set_policy_current_version(policy_id, new_version["id"])
        self.repo.record_lifecycle_action(
            policy_id=policy_id, version_id=new_version["id"], action="rollback", from_status="active", to_status="active",
            payload={"from_version_id": current["id"], "restored_version_id": target["id"], "restored_version_number": target_version_number},
        )
        return new_version["id"]

    # ------------------------------------------------------------------
    # recommendations
    # ------------------------------------------------------------------
    def _recommendation_payload(self, row: Any) -> dict[str, Any]:
        item = dict(row)
        item["reason_codes"] = _decode(item.pop("reason_codes_json", None), [])
        item["evidence"] = _decode(item.pop("evidence_json", None), {})
        item["counter_evidence"] = _decode(item.pop("counter_evidence_json", None), [])
        return item

    def recommend(self, policy_id: str) -> dict[str, Any]:
        policy = self._require_policy(policy_id)
        current = self._current_version(policy)
        rec = recommend_for_version(self.repo, current["id"])
        self.repo.upsert_recommendation(rec)
        return self._recommendation_payload(self.repo.get_recommendation(rec["id"]))

    def duplicate_candidates(self) -> list[dict[str, Any]]:
        return detect_duplicate_policies(self.repo)

    def stale_signals(self, policy_id: str) -> list[str]:
        policy = self._require_policy(policy_id)
        current = self._current_version(policy)
        return detect_stale(self.repo, self.paths, current)

    def accept_recommendation(self, recommendation_id: str, **op_kwargs: Any) -> Any:
        row = self.repo.get_recommendation(recommendation_id)
        if row is None:
            raise LifecycleError(f"no recommendation {recommendation_id}")
        rec = self._recommendation_payload(row)
        if rec["status"] != "open":
            raise LifecycleError(f"recommendation {recommendation_id} is {rec['status']}, not open")
        policy_id = str(rec["policy_id"])
        op = str(rec["operation"])
        if op == "retain":
            result: Any = None
        elif op == "rollback":
            target_id = rec["evidence"].get("rollback_candidate_version_id")
            target_version = self.repo.get_policy_version(str(target_id)) if target_id else None
            if target_version is None:
                raise LifecycleError("rollback recommendation has no resolvable candidate version")
            result = self.rollback(policy_id, int(target_version["version_number"]), reason=f"accepted recommendation {recommendation_id}")
        elif op in {"promote", "demote"}:
            op_kwargs.setdefault("reason", f"accepted recommendation {recommendation_id}")
            result = getattr(self, op)(policy_id, **op_kwargs)
        elif op == "disable":
            self.disable(policy_id, reason=f"accepted recommendation {recommendation_id}")
            result = None
        elif op == "prune":
            self.prune(policy_id, reason=f"accepted recommendation {recommendation_id}")
            result = None
        else:
            raise LifecycleError(f"recommended operation {op!r} cannot be auto-applied; perform it explicitly")
        self.repo.update_recommendation_status(recommendation_id, "accepted")
        self.repo.record_lifecycle_action(
            policy_id=policy_id, version_id=str(rec["version_id"]), action="recommendation_accepted",
            from_status=None, to_status=None, recommendation_id=recommendation_id, payload={"operation": op},
        )
        return result

    def reject_recommendation(self, recommendation_id: str, *, reason: str | None = None) -> None:
        row = self.repo.get_recommendation(recommendation_id)
        if row is None:
            raise LifecycleError(f"no recommendation {recommendation_id}")
        if row["status"] != "open":
            raise LifecycleError(f"recommendation {recommendation_id} is {row['status']}, not open")
        self.repo.update_recommendation_status(recommendation_id, "rejected")
        self.repo.record_lifecycle_action(
            policy_id=str(row["policy_id"]), version_id=str(row["version_id"]), action="recommendation_rejected",
            from_status=None, to_status=None, recommendation_id=recommendation_id, payload={"reason": reason},
        )

    def defer_recommendation(self, recommendation_id: str, *, reason: str | None = None) -> None:
        row = self.repo.get_recommendation(recommendation_id)
        if row is None:
            raise LifecycleError(f"no recommendation {recommendation_id}")
        if row["status"] != "open":
            raise LifecycleError(f"recommendation {recommendation_id} is {row['status']}, not open")
        self.repo.update_recommendation_status(recommendation_id, "deferred")
        self.repo.record_lifecycle_action(
            policy_id=str(row["policy_id"]), version_id=str(row["version_id"]), action="recommendation_deferred",
            from_status=None, to_status=None, recommendation_id=recommendation_id, payload={"reason": reason},
        )

    # ------------------------------------------------------------------
    # preview / apply - the only methods that ever touch the filesystem
    # ------------------------------------------------------------------
    def preview(
        self, policy_id: str, *, project_root: str | None = None, target_agent: str | None = None,
        target_override: str | None = None,
    ) -> dict[str, Any]:
        policy = self._require_policy(policy_id)
        version = self._require_operable(policy)
        if target_agent:
            version = dict(version)
            version["target_agent"] = target_agent
        if target_override:
            version = dict(version)
            version["target_override"] = target_override
        resolved_root = project_root or self._auto_project_root(version)
        global_root = self.paths.home / "policies" / str(version.get("target_agent") or "codex")
        spec = resolve_lifecycle_target(version=version, project_root=resolved_root, global_root=global_root)
        built = build_lifecycle_preview(target=spec, policy_id=policy_id, version=version)
        preview_id = self.repo.record_lifecycle_preview(policy_id, version["id"], {
            "target_path": str(spec.path), "target_hash": built.target_hash, "output_hash": built.output_hash,
            "proposed_content": version["content"], "unified_diff": built.unified_diff, "target_kind": spec.kind,
        })
        self.repo.record_lifecycle_action(
            policy_id=policy_id, version_id=version["id"], action="preview", from_status="active", to_status="active",
            payload={"preview_id": preview_id, "target": spec.as_dict()},
        )
        return {
            "preview_id": preview_id, "policy_id": policy_id, "version_id": version["id"],
            "target": spec.as_dict(), "proposed_content": version["content"], "target_hash": built.target_hash,
            "output_hash": built.output_hash, "unified_diff": built.unified_diff, "diff": built.unified_diff,
        }

    def apply(
        self, policy_id: str, *, project_root: str | None = None, dry_run: bool = False,
        target_agent: str | None = None, target_override: str | None = None, confirm_global: bool = False,
    ) -> dict[str, Any]:
        policy = self._require_policy(policy_id)
        version = self._require_operable(policy)
        if version["placement_type"] == "global_rule" and not confirm_global and not dry_run:
            raise LifecycleError("applying a global_rule policy requires explicit human approval; pass confirm_global=True")
        if target_agent:
            version = dict(version)
            version["target_agent"] = target_agent
        if target_override:
            version = dict(version)
            version["target_override"] = target_override
        preview_row = self.repo.latest_lifecycle_preview(version["id"])
        if preview_row is None and dry_run:
            self.preview(policy_id, project_root=project_root, target_agent=target_agent, target_override=target_override)
            preview_row = self.repo.latest_lifecycle_preview(version["id"])
        if preview_row is None:
            raise LifecycleError("no preview exists; run preview before apply")
        preview_data = dict(preview_row)
        resolved_root = project_root or self._auto_project_root(version)
        global_root = self.paths.home / "policies" / str(version.get("target_agent") or "codex")
        spec = resolve_lifecycle_target(version=version, project_root=resolved_root, global_root=global_root)
        built = build_lifecycle_preview(target=spec, policy_id=policy_id, version=version)
        # Whether the file on disk *right now* already has the desired content -
        # not whether this render matches the (possibly stale) preview record,
        # which would be true even on a first write for unchanged content.
        already_applied = built.target_hash is not None and built.before == built.after
        if not already_applied and (built.target_hash != preview_data["target_hash"] or built.output_hash != preview_data["output_hash"]):
            raise LifecycleError("preview is stale or target configuration changed; create a new preview")
        if dry_run:
            return {
                "outcome": "dry_run", "preview_id": preview_data["id"], "target_path": str(spec.path),
                "target_hash": built.target_hash, "after_hash": built.output_hash,
                "unified_diff": built.unified_diff, "writes": False,
            }
        try:
            outcome = "already applied" if already_applied else apply_lifecycle_preview(
                Preview(spec, "", built.before, built.after, preview_data["target_hash"], built.output_hash, built.unified_diff)
            )
        except SafetyError as exc:
            self.repo.record_lifecycle_application(policy_id, version["id"], {
                "preview_id": preview_data["id"], "outcome": "safety_refused", "target_path": str(spec.path),
                "before_hash": built.target_hash, "after_hash": None, "detail": str(exc),
            })
            raise LifecycleError(str(exc)) from exc
        result = "already_applied" if outcome == "already applied" else "applied"
        self.repo.record_lifecycle_application(policy_id, version["id"], {
            "preview_id": preview_data["id"], "outcome": result, "target_path": str(spec.path),
            "before_hash": built.target_hash, "after_hash": built.output_hash, "detail": outcome,
        })
        self.repo.consume_lifecycle_preview(preview_data["id"])
        self.repo.record_lifecycle_action(
            policy_id=policy_id, version_id=version["id"], action=f"apply_{result}",
            from_status="active", to_status="active", payload={"target_path": str(spec.path)},
        )
        return {
            "outcome": result, "preview_id": preview_data["id"], "target_path": str(spec.path),
            "before_hash": built.target_hash, "after_hash": built.output_hash,
            "unified_diff": built.unified_diff, "writes": result == "applied",
        }

    # ------------------------------------------------------------------
    # read model - shared by the CLI and the UI
    # ------------------------------------------------------------------
    def list(self, *, status: str | None = None) -> list[dict[str, Any]]:
        return [dict(row) for row in self.repo.list_policies(status=status)]

    def show(self, policy_id: str) -> dict[str, Any]:
        policy = self._require_policy(policy_id)
        versions = [dict(v) for v in self.repo.list_policy_versions(policy_id)]
        current = next((v for v in versions if v["id"] == policy.get("current_version_id")), None)
        version_ids = {v["id"] for v in versions}
        lineage_rows: dict[int, dict[str, Any]] = {}
        for vid in version_ids:
            for row in list(self.repo.lineage_from(vid)) + list(self.repo.lineage_to(vid)):
                lineage_rows[int(row["id"])] = dict(row)
        evidence = aggregate_evidence(self.repo, current["id"]) if current else None
        recommendations = [self._recommendation_payload(r) for r in self.repo.list_recommendations(policy_id)]
        history = self.repo.lifecycle_history(policy_id)
        return {
            "policy": policy, "versions": versions, "current_version": current,
            "lineage": sorted(lineage_rows.values(), key=lambda r: r["id"]),
            "evidence": evidence, "recommendations": recommendations,
            "actions": [dict(r) for r in history["actions"]],
            "previews": [dict(r) for r in history["previews"]],
            "applications": [dict(r) for r in history["applications"]],
        }

    def history(self, policy_id: str) -> dict[str, Any]:
        return self.show(policy_id)

    def full_provenance(self, version_id: str) -> dict[str, Any]:
        """Trace version -> lifecycle event -> recommendation -> Stage 8
        comparison -> Stage 7 review -> Stage 6 placement -> Stage 5
        experience -> evidence occurrence -> trajectory."""

        version = self.repo.get_policy_version(version_id)
        if version is None:
            raise LifecycleError(f"no policy version {version_id}")
        version = dict(version)
        actions = [dict(r) for r in self.repo.list_lifecycle_actions_for_version(version_id)]
        recommendations = [
            self._recommendation_payload(r)
            for r in self.repo.db.query(
                "SELECT * FROM policy_recommendations WHERE version_id = ? ORDER BY created_at", (version_id,)
            )
        ]
        comparisons = [dict(r) for r in self.repo.list_version_evidence(version_id)]
        reviews: dict[str, Any] = {}
        placements: dict[str, Any] = {}
        experiences: dict[str, Any] = {}
        for comparison in comparisons:
            spec = self.repo.get_evaluation_spec(str(comparison["evaluation_id"]))
            if spec is None:
                continue
            review_id = str(spec["review_id"])
            review = self.repo.get_policy_review(review_id)
            if review is None:
                continue
            reviews[review_id] = dict(review)
            context = self.repo.get_policy_proposal_context(str(review["selected_proposal_id"]))
            if context is None:
                continue
            placements[str(context["id"])] = dict(context)
            experience_id = str(context["experience_id"])
            if experience_id not in experiences:
                experience = self.repo.get_experience(experience_id)
                evidence_rows = [dict(e) for e in self.repo.get_experience_evidence(experience_id)]
                experiences[experience_id] = {
                    "experience": dict(experience) if experience else None,
                    "evidence_occurrences": evidence_rows,
                }
        return {
            "version": version, "lifecycle_actions": actions, "recommendations": recommendations,
            "evaluation_comparisons": comparisons, "stage7_reviews": list(reviews.values()),
            "stage6_placements": list(placements.values()), "stage5_experiences": list(experiences.values()),
        }
