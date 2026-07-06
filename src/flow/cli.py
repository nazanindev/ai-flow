"""CLI entry point — `flow` command."""
import warnings
warnings.filterwarnings("ignore", message=".*urllib3 v2 only supports OpenSSL.*", category=UserWarning)

import typer
from typing import Optional

app = typer.Typer(
    name="flow",
    help="AI Flow — a control plane for autonomous coding agents",
    no_args_is_help=False,
    invoke_without_command=True,
)
features_app = typer.Typer(
    help="Manage repo-local features.yaml state",
    no_args_is_help=False,
    invoke_without_command=True,
)
app.add_typer(features_app, name="features")


@app.callback(invoke_without_command=True)
def main(ctx: typer.Context) -> None:
    """Start the AI Flow REPL. Run with no arguments to enter interactive mode."""
    if ctx.invoked_subcommand is None:
        from flow.repl import start_repl
        start_repl()


@app.command()
def init(
    force: bool = typer.Option(False, "--force", help="Overwrite existing hooks"),
    repo: bool = typer.Option(False, "--repo", help="Also scaffold repo-local harness artifacts"),
) -> None:
    """Wire AI Flow hooks into ~/.claude/settings.json and create ~/.flow/.env."""
    from flow.commands.init import cmd_init
    cmd_init(force=force, repo=repo)


@app.command()
def doctor(
    fix: bool = typer.Option(False, "--fix", help="Rewrite hooks via `flow init --force`"),
) -> None:
    """Verify Claude Code hook commands can import and run AI Flow hooks."""
    from flow.commands.doctor import cmd_doctor

    cmd_doctor(fix=fix)


@app.command()
def status() -> None:
    """Show current run state and today's cost."""
    from flow.commands.stats import cmd_status
    cmd_status()


@app.command()
def stats(project: Optional[str] = typer.Option(None, "--project", "-p", help="Filter by project name")) -> None:
    """Show cost breakdown by project and recent runs."""
    from flow.commands.stats import cmd_stats
    cmd_stats(project)


@app.command()
def ship(
    branch_name: str = typer.Option("", "--branch-name", help="Rename current branch before push"),
    pr_title: str = typer.Option("", "--pr-title", help="Override generated PR title"),
) -> None:
    """Verify tests, commit with AI message, create PR with AI description."""
    from flow.commands.ship import cmd_ship
    cmd_ship(branch_name=branch_name, pr_title_override=pr_title)


@app.command()
def verify() -> None:
    """Run tests/lint for the current project."""
    from flow.commands.verify import cmd_verify
    cmd_verify()


@app.command()
def check(
    json_output: bool = typer.Option(False, "--json", help="Print structured JSON output"),
) -> None:
    """Run independent checker against local uncommitted diff."""
    from flow.commands.check import cmd_check

    cmd_check(json_output=json_output)


@app.command()
def resume(run_id: Optional[str] = typer.Argument(None, help="Run ID to resume (shows picker if omitted)")) -> None:
    """Resume an interrupted run. Shows a picker if no run ID is given."""
    from flow.tracker import init_db, load_run, get_recent_runs, RunStatus
    from rich.console import Console
    c = Console()
    init_db()

    if not run_id:
        runs = [r for r in get_recent_runs(limit=10) if r["status"] != RunStatus.complete.value]
        if not runs:
            c.print("[yellow]No incomplete runs found.[/yellow]")
            raise typer.Exit()
        c.print("\n[bold]Recent incomplete runs:[/bold]")
        for i, r in enumerate(runs, 1):
            c.print(
                f"  [cyan]{i}.[/cyan] [{r['run_id']}] {r['goal'][:60]}  "
                f"[dim]{r['phase']} · ${r['cost_usd']:.4f}[/dim]"
            )
        run_id = typer.prompt("Run ID to resume")

    r = load_run(run_id)
    if not r:
        c.print(f"[red]Run {run_id} not found.[/red]")
        raise typer.Exit(1)

    c.print(f"[green]Resuming run {r.run_id}:[/green] {r.goal}")
    c.print(f"[dim]Phase: {r.phase.value} | Steps: {r.current_step}/{r.max_steps} | Cost: ${r.cost_usd:.4f}[/dim]")
    if r.plan_steps:
        c.print("\n[bold]Plan steps:[/bold]")
        for s in r.plan_steps:
            marker = "[green]✓[/green]" if s.get("status") == "done" else "○"
            c.print(f"  {marker} {s['description']}")
    c.print("\n[dim]Start the REPL with `flow` and use /resume to continue in an interactive session.[/dim]")


@app.command(name="ci-review")
def ci_review(
    diff: Optional[str] = typer.Option(None, "--diff", help="Path to diff file"),
    pr: Optional[int] = typer.Option(None, "--pr", help="PR number to review"),
) -> None:
    """AI code review for CI (two-pass Haiku→Sonnet). Designed for GitHub Actions."""
    from flow.commands.ci_review import cmd_ci_review
    cmd_ci_review(diff_path=diff, pr_number=pr)


@app.command()
def events(
    run_id: Optional[str] = typer.Argument(None, help="Run ID (defaults to most recent)"),
) -> None:
    """Show the event timeline for a run (phase transitions, blocks, session ends)."""
    from flow.tracker import init_db, get_run_events, get_recent_runs
    from flow.config import get_project_id
    from rich.console import Console
    from datetime import datetime, timezone
    c = Console()
    init_db()

    if not run_id:
        recent = get_recent_runs(get_project_id(), limit=1)
        if not recent:
            recent = get_recent_runs(limit=1)
        if not recent:
            c.print("[yellow]No runs found.[/yellow]")
            raise typer.Exit()
        run_id = recent[0]["run_id"]

    evts = get_run_events(run_id)
    if not evts:
        c.print(f"[yellow]No events found for run {run_id}.[/yellow]")
        raise typer.Exit()

    c.print(f"\n[bold cyan]Event timeline:[/bold cyan] [dim]{run_id}[/dim]\n")
    try:
        t0 = datetime.fromisoformat(evts[0]["created_at"]).replace(tzinfo=timezone.utc)
    except Exception:
        t0 = datetime.now(timezone.utc)

    for e in evts:
        try:
            ts = datetime.fromisoformat(e["created_at"]).replace(tzinfo=timezone.utc)
            rel = (ts - t0).total_seconds()
            m = int(rel // 60); s = int(rel % 60)
            time_str = f"{m:02d}:{s:02d}"
        except Exception:
            time_str = "??:??"

        phase = f"[cyan]{e.get('phase',''):7s}[/cyan]"
        evt_type = e.get("event_type", "")
        tool = e.get("tool_name", "") or ""
        blocked = e.get("blocked", False)
        reason = e.get("block_reason", "") or ""
        meta = e.get("metadata") or {}

        if blocked:
            event_color = "red"
        elif evt_type == "phase_transition":
            event_color = "green"
        elif evt_type == "session_end":
            event_color = "blue"
        elif evt_type in ("verify_result", "check_result"):
            passed = meta.get("passed")
            event_color = "green" if passed else "yellow"
        else:
            event_color = "white"

        detail_parts = []
        if tool:
            detail_parts.append(f"[bold]{tool}[/bold]")
        if reason:
            detail_parts.append(f'[dim]"{reason[:60]}"[/dim]')
        for k, v in meta.items():
            if k not in ("from_phase",):
                detail_parts.append(f"[dim]{k}={v}[/dim]")
        detail = "  ".join(detail_parts)

        c.print(
            f"  [dim]{time_str}[/dim]  {phase}  [{event_color}]{evt_type:<20s}[/{event_color}]  {detail}"
        )
    c.print()


@app.command()
def serve(port: int = typer.Option(7331, "--port", "-p")) -> None:
    """Start local API server for dashboard frontend."""
    from flow.commands.serve import cmd_serve
    cmd_serve(port)


@app.command()
def route(task: str = typer.Argument(..., help="Describe the task")) -> None:
    """Recommend which model tier to use for a task."""
    from flow.router import model_for
    from flow.tracker import Phase
    from rich.console import Console
    c = Console()
    model = model_for(Phase.execute, task)
    c.print(f"[bold]Recommended model:[/bold] {model}")
    c.print(f"[dim]For task:[/dim] {task}")


@features_app.callback(invoke_without_command=True)
def features_main(ctx: typer.Context) -> None:
    """Default to listing feature state when no subcommand is passed."""
    if ctx.invoked_subcommand is None:
        from flow.commands.features import cmd_features_list

        cmd_features_list()


@features_app.command("list")
def features_list() -> None:
    """List all features from features.yaml."""
    from flow.commands.features import cmd_features_list
    cmd_features_list()


@features_app.command("add")
def features_add(
    feature_id: str = typer.Argument(..., help="Feature ID (e.g. F01)"),
    behavior: str = typer.Argument(..., help="Behavior statement"),
    verify: str = typer.Option(..., "--verify", help="Verification command"),
    state: str = typer.Option("not_started", "--state", help="Initial state"),
) -> None:
    """Add a feature entry."""
    from flow.commands.features import cmd_features_add
    cmd_features_add(feature_id=feature_id, behavior=behavior, verification=verify, state=state)


@features_app.command("active")
def features_active() -> None:
    """Show the active feature."""
    from flow.commands.features import cmd_features_active
    cmd_features_active()


@features_app.command("pick")
def features_pick(
    feature_id: Optional[str] = typer.Argument(None, help="Feature ID to activate; default picks first not_started"),
) -> None:
    """Set active feature (WIP=1)."""
    from flow.commands.features import cmd_features_pick
    cmd_features_pick(feature_id=feature_id)


@features_app.command("verify")
def features_verify(
    feature_id: Optional[str] = typer.Option(None, "--id", help="Feature ID; default uses active feature"),
) -> None:
    """Run feature verification and transition active -> passing."""
    from flow.commands.features import cmd_features_verify
    cmd_features_verify(feature_id=feature_id)
