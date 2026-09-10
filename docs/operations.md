# Operating a garden

For installation and your first project, start with [getting started](getting-started.md).
This page assumes a garden is configured and covers steady-state operation and recovery.

## Know what needs a person

The scheduler automatically orders dependencies, dispatches eligible tasks, collects runs,
runs configured checks and reviews, publishes PRs, polls CI, and applies bounded retries.
The person retains authority over draft scope, worker questions, PR triage, unresolved review
tradeoffs, protected changes, and merging when automerge is disabled. A delegated operator
may perform routine actions only within authority already granted by the person.

Use the Inbox as the action queue and the Trellis as the dependency explanation. `blocked`
is derived when dependencies are unmet; it is not stored as a task status.

| State | Meaning | Typical next actor |
| --- | --- | --- |
| `draft` | Proposed work is not approved. | Person approves, edits, or cancels. |
| `ready` / derived `blocked` | Approved and eligible, or waiting on dependencies/capacity. | Scheduler dispatches when gates clear. |
| `running` | A work or revision run is active. | Scheduler collects it; person acts only on a stop. |
| `waiting_human` | The worker asked a question or proposed no change/won't do. | Person answers, accepts, or rejects. |
| `awaiting_triage` | A draft PR needs the first human look. | Person marks ready or requests changes. |
| `in_review` | Review and CI gates are being evaluated. | Automation handles feedback; person resolves tradeoffs. |
| `changes_requested` | Actionable feedback is queued for revision. | Scheduler dispatches a bounded revision. |
| `merged_into_parent` | A stacked change reached its parent branch, not the product base. | Scheduler waits for the parent to reach the base. |
| `failed` | A run, closed PR, or bounded loop needs diagnosis. | Person or authorized operator fixes the cause and retries. |
| `done`, `wont_do`, `cancelled` | Terminal outcome. | No action unless deliberately reopened. |

## Phase retrospectives

A phase retrospective brings together persona reviews, friction, outcomes, and costs. It can close the phase, propose follow-ups, or identify blockers to reopen. Use those findings to revise the goals and specifications for the next phase.

Publishing retrospective documents as a PR requires the garden itself to be registered as a `self: true` product. See [the architecture guide](architecture.md#git-and-the-pull-request) for the repository and publication flow.

## Checks, merging and capacity

Workers commit in their assigned worktrees. The scheduler publishes branches and owns
PR creation and updates. Local workers may push **only when the product explicitly sets
`setup.worker_push: true`**, for example to await CI on their exact commit. Without that
permission, they leave pushing to the scheduler. The SSH runner's transport pushes its
host-side branch so the scheduler can fetch it.

`checks.pre_pr` selects local token-free checks; when no explicit list is supplied,
product `setup.test` and `setup.lint` supply the defaults. These are distinct from CI on
the PR. For this repository, the [worker CI workflow](worker-ci.md) offloads the full
suite through `scripts/check_ci.py`, with focused local validation first. Other products
can supply their own CI helper and analyser; GitHub Actions is not required by the package.
A worker's assertion that CI passed does not replace the scheduler's merge gates.

`github.automerge` defaults to false. When enabled, the queue waits for triage, review,
CI and the product-base requirements. Easy and medium tasks are eligible under the plain
policy; hard tasks require two approving rounds and a scratch-merge check by default.
Self-products default to a second opinion from a current-head persona review or human
approval; tool products default to two automated rounds. Changes to protected loop rules
are held for a person. The queue mechanically rebases when needed; a real conflict uses
an agent. See [review, stacking and merge policy](architecture.md#git-and-the-pull-request)
for the complete gates and overrides.

`max_parallel` defaults to 10 work slots; `review_parallel` defaults to the same number
for review/persona/comparison runs. It is not one combined ten-process limit.
`resources.max_parallel` adds a shared local admission bound including checks, and
optional memory/free-space thresholds defer new launches. Supported heavy local
setup/check/validation commands share `resources.heavy_test_parallel` leases (default 1).
CPU/memory containment requires a configured delegated execution cgroup; queue limits
alone do not enforce it. [Resource controls](architecture.md#dispatch-filling-the-slots)
explain the boundaries. Size capacity for your machine and model accounts.

The supported runners are **local**, **ssh** to prepared hosts, **remote** pull-based
workers that claim HTTPS leases, and **manual** for an interactive worker. [Transport
details](worker-protocol.md#variants-of-the-transport) and [an SSH/non-Python
configuration example](../examples/garden.work.yaml) cover those options. Automatic AWS
provisioning and an OpenRouter adapter are not standard product capabilities. Their
development plans are not configuration options. SSH remains available for a host you
provision yourself, and the remote runner is available for an independently prepared host
running `garden worker`. Tier and review model pools are supported independently of runner
choice; see the [README CLI overview](../README.md#use-the-cli).

Worker absence, host pressure, or a full slot pool defers dispatch rather than changing a
ready task into a failure. Inspect `garden observe`, the Now worker/queue regions, and
`garden doctor`; then check configured runner hosts and the resource limits described in
[architecture](architecture.md#dispatch-filling-the-slots). Do not raise concurrency until
memory, temporary storage, model quota, and worker availability can support it.

## Diagnose and recover

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

For red pre-PR checks or CI, inspect `garden show ID`, `garden runs ID`, the PR check at the
recorded commit, and the analyser diagnostic. Fix configuration or infrastructure before
`garden retry ID`; actionable code feedback normally enters the automatic bounded revision
loop. Review approval alone does not make a PR mergeable: triage, required current-head CI,
review policy, dependency/base requirements, and queue ownership must all be satisfied.

If the loop pauses because of a phase budget, raise or clear it with `garden budget`; an
already-running job is not cancelled at the threshold. Harness quota pauses affect that
harness while eligible alternatives in a configured pool may continue. For a deliberate
garden-wide dispatch hold use `garden pause`, and reserve maintenance pause for replacing or
restarting the controller.

`garden freeze product/phase` prevents ordinary approvals and dispatch in one phase;
`garden unfreeze product/phase` reverses it. This is distinct from `garden pause`, which
holds dispatch garden-wide, and maintenance pause, which also stops collection and polling.

Normal dispatch pause still permits collection, checks, reviews and merges. For
installation or service maintenance, use `garden maintenance-pause`, wait for
`garden maintenance-status` to report quiescence, and let its live-process blockers
drain before replacing the installation. `garden maintenance-resume` explicitly
re-enables collection and scheduling. Finished, unreaped results are durable; they need not be discarded to
replace the service. See [maintenance and process roles](architecture.md#the-shape-of-it).

Most configuration reloads on the next tick. `work_dir`, `tick_interval` and certain
GitHub-client/installer settings require restart. Executable changes can be held while
fenced runs are in flight; inspect Config before deliberately confirming a held reload
with `garden config accept`. Environment overlays and the exact restart rules are in
[configuration and environments](architecture.md#configuration-and-environments).
Precedence is package defaults, tracked `garden.yaml`, optional
`garden.<GARDEN_ENV>.yaml`, then gitignored `garden.local.yaml`; dictionaries merge while
lists and scalars replace. Product settings override the corresponding shared settings.
`garden doctor` prints loaded sources, and the Config page shows effective values.
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
stashes, not automatically included in PRs. [Worker recovery](worker-protocol.md#when-things-go-wrong)
describes the failure paths.

## Upgrade and roll back

Use `garden maintenance-pause` and wait for quiescence before replacing an installation.
`garden upgrade` advances the pinned install only to a recorded eligible tool build; an
automatic upgrade requires `upgrade: auto` and an idle tick boundary. Confirm the installed
commit in `garden status` and `/healthz`, then run `garden maintenance-resume`. A failed
replacement retains or restores the previous install where supported and reports its state.
For a deliberate downgrade, follow the immutable-version rules in the
[release protocol](release-protocol.md#rollback) and use the pinned-install maintenance
flow; there is no generic data-schema rollback, so preserve the garden and `.garden/` state
and verify version compatibility first.

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



## GitHub Enterprise

Scope a product's repository identity, API base, and token source together so its operations are independent of the host selected in a local `gh` session:

```yaml
products:
  internal-service:
    repo: git@forge.example.test:team/internal-service.git
    base_branch: release
    github:
      slug: team/internal-service
      host: forge.example.test
      api_base: https://forge.example.test/api/v3
      token_env: INTERNAL_SERVICE_GITHUB_TOKEN
```
