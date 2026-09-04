"""Per-task usage rollups, trial cost columns, config overlays."""

import yaml

from garden.checks import github_actions_failures
from garden.config import Config
from garden.runs import RunStore
from garden.trials import TrialLog, ranking_markdown
from tests.conftest import wait_for_runs


def test_usage_rollup_per_task_and_mode(sched, fake_github):
    from garden.github import Feedback

    sched.tick()
    wait_for_runs(sched)
    sched.tick()
    pr = fake_github.prs["garden/dm-001-first-task"]
    pr.updated_at = "t2"
    fake_github.feedback[pr.number] = Feedback(items=[{"kind": "comment", "author": "josh", "body": "fix", "created": "2099-01-01T00:00:00Z"}])
    sched.tick()
    wait_for_runs(sched)
    sched.tick()
    rs = RunStore(sched.cfg.garden_dir)
    u = rs.usage_for("DM-001")
    assert u["runs"] == 2 and u["input_tokens"] == 2468 and u["output_tokens"] == 642 and u["cache_read_input_tokens"] == 200
    assert u["cost_usd"] == 0.10 and set(u["by_mode"]) == {"work", "revise"} and u["by_mode"]["revise"]["cost_usd"] == 0.05
    assert u["total_tokens"] == 2468 + 642 + 200
    assert rs.usage_by_task()["DM-001"]["runs"] == 2
    assert rs.usage_for("NOPE")["runs"] == 0


def test_trial_records_tokens_and_cost_per_point(sched, fake_github, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_WINNER", "claude:opus")
    sched.start_trial(sched.store.task("DM-001"), ["claude:sonnet", "claude:opus"])
    wait_for_runs(sched)
    sched.tick()
    wait_for_runs(sched)
    sched.tick()
    log = TrialLog(sched.cfg.garden_dir / "trials.jsonl")
    rec = log.read()[-1]
    opus = next(c for c in rec["contenders"] if c["label"] == "claude:opus")
    assert opus["input_tokens"] == 1234 and opus["output_tokens"] == 321 and opus["cost"] == 0.05
    assert rec["compare_cost"] == 0.03
    md = ranking_markdown(rec)
    assert "$ per point" in md and "1,234 / 321" in md and "$0.006" in md and "_comparison run_" in md
    by = {r["label"]: r for r in log.leaderboard()}
    assert by["claude:opus"]["cost_per_point"] == round(0.05 / 9, 4) and by["claude:sonnet"]["cost_per_point"] == round(0.05 / 6, 4)
    assert by["claude:opus"]["avg_input_tokens"] == 1234


def test_config_overlays(tmp_path, monkeypatch):
    (tmp_path / "garden.yaml").write_text(yaml.safe_dump({"name": "g", "max_parallel": 3, "checks": {"ci": [{"name": "actions"}], "pre_pr": [{"name": "t"}]},
                                                          "harnesses": {"claude": {"models": {"easy": "haiku", "hard": "opus"}}}}))
    (tmp_path / "garden.work.yaml").write_text(yaml.safe_dump({"runner": "ssh", "checks": {"ci": [{"name": "jenkins"}]},
                                                               "harnesses": {"claude": {"models": {"hard": "sonnet"}}}}))
    (tmp_path / "garden.local.yaml").write_text(yaml.safe_dump({"max_parallel": 1}))
    home = Config.load(tmp_path, env="")
    assert home.sources == ["garden.yaml", "garden.local.yaml"] and home.get("runner") == "local" and home.get("max_parallel") == 1
    assert [c["name"] for c in home.get("checks.ci")] == ["actions"]
    work = Config.load(tmp_path, env="work")
    assert work.sources == ["garden.yaml", "garden.work.yaml", "garden.local.yaml"] and work.env == "work"
    assert work.get("runner") == "ssh" and work.get("max_parallel") == 1
    assert [c["name"] for c in work.get("checks.ci")] == ["jenkins"]  # lists replace
    assert [c["name"] for c in work.get("checks.pre_pr")] == ["t"]  # untouched by the overlay
    assert work.harness("claude").model_for("hard") == "sonnet" and work.harness("claude").model_for("easy") == "haiku"  # dicts merge
    monkeypatch.setenv("GARDEN_ENV", "work")
    assert Config.load(tmp_path).env == "work"


def test_actions_analyser_needs_gh_context(monkeypatch):
    monkeypatch.setenv("PATH", "/nonexistent")
    out = github_actions_failures({"repo_slug": "a/b", "branch": "x"}, {})
    assert out["status"] == "error" and "gh" in out["summary"]
