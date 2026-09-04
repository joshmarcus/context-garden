# The botanical theme

Chosen direction: **the herbarium**. Pressed specimens on mount cards, typed specimen labels,
small rubber stamps for decisions; Newsreader for text, Courier Prime for ids, labels and
controls; a light palette of mount-card greys and label paper, and a separately tuned dark
palette. Titles, headers and copy are the app's plain wording; only the visual layer is
botanical, so the tool reads as professional.

## Plants per phase

Every phase carries a plant: the emblem drawn as a pressed specimen in the rail, on the
phase page (a mounted sheet with tape, a typed label of the phase's facts and an
`open`/`complete` stamp), and named in its Latin binomial with a plate number. Assignment:

- `plant:` (and optional `latin:`, `plate:`) in the YAML frontmatter of `goals.md`;
  `garden new-phase` writes it, choosing the next unused plant in the product
  (`--plant` overrides; an unknown name is an error). Phases without frontmatter get plants
  by position, skipping a plant another phase has pinned, so existing gardens need no edits
  and pinning one phase's plant never moves the others'. The frontmatter is stripped from briefs and planner prompts.
- The seed packet, in order: garden pea (*Pisum sativum*), bramble (*Rubus fruticosus*),
  foxglove (*Digitalis purpurea*), male fern (*Dryopteris filix-mas*), corn poppy
  (*Papaver rhoeas*). A product with more phases wraps around; the plate number still
  distinguishes them. `garden plants` prints the key.

## Growth stages for task states

Task states keep their names and colours; each also has a growth-stage drawing that
appears beside the state everywhere (board columns and cards, inbox rows, task and phase
pages, the trellis): draft = seed, ready = sprout, running = in leaf, waiting_human = bud
tagged with a question, awaiting_triage = in bud, in_review = in flower,
changes_requested = pruned, done = in fruit, failed = wilted, cancelled = pressed. The
stage word is in each glyph's title, never the only signal.

## Drawings

`src/garden/plants.py` holds every drawing as SVG symbols (`DEFS`), inlined once per page.
Plants and glyphs are placed with `<use>`, and colours come from CSS variables, so one
source renders as a pressed specimen in both themes and could be restyled for another
direction without redrawing. No image files are involved.

## The trellis

`graph.svg` draws the dependency graph as a lattice with the work climbing it: tasks are
stage glyphs at lattice crossings, dependencies are vine between them, tasks without
dependencies rise from the ground, and discovered work hangs from a dashed tendril.

## The background vine

Every web page carries one faint drawing behind its content: a climbing stem that enters
from the bottom right of the viewport and curves up and to the left, with leaves and
tendrils reusing the pea's symbols. `plants.vine_svg()` places them along two cubic
curves, so the drawing is generated, not stored. It is fixed to the viewport, cannot be
clicked, sits under every panel, takes its strength from `--vine-opacity` per theme, and is
hidden on narrow screens and in print. It is the only decoration that is not tied to a
phase or a state.
