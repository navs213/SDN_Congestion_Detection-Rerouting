# Test Cases

## Deliverable 1

Fill in the **Result** column and add the screenshot file name as you run each test.
Before every Mininet run: `sudo mn -c`, then start the controller, then the topology.

## A. Environment

| ID | Test | Steps | Expected | Result | Screenshot |
|---|---|---|---|---|---|
| A1 | Mininet installed | `mn --version`; `sudo mn --test pingall` | Version printed; `0% dropped (2/2)` | | 01 |
| A2 | Ryu installed | activate your Ryu venv; `ryu-manager --version` | `ryu-manager 4.34` | | 02 |

## B. Mininet + SDN

| ID | Test | Steps | Expected | Result | Screenshot |
|---|---|---|---|---|---|
| B1 | Controller loads | `ryu-manager controller.py` | Loads with no traceback; `QoS listener on UDP port 9000` | | 03 |
| B2 | Switches connect | Start `topology.py` | Controller logs `Switch s1..s5 connected` | | 03 |
| B3 | Topology built correctly | `mininet> net` | Links match the port table in ARCHITECTURE.md | | 05 |
| B4 | Full connectivity | `mininet> pingall` | `0% dropped (20/20)` | | 06 |
| B5 | Flow rules installed | after `pingall`: `sh ovs-ofctl -O OpenFlow13 dump-flows s1` | local-host rules (priority 10), table-miss → CONTROLLER (priority 1), and one priority-20 rule per (src, dst) pair crossing the core, output s1-eth4 (Path A) | | 07 |
| B6 | Path delay applied | `h1 ping -c 5 h3` | RTT ≥ 8 ms (2 links × 2 ms × 2 directions) | | 08 |
| B7 | Monitoring works | Watch controller for 10 s | `Path A`/`Path B`/`Path C` load lines and `ACTIVE FLOW` lines every second | | 03 |

## C. Socket application

| ID | Test | Steps | Expected | Result | Screenshot |
|---|---|---|---|---|---|
| C1 | Single flow | h3: receiver; h1: `--rate 2 --duration 20 --id 1` | ~2 Mbps, 0% loss | | 09 |
| C2 | Multiple clients | h1 and h2 together, `--rate 2` each | Two flows listed separately, each ~2 Mbps, 0% loss | | 10 |
| C3 | Three clients | h1, h2, h4 → h3 at 1 Mbps each | Three flows tracked | | 11 |
| C4 | Malformed packet | `h1 python3 -c "import socket; socket.socket(2,2).sendto(b'hi',('10.0.0.3',5001))"` | `malformed packets dropped: 1`; receiver keeps running | | 12 |
| C5 | Overload on purpose | h1 `--rate 12` (path is 10 Mbps) | Loss > 0%, throughput capped near 10 Mbps | | 13 |
| C6 | Receiver absent | Run a sender with no receiver running | Sender finishes without crashing | | – |
| C7 | Clean shutdown | Ctrl+C on the receiver | Prints `FINAL` stats; CSV saved | | – |

## D. Socket ↔ SDN integration

| ID | Test | Steps | Expected | Result | Screenshot |
|---|---|---|---|---|---|
| D1 | Management link | `h3 ping -c 2 192.168.100.1` | 0% loss | | 14 |
| D2 | QoS reports reach controller | h3 receiver with `--report-to 192.168.100.1:9000`; h1 sends | Controller logs `QoS report from 192.168.100.3: flow 10.0.0.1#1 ...` every second | | 15 |
| D3 | Network-detected congestion | C2 running + h4 → h5 at 9 Mbps | Path A `HIGH`; `CONGESTION DETECTED on Path A (link util ...)` | | 16 |
| D4 | Application-detected congestion | Same as D3 | Receiver shows loss > 5%; controller reason includes `app loss ...` | | 16 |
| D5 | Recovery detected | Let the h4 flood end | `Congestion cleared on Path A`; receiver loss back to 0 | | 17 |
| D6 | Baseline data saved | Receiver `--csv baseline.csv` (C2) and `--csv congested.csv` (D3) | CSV files with per-interval throughput/loss/delay | | – |

---

## Deliverable 2

Setup for E–G: `sudo mn -c`, controller, `python3 dashboard.py` (open http://localhost:8000), `sudo python3 topology.py`, `xterm h1 h2 h3 h4 h5`; h5 and h3 receivers running, h1 and h2 sending 2 Mbps with `--duration 900` (commands in README).

### E. Dynamic SDN behaviour

| ID | Test | Steps | Expected | Result | Screenshot |
|---|---|---|---|---|---|
| E1 | Normal operation | Dynamic mode, h1 + h2 sending | Dashboard NORMAL; both APP flows on Path A, A ~42%, 0% loss, ~5 ms | | 20 |
| E2 | Flow classification | Look at the Flow placement table / `ACTIVE FLOW` log lines | h1→h3, h2→h3 = APP (h3 sends QoS reports); h4→h5 = BG | | 20 |
| E3 | Congestion → reroute | h4: `--rate 9 --duration 20` | `CONGESTION DETECTED on Path A`, then `Rerouting (congestion)` of the APP flows to the least-loaded paths (B and/or C) within ~1–2 s; h3 loss back to 0% | | 21 |
| E4 | Flood is not chased | During E3 | BG flow h4→h5 stays on A; log `No healthy path can absorb 10.0.0.4->10.0.0.5 (9.00 Mbps)` | | 22 |
| E5 | Rules changed | During E3: `sh ovs-ofctl -O OpenFlow13 dump-flows s1` | `nw_src=10.0.0.1,nw_dst=10.0.0.3` → s1-eth5 or s1-eth6; `nw_src=10.0.0.4,nw_dst=10.0.0.5` → s1-eth4 | | 22 |
| E6 | Recovery time | After E3 | `Application RECOVERED ... in X s (dynamic mode)` in the log and the dashboard | | 21 |
| E7 | Static baseline | Dashboard → Static, repeat E3 | Congestion detected, nothing moved; loss/delay high for the whole 20 s; recovery ≈ 20 s | | 23 |

### F. Robustness – link failures

| ID | Test | Steps | Expected | Result | Screenshot |
|---|---|---|---|---|---|
| F1 | Failover | Dynamic, all flows on A, `mininet> link s1 s2 down` | `LINK DOWN ... Path A is DOWN`; every flow on A rerouted (APP first); `Failover rules sent X ms`, `confirmed installed after Y ms`; h3 CSV gets `link_down`, `failover_rules_sent`, `traffic_recovered` rows | | 24 |
| F2 | Failback | `mininet> link s1 s2 up` | `LINK UP: Path A is available again`; after 5 s the displaced flows move back (`Rerouting (failback)`) | | 25 |
| F3 | Double failure | Dynamic: `link s1 s2 down`, then `link s1 s3 down` | Flows end up on Path C; app keeps running | | 26 |
| F4 | All paths down | `link s1 s2 down`, `link s1 s3 down`, `link s1 s5 down` | `ALL PATHS DOWN - cannot fail over`; h3 shows STALLED; restoring any link brings traffic back | | – |
| F5 | Static failure | Static mode, `link s1 s2 down` for 15 s | No failover; h3 STALLED (100% loss) until `link s1 s2 up`; `traffic_recovered` ≈ 15 000 ms | | 27 |

### G. Application robustness

| ID | Test | Steps | Expected | Result | Screenshot |
|---|---|---|---|---|---|
| G1 | Finished vs stalled | Let a sender finish | h3 prints `flow ... finished`, never STALLED | | – |
| G2 | Reordering after reroute | `ooo=` in h3 after E3 | A few out-of-order packets (old path's queue drains after the new path's packets), counted correctly | | – |
| G3 | Bad control message | `echo bad \| nc -u -w1 127.0.0.1 9000` on the VM | Controller logs `Bad QoS report ignored` and keeps running | | – |

### H. Performance evaluation

| ID | Test | Steps | Expected | Result |
|---|---|---|---|---|
| H1 | Automated runs | `sudo python3 run_experiments.py` | 10 runs complete; `results/` holds 10 CSV + 10 JSON | |
| H2 | Analysis | `python3 analyze.py results` | `RESULTS.md`, `summary.csv`, 5 × `plot_*.png`, `comparison.png` | |
| H3 | Dynamic beats static | Read `RESULTS.md` | For every scenario, dynamic has lower loss and shorter recovery than static | |
