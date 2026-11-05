# Early D2 run (team VM, 3 Nov)

Data from the first run of the per-flow controller on the team's VM, kept as evidence.

| File | Contents |
|---|---|
| `d2_results.csv` | Receiver CSV for flow 10.0.0.4 → h3 with SDN events. The link failure on Path A at t = 242 s: failover rules sent 10.8 ms and confirmed installed 15.1 ms after link-down detection; the first packet arrived 21.8 ms after detection. Failback after the repair confirmed in 1.1 ms. At t = 565 s all three paths were taken down. |
| `qos_metrics.csv` | Every QoS report the controller received during that run |
| `baseline.csv` | Earlier receiver baseline |

These files use the older receiver format (2 s interval, no `unix_ts`/`status`
columns), so `analyze.py` does not read them. The full comparison comes from
`run_experiments.py`.
