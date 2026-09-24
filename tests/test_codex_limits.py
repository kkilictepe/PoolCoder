"""Codex plan limits: rate-limit parsing and the local rollout poller."""

from __future__ import annotations

import ast
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from codex_records import (
    CHILD,
    ROOT,
    append_records,
    line,
    rate_limits,
    rec,
    session_meta,
    subagent_meta,
    token_count,
    ts,
    usage,
    window,
    write_rollout,
)
from pool_coder.codex import paths as cpaths
from pool_coder.snapshot import PlanLimitsView
from pool_coder.sources import codex_limits
from pool_coder.sources.codex_limits import (
    MIN_INTERVAL,
    NO_DATA,
    RECENT_FILES,
    CodexLimitsSource,
    latest_rate_limits,
    parse_rate_limits,
    window_label,
)

AT = datetime(2026, 9, 22, 20, 47, 52, tzinfo=timezone.utc)
BASE = datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc)  # file mtimes, after every record
RESET = 1790000000  # epoch seconds
BUSINESS = rate_limits(plan_type="business",
                       credits={"has_credits": True, "unlimited": True, "balance": None})


def _dt(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def _tid(n: int) -> str:
    return f"01a0d000-0000-7000-8000-{n:012d}"


def _utime(path: Path, when: datetime) -> None:
    stamp = when.timestamp()
    os.utime(path, (stamp, stamp))


def _rollout(home: Path, tid: str, records: list[dict], *, mtime: datetime | None = None,
             meta: dict | None = None) -> Path:
    path = write_rollout(home, tid, [meta or session_meta(tid)] + records)
    if mtime is not None:
        _utime(path, mtime)
    return path


def _limits(pct: float, minutes: int = 300, plan: str = "plus") -> dict:
    return rate_limits(window(pct, minutes, resets_at=RESET), None, plan_type=plan)


# -- parse_rate_limits: resets -----------------------------------------------------
def test_resets_at_is_epoch_seconds_utc():
    view = parse_rate_limits(rate_limits(window(12.5, 300, resets_at=RESET)), AT)
    assert view.available is True and view.error is None
    assert view.five_hour_pct == 12.5
    assert view.five_hour_resets_at == datetime.fromtimestamp(RESET, tz=timezone.utc)
    assert view.five_hour_resets_at.tzinfo is not None
    assert view.as_of == AT


def test_resets_in_seconds_is_relative_to_as_of():
    view = parse_rate_limits(rate_limits(window(40.0, 300, resets_in_seconds=3600),
                                         window(10.0, 10080, resets_in_seconds=86400)), AT)
    assert view.five_hour_resets_at == AT + timedelta(hours=1)
    assert view.seven_day_resets_at == AT + timedelta(days=1)


def test_resets_at_wins_over_resets_in_seconds():
    w = window(1.0, 300, resets_at=RESET, resets_in_seconds=60)
    assert parse_rate_limits(rate_limits(w), AT).five_hour_resets_at == \
        datetime.fromtimestamp(RESET, tz=timezone.utc)


def test_relative_reset_without_as_of_is_unknown():
    view = parse_rate_limits(rate_limits(window(5.0, 300, resets_in_seconds=60)), None)
    assert view.available is True
    assert view.five_hour_resets_at is None and view.as_of is None


def test_millisecond_resets_at_is_tolerated():
    view = parse_rate_limits(rate_limits(window(5.0, 300, resets_at=RESET * 1000)), AT)
    assert view.five_hour_resets_at == datetime.fromtimestamp(RESET, tz=timezone.utc)


# -- parse_rate_limits: classification by window_minutes ----------------------------
def test_normal_five_hour_and_weekly():
    view = parse_rate_limits(rate_limits(window(30.0, 300, resets_at=RESET),
                                         window(55.5, 10080, resets_at=RESET + 1)), AT)
    assert (view.five_hour_pct, view.five_hour_label) == (30.0, "5-hour")
    assert (view.seven_day_pct, view.seven_day_label) == (55.5, "weekly")
    assert view.seven_day_resets_at == datetime.fromtimestamp(RESET + 1, tz=timezone.utc)
    assert view.plan_type == "plus" and view.note is None


def test_weekly_primary_with_null_secondary_lands_in_seven_day_slot():
    view = parse_rate_limits(rate_limits(window(42.0, 10080, resets_at=RESET), None), AT)
    assert view.five_hour_pct is None and view.five_hour_resets_at is None
    assert view.seven_day_pct == 42.0
    assert view.seven_day_label == "weekly"
    assert view.seven_day_resets_at == datetime.fromtimestamp(RESET, tz=timezone.utc)


def test_classified_by_minutes_not_by_position():
    # A weekly primary with a 5-hour secondary: slots follow the lengths.
    view = parse_rate_limits(rate_limits(window(70.0, 10080), window(20.0, 300)), AT)
    assert (view.five_hour_pct, view.five_hour_label) == (20.0, "5-hour")
    assert (view.seven_day_pct, view.seven_day_label) == (70.0, "weekly")


def test_short_secondary_alone_uses_five_hour_slot():
    view = parse_rate_limits(rate_limits(None, window(9.0, 300)), AT)
    assert view.five_hour_pct == 9.0 and view.seven_day_pct is None


@pytest.mark.parametrize("minutes, label", [
    (300, "5-hour"), (10080, "weekly"), (60, "1h"), (120, "2h"), (1440, "1d"),
    (2880, "2d"), (43200, "30d"), (90, "90m"), (1, "1m"),
])
def test_window_labels(minutes, label):
    assert window_label(minutes, "x") == label


@pytest.mark.parametrize("minutes, slot, label", [
    (301, "five_hour", "301m"),
    (720, "five_hour", "12h"),
    (1439, "five_hour", "1439m"),
    (1440, "five_hour", "1d"),      # up to one day: the short slot
    (1441, "seven_day", "1441m"),
    (2880, "seven_day", "2d"),
])
@pytest.mark.parametrize("position", ["primary", "secondary"])
def test_a_lone_window_takes_its_slot_by_length(minutes, slot, label, position):
    w = window(7.0, minutes, resets_at=RESET)
    rl = rate_limits(w, None) if position == "primary" else rate_limits(None, w)
    view = parse_rate_limits(rl, AT)
    other = "seven_day" if slot == "five_hour" else "five_hour"
    assert getattr(view, f"{slot}_pct") == 7.0
    assert getattr(view, f"{slot}_label") == label
    assert getattr(view, f"{other}_pct") is None


def test_odd_windows_are_labelled():
    view = parse_rate_limits(rate_limits(window(1.0, 60), window(2.0, 2880)), AT)
    assert (view.five_hour_label, view.five_hour_pct) == ("1h", 1.0)
    assert (view.seven_day_label, view.seven_day_pct) == ("2d", 2.0)
    view = parse_rate_limits(rate_limits(window(3.0, 90)), AT)
    assert (view.five_hour_label, view.five_hour_pct) == ("90m", 3.0)
    assert view.seven_day_pct is None


def test_two_short_windows_fill_both_slots_shorter_first():
    view = parse_rate_limits(rate_limits(window(50.0, 300), window(10.0, 60)), AT)
    assert (view.five_hour_label, view.five_hour_pct) == ("1h", 10.0)
    assert (view.seven_day_label, view.seven_day_pct) == ("5-hour", 50.0)


def test_two_long_windows_fill_both_slots_shorter_first():
    view = parse_rate_limits(rate_limits(window(80.0, 43200), window(30.0, 10080)), AT)
    assert (view.five_hour_label, view.five_hour_pct) == ("weekly", 30.0)
    assert (view.seven_day_label, view.seven_day_pct) == ("30d", 80.0)


def test_window_without_minutes_keeps_codex_convention():
    view = parse_rate_limits(rate_limits({"used_percent": 5.0}, {"used_percent": 6.0}), AT)
    assert (view.five_hour_label, view.five_hour_pct) == ("5-hour", 5.0)
    assert (view.seven_day_label, view.seven_day_pct) == ("weekly", 6.0)
    # A known window keeps its slot; the unknown one takes the free slot.
    view = parse_rate_limits(rate_limits({"used_percent": 5.0}, window(6.0, 300)), AT)
    assert (view.five_hour_label, view.five_hour_pct) == ("5-hour", 6.0)
    assert (view.seven_day_label, view.seven_day_pct) == ("weekly", 5.0)


# -- parse_rate_limits: plan, credits, notes ------------------------------------
def test_business_unlimited_with_null_windows_is_available():
    view = parse_rate_limits(BUSINESS, AT)
    assert view.available is True and view.error is None
    assert view.note == "unlimited credits"
    assert view.plan_type == "business"
    assert view.five_hour_pct is None and view.seven_day_pct is None
    assert view.five_hour_resets_at is None and view.seven_day_resets_at is None
    assert (view.five_hour_label, view.seven_day_label) == ("5-hour", "weekly")
    assert view.as_of == AT


def test_real_business_shape_from_survey():
    rl = {"limit_id": "codex", "limit_name": None, "primary": None, "secondary": None,
          "credits": {"has_credits": True, "unlimited": True, "balance": None},
          "individual_limit": None, "spend_control_reached": None,
          "plan_type": "business", "rate_limit_reached_type": None}
    view = parse_rate_limits(rl, AT)
    assert (view.available, view.plan_type, view.note) == (True, "business", "unlimited credits")


def test_credits_balance_note():
    rl = rate_limits(window(10.0, 300), credits={"has_credits": True, "unlimited": False,
                                                 "balance": "12.50"})
    assert parse_rate_limits(rl, AT).note == "credits 12.50"
    rl = rate_limits(credits={"has_credits": True, "unlimited": False, "balance": 7})
    assert parse_rate_limits(rl, AT).note == "credits 7"


def test_no_credit_note_without_balance():
    rl = rate_limits(credits={"has_credits": False, "unlimited": False, "balance": None})
    assert parse_rate_limits(rl, AT).note is None


def test_limit_reached_note():
    rl = rate_limits(window(100.0, 300), reached="rate_limit_reached")
    assert parse_rate_limits(rl, AT).note == "limit reached: rate_limit_reached"


def test_notes_are_joined():
    rl = rate_limits(window(100.0, 300), reached="workspace_owner_credits_depleted",
                     credits={"has_credits": True, "unlimited": False, "balance": "0"})
    assert parse_rate_limits(rl, AT).note == \
        "credits 0 · limit reached: workspace_owner_credits_depleted"


def test_garbage_fields_do_not_raise():
    rl = {"primary": {"used_percent": "n/a", "window_minutes": "soon", "resets_at": "never",
                      "resets_in_seconds": [1]},
          "secondary": "oops", "credits": ["x"], "plan_type": 3,
          "rate_limit_reached_type": {"kind": "x"}}
    view = parse_rate_limits(rl, AT)
    assert view.available is True
    assert view.five_hour_pct is None and view.five_hour_resets_at is None
    assert view.five_hour_label == "5-hour"
    assert view.plan_type is None and view.note is None
    assert parse_rate_limits(None, AT).available is False  # type: ignore[arg-type]
    huge = parse_rate_limits(rate_limits(window(1.0, 300, resets_at=10 ** 30)), AT)
    assert huge.available is True and huge.five_hour_resets_at is None
    far = parse_rate_limits(rate_limits(window(1.0, 300, resets_in_seconds=10 ** 30)), AT)
    assert far.five_hour_resets_at is None


def test_numeric_strings_and_iso_resets_are_accepted():
    w = {"used_percent": "12.5", "window_minutes": "300", "resets_at": "2026-09-23T01:00:00Z"}
    view = parse_rate_limits(rate_limits(w), AT)
    assert view.five_hour_pct == 12.5 and view.five_hour_label == "5-hour"
    assert view.five_hour_resets_at == _dt("2026-09-23T01:00:00Z")


# -- latest_rate_limits (one file) -------------------------------------------------
def test_latest_in_file_skips_null_rate_limits(codex_home):
    path = _rollout(codex_home, ROOT, [
        token_count(usage(10), rate_limits=_limits(10.0), at=ts(1)),
        token_count(usage(20), rate_limits=_limits(20.0), at=ts(2)),
        token_count(usage(30), rate_limits=None, at=ts(3)),
    ])
    at, rl = latest_rate_limits(path)
    assert at == _dt(ts(2))
    assert rl["primary"]["used_percent"] == 20.0


def test_info_null_token_count_still_carries_rate_limits(codex_home):
    path = _rollout(codex_home, ROOT, [token_count(None, rate_limits=BUSINESS, at=ts(4))])
    at, rl = latest_rate_limits(path)
    assert at == _dt(ts(4)) and rl["plan_type"] == "business"


def test_only_token_count_event_msgs_count(codex_home):
    fake = rec("response_item", {"type": "token_count", "rate_limits": _limits(99.0)}, ts(9))
    mention = rec("response_item", {"type": "function_call_output", "call_id": "c",
                                    "output": 'grep "token_count" rate_limits'}, ts(8))
    path = _rollout(codex_home, ROOT, [
        token_count(usage(1), rate_limits=_limits(5.0), at=ts(1)), fake, mention,
    ])
    at, rl = latest_rate_limits(path)
    assert at == _dt(ts(1)) and rl["primary"]["used_percent"] == 5.0


def test_tail_cap_drops_leading_partial_line(codex_home):
    early = token_count(usage(1), rate_limits=_limits(1.0), at=ts(1))
    late = token_count(usage(2), rate_limits=_limits(2.0), at=ts(2))
    filler = [rec("response_item", {"type": "message", "role": "assistant",
                                    "content": [{"type": "output_text", "text": "y" * 500}]},
                  ts(3)) for _ in range(20)]
    path = _rollout(codex_home, ROOT, [early, late] + filler)
    size = path.stat().st_size
    tail_len = sum(len(line(r)) + 1 for r in filler)
    # The window ends mid-way through `late`: that partial line is dropped.
    assert latest_rate_limits(path, max_bytes=tail_len + 20) is None
    assert latest_rate_limits(path, max_bytes=size)[0] == _dt(ts(2))


def test_partial_last_line_is_ignored(codex_home):
    path = _rollout(codex_home, ROOT, [token_count(usage(1), rate_limits=_limits(3.0), at=ts(1))])
    with open(path, "ab") as fh:
        fh.write(line(token_count(usage(2), rate_limits=_limits(4.0), at=ts(2)))[:120].encode())
    at, rl = latest_rate_limits(path)
    assert at == _dt(ts(1)) and rl["primary"]["used_percent"] == 3.0


def test_missing_file_is_none(codex_home):
    assert latest_rate_limits(codex_home / "nope.jsonl") is None


# -- CodexLimitsSource.fetch_once -----------------------------------------------------
def test_no_data_yet(codex_home):
    src = CodexLimitsSource()
    assert src.view.available is False and src.view.error == "not polled yet"
    view = src.fetch_once()
    assert view.available is False and view.error == NO_DATA == "no Codex rate-limit data yet"
    assert src.view is view


def test_no_token_count_records_is_no_data(codex_home):
    _rollout(codex_home, ROOT, [rec("event_msg", {"type": "task_started"}, ts(1))])
    view = CodexLimitsSource().fetch_once()
    assert (view.available, view.error) == (False, NO_DATA)


def test_business_view_from_rollout(codex_home):
    _rollout(codex_home, ROOT, [token_count(usage(100), rate_limits=BUSINESS, at=ts(5))])
    view = CodexLimitsSource().fetch_once()
    assert view.available is True
    assert (view.plan_type, view.note) == ("business", "unlimited credits")
    assert view.as_of == _dt(ts(5))


def test_latest_timestamp_wins_not_file_order_or_mtime(codex_home):
    # Most recently modified file, but its snapshot is the oldest.
    _rollout(codex_home, _tid(1), [token_count(usage(1), rate_limits=_limits(90.0), at=ts(5))],
             mtime=BASE)
    # Least recently modified file holds the newest snapshot (written early in the file,
    # followed by a newer-looking but null one).
    _rollout(codex_home, _tid(2), [
        token_count(usage(1), rate_limits=_limits(33.0, plan="pro"), at=ts(20)),
        token_count(usage(2), rate_limits=None, at=ts(21)),
    ], mtime=BASE - timedelta(days=1))
    _rollout(codex_home, _tid(3), [token_count(usage(1), rate_limits=_limits(60.0), at=ts(10))],
             mtime=BASE - timedelta(hours=1))
    order = [s.session_id for s in cpaths.list_sessions(include_subagents=True)]
    assert order[0] == _tid(1)  # sanity: file order differs from timestamp order
    view = CodexLimitsSource().fetch_once()
    assert view.five_hour_pct == 33.0
    assert view.plan_type == "pro"
    assert view.as_of == _dt(ts(20))


def test_subagent_rollout_counts(codex_home):
    _rollout(codex_home, ROOT, [token_count(usage(1), rate_limits=_limits(10.0), at=ts(1))])
    _rollout(codex_home, CHILD, [token_count(usage(1), rate_limits=_limits(11.0), at=ts(2))],
             meta=subagent_meta(CHILD))
    assert [s.session_id for s in cpaths.list_sessions()] == [ROOT]  # child is hidden there
    view = CodexLimitsSource().fetch_once()
    assert view.five_hour_pct == 11.0 and view.as_of == _dt(ts(2))


def test_only_the_most_recent_rollouts_are_read(codex_home):
    for n in range(RECENT_FILES):
        _rollout(codex_home, _tid(10 + n),
                 [token_count(usage(1), rate_limits=_limits(float(n)), at=ts(1, n))],
                 mtime=BASE - timedelta(minutes=n))
    # Ninth by activity: its record is newer than all of the above, but it
    # is not among the 8 most recently active files.
    _rollout(codex_home, _tid(40), [token_count(usage(1), rate_limits=_limits(99.0), at=ts(30))],
             mtime=_dt(ts(30)))
    view = CodexLimitsSource().fetch_once()
    assert view.five_hour_pct == float(RECENT_FILES - 1)
    assert view.as_of == _dt(ts(1, RECENT_FILES - 1))


def test_older_rollouts_are_read_while_recent_ones_have_no_data(codex_home):
    for n in range(RECENT_FILES):  # e.g. freshly spawned sub-agents: no token_count yet
        _rollout(codex_home, _tid(10 + n), [rec("event_msg", {"type": "task_started"}, ts(2))],
                 mtime=BASE - timedelta(minutes=n))
    _rollout(codex_home, _tid(40), [token_count(usage(1), rate_limits=_limits(64.0), at=ts(1))],
             mtime=BASE - timedelta(days=1))
    view = CodexLimitsSource().fetch_once()
    assert view.available is True and view.five_hour_pct == 64.0


def test_newer_snapshot_replaces_and_known_one_is_kept(codex_home):
    path = _rollout(codex_home, ROOT, [token_count(usage(1), rate_limits=_limits(10.0), at=ts(1))])
    src = CodexLimitsSource()
    assert src.fetch_once().five_hour_pct == 10.0
    append_records(path, [token_count(usage(2), rate_limits=_limits(25.0), at=ts(2))])
    view = src.fetch_once()
    assert view.five_hour_pct == 25.0 and view.as_of == _dt(ts(2))
    # The file disappears (or only has newer files without data): keep what we know.
    path.unlink()
    _rollout(codex_home, _tid(5), [rec("event_msg", {"type": "task_started"}, ts(9))])
    view = src.fetch_once()
    assert view.available is True and view.five_hour_pct == 25.0 and view.as_of == _dt(ts(2))


def test_unchanged_rollouts_are_not_reopened(codex_home, monkeypatch):
    # On Windows an open handle blocks Codex from renaming (archiving) or
    # deleting a rollout: a rescan must not reopen files that did not change.
    path = _rollout(codex_home, ROOT, [token_count(usage(1), rate_limits=_limits(10.0), at=ts(1))])
    _rollout(codex_home, _tid(1), [rec("event_msg", {"type": "task_started"}, ts(0))],
             mtime=BASE - timedelta(hours=1))  # no data: remembered as such
    opened: list[str] = []
    real = codex_limits._read_tail

    def counting(p, max_bytes):
        opened.append(Path(p).name)
        return real(p, max_bytes)

    monkeypatch.setattr(codex_limits, "_read_tail", counting)
    src = CodexLimitsSource()
    assert src.fetch_once().five_hour_pct == 10.0 and len(opened) == 2
    assert src.fetch_once().five_hour_pct == 10.0 and len(opened) == 2
    append_records(path, [token_count(usage(2), rate_limits=_limits(25.0), at=ts(2))])
    assert src.fetch_once().five_hour_pct == 25.0 and opened[2:] == [path.name]


def test_unreadable_rollout_is_retried(codex_home, monkeypatch):
    _rollout(codex_home, ROOT, [token_count(usage(1), rate_limits=_limits(10.0), at=ts(1))])
    real = codex_limits._read_tail
    failing = {"on": True}

    def flaky(p, max_bytes):
        if failing["on"]:
            raise PermissionError(32, "in use")  # a transient Windows sharing error
        return real(p, max_bytes)

    monkeypatch.setattr(codex_limits, "_read_tail", flaky)
    src = CodexLimitsSource()
    assert src.fetch_once().available is False
    failing["on"] = False
    assert src.fetch_once().five_hour_pct == 10.0


def test_older_snapshot_never_replaces_a_newer_known_one(codex_home):
    newer = _rollout(codex_home, _tid(1), [token_count(usage(1), rate_limits=_limits(40.0),
                                                       at=ts(9))], mtime=BASE)
    _rollout(codex_home, _tid(2), [token_count(usage(1), rate_limits=_limits(15.0), at=ts(2))],
             mtime=BASE - timedelta(hours=1))
    src = CodexLimitsSource()
    assert src.fetch_once().five_hour_pct == 40.0
    newer.unlink()  # e.g. rewritten/compressed: only the older snapshot is visible now
    view = src.fetch_once()
    assert view.five_hour_pct == 40.0 and view.as_of == _dt(ts(9))


def test_root_argument_overrides_codex_home(codex_home, tmp_path):
    write_rollout(tmp_path / "fake_home", ROOT,
                  [session_meta(), token_count(usage(1), rate_limits=_limits(77.0), at=ts(1))])
    src = CodexLimitsSource(root=tmp_path / "fake_home" / "sessions")
    assert src.fetch_once().five_hour_pct == 77.0
    assert CodexLimitsSource().fetch_once().available is False  # CODEX_HOME is empty


def test_fetch_once_never_raises_on_garbage(codex_home):
    sessions = codex_home / "sessions" / "2026" / "09" / "22"
    good = _rollout(codex_home, ROOT, [
        token_count(usage(1), rate_limits=_limits(12.0), at=ts(1)),
    ])
    with open(good, "ab") as fh:
        fh.write(b"\xff\xfe garbage \x00\n{not json\n[1,2]\n\"token_count\"\n")
        fh.write(b'{"timestamp":"bad","type":"event_msg","payload":{"type":"token_count",'
                 b'"rate_limits":{"plan_type":"x"}}}\n')
        fh.write(b'{"type":"event_msg","payload":{"type":"token_count","rate_limits":[]}}\n')
        fh.write(b'{"timestamp":"2026-09-22T21:00:00Z","type":"event_msg",'
                 b'"payload":"token_count"}\n')
        fh.write(b'{"timestamp":"2026-09-22T21:00:00Z","type":"event_msg","payload":{"type":"tok')
    # A rollout whose first line is still being written, a binary one, an empty one.
    (sessions / f"rollout-2026-09-22T23-50-00-{_tid(7)}.jsonl").write_bytes(b'{"timestamp":"20')
    (sessions / f"rollout-2026-09-22T23-51-00-{_tid(8)}.jsonl").write_bytes(os.urandom(4096))
    (sessions / f"rollout-2026-09-22T23-52-00-{_tid(9)}.jsonl").write_bytes(b"")
    # Valid meta, garbage body with a token_count whose rate_limits is not a dict.
    bad = _rollout(codex_home, _tid(6), [])
    with open(bad, "ab") as fh:
        fh.write(line(rec("event_msg", {"type": "token_count", "rate_limits": "full"},
                          ts(50))).encode() + b"\n")
    view = CodexLimitsSource().fetch_once()
    assert view.available is True
    assert view.five_hour_pct == 12.0 and view.as_of == _dt(ts(1))


def test_fetch_once_never_raises_when_listing_fails(codex_home, monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(cpaths, "list_sessions", boom)
    view = CodexLimitsSource().fetch_once()
    assert (view.available, view.error) == (False, NO_DATA)


# -- lifecycle ---------------------------------------------------------------------------
def test_interval_floor_and_default():
    assert CodexLimitsSource().interval == 30
    assert CodexLimitsSource(interval=0).interval == MIN_INTERVAL
    assert CodexLimitsSource(interval=-5).interval == MIN_INTERVAL
    assert CodexLimitsSource(interval=45).interval == 45


def test_start_stop_daemon_thread_exits_promptly(codex_home):
    _rollout(codex_home, ROOT, [token_count(usage(1), rate_limits=BUSINESS, at=ts(1))])
    src = CodexLimitsSource(interval=3600)
    src.start()
    thread = src._thread
    assert thread is not None and thread.daemon and thread.name == "codex-limits"
    src.start()  # idempotent
    assert src._thread is thread
    deadline = time.monotonic() + 5
    while src.view.error == "not polled yet" and time.monotonic() < deadline:
        time.sleep(0.01)
    assert src.view.available is True and src.view.plan_type == "business"
    begin = time.monotonic()
    src.stop()  # interrupts the hour-long wait
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert time.monotonic() - begin < 2


def test_run_waits_interval_between_fetches(codex_home):
    """Deterministic: one fetch per wait, every wait uses the interval."""

    class FakeStop:
        def __init__(self):
            self.waits: list[float] = []

        def is_set(self) -> bool:
            return len(self.waits) >= 3

        def wait(self, timeout: float) -> bool:
            self.waits.append(timeout)
            return self.is_set()

    src = CodexLimitsSource(interval=12.5)
    calls = []
    real_fetch = src.fetch_once
    src.fetch_once = lambda: calls.append(1) or real_fetch()  # type: ignore[method-assign]
    src._stop = FakeStop()  # type: ignore[assignment]
    src._run()
    assert src._stop.waits == [12.5, 12.5, 12.5]
    assert len(calls) == 3


def test_no_busy_loop_in_real_time(codex_home, monkeypatch):
    calls = []
    real = CodexLimitsSource.fetch_once

    def counting(self):
        calls.append(time.monotonic())
        return real(self)

    monkeypatch.setattr(CodexLimitsSource, "fetch_once", counting)
    src = CodexLimitsSource(interval=0)  # clamped to MIN_INTERVAL
    src.start()
    time.sleep(0.3)
    src.stop()
    src._thread.join(timeout=2)
    assert not src._thread.is_alive()
    assert len(calls) == 1


def test_view_is_thread_safe_snapshot(codex_home):
    _rollout(codex_home, ROOT, [token_count(usage(1), rate_limits=_limits(5.0), at=ts(1))])
    src = CodexLimitsSource()
    views: list[PlanLimitsView] = []
    threads = [threading.Thread(target=lambda: views.append(src.fetch_once())) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
    assert len(views) == 4 and all(v.five_hour_pct == 5.0 for v in views)
    assert src.view.five_hour_pct == 5.0


def test_module_is_read_only():
    """Files are only opened "rb"; no network modules; auth.json never named in code."""
    tree = ast.parse(Path(codex_limits.__file__).read_text(encoding="utf-8"))
    modes = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "open":
            assert len(node.args) >= 2 and isinstance(node.args[1], ast.Constant)
            modes.append(node.args[1].value)
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [a.name for a in node.names] + [getattr(node, "module", None) or ""]
            assert not any(n.split(".")[0] in {"urllib", "socket", "http", "requests"}
                           for n in names)
        if isinstance(node, ast.Attribute):
            assert node.attr not in {"write", "write_bytes", "write_text", "truncate", "unlink"}
    assert modes and set(modes) <= {"rb", "r"}
    doc = ast.get_docstring(tree, clean=False)
    strings = [n.value for n in ast.walk(tree)
               if isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value != doc]
    assert not any("auth.json" in s for s in strings)
