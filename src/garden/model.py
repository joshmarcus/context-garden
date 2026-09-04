"""Data model: a garden is a tree of markdown files.

    <root>/garden.yaml                 repo config
    <root>/principles/*.md             cross-cutting principles (00-index.md is the digest)
    <root>/<product>/product.md        product overview
    <root>/<product>/<phase>/goals.md  phase goals
    <root>/<product>/<phase>/specs/    specs and supporting docs
    <root>/<product>/<phase>/tasks/    one markdown file per task, YAML frontmatter

Git is the database. Nothing here talks to the network.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

import yaml

FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.DOTALL)


class Status(str, Enum):
    """Stored task status. `blocked` is derived (deps unmet), never stored."""

    DRAFT = "draft"  # planner output; needs human approval
    READY = "ready"  # approved; dispatch when deps are done and a slot is free
    RUNNING = "running"  # a worker is on it
    AWAITING_TRIAGE = "awaiting_triage"  # draft PR open; a human's first look decides if it is ready for review
    IN_REVIEW = "in_review"  # PR marked ready, waiting on human review / CI
    CHANGES_REQUESTED = "changes_requested"  # review feedback waiting for a revise run
    WAITING_HUMAN = "waiting_human"  # worker asked a question, or reported wont_do / no_change; resumes when the person decides
    DONE = "done"  # PR merged (or manually closed out)
    FAILED = "failed"  # worker failed / PR closed unmerged / needs a human
    WONT_DO = "wont_do"  # a person accepted a worker's call that the task should not be done; terminal, neither done nor failed
    CANCELLED = "cancelled"

    @property
    def terminal(self) -> bool:
        return self in (Status.DONE, Status.CANCELLED, Status.WONT_DO)

    @property
    def active(self) -> bool:
        return self in (Status.RUNNING, Status.AWAITING_TRIAGE, Status.IN_REVIEW, Status.CHANGES_REQUESTED, Status.WAITING_HUMAN)

    @property
    def has_branch(self) -> bool:
        """The task's branch is pushed and a PR is open (stackable)."""
        return self in (Status.AWAITING_TRIAGE, Status.IN_REVIEW, Status.CHANGES_REQUESTED)

    @property
    def pr_open(self) -> bool:
        return self in (Status.AWAITING_TRIAGE, Status.IN_REVIEW, Status.CHANGES_REQUESTED)


STATUS_ORDER = [s.value for s in Status]


def now_iso() -> str:
    return dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat()


def slugify(text: str, max_len: int = 48) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:max_len].rstrip("-") or "task"


def split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    m = FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    data = yaml.safe_load(m.group(1)) or {}
    if not isinstance(data, dict):
        raise ValueError("frontmatter must be a mapping")
    return data, text[m.end() :]


def join_frontmatter(data: dict[str, Any], body: str) -> str:
    fm = yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=100).rstrip("\n")
    body = body.lstrip("\n")
    return f"---\n{fm}\n---\n\n{body.rstrip()}\n"


@dataclass
class Task:
    path: Path
    id: str
    title: str
    status: Status = Status.DRAFT
    product: str = ""
    phase: str = ""
    depends_on: list[str] = field(default_factory=list)
    priority: int = 3  # 1 = highest
    estimate: str = ""  # S / M / L, informational
    reading: list[str] = field(default_factory=list)  # garden-relative paths inlined in the brief
    repo: str = ""  # override product repo (rare)
    branch: str = ""
    pr: str = ""
    runner: str = ""  # override product/default runner
    harness: str = ""  # override harness (claude | codex | ...)
    difficulty: str = "medium"  # easy | medium | hard -> picks the model tier
    model: str = ""  # explicit model override
    discovered_from: str = ""  # task id that reported this one as discovered work
    attempts: int = 0
    last_dispatched_at: str = ""
    created: str = ""
    updated: str = ""
    extra: dict[str, Any] = field(default_factory=dict)
    body: str = ""

    # ---- (de)serialisation -------------------------------------------------
    KNOWN = (
        "id",
        "title",
        "status",
        "product",
        "phase",
        "depends_on",
        "priority",
        "estimate",
        "reading",
        "repo",
        "branch",
        "pr",
        "runner",
        "harness",
        "difficulty",
        "model",
        "discovered_from",
        "attempts",
        "last_dispatched_at",
        "created",
        "updated",
    )

    @classmethod
    def parse(cls, path: Path, text: str, product: str = "", phase: str = "") -> Task:
        data, body = split_frontmatter(text)
        if "id" not in data:
            raise ValueError(f"{path}: task has no id")
        status_raw = str(data.get("status", "draft"))
        try:
            status = Status(status_raw)
        except ValueError as e:
            raise ValueError(f"{path}: unknown status {status_raw!r}") from e
        extra = {k: v for k, v in data.items() if k not in cls.KNOWN}
        return cls(
            path=path,
            id=str(data["id"]),
            title=str(data.get("title", "")),
            status=status,
            product=str(data.get("product") or product),
            phase=str(data.get("phase") or phase),
            depends_on=[str(d) for d in (data.get("depends_on") or [])],
            priority=int(data.get("priority", 3)),
            estimate=str(data.get("estimate") or ""),
            reading=[str(r) for r in (data.get("reading") or [])],
            repo=str(data.get("repo") or ""),
            branch=str(data.get("branch") or ""),
            pr=str(data.get("pr") or ""),
            runner=str(data.get("runner") or ""),
            harness=str(data.get("harness") or ""),
            difficulty=str(data.get("difficulty") or "medium"),
            model=str(data.get("model") or ""),
            discovered_from=str(data.get("discovered_from") or ""),
            attempts=int(data.get("attempts", 0) or 0),
            last_dispatched_at=str(data.get("last_dispatched_at") or ""),
            created=str(data.get("created") or ""),
            updated=str(data.get("updated") or ""),
            extra=extra,
            body=body,
        )

    def to_frontmatter(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "id": self.id,
            "title": self.title,
            "status": self.status.value,
            "product": self.product,
            "phase": self.phase,
            "depends_on": list(self.depends_on),
            "priority": self.priority,
        }
        if self.estimate:
            data["estimate"] = self.estimate
        data["difficulty"] = self.difficulty
        data["reading"] = list(self.reading)
        for k in ("repo", "branch", "pr", "runner", "harness", "model", "discovered_from"):
            v = getattr(self, k)
            if v:
                data[k] = v
        if self.attempts:
            data["attempts"] = self.attempts
        if self.last_dispatched_at:
            data["last_dispatched_at"] = self.last_dispatched_at
        data["created"] = self.created
        data["updated"] = self.updated
        data.update(self.extra)
        return data

    def render(self) -> str:
        return join_frontmatter(self.to_frontmatter(), self.body)

    # ---- helpers -----------------------------------------------------------
    @property
    def key(self) -> str:
        return f"{self.product}/{self.phase}"

    @property
    def slug(self) -> str:
        return slugify(self.title)

    def default_branch(self) -> str:
        return f"garden/{self.id.lower()}-{self.slug}"

    def log(self, message: str) -> None:
        """Append a timestamped line to the task's `## Log` section (created on demand)."""
        stamp = now_iso()
        line = f"- {stamp} {message}"
        if re.search(r"^## Log\s*$", self.body, re.MULTILINE):
            self.body = self.body.rstrip("\n") + "\n" + line + "\n"
        else:
            self.body = self.body.rstrip("\n") + "\n\n## Log\n\n" + line + "\n"
        self.updated = stamp

    def touch(self) -> None:
        self.updated = now_iso()


@dataclass
class Phase:
    product: str
    name: str
    path: Path
    goals_path: Path | None
    specs: list[Path]
    docs: list[Path]
    tasks: list[Task]
    plant: str = ""  # botanical emblem (see plants.py); from goals.md frontmatter or assigned by position
    plate: str = ""  # roman plate number within the product
    meta: dict[str, Any] = field(default_factory=dict)  # goals.md frontmatter

    @property
    def key(self) -> str:
        return f"{self.product}/{self.name}"

    @property
    def latin(self) -> str:
        from .plants import plant_info

        return str(self.meta.get("latin") or plant_info(self.plant)["latin"])

    @property
    def common(self) -> str:
        from .plants import plant_info

        return str(self.meta.get("common") or plant_info(self.plant)["common"])

    @property
    def closed(self) -> str:
        """Close date from `closed:` in goals.md frontmatter; empty while the phase is open."""
        v = self.meta.get("closed")
        return str(v) if v else ""


def goals_text(path: Path | None) -> str:
    """goals.md without its frontmatter (the frontmatter carries the plant assignment)."""
    if not path or not path.exists():
        return ""
    try:
        _, body = split_frontmatter(path.read_text())
    except (OSError, ValueError):
        return ""
    return body.strip()


@dataclass
class Product:
    name: str
    path: Path
    overview_path: Path | None
    phases: list[Phase]
    config: dict[str, Any] = field(default_factory=dict)  # from garden.yaml products.<name>


def estimate_tokens(text: str) -> int:
    """Cheap, provider-agnostic estimate (~4 chars/token for English + code)."""
    return max(1, len(text) // 4)
