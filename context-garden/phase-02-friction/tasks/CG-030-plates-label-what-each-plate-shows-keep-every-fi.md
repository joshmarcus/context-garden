---
id: CG-030
title: 'Plates: label what each plate shows, keep every file public domain, credit Thomé'
status: draft
product: context-garden
phase: phase-02-friction
depends_on: []
priority: 2
estimate: S
difficulty: easy
reading:
- src/garden/web/static/plates/SOURCES.md
- context-garden/phase-01-bootstrap/specs/botanical-theme.md
runner: manual
created: '2026-09-04T14:36:12+00:00'
updated: '2026-09-04T14:36:12+00:00'
---

## Goal

Make the specimen labels, the licence record and the credits agree with the plates that were actually fetched.

## Context

The twelve plates came in through PR #5 with three substitutions that the fetch code documents but the UI does not: the poppy plate is Thomé's prickly poppy (*Papaver argemone*, Tafel 260) while the label reads *Papaver rhoeas*, corn poppy; the bramble plate is *Rubus thyrsoideus* of the *R. fruticosus* aggregate, filed on Commons as "candidans"; the fern is plate 10 of the Biodiversity Heritage Library scan of the 1903 printing. `SOURCES.md` also records the foxglove file as CC BY-SA 3.0 (the cleaned derivative's own tag; the original plate is public domain) and lists Commons uploaders as artists for the cleaned files.

Everything involved lives in `PLANTS` (`src/garden/plants.py`), `CANDIDATES` and `sources_markdown` (`src/garden/platefetch.py`), and the specimen label in `src/garden/web/templates/phase.html`. Re-fetching is `garden plants --fetch`, which needs access to Wikimedia Commons, so run it on the machine the plates were fetched from (`runner: manual`).

## Acceptance criteria

- [ ] The poppy's `latin` and `common` in `PLANTS` name what the plate shows (*Papaver argemone*, prickly poppy), or the poppy is re-fetched from a Thomé plate of *P. rhoeas* if one exists; the README's "corn poppy" wording follows.
- [ ] The bramble entry reads *Rubus fruticosus* agg., and the specimen label's plate line names the plate's own species where it differs from the plant's (here Tafel 398, *R. thyrsoideus*).
- [ ] Foxglove comes from the original public-domain scan (`Illustration_Digitalis_purpurea0.jpg`) rather than the CC BY-SA derivative, or the share-alike terms are recorded and met; every row of `SOURCES.md` ends up public domain.
- [ ] `SOURCES.md` credits Thomé as the artist of every plate and names the derivative's editor separately where a cleaned file was used.
- [ ] Tests updated; `garden plants` and the phase page show the corrected names.

## Out of scope

- Re-cropping or replacing plates that already show the right species.
