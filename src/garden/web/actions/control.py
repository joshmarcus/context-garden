"""Global controls: a manual tick, pause and resume, the live max_parallel override, the tool upgrade."""

from __future__ import annotations

from fastapi import FastAPI, Form, HTTPException, Request
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
            with hub.action_lock:
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
            with hub.action_lock:
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
    def web_set_max_parallel(request: Request, value: str = Form("")):
        value = value.strip()
        with hub.action_lock:
            sched = hub.scheduler()
            if not value:
                sched.clear_override("max_parallel", by="web")
                hub._log("max_parallel override cleared via web")
            else:
                try:
                    parsed = int(value)
                except ValueError:
                    raise HTTPException(400, "max_parallel must be a whole number") from None
                if parsed < 1:
                    raise HTTPException(400, "max_parallel must be at least 1")
                sched.set_override("max_parallel", parsed, by="web")
                hub._log(f"max_parallel set to {parsed} via web")
        return RedirectResponse(request.headers.get("referer", "/config"), status_code=303)

    @app.post("/config/observe-profile")
    def web_set_observe_profile(request: Request, value: str = Form("")):
        """Switch `garden observe`'s profile live (see garden.observe.resolve): a running
        `garden observe --follow` re-reads this override every pass, so the switch takes
        effect on its next tick without a restart. An empty value clears it, back to
        `observe.profile` in garden.yaml."""
        value = value.strip()
        with hub.action_lock:
            sched = hub.scheduler()
            if not value:
                sched.clear_override("observe.profile", by="web")
                hub._log("observe.profile override cleared via web")
            else:
                sched.set_override("observe.profile", value, by="web")
                hub._log(f"observe.profile set to {value} via web")
        return RedirectResponse(request.headers.get("referer", "/config"), status_code=303)

    @app.post("/config/operating-profile")
    def web_set_operating_profile(request: Request, value: str = Form("")):
        """Switch the operating profile (CG-221) live from the rail or the Config page: sets
        workers, reviews, the tier map, the review and retro tiers and the observe profile
        together. A running scheduler picks it up within a tick (Scheduler.effective); an
        empty value clears it, back to plain garden.yaml values."""
        value = value.strip()
        back = request.headers.get("referer", "/config")
        with hub.action_lock:
            sched = hub.scheduler()
            try:
                sched.set_operating_profile(value, by="web")
            except ValueError as e:
                return RedirectResponse(_flash_url(back, str(e)), status_code=303)
            hub._log(f"operating profile set to {value or '(none)'} via web")
        return RedirectResponse(back, status_code=303)

    @app.post("/config/accept-reload")
    def web_accept_config_reload(request: Request):
        """Apply a held garden.yaml reload now, even while its runs are still in flight
        (CG-242): the operator vouches the change is theirs, not a worker's write racing the
        fence."""
        back = request.headers.get("referer", "/config")
        with hub.action_lock:
            sched = hub.scheduler()
            if not sched.config_hold():
                return RedirectResponse(_flash_url(back, "no config reload is held"), status_code=303)
            sched.accept_config_reload(by="web")
            hub._log("held config reload accepted via web")
        return RedirectResponse(back, status_code=303)

    @app.post("/upgrade")
    def web_upgrade(request: Request):
        back = request.headers.get("referer", "/")
        try:
            with hub.action_lock:
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
