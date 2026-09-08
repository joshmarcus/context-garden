"""Build the worker briefing for a task.

The brief is the *only* context a worker gets by default. It is deliberately small:
principles digest + product overview + phase goals + task + inlined reading list.
Workers are told not to go exploring the garden; if something is missing, the task
(or the reading list) is what should be fixed.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .criteria import parse_criteria
from .host_identity import scrub_shared_text
from .model import Task, estimate_tokens, goals_text
from .preflight import preflight_section
from .store import Store

RESULT_MARKER = "GARDEN_RESULT:"
REBASE_BRIEF_MAX_BYTES = 128 * 1024
REBASE_INLINE_HUNK_MAX_BYTES = 16 * 1024

EVIDENCE_GUIDANCE = """\
## Verification evidence and implementation latitude

Judge the requested outcome and explicit constraints. The implementer may choose the
mechanism and equivalent meaningful verification; a suggested filename, helper or test
sequence is guidance unless it carries a real compatibility or correctness constraint.
A blocking finding must identify a concrete defect, failed check, contradictory source
identity or materially unverified outcome. Missing artifact metadata alone is advisory.
Never invent evidence or turn an unverified outcome into a pass.

Choose verification in proportion to the actual change. Focused tests, source inspection,
a CLI command, CI, browser interaction, or a small integration exercise can each be enough.
Report what you actually tested or inspected and the result; a clear honest attestation is
valid evidence and does not need a prescribed JSON shape, artifact manifest, HTTP journey,
state matrix, screenshot set, or load report. Reuse trustworthy existing checks instead of
rerunning them for packaging. Save any new artifacts only inside the allowed worktree or a
disposable output area; never write into the live garden.

For a material UI or workflow change, inspect the named affected behavior with a method you
judge suitable and say what you observed. Performance or load work is required only when the
task's actual outcome needs it, never because a path or keyword matched. Optional evidence
fields and presentation omissions are advisory. Explicit phase evidence holds and final
current-head CI still apply.
"""


OPERATING_RULES = """\
## Operating rules

- You are working in a git worktree checked out on branch `{branch}` (based on `{base}`). Everything you change must be committed on this branch. Commit in small, well-described steps. {push_rule}
{turn_cap_rule}- Do NOT edit files under `**/tasks/` in the context garden; task state is managed by the scheduler.
- Work only in the directory you were started in: it is your checkout on your branch. Do not change into any other checkout of this repository.
- Do NOT run `garden` commands: `GARDEN_ROOT` is set to a non-existent path so any `garden` invocation will refuse with a clear error.
{env_rule}- Everything you need should be in this brief. Read the *additional files* listed under "Reading list (read these)" before you start. Beyond that, explore only the code you need to change. Do not read the whole context garden.
- Follow the principles digest. If the task conflicts with a principle or a spec, say so in your final report and take the most conservative reasonable path.
- During iteration run focused tests only. Before finishing, run the project's checks sequentially
  (tests, lint, typecheck); full CI remains the merge gate. Fix what you broke.
- In a supervised local run, launch each potentially heavy validation as
  `"$GARDEN_VALIDATION_RUNNER" -m garden.validation -- <command>` so competing validations inside this run queue
  within its execution budget. Run ordinary lightweight inspection commands directly.
 - The run ends when you stop: run long commands in the foreground, never background a command to await a notification, and write your result only after the checks have returned.
- If you need a decision only a human can make, commit what you have, stop, and report `status: needs_input` with one precise `question`. Your session is paused, not discarded: the human's answer comes back to you and you continue from where you stopped. Do not guess on questions that change the design.
- If you conclude the task should not be done at all, do not force a change you don't believe in: report `status: wont_do` with a `reason`. If this is a revision round and there is genuinely nothing to change (the code is already right, e.g. the failing check is the environment, not the diff), report `status: no_change` with a `reason`. Either way a person reads your reasoning and decides; it is not a failure.
- If you discover work that should be done but is outside this task (a bug you noticed, a missing spec, a refactor the task needs but did not ask for), do NOT do it. List it under `discovered` in your result and, if you truly cannot finish without it, mark it `blocking`.
- Judge the task's goal and acceptance outcomes and report what you actually tested or inspected. Use `verified` when per-criterion rows help, or give a clear attestation in `summary` or `notes`. A genuinely unmet outcome is `{{"criterion": "...", "not_done": true, "reason": "<why>"}}`; never turn uncertainty into a pass. Do not write a generated verification checklist into `pr_body`.
- End your final message with exactly one line of the form:

  {marker} {{"status": "done" | "needs_input" | "blocked" | "wont_do" | "no_change", "summary": "<1-3 sentences, including what you verified when useful>", "question": "<only for needs_input>", "reason": "<only for wont_do / no_change>", "pr_title": "<title>", "pr_body": "<markdown body>", "pr_comment": "<optional comment to post on the PR>", "verified": [{{"criterion": "<acceptance criterion, quoted>", "evidence": "<test, inspection, or attestation>"}}, {{"criterion": "<another>", "not_done": true, "reason": "<why>"}}], "criteria_amended": [{{"index": 0, "text": "<replacement criterion>", "reason": "<why the original was false or missing>"}}], "improvements_taken": ["<optional review improvement taken>"], "improvements_declined": [{{"suggestion": "<optional review improvement declined>", "reason": "<why>"}}], "friction": ["<short friction item>"], "notes": "<anything the human should know>", "discovered": [{{"kind": "task", "title": "<short>", "body": "<goal + context, markdown>", "file": "<affected path>", "error": "<symptom>", "difficulty": "easy" | "medium" | "hard", "blocking": false}}]}}

  The JSON must be on a single line. `pr_title` and `pr_body` are used verbatim for the pull request. `pr_comment` is posted as a comment and is optional. `discovered` may be omitted or empty; each item carries a `kind` (default `task`):

  **The `pr_body` guidance.** Prefer a durable description of what changed and why. Description style, section choice, and omitted process cleanup are editorial advice and do not by themselves make correct source fail review. Use `pr_comment` for replies when useful. On a revision round, omit `pr_body` unless the description itself should change. Report tooling or context friction as `friction` items when useful.

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

Continue the task from where you stopped, in the same worktree and branch. The same rules apply: commit your work, follow the original brief's push/CI rules, and end your final message with the `{marker}` line (status `done`, or `needs_input` again with a new question).
"""

REVISE_RULES = """\
## Revision round

This branch already has an open pull request: {pr}. Reviewers left feedback (below). Findings and their fixes are this round's work: address every one, or explain why not. Improvements are optional: take or decline each one, and name your choices in `improvements_taken` and `improvements_declined` (with a reason for each decline). Do not start over; build on the existing commits. To reply to reviewers (e.g., if you decline a suggestion or explain a tradeoff), set `pr_comment` in your GARDEN_RESULT JSON; the garden will post it as a comment on the PR. Do not add review responses to the PR description (`pr_body`) — they belong in the comment thread, not in the change description.
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


def _push_rule(setup: dict, validation: dict | None = None,
               ci: dict[str, Any] | None = None) -> str:
    ci = ci or {}
    provider = str((validation or {}).get("provider") or "legacy")
    if ci.get("status_provider") == "worker_check":
        command = str((ci.get("worker_check") or {}).get("command") or "").strip()
        validation_command = f" Run `{command}`" if command else " Run the configured ordinary suite"
        return (
            "Do NOT push and do NOT poll GitHub Actions. The controller owns publication and "
            "external status reads." + validation_command + " through `$GARDEN_VALIDATION_RUNNER -m "
            "garden.validation -- ...`; its Garden-authored exact-head receipt is the final "
            "validation gate. Commit the intended source before that suite and make no source "
            "changes afterward; a later commit or rebase invalidates the receipt. Keep local iteration focused and exclude stress/load tests unless "
            "a separate bounded experiment explicitly opts in."
        )
    if setup.get("worker_push") is True and provider in ("legacy", "actions"):
        return (
            "You may push ONLY this assigned branch to origin for the configured CI checks, "
            "without force or changing git configuration. Run focused local checks first, "
            "commit, push and wait for CI on the exact final commit in this session. Fix "
            "failures and recheck before declaring done; missing, pending or stale CI is not "
            "a pass. Report the commit, run URL and conclusion in your acceptance evidence. "
            "Do NOT open, edit or merge pull requests: the garden runner owns them."
        )
    rule = "Do NOT push and do NOT open a pull request: the garden runner does that when you finish."
    if provider == "status":
        rule += " The scheduler awaits the configured external status provider; do not start or poll GitHub Actions."
    elif provider == "command":
        rule += " The scheduler runs the configured exact-head validation command before opening or updating the PR."
    elif provider == "none":
        rule += " This product explicitly has no external CI service; do not start or poll GitHub Actions."
    return rule


def _env_rule(setup: dict, validation: dict | None = None) -> str:
    """The operating rule about the working environment: it is already prepared, so the worker
    must not install packages or make a virtualenv, and here are the exact commands to run its
    checks (from the product's `setup.test`/`setup.lint`). Nothing here names pip, uv or a venv
    unless the product's own config does. Ends with a newline so it slots between rule lines."""
    prepared = (
        "- Your working environment is already prepared: do not install packages, create a "
        "virtualenv, or run a package manager (the runner did any setup before you started)."
    )
    from .checks import is_publishing_ci_helper
    test = str((setup or {}).get("test") or "")
    publishing_helper_needs_permission = (
        is_publishing_ci_helper(test) and (
            setup.get("worker_push") is not True
            or str((validation or {}).get("provider") or "legacy") not in ("legacy", "actions")
        )
    )
    checks = []
    for label, key in (("tests", "test"), ("lint", "lint")):
        cmd = str((setup or {}).get(key) or "").strip()
        if key == "test" and publishing_helper_needs_permission:
            continue
        if cmd:
            checks.append(f"`{cmd}` ({label})")
    if checks:
        prepared += (" During iteration run focused tests only. Before finishing, run the project's checks "
                     "sequentially with " + " and ".join(checks) + "; full CI remains the merge gate.")
    if publishing_helper_needs_permission:
        prepared += (
            " The configured test publishes a branch; do not run it until the product explicitly "
            "sets setup.worker_push: true and configures Git/GitHub credentials for this worker."
        )
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
    Refuses an absolute path and any path that resolves outside its base (a `../` escape)
    rather than following it off the garden or the product checkout — a task's reading list
    is plain text a planner or a discovered-task write, not a trusted boundary, so it must not
    be able to pull an arbitrary host file into a worker's brief (CG-239). Returns (path, base)
    or (None, None)."""
    if Path(rel).is_absolute():
        return None, None
    for base in [store.root, *product_dirs(store, task)]:
        p = (base / rel).resolve()
        if not p.is_relative_to(base.resolve()):
            continue
        if p.exists():
            return p, base
    return None, None


def _read_at_base(path: Path, repo: Path, base: str) -> str | None:
    """Read ``path`` as it was at ``base``, never from a dirty checkout.

    A reading entry can name a garden file or a product file.  Both are git
    checkouts in normal operation; a non-git fixture simply falls back to its
    on-disk file so the offline brief builder remains useful there.
    """
    try:
        rel = path.resolve().relative_to(repo.resolve()).as_posix()
    except ValueError:
        return None
    try:
        is_repo = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--is-inside-work-tree"],
            capture_output=True, text=True, check=False,
        )
        if is_repo.returncode != 0:
            return _read(path)
        shown = subprocess.run(
            ["git", "-C", str(repo), "show", f"{base}:{rel}"],
            capture_output=True, text=True, check=False,
        )
    except OSError:
        return _read(path)
    return shown.stdout if shown.returncode == 0 else None


# A criterion whose text is only one of these is a planning placeholder, not a real,
# testable acceptance criterion: the template's own `...`, filler words, or a "to be
# written at planning" promise. Matched against a single line after its list/checkbox
# marker is stripped, case-insensitively.
_PLACEHOLDER_CRITERION = re.compile(
    r"^(?:"
    r"[.\-_?x]+"  # ..., ---, ___, ???, xxx and the template's `...`
    r"|tbd|tba|todo|fixme|n/?a|none|placeholder"
    r"|to be (?:written|added|filled|filled in|specified|decided|determined|defined|planned)\b.*"
    r"|written at planning\b.*|filled (?:in )?at planning\b.*"
    r"|<[^>]*>"  # <fill me in>
    r")$",
    re.IGNORECASE,
)


def _criteria_are_placeholder(body: str) -> str:
    """Empty if `criteria.parse_criteria` finds at least one real, non-placeholder criterion in
    the task's `## Acceptance criteria` checklist; otherwise the reason it does not (no checklist,
    or every item is a placeholder). Uses the same parser as review, reap and the task page, so a
    criterion that passes this gate is not invisible to them: a non-checkbox bullet counts as no
    criteria at all, same as an empty section."""
    items = parse_criteria(body)
    if not items and not re.search(r"^#{1,6}\s+acceptance criteria\b", body, re.IGNORECASE | re.MULTILINE):
        return ""  # Criteria are optional; the Goal is the contract.
    if not items:
        return "no `## Acceptance criteria` checklist (`- [ ] ...` items)"
    for text in items:
        if not _PLACEHOLDER_CRITERION.match(text.strip()):
            return ""
    return "acceptance criteria are placeholders (fill `## Acceptance criteria` with testable items)"


def brief_gaps(store: Store, task: Task) -> list[str]:
    """What would make this task's brief cost a run without being ready to work: placeholder
    acceptance criteria, and reading-list paths that resolve to no file in the garden or the
    product checkout. Empty when the brief is complete. The one place `approve` and the Inbox
    card ask 'is this brief good enough to dispatch?'."""
    gaps: list[str] = []
    crit = _criteria_are_placeholder(task.body)
    if crit:
        gaps.append(crit)
    if not task.reading and task.extra.get("allow_empty_reading_list") is not True:
        gaps.append("reading list is empty (set `allow_empty_reading_list: true` only for a mechanical task)")
    seen: set[str] = set()
    for rel in task.reading:
        if rel in seen:
            continue
        seen.add(rel)
        p, _ = resolve_reading(store, task, rel)
        if p is None:
            gaps.append(f"reading-list path not found: `{rel}`")
    return gaps


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
    validation_plan: dict[str, Any] | None = None,
    criteria_snapshot: list[str] | None = None,
    generated_context: Path | None = None,
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
            env_rule=_env_rule(cfg.product_setup(task.product), cfg.product_validation(task.product)),
            push_rule=_push_rule(cfg.product_setup(task.product), cfg.product_validation(task.product),
                                 dict(cfg.get("ci", {}) or {})),
        )
        sections.append(("rules", rules + "\n" + EVIDENCE_GUIDANCE))
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
    if generated_context is not None:
        sections.append((
            "generated_context",
            "## Generated design context\n\n"
            "The scheduler saved sanitized, read-only operational context for this run at "
            f"`{generated_context}`. Read it when it helps with this task. Do not copy it into "
            "the product checkout or commit it.\n",
        ))
    if not parse_criteria(task.body):
        sections.append(("criteria_contract", "## Criteria contract\n\nThis task has no acceptance-criteria checklist. Its Goal is the contract; state what you verified and how in `verified`.\n"))
    frozen = criteria_snapshot if criteria_snapshot is not None else parse_criteria(task.body)
    if frozen:
        sections.append(("criteria", "## Criteria frozen for this dispatch\n\n" +
                         "\n".join(f"- {item}" for item in frozen) + "\n"))
    if include_rules:
        sections.append(("pre_flight", preflight_section(cfg.capture_infrastructure_policy())))

    # Reading list: inline what fits, reference the rest.
    reading_parts: list[str] = []
    to_read: list[str] = []
    seen: set[str] = set()
    for rel in task.reading:
        if rel in seen:
            continue
        seen.add(rel)
        p, source_root = resolve_reading(store, task, rel)
        if p is None or source_root is None:
            missing.append(rel)
            continue
        if p.is_dir():
            files = sorted(f for f in p.rglob("*") if f.is_file() and not f.name.startswith("."))
        else:
            files = [p]
        for f in files:
            frel = str(f.resolve().relative_to(source_root.resolve()))
            if frel in inlined:
                continue
            content = _read_at_base(f, source_root, base or "HEAD")
            if content is None:
                missing.append(frel)
                continue
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
                "## Reading list (read these)\n\nThese files are relevant but too large to inline. "
                "Read them (paths relative to your current directory) before starting:\n\n"
                + "\n".join(f"- `{r}`" for r in to_read)
                + "\n",
            )
        )
    if missing:
        sections.append(("gaps", "## Brief gaps\n\nThe following reading-list entries were dropped because they did not resolve:\n\n"
                         + "\n".join(f"- `{path}`" for path in missing) + "\n"))
    if review_feedback:
        sections.append(("feedback", "## Review feedback to address\n\n" + review_feedback.strip() + "\n"))
    if validation_plan is not None:
        sections.append(("validation_plan", "## Validation plan\n\nThis frozen, head-bound plan is shared with the pre-check and reviewer. Keep its acceptance claims and valid current-head evidence; report any newly discovered demand as a justified scope expansion.\n\n```json\n" + json.dumps(validation_plan, indent=2, sort_keys=True) + "\n```\n"))
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
        text=scrub_shared_text(text, cfg.data),
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
    artifacts: dict[str, dict[str, object]] | None = None,
) -> str:
    """A minimal brief for an agent that only resolves a rebase conflict: the task's goal for
    intent, the rule "resolve the conflict, change nothing else", and the conflicting hunks.
    Deliberately small — a rebase is not a fresh worker round and gets no reading list."""
    artifacts = artifacts or {}
    if hunks:
        parts: list[str] = []
        goal = _truncate_utf8(task.body.strip(), REBASE_BRIEF_MAX_BYTES // 4)
        for path, content in hunks.items():
            artifact_path = _conflict_artifact_locations(artifacts.get(path, {}))
            content_bytes = len(content.encode("utf-8", "replace"))
            if content_bytes > REBASE_INLINE_HUNK_MAX_BYTES:
                parts.append(_rebase_hunk_summary(path, content_bytes, artifact_path))
                continue
            fence = "````" if "```" in content else "```"
            candidate = f"\n### {path}\n\n{fence}\n{content.rstrip()}\n{fence}\n"
            rendered = REBASE_BRIEF.format(task_id=task.id, title=task.title, branch=branch,
                                           base=base, goal=goal, hunks="\n".join([*parts, candidate]),
                                           marker=RESULT_MARKER)
            if len(rendered.encode("utf-8", "replace")) > REBASE_BRIEF_MAX_BYTES:
                parts.append(_rebase_hunk_summary(path, content_bytes, artifact_path))
            else:
                parts.append(candidate)
        hunks_text = "\n".join(parts)
    elif files:
        parts = []
        for path in files:
            artifact_path = _conflict_artifact_locations(artifacts.get(path, {}))
            if artifact_path:
                parts.append(_rebase_hunk_summary(path, 0, artifact_path))
            else:
                parts.append(f"\n### {path}\n\nConflict hunk unavailable; re-run the rebase to inspect it.\n")
        hunks_text = "".join(parts)
    else:
        hunks_text = "\n(The conflicting hunks were not captured; run the rebase to see them.)\n"
    return REBASE_BRIEF.format(
        task_id=task.id,
        title=task.title,
        branch=branch,
        base=base,
        goal=_truncate_utf8(task.body.strip(), REBASE_BRIEF_MAX_BYTES // 4),
        hunks=hunks_text,
        marker=RESULT_MARKER,
    )


def _truncate_utf8(text: str, max_bytes: int) -> str:
    encoded = text.encode("utf-8", "replace")
    if len(encoded) <= max_bytes:
        return text
    marker = "\n[truncated for the bounded rebase prompt]\n"
    return encoded[:max_bytes - len(marker.encode())].decode("utf-8", "ignore") + marker


def _conflict_artifact_locations(artifact: dict[str, object]) -> str:
    stages = artifact.get("stages")
    if not isinstance(stages, list):
        return ""
    paths = [str(stage.get("path")) for stage in stages if isinstance(stage, dict) and stage.get("path")]
    return ", ".join(paths)


def _rebase_hunk_summary(path: str, size: int, artifact_path: str) -> str:
    location = f" Preserved Git-stage artifacts: `{artifact_path}`." if artifact_path else ""
    return (f"\n### {path}\n\nLarge conflict omitted from this prompt ({size:,} bytes)."
            f" Re-run the rebase to inspect Git's conflict stages.{location}\n")


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
    """Find the trailing marker object in a worker's final message."""
    return _parse_marked_json(output_text, marker)


def _parse_marked_json(text: str, marker: str) -> dict:
    """Parse a marker at the start of a line, allowing markdown decoration and wrapping.

    The object may continue on following lines. Leading emphasis/code characters are
    accepted only before the marker, so prose that mentions a marker is not interpreted.
    """
    lines = text.splitlines()
    for index in range(len(lines) - 1, -1, -1):
        line = lines[index].strip()
        normalized = line.lstrip("*_`").strip()
        if not normalized.startswith(marker):
            continue
        payload = normalized[len(marker):].lstrip("*_`").strip()
        if index + 1 < len(lines):
            payload += "\n" + "\n".join(lines[index + 1:])
        start = payload.find("{")
        if start == -1:
            continue
        try:
            data, _ = json.JSONDecoder().raw_decode(payload[start:])
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
    return {}
