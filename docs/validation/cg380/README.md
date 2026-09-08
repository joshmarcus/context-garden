# CG-380 controller, scheduler, and worker attribution

Measured 2026-09-08 02:37:16Z–02:40:31Z against source commit
`2ff015bb9a63877d8f9b0970611b551bc312eb54`. `report.json` retains every request,
tick-phase sample, PID/cgroup identity, counter snapshot, and the structured served-app
interaction (source head, actions, and observations).

## Method and bounds

`reproduce.py` extends CG-367's retained-history fixture. Disposable gardens contain 100
and 1,000 tasks respectively, each with 1,549 run records and 12,500 events. A one-process
Uvicorn app serves seven serial GETs for Now 1, Inbox, and Config in each of three
1.1-second cache-expiry cycles, with zero, one, and four occupied slots. Occupants are
120-second bounded replays kept alive for the flow, then stopped: each allocates 32 MiB and
alternates deterministic CPU work with short sleeps. They are not model agents.

The separate CI-wait replay sleeps after the same allocation; setup is one `compileall`;
validation is one focused resource test. These represent contention and descendant
accounting, not vendor/network, download, or full-suite cost. Direct child PIDs/cgroups and
reaped-descendant CPU are retained. `ru_maxrss` is a high-water mark, never summed or
described as aggregate memory.

```bash
result_dir=$(mktemp -d /tmp/cg380-final.XXXXXX)
"$GARDEN_VALIDATION_RUNNER" -m garden.validation -- \
  systemd-run --user --wait --pipe --collect \
  --property=CPUQuota=400% --property=MemoryHigh=768M \
  --property=MemoryMax=1G --property=MemorySwapMax=0 \
  --property=TasksMax=128 --property=RuntimeMaxSec=600 \
  "$PWD/.venv/bin/python" "$PWD/docs/validation/cg380/reproduce.py" \
  --source "$PWD" --output "$result_dir" --samples 7
```

The unit used 227.019 CPU-seconds in 195.000 seconds, peaked at 356.4 MiB, and wrote
19,724 KiB. None of 1,800 CPU periods throttled; CPU PSI some/full rose
25.976/25.976ms, I/O PSI .004/.004ms, and memory PSI did not rise. Memory
high/max/OOM/OOM-kill deltas were zero. Initial/final `memory.current` was
15,294,464/46,968,832 bytes; peak was 373,706,752. Final anon/file/shmem/inactive-file
were 16,863,232/20,463,616/20,459,520/4,096 bytes. Temp headroom fell from
3,200,143,360 to 3,179,683,840 bytes.

## Served results

Pooled empirical p50/p95/max over 21 requests (raw samples retain each expiry cycle):

<!-- report-latency-start -->
| tasks | slots | Now 1 | Inbox | Config |
| ---: | ---: | ---: | ---: | ---: |
| 100 | 0 | .125/.188/.230s | .124/.171/.187s | .043/.055/.081s |
| 100 | 1 | .124/.161/.242s | .124/.161/.166s | .043/.052/.056s |
| 100 | 4 | .128/.167/.232s | .122/.174/.186s | .044/.053/.083s |
| 1,000 | 0 | .624/.676/.725s | .657/.680/.714s | .294/.337/.338s |
| 1,000 | 1 | .719/.766/.786s | .751/.785/.790s | .320/.375/.383s |
| 1,000 | 4 | .659/.730/.759s | .679/.713/.753s | .300/.350/.370s |
<!-- report-latency-end -->

All samples stayed below the owner's four-second tolerance. History size, not slot count,
dominates. Serial GETs do not queue and handlers do not take Hub locks, so request/Hub-lock
wait is zero by construction. Mean request wall and CPU agree within .5ms; filesystem
syscall wait cannot be separated from Python parsing by these spans.

Cold is the first request for a page after each cache-expiry sleep; warm is the remaining
six requests in that same cycle. This zero-slot comparison avoids mixing cache state with
replay contention:

<!-- report-cold-warm-start -->
| tasks | page | cold (n/p50/p95/max) | warm (n/p50/p95/max) |
| ---: | --- | ---: | ---: |
| 100 | Now 1 | 3/.188/.230/.230s | 18/.125/.138/.138s |
| 100 | Inbox | 3/.127/.187/.187s | 18/.120/.171/.171s |
| 100 | Config | 3/.044/.055/.055s | 18/.042/.081/.081s |
| 1,000 | Now 1 | 3/.658/.725/.725s | 18/.620/.676/.676s |
| 1,000 | Inbox | 3/.625/.660/.660s | 18/.657/.714/.714s |
| 1,000 | Config | 3/.291/.311/.311s | 18/.294/.338/.338s |
<!-- report-cold-warm-end -->

Cold samples are not consistently slower than warm samples: the dominant task scan runs
on every request, so the measured in-process expiry does not provide a meaningful warm-page
latency benefit.

| tasks | page | task/product scan | event parse | run index | resource inspect | render |
| ---: | --- | ---: | ---: | ---: | ---: | ---: |
| 100 | Now 1 | .054s | .017s | .006s | .0004s | .004s |
| 100 | Inbox | .054s | .015s | .006s | .0004s | .005s |
| 100 | Config | .026s | — | .002s | .0004s | .001s |
| 1,000 | Now 1 | .514s | .017s | .026s | .0004s | .004s |
| 1,000 | Inbox | .512s | .015s | .019s | .0004s | .035s |
| 1,000 | Config | .261s | — | .013s | .0004s | .001s |

Matched uninstrumented 100-task p50 was .121/.122/.041s versus .125/.124/.043s
instrumented. The instrumented run was 4/2/2ms slower; p95 varied bidirectionally,
so overhead is below run variation and no correction is applied.

## Scheduler phases

Each expiry cycle launched a fresh no-dispatch tick. `profile_tick.py` retains native phase
wall/CPU plus phase-local counters. Lock acquisition is separate. Fixture tasks have no
PRs, so GitHub/network waits are unsupported; model/worker-completion waits are replayed.

<!-- report-tick-start -->
| tasks | slots | tick wall min/p50/max | tick CPU p50 | controller-other wall/CPU p50 | scan wall p50 | reap wall p50 |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 100 | 0 | .112/.120/.120s | .120s | .092/.091s | .053s | .027s |
| 100 | 1 | .111/.113/.115s | .113s | .087/.087s | .051s | .026s |
| 100 | 4 | .116/.116/.119s | .116s | .089/.089s | .052s | .027s |
| 1,000 | 0 | .594/.621/.630s | .621s | .591/.590s | .536s | .027s |
| 1,000 | 1 | .604/.675/.688s | .674s | .644/.643s | .577s | .029s |
| 1,000 | 4 | .616/.621/.658s | .621s | .592/.591s | .536s | .028s |
<!-- report-tick-end -->

`controller-other` is measured total minus named phases. Every sample records exactly two
full scans there: pre-body and post-resource refresh. `reap` contains three run-index checks.
The JSON separately retains `poll`, `base_reprobe`, `merge_queue`, `retro_close`,
`harness_probe`, `tool_update`, and `audit`; medians are each <=1ms. CPU essentially equals
wall. Uncontended lock wait is below 1ms. Initialization/save remain honestly aggregated in
controller-other rather than falsely assigned.

## Worker and conclusion

One/four CPU-active replays used 1.738/7.323 descendant CPU-seconds over 6.125/6.127s;
CI-wait replays used .096/.463 CPU-seconds over 6.126/6.127s. Setup used .038 CPU/.064s
wall; focused validation .528 CPU/.565s wall. Short-lived descendants are included, and
parent controller CPU is not substituted for them.

Confirmed bottleneck: scanning is the largest request span and dominant tick cost, growing
from about .054s to .51s per full scan at ten times the tasks. Rendering, event parsing,
run indexing, resource inspection, and lock wait are secondary. CPU-active occupants
dominate total workload CPU but four replays did not systematically degrade latency under
the 400% cap. Real model/network traffic and production tails remain unmeasured.

The highest-value next action is a focused Store correction: count stats/YAML parses, then
reuse a request/tick-local immutable task snapshot while preserving cross-process freshness.
This evidence identifies the target but does not prove a safe correction, so none is made.

The cache-heavy admission stop is separately confirmed conservative: current cgroup
headroom counts 2,198MiB file and 58MiB shmem like 251MiB anon despite 5,621MiB host
availability and zero pressure/events. That does not prove cache reclaimable. Before a
guarded allowance, measure a bounded hard-cap `memory.reclaim` and one-slot launch,
inactive-file working set, PSI/events, reclaimed bytes, and high-water mark. Preserve hard
caps, pressure/OOM safeguards, 1,536MiB reserve, and the four-slot cap.

Run `.venv/bin/python docs/validation/cg380/verify_report.py` to recompute the generated
tables from `report.json`. Reproduction creates only its output tree, starts no agents, and
does not touch production.
