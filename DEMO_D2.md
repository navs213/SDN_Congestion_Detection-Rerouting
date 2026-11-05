# Final Demo Script – Deliverable 2 (about 15 minutes)

The story: **show the problem (static), show the fix (dynamic), show link
failures, then show the measured comparison.** The dashboard is on screen the
whole time.

---

## Part 0 – Before the demo (alone, about 20 min)

1. **Run the experiments first** (they need Mininet to themselves):
   ```bash
   sudo mn -c
   # Terminal 1
   source ~/ryu-env/bin/activate && cd ~/SDN && ryu-manager controller.py
   # Terminal 2
   cd ~/SDN && sudo python3 run_experiments.py      # about 11 min
   python3 analyze.py results
   ```
   Keep `results/comparison.png`, `results/plot_congestion-9.png`,
   `results/plot_failure.png` and `results/RESULTS.md` open for Part 6.
   Then stop the controller (Ctrl+C).

2. **Prepare the screen:**
   ```bash
   date                                   # fix the clock if it is wrong (see README)
   gsettings set org.gnome.desktop.session idle-delay 0
   gsettings set org.gnome.settings-daemon.plugins.power sleep-inactive-ac-type 'nothing'
   sudo mn -c
   xhost +SI:localuser:root
   ```
   Three terminals: **T1** controller, **T2** Mininet, **T3** dashboard.

---

## Part 1 – Start everything (2 min)

**T1 – controller, starting in static mode:**
```bash
source ~/ryu-env/bin/activate && cd ~/SDN
P27_MODE=static ryu-manager controller.py
```
**T3 – dashboard:** `cd ~/SDN && python3 dashboard.py`, then open
**http://localhost:8000** in Firefox (F11 = full screen).

**T2 – network:**
```bash
cd ~/SDN && sudo python3 topology.py
mininet> pingall
mininet> xterm h1 h2 h3 h4 h5
```
**h5:** `python3 udp_receiver.py --port 5001`
**h3:** `python3 udp_receiver.py --port 5001 --csv demo.csv --report-to 192.168.100.1:9000`
**h1:** `python3 udp_sender.py --dst 10.0.0.3 --rate 2 --duration 900 --id 1`
**h2:** `python3 udp_sender.py --dst 10.0.0.3 --rate 2 --duration 900 --id 2`

Say: *"Three paths between s1 and s4. h1 and h2 each send 2 Mbps of UDP to h3.
The controller learned both flows from their first packets and classified them
as APP, because h3 sends it QoS reports."* Point at the **Flow placement**
table (both on Path A) and the charts (0% loss, ~5 ms).

---

## Part 2 – The problem: static routing (3 min)

Dashboard mode is **Static**. In **h4**:
```bash
python3 udp_sender.py --dst 10.0.0.5 --rate 9 --duration 20 --id 99
```
- The flow table gains `h4 → h5`, class **BG**, on Path A.
- Path A goes to ~100% (above the dashed line); B and C stay at 0%.
- Delay climbs to ~845 ms, then loss jumps to 20–30%.
- The log says **CONGESTION DETECTED** and nothing moves: *"Static routing
  only detects. The application suffers for the whole 20 seconds even though
  two paths are idle."*
- Afterwards: **RECOVERED in ~20 s (static)** in the Recovery table.

---

## Part 3 – The fix: dynamic rerouting (3 min)

Click **Dynamic**, then run the same flood again in **h4** (up arrow, Enter).
- Within a second or two: **CONGESTION DETECTED**, then **Rerouted APP flow
  h1→h3: Path A → Path B** and **h2→h3: Path A → Path C** (blue markers).
  *"The controller moves the application flows, APP first, to the least-loaded
  paths that have room, and spreads them out."*
- The flood stays on A: *"No path has room for the 9 Mbps background flow, so
  it isn't moved. We never chase the flood around the network."*
- Loss back to 0%, delay back to ~5 ms within a couple of seconds.
- **RECOVERED in ~1–3 s (dynamic)** next to the static ~20 s.

Optional, at `mininet>`:
```
mininet> sh ovs-ofctl -O OpenFlow13 dump-flows s1
```
*"Each flow has its own rule. h1→h3 now leaves on s1-eth5 (B), h2→h3 on
s1-eth6 (C), and h4→h5 still on s1-eth4 (A)."*

---

## Part 4 – Robustness: link failures (3 min)

Click **Static**, then **Dynamic** again: this puts every flow back on Path A.
At `mininet>`:
```
mininet> link s1 s2 down
```
- **LINK FAILURE on Path A** comes straight from an OpenFlow port-status message.
- **Failover on Path A: rules sent after ~10 ms, installed after ~15 ms**,
  confirmed with an OpenFlow barrier.
- The h3 window prints **TRAFFIC RECOVERED (recovery = ~20 ms)**, the first
  packet after the failure.

Now take out the second path as well:
```
mininet> link s1 s3 down
```
- The flows on B move to **Path C**. The application keeps running.

Repair:
```
mininet> link s1 s2 up
mininet> link s1 s3 up
```
- **Path A is back up**. After 5 s the displaced flows return (**failback**).

Optional contrast: **Static** mode, `link s1 s2 down` for ~15 s. h3 shows
**STALLED** (100% loss) the whole time. *"The receiver knows the senders didn't
finish, because they never sent their end-of-stream marker, so silence counts
as loss."*

---

## Part 5 – How it works (1 min)

Point at the architecture flowchart in `docs/ARCHITECTURE.md`: packet-in →
per-flow rule; APP/BG classification from QoS reports; stats every second;
congestion = 80% utilization or 5% app loss; move only if the flow fits; 6 s
hold-down against ping-pong; failover + barrier timing; failback.

---

## Part 6 – Measured results (3 min)

Show `comparison.png`, `plot_congestion-9.png`, `plot_failure.png`, then the
tables in `RESULTS.md`. Say: *"Each scenario ran twice under identical
conditions with `run_experiments.py`: three flood strengths, a single failure
and a double failure, in static and dynamic mode. Time zero is when the
problem starts."* Read out your own numbers:
- recovery time and loss, static vs dynamic, for congestion-9 and failure
- the failover timing table (rules installed, first packet)
- static gets worse as the flood grows; dynamic stays near zero

---

## Cleanup

Ctrl+C in h3 and h5 → `exit` at `mininet>` → Ctrl+C in T1 and T3 → `sudo mn -c`.

## If something goes wrong

| Problem | Fix |
|---|---|
| Dashboard: "Controller not running" | Restart T1; if pings then fail, `exit`, `sudo mn -c`, restart the controller, then the topology |
| Page won't load | Start T3: `python3 dashboard.py` |
| No reroute in dynamic mode | Check the mode button; wait 6 s after a previous move (hold-down) |
| Flood has no effect | h1/h2 finished; restart them with `--duration 900` |
| `pingall` 100% dropped | `exit`, `sudo mn -c`, restart the controller, then the topology |
| xterms don't open | `exit`, `sudo mn -c`, `xhost +SI:localuser:root`, restart |

**Backup:** your plots and screenshots from earlier runs.
