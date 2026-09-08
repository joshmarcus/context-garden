"""Run a worker-issued validation inside its owning execution budget."""

from __future__ import annotations

import json
import math
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

HARD_VALIDATION_TIMEOUT_SECONDS = 900


def inherits_validation_lease() -> bool:
    """Whether this wrapper already runs below another validation supervisor."""
    return (os.environ.get("GARDEN_VALIDATION_INHERITS_LEASE") == "1"
            or (os.environ.get("GARDEN_HEAVY_EXECUTION") == "1"
                and os.environ.get("GARDEN_OWNER_SCOPED") == "1"))


def bounded_validation_timeout_seconds(raw: object | None = None) -> float:
    """Return a usable validation budget without allowing the hard ceiling to rise."""
    if raw is None:
        raw = os.environ.get("GARDEN_VALIDATION_TIMEOUT_SECONDS", HARD_VALIDATION_TIMEOUT_SECONDS)
    try:
        configured = float(raw)
    except (TypeError, ValueError):
        configured = HARD_VALIDATION_TIMEOUT_SECONDS
    if not math.isfinite(configured) or configured <= 0:
        configured = HARD_VALIDATION_TIMEOUT_SECONDS
    return min(configured, HARD_VALIDATION_TIMEOUT_SECONDS)


def validation_timeout_result(run_dir: Path, exit_code: int | None) -> dict[str, str] | None:
    """Translate a supervisor timeout receipt into one stable check result."""
    if exit_code != 124:
        return None
    try:
        receipt = json.loads((run_dir / "validation_timeout.json").read_text())
        if (receipt.get("kind") != "validation_execution_timeout"
                or int(receipt.get("exit_code")) != 124):
            return None
        reason = str(receipt.get("reason") or "").strip()
    except (AttributeError, OSError, TypeError, ValueError, json.JSONDecodeError):
        return None
    if not reason:
        return None
    return {
        "name": "checks", "status": "error", "summary": "check execution timed out",
        "details": reason[:2000],
    }


POLICY_SOURCE_SHA = "887dbf76430e7ea8688a73eaa5392a210d4a36e6"
STRESS_NODES = (
    "tests/test_web.py::test_initial_pages_stay_bounded_with_large_run_history",
    "tests/test_web.py::test_retained_history_journey_stays_responsive_with_running_and_waiting_pytest",
    "tests/test_web.py::test_served_incident_controls_retry_and_restart_during_overload",
)


class ValidationPolicyError(RuntimeError):
    """The current policy cannot safely govern the requested validation command."""


def _pytest_command(argv: list[str]) -> bool:
    executable = Path(argv[0]).name
    if executable in {"pytest", "py.test"}:
        return True
    return executable.startswith("python") and len(argv) > 2 and argv[1:3] == ["-m", "pytest"]


def resolve_validation(argv: list[str], cwd: Path) -> tuple[list[str], dict[str, object]]:
    """Apply the approved current test policy without modifying the source checkout."""
    requested = list(argv)
    if not _pytest_command(argv):
        if Path(argv[0]).name in {"sh", "bash", "dash"} and any(
            re.search(r"(?:^|\s)(?:\S*/)?(?:python\S*\s+-m\s+)?pytest(?:\s|$)", arg)
            for arg in argv[1:]
        ):
            raise ValidationPolicyError(
                "pytest validation hidden inside a shell command cannot be governed safely; "
                "invoke pytest directly through garden.validation"
            )
        return argv, {"version": 1, "source_sha": POLICY_SOURCE_SHA, "kind": "non-pytest"}

    opted_in = "--run-stress" in argv
    policy_hook = cwd / "tests" / "conftest.py"
    branch_supports_opt_in = policy_hook.is_file() and "--run-stress" in policy_hook.read_text(
        errors="replace"
    )
    effective = list(argv)
    if opted_in and not branch_supports_opt_in:
        effective = [arg for arg in effective if arg != "--run-stress"]
    excluded: list[str] = []
    if not opted_in:
        excluded = list(STRESS_NODES)
        effective.extend(f"--deselect={node}" for node in excluded)
    return effective, {
        "version": 1,
        "source_sha": POLICY_SOURCE_SHA,
        "kind": "pytest",
        "stress_opt_in": opted_in,
        "excluded_nodes": excluded,
        "requested_selection": requested,
        "effective_selection": effective,
    }


def _source_state(cwd: Path) -> tuple[str, str]:
    try:
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=cwd, check=True, capture_output=True, text=True,
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain"], cwd=cwd, check=True, capture_output=True, text=True,
        ).stdout
        return sha, dirty
    except (OSError, subprocess.CalledProcessError):
        return "", ""


def main() -> int:
    argv = sys.argv[1:]
    if argv[:1] == ["--"]:
        argv = argv[1:]
    if not argv:
        print("usage: python -m garden.validation -- COMMAND [ARG ...]", file=sys.stderr)
        return 2
    outer = os.environ.get("GARDEN_EXECUTION_RUN_DIR")
    if not outer or not os.environ.get("GARDEN_EXECUTION_OWNER"):
        print("validation must run inside a supervised local garden run", file=sys.stderr)
        return 2
    status_dir = Path(outer) / "validations" / str(os.getpid())
    status_dir.mkdir(parents=True, exist_ok=True)
    cwd = Path.cwd()
    source_sha, source_dirty = _source_state(cwd)
    try:
        effective_argv, policy = resolve_validation(argv, cwd)
    except ValidationPolicyError as exc:
        print(f"validation policy block: {exc}", file=sys.stderr)
        return 2
    if inherits_validation_lease():
        # The enclosing validation supervisor already owns the host slot and this
        # run's owner lock.  Reacquiring either would wait on that ancestor until
        # the full suite exits.  Retain supervision and its timeout, but admit the
        # nested command through the enclosing lease.
        os.environ.pop("GARDEN_HEAVY_EXECUTION", None)
        os.environ.pop("GARDEN_OWNER_SCOPED", None)
        os.environ["GARDEN_VALIDATION_INHERITS_LEASE"] = "1"
    else:
        os.environ["GARDEN_HEAVY_EXECUTION"] = "1"
        os.environ["GARDEN_OWNER_SCOPED"] = "1"
    os.environ["GARDEN_EXECUTION_TIMEOUT_SECONDS"] = f"{bounded_validation_timeout_seconds():g}"
    command = shlex.join(argv)
    effective_command = shlex.join(effective_argv)
    completed = subprocess.run(
        [sys.executable, "-m", "garden.run_supervisor", str(status_dir), effective_command],
        check=False,
    )
    final_sha, final_dirty = _source_state(cwd)
    receipt = {
        "version": 1,
        "source_sha": source_sha,
        "source_dirty": source_dirty,
        "source_changed": (source_sha, source_dirty) != (final_sha, final_dirty),
        "command": command,
        "selection": effective_argv,
        "policy": policy,
        "exit_code": completed.returncode,
        "log_location": str(status_dir),
    }
    (status_dir / "result.json").write_text(json.dumps(receipt, sort_keys=True) + "\n")
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
