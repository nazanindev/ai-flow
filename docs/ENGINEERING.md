# Engineering notes

Deep-dive on harness design, tradeoffs, and internals. See [README.md](README.md) for setup and usage.

---

## Harness engineering principles

**Configuration-driven enforcement.** `constraints.yaml` is applied in `PreToolUse` / `Stop` (budgets, bash allowlist, spawn gates, API spend caps). Host-side gates; model output does not bypass them.

**Explicit phase machine, persisted run state.** Phases: `plan → execute → verify → ship`. `RunState` lives in SQLite; each turn injects a structured briefing (goal, phase, plan steps, artifacts, decisions), not a chat transcript.

**Repo-scoped product state.** `features.yaml` holds feature intent and verification commands (`flow features`, `flow init --repo`); versioned with the repo, distinct from session DB.

**Separate implementation and review.** Execute loop vs `flow check` over `git diff HEAD` (rubric, structured output). Blocker findings can require acknowledgement before `/ship`.

**Completion tied to checks.** `flow verify` plus Stop-hook clean-state rules (verify/ship phases) bind "done" to test exit status and working-tree hygiene.

**Two cost surfaces.** Subscription quota (Claude Code) vs API-metered utilities — [billing](#two-billing-surfaces).

**Explicit human approvals.** Plan (`/approve`, `/reject`), PR (`/gate pr`), optional checker acknowledgement before ship when policy requires it.

**Orchestrator vs worker session.** The CLI / REPL owns scheduling (phases, utilities, billing hooks, `RunState` I/O). Each Claude Code run is a bounded worker turn under that supervision — not the sole locus of policy or persistence.

### References

1. OpenAI — [*Harness engineering: leveraging Codex in an agent-first world*](https://openai.com/index/harness-engineering/)
2. Anthropic — [*Building effective agents*](https://www.anthropic.com/research/building-effective-agents/)
3. Anthropic — [*Harness design for long-running application development*](https://www.anthropic.com/engineering/harness-design-long-running-apps/)

---

## Direction (planned)

Work in progress on the orchestration layer:

- **Parallel orchestration** — scheduling, isolation, budgets: [multi-role swarms vs multi-run worktrees](#two-scaling-modes); disposable checkouts with `RunState` / verify / check / ship bound to the right path.
- **Auto-remediation loops** — when `flow check` or `flow verify` finds blockers, spawn a bounded fix worker, re-verify, and only proceed to ship on a clean pass. Currently a manual cycle (`/check` → fix → `/verify` → `/ship`); the phase machine is already structured for this to be automatic.
- **Crash-investigation agent** — on startup failure or uncaught exception, capture the traceback, inject it as the run briefing, and spawn a diagnostic worker turn to investigate. The structured briefing format is already built for this kind of context injection; it just needs a trigger path from the exception handler.

---

## Map-reduce scaling path

The README **Scaling plan** has two figures: **Today** (serial Claude Code + hooks + utilities) and **Target** below. **Target** is the abstraction: **map** assigns bounded units to workers under a stable contract (briefing in, artifacts and markers out); **reduce** is the host merging into `RunState`, gates, and **tools** (`verify`, `check`, `ship`, `gh`) — not the model. **Business logic** (constraints, routing, features, phase gates) is **part of the orchestrator** — config on disk, evaluation and scheduling in the REPL — not a separate runtime from map/reduce.

### Two scaling modes

Parallel **N** means different things in each shape:

**Multi-role, one task (swarm / queue).** Several *roles* on *one* run (research, build, review) with handoffs over a **host-owned** queue or artifact channel. Reduce keeps **one** `RunState`, ordering, and liveness. Needs a parseable handoff contract and ideally **one mutating writer** on the tree (or explicit merge rules).

**Many runs, many trees (throughput).** Several *tasks* at once, each with its own checkout (worktrees / clones). **N** = **N runs**, each with its own reduce and `RunState` → checkout linkage for tools. Isolation is **per-run filesystem** — orthogonal to the swarm mailbox.

### Workers

Model-agnostic in principle. **Today:** Claude Code + hooks ([limitations](#known-limitations)); other backends need their own enforcement or host-side API checkpoints.

### What must harden as N grows

- **Scheduling** — parallel vs sequence, budgets, attribution across workers or runs ([Two scaling modes](#two-scaling-modes)).
- **Isolation** — throughput mode: checkout per run so mappers do not share one working tree.
- **Reduce semantics** — ordering, conflicts, single owner for `git` before ship (swarm mode especially).
- **Enforcement parity** — hooks are Claude-specific; see [Known limitations](#known-limitations).
- **Observability** — per-worker / per-unit signals; extends [Observability gaps](#observability).

### Current baseline

**N = 1**, serial turns: one worker role, one run, SQLite `RunState`, host utilities — matches the README **Today** figure. The **Target** figure is **direction**, not full parallel product yet.

---

## Two billing surfaces

AI Flow tracks two distinct cost surfaces separately — mixing them up produces meaningless numbers:

| Surface | Auth | Billing | What flow tracks |
|---|---|---|---|
| **Claude Code sessions** | `claude login` (claude.ai Pro/Max) | Flat subscription — $0 per session | 5-hour quota window msgs + tokens |
| **flow utility calls** | `ANTHROPIC_API_KEY` | Per-token API billing | Real USD per call (ship, ci-review, check) |

Claude Code interactive sessions (the big coding loop) run against your Pro/Max subscription — $0 marginal, but they burn your 5-hour message window. The `flow` CLI makes direct SDK calls for utilities (ship, review, check) — those are metered and cost real money (typically cents, Haiku-heavy).

**Trust boundary:** If you flip to API mode (`FLOW_FORCE_API_KEY=1`), set a workspace spend cap in the [Anthropic console](https://console.anthropic.com) — flow's gates don't protect against runaway in-session spend. The guards here only gate the flow utility calls.

---

## Hard constraints

Configured in `constraints.yaml` and enforced via the `PreToolUse` hook — not prompted, not hoped for:

```yaml
max_steps_per_run: 30        # fallback step ceiling
max_tokens_per_step: 8000    # per-step token ceiling

# Weighted step budgets — each tool call deducts its weight from the budget
tool_weights:
  Write: 2.0
  Edit: 1.5
  Bash: 1.0
  Agent: 5.0
  Read: 0.25
  Glob: 0.1
  Grep: 0.1

# Per-phase budgets (weighted units); overrides max_steps_per_run
phase_step_budgets:
  plan: 20.0
  execute: 60.0
  verify: 20.0
  ship: 10.0

# When plan steps are parsed, max_steps = len(steps) * multiplier
plan_steps_multiplier: 3.0

# Approval gates
plan_approval_gate: true     # require /approve before execute
pr_approval_gate: true       # require confirmation before /ship

# flow utility API spend gate (ship, ci-review, check hit ANTHROPIC_API_KEY)
api_spend_gate_usd: 1.00     # blocks Agent spawns if today's spend >= this

# Subscription quota warning (Claude Code sessions — warns, never hard-blocks)
subscription_quota_warn_pct: 0.80
plan_window_caps:
  pro:      { msgs: 45 }
  max5:     { msgs: 225 }
  max20:    { msgs: 900 }

allowed_bash_commands:       # unlisted commands are blocked
  - git, pytest, python, uv, pip, npm, npx, gh, cat, ls, find, grep, curl, jq ...

agent_spawns_allowed_in:     # subagents only during planning phase
  - plan

edits_allowed_in:            # file edits restricted to execute/verify/ship phases
  - execute                  # plan phase is read-only for project files
  - verify                   # (exception: ~/.claude/plans/* always writable)
  - ship

allowed_write_paths:
  - "./"
```

### Hooks

Three Claude Code hooks run for `flow` sessions via `~/.claude/settings.json`, plus one git hook installed per-repo:

| Hook | File | Purpose |
|---|---|---|
| `Stop` | `hooks/stop.py` | Captures token usage → subscription quota window (SQLite + Langfuse) on session end |
| `PreToolUse` | `hooks/pretool.py` | Step counter, bash allowlist, Agent spawn gate, API spend gate, quota warnings |
| `PreCompact` | `hooks/precompact.py` | Injects custom compaction prompt that preserves RunState artifacts |
| `post-merge` (git) | `hooks/postmerge.py` | Checks the active run's PR via `gh`; auto-closes the run when the PR is merged |

Hooks only fire when you launch Claude Code through `flow` — regular `claude` sessions are unaffected.

### Hook health

`flow init` writes hook commands as **`{sys.executable} -m flow.hooks.*`**, not bare `python3`, because on many machines `python3` on `PATH` is a different interpreter than the one that installed `flow` (e.g. Homebrew 3.14 vs Apple CLT 3.9). If the hook’s interpreter cannot `import flow`, Claude Code still runs tools: the hook process exits with `ModuleNotFoundError` and enforcement silently disappears — `/approve` never sees plan steps, `step:N/M` on the prompt stays flat, Agent spawn / edit gates / step metering do not run, and **Stop** may not record subscription usage for IDE sessions.

- **`flow doctor`** — for each configured hook: checks `import flow` on the command’s interpreter and runs a small stdin smoke test (`PreToolUse` with `FLOW_ACTIVE=1`, etc.).
- **`flow doctor --fix`** — same as **`flow init --force`**: rewrite `~/.claude/settings.json` with this install’s `sys.executable`.
- **Auto-repair on `flow init`** — if hooks already exist but their interpreter fails `import flow`, `flow init` (without `--force`) still rewrites hooks so a broken install self-heals on the next init.
- **`flow verify`** and the **REPL startup banner** print a one-line hook health summary when something is wrong.

### Known limitations

Hook-based enforcement is the current mechanism but it has real fragility:

- **Claude Code-specific.** Hooks live in `~/.claude/settings.json` and are invoked by the Claude Code runtime. There is no equivalent for any other worker — adding a second model means building a separate enforcement surface from scratch.
- **Side-channel, not in-band.** Hooks fire at session boundaries (`Stop`) or before tool dispatch (`PreToolUse`), but they have no visibility inside a turn. A sufficiently long turn can do a lot of work before any hook can intervene.
- **Bypassable.** If a session is launched outside `flow` (plain `claude` in the terminal), `FLOW_ACTIVE` is never set and no hooks fire. Enforcement only holds when `flow` owns the subprocess.

**Direction.** The longer-term model is API-forward enforcement: structured API calls with explicit checkpoints between turns, where the orchestrator intercepts and evaluates before the next turn begins — rather than relying on runtime hooks injected into a subprocess it doesn't fully control. This generalizes across workers and makes the enforcement boundary unambiguous.

---

## Observability

Tightening the orchestrator requires **closing the loop on worker behavior**: how Claude Code actually runs under our injected context, not only whether a turn finished. That implies durable signals you can slice by project, phase, and run.

**Questions observability should eventually answer well**

- Which tools (Read / Write / Bash / …) dominate time and budget for a given phase?
- What does the worker attempt that the harness rejects (PreToolUse blocks, spawn denials), and how often?
- Which turns are fast vs slow for the same phase model (latency outliers, not only token totals)?
- How do briefing changes or constraint changes correlate with outcome (verify pass, check blockers, ship retries)?

**What is emitted today**

[Langfuse](https://cloud.langfuse.com) (optional, when `LANGFUSE_*` keys are set) only shows **events this harness posts** via the Langfuse SDK. It does **not** auto-instrument Anthropic or Claude Code, and it is **not** a live trace of every model HTTP call inside a worker turn — only **aggregates and milestones** the hooks and run manager send:

- **Run root trace** — start + milestones (`phase:*`, `plan_set`, `run_complete`, `pr_created`, …)
- **Session end roll-up** — aggregate token counts from the Stop hook payload per finished session
- **Subagent gate** — allow/deny + reason from `PreToolUse` when the Agent tool is evaluated

**SQLite (`~/.flow/costs.sqlite`)** is authoritative for money and quotas: subscription session rows and API-metered `flow` utilities via `metered_call`. Utility calls are not currently mirrored into Langfuse; `flow stats` reads SQLite so you can operate with zero Langfuse dependency.

**Gaps (direction, not yet first-class)**

Per-tool traces inside a Claude Code turn (each Bash / Write with outcome and latency), structured hook denials as first-class spans, and richer joins from prompting + policy version → tool graph are the next layer. Baseline today; granularity rises as [N grows](#map-reduce-scaling-path).

---

## API mode

To bill Claude Code sessions via the API instead of your subscription:

```sh
FLOW_FORCE_API_KEY=1   # in ~/.flow/.env or shell
```

The Stop hook routes session tokens through the `api` billing path and computes real USD. **Before doing this, set a workspace spend cap in the [Anthropic console](https://console.anthropic.com) — Claude Code sessions can run 2–10M tokens and flow's step gate doesn't cap in-session spend.**

---

## Style system

Flow injects your personal style into every AI-generated artifact. `flow init` creates `~/.flow/style.yaml` with defaults — edit what you care about, set a section to `null` to skip it:

```yaml
commit_message:
  format: "short, imperative, no label prefix"
  max_length: 72

pr_title:
  format: "plain description, sentence case, no prefix brackets"

pr_body: |
  ## What
  {what}

  ## Why
  {why}

  ## Checklist
  - [ ] tests pass

ci_review:
  tone: "direct, no filler, flag real issues only"
  severity_labels: [blocker, suggestion, nit]

agent:
  verbosity: concise
  emoji: false
  confirm_before_destructive: true

ship:
  branch_from_goal: true
  branch_prefix: "feat/"
  pr_title_from_goal: false
  pr_title_prefix: ""
```

Per-repo overrides: create `.flow-style.yaml` in the repo root. It deep-merges on top of the global file. Each call site only receives the sections it needs — no context bloat.

---

## Self-hosted development

Harness changes are exercised on this repo with the same surface users run: `flow` REPL (Claude Code subprocess with hooks), `flow verify`, `flow check`, ship/review paths, and `constraints.yaml` / hook behavior under real sessions. Regressions surface through those entrypoints, not only through README or design notes.
