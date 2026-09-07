"""Pure retro document rendering and parsing tests.

These do not need a temporary garden, repository, or fake worker.  Keep them separate
from the retro lifecycle tests so a document change has a small feedback loop.
"""

from __future__ import annotations

from pathlib import Path

from garden.retro import (
    next_phase_name,
    parse_retro,
    reconciliation_table,
    render_next_goals,
    render_retro_doc,
)


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
    assert "| Friction item | Logged | Fixed by | Verdict | Evidence |" in table
    for word in ("still true", "fixed", "outdated", "disputed"):
        assert word in table
    assert "CG-101" in table and "CG-110" in table
    assert "setup.command runs uv sync" in table
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
