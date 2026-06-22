"""Textual application: session picker -> live dashboard.

The dashboard owns an ``Engine`` (reader thread). On every interval it pulls an
immutable ``Snapshot`` via ``engine.get_snapshot()`` and repaints the panels —
the UI never touches mutable state.
"""

from __future__ import annotations

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.screen import Screen
from textual.widgets import Footer, Header, OptionList, Static
from textual.widgets.option_list import Option
from rich.text import Text

from ..config import UI_REFRESH_INTERVAL, Config
from ..engine import Engine
from ..format import fmt_age, fmt_pct
from ..overview import peek_session
from ..paths import SessionInfo, list_sessions
from ..pricing import Pricing
from . import render


class PickerScreen(Screen):
    BINDINGS = [
        ("enter", "select", "Open"),
        ("r", "reload", "Reload"),
        ("a", "toggle_all", "All/Active"),
    ]

    def __init__(self, config: Config):
        super().__init__()
        self.config = config
        self.show_all = config.active_window_seconds > 10 ** 12
        self._by_id: dict[str, SessionInfo] = {}

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(id="picker-title")
        yield OptionList(id="sessions")
        yield Footer()

    def on_mount(self) -> None:
        self.action_reload()

    def _format(self, info: SessionInfo) -> Text:
        ov = peek_session(info, self.config)
        line = Text(no_wrap=True, overflow="ellipsis")
        line.append(" ● " if ov.is_live else "   ", style="bold green")
        line.append(f"{fmt_age(info.age_seconds()):>10}  ", style="grey62")
        line.append(f"{fmt_pct(ov.occupancy):>5}  ", style="cyan")
        line.append(f"{(ov.model or '?'):<15.15} ", style="grey74")
        line.append(f"{ov.label:<22.22} ", style="bold")
        line.append(ov.last_text or "", style="grey50")
        return line

    def action_reload(self) -> None:
        option_list = self.query_one(OptionList)
        option_list.clear_options()
        self._by_id.clear()
        sessions = list_sessions()
        if not self.show_all:
            sessions = [s for s in sessions if s.age_seconds() <= self.config.active_window_seconds]
        scope = "all" if self.show_all else "active (30m)"
        self.query_one("#picker-title", Static).update(
            Text(f"Select a session to monitor — {len(sessions)} {scope}.  "
                 f"↑/↓ move · Enter open · a=all · r=reload · q=quit", style="bold")
        )
        for info in sessions[:80]:
            self._by_id[info.session_id] = info
            option_list.add_option(Option(self._format(info), id=info.session_id))
        if option_list.option_count:
            option_list.highlighted = 0
        option_list.focus()

    def action_toggle_all(self) -> None:
        self.show_all = not self.show_all
        self.action_reload()

    def action_select(self) -> None:
        option_list = self.query_one(OptionList)
        if option_list.highlighted is None:
            return
        option = option_list.get_option_at_index(option_list.highlighted)
        self._open(option.id)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self._open(event.option.id)

    def _open(self, session_id: str | None) -> None:
        info = self._by_id.get(session_id or "")
        if info is not None:
            self.app.switch_screen(DashboardScreen(info, self.config, self.app.pricing))


class DashboardScreen(Screen):
    BINDINGS = [
        ("b", "back", "Sessions"),
        ("p", "toggle_pause", "Pause"),
    ]

    def __init__(self, info: SessionInfo, config: Config, pricing: Pricing):
        super().__init__()
        self.info = info
        self.config = config
        self.pricing = pricing
        self.engine: Engine | None = None
        self.paused = False

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static(id="hdr")
        with VerticalScroll(id="body"):
            with Horizontal(classes="row"):
                yield Static(id="ctx", classes="cell")
                yield Static(id="tokens", classes="cell")
            with Horizontal(classes="row"):
                yield Static(id="activity", classes="cell")
                yield Static(id="plan", classes="cell")
            with Horizontal(classes="row"):
                yield Static(id="subagents", classes="cell")
                yield Static(id="workflows", classes="cell")
            yield Static(id="events")
        yield Footer()

    def on_mount(self) -> None:
        self.engine = Engine(self.info, self.config, self.pricing,
                             enable_plan_limits=self.config.plan_limits)
        self.engine.start()
        self.set_interval(UI_REFRESH_INTERVAL, self._tick)
        self._tick()

    def _tick(self) -> None:
        if self.paused or self.engine is None:
            return
        snap = self.engine.get_snapshot()
        if snap is None:
            return
        self.query_one("#hdr", Static).update(render.header_bar(snap))
        self.query_one("#ctx", Static).update(render.panel_context(snap))
        self.query_one("#tokens", Static).update(render.panel_tokens(snap))
        self.query_one("#activity", Static).update(render.panel_activity(snap))
        self.query_one("#plan", Static).update(render.panel_plan(snap))
        self.query_one("#subagents", Static).update(render.panel_subagents(snap))
        self.query_one("#workflows", Static).update(render.panel_workflows(snap))
        self.query_one("#events", Static).update(render.panel_events(snap))

    def action_toggle_pause(self) -> None:
        self.paused = not self.paused

    def action_back(self) -> None:
        self._shutdown()
        self.app.switch_screen(PickerScreen(self.config))

    def _shutdown(self) -> None:
        if self.engine is not None:
            self.engine.stop()
            self.engine = None

    def on_unmount(self) -> None:
        self._shutdown()


class PoolCoderApp(App):
    TITLE = "pool-coder"
    # App-level so they work on every screen; priority so a focused widget can't
    # swallow them. Override `help_quit` so Ctrl+C quits instead of just hinting.
    BINDINGS = [
        Binding("q", "quit", "Quit", priority=True),
        Binding("ctrl+c", "quit", "Quit", show=False, priority=True),
    ]
    CSS = """
    Screen { background: $surface; }
    #picker-title { padding: 1 2; }
    OptionList { height: 1fr; border: round $primary; }
    #hdr { height: auto; }
    .row { height: auto; }
    .cell { width: 1fr; height: auto; }
    #events { height: auto; }
    """

    def __init__(self, info: SessionInfo | None, config: Config, pricing: Pricing):
        super().__init__()
        self.start_info = info
        self.config = config
        self.pricing = pricing

    def on_mount(self) -> None:
        if self.start_info is not None:
            self.push_screen(DashboardScreen(self.start_info, self.config, self.pricing))
        else:
            self.push_screen(PickerScreen(self.config))

    def action_quit(self) -> None:
        self.exit()

    def action_help_quit(self) -> None:
        # Textual 8.x binds Ctrl+C to a "press X to quit" hint. We want it to
        # quit immediately.
        self.exit()


def run_app(info: SessionInfo | None, config: Config, pricing: Pricing) -> int:
    PoolCoderApp(info, config, pricing).run()
    return 0
