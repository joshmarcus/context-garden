# CG-380 controller, scheduler, and worker attribution

Measured on 2026-09-07 against build `37f659fab4480efe1c29c07ac996b6f0a2899c1b`.
The machine-readable samples, raw request spans, process identities, cgroup membership and
counter snapshots are in `report.json`.

## Scope and method

`reproduce.py` extends CG-367's deterministic retained-history garden (100 tasks, 1,549
runs, 12,500 events). It starts a one-process Uvicorn server and takes seven serial samples
for each of Now 1, Inbox and Config in cold, immediate-warm and 1.1-second index-expiry
periods. Cases have zero, one or four occupied slots. GET handlers do not acquire the Hub
tick/action locks and requests are serial, so queue and Hub-lock wait are zero by
construction; wall minus process CPU was below timer noise in the zero-slot profile.

The slot occupants are explicit bounded replays, not model agents: each allocates 32 MiB,
does deterministic CPU work and sleeps intermittently for six seconds. The CI-wait replay
allocates the same memory and sleeps. Setup is a one-process `compileall`; validation is the
single focused resource-headroom pytest. These represent process/resource competition and
short-lived descendants, not vendor CLI, GitHub-network, setup-download, or full-suite cost.
`RUSAGE_CHILDREN` includes reaped descendants; every direct child's PID and cgroup are
recorded. Its `ru_maxrss` is a cumulative high-water mark and is not summed or interpreted
as aggregate memory.

The exact measured invocation was:

```bash
result_dir=$(mktemp -d /home/joshua/work/operator-test-tmp/cg380-final.XXXXXX)
"$GARDEN_VALIDATION_RUNNER" -m garden.validation -- \
  systemd-run --user --wait --pipe --collect \
  --property=CPUQuota=400% --property=MemoryHigh=768M \
  --property=MemoryMax=1G --property=MemorySwapMax=0 \
  --property=TasksMax=128 --property=RuntimeMaxSec=300 \
  "$PWD/.venv/bin/python" "$PWD/docs/validation/cg380/reproduce.py" \
  --source "$PWD" --output "$result_dir" --samples 7
```

It ran from 12:45:40Z to 12:46:41Z. The service used 49.104 CPU-seconds in 61.054 wall
seconds and peaked at 332.2 MiB. It wrote 14,708 KiB. CPU throttling was zero. CPU PSI
some/full totals rose 17.731/17.730 ms, I/O PSI 0.034/0.034 ms, memory PSI did not move,
and memory high/max/OOM/OOM-kill deltas were all zero. `memory.current` began/ended at
35,995,648/39,030,784 bytes and `memory.peak` was 348,356,608. Final `memory.stat` was anon
16,138,240, file 16,297,984, shmem 262,144, inactive_file 331,776 and active_file
15,704,064 bytes. Temp headroom began/ended at 2,000,801,792/2,000,539,648 bytes.

## Results

The table pools the three periods (21 samples per row); `report.json` retains period-level
p50/p95/max and each cold first sample.

| occupied slots | page | p50 | p95 | max |
| ---: | --- | ---: | ---: | ---: |
| 0 | Now 1 | .123s | .178s | .226s |
| 0 | Inbox | .120s | .173s | .181s |
| 0 | Config | .041s | .048s | .051s |
| 1 | Now 1 | .121s | .175s | .222s |
| 1 | Inbox | .121s | .173s | .189s |
| 1 | Config | .044s | .050s | .053s |
| 4 | Now 1 | .123s | .183s | .239s |
| 4 | Inbox | .124s | .177s | .180s |
| 4 | Config | .044s | .051s | .051s |

Cold first samples were .226/.137/.051 seconds at zero slots and .239/.151/.051 at four.
Nine background no-dispatch ticks were .237-.268 seconds. Thus this bounded four-slot
replay did not produce material request or tick degradation, and all samples were far below
the owner's four-second operational tolerance. It does not establish production tail
latency under real simultaneous model and CI processes.

At zero slots, mean request CPU was .130s Now, .128s Inbox and .041s Config. Mean spans
(nested run-index time is non-additive) were:

| page | task/product scan | event read/parse | run index | process/resource inspection | rendering |
| --- | ---: | ---: | ---: | ---: | ---: |
| Now 1 | .052s | .017s | .007s | .0004s | .003s |
| Inbox | .052s | .016s | .007s | .0004s | .005s |
| Config | .026s | — | ~0s | .0004s | .001s |

The matched uninstrumented zero-slot server measured Now/Inbox/Config pooled p50 at
.120/.119/.041s versus .123/.120/.041s instrumented. P95 was .177/.168/.049s versus
.178/.173/.048s. The observed profiler p50 increment was therefore 0-3 ms; max variation
was larger and bidirectional, so no correction was applied.

Workload attribution shows why parent CPU cannot stand for total work. One/four session
replays used 1.774/7.333 descendant CPU-seconds over 6.132/6.145 seconds. One/four CI-wait
replays used only .082/.422 CPU-seconds over 6.103/6.158 seconds. Setup used .040 CPU-seconds
and .065 wall seconds; the focused validation used .482 CPU-seconds and .515 wall seconds.
The controller page and tick costs above remain separate from those descendant totals.

## Conclusions and next action

Confirmed: retained task/product scanning is the largest named controller span (40% of
Now/Inbox request CPU in this fixture); rendering and resource inspection are negligible.
CPU-active workers dominate total service CPU as slots fill, while CI-wait occupancy costs
memory/slots but little CPU. CG-367's single event-history read remains effective. Filesystem
I/O wait versus parsing inside each Python read is not separable with this instrumentation,
and real vendor/network/session cost is unmeasured.

The cache-heavy admission stop is also confirmed as a conservative policy outcome, not
host exhaustion: `_cgroup_memory_status` subtracts all `memory.current` from the tighter
soft/hard ceiling. Applied to the operator sample, roughly 2,198 MiB file and 58 MiB shmem
were counted identically to 251 MiB anon, leaving 550 MiB reported headroom despite 5,621
MiB host `MemAvailable`, zero pressure/events and no throttling. This does **not** prove all
file cache reclaimable, and this low-cache experiment cannot safely calibrate a discount.

The highest-value next controller action is a focused Store scan profile: count stats and
YAML parses per request, then reuse an immutable request-local store snapshot only if it
preserves cross-process freshness. Separately, admission policy needs a precise bounded
follow-up: under the existing hard cap, populate disposable file cache, record
`inactive_file`/working-set and PSI/events, issue bounded `memory.reclaim`, and compare
reclaimed bytes plus a one-slot launch high-water mark. Only that evidence can justify a
guarded reclaimable-cache allowance; hard memory caps, PSI/OOM safeguards, the 1,536 MiB
reserve, and four-slot cap must remain. No speculative caching or admission change is made.

## Reproduction

Run the command above from the repository root. The script creates only its output tree,
starts no model agents, and never reads or modifies the production garden. `serve_plain.py`
is the matched profiler-overhead control; `serve_profiled.py` contains the request spans.
