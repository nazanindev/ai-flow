# `flow`

30 minutes. 7 games. Each one planned, implemented, tested, and PR'd by a parallel Claude agent while I watched from the control room.

That's what this is for.

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

## What makes it not a toy

**Hook-enforced constraints, not prompt instructions.**
Limits live in `constraints.yaml` and fire in a pre-tool hook before every agent action. The agent can't talk its way past them.

**Weighted step budgets.**
Every tool call has a cost: `Agent: 5.0`, `Write: 2.0`, `Edit: 1.5`, `Read: 0.25`. Each pipeline phase has its own budget — plan: 15, execute: 40, verify: 15, ship: 8. When the budget runs out, the hook blocks further calls. Not a warning. A block.

**Spend gates.**
API utility calls are gated at `$1.00` by default. Hard stop, not a nudge.

**Model routing.**
Opus plans. Sonnet executes. Haiku reviews and writes commit messages. Routing is in `routing.yaml` with per-keyword overrides — prefix a task with `architecture:` or `quick:` to change the model without touching config.

**Auto-remediation.**
If verify fails, a fix worker spawns automatically, retries up to 2 times, and surfaces the failure to you only if it can't resolve it.

---

## What it's not

Not a Claude API wrapper with a system prompt. Not a harness that asks the model to "please stay within budget." Not another agent framework with 40 config files and a plugin registry.

It's a thin orchestrator running real `claude -p` subprocesses, with real limits enforced at the hook layer.

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
| `/resume [run_id]` | Reattach to an interrupted run |
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
