# `flow`

Personal AI dev harness. Each task gets an isolated worktree and runs `plan → execute → verify → ship` automatically.

[Six casino games in 30 minutes](https://github.com/nazanindev/ai_1.0) — each one a parallel agent, each one its own pipeline.

![flow control room](docs/screenshot.png)

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

Three hook types fire on every session:

| Hook | When | What it does |
|------|------|-------------|
| `PreToolUse` | Before every tool call | Checks step budget, bash allowlist, agent spawn gate, spend gate |
| `PostToolUse` | After every tool call | Writes a `tool_completed` event for resume reconciliation |
| `Stop` | Session end | Records token usage, runs clean-state checks |

### Weighted step budgets

Every tool call has a cost: `Agent: 5.0`, `Write: 2.0`, `Edit: 1.5`, `Read: 0.25`. Each phase has a budget — plan: 15, execute: 40, verify: 15, ship: 8. When it's spent, the hook blocks and forces a summary. The check and increment happen atomically inside a `BEGIN IMMEDIATE` transaction — two parallel agents can't both slip past the same budget threshold.

### Event-sourced state

Every tool invocation is appended to an immutable event log before execution. The session state (`runs` table) is a derived snapshot — never the write target. This means:

- **No race conditions** on quota accounting: subscription usage is derived from `session_end` events, not a mutable counter
- **Exact resume**: on restart after a kill, the briefing injected into the new session includes which tools were in-flight and what uncommitted filesystem changes landed — Claude reconciles before re-doing work
- **Full audit trail**: `flow events <run_id>` shows the complete history of every tool attempted, blocked, or completed

### Model routing

Opus plans, Sonnet executes, Haiku reviews and writes commit messages. Routing is in `routing.yaml` with per-keyword overrides — prefix a task with `architecture:` or `quick:` to change the model without touching config.

### Auto-remediation

If verify fails, a fix worker spawns, retries up to twice, then surfaces the failure if it can't resolve it.

### Parallel isolation

Each session lives in its own git worktree on its own branch. Filesystem conflicts are structurally impossible. The PR is the exit gate — human review is baked into the pipeline, not bolted on.

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

### Commands

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

## CI / scripting

```sh
flow doctor [--fix]          # check hook health
flow stats                   # cost by project
flow ship                    # verify → commit → PR
flow check                   # AI review of local diff
flow ci-review --pr 42       # for GitHub Actions
```

---

## Prerequisites

- [Claude Code](https://claude.ai/code) installed and authenticated
- Python 3.9+
- [`gh`](https://cli.github.com) (for `flow ship` and CI review)
- A GitHub repo with `origin` set
- Anthropic API key
