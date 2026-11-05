#!/usr/bin/env python3
"""
Live dashboard for Project 27.

Reads the controller's live state (written every second to
/tmp/sdn_live_state.json by controller.py), keeps a 10-minute history, and
serves a web page showing:
  * a live network map: the three paths coloured by load, dead links broken,
    and every flow drawn along the path it is using right now
  * a plain-language verdict (healthy / affected / recovering) and key numbers
  * recovery times of every incident, static vs dynamic
  * charts of path utilization, delay, loss and throughput with the periods
    the application was affected shaded, event markers and a hover readout
  * the flow table and the controller's event log
The page can also switch the controller between STATIC and DYNAMIC mode.

Only the Python standard library is used, and the page needs no internet
(charts are drawn on <canvas> by hand).

Run on the VM (normal user, no sudo):
  python3 dashboard.py            # then open http://localhost:8000
"""

import argparse
import collections
import json
import os
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

STATE_FILE = os.environ.get('SDN_STATE_FILE', '/tmp/sdn_live_state.json')
CONTROLLER_CMD = ('127.0.0.1', 9000)
HISTORY_SECONDS = 600


class History:
    def __init__(self):
        self.lock = threading.Lock()
        self.samples = collections.deque(maxlen=HISTORY_SECONDS)
        self.latest = None
        self.last_ts = None

    def poll_forever(self):
        while True:
            try:
                with open(STATE_FILE) as f:
                    state = json.load(f)
            except (OSError, ValueError):
                state = None
            if state and state.get('ts') != self.last_ts:
                self.last_ts = state['ts']
                sample = {
                    't': state['ts'],
                    'mode': state['mode'],
                    'incident': bool(state.get('incident')),
                    'paths': {p: {'util': v['util'], 'up': v['up']}
                              for p, v in state['paths'].items()},
                    'flows': {n: {'loss': r['loss_pct'], 'delay': r['delay_ms'],
                                  'thr': r['thr_mbps'], 'stalled': r['stalled']}
                              for n, r in state['reports'].items()},
                }
                with self.lock:
                    self.latest = state
                    self.samples.append(sample)
            time.sleep(0.5)

    def snapshot(self, since):
        with self.lock:
            hist = [s for s in self.samples if since is None or s['t'] > since]
            return {'state': self.latest, 'history': hist, 'server_time': time.time()}

    def clear(self):
        with self.lock:
            self.samples.clear()


HISTORY = History()


def send_cmd(msg):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.sendto(json.dumps(msg).encode(), CONTROLLER_CMD)
    finally:
        s.close()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):      # keep the terminal quiet
        pass

    def _send(self, code, body, ctype):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == '/':
            self._send(200, PAGE, 'text/html; charset=utf-8')
        elif url.path == '/favicon.ico':
            self._send(204, b'', 'image/x-icon')
        elif url.path == '/api/state':
            q = parse_qs(url.query)
            since = float(q['since'][0]) if 'since' in q else None
            self._send(200, json.dumps(HISTORY.snapshot(since)), 'application/json')
        else:
            self._send(404, 'not found', 'text/plain')

    def do_POST(self):
        url = urlparse(self.path)
        length = int(self.headers.get('Content-Length', 0) or 0)
        try:
            body = json.loads(self.rfile.read(length) or b'{}')
        except ValueError:
            body = {}
        if url.path == '/api/mode' and body.get('mode') in ('static', 'dynamic'):
            send_cmd({'type': 'cmd', 'cmd': 'mode', 'mode': body['mode']})
        elif url.path == '/api/reset':
            send_cmd({'type': 'cmd', 'cmd': 'reset'})
            HISTORY.clear()
        elif url.path == '/api/mark':
            send_cmd({'type': 'cmd', 'cmd': 'mark', 'text': str(body.get('text', ''))[:200]})
        else:
            self._send(400, '{"ok": false}', 'application/json')
            return
        self._send(200, '{"ok": true}', 'application/json')


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SDN Live Console</title>
<style>
  :root {
    --bg: #e9edf1;       /* cool console grey */
    --panel: #fbfcfd;
    --ink: #17202b;
    --muted: #5a6573;
    --line: #cfd6de;
    --grid: #e3e8ee;
    --ok: #1d8a57;
    --warn: #c47f00;
    --fault: #c4372e;
    --pathA: #3358c4;    /* path identities, kept apart from status colours */
    --pathB: #a8457a;
    --pathC: #677a12;
    --flow1: #e0711c;
    --flow2: #1696b4;
    --bgflow: #8b95a3;
    --band: rgba(196, 55, 46, .09);
  }
  * { box-sizing: border-box; }
  html { font-size: 15px; }
  body { margin: 0; background: var(--bg); color: var(--ink);
         font-family: "Ubuntu", "Cantarell", "Segoe UI", system-ui, sans-serif;
         font-variant-numeric: tabular-nums; line-height: 1.4; }
  button, select { font: inherit; }
  :focus-visible { outline: 2px solid var(--pathA); outline-offset: 2px; }

  /* ---------- top bar */
  .bar { display: flex; flex-wrap: wrap; align-items: center; gap: 12px 20px;
         padding: 14px 24px; background: var(--ink); color: #fff; }
  .bar h1 { margin: 0; font-size: 1.15rem; font-weight: 500; letter-spacing: .01em; }
  .bar .proj { color: #a9b4c2; font-size: .9rem; }
  .bar .grow { flex: 1; }
  .live { display: inline-flex; align-items: center; gap: 8px; font-size: .9rem; color: #d8dee6; }
  .live i { width: 9px; height: 9px; border-radius: 50%; background: var(--fault); }
  .live.on i { background: #3ccf86; box-shadow: 0 0 0 4px rgba(60,207,134,.18); }
  .seg { display: inline-flex; border: 1px solid #47525f; border-radius: 9px; padding: 3px; gap: 3px; }
  .seg button { border: 0; background: transparent; color: #c9d1db; padding: 6px 14px;
                border-radius: 6px; cursor: pointer; font-weight: 500; }
  .seg button[aria-pressed="true"] { background: #fff; color: var(--ink); }
  .bar select, .bar .plain { background: transparent; color: #d8dee6; border: 1px solid #47525f;
                border-radius: 8px; padding: 6px 10px; cursor: pointer; }
  .bar select option { color: var(--ink); }

  main { padding: 20px 24px 32px; display: grid; gap: 20px; max-width: 1600px; margin: 0 auto; }
  section, .panel { background: var(--panel); border: 1px solid var(--line); border-radius: 12px; }
  h2 { font-size: 1rem; font-weight: 500; margin: 0; }
  .sub { color: var(--muted); font-size: .88rem; }

  /* ---------- hero: map + status */
  .hero { display: grid; grid-template-columns: minmax(0, 1.85fr) minmax(300px, 1fr); }
  .map { padding: 16px 18px 10px; border-right: 1px solid var(--line); }
  .map svg { width: 100%; height: auto; display: block; }
  .maphead { display: flex; justify-content: space-between; align-items: baseline; gap: 12px; flex-wrap: wrap; }
  .key { display: flex; flex-wrap: wrap; gap: 6px 16px; font-size: .85rem; color: var(--muted); }
  .key span { display: inline-flex; align-items: center; gap: 6px; }
  .key b { width: 18px; height: 4px; border-radius: 2px; display: inline-block; }
  .status { padding: 20px 22px; display: flex; flex-direction: column; gap: 18px; }
  .verdict { border-radius: 10px; padding: 16px 18px; background: #e7f4ec; }
  .verdict.problem { background: #fbe9e7; }
  .verdict.recovering { background: #fff3dc; }
  .verdict .state { font-size: 1.75rem; font-weight: 700; line-height: 1.1; }
  .verdict.normal .state { color: var(--ok); }
  .verdict.problem .state { color: var(--fault); }
  .verdict.recovering .state { color: var(--warn); }
  .verdict p { margin: 6px 0 0; }
  .nums { display: grid; grid-template-columns: 1fr 1fr; gap: 14px 18px; }
  .num .v { font-size: 1.6rem; font-weight: 700; }
  .num .l { color: var(--muted); font-size: .85rem; }
  .compare { display: grid; gap: 8px; }
  .cmp { display: grid; grid-template-columns: 70px 1fr 64px; align-items: center; gap: 10px; font-size: .9rem; }
  .cmp .track { height: 10px; background: var(--grid); border-radius: 5px; overflow: hidden; }
  .cmp .fill { height: 100%; border-radius: 5px; }
  .cmp .t { text-align: right; font-weight: 700; }

  /* ---------- charts */
  .charts { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 20px; }
  .chart { padding: 14px 16px 10px; position: relative; }
  .chart header { display: flex; justify-content: space-between; align-items: baseline; gap: 10px; flex-wrap: wrap; }
  .legend { display: flex; gap: 14px; flex-wrap: wrap; font-size: .85rem; color: var(--muted); }
  .legend span::before { content: ""; display: inline-block; width: 14px; height: 3px; border-radius: 2px;
                         background: var(--c); vertical-align: middle; margin-right: 6px; }
  .chart canvas { width: 100%; height: 210px; display: block; margin-top: 6px; cursor: crosshair; }
  .chart .watch { margin: 4px 0 0; color: var(--muted); font-size: .86rem; }
  .tip { position: fixed; pointer-events: none; background: var(--ink); color: #fff; font-size: .82rem;
         padding: 8px 10px; border-radius: 8px; display: none; z-index: 5; min-width: 150px; }
  .tip div { display: flex; justify-content: space-between; gap: 14px; }
  .marks { display: flex; gap: 18px; flex-wrap: wrap; font-size: .85rem; color: var(--muted); padding: 0 2px; }
  .marks span { display: inline-flex; align-items: center; gap: 6px; }
  .marks i { width: 2px; height: 14px; display: inline-block; }
  .marks em { font-style: normal; width: 22px; height: 12px; background: var(--band); border-radius: 2px; display: inline-block; }

  /* ---------- lower */
  .lower { display: grid; grid-template-columns: minmax(0, 1.2fr) minmax(0, 1fr); gap: 20px; }
  .panel { padding: 14px 16px; }
  table { width: 100%; border-collapse: collapse; font-size: .92rem; margin-top: 8px; }
  th { text-align: left; color: var(--muted); font-weight: 500; padding: 6px 6px; border-bottom: 1px solid var(--line); }
  td { padding: 8px 6px; border-bottom: 1px solid var(--grid); vertical-align: middle; }
  tr:last-child td { border-bottom: 0; }
  .chip { display: inline-block; padding: 2px 9px; border-radius: 999px; font-size: .82rem; font-weight: 500; color: #fff; }
  .cls { font-size: .82rem; color: var(--muted); }
  .dot { display: inline-block; width: 10px; height: 10px; border-radius: 50%; margin-right: 8px; vertical-align: -1px; }
  .log { max-height: 330px; overflow: auto; margin-top: 8px; }
  .ev { display: grid; grid-template-columns: 74px 12px 1fr; gap: 10px; padding: 7px 0; border-bottom: 1px solid var(--grid); font-size: .9rem; }
  .ev:last-child { border-bottom: 0; }
  .ev time { color: var(--muted); }
  .ev i { width: 10px; height: 10px; border-radius: 50%; margin-top: 5px; background: var(--line); }
  .ev.congestion i, .ev.failure i { background: var(--fault); }
  .ev.reroute i { background: var(--pathA); }
  .ev.recovered i { background: var(--ok); }
  .ev.mark i { background: var(--warn); }
  .ev.congestion span, .ev.failure span { color: var(--fault); font-weight: 500; }
  .ev.recovered span { color: var(--ok); font-weight: 500; }
  .empty { color: var(--muted); padding: 10px 0; }

  /* flows on the map */
  .flow { fill: none; stroke-linecap: round; stroke-linejoin: round; stroke-dasharray: 2 12;
          animation: run 0.9s linear infinite; }
  .flow.bg { stroke-dasharray: 1 9; }
  @keyframes run { to { stroke-dashoffset: -14; } }
  @media (prefers-reduced-motion: reduce) { .flow { animation: none; stroke-dasharray: none; } }

  @media (max-width: 1100px) {
    .hero { grid-template-columns: 1fr; }
    .map { border-right: 0; border-bottom: 1px solid var(--line); }
    .charts, .lower { grid-template-columns: 1fr; }
  }
  @media (max-width: 560px) {
    main { padding: 14px; }
    .bar { padding: 12px 14px; }
    .nums { grid-template-columns: 1fr; }
  }
</style>
</head>
<body>
<div class="bar">
  <div>
    <h1>SDN live console</h1>
    <div class="proj">Project 27 &ndash; congestion detection and traffic rerouting</div>
  </div>
  <div class="grow"></div>
  <span class="live" id="live"><i></i><span>Waiting for the controller</span></span>
  <div class="seg" role="group" aria-label="Controller mode">
    <button id="m-static" aria-pressed="false" onclick="setMode('static')">Static</button>
    <button id="m-dynamic" aria-pressed="false" onclick="setMode('dynamic')">Dynamic</button>
  </div>
  <label class="proj" for="win">Show last</label>
  <select id="win">
    <option value="60">1 minute</option><option value="120" selected>2 minutes</option>
    <option value="300">5 minutes</option><option value="600">10 minutes</option>
  </select>
  <button class="plain" onclick="resetAll()">Clear history</button>
</div>

<main>
  <section class="hero">
    <div class="map">
      <div class="maphead">
        <h2>Where the traffic is going</h2>
        <div class="key">
          <span><b style="background:var(--ok)"></b>under 50%</span>
          <span><b style="background:var(--warn)"></b>50&ndash;80%</span>
          <span><b style="background:var(--fault)"></b>over 80% or down</span>
          <span><b style="background:var(--bgflow)"></b>background flow</span>
        </div>
      </div>
      <svg id="topo" viewBox="0 0 960 365" role="img" aria-label="Live network map"></svg>
    </div>
    <div class="status">
      <div class="verdict normal" id="verdict">
        <div class="state" id="v-state">Waiting</div>
        <p id="v-text">Start the controller and the network to see live data.</p>
      </div>
      <div class="nums">
        <div class="num"><div class="v" id="n-loss">&ndash;</div><div class="l">worst application loss, last second</div></div>
        <div class="num"><div class="v" id="n-delay">&ndash;</div><div class="l">worst one-way delay</div></div>
        <div class="num"><div class="v" id="n-thr">&ndash;</div><div class="l">application throughput delivered</div></div>
        <div class="num"><div class="v" id="n-flows">&ndash;</div><div class="l">flows the controller is tracking</div></div>
      </div>
      <div>
        <h2>Recovery time, static vs dynamic</h2>
        <div class="sub" id="cmp-note">Each problem that hit the application, and how long until it was healthy again.</div>
        <div class="compare" id="compare"></div>
      </div>
    </div>
  </section>

  <div class="marks">
    <span><em></em>application affected</span>
    <span><i style="background:var(--fault)"></i>problem detected</span>
    <span><i style="background:var(--pathA)"></i>flow rerouted</span>
    <span><i style="background:var(--ok)"></i>recovered</span>
    <span><i style="background:var(--warn)"></i>experiment step</span>
  </div>

  <div class="charts">
    <section class="chart"><header><h2>Path utilization</h2><div class="legend" id="lg-util"></div></header>
      <canvas id="ch-util"></canvas>
      <p class="watch">The cause: a path crossing the dashed 80% line, and load moving to other paths when flows are rerouted.</p></section>
    <section class="chart"><header><h2>One-way delay per flow</h2><div class="legend" id="lg-delay"></div></header>
      <canvas id="ch-delay"></canvas>
      <p class="watch">The first symptom: the switch queue fills, so delay rises from ~5 ms toward ~850 ms before packets drop. Log scale.</p></section>
    <section class="chart"><header><h2>Packet loss per flow</h2><div class="legend" id="lg-loss"></div></header>
      <canvas id="ch-loss"></canvas>
      <p class="watch">The damage: packets dropped when the queue overflows, or 100% while a flow is cut off by a dead link.</p></section>
    <section class="chart"><header><h2>Throughput per flow (Mbps)</h2><div class="legend" id="lg-thr"></div></header>
      <canvas id="ch-thr"></canvas>
      <p class="watch">What the application gets: 2 Mbps per flow when healthy, less under congestion, zero during an outage.</p></section>
  </div>

  <div class="lower">
    <div class="panel">
      <h2>Flows and their current paths</h2>
      <div class="sub">Learned from the first packet of each flow. APP flows go to a receiver that sends QoS reports; everything else is BG.</div>
      <table><thead><tr><th>Flow</th><th>Class</th><th>Path now</th><th>Rate</th><th>Note</th></tr></thead>
      <tbody id="flowtab"></tbody></table>
    </div>
    <div class="panel">
      <h2>What the controller did</h2>
      <div class="log" id="events"></div>
    </div>
  </div>
</main>
<div class="tip" id="tip"></div>

<script>
const css = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
const HOSTS = {'10.0.0.1': 'h1', '10.0.0.2': 'h2', '10.0.0.3': 'h3', '10.0.0.4': 'h4', '10.0.0.5': 'h5'};
const host = ip => HOSTS[ip] || ip;
const PATHC = {A: '--pathA', B: '--pathB', C: '--pathC'};
const FLOWC = {'10.0.0.1': '--flow1', '10.0.0.2': '--flow2'};
const flowColor = src => css(FLOWC[src] || '--bgflow');
const esc = t => String(t).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const fmtTime = t => new Date(t * 1000).toLocaleTimeString([], {hour12: false});
let history = [], latest = null, lastTs = null, hoverT = null;

/* ---------------------------------------------------------------- data */
async function poll() {
  try {
    const r = await fetch('/api/state' + (lastTs ? '?since=' + lastTs : ''), {cache: 'no-store'});
    const d = await r.json();
    if (d.history.length) { history.push(...d.history); lastTs = d.history[d.history.length - 1].t; }
    if (history.length > 1200) history = history.slice(-1200);
    latest = d.state;
    const fresh = latest && (d.server_time - latest.ts) < 5;
    const live = document.getElementById('live');
    live.classList.toggle('on', !!fresh);
    live.lastElementChild.textContent = fresh ? 'Controller live' :
      (latest ? 'Controller stopped' : 'Waiting for the controller');
    if (latest) render();
  } catch (e) {
    const live = document.getElementById('live');
    live.classList.remove('on'); live.lastElementChild.textContent = 'Dashboard server stopped';
  }
}

/* ---------------------------------------------------------------- page */
function render() {
  const s = latest;
  document.getElementById('m-static').setAttribute('aria-pressed', s.mode === 'static');
  document.getElementById('m-dynamic').setAttribute('aria-pressed', s.mode === 'dynamic');
  renderVerdict(s);
  renderNumbers(s);
  renderCompare(s);
  drawMap(s);
  renderFlows(s);
  renderEvents(s);
  drawCharts();
}

function renderVerdict(s) {
  const v = document.getElementById('verdict'), st = document.getElementById('v-state'),
        tx = document.getElementById('v-text');
  const appPaths = Object.entries(s.paths).filter(([p, x]) => x.app_flows > 0);
  const hurting = appPaths.some(([p, x]) => !x.up || x.congested);
  const down = Object.entries(s.paths).filter(([p, x]) => !x.up).map(([p]) => p);
  if (s.incident) {
    const secs = Math.max(0, Math.round(s.ts - s.incident.start));
    v.className = 'verdict ' + (hurting ? 'problem' : 'recovering');
    st.textContent = hurting ? 'Application affected' : 'Recovering';
    let t = cap(s.incident.cause) + ', ' + secs + ' s ago. ';
    t += s.mode === 'dynamic'
      ? (hurting ? 'The controller is moving flows away.' : 'Flows have been moved; waiting for 2 healthy seconds.')
      : 'Static mode: the controller only watches, so the application waits it out.';
    tx.textContent = t;
  } else {
    v.className = 'verdict normal';
    st.textContent = 'All flows healthy';
    const where = (s.flows || []).filter(f => f.cls === 'app').map(f => host(f.src) + ' on ' + f.path);
    tx.textContent = (where.length ? 'Application flows: ' + where.join(', ') + '. ' : 'No application traffic yet. ') +
      (down.length ? 'Down: Path ' + down.join(', ') + '. ' : '') +
      cap(s.mode) + ' mode.';
  }
}
const cap = t => t.charAt(0).toUpperCase() + t.slice(1);

function renderNumbers(s) {
  const r = Object.values(s.reports || {});
  const set = (id, txt, cls) => { const e = document.getElementById(id); e.textContent = txt; e.style.color = cls ? css(cls) : ''; };
  if (!r.length) { ['n-loss', 'n-delay', 'n-thr'].forEach(i => set(i, '–')); }
  else {
    const loss = Math.max(...r.map(x => x.loss_pct));
    const ds = r.map(x => x.delay_ms).filter(x => x !== null);
    const thr = r.reduce((a, x) => a + x.thr_mbps, 0);
    set('n-loss', loss.toFixed(1) + '%', loss >= s.thresholds.loss ? '--fault' : (loss > 0 ? '--warn' : '--ok'));
    if (ds.length) { const d = Math.max(...ds); set('n-delay', fmtMs(d), d > 100 ? '--fault' : (d > 20 ? '--warn' : '--ok')); }
    else set('n-delay', 'no packets', '--fault');
    set('n-thr', thr.toFixed(2) + ' Mbps');
  }
  const f = s.flows || [];
  set('n-flows', f.length + ' (' + f.filter(x => x.cls === 'app').length + ' app)');
}
const fmtMs = d => d >= 100 ? Math.round(d) + ' ms' : d.toFixed(1) + ' ms';

function renderCompare(s) {
  const recs = (s.recoveries || []).slice(-6);
  const box = document.getElementById('compare');
  if (!recs.length) { box.innerHTML = '<div class="empty">Nothing has gone wrong yet. Start a flood or take a link down.</div>'; return; }
  const max = Math.max(...recs.map(r => r.recovery_s), 5);
  box.innerHTML = recs.map(r => `<div class="cmp" title="${esc(r.cause)} at ${fmtTime(r.start)}">
      <span>${r.mode}</span>
      <div class="track"><div class="fill" style="width:${Math.max(2, r.recovery_s / max * 100)}%;background:${css(r.mode === 'static' ? '--fault' : '--ok')}"></div></div>
      <span class="t">${r.recovery_s.toFixed(1)} s</span></div>
      <div class="sub" style="margin:-4px 0 4px">${esc(cap(r.cause))}, ${fmtTime(r.start)}</div>`).join('');
}

/* ---------------------------------------------------------------- network map */
const POS = {h1: [70, 95], h2: [70, 185], h4: [70, 275], s1: [255, 185], s2: [480, 70], s3: [480, 185],
             s5: [480, 300], s4: [705, 185], h3: [890, 135], h5: [890, 255], ctl: [890, 32]};
const IPHOST = {'10.0.0.1': 'h1', '10.0.0.2': 'h2', '10.0.0.3': 'h3', '10.0.0.4': 'h4', '10.0.0.5': 'h5'};
function utilColor(p) { if (!p.up) return css('--fault'); return css(p.util >= 0.8 ? '--fault' : p.util >= 0.5 ? '--warn' : '--ok'); }

function drawMap(s) {
  const g = [];
  const L = (a, b) => `M${POS[a][0]} ${POS[a][1]} L${POS[b][0]} ${POS[b][1]}`;
  // host and management links
  for (const [h, sw] of [['h1', 's1'], ['h2', 's1'], ['h4', 's1'], ['h3', 's4'], ['h5', 's4']])
    g.push(`<path d="${L(h, sw)}" stroke="${css('--line')}" stroke-width="3" fill="none"/>`);
  g.push(`<path d="M${POS.h3[0]} ${POS.h3[1] - 22} L${POS.ctl[0]} ${POS.ctl[1] + 18}" stroke="${css('--muted')}" stroke-width="1.5" stroke-dasharray="4 4" fill="none"/>`);
  g.push(`<text x="${POS.ctl[0] - 10}" y="${(POS.h3[1] + POS.ctl[1]) / 2 + 4}" text-anchor="end" font-size="12" fill="${css('--muted')}">QoS reports</text>`);
  // the three paths as pipes coloured by load
  for (const [p, v] of Object.entries(s.paths)) {
    const via = v.via, col = utilColor(v);
    const d = L('s1', via) + ' ' + L(via, 's4');
    g.push(`<path d="${d}" stroke="${col}" stroke-opacity=".22" stroke-width="18" fill="none" stroke-linejoin="round"/>`);
    g.push(`<path d="${d}" stroke="${col}" stroke-width="3" fill="none" ${v.up ? '' : 'stroke-dasharray="8 7"'}/>`);
    const [x, y] = POS[via];
    const label = v.up ? `Path ${p}  ${Math.round(v.util * 100)}%` : `Path ${p}  down`;
    const info = `${v.mbps.toFixed(1)} Mbps, ${v.flows} flow${v.flows === 1 ? '' : 's'}`;
    // top path: both lines above; bottom path: both below; middle: name above, load below
    const [ly, iy] = y < 120 ? [y - 44, y - 28] : y > 250 ? [y + 36, y + 52] : [y - 28, y + 38];
    g.push(`<text x="${x}" y="${ly}" text-anchor="middle" font-size="14" font-weight="700" fill="${css(PATHC[p] || '--ink')}">${label}</text>`);
    g.push(`<text x="${x}" y="${iy}" text-anchor="middle" font-size="12" fill="${css('--muted')}">${info}</text>`);
  }
  // flows drawn along their current path
  const flows = (s.flows || []);
  flows.forEach((f, i) => {
    const via = (s.paths[f.path] || {}).via; const a = IPHOST[f.src], b = IPHOST[f.dst];
    if (!via || !POS[a] || !POS[b]) return;
    const off = (i - (flows.length - 1) / 2) * 6;
    const pts = [POS[a], POS.s1, POS[via], POS.s4, POS[b]].map(([x, y]) => [x, y + off]);
    const w = f.cls === 'app' ? 3.5 : 2.5;
    g.push(`<path class="flow ${f.cls === 'app' ? '' : 'bg'}" d="M${pts.map(p => p.join(' ')).join(' L')}" stroke="${flowColor(f.src)}" stroke-width="${w}"/>`);
  });
  // nodes
  const sw = n => { const [x, y] = POS[n]; return `<rect x="${x - 30}" y="${y - 17}" width="60" height="34" rx="8" fill="${css('--panel')}" stroke="${css('--ink')}" stroke-width="1.5"/>
    <text x="${x}" y="${y + 5}" text-anchor="middle" font-size="14" font-weight="700" fill="${css('--ink')}">${n}</text>`; };
  const hs = (n, sub) => { const [x, y] = POS[n]; const c = n === 'h1' ? css('--flow1') : n === 'h2' ? css('--flow2') : css('--ink');
    return `<circle cx="${x}" cy="${y}" r="21" fill="${css('--panel')}" stroke="${c}" stroke-width="2.5"/>
    <text x="${x}" y="${y + 5}" text-anchor="middle" font-size="14" font-weight="700" fill="${css('--ink')}">${n}</text>
    <text x="${x}" y="${y + 39}" text-anchor="middle" font-size="12" fill="${css('--muted')}">${sub}</text>`; };
  for (const n of ['s1', 's2', 's3', 's5', 's4']) g.push(sw(n));
  g.push(hs('h1', 'sender'), hs('h2', 'sender'), hs('h4', 'flood'), hs('h3', 'receiver'), hs('h5', 'sink'));
  const [cx, cy] = POS.ctl;
  g.push(`<rect x="${cx - 52}" y="${cy - 17}" width="104" height="34" rx="8" fill="${css('--ink')}"/>
    <text x="${cx}" y="${cy + 5}" text-anchor="middle" font-size="13" font-weight="700" fill="#fff">Ryu ${s.mode}</text>`);
  document.getElementById('topo').innerHTML = g.join('');
}

/* ---------------------------------------------------------------- tables */
function renderFlows(s) {
  const f = s.flows || [];
  document.getElementById('flowtab').innerHTML = f.length ? f.map(x =>
    `<tr><td><span class="dot" style="background:${flowColor(x.src)}"></span>${host(x.src)} &rarr; ${host(x.dst)}
       <span class="cls">${x.src} &rarr; ${x.dst}</span></td>
     <td>${x.cls.toUpperCase()}</td>
     <td><span class="chip" style="background:${css(PATHC[x.path] || '--ink')}">Path ${x.path}</span></td>
     <td>${x.rate_mbps.toFixed(2)} Mbps</td>
     <td class="cls">${x.displaced_from ? 'moved off Path ' + x.displaced_from + ' by a link failure; will return after repair' : ''}</td></tr>`).join('')
    : '<tr><td colspan="5" class="empty">No flows yet. Start the senders in h1 and h2.</td></tr>';
}
function renderEvents(s) {
  const ev = (s.events || []).slice().reverse();
  document.getElementById('events').innerHTML = ev.length ? ev.map(e =>
    `<div class="ev ${e.kind}"><time>${fmtTime(e.t)}</time><i></i><span>${esc(e.text)}</span></div>`).join('')
    : '<div class="empty">No events yet.</div>';
}

/* ---------------------------------------------------------------- charts */
function drawCharts() {
  if (!latest) return;
  const win = +document.getElementById('win').value;
  const t1 = latest.ts, t0 = t1 - win;
  const hist = history.filter(h => h.t >= t0 - 2);
  const names = [...new Set(hist.flatMap(h => Object.keys(h.flows)))].sort();
  const fl = (key) => names.map(n => ({ name: host(n.split('#')[0]) + ' → h3', color: flowColor(n.split('#')[0]),
      pts: hist.map(h => [h.t, h.flows[n] ? h.flows[n][key] : undefined]).filter(p => p[1] !== undefined) }));
  const bands = []; let open = null;
  for (const h of hist) { if (h.incident && open === null) open = h.t; if (!h.incident && open !== null) { bands.push([open, h.t]); open = null; } }
  if (open !== null) bands.push([open, t1]);
  const marks = (latest.events || []).filter(e => ['congestion', 'failure', 'reroute', 'recovered', 'mark'].includes(e.kind) && e.t >= t0);
  const ctx = {t0, t1, bands, marks};
  const paths = Object.keys(latest.paths).map(p => ({ name: 'Path ' + p, color: css(PATHC[p] || '--ink'),
      pts: hist.map(h => [h.t, h.paths[p] && h.paths[p].up ? h.paths[p].util * 100 : null]) }));
  chart('ch-util', 'lg-util', paths, {min: 0, max: 110, unit: '%', threshold: 80}, ctx);
  chart('ch-delay', 'lg-delay', fl('delay'), {log: true, min: 1, max: 2000, unit: ' ms'}, ctx);
  chart('ch-loss', 'lg-loss', fl('loss'), {min: 0, max: 100, unit: '%'}, ctx);
  const tmax = Math.max(3, ...hist.flatMap(h => Object.values(h.flows).map(f => f.thr))) * 1.15;
  chart('ch-thr', 'lg-thr', fl('thr'), {min: 0, max: tmax, unit: ' Mbps'}, ctx);
}

const CHARTS = {};
function chart(id, legendId, series, o, c) {
  const cv = document.getElementById(id), dpr = window.devicePixelRatio || 1;
  const W = cv.clientWidth, H = cv.clientHeight;
  if (cv.width !== Math.round(W * dpr) || cv.height !== Math.round(H * dpr)) { cv.width = Math.round(W * dpr); cv.height = Math.round(H * dpr); }
  const g = cv.getContext('2d'); g.setTransform(dpr, 0, 0, dpr, 0, 0); g.clearRect(0, 0, W, H);
  const Lm = 52, R = 10, T = 10, B = 24, pw = W - Lm - R, ph = H - T - B;
  const X = t => Lm + (t - c.t0) / (c.t1 - c.t0) * pw;
  const lg = v => Math.log10(Math.max(v, o.min));
  const Y = v => o.log ? T + ph - (lg(v) - lg(o.min)) / (lg(o.max) - lg(o.min)) * ph
                       : T + ph - (Math.min(v, o.max) - o.min) / (o.max - o.min) * ph;
  CHARTS[id] = {X, series, o, Lm, pw, c};
  g.font = '12px Ubuntu, Cantarell, system-ui, sans-serif';
  // incident bands
  g.fillStyle = css('--band');
  for (const [a, b] of c.bands) { const x0 = Math.max(Lm, X(a)), x1 = Math.min(Lm + pw, X(b)); if (x1 > x0) g.fillRect(x0, T, x1 - x0, ph); }
  // grid
  const ticks = o.log ? [1, 10, 100, 1000] : niceTicks(o.min, o.max);
  g.strokeStyle = css('--grid'); g.fillStyle = css('--muted'); g.lineWidth = 1; g.textAlign = 'right'; g.textBaseline = 'middle';
  for (const v of ticks) { const y = Y(v); g.beginPath(); g.moveTo(Lm, y); g.lineTo(Lm + pw, y); g.stroke(); g.fillText(tickLabel(v, o.unit), Lm - 6, y); }
  const span = c.t1 - c.t0, step = span > 300 ? 60 : span > 120 ? 30 : 15;
  g.textAlign = 'center'; g.textBaseline = 'top';
  for (let t = Math.ceil(c.t0 / step) * step; t <= c.t1; t += step) { const x = X(t); g.beginPath(); g.moveTo(x, T); g.lineTo(x, T + ph); g.stroke(); g.fillText(fmtTime(t), x, T + ph + 6); }
  if (o.threshold) { g.save(); g.setLineDash([6, 5]); g.strokeStyle = css('--fault'); g.beginPath(); g.moveTo(Lm, Y(o.threshold)); g.lineTo(Lm + pw, Y(o.threshold)); g.stroke(); g.restore(); }
  // event markers
  const mc = {congestion: '--fault', failure: '--fault', reroute: '--pathA', recovered: '--ok', mark: '--warn'};
  for (const m of c.marks) { const x = X(m.t); if (x < Lm || x > Lm + pw) continue;
    g.save(); g.strokeStyle = css(mc[m.kind]); g.globalAlpha = .8; g.lineWidth = 1.5; g.beginPath(); g.moveTo(x, T); g.lineTo(x, T + ph); g.stroke(); g.restore(); }
  // series
  g.save(); g.beginPath(); g.rect(Lm, T, pw, ph); g.clip();
  for (const s of series) { g.strokeStyle = s.color; g.lineWidth = 2.2; g.beginPath(); let pen = false;
    for (const [t, v] of s.pts) { if (v === null || v === undefined) { pen = false; continue; }
      const x = X(t), y = Y(v); if (!pen) { g.moveTo(x, y); pen = true; } else g.lineTo(x, y); }
    g.stroke(); }
  g.restore();
  g.strokeStyle = css('--line'); g.strokeRect(Lm, T, pw, ph);
  // hover crosshair
  if (hoverT !== null && hoverT >= c.t0 && hoverT <= c.t1) { const x = X(hoverT); g.save(); g.strokeStyle = css('--ink'); g.globalAlpha = .45;
    g.beginPath(); g.moveTo(x, T); g.lineTo(x, T + ph); g.stroke(); g.restore(); }
  if (!series.some(s => s.pts.some(p => p[1] !== null))) { g.fillStyle = css('--muted'); g.textAlign = 'center'; g.textBaseline = 'middle';
    g.fillText('No data yet', Lm + pw / 2, T + ph / 2); }
  document.getElementById(legendId).innerHTML = series.map(s => `<span style="--c:${s.color}">${esc(s.name)}</span>`).join('');
}
function niceTicks(a, b) { const raw = (b - a) / 4, p = Math.pow(10, Math.floor(Math.log10(raw)));
  const st = [1, 2, 2.5, 5, 10].map(m => m * p).find(s => s >= raw); const out = [];
  for (let v = Math.ceil(a / st) * st; v <= b + 1e-9; v += st) out.push(+v.toFixed(6)); return out; }
function tickLabel(v, unit) {
  if (unit === ' ms') return v >= 1000 ? (v / 1000) + ' s' : v + ' ms';
  if (unit === '%') return v + '%';
  return fmtTick(v);
}
function fmtTick(v) { return v >= 1000 ? (v / 1000) + 'k' : String(+v.toFixed(2)); }

/* hover readout shared by all charts */
for (const id of ['ch-util', 'ch-delay', 'ch-loss', 'ch-thr']) {
  const cv = document.getElementById(id);
  cv.addEventListener('mousemove', e => {
    const ch = CHARTS[id]; if (!ch) return;
    const rect = cv.getBoundingClientRect(), x = e.clientX - rect.left;
    if (x < ch.Lm || x > ch.Lm + ch.pw) { hideTip(); return; }
    hoverT = ch.c.t0 + (x - ch.Lm) / ch.pw * (ch.c.t1 - ch.c.t0);
    const rows = ch.series.map(s => { let best = null, bd = 1e9;
      for (const p of s.pts) { const d = Math.abs(p[0] - hoverT); if (d < bd) { bd = d; best = p; } }
      const v = best && bd < 2 ? best[1] : null;
      return `<div><span style="color:${s.color}">${esc(s.name)}</span><b>${v === null || v === undefined ? 'no data' : fmtVal(v, ch.o.unit)}</b></div>`; });
    const tip = document.getElementById('tip');
    tip.innerHTML = `<div><span>${fmtTime(hoverT)}</span></div>` + rows.join('');
    tip.style.display = 'block'; tip.style.left = Math.min(e.clientX + 14, window.innerWidth - 200) + 'px'; tip.style.top = (e.clientY + 14) + 'px';
    drawCharts();
  });
  cv.addEventListener('mouseleave', hideTip);
}
function hideTip() { hoverT = null; document.getElementById('tip').style.display = 'none'; drawCharts(); }
function fmtVal(v, unit) { return (unit === ' ms' ? fmtMs(v) : (unit === '%' ? v.toFixed(1) + '%' : v.toFixed(2) + unit)); }

/* ---------------------------------------------------------------- actions */
async function setMode(m) { await fetch('/api/mode', {method: 'POST', body: JSON.stringify({mode: m})}); setTimeout(poll, 300); }
async function resetAll() { await fetch('/api/reset', {method: 'POST', body: '{}'}); history = []; lastTs = null; setTimeout(poll, 300); }
document.getElementById('win').addEventListener('change', drawCharts);
window.addEventListener('resize', () => latest && drawCharts());
poll(); setInterval(poll, 1000);
</script>
</body>
</html>
"""


def main():
    ap = argparse.ArgumentParser(description='Live dashboard for the SDN controller')
    ap.add_argument('--port', type=int, default=8000)
    ap.add_argument('--host', default='0.0.0.0')
    args = ap.parse_args()
    threading.Thread(target=HISTORY.poll_forever, daemon=True).start()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f'Dashboard running: open http://localhost:{args.port} in a browser '
          f'(reading {STATE_FILE}). Ctrl+C to stop.', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
