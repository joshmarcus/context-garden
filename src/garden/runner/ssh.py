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

import os
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Any

from ..runs import Run
from .base import Runner, RunnerError

REMOTE_SCRIPT = r"""
set -e
REPO={repo}
WT=$REPO/.garden-worktrees/{task}
BRANCH={branch}
BASE={base}
cd "$REPO"
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
cd "$WT"
git checkout -q "$BRANCH" >&2 || true
if git show-ref --verify --quiet "refs/remotes/origin/$BRANCH"; then
  if [ "$(git rev-list --count HEAD..origin/$BRANCH)" != "0" ] && [ "$(git rev-list --count origin/$BRANCH..HEAD)" = "0" ]; then git merge -q --ff-only "origin/$BRANCH" >&2 || true; fi
  if [ "$(git rev-list --count origin/$BRANCH..HEAD)" != "0" ] && [ "$(git rev-list --count HEAD..origin/$BRANCH)" != "0" ]; then git reset -q --hard "origin/$BRANCH" >&2; fi
fi
mkdir -p .garden-run
cat > .garden-run/brief.md <<'GARDEN_BRIEF_EOF'
{brief}
GARDEN_BRIEF_EOF
# Prevent the worker from finding and mutating a live garden: if the remote product repo is
# itself a garden, find_root() walking up from the worktree would otherwise accept its
# garden.yaml. GARDEN_ROOT points at a path with no garden.yaml, so any `garden` command refuses.
export GARDEN_TASK_ID={task} GARDEN_RUN_ID={run_id} GARDEN_ROOT="$WT/.garden-no-live-garden"
set +e
{harness} < .garden-run/brief.md
RC=$?
set -e
rm -rf .garden-run
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
            if r.runner == self.name and r.host in load:
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
        script = REMOTE_SCRIPT.format(
            repo=shlex.quote(str(repo)), task=run.task_id, branch=shlex.quote(run.branch), base=shlex.quote(run.base),
            brief=brief_text, harness=harness_cmd, run_id=run.run_id,
        )
        (d / "remote.sh").write_text(script)
        ssh_bin = str(self.config.get("ssh_bin") or "ssh")
        opts = [str(o) for o in (self.config.get("options") or ["-o", "BatchMode=yes"])]
        ssh_cmd = " ".join(shlex.quote(c) for c in [ssh_bin, *opts, str(host["host"]), "sh", "-s"])
        timeout_min = int(self.config.get("timeout_minutes", 90) or 0)
        if timeout_min and shutil.which("timeout"):
            ssh_cmd = f"timeout {timeout_min * 60} {ssh_cmd}"
        wrapper = (
            f"{ssh_cmd} < {shlex.quote(str(d / 'remote.sh'))} > {shlex.quote(str(d / 'stdout.json'))} "
            f"2> {shlex.quote(str(d / 'stderr.log'))}; echo $? > {shlex.quote(str(d / 'exit_code'))}"
        )
        env = dict(os.environ)
        env.pop("CLAUDECODE", None)
        proc = subprocess.Popen(["sh", "-c", wrapper], env=env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        run.pid = proc.pid
        run.harness = self.harness.name
        run.save()
        (d / "command.txt").write_text(wrapper + "\n")

    def collect(self, run: Run) -> dict[str, Any]:
        assert self.harness is not None
        return self.harness.parse(run.stdout_text(), run.stderr_text(), None)

    def doctor(self) -> list[str]:
        probs = []
        if not self.hosts():
            probs.append("ssh runner: no hosts configured under ssh.hosts")
        if not shutil.which(str(self.config.get("ssh_bin") or "ssh")):
            probs.append("ssh binary not found")
        return probs
