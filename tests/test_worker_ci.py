"""Remote checks must never confuse an old green build with this checkout."""
import importlib.util
import json
from pathlib import Path

import pytest
import yaml

from garden.brief import build_brief, resume_prompt
from garden.store import Store

SPEC = importlib.util.spec_from_file_location("worker_ci", Path(__file__).parents[1] / "scripts/check_ci.py")
ci = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ci)
SHA = "a" * 40
BRANCH = "garden/test-worker"


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


def test_repository_uses_explicit_push_host():
    assert ci.repository("git@ghe.example:team/repo.git") == "ghe.example/team/repo"
    assert ci.repository("https://github.com/team/repo.git") == "github.com/team/repo"
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
        assert "python scripts/check_ci.py" in text
    assert "original brief's push/CI rules" in resume_prompt("q", "a")


def test_repository_ci_runs_before_pr_and_keeps_full_suite():
    # BaseLoader preserves the YAML key 'on' under both YAML 1.1 and 1.2.
    cfg = yaml.load((Path(__file__).parents[1] / ".github/workflows/ci.yml").read_text(), Loader=yaml.BaseLoader)
    assert set(cfg["on"]["push"]["branches"]) >= {"garden/**", "codex/**", "main"}
    assert "pull_request" in cfg["on"]
    assert any(s.get("run") == "pytest -q" for s in cfg["jobs"]["test"]["steps"])
