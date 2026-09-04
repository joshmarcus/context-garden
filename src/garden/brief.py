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
- Do NOT edit files under `**/tasks/` in the context garden; task state is managed by the scheduler.
- Everything you need should be in this brief. Read the *additional files* listed under "Reading list (read these)" before you start. Beyond that, explore only the code you need to change. Do not read the whole context garden.
- Follow the principles digest. If the task conflicts with a principle or a spec, say so in your final report and take the most conservative reasonable path.
- Run the project's own fast checks (tests, lint, typecheck) before you finish. Fix what you broke.
- If you need a decision only a human can make, commit what you have, stop, and report `status: needs_input` with one precise `question`. Your session is paused, not discarded: the human's answer comes back to you and you continue from where you stopped. Do not guess on questions that change the design.
- If you discover work that should be done but is outside this task (a bug you noticed, a missing spec, a refactor the task needs but did not ask for), do NOT do it. List it under `discovered` in your result and, if you truly cannot finish without it, mark it `blocking`.
- End your final message with exactly one line of the form:

  {marker} {{"status": "done" | "needs_input" | "blocked", "summary": "<1-3 sentences>", "question": "<only for needs_input>", "pr_title": "<title>", "pr_body": "<markdown body>", "notes": "<anything the human should know>", "discovered": [{{"title": "<short>", "body": "<goal + context, markdown>", "difficulty": "easy" | "medium" | "hard", "blocking": false}}]}}

  The JSON must be on a single line. `pr_title` and `pr_body` are used verbatim for the pull request. `discovered` may be omitted or empty.
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

This branch already has an open pull request: {pr}. Reviewers left feedback (below). Address every item: make the change, or explain in the PR body why not. Do not start over; build on the existing commits. Reply to each review point in `pr_body` under a "Review responses" heading.
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
        """Fixed cost: head + rules + principles + product + goals (+ optional stack/revise)."""
        fixed_sections = {"head", "rules", "principles", "product", "goals", "stack", "revise", "qa"}
        chars = sum(v for k, v in self.sections.items() if k in fixed_sections)
        return max(1, chars // 4)

    @property
    def reading_tokens(self) -> int:
        """Reading list cost: inlined and referenced content."""
        reading_sections = {"reading", "reading_refs", "feedback"}
        chars = sum(v for k, v in self.sections.items() if k in reading_sections)
        return max(1, chars // 4)


def _read(p: Path) -> str:
    try:
        return p.read_text()
    except (OSError, UnicodeDecodeError):
        return ""


def resume_prompt(question: str, answer: str) -> str:
    return RESUME_PROMPT.format(question=question.strip(), answer=answer.strip(), marker=RESULT_MARKER)


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
        rules = OPERATING_RULES.format(
            branch=branch or task.branch or task.default_branch(),
            base=base or cfg.product_base_branch(task.product),
            marker=RESULT_MARKER,
        )
        sections.append(("rules", rules))
        if review_feedback:
            sections.append(("revise", REVISE_RULES.format(pr=task.pr or "(unknown)")))
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
        p = (root / rel).resolve()
        if not p.exists():
            missing.append(rel)
            continue
        if p.is_dir():
            files = sorted(f for f in p.rglob("*") if f.is_file() and not f.name.startswith("."))
        else:
            files = [p]
        for f in files:
            frel = store.rel(f)
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
                "## Reading list (read these)\n\nThese files are relevant but too large to inline. "
                "Read them (paths relative to the context garden root `"
                + str(root)
                + "`) before starting:\n\n"
                + "\n".join(f"- `{r}`" for r in to_read)
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


def estimate_brief_tokens(store: Store, task: Task) -> tuple[int, int]:
    """Estimate brief cost: (fixed_tokens, reading_tokens)."""
    brief = build_brief(store, task)
    return (brief.fixed_tokens, brief.reading_tokens)


def parse_result(output_text: str) -> dict:
    """Find the trailing GARDEN_RESULT line in a worker's final message."""
    import json

    for line in reversed(output_text.splitlines()):
        line = line.strip()
        if line.startswith(RESULT_MARKER):
            payload = line[len(RESULT_MARKER) :].strip()
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
