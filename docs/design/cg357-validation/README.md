# Served application recovery validation — 2026-09-06

The operator ran served_benchmark.py in a disposable garden using the PR source, a real TCP uvicorn server and three separate local worker processes executing CPU work, writing run metadata and updating output. All four processes shared CPUQuota=200%, MemoryHigh=3G, MemoryMax=4G and MemorySwapMax=512M in one systemd unit. No model API calls or production worker sessions were used; this establishes controlled resource contention, not all production harness behaviors.

| Historical records (+3 live) | Requests | Empirical p95 | Maximum | Index refreshes | Metadata reads |
|---|---:|---:|---:|---:|---:|
| 1546 | 60 | 0.213s | 0.320s | 10 | 1576 |
| 6000 | 60 | 0.570s | 0.695s | 19 | 6054 |

Each test covers Inbox, Board, Board partial, Now 2 and Now period, 12 requests each across multiple one-second expiry intervals. Workers were confirmed alive when measurements completed. The small extra reads are changing live-task buckets, not complete history rereads per request. Raw data is in result-1546.json and result-6000.json. The 1546 test peaked at231.6MiB with zero swap; the second unit's final resource summary is recorded in the operator incident log when it stops.

In the Windows in-app browser against the 6000-record server atlocalhost:8783, the operator observed Now's connected live updates and changing worker output, followed Open run to actual run details, navigated to Board, and then Inbox. All rendered successfully during CPU work. inbox-windows.png was saved and read back. These are actual browser interactions; archived-run interaction remains covered by automated round-trip/detail tests, not claimed as a browser journey here.

Repeat from this worktree using PYTHONPATH=src and its Python environment, with systemd-run resource settings above, then run `python docs/design/cg357-validation/served_benchmark.py --history 1546` and `--history 6000 --hold 120`. A fixture is created under /home/joshua/work/operator-test-tmp; no live state is touched. The hold permits browser inspection, and workers/server are cleaned up by the script. Production recovery still requires safe installation and the incident observation window.
