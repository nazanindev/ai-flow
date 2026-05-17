"""flow serve — local FastAPI dashboard on :7331."""
import os

from rich.console import Console

console = Console()

_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AI Flow</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", monospace; background: #0d1117; color: #e6edf3; padding: 2rem; }
  h1 { font-size: 1.25rem; color: #58a6ff; margin-bottom: 1.5rem; letter-spacing: 0.05em; }
  h2 { font-size: 0.8rem; text-transform: uppercase; letter-spacing: 0.1em; color: #8b949e; margin: 1.5rem 0 0.75rem; }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 1rem; margin-bottom: 1.5rem; }
  .card { background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 1.25rem; }
  .card .label { font-size: 0.7rem; color: #8b949e; text-transform: uppercase; letter-spacing: 0.08em; margin-bottom: 0.4rem; }
  .card .value { font-size: 1.5rem; font-weight: 600; color: #e6edf3; }
  .card .sub { font-size: 0.75rem; color: #8b949e; margin-top: 0.25rem; }
  .bar-wrap { background: #21262d; border-radius: 4px; height: 6px; margin-top: 0.5rem; overflow: hidden; }
  .bar { height: 100%; border-radius: 4px; background: #238636; transition: width 0.3s; }
  .bar.warn { background: #d29922; }
  .bar.danger { background: #da3633; }
  table { width: 100%; border-collapse: collapse; font-size: 0.8rem; }
  th { text-align: left; color: #8b949e; font-weight: 500; padding: 0.5rem 0.75rem; border-bottom: 1px solid #21262d; }
  td { padding: 0.5rem 0.75rem; border-bottom: 1px solid #161b22; color: #c9d1d9; }
  tr:hover td { background: #161b22; }
  .badge { display: inline-block; padding: 0.15rem 0.5rem; border-radius: 12px; font-size: 0.7rem; font-weight: 500; }
  .badge-active { background: #0d4429; color: #3fb950; }
  .badge-complete { background: #0c2d6b; color: #58a6ff; }
  .badge-failed { background: #4b0000; color: #f85149; }
  .badge-blocked { background: #3d2b00; color: #d29922; }
  .phase { font-size: 0.7rem; color: #58a6ff; text-transform: uppercase; }
  #refresh { font-size: 0.7rem; color: #8b949e; float: right; cursor: pointer; background: none; border: none; color: #8b949e; }
  #refresh:hover { color: #58a6ff; }
  .btn-stop { background: #da3633; color: #fff; border: none; border-radius: 6px; padding: 0.3rem 0.8rem; font-size: 0.75rem; cursor: pointer; margin-top: 0.75rem; }
  .btn-stop:hover { background: #b62324; }
  .section-label { font-size: 0.65rem; color: #58a6ff; text-transform: uppercase; letter-spacing: 0.1em; margin-bottom: 0.3rem; }
</style>
</head>
<body>
<h1>⚡ AI Flow <button id="refresh" onclick="load()">↻ refresh</button><span id="auto" style="font-size:0.65rem;color:#8b949e;margin-left:0.5rem">auto 5s</span></h1>
<div id="app"><p style="color:#8b949e">Loading...</p></div>
<script>
async function stopSession() {
  const r = await fetch('/stop', {method: 'POST'});
  if (r.ok) load();
}
async function load() {
  const [status, stats, runs] = await Promise.all([
    fetch('/status').then(r=>r.json()),
    fetch('/stats').then(r=>r.json()),
    fetch('/runs').then(r=>r.json()),
  ]);

  const apiGate = status.api_spend_gate_usd || 1.0;
  const apiPct = Math.min((status.api_spend_today / apiGate) * 100, 100);
  const apiBarClass = apiPct > 80 ? 'danger' : apiPct > 50 ? 'warn' : '';

  const quota = status.quota || {};
  const msgCap = quota.msg_cap || 0;
  const msgsUsed = quota.msgs_used || 0;
  const quotaPct = msgCap > 0 ? Math.min((msgsUsed / msgCap) * 100, 100) : 0;
  const quotaBarClass = quotaPct > 80 ? 'danger' : quotaPct > 50 ? 'warn' : '';

  function fmtElapsed(s) {
    if (!s && s !== 0) return '';
    const m = Math.floor(s / 60), sec = Math.floor(s % 60);
    return m > 0 ? `${m}m ${sec}s` : `${sec}s`;
  }
  function activityLabel(status) {
    const lt = status.last_tool;
    const age = status.last_tool_age_s;
    if (!lt) return '<span style="color:#8b949e">waiting…</span>';
    if (age < 15) return `<span style="color:#3fb950">⚡ ${lt} (${Math.floor(age)}s ago)</span>`;
    if (age < 90) return `<span style="color:#d29922">💭 thinking… (${Math.floor(age)}s)</span>`;
    return `<span style="color:#8b949e">⏸ idle (${fmtElapsed(age)})</span>`;
  }

  let activeHtml = '';
  if (status.active_run) {
    const r = status.active_run;
    const runPct = r.max_steps > 0 ? Math.min((r.current_step / r.max_steps) * 100, 100) : 0;
    const projected = r.current_step > 0 ? (r.cost_usd / r.current_step * r.max_steps).toFixed(4) : '—';
    const phaseElapsed = status.phase_elapsed_s != null ? ` · phase ${fmtElapsed(status.phase_elapsed_s)}` : '';
    activeHtml = `
      <h2>Active run</h2>
      <div class="card" style="grid-column:1/-1">
        <div class="label">${r.run_id} &middot; <span class="phase">${r.phase}</span>${phaseElapsed}</div>
        <div class="value" style="font-size:1rem;margin-top:0.25rem">${r.goal.substring(0,100)}</div>
        <div class="sub">Step ${r.current_step}/${r.max_steps} &middot; API: $${r.cost_usd.toFixed(4)} &middot; ~$${projected} projected</div>
        <div class="sub">Subscription: ${r.subscription_msgs} msgs &middot; ${((r.subscription_tokens_in||0)+(r.subscription_tokens_out||0)).toLocaleString()} tokens</div>
        <div class="sub" style="margin-top:0.4rem">${activityLabel(status)}</div>
        <div class="bar-wrap"><div class="bar ${runPct>80?'warn':''}" style="width:${runPct}%"></div></div>
        <div style="margin-top:0.75rem"><a href="/events/${r.run_id}" style="color:#58a6ff;font-size:0.75rem" target="_blank">↗ event timeline</a>
        &nbsp; <button class="btn-stop" onclick="stopSession()">⏹ Stop</button></div>
      </div>`;
  }

  const projectRows = (stats.projects||[]).map(p => `
    <tr>
      <td>${p.project}</td>
      <td>${p.sessions}</td>
      <td>$${(p.api_spend||0).toFixed(4)}</td>
      <td>${((p.sub_tokens||0)).toLocaleString()}</td>
      <td>${(p.last_active||'').substring(0,10)}</td>
    </tr>`).join('');

  const runRows = (runs||[]).map(r => {
    const badge = `badge-${r.status}`;
    return `<tr>
      <td style="font-family:monospace;font-size:0.75rem">${r.run_id}</td>
      <td>${r.goal.substring(0,50)}</td>
      <td><span class="phase">${r.phase}</span></td>
      <td><span class="badge ${badge}">${r.status}</span></td>
      <td>$${r.cost_usd.toFixed(4)}</td>
      <td>${r.subscription_msgs||0}</td>
      <td>${(r.updated_at||'').substring(0,10)}</td>
    </tr>`;}).join('');

  const quotaCard = msgCap > 0 ? `
    <div class="card">
      <div class="section-label">Subscription</div>
      <div class="label">5h window quota (${quota.plan||'pro'})</div>
      <div class="value">${msgsUsed}<span style="font-size:1rem;color:#8b949e">/${msgCap}</span></div>
      <div class="sub">msgs used &middot; ${quotaPct.toFixed(0)}% of window</div>
      <div class="bar-wrap"><div class="bar ${quotaBarClass}" style="width:${quotaPct}%"></div></div>
    </div>` : `
    <div class="card">
      <div class="section-label">Subscription</div>
      <div class="label">Quota</div>
      <div class="value" style="font-size:1rem;margin-top:0.3rem">${quota.plan||'api_only'}</div>
      <div class="sub">No window cap to track</div>
    </div>`;

  document.getElementById('app').innerHTML = `
    <div class="grid">
      ${quotaCard}
      <div class="card">
        <div class="section-label">API utility calls</div>
        <div class="label">Spend today (this project)</div>
        <div class="value">$${status.api_spend_today.toFixed(4)}</div>
        <div class="sub">${apiPct.toFixed(0)}% of $${apiGate} gate</div>
        <div class="bar-wrap"><div class="bar ${apiBarClass}" style="width:${apiPct}%"></div></div>
      </div>
      <div class="card">
        <div class="section-label">API utility calls</div>
        <div class="label">Spend today (all projects)</div>
        <div class="value">$${status.api_spend_all.toFixed(4)}</div>
        <div class="sub">clarify + ship + ci-review</div>
      </div>
      <div class="card">
        <div class="label">Active project</div>
        <div class="value" style="font-size:1rem;margin-top:0.3rem">${status.project}</div>
      </div>
    </div>
    ${activeHtml}
    <h2>By project</h2>
    <table>
      <thead><tr><th>Project</th><th>Sessions</th><th>API spend</th><th>Sub tokens</th><th>Last active</th></tr></thead>
      <tbody>${projectRows || '<tr><td colspan="5" style="color:#8b949e">No data yet</td></tr>'}</tbody>
    </table>
    <h2>Recent runs</h2>
    <table>
      <thead><tr><th>ID</th><th>Goal</th><th>Phase</th><th>Status</th><th>API spend</th><th>Sub msgs</th><th>Updated</th></tr></thead>
      <tbody>${runRows || '<tr><td colspan="7" style="color:#8b949e">No runs yet</td></tr>'}</tbody>
    </table>`;
}
load();
setInterval(load, 5000);
</script>
</body>
</html>"""


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
        load_active_run, get_window_usage, get_run_events, activity_path,
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
        api_gate = float(os.getenv("AP_BUDGET_USD") or c.get("api_spend_gate_usd", 1.0))
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

    @app.get("/events/{run_id}")
    async def events(run_id: str):
        from fastapi.responses import HTMLResponse
        evts = get_run_events(run_id)
        if not evts:
            return JSONResponse([])
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

    console.print(f"[bold cyan]AI Flow dashboard[/bold cyan] → http://localhost:{port}")
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")
