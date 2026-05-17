"""Context injection — builds session briefing from RunState (not chat history)."""
import subprocess
from pathlib import Path
from typing import Optional
from flow.tracker import RunState, Phase


def _repo_tree(cwd: Optional[Path] = None, max_files: int = 80) -> str:
    """Compact file listing from git ls-files, grouped by top-level directory."""
    try:
        result = subprocess.run(
            ["git", "ls-files"],
            capture_output=True, text=True, timeout=5,
            cwd=str(cwd) if cwd else None,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return ""
        lines = result.stdout.strip().splitlines()
        if len(lines) > max_files:
            # Summarize by directory when too many files
            from collections import Counter
            dirs = Counter(
                (Path(l).parts[0] if "/" in l else ".") for l in lines
            )
            summary = "  " + "\n  ".join(
                f"{d}/  ({n} files)" for d, n in sorted(dirs.items())
            )
            return f"({len(lines)} files — top-level dirs)\n{summary}"
        return "  " + "\n  ".join(lines)
    except Exception:
        return ""


def build_briefing(run: RunState, style: dict = None, cwd: Optional[Path] = None) -> str:
    """Compact structured briefing injected at the start of each Claude session."""
    artifacts_str = "\n".join(f"  - {a}" for a in run.artifacts) or "  (none yet)"
    decisions_str = "\n".join(f"  - {d}" for d in run.decisions) or "  (none yet)"

    plan_str = ""
    if run.plan_steps:
        lines = []
        for s in run.plan_steps:
            marker = "x" if s.get("status") == "done" else " "
            lines.append(f"  - [{marker}] {s['description']}")
        plan_str = "\n**Plan steps:**\n" + "\n".join(lines) + "\n"

    feature_str = ""
    sprint_contract_str = ""
    if run.feature_id:
        try:
            from flow.features import get_feature

            feat = get_feature(run.feature_id)
            if feat:
                feature_str = (
                    f"\n**Active feature:** {feat.id}\n"
                    f"- Behavior: {feat.behavior}\n"
                    f"- Verification: `{feat.verification}`\n"
                    f"- State: {feat.state}\n"
                )
                sprint_contract_str = (
                    "\n**Sprint contract (this run):**\n"
                    f"- Scope: {feat.behavior}\n"
                    f"- Verification command: `{feat.verification}`\n"
                    "- Out of scope: anything not required to make the verification command exit 0\n"
                )
            else:
                feature_str = f"\n**Active feature:** {run.feature_id}\n"
        except Exception:
            feature_str = f"\n**Active feature:** {run.feature_id}\n"

    agent_style_str = ""
    if style:
        from flow.config import style_prompt
        sp = style_prompt(style, ["agent"])
        if sp:
            agent_style_str = f"\n**Agent style:**\n{sp}\n"

    tree = _repo_tree(cwd)
    repo_str = f"\n**Repo files:**\n{tree}\n" if tree else ""

    return f"""## AUTOPILOT SESSION BRIEFING
> This is a structured run context, not a chat history. Do not reference prior conversation.

**Run ID:** {run.run_id}
**Goal:** {run.goal}
**Phase:** {run.phase.value.upper()} (step {run.current_step}/{run.max_steps})
**Status:** {run.status.value}
**API spend so far:** ${run.cost_usd:.4f} (subscription: {run.subscription_msgs} msgs)
{plan_str}
{feature_str}
{sprint_contract_str}
**Artifacts:**
{artifacts_str}

**Key decisions:**
{decisions_str}

**Context summary:**
{run.context_summary or "(no prior summary — this is the first session for this run)"}
{repo_str}{agent_style_str}
---
"""


def phase_directive(run: RunState) -> str:
    """Return a terse, phase-specific action instruction appended to the initial message."""
    pending = [s["description"] for s in run.plan_steps if s.get("status") != "done"]
    done_count = sum(1 for s in run.plan_steps if s.get("status") == "done")

    if run.phase == Phase.plan:
        return (
            "You are in the PLAN phase. Enter plan mode now.\n\n"
            "Build a numbered execution plan. Each step must be a concrete, atomic action "
            "(e.g. 'Add JWT middleware to routes/auth.py', not 'Handle auth'). Include:\n"
            "- File-level actions (create / edit / delete)\n"
            "- Test or verification steps\n"
            "- Any migration or config changes\n\n"
            "FORMAT REQUIREMENT (strict): output one step per line as `1. ...`, `2. ...`, etc. "
            "or `Step 1: ...`, `Step 2: ...`. Do not use prose summaries like "
            "`my plan has one step` and do not collapse steps into a paragraph.\n\n"
            "If the user goal names a specific file/path, the first plan step must target that "
            "file/path directly (or explicitly ask a clarification question first).\n\n"
            "Do NOT include git commit, git push, or PR creation steps — those are handled "
            "automatically by the ship phase after execution completes.\n\n"
            "When the plan is complete, AI Flow will automatically capture your numbered plan "
            "and move to the execute phase."
        )

    if run.phase == Phase.execute:
        steps_str = ""
        if pending:
            steps_str = "\n\nRemaining steps:\n" + "\n".join(f"  {i+1}. {s}" for i, s in enumerate(pending))
            if done_count:
                steps_str = f"\n\n{done_count} step(s) already done.{steps_str}"
        return (
            f"You are in the EXECUTE phase. Work through the plan steps in order, "
            f"one at a time. After completing each step, briefly confirm what was done "
            f"before moving to the next. When you finish a step, include a standalone line "
            f"`STEP_DONE: <step_id>` (example: `STEP_DONE: 1`). "
            f"Do NOT run `git commit`, `git push`, or create PRs — shipping is handled "
            f"automatically after all steps complete.{steps_str}"
        )

    if run.phase == Phase.verify:
        return (
            "You are in the VERIFY phase. Run the full test suite and linter. "
            "If anything fails, fix it before reporting back. "
            "Do not mark the run complete until all checks pass. "
            "IMPORTANT: `/ship` requires a passing `flow check` run (or explicit `/ack-check` "
            "if you accept blocker risk). Run `/check` before attempting to ship."
        )

    if run.phase == Phase.ship:
        return (
            "You are in the SHIP phase. Run `flow ship` to verify, commit, and open the PR. "
            "Do not make further code changes."
        )

    return "Continue from the current phase. Do not re-litigate decisions already recorded above."


def summarize_for_new_session(run: RunState, anthropic_client) -> str:
    """Ask Haiku to compress the run state into a tight context summary."""
    pending_steps = [s["description"] for s in run.plan_steps if s.get("status") != "done"]
    done_steps = [s["description"] for s in run.plan_steps if s.get("status") == "done"]

    prompt = f"""Compress this run state into a tight context summary (max 300 words).
Preserve: goal, plan steps, key decisions, artifacts created, current status.
Discard: conversational detail, repeated information.

Run ID: {run.run_id}
Goal: {run.goal}
Phase: {run.phase.value} step {run.current_step}/{run.max_steps}
Plan steps done: {done_steps}
Plan steps pending: {pending_steps}
Artifacts: {run.artifacts}
Decisions: {run.decisions}
Existing summary: {run.context_summary}
"""
    try:
        resp = anthropic_client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}],
        )
        return resp.content[0].text.strip()
    except Exception:
        return run.context_summary
