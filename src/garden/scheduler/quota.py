"""Harness-level pause: a quota or spend-limit error from a harness's own account stops
dispatch for that harness (not the task), and a cheap probe resumes it by itself once the
account is usable again."""

from __future__ import annotations

import datetime as dt
from typing import Any

from ..model import Task, now_iso
from ..notify import notify
from ..runs import Run
from .report import TickReport


def _minutes_since(iso: str) -> float:
    if not iso:
        return 1e9
    try:
        t = dt.datetime.fromisoformat(iso)
    except ValueError:
        return 1e9
    return max(0.0, (dt.datetime.now(dt.UTC) - t).total_seconds() / 60)


class QuotaMixin:
    # ---- pause/resume --------------------------------------------------------
    def paused_harnesses(self) -> dict[str, Any]:
        return self.control().setdefault("paused_harnesses", {})

    def is_harness_paused(self, name: str) -> bool:
        return bool(name) and name in self.paused_harnesses()

    def pause_harness(self, name: str, reason: str, run_id: str = "") -> None:
        if not name:
            return
        already = name in self.paused_harnesses()
        self.paused_harnesses()[name] = {"reason": reason, "at": now_iso(), "run_id": run_id}
        self.state.save()
        if already:
            return  # already paused; don't re-notify on every quota hit while it's down
        self.events.emit("harness_paused", "", harness=name, reason=reason)
        self.log(f"harness {name} paused: {reason}")
        notify(self.cfg.data, name, "harness_paused", f"{name}: {reason}", "")

    def resume_harness(self, name: str, by: str = "probe") -> None:
        if self.paused_harnesses().pop(name, None) is None:
            return
        self.state.save()
        self.events.emit("dispatch_resumed", "", harness=name, by=by)
        self.log(f"harness {name} resumed by {by}")

    def _raise_if_harness_paused(self, name: str) -> None:
        """The gate every non-queue dispatch (a fresh trial contender, a review round, a
        persona or comparison aux run) passes through, mirroring the ready-queue's own
        `is_harness_paused` skip in dispatch_ready: a paused harness refuses a new run
        instead of starting one that would only hit the same account limit again."""
        if not name or not self.is_harness_paused(name):
            return
        entry = self.paused_harnesses().get(name) or {}
        reason = entry.get("reason") or "quota limit"
        raise RuntimeError(f"{name} is paused ({reason}); dispatch resumes automatically once a probe succeeds")

    def _pause_for_env_error(self, run: Run, collected: dict[str, Any]) -> None:
        """A review, persona, comparison or trial-contender round hit the same harness-account
        limit a work/revise round would (see reap._handle_quota_env_error for that path):
        pause the harness so nothing else dispatches to it either, without touching the task
        the way an ordinary failure would."""
        kind = str(collected.get("env_kind") or "quota")
        if kind == "resource":
            # Resource exhaustion belongs to the host, not one model account. Admission
            # sampling controls the retry; pausing the harness would require an unrelated
            # network probe and could strand work after the machine has recovered.
            return
        if run.harness:
            self.pause_harness(run.harness, f"{kind} limit hit on {run.harness}", run_id=run.run_id)

    # ---- probe -----------------------------------------------------------
    def _probe_interval_minutes(self) -> float:
        v = self.cfg.get("harness_pause.probe_minutes", 10)
        return float(v) if v is not None else 10.0

    def probe_paused_harnesses(self, rep: TickReport) -> None:
        """Every `harness_pause.probe_minutes` (default 10), send one paused harness a cheap
        one-line prompt; a response with no quota env_error clears the pause. Nothing here
        touches a task or spends an attempt — the probe is not a run record."""
        interval = self._probe_interval_minutes()
        for name in list(self.paused_harnesses()):
            entry = self.paused_harnesses().get(name) or {}
            last = str(entry.get("probed_at") or entry.get("at") or "")
            if _minutes_since(last) < interval:
                continue
            entry["probed_at"] = now_iso()
            self.state.save()
            probe_task = Task(path=self.store.root, id=f"_probe-{name}", title="", product="", phase="")
            try:
                runner = self.runner_for(probe_task, "local", name)
                cwd = self.store.config.garden_dir / "probe" / name
                result = runner.probe(cwd)
            except Exception as e:  # noqa: BLE001 - keep the tick alive on a probe failure
                rep.errors.append(f"harness {name} probe failed: {e}")
                continue
            if result.get("env_error"):
                continue  # still down; try again next interval
            self.resume_harness(name, by="probe")
            rep.transitions.append(f"harness {name} resumed (probe ok)")
