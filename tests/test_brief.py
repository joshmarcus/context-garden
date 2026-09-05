from garden.brief import (
    RESULT_MARKER,
    build_brief,
    estimate_brief_tokens,
    parse_result,
    phase_fixed_tokens,
)
from garden.store import Store


def test_brief_sections(garden):
    store = Store(garden)
    b = build_brief(store, store.task("DM-001"), branch="garden/x", base="main")
    assert "# Task DM-001" in b.text
    assert "## Principles (digest)" in b.text and "be good" in b.text
    assert "## Product: demo" in b.text and "## Phase goals: p1" in b.text
    assert "### demo/p1/specs/spec.md" in b.text and "Details." in b.text
    assert "garden/x" in b.text and RESULT_MARKER in b.text
    assert set(b.sections) >= {"head", "rules", "principles", "product", "goals", "task", "reading"}
    assert b.tokens > 100


def test_brief_cost_breakdown(garden):
    store = Store(garden)
    b = build_brief(store, store.task("DM-001"), branch="garden/x", base="main")
    assert b.fixed_tokens > 0
    assert b.reading_tokens >= 0
    assert b.fixed_tokens + b.reading_tokens <= b.tokens + 10  # small tolerance for rounding


def test_estimate_brief_tokens(garden):
    store = Store(garden)
    fixed, reading = estimate_brief_tokens(store, store.task("DM-001"))
    assert fixed > 0
    assert reading >= 0
    assert fixed + reading > 0


def test_phase_fixed_tokens_measured_once_per_phase(garden):
    store = Store(garden)
    ph = store.phase("demo", "p1")
    fixed = phase_fixed_tokens(store, ph.tasks)
    # It is the fixed part of a full brief for a task in the phase, not the reading list.
    b = build_brief(store, ph.tasks[0], include_rules=True)
    assert fixed == b.fixed_tokens > 0
    assert fixed < b.tokens  # the reading list is not counted in the fixed cost
    assert phase_fixed_tokens(store, []) == 0


def test_brief_oversized_reading_is_referenced(garden):
    store = Store(garden)
    big = garden / "demo" / "p1" / "specs" / "big.md"
    big.write_text("x" * 30000)
    t = store.task("DM-001")
    t.reading.append("demo/p1/specs/big.md")
    t.reading.append("demo/p1/specs/nope.md")
    b = build_brief(store, t)
    assert "demo/p1/specs/big.md" in b.referenced
    assert "Reading list (read these)" in b.text
    assert b.missing == ["demo/p1/specs/nope.md"]


def test_brief_directory_reading(garden):
    store = Store(garden)
    t = store.task("DM-002")
    t.reading = ["demo/p1/specs"]
    b = build_brief(store, t)
    assert "demo/p1/specs/spec.md" in b.inlined


def test_parse_result():
    assert parse_result('blah\nGARDEN_RESULT: {"status": "done", "summary": "s"}\n') == {"status": "done", "summary": "s"}
    assert parse_result("GARDEN_RESULT: ```{\"status\": \"done\"}```")["status"] == "done"
    assert parse_result("nothing") == {}


def test_brief_states_the_pr_body_contract(garden):
    store = Store(garden)
    b = build_brief(store, store.task("DM-001"), branch="garden/x", base="main")
    # the permanent-description contract and where friction/process narration go
    assert "permanent description" in b.text
    assert "as if the change were right the first time" in b.text
    assert "omit `pr_body` unless the description itself must change" in b.text
    assert '"friction"' in b.text and "`friction` items" in b.text
    # the JSON schema line advertises the friction field
    assert '"friction": ["<short friction item>"]' in b.text


def test_brief_pre_pr_revision(garden):
    store = Store(garden)
    task = store.task("DM-001")
    task.pr = None  # no PR yet
    b = build_brief(store, task, branch="garden/x", base="main", review_feedback="ruff failed")
    assert "## Revision round" in b.text
    assert "pre-PR check failed" in b.text
    assert "This branch already has an open pull request" not in b.text
    assert "Review feedback to address" in b.text


def test_brief_pr_revision(garden):
    store = Store(garden)
    task = store.task("DM-001")
    task.pr = "https://github.com/example/repo/pull/123"
    b = build_brief(store, task, branch="garden/x", base="main", review_feedback="update docstring")
    assert "## Revision round" in b.text
    assert "This branch already has an open pull request" in b.text
    assert "https://github.com/example/repo/pull/123" in b.text


def test_brief_commits_ahead(garden):
    store = Store(garden)
    commits = [
        "abc1234 First commit",
        "def5678 Second commit",
        "ghi9012 Third commit",
    ]
    b = build_brief(store, store.task("DM-001"), branch="garden/x", base="main", commits_ahead=commits)
    assert "## Already on this branch" in b.text
    assert "First commit" in b.text
    assert "Second commit" in b.text
    assert "Third commit" in b.text
    assert "- abc1234 First commit" in b.text


def test_reading_list_resolves_against_the_product_checkout(garden):
    from pathlib import Path

    from garden.brief import build_brief, product_dirs, resolve_reading
    from garden.store import Store

    store = Store(garden)
    task = next(iter(store.tasks().values()))
    repo = store.config.product_repo(task.product)
    assert isinstance(repo, Path) and repo.resolve() != garden.resolve()
    (repo / "src").mkdir(exist_ok=True)
    (repo / "src" / "thing.py").write_text("VALUE = 1\n")
    task.reading = ["src/thing.py", "src/nowhere.py"]
    assert repo in product_dirs(store, task)
    assert resolve_reading(store, task, "src/thing.py")[1] == repo
    b = build_brief(store, task)
    assert "### src/thing.py" in b.text and "VALUE = 1" in b.text
    assert "src/nowhere.py" in b.missing
    assert "- `src/nowhere.py` (not found when the brief was built)" in b.text
    assert str(garden) not in b.text


def test_brief_never_names_the_garden_root(garden):
    from garden.brief import build_brief
    from garden.store import Store

    store = Store(garden)
    task = next(iter(store.tasks().values()))
    big = garden / "big.md"
    big.write_text("x" * 30000)
    task.reading = [str(big.relative_to(garden))]
    text = build_brief(store, task).text
    assert str(garden) not in text
    assert "context garden root" not in text
    assert "relative to your current directory" in text
    assert "Work only in the directory you were started in" in text


def test_brief_includes_max_turns_cap(garden):
    store = Store(garden)
    t = store.task("DM-001")
    t.difficulty = "easy"
    b = build_brief(store, t, branch="garden/x", base="main")
    assert "You have **" in b.text and "turns**" in b.text
    assert "40 turns" in b.text  # easy tier in test config is 40

    t.difficulty = "hard"
    b = build_brief(store, t, branch="garden/x", base="main")
    assert "80 turns" in b.text  # hard tier in test config is 80


def test_brief_omits_max_turns_when_unset(garden):
    import yaml
    cfg = yaml.safe_load((garden / "garden.yaml").read_text())
    cfg["harnesses"]["claude"].pop("max_turns")
    (garden / "garden.yaml").write_text(yaml.safe_dump(cfg))
    store = Store(garden)
    b = build_brief(store, store.task("DM-001"), branch="garden/x", base="main")
    assert "You have **" not in b.text


# ---- brief_gaps: placeholder criteria and unresolved reading paths (CG-193) ----


def _task_with(store, body="", reading=None):
    """A draft task in demo/p1 with the given body and reading list, for brief_gaps."""
    t = store.create_task("demo", "p1", "Gap probe", body, reading=reading or [], status="draft")
    return t


def test_brief_gaps_flags_missing_criteria_section(garden):
    from garden.brief import brief_gaps

    store = Store(garden)
    t = _task_with(store, "## Goal\n\nDo a thing.\n")
    gaps = brief_gaps(store, t)
    assert any("Acceptance criteria" in g for g in gaps)


def test_brief_gaps_flags_placeholder_criteria(garden):
    from garden.brief import brief_gaps

    store = Store(garden)
    for placeholder in ("- [ ] ...", "- [ ] to be written at planning", "- [ ] TBD", "- [ ] TODO"):
        t = _task_with(store, f"## Goal\n\nX\n\n## Acceptance criteria\n\n{placeholder}\n")
        assert any("placeholder" in g for g in brief_gaps(store, t)), placeholder


def test_brief_gaps_flags_non_checkbox_criteria(garden):
    """A plain bullet (no `- [ ] ` checkbox) is invisible to criteria.parse_criteria — the same
    parser review, reap and the task page use — so approve must not treat it as real, either."""
    from garden.brief import brief_gaps

    store = Store(garden)
    body = "## Goal\n\nX\n\n## Acceptance criteria\n\n- The parser rejects an empty file.\n"
    t = _task_with(store, body)
    gaps = brief_gaps(store, t)
    assert any("Acceptance criteria" in g for g in gaps)


def test_brief_gaps_accepts_real_criteria(garden):
    from garden.brief import brief_gaps

    store = Store(garden)
    t = _task_with(store, "## Goal\n\nX\n\n## Acceptance criteria\n\n- [ ] The parser rejects an empty file.\n")
    assert brief_gaps(store, t) == []


def test_brief_gaps_flags_unresolved_reading_path(garden):
    from garden.brief import brief_gaps

    store = Store(garden)
    body = "## Goal\n\nX\n\n## Acceptance criteria\n\n- [ ] It works and is tested.\n"
    t = _task_with(store, body, reading=["demo/p1/specs/spec.md", "demo/p1/specs/missing.md"])
    gaps = brief_gaps(store, t)
    assert any("missing.md" in g for g in gaps)
    assert not any("spec.md`" in g for g in gaps)  # the resolvable one is not flagged


# ---- resolve_reading refuses absolute and parent-escaping paths (CG-239) ----


def test_resolve_reading_refuses_an_absolute_path(garden):
    from garden.brief import resolve_reading

    store = Store(garden)
    task = store.task("DM-001")
    p, base = resolve_reading(store, task, "/etc/passwd")
    assert p is None and base is None


def test_resolve_reading_refuses_a_parent_escape(garden):
    from garden.brief import resolve_reading

    store = Store(garden)
    task = store.task("DM-001")
    (garden.parent / "outside.md").write_text("secret\n")
    p, base = resolve_reading(store, task, "../outside.md")
    assert p is None and base is None


def test_brief_gaps_flags_an_absolute_reading_entry(garden):
    from garden.brief import brief_gaps

    store = Store(garden)
    body = "## Goal\n\nX\n\n## Acceptance criteria\n\n- [ ] It works and is tested.\n"
    t = _task_with(store, body, reading=["/etc/passwd"])
    gaps = brief_gaps(store, t)
    assert any("/etc/passwd" in g for g in gaps)


def test_brief_gaps_flags_a_parent_escaping_reading_entry(garden):
    from garden.brief import brief_gaps

    store = Store(garden)
    body = "## Goal\n\nX\n\n## Acceptance criteria\n\n- [ ] It works and is tested.\n"
    t = _task_with(store, body, reading=["../outside.md"])
    gaps = brief_gaps(store, t)
    assert any("../outside.md" in g for g in gaps)
