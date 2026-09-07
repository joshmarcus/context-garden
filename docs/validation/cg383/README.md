# CG-383 reclaimable-file-cache calibration

This disposable cgroup experiment distinguishes disk cache from tmpfs shared memory.
It changes no production admission policy or cache. The original `report.json` is retained
as historical evidence: its tempfile backing was unspecified and it omitted `shmem`, so
its EAGAIN result cannot establish that disk-backed cache is unreclaimable. Its 8 MiB
allocation was synthetic, not a representative garden worker.

The corrected reproducer requires an explicit fixture directory and records its filesystem,
shared-memory accounting, swap cap, invocation and source digest. `memory.reclaim` is a
best-effort request, not a guarantee or strict upper bound on bytes reclaimed. EAGAIN is
recorded rather than converted into assumed capacity. A failed validation command fails
the experiment. Existing production files are never touched.

## Reproduction

Run from this worktree on Linux with cgroup v2 and user delegation. The fixture directory
must already exist. Execute serially; use a unique unit name for each invocation.

```bash
systemd-run --user --wait --pipe --collect --unit=operator-cg383-disk \
  -p Delegate=yes -p CPUQuota=100% -p MemoryHigh=96M -p MemoryMax=128M \
  -p MemorySwapMax=0 -p RuntimeMaxSec=90 /usr/bin/python3 \
  "$PWD/docs/validation/cg383/reproduce.py" \
  --fixture-root /home/joshua/work/operator-test-tmp \
  --output "$PWD/docs/validation/cg383/disk-report.json"
```

For the matched tmpfs run change the unit to `operator-cg383-tmpfs`, fixture root to
`/tmp` and output to `tmpfs-report.json`. The retained roots are explicitly ext4 and tmpfs.
Both use 72 MiB payload / 64 MiB reclaim request / 8 MiB synthetic child.

A separate real focused validation used the owner's production memory ceilings, a 256 MiB
payload and 192 MiB reclaim request. Swap was disabled, which is stricter than production.
The command was:

```bash
systemd-run --user --wait --pipe --collect --unit=operator-cg383-validation \
  -p Delegate=yes -p CPUQuota=200% -p MemoryHigh=4608M -p MemoryMax=5G \
  -p MemorySwapMax=0 -p RuntimeMaxSec=90 --working-directory="$PWD" \
  --setenv=PYTHONPATH="$PWD/src" --setenv=PYTHONDONTWRITEBYTECODE=1 \
  --setenv=TMPDIR=/home/joshua/work/operator-test-tmp \
  /home/joshua/garden/.venv/bin/python "$PWD/docs/validation/cg383/reproduce.py" \
  --fixture-root /home/joshua/work/operator-test-tmp --cache-mib 256 --reclaim-mib 192 \
  --output "$PWD/docs/validation/cg383/validation-report.json" \
  --slot-command /home/joshua/garden/.venv/bin/python -m pytest \
  tests/scheduler/test_resources.py -q -p no:cacheprovider
```

## Measurements (2026-09-07)

| Fixture | File-memory reduction after reclaim | Peak of whole experiment | Result |
| --- | ---: | ---: | --- |
| ext4, 72 MiB payload | 64.19 MiB | 87.7 MiB | reclaim succeeded |
| tmpfs, 72 MiB payload | 0 MiB | 94.2 MiB | EAGAIN; payload accounted as shmem |
| ext4 + resource tests, 256 MiB payload | 192.21 MiB | 275.7 MiB | reclaim succeeded; 12 tests passed |

Every run had zero high/max/OOM increments and zero memory PSI increments across reclaim
and child execution, with zero swap. JSON reports retain exact byte values and interpreter
build. `validation-output.txt` retains the test output. The cgroup peak includes population,
so it is an upper bound for the child stage, not an isolated child RSS measurement. The
resource test run stayed far below the 1536 MiB admission reserve; this does not validate
five simultaneous workers, a full suite, or reclaim latency under production pressure.
The implementation base was 4c5a956 (the source modules were unchanged by this experiment).

## Disposition and next implementation

Reject both unconditional subtraction of `memory.stat:file` and the original conclusion
that cache cannot provide headroom. tmpfs contributes to `file` but cannot be discarded as
disk cache; inactive_file alone is also an estimate, not admission credit.

The evidence supports a bounded reclaim attempt followed by a fresh ordinary headroom
check under the existing admission lock. Only attempt it when file cache is the limiting
factor and host memory, temp space, slot count and pressure checks permit recovery. Never
count requested reclaim bytes as available memory; failure, short reclaim, stale/missing
readings and concurrent growth must retain the normal stop. Keep memory.high, memory.max,
OOM safeguards, reserve and shared-slot limits unchanged. Apply to the actual limiting
controller/execution cgroup and bound the operation so it cannot block web requests.

That product change requires separate implementation and regression checks; this PR
completes calibration only. There has been no production cache reclaim or cap bypass.
`measured-reproducer.txt` is the exact source matching the report digests. The current
reproducer differs only in import spacing and the equivalent datetime.UTC alias.

Operator self-review corrected the storage ambiguity, synthetic-worker claim and reclaim
upper-bound wording; raw prior evidence remains available.
