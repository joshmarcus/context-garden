"""Acceptance criteria: parse them from a task body, and line up a worker's `verified`
evidence and a reviewer's `criteria` verdict against them.

A task's acceptance criteria are the `- [ ]` checklist under its **Acceptance criteria**
heading. A worker reports one `verified` entry per criterion (evidence, or `not_done` with a
reason); the reviewer reports one `criteria` entry per criterion (`met` and a one-line reason).
`reconcile` aligns the three lists into one row per criterion so the task page, the PR body's
Verification section and `garden metrics` all speak to the same list. No network here."""

from __future__ import annotations

import re
from typing import Any

_HEADING_RE = re.compile(r"^#{1,6}\s+(.*?)\s*#*$")
_CHECK_RE = re.compile(r"^\s*[-*]\s+\[[ xX]\]\s+(.*\S)\s*$")
_VERIFICATION_HEADING_RE = re.compile(r"(?im)^#{1,6}\s+verification\b.*$")
_PERSONA_REQUIREMENT_RE = re.compile(r"\bpersona-review\b.*?\s-p\s+([a-z0-9][a-z0-9-]*)\b", re.I)
_CHECK_REQUIREMENT_RE = re.compile(r"\bcheck\s*:\s*`?([a-z0-9][a-z0-9_-]*)`?", re.I)


def required_evidence(body: str, requires: Any = None) -> list[dict[str, str]]:
    """Evidence the task explicitly asks the scheduler to produce.

    The portable frontmatter form is ``requires: ["persona-review -p designer",
    "captures", "check: unit"]``.  The same concise forms in an acceptance criterion work
    for author-written tasks.  A named check refers to a configured pre-PR check; task text
    never supplies a shell command.
    """
    text = "\n".join(parse_criteria(body))
    values = requires if isinstance(requires, list) else []
    out: list[dict[str, str]] = []

    def add(kind: str, name: str = "") -> None:
        item = {"kind": kind, "name": name}
        if item not in out:
            out.append(item)

    def parse(value: Any) -> None:
        if isinstance(value, dict):
            if value.get("persona"):
                add("persona", str(value["persona"]))
            if value.get("check"):
                add("check", str(value["check"]))
            if value.get("captures") or value.get("capture"):
                add("capture")
            return
        value = str(value)
        for name in _PERSONA_REQUIREMENT_RE.findall(value):
            add("persona", name)
        if re.search(r"\bcaptures?\b", value, re.I):
            add("capture")
        for name in _CHECK_REQUIREMENT_RE.findall(value):
            add("check", name)

    parse(text)
    for value in values:
        parse(value)
    return out


def required_evidence_rows(requirements: list[dict[str, str]], state: Any) -> list[dict[str, str]]:
    """Display-ready required evidence rows, retaining queued/running/posted state."""
    stored = (state or {}).get("required_evidence") if isinstance(state, dict) else {}
    stored = stored if isinstance(stored, dict) else {}
    rows = []
    for item in requirements:
        key = f"{item['kind']}:{item['name']}"
        label = (f"persona review · {item['name']}" if item["kind"] == "persona" else
                 "UI captures" if item["kind"] == "capture" else f"check · {item['name']}")
        rows.append({**item, "label": label, "state": str(stored.get(key, "queued"))})
    return rows


def parse_criteria(body: str) -> list[str]:
    """The acceptance-criteria bullets from a task body: the `- [ ]` checklist items under a
    heading whose text starts with 'Acceptance criteria'. Empty when the task has none."""
    out: list[str] = []
    in_section = False
    for line in body.splitlines():
        m = _HEADING_RE.match(line)
        if m:
            in_section = m.group(1).strip().lower().startswith("acceptance criteria")
            continue
        if in_section:
            cm = _CHECK_RE.match(line)
            if cm:
                out.append(cm.group(1).strip())
    return out


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", " ", str(s).lower())).strip()


def _match(entries: list[dict[str, Any]], criterion: str, i: int) -> dict[str, Any]:
    """The worker/reviewer entry for a criterion. When the entries quote a `criterion`, match by
    normalised text and nothing else — a criterion with no matching entry was skipped, and must
    not borrow the i-th entry once a skip has shifted the positions. Only when no entry quotes a
    criterion (a purely positional list) fall back to the i-th entry. Empty dict on no match."""
    if any(_norm(e.get("criterion", "")) for e in entries):
        n = _norm(criterion)
        return next((e for e in entries if _norm(e.get("criterion", "")) == n), {}) if n else {}
    if 0 <= i < len(entries):
        return entries[i]
    return {}


def _dicts(value: Any) -> list[dict[str, Any]]:
    return [e for e in value if isinstance(e, dict)] if isinstance(value, list) else []


def reconcile(criteria: list[str], verified: Any = None, review_criteria: Any = None) -> list[dict[str, Any]]:
    """One row per acceptance criterion, carrying the worker's evidence and the reviewer's
    verdict. `evidence`/`not_done`/`worker_reason` come from the worker's `verified`; `met`
    (True/False/None) and `review_reason` from the reviewer's `criteria`."""
    ws = _dicts(verified)
    rs = _dicts(review_criteria)
    rows: list[dict[str, Any]] = []
    for i, crit in enumerate(criteria):
        w = _match(ws, crit, i)
        r = _match(rs, crit, i)
        met = r.get("met")
        rows.append({
            "criterion": crit,
            "evidence": str(w.get("evidence") or "").strip(),
            "not_done": bool(w.get("not_done")),
            "worker_reason": str(w.get("reason") or "").strip(),
            "has_worker": bool(w),
            "met": bool(met) if isinstance(met, bool) else None,
            "review_reason": str(r.get("reason") or "").strip(),
            "has_review": bool(r),
        })
    return rows


def verification_markdown(rows: list[dict[str, Any]]) -> str:
    """A `## Verification` section built from reconciled rows, or '' when there is nothing to
    say. One bullet per criterion: ✅ with evidence, 🚧 for a criterion the worker did not do,
    ⚠️ for one with no evidence."""
    if not rows:
        return ""
    lines = ["## Verification", ""]
    for row in rows:
        if row["not_done"]:
            lines.append(f"- 🚧 **{row['criterion']}** — not done: {row['worker_reason'] or 'no reason given'}")
        elif row["evidence"]:
            lines.append(f"- ✅ **{row['criterion']}** — {row['evidence']}")
        else:
            lines.append(f"- ⚠️ **{row['criterion']}** — no evidence given")
    return "\n".join(lines) + "\n"


def _strip_verification(body: str) -> str:
    """Remove a `## Verification` section (heading to the next heading or end of body)."""
    m = _VERIFICATION_HEADING_RE.search(body)
    if not m:
        return body
    tail = body[m.end():]
    nxt = re.search(r"(?m)^#{1,6}\s+\S", tail)
    rest = tail[nxt.start():] if nxt else ""
    return (body[:m.start()].rstrip() + ("\n\n" + rest if rest else "\n")).rstrip() + "\n"


def apply_verification(body: str, criteria: list[str], verified: Any) -> str:
    """Return `body` with a garden-generated `## Verification` section built from the worker's
    `verified` list: any Verification section the worker wrote is replaced. When the worker
    reported no `verified` entries the body is returned untouched, so tasks without acceptance
    criteria (and older results) are unaffected."""
    if not _dicts(verified):
        return body
    section = verification_markdown(reconcile(criteria, verified))
    if not section:
        return body
    stripped = _strip_verification(body).rstrip()
    return (stripped + "\n\n" + section) if stripped else section


def worker_verified(runs: list[Any]) -> list[dict[str, Any]]:
    """The `verified` list from the most recent worker round (work/revise/resume) that reported
    one, for the task page. `runs` are Run records (oldest first); duck-typed on .mode/.result."""
    for run in reversed(runs):
        if getattr(run, "mode", "") in ("work", "revise", "resume"):
            v = _dicts((getattr(run, "result", None) or {}).get("verified"))
            if v:
                return v
    return []


def criteria_counts(review_criteria: Any) -> tuple[int, int]:
    """(met, total) from a reviewer's `criteria` list: total entries and how many are met."""
    rs = _dicts(review_criteria)
    return sum(1 for e in rs if e.get("met") is True), len(rs)
