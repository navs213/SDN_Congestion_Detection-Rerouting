# Live Demo Script – Deliverable 1 (about 10 minutes)

Keep this file open in a text editor on the VM so you can copy-paste commands.

---

## Part 0 – Before the teacher arrives (about 10 min, do this alone)

1. **Check the clock** (a wrong clock breaks apt and confuses timestamps):
   ```bash
   date
   ```
   If it's wrong:
   ```bash
   sudo date -s "$(curl -sI https://www.google.com | grep -i '^date:' | cut -d' ' -f2-)"
   ```

2. **Stop the VM from sleeping:**
   ```bash
   gsettings set org.gnome.desktop.session idle-delay 0
   gsettings set org.gnome.settings-daemon.plugins.power sleep-inactive-ac-type 'nothing'
   ```

3. **Open two terminals.** Put Terminal 1 (controller) on the left and Terminal 2 (Mininet) on the right.

4. **Terminal 2 – clean state and window permission:**
   ```bash
   sudo mn -c
   xhost +SI:localuser:root
   ```

5. **Do one full dry run** of Parts 2–6, then clean up again:
   1. At `mininet>`, type `exit`.
   2. In Terminal 1, press Ctrl+C.
   3. In Terminal 2, run `sudo mn -c`.

   This catches problems before they happen in front of the teacher.

6. Open `docs/ARCHITECTURE.md` (or the GitHub page) in a browser tab for Part 1.

---

## Part 1 – Explain the problem and design (about 2 min, no commands)

Show the topology diagram in `docs/ARCHITECTURE.md` and say:

* **Problem:** "When one path between two points gets overloaded, UDP traffic
  suffers loss and delay, because UDP has no congestion control. Our SDN
  controller detects congestion on the active path so traffic can be moved to
  a less-loaded path."
* **Topology:** "Two paths from s1 to s4: Path A through s2 and Path B through s3.
  Each link is 10 Mbps with 2 ms delay. h1 and h2 are UDP senders, h3 is the
  receiver, and h4 → h5 is background traffic we use to create congestion."
* **Socket app:** "Our UDP header has a magic value, a sender id, a sequence
  number and a timestamp. The receiver uses them to measure loss, delay and
  jitter per flow, for many senders on one socket."
* **Controller:** "Ryu installs flow rules, polls switch port statistics every
  2 seconds, and also receives QoS reports from the receiver. If link use is
  80% or more, or the application sees 5% or more loss, it declares congestion."
* **Integration:** "The receiver sends QoS reports to the controller over a
  separate management link, so application events influence the controller."

---

## Part 2 – Start the controller (Terminal 1)

```bash
source ~/ryu-env/bin/activate
cd ~/project27
ryu-manager controller.py
```
**Point out:** `QoS listener on UDP port 9000`. It is now waiting for switches.

---

## Part 3 – Start the network (Terminal 2)

```bash
cd ~/project27
sudo python3 topology.py
```
**Point out:**
* In Terminal 2: hosts h1–h5, switches s1–s4, and the 10 Mbit / 2 ms links.
* In Terminal 1: `Switch s1 connected` … `s4 connected`, then Path A / Path B
  load lines every 2 seconds (both at 0%).

---

## Part 4 – Prove the SDN network works (Terminal 2)

```
mininet> net
```
"These are the links. s1 port 4 goes to Path A, port 5 goes to Path B."

```
mininet> pingall
```
"0% dropped, so every host can reach every other host through our controller's rules."

```
mininet> sh ovs-ofctl -O OpenFlow13 dump-flows s1
```
"These are the flow rules our controller installed. Traffic to h3 and h5
(10.0.0.3, .5) leaves on s1-eth4, which is Path A. The packet counters show
the ping traffic matched them."

```
mininet> h1 ping -c 3 h3
```
"About 8 ms round trip: two links of 2 ms each, there and back."

```
mininet> h3 ping -c 2 192.168.100.1
```
"This is the management link from h3 to the controller, used for QoS reports."

---

## Part 5 – Socket application under normal load

```
mininet> xterm h1 h2 h3 h4 h5
```
Arrange the windows so the h3 window and Terminal 1 (controller) are both visible.

**h5 window** (sink for the background traffic):
```bash
python3 udp_receiver.py --port 5001
```

**h3 window** (start the receiver first):
```bash
python3 udp_receiver.py --port 5001 --csv demo.csv --report-to 192.168.100.1:9000
```

**h1 window:**
```bash
python3 udp_sender.py --dst 10.0.0.3 --rate 2 --duration 120 --id 1
```

**h2 window** (right after h1):
```bash
python3 udp_sender.py --dst 10.0.0.3 --rate 2 --duration 120 --id 2
```

**Point out (wait about 10 s):**
* **h3:** two separate flows, `10.0.0.1#1` and `10.0.0.2#2`, at 2 Mbps each,
  0% loss, about 5 ms delay. "One UDP socket serving multiple clients."
* **Controller:** `QoS report from 192.168.100.3: flow ...` lines, and
  `Path A (active) load≈4.2 Mbps util≈42% ok`. "Both views agree: the app
  reports 4 Mbps and the switch counters show 4.2, which includes headers."

---

## Part 6 – Create congestion (the main event)

**Start this within about 30 s of starting h1 and h2, while they're still sending.**

**h4 window:**
```bash
python3 udp_sender.py --dst 10.0.0.5 --rate 9 --duration 20 --id 99
```
Say: "h4 is now pushing 9 Mbps through the same Path A. That's 13 Mbps on a
10 Mbps link."

**Point out over the next 20 s:**
* **h3:** delay climbs from 5 ms to 50, then 500, then about 845 ms (the switch
  queue filling up), and then loss jumps to 30–50%. "Delay rises first while
  the buffer fills. Loss starts when it's full."
* **Controller:** Path A `util≈100% HIGH`, and
  `*** CONGESTION DETECTED on Path A (link util ...%, app loss ...%)`.
  "It's detected from both views: the switch statistics and the application's
  own QoS report."
* Path B stays at 0%. "The alternate path is idle. In D2 the controller will
  move traffic onto it."

**After h4 prints `done`:**
* **h3:** loss back to 0%, delay back to about 5 ms.
* **Controller:** `*** Congestion cleared on Path A`.

---

## Part 7 – Error handling (optional, 30 s, if there's time)

While h3's receiver is still running, at `mininet>`:
```
mininet> h1 python3 -c "import socket; socket.socket(2,2).sendto(b'hi',('10.0.0.3',5001))"
```
**Point out:** h3 shows `malformed packets dropped: 1` and keeps running.
"Packets without our magic header are rejected."

---

## Part 8 – Wrap up

1. In h3, press **Ctrl+C** and point out the `FINAL` stats: "Results are also
   saved to `demo.csv` for performance graphs."
2. Close by saying what's next: "For D2 we'll enable automatic rerouting to
   Path B when congestion is detected, add a link-failure scenario, and compare
   recovery time and throughput against this static baseline."
3. Clean up:
   1. In h5, press Ctrl+C.
   2. At `mininet>`, type `exit`.
   3. In Terminal 1, press Ctrl+C.
   4. In Terminal 2, run `sudo mn -c`.

---

## If something goes wrong

| Problem | Fix |
|---|---|
| `pingall` 100% dropped | The controller isn't running (or `mn -c` was run after it). Do `exit`, `sudo mn -c`, restart the controller, then the topology. |
| xterm windows don't open | `exit`, `sudo mn -c`, `xhost +SI:localuser:root`, restart. |
| `Unknown command` at `mininet>` | You typed a host command at the Mininet prompt. Put it in the host's xterm, or prefix it with the host name (`h3 ...`). |
| Typo crashed Mininet (Traceback) | Close the xterms, `sudo mn -c`, restart the controller, then the topology. |
| h3 shows no loss during the flood | The flood didn't overlap with h1/h2. Restart the senders and start h4 within 30 s. |
| `ryu-manager: command not found` | `source ~/ryu-env/bin/activate` |
| `Address already in use` (Ryu) | `pkill -f ryu-manager`, then start it again. |

**Backup plan:** keep the screenshots folder open. If something fails live and
can't be fixed in a minute, show the screenshots from your earlier successful run.
