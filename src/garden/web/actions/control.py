"""Global controls: a manual tick, pause and resume, the live max_parallel override, the tool upgrade."""

from __future__ import annotations

from fastapi import FastAPI, Form, Request
from fastapi.responses import RedirectResponse

from ...github import GitHubError
from ...gitops import GitError
from ..common import LOGGER, Site, _flash_url


def register(app: FastAPI, site: Site) -> None:
    hub = site.hub

    @app.post("/tick")
    def tick(request: Request):
        summary = hub.tick()
        hub._log(f"manual tick: {summary}")
        return RedirectResponse(request.headers.get("referer", "/"), status_code=303)

    @app.post("/pause")
    def web_pause(request: Request, reason: str = Form("")):
        back = request.headers.get("referer", "/")
        try:
            with hub.lock:
                sched = hub.scheduler()
                sched.pause(by="web", reason=reason.strip())
            hub._log("dispatch paused via web" + (f": {reason.strip()}" if reason.strip() else ""))
        except (RuntimeError, GitError, GitHubError) as e:
            message = str(e)
            hub._log(f"pause failed: {message}")
            return RedirectResponse(_flash_url(back, message), status_code=303)
        except Exception:
            LOGGER.exception("pause failed")
            hub._log("pause failed: unexpected error, see the log")
            return RedirectResponse(_flash_url(back, "something failed; see the log"), status_code=303)
        return RedirectResponse(back, status_code=303)

    @app.post("/resume")
    def web_resume(request: Request):
        back = request.headers.get("referer", "/")
        try:
            with hub.lock:
                sched = hub.scheduler()
                sched.resume(by="web")
            hub._log("dispatch resumed via web")
        except (RuntimeError, GitError, GitHubError) as e:
            message = str(e)
            hub._log(f"resume failed: {message}")
            return RedirectResponse(_flash_url(back, message), status_code=303)
        except Exception:
            LOGGER.exception("resume failed")
            hub._log("resume failed: unexpected error, see the log")
            return RedirectResponse(_flash_url(back, "something failed; see the log"), status_code=303)
        return RedirectResponse(back, status_code=303)

    @app.post("/config/max-parallel")
    def web_set_max_parallel(request: Request, value: int = Form(...)):
        with hub.lock:
            sched = hub.scheduler()
            sched.set_override("max_parallel", value, by="web")
        hub._log(f"max_parallel set to {value} via web")
        return RedirectResponse(request.headers.get("referer", "/config"), status_code=303)

    @app.post("/config/max-parallel/clear")
    def web_clear_max_parallel(request: Request):
        with hub.lock:
            sched = hub.scheduler()
            sched.clear_override("max_parallel", by="web")
        hub._log("max_parallel override cleared via web")
        return RedirectResponse(request.headers.get("referer", "/config"), status_code=303)

    @app.post("/upgrade")
    def web_upgrade(request: Request):
        back = request.headers.get("referer", "/")
        try:
            with hub.lock:
                sched = hub.scheduler()
                result = sched.upgrade(restart=True)
        except (RuntimeError, GitError, GitHubError) as e:
            message = str(e)
            hub._log(f"tool upgrade failed: {message}")
            return RedirectResponse(_flash_url(back, message), status_code=303)
        except Exception:
            LOGGER.exception("tool upgrade failed")
            hub._log("tool upgrade failed: unexpected error, see the log")
            return RedirectResponse(_flash_url(back, "something failed; see the log"), status_code=303)
        # On success the process re-execs and never reaches here; a failure falls through.
        reason = result.get("reason") or "see the log"
        hub._log("tool upgrade: " + ("restarting" if result.get("ok") else f"failed ({reason})"))
        if not result.get("ok"):
            return RedirectResponse(_flash_url(back, f"upgrade failed: {reason}"), status_code=303)
        return RedirectResponse(back, status_code=303)
