"""A phase's page (open or closed), its documents, and the Herbarium of closed phases."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse

from ...charts import burnup_svg, tier_bars_svg
from ...events import EventLog, metrics, phase_summary
from ...graph import effective_status
from ...inbox import split_log
from ...plants import plant_info
from ...runs import RunStore
from ...scheduler import State
from ...trials import TrialLog
from ..common import Site, render_md, tier_rows


def register(app: FastAPI, site: Site) -> None:
    hub, templates, ctx = site.hub, site.templates, site.ctx

    @app.get("/phases/{product}/{phase}", response_class=HTMLResponse)
    def phase_page(request: Request, product: str, phase: str, hide: str | None = None):
        s = hub.fresh()
        try:
            ph = s.phase(product, phase)
        except KeyError:
            raise HTTPException(404) from None
        tasks = s.tasks()
        from ...model import goals_text

        goals = goals_text(ph.goals_path)
        specs = [(s.rel(p), p.read_text()) for p in ph.specs]
        docs = [(s.rel(p), p.read_text()) for p in ph.docs if p.suffix == ".md"]
        state = State(s.config.garden_dir / "state.json")
        stack = bool(s.config.get("stack", True))
        sched = hub.reader()  # a GET reads through the scheduler; it must not log (CG-182)
        phase_tasks = {t.id: t for t in ph.tasks}
        all_events = EventLog(s.config.garden_dir / "events.jsonl").read()
        m = metrics(all_events, phase_tasks)
        reviews = sorted((ph.path / "docs" / "reviews").glob("*.md"), reverse=True) if (ph.path / "docs" / "reviews").exists() else []
        rs = RunStore(s.config.garden_dir)
        usage = rs.usage_by_task()
        no_usage = rs.empty_usage()
        in_scope = [t for t in ph.tasks if t.status.value != "cancelled"]
        merged = sum(1 for t in in_scope if t.status.value == "done")
        prs_open = sum(1 for t in in_scope if t.pr and t.status.value != "done")
        complete = bool(in_scope) and merged == len(in_scope)
        sheet = {"merged": merged, "total": len(in_scope), "prs_open": prs_open, "complete": complete, "info": plant_info(ph.plant)}
        spent = sched.spent_for(ph.key)
        verdict_view = _verdict_view(sched.retro_verdict(ph.key), tasks)

        if ph.closed:
            # the closing header: the record of what the phase did, no working controls
            summary = phase_summary(all_events, phase_tasks)

            def doc_url(p: Path) -> str:
                return f"/phases/{ph.product}/{ph.name}/doc/{p.relative_to(ph.path)}"

            merged_rows = [{"t": t, "number": t.pr.rsplit("/", 1)[-1], "merged": (summary["done_at"].get(t.id) or "")[:10]}
                           for t in ph.tasks if t.pr and t.status.value == "done"]
            unmerged_rows = [{"t": t, "why": (split_log(t.body)[1] or ["closed unmerged"])[-1]}
                             for t in ph.tasks if t.pr and t.status.value != "done"]
            review_heads = []
            for p in reviews:
                head = _review_head(p)
                head["url"] = doc_url(p)
                head["tasks"] = [t for t in ph.tasks if t.discovered_from == f"persona:{head['persona']}"]
                review_heads.append(head)
            closing = next((p for p in ph.docs if "closing" in p.name and p.suffix == ".md"), None)
            friction = ph.path / "docs" / "friction.md"
            artifacts = [("closing document", doc_url(closing))] if closing else []
            if friction.exists():
                artifacts.append(("friction report (docs/friction.md)", doc_url(friction)))
            artifacts += [(s.rel(p), doc_url(p)) for p in ph.specs]
            artifacts += [(s.rel(p), doc_url(p)) for p in ph.docs
                          if p.suffix == ".md" and p != closing and p != friction and "reviews" not in p.parts]
            trials_n = sum(1 for tr in TrialLog(s.config.garden_dir / "trials.jsonl").read() if tr.get("task") in phase_tasks)
            return templates.TemplateResponse(request, "phase_closed.html", ctx(
                request, page="phase", phase_key=ph.key, phase=ph, goals_html=render_md(goals), sheet=sheet,
                summary=summary, metrics=m, spent=spent, review_heads=review_heads, artifacts=artifacts,
                trials_n=trials_n, merged_rows=merged_rows, unmerged_rows=unmerged_rows,
                has_retro=bool(_retro_doc(ph)),
                rows=[(t, effective_status(t, tasks, stack), state.get(t.id), usage.get(t.id) or no_usage)
                      for t in sorted(ph.tasks, key=lambda t: (t.priority, t.id))],
                retro_verdict=verdict_view,
            ))

        from ...brief import estimate_brief_tokens, phase_fixed_tokens
        from ...personas import DEFAULT_PERSONAS, list_personas

        fixed_tokens = phase_fixed_tokens(s, ph.tasks)

        phase_events = [e for e in all_events if e.get("task") in phase_tasks]
        hide_done = hide == "done"
        all_rows = [(t, effective_status(t, tasks, stack), state.get(t.id), usage.get(t.id) or no_usage, fixed_tokens + estimate_brief_tokens(s, t)[1]) for t in sorted(ph.tasks, key=lambda t: (t.priority, t.id))]
        hidden_count = sum(1 for row in all_rows if row[1] in ("done", "cancelled"))
        rows = [row for row in all_rows if not hide_done or row[1] not in ("done", "cancelled")]
        return templates.TemplateResponse(request, "phase.html", ctx(
            request, page="phase", phase_key=ph.key, phase=ph, goals_html=render_md(goals), specs=specs, docs=docs,
            sheet=sheet,
            burnup=burnup_svg(phase_events, len(in_scope), done_ids={t.id for t in in_scope if t.status.value == 'done'}), tiers=tier_bars_svg(tier_rows(s, phase_tasks)),
            personas=sorted(set(list_personas(s)) | set(DEFAULT_PERSONAS)),
            reviews=[{"rel": s.rel(p), "text": p.read_text(), **_review_head(p)} for p in reviews[:10]],
            budget=sched.budget_for(ph.key), spent=spent, metrics=m,
            rows=rows, hide_done=hide_done, hidden_count=hidden_count,
            planning=hub.planning.get(ph.key, ""), fixed_tokens=fixed_tokens,
            retro_pending=sched.retro_pending(ph.key), has_retro=bool(_retro_doc(ph)),
            new_task=_new_task_prefill(request),
            kickoff=_kickoff_panel(s, sched, ph),
            retro_verdict=verdict_view,
        ))

    @app.get("/herbarium", response_class=HTMLResponse)
    def herbarium(request: Request):
        s = hub.fresh()
        all_events = EventLog(s.config.garden_dir / "events.jsonl").read()
        sched = hub.reader()
        entries = []
        for p in s.products():
            for ph in p.phases:
                if not ph.closed:
                    continue
                phase_tasks = {t.id: t for t in ph.tasks}
                friction = ph.path / "docs" / "friction.md"
                closing = next((f for f in ph.docs if "closing" in f.name and f.suffix == ".md"), None)
                entries.append({
                    "phase": ph, "info": plant_info(ph.plant),
                    "summary": phase_summary(all_events, phase_tasks),
                    "spent": sched.spent_for(ph.key),
                    "friction_url": f"/phases/{ph.product}/{ph.name}/doc/docs/friction.md" if friction.exists() else "",
                    "closing_url": f"/phases/{ph.product}/{ph.name}/doc/{closing.relative_to(ph.path)}" if closing else "",
                    "retro_url": f"/phases/{ph.product}/{ph.name}/retro" if _retro_doc(ph) else "",
                    "scores": _persona_scores(ph),
                })
        entries.sort(key=lambda e: str(e["phase"].closed), reverse=True)
        groups: list[tuple[str, list]] = []
        if len({e["phase"].product for e in entries}) > 1:
            for e in entries:
                if not groups or groups[-1][0] != e["phase"].product:
                    groups.append((e["phase"].product, []))
                groups[-1][1].append(e)
        else:
            groups = [("", entries)]
        return templates.TemplateResponse(request, "herbarium.html", ctx(request, page="herbarium", groups=groups, n=len(entries)))

    @app.get("/phases/{product}/{phase}/retro", response_class=HTMLResponse)
    def phase_retro(request: Request, product: str, phase: str):
        s = hub.fresh()
        try:
            ph = s.phase(product, phase)
        except KeyError:
            raise HTTPException(404) from None

        def doc_url(p: Path) -> str:
            return f"/phases/{ph.product}/{ph.name}/doc/{p.relative_to(ph.path)}"

        recon = _retro_doc(ph)
        operator = _retro_operator(ph)
        reviews_dir = ph.path / "docs" / "reviews"
        reviews = sorted(reviews_dir.glob("*.md"), reverse=True) if reviews_dir.exists() else []
        persona_heads = []
        for p in reviews:
            head = _review_head(p)
            head["url"] = doc_url(p)
            persona_heads.append(head)
        retro_tasks = sorted((t for t in s.tasks().values() if t.discovered_from == f"retro:{ph.key}"),
                             key=lambda t: (t.phase, t.priority, t.id))
        all_events = EventLog(s.config.garden_dir / "events.jsonl").read()
        phase_tasks = {t.id: t for t in ph.tasks}
        summary = phase_summary(all_events, phase_tasks)
        runs = sum(r["runs"] for r in summary["metrics"]["tasks"])
        cancelled = sum(1 for t in ph.tasks if t.status.value == "cancelled")
        spent = hub.reader().spent_for(ph.key)
        return templates.TemplateResponse(request, "phase_retro.html", ctx(
            request, page="phase", phase_key=ph.key, phase=ph, summary=summary,
            runs=runs, cancelled=cancelled, spent=spent, has_retro=bool(recon),
            retro_html=render_md(recon.read_text()) if recon else "",
            operator_html=render_md(operator.read_text()) if operator else "",
            persona_heads=persona_heads, retro_tasks=retro_tasks))

    @app.get("/phases/{product}/{phase}/doc/{name:path}", response_class=HTMLResponse)
    def phase_doc(request: Request, product: str, phase: str, name: str):
        s = hub.fresh()
        try:
            ph = s.phase(product, phase)
        except KeyError:
            raise HTTPException(404) from None
        target = (ph.path / name).resolve()
        allowed = {p.resolve() for p in [*ph.docs, *ph.specs]}
        if target not in allowed or target.suffix != ".md":
            raise HTTPException(404)
        return templates.TemplateResponse(request, "doc.html", ctx(
            request, page="phase", phase_key=ph.key, phase=ph, name=name, doc_html=render_md(target.read_text())))


def _kickoff_panel(s: Any, sched: Any, ph: Any) -> dict[str, Any]:
    """The phase page's Kickoff panel context: whether a report exists, whether a review is
    in flight, and the live state of every item it raised (CG-224) — a draft task for each
    design/doc gap, a decision card for each question, cross-referenced by `discovered_from`
    and `phase` rather than trusted from the (possibly stale) committed report."""
    from ...kickoff import kickoff_doc_path

    tag = f"kickoff:{ph.key}"
    filed = sorted((t for t in s.tasks().values() if t.discovered_from == tag), key=lambda t: (t.priority, t.id))
    return {
        "has_report": kickoff_doc_path(ph).exists(),
        "running": sched.kickoff_pending(ph.key),
        "design_tasks": [t for t in filed if t.extra.get("spike")],
        "doc_tasks": [t for t in filed if not t.extra.get("spike")],
        "questions": [d for d in sched.pending_decisions() if d.get("kind") == "question" and d.get("phase") == ph.key],
    }


def _retro_doc(ph: Any) -> Path | None:
    """The reconciled retro document on disk once a phase's retro has run: docs/retro.md
    (what `garden retro` writes) or docs/retro/README.md (the phase-02 layout)."""
    for rel in ("retro.md", "retro/README.md"):
        p = ph.path / "docs" / rel
        if p.exists():
            return p
    return None


def _retro_operator(ph: Any) -> Path | None:
    """The operator's own retrospective, if written: docs/retro/operator.md."""
    p = ph.path / "docs" / "retro" / "operator.md"
    return p if p.exists() else None


def _persona_scores(ph: Any) -> list[dict[str, str]]:
    """The latest score per persona from docs/reviews/, newest kept, for the Herbarium card."""
    d = ph.path / "docs" / "reviews"
    if not d.exists():
        return []
    latest: dict[str, dict[str, str]] = {}
    for p in sorted(d.glob("*.md")):
        head = _review_head(p)
        if head["score"]:
            latest[head["persona"]] = {"persona": head["persona"], "score": head["score"]}
    return list(latest.values())


# The new-task form's field names; a failed submission redirects here with `nt_<field>`
# query params carrying back what was typed (see actions/phases.py: new_task_web).
NEW_TASK_FIELDS = ("title", "goal", "context", "acceptance", "difficulty", "priority", "reading", "depends_on", "ready")
NEW_TASK_DEFAULTS = {"difficulty": "medium", "priority": "3", "acceptance": "- [ ] \n- [ ] \n- [ ] "}


def _new_task_prefill(request: Request) -> dict[str, str]:
    return {f: request.query_params.get(f"nt_{f}", NEW_TASK_DEFAULTS.get(f, "")) for f in NEW_TASK_FIELDS}


def _verdict_view(rec: dict[str, Any] | None, tasks: dict[str, Any]) -> dict[str, Any] | None:
    """The retro verdict for the phase page: the record plus the generated tasks with their
    current status. A follow-up filed in the next phase lands only when the retro PR merges,
    so until then it shows as `in retro PR`."""
    from ...retro import PHASE_VERDICTS

    if not rec:
        return None

    def row(tid: str, kind: str) -> dict[str, str]:
        t = tasks.get(tid)
        return {"id": tid, "title": t.title if t else "", "kind": kind,
                "status": t.status.value if t else "in retro PR"}

    generated = ([row(i, "blocking") for i in rec.get("blocking_ids") or []]
                 + [row(i, "follow-up") for i in rec.get("followup_ids") or []])
    word = PHASE_VERDICTS.get(rec.get("verdict"), rec.get("verdict") or "none")
    return {**rec, "tasks": generated, "word": word,
            "pending": rec.get("status") == "pending", "verdict": rec.get("verdict") or ""}


def _review_head(path: Path) -> dict[str, Any]:
    """Persona, date, score, headline and high findings of a docs/reviews report
    (written by personas.report_markdown as <persona>-<date>[-n].md)."""
    m = re.match(r"(.+?)-(\d{4}-\d{2}-\d{2})(?:-\d+)?$", path.stem)
    persona, date = (m.group(1), m.group(2)) if m else (path.stem, "")
    score = ""
    overall = ""
    highs: list[str] = []
    features = 0
    section = ""
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            section = stripped[3:].lower()
            continue
        if "**Score:**" in stripped:
            sm = re.search(r"\*\*Score:\*\*\s*([^·]+)", stripped)
            score = sm.group(1).strip() if sm else ""
            continue
        # a `features` section renders one top-level bullet per feature (personas.report_markdown)
        if section == "features" and line.startswith("- "):
            features += 1
        if not stripped or stripped.startswith("#") or stripped.startswith("_"):
            continue
        if not section and not overall:
            overall = stripped
        elif section == "high" and stripped.startswith("- "):
            highs.append(stripped[2:])
    return {"persona": persona, "date": date, "score": score, "overall": overall, "highs": highs,
            "features": features}
