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


def _png(w=120, h=180):
    Image = pytest.importorskip("PIL.Image")
    import io

    im = Image.new("RGB", (w, h), (255, 255, 255))
    for x in range(30, 90):
        for y in range(40, 140):
            im.putpixel((x, y), (40, 80, 30))
    out = io.BytesIO()
    im.save(out, "PNG")
    return out.getvalue()


def _mock_client(fail_key: str = ""):
    import json

    import httpx

    png = _png()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "commons.wikimedia.org":
            # Each candidate prefix pins one exact Commons file (a trailing "0." or the full
            # cleaned name), so - like the real API - exactly one match comes back.
            prefix = request.url.params["aiprefix"]
            name = f"{prefix}jpg" if prefix.endswith(".") else f"{prefix}0.jpg"
            files = [{"name": name, "url": f"https://upload.example/{name}", "width": 1200, "height": 1800,
                      "descriptionurl": f"https://commons.wikimedia.org/wiki/File:{name}",
                      "extmetadata": {"Artist": {"value": "<a href=x>Otto Wilhelm Thomé</a>"}, "LicenseShortName": {"value": "Public domain"}}}]
            return httpx.Response(200, content=json.dumps({"query": {"allimages": files}}).encode())
        if fail_key and fail_key in str(request.url):
            return httpx.Response(503, text="upstream down")
        return httpx.Response(200, content=png)

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_fetch_all_publishes_a_complete_set_with_provenance(tmp_path):
    from garden.platefetch import fetch_all

    out = tmp_path / "static" / "plates"
    keys = [p["key"] for p in PLANTS]
    rows = fetch_all(out, height=120, log=lambda m: None, client=_mock_client())
    assert [r["key"] for r in rows] == keys
    assert all(r["title"] for r in rows)
    for key in keys:
        assert (out / f"{key}.webp").stat().st_size > 0 and (out / f"{key}-thumb.webp").stat().st_size > 0
    sources = (out / "SOURCES.md").read_text(encoding="utf-8")
    assert "Otto Wilhelm Thomé" in sources and sources.count("| Public domain |") == len(keys)
    assert not [d for d in out.parent.iterdir() if d.name.startswith(".plates-")]  # staging cleaned up


def test_fetch_all_failure_leaves_nothing_behind(tmp_path):
    import httpx

    from garden.platefetch import fetch_all

    out = tmp_path / "plates"
    (out).mkdir()
    (out / "pea.webp").write_bytes(b"old")
    with pytest.raises(httpx.HTTPStatusError):
        fetch_all(out, height=120, log=lambda m: None, client=_mock_client(fail_key="Digitalis"))
    assert sorted(f.name for f in out.iterdir()) == ["pea.webp"]  # the old plate untouched, no partial set
    assert (out / "pea.webp").read_bytes() == b"old"
    assert not [d for d in out.parent.iterdir() if d.name.startswith(".plates-")]
