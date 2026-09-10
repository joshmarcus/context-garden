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

IMPLEMENTATION_FAILURE = "implementation"

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
    """Return the mechanical evidence requirements for a review.

    File paths, keywords, and legacy ``interaction_evidence: required`` prose do not know
    what behavior changed. The reviewing agent chooses an appropriate verification method
    from the task, diff, and existing evidence, so the scheduler never imposes a generic
    served replay or load schema here.
    """
    del changed, review_context
    return False, False, "reviewer chooses proportionate verification for the actual change"


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
    row = review.get("interaction")
    if not isinstance(row, dict):
        return []
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
        warnings.append("interaction was not performed in a disposable garden")
    if affected_flow:
        recorded_flow = str(row.get("affected_flow") or "").strip()
        if recorded_flow and recorded_flow != affected_flow:
            gaps.append(f"interaction evidence contradicts the declared affected flow: {affected_flow}")
        elif not recorded_flow:
            warnings.append("interaction affected flow was not recorded")
    command = row.get("command")
    if not isinstance(command, str) or not command.strip():
        warnings.append("interaction command was not reported")
    elif command.strip() in {"true", ":"}:
        warnings.append("interaction command is only an attestation placeholder")
    states = row.get("states") if isinstance(row.get("states"), dict) else {}
    for state in ("affected", "empty", "failure_recovery"):
        evidence = states.get(state) if isinstance(states.get(state), dict) else {}
        actions = evidence.get("actions")
        observed = evidence.get("observed")
        status = str(evidence.get("status") or "").strip().lower()
        if status and status != "pass":
            gaps.append(f"{state.replace('_', '/')} interaction explicitly failed")
        elif (status != "pass" or not isinstance(actions, list) or not actions
                or any(not isinstance(action, str) or not action.strip() for action in actions)
                or not isinstance(observed, str) or not observed.strip()):
            warnings.append(f"{state.replace('_', '/')} interaction detail was not reported")
    events = row.get("events")
    warnings.extend(_interaction_event_gaps(events))
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
                warnings.append("unverified outcome metadata did not name a criterion or affected flow")
            elif criterion and criterion not in frozen_criteria:
                warnings.append(f"unverified outcome names no frozen criterion: {criterion}")
            elif flow and (not affected_flow or flow != affected_flow):
                warnings.append(f"unverified outcome names no declared affected flow: {flow}")
            else:
                gaps.append(f"required outcome remains unverified ({target}; {outcome}): {reason}")
    if scalability:
        load = row.get("scalability") if isinstance(row.get("scalability"), dict) else {}
        if not isinstance(load.get("served_app"), str) or not load["served_app"].startswith(("http://", "https://")):
            warnings.append("scalability served_app was not recorded as a served HTTP URL")
        sizes = _numbers(load.get("history_sizes"), minimum_items=2)
        if sizes is None or any(size < 0 for size in sizes) or sizes != sorted(set(sizes)):
            warnings.append("scalability history sizes were not reported in the legacy shape")
        intervals = load.get("cache_expiry_intervals")
        if isinstance(intervals, bool) or not isinstance(intervals, int) or intervals < 2:
            warnings.append("scalability cache-expiry intervals were not reported in the legacy shape")
        processes = load.get("executing_processes")
        if isinstance(processes, bool) or not isinstance(processes, int) or processes < 1:
            warnings.append("scalability executing-process count was not reported")
        latencies = _numbers(load.get("latencies"), minimum_items=2)
        if latencies is None or any(latency < 0 for latency in latencies):
            warnings.append("scalability latency samples were not reported in the legacy shape")
        counts = load.get("read_scan_counts")
        if not isinstance(counts, dict) or any(
            isinstance(counts.get(name), bool) or not isinstance(counts.get(name), (int, float))
            or not math.isfinite(counts[name]) or counts[name] < 0 for name in ("reads", "scans")
        ):
            warnings.append("scalability read/scan counts were not reported in the legacy shape")
        if load.get("load_kind") not in SCALABILITY_LOAD_KINDS:
            warnings.append("scalability load kind was not reported in the legacy shape")
    return gaps


def ambiguous_unverified(review: dict[str, Any], *, expected_criteria: list[str] | None = None,
                         affected_flow: str = "") -> list[str]:
    """Legacy hook retained for stored reviews; evidence shape never needs a re-ask."""
    del review, expected_criteria, affected_flow
    return []


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
    if record.get("environment") != "disposable":
        metadata_warnings.append("scheduler-produced replay environment was not recorded as disposable")
    replay_status = str(record.get("status") or "").strip().lower()
    if replay_status and replay_status != "pass":
        return ["scheduler-produced interaction replay explicitly failed"]
    if not replay_status:
        metadata_warnings.append("scheduler-produced interaction replay status was not recorded")
    if affected_flow:
        recorded_flow = str(record.get("affected_flow") or "").strip()
        if recorded_flow and recorded_flow != affected_flow:
            return [f"scheduler replay contradicts the declared affected flow: {affected_flow}"]
        if not recorded_flow:
            metadata_warnings.append("scheduler-produced replay affected flow was not recorded")
    if not all(isinstance(record.get(name), str) and record[name] for name in ("started_at", "finished_at")):
        metadata_warnings.append("scheduler-produced interaction replay timestamps are incomplete")
    flows = record.get("flows")
    if not isinstance(flows, list) or not flows or any(
        not isinstance(flow, dict) or flow.get("ok") is not True
        or not isinstance(flow.get("requests"), list) or not flow["requests"]
        for flow in flows
    ):
        metadata_warnings.append("scheduler-produced interaction replay has no successful request/response flows")
        flows = []
    if any(not isinstance(event.get("at"), (int, float)) or not event.get("url")
           or not isinstance(event.get("status_code"), int)
           for flow in flows for event in flow["requests"] if isinstance(event, dict)) \
            or any(not isinstance(event, dict) for flow in flows for event in flow["requests"]):
        metadata_warnings.append("scheduler-produced interaction replay request/response transcript is incomplete")
    states = record.get("states")
    if not isinstance(states, dict) or any(
        not isinstance(states.get(state), dict) or states[state].get("status") != "pass"
        or not states[state].get("action") or not states[state].get("observed")
        for state in ("affected", "empty", "failure", "recovery")
    ):
        metadata_warnings.append("scheduler-produced replay omits some legacy lifecycle outcomes")
    events = record.get("events")
    event_states = [event.get("state") for event in events if isinstance(event, dict)] if isinstance(events, list) else []
    event_times = [event.get("at") for event in events if isinstance(event, dict)] if isinstance(events, list) else []
    if (event_states != ["affected", "failure", "recovery", "empty"]
            or any(not isinstance(at, (int, float)) for at in event_times)
            or event_times != sorted(event_times)):
        metadata_warnings.append("scheduler-produced replay is not a complete chronological transcript")
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

Use your judgment to choose verification for the actual change. Focused tests, source
inspection, a CLI command, CI, browser interaction, or another small exercise may each be
sufficient. You may attest clearly to what you tested or inspected and what happened. Do
not demand a served app, generic replay, screenshot matrix, empty/failure/recovery matrix,
load measurement, artifact manifest, or checklist because a path or keyword matched.

Check correctness, the task's intended outcomes, scope, and applicable project principles.
Treat actual defects, failed applicable checks, contradictory source/result claims, and
outcomes you judge genuinely unmet as blocking. If an artifact is not attached, assume it
is not included and omit commentary about its absence. Do not add findings, nits, caveats,
or revision feedback for missing optional evidence fields or attachments. Discuss what
you actually inspected or tested. Checklist rows, mapping fields, and PR-description
polish are optional; preserve useful existing evidence without requesting an unchanged
source revision to repackage it.

For a material UI, CLI, or workflow change, choose a direct verification of the named
affected behavior when needed. Say what you inspected in `attestation`, `summary`, criterion
reasons, or any optional evidence fields you find useful. An explicitly unmet criterion
must remain visible and blocking. For every unmet criterion and blocking finding, set
`failure_category` to exactly one of `implementation`, `infrastructure`, `admission`,
`stale_check`, `unavailable_evidence`, or `owner_input`. Use `implementation` only when the
reviewed source owns a defect or unmet required outcome; the other categories identify
conditions that can still block review but must not escalate the author's model. Use
`findings` with severity `blocking` for changes needed before merge and `nit` for optional
improvements. A missing `fix` field does not invalidate an otherwise clear finding.
Description feedback is always advisory and must not be the sole reason for
`request_changes`.

End your final message with exactly one line. Only `verdict` is mechanically required;
the other fields are optional and may be omitted when they add no value:

  {marker} {{"verdict": "approve" | "request_changes", "summary": "<1-2 sentences>", "attestation": "<what you tested or inspected and the result>", "criteria": [{{"criterion": "<criterion>", "met": true | false, "failure_category": "<required when met is false>", "evidence": "<optional evidence>", "reason": "<optional reason>"}}], "findings": [{{"severity": "blocking" | "nit", "failure_category": "<required when blocking>", "file": "<path or empty>", "line": <number or null>, "summary": "<concrete issue>", "fix": "<optional fix>"}}], "description_ok": true | false, "description_feedback": "<optional editorial advice>", "improvements": []}}

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
                 author_interaction: Any = None, clarify_unverified: list[str] | None = None,
                 author_source_run: str = "", author_source_head: str = "") -> str:
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
            "application or renderer errors, failed observed interactions, failed functional checks, "
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
        provenance = (f"Source author run: `{author_source_run}`\n\n"
                      f"Source head: `{author_source_head}`\n\n")
        parts.append("## Author's task-specific interaction evidence\n\n"
                     + provenance
                     + "Reuse this evidence when its source and affected-flow provenance are valid; "
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


def review_implementation_failure_signal(review: dict[str, Any]) -> str:
    """Return the escalation signal explicitly classified by the review producer.

    Blocking severity controls the review outcome, not who owns the failure. Stored legacy
    results without a valid category remain actionable but cannot raise the worker tier.
    """
    for item in review.get("criteria") or []:
        if (isinstance(item, dict) and item.get("met") is False
                and item.get("failure_category") == IMPLEMENTATION_FAILURE):
            return "unmet_acceptance_criteria"
    for item in review.get("findings") or []:
        if (isinstance(item, dict) and item.get("severity") == "blocking"
                and item.get("failure_category") == IMPLEMENTATION_FAILURE):
            return "verification_rejected"
    return ""


def enforce_criteria_verdict(review: dict[str, Any]) -> dict[str, Any]:
    """Honor reviewer judgment while keeping explicit defects and unmet outcomes blocking."""
    unmet = [
        criterion for criterion in review.get("criteria") or []
        if isinstance(criterion, dict)
        and criterion.get("met") is False
    ]
    findings = review.setdefault("findings", [])
    if not isinstance(findings, list):
        findings = review["findings"] = []
    existing = {str(finding.get("summary") or "") for finding in findings if isinstance(finding, dict)}
    for criterion in unmet:
        text = str(criterion.get("criterion") or "unnamed criterion")
        summary = f"Acceptance criterion is explicitly unmet: {text}"
        if summary not in existing:
            findings.append({"severity": "blocking", "file": "", "line": None, "summary": summary,
                             "fix": "Make the intended outcome pass or explain why the task should change.",
                             "failure_category": criterion.get("failure_category")})
    blocking = [finding for finding in findings
                if isinstance(finding, dict) and finding.get("severity") == "blocking"]
    if unmet or blocking:
        review["verdict"] = "request_changes"
    elif review.get("description_ok") is False:
        # Editorial presentation is useful advice, but it cannot send correct source through
        # an unchanged implementation round.
        feedback = str(review.get("description_feedback") or "PR description could be clearer").strip()
        findings.append({"severity": "nit", "file": "", "line": None,
                         "summary": "PR description advisory: " + feedback, "fix": ""})
        review["description_advisory"] = feedback
        review["description_ok"] = True
    return review


def review_to_markdown(rev: dict[str, Any], run_id: str = "") -> str:
    verdict = str(rev.get("verdict", "?"))
    icon = "✅" if verdict == "approve" else "🔁"
    out = [f"{icon} **Automated review: {verdict.replace('_', ' ')}** — {rev.get('summary', '')}".rstrip(" —")]
    attestation = str(rev.get("attestation") or "").strip()
    if attestation:
        out.append("\n**Reviewer verification**\n\n" + attestation)
    criteria = [c for c in (rev.get("criteria") or []) if isinstance(c, dict)]
    if criteria:
        out.append("\n**Acceptance criteria**")
        for c in criteria:
            mark = "✅" if c.get("met") is True else "❌"
            out.append(f"- {mark} {c.get('criterion', '')}" + (f" — {c['reason']}" if c.get("reason") else ""))
    interaction = rev.get("interaction")
    unverified = (interaction.get("unverified") or []) if isinstance(interaction, dict) else []
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
    if actionable and rev.get("verdict") == "request_changes":
        # The verdict is sufficient even when the reviewer used an attestation instead
        # of the optional findings shape. Never reactivate a superseded provenance row.
        detail = str(rev.get("summary") or rev.get("attestation") or "reviewer requested changes").strip()
        if detail and not any(detail in item for item in items):
            items.insert(0, "- **automated review summary**: " + detail)
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
