"""Provider-neutral source-control contracts and scoped outbound trust policy."""

from __future__ import annotations

import re
import ssl
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urlparse

_PEM_CERTIFICATE = re.compile(
    r"-----BEGIN CERTIFICATE-----\s+"
    r"[A-Za-z0-9+/=\r\n]+"
    r"-----END CERTIFICATE-----"
)


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


class RateLimitFailure(ProviderUnavailable):
    """Provider refusal carrying an optional absolute retry timestamp."""

    def __init__(self, reset_at: float | None = None):
        super().__init__("source-control provider rate limited")
        self.reset_at = reset_at


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
    def me(self) -> str: ...
    def is_authenticated(self) -> bool: ...
    def repository_from_remote(self, repository: str, url: str) -> str | None: ...
    def change_request_number(self, repository: str, url: str) -> int | None: ...
    def is_safe_change_request_url(self, repository: str, url: str) -> bool: ...
    def find_pr(self, repository: str, head_branch: str) -> Any: ...
    def find_open_pr(self, repository: str, head_branch: str) -> Any: ...
    def find_open_pr_by_base(self, repository: str, base_branch: str) -> Any: ...
    def list_open_prs(self, repository: str) -> list[Any]: ...
    def get_pr(self, repository: str, number: int) -> Any: ...
    def create_pr(self, repository: str, head: str, base: str, title: str, body: str,
                  draft: bool = ..., reviewers: list[str] | None = ...) -> Any: ...
    def feedback_since(self, repository: str, number: int, since_iso: str,
                       exclude_logins: set[str] | None = ...) -> Any: ...
    def incremental_feedback_since(self, repository: str, number: int, since_iso: str,
                                   exclude_logins: set[str] | None = ...) -> Any: ...
    def complete_feedback(self, repository: str, number: int) -> dict[str, Any]: ...
    def update_pr(self, repository: str, number: int, title: str = ..., body: str = ...,
                  base: str = ...) -> None: ...
    def mark_ready(self, repository: str, number: int) -> None: ...
    def close_pr(self, repository: str, number: int) -> None: ...
    def reopen_pr(self, repository: str, number: int) -> None: ...
    def branch_exists(self, repository: str, branch: str) -> bool: ...
    def base_ref_deleted(self, repository: str, number: int) -> bool: ...
    def merge_pr(self, repository: str, number: int, method: str = ...,
                 delete_branch: bool = ..., expected_head: str = ...) -> None: ...
    def delete_branch(self, repository: str, branch: str) -> None: ...
    def issue_comments(self, repository: str, number: int) -> list[str]: ...
    def comment(self, repository: str, number: int, body: str) -> None: ...


SourceControlFactory = Callable[[Mapping[str, str]], SourceControlProvider]


class RepositoryIdentity(str):
    """Repository name carrying the adapter route without exposing endpoint details."""

    def __new__(cls, repository: str, provider: str):
        value = super().__new__(cls, repository)
        value.provider = provider
        return value

    def __getnewargs__(self) -> tuple[str, str]:
        return str(self), self.provider


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
        web_authority = urlparse(self.web_url).hostname
        api_authority = urlparse(self.api_url).hostname
        if web_authority != api_authority and (web_authority, api_authority) != (
            "github.com", "api.github.com"
        ):
            raise ValueError("web_url and api_url authorities do not match")
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
            certificates = list(_PEM_CERTIFICATE.finditer(content))
            remainder = _PEM_CERTIFICATE.sub("", content)
            if not certificates or remainder.strip():
                raise ValueError("ca_bundle is not a PEM certificate bundle")
            try:
                context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                for certificate in certificates:
                    context.load_verify_locations(cadata=certificate.group())
            except (OSError, ssl.SSLError) as exc:
                raise ValueError("ca_bundle contains an invalid PEM certificate") from exc

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
