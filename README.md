# `flow`

A control plane for autonomous coding agents.

flow owns the loop around Claude Code: it admits or blocks every action, meters what sessions spend, records what they do in an event log, schedules parallel agents so they can't collide, and recovers runs that die mid-task. The agent supplies judgment; flow supplies what production systems wrap around judgment — **admission control, metering, durable state, scheduling, recovery, and audit**.

The design philosophy is to constrain the agent rather than amplify it. An unconstrained agent loop is open-ended by default; flow makes it bounded and auditable by construction. It runs on one machine with no infrastructure to stand up — "personal deployment" describes where it runs, not what it is.

Built by running it on real work:

- [6 Python CLI games in 10 minutes](https://github.com/nazanindev/ai_1.0) — each game its own task and parallel agent
- [FastAPI blog API](https://github.com/nazanindev/ai_1.1) — single agent, full CRUD with SQLAlchemy and pytest
- [GitHub metrics service](https://github.com/nazanindev/ai_1.2) — dispatcher spawned 4 parallel agents; first real test of foundation-first spawning
- [Project Management API](https://github.com/nazanindev/ai_1.3) — WIP

<img width="3448" height="2090" alt="image" src="https://github.com/user-attachments/assets/b25af610-d4e1-4af9-8a8e-a9f045a7e993" />

---

## Admission control

Every tool call the agent attempts passes through a **pre-tool hook** before it executes. Limits live in `constraints.yaml`; the hook enforces them and blocks with a structured reason the model sees. The difference between this and prompt-level guardrails matters: a system prompt is a suggestion the model can reason around. A hook is a wall it physically hits.

| Hook | When | What it does |
|------|------|-------------|
| `PreToolUse` | Before every tool call | Step budget, bash allowlist, phase write-gate, agent spawn policy, spend gate |
| `PostToolUse` | After every tool call | Writes a `tool_completed` event, paired to its `tool_attempted` event for recovery |
| `PreCompact` | Before context compaction | Preserves RunState artifacts across compression |
| `Stop` | Session end | Records usage, runs clean-state checks |
| `post-merge` (git) | After `git merge` | Auto-closes the run when its PR merges |

Policy is tiered, not binary. Read-only agents always pass. Write-capable agents pass at low spend, are restricted to allowed phases at medium spend, and are blocked outright past the spend gate. File edits are phase-gated — the plan phase is read-only for project files. Bash runs against an allowlist.

> This is also why flow isn't exposed as an MCP server. A tool is something the client's model can decline to call, or call and ignore the answer from — enforcement requires *owning the loop*, and a server that merely offers tools doesn't. The giveable artifact is the harness, not a service: `flow init` writes the hooks into your own Claude Code, so the wall runs on your machine instead of being a call you can skip.

## Metering

Every tool call has a weight — `Agent: 5.0`, `MultiEdit: 2.5`, `Write: 2.0`, `Edit: 1.5`, `Bash: 1.0`, `Read: 0.25`, `Glob`/`Grep`: 0.1 — and every phase has a budget: plan 15, execute 40, verify 15, ship 8. When a budget is spent, the hook blocks and forces a summary. The check and the charge happen atomically inside a `BEGIN IMMEDIATE` transaction, so two parallel agents can't both slip past the same threshold.

Two cost surfaces are tracked separately, because they behave differently: Claude Code subscription sessions ($0 marginal, quota-limited in 5-hour windows) and API-metered utility calls (ship, check, ci-review — real money, gated by `FLOW_BUDGET_USD`). `max_turns` and the step budget are independent: turns bound context consumption, steps bound tool cost.

## Durable state

Every attempted tool call is appended to an event log **before it executes**. The log is the authority: budgets are computed by summing `tool_attempted` weights, quota windows by aggregating `session_end` events — never by trusting a mutable counter. The `runs` row is a snapshot maintained alongside the log for fast reads; anything that must be correct under concurrency is derived from the log itself.

This buys three things:

- **Race-free accounting** — concurrent sessions can't corrupt a number that is computed, not stored
- **Exact recovery** — see below
- **A full audit trail** — `flow events <run_id>` replays the complete history of every tool attempted, blocked, or completed

## Recovery: brief, don't replay

Durable-execution engines recover by replaying event history, which requires the decider to be deterministic — in Temporal, nondeterminism is an error class. An agent's decider is non-deterministic by definition, so flow takes the other branch: recovery is a *reasoning task*, delegated to the component that is best at reasoning. The division of labor is deliberate. flow spends its engineering on what the model can't do — enforce its own limits — and trusts it with what it demonstrably can: resuming from an honest description of the world.

When a run resumes after a kill, flow computes the in-flight set — `tool_attempted` events since the last session boundary with no paired `tool_completed` — takes a `git diff` of uncommitted filesystem changes, and injects both into the new session's briefing: *here is what was in flight when you died, here is what actually landed on disk; reconcile before re-doing work.* The model does the reconciliation — not as a fallback, but because reconciliation is judgment, and judgment is what the model is good at.

The briefing itself is the same idea applied to context: each session starts from a structured projection of durable RunState — goal, phase, plan-step status, decisions, artifacts, a compressed summary — never from accumulated chat history. The context window is a view, rebuilt from state; it is not the state.

The tradeoff is stated plainly: replay gives guarantees, briefing gives judgment. For a system whose whole job is supervising judgment, that's the right side of the trade — but it is a trade. It also leans on a property of the domain: the model can only reconcile what it can see, and in coding the world state is cheaply observable — `git diff` is a complete oracle of what actually landed.

### Relation to durable execution

flow converged on this architecture through trial and error — each piece was built in response to a failure, and the vocabulary came later:

| flow | durable execution |
|------|-------------------|
| Event log, appended before execution | Event history / write-ahead log |
| `runs` snapshot maintained beside the log | Projection over the event stream |
| Budgets and quota computed from events | Event-sourced accounting |
| `tool_completed` paired to `tool_attempted` | Activity completion records |
| Resume briefing with in-flight set + disk diff | Recovery — by briefing instead of replay |
| Fix worker, two retries, then surface | Retry policy with max attempts |

## Scheduling

`dispatch:` decomposes a goal into parallel agents — with the constraint that every agent ships a PR, and merge conflicts destroy the output. When tasks share infrastructure, the dispatcher runs **foundation-first**: one agent creates all shared files, then feature agents spawn in parallel from its branch, each with declared file ownership — no two tasks may own the same file. File paths become the lock discipline that lets parallel agents merge cleanly.

Spawning is bounded by `dispatcher_max_spawn` and sits behind the same spend gate as everything else.

## The pipeline

A plain task runs `plan → execute → verify → fix (if needed) → ship` automatically. Plans are captured as numbered steps and tracked to completion via `STEP_DONE` markers; when all steps land, verification runs, then an AI review of the diff, then ship. If verify or review fails, a fix worker retries up to twice before surfacing the failure. The PR is the review gate — human approval is baked in, not bolted on.

Post-ship review is configurable (`auto_review`): a local reviewer session, a two-pass CI review posted to the GitHub PR, both, or off.

## Model routing

Routing lives in `routing.yaml`: Opus plans, Sonnet executes, Haiku handles cheap utility passes (commit messages, first-pass review), Sonnet the smarter ones (PR bodies, second-pass review). Keyword overrides route by task — prefix with `architecture:` or `quick:` to change the tier without touching config. Utility calls take Gemini models as a drop-in swap via `GOOGLE_API_KEY`.

## Observability

`flow serve` starts a local dashboard on `:7331` — live run table, event timeline, cost by project. `flow events` prints a run's full timeline with per-event timing; `flow stats` covers cost from the CLI. Optional [Langfuse](https://cloud.langfuse.com) integration records run traces, phase transitions, and spawn-gate decisions as structured spans.

## Features and style

`features.yaml` (versioned with the repo) tracks active feature work — the active feature's behavior and verification command are injected into every briefing as a sprint contract, and the pre-tool hook warns when writes start without one (WIP=1). `~/.flow/style.yaml` controls AI-generated artifact format (commit messages, PR titles and bodies); per-repo `.flow-style.yaml` deep-merges on top.

**[Engineering notes](docs/ENGINEERING.md)** — design, tradeoffs, and internals

---

## Install

```sh
pip install -e .
flow init
```

`flow init` writes hooks into `~/.claude/settings.json` and creates `~/.flow/.env`:

```sh
ANTHROPIC_API_KEY=sk-ant-...   # for ship, check, ci-review
FLOW_PLAN=pro                  # pro | max5 | max20 | api_only
```

`flow init --repo` also scaffolds `features.yaml` and `.flow-style.yaml` in the current repo. State persists in `~/.flow/costs.sqlite` (WAL mode — safe for concurrent writers).

## Usage

```sh
flow
```

Type a task, press Enter. Prefix to route it:

| Prefix | Model | Behavior |
|---|---|---|
| _(none)_ | Sonnet | Full pipeline: plan → execute → verify → ship → review |
| `plan: <question>` | Opus | Interactive planner — stays alive, responds to follow-ups |
| `review: <branch>` | Sonnet | One-shot diff review |
| `dispatch: <goal>` | Opus | Decompose into parallel agents, foundation-first |

### TUI commands

| | |
|---|---|
| `/view N` | Drill into session N — full output + live input |
| `/stop [N]` | Stop session N, or all running |
| `/dismiss N` | Dismiss a finished session from the table |
| `/prompt N <msg>` | Inject a message into session N |
| `/model opus\|sonnet\|haiku` | Override model for new sessions |
| `/no-agents` | Disable subagent spawning for new sessions |
| `/budget $X` | Set the API spend gate for this session |
| `/resume [run_id]` | Reattach to an interrupted run — briefing includes in-flight tools and disk diff |
| `/sessions` `/status` | List sessions; show run state and today's cost |
| `/quit` | Exit, clean up completed worktrees |

### CLI

```sh
flow init [--force] [--repo]   # wire hooks; --repo scaffolds features.yaml / .flow-style.yaml
flow doctor [--fix]            # check hook health
flow serve [--port N]          # local dashboard on :7331
flow status                    # current run state and today's cost
flow events [run_id]           # full event timeline for a run
flow resume [run_id]           # inspect an interrupted run; picker if omitted
flow verify                    # run tests/lint
flow check [--json]            # AI review of local diff
flow ship [--branch-name X]    # verify → commit → PR
flow ci-review [--pr 42]       # two-pass review for GitHub Actions
flow route "<task>"            # show which model tier a task routes to
flow features [list|add|pick|active|verify]   # sprint contract state
flow stats [--project name]    # cost breakdown
```

## Prerequisites

- [Claude Code](https://claude.ai/code) installed and authenticated
- Python 3.9+
- [`gh`](https://cli.github.com) (for `flow ship` and CI review)
- A GitHub repo with `origin` set
- Anthropic API key
