# Remote workers (ssh runner)

`runner: ssh` (garden-wide, per product, or per task) runs the harness on one of the
hosts under `ssh.hosts`. Each host entry has an ssh destination, a `repos` map from product
to a clone path on that host, and `max_parallel`. The scheduler picks the least-loaded
host that has a clone for the product.

The run is one ssh session: the local wrapper pipes a generated script (which embeds the
brief) to `sh -s` on the host. The script refreshes the clone (`git fetch --prune`),
creates or reuses a worktree on the task branch under `<repo>/.garden-worktrees/<ID>`,
runs the harness with the brief on stdin, commits leftovers, and pushes the branch when
there are commits. Harness output comes back on stdout into the run's `stdout.json`; git
noise goes to stderr.

On reap, the scheduler fetches the branch, requires commits ahead of base, materialises a
local worktree from `origin/<branch>` (so the review pass and revise runs can use it), and
opens the PR from local exactly as for local runs. Revise runs on an ssh task go back to a
remote host, which fast-forwards its worktree to `origin/<branch>` first.

Requirements on each host: git with push access to origin, the harness binary and its
credentials, and non-interactive ssh (`BatchMode=yes` is the default option).
