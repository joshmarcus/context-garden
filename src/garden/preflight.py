"""Shared worker pre-flight rules and token-free mechanical checks."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

PREFLIGHT_ITEMS = (
    "A test or stated reason for every acceptance criterion",
    "Lint is clean",
    "No conflict markers remain",
    "UI changes have 1280px and 390px captures",
    "The PR description states the goal and outcome without process history",
    "Every acceptance criterion is addressed by name",
)

PREFLIGHT_RULES = """\
## Review pre-flight

Before writing your result, walk this rubric and include `pre_flight` in `GARDEN_RESULT`.
It is a list with one entry for each item below, each shaped as
`{{"item": "<item>", "status": "pass" | "not_applicable" | "fail", "evidence": "<short reason>"}}`.

{items}

The garden rejects a result that omits this list or any item. Mechanical failures are sent
back before review; include a stated reason rather than silently skipping an item.
{capture_policy}
"""

CAPTURE_INFRASTRUCTURE_POLICIES = ("require", "advisory")
_TRUSTED_CAPTURE_FAILURES = {
    "browser_unavailable",
    "capture_path_unavailable",
    "capture_result_unavailable",
}

# A regular unified diff prefixes newly-added source lines with ``+``.  Match both
# that form and raw file content, but not a marker removed from the branch.
# A separator alone is also valid Setext Markdown; require a boundary marker.
_CONFLICT = re.compile(r"^(?:\+)?(?:<<<<<<<|>>>>>>>)(?:[ \t]|$)", re.MULTILINE)


def _is_ui_path(path: str) -> bool:
    return (path.startswith(("src/garden/web/", "templates/", "static/")) or "/templates/" in path
            or path.endswith((".css", ".scss")))


def preflight_section(capture_infrastructure_policy: str = "require") -> str:
    policy = _capture_policy(capture_infrastructure_policy)
    if policy == "advisory":
        capture_policy = (
            "\nCapture infrastructure policy for this run: `advisory`. If the trusted UI check "
            "cannot launch its browser, reach its capture path, or return its result, report that "
            "attempt as failed with its diagnostic and preserve any HTML/text artifacts. The "
            "scheduler may continue with focused behavior evidence. Observed UI defects, "
            "application/render errors, incomplete interaction evidence, functional check "
            "failures, and contradictory source or artifacts still fail this rubric. Never mark "
            "a missing screenshot as a pass."
        )
    else:
        capture_policy = ""
    return PREFLIGHT_RULES.format(
        items="\n".join(f"- {item}" for item in PREFLIGHT_ITEMS),
        capture_policy=capture_policy,
    )


def capture_infrastructure_reason(result: dict[str, Any], *, policy: str,
                                  trusted_generated_check: bool) -> str:
    """Return a trusted capture-infrastructure diagnostic eligible for advisory handling.

    The result field alone is not trusted: the scheduler must also prove that it generated the
    built-in UI check.  The built-in wrapper removes any child-supplied classification before it
    writes this metadata, so worker code cannot opt its own application failure out of review.
    """
    if _capture_policy(policy) != "advisory" or not trusted_generated_check:
        return ""
    if result.get("name") != "ui" or result.get("status") not in ("fail", "error"):
        return ""
    infrastructure = result.get("capture_infrastructure")
    if not isinstance(infrastructure, dict):
        return ""
    if infrastructure.get("source") != "garden.walkthrough:ui_check":
        return ""
    if infrastructure.get("kind") not in _TRUSTED_CAPTURE_FAILURES:
        return ""
    return str(infrastructure.get("diagnostic") or result.get("summary") or "capture infrastructure unavailable").strip()


def _capture_policy(value: Any) -> str:
    """Normalize internal/default callers while failing closed on unknown values."""
    return "advisory" if value == "advisory" else "require"


def missing_preflight(value: Any) -> list[str]:
    """Return required rubric items missing from a worker result."""
    if not isinstance(value, list):
        return list(PREFLIGHT_ITEMS)
    reported = {str(row.get("item") or "").strip() for row in value if isinstance(row, dict)
                and str(row.get("status") or "").strip()}
    return [item for item in PREFLIGHT_ITEMS if item not in reported]


def mechanical_results(worktree: Path, base: str, pr_body: str, *, require_description: bool,
                       ui_changed: bool, captures: list[str], inspection_error: str = "",
                       required_ui: bool | None = None,
                       capture_infrastructure_advisory: str = "") -> list[dict[str, Any]]:
    """Checks that never need a reviewer or model, one concise failure each."""
    if inspection_error:
        return [_fail("mechanical pre-flight", f"could not inspect candidate diff: {inspection_error}")]
    try:
        from . import gitops

        ref = gitops.base_ref(worktree, base)
        diff = gitops.git("diff", f"{ref}...HEAD", cwd=worktree)
        names = [name.strip() for name in gitops.git("diff", "--name-only", f"{ref}...HEAD", cwd=worktree).splitlines()
                 if name.strip()]
    except gitops.GitError as exc:
        return [_fail("mechanical pre-flight", f"could not inspect candidate diff: {exc}")]

    results: list[dict[str, Any]] = []
    if _CONFLICT.search(diff):
        results.append(_fail("conflict markers", "diff contains unresolved conflict markers"))
    else:
        results.append(_pass("conflict markers"))
    syntax_error = ""
    for name in names:
        if not name.endswith(".py"):
            continue
        path = worktree / name
        # A deleted Python module is still listed by `git diff --name-only`, but
        # it cannot be a syntax error in the candidate branch.
        if not path.is_file():
            continue
        try:
            compile(path.read_text(), str(path), "exec")
        except (OSError, UnicodeDecodeError, SyntaxError) as exc:
            syntax_error = str(exc).splitlines()[-1]
            break
    results.append(_fail("syntax", f"Python syntax error: {syntax_error}") if syntax_error else _pass("syntax"))
    ui_changed = (ui_changed or any(_is_ui_path(name) for name in names)) if required_ui is None else required_ui
    pngs = [p for p in captures if p.endswith(".png")]
    if ui_changed and not pngs:
        if capture_infrastructure_advisory:
            results.append(_advisory(
                "UI captures",
                "planned visual behavior has no PNG captures because capture infrastructure was unavailable",
                capture_infrastructure_advisory,
            ))
        else:
            results.append(_fail("UI captures", "planned visual behavior has no PNG captures"))
    else:
        results.append(_pass("UI captures"))
    if require_description and not pr_body.strip():
        results.append(_fail("PR description", "worker result has an empty pr_body"))
    else:
        results.append(_pass("PR description"))
    return results


def _pass(name: str) -> dict[str, Any]:
    return {"name": name, "status": "pass", "summary": "ok", "details": ""}


def _fail(name: str, summary: str) -> dict[str, Any]:
    return {"name": name, "status": "fail", "summary": summary, "details": ""}


def _advisory(name: str, summary: str, details: str) -> dict[str, Any]:
    return {"name": name, "status": "advisory", "summary": summary, "details": details}
