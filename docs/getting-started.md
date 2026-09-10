# Getting started

This walkthrough takes an existing repository from installation to one completed Garden
task. Commands run in a POSIX shell. Linux and macOS are supported directly; on Windows,
use WSL and keep both the garden and product checkout in the Linux filesystem for reliable
permissions and Git performance.

## Before you begin

You need Python 3.11 or newer, Git, a configured Git author, and one authenticated agent
harness: Claude Code or Codex. The pull-request loop also needs access to the product's
GitHub repository through authenticated `gh` or `GITHUB_TOKEN`. The product must have a
committed base branch and a remote you can push to.

On macOS, install command-line developer tools so Git and a compiler are available. On
Windows, install WSL 2 and a Linux distribution, then follow the Linux commands inside it;
native PowerShell and cmd are not supported execution environments. Paths in `garden.yaml`
may be absolute, so use paths valid inside the environment running Garden.

Install from a checkout:

```bash
git clone https://github.com/joshmarcus/context-garden
cd context-garden
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
garden --help
```

With `uv`, the equivalent environment commands are `uv venv` and `uv pip install -e .`.
Keep the environment active after moving into the garden directory. Upgrading and rolling
back an installed controller are separate maintenance operations; see the
[release protocol](release-protocol.md#rollback).

## 1. Draft a garden from the repository

Use separate directories for the tool, the context garden, and the product. Replace the
synthetic paths below with absolute paths on your machine:

```bash
garden init ../my-garden --name my-garden
# Edit ../my-garden/garden.yaml now if you want harness: codex.
garden onboard /absolute/path/to/widget --into ../my-garden
cd ../my-garden
git init
```

Onboarding reads repository metadata and documentation, attempts GitHub discovery, and
calls the configured planner. It creates a product, a first phase, draft tasks, and an
onboarding report. It does not install product dependencies or prove inferred commands.

If onboarding cannot identify the repository, confirm the path exists inside Linux/WSL and
contains a committed checkout. If harness authentication fails, run the harness's login
command in the same environment and configure credential sources before retrying. Codex
users should follow [Codex setup](codex.md).

## 2. Correct the generated context and configuration

The inferred product name is used below as `widget`. Read:

- `widget/docs/onboarding.md` for facts, inferences, and unresolved items;
- `principles/00-index.md` and `widget/product.md` for instructions every worker receives;
- `widget/phase-01/goals.md`, its specs, and draft tasks for scope and outcomes;
- `garden.yaml` for repository, harness, setup, test, lint, review, and capacity settings.

Run the configured `setup.command`, `setup.test`, and `setup.lint` yourself in a disposable
clone of the product. Fix generated commands or reading-list paths before approval. Keep
tokens and machine-specific credential paths out of tracked configuration; put local
overrides in gitignored `garden.local.yaml`.

For a cautious first run, set one work and review slot, keep generated work as drafts, and
leave merging to a person:

```yaml
max_parallel: 1
review_parallel: 1
plan: {auto_approve: false}
discovered: {auto_approve_blocking: false}
review: {enabled: true}
github: {draft_pr: true, automerge: false}
```

## 3. Validate and approve one useful task

```bash
garden doctor
garden trellis
garden validate
garden ls --product widget --phase phase-01
garden brief WID-001 --stats
garden approve WID-001
```

Replace `WID-001` with an ID printed by `garden ls`. `doctor` checks configuration,
repositories, GitHub access, and harness login; it sends a small harness prompt and may run
the notification command. `validate` is the offline graph and reading-list check. A task is
useful when its outcome, scope, acceptance criteria, dependencies, and reading list are
specific enough that you would know whether to accept its PR.

If a task remains blocked, use `garden trellis` to find unfinished dependencies. If
approval rejects it, complete the missing brief fields or fix invalid reading paths and
validate again. Approve only the first task until the workflow is proven; `garden approve
--all widget/phase-01` is available later.

## 4. Run, observe, and decide

```bash
garden serve
```

Open `http://127.0.0.1:8765`. `serve` runs the web UI and scheduler together, so a ready,
unblocked task may dispatch immediately. Use `garden serve --no-watch` when you only want
to inspect the UI. From a second terminal in the garden:

```bash
garden observe --feed quiet
garden inbox
garden show WID-001
garden runs WID-001
```

The scheduler selects ready work, creates an isolated worktree, runs the worker, executes
configured pre-PR checks, publishes a draft PR, and starts configured review. The Inbox
contains only decisions needing a person. Answer a worker question with `garden answer`,
or inspect the suggested command printed by `garden inbox`.

## 5. Review and complete the result

Inspect the diff, worker evidence, automated review, and exact-head CI. Send the draft back
with `garden triage WID-001 --changes "Add coverage for the empty result"`, or mark it ready
with `garden triage WID-001 --ready`. With automerge disabled, merge the accepted PR on
GitHub. A later scheduler poll records `done` and unblocks dependants.

Expected progress is `draft` → `ready` → `running` → `awaiting_triage` → `in_review` →
`done`, with temporary stops for questions, failed checks, or requested changes. If the
result stops, do not edit task frontmatter or `.garden/state.json`; follow the matching
Inbox action and the [operations recovery table](operations.md#diagnose-and-recover).

The scaffold, onboarding, graph validation, and approval portions of this walkthrough were
smoke-checked in disposable Linux test repositories. The live harness, GitHub review/merge,
macOS, and WSL portions require external accounts or platforms and were source-inspected but
not executed for this documentation revision.
