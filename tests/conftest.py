"""Shared fixtures."""

from __future__ import annotations

import importlib

import pytest


def _clear_codex_caches() -> None:
    """Reset module-level caches so each test sees only its own files."""
    for name in ("pool_coder.codex.paths", "pool_coder.codex.overview"):
        try:
            module = importlib.import_module(name)
        except ImportError:
            continue
        clear = getattr(module, "clear_caches", None)
        if clear is not None:
            clear()


@pytest.fixture
def codex_home(tmp_path, monkeypatch):
    """A temporary ``$CODEX_HOME`` with an empty ``sessions/`` tree.

    Tests must never read the real ``~/.codex``; this points every Codex
    lookup at ``tmp_path/.codex`` and clears the per-path caches before and
    after the test.
    """
    home = tmp_path / ".codex"
    (home / "sessions").mkdir(parents=True)
    monkeypatch.setenv("CODEX_HOME", str(home))
    _clear_codex_caches()
    yield home
    _clear_codex_caches()


@pytest.fixture
def claude_projects(tmp_path, monkeypatch):
    """A temporary, empty ``~/.claude/projects``.

    Points the home directory at ``tmp_path`` so Claude Code lookups (and
    its credentials) never touch the real ``~/.claude``.
    """
    projects = tmp_path / ".claude" / "projects"
    projects.mkdir(parents=True)
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("POOL_CODER_CREDENTIALS", raising=False)
    return projects
