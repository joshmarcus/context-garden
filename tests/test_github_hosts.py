"""Explicit GitHub host routing never inherits an ambient gh selection."""

from __future__ import annotations

import pytest

from garden.github import GitHub, GitHubError, GitHubRouter, RepositorySlug, repo_slug_from_remote


@pytest.mark.parametrize("remote", [
    "https://forge-one.test/team/repo.git",
    "ssh://git@forge-one.test/team/repo.git",
    "git@forge-one.test:team/repo.git",
    "ssh://acct-1234@forge-one.test/team/repo.git",
    "acct-1234@forge-one.test:team/repo.git",
    "forge-one.test:team/repo.git",
])
def test_enterprise_remote_forms_require_the_configured_host(remote: str):
    assert repo_slug_from_remote(remote, "forge-one.test") == "team/repo"
    assert repo_slug_from_remote(remote, "forge-two.test") is None


@pytest.mark.parametrize("remote", [
    "https://token@forge-one.test/team/repo.git",
    "git@forge-one.test:team/repo/extra.git",
])
def test_ambiguous_enterprise_remotes_are_rejected(remote: str):
    assert repo_slug_from_remote(remote, "forge-one.test") is None


def test_gh_operations_qualify_the_enterprise_host(monkeypatch):
    gh = GitHub(use_gh=False, host="forge-one.test", token="one")
    gh.gh = "/usr/bin/gh"
    calls: list[tuple[str, ...]] = []

    def fake_run(command, **kwargs):
        calls.append(tuple(command))
        class Result:
            returncode = 0
            stdout = "[]"
            stderr = ""
        return Result()

    monkeypatch.setattr("garden.github.subprocess.run", fake_run)
    assert gh.find_pr("team/repo", "feature") is None
    command = calls[0]
    assert ("-R", "forge-one.test/team/repo") == command[3:5]


def test_authentication_check_names_the_enterprise_host(monkeypatch):
    gh = GitHub(use_gh=False, host="forge-one.test", token="one")
    gh.gh = "/usr/bin/gh"
    calls = []

    def fake_run(command, **kwargs):
        calls.append(tuple(command))
        class Result:
            returncode = 0
        return Result()

    monkeypatch.setattr("garden.github.subprocess.run", fake_run)
    assert gh.is_authenticated()
    assert calls == [("/usr/bin/gh", "auth", "status", "--hostname", "forge-one.test")]


def test_router_keeps_rest_tokens_and_requests_on_their_own_hosts(monkeypatch):
    one = GitHub(use_gh=False, host="forge-one.test", api_base="https://forge-one.test/api/v3", token="one")
    two = GitHub(use_gh=False, host="forge-two.test", api_base="https://forge-two.test/api/v3", token="two")
    router = GitHubRouter(GitHub(use_gh=False), {"team/one": one, "team/two": two})
    requests = []

    class Response:
        status_code = 200
        content = b"[]"
        text = "[]"
        def json(self):
            return []

    def fake_request(method, url, **kwargs):
        requests.append((method, url, kwargs["headers"]["Authorization"]))
        return Response()

    monkeypatch.setattr("garden.github.httpx.request", fake_request)
    assert router.find_pr("team/one", "branch") is None
    assert router.find_pr("team/two", "branch") is None
    assert requests == [
        ("GET", "https://forge-one.test/api/v3/repos/team/one/pulls", "Bearer one"),
        ("GET", "https://forge-two.test/api/v3/repos/team/two/pulls", "Bearer two"),
    ]


def test_missing_scoped_token_never_falls_back_to_the_ambient_token(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "public-token")
    gh = GitHub(use_gh=False, host="forge-one.test", token_env="FORGE_ONE_TOKEN")
    assert not gh.available
    with pytest.raises(GitHubError, match="no GitHub token"):
        gh.find_pr("team/repo", "branch")


@pytest.mark.parametrize("api_base", [
    "https://forge-two.test/api/v3",
    "https://token@forge-one.test/api/v3",
    "https://forge-one.test:8443/api/v3",
    "https://forge-one.test/api/v3?token=wrong",
    "https://forge-one.test/api/v3#wrong",
])
def test_explicit_api_base_cannot_redirect_a_scoped_token(api_base: str):
    with pytest.raises(ValueError, match="configured GitHub host"):
        GitHub(use_gh=False, host="forge-one.test", api_base=api_base, token="one")


def test_same_slug_on_two_hosts_keeps_rest_tokens_isolated(monkeypatch):
    one = GitHub(use_gh=False, host="forge-one.test", token="one")
    two = GitHub(use_gh=False, host="forge-two.test", token="two")
    router = GitHubRouter(GitHub(use_gh=False), {
        ("forge-one.test", "team/repo"): one,
        ("forge-two.test", "team/repo"): two,
    })
    requests = []

    class Response:
        status_code = 200
        content = b"[]"
        text = "[]"
        def json(self):
            return []

    def fake_request(method, url, **kwargs):
        requests.append((url, kwargs["headers"]["Authorization"]))
        return Response()

    monkeypatch.setattr("garden.github.httpx.request", fake_request)
    assert router.find_pr(RepositorySlug("team/repo", "forge-one.test"), "branch") is None
    assert router.find_pr(RepositorySlug("team/repo", "forge-two.test"), "branch") is None
    assert requests == [
        ("https://forge-one.test/api/v3/repos/team/repo/pulls", "Bearer one"),
        ("https://forge-two.test/api/v3/repos/team/repo/pulls", "Bearer two"),
    ]


def test_config_accepts_a_service_account_scp_repository(tmp_path):
    from garden.config import Config

    (tmp_path / "garden.yaml").write_text("""
products:
  enterprise:
    repo: acct-1234@forge-one.test:team/repo.git
""")
    assert Config.load(tmp_path).product_repo("enterprise") == "acct-1234@forge-one.test:team/repo.git"


def test_two_host_routes_keep_nondefault_pr_bases_separate(monkeypatch):
    one = GitHub(use_gh=False, host="forge-one.test", token="one")
    two = GitHub(use_gh=False, host="forge-two.test", token="two")
    router = GitHubRouter(GitHub(use_gh=False), {"team/one": one, "team/two": two})
    requests = []

    class Response:
        status_code = 201
        content = b"{}"
        text = "{}"
        def json(self):
            return {"number": 7, "html_url": "https://forge-one.test/team/one/pull/7", "state": "open",
                    "head": {"ref": "work"}, "base": {"ref": "release"}}

    def fake_request(method, url, **kwargs):
        requests.append((url, kwargs["headers"]["Authorization"], kwargs["json"]["base"]))
        return Response()

    monkeypatch.setattr("garden.github.httpx.request", fake_request)
    router.create_pr("team/one", "work", "release-one", "one", "body")
    router.create_pr("team/two", "work", "release-two", "two", "body")
    assert requests == [
        ("https://forge-one.test/api/v3/repos/team/one/pulls", "Bearer one", "release-one"),
        ("https://forge-two.test/api/v3/repos/team/two/pulls", "Bearer two", "release-two"),
    ]


def test_gh_rejects_a_pr_url_from_another_host(monkeypatch):
    gh = GitHub(use_gh=False, host="forge-one.test", token="one")
    gh.gh = "/usr/bin/gh"
    monkeypatch.setattr(gh, "_gh", lambda *args, input_=None: "https://forge-two.test/team/repo/pull/7\n")
    with pytest.raises(GitHubError, match="outside"):
        gh.create_pr("team/repo", "feature", "release", "title", "body")
