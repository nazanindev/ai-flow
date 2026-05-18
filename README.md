# `flow`

Personal AI dev harness. Single-agent pipeline for straightforward tasks, dispatcher mode for larger projects. Dispatcher takes a goal, breaks it into a plan, and dispatches to parallel agents. Hook-based enforcement, event-sourced state, foundation-first spawn pattern.

[6 Python CLI games in 10 minutes](https://github.com/nazanindev/ai_1.0) — each one its own task and parallel agent.

[FastAPI blog API](https://github.com/nazanindev/ai_1.1) — single agent, full CRUD across users / posts / tags with SQLAlchemy and pytest.

[GitHub metrics service](https://github.com/nazanindev/ai_1.2) — dispatcher spawned 4 parallel agents (repos, pulls, contributors, webhooks). First real test of the foundation-first spawn pattern.

![flow control room](docs/screenshot.png)

---

## How it works

### Deterministic enforcement via hooks

Limits live in `constraints.yaml` and are enforced by a **pre-tool hook** that runs before every agent action — not in a system prompt. The difference matters: a system prompt is a suggestion the model can reason around. A hook is a wall it physically hits.

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

The pipeline runs `prompt → plan → execute → verify → fix (if needed) → ship` automatically. If verify or check fails, a fix worker spawns and retries up to twice before surfacing the failure. The PR is the review gate — human approval is baked in, not bolted on.

### Model routing

Opus plans, Sonnet executes, Haiku reviews and writes commit messages. Routing is in `routing.yaml` with per-keyword overrides — prefix a task with `architecture:` or `quick:` to change the model without touching config. Utility calls (ship, check, ci-review) support Gemini models as a drop-in swap via `GOOGLE_API_KEY`.

### Features and style

`features.yaml` (versioned with the repo) tracks active feature work — the active feature's behavior and verification command are injected into every session briefing as a sprint contract. `~/.autopilot/style.yaml` controls AI-generated artifact format (commit messages, PR titles, PR bodies); per-repo overrides in `.ap-style.yaml` deep-merge on top.

### Observability

`flow serve` starts a local dashboard on `:7331` — live run table, event timeline, cost breakdown by project. `flow stats` covers the same data from the CLI. Two cost surfaces are tracked separately: Claude Code subscription sessions ($0 marginal) and API-metered utility calls (ship, check, ci-review).

Optional [Langfuse](https://cloud.langfuse.com) integration records run traces, phase transitions, and subagent gate events as structured spans. See [Engineering notes](docs/ENGINEERING.md).

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

`flow init --repo` also scaffolds `features.yaml` and `.ap-style.yaml` in the current repo. State is persisted in `~/.autopilot/costs.sqlite` (SQLite WAL mode — safe for concurrent writes).

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
| `dispatch: <goal>` | Opus | Dispatcher — decomposes goal into parallel agents, foundation-first |

### TUI commands

| | |
|---|---|
| `/view N` | Drill into session N — full output + live input |
| `/stop [N]` | Stop session N or all running |
| `/prompt N <msg>` | Inject a message into session N |
| `/model opus\|sonnet\|haiku` | Override model for new sessions |
| `/resume [run_id]` | Reattach to an interrupted run — briefing includes in-flight tools and uncommitted changes |
| `/quit` | Exit, clean up completed worktrees |

### CLI

```sh
flow init [--force] [--repo]   # wire hooks; --repo scaffolds features.yaml / .ap-style.yaml
flow doctor [--fix]            # check hook health
flow serve                     # local dashboard on :7331
flow status                    # current run state and today's cost
flow events [run_id]           # full event timeline
flow verify                    # run tests/lint
flow check [--json]            # AI review of local diff
flow ship                      # verify → commit → PR
flow ci-review [--pr 42]       # two-pass review for GitHub Actions
flow features pick [id]        # set active feature (injected into session briefings)
flow stats [--project name]    # cost breakdown
```

---

## Prerequisites

- [Claude Code](https://claude.ai/code) installed and authenticated
- Python 3.9+
- [`gh`](https://cli.github.com) (for `flow ship` and CI review)
- A GitHub repo with `origin` set
- Anthropic API key
