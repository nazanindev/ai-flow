"""
Textual TUI for AI Flow — the control room.

FlowOrchestrator logic (sessions, workers, pipeline) lives in repl.py unchanged.
This module owns the terminal: split panes per session, live header, input footer.
"""
from __future__ import annotations

import json
import threading
import time
from typing import TYPE_CHECKING, Optional

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, ScrollableContainer
from textual.css.query import NoMatches
from textual.reactive import reactive
from textual.screen import Screen
from textual.widgets import Footer, Header, Input, Label, RichLog, Static, TextArea

if TYPE_CHECKING:
    from flow.repl import AgentSession, FlowOrchestrator


# ── Per-session pane ──────────────────────────────────────────────────────────

class SessionPane(Vertical):
    """One scrollable output pane per AgentSession."""

    DEFAULT_CSS = """
    SessionPane {
        border: solid $surface-lighten-1;
        min-width: 30;
    }
    SessionPane:focus-within {
        border: solid $accent;
    }
    SessionPane > .pane-title {
        background: $surface-lighten-1;
        padding: 0 1;
        height: 1;
    }
    SessionPane > .pane-activity {
        background: $surface;
        padding: 0 1;
        height: 1;
        color: $text-muted;
    }
    SessionPane > RichLog {
        height: 1fr;
        scrollbar-gutter: stable;
    }
    """

    def __init__(self, session: "AgentSession") -> None:
        super().__init__(id=f"pane-{session.idx}")
        self.session = session
        self._line_buf = ""

    def compose(self) -> ComposeResult:
        type_labels = {"executor": "ex", "planner": "pl", "reviewer": "rv"}
        tag = type_labels.get(self.session.session_type, self.session.session_type[:2])
        yield Label(
            f"[bold][{self.session.idx}][/bold] {tag} · {self.session.goal[:35]}",
            classes="pane-title",
        )
        yield Label("", classes="pane-activity")
        yield RichLog(
            id=f"log-{self.session.idx}",
            highlight=False,
            markup=False,
            wrap=True,
        )

    def append(self, text: str) -> None:
        try:
            log = self.query_one(f"#log-{self.session.idx}", RichLog)
            self._line_buf += text
            while "\n" in self._line_buf:
                line, self._line_buf = self._line_buf.split("\n", 1)
                log.write(line)
        except NoMatches:
            pass

    @staticmethod
    def _fmt_elapsed(seconds: float) -> str:
        m = int(seconds // 60)
        s = int(seconds % 60)
        return f"{m}m{s:02d}s"

    def refresh_title(self) -> None:
        try:
            label = self.query_one(".pane-title", Label)
            type_labels = {"executor": "ex", "planner": "pl", "reviewer": "rv"}
            tag = type_labels.get(self.session.session_type, self.session.session_type[:2])
            with self.session.lock:
                st = self.session.status
                waiting = self.session.waiting_for_input
                run = self.session.run
            phase = run.phase.value if run and st == "running" else st

            elapsed_str = ""
            if run and st == "running" and run.phase_started_at:
                try:
                    from datetime import datetime, timezone
                    started = datetime.fromisoformat(run.phase_started_at)
                    if started.tzinfo is None:
                        started = started.replace(tzinfo=timezone.utc)
                    elapsed = (datetime.now(timezone.utc) - started).total_seconds()
                    elapsed_str = f" {self._fmt_elapsed(elapsed)}"
                except Exception:
                    pass

            if waiting:
                icon = "?"
            elif st == "running":
                icon = "●"
            elif st == "done":
                icon = "✓"
            else:
                icon = "✗"
            branch = self.session.branch
            branch_short = branch[-16:] if len(branch) > 16 else branch
            label.update(
                f"[bold][{self.session.idx}][/bold] {icon} {tag}:{phase[:4]}{elapsed_str} "
                f"[dim]{branch_short}[/dim] · {self.session.goal[:22]}"
            )
        except NoMatches:
            pass

    def refresh_activity(self) -> None:
        try:
            bar = self.query_one(".pane-activity", Label)
            with self.session.lock:
                st = self.session.status
                run = self.session.run

            if st not in ("running",) or not run:
                bar.update("")
                return

            run_id = run.run_id
            try:
                from flow.tracker import activity_path
                path = activity_path(run_id)
                if not path.exists():
                    bar.update("  waiting for first tool call…")
                    return
                data = json.loads(path.read_text(encoding="utf-8"))
                tool = data.get("tool", "?")
                ts = float(data.get("ts", 0))
                age_s = time.time() - ts
                if age_s < 15:
                    bar.update(f"  ⚡ {tool}  ({int(age_s)}s ago)")
                elif age_s < 90:
                    bar.update(f"  💭 thinking…  ({int(age_s)}s)")
                else:
                    bar.update(f"  ⏸ idle  ({self._fmt_elapsed(age_s)})")
            except Exception:
                bar.update("")
        except NoMatches:
            pass


# ── Submit-on-enter TextArea ──────────────────────────────────────────────────

class SubmitArea(TextArea):
    """TextArea that submits on Enter so paste works naturally.

    Replaces Input in the input bar. Paste lands multi-line, Enter sends it.
    """

    class Submitted(TextArea.Changed):
        """Posted when the user presses Enter to submit."""
        def __init__(self, area: "SubmitArea", value: str) -> None:
            super().__init__(area, area.text)
            self.value = value

    BINDINGS = [
        Binding("enter", "submit_text", "Submit", show=False),
    ]

    def action_submit_text(self) -> None:
        text = self.text.strip()
        if text:
            self.post_message(self.Submitted(self, text))
            self.load_text("")


# ── Drill-down screen ─────────────────────────────────────────────────────────

class DrillDownScreen(Screen):
    """Full-screen view of one session's output + interactive input."""

    BINDINGS = [
        Binding("escape", "pop_screen", "Back"),
    ]

    DEFAULT_CSS = """
    DrillDownScreen {
        layout: vertical;
    }
    DrillDownScreen > .drill-header {
        background: $surface-lighten-2;
        padding: 0 1;
        height: 1;
        color: $accent;
    }
    DrillDownScreen > RichLog {
        height: 1fr;
    }
    DrillDownScreen > SubmitArea {
        dock: bottom;
        height: 3;
        border: none;
        background: $surface;
        padding: 0 1;
    }
    """

    def __init__(self, session: "AgentSession", orchestrator: "FlowOrchestrator") -> None:
        super().__init__()
        self.session = session
        self.orchestrator = orchestrator
        self._drain_stop = threading.Event()

    def compose(self) -> ComposeResult:
        type_labels = {"executor": "exec", "planner": "plan", "reviewer": "rev"}
        tag = type_labels.get(self.session.session_type, self.session.session_type)
        yield Label(
            f"── Session {self.session.idx}: {self.session.goal} [{tag}] "
            f"── Esc to return ──",
            classes="drill-header",
        )
        yield RichLog(id="drill-log", highlight=False, markup=False, wrap=True)
        yield SubmitArea(show_line_numbers=False, language=None, id="drill-input")

    def _start_drain(self) -> None:
        def _drain():
            log_widget = self.query_one("#drill-log", RichLog)
            buf = self._drill_buf
            while not self._drain_stop.is_set():
                try:
                    while True:
                        chunk = self.session.output_queue.get_nowait()
                        self.session.output_history.append(chunk)
                        with self.session.lock:
                            stripped = chunk.strip()
                            if stripped:
                                self.session.last_line = stripped[-100:]
                        buf += chunk
                        while "\n" in buf:
                            line, buf = buf.split("\n", 1)
                            self.call_from_thread(log_widget.write, line)
                except Exception:
                    if self._drain_stop.is_set():
                        break
                    time.sleep(0.05)
        t = threading.Thread(target=_drain, daemon=True)
        t.start()

    def on_unmount(self) -> None:
        self._drain_stop.set()

    def on_mount(self) -> None:
        log = self.query_one("#drill-log", RichLog)
        buf = ""
        for chunk in self.session.output_history:
            buf += chunk
            while "\n" in buf:
                line, buf = buf.split("\n", 1)
                log.write(line)
        self._drill_buf = buf
        self._start_drain()
        self.query_one("#drill-input", SubmitArea).focus()

    def action_pop_screen(self) -> None:
        self.app.pop_screen()

    @on(SubmitArea.Submitted, "#drill-input")
    def on_drill_input(self, event: SubmitArea.Submitted) -> None:
        text = event.value.strip()
        if not text or text in ("/back", "b"):
            self.app.pop_screen()
            return
        if text.startswith("/stop"):
            self.orchestrator._stop_session(self.session.idx)
            self.query_one("#drill-log", RichLog).write("→ stop signal sent\n")
            return
        if text.startswith("/") and not text.startswith("/prompt "):
            self.query_one("#drill-log", RichLog).write(
                "drill-down: /prompt <msg> to inject · /stop · /back to exit\n"
            )
            return
        msg = text[8:].strip() if text.startswith("/prompt ") else text
        if msg:
            self.session.inject_queue.put(msg)
            self.query_one("#drill-log", RichLog).write(f"→ [queued] {msg[:80]}{'…' if len(msg) > 80 else ''}\n")


# ── Status header ─────────────────────────────────────────────────────────────

class FlowHeader(Static):
    DEFAULT_CSS = """
    FlowHeader {
        background: $surface-lighten-1;
        padding: 0 1;
        height: 1;
        color: $text-muted;
    }
    """

    def update_status(
        self, project: str, branch: str, api_spend: float, sub_tokens: int, sessions: list
    ) -> None:
        running = sum(1 for s in sessions if s.status == "running")
        done = sum(1 for s in sessions if s.status == "done")
        failed = sum(1 for s in sessions if s.status == "failed")
        counts = f"↻{running} ✓{done}" + (f" ✗{failed}" if failed else "")
        cost_parts = []
        if api_spend > 0:
            cost_parts.append(f"${api_spend:.4f} api")
        if sub_tokens > 0:
            cost_parts.append(f"{sub_tokens // 1000}k tok")
        cost_str = "  │  " + "  ".join(cost_parts) if cost_parts else ""
        self.update(
            f"[bold cyan]flow[/bold cyan] · {project} ({branch})"
            + cost_str
            + (f"  │  {counts}" if sessions else "")
        )


# ── Session grid ──────────────────────────────────────────────────────────────

class SessionGrid(Horizontal):
    DEFAULT_CSS = """
    SessionGrid {
        height: 1fr;
    }
    """


# ── Empty state ───────────────────────────────────────────────────────────────

class EmptyState(Static):
    DEFAULT_CSS = """
    EmptyState {
        height: 1fr;
        content-align: center middle;
        color: $text-muted;
    }
    """

    def on_mount(self) -> None:
        self.update(
            "No sessions yet.\n\n"
            "Type a task to start.\n"
            "  plan: <question>   — interactive planner (opus)\n"
            "  review: <branch>   — one-shot code review (haiku)\n"
            "  /dismiss N         — remove a done/failed session\n"
            "  /help              — all commands"
        )


# ── Main App ──────────────────────────────────────────────────────────────────

class FlowApp(App):
    """The Flow control room."""

    CSS = """
    Screen {
        layout: vertical;
    }
    #input-bar {
        dock: bottom;
        height: 3;
        padding: 0 1;
        background: $surface;
    }
    #input-bar SubmitArea {
        border: none;
        background: $surface;
        padding: 0;
    }
    """

    BINDINGS = [
        Binding("ctrl+q", "quit_flow", "Quit", show=True),
    ]

    def __init__(self, orchestrator: "FlowOrchestrator") -> None:
        super().__init__()
        self.orchestrator = orchestrator
        self._refresh_timer = None
        self._notified_waiting: set = set()  # session idx already notified

    def compose(self) -> ComposeResult:
        yield FlowHeader(id="flow-header")
        yield EmptyState(id="empty-state")
        yield SessionGrid(id="session-grid")
        with Vertical(id="input-bar"):
            yield SubmitArea(show_line_numbers=False, language=None, id="main-input")

    def on_mount(self) -> None:
        self.query_one("#session-grid").display = False
        self._refresh_timer = self.set_interval(0.25, self._tick)
        self.query_one("#main-input", SubmitArea).focus()

        # Hook health warning
        from flow.commands.doctor import hook_health_ok, hook_health_one_liner
        if not hook_health_ok():
            self.notify(
                hook_health_one_liner() + " — run flow doctor --fix",
                title="Hook issue",
                severity="error",
                timeout=10,
            )

    # ── Tick: drain queues, refresh pane titles and header ────────────────────

    def _tick(self) -> None:
        orchestrator = self.orchestrator

        # Refresh API spend + subscription token cache
        now = time.monotonic()
        if now - orchestrator._api_spend_last_refresh > 5.0:
            try:
                from flow.tracker import get_api_spend_today, get_subscription_tokens_today
                orchestrator._api_spend_cache = get_api_spend_today(orchestrator.project)
                tok = get_subscription_tokens_today(orchestrator.project)
                orchestrator._sub_tokens_cache = tok["tokens_in"] + tok["tokens_out"]
            except Exception:
                pass
            orchestrator._api_spend_last_refresh = now

        # Update header
        try:
            header = self.query_one("#flow-header", FlowHeader)
            header.update_status(
                orchestrator.project,
                orchestrator.branch,
                orchestrator._api_spend_cache,
                getattr(orchestrator, "_sub_tokens_cache", 0),
                orchestrator.sessions,
            )
        except NoMatches:
            pass

        # Auto-create panes for sessions spawned by workers (e.g. auto-reviewer)
        known_ids = {w.id for w in self.query(SessionPane)}
        for session in orchestrator.sessions:
            if f"pane-{session.idx}" not in known_ids:
                self.add_session_pane(session)

        # Drain output queues into panes
        for session in orchestrator.sessions:
            pane_id = f"pane-{session.idx}"
            try:
                pane = self.query_one(f"#{pane_id}", SessionPane)
            except NoMatches:
                continue
            chunks = []
            while True:
                try:
                    chunk = session.output_queue.get_nowait()
                    session.output_history.append(chunk)
                    with session.lock:
                        stripped = chunk.strip()
                        if stripped:
                            session.last_line = stripped[-100:]
                    chunks.append(chunk)
                except Exception:
                    break
            for chunk in chunks:
                pane.append(chunk)
            pane.refresh_title()
            pane.refresh_activity()

            # Notify once when a planner session starts waiting for input
            with session.lock:
                waiting = session.waiting_for_input
            if waiting and session.idx not in self._notified_waiting:
                self._notified_waiting.add(session.idx)
                self.notify(
                    f"/view {session.idx} to reply",
                    title=f"[{session.idx}] Planner waiting",
                    timeout=8,
                )
            elif not waiting:
                self._notified_waiting.discard(session.idx)

    # ── Add a new session pane when a session starts ──────────────────────────

    def add_session_pane(self, session: "AgentSession") -> None:
        grid = self.query_one("#session-grid", SessionGrid)
        empty = self.query_one("#empty-state")
        empty.display = False
        grid.display = True
        pane = SessionPane(session)
        grid.mount(pane)

    # ── Input handling ────────────────────────────────────────────────────────

    @on(SubmitArea.Submitted, "#main-input")
    def on_main_input(self, event: SubmitArea.Submitted) -> None:
        text = event.value.strip()
        if not text:
            return
        if text.startswith("/"):
            self._handle_slash(text)
        elif not self.orchestrator._try_dispatch_flow_cmd(text):
            session = self.orchestrator._start_session(text)
            self.add_session_pane(session)
            self.notify(
                f"Session {session.idx} started on {session.branch}",
                timeout=3,
            )

    def _handle_slash(self, cmd: str) -> None:
        parts = cmd.strip().split(None, 1)
        verb = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""

        if verb in ("/quit", "/exit", "/q"):
            self.action_quit_flow()
        elif verb == "/view":
            if not arg.isdigit():
                self.notify("Usage: /view N", severity="warning")
            else:
                idx = int(arg)
                sessions = self.orchestrator.sessions
                if 1 <= idx <= len(sessions):
                    self.push_screen(DrillDownScreen(sessions[idx - 1], self.orchestrator))
                else:
                    self.notify(f"No session {idx}", severity="warning")
        elif verb == "/stop":
            self.orchestrator._stop_session(int(arg) if arg.isdigit() else None)
            self.notify("Stop signal sent")
        elif verb == "/dismiss":
            if not arg.isdigit():
                self.notify("Usage: /dismiss N", severity="warning")
            else:
                idx = int(arg)
                err = self.orchestrator.dismiss_session(idx)
                if err:
                    self.notify(err, severity="warning")
                else:
                    try:
                        self.query_one(f"#pane-{idx}", SessionPane).remove()
                    except NoMatches:
                        pass
                    if not self.orchestrator.sessions:
                        self.query_one("#session-grid").display = False
                        self.query_one("#empty-state").display = True
                    self.notify(f"Session {idx} dismissed")
        elif verb == "/prompt":
            self.orchestrator._inject_prompt(arg)
        elif verb == "/sessions":
            from rich.console import Console
            from io import StringIO
            buf = StringIO()
            c = Console(file=buf, highlight=False)
            self.orchestrator._drain_queues()
            c.print(self.orchestrator._render_table())
            self.notify(buf.getvalue()[:500], timeout=8)
        elif verb == "/status":
            self.orchestrator._show_status()
        elif verb == "/model":
            from flow.router import MODEL_ALIASES
            m = MODEL_ALIASES.get(arg.lower(), arg)
            self.orchestrator.model_override = m
            self.notify(f"Model → {m}")
        elif verb == "/no-agents":
            import os
            self.orchestrator.no_agents = not self.orchestrator.no_agents
            os.environ["AP_NO_SPAWN"] = "1" if self.orchestrator.no_agents else "0"
            self.notify(f"Agent spawning: {'OFF' if self.orchestrator.no_agents else 'ON'}")
        elif verb == "/budget":
            import os
            os.environ["AP_BUDGET_USD"] = arg or "2.00"
            self.notify(f"Budget cap: ${os.environ['AP_BUDGET_USD']}")
        elif verb == "/test-flow":
            session = self.orchestrator._start_test_session()
            self.add_session_pane(session)
            self.notify("Smoke test started", timeout=3)
        elif verb == "/resume":
            self.orchestrator._resume(arg)
        elif verb == "/help":
            self.notify(
                "/view N  /stop [N]  /dismiss N  /prompt N <msg>  /model opus|sonnet|haiku\n"
                "/no-agents  /budget $X  /test-flow  /sessions  /status  /quit\n"
                "Prefix tasks: plan: …  review: …",
                title="Commands",
                timeout=10,
            )
        else:
            self.notify(f"Unknown command: {verb}", severity="warning")

    # ── Quit ──────────────────────────────────────────────────────────────────

    def action_quit_flow(self) -> None:
        done = [s for s in self.orchestrator.sessions if s.status in ("done", "failed")]
        running = [s for s in self.orchestrator.sessions if s.status == "running"]
        for s in done:
            self.orchestrator._remove_worktree(s)
        if running:
            self.notify(
                f"{len(running)} session(s) still running — worktrees kept.",
                severity="warning",
                timeout=3,
            )
        self.exit()


def start_tui() -> None:
    from flow.tracker import init_db
    from flow.repl import FlowOrchestrator
    init_db()
    orchestrator = FlowOrchestrator()
    app = FlowApp(orchestrator)
    app.run()
