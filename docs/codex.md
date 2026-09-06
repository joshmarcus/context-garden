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
    models: {easy: gpt-5.6-luna, medium: gpt-5.6-terra, hard: gpt-5.6-sol}
```

### Recommended models

Recommended starting point, checked against official OpenAI documentation on 2026-09-05:

| Difficulty | Codex model | Intended work | Existing Claude default |
|---|---|---|---|
| easy | `gpt-5.6-luna` | Clear, bounded changes with explicit acceptance checks | `haiku` |
| medium | `gpt-5.6-terra` | Everyday feature implementation and debugging | `sonnet` |
| hard | `gpt-5.6-sol` | Complex changes requiring design judgment | `opus` |

Garden supplies this Codex mapping when `harness: codex` is selected without a model
map. New gardens write both providers' maps while retaining `harness: claude`.
These are starting recommendations, not measured equivalences between providers.
For exceptionally difficult tasks, consider an explicit `model: gpt-6-astra` override
if your account has access. Astra availability depends on plan and rollout; it is not
required by the recommended map. Test the mapping against your own tasks with trials.

Sources: [OpenAI's Codex model guide](https://developers.openai.com/codex/models)
and [model catalog](https://developers.openai.com/api/docs/models).

An explicit `models: {}` still opts out and uses the Codex CLI's configured model for
all tiers. Existing tier maps and task `model:` overrides remain binding: remove old
provider-specific task overrides when switching harnesses. Model availability depends
on authentication and account access; `codex login status` checks authentication, not
entitlement to every model. Check your Codex model picker before starting a large batch.

`extra_args` accepts CLI arguments such as
`['-c', 'model_reasoning_effort="medium"']`. This setting applies to all tiers; it does
not automatically vary with difficulty. Keep the same reasoning setting when comparing
models, and use arguments supported by both `exec` and `exec resume`.

### Pricing

Every codex run is costed like a claude run: `garden costs`, `garden metrics`, trials and
the retro all read a run's `cost_usd`, computed from its usage and a per-model price table,
so the two harnesses compare on one basis. The table lives at `harnesses.codex.prices` (a
generic `prices:` map at the top level works for any harness that reuses the codex-jsonl
output format under a different name); each model gives `input`, `cached_input`,
`cache_write` and `output` prices in USD per million tokens:

```yaml
harnesses:
  codex:
    prices:
      gpt-5.6-terra: {input: 2.0, cached_input: 0.2, output: 12.0}
```

Setting a model here overrides only that model; the rest of the built-in table (below)
stays in place, so a price bump needs one line, not a copy of the whole table.

Built-in defaults, from OpenAI list pricing recorded by the operator on 2026-09-05 (see
`context-garden/phase-05/specs/cost-aware-model-routing.md`'s operator notes): cached input
at 10% of the input price throughout.

| Model | input | cached input | cache write | output |
|---|---|---|---|---|
| `gpt-6-astra` | $10 | $1 | $12.50 | $50 (a long-context tier above 272K tokens in one request: $20 / $75 input/output) |
| `gpt-5.6-sol` | $4 | $0.40 | — | $20 (promotional) |
| `gpt-5.6-terra` | $2 | $0.20 | — | $12 |
| `gpt-5.6-luna` | $0.20 | $0.02 | — | $1.20 |

Both a Claude subscription and a Codex/ChatGPT subscription bill by quota, not by these list
prices — this table exists so quota consumption compares across harnesses on one basis, not
to predict a bill. **List prices change; update this table** (`harnesses.codex.prices` or
the example `garden.yaml`'s defaults) when they do — `garden doctor` does not check it. A
model with usage but no entry here records that usage with `cost_usd: null` and a log line
naming the missing price, rather than costing it at the wrong rate. `garden costs --backfill`
recomputes `cost_usd` for existing codex runs from their stored transcripts, e.g. after adding
a price or fixing one.

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

### Keeping a local operator machine responsive

Put machine-specific limits in the gitignored `garden.local.yaml`. This conservative
profile is suitable for an 8 GiB WSL instance whose `/tmp` is a 3 GiB tmpfs:

```yaml
max_parallel: 1
review_parallel: 1
resources:
  max_parallel: 2               # all local workers, reviews, personas and checks together
  min_memory_available_mb: 1536 # defer new processes below this headroom
  min_temp_free_mb: 1024        # measured on work_dir/tmp, not the system /tmp
```

All scheduler and direct CLI launches consult the same run records and limits, so
`garden dispatch` and `garden review` cannot create extra local concurrency outside the
served loop; admission is serialized across processes when the running record is created.
A pressure stop never kills or fails existing work: reaping continues, new
local launches wait, and admission resumes when a run finishes or headroom returns. The
web rail and `garden observe` show the effective local limit, the measured pressure and
the recovery action. `garden pause --reason "resource pressure"` is available when an
operator also wants to hold ordinary dispatch while the current work drains.

Per-run temporary directories live below `work_dir/tmp`, receive both `TMPDIR` and
`PYTEST_DEBUG_TEMPROOT`, and are removed only after their run record is terminal. Keep
`work_dir` on disk rather than tmpfs. A per-run subreaper owns daemonized descendants too,
so stopping a run terminates them and cleanup waits for the whole process tree. Process-level
CPU and memory ceilings remain an OS
responsibility; when running `garden serve` as a user service, use persistent systemd
settings such as `CPUQuota=200%`, `CPUWeight=20`, `MemoryHigh=3G`, `MemoryMax=4G` and
`MemorySwapMax=512M`. The garden admission gate is still required because it covers
direct CLI launches and automatic base probes that a service-only process count misses.

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

## Compare Claude and Codex on the same task

Authenticate both CLIs. Select a ready, draft, or failed task without an existing PR.
Trials create one branch and PR per contender, run a comparison judge, retain the
winner's PR and close the others. They spend tokens and publish PRs; the winning PR
still follows the garden's normal review/merge policy.

```bash
garden trial WID-003 -c claude:haiku -c codex:gpt-5.6-luna
garden trial WID-004 -c claude:sonnet -c codex:gpt-5.6-terra
garden trial WID-005 -c claude:opus -c codex:gpt-5.6-sol
garden trials
```

Use distinct eligible tasks for separate trials, or include additional contenders in
one trial. `harness:model` pins each contender independently of the garden default.
Every contender receives the same task context and its own worktree. After comparison,
the winning harness and model stay on the task so revisions use the winning setup.

The judge uses `review.harness` (otherwise the garden default) and `review.difficulty`
(otherwise hard). For example, `review: {harness: codex, difficulty: hard}` uses the
Codex hard-tier model. This also changes normal automated reviews. Try judges from both
providers across representative tasks; one judge's score is not an objective benchmark.
Scores prioritize acceptance criteria and correctness, then quality and scope, with
smaller diffs breaking ties. The leaderboard records scores, wins and reported costs.

Codex costs and dollars per score point remain unknown, displayed as a dash, rather
than zero. Compare quality and recorded tokens, but do not infer that Codex is cheaper
from absent cost data. Token counts are provider-specific and are not a dollar metric.

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
