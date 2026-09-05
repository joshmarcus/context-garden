"""Phase actions: approve every draft, close the phase, set the budget, run persona reviews, plan."""

from __future__ import annotations

from fastapi import BackgroundTasks, FastAPI, Form, HTTPException
from fastapi.responses import RedirectResponse

from ...github import GitHubError
from ...gitops import GitError
from ...model import Status, now_iso, phase_refusal
from ..common import LOGGER, Site, _flash_url


def register(app: FastAPI, site: Site) -> None:
    hub = site.hub

    @app.post("/phases/{product}/{phase}/approve-all")
    def approve_all(product: str, phase: str):
        s = hub.fresh()
        back = f"/phases/{product}/{phase}"
        try:
            ph = s.phase(product, phase)
        except KeyError:
            raise HTTPException(404) from None
        try:
            refusal = ""
            for t in s.tasks().values():
                if t.key == f"{product}/{phase}" and t.status == Status.DRAFT:
                    refusal = phase_refusal(ph, t)
                    if refusal:
                        continue
                    t.status = Status.READY
                    t.log("approved (web)")
                    s.save(t)
            if refusal:
                return RedirectResponse(_flash_url(back, refusal), status_code=303)
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
            with hub.lock:
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

    @app.post("/phases/{product}/{phase}/budget")
    def set_budget(product: str, phase: str, amount: str = Form(""), no_budget: str = Form("")):
        key = f"{product}/{phase}"
        with hub.lock:
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
            with hub.lock:
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
