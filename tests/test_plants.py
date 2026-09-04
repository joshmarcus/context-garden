"""The botanical system: plants per phase, growth-stage glyphs, goals frontmatter."""

from garden.model import goals_text
from garden.plants import (
    DEFS,
    DRAWN_PLANTS,
    PLANTS,
    STAGE,
    assign_plant,
    plant_svg,
    positional_plant,
    roman,
    stage_svg,
    stage_word,
)
from garden.store import Store


def test_roman_and_assignment():
    assert [roman(n) for n in (1, 2, 4, 9, 12)] == ["I", "II", "IV", "IX", "XII"]
    assert assign_plant([]) == "pea" and assign_plant(["pea"]) == "bramble" and assign_plant([p["key"] for p in PLANTS]) == "pea"
    assert len({p["key"] for p in PLANTS}) == len(PLANTS)
    # a phase without a plant of its own keeps its positional plant whatever the others pin
    assert positional_plant(1, ["poppy"]) == "bramble"
    assert positional_plant(1, ["bramble"]) == "foxglove"  # pinned elsewhere: the next free one
    assert positional_plant(len(PLANTS), [p["key"] for p in PLANTS]) == "pea"  # wraps


def test_drawings_cover_every_status_and_plant():
    from garden.model import Status

    for st in Status:
        assert st.value in STAGE, st
        assert f'id="{STAGE[st.value]}"' in DEFS
        assert f'href="#{STAGE[st.value]}"' in stage_svg(st.value)
        assert stage_word(st.value)
    for p in PLANTS:
        # the original five have a bespoke drawing; the rest fall back to the generic sprig
        sym = p["key"] if p["key"] in DRAWN_PLANTS else "sprig"
        assert f'id="{sym}"' in DEFS and f'href="#{sym}"' in plant_svg(p["key"], 40, 60)
    assert 'href="#pea"' in plant_svg("nope", 10, 10)  # unknown plant falls back


def test_phases_get_plants_by_position_or_frontmatter(garden):
    store = Store(garden)
    ph = store.phase("demo", "p1")
    assert ph.plant == "pea" and ph.plate == "I" and ph.latin == "Pisum sativum"
    (garden / "demo" / "p2").mkdir()
    (garden / "demo" / "p2" / "goals.md").write_text("---\nplant: poppy\nplate: VII\n---\n# p2\n\nGoals.\n")
    (garden / "demo" / "p2" / "tasks").mkdir()
    store.invalidate()
    p2 = store.phase("demo", "p2")
    assert p2.plant == "poppy" and p2.plate == "VII" and p2.common == "corn poppy"
    # pinning p2 does not move p1, and a later phase without frontmatter stays positional
    assert store.phase("demo", "p1").plant == "pea"
    (garden / "demo" / "p3" / "tasks").mkdir(parents=True)
    store.invalidate()
    assert store.phase("demo", "p3").plant == "foxglove" and store.phase("demo", "p3").plate == "III"
    assert goals_text(p2.goals_path) == "# p2\n\nGoals."
    # frontmatter never reaches the brief or the planner
    from garden.brief import build_brief
    from garden.planner import plan_prompt

    t = store.task("DM-001")
    t.phase = "p2"
    assert "plant: poppy" not in build_brief(store, t).text
    assert "plant: poppy" not in plan_prompt(store, "demo", "p2")


def test_new_phase_assigns_next_plant(garden):
    from garden.scaffold import new_phase

    store = Store(garden)
    new_phase(store, "demo", "p3")
    store.invalidate()
    ph = store.phase("demo", "p3")
    assert ph.plant == "bramble" and ph.plate == "II"
    assert (garden / "demo" / "p3" / "goals.md").read_text().startswith("---\nplant: bramble\nlatin: Rubus fruticosus\nplate: II\n---")
    new_phase(store, "demo", "p4", plant="fern")
    store.invalidate()
    assert store.phase("demo", "p4").plant == "fern"
    # a mistyped plant is an error, and nothing is scaffolded
    import pytest

    with pytest.raises(ValueError, match="unknown plant 'fernn'"):
        new_phase(store, "demo", "p5", plant="fernn")
    assert not (garden / "demo" / "p5").exists()


def test_background_vine_is_generated_from_the_shared_symbols():
    from garden.plants import vine_svg

    svg = vine_svg()
    assert svg.startswith('<svg class="bg-vine" viewBox="0 0 300 440"')
    assert svg.count('<use href="#lf"') == 13
    assert '<use href="#tendril"' in svg
    assert 'aria-hidden="true"' in svg
    assert vine_svg(300).count("<path") == 2
