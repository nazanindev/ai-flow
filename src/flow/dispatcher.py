"""_DispatcherMixin — decompose a goal into parallel agents, foundation-first.

The `dispatch:` session type: plan the work with Opus, then spawn sub-agents.
When tasks share infrastructure it runs a single "foundation" agent first and
starts the feature agents from its branch, so parallel PRs don't collide. This is
the fan-out algorithm, kept separate from the plain worker threads in flow.workers.
Mixed into FlowOrchestrator.
"""
import json
import os
import re
import time

from flow.config import constraints
from flow.tracker import get_api_spend_today
from flow.session import AgentSession


class _DispatcherMixin:
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
