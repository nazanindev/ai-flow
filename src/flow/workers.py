"""_WorkersMixin — session spawning, the per-type worker threads, and output routing.

Each session runs on a daemon thread whose entrypoint is `_session_worker`, which
dispatches to the executor / planner / reviewer / dispatcher worker. Mixed into
FlowOrchestrator.
"""
import subprocess
import threading
import queue

from rich.console import Console

from flow.tracker import RunState, init_db, save_run
from flow.observe import trace_run_started
from flow.session import AgentSession

console = Console()


class _WorkersMixin:
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

    # ── Test session (smoke test of the full pipeline) ────────────────────────

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
