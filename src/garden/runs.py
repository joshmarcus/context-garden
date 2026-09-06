"""Run records: one JSON file per worker run under .garden/runs/<task>/<run_id>/.

A run directory holds:
    run.json      metadata + final status + usage
    brief.md      the exact prompt sent
    stdout.json   raw runner output (claude -p --output-format json)
    stderr.log
    exit_code     written by the wrapper when the process ends (completion signal)
"""

from __future__ import annotations

import datetime as dt
import json
import os
import signal
import threading
import time
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .config import Config
    from .events import EventLog


_INDEXES: dict[Path, _RunIndex] = {}
_INDEXES_LOCK = threading.Lock()
_MAX_SHARED_INDEXES = 32


@dataclass
class _RunIndex:
    """One process-wide, short-lived view of run metadata for a garden.

    Web requests create many RunStore instances.  Sharing this view means concurrent
    requests coalesce onto one scan rather than each parsing every run.  Writers bump
    ``generation`` immediately; otherwise the one-second maximum age bounds visibility of
    changes made by another process.
    """

    lock: threading.Lock = field(default_factory=threading.Lock)
    generation: int = 0
    built_generation: int = -1
    built_at: float = 0.0
    runs: tuple[Run, ...] = ()
    by_task: dict[str, tuple[Run, ...]] = field(default_factory=dict)
    active: tuple[Run, ...] = ()
    totals: dict[str, Any] = field(default_factory=dict)
    task_fingerprints: dict[str, tuple[int, int]] = field(default_factory=dict)
    archive_fingerprint: tuple[int, int] | None = None
    dirty_tasks: set[str] = field(default_factory=set)
    scans: int = 0
    reads: int = 0


class HistoryUnavailable(RuntimeError):
    """Durable history exists but its compact index cannot be trusted."""


@dataclass
class Run:
    task_id: str
    run_id: str
    dir: str
    runner: str
    mode: str = "work"  # work | revise | resume | trial | rebase | review | persona | compare | edit | check
    harness: str = ""
    model: str = ""
    difficulty: str = ""  # easy | medium | hard; determines the turn cap
    host: str = ""  # ssh runner: which host
    session_id: str = ""  # harness session, for resume
    status: str = "running"  # running | done | blocked | failed | timeout | cancelled | superseded
    pid: int | None = None
    started_at: str = ""
    finished_at: str = ""
    worktree: str = ""
    branch: str = ""
    base: str = ""
    start_head: str = ""  # origin/<branch>'s sha this run started from, for a lease-protected push (CG-220)
    exit_code: int | None = None
    diff_stat: str = ""  # `git diff --stat base...branch` at finalize, for attention/triage evidence
    patch_id_before: str = ""  # rebase mode only: patch id of the branch's diff before the rebase
    patch_id_after: str = ""  # rebase mode only: patch id of the branch's diff after the rebase
    result: dict[str, Any] = field(default_factory=dict)
    usage: dict[str, Any] = field(default_factory=dict)
    cost_usd: float | None = None
    brief_tokens: int = 0
    error: str = ""
    fence_paths: list[str] = field(default_factory=list)  # dirs a worker must not write (garden, product clone)
    # What dispatch() cleared from state to start a revise/rebase round (the feedback text,
    # its easy/rebase tags, or that rebase_pending was popped): a quota env_error restores
    # these instead of losing the round's context (see reap._handle_quota_env_error).
    env_snapshot: dict[str, Any] = field(default_factory=dict)

    @property
    def path(self) -> Path:
        return Path(self.dir)

    def save(self) -> None:
        self.path.mkdir(parents=True, exist_ok=True)
        (self.path / "run.json").write_text(json.dumps(asdict(self), indent=2))
        # A metadata rewrite does not change the parent directory mtime by itself.  Touch
        # the task bucket so other processes can detect this one changed without statting
        # every run.json in it.
        self.path.parent.touch()
        _invalidate_index(self.path.parents[1], self.task_id)

    @classmethod
    def load(cls, d: Path) -> Run:
        data = json.loads((d / "run.json").read_text())
        data["dir"] = str(d)
        return cls(**data)

    # ---- process state -----------------------------------------------------
    @property
    def no_process(self) -> bool:
        """A record written at dispatch and never launched: still running, no pid and no
        output. The scheduler counts it against a slot until a tick reaps it, so the Now page
        shows it as what it is and a review behind it says what it waits for."""
        return self.status == "running" and self.pid is None and not (self.path / "stdout.json").exists()

    def process_finished(self) -> bool:
        if self.pid == os.getpid():
            return (self.path / "exit_code").exists()
        if self.pid is None:
            return (self.path / "exit_code").exists()
        # Local wrappers are session leaders, so their pid is also the process-group id.
        # The wrapper may exit after a harness leaves children behind. Keep the run active
        # until that entire owned group is gone; otherwise cleanup and slot accounting can
        # race a detached test suite that is still consuming the host.
        if self.runner == "local":
            return not _process_group_alive(self.pid)
        if (self.path / "exit_code").exists():
            return True
        return not _pid_alive(self.pid)

    def read_exit_code(self) -> int | None:
        p = self.path / "exit_code"
        if p.exists():
            try:
                return int(p.read_text().strip() or -1)
            except ValueError:
                return -1
        return None

    def elapsed_minutes(self) -> float:
        if not self.started_at:
            return 0.0
        start = dt.datetime.fromisoformat(self.started_at)
        end = dt.datetime.fromisoformat(self.finished_at) if self.finished_at else dt.datetime.now(dt.UTC)
        return max(0.0, (end - start).total_seconds() / 60)

    def last_activity_at(self) -> dt.datetime | None:
        """The most recent sign of life from the worker: its captured output growing or
        any file in its worktree changing. A worker's edits and commits touch the
        worktree, and streamed output grows stdout.json, so the newest of these mtimes
        stands in for "is it doing anything". Returns None when nothing is measurable
        yet (e.g. a remote run with no local worktree and no output)."""
        times: list[float] = []
        for name in ("stdout.json", "stderr.log"):
            try:
                times.append((self.path / name).stat().st_mtime)
            except OSError:
                pass
        if self.worktree:
            m = _newest_mtime(Path(self.worktree))
            if m:
                times.append(m)
        if not times:
            return None
        return dt.datetime.fromtimestamp(max(times), dt.UTC)

    def idle_minutes(self) -> float:
        """Minutes since the last sign of life (see last_activity_at). 0 when unknown."""
        last = self.last_activity_at()
        if last is None:
            return 0.0
        return max(0.0, (dt.datetime.now(dt.UTC) - last).total_seconds() / 60)

    def kill(self) -> None:
        # The in-process test runner uses the scheduler process as the liveness sentinel.
        # Never let a corrupt or synthetic run record terminate the process doing the reap.
        if self.pid == os.getpid():
            return
        if self.pid and (self.runner == "local" and _process_group_alive(self.pid) or _pid_alive(self.pid)):
            try:
                os.killpg(self.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                try:
                    os.kill(self.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass

    def stop(self, timeout: float = 5.0) -> bool:
        """Stop this worker and confirm it has exited before its worktree is reused.

        SIGTERM gives a harness a brief chance to leave its transcript intact.  A worker that
        ignores it is force-killed; failure to observe its death is deliberately reported to the
        caller rather than allowing two processes to edit one worktree.
        """
        if self.pid is None or self.pid == os.getpid():
            return False  # No safely identifiable process to terminate.
        self.kill()
        deadline = time.monotonic() + timeout
        alive = _process_group_alive if self.runner == "local" else _pid_alive
        while time.monotonic() < deadline:
            if not alive(self.pid):
                return True
            time.sleep(0.05)
        if self.pid and alive(self.pid):
            try:
                os.killpg(self.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                try:
                    os.kill(self.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not alive(self.pid):
                return True
            time.sleep(0.05)
        return not alive(self.pid)

    def stdout_text(self) -> str:
        p = self.path / "stdout.json"
        return p.read_text() if p.exists() else ""

    def stdout_events(self, n: int | None = 50) -> list[dict[str, Any]]:
        """Parse stdout.json as JSONL and return event dicts (the last n, or all when n is None)."""
        out: list[dict[str, Any]] = []
        for line in self.stdout_text().splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                ev = json.loads(line)
                if isinstance(ev, dict):
                    out.append(ev)
            except json.JSONDecodeError:
                continue
        return out if n is None else out[-n:]

    def stderr_text(self) -> str:
        p = self.path / "stderr.log"
        return p.read_text() if p.exists() else ""


def _newest_mtime(root: Path) -> float:
    """Newest mtime of any file under root, skipping the .git bookkeeping dir. 0.0 for an
    empty, missing or unreadable tree. Used to tell whether a worker is still touching its
    worktree (a linked worktree's .git is a gitlink file, not a dir, so it costs nothing to
    skip; a plain checkout's .git dir is skipped so git's own churn is not read as work)."""
    newest = 0.0
    for dirpath, dirnames, filenames in os.walk(root):
        if ".git" in dirnames:
            dirnames.remove(".git")
        for name in filenames:
            try:
                m = os.stat(os.path.join(dirpath, name)).st_mtime
            except OSError:
                continue
            if m > newest:
                newest = m
    return newest


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # zombie check on linux
    try:
        state = Path(f"/proc/{pid}/stat").read_text().split(")")[-1].split()[0]
        return state != "Z"
    except OSError:
        return True


def _process_group_alive(pgid: int) -> bool:
    """Whether any process remains in a local run's session/process group."""
    proc = Path("/proc")
    if proc.is_dir():
        try:
            for entry in proc.iterdir():
                if not entry.name.isdigit():
                    continue
                fields = (entry / "stat").read_text().split(")", 1)[1].split()
                if len(fields) > 2 and int(fields[2]) == pgid and fields[0] != "Z":
                    return True
            return False
        except (OSError, ValueError):
            pass
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _invalidate_index(runs_dir: Path, task_id: str | None = None) -> None:
    key = runs_dir.resolve()
    with _INDEXES_LOCK:
        idx = _INDEXES.get(key)
    if idx is not None:
        with idx.lock:
            idx.generation += 1
            if task_id:
                idx.dirty_tasks.add(task_id)


class RunStore:
    MAX_INDEX_AGE_SECONDS = 1.0

    def __init__(self, garden_dir: Path):
        self.dir = garden_dir / "runs"
        self.archive_dir = garden_dir / "run-archive"
        key = self.dir.resolve()
        with _INDEXES_LOCK:
            if key not in _INDEXES and len(_INDEXES) >= _MAX_SHARED_INDEXES:
                _INDEXES.pop(next(iter(_INDEXES)))
            self._index = _INDEXES.setdefault(key, _RunIndex())

    def invalidate(self) -> None:
        _invalidate_index(self.dir)

    def _snapshot(self) -> list[Run]:
        idx = self._ensure_index()
        with idx.lock:
            return deepcopy(list(idx.runs))

    def _ensure_index(self) -> _RunIndex:
        idx = self._index
        now = time.monotonic()
        with idx.lock:
            if idx.built_generation != idx.generation or now - idx.built_at > self.MAX_INDEX_AGE_SECONDS:
                task_fingerprints = self._task_fingerprints()
                archive_fingerprint = self._archive_fingerprint()
                initial = idx.built_generation < 0
                changed = (set(task_fingerprints) if initial else {
                    task for task in set(task_fingerprints) | set(idx.task_fingerprints)
                    if task_fingerprints.get(task) != idx.task_fingerprints.get(task)
                }) | idx.dirty_tasks
                active_by_task: dict[str, list[Run]] = {}
                for run in idx.runs:
                    if not str(run.path).startswith(str(self.archive_dir)):
                        active_by_task.setdefault(run.task_id, []).append(run)
                for task in changed:
                    active_by_task[task] = self._read_task_runs(task) if task in task_fingerprints else []
                found = [run for runs in active_by_task.values() for run in runs]
                if initial or archive_fingerprint != idx.archive_fingerprint:
                    archived = self._archived_runs()
                else:
                    archived = [r for r in idx.runs if str(r.path).startswith(str(self.archive_dir))]
                found.extend(archived)
                found.sort(key=lambda r: (r.started_at, r.task_id, r.run_id))
                idx.runs = tuple(found)
                grouped: dict[str, list[Run]] = {}
                for run in found:
                    grouped.setdefault(run.task_id, []).append(run)
                idx.by_task = {task: tuple(runs) for task, runs in grouped.items()}
                idx.active = tuple(run for run in found if run.status == "running")
                idx.totals = _totals(found)
                idx.task_fingerprints = task_fingerprints
                idx.archive_fingerprint = archive_fingerprint
                idx.dirty_tasks.clear()
                idx.built_generation = idx.generation
                idx.built_at = time.monotonic()
                idx.scans += 1
            return idx

    def _task_fingerprints(self) -> dict[str, tuple[int, int]]:
        """Cheap freshness signal: writers touch a task directory when run metadata changes."""
        out: dict[str, tuple[int, int]] = {}
        if not self.dir.exists():
            return out
        for task_dir in self.dir.iterdir():
            if not task_dir.is_dir():
                continue
            try:
                stat = task_dir.stat()
                out[task_dir.name] = (stat.st_mtime_ns, stat.st_size)
            except OSError:
                continue
        return out

    def _read_task_runs(self, task: str) -> list[Run]:
        found: list[Run] = []
        for run_json in (self.dir / task).glob("*/run.json"):
            try:
                found.append(Run.load(run_json.parent))
                self._index.reads += 1
            except (OSError, json.JSONDecodeError, TypeError):
                continue
        return found

    def _archive_fingerprint(self) -> tuple[int, int] | None:
        manifest = self.archive_dir / "index.json"
        try:
            stat = manifest.stat()
            return stat.st_mtime_ns, stat.st_size
        except FileNotFoundError:
            return None

    def _archived_runs(self) -> list[Run]:
        """Read the compact archive index, never the archived directory tree."""
        manifest = self.archive_dir / "index.json"
        if not manifest.exists():
            if self.archive_dir.exists() and any(self.archive_dir.iterdir()):
                raise HistoryUnavailable("archive index is missing; historical totals are unavailable")
            return []
        try:
            rows = json.loads(manifest.read_text()).get("runs", [])
            for row in rows:
                row["dir"] = str(self.archive_dir / row["task_id"] / row["run_id"])
            return [Run(**row) for row in rows]
        except (OSError, json.JSONDecodeError, TypeError, AttributeError) as exc:
            raise HistoryUnavailable("archive index is unreadable; historical totals are unavailable") from exc

    def archive_health(self) -> str:
        """Return an honest, cheap archive problem description, or an empty string."""
        manifest = self.archive_dir / "index.json"
        if not self.archive_dir.exists():
            return ""
        if not manifest.exists():
            return "archive index is missing; run garden archive-runs to verify and rebuild it"
        try:
            rows = json.loads(manifest.read_text()).get("runs")
            if not isinstance(rows, list):
                raise TypeError
        except (OSError, json.JSONDecodeError, TypeError, AttributeError):
            return "archive index is unreadable; run garden archive-runs to verify and rebuild it"
        return ""

    @property
    def scan_count(self) -> int:
        """Diagnostic count used by bounded-work regression tests and measurements."""
        return self._index.scans

    @property
    def read_count(self) -> int:
        return self._index.reads

    @property
    def generation(self) -> int:
        """Changes immediately after an in-process run metadata write."""
        return self._index.generation

    def next_run_id(self, task_id: str, mode: str) -> str:
        """Reserve the id `new_run` would generate for `task_id`/`mode` right now, without
        creating the run — so a caller that needs the id before the run exists (e.g. to name a
        `backup/<run-id>` ref before dispatch) gets the same id `new_run` will use a moment
        later. IDs are unique across tasks too, because they also name the shared work root's
        per-run temp directory."""
        stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
        run_id = f"{stamp}-{mode}"
        d = self.dir / task_id / run_id

        def exists_anywhere(candidate: str) -> bool:
            return d.exists() or (self.dir.exists() and any(
                (task_dir / candidate).exists() for task_dir in self.dir.iterdir() if task_dir.is_dir()))

        n = 1
        while exists_anywhere(run_id):
            n += 1
            run_id = f"{stamp}-{mode}-{n}"
            d = self.dir / task_id / run_id
        return run_id

    def new_run(self, task_id: str, runner: str, mode: str = "work", run_id: str = "") -> Run:
        run_id = run_id or self.next_run_id(task_id, mode)
        d = self.dir / task_id / run_id
        d.mkdir(parents=True, exist_ok=True)
        run = Run(
            task_id=task_id,
            run_id=run_id,
            dir=str(d),
            runner=runner,
            mode=mode,
            started_at=dt.datetime.now(dt.UTC).isoformat(),
        )
        run.save()
        return run

    def runs_for(self, task_id: str) -> list[Run]:
        idx = self._ensure_index()
        with idx.lock:
            return deepcopy(list(idx.by_task.get(task_id, ())))

    def latest(self, task_id: str) -> Run | None:
        runs = self.runs_for(task_id)
        active = [r for r in runs if r.status == "running"]
        if active:
            return active[-1]
        return runs[-1] if runs else None

    def all_runs(self) -> list[Run]:
        return self._snapshot()

    def active(self) -> list[Run]:
        idx = self._ensure_index()
        with idx.lock:
            return deepcopy(list(idx.active))

    def usage_for(self, task_id: str) -> dict[str, Any]:
        """Tokens and cost across every run of one task, split by run mode."""
        return _rollup(self.runs_for(task_id))

    def empty_usage(self) -> dict[str, Any]:
        """The zero-valued shape `usage_for`/`usage_by_task` use for a task with no runs,
        so a page indexing `usage_by_task()` by task id never falls back to a bare `{}`
        (a template's `u.runs` must see a real 0, not raise, under a strict Jinja environment)."""
        return _rollup([])

    def usage_by_task(self) -> dict[str, dict[str, Any]]:
        out: dict[str, list[Run]] = {}
        for r in self.all_runs():
            out.setdefault(r.task_id, []).append(r)
        return {tid: _rollup(rs) for tid, rs in out.items()}

    def totals(self) -> dict[str, Any]:
        idx = self._ensure_index()
        with idx.lock:
            return dict(idx.totals)

    def costs_by_task(self) -> dict[str, float]:
        """Compact cost rollup for status/budget reads without materialising Run objects."""
        idx = self._ensure_index()
        with idx.lock:
            return {
                task: round(sum(run.cost_usd or 0.0 for run in runs), 4)
                for task, runs in idx.by_task.items()
            }

    def spend_since(self, since_iso: str) -> float:
        """Total cost_usd of runs that finished at or after `since_iso` (an events.parse_since
        cutoff), for a spend-rate reading beside the operating profile (CG-221)."""
        return round(sum(r.cost_usd or 0.0 for r in self.all_runs() if (r.finished_at or "") >= since_iso), 4)

    def archive_terminal(self, before: dt.datetime, protected_run_ids: set[str] | None = None) -> int:
        """Move old terminal run directories out of the active working set.

        Selection is deliberately conservative: a record must have a terminal status,
        a recorded finish before ``before``, and must not be named by recovery state.
        Each directory move is atomic.  The archive index is then rebuilt from the
        archive itself, so retrying after interruption repairs a move that happened
        before its index write without losing or double-counting the run.
        """
        protected = protected_run_ids or set()
        terminal = {"done", "blocked", "failed", "timeout", "cancelled", "superseded"}
        moved = 0
        for run in self._active_disk_runs():
            if run.status not in terminal or not run.finished_at or run.run_id in protected:
                continue
            try:
                finished = dt.datetime.fromisoformat(run.finished_at)
            except ValueError:
                continue
            if finished >= before:
                continue
            target = self.archive_dir / run.task_id / run.run_id
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                continue
            os.replace(run.path, target)
            moved += 1
        self.rebuild_archive_index()
        self.invalidate()
        return moved

    def restore_archived(self, task_id: str, run_id: str) -> bool:
        """Restore one archived run atomically for recovery or inspection tooling."""
        source = self.archive_dir / task_id / run_id
        if not source.exists():
            return False
        target = self.dir / task_id / run_id
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            return False
        os.replace(source, target)
        self.rebuild_archive_index()
        self.invalidate()
        return True

    def rebuild_archive_index(self) -> int:
        """Verify archived metadata and atomically replace its compact index."""
        rows: list[dict[str, Any]] = []
        invalid: list[str] = []
        if self.archive_dir.exists():
            for run_json in self.archive_dir.glob("*/*/run.json"):
                try:
                    run = Run.load(run_json.parent)
                except (OSError, json.JSONDecodeError, TypeError):
                    invalid.append(str(run_json))
                    continue
                rows.append(asdict(run))
        if invalid:
            raise ValueError(f"archive contains {len(invalid)} unreadable run record(s): {invalid[0]}")
        rows.sort(key=lambda row: (row.get("started_at", ""), row.get("task_id", ""), row.get("run_id", "")))
        self.archive_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.archive_dir / "index.json.tmp"
        tmp.write_text(json.dumps({"version": 1, "runs": rows}, indent=2))
        os.replace(tmp, self.archive_dir / "index.json")
        self.invalidate()
        return len(rows)

    def update_archived(self, run: Run) -> None:
        """Persist an archived metadata correction and atomically refresh its ledger."""
        if not str(run.path).startswith(str(self.archive_dir)):
            raise ValueError("run is not archived")
        run.save()
        self.rebuild_archive_index()

    def _active_disk_runs(self) -> list[Run]:
        out: list[Run] = []
        if self.dir.exists():
            for run_json in self.dir.glob("*/*/run.json"):
                try:
                    out.append(Run.load(run_json.parent))
                except (OSError, json.JSONDecodeError, TypeError):
                    continue
        return out

    def backfill_codex_costs(self, config: Config, events: EventLog | None = None) -> int:
        """Recompute usage/cost_usd/model for every codex run from its stored transcript
        (`stdout.json`), using the harness's current price table (`garden costs --backfill`,
        CG-233): a one-off fix for runs recorded before codex usage was priced, so today's
        runs enter the cost record instead of sitting at `cost_usd: null` forever. Patches the
        matching `run_finished` events too (see `EventLog.patch_run_costs`), so `garden costs`,
        `garden metrics` and the retro pick up the correction. Returns the number of runs
        changed."""
        harness = config.harness("codex")
        patches: dict[str, dict[str, Any]] = {}
        updated = 0
        for run in self.all_runs():
            if run.harness != "codex":
                continue
            stdout = run.stdout_text()
            if not stdout.strip():
                continue
            parsed = harness.parse(stdout, run.stderr_text(), model=run.model)
            usage, cost, model = parsed.get("usage") or {}, parsed.get("cost_usd"), str(parsed.get("model") or run.model)
            if usage == run.usage and cost == run.cost_usd and model == run.model:
                continue
            run.usage, run.cost_usd, run.model = usage, cost, model
            if str(run.path).startswith(str(self.archive_dir)):
                self.update_archived(run)
            else:
                run.save()
            updated += 1
            patches[run.run_id] = {"cost_usd": cost, "usage": usage, "model": model}
        if events is not None:
            events.patch_run_costs(patches)
        return updated


def _rollup(runs: list[Run]) -> dict[str, Any]:
    tot = {"runs": len(runs), "input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0,
           "cache_creation_input_tokens": 0, "cost_usd": 0.0, "minutes": 0.0, "by_mode": {}}
    for r in runs:
        u = r.usage or {}
        inp = int(u.get("input_tokens", 0) or 0)
        outp = int(u.get("output_tokens", 0) or 0)
        cr = int(u.get("cache_read_input_tokens", 0) or 0)
        cc = int(u.get("cache_creation_input_tokens", 0) or 0)
        cost = float(r.cost_usd or 0.0)
        tot["input_tokens"] += inp
        tot["output_tokens"] += outp
        tot["cache_read_input_tokens"] += cr
        tot["cache_creation_input_tokens"] += cc
        tot["cost_usd"] += cost
        tot["minutes"] += r.elapsed_minutes() if r.finished_at else 0.0
        m = tot["by_mode"].setdefault(r.mode, {"runs": 0, "input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0, "cost_usd": 0.0})
        m["runs"] += 1
        m["input_tokens"] += inp
        m["output_tokens"] += outp
        m["cache_read_input_tokens"] += cr
        m["cost_usd"] += cost
    tot["cost_usd"] = round(tot["cost_usd"], 4)
    tot["minutes"] = round(tot["minutes"], 1)
    tot["total_tokens"] = tot["input_tokens"] + tot["output_tokens"] + tot["cache_read_input_tokens"] + tot["cache_creation_input_tokens"]
    for m in tot["by_mode"].values():
        m["cost_usd"] = round(m["cost_usd"], 4)
    return tot


def _totals(runs: list[Run]) -> dict[str, Any]:
    rollup = _rollup(runs)
    return {key: rollup[key] for key in (
        "runs", "cost_usd", "input_tokens", "output_tokens", "cache_read_input_tokens"
    )}
