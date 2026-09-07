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

    def _record_tool_upgrade(self, *, product: str, repo: Any, base: str, task_id: str = "") -> None:
        """Record the configured tool base tip as an authorized update.

        A fetch alone is not authorization: only the configured base branch of the product
        marked ``provides_tool`` is followed, and it must advance the installed commit.
        """
        new_sha = gitops.git("rev-parse", gitops.base_ref(repo, base), cwd=repo).strip()
        if not new_sha:
            return
        current = self.upgrader.installed_commit() or ""
        pending = self.upgrade_available() or {}
        if new_sha == current or new_sha == pending.get("sha"):
            return
        if current:
            merge_base = gitops.git("merge-base", current, new_sha, cwd=repo, check=False).strip()
            if merge_base != current:
                self.log(f"{product}: configured base {base} at {new_sha[:12]} does not advance active {current[:12]}; not authorized")
                return
            # ``git`` returns an empty string for both a successful quiet command and a
            # failed one, so ask rev-list whether the configured base actually advanced.
            count_text = gitops.git("rev-list", "--count", f"{current}..{new_sha}",
                                    cwd=repo, check=False).strip()
            try:
                count = int(count_text)
            except ValueError:
                count = 0
            if count <= 0:
                return
        else:
            count = None
        ctrl = self.control()
        ctrl["upgrade"] = {"sha": new_sha, "from": current, "count": count,
                           "product": product, "url": self._tool_url(product),
                           "base": base, "at": now_iso(), "status": "available"}
        self.events.emit("upgrade_available", task_id or product, sha=new_sha[:12],
                         product=product, base=base,
                         **({"count": count} if count is not None else {}))
        self.log(f"{product}: tool update available at {new_sha[:12]} from {base}"
                 + (f" ({count} commit(s) since {current[:12]})" if count is not None else ""))
        notify(self.cfg.data, task_id or product, "upgrade_available",
               f"tool update available: {new_sha[:12]}", "")

    def _note_tool_upgrade(self, task: Task) -> None:
        """A PR merged into the tool's own product: record the new base sha so the Inbox,
        `garden status` and `garden upgrade` can move the pinned install forward."""
        repo = self.repo_for(task)
        gitops.fetch(repo)
        base = self.final_base_for(task)
        self._record_tool_upgrade(product=task.product, repo=repo, base=base, task_id=task.id)

    def detect_tool_upgrade(self) -> None:
        """Notice base advances even when this controller missed the merge transition."""
        if not self.cfg.upgrade_auto():
            return
        product = self.cfg.tool_product()
        if not product:
            return
        probe = Task(path=self.store.root, id=f"_{product}", title="", product=product, phase="")
        repo = self.repo_for(probe)
        gitops.fetch(repo)
        self._record_tool_upgrade(product=product, repo=repo,
                                  base=self.cfg.product_base_branch(product), task_id=probe.id)

    def upgrade_available(self) -> dict[str, Any] | None:
        """The pending tool upgrade recorded by a merge, or None."""
        u = self.control().get("upgrade")
        if isinstance(u, dict) and u.get("sha"):
            return dict(u)
        return None

    def upgrade_status(self) -> dict[str, Any]:
        """One operator-facing account of the active and pending/last build."""
        installed = self.upgrader.installed_commit() or ""
        info = self.upgrade_available() or {}
        return {"active": installed, **info}

    def confirm_restarted_upgrade(self) -> None:
        """The replacement controller confirms that the requested build is now active."""
        info = self.upgrade_available()
        if not info or info.get("status") != "restart_pending":
            return
        installed = self.upgrader.installed_commit() or ""
        sha = str(info.get("sha") or "")
        if installed == sha or (installed and sha.startswith(installed)):
            self.control()["last_upgrade"] = {**info, "status": "active", "active": installed,
                                               "completed_at": now_iso()}
            self.control().pop("upgrade", None)
            self.state.save()
            self.events.emit("upgrade_active", str(info.get("product") or "tool"), sha=sha[:12])
            self.events.emit("upgraded", str(info.get("product") or "tool"), sha=sha[:12],
                             **({"count": info.get("count")} if info.get("count") is not None else {}))
            self.log(f"tool upgrade active at {sha[:12]}; restarted controller confirmed")
            return
        info.update(status="failed", reason="restarted process serves an unexpected build",
                    diagnosis=f"expected {sha[:12]}, active metadata reports {installed[:12] or 'unversioned'}")
        self.control()["upgrade"] = info
        self.state.save()
        self.events.emit("upgrade_failed", str(info.get("product") or "tool"), sha=sha[:12], reason="restart verify")
        self.log(str(info["diagnosis"]))

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
        old_sha = self.upgrader.installed_commit() or str(info.get("from") or "")
        info.update(status="installing", reason="installing verified configured-base update")
        self.control()["upgrade"] = info
        self.state.save()
        ok, output = self.upgrader.install(url, sha)
        if not ok:
            self.events.emit("upgrade_failed", product, sha=sha[:12], reason="install")
            self.log(f"tool upgrade to {sha[:12]} failed to install; keeping the current install")
            info.update(status="failed", reason="install failed", diagnosis=output[-2000:])
            self.control()["upgrade"] = info
            self.state.save()
            return {"ok": False, "reason": "install failed", "output": output}
        installed = self.upgrader.installed_commit() or ""
        if not (installed == sha or (installed and sha.startswith(installed))):
            self.events.emit("upgrade_failed", product, sha=sha[:12], installed=installed[:12], reason="verify")
            self.log(f"tool upgrade verify failed: installed {installed[:12] or '?'} != {sha[:12]}; keeping the current install")
            self._rollback_upgrade(info, url, old_sha, "verify failed")
            return {"ok": False, "reason": "verify failed", "installed": installed}
        if not self.upgrader.doctor_ok():
            self.events.emit("upgrade_failed", product, sha=sha[:12], reason="doctor")
            self.log(f"tool upgrade to {sha[:12]} installed but `garden doctor` failed; not restarting")
            self._rollback_upgrade(info, url, old_sha, "doctor failed")
            return {"ok": False, "reason": "doctor failed"}
        info.update(status="restart_pending", reason="waiting for controller restart")
        self.control()["upgrade"] = info
        self.state.save()
        self.events.emit("upgrade_restart_pending", product, sha=sha[:12])
        self.log(f"tool installed at {sha[:12]}" + (" — restart pending" if restart else ""))
        if restart and self._restarter is not None:
            try:
                self._restarter()
            except Exception as e:
                self._rollback_upgrade(info, url, old_sha, f"restart failed: {e}")
                return {"ok": False, "reason": f"restart failed: {e}"}
        return {"ok": True, "sha": sha, "restarted": bool(restart)}

    def _rollback_upgrade(self, info: dict[str, Any], url: str, old_sha: str, reason: str) -> None:
        recovered = False
        diagnosis = reason
        if old_sha:
            recovered, output = self.upgrader.install(url, old_sha)
            if not recovered:
                diagnosis += f"; rollback to {old_sha[:12]} failed: {output[-1000:]}"
        info.update(status="failed", reason=reason, diagnosis=diagnosis,
                    recovered=bool(recovered), active=old_sha if recovered else "")
        self.control()["upgrade"] = info
        self.state.save()

    def pin(self, sha: str, url: str, product: str = "") -> dict[str, Any]:
        """Queue a canaried pin for the long-lived controller's next tick boundary.

        A one-shot ``garden pin`` process is not the scheduler and must never re-exec itself.
        The controller consumes this request after its own tick, where its restart callback is
        the service/watch process rather than the pin command.
        """
        self.control()["upgrade"] = {"sha": sha, "url": url, "product": product,
                                      "at": now_iso(), "restart": True, "pinned": True}
        self.state.save()
        return {"ok": True, "sha": sha, "pending": True}

    def maybe_auto_upgrade(self, rep: TickReport) -> None:
        """On an idle tick, install a pending tool upgrade if config `upgrade: auto` is set."""
        info = self.upgrade_available()
        # A pin is an explicit command, not an auto-upgrade.  It is consumed by this
        # controller only after _tick_body has returned and while tick.lock is still held.
        if info and info.get("pinned"):
            if info.get("status") in {"restart_pending", "installed"}:
                return
            if self.runs.active():
                return  # Defer the install/restart until workers and checks have drained.
            result = self.upgrade(restart=True)
            if result.get("ok"):
                rep.transitions.append("pinned tool installed")
            else:
                rep.errors.append(f"pinned tool install failed: {result.get('reason')}")
            return
        if not self.cfg.upgrade_auto():
            return
        if not info or info.get("status") in {"failed", "restart_pending", "installed"}:
            return
        if self.is_dispatch_paused():
            info.update(status="held", reason="dispatch is paused; resume to authorize automatic installation")
            self.control()["upgrade"] = info
            self.state.save()
            return
        if self.active_runs():
            info.update(status="held", reason=f"draining {len(self.active_runs())} active worker/check run(s)")
            self.control()["upgrade"] = info
            self.state.save()
            return
        try:
            result = self.upgrade(restart=True)
        except Exception as e:  # noqa: BLE001
            rep.errors.append(f"auto-upgrade failed: {e}")
            return
        if result.get("ok"):
            rep.transitions.append("tool upgraded")
