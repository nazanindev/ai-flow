"""AgentSession — the per-session state record shared across the orchestrator.

Kept in its own leaf module (no flow-internal imports) so every orchestrator
mixin can import it without creating an import cycle.
"""
import queue
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, List, Optional


@dataclass
class AgentSession:
    idx: int
    goal: str
    run: Any                          # RunState — owned by session worker thread
    project: str
    branch: str
    cwd: Path
    session_type: str = "executor"    # "executor" | "planner" | "reviewer"
    model_override: Optional[str] = None
    auto_ship: bool = True            # False skips the ship step (used by test sessions)
    thread: Optional[threading.Thread] = None
    output_queue: queue.Queue = field(default_factory=queue.Queue)
    output_history: List[str] = field(default_factory=list)
    inject_queue: queue.Queue = field(default_factory=queue.Queue)
    lock: threading.Lock = field(default_factory=threading.Lock)
    status: str = "running"           # "running" | "done" | "failed"
    last_line: str = ""
    pr_url: str = ""
    started_at: float = field(default_factory=time.monotonic)
    waiting_for_input: bool = False   # planner is paused, needs /prompt
    _turn_depth: int = field(default=0, compare=False)  # recursion guard for _run_turn
