"""Discover a session's growing fileset and load write-once side-data.

Each ``scan()`` diffs the four sidecar globs against what we've already seen
and returns only the *new* work: tailer specs to attach and subagent/workflow
registrations parsed from ``*.meta.json`` / ``wf_*.json``. Cheap, write-once
files are read at most once (or on mtime change).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import paths

_RE_NAME = re.compile(r"name\s*:\s*['\"]([^'\"]*)['\"]")
_RE_DESC = re.compile(r"description\s*:\s*['\"]([^'\"]*)['\"]")
_RE_PHASES = re.compile(r"phases\s*:\s*\[(.*?)\]", re.DOTALL)
_RE_OBJ = re.compile(r"\{(.*?)\}", re.DOTALL)
_RE_TITLE = re.compile(r"title\s*:\s*['\"]([^'\"]*)['\"]")
_RE_DETAIL = re.compile(r"detail\s*:\s*['\"]([^'\"]*)['\"]")


def parse_workflow_script(script: str) -> tuple[str, str, list[tuple[str, str]]]:
    """Extract ``(name, description, phases)`` from a workflow's JS ``meta``."""
    name_m = _RE_NAME.search(script)
    desc_m = _RE_DESC.search(script)
    name = name_m.group(1) if name_m else ""
    desc = desc_m.group(1) if desc_m else ""
    phases: list[tuple[str, str]] = []
    block = _RE_PHASES.search(script)
    if block:
        for obj in _RE_OBJ.findall(block.group(1)):
            t = _RE_TITLE.search(obj)
            if t:
                d = _RE_DETAIL.search(obj)
                phases.append((t.group(1), d.group(1) if d else ""))
    return name, desc, phases


@dataclass
class TailerSpec:
    source_id: str
    path: Path


@dataclass
class SubagentReg:
    agent_id: str
    agent_type: str
    description: str
    parent_tool_use_id: str


@dataclass
class WorkflowReg:
    run_id: str
    name: str
    description: str
    phases: list[tuple[str, str]]


@dataclass
class DiscoveryDelta:
    new_tailers: list[TailerSpec] = field(default_factory=list)
    subagents: list[SubagentReg] = field(default_factory=list)
    workflows: list[WorkflowReg] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.new_tailers or self.subagents or self.workflows)


class SessionDiscovery:
    def __init__(self, main_path: str | Path):
        self.main_path = Path(main_path)
        self.known_files: set[Path] = set()
        self.loaded_wf_json: dict[Path, float] = {}
        self.pending_meta: dict[str, Path] = {}  # agent_id -> jsonl path awaiting meta

    def initial(self) -> DiscoveryDelta:
        delta = DiscoveryDelta()
        delta.new_tailers.append(TailerSpec("main", self.main_path))
        self.known_files.add(self.main_path)
        self._scan_into(delta)
        return delta

    def scan(self) -> DiscoveryDelta:
        delta = DiscoveryDelta()
        self._scan_into(delta)
        return delta

    # -- internals -------------------------------------------------------
    def _scan_into(self, delta: DiscoveryDelta) -> None:
        # top-level subagents
        for f in paths.subagent_files(self.main_path):
            if f in self.known_files:
                continue
            self.known_files.add(f)
            agent_id = f.stem[len("agent-"):] if f.stem.startswith("agent-") else f.stem
            delta.new_tailers.append(TailerSpec(f"agent:{agent_id}", f))
            reg = self._load_subagent_meta(f, agent_id)
            delta.subagents.append(reg)
            if not (reg.agent_type or reg.parent_tool_use_id):
                self.pending_meta[agent_id] = f

        # retry subagent meta that wasn't written yet on first sight
        for agent_id, f in list(self.pending_meta.items()):
            reg = self._load_subagent_meta(f, agent_id)
            if reg.agent_type or reg.parent_tool_use_id:
                delta.subagents.append(reg)
                self.pending_meta.pop(agent_id, None)

        # workflow definition side-data (wf_*.json) — re-read on mtime change
        for wf in paths.workflow_meta_files(self.main_path):
            try:
                mtime = wf.stat().st_mtime
            except OSError:
                continue
            if self.loaded_wf_json.get(wf) == mtime:
                continue
            self.loaded_wf_json[wf] = mtime
            reg = self._load_workflow_json(wf)
            if reg:
                delta.workflows.append(reg)

        # per-workflow journal + agent transcripts
        for wd in paths.workflow_dirs(self.main_path):
            run_id = wd.name
            journal = paths.workflow_journal(wd)
            if journal.exists() and journal not in self.known_files:
                self.known_files.add(journal)
                delta.new_tailers.append(TailerSpec(f"wfjournal:{run_id}", journal))
                delta.workflows.append(WorkflowReg(run_id, "", "", []))
            for af in paths.workflow_agent_files(wd):
                if af not in self.known_files:
                    self.known_files.add(af)
                    delta.new_tailers.append(TailerSpec(f"wfagent:{run_id}", af))

    def _load_subagent_meta(self, jsonl_path: Path, agent_id: str) -> SubagentReg:
        meta_path = jsonl_path.parent / (jsonl_path.stem + ".meta.json")
        agent_type = description = parent = ""
        try:
            data = json.loads(meta_path.read_text(encoding="utf-8"))
            agent_type = data.get("agentType") or ""
            description = data.get("description") or ""
            parent = data.get("toolUseId") or ""
        except (OSError, json.JSONDecodeError, ValueError):
            pass
        return SubagentReg(agent_id, agent_type, description, parent)

    def _load_workflow_json(self, wf_path: Path) -> WorkflowReg | None:
        try:
            data = json.loads(wf_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError):
            return None
        run_id = data.get("runId") or wf_path.stem
        name, desc, phases = parse_workflow_script(data.get("script") or "")
        return WorkflowReg(run_id, name, desc, phases)
