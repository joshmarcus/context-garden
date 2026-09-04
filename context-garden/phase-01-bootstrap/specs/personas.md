# Persona reviews

A persona is a markdown file under `personas/<name>.md` describing who the reviewer is,
what they look for, and how they report. `garden init` writes six defaults: designer,
project-manager, staff-engineer, usability-expert, user, security. Add your own.

Two targets:

- **A phase's body of work**: `garden persona-review product/phase -p user -p security`.
  The persona runs in a detached worktree of the base branch with the principles digest,
  product overview, phase goals, specs, and every PR of the phase (title, status, link,
  description). Its report is written to `<phase>/docs/reviews/<persona>-<date>.md`, which
  the planner reads on the next `garden plan`. `--file-tasks` turns high-severity findings
  into draft tasks with `discovered_from: persona:<name>`.
- **One PR**: `garden persona-review ID -p security`. The persona runs in the task
  worktree with the task brief, PR description and diff; the report is posted as a PR
  comment. `--request-changes` routes high findings into the revise loop.
  `review.personas: [...]` in `garden.yaml` runs the listed personas on every new PR round.

Output contract: `GARDEN_PERSONA: {"persona", "score" 0-10, "overall", "findings":
[{"severity": high|medium|low, "area", "summary", "suggestion"}]}`. Persona runs use the
review harness/difficulty settings and are recorded like any run (cost, usage, events).
