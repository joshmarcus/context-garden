"""Discover products, phases and tasks on disk; read and write task files."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from .config import Config, find_root
from .model import Phase, Product, Task, join_frontmatter, now_iso, split_frontmatter
from .plants import PLANT_BY_KEY, positional_plant, roman

SKIP_DIRS = {".git", ".garden", ".venv", "node_modules", "src", "tests", "principles", ".claude"}


class Store:
    def __init__(self, root: Path | None = None, config: Config | None = None):
        self.root = (root or find_root()).resolve()
        self.config = config or Config.load(self.root)
        self._tasks: dict[str, Task] | None = None
        self._products: list[Product] | None = None

    # ---- discovery ---------------------------------------------------------
    def invalidate(self) -> None:
        self._tasks = None
        self._products = None

    def products(self) -> list[Product]:
        if self._products is None:
            self._products = self._scan()
        return self._products

    def _scan(self) -> list[Product]:
        out: list[Product] = []
        configured = self.config.data.get("products", {}) or {}
        for d in sorted(self.root.iterdir()):
            if not d.is_dir() or d.name.startswith(".") or d.name in SKIP_DIRS:
                continue
            if d.name not in configured and not (d / "product.md").exists():
                continue
            phases: list[Phase] = []
            for pd in sorted(d.iterdir()):
                if not pd.is_dir() or pd.name.startswith("."):
                    continue
                tasks_dir = pd / "tasks"
                goals = pd / "goals.md"
                if not tasks_dir.exists() and not goals.exists():
                    continue
                specs = sorted((pd / "specs").glob("*.md")) if (pd / "specs").exists() else []
                docs = sorted(f for f in (pd / "docs").rglob("*") if f.is_file()) if (pd / "docs").exists() else []
                tasks = [
                    self._load_task(f, d.name, pd.name)
                    for f in sorted(tasks_dir.glob("*.md"))
                    if tasks_dir.exists()
                ]
                meta: dict = {}
                if goals.exists():
                    try:
                        meta, _ = split_frontmatter(goals.read_text())
                    except (OSError, ValueError):
                        meta = {}
                phases.append(
                    Phase(
                        product=d.name,
                        name=pd.name,
                        path=pd,
                        goals_path=goals if goals.exists() else None,
                        specs=specs,
                        docs=docs,
                        tasks=tasks,
                        plant=str(meta.get("plant") or ""),
                        plate=str(meta.get("plate") or ""),
                        meta=meta,
                    )
                )
            # botanical emblems: explicit in goals.md frontmatter, else by position (skipping a
            # plant another phase has pinned, so one phase's choice never moves the others)
            taken = [ph.plant for ph in phases if ph.plant in PLANT_BY_KEY]
            for i, ph in enumerate(phases):
                if not ph.plate:
                    ph.plate = roman(i + 1)
                if ph.plant not in PLANT_BY_KEY:
                    ph.plant = positional_plant(i, taken)
                    taken.append(ph.plant)
            overview = d / "product.md"
            out.append(
                Product(
                    name=d.name,
                    path=d,
                    overview_path=overview if overview.exists() else None,
                    phases=phases,
                    config=self.config.product(d.name),
                )
            )
        return out

    def _load_task(self, path: Path, product: str, phase: str) -> Task:
        try:
            return Task.parse(path, path.read_text(), product=product, phase=phase)
        except Exception as e:
            raise ValueError(f"{self.rel(path)}: {e}") from e

    def tasks(self) -> dict[str, Task]:
        if self._tasks is None:
            tasks: dict[str, Task] = {}
            for p in self.products():
                for ph in p.phases:
                    for t in ph.tasks:
                        if t.id in tasks:
                            raise ValueError(f"duplicate task id {t.id}: {t.path} and {tasks[t.id].path}")
                        tasks[t.id] = t
            self._tasks = tasks
        return self._tasks

    def task(self, task_id: str) -> Task:
        tasks = self.tasks()
        if task_id in tasks:
            return tasks[task_id]
        # tolerate case differences and prefixes like "cg-3"
        for tid, t in tasks.items():
            if tid.lower() == task_id.lower():
                return t
        raise KeyError(f"no task {task_id!r}")

    def phase(self, product: str, phase: str) -> Phase:
        for p in self.products():
            if p.name == product:
                for ph in p.phases:
                    if ph.name == phase:
                        return ph
                raise KeyError(f"product {product!r} has no phase {phase!r}")
        raise KeyError(f"no product {product!r}")

    def product(self, name: str) -> Product:
        for p in self.products():
            if p.name == name:
                return p
        raise KeyError(f"no product {name!r}")

    # ---- writing -----------------------------------------------------------
    def save(self, task: Task) -> None:
        task.touch()
        task.path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(task.path, task.render())

    def next_id(self, product: str) -> str:
        prefix = str(self.config.product(product).get("id_prefix") or _prefix_for(product))
        n = 0
        for t in self.tasks().values():
            if t.id.upper().startswith(prefix.upper() + "-"):
                try:
                    n = max(n, int(t.id.split("-", 1)[1]))
                except ValueError:
                    pass
        return f"{prefix}-{n + 1:03d}"

    def create_task(
        self,
        product: str,
        phase: str,
        title: str,
        body: str,
        *,
        depends_on: list[str] | None = None,
        reading: list[str] | None = None,
        priority: int = 3,
        estimate: str = "",
        status: str = "draft",
        task_id: str | None = None,
        difficulty: str = "medium",
    ) -> Task:
        from .model import Status, slugify

        tid = task_id or self.next_id(product)
        ph = self.phase(product, phase)
        path = ph.path / "tasks" / f"{tid}-{slugify(title)}.md"
        t = Task(
            path=path,
            id=tid,
            title=title,
            status=Status(status),
            product=product,
            phase=phase,
            depends_on=list(depends_on or []),
            priority=priority,
            estimate=estimate,
            reading=list(reading or []),
            difficulty=difficulty,
            created=now_iso(),
            updated=now_iso(),
            body=body,
        )
        self.save(t)
        self.invalidate()
        return t

    def set_phase_closed(self, phase: Phase, closed: str) -> None:
        """Write (or, with an empty string, clear) `closed:` in the phase's goals.md frontmatter."""
        goals = phase.goals_path or (phase.path / "goals.md")
        meta: dict = {}
        body = ""
        if goals.exists():
            meta, body = split_frontmatter(goals.read_text())
        if closed:
            meta["closed"] = closed
        else:
            meta.pop("closed", None)
        _atomic_write(goals, join_frontmatter(meta, body) if meta else body)
        self.invalidate()

    def rel(self, path: Path) -> str:
        try:
            return str(path.resolve().relative_to(self.root))
        except ValueError:
            return str(path)


def _prefix_for(product: str) -> str:
    parts = [p for p in product.replace("_", "-").split("-") if p]
    if len(parts) >= 2:
        return "".join(p[0] for p in parts[:3]).upper()
    return product[:3].upper()


def _atomic_write(path: Path, text: str) -> None:
    """Write text to path without a window where a concurrent reader sees a truncated file:
    write to a temp file in the same directory, then rename (atomic on POSIX)."""
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise
