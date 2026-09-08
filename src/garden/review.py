"""Automated review pass: one headless run per PR round that checks the diff against the
task's acceptance criteria and the PR description against the garden's standards, then
reports JSON. The scheduler posts the result on the PR and, on `request_changes`, routes
the findings into the normal revise loop."""

from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any

from .brief import EVIDENCE_GUIDANCE, _parse_marked_json, build_brief
from .criteria import parse_criteria, reconcile
from .model import Task
from .preflight import preflight_section
from .store import Store

REVIEW_MARKER = "GARDEN_REVIEW:"

INTERACTION_PATHS = (
    "src/garden/browser.py", "src/garden/canary.py", "src/garden/checkrun.py",
    "src/garden/checks.py", "src/garden/gitops.py",
    "src/garden/github.py", "src/garden/harness.py", "src/garden/inbox.py",
    "src/garden/kickoff.py", "src/garden/notify.py", "src/garden/now1.py", "src/garden/onboard.py",
    "src/garden/model.py", "src/garden/outcomes.py", "src/garden/profiles.py", "src/garden/qa/",
    "src/garden/review.py", "src/garden/run_supervisor.py", "src/garden/runner/",
    "src/garden/runs.py", "src/garden/scheduler/__init__.py", "src/garden/scheduler/aux.py",
    "src/garden/scheduler/browser.py", "src/garden/scheduler/budget.py",
    "src/garden/scheduler/checkruns.py", "src/garden/scheduler/discovered.py",
    "src/garden/scheduler/dispatch.py", "src/garden/scheduler/edits.py",
    "src/garden/scheduler/fence.py", "src/garden/scheduler/human.py",
    "src/garden/scheduler/kickoff.py", "src/garden/scheduler/persona.py",
    "src/garden/scheduler/poll.py", "src/garden/scheduler/queue.py",
    "src/garden/scheduler/quota.py", "src/garden/scheduler/reap.py",
    "src/garden/scheduler/rebase.py", "src/garden/scheduler/resources.py",
    "src/garden/scheduler/retro.py", "src/garden/scheduler/review.py",
    "src/garden/scheduler/selection.py", "src/garden/scheduler/snapshot.py",
    "src/garden/scheduler/state.py", "src/garden/scheduler/trials.py",
    "src/garden/scheduler/upgrades.py", "src/garden/stabilization.py",
    "src/garden/tui/", "src/garden/upgrade.py", "src/garden/walkthrough.py",
    "src/garden/web/",
)

SCALABILITY_LOAD_KINDS = {"controlled", "real_model_harnesses"}

# The walkthrough deliberately has a larger inventory than a normal PR needs.  Keep this
# mapping here, beside the review policy, so the check runner and reviewer consume one plan.
_PAGE_MODULES = {
    "board": ("board", "board-list"), "config": ("config",), "costs": ("costs",),
    "events": ("events",), "inbox": ("inbox",), "now1": ("now",),
    "phase": ("phase",), "runs": ("runs", "run"),
    "task": ("task",), "trellis": ("trellis",), "trials": ("trials",),
}
_SHARED_UI_PATHS = ("src/garden/web/app.py", "src/garden/web/common.py",
                    "src/garden/web/templates/base.html", "templates/base.html")
_SHARED_UI_PREFIXES = ("src/garden/web/static/",)
_CAPTURE_ARTIFACT_PREFIXES = ("docs/design/captures/", "docs/design/snapshots/")
_CAPTURE_ARTIFACT_NAMES = {"docs/design/snapshot.json"}
# A path says where a change lives, not whether a person can see it.  The task and PR
# describe the intended behaviour; use that declaration to distinguish a route or
# authentication change in a web module from a rendered change in the same module.
_REPRESENTATIVE_SHARED_PAGES = ("board", "inbox")


def _is_capture_artifact(path: str) -> bool:
    """Whether a generated visual-evidence output must not request more evidence."""
    return path in _CAPTURE_ARTIFACT_NAMES or path.startswith(_CAPTURE_ARTIFACT_PREFIXES)


def _visual_scope(scope: Any) -> tuple[str, list[str]]:
    """Read the task's explicit visual-scope declaration, if it has one.

    Free text can describe a visual change or explicitly deny one.  It is therefore not a
    reliable policy input.  The declaration is intentionally small: a named behaviour and,
    optionally, the exact affected page slugs.
    """
    if not isinstance(scope, dict) or not isinstance(scope.get("behavior"), str):
        return "", []
    behavior = scope["behavior"].strip()
    pages = scope.get("pages", [])
    if not behavior or not isinstance(pages, list) or not all(isinstance(page, str) for page in pages):
        return "", []
    return behavior[:240], sorted(set(page for page in pages if page))


def visual_source_digest(worktree: Path, plan: dict[str, Any]) -> str:
    """Fingerprint just the source responsible for planned captures on a reviewed head."""
    digest = hashlib.sha256()
    for name in sorted(str(path) for path in plan.get("visual_paths", [])):
        path = worktree / name
        digest.update(name.encode())
        digest.update(b"\0")
        digest.update(path.read_bytes() if path.is_file() else b"<missing>")
        digest.update(b"\0")
    return digest.hexdigest()


def _numbers(value: Any, *, minimum_items: int) -> list[int | float] | None:
    if not isinstance(value, list) or len(value) < minimum_items:
        return None
    if any(isinstance(item, bool) or not isinstance(item, (int, float)) or not math.isfinite(item)
           for item in value):
        return None
    return value


def interaction_requirement(changed: list[str], *review_context: str) -> tuple[bool, bool, str]:
    """Classify reviews that need a running-app journey, and performance claims that need load evidence."""
    affected = [path for path in changed if path.startswith(INTERACTION_PATHS)]
    context = "\n".join(review_context)
    explicitly_required = bool(re.search(r"\binteraction[-_ ]evidence\s*:\s*required\b", context, re.I))
    required = bool(affected) or explicitly_required
    scalability = bool(re.search(
        r"\b(scalab(?:ility|le)|performance|latency|p95|cache.expir|history (?:size|scan)|read/scan)\b",
        context, re.I,
    ))
    if affected:
        reason = "affected UI/lifecycle paths: " + ", ".join(affected[:6])
    elif explicitly_required:
        reason = "change metadata requires interaction evidence"
    else:
        reason = "non-UI change"
    return required, scalability, reason


def validation_plan(changed: list[str], *review_context: str, head: str = "",
                    check_specs: list[dict[str, Any]] | None = None,
                    visual_scope: Any = None,
                    capture_infrastructure_policy: str = "require") -> dict[str, Any]:
    """Return the head-bound functional and visual evidence decision for a change.

    A screenshot is evidence for a named visible behaviour, never a side effect of touching
    a web module. Unknown UI code remains a bounded inspection request, so it cannot silently
    evade functional validation or turn into an all-pages capture demand.
    """
    interaction, scalability, interaction_reason = interaction_requirement(changed, *review_context)
    pages: set[str] = set()
    reasons: list[dict[str, str]] = []
    unknown: list[str] = []
    visual_paths: set[str] = set()
    shared = False
    behavior, declared_pages = _visual_scope(visual_scope)
    for path in changed:
        if _is_capture_artifact(path):
            continue
        # app.py and common.py contain route/auth and shared helpers as well as rendering
        # plumbing. They need normal functional evidence unless the stated behavior is visual.
        if path in _SHARED_UI_PATHS[:2]:
            if behavior:
                shared = True
            continue
        if (path in _SHARED_UI_PATHS[2:] or path.startswith(_SHARED_UI_PREFIXES)
                or path.endswith((".css", ".scss"))):
            shared = True
            continue
        match = re.search(r"(?:pages/|templates/)([a-z0-9_]+)\.(?:py|html)$", path)
        if behavior and match and match.group(1) in _PAGE_MODULES:
            names = tuple(declared_pages) or _PAGE_MODULES[match.group(1)]
            pages.update(names)
            visual_paths.add(path)
            reasons.append({"item": ", ".join(names),
                            "reason": f"visible behavior: {behavior}; changed page implementation: {path}"})
        elif path.startswith("src/garden/web/") or "/templates/" in path:
            unknown.append(path)
    if shared and behavior:
        pages.update(declared_pages or _REPRESENTATIVE_SHARED_PAGES)
        visual_paths.update(path for path in changed if path in _SHARED_UI_PATHS or path.startswith(_SHARED_UI_PREFIXES)
                            or path.endswith((".css", ".scss")))
        selected_pages = declared_pages or list(_REPRESENTATIVE_SHARED_PAGES)
        selection_reason = "declared affected pages" if declared_pages else "representative consumers cover distinct board and inbox layouts"
        reasons.append({"item": ", ".join(selected_pages),
                        "reason": f"visible shared behavior: {behavior}; {selection_reason}"})
    elif shared:
        reasons.append({"item": "no screenshot scope",
                        "reason": "shared UI path changed without a declared visible behavior; retain functional evidence"})
    if unknown:
        reasons.append({"item": "bounded UI inspection", "reason": "map affected consumers for: " + ", ".join(unknown)})
    if interaction:
        reasons.append({"item": "served interaction", "reason": interaction_reason})
    if scalability:
        reasons.append({"item": "served load evidence", "reason": "acceptance claim includes scalability or performance"})
    if interaction:
        check_reason = "changed behavior requires the configured pre-PR checks"
    elif any(path.startswith("docs/") or path.endswith((".md", ".rst")) for path in changed):
        check_reason = "documentation changed without rendered behavior"
    elif any(path.startswith(("src/garden/criteria.py", "src/garden/brief.py")) for path in changed):
        check_reason = "parser or brief behavior changed without rendered behavior"
    else:
        check_reason = "changed code requires focused regression coverage"
    checks = ([{"item": str(spec.get("name") or "unnamed configured check"), "reason": check_reason}
               for spec in check_specs] if check_specs is not None
              else [{"item": "configured pre-PR checks", "reason": check_reason}])
    if not reasons:
        reasons.append({"item": "no rendered evidence", "reason": "no rendered or lifecycle behavior changed"})
    return {"head": head, "pages": sorted(pages), "interaction": interaction,
            "scalability": scalability, "unknown_ui": unknown, "checks": checks, "reasons": reasons,
            "visual_paths": sorted(visual_paths),
            "capture_infrastructure_policy": (
                "advisory" if capture_infrastructure_policy == "advisory" else "require"
            )}


def interaction_evidence_gaps(review: dict[str, Any], *, required: bool, scalability: bool,
                              expected_head: str, replay_manifest: Path | None = None,
                              replay_nonce: str = "", replay_digest: str = "",
                              affected_flow: str = "",
                              expected_criteria: list[str] | None = None,
                              metadata_warnings: list[str] | None = None) -> list[str]:
    """Return substantive blockers; report missing packaging separately as advisories."""
    if not required and not scalability:
        return []
    row = review.get("interaction")
    if not isinstance(row, dict):
        return ["running-application interaction evidence was not reported"]
    gaps: list[str] = []
    warnings = metadata_warnings if metadata_warnings is not None else []
    if required and (replay_manifest is not None or replay_nonce):
        gaps.extend(_replay_manifest_gaps(
            replay_manifest, expected_head, replay_nonce, replay_digest, warnings,
            affected_flow=affected_flow))
    if not row.get("head"):
        warnings.append("interaction source head was not recorded; attach the independently reviewed commit")
    elif row.get("head") != expected_head:
        gaps.append("interaction evidence is stale or not tied to the reviewed head")
    if not row.get("environment"):
        warnings.append("interaction environment was not recorded")
    elif row.get("environment") != "disposable":
        gaps.append("interaction was not performed in a disposable garden")
    if affected_flow and row.get("affected_flow") != affected_flow:
        gaps.append(f"interaction evidence does not cover the declared affected flow: {affected_flow}")
    command = row.get("command")
    if not isinstance(command, str) or not command.strip():
        warnings.append("interaction command was not reported")
    elif command.strip() in {"true", ":"}:
        gaps.append("interaction command does not perform a served application")
    states = row.get("states") if isinstance(row.get("states"), dict) else {}
    for state in ("affected", "empty", "failure_recovery"):
        evidence = states.get(state) if isinstance(states.get(state), dict) else {}
        actions = evidence.get("actions")
        observed = evidence.get("observed")
        if (evidence.get("status") != "pass"
                or not isinstance(actions, list) or not actions
                or any(not isinstance(action, str) or not action.strip() for action in actions)
                or not isinstance(observed, str) or not observed.strip()):
            gaps.append(f"{state.replace('_', '/')} interaction is missing or failed")
    events = row.get("events")
    gaps.extend(_interaction_event_gaps(events))
    artifacts = row.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts or any(not isinstance(path, str) for path in artifacts):
        warnings.append("interaction artifact paths were not reported")
    else:
        for artifact in artifacts:
            path = Path(artifact)
            if not path.exists():
                warnings.append(f"interaction artifact is unavailable: {artifact}")
                continue
            if path.suffix.lower() != ".json":
                continue
            try:
                record = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError):
                warnings.append(f"interaction artifact metadata is unreadable: {artifact}")
                continue
            # Evidence may use another schema or the reviewer's own paraphrase. A
            # contradictory source claim is material; duplicated prose is not required.
            if isinstance(record, dict) and record.get("head") and record["head"] != expected_head:
                gaps.append(f"interaction artifact source contradicts the reviewed head: {artifact}")
    if not isinstance(row.get("automated_checks"), list):
        warnings.append("automated checks were not separately recorded")
    if not isinstance(row.get("unverified"), list):
        warnings.append("unverified requirements were not explicitly recorded")
    else:
        frozen_criteria = ({str(criterion).strip() for criterion in expected_criteria}
                           if expected_criteria is not None else {
                               str(entry.get("criterion") or "").strip()
                               for entry in review.get("criteria") or [] if isinstance(entry, dict)
                           })
        for item in row["unverified"]:
            if not isinstance(item, dict):
                continue  # Legacy strings are resolved by the scheduler's bounded re-ask.
            scope = str(item.get("scope") or "").strip()
            # A limitation is out of scope only when it is just an observation.  Required
            # targeting fields are authoritative even if the reviewer chose the wrong label.
            # Unknown/malformed structured entries also remain conservative blockers.
            has_required_fields = any(item.get(name) for name in
                                      ("criterion", "affected_flow", "outcome", "reason"))
            if scope == "limitation" and not has_required_fields:
                continue
            criterion = str(item.get("criterion") or "").strip()
            flow = str(item.get("affected_flow") or "").strip()
            outcome = str(item.get("outcome") or "").strip()
            reason = str(item.get("reason") or "").strip()
            target = f"criterion: {criterion}" if criterion else f"affected flow: {flow}" if flow else ""
            if not target or not outcome or not reason:
                gaps.append("required unverified outcome must name its criterion or affected flow and explain the missing outcome")
            elif criterion and criterion not in frozen_criteria:
                gaps.append(f"required unverified outcome names no frozen criterion: {criterion}")
            elif flow and (not affected_flow or flow != affected_flow):
                gaps.append(f"required unverified outcome names no justified affected flow: {flow}")
            else:
                gaps.append(f"required outcome remains unverified ({target}; {outcome}): {reason}")
    if scalability:
        load = row.get("scalability") if isinstance(row.get("scalability"), dict) else {}
        if not isinstance(load.get("served_app"), str) or not load["served_app"].startswith(("http://", "https://")):
            gaps.append("scalability served_app must be a served HTTP URL")
        sizes = _numbers(load.get("history_sizes"), minimum_items=2)
        if sizes is None or any(size < 0 for size in sizes) or sizes != sorted(set(sizes)):
            gaps.append("scalability history_sizes must contain at least two distinct increasing numeric sizes")
        intervals = load.get("cache_expiry_intervals")
        if isinstance(intervals, bool) or not isinstance(intervals, int) or intervals < 2:
            gaps.append("scalability cache_expiry_intervals must be an integer of at least two")
        processes = load.get("executing_processes")
        if isinstance(processes, bool) or not isinstance(processes, int) or processes < 1:
            gaps.append("scalability executing_processes must be a positive integer")
        latencies = _numbers(load.get("latencies"), minimum_items=2)
        if latencies is None or any(latency < 0 for latency in latencies):
            gaps.append("scalability latencies must contain at least two non-negative numeric samples")
        counts = load.get("read_scan_counts")
        if not isinstance(counts, dict) or any(
            isinstance(counts.get(name), bool) or not isinstance(counts.get(name), (int, float))
            or not math.isfinite(counts[name]) or counts[name] < 0 for name in ("reads", "scans")
        ):
            gaps.append("scalability read_scan_counts must contain non-negative numeric reads and scans")
        if load.get("load_kind") not in SCALABILITY_LOAD_KINDS:
            gaps.append("scalability load_kind must be controlled or real_model_harnesses")
    return gaps


def ambiguous_unverified(review: dict[str, Any], *, expected_criteria: list[str] | None = None,
                         affected_flow: str = "") -> list[str]:
    """Entries whose target/classification needs reviewer clarification, not author work."""
    interaction = review.get("interaction")
    values = interaction.get("unverified") if isinstance(interaction, dict) else None
    if not isinstance(values, list):
        return []
    frozen_criteria = ({str(criterion).strip() for criterion in expected_criteria}
                       if expected_criteria is not None else {
                           str(entry.get("criterion") or "").strip()
                           for entry in review.get("criteria") or [] if isinstance(entry, dict)
                       })
    ambiguous: list[str] = []
    for value in values:
        if not isinstance(value, dict):
            if str(value).strip():
                ambiguous.append(str(value).strip())
            continue
        scope = str(value.get("scope") or "").strip()
        has_required_fields = any(value.get(name) for name in
                                  ("criterion", "affected_flow", "outcome", "reason"))
        if scope == "limitation" and not has_required_fields:
            continue
        criterion = str(value.get("criterion") or "").strip()
        flow = str(value.get("affected_flow") or "").strip()
        outcome = str(value.get("outcome") or "").strip()
        reason = str(value.get("reason") or "").strip()
        invalid_target = (bool(criterion) == bool(flow)
                          or bool(criterion and criterion not in frozen_criteria)
                          or bool(flow and (not affected_flow or flow != affected_flow)))
        if scope not in {"required", "limitation"} or invalid_target or not outcome or not reason:
            ambiguous.append(json.dumps(value, sort_keys=True))
    return ambiguous


def _replay_manifest_gaps(path: Path | None, expected_head: str, nonce: str, digest: str,
                          metadata_warnings: list[str], *, affected_flow: str = "") -> list[str]:
    """Validate evidence produced by the scheduler, outside the reviewer's process."""
    try:
        raw = path.read_bytes() if path else b""
        record = json.loads(raw) if raw else None
    except (OSError, json.JSONDecodeError):
        record = None
    if not isinstance(record, dict):
        metadata_warnings.append("scheduler-produced interaction replay manifest is missing or unreadable")
        return []  # The review still has to establish the actual required outcomes.
    if digest and hashlib.sha256(raw).hexdigest() != digest:
        return ["scheduler-produced interaction replay manifest changed after execution"]
    if not digest:
        metadata_warnings.append("scheduler-produced interaction replay digest was not recorded")
    identity = {"producer": "garden.scheduler.interaction-replay/v1", "head": expected_head, "nonce": nonce}
    if any(record.get(key) and value and record[key] != value for key, value in identity.items()):
        return ["scheduler-produced interaction replay provenance does not match this review"]
    if any(not record.get(key) or not value for key, value in identity.items()):
        metadata_warnings.append("scheduler-produced interaction replay identity metadata is incomplete")
    if record.get("environment") != "disposable" or record.get("status") != "pass":
        return ["scheduler-produced disposable interaction replay did not pass"]
    if affected_flow and record.get("affected_flow") != affected_flow:
        return [f"scheduler replay does not cover the declared affected flow: {affected_flow}"]
    if not all(isinstance(record.get(name), str) and record[name] for name in ("started_at", "finished_at")):
        metadata_warnings.append("scheduler-produced interaction replay timestamps are incomplete")
    flows = record.get("flows")
    if not isinstance(flows, list) or not flows or any(
        not isinstance(flow, dict) or flow.get("ok") is not True
        or not isinstance(flow.get("requests"), list) or not flow["requests"]
        for flow in flows
    ):
        return ["scheduler-produced interaction replay lacks successful request/response flows"]
    if any(not isinstance(event.get("at"), (int, float)) or not event.get("url")
           or not isinstance(event.get("status_code"), int)
           for flow in flows for event in flow["requests"] if isinstance(event, dict)) \
            or any(not isinstance(event, dict) for flow in flows for event in flow["requests"]):
        return ["scheduler-produced interaction replay request/response transcript is incomplete"]
    states = record.get("states")
    if not isinstance(states, dict) or any(
        not isinstance(states.get(state), dict) or states[state].get("status") != "pass"
        or not states[state].get("action") or not states[state].get("observed")
        for state in ("affected", "empty", "failure", "recovery")
    ):
        return ["scheduler-produced replay does not prove affected, empty, failure, and recovery outcomes"]
    events = record.get("events")
    event_states = [event.get("state") for event in events if isinstance(event, dict)] if isinstance(events, list) else []
    event_times = [event.get("at") for event in events if isinstance(event, dict)] if isinstance(events, list) else []
    if (event_states != ["affected", "failure", "recovery", "empty"]
            or any(not isinstance(at, (int, float)) for at in event_times)
            or event_times != sorted(event_times)):
        return ["scheduler-produced replay outcomes are not a complete chronological transcript"]
    return []


def _interaction_event_gaps(events: Any) -> list[str]:
    """Validate replayable request/browser events rather than screenshot descriptions."""
    if not isinstance(events, list) or not events:
        return ["performed HTTP/browser interaction events were not reported"]
    covered: set[str] = set()
    failure_index: int | None = None
    recovery_index: int | None = None
    for index, event in enumerate(events):
        if not isinstance(event, dict):
            return ["interaction events must be structured request or browser-action records"]
        state = event.get("state")
        kind = event.get("kind")
        observed = event.get("observed")
        if state not in {"affected", "empty", "failure", "recovery"}:
            return ["each interaction event must name an affected, empty, failure, or recovery phase"]
        outcome = event.get("outcome")
        expected_outcome = {"affected": "success", "empty": "empty", "failure": "failure",
                            "recovery": "success"}[state]
        if outcome != expected_outcome:
            return [f"{state} interaction event must record outcome {expected_outcome}"]
        if not isinstance(observed, str) or not observed.strip():
            return ["each interaction event must record its resulting observation"]
        if kind == "http_request":
            method, url, status = event.get("method"), event.get("url"), event.get("status_code")
            if (method not in {"GET", "POST", "PUT", "PATCH", "DELETE"}
                    or not isinstance(url, str) or not url.startswith(("http://", "https://"))
                    or isinstance(status, bool) or not isinstance(status, int) or not 100 <= status <= 599):
                return ["HTTP interaction events require a method, served URL, and response status"]
            if state == "failure" and status < 400:
                return ["failure HTTP event must record an unsuccessful response"]
            if state == "recovery" and status >= 400:
                return ["recovery HTTP event must record a successful response"]
        elif kind == "browser_action":
            action, target = event.get("action"), event.get("target")
            if (not isinstance(action, str) or not action.strip()
                    or not isinstance(target, str) or not target.strip()
                    or re.search(r"\b(screenshot|image|png|jpe?g|gif|webp)\b", action, re.I)):
                return ["browser interaction events require a non-image action and target"]
        else:
            return ["interaction events must be served HTTP requests or browser actions"]
        covered.add(state)
        if state == "failure" and failure_index is None:
            failure_index = index
        elif state == "recovery" and recovery_index is None:
            recovery_index = index
    if {"affected", "empty", "failure", "recovery"} - covered:
        return ["performed interaction events do not cover affected, empty, failure, and recovery phases"]
    if failure_index is None or recovery_index is None or failure_index >= recovery_index:
        return ["a failure event must be followed chronologically by a successful recovery event"]
    return []

REVIEW_RULES = """\
## Your job

You are the automated first reviewer for the pull request described below. The human
reviewer reads your comment before looking at the code, so be precise and terse. You are
in a git worktree of the PR branch (`{branch}`, based on `{base}`); the diff is included
below when it fits, otherwise run `git diff {base}...HEAD`. You may run the project's
checks if they are fast. Do NOT modify tracked worktree files and do NOT commit. Running-app
evidence may write artifacts only into its disposable garden or a temporary directory.

Check, in this order:

1. **Worker pre-flight.** Verify the applicable checks and the actual outcome. Missing
   reporting metadata alone is advisory; a failed check or materially unverified outcome
   is blocking. Inspect available evidence before requiring the author to repeat work.
2. **Acceptance criteria.** Return one `criteria` entry per criterion in the task, in order:
   quote the `criterion`, set `met` true or false, and give a one-line `reason` pointing at
   the evidence (the diff, a test, a page). The author's own per-criterion evidence is under
   "Author's verification" below; check each claim against the diff rather than taking it on
   trust. A criterion with no evidence, or one the author marked not done without a reason you
   accept, is `met: false` and a blocking finding. If the task has no criteria, judge its Goal
   on the author's evidence and return `criteria: []`.
   Every returned criterion needs its own non-empty `evidence`: the scheduler mechanically
   changes the verdict to `request_changes` for an unmet or evidence-less criterion.
3. **Correctness.** Bugs, unhandled cases, broken behaviour, security problems.
4. **Scope.** Changes outside the task, or task work that is missing.
5. **PR description.** It must give a reader without the task file the broader context:
   what is being accomplished and why, how it fits the phase goals, what was verified, and
   any follow-ups. It must have no scar tissue: no references to earlier review rounds or
   abandoned approaches ("as requested", "reverted the previous attempt"), no narration of
   the process, no leftover TODO/debug notes. The diff must be equally clean: no
   commented-out code, no stray debug output, no "fixed review comment" commit messages
   left in the final story of the change. Describe the change as if it were written right
   the first time.
7. **Principles.** Tests skipped or weakened, scope widened, history rewritten, new
   dependencies without justification.

The Validation plan below is the required evidence for this reviewed head, not the available
walkthrough inventory. Inspect each planned page and name it in `pages_seen`; do not demand an
unrelated page merely because a capture exists. A plan names pages only for a declared visible
behavior; shared styling uses representative consumers, expanding only for a distinct visual
risk. For bounded UI inspection, inspect the named paths and either map consumers or
report a `scope_expansions` entry with the changed claim or discovered risk that justifies it.
New evidence demands likewise need that entry; frozen criteria and current valid evidence remain
valid, but evidence for another head never does.

When "Running-application interaction required" is present, start the proposed head as a
served application against a disposable garden and perform the affected journey through its
HTTP/browser surface. Cover the user objective, an empty state, and a relevant failure followed
by recovery. Record actions and their observed consequences; screenshots and test-client
assertions are supporting artifacts, not performed interaction. Treat no_change reconciliation
and attention prompts as user outcomes when they are affected. Never use the live operator
garden. Report the exact command, artifact paths, separately named automated checks, and every
unverified outcome. Each `interaction.unverified` entry is an object. Use `scope: required`
only for a failed or missing frozen acceptance outcome, naming either its exact `criterion`
or a justified `affected_flow`, plus the missing `outcome` and `reason`. Use
`scope: limitation` with an `observation` for honest out-of-scope uncertainty or follow-up;
limitations remain visible but cannot request changes. Never hide an actual failed state,
unmet criterion, affected-flow gap, or contradictory provenance by calling it a limitation.
Use the reviewed full SHA supplied below as `interaction.head`.

For a scalability claim, additionally use a served disposable app with representative and larger
histories, repeated cache-expiry intervals, actual executing bounded workload processes, empirical
latency samples/distribution, and read/scan counts. State whether load is controlled or uses real
model harnesses; controlled load must not be described as a real harness run.

Report `interaction.events` as a chronological sequence with explicit `state` phases: `affected`,
`empty`, `failure`, and `recovery`. Each event includes `outcome`: `success` for affected/recovery,
`empty` for empty, and `failure` for failure. A served HTTP event also contains `kind: http_request`,
`method`, `url`, `status_code`, and `observed`; failure HTTP status is unsuccessful and recovery is
successful. A browser event also contains `kind: browser_action`, `action`, `target`, and `observed`.
Screenshot/image operations are not actions. Report the performed events; an existing artifact
may use a different schema or wording. Do not demand a duplicate of your own paraphrase.

Severity: `blocking` means the PR should not merge as is; `nit` is optional polish. Only
request changes for blocking findings or a description that fails the standard above.

Every finding carries a concrete `fix`: say what to change and where; include a short code
sketch when it makes the change clearer. `fix` is required for `blocking` and `high` findings
and encouraged for nits. Also return `improvements`: non-blocking suggestions beyond the
acceptance criteria, such as a simpler design, clearer name, missing test, doc line, or cheaper
implementation. They are optional work for the author, not reasons to request changes.

If the *only* problem is the description (`description_ok` is false and there is no blocking
finding), do not send the change back for another round: rewrite the description yourself and
return the full corrected body in `description_rewrite`. The garden applies it directly. Write
it to the same contract the author was given — the permanent description of the change, with
no process narration, no review or rebase references, no scar tissue. Leave `description_rewrite`
empty when a blocking finding means the change is going back anyway.

End your final message with exactly one line:

  {marker} {{"verdict": "approve" | "request_changes", "summary": "<1-2 sentences>", "pages_seen": ["<required page slug>"], "ui_scope": [{{"path": "<unknown UI path from plan>", "consumers": ["<affected page slug>"]}}], "scope_expansions": [{{"item": "<new evidence demand or unknown UI path>", "reason": "<changed claim or discovered risk>"}}], "interaction": {{"head": "<reviewed full SHA>", "environment": "disposable", "command": "<served-app command>", "states": {{"affected": {{"status": "pass|fail", "actions": ["<action>"], "observed": "<consequence>"}}, "empty": {{"status": "pass|fail", "actions": ["<action>"], "observed": "<consequence>"}}, "failure_recovery": {{"status": "pass|fail", "actions": ["<action>"], "observed": "<failure and recovery consequence>"}}}}, "events": [{{"kind": "http_request", "state": "affected|empty|failure|recovery", "outcome": "success|empty|failure", "method": "<method>", "url": "<served URL>", "status_code": 200, "observed": "<actual consequence>"}}], "artifacts": ["<path>"], "automated_checks": ["<separate check>"], "unverified": [{{"scope": "required", "criterion": "<exact frozen criterion>", "outcome": "<failed or missing outcome>", "reason": "<why>"}}, {{"scope": "limitation", "observation": "<out-of-scope uncertainty or follow-up>"}}], "scalability": {{"served_app": "<URL>", "history_sizes": [100, 1000], "cache_expiry_intervals": 3, "executing_processes": 2, "latencies": [0.1, 0.2], "read_scan_counts": {{"reads": 3, "scans": 1}}, "load_kind": "controlled|real_model_harnesses"}}}}, "criteria": [{{"criterion": "<acceptance criterion, quoted>", "met": true | false, "evidence": "<diff, test, or performed interaction>", "reason": "<one line, with the evidence>"}}], "description_ok": true | false, "description_feedback": "<what to change in the PR description, or empty>", "description_rewrite": "<the full corrected PR body, or empty>", "findings": [{{"severity": "blocking" | "high" | "nit", "file": "<path or empty>", "line": <number or null>, "summary": "<one sentence>", "fix": "<concrete change, location, and optional code sketch>"}}], "improvements": [{{"area": "<design, naming, tests, docs, cost>", "suggestion": "<non-blocking improvement>", "why": "<benefit>", "effort": "small" | "medium"}}]}}

The JSON must be on one line.
"""


def _verification_brief(task: Task, verified: Any, criteria: list[str] | None = None) -> str:
    """The author's per-criterion evidence, laid out for the reviewer to check the diff
    against. Empty when the task has no criteria and the author claimed nothing."""
    rows = reconcile(criteria if criteria is not None else parse_criteria(task.body), verified)
    if not rows:
        return ""
    lines = ["## Author's verification\n", "One row per acceptance criterion; check each against the diff.\n"]
    for row in rows:
        if row["not_done"]:
            lines.append(f"- **{row['criterion']}** — author says NOT DONE: {row['worker_reason'] or 'no reason given'}")
        elif row["evidence"]:
            lines.append(f"- **{row['criterion']}** — {row['evidence']}")
        else:
            lines.append(f"- **{row['criterion']}** — author gave no evidence")
    return "\n".join(lines) + "\n"


def review_brief(store: Store, task: Task, *, branch: str, base: str, pr_title: str, pr_body: str, diff: str,
                 max_diff_chars: int, pr_comment: str = "", verified: Any = None,
                 captures: list[str] | None = None, checks: list[dict[str, Any]] | None = None,
                 capture_advisories: list[dict[str, Any]] | None = None,
                 reask_missing_fixes: bool = False, interaction_required: bool = False,
                 scalability_required: bool = False, review_head: str = "", interaction_reason: str = "",
                 interaction_manifest: str = "", criteria_snapshot: list[str] | None = None,
                 pre_flight: Any = None, plan: dict[str, Any] | None = None,
                 author_interaction: Any = None, clarify_unverified: list[str] | None = None) -> str:
    frozen = criteria_snapshot if criteria_snapshot is not None else parse_criteria(task.body)
    task_brief = build_brief(store, task, include_rules=False, criteria_snapshot=frozen)
    amendments = {int(a["index"]): a for a in task.extra.get("criteria_amended", [])
                  if isinstance(a, dict) and isinstance(a.get("index"), int)}
    criteria_note = ""
    if amendments:
        lines = ["## Amended acceptance criteria\n",
                 "Judge each amended line against its stated outcome; the original wording is superseded.\n"]
        for index, criterion in enumerate(parse_criteria(task.body)):
            if index in amendments:
                lines.append(f"- **{criterion}** *(amended — {amendments[index].get('reason', '')})*")
        criteria_note = "\n".join(lines) + "\n"
    parts = [
        f"# Review: PR for task {task.id} ({task.title})\n",
        REVIEW_RULES.format(branch=branch, base=base, marker=REVIEW_MARKER),
        EVIDENCE_GUIDANCE,
        preflight_section(str((plan or {}).get("capture_infrastructure_policy") or "require")),
        "## Task brief (what the author was given)\n\n" + task_brief.text,
        f"## PR title\n\n{pr_title}\n\n## PR description\n\n{pr_body.strip() or '(empty)'}\n",
    ]
    if clarify_unverified:
        parts.append(
            "## Clarification required\n\nThe prior review used ambiguous or malformed entries in "
            "`interaction.unverified`. Preserve each observation, classify it under the "
            "structured contract, and target required gaps only to an exact frozen criterion "
            "or the declared affected flow. Do not ask the author to revise unless a required "
            "outcome actually failed. Prior entries:\n\n"
            + "\n".join(f"- {item}" for item in clarify_unverified) + "\n"
        )
    if criteria_note:
        parts.append(criteria_note)
    verification = _verification_brief(task, verified, frozen)
    if verification:
        parts.append(verification)
    if isinstance(pre_flight, list):
        parts.append("## Author's pre-flight\n\n" + "\n".join(
            f"- **{row.get('item', '')}** — {row.get('status', '')}: {row.get('evidence', '')}"
            for row in pre_flight if isinstance(row, dict)
        ) + "\n")
    current = parse_criteria(task.body)
    if current != frozen:
        parts.append("## Criteria changed after dispatch\n\nThe worker was judged against the frozen criteria above. "
                     "The task now has:\n\n" + "\n".join(f"- {item}" for item in current) + "\n")
    if captures:
        parts.append("## Rendered UI captures\n\nOpen these image paths before judging the UI:\n\n" +
                     "\n".join(f"- `{path}`" for path in captures) + "\n")
    if capture_advisories:
        advisory_lines = []
        for advisory in capture_advisories:
            diagnostic = str(advisory.get("diagnostic") or "capture infrastructure unavailable")
            artifacts = [str(path) for path in advisory.get("artifacts", [])]
            advisory_lines.append(f"- Failed capture attempt: {diagnostic}")
            advisory_lines.extend(f"  - focused fallback artifact: `{path}`" for path in artifacts)
        parts.append(
            "## UI capture infrastructure advisory\n\n"
            "The screenshot attempt remains recorded as failed; do not call it a pass. The "
            "owner-selected policy permits review of this head with the focused HTML/text and "
            "functional evidence below. Judge what that evidence proves. Observed UI defects, "
            "application or renderer errors, incomplete interactions, failed functional checks, "
            "and contradictory source or artifacts remain blocking.\n\n"
            + "\n".join(advisory_lines) + "\n"
        )
    if plan:
        parts.append("## Validation plan\n\n```json\n" + json.dumps(plan, indent=2, sort_keys=True) + "\n```\n")
    if interaction_required or scalability_required:
        parts.append("## Running-application interaction required\n\n"
                     f"Reviewed head: `{review_head}`\n\nReason: {interaction_reason}.\n\n"
                     + (f"The scheduler independently replayed the disposable app; inspect its "
                        f"request/response manifest at `{interaction_manifest}`.\n\n" if interaction_manifest else "")
                     + ("This includes the scalability evidence fields described above.\n" if scalability_required else ""))
    if isinstance(author_interaction, dict):
        parts.append("## Author's task-specific interaction evidence\n\n"
                     "Reuse this evidence when its source and affected-flow provenance are valid; "
                     "missing packaging metadata alone is advisory.\n\n```json\n"
                     + json.dumps(author_interaction, indent=2, sort_keys=True) + "\n```\n")
    if checks:
        parts.append("## Pre-review checks\n\n" + "\n".join(
            f"- **{c.get('name', 'check')}**: {c.get('status', 'unknown')}"
            + (f" — {c.get('summary')}" if c.get("summary") else "") for c in checks) + "\n")
    if pr_comment.strip():
        parts.append(
            "## Author's response to the previous review (posted as a PR comment, not part of the description)\n\n"
            + pr_comment.strip() + "\n"
        )
    if reask_missing_fixes:
        parts.append("## Follow-up required\n\nYour previous review had blocking findings without concrete fixes. "
                     "Return the same review with a `fix` for every blocking finding; do not discover new issues "
                     "unless needed to make those fixes accurate.\n")
    if diff and len(diff) <= max_diff_chars:
        fence = "````" if "```" in diff else "```"
        parts.append(f"## Diff ({base}...HEAD)\n\n{fence}diff\n{diff.rstrip()}\n{fence}\n")
    else:
        parts.append(f"## Diff\n\nThe diff is {len(diff):,} characters; read it with `git diff {base}...HEAD` (and `git log {base}..HEAD`).\n")
    return "\n".join(parts)


def parse_review(text: str) -> dict[str, Any]:
    data = _parse_marked_json(text, REVIEW_MARKER)
    if "verdict" in data:
        return data
    return {}


def enforce_criteria_verdict(review: dict[str, Any]) -> dict[str, Any]:
    """Make unsupported or unmet criteria mechanically request changes.

    A reviewer is advisory about its conclusion, but not about this gate: an approving
    top-level verdict cannot override a criterion marked unmet or without evidence.
    """
    unsupported = [
        criterion for criterion in review.get("criteria") or []
        if isinstance(criterion, dict)
        and (criterion.get("met") is not True or not str(criterion.get("evidence") or "").strip())
    ]
    if not unsupported:
        return review

    review["verdict"] = "request_changes"
    findings = review.setdefault("findings", [])
    if not isinstance(findings, list):
        findings = review["findings"] = []
    existing = {str(finding.get("summary") or "") for finding in findings if isinstance(finding, dict)}
    for criterion in unsupported:
        text = str(criterion.get("criterion") or "unnamed criterion")
        summary = f"Acceptance criterion lacks a passing, evidenced assessment: {text}"
        if summary not in existing:
            findings.append({"severity": "blocking", "file": "", "line": None, "summary": summary,
                             "fix": "Make the criterion pass and cite concrete review evidence."})
    return review


def review_to_markdown(rev: dict[str, Any], run_id: str = "") -> str:
    verdict = str(rev.get("verdict", "?"))
    icon = "✅" if verdict == "approve" else "🔁"
    out = [f"{icon} **Automated review: {verdict.replace('_', ' ')}** — {rev.get('summary', '')}".rstrip(" —")]
    criteria = [c for c in (rev.get("criteria") or []) if isinstance(c, dict)]
    if criteria:
        out.append("\n**Acceptance criteria**")
        for c in criteria:
            mark = "✅" if c.get("met") is True else "❌"
            out.append(f"- {mark} {c.get('criterion', '')}" + (f" — {c['reason']}" if c.get("reason") else ""))
    interaction = rev.get("interaction")
    unverified = interaction.get("unverified") if isinstance(interaction, dict) else []
    limitations = [str(item.get("observation") or "").strip() for item in unverified
                   if isinstance(item, dict) and item.get("scope") == "limitation"
                   and str(item.get("observation") or "").strip()]
    if limitations:
        out.append("\n**Limitations and follow-ups**")
        out += [f"- {item}" for item in limitations]
    findings = [f for f in (rev.get("findings") or []) if isinstance(f, dict)]
    blocking = [f for f in findings if f.get("severity") == "blocking"]
    high = [f for f in findings if f.get("severity") == "high"]
    nits = [f for f in findings if f.get("severity") not in ("blocking", "high")]
    if blocking:
        out.append("\n**Blocking**")
        out += [_finding_line(f) for f in blocking]
    if high:
        out.append("\n**High priority**")
        out += [_finding_line(f) for f in high]
    if nits:
        out.append("\n**Nits**")
        out += [_finding_line(f) for f in nits]
    improvements = [i for i in (rev.get("improvements") or []) if isinstance(i, dict)]
    if improvements:
        out.append("\n**Improvements**")
        for item in improvements:
            area = str(item.get("area") or "general")
            effort = str(item.get("effort") or "")
            detail = str(item.get("suggestion") or "")
            why = str(item.get("why") or "")
            suffix = f" — {why}" if why else ""
            effort_text = f" · {effort}" if effort else ""
            out.append(f"- **{area}{effort_text}**: {detail}{suffix}")
    if not rev.get("description_ok", True):
        out.append("\n**PR description**\n\n" + str(rev.get("description_feedback") or "needs work"))
    if run_id:
        out.append(f"\n_garden review run {run_id}_")
    return "\n".join(out)


def review_is_description_only(rev: dict[str, Any]) -> bool:
    """True when a request_changes verdict has no blocking code findings, only a PR
    description fix. CG-109: that revise round is a paragraph rewrite, not a code review,
    and should not cost a code-review-tier model."""
    findings = [f for f in (rev.get("findings") or []) if isinstance(f, dict)]
    return not any(f.get("severity") == "blocking" for f in findings) and not rev.get("description_ok", True)


def review_item_id(kind: str, item: dict[str, Any]) -> str:
    """Stable operator-facing identity for one criterion or finding."""
    if kind == "criterion":
        identity = {"criterion": str(item.get("criterion") or "")}
    elif kind == "finding":
        identity = {name: item.get(name) for name in ("file", "line", "summary")}
    else:
        raise ValueError(f"unknown review item kind: {kind}")
    digest = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:12]
    return f"{kind}:{digest}"


def review_item_ids(rev: dict[str, Any]) -> set[str]:
    """Every selectable item in a review record."""
    out = {review_item_id("criterion", item) for item in rev.get("criteria") or []
           if isinstance(item, dict)}
    out.update(review_item_id("finding", item) for item in rev.get("findings") or []
               if isinstance(item, dict))
    return out


def feedback_from_review(rev: dict[str, Any], *, run_id: str = "", source_head: str = "",
                         actionable: bool = True) -> str:
    """Return the complete review record needed by the next revision author.

    A short scheduler or operator note is useful for triage, but is not a substitute for
    the review that made the author revise.  Keep the finding fixes and failed criterion
    assessments verbatim so a criterion-only rejection remains actionable too.
    """
    items = ["### Applicable automated review\n" if actionable
             else "### Automated review provenance\n"]
    source = "automated review"
    if run_id:
        source += f" run `{run_id}`"
    if source_head:
        source += f" on head `{source_head}`"
    items.append(f"- **Source:** {source}")
    if rev.get("summary"):
        items.append(f"- **Summary:** {str(rev['summary'])}")
    criteria = [criterion for criterion in rev.get("criteria") or []
                if isinstance(criterion, dict) and criterion.get("met") is not True]
    if criteria:
        items.append("\n### Failed acceptance criteria\n" if actionable
                     else "\n### Recorded criterion assessments\n")
        for criterion in criteria:
            text = str(criterion.get("criterion") or "unnamed criterion")
            reason = str(criterion.get("reason") or "reviewer did not provide a reason")
            evidence = str(criterion.get("evidence") or "reviewer did not provide evidence")
            item_id = review_item_id("criterion", criterion)
            items.append(f"- **{text}** (`{item_id}`)\n  - **Reason:** {reason}\n  - **Evidence:** {evidence}")
    if rev.get("findings"):
        items.append("\n### Findings to address\n" if actionable
                     else "\n### Recorded findings\n")
    for f in rev.get("findings") or []:
        if not isinstance(f, dict):
            continue
        severity = str(f.get("severity") or "nit")
        fix = str(f.get("fix") or "").strip()
        item_id = review_item_id("finding", f)
        line = ("- **automated review** " + severity + _where(f) + ": "
                + str(f.get("summary", "")) + f" (`{item_id}`)")
        missing_fix = ("reviewer did not provide one; determine the smallest correct change."
                       if actionable else "reviewer did not provide one.")
        items.append(line + (f"\n  - **Fix:** {fix}" if fix else f"\n  - **Fix:** {missing_fix}"))
    if not rev.get("description_ok", True):
        description = str(rev.get("description_feedback") or (
            "rewrite it to give broader context and remove scar tissue" if actionable
            else "reviewer did not provide details"))
        items.append(("- **automated review** PR description: " + description +
                      " (put the new description in `pr_body`; it replaces the current one)")
                     if actionable else "- **Recorded PR description assessment:** " + description)
    improvements = [i for i in (rev.get("improvements") or []) if isinstance(i, dict)]
    if improvements:
        items.append(("\n### Optional improvements\n\nTake or decline each item. In your result, set `improvements_taken` "
                      "to the suggestions you took and `improvements_declined` to objects with `suggestion` and `reason`; "
                      "declined items are kept for the retro.") if actionable
                     else "\n### Recorded optional improvements\n")
        for item in improvements:
            area = str(item.get("area") or "general")
            effort = str(item.get("effort") or "")
            suggestion = str(item.get("suggestion") or "")
            why = str(item.get("why") or "")
            items.append(f"- **{area}{f' · {effort}' if effort else ''}**: {suggestion}" + (f" — {why}" if why else ""))
    return "\n".join(items)


def feedback_with_operator_note(rev: dict[str, Any], note: str, *, kind: str,
                                run_id: str = "", source_head: str = "",
                                superseded: bool = False,
                                resolved_items: list[str] | None = None) -> str:
    """Add an operator handoff while preserving item-level applicability and provenance."""
    label = "Operator triage note" if kind == "triage" else "Operator recovery note"
    handoff = f"## {label}\n\n{note.strip()}"
    resolved = set(resolved_items or [])
    if superseded:
        handoff += ("\n\nThe automated review record below is **superseded for this revision** "
                    "by this handoff. Retain it for provenance; do not repeat its requests "
                    "unless this note explicitly raises them again.")
        record = feedback_from_review(
            rev, run_id=run_id, source_head=source_head, actionable=False)
        return f"{handoff}\n\n## Superseded automated review record\n\n{record}"
    if not resolved:
        handoff += "\n\nThe automated review record below remains applicable and this note supplements it."
        record = feedback_from_review(rev, run_id=run_id, source_head=source_head)
        return f"{handoff}\n\n## Applicable automated review record\n\n{record}"

    applicable = dict(rev)
    applicable["criteria"] = [item for item in rev.get("criteria") or []
                              if not isinstance(item, dict)
                              or review_item_id("criterion", item) not in resolved]
    applicable["findings"] = [item for item in rev.get("findings") or []
                              if not isinstance(item, dict)
                              or review_item_id("finding", item) not in resolved]
    selected = dict(rev)
    selected["criteria"] = [item for item in rev.get("criteria") or []
                            if isinstance(item, dict)
                            and review_item_id("criterion", item) in resolved]
    selected["findings"] = [item for item in rev.get("findings") or []
                            if isinstance(item, dict)
                            and review_item_id("finding", item) in resolved]
    handoff += ("\n\nOnly the explicitly selected review items are resolved for this revision. "
                "Unmatched items remain applicable.")
    return (f"{handoff}\n\n## Resolved automated review items\n\n"
            f"{feedback_from_review(selected, run_id=run_id, source_head=source_head, actionable=False)}\n\n"
            "## Applicable automated review record\n\n"
            f"{feedback_from_review(applicable, run_id=run_id, source_head=source_head)}")


def _where(f: dict[str, Any]) -> str:
    if f.get("file"):
        return f" (`{f['file']}`" + (f":{f['line']}" if f.get("line") else "") + ")"
    return ""


def _finding_line(f: dict[str, Any]) -> str:
    fix = str(f.get("fix") or "").strip()
    return f"- {f.get('summary', '')}{_where(f)}" + (f"\n  - **Fix:** {fix}" if fix else "")
