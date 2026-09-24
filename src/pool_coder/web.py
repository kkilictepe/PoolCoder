"""HTTP front-end: serve the dashboard as a mobile-friendly HTML page.

A second renderer over the same decoupled core. ``EngineManager`` lazily runs
one ``Engine`` per viewed session (idle-evicted, with active all-view batches
protected from the ordinary cache cap) and shares a single plan-limits poller.
The HTTP layer reads immutable snapshots and renders HTML — it never touches
mutable state. Stdlib only (matches ``usage-exporter``).
Sessions, previews and the plan poller come from the provider of
``config.agent`` (Claude Code or Codex); Codex differs only in the tokens and
plan cards.

Endpoints:
    GET /                 session list (auto-refreshing)
    GET /all              all active sessions in one dashboard
    GET /s/<id>           live dashboard page for a session
    GET /partial/list     list fragment (polled by the page)
    GET /partial/all      all-sessions fragment (polled by the page)
    GET /partial/s/<id>   dashboard fragment (polled by the page)
    GET /api/s/<id>       JSON snapshot
    GET /health           ok
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
import html
import json
import os
import socket
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .config import Config
from .engine import Engine
from .format import (
    fmt_age, fmt_clock, fmt_duration, fmt_int, fmt_pct, fmt_pct100, fmt_tokens, fmt_usd,
    limit_window, reset_in, short_id, short_model,
)
from .paths import SessionInfo
from .pricing import Pricing
from .providers import CLAUDE, get_provider
from .snapshot import PlanLimitsView, SessionSnapshot, Snapshot

esc = html.escape


# --------------------------------------------------------------------------- #
# Engine lifecycle
# --------------------------------------------------------------------------- #
def _transcript_gone(engine: Engine) -> bool:
    """One stat: has the engine's transcript been deleted? Only a definite
    "not found" counts (a transient Windows sharing error must not restart it)."""
    try:
        os.stat(engine.state.main_path)
    except FileNotFoundError:
        return True
    except OSError:
        pass
    return False


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
        self._active_order: list[str] = []
        self._lock = threading.Lock()
        self.provider = get_provider(config.agent)
        self.plan = (self.provider.make_plan_source()
                     if (enable_plan_limits and config.plan_limits) else None)
        if self.plan:
            self.plan.start()

    def _evict_locked(self, now: float, protected: set[str] | None = None) -> None:
        protected = protected or set()
        for sid in list(self._engines):
            if sid not in protected and now - self._last.get(sid, now) > self.idle_ttl:
                self._engines.pop(sid).stop()
                self._last.pop(sid, None)
        while len(self._engines) > self.max_engines:
            candidates = [sid for sid in self._engines if sid not in protected]
            if not candidates:
                break
            sid = min(candidates, key=self._last.get)
            self._engines.pop(sid).stop()
            self._last.pop(sid, None)

    def snapshot(self, session_id: str) -> Snapshot | None:
        now = time.monotonic()
        # one engine per session however the URL spells it (Codex ids match
        # case-insensitively; Claude ids exactly, as before)
        session_id = self.provider.normalize_id(session_id)
        with self._lock:
            self._evict_locked(now)
            engine = self._engines.get(session_id)
            if engine is not None:
                self._last[session_id] = now
        if engine is not None and _transcript_gone(engine):
            # Deleted: stop serving it (a 404 below, as before the reuse; a
            # moved transcript is simply found again).
            with self._lock:
                ours = self._engines.get(session_id) is engine  # not replaced meanwhile
                if ours:
                    del self._engines[session_id]
                    self._last.pop(session_id, None)
            if ours:
                engine.stop()
            engine = None
        if engine is None:
            # Only a session without a running engine is looked up: the page
            # polls every 1.5 s, and a Codex lookup globs the sessions tree.
            info = self.provider.find_session(session_id)
            if info is None:
                return None
            with self._lock:
                engine = self._engines.get(session_id)  # another request may have won
                if engine is None:
                    engine = Engine(info, self.config, self.pricing, enable_plan_limits=False)
                    engine.start()
                    self._engines[session_id] = engine
                self._last[session_id] = now
        snap = engine.get_snapshot()
        if snap is not None and self.plan is not None:
            snap = dataclasses.replace(snap, plan_limits=self.plan.view)
        return snap

    def snapshots(self, sessions: list[SessionInfo]) -> list[Snapshot]:
        """Snapshots for one discovered batch, preserving its order.

        Every requested id is protected from the ordinary LRU cap for this
        pass.  The all-sessions page may therefore monitor more than
        ``max_engines`` sessions, while engines that fall out of its active
        window return to the normal idle/cap eviction rules.
        """
        now = time.monotonic()
        requested: list[tuple[str, SessionInfo]] = []
        seen: set[str] = set()
        for info in sessions:
            sid = self.provider.normalize_id(info.session_id)
            if sid in seen:
                continue
            seen.add(sid)
            requested.append((sid, info))

        engines: list[Engine] = []
        with self._lock:
            self._evict_locked(now, seen)
            for sid, info in requested:
                engine = self._engines.get(sid)
                if engine is not None and _transcript_gone(engine):
                    self._engines.pop(sid, None)
                    self._last.pop(sid, None)
                    engine.stop()
                    engine = None
                if engine is None:
                    engine = Engine(info, self.config, self.pricing, enable_plan_limits=False)
                    engine.start()
                    self._engines[sid] = engine
                self._last[sid] = now
                engines.append(engine)

        out: list[Snapshot] = []
        for engine in engines:
            snap = engine.get_snapshot()
            if snap is not None:
                if self.plan is not None:
                    snap = dataclasses.replace(snap, plan_limits=self.plan.view)
                out.append(snap)
        return out

    def active_snapshots(self) -> list[Snapshot]:
        """All provider sessions inside the active window, in stable order.

        Discovery is newest-first only for the initial placement. Existing
        sessions keep their relative positions on later polls, while newly
        discovered sessions append to the right.
        """
        sessions = [
            info for info in self.provider.list_sessions()
            if info.age_seconds() <= self.config.active_window_seconds
        ]
        sessions.sort(key=lambda info: info.mtime, reverse=True)
        by_id: dict[str, SessionInfo] = {}
        for info in sessions:
            by_id.setdefault(self.provider.normalize_id(info.session_id), info)
        with self._lock:
            self._active_order = [sid for sid in self._active_order if sid in by_id]
            known = set(self._active_order)
            self._active_order.extend(sid for sid in by_id if sid not in known)
            ordered = [by_id[sid] for sid in self._active_order]
        return self.snapshots(ordered)

    def stop_all(self) -> None:
        with self._lock:
            for engine in self._engines.values():
                engine.stop()
            self._engines.clear()
            self._last.clear()
            self._active_order.clear()
            if self.plan:
                self.plan.stop()


# --------------------------------------------------------------------------- #
# HTML rendering
# --------------------------------------------------------------------------- #
_CSS = """
:root{color-scheme:dark;}
*{box-sizing:border-box;}
[hidden]{display:none!important;}
body{margin:0;background:#0d1117;color:#c9d1d9;
 font:14px/1.5 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;}
a{color:#58a6ff;text-decoration:none;} a:active{opacity:.6;}
button{font:inherit;}
.wrap{max-width:1100px;margin:0 auto;padding:10px;}
.wrap.multi-wrap{max-width:none;}
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
.btn{border:1px solid #30363d;border-radius:6px;background:#21262d;color:#c9d1d9;
 cursor:pointer;padding:4px 9px;line-height:1.4;}
.btn:hover{background:#30363d;color:#e6edf3;}
.btn:focus-visible,.tab:focus-visible{outline:2px solid #58a6ff;outline-offset:2px;}
.list-action{margin-left:auto;}
.multi-toolbar .r1{display:flex;flex-wrap:wrap;gap:8px;align-items:center;}
.hidden-tray{display:flex;gap:6px;align-items:center;flex-wrap:wrap;margin-top:7px;}
.hidden-tray .btn{font-size:12px;padding:2px 7px;}
.multi-columns{display:grid;grid-auto-flow:column;
 grid-auto-columns:minmax(min(320px,calc(100vw - 40px)),1fr);gap:10px;
 align-items:start;overflow-x:auto;overscroll-behavior-inline:contain;padding:10px 1px 8px;}
.session-column{min-width:0;scroll-snap-align:start;}
.session-head{background:#161b22;border:1px solid #21262d;border-radius:10px;padding:9px 10px;}
.session-head .r1{display:flex;flex-wrap:wrap;gap:6px;align-items:center;}
.session-head .session-action{margin-left:auto;}
.session-stack{display:grid;gap:10px;margin-top:10px;}
.all-hidden{margin:10px 0;padding:18px;border:1px dashed #30363d;border-radius:10px;text-align:center;}
.activity-card{margin-top:10px;}
.tabs{display:flex;gap:5px;overflow-x:auto;padding:2px 1px 8px;}
.tab{flex:0 0 auto;max-width:260px;border:1px solid #30363d;border-radius:7px;
 background:#0d1117;color:#8b949e;cursor:pointer;padding:5px 9px;white-space:nowrap;
 overflow:hidden;text-overflow:ellipsis;}
.tab[aria-selected=true]{background:#1f2937;border-color:#58a6ff;color:#e6edf3;}
.tab-panel{min-height:24px;}
.log .session-tag{flex:0 0 auto;max-width:220px;overflow:hidden;text-overflow:ellipsis;
 color:#d29922;font-weight:700;}
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
@media(max-width:600px){
 .wrap.multi-wrap{padding-inline:8px;}
 .multi-columns{grid-auto-columns:calc(100vw - 18px);scroll-snap-type:x proximity;}
 .log .session-tag{max-width:110px;}
}
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

_MULTI_JS = """
(function(){
 var dash=document.getElementById('dash'), ts=document.getElementById('ts');
 var storageKey=__STORE__, activeTab='all', hidden=Object.create(null);
 var own=Object.prototype.hasOwnProperty;
 function isHidden(id){return own.call(hidden,id);}
 try{
  var saved=JSON.parse(localStorage.getItem(storageKey)||'[]');
  if(Array.isArray(saved))saved.forEach(function(id){if(typeof id==='string')hidden[id]=true;});
 }catch(e){}
 function save(){
  try{localStorage.setItem(storageKey,JSON.stringify(Object.keys(hidden)));}catch(e){}
 }
 function currentScroll(){
  var scroller=dash.querySelector('[data-session-scroller]');
  return scroller?scroller.scrollLeft:0;
 }
 function applyState(left){
  var ids=Object.create(null), visible=0, hiddenHere=0;
  dash.querySelectorAll('.session-column[data-session-id]').forEach(function(el){
   var id=el.getAttribute('data-session-id'), hide=isHidden(id);
   ids[id]=true;el.hidden=hide;
   if(hide)hiddenHere++;else visible++;
  });
  dash.querySelectorAll('[data-action=show][data-session-id]').forEach(function(el){
   el.hidden=!isHidden(el.getAttribute('data-session-id'));
  });
  var tray=dash.querySelector('[data-hidden-tray]');
  if(tray)tray.hidden=hiddenHere===0;
  var count=dash.querySelector('[data-visible-count]');
  if(count)count.textContent=visible+' shown';
  var allHidden=dash.querySelector('[data-all-hidden]');
  if(allHidden)allHidden.hidden=visible!==0;
  if(activeTab!=='all'&&(!ids[activeTab]||isHidden(activeTab)))activeTab='all';
  dash.querySelectorAll('[role=tab][data-tab-key]').forEach(function(el){
   var key=el.getAttribute('data-tab-key');
   el.hidden=key!=='all'&&isHidden(key);
   var selected=key===activeTab;
   el.setAttribute('aria-selected',selected?'true':'false');
   el.tabIndex=selected?0:-1;
  });
  dash.querySelectorAll('[role=tabpanel][data-panel-key]').forEach(function(el){
   var key=el.getAttribute('data-panel-key');
   el.hidden=key!==activeTab||(key!=='all'&&isHidden(key));
  });
  var visibleRows=0;
  dash.querySelectorAll('[data-panel-key=all] .e[data-session-id]').forEach(function(el){
   var hide=isHidden(el.getAttribute('data-session-id'));
   el.hidden=hide;if(!hide)visibleRows++;
  });
  var empty=dash.querySelector('[data-all-activity-empty]');
  if(empty)empty.hidden=visibleRows!==0;
  var scroller=dash.querySelector('[data-session-scroller]');
  if(scroller)scroller.scrollLeft=left||0;
 }
 dash.addEventListener('click',function(ev){
  var control=ev.target.closest('[data-action]');
  if(!control||!dash.contains(control))return;
  var action=control.getAttribute('data-action'), id=control.getAttribute('data-session-id');
  if(action==='hide'&&id){hidden[id]=true;save();}
  else if(action==='show'&&id){delete hidden[id];save();}
  else if(action==='show-all'){hidden=Object.create(null);save();}
  else if(action==='tab'){activeTab=control.getAttribute('data-tab-key')||'all';}
  else return;
  applyState(currentScroll());
 });
 dash.addEventListener('keydown',function(ev){
  var tab=ev.target.closest('[role=tab][data-tab-key]');
  if(!tab)return;
  var keys=['ArrowLeft','ArrowRight','Home','End'];
  if(keys.indexOf(ev.key)<0)return;
  var tabs=Array.prototype.filter.call(
   dash.querySelectorAll('[role=tab][data-tab-key]'),function(el){return !el.hidden;});
  var index=tabs.indexOf(tab), next=index;
  if(ev.key==='ArrowLeft')next=(index-1+tabs.length)%tabs.length;
  if(ev.key==='ArrowRight')next=(index+1)%tabs.length;
  if(ev.key==='Home')next=0;if(ev.key==='End')next=tabs.length-1;
  if(tabs[next]){ev.preventDefault();tabs[next].focus();tabs[next].click();}
 });
 function tick(){
  var left=currentScroll();
  fetch('__EP__',{cache:'no-store'}).then(function(r){if(!r.ok)throw 0;return r.text();})
   .then(function(t){dash.innerHTML=t;applyState(left);if(ts)ts.textContent=new Date().toLocaleTimeString();
     document.body.classList.remove('off');})
   .catch(function(){document.body.classList.add('off');});
 }
 applyState(0);
 setInterval(tick,__MS__);
})();
"""


def _page(title: str, inner: str, endpoint: str, ms: int = 1500,
          multi_agent: str | None = None) -> str:
    template = _MULTI_JS if multi_agent is not None else _JS
    js = template.replace("__EP__", endpoint).replace("__MS__", str(ms))
    if multi_agent is not None:
        js = js.replace("__STORE__", json.dumps(f"pool-coder:hidden:{multi_agent}"))
    wrap = "wrap multi-wrap" if multi_agent is not None else "wrap"
    return (
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width,initial-scale=1'>"
        f"<title>{esc(title)}</title><style>{_CSS}</style></head><body>"
        f"<div class='{wrap}'><div id=dash>{inner}</div>"
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


def _header_body(snap: Snapshot, action: str = "") -> str:
    s = snap.session
    badge = ("<span class='badge live'>● live</span>" if s.is_live
             else f"<span class='badge idle'>idle {esc(fmt_age(s.idle_s))}</span>")
    mode = f"<span class=mode>[{esc(s.mode)}]</span>" if s.mode and s.mode != "normal" else ""
    loading = "<span class=mode>⏳ loading…</span>" if snap.loading else ""
    prompt = esc(s.last_prompt) if s.last_prompt else "<span class=dim>(no prompt yet)</span>"
    return (
        f"<div class=r1>{badge}{loading}<span class=proj>{esc(s.cwd or s.project_hash)}</span>"
        f"<span class=dim>({esc(s.git_branch or 'no branch')})</span>"
        f"<span class=model>{esc(s.model or '?')}</span>{mode}{action}</div>"
        f"<div class=dim>{esc(fmt_duration(s.duration_s))} · {s.turns} turns · {s.user_messages} prompts</div>"
        f"<div class=prompt>» {prompt}</div>"
    )


def _header(snap: Snapshot) -> str:
    return f"<div class='topbar hdr'>{_header_body(snap)}</div>"


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
    cost = (f"<div class=big style='color:#3fb950'>{esc(fmt_usd(s.cost.total_usd))}</div>"
            f"<div class=dim>{esc(fmt_usd(s.cost.per_min_usd))}/min · {fmt_tokens(s.tokens_per_min)}/min</div>")
    if s.agent == "codex":
        return _card("Tokens & cost", cost + _codex_model_costs(s) + _kv(_codex_token_rows(s)))
    rows = [
        ("input", fmt_tokens(c.input)),
        ("cache read", fmt_tokens(c.cache_read)),
        ("cache write", fmt_tokens(c.cache_creation)),
        ("output", fmt_tokens(c.output)),
        ("cache hit", fmt_pct(s.cache_hit_ratio)),
    ]
    return _card("Tokens & cost", cost + _kv(rows))


def _codex_token_rows(s: SessionSnapshot) -> list[tuple[str, str]]:
    # Codex: "input" is the non-cached part; reasoning is already inside output.
    c = s.cumulative
    rows = [("input", fmt_tokens(c.input)), ("cached", fmt_tokens(c.cache_read))]
    if c.cache_creation > 0:
        rows.append(("cache write", fmt_tokens(c.cache_creation)))
    rows += [
        ("output", fmt_tokens(c.output)),
        ("reasoning", fmt_tokens(c.reasoning)),
        ("cache hit", fmt_pct(s.cache_hit_ratio)),
    ]
    return rows


def _codex_model_costs(s: SessionSnapshot) -> str:
    # e.g. the main model plus the guardian reviewer (priced at the [gpt] fallback)
    if len(s.cost.by_model) <= 1:
        return ""
    return "<div class=dim>" + " ".join(
        f"{esc(short_model(m))}:{esc(fmt_usd(usd))}" for m, usd in s.cost.by_model[:3]
    ) + "</div>"


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
    if snap.session.agent == "codex":
        return _card_plan_codex(pl)
    rows = [
        ("5-hour", f"{esc(fmt_pct100(pl.five_hour_pct))} · resets {esc(reset_in(pl.five_hour_resets_at))}"),
        ("weekly", esc(fmt_pct100(pl.seven_day_pct))),
        ("wk opus", esc(fmt_pct100(pl.seven_day_opus_pct))),
        ("wk sonnet", esc(fmt_pct100(pl.seven_day_sonnet_pct))),
    ]
    return _card("Plan limits", _bar((pl.five_hour_pct or 0) / 100.0) + _kv(rows))


def _plan_windows(pl: PlanLimitsView) -> list[tuple[str, float | None, _dt.datetime | None]]:
    """Codex rate-limit windows that exist: ``(label, pct, resets_at)``."""
    return [
        (label, pct, resets)
        for label, pct, resets in ((pl.five_hour_label, pl.five_hour_pct, pl.five_hour_resets_at),
                                   (pl.seven_day_label, pl.seven_day_pct, pl.seven_day_resets_at))
        if pct is not None or resets is not None
    ]


def _card_plan_codex(pl: PlanLimitsView) -> str:
    # Codex limits come from local records: labelled windows, the plan and their age.
    rows = []
    pct = None  # the bar: the first window with a live percentage (5-hour slot first)
    for label, window_pct, resets in _plan_windows(pl):
        value, reset, live = limit_window(window_pct, resets)  # a passed reset: no stale %
        rows.append((label, esc(value) + (f" · {esc(reset)}" if reset else "")))
        pct = live if pct is None else pct
    plan = " · ".join(x for x in (pl.plan_type, pl.note) if x)
    if plan:
        rows.append(("plan", esc(plan)))
    if pl.as_of is not None:
        age = (_dt.datetime.now(_dt.timezone.utc) - pl.as_of).total_seconds()
        rows.append(("as of", f"{esc(pl.as_of.astimezone().strftime('%H:%M'))} "
                              f"({esc(fmt_age(age))})"))
    if not rows:
        return _card("Plan limits", "<div class=dim>no limit data</div>")
    return _card("Plan limits", (_bar(pct / 100.0) if pct is not None else "") + _kv(rows))


def _card_events(snap: Snapshot) -> str:
    s = snap.session
    rows = [_event_row(snap, ev) for ev in list(s.events)[-22:]]
    body = "".join(rows) if rows else "<div class=dim>waiting for activity…</div>"
    return _card("Recent activity", f"<div class=log>{body}</div>", wide=True)


def _session_label(s: SessionSnapshot) -> str:
    """Short, readable and unique-enough label for tabs/activity rows."""
    title = (s.title or "").strip()
    cwd = (s.cwd or "").rstrip("/\\")
    project = cwd.replace("\\", "/").rsplit("/", 1)[-1] if cwd else s.project_hash
    return f"{title or project or 'session'} · {short_id(s.session_id, s.agent)}"


def _event_row(snap: Snapshot, ev, show_session: bool = False) -> str:
    s = snap.session
    t = fmt_clock(ev.at) if ev.at else "--:--:--"
    sid = esc(s.session_id, quote=True)
    session = ""
    if show_session:
        label = _session_label(s)
        session = (f"<span class=session-tag title='{esc(label, quote=True)}'>"
                   f"{esc(label)}</span>")
    return (f"<div class=e data-session-id='{sid}'><span class=t>{esc(t)}</span>{session}"
            f"<span class='k-{esc(ev.kind, quote=True)}'>{esc(ev.text[:140])}</span></div>")


def _event_timestamp(at: _dt.datetime | None) -> float:
    if at is None:
        return float("-inf")
    if at.tzinfo is None:
        at = at.replace(tzinfo=_dt.timezone.utc)
    try:
        return at.timestamp()
    except (OSError, OverflowError, ValueError):
        return float("-inf")


def _card_activity_tabs(snapshots: list[Snapshot]) -> str:
    """One tabbed activity card for the all-sessions dashboard."""
    tabs = [
        "<button type=button class=tab role=tab id=activity-tab-all "
        "aria-controls=activity-panel-all aria-selected=true data-action=tab "
        "data-tab-key=all>All</button>"
    ]
    panels = []
    merged = []
    recent_by_session = []
    for snap_index, snap in enumerate(snapshots):
        recent = list(snap.session.events)[-50:]
        recent_by_session.append(recent)
        for event_index, ev in enumerate(recent):
            merged.append((_event_timestamp(ev.at), snap_index, event_index, snap, ev))
    merged.sort(key=lambda item: item[:3])
    all_rows = "".join(_event_row(snap, ev, show_session=True)
                       for _, _, _, snap, ev in merged)
    panels.append(
        "<div class=tab-panel role=tabpanel id=activity-panel-all "
        "aria-labelledby=activity-tab-all data-panel-key=all>"
        f"<div class=log>{all_rows}<div class=dim data-all-activity-empty"
        f"{' hidden' if all_rows else ''}>waiting for activity…</div></div></div>"
    )
    for index, (snap, events) in enumerate(zip(snapshots, recent_by_session), start=1):
        s = snap.session
        sid = esc(s.session_id, quote=True)
        label = _session_label(s)
        tabs.append(
            f"<button type=button class=tab role=tab id=activity-tab-{index} "
            f"aria-controls=activity-panel-{index} aria-selected=false data-action=tab "
            f"data-tab-key='{sid}' title='{esc(label, quote=True)}'>{esc(label)}</button>"
        )
        rows = "".join(_event_row(snap, ev) for ev in events)
        if not rows:
            rows = "<div class=dim>waiting for activity…</div>"
        panels.append(
            f"<div class=tab-panel role=tabpanel id=activity-panel-{index} "
            f"aria-labelledby=activity-tab-{index} data-panel-key='{sid}' hidden>"
            f"<div class=log>{rows}</div></div>"
        )
    inner = f"<div class=tabs role=tablist aria-label='Session activity'>{''.join(tabs)}</div>{''.join(panels)}"
    return f"<div class=activity-card>{_card('Recent activity', inner, wide=True)}</div>"


def _session_column(snap: Snapshot) -> str:
    s = snap.session
    sid = esc(s.session_id, quote=True)
    label = _session_label(s)
    action = (
        f"<button type=button class='btn session-action' data-action=hide "
        f"data-session-id='{sid}' aria-label='Hide {esc(label, quote=True)}'>Hide</button>"
    )
    panels = "".join([
        _card_context(snap), _card_tokens(snap), _card_activity(snap),
        _card_subagents(snap), _card_workflows(snap), _card_plan(snap),
    ])
    return (
        f"<article class=session-column data-session-id='{sid}'>"
        f"<div class='session-head hdr'>{_header_body(snap, action)}</div>"
        f"<div class=session-stack>{panels}</div></article>"
    )


def fragment_all(snapshots: list[Snapshot], config: Config) -> str:
    prov = get_provider(config.agent)
    count = len(snapshots)
    restore = "".join(
        f"<button type=button class=btn data-action=show data-session-id='"
        f"{esc(snap.session.session_id, quote=True)}' hidden>Show {esc(_session_label(snap.session))}</button>"
        for snap in snapshots
    )
    tray = (
        "<div class=hidden-tray data-hidden-tray hidden><span class=dim>Hidden:</span>"
        f"{restore}<button type=button class=btn data-action=show-all>Show all</button></div>"
    )
    toolbar = (
        "<div class='topbar multi-toolbar'><div class=r1><span class=proj>pool-coder</span>"
        f"<span class=dim>all active {esc(prov.label)} sessions · {count} matched · "
        f"<span data-visible-count>{count} shown</span></span>"
        "<a class='btn list-action' href='/'>Session list</a></div>"
        f"{tray}</div>"
    )
    if not snapshots:
        return (toolbar + "<div class=all-hidden>No sessions in the configured active window. "
                "The dashboard will update automatically.</div>" + _card_activity_tabs([]))
    columns = "".join(_session_column(snap) for snap in snapshots)
    hidden_empty = (
        "<div class=all-hidden data-all-hidden hidden>All matching sessions are hidden. "
        "Use a Show button above to restore one.</div>"
    )
    return (toolbar + hidden_empty
            + f"<div class=multi-columns data-session-scroller>{columns}</div>"
            + _card_activity_tabs(snapshots))


def fragment_dashboard(snap: Snapshot | None) -> str:
    if snap is None:
        return ("<div class='topbar hdr'><div class=r1><span class=mode>session not found</span>"
                "<a href='/'>← back to sessions</a></div></div>")
    panels = "".join([
        _card_context(snap), _card_tokens(snap), _card_activity(snap),
        _card_subagents(snap), _card_workflows(snap), _card_plan(snap), _card_events(snap),
    ])
    return _header(snap) + f"<div class=grid>{panels}</div>"


def page_dashboard(session_id: str, snap: Snapshot | None, agent: str | None = None) -> str:
    agent = agent or (snap.session.agent if snap is not None else "claude")
    title = f"pool-coder · {short_id(session_id, agent)}"
    return _page(title, fragment_dashboard(snap), f"/partial/s/{urllib.parse.quote(session_id)}")


def page_all(snapshots: list[Snapshot], config: Config) -> str:
    prov = get_provider(config.agent)
    title = f"pool-coder · all {prov.label} sessions"
    return _page(title, fragment_all(snapshots, config), "/partial/all",
                 multi_agent=prov.name)


def fragment_list(config: Config) -> str:
    prov = get_provider(config.agent)
    sessions = [s for s in prov.list_sessions()
                if s.age_seconds() <= config.active_window_seconds][:40]
    header = ("<div class='topbar hdr'><div class=r1><span class=proj>pool-coder</span>"
              f"<span class=dim>active {esc(prov.label)} sessions — tap to monitor</span>"
              "<a class='btn list-action' href='/all'>Monitor all</a></div></div>")
    if not sessions:
        agent = "" if prov is CLAUDE else f"{esc(prov.label)} "
        return (header + f"<div class=dim style='padding:20px'>"
                f"No active {agent}sessions in the last 30 min.</div>")
    rows = []
    for info in sessions:
        ov = prov.peek_session(info, config)
        dot = "<span class='badge live'>●</span>" if ov.is_live else "<span class=dim>○</span>"
        rows.append(
            f"<a class=row href='/s/{urllib.parse.quote(info.session_id)}'>{dot}"
            f"<span class=dim>{esc(fmt_age(info.age_seconds()))}</span>"
            f"<span class=pct>{esc(fmt_pct(ov.occupancy))}</span>"
            f"<span class=name>{esc(ov.label)}</span>"
            f"<span class=last>{esc(ov.last_shown[:60])}</span></a>"
        )
    return header + f"<div class=list style='margin-top:10px'>{''.join(rows)}</div>"


def page_list(config: Config) -> str:
    prov = get_provider(config.agent)
    title = "pool-coder" if prov is CLAUDE else f"pool-coder · {prov.label}"
    return _page(title, fragment_list(config), "/partial/list", ms=3000)


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
                elif path == "/partial/all":
                    self._send(fragment_all(manager.active_snapshots(), manager.config))
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
                    self._send(page_dashboard(sid, manager.snapshot(sid), manager.provider.name))
                elif path == "/all":
                    self._send(page_all(manager.active_snapshots(), manager.config))
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

    agent = "" if manager.provider is CLAUDE else f" ({manager.provider.label})"
    print(f"pool-coder — web dashboard{agent}")
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
