"""Discovery: glob-diff attaches only new files; meta/workflow side-data."""

from __future__ import annotations

import json

from pool_coder.discovery import SessionDiscovery, parse_workflow_script

SCRIPT = """
export const meta = {
  name: 'slice-review',
  description: 'Adversarial review of the slice',
  phases: [
    { title: 'Review', detail: 'find issues' },
    { title: 'Verify' },
  ],
}
const FILES = ['a.ts']
"""


def test_parse_workflow_script():
    name, desc, phases = parse_workflow_script(SCRIPT)
    assert name == "slice-review"
    assert desc == "Adversarial review of the slice"
    assert phases == [("Review", "find issues"), ("Verify", "")]


def _build_session(tmp_path):
    main = tmp_path / "sid.jsonl"
    main.write_text("{}\n", encoding="utf-8")
    sd = tmp_path / "sid"
    (sd / "subagents").mkdir(parents=True)
    (sd / "workflows").mkdir(parents=True)
    (sd / "tool-results").mkdir(parents=True)  # must be ignored
    return main, sd


def test_initial_then_incremental(tmp_path):
    main, sd = _build_session(tmp_path)
    # one subagent with meta
    (sd / "subagents" / "agent-aaa.jsonl").write_text("{}\n", encoding="utf-8")
    (sd / "subagents" / "agent-aaa.meta.json").write_text(
        json.dumps({"agentType": "Explore", "description": "look around", "toolUseId": "t1"}),
        encoding="utf-8",
    )
    # noise that must be excluded
    (sd / "tool-results" / "blob.txt").write_text("x" * 100, encoding="utf-8")

    disc = SessionDiscovery(main)
    delta = disc.initial()
    sources = {t.source_id for t in delta.new_tailers}
    assert "main" in sources
    assert "agent:aaa" in sources
    assert all("tool-results" not in str(t.path) for t in delta.new_tailers)
    assert delta.subagents[0].agent_type == "Explore"
    assert delta.subagents[0].parent_tool_use_id == "t1"

    # second scan: nothing new yet
    assert not disc.scan()

    # now a workflow appears
    (sd / "workflows" / "wf_x.json").write_text(
        json.dumps({"runId": "wf_x", "script": SCRIPT}), encoding="utf-8"
    )
    wfdir = sd / "subagents" / "workflows" / "wf_x"
    wfdir.mkdir(parents=True)
    (wfdir / "journal.jsonl").write_text("{}\n", encoding="utf-8")
    (wfdir / "agent-bbb.jsonl").write_text("{}\n", encoding="utf-8")

    delta2 = disc.scan()
    sources2 = {t.source_id for t in delta2.new_tailers}
    assert sources2 == {"wfjournal:wf_x", "wfagent:wf_x"}
    names = {w.name for w in delta2.workflows if w.name}
    assert "slice-review" in names


def test_late_meta_is_retried(tmp_path):
    main, sd = _build_session(tmp_path)
    agent = sd / "subagents" / "agent-ccc.jsonl"
    agent.write_text("{}\n", encoding="utf-8")  # meta not written yet

    disc = SessionDiscovery(main)
    delta = disc.initial()
    assert delta.subagents[0].agent_type == ""  # unknown on first sight
    assert "ccc" in disc.pending_meta

    # meta lands later
    (sd / "subagents" / "agent-ccc.meta.json").write_text(
        json.dumps({"agentType": "Plan", "description": "design", "toolUseId": "t9"}),
        encoding="utf-8",
    )
    delta2 = disc.scan()
    assert delta2.subagents[0].agent_type == "Plan"
    assert "ccc" not in disc.pending_meta
