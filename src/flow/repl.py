"""Backward-compatible entry point.

The orchestrator was split out of this module: `AgentSession` now lives in
`flow.session` and `FlowOrchestrator` in `flow.orchestrator` (assembled from the
`flow.runner`, `flow.workers`, and `flow.controls` mixins). This module re-exports
them so existing `from flow.repl import ...` call sites keep working, and provides
the REPL entry point.
"""
from flow.session import AgentSession
from flow.orchestrator import FlowOrchestrator

__all__ = ["AgentSession", "FlowOrchestrator", "start_repl"]


def start_repl() -> None:
    from flow.tui import start_tui
    start_tui()
