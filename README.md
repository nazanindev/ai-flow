# `flow`

Personal AI dev harness. Each task gets an isolated worktree and runs `plan → execute → verify → ship` automatically.

Includes sub-agent spawn guardrails, cost controls, context management, and observability metrics + dashboard.

[6 Python CLI games in 10 minutes](https://github.com/nazanindev/ai_1.0) — each one a parallel agent, each one its own task and pipeline.

[FastAPI blog API](https://github.com/nazanindev/ai_1.1) — single agent, full CRUD across users / posts / tags with SQLAlchemy and pytest.

[GitHub metrics service](https://github.com/nazanindev/ai_1.2) — coordinator spawned 4 parallel agents (repos, pulls, contributors, webhooks). First real test of the foundation-first spawn pattern.


---

## What it does

You type a task. `flow` spins up an isolated git worktree, runs it through a full `plan → execute → verify → ship` pipeline, and opens a PR — without you touching it again. Multiple tasks run in parallel. The TUI shows all of them.

```
[1] plan    ── Architecture question        (Opus)
[2] execute ── Rate limiting impl           (Sonnet)  step 8/40
[3] ship    ── PR opened: .../pull/42
```

---

## How it works

### Deterministic enforcement via hooks

Limits live in `constraints.yaml` and are enforced by a **pre-tool hook** that runs before every agent action — not in a system prompt. The difference matters: a system prompt is a suggestion the model can reason around. A hook is a wall it physically hits.

Four hook types fire on every session:

| Hook | When | What it does |
|------|------|-------------|
| `PreToolUse` | Before every tool call | Checks step budget, bash allowlist, agent spawn gate, spend gate |
| `PostToolUse` | After every tool call | Writes a `tool_completed` event for resume reconciliation |
| `PreCompact` | Before context compaction | Injects a prompt that preserves RunState artifacts across compression |
| `Stop` | Session end | Records token usage, runs clean-state checks |
| `post-merge` (git) | After `git merge` | Checks if the active run's PR was merged; auto-closes the run |

### Weighted step budgets

Every tool call has a cost: `Agent: 5.0`, `MultiEdit: 2.5`, `Write: 2.0`, `Edit: 1.5`, `Bash: 1.0`, `Read: 0.25`, `Glob/Grep: 0.1`. Each phase has a budget — plan: 15, execute: 40, verify: 15, ship: 8. When it's spent, the hook blocks and forces a summary. The check and increment happen atomically inside a `BEGIN IMMEDIATE` transaction — two parallel agents can't both slip past the same budget threshold.

`max_turns` and `max_steps` are separate: turns control context consumption, the step budget controls tool cost.

### Event-sourced state

Every tool invocation is appended to an immutable event log before execution. The session state (`runs` table) is a derived snapshot — never the write target. This means:

- **No race conditions** on quota accounting: subscription usage is derived from `session_end` events, not a mutable counter
- **Exact resume**: on restart after a kill, the briefing injected into the new session includes which tools were in-flight and what uncommitted filesystem changes landed — Claude reconciles before re-doing work
- **Full audit trail**: `flow events <run_id>` shows the complete history of every tool attempted, blocked, or completed

### Auto-pipeline

With defaults from `constraints.yaml`, no manual intervention is needed after submitting a task:

```yaml
auto_verify_on_steps_complete: true   # run verify when all plan steps done
auto_check_before_ship: true          # run code review before ship
auto_remediate: true                  # spawn fix worker on verify/check failure
auto_remediate_max_tries: 2           # cap before surfacing failure
```

The pipeline runs `prompt → plan → execute → verify → fix (if needed) → ship`. The PR is the review gate — human approval is baked in, not bolted on.

### Model routing

Opus plans, Sonnet executes, Haiku reviews and writes commit messages. Routing is in `routing.yaml` with per-keyword overrides — prefix a task with `architecture:` or `quick:` to change the model without touching config. Utility calls (ship, check, ci-review) support Gemini models as a drop-in swap via `GOOGLE_API_KEY`.

### Smart agent spawn policy

The spawn gate (`agent_spawn_policy: smart`) classifies sub-agent requests by capability and spend tier:

- Read-only agents: always allowed
- Write-capable, low spend: allowed in any phase
- Write-capable, medium spend: restricted to `plan` and `execute` phases
- Write-capable, high spend (≥ API gate): blocked

### Auto-remediation

If verify or check fails, a fix worker spawns, retries up to twice, then surfaces the failure if it can't resolve it.

### Parallel isolation

Each session lives in its own git worktree on its own branch. Filesystem conflicts are structurally impossible.

### Features system

`features.yaml` (versioned with each repo) tracks the feature work the harness is driving. The active feature's behavior and verification command are injected into every session briefing as a sprint contract — scope guardrails, not just documentation.

```sh
flow features add F01 "Users can reset their password via email" --verify "pytest tests/test_reset.py"
flow features pick F01      # mark active; injected into all run briefings
flow features verify        # run verification command → transitions active → passing
flow features list          # tabular state: not_started | active | blocked | passing
```

### Style system

`flow init` creates `~/.autopilot/style.yaml` — controls AI-generated artifact format (commit messages, PR titles, PR bodies, review tone, agent verbosity). Per-repo overrides live in `.ap-style.yaml` and deep-merge on top of the global file.

### Observability

Optional [Langfuse](https://cloud.langfuse.com) integration (set `LANGFUSE_*` keys) records run traces, phase transitions, session-end token rollups, and subagent gate events. `flow stats` reads SQLite directly — zero Langfuse dependency for cost tracking.

**[Engineering notes](docs/ENGINEERING.md)** — design, tradeoffs, and internals 

---

## Install

```sh
pip install -e .
flow init
```

`flow init` writes hooks into `~/.claude/settings.json` and creates `~/.autopilot/.env`:

```sh
ANTHROPIC_API_KEY=sk-ant-...   # for ship, check, ci-review
AP_PLAN=pro                    # pro | max5 | max20 | api_only
```

To also scaffold repo-local harness artifacts (`features.yaml`, `.ap-style.yaml`):

```sh
flow init --repo
```

State is persisted in `~/.autopilot/costs.sqlite` (SQLite WAL mode — safe for concurrent writes from parallel agents).

---

## Usage

```sh
flow
```

Type a task, press Enter. Prefix to route it:

| Prefix | Model | Behavior |
|---|---|---|
| _(none)_ | Sonnet | Full pipeline: plan → execute → verify → ship, reviewer auto-spawned |
| `plan: <question>` | Opus | Interactive planner — stays alive, responds to follow-ups |
| `review: <branch>` | Haiku | One-shot diff review |
| `coord: <goal>` | Opus | Coordinator — decomposes goal into parallel sub-agents using foundation-first pattern |

### TUI commands

| | |
|---|---|
| `/view N` | Drill into session N — full output + live input |
| `/stop [N]` | Stop session N or all running |
| `/prompt N <msg>` | Inject a message into session N |
| `/model opus\|sonnet\|haiku` | Override model for new sessions |
| `/resume [run_id]` | Reattach to an interrupted run — briefing includes in-flight tools and uncommitted changes |
| `/quit` | Exit, clean up completed worktrees |

Planner sessions show `?` in the pane title when waiting for input.

---

## CLI reference

```sh
# Harness management
flow init [--force] [--repo]   # wire hooks; --repo scaffolds features.yaml / .ap-style.yaml
flow doctor [--fix]            # check hook health; --fix rewrites hooks for current interpreter
flow serve [--port 7331]       # start local dashboard API + UI

# Run lifecycle
flow                           # interactive TUI
flow resume [run_id]           # reattach to interrupted run (picker if no ID given)
flow status                    # current run state and today's cost
flow events [run_id]           # full event timeline: tool attempts, blocks, phase transitions

# Code quality
flow verify                    # run tests/lint for current project
flow check [--json]            # AI review of local uncommitted diff
flow ship [--branch-name X]    # verify → commit → PR (with AI commit message + PR body)
flow ci-review [--pr 42]       # two-pass Haiku→Sonnet review for GitHub Actions

# Features (sprint tracking)
flow features                  # list all features
flow features add <id> <behavior> --verify <cmd>
flow features pick [id]        # activate; injected into session briefings
flow features verify [--id]    # run verification → marks passing
flow features active           # show current active feature

# Utilities
flow stats [--project name]    # cost breakdown by project and recent runs
flow route <task>              # show which model tier would be used for a task
```

---

## Billing surfaces

`flow` tracks two cost surfaces separately:

| Surface | Auth | Billing | Tracked by |
|---|---|---|---|
| Claude Code sessions | `claude login` (Pro/Max) | Flat subscription — $0 per session | 5-hour quota window msgs + tokens |
| flow utility calls | `ANTHROPIC_API_KEY` | Per-token API billing | Real USD per call (ship, check, ci-review) |

The `api_spend_gate_usd` in `constraints.yaml` gates utility calls. It does not cap in-session spend — set a workspace spend cap in the [Anthropic console](https://console.anthropic.com) if you switch to API mode (`AP_FORCE_API_KEY=1`).

---

## Design

See [`docs/tradeoffs.md`](docs/tradeoffs.md) for the architectural decisions behind event sourcing, SQLite WAL, hook enforcement, and the coordinator spawn patterns.

See [`docs/ENGINEERING.md`](docs/ENGINEERING.md) for hook health, the two billing surfaces, Langfuse observability gaps, the map-reduce scaling path, and known limitations of hook-based enforcement.

---

## Prerequisites

- [Claude Code](https://claude.ai/code) installed and authenticated
- Python 3.9+
- [`gh`](https://cli.github.com) (for `flow ship` and CI review)
- A GitHub repo with `origin` set
- Anthropic API key
