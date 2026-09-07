"""Budgets, the dispatch pause, live config overrides and the operating profile: what a
person can turn without a restart."""

from __future__ import annotations

from typing import Any

from ..model import Task, now_iso
from ..notify import notify
from ..profiles import stops as profile_stops

# effective() key -> the field of the active operating profile that answers it, when no more
# specific live override is set for that key (see effective and operating_profile below).
_PROFILE_KEYS: dict[str, str] = {
    "max_parallel": "workers",
    "review_parallel": "reviews",
    "review.difficulty": "review_difficulty",
    "retro.difficulty": "retro_difficulty",
    "observe.profile": "observe",
}


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
        # Page chrome and queue widgets ask for the same phase repeatedly.  Build every
        # phase total in one pass over the shared RunStore snapshot per scheduler facade.
        cached = getattr(self, "_spent_by_phase", None)
        generation = self.runs.generation
        if cached is None or getattr(self, "_spent_generation", -1) != generation:
            task_phases = {t.id: t.key for t in self.store.tasks().values()}
            cached = {}
            for task_id, cost in self.runs.costs_by_task().items():
                phase = task_phases.get(task_id)
                if phase:
                    cached[phase] = cached.get(phase, 0.0) + cost
            self._spent_by_phase = {phase: round(cost, 4) for phase, cost in cached.items()}
            self._spent_generation = generation
        return self._spent_by_phase.get(key, 0.0)

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
        """The live override for `key` if one is set, else the active operating profile's
        value for it (see operating_profile) if the profile sets that facet, else the
        garden.yaml value. A live override is always the most specific: it wins over the
        stop even while one is active."""
        ov = self.overrides()
        if key in ov:
            return ov[key]
        field = _PROFILE_KEYS.get(key)
        if field:
            profile = self.operating_profile()
            if field in profile:
                return profile[field]
        return self.cfg.get(key, default)

    def effective_source(self, key: str) -> str:
        """Which layer answers `effective(key)` right now: "override" (a live override on
        this exact key), "profile" (the active operating profile sets this facet), or "yaml"
        (the plain garden.yaml/default value) — for the Config page to say where a value
        comes from."""
        if key in self.overrides():
            return "override"
        field = _PROFILE_KEYS.get(key)
        if field and field in self.operating_profile():
            return "profile"
        return "yaml"

    def effective_max_parallel(self) -> int:
        return int(self.effective("max_parallel", 10))

    # ---- operating profile (CG-221) -----------------------------------------
    def operating_profile_stops(self) -> dict[str, dict[str, Any]]:
        """Every stop a garden can be switched to: the built-ins plus whatever it defines or
        overrides under `profiles:` (see garden.profiles.stops)."""
        return profile_stops(self.cfg)

    def operating_profile_name(self) -> str:
        """The active stop's name, or "" when none is set (plain garden.yaml/live-override
        values). A live override on `operating_profile` (`garden profile <name>` or the rail)
        wins; otherwise the garden.yaml `operating_profile` key, if set."""
        ov = self.overrides()
        if "operating_profile" in ov:
            return str(ov["operating_profile"] or "")
        return str(self.cfg.get("operating_profile") or "")

    def operating_profile(self) -> dict[str, Any]:
        """The resolved fields of the active stop, or {} if none is active or its name is
        unrecognised (a stop removed from `profiles:` after being selected doesn't crash the
        scheduler; effective() just falls through to garden.yaml values)."""
        return dict(self.operating_profile_stops().get(self.operating_profile_name()) or {})

    def set_operating_profile(self, name: str, by: str = "cli") -> None:
        """Switch the active stop live: an empty name clears it, back to plain garden.yaml
        values. Emits `profile_changed` (from/to) so the change is visible on the costs chart
        once it reads the event log, besides the generic `config_override` trail."""
        name = (name or "").strip()
        if name and name not in self.operating_profile_stops():
            raise ValueError(f"unknown operating profile {name!r}")
        old = self.operating_profile_name()
        if name:
            self.overrides()["operating_profile"] = name
        else:
            self.overrides().pop("operating_profile", None)
        self.state.save()
        self.events.emit("profile_changed", "", **{"from": old, "to": name})
        self.log(f"operating profile: {old or '(none)'} -> {name or '(none)'} by {by}")
