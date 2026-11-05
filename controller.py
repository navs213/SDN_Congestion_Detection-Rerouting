"""
Ryu controller for Project 27 (OpenFlow 1.3).

Topology (must match topology.py):
    left edge s1 (h1, h2, h4)  --[A via s2 | B via s3 | C via s5]--  right edge s4 (h3, h5)

D1 - monitoring
  * Network view: polls port stats on BOTH edge switches (s1 and s4) every
    POLL_INTERVAL seconds. A path's load is the busier of its two directions.
  * Application view: UDP listener on QOS_PORT receives QoS reports
    (loss, throughput, delay) from the receivers.
  * A path is CONGESTED when its utilization >= UTIL_THRESHOLD or the
    application-reported loss on flows crossing it >= LOSS_THRESHOLD.

D2 - adaptation
  [1] Dynamic traffic classification
      Nothing is tied to a specific host any more. Any host can talk to any
      host. The first packet of every cross-edge (src_ip, dst_ip) pair goes to
      the controller (packet-in), becomes a tracked flow, and gets its own
      forwarding rule on the source's edge switch. Each flow is classified:
        APP  - the destination is a QoS receiver (it sends QoS reports to the
               controller, or is listed in P27_APP_HOSTS)
        BG   - everything else (e.g. the background flood)
      Classes are refreshed continuously, so a host that starts a receiver
      later is picked up automatically.
  [2] Load-aware path selection
      A new flow goes to the healthy path with the lowest cost, where
      cost = measured utilization + a small reservation for flows that were
      placed but have no rate sample yet (stops a burst of new flows from all
      landing on the same "empty" path). Per-flow rates come from flow stats.
  [3] Congestion management
      On congestion, APP flows are moved first, then (only if the path is
      still over threshold) the heaviest BG flows. A move is made only if the
      flow's measured rate fits on the target without pushing it over
      UTIL_THRESHOLD, so a flood is never chased from path to path.
  [-] Link failure: all flows on a dead path are failed over immediately
      (APP first). A hold-down after every move stops stale measurements from
      triggering a second move.

Recovery measurement and failback (added)
  [4] Recovery time
      The controller timestamps link-down detection (t_detect) and the moment
      the failover rules were sent, then sends an OpenFlow barrier and
      timestamps the barrier reply (= rules installed on the switch). Both
      latencies go to the receiver on h3 (failover_rule_ms, failover_install_ms).
      The receiver timestamps the first application packet received after the
      reroute and writes recovery_time_ms to its CSV.
  [5] Events to the receiver (UDP 5002 over the management link):
      link_down, link_up, failover_rules_sent, failback.
  [6] QoS metrics: loss, throughput, delay AND jitter are parsed from every
      report, logged and written to qos_metrics.csv.
  [7] Failback: flows displaced by a link failure return to their original path
      once it has been up for RESTORE_STABLE seconds and has room for them
      (same capacity check as congestion moves, so a flood is not chased back).

Live view and runtime control (added for the final demo)
  [8] Every poll the controller writes its state (paths, flows, QoS, events,
      recovery times) to SDN_STATE_FILE for dashboard.py.
  [9] Commands on the QoS port (UDP JSON), used by the dashboard and
      run_experiments.py:
        {"type": "cmd", "cmd": "mode", "mode": "static" | "dynamic"}
            switch mode at runtime; every flow is put back on DEFAULT_PATH so
            each experiment starts from the same state
        {"type": "cmd", "cmd": "mark", "text": "..."}   label in the event log
        {"type": "cmd", "cmd": "reset"}                  clear events/recoveries
  [10] Incidents: from the first detection (congestion or link down on a path
      carrying application flows) until every application flow is healthy
      again (loss < 1%, delay < 20 ms, not stalled) for 2 polls in a row.

Environment variables:
  P27_MODE       dynamic (default) | static
                   static = fixed routing: every flow on DEFAULT_PATH,
                   detect and log only, never reroute (baseline).
  P27_PLACEMENT  fixed (default) | load
                   how NEW flows pick a path in dynamic mode. 'fixed' (default) starts
                   every flow on DEFAULT_PATH (the old behaviour: flood and app
                   share Path A, then congestion rerouting kicks in).
  P27_APP_HOSTS  comma-separated IPs always treated as QoS receivers
                   (default 10.0.0.3).
  P27_FAILBACK   on (default) | off   move displaced flows back after repair
  P27_RESTORE_STABLE  seconds a repaired path must stay up first (default 5)
  P27_EVENT_IP   where link events go (default 192.168.100.3 = h3 management)
  P27_LOG_DIR    directory for qos_metrics.csv (default .)
  SDN_STATE_FILE live state for the dashboard (default /tmp/sdn_live_state.json)

Run:
  ryu-manager controller.py
  P27_MODE=static ryu-manager controller.py
  P27_PLACEMENT=fixed ryu-manager controller.py
"""

import collections
import csv
import json
import math
import os
import socket
import time

from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import (CONFIG_DISPATCHER, DEAD_DISPATCHER,
                                    MAIN_DISPATCHER, set_ev_cls)
from ryu.lib import hub
from ryu.lib.packet import ipv4, packet
from ryu.ofproto import ofproto_v1_3

# --------------------------------------------------------------------------- #
# Tunables
# --------------------------------------------------------------------------- #
POLL_INTERVAL = 1.0          # seconds between stats requests
STATS_SETTLE = 0.3           # seconds to wait for replies before deciding
LINK_CAPACITY_MBPS = 10.0    # must match --bw in topology.py
UTIL_THRESHOLD = 0.80        # 80% link utilization => congested
LOSS_THRESHOLD = 5.0         # 5% application-reported loss => congested
QOS_PORT = 9000              # UDP port for QoS reports from the receivers
EVENT_RECEIVER_IP = os.environ.get('P27_EVENT_IP', '192.168.100.3')  # h3 management
EVENT_RECEIVER_PORT = 5002   # receiver CSV event channel
REPORT_MAX_AGE = 6.0         # ignore QoS reports older than this (s)
REROUTE_HOLD = 6.0           # s: no new decision on a path after a move
FLOW_IDLE_TIMEOUT = 30       # s: idle per-flow rules expire and are forgotten
APP_RECEIVER_TTL = 30.0      # s: a learned receiver expires without reports
NEW_FLOW_RESERVE_MBPS = 1.0  # assumed load of a placed-but-unmeasured flow
MIN_MOVE_MBPS = 0.1          # BG flows slower than this are not worth moving
DEFAULT_PATH = 'A'           # used by static mode / 'fixed' placement
BARRIER_TIMEOUT = 1.0        # s: max wait for the switch to confirm new rules
FAILBACK = os.environ.get('P27_FAILBACK', 'on').lower() not in ('off', '0', 'no')
RESTORE_STABLE = float(os.environ.get('P27_RESTORE_STABLE', 5.0))
FAILBACK_GIVEUP = 60.0       # s: stop waiting for a repaired path to have room
LOG_DIR = os.environ.get('P27_LOG_DIR', '.')
STATE_FILE = os.environ.get('SDN_STATE_FILE', '/tmp/sdn_live_state.json')
LIVE_REPORT_AGE = 3.0        # s: reports newer than this count as "current"
RECOVERY_LOSS = 1.0          # healthy: loss below 1% ...
RECOVERY_DELAY_MS = 20.0     # ... and delay below 20 ms ...
RECOVERY_POLLS = 2           # ... for 2 polls in a row
PATH_VIA = {'A': 's2', 'B': 's3', 'C': 's5'}
QOS_FIELDS = ['time', 'mode', 'reporter', 'flow', 'thr_mbps', 'loss_pct',
              'delay_ms', 'jitter_ms']

MODE = os.environ.get('P27_MODE', 'dynamic').lower()
PLACEMENT = os.environ.get('P27_PLACEMENT', 'fixed').lower()
SEED_APP_HOSTS = {h.strip() for h in
                  os.environ.get('P27_APP_HOSTS', '10.0.0.3').split(',')
                  if h.strip()}

# --------------------------------------------------------------------------- #
# Addressing and topology (must match topology.py)
# --------------------------------------------------------------------------- #
# ip -> (edge switch dpid, host port on that switch)
HOSTS = {
    '10.0.0.1': (1, 1), '10.0.0.2': (1, 2), '10.0.0.4': (1, 3),
    '10.0.0.3': (4, 1), '10.0.0.5': (4, 4),
}
EDGES = (1, 4)               # left edge, right edge
MIDDLE = (2, 3, 5)           # one middle switch per path

# path -> {edge dpid: port that faces the path}
PATHS = {
    'A': {1: 4, 4: 2},       # via s2
    'B': {1: 5, 4: 3},       # via s3
    'C': {1: 6, 4: 5},       # via s5
}
# Middle switches: port 1 faces s1, port 2 faces s4.  edge dpid -> port
MIDDLE_PORT_TO = {1: 1, 4: 2}

# edge dpid -> {uplink port: path}
EDGE_PORT_TO_PATH = {dpid: {} for dpid in EDGES}
for _name, _ports in PATHS.items():
    for _dpid, _port in _ports.items():
        EDGE_PORT_TO_PATH[_dpid][_port] = _name

# Every switch port that belongs to a path. If any of these links goes down,
# the whole path is unusable.  (dpid, port) -> path name
LINK_TO_PATH = {
    (1, 4): 'A', (2, 1): 'A', (2, 2): 'A', (4, 2): 'A',
    (1, 5): 'B', (3, 1): 'B', (3, 2): 'B', (4, 3): 'B',
    (1, 6): 'C', (5, 1): 'C', (5, 2): 'C', (4, 5): 'C',
}

# Flow-table priorities
MISS_PRIORITY = 1            # IPv4 table-miss -> controller
BASE_PRIORITY = 10           # per-destination rules (local delivery / middle)
FLOW_PRIORITY = 20           # per-flow (src,dst) rules on the source edge

APP, BG = 'app', 'bg'


class CsvLog:
    """Append-only CSV, flushed per row. NaN/None become blank cells."""

    def __init__(self, name, fields):
        os.makedirs(LOG_DIR, exist_ok=True)
        path = os.path.join(LOG_DIR, name)
        new = not os.path.exists(path) or os.path.getsize(path) == 0
        self.fields = fields
        self.f = open(path, 'a', newline='')
        self.w = csv.writer(self.f)
        if new:
            self.w.writerow(fields)
            self.f.flush()

    @staticmethod
    def _cell(v):
        if v is None or (isinstance(v, float) and (math.isnan(v) or math.isinf(v))):
            return ''
        return round(v, 6) if isinstance(v, float) else v

    def write(self, **row):
        self.w.writerow([self._cell(row.get(k)) for k in self.fields])
        self.f.flush()


class Flow:
    """One directional host-to-host flow that crosses the core."""

    def __init__(self, src, dst, path, cls):
        self.src = src
        self.dst = dst
        self.src_dpid = HOSTS[src][0]
        self.path = path
        self.cls = cls
        self.rate_mbps = 0.0
        self.measured = False        # has a rate sample on the current path
        self.prev = None             # (time, byte_count) of last sample
        self.since = time.time()
        self.displaced_from = None   # path this flow was pushed off by a link failure

    @property
    def key(self):
        return (self.src, self.dst)

    def __str__(self):
        return '%s->%s' % (self.src, self.dst)


class CongestionController(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.datapaths = {}
        if MODE not in ('dynamic', 'static'):
            self.logger.warning('Unknown P27_MODE %r, using dynamic', MODE)
        self.mode = MODE if MODE in ('dynamic', 'static') else 'dynamic'
        if PLACEMENT not in ('load', 'fixed'):
            self.logger.warning('Unknown P27_PLACEMENT %r, using load', PLACEMENT)
        self.placement = PLACEMENT if PLACEMENT in ('load', 'fixed') else 'load'

        self.flows = {}                # (src, dst) -> Flow
        self.app_receivers = {}        # learned receiver ip -> last report time
        self.down_links = {p: set() for p in PATHS}   # path -> {(dpid, port)}
        self.hold_until = {p: 0.0 for p in PATHS}     # per-path decision hold
        self.congested = {p: False for p in PATHS}
        self.relief_failed = set()     # paths where "no target" was logged
        self.prev_tx = {}              # (dpid, port) -> (time, tx_bytes)
        self.dir_mbps = {}             # (path, edge dpid) -> Mbps
        self.path_util = {p: 0.0 for p in PATHS}
        self.qos_reports = {}          # flow name -> (time, report, rx_ip)
        self.restored_at = {}          # path -> time its last dead link came back
        self.barrier_ctx = {}          # barrier xid -> pending-install context
        self.qos_csv = CsvLog('qos_metrics.csv', QOS_FIELDS)
        self.latest_reports = {}       # flow name -> (time, report): every report, for the live view
        self.event_log = collections.deque(maxlen=200)
        self.recoveries = collections.deque(maxlen=50)
        self.incident = None
        self.good_polls = 0
        self.good_since = None
        self.event('info', 'Controller started in %s mode (%s placement)'
                   % (self.mode.upper(), 'fixed' if self._fixed_placement() else 'load'))

        self.logger.info('Controller mode: %s, placement: %s, failback: %s',
                         self.mode.upper(),
                         'FIXED' if self._fixed_placement() else 'LOAD',
                         'ON' if (FAILBACK and self.mode == 'dynamic') else 'OFF')
        self.monitor_thread = hub.spawn(self._monitor)
        self.qos_thread = hub.spawn(self._qos_listener)

    # ------------------------------------------------------------------ #
    # Switch connection handling
    # ------------------------------------------------------------------ #
    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        dp = ev.msg.datapath
        self.logger.info('Switch s%d connected', dp.id)
        # The table is wiped on (re)connect, so forget flows that lived on it.
        for key in [k for k, f in self.flows.items() if f.src_dpid == dp.id]:
            del self.flows[key]
        self.install_base_rules(dp)

    @set_ev_cls(ofp_event.EventOFPStateChange,
                [MAIN_DISPATCHER, DEAD_DISPATCHER])
    def state_change_handler(self, ev):
        dp = ev.datapath
        if ev.state == MAIN_DISPATCHER:
            self.datapaths[dp.id] = dp
        elif ev.state == DEAD_DISPATCHER:
            self.datapaths.pop(dp.id, None)

    # ------------------------------------------------------------------ #
    # Flow rules
    # ------------------------------------------------------------------ #
    def add_flow(self, dp, priority, match, actions, idle_timeout=0, flags=0):
        ofp, parser = dp.ofproto, dp.ofproto_parser
        inst = [parser.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, actions)]
        dp.send_msg(parser.OFPFlowMod(datapath=dp, priority=priority,
                                      match=match, instructions=inst,
                                      idle_timeout=idle_timeout, flags=flags))

    def ip_rule(self, dp, dst_ip, out_port):
        parser = dp.ofproto_parser
        match = parser.OFPMatch(eth_type=0x0800, ipv4_dst=dst_ip)
        self.add_flow(dp, BASE_PRIORITY, match,
                      [parser.OFPActionOutput(out_port)])

    def install_base_rules(self, dp):
        """Path-independent rules. Per-flow rules are added on packet-in.

        Edge:   deliver to local hosts; everything else IPv4 -> controller.
        Middle: forward by destination (toward s1 or s4).
        """
        ofp, parser = dp.ofproto, dp.ofproto_parser
        dp.send_msg(parser.OFPFlowMod(                # start from a clean table
            datapath=dp, table_id=ofp.OFPTT_ALL, command=ofp.OFPFC_DELETE,
            out_port=ofp.OFPP_ANY, out_group=ofp.OFPG_ANY,
            match=parser.OFPMatch()))
        if dp.id in EDGES:
            for ip, (dpid, port) in HOSTS.items():
                if dpid == dp.id:
                    self.ip_rule(dp, ip, port)
            miss = parser.OFPMatch(eth_type=0x0800)
            self.add_flow(dp, MISS_PRIORITY, miss, [parser.OFPActionOutput(
                ofp.OFPP_CONTROLLER, ofp.OFPCML_NO_BUFFER)])
        elif dp.id in MIDDLE:
            for ip, (dpid, _port) in HOSTS.items():
                self.ip_rule(dp, ip, MIDDLE_PORT_TO[dpid])

    def _install_flow(self, flow):
        """(Re)write the per-flow rule on the flow's source edge switch.
        An add with identical match+priority overwrites the old rule."""
        dp = self.datapaths.get(flow.src_dpid)
        if dp is None:
            return False
        ofp, parser = dp.ofproto, dp.ofproto_parser
        match = parser.OFPMatch(eth_type=0x0800, ipv4_src=flow.src,
                                ipv4_dst=flow.dst)
        out = PATHS[flow.path][dp.id]
        self.add_flow(dp, FLOW_PRIORITY, match, [parser.OFPActionOutput(out)],
                      idle_timeout=FLOW_IDLE_TIMEOUT,
                      flags=ofp.OFPFF_SEND_FLOW_REM)
        return True

    def path_is_up(self, path_name):
        return not self.down_links[path_name]

    def event(self, kind, text):
        """Remember an event for the dashboard (the normal log lines stay as they are)."""
        self.event_log.append({'t': time.time(), 'kind': kind, 'text': text})

    # ------------------------------------------------------------------ #
    # [1] Dynamic traffic classification
    # ------------------------------------------------------------------ #
    def _is_receiver(self, ip):
        if ip in SEED_APP_HOSTS:
            return True
        t = self.app_receivers.get(ip)
        return t is not None and time.time() - t <= APP_RECEIVER_TTL

    def _classify(self, dst_ip):
        return APP if self._is_receiver(dst_ip) else BG

    def _reclassify(self):
        for f in self.flows.values():
            cls = self._classify(f.dst)
            if cls != f.cls:
                self.logger.info('Flow %s reclassified %s -> %s',
                                 f, f.cls.upper(), cls.upper())
                f.cls = cls

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def packet_in_handler(self, ev):
        msg = ev.msg
        dp = msg.datapath
        ofp, parser = dp.ofproto, dp.ofproto_parser
        in_port = msg.match['in_port']
        ip = packet.Packet(msg.data).get_protocol(ipv4.ipv4)
        if ip is None or ip.src not in HOSTS or ip.dst not in HOSTS:
            return
        src_dpid, src_port = HOSTS[ip.src]
        if dp.id != src_dpid or in_port != src_port:
            return                      # only trust the source's own edge port
        if HOSTS[ip.dst][0] == src_dpid:
            return                      # same edge: local rule handles it

        flow = self.flows.get((ip.src, ip.dst))
        if flow is None:
            path = self.place_flow()
            if path is None:
                self.logger.critical('No healthy path for new flow %s->%s',
                                     ip.src, ip.dst)
                return
            flow = Flow(ip.src, ip.dst, path, self._classify(ip.dst))
            self.flows[flow.key] = flow
            self.logger.info(
                '========== ACTIVE PATH: %s -> %s | Path %s | class=%s ==========',
                flow.src, flow.dst, flow.path, flow.cls.upper())
        self._install_flow(flow)
        # Do not lose the packet that triggered the miss.
        dp.send_msg(parser.OFPPacketOut(
            datapath=dp, buffer_id=msg.buffer_id, in_port=in_port,
            actions=[parser.OFPActionOutput(PATHS[flow.path][dp.id])],
            data=msg.data if msg.buffer_id == ofp.OFP_NO_BUFFER else None))

    @set_ev_cls(ofp_event.EventOFPFlowRemoved, MAIN_DISPATCHER)
    def flow_removed_handler(self, ev):
        msg = ev.msg
        if msg.priority != FLOW_PRIORITY:
            return
        if msg.reason not in (msg.datapath.ofproto.OFPRR_IDLE_TIMEOUT,
                              msg.datapath.ofproto.OFPRR_HARD_TIMEOUT):
            return
        try:
            key = (msg.match['ipv4_src'], msg.match['ipv4_dst'])
        except KeyError:
            return
        flow = self.flows.get(key)
        if flow is not None and flow.src_dpid == msg.datapath.id:
            del self.flows[key]
            self.logger.info('Flow %s idle, forgotten', flow)

    # ------------------------------------------------------------------ #
    # [2] Load-aware path selection
    # ------------------------------------------------------------------ #
    def _fixed_placement(self):
        return self.mode == 'static' or self.placement == 'fixed'

    def _path_cost(self, path):
        """Load on a path as a fraction of capacity, plus a reservation for
        flows placed there but not yet measured.

        Port counters show what the link actually *sent*, which can never go
        above 100% - on an overloaded link the excess is queued or dropped.
        Flow counters on the ingress edge show what was *offered* to the path
        (13 Mbps when a 9 Mbps flood meets 4 Mbps of application traffic).
        Using the larger of the two means congestion relief moves enough flows
        in one step instead of stopping when the capped figure dips below the
        threshold."""
        offered = sum(f.rate_mbps for f in self.flows.values()
                      if f.path == path and f.measured) / LINK_CAPACITY_MBPS
        unmeasured = sum(1 for f in self.flows.values()
                         if f.path == path and not f.measured)
        return (max(self.path_util[path], offered)
                + unmeasured * NEW_FLOW_RESERVE_MBPS / LINK_CAPACITY_MBPS)

    def _n_flows(self, path):
        return sum(1 for f in self.flows.values() if f.path == path)

    def place_flow(self):
        """Path for a brand-new flow, or None if every path is down."""
        if self.mode == 'static':
            return DEFAULT_PATH         # baseline: fixed routing, no failover
        up = [p for p in PATHS if self.path_is_up(p)]
        if not up:
            return None
        if self.placement == 'fixed' and DEFAULT_PATH in up:
            return DEFAULT_PATH
        return min(up, key=lambda p: (self._path_cost(p), self._n_flows(p), p))

    # ------------------------------------------------------------------ #
    # Moving flows
    # ------------------------------------------------------------------ #
    def set_flow_path(self, flow, path, reason):
        if path == flow.path:
            return
        self.logger.warning('*** Rerouting (%s): %s [%s] Path %s -> Path %s   [t=%.3f]',
                            reason, flow, flow.cls.upper(), flow.path, path,
                            time.time())
        old = flow.path
        self.event('reroute', 'Rerouted %s flow %s->%s: Path %s -> Path %s (%s)'
                   % (flow.cls.upper(), flow.src, flow.dst, old, path, reason))
        if reason == 'link failure':
            if flow.displaced_from is None:
                flow.displaced_from = old       # remember the original home
        else:
            flow.displaced_from = None          # failback / congestion: settled
        flow.path = path
        flow.prev = None                # counters restart with the new rule
        flow.measured = False
        installed = self._install_flow(flow)
        if installed:
            self.logger.info(
                '========== ACTIVE PATH UPDATED: %s -> %s | Path %s | class=%s ==========',
                flow.src, flow.dst, flow.path, flow.cls.upper())
        # Forget stale loss reports for this receiver and hold both paths so
        # old measurements cannot trigger another move (ping-pong).
        for name in [n for n, (_t, _r, rx) in self.qos_reports.items()
                     if rx in (None, flow.dst)]:
            del self.qos_reports[name]
        hold = time.time() + REROUTE_HOLD
        self.hold_until[old] = self.hold_until[path] = hold
        self.relief_failed.discard(old)

    def _send_csv_event(self, event, path, timestamp=None, flow=None,
                        failover_rule_ms=None, failover_install_ms=None,
                        t_detect=None, t_rules_sent=None):
        """Send link/failover events to h3 so they enter the receiver's CSV."""
        payload = {
            'type': 'sdn_event',
            'event': event,
            'path': path,
            'timestamp': time.time() if timestamp is None else timestamp,
            'src_ip': flow.src if flow is not None else '',
            'dst_ip': flow.dst if flow is not None else '',
            'failover_rule_ms': failover_rule_ms,          # detect -> rules sent
            'failover_install_ms': failover_install_ms,    # detect -> barrier reply
            't_detect': t_detect,
            't_rules_sent': t_rules_sent,
        }
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as event_sock:
                event_sock.sendto(json.dumps(payload).encode(),
                                  (EVENT_RECEIVER_IP, EVENT_RECEIVER_PORT))
        except OSError as exc:
            self.logger.warning('Could not send CSV event to h3: %s', exc)

    # ------------------------------------------------------------------ #
    # Rule-installation timing (OpenFlow barrier)
    # ------------------------------------------------------------------ #
    def _barrier(self, dpids):
        """Send a barrier to each switch. The reply means every flow-mod sent
        before it has been applied. Returns a context dict."""
        ctx = {'pending': set(), 't_installed': None}
        for dpid in dpids:
            dp = self.datapaths.get(dpid)
            if dp is None:
                continue
            req = dp.ofproto_parser.OFPBarrierRequest(dp)
            dp.send_msg(req)                    # send_msg assigns req.xid
            self.barrier_ctx[req.xid] = ctx
            ctx['pending'].add(req.xid)
        return ctx

    @set_ev_cls(ofp_event.EventOFPBarrierReply, MAIN_DISPATCHER)
    def barrier_reply_handler(self, ev):
        ctx = self.barrier_ctx.pop(ev.msg.xid, None)
        if ctx is None:
            return
        ctx['pending'].discard(ev.msg.xid)
        if not ctx['pending']:
            ctx['t_installed'] = time.time()

    def _notify_installed(self, ctx, event, path, flows, t_ref, t_sent):
        """Greenlet: wait for the barrier reply, then tell the receiver how
        long the rules took (from t_ref: link-down detection or failback
        decision). Sent even if the reply never comes (install time blank)."""
        deadline = time.time() + BARRIER_TIMEOUT
        while ctx['pending'] and time.time() < deadline:
            hub.sleep(0.002)
        for xid in list(ctx['pending']):
            self.barrier_ctx.pop(xid, None)
        rule_ms = (t_sent - t_ref) * 1000
        install_ms = None
        if ctx['t_installed'] is not None:
            install_ms = (ctx['t_installed'] - t_ref) * 1000
            self.logger.warning('*** %s: rules sent after %.1f ms, confirmed '
                                'installed after %.1f ms', event, rule_ms, install_ms)
            if event == 'failover_rules_sent':
                self.event('info', 'Failover on Path %s: rules sent after %.1f ms, '
                           'installed after %.1f ms' % (path, rule_ms, install_ms))
        else:
            self.logger.warning('%s: no barrier reply within %.1fs', event,
                                BARRIER_TIMEOUT)
        for f in flows:
            if f.dst == '10.0.0.3':
                self._send_csv_event(event, path, time.time(), f, rule_ms,
                                     install_ms, t_detect=t_ref,
                                     t_rules_sent=t_sent)

    # ------------------------------------------------------------------ #
    # Link-failure handling
    # ------------------------------------------------------------------ #
    @set_ev_cls(ofp_event.EventOFPPortStatus, MAIN_DISPATCHER)
    def port_status_handler(self, ev):
        """A switch reports that a port changed state (link up/down)."""
        t_detect = time.time()
        msg = ev.msg
        ofp = msg.datapath.ofproto
        port = msg.desc
        down = bool(port.state & ofp.OFPPS_LINK_DOWN) \
            or bool(port.config & ofp.OFPPC_PORT_DOWN) \
            or msg.reason == ofp.OFPPR_DELETE
        self._link_changed(msg.datapath.id, port.port_no, down, t_detect)

    def _link_changed(self, dpid, port_no, down, t_detect):
        key = (dpid, port_no)
        path = LINK_TO_PATH.get(key)
        if path is None:
            return                      # host port or unknown port
        was_up = self.path_is_up(path)
        if down:
            self.down_links[path].add(key)
        else:
            self.down_links[path].discard(key)
        now_up = self.path_is_up(path)

        if was_up and not now_up:
            self.logger.error('*** LINK DOWN: s%d port %d  ->  Path %s is DOWN   [t=%.3f]',
                              dpid, port_no, path, t_detect)
            self.restored_at.pop(path, None)        # cancel any pending failback
            victims = [f for f in self.flows.values() if f.path == path]
            self.event('failure', 'LINK FAILURE on Path %s (s%d port %d)' % (path, dpid, port_no))
            if any(f.cls == APP for f in victims):
                self.start_incident('link failure on Path %s' % path, t_detect)
            receiver_flows = [f for f in victims if f.dst == '10.0.0.3']
            if receiver_flows:
                for flow in receiver_flows:
                    self._send_csv_event('link_down', path, t_detect, flow)
            else:
                self._send_csv_event('link_down', path, t_detect)
            self._handle_path_down(path, t_detect)
        elif not was_up and now_up:
            t_up = time.time()
            if self.mode == 'dynamic' and FAILBACK:
                self.restored_at[path] = t_up
                n = sum(1 for f in self.flows.values() if f.displaced_from == path)
                self.logger.warning('*** LINK UP: Path %s is available again; %d '
                                    'displaced flow(s) move back after %.0fs if '
                                    'it stays up and has room', path, n, RESTORE_STABLE)
            else:
                self.logger.warning('*** LINK UP: Path %s is available again '
                                    '(existing flows stay where they are)', path)
            self.event('info', 'Path %s is back up' % path)
            self._send_csv_event('link_up', path, t_up)
        elif down:
            self.logger.info('Link s%d port %d down (Path %s already down)',
                             dpid, port_no, path)

    def _handle_path_down(self, path, t_detect):
        victims = [f for f in self.flows.values() if f.path == path]
        if self.mode == 'static':
            self.logger.warning('Static mode: %d flow(s) stay on Path %s (no failover)',
                                len(victims), path)
            self.event('info', 'Static mode: no failover, %d flow(s) stay on the dead Path %s'
                       % (len(victims), path))
            return
        pred = {p: self._path_cost(p) for p in PATHS}
        moved_flows = []
        # APP flows first - that is what the user notices.
        for f in sorted(victims, key=lambda f: (f.cls != APP, -f.rate_mbps)):
            up = [p for p in PATHS if p != path and self.path_is_up(p)]
            if not up:
                self.logger.critical('ALL PATHS DOWN - cannot fail over %s', f)
                break
            tgt = min(up, key=lambda p: (pred[p], self._n_flows(p), p))
            self.set_flow_path(f, tgt, 'link failure')
            pred[tgt] += f.rate_mbps / LINK_CAPACITY_MBPS
            moved_flows.append(f)
        if moved_flows:
            ctx = self._barrier({f.src_dpid for f in moved_flows})
            t_sent = time.time()
            self.logger.warning('*** Failover rules sent %.1f ms after link-down detection',
                                (t_sent - t_detect) * 1000)
            hub.spawn(self._notify_installed, ctx, 'failover_rules_sent', path,
                      moved_flows, t_detect, t_sent)

    # ------------------------------------------------------------------ #
    # Link restoration (failback)
    # ------------------------------------------------------------------ #
    def _check_failback(self):
        """Move flows displaced by a link failure back to their original path
        once it has been up for RESTORE_STABLE seconds and can absorb them
        without crossing UTIL_THRESHOLD. APP flows go first."""
        if self.mode != 'dynamic' or not FAILBACK or not self.restored_at:
            return
        now = time.time()
        pred = {p: self._path_cost(p) for p in PATHS}
        moved = []
        for path, t_up in list(self.restored_at.items()):
            if not self.path_is_up(path):
                del self.restored_at[path]
                continue
            if now - t_up < RESTORE_STABLE or now < self.hold_until[path]:
                continue
            cands = sorted((f for f in list(self.flows.values())
                            if f.displaced_from == path and f.path != path
                            and now >= self.hold_until[f.path]),
                           key=lambda f: (f.cls != APP, -f.rate_mbps))
            waiting = []
            for f in cands:
                share = f.rate_mbps / LINK_CAPACITY_MBPS
                if pred[path] + share >= UTIL_THRESHOLD:
                    waiting.append(f)
                    continue
                old = f.path
                self.set_flow_path(f, path, 'failback')
                pred[path] += share
                pred[old] -= share
                moved.append(f)
            still = [f for f in self.flows.values()
                     if f.displaced_from == path and f.path != path]
            if not still:
                del self.restored_at[path]
            elif now - t_up > FAILBACK_GIVEUP:
                for f in still:
                    f.displaced_from = None
                    self.logger.warning('Failback of %s to Path %s abandoned '
                                        '(no room after %.0fs)', f, path,
                                        FAILBACK_GIVEUP)
                del self.restored_at[path]
            elif waiting:
                self.logger.info('Failback to Path %s waiting: %d flow(s) do not '
                                 'fit yet (util %.0f%%)', path, len(waiting),
                                 self.path_util[path] * 100)
        if moved:
            ctx = self._barrier({f.src_dpid for f in moved})
            t_sent = time.time()
            for path in {f.path for f in moved}:
                hub.spawn(self._notify_installed, ctx, 'failback', path,
                          [f for f in moved if f.path == path], t_sent, t_sent)

    # ------------------------------------------------------------------ #
    # Network view: port statistics (both edges) and per-flow statistics
    # ------------------------------------------------------------------ #
    def _monitor(self):
        while True:
            for dpid in EDGES:
                dp = self.datapaths.get(dpid)
                if dp:
                    parser, ofp = dp.ofproto_parser, dp.ofproto
                    dp.send_msg(parser.OFPPortStatsRequest(dp, 0, ofp.OFPP_ANY))
                    dp.send_msg(parser.OFPFlowStatsRequest(dp))
            hub.sleep(STATS_SETTLE)         # let the replies arrive
            self._evaluate()
            hub.sleep(max(POLL_INTERVAL - STATS_SETTLE, 0.1))

    @set_ev_cls(ofp_event.EventOFPPortStatsReply, MAIN_DISPATCHER)
    def port_stats_reply_handler(self, ev):
        dpid = ev.msg.datapath.id
        if dpid not in EDGE_PORT_TO_PATH:
            return
        now = time.time()
        for stat in ev.msg.body:
            path = EDGE_PORT_TO_PATH[dpid].get(stat.port_no)
            if path is None:
                continue
            key = (dpid, stat.port_no)
            prev = self.prev_tx.get(key)
            self.prev_tx[key] = (now, stat.tx_bytes)
            if prev is None or now <= prev[0]:
                continue
            delta = max(stat.tx_bytes - prev[1], 0)
            self.dir_mbps[(path, dpid)] = delta * 8 / (now - prev[0]) / 1e6
        for p in PATHS:
            worst = max(self.dir_mbps.get((p, d), 0.0) for d in EDGES)
            self.path_util[p] = worst / LINK_CAPACITY_MBPS

    @set_ev_cls(ofp_event.EventOFPFlowStatsReply, MAIN_DISPATCHER)
    def flow_stats_reply_handler(self, ev):
        dpid = ev.msg.datapath.id
        now = time.time()
        for stat in ev.msg.body:
            if stat.priority != FLOW_PRIORITY:
                continue
            try:
                key = (stat.match['ipv4_src'], stat.match['ipv4_dst'])
            except KeyError:
                continue
            flow = self.flows.get(key)
            if flow is None or flow.src_dpid != dpid:
                continue
            prev, flow.prev = flow.prev, (now, stat.byte_count)
            if prev is None or now <= prev[0] or stat.byte_count < prev[1]:
                continue                # first sample / counters were reset
            flow.rate_mbps = (stat.byte_count - prev[1]) * 8 \
                / (now - prev[0]) / 1e6
            flow.measured = True

    # ------------------------------------------------------------------ #
    # Application view: QoS reports from the socket application
    # ------------------------------------------------------------------ #
    def _qos_listener(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(('0.0.0.0', QOS_PORT))
        except OSError as e:
            self.logger.error('QoS listener could not bind UDP %d: %s', QOS_PORT, e)
            return
        self.logger.info('QoS listener on UDP port %d', QOS_PORT)
        while True:
            try:
                data, addr = sock.recvfrom(4096)
                report = json.loads(data.decode())
                if isinstance(report, dict) and report.get('type') == 'cmd':
                    self.handle_command(report)
                    continue
                flow = report['flow']
                loss = float(report['loss_pct'])
                thr = float(report['thr_mbps'])
                delay = self._opt_float(report.get('delay_ms'))
                jitter = self._opt_float(report.get('jitter_ms', report.get('jitter')))
            except (ValueError, KeyError, TypeError, AttributeError,
                    UnicodeDecodeError) as e:
                self.logger.warning('Bad QoS report ignored: %s', e)
                continue
            except OSError as e:
                self.logger.error('QoS socket error: %s', e)
                hub.sleep(1)
                continue
            self._handle_report(addr[0], flow, report)
            self._log_qos(addr[0], flow, thr, loss, delay, jitter)

    @staticmethod
    def _opt_float(v):
        """Delay/jitter are null when the receiver had no packets in the
        interval: treat that as 'no sample' (NaN), not as 0."""
        return float('nan') if v is None else float(v)

    def _log_qos(self, reporter, flow, thr, loss, delay, jitter):
        def ms(v):
            return 'n/a' if math.isnan(v) else '%.2f' % v
        self.logger.info('QoS report from %s: flow %s thr=%.2f Mbps loss=%.1f%% '
                         'delay=%s ms jitter=%s ms',
                         reporter, flow, thr, loss, ms(delay), ms(jitter))
        self.qos_csv.write(time=time.time(), mode=self.mode, reporter=reporter,
                           flow=flow, thr_mbps=thr, loss_pct=loss,
                           delay_ms=delay, jitter_ms=jitter)

    def _handle_report(self, addr_ip, flow_name, report):
        """Identify the receiver (optional 'rx_ip' field, else the sender's
        address if it is a known host), mark it as a QoS receiver, and store
        the report unless its paths are in hold-down."""
        rx = report.get('rx_ip', addr_ip)
        rx = rx if rx in HOSTS else None
        now = time.time()
        if flow_name not in self.latest_reports:
            self.event('info', 'QoS reports started for flow %s' % flow_name)
        self.latest_reports[flow_name] = (now, report)
        if rx is not None:
            self.app_receivers[rx] = now
            self._reclassify()
        paths = self._paths_of_receiver(rx)
        if any(now < self.hold_until[p] for p in paths):
            return                      # stale: measured before the last move
        self.qos_reports[flow_name] = (now, report, rx)

    def _paths_of_receiver(self, rx):
        """Paths carrying APP flows toward this receiver (all APP paths if the
        receiver could not be identified)."""
        return {f.path for f in self.flows.values()
                if f.cls == APP and (rx is None or f.dst == rx)}

    def _path_app_loss(self, path):
        now = time.time()
        dsts = {f.dst for f in self.flows.values()
                if f.path == path and f.cls == APP}
        has_app = bool(dsts)
        losses = [float(r['loss_pct'])
                  for t, r, rx in self.qos_reports.values()
                  if now - t <= REPORT_MAX_AGE and has_app
                  and (rx is None or rx in dsts)]
        return max(losses) if losses else 0.0

    # ------------------------------------------------------------------ #
    # [3] Congestion detection and management
    # ------------------------------------------------------------------ #
    def _log_state(self):
        for p in PATHS:
            n = self._n_flows(p)
            napp = sum(1 for f in self.flows.values()
                       if f.path == p and f.cls == APP)
            u = self.path_util[p]
            if not self.path_is_up(p):
                status = 'DOWN'
            else:
                status = 'HIGH' if u >= UTIL_THRESHOLD else 'ok'
            self.logger.info(
                'Path %s  fwd=%5.2f rev=%5.2f Mbps  util=%5.1f%%  %-4s flows=%d (app=%d)',
                p, self.dir_mbps.get((p, 1), 0.0), self.dir_mbps.get((p, 4), 0.0),
                u * 100, status, n, napp)
        for f in self.flows.values():
            self.logger.info(
                'ACTIVE FLOW: %s -> %s | ACTIVE PATH: %s | class=%s | rate=%6.2f Mbps',
                f.src, f.dst, f.path, f.cls.upper(), f.rate_mbps)

    def _evaluate(self):
        self._reclassify()
        self._log_state()
        self._check_failback()
        now = time.time()
        for path in PATHS:
            if not self.path_is_up(path):
                self.congested[path] = False
                continue
            if now < self.hold_until[path]:
                continue                # just rerouted: wait for fresh data
            util = self.path_util[path]
            loss = self._path_app_loss(path)
            net_cong = util >= UTIL_THRESHOLD
            app_cong = loss >= LOSS_THRESHOLD
            cong = net_cong or app_cong

            if cong and not self.congested[path]:
                reasons = []
                if net_cong:
                    reasons.append(f'link util {util*100:.0f}% >= {UTIL_THRESHOLD*100:.0f}%')
                if app_cong:
                    reasons.append(f'app loss {loss:.1f}% >= {LOSS_THRESHOLD:.0f}%')
                self.logger.warning('*** CONGESTION DETECTED on Path %s (%s)',
                                    path, ', '.join(reasons))
                self.event('congestion', 'CONGESTION DETECTED on Path %s (%s)'
                           % (path, ', '.join(reasons)))
                if any(f.path == path and f.cls == APP for f in self.flows.values()):
                    self.start_incident('congestion on Path %s' % path)
            elif not cong and self.congested[path]:
                self.logger.info('*** Congestion cleared on Path %s', path)
                self.event('info', 'Congestion cleared on Path %s' % path)
                self.relief_failed.discard(path)
            self.congested[path] = cong

            if cong and self.mode == 'dynamic':
                self._relieve(path, app_cong)
        self._check_recovery()
        self._write_state()

    def _relieve(self, path, app_loss):
        """Move flows off a congested path.

        Order: APP flows first, then BG flows by descending rate. A flow moves
        only to a healthy path that can absorb its measured rate while staying
        under UTIL_THRESHOLD. BG flows move only while the path is still over
        threshold; APP flows move whenever the trigger was app-reported loss.
        """
        flows = [f for f in self.flows.values() if f.path == path]
        app = sorted((f for f in flows if f.cls == APP),
                     key=lambda f: -f.rate_mbps)
        bg = sorted((f for f in flows
                     if f.cls != APP and f.rate_mbps >= MIN_MOVE_MBPS),
                    key=lambda f: -f.rate_mbps)
        if not app and not bg:
            self.logger.info('Path %s congested but nothing movable on it', path)
            return
        pred = {p: self._path_cost(p) for p in PATHS}
        moved = False
        for f in app + bg:
            if pred[path] < UTIL_THRESHOLD and not (f.cls == APP and app_loss):
                break                   # relieved; no need to move more
            share = f.rate_mbps / LINK_CAPACITY_MBPS
            targets = [p for p in PATHS if p != path and self.path_is_up(p)
                       and pred[p] + share < UTIL_THRESHOLD]
            if not targets:
                if path not in self.relief_failed:
                    self.relief_failed.add(path)
                    self.logger.warning(
                        'No healthy path can absorb %s (%.2f Mbps); not rerouting',
                        f, f.rate_mbps)
                    self.event('info', 'No path has room for %s %s (%.1f Mbps) - not moved'
                               % (f.cls.upper(), f, f.rate_mbps))
                continue
            tgt = min(targets, key=lambda p: (pred[p], self._n_flows(p), p))
            self.set_flow_path(f, tgt, 'congestion')
            pred[path] -= share
            pred[tgt] += share
            moved = True
        if moved:
            self.congested[path] = False

    # ------------------------------------------------------------------ #
    # [9] Runtime commands
    # ------------------------------------------------------------------ #
    def handle_command(self, msg):
        cmd = msg.get('cmd')
        if cmd == 'mode' and msg.get('mode') in ('static', 'dynamic'):
            self.mode = msg['mode']
            self.restored_at.clear()
            for f in list(self.flows.values()):
                f.displaced_from = None
                if f.path != DEFAULT_PATH and self.path_is_up(DEFAULT_PATH):
                    self.set_flow_path(f, DEFAULT_PATH, 'mode change')
            for p in PATHS:
                self.hold_until[p] = 0.0
                self.congested[p] = False
            self.relief_failed.clear()
            self.incident = None
            self.good_polls = 0
            self.logger.warning('*** Mode set to %s', self.mode.upper())
            self.event('info', 'Mode set to %s' % self.mode.upper())
        elif cmd == 'mark':
            self.event('mark', str(msg.get('text', ''))[:200])
        elif cmd == 'reset':
            self.event_log.clear()
            self.recoveries.clear()
            self.latest_reports.clear()
            self.incident = None
            self.event('info', 'Dashboard history reset')
        else:
            self.logger.warning('Unknown command ignored: %s', msg)

    # ------------------------------------------------------------------ #
    # [10] Incidents and recovery time
    # ------------------------------------------------------------------ #
    def start_incident(self, cause, t=None):
        if self.incident is None:
            self.incident = {'start': time.time() if t is None else t,
                             'cause': cause, 'mode': self.mode}
            self.good_polls = 0

    def _live_reports(self):
        now = time.time()
        return [r for t, r in self.latest_reports.values() if now - t <= LIVE_REPORT_AGE]

    def _check_recovery(self):
        if self.incident is None:
            return
        now = time.time()
        reps = self._live_reports()
        app_paths = {f.path for f in self.flows.values() if f.cls == APP}
        paths_ok = all(self.path_is_up(p) and not self.congested[p] for p in app_paths)

        def good(r):
            d = r.get('delay_ms')
            return (not r.get('stalled') and float(r['loss_pct']) < RECOVERY_LOSS
                    and d is not None and float(d) < RECOVERY_DELAY_MS)
        if not (paths_ok and reps and all(good(r) for r in reps)):
            self.good_polls = 0
            return
        if self.good_polls == 0:
            self.good_since = now
        self.good_polls += 1
        if self.good_polls >= RECOVERY_POLLS:
            took = max(0.0, self.good_since - self.incident['start'])
            self.recoveries.append({'t': now, 'start': self.incident['start'],
                                    'cause': self.incident['cause'],
                                    'mode': self.incident['mode'],
                                    'recovery_s': round(took, 2)})
            self.logger.warning('*** Application RECOVERED from %s in %.1f s (%s mode)',
                                self.incident['cause'], took, self.incident['mode'])
            self.event('recovered', 'Application RECOVERED from %s in %.1f s (%s mode)'
                       % (self.incident['cause'], took, self.incident['mode']))
            self.incident = None
            self.good_polls = 0

    # ------------------------------------------------------------------ #
    # [8] Live state for the dashboard
    # ------------------------------------------------------------------ #
    def _write_state(self):
        now = time.time()
        paths = {}
        for p in PATHS:
            on = [f for f in self.flows.values() if f.path == p]
            paths[p] = {'up': self.path_is_up(p), 'via': PATH_VIA.get(p, ''),
                        'util': round(self.path_util[p], 4),
                        'mbps': round(self.path_util[p] * LINK_CAPACITY_MBPS, 3),
                        'fwd_mbps': round(self.dir_mbps.get((p, 1), 0.0), 3),
                        'rev_mbps': round(self.dir_mbps.get((p, 4), 0.0), 3),
                        'flows': len(on), 'app_flows': sum(1 for f in on if f.cls == APP),
                        'congested': self.congested[p]}
        flows = [{'src': f.src, 'dst': f.dst, 'path': f.path, 'cls': f.cls,
                  'rate_mbps': round(f.rate_mbps, 3), 'displaced_from': f.displaced_from}
                 for f in sorted(self.flows.values(), key=lambda f: f.key)]
        reports = {}
        for name, (t, r) in sorted(self.latest_reports.items()):
            if now - t <= LIVE_REPORT_AGE:
                reports[name] = {'thr_mbps': float(r.get('thr_mbps', 0)),
                                 'loss_pct': float(r.get('loss_pct', 0)),
                                 'delay_ms': None if r.get('delay_ms') is None
                                 else float(r['delay_ms']),
                                 'jitter_ms': None if r.get('jitter_ms') is None
                                 else float(r['jitter_ms']),
                                 'stalled': bool(r.get('stalled', False)),
                                 'age': round(now - t, 2)}
        state = {'ts': now, 'mode': self.mode,
                 'placement': 'fixed' if self._fixed_placement() else 'load',
                 'default_path': DEFAULT_PATH, 'paths': paths, 'flows': flows,
                 'reports': reports, 'incident': self.incident,
                 'thresholds': {'util': UTIL_THRESHOLD, 'loss': LOSS_THRESHOLD,
                                'capacity_mbps': LINK_CAPACITY_MBPS},
                 'events': list(self.event_log)[-100:],
                 'recoveries': list(self.recoveries)}
        tmp = STATE_FILE + '.tmp'
        try:
            with open(tmp, 'w') as fh:
                json.dump(state, fh)
            os.replace(tmp, STATE_FILE)
        except OSError as e:
            self.logger.error('Could not write %s: %s', STATE_FILE, e)
