from __future__ import annotations

from pathlib import Path

from trajweave.config.paths import get_paths


def test_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("TRAJWEAVE_HOME", str(tmp_path / "custom"))
    paths = get_paths()
    assert paths.home == (tmp_path / "custom").resolve()
    assert paths.db_path == paths.home / "trajweave.db"


def test_explicit_home_beats_env(monkeypatch, tmp_path):
    monkeypatch.setenv("TRAJWEAVE_HOME", str(tmp_path / "env"))
    paths = get_paths(tmp_path / "explicit")
    assert paths.home == (tmp_path / "explicit").resolve()


def test_ensure_creates_tree(tmp_path):
    paths = get_paths(tmp_path / "h").ensure()
    assert paths.home.is_dir()
    assert paths.logs_dir.is_dir()
    assert paths.cache_dir.is_dir()


def test_default_home_without_env(monkeypatch):
    monkeypatch.delenv("TRAJWEAVE_HOME", raising=False)
    assert get_paths().home == Path.home() / ".trajweave"
