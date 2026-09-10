"""Scoped production API clients for durable worker enrollment.

Credential values are loaded from private files for each request and are never returned
from these adapters except for newly minted one-time enrollment values.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

import httpx


def _private_json(path: Path) -> dict[str, Any]:
    if not path.is_file() or path.stat().st_mode & 0o077:
        raise RuntimeError(f"credential file must exist and be mode 0600: {path}")
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise RuntimeError(f"credential file must contain a JSON object: {path}")
    return value


class BotoSecretsManagerClient:
    """Small Secrets Manager adapter using a named, scoped boto3 profile.

    Same-operation prelaunch renewal requires narrowly scoped
    ``secretsmanager:PutSecretValue`` in addition to create/describe/delete.  A deployment
    whose installed policy omits it receives an actionable AWS denial; this adapter never
    changes IAM policy or falls back to a broader identity.
    """

    def __init__(self, *, profile: str, region: str, account: str):
        try:
            import boto3
        except ImportError as error:  # pragma: no cover - depends on production packaging
            raise RuntimeError("production AWS enrollment requires boto3") from error
        self.account, self.region = account, region
        session = boto3.Session(profile_name=profile, region_name=region)
        self.client = session.client("secretsmanager")
        self.identity = session.client("sts")

    def _require_account(self) -> None:
        identity = self.identity.get_caller_identity()
        expected_role = f"arn:aws:sts::{self.account}:assumed-role/ContextGardenProvisioner/"
        if (
            str(identity.get("Account")) != self.account
            or not str(identity.get("Arn", "")).startswith(expected_role)
        ):
            raise RuntimeError("AWS profile must assume the scoped ContextGardenProvisioner role")

    def _row(self, value: dict[str, Any]) -> dict[str, Any]:
        prefix = f"arn:aws:secretsmanager:{self.region}:{self.account}:secret:context-garden/phase05/renew-"
        if not str(value.get("ARN", "")).startswith(prefix):
            raise RuntimeError("Secrets Manager response exceeds the scoped account/region/prefix")
        versions = value.get("VersionIdsToStages") or {}
        current = [version for version, stages in versions.items() if "AWSCURRENT" in stages]
        if len(current) != 1:
            raise RuntimeError("bootstrap secret must have exactly one AWSCURRENT version")
        return {
            "arn": value["ARN"],
            "name": value.get("Name", ""),
            "tags": {row["Key"]: row["Value"] for row in value.get("Tags", [])},
            "client_token": current[0],
            "deleted": value.get("DeletedDate") is not None,
        }

    def describe(self, ref: str) -> dict[str, Any] | None:
        try:
            row = self._row(self.client.describe_secret(SecretId=ref))
            if row["name"] != ref and row["arn"] != ref:
                raise RuntimeError("Secrets Manager returned a different secret name")
            return row
        except self.client.exceptions.ResourceNotFoundException:
            return None

    def create(self, ref: str, token: str, envelope: dict, tags: dict) -> dict[str, Any]:
        self._require_account()
        value = self.client.create_secret(
            Name=ref,
            Description="Context Garden one-use worker bootstrap",
            ClientRequestToken=token,
            SecretString=json.dumps(envelope, sort_keys=True, separators=(",", ":")),
            Tags=[{"Key": key, "Value": item} for key, item in sorted(tags.items())],
        )
        row = {
            "arn": value["ARN"],
            "name": value["Name"],
            "tags": dict(tags),
            "client_token": value.get("VersionId", token),
            "deleted": False,
        }
        prefix = f"arn:aws:secretsmanager:{self.region}:{self.account}:secret:context-garden/phase05/renew-"
        if not row["arn"].startswith(prefix):
            raise RuntimeError("Secrets Manager create response exceeds the scoped account/region/prefix")
        if row["name"] != ref:
            raise RuntimeError("Secrets Manager create response has a different secret name")
        return row

    def schedule_delete(self, ref: str, recovery_days: int) -> dict[str, Any]:
        self._require_account()
        if recovery_days != 7:
            raise RuntimeError("bootstrap secrets require the reviewed seven-day recovery window")
        owned = self.describe(ref)
        if owned is None or owned["deleted"]:
            return owned or {"arn": ref, "deleted": True}
        value = self.client.delete_secret(SecretId=owned["arn"], RecoveryWindowInDays=7)
        return {"arn": value["ARN"], "deleted": value.get("DeletionDate") is not None}

    def put(self, ref: str, token: str, envelope: dict, tags: dict) -> dict[str, Any]:
        """Add one idempotent version to an already ownership-checked secret."""
        self._require_account()
        owned = self.describe(ref)
        if not owned or owned["tags"] != tags or owned["deleted"]:
            raise RuntimeError("refusing to refresh an unrelated bootstrap secret")
        value = self.client.put_secret_value(
            SecretId=owned["arn"],
            ClientRequestToken=token,
            SecretString=json.dumps(envelope, sort_keys=True, separators=(",", ":")),
        )
        if value.get("VersionId") != token or value.get("ARN") != owned["arn"]:
            raise RuntimeError("Secrets Manager returned a different refreshed version")
        return {**owned, "client_token": token}


class TailscaleOAuthClient:
    """Auth-key-only Tailscale OAuth adapter backed by a private saved client file."""

    def __init__(self, credential_file: Path, *, tailnet: str = "-", client=None):
        self.credential_file, self.tailnet = credential_file, tailnet
        self.http = client or httpx.Client(timeout=30)

    def _token(self) -> str:
        auth = _private_json(self.credential_file)
        client_id = auth.get("client_id") or auth.get("id")
        client_secret = auth.get("client_secret") or auth.get("secret")
        if not client_id or not client_secret:
            raise RuntimeError("Tailscale credential file lacks client_id/client_secret")
        response = self.http.post(
            "https://api.tailscale.com/api/v2/oauth/token",
            data={
                "grant_type": "client_credentials",
                "scope": "auth_keys",
                "tags": "tag:garden-worker",
            },
            auth=(client_id, client_secret),
        )
        response.raise_for_status()
        return response.json()["access_token"]

    def _request(self, method: str, suffix: str, **kwargs):
        response = self.http.request(
            method,
            f"https://api.tailscale.com/api/v2/tailnet/{quote(self.tailnet, safe='')}/{suffix}",
            headers={"Authorization": f"Bearer {self._token()}"},
            **kwargs,
        )
        response.raise_for_status()
        return response

    def find_key(self, description: str) -> dict[str, Any] | None:
        value = self._request("GET", "keys").json()
        rows = value.get("keys", value) if isinstance(value, dict) else value
        matches = [
            row
            for row in rows
            if row.get("description") == description
            and not row.get("invalid", False)
            and not row.get("revoked", False)
        ]
        if len(matches) > 1:
            raise RuntimeError("multiple Tailscale keys match one enrollment attempt")
        return matches[0] if matches else None

    def create_key(
        self,
        *,
        description: str,
        tag: str,
        reusable: bool,
        ephemeral: bool,
        preauthorized: bool,
        expiry_seconds: int,
    ) -> dict[str, Any]:
        payload = {
            "capabilities": {
                "devices": {
                    "create": {
                        "reusable": reusable,
                        "ephemeral": ephemeral,
                        "preauthorized": preauthorized,
                        "tags": [tag],
                    }
                }
            },
            "expirySeconds": expiry_seconds,
            "description": description,
        }
        return self._request("POST", "keys", json=payload).json()

    def delete_key(self, key: str) -> None:
        try:
            self._request("DELETE", f"keys/{quote(str(key), safe='')}")
        except httpx.HTTPStatusError as error:
            if error.response.status_code != 404:
                raise


class GitHubDeployKeyClient:
    """Repository deploy-key adapter backed by a private fine-grained token file."""

    def __init__(
        self, credential_file: Path, *, api_url: str = "https://api.github.com", client=None
    ):
        self.credential_file, self.api_url = credential_file, api_url.rstrip("/")
        if urlsplit(self.api_url).scheme != "https":
            raise RuntimeError("GitHub API URL must use HTTPS")
        self.http = client or httpx.Client(timeout=30)

    def _token(self) -> str:
        raw = self.credential_file.read_text().strip() if self.credential_file.is_file() else ""
        if not raw or self.credential_file.stat().st_mode & 0o077:
            raise RuntimeError("GitHub credential file must exist and be mode 0600")
        if raw.startswith("{"):
            value = json.loads(raw)
            raw = value.get("token") or value.get("access_token") or ""
        if not raw:
            raise RuntimeError("GitHub credential file lacks a token")
        return raw

    def _request(self, method: str, repo: str, suffix: str = "", **kwargs):
        parts = repo.split("/")
        allowed = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-"
        if (
            len(parts) != 2
            or any(not part or part in {".", ".."} for part in parts)
            or any(any(character not in allowed for character in part) for part in parts)
        ):
            raise RuntimeError("GitHub repository must be an exact OWNER/REPO name")
        response = self.http.request(
            method,
            f"{self.api_url}/repos/{repo}/keys{suffix}",
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self._token()}",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            **kwargs,
        )
        response.raise_for_status()
        return response

    def find_deploy_key(self, repo: str, title: str, public: str) -> dict | None:
        page = 1
        while True:
            rows = self._request("GET", repo, params={"per_page": 100, "page": page}).json()
            for row in rows:
                if row.get("title") == title:
                    remote = " ".join(row.get("key", "").strip().split()[:2])
                    expected = " ".join(public.strip().split()[:2])
                    if remote != expected:
                        raise RuntimeError("deploy-key title is already owned by a different key")
                    return row
            if len(rows) < 100:
                return None
            page += 1

    def create_deploy_key(self, repo: str, title: str, public: str, read_only: bool) -> dict:
        return self._request(
            "POST", repo, json={"title": title, "key": public, "read_only": read_only}
        ).json()

    def delete_deploy_key(self, repo: str, key: str) -> None:
        try:
            self._request("DELETE", repo, f"/{quote(str(key), safe='')}")
        except httpx.HTTPStatusError as error:
            if error.response.status_code != 404:
                raise


def clients_from_config(config: dict) -> tuple[object, object, object]:
    """Build scoped clients from path/profile references, never inline credentials."""
    aws = BotoSecretsManagerClient(
        profile=config["aws_profile"],
        region=config["aws_region"],
        account=str(config["aws_account_id"]),
    )
    tailscale = TailscaleOAuthClient(
        Path(config["tailscale_oauth_file"]), tailnet=str(config.get("tailscale_tailnet", "-"))
    )
    github = GitHubDeployKeyClient(
        Path(config["github_token_file"]),
        api_url=str(config.get("github_api_url", "https://api.github.com")),
    )
    return aws, tailscale, github
