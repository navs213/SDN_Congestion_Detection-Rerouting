#!/usr/bin/env python3
"""
Automated experiments for Project 27 (performance evaluation, D2).

Builds the Mininet topology, then runs every experiment twice - once with the
controller in STATIC mode (baseline: detect only) and once in DYNAMIC mode
(rerouting + failover) - under identical conditions:

  congestion-7   h4 floods 7 Mbps for 20 s   (4 + 7 = 11 Mbps on a 10 Mbps path)
  congestion-9   h4 floods 9 Mbps for 20 s   (13 Mbps offered)
  congestion-12  h4 floods 12 Mbps for 20 s  (16 Mbps offered)
  failure        link s1-s2 (Path A) goes down for 15 s
  double-failure link s1-s2 (Path A) goes down, 7 s later link s1-s3 (Path B)
                 too, so traffic must end up on Path C; both come back at 15 s

In every run h1 and h2 each send 2 Mbps of UDP to h3 for 45 s, and the event
starts 15 s in. The receiver on h3 writes one CSV per run; the controller's
events (detections, reroutes, recoveries) are saved next to it.

Before running:
  Terminal 1:  ryu-manager controller.py        (in your Ryu virtual environment)
  Terminal 2:  sudo mn -c
               sudo python3 run_experiments.py           # all 10 runs, ~11 min
               sudo python3 run_experiments.py --quick   # 4 runs, ~4.5 min
Then:          python3 analyze.py results
"""

import argparse
import json
import os
import socket
import sys
import time

from mininet.log import setLogLevel, info
from mininet.net import Mininet
from mininet.node import OVSSwitch, RemoteController
from mininet.link import TCLink

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from topology import MultiPathTopo, add_management_link   # noqa: E402

STATE_FILE = os.environ.get('SDN_STATE_FILE', '/tmp/sdn_live_state.json')
CMD_ADDR = ('127.0.0.1', 9000)
SENDER = os.path.join(HERE, 'udp_sender.py')
RECEIVER = os.path.join(HERE, 'udp_receiver.py')

APP_RATE = 2.0          # Mbps per application sender (h1, h2)
RUN_LENGTH = 45         # seconds the application senders run
EVENT_AT = 15           # seconds into the run when the problem starts
FLOOD_LENGTH = 20       # seconds of background flood
FAILURE_LENGTH = 15     # seconds the link stays down
SETTLE = 8              # pause between runs

CORE = [('congestion', 9), ('failure', None)]
EXTRA = [('congestion', 7), ('congestion', 12), ('double-failure', None)]
SECOND_FAILURE_AFTER = 7    # double-failure: seconds until Path B fails too


def cmd(msg):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.sendto(json.dumps(msg).encode(), CMD_ADDR)
    s.close()


def read_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def controller_alive():
    st = read_state()
    return st is not None and time.time() - st['ts'] < 5


def run_one(net, out_dir, scenario, rate, mode, index, total):
    name = f'{scenario}-{rate}' if rate else scenario
    tag = f'{name}_{mode}'
    h1, h2, h3, h4, h5 = (net.get(h) for h in ('h1', 'h2', 'h3', 'h4', 'h5'))
    info(f'\n=== [{index}/{total}] {tag} ===\n')

    cmd({'type': 'cmd', 'cmd': 'mode', 'mode': mode})
    time.sleep(2)
    cmd({'type': 'cmd', 'cmd': 'mark', 'text': f'Experiment {tag}: start'})

    csv_path = os.path.join(out_dir, f'{tag}.csv')
    logs = {n: open(os.path.join(out_dir, f'{tag}_{n}.log'), 'w')
            for n in ('h3', 'h5', 'h1', 'h2', 'h4')}
    procs = []
    t_start = time.time()
    rx = h3.popen(['python3', '-u', RECEIVER, '--port', '5001', '--interval', '1',
                   '--csv', csv_path, '--report-to', '192.168.100.1:9000'],
                  stdout=logs['h3'], stderr=logs['h3'])
    sink = h5.popen(['python3', '-u', RECEIVER, '--port', '5001', '--interval', '1'],
                    stdout=logs['h5'], stderr=logs['h5'])
    time.sleep(1)
    for host, sid in ((h1, 1), (h2, 2)):
        procs.append(host.popen(['python3', '-u', SENDER, '--dst', '10.0.0.3',
                                 '--rate', str(APP_RATE), '--duration', str(RUN_LENGTH),
                                 '--id', str(sid)],
                                stdout=logs[host.name], stderr=logs[host.name]))

    time.sleep(max(0, t_start + EVENT_AT - time.time()))
    t_event = time.time()
    if scenario == 'congestion':
        info(f'  t={EVENT_AT}s: h4 floods {rate} Mbps for {FLOOD_LENGTH} s\n')
        cmd({'type': 'cmd', 'cmd': 'mark', 'text': f'{tag}: flood {rate} Mbps starts'})
        procs.append(h4.popen(['python3', '-u', SENDER, '--dst', '10.0.0.5',
                               '--rate', str(rate), '--duration', str(FLOOD_LENGTH),
                               '--id', '99'], stdout=logs['h4'], stderr=logs['h4']))
        event_len = FLOOD_LENGTH
        time.sleep(FLOOD_LENGTH)
    elif scenario == 'failure':
        info(f'  t={EVENT_AT}s: link s1-s2 DOWN for {FAILURE_LENGTH} s\n')
        cmd({'type': 'cmd', 'cmd': 'mark', 'text': f'{tag}: link s1-s2 down'})
        net.configLinkStatus('s1', 's2', 'down')
        time.sleep(FAILURE_LENGTH)
        net.configLinkStatus('s1', 's2', 'up')
        cmd({'type': 'cmd', 'cmd': 'mark', 'text': f'{tag}: link s1-s2 up'})
        info('  link s1-s2 UP\n')
        event_len = FAILURE_LENGTH
    else:   # double-failure
        info(f'  t={EVENT_AT}s: link s1-s2 DOWN, +{SECOND_FAILURE_AFTER}s link s1-s3 DOWN\n')
        cmd({'type': 'cmd', 'cmd': 'mark', 'text': f'{tag}: link s1-s2 down'})
        net.configLinkStatus('s1', 's2', 'down')
        time.sleep(SECOND_FAILURE_AFTER)
        cmd({'type': 'cmd', 'cmd': 'mark', 'text': f'{tag}: link s1-s3 down'})
        net.configLinkStatus('s1', 's3', 'down')
        time.sleep(max(0, FAILURE_LENGTH - SECOND_FAILURE_AFTER))
        net.configLinkStatus('s1', 's2', 'up')
        net.configLinkStatus('s1', 's3', 'up')
        cmd({'type': 'cmd', 'cmd': 'mark', 'text': f'{tag}: links up'})
        info('  links s1-s2 and s1-s3 UP\n')
        event_len = FAILURE_LENGTH
    t_event_end = time.time()

    for p in procs:
        p.wait()
    time.sleep(2)
    for p in (rx, sink):
        p.terminate()
        p.wait()
    for f in logs.values():
        f.close()
    t_end = time.time()

    st = read_state() or {}
    meta = {
        'name': name, 'tag': tag, 'scenario': scenario, 'mode': mode,
        'flood_mbps': rate, 'app_rate_mbps': APP_RATE, 'flows': 2,
        't_start': t_start, 't_event': t_event, 't_event_end': t_event_end,
        'event_length_s': event_len, 't_end': t_end, 'csv': os.path.basename(csv_path),
        'controller_events': [e for e in st.get('events', []) if t_start <= e['t'] <= t_end],
        'controller_recoveries': [r for r in st.get('recoveries', [])
                                  if t_start <= r['start'] <= t_end],
    }
    with open(os.path.join(out_dir, f'{tag}.json'), 'w') as f:
        json.dump(meta, f, indent=2)
    recs = ', '.join(f"{r['recovery_s']} s" for r in meta['controller_recoveries']) or 'none'
    info(f'  done. controller-measured recovery: {recs}\n')


def main():
    ap = argparse.ArgumentParser(description='Run static vs dynamic experiments')
    ap.add_argument('--quick', action='store_true', help='only congestion-9 and failure')
    ap.add_argument('--out', default=os.path.join(HERE, 'results'))
    ap.add_argument('--bw', type=float, default=10)
    args = ap.parse_args()

    if os.geteuid() != 0:
        sys.exit('Run with sudo: sudo python3 run_experiments.py')
    if not controller_alive():
        sys.exit('The controller is not running (no fresh state in %s).\n'
                 'Start it first: ryu-manager controller.py' % STATE_FILE)

    os.makedirs(args.out, exist_ok=True)
    setLogLevel('info')
    net = Mininet(topo=MultiPathTopo(path_bw=args.bw), switch=OVSSwitch, link=TCLink,
                  controller=None, autoSetMacs=False)
    net.addController('c0', controller=RemoteController, ip='127.0.0.1', port=6653)
    add_management_link(net)
    net.start()
    net.staticArp()
    net.waitConnected()
    time.sleep(2)
    loss = net.pingAll(timeout='1')
    if loss > 0:
        info('*** WARNING: pingAll lost packets - check the controller\n')

    plan = CORE + ([] if args.quick else EXTRA)
    runs = [(s, r, m) for s, r in plan for m in ('static', 'dynamic')]
    try:
        for i, (scenario, rate, mode) in enumerate(runs, 1):
            run_one(net, args.out, scenario, rate, mode, i, len(runs))
            time.sleep(SETTLE)
    finally:
        cmd({'type': 'cmd', 'cmd': 'mode', 'mode': 'dynamic'})
        net.stop()
        uid, gid = os.environ.get('SUDO_UID'), os.environ.get('SUDO_GID')
        if uid and gid:   # give the results back to the normal user
            for root, dirs, files in os.walk(args.out):
                for n in [root] + [os.path.join(root, f) for f in files]:
                    os.chown(n, int(uid), int(gid))
    info(f'\n*** All runs finished. Results in {args.out}\n'
         f'*** Next: python3 analyze.py {args.out}\n')


if __name__ == '__main__':
    main()
