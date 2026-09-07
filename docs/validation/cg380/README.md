# CG-380 controller, scheduler, and worker attribution

Measured 2026-09-07 23:27:33Z–23:31:11Z against source commit
`b701aa915271ab4152cfae8a04afc42230d1fe99`. `report.json` retains every request,
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

The unit used 259.808 CPU-seconds in 218.022 seconds, peaked at 357.3 MiB, and wrote
19,724 KiB. One of 2,008 CPU periods throttled for .093s; CPU PSI some/full rose
41.610/41.610ms, I/O PSI 22.614/22.614ms, and memory PSI rose 10.070/10.070ms. Memory
high/max/OOM/OOM-kill deltas were zero. Initial/final `memory.current` was
15,503,360/48,234,496 bytes; peak was 374,693,888. Final anon/file/shmem/inactive-file
were 16,838,656/21,753,856/20,459,520/4,096 bytes. Temp headroom fell from
2,135,285,760 to 2,115,231,744 bytes.

## Served results

Pooled empirical p50/p95/max over 21 requests (raw samples retain each expiry cycle):

<!-- report-latency-start -->
| tasks | slots | Now 1 | Inbox | Config |
| ---: | ---: | ---: | ---: | ---: |
| 100 | 0 | .129/.182/.272s | .133/.180/.210s | .044/.058/.083s |
| 100 | 1 | .133/.183/.222s | .127/.180/.184s | .044/.081/.082s |
| 100 | 4 | .142/.182/.254s | .144/.291/.315s | .048/.083/.091s |
| 1,000 | 0 | .733/.787/.793s | .783/1.041/1.179s | .331/.404/.406s |
| 1,000 | 1 | .733/.801/.839s | .770/.904/1.006s | .321/.378/.382s |
| 1,000 | 4 | .744/.800/.968s | .753/.848/.851s | .328/.393/.400s |
<!-- report-latency-end -->

All samples stayed below the owner's four-second tolerance. History size, not slot count,
dominates. Serial GETs do not queue and handlers do not take Hub locks, so request/Hub-lock
wait is zero by construction. Mean request wall and CPU agree within .5ms; filesystem
syscall wait cannot be separated from Python parsing by these spans.

| tasks | page | task/product scan | event parse | run index | resource inspect | render |
| ---: | --- | ---: | ---: | ---: | ---: | ---: |
| 100 | Now 1 | .055s | .017s | .006s | .0004s | .004s |
| 100 | Inbox | .056s | .016s | .006s | .0004s | .005s |
| 100 | Config | .027s | — | .002s | .0004s | .001s |
| 1,000 | Now 1 | .603s | .017s | .037s | .0006s | .004s |
| 1,000 | Inbox | .603s | .017s | .029s | .0005s | .055s |
| 1,000 | Config | .298s | — | .012s | .0006s | .001s |

Matched uninstrumented 100-task p50 was .139/.138/.048s versus .129/.133/.044s
instrumented. The instrumented run was 10/5/4ms faster; p95 also varied bidirectionally,
so overhead is below run variation and no correction is applied.

## Scheduler phases

Each expiry cycle launched a fresh no-dispatch tick. `profile_tick.py` retains native phase
wall/CPU plus phase-local counters. Lock acquisition is separate. Fixture tasks have no
PRs, so GitHub/network waits are unsupported; model/worker-completion waits are replayed.

<!-- report-tick-start -->
| tasks | slots | tick wall min/p50/max | tick CPU p50 | controller-other wall/CPU p50 | scan wall p50 | reap wall p50 |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 100 | 0 | .135/.143/.229s | .142s | .090/.090s | .053s | .052s |
| 100 | 1 | .145/.153/.162s | .153s | .093/.093s | .056s | .057s |
| 100 | 4 | .160/.184/.721s | .183s | .110/.110s | .060s | .073s |
| 1,000 | 0 | .743/.828/1.000s | .827s | .706/.706s | .608s | .120s |
| 1,000 | 1 | .713/.735/1.452s | .734s | .669/.669s | .594s | .064s |
| 1,000 | 4 | .669/.683/.770s | .683s | .631/.630s | .570s | .051s |
<!-- report-tick-end -->

`controller-other` is measured total minus named phases. Every sample records exactly two
full scans there: pre-body and post-resource refresh. `reap` contains three run-index checks.
The JSON separately retains `poll`, `base_reprobe`, `merge_queue`, `retro_close`,
`harness_probe`, `tool_update`, and `audit`; medians are each <=1ms. CPU essentially equals
wall. Uncontended lock wait is below 1ms. Initialization/save remain honestly aggregated in
controller-other rather than falsely assigned.

## Worker and conclusion

One/four CPU-active replays used 1.717/6.809 descendant CPU-seconds over 6.131/6.135s;
CI-wait replays used .074/.372 CPU-seconds over 6.081/6.132s. Setup used .255 CPU/.265s
wall; focused validation 1.327 CPU/1.368s wall. Short-lived descendants are included, and
parent controller CPU is not substituted for them.

Confirmed bottleneck: scanning is the largest request span and dominant tick cost, growing
from about .066s to .6s per full scan at ten times the tasks. Rendering, event parsing,
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
