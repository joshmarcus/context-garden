# Production Codex runtime

The production branch of `scripts/managed-worker-bootstrap` installs the complete
official Codex 0.153.4 Linux package after checking its pinned SHA256. A standalone
`codex` executable is insufficient: Code Mode also needs `codex-code-mode-host`.
The package layout under `/opt/codex-0.153.4` retains its manifest, tool host,
packaged `rg`, and sandbox/shell resources. Both CLI entry points are linked from
`/usr/local/bin`; vendor directories remain readable and traversable by the
unprivileged worker even though bootstrap uses a private umask. Dedicated model
credentials remain private and writable by that worker for normal token refresh.

The controller accepts a product repository URL or local checkout. For remote
claims it resolves the controller checkout before fetching the branch head used
by the publication lease. The worker still receives the credential-free clone URL,
not the controller's checkout path or Git credentials.

Verify a real model tool call and a returned artifact after installation. An
authenticated model response alone does not establish that its execution tools
are available, and a live corrective install does not establish a fresh-image
replay of the final bootstrap.

