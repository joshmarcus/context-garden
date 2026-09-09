"""Remote checks must never confuse an old green build with this checkout."""
import importlib.util
import json
from io import StringIO
from pathlib import Path

import pytest
import yaml

from garden.brief import build_brief, resume_prompt
from garden.scheduler.reap import ReapMixin
from garden.store import Store

SPEC = importlib.util.spec_from_file_location("worker_ci", Path(__file__).parents[1] / "scripts/check_ci.py")
ci = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ci)
SHA = "a" * 40
BRANCH = "garden/test-worker"


class PublicResponse(StringIO):
    def __init__(self, payload, headers):
        super().__init__(json.dumps(payload))
        self.headers = headers

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None


def run(**overrides):
    return {"databaseId": 12, "headSha": SHA, "headBranch": BRANCH, "event": "push",
            "status": "completed", "conclusion": "success", "url": "https://github.com/o/r/actions/runs/12",
            **overrides}


@pytest.fixture
def fake(monkeypatch):
    state = {"runs": [run()], "branch": BRANCH, "sha": SHA, "dirty": "", "remote_sha": SHA, "calls": []}
    monkeypatch.delenv("GARDEN_BRANCH", raising=False)

    def command(*args):
        state["calls"].append(args)
        if args[:3] == ("gh", "auth", "status"):
            return "github.com\n  ✓ Logged in"
        if "status" in args:
            return state["dirty"]
        if "symbolic-ref" in args:
            return state["branch"]
        if "rev-parse" in args:
            return state["sha"]
        if "get-url" in args:
            return "https://github.com/o/r.git"
        if args[0] == "git" and "push" in args:
            return ""
        if "ls-remote" in args:
            return state["remote_sha"] + " refs/heads/" + BRANCH
        if "list" in args:
            return json.dumps(state["runs"])
        if "--log-failed" in args:
            return "FAILED tests/test_regression.py::test_behavior"
        raise AssertionError(args)

    monkeypatch.setattr(ci, "command", command)
    return state


def test_push_and_reuse_exact_run(fake, capsys):
    ci.check_ci()
    ci.check_ci()
    calls = fake["calls"]
    pushes = [c for c in calls if c[0] == "git" and "push" in c]
    assert len(pushes) == 2
    assert all(c[-3:] == ("push", "origin", SHA + ":refs/heads/" + BRANCH) for c in pushes)
    assert not any("--force" in a or a in {"rerun", "create", "config", "--set-upstream"} for c in calls for a in c)
    assert "PASS " + SHA in capsys.readouterr().out


@pytest.mark.parametrize("conclusion", ["failure", "cancelled", "skipped", "timed_out", "neutral", None])
def test_non_success_is_failure_with_log(fake, capsys, conclusion):
    fake["runs"] = [run(conclusion=conclusion)]
    with pytest.raises(ci.CIError, match="did not pass"):
        ci.check_ci()
    assert "test_regression.py" in capsys.readouterr().err


@pytest.mark.parametrize("runs", [[], [run(headSha="b" * 40)], [run(headBranch="garden/other")],
                                   [run(event="pull_request")], [run(status="in_progress", conclusion=None)]])
def test_absent_stale_or_pending_times_out(fake, monkeypatch, runs):
    fake["runs"] = runs
    ticks = iter([0, 0, 2, 2])
    monkeypatch.setattr(ci.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(ci.time, "sleep", lambda _: None)
    with pytest.raises(ci.CIError, match="Timed out"):
        ci.check_ci(timeout=1)


def test_latest_attempt_wins_over_older_success(fake):
    fake["runs"] = [run(), run(databaseId=13, conclusion="failure")]
    with pytest.raises(ci.CIError, match="did not pass"):
        ci.check_ci()


@pytest.mark.parametrize("key,value", [("dirty", " M src/a.py"), ("branch", "main")])
def test_invalid_checkout_never_pushes(fake, key, value):
    fake[key] = value
    with pytest.raises(ci.CIError):
        ci.check_ci()
    assert not any(c[0] == "git" and "push" in c for c in fake["calls"])


def test_wrong_assigned_branch_never_pushes(fake, monkeypatch):
    monkeypatch.setenv("GARDEN_BRANCH", "garden/other")
    with pytest.raises(ci.CIError, match="not assigned"):
        ci.check_ci()
    assert not any(c[0] == "git" and "push" in c for c in fake["calls"])


def test_remote_movement_cannot_pass(fake):
    fake["remote_sha"] = "b" * 40
    with pytest.raises(ci.CIError, match="Remote branch changed"):
        ci.check_ci()


def test_edits_while_waiting_cannot_pass(fake, monkeypatch):
    original = ci.command

    def move(*args):
        result = original(*args)
        if "list" in args:
            fake["dirty"] = " M file.py"
        return result

    monkeypatch.setattr(ci, "command", move)
    with pytest.raises(ci.CIError, match="uncommitted"):
        ci.check_ci()


def test_api_failure_cannot_pass(fake, monkeypatch):
    original = ci.command

    def unavailable(*args):
        if "list" in args:
            raise ci.CIError("GitHub unavailable")
        return original(*args)

    monkeypatch.setattr(ci, "command", unavailable)
    with pytest.raises(ci.CIError, match="unavailable"):
        ci.check_ci()


def test_public_rest_path_handles_the_official_ssh_alias(fake, monkeypatch, capsys):
    def no_gh(*args):
        if args[:3] == ("gh", "auth", "status"):
            raise ci.CIError("gh could not finish: not logged in")
        return fake_command(*args)

    fake_command = ci.command
    monkeypatch.setattr(ci, "command", no_gh)
    monkeypatch.setattr(ci, "public_workflow_runs", lambda repo, branch, sha: fake["runs"])
    # The production deploy-key transport has no authenticated gh identity.
    original = fake_command

    def ssh_remote(*args):
        if "get-url" in args:
            return "ssh://git@ssh.github.com:443/o/r.git"
        return original(*args)

    monkeypatch.setattr(ci, "command", lambda *args: no_gh(*args) if "get-url" not in args else ssh_remote(*args))
    ci.check_ci()
    assert "public GitHub Actions metadata" in capsys.readouterr().out


def test_public_rest_rate_limit_and_malformed_results_cannot_pass(fake, monkeypatch):
    monkeypatch.setattr(ci, "authenticated_gh", lambda _: False)
    monkeypatch.setattr(ci, "public_workflow_runs", lambda *_: (_ for _ in ()).throw(
        ci.CIError("public GitHub Actions API rate limit is exhausted")))
    with pytest.raises(ci.CIError, match="rate limit"):
        ci.check_ci()


def test_public_rest_normalizes_and_validates_github_response(monkeypatch):
    response = PublicResponse({"workflow_runs": [{
        "id": 12, "head_sha": SHA, "head_branch": BRANCH, "event": "push",
        "status": "completed", "conclusion": "success", "html_url": "https://github.com/o/r/actions/runs/12",
        "run_attempt": 2,
    }]}, {"X-RateLimit-Remaining": "59"})
    seen = []
    monkeypatch.setattr(ci, "urlopen", lambda request, timeout: seen.append((request, timeout)) or response)
    assert ci.public_workflow_runs("github.com/o/r", BRANCH, SHA) == [
        {"databaseId": 12, "headSha": SHA, "headBranch": BRANCH, "event": "push",
         "status": "completed", "conclusion": "success",
         "url": "https://github.com/o/r/actions/runs/12", "attempt": 2}
    ]
    assert "head_sha=" + SHA in seen[0][0].full_url
    assert seen[0][1] == 30


@pytest.mark.parametrize("payload,headers", [
    ({"workflow_runs": [{}]}, {"X-RateLimit-Remaining": "59"}),
    ({"workflow_runs": []}, {"X-RateLimit-Remaining": "not-a-number"}),
    ({"workflow_runs": []}, {"X-RateLimit-Remaining": "0"}),
])
def test_public_rest_malformed_or_limited_response_fails(monkeypatch, payload, headers):
    response = PublicResponse(payload, headers)
    monkeypatch.setattr(ci, "urlopen", lambda *_, **__: response)
    with pytest.raises(ci.CIError):
        ci.public_workflow_runs("github.com/o/r", BRANCH, SHA)


def test_public_rest_uses_conservative_poll_floor(fake, monkeypatch):
    assert ci.PUBLIC_API_POLL_SECONDS == 65
    monkeypatch.setattr(ci, "authenticated_gh", lambda _: False)
    monkeypatch.setattr(ci, "public_workflow_runs", lambda *_: [])
    ticks = iter([0, 0, 0, 1])
    sleeps = []
    monkeypatch.setattr(ci.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(ci.time, "sleep", sleeps.append)
    with pytest.raises(ci.CIError, match="Timed out"):
        ci.check_ci(timeout=1, poll=1)
    assert sleeps == [1]


def test_repository_uses_explicit_push_host():
    assert ci.repository("git@ghe.example:team/repo.git") == "ghe.example/team/repo"
    assert ci.repository("https://github.com/team/repo.git") == "github.com/team/repo"
    assert ci.repository("ssh://git@ssh.github.com:443/team/repo.git") == "github.com/team/repo"
    for remote in ["ssh://git@ssh.github.com:22/team/repo.git", "https://token@github.com/team/repo.git",
                   "ssh://alice@ssh.github.com:443/team/repo.git"]:
        with pytest.raises(ci.CIError):
            ci.repository(remote)
    with pytest.raises(ci.CIError):
        ci.repository("/tmp/unrelated.git")


def test_brief_ci_permission_is_explicit_per_product(garden):
    path = garden / "garden.yaml"
    config = yaml.safe_load(path.read_text())
    for value, allowed in [(None, False), (False, False), ("true", False), (True, True)]:
        config["products"]["demo"]["setup"] = {"worker_push": value, "test": "python scripts/check_ci.py"}
        path.write_text(yaml.safe_dump(config))
        store = Store(garden)
        text = build_brief(store, store.task("DM-001")).text
        assert ("You may push ONLY this assigned branch" in text) == allowed
        assert ("Do NOT push and do NOT open" in text) != allowed
        assert ("python scripts/check_ci.py" in text) == allowed
        assert ("do not run it until the product explicitly sets setup.worker_push: true" in text) != allowed
    assert "original brief's push/CI rules" in resume_prompt("q", "a")


def test_publishing_ci_check_waits_for_explicit_worker_push_permission(garden):
    path = garden / "garden.yaml"
    config = yaml.safe_load(path.read_text())
    config["products"]["demo"]["setup"] = {"test": "python3 scripts/check_ci.py"}
    path.write_text(yaml.safe_dump(config))
    store = Store(garden)

    class Scheduler:
        cfg = store.config

    specs = ReapMixin._pre_pr_specs(Scheduler(), store.task("DM-001"))
    assert specs == [{"name": "test", "command": "python3 scripts/check_ci.py", "requires_worker_push": True}]

    config["products"]["demo"]["setup"]["worker_push"] = True
    path.write_text(yaml.safe_dump(config))
    store = Store(garden)
    Scheduler.cfg = store.config
    assert ReapMixin._pre_pr_specs(Scheduler(), store.task("DM-001")) == [
        {"name": "test", "command": "python3 scripts/check_ci.py"}
    ]


def test_repository_ci_runs_before_pr_and_keeps_full_suite():
    # BaseLoader preserves the YAML key 'on' under both YAML 1.1 and 1.2.
    cfg = yaml.load((Path(__file__).parents[1] / ".github/workflows/ci.yml").read_text(), Loader=yaml.BaseLoader)
    assert set(cfg["on"]["push"]["branches"]) >= {"garden/**", "codex/**", "main"}
    assert "pull_request" in cfg["on"]
    assert any(
        s.get("run") == "pytest -q --durations=40 --timeout=120 --timeout-method=thread"
        for s in cfg["jobs"]["test"]["steps"]
    )
