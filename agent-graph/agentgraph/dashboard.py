"""Live dashboard: every fork, its probabilities, the route taken and the result. Server-sent events, read-only."""

from __future__ import annotations

import hmac
import json
import os
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .report import daily

PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Agent Graph</title>
<style>
:root{--bg:#f7f7f5;--fg:#1d1d1b;--mut:#6b6b66;--card:#fff;--line:#e3e3de;--ok:#1f7a4d;--warn:#a15c00;--bad:#b42318;--bar:#3b6fd8}
@media (prefers-color-scheme:dark){:root{--bg:#141413;--fg:#ecebe6;--mut:#9a9993;--card:#1e1e1c;--line:#33332f;--ok:#4cc38a;--warn:#e0a24a;--bad:#f07167;--bar:#7aa2ff}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,sans-serif}
main{max-width:1200px;margin:0 auto;padding:16px}h1{font-size:18px;margin:0 0 12px}h2{font-size:14px;margin:20px 0 8px;color:var(--mut);text-transform:uppercase;letter-spacing:.04em}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:8px}
.k{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:10px}.k b{display:block;font-size:20px}.k span{color:var(--mut);font-size:12px}
.wrap{overflow-x:auto;background:var(--card);border:1px solid var(--line);border-radius:8px}
table{border-collapse:collapse;width:100%;min-width:720px}th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);vertical-align:top}
th{font-size:12px;color:var(--mut);font-weight:600}tr.new{animation:f 1.5s}@keyframes f{from{background:rgba(59,111,216,.18)}}
.p{display:flex;align-items:center;gap:6px;font-variant-numeric:tabular-nums;font-size:12px}.p i{display:inline-block;height:6px;background:var(--bar);border-radius:3px}
.tag{font-size:11px;padding:1px 6px;border-radius:10px;border:1px solid var(--line)}
.shard{color:var(--ok)}.split{color:var(--warn)}.timeout{color:var(--bad)}.ok{color:var(--ok)}.miss{color:var(--bad)}
#live{font-size:12px;color:var(--mut)}
</style></head><body><main>
<h1>Agent Graph <span id="live">connecting</span></h1>
<div class="kpis" id="kpis"></div>
<h2>Forks</h2><div class="wrap"><table><thead><tr><th>time</th><th>job</th><th>node</th><th>question</th><th>probabilities</th><th>answer</th><th>route</th><th>latency</th><th>result</th></tr></thead><tbody id="forks"></tbody></table></div>
<h2>Edges</h2><div class="wrap"><table><thead><tr><th>time</th><th>job</th><th>edge</th><th>type</th><th>summary</th></tr></thead><tbody id="edges"></tbody></table></div>
<h2>Gates</h2><div class="wrap"><table><thead><tr><th>id</th><th>job</th><th>action</th><th>status</th><th>by</th></tr></thead><tbody id="gates"></tbody></table></div>
<h2>Alerts</h2><div class="wrap"><table><thead><tr><th>time</th><th>job</th><th>level</th><th>text</th></tr></thead><tbody id="events"></tbody></table></div>
</main><script>
const tok=new URLSearchParams(location.search).get('token')||'';
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const t=ts=>new Date(ts*1000).toLocaleTimeString();
function probs(p){const e=Object.entries(p||{}).sort((a,b)=>b[1]-a[1]).slice(0,4);
 return e.map(([k,v])=>`<div class="p"><i style="width:${Math.round(v*60)}px"></i>${esc(k)} ${v.toFixed(2)}</div>`).join('')||'<span class="timeout">none</span>'}
function add(id,html,max=200,rid){const tb=document.getElementById(id);const tr=document.createElement('tr');tr.className='new';if(rid)tr.dataset.id=rid;tr.innerHTML=html;tb.prepend(tr);while(tb.rows.length>max)tb.deleteRow(-1)}
function kpis(s){const items=[['Loops closed',s.loops_closed],['Forks answered',s.forks_answered],['To code',s.forks_clear_to_code],['To Opus',s.forks_to_opus],['Hours saved',s.hours_saved],['Opus calls',s.opus_calls],['Jev latency ms',s.jev_avg_latency_ms],['Jev $/decision',(s.jev_avg_cost_per_decision_usd||0).toExponential(2)]];
 document.getElementById('kpis').innerHTML=items.map(([k,v])=>`<div class="k"><b>${esc(v)}</b><span>${k} (24h)</span></div>`).join('')}
const es=new EventSource('/events?token='+encodeURIComponent(tok));
es.onopen=()=>document.getElementById('live').textContent='live';
es.onerror=()=>document.getElementById('live').textContent='reconnecting';
es.onmessage=m=>{const d=JSON.parse(m.data);
 (d.forks||[]).forEach(f=>add('forks',`<td>${t(f.ts)}</td><td>${f.job_id??''}</td><td>${esc(f.node)}</td><td>${esc(f.question)}</td><td>${probs(JSON.parse(f.probs))}</td><td>${esc(f.value)}</td><td><span class="tag ${f.kind}">${f.kind}</span> ${esc(f.route)}</td><td>${f.latency_ms?Math.round(f.latency_ms)+' ms':'-'}</td><td class="res ${f.result||''}">${esc(f.result||'open')}</td>`,200,f.id));
 (d.results||[]).forEach(r=>{const c=document.querySelector(`#forks tr[data-id="${r.id}"] .res`);if(c){c.textContent=r.result;c.className='res '+r.result}});
 (d.edges||[]).forEach(e=>add('edges',`<td>${t(e.ts)}</td><td>${e.job_id}</td><td>${esc(e.src)} → ${esc(e.dst)} <small>${esc(e.name)}</small></td><td>${esc(e.type)}</td><td><small>${esc(e.summary)}</small></td>`));
 (d.events||[]).forEach(e=>add('events',`<td>${t(e.ts)}</td><td>${e.job_id??''}</td><td>${esc(e.level)}</td><td>${esc(e.text)}</td>`));
 if(d.gates){document.getElementById('gates').innerHTML=d.gates.map(g=>`<tr><td>${g.id}</td><td>${g.job_id}</td><td>${esc(g.action)}</td><td>${esc(g.status)}</td><td>${esc(g.decided_by||'')}</td></tr>`).join('')}
 if(d.stats)kpis(d.stats)};
</script></body></html>"""


def make_handler(store, graph, token: str):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):  # keep tokens out of access logs
            pass

        def _authed(self, q: dict) -> bool:
            given = q.get("token", [""])[0] or self.headers.get("X-Dashboard-Token", "")
            return bool(token) and hmac.compare_digest(given, token)

        def do_GET(self):
            u = urllib.parse.urlparse(self.path)
            q = urllib.parse.parse_qs(u.query)
            if not self._authed(q):
                self.send_response(401)
                self.end_headers()
                self.wfile.write(b"token required")
                return
            if u.path == "/":
                body = PAGE.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif u.path == "/api/report":
                body = json.dumps(daily(store, graph), default=str).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)
            elif u.path == "/events":
                self._stream()
            else:
                self.send_response(404)
                self.end_headers()

        def _stream(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            last = {"forks": 0, "edges": 0, "events": 0}
            first, tick, results = True, 0, {}
            try:
                while True:
                    d = {}
                    for tbl in last:
                        limit = 200 if first else 500
                        rows = store.rows(f"SELECT * FROM {tbl} WHERE id>? ORDER BY id DESC LIMIT ?", (last[tbl], limit))
                        if tbl == "forks":  # forks are updated in place when their job ends
                            rows = [{k: v for k, v in r.items() if k != "state"} for r in rows]
                        if rows:
                            last[tbl] = rows[0]["id"]
                            d[tbl] = list(reversed(rows))
                    changed = store.rows("SELECT id,result FROM forks WHERE result IS NOT NULL ORDER BY id DESC LIMIT 300")
                    upd = [c for c in changed if results.get(c["id"]) != c["result"]]
                    for c in changed:
                        results[c["id"]] = c["result"]
                    if upd and not first:
                        d["results"] = upd
                    if first or tick % 5 == 0:
                        d["gates"] = store.rows("SELECT id,job_id,action,status,decided_by FROM gates ORDER BY id DESC LIMIT 30")
                        d["stats"] = daily(store, graph)
                    if d:
                        self.wfile.write(f"data: {json.dumps(d, default=str)}\n\n".encode())
                    else:
                        self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    first, tick = False, tick + 1
                    time.sleep(1)
            except (BrokenPipeError, ConnectionResetError):
                return

    return H


def serve(store, graph, host: str = "127.0.0.1", port: int = 8765, token: str | None = None,
          background: bool = False) -> ThreadingHTTPServer:
    token = token or os.environ.get("DASHBOARD_TOKEN", "")
    if not token:
        raise SystemExit("DASHBOARD_TOKEN missing from .env; dashboard refuses to start without it")
    srv = ThreadingHTTPServer((host, port), make_handler(store, graph, token))
    srv.daemon_threads = True
    if background:
        threading.Thread(target=srv.serve_forever, daemon=True, name="dashboard").start()
    else:
        srv.serve_forever()
    return srv
