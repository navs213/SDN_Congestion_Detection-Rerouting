#!/usr/bin/env python3
"""
UDP receiver (server) for Project 27.

- One UDP socket receives from any number of senders at once.
- Every packet carries an app-level header: magic, sender id, sequence number,
  send timestamp (see udp_sender.py).
- Per-sender stats: packets, throughput, loss, out-of-order, delay, jitter.
- A separate reporter thread prints stats every --interval seconds; the main
  thread only receives, so reporting never blocks reception.
- With --report-to, every report is also sent as a JSON QoS message over a
  second UDP socket to the SDN controller (socket -> SDN integration).

QoS report format (one datagram per flow, UTF-8 JSON):
  {"type": "qos", "flow": "10.0.0.1#1", "thr_mbps": 1.98, "loss_pct": 0.0,
   "delay_ms": 8.4, "jitter_ms": 0.2, "ts": 1761300000.0}

Usage (on h3):
  python3 udp_receiver.py --port 5001
  python3 udp_receiver.py --port 5001 --csv results.csv
  python3 udp_receiver.py --port 5001 --report-to 192.168.100.1:9000
"""

import argparse
import csv
import json
import socket
import struct
import threading
import time

HEADER = struct.Struct('!4sHId')    # magic, sender_id, seq, send_ts
MAGIC = b'JKFT'


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
        self.interval_bytes = 0      # reset each report
        self.mark_seq = None         # max_seq at the last report
        self.mark_received = 0       # received at the last report
        self.interval_delay_sum = 0.0
        self.interval_count = 0

    def interval_loss_pct(self):
        """Loss since the previous report (reacts faster than total loss)."""
        start = self.first_seq - 1 if self.mark_seq is None else self.mark_seq
        expected = self.max_seq - start
        got = self.received - self.mark_received
        self.mark_seq = self.max_seq
        self.mark_received = self.received
        return 0.0 if expected <= 0 else max(0, expected - got) * 100 / expected

    def update(self, seq, send_ts, nbytes, now):
        if self.first_seq is None:
            self.first_seq = seq
        if seq < self.max_seq:
            self.out_of_order += 1
        self.max_seq = max(self.max_seq, seq)
        self.received += 1
        self.bytes += nbytes
        self.interval_bytes += nbytes

        transit = now - send_ts      # hosts share one clock inside Mininet
        self.delay_sum += transit
        self.interval_delay_sum += transit
        self.interval_count += 1
        if self.last_transit is not None:
            d = abs(transit - self.last_transit)
            self.jitter += (d - self.jitter) / 16
        self.last_transit = transit

    def interval_delay_ms(self):
        """Average one-way delay since the previous report."""
        d = (self.interval_delay_sum / self.interval_count * 1000
             if self.interval_count else 0.0)
        self.interval_delay_sum = 0.0
        self.interval_count = 0
        return d

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


class Receiver:
    def __init__(self, port, interval, csv_path, report_to=None):
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
        if csv_path:
            self.csv_file = open(csv_path, 'w', newline='')
            self.csv_writer = csv.writer(self.csv_file)
            self.csv_writer.writerow(['time_s', 'flow', 'throughput_mbps',
                                      'loss_pct', 'interval_loss_pct',
                                      'delay_ms', 'jitter_ms',
                                      'received', 'out_of_order'])
        self.report_to = report_to
        self.report_sock = None
        if report_to:
            self.report_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            print(f'[receiver] sending QoS reports to {report_to[0]}:{report_to[1]}')
        print(f'[receiver] listening on UDP port {port}')

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
            if magic != MAGIC:
                self.malformed += 1
                continue
            key = (addr[0], sender_id)
            with self.lock:
                flow = self.flows.get(key)
                if flow is None:
                    flow = self.flows[key] = FlowStats()
                    print(f'[receiver] new flow from {addr[0]} (sender id {sender_id})')
                flow.update(seq, send_ts, len(data), now)

    def report_loop(self):
        while self.running:
            time.sleep(self.interval)
            self.report()

    def report(self, final=False):
        elapsed = time.time() - self.start_time
        with self.lock:
            title = 'FINAL' if final else f't={elapsed:6.1f}s'
            print(f'\n--- {title} ---')
            if not self.flows:
                print('  (no traffic yet)')
            for (ip, sid), f in sorted(self.flows.items()):
                mbps = f.interval_bytes * 8 / self.interval / 1e6
                name = f'{ip}#{sid}'
                if final:
                    print(f'  {name:16s} total loss={f.loss_pct:5.1f}%  '
                          f'delay={f.avg_delay_ms:7.2f} ms  jitter={f.jitter*1000:6.2f} ms  '
                          f'rx={f.received} ooo={f.out_of_order}')
                    continue
                iloss = f.interval_loss_pct()
                idelay = f.interval_delay_ms()
                print(f'  {name:16s} thr={mbps:6.2f} Mbps  loss={iloss:5.1f}% '
                      f'(total {f.loss_pct:4.1f}%)  delay={idelay:7.2f} ms  '
                      f'jitter={f.jitter*1000:6.2f} ms  rx={f.received} ooo={f.out_of_order}')
                if self.csv_writer:
                    self.csv_writer.writerow([round(elapsed, 1), name, round(mbps, 3),
                                              round(f.loss_pct, 2), round(iloss, 2),
                                              round(idelay, 3),
                                              round(f.jitter * 1000, 3), f.received,
                                              f.out_of_order])
                self.send_qos(name, mbps, iloss, idelay, f)
                f.interval_bytes = 0
            if self.malformed:
                print(f'  malformed packets dropped: {self.malformed}')
            if self.csv_file:
                self.csv_file.flush()

    def send_qos(self, name, mbps, loss, delay_ms, f):
        if not self.report_sock:
            return
        msg = {'type': 'qos', 'flow': name, 'thr_mbps': round(mbps, 3),
               'loss_pct': round(loss, 2), 'delay_ms': round(delay_ms, 3),
               'jitter_ms': round(f.jitter * 1000, 3), 'ts': time.time()}
        try:
            self.report_sock.sendto(json.dumps(msg).encode(), self.report_to)
        except OSError as e:          # controller unreachable: keep receiving
            print(f'[receiver] QoS report not sent: {e}')

    def run(self):
        reporter = threading.Thread(target=self.report_loop, daemon=True)
        reporter.start()
        try:
            self.receive_loop()
        except KeyboardInterrupt:
            pass
        finally:
            self.running = False
            self.report(final=True)
            self.sock.close()
            if self.report_sock:
                self.report_sock.close()
            if self.csv_file:
                self.csv_file.close()


def main():
    p = argparse.ArgumentParser(description='UDP receiver with QoS stats')
    p.add_argument('--port', type=int, default=5001)
    p.add_argument('--interval', type=float, default=2.0, help='report interval (s)')
    p.add_argument('--csv', help='write per-interval stats to this CSV file')
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
    Receiver(args.port, args.interval, args.csv, report_to).run()


if __name__ == '__main__':
    main()
