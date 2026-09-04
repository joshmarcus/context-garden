"""Plate fetching: the pure parts (file choice, sources table, image preparation)."""

import pytest

from garden.plants import PLANTS
from garden.platefetch import CANDIDATES, pick_file, prepare, sources_markdown


def test_every_plant_has_commons_candidates_and_a_file_is_chosen_sensibly():
    assert set(CANDIDATES) == {p["key"] for p in PLANTS}
    files = [
        {"name": "Illustration_Pisum_sativum0.jpg", "url": "u0"},
        {"name": "Illustration_Pisum_sativum1.jpg", "url": "u1"},
        {"name": "Illustration_Pisum_sativum0_clean.jpg", "url": "uc"},
    ]
    assert pick_file(files)["url"] == "uc"  # background-removed scan first
    assert pick_file(files[:2])["url"] == "u0"  # else the plate itself
    assert pick_file([{"name": "notes.pdf", "url": "x"}]) is None


def test_sources_markdown_lists_each_plate():
    md = sources_markdown([{"key": "pea", "latin": "Pisum sativum", "title": "Illustration_Pisum_sativum0_clean.jpg",
                            "url": "https://commons.wikimedia.org/wiki/File:x", "artist": "Otto Wilhelm Thomé", "license": "Public domain", "bytes": 150000}])
    assert "| pea | *Pisum sativum* | [Illustration_Pisum_sativum0_clean.jpg](https://commons.wikimedia.org/wiki/File:x) | Otto Wilhelm Thomé | Public domain | 146 KB |" in md
    assert "1885" in md


def test_prepare_crops_margins_and_makes_a_thumbnail():
    Image = pytest.importorskip("PIL.Image")
    import io

    im = Image.new("RGB", (400, 600), (255, 255, 255))
    for x in range(100, 300):
        for y in range(150, 450):
            im.putpixel((x, y), (60, 90, 40))
    raw = io.BytesIO()
    im.save(raw, "PNG")
    main, thumb = prepare(raw.getvalue(), height=300, thumb_height=60)
    big, small = Image.open(io.BytesIO(main)), Image.open(io.BytesIO(thumb))
    assert big.format == "WEBP" and big.height == 300 and small.height == 60
    assert big.width < 300  # the white margins were cropped, so it is narrower than 2:3
