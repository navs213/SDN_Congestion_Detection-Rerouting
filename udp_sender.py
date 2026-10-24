#!/usr/bin/env python3
"""
UDP sender (client) for Project 27.

Sends fixed-size UDP datagrams at a controlled rate. Each datagram starts with
an application header the receiver uses to measure loss, delay and jitter:

  magic (4 bytes 'JKFT') | sender_id (uint16) | seq (uint32) | send_ts (float64)

Usage (on h1 / h2):
  python3 udp_sender.py --dst 10.0.0.3 --rate 2 --duration 30 --id 1
  python3 udp_sender.py --dst 10.0.0.3 --rate 2 --duration 30 --id 2
Background congestion (on h4, toward h5):
  python3 udp_sender.py --dst 10.0.0.5 --rate 9 --duration 60 --id 99
"""

import argparse
import socket
import struct
import time

HEADER = struct.Struct('!4sHId')
MAGIC = b'JKFT'


def main():
    p = argparse.ArgumentParser(description='Rate-controlled UDP sender')
    p.add_argument('--dst', required=True, help='receiver IP address')
    p.add_argument('--port', type=int, default=5001)
    p.add_argument('--rate', type=float, default=1.0, help='send rate in Mbps')
    p.add_argument('--size', type=int, default=1000, help='datagram size in bytes')
    p.add_argument('--duration', type=float, default=20.0, help='seconds to send')
    p.add_argument('--id', type=int, default=1, help='sender id (0-65535)')
    args = p.parse_args()

    if args.size < HEADER.size:
        p.error(f'--size must be at least {HEADER.size} bytes')
    if args.rate <= 0:
        p.error('--rate must be positive')

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    padding = b'x' * (args.size - HEADER.size)
    gap = args.size * 8 / (args.rate * 1e6)      # seconds between packets

    print(f'[sender {args.id}] -> {args.dst}:{args.port}  {args.rate} Mbps, '
          f'{args.size} B packets, {args.duration} s')

    seq = sent = errors = 0
    start = time.time()
    next_send = start
    try:
        while time.time() - start < args.duration:
            now = time.time()
            if now < next_send:
                time.sleep(next_send - now)
            packet = HEADER.pack(MAGIC, args.id, seq, time.time()) + padding
            try:
                sock.sendto(packet, (args.dst, args.port))
                sent += 1
            except OSError as e:          # e.g. network unreachable, buffer full
                errors += 1
                if errors <= 5:
                    print(f'[sender {args.id}] send error: {e}')
            seq += 1
            next_send += gap
    except KeyboardInterrupt:
        pass
    finally:
        elapsed = time.time() - start
        sock.close()
        rate = sent * args.size * 8 / elapsed / 1e6 if elapsed > 0 else 0
        print(f'[sender {args.id}] done: sent={sent} errors={errors} '
              f'time={elapsed:.1f}s avg={rate:.2f} Mbps')


if __name__ == '__main__':
    main()
