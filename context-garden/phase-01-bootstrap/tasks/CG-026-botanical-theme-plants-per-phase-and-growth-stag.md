---
id: CG-026
title: "Botanical theme: plants per phase and growth-stage glyphs"
status: done
product: context-garden
phase: phase-01-bootstrap
depends_on: [CG-006]
priority: 1
estimate: M
difficulty: medium
reading:
  - context-garden/phase-01-bootstrap/specs/botanical-theme.md
created: '2026-09-04T00:00:00+00:00'
updated: '2026-09-04T00:00:00+00:00'
---

## Goal

Give every phase a plant drawn as a pressed specimen and every task state a growth-stage glyph, and set the web UI as a herbarium sheet without changing any titles or copy.

## Acceptance criteria

- [x] `plant:` in goals.md frontmatter, assigned by `garden new-phase` or by position; stripped from briefs.
- [x] Drawings as SVG symbols in `plants.py`; specimen panels in the rail and on phase pages; glyphs beside every state; the trellis as a lattice.
- [x] Light and dark palettes; Newsreader + Courier Prime with system fallbacks.

## Log

- 2026-09-04T00:00:00+00:00 shipped in the bootstrap commit
