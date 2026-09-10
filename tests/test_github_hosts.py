"""Explicit GitHub host routing never inherits an ambient gh selection."""

from __future__ import annotations

import json

import pytest

from garden.github import (
    GitHub,
    GitHubError,
    GitHubRouter,
    PRInfo,
    RepositorySlug,
    is_git_remote_url,
    pull_request_number,
    repo_slug_from_remote,
)


@pytest.mark.parametrize("url", [
    "https://operator:synthetic-password@forge-one.test/Team/Repo/pull/7",
    "https://operator@forge-one.test/Team/Repo/pull/7",
    "https://forge-one.test/Team/Repo/pull/7?access=synthetic-token",
    "https://forge-one.test/Team/Repo/pull/7#synthetic-fragment",
    "https://forge-one.test:8443/Team/Repo/pull/7",
])
def test_pull_request_number_rejects_credential_bearing_and_ambiguous_urls(url: str):
    assert pull_request_number(url, "Team/Repo", "forge-one.test") is None


@pytest.mark.parametrize("url", [
    "https://forge-one.test/Team/Repo/pull/7",
    "https://FORGE-ONE.TEST/tEAM/rEPO/pull/7",
    "https://github.com/Team/Repo/pull/7",
])
def test_pull_request_number_accepts_configured_host_and_repository_case_variants(url: str):
    host = "github.com" if "github.com" in url else "forge-one.test"
    assert pull_request_number(url, "Team/Repo", host) == 7


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


def test_official_github_ssh_port_remote_is_recognized():
    remote = "ssh://git@ssh.github.com:443/team/repo.git"
    assert repo_slug_from_remote(remote) == "team/repo"
    assert repo_slug_from_remote(remote, "ssh.github.com") is None


@pytest.mark.parametrize("remote", [
    "ssh://acct-1234@forge-one.test:443/team/repo.git",
    "ssh://acct-1234@forge-one.test:2222/team/repo.git",
])
def test_enterprise_ssh_ports_keep_the_configured_host_identity(remote: str):
    assert repo_slug_from_remote(remote, "forge-one.test") == "team/repo"
    assert repo_slug_from_remote(remote, "forge-two.test") is None


@pytest.mark.parametrize("remote", [
    "ssh://acct-1234@forge-one.test:0/team/repo.git",
    "ssh://acct-1234@forge-one.test:65536/team/repo.git",
    f"ssh://acct-1234@forge-one.test:{'9' * 5_000}/team/repo.git",
    "ssh://acct-1234@forge-one.test:bad/team/repo.git",
    "ssh://acct-1234:secret@forge-one.test:443/team/repo.git",
])
def test_invalid_enterprise_ssh_remotes_are_not_routed(remote: str):
    assert repo_slug_from_remote(remote, "forge-one.test") is None


@pytest.mark.parametrize("remote", [
    "https://forge-one.test/tEam/rEpo.git",
    "ssh://git@forge-one.test/tEam/rEpo.git",
    "ssh://acct-1234@forge-one.test/tEam/rEpo.git",
    "ssh://acct-1234@forge-one.test:443/tEam/rEpo.git",
    "ssh://acct-1234@forge-one.test:2222/tEam/rEpo.git",
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


def test_rest_pr_preserves_explicit_merge_conflict(monkeypatch):
    github = GitHub(use_gh=False, token="scoped-token")
    responses = {
        "/repos/team/repo/pulls/7": {
            "number": 7, "html_url": "https://github.com/team/repo/pull/7",
            "state": "open", "mergeable": False,
            "head": {"ref": "feature", "sha": "head-7"}, "base": {"ref": "main"},
        },
        "/repos/team/repo/commits/head-7/check-runs": {"check_runs": []},
        "/repos/team/repo/commits/head-7/status": {"statuses": []},
        "/repos/team/repo/pulls/7/reviews": [],
    }
    monkeypatch.setattr(github, "_rest", lambda method, path, **kwargs: responses[path])

    assert github.get_pr("team/repo", 7).mergeable == "CONFLICTING"


def test_rest_pr_paginates_check_runs_before_computing_rollup(monkeypatch):
    github = GitHub(use_gh=False, token="scoped-token")
    pull = {
        "number": 7, "html_url": "https://github.com/team/repo/pull/7",
        "state": "open", "mergeable": True,
        "head": {"ref": "feature", "sha": "head-7"}, "base": {"ref": "main"},
    }
    pages = []

    def rest(method, path, **kwargs):
        if path.endswith("/check-runs"):
            page = kwargs["params"]["page"]
            pages.append(page)
            if page == 1:
                return {"total_count": 101, "check_runs": [
                    {"name": f"pass-{i}", "status": "completed", "conclusion": "success"}
                    for i in range(100)
                ]}
            return {"total_count": 101, "check_runs": [
                {"name": "late-failure", "status": "completed", "conclusion": "failure"}
            ]}
        if path.endswith("/status"):
            return {"statuses": []}
        if path.endswith("/reviews"):
            return []
        return pull

    monkeypatch.setattr(github, "_rest", rest)

    pr = github.get_pr("team/repo", 7)
    assert pages == [1, 2]
    assert pr.checks == "FAILURE" and pr.failed_checks == ["late-failure"]


@pytest.mark.parametrize("status, expected", [(403, "PERMISSION"), (503, "UNAVAILABLE")])
def test_rest_pr_preserves_check_rollup_fetch_errors(monkeypatch, status, expected):
    github = GitHub(use_gh=False, token="scoped-token")
    pull = {
        "number": 7, "html_url": "https://github.com/team/repo/pull/7",
        "state": "open", "mergeable": True,
        "head": {"ref": "feature", "sha": "head-7"}, "base": {"ref": "main"},
    }

    def rest(method, path, **kwargs):
        if path.endswith(("/check-runs", "/status")):
            raise GitHubError(f"GET {path}: {status} synthetic failure")
        if path.endswith("/reviews"):
            return []
        return pull

    monkeypatch.setattr(github, "_rest", rest)

    assert github.get_pr("team/repo", 7).checks == expected


@pytest.mark.parametrize(
    ("blocked_path", "accessible_path", "accessible_response", "expected", "failed_checks"),
    [
        ("/check-runs", "/status", {"statuses": [
            {"context": "external/validation", "state": "failure"}
        ]}, "FAILURE", ["external/validation"]),
        ("/status", "/check-runs", {"check_runs": [
            {"name": "actions/unit", "status": "completed", "conclusion": "success"}
        ]}, "SUCCESS", []),
    ],
)
def test_rest_pr_uses_accessible_check_source_when_other_is_forbidden(
    monkeypatch, blocked_path, accessible_path, accessible_response, expected, failed_checks
):
    github = GitHub(use_gh=False, token="scoped-token")
    pull = {
        "number": 7, "html_url": "https://github.com/team/repo/pull/7",
        "state": "open", "mergeable": True,
        "head": {"ref": "feature", "sha": "head-7"}, "base": {"ref": "main"},
    }

    def rest(method, path, **kwargs):
        if path.endswith(blocked_path):
            raise GitHubError(f"GET {path}: 403 synthetic failure")
        if path.endswith(accessible_path):
            return accessible_response
        if path.endswith("/reviews"):
            return []
        return pull

    monkeypatch.setattr(github, "_rest", rest)

    pr = github.get_pr("team/repo", 7)
    assert pr.checks == expected
    assert pr.failed_checks == failed_checks


def test_rest_pr_combines_commit_statuses_with_check_runs(monkeypatch):
    github = GitHub(use_gh=False, token="scoped-token")
    pull = {
        "number": 7, "html_url": "https://github.com/team/repo/pull/7",
        "state": "open", "mergeable": True,
        "head": {"ref": "feature", "sha": "head-7"}, "base": {"ref": "main"},
    }

    def rest(method, path, **kwargs):
        if path.endswith("/check-runs"):
            return {"check_runs": [
                {"name": "unit", "status": "completed", "conclusion": "success"}
            ]}
        if path.endswith("/status"):
            return {"statuses": [
                {"context": "external/validation", "state": "failure"}
            ]}
        if path.endswith("/reviews"):
            return []
        return pull

    monkeypatch.setattr(github, "_rest", rest)

    pr = github.get_pr("team/repo", 7)
    assert pr.checks == "FAILURE"
    assert pr.failed_checks == ["external/validation"]


def test_rest_open_pr_list_propagates_pr_detail_failure(monkeypatch):
    github = GitHub(use_gh=False, token="scoped-token")
    github._me = "operator"

    def rest(method, path, **kwargs):
        if path == "/search/issues":
            return {"items": [{"number": 7}]}
        raise GitHubError(f"GET {path}: 503 synthetic detail failure")

    monkeypatch.setattr(github, "_rest", rest)

    with pytest.raises(GitHubError, match="synthetic detail failure"):
        github.list_open_prs("team/repo")


def test_gh_open_pr_list_queries_only_current_and_project_users(monkeypatch):
    github = GitHub(use_gh=True)
    github.gh = "gh"  # Exercise the mocked CLI backend even when gh is not on the test PATH.
    calls: list[tuple[str, ...]] = []

    def gh(*args, **kwargs):
        if args[:2] == ("api", "user"):
            return "operator\n"
        calls.append(args)
        author = args[args.index("--author") + 1]
        number = 1 if author == "operator" else 2
        return json.dumps([{
            "number": number,
            "url": f"https://github.com/team/repo/pull/{number}",
            "state": "OPEN",
            "title": author,
            "author": {"login": author},
            "updatedAt": f"2026-01-0{number}T00:00:00Z",
        }])

    monkeypatch.setattr(github, "_gh", gh)

    prs = github.list_open_prs("team/repo", ["maintainer", "operator"])

    assert [call[call.index("--author") + 1] for call in calls] == ["maintainer", "operator"]
    assert [pr.author for pr in prs] == ["maintainer", "operator"]
    assert all(call[call.index("--limit") + 1] == "1000" for call in calls)


def test_rest_open_pr_list_searches_each_relevant_author_without_listing_repository(monkeypatch):
    github = GitHub(use_gh=False, token="scoped-token")
    github._me = "operator"
    searches: list[str] = []

    def rest(method, path, **kwargs):
        assert path == "/search/issues"
        searches.append(kwargs["params"]["q"])
        author = kwargs["params"]["q"].rsplit("author:", 1)[1]
        return {"items": [{"number": 1 if author == "operator" else 2}]}

    monkeypatch.setattr(github, "_rest", rest)
    monkeypatch.setattr(
        github,
        "get_pr",
        lambda slug, number: PRInfo(
            number, f"https://github.com/{slug}/pull/{number}", "OPEN", updated_at=str(number)
        ),
    )

    assert [pr.number for pr in github.list_open_prs("team/repo", ["maintainer"])] == [2, 1]
    assert searches == [
        "repo:team/repo is:pr is:open author:maintainer",
        "repo:team/repo is:pr is:open author:operator",
    ]


def test_project_users_inherit_globally_and_allow_product_override(tmp_path):
    from garden.config import Config

    (tmp_path / "garden.yaml").write_text("""
github:
  project_users: [shared-maintainer]
products:
  inherited:
    github: team/inherited
  overridden:
    github:
      slug: team/overridden
      project_users: [repo-maintainer]
  current-only:
    github:
      slug: team/current-only
      project_users: []
""")

    config = Config.load(tmp_path)
    assert config.product_project_users("inherited") == ["shared-maintainer"]
    assert config.product_project_users("overridden") == ["repo-maintainer"]
    assert config.product_project_users("current-only") == []


@pytest.mark.parametrize("value", ["maintainer", [""], [1]])
def test_project_users_reject_invalid_values(tmp_path, value):
    from garden.config import Config

    (tmp_path / "garden.yaml").write_text(
        "github:\n  project_users: " + json.dumps(value) + "\n"
    )
    with pytest.raises(ValueError, match="github.project_users"):
        Config.load(tmp_path)


@pytest.mark.parametrize("repo", [
    "acct-1234@forge-one.test:team/repo.git",
    "forge-one.test:team/repo.git",
])
def test_config_preserves_scp_repository_references(tmp_path, repo):
    from garden.config import Config

    (tmp_path / "garden.yaml").write_text(f"""
products:
  enterprise:
    repo: {repo}
""")
    assert Config.load(tmp_path).product_repo("enterprise") == repo


@pytest.mark.parametrize("path", ["C:/garden/repo", r"C:\garden\repo", r"\\server\share\repo"])
def test_remote_classifier_preserves_windows_path_spellings(path):
    assert not is_git_remote_url(path)


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
