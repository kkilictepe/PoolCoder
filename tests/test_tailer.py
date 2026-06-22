"""Tailer behaviour: append, partial lines, UTF-8 splits, rotation, bounds."""

from __future__ import annotations

from pool_coder.tailer import RESET, Tailer


def _append(path, data: bytes) -> None:
    with open(path, "ab") as fh:
        fh.write(data)


def test_basic_append(tmp_path):
    f = tmp_path / "s.jsonl"
    f.write_bytes(b'{"a":1}\n{"a":2}\n')
    t = Tailer(f)
    assert t.poll() == ['{"a":1}', '{"a":2}']
    assert t.poll() == []  # fast path: nothing new


def test_partial_trailing_line_excluded_until_newline(tmp_path):
    f = tmp_path / "s.jsonl"
    f.write_bytes(b'{"complete":1}\n{"partial":')
    t = Tailer(f)
    assert t.poll() == ['{"complete":1}']  # half-written line not surfaced
    _append(f, b'2}\n')
    assert t.poll() == ['{"partial":2}']


def test_utf8_multibyte_split_across_reads(tmp_path):
    f = tmp_path / "s.jsonl"
    # "é" == b"\xc3\xa9"; split the two bytes across two writes, no newline yet.
    f.write_bytes(b'{"v":"\xc3')
    t = Tailer(f)
    assert t.poll() == []  # incomplete byte sequence held in partial
    _append(f, b'\xa9"}\n')
    assert t.poll() == ['{"v":"é"}']  # decodes cleanly, no replacement char


def test_truncation_emits_reset_then_replays(tmp_path):
    f = tmp_path / "s.jsonl"
    f.write_bytes(b'{"old":1}\n{"old":2}\n')
    t = Tailer(f)
    assert t.poll() == ['{"old":1}', '{"old":2}']
    # Rewrite smaller -> size shrinks below offset -> rotation.
    f.write_bytes(b'{"new":1}\n')
    out = t.poll()
    assert out[0] is RESET
    assert out[1:] == ['{"new":1}']


def test_bounded_read_consumes_backlog_over_polls(tmp_path):
    f = tmp_path / "s.jsonl"
    lines = [f'{{"i":{i}}}'.encode() for i in range(20)]
    f.write_bytes(b"\n".join(lines) + b"\n")
    t = Tailer(f, max_read=8)  # tiny cap forces multi-poll catch-up
    collected: list[str] = []
    for _ in range(200):
        out = t.poll()
        collected.extend(x for x in out if x is not RESET)
        if not t.has_backlog and not out:
            break
    assert collected == [ln.decode() for ln in lines]


def test_missing_file_is_safe(tmp_path):
    t = Tailer(tmp_path / "does-not-exist.jsonl")
    assert t.poll() == []
