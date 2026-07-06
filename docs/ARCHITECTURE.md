# flow — Architecture & Design Deep Dive

A reference for understanding the system end-to-end: decisions, tradeoffs, and where the bodies are buried.

---

## What it does (one paragraph)

`flow` is a personal AI dev harness. You type a task; it spins up an isolated git worktree, runs a `plan → execute → verify → ship` pipeline using Claude Code as the execution engine, and opens a PR. Multiple tasks run in parallel. A TUI shows all of them. The human's job is reviewing PRs — nothing else.

---

## Architecture Map

```
flow (CLI)
  └── repl.py: FlowOrchestrator          ← main orchestrator
        ├── AgentSession (per task)       ← thread-per-session
        │     ├── _session_worker()       ← worker loop
        │     ├── _launch_claude()        ← spawns `claude` subprocess
        │     ├── _run_turn()             ← single claude turn
        │     └── _run_pipeline()         ← verify → check → ship sequence
        └── tui.py: FlowApp              ← Textual TUI (main thread)

tracker.py                               ← SQLite state machine
router.py                                ← model selection
billing.py + session_accounting.py       ← cost tracking
context.py                               ← briefing builder, compaction prompts
observe.py                               ← Langfuse tracing

hooks/ (invoked by Claude Code harness)
  pretool.py    ← before every tool call: enforce budget, gate spawns, log
  stop.py       ← after every session: record usage, check clean state
  posttool.py   ← after tool completes: pair with tool_attempted for inflight detection
  precompact.py ← before context compaction: inject structured summary
  postmerge.py  ← after git merge: auto-complete run if PR merged
```

---

## Database: SQLite at `~/.flow/costs.sqlite`

> **Not DuckDB.** The store is SQLite. DuckDB was considered for analytics but the implementation uses SQLite with WAL mode for concurrent access.

### `runs` — one row per task
| Column | Type | Notes |
|---|---|---|
| `run_id` | VARCHAR PK | 8-char UUID |
| `project`, `branch`, `goal` | VARCHAR/TEXT | |
| `phase` | VARCHAR | `plan \| execute \| verify \| ship` |
| `status` | VARCHAR | `active \| blocked \| complete \| failed \| cancelled` |
| `current_step`, `max_steps` | INTEGER | weighted step counter |
| `plan_steps` | JSON | `[{"id":"1","description":"...","status":"pending\|done"}]` |
| `context_summary` | TEXT | Haiku-compressed state for resume |
| `cost_usd` | DOUBLE | real $ spent (API calls only) |
| `claude_session_id` | VARCHAR | last Claude Code session ID (for `--resume`) |
| `step_budget_used` | DOUBLE | weighted steps consumed |
| `pr_url` | VARCHAR | GitHub PR URL after ship |
| `phase_started_at` | VARCHAR | ISO timestamp of current phase start |
| `artifacts`, `decisions` | JSON | files modified, key decisions made |
| `subscription_msgs`, `subscription_tokens_in/out` | INTEGER | Claude Code quota usage |

### `events` — append-only event log
Every tool call, phase transition, session end, and block is recorded here. This is the source of truth for step budget calculation, inflight tool detection on resume, and cost aggregation.

Key `event_type` values: `tool_attempted`, `tool_completed`, `tool_blocked`, `phase_transition`, `session_end`, `verify_result`, `check_result`, `plan_set`, `run_complete`

### `sessions` — one row per Claude Code session
Billing record: tokens, cost, model, duration, `billing_source` (`subscription | api`).

### `subscription_windows` — 5-hour sliding quota buckets
Tracks Claude Code subscription usage per 5-hour window per plan.

---

## The Hook System — Why and How

### Why hooks over system prompts

A system prompt is advisory. Claude can reason around it, reinterpret it, or simply ignore it if another instruction conflicts. A hook intercepts at the **harness layer** — before the tool call executes. The agent cannot bypass it because it never sees the bypass; the execution is stopped before it starts.

**Result:** deterministic enforcement vs. probabilistic compliance. When the step budget says stop, the agent stops. Every time.

### `hooks/pretool.py` — the enforcement engine

Invoked before every tool call via `PreToolUse` hook. Decision tree:

1. **`ExitPlanMode`** → parse numbered steps → save `plan_steps` → advance phase `plan → execute`
2. **`Agent`** (subagent spawn) → gate by policy:
   - `smart` (default): read-only agents always allowed; write-capable agents blocked if `cost_usd > FLOW_BUDGET_USD` (default `$1.00`)
   - `phase_only`: only allowed in configured phases
3. **`Bash`** → whitelist check against `allowed_bash_commands` in `constraints.yaml`
4. **`Write/Edit/MultiEdit`** → phase gate (blocked in plan phase, except `.claude/plans/`)
5. **All tools** → weighted step budget check (atomic `BEGIN IMMEDIATE` transaction):
   - `SUM(weight)` from `tool_attempted` events for this run
   - Tool weights: `Agent: 5.0`, `Write: 2.0`, `Edit: 1.5`, `Read: 0.25`, `Bash: 0.5`
   - If budget exhausted → exit code 2 (blocked)
   - Else → append `tool_attempted` event, write activity file

Exit codes: `0` = allow, `2` = block.

### `hooks/stop.py` — usage accounting

Invoked when a Claude Code session ends. Records tokens, model, duration. If in `verify` or `ship` phase, runs the verification command and git status check — if it fails, sets `run.status = blocked`.

### `hooks/posttool.py` — inflight detection

Pairs with `pretool`: writes a `tool_completed` event referencing the `tool_attempted` event ID. On resume, `get_inflight_tools()` finds `tool_attempted` events with no matching `tool_completed` — these are tools that were interrupted mid-flight and surfaces them as a warning in the briefing.

### `hooks/precompact.py` — structured compaction

When Claude Code compacts a context, this hook injects a structured summary prompt that preserves: goal, phase, plan steps, decisions, artifacts, spend. Drops conversational noise. This is how context survives long sessions without losing task state.

---

## Concurrency Model

### Threading architecture

- **Main thread**: Textual TUI event loop + user input
- **Worker threads**: One `daemon` thread per `AgentSession`, runs `_session_worker()`
- **Pump threads**: Two per claude subprocess (stdout/stderr readers)

### Synchronization

**`AgentSession.lock` (threading.Lock):** Guards per-session mutable state (status, last_line, pr_url, waiting_for_input). Held briefly.

**`output_queue` (queue.Queue):** Session worker pushes output chunks; TUI drains at 4Hz via `get_nowait()`. Thread-safe by construction.

**`inject_queue` (queue.Queue):** TUI pushes `/prompt` messages; planner worker blocks on `get(timeout=0.5)`.

**Stop sentinel file (`~/.flow/stop_{run_id}`):** TUI creates it for `/stop`; `_launch_claude` polls it during subprocess loop.

**Database writes:** SQLite with WAL mode. `pretool` uses `BEGIN IMMEDIATE` for the atomic budget check+append. Session updates are single-row writes, serialized by SQLite naturally. Hooks are separate processes, not threads — no shared memory conflicts.

### Git isolation

Each session gets its own git worktree on its own branch. Filesystem conflicts between parallel sessions are **impossible by construction** — not mitigated, not handled, structurally eliminated.

---

## The Pipeline

```
NEW RUN
  └── Phase.plan
        Claude outputs numbered steps (1. ..., 2. ...)
        pretool hook catches ExitPlanMode → saves plan_steps
        ↓
  Phase.execute
        Claude works steps, outputs "STEP_DONE: N"
        _run_turn() detects → complete_plan_step()
        All steps done →
        ↓
  _run_pipeline():
    [verify]
        run_checks() (pytest, linter, etc.)
        If fail + auto_remediate:
          _auto_remediate_verify() → spawn fix worker → retry (max 2)
          If still failing: status=failed, surface to user
        ↓
    [check / code review]
        flow check → AI review of diff
        Parse: blocker_count, overall
        If blockers + auto_remediate:
          _auto_remediate_check() → spawn fix worker
        ↓
    [ship]
        flow ship subprocess → commit (AI message) → PR
        Extract pr_url from output
        Spawn reviewer session (read-only, Haiku)
        status = complete
```

### Who drives phase transitions

| Transition | Driver |
|---|---|
| `plan → execute` | `pretool.py` catches `ExitPlanMode` |
| `execute → verify` | `_run_turn_inner` detects all steps done |
| `verify → execute` (remediate) | `_auto_remediate_verify` spawns fix worker |
| `check → execute` (remediate) | `_auto_remediate_check` spawns fix worker |
| `any → ship` | `_run_pipeline` after checks pass |
| `ship → complete` | `_run_pipeline` after PR created |

---

## Model Routing

### `routing.yaml` structure

```yaml
phases:
  plan: claude-opus-4-7        # Thinking, architecture
  execute: claude-sonnet-4-6   # Main implementation
  fast: claude-haiku-4-5-20251001
  ci: claude-haiku-4-5-20251001

utility:
  fast: claude-haiku-4-5-20251001   # Pass 1: commit msgs, initial review
  smart: claude-sonnet-4-6          # Pass 2: PR bodies, deeper review

task_overrides:                # Keyword matching on goal.lower()
  architecture: claude-opus-4-7
  design: claude-opus-4-7
  quick: claude-haiku-4-5-20251001
```

### `model_for(phase, goal) → str`

1. Check `task_overrides`: if any keyword appears in `goal.lower()` → return that model
2. Else return `phases[phase.value]`, default `claude-sonnet-4-6`

### Design tradeoff

Keyword matching is deterministic and cheap. The alternative — routing via an LLM call — introduces latency and a failure mode where the router itself makes the wrong call, making failures harder to diagnose. The current design is intentionally simple: earn the complexity before adding it.

The `plan:` / `quick:` / `review:` prefixes are user-facing overrides. They work but they're a bandaid — they require the user to know which model they want. A smarter classifier is the right long-term answer, but only after the deterministic version is proven insufficient.

---

## Cost & Billing

### Two billing surfaces

**Subscription (default):** Claude Code runs against your claude.ai Pro/Max login. No real $ cost per call, but quota-limited. Tracked in 5-hour rolling windows.

**API (`FLOW_FORCE_API_KEY=1`):** Utility calls (ship, check, ci-review, clarify) hit `ANTHROPIC_API_KEY` directly. Real $ billed.

### `billing.py::calc_cost()`

```python
COSTS = {
    "claude-haiku-4-5-20251001": {"in": 0.8,  "out": 4.0},    # per 1M tokens
    "claude-sonnet-4-6":         {"in": 3.0,  "out": 15.0},
    "claude-opus-4-7":           {"in": 15.0, "out": 75.0},
}
# cache_read_tokens billed at 10% of input rate
cost = (tokens_in * in_rate + tokens_out * out_rate + cache_read * in_rate * 0.1) / 1_000_000
```

### Spend gate

`api_spend_gate_usd` (default `$1.00` in `constraints.yaml`) blocks write-capable subagent spawns. Checked atomically in `pretool` via `get_api_spend_today()`.

---

## Resume Mechanism

### What survives an interruption

- `plan_steps` with `status=done/pending` — persisted in `runs.plan_steps`
- `current_step`, `max_steps` — persisted
- `context_summary` — Haiku-compressed state written by `precompact` hook or `refresh_context_summary()`
- `claude_session_id` — stored by `stop.py`, used to `--resume` the Claude Code session
- `phase_started_at` — for elapsed time display on resume

### Inflight tool detection

`get_inflight_tools()` finds `tool_attempted` events in the `events` table that have no matching `tool_completed` event since the last `session_end`. These are tools that were mid-flight when the session died. The resume briefing surfaces them as a warning so Claude can check filesystem state before continuing.

### The consistency gap

Context state and filesystem state are separate. If a `Write` tool was interrupted after the file was written but before `tool_completed` was logged, the resumed session's context doesn't know the write happened — it must re-derive it from the filesystem. Claude is generally good at this (`git status`, re-reading files) but it's not guaranteed. The design relies on Claude's ability to reconcile rather than on atomic commit semantics.

---

## Worktree Lifecycle

### Creation

```python
slug = re.sub(r"[^a-z0-9]+", "-", goal.lower())[:25].strip("-")
name = f"flow-{slug}-{uuid.uuid4().hex[:4]}"   # e.g. flow-add-jwt-auth-a1b2
git worktree add .claude/worktrees/{name} -b {name}
```

### Cleanup

`/dismiss N` in the TUI calls `git worktree remove --force`. The main repo directory is never removed. Reviewer sessions reuse the executor's worktree (same `cwd`).

### Feature agent pattern

When tasks share a dependency (e.g. "foundation" task that others build on), downstream tasks can be started with `base_branch=<foundation_branch>`. They create their worktree branching from the foundation, not from `main`.

---

## TUI — Data Flow

```
_tick() @ 4Hz (Textual timer):
  1. Refresh spend cache every 5s → update FlowHeader
  2. Detect new sessions → add_session_pane()
  3. For each session:
     a. Drain output_queue.get_nowait() → append to RichLog
     b. refresh_title(): icon + phase + elapsed + PR link + branch + goal
     c. refresh_activity(): read activity_{run_id}.json
        - < 15s:  ⚡ {tool} ({age}s ago)
        - 15–90s: 💭 thinking… ({age}s)
        - > 90s:  ⏸ idle ({elapsed})
  4. Notify if planner waiting for input
```

**Activity file** (`~/.flow/activity_{run_id}.json`): written atomically by `pretool` on each tool call (`.tmp` rename), deleted by `stop.py`. Contains `{tool, ts, phase, event_id}`. Gives the TUI sub-second tool activity without polling the DB.

**Drill-down** (`/view N`): Full-screen RichLog of `output_history` + input bar. `/prompt <msg>` pushes to `inject_queue`; `/stop` creates stop sentinel file; `/back` pops screen.

---

## Known Gaps & Honest Tradeoffs

### Concurrency and the DB
SQLite WAL handles concurrent readers well and serializes writers. In practice, hooks are separate processes firing sequentially (one tool call at a time per session), so write contention is low. If you scaled to many more parallel sessions, this could become a bottleneck.

### At-most-once tool semantics
Tool side effects (file writes, shell commands) have at-most-once delivery guarantees. An interrupted tool call may have partially executed. Recovery relies on Claude re-deriving state from the filesystem — robust in practice but not transactional by design.

### Routing is deterministic, not intelligent
Keyword matching works for explicit prefixes but doesn't classify ambiguous tasks. A task like "make the dashboard faster" won't route to Opus even if it's fundamentally an architecture problem. The long-term fix is a classifier, but that adds latency and a new failure mode.

### Tight coupling to Claude Code's hook API
The entire enforcement layer depends on Anthropic's `PreToolUse`, `Stop`, `PostToolUse`, and `PreCompact` hook contracts. Any change to how Claude Code invokes hooks, what it passes in the payload, or what exit codes it respects could silently break the harness. This is the biggest external dependency risk.

### No integration tests for the full pipeline
The smoke test (`/test-flow`) runs the full pipeline including ship. Unit tests exist for individual components. But the pipeline has many moving parts (worktree creation, hook invocation, phase transitions, PR creation) that are hard to test in isolation, so most pipeline bugs surface in production runs.
