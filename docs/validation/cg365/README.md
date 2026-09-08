# CG-365 bounded-validation evidence

This artifact records a disposable, finite execution-cgroup exercise. The fixture web
controller remained in the garden service cgroup while two real focused pytest processes
were moved by their supervisors into a transient delegated user execution cgroup. One held
the authoritative heavy slot and the other waited. The retained-history journey then read
Inbox and Now, changed task control, and paused dispatch while the workload was live.

The command in `report.json` creates a transient unit only; it neither changes the garden
service nor increases host limits. It is safe to repeat on a host with systemd user units
and cgroup v2. The test refuses the configured exercise unless each supervisor records
verified cgroup migration.

The 2026-09-07 run used a 20% CPU quota, `MemoryHigh=96MiB`, and `MemoryMax=128MiB`.
Its temporary pytest target allocated 24 MiB and repeatedly touched and hashed that
memory for 2.5 seconds. It wrote a ready marker only after CPU work had begun; the
journey sampled the execution cgroup while that test was still present, alongside the
waiting second validation. The raw cgroup process list, `cpu.stat`, CPU/memory PSI,
memory events, memory use, temporary-space readings, and request latencies are retained
in `report.json`. No new `high`, `oom`, or `oom_kill` event occurred. The temporary unit
was stopped after the test.

## Current-head surface validation

On 2026-09-07, the current integrated head was served from a disposable fixture
controller outside a transient delegated execution cgroup (`CPUQuota=20%`,
`MemoryHigh=96MiB`, `MemoryMax=128MiB`). The fresh captures show the rail and
Configuration page agreeing on `0/1 authoritative (requested 1)` before a
supervisor is active. They label the finite, writable execution cgroup
`available`, rather than claiming it is already enforced; the bounded workload
exercise above verifies the active supervisor transition to `enforced`.

- `rail-1280-light.png` — Inbox rail capacity summary.
- `config-1280-dark.png` — Configuration capacity and cgroup detail.
