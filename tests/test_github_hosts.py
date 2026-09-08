"""Explicit GitHub host routing never inherits an ambient gh selection."""

from __future__ import annotations

import pytest

from garden.github import GitHub, GitHubError, GitHubRouter, RepositorySlug, repo_slug_from_remote


@pytest.mark.parametrize("remote", [
    "https://github.com/team/repo.git",
    "git@github.com:team/repo.git",
    "ssh://git@github.com:22/team/repo.git",
    "ssh://git@ssh.github.com:443/team/repo.git",
])
def test_public_github_transports_resolve_to_one_repository(remote: str):
    assert repo_slug_from_remote(remote) == "team/repo"


def test_public_ssh_alias_is_not_an_enterprise_route():
    assert repo_slug_from_remote("ssh://git@ssh.github.com:443/team/repo.git", "ghe.example") is None


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


@pytest.mark.parametrize("remote", [
    "https://forge-one.test/tEam/rEpo.git",
    "ssh://git@forge-one.test/tEam/rEpo.git",
    "ssh://acct-1234@forge-one.test/tEam/rEpo.git",
    "acct-1234@forge-one.test:tEam/rEpo.git",
])
def test_scheduler_accepts_case_variants_of_configured_repository(garden, monkeypatch, remote):
    from garden.scheduler import Scheduler
    from garden.store import Store

    store = Store(garden)
    store.config.data["products"]["demo"]["github"] = {"host": "forge-one.test", "slug": "Team/Repo"}
    sched = Scheduler(store, read_only=True)
    monkeypatch.setattr(sched, "repo_for", lambda _task: garden.parent / "repo")
    monkeypatch.setattr("garden.scheduler.gitops.remote_url", lambda _repo: remote)

    identity = sched.slug_for(store.task("DM-001"))

    assert str(identity) == "Team/Repo"
    assert identity.host == "forge-one.test"


@pytest.mark.parametrize("remote", [
    "https://forge-two.test/Team/Repo.git",
    "https://forge-one.test/Other/Repo.git",
    "ssh://acct-1234@forge-one.test/Team/Other.git",
    "https://token@forge-one.test/Team/Repo.git",
])
def test_scheduler_rejects_a_different_or_invalid_repository(garden, monkeypatch, remote):
    from garden.gitops import GitError
    from garden.scheduler import Scheduler
    from garden.store import Store

    store = Store(garden)
    store.config.data["products"]["demo"]["github"] = {"host": "forge-one.test", "slug": "Team/Repo"}
    sched = Scheduler(store, read_only=True)
    monkeypatch.setattr(sched, "repo_for", lambda _task: garden.parent / "repo")
    monkeypatch.setattr("garden.scheduler.gitops.remote_url", lambda _repo: remote)

    with pytest.raises(GitError, match="does not match configured GitHub host and repository"):
        sched.slug_for(store.task("DM-001"))


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


@pytest.mark.parametrize("host,api_base,rest_url,graphql_url", [
    ("github.com", "", "https://api.github.com", "https://api.github.com/graphql"),
    ("forge-one.test", "", "https://forge-one.test/api/v3", "https://forge-one.test/api/graphql"),
    ("forge-one.test", "https://forge-one.test/prefix/api/v3",
     "https://forge-one.test/prefix/api/v3", "https://forge-one.test/prefix/api/graphql"),
])
def test_mark_ready_uses_the_host_graphql_endpoint(monkeypatch, host, api_base, rest_url, graphql_url):
    import httpx

    github = GitHub(use_gh=False, host=host, api_base=api_base, token="scoped-token")
    calls = []

    def request(method, url, **kwargs):
        calls.append((method, url, kwargs["headers"]["Authorization"]))
        if method == "GET":
            return httpx.Response(200, json={"node_id": "PR_fixture"})
        assert kwargs["json"]["variables"] == {"id": "PR_fixture"}
        return httpx.Response(200, json={"data": {"markPullRequestReadyForReview": {}}})

    monkeypatch.setattr("garden.github.httpx.request", request)
    github.mark_ready("Team/Repo", 7)

    assert calls == [
        ("GET", rest_url + "/repos/Team/Repo/pulls/7", "Bearer scoped-token"),
        ("POST", graphql_url, "Bearer scoped-token"),
    ]


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


@pytest.mark.parametrize("copy_kind", ["shallow", "deep", "pickle"])
def test_repository_host_survives_copy_and_serialization(copy_kind):
    import copy
    import pickle

    slug = RepositorySlug("Team/Repo", "FORGE-ONE.TEST.")
    copied = {"shallow": copy.copy, "deep": copy.deepcopy,
              "pickle": lambda value: pickle.loads(pickle.dumps(value))}[copy_kind](slug)
    assert copied == "Team/Repo"
    assert copied.host == "forge-one.test"


def test_public_host_uses_default_client_without_an_explicit_route(monkeypatch):
    default = GitHub(use_gh=False, token="public")
    enterprise = GitHub(use_gh=False, host="forge-one.test", token="enterprise")
    router = GitHubRouter(default, {("forge-one.test", "team/repo"): enterprise})
    calls = []
    monkeypatch.setattr(default, "mark_ready", lambda slug, number: calls.append((str(slug), number)))
    monkeypatch.setattr(enterprise, "mark_ready", lambda *args: pytest.fail("cross-host route"))

    router.mark_ready(RepositorySlug("team/repo", "github.com"), 7)

    assert calls == [("team/repo", 7)]


@pytest.mark.parametrize("slug", [
    RepositorySlug("team/repo", "unconfigured.test"),
    "team/repo",
])
def test_unconfigured_or_ambiguous_host_never_uses_ambient_client(monkeypatch, slug):
    default = GitHub(use_gh=False, token="public")
    one = GitHub(use_gh=False, host="forge-one.test", token="one")
    two = GitHub(use_gh=False, host="forge-two.test", token="two")
    router = GitHubRouter(default, {("forge-one.test", "team/repo"): one,
                                  ("forge-two.test", "team/repo"): two})
    for client in (default, one, two):
        monkeypatch.setattr(client, "mark_ready", lambda *args: pytest.fail("unknown host made a request"))

    with pytest.raises(GitHubError, match="host|ambiguous"):
        router.mark_ready(slug, 7)
