"""Run a harness on a remote host over ssh.

Each host in `ssh.hosts` has a clone of each product repo. The runner refreshes that clone
(`git fetch`), creates a worktree on the task branch, pipes the brief in, runs the harness,
commits leftovers and pushes the branch. The local scheduler then fetches the branch and
opens the PR. The remote host needs: git with push access to origin, the harness binary,
and its API credentials.

garden.yaml:

    ssh:
      hosts:
        - name: box1
          host: user@box1.example.com     # anything ssh accepts
          repos: {widget: /srv/repos/widget}
          max_parallel: 4
          harness: claude                 # optional per-host override
      options: ["-o", "BatchMode=yes"]    # extra ssh args
"""

from __future__ import annotations

import json
import os
import posixpath
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Any

from ..runs import Run
from .base import (
    Runner,
    RunnerError,
    config_dir_env,
    config_file_shell,
    pass_env_patterns,
    setup_stamp,
)

REMOTE_SCRIPT = r"""
set -e
REPO={repo}
GARDEN_CHECKOUT_STRATEGY={checkout_strategy}
if [ "$GARDEN_CHECKOUT_STRATEGY" = in_place ]; then WT=$REPO; else WT=$REPO/.garden-worktrees/{task}; fi
BRANCH={branch}
BASE={base}
cd "$REPO"
if [ "$GARDEN_CHECKOUT_STRATEGY" = in_place ] && [ "$REPO" != "$(pwd -P)" ]; then
  echo "canonical checkout root may not contain symlinks or relative components: $REPO" >&2; exit 4
fi
if [ "$GARDEN_CHECKOUT_STRATEGY" = in_place ]; then
  GARDEN_CANONICAL_LEASE="$REPO/.git/garden-canonical-lease"
  GARDEN_ACTIVE_RUN_IDS={active_run_ids}
  # The run store, not the lifetime of this shell, owns the checkout.  In particular, a
  # completed ssh process remains active until the scheduler has collected and fenced it.
  # Refuse that durable evidence even if an interrupted older implementation left no lease.
  if [ -n "$GARDEN_ACTIVE_RUN_IDS" ]; then
    GARDEN_ACTIVE_OWNER=${{GARDEN_ACTIVE_RUN_IDS%% *}}
    echo "canonical checkout is leased by active run $GARDEN_ACTIVE_OWNER" >&2; exit 4
  fi
  if ! mkdir "$GARDEN_CANONICAL_LEASE" 2>/dev/null; then
    GARDEN_LEASE_OWNER=$(cat "$GARDEN_CANONICAL_LEASE/run-id" 2>/dev/null || :)
    case " $GARDEN_ACTIVE_RUN_IDS " in
      *" $GARDEN_LEASE_OWNER "*) echo "canonical checkout is leased by active run $GARDEN_LEASE_OWNER" >&2; exit 4;;
    esac
    if [ -z "$GARDEN_LEASE_OWNER" ]; then echo "canonical checkout has an unreadable lease" >&2; exit 4; fi
    if [ -n "$(git status --porcelain)" ]; then echo "stale canonical lease $GARDEN_LEASE_OWNER preserves uncommitted work; refusing recovery" >&2; exit 4; fi
    GARDEN_STALE_BRANCH=$(git rev-parse --abbrev-ref HEAD)
    if [ "$GARDEN_STALE_BRANCH" != "$BRANCH" ] && [ "$GARDEN_STALE_BRANCH" != "$BASE" ]; then echo "stale canonical lease $GARDEN_LEASE_OWNER has branch drift: $GARDEN_STALE_BRANCH" >&2; exit 4; fi
    rm -f "$GARDEN_CANONICAL_LEASE/run-id" && rmdir "$GARDEN_CANONICAL_LEASE"
    if ! mkdir "$GARDEN_CANONICAL_LEASE" 2>/dev/null; then echo "canonical checkout was claimed during recovery" >&2; exit 4; fi
  fi
  printf '%s\n' {run_id} > "$GARDEN_CANONICAL_LEASE/run-id"
  if [ -n "$(git status --porcelain)" ]; then echo "canonical checkout has uncommitted work; refusing preparation" >&2; exit 4; fi
  GARDEN_CURRENT_BRANCH=$(git rev-parse --abbrev-ref HEAD)
  if [ "$GARDEN_CURRENT_BRANCH" != "$BRANCH" ] && [ "$GARDEN_CURRENT_BRANCH" != "$BASE" ]; then echo "canonical checkout branch drift: $GARDEN_CURRENT_BRANCH" >&2; exit 4; fi
  git fetch --prune origin >&2
  if [ "$GARDEN_CURRENT_BRANCH" = "$BASE" ] && [ "$BRANCH" != "$BASE" ]; then
    if git show-ref --verify --quiet "refs/heads/$BRANCH"; then git checkout -q "$BRANCH" >&2; else git checkout -q -b "$BRANCH" "origin/$BASE" >&2; fi
  fi
else
git fetch --prune origin >&2
git worktree prune >&2
if [ ! -d "$WT/.git" ] && [ ! -f "$WT/.git" ]; then
  if git show-ref --verify --quiet "refs/heads/$BRANCH"; then
    git worktree add "$WT" "$BRANCH" >&2
  elif git show-ref --verify --quiet "refs/remotes/origin/$BRANCH"; then
    git worktree add --track -b "$BRANCH" "$WT" "origin/$BRANCH" >&2
  else
    git worktree add -b "$BRANCH" "$WT" "origin/$BASE" >&2
  fi
fi
fi
cd "$WT"
if [ "$GARDEN_CHECKOUT_STRATEGY" != in_place ]; then git checkout -q "$BRANCH" >&2 || true; fi
if [ "$GARDEN_CHECKOUT_STRATEGY" != in_place ] && git show-ref --verify --quiet "refs/remotes/origin/$BRANCH"; then
  if [ "$(git rev-list --count HEAD..origin/$BRANCH)" != "0" ] && [ "$(git rev-list --count origin/$BRANCH..HEAD)" = "0" ]; then git merge -q --ff-only "origin/$BRANCH" >&2 || true; fi
  if [ "$(git rev-list --count origin/$BRANCH..HEAD)" != "0" ] && [ "$(git rev-list --count HEAD..origin/$BRANCH)" != "0" ]; then git reset -q --hard "origin/$BRANCH" >&2; fi
fi
if [ "$GARDEN_CHECKOUT_STRATEGY" = in_place ]; then
  GARDEN_RUN_DIR="$REPO/.git/garden-run-{run_id}"
else
  GARDEN_RUN_DIR="$WT/.garden-run"
fi
mkdir -p "$GARDEN_RUN_DIR"
cat > "$GARDEN_RUN_DIR/brief.md" <<'GARDEN_BRIEF_EOF'
{brief}
GARDEN_BRIEF_EOF
# The worker (harness) and its setup command run in an allowlisted environment, the same scrub
# the local runner applies (runner.base.PASS_ENV plus worker_env.pass and setup.env): every
# other variable of the remote login environment is dropped, so a remote host's ambient tokens
# (a GitHub token, cloud credentials, an ssh agent) do not reach the worker. HOME is not on the
# allowlist: unless worker_env.pass restores it, it is set to an isolated scratch home beside
# the worktree, so the worker (and a branch's test suite) cannot read the remote login's gh
# token, git credentials or ssh keys out of ~. Only git's own fetch above and push below keep
# the login environment, since the remote host does its own pushing. GARDEN_ROOT points at a
# path with no garden.yaml so any `garden` command the worker runs refuses: find_root() walking
# up from the worktree would otherwise accept the remote product repo's own garden.yaml.
# `setup.env` rides on top, matching runner.base.scrubbed_env.
GARDEN_ENV_ALLOW={env_allow}
if [ "$GARDEN_CHECKOUT_STRATEGY" = in_place ]; then
  GARDEN_WORKER_HOME="$REPO/.git/garden-home-{task}"
else
  GARDEN_WORKER_HOME="$REPO/.garden-worktrees/.garden-home-{task}"
fi
garden_copy_config() {{
  source=$1 destination=$2 required=$3 current="$GARDEN_WORKER_HOME"
  [ ! -L "$current" ] || {{ echo "unsafe worker config HOME" >&2; exit 4; }}
  old_ifs=$IFS; IFS=/
  set -- $destination
  IFS=$old_ifs
  while [ "$#" -gt 1 ]; do
    [ "$1" != . ] && [ "$1" != .. ] && [ -n "$1" ] || exit 4
    current="$current/$1"
    [ ! -L "$current" ] || {{ echo "unsafe worker config destination" >&2; exit 4; }}
    mkdir -p "$current" && chmod 700 "$current"
    shift
  done
  target="$current/$1"
  [ ! -L "$target" ] || {{ echo "unsafe worker config destination" >&2; exit 4; }}
  if [ -f "$source" ]; then
    [ ! -L "$target.garden-new" ] || {{ echo "unsafe worker config temporary destination" >&2; exit 4; }}
    rm -f "$target.garden-new"
    cp "$source" "$target.garden-new" && chmod 600 "$target.garden-new" && mv "$target.garden-new" "$target"
  else
    rm -f "$target"
    [ "$required" = 0 ] || {{ echo "required worker config file is unavailable" >&2; exit 4; }}
  fi
}}
garden_scrub() {{
  garden_harness_key=
  if [ "${{1:-}}" = harness ] && [ -n {api_key_env} ]; then
    eval "garden_harness_key=\${{{api_key_env}:-}}"
  fi
  set -f  # keep `for pat in $GARDEN_ENV_ALLOW` below from globbing a bare `*` against the worktree
  for name in $(env | sed -n 's/^\([A-Za-z_][A-Za-z0-9_]*\)=.*/\1/p'); do
    keep=0
    for pat in $GARDEN_ENV_ALLOW; do case "$name" in $pat) keep=1; break;; esac; done
    [ "$keep" = 1 ] || unset "$name" 2>/dev/null || :
  done
  set +f
  unset CLAUDECODE 2>/dev/null || :
  if [ -z "${{HOME:-}}" ]; then mkdir -p "$GARDEN_WORKER_HOME" 2>/dev/null || :; export HOME="$GARDEN_WORKER_HOME"; fi
  # Build fresh harness homes under the scratch HOME.  The configured locations are sources,
  # never paths handed to the worker: only each harness's login file crosses the boundary.
{config_dirs}
{config_files}
  export GARDEN_TASK_ID={task} GARDEN_RUN_ID={run_id} GARDEN_ROOT="$WT/.garden-no-live-garden"
{setup_env}
  if [ "${{1:-}}" = harness ] && [ -n {api_key_env} ] && [ -n "$garden_harness_key" ]; then
    export {api_key_env}="$garden_harness_key"
  fi
  export GARDEN_VALIDATION_TIMEOUT_SECONDS={validation_timeout}
}}
# Reconciliation is per run, bounded, and followed by the same clean/branch readiness
# checks. It is intentionally separate from the stamped one-time setup below.
GARDEN_RECONCILE_CMD={reconcile_cmd}
GARDEN_RECONCILE_TIMEOUT={reconcile_timeout}
if [ "$GARDEN_CHECKOUT_STRATEGY" = in_place ] && [ -n "$GARDEN_RECONCILE_CMD" ]; then
  if ! command -v timeout >/dev/null 2>&1 || ! timeout --version >/dev/null 2>&1; then echo "canonical reconciliation requires working timeout(1)" >&2; exit 4; fi
  GARDEN_RECONCILE_RUN="timeout $GARDEN_RECONCILE_TIMEOUT sh -c"
  if ! ( garden_scrub; $GARDEN_RECONCILE_RUN "$GARDEN_RECONCILE_CMD" >&2 ); then echo "canonical reconciliation failed or timed out" >&2; exit 4; fi
  if [ -n "$(git status --porcelain)" ] || [ "$(git rev-parse --abbrev-ref HEAD)" != "$BRANCH" ]; then echo "canonical checkout not ready after reconciliation" >&2; exit 4; fi
fi
# Run the setup command once per worktree (again only when it changes, tracked by a marker kept
# beside the worktree so `git add -A` above cannot commit it) in the scrubbed environment. A
# setup failure fails the run before any push.
GARDEN_SETUP_CMD={setup_cmd}
GARDEN_SETUP_STAMP={setup_stamp}
GARDEN_SETUP_TIMEOUT={setup_timeout}
GARDEN_SETUP_MARKER="$REPO/.garden-worktrees/.garden-setup-{task}"
if [ -n "$GARDEN_SETUP_CMD" ] && [ "$(cat "$GARDEN_SETUP_MARKER" 2>/dev/null)" != "$GARDEN_SETUP_STAMP" ]; then
  if command -v timeout >/dev/null 2>&1; then GARDEN_SETUP_RUN="timeout $GARDEN_SETUP_TIMEOUT sh -c"; else GARDEN_SETUP_RUN="sh -c"; fi
  if ( garden_scrub; $GARDEN_SETUP_RUN "$GARDEN_SETUP_CMD" >&2 ); then printf '%s' "$GARDEN_SETUP_STAMP" > "$GARDEN_SETUP_MARKER"; else echo "garden setup command failed (or timed out after ${{GARDEN_SETUP_TIMEOUT}}s)" >&2; exit 3; fi
fi
set +e
( garden_scrub harness; {harness} < "$GARDEN_RUN_DIR/brief.md" )
RC=$?
set -e
rm -rf "$GARDEN_RUN_DIR"
if [ -n "$(git status --porcelain)" ]; then git add -A >&2; git -c user.name=garden -c user.email=garden@localhost commit -q -m "{task}: leftover changes from run {run_id}" >&2 || true; fi
if [ "$(git rev-list --count origin/$BASE..HEAD)" != "0" ]; then git push -u --force-with-lease origin "HEAD:refs/heads/$BRANCH" >&2; fi
exit $RC
"""


class SSHRunner(Runner):
    name = "ssh"
    remote = True

    def hosts(self) -> list[dict[str, Any]]:
        return list(self.config.get("hosts") or [])

    def assign(self, run: Run, active: list[Run]) -> None:
        """Least-loaded host that has a clone of this product (run.host may be preset, e.g. to
        resume a session that lives on that host)."""
        product = str(getattr(run, "product", "") or self.config.get("_product") or "")
        candidates = [h for h in self.hosts() if not product or product in (h.get("repos") or {})]
        if run.host:
            candidates = [h for h in candidates if h.get("name") == run.host] or candidates
        if not candidates:
            raise RunnerError(f"no ssh host has a repo for product {product!r}")
        load = {h["name"]: 0 for h in candidates}
        for r in active:
            if r.run_id != run.run_id and r.runner == self.name and r.host in load:
                load[r.host] += 1
        free = [h for h in candidates if load[h["name"]] < int(h.get("max_parallel", 1))]
        if not free:
            raise RunnerError("all ssh hosts are at max_parallel")
        run.host = min(free, key=lambda h: load[h["name"]])["name"]

    def _host(self, name: str) -> dict[str, Any]:
        for h in self.hosts():
            if h.get("name") == name:
                return h
        raise RunnerError(f"unknown ssh host {name!r}")

    def canonical_checkout_identity(self, run: Run) -> str:
        """Return the durable identity of this run's provisioned checkout."""
        host = self._host(run.host)
        product = str(self.config.get("_product") or "")
        repo = str((host.get("repos") or {}).get(product) or "").strip()
        if not repo:
            raise RunnerError(f"host {run.host} has no repo path for product {product!r}")
        # Use the configured SSH destination rather than its garden-local display name so
        # duplicate host entries that point at the same machine still arbitrate together.
        destination = str(host.get("host") or "").strip()
        return json.dumps([destination, posixpath.normpath(repo)], separators=(",", ":"))

    def _setup_for(self, host: dict[str, Any]) -> dict[str, Any]:
        """The product's `setup` block, with a per-host `setup` override merged on top
        (env keys merge, other keys replace) so a host can differ in how it prepares the env."""
        setup = dict(self.config.get("setup") or {})
        override = host.get("setup")
        if isinstance(override, dict) and override:
            env = {**(setup.get("env") or {}), **(override.get("env") or {})}
            setup = {**setup, **override, "env": env}
        return setup

    def start(self, run: Run, worktree: Path, brief_text: str) -> None:
        if self.harness is None:
            raise RunnerError("ssh runner needs a harness")
        host = self._host(run.host)
        product = str(self.config.get("_product") or "")
        repo = (host.get("repos") or {}).get(product)
        if not repo:
            raise RunnerError(f"host {run.host} has no repo path for product {product!r}")
        d = run.path
        (d / "brief.md").write_text(brief_text)
        if "GARDEN_BRIEF_EOF" in brief_text:
            raise RunnerError("brief contains the heredoc delimiter")
        harness_cmd = self.harness_shell(run, None)
        api_key_env = self.harness.api_key_env
        if api_key_env and (not api_key_env.replace("_", "a").isalnum() or api_key_env[0].isdigit()):
            raise RunnerError("harness api_key_env must be an environment variable name")
        setup = self._setup_for(host)
        checkout = dict(self.config.get("checkout") or {})
        setup_cmd = str(setup.get("command") or "").strip()
        setup_env = "\n".join(
            f"  export {k}={shlex.quote(str(v))}" for k, v in (setup.get("env") or {}).items()
        )
        config_sources = config_dir_env(self.config)
        config_dirs = "\n".join([
            '  rm -rf "$GARDEN_WORKER_HOME/.claude" "$GARDEN_WORKER_HOME/.codex"',
            '  mkdir -p "$GARDEN_WORKER_HOME/.claude" "$GARDEN_WORKER_HOME/.codex"',
            f'  if [ -f {shlex.quote(config_sources["CLAUDE_CONFIG_DIR"] + "/.credentials.json")} ]; then cp {shlex.quote(config_sources["CLAUDE_CONFIG_DIR"] + "/.credentials.json")} "$GARDEN_WORKER_HOME/.claude/.credentials.json"; fi',
            f'  if [ -f {shlex.quote(config_sources["CODEX_HOME"] + "/auth.json")} ]; then cp {shlex.quote(config_sources["CODEX_HOME"] + "/auth.json")} "$GARDEN_WORKER_HOME/.codex/auth.json"; fi',
            '  export CLAUDE_CONFIG_DIR="$GARDEN_WORKER_HOME/.claude" CODEX_HOME="$GARDEN_WORKER_HOME/.codex"',
            *[f'  export {variable}={shlex.quote(source)}'
              for variable, source in config_sources.items()
              if variable not in {"CLAUDE_CONFIG_DIR", "CODEX_HOME"}],
        ])
        env_allow = shlex.quote(" ".join(pass_env_patterns(self.config)))
        script = REMOTE_SCRIPT.format(
            repo=shlex.quote(str(repo)), task=run.task_id, branch=shlex.quote(run.branch), base=shlex.quote(run.base),
            brief=brief_text, harness=harness_cmd, run_id=run.run_id, env_allow=env_allow,
            config_dirs=config_dirs, setup_env=setup_env, setup_cmd=shlex.quote(setup_cmd),
            config_files=config_file_shell(self.config),
            setup_stamp=shlex.quote(setup_stamp(setup_cmd) if setup_cmd else ""),
            setup_timeout=shlex.quote(str(int(setup.get("timeout_seconds") or 600))),
            validation_timeout=shlex.quote(str(
                int(self.config.get("checks", {}).get("timeout_seconds", 900) or 900)
            )),
            checkout_strategy=shlex.quote(str(checkout.get("strategy") or "worktree")),
            reconcile_cmd=shlex.quote(str(checkout.get("reconcile_command") or "").strip()),
            reconcile_timeout=shlex.quote(str(int(checkout.get("reconcile_timeout_seconds") or 600))),
            active_run_ids=shlex.quote(" ".join(str(item) for item in
                                                run.env_snapshot.get("canonical_active_run_ids", []))),
            api_key_env=api_key_env,
        )
        (d / "remote.sh").write_text(script)
        ssh_bin = str(self.config.get("ssh_bin") or "ssh")
        opts = [str(o) for o in (self.config.get("options") or ["-o", "BatchMode=yes"])]
        ssh_cmd = " ".join(shlex.quote(c) for c in [ssh_bin, *opts, str(host["host"]), "sh", "-s"])
        timeout_min = float(self.config.get("timeout_minutes", 90) or 0)
        if timeout_min and shutil.which("timeout"):
            ssh_cmd = f"timeout {timeout_min * 60:g} {ssh_cmd}"
        wrapper = (
            f"{ssh_cmd} < {shlex.quote(str(d / 'remote.sh'))} > {shlex.quote(str(d / 'stdout.json'))} "
            f"2> {shlex.quote(str(d / 'stderr.log'))}; echo $? > {shlex.quote(str(d / 'exit_code'))}"
        )
        env = dict(os.environ)
        env.pop("CLAUDECODE", None)
        proc = subprocess.Popen(["sh", "-c", wrapper], env=env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        run.pid = proc.pid
        run.status = "running"
        run.harness = self.harness.name
        run.save()
        (d / "command.txt").write_text(wrapper + "\n")

    def collect(self, run: Run) -> dict[str, Any]:
        assert self.harness is not None
        return self.harness.parse(run.stdout_text(), run.stderr_text(), None, model=run.model)

    def doctor(self) -> list[str]:
        probs = []
        if not self.hosts():
            probs.append("ssh runner: no hosts configured under ssh.hosts")
        if not shutil.which(str(self.config.get("ssh_bin") or "ssh")):
            probs.append("ssh binary not found")
        return probs
