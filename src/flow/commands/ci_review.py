"""flow ci-review — two-pass Haiku→Sonnet AI code reviewer for GitHub Actions."""
import json
import os
import subprocess
from pathlib import Path
from typing import Optional

import anthropic
from rich.console import Console

from flow.billing import metered_call
from flow.config import load_style, style_prompt
from flow.router import utility_model

console = Console()

PASS1_SYSTEM = """You are a fast code reviewer. Scan the diff and output a JSON array of issues.
Each issue: {"file": "path", "line": <int or null>, "severity": "blocker|suggestion|nit", "comment": "..."}
Rules:
- Flag real issues only: bugs, security holes, missing error handling, broken logic.
- Do NOT flag style nitpicks unless severity is "nit".
- Output ONLY the JSON array, no prose."""

PASS2_SYSTEM = """You are a thorough code reviewer. You receive a list of flagged issues and the full diff.
Write a concise GitHub PR review comment in markdown.
Structure:
## Review
<1-2 sentence summary>

### Blockers
<list only if any blocker exists>

### Suggestions
<list only if any exist>

### Nits
<list only if any exist, keep brief>

If there are no issues, write: "Looks good."
Output ONLY the markdown."""


def _client() -> anthropic.Anthropic:
    key = os.getenv("ANTHROPIC_API_KEY") or ("mock-key" if os.getenv("FLOW_MOCK_API") == "1" else "")
    return anthropic.Anthropic(api_key=key)


def _get_diff(pr_number: Optional[int], diff_path: Optional[str]) -> str:
    if diff_path:
        return Path(diff_path).read_text()
    if pr_number:
        result = subprocess.run(
            ["gh", "pr", "diff", str(pr_number)],
            capture_output=True, text=True,
        )
        if result.returncode == 0:
            return result.stdout
        raise RuntimeError(f"gh pr diff failed: {result.stderr}")
    result = subprocess.run(["git", "diff", "HEAD~1"], capture_output=True, text=True)
    return result.stdout


def _post_review(pr_number: int, body: str) -> None:
    result = subprocess.run(
        ["gh", "pr", "review", str(pr_number), "--comment", "--body", body],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        console.print(f"[yellow]Could not post review: {result.stderr}[/yellow]")


def cmd_ci_review(diff_path: Optional[str] = None, pr_number: Optional[int] = None) -> None:
    style = load_style()
    ci_style = style_prompt(style, ["ci_review"])

    try:
        diff = _get_diff(pr_number, diff_path)
    except Exception as e:
        console.print(f"[red]Could not get diff: {e}[/red]")
        raise SystemExit(1)

    if not diff.strip():
        console.print("[dim]Empty diff — nothing to review.[/dim]")
        return

    client = _client()
    run_id = f"ci-pr{pr_number}" if pr_number else "ci-local"

    # ── Pass 1: Haiku quick scan ──────────────────────────────────────────────
    system1 = (ci_style + "\n\n" + PASS1_SYSTEM) if ci_style else PASS1_SYSTEM
    console.print("[dim]Pass 1: quick scan (Haiku)...[/dim]")

    resp1 = metered_call(
        client, utility_model("fast"),
        run_id=run_id, purpose="ci_review_pass1",
        max_tokens=1000,
        system=system1,
        messages=[{"role": "user", "content": f"Diff:\n\n{diff[:12000]}"}],
    )

    issues_raw = resp1.content[0].text.strip()
    try:
        issues = json.loads(issues_raw)
    except json.JSONDecodeError:
        issues = []

    blockers = [i for i in issues if i.get("severity") == "blocker"]
    suggestions = [i for i in issues if i.get("severity") == "suggestion"]
    nits = [i for i in issues if i.get("severity") == "nit"]

    console.print(
        f"[dim]Pass 1 found: {len(blockers)} blocker(s), "
        f"{len(suggestions)} suggestion(s), {len(nits)} nit(s)[/dim]"
    )

    # ── Pass 2: Sonnet deep review (only if there are issues) ────────────────
    if not issues:
        final_comment = "Looks good."
    else:
        system2 = (ci_style + "\n\n" + PASS2_SYSTEM) if ci_style else PASS2_SYSTEM
        console.print("[dim]Pass 2: deep review (Sonnet)...[/dim]")

        issues_summary = json.dumps(issues, indent=2)
        resp2 = metered_call(
            client, utility_model("smart"),
            run_id=run_id, purpose="ci_review_pass2",
            max_tokens=800,
            system=system2,
            messages=[{"role": "user", "content": (
                f"Issues found:\n{issues_summary}\n\n"
                f"Full diff:\n{diff[:10000]}"
            )}],
        )
        final_comment = resp2.content[0].text.strip()

    console.print(final_comment)

    if pr_number:
        _post_review(pr_number, final_comment)
        console.print(f"[green]✓ Review posted to PR #{pr_number}[/green]")
    else:
        console.print("[dim](No PR number provided — review printed above)[/dim]")

    if blockers:
        raise SystemExit(1)
