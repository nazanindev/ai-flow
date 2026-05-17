"""DuckDB-backed store for RunState, sessions, and subagent events."""
import json
import time
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Optional

import duckdb

from flow.config import DB_PATH

# DuckDB allows only one read-write connection at a time per file.
# _db_lock serializes in-process threads; the retry loop handles cross-process
# contention from claude subprocess hooks that also open DuckDB briefly.
_db_lock = threading.Lock()


@contextmanager
def _conn():
    with _db_lock:
        last_err: Exception = RuntimeError("duckdb.connect never attempted")
        for attempt in range(6):
            try:
                con = duckdb.connect(str(DB_PATH))
                break
            except Exception as exc:
                last_err = exc
                time.sleep(0.02 * (2 ** attempt))  # 20 40 80 160 320 ms
        else:
            raise last_err
        try:
            yield con
        finally:
            con.close()


class Phase(str, Enum):
    plan = "plan"
    execute = "execute"
    verify = "verify"
    ship = "ship"


class RunStatus(str, Enum):
    active = "active"
    blocked = "blocked"
    complete = "complete"
    failed = "failed"


@dataclass
class RunState:
    goal: str
    project: str
    branch: str
    run_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    phase: Phase = Phase.plan
    current_step: int = 0
    max_steps: int = 20
    artifacts: list = field(default_factory=list)
    decisions: list = field(default_factory=list)
    plan_steps: list = field(default_factory=list)
    status: RunStatus = RunStatus.active
    context_summary: str = ""
    # Real API spend only (flow utility calls: clarify, ship, ci-review)
    cost_usd: float = 0.0
    model: str = "claude-sonnet-4-6"
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    step_budget_used: float = 0.0
    pr_url: str = ""
    # Subscription quota consumed by Claude Code sessions for this run
    subscription_msgs: int = 0
    subscription_tokens_in: int = 0
    subscription_tokens_out: int = 0
    # Claude Code headless session (for --resume across flow turns)
    claude_session_id: str = ""
    # Optional feature primitive ID attached to this run
    feature_id: str = ""
    # Verify-gate: persisted check state across sessions
    check_blockers_acked: bool = False
    last_check_result: str = ""
    # Observability: UTC ISO timestamp when current phase started
    phase_started_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())




def activity_path(run_id: str) -> Path:
    """Path to the lightweight activity file written by pretool on each allowed tool call."""
    return DB_PATH.parent / f"activity_{run_id}.json"


def _window_start_for(dt: datetime) -> str:
    """Return the start of the 5-hour quota window containing dt (UTC)."""
    bucket = (dt.hour // 5) * 5
    w = dt.replace(hour=bucket, minute=0, second=0, microsecond=0)
    return w.isoformat()


def current_window_start() -> str:
    return _window_start_for(datetime.now(timezone.utc))


def init_db() -> None:
    with _conn() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS runs (
                run_id VARCHAR PRIMARY KEY,
                project VARCHAR,
                branch VARCHAR,
                goal TEXT,
                phase VARCHAR,
                current_step INTEGER,
                max_steps INTEGER,
                artifacts JSON,
                decisions JSON,
                plan_steps JSON,
                status VARCHAR,
                context_summary TEXT,
                cost_usd DOUBLE,
                model VARCHAR,
                created_at VARCHAR,
                updated_at VARCHAR,
                step_budget_used DOUBLE,
                pr_url VARCHAR,
                subscription_msgs INTEGER,
                subscription_tokens_in INTEGER,
                subscription_tokens_out INTEGER,
                claude_session_id VARCHAR DEFAULT '',
                feature_id VARCHAR DEFAULT ''
            )
        """)
        for migration in [
            "ALTER TABLE runs ADD COLUMN IF NOT EXISTS plan_steps JSON",
            "ALTER TABLE runs ADD COLUMN IF NOT EXISTS step_budget_used DOUBLE DEFAULT 0.0",
            "ALTER TABLE runs ADD COLUMN IF NOT EXISTS pr_url VARCHAR DEFAULT ''",
            "ALTER TABLE runs ADD COLUMN IF NOT EXISTS subscription_msgs INTEGER DEFAULT 0",
            "ALTER TABLE runs ADD COLUMN IF NOT EXISTS subscription_tokens_in INTEGER DEFAULT 0",
            "ALTER TABLE runs ADD COLUMN IF NOT EXISTS subscription_tokens_out INTEGER DEFAULT 0",
            "ALTER TABLE runs ADD COLUMN IF NOT EXISTS claude_session_id VARCHAR DEFAULT ''",
            "ALTER TABLE runs ADD COLUMN IF NOT EXISTS feature_id VARCHAR DEFAULT ''",
            "ALTER TABLE runs ADD COLUMN IF NOT EXISTS check_blockers_acked BOOLEAN DEFAULT false",
            "ALTER TABLE runs ADD COLUMN IF NOT EXISTS last_check_result TEXT DEFAULT ''",
            "ALTER TABLE runs ADD COLUMN IF NOT EXISTS phase_started_at VARCHAR DEFAULT ''",
        ]:
            try:
                con.execute(migration)
            except Exception:
                pass

        # Phase.clarify removed — migrate any persisted runs still on clarify.
        try:
            con.execute("UPDATE runs SET phase = 'plan' WHERE phase = 'clarify'")
        except Exception:
            pass

        con.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                session_id VARCHAR PRIMARY KEY,
                run_id VARCHAR,
                project VARCHAR,
                branch VARCHAR,
                phase VARCHAR,
                model VARCHAR,
                tokens_in INTEGER,
                tokens_out INTEGER,
                cost_usd DOUBLE,
                context_tokens INTEGER,
                duration_s DOUBLE,
                created_at VARCHAR,
                billing_source VARCHAR
            )
        """)
        for migration in [
            "ALTER TABLE sessions ADD COLUMN IF NOT EXISTS billing_source VARCHAR DEFAULT 'subscription'",
        ]:
            try:
                con.execute(migration)
            except Exception:
                pass

        con.execute("""
            CREATE TABLE IF NOT EXISTS subagent_spawns (
                id VARCHAR PRIMARY KEY,
                session_id VARCHAR,
                run_id VARCHAR,
                project VARCHAR,
                phase VARCHAR,
                description TEXT,
                allowed BOOLEAN,
                block_reason VARCHAR,
                created_at VARCHAR
            )
        """)

        # 5-hour subscription quota windows
        con.execute("""
            CREATE TABLE IF NOT EXISTS subscription_windows (
                window_start VARCHAR PRIMARY KEY,
                plan VARCHAR,
                msgs_used INTEGER,
                tokens_in INTEGER,
                tokens_out INTEGER,
                updated_at VARCHAR
            )
        """)

        # Structured event log: phase transitions, blocks, session ends, verify/check results
        con.execute("""
            CREATE TABLE IF NOT EXISTS events (
                id VARCHAR PRIMARY KEY,
                run_id VARCHAR NOT NULL,
                project VARCHAR,
                event_type VARCHAR NOT NULL,
                phase VARCHAR,
                tool_name VARCHAR,
                blocked BOOLEAN DEFAULT FALSE,
                block_reason VARCHAR,
                metadata VARCHAR,
                created_at VARCHAR
            )
        """)


def save_run(run: RunState) -> None:
    run.updated_at = datetime.now(timezone.utc).isoformat()
    with _conn() as con:
        con.execute("""
            INSERT OR REPLACE INTO runs (
                run_id, project, branch, goal,
                phase, current_step, max_steps,
                artifacts, decisions, plan_steps,
                status, context_summary, cost_usd,
                model, created_at, updated_at,
                step_budget_used, pr_url,
                subscription_msgs, subscription_tokens_in, subscription_tokens_out,
                claude_session_id, feature_id,
                check_blockers_acked, last_check_result,
                phase_started_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, [
            run.run_id, run.project, run.branch, run.goal,
            run.phase.value, run.current_step, run.max_steps,
            json.dumps(run.artifacts), json.dumps(run.decisions),
            json.dumps(run.plan_steps),
            run.status.value, run.context_summary, run.cost_usd,
            run.model, run.created_at, run.updated_at,
            run.step_budget_used, run.pr_url,
            run.subscription_msgs, run.subscription_tokens_in,
            run.subscription_tokens_out,
            run.claude_session_id or "",
            run.feature_id or "",
            run.check_blockers_acked,
            run.last_check_result or "",
            run.phase_started_at or "",
        ])


def _phase_from_stored(value: object) -> Phase:
    """Map DB phase strings to Phase; unknown values (e.g. legacy 'clarify') → plan."""
    s = (value or "").strip() if isinstance(value, str) else ""
    if not s:
        return Phase.plan
    try:
        return Phase(s)
    except ValueError:
        return Phase.plan


def load_run(run_id: str) -> Optional[RunState]:
    cols = [
        "run_id", "project", "branch", "goal", "phase", "current_step", "max_steps",
        "artifacts", "decisions", "plan_steps", "status", "context_summary", "cost_usd",
        "model", "created_at", "updated_at", "step_budget_used", "pr_url",
        "subscription_msgs", "subscription_tokens_in", "subscription_tokens_out",
        "claude_session_id", "feature_id",
        "check_blockers_acked", "last_check_result",
        "phase_started_at",
    ]
    with _conn() as con:
        row = con.execute(
            f"SELECT {', '.join(cols)} FROM runs WHERE run_id = ?", [run_id]
        ).fetchone()
    if not row:
        return None
    d = dict(zip(cols, row))
    d["artifacts"] = json.loads(d["artifacts"] or "[]")
    d["decisions"] = json.loads(d["decisions"] or "[]")
    d["plan_steps"] = json.loads(d["plan_steps"] or "[]")
    d["phase"] = _phase_from_stored(d["phase"])
    d["status"] = RunStatus(d["status"])
    d["step_budget_used"] = float(d["step_budget_used"] or 0.0)
    d["pr_url"] = d["pr_url"] or ""
    d["subscription_msgs"] = int(d["subscription_msgs"] or 0)
    d["subscription_tokens_in"] = int(d["subscription_tokens_in"] or 0)
    d["subscription_tokens_out"] = int(d["subscription_tokens_out"] or 0)
    d["claude_session_id"] = str(d.get("claude_session_id") or "")
    d["feature_id"] = str(d.get("feature_id") or "")
    d["check_blockers_acked"] = bool(d.get("check_blockers_acked") or False)
    d["last_check_result"] = str(d.get("last_check_result") or "")
    d["phase_started_at"] = str(d.get("phase_started_at") or "")
    return RunState(**{k: v for k, v in d.items() if k in RunState.__dataclass_fields__})


def load_active_run(project: str, branch: Optional[str] = None) -> Optional[RunState]:
    with _conn() as con:
        if branch:
            row = con.execute("""
                SELECT run_id FROM runs
                WHERE project = ? AND branch = ? AND status = 'active'
                ORDER BY updated_at DESC LIMIT 1
            """, [project, branch]).fetchone()
        else:
            row = con.execute("""
                SELECT run_id FROM runs
                WHERE project = ? AND status = 'active'
                ORDER BY updated_at DESC LIMIT 1
            """, [project]).fetchone()
    return load_run(row[0]) if row else None


def get_incomplete_runs(project: str, limit: int = 20) -> list:
    """List non-complete runs for a project, newest first."""
    with _conn() as con:
        rows = con.execute("""
            SELECT run_id, goal, phase, status, cost_usd, updated_at
            FROM runs
            WHERE project = ? AND status != 'complete'
            ORDER BY updated_at DESC
            LIMIT ?
        """, [project, limit]).fetchall()
    cols = ["run_id", "goal", "phase", "status", "cost_usd", "updated_at"]
    return [dict(zip(cols, r)) for r in rows]


def cleanup_incomplete_runs(project: str, keep_run_id: str = "", include_keep: bool = False) -> int:
    """Mark incomplete runs as complete for quick hygiene."""
    with _conn() as con:
        if include_keep:
            row = con.execute("""
                UPDATE runs
                SET status = 'complete', updated_at = ?
                WHERE project = ? AND status != 'complete'
                RETURNING run_id
            """, [datetime.now(timezone.utc).isoformat(), project]).fetchall()
        else:
            row = con.execute("""
                UPDATE runs
                SET status = 'complete', updated_at = ?
                WHERE project = ? AND status != 'complete' AND run_id != ?
                RETURNING run_id
            """, [datetime.now(timezone.utc).isoformat(), project, keep_run_id]).fetchall()
    return len(row or [])


def save_session(
    session_id: str, run_id: str, project: str, branch: str, phase: str,
    model: str, tokens_in: int, tokens_out: int, cost_usd: float,
    context_tokens: int = 0, duration_s: float = 0.0,
    billing_source: str = "subscription",
) -> None:
    with _conn() as con:
        con.execute("""
            INSERT OR REPLACE INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, [
            session_id, run_id, project, branch, phase, model,
            tokens_in, tokens_out, cost_usd, context_tokens, duration_s,
            datetime.now(timezone.utc).isoformat(),
            billing_source,
        ])


def record_subscription_window(
    tokens_in: int, tokens_out: int, plan: str = "pro",
) -> None:
    """Upsert quota usage into the current 5-hour window bucket."""
    ws = current_window_start()
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as con:
        existing = con.execute(
            "SELECT msgs_used, tokens_in, tokens_out FROM subscription_windows WHERE window_start = ?",
            [ws],
        ).fetchone()
        if existing:
            con.execute("""
                UPDATE subscription_windows
                SET msgs_used = ?, tokens_in = ?, tokens_out = ?, updated_at = ?
                WHERE window_start = ?
            """, [
                existing[0] + 1,
                existing[1] + tokens_in,
                existing[2] + tokens_out,
                now, ws,
            ])
        else:
            con.execute("""
                INSERT INTO subscription_windows VALUES (?,?,?,?,?,?)
            """, [ws, plan, 1, tokens_in, tokens_out, now])


def get_window_usage(plan: str = "pro") -> dict:
    """Return quota usage for the current 5-hour window."""
    ws = current_window_start()
    with _conn() as con:
        row = con.execute(
            "SELECT msgs_used, tokens_in, tokens_out FROM subscription_windows WHERE window_start = ?",
            [ws],
        ).fetchone()
    if not row:
        return {"msgs_used": 0, "tokens_in": 0, "tokens_out": 0, "window_start": ws}
    return {
        "msgs_used": row[0],
        "tokens_in": row[1],
        "tokens_out": row[2],
        "window_start": ws,
    }


def save_subagent_event(
    session_id: str, run_id: str, project: str, phase: str,
    description: str, allowed: bool, block_reason: str = "",
) -> None:
    with _conn() as con:
        con.execute("""
            INSERT INTO subagent_spawns VALUES (?,?,?,?,?,?,?,?,?)
        """, [
            str(uuid.uuid4()), session_id, run_id, project, phase,
            description, allowed, block_reason, datetime.now(timezone.utc).isoformat(),
        ])


def get_api_spend_today(project: Optional[str] = None) -> float:
    """Real $ spent today via ANTHROPIC_API_KEY (flow utility calls only)."""
    with _conn() as con:
        if project:
            row = con.execute("""
                SELECT COALESCE(SUM(cost_usd), 0) FROM sessions
                WHERE billing_source = 'api' AND project = ?
                AND substr(created_at, 1, 10) >= current_date::VARCHAR
            """, [project]).fetchone()
        else:
            row = con.execute("""
                SELECT COALESCE(SUM(cost_usd), 0) FROM sessions
                WHERE billing_source = 'api'
                AND substr(created_at, 1, 10) >= current_date::VARCHAR
            """).fetchone()
    return row[0] if row else 0.0


def get_cost_today(project: Optional[str] = None) -> float:
    """Alias for get_api_spend_today (kept for backward compatibility)."""
    return get_api_spend_today(project)


def get_subscription_tokens_today(project: Optional[str] = None) -> dict:
    """Total subscription tokens sent through Claude Code sessions today."""
    with _conn() as con:
        if project:
            row = con.execute("""
                SELECT COALESCE(SUM(tokens_in), 0), COALESCE(SUM(tokens_out), 0)
                FROM sessions
                WHERE billing_source = 'subscription' AND project = ?
                AND substr(created_at, 1, 10) >= current_date::VARCHAR
            """, [project]).fetchone()
        else:
            row = con.execute("""
                SELECT COALESCE(SUM(tokens_in), 0), COALESCE(SUM(tokens_out), 0)
                FROM sessions
                WHERE billing_source = 'subscription'
                AND substr(created_at, 1, 10) >= current_date::VARCHAR
            """).fetchone()
    return {"tokens_in": row[0] if row else 0, "tokens_out": row[1] if row else 0}


def get_project_stats() -> list:
    with _conn() as con:
        rows = con.execute("""
            SELECT project,
                   COUNT(*) as sessions,
                   COALESCE(SUM(CASE WHEN billing_source = 'api' THEN cost_usd ELSE 0 END), 0) as api_spend,
                   COALESCE(SUM(tokens_in + tokens_out), 0) as total_tokens,
                   COALESCE(SUM(CASE WHEN billing_source = 'subscription' THEN tokens_in + tokens_out ELSE 0 END), 0) as sub_tokens,
                   MAX(created_at) as last_active
            FROM sessions
            GROUP BY project
            ORDER BY api_spend DESC
        """).fetchall()
    cols = ["project", "sessions", "api_spend", "total_tokens", "sub_tokens", "last_active"]
    return [dict(zip(cols, r)) for r in rows]


def get_cost_per_pr(project: Optional[str] = None) -> list:
    """Return cost + step stats for all shipped runs that have a PR URL."""
    with _conn() as con:
        if project:
            rows = con.execute("""
                SELECT run_id, goal, pr_url, cost_usd, step_budget_used, updated_at,
                       subscription_msgs, subscription_tokens_in, subscription_tokens_out
                FROM runs WHERE pr_url != '' AND pr_url IS NOT NULL AND project = ?
                ORDER BY updated_at DESC
            """, [project]).fetchall()
        else:
            rows = con.execute("""
                SELECT run_id, goal, pr_url, cost_usd, step_budget_used, updated_at,
                       subscription_msgs, subscription_tokens_in, subscription_tokens_out
                FROM runs WHERE pr_url != '' AND pr_url IS NOT NULL
                ORDER BY updated_at DESC
            """).fetchall()
    cols = [
        "run_id", "goal", "pr_url", "cost_usd", "step_budget_used", "updated_at",
        "subscription_msgs", "subscription_tokens_in", "subscription_tokens_out",
    ]
    return [dict(zip(cols, r)) for r in rows]


def get_recent_runs(project: Optional[str] = None, limit: int = 10) -> list:
    with _conn() as con:
        if project:
            rows = con.execute("""
                SELECT run_id, goal, phase, status, cost_usd, updated_at,
                       subscription_msgs, subscription_tokens_in, subscription_tokens_out,
                       project, branch
                FROM runs WHERE project = ?
                ORDER BY updated_at DESC LIMIT ?
            """, [project, limit]).fetchall()
        else:
            rows = con.execute("""
                SELECT run_id, goal, phase, status, cost_usd, updated_at,
                       subscription_msgs, subscription_tokens_in, subscription_tokens_out,
                       project, branch
                FROM runs ORDER BY updated_at DESC LIMIT ?
            """, [limit]).fetchall()
    cols = [
        "run_id", "goal", "phase", "status", "cost_usd", "updated_at",
        "subscription_msgs", "subscription_tokens_in", "subscription_tokens_out",
        "project", "branch",
    ]
    return [dict(zip(cols, r)) for r in rows]


def save_event(
    run_id: str,
    event_type: str,
    project: str = "",
    phase: str = "",
    tool_name: str = "",
    blocked: bool = False,
    block_reason: str = "",
    metadata: Optional[dict] = None,
) -> None:
    """Append a structured event to the events table (append-only, non-blocking on error)."""
    try:
        with _conn() as con:
            con.execute("""
                INSERT INTO events
                    (id, run_id, project, event_type, phase, tool_name,
                     blocked, block_reason, metadata, created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?)
            """, [
                str(uuid.uuid4()),
                run_id,
                project or "",
                event_type,
                phase or "",
                tool_name or "",
                blocked,
                block_reason or "",
                json.dumps(metadata) if metadata else "",
                datetime.now(timezone.utc).isoformat(),
            ])
    except Exception:
        pass


def get_run_events(run_id: str) -> list:
    """Return all events for a run ordered by created_at."""
    with _conn() as con:
        rows = con.execute("""
            SELECT event_type, phase, tool_name, blocked, block_reason, metadata, created_at
            FROM events WHERE run_id = ?
            ORDER BY created_at ASC
        """, [run_id]).fetchall()
    cols = ["event_type", "phase", "tool_name", "blocked", "block_reason", "metadata", "created_at"]
    result = []
    for r in rows:
        d = dict(zip(cols, r))
        try:
            d["metadata"] = json.loads(d["metadata"]) if d["metadata"] else {}
        except Exception:
            d["metadata"] = {}
        result.append(d)
    return result


def get_recent_blocks(project: str, n: int = 20) -> list:
    """Return the most recent tool_blocked events for a project."""
    with _conn() as con:
        rows = con.execute("""
            SELECT e.run_id, e.phase, e.tool_name, e.block_reason, e.created_at
            FROM events e
            JOIN runs r ON r.run_id = e.run_id
            WHERE e.event_type = 'tool_blocked' AND r.project = ?
            ORDER BY e.created_at DESC LIMIT ?
        """, [project, n]).fetchall()
    cols = ["run_id", "phase", "tool_name", "block_reason", "created_at"]
    return [dict(zip(cols, r)) for r in rows]
