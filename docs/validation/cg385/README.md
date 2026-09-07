# CG-385 bounded-reclaim admission validation

The focused regressions exercise the shared worker/reviewer/check admission lock, sole-gate
selection, bounded single-helper launch, cooldown, partial reclaim, fresh-headroom admission,
and changed-cgroup failure. The retained-history journey runs real supervised focused pytest
processes (not synthetic allocations) while requesting the Inbox, Now page and two control
routes. Its generated `real-workload-report.json` records route latency, the cgroup launch high
water, `memory.stat` including `shmem` and `inactive_file`, memory events and PSI before/after.

The real workload is run in a disposable systemd user unit with a 1536 MiB soft ceiling,
2 GiB hard ceiling, no swap and a 90-second runtime cap:

```bash
systemd-run --user --wait --pipe --collect --unit=operator-cg385-validation \
  -p Delegate=yes -p CPUQuota=200% -p MemoryHigh=1536M -p MemoryMax=2G \
  -p MemorySwapMax=0 -p RuntimeMaxSec=90 --working-directory="$PWD" \
  --setenv=PYTHONPATH="$PWD/src" --setenv=PYTHONDONTWRITEBYTECODE=1 \
  --setenv=CG385_REPORT="$PWD/docs/validation/cg385/real-workload-report.json" \
  "$PWD/.venv/bin/python" -m pytest \
  tests/test_web.py::test_retained_history_journey_stays_responsive_with_running_and_waiting_pytest \
  -q -s -p no:cacheprovider
```

This validation creates only disposable pytest fixtures. It does not delete production cache,
change a production cap or claim that synthetic requested reclaim bytes are available memory.
