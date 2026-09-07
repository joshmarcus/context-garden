# context-garden

context-garden turns a repository of goals and context into reviewed changes to your
software. It is for a developer or small team who wants to specify work, make product
decisions and accept pull requests while agents handle implementation and revision.
It works with existing projects: each product supplies its own setup, test and lint
commands, whether it uses Python, JavaScript, shell or another toolchain.

Your **garden** is a git repository of markdown: shared principles, a product overview,
phase goals, specs and tasks. The product's code lives in its own repository. A Python
scheduler advances the work; local web and terminal interfaces let you inspect and
operate it. The garden driving this tool's development lives at
[joshmarcus/garden](https://github.com/joshmarcus/garden).

## From a goal to the next phase

1. **Describe the outcome.** Write the product context, phase goals and relevant specs.
2. **Plan and approve.** A model turns that context into tasks with acceptance criteria,
   reading lists, dependencies and difficulty tiers. Inspect the drafts and approve the
   work you want. Planning can also auto-approve through the same brief validation gate;
   the quickstart below explicitly keeps drafts for your inspection.
3. **Build and check.** The scheduler starts a worker in a dedicated git worktree. The
   worker implements, tests and commits its task, then reports evidence or a question.
   Configured pre-PR checks run without a model. Failed checks and actionable feedback
   feed the bounded revision loop.
4. **Review and triage.** The scheduler publishes a draft PR by default and arranges an
   automated review against the task's criteria. You mark the draft ready or send it
   back with feedback. Reviews can run while a PR is still a draft.
5. **Merge.** By default you merge on GitHub after inspecting the verdict and CI. The
   scheduler observes the merge and unblocks or restacks dependent work. Opt-in automerge
   handles eligible PRs through the garden's merge queue.
6. **Reflect.** A phase retrospective brings together persona reviews, friction, outcomes
   and costs. It can close the phase, propose follow-ups, or identify blockers to reopen.
   Publishing retro documents as a PR requires the garden itself to be registered as a
   `self: true` product; see [the architecture guide](docs/architecture.md#git-and-the-pull-request).

The **scheduler is token-free**: polling, ordering, bookkeeping and mechanical recovery
are Python. Model calls do the planning, implementation, review, conflict resolution and
retrospective work. An optional **delegated operator** is another model-powered session
that watches the loop and uses its actions under your authority. It is separate from the
scheduler, and its observations and repairs also cost tokens.

You retain decisions about scope, priorities, acceptable tradeoffs and unresolved product
questions. An operator can handle already-authorized routine recovery, triage and retries;
it should bring you a meaningful decision when your authority or intent is missing.
Repeated intervention is evidence of a product gap to record, not invisible free labor.

![Inbox](docs/screenshots/inbox-light.png)

## Install

You need Python 3.11+, git, GitHub access (authenticated `gh`, or `GITHUB_TOKEN` for the
REST fallback), and a logged-in harness CLI: Claude Code (`claude`) or Codex (`codex`).
Use a POSIX environment; on Windows, run the tool inside WSL. Configure a git author
identity and ensure the product repository has a committed base branch and a GitHub
remote you can push to.

From a checkout, install and activate the environment so `garden` remains on PATH when
you move into your garden directory:

```bash
git clone https://github.com/joshmarcus/context-garden
cd context-garden
uv venv
uv pip install -e .
source .venv/bin/activate
garden --help
```

Without uv, use `python3 -m venv .venv`, activate it, then `python -m pip install -e .`.
The tool defaults to the Claude harness; set `harness: codex` in `garden.yaml` before
planning if that is your harness. [Codex setup](docs/codex.md) explains its configuration.
Workers have a private HOME and a scrubbed environment. Saved harness credentials are
copied into private directories per dispatch; `worker_env.config_dirs` can specify their
sources. Keep secrets out of tracked YAML.

## First project and first PR

For an existing project, onboarding drafts context, setup configuration and a first phase
from repository metadata, documentation and backlog. It also attempts GitHub discovery
and calls the configured planner, so this step uses a model. To select a harness first,
initialize the destination before onboarding:

```bash
garden init ../my-garden --name my-garden
# Edit ../my-garden/garden.yaml if you need a different harness or credential sources.
garden onboard /absolute/path/to/widget --into ../my-garden
cd ../my-garden
git init
```

Use your actual repository path. Onboarding infers the product name from its manifest or
directory; the examples below assume `widget`. It creates `phase-01` with **draft** tasks
and writes `widget/docs/onboarding.md`, recording what it read, inferred and could not
determine. Read that report, `widget/product.md`, `principles/`, phase goals and tasks.
Correct the inferred commands and scope, configure missing credentials, and resolve any
overlap with existing work before approval. Onboarding does not install the product's
dependencies or prove its generated commands work.

For a project you want to describe yourself, use this alternative after `garden init`
and entering the garden:

```bash
garden new-product widget --repo /absolute/path/to/widget --base-branch main
garden new-phase widget phase-01
```

Fill in `principles/00-index.md`, `widget/product.md`,
`widget/phase-01/goals.md` and specs under `widget/phase-01/specs/`. Set the product's
`setup.command`, `setup.test` and `setup.lint` in `garden.yaml` to commands that work in a
fresh checkout. Then plan with `garden plan widget/phase-01 --draft`; it attempts a kickoff
review first when none exists. `--dry-run` prints the planning prompt without a model call.
Onboarding already creates drafts, so another planning call is optional there.

Before starting the scheduler, review configuration. This small example makes approval
explicit and keeps the first experiment's concurrency low; these are chosen settings,
not the package defaults:

```yaml
max_parallel: 1
review_parallel: 1
plan:
  auto_approve: false
discovered:
  auto_approve_blocking: false
review:
  enabled: true
github:
  draft_pr: true
  automerge: false
```

Merge these keys into the generated `garden.yaml`, retaining its `products` and harness
settings. `plan.auto_approve` and `discovered.auto_approve_blocking` both default to true;
`--draft` overrides automatic approval for that planning call. Difficulty routes work to
the selected harness's model tier, unless a task explicitly names a model.
Optionally set `garden budget widget/phase-01 50` before serving: the USD budget pauses
new phase dispatch at the threshold; it does not cancel already-running work.

```bash
garden doctor
garden trellis
garden validate
garden approve --all widget/phase-01
garden serve
```

`doctor` checks configuration, repositories, graph and logins in the worker environment.
It sends a small harness prompt and executes a configured notification command, so it is
not an offline check. Resolve its failures and validate the product's setup/test/lint
commands in a disposable checkout before approving. `validate` checks the task graph and
reading lists; approval rejects incomplete briefs. Approve individual task IDs instead
of `--all` if you only want part of the plan.

Open **http://127.0.0.1:8765**. `serve` runs both the web app and scheduler loop; a ready,
unblocked task can now dispatch. On its task page, follow the run and its evidence, then
triage the resulting draft PR. After review and CI are satisfactory, merge on GitHub
into the configured product base branch. A later poll marks it done and advances its
dependents. From another terminal in the garden, `garden status`, `garden inbox` and
`garden observe` show progress. `garden watch` runs the loop without the web app;
`garden serve --no-watch` serves the UI without automatic ticks.

## Checks, merging and capacity

Workers commit in their assigned worktrees. The scheduler publishes branches and owns
PR creation and updates. Local workers may push **only when the product explicitly sets
`setup.worker_push: true`**, for example to await CI on their exact commit. Without that
permission, they leave pushing to the scheduler. The SSH runner's transport pushes its
host-side branch so the scheduler can fetch it.

`checks.pre_pr` selects local token-free checks; when no explicit list is supplied,
product `setup.test` and `setup.lint` supply the defaults. These are distinct from CI on
the PR. For this repository, the [worker CI workflow](docs/worker-ci.md) offloads the full
suite through `scripts/check_ci.py`, with focused local validation first. Other products
can supply their own CI helper and analyser; GitHub Actions is not required by the package.
A worker's assertion that CI passed does not replace the scheduler's merge gates.

`github.automerge` defaults to false. When enabled, the queue waits for triage, review,
CI and the product-base requirements. Easy and medium tasks are eligible under the plain
policy; hard tasks require two approving rounds and a scratch-merge check by default.
Self-products default to a second opinion from a current-head persona review or human
approval; tool products default to two automated rounds. Changes to protected loop rules
are held for a person. The queue mechanically rebases when needed; a real conflict uses
an agent. See [review, stacking and merge policy](docs/architecture.md#git-and-the-pull-request)
for the complete gates and overrides.

`max_parallel` defaults to 10 work slots; `review_parallel` defaults to the same number
for review/persona/comparison runs. It is not one combined ten-process limit.
`resources.max_parallel` adds a shared local admission bound including checks, and
optional memory/free-space thresholds defer new launches. Supported heavy local
setup/check/validation commands share `resources.heavy_test_parallel` leases (default 1).
CPU/memory containment requires a configured delegated execution cgroup; queue limits
alone do not enforce it. [Resource controls](docs/architecture.md#dispatch-filling-the-slots)
explain the boundaries. Size capacity for your machine and model accounts.

The supported runners are **local**, **ssh** to prepared hosts, and **manual** for an
interactive worker. [Transport details](docs/worker-protocol.md#variants-of-the-transport)
and [an SSH/non-Python configuration example](examples/garden.work.yaml) cover those
options. HTTP claim/heartbeat workers, automatic AWS provisioning, model pools and an
OpenRouter adapter are not implemented in this version. Their development plans are not
configuration options; SSH remains available for a host you provision yourself.

## Operate and recover

Start with `garden observe --profile quiet`, then open the relevant **Inbox** card or task
page. The **Board** and **Trellis** show state and dependencies; **Runs** and **Timeline**
show evidence and history; **Costs** and **Config** show spend and effective settings.
The **Herbarium** holds closed phases. `garden tui` provides a
terminal Inbox and task list. Use a task's current state and PR status before acting:

| Situation | Next action |
| --- | --- |
| A draft needs a scope decision | `garden approve ID` or `garden cancel ID` |
| A worker asks a product question | `garden answer ID "answer"` |
| A decision card requests acceptance | Use its accept/reject/answer action; CLI details: `garden decide --help` |
| A draft PR is ready to triage | `garden triage ID --ready`, or `garden triage ID --changes "feedback"` |
| A stopped task needs recovery | Read its cause and run evidence, fix the cause, then `garden retry ID`; `garden review ID` requests another review |
| New dispatch should stop | `garden pause`; `garden unpause` resumes it |

Normal dispatch pause still permits collection, checks, reviews and merges. For
installation or service maintenance, use `garden maintenance-pause`, wait for
`garden maintenance-status` to report quiescence, and let its live-process blockers
drain before replacing the installation. `garden maintenance-resume` explicitly
re-enables collection and scheduling. Finished, unreaped results are durable; they need not be discarded to
replace the service. See [maintenance and process roles](docs/architecture.md#the-shape-of-it).

Most configuration reloads on the next tick. `work_dir`, `tick_interval` and certain
GitHub-client/installer settings require restart. Executable changes can be held while
fenced runs are in flight; inspect Config before deliberately confirming a held reload
with `garden config accept`. Environment overlays and the exact restart rules are in
[configuration and environments](docs/architecture.md#configuration-and-environments).
Use one long-running controller per garden; CLI actions can run alongside it through the
scheduler's locks. Do not edit task status or `.garden/state.json` to clear a stop.

For an overnight deployment, run `garden serve` under a service manager with the garden
as its working directory and an absolute path to the installed executable. On systemd,
`KillMode=process` preserves detached workers across a controller restart. Use the
maintenance protocol for replacement, confirm the active build and `/healthz` afterward,
and resume. Keep the UI on loopback or behind your own authenticated access layer;
`web.trusted_origins` allows a proxy origin, not user authentication. This is a local,
single-operator application, not a hosted multi-user service. After a machine reboot,
inspect recovery diagnostics; uncommitted worker files are preserved in named recovery
stashes, not automatically included in PRs. [Worker recovery](docs/worker-protocol.md#when-things-go-wrong)
describes the failure paths.

## Hand off the operator, keep the evidence

`garden init` scaffolds five Claude Code skills under `.claude/skills/`: `garden-onboard`,
`garden-plan`, `garden-take`, `garden-review` and `garden-operate`. The generated
`garden-operate/SKILL.md` is the detailed operator playbook; other interactive clients can
use the same CLI and the generated `AGENTS.md`.

A new operator session should read that playbook and the current phase's goals and
handoff notes, run `garden observe --profile quiet`, and inspect recent garden commits.
Open full transcripts only to answer a specific question. Before ending a session,
record outstanding decisions, authorized next actions and relevant run/PR references in
phase docs. Commit scheduler-owned task changes with `garden commit`. The state lives in
the garden and its run records; stopping an operator chat does not stop detached workers.

For Claude Code operator transcripts, record usage with `garden operator-spend record`; use
`garden operator-spend record --compacted --session <id>` at compaction boundaries.
See `garden operator-spend record --help` for transcript/session options. The default
ledger is the garden's `docs/operator-spend.jsonl`; its `operator` activity appears beside
worker spend in Costs. `garden metrics` reports cost per accepted task and first-pass
approval by model, tier and harness; `garden costs --by model` and `garden usage` provide
other views. Include operator spend when judging the total cost of running the loop.

## Development and further reading

[Design](docs/design.md) explains the vocabulary, [architecture](docs/architecture.md)
maps the implementation, and [worker protocol](docs/worker-protocol.md) defines the
brief/result contract. For development, install the `dev` extra and run the
[focused serial test selections](docs/test-suites.md). Before completing an authorized
worker branch, commit and run `python3 scripts/check_ci.py` in the foreground to push
that branch and await its exact-commit CI; lint with `.venv/bin/ruff check src tests scripts`.
The tests use fake harnesses and spend no model tokens. MIT licensed; see [LICENSE](LICENSE).
