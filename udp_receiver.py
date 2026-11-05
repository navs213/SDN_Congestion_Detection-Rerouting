#!/usr/bin/env python3
"""
UDP receiver (server) for Project 27.

- One UDP socket receives from any number of senders at once.
- Every packet carries an app-level header: magic, sender id, sequence number,
  send timestamp (see udp_sender.py). Magic 'JKFT' = data, 'JKFE' = the
  sender's end-of-stream marker.
- Per-flow stats every --interval seconds (default 1 s): throughput, loss,
  delay, jitter, out-of-order packets. A reporter thread does this so it never
  blocks reception.
- Stall detection: a flow that goes silent without having sent its
  end-of-stream marker is reported as STALLED = 100% loss for every silent
  interval. That is how a link failure shows up at the application.
- With --report-to, every interval is also sent as a JSON QoS report to the
  SDN controller (socket -> SDN integration).

QoS report format (one datagram per flow, UTF-8 JSON):
  {"type": "qos", "flow": "10.0.0.1#1", "thr_mbps": 1.98, "loss_pct": 0.0,
   "delay_ms": 5.1, "jitter_ms": 0.2, "stalled": false, "ts": 1761300000.0}
  delay_ms / jitter_ms are null when no packet arrived in the interval.

SDN events (UDP --event-port, default 5002, sent by the controller over the
management link) are written to the same CSV:
  link_down, link_up, failover_rules_sent, failback, traffic_recovered
  traffic_recovered = first packet received after a link_down hit a flow;
  recovery_time_ms  = that packet's arrival - controller's link-down detection
  failover_rule_ms / failover_install_ms = detection -> rules sent / confirmed
  event_ts / first_rx_ts = absolute (epoch) timestamps of the trigger and of the
  first packet received afterwards.

CSV columns: unix_ts and time_s (absolute / since start), flow, status
(ok | stalled), throughput_mbps, loss_pct (whole run so far),
interval_loss_pct (this interval), delay_ms, jitter_ms, received, expected,
out_of_order, then the SDN event columns above (blank on normal rows).

Usage (on h3):
  python3 udp_receiver.py --port 5001 --csv results.csv --report-to 192.168.100.1:9000
"""

import argparse
import csv
import json
import signal
import socket
import struct
import threading
import time

HEADER = struct.Struct('!4sHId')    # magic, sender_id, seq, send_ts
MAGIC = b'JKFT'
MAGIC_END = b'JKFE'
STALL_TIMEOUT = 60.0                # a silent flow counts as stalled for this long

CSV_FIELDS = ['unix_ts', 'time_s', 'flow', 'status', 'throughput_mbps', 'loss_pct',
              'interval_loss_pct', 'delay_ms', 'jitter_ms', 'received', 'expected',
              'out_of_order', 'event_type', 'path', 'link_state',
              'failover_rule_ms', 'recovery_time_ms',
              'event_ts', 'first_rx_ts', 'failover_install_ms']


def _r(v, n=3):
    """Round a number for the CSV; None/'' stay blank."""
    return '' if v is None or v == '' else round(float(v), n)


class FlowStats:
    def __init__(self):
        self.received = 0
        self.bytes = 0
        self.first_seq = None
        self.max_seq = -1
        self.out_of_order = 0
        self.delay_sum = 0.0
        self.jitter = 0.0            # RFC 3550 style smoothed jitter (s)
        self.last_transit = None
        self.last_rx = 0.0
        self.finished = False
        self.reported_finished = False
        # per-interval counters (reset at every report)
        self.i_bytes = 0
        self.i_count = 0
        self.i_delay_sum = 0.0
        self.i_min_seq = None
        self.mark_seq = None         # max_seq at the previous report
        self.mark_received = 0
        self.stalled_prev = False

    def update(self, seq, send_ts, nbytes, now):
        if self.first_seq is None:
            self.first_seq = seq
        if seq < self.max_seq:
            self.out_of_order += 1
        self.max_seq = max(self.max_seq, seq)
        self.received += 1
        self.bytes += nbytes
        self.last_rx = now
        transit = now - send_ts      # hosts share one clock inside Mininet
        self.delay_sum += transit
        if self.last_transit is not None:
            self.jitter += (abs(transit - self.last_transit) - self.jitter) / 16
        self.last_transit = transit
        self.i_bytes += nbytes
        self.i_count += 1
        self.i_delay_sum += transit
        self.i_min_seq = seq if self.i_min_seq is None else min(self.i_min_seq, seq)

    @property
    def expected(self):
        return 0 if self.first_seq is None else self.max_seq - self.first_seq + 1

    @property
    def loss_pct(self):
        exp = self.expected
        return 0.0 if exp == 0 else max(0, exp - self.received) * 100 / exp

    @property
    def avg_delay_ms(self):
        return 0.0 if self.received == 0 else self.delay_sum / self.received * 1000

    def close_interval(self, interval, now):
        """Return this interval's stats and reset the interval counters."""
        if self.i_count:
            if self.stalled_prev:
                # Packets resumed after a stall: the stall intervals already
                # counted as 100% loss, so only count gaps from here on.
                start = self.i_min_seq - 1
            elif self.mark_seq is None:
                start = self.first_seq - 1
            else:
                start = self.mark_seq
            expected = self.max_seq - start
            got = self.received - self.mark_received
            loss = 0.0 if expected <= 0 else max(0, expected - got) * 100 / expected
            result = {'status': 'ok', 'thr_mbps': self.i_bytes * 8 / interval / 1e6,
                      'loss_pct': loss, 'delay_ms': self.i_delay_sum / self.i_count * 1000,
                      'jitter_ms': self.jitter * 1000}
            self.stalled_prev = False
        elif self.finished:
            result = {'status': 'finished'}
        elif now - self.last_rx <= STALL_TIMEOUT:
            result = {'status': 'stalled', 'thr_mbps': 0.0, 'loss_pct': 100.0,
                      'delay_ms': None, 'jitter_ms': None}
            self.stalled_prev = True
        else:
            result = {'status': 'idle'}
        self.mark_seq = self.max_seq
        self.mark_received = self.received
        self.i_bytes = self.i_count = 0
        self.i_delay_sum = 0.0
        self.i_min_seq = None
        return result


class Receiver:
    def __init__(self, port, interval, csv_path, report_to=None, event_port=5002):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
        self.sock.bind(('0.0.0.0', port))
        self.sock.settimeout(1.0)
        self.interval = interval
        self.flows = {}              # (ip, sender_id) -> FlowStats
        self.lock = threading.Lock()
        self.malformed = 0
        self.running = True
        self.start_time = time.time()
        self.csv_file = None
        self.csv_writer = None
        self.pending_recovery = {}   # src IP -> dict(detected_at, path, flow, rules_sent_at)
        self.event_port = event_port
        self.event_sock = None
        if csv_path:
            self.csv_file = open(csv_path, 'w', newline='')
            self.csv_writer = csv.writer(self.csv_file)
            self.csv_writer.writerow(CSV_FIELDS)
        self.report_to = report_to
        self.report_sock = None
        if report_to:
            self.report_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            print(f'[receiver] sending QoS reports to {report_to[0]}:{report_to[1]}')
        print(f'[receiver] listening on UDP port {port}, reporting every {interval:g} s',
              flush=True)

    # ------------------------------------------------------------ SDN events
    def event_loop(self):
        """Receive SDN link events from the controller over h3's management link."""
        self.event_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.event_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self.event_sock.bind(('0.0.0.0', self.event_port))
        except OSError as e:
            print(f'[receiver] SDN event port {self.event_port} unavailable: {e}')
            return
        self.event_sock.settimeout(1.0)
        print(f'[receiver] listening for SDN link events on UDP port {self.event_port}')
        while self.running:
            try:
                data, _addr = self.event_sock.recvfrom(4096)
                event = json.loads(data.decode())
            except socket.timeout:
                continue
            except (OSError, ValueError, UnicodeDecodeError):
                if self.running:
                    continue
                break
            if isinstance(event, dict) and event.get('type') == 'sdn_event':
                self.record_sdn_event(event)

    def write_row(self, **kw):
        """Write one CSV row; columns not given stay blank. Caller holds lock."""
        if self.csv_writer:
            kw.setdefault('unix_ts', round(time.time(), 3))
            self.csv_writer.writerow([kw.get(k, '') for k in CSV_FIELDS])
            self.csv_file.flush()

    def record_sdn_event(self, event):
        """Write controller events into the same CSV as receiver QoS samples."""
        now = time.time()
        kind = event.get('event', 'unknown')
        path = event.get('path', '')
        src_ip = event.get('src_ip', '')
        dst_ip = event.get('dst_ip', '')
        flow_name = f'{src_ip}->{dst_ip}' if src_ip else ''
        event_ts = float(event.get('timestamp', now))
        t_detect = event.get('t_detect')        # failover/failback trigger time
        t_sent = event.get('t_rules_sent')
        state = {'link_down': 'DOWN', 'link_up': 'UP',
                 'failback': 'FAILBACK'}.get(kind, '')
        with self.lock:
            if kind == 'link_down' and src_ip:
                self.pending_recovery[src_ip] = {
                    'detected_at': event_ts, 'path': path, 'flow': flow_name,
                    'rules_sent_at': None}
            elif kind == 'failover_rules_sent' and src_ip and t_sent is not None:
                pend = self.pending_recovery.get(src_ip)
                if pend is not None:
                    pend['rules_sent_at'] = float(t_sent)   # only count packets after this
            self.write_row(
                unix_ts=round(now, 3), time_s=round(now - self.start_time, 3),
                flow=flow_name, event_type=kind, path=path, link_state=state,
                failover_rule_ms=_r(event.get('failover_rule_ms')),
                failover_install_ms=_r(event.get('failover_install_ms')),
                event_ts=_r(t_detect if t_detect is not None else event_ts, 6))
        print(f'[receiver] SDN event: {kind} Path {path}', flush=True)

    def record_recovery_if_pending(self, src_ip, rx_time):
        """First received UDP packet after a link-down event marks traffic
        recovery (once the failover rules are known, only a packet that
        arrived after they were sent counts). Caller holds the lock."""
        pend = self.pending_recovery.get(src_ip)
        if pend is None:
            return
        if pend['rules_sent_at'] is not None and rx_time < pend['rules_sent_at']:
            return
        del self.pending_recovery[src_ip]
        recovery_ms = max(0.0, (rx_time - pend['detected_at']) * 1000)
        print(f"[receiver] TRAFFIC RECOVERED: {pend['flow']} "
              f"(recovery={recovery_ms:.1f} ms)", flush=True)
        self.write_row(
            unix_ts=round(rx_time, 3), time_s=round(rx_time - self.start_time, 3),
            flow=pend['flow'], event_type='traffic_recovered', path=pend['path'],
            link_state='RECOVERED', recovery_time_ms=round(recovery_ms, 3),
            event_ts=_r(pend['detected_at'], 6), first_rx_ts=_r(rx_time, 6))

    # ------------------------------------------------------------ data
    def receive_loop(self):
        while self.running:
            try:
                data, addr = self.sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError as e:
                print(f'[receiver] socket error: {e}')
                continue
            now = time.time()
            if len(data) < HEADER.size:
                self.malformed += 1
                continue
            magic, sender_id, seq, send_ts = HEADER.unpack_from(data)
            key = (addr[0], sender_id)
            with self.lock:
                if magic == MAGIC_END:
                    flow = self.flows.get(key)
                    if flow and not flow.finished:
                        flow.finished = True
                        print(f'[receiver] flow {addr[0]}#{sender_id} finished', flush=True)
                    continue
                if magic != MAGIC:
                    self.malformed += 1
                    continue
                self.record_recovery_if_pending(addr[0], now)
                flow = self.flows.get(key)
                if flow is None or flow.finished:
                    flow = self.flows[key] = FlowStats()
                    print(f'[receiver] new flow from {addr[0]} (sender id {sender_id})',
                          flush=True)
                flow.update(seq, send_ts, len(data), now)

    def report_loop(self):
        next_t = time.time() + self.interval
        while self.running:
            time.sleep(max(0.0, next_t - time.time()))
            next_t += self.interval
            if self.running:
                self.report()

    def report(self, final=False):
        now = time.time()
        elapsed = now - self.start_time
        lines = [f'\n--- {"FINAL" if final else f"t={elapsed:6.1f}s"} ---']
        with self.lock:
            if not self.flows:
                lines.append('  (no traffic yet)')
            for (ip, sid), f in sorted(self.flows.items()):
                name = f'{ip}#{sid}'
                if final:
                    lines.append(f'  {name:16s} total loss={f.loss_pct:5.1f}%  '
                                 f'delay={f.avg_delay_ms:7.2f} ms  jitter={f.jitter*1000:6.2f} ms  '
                                 f'rx={f.received} ooo={f.out_of_order}')
                    continue
                r = f.close_interval(self.interval, now)
                if r['status'] in ('finished', 'idle'):
                    if r['status'] == 'finished' and not f.reported_finished:
                        f.reported_finished = True
                        lines.append(f'  {name:16s} finished (total loss {f.loss_pct:.1f}%)')
                    continue
                if r['status'] == 'stalled':
                    lines.append(f'  {name:16s} STALLED - no packets (counted as 100% loss)')
                else:
                    lines.append(f'  {name:16s} thr={r["thr_mbps"]:6.2f} Mbps  '
                                 f'loss={r["loss_pct"]:5.1f}% (total {f.loss_pct:4.1f}%)  '
                                 f'delay={r["delay_ms"]:8.2f} ms  jitter={r["jitter_ms"]:6.2f} ms  '
                                 f'rx={f.received} ooo={f.out_of_order}')
                self.write_row(unix_ts=round(now, 3), time_s=round(elapsed, 2), flow=name,
                               status=r['status'], throughput_mbps=round(r['thr_mbps'], 3),
                               loss_pct=round(f.loss_pct, 2),
                               interval_loss_pct=round(r['loss_pct'], 2),
                               delay_ms=_r(r['delay_ms']), jitter_ms=_r(r['jitter_ms']),
                               received=f.received, expected=f.expected,
                               out_of_order=f.out_of_order)
                self.send_qos(name, r, now)
            if self.malformed:
                lines.append(f'  malformed packets dropped: {self.malformed}')
            if self.csv_file:
                self.csv_file.flush()
        print('\n'.join(lines), flush=True)

    def send_qos(self, name, r, now):
        if not self.report_sock:
            return
        msg = {'type': 'qos', 'flow': name, 'thr_mbps': round(r['thr_mbps'], 3),
               'loss_pct': round(r['loss_pct'], 2),
               'delay_ms': None if r['delay_ms'] is None else round(r['delay_ms'], 3),
               'jitter_ms': None if r['jitter_ms'] is None else round(r['jitter_ms'], 3),
               'stalled': r['status'] == 'stalled', 'ts': now}
        try:
            self.report_sock.sendto(json.dumps(msg).encode(), self.report_to)
        except OSError as e:          # controller unreachable: keep receiving
            print(f'[receiver] QoS report not sent: {e}')

    def run(self):
        # Treat `kill`/`pkill` (SIGTERM) like Ctrl+C so FINAL stats are printed.
        signal.signal(signal.SIGTERM, _raise_interrupt)
        threading.Thread(target=self.report_loop, daemon=True).start()
        threading.Thread(target=self.event_loop, daemon=True).start()
        try:
            self.receive_loop()
        except KeyboardInterrupt:
            pass
        finally:
            self.running = False
            self.report(final=True)
            self.sock.close()
            if self.event_sock:
                self.event_sock.close()
            if self.report_sock:
                self.report_sock.close()
            if self.csv_file:
                self.csv_file.close()


def _raise_interrupt(signum, frame):
    raise KeyboardInterrupt


def main():
    p = argparse.ArgumentParser(description='UDP receiver with QoS stats')
    p.add_argument('--port', type=int, default=5001)
    p.add_argument('--interval', type=float, default=1.0, help='report interval (s)')
    p.add_argument('--csv', help='write per-interval stats to this CSV file')
    p.add_argument('--event-port', type=int, default=5002,
                   help='UDP port for controller link events (default 5002)')
    p.add_argument('--report-to', metavar='IP:PORT',
                   help='send QoS reports to the controller, e.g. 192.168.100.1:9000')
    args = p.parse_args()
    report_to = None
    if args.report_to:
        try:
            ip, port = args.report_to.rsplit(':', 1)
            report_to = (ip, int(port))
        except ValueError:
            p.error('--report-to must look like IP:PORT')
    Receiver(args.port, args.interval, args.csv, report_to, args.event_port).run()


if __name__ == '__main__':
    main()
