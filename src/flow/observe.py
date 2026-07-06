"""Langfuse observability — OTEL-based SDK v3+.

Trace IDs must be 32 lowercase hex characters (Langfuse requirement). Short IDs
like DuckDB run_id are mapped deterministically via SHA-256.

Env: LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY, LANGFUSE_HOST (optional).
Import this module only after dotenv/config has loaded when tracing from CLIs.
"""
from __future__ import annotations

import hashlib
import json
import os
from typing import Optional


def _trace_id_hex(seed: str) -> str:
    """Deterministic 32-char hex trace id for a stable string seed."""
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:32]


def _meta_str(d: Optional[dict]) -> dict[str, str]:
    """Flatten metadata to string values (Langfuse string metadata conventions)."""
    if not d:
        return {}
    out: dict[str, str] = {}
    for k, v in d.items():
        if v is None:
            continue
        if isinstance(v, str):
            s = v
        else:
            s = json.dumps(v, default=str)
        out[str(k)] = s[:200]
    return out


def _client():
    """Lazy Langfuse client — returns None if keys not configured."""
    pk = os.getenv("LANGFUSE_PUBLIC_KEY", "")
    sk = os.getenv("LANGFUSE_SECRET_KEY", "")
    host = os.getenv("LANGFUSE_HOST", "https://cloud.langfuse.com")
    if not pk or not sk:
        return None
    try:
        from langfuse import Langfuse

        return Langfuse(public_key=pk, secret_key=sk, host=host)
    except Exception:
        return None


def _run_trace_id(run_id: str) -> str:
    return _trace_id_hex(f"flow:run:{run_id}")


def _claude_session_trace_id(run_id: str, session_id: str) -> str:
    return _trace_id_hex(f"flow:claude_session:{run_id}:{session_id}")


def trace_run_started(
    run_id: str,
    project: str,
    branch: str,
    goal: str,
) -> None:
    """Open a root trace for an flow run (call once from create_run)."""
    lf = _client()
    if not lf or not run_id or run_id == "none":
        return
    try:
        tid = _run_trace_id(run_id)
        span = lf.start_span(
            trace_context={"trace_id": tid},
            name="flow-run",
            metadata=_meta_str({"run_id": run_id, "branch": branch, "feature": "flow"}),
            input={"goal": (goal[:500] + "…") if len(goal) > 500 else goal or ""},
        )
        span.update_trace(
            name=f"run:{run_id}",
            user_id=project,
            session_id=run_id,
            tags=["flow", project, branch],
            metadata=_meta_str({"run_id": run_id, "branch": branch}),
        )
        span.end()
        lf.flush()
    except Exception:
        pass


def trace_session(
    session_id: str,
    run_id: str,
    project: str,
    branch: str,
    phase: str,
    model: str,
    tokens_in: int,
    tokens_out: int,
    cost_usd: float,
    context_tokens: int = 0,
    duration_s: float = 0.0,
    metadata: Optional[dict] = None,
) -> None:
    """Record one Claude Code session completion as a generation on its own trace."""
    lf = _client()
    if not lf:
        return
    try:
        tid = _claude_session_trace_id(run_id, session_id)
        lf_session = run_id if run_id and run_id != "none" else f"no-run:{session_id}"
        meta = {
            "run_id": run_id,
            "project": project,
            "branch": branch,
            "phase": phase,
            "claude_session_id": session_id,
            "context_tokens": str(context_tokens),
            "duration_s": str(duration_s),
            "feature": "flow",
            **{k: str(v) for k, v in (metadata or {}).items()},
        }
        gen = lf.start_observation(
            trace_context={"trace_id": tid},
            name=f"claude-code:{phase}",
            as_type="generation",
            model=model,
            usage_details={
                "prompt_tokens": tokens_in,
                "completion_tokens": tokens_out,
                "total_tokens": tokens_in + tokens_out,
            },
            cost_details={"total_cost": cost_usd} if cost_usd else None,
            metadata=_meta_str(meta),
            input={"phase": phase, "model": model},
        )
        gen.update_trace(
            user_id=project,
            session_id=lf_session,
            tags=["flow", project, phase, branch],
            metadata=_meta_str(
                {
                    "run_id": run_id,
                    "billing_source": (metadata or {}).get("billing_source", ""),
                }
            ),
        )
        gen.end()
        lf.flush()
    except Exception:
        pass


def trace_subagent(
    session_id: str,
    run_id: str,
    project: str,
    phase: str,
    allowed: bool,
    block_reason: str = "",
) -> None:
    """Subagent gate decision on the same trace as the active Claude session."""
    lf = _client()
    if not lf:
        return
    try:
        tid = _claude_session_trace_id(run_id, session_id)
        lf.create_event(
            trace_context={"trace_id": tid},
            name="subagent_spawn",
            metadata=_meta_str(
                {
                    "run_id": run_id,
                    "project": project,
                    "phase": phase,
                    "allowed": allowed,
                    "block_reason": block_reason or "",
                }
            ),
        )
        lf.flush()
    except Exception:
        pass


def trace_run_event(
    run_id: str,
    project: str,
    event: str,
    metadata: Optional[dict] = None,
) -> None:
    """Lifecycle milestone on the run root trace (requires trace_run_started for real run_ids)."""
    lf = _client()
    if not lf or not run_id or run_id == "none":
        return
    try:
        tid = _run_trace_id(run_id)
        lf.create_event(
            trace_context={"trace_id": tid},
            name=event,
            metadata=_meta_str({"project": project, **(metadata or {})}),
        )
        lf.flush()
    except Exception:
        pass


def trace_phase_transition(
    run_id: str,
    project: str,
    from_phase: str,
    to_phase: str,
    duration_s: float = 0.0,
) -> None:
    """Record a phase transition on the run root trace with timing."""
    trace_run_event(run_id, project, f"phase:{to_phase}", {
        "from_phase": from_phase,
        "to_phase": to_phase,
        "duration_s": str(round(duration_s, 1)),
    })


def trace_tool_blocked(
    run_id: str,
    session_id: str,
    project: str,
    phase: str,
    tool_name: str,
    reason: str,
) -> None:
    """Record a hook block decision on the active session trace."""
    lf = _client()
    if not lf:
        return
    try:
        tid = _claude_session_trace_id(run_id, session_id)
        lf.create_event(
            trace_context={"trace_id": tid},
            name="tool_blocked",
            metadata=_meta_str({
                "run_id": run_id,
                "project": project,
                "phase": phase,
                "tool_name": tool_name,
                "reason": reason,
            }),
        )
        lf.flush()
    except Exception:
        pass


def shutdown_observe() -> None:
    """Best-effort flush + shutdown before process exit (optional for REPL/scripts)."""
    lf = _client()
    if not lf:
        return
    try:
        lf.flush()
        lf.shutdown()
    except Exception:
        pass
