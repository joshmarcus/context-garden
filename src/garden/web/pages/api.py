"""JSON endpoints."""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from ... import gitops
from ...events import DECISION_KINDS, EventLog, decision_notifications
from ...graph import effective_status
from ..common import Site


def register(app: FastAPI, site: Site) -> None:
    hub = site.hub

    def worker_host(authorization: str) -> dict[str, Any]:
        if not authorization.startswith("Bearer "):
            raise HTTPException(401, "bearer token required")
        token = authorization[7:]
        for host in (hub.store.config.get("workers.hosts") or []):
            if token and token == os.environ.get(str(host.get("token_env") or ""), ""):
                return dict(host)
        raise HTTPException(403, "unknown worker token")

    def leased(run: Any) -> bool:
        return bool(run.lease_expires_at and run.lease_expires_at > dt.datetime.now(dt.UTC).isoformat())

    def run_for(run_id: str):
        from ...runs import RunStore

        run = next((r for r in RunStore(hub.store.config.garden_dir).all_runs() if r.run_id == run_id), None)
        if run is None or run.runner != "remote":
            raise HTTPException(404, "remote run not found")
        return run

    @app.get("/api/tasks")
    def api_tasks():
        s = hub.fresh()
        tasks = s.tasks()
        stack = bool(s.config.get("stack", True))
        return JSONResponse([{**t.to_frontmatter(), "effective_status": effective_status(t, tasks, stack)} for t in tasks.values()])

    @app.get("/api/events")
    def api_events():
        return JSONResponse(hub.events[-50:])

    @app.get("/api/decisions")
    def api_decisions(since: str = ""):
        """The decision-kind events since a timestamp, each with a one-line title and the URL
        to open — what an open browser tab polls to notify a person that the loop needs them
        (CG-208). Notices never appear here; on `since` in the future it returns nothing."""
        s = hub.fresh()
        evs = EventLog(s.config.garden_dir / "events.jsonl").read(since=since, kinds=DECISION_KINDS)
        titles = {t.id: t.title for t in s.tasks().values()}
        return JSONResponse(decision_notifications(evs, titles))

    @app.post("/api/runs/claim")
    async def claim(request: Request, authorization: str = Header(default="")):
        """Atomically lease the oldest compatible queued remote run to one configured host."""
        host_cfg = worker_host(authorization)
        body = await request.json()
        if str(body.get("host") or "") != str(host_cfg.get("name") or ""):
            raise HTTPException(403, "token does not belong to this host")
        offered = {str(x) for x in (body.get("harnesses") or [])}
        tiers = {str(x) for x in (body.get("tiers") or [])}
        capacity = min(max(1, int(body.get("capacity") or 1)), int(host_cfg.get("max_parallel") or 1))
        with hub.action_lock:
            from ...runner.base import pass_env_patterns
            from ...runs import RunStore

            runs = RunStore(hub.store.config.garden_dir).all_runs()
            owned = [r for r in runs if r.runner == "remote" and r.status == "running"
                     and r.host == body["host"] and leased(r)]
            if len(owned) >= capacity:
                return JSONResponse({}, status_code=204)
            now = dt.datetime.now(dt.UTC)
            for run in runs:
                if run.runner != "remote" or run.status != "running":
                    continue
                if run.host and leased(run):
                    continue
                if run.harness and offered and run.harness not in offered:
                    continue
                if run.difficulty and tiers and run.difficulty not in tiers:
                    continue
                run.host = str(body["host"])
                run.claimed_at = now.isoformat()
                run.lease_expires_at = (now + dt.timedelta(seconds=int(hub.store.config.get("workers.lease_seconds", 120)))).isoformat()
                run.save()
                task = hub.fresh().task(run.task_id)
                harness = hub.store.config.harness(run.harness) if run.harness else None
                configured_repo = task.repo or hub.store.config.product_repo(task.product)
                repo_value = str(configured_repo)
                repo_path = Path(repo_value)
                if not repo_path.is_absolute() and not repo_value.startswith(("http://", "https://", "git@", "ssh://")):
                    repo_value = str((hub.store.root / repo_path).resolve())
                    repo_path = Path(repo_value)
                if repo_path.exists():
                    try:
                        repo_value = gitops.git("remote", "get-url", "origin", cwd=repo_path).strip()
                    except gitops.GitError:
                        pass
                payload: dict[str, Any] = {
                    "id": run.run_id, "task_id": run.task_id, "mode": run.mode,
                    "brief": (run.path / "brief.md").read_text() if (run.path / "brief.md").exists() else "",
                    "branch": run.branch, "base": run.base,
                    "repo": repo_value,
                    "setup": {"command": str((hub.store.config.product_setup(task.product) or {}).get("command") or ""),
                              "timeout_seconds": int((hub.store.config.product_setup(task.product) or {}).get("timeout_seconds") or 600)},
                    "env_allowlist": pass_env_patterns(hub.store.config.data),
                    "harness": run.harness, "model": run.model, "difficulty": run.difficulty,
                    "harness_config": {k: v for k, v in ((harness.cfg if harness else {}) or {}).items()
                                       if k in {"bin", "args", "max_turns", "output_format"}},
                    "turn_cap": harness.max_turns_for(run.difficulty) if harness else 0,
                }
                checks = run.path / "checks_input.json"
                if checks.exists():
                    check_payload = json.loads(checks.read_text())
                    # Host-local paths and the scheduler config never cross the boundary.
                    payload["checks"] = {k: v for k, v in check_payload.items() if k in {"specs", "timeout", "extra"}}
                return JSONResponse(payload)
        return JSONResponse({}, status_code=204)

    @app.post("/api/runs/{run_id}/heartbeat")
    async def heartbeat(run_id: str, request: Request, authorization: str = Header(default="")):
        host = worker_host(authorization)
        run = run_for(run_id)
        if run.host != host.get("name"):
            raise HTTPException(409, "run is leased to another host")
        body = await request.json()
        chunk = str(body.get("transcript") or "")
        if chunk:
            with (run.path / "stdout.json").open("a") as f:
                f.write(chunk)
        run.lease_expires_at = (dt.datetime.now(dt.UTC) + dt.timedelta(seconds=int(hub.store.config.get("workers.lease_seconds", 120)))).isoformat()
        run.save()
        return {"ok": True, "lease_expires_at": run.lease_expires_at}

    @app.post("/api/runs/{run_id}/finish")
    async def finish(run_id: str, request: Request, authorization: str = Header(default="")):
        host = worker_host(authorization)
        run = run_for(run_id)
        if run.host != host.get("name"):
            raise HTTPException(409, "run is leased to another host")
        body = await request.json()
        run.pushed_head = str(body.get("pushed_head") or "")
        final = str(body.get("final_text") or "")
        (run.path / "final.md").write_text(final)
        (run.path / "exit_code").write_text(str(int(body.get("exit_code") or 0)))
        posted = {"result": body.get("result") or {}, "usage": body.get("usage") or {},
                  "cost_usd": body.get("cost_usd"), "final_text": final,
                  "error": str(body.get("error") or ""), "session_id": str(body.get("session_id") or "")}
        (run.path / "remote_result.json").write_text(json.dumps(posted))
        if run.mode == "check":
            (run.path / "checks.json").write_text(json.dumps((body.get("result") or {}).get("checks") or []))
        run.save()
        return {"ok": True}
