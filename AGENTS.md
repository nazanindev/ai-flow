# AGENTS.md

This repository builds `flow`: a multi-agent CLI orchestrator for AI coding sessions.
The goal is reliable session execution, cost visibility, and clean handoffs — not maximum code output.

## Quick Start

- Install: `pip install -e .`
- Initialize hooks: `flow init`
- Start orchestrator: `flow`

## Verification

- Primary check: `flow verify`
- Python sanity: `python3 -m compileall src/flow`

Never treat code as complete until verification passes.

## Runtime Model

- Each user task spawns an `AgentSession`: git worktree + branch + background thread
- Sessions run in parallel; the live table shows all of them
- State machine per session: `plan -> execute -> verify -> ship`
- Session state persisted in SQLite WAL (`~/.autopilot/costs.sqlite`)
- Context injected from structured run artifacts, not raw chat transcripts
- Hooks fire inside each `claude -p` subprocess (not in the orchestrator process)

## Hard Constraints

- Enforce constraints via hooks, not prompt-only instructions
- Respect `constraints.yaml` for step budgets, spawn gates, and spend gates
- Avoid destructive git operations unless explicitly requested
- Keep work scoped to the current run phase

## Key Files

- `README.md` — product overview and usage
- `constraints.yaml` — hard runtime limits and gating rules
- `routing.yaml` — phase and keyword model routing
- `src/flow/orchestrator.py` — `FlowOrchestrator`: shared state, git helpers, assembles the mixins below
- `src/flow/runner.py` — `_RunnerMixin`: driving `claude -p` and the verify→check→ship auto-pipeline
- `src/flow/workers.py` — `_WorkersMixin`: session spawning and the per-type worker threads
- `src/flow/dispatcher.py` — `_DispatcherMixin`: foundation-first fan-out into parallel agents
- `src/flow/controls.py` — `_ControlsMixin`: live table and interactive commands (`/stop`, `/prompt`, `/resume`)
- `src/flow/session.py` — `AgentSession` dataclass
- `src/flow/hooks/pretool.py` — pre-tool enforcement gate (step budget, bash allowlist, agent spawn, spend gate)
- `src/flow/hooks/stop.py` — stop hook usage tracking and clean-state checks
- `src/flow/tracker.py` — persistent state store (SQLite WAL)
- `src/flow/run_manager.py` — `RunState` lifecycle: create, phase transitions, artifact recording

## Session Exit Expectations

Before ending implementation work:

1. Run verification (`flow verify`).
2. Ensure no stale debug artifacts remain in the working tree.
3. Leave the repo in a restartable state for the next session.
