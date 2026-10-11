"""JsonlSource: pluggable discovery/parse keywords around the shared drain loop."""

from __future__ import annotations

import json

from pool_coder.aggregator import Aggregator
from pool_coder.discovery import DiscoveryDelta, SessionDiscovery, SubagentReg, TailerSpec, WorkflowReg
from pool_coder.parser import parse_line
from pool_coder.sources.base import Discoverer, FoldTarget
from pool_coder.sources.jsonl_source import JsonlSource
from pool_coder.state import SessionState


class RecordingFold:
    """A minimal ``FoldTarget`` that logs every call in order."""

    def __init__(self):
        self.calls: list[tuple] = []

    def apply(self, source_id, record):
        self.calls.append(("apply", source_id, record))

    def reset_source(self, source_id):
        self.calls.append(("reset", source_id))

    def register_subagent(self, agent_id, agent_type, description, parent_tool_use_id):
        self.calls.append(("subagent", agent_id, agent_type, description, parent_tool_use_id))

    def register_workflow(self, run_id, name, description, phases):
        self.calls.append(("workflow", run_id, name))

    def applied(self, source_id=None):
        return [c[2] for c in self.calls if c[0] == "apply" and (source_id is None or c[1] == source_id)]


class FakeDiscovery:
    """Yields a fixed initial delta, then whatever deltas are queued for scan()."""

    def __init__(self, initial: DiscoveryDelta):
        self._initial = initial
        self.queued: list[DiscoveryDelta] = []
        self.scans = 0

    def initial(self) -> DiscoveryDelta:
        return self._initial

    def scan(self) -> DiscoveryDelta:
        self.scans += 1
        return self.queued.pop(0) if self.queued else DiscoveryDelta()


def parse_upper(line: str):
    """A custom parser: JSON objects only, value upper-cased; anything else skipped."""
    try:
        data = json.loads(line)
    except ValueError:
        return None
    return data["v"].upper() if isinstance(data, dict) and "v" in data else None


def _lines(*values) -> bytes:
    return b"".join(json.dumps({"v": v}).encode() + b"\n" for v in values)


def test_protocols_are_structural():
    assert isinstance(Aggregator(SessionState()), FoldTarget)
    assert isinstance(RecordingFold(), FoldTarget)
    assert not isinstance(object(), FoldTarget)
    assert isinstance(FakeDiscovery(DiscoveryDelta()), Discoverer)
    assert isinstance(SessionDiscovery("x.jsonl"), Discoverer)


def test_custom_discovery_and_parse(tmp_path):
    main = tmp_path / "main.jsonl"
    child = tmp_path / "child.jsonl"
    main.write_bytes(_lines("a", "b") + b"not json\n")
    child.write_bytes(_lines("c"))
    disc = FakeDiscovery(DiscoveryDelta(
        new_tailers=[TailerSpec("main", main), TailerSpec("agent:t1", child)],
        subagents=[SubagentReg("t1", "worker", "nick · /root/worker", "")],
        workflows=[WorkflowReg("wf", "name", "", [])],
    ))
    fold = RecordingFold()
    src = JsonlSource(main, fold, discovery=disc, parse=parse_upper)
    assert src.discovery is disc and src.parse is parse_upper

    src.initial_catchup()
    assert src.files_watched == 2
    # side-data is registered before any record is folded
    assert fold.calls[0] == ("subagent", "t1", "worker", "nick · /root/worker", "")
    assert fold.calls[1] == ("workflow", "wf", "name")
    assert fold.applied("main") == ["A", "B"]  # the bad line was skipped by parse
    assert fold.applied("agent:t1") == ["C"]
    assert disc.scans == 1  # initial_catchup's second pass


def test_scan_attaches_new_files_on_poll(tmp_path):
    main = tmp_path / "main.jsonl"
    main.write_bytes(_lines("a"))
    disc = FakeDiscovery(DiscoveryDelta(new_tailers=[TailerSpec("main", main)]))
    fold = RecordingFold()
    src = JsonlSource(main, fold, discovery_interval=1.0, discovery=disc, parse=parse_upper)
    src.initial_catchup()

    late = tmp_path / "late.jsonl"
    late.write_bytes(_lines("z"))
    disc.queued.append(DiscoveryDelta(new_tailers=[TailerSpec("agent:late", late)]))
    base = src._last_discovery
    src.poll(now=base + 0.5)  # before the interval: no scan
    assert src.files_watched == 1
    src.poll(now=base + 1.5)
    assert src.files_watched == 2
    assert fold.applied("agent:late") == ["Z"]


def test_reset_still_triggers_reset_source(tmp_path):
    main = tmp_path / "main.jsonl"
    main.write_bytes(_lines("old1", "old2", "old3"))
    fold = RecordingFold()
    src = JsonlSource(main, fold, discovery=FakeDiscovery(
        DiscoveryDelta(new_tailers=[TailerSpec("main", main)])), parse=parse_upper)
    src.initial_catchup()
    assert fold.applied() == ["OLD1", "OLD2", "OLD3"]

    main.write_bytes(_lines("new"))  # rewritten smaller -> rotation
    fold.calls.clear()
    src.poll(now=src._last_discovery)
    assert fold.calls == [("reset", "main"), ("apply", "main", "NEW")]


def test_default_construction_is_claude(tmp_path):
    main = tmp_path / "abc.jsonl"
    rec = {"type": "assistant", "requestId": "r1", "timestamp": "2026-06-20T10:00:00.000Z",
           "message": {"model": "claude-opus-4-8", "content": [],
                       "usage": {"input_tokens": 5, "output_tokens": 7}}}
    main.write_bytes(json.dumps(rec).encode() + b"\n")
    agg = Aggregator(SessionState())
    src = JsonlSource(main, agg)
    assert isinstance(src.discovery, SessionDiscovery)
    assert src.parse is parse_line
    src.initial_catchup()
    assert agg.state.cumulative_tokens().output == 7
    assert agg.state.model == "claude-opus-4-8"
    assert src.files_watched == 1
