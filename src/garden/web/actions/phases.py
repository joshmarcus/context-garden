"""Phase actions: approve every draft, close the phase, set the budget, run persona reviews,
plan, create a task from the New task form."""

from __future__ import annotations

import re

from fastapi import BackgroundTasks, FastAPI, Form, HTTPException
from fastapi.responses import RedirectResponse

from ...github import GitHubError
from ...gitops import GitError
from ...model import Status, now_iso
from ..common import LOGGER, Site, _flash_url

_AC_LINE_RE = re.compile(r"^-\s*\[.\]\s*(.*)$")


def _parse_acceptance(text: str) -> list[str]:
    """Turn the acceptance-criteria textarea into a list of items: strip a leading markdown
    checkbox off each non-blank line (the form seeds the field with three empty ones), and
    drop lines that are still empty after that."""
    items = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        m = _AC_LINE_RE.match(line)
        items.append((m.group(1) if m else line).strip())
    return [i for i in items if i]


def register(app: FastAPI, site: Site) -> None:
    hub = site.hub

    @app.post("/phases/{product}/{phase}/approve-all")
    def approve_all(product: str, phase: str):
        back = f"/phases/{product}/{phase}"
        try:
            refusal = ""
            warning = ""
            with hub.action_lock:
                sched = hub.scheduler()
                try:
                    ph = sched.store.phase(product, phase)
                except KeyError:
                    raise HTTPException(404) from None
                for t in list(sched.store.tasks().values()):
                    if t.key == f"{product}/{phase}" and t.status == Status.DRAFT:
                        try:
                            warning = sched.approve(t, by="web", phase=ph) or warning
                        except RuntimeError as e:
                            refusal = str(e)
            if refusal:
                return RedirectResponse(_flash_url(back, refusal), status_code=303)
            if warning:
                return RedirectResponse(_flash_url(back, warning), status_code=303)
        except HTTPException:
            raise
        except (RuntimeError, GitError, GitHubError) as e:
            message = str(e)
            hub._log(f"approve-all {product}/{phase} failed: {message}")
            return RedirectResponse(_flash_url(back, message), status_code=303)
        except Exception:
            LOGGER.exception("approve-all %s/%s failed", product, phase)
            hub._log(f"approve-all {product}/{phase} failed: unexpected error, see the log")
            return RedirectResponse(_flash_url(back, "something failed; see the log"), status_code=303)
        return RedirectResponse(back, status_code=303)

    @app.post("/phases/{product}/{phase}/close")
    def close_phase(product: str, phase: str):
        back = f"/phases/{product}/{phase}"
        try:
            with hub.action_lock:
                sched = hub.scheduler()
                try:
                    ph = sched.store.phase(product, phase)
                except KeyError:
                    raise HTTPException(404) from None
                sched.close_phase(ph)
        except HTTPException:
            raise
        except (RuntimeError, GitError, GitHubError) as e:
            message = str(e)
            hub._log(f"close {product}/{phase} failed: {message}")
            return RedirectResponse(_flash_url(back, message), status_code=303)
        except Exception:
            LOGGER.exception("close %s/%s failed", product, phase)
            hub._log(f"close {product}/{phase} failed: unexpected error, see the log")
            return RedirectResponse(_flash_url(back, "something failed; see the log"), status_code=303)
        return RedirectResponse(back, status_code=303)

    @app.post("/phases/{product}/{phase}/retro-decide")
    def retro_decide(product: str, phase: str, choice: str = Form(""), note: str = Form("")):
        back = f"/phases/{product}/{phase}"
        try:
            with hub.action_lock:
                sched = hub.scheduler()
                try:
                    ph = sched.store.phase(product, phase)
                except KeyError:
                    raise HTTPException(404) from None
                sched.retro_decide(ph, choice, note=note, by="web")
        except HTTPException:
            raise
        except (RuntimeError, GitError, GitHubError) as e:
            message = str(e)
            hub._log(f"retro-decide {product}/{phase} failed: {message}")
            return RedirectResponse(_flash_url(back, message), status_code=303)
        except Exception:
            LOGGER.exception("retro-decide %s/%s failed", product, phase)
            hub._log(f"retro-decide {product}/{phase} failed: unexpected error, see the log")
            return RedirectResponse(_flash_url(back, "something failed; see the log"), status_code=303)
        return RedirectResponse(back, status_code=303)

    @app.post("/phases/{product}/{phase}/budget")
    def set_budget(product: str, phase: str, amount: str = Form(""), no_budget: str = Form("")):
        key = f"{product}/{phase}"
        with hub.action_lock:
            sched = hub.scheduler()
            if no_budget or not amount.strip():
                sched.set_budget(key, None, by="web")
            else:
                try:
                    usd = float(amount)
                except ValueError:
                    raise HTTPException(400, "budget must be a number") from None
                if usd < 0:
                    raise HTTPException(400, "budget must not be negative")
                sched.set_budget(key, usd, by="web")
        return RedirectResponse(f"/phases/{product}/{phase}", status_code=303)

    @app.post("/phases/{product}/{phase}/persona")
    def persona_phase(product: str, phase: str, personas: str = Form(""), file_tasks: str = Form("")):
        s = hub.fresh()
        back = f"/phases/{product}/{phase}"
        try:
            ph = s.phase(product, phase)
        except KeyError:
            raise HTTPException(404) from None
        try:
            with hub.action_lock:
                sched = hub.scheduler()
                for name in [n.strip() for n in personas.split(",") if n.strip()]:
                    sched.dispatch_persona_phase(ph, name, file_tasks=bool(file_tasks))
        except (RuntimeError, GitError, GitHubError) as e:
            message = str(e)
            hub._log(f"persona review {product}/{phase} failed: {message}")
            return RedirectResponse(_flash_url(back, message), status_code=303)
        except Exception:
            LOGGER.exception("persona review %s/%s failed", product, phase)
            hub._log(f"persona review {product}/{phase} failed: unexpected error, see the log")
            return RedirectResponse(_flash_url(back, "something failed; see the log"), status_code=303)
        return RedirectResponse(back, status_code=303)

    @app.post("/phases/{product}/{phase}/kickoff")
    def kickoff_phase(product: str, phase: str):
        s = hub.fresh()
        back = f"/phases/{product}/{phase}"
        try:
            ph = s.phase(product, phase)
        except KeyError:
            raise HTTPException(404) from None
        try:
            with hub.action_lock:
                sched = hub.scheduler()
                sched.start_kickoff(ph)
        except (RuntimeError, GitError, GitHubError) as e:
            message = str(e)
            hub._log(f"kickoff {product}/{phase} failed: {message}")
            return RedirectResponse(_flash_url(back, message), status_code=303)
        except Exception:
            LOGGER.exception("kickoff %s/%s failed", product, phase)
            hub._log(f"kickoff {product}/{phase} failed: unexpected error, see the log")
            return RedirectResponse(_flash_url(back, "something failed; see the log"), status_code=303)
        return RedirectResponse(_flash_url(back, "kickoff review started; `garden tick` writes the report"), status_code=303)

    @app.post("/phases/{product}/{phase}/plan")
    def plan_phase(product: str, phase: str, background: BackgroundTasks, guidance: str = Form("")):
        key = f"{product}/{phase}"
        back = f"/phases/{product}/{phase}"
        if hub.planning.get(key, "").startswith("running"):
            return RedirectResponse(back, status_code=303)
        try:
            ph = hub.fresh().phase(product, phase)
        except KeyError:
            raise HTTPException(404) from None
        if ph.closed:
            hub.planning[key] = f"failed {now_iso()}: {ph.key} is closed ({ph.closed}); reopen it first (`garden reopen-phase {ph.key}`)"
            return RedirectResponse(back, status_code=303)
        if ph.frozen:
            hub.planning[key] = f"failed {now_iso()}: {ph.key} is frozen ({ph.frozen}); planning is blocked while frozen -- unfreeze it first (`garden unfreeze {ph.key}`)"
            return RedirectResponse(back, status_code=303)
        hub.planning[key] = f"running since {now_iso()}"

        def job() -> None:
            from ...planner import import_plan, parse_plan, plan_prompt, run_planner

            try:
                s = hub.fresh()
                raw = run_planner(s, plan_prompt(s, product, phase, extra=guidance))
                created = import_plan(s, product, phase, parse_plan(raw))  # ready by default (plan.auto_approve)
                hub.planning[key] = f"done {now_iso()}: created {', '.join(t.id for t in created) or 'nothing new'}"
            except Exception as e:  # noqa: BLE001
                hub.planning[key] = f"failed {now_iso()}: {e}"

        background.add_task(job)
        return RedirectResponse(back, status_code=303)

    @app.post("/phases/{product}/{phase}/new-task")
    def new_task_web(
        product: str, phase: str,
        title: str = Form(""),
        goal: str = Form(""),
        context: str = Form(""),
        acceptance: str = Form(""),
        difficulty: str = Form("medium"),
        priority: str = Form("3"),
        reading: str = Form(""),
        depends_on: str = Form(""),
        ready: str = Form(""),
    ):
        from ...brief import resolve_reading
        from ...harness import DIFFICULTIES
        from ...model import Task
        from ...scaffold import render_task_body

        s = hub.fresh()
        back = f"/phases/{product}/{phase}"
        try:
            ph = s.phase(product, phase)
        except KeyError:
            raise HTTPException(404) from None

        typed = {"title": title, "goal": goal, "context": context, "acceptance": acceptance,
                 "difficulty": difficulty, "priority": priority, "reading": reading,
                 "depends_on": depends_on, "ready": ready}

        errors: list[str] = []
        title = title.strip()
        if not title:
            errors.append("a title is required")
        if difficulty not in DIFFICULTIES:
            errors.append(f"difficulty must be one of {', '.join(DIFFICULTIES)}")
        try:
            priority_n = int(priority.strip())
        except ValueError:
            errors.append("priority must be a number")
            priority_n = 3
        deps = [d.strip() for d in re.split(r"[,\n]", depends_on) if d.strip()]
        tasks = s.tasks()
        unknown_deps = [d for d in deps if d not in tasks]
        if unknown_deps:
            errors.append(f"depends on unknown task(s): {', '.join(unknown_deps)}")
        reading_list = [r.strip() for r in reading.splitlines() if r.strip()]
        stub = Task(path=ph.path / "tasks" / "new.md", id="", title=title or "untitled", product=product, phase=phase)
        for r in reading_list:
            if resolve_reading(s, stub, r)[0] is None:
                errors.append(f"reading path {r!r} does not exist in the garden or the product checkout")

        if errors:
            return RedirectResponse(_flash_url(f"{back}#new-task", "; ".join(errors), extra={f"nt_{k}": v for k, v in typed.items()}), status_code=303)

        refusal = ""
        warning = ""
        try:
            with hub.action_lock:
                sched = hub.scheduler()
                try:
                    ph = sched.store.phase(product, phase)
                except KeyError:
                    raise HTTPException(404) from None
                if ph.closed:
                    raise RuntimeError(f"{ph.key} is closed ({ph.closed}); reopen it first (`garden reopen-phase {ph.key}`)")
                body = render_task_body(goal=goal, context=context, acceptance=_parse_acceptance(acceptance))
                t = sched.store.create_task(product, phase, title, body, depends_on=deps, reading=reading_list,
                                  priority=priority_n, status="draft", difficulty=difficulty)
                if ready:
                    # Goes through the same gate a hand approval does (CG-238): a placeholder
                    # acceptance criterion or an unresolved reading path leaves the task a
                    # draft instead of dispatching a brief that isn't ready to cost a run.
                    try:
                        warning = sched.approve(t, by="web", phase=ph) or ""
                    except RuntimeError as e:
                        refusal = str(e)
        except HTTPException:
            raise
        except (RuntimeError, GitError, GitHubError) as e:
            return RedirectResponse(_flash_url(f"{back}#new-task", str(e), extra={f"nt_{k}": v for k, v in typed.items()}), status_code=303)
        except Exception:
            LOGGER.exception("new-task %s/%s failed", product, phase)
            return RedirectResponse(_flash_url(f"{back}#new-task", "something failed; see the log", extra={f"nt_{k}": v for k, v in typed.items()}), status_code=303)
        if refusal:
            return RedirectResponse(_flash_url(f"/tasks/{t.id}", f"created {t.id} as a draft: {refusal} ({s.rel(t.path)})"), status_code=303)
        message = f"created {t.id}" + (f"; {warning}" if warning else "")
        return RedirectResponse(_flash_url(f"/tasks/{t.id}", message), status_code=303)
