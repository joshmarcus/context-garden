"""Discover products, phases and tasks on disk; read and write task files."""

from __future__ import annotations

import fcntl
import json
import os
import re
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .config import Config, find_root
from .model import Phase, Product, Task, join_frontmatter, now_iso, split_frontmatter
from .plants import PLANT_BY_KEY, PLANTS, roman

SKIP_DIRS = {".git", ".garden", ".venv", "node_modules", "src", "tests", "principles", ".claude"}


class Store:
    def __init__(self, root: Path | None = None, config: Config | None = None):
        self.root = (root or find_root()).resolve()
        self.config = config or Config.load(self.root)
        self._tasks: dict[str, Task] | None = None
        self._products: list[Product] | None = None
        # Ids claimed by more than one task file, found on the last scan: quarantined out of
        # `_tasks` (they are ambiguous, so they cannot dispatch) and surfaced by `duplicate_ids`.
        self._duplicate_ids: dict[str, list[Path]] = {}
        self._config_sig = self._config_signature()
        # keys whose value changed in the most recent reload, so the scheduler can log them once.
        # Set only when a reload actually happens (never cleared by a no-op invalidate), so a
        # reload triggered by an action's invalidate still gets logged by the next tick that
        # consumes it. The scheduler clears it after logging.
        self.last_config_change: dict[str, tuple[object, object]] = {}

    # ---- discovery ---------------------------------------------------------
    def invalidate(self) -> None:
        self.reload_config_if_changed()
        self.invalidate_tasks()

    def invalidate_tasks(self) -> None:
        """Drop the cached task/product scan, without touching garden.yaml — for a caller
        that already knows a config reload does not belong here (e.g. mid-tick, after the
        scheduler's own config-reload gate has already run once for this pass; see
        Scheduler._reload_config_if_safe, CG-242)."""
        self._tasks = None
        self._products = None

    def _config_signature(self) -> dict[str, int]:
        """The mtime (nanoseconds) of each garden*.yaml file that currently exists, keyed by
        name. A new or removed file changes the key set; an edit changes an mtime — either way
        the signature differs from config_changed_on_disk()/reload_config_if_changed() on."""
        sig: dict[str, int] = {}
        for name in self.config.source_names():
            try:
                sig[name] = (self.root / name).stat().st_mtime_ns
            except OSError:
                pass
        return sig

    def config_changed_on_disk(self) -> bool:
        """Whether garden*.yaml has a newer mtime than the config currently loaded — without
        reading or adopting it. Lets a caller (the scheduler's live-reload gate, CG-242) decide
        whether it is safe to adopt the change before touching `self.config`."""
        return self._config_signature() != self._config_sig

    def load_config_from_disk(self) -> Config:
        """A fresh Config read from the current garden*.yaml files, without adopting it (see
        adopt_config)."""
        return Config.load(self.root, env=self.config.env)

    def adopt_config(self, new_config: Config) -> dict[str, tuple[object, object]]:
        """Make `new_config` current, returning (and recording on `last_config_change`) the
        top-level keys whose value changed. Used directly by a caller (the scheduler's
        live-reload gate, CG-242) that already read the pending config itself via
        `load_config_from_disk` before deciding it is safe to adopt; `reload_config_if_changed`
        is the immediate-adopt path for every other caller."""
        old = self.config.data
        self.config = new_config
        self._config_sig = self._config_signature()
        new = self.config.data
        changed = {k: (old.get(k), new.get(k)) for k in set(old) | set(new) if old.get(k) != new.get(k)}
        self.last_config_change = changed
        self.invalidate_tasks()
        return changed

    def reload_config_if_changed(self) -> dict[str, tuple[object, object]]:
        """Re-read garden.yaml (and its env/local overlays) from disk when any of them has
        changed since the last read, so an edit takes effect within one tick without a restart
        (see docs/architecture.md and RESTART_KEYS for the keys this does *not* cover). Returns
        (and records on `last_config_change`) the top-level keys whose value changed, so the
        caller can log them; returns an empty mapping when nothing changed. Adopts unconditionally
        — the scheduler's tick uses `config_changed_on_disk`/`load_config_from_disk`/
        `adopt_config` directly instead, so it can hold an executable-field change against an
        in-flight run's fence manifest first (CG-242)."""
        if not self.config_changed_on_disk():
            return {}
        return self.adopt_config(self.load_config_from_disk())

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
            # botanical emblems: explicit in goals.md frontmatter, else the plant at this phase's
            # position (wrapping if there are more phases than plants; the plate number still
            # distinguishes them). Purely positional, so pinning one phase's plant never moves
            # another phase's pick, even when the pin collides with what that position would get.
            for i, ph in enumerate(phases):
                if not ph.plate:
                    ph.plate = roman(i + 1)
                if ph.plant not in PLANT_BY_KEY:
                    ph.plant = PLANTS[i % len(PLANTS)]["key"]
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
            dups: dict[str, list[Path]] = {}
            for p in self.products():
                for ph in p.phases:
                    for t in ph.tasks:
                        if t.id in dups:
                            dups[t.id].append(t.path)
                            continue
                        if t.id in tasks:
                            # An id claimed by two files is ambiguous: quarantine it. Drop both
                            # claimants from the map so neither can dispatch, and record the
                            # collision for `duplicate_ids` (surfaced by `garden validate` and the
                            # tick) rather than raising and taking every page and tick down with it.
                            dups[t.id] = [tasks.pop(t.id).path, t.path]
                            continue
                        tasks[t.id] = t
            self._tasks = tasks
            self._duplicate_ids = dups
        return self._tasks

    def duplicate_ids(self) -> dict[str, list[str]]:
        """Ids claimed by more than one task file, each mapped to the files that claim it. Such
        an id is ambiguous: `tasks()` keeps it out of the task map so it cannot dispatch, and this
        is how `garden validate`, `doctor` and the tick surface it. Empty when the garden is
        healthy."""
        self.tasks()  # ensure a scan has populated _duplicate_ids
        return {tid: [self.rel(p) for p in paths] for tid, paths in self._duplicate_ids.items()}

    def task(self, task_id: str) -> Task:
        tasks = self.tasks()
        if task_id in tasks:
            return tasks[task_id]
        # tolerate case differences and prefixes like "cg-3"
        for tid, t in tasks.items():
            if tid.lower() == task_id.lower():
                return t
        raise KeyError(f"no task {task_id!r}")

    def control_task(self, task_id: str) -> Task:
        """Load one canonical task without discovering the garden.

        Incident controls use this bounded path so an expensive products/tasks page read
        cannot delay accepting a recovery launch.  Control clients use the canonical id
        already shown by the operation/status API; fuzzy lookup remains a UI convenience.
        """
        if re.fullmatch(r"[A-Za-z][A-Za-z0-9]*-[0-9]+", task_id) is None:
            raise KeyError(f"invalid canonical task id {task_id!r}")
        matches = list(self.root.glob(f"*/*/tasks/{task_id}-*.md"))
        if len(matches) != 1:
            raise KeyError(f"no unique task {task_id!r}")
        path = matches[0]
        return self._load_task(path, path.parents[2].name, path.parents[1].name)

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
    def save(self, task: Task) -> bool:
        """Write a task file, merging our change onto whatever is on disk instead of trusting the
        in-memory copy to be current. A task file is a whole document but a save only ever means
        to change a few of its fields, so a concurrent tick and web action that touch different
        fields (a status transition and a priority edit, say) must both survive rather than the
        later writer clobbering the earlier one's whole file.

        Under an exclusive lock (a single `.garden/tasks.lock`, so the read-merge-write is atomic
        across threads and processes) this re-reads the current file and 3-way-merges against the
        snapshot the task was loaded from: only fields this writer actually changed are reapplied,
        and appended log lines from both writers are kept. A brand-new task, or one being written
        to a new path (a move), is written whole.

        Returns True if it wrote. Returns False without writing when the file this task was loaded
        from is gone — a concurrent move relocated it — so a stale save can't recreate the old
        path and leave two files with the same id (which would stop the whole garden). The moved
        file already carries the current state; the next tick reapplies anything this drop lost."""
        loaded_fm = task._loaded_fm
        loaded_body = task._loaded_body
        moved = task._loaded_path is not None and task._loaded_path != task.path
        task.touch()
        task.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.config.garden_dir / "tasks.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with open(lock_path, "a") as lf:
            fcntl.flock(lf, fcntl.LOCK_EX)
            if loaded_fm is None or moved:
                # a task never loaded from disk (freshly created), or a rename to a new path:
                # nothing on disk here to merge against, so write our copy whole.
                fm, body = task.to_frontmatter(), task.body
            elif not task.path.exists():
                # the file we loaded is gone: a concurrent move took it. Recreating it here would
                # resurrect the task at its old location as a duplicate id. Drop this save.
                return False
            else:
                disk_fm, disk_body = _read_task(task.path, task.product, task.phase)
                fm = _merge_frontmatter(loaded_fm, task.to_frontmatter(), disk_fm)
                body = _merge_body(loaded_body or "", task.body, disk_body)
            _atomic_write(task.path, join_frontmatter(fm, body))
        task.snapshot()
        return True

    def next_id(self, product: str) -> str:
        """The next free id for `product`, skipping both existing task files and any ids reserved
        in the durable ledger (see `reserve_ids`). Advisory: it takes no lock, so a caller that
        must not collide with a concurrent creator uses `create_task` or `reserve_ids`, which
        allocate under one."""
        return self._compute_next_id(product, self._read_reservations())

    def _compute_next_id(self, product: str, ledger: dict[str, dict]) -> str:
        prefix = str(self.config.product(product).get("id_prefix") or _prefix_for(product))
        up = prefix.upper() + "-"
        n = 0
        # Existing task files, reserved ids, and quarantined duplicate ids all count as taken:
        # a duplicate is out of the task map but its files are on disk, so reusing its number
        # would only add a third clashing file.
        for tid in (*self.tasks().keys(), *ledger.keys(), *self._duplicate_ids.keys()):
            if tid.upper().startswith(up):
                try:
                    n = max(n, int(tid.split("-", 1)[1]))
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
        discovered_from: str = "",
    ) -> Task:
        from .model import Status, slugify

        def build(tid: str) -> Task:
            ph = self.phase(product, phase)
            return Task(
                path=ph.path / "tasks" / f"{tid}-{slugify(title)}.md",
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
                discovered_from=discovered_from,
                created=now_iso(),
                updated=now_iso(),
                body=body,
            )

        if task_id is None:
            # Allocate the id and write the file under the reservation lock, so the id is durably
            # taken (the file itself is the record) before any concurrent creator or reservation
            # can pick it — the same lock every reserve_ids call holds.
            with self._reservation_lock():
                self._tasks = None  # rescan under the lock, so a file another creator just wrote is seen
                ledger = self._prune_reservations_locked(self._read_reservations())
                t = build(self._compute_next_id(product, ledger))
                self.save(t)
        else:
            t = build(task_id)
            self.save(t)
        self.invalidate_tasks()
        return t

    # ---- durable id reservations ------------------------------------------
    # A retro drafts its next-phase tasks into a git worktree, not the live tree, so their ids are
    # invisible to next_id until the PR merges. Between filing and merge every live task creator
    # (discovered work, another retro, the planner) would hand out the same ids and collide on
    # merge. Reserving those ids in `.garden/reservations.json` — read by next_id and held under a
    # lock shared with create_task — closes that window durably (it survives a restart).
    @property
    def reservations_path(self) -> Path:
        return self.config.garden_dir / "reservations.json"

    @contextmanager
    def _reservation_lock(self):
        garden_dir = self.config.garden_dir
        garden_dir.mkdir(parents=True, exist_ok=True)
        with open(garden_dir / (self.reservations_path.name + ".lock"), "a") as lf:
            fcntl.flock(lf, fcntl.LOCK_EX)
            yield

    def _read_reservations(self) -> dict[str, dict]:
        try:
            raw = json.loads(self.reservations_path.read_text())
        except (OSError, json.JSONDecodeError):
            return {}
        return {k: v for k, v in raw.items() if isinstance(v, dict)} if isinstance(raw, dict) else {}

    def _write_reservations_locked(self, ledger: dict[str, dict]) -> None:
        if ledger:
            _atomic_write(self.reservations_path, json.dumps(ledger, indent=2, sort_keys=True))
        else:
            self.reservations_path.unlink(missing_ok=True)

    def _prune_reservations_locked(self, ledger: dict[str, dict]) -> dict[str, dict]:
        """Drop reservations whose id now exists as a task file: the reservation did its job (the
        worktree draft merged) and is redundant. The natural release for a completed retro."""
        tasks = self.tasks()
        return {tid: meta for tid, meta in ledger.items() if tid not in tasks}

    def reserve_ids(self, product: str, count: int, *, owner: str, reason: str = "") -> list[str]:
        """Atomically reserve `count` ids for `product` and record them durably, so every other
        creator (via next_id / create_task) skips them until they are materialised as task files
        or released. Reservations survive a restart. `owner` groups them so `release_reservation`
        can drop an abandoned batch; a batch is otherwise released piecemeal as its ids merge (see
        `_prune_reservations_locked`)."""
        if count <= 0:
            return []
        ids: list[str] = []
        with self._reservation_lock():
            self._tasks = None  # rescan under the lock, past any file another creator just wrote
            ledger = self._prune_reservations_locked(self._read_reservations())
            for _ in range(count):
                tid = self._compute_next_id(product, ledger)
                ledger[tid] = {"owner": owner, "reason": reason, "at": now_iso()}
                ids.append(tid)
            self._write_reservations_locked(ledger)
        return ids

    def release_reservation(self, owner: str) -> list[str]:
        """Drop every reservation held by `owner`. A retro releases its own batch before it files
        a fresh one, so an abandoned prior attempt's ids are reclaimed rather than leaked."""
        with self._reservation_lock():
            ledger = self._read_reservations()
            released = [tid for tid, meta in ledger.items() if meta.get("owner") == owner]
            for tid in released:
                ledger.pop(tid, None)
            if released:
                self._write_reservations_locked(ledger)
        return released

    def prune_reservations(self) -> list[str]:
        """Drop reservations whose id has since become a task file (the worktree draft merged).
        Called each tick so the ledger does not accumulate fulfilled entries."""
        with self._reservation_lock():
            self._tasks = None
            ledger = self._read_reservations()
            kept = self._prune_reservations_locked(ledger)
            pruned = [tid for tid in ledger if tid not in kept]
            if pruned:
                self._write_reservations_locked(kept)
        return pruned

    def reserved_ids(self) -> dict[str, dict]:
        """The current reservation ledger (id -> {owner, reason, at}). A copy of what is on disk."""
        return self._read_reservations()

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
        self.invalidate_tasks()

    def set_phase_frozen(self, phase: Phase, frozen: str) -> None:
        """Write (or, with an empty string, clear) `frozen:` in the phase's goals.md frontmatter."""
        goals = phase.goals_path or (phase.path / "goals.md")
        meta: dict = {}
        body = ""
        if goals.exists():
            meta, body = split_frontmatter(goals.read_text())
        if frozen:
            meta["frozen"] = frozen
        else:
            meta.pop("frozen", None)
        _atomic_write(goals, join_frontmatter(meta, body) if meta else body)
        self.invalidate_tasks()

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


def _read_task(path: Path, product: str, phase: str) -> tuple[dict[str, Any], str]:
    """The on-disk frontmatter (in canonical `to_frontmatter` shape) and body of a task file,
    for merging a save against. Parsing through `Task` normalises types so the three sides of
    the merge compare like with like."""
    disk = Task.parse(path, path.read_text(), product=product, phase=phase)
    return disk.to_frontmatter(), disk.body


_MISSING = object()


def _merge_frontmatter(base: dict[str, Any], ours: dict[str, Any], theirs: dict[str, Any]) -> dict[str, Any]:
    """3-way merge of frontmatter: reapply only the keys this writer changed (`ours` vs `base`)
    onto the current disk copy (`theirs`), so a key another writer changed meanwhile is kept.
    A key we changed wins over a concurrent change to the same key; a key neither of us touched
    keeps its disk value. Setting a key to absent (dropped from `to_frontmatter`) counts as a
    change. Key order follows `ours`, then any keys only the other writer added."""
    merged: dict[str, Any] = {}
    for key in list(ours) + [k for k in theirs if k not in ours] + [k for k in base if k not in ours and k not in theirs]:
        if key in merged:
            continue
        b, o, t = base.get(key, _MISSING), ours.get(key, _MISSING), theirs.get(key, _MISSING)
        chosen = o if o != b else t  # we changed it -> ours; else whatever disk has now
        if chosen is not _MISSING:
            merged[key] = chosen
    return merged


def _merge_body(base: str, ours: str, theirs: str) -> str:
    """3-way merge of the task body. The body changes almost only by `Task.log` appending lines,
    so when both sides changed it, reapply the lines we appended past the shared prefix onto the
    disk copy (skipping any already there). A change that is not a clean append keeps our body."""
    if ours == theirs or theirs == base:
        return ours
    if ours == base:
        return theirs
    base_lines, our_lines = base.rstrip("\n").splitlines(), ours.rstrip("\n").splitlines()
    i = 0
    while i < len(base_lines) and i < len(our_lines) and base_lines[i] == our_lines[i]:
        i += 1
    if i < len(base_lines):
        return ours  # we changed existing content, not a clean append; can't safely splice
    merged = theirs.rstrip("\n").splitlines()
    for line in our_lines[i:]:
        if line not in merged:
            merged.append(line)
    return "\n".join(merged) + "\n"


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
