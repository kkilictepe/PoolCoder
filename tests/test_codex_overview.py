"""Codex picker/list previews: tail + head + meta, thread names, (path, size) cache."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from codex_records import (
    CHILD,
    ROOT,
    WINDOW,
    agent_message_item,
    compacted,
    exec_call,
    exec_js,
    exec_output,
    reasoning,
    rollout_path,
    session_meta,
    subagent_meta,
    task_complete,
    task_started,
    thread_settings,
    token_count,
    token_usage,
    ts,
    turn_context,
    usage,
    user_message_event,
    user_message_item,
    write_rollout,
)
from pool_coder.codex import overview as cov
from pool_coder.codex import paths as cpaths
from pool_coder.config import Config
from pool_coder.overview import SessionOverview
from pool_coder.paths import SessionInfo

OTHER = "01a0cf02-083f-7bf0-8695-efe1f1a05889"
IDE_PROMPT = ("# Context from my IDE setup:\n\n## Active file: a.py\n\n## Open tabs:\n"
              "- a.py: a.py\n\n## My request:\ninstall all packages\n")


# -- helpers -----------------------------------------------------------------------
def _real_line(record: dict, ordinal: int | None) -> str:
    """A line in Codex's own layout: compact, ``timestamp, ordinal?, type, payload``."""
    out: dict = {"timestamp": record["timestamp"]}
    if ordinal is not None:
        out["ordinal"] = ordinal
    out["type"] = record["type"]
    out["payload"] = record["payload"]
    return json.dumps(out, ensure_ascii=False, separators=(",", ":"))


def _write(home: Path, tid: str, records: list[dict], *, ordinal: bool = True) -> Path:
    """Write a rollout as Codex does (``ordinal=False``: the newer event format)."""
    path = rollout_path(home, tid)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        for i, r in enumerate(records):
            fh.write(_real_line(r, i if ordinal else None) + "\n")
    return path


def _append(path: Path, records: list[dict], *, ordinal: int | None = 1000) -> None:
    with open(path, "a", encoding="utf-8", newline="\n") as fh:
        for i, r in enumerate(records):
            fh.write(_real_line(r, None if ordinal is None else ordinal + i) + "\n")


def _info(path: Path, tid: str = ROOT, *, age: float = 0.0) -> SessionInfo:
    return SessionInfo(main_path=path, session_id=tid, project_hash="2026/09/22",
                       mtime=datetime.now(timezone.utc) - timedelta(seconds=age),
                       size=path.stat().st_size)


def _padding(nbytes: int) -> list[dict]:
    """Bulky tool traffic (never decoded by the overview) worth ``nbytes``."""
    out: list[dict] = []
    for i in range(nbytes // 16000 + 1):
        out.append(exec_call(f"call-pad-{i}", exec_js("echo " + "x" * 8000)))
        out.append(exec_output(f"call-pad-{i}", body="y" * 8000))
    return out


def _names(home: Path, rows: dict[str, str]) -> None:
    with open(home / "session_index.jsonl", "w", encoding="utf-8") as fh:
        for tid, name in rows.items():
            fh.write(json.dumps({"id": tid, "thread_name": name}) + "\n")


@pytest.fixture
def reads(monkeypatch):
    """Count tail and head reads of the overview module."""
    counts = {"tail": 0, "head": 0}
    real_tail, real_head = cov.read_tail_text, cov._read_head

    def tail(path, max_bytes=65536):
        counts["tail"] += 1
        return real_tail(path, max_bytes)

    def head(path):
        counts["head"] += 1
        return real_head(path)

    monkeypatch.setattr(cov, "read_tail_text", tail)
    monkeypatch.setattr(cov, "_read_head", head)
    return counts


# -- the basics --------------------------------------------------------------------------
def test_item_format_preview(codex_home):
    path = _write(codex_home, ROOT, [
        session_meta(ROOT, branch="feature/x"),
        task_started(),
        turn_context("gpt-5.6-sol"),
        user_message_item(IDE_PROMPT),
        reasoning(),
        token_usage("r1", inp=100_000, cached=90_000, out=500),
        token_count(usage(100_000, 90_000, out=500)),
        exec_call("call-1", exec_js("git status")),
        exec_output("call-1"),
        agent_message_item("done"),
        token_usage("r2", inp=129_200, cached=120_000, out=300, at=ts(1)),
        task_complete(at=ts(1)),
    ])
    ov = cov.peek_codex_session(_info(path))
    assert isinstance(ov, SessionOverview)
    assert ov.model == "gpt-5.6-sol"
    assert ov.cwd == "C:\\Git\\proj"
    assert ov.git_branch == "feature/x"
    assert ov.label == "proj"
    assert ov.context_tokens == 129_200  # input_tokens already include the cached part
    assert ov.occupancy == pytest.approx(0.5)  # of the reported 258,400
    assert ov.last_text == "install all packages"  # IDE preamble dropped
    assert ov.is_live


def test_event_format_preview(codex_home):
    path = _write(codex_home, ROOT, [
        session_meta(ROOT),
        task_started(),
        thread_settings("gpt-5.5", profile=":workspace", cwd="C:\\Git\\evented"),
        user_message_event("<environment_context>\n  <cwd>C:\\x</cwd>\n</environment_context>"
                           "\nrefactor the parser\n", "c-9"),
        token_usage("r1", inp=64_600, cached=60_000),
    ], ordinal=False)
    ov = cov.peek_codex_session(_info(path))
    assert ov.model == "gpt-5.5"
    assert ov.cwd == "C:\\Git\\evented"  # latest settings win over the meta's cwd
    assert ov.label == "evented"
    assert ov.context_tokens == 64_600
    assert ov.occupancy == pytest.approx(64_600 / WINDOW)
    assert ov.last_text == "refactor the parser"


def test_builder_layout_is_read_too(codex_home):
    # tests' default json.dumps layout: spaces, "ordinal" last, CRLF on Windows
    path = write_rollout(codex_home, ROOT, [
        session_meta(ROOT), task_started(), turn_context("gpt-5.6-terra"),
        user_message_item("hello there"), token_usage("r1", inp=25_840),
    ])
    ov = cov.peek_codex_session(_info(path))
    assert (ov.model, ov.context_tokens, ov.last_text) == ("gpt-5.6-terra", 25_840, "hello there")
    assert ov.occupancy == pytest.approx(0.1)


def test_latest_prompt_wins_and_raw_line_separators_survive(codex_home):
    path = _write(codex_home, ROOT, [
        session_meta(ROOT), turn_context(),
        user_message_item("first prompt", "c1"),
        user_message_event("second\u2028prompt", "c2"),  # raw U+2028 inside the JSON string
        user_message_item("   ", "c3"),                   # nothing left after cleaning
    ])
    assert cov.peek_codex_session(_info(path)).last_text == "second prompt"


def test_list_sessions_integration(codex_home):
    _write(codex_home, ROOT, [session_meta(ROOT), turn_context(), user_message_item("root work"),
                              token_usage("r1", inp=1000)])
    _write(codex_home, CHILD, [subagent_meta(CHILD), turn_context()])
    [info] = cpaths.list_sessions()  # the sub-agent thread is hidden
    ov = cov.peek_codex_session(info)
    assert ov.info is info
    assert (ov.last_text, ov.context_tokens) == ("root work", 1000)


# -- head and meta fallbacks -------------------------------------------------------------
def _long_turn(home: Path, *, tail: list[dict] | None = None) -> Path:
    """A rollout whose last 256 KB hold no session_meta, turn_context, window
    or prompt: one long agentic turn after the settings at the top."""
    return _write(home, ROOT, [
        session_meta(ROOT, model="gpt-5.5", pad=20_000),
        task_started(),
        turn_context("gpt-5.6-sol"),
        user_message_item("an old prompt"),
        *_padding(cov.TAIL_BYTES + 40_000),
        *(tail if tail is not None else [token_usage("r9", inp=129_200, at=ts(5))]),
    ])


def test_tail_without_meta_or_turn_context_uses_the_head(codex_home, reads):
    path = _long_turn(codex_home)
    assert path.stat().st_size > cov.TAIL_BYTES + 30_000
    ov = cov.peek_codex_session(_info(path))
    assert ov.model == "gpt-5.6-sol"                   # head's turn_context, not the meta's
    assert ov.occupancy == pytest.approx(0.5)          # head's reported 258,400 window
    assert ov.context_tokens == 129_200                # from the tail
    assert (ov.cwd, ov.git_branch) == ("C:\\Git\\proj", "main")  # from the meta
    assert ov.last_text is None                        # old prompt is outside the tail
    assert reads == {"tail": 1, "head": 1}


def test_head_is_cached_while_the_tail_grows(codex_home, reads):
    path = _long_turn(codex_home)
    cov.peek_codex_session(_info(path))
    _append(path, [token_usage("r10", inp=64_600, at=ts(6))])
    ov = cov.peek_codex_session(_info(path))
    assert ov.context_tokens == 64_600
    assert ov.model == "gpt-5.6-sol"
    assert reads == {"tail": 2, "head": 1}  # the complete head is never read again


def test_head_not_read_when_the_tail_has_everything(codex_home, reads):
    path = _long_turn(codex_home, tail=[turn_context("gpt-5.6-terra"),
                                        token_count(usage(10_000), window=250_000)])
    ov = cov.peek_codex_session(_info(path))
    assert ov.model == "gpt-5.6-terra"
    assert ov.occupancy == pytest.approx(10_000 / 250_000)
    assert reads["head"] == 0


def test_model_falls_back_to_the_meta(codex_home, reads):
    # the whole file fits in the tail and has no turn_context: no head read
    path = _write(codex_home, ROOT, [session_meta(ROOT, model="gpt-5.5"),
                                     token_usage("r1", inp=27_200)])
    ov = cov.peek_codex_session(_info(path))
    assert ov.model == "gpt-5.5"
    assert reads == {"tail": 1, "head": 0}
    # a small tail window over the same file does consult the head (none there)
    ov = cov.peek_codex_session(_info(path), tail_bytes=512)
    assert ov.model == "gpt-5.5"
    assert reads["head"] == 1


# -- context and window ------------------------------------------------------------------
def test_window_falls_back_to_config(codex_home):
    path = _write(codex_home, ROOT, [session_meta(ROOT), turn_context("gpt-5.6-terra"),
                                     token_usage("r1", inp=136_000)])
    assert cov.peek_codex_session(_info(path)).occupancy == pytest.approx(0.5)  # 272,000
    ov = cov.peek_codex_session(_info(path), Config(gpt_window=400_000))
    assert ov.occupancy == pytest.approx(136_000 / 400_000)


def test_unknown_model_still_gets_the_gpt_window(codex_home, monkeypatch):
    path = _write(codex_home, ROOT, [session_meta(ROOT), token_usage("r1", inp=136_000)])
    monkeypatch.setattr(cpaths, "read_session_meta", lambda p: None)
    ov = cov.peek_codex_session(_info(path))
    assert ov.model is None and ov.cwd is None
    assert ov.occupancy == pytest.approx(0.5)


def test_reported_window_task_started_or_token_count(codex_home):
    path = _write(codex_home, ROOT, [session_meta(ROOT), turn_context(),
                                     task_started(window=200_000),
                                     token_usage("r1", inp=50_000)])
    assert cov.peek_codex_session(_info(path)).occupancy == pytest.approx(0.25)
    _append(path, [token_count(usage(50_000), window=100_000)])
    assert cov.peek_codex_session(_info(path)).occupancy == pytest.approx(0.5)


def test_context_ignores_other_threads_and_empty_responses(codex_home):
    path = _write(codex_home, ROOT, [
        session_meta(ROOT), turn_context(),
        token_usage("r1", inp=51_680),
        token_usage("r2", inp=200_000, thread_id=OTHER),  # another thread's usage
        token_usage("r3", inp=0, out=40),                  # no prompt side
        token_count(usage(99_999)),                        # primary data wins over this
    ])
    ov = cov.peek_codex_session(_info(path))
    assert ov.context_tokens == 51_680
    assert ov.occupancy == pytest.approx(0.2)


def test_context_falls_back_to_token_count(codex_home):
    path = _write(codex_home, ROOT, [
        session_meta(ROOT), turn_context(),
        token_count(usage(300_000), usage(38_760, cached=30_000)),
        token_count(None),                                 # info null: ignored
        token_usage("r1", inp=900, thread_id=OTHER),       # not ours: still fallback
    ], ordinal=False)
    ov = cov.peek_codex_session(_info(path))
    assert ov.context_tokens == 38_760                     # last_token_usage, not the total
    assert ov.occupancy == pytest.approx(0.15)


def _compaction_estimate(total: int) -> dict:
    """The token_count right after ``compacted``: empty last usage, estimate in total_tokens."""
    return token_count(usage(900_000), {**usage(), "total_tokens": total})


@pytest.mark.parametrize("history_bytes", [0, 120_000])
def test_context_after_a_manual_compaction(codex_home, monkeypatch, history_bytes):
    marker = compacted()
    marker["payload"]["replacement_history"] = [{"type": "message", "text": "h" * history_bytes}]
    path = _write(codex_home, ROOT, [
        session_meta(ROOT), turn_context(), task_started(),
        token_usage("r1", inp=204_238),      # the summary request: the old context
        marker,
        _compaction_estimate(6_820),         # Codex's own estimate afterwards
        task_complete(),
    ])
    decoded: list[str] = []
    real_parse = cov.parse_codex_line

    def parse(line):
        rec = real_parse(line)
        decoded.append(rec.type if rec else "?")
        return rec

    monkeypatch.setattr(cov, "parse_codex_line", parse)
    ov = cov.peek_codex_session(_info(path))
    assert ov.context_tokens == 6_820
    assert ov.occupancy == pytest.approx(6_820 / WINDOW)
    assert "compacted" not in decoded  # recognised from its first bytes only
    # the next response reports the real size
    _append(path, [task_started(), token_usage("r2", inp=31_000), _compaction_estimate(1)])
    assert cov.peek_codex_session(_info(path)).context_tokens == 31_000


def test_empty_last_usage_without_a_compaction_keeps_the_context(codex_home):
    path = _write(codex_home, ROOT, [session_meta(ROOT), turn_context(),
                                     token_usage("r1", inp=51_680), _compaction_estimate(6_820)])
    assert cov.peek_codex_session(_info(path)).context_tokens == 51_680


def test_other_threads_settings_and_prompts_ignored(codex_home):
    other_prompt = user_message_item("someone else's prompt", "c9")
    other_prompt["payload"]["thread_id"] = OTHER
    path = _write(codex_home, ROOT, [
        session_meta(ROOT), turn_context("gpt-5.6-sol"),
        user_message_item("mine", "c1"),
        thread_settings("gpt-5.5", tid=OTHER),
        other_prompt,
    ])
    ov = cov.peek_codex_session(_info(path))
    assert (ov.model, ov.last_text) == ("gpt-5.6-sol", "mine")


def test_subagent_thread_uses_its_own_usage(codex_home):
    path = _write(codex_home, CHILD, [
        subagent_meta(CHILD, cwd="C:\\Git\\child"),
        token_usage("p1", inp=100_000, thread_id=ROOT),   # copied parent history
        token_usage("c1", inp=25_840, thread_id=CHILD),
    ])
    ov = cov.peek_codex_session(_info(path, CHILD))
    assert ov.context_tokens == 25_840
    assert ov.label == "child"


# -- prompt fallback -------------------------------------------------------------------
def test_thread_name_when_no_prompt(codex_home):
    path = _long_turn(codex_home)
    _names(codex_home, {ROOT: "  Install   all packages ", OTHER: "Other thread"})
    assert cov.peek_codex_session(_info(path)).last_text == "Install all packages"
    # a rename shows up although the rollout did not change
    _names(codex_home, {ROOT: "Renamed thread"})
    assert cov.peek_codex_session(_info(path)).last_text == "Renamed thread"


def test_thread_name_is_marked_as_a_title(codex_home):
    # The last prompt left the 256 KB tail: the thread's name stands in, and is
    # shown as a title so it never reads as the last prompt.
    path = _long_turn(codex_home)
    _names(codex_home, {ROOT: "Auto generated title"})
    ov = cov.peek_codex_session(_info(path))
    assert (ov.last_text, ov.last_is_title) == ("Auto generated title", True)
    assert ov.last_shown == "[Auto generated title]"
    path = _write(codex_home, ROOT, [session_meta(ROOT), user_message_event("the prompt")],
                  ordinal=False)
    ov = cov.peek_codex_session(_info(path))
    assert (ov.last_is_title, ov.last_shown) == (False, "the prompt")


def test_prompt_wins_over_thread_name(codex_home):
    _names(codex_home, {ROOT: "Thread name"})
    path = _write(codex_home, ROOT, [session_meta(ROOT), user_message_event("the prompt")],
                  ordinal=False)
    assert cov.peek_codex_session(_info(path)).last_text == "the prompt"
    # an attachments-only prompt (nothing after "## My request:") falls back to the name
    path = _write(codex_home, ROOT, [session_meta(ROOT),
                                     user_message_event("# Context from my IDE setup:\n\n"
                                                        "## My request:\n")], ordinal=False)
    assert cov.peek_codex_session(_info(path)).last_text == "Thread name"


# -- caching -----------------------------------------------------------------------------
def test_unchanged_file_is_served_from_cache(codex_home, reads):
    path = _write(codex_home, ROOT, [session_meta(ROOT), turn_context(),
                                     user_message_item("cached"), token_usage("r1", inp=1000)])
    first = cov.peek_codex_session(_info(path))
    second = cov.peek_codex_session(_info(path, age=3600))
    assert reads["tail"] == 1
    assert second.last_text == first.last_text == "cached"
    # liveness comes from the SessionInfo passed in, never from the cache
    assert first.is_live and not second.is_live
    assert second.info.mtime < first.info.mtime


def test_appended_file_is_read_again(codex_home, reads):
    path = _write(codex_home, ROOT, [session_meta(ROOT), turn_context(),
                                     user_message_item("before"), token_usage("r1", inp=1000)])
    stale = _info(path)
    cov.peek_codex_session(stale)
    _append(path, [user_message_item("after", "c2"), token_usage("r2", inp=2000)])
    ov = cov.peek_codex_session(stale)  # a stale info.size does not hide the growth
    assert reads["tail"] == 2
    assert (ov.last_text, ov.context_tokens) == ("after", 2000)
    cov.peek_codex_session(_info(path))
    assert reads["tail"] == 2


def test_tail_bytes_is_part_of_the_cache_key_and_clear_caches(codex_home, reads):
    path = _write(codex_home, ROOT, [session_meta(ROOT), turn_context(),
                                     token_usage("r1", inp=1000)])
    cov.peek_codex_session(_info(path))
    cov.peek_codex_session(_info(path), tail_bytes=4096)
    assert reads["tail"] == 2
    cov.clear_caches()
    cov.peek_codex_session(_info(path))
    assert reads["tail"] == 3


# -- pre-filter --------------------------------------------------------------------------
@pytest.mark.parametrize("layout", ["codex", "builder"])
def test_bulky_records_are_never_decoded(codex_home, monkeypatch, layout):
    records = [session_meta(ROOT), task_started(), turn_context(), user_message_item("go"),
               reasoning("thinking"), agent_message_item("msg"),
               *_padding(60_000), token_usage("r1", inp=2584), task_complete()]
    if layout == "codex":
        path = _write(codex_home, ROOT, records)
    else:
        path = write_rollout(codex_home, ROOT, records)
    decoded: list[str] = []
    real_parse = cov.parse_codex_line

    def parse(line):
        rec = real_parse(line)
        decoded.append(rec.ptype or rec.type if rec else "?")
        return rec

    monkeypatch.setattr(cov, "parse_codex_line", parse)
    ov = cov.peek_codex_session(_info(path))
    assert (ov.last_text, ov.context_tokens) == ("go", 2584)
    # only the records the preview needs (the meta line is read by codex.paths)
    assert sorted(decoded) == ["item_completed", "task_started", "token_usage_record",
                               "turn_context"]


def test_wanted_falls_back_to_decoding_unknown_layouts():
    assert cov._wanted('{"type":"response_item","timestamp":"x","payload":{}}')
    assert not cov._wanted('{"timestamp":"x","ordinal":3,"type":"response_item","payload":{}}')
    assert not cov._wanted('{"timestamp": "x", "type": "compacted", "payload": {}}')
    assert not cov._wanted('{"timestamp":"x","type":"event_msg","payload":{"type":"exec_command_end"}}')
    assert cov._wanted('{"timestamp":"x","type":"event_msg","payload":{"thread_id":"t"}}')
    assert not cov._wanted("")


# -- robustness --------------------------------------------------------------------------
def test_missing_file_and_garbage_never_raise(codex_home):
    gone = rollout_path(codex_home, OTHER)
    info = SessionInfo(main_path=gone, session_id=OTHER, project_hash="2026/09/22",
                       mtime=datetime(2020, 1, 1, tzinfo=timezone.utc), size=123)
    ov = cov.peek_codex_session(info)
    assert (ov.model, ov.cwd, ov.context_tokens, ov.last_text) == (None, None, 0, None)
    assert ov.label == "2026/09/22" and not ov.is_live
    assert ov.occupancy == 0.0

    path = rollout_path(codex_home, ROOT)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(_real_line(session_meta(ROOT), 0) + "\n")
        fh.write("not json\n[1, 2]\n\n")
        fh.write('{"timestamp":"x","ordinal":1,"type":"event_msg","payload":{"type":"token_count","info":7}}\n')
        fh.write('{"timestamp":"x","type":"token_usage_record","payload":{"thread_id":"%s","usage":[1]}}\n' % ROOT)
        fh.write('{"timestamp":"x","type":"turn_context","payload":{"model":42,"cwd":null}}\n')
        fh.write('{"timestamp":"x","type":"event_msg","payload":{"type":"task_started","model_context_window":true}}\n')
        fh.write('{"timestamp":"x","type":"event_msg","payload":{"type":"item_completed","item":{"type":"UserMessage","content":"nope"}}}\n')
        fh.write('{"timestamp":"x","type":"event_msg","payload":{"type":"user_message","message":["x"]}}\n')
        fh.write('{"timestamp":"x","type":"event_msg","payload":{"type":"token_cou')  # partial
    ov = cov.peek_codex_session(_info(path))
    assert (ov.model, ov.context_tokens, ov.last_text) == ("gpt-5.6-terra", 0, None)
    assert ov.label == "proj"
