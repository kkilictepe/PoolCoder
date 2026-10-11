# Plan: Codex support for pool-coder (`--agent codex` / `--codex`)

> **Status:** implemented on branch `codex` (2026-09-24). Where the code deviates from this plan (fork-history filter, idle-until-first-task sub-agents, expired rate-limit windows, agent-aware pricing), the module docstrings and README explain why.
> **Drafted:** 2026-09-24, against commit `373c0e0`, plus a read-only survey of a local `~/.codex` (Codex 0.155).

## Context

pool-coder is a read-only live dashboard (Textual TUI, `--serve` web UI, `--once`, `--list`).
Today it only understands **Claude Code** transcripts under `~/.claude/projects`. The goal is to
give **OpenAI Codex** sessions (CLI, VS Code extension, Desktop) the same dashboard.

**Decisions:**
- A new option `--agent {claude,codex}`, default `claude`, plus a `--codex` shorthand.
- It works in every mode: picker, `--session`, `--list`, `--once [--json]`, `--serve`.
- One agent per run. The picker does not mix Claude and Codex sessions.
- Running with no flag must behave exactly as it does today.

The core pipeline (`tailer → fold → SessionState → Snapshot → TUI/web`) is already decoupled from Claude. Only these edge pieces are Claude-specific:
- **Session discovery:** `paths.py`
- **Record parsing and folding:** `parser.py`, `aggregator.py`, `discovery.py`
- **Picker preview:** `overview.py`
- **Plan-limit source:** `sources/plan_limits.py`

The plan adds an **agent provider seam** and a Codex implementation of each of those pieces. `SessionState`, `Snapshot` and the renderers stay shared.

## Facts about Codex rollouts that shape the design

These were checked read-only against a real `~/.codex` on Windows 11 and against the openai/codex source.

- **Layout:** `$CODEX_HOME` (default `~/.codex`), then `sessions/YYYY/MM/DD/rollout-<local-ts>-<uuidv7>.jsonl`.
  - Each line has the shape `{"timestamp","ordinal"?,"type","payload"}`.
  - The record types used here are `session_meta`, `turn_context`, `response_item`, `event_msg`, `compacted` and `token_usage_record`.
- **File modification times are unreliable on Windows.**
  - Some active files keep their creation time while they grow; others update.
  - Rewritten files can have a creation time later than their last-write time.
  - So activity time is taken as the later of mtime and the last record's timestamp.
- **Read access is verified.** CPython's read-only `open(path, "rb")` reads files Codex is still writing. A .NET `File.ReadLines` fails on the same files with a sharing violation. pool-coder's existing tailer approach is safe.
- **Codex rewrites files, not just appends:**
  - A resumed session appends to its original file, which stays in its original date folder.
  - Finished files can be rewritten in place during a migration.
  - Files idle for 7 days or more are compressed to `.jsonl.zst`.
- **Sub-agent threads are separate rollout files, and there are many** (131 of 159 on the surveyed machine).
  - Line 1 is the thread's own `session_meta`, about 20 KB, and can be larger. It carries `id`, `session_id` (the root thread), `parent_thread_id`, `thread_source` (`user`, `subagent`, `guardian_review`, `memory_consolidation`), and `source` (a string, or `{"subagent":…}`).
  - Forked children also contain copies of the parent's records, including a second `session_meta`.
- **Token usage:**
  - `token_usage_record` holds per-response `usage`, with a unique `response_id` and a `thread_id`.
  - The cumulative totals (`thread_token_usage`, and `token_count.total_token_usage`) include the parent's totals in forked children, so they must never be summed.
  - `event_msg.token_count` repeats and has duplicates; it is the fallback for older versions only.
- **Token field semantics:** `cached_input_tokens` and `cache_write_input_tokens` are part of `input_tokens`, and `reasoning_output_tokens` is part of `output_tokens`. Anthropic's input count excludes cache tokens, so the fields must be mapped to match.
- **Context window:** `model_context_window` = **258,400** (272K × 95%), found in `token_count.info` and `task_started`. Automatic compaction was observed at about 245K (0.9 × 272K), which is about 0.947 of the reported window.
- **Compaction:** the `compacted` record is written in every format. It is the single compaction marker we use.
- **Tool calls:**
  - With gpt-5.6, tools run in "code mode": a `custom_tool_call` named `exec` whose input is JavaScript calling `tools.exec_command({"cmd":…})`, `tools.apply_patch(...)`, `tools.web__run(...)` and so on.
  - Collaboration tools appear as a `function_call` (`spawn_agent`, `wait_agent`, …).
  - The legacy formats are `function_call` `shell`/`apply_patch`/`update_plan` and `local_shell_call`.
  - Structured results are in `event_msg.item_completed`: `CommandExecution`, which has `exit_code` and `parsed_cmd`, and `FileChange`, which lists changed file paths. Legacy files use `patch_apply_end` instead.
- **Prompts** come from `item_completed{UserMessage}` or, in legacy files, `event_msg.user_message`. The prompt text can be wrapped in an IDE preamble ending in `## My request…:`.
- **Rate limits** are in `token_count.rate_limits`: `{primary, secondary, plan_type, credits, …}`.
  - Each window is `{used_percent, window_minutes, resets_at}`; older versions use `resets_in_seconds`.
  - Classify a window by `window_minutes`, not by whether it is primary or secondary.
  - On a business plan both windows are null and credits are unlimited (verified locally).
- **Models seen:** `gpt-5.6-sol/terra`, `gpt-5.5` and `codex-auto-review` (the guardian reviewer, which has no public price). GPT-6 Astra/Sol/Luna launched on 2026-09-22.

## Design

### 1. The seam
- **`Config.agent: str = "claude"`** (`config.py`). `Config` already reaches every call site, so no function signatures change and every existing `Config()` stays on Claude.
- **New `src/pool_coder/providers.py`**: a frozen `Provider` dataclass and `get_provider(name)`. It must not import anything from the UI.
  ```python
  name, label, version_tag                      # "codex", "Codex", "codex"
  list_sessions() / find_session(id) / peek_session(info, config)
  open_session(state, config) -> (fold, JsonlSource)
  make_plan_source() -> PlanLimitsSource-shaped (.view/.start/.stop/.fetch_once)
  ```
  - `CLAUDE` wraps the existing functions and doesn't change behaviour.
  - `CODEX` wraps the new modules below.
- **`JsonlSource`** (`sources/jsonl_source.py`) gets keyword-only `discovery=None` and `parse=parse_line`. The defaults are today's behaviour, and Codex reuses the tested drain loop, including RESET handling. `sources/base.py` gains a `FoldTarget` Protocol with `apply`, `reset_source`, `register_subagent` and `register_workflow`.
- **Shared state methods on `SessionState`**: `record_usage(src, key, usage, model)`, `drop_source_tokens(src)`, `reset_main()` and `update_context(usage, ts, detect_drop=True)`.
  - The Claude `Aggregator` is refactored to call them. The refactor is mechanical and `tests/test_aggregator.py` covers it.
  - `CodexAggregator` is a **separate class, not a subclass**, because Claude's rules, such as "parent tool done ⇒ subagent done", don't apply to Codex.

### 2. Shared model changes
Every new field has a default, so Claude's output stays the same.
- `models.UsageTokens.reasoning: int = 0`: shown only, never billed separately, because it is already part of `output`. It is included in `__add__`.
- `state.SessionState`: new fields `agent="claude"`, `context_window=0` (the window the transcript reports), and `auto_compact_fraction=AUTO_COMPACT_FRACTION`.
- `snapshot.py`:
  - `SessionSnapshot.agent` goes last and defaults to `"claude"`.
  - `window = state.context_window or config.window_for(...)`, so the reported window skips Claude's 200K→1M auto-bump.
  - `auto_compact_at = window * state.auto_compact_fraction`.
  - `PlanLimitsView` gains `as_of`, `plan_type`, `note`, `five_hour_label="5-hour"` and `seven_day_label="weekly"`.
- `config.py`:
  - `model_family()` returns `"gpt"` for ids containing `gpt` or `codex`, or matching `^o\d`.
  - `DEFAULT_WINDOWS["gpt"] = 272_000`, used only when no window is reported.
  - `window_for` never auto-bumps the gpt family.
  - New constants `CODEX_AUTO_COMPACT_FRACTION = 0.947` (with a comment explaining where the number comes from) and `CODEX_LIMITS_INTERVAL = 30`.
- `format.py`:
  - `short_model(m)`: Claude names keep today's `split('-')[1]`; GPT names drop the `gpt-` prefix, so `gpt-5.6-sol` becomes `5.6-sol`.
  - `short_id(sid, agent)`: returns the **last** 8 characters for Codex, because UUIDv7 ids created within about a minute share the same first 8.

### 3. Pricing (`pricing.py`, mirrored in `pricing.toml`)
- `rate_for(model)` tries the **longest matching model-id prefix** among the non-family keys first, then the family, then `default`. Claude only has family keys, so its lookups don't change.
- The embedded TOML gains OpenAI Standard-tier prices (USD per 1M tokens, 2026-09):

  | Model(s) | Input | Output | Cache read | Cache write |
  |---|---|---|---|---|
  | `gpt-6-astra` | 10 | 50 | 1 | 12.5 |
  | `gpt-6-sol` | 2 | 10 | 0.2 | 2.5 |
  | `gpt-6-luna` | 0.1 | 0.5 | 0.01 | 0.125 |
  | `gpt-5.6-sol` | 4 | 20 | 0.4 | 5 |
  | `gpt-5.6-terra` | 2 | 12 | 0.2 | 2.5 |
  | `gpt-5.6-luna` | 0.2 | 1.2 | 0.02 | 0.25 |
  | `gpt-5.5` | 5 | 30 | 0.5 | – |
  | `gpt-5.4` | 2.5 | 15 | 0.25 | – |
  | `gpt-5.3-codex`, `gpt-5.2(-codex)` | 1.75 | 14 | 0.175 | – |
  | `gpt-5(.1)(-codex)` | 1.25 | 10 | 0.125 | – |
  | `gpt-5-mini`, `gpt-5.1-codex-mini` | 0.25 | 2 | 0.025 | – |
  | `codex-mini-latest` | 1.5 | 6 | 0.375 | – |
  | `o4-mini` | 1.1 | 4.4 | 0.275 | – |
  | `o3` | 2 | 8 | 0.5 | – |

- A **`[gpt]` fallback** uses gpt-6-sol's rates. `_from_toml_text` also `setdefault`s `gpt` so that a custom `--pricing` file without it doesn't price GPT at Opus rates.
- Cost is an estimate at API prices. It ignores fast/priority mode multipliers and is only notional on subscription plans.

### 4. New `src/pool_coder/codex/` package
- **`paths.py`**
  - `codex_home()`: `CODEX_HOME`, else `USERPROFILE`/`HOME` + `.codex`.
  - `ROLLOUT_RE` matches the thread UUID and an optional `_<rollout_id>` suffix. `.jsonl.zst` files and `archived_sessions/` are skipped.
  - `read_session_meta(path)`: reads the first line only, up to 1 MiB, never raises, and is cached per path (the first line is written once). It returns a `CodexMeta` with `is_subagent`, which is true for a subagent, guardian or memory `thread_source`, a `source` dict containing `subagent` or `internal`, or a set `parent_thread_id`.
  - **Activity time without mtime:** `last_activity(path, st)` looks for the last line-leading `{"timestamp":"…"` in the file's last 16 KB, widening to 256 KB if needed, otherwise falls back to mtime. Results are cached by `(path, size)`. `SessionInfo.mtime` is set to that time, so the existing sort, 30-minute filter, `fmt_age` and `is_live` all keep working unchanged.
  - `list_sessions(include_subagents=False)` returns sessions newest-first, deduplicated by thread id, with `project_hash` set to the date folder.
  - `find_session(sid)` globs `*/*/*/rollout-*-{sid}*.jsonl`.
  - `thread_names()` reads `session_index.jsonl`, where the last entry for each id wins.
- **`parser.py`**
  - `CodexRecord` view and `parse_codex_line()`, which returns `None` for bad input.
  - `usage_from(u)` fills `UsageTokens` as follows. The result is that `context_tokens` equals the prompt's `input_tokens`:

    | `UsageTokens` field | Computed from |
    |---|---|
    | `input` | `input − cached − cache_write` |
    | `cache_read` | `cached` |
    | `cache_creation` | `cache_write` |
    | `output` | `output` |
    | `reasoning` | `reasoning_output_tokens` |
  - Call description helpers:
    - `exec_inner_calls(js)` uses a regex over `tools.<name>(`. It maps `exec_command` to `shell` (label from the `"cmd"` value), `apply_patch` to its first `*** Update|Add|Delete File:` path, and `web__run` to `web_search` (label from the query).
    - `shell_label(cmd_list)`: turns `bash -lc X` into `X` and uses the last argument for pwsh/cmd.
    - `patch_files`: the file paths a patch touches.
    - `describe_call(name, args)`: covers the legacy calls (shell, exec_command, apply_patch, update_plan with the step in progress) and collaboration calls such as `spawn_agent`.
  - `output_failed(out)`: true for `Script failed`, a non-zero `metadata.exit_code`, or `Exit code: N` where N ≠ 0.
  - `clean_codex_prompt(text)`: keeps the text after the last `## My request…:`, strips `<environment_context>` and similar tags, then calls `clean_prompt`.
- **`aggregator.py`**: the `CodexAggregator` fold, detailed in the next section. It sets `state.agent="codex"` and `state.auto_compact_fraction`.
- **`discovery.py`**: `CodexDiscovery(main_path, session_id)`.
  - `initial()` returns the main file plus its children; `scan()` runs every 1.5 s.
  - Children are found in day folders from the main file's day to today (at most 8 days), where the cached meta has `session_id == root` or a `parent_thread_id` already in the known thread set. This repeats so grandchildren are found too.
  - Each child is emitted as `TailerSpec("agent:<tid>")` plus a `SubagentReg`. The agent type is the last segment of `agent_path` (or `guardian`), and the description is the nickname plus the path.
- **`overview.py`**: `peek_codex_session(info, config)` returns the existing `SessionOverview`.
  - The cwd and branch come from the meta; the context, window, model and last prompt come from a 256 KB tail.
  - If the tail has no `turn_context`, the model is taken from the head (the first 256 KB, cached).
  - If there is no prompt, the text falls back to the thread name.
  - Results are cached by `(path, size)` because the web list page refreshes every 3 s.

### 5. The Codex fold (`CodexAggregator.apply(source_id, record)`)

**Tokens**, for both the main and child sources:
- **Primary: `token_usage_record`.** Skip the record if `thread_id` is not the source's own thread. Otherwise call `record_usage(src, response_id, usage_from(usage), model or "gpt")`.
  - The first such record in a source switches that source to this mode and removes any fallback keys already recorded for it.
  - Keying by `response_id` makes replays and duplicates safe, and the model saved with each key gives the per-model breakdown.
- **Fallback: `token_count`** (older Codex versions). Take the component-wise difference of `total_token_usage` from the previous one (the ccusage approach). Skip differences that are zero or negative. In a forked child, the first total is only a baseline.
- **Always:** `token_count.info.model_context_window` or `task_started.model_context_window` updates `state.context_window`.
- **Turns** = the number of distinct model responses in `main`, the same idea as Claude's per-response count. A child's `sub.turns` is counted the same way.
- **Context gauge** = the latest real response's prompt `input_tokens` (records with 0 input are skipped), passed to `update_context(detect_drop=False)`. This means the same thing as on the Claude side, and reads within a few points of Codex's own "% left".

**Main-source records:**

| Record | Effect |
|---|---|
| `session_meta` whose `id` is this session's | `cwd`, `git_branch` (`git.branch`), `version` (`cli_version`). Copies of the parent's meta are ignored. |
| `turn_context`, `thread_settings_applied` | `model`, `cwd`, and `mode`, e.g. `plan · ultra · full-access` (collaboration mode, effort, short sandbox name). |
| `compacted` | Append a `CompactionEvent(before=current context)` and push `⟳`. `after` is filled in by the next response. This is the only compaction marker used, so nothing is counted twice. |
| `item_completed{UserMessage}`, `event_msg.user_message` | Deduplicate by `client_id`. Set `last_prompt` (via `clean_codex_prompt`) and `user_messages`, and push `»`. |
| `response_item` message with role `assistant` | Push `↳ text`. `AgentMessage` and `agent_message` duplicates are ignored. |
| `response_item` reasoning | Push `✎ summary` only if a summary exists; otherwise the content is encrypted. |
| `custom_tool_call` / `function_call` / `local_shell_call` | Create a `ToolStatus(ToolUse(call_id, display_name, {"command"/"path"/…: label}))` and push `→`. `exec` adds one count per inner call; other calls count by display name. |
| `*_output` | Mark the tool done and set `is_error` from `output_failed`; errors add to `tool_errors`. Push `✓`/`✗`. |
| `item_completed{CommandExecution}` | A failure or non-zero exit adds to `tool_errors` and pushes `✗ shell <cmd>`. `parsed_cmd` read paths are added to files touched. |
| `item_completed{FileChange}`, `patch_apply_end`, legacy `apply_patch` | Add the changed paths to `files_touched`, deduplicated by id. |
| `task_complete`, `turn_aborted` | Close all open tools so nothing stays "in flight" forever. An abort pushes `⊘`. |
| `SubAgentActivity` (item or legacy event) | `started` registers the subagent and pushes `⊕`; `completed` pushes `⊙`, `interrupted` pushes `⊘`. |

**Child sources (`agent:<tid>`):**
- Tokens and turns work as above, using the child's own model.
- Tool calls and their outputs update `open_tools` and `last_tool`.
- `task_started` sets `finished=False`; `task_complete` or `turn_aborted` sets `finished=True`.

**`reset_source`:**
- Every source: `drop_source_tokens(src)`, and reset that source's fallback baseline and token mode.
- `main`: also `reset_main()` and clear the prompt, seen-id and meta dedupe sets.
- A child: set turns to 0, clear `open_tools`, set `finished=False`.

So a RESET followed by a replay (rotation, or a migration rewrite) produces the same state.

### 6. Codex plan limits (`src/pool_coder/sources/codex_limits.py`)
`CodexLimitsSource` has the same API as `PlanLimitsSource`, runs on a daemon thread, and polls local files every 30 s. It makes no network calls and never reads `auth.json`.
- **Where the data comes from:** the 8 most recently active rollouts (by activity time), including sub-agents, because limits apply to the whole account. It uses the last `token_count` that has `rate_limits`, and `as_of` is that record's timestamp.
- `parse_rate_limits(rl, at)` sorts each non-null window by `window_minutes`:
  - a window of at most 1 day goes into `five_hour_*`, and a longer one into `seven_day_*`;
  - labels come from the minutes: 300 → `5-hour`, 10080 → `weekly`, anything else `Nh`/`Nd`;
  - the reset time is `resets_at` (epoch seconds), or `as_of + resets_in_seconds` for older versions.
- `plan_type` and a `note` (`unlimited credits`, `credits <balance>`, `limit reached: <type>`) are included. When both windows are null the result is `available=True` with the note, not "unavailable".
- Before any data exists: `available=False, error="no Codex rate-limit data yet"`.

### 7. CLI, UI and web
- **`cli.py`**
  - Add `--agent {claude,codex}` and `--codex` (`store_const`, `dest="agent"`), and pass `Config(..., agent=args.agent)`.
  - `print_session_list` and `_choose` go through the provider.
  - Help and description text becomes agent-neutral.
  - `format_snapshot_human` prints `{version_tag} v…` and, for Codex, the reasoning tokens and a Codex PLAN line showing labels, the note and the as-of time.
- **`engine.py`**: `self.agg, self.jsonl = prov.open_session(self.state, self.config)`, and `self.plan = prov.make_plan_source()` under the same enable flags.
- **`ui/app.py`**: the picker uses the provider's `list_sessions` and `peek_session`, and the titles name the agent ("Select a Codex session…").
- **`ui/render.py`** and **`web.py`**: the Claude branches stay exactly as they are. When `agent == "codex"`:
  - the tokens panel shows input (non-cached), cached, cache write only if above 0, output, reasoning and cache hit;
  - the plan panel shows rows only for non-null windows, using their labels and reset times, plus the note and "as of HH:MM (age)";
  - `short_model` and `short_id` are used everywhere.
- **`web.py`** additionally:
  - `EngineManager` uses the provider's `find_session` and plan source, and reuses an existing engine before looking the session up again.
  - `fragment_list` reads "active {label} sessions".

### 8. Docs
- **README:**
  - Mention Codex in the intro.
  - Add the `--codex` / `--agent codex` usage.
  - Add a "Codex" section covering:
    - the files it reads (read-only, no network, `auth.json` never touched);
    - sub-agent threads are hidden from the list and rolled up under their parent;
    - the reported window and the estimated auto-compact point;
    - that cached tokens are counted inside input;
    - that cost is an API-price estimate;
    - that plan limits come from local `token_count` records.
  - Pricing configuration for the gpt entries, and a mention of the provider seam.
- Update the `pyproject.toml` description and the `__init__.py` / `sources/__init__.py` docstrings.

## Critical files
- **New:**
  - `src/pool_coder/providers.py`
  - `src/pool_coder/codex/{__init__,paths,parser,aggregator,discovery,overview}.py`
  - `src/pool_coder/sources/codex_limits.py`
- **Modified:** `config.py`, `models.py`, `state.py`, `aggregator.py` (state-method refactor only), `snapshot.py`, `pricing.py`, `pricing.toml`, `format.py`, `engine.py`, `cli.py`, `sources/jsonl_source.py`, `sources/base.py`, `ui/app.py`, `ui/render.py`, `web.py`, `README.md`, `pyproject.toml`
- **Reused as-is:** `tailer.Tailer` and `RESET`, `parser.parse_timestamp` and `content_preview`, `aggregator.clean_prompt`, `paths.SessionInfo` and `read_tail_text`, `overview.SessionOverview`, `discovery.DiscoveryDelta`, `TailerSpec` and `SubagentReg`, `state.ToolStatus`, `CompactionEvent` and `SubagentStatus`

## Order of work
1. Refactor the `SessionState` methods and run the full suite.
2. Update models, config, pricing, snapshot and format, with their tests.
3. Add the `JsonlSource` options.
4. Build the Codex modules in this order, each with tests: `codex/parser`, then `paths`, `aggregator`, `discovery`, `overview`, `sources/codex_limits`.
5. Add `providers.py` and switch the engine over.
6. Update the CLI, app, render and web layers.
7. Update the docs.

## Tests
- **Fixtures:**
  - `tests/conftest.py` gets a `codex_home` fixture that monkeypatches `CODEX_HOME` to a temporary directory and clears the module caches.
  - `tests/codex_records.py` holds builders for records in the current format and the legacy format.
- **Test files:**
  - `test_codex_parser.py`: usage mapping arithmetic, JS `exec` parsing, patch paths, prompt cleaning, `output_failed`, shell labels.
  - `test_codex_aggregator.py`:
    - Tokens: replaying the file changes nothing and responses are deduplicated; `token_count` duplicates and the fallback-to-primary switch; per-model breakdown when the model changes; the parent's copied meta and records are ignored.
    - Context: a 258,400 window with no bump and an auto-compact point of 244,704; one compaction gives one event with the right `before` and `after`.
    - Session details: prompts deduplicated by `client_id`; the `mode` string.
    - Tools: labels for in-flight `exec`, errors, and `turn_aborted` closing tools; legacy shell, `update_plan` and `apply_patch`.
    - Children: child tokens are kept separate and `task_complete` marks the child finished; `reset_source("main")` keeps child tokens.
  - `test_codex_paths.py`: `CODEX_HOME` is honoured; newest-first order uses activity time rather than mtime; sub-agent and guardian threads are hidden; `find_session`; a first line of 300 KB or more; `thread_names`.
  - `test_codex_discovery.py`: a child, a grandchild and a guardian are attached; an unrelated file is not; a child that appears later is picked up.
  - `test_codex_overview.py`: a tail without meta or `turn_context` still produces a preview; the thread-name fallback.
  - `test_codex_limits.py`: `resets_at` and `resets_in_seconds`; classification by `window_minutes` (a weekly primary with a null secondary); the business/unlimited note; no data yet.
  - `test_codex_cli.py`:
    - `main(["--agent","codex","--once","--json","--no-plan-limits"])` returns `agent=codex`, the reported window and a gpt-rate cost;
    - `--codex --list` hides children;
    - the human output shows `codex v`;
    - the default agent is `claude`;
    - the headless path doesn't import `textual`, checked in a **subprocess**.
- **Additions to existing tests:**
  - `test_config.py`: the gpt family and no bump.
  - `test_pricing.py`: prefix-over-family matching, the gpt fallback, and a custom file with no gpt entry.
  - `test_render.py`, `test_web.py`, `test_app.py`: Codex snapshots render with and without data, and `PoolCoderApp(None, Config(agent="codex"), …)` can be constructed.
- **Existing tests must pass unchanged.**

## Verification
1. `uv run pytest -q` passes, both old and new tests.
2. **Claude regression:** compare the output of `uv run pool-coder --once --json` before and after. The only differences should be the new keys (`agent`, `reasoning`, `as_of`, `plan_type`, `note`, `*_label`).
3. **Real `~/.codex`, read-only:**
   - `uv run pool-coder --codex --list --all` shows only top-level threads (about 28 of 159 files on the surveyed machine).
   - `uv run pool-coder --codex --once` on a live thread shows a gpt-5.6 model, a window of 258,400, subagents with token counts, and a plan line like "business · unlimited credits".
   - `uv run pool-coder --codex --serve --host 127.0.0.1` shows "active Codex sessions".
   - `uv run pool-coder --codex` runs the TUI.
4. **Cross-check the numbers** with a one-off script: the session's cumulative tokens equal the sum of the `token_usage_record.usage` values for its own `thread_id`, and each subagent's tokens equal the sum for its child file. Context % is within a few points of Codex's own indicator.
5. **Read-only check:** `grep` the new modules for `open(` with any mode other than `"rb"` or `"r"`. There should be none.

## Out of scope for v1
- Compressed `.jsonl.zst` sessions (the stdlib has no zstd before 3.14) and `archived_sessions/`.
- A picker that mixes Claude and Codex.
- Codex's SQLite state databases (undocumented).
- Fast/priority tier price multipliers.
- A Codex-style "% left" calculation with the 12K baseline.
