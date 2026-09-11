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
_CONTINUATION_RE = re.compile(r"^\s+\S.*$")
_VERIFICATION_HEADING_RE = re.compile(r"(?im)^#{1,6}\s+verification\b.*$")
_PERSONA_REQUIREMENT_RE = re.compile(r"\bpersona-review\b.*?\s-p\s+([a-z0-9][a-z0-9-]*)\b", re.I)
_CHECK_REQUIREMENT_RE = re.compile(r"\bcheck\s*:\s*`?([a-z0-9][a-z0-9_-]*)`?", re.I)
_LEGACY_CAPTURE_REQUIREMENT_RE = re.compile(
    r"(?:\brequires?\s*:?\s*(?:ui\s+)?captures?\b|"
    r"\b(?:ui\s+)?captures?\s+(?:are\s+)?required\b|"
    r"^\s*(?:provide|attach|take|record|produce|create|generate)\b[^.]{0,80}\bcaptures?\b)",
    re.I,
)


def required_evidence(body: str, requires: Any = None) -> list[dict[str, str]]:
    """Evidence the task explicitly asks the scheduler to produce.

    The portable frontmatter form is ``requires: ["persona-review -p designer",
    "captures", "check: unit"]``. The same concise persona/check forms and explicit legacy
    capture requests in an acceptance criterion work for author-written tasks. A named check
    refers to a configured pre-PR check; task text never supplies a shell command.
    """
    values = requires if isinstance(requires, list) else []
    out: list[dict[str, str]] = []

    def add(kind: str, name: str = "") -> None:
        item = {"kind": kind, "name": name}
        if item not in out:
            out.append(item)

    def parse(value: Any, *, structured: bool) -> None:
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
        if ((structured and re.search(r"\bcaptures?\b", value, re.I))
                or (not structured and _LEGACY_CAPTURE_REQUIREMENT_RE.search(value))):
            add("capture")
        for name in _CHECK_REQUIREMENT_RE.findall(value):
            add("check", name)

    for criterion in parse_criteria(body):
        parse(criterion, structured=False)
    for value in values:
        parse(value, structured=True)
    return out


def browser_capture_authorized(body: str, requires: Any = None) -> bool:
    """Return whether a task deliberately opts in to browser capture work.

    Structured ``requires`` values are unambiguous. For compatibility with older tasks,
    acceptance criteria count only when they explicitly require captures or use a direct
    capture-producing imperative. Merely discussing captures (including saying not to run
    them) is not execution authority.
    """
    return any(item["kind"] == "capture" for item in required_evidence(body, requires))


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


def _criterion_ranges(lines: list[str]) -> list[tuple[int, int]]:
    """The (start, end) inclusive line numbers of each checklist item under an 'Acceptance
    criteria' heading, `end` extended over any indented continuation lines that wrap the
    item's text. A blank line, a new checklist item, or a heading ends the item."""
    ranges: list[tuple[int, int]] = []
    in_section = False
    start: int | None = None
    for n, line in enumerate(lines):
        heading = _HEADING_RE.match(line)
        if heading:
            if start is not None:
                ranges.append((start, n - 1))
            in_section = heading.group(1).strip().lower().startswith("acceptance criteria")
            start = None
            continue
        if not in_section:
            continue
        if _CHECK_RE.match(line):
            if start is not None:
                ranges.append((start, n - 1))
            start = n
            continue
        if start is not None and _CONTINUATION_RE.match(line):
            continue
        if start is not None:
            ranges.append((start, n - 1))
            start = None
    if start is not None:
        ranges.append((start, len(lines) - 1))
    return ranges


def parse_criteria(body: str) -> list[str]:
    """The acceptance-criteria bullets from a task body: the `- [ ]` checklist items under a
    heading whose text starts with 'Acceptance criteria', normalized to their complete text
    when an item wraps onto indented continuation lines. Empty when the task has none."""
    lines = body.splitlines()
    out = []
    for start, end in _criterion_ranges(lines):
        cm = _CHECK_RE.match(lines[start])
        parts = [cm.group(1).strip()] + [lines[i].strip() for i in range(start + 1, end + 1)]
        out.append(" ".join(parts))
    return out


def amend_criteria(body: str, amendments: Any) -> tuple[str, list[dict[str, Any]]]:
    """Apply valid worker amendments to checklist lines, preserving their order.

    Indexes are zero-based, matching the result's ordered ``verified`` list.  Invalid
    entries are ignored: a worker result must never be able to rewrite arbitrary task
    prose by supplying an out-of-range index. When a criterion wraps onto indented
    continuation lines, the amendment replaces the whole item: the continuation lines are
    removed so no stale text survives alongside the replacement.
    """
    if not isinstance(amendments, list):
        return body, []
    lines = body.splitlines()
    ranges = _criterion_ranges(lines)
    applied: list[dict[str, Any]] = []
    drop: set[int] = set()
    for item in amendments:
        if not isinstance(item, dict) or not isinstance(item.get("index"), int):
            continue
        index = item["index"]
        text, reason = str(item.get("text") or "").strip(), str(item.get("reason") or "").strip()
        if not (0 <= index < len(ranges) and text and reason):
            continue
        start, end = ranges[index]
        prefix = re.match(r"^(\s*[-*]\s+\[[ xX]\]\s+)", lines[start])
        if prefix is None:
            continue
        lines[start] = prefix.group(1) + text
        drop.update(range(start + 1, end + 1))
        applied.append({"index": index, "text": text, "reason": reason})
    lines = [line for n, line in enumerate(lines) if n not in drop]
    return "\n".join(lines) + ("\n" if body.endswith("\n") else ""), applied


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


def unmatched_worker_entries(criteria: list[str], verified: Any) -> list[dict[str, Any]]:
    """Worker `verified` entries that quote a `criterion` not found (by normalised text) among
    the frozen criteria: likely wording drift on the worker's part, not a skipped criterion.
    These entries carry real evidence that `reconcile` could not place against any row; surface
    them so a mismatch gets diagnosed rather than silently discarded."""
    ws = _dicts(verified)
    if not any(_norm(e.get("criterion", "")) for e in ws):
        return []
    known = {_norm(c) for c in criteria}
    return [e for e in ws if _norm(e.get("criterion", "")) and _norm(e.get("criterion", "")) not in known]


def evidence_gaps(criteria: list[str], verified: Any) -> list[str]:
    """Criteria whose reconciled row has neither evidence nor a `not_done` reason.

    Unmatched worker entries do not satisfy a frozen criterion: they are preserved separately by
    `unmatched_worker_entries` and described by `evidence_gap_diagnosis`, but publication remains
    blocked until the worker reconciles them or explicitly explains why evidence is unavailable.
    """
    return [row["criterion"] for row in reconcile(criteria, verified)
            if not row["not_done"] and not row["evidence"]]


def evidence_gap_diagnosis(criteria: list[str], verified: Any, run_id: str = "") -> str:
    """Actionable detail for an evidence-gap failure, retaining unmatched worker statements."""
    gaps = evidence_gaps(criteria, verified)
    if not gaps:
        return ""
    scope = f"Run {run_id}" if run_id else "The worker result"
    lines = [scope + " left these frozen criteria without reconciled evidence or an explicit reason: "
             + "; ".join(gaps) + "."]
    for entry in unmatched_worker_entries(criteria, verified):
        quoted = str(entry.get("criterion") or "").strip()
        detail = str(entry.get("evidence") or entry.get("reason") or "no further detail given").strip()
        lines.append(
            f'It also supplied unmatched evidence for "{quoted}": {detail}. Re-submit it using '
            "the exact frozen criterion text, or mark that criterion not_done with a reason."
        )
    return "\n".join(lines)


def verification_markdown(rows: list[dict[str, Any]], unmatched: list[dict[str, Any]] | None = None) -> str:
    """A `## Verification` section built from reconciled rows, or '' when there is nothing to
    say. One bullet per criterion: ✅ with evidence, 🚧 for a criterion the worker did not do,
    ⚠️ for one with no evidence. `unmatched` (from `unmatched_worker_entries`), when given and
    non-empty, adds a Reconciliation notes section instead of letting the evidence vanish."""
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
    if unmatched:
        lines.append("")
        lines.append("### Reconciliation notes")
        lines.append("")
        for e in unmatched:
            text = str(e.get("criterion") or "").strip()
            detail = str(e.get("evidence") or e.get("reason") or "").strip()
            lines.append(
                f"- Evidence was given for \"{text}\", which does not match any acceptance "
                f"criterion verbatim: {detail or 'no further detail given'}. Check whether this "
                "covers one of the ⚠️ rows above before treating it as missing."
            )
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
    section = verification_markdown(reconcile(criteria, verified), unmatched_worker_entries(criteria, verified))
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
