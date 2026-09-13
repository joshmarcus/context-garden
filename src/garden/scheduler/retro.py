"""The phase retro: harvest friction, run the missing personas, reconcile, open the PR."""

from __future__ import annotations

import os
import re
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .. import gitops
from ..brief import brief_gaps
from ..events import phase_summary
from ..github import GitHubError
from ..model import Phase, Status, Task, estimate_tokens, now_iso, phase_refusal, slugify
from ..multiplayer_client import MultiplayerUnavailable
from ..operator_spend import attributed_summary as operator_attributed_summary
from ..operator_spend import default_path as operator_spend_path
from ..operator_spend import read_records as read_operator_records
from ..outcomes import attributed_phase_key
from ..personas import (
    SEVERITY_PRIORITY,
    finding_body,
    finding_title,
    parse_persona,
    phase_brief,
    valid_name,
)
from ..retro import (
    PHASE_VERDICTS,
    flatten_findings,
    group_findings,
    next_phase_name,
    normalize_verdict,
    numbers_section,
    parse_retro,
    persona_features,
    persona_reports,
    reconcile_brief,
    render_next_goals,
    render_retro_doc,
    resolve_features,
    resolve_findings,
    resolve_retro_tasks,
)
from ..runs import Run
from .report import TickReport

_RUN_FOOTER_RE = re.compile(r"_garden persona run (\S+)_\s*$")
_SOURCE_FOOTER_RE = re.compile(r"(?m)^_garden phase source ([0-9a-f]{40})_$")
_SAFE_RUN_ID_RE = re.compile(r"[A-Za-z0-9_-]+\Z")


class RetroMixin:
    # ---- retro: harvest, personas, reconcile, PR ---------------------------
    def _self_product(self) -> str | None:
        """The product whose repo is the garden's own repo (`self: true`); the retro opens its
        PR there so the retro document and next-goals draft land as a PR, not a live edit."""
        for name in (self.cfg.data.get("products", {}) or {}):
            if self.cfg.product_self(name):
                return name
        return None

    def retro_default_personas(self) -> list[str]:
        from ..personas import DEFAULT_PERSONAS, list_personas

        return list_personas(self.store) or sorted(DEFAULT_PERSONAS)

    def _retro_list(self) -> list[dict[str, Any]]:
        return self.state.get("_retro").setdefault("runs", [])

    def _retro_remove(self, entry: dict[str, Any]) -> None:
        self.state.get("_retro")["runs"] = [e for e in self._retro_list() if e is not entry]

    def _closing_review_policy(self, phase: Phase) -> dict[str, Any]:
        """Evaluate the current automatic closing-review gates for ``phase``."""
        enabled = bool(phase.meta.get("auto_closing_review", self.cfg.get("retro.auto_start", False)))
        personas = phase.meta.get("closing_review_personas") or self.cfg.get("retro.personas") or self.retro_default_personas()
        configured = self.cfg.get("retro.prerequisites") or {}
        prerequisites = phase.meta.get("closing_review_prerequisites") or configured.get(phase.key, [])
        reasons: list[str] = []
        if not enabled:
            reasons.append("automatic closing review is disabled")
        if phase.closed:
            reasons.append(f"phase closed {phase.closed}")
        if not phase.tasks:
            reasons.append("phase has no tasks")
        nonterminal = [t.id for t in phase.tasks if not t.status.terminal]
        if nonterminal:
            reasons.append("non-terminal tasks: " + ", ".join(nonterminal))
        if phase.frozen and not bool(self.cfg.get("retro.allow_frozen", False)):
            reasons.append(f"phase frozen {phase.frozen}")
        for key in prerequisites:
            try:
                product, name = str(key).split("/", 1)
                prerequisite = self.store.phase(product, name)
            except (KeyError, ValueError):
                reasons.append(f"prerequisite {key} is missing")
            else:
                if not prerequisite.closed:
                    reasons.append(f"prerequisite {key} is not closed")
        if self.cfg.get("retro.require_owner_approval", False) and not phase.meta.get("closing_review_approved"):
            reasons.append("owner approval is required (set closing_review_approved in phase frontmatter)")
        evidence_identity = ""
        try:
            from ..stabilization import (
                acceptance_identity,
                gate,
                load_evidence,
                matching_acceptance,
                running_build_sha,
            )

            evidence = load_evidence(phase)
            evidence_identity = str(evidence.get("build_sha") or "")
            current_build = running_build_sha()
            if acceptance := matching_acceptance(phase, current_build):
                evidence_identity += ":acceptance:" + acceptance_identity(acceptance)
            accepted, missing = gate(phase)
            if not accepted:
                reasons.append("stabilization evidence is unaccepted: " + "; ".join(missing))
        except (OSError, ValueError) as exc:
            reasons.append(f"stabilization evidence could not be read: {exc}")
        return {"eligible": not reasons, "personas": list(personas),
                "evidence": evidence_identity, "reason": "; ".join(reasons)}

    def closing_review_status(self, phase: Phase) -> dict[str, Any]:
        """Return the durable/operator-facing automatic closing-review state and policy.

        Phase frontmatter may override the global switch with ``auto_closing_review`` and
        records an owner gate with ``closing_review_approved``. Stabilization remains the
        existing authoritative evidence gate; live canaries are intentionally not added.
        """
        active = next((e for e in self._retro_list() if e.get("phase") == phase.key), None)
        policy = self._closing_review_policy(phase)
        if active:
            reason = str(active.get("waiting_reason") or "")
            if active.get("automatic") and not policy["eligible"]:
                reason = policy["reason"]
            return {"eligible": False, "stage": str(active.get("stage") or "queued"),
                    "queued": True, "personas": list(active.get("personas") or policy["personas"]),
                    "source": str(active.get("source") or ""),
                    "evidence": str(active.get("evidence") or policy["evidence"]),
                    "reason": reason}
        return {**policy, "stage": "eligible" if policy["eligible"] else "waiting",
                "queued": False, "source": ""}

    def queue_eligible_closing_reviews(self, rep: TickReport) -> None:
        """Persist one idempotent request per newly eligible phase; admission happens later."""
        for product in self.store.products():
            for phase in product.phases:
                if not self.phase_is_authorized(phase.product, phase.name):
                    continue
                status = self.closing_review_status(phase)
                if not status["eligible"]:
                    continue
                effect_key = f"retro-queue:{phase.key}:{status['evidence']}"
                with self.phase_effect(phase.product, phase.name, effect_key):
                    status = self.closing_review_status(phase)
                    if not status["eligible"]:
                        continue
                    entry = {"phase": phase.key, "product": phase.product, "phase_name": phase.name,
                         "personas": status["personas"], "skip_personas": False,
                         "next_phase": next_phase_name(phase.name), "self_product": self._self_product() or "",
                         "stage": "queued", "persona_runs": {}, "automatic": True,
                         "requested_at": now_iso(), "request_id": uuid.uuid4().hex, "source": "",
                         "evidence": status["evidence"], "no_file": False}
                    self._retro_list().append(entry)
                    self.events.emit("retro_queued", "", phase=phase.key, source=entry["source"],
                                     evidence=entry["evidence"])
                    rep.transitions.append(f"retro {phase.key} queued")
                    self.state.save()

    def _current_phase_source(self, phase: Phase) -> str:
        probe = Task(path=self.store.root, id=f"_{phase.product}-{phase.name}", title="",
                     product=phase.product, phase=phase.name)
        base = self.final_base_for(probe)
        try:
            repo = self.repo_for(probe)
            return gitops.rev_parse(repo, gitops.base_ref(repo, base))
        except gitops.GitError:
            return ""

    def dispatch_queued_closing_reviews(self, rep: TickReport) -> None:
        """Claim one queued request; its potentially slow preparation runs after the tick lock."""
        for entry in self._retro_list():
            if entry.get("stage") not in {"queued", "preparing", "dispatching"}:
                continue
            if not self.phase_is_authorized(str(entry.get("product")), str(entry.get("phase_name"))):
                continue
            product, phase_name = str(entry["product"]), str(entry["phase_name"])
            owner_pid = int(entry.get("preparation_pid") or 0)
            if entry.get("stage") != "queued" and owner_pid:
                try:
                    os.kill(owner_pid, 0)
                except ProcessLookupError:
                    pass
                except PermissionError:
                    continue
                else:
                    continue
            try:
                effect_key = f"retro-claim:{entry.get('request_id', entry['phase'])}:{entry.get('stage')}"
                with self.phase_effect(product, phase_name, effect_key):
                    self._claim_queued_closing_review(entry)
            except RuntimeError as exc:
                entry["waiting_reason"] = str(exc)
                self.state.save()
            break

    def _claim_queued_closing_review(self, entry: dict[str, Any]) -> None:
        if len(self.review_runs_active()) >= self.review_parallel_limit():
            raise RuntimeError(
                f"waiting for review capacity ({len(self.review_runs_active())}/"
                f"{self.review_parallel_limit()} running)"
            )
        claim = uuid.uuid4().hex
        entry.update(stage="preparing", preparation_claim=claim, preparation_pid=os.getpid())
        entry.pop("waiting_reason", None)
        entry["preparation_action"] = "start"
        self._closing_review_claims.append((str(entry["request_id"]), claim, "start"))
        self.state.save()

    def prepare_claimed_closing_reviews(self, rep: TickReport) -> None:
        """Prepare requests claimed by this tick without holding the scheduler mutation lock.

        The durable claim prevents another tick or a simultaneous manual start from launching
        the same request.  Reconciliation under the lock checks both identities again before
        the normal retro launcher is allowed to create model jobs.
        """
        for request_id, claim, action in self._closing_review_claims:
            try:
                entry = next((item for item in self._retro_list()
                              if item.get("request_id") == request_id), None)
                if entry is None:
                    continue
                with self.phase_effect(
                    str(entry["product"]), str(entry["phase_name"]),
                    f"retro-prepare:{request_id}:{action}:{claim}",
                ):
                    self._prepare_claimed_closing_review(request_id, claim, action, rep)
            except RuntimeError as exc:
                self._defer_closing_review_preparation(request_id, claim, action, exc)
        self._closing_review_claims.clear()

    def _prepare_claimed_closing_review(self, request_id: str, claim: str, action: str,
                                        rep: TickReport) -> None:
            try:
                expected_stage = "preparing" if action == "start" else f"preparing_{action}"
                with self._controller_lock():
                    entry = next((item for item in self._retro_list()
                                  if item.get("request_id") == request_id), None)
                    if (entry is None or entry.get("stage") != expected_stage
                            or entry.get("preparation_claim") != claim):
                        return
                    phase = self.store.phase(entry["product"], entry["phase_name"])
                    if action == "start" and entry.get("automatic"):
                        policy = self._closing_review_policy(phase)
                        current_evidence = str(policy.get("evidence") or "")
                        if not policy["eligible"] or str(entry.get("evidence") or "") != current_evidence:
                            reason = (policy.get("reason") or
                                      "accepted stabilization evidence changed; re-preparing")
                            entry.update(stage="queued", source="", evidence=current_evidence,
                                         waiting_reason=reason)
                            self._clear_retro_preparation(entry)
                            self.state.save()
                            return
                if action != "start" and entry.get("automatic"):
                    current_source = self._current_phase_source(phase)
                    with self._controller_lock():
                        entry = next((item for item in self._retro_list()
                                      if item.get("request_id") == request_id), None)
                        if (entry is None or entry.get("stage") != expected_stage
                                or entry.get("preparation_claim") != claim):
                            return
                        phase = self.store.phase(entry["product"], entry["phase_name"])
                        policy = self._closing_review_policy(phase)
                        identity_changed = (
                            str(entry.get("evidence") or "") != str(policy.get("evidence") or "")
                            or str(entry.get("source") or "") != current_source
                        )
                        if not policy["eligible"] or identity_changed:
                            reason = policy.get("reason") or "accepted source or evidence changed"
                            entry.update(stage="queued", source="", evidence=policy.get("evidence") or "",
                                         waiting_reason=reason)
                            self._clear_retro_preparation(entry)
                            self.state.save()
                            return
                if action == "personas":
                    names = list(entry.get("preparation_names") or [])
                    source = str(entry.get("source") or "")
                    prepared = self._prepare_deferred_retro_personas(
                        phase, entry, names, source
                    )
                    with self._controller_lock():
                        self.state = type(self.state)(self.state.path)
                        entry = next((item for item in self._retro_list()
                                      if item.get("request_id") == request_id), None)
                        if (entry is None or entry.get("stage") != expected_stage
                                or entry.get("preparation_claim") != claim):
                            self._discard_prepared_retro_personas(
                                prepared, "persona preparation claim was superseded"
                            )
                            return
                        phase = self.store.phase(entry["product"], entry["phase_name"])
                        if entry.get("automatic"):
                            policy = self._closing_review_policy(phase)
                            current_source = self._current_phase_source(phase)
                            identity_changed = (
                                source != current_source
                                or str(entry.get("source") or "") != source
                                or str(entry.get("evidence") or "")
                                != str(policy.get("evidence") or "")
                            )
                            if not policy["eligible"] or identity_changed:
                                reason = policy.get("reason") or (
                                    "accepted source or stabilization evidence changed during "
                                    "persona preparation; re-preparing"
                                )
                                entry.update(stage="queued", source="",
                                             evidence=str(policy.get("evidence") or ""),
                                             waiting_reason=reason)
                                self._clear_retro_preparation(entry)
                                self.state.save()
                                self._discard_prepared_retro_personas(prepared, reason)
                                return
                        admitted = self._commit_prepared_retro_personas(entry, prepared)
                        entry["stage"] = "personas"
                        self._clear_retro_preparation(entry)
                        self.state.save()
                    self._launch_prepared_retro_personas(admitted)
                    return
                if action == "reconcile":
                    prepared = self._prepare_reconcile(
                        entry, run_id=str(entry["reconcile_launch_run_id"])
                    )
                    with self._controller_lock():
                        # Preparation may include Git operations and remote PR metadata. Reload
                        # the durable claim after that work so a concurrent policy/evidence
                        # update cannot launch a reconciliation for stale accepted inputs.
                        self.state = type(self.state)(self.state.path)
                        entry = next((item for item in self._retro_list()
                                      if item.get("request_id") == request_id), None)
                        if (entry is None or entry.get("stage") != expected_stage
                                or entry.get("preparation_claim") != claim):
                            self._discard_prepared_reconcile(
                                prepared, "reconciliation preparation claim was superseded"
                            )
                            return
                        phase = self.store.phase(entry["product"], entry["phase_name"])
                        if entry.get("automatic"):
                            policy = self._closing_review_policy(phase)
                            current_source = self._current_phase_source(phase)
                            identity_changed = (
                                str(entry.get("source") or "") != current_source
                                or str(entry.get("evidence") or "")
                                != str(policy.get("evidence") or "")
                            )
                            if not policy["eligible"] or identity_changed:
                                reason = policy.get("reason") or (
                                    "accepted source or stabilization evidence changed during "
                                    "reconciliation preparation; re-preparing"
                                )
                                entry.update(stage="queued", source="",
                                             evidence=str(policy.get("evidence") or ""),
                                             waiting_reason=reason)
                                self._clear_retro_preparation(entry)
                                self.state.save()
                                self._discard_prepared_reconcile(prepared, reason)
                                return
                        self._commit_prepared_reconcile(entry, prepared)
                        self._clear_retro_preparation(entry)
                        self.state.save()
                    self._launch_prepared_reconcile(prepared)
                    with self._controller_lock():
                        self.state = type(self.state)(self.state.path)
                        entry = next((item for item in self._retro_list()
                                      if item.get("request_id") == request_id), None)
                        if entry and entry.get("recon_run_id") == prepared["run"].run_id:
                            entry["stage"] = "reconciling"
                            self.state.save()
                    return
                source = self._current_phase_source(phase) if entry.get("automatic") else ""
                if entry.get("automatic") and not source:
                    raise RuntimeError("waiting for the accepted phase source identity")
                prepared = self._prepare_retro_start(phase, entry, source)
                with self._controller_lock():
                    # Walkthrough, Git/worktree setup and persona brief construction may all
                    # be slow. Reload the claim and accepted identities after that work, then
                    # publish the exact prepared role runs before any worker can start.
                    self.state = type(self.state)(self.state.path)
                    entry = next((item for item in self._retro_list()
                                  if item.get("request_id") == request_id), None)
                    if (entry is None or entry.get("stage") != "preparing"
                            or entry.get("preparation_claim") != claim):
                        self._discard_prepared_retro_start(
                            prepared, "closing-review preparation claim was superseded"
                        )
                        return
                    phase = self.store.phase(entry["product"], entry["phase_name"])
                    if entry.get("automatic"):
                        policy = self._closing_review_policy(phase)
                        if not policy["eligible"]:
                            entry.update(stage="queued", waiting_reason=policy["reason"])
                            self._clear_retro_preparation(entry)
                            self.state.save()
                            self._discard_prepared_retro_start(prepared, policy["reason"])
                            return
                        current_evidence = str(policy.get("evidence") or "")
                        if str(entry.get("evidence") or "") != current_evidence:
                            entry.update(stage="queued", evidence=current_evidence, source="",
                                         waiting_reason="accepted stabilization evidence changed; re-preparing")
                            self._clear_retro_preparation(entry)
                            self.state.save()
                            self._discard_prepared_retro_start(
                                prepared, "accepted stabilization evidence changed"
                            )
                            return
                    if entry.get("automatic") and self._current_phase_source(phase) != source:
                        reason = "accepted phase source changed during preparation"
                        entry.update(stage="queued", source="", waiting_reason=reason)
                        self._clear_retro_preparation(entry)
                        self.state.save()
                        self._discard_prepared_retro_start(prepared, reason)
                        return
                    entry["source"] = source
                    self._commit_prepared_retro_start(entry, prepared)
                    self._clear_retro_preparation(entry)
                    self.state.save()
                self._launch_prepared_retro_start(prepared)
                if not entry.get("automatic") and not prepared["missing"]:
                    try:
                        self._dispatch_reconcile(entry)
                    except RuntimeError as exc:
                        entry.update(stage="queued", waiting_reason=str(exc))
                        self.state.save()
                        raise
                rep.transitions.append(f"retro {phase.key} started")
            except RuntimeError:
                raise

    def _defer_closing_review_preparation(self, request_id: str, claim: str, action: str,
                                           exc: RuntimeError) -> None:
                entry = next((item for item in self._retro_list()
                              if item.get("request_id") == request_id), None)
                if entry is None:
                    return
                self.log(f"retro preparation {request_id} deferred: {exc}")
                if action == "reconcile":
                    probe_id = f"_retro-{entry['product']}-{entry['phase_name']}"
                    run_id = str(entry.get("reconcile_launch_run_id") or "")
                    run = next((candidate for candidate in self.runs.runs_for(probe_id)
                                if candidate.run_id == run_id), None)
                    if run is not None and run.pid is None and not run.finished_at:
                        run.status = "failed"
                        run.error = f"reconciliation preparation failed: {exc}"
                        run.finished_at = now_iso()
                        run.preparer_pid = None
                        run.save()
                with self._controller_lock():
                    entry = next((item for item in self._retro_list()
                                  if item.get("request_id") == request_id), None)
                    if entry and entry.get("preparation_claim") == claim:
                        retry_stage = "queued" if action == "start" else "personas"
                        if action == "reconcile" and entry.get("recon_run_id"):
                            retry_stage = "reconciling"
                        entry.update(stage=retry_stage, waiting_reason=str(exc))
                        self._clear_retro_preparation(entry)
                        self.state.save()

    def _persona_revs(self, phase: Phase, reports: dict[str, Path]) -> dict[str, dict[str, Any]]:
        """The parsed marker verdict behind each persona's on-disk report: the rendered markdown
        (`reports`, from `persona_reports`) carries only prose, so this recovers the run id from
        its footer line and re-parses that run's `final.md` for the structured findings and the
        persona's own sections (CG-187, CG-188). Phase persona runs are recorded under their
        phase-specific auxiliary task id, so reports from separate phases cannot collide."""
        out: dict[str, dict[str, Any]] = {}
        runs_dir = self.runs.dir.resolve()
        phase_run_dir = f"_{phase.product}-{phase.name}"
        for name, path in reports.items():
            try:
                text = path.read_text()
            except OSError:
                continue
            m = _RUN_FOOTER_RE.search(text)
            if not m:
                continue
            run_id = m.group(1)
            if not _SAFE_RUN_ID_RE.fullmatch(run_id):
                continue
            final_path = self.runs.dir / phase_run_dir / run_id / "final.md"
            try:
                if not final_path.resolve().is_relative_to(runs_dir):
                    continue
            except OSError:
                continue
            if not final_path.exists():
                continue
            out[name] = parse_persona(final_path.read_text())
        return out

    def _reports_for_entry(self, phase: Phase, entry: dict[str, Any]) -> dict[str, Path]:
        reports = persona_reports(phase, entry["personas"])
        source = str(entry.get("source") or "")
        if not source or not entry.get("automatic"):
            return reports
        fresh: dict[str, Path] = {}
        for name, path in reports.items():
            try:
                match = _SOURCE_FOOTER_RE.search(path.read_text())
            except OSError:
                continue
            if match and match.group(1) == source:
                fresh[name] = path
        return fresh

    def _persona_findings(self, phase: Phase, reports: dict[str, Path]) -> dict[str, list[dict[str, Any]]]:
        """One draft task per finding needs severity/area/suggestion (CG-187); pull them from
        the parsed verdicts."""
        return {name: [f for f in rev.get("findings") or [] if isinstance(f, dict)]
                for name, rev in self._persona_revs(phase, reports).items()}

    def _persona_sections(self, phase: Phase, reports: dict[str, Path]) -> dict[str, dict[str, Any]]:
        """Each persona's own declared sections (CG-188); only those that reported a `sections`
        object, so `persona_features` can lift structured features into the retro's list."""
        return {name: rev["sections"] for name, rev in self._persona_revs(phase, reports).items()
                if isinstance(rev.get("sections"), dict)}

    def _retro_materials(self, phase: Phase, names: list[str]):
        """Harvest what the reconciliation needs: friction from PR bodies, friction already
        recorded under the phase's '## Reported' log, friction still sitting in marked PR
        comments, persona reports on disk, the phase's task list with statuses, and the
        merged PRs. Read-only: nothing here writes to the live garden (see module docstring
        of `garden.retro`)."""
        from ..friction import collect_comment_friction, extract_section, harvest

        prod_probe = Task(path=self.store.root, id=f"_{phase.product}", title="", product=phase.product, phase=phase.name)
        slug = self.slug_for(prod_probe)
        gh = self.github if slug else None
        friction = harvest(phase, self.runs, github=gh, slug=slug)
        friction_doc = phase.path / "docs" / "friction.md"
        reported = extract_section(friction_doc.read_text(), "Reported") if friction_doc.exists() else ""
        comment_friction = collect_comment_friction(phase, gh, slug)
        reports = persona_reports(phase, names)
        task_rows = [{"id": t.id, "title": t.title, "status": t.status.value} for t in phase.tasks]
        merged = [{"id": t.id, "title": t.title, "pr": t.pr} for t in phase.tasks if t.pr and t.status == Status.DONE]
        return friction, reported, comment_friction, reports, task_rows, merged

    def retro_plan(self, phase: Phase, personas: list[str] | None = None, skip_personas: bool = False,
                   next_phase: str = "") -> dict[str, Any]:
        """The plan a `--dry-run` prints: which personas run vs reuse, what will be harvested,
        and a token/cost estimate grounded in this garden's own past runs."""
        names = personas or self.retro_default_personas()
        nxt = next_phase or next_phase_name(phase.name)
        have = persona_reports(phase, names)
        reuse = [n for n in names if n in have]
        to_run = [] if skip_personas else [n for n in names if n not in have]
        friction, reported, comment_friction, reports, task_rows, merged = self._retro_materials(phase, names)
        self_prod = self._self_product()
        base = self.cfg.product_base_branch(self_prod or phase.product)
        recon = reconcile_brief(self.store, phase, base, friction, reported, comment_friction,
                                reports, task_rows, merged, nxt)
        difficulty = str(self.effective("retro.difficulty") or "hard")
        probe = Task(path=self.store.root, id=f"_retro-{phase.product}-{phase.name}", title="",
                     product=self_prod or phase.product, phase="")
        runner = self.runner_for(probe, "local", str(self.cfg.get("review.harness") or ""))
        model = self.retro_model_for(runner) or self.model_for(probe, runner, difficulty)
        persona_toks = 0
        if to_run:
            persona_toks = estimate_tokens(phase_brief(self.store, phase, to_run[0], base, self.phase_prs(phase))) * len(to_run)
        est_tokens = persona_toks + estimate_tokens(recon)
        tot = self.runs.totals()
        seen = int(tot["input_tokens"]) + int(tot["output_tokens"])
        rate = (float(tot["cost_usd"]) / seen) if seen else 0.0
        reported_entries = len(re.findall(r"(?m)^### ", reported))
        comment_items = sum(len(items) for _, items in comment_friction)
        return {"phase": phase.key, "next_phase": nxt, "self_product": self_prod,
                "personas_run": to_run, "personas_reuse": reuse, "friction": len(friction),
                "reported": reported_entries, "comment_friction": comment_items,
                "merged": len(merged), "tasks": len(task_rows), "est_tokens": est_tokens,
                "est_cost": round(est_tokens * rate, 2), "have_cost_history": bool(seen),
                "difficulty": difficulty, "model": model}

    def start_retro(self, phase: Phase, personas: list[str] | None = None, skip_personas: bool = False,
                    next_phase: str = "", no_file: bool = False) -> dict[str, Any]:
        """Start a phase retro. Runs the missing persona reviews (unless `skip_personas`), then
        the reconciliation, then opens a PR to the garden's own repo. Driven across ticks by
        `reap_retro`, like a trial."""
        self.require_execution_authority()
        self.require_phase_authority(phase)
        self.require_maintenance_running()
        with self.phase_effect(phase.product, phase.name, f"retro-start:{phase.key}"):
            return self._start_retro(phase, personas, skip_personas, next_phase, no_file)

    def _start_retro(self, phase: Phase, personas: list[str] | None = None,
                     skip_personas: bool = False, next_phase: str = "",
                     no_file: bool = False) -> dict[str, Any]:
        # A queued automatic request already contains the configuration identity needed to
        # finish it.  Load that durable identity before consulting current configuration:
        # the self product may have been removed after queueing, and manual invocation is
        # also the supported retry/convergence path for that existing request.
        with self._controller_lock():
            self.state = type(self.state)(self.state.path)
            existing = next((e for e in self._retro_list() if e.get("phase") == phase.key), None)
        self_prod = str(existing.get("self_product") or "") if existing else self._self_product()
        if not self_prod:
            raise RuntimeError("garden retro needs a product with `self: true` (the garden's own repo) to "
                               "open the retro PR; see docs/architecture.md")
        # The persona briefs inline the newest walkthrough. Capture it before dispatching any
        # persona so every review sees the same page set and empty/error states.
        probe = Task(path=self.store.root, id=f"_retro-{phase.product}-{phase.name}", title="",
                     product=phase.product, phase=phase.name)
        self._refuse_if_phase_not_admitted(probe)

        from datetime import date

        from ..walkthrough import capture

        walkthrough_dir = phase.path / "docs" / "walkthrough" / date.today().isoformat()
        # Scheduler work must remain bounded and headless; the CLI can add PNGs explicitly.
        capture(self.store, phase, walkthrough_dir, screenshots=False, log=self.log)
        names = personas or self.retro_default_personas()
        for n in names:
            valid_name(n)
        nxt = next_phase or next_phase_name(phase.name)
        have = persona_reports(phase, names)
        if skip_personas and names and not have:
            raise RuntimeError("garden retro --skip-personas found no persona reports on disk for "
                               f"{', '.join(names)}; run without --skip-personas, or reuse only "
                               "personas that already have a report under docs/reviews/")

        # Reload and persist under the same lock used by ticks. A fresh Scheduler may otherwise
        # hold a stale State snapshot while an automatic tick queues this phase during manual
        # validation, producing two requests before either preparation notices the other.
        with self._controller_lock():
            self.state = type(self.state)(self.state.path)
            entry = next((e for e in self._retro_list() if e.get("phase") == phase.key), None)
            if entry is None:
                if not self_prod:
                    raise RuntimeError("garden retro needs a product with `self: true` (the garden's own repo) to "
                                       "open the retro PR; see docs/architecture.md")
                entry = {"phase": phase.key, "product": phase.product, "phase_name": phase.name,
                         "personas": names, "skip_personas": bool(skip_personas), "next_phase": nxt,
                         "self_product": self_prod, "stage": "queued", "persona_runs": {},
                         "no_file": bool(no_file), "request_id": uuid.uuid4().hex}
                self._retro_list().append(entry)
            elif (entry.get("automatic") and entry.get("stage") in {"queued", "preparing", "dispatching"}
                  and not self._closing_review_policy(phase)["eligible"]):
                # An explicit manual start is the supported policy override for a durable
                # automatic request that is waiting on a newly introduced gate.
                entry["automatic"] = False
                entry.pop("waiting_reason", None)
            if entry.get("stage") not in {"queued", "preparing", "dispatching"}:
                failed = list((entry.get("persona_failures") or {}).keys())
                if entry.get("stage") != "personas" or not failed:
                    return entry
                for name in failed:
                    entry.get("persona_runs", {}).pop(name, None)
                entry.pop("persona_failures", None)
                entry["stage"] = "queued"
            owner_pid = int(entry.get("preparation_pid") or 0)
            if entry.get("stage") != "queued" and owner_pid:
                try:
                    os.kill(owner_pid, 0)
                except (ProcessLookupError, PermissionError):
                    pass
                else:
                    return entry
            request_id = str(entry.setdefault("request_id", uuid.uuid4().hex))
            claim = uuid.uuid4().hex
            entry.update(stage="preparing", preparation_claim=claim, preparation_pid=os.getpid())
            self.state.save()
        entry["preparation_action"] = "start"
        self._closing_review_claims.append((request_id, claim, "start"))
        self.prepare_claimed_closing_reviews(TickReport())
        return next(item for item in self._retro_list() if item.get("request_id") == request_id)

    def _prepare_retro_start(self, phase: Phase, entry: dict[str, Any],
                             source: str) -> dict[str, Any]:
        """Prepare initial persona inputs without publishing or launching model jobs."""
        self.require_maintenance_running()
        if not entry.get("self_product"):
            raise RuntimeError("garden retro needs a product with `self: true`")
        from datetime import date

        from ..walkthrough import capture

        walkthrough_dir = phase.path / "docs" / "walkthrough" / date.today().isoformat()
        capture(self.store, phase, walkthrough_dir, screenshots=False, log=self.log)
        names = list(entry["personas"])
        have = self._reports_for_entry(phase, {**entry, "source": source})
        missing = [] if entry.get("skip_personas") else [name for name in names if name not in have]
        probe = self._phase_persona_probe(phase)
        available = self.review_slots_free_for(probe)
        if self.runner_for(probe).name != "remote":
            available = min(available, self.local_slots_free(probe.id))
        prepared: list[tuple[str, dict[str, Any]]] = []
        for name in missing[:max(0, available)]:
            run_id = f"retro-persona-{uuid.uuid4().hex}"
            payload = self.prepare_persona_phase(phase, name, run_id=run_id, source=source)
            prepared.append((name, payload))
        waiting = ""
        if len(prepared) < len(missing):
            waiting = (f"waiting for review capacity ({len(self.review_runs_active())}/"
                       f"{self.review_parallel_limit()} running)")
        return {"personas": prepared, "missing": missing, "have": have, "waiting": waiting}

    def _commit_prepared_retro_start(self, entry: dict[str, Any], prepared: dict[str, Any]) -> None:
        """Publish exact initial persona run identities while holding ``tick.lock``."""
        roles = prepared["personas"]
        for name, payload in roles:
            run_id = payload["run"].run_id
            entry.setdefault("persona_runs", {})[name] = run_id
            entry.setdefault("persona_launch_claims", {})[name] = {
                "run_id": run_id, "claimed_at": now_iso(),
            }
            self._commit_prepared_aux(payload)
        entry["stage"] = "personas"
        if prepared["waiting"]:
            entry["waiting_reason"] = prepared["waiting"]
        else:
            entry.pop("waiting_reason", None)
        if not prepared["missing"] and entry.get("automatic"):
            self._claim_retro_preparation(entry, "reconcile")

    def _launch_prepared_retro_start(self, prepared: dict[str, Any]) -> None:
        """Launch the initial persona payloads whose identities are already durable."""
        for _name, payload in prepared["personas"]:
            self._launch_prepared_aux(payload)

    def _discard_prepared_retro_start(self, prepared: dict[str, Any], reason: str) -> None:
        for _name, payload in prepared["personas"]:
            self._discard_prepared_aux(payload, reason)

    def _phase_persona_probe(self, phase: Phase) -> Task:
        return Task(path=self.store.root, id=f"_{phase.product}-{phase.name}", title="",
                    product=phase.product, phase=phase.name)

    def _reconcile_phase_persona_run(self, phase: Phase, entry: dict[str, Any], name: str) -> bool:
        """Reconnect a persisted role reservation to the durable run/aux records.

        The retro request is saved before launch. If the controller exits after the run is
        created, the same run id is adopted here instead of starting a second model job.
        """
        run_id = str((entry.get("persona_runs") or {}).get(name) or "")
        if not run_id:
            return False
        probe = self._phase_persona_probe(phase)
        run = next((candidate for candidate in self.runs.runs_for(probe.id)
                    if candidate.run_id == run_id), None)
        if run is None:
            return False
        if not any(aux.get("run_id") == run_id for aux in self._aux_list()):
            self._aux_list().append({
                "run_id": run_id, "task": probe.id, "kind": "persona", "id": probe.id,
                "product": phase.product, "phase": phase.name, "persona": name,
                "target": "phase", "file_tasks": False, "min_severity": "low",
            })
            self.state.save()
        return True

    def _dispatch_retro_personas(self, phase: Phase, entry: dict[str, Any], names: list[str]) -> list[str]:
        """Fill currently admitted review slots, durably reserving each role before launch."""
        launched: list[str] = []
        probe = self._phase_persona_probe(phase)
        for name in names:
            if self._reconcile_phase_persona_run(phase, entry, name):
                continue
            if self.review_slots_free_for(probe) <= 0:
                entry["waiting_reason"] = (
                    f"waiting for review capacity ({len(self.review_runs_active())}/"
                    f"{self.review_parallel_limit()} running)"
                )
                break
            runner_name = "remote" if self.runner_for(probe).name == "remote" else "local"
            if runner_name == "local" and self.local_slots_free(probe.id) <= 0:
                entry["waiting_reason"] = "waiting for local execution capacity"
                break
            run_id = f"retro-persona-{uuid.uuid4().hex}"
            entry.setdefault("persona_runs", {})[name] = run_id
            entry.setdefault("persona_launch_claims", {})[name] = {
                "run_id": run_id, "claimed_at": now_iso(),
            }
            self.state.save()
            try:
                run = self.dispatch_persona_phase(phase, name, run_id=run_id)
            except Exception as exc:  # launch races are durable waiting state, not a lost retro
                # The durable role remains reserved. A later tick either adopts the run
                # record created by the failed launch or retries the same run identity.
                if not self._reconcile_phase_persona_run(phase, entry, name):
                    entry["persona_runs"].pop(name, None)
                    entry.get("persona_launch_claims", {}).pop(name, None)
                entry["waiting_reason"] = f"persona `{name}` launch deferred: {exc}"
                self.state.save()
                break
            entry["persona_runs"][name] = run.run_id
            entry.get("persona_launch_claims", {}).pop(name, None)
            entry.pop("waiting_reason", None)
            self.state.save()
            launched.append(name)
        return launched

    def _prepare_deferred_retro_personas(
        self, phase: Phase, entry: dict[str, Any], names: list[str], source: str
    ) -> list[tuple[str, dict[str, Any]]]:
        """Build exact source-bound payloads for missing later-stage persona roles."""
        prepared: list[tuple[str, dict[str, Any]]] = []
        probe = self._phase_persona_probe(phase)
        available = self.review_slots_free_for(probe)
        if self.runner_for(probe).name != "remote":
            available = min(available, self.local_slots_free(probe.id))
        for name in names[:max(0, available)]:
            if (entry.get("persona_runs") or {}).get(name):
                continue
            run_id = f"retro-persona-{uuid.uuid4().hex}"
            payload = self.prepare_persona_phase(
                phase, name, run_id=run_id, source=source
            )
            prepared.append((name, payload))
        return prepared

    def _commit_prepared_retro_personas(
        self, entry: dict[str, Any], prepared: list[tuple[str, dict[str, Any]]]
    ) -> list[tuple[str, dict[str, Any]]]:
        """Admit and publish deferred roles while holding ``tick.lock``."""
        phase = self.store.phase(entry["product"], entry["phase_name"])
        probe = self._phase_persona_probe(phase)
        # Preparing an aux payload creates its not-yet-launched run record. Admission helpers
        # therefore already count these exact reservations; add only those reservations back
        # before applying the current limit so concurrent unrelated work still reduces slots.
        prepared_ids = {payload["run"].run_id for _name, payload in prepared}
        reserved = sum(run.run_id in prepared_ids for run in self.review_runs_active())
        available = self.review_slots_free_for(probe) + reserved
        if self.runner_for(probe).name != "remote":
            available = min(available, self.local_slots_free(probe.id) + len(prepared))
        admitted = prepared[:max(0, available)]
        deferred = prepared[len(admitted):]
        for name, payload in admitted:
            run_id = payload["run"].run_id
            entry.setdefault("persona_runs", {})[name] = run_id
            entry.setdefault("persona_launch_claims", {})[name] = {
                "run_id": run_id, "claimed_at": now_iso(),
            }
            self._commit_prepared_aux(payload)
        if deferred:
            entry["waiting_reason"] = (
                f"waiting for review capacity ({len(self.review_runs_active())}/"
                f"{self.review_parallel_limit()} running)"
            )
            self._discard_prepared_retro_personas(
                deferred, "persona preparation exceeded current review capacity"
            )
        else:
            entry.pop("waiting_reason", None)
        return admitted

    def _launch_prepared_retro_personas(
        self, prepared: list[tuple[str, dict[str, Any]]]
    ) -> None:
        for _name, payload in prepared:
            self._launch_prepared_aux(payload)

    def _discard_prepared_retro_personas(
        self, prepared: list[tuple[str, dict[str, Any]]], reason: str
    ) -> None:
        for _name, payload in prepared:
            self._discard_prepared_aux(payload, reason)

    def _record_retro_persona_failure(self, run: Run, name: str, detail: str) -> bool:
        """Attach a failed phase-persona run to its owning retro request, if any."""
        for entry in self._retro_list():
            if entry.get("stage") != "personas":
                continue
            if (entry.get("persona_runs") or {}).get(name) != run.run_id:
                continue
            reason = f"persona `{name}` failed: {detail}"
            failures = entry.setdefault("persona_failures", {})
            failures[name] = reason
            entry["waiting_reason"] = "; ".join(failures.values()) + \
                f"; retry with `garden retro {entry['phase']}`"
            self.state.save()
            return True
        return False

    def _clear_retro_persona_failure(self, run: Run, name: str) -> None:
        for entry in self._retro_list():
            if (entry.get("persona_runs") or {}).get(name) != run.run_id:
                continue
            failures = entry.get("persona_failures") or {}
            failures.pop(name, None)
            if failures:
                entry["waiting_reason"] = "; ".join(failures.values()) + \
                    f"; retry with `garden retro {entry['phase']}`"
            else:
                entry.pop("persona_failures", None)
                entry.pop("waiting_reason", None)
            self.state.save()
            return

    def _prepare_reconcile(self, entry: dict[str, Any], run_id: str = "") -> dict[str, Any]:
        """Build one immutable reconciliation launch payload outside ``tick.lock``."""
        self.require_maintenance_running()
        phase = self.store.phase(entry["product"], entry["phase_name"])
        admission_probe = Task(path=self.store.root, id=f"_retro-{phase.product}-{phase.name}",
                               title="", product=phase.product, phase=phase.name)
        self._refuse_if_phase_not_admitted(admission_probe)
        probe = Task(path=self.store.root, id=f"_retro-{phase.product}-{phase.name}", title="",
                     product=entry["self_product"], phase="")
        runner = self.runner_for(probe, "local", str(self.cfg.get("review.harness") or ""))
        self._raise_if_harness_paused(runner.harness.name if runner.harness else "")
        difficulty = str(self.effective("retro.difficulty") or "hard")
        run = self._new_local_run(
            probe.id, "retro", "retro",
            run_id=run_id,
            resource_weight=self.cfg.product_resource_weight(probe.product),
        )
        run.model = self.retro_model_for(runner) or self.model_for(probe, runner, difficulty)
        run.difficulty = difficulty
        base = self.final_base_for(probe)
        branch = f"garden/retro-{phase.product}-{phase.name}"
        wt = self.cfg.worktree_path(f"_retro-{phase.product}-{phase.name}")
        canonical = self.prepare_canonical_run(probe, run, runner, branch, base)
        if canonical is not None:
            wt = canonical
        else:
            repo = self.repo_for(probe)
            self._recheck_local_materialization(run, "retro checkout materialization")
            gitops.fetch(repo)
            gitops.prepare_worktree(repo, wt, branch, base)
        friction, reported, comment_friction, reports, task_rows, merged = self._retro_materials(phase, entry["personas"])
        references: dict[str, str] = {}
        text = reconcile_brief(self.store, phase, base, friction, reported, comment_friction,
                               reports, task_rows, merged, entry["next_phase"], references)
        from ..reference_snapshot import write_reference_files

        write_reference_files(run.path, references, self.cfg.data)
        run.worktree = str(wt)
        run.brief_tokens = max(1, len(text) // 4)
        return {"run": run, "runner": runner, "worktree": wt, "text": text,
                "phase": phase, "probe": probe, "branch": branch, "base": base}

    def _commit_prepared_reconcile(self, entry: dict[str, Any], prepared: dict[str, Any]) -> None:
        """Persist the exact prepared launch identity while holding ``tick.lock``."""
        run = prepared["run"]
        entry.update({"recon_run_id": run.run_id, "recon_task": prepared["probe"].id,
                      "branch": prepared["branch"], "worktree": str(prepared["worktree"]),
                      "base": prepared["base"], "slug": self.slug_for(prepared["probe"]) or "",
                      "stage": "launching_reconcile"})
        run.save()

    def _discard_prepared_reconcile(self, prepared: dict[str, Any], reason: str) -> None:
        """Close an admitted run whose prepared input lost its durable launch claim."""
        run = prepared["run"]
        run.status = "failed"
        run.error = reason
        run.finished_at = now_iso()
        run.preparer_pid = None
        run.save()

    def _launch_prepared_reconcile(self, prepared: dict[str, Any]) -> None:
        """Launch exactly the payload whose identity was durably committed under the lock."""
        self.require_execution_authority()
        run = prepared["run"]
        runner = prepared["runner"]
        phase = prepared["phase"]
        probe = prepared["probe"]
        runner.start(run, prepared["worktree"], prepared["text"])
        self.events.emit("dispatch", run.task_id, run=run.run_id, mode="retro", model=run.model,
                         harness=run.harness, phase=probe.phase)
        self.events.emit("retro_reconcile", "", phase=phase.key, run=run.run_id,
                         branch=prepared["branch"])

    def _dispatch_reconcile(self, entry: dict[str, Any], run_id: str = "") -> None:
        """Synchronous/manual compatibility path for reconciliation dispatch."""
        prepared = self._prepare_reconcile(entry, run_id=run_id)
        self._commit_prepared_reconcile(entry, prepared)
        self.state.save()
        self._launch_prepared_reconcile(prepared)
        entry["stage"] = "reconciling"
        self.state.save()

    @staticmethod
    def _clear_retro_preparation(entry: dict[str, Any]) -> None:
        for key in ("preparation_claim", "preparation_pid", "preparation_action", "preparation_names"):
            entry.pop(key, None)

    def _claim_retro_preparation(self, entry: dict[str, Any], action: str,
                                 names: list[str] | None = None) -> None:
        """Persist a later-stage launch claim for execution after ``tick.lock`` is released."""
        claim = uuid.uuid4().hex
        request_id = str(entry.setdefault("request_id", uuid.uuid4().hex))
        entry.update(stage=f"preparing_{action}", preparation_claim=claim,
                     preparation_pid=os.getpid(), preparation_action=action)
        if names is not None:
            entry["preparation_names"] = names
        if action == "reconcile":
            entry.setdefault("reconcile_launch_run_id", f"retro-reconcile-{uuid.uuid4().hex}")
        self._closing_review_claims.append((request_id, claim, action))

    def retro_pending(self, phase_key: str) -> dict[str, Any] | None:
        """The persona-wait state of the phase's active retro, if any is stuck waiting: `{"done":
        n, "total": m}`. For `garden status` and the phase page. None once every requested
        report is in (the reconciliation dispatches) or if no retro is running for the phase."""
        for entry in self._retro_list():
            if entry.get("phase") != phase_key or entry.get("stage") != "personas":
                continue
            phase = self.store.phase(entry["product"], entry["phase_name"])
            have = self._reports_for_entry(phase, entry)
            pending: dict[str, Any] = {"done": len(have), "total": len(entry["personas"])}
            if len(have) < len(entry["personas"]) and entry.get("waiting_reason"):
                pending["reason"] = str(entry["waiting_reason"])
            return pending
        return None

    def reap_retro(self, rep: TickReport) -> None:
        for entry in list(self._retro_list()):
            if not self.phase_is_authorized(str(entry.get("product")), str(entry.get("phase_name"))):
                continue
            try:
                effect_key = (f"retro-reap:{entry.get('request_id', entry['phase'])}:"
                              f"{entry.get('stage')}")
                with self.phase_effect(str(entry["product"]), str(entry["phase_name"]), effect_key):
                    self._reap_retro_entry(entry, rep)
            except (PermissionError, MultiplayerUnavailable):
                continue
            except Exception as e:  # noqa: BLE001 - one bad retro must not sink the tick
                rep.errors.append(f"retro {entry.get('phase')}: {e}")
                self._retro_remove(entry)
        self.state.save()

    def _reap_retro_entry(self, entry: dict[str, Any], rep: TickReport) -> None:
            try:
                if entry.get("stage") == "launching_reconcile":
                    run_id = str(entry.get("recon_run_id") or "")
                    probe_id = str(entry.get("recon_task") or "")
                    run = next((r for r in self.runs.runs_for(probe_id)
                                if r.run_id == run_id), None)
                    owner_pid = int(run.preparer_pid or 0) if run else 0
                    if owner_pid:
                        try:
                            os.kill(owner_pid, 0)
                        except ProcessLookupError:
                            pass
                        except PermissionError:
                            return
                        else:
                            return
                    if run is not None and run.pid is not None and not run.finished_at:
                        entry["stage"] = "reconciling"
                    else:
                        # The controller stopped after committing the launch identity but
                        # before a worker became observable. Retire that reservation and
                        # prepare a new exact input; never adopt it as a completed launch.
                        if run is not None and not run.finished_at:
                            run.status = "failed"
                            run.error = "reconciliation launch interrupted before worker start"
                            run.finished_at = now_iso()
                            run.preparer_pid = None
                            run.save()
                        entry["stage"] = "personas"
                        entry.pop("reconcile_launch_run_id", None)
                    self.state.save()
                if entry.get("stage") in {"preparing_personas", "preparing_reconcile"}:
                    owner_pid = int(entry.get("preparation_pid") or 0)
                    if owner_pid:
                        try:
                            os.kill(owner_pid, 0)
                        except ProcessLookupError:
                            pass
                        except PermissionError:
                            return
                        else:
                            return
                    action = str(entry.get("preparation_action") or "")
                    self._clear_retro_preparation(entry)
                    entry["stage"] = "personas"
                    if action == "reconcile" and entry.get("reconcile_launch_run_id"):
                        run_id = str(entry["reconcile_launch_run_id"])
                        probe_id = f"_retro-{entry['product']}-{entry['phase_name']}"
                        run = next((r for r in self.runs.runs_for(probe_id) if r.run_id == run_id), None)
                        if run is not None:
                            entry.update(stage="reconciling", recon_run_id=run_id, recon_task=probe_id)
                            return
                if entry.get("stage") == "personas":
                    # Gated on reports actually on disk, not on whether a run is still active:
                    # a concurrent tick reading state mid-dispatch (start_retro saves after each
                    # persona it kicks off) must not mistake "no run recorded yet" for "done"
                    # and reconcile before every persona has even started.
                    phase = self.store.phase(entry["product"], entry["phase_name"])
                    have = self._reports_for_entry(phase, entry)
                    if len(have) < len(entry["personas"]):
                        missing = [name for name in entry["personas"] if name not in have]
                        self._claim_retro_preparation(entry, "personas", missing)
                        return
                    admission_probe = Task(
                        path=self.store.root, id=f"_retro-{phase.product}-{phase.name}", title="",
                        product=phase.product, phase=phase.name,
                    )
                    if refusal := self.phase_admission_refusal(admission_probe):
                        if entry.get("reconcile_phase_wait") != refusal:
                            entry["reconcile_phase_wait"] = refusal
                            rep.transitions.append(
                                f"retro {entry['phase']} reconcile deferred ({refusal})"
                            )
                        return
                    entry.pop("reconcile_phase_wait", None)
                    entry.pop("waiting_reason", None)
                    probe = Task(path=self.store.root, id=f"_retro-{phase.product}-{phase.name}", title="",
                                product=entry["self_product"], phase="")
                    harness_name = self.resolved_harness_name(probe, str(self.cfg.get("review.harness") or ""))
                    if self.is_harness_paused(harness_name):
                        # Every persona is in but the reconcile's own harness is down: wait for
                        # the probe to resume it rather than dispatching into the same account
                        # limit (mirrors the ready-queue's own pause skip in dispatch_ready).
                        if not entry.get("reconcile_paused"):
                            entry["reconcile_paused"] = True
                            rep.transitions.append(f"retro {entry['phase']} reconcile deferred ({harness_name} paused)")
                        return
                    entry.pop("reconcile_paused", None)
                    self._claim_retro_preparation(entry, "reconcile")
                    return
                if entry.get("stage") != "reconciling":
                    return
                run = next((r for r in self.runs.runs_for(entry["recon_task"]) if r.run_id == entry.get("recon_run_id")), None)
                if run is None:
                    rep.errors.append(f"retro {entry['phase']}: reconcile run vanished")
                    self._retro_remove(entry)
                    return
                probe = Task(path=self.store.root, id=entry["recon_task"], title="", product=entry["self_product"], phase="")
                runner = self.runner_for(probe, run.runner, run.harness)
                if not self._finished_or_timed_out(run, runner):
                    return
                final = ""
                collected: dict[str, Any] = {}
                if run.status != "timeout":
                    run.exit_code = run.read_exit_code()
                    run.finished_at = now_iso()
                    collected = runner.collect(run)
                    run.usage = collected.get("usage") or {}
                    run.cost_usd = collected.get("cost_usd")
                    run.model = str(collected.get("model") or run.model)
                    run.error = collected.get("error") or ""
                    final = collected.get("final_text") or ""
                    if final and not (run.path / "final.md").exists():
                        (run.path / "final.md").write_text(final)
                    run.status = "env_error" if collected.get("env_error") else "done"
                    run.save()
                self.events.emit("run_finished", run.task_id, run=run.run_id, mode=run.mode,
                                 cost_usd=run.cost_usd, usage=run.usage, status=run.status)
                if collected.get("env_error"):
                    # The reconcile's own harness hit its account limit, not the retro itself:
                    # pause the harness and put the entry back to wait for reconcile-time
                    # gating above to redispatch once a probe resumes it, instead of treating
                    # an unparsed final.md as a failed retro and dropping the entry for good.
                    self._pause_for_env_error(run, collected)
                    entry["stage"] = "personas"
                    entry.pop("reconcile_launch_run_id", None)
                    entry["reconcile_paused"] = True
                    rep.transitions.append(f"retro {entry['phase']} reconcile paused (env_error)")
                    return
                self._finish_retro(entry, run, final, rep)
                self._retro_remove(entry)
            except Exception:
                raise

    def _retro_failed(self, phase: Phase, step: str, error: gitops.GitError, rep: TickReport) -> None:
        """Record a failed commit or push inside a retro reconciliation (CG-147): logged (so a
        human watching `garden watch` or the web dashboard sees it even on an otherwise silent
        tick), added to the tick's own errors (the CLI's `tick`/`watch` output), and emitted as
        a durable event so it outlives the in-memory log. `error`'s own text names the worktree
        (GitError includes the `cwd` it ran in)."""
        msg = f"retro {phase.key}: {step} failed: {error}"
        self.log(msg)
        rep.errors.append(msg)
        self.events.emit("retro_failed", "", phase=phase.key, step=step, error=str(error))

    @staticmethod
    def _retro_priority(v: Any) -> int:
        try:
            return int(v)
        except (TypeError, ValueError):
            return 3

    def _write_worktree_draft(self, tasks_dir: Path, tid: str, phase: Phase, target_phase: str,
                              title: str, body: str, priority: int, difficulty: str) -> None:
        """Write one draft task file into the retro worktree (the target phase may not exist on
        disk yet, so this cannot go through `store.create_task`); it lands with the retro PR."""
        t = Task(path=tasks_dir / f"{tid}-{slugify(title)}.md", id=tid, title=title,
                 status=Status.DRAFT, product=phase.product, phase=target_phase, priority=priority,
                 difficulty=difficulty if difficulty in ("easy", "medium", "hard") else "medium",
                 discovered_from=f"retro:{phase.key}", created=now_iso(), updated=now_iso(), body=body)
        tasks_dir.mkdir(parents=True, exist_ok=True)
        t.path.write_text(t.render())

    def _file_retro_features(self, phase: Phase, next_phase: str, rev: dict[str, Any], wt: Path,
                             rel_product: Path, existing_titles: dict[str, str],
                             alloc: Callable[[], str],
                             persona_feats: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
        """Turn the reconciliation's `features`, plus any structured `features` a persona
        reported (CG-188), into draft task files inside the retro's own worktree, so they land
        with the same PR as the retro document and the next phase's goals draft (the next phase
        may not exist on disk yet, so this cannot go through `store.create_task`, which requires
        an already-discovered phase). Skips whatever `resolve_features` flags as a duplicate;
        each id it uses comes from `alloc`, which reserves it durably (see `_finish_retro`)."""
        resolved = resolve_features(rev, existing_titles, persona_feats)
        tasks_dir = wt / rel_product / next_phase / "tasks"
        filed: list[dict[str, Any]] = []
        for f in resolved:
            if f["skip"]:
                filed.append({**f, "task_id": "", "status": "skipped"})
                self.log(f"retro {phase.key}: feature {f['title']!r} skipped ({f['reason']})")
                continue
            tid = alloc()
            body = f"## Goal\n\n{f['body'] or f['title']}\n\n## Context\n\nProposed at the {phase.key} retro."
            if f.get("source"):
                body += f" Raised by the {f['source']} persona."
            if f["rationale"]:
                body += f" {f['rationale']}"
            body += "\n"
            self._write_worktree_draft(tasks_dir, tid, phase, next_phase, f["title"], body,
                                       self._retro_priority(f.get("priority")), f["difficulty"])
            existing_titles[f["title"].strip().lower()] = tid
            filed.append({**f, "task_id": tid, "status": "draft"})
        return filed

    def _file_retro_findings(self, phase: Phase, next_phase: str, persona_findings: dict[str, list[dict[str, Any]]],
                             wt: Path, rel_product: Path, existing_titles: dict[str, str],
                             alloc: Callable[[], str]) -> list[dict[str, Any]]:
        """Turn every persona finding into a draft task in the next phase (CG-187): every
        severity, not only high, priority from severity, and findings that say the same thing
        across personas collapsed into one task via `group_findings`'s title match. Mirrors
        `_file_retro_features`: writes directly into the retro's own worktree, and shares its
        durable id allocator."""
        flat = flatten_findings(persona_findings)
        if not flat:
            return []
        groups = group_findings(flat)
        resolved = resolve_findings(groups, existing_titles)
        tasks_dir = wt / rel_product / next_phase / "tasks"
        filed: list[dict[str, Any]] = []
        for f in resolved:
            if f["skip"]:
                filed.append({**f, "task_id": "", "status": "skipped"})
                self.log(f"retro {phase.key}: finding {f['summary']!r} skipped ({f['reason']})")
                continue
            tid = alloc()
            personas = f["personas"]
            provenance = f"persona:{personas[0]}:{phase.key}"
            title = finding_title(f)
            body = finding_body(f, personas, provenance)
            priority = SEVERITY_PRIORITY.get(str(f.get("severity")), 2)
            t = Task(path=tasks_dir / f"{tid}-{slugify(title)}.md", id=tid, title=title,
                     status=Status.DRAFT, product=phase.product, phase=next_phase, priority=priority,
                     difficulty="medium", discovered_from=provenance,
                     created=now_iso(), updated=now_iso(), body=body)
            tasks_dir.mkdir(parents=True, exist_ok=True)
            t.path.write_text(t.render())
            existing_titles[title.strip().lower()] = tid
            filed.append({**f, "task_id": tid, "status": "draft"})
        return filed

    def _file_retro_followups(self, phase: Phase, next_phase: str, rev: dict[str, Any], wt: Path,
                              rel_product: Path, existing_titles: dict[str, str],
                              alloc: Callable[[], str]) -> list[dict[str, Any]]:
        """File the verdict's `followups` as draft tasks in the next phase (in the worktree, like
        features): a `close_with_followups` verdict carries work worth doing next but not blocking
        the close. Each id comes from `alloc`, the shared durable allocator."""
        resolved = resolve_retro_tasks(rev.get("followups"), existing_titles)
        tasks_dir = wt / rel_product / next_phase / "tasks"
        filed: list[dict[str, Any]] = []
        for f in resolved:
            if f["skip"]:
                filed.append({**f, "task_id": "", "status": "skipped"})
                self.log(f"retro {phase.key}: follow-up {f['title']!r} skipped ({f['dup_reason']})")
                continue
            tid = alloc()
            body = (f"## Goal\n\n{f['body'] or f['title']}\n\n## Context\n\n"
                    f"A follow-up carried into {next_phase} by the {phase.key} retro verdict.\n")
            self._write_worktree_draft(tasks_dir, tid, phase, next_phase, f["title"], body,
                                       self._retro_priority(f.get("priority")), f["difficulty"])
            existing_titles[f["title"].strip().lower()] = tid
            filed.append({**f, "task_id": tid, "status": "draft"})
        return filed

    def _file_retro_blocking(self, phase: Phase, rev: dict[str, Any],
                             existing_titles: dict[str, str]) -> list[dict[str, Any]]:
        """File the verdict's `blocking` items as draft tasks in the *current* phase, live (the
        phase exists), with `retro_blocking` and a freeze exception so a frozen phase still
        dispatches them and `close-phase` refuses until they are done. Skips a duplicate title."""
        resolved = resolve_retro_tasks(rev.get("blocking"), existing_titles)
        filed: list[dict[str, Any]] = []
        for b in resolved:
            if b["skip"]:
                filed.append({**b, "task_id": "", "status": "skipped"})
                self.log(f"retro {phase.key}: blocking task {b['title']!r} skipped ({b['dup_reason']})")
                continue
            reason = b["reason"] or "retro reopen: must land before the phase can close"
            body = (f"## Goal\n\n{b['body'] or b['title']}\n\n## Context\n\n"
                    f"Filed by the {phase.key} retro `reopen` verdict: it must land before the phase "
                    f"can close. Reason: {reason}\n")
            if b["acceptance"]:
                body += "\n## Acceptance criteria\n\n" + "\n".join(
                    f"- [ ] {criterion}" for criterion in b["acceptance"]
                ) + "\n"
            t = self.store.create_task(phase.product, phase.name, b["title"], body,
                                       priority=self._retro_priority(b.get("priority")), status="draft",
                                       reading=b["reading"],
                                       difficulty=b["difficulty"] if b["difficulty"] in ("easy", "medium", "hard") else "medium")
            t.discovered_from = f"retro:{phase.key}"
            t.retro_blocking = True
            t.freeze_exception = True
            t.freeze_exception_reason = reason
            t.log(f"filed by the {phase.key} retro reopen verdict (blocking)")
            gaps = brief_gaps(self.store, t)
            if gaps:
                t.log("brief_gap: " + "; ".join(gaps))
            self.store.save(t)
            self.store.invalidate_tasks()
            existing_titles[b["title"].strip().lower()] = t.id
            filed.append({**b, "task_id": t.id, "status": "draft", "brief_gaps": gaps})
            self.events.emit("retro_blocking_filed", t.id, phase=phase.key, title=b["title"])
        return filed

    def _finish_retro(self, entry: dict[str, Any], run: Run, final: str, rep: TickReport) -> None:
        phase = self.store.phase(entry["product"], entry["phase_name"])
        rev = parse_retro(final)
        if not rev:
            rep.errors.append(f"retro {phase.key}: no verdict ({run.error[:100] or 'see final.md'})")
            return
        next_phase = entry["next_phase"]
        reports = persona_reports(phase, entry["personas"])
        wt = Path(entry["worktree"])
        base, branch = entry["base"], entry["branch"]
        rel_phase = phase.path.relative_to(self.store.root)
        rel_product = phase.path.parent.relative_to(self.store.root)
        retro_path = wt / rel_phase / "docs" / "retro.md"
        goals_path = wt / rel_product / next_phase / "goals.md"
        retro_path.parent.mkdir(parents=True, exist_ok=True)
        goals_path.parent.mkdir(parents=True, exist_ok=True)
        questions = [f for f in (self._file_question(
            phase, item, i, run.run_id, source=f"retro:{phase.key}", document_paths=[
                retro_path, goals_path, phase.path / "docs" / "retro.md", phase.path.parent / next_phase / "goals.md",
            ]
        ) for i, item in enumerate(rev.get("questions") or []) if isinstance(item, dict)) if f]
        for question in questions:
            question.update(retro_worktree=str(wt), retro_branch=branch, retro_base=base,
                            live_retro_path=str(phase.path / "docs" / "retro.md"))
            decision = self.state.get("_decisions").get(question["decision_id"])
            if isinstance(decision, dict):
                decision.update(question)
        existing_titles = {t.title.strip().lower(): t.id for t in self.store.tasks().values()}
        # Blocking tasks go live into the current phase (it exists, they must dispatch and block
        # the close); features, followups and findings go into the worktree next phase (which may
        # not exist yet) to land with the retro PR. Blocking is filed first, as real files, so the
        # reservations below allocate past them.
        blocking = [] if entry.get("no_file") else self._file_retro_blocking(phase, rev, existing_titles)
        # Each worktree draft's id is reserved durably before it is written, so no live task
        # creator (discovered work, another retro, the planner) hands out the same id between now
        # and the PR merging — a collision that would otherwise disable every page and tick. The
        # reservation survives a restart; a re-run first releases this phase's prior batch, so an
        # abandoned earlier attempt's ids are reclaimed rather than leaked, and a merged draft's
        # id is pruned from the ledger once its file exists (store.prune_reservations, each tick).
        owner = f"retro:{phase.key}"
        if not entry.get("no_file"):
            self.store.release_reservation(owner)

        def alloc() -> str:
            return self.store.reserve_ids(phase.product, 1, owner=owner, reason=f"{phase.key} retro draft")[0]

        if entry.get("no_file"):
            filed, filed_findings, followups = [], [], []
        else:
            persona_feats = persona_features(self._persona_sections(phase, reports))
            filed = self._file_retro_features(phase, next_phase, rev, wt, rel_product, existing_titles, alloc,
                                              persona_feats=persona_feats)
            persona_findings = self._persona_findings(phase, reports)
            filed_findings = self._file_retro_findings(phase, next_phase, persona_findings, wt, rel_product,
                                                        existing_titles, alloc)
            followups = self._file_retro_followups(phase, next_phase, rev, wt, rel_product, existing_titles, alloc)
        summary = phase_summary(self.events.read(), {t.id: t for t in phase.tasks})
        ledger_path = operator_spend_path(self.store.root, self.cfg, phase.path.parent)
        operator_records = read_operator_records(ledger_path)
        operator = operator_attributed_summary(
            operator_records, since=summary["first_dispatch"],
            include=lambda record: record.get("product") == phase.product
            and attributed_phase_key(record) == phase.key)
        unattributed = operator_attributed_summary(
            operator_records, since=summary["first_dispatch"],
            include=lambda record: not record.get("product") or not record.get("phase"))
        numbers = numbers_section(summary["cost_usd"], operator["known_cost_usd"], summary["metrics"],
                                  operator_turns=operator["turns"],
                                  operator_priced_records=operator["priced_records"],
                                  operator_unpriced_records=operator["unpriced_records"],
                                  operator_ledger_path=ledger_path,
                                  unattributed_operator_cost_usd=unattributed["known_cost_usd"],
                                  unattributed_operator_turns=unattributed["turns"],
                                  unattributed_operator_priced_records=unattributed["priced_records"],
                                  unattributed_operator_unpriced_records=unattributed["unpriced_records"])
        retro_path.write_text(render_retro_doc(phase, rev, reports, self.store, filed=filed,
                                               filed_findings=filed_findings, filed_questions=questions, followups=followups,
                                               blocking=blocking, next_phase=next_phase,
                                               difficulty=run.difficulty, model=run.model, numbers=numbers,
                                               no_file=bool(entry.get("no_file"))))
        goals_path.write_text(render_next_goals(phase, next_phase, rev, filed=filed, followups=followups))
        try:
            gitops.commit_all(wt, f"garden retro: {phase.key} retrospective and {next_phase} goals draft")
        except gitops.GitError as e:
            # A failed commit here (e.g. no git identity in the worktree, CG-147) must not just
            # vanish: the rendered retro doc is already on disk, uncommitted, and nothing else
            # will ever surface that. self.log reaches the running log a human can see (the web
            # dashboard, `garden watch`) even when the tick that hit this is otherwise silent;
            # the event makes it durable on the Timeline too, not just in a 200-entry ring buffer.
            self._retro_failed(phase, "commit", e, rep)
            return
        if gitops.commits_ahead(wt, base) == 0:
            rep.errors.append(f"retro {phase.key}: nothing to commit")
            return
        try:
            gitops.push(wt, branch, base=base)
        except gitops.GitError as e:
            self._retro_failed(phase, "push", e, rep)
            return
        n_items = len([f for f in rev.get("reconciliation") or [] if isinstance(f, dict)])
        n_filed = sum(1 for f in filed if f.get("task_id"))
        n_skipped = len(filed) - n_filed
        n_findings_filed = sum(1 for f in filed_findings if f.get("task_id"))
        n_findings_skipped = len(filed_findings) - n_findings_filed
        n_followups = sum(1 for f in followups if f.get("task_id"))
        n_blocking = sum(1 for b in blocking if b.get("task_id"))
        n_questions = len(questions)
        verdict = normalize_verdict(rev.get("verdict"))
        title = f"Retro: {phase.key} — reconcile friction and draft {next_phase} goals"
        body = (f"Retrospective for **{phase.key}**, produced by `garden retro`.\n\n"
                f"- verdict: **{PHASE_VERDICTS.get(verdict, 'none')}**\n"
                f"- reconciled {n_items} friction item(s) against what merged\n"
                f"- {len(reports)} persona report(s)\n"
                f"- {n_findings_filed} persona finding(s) filed as draft tasks in {next_phase}"
                + (f" ({n_findings_skipped} duplicate(s) skipped)" if n_findings_skipped else "") + "\n"
                f"- {n_filed} feature(s) filed as draft tasks in {next_phase}"
                + (f" ({n_skipped} duplicate(s) skipped)" if n_skipped else "") + "\n"
                + (f"- {n_followups} follow-up(s) filed in {next_phase}\n" if n_followups else "")
                + (f"- {n_blocking} blocking task(s) filed in {phase.key}\n" if n_blocking else "")
                + (f"- {n_questions} owner question(s) filed as decision cards\n" if n_questions else "")
                + f"- retro document: `{rel_phase.as_posix()}/docs/retro.md`\n"
                f"- next-phase goals draft: `{rel_product.as_posix()}/{next_phase}/goals.md`\n\n"
                f"{str(rev.get('summary', '')).strip()}\n")
        if entry.get("no_file"):
            body += "- task filing disabled (judge-only mode)\n"
        pr_url = ""
        slug = entry.get("slug") or ""
        if slug and self.github.available:
            try:
                pr = self.github.create_pr(slug, branch, base, title, body,
                                           draft=bool(self.effective("github.draft_pr", False, phase.product)))
                pr_url = pr.url
            except GitHubError as e:
                rep.errors.append(f"retro {phase.key}: branch pushed but PR failed: {e}")
        self.events.emit("retro_done", "", phase=phase.key, pr=pr_url, branch=branch, items=n_items, cost_usd=run.cost_usd)
        rep.transitions.append(f"retro {phase.key} -> {pr_url or branch}")
        if not entry.get("no_file"):
            self._apply_retro_verdict(phase, rev, followups, blocking, next_phase, pr_url)

    # ---- the verdict: close, close with follow-ups, or reopen --------------
    def _apply_retro_verdict(self, phase: Phase, rev: dict[str, Any], followups: list[dict[str, Any]],
                             blocking: list[dict[str, Any]], next_phase: str, pr_url: str) -> None:
        """Record the retro's phase verdict and act on it: `close`/`close_with_followups` close
        the phase at once (the owner decided closing does not wait for approval); `reopen` leaves
        the phase open and records a pending decision that approves the blocking tasks when
        accepted (see `retro_decide`). The record is what the phase page, the retro page and
        `close-phase` read."""
        verdict = normalize_verdict(rev.get("verdict"))
        at = now_iso()
        rec: dict[str, Any] = {
            "phase": phase.key, "verdict": verdict, "at": at, "next_phase": next_phase,
            "followup_ids": [f["task_id"] for f in followups if f.get("task_id")],
            "blocking_ids": [b["task_id"] for b in blocking if b.get("task_id")],
            "brief_gaps": {
                b["task_id"]: "; ".join(b["brief_gaps"])
                for b in blocking
                if b.get("task_id") and b.get("brief_gaps")
            },
            "pr": pr_url, "note": "", "accepted_by": "", "accepted_at": "", "status": "recorded",
        }
        if verdict in ("close", "close_with_followups"):
            # Close at once (the owner decided closing does not wait for approval), but do not
            # force past genuinely open work: if a task is still in flight, leave the phase open
            # with the verdict recorded so `close-phase` can follow it once the work lands.
            try:
                self.close_phase(phase, force=False)
                rec.update(status="accepted", accepted_by="retro", accepted_at=at)
            except RuntimeError as e:
                self.log(f"retro {phase.key}: verdict {verdict} recorded but the phase is not "
                         f"closeable yet: {e}")
        elif verdict == "reopen":
            rec["status"] = "pending"  # a decision: accept to approve the blocking tasks
        self.state.get("_retro_verdicts")[phase.key] = rec
        self.events.emit("retro_verdict", "", phase=phase.key, verdict=verdict or "none",
                         status=rec["status"], blocking=",".join(rec["blocking_ids"]),
                         followups=",".join(rec["followup_ids"]))
        self.state.save()

    def retro_verdict(self, phase_key: str) -> dict[str, Any] | None:
        """The recorded verdict for a phase (verdict, status, who accepted it and when, and the
        ids of the tasks it filed), or None if no retro has run. A copy, so callers can't mutate
        the stored record."""
        rec = self.state.get("_retro_verdicts").get(phase_key)
        return dict(rec) if isinstance(rec, dict) else None

    def pending_retro_verdicts(self) -> list[dict[str, Any]]:
        """Retro verdicts still waiting for a person's call: a `reopen` verdict not yet accepted
        or changed. Stamped with `phase_key`. What the Inbox shows and the badge counts."""
        out: list[dict[str, Any]] = []
        for phase_key, rec in self.state.get("_retro_verdicts").items():
            if isinstance(rec, dict) and rec.get("status") == "pending":
                out.append({**rec, "phase_key": phase_key})
        return sorted(out, key=lambda r: str(r["phase_key"]))

    def retro_blocking_open(self, phase: Phase) -> list[Task]:
        """The phase's `retro_blocking` tasks that are not yet done or cancelled -- what
        `close-phase` refuses on."""
        return [t for t in phase.tasks if t.retro_blocking and not t.status.terminal]

    def _approve_retro_blocking(self, phase: Phase, blocking_ids: list[str]) -> tuple[list[str], dict[str, str]]:
        """Run every still-draft reopen blocker through the ordinary approval gate.

        An incomplete item leaves the verdict pending: that task stays draft and its refusal is
        retained for the phase and Inbox cards, while complete blockers can proceed.
        """
        approved: list[str] = []
        refused: dict[str, str] = {}
        tasks = self.store.tasks()
        for tid in blocking_ids:
            t = tasks.get(tid)
            if t is None or t.status != Status.DRAFT:
                continue
            refusal = phase_refusal(phase, t)
            if refusal:
                refused[tid] = refusal
                self.log(f"retro {phase.key}: cannot approve blocking {tid}: {refusal}")
                continue
            gaps = brief_gaps(self.store, t)
            if gaps:
                refusal = f"{t.id} has an incomplete brief; fix it before approving: " + "; ".join(gaps)
                refused[tid] = refusal
                self.log(f"retro {phase.key}: cannot approve blocking {tid}: {refusal}")
                continue
            self._transition(t, Status.READY, "approved by the retro reopen verdict")
            approved.append(tid)
        if approved:
            self.store.invalidate_tasks()
        return approved, refused

    def close_accepted_reopens(self, rep: TickReport) -> None:
        """Close an accepted reopen verdict after all of its named blockers are terminal."""
        for key, rec in self.state.get("_retro_verdicts").items():
            if not isinstance(rec, dict) or rec.get("verdict") != "reopen" or rec.get("status") != "accepted":
                continue
            try:
                phase = self.store.phase(*key.split("/", 1))
            except (KeyError, ValueError):
                continue
            if not self.phase_is_authorized(phase.product, phase.name):
                continue
            if phase.closed or self.retro_blocking_open(phase):
                continue
            try:
                with self.phase_effect(phase.product, phase.name,
                                       f"retro-reopen-close:{phase.key}"):
                    self.close_phase(phase)
            except RuntimeError as e:
                rep.errors.append(f"retro {phase.key}: accepted reopen could not close: {e}")
            else:
                rec["closed_at"] = now_iso()
                self.events.emit("retro_reopen_closed", "", phase=phase.key)
                rep.transitions.append(f"retro {phase.key} blockers complete -> closed")

    def retro_decide(self, phase: Phase, choice: str, note: str = "", by: str = "cli") -> dict[str, Any]:
        """Accept or change a phase's retro verdict. `reopen` (re)opens the phase and approves
        its blocking tasks; `close`/`close_with_followups` close the phase (refusing on open
        tasks the way `close-phase` does). Records who decided and when."""
        with self.phase_effect(phase.product, phase.name, f"retro-decision:{phase.key}"):
            return self._retro_decide(phase, choice, note, by)

    def _retro_decide(self, phase: Phase, choice: str, note: str, by: str) -> dict[str, Any]:
        self.require_phase_authority(phase)
        choice = normalize_verdict(choice)
        if not choice:
            raise RuntimeError("choose one of: close, followups, reopen")
        vs = self.state.get("_retro_verdicts")
        if phase.key not in vs:
            raise RuntimeError(f"{phase.key} has no retro verdict to decide; run `garden retro {phase.key}` first")
        pending_blocking = [d for d in self.pending_decisions()
                            if d.get("kind") == "question" and d.get("source") == f"retro:{phase.key}"
                            and d.get("blocking")]
        if pending_blocking:
            raise RuntimeError("answer the retro's blocking question before accepting its verdict: "
                               + str(pending_blocking[0].get("question") or "(unnamed question)"))
        rec = vs[phase.key]
        if choice == "reopen":
            if phase.closed:
                self.reopen_phase(phase)
                phase = self.store.phase(phase.product, phase.name)
            _, refused = self._approve_retro_blocking(phase, list(rec.get("blocking_ids") or []))
            rec["brief_gaps"] = refused
        else:
            self.close_phase(phase)  # raises on open tasks, like close-phase
        status = "pending" if choice == "reopen" and rec.get("brief_gaps") else "accepted"
        rec.update(verdict=choice, status=status, note=note, accepted_by=by, accepted_at=now_iso())
        self.events.emit("retro_verdict", "", phase=phase.key, verdict=choice, status=status, by=by)
        self.state.save()
        return dict(rec)

    def _publish_retro_question_answer(self, decision: dict[str, Any]) -> None:
        """An answer made before the retro PR merges belongs on that PR branch; after merge
        the live document was updated directly and needs no branch mutation."""
        if Path(str(decision.get("live_retro_path") or "")).exists():
            return
        worktree = Path(str(decision.get("retro_worktree") or ""))
        branch, base = str(decision.get("retro_branch") or ""), str(decision.get("retro_base") or "")
        if not worktree.exists() or not branch or not base:
            return
        try:
            gitops.commit_all(worktree, "garden retro: record owner question answer")
            if gitops.commits_ahead(worktree, base):
                gitops.push(worktree, branch, base=base)
        except gitops.GitError as e:
            self.log(f"retro question {decision.get('id', '')}: could not update its PR branch: {e}")
