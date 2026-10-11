"""Builders for Codex rollout records, current and legacy formats.

Each builder returns one decoded rollout line (a dict). ``line()`` turns it
into JSON text and ``write_rollout()`` lays a file out the way Codex does:
``<home>/sessions/YYYY/MM/DD/rollout-<local-ts>-<thread-id>.jsonl``.

Shapes follow a survey of real Codex 0.155 rollouts. Two formats coexist:
the "item" format (``ordinal`` on every line, ``event_msg.item_completed``)
and the "event" format (no ``ordinal``; ``event_msg.user_message``,
``sub_agent_activity``, ``patch_apply_end``, ``context_compacted``). The
legacy tool calls (``function_call`` ``shell``/``apply_patch``/
``update_plan``, ``local_shell_call``) predate code mode.
"""

from __future__ import annotations

import json
from pathlib import Path

TS = "2026-09-22T20:47:52.000Z"
WINDOW = 258_400
ROOT = "01a0cadf-d726-7cf2-ab93-18ccccdcbbfc"
CHILD = "01a0cadf-fe06-71b3-ab69-1a4802df0b58"
GRANDCHILD = "01a0cae0-2fd3-7c22-9200-95d52b7b6c42"
GUARDIAN = "01a0cdc9-9413-7440-b8a2-d6d481b7d2fe"


def ts(minute: int = 0, second: int = 0) -> str:
    """A UTC timestamp on the fixture day, ``minute`` minutes after 20:47."""
    total = 47 * 60 + 52 + minute * 60 + second
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"2026-09-22T{20 + h:02d}:{m:02d}:{s:02d}.000Z"


def rec(type_: str, payload: dict, at: str = TS, ordinal: int | None = None) -> dict:
    out = {"timestamp": at, "type": type_, "payload": payload}
    if ordinal is not None:
        out["ordinal"] = ordinal
    return out


def usage(inp: int = 0, cached: int = 0, cw: int = 0, out: int = 0, rs: int = 0) -> dict:
    return {
        "input_tokens": inp,
        "cached_input_tokens": cached,
        "cache_write_input_tokens": cw,
        "output_tokens": out,
        "reasoning_output_tokens": rs,
        "total_tokens": inp + out,
    }


# -- thread identity ---------------------------------------------------------
def session_meta(tid: str = ROOT, *, root: str | None = None, parent: str | None = None,
                 thread_source: str = "user", source: object = "vscode",
                 cwd: str = "C:\\Git\\proj", branch: str | None = "main",
                 cli_version: str = "0.155.0", agent_path: str | None = None,
                 nickname: str | None = None, forked_from: str | None = None,
                 model: str = "gpt-5.6-terra", pad: int = 0, at: str = TS) -> dict:
    """``session_meta``. ``pad`` adds that many bytes of instructions (real
    first lines are ~19 KB and can be much larger)."""
    payload = {
        "session_id": root or tid,
        "id": tid,
        "timestamp": at,
        "cwd": cwd,
        "originator": "codex_vscode",
        "cli_version": cli_version,
        "source": source,
        "thread_source": thread_source,
        "model_provider": "openai",
        "base_instructions": {"text": "You are Codex." + ("x" * pad),
                              "provenance": {"type": "model", "model": model}},
    }
    if parent:
        payload["parent_thread_id"] = parent
    if forked_from:
        payload["forked_from_id"] = forked_from
    if agent_path:
        payload["agent_path"] = agent_path
    if nickname:
        payload["agent_nickname"] = nickname
    if branch is not None:
        payload["git"] = {"commit_hash": "0" * 40, "branch": branch,
                          "repository_url": "https://example.invalid/r.git"}
    return rec("session_meta", payload, at)


def subagent_meta(tid: str = CHILD, parent: str = ROOT, root: str = ROOT,
                  agent_path: str = "/root/dependency_audit", nickname: str = "Pauli",
                  forked: bool = True, depth: int = 1, **kw) -> dict:
    source = {"subagent": {"thread_spawn": {"parent_thread_id": parent, "depth": depth,
                                            "agent_path": agent_path,
                                            "agent_nickname": nickname, "agent_role": None}}}
    return session_meta(tid, root=root, parent=parent, thread_source="subagent",
                        source=source, agent_path=agent_path, nickname=nickname,
                        forked_from=parent if forked else None, **kw)


def guardian_meta(tid: str = GUARDIAN, parent: str = ROOT, root: str = ROOT, **kw) -> dict:
    return session_meta(tid, root=root, parent=parent, thread_source="guardian_review",
                        source={"subagent": {"other": "guardian"}}, **kw)


# -- turn settings -----------------------------------------------------------
def turn_context(model: str = "gpt-5.6-terra", *, cwd: str = "C:\\Git\\proj",
                 effort: str = "ultra", mode: str = "default",
                 sandbox: str = "workspace-write", at: str = TS) -> dict:
    return rec("turn_context", {
        "turn_id": "turn-1", "cwd": cwd, "model": model, "effort": effort,
        "approval_policy": "on-request",
        "sandbox_policy": {"type": sandbox, "network_access": False},
        "collaboration_mode": {"mode": mode, "settings": {"model": model,
                                                          "reasoning_effort": effort}},
        "summary": "none",
    }, at)


def thread_settings(model: str = "gpt-5.6-terra", *, tid: str = ROOT, effort: str = "ultra",
                    mode: str = "default", profile: str = ":danger-full-access",
                    cwd: str = "C:\\Git\\proj", at: str = TS) -> dict:
    return rec("event_msg", {"type": "thread_settings_applied", "thread_id": tid,
                             "thread_settings": {
                                 "model": model, "cwd": cwd, "reasoning_effort": effort,
                                 "active_permission_profile": {"id": profile},
                                 "collaboration_mode": {"mode": mode, "settings": {
                                     "model": model, "reasoning_effort": effort}}}}, at)


def task_started(window: int = WINDOW, at: str = TS, turn_id: str = "turn-1") -> dict:
    return rec("event_msg", {"type": "task_started", "turn_id": turn_id,
                             "model_context_window": window,
                             "collaboration_mode_kind": "default"}, at)


def task_complete(at: str = TS, turn_id: str = "turn-1", message: str = "done") -> dict:
    return rec("event_msg", {"type": "task_complete", "turn_id": turn_id,
                             "last_agent_message": message}, at)


def turn_aborted(reason: str = "interrupted", at: str = TS, turn_id: str = "turn-1") -> dict:
    return rec("event_msg", {"type": "turn_aborted", "turn_id": turn_id, "reason": reason}, at)


# -- tokens --------------------------------------------------------------------
def token_usage(response_id: str, *, thread_id: str = ROOT, inp: int = 0, cached: int = 0,
                cw: int = 0, out: int = 0, rs: int = 0, at: str = TS) -> dict:
    """``token_usage_record`` — the primary per-response usage (current format)."""
    u = usage(inp, cached, cw, out, rs)
    return rec("token_usage_record", {
        "thread_id": thread_id, "turn_id": "turn-1", "session_id": ROOT,
        "response_id": response_id, "usage": u, "turn_token_usage": u,
        "thread_token_usage": u,  # cumulative in real files; never summed
    }, at)


def token_count(total: dict | None = None, last: dict | None = None, *,
                window: int | None = WINDOW, rate_limits: dict | None = None,
                at: str = TS) -> dict:
    """``event_msg.token_count`` — cumulative totals (the legacy fallback) and
    the account's rate limits. ``info`` is null when no usage is known yet."""
    info = None
    if total is not None:
        info = {"total_token_usage": total, "last_token_usage": last or total}
        if window is not None:
            info["model_context_window"] = window
    return rec("event_msg", {"type": "token_count", "info": info,
                             "rate_limits": rate_limits}, at)


def rate_limits(primary: dict | None = None, secondary: dict | None = None, *,
                plan_type: str | None = "plus", credits: dict | None = None,
                reached: str | None = None) -> dict:
    return {"limit_id": "codex", "limit_name": None, "primary": primary,
            "secondary": secondary, "credits": credits, "plan_type": plan_type,
            "rate_limit_reached_type": reached}


def window(used_percent: float, window_minutes: int, *, resets_at: int | None = None,
           resets_in_seconds: int | None = None) -> dict:
    out: dict = {"used_percent": used_percent, "window_minutes": window_minutes}
    if resets_at is not None:
        out["resets_at"] = resets_at
    if resets_in_seconds is not None:
        out["resets_in_seconds"] = resets_in_seconds
    return out


# -- conversation --------------------------------------------------------------
def user_message_item(text: str, client_id: str | None = "c1", *, tid: str = ROOT,
                      at: str = TS, item_id: str = "item-1") -> dict:
    """Current format prompt: ``item_completed{UserMessage}``. ``client_id=None``
    leaves the key out, as guardian review threads do."""
    item = {"type": "UserMessage", "id": item_id, "client_id": client_id,
            "content": [{"type": "text", "text": text, "text_elements": []}]}
    if client_id is None:
        del item["client_id"]
    return rec("event_msg", {"type": "item_completed", "thread_id": tid, "turn_id": "turn-1",
                             "item": item}, at)


def user_message_event(text: str, client_id: str = "c1", at: str = TS) -> dict:
    """Event format prompt: ``event_msg.user_message``."""
    return rec("event_msg", {"type": "user_message", "client_id": client_id,
                             "message": text, "images": [], "text_elements": []}, at)


def assistant_message(text: str, at: str = TS, phase: str = "commentary") -> dict:
    return rec("response_item", {"type": "message", "id": "msg_1", "role": "assistant",
                                 "content": [{"type": "output_text", "text": text}],
                                 "phase": phase}, at)


def agent_message_item(text: str, at: str = TS) -> dict:
    """Duplicate of an assistant message (item format) — must be ignored."""
    return rec("event_msg", {"type": "item_completed", "thread_id": ROOT, "turn_id": "turn-1",
                             "item": {"type": "AgentMessage", "id": "item-2",
                                      "content": [{"type": "Text", "text": text}]}}, at)


def agent_message_event(text: str, at: str = TS) -> dict:
    """Duplicate of an assistant message (event format) — must be ignored."""
    return rec("event_msg", {"type": "agent_message", "message": text,
                             "phase": "commentary"}, at)


def reasoning(summary: str | None = None, at: str = TS) -> dict:
    return rec("response_item", {
        "type": "reasoning", "id": "rs_1",
        "summary": [{"type": "summary_text", "text": summary}] if summary else [],
        "content": None, "encrypted_content": "gAAAA"}, at)


def compacted(at: str = TS) -> dict:
    return rec("compacted", {"message": "", "replacement_history": []}, at)


# -- tools -----------------------------------------------------------------------
def exec_js(cmd: str, workdir: str = "C:\\Git\\proj") -> str:
    """Code-mode JavaScript that runs one shell command (as gpt-5.6 writes it)."""
    args = json.dumps({"cmd": cmd, "workdir": workdir, "yield_time_ms": 10000})
    return f"const r = await tools.exec_command({args});\ntext(r.output);\n"


def patch_js(path: str, verb: str = "Update") -> str:
    """Code-mode JavaScript that applies a patch held in a string variable."""
    patch = f"*** Begin Patch\n*** {verb} File: {path}\n@@\n-old\n+new\n*** End Patch\n"
    return f"const patch = {json.dumps(patch)};\nconst r = await tools.apply_patch(patch);\ntext(r);\n"


def exec_call(call_id: str, js: str, at: str = TS) -> dict:
    return rec("response_item", {"type": "custom_tool_call", "id": "ctc_1",
                                 "status": "completed", "call_id": call_id,
                                 "name": "exec", "input": js}, at)


def exec_output(call_id: str, header: str = "Script completed", body: str = "ok",
                at: str = TS) -> dict:
    return rec("response_item", {"type": "custom_tool_call_output", "id": "ctco_1",
                                 "call_id": call_id,
                                 "output": [{"type": "input_text",
                                             "text": f"{header}\nWall time 0.4 seconds\nOutput:\n"},
                                            {"type": "input_text", "text": body}]}, at)


def function_call(name: str, arguments: dict | str, call_id: str, *,
                  namespace: str | None = None, at: str = TS) -> dict:
    payload = {"type": "function_call", "id": "fc_1", "name": name,
               "arguments": arguments if isinstance(arguments, str) else json.dumps(arguments),
               "call_id": call_id}
    if namespace:
        payload["namespace"] = namespace
    return rec("response_item", payload, at)


def function_output(call_id: str, output: object, at: str = TS) -> dict:
    return rec("response_item", {"type": "function_call_output", "id": "fco_1",
                                 "call_id": call_id, "output": output}, at)


def local_shell_call(call_id: str, command: list[str], at: str = TS) -> dict:
    return rec("response_item", {"type": "local_shell_call", "id": "lsc_1", "call_id": call_id,
                                 "status": "completed",
                                 "action": {"type": "exec", "command": command}}, at)


def command_execution(item_id: str, command: list[str], *, exit_code: int = 0,
                      status: str | None = None, parsed_cmd: list | None = None,
                      at: str = TS) -> dict:
    return rec("event_msg", {"type": "item_completed", "thread_id": ROOT, "turn_id": "turn-1",
                             "item": {"type": "CommandExecution", "id": item_id,
                                      "command": command, "cwd": "file:///C:/Git/proj",
                                      "parsed_cmd": parsed_cmd or [],
                                      "status": status or ("completed" if exit_code == 0 else "failed"),
                                      "exit_code": exit_code, "stdout": "", "stderr": ""}}, at)


def file_change(item_id: str, paths: list[str], at: str = TS) -> dict:
    return rec("event_msg", {"type": "item_completed", "thread_id": ROOT, "turn_id": "turn-1",
                             "item": {"type": "FileChange", "id": item_id,
                                      "changes": {p: {"type": "update", "unified_diff": "@@"}
                                                  for p in paths},
                                      "status": "completed"}}, at)


def patch_apply_end(call_id: str, paths: list[str], success: bool = True, at: str = TS) -> dict:
    return rec("event_msg", {"type": "patch_apply_end", "call_id": call_id, "turn_id": "turn-1",
                             "success": success, "status": "completed",
                             "changes": {p: {"type": "add", "content": "x"} for p in paths}}, at)


# -- sub-agents ------------------------------------------------------------------
def sub_agent_item(call_id: str, kind: str, agent_tid: str = CHILD,
                   agent_path: str = "/root/dependency_audit", at: str = TS) -> dict:
    """Current format: ``item_completed{SubAgentActivity}`` (kind started/completed/…)."""
    return rec("event_msg", {"type": "item_completed", "thread_id": ROOT, "turn_id": "turn-1",
                             "item": {"type": "SubAgentActivity", "id": call_id, "kind": kind,
                                      "agent_thread_id": agent_tid,
                                      "agent_path": agent_path}}, at)


def sub_agent_event(call_id: str, kind: str, agent_tid: str = CHILD,
                    agent_path: str = "/root/dependency_audit", at: str = TS) -> dict:
    """Event format: ``event_msg.sub_agent_activity``."""
    return rec("event_msg", {"type": "sub_agent_activity", "event_id": call_id,
                             "agent_thread_id": agent_tid, "agent_path": agent_path,
                             "kind": kind}, at)


# -- files -------------------------------------------------------------------------
def line(record: dict) -> str:
    return json.dumps(record, ensure_ascii=False)


def rollout_path(home: Path, tid: str, day: str = "2026/09/22",
                 local_ts: str = "2026-09-22T23-47-42") -> Path:
    return Path(home) / "sessions" / day / f"rollout-{local_ts}-{tid}.jsonl"


def write_rollout(home: Path, tid: str, records: list[dict], day: str = "2026/09/22",
                  local_ts: str = "2026-09-22T23-47-42") -> Path:
    """Write ``records`` as a rollout file under ``home`` and return its path."""
    path = rollout_path(home, tid, day, local_ts)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(line(r) + "\n" for r in records), encoding="utf-8")
    return path


def append_records(path: Path, records: list[dict]) -> None:
    with open(path, "a", encoding="utf-8") as fh:
        for r in records:
            fh.write(line(r) + "\n")
