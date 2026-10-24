# Test Cases – Deliverable 1

Fill in the **Result** column and add the screenshot file name as you run each test.
Before every Mininet run: `sudo mn -c`, then start the controller, then the topology.

## A. Environment

| ID | Test | Steps | Expected | Result | Screenshot |
|---|---|---|---|---|---|
| A1 | Mininet installed | `mn --version`; `sudo mn --test pingall` | Version printed; `0% dropped (2/2)` | | 01 |
| A2 | Ryu installed | `source ~/ryu-env/bin/activate`; `ryu-manager --version` | `ryu-manager 4.34` | | 02 |

## B. Mininet + SDN

| ID | Test | Steps | Expected | Result | Screenshot |
|---|---|---|---|---|---|
| B1 | Controller loads | `ryu-manager controller.py` | Loads with no traceback; `QoS listener on UDP port 9000` | | 03 |
| B2 | Switches connect | Start `topology.py` | Controller logs `Switch s1..s4 connected` | | 03 |
| B3 | Topology built correctly | `mininet> net` | Links match the port table in ARCHITECTURE.md | | 05 |
| B4 | Full connectivity | `mininet> pingall` | `0% dropped (20/20)` | | 06 |
| B5 | Flow rules installed | `sh ovs-ofctl -O OpenFlow13 dump-flows s1` | 5 rules; .3 and .5 → s1-eth4 (Path A) | | 07 |
| B6 | Path delay applied | `h1 ping -c 5 h3` | RTT ≥ 8 ms (2 links × 2 ms × 2 directions) | | 08 |
| B7 | Monitoring works | Watch controller for 10 s | `Path A`/`Path B` load lines every 2 s | | 03 |

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
| D2 | QoS reports reach controller | h3 receiver with `--report-to 192.168.100.1:9000`; h1 sends | Controller logs `QoS report from 192.168.100.3: flow 10.0.0.1#1 ...` every 2 s | | 15 |
| D3 | Network-detected congestion | C2 running + h4 → h5 at 9 Mbps | Path A `HIGH`; `CONGESTION DETECTED on Path A (link util ...)` | | 16 |
| D4 | Application-detected congestion | Same as D3 | Receiver shows loss > 5%; controller reason includes `app loss ...` | | 16 |
| D5 | Recovery detected | Let the h4 flood end | `Congestion cleared on Path A`; receiver loss back to 0 | | 17 |
| D6 | Baseline data saved | Receiver `--csv baseline.csv` (C2) and `--csv congested.csv` (D3) | CSV files with per-interval throughput/loss/delay | | – |
