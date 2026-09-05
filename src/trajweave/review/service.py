"""Application service for the Stage 7 review state machine."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from trajweave.config.paths import TrajWeavePaths
from trajweave.review.targets import (
    Preview,
    SafetyError,
    apply_preview,
    build_preview,
    resolve_target,
)
from trajweave.storage.repository import Repository


class ReviewError(RuntimeError):
    """A review action is invalid or its proposal is stale."""


_ACTIONS = {"accept", "reject", "defer", "test_first", "edit", "choose"}


def _decode(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class ReviewService:
    def __init__(self, repo: Repository, paths: TrajWeavePaths):
        self.repo = repo
        self.paths = paths

    def _context(self, proposal_id: str) -> dict[str, Any]:
        context = self.repo.get_policy_proposal_context(proposal_id)
        if context is None:
            raise ReviewError(f"no Stage 6 proposal {proposal_id}")
        context["feature_values"] = _decode(context.get("feature_values_json"), {})
        context["diagnostics"] = _decode(context.get("diagnostics_json"), [])
        return context

    def _review(self, review_or_proposal_id: str) -> dict[str, Any]:
        row = self.repo.get_policy_review(review_or_proposal_id)
        if row is None:
            context = self._context(review_or_proposal_id)
            return self._payload(None, context)
        review = dict(row)
        context = self._context(str(review["selected_proposal_id"]))
        if context["source_fingerprint"] != review["source_fingerprint"]:
            review["computed_status"] = "stale"
            review["stale_reason"] = "Stage 6 proposal source changed; new review required"
        return self._payload(review, context)

    def _payload(self, review: dict[str, Any] | None, context: dict[str, Any]) -> dict[str, Any]:
        selected = dict(context)
        selected["feature_values"] = context.get("feature_values", {})
        selected["diagnostics"] = context.get("diagnostics", [])
        content = (review or {}).get("content_override")
        selected["effective_content"] = content if content is not None else selected.get("proposed_content", "")
        payload = {
            "review": review,
            "proposal": selected,
            "experience": {
                "id": context.get("experience_id"),
                "title": context.get("experience_title"),
                "summary": context.get("experience_summary"),
                "reusable_lesson": context.get("reusable_lesson"),
                "pattern_type": context.get("experience_pattern_type"),
            },
            "alternatives": [dict(row) for row in self.repo.get_placement_proposals(context["experience_id"])],
            "evidence": [dict(row) for row in self.repo.get_placement_proposal_evidence(context["id"])],
        }
        for row in payload["alternatives"]:
            row["feature_values"] = _decode(row.get("feature_values_json"), {})
            row["diagnostics"] = _decode(row.get("diagnostics_json"), [])
        if review:
            history = self.repo.policy_review_history(review["id"])
            payload["history"] = {key: [dict(item) for item in value] for key, value in history.items()}
        else:
            payload["history"] = {"actions": [], "previews": [], "variants": [], "applications": []}
        return payload

    def list(self, *, status: str | None = None) -> list[dict[str, Any]]:
        rows = []
        for row in self.repo.list_policy_reviews(status=status):
            item = dict(row)
            persisted_review = bool(item.get("review_id"))
            item["review_id"] = item.get("review_id") or f"RV-{item['proposal_set_id']}"
            item["review_status"] = item.get("review_status_value") or "unreviewed"
            if persisted_review and item.get("review_source_fingerprint") != item.get("source_fingerprint"):
                item["review_status"] = "stale"
            rows.append(item)
        return rows

    def _ensure(self, proposal_id: str) -> dict[str, Any]:
        context = self._context(proposal_id)
        review_id = self.repo.create_policy_review(context)
        return dict(self.repo.get_policy_review(review_id))

    def _assert_fresh(self, review: dict[str, Any]) -> dict[str, Any]:
        context = self._context(str(review["selected_proposal_id"]))
        if context["source_fingerprint"] != review["source_fingerprint"]:
            self.repo.update_policy_review(
                review["id"], status="stale", stale_reason="Stage 6 proposal source changed; new review required",
                action="stale", payload={"reason": "source_fingerprint_changed"},
            )
            raise ReviewError("Stage 6 proposal changed; create a new review before continuing")
        return context

    def _act(self, proposal_or_review_id: str, action: str, *, status: str, **kwargs: Any) -> str:
        if action not in _ACTIONS:
            raise ReviewError(f"unsupported review action: {action}")
        if self.repo.get_policy_review(proposal_or_review_id) is None:
            if action == "choose":
                self._ensure(proposal_or_review_id)
            else:
                self._ensure(proposal_or_review_id)
        review = dict(self.repo.get_policy_review(proposal_or_review_id))
        self._assert_fresh(review)
        self.repo.update_policy_review(review["id"], status=status, action=action, **kwargs)
        return str(review["id"])

    def accept(self, value: str, *, agent: str | None = None, target: str | None = None) -> str:
        review = self._ensure(value) if self.repo.get_policy_review(value) is None else dict(self.repo.get_policy_review(value))
        if review["status"] in {"rejected", "deferred", "test_first", "stale"}:
            # Re-accept is explicit and allowed as a new decision.
            pass
        self._assert_fresh(review)
        self.repo.update_policy_review(
            review["id"], status="accepted", target_agent=agent, target_path=target,
            action="accept", payload={"explicit": True},
        )
        return str(review["id"])

    def reject(self, value: str) -> str:
        return self._act(value, "reject", status="rejected")

    def defer(self, value: str) -> str:
        return self._act(value, "defer", status="deferred")

    def test_first(self, value: str) -> str:
        return self._act(value, "test_first", status="test_first")

    def edit(self, value: str, content: str) -> str:
        if not content.strip():
            raise ReviewError("edited content must not be empty")
        review = self._ensure(value) if self.repo.get_policy_review(value) is None else dict(self.repo.get_policy_review(value))
        self._assert_fresh(review)
        revision = int(review["content_revision"]) + 1
        self.repo.record_policy_variant(
            review["id"], revision=revision, proposal_id=review["selected_proposal_id"],
            content=content, content_hash=_hash_text(content),
        )
        self.repo.update_policy_review(
            review["id"], status="unreviewed", content_override=content,
            content_revision=revision, action="edit", payload={"content_hash": _hash_text(content)},
        )
        return str(review["id"])

    def choose(self, value: str, placement_type: str) -> str:
        current_review = self.repo.get_policy_review(value)
        context = self._context(str(current_review["selected_proposal_id"])) if current_review else self._context(value)
        alternatives = {str(row["placement_type"]): dict(row) for row in self.repo.get_placement_proposals(context["experience_id"])}
        selected = alternatives.get(placement_type)
        if selected is None or selected["proposal_set_id"] != context["proposal_set_id"]:
            raise ReviewError(f"placement alternative is not in proposal set: {placement_type}")
        review = dict(current_review) if current_review else self._ensure(value)
        self._assert_fresh(review)
        self.repo.update_policy_review(
            review["id"], status="unreviewed", selected_proposal_id=selected["id"],
            clear_content_override=True, content_revision=int(review["content_revision"]) + 1,
            action="choose", payload={"placement_type": placement_type},
        )
        return str(review["id"])

    def _project_root(self, context: dict[str, Any]) -> str | None:
        if context.get("scope_value") and context.get("placement_type") == "project_rule":
            project = self.repo.get_project(str(context["scope_value"]))
            if project:
                return str(project["root"])
        return context.get("project_root")

    def preview(self, value: str, *, target: str | None = None, agent: str | None = None) -> dict[str, Any]:
        payload = self._review(value)
        review = payload.get("review")
        if not review:
            raise ReviewError("review the proposal explicitly before previewing")
        review = dict(review)
        if review.get("computed_status") == "stale":
            raise ReviewError(str(review["stale_reason"]))
        context = self._assert_fresh(review)
        if review["status"] not in {"accepted", "applied"}:
            raise ReviewError("Preview requires an explicitly accepted review")
        chosen_agent = agent or review.get("target_agent")
        chosen_target = target or review.get("target_path")
        global_root = self.paths.home / "policies" / (chosen_agent or "codex")
        spec = resolve_target(
            placement_type=context["placement_type"], project_root=self._project_root(context),
            agent=chosen_agent, target=chosen_target, global_root=global_root,
            review_id=review["id"], scope_type=context.get("scope_type"), scope_value=context.get("scope_value"),
        )
        content = review.get("content_override") if review.get("content_override") is not None else context["proposed_content"]
        built = build_preview(target=spec, experience_id=context["experience_id"], review_id=review["id"], content=content)
        preview_id = self.repo.record_policy_preview(review["id"], {
            "target_path": str(spec.path), "target_hash": built.target_hash, "output_hash": built.output_hash,
            "proposed_content": content, "unified_diff": built.unified_diff, "target_kind": spec.kind,
        })
        self.repo.update_policy_review(
            review["id"], target_agent=spec.agent, target_path=str(spec.path),
            target_scope_type=spec.scope_type, target_scope_value=spec.scope_value,
            action="preview", payload={"preview_id": preview_id, "target": spec.as_dict()},
        )
        return {"preview_id": preview_id, "review_id": review["id"], "proposal": context,
                "experience": payload["experience"], "target": spec.as_dict(),
                "proposed_content": content, "target_hash": built.target_hash,
                "output_hash": built.output_hash, "unified_diff": built.unified_diff,
                "diff": built.unified_diff}

    def apply(self, value: str, *, dry_run: bool = False) -> dict[str, Any]:
        payload = self._review(value)
        review = payload.get("review")
        if not review:
            raise ReviewError("Accept the proposal before Apply")
        review = dict(review)
        if review.get("computed_status") == "stale":
            raise ReviewError(str(review["stale_reason"]))
        self._assert_fresh(review)
        if review["status"] not in {"accepted", "applied"}:
            raise ReviewError("Apply requires an explicit Accept")
        pending = self.repo.pending_policy_application(review["id"])
        if pending is not None:
            raise ReviewError("a previous Apply is pending reconciliation; no further writes are allowed")
        preview_row = self.repo.latest_policy_preview(review["id"])
        if preview_row is None and dry_run:
            # The CLI's documented --dry-run path is a convenient preview
            # operation, but it still persists only the review/preview ledger,
            # never the target file.
            self.preview(value)
            preview_row = self.repo.latest_policy_preview(review["id"])
        if preview_row is None:
            raise ReviewError("no preview exists; run Preview before Apply")
        preview_data = dict(preview_row)
        spec = resolve_target(
            placement_type=str(self._context(review["selected_proposal_id"])["placement_type"]),
            project_root=self._project_root(self._context(review["selected_proposal_id"])),
            agent=review.get("target_agent"), target=review.get("target_path"),
            global_root=self.paths.home / "policies" / str(review.get("target_agent") or "codex"),
            review_id=review["id"],
        )
        built = build_preview(target=spec, experience_id=payload["proposal"]["experience_id"], review_id=review["id"], content=preview_data["proposed_content"])
        # Whether the file on disk *right now* already has the desired content -
        # not whether this render matches the (possibly stale) preview record,
        # which would be true even on a first write for unchanged content.
        already_applied = built.target_hash is not None and built.before == built.after
        if not already_applied and (built.target_hash != preview_data["target_hash"] or built.output_hash != preview_data["output_hash"]):
            raise ReviewError("preview is stale or target configuration changed; create a new preview")
        if dry_run:
            return {"outcome": "dry_run", "preview_id": preview_data["id"], "target_path": str(spec.path),
                    "target_hash": built.target_hash, "after_hash": built.output_hash,
                    "unified_diff": built.unified_diff, "writes": False}
        application_id = self.repo.begin_policy_application(review["id"], {
            "preview_id": preview_data["id"], "target_path": str(spec.path),
            "before_hash": built.target_hash, "after_hash": built.output_hash,
            "from_status": review["status"],
        })
        try:
            if already_applied:
                outcome = "already applied"
            else:
                outcome = apply_preview(Preview(spec, "", built.before, built.after, preview_data["target_hash"], built.output_hash, built.unified_diff))
        except SafetyError as exc:
            self.repo.finalize_policy_application(application_id, review["id"], {
                "preview_id": preview_data["id"], "outcome": "safety_refused", "target_path": str(spec.path),
                "before_hash": built.target_hash, "after_hash": None, "detail": str(exc),
                "from_status": review["status"], "to_status": review["status"],
            })
            raise ReviewError(str(exc)) from exc
        result = "already_applied" if outcome == "already applied" else "applied"
        self.repo.finalize_policy_application(application_id, review["id"], {
            "preview_id": preview_data["id"], "outcome": result, "target_path": str(spec.path),
            "before_hash": built.target_hash, "after_hash": built.output_hash, "detail": outcome,
            "from_status": review["status"], "to_status": "applied",
        })
        self.repo.consume_policy_preview(preview_data["id"])
        return {"outcome": result, "preview_id": preview_data["id"], "target_path": str(spec.path),
                "before_hash": built.target_hash, "after_hash": built.output_hash,
                "unified_diff": built.unified_diff, "writes": result == "applied"}

    def history(self, value: str) -> dict[str, Any]:
        payload = self._review(value)
        review = payload.get("review")
        if not review:
            return payload
        return payload
