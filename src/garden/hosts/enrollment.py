"""Production, resumable per-host enrollment for a durable scale operation.

Secret values live only in mode-0600 controller files and the scoped bootstrap secret.
Public operation state contains identities and hashes, never bearer tokens or private keys.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import secrets
import subprocess
import tempfile
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .config import pool_from_dict
from .locking import file_lock
from .models import CONTRACT_VERSION, PoolDeclaration, host_operation_id
from .scale import Enrollment


def _atomic(path: Path, value: Any, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.parent.stat().st_mode & 0o077:
        raise RuntimeError(f"private enrollment directory is not mode 0700: {path.parent}")
    temporary = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    with os.fdopen(descriptor, "w") as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.chmod(mode)
    os.replace(temporary, path)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class ProductionEnrollmentResolver:
    """Create only identities owned by one admitted production scale operation.

    API clients are injected so lifecycle tests cannot contact production.  A production
    adapter supplies clients with the small methods used below; no administrator client is
    accepted and no fallback identity is copied between hosts.
    """

    def __init__(
        self,
        root: Path,
        config: dict,
        pool: PoolDeclaration,
        operation_path: Path,
        *,
        secrets_client=None,
        tailscale_client=None,
        github_client=None,
        keygen=None,
        now=lambda: dt.datetime.now(dt.UTC),
    ):
        self.root, self.config, self.pool = root, config, pool
        self.operation_path = operation_path
        self.secrets, self.tailscale, self.github = secrets_client, tailscale_client, github_client
        self.keygen, self.now = keygen or self._ssh_keygen, now
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.root.stat().st_mode & 0o077:
            raise RuntimeError("production enrollment root must be mode 0700")

    def _operation(self) -> dict:
        value = json.loads(self.operation_path.read_text())
        admitted = value.get("admitted_declaration") or {}
        parsed = pool_from_dict({"contract_version": CONTRACT_VERSION, **admitted})
        if asdict(parsed) != asdict(self.pool) or value.get("desired") is None:
            raise RuntimeError("enrollment does not match the admitted scale operation")
        profile = admitted.get("profile") or {}
        options = {
            **(admitted.get("provider_options") or {}),
            **(profile.get("provider_options") or {}),
        }
        if not profile.get("endpoint") or not profile.get("source_head"):
            raise RuntimeError("admitted production profile needs endpoint and exact source_head")
        bootstrap_sha256 = str(options.get("bootstrap_sha256") or "")
        if len(bootstrap_sha256) != 64 or any(
            c not in "0123456789abcdef" for c in bootstrap_sha256
        ):
            raise RuntimeError("admitted production profile needs exact bootstrap_sha256")
        deadline = dt.datetime.fromisoformat(value["deadline"].replace("Z", "+00:00"))
        if deadline.tzinfo is None or deadline <= self.now():
            raise RuntimeError("admitted scale operation has expired")
        return value

    def _paths(self, host_id: str) -> tuple[Path, Path]:
        if host_id not in {f"{self.pool.name}-{slot}" for slot in range(self.pool.desired)}:
            raise RuntimeError("host is outside the admitted pool slots")
        return self.root / f"{host_id}.json", self.root / f".{host_id}.private.json"

    def _model(self, host_id: str, required_until: dt.datetime) -> tuple[Path, str, str]:
        files = self.config.get("model_auth_files") or {}
        labels = self.config.get("model_identities") or {}
        expiries = self.config.get("model_expires_at") or {}
        path, label, expiry = (
            Path(files.get(host_id, "")),
            str(labels.get(host_id, "")),
            str(expiries.get(host_id, "")),
        )
        if not path.is_file() or not label:
            raise RuntimeError(f"{host_id} needs dedicated model authentication owner handoff")
        selected = [str(Path(v).resolve()) for v in files.values() if v]
        if len(selected) != len(set(selected)) or len([v for v in labels.values() if v]) != len(
            set(v for v in labels.values() if v)
        ):
            raise RuntimeError("model identities and auth files must be unique per host")
        if expiry:
            parsed = dt.datetime.fromisoformat(expiry.replace("Z", "+00:00"))
            if parsed.tzinfo is None or parsed < required_until:
                raise RuntimeError(
                    f"{host_id} dedicated model identity must cover the operation deadline"
                )
        if path.stat().st_mode & 0o077:
            raise RuntimeError("model authentication file must be mode 0600")
        return path, label, expiry

    def resolve(self, host_id: str) -> Enrollment:
        """Read identity metadata only; never create credentials or call an API."""
        public, _ = self._paths(host_id)
        if not public.exists():
            return Enrollment()
        row = json.loads(public.read_text())
        return Enrollment(**{name: row.get(name, "") for name in Enrollment.__dataclass_fields__})

    def setup_missing(self, host_id: str) -> tuple[str, ...]:
        """Report local production inputs without minting credentials or calling APIs."""
        self._paths(host_id)
        operation_deadline = dt.datetime.fromisoformat(
            self._operation()["deadline"].replace("Z", "+00:00")
        )
        missing = []
        model_file = Path((self.config.get("model_auth_files") or {}).get(host_id, ""))
        if not model_file.is_file() or model_file.stat().st_mode & 0o077:
            missing.append(f"{host_id} dedicated mode-0600 model auth file")
        if not (self.config.get("model_identities") or {}).get(host_id):
            missing.append(f"{host_id} dedicated model identity label")
        expiry = str((self.config.get("model_expires_at") or {}).get(host_id, ""))
        if expiry:
            try:
                parsed = dt.datetime.fromisoformat(expiry.replace("Z", "+00:00"))
            except ValueError:
                parsed = None
            if parsed is None or parsed.tzinfo is None or parsed < operation_deadline:
                missing.append(f"{host_id} model identity covering the operation deadline")
        for key, label in (
            ("tailscale_oauth_file", "private Tailscale OAuth credential file"),
            ("github_token_file", "private GitHub access-token file"),
        ):
            path = Path(self.config.get(key, ""))
            if not path.is_file() or path.stat().st_mode & 0o077:
                missing.append(label)
        for key, label in (
            ("aws_profile", "scoped AWS profile"),
            ("aws_region", "AWS region"),
            ("aws_account_id", "AWS account ID"),
        ):
            if not self.config.get(key):
                missing.append(label)
        return tuple(missing)

    def ensure(self, host_id: str, secret_ref: str) -> Enrollment:
        with file_lock(self.root / f".{host_id}.lock"):
            return self._ensure_locked(host_id, secret_ref)

    def _ensure_locked(self, host_id: str, secret_ref: str) -> Enrollment:
        operation = self._operation()
        public, private = self._paths(host_id)
        allowed_name = "context-garden/phase05/renew-"
        if not (secret_ref.startswith(allowed_name) or ":secret:" + allowed_name in secret_ref):
            raise RuntimeError("bootstrap secret reference exceeds the durable namespace")
        current = self.resolve(host_id)
        operation_deadline = dt.datetime.fromisoformat(
            operation["deadline"].replace("Z", "+00:00")
        )
        model_path, model_identity, model_expiry = self._model(host_id, operation_deadline)
        model_auth_bytes = model_path.read_bytes()
        model_auth_sha256 = hashlib.sha256(model_auth_bytes).hexdigest()
        saved = json.loads(private.read_text()) if private.exists() else {}
        tailnet_expiry = saved.get("steps", {}).get("tailnet", {}).get("expires_at", "")
        validity_floor = self.now() + dt.timedelta(
            seconds=int(self.config.get("enrollment_min_validity_seconds", 300))
        )
        try:
            tailnet_valid = (
                bool(tailnet_expiry)
                and dt.datetime.fromisoformat(tailnet_expiry.replace("Z", "+00:00"))
                > validity_floor
            )
        except ValueError:
            tailnet_valid = False
        published_snapshot = saved.get("published_enrollment")
        public_matches_private = bool(published_snapshot) and current.__dict__ == published_snapshot
        saved_steps = saved.get("steps", {})
        private_generation_matches = (
            saved.get("published_tailnet_id")
            == saved_steps.get("tailnet", {}).get("id")
            and saved.get("published_bootstrap_client_token")
            == saved_steps.get("bootstrap", {}).get("client_token")
        )
        model_input_matches = (
            saved.get("published_model_auth_sha256") == model_auth_sha256
            and saved.get("published_model_identity") == model_identity
            and saved.get("published_model_expires_at") == model_expiry
        )
        publication_pending = (
            bool(published_snapshot)
            and current.__dict__ != published_snapshot
            and not saved.get("aws_refresh")
            and private_generation_matches
            and model_input_matches
        )
        private_enrollment = Enrollment(**published_snapshot) if published_snapshot else Enrollment()
        baseline_enrollment = current if current.secret_ref else private_enrollment
        refresh = bool(baseline_enrollment.secret_ref) and not publication_pending
        if refresh and baseline_enrollment.secret_ref != secret_ref:
            raise RuntimeError("existing enrollment uses a different bootstrap secret")
        if (
            not current.missing(self.now())
            and current.secret_ref == secret_ref
            and tailnet_valid
            and public_matches_private
            and private_generation_matches
            and model_input_matches
            and not saved.get("aws_refresh")
        ):
            return current
        if refresh:
            stable = (
                baseline_enrollment.repository_identity,
                baseline_enrollment.tailnet_identity,
                baseline_enrollment.controller_identity,
                baseline_enrollment.model_identity,
            )
            if not all(stable):
                raise RuntimeError("incomplete published enrollment cannot be refreshed")
        if not all((self.secrets, self.tailscale, self.github)):
            raise RuntimeError("production enrollment API clients are required")
        state = (
            json.loads(private.read_text())
            if private.exists()
            else {
                "version": 2,
                "host_id": host_id,
                "operation_id": operation["operation_id"],
                "secret_ref": secret_ref,
                "steps": {},
                "pending_cleanup": [],
            }
        )
        if (
            state["host_id"] != host_id
            or state["operation_id"] != operation["operation_id"]
            or state["secret_ref"] != secret_ref
        ):
            raise RuntimeError("existing enrollment belongs to another operation or secret")
        steps = state["steps"]
        # The state identity must exist before any external side effect.  This also makes
        # an interrupted first invocation distinguishable from an unrelated credential.
        if not private.exists():
            _atomic(private, state)

        if refresh and not tailnet_valid and steps.get("tailnet"):
            old_tailnet = steps.get("tailnet")
            found = self.tailscale.find_key(old_tailnet["description"])
            if found:
                self.tailscale.delete_key(found["id"])
            if self.tailscale.find_key(old_tailnet["description"]):
                raise RuntimeError("expired tailnet key revocation is not confirmed")
            state.setdefault("retired_tailnet", []).append(
                {key: old_tailnet[key] for key in ("id", "description", "expires_at")}
            )
            steps.pop("tailnet")
            previous_attempt = steps.pop("tailnet_attempt", old_tailnet)
            generation = int(previous_attempt.get("generation", 0)) + 1
            steps["tailnet_attempt"] = {
                "generation": generation,
                "description": f"Garden {operation['operation_id']} {host_id} attempt {generation}",
                "tag": "tag:garden-worker",
                "reusable": False,
            }
            _atomic(private, state)
        elif refresh and not tailnet_valid and not steps.get("tailnet_attempt"):
            raise RuntimeError("published tailnet enrollment lacks its private identity journal")

        if "repository" not in steps:
            self._validate_repository_name(self.config["github_repo"])
            title = f"garden {operation['operation_id']} {host_id}"
            intent = steps.get("repository_intent")
            if intent is None:
                private_key, public_key = self.keygen(host_id)
                intent = {
                    "repo": self.config["github_repo"],
                    "title": title,
                    "public_key": public_key,
                    "public_sha256": _digest(public_key),
                    "private_key": private_key,
                }
                steps["repository_intent"] = intent
                _atomic(private, state)
            found = self.github.find_deploy_key(
                intent["repo"], intent["title"], intent["public_key"]
            )
            created = found or self.github.create_deploy_key(
                intent["repo"], intent["title"], intent["public_key"], read_only=False
            )
            steps["repository"] = {
                "id": str(created["id"]),
                "title": intent["title"],
                "public_sha256": intent["public_sha256"],
                "private_key": intent["private_key"],
            }
            _atomic(private, state)
        if "tailnet" not in steps:
            attempt = steps.get("tailnet_attempt")
            if attempt is None:
                generation = 1
                attempt = {
                    "generation": generation,
                    "description": f"Garden {operation['operation_id']} {host_id} attempt {generation}",
                    "tag": "tag:garden-worker",
                    "reusable": False,
                }
                steps["tailnet_attempt"] = attempt
                _atomic(private, state)
            found = self.tailscale.find_key(attempt["description"])
            if found and not found.get("key"):
                # Auth-key values are returned exactly once.  If creation committed but
                # its response was lost, revoke that unusable key before minting another.
                self.tailscale.delete_key(found["id"])
                if self.tailscale.find_key(attempt["description"]):
                    raise RuntimeError("lost-response tailnet key revocation is not confirmed")
                generation = int(attempt["generation"]) + 1
                attempt = {
                    "generation": generation,
                    "description": f"Garden {operation['operation_id']} {host_id} attempt {generation}",
                    "tag": "tag:garden-worker",
                    "reusable": False,
                }
                steps["tailnet_attempt"] = attempt
                _atomic(private, state)
                found = None
            created = found or self.tailscale.create_key(
                description=attempt["description"],
                tag="tag:garden-worker",
                reusable=False,
                ephemeral=False,
                preauthorized=True,
                expiry_seconds=int(self.config.get("tailscale_expiry_seconds", 3600)),
            )
            expires_at = self._validate_tailnet(created, attempt)
            steps["tailnet"] = {
                "id": str(created["id"]),
                "key": created["key"],
                "expires_at": expires_at.isoformat(),
                **attempt,
            }
            _atomic(private, state)
        if "controller" not in steps:
            token = secrets.token_urlsafe(32)
            steps["controller"] = {"token": token, "sha256": _digest(token)}
            _atomic(private, state)
        if "bootstrap" not in steps or refresh:
            auth = json.loads(model_auth_bytes)
            admitted = operation["admitted_declaration"]
            admitted_profile = admitted["profile"]
            admitted_options = {
                **(admitted.get("provider_options") or {}),
                **(admitted_profile.get("provider_options") or {}),
            }
            slot = int(host_id.rsplit("-", 1)[1])
            operation_seed = operation["operation_id"] if operation.get("pool_identity") else ""
            operation_identity = host_operation_id(self.pool, slot, operation_seed)
            envelope = {
                "contract_version": CONTRACT_VERSION,
                "host": host_id,
                "operation_id": operation_identity,
                "endpoint": admitted_profile.get("endpoint") or self.config["endpoint"],
                "profile_version": admitted_profile["version"],
                "bootstrap_version": admitted_profile["bootstrap_version"],
                "bootstrap_sha256": admitted_options.get("bootstrap_sha256", ""),
                "source_head": admitted_profile.get("source_head") or operation["source_head"],
                "deadline_utc": operation["deadline"],
                "cpu": admitted_profile["cpu"],
                "memory_mib": admitted_profile["memory_mib"],
                "disk_gib": admitted_profile["disk_gib"],
                "worker_token": steps["controller"]["token"],
                "harnesses": list(self.config.get("harnesses") or ["codex"]),
                "tiers": list(self.config.get("tiers") or ["easy", "medium", "hard"]),
                "codex_auth": auth,
                "git_ssh_private_key": steps["repository"]["private_key"],
                "git_repo": steps["repository_intent"]["repo"],
                "tailscale_auth_key": steps["tailnet"]["key"],
            }
            if self.config.get("repository_private"):
                ci_path = Path((self.config.get("ci_read_token_files") or {}).get(host_id, ""))
                if not ci_path.is_file() or ci_path.stat().st_mode & 0o077:
                    raise RuntimeError(f"{host_id} needs a private mode-0600 CI read credential")
                envelope["github_ci_read_token"] = ci_path.read_text().strip()
            tags = dict(self.config["secret_tags"])
            expected = {
                "ManagedBy": "context-garden",
                "Pool": "phase05",
                "Purpose": "worker-bootstrap",
            }
            expected_operation_tag = "renew-six-" + operation["operation_id"]
            if (
                any(tags.get(k) != v for k, v in expected.items())
                or (
                    tags.get("OperationId") is not None
                    and tags.get("OperationId") != expected_operation_tag
                )
            ):
                raise RuntimeError("AWS bootstrap secret tags exceed the durable authority")
            tags["OperationId"] = expected_operation_tag
            requested_provenance = {
                "model_auth_sha256": model_auth_sha256,
                "model_identity": model_identity,
                "model_expires_at": model_expiry,
            }
            if refresh:
                previous = steps["bootstrap"]
                if previous.get("tags") != tags:
                    raise RuntimeError("saved bootstrap ownership differs from admitted tags")
                refresh_state = state.get("aws_refresh")
                if refresh_state is None:
                    refresh_state = {
                        "previous_client_token": previous["client_token"],
                        "client_token": str(uuid.uuid4()),
                        "envelope": envelope,
                        "provenance": requested_provenance,
                    }
                    state["aws_refresh"] = refresh_state
                    _atomic(private, state)
                envelope = refresh_state["envelope"]
                version_provenance = refresh_state["provenance"]
                described = self.secrets.describe(previous["arn"])
                if (
                    not described
                    or described.get("tags") != tags
                    or described.get("deleted")
                ):
                    raise RuntimeError("refusing to refresh an unrelated bootstrap secret")
                current_token = described.get("client_token")
                token = refresh_state["client_token"]
                if current_token == refresh_state["previous_client_token"]:
                    put = getattr(self.secrets, "put", None)
                    if put is None:
                        raise RuntimeError(
                            "bootstrap refresh requires scoped secretsmanager:PutSecretValue"
                        )
                    put(previous["arn"], token, envelope, tags)
                    described = self.secrets.describe(previous["arn"])
                    current_token = (described or {}).get("client_token")
                if current_token != token:
                    raise RuntimeError("bootstrap refresh has an unknown AWSCURRENT version")
                created = described
                state.pop("aws_refresh", None)
            else:
                create_state = state.get("aws_create")
                if create_state is None:
                    create_state = {
                        "client_token": state.setdefault("aws_client_token", str(uuid.uuid4())),
                        "envelope": envelope,
                        "provenance": requested_provenance,
                    }
                    state["aws_create"] = create_state
                    # Token and its exact payload must both survive a lost create response.
                    _atomic(private, state)
                token = create_state["client_token"]
                envelope = create_state["envelope"]
                version_provenance = create_state["provenance"]
                described = self.secrets.describe(secret_ref)
                if described:
                    if (
                        described.get("tags") != tags
                        or described.get("client_token") != token
                        or described.get("deleted")
                    ):
                        raise RuntimeError("refusing unrelated existing bootstrap secret")
                    created = described
                else:
                    created = self.secrets.create(secret_ref, token, envelope, tags)
                state.pop("aws_create", None)
            if created.get("tags") != tags:
                raise RuntimeError("created bootstrap secret ownership tags differ")
            steps["bootstrap"] = {
                "arn": created["arn"],
                "client_token": token,
                "tags": tags,
                "envelope_sha256": _digest(json.dumps(envelope, sort_keys=True)),
            }
            confirmed_enrollment = Enrollment(
                secret_ref,
                version_provenance["model_identity"],
                f"github-deploy-key:{steps['repository']['id']}",
                f"tailscale-auth-key:{steps['tailnet']['id']}",
                "controller-token-sha256:" + steps["controller"]["sha256"],
                version_provenance["model_expires_at"],
            )
            state["published_enrollment"] = confirmed_enrollment.__dict__
            state["published_tailnet_id"] = steps["tailnet"]["id"]
            state["published_bootstrap_client_token"] = token
            state["published_model_auth_sha256"] = version_provenance["model_auth_sha256"]
            state["published_model_identity"] = version_provenance["model_identity"]
            state["published_model_expires_at"] = version_provenance["model_expires_at"]
            # Keep source credentials in the mode-0600 journal until confirmed revocation.
            _atomic(private, state)
            if version_provenance != requested_provenance:
                raise RuntimeError(
                    "bootstrap version recovered for prior model input; retry to refresh current input"
                )

        registry = self.root / "controller-hosts.json"
        entry = {
            "name": host_id,
            "token_sha256": steps["controller"]["sha256"],
            "harnesses": list(self.config.get("harnesses") or ["codex"]),
            "tiers": list(self.config.get("tiers") or ["easy", "medium", "hard"]),
            "max_parallel": 1,
            "deadline_utc": operation["deadline"],
            "profile_version": operation["admitted_declaration"]["profile"]["version"],
            "source_head": operation["admitted_declaration"]["profile"].get("source_head")
            or operation["source_head"],
            "bootstrap_version": operation["admitted_declaration"]["profile"]["bootstrap_version"],
            "operation_id": host_operation_id(
                self.pool,
                int(host_id.rsplit("-", 1)[1]),
                operation["operation_id"] if operation.get("pool_identity") else "",
            ),
        }
        with file_lock(self.root / ".registry.lock"):
            existing = json.loads(registry.read_text()) if registry.exists() else {"hosts": []}
            existing["hosts"] = [
                row for row in existing["hosts"] if row.get("name") != host_id
            ] + [entry]
            _atomic(registry, existing)
        enrollment = (
            Enrollment(**published_snapshot)
            if publication_pending
            else Enrollment(
                secret_ref,
                model_identity,
                f"github-deploy-key:{steps['repository']['id']}",
                f"tailscale-auth-key:{steps['tailnet']['id']}",
                "controller-token-sha256:" + steps["controller"]["sha256"],
                model_expiry,
            )
        )
        state["published_enrollment"] = enrollment.__dict__
        _atomic(private, state)
        _atomic(public, enrollment.__dict__)
        return enrollment

    def revoke(self, host_id: str) -> tuple[str, ...]:
        with file_lock(self.root / f".{host_id}.lock"):
            return self._revoke_locked(host_id)

    def _revoke_locked(self, host_id: str) -> tuple[str, ...]:
        public, private = self._paths(host_id)
        if not private.exists():
            return ()
        state = json.loads(private.read_text())
        steps = state.get("steps", {})
        cleaned = state.setdefault("cleanup_done", {})
        pending = self._revoke_incomplete_attempts(steps, cleaned, private, state)
        for kind, label, action, confirm in (
            (
                "repository",
                f"github-deploy-key:{steps.get('repository', {}).get('id', '')}",
                lambda: self.github.delete_deploy_key(
                    steps["repository_intent"]["repo"], steps["repository"]["id"]
                ),
                lambda: (
                    self.github.find_deploy_key(
                        steps["repository_intent"]["repo"],
                        steps["repository"]["title"],
                        steps["repository_intent"]["public_key"],
                    )
                    is None
                ),
            ),
            (
                "tailnet",
                f"tailscale-auth-key:{steps.get('tailnet', {}).get('id', '')}",
                lambda: self.tailscale.delete_key(steps["tailnet"]["id"]),
                lambda: self.tailscale.find_key(steps["tailnet"]["description"]) is None,
            ),
            (
                "bootstrap",
                steps.get("bootstrap", {}).get("arn", ""),
                lambda: self._schedule_bootstrap_delete(steps),
                lambda: self._bootstrap_deleted(steps),
            ),
        ):
            if not label or label.endswith(":") or cleaned.get(kind):
                continue
            try:
                if confirm():
                    cleaned[kind] = True
                    _atomic(private, state)
                    continue
                action()
                if not confirm():
                    raise RuntimeError(f"{kind} revocation could not be confirmed")
                cleaned[kind] = True
                _atomic(private, state)
            except Exception:
                pending.append(label)
        registry = self.root / "controller-hosts.json"
        try:
            with file_lock(self.root / ".registry.lock"):
                value = json.loads(registry.read_text()) if registry.exists() else {"hosts": []}
                value["hosts"] = [row for row in value["hosts"] if row.get("name") != host_id]
                _atomic(registry, value)
            cleaned["controller"] = True
            _atomic(private, state)
        except Exception:
            pending.append("controller-registry:" + host_id)
        state["pending_cleanup"] = pending
        state["revoked_at"] = self.now().isoformat()
        _atomic(private, state)
        if not pending:
            steps.get("repository", {}).pop("private_key", None)
            steps.get("repository_intent", {}).pop("private_key", None)
            steps.get("tailnet", {}).pop("key", None)
            steps.get("controller", {}).pop("token", None)
            _atomic(private, state)
            public.unlink(missing_ok=True)
        return tuple(pending)

    def _schedule_bootstrap_delete(self, steps: dict) -> None:
        bootstrap = steps["bootstrap"]
        described = self.secrets.describe(bootstrap["arn"])
        if not described or described.get("tags") != bootstrap["tags"]:
            raise RuntimeError("refusing to delete bootstrap secret without exact ownership tags")
        if described.get("client_token") != bootstrap["client_token"]:
            raise RuntimeError("refusing to delete a different bootstrap secret version")
        self.secrets.schedule_delete(bootstrap["arn"], recovery_days=7)

    def _bootstrap_deleted(self, steps: dict) -> bool:
        bootstrap = steps["bootstrap"]
        described = self.secrets.describe(bootstrap["arn"])
        if not described:
            return False
        if (
            described.get("tags") != bootstrap["tags"]
            or described.get("client_token") != bootstrap["client_token"]
        ):
            raise RuntimeError("bootstrap deletion confirmation has different ownership")
        return bool(described.get("deleted"))

    @staticmethod
    def _validate_repository_name(repository: str) -> None:
        parts = repository.split("/")
        allowed = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-"
        if (
            len(parts) != 2
            or any(not part or part in {".", ".."} for part in parts)
            or any(any(character not in allowed for character in part) for part in parts)
        ):
            raise RuntimeError("github_repo must be an exact OWNER/REPO name")

    def _validate_tailnet(self, created: dict, attempt: dict) -> dt.datetime:
        key = str(created.get("key") or "")
        key_id = str(created.get("id") or "")
        capabilities = created.get("capabilities") or {}
        create = (capabilities.get("devices") or {}).get("create") or {}
        if (
            not key_id
            or not key.startswith("tskey-auth-")
            or created.get("description") != attempt["description"]
            or create.get("tags") != [attempt["tag"]]
            or create.get("reusable") is not False
            or create.get("ephemeral") is not False
            or create.get("preauthorized") is not True
        ):
            raise RuntimeError("tailnet API returned enrollment outside the admitted scope")
        try:
            expires = dt.datetime.fromisoformat(str(created["expires"]).replace("Z", "+00:00"))
        except (KeyError, ValueError):
            raise RuntimeError("tailnet API did not return an authoritative expiry") from None
        minimum = self.now() + dt.timedelta(
            seconds=int(self.config.get("enrollment_min_validity_seconds", 300))
        )
        if expires.tzinfo is None or expires <= minimum:
            raise RuntimeError("tailnet enrollment expires before it can be safely launched")
        return expires

    def _revoke_incomplete_attempts(
        self, steps: dict, cleaned: dict, private: Path, state: dict
    ) -> list[str]:
        pending = []
        intent = steps.get("repository_intent")
        if intent and "repository" not in steps and not cleaned.get("repository_intent"):
            label = "github-deploy-key-intent:" + intent["title"]
            try:
                found = self.github.find_deploy_key(
                    intent["repo"], intent["title"], intent["public_key"]
                )
                if found:
                    self.github.delete_deploy_key(intent["repo"], found["id"])
                if self.github.find_deploy_key(
                    intent["repo"], intent["title"], intent["public_key"]
                ):
                    raise RuntimeError("repository intent revocation could not be confirmed")
                cleaned["repository_intent"] = True
                _atomic(private, state)
            except Exception:
                pending.append(label)
        attempt = steps.get("tailnet_attempt")
        if attempt and "tailnet" not in steps and not cleaned.get("tailnet_attempt"):
            label = "tailscale-auth-key-attempt:" + attempt["description"]
            try:
                found = self.tailscale.find_key(attempt["description"])
                if found:
                    self.tailscale.delete_key(found["id"])
                if self.tailscale.find_key(attempt["description"]):
                    raise RuntimeError("tailnet attempt revocation could not be confirmed")
                cleaned["tailnet_attempt"] = True
                _atomic(private, state)
            except Exception:
                pending.append(label)
        return pending

    @staticmethod
    def _ssh_keygen(host_id: str) -> tuple[str, str]:
        with tempfile.TemporaryDirectory(prefix="garden-enroll-") as directory:
            path = Path(directory) / "key"
            subprocess.run(
                ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", host_id, "-f", str(path)],
                check=True,
                timeout=30,
                capture_output=True,
            )
            return path.read_text(), path.with_suffix(".pub").read_text().strip()
