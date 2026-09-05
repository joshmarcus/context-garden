"""Budgets, the dispatch pause and live config overrides: what a person can turn without a restart."""

from __future__ import annotations

from typing import Any

from ..model import Task, now_iso
from ..notify import notify


class BudgetMixin:
    # ---- budgets -----------------------------------------------------------
    def budget_for(self, task_or_key: Task | str) -> float:
        """The USD cap for a phase. A `_budgets` override in state.json (set from the web UI or
        `garden budget`) wins over garden.yaml, so a running scheduler picks up a change on the
        next tick (state is re-read every pass). An override value of null means "no budget"
        (cap removed) and beats any configured cap."""
        key = task_or_key if isinstance(task_or_key, str) else task_or_key.key
        product = key.split("/", 1)[0]
        overrides = self.state.get("_budgets")
        if key in overrides:
            return float(overrides[key] or 0.0)
        b = (self.cfg.get("budgets", {}) or {}).get(key)
        if b is None:
            b = self.cfg.product(product).get("budget_usd")
        return float(b or 0.0)

    def set_budget(self, key: str, usd: float | None, by: str = "cli") -> None:
        """Set (`usd`) or remove (`usd=None`, i.e. "no budget") a phase's cap, overriding
        garden.yaml. Stored in `.garden/state.json` under `_budgets`; the running scheduler
        re-reads state each tick, so a raised or removed cap unpauses dispatch on the next pass.
        Also clears the `budget_hit` marker so the pause state resets and a later re-exceed can
        notify again."""
        overrides = self.state.get("_budgets")
        overrides[key] = None if usd is None else round(float(usd), 2)
        self.state.get(f"_phase:{key}").pop("budget_hit", None)
        self.state.save()
        self.events.emit("budget_set", "", phase=key, budget=overrides[key], by=by)
        shown = "none" if usd is None else f"${float(usd):.2f}"
        self.log(f"{key}: budget set to {shown} by {by}")

    def spent_for(self, key: str) -> float:
        ids = {t.id for t in self.store.tasks().values() if t.key == key}
        return round(sum(r.cost_usd or 0.0 for r in self.runs.all_runs() if r.task_id in ids), 4)

    def budget_exceeded(self, task: Task) -> bool:
        budget = self.budget_for(task)
        if not budget:
            return False
        spent = self.spent_for(task.key)
        if spent < budget:
            return False
        marker = self.state.get(f"_phase:{task.key}")
        if not marker.get("budget_hit"):
            marker["budget_hit"] = now_iso()
            self.events.emit("budget", "", phase=task.key, spent=spent, budget=budget)
            self.log(f"{task.key}: budget ${budget:.2f} exceeded (spent ${spent:.2f}); dispatch paused")
            notify(self.cfg.data, task.key, "budget", f"budget ${budget:.2f} exceeded (spent ${spent:.2f})", "")
        return True

    # ---- dispatch pause/resume ---------------------------------------------
    def control(self) -> dict[str, Any]:
        """The _control entry: dispatch pause state (`dispatch`/`by`/`at`/`reason`) and any
        pending tool `upgrade` (see upgrade_available)."""
        return self.state.get("_control")

    def is_dispatch_paused(self) -> bool:
        return self.control().get("dispatch") == "paused"

    def pause(self, by: str = "cli", reason: str = "") -> None:
        ctrl = self.control()
        ctrl["dispatch"] = "paused"
        ctrl["by"] = by
        ctrl["at"] = now_iso()
        ctrl["reason"] = reason
        self.state.save()
        self.events.emit("dispatch_paused", "", by=by, reason=reason)
        self.log("dispatch paused by " + by + (f": {reason}" if reason else ""))

    def resume(self, by: str = "cli") -> None:
        ctrl = self.control()
        ctrl.pop("dispatch", None)
        ctrl.pop("by", None)
        ctrl.pop("at", None)
        ctrl.pop("reason", None)
        self.state.save()
        self.events.emit("dispatch_resumed", "", by=by)
        self.log(f"dispatch resumed by {by}")

    # ---- live config overrides ----------------------------------------------
    def overrides(self) -> dict[str, Any]:
        """Config values overridden live (via `garden set` or the Configuration page),
        stored in `_control.overrides` and reloaded every tick. Takes precedence over the
        same key in garden.yaml until cleared with `clear_override`/`garden clear`."""
        return self.control().setdefault("overrides", {})

    def set_override(self, key: str, value: Any, by: str = "cli") -> None:
        self.overrides()[key] = value
        self.state.save()
        self.events.emit("config_override", "", key=key, value=value, by=by)
        self.log(f"{key} set to {value} by {by} (live override; takes effect next tick)")

    def clear_override(self, key: str, by: str = "cli") -> None:
        ov = self.overrides()
        if key not in ov:
            return
        del ov[key]
        self.state.save()
        self.events.emit("config_override_cleared", "", key=key, by=by)
        self.log(f"{key} override cleared by {by} (back to the garden.yaml value)")

    def effective(self, key: str, default: Any = None) -> Any:
        """The live override for `key` if one is set, else the garden.yaml value."""
        ov = self.overrides()
        if key in ov:
            return ov[key]
        return self.cfg.get(key, default)

    def effective_max_parallel(self) -> int:
        return int(self.effective("max_parallel", 10))
