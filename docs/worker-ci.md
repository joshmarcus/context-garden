# Repository validation policies

Validation is selected per product and is independent of where its Git repository is
hosted. New configurations set `products.<name>.validation` to one of these policies:

- `actions` requires the exact-head GitHub checks rollup and may use this repository's
  branch-publishing Actions helper when `setup.worker_push: true` is also explicit.
- `status` requires the exact-head GitHub checks rollup populated by another installed CI
  or check provider. Garden reads the provider-neutral rollup and never starts Actions.
- `command` binds validation to a configured command instead of a checks rollup. With
  `run_by: worker` (the default) the gate is a Garden-authored exact-head validation
  receipt for that command; with `run_by: scheduler` the controller runs the command
  itself and reads one exact-head answer back from it. Either way the pass belongs to one
  exact commit and is asked for again after a scheduler rebase.
- `none` explicitly declares that there is no external CI service. Configured pre-PR
  tests, lint, review rejection, conflict detection, and atomic merge guards still apply;
  an absent external status is neither polled nor treated as evidence.

The omitted setting retains the historical behavior for existing gardens. Prefer an
explicit policy for new products. Examples:

```yaml
products:
  actions-app:
    validation: actions
    setup:
      worker_push: true
      test: python3 scripts/check_ci.py
  enterprise-app:
    validation: status
  appliance:
    validation:
      provider: command
      command: ./ci/validate-exact-head
  documentation:
    validation: none
```

For `actions` and `status`, an absent result remains unknown and blocks automatic merge.
The Inbox distinguishes a missing Actions run from a missing alternate-provider status;
provider access errors remain permission/API errors, a pending rollup remains pending,
and a failed required rollup enters the normal failure/revision path. Enterprise API
routing and credentials come only from the product's explicit GitHub host configuration.

## A validation command run by the scheduler

Some CI systems publish no status Garden can read on the PR: the result lives in that
system and has to be asked for. `run_by: scheduler` makes one configured command the whole
provider boundary, so the CI system's vocabulary, hosts and credentials stay in the
operator's own wrapper (or in gitignored local configuration) and never in Garden.

```yaml
products:
  appliance:
    validation:
      provider: command
      run_by: scheduler
      command: ./ci/head-status      # a wrapper the operator owns
      timeout_seconds: 60            # optional; default 120, must be in (0, 3600]
```

Garden appends the candidate commit as the command's final argument — never a branch name,
because a branch moves — and expects one JSON object on stdout:

```json
{"sha": "<the commit it was asked about>",
 "state": "success | failure | pending | missing | unavailable",
 "exists_for_sha": true,
 "stale": false,
 "evidence_url": "<optional link a person can open>",
 "failures": ["<optional short reasons>"]}
```

Only `state: success` with `exists_for_sha: true` and `stale: false`, echoing the commit it
was asked about, is a pass. A pass must state those two facts explicitly, so a result the
wrapper knows to be superseded, or one that belongs to no build for this head, cannot
become this head's evidence by omission. Everything else fails closed: `failure`,
`pending`, `missing`, `unavailable`, an answer about another commit, an unknown state,
unparsable or oversized output, a nonzero exit, a command that cannot be run, and a query
that exceeds its timeout. `evidence_url` and `failures` are truncated and redacted before
they are stored, and a nonzero exit contributes only its exit status, so the command's own
diagnostics cannot leak into Garden's state or its pages.

The command is a status read, not a build: Garden never triggers or reruns anything, and
the worker is neither asked to run this command nor given permission to publish. Within
one scheduler tick each commit is asked about once, and the answer is not carried into the
next tick, so a build that finishes meanwhile is seen on the next pass.

For the operator the distinction that matters is between an answer and no answer. `pending`
is quiet waiting and resolves itself; a `failure` bound to this head routes into the
ordinary revision path; an answer bound to no current result for this head ("CI status
missing") and a provider that could not answer at all ("CI status unavailable") both stop
the merge and ask for a person, and the Inbox names which one it is. `tests/fake_ci_command.py`
is a token-free stand-in that can produce every one of these outcomes, so a configuration
can be exercised end to end before a real CI system or credential is involved.

## This repository's GitHub Actions helper

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
and push-event runs from `api.github.com`; it polls no more often than every 65 seconds.
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
    validation: actions
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
