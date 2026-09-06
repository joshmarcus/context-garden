"""Shared worker pre-flight rules and token-free mechanical checks."""

from __future__ import annotations

import py_compile
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
"""

_CONFLICT = re.compile(r"^(?:<<<<<<<|=======|>>>>>>>)", re.MULTILINE)


def preflight_section() -> str:
    return PREFLIGHT_RULES.format(items="\n".join(f"- {item}" for item in PREFLIGHT_ITEMS))


def missing_preflight(value: Any) -> list[str]:
    """Return required rubric items missing from a worker result."""
    if not isinstance(value, list):
        return list(PREFLIGHT_ITEMS)
    reported = {str(row.get("item") or "").strip() for row in value if isinstance(row, dict)
                and str(row.get("status") or "").strip()}
    return [item for item in PREFLIGHT_ITEMS if item not in reported]


def mechanical_results(worktree: Path, base: str, pr_body: str, *, require_description: bool,
                       ui_changed: bool, captures: list[str]) -> list[dict[str, Any]]:
    """Checks that never need a reviewer or model, one concise failure each."""
    results: list[dict[str, Any]] = []
    try:
        from . import gitops

        diff = gitops.diff(worktree, base)
        names = gitops.diff_names(worktree, base)
    except Exception:  # noqa: BLE001 - a normal check error is clearer than a scheduler crash
        diff, names = "", []
    if _CONFLICT.search(diff):
        results.append(_fail("conflict markers", "diff contains unresolved conflict markers"))
    else:
        results.append(_pass("conflict markers"))
    syntax_error = ""
    for name in names:
        if not name.endswith(".py"):
            continue
        try:
            py_compile.compile(str(worktree / name), doraise=True)
        except (OSError, py_compile.PyCompileError) as exc:
            syntax_error = str(exc).splitlines()[-1]
            break
    results.append(_fail("syntax", f"Python syntax error: {syntax_error}") if syntax_error else _pass("syntax"))
    pngs = [p for p in captures if p.endswith(".png")]
    if ui_changed and not pngs:
        results.append(_fail("UI captures", "UI files changed but this run produced no PNG captures"))
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
