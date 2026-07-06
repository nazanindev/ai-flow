"""flow serve — local FastAPI dashboard on :7331."""
import os
from pathlib import Path

from rich.console import Console

console = Console()

_HTML = (Path(__file__).parent / "dashboard.html").read_text(encoding="utf-8")


def cmd_serve(port: int = 7331) -> None:
    try:
        from fastapi import FastAPI
        from fastapi.responses import HTMLResponse, JSONResponse
        import uvicorn
    except ImportError:
        console.print("[red]fastapi and uvicorn are required: pip install fastapi uvicorn[/red]")
        raise SystemExit(1)

    import time as _time
    from fastapi import Response
    from flow.tracker import (
        init_db, get_api_spend_today, get_project_stats, get_recent_runs,
        load_active_run, get_window_usage, get_run_events, get_latest_events, activity_path,
        RunStatus, set_run_status, retry_run, delete_run,
    )
    from flow.config import DB_PATH, get_project_id, constraints, get_plan, get_plan_window_caps

    init_db()
    app = FastAPI(title="AI Flow", docs_url=None, redoc_url=None)

    @app.get("/", response_class=HTMLResponse)
    async def dashboard():
        return _HTML

    @app.get("/status")
    async def status():
        project = get_project_id()
        run = load_active_run(project)
        c = constraints()
        plan = get_plan()
        caps = get_plan_window_caps()
        api_gate = float(os.getenv("FLOW_BUDGET_USD") or c.get("api_spend_gate_usd", 1.0))
        window = get_window_usage(plan)
        msg_cap = caps.get(plan, {}).get("msgs", 0)
        api_today = get_api_spend_today(project)
        api_today_all = get_api_spend_today()

        active = None
        phase_elapsed_s = None
        last_tool = None
        last_tool_age_s = None
        if run:
            projected = None
            if run.current_step > 0:
                projected = round(run.cost_usd / run.current_step * run.max_steps, 6)
            active = {
                "run_id": run.run_id,
                "goal": run.goal,
                "phase": run.phase.value,
                "current_step": run.current_step,
                "max_steps": run.max_steps,
                "cost_usd": run.cost_usd,
                "projected_usd": projected,
                "status": run.status.value,
                "plan_steps": run.plan_steps,
                "subscription_msgs": run.subscription_msgs,
                "subscription_tokens_in": run.subscription_tokens_in,
                "subscription_tokens_out": run.subscription_tokens_out,
            }
            # Phase elapsed
            if run.phase_started_at:
                try:
                    from datetime import datetime, timezone
                    started = datetime.fromisoformat(run.phase_started_at)
                    if started.tzinfo is None:
                        started = started.replace(tzinfo=timezone.utc)
                    phase_elapsed_s = round((datetime.now(timezone.utc) - started).total_seconds(), 1)
                except Exception:
                    pass
            # Last tool activity
            try:
                ap = activity_path(run.run_id)
                if ap.exists():
                    data = ap.read_text(encoding="utf-8")
                    import json as _json
                    ad = _json.loads(data)
                    last_tool = ad.get("tool", "")
                    last_tool_age_s = round(_time.time() - float(ad.get("ts", _time.time())), 1)
            except Exception:
                pass

        return JSONResponse({
            "project": project,
            "api_spend_today": api_today,
            "api_spend_all": api_today_all,
            "api_spend_gate_usd": api_gate,
            "phase_elapsed_s": phase_elapsed_s,
            "last_tool": last_tool,
            "last_tool_age_s": last_tool_age_s,
            "quota": {
                "plan": plan,
                "msgs_used": window["msgs_used"],
                "msg_cap": msg_cap,
                "tokens_in": window["tokens_in"],
                "tokens_out": window["tokens_out"],
                "window_start": window["window_start"],
            },
            "active_run": active,
        })

    @app.get("/stats")
    async def stats():
        return JSONResponse({"projects": get_project_stats()})

    @app.get("/runs")
    async def runs(limit: int = 20, project: str = None):
        return JSONResponse(get_recent_runs(project, limit=limit))

    @app.get("/events")
    async def events_list(limit: int = 20, run_id: str = None):
        evts = get_latest_events(limit=limit, run_id=run_id or None)
        return JSONResponse(evts)

    @app.get("/events/{run_id}")
    async def events(run_id: str):
        from fastapi.responses import HTMLResponse
        evts = get_run_events(run_id)
        if not evts:
            html = f"""<!DOCTYPE html><html><head><meta charset="UTF-8">
<title>Events: {run_id}</title>
<style>body{{font-family:monospace;background:#0d1117;color:#8b949e;padding:2rem}}
h1{{font-size:1rem;color:#58a6ff;margin-bottom:1rem}}</style></head>
<body><h1>Event timeline: {run_id}</h1><p>No events recorded for this run.</p></body></html>"""
            return HTMLResponse(html)
        # Compute relative timestamps from first event
        from datetime import datetime, timezone
        try:
            t0 = datetime.fromisoformat(evts[0]["created_at"]).replace(tzinfo=timezone.utc)
        except Exception:
            t0 = datetime.now(timezone.utc)
        rows = ""
        for e in evts:
            try:
                ts = datetime.fromisoformat(e["created_at"]).replace(tzinfo=timezone.utc)
                rel = (ts - t0).total_seconds()
                m = int(rel // 60); s = int(rel % 60)
                time_str = f"{m:02d}:{s:02d}"
            except Exception:
                time_str = "??:??"
            blocked_badge = '<span style="color:#f85149">blocked</span>' if e.get("blocked") else ""
            reason = e.get("block_reason", "") or ""
            reason_html = f'<span style="color:#8b949e"> &mdash; {reason[:80]}</span>' if reason else ""
            meta = e.get("metadata") or {}
            meta_str = "  ".join(f"{k}={v}" for k, v in meta.items()) if meta else ""
            meta_html = f'<span style="color:#8b949e;font-size:0.75rem"> {meta_str}</span>' if meta_str else ""
            rows += (
                f'<tr><td style="color:#8b949e;font-family:monospace">{time_str}</td>'
                f'<td><span style="color:#58a6ff">{e.get("phase","")}</span></td>'
                f'<td>{e.get("event_type","")}</td>'
                f'<td>{e.get("tool_name","") or ""}</td>'
                f'<td>{blocked_badge}{reason_html}{meta_html}</td></tr>\n'
            )
        html = f"""<!DOCTYPE html><html><head><meta charset="UTF-8">
<title>Events: {run_id}</title>
<style>body{{font-family:monospace;background:#0d1117;color:#e6edf3;padding:2rem}}
h1{{font-size:1rem;color:#58a6ff;margin-bottom:1rem}}
table{{border-collapse:collapse;width:100%;font-size:0.8rem}}
th{{text-align:left;color:#8b949e;padding:0.4rem 0.75rem;border-bottom:1px solid #21262d}}
td{{padding:0.3rem 0.75rem;border-bottom:1px solid #161b22}}
tr:hover td{{background:#161b22}}</style></head>
<body><h1>Event timeline: {run_id}</h1>
<table><thead><tr><th>Time</th><th>Phase</th><th>Event</th><th>Tool</th><th>Detail</th></tr></thead>
<tbody>{rows}</tbody></table></body></html>"""
        return HTMLResponse(html)

    @app.post("/stop")
    async def stop_session():
        project = get_project_id()
        run = load_active_run(project)
        if not run:
            return Response(status_code=404)
        sentinel = DB_PATH.parent / f"stop_{run.run_id}"
        sentinel.touch()
        return JSONResponse({"ok": True, "run_id": run.run_id})

    @app.post("/runs/{run_id}/complete")
    async def run_complete(run_id: str):
        ok = set_run_status(run_id, RunStatus.complete)
        return JSONResponse({"ok": ok, "run_id": run_id, "status": "complete"})

    @app.post("/runs/{run_id}/cancel")
    async def run_cancel(run_id: str):
        ok = set_run_status(run_id, RunStatus.cancelled)
        return JSONResponse({"ok": ok, "run_id": run_id, "status": "cancelled"})

    @app.post("/runs/{run_id}/retry")
    async def run_retry(run_id: str):
        ok = retry_run(run_id)
        return JSONResponse({"ok": ok, "run_id": run_id, "status": "active"})

    @app.delete("/runs/{run_id}")
    async def run_delete(run_id: str):
        ok = delete_run(run_id)
        return JSONResponse({"ok": ok, "run_id": run_id})

    console.print(f"[bold cyan]AI Flow dashboard[/bold cyan] → http://localhost:{port}")
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")
