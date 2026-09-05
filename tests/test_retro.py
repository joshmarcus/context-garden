"""`garden retro`: harvest the PR-body friction, run (or reuse) the persona reviews,
reconcile every friction item against what merged, and open a PR to the garden's own repo
with the retro document and a draft of the next phase's goals. Nothing edits the live
garden directly."""

from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from garden.retro import (
    next_phase_name,
    parse_retro,
    reconcile_brief,
    reconciliation_table,
    render_next_goals,
    render_retro_doc,
)
from garden.scheduler import Scheduler
from garden.store import Store
from tests.conftest import FAKE_CLAUDE


# --------------------------------------------------------------------------- pure logic
def test_next_phase_name_increments_the_number():
    assert next_phase_name("phase-02-friction") == "phase-03"
    assert next_phase_name("phase-09") == "phase-10"
    assert next_phase_name("p1") == "p2"
    assert next_phase_name("bootstrap") == "bootstrap-next"


def test_reconciliation_table_marks_each_item_with_a_verdict_and_evidence():
    rev = {"reconciliation": [
        {"item": "worktree has no venv", "logged": "CG-101", "pr": "CG-110", "verdict": "fixed", "evidence": "setup.command runs uv sync"},
        {"item": "brief is missing a spec", "logged": "CG-102", "verdict": "still_true", "evidence": "no task addressed it"},
        {"item": "$GARDEN_ROOT check flake", "logged": "CG-103", "verdict": "outdated", "evidence": "a 20-minute snapshot"},
        {"item": "the copy is wrong", "logged": "CG-104", "verdict": "disputed", "evidence": "reviewers disagree"},
    ]}
    table = reconciliation_table(rev)
    # a header, the verdict words, the evidence and the ids all render
    assert "| Friction item | Logged | Fixed by | Verdict | Evidence |" in table
    for word in ("still true", "fixed", "outdated", "disputed"):
        assert word in table
    assert "CG-101" in table and "CG-110" in table
    assert "setup.command runs uv sync" in table
    # one row per friction item plus the two header rows
    assert len(table.splitlines()) == 2 + 4


def test_reconciliation_table_escapes_pipes():
    rev = {"reconciliation": [{"item": "a | b", "verdict": "fixed", "evidence": "x | y"}]}
    table = reconciliation_table(rev)
    assert "a \\| b" in table and "x \\| y" in table


def test_reconciliation_table_empty():
    assert reconciliation_table({"reconciliation": []}) == "_No friction to reconcile._"


def test_parse_retro_reads_the_last_marker_line():
    text = 'noise\nGARDEN_RETRO: {"reconciliation": [{"item": "x", "verdict": "fixed"}], "summary": "s"}\ntrailer'
    rev = parse_retro(text)
    assert rev["summary"] == "s"
    assert rev["reconciliation"][0]["verdict"] == "fixed"
    assert parse_retro("no marker here") == {}


def test_render_documents_carry_the_verdicts_and_the_next_goals():
    from garden.model import Phase

    phase = Phase(product="context-garden", name="phase-02-friction", path=Path("/x/context-garden/phase-02-friction"),
                  goals_path=None, specs=[], docs=[], tasks=[])
    rev = {"reconciliation": [{"item": "venv missing", "logged": "CG-1", "pr": "CG-2", "verdict": "fixed", "evidence": "fixed by setup"}],
           "summary": "went well", "personas": "designer was happy", "still_open": ["live output"],
           "next_goals": "# goals\n\n- do the next thing\n"}
    doc = render_retro_doc(phase, rev, {}, None)
    assert "# Retrospective: context-garden/phase-02-friction" in doc
    assert "went well" in doc and "designer was happy" in doc
    assert "fixed" in doc and "venv missing" in doc
    assert "- live output" in doc
    goals = render_next_goals(phase, "phase-03", rev)
    assert goals.startswith("# phase-03 goals (draft)")
    assert "do the next thing" in goals


# --------------------------------------------------------------------------- end to end
def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", "-c", "user.email=t@example.com", "-c", "user.name=t", *args],
                   cwd=cwd, check=True, capture_output=True, text=True)


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text).lstrip())
    return path


def _garden_repo(tmp_path: Path) -> str:
    repo = tmp_path / "garden-repo"
    remote = tmp_path / "garden-remote.git"
    repo.mkdir()
    _git("init", "-q", "-b", "main", cwd=repo)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    _write(repo / "garden.yaml", "name: real-garden\n")
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "init", cwd=repo)
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    _git("remote", "add", "origin", str(remote), cwd=repo)
    _git("push", "-q", "-u", "origin", "main", cwd=repo)
    return str(repo)


def _live_garden(tmp_path: Path, *, repo: str, work_dir: str) -> Path:
    root = tmp_path / "live"
    root.mkdir()
    cfg = {
        "name": "live", "max_parallel": 2, "timeout_minutes": 1,
        "review": {"enabled": False}, "github": {"draft_pr": False},
        "work_dir": work_dir,
        "harnesses": {"claude": {"bin": str(FAKE_CLAUDE), "max_turns": {"easy": 40, "medium": 5, "hard": 80}}},
        "products": {"gdn": {"repo": repo, "base_branch": "main", "id_prefix": "GD", "self": True, "github": "test/garden"}},
    }
    (root / "garden.yaml").write_text(yaml.safe_dump(cfg))
    _write(root / "principles" / "00-index.md", "# Digest\n\n- be good\n")
    _write(root / "gdn" / "product.md", "# the garden\n\nThe garden's own files.\n")
    _write(root / "gdn" / "p1" / "goals.md", "# p1\n\nClose the phase.\n")
    for tid, title in (("GD-001", "First task"), ("GD-002", "Second task")):
        _write(root / "gdn" / "p1" / "tasks" / f"{tid}.md", f"""
            ---
            id: {tid}
            title: {title}
            status: done
            depends_on: []
            priority: 1
            reading: []
            pr: https://example.com/pull/{tid[-1]}
            created: '2026-01-01T00:00:00+00:00'
            updated: '2026-01-01T00:00:00+00:00'
            ---

            ## Goal

            Do {title}.
            """)
    # a fake persona report already on disk (skip-personas reuses it)
    _write(root / "gdn" / "p1" / "docs" / "reviews" / "designer-2026-01-01.md",
           "# designer review of gdn/p1\n\n**Persona:** designer · **Score:** 7/10\n\nThe onboarding is good.\n")
    return root


def _friction_run(sched: Scheduler, task_id: str, text: str) -> None:
    run = sched.runs.new_run(task_id, "local", mode="work")
    run.result = {"pr_body": f"## What\n\nDid it.\n\n## Friction\n\n{text}\n"}
    run.status = "done"
    run.save()


def _register_prs(fake_github) -> None:
    """The two merged PRs the phase's tasks point at (so phase_prs can look them up)."""
    from garden.github import PRInfo

    for n, title in ((1, "First task"), (2, "Second task")):
        fake_github.prs[f"h{n}"] = PRInfo(number=n, url=f"https://example.com/pull/{n}",
                                          state="MERGED", title=title, body="Merged.")


def test_retro_reconciles_friction_and_opens_a_pr_to_the_garden_repo(tmp_path, fake_github, monkeypatch):
    monkeypatch.delenv("FAKE_CLAUDE_MODE", raising=False)
    repo = _garden_repo(tmp_path)
    root = _live_garden(tmp_path, repo=repo, work_dir=str(tmp_path / "work"))
    store = Store(root)
    sched = Scheduler(store, github=fake_github, log=print)
    _register_prs(fake_github)

    _friction_run(sched, "GD-001", "The worktree has no venv until setup runs.")
    _friction_run(sched, "GD-002", "The check command references $GARDEN_ROOT.")

    ph = store.phase("gdn", "p1")
    entry = sched.start_retro(ph, ["designer"], skip_personas=True)
    assert entry["stage"] == "reconciling", entry  # designer report exists, so no persona run
    rep = sched.tick()  # reap_retro -> render, commit, push, PR
    assert not rep.errors, rep.errors

    # a PR opened to the garden repo, on the retro branch, based on main
    assert fake_github.created, "no retro PR opened"
    pr = fake_github.created[-1]
    assert pr["base"] == "main"
    assert pr["head"] == "garden/retro-gdn-p1"
    assert "Retro: gdn/p1" in pr["title"]

    # the retro document was written into the worktree with a verdict per friction item
    wt = store.config.worktree_path("_retro-gdn-p1")
    retro_md = (wt / "gdn" / "p1" / "docs" / "retro.md").read_text()
    assert "# Retrospective: gdn/p1" in retro_md
    assert "GD-001" in retro_md and "GD-002" in retro_md
    # the fake reconciler cycles verdicts: first fixed, second still_true
    assert "fixed" in retro_md and "still true" in retro_md
    # the next-phase goals draft was written too
    goals = (wt / "gdn" / "p2" / "goals.md").read_text()
    assert goals.startswith("# p2 goals (draft)")
    # nothing edited the live garden's own docs
    assert not (root / "gdn" / "p1" / "docs" / "retro.md").exists()
    assert not (root / "gdn" / "p2").exists()


def test_retro_commit_failure_becomes_a_card_not_a_silent_vanish(tmp_path, fake_github, monkeypatch):
    """CG-147: at the phase-02 retro, a commit that failed inside `reap_retro` (missing git
    identity in the retro worktree) left the rendered doc staged but uncommitted, opened no
    PR, and the retro entry just disappeared from state with nothing to show for it. A failed
    commit (or push) must instead be logged and recorded as an error, so it is visible even
    when the tick ran unattended (`garden serve`), not just when someone reads a CLI's output."""
    monkeypatch.delenv("FAKE_CLAUDE_MODE", raising=False)
    # No test may depend on whatever git identity the machine running it happens to have.
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "no-such-gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(tmp_path / "no-such-gitconfig"))
    repo = _garden_repo(tmp_path)
    # _garden_repo sets a local identity on the repo so its own setup commits work; strip it so
    # a later plain `git commit` (the one reap_retro makes) has no identity to fall back on.
    subprocess.run(["git", "config", "--unset", "user.email"], cwd=repo, check=True)
    subprocess.run(["git", "config", "--unset", "user.name"], cwd=repo, check=True)
    root = _live_garden(tmp_path, repo=repo, work_dir=str(tmp_path / "work"))
    store = Store(root)
    logs: list[str] = []
    sched = Scheduler(store, github=fake_github, log=logs.append)
    _register_prs(fake_github)

    _friction_run(sched, "GD-001", "The worktree has no venv until setup runs.")
    _friction_run(sched, "GD-002", "The check command references $GARDEN_ROOT.")

    ph = store.phase("gdn", "p1")
    entry = sched.start_retro(ph, ["designer"], skip_personas=True)
    assert entry["stage"] == "reconciling", entry
    rep = sched.tick()  # reap_retro -> render, commit (fails), no push, no PR

    assert any("commit failed" in e for e in rep.errors), rep.errors
    assert any("commit failed" in m for m in logs), logs  # reaches the running log, not just the CLI's rep
    assert not fake_github.created, "no PR should open when the commit never happened"
    # the render is left on disk in the worktree for a human to look at, uncommitted
    wt = store.config.worktree_path("_retro-gdn-p1")
    assert (wt / "gdn" / "p1" / "docs" / "retro.md").exists()


def test_retro_skip_personas_refuses_to_dispatch_with_no_reports_on_disk(tmp_path, fake_github, monkeypatch):
    """CG-160: `--skip-personas` means "reuse whatever's there", not "reconcile with nothing".
    Requesting a persona with no report at all yet must not silently dispatch the
    reconciliation with an empty Persona reviews section."""
    monkeypatch.delenv("FAKE_CLAUDE_MODE", raising=False)
    repo = _garden_repo(tmp_path)
    root = _live_garden(tmp_path, repo=repo, work_dir=str(tmp_path / "work"))
    store = Store(root)
    sched = Scheduler(store, github=fake_github, log=print)
    _register_prs(fake_github)

    ph = store.phase("gdn", "p1")
    # "usability-expert" has no report under docs/reviews/ (only "designer" does)
    with pytest.raises(RuntimeError, match="usability-expert"):
        sched.start_retro(ph, ["usability-expert"], skip_personas=True)
    assert not sched._retro_list(), "no retro entry should have been recorded"
    assert not fake_github.created


def test_retro_runs_missing_personas_first_then_reconciles(tmp_path, fake_github, monkeypatch):
    monkeypatch.delenv("FAKE_CLAUDE_MODE", raising=False)
    repo = _garden_repo(tmp_path)
    root = _live_garden(tmp_path, repo=repo, work_dir=str(tmp_path / "work"))
    store = Store(root)
    sched = Scheduler(store, github=fake_github, log=print)
    _register_prs(fake_github)
    _friction_run(sched, "GD-001", "The worktree has no venv.")

    ph = store.phase("gdn", "p1")
    # usability-expert has no report on disk -> it must run before the reconciliation
    entry = sched.start_retro(ph, ["usability-expert"], skip_personas=False)
    assert entry["stage"] == "personas"
    assert entry["persona_runs"], "no persona review dispatched"
    sched.tick()  # reap the persona review (writes its report), then dispatch the reconcile run
    rep = sched.tick()  # reap the reconcile run -> PR
    assert not rep.errors, rep.errors
    assert fake_github.created, "no retro PR opened after personas ran"
    # the persona report landed under the phase's reviews (an input, like garden persona-review)
    assert list((root / "gdn" / "p1" / "docs" / "reviews").glob("usability-expert-*.md"))


def test_retro_waits_for_every_persona_report_before_reconciling(tmp_path, fake_github, monkeypatch):
    """CG-145: the phase-02 retro dispatched the reconciliation while personas were still being
    started, because completion was judged by whether a run was still active rather than by
    what had actually landed on disk. `start_retro` saves state after each persona it kicks
    off (dispatch_aux), so a concurrent `garden tick` can read the retro entry mid-loop, when
    some personas are recorded and others have not even started. Reproduce that state
    directly and check `reap_retro`/`retro_pending` judge it by the reports on disk, not by
    run activity."""
    monkeypatch.delenv("FAKE_CLAUDE_MODE", raising=False)
    from garden.personas import DEFAULT_PERSONAS

    repo = _garden_repo(tmp_path)
    root = _live_garden(tmp_path, repo=repo, work_dir=str(tmp_path / "work"))
    store = Store(root)
    sched = Scheduler(store, github=fake_github, log=print)
    _register_prs(fake_github)
    _friction_run(sched, "GD-001", "The worktree has no venv.")

    ph = store.phase("gdn", "p1")
    names = sorted(DEFAULT_PERSONAS)
    assert len(names) == 6
    # a retro entry naming all six personas, none dispatched yet (the mid-race snapshot);
    # only "designer" has a report on disk (pre-seeded by _live_garden)
    entry = {"phase": ph.key, "product": ph.product, "phase_name": ph.name, "personas": names,
             "skip_personas": False, "next_phase": "p2", "self_product": "gdn",
             "stage": "personas", "persona_runs": {}}
    sched._retro_list().append(entry)
    sched.state.save()

    assert sched.retro_pending(ph.key) == {"done": 1, "total": 6}
    rep = sched.tick()  # tick() reloads state from disk, so re-fetch the entry after each call
    assert not rep.errors, rep.errors
    entry = sched._retro_list()[0]
    assert entry["stage"] == "personas", "reconciled before every persona report existed"
    assert not fake_github.created

    reviews = ph.path / "docs" / "reviews"
    for name in names:
        if name == "designer":
            continue
        (reviews / f"{name}-2026-01-02.md").write_text(f"# {name} review of gdn/p1\n\nfine.\n")

    assert sched.retro_pending(ph.key) == {"done": 6, "total": 6}
    friction, reports, task_rows, merged = sched._retro_materials(ph, names)
    brief = reconcile_brief(store, ph, "main", friction, reports, task_rows, merged, "p2")
    assert "(none)" not in brief.split("## Persona reviews")[1].split("## ")[0]
    for name in names:
        assert name in brief

    rep = sched.tick()  # every report is in now -> the reconciliation dispatches
    assert not rep.errors, rep.errors
    entry = sched._retro_list()[0]
    assert entry["stage"] == "reconciling"
    rep = sched.tick()
    assert not rep.errors, rep.errors
    assert fake_github.created, "reconciliation never dispatched once every report landed"


def test_retro_dry_run_prints_the_plan_and_a_cost_estimate(tmp_path, fake_github, monkeypatch):
    monkeypatch.delenv("FAKE_CLAUDE_MODE", raising=False)
    repo = _garden_repo(tmp_path)
    root = _live_garden(tmp_path, repo=repo, work_dir=str(tmp_path / "work"))
    store = Store(root)
    sched = Scheduler(store, github=fake_github, log=print)
    _register_prs(fake_github)
    _friction_run(sched, "GD-001", "The worktree has no venv.")

    ph = store.phase("gdn", "p1")
    plan = sched.retro_plan(ph, ["designer", "usability-expert"], skip_personas=False)
    assert plan["self_product"] == "gdn"
    assert plan["next_phase"] == "p2"
    assert plan["friction"] == 1
    assert plan["personas_reuse"] == ["designer"]
    assert plan["personas_run"] == ["usability-expert"]
    assert plan["est_tokens"] > 0
    # no PR is opened by a dry run
    assert not fake_github.created


def test_retro_dry_run_shows_waiting_for_personas_when_a_retro_is_already_in_flight(tmp_path, fake_github, monkeypatch):
    monkeypatch.delenv("FAKE_CLAUDE_MODE", raising=False)
    repo = _garden_repo(tmp_path)
    root = _live_garden(tmp_path, repo=repo, work_dir=str(tmp_path / "work"))
    store = Store(root)
    sched = Scheduler(store, github=fake_github, log=print)
    _register_prs(fake_github)
    _friction_run(sched, "GD-001", "The worktree has no venv.")

    ph = store.phase("gdn", "p1")
    entry = {"phase": ph.key, "product": ph.product, "phase_name": ph.name,
             "personas": ["designer", "security"], "skip_personas": False,
             "next_phase": "p2", "self_product": "gdn", "stage": "personas", "persona_runs": {}}
    sched._retro_list().append(entry)
    sched.state.save()

    from garden.cli import app

    cwd = os.getcwd()
    os.chdir(root)
    try:
        r = CliRunner().invoke(app, ["retro", "gdn/p1", "--dry-run"])
    finally:
        os.chdir(cwd)
    assert r.exit_code == 0, r.output
    assert "retro: waiting for personas (1 of 2)" in r.output
