# ruff: noqa: E501

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse

from app.core.config.settings import get_settings
from app.modules.traffic.store import get_traffic_store

router = APIRouter(prefix="/__traffic", tags=["traffic"])


def _is_localhost(request: Request) -> bool:
    host = request.client.host if request.client is not None else ""
    return host in {"127.0.0.1", "::1", "localhost", "testclient"}


def _has_valid_token(request: Request) -> bool:
    token = get_settings().traffic_dashboard_token
    if not token:
        return False
    authorization = request.headers.get("authorization", "")
    if authorization.lower().startswith("bearer ") and authorization[7:] == token:
        return True
    return request.query_params.get("token") == token


def _require_traffic_access(request: Request) -> None:
    settings = get_settings()
    if not settings.traffic_dashboard_enabled:
        raise HTTPException(status_code=404, detail="Not Found")
    if _is_localhost(request) or _has_valid_token(request):
        return
    raise HTTPException(status_code=403, detail="Traffic dashboard is only available from localhost")


@router.get("", response_class=HTMLResponse, include_in_schema=False)
@router.get("/", response_class=HTMLResponse, include_in_schema=False)
async def dashboard(request: Request) -> HTMLResponse:
    _require_traffic_access(request)
    return HTMLResponse(_dashboard_html())


@router.get("/ping", status_code=204)
async def ping(request: Request) -> Response:
    _require_traffic_access(request)
    return Response(status_code=204)


@router.post("/tunnel-heartbeat")
async def tunnel_heartbeat(request: Request, payload: dict[str, Any] = Body(...)) -> dict[str, bool]:
    _require_traffic_access(request)
    get_traffic_store().record_heartbeat(payload)
    return {"ok": True}


@router.get("/summary")
async def summary(request: Request) -> dict[str, Any]:
    _require_traffic_access(request)
    return get_traffic_store().summary()


@router.get("/requests")
async def requests(request: Request, limit: int = Query(default=200, ge=1, le=2000)) -> dict[str, Any]:
    _require_traffic_access(request)
    return {"requests": get_traffic_store().recent_requests(limit=limit)}


@router.get("/ports")
async def ports(request: Request) -> dict[str, Any]:
    _require_traffic_access(request)
    return {"ports": get_traffic_store().ports()}


def _dashboard_html() -> str:
    return """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>codex-lb Traffic</title>
  <style>
    :root{color-scheme:dark;--bg:#0b0e11;--panel:#141922;--panel2:#10151d;--line:#263142;--text:#e7edf7;--muted:#8b96a8;--ok:#37d67a;--warn:#f3c74d;--bad:#ff6262;--idle:#566173;--tag:#20304a}
    *{box-sizing:border-box} body{margin:0;background:linear-gradient(120deg,#0b0e11,#101827 55%,#0b0e11);color:var(--text);font:12px/1.4 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
    header{position:sticky;top:0;z-index:2;display:flex;align-items:center;gap:16px;padding:12px 16px;border-bottom:1px solid var(--line);background:rgba(11,14,17,.92);backdrop-filter:blur(8px)}
    h1{font-size:15px;margin:0;letter-spacing:.04em}.muted{color:var(--muted)}main{padding:14px 16px 24px;display:grid;gap:14px}.cards{display:grid;grid-template-columns:repeat(8,minmax(120px,1fr));gap:10px}.card,.panel{background:linear-gradient(180deg,var(--panel),var(--panel2));border:1px solid var(--line);border-radius:10px;box-shadow:0 12px 28px rgba(0,0,0,.25)}
    .card{padding:10px 12px}.label{color:var(--muted);font-size:11px;text-transform:uppercase}.value{font-size:20px;margin-top:3px}.panel{overflow:hidden}.panel h2{font-size:13px;margin:0;padding:10px 12px;border-bottom:1px solid var(--line)}.backend{padding:10px 12px;display:grid;grid-template-columns:repeat(6,minmax(140px,1fr));gap:8px}
    table{width:100%;border-collapse:collapse}th,td{padding:7px 8px;border-bottom:1px solid rgba(38,49,66,.65);text-align:left;vertical-align:top}th{color:var(--muted);font-weight:600;background:#111722}.scroll{overflow:auto;max-height:48vh}.tag{display:inline-block;padding:1px 7px;border:1px solid #33435c;border-radius:999px;background:var(--tag);color:#dce8ff}.ok{color:var(--ok)}.warn{color:var(--warn)}.bad{color:var(--bad)}.idle{color:var(--idle)}tr.badrow{background:rgba(255,98,98,.08)}tr.warnrow{background:rgba(243,199,77,.08)}tr.idlerow{opacity:.48}.path{max-width:420px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.right{text-align:right}@media(max-width:1200px){.cards{grid-template-columns:repeat(4,1fr)}.backend{grid-template-columns:repeat(3,1fr)}}@media(max-width:760px){.cards,.backend{grid-template-columns:repeat(2,1fr)}main{padding:10px}}
  </style>
</head>
<body>
  <header><h1>codex-lb traffic</h1><span id="status" class="muted">loading</span></header>
  <main>
    <section class="cards" id="cards"></section>
    <section class="panel"><h2>Local Backend</h2><div class="backend" id="backend"></div></section>
    <section class="panel"><h2>Ports</h2><div class="scroll"><table><thead><tr><th>port</th><th>name</th><th>role</th><th>owner</th><th>enabled</th><th>used</th><th>health</th><th class="right">lat</th><th class="right">streams</th><th class="right">active</th><th class="right">rpm</th><th class="right">p95</th><th class="right">err</th><th class="right">abort</th><th class="right">out/min</th><th>ssh</th><th>watchdog</th><th>last error</th></tr></thead><tbody id="ports"></tbody></table></div></section>
    <section class="panel"><h2>Recent Requests</h2><div class="scroll"><table><thead><tr><th>time</th><th>port</th><th>method/path</th><th>model</th><th>status</th><th class="right">duration</th><th class="right">bytes</th><th>state</th><th>error</th></tr></thead><tbody id="requests"></tbody></table></div></section>
  </main>
<script>
const token = new URLSearchParams(location.search).get('token');
const qs = token ? `?token=${encodeURIComponent(token)}` : '';
const fmt = n => n === null || n === undefined ? '--' : String(n);
const bytes = n => { n=Number(n||0); if(n>1048576)return (n/1048576).toFixed(1)+' MiB'; if(n>1024)return (n/1024).toFixed(1)+' KiB'; return n+' B'; };
const ms = n => n === null || n === undefined ? '--' : Number(n).toFixed(0)+' ms';
const time = s => s ? new Date(s).toLocaleTimeString() : '--';
const cls = p => p.heartbeat_stale ? 'warnrow' : (p.tunnel_healthy === false ? 'badrow' : (p.configured && !p.used ? 'idlerow' : ''));
function card(label,value,klass=''){return `<div class="card"><div class="label">${label}</div><div class="value ${klass}">${value}</div></div>`}
function kv(label,value,klass=''){return `<div><div class="label">${label}</div><div class="${klass}">${value}</div></div>`}
async function load(){
  const [summary, recent] = await Promise.all([
    fetch('/__traffic/summary'+qs).then(r=>r.json()),
    fetch('/__traffic/requests?limit=100'+(token?'&token='+encodeURIComponent(token):'')).then(r=>r.json())
  ]);
  document.getElementById('status').textContent = 'updated '+new Date().toLocaleTimeString();
  document.getElementById('cards').innerHTML = [
    card('active requests', summary.active_requests),
    card('active streams', summary.active_streams),
    card('rpm', summary.requests_per_minute),
    card('p95 latency', ms(summary.latency?.p95)),
    card('errors', summary.total_errors, summary.total_errors?'bad':''),
    card('aborted', summary.total_aborted, summary.total_aborted?'warn':''),
    card('bytes out/min', bytes(summary.bytes_out_per_minute)),
    card('heartbeat age', summary.latest_heartbeat_age_seconds==null?'--':summary.latest_heartbeat_age_seconds.toFixed(0)+' s', summary.heartbeat_stale?'warn':'')
  ].join('');
  const b = summary.local_backend || {};
  document.getElementById('backend').innerHTML = [
    kv('2455 health', b.healthy === true ? 'healthy' : (b.healthy === false ? 'unhealthy' : '--'), b.healthy === true ? 'ok' : (b.healthy === false ? 'bad' : 'muted')),
    kv('latency', ms(b.latency_ms)),
    kv('connections', fmt(b.established_connections)),
    kv('process', [b.process?.pid,b.process?.name].filter(Boolean).join(' ') || '--'),
    kv('cpu seconds', fmt(b.process?.cpu_seconds)),
    kv('working set', b.process?.working_set_mb==null?'--':b.process.working_set_mb+' MB')
  ].join('');
  document.getElementById('ports').innerHTML = (summary.per_port||[]).map(p => `<tr class="${cls(p)}"><td>${p.port}</td><td>${fmt(p.name)}</td><td><span class="tag">${fmt(p.role)}</span></td><td>${fmt(p.owner)}</td><td>${fmt(p.enabled)}</td><td>${fmt(p.used)}</td><td class="${p.heartbeat_stale?'warn':(p.tunnel_healthy===false?'bad':'ok')}">${p.heartbeat_stale?'stale':fmt(p.tunnel_healthy)}</td><td class="right">${ms(p.tunnel_latency_ms)}</td><td class="right">${p.stream_active}</td><td class="right">${p.request_active}</td><td class="right">${p.rpm}</td><td class="right">${ms(p.p95)}</td><td class="right">${p.request_errors}</td><td class="right">${p.request_aborted}</td><td class="right">${bytes(p.bytes_out_per_minute)}</td><td>${fmt(p.ssh_pid)}</td><td>${fmt(p.watchdog_pid)}</td><td>${fmt(p.last_error)}</td></tr>`).join('');
  document.getElementById('requests').innerHTML = (recent.requests||[]).map(r => `<tr><td>${time(r.start_time)}</td><td>${r.inferred_remote_port}</td><td class="path">${r.method} ${r.path}</td><td>${fmt(r.model)}</td><td>${fmt(r.status_code)}</td><td class="right">${ms(r.duration_ms)}</td><td class="right">${bytes(r.request_bytes)} / ${bytes(r.response_bytes)}</td><td>${r.state}</td><td>${fmt(r.error_message||r.error_type)}</td></tr>`).join('');
}
load().catch(e=>{document.getElementById('status').textContent='error '+e});
setInterval(()=>load().catch(e=>{document.getElementById('status').textContent='error '+e}),5000);
</script>
</body>
</html>"""


__all__ = ["router"]
