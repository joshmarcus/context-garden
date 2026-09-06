"""The friction report form."""

from __future__ import annotations

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import RedirectResponse

from ...github import GitHubError
from ...gitops import GitError
from ..common import LOGGER, Site, _flash_url


def register(app: FastAPI, site: Site) -> None:
    hub = site.hub

    @app.post("/friction-report")
    def friction_report_web(
        request: Request,
        product: str = Form(...),
        phase: str = Form(...),
        text: str = Form(...),
        page: str = Form(""),
        task_id: str = Form(""),
    ):
        import datetime as _dt

        from ...friction import append_friction_report, create_friction_draft_task

        s = hub.fresh()
        back = request.headers.get("referer", "/")
        try:
            ph = s.phase(product, phase)
        except KeyError:
            raise HTTPException(404) from None
        try:
            doc = ph.path / "docs" / "friction.md"
            provenance = page or "web"
            if task_id:
                provenance = f"{page} ({task_id})" if page else task_id
            date = _dt.date.today().isoformat()
            append_friction_report(doc, text, provenance, date)
            create_friction_draft_task(s, product, phase, text, provenance, date)
            s.invalidate_tasks()
        except (RuntimeError, GitError, GitHubError) as e:
            message = str(e)
            hub._log(f"friction report {product}/{phase} failed: {message}")
            return RedirectResponse(_flash_url(back, message), status_code=303)
        except Exception:
            LOGGER.exception("friction report %s/%s failed", product, phase)
            hub._log(f"friction report {product}/{phase} failed: unexpected error, see the log")
            return RedirectResponse(_flash_url(back, "something failed; see the log"), status_code=303)
        return RedirectResponse(back, status_code=303)
