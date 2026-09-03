"""Read-only Stage 6 placement payloads do not need a browser server."""

from __future__ import annotations

from trajweave.ui.server import build_placement, build_placements


class _PlacementRepo:
    def list_placement_proposal_sets(self, *, placement_type=None, recommended=None):
        assert placement_type == "project_rule"
        assert recommended == "project_rule"
        return [{
            "experience_id": "E-0007",
            "recommended_type": "project_rule",
            "recommended_score": 0.84,
            "recommended_scope_type": "project",
            "recommended_scope_value": "alpha",
        }]

    def get_placement_proposal_set(self, experience_id):
        assert experience_id == "E-0007"
        return {"id": "PS-E-0007", "experience_id": experience_id, "generator_version": "stage6-v1"}

    def get_placement_proposals(self, experience_id):
        return [
            {
                "id": "PP-E-0007-project_rule",
                "placement_type": "project_rule",
                "scope_type": "project",
                "scope_value": "alpha",
                "score": 0.84,
                "rank": 1,
                "proposed_content": "Use the project convention.",
                "diagnostics_json": '[{"sign": "+", "message": "three supports"}]',
                "feature_values_json": '{"project_count": 1}',
            },
            {
                "id": "PP-E-0007-ignore",
                "placement_type": "ignore",
                "scope_type": "global",
                "scope_value": None,
                "score": 0.20,
                "rank": 2,
                "proposed_content": "Use the project convention.",
                "diagnostics_json": "[]",
                "feature_values_json": "{}",
            },
        ]

    def get_placement_proposal_evidence(self, proposal_id):
        if proposal_id.endswith("ignore"):
            return []
        return [{
            "role": "support",
            "occurrence_id": "O-0001",
            "trajectory_id": "TW-000001",
            "classification": "support",
            "project_name": "alpha",
            "project_root": "/private/alpha",
            "start_sequence": 3,
            "end_sequence": 6,
            "features_json": '{"family": "test"}',
        }]


def test_placement_list_exposes_current_recommendation_only():
    payload = build_placements(_PlacementRepo(), {"recommended": ["project_rule"]})

    assert payload["schema_ok"] is True
    assert payload["placements"][0]["recommended_type"] == "project_rule"
    assert payload["placements"][0]["recommended_score"] == 0.84


def test_placement_detail_decodes_trace_without_private_root():
    payload = build_placement(_PlacementRepo(), "E-0007")

    assert payload is not None
    assert [p["placement_type"] for p in payload["proposals"]] == ["project_rule", "ignore"]
    assert payload["proposals"][0]["diagnostics"][0]["message"] == "three supports"
    assert payload["proposals"][0]["features"]["project_count"] == 1
    assert payload["evidence"][0]["relationship"] == "support"
    assert payload["evidence"][0]["project_name"] == "alpha"
    assert "project_root" not in payload["evidence"][0]
