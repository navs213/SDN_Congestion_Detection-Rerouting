"""
Ryu controller for Project 27 (OpenFlow 1.3).

What it does (D1):
  1. Installs IPv4 forwarding rules so all traffic between the left side
     (h1, h2, h4 on s1) and the right side (h3, h5 on s4) uses Path A.
  2. Network view: polls port statistics on s1 every POLL_INTERVAL seconds and
     computes the load on each path (s1 port 4 = Path A, s1 port 5 = Path B).
  3. Application view: listens on UDP port QOS_PORT for QoS reports sent by
     the UDP receiver on h3 (loss, throughput, delay per flow).
  4. Combines both views: the active path is marked CONGESTED when its link
     utilization crosses UTIL_THRESHOLD *or* the application reports loss
     above LOSS_THRESHOLD.

Rerouting (D2) only needs a call to self.set_path('B') when congestion is
detected - the function is already here.

Run:
  ryu-manager controller.py
"""

import json
import socket
import time

from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import (CONFIG_DISPATCHER, DEAD_DISPATCHER,
                                    MAIN_DISPATCHER, set_ev_cls)
from ryu.lib import hub
from ryu.ofproto import ofproto_v1_3

POLL_INTERVAL = 2.0          # seconds between port-stats requests
LINK_CAPACITY_MBPS = 10.0    # must match --bw in topology.py
UTIL_THRESHOLD = 0.80        # 80% link utilization => congested
LOSS_THRESHOLD = 5.0         # 5% application-reported loss => congested
QOS_PORT = 9000              # UDP port for QoS reports from the receiver
REPORT_MAX_AGE = 6.0         # ignore QoS reports older than this (s)

# Addressing (must match topology.py)
S1_HOSTS = {'10.0.0.1': 1, '10.0.0.2': 2, '10.0.0.4': 3}   # ip -> s1 port
S4_HOSTS = {'10.0.0.3': 1, '10.0.0.5': 4}                  # ip -> s4 port

# For each path: s1 port toward the path, s4 port toward the path
PATHS = {
    'A': {'s1_out': 4, 's4_out': 2, 'via': 2},   # via s2
    'B': {'s1_out': 5, 's4_out': 3, 'via': 3},   # via s3
}
PORT_TO_PATH = {4: 'A', 5: 'B'}   # s1 uplink port -> path name


class CongestionController(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.datapaths = {}
        self.active_path = 'A'
        self.prev_tx_bytes = {}
        self.path_util = {'A': 0.0, 'B': 0.0}
        self.qos_reports = {}          # flow name -> (time, report dict)
        self.congested = False
        self.monitor_thread = hub.spawn(self._monitor)
        self.qos_thread = hub.spawn(self._qos_listener)

    # ------------------------------------------------------------------ #
    # Switch connection handling
    # ------------------------------------------------------------------ #
    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        dp = ev.msg.datapath
        self.logger.info('Switch s%d connected', dp.id)
        self.install_rules(dp, self.active_path)

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
    def add_flow(self, dp, priority, match, actions):
        ofp, parser = dp.ofproto, dp.ofproto_parser
        inst = [parser.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, actions)]
        dp.send_msg(parser.OFPFlowMod(datapath=dp, priority=priority,
                                      match=match, instructions=inst))

    def ip_rule(self, dp, dst_ip, out_port, priority=10):
        parser = dp.ofproto_parser
        match = parser.OFPMatch(eth_type=0x0800, ipv4_dst=dst_ip)
        self.add_flow(dp, priority, match, [parser.OFPActionOutput(out_port)])

    def install_rules(self, dp, path_name):
        """Install (or overwrite) forwarding rules on one switch."""
        path = PATHS[path_name]
        if dp.id == 1:
            for ip, port in S1_HOSTS.items():
                self.ip_rule(dp, ip, port)
            for ip in S4_HOSTS:
                self.ip_rule(dp, ip, path['s1_out'])
        elif dp.id == 4:
            for ip, port in S4_HOSTS.items():
                self.ip_rule(dp, ip, port)
            for ip in S1_HOSTS:
                self.ip_rule(dp, ip, path['s4_out'])
        elif dp.id in (2, 3):
            # Middle switches: port 1 goes to s1, port 2 goes to s4
            for ip in S4_HOSTS:
                self.ip_rule(dp, ip, 2)
            for ip in S1_HOSTS:
                self.ip_rule(dp, ip, 1)

    def set_path(self, path_name):
        """Move all traffic onto the given path (used for rerouting in D2)."""
        if path_name == self.active_path:
            return
        self.logger.info('*** Rerouting: Path %s -> Path %s',
                         self.active_path, path_name)
        self.active_path = path_name
        for dpid in (1, 4):
            dp = self.datapaths.get(dpid)
            if dp:
                self.install_rules(dp, path_name)

    # ------------------------------------------------------------------ #
    # Network view: port statistics
    # ------------------------------------------------------------------ #
    def _monitor(self):
        while True:
            dp = self.datapaths.get(1)
            if dp:
                parser, ofp = dp.ofproto_parser, dp.ofproto
                dp.send_msg(parser.OFPPortStatsRequest(dp, 0, ofp.OFPP_ANY))
            hub.sleep(POLL_INTERVAL)

    @set_ev_cls(ofp_event.EventOFPPortStatsReply, MAIN_DISPATCHER)
    def port_stats_reply_handler(self, ev):
        if ev.msg.datapath.id != 1:
            return
        for stat in ev.msg.body:
            path_name = PORT_TO_PATH.get(stat.port_no)
            if path_name is None:
                continue
            prev = self.prev_tx_bytes.get(stat.port_no)
            self.prev_tx_bytes[stat.port_no] = stat.tx_bytes
            if prev is None:
                continue

            mbps = (stat.tx_bytes - prev) * 8 / POLL_INTERVAL / 1e6
            util = mbps / LINK_CAPACITY_MBPS
            self.path_util[path_name] = util
            status = 'HIGH' if util >= UTIL_THRESHOLD else 'ok'
            active = '(active)' if path_name == self.active_path else ''
            self.logger.info('Path %s %-8s load=%6.2f Mbps util=%5.1f%%  %s',
                             path_name, active, mbps, util * 100, status)
        self._evaluate()

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
                flow = report['flow']
                loss = float(report['loss_pct'])
                thr = float(report['thr_mbps'])
                delay = float(report['delay_ms'])
            except (ValueError, KeyError, UnicodeDecodeError) as e:
                self.logger.warning('Bad QoS report ignored: %s', e)
                continue
            except OSError as e:
                self.logger.error('QoS socket error: %s', e)
                hub.sleep(1)
                continue
            self.qos_reports[flow] = (time.time(), report)
            self.logger.info('QoS report from %s: flow %s thr=%.2f Mbps '
                             'loss=%.1f%% delay=%.1f ms',
                             addr[0], flow, thr, loss, delay)

    def _worst_app_loss(self):
        now = time.time()
        losses = [r['loss_pct'] for t, r in self.qos_reports.values()
                  if now - t <= REPORT_MAX_AGE]
        return max(losses) if losses else 0.0

    # ------------------------------------------------------------------ #
    # Combined congestion decision
    # ------------------------------------------------------------------ #
    def _evaluate(self):
        util = self.path_util[self.active_path]
        loss = self._worst_app_loss()
        net_congested = util >= UTIL_THRESHOLD
        app_congested = loss >= LOSS_THRESHOLD
        congested = net_congested or app_congested

        if congested and not self.congested:
            reasons = []
            if net_congested:
                reasons.append(f'link util {util*100:.0f}% >= {UTIL_THRESHOLD*100:.0f}%')
            if app_congested:
                reasons.append(f'app loss {loss:.1f}% >= {LOSS_THRESHOLD:.0f}%')
            self.logger.warning('*** CONGESTION DETECTED on Path %s (%s)',
                                self.active_path, ', '.join(reasons))
            # D2: self.set_path('B' if self.active_path == 'A' else 'A')
        elif not congested and self.congested:
            self.logger.info('*** Congestion cleared on Path %s', self.active_path)
        self.congested = congested
