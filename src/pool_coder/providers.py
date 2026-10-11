"""Agent providers: the seam between the shared core and each agent's files.

A ``Provider`` bundles the few agent-specific edge pieces: finding sessions,
previewing them for a picker, building the fold + transcript source for one
session, and the plan-limit source. Everything downstream (``SessionState``,
snapshots, renderers) is shared, so choosing an agent is choosing providers
(``--agent all`` chooses every one):

    list_sessions(providers_for(config.agent))

Each listed ``SessionInfo`` names its own agent, and ``get_provider(info.agent)``
is the provider that previews and reads it, whichever were selected.

``CLAUDE`` wraps the existing Claude Code modules unchanged; ``CODEX`` wraps
``pool_coder.codex`` and ``sources.codex_limits``. The wrappers look module
functions up at call time, so tests can monkeypatch the underlying modules.
Nothing here imports the UI, so the headless paths stay UI-free.
"""

from __future__ import annotations

from collections.abc import Iterable
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

ALL = "all"  # the --agent value that selects every provider at once
AGENT_CHOICES = (*PROVIDERS, ALL)


def get_provider(name: str | None) -> Provider:
    """The provider for one agent; ``None``/``""`` means Claude."""
    if not name:
        return CLAUDE
    prov = PROVIDERS.get(str(name).strip().lower())
    if prov is None:
        raise ValueError(f"unknown agent {name!r} (choose from: {', '.join(PROVIDERS)})")
    return prov


def providers_for(agent: str | None) -> tuple[Provider, ...]:
    """The providers an ``--agent`` value selects: one, or every one for ``all``."""
    if isinstance(agent, str) and agent.strip().lower() == ALL:
        return tuple(PROVIDERS.values())
    return (get_provider(agent),)


def agent_label(agent: str | None) -> str:
    """Human name of an ``--agent`` value: ``Codex``, or ``Claude Code + Codex``."""
    return " + ".join(prov.label for prov in providers_for(agent))


def agent_prefix(agent: str | None) -> str:
    """The agent named before "session(s)" in messages (``"Codex "``): none for
    Claude Code, the default, nor for ``all``, where any session counts."""
    provs = providers_for(agent)
    return "" if len(provs) > 1 or provs[0] is CLAUDE else f"{provs[0].label} "


def list_sessions(providers: Iterable[Provider]) -> list[SessionInfo]:
    """Every provider's top-level sessions, merged newest first."""
    providers = tuple(providers)
    if len(providers) == 1:
        return providers[0].list_sessions()
    sessions = [info for prov in providers for info in prov.list_sessions()]
    sessions.sort(key=lambda info: info.mtime, reverse=True)
    return sessions


def find_session(providers: Iterable[Provider], session_id: str) -> SessionInfo | None:
    """The session with this id from the first provider that has it."""
    for prov in providers:
        info = prov.find_session(session_id)
        if info is not None:
            return info
    return None
