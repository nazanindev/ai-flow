"""
Claude Code PreCompact hook — invoked as: python3 -m flow.hooks.precompact
Provides a custom compaction prompt that preserves RunState artifacts
and drops conversation noise.
"""
import json
import os
import sys

from flow.config import STATE_DIR
from dotenv import load_dotenv

load_dotenv(STATE_DIR / ".env")

from flow.config import get_project_id
from flow.tracker import init_db, load_active_run

COMPACTION_PROMPT = """You are compacting a Claude Code session. Preserve ALL of the following — discard everything else:

1. **Goal**: The original task goal in one sentence.
2. **Phase**: Current phase (plan/execute/verify/ship) and step number.
3. **Plan**: The complete plan if one was produced (every step, numbered).
4. **Decisions**: Key decisions made so far and the reasoning (bullet list).
5. **Artifacts**: Files created/modified, test results, diffs (filenames + status).
6. **Current state**: What was just completed and what comes next.
7. **Constraints**: Any active constraints or overrides (/no-agents, budget limit, etc.).

Do NOT preserve: conversational back-and-forth, repeated tool output, intermediate reasoning that led to a decision already recorded above.

Format the summary as structured markdown under these exact headers."""


def main() -> None:
    if os.getenv("FLOW_ACTIVE") != "1":
        sys.exit(0)

    init_db()
    project = get_project_id()
    run = load_active_run(project)

    context_addon = ""
    if run:
        context_addon = f"""
## Active Run Context
- Run ID: {run.run_id}
- Goal: {run.goal}
- Phase: {run.phase.value} (step {run.current_step}/{run.max_steps})
- Status: {run.status.value}
- API spend so far: ${run.cost_usd:.4f} (subscription: {run.subscription_msgs} msgs)
"""
        if run.plan_steps:
            context_addon += "\n### Plan Steps\n"
            for s in run.plan_steps:
                marker = "x" if s.get("status") == "done" else " "
                context_addon += f"- [{marker}] {s['description']}\n"

    output = {
        "summary_prompt": COMPACTION_PROMPT + context_addon,
    }
    print(json.dumps(output))


if __name__ == "__main__":
    main()
