"""Turn Codex rollout lines into ``CodexRecord`` views and describe tool calls.

A rollout line is ``{"timestamp", "ordinal"?, "type", "payload"}``.
``parse_codex_line`` never raises: blank, invalid or non-object lines become
``None`` and are skipped by the caller. The helpers below are pure and just as
defensive; the Codex fold uses them to label tool calls and results:

* ``usage_from`` maps a Codex ``usage`` block onto ``UsageTokens``. Codex
  counts cached and cache-write tokens *inside* ``input_tokens``; Anthropic
  (and ``UsageTokens``) keeps them separate, so they are split out.
* ``exec_inner_calls`` lists the ``tools.NAME(...)`` calls of a code-mode
  ``exec`` script (JavaScript). A small tokenizer skips string literals and
  comments, so a patch that edits code calling ``tools.foo(`` is not a call.
* ``describe_call`` labels ``function_call`` tools (legacy + collaboration).
* ``output_failed`` reads a tool output's header for a failure marker.
* ``clean_codex_prompt`` keeps the user's own words of a prompt.
"""

from __future__ import annotations

import json
import re
from datetime import datetime

from ..aggregator import clean_prompt
from ..models import UsageTokens
from ..parser import parse_timestamp

LABEL_MAX = 200  # labels are one line, at most this many characters


# ---------------------------------------------------------------------------
# records
# ---------------------------------------------------------------------------
class CodexRecord:
    """A lazy, defensive view over one decoded rollout line."""

    __slots__ = ("raw", "type", "timestamp", "payload", "ptype")

    raw: dict
    type: str
    timestamp: datetime | None
    payload: dict
    ptype: str

    def __init__(self, raw: dict):
        self.raw = raw
        rtype = raw.get("type")
        self.type = rtype if isinstance(rtype, str) else ""
        self.timestamp = parse_timestamp(raw.get("timestamp"))
        payload = raw.get("payload")
        self.payload = payload if isinstance(payload, dict) else {}
        ptype = self.payload.get("type")
        self.ptype = ptype if isinstance(ptype, str) else ""

    @property
    def item(self) -> dict:
        """``payload.item`` of an ``event_msg.item_completed`` ({} otherwise)."""
        item = self.payload.get("item")
        return item if isinstance(item, dict) else {}

    @property
    def item_type(self) -> str:
        itype = self.item.get("type")
        return itype if isinstance(itype, str) else ""

    def __repr__(self) -> str:
        return f"CodexRecord({self.type}/{self.ptype or '-'} @ {self.timestamp})"


def parse_codex_line(line: str) -> CodexRecord | None:
    """Parse one rollout line (``None`` if blank, invalid or not an object)."""
    if isinstance(line, (bytes, bytearray)):
        line = bytes(line).decode("utf-8", "replace")
    if not isinstance(line, str):
        return None
    line = line.strip()
    if not line:
        return None
    try:
        raw = json.loads(line)
    except (json.JSONDecodeError, ValueError, RecursionError):
        return None
    if not isinstance(raw, dict):
        return None
    return CodexRecord(raw)


# ---------------------------------------------------------------------------
# tokens
# ---------------------------------------------------------------------------
def _count(value: object) -> int:
    """A non-negative token count; missing/None/garbage -> 0."""
    try:
        n = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return 0
    return max(n, 0)


def usage_from(u: object) -> UsageTokens | None:
    """Codex ``usage`` -> ``UsageTokens`` (``None`` unless ``u`` is a dict).

    ``input`` is the non-cached part, so ``context_tokens`` equals Codex's
    ``input_tokens`` (the prompt size); ``reasoning`` is display-only (it is
    already inside ``output``).
    """
    if not isinstance(u, dict):
        return None
    total_in = _count(u.get("input_tokens"))
    cached = _count(u.get("cached_input_tokens"))
    cache_write = _count(u.get("cache_write_input_tokens"))
    return UsageTokens(
        input=max(total_in - cached - cache_write, 0),
        cache_creation=cache_write,
        cache_read=cached,
        output=_count(u.get("output_tokens")),
        reasoning=_count(u.get("reasoning_output_tokens")),
    )


# ---------------------------------------------------------------------------
# labels
# ---------------------------------------------------------------------------
def _clip(text: str, limit: int = LABEL_MAX) -> str:
    """One line, whitespace collapsed, capped."""
    return " ".join(text.split())[:limit]


def _text(value: object) -> str:
    return _clip(value) if isinstance(value, str) else ""


_POSIX_SHELLS = {"bash", "sh", "zsh", "dash", "ksh", "fish"}
_WIN_SHELLS = {"pwsh", "powershell", "cmd"}
# flags after which the rest of a Windows shell's argv is the command itself
_WIN_COMMAND_FLAGS = {"-command", "-c", "-commandwithargs", "-cwa", "/c", "/k"}


def _exe_name(arg: str) -> str:
    """``C:\\...\\pwsh.exe`` -> ``pwsh`` (lower-cased, path and .exe dropped)."""
    name = re.split(r"[\\/]", arg.strip().strip("\"'"))[-1].lower()
    return name[:-4] if name.endswith(".exe") else name


def shell_label(cmd: object) -> str:
    """Readable one-line label of a shell command (argv list or string).

    ``bash -lc X`` / ``sh -c X`` / ``zsh -lc X`` -> ``X``; ``pwsh``,
    ``powershell`` and ``cmd`` (any path, ``.exe`` optional) -> what follows
    ``-Command`` / ``/c`` (else the last argument); anything else is joined
    with spaces. A string is used as is.
    """
    if isinstance(cmd, str):
        return _clip(cmd)
    if not isinstance(cmd, (list, tuple)):
        return ""
    argv = [a if isinstance(a, str) else str(a) for a in cmd
            if isinstance(a, (str, int, float)) and not isinstance(a, bool)]
    if not argv:
        return ""
    exe = _exe_name(argv[0])
    if exe in _POSIX_SHELLS:
        for k in range(1, len(argv) - 1):
            flag = argv[k]
            # -c, -lc, -ic, ... (a short-flag cluster containing "c")
            if flag.startswith("-") and not flag.startswith("--") and "c" in flag[1:]:
                return _clip(argv[k + 1])
    elif exe in _WIN_SHELLS and len(argv) > 1:
        for k in range(1, len(argv) - 1):
            if argv[k].lower() in _WIN_COMMAND_FLAGS:
                return _clip(" ".join(argv[k + 1:]))
        return _clip(argv[-1])
    return _clip(" ".join(argv))


# ---------------------------------------------------------------------------
# patches
# ---------------------------------------------------------------------------
_PATCH_HEADER = re.compile(r"\*\*\* (?:(?:Add|Update|Delete) File|Move to): ")
_RAW_LINE = re.compile(r"[^\r\n]*")
_JS_CLOSERS = frozenset(",;)+]}")


def _raw_patch_path(text: str, i: int) -> str:
    path = _RAW_LINE.match(text, i).group().strip()
    # a template literal keeps real newlines but still escapes backslashes
    # (C:\\Git\\a.py); a real path only has "\\" at the start (UNC)
    if "\\\\" in path[1:]:
        path = path.replace("\\\\", "\\")
    return path


def _escaped_patch_path(text: str, i: int) -> str:
    """Read a path from JS/JSON-escaped patch text (``\\n`` ends it)."""
    buf: list[str] = []
    n = len(text)
    while i < n:
        ch = text[i]
        if ch in "\"`\r\n":
            break
        if ch == "'":
            # the closing quote of a single-quoted string ends the path;
            # an apostrophe inside a double-quoted one does not
            j = i + 1
            while j < n and text[j] in " \t":
                j += 1
            if j >= n or text[j] in _JS_CLOSERS or text[j] in "\r\n":
                break
        if ch == "\\":
            nxt = text[i + 1:i + 2]
            if nxt in ("", "n", "r", "t"):
                break
            if nxt == "u":
                m = re.match(r"u\{([0-9a-fA-F]{1,6})\}|u([0-9a-fA-F]{4})", text[i + 1:i + 10])
                if m:
                    buf.append(_chr(m.group(1) or m.group(2)))
                    i += 1 + m.end()
                    continue
            buf.append(nxt)
            i += 2
            continue
        buf.append(ch)
        i += 1
    return _fix_surrogates("".join(buf)).strip()


def _escaped(text: str, i: int) -> bool:
    """Is ``text[i]`` escaped (preceded by an odd run of backslashes)?"""
    run = 0
    while i - run - 1 >= 0 and text[i - run - 1] == "\\":
        run += 1
    return run % 2 == 1


def patch_files(patch: str) -> list[str]:
    """Paths a patch adds, updates, deletes or moves to, in order, deduped.

    Works on raw patch text and on the JS/JSON-escaped text of an ``exec``
    script (``\\n`` for newlines, ``C:\\\\Git\\\\a.py`` for paths). Only real
    headers count: in raw text those start a line, in escaped text they follow
    an escaped newline, so patch text quoted inside a patched file is skipped.
    """
    if not isinstance(patch, str) or "*** " not in patch:
        return []
    heads = list(_PATCH_HEADER.finditer(patch))
    raw = [m for m in heads if m.start() == 0 or patch[m.start() - 1] in "\r\n"]
    if raw:
        paths = [_raw_patch_path(patch, m.end()) for m in raw]
    else:
        esc = [m for m in heads
               if m.start() >= 2 and patch[m.start() - 1] == "n" and _escaped(patch, m.start() - 1)]
        if not esc:
            # one header per string, e.g. ["*** Begin Patch", "*** Update File: x"].join("\n")
            esc = [m for m in heads
                   if m.start() >= 1 and patch[m.start() - 1] in "\"'`"
                   and not _escaped(patch, m.start() - 1)]
        paths = [_escaped_patch_path(patch, m.end()) for m in esc]
    out: list[str] = []
    for path in paths:
        if path and path not in out:
            out.append(path)
    return out


# ---------------------------------------------------------------------------
# JavaScript (code-mode ``exec``) tokenizer
# ---------------------------------------------------------------------------
# A token is (kind, text, pos). kind: "id", "str" (text = raw source between
# the quotes; template literals included, ``${...}`` kept verbatim), "num",
# "re" (regex literal) or "p" (one punctuation character). Tokens inside a
# template's ``${...}`` follow the template's own token.
_WS = re.compile(r"\s+")
_IDENT = re.compile(r"(?:[^\W\d]|\$)[\w$]*")
_NUMBER = re.compile(r"\d[\w.]*")
_DQ = re.compile(r'([^"\\\n]*(?:\\[\s\S][^"\\\n]*)*)"?')
_SQ = re.compile(r"([^'\\\n]*(?:\\[\s\S][^'\\\n]*)*)'?")
_TPL_STOP = re.compile(r"[`\\]|\$\{")
_REGEX_AFTER_PUNCT = frozenset("(,=:[!&|?{;+-*%<>~^")
_REGEX_AFTER_WORDS = frozenset({"return", "typeof", "case", "do", "else", "in", "of",
                                "new", "delete", "void", "throw", "instanceof",
                                "yield", "await"})


def _regex_allowed(toks: list) -> bool:
    """Would a ``/`` here start a regex literal (vs. a division)?"""
    if not toks:
        return True
    kind, text, _ = toks[-1]
    if kind == "p":
        return text in _REGEX_AFTER_PUNCT
    return kind == "id" and text in _REGEX_AFTER_WORDS


def _regex_end(js: str, i: int) -> int:
    """End of the regex literal starting at ``js[i] == "/"`` (0 if none)."""
    n = len(js)
    j = i + 1
    in_class = False
    while j < n:
        c = js[j]
        if c == "\\":
            j += 2
            continue
        if c == "\n":
            return 0
        if in_class:
            if c == "]":
                in_class = False
        elif c == "[":
            in_class = True
        elif c == "/":
            j += 1
            while j < n and (js[j].isalnum() or js[j] in "_$"):
                j += 1  # flags
            return j
        j += 1
    return 0


def _scan_template(js: str, i: int, toks: list) -> int:
    """Tokenize the template literal at ``js[i] == "`"``; return its end."""
    slot = len(toks)
    toks.append(("str", "", i))
    n = len(js)
    j = i + 1
    close = n
    while j < n:
        m = _TPL_STOP.search(js, j)
        if m is None:
            j = n
            break
        stop = m.group()
        if stop == "`":
            close = m.start()
            j = m.end()
            break
        if stop == "\\":
            j = m.end() + 1
            continue
        j = _scan(js, m.end(), toks, nested=True)  # "${" ... "}"
    toks[slot] = ("str", js[i + 1:close], i)
    return j


def _scan(js: str, i: int, toks: list, nested: bool = False) -> int:
    """Append the tokens of ``js[i:]``; a nested (``${``) scan stops after
    its closing brace and returns the index after it."""
    n = len(js)
    braces = 0
    while i < n:
        ch = js[i]
        if ch.isspace():
            i = _WS.match(js, i).end()
            continue
        if ch == "/":
            nxt = js[i + 1:i + 2]
            if nxt == "/":
                j = js.find("\n", i)
                i = n if j < 0 else j
                continue
            if nxt == "*":
                j = js.find("*/", i + 2)
                i = n if j < 0 else j + 2
                continue
            if _regex_allowed(toks):
                j = _regex_end(js, i)
                if j:
                    toks.append(("re", js[i:j], i))
                    i = j
                    continue
            toks.append(("p", "/", i))
            i += 1
            continue
        if ch == '"' or ch == "'":
            m = (_DQ if ch == '"' else _SQ).match(js, i + 1)
            toks.append(("str", m.group(1), i))
            i = m.end()
            continue
        if ch == "`":
            i = _scan_template(js, i, toks)
            continue
        m = _IDENT.match(js, i)
        if m:
            toks.append(("id", m.group(), i))
            i = m.end()
            continue
        m = _NUMBER.match(js, i)
        if m:
            toks.append(("num", m.group(), i))
            i = m.end()
            continue
        if ch == "{":
            braces += 1
        elif ch == "}":
            if nested and braces == 0:
                return i + 1
            braces -= 1
        toks.append(("p", ch, i))
        i += 1
    return n


def _tokenize(js: str) -> list:
    toks: list = []
    _scan(js, 0, toks)
    return toks


_JS_ESCAPE = re.compile(r"\\(u\{[0-9a-fA-F]{1,6}\}|u[0-9a-fA-F]{4}|x[0-9a-fA-F]{2}|\r\n|[\s\S])")
_SIMPLE_ESCAPES = {"n": "\n", "r": "\r", "t": "\t", "b": "\b", "f": "\f", "v": "\v", "0": "\0"}
_SURROGATE = re.compile(r"[\ud800-\udfff]")


def _chr(code: str) -> str:
    try:
        return chr(int(code, 16))
    except (ValueError, OverflowError):
        return ""


def _fix_surrogates(text: str) -> str:
    """Join ``\\uD83D\\uDE00``-style pairs; drop lone halves (unprintable)."""
    if not _SURROGATE.search(text):
        return text
    return text.encode("utf-16", "surrogatepass").decode("utf-16", "replace")


def _unescape_match(m: re.Match) -> str:
    esc = m.group(1)
    if len(esc) > 1 and esc[0] == "u":
        return _chr(esc[2:-1] if esc[1] == "{" else esc[1:])
    if len(esc) == 3 and esc[0] == "x":
        return _chr(esc[1:])
    if esc in ("\n", "\r", "\r\n", "\u2028", "\u2029"):
        return ""  # line continuation
    return _SIMPLE_ESCAPES.get(esc, esc)


def _js_unescape(raw: str) -> str:
    """Cook the source text of a JS string literal."""
    if "\\" not in raw:
        return raw
    return _fix_surrogates(_JS_ESCAPE.sub(_unescape_match, raw))


def _is_p(tok: tuple, char: str) -> bool:
    return tok[0] == "p" and tok[1] == char


def _key_name(tok: tuple) -> str | None:
    if tok[0] == "id":
        return tok[1]
    if tok[0] == "str" and len(tok[1]) <= 64:  # keys are short; skip big values
        return _js_unescape(tok[1])
    return None


def _binding(toks: list, name: str, before: int) -> int | None:
    """Index of the value last assigned to ``name`` (``name = <value>``)
    before token ``before``; ``==``, ``=>`` and ``x.name =`` are skipped."""
    for k in range(min(before, len(toks) - 2) - 1, -1, -1):
        tok = toks[k]
        if tok[0] != "id" or tok[1] != name or not _is_p(toks[k + 1], "="):
            continue
        after = toks[k + 2]
        if after[0] == "p" and after[1] in "=>":
            continue
        if k > 0 and _is_p(toks[k - 1], "."):
            continue
        return k + 2
    return None


def _value_at(toks: list, k: int, before: int, depth: int = 0) -> str | list[str] | None:
    """The literal value starting at token ``k``: a string (``+``-joined
    string literals), a list of string literals, or a variable bound to one."""
    if k >= len(toks):
        return None
    kind, text, _ = toks[k]
    if kind == "str":
        parts = [_js_unescape(text)]
        j = k + 1
        while j + 1 < len(toks) and _is_p(toks[j], "+") and toks[j + 1][0] == "str":
            parts.append(_js_unescape(toks[j + 1][1]))
            j += 2
        return "".join(parts)
    if kind == "p" and text == "[":
        items: list[str] = []
        for j in range(k + 1, len(toks)):
            tok = toks[j]
            if tok[0] == "str":
                items.append(_js_unescape(tok[1]))
            elif _is_p(tok, "]"):
                return items
            elif not _is_p(tok, ","):
                return None  # not a plain list of string literals
        return None
    if kind == "id" and depth < 3:
        at = _binding(toks, text, before)
        if at is not None:
            return _value_at(toks, at, before, depth + 1)
    return None


def _call_arg(toks: list, lo: int, hi: int, keys: tuple[str, ...],
              call: int) -> str | list[str] | None:
    """Value of the first ``keys`` property in the call's arguments
    ``toks[lo:hi]`` (quoted, unquoted or shorthand key), or of a positional
    literal first argument."""
    if lo >= hi:
        return None
    if not _is_p(toks[lo], "{"):
        return _value_at(toks, lo, call)
    for key in keys:
        for k in range(lo, hi - 1):
            if _key_name(toks[k]) != key:
                continue
            prev = toks[k - 1]
            if not (_is_p(prev, "{") or _is_p(prev, ",")):
                continue  # not in key position (e.g. a ternary)
            nxt = toks[k + 1]
            if _is_p(nxt, ":"):
                value = _value_at(toks, k + 2, call)
            elif toks[k][0] == "id" and (_is_p(nxt, ",") or _is_p(nxt, "}")):
                value = _value_at(toks, k, call)  # shorthand {cmd}
            else:
                continue
            if value is not None:
                return value
    return None


def _inner_label(name: str, toks: list, lo: int, hi: int, call: int,
                 js: str) -> tuple[str, str]:
    if name == "exec_command":
        return "shell", shell_label(_call_arg(toks, lo, hi, ("cmd", "command"), call))
    if name == "apply_patch":
        value = _call_arg(toks, lo, hi, ("input", "patch"), call)
        files = patch_files(value) if isinstance(value, str) else []
        if not files:
            files = patch_files(js)  # patch built some other way
        return "apply_patch", files[0] if files else ""
    if name == "web__run":
        value = _call_arg(toks, lo, hi, ("q", "search_query", "query", "url"), call)
        if isinstance(value, list):
            value = value[0] if value else None  # search_query: ["a", "b"]
        if not isinstance(value, str):
            ref = _call_arg(toks, lo, hi, ("ref_id",), call)
            # open:[{ref_id:"https://..."}]; "turn0search2"-style ids say nothing
            value = ref if isinstance(ref, str) and ref.startswith(("http://", "https://")) else ""
        return "web_search", _clip(value)
    if name == "view_image":
        value = _call_arg(toks, lo, hi, ("path",), call)
        return "view_image", value.strip() if isinstance(value, str) else ""
    return name, ""


def exec_inner_calls(js: str) -> list[tuple[str, str]]:
    """``(display_name, label)`` of each ``tools.NAME(...)`` call in a
    code-mode ``exec`` script, in source order.

    Only calls in code count, never text inside string literals or comments.
    ``exec_command`` -> ``("shell", command)``, ``apply_patch`` ->
    ``("apply_patch", first path)``, ``web__run`` -> ``("web_search", query
    or url)``, ``view_image`` -> ``("view_image", path)``, anything else ->
    ``(NAME, "")``.
    """
    if not isinstance(js, str) or "tools" not in js:
        return []
    out: list[tuple[str, str]] = []
    try:
        toks = _tokenize(js)
        n = len(toks)
        for k in range(n - 3):
            tok = toks[k]
            if tok[0] != "id" or tok[1] != "tools":
                continue
            if not (_is_p(toks[k + 1], ".") and toks[k + 2][0] == "id" and _is_p(toks[k + 3], "(")):
                continue
            lo = k + 4
            hi, depth = n, 1
            for j in range(lo, n):
                t = toks[j]
                if t[0] != "p":
                    continue
                if t[1] in "([{":
                    depth += 1
                elif t[1] in ")]}":
                    depth -= 1
                    if depth == 0:
                        hi = j
                        break
            out.append(_inner_label(toks[k + 2][1], toks, lo, hi, k, js))
    except (RecursionError, IndexError, TypeError, ValueError):
        pass  # pathological input (e.g. templates nested 1000 deep): keep what was found
    return out


# ---------------------------------------------------------------------------
# function calls
# ---------------------------------------------------------------------------
_SHELL_CALLS = {"shell", "shell_command", "container.exec", "local_shell"}
_TARGET_CALLS = {"send_message", "followup_task", "interrupt_agent"}


def _args_dict(args: object) -> dict:
    if isinstance(args, dict):
        return args
    if isinstance(args, str):
        try:
            decoded = json.loads(args)
        except (json.JSONDecodeError, ValueError, RecursionError):
            return {}
        if isinstance(decoded, dict):
            return decoded
    return {}


def _first_patch_path(value: object) -> str:
    files = patch_files(value) if isinstance(value, str) else []
    return files[0] if files else ""


def describe_call(name: str, args: object) -> tuple[str, str]:
    """``(display_name, label)`` of a ``function_call`` (``args`` is a dict,
    a JSON string or anything else). Encrypted ``message`` args are never
    shown."""
    if not isinstance(name, str):
        return "", ""
    a = _args_dict(args)
    if name == "apply_patch":
        if not a and isinstance(args, str):
            return "apply_patch", _first_patch_path(args)  # freeform raw patch
        return "apply_patch", _first_patch_path(a.get("input") or a.get("patch"))
    if name in _SHELL_CALLS:
        cmd = a.get("command")
        return "shell", shell_label(cmd if cmd is not None else a.get("cmd"))
    if name == "exec_command":
        return "shell", shell_label(a.get("cmd"))
    if name == "update_plan":
        plan = a.get("plan")
        for step in plan if isinstance(plan, list) else ():
            if isinstance(step, dict) and step.get("status") == "in_progress":
                return "update_plan", _text(step.get("step"))
        return "update_plan", ""
    if name == "view_image":
        path = a.get("path")
        return "view_image", path.strip() if isinstance(path, str) else ""
    if name == "spawn_agent":
        return name, _text(a.get("task_name")) or _text(a.get("agent_type"))
    if name in _TARGET_CALLS:
        return name, _text(a.get("target"))
    if name == "request_user_input":
        questions = a.get("questions")
        first = questions[0] if isinstance(questions, list) and questions else None
        if isinstance(first, dict):
            return name, _text(first.get("header")) or _text(first.get("question"))
        return name, ""
    return name, ""


# ---------------------------------------------------------------------------
# outputs
# ---------------------------------------------------------------------------
_EXIT_MARK = re.compile(r"(?:Exit code:|Process exited with code)\s*(-?\d+)")
# outputs of these shapes report a failure no matter what follows
_FAILED_PREFIXES = ("collab spawn failed", "collab tool failed", "aborted by user")
_HEADER_LINES = 5


def _nonzero(code: object) -> bool:
    return isinstance(code, int) and not isinstance(code, bool) and code != 0


def _json_failed(obj: dict) -> bool:
    """Legacy shell output ``{"output", "metadata": {"exit_code"}}``."""
    meta = obj.get("metadata")
    if isinstance(meta, dict) and _nonzero(meta.get("exit_code")):
        return True
    return _nonzero(obj.get("exit_code"))


def _text_failed(text: str, head: str) -> bool:
    stripped = text.lstrip()
    if stripped.startswith(_FAILED_PREFIXES):
        return True
    if stripped.startswith("{") and '"exit_code"' in stripped:
        try:
            decoded = json.loads(stripped)
        except (json.JSONDecodeError, ValueError, RecursionError):
            decoded = None
        if isinstance(decoded, dict) and _json_failed(decoded):
            return True
    # the header: lines before the first "Output:" line, at most 5 of them;
    # what follows "Output:" is the command's own output and never inspected
    header: list[str] = []
    for line in head.lstrip().split("\n", _HEADER_LINES)[:_HEADER_LINES]:
        line = line.strip()
        if line.startswith("Output:"):
            break
        header.append(line)
    if header and header[0].startswith("Script "):
        # "Script completed" / "Script failed" / "Script running with cell ID N"
        return header[0].startswith("Script failed")
    m = _EXIT_MARK.search("\n".join(header))
    return bool(m) and int(m.group(1)) != 0


def output_failed(output: object) -> bool:
    """Whether a tool output (``*_call_output.output``) reports a failure.

    True for a ``Script failed`` header, an ``Exit code: N`` / ``Process
    exited with code N`` header line with N != 0, a JSON result whose
    ``metadata.exit_code`` is non-zero, and ``collab spawn failed`` /
    ``collab tool failed`` / ``aborted by user`` results. A script still
    running in the background is not a failure.
    """
    if isinstance(output, str):
        return _text_failed(output, output)
    if isinstance(output, list):
        texts = []
        for block in output:
            if isinstance(block, str):
                texts.append(block)
            elif isinstance(block, dict) and isinstance(block.get("text"), str):
                texts.append(block["text"])
        if not texts:
            return False
        # the header lives in the first text block
        return _text_failed("\n".join(texts), texts[0])
    if isinstance(output, dict):
        if _json_failed(output):
            return True
        inner = output.get("output", output.get("content"))
        return isinstance(inner, (str, list)) and output_failed(inner)
    return False


# ---------------------------------------------------------------------------
# prompts
# ---------------------------------------------------------------------------
# IDE extensions wrap the prompt: "# Context from my IDE setup: ... ## My request:"
_MY_REQUEST = re.compile(r"^#{1,3} My request[^\n]*:[ \t]*\r?$", re.MULTILINE)
_AGENTS_MD = re.compile(r"^# AGENTS\.md instructions for [^\n]*\n\s*(?=<INSTRUCTIONS>)", re.MULTILINE)
_CONTEXT_BLOCK = re.compile(
    r"<(environment_context|user_instructions|INSTRUCTIONS|turn_aborted|recommended_plugins)\b[^>]*>"
    r".*?</\1\s*>",
    re.DOTALL,
)


def clean_codex_prompt(text: str | None, limit: int = 200) -> str | None:
    """The user's own words of a Codex prompt (``None`` if nothing is left).

    Keeps the text after the last ``## My request…:`` line (IDE preamble),
    drops injected context blocks, then applies the shared ``clean_prompt``.
    """
    if not isinstance(text, str) or not text:
        return None
    requests = list(_MY_REQUEST.finditer(text))
    if requests:
        text = text[requests[-1].end():]  # the last one is the real request
    text = _AGENTS_MD.sub("", text)
    text = _CONTEXT_BLOCK.sub(" ", text)
    return clean_prompt(text, limit)
