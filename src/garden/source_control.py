"""Provider-neutral source-control contracts and scoped outbound trust policy."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urlparse


class SourceControlError(Exception):
    """A safe, operator-facing source-control diagnostic."""


class AuthenticationFailure(SourceControlError):
    pass


class CertificateFailure(SourceControlError):
    pass


class ProxyFailure(SourceControlError):
    pass


class ProviderUnavailable(SourceControlError):
    pass


class UnsupportedOperation(SourceControlError):
    pass


@runtime_checkable
class SourceControlProvider(Protocol):
    """The narrow change-request surface used by scheduler safety policy.

    Providers return the existing neutral ``PRInfo``/``Feedback`` value objects. Merge
    policy, protected paths, stack ownership, fresh conflict checks and exact-head guards
    remain in the scheduler rather than becoming provider-specific behavior.
    """

    @property
    def available(self) -> bool: ...
    def describe(self) -> str: ...
    def find_pr(self, repository: str, head_branch: str) -> Any: ...
    def list_open_prs(self, repository: str) -> list[Any]: ...
    def get_pr(self, repository: str, number: int) -> Any: ...
    def create_pr(self, repository: str, head: str, base: str, title: str, body: str,
                  draft: bool = ..., reviewers: list[str] | None = ...) -> Any: ...
    def feedback_since(self, repository: str, number: int, since_iso: str,
                       exclude_logins: set[str] | None = ...) -> Any: ...
    def branch_exists(self, repository: str, branch: str) -> bool: ...
    def merge_pr(self, repository: str, number: int, method: str = ...,
                 delete_branch: bool = ..., expected_head: str = ...) -> None: ...


def _https_endpoint(value: str, label: str) -> str:
    try:
        parsed = urlparse(value)
        port = parsed.port
    except ValueError as exc:
        raise ValueError(f"{label} must be a valid HTTPS URL") from exc
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or port not in (None, 443)):
        raise ValueError(f"{label} must be an HTTPS URL without credentials or URL decorations")
    return value.rstrip("/")


@dataclass(frozen=True)
class ConnectionPolicy:
    """Network trust attached to one configured provider endpoint, never process-global."""

    web_url: str
    api_url: str
    credential_env: str = ""
    ca_bundle: str = ""
    proxy: str = ""

    def __post_init__(self) -> None:
        _https_endpoint(self.web_url, "web_url")
        _https_endpoint(self.api_url, "api_url")
        if self.credential_env and not re.fullmatch(r"[A-Z_][A-Z0-9_]*", self.credential_env):
            raise ValueError("credential_env must name an environment variable")
        if self.proxy:
            proxy = _https_endpoint(self.proxy, "proxy")
            parsed = urlparse(proxy)
            if parsed.path not in ("", "/"):
                raise ValueError("proxy must identify an authority, not a path")
        if self.ca_bundle:
            bundle = Path(self.ca_bundle)
            try:
                content = bundle.read_text(encoding="ascii")
            except (OSError, UnicodeError) as exc:
                raise ValueError("ca_bundle is not a readable PEM certificate bundle") from exc
            if "-----BEGIN CERTIFICATE-----" not in content or "-----END CERTIFICATE-----" not in content:
                raise ValueError("ca_bundle is not a PEM certificate bundle")

    @property
    def authority(self) -> str:
        return str(urlparse(self.api_url).hostname)

    @property
    def verify(self) -> bool | str:
        return self.ca_bundle or True

    def request_options(self) -> dict[str, Any]:
        options: dict[str, Any] = {"verify": self.verify, "follow_redirects": False}
        if self.proxy:
            options["proxy"] = self.proxy
        return options

    def validate_response(self, response: Any) -> None:
        if 300 <= int(response.status_code) < 400:
            raise SourceControlError("provider returned an unsafe redirect")
