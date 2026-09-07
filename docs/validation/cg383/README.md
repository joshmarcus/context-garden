# CG-383 reclaimable-file-cache calibration

This is a disposable cgroup-v2 experiment, not an admission-policy change. It fills a
temporary 72 MiB file, samples the cgroup working-set fields and pressure counters, asks
the kernel to reclaim at most 64 MiB through `memory.reclaim`, then starts one 40 MiB
allocation as a representative slot. The temporary file is deleted automatically; the
fixture neither touches garden state nor deletes a production cache.

The command below creates a delegated transient user unit with the existing 96 MiB soft
limit, 128 MiB hard limit, disabled swap, and a 90-second lifetime. The unit is collected
after the script exits.

```bash
systemd-run --user --wait --pipe --collect --property=Delegate=yes \
  --property=MemoryHigh=96M --property=MemoryMax=128M \
  --property=MemorySwapMax=0 --property=TasksMax=64 --property=RuntimeMaxSec=90 \
  "$PWD/.venv/bin/python" "$PWD/docs/validation/cg383/reproduce.py" \
  --output "$PWD/docs/validation/cg383/report.json"
```

`report.json` retains the exact limits, all four samples, and deltas for `memory.current`,
the file/anonymous working-set fields, `memory.events`, and memory PSI totals.

## Disposition

The measurement shows that bounded `memory.reclaim` releases file-backed pages before the
one-slot launch without an OOM event. That is not sufficient to grant extra admission
capacity: reclaim is best-effort, the amount reclaimed is workload-dependent, and the
current scheduler cannot atomically reclaim and publish a run across a configured shared
slot cap. Keep the existing hard/soft caps, event stop, and `resources.max_parallel` gate
unchanged. A later policy change needs a separate guarded design with an admission-lock
recheck, an explicit conservative allowance, and regression coverage for reclaim failure.
