from __future__ import annotations

import httpx
import pytest

from garden.config import Config
from garden.github import GitHub, GitHubError
from garden.source_control import (
    AuthenticationFailure,
    CertificateFailure,
    ConnectionPolicy,
    ProviderUnavailable,
    ProxyFailure,
    SourceControlError,
    SourceControlProvider,
)


def test_provider_contract_accepts_two_synthetic_providers():
    class SyntheticProvider:
        available = True

        def describe(self):
            return "synthetic"

        def operation(self, *_args, **_kwargs):
            return None

        find_pr = list_open_prs = get_pr = create_pr = operation
        feedback_since = branch_exists = merge_pr = operation

    # Structural typing lets independently implemented adapters share scheduler policy.
    class OtherSynthetic(SyntheticProvider):
        pass

    assert isinstance(SyntheticProvider(), SourceControlProvider)
    assert isinstance(OtherSynthetic(), SourceControlProvider)


def test_connection_policy_scopes_custom_ca_and_proxy(tmp_path):
    bundle = tmp_path / "private-ca.pem"
    bundle.write_text("-----BEGIN CERTIFICATE-----\nfixture\n-----END CERTIFICATE-----\n")
    policy = ConnectionPolicy(
        "https://forge.test", "https://forge.test/api", credential_env="FORGE_TOKEN",
        ca_bundle=str(bundle), proxy="https://proxy.test",
    )
    assert policy.request_options() == {
        "verify": str(bundle), "follow_redirects": False, "proxy": "https://proxy.test"
    }


@pytest.mark.parametrize("field,value", [
    ("web_url", "http://forge.test"),
    ("api_url", "https://token@forge.test/api"),
    ("proxy", "https://user:password@proxy.test"),
])
def test_connection_policy_rejects_unsafe_endpoints(field, value):
    values = {"web_url": "https://forge.test", "api_url": "https://forge.test/api", "proxy": ""}
    values[field] = value
    with pytest.raises(ValueError):
        ConnectionPolicy(**values)


def test_connection_policy_rejects_invalid_ca(tmp_path):
    bundle = tmp_path / "not-a-ca.pem"
    bundle.write_text("verification=false")
    with pytest.raises(ValueError, match="PEM"):
        ConnectionPolicy("https://forge.test", "https://forge.test/api", ca_bundle=str(bundle))


def test_redirect_is_rejected_without_forwarding_authorization(monkeypatch):
    github = GitHub(use_gh=False, host="forge.test", token="secret")
    monkeypatch.setattr("garden.github.httpx.request", lambda *a, **k: httpx.Response(
        302, headers={"location": "https://other.test/collect"}
    ))
    with pytest.raises(SourceControlError, match="unsafe redirect"):
        github.find_pr("team/repo", "work")


@pytest.mark.parametrize(("raised", "expected"), [
    (httpx.ProxyError("proxy auth secret"), ProxyFailure),
    (httpx.ConnectError("SSL certificate failed private.test"), CertificateFailure),
    (httpx.ConnectError("private.test unavailable"), ProviderUnavailable),
])
def test_transport_failures_have_distinct_redacted_diagnostics(monkeypatch, raised, expected):
    github = GitHub(use_gh=False, host="forge.test", token="secret")
    def fail(*_args, **_kwargs):
        raise raised
    monkeypatch.setattr("garden.github.httpx.request", fail)
    with pytest.raises(expected) as caught:
        github.find_pr("team/repo", "work")
    assert "secret" not in str(caught.value)
    assert "private.test" not in str(caught.value)


def test_authentication_failure_does_not_echo_response_or_token(monkeypatch):
    github = GitHub(use_gh=False, host="forge.test", token="secret")
    monkeypatch.setattr("garden.github.httpx.request", lambda *a, **k: httpx.Response(
        401, text="secret https://private.test"
    ))
    with pytest.raises(AuthenticationFailure) as caught:
        github.find_pr("team/repo", "work")
    assert isinstance(caught.value, GitHubError)
    assert "secret" not in str(caught.value)
    assert "private.test" not in str(caught.value)


def test_provider_neutral_product_config_preserves_non_default_base_and_trust(tmp_path):
    bundle = tmp_path / "ca.pem"
    bundle.write_text("-----BEGIN CERTIFICATE-----\nfixture\n-----END CERTIFICATE-----\n")
    config = Config(tmp_path, {
        "products": {"demo": {
            "base_branch": "release/next",
            "source_control": {
                "provider": "github", "repository": "team/repo", "host": "forge.test",
                "web_url": "https://forge.test", "api_url": "https://forge.test/api/v3",
                "credential_env": "FORGE_TOKEN", "ca_bundle": str(bundle),
                "proxy": "https://proxy.test",
            },
        }},
    })
    route = config.product_source_control("demo")
    assert config.product_base_branch("demo") == "release/next"
    assert route == {
        "provider": "github", "repository": "team/repo", "host": "forge.test",
        "web_url": "https://forge.test", "api_base": "https://forge.test/api/v3",
        "token_env": "FORGE_TOKEN", "ca_bundle": str(bundle),
        "proxy": "https://proxy.test",
    }
