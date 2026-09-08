# Host identity boundary

Garden records a logical host alias, never a physical connection target. A tracked
configuration may declare an alias for routing, but its SSH `host` value must be that same
alias. Put the real DNS name, address, account prefix, port, and repository checkout paths in
the ignored `garden.local.yaml` overlay. `garden doctor` rejects tracked SSH host fields that
look like a connection target without echoing the target back to the terminal.

The SSH runner resolves the local target only while it starts the connection. Run metadata,
dispatch events, task transition text, briefs, notifications, and design/export snapshots use
the alias; configured targets and credential-shaped values are scrubbed before entering those
shared surfaces.

The local retention boundary is deliberate: `.garden/runs/` retains the exact worker brief,
SSH command wrapper, stdout and stderr needed to diagnose a run, and `.garden/events.jsonl`
retains scheduler history. Both are ignored local operational records, not committed context or
publication evidence. Local command scripts may contain connection details, so restrict them to
the operator account and remove/archive run records under the garden's retention policy before
transferring any diagnostic bundle.
