"""Codex rollout discovery: CODEX_HOME, activity time, sub-agent hiding, meta."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from codex_records import (
    CHILD,
    GRANDCHILD,
    GUARDIAN,
    ROOT,
    TS,
    append_records,
    guardian_meta,
    line,
    rec,
    rollout_path,
    session_meta,
    subagent_meta,
    token_usage,
    ts,
    turn_context,
    write_rollout,
)
from pool_coder.codex import paths
from pool_coder.codex.paths import (
    ROLLOUT_RE,
    CodexMeta,
    clear_caches,
    find_session,
    last_activity,
    list_sessions,
    read_session_meta,
    sessions_root,
    thread_names,
)

OTHER = "01a0cf02-083f-7bf0-8695-efe1f1a05889"
THIRD = "01a0d05e-92eb-7d00-a821-e1a558969e34"
MEMORY = "01a0d24a-40b6-7051-9c54-e2a007a08df2"
INTERNAL = "01a0cfdb-ed68-7182-b44f-31df22f8ea83"
ORPHAN = "01a0cdc2-d89b-7b51-983b-e47286341646"


def _dt(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def _utime(path: Path, when: datetime) -> None:
    stamp = when.timestamp()
    os.utime(path, (stamp, stamp))


def _mtime(path: Path) -> datetime:
    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)


OLD = datetime(2020, 1, 1, tzinfo=timezone.utc)


def _session(home: Path, tid: str, *, meta: dict | None = None, last: str = TS,
             day: str = "2026/09/22", local_ts: str = "2026-09-22T23-47-42") -> Path:
    """A rollout whose last record is at ``last``; its mtime is set to OLD."""
    records = [meta or session_meta(tid), turn_context(),
               token_usage(f"resp-{tid[-4:]}", thread_id=tid, inp=10, out=2, at=last)]
    path = write_rollout(home, tid, records, day=day, local_ts=local_ts)
    _utime(path, OLD)
    return path


# -- locations -------------------------------------------------------------------
def test_codex_home_honours_env(codex_home):
    assert paths.codex_home() == codex_home
    assert sessions_root() == codex_home / "sessions"


def test_codex_home_expands_user(tmp_path, monkeypatch):
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CODEX_HOME", "~/custom-codex")
    assert paths.codex_home() == tmp_path / "custom-codex"


def test_codex_home_defaults_to_profile_then_home(tmp_path, monkeypatch):
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "profile"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert paths.codex_home() == tmp_path / "profile" / ".codex"
    assert sessions_root() == tmp_path / "profile" / ".codex" / "sessions"
    monkeypatch.delenv("USERPROFILE")
    assert paths.codex_home() == tmp_path / "home" / ".codex"
    monkeypatch.setenv("CODEX_HOME", "   ")  # blank counts as unset
    assert paths.codex_home() == tmp_path / "home" / ".codex"


def test_rollout_re():
    m = ROLLOUT_RE.match(f"rollout-2026-09-22T23-47-42-{ROOT}.jsonl")
    assert m and m.group("tid") == ROOT and m.group("ts") == "2026-09-22T23-47-42"
    assert m.group("rid") is None
    m = ROLLOUT_RE.match(f"rollout-2026-09-22T23-47-42-{ROOT}_r7abc.jsonl")
    assert m and m.group("tid") == ROOT and m.group("rid") == "r7abc"
    assert not ROLLOUT_RE.match(f"rollout-2026-09-22T23-47-42-{ROOT}.jsonl.zst")
    assert not ROLLOUT_RE.match(f"rollout-2026-09-22-{ROOT}.jsonl")
    assert not ROLLOUT_RE.match("rollout-2026-09-22T23-47-42-notauuid.jsonl")


# -- session_meta ------------------------------------------------------------------
def test_read_session_meta_fields(codex_home):
    path = _session(codex_home, ROOT)
    meta = read_session_meta(path)
    assert isinstance(meta, CodexMeta)
    assert meta.thread_id == ROOT and meta.session_id == ROOT and meta.root_id == ROOT
    assert meta.parent_thread_id is None and meta.forked_from_id is None
    assert meta.thread_source == "user" and meta.source == "vscode"
    assert meta.cwd == "C:\\Git\\proj" and meta.git_branch == "main"
    assert meta.cli_version == "0.155.0" and meta.originator == "codex_vscode"
    assert meta.model == "gpt-5.6-terra"
    assert meta.started_at == _dt(TS)
    assert not meta.is_subagent and not meta.is_guardian


def test_read_session_meta_subagent_and_guardian(codex_home):
    child = _session(codex_home, CHILD, meta=subagent_meta(CHILD))
    meta = read_session_meta(child)
    assert meta.thread_id == CHILD and meta.root_id == ROOT
    assert meta.parent_thread_id == ROOT and meta.forked_from_id == ROOT
    assert meta.agent_path == "/root/dependency_audit" and meta.agent_nickname == "Pauli"
    assert meta.is_subagent and not meta.is_guardian

    guard = _session(codex_home, GUARDIAN, meta=guardian_meta())
    gmeta = read_session_meta(guard)
    assert gmeta.is_subagent and gmeta.is_guardian


def test_parent_and_path_fall_back_to_thread_spawn(codex_home):
    rec_ = subagent_meta(GRANDCHILD, parent=CHILD, agent_path="/root/a/b", nickname="Leibniz")
    for k in ("parent_thread_id", "agent_path", "agent_nickname"):
        rec_["payload"].pop(k)
    meta = read_session_meta(_session(codex_home, GRANDCHILD, meta=rec_))
    assert meta.parent_thread_id == CHILD
    assert meta.agent_path == "/root/a/b" and meta.agent_nickname == "Leibniz"


def test_guardian_detected_from_source_alone(codex_home):
    rec_ = session_meta(GUARDIAN, thread_source="user", source={"subagent": {"other": "guardian"}})
    meta = read_session_meta(_session(codex_home, GUARDIAN, meta=rec_))
    assert meta.is_guardian and meta.is_subagent


def test_partial_first_line_is_retried(codex_home):
    full = line(session_meta(ROOT))
    path = rollout_path(codex_home, ROOT)
    path.parent.mkdir(parents=True)
    path.write_bytes(full[: len(full) // 2].encode())
    assert read_session_meta(path) is None
    assert list_sessions() == []

    # the whole record, still without its newline: not complete yet either
    with open(path, "ab") as fh:
        fh.write(full[len(full) // 2:].encode())
    assert read_session_meta(path) is None
    assert list_sessions() == []

    with open(path, "ab") as fh:
        fh.write(b"\n")
    meta = read_session_meta(path)
    assert meta is not None and meta.thread_id == ROOT
    assert [s.session_id for s in list_sessions()] == [ROOT]


def test_garbled_or_foreign_first_line(codex_home):
    garbled = rollout_path(codex_home, ROOT)
    garbled.parent.mkdir(parents=True)
    garbled.write_text("{not json at all\n" + line(session_meta(ROOT)) + "\n", encoding="utf-8")
    assert read_session_meta(garbled) is None

    not_meta = write_rollout(codex_home, OTHER, [turn_context(), session_meta(OTHER)])
    assert read_session_meta(not_meta) is None

    for tid, first in ((THIRD, b"[1, 2, 3]\n"), (MEMORY, b'{"type":"session_meta"}\n'),
                       (INTERNAL, b"\xff\xfe\x00garbage\n"), (ORPHAN, b"")):
        p = rollout_path(codex_home, tid)
        p.write_bytes(first)
        assert read_session_meta(p) is None
    assert read_session_meta(codex_home / "sessions" / "missing.jsonl") is None
    assert list_sessions() == []


def test_huge_first_line(codex_home):
    big = _session(codex_home, ROOT, meta=session_meta(ROOT, pad=300_000))
    assert big.stat().st_size > 300_000
    meta = read_session_meta(big)
    assert meta is not None and meta.thread_id == ROOT

    too_big = _session(codex_home, OTHER, meta=session_meta(OTHER, pad=1_100_000))
    assert read_session_meta(too_big) is None
    assert read_session_meta(too_big) is None  # still None (and still no exception)
    assert [s.session_id for s in list_sessions()] == [ROOT]


# -- listing -----------------------------------------------------------------------------
def test_newest_first_uses_activity_not_mtime(codex_home):
    # A: old mtime but a recent last record -> activity is the record time.
    a = _session(codex_home, ROOT, last=ts(60), local_ts="2026-09-22T23-47-42")
    # B: newest by mtime, old records.
    b = _session(codex_home, OTHER, last=ts(0), local_ts="2026-09-22T23-50-00")
    b_mtime = datetime(2026, 9, 22, 23, 0, tzinfo=timezone.utc)
    _utime(b, b_mtime)
    # C: mtime a bit after its records, but older than A's record.
    c = _session(codex_home, THIRD, last=ts(0), local_ts="2026-09-22T23-55-00")
    c_mtime = datetime(2026, 9, 22, 21, 0, tzinfo=timezone.utc)
    _utime(c, c_mtime)

    got = list_sessions()
    assert [s.session_id for s in got] == [OTHER, ROOT, THIRD]
    by_id = {s.session_id: s for s in got}
    assert by_id[ROOT].mtime == _dt(ts(60))       # record time beats the OLD mtime
    assert by_id[OTHER].mtime == b_mtime          # mtime beats older records
    assert by_id[THIRD].mtime == c_mtime
    assert all(s.mtime.utcoffset().total_seconds() == 0 for s in got)
    assert _mtime(a) == OLD  # listing never touched the file


def test_session_info_fields(codex_home):
    path = _session(codex_home, ROOT, day="2026/09/23", local_ts="2026-09-23T01-02-03")
    [info] = list_sessions()
    assert info.main_path == path
    assert info.session_id == ROOT
    assert info.project_hash == "2026/09/23"
    assert info.size == path.stat().st_size


def test_subagent_threads_hidden(codex_home):
    _session(codex_home, ROOT, last=ts(1))
    _session(codex_home, CHILD, meta=subagent_meta(CHILD), last=ts(2))
    _session(codex_home, GRANDCHILD, meta=subagent_meta(GRANDCHILD, parent=CHILD), last=ts(3))
    _session(codex_home, GUARDIAN, meta=guardian_meta(), last=ts(4))
    _session(codex_home, MEMORY, last=ts(5),
             meta=session_meta(MEMORY, thread_source="memory_consolidation", source="exec"))
    _session(codex_home, INTERNAL, last=ts(6),
             meta=session_meta(INTERNAL, thread_source="user",
                               source={"internal": {"kind": "title"}}))
    # a parent pointer alone is enough to make it a sub-thread
    _session(codex_home, ORPHAN, last=ts(7), meta=session_meta(ORPHAN, parent=ROOT))

    assert [s.session_id for s in list_sessions()] == [ROOT]
    everything = list_sessions(include_subagents=True)
    assert [s.session_id for s in everything] == [
        ORPHAN, INTERNAL, MEMORY, GUARDIAN, GRANDCHILD, CHILD, ROOT]


def test_forked_child_keeps_its_own_id(codex_home):
    _session(codex_home, ROOT, last=ts(1))
    # A forked child carries a later copy of the parent's session_meta.
    child = write_rollout(codex_home, CHILD, [
        subagent_meta(CHILD), session_meta(ROOT), turn_context(),
        token_usage("r-child", thread_id=CHILD, inp=5, at=ts(9)),
    ], local_ts="2026-09-22T23-47-52")
    _utime(child, OLD)

    meta = read_session_meta(child)
    assert meta.thread_id == CHILD and meta.is_subagent
    assert [s.session_id for s in list_sessions()] == [ROOT]
    everything = list_sessions(include_subagents=True)
    assert [(s.session_id, s.main_path) for s in everything] == [
        (CHILD, child), (ROOT, rollout_path(codex_home, ROOT))]


def test_ignores_zst_archived_and_foreign_names(codex_home):
    good = _session(codex_home, ROOT)
    content = good.read_bytes()
    day = good.parent
    (day / (good.name + ".zst")).write_bytes(content)
    (day / "notes.jsonl").write_bytes(content)
    (day / f"rollout-bad-{OTHER}.jsonl").write_bytes(content)
    (codex_home / "sessions" / good.name).write_bytes(content)          # wrong depth
    (codex_home / "sessions" / "2026" / good.name).write_bytes(content)
    deep = day / "extra"
    deep.mkdir()
    (deep / good.name).write_bytes(content)
    archived = codex_home / "archived_sessions" / "2026" / "09" / "22"
    archived.mkdir(parents=True)
    (archived / f"rollout-2026-09-22T10-00-00-{OTHER}.jsonl").write_text(
        line(session_meta(OTHER)) + "\n", encoding="utf-8")
    (day / f"rollout-2026-09-22T10-00-00-{THIRD}.jsonl").mkdir()  # a directory, not a file

    assert [(s.session_id, s.main_path) for s in list_sessions(include_subagents=True)] == [
        (ROOT, good)]
    assert find_session(OTHER) is None


def test_rollout_id_suffix_matches(codex_home):
    path = write_rollout(codex_home, ROOT, [session_meta(ROOT)])
    renamed = path.with_name(f"rollout-2026-09-22T23-47-42-{ROOT}_r0123abc.jsonl")
    path.rename(renamed)
    assert [(s.session_id, s.main_path) for s in list_sessions()] == [(ROOT, renamed)]
    assert find_session(ROOT).main_path == renamed


def test_dedupe_by_thread_id_keeps_most_recent(codex_home):
    older = _session(codex_home, ROOT, last=ts(1), day="2026/09/22")
    newer = _session(codex_home, ROOT, last=ts(30), day="2026/09/23",
                     local_ts="2026-09-23T08-00-00")
    [info] = list_sessions()
    assert info.main_path == newer and info.project_hash == "2026/09/23"
    assert find_session(ROOT).main_path == newer
    # flip it: the older folder's file becomes the most recently active
    append_records(older, [token_usage("r-late", inp=1, at=ts(90))])
    _utime(older, OLD)
    [info] = list_sessions()
    assert info.main_path == older and info.mtime == _dt(ts(90))


def test_missing_root_and_explicit_root(codex_home, tmp_path):
    assert list_sessions() == []
    assert list_sessions(root=tmp_path / "nope") == []
    assert find_session(ROOT, root=tmp_path / "nope") is None
    path = write_rollout(tmp_path / "elsewhere", ROOT, [session_meta(ROOT)])
    assert [s.main_path for s in list_sessions(root=path.parents[3])] == [path]
    assert find_session(ROOT, root=path.parents[3]).main_path == path
    assert list_sessions() == []  # default root untouched


# -- find_session -------------------------------------------------------------------
def test_find_session(codex_home):
    root = _session(codex_home, ROOT)
    child = _session(codex_home, CHILD, meta=subagent_meta(CHILD), local_ts="2026-09-22T23-47-52")
    info = find_session(ROOT)
    assert info.main_path == root and info.session_id == ROOT
    assert info.project_hash == "2026/09/22" and info.size == root.stat().st_size
    sub = find_session(CHILD)  # sub-agent threads are allowed here
    assert sub.main_path == child and sub.session_id == CHILD
    assert find_session(ROOT.upper()).main_path == root
    assert find_session(OTHER) is None
    assert find_session("") is None
    assert find_session(ROOT[:8]) is None  # no prefix matching


def test_find_session_before_meta_is_written(codex_home):
    path = rollout_path(codex_home, ROOT)
    path.parent.mkdir(parents=True)
    path.write_bytes(b'{"timestamp":"2026-09-22T20:47:52.000Z","type":"sess')
    info = find_session(ROOT)
    assert info is not None and info.main_path == path and info.session_id == ROOT


# -- last_activity ---------------------------------------------------------------------
def test_last_activity_is_cached_by_path_and_size(codex_home):
    path = _session(codex_home, ROOT, last=ts(10))
    assert last_activity(path) == _dt(ts(10))

    # Same size, different content: the cached tail timestamp is reused.
    text = path.read_text(encoding="utf-8")
    assert ts(10) in text and ts(20) not in text
    path.write_text(text.replace(ts(10), ts(20)), encoding="utf-8")
    _utime(path, OLD)
    assert last_activity(path) == _dt(ts(10))

    # Growth invalidates it.
    append_records(path, [token_usage("r-2", inp=1, at=ts(30))])
    _utime(path, OLD)
    assert last_activity(path) == _dt(ts(30))

    # A newer mtime still wins over a cached tail timestamp.
    recent = datetime(2026, 9, 23, tzinfo=timezone.utc)
    _utime(path, recent)
    assert last_activity(path) == recent
    assert last_activity(path, path.stat()) == recent


def test_last_activity_widens_past_a_long_final_line(codex_home):
    path = _session(codex_home, ROOT, last=ts(5))
    long_line = rec("response_item", {"type": "message", "role": "assistant",
                                      "content": [{"type": "output_text", "text": "x" * 50_000}]},
                    at=ts(40))
    append_records(path, [long_line])
    _utime(path, OLD)
    assert last_activity(path) == _dt(ts(40))


def test_last_activity_falls_back_to_mtime(codex_home):
    # final line longer than the widened window: no line start in view
    path = _session(codex_home, ROOT, last=ts(5))
    append_records(path, [rec("compacted", {"message": "y" * 300_000}, at=ts(50))])
    when = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)
    _utime(path, when)
    assert last_activity(path) == when

    junk = codex_home / "junk.jsonl"
    junk.write_bytes(b"hello\nworld\n" + b'{"timestamp":"not a time","type":"x"}\n')
    _utime(junk, when)
    assert last_activity(junk) == when

    empty = codex_home / "empty.jsonl"
    empty.write_bytes(b"")
    _utime(empty, when)
    assert last_activity(empty) == when


def test_last_activity_uses_latest_timestamp_and_handles_a_partial_tail(codex_home):
    path = _session(codex_home, ROOT, last=ts(10))
    # records are not strictly ordered; a partial last line still has its timestamp
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(line(token_usage("r-a", inp=1, at=ts(3))) + "\n")
        fh.write(line(token_usage("r-b", inp=1, at=ts(12)))[:60])
    _utime(path, OLD)
    assert last_activity(path) == _dt(ts(12))


def test_last_activity_missing_file(codex_home):
    got = last_activity(codex_home / "sessions" / "missing.jsonl")
    assert got == datetime.fromtimestamp(0, tz=timezone.utc)


# -- thread names -------------------------------------------------------------------------
def _index(home: Path, rows: list[object]) -> Path:
    path = home / "session_index.jsonl"
    with open(path, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write((row if isinstance(row, str) else json.dumps(row)) + "\n")
    return path


def test_thread_names(codex_home):
    assert thread_names() == {}  # missing file
    path = _index(codex_home, [
        {"id": ROOT, "thread_name": "Install all packages", "updated_at": TS},
        "not json",
        "",
        "[1, 2]",
        {"id": OTHER, "thread_name": "Proceed to slice 10"},
        {"id": CHILD},                          # no name
        {"id": THIRD, "thread_name": 42},       # wrong type
        {"thread_name": "no id"},
        {"id": ROOT, "thread_name": "Renamed"},  # last entry wins
    ])
    assert thread_names() == {ROOT: "Renamed", OTHER: "Proceed to slice 10"}

    # returned dicts are copies
    thread_names()[ROOT] = "mutated"
    assert thread_names()[ROOT] == "Renamed"

    # changes to the file are picked up
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"id": ROOT, "thread_name": "Third name"}) + "\n")
        fh.write('{"id": "truncated')
    assert thread_names()[ROOT] == "Third name"


# -- caches -----------------------------------------------------------------------------
def test_clear_caches_empties_every_cache(codex_home):
    path = _session(codex_home, ROOT, last=ts(10))
    too_big = _session(codex_home, OTHER, meta=session_meta(OTHER, pad=1_100_000))
    _index(codex_home, [{"id": ROOT, "thread_name": "First"}])
    assert read_session_meta(path).cwd == "C:\\Git\\proj"
    assert read_session_meta(too_big) is None
    assert last_activity(path) == _dt(ts(10))
    assert thread_names() == {ROOT: "First"}
    assert paths._meta_cache and paths._meta_oversize
    assert paths._activity_cache and paths._names_cache

    # Rewrite everything behind the caches' backs (same sizes, same mtimes).
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace("C:\\\\Git\\\\proj", "C:\\\\Git\\\\abcd", 1)
                    .replace(ts(10), ts(20)), encoding="utf-8")
    _utime(path, OLD)
    index = codex_home / "session_index.jsonl"
    st = index.stat()
    index.write_text(json.dumps({"id": ROOT, "thread_name": "Other"}) + "\n", encoding="utf-8")
    os.utime(index, ns=(st.st_atime_ns, st.st_mtime_ns))
    assert read_session_meta(path).cwd == "C:\\Git\\proj"
    assert last_activity(path) == _dt(ts(10))
    assert thread_names() == {ROOT: "First"}

    clear_caches()
    assert not (paths._meta_cache or paths._meta_oversize
                or paths._activity_cache or paths._names_cache)
    assert read_session_meta(path).cwd == "C:\\Git\\abcd"
    assert last_activity(path) == _dt(ts(20))
    assert thread_names() == {ROOT: "Other"}


def test_list_sessions_reads_nothing_new_when_warm(codex_home, monkeypatch):
    for i, tid in enumerate((ROOT, OTHER, THIRD)):
        _session(codex_home, tid, last=ts(i), local_ts=f"2026-09-22T23-4{i}-00")
    _session(codex_home, CHILD, meta=subagent_meta(CHILD))
    first = list_sessions(include_subagents=True)

    opened: list[str] = []
    real_open = open

    def spy(file, mode="r", *a, **kw):
        opened.append(str(file))
        assert "r" in mode and not any(c in mode for c in "wax+")
        return real_open(file, mode, *a, **kw)

    monkeypatch.setattr("builtins.open", spy)
    again = list_sessions(include_subagents=True)
    assert [s.session_id for s in again] == [s.session_id for s in first]
    assert opened == []  # warm: metas and tail timestamps all come from the caches
