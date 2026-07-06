"""flow init — wire AI Flow hooks into ~/.claude/settings.json."""
import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterator

from rich.console import Console

from flow.config import STATE_DIR

console = Console()

SETTINGS_PATH = Path.home() / ".claude" / "settings.json"
FLOW_ENV_PATH = STATE_DIR / ".env"
FLOW_STYLE_PATH = STATE_DIR / "style.yaml"
FLOW_ENV_EXAMPLE = Path(__file__).parent.parent.parent.parent / ".env.example"


def _env_for_hook_subprocess() -> dict[str, str]:
    """Environment similar to Claude Code hook children (no repo PYTHONPATH hacks)."""
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    return env


def hooks_dict() -> dict[str, Any]:
    """Claude Code hook definitions using this install's Python (must be able to import `flow`)."""
    py = shlex.quote(sys.executable)
    return {
        "Stop": [
            {"hooks": [{"type": "command", "command": f"{py} -m flow.hooks.stop"}]}
        ],
        "PreToolUse": [
            {"matcher": "", "hooks": [{"type": "command", "command": f"{py} -m flow.hooks.pretool"}]}
        ],
        "PostToolUse": [
            {"matcher": "", "hooks": [{"type": "command", "command": f"{py} -m flow.hooks.posttool"}]}
        ],
        "PreCompact": [
            {"hooks": [{"type": "command", "command": f"{py} -m flow.hooks.precompact"}]}
        ],
    }


def _iter_hook_commands(hooks: dict[str, Any]) -> Iterator[tuple[str, str]]:
    for hook_type, configs in (hooks or {}).items():
        if not isinstance(configs, list):
            continue
        for cfg in configs:
            if not isinstance(cfg, dict):
                continue
            for h in cfg.get("hooks") or []:
                if not isinstance(h, dict):
                    continue
                cmd = h.get("command")
                if isinstance(cmd, str) and cmd.strip():
                    yield hook_type, cmd.strip()


def hook_interpreters_import_flow(hooks: dict[str, Any]) -> bool:
    """True if every configured hook command's leading interpreter can `import flow`."""
    seen: set[str] = set()
    for _hook_type, cmd in _iter_hook_commands(hooks):
        try:
            parts = shlex.split(cmd)
        except ValueError:
            return False
        if not parts:
            return False
        interp = parts[0]
        if interp in seen:
            continue
        seen.add(interp)
        try:
            r = subprocess.run(
                [interp, "-c", "import flow"],
                capture_output=True,
                text=True,
                timeout=30,
                env=_env_for_hook_subprocess(),
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        if r.returncode != 0:
            return False
    return bool(seen)


DEFAULT_STYLE = """\
# AI Flow style — controls AI voice across all outputs.
# Set any section to null (or delete it) to skip that style injection entirely.

agent:
  verbosity: concise          # concise | verbose
  emoji: false
  confirm_before_destructive: true

commit_message:
  format: "short, imperative, no label prefix"
  # e.g. "add JWT middleware" not "feat: add JWT middleware"
  max_length: 72

pr_title:
  format: "plain description, sentence case, no prefix brackets"
  # e.g. "Add rate limiting to the API" not "[Feature] add rate limiting"

pr_body: |
  ## What
  {what}

  ## Why
  {why}

  ## Checklist
  - [ ] tests pass
  - [ ] no new dependencies without justification

ci_review:
  tone: "direct, no filler, flag real issues only"
  severity_labels: [blocker, suggestion, nit]
  skip_nitpicks: false
"""

REPO_FEATURES_TEMPLATE = """\
features:
  - id: F01
    behavior: "Describe a concrete behavior"
    verification: "pytest tests/path_or_command -x"
    state: "not_started"
    evidence: ""
    blocked_reason: ""
"""

REPO_PROGRESS_TEMPLATE = """\
# Progress

## Completed
- [ ] Nothing yet

## In Progress
- [ ] Select active feature with `flow features pick`

## Blocked
- (none)

## Next
1. Keep WIP=1
2. Verify before marking passing
"""


def cmd_init(force: bool = False, repo: bool = False) -> None:
    # ── Ensure ~/.flow exists ─────────────────────────────────────────────────
    FLOW_ENV_PATH.parent.mkdir(parents=True, exist_ok=True)

    if not FLOW_ENV_PATH.exists():
        shutil.copy(FLOW_ENV_EXAMPLE, FLOW_ENV_PATH)
        console.print(
            f"[yellow]Created {FLOW_ENV_PATH} — fill in your API key and set FLOW_PLAN "
            f"(pro|max5|max20|api_only).[/yellow]"
        )
    else:
        console.print(f"[dim]Env file already exists: {FLOW_ENV_PATH}[/dim]")

    if not FLOW_STYLE_PATH.exists():
        FLOW_STYLE_PATH.write_text(DEFAULT_STYLE)
        console.print(f"[green]✓ Created {FLOW_STYLE_PATH}[/green]")
    else:
        console.print(f"[dim]Style file already exists: {FLOW_STYLE_PATH}[/dim]")

    # ── Read existing Claude Code settings ───────────────────────────────────
    settings: dict[str, Any] = {}
    if SETTINGS_PATH.exists():
        with open(SETTINGS_PATH) as f:
            try:
                settings = json.load(f)
            except json.JSONDecodeError:
                settings = {}

    existing_hooks = settings.get("hooks") or {}
    hooks_ok = hook_interpreters_import_flow(existing_hooks) if existing_hooks else False
    broken = bool(existing_hooks) and not hooks_ok

    if existing_hooks and not force and not broken:
        console.print(
            "[yellow]Hooks already configured in ~/.claude/settings.json.[/yellow]\n"
            "Run [bold]flow init --force[/bold] to overwrite."
        )
        _show_status(settings)
        _install_git_post_merge_hook()
        if repo:
            _scaffold_repo_artifacts()
        return

    if broken and not force:
        console.print(
            "[yellow]Hook commands cannot import `flow` with their configured interpreter "
            "(often `python3` ≠ the Python where flow is installed). Rewriting hooks to use "
            f"this install: {sys.executable}[/yellow]"
        )

    settings["hooks"] = hooks_dict()
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(SETTINGS_PATH, "w") as f:
        json.dump(settings, f, indent=2)

    console.print("[green]✓ Hooks wired into ~/.claude/settings.json[/green]")
    hd = hooks_dict()
    for hook_type, configs in hd.items():
        for cfg in configs:
            for h in cfg.get("hooks", []):
                console.print(f"[dim]  {hook_type}:[/dim] {h.get('command', '')}")
    console.print(f"\n[dim]Next: add your API keys to {FLOW_ENV_PATH}[/dim]")

    _install_git_post_merge_hook()

    if repo:
        _scaffold_repo_artifacts()


def _show_status(settings: dict) -> None:
    hooks = settings.get("hooks", {})
    for hook_type, configs in hooks.items():
        for cfg in configs:
            for h in cfg.get("hooks", []):
                console.print(f"  [dim]{hook_type}:[/dim] {h.get('command', '')}")


def _scaffold_repo_artifacts() -> None:
    """Create repo-local harness artifacts when missing."""
    cwd = Path.cwd()
    features_path = cwd / "features.yaml"
    progress_dir = cwd / "docs"
    progress_path = progress_dir / "PROGRESS.md"
    agents_path = cwd / "AGENTS.md"

    if not features_path.exists():
        features_path.write_text(REPO_FEATURES_TEMPLATE)
        console.print(f"[green]✓ Created {features_path}[/green]")
    else:
        console.print(f"[dim]Repo artifact exists: {features_path}[/dim]")

    progress_dir.mkdir(parents=True, exist_ok=True)
    if not progress_path.exists():
        progress_path.write_text(REPO_PROGRESS_TEMPLATE)
        console.print(f"[green]✓ Created {progress_path}[/green]")
    else:
        console.print(f"[dim]Repo artifact exists: {progress_path}[/dim]")

    if not agents_path.exists():
        agents_path.write_text(
            "# AGENTS.md\n\n"
            "Project-level agent instructions.\n\n"
            "- Use `flow features pick` before implementation.\n"
            "- Keep one active feature at a time.\n"
            "- Verify before marking features passing.\n"
        )
        console.print(f"[green]✓ Created {agents_path}[/green]")
    else:
        console.print(f"[dim]Repo artifact exists: {agents_path}[/dim]")


def _install_git_post_merge_hook() -> None:
    """Install .git/hooks/post-merge hook to auto-close merged PR runs."""
    git_dir = Path.cwd() / ".git"
    hooks_dir = git_dir / "hooks"
    if not hooks_dir.exists():
        console.print("[dim]Skipping git post-merge hook install (not in a git worktree).[/dim]")
        return

    hook_path = hooks_dir / "post-merge"
    py = shlex.quote(sys.executable)
    hook_body = (
        "#!/bin/sh\n"
        "# AI Flow: auto-close active run when linked PR is merged.\n"
        f"PYTHONPATH=src {py} -m flow.hooks.postmerge || {py} -m flow.hooks.postmerge\n"
    )

    if hook_path.exists():
        existing = hook_path.read_text()
        if "flow.hooks.postmerge" in existing:
            console.print(f"[dim]Git hook already configured: {hook_path}[/dim]")
            return
        hook_path.write_text(existing.rstrip() + "\n\n" + hook_body)
    else:
        hook_path.write_text(hook_body)

    hook_path.chmod(0o755)
    console.print(f"[green]✓ Installed git post-merge hook: {hook_path}[/green]")
