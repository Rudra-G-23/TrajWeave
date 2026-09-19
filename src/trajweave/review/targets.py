"""Pure target resolution and security-sensitive policy rendering.

The module deliberately knows nothing about SQLite.  A preview is a complete
description of the bytes that an explicit Apply may replace.  All paths are
canonicalised and checked before a file is read or written.
"""

from __future__ import annotations

import difflib
import hashlib
import os
import re
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class SafetyError(ValueError):
    """A target or rendered policy failed a safety check."""


_START = re.compile(r'^<!-- trajweave:managed key="([A-Za-z0-9._-]+)" -->$', re.MULTILINE)
_END = "<!-- trajweave:end -->"
_MARKER_PREFIX = "<!-- trajweave:"
_AGENT_FILE = {"codex": "AGENTS.md", "claude": "CLAUDE.md"}


@dataclass(frozen=True)
class TargetSpec:
    placement_type: str
    agent: str | None
    path: Path
    kind: str
    project_root: Path | None = None
    scope_type: str | None = None
    scope_value: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "placement_type": self.placement_type,
            "agent": self.agent,
            "path": str(self.path),
            "kind": self.kind,
            "project_root": str(self.project_root) if self.project_root else None,
            "scope_type": self.scope_type,
            "scope_value": self.scope_value,
        }


def _no_nul(value: str, label: str) -> None:
    if "\x00" in value:
        raise SafetyError(f"{label} contains a NUL byte")


def _inside(path: Path, root: Path, label: str) -> Path:
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise SafetyError(f"{label} escapes approved location: {path}") from exc
    return path


def _canonical_root(root: str | Path, label: str, *, allow_missing: bool = False) -> Path:
    value = Path(root).expanduser()
    if not value.is_absolute():
        raise SafetyError(f"{label} must be absolute")
    try:
        resolved = value.resolve(strict=not allow_missing)
    except (OSError, RuntimeError) as exc:
        raise SafetyError(f"{label} does not exist: {value}") from exc
    if not resolved.exists() and allow_missing:
        parent = resolved
        while not parent.exists() and parent != parent.parent:
            parent = parent.parent
        if not parent.is_dir():
            raise SafetyError(f"{label} parent is not a directory: {parent}")
        return resolved
    if not resolved.is_dir():
        raise SafetyError(f"{label} is not a directory: {resolved}")
    return resolved


def _canonical_target(raw: str | Path, root: Path, label: str) -> Path:
    text = os.fspath(raw)
    _no_nul(text, label)
    candidate = Path(text).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        resolved = candidate.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise SafetyError(f"could not resolve {label}: {candidate}") from exc
    return _inside(resolved, root, label)


def safe_slug(value: str) -> str:
    """Return a deterministic, path-safe skill directory component."""

    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip(".-")[:60]
    return cleaned or "policy"


def managed_key(experience_id: str, placement_type: str, target: TargetSpec) -> str:
    raw = "|".join((experience_id, placement_type, target.agent or "", str(target.path)))
    return "TW-POLICY-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def resolve_target(
    *,
    placement_type: str,
    project_root: str | Path | None,
    agent: str | None,
    target: str | Path | None,
    global_root: str | Path | None,
    review_id: str,
    scope_type: str | None = None,
    scope_value: str | None = None,
) -> TargetSpec:
    """Resolve a reviewed placement into exactly one safe target."""

    if placement_type == "ignore":
        raise SafetyError("Ignore has no apply target")
    if agent not in _AGENT_FILE:
        raise SafetyError("an explicit target agent is required (codex or claude)")

    if placement_type == "global_rule":
        if not global_root:
            raise SafetyError("Global Rule requires an approved global policy root")
        root = _canonical_root(global_root, "global policy root", allow_missing=True)
        raw = target or (root / _AGENT_FILE[agent])
        path = _canonical_target(raw, root, "global target")
        return TargetSpec(placement_type, agent, path, "global", root, scope_type, scope_value)

    if not project_root:
        raise SafetyError("a registered project is required for this placement")
    root = _canonical_root(project_root, "registered project root")
    if placement_type in {"project_rule", "scoped_rule"}:
        raw = target or (root / _AGENT_FILE[agent])
        path = _canonical_target(raw, root, "repository target")
        return TargetSpec(placement_type, agent, path, "policy", root, scope_type, scope_value)

    if placement_type == "skill":
        if target:
            path = _canonical_target(target, root, "skill target")
            if path.name != "SKILL.md":
                raise SafetyError("a Skill target must be named SKILL.md")
        else:
            path = _canonical_target(root / "skills" / safe_slug(review_id) / "SKILL.md", root, "skill target")
        return TargetSpec(placement_type, agent, path, "skill", root, scope_type, scope_value)

    raise SafetyError(f"unsupported placement type: {placement_type}")


def _secure_parent_fd(root: Path, parent: Path, *, create: bool) -> int:
    """Open a target parent by walking every component with O_NOFOLLOW."""

    root = root.resolve(strict=False)
    parent = parent.resolve(strict=False)
    try:
        parent.relative_to(root)
    except ValueError as exc:
        raise SafetyError(f"target parent escapes approved location: {parent}") from exc

    anchor = root
    while not anchor.exists():
        anchor = anchor.parent
    try:
        fd = os.open(anchor, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        raise SafetyError(f"cannot securely open approved directory: {anchor}") from exc
    try:
        components = list(root.relative_to(anchor).parts)
        components += list(parent.relative_to(root).parts)
        for component in components:
            if create:
                try:
                    os.mkdir(component, 0o755, dir_fd=fd)
                except FileExistsError:
                    pass
            try:
                next_fd = os.open(component, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0), dir_fd=fd)
            except OSError as exc:
                try:
                    os.stat(component, dir_fd=fd, follow_symlinks=False)
                except FileNotFoundError:
                    raise FileNotFoundError(component) from exc
                raise SafetyError(f"approved directory component is not safely traversable: {component}") from exc
            os.close(fd)
            fd = next_fd
        return fd
    except Exception:
        os.close(fd)
        raise


def _read_existing(path: Path, root: Path) -> tuple[str, bytes | None]:
    """Read a target while refusing symlinks in every path component."""

    try:
        parent_fd = _secure_parent_fd(root, path.parent, create=False)
    except FileNotFoundError:
        return "", None
    try:
        try:
            target_stat = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return "", None
        if not stat.S_ISREG(target_stat.st_mode):
            raise SafetyError(f"target is not a regular file: {path}")
        try:
            fd = os.open(path.name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent_fd)
            with os.fdopen(fd, "rb") as handle:
                data = handle.read()
        except OSError as exc:
            raise SafetyError(f"cannot read target: {path}") from exc
        if b"\x00" in data:
            raise SafetyError(f"target is binary or contains NUL bytes: {path}")
        return data.decode("utf-8"), data
    except UnicodeDecodeError as exc:
        raise SafetyError(f"target is not valid UTF-8: {path}") from exc
    except OSError as exc:
        raise SafetyError(f"cannot read target: {path}") from exc
    finally:
        os.close(parent_fd)


def _validate_markers(existing: str) -> list[tuple[str, int, int]]:
    starts = list(_START.finditer(existing))
    if _MARKER_PREFIX in existing and len(starts) == 0:
        raise SafetyError("target contains malformed TrajWeave markers")
    blocks: list[tuple[str, int, int]] = []
    cursor = 0
    for match in starts:
        if match.start() < cursor:
            raise SafetyError("target contains overlapping TrajWeave markers")
        end = existing.find(_END, match.end())
        if end < 0 or _START.search(existing, match.end(), end) is not None:
            raise SafetyError("target contains an unterminated TrajWeave managed block")
        blocks.append((match.group(1), match.start(), end + len(_END)))
        cursor = end + len(_END)
    if existing.count(_END) != len(blocks):
        raise SafetyError("target contains an unmatched TrajWeave end marker")
    return blocks


def _line_ending(existing: str) -> str:
    return "\r\n" if "\r\n" in existing and existing.count("\r\n") >= existing.count("\n") / 2 else "\n"


def _block(key: str, content: str, newline: str) -> str:
    if _MARKER_PREFIX in content or _END in content:
        raise SafetyError("approved content contains reserved TrajWeave marker syntax")
    body = content.replace("\r\n", "\n").replace("\r", "\n").replace("\n", newline).strip()
    return newline.join((f'<!-- trajweave:managed key="{key}" -->', body, _END))


def render_target(existing: str, *, key: str, content: str, target: TargetSpec, review_id: str) -> str:
    """Return the exact replacement text, preserving all human content."""

    newline = _line_ending(existing)
    blocks = _validate_markers(existing)
    matches = [item for item in blocks if item[0] == key]
    if len(matches) > 1:
        raise SafetyError("target contains duplicate managed blocks for this policy")
    replacement = _block(key, content, newline)
    if matches:
        _, start, end = matches[0]
        return existing[:start] + replacement + existing[end:]

    prefix = existing
    if target.kind == "skill" and not existing:
        name = safe_slug(review_id)
        prefix = f"---{newline}name: {name}{newline}description: TrajWeave reviewed policy{newline}---{newline}"
    if prefix and not prefix.endswith(("\n", "\r")):
        prefix += newline
    if prefix and not prefix.endswith(newline * 2):
        prefix += newline
    return prefix + replacement + newline


@dataclass(frozen=True)
class Preview:
    target: TargetSpec
    key: str
    before: str
    after: str
    target_hash: str | None
    output_hash: str
    unified_diff: str


def build_preview(*, target: TargetSpec, experience_id: str, review_id: str, content: str) -> Preview:
    if target.project_root is None:
        raise SafetyError("target has no approved root")
    existing, raw = _read_existing(target.path, target.project_root)
    key = managed_key(experience_id, target.placement_type, target)
    after = render_target(existing, key=key, content=content, target=target, review_id=review_id)
    before_hash = hashlib.sha256(raw).hexdigest() if raw is not None else None
    output_hash = hashlib.sha256(after.encode("utf-8")).hexdigest()
    diff = "".join(difflib.unified_diff(
        existing.splitlines(keepends=True), after.splitlines(keepends=True),
        fromfile=str(target.path), tofile=str(target.path),
    ))
    return Preview(target, key, existing, after, before_hash, output_hash, diff)


def _atomic_replace(path: Path, data: bytes, root: Path) -> None:
    """Replace one regular file using a directory fd and a no-follow policy."""

    try:
        parent_fd = _secure_parent_fd(root, path.parent, create=True)
    except (FileNotFoundError, OSError) as exc:
        raise SafetyError(f"cannot securely open target directory: {path.parent}") from exc
    temp_name = f".{path.name}.trajweave-{os.getpid()}-{next(tempfile._get_candidate_names())}"
    try:
        try:
            target_stat = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
            if not stat.S_ISREG(target_stat.st_mode):
                raise SafetyError(f"target changed to a non-regular file: {path}")
        except FileNotFoundError:
            pass
        fd = os.open(temp_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644, dir_fd=parent_fd)
        try:
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view):]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(temp_name, path.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        os.fsync(parent_fd)
    except SafetyError:
        raise
    except OSError as exc:
        try:
            os.unlink(temp_name, dir_fd=parent_fd)
        except OSError:
            pass
        raise SafetyError(f"atomic replacement failed for {path}") from exc
    finally:
        os.close(parent_fd)


def apply_preview(preview: Preview) -> str:
    """Apply a previously built preview after rechecking its target hash."""

    if preview.target.project_root is None:
        raise SafetyError("target has no approved root")
    current, raw = _read_existing(preview.target.path, preview.target.project_root)
    current_hash = hashlib.sha256(raw).hexdigest() if raw is not None else None
    if current_hash == preview.output_hash and current == preview.after:
        return "already applied"
    if current_hash != preview.target_hash:
        raise SafetyError("target changed since preview; create a new preview before applying")
    if current == preview.after:
        return "already applied"
    _atomic_replace(preview.target.path, preview.after.encode("utf-8"), preview.target.project_root)
    return "applied"
