# Friction log

Workers are asked (principles digest) to put a `## Friction` section in the PR body. The
garden should harvest those sections so planning sees them.

- `garden friction <product>/<phase>` fetches PR bodies for every task in the phase with a
  PR, extracts the `## Friction` section, and writes/updates
  `<product>/<phase>/docs/friction.md` grouped by task, with the PR link.
- The planner prompt includes `docs/*.md`, so friction is visible at the next planning round
  automatically.
- The web UI task detail shows the friction section when present.
