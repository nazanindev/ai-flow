"""Verification gate — auto-detect and run tests/lint before shipping."""
import json
import re
import subprocess
from pathlib import Path
from typing import Tuple, Optional

from rich.console import Console

console = Console()


def detect_runner(cwd: Path = None) -> Optional[str]:
    """Return the test command for the project, or None if none detected."""
    cwd = cwd or Path.cwd()

    # Python: pytest
    for marker in ("pytest.ini", "setup.cfg", "tox.ini"):
        if (cwd / marker).exists():
            return "pytest"
    pyproject = cwd / "pyproject.toml"
    if pyproject.exists():
        content = pyproject.read_text()
        if "[tool.pytest" in content:
            return "pytest"

    # Node: npm test (only if test script is defined)
    pkg = cwd / "package.json"
    if pkg.exists():
        try:
            data = json.loads(pkg.read_text())
            if data.get("scripts", {}).get("test"):
                return "npm test"
        except Exception:
            pass

    # Makefile with a test target
    makefile = cwd / "Makefile"
    if makefile.exists():
        content = makefile.read_text()
        if "test:" in content or "test :" in content:
            return "make test"

    return None


def run_checks(cwd: Path = None) -> Tuple[bool, str]:
    """Run the detected test suite. Returns (passed, output)."""
    cwd = cwd or Path.cwd()
    runner = detect_runner(cwd)

    if runner is None:
        msg = "No test runner detected — skipping verification."
        console.print(f"[yellow]{msg}[/yellow]")
        return True, msg

    console.print(f"[dim]→ Running: {runner}[/dim]")
    try:
        result = subprocess.run(
            runner,
            shell=True,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=300,
        )
        output = (result.stdout + result.stderr).strip()
        passed = result.returncode == 0
        return passed, output
    except subprocess.TimeoutExpired:
        return False, "Verification timed out after 300s."
    except Exception as e:
        return False, f"Verification error: {e}"


def _failure_summary(runner: str, output: str) -> tuple[str, str, str]:
    """Create agent-oriented WHAT/WHY/FIX guidance from runner output."""
    what = f"{runner} exited non-zero"
    why = "verification command reported one or more failing checks"
    fix = f"Run `{runner}` locally and address the first failure before re-running."

    if "Verification timed out" in output:
        what = f"{runner} timed out after 300s"
        why = "test suite or command exceeded timeout window"
        fix = f"Run `{runner}` directly, isolate slow tests, then retry `flow verify`."
        return what, why, fix

    if runner == "pytest":
        m = re.search(r"^FAILED\s+([^\n]+)", output, re.MULTILINE)
        if m:
            what = m.group(1).strip()
            why = "pytest reported a failing test case"
            fix = (
                f"Run `pytest {what.split('::')[0]} -x` (or target test) to fix the first failure."
            )
            return what, why, fix

    err = re.search(r"(?im)^(.*(?:error|failed|exception).*)$", output)
    if err:
        what = err.group(1).strip()[:220]
        why = "command output contains an explicit failure signal"
        fix = f"Inspect this failure line in `{runner}` output, fix root cause, then re-run."

    return what, why, fix


def cmd_verify() -> None:
    """Run verification checks and print the result."""
    from flow.commands.doctor import hook_health_one_liner, hook_health_ok
    from flow.tracker import init_db, load_active_run
    from flow.config import get_project_id
    from flow.run_manager import advance_phase
    from flow.tracker import Phase

    init_db()
    health = hook_health_one_liner()
    if hook_health_ok():
        console.print(f"[dim]{health}[/dim]")
    else:
        console.print(f"[yellow]{health}[/yellow]")
    project = get_project_id()
    run = load_active_run(project)

    if run:
        advance_phase(run, Phase.verify)

    runner = detect_runner()
    passed, output = run_checks()

    if passed:
        console.print("[green]✓ Verification passed[/green]")
        if run:
            from flow.tracker import save_event
            save_event(
                run_id=run.run_id,
                event_type="verify_result",
                project=run.project,
                phase=run.phase.value,
                metadata={"passed": True, "runner": runner or "none"},
            )
            advance_phase(run, Phase.ship)
    else:
        console.print("[red]✗ Verification failed[/red]")
        if run:
            from flow.tracker import save_event
            save_event(
                run_id=run.run_id,
                event_type="verify_result",
                project=run.project,
                phase=run.phase.value,
                metadata={"passed": False, "runner": runner or "none"},
            )
        if runner:
            what, why, fix = _failure_summary(runner, output)
            console.print(f"[yellow]WHAT:[/yellow] {what}")
            console.print(f"[yellow]WHY:[/yellow]  {why}")
            console.print(f"[yellow]FIX:[/yellow]  {fix}")
        console.print(f"[dim]{output[-2000:]}[/dim]")

    if not passed:
        raise SystemExit(1)
