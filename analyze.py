#!/usr/bin/env python3
"""
Performance evaluation for Project 27: static (baseline) vs dynamic rerouting.

Reads the results written by run_experiments.py and produces:
  results/summary.csv            one row per run with every metric
  results/RESULTS.md             comparison tables, ready for the report
  results/plot_<scenario>.png    loss, delay and throughput over time,
                                 static vs dynamic, for each scenario
  results/comparison.png         recovery time and loss during the event,
                                 static vs dynamic, for every condition

Metrics (application flows h1->h3 and h2->h3, time 0 = the moment the
problem was triggered):
  baseline loss/delay     the 10 s before the event
  loss during event       mean per-second loss from the event start until it ends
  mean / max delay        over the same window (seconds with packets only)
  throughput              mean per flow over the same window (Mbps)
  outage                  seconds in which a flow received nothing
  recovery time           from the event start until the first moment both
                          flows are healthy again (loss < 1%, delay < 20 ms)
                          for 2 seconds in a row, after the problem appeared;
                          0 if the application never saw a problem
  reroutes                path changes made by the controller during the run

Usage:  python3 analyze.py results
Needs:  sudo apt install python3-matplotlib   (for the plots)
"""

import csv
import glob
import math
import json
import os
import sys

HEALTHY_LOSS = 1.0
HEALTHY_DELAY_MS = 20.0
HEALTHY_RUN = 2
APP_FLOWS = ('10.0.0.1#1', '10.0.0.2#2')


def load_run(meta_path):
    with open(meta_path) as f:
        meta = json.load(f)
    rows, events = [], []
    with open(os.path.join(os.path.dirname(meta_path), meta['csv'])) as f:
        for r in csv.DictReader(f):
            if r.get('event_type'):
                events.append(r)
                continue
            if r['flow'] not in APP_FLOWS:
                continue
            rows.append({
                'rel': float(r['unix_ts']) - meta['t_event'],
                'flow': r['flow'], 'status': r['status'],
                'thr': float(r['throughput_mbps']), 'loss': float(r['interval_loss_pct']),
                'delay': float(r['delay_ms']) if r['delay_ms'] else None,
            })
    meta['sdn_events'] = events
    return meta, rows


def per_second(rows):
    """Group rows into seconds: key t = the report covering (t-1, t] after the event."""
    secs = {}
    for r in rows:
        secs.setdefault(math.ceil(r['rel'] - 1e-6), {})[r['flow']] = r
    return dict(sorted(secs.items()))


def healthy(sec_rows):
    return (len(sec_rows) == len(APP_FLOWS) and
            all(r['status'] == 'ok' and r['loss'] < HEALTHY_LOSS and
                r['delay'] is not None and r['delay'] < HEALTHY_DELAY_MS
                for r in sec_rows.values()))


def metrics(meta, rows):
    secs = per_second(rows)
    L = meta['event_length_s']
    before = [r for r in rows if -10 <= r['rel'] < 0]
    during = [r for r in rows if 0 < r['rel'] <= L + 1]
    mean = lambda xs: sum(xs) / len(xs) if xs else float('nan')

    # recovery: first unhealthy second after the event, then first healthy run
    times = [t for t in secs if t > 0]
    onset = next((t for t in times if not healthy(secs[t])), None)
    recovery = 0.0
    if onset is not None:
        recovery = None
        for t in times:
            if t < onset:
                continue
            run = [secs.get(t + k) for k in range(HEALTHY_RUN)]
            if all(s is not None and healthy(s) for s in run):
                recovery = float(max(0, t - 1))   # row at t covers (t-1, t]
                break

    reroutes = [e for e in meta['controller_events']
                if e['kind'] == 'reroute' and 'APP' in e['text'] and 'mode change' not in e['text']]
    ctrl = meta['controller_recoveries']
    ev = meta.get('sdn_events', [])
    first = lambda kind, col: next((float(e[col]) for e in ev
                                    if e['event_type'] == kind and e.get(col)), None)
    return {
        'run': meta['tag'], 'scenario': meta['name'], 'mode': meta['mode'],
        'event_s': L,
        'baseline_loss_pct': round(mean([r['loss'] for r in before]), 2),
        'baseline_delay_ms': round(mean([r['delay'] for r in before if r['delay'] is not None]), 2),
        'loss_during_pct': round(mean([r['loss'] for r in during]), 2),
        'mean_delay_ms': round(mean([r['delay'] for r in during if r['delay'] is not None]), 1),
        'max_delay_ms': round(max([r['delay'] for r in during if r['delay'] is not None]
                                  or [float('nan')]), 1),
        'throughput_mbps': round(mean([r['thr'] for r in during]), 3),
        'outage_s': sum(1 for r in during if r['status'] == 'stalled') / len(APP_FLOWS),
        'recovery_s': None if recovery is None else round(recovery, 1),
        'controller_recovery_s': ctrl[0]['recovery_s'] if ctrl else None,
        'reroutes': len(reroutes),
        'failover_install_ms': first('failover_rules_sent', 'failover_install_ms'),
        'first_packet_after_failure_ms': first('traffic_recovered', 'recovery_time_ms'),
    }


def fmt(v, unit=''):
    if v is None and unit == ' s':
        return 'not recovered'
    if v is None or (isinstance(v, float) and v != v):
        return '-'
    return f'{v}{unit}'


def write_reports(out_dir, results):
    keys = list(results[0].keys())
    with open(os.path.join(out_dir, 'summary.csv'), 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        w.writerows(results)

    by = {(r['scenario'], r['mode']): r for r in results}
    scenarios = sorted({r['scenario'] for r in results},
                       key=lambda s: ('failure' in s, len(s), s))
    lines = ['# Results: static vs dynamic', '',
             'Application traffic: h1 and h2 each send 2 Mbps of UDP to h3 over 10 Mbps paths. '
             'Time 0 is when the problem starts (a background flood from h4, or link s1-s2 going down; in double-failure s1-s3 follows 7 s later). '
             'Metrics cover the application flows from time 0 until the problem ends.', '',
             '| Scenario | Mode | Loss during event | Mean delay | Max delay | Throughput / flow '
             '| Outage | Recovery time | Reroutes |',
             '| --- | --- | --- | --- | --- | --- | --- | --- | --- |']
    for s in scenarios:
        for m in ('static', 'dynamic'):
            r = by.get((s, m))
            if not r:
                continue
            lines.append(f"| {s} | {m} | {fmt(r['loss_during_pct'], '%')} | "
                         f"{fmt(r['mean_delay_ms'], ' ms')} | {fmt(r['max_delay_ms'], ' ms')} | "
                         f"{fmt(r['throughput_mbps'], ' Mbps')} | {fmt(r['outage_s'], ' s')} | "
                         f"{fmt(r['recovery_s'], ' s')} | {r['reroutes']} |")
    fo = [r for r in results if r['failover_install_ms'] is not None
          or r['first_packet_after_failure_ms'] is not None]
    if fo:
        lines += ['', '## Link failure: controller and data-plane timing', '',
                  '| Run | Failover rules installed after | First packet after the failure |',
                  '| --- | --- | --- |']
        for r in fo:
            ms = lambda v: '-' if v is None else f'{v:.1f} ms'
            lines.append(f"| {r['run']} | {ms(r['failover_install_ms'])} | "
                         f"{ms(r['first_packet_after_failure_ms'])} |")
        lines += ['', 'Measured from the moment the controller received the link-down '
                  'port-status message (OpenFlow barrier reply = rules installed; '
                  'first packet = first application packet received by h3 afterwards).']
    lines += ['', '## Improvement with dynamic rerouting', '',
              '| Scenario | Loss: static → dynamic | Recovery: static → dynamic |',
              '| --- | --- | --- |']
    for s in scenarios:
        st, dy = by.get((s, 'static')), by.get((s, 'dynamic'))
        if st and dy:
            lines.append(f"| {s} | {fmt(st['loss_during_pct'], '%')} → {fmt(dy['loss_during_pct'], '%')} | "
                         f"{fmt(st['recovery_s'], ' s')} → {fmt(dy['recovery_s'], ' s')} |")
    lines += ['', 'Healthy = loss below 1% and delay below 20 ms for 2 seconds in a row. '
              'Baseline before the event: loss '
              f"{min(r['baseline_loss_pct'] for r in results)}–{max(r['baseline_loss_pct'] for r in results)}%, "
              f"delay {min(r['baseline_delay_ms'] for r in results)}–{max(r['baseline_delay_ms'] for r in results)} ms.",
              '', 'Plots: `plot_<scenario>.png` (over time) and `comparison.png` (summary).']
    with open(os.path.join(out_dir, 'RESULTS.md'), 'w') as f:
        f.write('\n'.join(lines) + '\n')
    return scenarios, by


def plot(out_dir, runs, scenarios, by):
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
    except ImportError:
        print('matplotlib not installed - skipping plots (sudo apt install python3-matplotlib)')
        return
    colors = {'static': '#c62828', 'dynamic': '#1f5fd1'}
    for s in scenarios:
        fig, axes = plt.subplots(3, 1, figsize=(9, 8.5), sharex=True)
        L = None
        for (meta, rows) in runs:
            if meta['name'] != s:
                continue
            L = meta['event_length_s']
            secs = per_second(rows)
            ts = [t for t in secs if -10 <= t <= L + 15]
            avg = lambda t, k: [r[k] for r in secs[t].values() if r[k] is not None]
            loss = [sum(avg(t, 'loss')) / len(secs[t]) for t in ts]
            delay = [(sum(avg(t, 'delay')) / len(avg(t, 'delay'))) if avg(t, 'delay') else float('nan') for t in ts]
            thr = [sum(avg(t, 'thr')) / len(secs[t]) for t in ts]
            c, lab = colors[meta['mode']], meta['mode']
            axes[0].plot(ts, loss, color=c, label=lab, lw=2)
            axes[1].plot(ts, delay, color=c, label=lab, lw=2)
            axes[2].plot(ts, thr, color=c, label=lab, lw=2)
            for e in meta['controller_events']:
                if e['kind'] == 'reroute' and 'APP' in e['text'] and 'mode change' not in e['text']:
                    for ax in axes:
                        ax.axvline(e['t'] - meta['t_event'], color=c, ls=':', lw=1.2)
        for ax in axes:
            if L:
                ax.axvspan(0, L, color='#999999', alpha=0.12, lw=0)
            ax.grid(alpha=0.3)
        axes[0].set_ylabel('Packet loss (%)')
        axes[0].set_ylim(-3, 103)
        axes[1].set_ylabel('One-way delay (ms)')
        axes[1].set_yscale('log')
        axes[2].set_ylabel('Throughput per flow (Mbps)')
        axes[2].set_ylim(bottom=0)
        axes[2].set_xlabel('Seconds since the problem started (grey = problem active; dotted = reroute)')
        st, dy = by.get((s, 'static')), by.get((s, 'dynamic'))
        title = f'{s}: static vs dynamic'
        if st and dy:
            title += (f"  |  recovery {fmt(st['recovery_s'], ' s')} vs {fmt(dy['recovery_s'], ' s')}, "
                      f"loss {st['loss_during_pct']}% vs {dy['loss_during_pct']}%")
        axes[0].set_title(title, fontsize=11)
        axes[0].legend(loc='upper right')
        fig.tight_layout()
        fig.savefig(os.path.join(out_dir, f'plot_{s}.png'), dpi=130)
        plt.close(fig)

    # summary bars
    pairs = [(s, by.get((s, 'static')), by.get((s, 'dynamic'))) for s in scenarios]
    pairs = [p for p in pairs if p[1] and p[2]]
    if not pairs:
        return
    import numpy as np
    x = np.arange(len(pairs))
    fig, axes = plt.subplots(1, 2, figsize=(12, 4.6))
    for ax, key, label in ((axes[0], 'recovery_s', 'Recovery time (s)'),
                           (axes[1], 'loss_during_pct', 'Loss during the event (%)')):
        stv = [p[1][key] if p[1][key] is not None else p[1]['event_s'] for p in pairs]
        dyv = [p[2][key] if p[2][key] is not None else p[2]['event_s'] for p in pairs]
        b1 = ax.bar(x - 0.2, stv, 0.4, label='static', color=colors['static'])
        b2 = ax.bar(x + 0.2, dyv, 0.4, label='dynamic', color=colors['dynamic'])
        ax.bar_label(b1, fmt='%.1f', fontsize=9)
        ax.bar_label(b2, fmt='%.1f', fontsize=9)
        ax.set_xticks(x, [p[0].replace('-', '\n', 1) for p in pairs])   # two-line labels
        ax.set_ylabel(label)
        ax.grid(axis='y', alpha=0.3)
        ax.legend()
    axes[0].set_title('Time until the application is healthy again')
    axes[1].set_title('Mean packet loss while the problem lasts')
    fig.tight_layout()
    fig.savefig(os.path.join(out_dir, 'comparison.png'), dpi=130)
    plt.close(fig)


def main():
    out_dir = sys.argv[1] if len(sys.argv) > 1 else 'results'
    metas = sorted(glob.glob(os.path.join(out_dir, '*.json')))
    if not metas:
        sys.exit(f'No experiment results in {out_dir}. Run: sudo python3 run_experiments.py')
    runs = [load_run(m) for m in metas]
    results = [metrics(meta, rows) for meta, rows in runs]
    scenarios, by = write_reports(out_dir, results)
    plot(out_dir, runs, scenarios, by)
    with open(os.path.join(out_dir, 'RESULTS.md')) as f:
        print(f.read())
    print(f'Wrote summary.csv, RESULTS.md and plots to {out_dir}/')


if __name__ == '__main__':
    main()
