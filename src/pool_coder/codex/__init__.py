"""OpenAI Codex support: rollout discovery, parsing and folding.

Read-only monitor over ``$CODEX_HOME/sessions/YYYY/MM/DD/rollout-*.jsonl``
(default ``~/.codex``). Sub-agent threads are separate rollout files that are
rolled up under their root thread. Selected with ``pool-coder --codex``.
"""
