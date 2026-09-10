"""Resolve logical workload identities at an operation boundary.

Provider registration and reference policy come from trusted, fenced garden configuration.
Callers may select a reference and request less authority, but cannot supply provider code,
delivery bindings, or broaden the configured operation, audience, scopes, or lifetime.
"""

from __future__ import annotations

import importlib
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Protocol


class WorkloadIdentityError(RuntimeError):
    """An actionable environment failure at credential resolution or use."""


@dataclass(frozen=True)
class AuthorityRequest:
    reference: str
    operation: str
    audience: str
    run_identity: str
    lifetime_seconds: int
    scopes: frozenset[str]
    target: str


@dataclass(frozen=True)
class ProviderAuthority:
    """Secret provider result. Values must never be serialized or logged."""

    values: Mapping[str, str] = field(repr=False)
    issuer: str
    expires_at: float
    scopes: frozenset[str]
    audience: str
    membership: str
    renewal_token: object | None = field(default=None, repr=False)


class IdentityProvider(Protocol):
    adapter_version: int
    capabilities: frozenset[str]

    def resolve(self, request: AuthorityRequest) -> ProviderAuthority: ...

    def renew(self, request: AuthorityRequest, authority: ProviderAuthority) -> ProviderAuthority: ...

    def validate(self, request: AuthorityRequest, authority: ProviderAuthority) -> None: ...


@dataclass(frozen=True)
class AuthorityMetadata:
    issuer: str
    expires_at: float
    audience: str
    scopes: tuple[str, ...]
    principal_kind: str
    automation_identity: str
    provider: str


@dataclass
class ResolvedAuthority:
    """Ephemeral authority whose public representation contains metadata only."""

    request: AuthorityRequest
    provider_name: str
    delivery: str
    bindings: Mapping[str, str]
    _provider: IdentityProvider = field(repr=False)
    _authority: ProviderAuthority = field(repr=False)
    _closed: bool = field(default=False, init=False, repr=False)

    @property
    def metadata(self) -> AuthorityMetadata:
        authority = self._authority
        return AuthorityMetadata(
            issuer=authority.issuer,
            expires_at=authority.expires_at,
            audience=authority.audience,
            scopes=tuple(sorted(authority.scopes)),
            principal_kind="automation",
            automation_identity=self.request.run_identity,
            provider=self.provider_name,
        )

    def _ensure_current(self) -> ProviderAuthority:
        if self._closed:
            raise WorkloadIdentityError(f"workload identity {self.request.reference!r} is closed")
        now = time.time()
        if self._authority.expires_at <= now:
            if "renewal" not in self._provider.capabilities:
                raise WorkloadIdentityError(f"workload identity {self.request.reference!r} expired")
            try:
                self._authority = self._provider.renew(self.request, self._authority)
            except Exception as exc:
                raise WorkloadIdentityError(
                    f"workload identity {self.request.reference!r} renewal failed: {type(exc).__name__}"
                ) from exc
        try:
            self._provider.validate(self.request, self._authority)
        except Exception as exc:
            raise WorkloadIdentityError(
                f"workload identity {self.request.reference!r} is unavailable: {type(exc).__name__}"
            ) from exc
        _validate_authority(self.request, self._authority)
        return self._authority

    def subprocess_env(self, base: Mapping[str, str], target: str) -> dict[str, str]:
        """Return an environment for the one named subprocess delivery boundary."""
        if self.delivery != "environment":
            raise WorkloadIdentityError("workload identity is not configured for subprocess delivery")
        if target != self.request.target:
            raise WorkloadIdentityError("workload identity is not configured for this subprocess")
        authority = self._ensure_current()
        return {**base, **_bound_values(self.bindings, authority.values)}

    def request_headers(self, base: Mapping[str, str], target: str) -> dict[str, str]:
        """Return headers for the one named protocol request delivery boundary."""
        if self.delivery != "headers":
            raise WorkloadIdentityError("workload identity is not configured for protocol delivery")
        if target != self.request.target:
            raise WorkloadIdentityError("workload identity is not configured for this protocol request")
        authority = self._ensure_current()
        return {**base, **_bound_values(self.bindings, authority.values)}

    def close(self) -> None:
        self._closed = True
        self._authority = ProviderAuthority({}, "closed", 0, frozenset(), "", "")

    def __repr__(self) -> str:
        return f"ResolvedAuthority(metadata={self.metadata!r}, delivery={self.delivery!r})"


def _bound_values(bindings: Mapping[str, str], values: Mapping[str, str]) -> dict[str, str]:
    missing = sorted(set(bindings.values()) - set(values))
    if missing:
        raise WorkloadIdentityError(f"provider omitted required authority values: {', '.join(missing)}")
    return {target: values[source] for target, source in bindings.items()}


def _validate_authority(request: AuthorityRequest, authority: ProviderAuthority) -> None:
    now = time.time()
    if authority.audience != request.audience:
        raise WorkloadIdentityError("provider returned authority for a different audience")
    if not request.scopes.issuperset(authority.scopes) or authority.scopes != request.scopes:
        raise WorkloadIdentityError("provider returned authority with mismatched scopes")
    if authority.expires_at <= now:
        raise WorkloadIdentityError("provider returned expired authority")
    if authority.expires_at > now + request.lifetime_seconds + 1:
        raise WorkloadIdentityError("provider returned authority exceeding the requested lifetime")
    if authority.membership != request.run_identity:
        raise WorkloadIdentityError("provider returned authority for a different run membership")


class WorkloadIdentityResolver:
    """Trusted resolver constructed from the host's local configuration."""

    def __init__(self, config: Mapping[str, Any]):
        section = config.get("workload_identity") or {}
        self._references = MappingProxyType(dict(section.get("references") or {}))
        self._providers = self._load_providers(section.get("providers") or {})

    @staticmethod
    def _load_providers(config: Mapping[str, Any]) -> dict[str, IdentityProvider]:
        providers: dict[str, IdentityProvider] = {}
        if not isinstance(config, Mapping):
            raise WorkloadIdentityError("workload_identity.providers must be a mapping")
        for name, spec in config.items():
            if not isinstance(spec, Mapping):
                raise WorkloadIdentityError(f"identity provider {name!r} must be a mapping")
            unknown = set(spec) - {"module", "class", "version", "capabilities", "config"}
            if unknown:
                raise WorkloadIdentityError(f"identity provider {name!r} has unsupported fields")
            try:
                cls = getattr(importlib.import_module(str(spec["module"])), str(spec["class"]))
                provider = cls(dict(spec.get("config") or {}))
            except Exception as exc:
                raise WorkloadIdentityError(f"identity provider {name!r} is unavailable") from exc
            expected_version = int(spec.get("version") or 0)
            if expected_version != 1 or getattr(provider, "adapter_version", None) != expected_version:
                raise WorkloadIdentityError(f"identity provider {name!r} requires interface version 1")
            declared = frozenset(str(item) for item in spec.get("capabilities") or [])
            if not declared or declared != frozenset(getattr(provider, "capabilities", ())):
                raise WorkloadIdentityError(f"identity provider {name!r} capabilities do not match")
            providers[str(name)] = provider
        return providers

    def resolve(self, reference: str, operation: str, audience: str, run_identity: str,
                lifetime_seconds: int, scopes: set[str] | frozenset[str] | None = None, *,
                target: str) -> ResolvedAuthority:
        policy = self._references.get(reference)
        if not isinstance(policy, Mapping):
            raise WorkloadIdentityError(f"unknown workload identity reference {reference!r}")
        allowed_scopes = frozenset(str(item) for item in policy.get("scopes") or [])
        requested_scopes = allowed_scopes if scopes is None else frozenset(scopes)
        try:
            maximum = int(policy.get("max_lifetime_seconds") or 0)
        except (TypeError, ValueError) as exc:
            raise WorkloadIdentityError(
                f"workload identity reference {reference!r} has an invalid lifetime"
            ) from exc
        if operation != policy.get("operation") or audience != policy.get("audience"):
            raise WorkloadIdentityError("workload identity operation or audience is not allowed")
        if target != policy.get("target"):
            raise WorkloadIdentityError("workload identity target is not allowed")
        if not requested_scopes.issubset(allowed_scopes):
            raise WorkloadIdentityError("requested workload identity scope is not allowed")
        if not run_identity or lifetime_seconds <= 0 or not maximum or lifetime_seconds > maximum:
            raise WorkloadIdentityError("requested workload identity lifetime or run identity is not allowed")
        provider_name = str(policy.get("provider") or "")
        provider = self._providers.get(provider_name)
        if provider is None:
            raise WorkloadIdentityError(f"identity provider {provider_name!r} is unavailable")
        request = AuthorityRequest(reference, operation, audience, run_identity,
                                   lifetime_seconds, requested_scopes, target)
        try:
            authority = provider.resolve(request)
        except Exception as exc:
            raise WorkloadIdentityError(
                f"workload identity {reference!r} resolution failed: {type(exc).__name__}"
            ) from exc
        _validate_authority(request, authority)
        delivery = str(policy.get("delivery") or "")
        bindings = policy.get("bindings") or {}
        if delivery not in {"environment", "headers"} or not isinstance(bindings, Mapping) or not bindings:
            raise WorkloadIdentityError(f"workload identity {reference!r} has invalid delivery policy")
        if delivery == "environment" and any("," in str(name) for name in bindings):
            raise WorkloadIdentityError(f"workload identity {reference!r} has invalid environment bindings")
        return ResolvedAuthority(request, provider_name, delivery,
                                 MappingProxyType({str(k): str(v) for k, v in bindings.items()}),
                                 provider, authority)

    @contextmanager
    def operation(self, *args: Any, **kwargs: Any) -> Iterator[ResolvedAuthority]:
        authority = self.resolve(*args, **kwargs)
        try:
            yield authority
        finally:
            authority.close()


@contextmanager
def subprocess_authority(config: Mapping[str, Any], target: str, run_identity: str,
                         base: Mapping[str, str]) -> Iterator[tuple[dict[str, str], AuthorityMetadata | None]]:
    """Apply the host-configured identity, if any, to exactly one subprocess target."""
    section = config.get("workload_identity") or {}
    boundaries = section.get("boundaries") or {}
    boundary = boundaries.get(target) if isinstance(boundaries, Mapping) else None
    if boundary is None:
        yield dict(base), None
        return
    if not isinstance(boundary, Mapping):
        raise WorkloadIdentityError(f"workload identity boundary {target!r} must be a mapping")
    unknown = set(boundary) - {"reference", "operation", "audience", "lifetime_seconds", "scopes"}
    if unknown:
        raise WorkloadIdentityError(f"workload identity boundary {target!r} has unsupported fields")
    try:
        lifetime = int(boundary.get("lifetime_seconds") or 0)
        scopes = {str(scope) for scope in boundary.get("scopes") or []} or None
    except (TypeError, ValueError) as exc:
        raise WorkloadIdentityError(
            f"workload identity boundary {target!r} has invalid lifetime or scopes"
        ) from exc
    resolver = WorkloadIdentityResolver(config)
    with resolver.operation(
        str(boundary.get("reference") or ""), str(boundary.get("operation") or ""),
        str(boundary.get("audience") or ""), run_identity, lifetime, scopes, target=target,
    ) as authority:
        yield authority.subprocess_env(base, target), authority.metadata
