from __future__ import annotations

import sys
import threading
import time
import types

import pytest

from garden.config import executable_diff
from garden.workload_identity import (
    AuthorityRequest,
    ProviderAuthority,
    WorkloadIdentityError,
    WorkloadIdentityResolver,
)


class SyntheticProvider:
    adapter_version = 1
    capabilities = frozenset({"membership", "renewal", "revocation"})

    def __init__(self, config):
        self.config = config
        self.revoked = False
        self.resolutions = 0
        self.renewals = 0
        self.lock = threading.Lock()

    def _authority(self, request: AuthorityRequest, suffix: str = "") -> ProviderAuthority:
        with self.lock:
            self.resolutions += 1
            serial = self.resolutions
        return ProviderAuthority(
            {"token": f"synthetic-secret-{serial}{suffix}"},
            "synthetic://issuer",
            time.time() + request.lifetime_seconds,
            request.scopes,
            request.audience,
            request.run_identity,
            renewal_token=object(),
        )

    def resolve(self, request: AuthorityRequest) -> ProviderAuthority:
        authority = self._authority(request)
        changed = dict(self.config)
        return ProviderAuthority(
            authority.values,
            authority.issuer,
            time.time() + changed.get("lifetime_offset", request.lifetime_seconds),
            frozenset(changed.get("scopes", authority.scopes)),
            changed.get("audience", authority.audience),
            changed.get("membership", authority.membership),
            authority.renewal_token,
        )

    def renew(self, request: AuthorityRequest, authority: ProviderAuthority) -> ProviderAuthority:
        self.renewals += 1
        return self._authority(request, "-renewed")

    def validate(self, request: AuthorityRequest, authority: ProviderAuthority) -> None:
        if self.revoked:
            raise RuntimeError("revoked synthetic grant")


@pytest.fixture
def provider_module(monkeypatch):
    module = types.ModuleType("garden_test_identity_provider")
    module.SyntheticProvider = SyntheticProvider
    monkeypatch.setitem(sys.modules, module.__name__, module)
    return module.__name__


def identity_config(provider_module: str, *, delivery: str = "environment", provider=None):
    binding = {"SERVICE_TOKEN": "token"} if delivery == "environment" else {"Authorization": "token"}
    return {"workload_identity": {
        "providers": {"synthetic": {
            "module": provider_module,
            "class": "SyntheticProvider",
            "version": 1,
            "capabilities": ["membership", "renewal", "revocation"],
            "config": provider or {},
        }},
        "references": {"packages/read": {
            "provider": "synthetic",
            "operation": "package.download",
            "audience": "packages.example",
            "scopes": ["packages:read"],
            "max_lifetime_seconds": 60,
            "delivery": delivery,
            "bindings": binding,
        }},
    }}


def resolve(resolver: WorkloadIdentityResolver, **kwargs):
    return resolver.resolve("packages/read", "package.download", "packages.example",
                            "automation:run-123", 30, {"packages:read"}, **kwargs)


def test_resolves_bounded_authority_and_redacts_values(provider_module):
    resolver = WorkloadIdentityResolver(identity_config(provider_module))
    with resolver.operation("packages/read", "package.download", "packages.example",
                            "automation:run-123", 30, {"packages:read"}) as authority:
        environment = authority.subprocess_env({"PATH": "/bin"})
        assert environment["SERVICE_TOKEN"].startswith("synthetic-secret-")
        assert authority.metadata.automation_identity == "automation:run-123"
        assert authority.metadata.principal_kind == "automation"
        assert authority.metadata.issuer == "synthetic://issuer"
        assert "synthetic-secret" not in repr(authority)
        assert "synthetic-secret" not in repr(authority._authority)
    with pytest.raises(WorkloadIdentityError, match="closed"):
        authority.subprocess_env({})


@pytest.mark.parametrize("change, message", [
    ({"audience": "wrong.example"}, "different audience"),
    ({"scopes": ["packages:write"]}, "mismatched scopes"),
    ({"membership": "human:operator"}, "different run membership"),
    ({"lifetime_offset": 300}, "exceeding the requested lifetime"),
])
def test_provider_output_fails_closed(provider_module, change, message):
    resolver = WorkloadIdentityResolver(identity_config(provider_module, provider=change))
    with pytest.raises(WorkloadIdentityError, match=message):
        resolve(resolver)


def test_request_cannot_broaden_reference_or_register_provider(provider_module):
    resolver = WorkloadIdentityResolver(identity_config(provider_module))
    with pytest.raises(WorkloadIdentityError, match="scope is not allowed"):
        resolver.resolve("packages/read", "package.download", "packages.example",
                         "automation:run-123", 30, {"packages:write"})
    with pytest.raises(WorkloadIdentityError, match="operation or audience"):
        resolver.resolve("packages/read", "package.publish", "packages.example",
                         "automation:run-123", 30, set())
    with pytest.raises(WorkloadIdentityError, match="unknown workload identity"):
        resolver.resolve("task-output-provider", "package.download", "packages.example",
                         "automation:run-123", 30, set())
    assert executable_diff({}, identity_config(provider_module)) == ["workload_identity"]


def test_renews_then_detects_revocation(provider_module):
    resolver = WorkloadIdentityResolver(identity_config(provider_module))
    authority = resolve(resolver)
    authority._authority = ProviderAuthority(
        authority._authority.values, "synthetic://issuer", time.time() - 1,
        frozenset({"packages:read"}), "packages.example", "automation:run-123",
    )
    assert authority.subprocess_env({})["SERVICE_TOKEN"].endswith("-renewed")
    authority._provider.revoked = True
    with pytest.raises(WorkloadIdentityError, match="unavailable: RuntimeError"):
        authority.subprocess_env({})


def test_concurrent_runs_get_distinct_authority(provider_module):
    resolver = WorkloadIdentityResolver(identity_config(provider_module))
    barrier = threading.Barrier(4)
    values = []

    def worker(number: int) -> None:
        barrier.wait()
        authority = resolver.resolve("packages/read", "package.download", "packages.example",
                                     f"automation:run-{number}", 30, {"packages:read"})
        values.append(authority.subprocess_env({})["SERVICE_TOKEN"])

    threads = [threading.Thread(target=worker, args=(number,)) for number in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(set(values)) == 4


def test_local_environment_and_remote_protocol_delivery_share_policy(provider_module):
    local = resolve(WorkloadIdentityResolver(identity_config(provider_module)))
    remote = resolve(WorkloadIdentityResolver(identity_config(provider_module, delivery="headers")))
    assert set(local.subprocess_env({})) == {"SERVICE_TOKEN"}
    assert set(remote.request_headers({})) == {"Authorization"}
    with pytest.raises(WorkloadIdentityError, match="not configured for protocol"):
        local.request_headers({})
    with pytest.raises(WorkloadIdentityError, match="not configured for subprocess"):
        remote.subprocess_env({})


def test_unavailable_or_misdeclared_provider_is_actionable(provider_module):
    config = identity_config(provider_module)
    config["workload_identity"]["providers"]["synthetic"]["capabilities"] = ["renewal"]
    with pytest.raises(WorkloadIdentityError, match="capabilities do not match"):
        WorkloadIdentityResolver(config)
    config = identity_config("missing_provider_module")
    with pytest.raises(WorkloadIdentityError, match="is unavailable"):
        WorkloadIdentityResolver(config)
