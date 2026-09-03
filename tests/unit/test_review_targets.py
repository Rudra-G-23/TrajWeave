from __future__ import annotations

import pytest

from trajweave.review.targets import SafetyError, apply_preview, build_preview, resolve_target


def _target(tmp_path, name="AGENTS.md"):
    root = tmp_path / "repo"
    root.mkdir()
    return root, resolve_target(
        placement_type="project_rule", project_root=root, agent="codex",
        target=name, global_root=None, review_id="RV-test",
    )


def test_preview_preserves_human_content_and_apply_is_idempotent(tmp_path):
    root, target = _target(tmp_path)
    path = root / "AGENTS.md"
    path.write_text("# Repository Instructions\n\nUse uv.\n\n# Architecture\nkeep this\n", "utf-8")
    preview = build_preview(target=target, experience_id="E-0001", review_id="RV-test", content="Run tests before committing.")
    assert "Use uv." in preview.after and "keep this" in preview.after
    assert "trajweave:managed" in preview.unified_diff
    assert apply_preview(preview) == "applied"
    first = path.read_text("utf-8")
    assert apply_preview(preview) == "already applied"
    assert path.read_text("utf-8") == first


def test_stale_preview_refuses_human_edit(tmp_path):
    root, target = _target(tmp_path)
    path = root / "AGENTS.md"
    path.write_text("# Human\n", "utf-8")
    preview = build_preview(target=target, experience_id="E-0001", review_id="RV-test", content="Keep builds green.")
    path.write_text("# Human edit\n", "utf-8")
    with pytest.raises(SafetyError, match="changed since preview"):
        apply_preview(preview)
    assert path.read_text("utf-8") == "# Human edit\n"


def test_target_rejects_escape_absolute_and_symlink(tmp_path):
    root, _ = _target(tmp_path)
    with pytest.raises(SafetyError):
        resolve_target(placement_type="project_rule", project_root=root, agent="codex", target="../outside", global_root=None, review_id="RV")
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "link").symlink_to(outside, target_is_directory=True)
    with pytest.raises(SafetyError):
        resolve_target(placement_type="project_rule", project_root=root, agent="codex", target="link/AGENTS.md", global_root=None, review_id="RV")


def test_malformed_binary_and_reserved_content_refuse(tmp_path):
    root, target = _target(tmp_path)
    path = root / "AGENTS.md"
    path.write_bytes(b"\x00binary")
    with pytest.raises(SafetyError, match="binary"):
        build_preview(target=target, experience_id="E-0001", review_id="RV-test", content="safe")
    path.write_text("<!-- trajweave:managed key=bad -->\n", "utf-8")
    with pytest.raises(SafetyError, match="malformed"):
        build_preview(target=target, experience_id="E-0001", review_id="RV-test", content="safe")
    path.write_text("", "utf-8")
    with pytest.raises(SafetyError, match="reserved"):
        build_preview(target=target, experience_id="E-0001", review_id="RV-test", content="<!-- trajweave:end -->")


def test_global_target_is_under_approved_root(tmp_path):
    approved = tmp_path / "global"
    approved.mkdir()
    target = resolve_target(placement_type="global_rule", project_root=None, agent="codex", target=None, global_root=approved, review_id="RV")
    assert target.path == approved / "AGENTS.md"
    with pytest.raises(SafetyError):
        resolve_target(placement_type="global_rule", project_root=None, agent="codex", target="/tmp/not-approved", global_root=approved, review_id="RV")


def test_global_default_and_skill_targets_are_safe_when_missing(tmp_path):
    approved = tmp_path / "home" / "policies" / "codex"
    global_target = resolve_target(placement_type="global_rule", project_root=None, agent="codex", target=None, global_root=approved, review_id="RV")
    assert global_target.path == approved / "AGENTS.md"
    root = tmp_path / "repo"
    root.mkdir()
    skill = resolve_target(placement_type="skill", project_root=root, agent="codex", target=None, global_root=None, review_id="RV-unsafe/../x")
    assert skill.path.name == "SKILL.md" and skill.path.is_relative_to(root)


@pytest.mark.parametrize("placement_type", ["global_rule", "scoped_rule", "skill"])
def test_each_writable_placement_renders_a_target(tmp_path, placement_type):
    root = tmp_path / "repo"
    root.mkdir()
    approved_global = tmp_path / "home" / "policies" / "codex"
    target = resolve_target(
        placement_type=placement_type,
        project_root=None if placement_type == "global_rule" else root,
        agent="codex",
        target="nested/AGENTS.md" if placement_type == "scoped_rule" else None,
        global_root=approved_global if placement_type == "global_rule" else None,
        review_id="RV-placement",
    )
    preview = build_preview(target=target, experience_id="E-0001", review_id="RV-placement", content="Use the verified workflow.")
    assert apply_preview(preview) == "applied"
    assert target.path.is_file()


def test_ignore_has_no_filesystem_target(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    with pytest.raises(SafetyError, match="no apply target"):
        resolve_target(placement_type="ignore", project_root=root, agent="codex", target=None, global_root=None, review_id="RV")
