from __future__ import annotations

import httpx
import pytest

from garden.config import Config
from garden.github import Feedback, GitHub, GitHubError, PRInfo
from garden.source_control import (
    AuthenticationFailure,
    CertificateFailure,
    ConnectionPolicy,
    ProviderUnavailable,
    ProxyFailure,
    RepositoryIdentity,
    SourceControlError,
    SourceControlProvider,
)


def test_provider_contract_accepts_two_synthetic_providers():
    class SyntheticProvider:
        available = True

        def describe(self):
            return "synthetic"

        def me(self):
            return "fixture"

        def is_authenticated(self):
            return True

        def repository_from_remote(self, _repository, _url):
            return "team/repo"

        def change_request_number(self, _repository, _url):
            return 1

        def is_safe_change_request_url(self, _repository, _url):
            return True

        def operation(self, *_args, **_kwargs):
            return None

        find_pr = list_open_prs = get_pr = create_pr = operation
        feedback_since = complete_feedback = update_pr = operation
        mark_ready = close_pr = reopen_pr = branch_exists = operation
        base_ref_deleted = merge_pr = delete_branch = operation
        issue_comments = comment = operation

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


def test_connection_policy_rejects_mismatched_endpoint_authorities():
    with pytest.raises(ValueError, match="authorities do not match"):
        ConnectionPolicy("https://forge.test", "https://api.other.test")


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


class RoutedSyntheticProvider:
    available = True

    def __init__(self, route):
        self.route = route
        self.calls = []

    def describe(self):
        return "synthetic provider"

    def me(self):
        return "fixture"

    def is_authenticated(self):
        return True

    def repository_from_remote(self, _repository, url):
        prefix = self.route["web_url"] + "/"
        return url.removeprefix(prefix).removesuffix(".git") if url.startswith(prefix) else None

    def change_request_number(self, _repository, url):
        tail = url.rstrip("/").rsplit("/", 1)[-1]
        return int(tail) if tail.isdigit() else None

    def is_safe_change_request_url(self, _repository, url):
        return url.startswith("https://example.invalid/change/")

    def get_pr(self, repository, number):
        self.calls.append(("get_pr", repository, number))
        return PRInfo(number, f"https://example.invalid/change/{number}", "OPEN", base="release/next",
                      head_sha="stale-head", mergeable="MERGEABLE", checks="SUCCESS")

    def merge_pr(self, repository, number, method="squash", delete_branch=True, expected_head=""):
        self.calls.append(("merge_pr", repository, number, expected_head))

    def comment(self, repository, number, body):
        self.calls.append(("comment", repository, number))

    def list_open_prs(self, _repository):
        return []

    def feedback_since(self, _repository, _number, _since_iso, exclude_logins=None):
        return Feedback()

    def branch_exists(self, _repository, _branch):
        return True


def test_scheduler_routes_two_registered_providers_and_keeps_exact_head_guard(garden, monkeypatch):
    from garden.scheduler import Scheduler
    from garden.scheduler.report import TickReport
    from garden.store import Store

    store = Store(garden)
    store.config.data["products"]["demo"].update({
        "base_branch": "release/next",
        "source_control": {
            "provider": "forge-a", "repository": "team/repo",
            "web_url": "https://forge-a.test", "api_url": "https://forge-a.test/api",
        },
    })
    store.config.data["products"]["other"] = {
        "repo": ".", "source_control": {
            "provider": "forge-b", "repository": "team/other",
            "web_url": "https://forge-b.test", "api_url": "https://forge-b.test/api",
        },
    }
    providers = {}

    def factory(route):
        provider = RoutedSyntheticProvider(route)
        providers[route["provider"]] = provider
        return provider

    sched = Scheduler(store, read_only=True, source_control_factories={
        "forge-a": factory, "forge-b": factory,
    })
    monkeypatch.setattr(sched, "repo_for", lambda _task: garden.parent / "repo")
    monkeypatch.setattr("garden.scheduler.gitops.remote_url", lambda _repo: "https://forge-a.test/team/repo.git")
    task = store.task("DM-001")
    identity = sched.slug_for(task)
    assert identity.provider == "forge-a"
    assert sched.github.get_pr(identity, 17).head_sha == "stale-head"
    sched.state.get(task.id)["pr_number"] = 17
    sched._do_merge(task, PRInfo(17, "https://example.invalid/change/17", "OPEN", head_sha="reviewed-head"), TickReport())
    assert ("merge_pr", "team/repo", 17, "reviewed-head") in providers["forge-a"].calls
    other = RepositoryIdentity("team/other", "forge-b")
    assert sched.github.get_pr(other, 4).base == "release/next"


def test_source_control_route_cannot_fall_back_to_ambient_gh(garden, monkeypatch):
    from garden.scheduler import Scheduler
    from garden.store import Store

    store = Store(garden)
    store.config.data["products"]["demo"]["source_control"] = {
        "provider": "github", "repository": "team/repo",
        "web_url": "https://github.com", "api_url": "https://api.github.com",
        "credential_env": "SCOPED_GITHUB_TOKEN",
    }
    monkeypatch.setattr("garden.github.shutil.which", lambda _name: "/usr/bin/gh")
    sched = Scheduler(store, read_only=True)
    client = sched.github.routes[("github.com", "team/repo")]
    assert client.gh is None
    assert client.token is None


def test_source_control_route_applies_checkout_identity_guard(garden, monkeypatch):
    from garden.gitops import GitError
    from garden.scheduler import Scheduler
    from garden.store import Store

    store = Store(garden)
    store.config.data["products"]["demo"]["source_control"] = {
        "provider": "forge-a", "repository": "team/repo",
        "web_url": "https://forge-a.test", "api_url": "https://forge-a.test/api",
    }
    sched = Scheduler(store, read_only=True, source_control_factories={
        "forge-a": RoutedSyntheticProvider,
    })
    monkeypatch.setattr(sched, "repo_for", lambda _task: garden.parent / "repo")
    monkeypatch.setattr("garden.scheduler.gitops.remote_url", lambda _repo: "https://forge-b.test/team/repo.git")
    with pytest.raises(GitError, match="does not match configured source-control repository"):
        sched.slug_for(store.task("DM-001"))
