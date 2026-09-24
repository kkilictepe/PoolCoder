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
