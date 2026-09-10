"""JSON endpoints."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
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
from ...workers import WorkerContactStore
from ...workers import snapshot as worker_snapshot
from ..common import Site


def register(app: FastAPI, site: Site) -> None:
    hub = site.hub

    def request_object(value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise HTTPException(422, "request body must be an object")
        return value

    async def worker_request(request: Request) -> dict[str, Any]:
        try:
            value = await request.json()
        except ValueError:
            raise HTTPException(422, "request body must be valid JSON") from None
        return request_object(value)

    def host_facts(value: Any) -> dict[str, Any] | None:
        """Validate the small, durable host-attribution record at the HTTP boundary."""
        if value is None:
            return None
        if not isinstance(value, dict):
            raise HTTPException(422, "host_facts must be an object")
        text_fields = ("profile_version", "bootstrap_version", "source_head", "provider_id",
                       "operation_id")
        number_fields = ("memory_available_bytes", "memory_total_bytes", "disk_free_bytes",
                         "cpu_count", "observed_at")
        facts: dict[str, Any] = {}
        schema_version = value.get("schema_version")
        if schema_version is not None:
            if schema_version != 1 or isinstance(schema_version, bool):
                raise HTTPException(422, "host_facts.schema_version must be 1")
            facts["schema_version"] = 1
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
        attestations = value.get("readiness_attestations")
        if attestations is not None:
            if not isinstance(attestations, dict):
                raise HTTPException(422, "host_facts.readiness_attestations must be an object")
            if schema_version is None:
                allowed = {"bootstrap_manifest", "authenticated_registration", "repository_ci"}
                if (set(attestations) - allowed
                        or any(item is not True and item is not False
                               for item in attestations.values())):
                    raise HTTPException(422, "legacy host readiness attestations must contain known booleans")
                facts["readiness_attestations"] = attestations
            else:
                shapes = {
                    "bootstrap_manifest": {"ok", "source_head", "profile_version",
                                           "bootstrap_version", "bootstrap_sha256",
                                           "installed_distribution", "direct_url_commit"},
                    "authenticated_registration": {"ok", "method"},
                    "repository_access": {"ok", "repository", "method"},
                    "ci_provider_read": {"ok", "provider", "repository", "source_head",
                                         "authenticated"},
                }
                if (set(attestations) - set(shapes) or any(not isinstance(item, dict)
                                                           for item in attestations.values())):
                    raise HTTPException(422, "versioned host readiness attestations contain unknown records")
                if (len(json.dumps(attestations)) > 4096
                        or any(set(item) - shapes[name]
                               or (name != "authenticated_registration" and item.get("ok") is not True)
                               or (name == "authenticated_registration"
                                   and item.get("ok") is not True and item.get("ok") is not False)
                               for name, item in attestations.items())
                        or any(not isinstance(field, (str, bool)) or len(str(field)) > 256
                               for item in attestations.values() for key, field in item.items()
                               if key != "ok")):
                    raise HTTPException(422, "versioned host readiness attestations must be bounded successes")
                facts["readiness_attestations"] = {name: dict(item)
                                                    for name, item in attestations.items()}
        source_bootstrap = value.get("source_bootstrap")
        if source_bootstrap is not None:
            expected = {"source_head", "bootstrap_sha256", "operation_id"}
            if (schema_version != 1 or not isinstance(source_bootstrap, dict)
                    or set(source_bootstrap) != expected
                    or any(not isinstance(source_bootstrap.get(name), str)
                           for name in expected)
                    or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}",
                                        source_bootstrap["source_head"])
                    or not re.fullmatch(r"[0-9a-f]{64}", source_bootstrap["bootstrap_sha256"])
                    or not source_bootstrap["operation_id"]
                    or len(source_bootstrap["operation_id"]) > 128):
                raise HTTPException(422, "host_facts.source_bootstrap is invalid")
            facts["source_bootstrap"] = source_bootstrap
            if (facts.get("source_head") != source_bootstrap["source_head"]
                    or facts.get("operation_id") != source_bootstrap["operation_id"]):
                raise HTTPException(422, "host_facts source/bootstrap identity is inconsistent")
        return facts

    def bound_host_facts(value: Any, host: dict[str, Any]) -> dict[str, Any] | None:
        facts = host_facts(value)
        if facts is not None:
            for name in ("provider_id", "operation_id", "source_head", "profile_version",
                         "bootstrap_version"):
                expected = host.get(name)
                if expected and facts.get(name) != expected:
                    raise HTTPException(409, f"host_facts.{name} does not match authenticated host")
        return facts

    def persist_host_facts(run: Any, value: Any, host: dict[str, Any]) -> None:
        facts = bound_host_facts(value, host)
        if facts is not None:
            if run.host != host.get("name"):
                raise HTTPException(409, "host facts do not belong to the leased host")
            (run.path / "host_facts.json").write_text(json.dumps(facts))

    def record_worker_contact(host: dict[str, Any], body: dict[str, Any], *, outcome: str) -> None:
        facts = host_facts(body.get("host_facts"))
        WorkerContactStore(hub.store.config.garden_dir).record(
            str(host.get("name") or ""),
            capacity=min(max(1, int(body.get("capacity") or 1)), int(host.get("max_parallel") or 1)),
            harnesses=[str(value) for value in (body.get("harnesses") or [])],
            tiers=[str(value) for value in (body.get("tiers") or [])],
            facts=facts,
            outcome=outcome,
        )

    def worker_host(authorization: str) -> dict[str, Any]:
        from ...hosts.registry import authenticate_worker, worker_configuration

        if not authorization.startswith("Bearer "):
            raise HTTPException(401, "bearer token required")
        token = authorization[7:]
        try:
            host = authenticate_worker(worker_configuration(hub.store.config), token)
        except (OSError, TypeError, ValueError):
            raise HTTPException(503, "worker enrollment registry is unavailable") from None
        if host is not None:
            return host
        raise HTTPException(403, "unknown worker token")

    def leased(run: Any) -> bool:
        return bool(run.lease_expires_at and run.lease_expires_at > dt.datetime.now(dt.UTC).isoformat())

    def execution_timeout_minutes(run: Any) -> float:
        """Return the snapshotted execution budget, keeping checks independently bounded."""
        snapshot = run.env_snapshot or {}
        if "execution_timeout_minutes" in snapshot:
            return float(snapshot["execution_timeout_minutes"] or 0)
        elif run.mode == "check":
            return 0
        product = str(snapshot.get("product") or "")
        return (hub.store.config.product_timeout_minutes(product) if product
                else float(hub.store.config.get("timeout_minutes", 90) or 0))

    def execution_deadline(run: Any) -> dt.datetime | None:
        """Fixed controller deadline; heartbeats renew liveness, never execution budget."""
        started = run.execution_started_at or run.claimed_at
        timeout = execution_timeout_minutes(run)
        if not started or not timeout:
            return None
        return dt.datetime.fromisoformat(started) + dt.timedelta(minutes=timeout + 5)

    def recovering(run: Any) -> bool:
        deadline = run.recovery_expires_at
        if not deadline and run.lease_expires_at:
            # Runs claimed by the immediately previous controller version have no
            # recovery field. Derive it from their durable lease so deploying the fix
            # does not itself strand those active subprocesses.
            deadline = (dt.datetime.fromisoformat(run.lease_expires_at) + dt.timedelta(
                seconds=int(hub.store.config.get("workers.recovery_seconds", 300))
            )).isoformat()
        return bool(deadline and deadline > dt.datetime.now(dt.UTC).isoformat())

    def renew(run: Any, now: dt.datetime | None = None) -> None:
        now = now or dt.datetime.now(dt.UTC)
        lease_seconds = int(hub.store.config.get("workers.lease_seconds", 120))
        recovery_seconds = int(hub.store.config.get("workers.recovery_seconds", 300))
        run.lease_expires_at = (now + dt.timedelta(seconds=lease_seconds)).isoformat()
        run.recovery_expires_at = (
            now + dt.timedelta(seconds=lease_seconds + recovery_seconds)
        ).isoformat()

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
        if not leased(run) and not recovering(run):
            raise HTTPException(409, "run lease recovery deadline has expired")
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

    @app.get("/api/workers")
    def api_workers():
        """Cached worker presence and occupancy; never probes infrastructure on request."""
        fresh = hub.fresh()
        from ...runs import RunStore

        return JSONResponse(worker_snapshot(fresh.config, RunStore(fresh.config.garden_dir)))

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
        """Atomically lease, or replay, one compatible run for a configured host.

        ``claim_request_id`` identifies one idle poll. Its response remains replayable only
        for the lifetime of the allocated lease generation (ordinary lease plus recovery
        grace); it cannot be reused after expiry, reclaim, completion, or by another host.
        """
        host_cfg = worker_host(authorization)
        body = await worker_request(request)
        if str(body.get("host") or "") != str(host_cfg.get("name") or ""):
            raise HTTPException(403, "token does not belong to this host")
        for name in ("harnesses", "tiers"):
            offered_values = body.get(name) or []
            if (not isinstance(offered_values, list)
                    or any(not isinstance(item, str) for item in offered_values)):
                raise HTTPException(422, f"{name} must be a list of strings")
        requested_capacity = body.get("capacity", 1)
        if (isinstance(requested_capacity, bool) or not isinstance(requested_capacity, int)
                or not 1 <= requested_capacity <= 1024):
            raise HTTPException(422, "capacity must be an integer between 1 and 1024")
        bound_host_facts(body.get("host_facts"), host_cfg)
        request_id = body.get("claim_request_id")
        if request_id is None:
            # Compatibility for older independent workers. New workers supply this value
            # so an ambiguous response can be replayed rather than allocating twice.
            request_id = secrets.token_urlsafe(24)
        if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", request_id):
            raise HTTPException(422, "claim_request_id must be 16-128 URL-safe characters")
        offered = set(body.get("harnesses") or [])
        tiers = set(body.get("tiers") or [])
        capacity = min(requested_capacity, int(host_cfg.get("max_parallel") or 1))
        in_place = bool(host_cfg.get("in_place"))
        with hub.action_lock:
            from ...runner.base import pass_env_patterns
            from ...runs import RunStore

            # An authenticated empty claim is independent liveness evidence. Record it
            # before admission so every 204 path keeps an idle worker visible.
            record_worker_contact(host_cfg, body, outcome="polling")

            all_runs = RunStore(hub.store.config.garden_dir).all_runs()
            replay = next((r for r in all_runs if r.runner == "remote" and (
                r.claim_request_id == request_id
                or any(item.get("claim_request_id") == request_id for item in r.claim_history)
            )), None)
            if replay is not None:
                response_token = str(replay.claim_response.get("lease_token") or "")
                if replay.claim_request_id != request_id or replay.host != body["host"] \
                        or not response_token:
                    raise HTTPException(409, "claim request identity cannot be replayed")
                claimed_run(replay.run_id, host_cfg, response_token)
                return JSONResponse(replay.claim_response)
            runs = RunStore(hub.store.config.garden_dir).active()
            owned = [r for r in runs if r.runner == "remote" and r.status == "running"
                     and r.host == body["host"] and (leased(r) or recovering(r))
                     and not r.process_finished()]
            if in_place and owned:
                return Response(status_code=204)
            used = sum(int((r.env_snapshot or {}).get("resource_weight") or 1) for r in owned)
            if not in_place and used >= capacity:
                return Response(status_code=204)
            now = dt.datetime.now(dt.UTC)
            for run in runs:
                if run.runner != "remote" or run.status != "running" or run.process_finished():
                    continue
                if run.host and (leased(run) or recovering(run)):
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
                weight = int((run.env_snapshot or {}).get("resource_weight") or 1)
                if not in_place and used + weight > capacity:
                    # First-fit admission lets a cheap product use remaining capacity while
                    # a heavier queued product waits. Bound those bypasses so a steady stream
                    # of cheap claims eventually reserves the next opening for old heavy work.
                    if weight <= capacity:
                        bypasses = int((run.env_snapshot or {}).get("admission_bypasses") or 0)
                        max_bypasses = max(0, int(hub.store.config.get("resources.max_bypasses", 3)))
                        if bypasses >= max_bypasses:
                            return Response(status_code=204)
                        run.env_snapshot["admission_bypasses"] = bypasses + 1
                        run.save()
                    continue
                run.host = str(body["host"])
                claim_time = now.isoformat()
                if not run.claimed_at:
                    run.claimed_at = claim_time
                if not run.execution_started_at:
                    # A pre-execution_started_at record may already have been claimed.
                    # Preserve that first execution boundary across reclaim instead of
                    # resetting its execution deadline to the newest generation.
                    run.execution_started_at = run.claimed_at
                renew(run, now)
                run.lease_updated_at = claim_time
                run.lease_token = secrets.token_urlsafe(32)
                run.claim_request_id = request_id
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
                                          "claim_request_id": request_id,
                                          "lease_token_sha256": hashlib.sha256(run.lease_token.encode()).hexdigest(),
                                          "pushed_ref": run.pushed_ref})
                if run.source_head:
                    # The scheduler recorded this immutable base-probe source before the
                    # lease. Do not replace it with the task branch's moving head.
                    run.start_head = run.source_head
                else:
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
                setup = hub.store.config.product_setup(product) or {}
                payload: dict[str, Any] = {
                    "id": run.run_id, "task_id": run.task_id, "mode": run.mode,
                    "lease_token": run.lease_token,
                    "heartbeat_seconds": max(0.05, int(hub.store.config.get("workers.lease_seconds", 120)) / 3),
                    "execution_deadline_at": execution_deadline(run).isoformat()
                    if execution_deadline(run) is not None else "",
                    "recovery_seconds": int(hub.store.config.get("workers.recovery_seconds", 300)),
                    "recovery_window_seconds": (
                        int(hub.store.config.get("workers.lease_seconds", 120))
                        + int(hub.store.config.get("workers.recovery_seconds", 300))
                    ),
                    "brief": (run.path / "brief.md").read_text() if (run.path / "brief.md").exists() else "",
                    "branch": run.branch, "base": run.base, "source_head": run.source_head,
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
                    "execution_timeout_minutes": execution_timeout_minutes(run),
                    "resource_weight": weight,
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
                run.claim_response = payload
                persist_host_facts(run, body.get("host_facts"), host_cfg)
                run.save()
                record_worker_contact(host_cfg, body, outcome="claimed")
                return JSONResponse(payload)
        return Response(status_code=204)

    @app.post("/api/runs/{run_id}/heartbeat")
    async def heartbeat(run_id: str, request: Request, authorization: str = Header(default="")):
        host = worker_host(authorization)
        body = await worker_request(request)
        bound_host_facts(body.get("host_facts"), host)
        transcript_value = body.get("transcript", "")
        if not isinstance(transcript_value, str):
            raise HTTPException(422, "transcript must be a string")
        offset_value = body.get("transcript_offset")
        if (offset_value is not None
                and (isinstance(offset_value, bool) or not isinstance(offset_value, int)
                     or offset_value < 0)):
            raise HTTPException(422, "transcript_offset must be a non-negative integer")
        with hub.action_lock:
            run = claimed_run(run_id, host, str(body.get("lease_token") or ""))
            chunk = transcript_value
            record_worker_contact(host, body, outcome="heartbeat")
            if chunk:
                transcript = run.path / "stdout.json"
                current = transcript.stat().st_size if transcript.exists() else 0
                offset = offset_value
                if offset is None:  # compatibility with workers deployed before CG-428
                    offset = current
                encoded = chunk.encode()
                if offset < current:
                    with transcript.open("rb") as f:
                        f.seek(offset)
                        if f.read(len(encoded)) != encoded:
                            raise HTTPException(409, "transcript upload conflicts with durable output")
                elif offset > current:
                    raise HTTPException(409, f"transcript offset {offset} is ahead of durable offset {current}")
                else:
                    with transcript.open("ab") as f:
                        f.write(encoded)
            persist_host_facts(run, body.get("host_facts"), host)
            renew(run)
            now = dt.datetime.now(dt.UTC)
            deadline = execution_deadline(run)
            if deadline is not None:
                run.lease_expires_at = min(
                    dt.datetime.fromisoformat(run.lease_expires_at), deadline
                ).isoformat()
            run.lease_updated_at = now.isoformat()
            run.save()
        transcript = run.path / "stdout.json"
        return {"ok": True, "lease_expires_at": run.lease_expires_at,
                "transcript_offset": transcript.stat().st_size if transcript.exists() else 0}

    @app.post("/api/runs/{run_id}/finish")
    async def finish(run_id: str, request: Request, authorization: str = Header(default="")):
        host = worker_host(authorization)
        body = await worker_request(request)
        result = body.get("result", {})
        usage = body.get("usage", {})
        if not isinstance(result, dict):
            raise HTTPException(422, "result must be an object")
        if not isinstance(usage, dict):
            raise HTTPException(422, "usage must be an object")
        checks = result.get("checks", [])
        if (not isinstance(checks, list)
                or any(not isinstance(item, dict) for item in checks)):
            raise HTTPException(422, "result.checks must be a list of objects")
        exit_code = body.get("exit_code", 0)
        if isinstance(exit_code, bool) or not isinstance(exit_code, int):
            raise HTTPException(422, "exit_code must be an integer")
        bound_host_facts(body.get("host_facts"), host)
        with hub.action_lock:
            run = run_for(run_id)
            token = str(body.get("lease_token") or "")
            final = str(body.get("final_text") or "")
            posted = {"result": result, "usage": usage,
                      "cost_usd": body.get("cost_usd"), "final_text": final,
                      "error": str(body.get("error") or ""),
                      "session_id": str(body.get("session_id") or ""),
                      # Host-side materialisation failures happen before a harness can emit
                      # its normal result. Preserve the existing bounded environment-error
                      # contract so reap can retry without blaming source or spending a round.
                      "env_error": bool(body.get("env_error", False)),
                      "env_kind": str(body.get("env_kind") or "")}
            if run.host != host.get("name") or not token or not secrets.compare_digest(run.lease_token, token):
                raise HTTPException(409, "run lease has been replaced")
            if run.process_finished():
                result_path = run.path / "remote_result.json"
                prior = json.loads(result_path.read_text()) if result_path.exists() else {}
                same = (run.pushed_head == str(body.get("pushed_head") or "")
                        and run.read_exit_code() == exit_code
                        and prior == posted)
                if not same:
                    raise HTTPException(409, "completed run result is immutable")
                return {"ok": True, "already_finished": True}
            if run.status != "running":
                raise HTTPException(409, "run lease has been revoked")
            run = claimed_run(run_id, host, token)
            persist_host_facts(run, body.get("host_facts"), host)
            run.pushed_head = str(body.get("pushed_head") or "")
            run.final_received_at = dt.datetime.now(dt.UTC).isoformat()
            (run.path / "final.md").write_text(final)
            (run.path / "remote_result.json").write_text(json.dumps(posted))
            if run.mode == "check":
                if posted["env_error"] and posted["env_kind"] == "materialization":
                    checks = [{"name": "checks", "status": "error",
                               "summary": "check execution did not complete",
                               "details": posted["error"]}]
                (run.path / "checks.json").write_text(json.dumps(checks))
            run.save()
            # Completion is written last: once visible, claim skips this run and the accepted
            # generation remains immutable until reap promotes its staging commit.
            (run.path / "exit_code").write_text(str(exit_code))
        return {"ok": True}
