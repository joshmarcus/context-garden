# Reproduce the evolving-node history film

This is the 60-second, 3840 × 2160, 30 fps **evolving-node cut without retrospective
excerpts**. It preserves the real dependency graph, growing file nodes, bright node
updates, individual task trails, direct merge-to-file flights, red rejection/green
approval pulses, and the left-hand revision arc. No model calls or running Garden
services are needed to render it.

The bundled snapshot covers 4–10 September 2026 through **13:24:10 UTC** at product
source `6e4f3521a1e28e59c0f9283968af3de114da7e3a`: 504 task records, 406 first-parent
commits, 384 task-linked merges, and 1,476 final dependency edges. It is a historical
film, not a claim about the current deployment or phase completion.

## Render the preserved cut

Requirements: Node.js 20 or newer, npm, and FFmpeg with `libx264` for video. Python,
Git history, the context repository, and network access are unnecessary after npm
has installed the locked canvas dependency.

From this directory:

```sh
npm ci --ignore-scripts
npm test
npm run stills
npm run video
```

Outputs are ignored under `out/`:

- Nine full-resolution PNGs at useful moments in the animation.
- `context-garden-evolving-nodes.mp4`: H.264, CRF 13, yuv420p, no audio.
- `stills.json` / `video.json`: input hashes, source revision, dimensions, timing,
  actual font hashes, runtime versions, platform, and final graph counts.
- `layout.json`: the deterministic file positions and peak observed file sizes.

FFmpeg uses two encoding threads. Allow time and disk space for a full 4K render;
the original file was about 139 MB. Existing files in the chosen output directory
are regenerated. To try the complete timeline quickly:

```sh
node render.cjs video --scale 0.25 --seconds 2 --fps 12 --output out/smoke
node render.cjs stills --frames 4,49.5,58.5 --output out/stills
```

All scene timing is expressed on the original 0–60 second timeline. `--seconds`
changes the encoded duration by resampling that whole timeline. `--scale 1` is
1920 × 1080; the default `--scale 2` is native 4K. `--help` lists the options.

### Fonts and platforms

The original was rendered on Windows with Bahnschrift, Segoe UI, and Consolas.
The renderer looks in the Windows Fonts directory, the macOS Supplemental fonts
directory, or Linux's DejaVu TrueType directory. Use `--fonts DIR` or
`GARDEN_FONT_DIR` to supply another directory containing the supported filenames:

| Alias | First available filename |
| --- | --- |
| Title | `bahnschrift.ttf`, `Arial.ttf`, `DejaVuSans.ttf` |
| Body | `segoeui.ttf`, `Arial.ttf`, `DejaVuSans.ttf` |
| Mono | `consola.ttf`, `Andale Mono.ttf`, `DejaVuSansMono.ttf` |

Missing fonts fail with an actionable error. Fonts are not redistributed. Different
fonts, Skia builds, and encoders can change pixels or MP4 bytes; reproduce the
original environment for pixel matching. The input hashes and font hashes in the
receipt make those differences inspectable.

On Windows, copy this directory from a WSL checkout to a local drive before
running npm and rendering. The full product contains `aux.py`, which Git for
Windows rejects even in a sparse index. Capture with Python and Git inside WSL, then copy
the resulting snapshot into the local utility directory. Linux and macOS use
their native Python, Git, Node, and FFmpeg. There
are no hardcoded usernames, WSL distribution names, systemd services, browser
dependencies, or Codex runtime paths. If FFmpeg is outside PATH:

```sh
node render.cjs video --ffmpeg /absolute/path/to/ffmpeg
```

PowerShell accepts the same options, including quoted Windows paths to `--ffmpeg`
and `--fonts`.

## Capture a future interval

`capture.py` reads the separate **context repository** (`garden`) and the **product
repository** (`context-garden`). Use a full product clone, with the desired ref
already fetched, and an archived copy of the context's `context-garden/` directory
plus `.garden/events.jsonl`. Keep that input copy stable during capture; the event
log is normally ignored by Git. The script does not fetch, check out revisions,
import historical product code, call providers, or alter scheduler state.

Python 3.11+ and PyYAML are needed only for capture; PyYAML is already a Garden
dependency. These commands use portable native Git and Python (run inside WSL on
Windows):

```sh
python3 -m pip install PyYAML
python3 scripts/history-film/capture.py \
  --garden /path/to/context-snapshot \
  --repo /path/to/context-garden \
  --ref origin/main \
  --until 2026-09-10T13:24:10Z \
  --output /path/to/new-film-snapshot
```

Omit `--until` to use the last event. The output directory must be new. A truncated
event line, ambiguous PR ownership, or missing Git object is an error; a partial
capture without `manifest.json` cannot render. The source tip is the contiguous
first-parent prefix within the selected interval. Clock skew is clamped in that
order, with original commit timestamps retained as `rawAt`.

Then, from this directory:

```sh
node render.cjs stills --data /path/to/new-film-snapshot --output out/new-cut
node render.cjs video --data /path/to/new-film-snapshot --output out/new-cut
```

This capture is intentionally specific to Context Garden: CG task frontmatter,
its native events, `joshmarcus/context-garden` PR links, Python source under `src/`,
and Garden's template paths. It is not a generic Git visualization tool. Task
titles and metadata reflect the supplied context snapshot; historical title edits
are not reconstructed. Unlinked commits still update nodes but have no task flight.

The fresh `story.json` uses a linear history clock and no featured task selections.
For an editorial cut, edit its `knots` (film seconds, Unix milliseconds), `shots`
(`[taskId, caption, fromSecond, toSecond]`), short `intro`, and `dateLabel`. Keep
the 0/2-second opening and 57/60-second closing holds; knots must be chronological.
Subsystem groups and the eight phase positions live in `render.cjs`. Extend these
if the product's structure changes. No narrative or retrospective overlay is used.

Input checksums deliberately detect edits. After intentional story edits, update
only that checksum from this directory:

```sh
node -e "const fs=require('node:fs'),p=require('node:path'),c=require('node:crypto'),d=process.argv[1],f=p.join(d,'manifest.json'),m=JSON.parse(fs.readFileSync(f));m.sha256['story.json']=c.createHash('sha256').update(fs.readFileSync(p.join(d,'story.json'))).digest('hex');fs.writeFileSync(f,JSON.stringify(m,null,2)+'\n')" /path/to/new-film-snapshot
```

## What the animation measures

- Edges are inspectable Python AST imports and literal template references at each
  first-parent commit. External packages, dynamic imports, runtime calls, and other
  languages are omitted. Syntax failures are retained in `dependencies.json` and
  its parse-error count; inspect that count before using a new capture.
- Code-node radius is `sqrt(blobBytes / 145)`, clamped to 2.8–38 logical pixels;
  area therefore tracks file size inside that range. File-size changes interpolate
  over 0.8 film seconds. Deleted nodes shrink from their preceding displayed size.
- Cluster footprints expand with the square root of current bytes relative to
  their observed peak. The spring layout and subsystem positions are illustrative.
- Modified nodes glow and fade. Dependency edges remain subdued with no update
  pulses. Merge flights end at the actual changed files.
- Rejection returns through the left revision arc. Review verdicts are pulses;
  approval alone does not create a merge. Phase marks use recorded closures, and
  featured task phases account for recorded moves.

The bundled data contains only the fields used by the film. Review summaries are
reduced to a presence boolean, preserving the pulse behavior; raw task bodies,
goals, commit subjects, run IDs, local paths, and retrospective excerpts are absent.
Inspect task titles and filenames before publishing a capture of private work.
Keep generated MP4s and new frame sets outside Git; retain the small input snapshot
and its manifest to make a cut reproducible. The root README uses one selected PNG
poster and links to the externally hosted video.

## Validation

The packaged renderer reproduced **all nine original 4K PNGs byte for byte** on
Windows. A 24-frame, two-second H.264 smoke export traversed the whole timeline.
The fresh capture command was also exercised against the real source interval in
WSL: 504 tasks, 406 commits, 2,975 parsed versions, 1,476 final edges, zero parse
errors. Dependency versions and blob sizes are checked against the preserved input.

`npm test` checks snapshot integrity and rejects modified, mixed, or incomplete
inputs. `tests/test_history_film.py` exercises real temporary Git history, import
edges, growth/deletion, lifecycle signals, phase moves, folded YAML, and malformed
event logs. The separate `history-film` workflow runs the capture tests, input
tests, and representative still rendering on Linux, macOS, and Windows. Its result
is the platform evidence; local macOS execution was not performed.
