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
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class Run:
    task_id: str
    run_id: str
    dir: str
    runner: str
    mode: str = "work"  # work | revise | review
    harness: str = ""
    model: str = ""
    host: str = ""  # ssh runner: which host
    session_id: str = ""  # harness session, for resume
    status: str = "running"  # running | done | blocked | failed | timeout | cancelled
    pid: int | None = None
    started_at: str = ""
    finished_at: str = ""
    worktree: str = ""
    branch: str = ""
    base: str = ""
    exit_code: int | None = None
    result: dict[str, Any] = field(default_factory=dict)
    usage: dict[str, Any] = field(default_factory=dict)
    cost_usd: float | None = None
    brief_tokens: int = 0
    error: str = ""

    @property
    def path(self) -> Path:
        return Path(self.dir)

    def save(self) -> None:
        self.path.mkdir(parents=True, exist_ok=True)
        (self.path / "run.json").write_text(json.dumps(asdict(self), indent=2))

    @classmethod
    def load(cls, d: Path) -> Run:
        data = json.loads((d / "run.json").read_text())
        return cls(**data)

    # ---- process state -----------------------------------------------------
    def process_finished(self) -> bool:
        if (self.path / "exit_code").exists():
            return True
        if self.pid is None:
            return False  # human-driven run: only `garden finish` completes it
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

    def kill(self) -> None:
        if self.pid and _pid_alive(self.pid):
            try:
                os.killpg(self.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                try:
                    os.kill(self.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass

    def stdout_text(self) -> str:
        p = self.path / "stdout.json"
        return p.read_text() if p.exists() else ""

    def stderr_text(self) -> str:
        p = self.path / "stderr.log"
        return p.read_text() if p.exists() else ""


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


class RunStore:
    def __init__(self, garden_dir: Path):
        self.dir = garden_dir / "runs"

    def new_run(self, task_id: str, runner: str, mode: str = "work") -> Run:
        stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
        run_id = f"{stamp}-{mode}"
        d = self.dir / task_id / run_id
        n = 1
        while d.exists():
            n += 1
            run_id = f"{stamp}-{mode}-{n}"
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
        d = self.dir / task_id
        if not d.exists():
            return []
        out = []
        for rd in sorted(d.iterdir()):
            if (rd / "run.json").exists():
                try:
                    out.append(Run.load(rd))
                except (json.JSONDecodeError, TypeError):
                    continue
        out.sort(key=lambda r: (r.started_at, r.run_id))
        return out

    def latest(self, task_id: str) -> Run | None:
        runs = self.runs_for(task_id)
        active = [r for r in runs if r.status == "running"]
        if active:
            return active[-1]
        return runs[-1] if runs else None

    def all_runs(self) -> list[Run]:
        if not self.dir.exists():
            return []
        out: list[Run] = []
        for td in sorted(self.dir.iterdir()):
            if td.is_dir():
                out.extend(self.runs_for(td.name))
        return out

    def active(self) -> list[Run]:
        return [r for r in self.all_runs() if r.status == "running"]

    def usage_for(self, task_id: str) -> dict[str, Any]:
        """Tokens and cost across every run of one task, split by run mode."""
        return _rollup(self.runs_for(task_id))

    def usage_by_task(self) -> dict[str, dict[str, Any]]:
        out: dict[str, list[Run]] = {}
        for r in self.all_runs():
            out.setdefault(r.task_id, []).append(r)
        return {tid: _rollup(rs) for tid, rs in out.items()}

    def totals(self) -> dict[str, Any]:
        runs = self.all_runs()
        cost = sum(r.cost_usd or 0.0 for r in runs)
        inp = sum(int(r.usage.get("input_tokens", 0) or 0) for r in runs)
        out = sum(int(r.usage.get("output_tokens", 0) or 0) for r in runs)
        cache_read = sum(int(r.usage.get("cache_read_input_tokens", 0) or 0) for r in runs)
        return {
            "runs": len(runs),
            "cost_usd": round(cost, 4),
            "input_tokens": inp,
            "output_tokens": out,
            "cache_read_input_tokens": cache_read,
        }


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
        m = tot["by_mode"].setdefault(r.mode, {"runs": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0})
        m["runs"] += 1
        m["input_tokens"] += inp
        m["output_tokens"] += outp
        m["cost_usd"] += cost
    tot["cost_usd"] = round(tot["cost_usd"], 4)
    tot["minutes"] = round(tot["minutes"], 1)
    tot["total_tokens"] = tot["input_tokens"] + tot["output_tokens"] + tot["cache_read_input_tokens"] + tot["cache_creation_input_tokens"]
    for m in tot["by_mode"].values():
        m["cost_usd"] = round(m["cost_usd"], 4)
    return tot
