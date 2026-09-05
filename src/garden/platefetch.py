"""Fetch the phase plates: scanned botanical illustrations from Wikimedia Commons.

The plates are from Otto Wilhelm Thomé's *Flora von Deutschland, Österreich und der Schweiz*
(Gera, 1885), chromolithographs long in the public domain. Commons hosts the scans, and
for most plates a "clean" version with the page background removed. This module resolves
each plant to a file on Commons, downloads it, crops the margins, downsamples it and writes
`<key>.webp` and `<key>-thumb.webp` under the web UI's static plates directory, plus a
`SOURCES.md` recording exactly what was taken from where. The set is staged and published
whole: a failure part-way through leaves whatever was there before.

Only `garden plants --fetch` calls this; nothing in the scheduler or the UIs touches the
network for plates. Needs Pillow (`pip install "context-garden[plates]"`).
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import httpx

from .plants import PLANTS

COMMONS_API = "https://commons.wikimedia.org/w/api.php"
USER_AGENT = "context-garden/plates (https://github.com/joshmarcus/context-garden)"
WORK = "Prof. Dr. Thomé's Flora von Deutschland, Österreich und der Schweiz"
ARTIST = "Otto Wilhelm Thomé"
YEAR = "1885"

# Commons names Thomé's plates "Illustration <Genus> <species>0.jpg" (and "... clean.jpg" for
# the background-removed version). Three plants have no plate under their own name: Thomé drew
# no corn poppy, so the poppy plate is his prickly poppy, Papaver argemone (Tafel 260); the
# bramble is his Tafel 398, Rubus thyrsoideus Wimm. of the R. fruticosus aggregate, filed under a
# misspelt "candidans"; and the male fern (Tafel 10) is missing from the plate-only set, so it
# comes from the Biodiversity Heritage Library's scan of the 1903 printing, which Commons files
# under Thomé's Flora as well.
#
# The foxglove prefix pins the original public-domain scan ("Digitalis_purpurea0.jpg"), not the
# "_clean" background-removed derivative, which Commons licenses CC BY-SA 3.0 as the editor's own
# work rather than a faithful reproduction of Thomé's plate: this repository is MIT-licensed and
# ships no share-alike material.
#
# The seven below are picked from Commons' "Thomé, Flora von Deutschland (modified)" category for
# colour that pops against the other five's greens and dusty pinks: peony's deep magenta, quince's
# blush, the lady's slipper's yellow pouch, thistle's violet, snapdragon's carmine, adonis' gloss
# yellow, daphne's magenta-on-bare-wood. Each prefix pins the exact background-removed (or
# otherwise cleaned) file named in the request, not a plain scan of the same plate.
CANDIDATES: dict[str, list[str]] = {
    "pea": ["Illustration_Pisum_sativum"],
    "bramble": ["Illustration_Rubus_candidans0."],
    "foxglove": ["Illustration_Digitalis_purpurea0."],
    "fern": ["Prof._Dr._Thomé's_Flora_von_Deutschland,_Österreich_und_der_Schweiz,_in_Wort_und_Bild,"
             "_für_Schule_und_Haus;_mit_..._Tafeln_..._von_Walter_Müller_(Pl._10)_(7845241910)"],
    "poppy": ["Illustration_Papaver_argemone0."],
    "peony": ["Illustration_Paeonia_mascula1."],
    "quince": ["Illustration_Cydonia_oblonga0_-_clean."],
    "orchid": ["Illustration_Cypripedium_calceolus0_clean."],
    "thistle": ["Illustration_Carduus_nutans0_white."],
    "snapdragon": ["Illustration_Antirrhinum_majus_clean."],
    "adonis": ["Illustration_Adonis_vernalis0_clean."],
    "daphne": ["Daphne_mezereum,_Thomé-347."],
}

# The edition's own title names a different plate illustrator than the work's author (Thomé):
# only the fern, whose plate is from the 1903 printing's Biodiversity Heritage Library scan,
# credited on its title page to Walter Müller (not "Migula, Walter", the 1903 text's reviser,
# who Commons' own Artist field lists alongside Thomé for that file).
ILLUSTRATOR: dict[str, str] = {"fern": "Walter Müller"}

# Where a plate did not come from a direct Commons upload of the scan.
SOURCE: dict[str, str] = {"fern": "Biodiversity Heritage Library"}


def pick_file(files: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Prefer the background-removed scan, then the plain plate, among an allimages result."""
    jpgs = [f for f in files if str(f.get("name", "")).lower().endswith((".jpg", ".jpeg", ".png"))]
    for f in jpgs:
        if "clean" in str(f.get("name", "")).lower():
            return f
    for f in jpgs:
        stem = str(f.get("name", "")).rsplit(".", 1)[0]
        if stem.endswith("0"):
            return f
    return jpgs[0] if jpgs else None


def resolve(key: str, client: httpx.Client) -> dict[str, Any]:
    for prefix in CANDIDATES[key]:
        r = client.get(COMMONS_API, params={
            "action": "query", "list": "allimages", "aiprefix": prefix, "ailimit": "50",
            "aiprop": "url|size|mime|extmetadata", "format": "json",
        })
        r.raise_for_status()
        files = r.json().get("query", {}).get("allimages", [])
        chosen = pick_file(files)
        if chosen:
            return chosen
    raise LookupError(f"no plate found on Commons for {key} (tried {', '.join(CANDIDATES[key])})")


def prepare(raw: bytes, height: int = 900, thumb_height: int = 160) -> tuple[bytes, bytes]:
    """Crop near-white margins, downsample, and encode the plate and its thumbnail as WebP."""
    import io

    from PIL import Image, ImageChops

    im = Image.open(io.BytesIO(raw)).convert("RGB")
    bg = Image.new("RGB", im.size, (255, 255, 255))
    diff = ImageChops.difference(im, bg).convert("L").point(lambda v: 255 if v > 28 else 0)
    box = diff.getbbox()
    if box:
        pad_x, pad_y = int(im.width * 0.03), int(im.height * 0.02)
        im = im.crop((max(0, box[0] - pad_x), max(0, box[1] - pad_y), min(im.width, box[2] + pad_x), min(im.height, box[3] + pad_y)))

    def encode(img: Image.Image, h: int) -> bytes:
        w = max(1, round(img.width * h / img.height))
        out = io.BytesIO()
        img.resize((w, h), Image.LANCZOS).save(out, "WEBP", quality=82, method=6)
        return out.getvalue()

    return encode(im, height), encode(im, thumb_height)


def fetch_all(out_dir: Path, keys: list[str] | None = None, height: int = 900, log=print,
              client: httpx.Client | None = None) -> list[dict[str, Any]]:
    """Fetch every plate into a staging directory first; only a complete set, with its
    SOURCES.md, is moved into `out_dir`, so a failure part-way leaves the UI as it was."""
    import shutil
    import tempfile

    out_dir.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".plates-", dir=out_dir.parent))
    rows: list[dict[str, Any]] = []
    own_client = client is None
    client = client or httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=60, follow_redirects=True)
    try:
        for p in PLANTS:
            key = p["key"]
            if keys and key not in keys:
                continue
            f = resolve(key, client)
            log(f"{key}: {f['name']} ({f.get('width')}x{f.get('height')})")
            r = client.get(f["url"])
            r.raise_for_status()
            main, thumb = prepare(r.content, height=height)
            (staging / f"{key}.webp").write_bytes(main)
            (staging / f"{key}-thumb.webp").write_bytes(thumb)
            meta = f.get("extmetadata") or {}
            raw_artist = _plain(meta.get("Artist", {}).get("value", ""))
            rows.append({
                "key": key, "latin": p["latin"], "title": f["name"], "url": f.get("descriptionurl") or f["url"],
                "illustrator": ILLUSTRATOR.get(key, ARTIST),
                "source": SOURCE.get(key, "Wikimedia Commons"),
                "editor": _editor(raw_artist),
                "license": _plain(meta.get("LicenseShortName", {}).get("value", "")) or "Public domain",
                "bytes": len(main),
            })
        (staging / "SOURCES.md").write_text(sources_markdown(rows), encoding="utf-8")
        for f in sorted(staging.iterdir()):
            f.replace(out_dir / f.name)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
        if own_client:
            client.close()
    return rows


def _plain(html: str) -> str:
    import re

    return re.sub(r"<[^>]+>", "", html).strip()


def _editor(raw_artist: str) -> str:
    """The Commons-credited editor of a cleaned derivative, verified from the file's own Artist
    metadata rather than assumed from a "_clean"/"_white" filename: most such files still name
    only Thomé as Artist (no separate editor is credited on the page), and a few explicitly
    credit a "derivative work" editor or a Commons username in place of Thomé."""
    import re

    for line in raw_artist.splitlines():
        line = line.strip()
        if line.lower().startswith("derivative work:"):
            return re.sub(r"\(talk\)\s*$", "", line.split(":", 1)[1], flags=re.I).strip()
    if raw_artist and "thom" not in raw_artist.lower():
        return raw_artist.strip()
    return ""


def sources_markdown(rows: list[dict[str, Any]]) -> str:
    import re

    def cell(value: Any) -> str:
        # Commons metadata (e.g. a multi-line Artist credit) can contain newlines, which would
        # otherwise split one table row into two and break the rest of the table.
        return re.sub(r"\s+", " ", str(value)).strip()

    lines = [
        "# Plate sources", "",
        f"Scanned plates from *{WORK}* ({ARTIST}, Gera, {YEAR}), via Wikimedia Commons. The work is in",
        "the public domain; the files here are cropped, downsampled WebP copies made by `garden plants --fetch`",
        f"on {dt.date.today().isoformat()}. Roles below are as verified on each file's own Commons page: the",
        "plate's illustrator (Thomé unless the edition names another), the scan's source, and the editor of a",
        "cleaned derivative where the file page credits one (a Commons username is an editor, not an artist).",
        "",
        "| plant | species | Commons file | illustrator | source | editor | licence | size |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        lines.append(f"| {cell(r['key'])} | *{cell(r['latin'])}* | [{cell(r['title'])}]({cell(r['url'])}) | "
                      f"{cell(r['illustrator'])} | {cell(r['source'])} | {cell(r['editor']) or '—'} | "
                      f"{cell(r['license'])} | {r['bytes'] // 1024} KB |")
    return "\n".join(lines) + "\n"
