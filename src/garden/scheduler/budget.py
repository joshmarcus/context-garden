"""Budgets, the dispatch pause, live config overrides and the operating profile: what a
person can turn without a restart."""

from __future__ import annotations

from typing import Any

from ..configuration import (
    CONFIG_FIELDS,
    ApplyMode,
    assert_mutation_allowed,
    audit_value,
    product_configuration,
    resolve_saved_effective_value,
)
from ..model import Task, now_iso
from ..notify import notify
from ..profiles import PROFILE_KEYS
from ..profiles import stops as profile_stops


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

    # ---- maintenance pause -------------------------------------------------
    def maintenance(self) -> dict[str, Any]:
        """The whole-scheduler stop used around an installation, if one exists.

        Reading an absent request must stay observational: a stale scheduler can
        otherwise persist its empty entry over a request another controller saved.
        ``request_maintenance_pause`` is the sole path that creates the durable entry.
        """
        return self.control().get("maintenance", {})

    def maintenance_requested(self) -> bool:
        return self.maintenance().get("status") in {"requested", "quiesced"}

    def maintenance_quiesced(self) -> bool:
        return self.maintenance().get("status") == "quiesced"

    def request_maintenance_pause(self, by: str = "cli", reason: str = "") -> None:
        """Acknowledge a request; a following locked tick records quiescence."""
        entry = self.control().setdefault("maintenance", {})
        if entry.get("status") == "quiesced":
            return
        entry.update(status="requested", by=by, at=now_iso(), reason=reason)
        entry.pop("quiesced_at", None)
        self.state.save()
        self.events.emit("maintenance_requested", "", by=by, reason=reason)
        self.log("maintenance pause requested by " + by + (f": {reason}" if reason else ""))

    def _quiesce_for_maintenance(self) -> None:
        entry = self.maintenance()
        if entry.get("status") != "requested":
            return
        entry["status"] = "quiesced"
        entry["quiesced_at"] = now_iso()
        self.events.emit("maintenance_quiesced", "", by=entry.get("by", ""))
        self.log("maintenance pause quiesced")

    def resume_maintenance(self, by: str = "cli") -> None:
        if not self.maintenance_requested():
            return
        self.control().pop("maintenance", None)
        self.state.save()
        self.events.emit("maintenance_resumed", "", by=by)
        self.log(f"maintenance resumed by {by}")

    def require_maintenance_running(self) -> None:
        if self.maintenance_requested():
            raise RuntimeError("maintenance pause is active; resume maintenance before starting scheduler work")

    def maintenance_readiness(self) -> dict[str, Any]:
        """Report installation blockers without treating durable results as live work."""
        live: list[dict[str, str]] = []
        dormant: list[str] = []
        for run in self.runs.active():
            if run.is_local_execution:
                blocker = "local process may still depend on the installed runtime"
            elif run.status in {"requested", "preparing"}:
                blocker = "preparation is incomplete; runtime dependency is not yet knowable"
            elif run.runner == "ssh" and run.env_snapshot.get("ssh_tmux_session"):
                import json

                from ..proctree import pid_alive

                state_path = run.path / "ssh-state.json"
                try:
                    state = json.loads(state_path.read_text())
                except (OSError, ValueError):
                    state = {}
                artifacts = state.get("artifacts") or {}
                collector_stopped = not run.pid or not pid_alive(run.pid)
                remote_stopped = (
                    state.get("held_kind") == "legacy_artifacts_present"
                    and artifacts.get("session_exists") is False
                    and state.get("process_exists") is False
                )
                if collector_stopped and remote_stopped:
                    dormant.append(f"{run.task_id}/{run.run_id}")
                    continue
                blocker = "SSH collector or remote process may still depend on the installed runtime"
            else:
                blocker = "remote/manual process dependency is unknown; verify its host before reinstalling"
            live.append({"task": run.task_id, "run": run.run_id, "mode": run.mode,
                         "state": run.status, "blocker": blocker})
        return {"requested": self.maintenance_requested(), "quiesced": self.maintenance_quiesced(),
                "live": live, "dormant": dormant,
                "finished_uncollected": sorted(self.unreaped_run_ids()),
                "ready": self.maintenance_quiesced() and not live}

    # ---- live config overrides ----------------------------------------------
    def overrides(self) -> dict[str, Any]:
        """Config values overridden live (via `garden set` or the Configuration page),
        stored in `_control.overrides` and reloaded every tick. Takes precedence over the
        same key in garden.yaml until cleared with `clear_override`/`garden clear`."""
        return self.control().setdefault("overrides", {})

    def set_override(self, key: str, value: Any, by: str = "cli", product: str | None = None) -> None:
        assert_mutation_allowed(self.cfg.data, key, product=product)
        field = CONFIG_FIELDS[key]
        if field.apply != ApplyMode.RUNTIME and key != "max_parallel":
            raise ValueError(f"{key} does not support a runtime override")
        field.validate(value)
        if product is not None:
            raise ValueError("project runtime overrides are not supported; save a project override instead")
        self._assert_runtime_change_preserves_locks(key, value)
        self.overrides()[key] = value
        self.state.save()
        safe = audit_value(key, value)
        self.events.emit("config_override", "", key=key, value=safe, scope="global", provenance="runtime", by=by)
        self.log(f"{key} set to {safe} by {by} (live override; takes effect next tick)")

    def clear_override(self, key: str, by: str = "cli", product: str | None = None) -> None:
        assert_mutation_allowed(self.cfg.data, key, product=product)
        if product is not None:
            raise ValueError("project runtime overrides are not supported; reset the saved project override instead")
        ov = self.overrides()
        if key not in ov:
            return
        self._assert_runtime_change_preserves_locks(key, None, clear=True)
        del ov[key]
        self.state.save()
        self.events.emit("config_override_cleared", "", key=key, scope="global", provenance="yaml", by=by)
        self.log(f"{key} override cleared by {by} (back to the garden.yaml value)")

    def _assert_runtime_change_preserves_locks(self, key: str, value: Any,
                                               *, clear: bool = False) -> None:
        """Keep a global live override from moving a project's plain inherited lock."""
        for product in (self.cfg.data.get("products") or {}):
            _, locks = product_configuration(self.cfg.data, str(product))
            raw = locks.get(key)
            if raw is None:
                continue
            policy = {"reason": raw} if isinstance(raw, str) else dict(raw)
            if "value" in policy or key in product_configuration(self.cfg.data, str(product))[0]:
                continue
            old = self.effective(key, product=str(product))
            if clear:
                profile_key = PROFILE_KEYS.get(key)
                profile = self.operating_profile()
                new = profile.get(profile_key, self.cfg.get(key)) if profile_key else self.cfg.get(key)
            else:
                new = value
            if old != new:
                reason = str(policy.get("reason") or "Locked by project policy")
                raise PermissionError(f"{key} is locked for {product}: {reason}")

    def _assert_reload_preserves_runtime_locks(self, new_cfg: Any) -> None:
        """Reject reloads that move a plain lock through the active profile layer."""
        for product in (self.cfg.data.get("products") or {}):
            _, locks = product_configuration(self.cfg.data, str(product))
            _, new_locks = product_configuration(new_cfg.data, str(product))
            for key, raw in locks.items():
                policy = {"reason": raw} if isinstance(raw, str) else dict(raw)
                new_raw = new_locks.get(key)
                new_policy = ({"reason": new_raw} if isinstance(new_raw, str)
                              else dict(new_raw) if isinstance(new_raw, dict) else None)
                if "value" in policy or new_policy is None or "value" in new_policy:
                    continue
                old = self._effective_with_config(key, self.cfg, str(product))
                new = self._effective_with_config(key, new_cfg, str(product))
                if old != new:
                    reason = str(policy.get("reason") or "Locked by project policy")
                    raise PermissionError(f"{key} is locked for {product}: {reason}")

    def _effective_with_config(self, key: str, cfg: Any, product: str) -> Any:
        """Resolve a scheduler-effective project value against a prospective config."""
        overrides = self.overrides()
        if key in overrides:
            value = overrides[key]
        else:
            profile_name = str(overrides.get("operating_profile", cfg.get("operating_profile") or ""))
            value = resolve_saved_effective_value(
                cfg.data, key, product, active_profile=profile_name,
            )
        if key in overrides:
            project_value = cfg.setting(key, product)
            if project_value.source != "global":
                return project_value.value
        return value
    def effective(self, key: str, default: Any = None, product: str | None = None) -> Any:
        """The live override for `key` if one is set, else the active operating profile's
        value for it (see operating_profile) if the profile sets that facet, else the
        garden.yaml value. A live override is always the most specific: it wins over the
        stop even while one is active."""
        ov = self.overrides()
        if key in ov:
            value = ov[key]
        else:
            value = None
            field = PROFILE_KEYS.get(key)
            if field:
                profile = self.operating_profile()
                if field in profile:
                    value = profile[field]
            if value is None:
                value = self.cfg.get(key, default)
        if product is not None:
            project = self.cfg.setting(key, product)
            if project.source != "global":
                return project.value
        return value

    def save_config_changes(self, changes: dict[str, Any], *, product: str | None = None,
                            expected_revision: str | None = None, reset: bool = False,
                            by: str = "cli") -> None:
        """Persist an ordinary edit through the shared atomic policy boundary."""
        self.cfg.save_changes(changes, product=product,
                              expected_revision=expected_revision, reset=reset)
        for key, value in changes.items():
            self.events.emit("config_saved", "", key=key,
                             value="<reset>" if reset else audit_value(key, value),
                             scope=f"project:{product}" if product else "global",
                             provenance="inherited" if reset else "saved", by=by)

    def effective_source(self, key: str) -> str:
        """Which layer answers `effective(key)` right now: "override" (a live override on
        this exact key), "profile" (the active operating profile sets this facet), or "yaml"
        (the plain garden.yaml/default value) — for the Config page to say where a value
        comes from."""
        if key in self.overrides():
            return "override"
        field = PROFILE_KEYS.get(key)
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
        assert_mutation_allowed(self.cfg.data, "operating_profile")
        name = (name or "").strip()
        if name and name not in self.operating_profile_stops():
            raise ValueError(f"unknown operating profile {name!r}")
        old = self.operating_profile_name()
        prospective = dict(self.operating_profile_stops().get(name) or {})
        for key, profile_key in PROFILE_KEYS.items():
            if key in self.overrides():
                continue
            new = prospective.get(profile_key, self.cfg.get(key))
            self._assert_runtime_change_preserves_locks(key, new)
        if name:
            self.overrides()["operating_profile"] = name
        else:
            self.overrides().pop("operating_profile", None)
        self.state.save()
        self.events.emit("profile_changed", "", **{"from": old, "to": name}, scope="global", by=by)
        self.log(f"operating profile: {old or '(none)'} -> {name or '(none)'} by {by}")
