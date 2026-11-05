# Project 27 – Congestion Detection and Traffic Rerouting (UDP + SDN)

A UDP socket application runs over a three-path Mininet network controlled by a
Ryu SDN controller. The controller learns every flow, classifies it as
application (its receiver sends QoS reports) or background, and watches port
statistics, per-flow rates, link status and the application's own QoS
reports. When a path becomes congested it moves the application flows to the
least-loaded path that has room (without chasing the flood). When a link
fails it fails every flow over within milliseconds, measuring the time with an
OpenFlow barrier, and fails back after the repair. A static mode (detect only)
is the baseline for the performance comparison, and a live web dashboard shows
loss, delay, throughput, path load, flow placement and recovery times.

Design: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) · Tests: [docs/TEST_CASES.md](docs/TEST_CASES.md) ·
Final demo script: [DEMO_D2.md](DEMO_D2.md) · Results: `results/RESULTS.md` (after running the experiments)

## Topology

```
            +---- s2 ----+
 h1,h2,h4 - s1 --- s3 --- s4 - h3, h5          h3 === VM (management link)
            +---- s5 ----+
  Path A: s1-s2-s4 (default)   Path B: s1-s3-s4   Path C: s1-s5-s4
  10 Mbps, 2 ms per link
```

| Host | IP | Role |
|---|---|---|
| h1, h2 | 10.0.0.1, 10.0.0.2 | UDP senders (application) |
| h3 | 10.0.0.3 (mgmt 192.168.100.3) | UDP receiver: QoS reports out, SDN events in |
| h4 → h5 | 10.0.0.4 → 10.0.0.5 | background flood (creates congestion) |
| Ryu | 127.0.0.1:6653 · UDP 9000 | OpenFlow · QoS reports and commands |

## Files

| File | What it does |
|---|---|
| `udp_sender.py` | Rate-controlled UDP client; header = magic, sender id, seq, timestamp; end-of-stream marker |
| `udp_receiver.py` | UDP server for many clients; per-second throughput/loss/delay/jitter; stall detection; QoS reports; SDN event channel (UDP 5002) with failover and recovery timings; CSV |
| `topology.py` | Three-path Mininet topology, static ARP, management link h3 ↔ VM |
| `controller.py` | Ryu app: flow learning and classification, load-aware placement, congestion relief, link failover with barrier timing, failback, static/dynamic modes, live state for the dashboard |
| `dashboard.py` | Live web dashboard on http://localhost:8000 (standard library only, works offline) |
| `run_experiments.py` | Runs every scenario in static and dynamic mode automatically |
| `analyze.py` | Turns the results into `RESULTS.md`, `summary.csv` and plots |
| `docs/` | Architecture, test cases |
| `results/early_run/` | Data from the team's first D2 run on the VM |

## Setup (once)

Tested with Mininet 2.3.0 and Ryu 4.34 (Ryu needs Python 3.9–3.11).

```bash
sudo apt update
sudo apt install mininet openvswitch-switch xterm curl python3-matplotlib -y

curl -LsSf https://astral.sh/uv/install.sh | sh && source ~/.bashrc
uv python install 3.11
uv venv ~/ryu-env --python 3.11 --seed
source ~/ryu-env/bin/activate
pip install setuptools==67.6.1 wheel pbr
pip install --no-build-isolation ryu
pip install eventlet==0.33.3
ryu-manager --version          # ryu-manager 4.34
```
If your Ryu environment lives elsewhere (for example `ryu311-venv` inside the
project), activate that one instead. Virtual environments are listed in
`.gitignore` and must not be committed.

## Running

**Order matters:** `sudo mn -c` first, then the controller, then the topology
(`sudo mn -c` kills `ryu-manager`).

```bash
# Terminal 2
sudo mn -c
xhost +SI:localuser:root

# Terminal 1 – controller (dynamic by default; P27_MODE=static for the baseline)
source ~/ryu-env/bin/activate && cd ~/SDN
ryu-manager controller.py

# Terminal 3 – dashboard, then open http://localhost:8000
cd ~/SDN && python3 dashboard.py

# Terminal 2 – network
cd ~/SDN && sudo python3 topology.py
mininet> pingall
mininet> xterm h1 h2 h3 h4 h5
```

Host windows:
```bash
# h5, then h3
python3 udp_receiver.py --port 5001
python3 udp_receiver.py --port 5001 --csv live.csv --report-to 192.168.100.1:9000
# h1, h2
python3 udp_sender.py --dst 10.0.0.3 --rate 2 --duration 900 --id 1
python3 udp_sender.py --dst 10.0.0.3 --rate 2 --duration 900 --id 2
```

Problems to create while they run:
```bash
# congestion – in h4
python3 udp_sender.py --dst 10.0.0.5 --rate 9 --duration 20 --id 99
# link failure (and repair) – at mininet>
mininet> link s1 s2 down
mininet> link s1 s2 up
```
Switch **Static** / **Dynamic** with the dashboard buttons and repeat.

## Performance evaluation

```bash
# controller running in Terminal 1, nothing else using Mininet
cd ~/SDN
sudo python3 run_experiments.py           # 10 runs, about 11 minutes
sudo python3 run_experiments.py --quick   # 4 runs, about 4.5 minutes
python3 analyze.py results
```

| Scenario | What happens (15 s into a 45 s run) |
|---|---|
| congestion-7 / -9 / -12 | h4 floods 7, 9 or 12 Mbps over Path A for 20 s |
| failure | link s1–s2 down for 15 s |
| double-failure | link s1–s2 down, then s1–s3 7 s later, both up at 15 s |

Each runs in static and dynamic mode under identical conditions. Output in
`results/`: per-run CSVs and controller events, `summary.csv`, `RESULTS.md`
(tables, including failover timings), `plot_<scenario>.png` and `comparison.png`.

## Configuration

`controller.py` constants: `UTIL_THRESHOLD` 0.80, `LOSS_THRESHOLD` 5%,
`POLL_INTERVAL` 1 s, `REROUTE_HOLD` 6 s, `RESTORE_STABLE` 5 s,
`LINK_CAPACITY_MBPS` 10 (must match `topology.py --bw`).

| Environment variable | Default | Meaning |
|---|---|---|
| `P27_MODE` | dynamic | starting mode (also switchable from the dashboard) |
| `P27_PLACEMENT` | fixed | `fixed`: new flows start on Path A; `load`: on the least-loaded path |
| `P27_APP_HOSTS` | 10.0.0.3 | IPs always treated as application receivers |
| `P27_FAILBACK` | on | move displaced flows back after a repair |
| `P27_RESTORE_STABLE` | 5 | seconds a repaired path must stay up first |
| `SDN_STATE_FILE` | /tmp/sdn_live_state.json | live state for the dashboard |

## Status

- [x] D1: topology, controller, monitoring, congestion detection, UDP app, QoS reports
- [x] Flow learning and APP/BG classification
- [x] Load-aware congestion relief that never chases the flood
- [x] Link failover with barrier-confirmed timing, failback after repair
- [x] Stall detection (failures measured as 100% loss) and recovery timing
- [x] Static vs dynamic experiments under 5 conditions, tables and plots
- [x] Live dashboard with mode switch
