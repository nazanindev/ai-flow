"""
Claude Code PreToolUse hook — invoked as: python3 -m flow.hooks.pretool
Enforces hard constraints: step limit, bash whitelist, Agent spawn gate, budget gate.
Exit 0 = allow. Exit 2 = block (Claude sees the reason).
"""
import json
import os
import sys
import uuid
from pathlib import Path

from dotenv import load_dotenv
load_dotenv(Path.home() / ".autopilot" / ".env")

from flow.config import get_project_id, get_branch, constraints, get_plan, get_plan_window_caps
from flow.tracker import (
    Phase, init_db, load_active_run, save_run, save_subagent_event,
    get_api_spend_today, get_window_usage, activity_path, save_event, DB_PATH,
    try_append_tool_event,
)
from flow.observe import trace_subagent

# Populated at start of main() so block() can write structured events
_ctx: dict = {}


def _write_activity(run_id: str, tool_name: str, phase: str, event_id: str = "") -> None:
    """Atomically write last-tool-call metadata for TUI polling.

    event_id is the tool_attempted event UUID — posttool reads it to pair tool_completed.
    """
    if not run_id or run_id == "none":
        return
    try:
        import time as _time
        path = activity_path(run_id)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps({"tool": tool_name, "ts": _time.time(), "phase": phase, "event_id": event_id}),
            encoding="utf-8",
        )
        tmp.replace(path)
    except Exception:
        pass


def _parse_plan_steps(plan_text: str) -> list:
    import re
    steps = []
    for line in (plan_text or "").splitlines():
        m = re.match(
            r"^\s*(?:\*\*)?\s*(?:step\s*)?(\d+)(?:\s*\*\*)?\s*(?:[.)]|:|—|-)\s+(.+)$",
            line,
            flags=re.IGNORECASE,
        )
        if m:
            steps.append({"id": m.group(1), "description": m.group(2).strip(), "status": "pending"})
    return steps


def block(reason: str) -> None:
    if _ctx.get("run_id") and _ctx["run_id"] != "none":
        try:
            save_event(
                run_id=_ctx["run_id"],
                event_type="tool_blocked",
                project=_ctx.get("project", ""),
                phase=_ctx.get("phase", ""),
                tool_name=_ctx.get("tool_name", ""),
                blocked=True,
                block_reason=reason,
            )
        except Exception:
            pass
    print(json.dumps({"decision": "block", "reason": reason}))
    sys.exit(2)


def allow() -> None:
    sys.exit(0)


# Tools that can modify the filesystem
_WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}


def _agent_spawn_decision(
    tool_input: dict,
    c: dict,
    project: str,
    phase: str,
    session_id: str,
    run_id: str,
) -> None:
    """Apply agent spawn policy and block or fall through."""
    policy = c.get("agent_spawn_policy", "smart")
    api_spend_gate = float(os.getenv("AP_BUDGET_USD") or c.get("api_spend_gate_usd", 1.0))
    api_spend_today = get_api_spend_today(project)

    # Classify agent by its declared tools
    agent_tools = set(tool_input.get("tools") or [])
    is_write_capable = bool(agent_tools & _WRITE_TOOLS)

    if policy == "smart":
        if not is_write_capable:
            # Read-only agents: always allowed regardless of phase or spend
            save_subagent_event(session_id, run_id, project, phase, str(tool_input), True)
            trace_subagent(session_id, run_id, project, phase, True)
            _maybe_warn_quota(c)
            return  # fall through to allow()

        # Write-capable agent: tiered spend gate
        low_tier = api_spend_gate * 0.5
        if api_spend_today >= api_spend_gate:
            reason = (
                f"Write-capable agent blocked: API spend ${api_spend_today:.2f} >= "
                f"gate ${api_spend_gate:.2f}."
            )
            save_subagent_event(session_id, run_id, project, phase, str(tool_input), False, reason)
            trace_subagent(session_id, run_id, project, phase, False, reason)
            block(reason)

        if api_spend_today >= low_tier:
            # Medium tier: check phase allowlist
            allowed_phases = c.get("agent_spawns_allowed_in", ["plan", "execute"])
            if phase not in allowed_phases:
                reason = (
                    f"Write-capable agent blocked: phase '{phase}' not in {allowed_phases} "
                    f"at spend ${api_spend_today:.2f} (medium tier)."
                )
                save_subagent_event(session_id, run_id, project, phase, str(tool_input), False, reason)
                trace_subagent(session_id, run_id, project, phase, False, reason)
                block(reason)

        # Low spend or allowed phase: permit
        save_subagent_event(session_id, run_id, project, phase, str(tool_input), True)
        trace_subagent(session_id, run_id, project, phase, True)
        _maybe_warn_quota(c)

    else:
        # Legacy phase_only policy
        allowed_phases = c.get("agent_spawns_allowed_in", ["plan"])
        if phase not in allowed_phases:
            reason = (
                f"Agent spawn blocked: phase '{phase}' not in allowed phases {allowed_phases}."
            )
            save_subagent_event(session_id, run_id, project, phase, "", False, reason)
            trace_subagent(session_id, run_id, project, phase, False, reason)
            block(reason)

        if api_spend_today >= api_spend_gate:
            reason = (
                f"API spend gate: ${api_spend_today:.2f} >= ${api_spend_gate:.2f}. "
                "Agent spawn blocked."
            )
            save_subagent_event(session_id, run_id, project, phase, "", False, reason)
            trace_subagent(session_id, run_id, project, phase, False, reason)
            block(reason)

        save_subagent_event(session_id, run_id, project, phase, str(tool_input), True)
        trace_subagent(session_id, run_id, project, phase, True)
        _maybe_warn_quota(c)


def main() -> None:
    if os.getenv("AP_ACTIVE") != "1":
        sys.exit(0)

    init_db()

    payload = {}
    try:
        raw = sys.stdin.read()
        if raw.strip():
            payload = json.loads(raw)
    except Exception:
        pass

    tool_name = payload.get("tool_name", "")
    tool_input = payload.get("tool_input", {})
    project = get_project_id()
    branch = get_branch()
    c = constraints()

    run = load_active_run(project, branch)
    run_id = run.run_id if run else "none"
    phase = run.phase.value if run else "unknown"
    session_id = payload.get("session_id", str(uuid.uuid4())[:8])

    _ctx.update({"run_id": run_id, "project": project, "phase": phase, "tool_name": tool_name})

    # ── ExitPlanMode — persist plan steps ────────────────────────────────────
    if tool_name == "ExitPlanMode":
        plan_text = tool_input.get("plan_text", "")
        if run and plan_text:
            steps = _parse_plan_steps(plan_text)
            if steps:
                from flow.run_manager import set_plan_steps, advance_phase
                set_plan_steps(run, steps)
                # Auto-advance to execute (gates removed)
                advance_phase(run, Phase.execute)
        _write_activity(run_id, "ExitPlanMode", phase)
        allow()

    # ── Agent spawn gate ─────────────────────────────────────────────────────
    if tool_name == "Agent":
        no_spawn = os.getenv("AP_NO_SPAWN", "0") == "1"
        if no_spawn:
            reason = "AP_NO_SPAWN=1: agent spawning disabled for this session"
            save_subagent_event(session_id, run_id, project, phase, "", False, reason)
            trace_subagent(session_id, run_id, project, phase, False, reason)
            block(reason)

        _agent_spawn_decision(tool_input, c, project, phase, session_id, run_id)

    # ── Bash command whitelist ────────────────────────────────────────────────
    if tool_name == "Bash":
        cmd = str(tool_input.get("command", "")).strip()
        allowed_cmds = c.get("allowed_bash_commands", [])
        base_cmd = cmd.split()[0] if cmd else ""
        base_cmd = base_cmd.split("/")[-1]
        if allowed_cmds and base_cmd and base_cmd not in allowed_cmds:
            block(f"Bash command '{base_cmd}' not in allowed_bash_commands whitelist.")

    # ── Edit phase gate ──────────────────────────────────────────────────────
    if tool_name in {"Write", "Edit", "MultiEdit", "NotebookEdit"}:
        allowed_phases = c.get("edits_allowed_in", ["execute", "verify", "ship"])
        if phase not in allowed_phases:
            target_path = tool_input.get("file_path") or tool_input.get("notebook_path")
            plan_dir = Path.home() / ".claude" / "plans"
            if target_path:
                try:
                    target_resolved = Path(target_path).resolve()
                    if target_resolved.is_relative_to(plan_dir.resolve()):
                        pass  # plan files always writable
                    else:
                        block(
                            f"File edits blocked: phase '{phase}' not in {allowed_phases}. "
                            "Plan phase is read-only for project files."
                        )
                except (ValueError, OSError):
                    block(
                        f"File edits blocked: phase '{phase}' not in {allowed_phases}."
                    )
            else:
                block(f"File edits blocked: phase '{phase}' not in {allowed_phases}.")

    # ── Soft WIP=1 guidance ───────────────────────────────────────────────────
    if tool_name in {"Write", "Edit", "MultiEdit"}:
        try:
            from flow.features import get_active_feature
            if not get_active_feature():
                print(
                    "[flow warn] No active feature in features.yaml. "
                    "Run `flow features pick` before writing to keep WIP=1.",
                    file=sys.stderr,
                )
        except Exception:
            pass

    # ── Step counter (weighted) — atomic check+append via BEGIN IMMEDIATE ────
    event_id = ""
    if run and tool_name not in ("", "Agent"):
        tool_weights = c.get("tool_weights", {})
        weight = float(tool_weights.get(tool_name, tool_weights.get("default", 1.0)))

        phase_budgets = c.get("phase_step_budgets", {})
        phase_budget = phase_budgets.get(run.phase.value)
        effective_max = float(
            phase_budget if phase_budget is not None
            else (run.max_steps or c.get("max_steps_per_run", 20))
        )

        allowed, event_id = try_append_tool_event(
            run.run_id, run.project, tool_name, run.phase.value, weight, effective_max,
        )
        if not allowed:
            block(
                f"Step budget exhausted ({run.step_budget_used:.1f}/{effective_max:.0f} weighted steps). "
                "Stop and summarize progress."
            )

    if run_id and run_id != "none" and tool_name:
        _write_activity(run_id, tool_name, phase, event_id)

    allow()


def _maybe_warn_quota(c: dict) -> None:
    warn_pct = float(c.get("subscription_quota_warn_pct", 0.80))
    plan = get_plan()
    caps = get_plan_window_caps()
    msg_cap = caps.get(plan, {}).get("msgs", 0)
    if not msg_cap:
        return
    window = get_window_usage(plan)
    used_pct = window["msgs_used"] / msg_cap
    if used_pct >= warn_pct:
        print(
            f"[flow warn] Subscription quota: {window['msgs_used']}/{msg_cap} msgs used "
            f"({used_pct*100:.0f}%) in current 5-hour window.",
            file=sys.stderr,
        )


if __name__ == "__main__":
    main()
