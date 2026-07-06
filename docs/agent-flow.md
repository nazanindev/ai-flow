# agent-flow

agent-flow is a harness for driving AI agents through structured, multi-phase workflows. It turns a high-level goal into a tracked run that moves through discrete phases — PLAN, EXECUTE, and SHIP — with human gate checks between each phase.

---

## Lifecycle

```
Goal
  │
  ▼
PLAN ──(gate)──▶ EXECUTE ──(gate)──▶ SHIP ──▶ Done
```

| Phase   | What happens |
|---------|-------------|
| PLAN    | The agent explores the codebase and produces a numbered, atomic execution plan. |
| EXECUTE | The agent works through plan steps one at a time, emitting `STEP_DONE: N` after each. |
| SHIP    | The agent opens a PR, writes a summary, and marks the run complete. |

---

## Key Concepts

### Run
A single end-to-end task tracked by a unique **Run ID**. A run carries:
- **Goal** — the original user instruction
- **Phase** — current lifecycle stage
- **Plan steps** — ordered checklist generated in PLAN
- **Artifacts** — files, PRs, or outputs produced during EXECUTE
- **Key decisions** — notable choices recorded for traceability

### Phases
Each phase is a separate agent session. The framework injects a **session briefing** at the top of every session so the agent has full context without relying on chat history.

### Gates
A gate is a human approval checkpoint between phases. Gates prevent the agent from proceeding until a human (or an automated policy) confirms the plan or reviews the output. Gates can be configured to auto-approve for fully autonomous runs.

### Step markers
During EXECUTE, the agent emits `STEP_DONE: <step_id>` after completing each plan step. The framework parses these markers to advance the checklist and detect stalls.

### Artifacts
Anything the agent produces (files created, PRs opened, config changed) is recorded as an artifact so the SHIP phase can summarize what changed.

---

## Quick-Start Example

### 1. Kick off a run

```bash
af run "Add rate limiting to the API"
```

This creates a new run, assigns it an ID, and starts the PLAN session.

### 2. Review the plan

The agent outputs a numbered plan:

```
1. Add rate-limit middleware to src/middleware/rate_limit.py
2. Register middleware in src/app.py
3. Add config values to config/settings.py
4. Write unit tests in tests/test_rate_limit.py
5. Run tests and verify all pass
```

Approve it to advance to EXECUTE:

```bash
af approve <run-id>
```

### 3. Watch execution

The agent works through each step and emits progress markers:

```
Step 1 complete — created src/middleware/rate_limit.py
STEP_DONE: 1
Step 2 complete — registered middleware in src/app.py
STEP_DONE: 2
...
```

### 4. Ship

Once all steps are done, the SHIP phase opens a PR and posts a summary. Review and merge.

---

## Configuration

| Setting | Default | Description |
|---------|---------|-------------|
| `gate.plan`    | `human`  | Who approves the plan (`human` or `auto`) |
| `gate.execute` | `human`  | Who approves before SHIP |
| `max_steps`    | `20`     | Hard limit on plan steps per run |
| `branch_prefix`| `fix/`   | Git branch prefix for SHIP PRs |

---

## Design Principles

- **Atomic steps** — each plan step touches one file or one concern; no compound actions.
- **Resumable** — every session is self-contained via the briefing; the agent can be interrupted and restarted without losing state.
- **Traceable** — decisions and artifacts are recorded so a human can audit what changed and why.
- **Human-in-the-loop by default** — gates require explicit approval; autonomy is opt-in.
