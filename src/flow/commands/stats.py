"""flow status and flow stats commands."""
import os

from rich.console import Console
from rich.table import Table
from rich.panel import Panel

from flow.tracker import (
    init_db, get_api_spend_today, get_subscription_tokens_today,
    get_project_stats, get_recent_runs, get_cost_per_pr, load_active_run,
    get_window_usage,
)
from flow.config import get_project_id, constraints, get_plan, get_plan_window_caps

console = Console()


def _active_feature_line() -> str:
    """Return a compact line describing the active feature, if any."""
    try:
        from flow.features import get_active_feature

        feat = get_active_feature()
        if not feat:
            return "[dim]Active feature:[/dim] none"
        return f"[bold]Active feature:[/bold] {feat.id} ({feat.state}) — {feat.behavior[:80]}"
    except Exception:
        return "[dim]Active feature:[/dim] unavailable"


def _budget_bar(used: float, total: float, width: int = 20) -> str:
    pct = min(used / total, 1.0) if total > 0 else 0
    filled = int(pct * width)
    bar = "█" * filled + "░" * (width - filled)
    color = "red" if pct >= 0.9 else "yellow" if pct >= 0.6 else "green"
    return f"[{color}]{bar}[/{color}] {pct*100:.0f}%"


def cmd_status() -> None:
    init_db()
    project = get_project_id()
    plan = get_plan()
    caps = get_plan_window_caps()

    api_today = get_api_spend_today(project)
    api_today_all = get_api_spend_today()
    sub_tokens = get_subscription_tokens_today(project)
    window = get_window_usage(plan)
    c = constraints()
    api_gate = float(os.getenv("FLOW_BUDGET_USD") or c.get("api_spend_gate_usd", 1.0))
    msg_cap = caps.get(plan, {}).get("msgs", 0)
    run = load_active_run(project)

    # ── Subscription quota panel ──────────────────────────────────────────────
    quota_lines = [f"[bold]Plan:[/bold] {plan}"]
    if msg_cap:
        quota_lines.append(
            f"[bold]5h window:[/bold] {window['msgs_used']}/{msg_cap} msgs  "
            f"{_budget_bar(window['msgs_used'], msg_cap)}"
        )
        quota_lines.append(
            f"[bold]Tokens in window:[/bold] "
            f"{window['tokens_in']:,} in / {window['tokens_out']:,} out"
        )
    else:
        quota_lines.append("[dim]No quota tracking (api_only plan)[/dim]")
    quota_lines.append(
        f"[bold]Subscription tokens today (this project):[/bold] "
        f"{sub_tokens['tokens_in']:,} in / {sub_tokens['tokens_out']:,} out"
    )

    # ── API spend panel ───────────────────────────────────────────────────────
    spend_lines = [
        f"[bold]Today (this project):[/bold] ${api_today:.4f}  "
        f"{_budget_bar(api_today, api_gate)}  of ${api_gate:.2f} gate",
        f"[bold]Today (all projects):[/bold] ${api_today_all:.4f}",
        "[dim]Covers: clarify questions, commit msgs, PR descriptions, CI reviews[/dim]",
    ]

    # ── Active run ────────────────────────────────────────────────────────────
    run_lines = []
    if run:
        projected = None
        if run.current_step > 0:
            projected = run.cost_usd / run.current_step * run.max_steps

        phase_budgets = c.get("phase_step_budgets", {})
        phase_budget = phase_budgets.get(run.phase.value)
        effective_max = float(phase_budget if phase_budget is not None else run.max_steps)

        run_lines += [
            f"[bold yellow]Active run:[/bold yellow] {run.run_id}",
            f"[bold]Goal:[/bold] {run.goal[:80]}",
            f"[bold]Phase:[/bold] {run.phase.value} | step {run.current_step}/{run.max_steps}"
            f" | budget {run.step_budget_used:.1f}/{effective_max:.0f}",
            (
                f"[bold]Feature:[/bold] {run.feature_id}"
                if run.feature_id
                else "[dim]Feature:[/dim] none attached"
            ),
            f"[bold]API spend:[/bold] ${run.cost_usd:.4f}"
            + (f"  →  ~${projected:.4f} projected" if projected else ""),
            f"[bold]Subscription:[/bold] {run.subscription_msgs} msgs | "
            f"{run.subscription_tokens_in:,} in / {run.subscription_tokens_out:,} out",
        ]

        if run.plan_steps:
            done = sum(1 for s in run.plan_steps if s.get("status") == "done")
            total_steps = len(run.plan_steps)
            run_lines.append(f"[bold]Plan:[/bold] {done}/{total_steps} steps done")
            for s in run.plan_steps:
                marker = "[green]✓[/green]" if s.get("status") == "done" else "○"
                run_lines.append(f"  {marker} {s['description']}")
    else:
        run_lines.append("[dim]No active run.[/dim]")

    console.print(Panel(
        "\n".join([
            "[bold cyan]Subscription quota[/bold cyan]",
            *quota_lines,
            "",
            "[bold cyan]API spend (utility calls)[/bold cyan]",
            *spend_lines,
            "",
            _active_feature_line(),
            "",
            *run_lines,
        ]),
        title="AI Flow Status", border_style="cyan",
    ))


def cmd_stats(project_filter=None) -> None:
    init_db()

    project_stats = get_project_stats()
    if not project_stats:
        console.print("[dim]No sessions recorded yet.[/dim]")
        return

    t = Table(title="Usage by project", show_lines=True)
    t.add_column("Project", style="cyan")
    t.add_column("Sessions", justify="right")
    t.add_column("API spend", justify="right")
    t.add_column("Sub tokens", justify="right")
    t.add_column("Last active")

    for row in project_stats:
        if project_filter and project_filter.lower() not in row["project"].lower():
            continue
        sub_tokens = row.get("sub_tokens", 0) or 0
        t.add_row(
            row["project"],
            str(row["sessions"]),
            f"${row['api_spend']:.4f}",
            f"{int(sub_tokens):,}",
            row["last_active"][:10] if row["last_active"] else "—",
        )
    console.print(t)

    # Recent runs
    runs = get_recent_runs(project_filter, limit=10)
    if runs:
        r = Table(title="Recent runs", show_lines=True)
        r.add_column("Run ID", style="dim")
        r.add_column("Goal")
        r.add_column("Phase")
        r.add_column("Status")
        r.add_column("API spend", justify="right")
        r.add_column("Sub msgs", justify="right")
        r.add_column("Updated")

        status_colors = {"active": "green", "complete": "blue", "failed": "red", "blocked": "yellow"}
        for row in runs:
            color = status_colors.get(row["status"], "white")
            r.add_row(
                row["run_id"],
                row["goal"][:50],
                row["phase"],
                f"[{color}]{row['status']}[/{color}]",
                f"${row['cost_usd']:.4f}",
                str(row.get("subscription_msgs", 0) or 0),
                row["updated_at"][:10] if row["updated_at"] else "—",
            )
        console.print(r)

    # Cost per PR
    pr_runs = get_cost_per_pr(project_filter)
    if pr_runs:
        p = Table(title="Cost per PR", show_lines=True)
        p.add_column("Run ID", style="dim")
        p.add_column("Goal")
        p.add_column("PR")
        p.add_column("API spend", justify="right")
        p.add_column("Sub msgs", justify="right")
        p.add_column("Budget used", justify="right")
        p.add_column("Shipped")
        for row in pr_runs:
            p.add_row(
                row["run_id"],
                row["goal"][:40],
                row["pr_url"],
                f"${row['cost_usd']:.4f}",
                str(row.get("subscription_msgs", 0) or 0),
                f"{row['step_budget_used']:.1f}",
                row["updated_at"][:10] if row["updated_at"] else "—",
            )
        console.print(p)
