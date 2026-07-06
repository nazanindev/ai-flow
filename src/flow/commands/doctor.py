"""flow doctor — verify Claude Code hook commands can import and run `flow` hooks."""
from __future__ import annotations

import json
import shlex
import subprocess
from typing import Any

from rich.console import Console

from flow.commands.init import (
    SETTINGS_PATH,
    _env_for_hook_subprocess,
    _iter_hook_commands,
    hook_interpreters_import_flow,
)

console = Console()


def _load_settings() -> dict[str, Any]:
    if not SETTINGS_PATH.exists():
        return {}
    try:
        return json.loads(SETTINGS_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def hook_health_ok() -> bool:
    """True when ~/.claude/settings.json hooks exist and their interpreter(s) can import `flow`."""
    if not SETTINGS_PATH.exists():
        return False
    hooks = _load_settings().get("hooks") or {}
    if not hooks:
        return False
    return hook_interpreters_import_flow(hooks)


def hook_health_one_liner() -> str:
    """Short status for `flow verify` / REPL banner (no subprocess pipe tests)."""
    if not SETTINGS_PATH.exists():
        return "Hook health: missing ~/.claude/settings.json — run `flow init`"
    hooks = _load_settings().get("hooks") or {}
    if not hooks:
        return "Hook health: no hooks in settings — run `flow init`"
    if not hook_interpreters_import_flow(hooks):
        return (
            "Hook health: FAIL — hook interpreter cannot import `flow` "
            "(run `flow doctor` or `flow init --force`)"
        )
    return "Hook health: OK"


def _hook_child_env(extra_env: dict[str, str] | None = None) -> dict[str, str]:
    env = _env_for_hook_subprocess()
    if extra_env:
        env.update(extra_env)
    return env


def _run_hook_stdin(cmd: str, stdin_payload: str, extra_env: dict[str, str] | None = None) -> tuple[int, str, str]:
    env = _hook_child_env(extra_env)
    try:
        proc = subprocess.run(
            shlex.split(cmd),
            input=stdin_payload,
            text=True,
            capture_output=True,
            timeout=60,
            env=env,
        )
        return proc.returncode, proc.stdout or "", proc.stderr or ""
    except (OSError, subprocess.TimeoutExpired) as e:
        return -1, "", str(e)


def cmd_doctor(fix: bool = False) -> None:
    """Print hook diagnostics; with --fix, rewrite ~/.claude/settings.json via flow init --force."""
    if fix:
        from flow.commands.init import cmd_init

        console.print("[dim]→ Rewriting hooks with `flow init --force`…[/dim]")
        cmd_init(force=True, repo=False)
        console.print("[green]✓ Fix applied. Re-running diagnostics below.[/green]\n")

    data = _load_settings()
    hooks = data.get("hooks") or {}

    if not SETTINGS_PATH.exists():
        console.print(f"[red]Missing {SETTINGS_PATH}[/red] — run `flow init` first.")
        raise SystemExit(1)

    if not hooks:
        console.print("[red]No hooks key in settings.[/red] Run `flow init`.")
        raise SystemExit(1)

    ok = True

    for hook_type, cmd in _iter_hook_commands(hooks):
        console.print(f"\n[bold]{hook_type}[/bold] [dim]{cmd}[/dim]")
        try:
            parts = shlex.split(cmd)
        except ValueError as e:
            console.print(f"  [red]Could not parse command: {e}[/red]")
            ok = False
            continue
        if not parts:
            console.print("  [red]Empty command[/red]")
            ok = False
            continue
        interp = parts[0]
        r = subprocess.run(
            [interp, "-c", "import flow"],
            capture_output=True,
            text=True,
            timeout=30,
            env=_env_for_hook_subprocess(),
        )
        if r.returncode != 0:
            console.print(f"  [red]✗ `{interp}` cannot import flow[/red]")
            if r.stderr.strip():
                console.print(f"  [dim]{r.stderr.strip()[:500]}[/dim]")
            ok = False
            continue

        console.print(f"  [green]✓ Interpreter imports flow[/green]")

        # Pipe smoke test per hook kind
        if "flow.hooks.pretool" in cmd:
            payload = json.dumps(
                {
                    "tool_name": "Bash",
                    "tool_input": {"command": "echo ok"},
                    "session_id": "doctor",
                }
            )
            code, out, err = _run_hook_stdin(cmd, payload + "\n", {"FLOW_ACTIVE": "1"})
            if code != 0:
                console.print(f"  [red]✗ pretool stdin smoke test exit {code}[/red]")
                if err.strip():
                    console.print(f"  [dim]{err.strip()[:500]}[/dim]")
                ok = False
            else:
                console.print("  [green]✓ PreToolUse smoke (Bash echo, FLOW_ACTIVE=1)[/green]")
        elif "flow.hooks.stop" in cmd:
            payload = json.dumps(
                {
                    "session_id": "doctor",
                    "model": "claude-sonnet-4-6",
                    "usage": {"input_tokens": 0, "output_tokens": 0},
                }
            )
            code, _out, err = _run_hook_stdin(cmd, payload + "\n", {"FLOW_HEADLESS": "1"})
            if code != 0:
                console.print(f"  [red]✗ stop hook exit {code}[/red]")
                if err.strip():
                    console.print(f"  [dim]{err.strip()[:500]}[/dim]")
                ok = False
            else:
                console.print("  [green]✓ Stop smoke (FLOW_HEADLESS=1)[/green]")
        elif "flow.hooks.posttool" in cmd:
            payload = json.dumps(
                {
                    "tool_name": "Write",
                    "tool_input": {},
                    "tool_response": {"output": "ok"},
                    "session_id": "doctor",
                }
            )
            code, _out, err = _run_hook_stdin(cmd, payload + "\n", {"FLOW_RUN_ID": "none"})
            if code != 0:
                console.print(f"  [red]✗ posttool exit {code}[/red]")
                if err.strip():
                    console.print(f"  [dim]{err.strip()[:500]}[/dim]")
                ok = False
            else:
                console.print("  [green]✓ PostToolUse smoke (FLOW_RUN_ID=none)[/green]")
        elif "flow.hooks.precompact" in cmd:
            code, out, err = _run_hook_stdin(cmd, "{}\n", {"FLOW_ACTIVE": "1"})
            if code != 0:
                console.print(f"  [red]✗ precompact exit {code}[/red]")
                if err.strip():
                    console.print(f"  [dim]{err.strip()[:500]}[/dim]")
                ok = False
            elif not out.strip().startswith("{"):
                console.print(f"  [yellow]? precompact stdout not JSON[/yellow]")
            else:
                console.print("  [green]✓ PreCompact smoke (FLOW_ACTIVE=1)[/green]")

    if ok:
        console.print("\n[green]✓ Doctor: all hook checks passed.[/green]")
    else:
        console.print("\n[red]✗ Doctor: one or more checks failed.[/red] Run [bold]flow doctor --fix[/bold] or [bold]flow init --force[/bold].")
        raise SystemExit(1)
