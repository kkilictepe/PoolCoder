"""Codex discovery: children, grandchildren and guardians attach; day window; JsonlSource."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

from codex_records import (
    CHILD,
    GRANDCHILD,
    GUARDIAN,
    ROOT,
    append_records,
    guardian_meta,
    line,
    rollout_path,
    session_meta,
    subagent_meta,
    task_complete,
    task_started,
    token_usage,
    turn_context,
    write_rollout,
)
from pool_coder.codex import paths as cpaths
from pool_coder.codex.discovery import (
    RETRY_SCANS,
    CodexDiscovery,
    subagent_description,
    subagent_type,
)
from pool_coder.codex.parser import parse_codex_line
from pool_coder.codex.paths import CodexMeta
from pool_coder.discovery import SubagentReg, TailerSpec
from pool_coder.sources.base import Discoverer
from pool_coder.sources.jsonl_source import JsonlSource

OTHER = "01a0cf02-083f-7bf0-8695-efe1f1a05889"
OTHER_CHILD = "01a0cfdb-bcfd-7e72-9c2a-bea6ed97aa9a"
SECOND = "01a0cae1-0000-7000-8000-000000000002"
LATE = "01a0cae2-0000-7000-8000-000000000003"
FAR = "01a0cae3-0000-7000-8000-000000000004"
DAY = "2026/09/22"


def _today(y: int = 2026, m: int = 9, d: int = 22):
    return lambda: date(y, m, d)


def _main(home: Path, day: str = DAY, local_ts: str = "2026-09-22T23-47-42") -> Path:
    return write_rollout(home, ROOT, [session_meta(ROOT), turn_context()], day=day,
                         local_ts=local_ts)


def _child(home: Path, tid: str = CHILD, *, day: str = DAY, local_ts: str = "2026-09-22T23-47-52",
           **kw) -> Path:
    kw.setdefault("agent_path", "/root/dependency_audit")
    kw.setdefault("nickname", "Pauli")
    return write_rollout(home, tid, [subagent_meta(tid, **kw)], day=day, local_ts=local_ts)


def _disc(main: Path, **kw) -> CodexDiscovery:
    kw.setdefault("today", _today())
    return CodexDiscovery(main, ROOT, **kw)


def _sources(delta) -> list[str]:
    return [t.source_id for t in delta.new_tailers]


def _regs(delta) -> dict[str, tuple[str, str, str]]:
    return {r.agent_id: (r.agent_type, r.description, r.parent_tool_use_id)
            for r in delta.subagents}


# -- attaching ---------------------------------------------------------------
def test_initial_attaches_child_grandchild_and_guardian(codex_home):
    main = _main(codex_home)
    child = _child(codex_home)
    grand = _child(codex_home, GRANDCHILD, parent=CHILD, agent_path="/root/dependency_audit/pool_setup",
                   nickname="Leibniz", depth=2, local_ts="2026-09-22T23-48-05")
    guard = write_rollout(codex_home, GUARDIAN, [guardian_meta(GUARDIAN, parent=CHILD)],
                          day=DAY, local_ts="2026-09-22T23-49-00")

    disc = _disc(main)
    delta = disc.initial()

    assert delta.new_tailers[0] == TailerSpec("main", main)
    assert _sources(delta) == ["main", f"agent:{CHILD}", f"agent:{GRANDCHILD}", f"agent:{GUARDIAN}"]
    assert {t.source_id: t.path for t in delta.new_tailers[1:]} == {
        f"agent:{CHILD}": child, f"agent:{GRANDCHILD}": grand, f"agent:{GUARDIAN}": guard}
    assert _regs(delta) == {
        CHILD: ("dependency_audit", "Pauli · /root/dependency_audit", ""),
        GRANDCHILD: ("pool_setup", "Leibniz · /root/dependency_audit/pool_setup", ""),
        GUARDIAN: ("guardian", "", ""),
    }
    assert disc.children == {CHILD: child, GRANDCHILD: grand, GUARDIAN: guard}
    assert not delta.workflows
    assert isinstance(disc, Discoverer)


def test_grandchild_attaches_through_parent_chain_in_one_scan(codex_home):
    # Session id says nothing here: only parent_thread_id links it, and its
    # file sorts before its parent's — the repeat pass still attaches it.
    main = _main(codex_home)
    _child(codex_home, GRANDCHILD, parent=CHILD, root="", agent_path="/root/a/b",
           local_ts="2026-09-22T23-47-43")
    _child(codex_home, CHILD, root=ROOT, agent_path="/root/a", local_ts="2026-09-22T23-47-50")
    delta = _disc(main).initial()
    assert _sources(delta) == ["main", f"agent:{CHILD}", f"agent:{GRANDCHILD}"]


def test_guardian_of_main_thread(codex_home):
    main = _main(codex_home)
    write_rollout(codex_home, GUARDIAN, [guardian_meta(GUARDIAN)], day=DAY,
                  local_ts="2026-09-22T23-50-00")
    delta = _disc(main).initial()
    assert _regs(delta) == {GUARDIAN: ("guardian", "", "")}


def test_unrelated_thread_is_not_attached(codex_home):
    main = _main(codex_home)
    write_rollout(codex_home, OTHER, [session_meta(OTHER)], day=DAY,
                  local_ts="2026-09-22T23-50-00")
    # A child of the other session (its session_id and parent are OTHER).
    _child(codex_home, OTHER_CHILD, parent=OTHER, root=OTHER, local_ts="2026-09-22T23-51-00")
    _child(codex_home)
    disc = _disc(main)
    delta = disc.initial()
    assert _sources(delta) == ["main", f"agent:{CHILD}"]
    assert not disc.scan()


def test_main_thread_files_are_never_attached(codex_home):
    main = _main(codex_home)
    # A second file of the main thread (rollout-id suffix) in a later day folder,
    # and a file whose name differs but whose meta is the main thread's.
    copy = rollout_path(codex_home, ROOT, "2026/09/23", "2026-09-23T08-00-00")
    copy = copy.with_name(copy.stem + "_r2.jsonl")
    copy.parent.mkdir(parents=True, exist_ok=True)
    copy.write_text(line(session_meta(ROOT)) + "\n", encoding="utf-8")
    write_rollout(codex_home, SECOND, [session_meta(ROOT)], day=DAY,
                  local_ts="2026-09-22T23-59-00")
    disc = _disc(main, today=_today(2026, 9, 23))
    assert _sources(disc.initial()) == ["main"]
    assert not disc.scan()
    assert disc.children == {}


def test_second_file_of_attached_child_is_ignored(codex_home):
    main = _main(codex_home)
    first = _child(codex_home)
    dup = rollout_path(codex_home, CHILD, DAY, "2026-09-22T23-58-00")
    dup = dup.with_name(dup.stem + "_r2.jsonl")
    dup.write_text(line(subagent_meta(CHILD)) + "\n", encoding="utf-8")
    disc = _disc(main)
    delta = disc.initial()
    assert _sources(delta) == ["main", f"agent:{CHILD}"]
    assert disc.children == {CHILD: first}
    assert not disc.scan()


def test_non_rollout_files_are_ignored(codex_home):
    main = _main(codex_home)
    folder = main.parent
    (folder / "notes.txt").write_text("hi", encoding="utf-8")
    (folder / f"rollout-2026-09-22T23-50-00-{CHILD}.jsonl.zst").write_bytes(b"\x28\xb5\x2f\xfd")
    (folder / "rollout-garbage.jsonl").write_text(line(subagent_meta()) + "\n", encoding="utf-8")
    disc = _disc(main)
    assert _sources(disc.initial()) == ["main"]
    assert not disc.scan()


def test_missing_root_and_folders_do_not_raise(tmp_path):
    main = tmp_path / "nowhere" / "2026" / "09" / "22" / f"rollout-2026-09-22T23-47-42-{ROOT}.jsonl"
    disc = CodexDiscovery(main, ROOT, root=tmp_path / "missing", today=_today(2026, 9, 24))
    delta = disc.initial()
    assert delta.new_tailers == [TailerSpec("main", main)] and not delta.subagents
    assert not disc.scan()


def test_empty_session_id_falls_back_to_file_name(codex_home):
    main = _main(codex_home)
    _child(codex_home)
    disc = CodexDiscovery(main, "", today=_today())
    assert disc.session_id == ROOT
    assert _sources(disc.initial()) == ["main", f"agent:{CHILD}"]


# -- incremental scans ------------------------------------------------------------
def test_scan_returns_only_new_children(codex_home):
    main = _main(codex_home)
    _child(codex_home)
    disc = _disc(main)
    assert _sources(disc.initial()) == ["main", f"agent:{CHILD}"]
    empty = disc.scan()
    assert not empty and empty.new_tailers == [] and empty.subagents == []

    late = _child(codex_home, LATE, agent_path="/root/late_review", nickname="Noether",
                  local_ts="2026-09-22T23-59-59")
    delta = disc.scan()
    assert delta.new_tailers == [TailerSpec(f"agent:{LATE}", late)]
    assert delta.subagents == [SubagentReg(LATE, "late_review", "Noether · /root/late_review", "")]
    assert not disc.scan()


def test_child_with_partial_first_line_is_retried(codex_home):
    main = _main(codex_home)
    text = line(subagent_meta()) + "\n"
    path = rollout_path(codex_home, CHILD, DAY, "2026-09-22T23-47-52")
    cut = len(text) // 2
    path.write_text(text[:cut], encoding="utf-8")  # Codex is still writing line 1
    disc = _disc(main)
    assert _sources(disc.initial()) == ["main"]
    assert not disc.scan()

    with open(path, "a", encoding="utf-8") as fh:
        fh.write(text[cut:])
    delta = disc.scan()
    assert _sources(delta) == [f"agent:{CHILD}"]
    assert _regs(delta)[CHILD][0] == "dependency_audit"


def test_unreadable_first_line_is_reread_only_when_the_file_grows(codex_home, monkeypatch):
    main = _main(codex_home)
    path = rollout_path(codex_home, CHILD, DAY, "2026-09-22T23-47-52")
    path.write_text('{"timestamp": "2026-09-22T20:47:52.000Z", "type": "sess', encoding="utf-8")
    calls: list[Path] = []
    real = cpaths.read_session_meta

    def counting(p):
        calls.append(Path(p))
        return real(p)

    monkeypatch.setattr(cpaths, "read_session_meta", counting)
    disc = _disc(main)
    disc.initial()
    disc.scan()  # the size is not known yet: read once more and remember it
    n = len(calls)
    for _ in range(RETRY_SCANS):
        disc.scan()
    assert len(calls) == n  # unchanged file: not re-read
    disc.scan()
    assert len(calls) == n + 1  # periodic retry

    path.write_text(line(subagent_meta()) + "\n", encoding="utf-8")
    assert _sources(disc.scan()) == [f"agent:{CHILD}"]


def test_attached_and_unrelated_files_are_not_reread(codex_home, monkeypatch):
    main = _main(codex_home)
    _child(codex_home)
    write_rollout(codex_home, OTHER, [session_meta(OTHER)], day=DAY, local_ts="2026-09-22T23-50-00")
    disc = _disc(main)
    disc.initial()
    calls: list[Path] = []
    monkeypatch.setattr(cpaths, "read_session_meta", lambda p: calls.append(p))
    for _ in range(5):
        assert not disc.scan()
    assert calls == []


def test_unrelated_file_attaches_once_its_parent_does(codex_home):
    # A grandchild whose meta names only its parent is seen before the parent
    # exists; it attaches in the scan that attaches the parent.
    main = _main(codex_home)
    _child(codex_home, GRANDCHILD, parent=CHILD, root="", local_ts="2026-09-22T23-47-43")
    disc = _disc(main)
    assert _sources(disc.initial()) == ["main"]
    _child(codex_home, CHILD, local_ts="2026-09-22T23-47-50")
    assert _sources(disc.scan()) == [f"agent:{CHILD}", f"agent:{GRANDCHILD}"]


# -- day folders ---------------------------------------------------------------------
def test_child_in_next_days_folder_attaches(codex_home):
    main = _main(codex_home)
    nxt = _child(codex_home, day="2026/09/23", local_ts="2026-09-23T00-10-00")
    disc = _disc(main, today=_today(2026, 9, 23))
    delta = disc.initial()
    assert {t.source_id: t.path for t in delta.new_tailers} == {"main": main, f"agent:{CHILD}": nxt}


def test_day_folder_window(codex_home):
    main = _main(codex_home, day="2026/09/10", local_ts="2026-09-10T09-00-00")
    disc = _disc(main, today=_today(2026, 9, 24))
    sessions = codex_home / "sessions"
    expected = [main.parent] + [sessions / "2026" / "09" / f"{d:02d}" for d in range(24, 17, -1)]
    assert disc.day_folders() == expected
    assert len(expected) == 8

    tids = {}
    for i, day in enumerate((10, 11, 17, 18, 24)):
        tid = f"01a0cb{i:02d}-0000-7000-8000-0000000000{day:02d}"
        tids[day] = tid
        _child(codex_home, tid, day=f"2026/09/{day:02d}", local_ts=f"2026-09-{day:02d}T10-00-00")
    delta = disc.initial()
    # main day always searched; 11 and 17 fall outside the 8-folder window
    assert _sources(delta) == ["main"] + [f"agent:{tids[d]}" for d in (10, 18, 24)]


def test_day_window_edges(codex_home):
    main = _main(codex_home, day="2026/09/10", local_ts="2026-09-10T09-00-00")
    assert _disc(main, today=_today(2026, 9, 24), max_days=1).day_folders() == [main.parent]
    # today == main day, or a clock behind it: just the main folder
    assert _disc(main, today=_today(2026, 9, 10)).day_folders() == [main.parent]
    assert _disc(main, today=_today(2026, 9, 9)).day_folders() == [main.parent]
    # a datetime from the injected clock is accepted
    folders = _disc(main, today=lambda: datetime(2026, 9, 12, 8, 30)).day_folders()
    assert [f.name for f in folders] == ["10", "12", "11"]
    # the month/year boundary is walked by calendar days
    main2 = _main(codex_home, day="2026/12/30", local_ts="2026-12-30T09-00-00")
    folders = _disc(main2, today=_today(2027, 1, 2)).day_folders()
    assert ["/".join(f.parts[-3:]) for f in folders] == [
        "2026/12/30", "2027/01/02", "2027/01/01", "2026/12/31"]


def test_main_outside_a_date_folder_searches_recent_days(codex_home, tmp_path):
    main = tmp_path / f"rollout-2026-09-22T23-47-42-{ROOT}.jsonl"
    main.write_text(line(session_meta(ROOT)) + "\n", encoding="utf-8")
    _child(codex_home)
    disc = _disc(main, today=_today(2026, 9, 23), max_days=3)
    assert [f.name for f in disc.day_folders()] == [main.parent.name, "23", "22"]
    assert _sources(disc.initial()) == ["main", f"agent:{CHILD}"]


# -- labels -----------------------------------------------------------------------------
def test_subagent_type_and_description_fallbacks():
    assert subagent_type(CodexMeta("t", agent_path="/root/a/b/")) == "b"
    assert subagent_type(CodexMeta("t", thread_source="subagent")) == "subagent"
    assert subagent_type(CodexMeta("t", thread_source="memory_consolidation")) == "memory_consolidation"
    assert subagent_type(CodexMeta("t")) == "subagent"
    assert subagent_type(CodexMeta("t", thread_source="guardian_review", agent_path="/root/x")) == "guardian"
    assert subagent_type(CodexMeta("t", source={"subagent": {"other": "guardian"}})) == "guardian"
    assert subagent_description(CodexMeta("t", agent_nickname="Pauli")) == "Pauli"
    assert subagent_description(CodexMeta("t", agent_path="/root/x")) == "/root/x"
    assert subagent_description(CodexMeta("t")) == ""


# -- JsonlSource integration -----------------------------------------------------------
class RecordingFold:
    """A tiny ``FoldTarget`` recording ``(source_id, record.type)`` and registrations."""

    def __init__(self):
        self.applied: list[tuple[str, str]] = []
        self.subagents: list[tuple[str, str, str, str]] = []
        self.resets: list[str] = []

    def apply(self, source_id, record):
        self.applied.append((source_id, record.type))

    def reset_source(self, source_id):
        self.resets.append(source_id)

    def register_subagent(self, agent_id, agent_type, description, parent_tool_use_id):
        self.subagents.append((agent_id, agent_type, description, parent_tool_use_id))

    def register_workflow(self, run_id, name, description, phases):  # pragma: no cover
        raise AssertionError("Codex has no workflows")

    def of(self, source_id):
        return [t for s, t in self.applied if s == source_id]


def test_jsonl_source_integration(codex_home):
    main = write_rollout(codex_home, ROOT, [
        session_meta(ROOT), turn_context(), task_started(),
        token_usage("resp-1", inp=100, out=10),
    ], day=DAY)
    child_path = write_rollout(codex_home, CHILD, [
        subagent_meta(CHILD), task_started(), token_usage("resp-c1", thread_id=CHILD, inp=5),
    ], day=DAY, local_ts="2026-09-22T23-47-52")
    fold = RecordingFold()
    src = JsonlSource(main, fold, 1.5, discovery=CodexDiscovery(main, ROOT, today=_today()),
                      parse=parse_codex_line)
    src.initial_catchup()

    assert fold.of("main") == ["session_meta", "turn_context", "event_msg", "token_usage_record"]
    assert fold.of(f"agent:{CHILD}") == ["session_meta", "event_msg", "token_usage_record"]
    assert fold.subagents == [(CHILD, "dependency_audit", "Pauli · /root/dependency_audit", "")]
    assert src.files_watched == 2 and fold.resets == []

    # A new child and more lines in the existing files.
    write_rollout(codex_home, LATE, [
        subagent_meta(LATE, agent_path="/root/late", nickname="Noether"),
        token_usage("resp-l1", thread_id=LATE, inp=7),
    ], day=DAY, local_ts="2026-09-22T23-59-00")
    append_records(child_path, [task_complete()])
    append_records(main, [token_usage("resp-2", inp=120, out=12)])

    t0 = src._last_discovery
    src.poll(now=t0 + 0.5)  # before the interval: tails known files only
    assert fold.of(f"agent:{LATE}") == []
    assert fold.of(f"agent:{CHILD}")[-1] == "event_msg"
    assert fold.of("main")[-1] == "token_usage_record" and len(fold.of("main")) == 5

    src.poll(now=t0 + 1.5)  # discovery runs: the late child attaches and drains
    assert fold.of(f"agent:{LATE}") == ["session_meta", "token_usage_record"]
    assert fold.subagents[-1] == (LATE, "late", "Noether · /root/late", "")
    assert src.files_watched == 3

    src.poll(now=t0 + 3.0)
    assert len(fold.subagents) == 2 and src.files_watched == 3
