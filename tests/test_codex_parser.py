"""Codex rollout parsing: records, usage mapping, tool-call labels, outputs, prompts.

The ``exec`` inputs below are real code-mode scripts copied from Codex 0.155
rollouts (long commands and patches trimmed, structure kept; personal data
replaced). They are Python literals of the JavaScript source, so ``\\\\`` in
the literal is ``\\`` in the JS text, which decodes to one backslash.
"""

from __future__ import annotations

import json
import re

import pytest

from codex_records import (
    compacted,
    line,
    token_usage,
    usage,
    user_message_event,
    user_message_item,
)
from pool_coder.codex.parser import (
    CodexRecord,
    clean_codex_prompt,
    describe_call,
    exec_inner_calls,
    output_failed,
    parse_codex_line,
    patch_files,
    shell_label,
    usage_from,
)
from pool_coder.models import UsageTokens

NAIVE_CALL = re.compile(r"tools\.(\w+)\(")


# ---------------------------------------------------------------------------
# parse_codex_line / CodexRecord
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("text", ["", "   \n", "{not json", "[1, 2]", '"a string"', "42", "null"])
def test_parse_codex_line_rejects_blank_invalid_and_non_objects(text):
    assert parse_codex_line(text) is None


def test_parse_codex_line_rejects_non_text():
    assert parse_codex_line(None) is None  # type: ignore[arg-type]
    assert parse_codex_line(12) is None  # type: ignore[arg-type]


def test_parse_codex_line_valid_record():
    rec = parse_codex_line(line(user_message_item("hello", client_id="c9")) + "\n")
    assert isinstance(rec, CodexRecord)
    assert rec.type == "event_msg"
    assert rec.ptype == "item_completed"
    assert rec.timestamp is not None and rec.timestamp.tzinfo is not None
    assert rec.timestamp.isoformat() == "2026-09-22T20:47:52+00:00"
    assert rec.item_type == "UserMessage"
    assert rec.item["client_id"] == "c9"
    assert rec.payload["thread_id"]


def test_parse_codex_line_accepts_bytes():
    rec = parse_codex_line(line(compacted()).encode("utf-8"))  # type: ignore[arg-type]
    assert rec is not None and rec.type == "compacted"


def test_record_properties_are_defensive():
    rec = parse_codex_line('{"type": "compacted", "payload": [1, 2]}')
    assert rec is not None
    assert rec.payload == {} and rec.ptype == "" and rec.item == {} and rec.item_type == ""
    assert rec.timestamp is None

    rec = parse_codex_line('{"type": 7, "timestamp": "garbage", "payload": {"type": 3, "item": "x"}}')
    assert rec.type == "" and rec.ptype == "" and rec.timestamp is None
    assert rec.item == {} and rec.item_type == ""

    rec = parse_codex_line('{"payload": {"type": "item_completed", "item": {"type": ["list"]}}}')
    assert rec.type == "" and rec.ptype == "item_completed"
    assert rec.item == {"type": ["list"]} and rec.item_type == ""


def test_record_without_item_and_event_format_prompt():
    rec = parse_codex_line(line(user_message_event("hi")))
    assert rec.ptype == "user_message" and rec.item == {} and rec.item_type == ""
    assert rec.payload["message"] == "hi"


# ---------------------------------------------------------------------------
# usage_from
# ---------------------------------------------------------------------------
def test_usage_from_real_record_splits_cache_out_of_input():
    # numbers from a real token_usage_record
    u = usage(inp=14664, cached=6912, out=187, rs=89)
    tok = usage_from(u)
    assert tok == UsageTokens(input=14664 - 6912, cache_creation=0, cache_read=6912,
                              output=187, reasoning=89)
    assert tok.context_tokens == 14664 == u["input_tokens"]
    # reasoning is inside output: shown, never billed twice
    assert tok.billable_total == 14664 + 187
    assert tok.cache_hit_ratio == pytest.approx(6912 / 14664)


def test_usage_from_cache_write_is_part_of_input():
    tok = usage_from(usage(inp=1000, cached=600, cw=100, out=50, rs=20))
    assert (tok.input, tok.cache_read, tok.cache_creation) == (300, 600, 100)
    assert tok.context_tokens == 1000
    assert (tok.output, tok.reasoning) == (50, 20)


def test_usage_from_record_builder_payload():
    rec = parse_codex_line(line(token_usage("resp_1", inp=500, cached=400, out=10, rs=4)))
    tok = usage_from(rec.payload["usage"])
    assert tok.context_tokens == 500 and tok.input == 100 and tok.reasoning == 4


@pytest.mark.parametrize("u", [
    {},
    {"input_tokens": None, "cached_input_tokens": None, "cache_write_input_tokens": None,
     "output_tokens": None, "reasoning_output_tokens": None},
    {"input_tokens": "abc", "output_tokens": [1]},
])
def test_usage_from_missing_or_bad_fields_are_zero(u):
    assert usage_from(u) == UsageTokens()


def test_usage_from_older_record_without_cache_write():
    tok = usage_from({"input_tokens": 90, "cached_input_tokens": 40, "output_tokens": 7})
    assert (tok.input, tok.cache_read, tok.cache_creation, tok.output, tok.reasoning) == (50, 40, 0, 7, 0)


def test_usage_from_clamps_input_at_zero():
    tok = usage_from({"input_tokens": 100, "cached_input_tokens": 90, "cache_write_input_tokens": 30})
    assert tok.input == 0
    assert (tok.cache_read, tok.cache_creation) == (90, 30)


def test_usage_from_numeric_strings_and_negatives():
    tok = usage_from({"input_tokens": "12", "output_tokens": -5})
    assert tok.input == 12 and tok.output == 0


@pytest.mark.parametrize("u", [None, [], "usage", 5, 1.5])
def test_usage_from_non_dict_is_none(u):
    assert usage_from(u) is None


# ---------------------------------------------------------------------------
# exec_inner_calls — real code-mode scripts
# ---------------------------------------------------------------------------
REAL_EXEC_JSON_ARGS = 'const r = await tools.exec_command({"cmd":"py -0p","workdir":"C:\\\\Git\\\\Axon\\\\PMO2","yield_time_ms":10000,"max_output_tokens":5000});\ntext(r.output);\n'

REAL_EXEC_POWERSHELL_QUOTES = 'const r = await tools.exec_command({"cmd":"$p=\'tests/agents/test_slice11_goldens.py\'; $lines=Get-Content $p; for($i=1;$i -le $lines.Length;$i++){ if($i -ge 1 -and $i -le 900){ \'{0,4}: {1}\' -f $i,$lines[$i-1] } }","workdir":"C:\\\\Git\\\\Axon\\\\PMO2","yield_time_ms":10000,"max_output_tokens":60000});\ntext(r.output);\n'

REAL_EXEC_ESCAPED_QUOTES = 'const r = await tools.exec_command({"cmd":"rg -n \\"^###|^##\\" README.md","workdir":"C:\\\\Git\\\\Axon\\\\PMO2","yield_time_ms":10000,"max_output_tokens":10000});\ntext(r.output);\n'

REAL_EXEC_MIXED_QUOTES = 'const r = await tools.exec_command({"cmd":"rg -n \'\\"cue\\"\' tests/agents/golden","workdir":"C:\\\\Git\\\\Axon\\\\PMO2","yield_time_ms":10000,"max_output_tokens":5000}); text(r.output);\n'

REAL_EXEC_LEADING_COMMENT = '// @exec: {"yield_time_ms": 10000}\n\nconst r = await tools.exec_command({"cmd":"rg -n \\"EntityRow|SourceRow\\" src/pmo_agent/api/routes/evidence.py | Select-Object -First 100","workdir":"C:\\\\Git\\\\Axon\\\\PMO2","yield_time_ms":10000,"max_output_tokens":12000});\ntext(r.output);\n'

REAL_EXEC_UNQUOTED_KEYS = 'const r=await tools.exec_command({cmd:"uv run pytest -q",workdir:"C:\\\\Git\\\\Axon\\\\PMO2",yield_time_ms:30000,max_output_tokens:50000});text(r.output);if(r.session_id)text(`SESSION_ID=${r.session_id}`)\n'

REAL_EXEC_PROMISE_ALL = 'const results = await Promise.all([\n  tools.exec_command({"cmd":"uv run pytest tests/review/test_deterministic_checks.py -q","workdir":"C:\\\\Git\\\\EnterpriseAI\\\\backend","yield_time_ms":30000,"max_output_tokens":30000}),\n  tools.exec_command({"cmd":"uv run mypy app/review/deterministic_checks.py","workdir":"C:\\\\Git\\\\EnterpriseAI\\\\backend","yield_time_ms":30000,"max_output_tokens":30000})\n]);\nfor (const r of results) { text(JSON.stringify(r)); }\n'

REAL_EXEC_MULTILINE_OBJECTS = 'const outputs = await Promise.all([\n  tools.exec_command({\n    cmd: "Get-Content -Raw src/pmo_agent/agents/resolver.py; Get-Content -Raw src/pmo_agent/agents/validate.py",\n    workdir: "C:\\\\Git\\\\Axon\\\\PMO2",\n    yield_time_ms: 10000,\n    max_output_tokens: 30000\n  }),\n  tools.exec_command({\n    cmd: "Get-Content -Raw src/pmo_agent/orchestration/runner.py",\n    workdir: "C:\\\\Git\\\\Axon\\\\PMO2",\n    yield_time_ms: 10000,\n    max_output_tokens: 30000\n  })\n]);\noutputs.forEach((r) => text(r.output));\n'

REAL_EXEC_TEMPLATE_CMD = 'const cmds = ["onboard","run","freeze","upload","intake","runs","export","serve"];\nconst results = await Promise.all(cmds.map(c => tools.exec_command({\n  cmd: `uv run pmo ${c} --help`,\n  workdir: "C:\\\\Git\\\\Axon\\\\PMO2",\n  yield_time_ms: 30000,\n  max_output_tokens: 12000\n})));\nfor (let i = 0; i < cmds.length; i++) text(`COMMAND ${cmds[i]}\\n${results[i].output}`);\n'

REAL_EXEC_LOOP_VARIABLE = 'const cmds = [\n  "git diff --check",\n  "git diff -- .gitignore"\n];\nconst rs = await Promise.all(cmds.map(cmd=>tools.exec_command({cmd,workdir:"C:\\\\Git\\\\Axon\\\\PMO2",yield_time_ms:10000,max_output_tokens:20000})));\nrs.forEach((r,i)=>text(`--- ${i+1} ---\\n${r.output}`));\n'

# a patch (in a variable) whose Python source calls tools.list_meetings(...)
REAL_PATCH_WITH_TOOLS_TEXT = 'const patch = "*** Begin Patch\\n*** Update File: C:\\\\Git\\\\Axon\\\\PMO2\\\\src\\\\pmo_agent\\\\agents\\\\tools.py\\n@@\\n-        return tools.list_meetings(limit=request.limit)\\n+        return tools.list_meetings(\\n+            query=request.query,\\n+            offset=request.offset,\\n+            limit=request.limit,\\n+        )\\n@@\\n+    with pytest.raises(ValueError, match=\\"query cannot be blank\\"):\\n+        tools.list_meetings(query=\\"   \\")\\n*** End Patch";\ntext(await tools.apply_patch(patch));\n'

REAL_PATCH_WITH_PROPOSE = 'const patch = "*** Begin Patch\\n*** Update File: C:\\\\Git\\\\Axon\\\\PMO2\\\\src\\\\pmo_agent\\\\orchestration\\\\source_mapper.py\\n@@\\n-        result = ProposalResult.model_validate(tools.propose(cast(SourceMappingProposal, value)))\\n+        result = ProposalResult.model_validate(tools.propose(proposal))\\n*** End Patch";\ntext(await tools.apply_patch(patch));\n'

REAL_PATCH_QUOTES_AND_BACKTICKS = 'const patch = "*** Begin Patch\\n*** Update File: C:\\\\Git\\\\EnterpriseAI\\\\backend\\\\tests\\\\review\\\\test_runner.py\\n@@\\n         \'The clause says \\"invented\\".\',\\n+        \\"The clause says \'invented\'.\\",\\n+        \\"The clause says `invented`.\\",\\n*** End Patch";\ntext(await tools.apply_patch(patch));\n'

REAL_PATCH_RELATIVE = 'const patch = "*** Begin Patch\\n*** Update File: .gitignore\\n@@\\n-tmp/\\n*** End Patch";\ntext(await tools.apply_patch(patch));\n'

REAL_PATCH_INLINE_DELETE = 'text(await tools.apply_patch("*** Begin Patch\\n*** Delete File: backend/config/flows/legal-contract-review/system_prompt.md\\n*** End Patch"));\n'

REAL_PATCH_INLINE_FORWARD_SLASHES = 'const p = await tools.apply_patch("*** Begin Patch\\n*** Update File: C:/Git/EnterpriseAI/backend/app/data/models/review.py\\n@@\\n     DateTime,\\n-    ForeignKey,\\n     ForeignKeyConstraint,\\n*** End Patch");\ntext(p);\n'

REAL_PATCH_DELETE_AND_ADD = 'const patch = "*** Begin Patch\\n*** Delete File: C:\\\\Git\\\\EnterpriseAI\\\\backend\\\\config\\\\review_profiles.yaml\\n*** Add File: C:\\\\Git\\\\EnterpriseAI\\\\backend\\\\config\\\\review_profiles\\\\legal-contract-v1\\\\profile.yaml\\n+id: legal-contract-v1\\n+version: \\"1\\"\\n*** Add File: C:\\\\Git\\\\EnterpriseAI\\\\backend\\\\config\\\\review_profiles\\\\legal-contract-v1\\\\taxonomy.yaml\\n+topics:\\n+  - { id: obligations_remedies, label: Obligations and remedies }\\n*** End Patch";\ntext(await tools.apply_patch(patch));\n'

REAL_EMPTY_PATCH_THEN_SHELL = 'const patch = "*** Begin Patch\\n*** End Patch";\nconst a = await tools.apply_patch(patch);\nconst r = await tools.exec_command({"cmd":"git config --local user.email \'dev@example.com\'; git config --local --get-regexp \'^user\\\\.(name|email)$\'","workdir":"C:\\\\Git\\\\EnterpriseAI","yield_time_ms":10000,"max_output_tokens":2000});\ntext(r.output);\n'

REAL_WEB_SEARCH_UNQUOTED = 'const r = await tools.web__run({search_query:[\n  {q:"site:github.com/open-telemetry/opentelemetry-collector-contrib telemetrygen Docker ghcr.io v0.161.0 traces"},\n  {q:"site:opentelemetry.io telemetrygen docker collector test"}\n],response_length:"long"}); \ntext(r);\n'

REAL_WEB_SEARCH_QUOTED = 'const r = await tools.web__run({"search_query":[{"q":"site:developers.openai.com/codex AGENTS.md instructions shell commands Codex"},{"q":"site:developers.openai.com/codex multiple agents shared workspace"}],"response_length":"short"}); text(r)\n'

REAL_WEB_SEARCH_BACKTICK_IN_STRING = 'const r = await tools.web__run({search_query:[\n  {q:"site:github.com/open-telemetry/opentelemetry-collector \\"The `otlp` deprecated alias\\" exporter"},\n  {q:"site:github.com/open-telemetry/opentelemetry-collector exporter/otlpexporter README"}\n],response_length:"long"}); text(r)\n'

REAL_WEB_OPEN_URL = 'const result = await tools.web__run({"open":[{"ref_id":"https://github.com/minio/minio/pull/21550"}],"response_length":"long"}); text(result);\n'

REAL_WEB_OPEN_REF = 'const r = await tools.web__run({open:[{ref_id:"turn0search2"}],response_length:"long"}); text(r)\n'

REAL_WRITE_STDIN = 'const r = await tools.write_stdin({ session_id: 60442, yield_time_ms: 30000, chars: "" });\ntext(r.output);\n'

REAL_WRITE_STDIN_TEMPLATES = 'const r = await tools.write_stdin({session_id: 86881, chars: "", yield_time_ms: 30000, max_output_tokens: 12000});\ntext(r.output);\nif (r.session_id) text(`SESSION_ID=${r.session_id}`);\ntext(`\\nexit=${r.exit_code} wall=${r.wall_time_seconds}s`);'

REAL_VIEW_IMAGE = 'const r = await tools.view_image({path:"C:\\\\Git\\\\EnterpriseAI\\\\.tmp-letterhead-body.bmp", detail:"original"});\nimage(r.image_url);\n'

REAL_VIEW_IMAGE_TWICE = 'const a = await tools.view_image({path:"C:\\\\Git\\\\EnterpriseAI\\\\backend\\\\assets\\\\logos\\\\logo-horizontal-positive.png",detail:"original"});\nimage(a.image_url);\nconst b = await tools.view_image({path:"C:\\\\Git\\\\EnterpriseAI\\\\backend\\\\assets\\\\logos\\\\logo-stacked-positive.png",detail:"original"});\nimage(b.image_url);\n'

REAL_VIEW_IMAGE_TEMPLATE_PATH = 'const pages=[1,2,3]; const rs=await Promise.all(pages.map(n=>tools.view_image({path:`C:\\\\Git\\\\EnterpriseAI\\\\tmp\\\\pdfs\\\\manager\\\\manager-page-${String(n).padStart(2,\'0\')}.png`,detail:"high"}))); rs.forEach((r,i)=>{text(`Page ${pages[i]}`);image(r.image_url);});\n'

REAL_NO_CALLS_REGEX_LITERAL = 'const matches = ALL_TOOLS.filter(x =>\n  /openai|docs|codex/i.test(x.name + " " + x.description)\n);\ntext(matches);\n'

REAL_EXEC_CASES = [
    (REAL_EXEC_JSON_ARGS, [("shell", "py -0p")]),
    (REAL_EXEC_POWERSHELL_QUOTES, [(
        "shell",
        "$p='tests/agents/test_slice11_goldens.py'; $lines=Get-Content $p; "
        "for($i=1;$i -le $lines.Length;$i++){ if($i -ge 1 -and $i -le 900){ "
        "'{0,4}: {1}' -f $i,$lines[$i-1] } }",
    )]),
    (REAL_EXEC_ESCAPED_QUOTES, [("shell", 'rg -n "^###|^##" README.md')]),
    (REAL_EXEC_MIXED_QUOTES, [("shell", "rg -n '\"cue\"' tests/agents/golden")]),
    (REAL_EXEC_LEADING_COMMENT, [(
        "shell",
        'rg -n "EntityRow|SourceRow" src/pmo_agent/api/routes/evidence.py | Select-Object -First 100',
    )]),
    (REAL_EXEC_UNQUOTED_KEYS, [("shell", "uv run pytest -q")]),
    (REAL_EXEC_PROMISE_ALL, [
        ("shell", "uv run pytest tests/review/test_deterministic_checks.py -q"),
        ("shell", "uv run mypy app/review/deterministic_checks.py"),
    ]),
    (REAL_EXEC_MULTILINE_OBJECTS, [
        ("shell", "Get-Content -Raw src/pmo_agent/agents/resolver.py; "
                  "Get-Content -Raw src/pmo_agent/agents/validate.py"),
        ("shell", "Get-Content -Raw src/pmo_agent/orchestration/runner.py"),
    ]),
    (REAL_EXEC_TEMPLATE_CMD, [("shell", "uv run pmo ${c} --help")]),
    # the command is a loop variable: no literal to show, but the call counts
    (REAL_EXEC_LOOP_VARIABLE, [("shell", "")]),
    (REAL_PATCH_WITH_TOOLS_TEXT, [("apply_patch", r"C:\Git\Axon\PMO2\src\pmo_agent\agents\tools.py")]),
    (REAL_PATCH_WITH_PROPOSE, [
        ("apply_patch", r"C:\Git\Axon\PMO2\src\pmo_agent\orchestration\source_mapper.py"),
    ]),
    (REAL_PATCH_QUOTES_AND_BACKTICKS, [
        ("apply_patch", r"C:\Git\EnterpriseAI\backend\tests\review\test_runner.py"),
    ]),
    (REAL_PATCH_RELATIVE, [("apply_patch", ".gitignore")]),
    (REAL_PATCH_INLINE_DELETE, [("apply_patch", "backend/config/flows/legal-contract-review/system_prompt.md")]),
    (REAL_PATCH_INLINE_FORWARD_SLASHES, [("apply_patch", "C:/Git/EnterpriseAI/backend/app/data/models/review.py")]),
    (REAL_PATCH_DELETE_AND_ADD, [("apply_patch", r"C:\Git\EnterpriseAI\backend\config\review_profiles.yaml")]),
    (REAL_EMPTY_PATCH_THEN_SHELL, [
        ("apply_patch", ""),
        ("shell", "git config --local user.email 'dev@example.com'; "
                  "git config --local --get-regexp '^user\\.(name|email)$'"),
    ]),
    (REAL_WEB_SEARCH_UNQUOTED, [(
        "web_search",
        "site:github.com/open-telemetry/opentelemetry-collector-contrib telemetrygen Docker ghcr.io v0.161.0 traces",
    )]),
    (REAL_WEB_SEARCH_QUOTED, [
        ("web_search", "site:developers.openai.com/codex AGENTS.md instructions shell commands Codex"),
    ]),
    (REAL_WEB_SEARCH_BACKTICK_IN_STRING, [(
        "web_search",
        'site:github.com/open-telemetry/opentelemetry-collector "The `otlp` deprecated alias" exporter',
    )]),
    (REAL_WEB_OPEN_URL, [("web_search", "https://github.com/minio/minio/pull/21550")]),
    (REAL_WEB_OPEN_REF, [("web_search", "")]),
    (REAL_WRITE_STDIN, [("write_stdin", "")]),
    (REAL_WRITE_STDIN_TEMPLATES, [("write_stdin", "")]),
    (REAL_VIEW_IMAGE, [("view_image", r"C:\Git\EnterpriseAI\.tmp-letterhead-body.bmp")]),
    (REAL_VIEW_IMAGE_TWICE, [
        ("view_image", r"C:\Git\EnterpriseAI\backend\assets\logos\logo-horizontal-positive.png"),
        ("view_image", r"C:\Git\EnterpriseAI\backend\assets\logos\logo-stacked-positive.png"),
    ]),
    (REAL_VIEW_IMAGE_TEMPLATE_PATH, [
        ("view_image", r"C:\Git\EnterpriseAI\tmp\pdfs\manager\manager-page-${String(n).padStart(2,'0')}.png"),
    ]),
    (REAL_NO_CALLS_REGEX_LITERAL, []),
]


@pytest.mark.parametrize("js,expected", REAL_EXEC_CASES,
                         ids=[f"real{i}" for i in range(len(REAL_EXEC_CASES))])
def test_exec_inner_calls_on_real_scripts(js, expected):
    assert exec_inner_calls(js) == expected


@pytest.mark.parametrize("js,hidden", [
    (REAL_PATCH_WITH_TOOLS_TEXT, "list_meetings"),
    (REAL_PATCH_WITH_PROPOSE, "propose"),
])
def test_tools_text_inside_a_patch_string_is_not_a_call(js, hidden):
    # the naive regex is fooled by the patched source; the tokenizer is not
    assert hidden in NAIVE_CALL.findall(js)
    names = [name for name, _ in exec_inner_calls(js)]
    assert names == ["apply_patch"]


def test_patch_files_on_a_real_exec_script():
    assert patch_files(REAL_PATCH_DELETE_AND_ADD) == [
        r"C:\Git\EnterpriseAI\backend\config\review_profiles.yaml",
        r"C:\Git\EnterpriseAI\backend\config\review_profiles\legal-contract-v1\profile.yaml",
        r"C:\Git\EnterpriseAI\backend\config\review_profiles\legal-contract-v1\taxonomy.yaml",
    ]
    assert patch_files(REAL_PATCH_QUOTES_AND_BACKTICKS) == [
        r"C:\Git\EnterpriseAI\backend\tests\review\test_runner.py",
    ]


# ---------------------------------------------------------------------------
# exec_inner_calls — tokenizer edge cases
# ---------------------------------------------------------------------------
def test_single_quoted_js_strings():
    js = "const r = await tools.exec_command({'cmd': 'git log --format=\"%an\"', workdir: 'C:\\\\x'});"
    assert exec_inner_calls(js) == [("shell", 'git log --format="%an"')]
    js = "text('tools.fake(1)'); await tools.exec_command({cmd: 'it\\'s fine'});"
    assert exec_inner_calls(js) == [("shell", "it's fine")]


def test_tools_text_in_template_literal_comment_or_regex_is_ignored():
    js = (
        "// tools.commented_out({})\n"
        "/* tools.block_comment({}) */\n"
        "const doc = `usage: tools.in_template(x) \\` still inside`;\n"
        "const re = /[\"']tools\\.in_regex\\(/g;\n"
        "const s = 'tools.in_single(' + \"tools.in_double(\";\n"
        "await tools.exec_command({cmd: \"ls\"});\n"
    )
    assert exec_inner_calls(js) == [("shell", "ls")]


def test_calls_inside_template_substitutions_are_found():
    js = 'text(`out: ${(await tools.exec_command({cmd:"git status"})).output} done`);'
    assert exec_inner_calls(js) == [("shell", "git status")]


def test_command_from_a_variable_or_concatenation():
    js = 'const cmd = "git status --short";\nconst r = await tools.exec_command({cmd, workdir: "x"});'
    assert exec_inner_calls(js) == [("shell", "git status --short")]
    js = 'const c = "npm test";\nawait tools.exec_command({cmd: c});'
    assert exec_inner_calls(js) == [("shell", "npm test")]
    js = 'await tools.exec_command({cmd: "git " + "diff " + \'--stat\'});'
    assert exec_inner_calls(js) == [("shell", "git diff --stat")]
    js = 'await tools.exec_command({cmd: ["git", "status"]});'
    assert exec_inner_calls(js) == [("shell", "git status")]


def test_apply_patch_label_follows_its_own_argument():
    js = (
        'const a = "*** Begin Patch\\n*** Update File: first.py\\n*** End Patch";\n'
        'const b = "*** Begin Patch\\n*** Add File: second.py\\n+x\\n*** End Patch";\n'
        "await tools.apply_patch(a);\nawait tools.apply_patch(b);\n"
    )
    assert exec_inner_calls(js) == [("apply_patch", "first.py"), ("apply_patch", "second.py")]
    # a patch assembled some other way falls back to scanning the whole script
    js = ('const patch = ["*** Begin Patch", "*** Update File: src/x.py", "@@"].join("\\n");\n'
          "await tools.apply_patch(patch);")
    assert exec_inner_calls(js) == [("apply_patch", "src/x.py")]


def test_web_run_open_with_url_key():
    js = 'await tools.web__run({open: [{url: "https://example.com/docs"}]});'
    assert exec_inner_calls(js) == [("web_search", "https://example.com/docs")]
    js = 'await tools.web__run({search_query: "plain query"});'
    assert exec_inner_calls(js) == [("web_search", "plain query")]


def test_unknown_inner_tool_keeps_its_name():
    js = 'const r = await tools.list_mcp_resources({server: "x"});\nawait tools.exec_command({cmd:"ls"});'
    assert exec_inner_calls(js) == [("list_mcp_resources", ""), ("shell", "ls")]


def test_unicode_escapes_are_decoded():
    js = 'await tools.exec_command({"cmd":"echo \\u00e7ay \\ud83d\\ude00"});'
    assert exec_inner_calls(js) == [("shell", "echo \u00e7ay \U0001F600")]


def test_long_commands_are_capped_to_one_line():
    js = 'await tools.exec_command({cmd: "echo a\\n\\n   echo b ' + "x" * 400 + '"});'
    [(name, label)] = exec_inner_calls(js)
    assert name == "shell" and len(label) == 200 and label.startswith("echo a echo b x")


@pytest.mark.parametrize("js", [None, 12, "", "text('no tools here')", "tools.", "tools.exec_command",
                                "const f = tools.exec_command;", "tools[\"exec_command\"]({})"])
def test_exec_inner_calls_without_calls(js):
    assert exec_inner_calls(js) == []


def test_exec_inner_calls_on_truncated_scripts():
    assert exec_inner_calls('await tools.exec_command({"cmd":"git st') == [("shell", "git st")]
    assert exec_inner_calls("await tools.exec_command({cmd: `unterminated ${x") == [("shell", "unterminated ${x")]
    assert exec_inner_calls("await tools.apply_patch(") == [("apply_patch", "")]
    assert exec_inner_calls("await tools.view_image({path: /* never closed") == [("view_image", "")]


# ---------------------------------------------------------------------------
# shell_label
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("cmd,expected", [
    (["bash", "-lc", "ls -la"], "ls -la"),
    (["/bin/bash", "-lc", "cd src && pytest -q"], "cd src && pytest -q"),
    (["sh", "-c", "echo hi"], "echo hi"),
    (["zsh", "-lc", "git status"], "git status"),
    ([r"C:\Users\dev\AppData\Local\Microsoft\WindowsApps\pwsh.exe", "-Command", "git status --short"],
     "git status --short"),
    (['"C:\\Program Files\\PowerShell\\7\\pwsh.exe"', "-c", "Get-Date"], "Get-Date"),
    (["powershell", "-NoProfile", "-Command", "Get-ChildItem -Force"], "Get-ChildItem -Force"),
    (["powershell.exe", "-NoLogo", "Get-Date"], "Get-Date"),  # no -Command: last argument
    (["cmd.exe", "/c", "dir /b"], "dir /b"),
    (["CMD", "/d", "/s", "/c", "echo", "hi"], "echo hi"),
    (["git", "status", "--short"], "git status --short"),
    (["bash", "script.sh"], "bash script.sh"),
    (["python", "-c", "print(1)"], "python -c print(1)"),
    (["echo", 1, 2.5], "echo 1 2.5"),
    ("  git   status\n  --short ", "git status --short"),
])
def test_shell_label(cmd, expected):
    assert shell_label(cmd) == expected


@pytest.mark.parametrize("cmd", [None, 42, {}, {"command": "ls"}, [], [None], "", "   "])
def test_shell_label_garbage(cmd):
    assert shell_label(cmd) == ""


def test_shell_label_is_capped():
    assert len(shell_label(["bash", "-lc", "echo " + "x" * 500])) == 200


# ---------------------------------------------------------------------------
# patch_files
# ---------------------------------------------------------------------------
RAW_PATCH = (
    "*** Begin Patch\n"
    "*** Update File: src/a.py\n@@\n-x\n+y\n"
    "*** Add File: C:\\Git\\new_flow.md\n+z\n"
    "*** Delete File: old.txt\n"
    "*** Update File: b.py\n*** Move to: docs/renamed b.py\n@@\n-q\n"
    "*** Update File: src/a.py\n@@\n"
    "*** End Patch\n"
)


def test_patch_files_raw_text_in_order_and_deduped():
    assert patch_files(RAW_PATCH) == ["src/a.py", r"C:\Git\new_flow.md", "old.txt", "b.py", "docs/renamed b.py"]


def test_patch_files_crlf_and_trailing_header():
    assert patch_files("*** Begin Patch\r\n*** Update File: a.py \r\n@@\r\n") == ["a.py"]
    assert patch_files("*** Delete File: gone.txt") == ["gone.txt"]


def test_patch_files_js_escaped_text():
    # literal \n between lines; \\n / \\r inside paths are escaped backslashes
    js = ('const patch = "*** Begin Patch\\n*** Update File: C:\\\\Git\\\\new_flow.md\\n@@\\n-a\\n'
          '*** Move to: C:\\\\Git\\\\docs\\\\renamed.md\\n*** Add File: docs/John\'s \\"notes\\".md\\n+x\\n'
          '*** End Patch";')
    assert patch_files(js) == [r"C:\Git\new_flow.md", r"C:\Git\docs\renamed.md", "docs/John's \"notes\".md"]


def test_patch_files_escaped_unicode_and_single_quotes():
    assert patch_files('"*** Add File: docs/\\u00e7ay.md\\n+x"') == ["docs/\u00e7ay.md"]
    js = "const p = ['*** Begin Patch', '*** Update File: src/x.py', '@@'].join('\\n');"
    assert patch_files(js) == ["src/x.py"]


def test_patch_files_template_literal_with_real_newlines():
    js = "const patch = `*** Begin Patch\n*** Update File: C:\\\\Git\\\\x.py\n@@\n-a\n+b\n*** End Patch`;"
    assert patch_files(js) == [r"C:\Git\x.py"]
    # a raw UNC path keeps its leading double backslash
    assert patch_files("*** Update File: \\\\server\\share\\a.txt\n") == [r"\\server\share\a.txt"]


def test_patch_files_ignores_patch_text_inside_a_patched_file():
    # editing a test file that itself contains patch text (raw and escaped)
    raw = ("*** Begin Patch\n*** Update File: tests/test_x.py\n@@\n"
           "+    js = '\"*** Begin Patch\\n*** Update File: bogus.py\\n\"'\n"
           "+*** Add File: also_bogus.py\n"
           "+    RAW = \"*** Delete File: nope.txt\"\n"
           "*** End Patch\n")
    assert patch_files(raw) == ["tests/test_x.py"]
    js = f"const patch = {json.dumps(raw)};\nconst r = await tools.apply_patch(patch);\ntext(r);\n"
    assert patch_files(js) == ["tests/test_x.py"]
    assert exec_inner_calls(js) == [("apply_patch", "tests/test_x.py")]


@pytest.mark.parametrize("patch", [None, 5, "", "no patch here", "*** Begin Patch\n*** End Patch\n",
                                   "*** Update File: \n"])
def test_patch_files_nothing(patch):
    assert patch_files(patch) == []


# ---------------------------------------------------------------------------
# describe_call
# ---------------------------------------------------------------------------
PATCH_TEXT = "*** Begin Patch\n*** Update File: src/app.py\n@@\n-a\n+b\n*** End Patch\n"


@pytest.mark.parametrize("name,args,expected", [
    # legacy tools
    ("shell", {"command": ["bash", "-lc", "ls -la"], "workdir": "/x"}, ("shell", "ls -la")),
    ("shell", json.dumps({"command": ["pwsh.exe", "-Command", "git status"]}), ("shell", "git status")),
    ("shell_command", {"command": "git log -1"}, ("shell", "git log -1")),
    ("exec_command", '{"cmd":"npm test","yield_time_ms":1000}', ("shell", "npm test")),
    ("apply_patch", {"input": PATCH_TEXT}, ("apply_patch", "src/app.py")),
    ("apply_patch", json.dumps({"patch": PATCH_TEXT}), ("apply_patch", "src/app.py")),
    ("apply_patch", PATCH_TEXT, ("apply_patch", "src/app.py")),  # freeform tool input
    ("apply_patch", {"input": 5}, ("apply_patch", "")),
    ("update_plan", {"plan": [{"step": "Read the code", "status": "completed"},
                              {"step": "Write   tests", "status": "in_progress"},
                              {"step": "Ship", "status": "pending"}]}, ("update_plan", "Write tests")),
    ("update_plan", {"explanation": "x", "plan": [{"step": "Ship", "status": "pending"}]}, ("update_plan", "")),
    ("update_plan", {"plan": "not a list"}, ("update_plan", "")),
    ("view_image", {"path": "C:\\x.png"}, ("view_image", "C:\\x.png")),
    # collaboration tools (real argument shapes; messages are encrypted)
    ("spawn_agent", '{"task_name":"dependency_audit","fork_turns":"all","message":"gAAAAABqsul3zJ7-B-Bh"}',
     ("spawn_agent", "dependency_audit")),
    ("spawn_agent", {"agent_type": "explorer", "message": "gAAAAAB"}, ("spawn_agent", "explorer")),
    ("send_message", '{"target":"/root","message":"gAAAAABqsun2LhpftotoclGr"}', ("send_message", "/root")),
    ("followup_task", '{"target":"deployment_audit","message":"gAAAAABqs6icD-Gs36rIKB"}',
     ("followup_task", "deployment_audit")),
    ("interrupt_agent", '{"target":"test_gap_audit"}', ("interrupt_agent", "test_gap_audit")),
    ("wait_agent", '{"timeout_ms":10000}', ("wait_agent", "")),
    ("list_agents", "{}", ("list_agents", "")),
    ("wait", '{"cell_id":"4","yield_time_ms":30000,"max_tokens":20000}', ("wait", "")),
    ("request_user_input",
     '{"questions":[{"header":"Codex scope","id":"codex_scope","question":"What kind of Codex support?",'
     '"options":[{"label":"Monitor local sessions (Recommended)","description":"..."}]}]}',
     ("request_user_input", "Codex scope")),
    ("request_user_input", {"questions": [{"question": "Proceed?"}]}, ("request_user_input", "Proceed?")),
    ("request_user_input", {"questions": []}, ("request_user_input", "")),
    # anything else
    ("mcp__github__create_issue", {"title": "x"}, ("mcp__github__create_issue", "")),
])
def test_describe_call(name, args, expected):
    assert describe_call(name, args) == expected


@pytest.mark.parametrize("args", [None, 5, "not json", "[1, 2]", '"a string"', "{", [1], {"unexpected": True}])
@pytest.mark.parametrize("name", ["shell", "exec_command", "spawn_agent", "send_message",
                                  "update_plan", "view_image", "request_user_input", "wait_agent"])
def test_describe_call_garbage_args(name, args):
    display, label = describe_call(name, args)
    assert label == ""
    assert display in (name, "shell")


def test_describe_call_bad_name():
    assert describe_call(None, {}) == ("", "")  # type: ignore[arg-type]


@pytest.mark.parametrize("name", ["spawn_agent", "send_message", "followup_task", "interrupt_agent",
                                  "wait_agent", "list_agents", "some_future_tool"])
def test_describe_call_never_shows_the_encrypted_message(name):
    secret = "gAAAAABqsul3zJ7-SECRET"
    _, label = describe_call(name, {"message": secret})
    assert secret not in label and label == ""
    _, label = describe_call(name, json.dumps({"target": "/root/a", "message": secret}))
    assert secret not in label


# ---------------------------------------------------------------------------
# output_failed
# ---------------------------------------------------------------------------
def blocks(header: str, body: str = "") -> list:
    """A code-mode ``custom_tool_call_output.output`` (list of input_text)."""
    return [{"type": "input_text", "text": header}, {"type": "input_text", "text": body}]


@pytest.mark.parametrize("output,failed", [
    # code-mode exec results (header block + body block)
    (blocks("Script completed\nWall time 10.9 seconds\nOutput:\n", "# CLAUDE.md\r\n"), False),
    (blocks("Script failed\nWall time 0.1 seconds\nOutput:\n",
            "Script error:\napply_patch verification failed: Failed to find expected lines"), True),
    (blocks("Script running with cell ID 60\nWall time 31.0 seconds\nOutput:\n", ""), False),
    # a completed script whose command output says "Exit code: 1" is not a failure
    (blocks("Script completed\nWall time 1.0 seconds\nOutput:\n",
            "Exit code: 1\nWall time: 0.2 seconds\nOutput:\nboom"), False),
    ("Script completed\nWall time 1.0 seconds\nOutput:\nExit code: 1\nProcess exited with code 3\n", False),
    ("Script failed\nWall time 0.1 seconds\nOutput:\nReferenceError: x is not defined", True),
    ("Script running with cell ID 4\nWall time 30.0 seconds\nOutput:\n", False),
    # unified-exec style text results
    ("Exit code: 0\nWall time: 0.5 seconds\nOutput:\nok", False),
    ("Exit code: 1\nWall time: 0.5 seconds\nOutput:\nerror: boom", True),
    ("Chunk ID: 3f2a\nWall time: 0.2 seconds\nProcess exited with code 2\nOriginal token count: 5\nOutput:\nfatal", True),
    ("Chunk ID: 3f2a\nWall time: 0.2 seconds\nProcess exited with code 0\nOutput:\n", False),
    ("Chunk ID: 1\nWall time: 1 seconds\nOutput:\nProcess exited with code 3", False),  # body only
    ("a\nb\nc\nd\ne\nExit code: 1", False),  # beyond the 5-line header
    # legacy shell JSON results
    (json.dumps({"output": "x", "metadata": {"exit_code": 0, "duration_seconds": 0.1}}), False),
    (json.dumps({"output": "boom", "metadata": {"exit_code": 1, "duration_seconds": 0.1}}), True),
    ({"output": "boom", "metadata": {"exit_code": 127}}, True),
    ({"output": "ok", "metadata": {"exit_code": 0}}, False),
    ({"output": "Exit code: 2\nOutput:\n"}, True),
    ({"content": "fine", "success": True}, False),
    # collaboration results (real strings)
    ("collab spawn failed: agent thread limit reached", True),
    ("collab tool failed: agent thread limit reached", True),
    ("aborted by user after 7.5s", True),
    ('{"task_name":"/root/dependency_audit"}', False),
    ('{"agents":[{"agent_name":"/root","agent_status":"running"}]}', False),
    ('{"message":"Wait timed out.","timed_out":true}', False),
    ("", False),
    # code-mode wait results come back as a list too
    ([{"type": "input_text", "text": "Script completed\nWall time 24.1 seconds\nOutput:\n"}], False),
    ([{"type": "input_image", "image_url": "data:..."}], False),
    (["Script failed\nOutput:\n"], True),
    ([], False),
    (None, False),
    (42, False),
])
def test_output_failed(output, failed):
    assert output_failed(output) is failed


# ---------------------------------------------------------------------------
# clean_codex_prompt
# ---------------------------------------------------------------------------
IDE_PROMPT = ("# Context from my IDE setup:\n\n## Active file: a.md\n\n## Open tabs:\n- a.md: a.md\n\n"
              "## My request:\ninstall all packages\n")


def test_clean_codex_prompt_ide_preamble():
    assert clean_codex_prompt(IDE_PROMPT) == "install all packages"


def test_clean_codex_prompt_my_request_for_codex_variant():
    text = "# Context from my IDE setup:\n\n## Open tabs:\n- x.py: x.py\n\n## My request for Codex:\nfix the bug\n"
    assert clean_codex_prompt(text) == "fix the bug"


def test_clean_codex_prompt_last_my_request_wins():
    # the active selection itself contains a "## My request:" line
    text = ("# Context from my IDE setup:\n\n## Active selection of the file:\n## My request:\nold text\n"
            "## Open tabs:\n- a: a\n\n## My request:\nthe real request\n")
    assert clean_codex_prompt(text) == "the real request"


def test_clean_codex_prompt_strips_injected_blocks():
    text = ("<environment_context>\n  <cwd>C:\\Git\\x</cwd>\n  <shell>powershell</shell>\n</environment_context>\n"
            "# AGENTS.md instructions for C:\\Git\\x\n\n<INSTRUCTIONS>\n@CLAUDE.md\n</INSTRUCTIONS>\n"
            "<recommended_plugins>\n- Figma\n</recommended_plugins>"
            "<user_instructions>be nice</user_instructions>"
            "<turn_aborted>The user interrupted.</turn_aborted>"
            "  please   run the tests  ")
    assert clean_codex_prompt(text) == "please run the tests"


def test_clean_codex_prompt_plain_text_passthrough():
    assert clean_codex_prompt("proceed to slice 10\n") == "proceed to slice 10"
    assert clean_codex_prompt("check the file `deploy/.env`") == "check the file `deploy/.env`"


@pytest.mark.parametrize("text", [None, "", "   ", "## My request:\n   \n",
                                  "<environment_context><cwd>x</cwd></environment_context>"])
def test_clean_codex_prompt_empty(text):
    assert clean_codex_prompt(text) is None


def test_clean_codex_prompt_limit():
    # clean_prompt collapses whitespace, then cuts at 200 characters
    assert clean_codex_prompt("## My request:\n" + "word " * 100) == ("word " * 40)
    assert len(clean_codex_prompt("x" * 500)) == 200
    assert clean_codex_prompt("x" * 500, limit=50) == "x" * 50
