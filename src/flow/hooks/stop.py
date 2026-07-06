"""
Claude Code Stop hook — invoked as: python3 -m flow.hooks.stop
Reads session data from hook payload, writes to DuckDB + Langfuse.

Usage and quota are persisted when this hook runs, except when FLOW_HEADLESS=1
(headless `claude -p` spawned by the flow REPL — that process records usage on exit
so the prompt matches Anthropic). Interactive / IDE sessions omit that flag, so Stop
remains authoritative there.
Clean-state checks in verify/ship phases run only when FLOW_ACTIVE=1 (sessions launched
from the flow REPL), so IDE or plain claude sessions are not penalized for an
active run left on disk.

Two billing surfaces:
  subscription (default) — Claude Code runs against claude.ai Pro/Max login.
    Records token + message quota into subscription_windows; no real $ computed.
  api (FLOW_FORCE_API_KEY=1) — Claude Code bills via ANTHROPIC_API_KEY.
    Computes and records real USD cost.
"""
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

from flow.config import STATE_DIR
from dotenv import load_dotenv

load_dotenv(STATE_DIR / ".env")

from flow.config import get_project_id, get_branch, constraints
from flow.session_accounting import account_claude_code_session_end
from flow.tracker import init_db, load_active_run, save_run, RunStatus, save_event, activity_path


def _run_clean_state_checks() -> tuple[bool, list[str]]:
    """Run lightweight clean-state checks for end-of-session handoff."""
    failures: list[str] = []

    try:
        from flow.commands.verify import detect_runner

        runner = detect_runner(Path.cwd())
        if runner:
            result = subprocess.run(
                runner,
                shell=True,
                capture_output=True,
                text=True,
                timeout=300,
            )
            if result.returncode != 0:
                failures.append(f"verification command failed: {runner}")
    except Exception as e:
        failures.append(f"verification check error: {e}")

    try:
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if status.returncode == 0 and status.stdout.strip():
            patterns = constraints().get(
                "clean_state_artifact_patterns",
                [".log", ".tmp", ".DS_Store", "__pycache__"],
            )
            dirty_lines = [l for l in status.stdout.splitlines() if l.strip()]
            artifact_hits = []
            for line in dirty_lines:
                path = line[3:].strip() if len(line) > 3 else line.strip()
                if any(path.endswith(p) or p in path for p in patterns):
                    artifact_hits.append(path)
            if artifact_hits:
                failures.append(
                    "stale artifacts detected: " + ", ".join(artifact_hits[:8])
                )
    except Exception as e:
        failures.append(f"git clean-state check error: {e}")

    return (len(failures) == 0, failures)


def main() -> None:
    init_db()

    payload = {}
    try:
        raw = sys.stdin.read()
        if raw.strip():
            payload = json.loads(raw)
    except Exception:
        pass

    project = get_project_id()
    branch = get_branch()
    session_id = payload.get("session_id") or str(uuid.uuid4())[:8]

    usage = payload.get("usage", {})
    if not isinstance(usage, dict):
        usage = {}
    tokens_in = int(usage.get("input_tokens") or 0)
    tokens_out = int(usage.get("output_tokens") or 0)
    model = payload.get("model", "claude-sonnet-4-6")

    run = load_active_run(project)

    if os.getenv("FLOW_ACTIVE") == "1":
        clean_state_phases = set(
            constraints().get("clean_state_check_phases", ["verify", "ship"])
        )
        if run and run.phase.value in clean_state_phases:
            clean_ok, reasons = _run_clean_state_checks()
            if not clean_ok:
                run.status = RunStatus.blocked
                save_run(run)
                print(
                    f"[flow stop] clean-state checks failed for run {run.run_id}: "
                    + " | ".join(reasons),
                    file=sys.stderr,
                )

    # Headless `claude -p` from the flow REPL meters here instead — Stop often
    # does not receive the same payload / timing; double-counting is avoided.
    if os.getenv("FLOW_HEADLESS") == "1":
        return

    cr = int(usage.get("cache_read_input_tokens") or 0)

    if run and run.run_id:
        try:
            save_event(
                run_id=run.run_id,
                event_type="session_end",
                project=project,
                phase=run.phase.value if run else "",
                metadata={
                    "session_id": session_id,
                    "model": model,
                    "tokens_in": tokens_in,
                    "tokens_out": tokens_out,
                    "cache_read_tokens": cr,
                },
            )
        except Exception:
            pass
        # Clean up stale activity file on session end
        try:
            activity_path(run.run_id).unlink(missing_ok=True)
        except Exception:
            pass

    account_claude_code_session_end(
        project=project,
        branch=branch,
        session_id=session_id,
        model=model,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cache_read_input_tokens=cr,
        run=run,
    )


if __name__ == "__main__":
    main()
