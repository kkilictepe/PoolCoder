"""HTTP front-end: serve the dashboard as a mobile-friendly HTML page.

A second renderer over the same decoupled core. ``EngineManager`` lazily runs
one ``Engine`` per viewed session (idle-evicted, capped) and shares a single
plan-limits poller. The HTTP layer reads immutable snapshots and renders HTML —
it never touches mutable state. Stdlib only (matches ``usage-exporter``).

Endpoints:
    GET /                 session list (auto-refreshing)
    GET /s/<id>           live dashboard page for a session
    GET /partial/list     list fragment (polled by the page)
    GET /partial/s/<id>   dashboard fragment (polled by the page)
    GET /api/s/<id>       JSON snapshot
    GET /health           ok
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
import html
import json
import socket
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .config import Config
from .engine import Engine
from .format import (
    fmt_age, fmt_clock, fmt_duration, fmt_int, fmt_pct, fmt_pct100, fmt_tokens, fmt_usd, reset_in,
)
from .overview import peek_session
from .paths import find_session, list_sessions
from .pricing import Pricing
from .snapshot import Snapshot

esc = html.escape


# --------------------------------------------------------------------------- #
# Engine lifecycle
# --------------------------------------------------------------------------- #
class EngineManager:
    """Lazily runs an Engine per viewed session; evicts idle ones."""

    def __init__(self, config: Config, pricing: Pricing, enable_plan_limits: bool = True,
                 max_engines: int = 8, idle_ttl: float = 300.0):
        self.config = config
        self.pricing = pricing
        self.max_engines = max_engines
        self.idle_ttl = idle_ttl
        self._engines: dict[str, Engine] = {}
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()
        from .sources.plan_limits import PlanLimitsSource
        self.plan = PlanLimitsSource() if (enable_plan_limits and config.plan_limits) else None
        if self.plan:
            self.plan.start()

    def _evict_locked(self, now: float) -> None:
        for sid in list(self._engines):
            if now - self._last.get(sid, now) > self.idle_ttl:
                self._engines.pop(sid).stop()
                self._last.pop(sid, None)
        while len(self._engines) > self.max_engines:
            sid = min(self._last, key=self._last.get)
            self._engines.pop(sid).stop()
            self._last.pop(sid, None)

    def snapshot(self, session_id: str) -> Snapshot | None:
        info = find_session(session_id)
        if info is None:
            return None
        now = time.monotonic()
        with self._lock:
            self._evict_locked(now)
            engine = self._engines.get(session_id)
            if engine is None:
                engine = Engine(info, self.config, self.pricing, enable_plan_limits=False)
                engine.start()
                self._engines[session_id] = engine
            self._last[session_id] = now
        snap = engine.get_snapshot()
        if snap is not None and self.plan is not None:
            snap = dataclasses.replace(snap, plan_limits=self.plan.view)
        return snap

    def stop_all(self) -> None:
        with self._lock:
            for engine in self._engines.values():
                engine.stop()
            self._engines.clear()
            self._last.clear()
            if self.plan:
                self.plan.stop()


# --------------------------------------------------------------------------- #
# HTML rendering
# --------------------------------------------------------------------------- #
_CSS = """
:root{color-scheme:dark;}
*{box-sizing:border-box;}
body{margin:0;background:#0d1117;color:#c9d1d9;
 font:14px/1.5 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;}
a{color:#58a6ff;text-decoration:none;} a:active{opacity:.6;}
.wrap{max-width:1100px;margin:0 auto;padding:10px;}
.topbar{position:sticky;top:0;background:#0d1117ee;backdrop-filter:blur(6px);
 padding:8px 4px;border-bottom:1px solid #21262d;z-index:5;}
.hdr .r1{display:flex;flex-wrap:wrap;gap:6px;align-items:center;}
.proj{font-weight:700;color:#e6edf3;} .model{color:#39c5cf;}
.mode{color:#bc8cff;} .dim{color:#8b949e;}
.badge{font-weight:700;padding:0 6px;border-radius:6px;}
.badge.live{color:#3fb950;} .badge.idle{color:#8b949e;}
.prompt{color:#adbac7;font-style:italic;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
.grid{display:grid;gap:10px;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));margin-top:10px;}
.card{background:#161b22;border:1px solid #21262d;border-radius:10px;padding:10px 12px;}
.card h2{margin:0 0 6px;font-size:12px;letter-spacing:.05em;text-transform:uppercase;color:#8b949e;}
.card.wide{grid-column:1/-1;}
.big{font-size:20px;font-weight:700;color:#e6edf3;}
.bar{height:10px;border-radius:6px;background:#21262d;overflow:hidden;margin:4px 0 8px;}
.bar .fill{height:100%;border-radius:6px;transition:width .4s ease;}
table.kv{width:100%;border-collapse:collapse;}
table.kv td{padding:1px 0;vertical-align:top;}
table.kv td:first-child{color:#8b949e;white-space:nowrap;padding-right:10px;}
table.kv td:last-child{text-align:right;color:#e6edf3;}
.tools span{color:#39c5cf;margin-right:8px;white-space:nowrap;}
.tools b{color:#8b949e;font-weight:400;}
.list .row{display:flex;gap:8px;align-items:center;padding:9px 10px;border:1px solid #21262d;
 border-radius:10px;background:#161b22;margin-bottom:8px;}
.list .row .pct{color:#39c5cf;width:48px;text-align:right;}
.list .row .name{font-weight:700;color:#e6edf3;}
.list .last{color:#8b949e;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;flex:1;}
.log{font-size:13px;}
.log .e{display:flex;gap:8px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
.log .t{color:#484f58;}
.k-tool{color:#39c5cf;} .k-result{color:#3fb950;} .k-prompt{color:#e6edf3;font-weight:700;}
.k-text{color:#c9d1d9;} .k-thinking{color:#6e7681;font-style:italic;}
.k-compaction{color:#bc8cff;} .k-agent{color:#d29922;} .k-workflow{color:#58a6ff;}
.sub{display:flex;gap:8px;align-items:baseline;}
.sub .st{width:14px;} .run{color:#d29922;} .done{color:#3fb950;}
.foot{color:#6e7681;padding:12px 4px;text-align:center;}
body.off .topbar::after{content:" · reconnecting…";color:#d29922;}
"""

_JS = """
(function(){
 var dash=document.getElementById('dash'), ts=document.getElementById('ts');
 function tick(){
  fetch('__EP__',{cache:'no-store'}).then(function(r){if(!r.ok)throw 0;return r.text();})
   .then(function(t){dash.innerHTML=t;if(ts)ts.textContent=new Date().toLocaleTimeString();
     document.body.classList.remove('off');})
   .catch(function(){document.body.classList.add('off');});
 }
 setInterval(tick,__MS__);
})();
"""


def _page(title: str, inner: str, endpoint: str, ms: int = 1500) -> str:
    js = _JS.replace("__EP__", endpoint).replace("__MS__", str(ms))
    return (
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width,initial-scale=1'>"
        f"<title>{esc(title)}</title><style>{_CSS}</style></head><body>"
        f"<div class=wrap><div id=dash>{inner}</div>"
        "<div class=foot>updated <span id=ts>…</span> · "
        "<a href='/'>← sessions</a></div></div>"
        f"<script>{js}</script></body></html>"
    )


def _bar(frac: float | None) -> str:
    frac = max(0.0, min(frac or 0.0, 1.0))
    color = "#3fb950" if frac < 0.6 else "#d29922" if frac < 0.85 else "#f85149"
    return f"<div class=bar><div class=fill style='width:{frac*100:.1f}%;background:{color}'></div></div>"


def _kv(rows: list[tuple[str, str]]) -> str:
    body = "".join(f"<tr><td>{esc(k)}</td><td>{v}</td></tr>" for k, v in rows)
    return f"<table class=kv>{body}</table>"


def _card(title: str, inner: str, wide: bool = False) -> str:
    cls = "card wide" if wide else "card"
    return f"<section class='{cls}'><h2>{esc(title)}</h2>{inner}</section>"


def _occ_color(frac: float) -> str:
    return "#3fb950" if frac < 0.6 else "#d29922" if frac < 0.85 else "#f85149"


def _header(snap: Snapshot) -> str:
    s = snap.session
    badge = ("<span class='badge live'>● live</span>" if s.is_live
             else f"<span class='badge idle'>idle {esc(fmt_age(s.idle_s))}</span>")
    mode = f"<span class=mode>[{esc(s.mode)}]</span>" if s.mode and s.mode != "normal" else ""
    loading = "<span class=mode>⏳ loading…</span>" if snap.loading else ""
    prompt = esc(s.last_prompt) if s.last_prompt else "<span class=dim>(no prompt yet)</span>"
    return (
        "<div class='topbar hdr'>"
        f"<div class=r1>{badge}{loading}<span class=proj>{esc(s.cwd or s.project_hash)}</span>"
        f"<span class=dim>({esc(s.git_branch or 'no branch')})</span>"
        f"<span class=model>{esc(s.model or '?')}</span>{mode}</div>"
        f"<div class=dim>{esc(fmt_duration(s.duration_s))} · {s.turns} turns · {s.user_messages} prompts</div>"
        f"<div class=prompt>» {prompt}</div></div>"
    )


def _card_context(snap: Snapshot) -> str:
    s = snap.session
    head = (f"<div class=big><span style='color:{_occ_color(s.occupancy)}'>{fmt_pct(s.occupancy, 1)}</span>"
            f"</div><div class=dim>{fmt_int(s.current_context)} / {fmt_int(s.effective_window)} tokens</div>")
    comp = str(s.compaction_count) + (f" (last {esc(fmt_clock(s.last_compaction))})" if s.compaction_count else "")
    rows = [
        ("to limit", fmt_tokens(s.tokens_to_limit)),
        ("compact headroom", fmt_tokens(s.auto_compact_headroom)),
        ("compactions", comp),
        ("peak", fmt_tokens(s.max_context)),
    ]
    return _card("Context window", _bar(s.occupancy) + head + _kv(rows))


def _card_tokens(snap: Snapshot) -> str:
    s = snap.session
    c = s.cumulative
    rows = [
        ("input", fmt_tokens(c.input)),
        ("cache read", fmt_tokens(c.cache_read)),
        ("cache write", fmt_tokens(c.cache_creation)),
        ("output", fmt_tokens(c.output)),
        ("cache hit", fmt_pct(s.cache_hit_ratio)),
    ]
    cost = (f"<div class=big style='color:#3fb950'>{esc(fmt_usd(s.cost.total_usd))}</div>"
            f"<div class=dim>{esc(fmt_usd(s.cost.per_min_usd))}/min · {fmt_tokens(s.tokens_per_min)}/min</div>")
    return _card("Tokens & cost", cost + _kv(rows))


def _card_activity(snap: Snapshot) -> str:
    s = snap.session
    if s.current_activity:
        a = s.current_activity
        now = (f"<div class=big style='color:#d29922'>▶ {esc(a.name)}</div>"
               f"<div class=dim>{esc(a.target[:80])} · {esc(fmt_duration(a.elapsed_s))}</div>")
    elif s.in_flight:
        now = f"<div class=big style='color:#d29922'>▶ {len(s.in_flight)} tools in flight</div>"
    else:
        now = "<div class=big dim>· idle</div>"
    rows = [("tool calls", f"{s.tool_call_total} (errors {s.tool_errors})"), ("files", str(s.files_count))]
    tools = "".join(f"<span>{esc(n)}<b>×{c}</b></span>" for n, c in s.top_tools[:7])
    return _card("Activity", now + _kv(rows) + f"<div class=tools>{tools}</div>")


def _card_subagents(snap: Snapshot) -> str:
    s = snap.session
    if not s.subagents:
        return _card("Subagents", "<div class=dim>none</div>")
    rows = []
    for sub in s.subagents[:12]:
        st = "<span class='st run'>▶</span>" if sub.running else "<span class='st done'>✓</span>"
        rows.append(
            f"<div class=sub>{st}<b>{esc(sub.agent_type)}</b>"
            f"<span class=dim style='margin-left:auto'>{fmt_tokens(sub.tokens)}</span></div>"
            f"<div class=dim style='margin:-2px 0 6px 22px'>{esc(sub.description[:60])}</div>"
        )
    title = f"Subagents · {s.subagents_running} running / {len(s.subagents)}"
    return _card(title, "".join(rows))


def _card_workflows(snap: Snapshot) -> str:
    s = snap.session
    if not s.workflows:
        return _card("Workflows", "<div class=dim>none</div>")
    rows = []
    for wf in s.workflows[:6]:
        running = wf.running_agents > 0
        run = f" · <span class=run>{wf.running_agents} running</span>" if running else ""
        phases = f" <span class=dim>[{esc(', '.join(wf.phases))}]</span>" if wf.phases else ""
        rows.append(
            f"<div><b>⊞ {esc(wf.name)}</b>{phases}</div>"
            f"<div class=dim style='margin:-2px 0 6px 16px'>{wf.completed_agents}/{wf.total_agents} done{run}</div>"
        )
    title = f"Workflows · {s.workflows_running_agents} agents running"
    return _card(title, "".join(rows))


def _card_plan(snap: Snapshot) -> str:
    pl = snap.plan_limits
    if pl is None:
        return _card("Plan limits", "<div class=dim>disabled</div>")
    if not pl.available:
        return _card("Plan limits", f"<div class=dim>{esc(pl.error or 'unavailable')}</div>")
    rows = [
        ("5-hour", f"{esc(fmt_pct100(pl.five_hour_pct))} · resets {esc(reset_in(pl.five_hour_resets_at))}"),
        ("weekly", esc(fmt_pct100(pl.seven_day_pct))),
        ("wk opus", esc(fmt_pct100(pl.seven_day_opus_pct))),
        ("wk sonnet", esc(fmt_pct100(pl.seven_day_sonnet_pct))),
    ]
    return _card("Plan limits", _bar((pl.five_hour_pct or 0) / 100.0) + _kv(rows))


def _card_events(snap: Snapshot) -> str:
    s = snap.session
    rows = []
    for ev in list(s.events)[-22:]:
        t = fmt_clock(ev.at) if ev.at else "--:--:--"
        rows.append(f"<div class=e><span class=t>{esc(t)}</span>"
                    f"<span class='k-{esc(ev.kind)}'>{esc(ev.text[:140])}</span></div>")
    if not rows:
        rows.append("<div class=dim>waiting for activity…</div>")
    return _card("Recent activity", f"<div class=log>{''.join(rows)}</div>", wide=True)


def fragment_dashboard(snap: Snapshot | None) -> str:
    if snap is None:
        return ("<div class='topbar hdr'><div class=r1><span class=mode>session not found</span>"
                "<a href='/'>← back to sessions</a></div></div>")
    panels = "".join([
        _card_context(snap), _card_tokens(snap), _card_activity(snap),
        _card_subagents(snap), _card_workflows(snap), _card_plan(snap), _card_events(snap),
    ])
    return _header(snap) + f"<div class=grid>{panels}</div>"


def page_dashboard(session_id: str, snap: Snapshot | None) -> str:
    title = f"pool-coder · {session_id[:8]}"
    return _page(title, fragment_dashboard(snap), f"/partial/s/{urllib.parse.quote(session_id)}")


def fragment_list(config: Config) -> str:
    sessions = [s for s in list_sessions() if s.age_seconds() <= config.active_window_seconds][:40]
    if not sessions:
        return "<div class=dim style='padding:20px'>No active sessions in the last 30 min.</div>"
    rows = []
    for info in sessions:
        ov = peek_session(info, config)
        dot = "<span class='badge live'>●</span>" if ov.is_live else "<span class=dim>○</span>"
        rows.append(
            f"<a class=row href='/s/{urllib.parse.quote(info.session_id)}'>{dot}"
            f"<span class=dim>{esc(fmt_age(info.age_seconds()))}</span>"
            f"<span class=pct>{esc(fmt_pct(ov.occupancy))}</span>"
            f"<span class=name>{esc(ov.label)}</span>"
            f"<span class=last>{esc((ov.last_text or '')[:60])}</span></a>"
        )
    return ("<div class='topbar hdr'><div class=r1><span class=proj>pool-coder</span>"
            "<span class=dim>active Claude Code sessions — tap to monitor</span></div></div>"
            f"<div class=list style='margin-top:10px'>{''.join(rows)}</div>")


def page_list(config: Config) -> str:
    return _page("pool-coder", fragment_list(config), "/partial/list", ms=3000)


def snapshot_json(snap: Snapshot | None) -> str:
    if snap is None:
        return json.dumps({"error": "no snapshot"})

    def default(o):
        if isinstance(o, _dt.datetime):
            return o.isoformat()
        return str(o)

    return json.dumps(dataclasses.asdict(snap), default=default)


# --------------------------------------------------------------------------- #
# HTTP server
# --------------------------------------------------------------------------- #
def _make_handler(manager: EngineManager, default_sid: str | None):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _send(self, body: str, content_type: str = "text/html; charset=utf-8", code: int = 200):
            data = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):  # noqa: N802 (http.server API)
            path = urllib.parse.urlparse(self.path).path
            try:
                if path == "/":
                    if default_sid:
                        self.send_response(302)
                        self.send_header("Location", f"/s/{urllib.parse.quote(default_sid)}")
                        self.end_headers()
                    else:
                        self._send(page_list(manager.config))
                elif path == "/health":
                    self._send("ok", "text/plain; charset=utf-8")
                elif path == "/partial/list":
                    self._send(fragment_list(manager.config))
                elif path.startswith("/partial/s/"):
                    sid = urllib.parse.unquote(path[len("/partial/s/"):])
                    snap = manager.snapshot(sid)
                    self._send(fragment_dashboard(snap),
                               code=200 if snap is not None else 404)
                elif path.startswith("/api/s/"):
                    sid = urllib.parse.unquote(path[len("/api/s/"):])
                    snap = manager.snapshot(sid)
                    self._send(snapshot_json(snap), "application/json; charset=utf-8",
                               code=200 if snap is not None else 404)
                elif path.startswith("/s/"):
                    sid = urllib.parse.unquote(path[len("/s/"):])
                    self._send(page_dashboard(sid, manager.snapshot(sid)))
                else:
                    self._send("not found", "text/plain; charset=utf-8", 404)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as exc:  # pragma: no cover - defensive
                try:
                    self._send(f"error: {exc}", "text/plain; charset=utf-8", 500)
                except OSError:
                    pass

        def log_message(self, *args):  # silence request logging
            pass

    return Handler


def _lan_ip() -> str | None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        return None
    finally:
        sock.close()


def serve(host: str, port: int, config: Config, pricing: Pricing,
          session: str | None = None, enable_plan_limits: bool = True) -> int:
    manager = EngineManager(config, pricing, enable_plan_limits=enable_plan_limits)
    httpd = ThreadingHTTPServer((host, port), _make_handler(manager, session))
    httpd.daemon_threads = True

    print("pool-coder — web dashboard")
    print(f"  local : http://127.0.0.1:{port}/")
    if host in ("0.0.0.0", "::"):
        ip = _lan_ip()
        if ip:
            print(f"  phone : http://{ip}:{port}/   ← open this on your phone (same Wi-Fi)")
    else:
        print(f"  bound : http://{host}:{port}/")
    print("  read-only & unauthenticated — only expose on a trusted network.")
    print("  Ctrl+C to stop.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping…")
    finally:
        httpd.shutdown()
        manager.stop_all()
    return 0
