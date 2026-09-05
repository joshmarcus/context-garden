"""Build the worker briefing for a task.

The brief is the *only* context a worker gets by default. It is deliberately small:
principles digest + product overview + phase goals + task + inlined reading list.
Workers are told not to go exploring the garden; if something is missing, the task
(or the reading list) is what should be fixed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .model import Task, estimate_tokens, goals_text
from .store import Store

RESULT_MARKER = "GARDEN_RESULT:"

OPERATING_RULES = """\
## Operating rules

- You are working in a git worktree checked out on branch `{branch}` (based on `{base}`). Everything you change must be committed on this branch. Commit in small, well-described steps. Do NOT push and do NOT open a pull request: the garden runner does that when you finish.
{turn_cap_rule}- Do NOT edit files under `**/tasks/` in the context garden; task state is managed by the scheduler.
- Work only in the directory you were started in: it is your checkout on your branch. Do not change into any other checkout of this repository.
- Do NOT run `garden` commands: `GARDEN_ROOT` is set to a non-existent path so any `garden` invocation will refuse with a clear error.
{env_rule}- Everything you need should be in this brief. Read the *additional files* listed under "Reading list (read these)" before you start. Beyond that, explore only the code you need to change. Do not read the whole context garden.
- Follow the principles digest. If the task conflicts with a principle or a spec, say so in your final report and take the most conservative reasonable path.
- Run the project's own fast checks (tests, lint, typecheck) before you finish. Fix what you broke.
- If you need a decision only a human can make, commit what you have, stop, and report `status: needs_input` with one precise `question`. Your session is paused, not discarded: the human's answer comes back to you and you continue from where you stopped. Do not guess on questions that change the design.
- If you conclude the task should not be done at all, do not force a change you don't believe in: report `status: wont_do` with a `reason`. If this is a revision round and there is genuinely nothing to change (the code is already right, e.g. the failing check is the environment, not the diff), report `status: no_change` with a `reason`. Either way a person reads your reasoning and decides; it is not a failure.
- If you discover work that should be done but is outside this task (a bug you noticed, a missing spec, a refactor the task needs but did not ask for), do NOT do it. List it under `discovered` in your result and, if you truly cannot finish without it, mark it `blocking`.
- End your final message with exactly one line of the form:

  {marker} {{"status": "done" | "needs_input" | "blocked" | "wont_do" | "no_change", "summary": "<1-3 sentences>", "question": "<only for needs_input>", "reason": "<only for wont_do / no_change>", "pr_title": "<title>", "pr_body": "<markdown body>", "pr_comment": "<optional comment to post on the PR>", "friction": ["<short friction item>"], "notes": "<anything the human should know>", "discovered": [{{"kind": "task", "title": "<short>", "body": "<goal + context, markdown>", "difficulty": "easy" | "medium" | "hard", "blocking": false}}]}}

  The JSON must be on a single line. `pr_title` and `pr_body` are used verbatim for the pull request. `pr_comment` is posted as a comment and is optional. `discovered` may be omitted or empty; each item carries a `kind` (default `task`):

  **The `pr_body` contract.** `pr_body` is the permanent description of this change for a reader who does not have the task file: what it does, why, how it fits the phase, how you verified it, and any follow-ups. It never mentions rounds, rebases, reviews, checks, prior attempts or this run — a description is written as if the change were right the first time. Anything about the process (answering a review, resolving a rebase, explaining a decision) goes in `pr_comment`, which is posted as a PR comment, not in the description. On a revision round, omit `pr_body` unless the description itself must change; the current description stays as it is. Report friction (missing context, a confusing spec, tooling pain) as `friction` items — short strings — not in the body; the garden posts them as one PR comment and files them for the next planning round.

  - `task` — work to do; becomes a draft task file (`blocking: true` fast-tracks it). This is the shape above.
  - `duplicate` — two tasks are the same. Not work: it reaches the human as a decision. `{{"kind": "duplicate", "of": "<task-id-to-keep>", "duplicates": "<task-id-to-cancel>", "reason": "<why>"}}`. If accepted it cancels the `duplicates` task in favour of `of`.
  - `cancel` — a task you believe is now obsolete. `{{"kind": "cancel", "task": "<task-id>", "reason": "<why>"}}`. If accepted it cancels that task.
  - `note` — information for the phase's friction record, no action: `{{"kind": "note", "note": "<text>"}}`.
"""

STACK_NOTE = """\
## Stacked branch

This task depends on **{parent_id}** ({parent_title}), whose pull request ({parent_pr}) is open but not merged yet. Your branch is based on that task's branch (`{parent_branch}`), so its changes are already in your worktree; build on them and do not modify them. Your PR will target that branch and is retargeted to `{final_base}` automatically when the parent merges.
"""

RESUME_PROMPT = """\
The human answered your question.

**Your question:** {question}

**Answer:** {answer}

Continue the task from where you stopped, in the same worktree and branch. The same rules apply: commit your work, do not push, and end your final message with the `{marker}` line (status `done`, or `needs_input` again with a new question).
"""

REVISE_RULES = """\
## Revision round

This branch already has an open pull request: {pr}. Reviewers left feedback (below). Address every item: make the change, or explain why not. Do not start over; build on the existing commits. To reply to reviewers (e.g., if you decline a suggestion or explain a tradeoff), set `pr_comment` in your GARDEN_RESULT JSON; the garden will post it as a comment on the PR. Do not add review responses to the PR description (`pr_body`) — they belong in the comment thread, not in the change description.
"""

PRE_PR_REVISE_RULES = """\
## Revision round

The pre-PR check failed (below). Fix the issue, commit the change, and the garden will re-run the check. Do not start over; build on the existing commits.
"""

COMMITS_AHEAD = """\
## Already on this branch

The worktree has commits from the prior attempt:

{commits}
"""

REBASE_BRIEF = """\
# Rebase {task_id}: {title}

Branch `{branch}` has an open pull request that fell behind `{base}`, and a plain
`git rebase` hit a textual conflict. Your only job is to resolve that conflict — nothing else.

## Rules

- **Resolve the conflict, change nothing else.** Do not refactor, add features, rename, or touch
  any file the rebase did not mark as conflicted. Keep the intent of both sides.
- Run `git fetch origin && git rebase origin/{base}`. When it stops on a conflict, resolve the
  marked hunks, `git add` them, and `git rebase --continue` until the rebase completes.
- Do NOT push and do NOT open or update the PR yourself: the runner force-pushes the rebased
  branch when you finish.
- If `{base}` now already contains something this branch's PR description claims as new, drop it
  from the description and return the corrected `pr_body`; otherwise omit `pr_body`.
- If resolving the conflict needs a decision only a human can make, stop and report
  `status: needs_input` with one precise `question`.

## The task's goal (for intent only — do not implement anything new)

{goal}

## Conflicting hunks
{hunks}

End your final message with exactly one line:

  {marker} {{"status": "done", "summary": "<what you resolved>", "pr_comment": "<optional>", "pr_body": "<only if the description must change>"}}
"""


@dataclass
class Brief:
    task: Task
    text: str
    sections: dict[str, int] = field(default_factory=dict)  # section -> chars
    inlined: list[str] = field(default_factory=list)
    referenced: list[str] = field(default_factory=list)  # too big to inline; worker must read
    missing: list[str] = field(default_factory=list)

    @property
    def chars(self) -> int:
        return len(self.text)

    @property
    def tokens(self) -> int:
        return estimate_tokens(self.text)

    @property
    def fixed_tokens(self) -> int:
        """Fixed cost: head + rules + principles + product + goals (+ optional stack/revise/commits_ahead/qa)."""
        fixed_sections = {"head", "rules", "principles", "product", "goals", "stack", "revise", "commits_ahead", "qa"}
        chars = sum(v for k, v in self.sections.items() if k in fixed_sections)
        return max(1, chars // 4)

    @property
    def reading_tokens(self) -> int:
        """Reading list cost: inlined and referenced content."""
        reading_sections = {"reading", "reading_refs", "feedback"}
        chars = sum(v for k, v in self.sections.items() if k in reading_sections)
        return max(1, chars // 4)


def _env_rule(setup: dict) -> str:
    """The operating rule about the working environment: it is already prepared, so the worker
    must not install packages or make a virtualenv, and here are the exact commands to run its
    checks (from the product's `setup.test`/`setup.lint`). Nothing here names pip, uv or a venv
    unless the product's own config does. Ends with a newline so it slots between rule lines."""
    prepared = (
        "- Your working environment is already prepared: do not install packages, create a "
        "virtualenv, or run a package manager (the runner did any setup before you started)."
    )
    checks = []
    for label, key in (("tests", "test"), ("lint", "lint")):
        cmd = str((setup or {}).get(key) or "").strip()
        if cmd:
            checks.append(f"`{cmd}` ({label})")
    if checks:
        prepared += " Run the project's checks with " + " and ".join(checks) + " before you finish."
    return prepared + "\n"


def _read(p: Path) -> str:
    try:
        return p.read_text()
    except (OSError, UnicodeDecodeError):
        return ""


def resume_prompt(question: str, answer: str) -> str:
    return RESUME_PROMPT.format(question=question.strip(), answer=answer.strip(), marker=RESULT_MARKER)


def product_dirs(store: Store, task: Task) -> list[Path]:
    """Where the product's own files may be read from, in order: the task's worktree if one
    exists, then the product checkout (a local path, or the clone made under .garden/repos).
    No network: a clone that does not exist yet is simply not a candidate."""
    dirs: list[Path] = []
    wt = store.config.worktree_path(task.id)
    if wt.is_dir():
        dirs.append(wt)
    repo = store.config.product_repo(task.product)
    if isinstance(repo, Path):
        if repo.is_dir() and repo.resolve() != store.root.resolve():
            dirs.append(repo)
    else:
        name = str(repo).rstrip("/").split("/")[-1].removesuffix(".git")
        clone = store.config.repos_dir / name
        if clone.is_dir():
            dirs.append(clone)
    return dirs


def resolve_reading(store: Store, task: Task, rel: str) -> tuple[Path | None, Path | None]:
    """Find a reading-list entry: first in the garden, then in the product's checkout.
    Returns (path, base) or (None, None)."""
    for base in [store.root, *product_dirs(store, task)]:
        p = (base / rel).resolve()
        if p.exists():
            return p, base
    return None, None


def build_brief(
    store: Store,
    task: Task,
    *,
    branch: str | None = None,
    base: str | None = None,
    review_feedback: str = "",
    include_rules: bool = True,
    stack: dict | None = None,
    qa: list[dict] | None = None,
    commits_ahead: list[str] | None = None,
) -> Brief:
    cfg = store.config
    inline_max = int(cfg.get("brief.inline_max_chars", 24000))
    total_max = int(cfg.get("brief.total_max_chars", 120000))
    root = store.root
    product = store.product(task.product)
    phase = store.phase(task.product, task.phase)

    sections: list[tuple[str, str]] = []
    inlined: list[str] = []
    referenced: list[str] = []
    missing: list[str] = []

    head = f"# Task {task.id}: {task.title}\n\nProduct: **{task.product}** · Phase: **{task.phase}**\n"
    sections.append(("head", head))

    if include_rules:
        harness = cfg.harness(task.harness or cfg.product_harness(task.product))
        max_turns = harness.max_turns_for(task.difficulty or "medium")
        turn_cap_rule = (
            f"- You have **{max_turns} turns** to complete this task. Commit your work early and"
            " report your findings before you run out of turns. If you near the limit, commit"
            " what you have and finish with the final message even if incomplete.\n"
            if max_turns > 0 else ""
        )
        rules = OPERATING_RULES.format(
            branch=branch or task.branch or task.default_branch(),
            base=base or cfg.product_base_branch(task.product),
            marker=RESULT_MARKER,
            turn_cap_rule=turn_cap_rule,
            env_rule=_env_rule(cfg.product_setup(task.product)),
        )
        sections.append(("rules", rules))
        if review_feedback:
            if task.pr:
                sections.append(("revise", REVISE_RULES.format(pr=task.pr)))
            else:
                sections.append(("revise", PRE_PR_REVISE_RULES))
        if commits_ahead:
            commits_text = "\n".join(f"- {line}" for line in commits_ahead)
            sections.append(("commits_ahead", COMMITS_AHEAD.format(commits=commits_text)))
        if stack:
            sections.append(("stack", STACK_NOTE.format(**stack)))

    digest = root / str(cfg.get("principles_digest"))
    if digest.exists():
        sections.append(("principles", "## Principles (digest)\n\n" + _read(digest).strip() + "\n"))
        inlined.append(store.rel(digest))

    if product.overview_path:
        sections.append(("product", f"## Product: {product.name}\n\n" + _read(product.overview_path).strip() + "\n"))
        inlined.append(store.rel(product.overview_path))

    if phase.goals_path:
        sections.append(("goals", f"## Phase goals: {phase.name}\n\n" + goals_text(phase.goals_path) + "\n"))
        inlined.append(store.rel(phase.goals_path))

    sections.append(("task", "## Task\n\n" + task.body.strip() + "\n"))

    # Reading list: inline what fits, reference the rest.
    reading_parts: list[str] = []
    to_read: list[str] = []
    seen: set[str] = set()
    for rel in task.reading:
        if rel in seen:
            continue
        seen.add(rel)
        p, base = resolve_reading(store, task, rel)
        if p is None or base is None:
            missing.append(rel)
            to_read.append(rel)  # still named for the worker: paths are relative to its checkout
            continue
        if p.is_dir():
            files = sorted(f for f in p.rglob("*") if f.is_file() and not f.name.startswith("."))
        else:
            files = [p]
        for f in files:
            frel = str(f.resolve().relative_to(base.resolve()))
            if frel in inlined:
                continue
            content = _read(f)
            if not content or len(content) > inline_max:
                referenced.append(frel)
                to_read.append(frel)
                continue
            fence = "````" if "```" in content else "```"
            lang = f.suffix.lstrip(".") or "text"
            reading_parts.append(f"### {frel}\n\n{fence}{lang}\n{content.rstrip()}\n{fence}\n")
            inlined.append(frel)
    if reading_parts:
        sections.append(("reading", "## Reading list (inlined)\n\n" + "\n".join(reading_parts)))
    if to_read:
        sections.append(
            (
                "reading_refs",
                "## Reading list (read these)\n\nThese files are relevant but too large to inline, "
                "or were not found where the brief was built. "
                "Read them (paths relative to your current directory) before starting:\n\n"
                + "\n".join(f"- `{r}`" + (" (not found when the brief was built)" if r in missing else "") for r in to_read)
                + "\n",
            )
        )
    if review_feedback:
        sections.append(("feedback", "## Review feedback to address\n\n" + review_feedback.strip() + "\n"))
    if qa:
        lines = ["## Answers from the human\n", "Earlier runs of this task asked questions; the answers are binding.\n"]
        for i, item in enumerate(qa, 1):
            lines.append(f"{i}. **Q:** {str(item.get('q', '')).strip()}\n   **A:** {str(item.get('a', '')).strip()}\n")
        sections.append(("qa", "\n".join(lines)))

    # Enforce the total budget by trimming the largest inlined reading entries first.
    text = "\n".join(s for _, s in sections)
    if len(text) > total_max:
        trimmed: list[tuple[str, str]] = []
        for name, s in sections:
            if name == "reading":
                s = (
                    "## Reading list (read these)\n\nThe inlined reading list exceeded the brief budget; read these files instead:\n\n"
                    + "\n".join(f"- `{r}`" for r in inlined if r not in (store.rel(digest), ))
                    + "\n"
                )
            trimmed.append((name, s))
        sections = trimmed
        text = "\n".join(s for _, s in sections)

    return Brief(
        task=task,
        text=text,
        sections={n: len(s) for n, s in sections},
        inlined=inlined,
        referenced=referenced,
        missing=missing,
    )


def rebase_brief(
    store: Store,
    task: Task,
    *,
    branch: str,
    base: str,
    hunks: dict[str, str],
    files: list[str] | None = None,
) -> str:
    """A minimal brief for an agent that only resolves a rebase conflict: the task's goal for
    intent, the rule "resolve the conflict, change nothing else", and the conflicting hunks.
    Deliberately small — a rebase is not a fresh worker round and gets no reading list."""
    if hunks:
        parts = []
        for path, content in hunks.items():
            fence = "````" if "```" in content else "```"
            parts.append(f"\n### {path}\n\n{fence}\n{content.rstrip()}\n{fence}\n")
        hunks_text = "\n".join(parts)
    elif files:
        hunks_text = "\nConflicting files: " + ", ".join(f"`{f}`" for f in files) + "\n"
    else:
        hunks_text = "\n(The conflicting hunks were not captured; run the rebase to see them.)\n"
    return REBASE_BRIEF.format(
        task_id=task.id,
        title=task.title,
        branch=branch,
        base=base,
        goal=task.body.strip(),
        hunks=hunks_text,
        marker=RESULT_MARKER,
    )


def estimate_brief_tokens(store: Store, task: Task) -> tuple[int, int]:
    """Estimate brief cost: (fixed_tokens, reading_tokens)."""
    brief = build_brief(store, task)
    return (brief.fixed_tokens, brief.reading_tokens)


def phase_fixed_tokens(store: Store, tasks: list[Task]) -> int:
    """The fixed brief cost (head + rules + principles digest + product + goals), measured once
    for a phase. It is the same for every task in the phase apart from the per-task head and
    turn-cap lines, so a representative task stands in for all of them; only the reading list
    varies per task. Returns 0 for an empty phase."""
    if not tasks:
        return 0
    return build_brief(store, tasks[0], include_rules=True).fixed_tokens


def parse_result(output_text: str, marker: str = RESULT_MARKER) -> dict:
    """Find the trailing GARDEN_RESULT line (or another `marker` line, e.g. `GARDEN_QA:`)
    in a worker's final message."""
    import json

    for line in reversed(output_text.splitlines()):
        line = line.strip()
        if line.startswith(marker):
            payload = line[len(marker) :].strip()
            try:
                data = json.loads(payload)
                if isinstance(data, dict):
                    return data
            except json.JSONDecodeError:
                pass
            # tolerate a fenced or trailing-junk line by taking the outermost braces
            s, e = payload.find("{"), payload.rfind("}")
            if s != -1 and e > s:
                try:
                    data = json.loads(payload[s : e + 1])
                    if isinstance(data, dict):
                        return data
                except json.JSONDecodeError:
                    pass
    return {}
