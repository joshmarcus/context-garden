"""Self-upgrade of the pinned tool install when a PR merges into the product that provides it."""

from __future__ import annotations

from typing import Any

from .. import gitops
from ..model import Task, now_iso
from ..notify import notify
from .report import TickReport


class UpgradeMixin:
    # ---- self-upgrade (the pinned tool install) ----------------------------
    def _tool_url(self, product: str) -> str:
        repo = self.cfg.product_repo(product)
        return repo if isinstance(repo, str) else str(repo)

    def _note_tool_upgrade(self, task: Task) -> None:
        """A PR merged into the tool's own product: record the new base sha so the Inbox,
        `garden status` and `garden upgrade` can move the pinned install forward."""
        repo = self.repo_for(task)
        gitops.fetch(repo)
        base = self.final_base_for(task)
        new_sha = gitops.git("rev-parse", gitops.base_ref(repo, base), cwd=repo).strip()
        if not new_sha:
            return
        current = self.upgrader.installed_commit() or ""
        if new_sha == current:
            return  # already on it (e.g. an editable install, or a re-merge)
        count = None
        if current:
            try:
                count = int(gitops.git("rev-list", "--count", f"{current}..{new_sha}", cwd=repo, check=False).strip() or 0)
            except (gitops.GitError, ValueError):
                count = None
        ctrl = self.control()
        ctrl["upgrade"] = {"sha": new_sha, "from": current, "count": count,
                           "product": task.product, "url": self._tool_url(task.product), "at": now_iso()}
        self.events.emit("upgrade_available", task.id, sha=new_sha[:12], product=task.product,
                         **({"count": count} if count is not None else {}))
        self.log(f"{task.product}: tool update available at {new_sha[:12]}"
                 + (f" ({count} PR(s) since {current[:12]})" if count is not None else ""))
        notify(self.cfg.data, task.id, "upgrade_available", f"tool update available: {new_sha[:12]}", task.pr or "")

    def upgrade_available(self) -> dict[str, Any] | None:
        """The pending tool upgrade recorded by a merge, or None."""
        u = self.control().get("upgrade")
        if isinstance(u, dict) and u.get("sha"):
            return dict(u)
        return None

    def upgrade(self, restart: bool = False) -> dict[str, Any]:
        """Install the pending tool sha, verify the installed commit and `garden doctor`,
        then (optionally) restart the loop. A failed install or verify leaves the current
        install running: the running process is never re-exec'd unless everything passed."""
        info = self.upgrade_available()
        if not info:
            return {"ok": False, "reason": "no tool upgrade available"}
        sha, url, product = str(info["sha"]), str(info.get("url") or ""), str(info.get("product") or "")
        if not url:
            return {"ok": False, "reason": "the tool product has no install URL"}
        ok, output = self.upgrader.install(url, sha)
        if not ok:
            self.events.emit("upgrade_failed", product, sha=sha[:12], reason="install")
            self.log(f"tool upgrade to {sha[:12]} failed to install; keeping the current install")
            return {"ok": False, "reason": "install failed", "output": output}
        installed = self.upgrader.installed_commit() or ""
        if not (installed == sha or (installed and sha.startswith(installed))):
            self.events.emit("upgrade_failed", product, sha=sha[:12], installed=installed[:12], reason="verify")
            self.log(f"tool upgrade verify failed: installed {installed[:12] or '?'} != {sha[:12]}; keeping the current install")
            return {"ok": False, "reason": "verify failed", "installed": installed}
        if not self.upgrader.doctor_ok():
            self.events.emit("upgrade_failed", product, sha=sha[:12], reason="doctor")
            self.log(f"tool upgrade to {sha[:12]} installed but `garden doctor` failed; not restarting")
            return {"ok": False, "reason": "doctor failed"}
        self.control().pop("upgrade", None)
        self.state.save()
        self.events.emit("upgraded", product, sha=sha[:12], **({"count": info.get("count")} if info.get("count") is not None else {}))
        self.log(f"tool upgraded to {sha[:12]}" + (" — restarting the loop" if restart else ""))
        if restart and self._restarter is not None:
            self._restarter()
        return {"ok": True, "sha": sha, "restarted": bool(restart)}

    def maybe_auto_upgrade(self, rep: TickReport) -> None:
        """On an idle tick, install a pending tool upgrade if config `upgrade: auto` is set."""
        if not self.cfg.upgrade_auto() or self.is_dispatch_paused():
            return
        if not self.upgrade_available() or self.active_runs():
            return
        try:
            result = self.upgrade(restart=True)
        except Exception as e:  # noqa: BLE001
            rep.errors.append(f"auto-upgrade failed: {e}")
            return
        if result.get("ok"):
            rep.transitions.append("tool upgraded")
