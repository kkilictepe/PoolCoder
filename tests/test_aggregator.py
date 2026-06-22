"""Aggregator folding: tokens, tools, subagents, workflows, compaction, reset."""

from __future__ import annotations

from pool_coder.aggregator import Aggregator, clean_prompt
from pool_coder.parser import Record
from pool_coder.state import SessionState

TS = "2026-06-20T10:00:00.000Z"


def asst(usage=None, tools=None, model="claude-opus-4-8", request_id="r1", ts=TS, **extra):
    content = []
    for tid, name, *rest in (tools or []):
        content.append({"type": "tool_use", "id": tid, "name": name, "input": rest[0] if rest else {}})
    msg = {"model": model, "content": content}
    if usage:
        msg["usage"] = usage
    return Record({"type": "assistant", "requestId": request_id, "timestamp": ts, "message": msg, **extra})


def usage(inp=0, cc=0, cr=0, out=0):
    return {
        "input_tokens": inp,
        "cache_creation_input_tokens": cc,
        "cache_read_input_tokens": cr,
        "output_tokens": out,
    }


def user_result(tid, is_error=False, ts=TS):
    return Record({"type": "user", "timestamp": ts, "message": {
        "content": [{"type": "tool_result", "tool_use_id": tid, "is_error": is_error, "content": "ok"}]}})


def user_prompt(text, ts=TS):
    return Record({"type": "user", "timestamp": ts, "message": {"content": text}})


def make() -> Aggregator:
    return Aggregator(SessionState())


def test_token_fold_cumulative_and_replay_safe():
    agg = make()
    agg.apply("main", asst(usage=usage(inp=10, cc=5, cr=100, out=20), request_id="r1"))
    agg.apply("main", asst(usage=usage(inp=2, cr=200, out=30), request_id="r2", ts="2026-06-20T10:01:00.000Z"))
    cum = agg.state.cumulative_tokens()
    assert (cum.input, cum.cache_creation, cum.cache_read, cum.output) == (12, 5, 300, 50)
    # latest turn drives current context (input+cc+cr of r2 = 202)
    assert agg.state.current_context_tokens == 202
    # replaying r2 must not double-count
    agg.apply("main", asst(usage=usage(inp=2, cr=200, out=30), request_id="r2"))
    cum2 = agg.state.cumulative_tokens()
    assert cum2.output == 50


def test_in_flight_tool_then_resolved():
    agg = make()
    agg.apply("main", asst(tools=[("t1", "Read", {"file_path": "a.py"})]))
    assert [t.tool.name for t in agg.state.in_flight_tools()] == ["Read"]
    assert agg.state.files_touched.get("a.py") is not None
    agg.apply("main", user_result("t1"))
    assert agg.state.in_flight_tools() == []
    assert agg.state.tool_counts["Read"] == 1
    assert agg.state.tools["t1"].done is True


def test_subagent_registered_and_finished_via_parent_tool():
    agg = make()
    agg.apply("main", asst(tools=[("t9", "Task", {"description": "explore"})]))
    agg.register_subagent("a1", "Explore", "explore the repo", parent_tool_use_id="t9")
    # subagent does some work in its own transcript
    agg.apply("agent:a1", asst(tools=[("x1", "Grep")], request_id="ar1"))
    assert agg.state.subagents["a1"].in_flight_tools == 1
    assert agg.state.subagents["a1"].finished is False
    # parent Task tool completes in the main transcript
    agg.apply("main", user_result("t9"))
    assert agg.state.subagents["a1"].finished is True


def test_workflow_journal_counts():
    agg = make()
    agg.register_workflow("wf1", "review", "adversarial review", [("Review", ""), ("Verify", "")])
    agg.apply("wfjournal:wf1", Record({"type": "started", "key": "k1", "agentId": "a1"}))
    agg.apply("wfjournal:wf1", Record({"type": "started", "key": "k2", "agentId": "a2"}))
    agg.apply("wfjournal:wf1", Record({"type": "result", "key": "k1", "agentId": "a1"}))
    wf = agg.state.workflows["wf1"]
    assert (wf.running_agents, wf.completed_agents, wf.total_agents) == (1, 1, 2)
    assert [p.title for p in wf.phases] == ["Review", "Verify"]


def test_compaction_detected_on_sharp_drop():
    agg = make()
    agg.apply("main", asst(usage=usage(inp=800_000), request_id="r1"))
    agg.apply("main", asst(usage=usage(inp=120_000), request_id="r2", ts="2026-06-20T10:05:00.000Z"))
    assert len(agg.state.compactions) == 1
    ev = agg.state.compactions[0]
    assert ev.before == 800_000 and ev.after == 120_000


def test_reset_main_clears_main_but_keeps_subagent_tokens():
    agg = make()
    agg.apply("main", asst(usage=usage(inp=50), tools=[("t1", "Read")], request_id="r1"))
    agg.apply("agent:a1", asst(usage=usage(inp=7), request_id="ar1"))
    agg.reset_source("main")
    assert agg.state.turns == 0
    assert agg.state.tools == {}
    assert agg.state.current_context_tokens == 0
    # subagent tokens survive a main reset
    assert agg.state.source_tokens("agent:a1").input == 7


def test_assistant_text_thinking_and_todo_events():
    agg = make()
    rec = Record({"type": "assistant", "requestId": "r1", "timestamp": TS, "message": {
        "model": "claude-opus-4-8", "content": [
            {"type": "thinking", "thinking": "let me consider this carefully"},
            {"type": "text", "text": "I'll start by reading the config."},
            {"type": "tool_use", "id": "t1", "name": "TodoWrite", "input": {"todos": [
                {"content": "Build parser", "status": "completed", "activeForm": "Building parser"},
                {"content": "Build tailer", "status": "in_progress", "activeForm": "Building tailer"}]}},
        ]}})
    agg.apply("main", rec)
    events = [(e.kind, e.text) for e in agg.state.events]
    assert any(k == "thinking" for k, _ in events)
    assert any(k == "text" and "reading the config" in t for k, t in events)
    assert any(k == "tool" and "Building tailer" in t for k, t in events)  # TodoWrite shows active item


def test_clean_prompt_strips_wrappers():
    raw = "<ide_selection>noise</ide_selection> real question here"
    assert clean_prompt(raw) == "real question here"
    assert clean_prompt("") is None
