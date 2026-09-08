# CG-367 loaded-page request profile

Measured 2026-09-07 on Linux/WSL with Python 3.14.0. The before revision is
`3043e30257947b039e887a48a3c464873c689c24`; the measured application change is
`b9c070e86df2a40b99d42648f4ba0db1ef58c352`. The complete
machine-readable results and request spans are in `report.json`.

## Method

`reproduce.py` creates disposable gardens, starts a single-worker Uvicorn server, and makes
serial HTTP requests to `/now1` and `/inbox`. It never starts the scheduler and never reads
or changes the production garden. Two deterministic datasets were used:

| dataset | tasks | runs | events |
| --- | ---: | ---: | ---: |
| representative current | 100 | 1,549 | 12,500 |
| larger history | 100 | 6,003 | 50,000 |

Each revision/dataset/load combination starts a fresh server. Five samples per page are
taken in each cold, immediate-warm and 1.1-second run-index-expiry period: 30 measured HTTP
requests per case. One or four bounded workload processes represent occupied execution
slots; each holds 32 MiB and performs CPU work with a short sleep. The entire ordered run
used one systemd service with `CPUQuota=400%`, `MemoryHigh=768M`, `MemoryMax=1G`, swap
disabled, `TasksMax=128`, and `RuntimeMaxSec=900`. The service completed in 111 seconds,
consumed 218 seconds of CPU, peaked at 441.1 MiB, used no swap, and produced 70,140 KiB of
disposable data and logs.

The exact invocation was:

```bash
baseline_dir=$(mktemp -d /tmp/cg367-base.XXXXXX)
git archive 3043e30257947b039e887a48a3c464873c689c24 | tar -x -C "$baseline_dir"
result_dir=$(mktemp -d /home/joshua/work/operator-test-tmp/cg367.XXXXXX)
systemd-run --user --wait --pipe --collect \
  --property=CPUQuota=400% --property=MemoryHigh=768M \
  --property=MemoryMax=1G --property=MemorySwapMax=0 \
  --property=TasksMax=128 --property=RuntimeMaxSec=900 \
  "$PWD/.venv/bin/python" "$PWD/docs/validation/cg367/reproduce.py" \
  --before-source "$baseline_dir" --after-source "$PWD" \
  --output "$result_dir" --samples 5
```

## Attribution and diagnosis

The benchmark-only server records wall and process CPU time plus spans around task/product
scanning, run-index access, event reads/parses, resource observation, and template rendering.
The requests are serial, and these GET handlers do not acquire the Hub scheduler or action
locks, so request-queue/Hub-lock wait was zero by construction. The run-index span includes
nested calls and therefore is useful for comparison, not additive accounting. The cgroup
reported no CPU throttling in any case; CPU, I/O and memory PSI averages remained 0.00, and
memory PSI totals did not move. Page wall time closely tracked process CPU time.

For the larger history at four occupied slots (15 requests per page across the three
periods), the before profile attributed the following mean request costs:

| page | wall | CPU | event read/parse | run index | task/product scan | resource | template |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Now | 0.697s | 0.697s | 0.116s (1 read/request) | 0.182s | 0.074s | 0.0003s | 0.006s |
| Inbox | 0.904s | 0.904s | 0.398s (4 reads/request) | 0.107s | 0.075s | 0.0003s | 0.007s |

The four Inbox reads parsed the same immutable-in-request event file separately for 24-hour
spend, the merge queue, burn-up and tier bars. This was the largest demonstrated avoidable
cost. Now already used one event snapshot, so no Now-specific caching was justified. Resource
observation and template rendering were negligible; run/history indexing and task scans remain
real costs, but this result does not establish that either is avoidable without weakening
freshness.

The change reads the event log once at the start of the Inbox request and derives each panel
from that same snapshot. It adds no cache or permanent telemetry. A following request still
reads disk, in-process writers remain immediately visible, and cross-process task/control/run
truth keeps its existing invalidation behavior.

## Before and after

Ranges below cover the cold, warm and expiry-period p50s; p95 and max are the worst observed
period for each case. Every row has 15 samples per revision/page. The owner tolerance is 4s;
all observations are below it, but these bounded trials are not a universal speedup claim.

| data / slots / page | before p50 range | before p95 / max | after p50 range | after p95 / max |
| --- | ---: | ---: | ---: | ---: |
| representative / 1 / Now | 0.133–0.142s | 0.246 / 0.246s | 0.136–0.157s | 0.270 / 0.270s |
| representative / 1 / Inbox | 0.190–0.202s | 0.240 / 0.240s | 0.128–0.156s | 0.204 / 0.204s |
| representative / 4 / Now | 0.153–0.158s | 0.288 / 0.288s | 0.138–0.146s | 0.265 / 0.265s |
| representative / 4 / Inbox | 0.202–0.225s | 0.297 / 0.297s | 0.133–0.151s | 0.204 / 0.204s |
| larger / 1 / Now | 0.393–0.662s | 0.707 / 0.707s | 0.429–0.681s | 0.859 / 0.859s |
| larger / 1 / Inbox | 0.709–0.799s | 0.901 / 0.901s | 0.416–0.436s | 0.819 / 0.819s |
| larger / 4 / Now | 0.675–0.829s | 1.419 / 1.419s | 0.414–0.651s | 0.801 / 0.801s |
| larger / 4 / Inbox | 0.714–0.953s | 1.267 / 1.267s | 0.363–0.402s | 0.598 / 0.598s |

In the larger/four-slot after profile, Inbox event parsing fell from 60 reads/5.972s total
to 15 reads/1.306s. Mean Inbox wall/CPU fell from 0.904/0.904s to 0.437/0.437s. Server peak
RSS stayed comparable (162,524 KiB before, 160,140 KiB after); across individual cases it
ranged from 95,764 to 162,524 KiB. Now was not the optimization target, and its variation is
reported rather than attributed to this change. The full JSON retains every period's exact
p50, p95, max, sample count, CPU ticks, peak/final RSS, cgroup counters, span and read count.
