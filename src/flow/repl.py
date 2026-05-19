"""
AI Flow Orchestrator — multi-agent TUI with drill-down.

Type a task → runs in a background thread with its own git worktree.
Multiple tasks run simultaneously. /view N to drill into any session.
"""
import json
import os
import queue
import re
import shlex
import subprocess
import sys
import threading
import time
import uuid
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

warnings.filterwarnings("ignore", message=".*urllib3 v2 only supports OpenSSL.*", category=UserWarning)

from prompt_toolkit import PromptSession
from prompt_toolkit.history import FileHistory
from prompt_toolkit.styles import Style
from rich.console import Console
from rich.table import Table

from flow.config import DB_PATH, constraints, get_project_id, get_branch, get_plan, get_plan_window_caps
from flow.router import MODEL_ALIASES, model_for
from flow.tracker import (
    Phase, RunState, RunStatus, init_db, load_active_run, load_run, save_run,
    get_api_spend_today, get_window_usage,
)
from flow.run_manager import (
    advance_phase, complete_plan_step, get_session_briefing,
    set_plan_steps, store_check_result, save_pr_url,
)
from flow.session_accounting import account_claude_code_session_end, usage_from_claude_result
from flow.context import phase_directive
from flow.observe import trace_run_started


console = Console()
HISTORY_PATH = Path.home() / ".autopilot" / "repl_history"
HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)


def _parse_claude_json_stdout(raw_out: str) -> Optional[Dict[str, Any]]:
    raw_out = (raw_out or "").strip()
    if not raw_out:
        return None
    try:
        return json.loads(raw_out)
    except json.JSONDecodeError:
        pass
    for line in reversed(raw_out.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    return None


@dataclass
class AgentSession:
    idx: int
    goal: str
    run: Any                          # RunState — owned by session worker thread
    project: str
    branch: str
    cwd: Path
    session_type: str = "executor"    # "executor" | "planner" | "reviewer"
    model_override: Optional[str] = None
    auto_ship: bool = True            # False skips the ship step (used by test sessions)
    thread: Optional[threading.Thread] = None
    output_queue: queue.Queue = field(default_factory=queue.Queue)
    output_history: List[str] = field(default_factory=list)
    inject_queue: queue.Queue = field(default_factory=queue.Queue)
    lock: threading.Lock = field(default_factory=threading.Lock)
    status: str = "running"           # "running" | "done" | "failed"
    last_line: str = ""
    pr_url: str = ""
    started_at: float = field(default_factory=time.monotonic)
    waiting_for_input: bool = False   # planner is paused, needs /prompt
    _turn_depth: int = field(default=0, compare=False)  # recursion guard for _run_turn


class FlowOrchestrator:
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
            console.print(f"[yellow]Worktree creation failed — using main directory.[/yellow]")
            return git_root, self.branch
        return worktree_dir / name, name

    def _remove_worktree(self, session: AgentSession) -> None:
        if session.cwd == self._git_root():
            return
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(session.cwd)],
            capture_output=True, text=True,
        )

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

    # ── Session output routing ────────────────────────────────────────────────

    def _session_push(self, session: AgentSession, text: str) -> None:
        """Thread-safe: route output chunk to session queue and update last_line."""
        if not text:
            return
        session.output_queue.put(text)
        with session.lock:
            stripped = text.strip()
            if stripped:
                session.last_line = stripped[-100:]

    def _drain_queues(self) -> None:
        """Main-thread: drain all session queues into output_history."""
        for session in self.sessions:
            while True:
                try:
                    chunk = session.output_queue.get_nowait()
                    session.output_history.append(chunk)
                except queue.Empty:
                    break

    # ── Session lifecycle ─────────────────────────────────────────────────────

    def _start_session(self, goal: str, base_branch: str = None) -> AgentSession:
        # Parse session type from prefix: "plan: ..." | "review: ..." | default executor
        session_type = "executor"
        model_override = None
        display_goal = goal

        lower = goal.lower()
        if lower.startswith("plan:") or lower.startswith("plan "):
            session_type = "planner"
            display_goal = goal[5:].strip()
            model_override = "claude-opus-4-7"
        elif lower.startswith("review:") or lower.startswith("review "):
            session_type = "reviewer"
            display_goal = goal[7:].strip()
            model_override = "claude-haiku-4-5-20251001"
        elif lower.startswith("dispatch:") or lower.startswith("dispatch "):
            session_type = "dispatcher"
            display_goal = goal[9:].strip()
            model_override = "claude-opus-4-7"

        # Dispatcher and reviewer sessions don't need an isolated worktree
        if session_type in ("reviewer", "dispatcher"):
            cwd = self._git_root()
            branch = self.branch
        else:
            cwd, branch = self._create_worktree(display_goal, base_branch=base_branch)

        init_db()
        run = RunState(goal=display_goal, project=self.project, branch=branch)
        save_run(run)
        trace_run_started(run.run_id, run.project, run.branch, display_goal)

        idx = len(self.sessions) + 1
        session = AgentSession(
            idx=idx, goal=display_goal, run=run,
            project=self.project, branch=branch, cwd=cwd,
            session_type=session_type,
            model_override=model_override or self.model_override,
        )
        session.thread = threading.Thread(
            target=self._session_worker, args=(session,), daemon=True,
        )
        self.sessions.append(session)
        session.thread.start()
        return session

    def _session_worker(self, session: AgentSession) -> None:
        try:
            if session.session_type == "planner":
                self._planner_worker(session)
            elif session.session_type == "reviewer":
                self._reviewer_worker(session)
            elif session.session_type == "dispatcher":
                self._dispatcher_worker(session)
            else:
                self._executor_worker(session)
        except SystemExit:
            with session.lock:
                if session.status == "running":
                    session.status = "done"
            from flow.tracker import set_run_status, RunStatus
            set_run_status(session.run.run_id, RunStatus.complete)
        except Exception as e:
            with session.lock:
                session.status = "failed"
                session.last_line = str(e)[:100]
            from flow.tracker import set_run_status, RunStatus
            set_run_status(session.run.run_id, RunStatus.failed)
            self._remove_worktree(session)
        else:
            # Worker returned normally — reviewer and dispatcher don't always call
            # set_run_status themselves, so ensure DB is consistent here.
            from flow.tracker import set_run_status, RunStatus
            with session.lock:
                if session.status == "running":
                    session.status = "done"
            set_run_status(session.run.run_id, RunStatus.complete)

    def _executor_worker(self, session: AgentSession) -> None:
        """Standard pipeline: plan → execute → verify → fix → ship."""
        self._run_turn(session.goal, session)
        # Only drain injected messages if the auto-pipeline didn't already finish
        # the session — otherwise each queued message spawns a spurious Claude turn.
        with session.lock:
            still_running = session.status == "running"
        if still_running:
            self._drain_inject(session)
        # Force pipeline if Claude made changes but didn't emit all STEP_DONE markers
        with session.lock:
            still_running = session.status == "running"
        if still_running:
            r = subprocess.run(
                ["git", "status", "--porcelain"],
                capture_output=True, text=True, cwd=str(session.cwd),
            )
            if r.stdout.strip():
                self._run_pipeline(session)
        from flow.tracker import set_run_status, RunStatus
        with session.lock:
            if session.status == "running":
                session.status = "done"
        set_run_status(session.run.run_id, RunStatus.complete)

    def _planner_worker(self, session: AgentSession) -> None:
        """Interactive planning session: runs forever, responds to /prompt N."""
        self._run_turn(session.goal, session)
        self._session_push(session, "\n[planner] Waiting — use /view to reply\n")
        with session.lock:
            session.waiting_for_input = True
        while True:
            with session.lock:
                if session.status != "running":
                    return
            try:
                msg = session.inject_queue.get(timeout=0.5)
                with session.lock:
                    session.waiting_for_input = False
                self._session_push(session, f"\n→ [prompt] {msg}\n")
                self._run_turn(msg, session)
                self._session_push(session, "\n[planner] Waiting — use /view to reply\n")
                with session.lock:
                    session.waiting_for_input = True
            except queue.Empty:
                continue

    def _reviewer_worker(self, session: AgentSession) -> None:
        """Subscription-based code review via Claude Code (read-only tools)."""
        target = session.goal.strip() or "HEAD"
        self._session_push(session, f"→ Reviewing {target}...\n")

        # Get diff — prefer gh pr diff when a PR URL is set
        diff = ""
        if session.pr_url:
            pr_num = session.pr_url.rstrip("/").split("/")[-1]
            r = subprocess.run(
                ["gh", "pr", "diff", pr_num],
                capture_output=True, text=True, cwd=str(session.cwd),
            )
            if r.returncode == 0 and r.stdout.strip():
                diff = r.stdout

        if not diff:
            default_branch = self._get_default_branch(str(session.cwd))
            for diff_args in (
                ["diff", f"{default_branch}...{target}"],
                ["diff", target],
                ["diff", "HEAD"],
            ):
                r = subprocess.run(
                    ["git"] + diff_args,
                    capture_output=True, text=True, cwd=str(session.cwd),
                )
                if r.returncode == 0 and r.stdout.strip():
                    diff = r.stdout
                    break

        if not diff.strip():
            self._session_push(session, "No diff found — nothing to review.\n")
            with session.lock:
                session.status = "done"
            return

        pr_hint = f"\nPR: {session.pr_url}" if session.pr_url else ""
        review_prompt = f"""You are a thorough, skeptical code reviewer. Find real problems — not style nitpicks.

Read the diff carefully. Use Read, Grep, and Glob to explore relevant files and understand context before forming conclusions. Look at callers, tests, and related modules.{pr_hint}

**Flag only real issues:**
- Bugs: null/undefined access, wrong comparisons, off-by-one, type mismatches, missing awaits
- Security: unvalidated input, injection risks, auth bypass, exposed secrets, missing CSRF, path traversal
- Missing error handling: uncaught exceptions, unhandled rejections, missing null checks, swallowed errors
- Race conditions: shared mutable state without locks, TOCTOU vulnerabilities
- Logic errors: wrong conditions, incorrect algorithm, silent data loss, broken edge cases
- Broken contracts: API changes without updating callers, schema changes without migrations, removed exports

**Do not flag:** style, formatting, variable naming, line length, import order, minor refactors, personal preference.

**For each real issue, write:**
`[BLOCKER]` `file:line` — clear description of the problem and why it matters.
Suggested fix: specific, actionable recommendation.

If there's nothing real to flag: write "LGTM — no issues found."

---

Diff to review:
```diff
{diff[:14000]}
```
"""

        self._launch_claude(
            "", session,
            override_message=review_prompt,
            allowed_tools=["Read", "Grep", "Glob"],
            ap_active=False,
        )

    def _dispatcher_worker(self, session: AgentSession) -> None:
        """Plan via Opus, then spawn sub-agents for each task in the plan."""
        import anthropic
        from flow.billing import metered_call
        from flow.tracker import save_event, set_run_status, RunStatus, activity_path

        c = constraints()
        max_spawn = int(c.get("dispatcher_max_spawn", 4))
        COORD_MODEL = "claude-opus-4-7"
        run_id = session.run.run_id
        project = session.run.project

        def _ev(event_type: str, metadata: dict = None) -> None:
            save_event(run_id, event_type, project=project, phase="coordinate", metadata=metadata)

        def _activity(msg: str) -> None:
            try:
                import time as _t
                activity_path(run_id).write_text(
                    json.dumps({"tool": msg, "ts": _t.time(), "phase": "coordinate", "event_id": ""})
                )
            except Exception:
                pass

        self._session_push(session, f"→ Dispatching: {session.goal}\n")
        _ev("dispatcher_started", {"goal": session.goal, "max_spawn": max_spawn})
        _activity("planning")

        system = (
            "You are a task dispatcher for an AI coding harness. "
            "Each task runs as an isolated agent in its own git worktree and opens a PR. "
            "PRs are merged sequentially — merge conflicts destroy the output.\n\n"
            "To prevent conflicts, use a FOUNDATION-FIRST pattern when tasks share infrastructure:\n"
            "- 'foundation': one task that creates ALL shared files (main.py, database.py, "
            "requirements.txt, config, base models, folder structure). Set to null if not needed.\n"
            "- 'tasks': feature agents that run IN PARALLEL after the foundation is done. "
            "Each task must only create/modify its own files — never shared infrastructure.\n\n"
            "Output ONLY valid JSON:\n"
            '{"foundation": {"goal": "..."} | null, '
            '"tasks": [{"goal": "...", "type": "executor|planner|reviewer", "owns": ["path/file.py"]}]}\n\n'
            f"Rules:\n"
            f"- Maximum {max_spawn} feature tasks (foundation is additional)\n"
            "- executor: full pipeline (plan→execute→verify→ship)\n"
            "- planner: interactive planning/architecture only\n"
            "- reviewer: one-shot code review\n"
            "- No two tasks may own the same file\n"
            "- Foundation goal must explicitly list every shared file it creates\n"
            "- Each feature task goal must say: 'Assume <shared files> exist. Only create <owned files>.'\n"
            "- Output JSON only, no markdown"
        )

        try:
            client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY", ""))
            msg = metered_call(
                client, COORD_MODEL,
                run_id=run_id,
                purpose="dispatcher-plan",
                max_tokens=4096,
                system=system,
                messages=[{"role": "user", "content": session.goal}],
            )
            raw = msg.content[0].text if msg.content else ""
            self._session_push(session, f"{raw}\n")

            if msg.stop_reason == "max_tokens":
                self._session_push(session, "✗ Dispatcher plan truncated — spawn plan too long for token budget\n")
                _ev("dispatcher_failed", {"reason": "max_tokens", "raw": raw[:500]})
                set_run_status(run_id, RunStatus.failed)
                with session.lock:
                    session.status = "failed"
                return

            match = re.search(r'\{.*\}', raw, re.DOTALL)
            if not match:
                self._session_push(session, "✗ No JSON plan found in response\n")
                _ev("dispatcher_failed", {"reason": "no_json", "raw": raw[:500]})
                set_run_status(run_id, RunStatus.failed)
                with session.lock:
                    session.status = "failed"
                return

            plan = json.loads(match.group())
            tasks = [t for t in plan.get("tasks", []) if t.get("goal", "").strip()][:max_spawn]
            foundation_spec = plan.get("foundation")

            _ev("dispatcher_plan_complete", {
                "has_foundation": bool(foundation_spec),
                "task_count": len(tasks),
                "tasks": [{"goal": t["goal"][:80], "type": t.get("type", "executor")} for t in tasks],
            })

            if not tasks and not foundation_spec:
                self._session_push(session, "✗ Empty plan\n")
                _ev("dispatcher_failed", {"reason": "empty_plan"})
                set_run_status(run_id, RunStatus.failed)
                with session.lock:
                    session.status = "failed"
                return

            # Budget gate
            api_gate = float(os.getenv("AP_BUDGET_USD") or c.get("api_spend_gate_usd", 1.0))
            api_today = get_api_spend_today(self.project)
            if api_today >= api_gate:
                self._session_push(
                    session,
                    f"✗ API spend gate reached (${api_today:.4f} ≥ ${api_gate:.2f}) — not spawning\n",
                )
                _ev("dispatcher_blocked", {"reason": "budget_gate", "spend": api_today, "gate": api_gate})
                set_run_status(run_id, RunStatus.blocked)
                with session.lock:
                    session.status = "failed"
                return

            # ── Phase 1: foundation ───────────────────────────────────────────
            feature_base_branch = None
            if foundation_spec:
                foundation_goal = foundation_spec.get("goal", "").strip()
                self._session_push(session, f"\n→ Phase 1 — foundation: {foundation_goal[:70]}\n")
                _activity("spawning foundation")
                foundation_session = self._start_session(foundation_goal)
                _ev("dispatcher_spawn", {
                    "sub_run_id": foundation_session.run.run_id,
                    "type": "foundation",
                    "goal": foundation_goal[:80],
                })
                self._session_push(session, f"  [{foundation_session.idx}] foundation spawned — waiting…\n")

                # Poll until foundation is done or failed (10 min timeout)
                deadline = time.monotonic() + 600
                while time.monotonic() < deadline:
                    with foundation_session.lock:
                        st = foundation_session.status
                    if st == "done":
                        feature_base_branch = foundation_session.branch
                        self._session_push(session, f"  ✓ foundation complete (branch: {feature_base_branch})\n")
                        _ev("dispatcher_foundation_done", {"branch": feature_base_branch})
                        break
                    elif st == "failed":
                        self._session_push(session, "  ✗ foundation failed — aborting feature spawn\n")
                        _ev("dispatcher_failed", {"reason": "foundation_failed"})
                        set_run_status(run_id, RunStatus.failed)
                        with session.lock:
                            session.status = "failed"
                        return
                    _activity("waiting for foundation")
                    time.sleep(3)
                else:
                    self._session_push(session, "  ✗ foundation timed out\n")
                    _ev("dispatcher_failed", {"reason": "foundation_timeout"})
                    set_run_status(run_id, RunStatus.failed)
                    with session.lock:
                        session.status = "failed"
                    return

            # ── Phase 2: feature agents ───────────────────────────────────────
            if tasks:
                self._session_push(session, f"\n→ Phase 2 — spawning {len(tasks)} feature agents"
                    + (f" from {feature_base_branch}" if feature_base_branch else "") + "\n")
                _activity("spawning features")
                prefix_map = {"planner": "plan:", "reviewer": "review:", "executor": ""}
                for t in tasks:
                    goal_text = t["goal"].strip()
                    task_type = t.get("type", "executor")
                    owns = t.get("owns", [])
                    if owns:
                        owns_str = ", ".join(owns[:6])
                        goal_text = (f"{goal_text}\n\nFile ownership: only create/modify "
                                     f"[{owns_str}]. Do not recreate shared infrastructure.")
                    prefix = prefix_map.get(task_type, "")
                    full_goal = f"{prefix} {goal_text}".strip() if prefix else goal_text
                    sub = self._start_session(full_goal, base_branch=feature_base_branch)
                    _ev("dispatcher_spawn", {
                        "sub_run_id": sub.run.run_id, "type": task_type,
                        "goal": t["goal"][:80], "owns": owns,
                        "base_branch": feature_base_branch,
                    })
                    self._session_push(session, f"  [{sub.idx}] {task_type}: {t['goal'][:60]}\n")

            _ev("dispatcher_done", {"foundation": bool(foundation_spec), "spawned": len(tasks)})
            set_run_status(run_id, RunStatus.complete)
            with session.lock:
                session.status = "done"

        except Exception as e:
            self._session_push(session, f"✗ Coordinator error: {e}\n")
            _ev("dispatcher_failed", {"reason": "exception", "error": str(e)[:200]})
            set_run_status(run_id, RunStatus.failed)
            with session.lock:
                session.status = "failed"

    def _drain_inject(self, session: AgentSession) -> None:
        """Process any queued /prompt messages after the current turn."""
        while True:
            try:
                msg = session.inject_queue.get_nowait()
                self._session_push(session, f"\n→ [prompt injected] {msg}\n")
                self._run_turn(msg, session)
            except queue.Empty:
                break

    # ── Plan helpers ──────────────────────────────────────────────────────────

    def _parse_numbered_plan_steps(self, text: str) -> list:
        steps = []
        for line in (text or "").splitlines():
            m = re.match(
                r"^\s*(?:\*\*)?\s*(?:step\s*)?(\d+)(?:\s*\*\*)?\s*(?:[.)]|:|—|-)\s+(.+)$",
                line, flags=re.IGNORECASE,
            )
            if m:
                steps.append({"id": m.group(1), "description": m.group(2).strip(), "status": "pending"})
        return steps

    def _parse_plan_from_file(self, max_age_s: float = 120.0) -> list:
        """Scan ~/.claude/plans/ for a recently written plan file and extract numbered steps.

        Called as a fallback when Claude wrote to a plan file instead of outputting steps
        in its response text. Only considers files written within the last max_age_s seconds
        to avoid picking up stale plans from previous sessions.
        """
        plans_dir = Path.home() / ".claude" / "plans"
        if not plans_dir.is_dir():
            return []
        now = time.time()
        candidates = [
            p for p in plans_dir.iterdir()
            if p.suffix in (".md", ".txt", "") and p.is_file()
            and (now - p.stat().st_mtime) <= max_age_s
        ]
        if not candidates:
            return []
        # Most recently modified wins
        plan_file = max(candidates, key=lambda p: p.stat().st_mtime)
        try:
            text = plan_file.read_text(encoding="utf-8")
        except Exception:
            return []
        return self._parse_numbered_plan_steps(text)

    def _extract_step_done_ids(self, text: str) -> list:
        results = []
        pattern = re.compile(
            r"^\s*STEP_DONE\s*:\s*(\d+)"
            r"(?:\s+\[evidence:\s*([^\]]+)\])?\s*$",
            flags=re.IGNORECASE,
        )
        for line in (text or "").splitlines():
            m = pattern.match(line.strip())
            if m:
                results.append((m.group(1), (m.group(2) or "").strip()))
        return results

    # ── Turn execution ────────────────────────────────────────────────────────

    def _run_turn(self, task: str, session: AgentSession) -> str:
        session._turn_depth += 1
        if session._turn_depth > 8:
            session._turn_depth -= 1
            self._session_push(session, "✗ Pipeline recursion limit reached — stopping.\n")
            with session.lock:
                session.status = "failed"
            return ""
        try:
            return self._run_turn_inner(task, session)
        finally:
            session._turn_depth -= 1

    def _run_turn_inner(self, task: str, session: AgentSession) -> str:
        response_text = self._launch_claude(task, session)

        prev_phase = session.run.phase
        updated = load_run(session.run.run_id)
        if updated:
            with session.lock:
                session.run = updated

        # Fallback plan step parsing if ExitPlanMode wasn't called
        if session.run.phase == Phase.plan and not session.run.plan_steps:
            parsed = self._parse_numbered_plan_steps(response_text) if response_text else []
            if not parsed:
                # Claude wrote to a .claude/plans/ file instead of outputting numbered steps —
                # scan for a recently modified plan file and extract steps from it.
                parsed = self._parse_plan_from_file()
                if parsed:
                    self._session_push(session, "⚠ Plan was written to file — extracting steps automatically\n")
            if parsed:
                set_plan_steps(session.run, parsed)
                updated = load_run(session.run.run_id)
                if updated:
                    with session.lock:
                        session.run = updated
                advance_phase(session.run, Phase.execute)
                with session.lock:
                    session.run.phase = Phase.execute
                self._session_push(session, "✓ Plan captured — executing\n")
            else:
                # No numbered steps found — re-prompt once explicitly
                self._session_push(session, "⚠ No plan steps found — re-prompting for numbered plan\n")
                retry_text = self._launch_claude(
                    "Output a numbered plan for the task above. "
                    "Format each step as: '1. description', '2. description', etc. "
                    "One step per line. No prose before or after.",
                    session,
                )
                parsed = self._parse_numbered_plan_steps(retry_text) if retry_text else []
                if not parsed:
                    parsed = self._parse_plan_from_file()
                if parsed:
                    set_plan_steps(session.run, parsed)
                    updated = load_run(session.run.run_id)
                    if updated:
                        with session.lock:
                            session.run = updated
                    advance_phase(session.run, Phase.execute)
                    with session.lock:
                        session.run.phase = Phase.execute
                    self._session_push(session, "✓ Plan captured — executing\n")
                else:
                    self._session_push(session, "✗ No plan produced — session ending without work\n")
                    with session.lock:
                        session.status = "failed"
                    from flow.tracker import set_run_status, RunStatus
                    set_run_status(session.run.run_id, RunStatus.failed)
                    return response_text

        # Detect STEP_DONE markers in execute phase
        if session.run.phase == Phase.execute and session.run.plan_steps and response_text:
            marked = 0
            known_ids = {str(s.get("id")) for s in session.run.plan_steps}
            for step_id, _ in self._extract_step_done_ids(response_text):
                if step_id in known_ids:
                    complete_plan_step(session.run, step_id)
                    for step in session.run.plan_steps:
                        if str(step.get("id")) == step_id:
                            step["status"] = "done"
                            break
                    marked += 1
            if marked:
                done = sum(1 for s in session.run.plan_steps if s.get("status") == "done")
                total = len(session.run.plan_steps)
                self._session_push(session, f"✓ Steps done ({done}/{total})\n")

        api_today = get_api_spend_today(session.project)
        self._session_push(
            session,
            f"API: ${session.run.cost_usd:.4f} this run / ${api_today:.4f} today\n",
        )

        # Auto-advance plan → execute
        if prev_phase == Phase.plan and session.run.phase == Phase.execute and session.run.plan_steps:
            self._session_push(session, "→ Starting execution...\n")
            self._run_turn("Begin executing the first pending plan step now.", session)
            return response_text

        # Auto-pipeline when all steps complete
        if (
            session.run.phase == Phase.execute
            and session.run.plan_steps
            and all(s.get("status") == "done" for s in session.run.plan_steps)
            and self.auto_verify
        ):
            self._run_pipeline(session)

        return response_text

    def _run_pipeline(self, session: AgentSession) -> None:
        from flow.commands.verify import run_checks

        self._session_push(session, "\n→ Running verification...\n")
        passed, output = run_checks(cwd=session.cwd)

        if not passed:
            if self.auto_remediate:
                ok = self._auto_remediate_verify(output, self.auto_remediate_max_tries, session)
                if not ok:
                    self._session_push(session, "✗ Verification still failing — fix manually.\n")
                    with session.lock:
                        session.status = "failed"
                    return
            else:
                self._session_push(session, f"✗ Verification failed\n{output[-1500:]}\n")
                with session.lock:
                    session.status = "failed"
                return
        self._session_push(session, "✓ Verification passed\n")

        if self.auto_check and os.getenv("ANTHROPIC_API_KEY"):
            self._session_push(session, "→ Running code review...\n")
            try:
                diff_result = subprocess.run(
                    ["git", "diff", "HEAD"],
                    capture_output=True, text=True, cwd=str(session.cwd),
                )
                from flow.commands.check import run_check
                report = run_check(diff_text=diff_result.stdout or None, run_id=session.run.run_id)
                blockers = int(report.get("blocker_count", 0))
                overall = report.get("overall", "?")
                self._session_push(
                    session,
                    f"Review: {overall} ({blockers} blocker{'s' if blockers != 1 else ''})\n",
                )
                store_check_result(session.run, json.dumps(report))
                if blockers > 0:
                    if self.auto_remediate:
                        ok = self._auto_remediate_check(report, self.auto_remediate_max_tries, session)
                        if not ok:
                            self._session_push(session, "✗ Code review blockers remain — fix manually.\n")
                            with session.lock:
                                session.status = "failed"
                            return
                    else:
                        self._session_push(session, "✗ Code review found blockers\n")
                        with session.lock:
                            session.status = "failed"
                        return
                self._session_push(session, "✓ Code review passed\n")
            except Exception as e:
                self._session_push(session, f"Code review skipped: {e}\n")

        if not session.auto_ship:
            elapsed = time.monotonic() - session.started_at
            self._session_push(session, f"✓ Test complete in {elapsed:.0f}s — ship skipped\n")
            with session.lock:
                session.last_line = f"✓ passed in {elapsed:.0f}s"
            return

        self._session_push(session, "→ Shipping...\n")
        ship_env = {**os.environ, "AP_ACTIVE": "0"}
        ship_result = subprocess.run(
            ["flow", "ship"],
            cwd=str(session.cwd),
            capture_output=True, text=True,
            env=ship_env,
        )
        ship_output = (ship_result.stdout + ship_result.stderr).strip()
        self._session_push(session, ship_output + "\n")

        pr_match = re.search(r"https?://github\.com/\S+/pull/\d+", ship_output)
        if pr_match:
            with session.lock:
                session.pr_url = pr_match.group(0)
                session.last_line = f"PR: {session.pr_url}"

        c = constraints()
        review_mode = c.get("auto_review", "local")
        if review_mode in ("local", "both"):
            self._spawn_reviewer(session)
        if review_mode in ("gh", "both") and session.pr_url and os.getenv("ANTHROPIC_API_KEY"):
            self._ci_review(session.pr_url, session)

    def _ci_review(self, pr_url: str, session: AgentSession) -> None:
        """Run flow ci-review --pr N after ship, post findings to GH, show summary in TUI."""
        if not os.getenv("ANTHROPIC_API_KEY"):
            return
        pr_num = pr_url.rstrip("/").split("/")[-1]
        self._session_push(session, f"→ CI review on PR #{pr_num}...\n")
        result = subprocess.run(
            ["flow", "ci-review", "--pr", pr_num],
            cwd=str(session.cwd),
            capture_output=True, text=True,
            env={**os.environ, "AP_ACTIVE": "0"},
        )
        output = (result.stdout + result.stderr).strip()
        # Strip ANSI escape codes so TUI RichLog shows clean text
        ansi_re = re.compile(r"\x1b\[[0-9;]*[mGKH]")
        clean = ansi_re.sub("", output)
        # Surface the most informative lines: found count + final verdict
        useful = [
            l for l in clean.splitlines()
            if any(kw in l for kw in ("found:", "blocker", "posted", "Looks good", "##", "Review"))
        ]
        summary = "\n".join(useful[-6:]) if useful else clean[-300:]
        self._session_push(session, summary + "\n")

    def _spawn_reviewer(self, parent: AgentSession) -> AgentSession:
        """Spawn a read-only Claude Code reviewer session for a completed run."""
        init_db()
        goal = f"review: {parent.branch}"
        run = RunState(goal=goal, project=self.project, branch=parent.branch)
        save_run(run)
        trace_run_started(run.run_id, run.project, run.branch, goal)

        idx = len(self.sessions) + 1
        rev = AgentSession(
            idx=idx, goal=goal, run=run,
            project=self.project, branch=parent.branch,
            cwd=parent.cwd,
            session_type="reviewer",
        )
        rev.pr_url = parent.pr_url
        rev.thread = threading.Thread(
            target=self._session_worker, args=(rev,), daemon=True,
        )
        self.sessions.append(rev)
        rev.thread.start()
        return rev

    def _auto_remediate_verify(self, output: str, tries_left: int, session: AgentSession) -> bool:
        if tries_left <= 0:
            return False
        self._session_push(
            session,
            f"→ Auto-fix: verify failed ({tries_left} attempt{'s' if tries_left != 1 else ''} left)\n",
        )
        fix_task = (
            "Verification failed. Fix the root cause — do not add new features:\n\n"
            f"{output[-2000:]}"
        )
        self._run_turn(fix_task, session)
        from flow.commands.verify import run_checks
        passed, new_output = run_checks(cwd=session.cwd)
        if passed:
            return True
        return self._auto_remediate_verify(new_output, tries_left - 1, session)

    def _auto_remediate_check(self, report: dict, tries_left: int, session: AgentSession) -> bool:
        if tries_left <= 0:
            return False
        self._session_push(
            session,
            f"→ Auto-fix: code review blockers ({tries_left} attempt{'s' if tries_left != 1 else ''} left)\n",
        )
        blockers = [f for f in report.get("findings", []) if f.get("severity") == "blocker"]
        items = "\n".join(
            f"- {f['title']} ({f.get('file', 'unknown')}:{f.get('line', 0)}): "
            f"{f.get('detail', '')} → {f.get('action', '')}"
            for f in blockers
        )
        self._run_turn(f"Code review found blockers. Fix all — do not add features:\n\n{items}", session)
        try:
            diff_result = subprocess.run(
                ["git", "diff", "HEAD"], capture_output=True, text=True, cwd=str(session.cwd),
            )
            from flow.commands.check import run_check
            new_report = run_check(diff_text=diff_result.stdout or None, run_id=session.run.run_id)
        except Exception:
            return False
        if new_report.get("blocker_count", 0) == 0:
            return True
        return self._auto_remediate_check(new_report, tries_left - 1, session)

    # ── Claude subprocess ─────────────────────────────────────────────────────

    def _launch_claude(
        self, task: str, session: AgentSession,
        *,
        override_message: str = None,
        allowed_tools: list = None,
        ap_active: bool = True,
    ) -> str:
        model = session.model_override or self.model_override or model_for(session.run.phase, session.run.goal)

        if override_message is not None:
            initial_message = override_message
        else:
            briefing = get_session_briefing(session.run, cwd=session.cwd)
            directive = phase_directive(session.run)
            initial_message = (
                f"{briefing}\n"
                f"**Instructions for this session:**\n{directive}\n\n"
                f"---\n\n"
                f"{task}"
            )

        env = os.environ.copy()
        env["AP_ACTIVE"] = "1" if ap_active else "0"
        env["AP_FLOW_HEADLESS"] = "1"
        env["AP_NO_SPAWN"] = "1" if self.no_agents else env.get("AP_NO_SPAWN", "0")
        env["AP_RUN_ID"] = session.run.run_id
        if os.getenv("AP_FORCE_API_KEY") != "1":
            env.pop("ANTHROPIC_API_KEY", None)

        c = constraints()
        max_turns = int(c.get("max_turns_per_run", c.get("max_steps_per_run", 50)))
        perm = os.getenv("AP_CLAUDE_PERMISSION_MODE", "bypassPermissions")
        timeout_s = int(os.getenv("AP_CLAUDE_TIMEOUT_S", "600"))
        stream_enabled = os.getenv("AP_CLAUDE_STREAM", "1") != "0"
        output_format = "stream-json" if stream_enabled else "json"

        cmd = [
            "claude", "-p", initial_message,
            "--output-format", output_format,
            "--model", model,
            "--permission-mode", perm,
            "--max-turns", str(max_turns),
        ]
        if allowed_tools:
            cmd.extend(["--allowedTools", ",".join(allowed_tools)])
        if stream_enabled:
            cmd.extend(["--verbose", "--include-partial-messages"])
        sid = (session.run.claude_session_id or "").strip()
        if sid and override_message is None:
            cmd.extend(["--resume", sid])

        self._session_push(
            session,
            f"\n→ {model} | {session.run.phase.value}"
            + (f" | resume {sid[:8]}" if sid else "")
            + "\n",
        )

        stdout_lines: list = []
        stderr_lines: list = []
        streamed_parts: list = []
        final_data: Optional[Dict[str, Any]] = None
        printed_header = False

        try:
            proc = subprocess.Popen(
                cmd, env=env,
                cwd=str(session.cwd),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, bufsize=1,
            )
        except FileNotFoundError:
            self._session_push(session, "Error: 'claude' CLI not found.\n")
            return ""

        q: "queue.Queue" = queue.Queue()

        def _pump(stream_name: str, pipe) -> None:
            try:
                for line in iter(pipe.readline, ""):
                    q.put((stream_name, line))
            finally:
                q.put((stream_name, None))

        assert proc.stdout is not None and proc.stderr is not None
        t_out = threading.Thread(target=_pump, args=("stdout", proc.stdout), daemon=True)
        t_err = threading.Thread(target=_pump, args=("stderr", proc.stderr), daemon=True)
        t_out.start()
        t_err.start()

        done_streams: set = set()
        start_ts = time.monotonic()
        user_stopped = False

        while True:
            if len(done_streams) == 2 and proc.poll() is not None and q.empty():
                break
            if (time.monotonic() - start_ts) > timeout_s:
                proc.kill()
                self._session_push(
                    session,
                    f"\n✗ Timed out after {timeout_s}s — set AP_CLAUDE_TIMEOUT_S to increase\n",
                )
                with session.lock:
                    session.status = "failed"
                    session.last_line = f"timeout after {timeout_s}s"
                break
            sentinel = DB_PATH.parent / f"stop_{session.run.run_id}"
            if sentinel.exists():
                sentinel.unlink(missing_ok=True)
                user_stopped = True
                proc.kill()
                self._session_push(session, "Stopped via /stop\n")
                break

            try:
                stream_name, line = q.get(timeout=0.2)
            except queue.Empty:
                continue

            if line is None:
                done_streams.add(stream_name)
                continue

            if stream_name == "stderr":
                stderr_lines.append(line)
                msg = line.strip()
                if msg:
                    self._session_push(session, msg + "\n")
                continue

            stdout_lines.append(line)
            stripped = line.strip()
            if not stripped or not stream_enabled:
                continue
            try:
                evt = json.loads(stripped)
            except json.JSONDecodeError:
                continue

            if evt.get("type") == "result":
                final_data = evt
                continue
            if evt.get("type") != "stream_event":
                continue
            event = evt.get("event", {})
            if event.get("type") != "content_block_delta":
                continue
            delta = event.get("delta", {})
            if delta.get("type") != "text_delta":
                continue
            text = str(delta.get("text", ""))
            if not text:
                continue
            if not printed_header:
                self._session_push(session, "Claude: ")
                printed_header = True
            self._session_push(session, text)
            streamed_parts.append(text)

        if user_stopped:
            session.run.status = RunStatus.blocked
            save_run(session.run)
            return ""

        try:
            return_code = proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            return_code = proc.wait(timeout=5)
        if printed_header:
            self._session_push(session, "\n")

        stdout_raw = "".join(stdout_lines)
        stderr_raw = "".join(stderr_lines)
        data = (
            final_data
            or _parse_claude_json_stdout(stdout_raw)
            or _parse_claude_json_stdout(stderr_raw)
        )

        if return_code != 0 and not data:
            self._session_push(session, f"claude exited {return_code}\n")
            tail = (stderr_raw or stdout_raw).strip()
            if tail:
                self._session_push(session, tail[-2000:] + "\n")
            return ""

        if not data:
            self._session_push(session, "No result from claude.\n")
            return ""

        if isinstance(data, dict):
            tin, tout, model_used, cr = usage_from_claude_result(data)
            sid = str(data.get("session_id") or "").strip() or str(uuid.uuid4())[:8]
            try:
                account_claude_code_session_end(
                    project=session.project, branch=session.branch, session_id=sid,
                    model=model_used, tokens_in=tin, tokens_out=tout,
                    cache_read_input_tokens=cr, run=session.run,
                )
            except Exception as e:
                self._session_push(session, f"Could not record usage: {e}\n")

            new_sid = str(data.get("session_id") or "").strip()
            if new_sid:
                session.run.claude_session_id = new_sid
                save_run(session.run)

        if data.get("is_error") or data.get("subtype") == "error":
            err = data.get("result") or data.get("error") or str(data)
            if str(data.get("api_error_status")) == "429" or "limit" in str(err).lower():
                self._session_push(session, f"Quota reached: {err}\n")
            else:
                self._session_push(session, f"Claude error: {err}\n")
            return ""

        result_text = (data.get("result") or "").strip()
        streamed_text = "".join(streamed_parts).strip()
        if result_text and not streamed_text:
            self._session_push(session, result_text + "\n")
        # Prefer streamed_text: it accumulates all text deltas including content
        # emitted before tool calls (e.g. numbered plan before ExitPlanMode).
        # result_text is only the final message fragment after the last tool call.
        return streamed_text or result_text

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

    def _start_test_session(self) -> AgentSession:
        """Start a fixed micro-task that exercises plan→execute→verify without shipping."""
        task = (
            "Create exactly two files and nothing else:\n\n"
            "1. `src/flow/ping.py` containing:\n"
            "```python\n"
            "def flow_ping() -> str:\n"
            "    return 'pong'\n"
            "```\n\n"
            "2. `tests/test_ping.py` containing:\n"
            "```python\n"
            "from flow.ping import flow_ping\n\n"
            "def test_ping():\n"
            "    assert flow_ping() == 'pong'\n"
            "```\n\n"
            "Do not modify any other files. Do not add imports or docstrings. "
            "These two files are the complete deliverable."
        )
        cwd, branch = self._create_worktree("test-flow-ping")
        init_db()
        run = RunState(goal="[test] add flow_ping smoke test", project=self.project, branch=branch)
        save_run(run)
        trace_run_started(run.run_id, run.project, run.branch, run.goal)

        idx = len(self.sessions) + 1
        session = AgentSession(
            idx=idx,
            goal="[test] flow_ping smoke test",
            run=run,
            project=self.project,
            branch=branch,
            cwd=cwd,
            session_type="executor",
            auto_ship=True,
        )
        session._test_task = task  # store the real task text
        session.thread = threading.Thread(
            target=self._test_session_worker, args=(session,), daemon=True,
        )
        self.sessions.append(session)
        session.thread.start()
        console.print(
            f"[dim]→ Test session {idx} started — plan+execute+verify+ship[/dim]"
        )
        return session

    def _test_session_worker(self, session: AgentSession) -> None:
        try:
            task = getattr(session, "_test_task", session.goal)
            self._run_turn(task, session)
            self._drain_inject(session)
            # Force pipeline if Claude wrote files but didn't emit STEP_DONE markers
            r = subprocess.run(
                ["git", "status", "--porcelain"],
                capture_output=True, text=True, cwd=str(session.cwd),
            )
            if r.stdout.strip() and session.status == "running":
                self._run_pipeline(session)
            with session.lock:
                if session.status == "running":
                    session.status = "done"
        except SystemExit:
            with session.lock:
                if session.status == "running":
                    session.status = "done"
        except Exception as e:
            with session.lock:
                session.status = "failed"
                session.last_line = str(e)[:100]

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


def start_repl() -> None:
    from flow.tui import start_tui
    start_tui()
