# Worker tests on GitHub CI

For this repository, `python scripts/check_ci.py` runs the full suite on GitHub before
finishing a worker session. Run focused tests and lint locally, self-review, commit all
intended changes, then run the helper in the foreground and read its result. If CI fails,
fix the failure and check the new commit before reporting done. Include the final SHA,
run URL and result with the ordinary acceptance evidence. Do not start another local
full suite while waiting. UI changes still need actual application journey evidence.

The helper non-force pushes only the current `garden/` or `codex/` branch to origin.
It never changes git configuration or creates/updates PRs. CI runs on pushes to these
branches, so a PR is not needed. The helper reuses the latest push run for that exact
branch and SHA, including a pending run. Pushing an unchanged commit creates no new run.
Failed, skipped, cancelled, absent, pending or stale results are not a pass. Timeouts
exit nonzero; an unavailable GitHub service does not justify claiming tests passed.
It does not automatically rerun failed CI. Diagnose the log before requesting a rerun.
The existing full PR CI still tests integration with the base and gates merging.

## Public repository workers without `gh`

An AWS worker with only this public repository's SSH deploy key may use the helper
without an operator GitHub token or a `gh` installation. When authenticated `gh` is
unavailable, the helper reads only the public `ci.yml` workflow's exact branch, commit,
and push-event runs from `api.github.com`; it polls no more often than once per minute.
The official `ssh://git@ssh.github.com:443/OWNER/REPO.git` push transport is normalized
to the `github.com` API identity. Other hosts, enterprise instances, custom HTTPS ports,
and credential-bearing URLs still require authenticated `gh` and never use this fallback.

The REST response, rate-limit header, workflow result, and remote branch tip are all
validated. A malformed response, exhausted rate limit, API error, missing run, pending
run, failed conclusion, changed checkout, or moved remote branch exits nonzero. The
fallback grants no GitHub write access beyond the already-scoped deploy key used for the
assigned-branch push.

## Enable in a garden

Push permission is opt-in for each product; other products keep the no-push rule:

```yaml
products:
  context-garden:
    setup:
      worker_push: true
      test: python3 scripts/check_ci.py
      lint: .venv/bin/ruff check src tests scripts
      env:
        GH_CONFIG_DIR: /path/to/explicitly-authorized-gh-config
checks:
  pre_pr:
    - name: lint
      command: .venv/bin/ruff check src tests scripts
```

The explicit pre-PR list avoids repeating the full suite locally after the worker has
validated its commit remotely. Keep full PR CI required by the merge gate; the worker's
claim alone must not authorize a merge. This repository-specific setup leaves base probes
and scratch-merge checks focused on local lint, while GitHub tests PR integration.
Existing UI evidence checks still apply. The explicit pre-PR list is garden-wide: do not
copy this example into a multi-product garden without accounting for every product's
local checks. Opted-in products cannot automerge while the PR has no CI result.

`worker_push` is a brief permission, not a new credential or sandbox enforcement layer.
Provide a repository-scoped GitHub credential where available. Workers retain their private
HOME; explicitly configure `setup.env.GH_CONFIG_DIR` for saved `gh` authentication, or
allow `GH_TOKEN` through `worker_env.pass`. Never put tokens in tracked YAML. The helper's
credential configuration is command-local and works with HTTPS origin without `gh auth
setup-git`. SSH origins additionally require explicitly authorized SSH authentication.
This does not grant workers permission to edit other branches, rewrite shared history,
modify the controller or manage PRs. Rebase-only runs retain the existing runner-owned
force-push protocol.

The helper is repository tooling, not a GitHub dependency of the garden package. Other
products can use another CI provider's equivalent command with the same explicit permission
and exact-commit contract. Configure and validate the real worker environment before
resuming dispatch; changing garden configuration requires a drained restart. Existing
branches must incorporate the CI workflow and helper before using this command. An old
branch without the workflow cannot start branch CI just by pushing.
