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
