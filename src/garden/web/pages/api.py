"""JSON endpoints."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import os
import re
import secrets
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from ... import gitops
from ...events import DECISION_KINDS, EventLog, decision_notifications
from ...github import is_git_remote_url
from ...graph import effective_status
from ...model import effective_owner
from ...runs import Run
from ..common import Site


def register(app: FastAPI, site: Site) -> None:
    hub = site.hub

    def host_facts(value: Any) -> dict[str, Any] | None:
        """Validate the small, durable host-attribution record at the HTTP boundary."""
        if value is None:
            return None
        if not isinstance(value, dict):
            raise HTTPException(422, "host_facts must be an object")
        text_fields = ("profile_version", "bootstrap_version", "source_head", "provider_id")
        number_fields = ("memory_available_bytes", "memory_total_bytes", "disk_free_bytes",
                         "cpu_count", "observed_at")
        facts: dict[str, Any] = {}
        for name in text_fields:
            item = value.get(name)
            if item is not None:
                if not isinstance(item, str) or not item or len(item) > 128:
                    raise HTTPException(422, f"host_facts.{name} must be 1-128 characters")
                facts[name] = item
        for name in number_fields:
            item = value.get(name)
            if item is not None:
                if isinstance(item, bool) or not isinstance(item, (int, float)) \
                        or item < 0 or item > 2**63 - 1 or not math.isfinite(item):
                    raise HTTPException(422, f"host_facts.{name} must be a finite number between 0 and 2**63-1")
                facts[name] = item
        return facts

    def persist_host_facts(run: Any, value: Any) -> None:
        facts = host_facts(value)
        if facts is not None:
            (run.path / "host_facts.json").write_text(json.dumps(facts))

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

    def execution_deadline(run: Any) -> dt.datetime | None:
        """Fixed controller deadline; heartbeats renew liveness, never execution budget."""
        started = run.execution_started_at or run.claimed_at
        timeout = float(hub.store.config.get("timeout_minutes", 90) or 0)
        if not started or not timeout:
            return None
        return dt.datetime.fromisoformat(started) + dt.timedelta(minutes=timeout + 5)

    def run_for(run_id: str):
        from ...runs import RunStore

        run = next((r for r in RunStore(hub.store.config.garden_dir).all_runs() if r.run_id == run_id), None)
        if run is None or run.runner != "remote":
            raise HTTPException(404, "remote run not found")
        return run

    def claimed_run(run_id: str, host: dict[str, Any], lease_token: str):
        run = run_for(run_id)
        if run.status != "running" or run.process_finished():
            raise HTTPException(409, "run generation is no longer active")
        deadline = execution_deadline(run)
        if deadline is not None and dt.datetime.now(dt.UTC) >= deadline:
            raise HTTPException(409, "run execution deadline has passed")
        if run.host != host.get("name"):
            raise HTTPException(409, "run is leased to another host")
        if not lease_token or not secrets.compare_digest(run.lease_token, lease_token):
            raise HTTPException(409, "run lease has been replaced")
        if not leased(run):
            raise HTTPException(409, "run lease has expired")
        return run

    def credential_free_repo_url(value: str) -> str:
        """Return a clone URL without credentials; reject ambiguous git URL syntax."""
        if "://" not in value:
            # SCP syntax permits an arbitrary transport username. It cannot contain a
            # colon, so this preserves service-account identities without accepting a
            # password-bearing URL form.
            if re.fullmatch(r"(?:[A-Za-z0-9._-]+@)?[A-Za-z0-9.-]+:[^\s:@]+(?:/[^\s:@]+)*", value):
                return value
            if "@" in value or re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", value):
                raise HTTPException(409, "repository remote is not a safe clone URL")
            return value
        parts = urlsplit(value)
        if parts.scheme not in {"http", "https", "ssh"} or not parts.hostname:
            raise HTTPException(409, "repository remote is not a safe clone URL")
        host = parts.hostname or ""
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        try:
            port = parts.port
        except ValueError:
            raise HTTPException(409, "repository remote is not a safe clone URL") from None
        if port:
            host = f"{host}:{port}"
        if parts.scheme == "ssh":
            if parts.password is not None:
                raise HTTPException(409, "repository remote is not a safe clone URL")
            if parts.username is not None:
                if not re.fullmatch(r"[A-Za-z0-9._-]+", parts.username):
                    raise HTTPException(409, "repository remote is not a safe clone URL")
                host = f"{parts.username}@{host}"
        return urlunsplit((parts.scheme, host, parts.path, "", ""))

    @app.get("/api/tasks")
    def api_tasks():
        s = hub.fresh()
        tasks = s.tasks()
        stack = bool(s.config.get("stack", True))
        return JSONResponse([{**t.to_frontmatter(), "effective_status": effective_status(t, tasks, stack),
                              "effective_owner": effective_owner(t, s.phase(t.product, t.phase))[0],
                              "owner_source": effective_owner(t, s.phase(t.product, t.phase))[1]}
                             for t in tasks.values()])

    @app.get("/api/operations/{task_id}/{run_id}")
    def api_operation(task_id: str, run_id: str):
        """Read one durable launch identity directly; never scan run history."""
        if any(part in {"", ".", ".."} or "/" in part or "\\" in part for part in (task_id, run_id)):
            raise HTTPException(404)
        path = hub.store.config.garden_dir / "runs" / task_id / run_id
        try:
            run = Run.load(path)
        except (OSError, ValueError, TypeError):
            raise HTTPException(404) from None
        return JSONResponse({"operation_id": run.run_id, "task_id": run.task_id,
                             "state": run.lifecycle_state, "status": run.status,
                             "pid": run.pid if run.status == "running" else None,
                             "requested_at": run.started_at, "finished_at": run.finished_at,
                             "error": run.error})

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
                     and r.host == body["host"] and leased(r) and not r.process_finished()]
            if len(owned) >= capacity:
                return Response(status_code=204)
            now = dt.datetime.now(dt.UTC)
            for run in runs:
                if run.runner != "remote" or run.status != "running" or run.process_finished():
                    continue
                if run.host and leased(run):
                    continue
                deadline = execution_deadline(run)
                if deadline is not None and now >= deadline:
                    continue
                # Checks execute the portable check payload and need no model harness.
                # Every other remote mode is harness-backed: an empty offer means the
                # host cannot execute it, rather than acting as a wildcard.
                if run.mode != "check" and (not run.harness or run.harness not in offered):
                    continue
                if run.difficulty and tiers and run.difficulty not in tiers:
                    continue
                run.host = str(body["host"])
                claim_time = now.isoformat()
                if not run.claimed_at:
                    run.claimed_at = claim_time
                if not run.execution_started_at:
                    run.execution_started_at = claim_time
                run.lease_updated_at = claim_time
                run.lease_expires_at = (now + dt.timedelta(seconds=int(hub.store.config.get("workers.lease_seconds", 120)))).isoformat()
                run.lease_token = secrets.token_urlsafe(32)
                fresh = hub.fresh()
                task = fresh.tasks().get(run.task_id)
                product = task.product if task is not None else str(run.env_snapshot.get("product") or "")
                if not product:
                    raise HTTPException(409, "remote run has no product identity")
                harness = hub.store.config.harness(run.harness) if run.harness else None
                configured_repo = (task.repo if task is not None else "") or hub.store.config.product_repo(product)
                repo_value = str(configured_repo)
                repo_path = Path(repo_value)
                if not repo_path.is_absolute() and not is_git_remote_url(repo_value):
                    repo_value = str((hub.store.root / repo_path).resolve())
                    repo_path = Path(repo_value)
                if repo_path.exists():
                    try:
                        repo_value = gitops.git("remote", "get-url", "origin", cwd=repo_path).strip()
                    except gitops.GitError:
                        pass
                repo_value = credential_free_repo_url(repo_value)
                # A worker never writes the task branch directly. Each lease owns an
                # unguessable staging ref; after an authenticated finish the scheduler
                # promotes that exact commit. An expired generation can therefore push only
                # to its abandoned ref, never overwrite work from its replacement.
                run.pushed_ref = f"refs/heads/garden-worker/{run.run_id}/{secrets.token_urlsafe(12)}"
                run.claim_history.append({"claimed_at": claim_time, "host": run.host,
                                          "lease_token_sha256": hashlib.sha256(run.lease_token.encode()).hexdigest(),
                                          "pushed_ref": run.pushed_ref})
                try:
                    source = str(configured_repo)
                    scheduler_repo = gitops.ensure_repo(
                        source if is_git_remote_url(source) else repo_path,
                        hub.store.config.repos_dir,
                    )
                    gitops.fetch(scheduler_repo)
                    run.start_head = gitops.remote_head(scheduler_repo, run.branch)
                except (AttributeError, gitops.GitError):
                    run.start_head = ""
                persist_host_facts(run, body.get("host_facts"))
                run.save()
                setup = hub.store.config.product_setup(product) or {}
                payload: dict[str, Any] = {
                    "id": run.run_id, "task_id": run.task_id, "mode": run.mode,
                    "lease_token": run.lease_token,
                    "heartbeat_seconds": max(0.05, int(hub.store.config.get("workers.lease_seconds", 120)) / 3),
                    "execution_deadline_at": execution_deadline(run).isoformat()
                    if execution_deadline(run) is not None else "",
                    "brief": (run.path / "brief.md").read_text() if (run.path / "brief.md").exists() else "",
                    "branch": run.branch, "base": run.base,
                    "push_ref": run.pushed_ref,
                    "repo": repo_value,
                    # The product command is trusted executable configuration. Values from
                    # setup.env stay host-local; execution uses the claim's scrubbed env.
                    "setup": {"command": str(setup.get("command") or ""),
                              "timeout_seconds": int(setup.get("timeout_seconds") or 600)},
                    "env_allowlist": pass_env_patterns(hub.store.config.data),
                    "validation_timeout_seconds": int(
                        hub.store.config.get("checks.timeout_seconds", 900) or 900
                    ),
                    # Only mapping metadata crosses; file contents and credentials remain
                    # host-local and are resolved by the portable worker.
                    "config_files": dict(hub.store.config.get("worker_env.config_files") or {}),
                    "harness": run.harness, "model": run.model, "difficulty": run.difficulty,
                    # Command arguments may contain inline API keys. Remote hosts use the
                    # built-in harness defaults; only inert executable/output settings cross.
                    "harness_config": {k: v for k, v in ((harness.cfg if harness else {}) or {}).items()
                                       if k in {"bin", "max_turns", "output_format", "permission_mode"}},
                    "turn_cap": harness.max_turns_for(run.difficulty) if harness else 0,
                }
                checks = run.path / "checks_input.json"
                if checks.exists():
                    check_payload = json.loads(checks.read_text())
                    # Host-local paths and the scheduler config never cross the boundary.
                    ctx = dict(check_payload.get("ctx") or {})
                    # Scheduler-local checkout paths have no meaning on an independent host.
                    # The worker replaces these two with its clone; all other context (PR,
                    # branch, head and failed checks) is required by check plugins.
                    ctx.pop("exec_root", None)
                    ctx.pop("worktree", None)
                    payload["checks"] = {
                        "specs": list(check_payload.get("specs") or []),
                        "ctx": ctx,
                        "timeout": int(check_payload.get("timeout") or 600),
                        "config": {"worker_env": {
                            "pass": list(((check_payload.get("config") or {}).get("worker_env") or {}).get("pass") or []),
                            "config_files": dict(((check_payload.get("config") or {}).get("worker_env") or {}).get("config_files") or {}),
                        }},
                        **({"ci_rerun": True} if check_payload.get("ci_rerun") else {}),
                    }
                return JSONResponse(payload)
        return Response(status_code=204)

    @app.post("/api/runs/{run_id}/heartbeat")
    async def heartbeat(run_id: str, request: Request, authorization: str = Header(default="")):
        host = worker_host(authorization)
        body = await request.json()
        with hub.action_lock:
            run = claimed_run(run_id, host, str(body.get("lease_token") or ""))
            persist_host_facts(run, body.get("host_facts"))
            chunk = str(body.get("transcript") or "")
            if chunk:
                with (run.path / "stdout.json").open("a") as f:
                    f.write(chunk)
            now = dt.datetime.now(dt.UTC)
            lease_end = now + dt.timedelta(seconds=int(hub.store.config.get("workers.lease_seconds", 120)))
            deadline = execution_deadline(run)
            if deadline is not None:
                lease_end = min(lease_end, deadline)
            run.lease_expires_at = lease_end.isoformat()
            run.lease_updated_at = now.isoformat()
            run.save()
        return {"ok": True, "lease_expires_at": run.lease_expires_at}

    @app.post("/api/runs/{run_id}/finish")
    async def finish(run_id: str, request: Request, authorization: str = Header(default="")):
        host = worker_host(authorization)
        body = await request.json()
        with hub.action_lock:
            run = claimed_run(run_id, host, str(body.get("lease_token") or ""))
            run.pushed_head = str(body.get("pushed_head") or "")
            run.final_received_at = dt.datetime.now(dt.UTC).isoformat()
            final = str(body.get("final_text") or "")
            (run.path / "final.md").write_text(final)
            posted = {"result": body.get("result") or {}, "usage": body.get("usage") or {},
                      "cost_usd": body.get("cost_usd"), "final_text": final,
                      "error": str(body.get("error") or ""), "session_id": str(body.get("session_id") or "")}
            (run.path / "remote_result.json").write_text(json.dumps(posted))
            if run.mode == "check":
                (run.path / "checks.json").write_text(json.dumps((body.get("result") or {}).get("checks") or []))
            run.save()
            # Completion is written last: once visible, claim skips this run and the accepted
            # generation remains immutable until reap promotes its staging commit.
            (run.path / "exit_code").write_text(str(int(body.get("exit_code") or 0)))
        return {"ok": True}
