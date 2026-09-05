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
    features_section,
    next_phase_name,
    numbers_section,
    parse_retro,
    reconcile_brief,
    reconciliation_table,
    render_next_goals,
    render_retro_doc,
    resolve_features,
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


def test_numbers_section_reports_the_operators_share_of_total_spend():
    text = numbers_section(80.0, 20.0)
    assert "$80.00" in text and "$20.00" in text and "$100.00" in text
    assert "20%" in text


def test_numbers_section_handles_zero_total():
    text = numbers_section(0.0, 0.0)
    assert "$0.00" in text
    assert "%" not in text  # no share line when there is nothing to divide


def test_render_retro_doc_includes_numbers_when_given():
    from garden.model import Phase

    phase = Phase(product="context-garden", name="phase-04", path=Path("/x/context-garden/phase-04"),
                  goals_path=None, specs=[], docs=[], tasks=[])
    rev = {"reconciliation": [], "summary": "", "personas": "", "still_open": [], "next_goals": ""}
    doc = render_retro_doc(phase, rev, {}, None, numbers=numbers_section(80.0, 20.0))
    assert "## Numbers" in doc
    assert "$80.00" in doc and "20%" in doc
    # omitted entirely when there is nothing to report
    assert "## Numbers" not in render_retro_doc(phase, rev, {}, None)


def test_resolve_features_flags_a_title_match_and_an_explicit_duplicate():
    rev = {"features": [
        {"title": "New thing", "body": "b", "difficulty": "medium", "priority": 2, "rationale": "r"},
        {"title": "First task", "body": "b2", "difficulty": "easy", "priority": 4, "rationale": "already tracked"},
        {"title": "Another new thing", "duplicate_of": "CG-9", "body": "b3", "difficulty": "hard", "priority": 1, "rationale": "r3"},
        {"title": "  "},  # blank titles are dropped
        "not a dict",
    ]}
    resolved = resolve_features(rev, {"first task": "GD-001"})
    assert len(resolved) == 3
    assert resolved[0]["skip"] is False and resolved[0]["reason"] == ""
    assert resolved[1]["skip"] is True and "GD-001" in resolved[1]["reason"]
    assert resolved[2]["skip"] is True and "CG-9" in resolved[2]["reason"]


def test_resolve_features_empty():
    assert resolve_features({}, {}) == []
    assert resolve_features({"features": []}, {}) == []


def test_features_section_renders_rank_ids_and_skips():
    filed = [
        {"title": "New thing", "task_id": "GD-003", "status": "draft", "difficulty": "medium",
         "rationale": "r", "body": "b"},
        {"title": "First task", "task_id": "", "reason": "same title as GD-001"},
    ]
    section = features_section(filed)
    assert section.startswith("1. **New thing** — GD-003 [draft]")
    assert "size: medium" in section and "why now: r" in section
    assert "2. **First task** — _skipped: same title as GD-001_" in section
    assert features_section([]) == "_No features proposed for the next phase._"


def test_reconcile_brief_includes_reported_and_comment_friction():
    """CG-150: the reconciliation used to see only PR-body friction; friction already logged
    under friction.md's '## Reported' section, and friction still sitting in a marked but
    unreconciled PR comment, must reach the model too."""
    import types

    from garden.model import Phase

    phase = Phase(product="p", name="ph1", path=Path("/x/p/ph1"), goals_path=None, specs=[], docs=[], tasks=[])
    store = types.SimpleNamespace(
        root=Path("/x"),
        config=types.SimpleNamespace(get=lambda key: None),
        rel=lambda path: str(path),
    )

    class _T:
        def __init__(self, id, pr=""):
            self.id, self.pr = id, pr

    reported = "## Reported\n\n### 2026-01-01 · cli\n\nThe onboarding doc is stale."
    comment_friction = [(_T("CG-1", "https://example.com/pull/1"), ["Spec had no schema link."])]
    brief = reconcile_brief(store, phase, "main", [], reported, comment_friction, {}, [], [], "ph2")
    assert "## Reported friction" in brief
    assert "The onboarding doc is stale." in brief
    assert "## Friction reported in PR comments" in brief
    assert "Spec had no schema link." in brief
    assert "CG-1" in brief and "https://example.com/pull/1" in brief


def test_reconcile_brief_marks_absent_reported_and_comment_friction():
    import types

    from garden.model import Phase

    phase = Phase(product="p", name="ph1", path=Path("/x/p/ph1"), goals_path=None, specs=[], docs=[], tasks=[])
    store = types.SimpleNamespace(
        root=Path("/x"),
        config=types.SimpleNamespace(get=lambda key: None),
        rel=lambda path: str(path),
    )
    brief = reconcile_brief(store, phase, "main", [], "", [], {}, [], [], "ph2")
    assert "## Reported friction (friction.md '## Reported' log)\n\n(none)" in brief
    assert "## Friction reported in PR comments\n\n(none)" in brief


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
        "worker_env": {"pass": ["FAKE_CLAUDE_*"]},
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


def test_retro_document_reports_operator_spend_and_its_share(tmp_path, fake_github, monkeypatch):
    """CG-223: docs/operator-spend.jsonl in the live garden feeds the retro's own '## Numbers'
    section, so the operator's spend and its share of the phase's total are quoted, not
    guessed at — no run_finished events exist for this phase's tasks here, so the whole
    total is the operator's."""
    import json

    monkeypatch.delenv("FAKE_CLAUDE_MODE", raising=False)
    repo = _garden_repo(tmp_path)
    root = _live_garden(tmp_path, repo=repo, work_dir=str(tmp_path / "work"))
    (root / "docs").mkdir(parents=True, exist_ok=True)
    with (root / "docs" / "operator-spend.jsonl").open("w") as f:
        f.write(json.dumps({"at": "2026-01-01T00:00:00+00:00", "session": "sess-a", "list_price_usd": 3.5}) + "\n")
    store = Store(root)
    sched = Scheduler(store, github=fake_github, log=print)
    _register_prs(fake_github)

    _friction_run(sched, "GD-001", "The worktree has no venv until setup runs.")
    _friction_run(sched, "GD-002", "The check command references $GARDEN_ROOT.")

    ph = store.phase("gdn", "p1")
    sched.start_retro(ph, ["designer"], skip_personas=True)
    rep = sched.tick()
    assert not rep.errors, rep.errors

    wt = store.config.worktree_path("_retro-gdn-p1")
    retro_md = (wt / "gdn" / "p1" / "docs" / "retro.md").read_text()
    assert "## Numbers" in retro_md
    assert "$3.50" in retro_md
    assert "100%" in retro_md  # no worker run_finished events recorded here, so it's all operator


def test_retro_files_features_in_the_next_phase_and_skips_a_duplicate(tmp_path, fake_github, monkeypatch):
    """CG-181: the fake harness proposes two new features and one that repeats the phase's
    first task by title (see tests/fake_claude.py:retro); the reap must file the two new ones
    as draft tasks in the next phase, with provenance back to this retro, and skip the
    duplicate with a log line rather than filing it again."""
    monkeypatch.delenv("FAKE_CLAUDE_MODE", raising=False)
    repo = _garden_repo(tmp_path)
    root = _live_garden(tmp_path, repo=repo, work_dir=str(tmp_path / "work"))
    store = Store(root)
    logs: list[str] = []
    sched = Scheduler(store, github=fake_github, log=logs.append)
    _register_prs(fake_github)

    ph = store.phase("gdn", "p1")
    sched.start_retro(ph, ["designer"], skip_personas=True)
    rep = sched.tick()
    assert not rep.errors, rep.errors
    assert fake_github.created

    wt = store.config.worktree_path("_retro-gdn-p1")
    tasks_dir = wt / "gdn" / "p2" / "tasks"
    filed = {t.stem: t for t in tasks_dir.glob("*.md")}
    assert len(filed) == 2, filed  # two new features filed, the duplicate skipped

    from garden.model import Task as TaskModel

    parsed = [TaskModel.parse(p, p.read_text(), product="gdn", phase="p2") for p in filed.values()]
    ids = sorted(t.id for t in parsed)
    assert ids == ["GD-003", "GD-004"]
    for t in parsed:
        assert t.status.value == "draft"
        assert t.discovered_from == "retro:gdn/p1"
        assert "retro" in t.body.lower()
    titles = {t.title for t in parsed}
    assert titles == {"Add a task-creation form to the web UI", "One vocabulary across CLI, web and TUI"}
    assert not any(t.title == "First task" for t in parsed)

    # the skip reached the running log, and the retro doc renders both the filed ids and the skip
    assert any("First task" in m and "skipped" in m for m in logs)
    retro_md = (wt / "gdn" / "p1" / "docs" / "retro.md").read_text()
    assert "## Features for the next phase" in retro_md
    assert "GD-003" in retro_md and "GD-004" in retro_md
    assert "_skipped:" in retro_md and "First task" in retro_md
    goals = (wt / "gdn" / "p2" / "goals.md").read_text()
    assert "GD-003" in goals and "GD-004" in goals

    pr = fake_github.created[-1]
    assert "2 feature(s) filed" in pr["body"] and "1 duplicate(s) skipped" in pr["body"]


def test_retro_files_persona_findings_merged_across_personas_by_title(tmp_path, fake_github, monkeypatch):
    """CG-187: every persona finding becomes a draft, not only the high ones, priority from
    severity, and findings that say the same thing across personas (the fake harness returns
    the same three findings for every persona) collapse into one task naming every persona
    that raised it."""
    monkeypatch.delenv("FAKE_CLAUDE_MODE", raising=False)
    monkeypatch.setenv("FAKE_CLAUDE_PERSONA_FINDINGS", "all")
    repo = _garden_repo(tmp_path)
    root = _live_garden(tmp_path, repo=repo, work_dir=str(tmp_path / "work"))
    store = Store(root)
    sched = Scheduler(store, github=fake_github, log=print)
    _register_prs(fake_github)

    ph = store.phase("gdn", "p1")
    # neither persona has a report pre-seeded (only "designer" does), so both actually run
    entry = sched.start_retro(ph, ["security", "usability-expert"], skip_personas=False)
    assert entry["stage"] == "personas"
    sched.tick()  # reap both persona runs -> dispatch the reconcile run
    rep = sched.tick()  # reap the reconcile run -> PR
    assert not rep.errors, rep.errors
    assert fake_github.created

    wt = store.config.worktree_path("_retro-gdn-p1")
    tasks_dir = wt / "gdn" / "p2" / "tasks"
    from garden.model import Task as TaskModel

    parsed = [TaskModel.parse(p, p.read_text(), product="gdn", phase="p2") for p in tasks_dir.glob("*.md")]
    finding_tasks = [t for t in parsed if t.discovered_from.startswith("persona:")]
    # three merged findings (one per severity) -- each raised by both personas, not six
    assert len(finding_tasks) == 3
    by_priority = {t.priority: t for t in finding_tasks}
    assert set(by_priority) == {1, 2, 3}
    assert by_priority[1].title == "Secrets can leak into run logs"
    assert by_priority[2].title == "garden.yaml needs a restart to take effect"
    assert by_priority[3].title == "The Inbox button label is inconsistent"
    for t in finding_tasks:
        assert t.status.value == "draft"
        assert "security" in t.body and "usability-expert" in t.body

    retro_md = (wt / "gdn" / "p1" / "docs" / "retro.md").read_text()
    assert "## Findings from persona reviews" in retro_md
    assert "### High" in retro_md and "### Medium" in retro_md and "### Low" in retro_md
    assert by_priority[1].id in retro_md and by_priority[3].id in retro_md

    pr = fake_github.created[-1]
    assert "3 persona finding(s) filed" in pr["body"]


def test_retro_lifts_a_personas_structured_features_into_the_features_list(tmp_path, fake_github, monkeypatch):
    """CG-188: a persona that declares a `features` section (the product-manager) returns
    structured features; the retro lifts them into its own features list and files each as a
    draft task in the next phase, naming the persona as the source in the task body."""
    monkeypatch.delenv("FAKE_CLAUDE_MODE", raising=False)
    repo = _garden_repo(tmp_path)
    root = _live_garden(tmp_path, repo=repo, work_dir=str(tmp_path / "work"))
    store = Store(root)
    sched = Scheduler(store, github=fake_github, log=print)
    _register_prs(fake_github)

    ph = store.phase("gdn", "p1")
    entry = sched.start_retro(ph, ["product-manager"], skip_personas=False)
    assert entry["stage"] == "personas"
    sched.tick()  # run the product-manager persona, then dispatch the reconcile run
    rep = sched.tick()  # reap the reconcile run -> PR
    assert not rep.errors, rep.errors
    assert fake_github.created

    from garden.model import Task as TaskModel

    wt = store.config.worktree_path("_retro-gdn-p1")
    tasks_dir = wt / "gdn" / "p2" / "tasks"
    parsed = [TaskModel.parse(p, p.read_text(), product="gdn", phase="p2") for p in tasks_dir.glob("*.md")]
    titles = {t.title for t in parsed}
    assert "A form to file a task from the web" in titles
    pm_task = next(t for t in parsed if t.title == "A form to file a task from the web")
    assert "product-manager persona" in pm_task.body
    assert pm_task.discovered_from == "retro:gdn/p1"

    retro_md = (wt / "gdn" / "p1" / "docs" / "retro.md").read_text()
    assert "A form to file a task from the web" in retro_md


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
    assert len(names) == 7
    # a retro entry naming all six personas, none dispatched yet (the mid-race snapshot);
    # only "designer" has a report on disk (pre-seeded by _live_garden)
    entry = {"phase": ph.key, "product": ph.product, "phase_name": ph.name, "personas": names,
             "skip_personas": False, "next_phase": "p2", "self_product": "gdn",
             "stage": "personas", "persona_runs": {}}
    sched._retro_list().append(entry)
    sched.state.save()

    assert sched.retro_pending(ph.key) == {"done": 1, "total": 7}
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

    assert sched.retro_pending(ph.key) == {"done": 7, "total": 7}
    friction, reported, comment_friction, reports, task_rows, merged = sched._retro_materials(ph, names)
    brief = reconcile_brief(store, ph, "main", friction, reported, comment_friction, reports, task_rows, merged, "p2")
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


def test_retro_materials_reads_reported_section_and_pr_comment_friction(tmp_path, fake_github, monkeypatch):
    """CG-150: the reconciliation only ever harvested PR-body friction; friction already
    logged under friction.md's '## Reported' section, and friction still sitting in a
    marked PR comment, must reach it too."""
    monkeypatch.delenv("FAKE_CLAUDE_MODE", raising=False)
    from garden.friction import friction_comment

    repo = _garden_repo(tmp_path)
    root = _live_garden(tmp_path, repo=repo, work_dir=str(tmp_path / "work"))
    store = Store(root)
    sched = Scheduler(store, github=fake_github, log=print)
    _register_prs(fake_github)

    ph = store.phase("gdn", "p1")
    doc = ph.path / "docs" / "friction.md"
    doc.parent.mkdir(parents=True, exist_ok=True)
    doc.write_text("# Friction\n\n_No friction reported yet._\n\n"
                    "## Reported\n\n### 2026-01-01 · cli\n\nThe changelog was stale.\n")
    fake_github.comments.append(friction_comment(["The rebase docs never mention conflicts."]))

    friction, reported, comment_friction, reports, task_rows, merged = sched._retro_materials(ph, ["designer"])
    assert "The changelog was stale." in reported
    items = [i for _, its in comment_friction for i in its]
    assert "The rebase docs never mention conflicts." in items
    # nothing was written back to the live friction.md; reading is read-only
    assert doc.read_text().count("## Reported") == 1


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
    # CG-207: the plan names the tier and model it will run on, defaulting to hard
    assert plan["difficulty"] == "hard" and plan["model"] == "opus"
    # no PR is opened by a dry run
    assert not fake_github.created


def test_retro_reconciliation_uses_retro_difficulty_not_review_difficulty(tmp_path, fake_github, monkeypatch):
    """CG-207: review.difficulty is for PR reviews only; the reconciliation run resolves its
    model from retro.difficulty, which defaults to hard, so nobody edits garden.yaml before a
    retro."""
    monkeypatch.delenv("FAKE_CLAUDE_MODE", raising=False)
    repo = _garden_repo(tmp_path)
    root = _live_garden(tmp_path, repo=repo, work_dir=str(tmp_path / "work"))
    cfg = yaml.safe_load((root / "garden.yaml").read_text())
    cfg["review"] = {"difficulty": "easy"}
    (root / "garden.yaml").write_text(yaml.safe_dump(cfg))
    store = Store(root)
    sched = Scheduler(store, github=fake_github, log=print)
    _register_prs(fake_github)
    _friction_run(sched, "GD-001", "The worktree has no venv until setup runs.")

    ph = store.phase("gdn", "p1")
    plan = sched.retro_plan(ph, ["designer"], skip_personas=True)
    assert plan["difficulty"] == "hard" and plan["model"] == "opus"
    entry = sched.start_retro(ph, ["designer"], skip_personas=True)
    assert entry["stage"] == "reconciling"
    run = sched.runs.latest(entry["recon_task"])
    assert run.difficulty == "hard" and run.model == "opus"


def test_retro_reconciliation_uses_retro_model_when_set(tmp_path, fake_github, monkeypatch):
    """CG-235: retro.model names the judge outright, ahead of retro.difficulty's tier map, so a
    garden pricing hard work cheaply can still hand the retro to its best model."""
    monkeypatch.delenv("FAKE_CLAUDE_MODE", raising=False)
    repo = _garden_repo(tmp_path)
    root = _live_garden(tmp_path, repo=repo, work_dir=str(tmp_path / "work"))
    cfg = yaml.safe_load((root / "garden.yaml").read_text())
    cfg["harnesses"]["claude"]["models"] = {"easy": "haiku", "medium": "sonnet", "hard": "opus"}
    cfg["retro"] = {"model": "fable"}
    (root / "garden.yaml").write_text(yaml.safe_dump(cfg))
    store = Store(root)
    sched = Scheduler(store, github=fake_github, log=print)
    _register_prs(fake_github)
    _friction_run(sched, "GD-001", "The worktree has no venv until setup runs.")

    ph = store.phase("gdn", "p1")
    plan = sched.retro_plan(ph, ["designer"], skip_personas=True)
    assert plan["difficulty"] == "hard" and plan["model"] == "fable"
    entry = sched.start_retro(ph, ["designer"], skip_personas=True)
    run = sched.runs.latest(entry["recon_task"])
    assert run.difficulty == "hard" and run.model == "fable"


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


def _retro_entry(sched: Scheduler, phase_key: str) -> dict:
    """tick() replaces `sched.state` with a fresh read from disk, so a dict handed back by
    `start_retro` before a tick is a stale copy afterwards; re-fetch it from the live list."""
    return next(e for e in sched._retro_list() if e["phase"] == phase_key)


# ---- quota/pause handling in the retro flow (CG-227) --------------------------------------
# The retro's own reconcile dispatch (`_dispatch_retro_run`, used by `_dispatch_reconcile`) sat
# outside the paused-harness gate every other aux dispatch (review, persona, compare) already
# has (CG-212): a paused harness could still be sent a fresh reconcile run, and a quota
# env_error mid-reconcile was read as a retro with no verdict and the whole entry dropped.

def test_reconcile_dispatch_refuses_a_paused_harness(tmp_path, fake_github, monkeypatch):
    """A direct call that would dispatch the reconcile run (here, `skip_personas=True` with the
    designer report already on disk, so `start_retro` goes straight to `_dispatch_reconcile`)
    gets the same refusal a fresh work/review/persona dispatch would, instead of starting a run
    that can only hit the same account limit again."""
    monkeypatch.delenv("FAKE_CLAUDE_MODE", raising=False)
    repo = _garden_repo(tmp_path)
    root = _live_garden(tmp_path, repo=repo, work_dir=str(tmp_path / "work"))
    store = Store(root)
    sched = Scheduler(store, github=fake_github, log=print)
    _register_prs(fake_github)
    _friction_run(sched, "GD-001", "The worktree has no venv until setup runs.")

    sched.pause_harness("claude", "quota limit hit on claude")
    ph = store.phase("gdn", "p1")
    with pytest.raises(RuntimeError, match="paused"):
        sched.start_retro(ph, ["designer"], skip_personas=True)
    assert not fake_github.created


def test_reap_retro_defers_the_reconcile_while_the_harness_is_paused_then_dispatches_on_resume(
        tmp_path, fake_github, monkeypatch):
    """Every persona report is in, but the harness the reconcile would use is paused: reap_retro
    must wait for it to resume rather than raising into a caught-and-dropped retro entry."""
    monkeypatch.delenv("FAKE_CLAUDE_MODE", raising=False)
    repo = _garden_repo(tmp_path)
    root = _live_garden(tmp_path, repo=repo, work_dir=str(tmp_path / "work"))
    store = Store(root)
    sched = Scheduler(store, github=fake_github, log=print)
    _register_prs(fake_github)
    _friction_run(sched, "GD-001", "The worktree has no venv until setup runs.")

    ph = store.phase("gdn", "p1")
    # "security" has no report on disk (only "designer" does), so it actually dispatches.
    entry = sched.start_retro(ph, ["security"], skip_personas=False)
    assert entry["stage"] == "personas"

    sched.pause_harness("claude", "quota limit hit on claude")
    rep = sched.tick()  # reap_aux collects the (already-dispatched) persona run; reap_retro
    # finds every report present but defers the reconcile instead of dispatching into a paused harness
    entry = _retro_entry(sched, ph.key)
    assert entry["stage"] == "personas"
    assert entry.get("reconcile_paused") is True
    assert any("reconcile deferred" in t for t in rep.transitions)
    assert not fake_github.created

    sched.resume_harness("claude", by="test")
    rep2 = sched.tick()  # reconcile dispatches now that the harness is up
    entry = _retro_entry(sched, ph.key)
    assert entry["stage"] == "reconciling"
    assert not entry.get("reconcile_paused")
    assert "reconcile deferred" not in " ".join(rep2.transitions)

    rep3 = sched.tick()  # reap the reconcile run -> PR
    assert not rep3.errors, rep3.errors
    assert fake_github.created


def test_reap_retro_reconcile_env_error_pauses_the_harness_instead_of_failing_the_retro(
        tmp_path, fake_github, monkeypatch):
    """A quota/spend-limit hit mid-reconcile is the harness's own account trouble, not a failed
    retro: the harness pauses, the entry goes back to wait for it to resume, and the retro is
    retried (not dropped, not logged as "no verdict") once the harness recovers."""
    monkeypatch.delenv("FAKE_CLAUDE_MODE", raising=False)
    repo = _garden_repo(tmp_path)
    root = _live_garden(tmp_path, repo=repo, work_dir=str(tmp_path / "work"))
    store = Store(root)
    sched = Scheduler(store, github=fake_github, log=print)
    _register_prs(fake_github)
    _friction_run(sched, "GD-001", "The worktree has no venv until setup runs.")

    ph = store.phase("gdn", "p1")
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "quota")
    entry = sched.start_retro(ph, ["designer"], skip_personas=True)  # dispatches the reconcile run
    assert entry["stage"] == "reconciling"
    rep = sched.tick()  # reap_retro collects the quota output

    assert sched.is_harness_paused("claude")
    entry = _retro_entry(sched, ph.key)
    assert entry["stage"] == "personas"
    assert entry.get("reconcile_paused") is True
    assert any("reconcile paused (env_error)" in t for t in rep.transitions)
    assert not any("no verdict" in e for e in rep.errors), rep.errors
    assert not fake_github.created

    monkeypatch.setenv("FAKE_CLAUDE_MODE", "nocommit")  # protects the probe (its cwd isn't a
    # git repo); the reconcile is picked out by its own `GARDEN_RETRO:` brief marker regardless
    sched.cfg.data["harness_pause"] = {"probe_minutes": 0}
    sched.tick()  # probe resumes claude; too late for this tick's reap_retro pass
    assert not sched.is_harness_paused("claude")

    sched.tick()  # reap_retro redispatches the reconcile now that claude is up
    entry = _retro_entry(sched, ph.key)
    assert entry["stage"] == "reconciling"
    rep4 = sched.tick()  # reap the reconcile run -> PR
    assert not rep4.errors, rep4.errors
    assert fake_github.created
