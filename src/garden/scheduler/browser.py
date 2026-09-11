"""Admission gate for work whose acceptance requires browser captures."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from pathlib import Path
from typing import Any

from ..browser import probe_browser_runtime
from ..criteria import browser_capture_authorized
from ..model import Task, now_iso


class BrowserMixin:
    def browser_authorized(self, task: Task) -> bool:
        """Project policy or explicit task evidence may authorize browser work."""
        return self.cfg.browser_enabled(task.product) or browser_capture_authorized(
            task.body, task.extra.get("requires")
        )

    def browser_check_authorized(self, task: Task, specs: list[dict[str, Any]]) -> bool:
        """Include a deliberately configured Playwright/UI check as scoped authority."""
        return self.browser_authorized(task) or any(
            spec.get("python") == "garden.walkthrough:ui_check"
            or "-m garden.walkthrough --ui-check" in str(spec.get("command") or "")
            for spec in specs
        )

    def capture_required(self, task: Task) -> bool:
        required = browser_capture_authorized(task.body, task.extra.get("requires"))
        configured = any(
            spec.get("python") == "garden.walkthrough:ui_check"
            or "-m garden.walkthrough --ui-check" in str(spec.get("command") or "")
            for spec in self._pre_pr_specs(task)
        )
        # Advisory mode still runs and records the generated UI check. It only avoids holding
        # the worker before that check can produce its preserved diagnostic/fallback artifacts.
        return (required or configured) and self.cfg.capture_infrastructure_policy() == "require"

    def _browser_probe_signature(self, task: Task) -> str:
        setup = self.cfg.product_setup(task.product)
        passed = ((self.cfg.data.get("worker_env") or {}).get("pass") or [])
        relevant = {key: value for key, value in os.environ.items()
                    if key in {"PATH", "LD_LIBRARY_PATH", "PLAYWRIGHT_BROWSERS_PATH"}}
        value = {"worker_env_pass": passed, "setup_env": setup.get("env") or {}, "environment": relevant}
        return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()

    def browser_ready_for(self, task: Task) -> bool:
        """Use one cached, bounded launch result; retry after config/env change or cadence."""
        key = f"browser:{task.product}"
        state = self.control().setdefault("browser_readiness", {})
        previous = dict(state.get(key) or {})
        signature = self._browser_probe_signature(task)
        retry_seconds = int(self.cfg.get("browser_readiness.retry_seconds", 300) or 300)
        try:
            checked = dt.datetime.fromisoformat(str(previous.get("checked_at") or ""))
            age = (dt.datetime.now(dt.UTC) - checked).total_seconds()
        except ValueError:
            age = retry_seconds
        if previous.get("signature") == signature and (previous.get("ready") or age < retry_seconds):
            if not previous.get("ready"):
                self.state.get(task.id)["infrastructure_hold"] = {
                    "kind": previous.get("kind"), "diagnostic": previous.get("diagnostic")}
                self.state.save()
            return bool(previous.get("ready"))

        worktree = Path(self.cfg.garden_dir) / "browser-probe" / task.product
        worktree.mkdir(parents=True, exist_ok=True)
        result = probe_browser_runtime(self.cfg.data, setup=self.cfg.product_setup(task.product),
                                       worktree=worktree,
                                       timeout=int(self.cfg.get("browser_readiness.timeout_seconds", 20) or 20))
        entry: dict[str, Any] = {**result, "signature": signature, "checked_at": now_iso()}
        hold = self.config_hold()
        if not result.get("ready") and "worker_env.pass" in (hold.get("keys") or []):
            entry["diagnostic"] = (str(entry.get("diagnostic") or "") +
                                   " A worker_env.pass reload is currently held; accept that supported config reload before retrying.")
        state[key] = entry
        if result.get("ready"):
            self.state.get(task.id).pop("infrastructure_hold", None)
            self.state.save()
            if previous and not previous.get("ready"):
                self.events.emit("browser_ready", task.id, product=task.product)
                self.log(f"browser capture runtime ready for {task.product}; capture-dependent work resumes")
            return True
        self.state.get(task.id)["infrastructure_hold"] = {
            "kind": entry.get("kind"), "diagnostic": entry.get("diagnostic")}
        self.state.save()
        if not previous or previous.get("signature") != signature or previous.get("ready"):
            self.events.emit("browser_unready", task.id, product=task.product,
                             failure_kind=entry.get("kind"), diagnostic=entry.get("diagnostic"))
            self.log(f"browser capture runtime unavailable for {task.product}: {entry.get('diagnostic')}")
        return False
