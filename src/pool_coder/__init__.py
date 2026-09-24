"""pool-coder — realtime terminal/web dashboard for Claude Code and Codex sessions.

Read-only monitor over local session transcripts: Claude Code's
``~/.claude/projects/<hash>/<session>.jsonl`` and its subagent/workflow
sidecars, or (``--codex``) OpenAI Codex's
``$CODEX_HOME/sessions/YYYY/MM/DD/rollout-*.jsonl`` and its sub-agent threads.
The agent-specific pieces sit behind ``providers.get_provider(config.agent)``.
Entry point: ``pool_coder.cli:main``.
"""

__version__ = "0.1.0"
