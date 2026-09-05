"""Templates for `garden init`, `new-product`, `new-phase`, `new-task`."""

from __future__ import annotations

from pathlib import Path

import yaml

from .config import CONFIG_NAME
from .harness import DEFAULT_HARNESSES
from .store import Store

AGENTS_TEMPLATE = """\
# Working in this context garden

Read `principles/00-index.md`, the selected product's `product.md`, its phase's
`goals.md`, and the task's reading list. Use `garden status`, `garden ready`, and
`garden brief ID` to select and inspect work without loading the entire garden.

For planning, use `garden plan product/phase --dry-run` to inspect the planning
prompt, then `garden plan product/phase --draft` to create tasks for approval.
A human approves drafts with `garden approve`; follow the requested scope.

For an interactive implementation, `garden take ID --worktree` claims the task and
prints its brief. Work in the reported worktree, run the product's checks, commit,
and report the single-line `GARDEN_RESULT` JSON specified in the brief. The garden
controller runs `garden finish ID --result '<JSON>'` from the garden checkout to
push and open the PR when authorized. Automated workers only report their result;
the scheduler handles publication and state transitions.

Task status and logs belong to the scheduler: use garden commands to change them.
Workers must not run garden dispatch, tick, watch, serve, take, or finish from their
worktrees, or modify the controller checkout or its `.garden` state. Answer human
questions through `garden answer ID`; inspect feedback with `garden inbox`.
"""

DIGEST_TEMPLATE = """\
# Principles digest

This file is inlined into every agent brief. Keep it under ~60 lines; put the long-form
reasoning in sibling files in this directory and link to them from tasks' reading lists.

- Small, shippable slices. Every PR leaves the product working.
- Tests before "done". Run the project's own checks before reporting.
- Say what you don't know. A precise blocked-report beats a confident guess.
- Don't widen scope. Note follow-ups in the PR body instead.
"""

PRODUCT_TEMPLATE = """\
# {name}

One paragraph on what this product is, who it is for, and what "good" looks like.

## Repo

Code lives in `{repo}` (base branch `{base}`). Describe how to run tests and checks here so
every task brief carries it.

## Conventions

- ...
"""

GOALS_TEMPLATE = """\
# {phase} goals

## Why this phase

What changes for users/the team when this phase ships.

## Goals

1. ...
2. ...

## Non-goals

- ...

## Definition of done

- ...
"""

def render_task_body(goal: str = "", context: str = "", acceptance: list[str] | None = None) -> str:
    """The body `garden new-task` writes. A blank field keeps the scaffold's placeholder
    text, so a task created with nothing typed is byte-identical to `garden new-task`'s
    file; the web form's Goal/Context/Acceptance-criteria fields fill these in."""
    items = [a.strip() for a in (acceptance or []) if a.strip()]
    ac_block = "\n".join(f"- [ ] {a}" for a in items) if items else "- [ ] ..."
    return (
        "## Goal\n\n"
        f"{goal.strip() or 'One or two sentences.'}\n\n"
        "## Context\n\n"
        f"{context.strip() or 'What the agent needs to know that is not in the reading list.'}\n\n"
        "## Acceptance criteria\n\n"
        f"{ac_block}\n\n"
        "## Out of scope\n\n"
        "- ...\n"
    )


TASK_TEMPLATE = render_task_body()

TAKE_SKILL = """\
---
name: garden-take
description: Take a task from the context garden queue into this interactive session, do the work, and hand the result back so the garden pushes the branch and opens the PR. Use when the user says "take CG-012", "/garden-take <id>", or "pick up the next ready task".
---

# garden-take

You are acting as a garden worker inside an interactive Claude Code session. The garden
(`garden` CLI, config in `garden.yaml` at the repo root or a parent) owns task state; you
own the code change.

## Steps

1. Pick the task. If the user gave an id, use it. Otherwise run `garden ready` and take the
   first line (highest priority, unblocked).
2. Claim it and get the brief:

   ```bash
   garden take <ID> --worktree -q      # prints the brief path; creates .garden/worktrees/<ID>
   ```

   Read the brief file it printed. It is the complete specification: operating rules,
   principles digest, product overview, phase goals, the task, and the reading list.
   If the reading list says "read these", read those files too. Do not explore the
   context garden beyond that.
3. `cd` into the worktree path shown by `garden take` (`.garden/worktrees/<ID>`), which is
   already on the task branch. Do the work there. Commit in small steps. Do NOT push and
   do NOT open a PR.
4. Run the project's checks named in the product overview. Fix what you broke.
5. Hand back the result. Write the same JSON the brief asks for and pass it to `finish`:

   ```bash
   garden finish <ID> --result '{"status": "done", "summary": "...", "pr_title": "...", "pr_body": "...", "notes": ""}'
   ```

   `finish` pushes the branch and opens the PR (or comments on the existing one for a
   revision round) and moves the task to `in_review`. Add `"discovered": [{"title", "body",
   "difficulty", "blocking"}]` for out-of-scope work you noticed; the garden files it as
   tasks. If you need a human decision, ask the user directly (you are interactive) rather
   than reporting `needs_input`; that status is for headless workers.

## If you are already inside the product repo (no worktree)

Run `garden take <ID>` without `--worktree`, create the branch it names from the base
branch, work, push, open the PR yourself, then `garden finish <ID> --pr <url> --summary "..."`.

## Rules

- Never edit files under `**/tasks/` in the garden; the scheduler owns them.
- One task, one PR. Note follow-ups in `pr_body`, not in the diff.
- Put a `## Friction` section in `pr_body` listing anything the brief should have told you.
"""

PLAN_SKILL = """\
---
name: garden-plan
description: Plan a context-garden phase from this interactive session - turn goals and specs into draft task files without a separate headless model call. Use when the user says "plan phase-02", "/garden-plan product/phase", or "break this phase into tasks".
---

# garden-plan

`garden plan <product>/<phase>` normally makes one headless `claude -p` call. From an
interactive session you *are* the planner, so do the same work here and import the result.

## Steps

1. Get the planning prompt (it contains the rules, digest, product, goals, specs, docs and
   existing tasks):

   ```bash
   garden plan <product>/<phase> --dry-run > /tmp/garden-plan-prompt.md
   ```

   Read it in full. If the user gave extra guidance, add `--guidance "..."`.
2. Produce the JSON array the prompt asks for. Write it to `/tmp/garden-plan.json`.
   Rules that matter most:
   - 3-12 tasks, each one PR, each shippable on its own.
   - `depends_on` only for real ordering constraints; reference other tasks in the batch
     by exact title.
   - `reading` lists only the garden files the worker needs (digest/product/goals are
     automatic).
   - `difficulty` (`easy|medium|hard`) picks the model tier; be honest, it sets the cost.
   - Body has `## Goal`, `## Context`, `## Acceptance criteria` (testable checklist),
     `## Out of scope`.
3. Import:

   ```bash
   garden plan <product>/<phase> --import /tmp/garden-plan.json
   ```

   Tasks are created `ready` (dispatched on the next tick) unless `plan.auto_approve` is
   off or you pass `--draft`. Show the user `garden ls -p <product> --phase <phase>` and
   `garden graph --phase <phase>`.

## Replanning

If tasks failed or came back blocked, read their logs first (`garden show <ID>`), then
plan only the gap: smaller tasks, clearer reading lists, or a spec fix. Prefer fixing the
spec over adding a task that explains the spec.
"""

REVIEW_SKILL = """\
---
name: garden-review
description: Review a context-garden task's pull request against its brief and acceptance criteria, and post the review on GitHub so the scheduler dispatches a revision round. Use when the user says "review CG-012", "/garden-review <id>", or "check the PR for this task".
---

# garden-review

Review a garden task's PR the way a careful maintainer would, with the task brief as the
contract. The garden already ran an automated review (its comment is on the PR); build on
it rather than repeating it.

## Steps

1. Load the contract: `garden show <ID>` (status, PR url, acceptance criteria) and
   `garden brief <ID> --no-rules` (the exact context the worker had).
2. Fetch the change: `gh pr diff <number>` and `gh pr view <number> --json body,title`.
   Check the PR body's "Friction" section; anything there is a spec/task fix to suggest to
   the user, not something to block the PR on.
3. Judge, in this order:
   - Does every acceptance criterion hold? Point at the evidence (test, code) or its absence.
   - Correctness and safety of the diff itself.
   - Scope: anything outside the task? Anything the task asked for that is missing?
   - Principles digest violations (tests skipped, scope widened, history rewritten).
4. Post the review with `gh pr review <number> --comment -b "..."` (or
   `--request-changes`). Be specific: file, line, what and why. The scheduler treats every
   new non-bot comment as feedback and will dispatch a `revise` run on the next tick
   (bounded by `max_revisions`), so only post what you want acted on.
5. If the PR is good, say so to the user and stop. Do NOT merge; merging is the human's
   decision. Once merged, the scheduler marks the task `done` and unblocks dependents.
"""

OPERATE_SKILL = """\
---
name: garden-operate
description: Observe and troubleshoot a live context-garden loop as its operator. Use when the user says "watch the garden", "what's stuck", "why isn't X moving", "/garden-operate", or hands you the loop to run. Covers where the state lives, what each stall looks like, which product action clears it, and what must never be done by hand.
---

# garden-operate

You are the person's agent at the controls of a running garden: `garden serve` ticks every
minute, workers run detached in worktrees, PRs open and merge on GitHub. Your job is to
notice when the loop stops moving, name why, clear it with the loop's own actions, and file
every gap as a task so the loop needs you less next time. Written from the first live run
(2026-09-04/05), when the loop merged 83 PRs in a day and needed a hand about 100 times.

## Where the truth is

- `garden.yaml` (plus `garden.<env>.yaml`, `garden.local.yaml`): read once at start.
  **Any config change needs a restart.**
- `.garden/state.json`: per-task scheduler state (`revisions`, `review_rounds`,
  `pending_feedback`, `needs_human`, `automerge_blocked`, `last_review`), plus `_control`
  (pause) and `_phase:*`. Re-read by every tick; written at the end of every tick, so its
  mtime is the tick clock.
- `.garden/events.jsonl`: every transition, dispatch, review verdict, conflict, merge.
  `garden digest --since 2h` summarises it; the raw file answers "what happened at 01:41".
- `.garden/runs/<task>/<run>/`: `brief.md` (what the worker saw), `stdout.json` (the
  transcript, one JSON event per line), `final.md` (its last message and `GARDEN_RESULT`),
  `run.json` (status, cost, error), `setup.log`, `stderr.log`.
- Task files under `<product>/<phase>/tasks/`: status, log lines, PR link. The scheduler
  owns them; you change them only through commands and web actions.
- Worktrees under `work_dir/worktrees/<task>`; the product clone under `work_dir/repos/`.
- The web UI at `http://127.0.0.1:8765`: Inbox (cards that need a decision), Board,
  Trellis, task pages with the live log, run pages, Config (pause/resume, live overrides).

## First look, every time

```bash
garden status                      # counts, cost, "dispatch paused" if it is
garden inbox                       # decisions vs notices
gh pr list --repo <owner/repo> --state open --json number,title,mergeable,statusCheckRollup
python3 - <<'EOF'                  # active runs and whether they are alive
import json,glob,os,time
for p in glob.glob('.garden/runs/*/*/run.json'):
    r=json.load(open(p))
    if r['status']=='running':
        so=os.path.join(os.path.dirname(p),'stdout.json'); age=int(time.time()-os.path.getmtime(so)) if os.path.exists(so) else -1
        print(r['task_id'], r['mode'], r.get('model'), r['started_at'][11:19], f'last output {age}s ago')
EOF
tail -5 .garden/events.jsonl | cut -c1-200
stat -c %y .garden/state.json      # last tick; if it is minutes old, the server is dead or wedged
```

A worker writing to `stdout.json` within the last minute is working. Silence for ten
minutes with no events and no active runs means the loop is waiting on a person: read
`needs_human` for every non-terminal task before assuming a crash.

## Act through the product, not around it

Web actions are `POST /tasks/<id>/<action>` with an optional `note` field; the CLI has the
same verbs. Use them in this order of preference: web action, CLI command, and only then a
hand edit of `state.json` (never of a task's status field). Actions you will use:

| need | action |
|---|---|
| approve / cancel a discovered draft | `approve`, `cancel` (with a note naming the duplicate or the fix) |
| move a task's rank | `priority` (note = number), `difficulty` |
| one more automated review after the cap | `triage-ready` then `review` |
| clear a revision-cap stop and rebase again | `reset-revisions` then `retry` |
| accept a "nothing to change" / "won't do" card | `accept` / `reject` (note) |
| answer a worker's question | `answer` (note = your answer) |
| send a PR back with a note | `triage-changes` (note) |
| stop / start automatic dispatch | `POST /pause` (field `reason`), `POST /resume` |
| run a task now regardless of slots or pause | `dispatch` |
| commit task-file state to the garden repo | `garden commit`, then `git push` |

Before pressing anything on a task, confirm its PR is still open: an action landing seconds
after automerge moves a `done` task back into the loop (seen once; CG-142).

Before merging a PR by hand (with `automerge_hard_tier` off, hard tier does not automerge),
confirm nothing merged since its CI last ran: `gh pr view N --json mergeStateStatus` must say `CLEAN`, not `BLOCKED` or
`BEHIND`. If something did, wait for the scheduler's rebase round rather than merging a
green-but-stale branch (2026-09-05: two such merges a minute apart left main red).

## Stall patterns and what they mean

| what you see | what it is | what to do |
|---|---|---|
| `no active run found; back to ready` right after a run finished | the run was swept or its reap was interrupted; the worker's commits are in the worktree | read `final.md`; if the work is done and committed, push the branch, `triage-ready`, then `review`; if not, let it re-run (it reuses the worktree) |
| `2 automated review round(s) used; this PR is yours` | review cap (`review.max_rounds`); happens after rebases too | if the last verdict was approve or the code is fine: `triage-ready` + `review`; a description-only verdict is rewritten by the reviewer without a round |
| `revision cap reached; needs a human` on a conflict | rebase rounds counted against `max_revisions` | `reset-revisions` + `retry` |
| `worker says nothing to change` card | the failure was not the branch's (usually the base) | check main is green on a clean checkout first; if the branch is behind, rebase it, then `accept`; accepting on a stale base fails the checks again |
| pre-PR `test` failing on every branch at once | main is red, or the check environment is wrong | run the suite on a clean checkout of main; if red, `POST /pause`, dispatch the fix with `dispatch`, resume when it merges; if green, read the check command and the worktree's env |
| `CI failure` with 0 review items | a CI job failed | read `gh run view <id> --log-failed`; a known flake belongs in `checks.ci[].flaky_patterns`, a real failure belongs to the branch |
| task `in_review`, `automerge_blocked` says "feedback is pending" | stale `pending_feedback` on a task nothing dispatches from | `retry` (starts the description round) |
| `automerge_blocked` says "GitHub reports the PR unknown" | mergeability being recomputed after a merge | wait one tick |
| a PR shows `CONFLICTING` seconds after another merged | the cascade: PRs sharing a file rebase against each other | let the rebase round run; it is the cost of many PRs in one file |
| a worker asks about files in the garden repo | the task's deliverable is not in the product checkout | park it: `runner: manual` in the frontmatter, `set-status <id> ready --note`, or move it to a `self: true` product; never answer "make the fix yourself" |
| task page returns 500 | a template assumption broke on real data | read the traceback in the serve log; hot-patch the installed template (auto-reloads) and file the fix |
| check recorded as `exit -15` | the check was killed, usually by a restart mid-tick | wait for the next round; restart only right after a tick |
| a worker commit appears in the garden repo's history | a worker wrote outside its worktree | revert it, keep the diff as a patch, check the fence config; never push before reading `git log` |
| `PR merged` from `changes_requested` or `failed` | someone merged on GitHub; the poll caught it | nothing; the task is done |
| `base branch main is itself broken ... waiting for the base` on several tasks at once | main is red: two PRs that were each green alone merged within a minute of each other (a branch's CI is against the main of its last push, not the main it lands on) | `POST /pause`; run lint and the suite on a scratch checkout of `origin/main`; fix on a branch, PR, merge on green CI; `POST /resume`. Parked tasks re-probe main on the next tick |

## Restarting the server safely

Never stop the serve background task with the harness's task-stop: it kills the process
tree and the detached workers with it. Instead:

```bash
SERVE=$(pgrep -f "^$PWD/.venv/bin/python3 .venv/bin/garden serve" | head -1)   # anchored: do not match your own shell
# wait for state.json's mtime to change (the tick just saved), sleep 1.5s, then:
kill -TERM "$SERVE"
env -u CLAUDECODE .venv/bin/garden serve > serve.log 2>&1 &    # or the harness's background run
```

Then confirm `curl -s -o /dev/null -w %{http_code} http://127.0.0.1:8765/` is 200, the
first tick's events look sane, and the worker count did not drop.

## Moving the pin (the garden runs a pinned install of the tool)

**Run the canary before you move the pin.** It installs the candidate build into a throwaway
venv and drives it end to end — the scripted QA flows plus a stacked-PR and a merge-queue
scenario against an in-memory GitHub that behaves like the real one (a pushed rollup is
PENDING for a poll or two; deleting a branch closes a child PR that still targets it). Those
are the two ways the fake used to lie, and three green-tested builds still broke the live loop
within the hour on 2026-09-05.

```bash
garden canary <sha>                   # non-zero exit = do NOT move the pin
```

Only once it passes:

```bash
chmod -R u+w .venv/bin .venv/lib      # the lock is recursive
.venv/bin/pip install --force-reinstall --no-deps "context-garden[dev,plates] @ git+https://github.com/joshmarcus/context-garden@<sha>"
chmod -R a-w .venv/bin .venv/lib
grep -o '"commit_id": "[0-9a-f]\\{7\\}' .venv/lib/python3*/site-packages/context_garden-*.dist-info/direct_url.json
```

Grep the installed package for a symbol from each merged PR you expect, update the pin
line in the garden's `CLAUDE.md`, then restart as above. Do it when merges have landed that
change what the running loop does (sweep, automerge, fence, checks), not for every merge.

## Cost hygiene

- `garden metrics` per tier: revise rounds, first-pass approval, cost per task.
- Description-only review requests should cost nothing (the reviewer rewrites the body). If
  you see revise rounds for descriptions, the rewrite path is broken.
- Rebase rounds: count them (`PR conflicts with main` events per merge). Above about 0.3
  per merge, merges are cascading; hold same-file PRs or serialise merges.
- Bot review comments: a notice ("usage limit", "no issues") must not start a round.
- Watch for the same failure across many tasks in one tick; that is the environment, and
  every round spent on it is waste. Pause first, then find it.

## Filing friction

Every time the loop needed you, file the reason as a task with provenance:

```bash
garden new-task <product>/<phase> "<one-line goal>" --difficulty easy|medium|hard --priority N [--ready]
```

Then replace the template body with Goal / Context (what happened, when, the evidence) /
Acceptance criteria, and `garden commit && git push`. Under a feature freeze, leave feature
ideas as drafts with a log line "deferred by the feature freeze (date)". Judge discovered
drafts the same way: cancel duplicates and already-fixed items with a note naming why.

## Never

- Edit a task's `status:` by hand, or `state.json` while a tick may be writing it.
- Answer a worker with an instruction that sends it outside its worktree.
- Push the garden repo without reading `git log` for commits you did not make.
- Merge or mark PRs ready unless the person delegated it (they can; check).
- Stop the serve task with the harness's task-stop.
- Restart mid-tick, or reinstall the pin without restarting right after.
"""

SKILL_TEMPLATES = {
    "garden-take": TAKE_SKILL,
    "garden-plan": PLAN_SKILL,
    "garden-review": REVIEW_SKILL,
    "garden-operate": OPERATE_SKILL,
}


def init_garden(directory: Path, name: str) -> list[Path]:
    created = []
    directory.mkdir(parents=True, exist_ok=True)
    cfg = directory / CONFIG_NAME
    if not cfg.exists():
        cfg.write_text(yaml.safe_dump({
            "name": name,
            "runner": "local",
            "harness": "claude",
            "max_parallel": 10,
            "max_attempts": 2,
            "max_revisions": 3,
            "timeout_minutes": 90,
            "tick_interval": 60,
            "auto_revise": True,
            "plan": {"auto_approve": True},
            "stack": True,
            "stall": {"enabled": True},
            "budgets": {},
            "review": {"enabled": True, "max_rounds": 2, "personas": []},
            "checks": {"pre_pr": [], "ci": []},
            "github": {"draft_pr": True},
            "harnesses": {
                "claude": {"models": {"easy": "haiku", "medium": "sonnet", "hard": "opus"}},
                "codex": {"models": dict(DEFAULT_HARNESSES["codex"]["models"])},
            },
            "ssh": {"hosts": []},
            "products": {},
        }, sort_keys=False))
        created.append(cfg)
    agents = directory / "AGENTS.md"
    if not agents.exists():
        agents.write_text(AGENTS_TEMPLATE)
        created.append(agents)
    pdir = directory / "principles"
    pdir.mkdir(exist_ok=True)
    digest = pdir / "00-index.md"
    if not digest.exists():
        digest.write_text(DIGEST_TEMPLATE)
        created.append(digest)
    gi = directory / ".gitignore"
    existing = gi.read_text() if gi.exists() else ""
    missing = [line for line in (".garden/", "garden.local.yaml") if line not in existing]
    if missing:
        with gi.open("a") as f:
            f.write("".join(m + "\n" for m in missing))
        created.append(gi)
    created += write_default_skills(directory)
    return created


def write_default_skills(directory: Path) -> list[Path]:
    created = []
    for slug, text in SKILL_TEMPLATES.items():
        d = directory / ".claude" / "skills" / slug
        d.mkdir(parents=True, exist_ok=True)
        p = d / "SKILL.md"
        if not p.exists():
            p.write_text(text)
            created.append(p)
    return created


def new_product(store: Store, name: str, repo: str, base_branch: str) -> list[Path]:
    created = []
    d = store.root / name
    d.mkdir(exist_ok=True)
    overview = d / "product.md"
    if not overview.exists():
        overview.write_text(PRODUCT_TEMPLATE.format(name=name, repo=repo, base=base_branch))
        created.append(overview)
    cfg_path = store.root / CONFIG_NAME
    data = yaml.safe_load(cfg_path.read_text()) or {}
    products = data.setdefault("products", {}) or {}
    if name not in products:
        products[name] = {"repo": repo, "base_branch": base_branch}
        data["products"] = products
        cfg_path.write_text(yaml.safe_dump(data, sort_keys=False))
        created.append(cfg_path)
    return created


def new_phase(store: Store, product: str, phase: str, plant: str = "") -> list[Path]:
    from .plants import PLANT_BY_KEY, assign_plant, plant_info, roman

    configured = store.config.data.get("products", {}) or {}
    if product not in configured:
        raise ValueError(
            f"{product!r} is not registered in garden.yaml's products block; run "
            f"`garden new-product {product}` first"
        )
    if plant and plant not in PLANT_BY_KEY:
        raise ValueError(f"unknown plant {plant!r}; choose one of {', '.join(PLANT_BY_KEY)}")
    created = []
    d = store.root / product / phase
    for sub in ("specs", "tasks"):
        (d / sub).mkdir(parents=True, exist_ok=True)
        keep = d / sub / ".gitkeep"
        if not any((d / sub).iterdir()):
            keep.write_text("")
            created.append(keep)
    goals = d / "goals.md"
    if not goals.exists():
        try:
            existing = store.product(product).phases
        except KeyError:
            existing = []
        taken = [ph.plant for ph in existing if ph.name != phase]
        key = plant or assign_plant(taken)
        info = plant_info(key)
        plate = roman(len([ph for ph in existing if ph.name != phase]) + 1)
        front = f"---\nplant: {key}\nlatin: {info['latin']}\nplate: {plate}\n---\n\n"
        goals.write_text(front + GOALS_TEMPLATE.format(phase=phase))
        created.append(goals)
    return created
