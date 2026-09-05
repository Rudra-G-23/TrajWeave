from __future__ import annotations

import pytest

from trajweave.evaluation.apply import SafetyError, apply_candidate_policy


def _spec(root, **overrides):
    base = {
        "placement_type": "project_rule",
        "target_agent": "codex",
        "target_override": None,
        "scope_type": "project",
        "scope_value": "p1",
        "review_id": "RV-PS-E-0001",
        "experience_id": "E-0001",
        "policy_content": "Always run the test suite before declaring victory.",
    }
    base.update(overrides)
    return base


def test_apply_candidate_policy_renders_default_target(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    result = apply_candidate_policy(root, _spec(root))
    assert result["outcome"] == "applied"
    text = (root / "AGENTS.md").read_text("utf-8")
    assert "trajweave:managed" in text
    assert "Always run the test suite" in text


def test_apply_candidate_policy_is_idempotent(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    spec = _spec(root)
    first = apply_candidate_policy(root, spec)
    second = apply_candidate_policy(root, spec)
    assert first["outcome"] == "applied"
    assert second["outcome"] == "already applied"


def test_apply_candidate_policy_rejects_path_traversal(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    with pytest.raises(SafetyError, match="escapes approved location"):
        apply_candidate_policy(root, _spec(root, target_override="../outside.md"))


def test_apply_candidate_policy_rejects_symlink_escape(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "escape").symlink_to(outside)
    with pytest.raises(SafetyError):
        apply_candidate_policy(root, _spec(root, target_override="escape/AGENTS.md"))


def test_apply_candidate_policy_rejects_binary_target(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "AGENTS.md").write_bytes(b"\x00\x01binary")
    with pytest.raises(SafetyError, match="binary"):
        apply_candidate_policy(root, _spec(root))


def test_apply_candidate_policy_rejects_malformed_markers(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "AGENTS.md").write_text("<!-- trajweave:weird-syntax -->\nnot a real marker\n")
    with pytest.raises(SafetyError, match="malformed"):
        apply_candidate_policy(root, _spec(root))


def test_apply_candidate_policy_preserves_human_content(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "AGENTS.md").write_text("# My hand-written notes\n\nDo not touch this.\n")
    apply_candidate_policy(root, _spec(root))
    text = (root / "AGENTS.md").read_text("utf-8")
    assert "Do not touch this." in text
    assert "trajweave:managed" in text


def test_apply_candidate_policy_global_rule_stays_inside_workspace(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    result = apply_candidate_policy(
        root,
        _spec(root, placement_type="global_rule", scope_type=None, scope_value=None),
    )
    assert result["outcome"] == "applied"
    target = root / ".trajweave-eval-global" / "codex" / "AGENTS.md"
    assert target.exists()
    assert "trajweave:managed" in target.read_text("utf-8")
