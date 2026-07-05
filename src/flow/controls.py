"""_ControlsMixin — the live table plus the interactive command surface.

Everything the TUI drives: rendering the session table, /stop, /prompt, /resume,
status, dismiss, quit, and dispatching bare `flow ...` subcommands. Mixed into
FlowOrchestrator.
"""
import shlex
import subprocess
import sys
import time
import threading
from pathlib import Path
from typing import Optional

from rich.console import Console
from rich.table import Table

from flow.config import DB_PATH, get_plan, get_plan_window_caps
from flow.tracker import (
    RunState, RunStatus, load_run, get_api_spend_today, get_window_usage,
)
from flow.session import AgentSession

console = Console()


class _ControlsMixin:
    def dismiss_session(self, idx: int) -> str:
        """Remove a done/failed session and clean up its worktree. Returns error string or ''."""
        session = next((s for s in self.sessions if s.idx == idx), None)
        if session is None:
            return f"No session {idx}"
        with session.lock:
            status = session.status
        if status == "running":
            return f"Session {idx} is still running — /stop it first"
        self._remove_worktree(session)
        self.sessions.remove(session)
        return ""

    # ── Live table display ────────────────────────────────────────────────────

    def _render_table(self) -> Table:
        # Refresh api spend at most once every 5s to avoid DB on every 4Hz tick
        now = time.monotonic()
        if now - self._api_spend_last_refresh > 5.0:
            try:
                self._api_spend_cache = get_api_spend_today(self.project)
            except Exception:
                pass
            self._api_spend_last_refresh = now
        api_today = self._api_spend_cache

        running = sum(1 for s in self.sessions if s.status == "running")

        table = Table(
            title=f"flow  |  ${api_today:.2f} today  |  {running} running",
            show_header=True, header_style="bold",
            border_style="dim", expand=True,
        )
        table.add_column("#", width=3, justify="right")
        table.add_column("Type", width=8)
        table.add_column("Task", ratio=3)
        table.add_column("Phase", width=8)
        table.add_column("Steps", width=6)
        table.add_column("Cost", width=8)
        table.add_column("Last output", ratio=4)

        for session in self.sessions:
            with session.lock:
                run = session.run
                status = session.status
                last = session.last_line
                pr_url = session.pr_url

            phase = run.phase.value if run else "?"

            steps_str = ""
            if run and run.plan_steps:
                done = sum(1 for s in run.plan_steps if s.get("status") == "done")
                total = len(run.plan_steps)
                steps_str = f"{done}/{total}" if status == "running" else "✓"

            cost_str = f"${run.cost_usd:.2f}" if run else "$0.00"

            if status == "done":
                status_str = "[green]done[/green]"
                display_last = pr_url or last
            elif status == "failed":
                status_str = "[red]failed[/red]"
                display_last = last
            else:
                status_str = phase
                display_last = last

            type_colors = {"planner": "magenta", "reviewer": "yellow", "executor": "cyan"}
            type_labels = {"planner": "plan", "reviewer": "rev", "executor": "exec"}
            color = type_colors.get(session.session_type, "cyan")
            label = type_labels.get(session.session_type, session.session_type[:4])
            type_str = f"[{color}]{label}[/{color}]"

            table.add_row(
                str(session.idx),
                type_str,
                session.goal[:50],
                status_str,
                steps_str,
                cost_str,
                display_last[:75],
            )

        if not self.sessions:
            table.add_row("", "", "[dim]No sessions yet — type a task to start[/dim]", "", "", "", "")

        return table

    def _stop_session(self, idx: Optional[int]) -> None:
        if idx:
            target = next((s for s in self.sessions if s.idx == idx), None)
            targets = [target] if target else []
        else:
            targets = [s for s in self.sessions if s.status == "running"]
        if not targets:
            console.print("[dim]No running sessions.[/dim]")
            return
        for s in targets:
            sentinel = DB_PATH.parent / f"stop_{s.run.run_id}"
            sentinel.touch()
            console.print(f"[yellow]→ Stop signal sent to session {s.idx}[/yellow]")

    def _inject_prompt(self, arg: str) -> None:
        parts = arg.split(None, 1)
        if len(parts) < 2 or not parts[0].isdigit():
            console.print("[red]Usage: /prompt N <message>[/red]")
            return
        idx = int(parts[0])
        msg = parts[1].strip()
        if not msg:
            console.print("[red]Message cannot be empty.[/red]")
            return
        session = next((s for s in self.sessions if s.idx == idx), None)
        if session is None:
            console.print(f"[red]No session {idx}[/red]")
            return
        with session.lock:
            if session.status != "running":
                console.print(f"[yellow]Session {idx} is not running.[/yellow]")
                return
        session.inject_queue.put(msg)
        console.print(f"[dim]→ Message queued for session {idx} ({session.session_type})[/dim]")

    def _resume(self, run_id: str) -> None:
        from flow.tracker import get_recent_runs
        if run_id:
            r = load_run(run_id)
            if not r:
                console.print(f"[red]Run {run_id} not found.[/red]")
                return None
            return self._attach_existing_run(r)

        runs = [r for r in get_recent_runs(limit=10) if r["status"] != RunStatus.complete.value]
        if not runs:
            console.print("[yellow]No incomplete runs found.[/yellow]")
            return

        console.print("\n[bold]Recent incomplete runs:[/bold]")
        for i, r in enumerate(runs, 1):
            console.print(
                f"  [cyan]{i}.[/cyan] [{r['run_id']}] {r['goal'][:50]}  "
                f"[dim]{r['phase']} · ${r['cost_usd']:.4f}[/dim]"
            )
        try:
            choice = self.prompt_session.prompt("Pick (number or ID): ").strip()
        except (EOFError, KeyboardInterrupt):
            return

        run_id = runs[int(choice) - 1]["run_id"] if choice.isdigit() and 1 <= int(choice) <= len(runs) else choice
        r = load_run(run_id)
        if not r:
            console.print(f"[red]Run {run_id} not found.[/red]")
            return None
        return self._attach_existing_run(r)

    def _attach_existing_run(self, run: RunState) -> None:
        git_root = self._git_root()
        cwd = git_root  # fallback if original worktree is gone

        wt_result = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            capture_output=True, text=True,
        )
        if wt_result.returncode == 0:
            current_wt: Optional[Path] = None
            for line in wt_result.stdout.splitlines():
                if line.startswith("worktree "):
                    current_wt = Path(line[9:].strip())
                elif line.startswith("branch ") and current_wt:
                    branch_ref = line[7:].strip()
                    branch_name = branch_ref.split("/")[-1] if "/" in branch_ref else branch_ref
                    if branch_name == run.branch and current_wt.exists():
                        cwd = current_wt
                        break

        idx = len(self.sessions) + 1
        session = AgentSession(
            idx=idx, goal=run.goal, run=run,
            project=run.project, branch=run.branch,
            cwd=cwd,
        )
        session.thread = threading.Thread(
            target=self._session_worker, args=(session,), daemon=True,
        )
        self.sessions.append(session)
        session.thread.start()
        console.print(f"[green]✓ Resumed: {run.goal[:55]}[/green]")
        return session

    def _show_status(self) -> None:
        api_today = get_api_spend_today(self.project)
        plan = get_plan()
        window = get_window_usage(plan)
        cap = get_plan_window_caps().get(plan, {}).get("msgs", 0)
        quota_str = f"{window['msgs_used']}/{cap} msgs" if cap else f"{window['msgs_used']} msgs"
        console.print(
            f"[bold]Project:[/bold] {self.project} | "
            f"[bold]API spend today:[/bold] ${api_today:.4f} | "
            f"[bold]Quota (5h):[/bold] {quota_str}"
        )
        self._show_sessions()

    def _show_sessions(self) -> None:
        self._drain_queues()
        if not self.sessions:
            console.print("[dim]No sessions.[/dim]")
            return
        console.print(self._render_table())

    def _on_quit(self) -> None:
        done = [s for s in self.sessions if s.status in ("done", "failed")]
        running = [s for s in self.sessions if s.status == "running"]
        for s in done:
            self._remove_worktree(s)
        if running:
            console.print(
                f"[yellow]{len(running)} session(s) still running — worktrees kept.[/yellow]"
            )
        console.print("[dim]Goodbye.[/dim]")
        sys.exit(0)

    def _try_dispatch_flow_cmd(self, user_input: str) -> bool:
        stripped = user_input.strip()
        if stripped == "flow":
            console.print("[yellow]Bare `flow` blocked — you're already in the REPL.[/yellow]")
            return True
        if not stripped.startswith("flow "):
            return False
        rest = stripped[5:].strip()
        if not rest:
            return False
        try:
            argv = shlex.split(rest)
        except ValueError as e:
            console.print(f"[red]Could not parse command: {e}[/red]")
            return True
        from flow.cli import app
        try:
            app(argv, standalone_mode=True)
        except SystemExit as e:
            if e.code not in (0, None):
                console.print(f"[red]Command exited with code {e.code}[/red]")
        return True
