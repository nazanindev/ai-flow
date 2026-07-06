"""FlowOrchestrator — the multi-session engine behind the TUI.

Holds shared state (`__init__`) and the git-worktree helpers, and assembles the
behavior from three mixins:

- `_RunnerMixin`     (flow.runner)     — driving `claude -p` and the auto-pipeline
- `_WorkersMixin`    (flow.workers)    — session spawning and the worker threads
- `_DispatcherMixin` (flow.dispatcher) — the foundation-first fan-out algorithm
- `_ControlsMixin`   (flow.controls)   — the live table and interactive commands
"""
import re
import subprocess
import uuid
from pathlib import Path
from typing import List, Optional

from prompt_toolkit import PromptSession
from prompt_toolkit.history import FileHistory
from prompt_toolkit.styles import Style
from rich.console import Console

from flow.config import constraints, get_project_id, get_branch
from flow.session import AgentSession
from flow.runner import _RunnerMixin
from flow.workers import _WorkersMixin
from flow.dispatcher import _DispatcherMixin
from flow.controls import _ControlsMixin

console = Console()
HISTORY_PATH = Path.home() / ".autopilot" / "repl_history"
HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)


class FlowOrchestrator(_RunnerMixin, _WorkersMixin, _DispatcherMixin, _ControlsMixin):
    def __init__(self):
        self.project = get_project_id()
        self.branch = get_branch()
        self.sessions: List[AgentSession] = []
        self.model_override: Optional[str] = None
        self.no_agents = False
        c = constraints()
        self.auto_remediate = bool(c.get("auto_remediate", True))
        self.auto_remediate_max_tries = int(c.get("auto_remediate_max_tries", 2))
        self._api_spend_cache: float = 0.0
        self._sub_tokens_cache: int = 0
        self._api_spend_last_refresh: float = 0.0
        self.auto_verify = bool(c.get("auto_verify_on_steps_complete", True))
        self.auto_check = bool(c.get("auto_check_before_ship", True))
        self.prompt_session = PromptSession(
            history=FileHistory(str(HISTORY_PATH)),
            style=Style.from_dict({"prompt": "bold cyan"}),
        )

    # ── Git helpers ───────────────────────────────────────────────────────────

    def _get_default_branch(self, cwd: str = ".") -> str:
        """Detect the repo's default branch via origin/HEAD, falling back to common names."""
        r = subprocess.run(
            ["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"],
            capture_output=True, text=True, cwd=cwd,
        )
        if r.returncode == 0:
            ref = r.stdout.strip()
            if "/" in ref:
                return ref.split("/", 1)[1]
        for candidate in ("main", "master", "develop", "trunk"):
            r = subprocess.run(
                ["git", "rev-parse", "--verify", candidate],
                capture_output=True, text=True, cwd=cwd,
            )
            if r.returncode == 0:
                return candidate
        return "main"

    def _git_root(self) -> Path:
        try:
            r = subprocess.run(
                ["git", "rev-parse", "--show-toplevel"],
                capture_output=True, text=True, check=True,
            )
            return Path(r.stdout.strip())
        except subprocess.CalledProcessError:
            return Path.cwd()

    def _create_worktree(self, goal: str, base_branch: str = None) -> tuple:
        """Create a git worktree for a new session. Returns (path, branch_name).

        If base_branch is given, the new branch starts from that branch instead of HEAD.
        Used by the dispatcher to start feature agents from the foundation branch.
        """
        slug = re.sub(r"[^a-z0-9]+", "-", goal.lower())[:25].strip("-")
        name = f"flow-{slug}-{uuid.uuid4().hex[:4]}"
        git_root = self._git_root()
        worktree_dir = git_root / ".claude" / "worktrees"
        worktree_dir.mkdir(parents=True, exist_ok=True)
        cmd = ["git", "worktree", "add", str(worktree_dir / name), "-b", name]
        if base_branch:
            cmd.append(base_branch)
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            console.print("[yellow]Worktree creation failed — using main directory.[/yellow]")
            return git_root, self.branch
        return worktree_dir / name, name

    def _remove_worktree(self, session: AgentSession) -> None:
        if session.cwd == self._git_root():
            return
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(session.cwd)],
            capture_output=True, text=True,
        )
