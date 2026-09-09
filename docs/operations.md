# Operating a garden

For installation and your first project, start with the [README](../README.md).

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
provisioning, model pools and an OpenRouter adapter are not implemented in this version.
Their development plans are not configuration options; SSH remains available for a host
you provision yourself, and the remote runner remains available for an independently
prepared host running `garden worker`.

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
replace the service. See [maintenance and process roles](architecture.md#the-shape-of-it).

Most configuration reloads on the next tick. `work_dir`, `tick_interval` and certain
GitHub-client/installer settings require restart. Executable changes can be held while
fenced runs are in flight; inspect Config before deliberately confirming a held reload
with `garden config accept`. Environment overlays and the exact restart rules are in
[configuration and environments](architecture.md#configuration-and-environments).
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
