"""Agent providers: the seam between the shared core and each agent's files.

A ``Provider`` bundles the few agent-specific edge pieces: finding sessions,
previewing them for a picker, building the fold + transcript source for one
session, and the plan-limit source. Everything downstream (``SessionState``,
snapshots, renderers) is shared, so choosing an agent is choosing a provider:

    get_provider(config.agent).list_sessions()

``CLAUDE`` wraps the existing Claude Code modules unchanged; ``CODEX`` wraps
``pool_coder.codex`` and ``sources.codex_limits``. The wrappers look module
functions up at call time, so tests can monkeypatch the underlying modules.
Nothing here imports the UI, so the headless paths stay UI-free.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import aggregator, overview, paths
from .codex import aggregator as codex_aggregator
from .codex import discovery as codex_discovery
from .codex import overview as codex_overview
from .codex import parser as codex_parser
from .codex import paths as codex_paths
from .config import Config
from .overview import SessionOverview
from .paths import SessionInfo
from .sources import codex_limits, jsonl_source, plan_limits
from .state import SessionState


@dataclass(frozen=True)
class Provider:
    name: str            # "claude" | "codex" (the --agent value)
    label: str           # human name: "Claude Code" | "Codex"
    version_tag: str     # prefix of the version in --once output: "cc" | "codex"
    list_sessions: Callable[[], list[SessionInfo]]            # top-level sessions, newest first
    find_session: Callable[[str], SessionInfo | None]
    peek_session: Callable[..., SessionOverview]              # (info, config=None)
    open_session: Callable[[SessionState, Config], tuple]     # -> (fold, JsonlSource)
    make_plan_source: Callable[[], object]                    # PlanLimitsSource-shaped
    # the id find_session actually matches, so one session is one cache key
    normalize_id: Callable[[str], str] = lambda session_id: session_id  # noqa: E731


# -- Claude Code ---------------------------------------------------------------
def _open_claude(state: SessionState, config: Config) -> tuple:
    agg = aggregator.Aggregator(state, config)
    return agg, jsonl_source.JsonlSource(Path(state.main_path), agg)


CLAUDE = Provider(
    name="claude",
    label="Claude Code",
    version_tag="cc",
    list_sessions=lambda: paths.list_sessions(),
    find_session=lambda session_id: paths.find_session(session_id),
    peek_session=lambda info, config=None: overview.peek_session(info, config),
    open_session=_open_claude,
    make_plan_source=lambda: plan_limits.PlanLimitsSource(),
)


# -- OpenAI Codex ----------------------------------------------------------------
def _open_codex(state: SessionState, config: Config) -> tuple:
    agg = codex_aggregator.CodexAggregator(state, config)
    # Sub-agent threads are separate rollouts found by the Codex discovery;
    # the drain loop (and its RESET handling) is the shared one.
    discovery = codex_discovery.CodexDiscovery(state.main_path, state.session_id)
    source = jsonl_source.JsonlSource(Path(state.main_path), agg, discovery=discovery,
                                      parse=codex_parser.parse_codex_line)
    return agg, source


CODEX = Provider(
    name="codex",
    label="Codex",
    version_tag="codex",
    list_sessions=lambda: codex_paths.list_sessions(),  # sub-agent threads hidden
    find_session=lambda session_id: codex_paths.find_session(session_id),
    peek_session=lambda info, config=None: codex_overview.peek_codex_session(info, config),
    open_session=_open_codex,
    make_plan_source=lambda: codex_limits.CodexLimitsSource(),
    normalize_id=lambda session_id: codex_paths.normalize_id(session_id),  # case-insensitive
)


PROVIDERS: dict[str, Provider] = {"claude": CLAUDE, "codex": CODEX}


def get_provider(name: str | None) -> Provider:
    """The provider for an ``--agent`` value; ``None``/``""`` means Claude."""
    if not name:
        return CLAUDE
    prov = PROVIDERS.get(str(name).strip().lower())
    if prov is None:
        raise ValueError(f"unknown agent {name!r} (choose from: {', '.join(PROVIDERS)})")
    return prov
