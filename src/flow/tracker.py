"""SQLite-backed store for RunState, sessions, and subagent events."""
import json
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Optional

from flow.config import DB_PATH


@contextmanager
def _conn():
    """Standard read-write connection. All statements execute in a single transaction."""
    con = sqlite3.connect(str(DB_PATH), timeout=10, check_same_thread=False, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("BEGIN")
    try:
        yield con
        con.execute("COMMIT")
    except Exception:
        try:
            con.execute("ROLLBACK")
        except Exception:
            pass
        raise
    finally:
        con.close()


@contextmanager
def _conn_exclusive():
    """Write-locked connection for atomic read-modify-append operations (BEGIN IMMEDIATE)."""
    con = sqlite3.connect(str(DB_PATH), timeout=10, check_same_thread=False, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("BEGIN IMMEDIATE")
    try:
        yield con
        con.execute("COMMIT")
    except Exception:
        try:
            con.execute("ROLLBACK")
        except Exception:
            pass
        raise
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
    cancelled = "cancelled"


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


def _window_end_for(window_start: str) -> str:
    """Return the exclusive end of a 5-hour window given its ISO start."""
    from datetime import timedelta
    dt = datetime.fromisoformat(window_start)
    return (dt + timedelta(hours=5)).isoformat()


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
            "ALTER TABLE runs ADD COLUMN plan_steps JSON",
            "ALTER TABLE runs ADD COLUMN step_budget_used DOUBLE DEFAULT 0.0",
            "ALTER TABLE runs ADD COLUMN pr_url VARCHAR DEFAULT ''",
            "ALTER TABLE runs ADD COLUMN subscription_msgs INTEGER DEFAULT 0",
            "ALTER TABLE runs ADD COLUMN subscription_tokens_in INTEGER DEFAULT 0",
            "ALTER TABLE runs ADD COLUMN subscription_tokens_out INTEGER DEFAULT 0",
            "ALTER TABLE runs ADD COLUMN claude_session_id VARCHAR DEFAULT ''",
            "ALTER TABLE runs ADD COLUMN feature_id VARCHAR DEFAULT ''",
            "ALTER TABLE runs ADD COLUMN check_blockers_acked BOOLEAN DEFAULT 0",
            "ALTER TABLE runs ADD COLUMN last_check_result TEXT DEFAULT ''",
            "ALTER TABLE runs ADD COLUMN phase_started_at VARCHAR DEFAULT ''",
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
            "ALTER TABLE sessions ADD COLUMN billing_source VARCHAR DEFAULT 'subscription'",
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
                blocked BOOLEAN DEFAULT 0,
                block_reason VARCHAR,
                metadata VARCHAR,
                weight REAL DEFAULT 0,
                created_at VARCHAR
            )
        """)
        try:
            con.execute("ALTER TABLE events ADD COLUMN weight REAL DEFAULT 0")
        except Exception:
            pass  # column already exists


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


def try_append_tool_event(
    run_id: str, project: str, tool_name: str, phase: str,
    weight: float, effective_max: float,
) -> tuple[bool, str]:
    """Atomically check budget and append a tool_attempted event.

    Uses BEGIN IMMEDIATE so two concurrent hooks cannot both pass the budget check.
    Returns (True, event_id) if allowed, (False, "") if budget exhausted.
    """
    now = datetime.now(timezone.utc).isoformat()
    event_id = str(uuid.uuid4())
    with _conn_exclusive() as con:
        row = con.execute(
            "SELECT COALESCE(SUM(weight), 0) FROM events WHERE run_id = ? AND event_type = 'tool_attempted'",
            [run_id],
        ).fetchone()
        budget_used = float(row[0]) if row and row[0] is not None else 0.0
        if budget_used >= effective_max:
            return False, ""
        con.execute("""
            INSERT INTO events
                (id, run_id, project, event_type, phase, tool_name,
                 blocked, block_reason, metadata, weight, created_at)
            VALUES (?,?,?,?,?,?,0,'',?,?,?)
        """, [
            event_id, run_id, project or "", "tool_attempted",
            phase or "", tool_name or "",
            json.dumps({"effective_max": effective_max}),
            weight, now,
        ])
        con.execute("""
            UPDATE runs
            SET step_budget_used = step_budget_used + ?,
                current_step = current_step + 1,
                updated_at = ?
            WHERE run_id = ?
        """, [weight, now, run_id])
    return True, event_id


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


def get_window_usage(plan: str = "pro") -> dict:
    """Return quota usage for the current 5-hour window, derived from session_end events."""
    ws = current_window_start()
    window_end = _window_end_for(ws)
    with _conn() as con:
        row = con.execute("""
            SELECT
                COUNT(*) AS msgs_used,
                COALESCE(SUM(CAST(json_extract(metadata, '$.tokens_in') AS INTEGER)), 0) AS tokens_in,
                COALESCE(SUM(CAST(json_extract(metadata, '$.tokens_out') AS INTEGER)), 0) AS tokens_out
            FROM events
            WHERE event_type = 'session_end'
              AND created_at >= ? AND created_at < ?
        """, [ws, window_end]).fetchone()
    if not row:
        return {"msgs_used": 0, "tokens_in": 0, "tokens_out": 0, "window_start": ws}
    return {
        "msgs_used": int(row[0] or 0),
        "tokens_in": int(row[1] or 0),
        "tokens_out": int(row[2] or 0),
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
                AND substr(created_at, 1, 10) >= date('now')
            """, [project]).fetchone()
        else:
            row = con.execute("""
                SELECT COALESCE(SUM(cost_usd), 0) FROM sessions
                WHERE billing_source = 'api'
                AND substr(created_at, 1, 10) >= date('now')
            """).fetchone()
    return row[0] if row else 0.0


def get_subscription_tokens_today(project: Optional[str] = None) -> dict:
    """Total subscription tokens sent through Claude Code sessions today."""
    with _conn() as con:
        if project:
            row = con.execute("""
                SELECT COALESCE(SUM(tokens_in), 0), COALESCE(SUM(tokens_out), 0)
                FROM sessions
                WHERE billing_source = 'subscription' AND project = ?
                AND substr(created_at, 1, 10) >= date('now')
            """, [project]).fetchone()
        else:
            row = con.execute("""
                SELECT COALESCE(SUM(tokens_in), 0), COALESCE(SUM(tokens_out), 0)
                FROM sessions
                WHERE billing_source = 'subscription'
                AND substr(created_at, 1, 10) >= date('now')
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
    base = """
        SELECT r.run_id, r.goal, r.phase, r.status, r.cost_usd, r.updated_at,
               r.subscription_msgs, r.subscription_tokens_in, r.subscription_tokens_out,
               r.project, r.branch, r.created_at, r.pr_url,
               COALESCE(b.cnt, 0) AS block_count
        FROM runs r
        LEFT JOIN (
            SELECT run_id, COUNT(*) AS cnt FROM events
            WHERE event_type = 'tool_blocked' GROUP BY run_id
        ) b ON b.run_id = r.run_id
    """
    with _conn() as con:
        if project:
            rows = con.execute(
                base + " WHERE r.project = ? ORDER BY r.updated_at DESC LIMIT ?",
                [project, limit],
            ).fetchall()
        else:
            rows = con.execute(
                base + " ORDER BY r.updated_at DESC LIMIT ?",
                [limit],
            ).fetchall()
    cols = [
        "run_id", "goal", "phase", "status", "cost_usd", "updated_at",
        "subscription_msgs", "subscription_tokens_in", "subscription_tokens_out",
        "project", "branch", "created_at", "pr_url", "block_count",
    ]
    return [dict(zip(cols, r)) for r in rows]


def get_latest_events(limit: int = 20, run_id: Optional[str] = None) -> list:
    """Return the most recent events across all runs (or filtered to one run)."""
    with _conn() as con:
        if run_id:
            rows = con.execute("""
                SELECT event_type, phase, tool_name, blocked, block_reason,
                       metadata, created_at, run_id
                FROM events WHERE run_id = ?
                ORDER BY created_at DESC LIMIT ?
            """, [run_id, limit]).fetchall()
        else:
            rows = con.execute("""
                SELECT event_type, phase, tool_name, blocked, block_reason,
                       metadata, created_at, run_id
                FROM events ORDER BY created_at DESC LIMIT ?
            """, [limit]).fetchall()
    cols = ["event_type", "phase", "tool_name", "blocked", "block_reason",
            "metadata", "created_at", "run_id"]
    result = []
    for r in rows:
        d = dict(zip(cols, r))
        try:
            d["metadata"] = json.loads(d["metadata"]) if d["metadata"] else {}
        except Exception:
            d["metadata"] = {}
        result.append(d)
    return result


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


def get_inflight_tools(run_id: str) -> list[str]:
    """Return tools attempted in the last session with no matching tool_completed event.

    These represent operations that were in-flight when the session was killed.
    Scoped to events since the most recent session_started/session_resumed boundary.
    """
    with _conn() as con:
        row = con.execute("""
            SELECT COALESCE(MAX(created_at), '1970-01-01') FROM events
            WHERE run_id = ? AND event_type IN ('session_started', 'session_resumed')
        """, [run_id]).fetchone()
        since = row[0] if row else "1970-01-01"
        rows = con.execute("""
            SELECT e.id, e.tool_name FROM events e
            WHERE e.run_id = ?
              AND e.event_type = 'tool_attempted'
              AND e.created_at > ?
              AND NOT EXISTS (
                SELECT 1 FROM events c
                WHERE c.run_id = ?
                  AND c.event_type = 'tool_completed'
                  AND json_extract(c.metadata, '$.attempted_event_id') = e.id
              )
            ORDER BY e.created_at ASC
        """, [run_id, since, run_id]).fetchall()
        return [r[1] for r in rows if r[1]]


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


def set_run_status(run_id: str, status: RunStatus) -> bool:
    """Update a run's status. Returns True if a row was changed."""
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as con:
        result = con.execute(
            "UPDATE runs SET status = ?, updated_at = ? WHERE run_id = ?",
            [status.value, now, run_id],
        )
        return (result.rowcount or 0) > 0


def retry_run(run_id: str) -> bool:
    """Set a blocked/failed run back to active so the user can resume it."""
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as con:
        result = con.execute(
            "UPDATE runs SET status = 'active', updated_at = ? WHERE run_id = ? AND status IN ('blocked','failed')",
            [now, run_id],
        )
        return (result.rowcount or 0) > 0


def delete_run(run_id: str) -> bool:
    """Hard-delete a run and its events. Sessions are kept as billing records."""
    with _conn() as con:
        con.execute("DELETE FROM events WHERE run_id = ?", [run_id])
        result = con.execute("DELETE FROM runs WHERE run_id = ?", [run_id])
        return (result.rowcount or 0) > 0
