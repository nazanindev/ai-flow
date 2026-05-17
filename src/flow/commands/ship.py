"""flow ship — verify → commit → PR create/update."""
import os
import re
import subprocess

import anthropic
from rich.console import Console
from rich.panel import Panel

from flow.billing import metered_call
from flow.config import get_project_id, load_style, style_prompt
from flow.router import utility_model
from flow.tracker import init_db, load_active_run, Phase, RunStatus
from flow.run_manager import advance_phase, complete_run, save_pr_url
from flow.commands.verify import run_checks

console = Console()


def _git(args: list, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git"] + args, capture_output=True, text=True, check=check)


def _gh(args: list, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["gh"] + args, capture_output=True, text=True, check=check)


def _client() -> anthropic.Anthropic:
    return anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY", ""))


def _slugify(value: str) -> str:
    value = re.sub(r"[^a-zA-Z0-9]+", "-", (value or "").strip().lower())
    return value.strip("-")


def _style_ship_defaults(style: dict, run) -> tuple[str, str, str]:
    """Return (branch_name_default, pr_title_prefix, pr_title_from_goal)."""
    ship_style = style.get("ship") if isinstance(style, dict) else {}
    if not isinstance(ship_style, dict):
        return "", "", ""

    branch_name_default = ""
    if ship_style.get("branch_from_goal") and run and run.goal:
        prefix = str(ship_style.get("branch_prefix", "") or "")
        goal_slug = _slugify(run.goal)[:64]
        if goal_slug:
            branch_name_default = f"{prefix}{goal_slug}"

    pr_title_prefix = str(ship_style.get("pr_title_prefix", "") or "")
    pr_title_from_goal = ""
    if ship_style.get("pr_title_from_goal") and run and run.goal:
        pr_title_from_goal = str(run.goal).strip()
    return branch_name_default, pr_title_prefix, pr_title_from_goal


def _generate_commit_message(diff: str, style: dict, run_id: str) -> str:
    system = style_prompt(style, ["commit_message"]) or "Write a short, imperative commit message."
    system += "\nOutput ONLY the commit message — no explanation, no code fences."

    resp = metered_call(
        _client(), utility_model("fast"),
        run_id=run_id, purpose="commit_msg",
        max_tokens=120,
        system=system,
        messages=[{"role": "user", "content": f"Git diff:\n\n{diff[:8000]}"}],
    )
    return resp.content[0].text.strip().strip('"').strip("'")


def _generate_pr_body(run, diff: str, style: dict) -> tuple[str, str]:
    """Returns (title, body)."""
    system_title = style_prompt(style, ["pr_title"]) or "Write a plain PR title, sentence case."
    system_title += "\nOutput ONLY the title — one line, no explanation."

    system_body = style_prompt(style, ["pr_body"]) or "Write a clear PR description."
    system_body += "\nOutput ONLY the PR body in markdown."

    plan_summary = ""
    if run and run.plan_steps:
        done = [s["description"] for s in run.plan_steps if s.get("status") == "done"]
        pending = [s["description"] for s in run.plan_steps if s.get("status") != "done"]
        if done:
            plan_summary += "\nCompleted steps:\n" + "\n".join(f"- {d}" for d in done)
        if pending:
            plan_summary += "\nRemaining:\n" + "\n".join(f"- {p}" for p in pending)

    context = f"""Goal: {run.goal if run else 'unknown'}
Decisions: {run.decisions if run else []}
Artifacts: {run.artifacts if run else []}
{plan_summary}

Diff (first 6000 chars):
{diff[:6000]}"""

    client = _client()
    run_id = run.run_id if run else "none"

    title_resp = metered_call(
        client, utility_model("fast"),
        run_id=run_id, purpose="pr_title",
        max_tokens=80, system=system_title,
        messages=[{"role": "user", "content": context}],
    )
    body_resp = metered_call(
        client, utility_model("smart"),
        run_id=run_id, purpose="pr_body",
        max_tokens=600, system=system_body,
        messages=[{"role": "user", "content": context}],
    )

    return (
        title_resp.content[0].text.strip().strip('"'),
        body_resp.content[0].text.strip(),
    )


def cmd_ship(branch_name: str = "", pr_title_override: str = "") -> None:
    init_db()
    project = get_project_id()
    run = load_active_run(project)
    style = load_style()
    style_branch_name, style_title_prefix, style_title_from_goal = _style_ship_defaults(style, run)
    run_id = run.run_id if run else "none"

    # ── 1. Verify gate ────────────────────────────────────────────────────────
    console.print("[bold]Running verification...[/bold]")
    passed, output = run_checks()
    if not passed:
        console.print("[red]✗ Verification failed — fix tests before shipping.[/red]")
        console.print(f"[dim]{output[-2000:]}[/dim]")
        raise SystemExit(1)
    console.print("[green]✓ Verification passed[/green]")

    # ── 2. Get diff ───────────────────────────────────────────────────────────
    status_result = _git(["status", "--porcelain"], check=False)
    if not status_result.stdout.strip():
        console.print("[yellow]Nothing to commit — working tree is clean.[/yellow]")
        raise SystemExit(0)

    diff_result = _git(["diff", "HEAD"], check=False)
    diff = diff_result.stdout.strip()
    if not diff:
        diff_result = _git(["diff", "--cached"], check=False)
        diff = diff_result.stdout.strip()
    if not diff:
        # Only untracked new files — use status as the change summary for commit message
        diff = status_result.stdout.strip()

    # ── 3. Generate commit message ────────────────────────────────────────────
    console.print("[dim]Generating commit message...[/dim]")
    commit_msg = _generate_commit_message(diff, style, run_id)
    console.print(f"[bold]Commit:[/bold] {commit_msg}")

    # ── 4. Stage and commit ───────────────────────────────────────────────────
    _RISK_PATTERNS = (".env", "credentials", ".pem", ".key", "id_rsa", "service_account", "secret")
    untracked = _git(["ls-files", "--others", "--exclude-standard"], check=False)
    for uf in untracked.stdout.strip().splitlines():
        if any(pat in uf.lower() for pat in _RISK_PATTERNS):
            console.print(f"[red]✗ Ship aborted — potential secret file: {uf}[/red]")
            console.print("[dim]Remove or add to .gitignore, then re-run flow ship.[/dim]")
            raise SystemExit(1)
    _git(["add", "-A"])
    commit_result = _git(["commit", "-m", commit_msg], check=False)
    if commit_result.returncode != 0:
        console.print(f"[red]git commit failed:[/red] {commit_result.stderr}")
        raise SystemExit(1)
    console.print("[green]✓ Committed[/green]")

    # ── 5. Generate PR title + body ───────────────────────────────────────────
    console.print("[dim]Generating PR description...[/dim]")
    pr_title, pr_body = _generate_pr_body(run, diff, style)
    if style_title_from_goal:
        pr_title = style_title_from_goal
    if style_title_prefix and not pr_title.startswith(style_title_prefix):
        pr_title = f"{style_title_prefix}{pr_title}"
    if pr_title_override.strip():
        pr_title = pr_title_override.strip()
    console.print(f"[bold]PR title:[/bold] {pr_title}")

    # ── 6. Push and create PR ─────────────────────────────────────────────────
    branch_result = _git(["rev-parse", "--abbrev-ref", "HEAD"], check=False)
    branch = (branch_name or "").strip() or style_branch_name or branch_result.stdout.strip() or "HEAD"

    # Optional branch rename for this ship if caller requested a custom name.
    if branch_name.strip() or style_branch_name:
        rename_result = _git(["branch", "-M", branch], check=False)
        if rename_result.returncode != 0:
            console.print(f"[red]git branch rename failed:[/red] {rename_result.stderr}")
            raise SystemExit(1)

    push_result = subprocess.run(["git", "push", "-u", "origin", branch], capture_output=True, text=True)
    if push_result.returncode != 0:
        console.print(f"[red]git push failed:[/red] {push_result.stderr}")
        raise SystemExit(1)

    pr_result = _gh(["pr", "create", "--title", pr_title, "--body", pr_body], check=False)
    if pr_result.returncode != 0:
        combined = (pr_result.stderr or "") + "\n" + (pr_result.stdout or "")
        existing = re.search(r"https?://github\.com/\S+/pull/\d+", combined)
        if existing:
            pr_url = existing.group(0)
            console.print(f"[yellow]PR already exists:[/yellow] {pr_url}")
            if run:
                save_pr_url(run, pr_url)
                advance_phase(run, Phase.ship)
                complete_run(run)
            return
        console.print(f"[red]gh pr create failed:[/red] {pr_result.stderr}")
        raise SystemExit(1)

    pr_url = pr_result.stdout.strip()
    console.print(f"[green]✓ PR created:[/green] {pr_url}")

    # ── 7. Advance run to ship + complete ─────────────────────────────────────
    if run:
        save_pr_url(run, pr_url)
        advance_phase(run, Phase.ship)
        complete_run(run)
        console.print(Panel(
            f"[bold green]Run complete[/bold green]\n"
            f"Goal: {run.goal[:80]}\n"
            f"API spend: ${run.cost_usd:.4f} | Budget used: {run.step_budget_used:.1f} steps\n"
            f"Subscription: {run.subscription_msgs} msgs | "
            f"{(run.subscription_tokens_in + run.subscription_tokens_out):,} tokens\n"
            f"PR: {pr_url}",
            border_style="green",
        ))
