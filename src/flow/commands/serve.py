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
:root {
  --bg:#0d1117; --surface:#161b22; --surface2:#21262d; --border:#30363d;
  --text:#e6edf3; --muted:#8b949e; --dim:#6e7681;
  --accent:#58a6ff; --green:#3fb950; --yellow:#d29922; --red:#f85149; --purple:#bc8cff;
}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:var(--bg);color:var(--text);min-height:100vh}
a{color:var(--accent);text-decoration:none}
a:hover{text-decoration:underline}

/* Topbar */
.topbar{display:flex;align-items:center;gap:.75rem;padding:.6rem 1.5rem;border-bottom:1px solid var(--border);position:sticky;top:0;background:var(--bg);z-index:10}
.topbar-title{font-size:1rem;font-weight:700;color:var(--accent)}
.proj-pill{font-size:.75rem;color:var(--muted);background:var(--surface2);padding:.15rem .5rem;border-radius:4px}
.topbar-right{margin-left:auto;display:flex;align-items:center;gap:.75rem;font-size:.72rem;color:var(--dim)}
.topbar-right button{background:none;border:none;color:var(--muted);cursor:pointer;font-size:.72rem;padding:.15rem .35rem;border-radius:3px}
.topbar-right button:hover{background:var(--surface2);color:var(--accent)}

/* Body */
.body{padding:1rem 1.5rem}

/* Two-column layout */
.layout{display:grid;grid-template-columns:1fr 276px;gap:1rem;margin-bottom:1rem;align-items:start}
@media(max-width:860px){.layout{grid-template-columns:1fr}}

/* Generic card */
.card{background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:1rem}
.card-lit{border-color:#388bfd55}
.section-card{background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:1rem;margin-bottom:1rem}
.sh{font-size:.62rem;font-weight:700;text-transform:uppercase;letter-spacing:.1em;color:var(--accent);margin-bottom:.7rem}

/* Text helpers */
.mono{font-family:"SF Mono","Cascadia Code",monospace}
.muted{color:var(--muted)}
.dim{color:var(--dim)}

/* Phase chips */
.ph{font-size:.6rem;font-weight:700;text-transform:uppercase;letter-spacing:.06em;padding:.15rem .45rem;border-radius:3px}
.ph-plan{background:#0c2d6b;color:#79c0ff}
.ph-execute{background:#0d4429;color:#3fb950}
.ph-verify{background:#3d2b00;color:#d29922}
.ph-ship{background:#1e0f3d;color:#bc8cff}

/* Active run */
.run-meta{display:flex;align-items:center;gap:.5rem;flex-wrap:wrap;font-size:.72rem;margin-bottom:.5rem}
.run-goal{font-size:.95rem;font-weight:600;line-height:1.4;margin-bottom:.75rem}

/* Plan steps */
.steps{margin:.5rem 0 .75rem}
.step{display:flex;align-items:baseline;gap:.4rem;padding:.18rem 0;font-size:.78rem;line-height:1.35}
.si{min-width:12px;flex-shrink:0;font-size:.7rem}
.s-done{color:var(--muted)}.s-done .si{color:var(--green)}
.s-cur{color:var(--text);font-weight:600}.s-cur .si{color:var(--accent)}
.s-pend{color:var(--dim)}
.steps-more{font-size:.7rem;color:var(--dim);padding-left:1rem;margin-top:.2rem}

/* Progress bar */
.prog-row{display:flex;justify-content:space-between;font-size:.7rem;color:var(--muted);margin-bottom:.3rem}
.bw{background:var(--surface2);border-radius:3px;height:4px;overflow:hidden;margin:.4rem 0}
.b{height:100%;border-radius:3px;background:var(--green);transition:width .5s ease}
.b.warn{background:var(--yellow)}.b.danger{background:var(--red)}

/* Run actions */
.run-actions{display:flex;align-items:center;gap:.75rem;margin-top:.75rem;padding-top:.7rem;border-top:1px solid var(--surface2)}
.btn{border:none;border-radius:5px;padding:.3rem .75rem;font-size:.72rem;cursor:pointer;font-weight:500}
.btn-stop{background:#da3633;color:#fff}.btn-stop:hover{background:#b62324}

/* Idle state */
.idle-card{text-align:center;padding:2rem 1rem;color:var(--dim);font-size:.85rem}

/* Sidebar */
.sidebar{display:flex;flex-direction:column;gap:1rem}
.res-lbl{font-size:.68rem;color:var(--muted);margin-bottom:.15rem}
.res-val{font-size:1.1rem;font-weight:700}
.res-sub{font-size:.68rem;color:var(--muted);margin:.1rem 0}

/* By project */
.proj-row{display:flex;align-items:center;justify-content:space-between;padding:.3rem 0;border-bottom:1px solid var(--surface2)}
.proj-row:last-child{border-bottom:none}
.proj-nm{font-size:.78rem;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:145px}
.proj-st{font-size:.68rem;color:var(--muted);text-align:right;flex-shrink:0;line-height:1.5}

/* Events feed */
.ev-row{display:flex;align-items:baseline;gap:.6rem;padding:.23rem 0;border-bottom:1px solid var(--surface2);font-size:.73rem}
.ev-row:last-child{border-bottom:none}
.ev-age{color:var(--dim);min-width:38px;flex-shrink:0;font-family:monospace;font-size:.68rem}
.ev-run{color:var(--dim);min-width:58px;flex-shrink:0;font-family:monospace;font-size:.68rem}
.ev-ph{min-width:54px;flex-shrink:0}
.ev-type{min-width:120px;flex-shrink:0}
.ev-det{color:var(--muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;min-width:0}

/* Runs table */
.rt{width:100%;border-collapse:collapse;font-size:.77rem}
.rt th{text-align:left;color:var(--muted);font-weight:500;padding:.4rem .5rem;border-bottom:1px solid var(--surface2);white-space:nowrap}
.rt td{padding:.38rem .5rem;border-bottom:1px solid var(--bg);vertical-align:middle}
.rt tr:hover td{background:var(--surface2)}
.badge{display:inline-block;padding:.1rem .45rem;border-radius:10px;font-size:.62rem;font-weight:700}
.badge-active{background:#0d4429;color:#3fb950}
.badge-complete{background:#0c2d6b;color:#58a6ff}
.badge-failed{background:#4b0000;color:#f85149}
.badge-blocked{background:#3d2b00;color:#d29922}
.badge-cancelled{background:#21262d;color:#8b949e}
.blk{color:var(--yellow)}

/* CRUD action buttons in runs table */
.act-btn{border:none;border-radius:3px;padding:.15rem .4rem;font-size:.65rem;cursor:pointer;font-weight:500;line-height:1.4}
.act-done{background:#0d4429;color:#3fb950}.act-done:hover{background:#1a6035}
.act-cancel{background:#3d2b00;color:#d29922}.act-cancel:hover{background:#5a3e00}
.act-retry{background:#1e0f3d;color:#bc8cff}.act-retry:hover{background:#2d1a5a}
.act-del{background:#4b0000;color:#f85149}.act-del:hover{background:#6b0000}
</style>
</head>
<body>
<div class="topbar">
  <span class="topbar-title">⚡ AI Flow</span>
  <span class="proj-pill" id="proj-name">—</span>
  <div class="topbar-right">
    <span id="ts" class="dim"></span>
    <button onclick="load()">↻ refresh</button>
    <span>auto 5s</span>
  </div>
</div>
<div class="body">
  <div id="app"><p class="muted" style="padding:2rem">Loading…</p></div>
</div>
<script>
function phCls(p){return{plan:'ph-plan',execute:'ph-execute',verify:'ph-verify',ship:'ph-ship'}[p]||''}

function fmtDur(s){
  if(!s&&s!==0)return'—';
  s=Math.max(0,Math.floor(s));
  const h=Math.floor(s/3600),m=Math.floor((s%3600)/60),sec=s%60;
  if(h>0)return h+'h '+m+'m';
  if(m>0)return m+'m '+sec+'s';
  return sec+'s';
}
function fmtTok(n){
  n=n||0;
  if(n>=1e6)return(n/1e6).toFixed(1)+'M';
  if(n>=1000)return Math.round(n/1000)+'k';
  return String(n);
}
function relAge(iso){
  const s=Math.floor((Date.now()-new Date(iso))/1000);
  if(s<60)return s+'s';
  if(s<3600)return Math.floor(s/60)+'m';
  if(s<86400)return Math.floor(s/3600)+'h';
  const d=Math.floor(s/86400);
  return d+'d';
}

function actHtml(st){
  const lt=st.last_tool,age=st.last_tool_age_s;
  if(!lt)return'<span class="dim">waiting…</span>';
  if(age<15)return`<span style="color:var(--green)">⚡ ${lt} &middot; ${Math.floor(age)}s ago</span>`;
  if(age<90)return`<span style="color:var(--yellow)">💭 thinking… &middot; ${Math.floor(age)}s</span>`;
  return`<span class="dim">⏸ idle &middot; ${fmtDur(age)}</span>`;
}

function renderRun(r,status){
  if(!r) return`<div class="card idle-card"><p>No active run</p><p class="dim" style="margin-top:.3rem;font-size:.8rem">Type a task in the TUI to start</p></div>`;

  const pct=r.max_steps>0?Math.min(r.current_step/r.max_steps*100,100):0;
  const proj=r.current_step>0?'$'+(r.cost_usd/r.current_step*r.max_steps).toFixed(4):'—';
  const bCls=pct>80?'b warn':'b';
  const phEl=status.phase_elapsed_s!=null?' &middot; '+fmtDur(status.phase_elapsed_s):'';

  const steps=r.plan_steps||[];
  let stepsHtml='';
  if(steps.length>0){
    const fp=steps.findIndex(s=>s.status!=='done');
    stepsHtml='<div class="steps">';
    steps.slice(0,8).forEach((s,i)=>{
      const done=s.status==='done',cur=!done&&i===fp;
      const cls=done?'s-done':cur?'s-cur':'s-pend';
      const icon=done?'✓':cur?'●':'○';
      stepsHtml+=`<div class="step ${cls}"><span class="si">${icon}</span><span>${s.description}</span></div>`;
    });
    const rest=steps.length-Math.min(steps.length,8);
    if(rest>0)stepsHtml+=`<div class="steps-more">…and ${rest} more</div>`;
    stepsHtml+='</div>';
  }

  const tok=fmtTok((r.subscription_tokens_in||0)+(r.subscription_tokens_out||0));
  return`<div class="card card-lit">
    <div class="run-meta">
      <span class="ph ${phCls(r.phase)}">${r.phase}</span>
      <span class="dim mono">${r.run_id}</span>
      <span class="dim">${phEl}</span>
      <span style="margin-left:auto">${actHtml(status)}</span>
    </div>
    <div class="run-goal">${r.goal}</div>
    ${stepsHtml}
    <div class="prog-row">
      <span>step ${r.current_step} / ${r.max_steps}</span>
      <span>API $${r.cost_usd.toFixed(4)} &middot; est. ${proj}</span>
    </div>
    <div class="bw"><div class="${bCls}" style="width:${pct}%"></div></div>
    <div class="muted" style="font-size:.7rem;margin-top:.35rem">${r.subscription_msgs} msgs &middot; ${tok} tok (subscription)</div>
    <div class="run-actions">
      <a href="/events/${r.run_id}" target="_blank">↗ event timeline</a>
      <button class="btn btn-stop" onclick="stopSession()">⏹ stop</button>
    </div>
  </div>`;
}

function renderSidebar(status,stats){
  const q=status.quota||{},cap=q.msg_cap||0,used=q.msgs_used||0;
  const qPct=cap>0?Math.min(used/cap*100,100):0;
  const gate=status.api_spend_gate_usd||1.0;
  const aPct=Math.min(status.api_spend_today/gate*100,100);
  const qBar=qPct>80?'b danger':qPct>50?'b warn':'b';
  const aBar=aPct>80?'b danger':aPct>50?'b warn':'b';

  const projs=[...(stats.projects||[])].sort((a,b)=>(b.sub_tokens||0)-(a.sub_tokens||0)).slice(0,12);
  const projHtml=projs.map(p=>{
    const tok=p.sub_tokens||0, sess=p.sessions||1;
    const perSess=tok>0?` &middot; ${fmtTok(Math.round(tok/sess))}/sess`:'';
    return`<div class="proj-row">
      <span class="proj-nm" title="${p.project}">${p.project}</span>
      <span class="proj-st">${fmtTok(tok)} tok${perSess}<br><span class="dim">${p.sessions} sess &middot; ${relAge(p.last_active)} ago</span></span>
    </div>`;
  }).join('')||'<span class="dim" style="font-size:.75rem">No data yet</span>';

  return`<div class="sidebar">
    <div class="card">
      <div class="sh">Resources</div>
      ${cap>0?`
        <div class="res-lbl">Subscription (5h window &middot; ${q.plan||'pro'})</div>
        <div class="res-val">${used}<span class="muted" style="font-size:.85rem"> / ${cap} msgs</span></div>
        <div class="res-sub">${qPct.toFixed(0)}% of window</div>
        <div class="bw"><div class="${qBar}" style="width:${qPct}%"></div></div>
      `:`<div class="res-sub" style="margin-bottom:.75rem">No subscription cap configured</div>`}
      <div class="res-lbl" style="margin-top:.6rem">API utility spend today</div>
      <div class="res-val">$${status.api_spend_today.toFixed(4)}</div>
      <div class="res-sub">${aPct.toFixed(0)}% of $${gate} gate &middot; all projects: $${status.api_spend_all.toFixed(4)}</div>
      <div class="bw"><div class="${aBar}" style="width:${aPct}%"></div></div>
    </div>
    <div class="card">
      <div class="sh">By project (subscription tokens)</div>
      ${projHtml}
    </div>
  </div>`;
}

function renderEvents(events){
  if(!events||!events.length)return'';
  const rows=events.map(e=>{
    const meta=e.metadata||{};
    let typeColor='var(--text)',detail='';
    if(e.event_type==='phase_transition'){
      typeColor='var(--green)';
      const dur=meta.duration_s>0?' ('+fmtDur(meta.duration_s)+' in '+meta.from_phase+')':'';
      detail=`→ ${meta.to_phase}${dur}`;
    } else if(e.blocked){
      typeColor='var(--red)';
      detail=(e.block_reason||'').substring(0,80);
    } else if(e.event_type==='session_end'){
      typeColor='var(--accent)';
      const tot=(meta.tokens_in||0)+(meta.tokens_out||0);
      detail=`tok=${fmtTok(tot)}`;
    } else if(e.event_type==='verify_result'){
      typeColor=meta.passed?'var(--green)':'var(--red)';
      detail=meta.passed?'passed':'failed';
    } else if(e.event_type==='check_result'){
      typeColor=meta.overall==='A'?'var(--green)':'var(--yellow)';
      detail=`overall=${meta.overall} &middot; blocks=${meta.blocker_count||0}`;
    }
    return`<div class="ev-row">
      <span class="ev-age">${relAge(e.created_at)}</span>
      <span class="ev-run mono">${(e.run_id||'').substring(0,7)}</span>
      <span class="ev-ph"><span class="ph ${phCls(e.phase)}">${e.phase||''}</span></span>
      <span class="ev-type" style="color:${typeColor}">${e.event_type}</span>
      <span class="ev-det">${detail}</span>
    </div>`;
  }).join('');
  return`<div class="section-card">
    <div class="sh">Live events <span class="dim" style="text-transform:none;font-weight:400;font-size:.65rem">last ${events.length} &middot; auto-refreshing</span></div>
    ${rows}
  </div>`;
}

function renderRuns(runs){
  const now=Date.now();
  const rows=runs.map(r=>{
    const isActive=r.status==='active';
    const t0=r.created_at?new Date(r.created_at):null;
    const t1=isActive?new Date():r.updated_at?new Date(r.updated_at):null;
    const dur=t0&&t1?fmtDur((t1-t0)/1000):'—';
    const tok=(r.subscription_tokens_in||0)+(r.subscription_tokens_out||0);
    const blocks=r.block_count||0;
    let acts='<span class="dim">—</span>';
    if(r.status==='active'){
      acts=`<button class="act-btn act-done" onclick="markDone('${r.run_id}')">✓ done</button> `+
           `<button class="act-btn act-cancel" onclick="cancelRun('${r.run_id}')">⊘ cancel</button>`;
    } else if(r.status==='blocked'){
      acts=`<button class="act-btn act-retry" onclick="retryRun('${r.run_id}')">↺ retry</button> `+
           `<button class="act-btn act-done" onclick="markDone('${r.run_id}')">✓ done</button> `+
           `<button class="act-btn act-cancel" onclick="cancelRun('${r.run_id}')">⊘ cancel</button>`;
    } else if(r.status==='complete'||r.status==='failed'||r.status==='cancelled'){
      acts=`<button class="act-btn act-del" onclick="deleteRun('${r.run_id}')">🗑 delete</button>`;
    }
    return`<tr>
      <td><a class="mono" href="/events/${r.run_id}" target="_blank" title="view events">${r.run_id}</a></td>
      <td style="max-width:220px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${r.goal}">${r.goal}</td>
      <td><span class="badge badge-${r.status}">${r.status}</span></td>
      <td><span class="ph ${phCls(r.phase)}">${r.phase}</span></td>
      <td class="muted">${dur}</td>
      <td class="muted mono">$${r.cost_usd.toFixed(4)}</td>
      <td class="muted">${fmtTok(tok)}</td>
      <td class="${blocks>0?'blk':'dim'}">${blocks>0?'⚠ '+blocks:'—'}</td>
      <td style="white-space:nowrap">${acts}</td>
    </tr>`;
  }).join('');
  return`<div class="section-card">
    <div class="sh">Recent runs</div>
    <table class="rt">
      <thead><tr><th>Run ID</th><th>Goal</th><th>Status</th><th>Phase</th><th>Duration</th><th>API cost</th><th>Sub tokens</th><th>Blocks</th><th>Actions</th></tr></thead>
      <tbody>${rows||'<tr><td colspan="9" class="dim" style="text-align:center;padding:1rem">No runs yet</td></tr>'}</tbody>
    </table>
  </div>`;
}

async function stopSession(){
  const r=await fetch('/stop',{method:'POST'});
  if(r.ok)load();
}
async function markDone(id){
  await fetch('/runs/'+id+'/complete',{method:'POST'});load();
}
async function cancelRun(id){
  await fetch('/runs/'+id+'/cancel',{method:'POST'});load();
}
async function retryRun(id){
  await fetch('/runs/'+id+'/retry',{method:'POST'});load();
}
async function deleteRun(id){
  if(!confirm('Delete run '+id+'? This cannot be undone.'))return;
  await fetch('/runs/'+id,{method:'DELETE'});load();
}

async function load(){
  try{
    const [status,stats,runs,events]=await Promise.all([
      fetch('/status').then(r=>r.json()),
      fetch('/stats').then(r=>r.json()),
      fetch('/runs?limit=20').then(r=>r.json()),
      fetch('/events?limit=18').then(r=>r.json()),
    ]);
    document.getElementById('proj-name').textContent=status.project||'—';
    document.getElementById('ts').textContent=new Date().toLocaleTimeString();
    document.getElementById('app').innerHTML=`
      <div class="layout">
        <div>${renderRun(status.active_run,status)}</div>
        ${renderSidebar(status,stats)}
      </div>
      ${renderEvents(events)}
      ${renderRuns(runs)}
    `;
  }catch(e){
    document.getElementById('app').innerHTML=`<p class="muted" style="padding:1rem">Load error: ${e.message}</p>`;
  }
}
load();
setInterval(load,5000);
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

    @app.get("/events")
    async def events_list(limit: int = 20, run_id: str = None):
        evts = get_latest_events(limit=limit, run_id=run_id or None)
        return JSONResponse(evts)

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
