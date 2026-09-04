"""The botanical system: a plant for every phase, a growth stage for every task state.

Every plant is an SVG drawing (no image files) defined once as reusable symbols; the web UI
inlines `DEFS` once per page and places plants and stage glyphs with `<use>`. Colours come
from CSS variables set by the theme, so the same drawing renders as a pressed specimen in
light and dark.
"""

from __future__ import annotations

import math
from typing import Any

# Assigned to phases in this order, per product; a product with more phases than plants
# wraps around (the plate number still distinguishes them).
PLANTS: list[dict[str, str]] = [
    {"key": "pea", "latin": "Pisum sativum", "common": "garden pea", "note": "a climber: it needs the trellis it is given"},
    {"key": "bramble", "latin": "Rubus fruticosus", "common": "bramble", "note": "thorns first, fruit after"},
    {"key": "foxglove", "latin": "Digitalis purpurea", "common": "foxglove", "note": "tall, and every bell in its turn"},
    {"key": "fern", "latin": "Dryopteris filix-mas", "common": "male fern", "note": "unfurls one frond at a time"},
    {"key": "poppy", "latin": "Papaver rhoeas", "common": "corn poppy", "note": "short-lived flower, long-lived seed"},
    {"key": "peony", "latin": "Paeonia mascula", "common": "peony", "note": "big first, before anything else opens"},
    {"key": "quince", "latin": "Cydonia oblonga", "common": "quince", "note": "blossoms early, fruit takes all season"},
    {"key": "orchid", "latin": "Cypripedium calceolus", "common": "lady's slipper orchid", "note": "one pouch, waiting for the right bee"},
    {"key": "thistle", "latin": "Carduus nutans", "common": "musk thistle", "note": "armored, and nodding anyway"},
    {"key": "snapdragon", "latin": "Antirrhinum majus", "common": "snapdragon", "note": "answers back when squeezed"},
    {"key": "adonis", "latin": "Adonis vernalis", "common": "spring pheasant's eye", "note": "opens before the frost is sure it's gone"},
    {"key": "daphne", "latin": "Daphne mezereum", "common": "mezereon", "note": "flowers before its own leaves"},
]
PLANT_BY_KEY = {p["key"]: p for p in PLANTS}

# The original five have a hand-drawn specimen in DEFS; the rest arrived as scanned plates only
# (`garden plants --fetch`) and share the generic "sprig" drawing as their pre-scan fallback.
DRAWN_PLANTS = {"pea", "bramble", "foxglove", "fern", "poppy"}

# Where a scanned plate for a plant lives once fetched (`garden plants --fetch`): the web UI shows
# it in place of the drawn specimen when the file exists, and falls back to the drawing otherwise.
PLATE_CREDIT = "Thomé, Flora von Deutschland, 1885"


def plate_filename(key: str, thumb: bool = False) -> str:
    key = key if key in PLANT_BY_KEY else PLANTS[0]["key"]
    return f"{key}-thumb.webp" if thumb else f"{key}.webp"

STAGE: dict[str, str] = {
    "draft": "st-seed", "ready": "st-sprout", "running": "st-leaf", "waiting_human": "st-tag", "awaiting_triage": "st-bud",
    "in_review": "st-flower", "changes_requested": "st-cut", "done": "st-fruit", "failed": "st-wilt", "cancelled": "st-pressed",
    "wont_do": "st-fallow", "blocked": "st-seed",
}
STAGE_WORD: dict[str, str] = {
    "draft": "seed", "ready": "sprout", "running": "in leaf", "waiting_human": "bud, tagged", "awaiting_triage": "in bud",
    "in_review": "in flower", "changes_requested": "pruned", "done": "in fruit", "failed": "wilted", "cancelled": "pressed",
    "wont_do": "set aside", "blocked": "seed, waiting",
}

DEFS = r'''
<svg width="0" height="0" style="position:absolute" aria-hidden="true"><defs>
<pattern id="hatch" patternUnits="userSpaceOnUse" width="4" height="4" patternTransform="rotate(45)"><rect width="4" height="4" fill="var(--leaf-tint)"/><line x1="0" y1="0" x2="0" y2="4" stroke="var(--hatch)" stroke-width="0.7"/></pattern>
<pattern id="hatch2" patternUnits="userSpaceOnUse" width="4" height="4" patternTransform="rotate(-40)"><rect width="4" height="4" fill="var(--petal-tint)"/><line x1="0" y1="0" x2="0" y2="4" stroke="var(--hatch)" stroke-width="0.6"/></pattern>
<pattern id="hatch3" patternUnits="userSpaceOnUse" width="3" height="3" patternTransform="rotate(60)"><rect width="3" height="3" fill="var(--berry-tint)"/><line x1="0" y1="0" x2="0" y2="3" stroke="var(--hatch)" stroke-width="0.6"/></pattern>
<filter id="wobble" x="-5%" y="-5%" width="110%" height="110%"><feTurbulence type="fractalNoise" baseFrequency="0.035" numOctaves="2" seed="3" result="n"/><feDisplacementMap in="SourceGraphic" in2="n" scale="2.2" xChannelSelector="R" yChannelSelector="G"/></filter>
<filter id="wash" x="-20%" y="-20%" width="140%" height="140%"><feGaussianBlur stdDeviation="2.4"/></filter>

<g id="lf"><path d="M0 0 C6 -11 24 -13 34 0 C24 13 6 11 0 0 Z" fill="var(--leaf)" stroke="var(--ink)" stroke-width="1.1" stroke-linejoin="round"/><path d="M2 0 L31 0" stroke="var(--ink)" stroke-width="0.7" fill="none"/><path d="M8 -1 L13 -6 M14 -1 L20 -8 M20 -1 L26 -6 M8 1 L13 6 M14 1 L20 8 M20 1 L26 6" stroke="var(--vein)" stroke-width="0.5" fill="none"/></g>
<g id="tendril"><path d="M0 0 c14 -8 26 -4 26 6 c0 9 -12 11 -14 3 c-1 -6 7 -8 9 -2" fill="none" stroke="var(--ink)" stroke-width="0.9" stroke-linecap="round"/></g>
<g id="peaflower"><path d="M0 0 C-14 -6 -16 -28 -2 -32 C12 -28 12 -6 0 0 Z" fill="var(--petal)" stroke="var(--ink)" stroke-width="1"/><path d="M-3 -2 C-11 -8 -7 -20 -1 -15 Z M3 -2 C11 -8 7 -20 1 -15 Z" fill="var(--petal2)" stroke="var(--ink)" stroke-width="0.8"/><path d="M-2 0 C-1 5 1 5 2 0" stroke="var(--ink)" stroke-width="0.8" fill="none"/></g>
<g id="pod"><path d="M0 0 C10 -6 34 -2 46 14 C30 20 8 14 0 0 Z" fill="var(--leaf)" stroke="var(--ink)" stroke-width="1.1"/><circle cx="12" cy="6" r="3.2" fill="var(--pea)" stroke="var(--ink)" stroke-width=".6"/><circle cx="22" cy="8.5" r="3.2" fill="var(--pea)" stroke="var(--ink)" stroke-width=".6"/><circle cx="32" cy="11" r="3.2" fill="var(--pea)" stroke="var(--ink)" stroke-width=".6"/></g>
<g id="pea">
  <path d="M100 292 C92 250 112 214 102 176 C94 146 112 118 100 84 C94 66 100 48 104 30" fill="none" stroke="var(--ink)" stroke-width="1.6" stroke-linecap="round"/>
  <path d="M97 288 C90 250 109 214 99 178" fill="none" stroke="var(--hatch)" stroke-width="0.6" stroke-dasharray="1.5 2"/>
  <use href="#lf" transform="translate(101 252) rotate(-28)"/><use href="#lf" transform="translate(101 252) scale(-1 1) rotate(-28)"/>
  <use href="#lf" transform="translate(105 220) rotate(-22) scale(.95)"/><use href="#lf" transform="translate(105 220) scale(-1 1) rotate(-22) scale(.95)"/>
  <use href="#pod" transform="translate(104 198) rotate(20)"/>
  <use href="#lf" transform="translate(98 186) rotate(-30) scale(.9)"/><use href="#lf" transform="translate(98 186) scale(-1 1) rotate(-30) scale(.9)"/>
  <use href="#peaflower" transform="translate(84 168) rotate(-30) scale(.9)"/>
  <use href="#lf" transform="translate(103 150) rotate(-26) scale(.85)"/><use href="#lf" transform="translate(103 150) scale(-1 1) rotate(-26) scale(.85)"/>
  <use href="#peaflower" transform="translate(118 132) rotate(25) scale(.85)"/>
  <use href="#tendril" transform="translate(106 118) rotate(-10)"/>
  <use href="#lf" transform="translate(99 92) rotate(-30) scale(.75)"/><use href="#lf" transform="translate(99 92) scale(-1 1) rotate(-30) scale(.75)"/>
  <use href="#lf" transform="translate(103 60) rotate(-34) scale(.6)"/><use href="#lf" transform="translate(103 60) scale(-1 1) rotate(-34) scale(.6)"/>
  <use href="#tendril" transform="translate(103 44) rotate(-30) scale(.8)"/><use href="#tendril" transform="translate(103 40) scale(-1 1) rotate(-50) scale(.7)"/>
</g>

<g id="bleaf"><path d="M16 2 C4 0 -3 -14 1 -24 C4 -32 10 -37 16 -41 C22 -37 28 -32 31 -24 C35 -14 28 0 16 2 Z" fill="var(--leaf)" stroke="var(--ink)" stroke-width="1" stroke-linejoin="round"/><path d="M16 -38 L16 0" stroke="var(--ink)" stroke-width=".6"/><path d="M16 -28 L7 -20 M16 -20 L5 -12 M16 -12 L8 -5 M16 -28 L25 -20 M16 -20 L27 -12 M16 -12 L24 -5" stroke="var(--vein)" stroke-width=".45" fill="none"/><path d="M1 -24 C4 -32 10 -37 16 -41 C22 -37 28 -32 31 -24" fill="none" stroke="var(--ink)" stroke-width="1.2" stroke-dasharray="1.6 1.4"/></g>
<g id="thorn"><path d="M0 0 l7 -3 l-2.5 6.5 z" fill="var(--ink)"/></g>
<g id="berry"><circle cx="0" cy="0" r="3.4" fill="var(--berry)" stroke="var(--ink)" stroke-width=".6"/><circle cx="6" cy="1" r="3.4" fill="var(--berry)" stroke="var(--ink)" stroke-width=".6"/><circle cx="3" cy="6" r="3.4" fill="var(--berry)" stroke="var(--ink)" stroke-width=".6"/><circle cx="-3.5" cy="5" r="3.2" fill="var(--berry)" stroke="var(--ink)" stroke-width=".6"/><circle cx="2" cy="-5" r="3.1" fill="var(--berry)" stroke="var(--ink)" stroke-width=".6"/><circle cx="-4" cy="-2" r="3" fill="var(--berry)" stroke="var(--ink)" stroke-width=".6"/><circle cx="8" cy="7" r="2.9" fill="var(--berry)" stroke="var(--ink)" stroke-width=".6"/><circle cx="1" cy="11" r="2.9" fill="var(--berry)" stroke="var(--ink)" stroke-width=".6"/><path d="M-1 -8 l-4 -5 M1 -8 l1 -6 M3 -8 l5 -4" stroke="var(--ink)" stroke-width=".7" fill="none"/></g>
<g id="bramble">
  <path d="M24 292 C40 220 70 160 130 128 C170 108 196 84 190 44" fill="none" stroke="var(--ink)" stroke-width="1.8" stroke-linecap="round"/>
  <path d="M62 292 C70 250 58 210 92 178 C110 162 130 156 146 150" fill="none" stroke="var(--ink)" stroke-width="1.4" stroke-linecap="round"/>
  <use href="#thorn" transform="translate(34 262) rotate(-70)"/><use href="#thorn" transform="translate(46 236) rotate(-65)"/><use href="#thorn" transform="translate(64 204) rotate(-55)"/><use href="#thorn" transform="translate(86 172) rotate(-40)"/><use href="#thorn" transform="translate(112 144) rotate(-25)"/><use href="#thorn" transform="translate(150 118) rotate(-15)"/><use href="#thorn" transform="translate(176 100) rotate(-10)"/><use href="#thorn" transform="translate(68 240) rotate(-80)"/><use href="#thorn" transform="translate(74 200) rotate(-60)"/>
  <g transform="translate(100 162) scale(1.35)"><use href="#bleaf" transform="rotate(-42)"/><use href="#bleaf" transform="rotate(-2)"/><use href="#bleaf" transform="rotate(38)"/></g>
  <g transform="translate(160 106) scale(1.05)"><use href="#bleaf" transform="rotate(-50)"/><use href="#bleaf" transform="rotate(-8)"/><use href="#bleaf" transform="rotate(34)"/></g>
  <g transform="translate(56 230) scale(.9)"><use href="#bleaf" transform="rotate(-70)"/><use href="#bleaf" transform="rotate(-25)"/></g>
  <use href="#berry" transform="translate(178 74)"/><use href="#berry" transform="translate(140 138) scale(.9)"/><g style="--berry:var(--berry2)"><use href="#berry" transform="translate(120 128) scale(.7)"/></g>
  <path d="M186 52 c-2 -8 4 -14 8 -12" stroke="var(--ink)" stroke-width="1" fill="none"/>
</g>

<g id="bell"><path d="M0 0 C-7 10 -9 24 -3 34 C1 37 7 37 11 34 C17 24 15 10 8 0 C5 -2 3 -2 0 0 Z" fill="var(--petal)" stroke="var(--ink)" stroke-width="1"/><circle cx="3" cy="26" r="1" fill="var(--ink)"/><circle cx="7.5" cy="22" r="1" fill="var(--ink)"/><circle cx="4" cy="18" r=".9" fill="var(--ink)"/><circle cx="8" cy="30" r=".9" fill="var(--ink)"/></g>
<g id="foxglove">
  <path d="M100 292 C100 240 102 180 100 120 C99 90 100 60 100 34" fill="none" stroke="var(--ink)" stroke-width="1.6" stroke-linecap="round"/>
  <path d="M100 282 C72 266 40 272 32 298 C60 302 90 298 100 282 Z" fill="var(--leaf)" stroke="var(--ink)" stroke-width="1.1"/><path d="M100 282 C128 266 160 272 168 298 C140 302 110 298 100 282 Z" fill="var(--leaf)" stroke="var(--ink)" stroke-width="1.1"/><path d="M98 284 L42 292 M102 284 L158 292" stroke="var(--ink)" stroke-width=".6"/>
  <path d="M100 262 C82 250 60 256 54 274 C74 276 92 272 100 262 Z" fill="var(--leaf)" stroke="var(--ink)" stroke-width="1"/><path d="M100 262 C118 250 140 256 146 274 C126 276 108 272 100 262 Z" fill="var(--leaf)" stroke="var(--ink)" stroke-width="1"/>
  <use href="#bell" transform="translate(100 226) rotate(-95)"/><use href="#bell" transform="translate(100 206) rotate(95) scale(-1 1) scale(.95)"/>
  <use href="#bell" transform="translate(100 186) rotate(-92) scale(.9)"/><use href="#bell" transform="translate(100 168) rotate(92) scale(-1 1) scale(.85)"/>
  <use href="#bell" transform="translate(100 150) rotate(-88) scale(.8)"/><use href="#bell" transform="translate(100 134) rotate(88) scale(-1 1) scale(.72)"/>
  <use href="#bell" transform="translate(100 118) rotate(-84) scale(.64)"/><use href="#bell" transform="translate(100 104) rotate(84) scale(-1 1) scale(.56)"/>
  <use href="#bell" transform="translate(100 92) rotate(-80) scale(.48)"/><use href="#bell" transform="translate(100 82) rotate(80) scale(-1 1) scale(.4)"/>
  <ellipse cx="96" cy="66" rx="3" ry="5.5" transform="rotate(-20 96 66)" fill="var(--petal)" stroke="var(--ink)" stroke-width=".8"/><ellipse cx="104" cy="56" rx="2.6" ry="5" transform="rotate(20 104 56)" fill="var(--petal)" stroke="var(--ink)" stroke-width=".8"/><ellipse cx="100" cy="44" rx="2.2" ry="4.4" fill="var(--petal)" stroke="var(--ink)" stroke-width=".8"/>
</g>

<g id="pinna"><path d="M0 0 C4 -3 8 -4 12 -3 C16 -5 20 -6 24 -4 C28 -6 32 -6 36 -3 C40 -5 44 -4 48 -2 C52 -3 56 -1 60 0 C56 1 52 3 48 2 C44 4 40 5 36 3 C32 6 28 6 24 4 C20 6 16 5 12 3 C8 4 4 3 0 0 Z" fill="var(--leaf)" stroke="var(--ink)" stroke-width=".9" stroke-linejoin="round"/><path d="M0 0 L58 0" stroke="var(--ink)" stroke-width=".55"/></g>
<g id="fern">
  <path d="M60 292 C70 230 90 170 120 110 C135 82 150 60 170 40" fill="none" stroke="var(--ink)" stroke-width="1.5" stroke-linecap="round"/>
  <use href="#pinna" transform="translate(64 268) rotate(-50) scale(1.05)"/><use href="#pinna" transform="translate(64 268) rotate(-130) scale(-1 1) scale(1.05)"/>
  <use href="#pinna" transform="translate(70 244) rotate(-52) scale(1.1)"/><use href="#pinna" transform="translate(70 244) rotate(-128) scale(-1 1) scale(1.1)"/>
  <use href="#pinna" transform="translate(78 218) rotate(-52) scale(1.05)"/><use href="#pinna" transform="translate(78 218) rotate(-128) scale(-1 1) scale(1.05)"/>
  <use href="#pinna" transform="translate(88 194) rotate(-54) scale(1)"/><use href="#pinna" transform="translate(88 194) rotate(-126) scale(-1 1) scale(1)"/>
  <use href="#pinna" transform="translate(100 170) rotate(-56) scale(.9)"/><use href="#pinna" transform="translate(100 170) rotate(-124) scale(-1 1) scale(.9)"/>
  <use href="#pinna" transform="translate(112 146) rotate(-58) scale(.8)"/><use href="#pinna" transform="translate(112 146) rotate(-122) scale(-1 1) scale(.8)"/>
  <use href="#pinna" transform="translate(124 122) rotate(-60) scale(.68)"/><use href="#pinna" transform="translate(124 122) rotate(-120) scale(-1 1) scale(.68)"/>
  <use href="#pinna" transform="translate(136 100) rotate(-62) scale(.55)"/><use href="#pinna" transform="translate(136 100) rotate(-118) scale(-1 1) scale(.55)"/>
  <use href="#pinna" transform="translate(148 80) rotate(-64) scale(.42)"/><use href="#pinna" transform="translate(148 80) rotate(-116) scale(-1 1) scale(.42)"/>
  <use href="#pinna" transform="translate(158 62) rotate(-66) scale(.3)"/><use href="#pinna" transform="translate(158 62) rotate(-114) scale(-1 1) scale(.3)"/>
  <path d="M170 40 c6 -8 2 -16 -6 -14 c-6 2 -4 10 2 10" fill="none" stroke="var(--ink)" stroke-width="1"/>
</g>

<g id="petal"><path d="M0 0 C-30 -6 -40 -40 -12 -46 C0 -50 14 -44 16 -30 C18 -14 12 -2 0 0 Z" fill="var(--poppy)" stroke="var(--ink)" stroke-width="1"/><path d="M-6 -8 C-16 -18 -20 -30 -14 -40" stroke="var(--hatch)" stroke-width=".5" fill="none"/></g>
<g id="poppy">
  <path d="M120 292 C118 250 124 200 112 150 C106 126 110 100 116 80" fill="none" stroke="var(--ink)" stroke-width="1.5" stroke-linecap="round"/>
  <path d="M116 260 l-4 -2 M118 230 l-4 -1 M114 200 l-4 -2 M112 170 l-4 -1 M110 140 l-4 -2 M112 112 l-4 -1" stroke="var(--ink)" stroke-width=".7"/>
  <path d="M120 292 C124 250 140 220 150 190 C156 172 152 154 146 140" fill="none" stroke="var(--ink)" stroke-width="1.2" stroke-linecap="round"/>
  <ellipse cx="146" cy="128" rx="9" ry="13" fill="var(--leaf)" stroke="var(--ink)" stroke-width="1"/><path d="M137 122 c3 -3 15 -3 18 0 M140 116 c3 -2 9 -2 12 0" stroke="var(--ink)" stroke-width=".8" fill="none"/><path d="M142 115 l-2 -5 M146 114 l0 -5 M150 115 l2 -5" stroke="var(--ink)" stroke-width=".8"/>
  <path d="M60 292 C70 250 62 200 90 170 C100 158 104 150 96 140" fill="none" stroke="var(--ink)" stroke-width="1.2" stroke-linecap="round"/><ellipse cx="94" cy="134" rx="5" ry="8" transform="rotate(-30 94 134)" fill="var(--leaf)" stroke="var(--ink)" stroke-width="1"/>
  <path d="M96 236 C84 232 70 236 62 250 C76 254 90 250 96 236 Z" fill="var(--leaf)" stroke="var(--ink)" stroke-width="1"/><path d="M124 210 C136 204 150 206 158 220 C144 226 130 222 124 210 Z" fill="var(--leaf)" stroke="var(--ink)" stroke-width="1"/>
  <g transform="translate(116 82)"><use href="#petal" transform="rotate(10) scale(1.05)"/><use href="#petal" transform="rotate(100) scale(1.05)"/><use href="#petal" transform="rotate(190) scale(1.05)"/><use href="#petal" transform="rotate(280) scale(1.05)"/><circle cx="0" cy="0" r="7" fill="var(--ink)"/><circle cx="0" cy="0" r="3" fill="var(--seed)"/><path d="M-11 -4 l-4 -3 M11 -4 l4 -3 M-8 8 l-3 5 M8 8 l3 5 M-12 3 l-5 1 M12 3 l5 1" stroke="var(--ink)" stroke-width=".8"/></g>
</g>

<!-- generic sprig: the pre-scan placeholder for a plant with a plate but no bespoke drawing -->
<g id="sprig">
  <path d="M100 292 C98 240 102 190 100 140 C99 110 100 80 100 50" fill="none" stroke="var(--ink)" stroke-width="1.6" stroke-linecap="round"/>
  <use href="#lf" transform="translate(100 230) rotate(-28) scale(1.05)"/><use href="#lf" transform="translate(100 230) scale(-1 1) rotate(-28) scale(1.05)"/>
  <use href="#lf" transform="translate(100 178) rotate(-25) scale(.95)"/><use href="#lf" transform="translate(100 178) scale(-1 1) rotate(-25) scale(.95)"/>
  <use href="#lf" transform="translate(100 126) rotate(-30) scale(.82)"/><use href="#lf" transform="translate(100 126) scale(-1 1) rotate(-30) scale(.82)"/>
  <use href="#bell" transform="translate(100 80) rotate(-90) scale(.7)"/>
</g>

<!-- growth-stage glyphs: 24x24 -->
<g id="st-seed"><ellipse cx="12" cy="13" rx="5" ry="7.5" transform="rotate(-25 12 13)" fill="var(--seed)" stroke="var(--ink)" stroke-width="1.1"/><path d="M9.5 9 c1.2 2 1.2 6 0 8" stroke="var(--ink)" stroke-width=".8" fill="none"/></g>
<g id="st-sprout"><path d="M12 21 V12" stroke="var(--ink)" stroke-width="1.2" fill="none"/><path d="M12 13 C6 13 5 8 4 6 C9 6 12 9 12 13 Z" fill="var(--leaf)" stroke="var(--ink)" stroke-width="1"/><path d="M12 13 C18 13 19 8 20 6 C15 6 12 9 12 13 Z" fill="var(--leaf)" stroke="var(--ink)" stroke-width="1"/><path d="M6 21 H18" stroke="var(--ink)" stroke-width="1"/></g>
<g id="st-leaf"><path d="M4 20 C4 8 14 4 20 4 C20 12 14 20 4 20 Z" fill="var(--leaf)" stroke="var(--ink)" stroke-width="1.1"/><path d="M5 19 L17 7" stroke="var(--ink)" stroke-width=".7"/></g>
<g id="st-bud"><path d="M12 22 V14" stroke="var(--ink)" stroke-width="1.2"/><path d="M12 14 C7 14 7 5 12 3 C17 5 17 14 12 14 Z" fill="var(--petal)" stroke="var(--ink)" stroke-width="1"/><path d="M12 14 C9 12 9 8 12 6 M12 14 C15 12 15 8 12 6" stroke="var(--ink)" stroke-width=".7" fill="none"/><path d="M12 14 C9 15 7 13 7 11 M12 14 C15 15 17 13 17 11" stroke="var(--ink)" stroke-width=".8" fill="none"/></g>
<g id="st-flower"><ellipse cx="12" cy="6" rx="3" ry="5" fill="var(--petal)" stroke="var(--ink)" stroke-width=".9"/><ellipse cx="12" cy="6" rx="3" ry="5" transform="rotate(72 12 12)" fill="var(--petal)" stroke="var(--ink)" stroke-width=".9"/><ellipse cx="12" cy="6" rx="3" ry="5" transform="rotate(144 12 12)" fill="var(--petal)" stroke="var(--ink)" stroke-width=".9"/><ellipse cx="12" cy="6" rx="3" ry="5" transform="rotate(216 12 12)" fill="var(--petal)" stroke="var(--ink)" stroke-width=".9"/><ellipse cx="12" cy="6" rx="3" ry="5" transform="rotate(288 12 12)" fill="var(--petal)" stroke="var(--ink)" stroke-width=".9"/><circle cx="12" cy="12" r="2.6" fill="var(--ink)"/></g>
<g id="st-cut"><circle cx="12" cy="12" r="7.5" fill="none" stroke="var(--ink)" stroke-width="1" stroke-dasharray="2 1.6"/><circle cx="12" cy="12" r="2.4" fill="var(--ink)"/><path d="M4 4 l4 4 M20 4 l-4 4" stroke="var(--ink)" stroke-width="1"/></g>
<g id="st-fruit"><path d="M12 7 C16 5 21 9 21 15 C21 20 17 22 12 22 C7 22 3 20 3 15 C3 9 8 5 12 7 Z" fill="var(--apple)" stroke="var(--ink)" stroke-width=".9"/><path d="M12 7 C11 9 11 12 12 14" stroke="var(--ink)" stroke-width=".6" fill="none"/><path d="M12 7 L13 3" stroke="var(--ink)" stroke-width="1" stroke-linecap="round" fill="none"/><path d="M13 4 C16 1 20 2 20 6 C20 8 15 8 13 5 Z" fill="var(--leaf)" stroke="var(--ink)" stroke-width=".7"/></g>
<g id="st-wilt"><path d="M8 22 C8 14 10 10 14 8 C18 6 18 10 14 12" stroke="var(--ink)" stroke-width="1.2" fill="none"/><path d="M14 12 C18 10 21 14 17 17 C14 18 13 15 14 12 Z" fill="var(--wilt)" stroke="var(--ink)" stroke-width=".9"/><path d="M5 22 H12" stroke="var(--ink)" stroke-width="1"/></g>
<g id="st-pressed"><path d="M5 19 C5 9 13 5 19 5 C19 13 13 19 5 19 Z" fill="var(--pressed)" stroke="var(--ink)" stroke-width="1"/><path d="M6 18 L17 7" stroke="var(--ink)" stroke-width=".6"/><circle cx="18.5" cy="5.5" r="2.2" fill="var(--pin)" stroke="var(--ink)" stroke-width=".6"/></g>
<g id="st-tag"><path d="M10 22 V13" stroke="var(--ink)" stroke-width="1.2"/><path d="M10 13 C6 13 6 6 10 4 C14 6 14 13 10 13 Z" fill="var(--petal)" stroke="var(--ink)" stroke-width="1"/><path d="M12 8 l9 -3.5 v6 l-9 3.5 z" fill="var(--paper)" stroke="var(--ink)" stroke-width=".9" stroke-linejoin="round"/><text x="14.6" y="10.6" font-size="5" font-family="serif" font-weight="700" fill="var(--ink)">?</text></g>
<!-- wont_do: seeds saved back into a labelled packet, set aside rather than sown -->
<g id="st-fallow"><rect x="5" y="6.5" width="14" height="12" rx="1" fill="var(--paper)" stroke="var(--ink)" stroke-width="1.1"/><path d="M5 10 H19" stroke="var(--ink)" stroke-width=".8"/><path d="M8 13 H16 M8 15.5 H13" stroke="var(--ink)" stroke-width=".7"/><ellipse cx="15.5" cy="15.5" rx="1.4" ry="2" transform="rotate(-25 15.5 15.5)" fill="var(--seed)" stroke="var(--ink)" stroke-width=".5"/></g>
</defs></svg>
'''


def roman(n: int) -> str:
    out = ""
    for value, numeral in ((1000, "M"), (900, "CM"), (500, "D"), (400, "CD"), (100, "C"), (90, "XC"), (50, "L"), (40, "XL"),
                           (10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I")):
        while n >= value:
            out += numeral
            n -= value
    return out


def assign_plant(taken: list[str]) -> str:
    """The next plant in order that no phase of this product uses yet (wrapping when all are used)."""
    for p in PLANTS:
        if p["key"] not in taken:
            return p["key"]
    return PLANTS[len(taken) % len(PLANTS)]["key"]


def positional_plant(index: int, taken: list[str]) -> str:
    """The plant for the phase at `index` when its goals.md names none: the plant at that
    position, or the next one after it that no other phase uses, so pinning one phase's
    plant never moves the others; wraps around when every plant is used."""
    n = len(PLANTS)
    for k in range(n):
        key = PLANTS[(index + k) % n]["key"]
        if key not in taken:
            return key
    return PLANTS[index % n]["key"]


def plant_info(key: str) -> dict[str, str]:
    return PLANT_BY_KEY.get(key, PLANTS[0])


def plant_svg(key: str, width: int, height: int, cls: str = "plant", style: str = "") -> str:
    key = key if key in PLANT_BY_KEY else PLANTS[0]["key"]
    sym = key if key in DRAWN_PLANTS else "sprig"
    return (f'<svg class="{cls}" viewBox="0 0 200 300" width="{width}" height="{height}" style="{style}" aria-hidden="true">'
            f'<use href="#{sym}"/></svg>')


def stage_svg(status: str, size: int = 20, cls: str = "stage") -> str:
    sym = STAGE.get(status, "st-seed")
    word = STAGE_WORD.get(status, status)
    return (f'<svg class="{cls}" viewBox="0 0 24 24" width="{size}" height="{size}" role="img" aria-label="{word}">'
            f'<title>{word}</title><use href="#{sym}"/></svg>')


def stage_word(status: str) -> str:
    return STAGE_WORD.get(status, status.replace("_", " "))


def describe(plant: str, plate: str) -> dict[str, Any]:
    info = plant_info(plant)
    return {"key": info["key"], "latin": info["latin"], "common": info["common"], "note": info["note"], "plate": plate}


# ---- the background vine ------------------------------------------------------------
# One climbing stem drawn once per page behind the content: it enters from the bottom right
# of the viewport and curves up and to the left. Leaves and tendrils reuse the pea's symbols,
# placed along two cubic curves; opacity and colour come from the theme, so it stays faint.
_VINE_SEGMENTS = [
    ((292.0, 440.0), (312.0, 352.0), (228.0, 300.0), (246.0, 226.0)),
    ((246.0, 226.0), (266.0, 152.0), (156.0, 44.0), (44.0, 58.0)),
]


def _bezier(seg: tuple, t: float) -> tuple[float, float]:
    (x0, y0), (x1, y1), (x2, y2), (x3, y3) = seg
    u = 1 - t
    return (u ** 3 * x0 + 3 * u * u * t * x1 + 3 * u * t * t * x2 + t ** 3 * x3,
            u ** 3 * y0 + 3 * u * u * t * y1 + 3 * u * t * t * y2 + t ** 3 * y3)


def _tangent(seg: tuple, t: float) -> float:
    """Direction of travel along the curve at t, in degrees (SVG coordinates)."""
    (x0, y0), (x1, y1), (x2, y2), (x3, y3) = seg
    u = 1 - t
    dx = 3 * u * u * (x1 - x0) + 6 * u * t * (x2 - x1) + 3 * t * t * (x3 - x2)
    dy = 3 * u * u * (y1 - y0) + 6 * u * t * (y2 - y1) + 3 * t * t * (y3 - y2)
    return math.degrees(math.atan2(dy, dx))


def _along(s: float) -> tuple[float, float, float]:
    """A point and tangent for s in [0, 1] over both segments."""
    seg, t = (_VINE_SEGMENTS[0], s * 2) if s < 0.5 else (_VINE_SEGMENTS[1], (s - 0.5) * 2)
    x, y = _bezier(seg, t)
    return x, y, _tangent(seg, t)


def vine_svg(width: int = 500, cls: str = "bg-vine") -> str:
    """The background vine as one inline SVG (viewBox 300 x 440, rendered `width` wide)."""
    parts: list[str] = []
    d = "M{:.0f} {:.0f}".format(*_VINE_SEGMENTS[0][0])
    for seg in _VINE_SEGMENTS:
        d += " C{:.0f} {:.0f} {:.0f} {:.0f} {:.0f} {:.0f}".format(*seg[1], *seg[2], *seg[3])
    parts.append(f'<path class="stem" d="{d}" fill="none" stroke="var(--ink)" stroke-width="1.8" stroke-linecap="round"/>')
    parts.append(f'<path d="{d}" fill="none" stroke="var(--hatch)" stroke-width="0.6" stroke-dasharray="1.5 2.5" transform="translate(-3 0)"/>')
    n = 13
    for i in range(n):
        s = 0.06 + 0.86 * i / (n - 1)
        x, y, ang = _along(s)
        side = 1 if i % 2 else -1
        scale = 1.55 - 1.0 * (i / (n - 1))
        parts.append(f'<use href="#lf" transform="translate({x:.1f} {y:.1f}) rotate({ang + side * 62:.1f}) scale({scale:.2f})"/>')
        if i in (3, 8):
            parts.append(f'<use href="#tendril" transform="translate({x:.1f} {y:.1f}) rotate({ang - side * 40:.1f}) scale({scale * 0.8:.2f})"/>')
    tx, ty, tang = _along(1.0)
    parts.append(f'<use href="#tendril" transform="translate({tx:.1f} {ty:.1f}) rotate({tang - 20:.1f}) scale(1.1)"/>')
    parts.append(f'<use href="#tendril" transform="translate({tx:.1f} {ty:.1f}) scale(-1 1) rotate({-tang - 150:.1f}) scale(0.8)"/>')
    height = round(width * 440 / 300)
    return (f'<svg class="{cls}" viewBox="0 0 300 440" width="{width}" height="{height}" aria-hidden="true" focusable="false">'
            + "".join(parts) + "</svg>")
