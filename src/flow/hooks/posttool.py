"""
Claude Code PostToolUse hook — invoked as: python3 -m flow.hooks.posttool
Writes a tool_completed event that pairs with the tool_attempted event from pretool.
This closes the pre/post loop needed for exact in-flight detection on resume.
Exit 0 always — PostToolUse hooks cannot block execution.
"""
import json
import os
import sys

from flow.config import STATE_DIR
from dotenv import load_dotenv

load_dotenv(STATE_DIR / ".env")


def main() -> None:
    try:
        payload = json.loads(sys.stdin.read())
    except Exception:
        return

    run_id = os.getenv("FLOW_RUN_ID", "")
    if not run_id or run_id == "none":
        return

    from flow.tracker import init_db, save_event, activity_path
    init_db()

    # Read the attempted_event_id written into the activity file by pretool
    event_id = ""
    try:
        data = json.loads(activity_path(run_id).read_text())
        event_id = data.get("event_id", "")
    except Exception:
        pass

    tool_name = payload.get("tool_name", "")
    tool_response = payload.get("tool_response") or {}
    success = "error" not in tool_response

    save_event(
        run_id,
        "tool_completed",
        tool_name=tool_name,
        metadata={"attempted_event_id": event_id, "success": success},
    )


if __name__ == "__main__":
    main()
