# Design Tradeoffs

Decisions made while building `flow` and what they cost.

---

## Mutable counters → event sourcing

**Problem.** The original design stored `step_budget_used` as a column on the `runs` table. Multiple parallel agents would read it, add their tool weight, and write it back. Under concurrency this is a TOCTOU race: two agents can both read the same value, both pass the budget check, and both proceed — the budget overruns silently.

**Decision.** Budget enforcement moved to an append-only `events` table. `try_append_tool_event()` opens a `BEGIN IMMEDIATE` transaction, reads `SUM(events.weight)` for the run, and either blocks or inserts atomically. The `runs` table is now a denormalized snapshot — fast for dashboard reads, but never the write target for anything safety-critical.

**Cost.** One extra DB write per tool call. The `events` table grows fast under parallel agents (each run generates ~30–80 rows). Query patterns that used to be a single column read now aggregate over the events table.

**Payoff.** Exact resume: `get_inflight_tools()` queries unmatched `tool_attempted` / `tool_completed` pairs since the last session boundary. No more "what was I doing when the process was killed?"

---

## DuckDB → SQLite WAL

**Problem.** DuckDB enforces a single read-write connection. Parallel agents — each a separate `claude -p` subprocess — contend on every write. The retry loop we had was papering over the symptom.

**Decision.** Migrated to SQLite with WAL mode (`PRAGMA journal_mode=WAL`). WAL allows concurrent readers while a writer holds the lock; writers queue at the OS level with no application retry loop needed.

**Cost.** SQLite is less capable for analytical queries (no columnar storage, limited window functions). `json_extract()` instead of DuckDB's native JSON operators.

**Payoff.** No connection contention. `BEGIN IMMEDIATE` works as a real serialization primitive, which is what made atomic budget enforcement possible.

---

## Hook enforcement vs system prompt

**Problem.** Putting budget limits and bash allowlists in a system prompt means the model can reason around them. Under context pressure or with a particularly motivated task, it will find ways to proceed anyway.

**Decision.** Constraints live in `constraints.yaml` and are enforced by a PreToolUse hook that runs as a subprocess before every tool call. The hook exits non-zero to block; Claude Code treats this as a hard refusal.

**Cost.** The model can't see why it's blocked in a nuanced way — it just gets a string. Poorly worded block messages cause the agent to retry the same blocked action repeatedly (visible in event timelines as clusters of `tool_blocked` rows).

**Payoff.** The constraint is actually enforced. A system prompt is a suggestion. A hook is a wall.

---

## Agent spawn patterns

Three patterns emerged from testing, each with different tradeoffs:

### Flat parallel
All agents start from the same branch simultaneously. Fast — no sequential phase. Guaranteed merge conflicts if any agent touches shared infrastructure (main.py, database.py, requirements.txt). Works only when tasks are truly orthogonal with no shared files.

### Coordinator
An Opus session decomposes a goal into a JSON spawn plan and launches sub-agents. The decomposition happens once at spawn time — after that, agents run blind with no communication channel. `owns[]` in the plan limits each agent to a declared file set, but this is enforced by prompting, not by the filesystem.

### Foundation-first
The coordinator spawns one foundation agent first, blocks until it completes, then spawns feature agents with `base_branch=foundation.branch`. Feature worktrees start from the foundation's committed state. Feature PRs target the foundation branch; when foundation merges to main, GitHub auto-retargets them.

**Cost.** Sequential phase 1. A slow or stuck foundation blocks all feature work. If foundation fails, the coordinator aborts rather than gracefully falling back.

**Payoff.** Feature PRs contain only their own module files. No conflicts at merge time.

---

## Agent communication

**Current state.** Agents don't communicate. The coordinator passes a goal string at spawn time. After that, each agent runs in its own worktree with no way to signal siblings or the coordinator. The events table is observability-only — no agent reads it at runtime.

**What this means in practice.** Feature agents have to assume the shape of shared infrastructure rather than reading it from the foundation. Assumptions encoded in the goal text can drift from what the foundation actually built.

**Planned approaches (not yet implemented):**

- *Contract file*: coordinator writes `AGENTS_CONTRACT.md` into each worktree before spawning — shared interface (DB session signature, router registration pattern, base model fields). Agents read it in plan phase.
- *Events as message bus*: add an `agent_message` event type; agents poll `get_latest_events()` watching for messages from sibling run IDs. Requires agents to know their siblings' run IDs (already available via `coordinator_spawn` events).
- *Explicit wait-for*: coordinator waits for a `phase_transition → execute` event from foundation before spawning features, rather than waiting for `status == done`. Finer-grained sequencing.

---

## Budget model

**Weighted tool costs** reflect actual compute rather than treating every action equally:

| Tool | Weight | Rationale |
|------|--------|-----------|
| Agent | 5.0 | Spawns a new session — most expensive |
| Write | 2.0 | Creates/overwrites a file |
| MultiEdit | 2.5 | Multiple edits in one call |
| Edit | 1.5 | Modifies an existing file |
| Bash | 1.0 | Executes a command |
| Read | 0.25 | Reads without modifying |
| Glob/Grep | 0.1 | Passive search |

**Per-phase budgets** match expected work (plan: 15, execute: 40, verify: 15, ship: 8). A plan phase that exhausts its budget probably got distracted exploring rather than planning.

**max_turns vs step budget** are now separate. `max_steps_per_run` (default 30) controls the weighted budget ceiling for phases that don't have their own budget. `max_turns_per_run` (default 60) is the `--max-turns` flag passed to claude — it controls how much context the session can consume, independent of cost. Conflating these caused sessions to hit the turn wall while still having budget headroom.

**Cost.** Budget exhaustion mid-task produces jarring "Step budget exhausted — stop and summarize" messages that can confuse the agent and cause it to retry the same blocked call multiple times before summarizing.
