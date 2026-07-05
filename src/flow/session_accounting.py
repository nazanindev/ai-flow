"""Persist Claude Code session usage (quota window, runs row, sessions, Langfuse)."""
from __future__ import annotations

import os
from typing import Any, Optional, TYPE_CHECKING

from flow.billing import calc_cost
from flow.observe import trace_session
from flow.tracker import (
    load_active_run,
    save_run,
    save_session,
)

if TYPE_CHECKING:
    from flow.tracker import RunState


def usage_from_claude_result(data: dict[str, Any]) -> tuple[int, int, str, int]:
    """Extract (input_tokens, output_tokens, model, cache_read_input_tokens) from CLI JSON."""
    usage = data.get("usage")
    if not isinstance(usage, dict):
        usage = {}
    tin = int(usage.get("input_tokens") or 0)
    tout = int(usage.get("output_tokens") or 0)
    cr = int(usage.get("cache_read_input_tokens") or 0)
    model = str(data.get("model") or "claude-sonnet-4-6")
    return tin, tout, model, cr


def account_claude_code_session_end(
    *,
    project: str,
    branch: str,
    session_id: str,
    model: str,
    tokens_in: int,
    tokens_out: int,
    cache_read_input_tokens: int = 0,
    run: Optional["RunState"] = None,
) -> None:
    """Mirror hooks/stop.py accounting (without clean-state checks)."""
    billing_source = "api" if os.getenv("AP_FORCE_API_KEY") == "1" else "subscription"
    if run is None:
        run = load_active_run(project)
    run_id = run.run_id if run else "none"
    phase = run.phase.value if run else "unknown"
    context_tokens = cache_read_input_tokens + tokens_in

    if billing_source == "subscription":
        cost = 0.0
        if run:
            run.subscription_msgs += 1
            run.subscription_tokens_in += tokens_in
            run.subscription_tokens_out += tokens_out
            save_run(run)
    else:
        cost = calc_cost(model, tokens_in, tokens_out, cache_read_input_tokens)
        if run:
            run.cost_usd += cost
            save_run(run)

    save_session(
        session_id=session_id,
        run_id=run_id,
        project=project,
        branch=branch,
        phase=phase,
        model=model,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cost_usd=cost,
        context_tokens=context_tokens,
        billing_source=billing_source,
    )

    trace_session(
        session_id=session_id,
        run_id=run_id,
        project=project,
        branch=branch,
        phase=phase,
        model=model,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cost_usd=cost,
        context_tokens=context_tokens,
        metadata={"billing_source": billing_source},
    )
