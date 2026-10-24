# Project 27 – Congestion Detection and Traffic Rerouting (UDP + SDN)

A UDP socket application runs over a two-path Mininet network controlled by a
Ryu SDN controller. The controller monitors link load (OpenFlow port stats)
and application QoS reports sent by the receiver, and detects congestion on
the active path. In D2 it reroutes traffic to the less-loaded path.

Full design: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) · Tests: [docs/TEST_CASES.md](docs/TEST_CASES.md)

## Topology

```
            +---- s2 ----+
 h1,h2,h4 - s1            s4 - h3, h5          h3 === VM (management link for QoS reports)
            +---- s3 ----+
  Path A: s1-s2-s4 (default)   Path B: s1-s3-s4 (alternate)   10 Mbps, 2 ms per link
```

| Host | IP | Role |
|---|---|---|
| h1, h2 | 10.0.0.1, 10.0.0.2 | UDP senders |
| h3 | 10.0.0.3 (mgmt 192.168.100.3) | UDP receiver, sends QoS reports |
| h4 → h5 | 10.0.0.4 → 10.0.0.5 | background traffic (creates congestion) |
| Ryu | 127.0.0.1:6653 / 192.168.100.1:9000 | OpenFlow / QoS report listener |

## Files

| File | What it does |
|---|---|
| `topology.py` | Two-path Mininet topology, static ARP, management link h3 ↔ VM |
| `controller.py` | Ryu app: flow rules, port-stats monitoring, QoS listener, congestion detection |
| `udp_sender.py` | Rate-controlled UDP client; header = magic, sender id, seq, timestamp |
| `udp_receiver.py` | UDP server for many clients; per-flow throughput/loss/delay/jitter; CSV; QoS reports |
| `docs/ARCHITECTURE.md` | Problem, objectives, components, message formats, communication flow |
| `docs/TEST_CASES.md` | D1 test cases with expected results |

## Setup

Tested on Ubuntu 26.04 in VirtualBox with Mininet 2.3.0 and Ryu 4.34.

```bash
sudo apt update
sudo apt install mininet openvswitch-switch curl -y

# Ryu needs Python 3.9; uv provides it without touching the system Python
curl -LsSf https://astral.sh/uv/install.sh | sh && source ~/.bashrc
uv python install 3.9
uv venv ~/ryu-env --python 3.9 --seed
source ~/ryu-env/bin/activate
pip install setuptools==67.6.1 wheel pbr
pip install --no-build-isolation ryu
pip install eventlet==0.30.2
ryu-manager --version          # ryu-manager 4.34
```

(On Ubuntu 20.04/22.04 you can skip uv: `pip3 install setuptools==67.6.1 wheel pbr`,
`pip3 install --no-build-isolation ryu`, `pip3 install eventlet==0.30.2`.)

## Running

**Order matters:** clean up first, then the controller, then the topology.
(`sudo mn -c` kills `ryu-manager`, so never run it after the controller is up.)

```bash
# Terminal 2
sudo mn -c

# Terminal 1 – controller
source ~/ryu-env/bin/activate
cd ~/project27
ryu-manager controller.py

# Terminal 2 – topology
cd ~/project27
sudo python3 topology.py
mininet> pingall               # expect 0% dropped (20/20)
```

### Normal traffic
```bash
mininet> xterm h1 h2 h3
# h3
python3 udp_receiver.py --port 5001 --csv baseline.csv --report-to 192.168.100.1:9000
# h1
python3 udp_sender.py --dst 10.0.0.3 --rate 2 --duration 60 --id 1
# h2
python3 udp_sender.py --dst 10.0.0.3 --rate 2 --duration 60 --id 2
```
The controller logs QoS reports and Path A at about 40% utilization.

### Controlled congestion
While the senders are running, at the `mininet>` prompt:
```bash
mininet> h5 python3 udp_receiver.py --port 5001 > /tmp/h5.log 2>&1 &
mininet> h4 python3 udp_sender.py --dst 10.0.0.5 --rate 9 --duration 20 --id 99 > /tmp/h4.log 2>&1 &
```
The controller logs `CONGESTION DETECTED on Path A (...)`, the h3 receiver shows
loss and delay rising, and 20 s later `Congestion cleared on Path A`.

### Useful commands
```bash
mininet> sh ovs-ofctl -O OpenFlow13 dump-flows s1     # rules installed by the controller
mininet> h3 ping -c 2 192.168.100.1                    # management link to controller
mininet> exit                                          # then Ctrl+C the controller, then sudo mn -c
```

## Configuration

| Setting | Where | Default |
|---|---|---|
| Path link bandwidth | `topology.py --bw` and `LINK_CAPACITY_MBPS` in `controller.py` (keep equal) | 10 Mbps |
| Utilization threshold | `UTIL_THRESHOLD` in `controller.py` | 80% |
| Loss threshold | `LOSS_THRESHOLD` in `controller.py` | 5% |
| Stats poll interval | `POLL_INTERVAL` in `controller.py` | 2 s |
| QoS report port | `QOS_PORT` in `controller.py`, `--report-to` on receiver | 9000 |

## Status

**D1**
- [x] Two-path topology with bandwidth/delay-limited links
- [x] SDN controller with proactive flow rules
- [x] Port-stats monitoring and threshold-based congestion detection
- [x] UDP sender/receiver: multiple clients, custom header, error handling, QoS metrics, CSV logging
- [x] Socket → SDN integration: receiver QoS reports drive the controller's congestion decision
- [x] Architecture document and test cases

**D2**
- [ ] Automatic rerouting to Path B (`set_path()` is ready; see the comment in `_evaluate()`)
- [ ] Link-failure scenario
- [ ] Static vs dynamic comparison: recovery time, throughput, loss, with plots from the CSVs
