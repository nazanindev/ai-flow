"""_RunnerMixin — driving `claude -p`, interpreting a turn, and the auto-pipeline.

This is the behavioral core: launch a Claude subprocess, stream its output,
parse plan steps and STEP_DONE markers, and run verify → check → ship → review
with auto-remediation. Mixed into FlowOrchestrator.
"""
import json
import os
import re
import subprocess
import threading
import time
import uuid
import queue
from pathlib import Path
from typing import Any, Dict, Optional

from rich.console import Console

from flow.config import DB_PATH, constraints
from flow.router import model_for
from flow.tracker import (
    Phase, RunState, RunStatus, init_db, load_run, save_run, get_api_spend_today,
)
from flow.run_manager import (
    advance_phase, complete_plan_step, get_session_briefing,
    set_plan_steps, store_check_result,
)
from flow.session_accounting import account_claude_code_session_end, usage_from_claude_result
from flow.context import phase_directive
from flow.observe import trace_run_started
from flow.session import AgentSession

console = Console()


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


class _RunnerMixin:
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

    def _drain_inject(self, session: AgentSession) -> None:
        """Process any queued /prompt messages after the current turn."""
        while True:
            try:
                msg = session.inject_queue.get_nowait()
                self._session_push(session, f"\n→ [prompt injected] {msg}\n")
                self._run_turn(msg, session)
            except queue.Empty:
                break

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
        ship_env = {**os.environ, "FLOW_ACTIVE": "0"}
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
            env={**os.environ, "FLOW_ACTIVE": "0"},
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
        env["FLOW_ACTIVE"] = "1" if ap_active else "0"
        env["FLOW_HEADLESS"] = "1"
        env["FLOW_NO_SPAWN"] = "1" if self.no_agents else env.get("FLOW_NO_SPAWN", "0")
        env["FLOW_RUN_ID"] = session.run.run_id
        if os.getenv("FLOW_FORCE_API_KEY") != "1":
            env.pop("ANTHROPIC_API_KEY", None)

        c = constraints()
        max_turns = int(c.get("max_turns_per_run", c.get("max_steps_per_run", 50)))
        perm = os.getenv("FLOW_CLAUDE_PERMISSION_MODE", "bypassPermissions")
        timeout_s = int(os.getenv("FLOW_CLAUDE_TIMEOUT_S", "600"))
        stream_enabled = os.getenv("FLOW_CLAUDE_STREAM", "1") != "0"
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
                    f"\n✗ Timed out after {timeout_s}s — set FLOW_CLAUDE_TIMEOUT_S to increase\n",
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
