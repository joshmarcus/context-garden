from garden.brief import RESULT_MARKER, build_brief, estimate_brief_tokens, parse_result
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
