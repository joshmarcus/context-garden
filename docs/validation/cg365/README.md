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

The 2026-09-06 run used a 20% CPU quota, `MemoryHigh=96MiB`, and `MemoryMax=128MiB`.
The web/control requests all completed below two seconds, the waiting validation was
observed, and `memory.events` reported no new `high`, `oom`, or `oom_kill` event. The
temporary unit was stopped after the test.
