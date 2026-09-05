# Using context-garden with Codex

The garden uses `codex exec --json` for planning, implementation, revision, automated
review, personas and model trials. The scheduler remains a Python process: GPT reads
context and does the work; the scheduler manages dependencies, worktrees and PRs.

## Setup

Install the Codex CLI and authenticate with `codex login`. Install this package with
`uv venv && uv pip install -e ".[dev]"`, then activate the environment. Git and an
authenticated `gh` CLI (or `GITHUB_TOKEN`) are required for the PR loop.

New gardens (`garden init my-garden`) default to Claude. To opt into Codex for a
new or existing garden, set:

```yaml
harness: codex
harnesses:
  codex:
    bin: codex
    permission_mode: workspace-write
    resume: true
    models: {}
```

An empty model map uses your Codex CLI's configured model. To choose models by task
difficulty, set `models.easy`, `models.medium`, and `models.hard` to model IDs available
to your account; a task's explicit `model` overrides that map. `extra_args` accepts CLI
arguments, for example `['-c', 'model_reasoning_effort="high"']`. Those arguments must
work for both `codex exec` and `codex exec resume` when resume is enabled.

The default policy is workspace writes with `approval_policy="never"`, so detached
workers cannot wait for terminal approvals. The legacy garden value `full-auto` maps
to this policy. `read-only` is also supported. `bypass` explicitly disables the sandbox
and approvals; use it only in an independently isolated execution environment.
If a required command is denied, adjust the execution environment before retrying.

`garden doctor` checks binary presence, configuration, repositories and the task graph;
also checks harness authentication. You can inspect Codex access separately with
`codex login status`.
For SSH workers, install and authenticate Codex on each host too.

The garden driving this tool now lives in [joshmarcus/garden](https://github.com/joshmarcus/garden).
To migrate that garden or another existing garden, apply the configuration above in
its own checkout. Preserve its machine overlays and any intentional dispatch pause.

## From context to reviewed code

1. Create a product and phase with `garden new-product widget --repo ../widget` and
   `garden new-phase widget phase-01`.
2. Write the principles digest, product overview (including test commands), phase
   goals, and specs. Inspect the planner input with `garden plan widget/phase-01 --dry-run`.
3. Run `garden plan widget/phase-01 --draft`. Review task acceptance criteria, reading
   lists, model difficulty and dependencies with `garden trellis` and `garden brief ID`.
4. Approve selected tasks with `garden approve ID`, then run `garden serve` or
   `garden watch` to dispatch workers. These commands spend model tokens and publish PRs.
5. Use `garden inbox` to answer questions and triage PRs. `garden answer ID "answer"`
   resumes the recorded Codex thread in the same worktree. Set `resume: false` to
   start a fresh session with the prior question and answer included in its brief.
6. Review and merge PRs; the scheduler marks tasks done and unblocks dependencies.

Codex receives the same bounded briefs and `GARDEN_RESULT`, `GARDEN_REVIEW`, and other
response markers as other harnesses. It commits locally; the runner pushes and opens
PRs. Native `codex exec review` is not used: garden's review prompt includes its own
acceptance criteria and structured verdict protocol.

## Interactive Codex sessions

The repository's `AGENTS.md` explains development and links to its separate garden. New
gardens also receive an `AGENTS.md` without overwriting existing instructions.

In the garden controller checkout, inspect `garden ready` and `garden brief ID`.
`garden take ID --worktree` claims the selected task and prints the working directory
and brief. Carry out the brief in that worktree, run the product's checks, and commit.
Return the requested result JSON. When publication is authorized, the controller runs
`garden finish ID --result '<JSON>'` from the garden checkout. Automated workers must
only emit their result; they must not run another scheduler or finish their own task.

For ChatGPT without repository tools, use `garden plan product/phase --dry-run` as the
planning prompt, save the resulting JSON array, and import it with
`garden plan product/phase --import plan.json --draft`. Review before approval.

## Usage and verification

Codex JSONL supplies input, cached-input and output token counts, plus a thread ID.
Garden stores fresh input separately from cached input so totals do not double count.
Codex does not supply a dollar cost here: a missing cost is unknown, and dollar-budget
limits cannot reliably cap Codex spending. Use account limits and task/concurrency
limits as appropriate; do not interpret a displayed zero-dollar rollup as free usage.

Offline tests use `tests/fake_codex.py`, isolated git repositories and a fake GitHub
service. Run `PYTHONPATH=src .venv/bin/python -m pytest -q` and
`.venv/bin/ruff check src tests`. These verify the protocol without paid model calls.

References: [official non-interactive mode documentation](https://developers.openai.com/codex/noninteractive)
and the installed CLI's `codex exec --help` / `codex exec resume --help` describe the
stdin prompt, JSONL events, final-message file and session continuation interfaces.
