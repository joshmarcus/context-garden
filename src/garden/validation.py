"""Run a worker-issued validation inside its owning execution budget."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

POLICY_SOURCE_SHA = "887dbf76430e7ea8688a73eaa5392a210d4a36e6"
STRESS_NODES = (
    "tests/test_web.py::test_initial_pages_stay_bounded_with_large_run_history",
    "tests/test_web.py::test_retained_history_journey_stays_responsive_with_running_and_waiting_pytest",
    "tests/test_web.py::test_served_incident_controls_retry_and_restart_during_overload",
)
POLICY_ADDOPTS = tuple(f"--deselect={node}" for node in STRESS_NODES)
INDIRECT_LAUNCHERS = frozenset({"env", "make", "tox", "uv"})


class ValidationPolicyError(RuntimeError):
    """The current policy cannot safely govern the requested validation command."""


def enforce_validation_policy_env(env: dict[str, str]) -> None:
    """Make even a branch-issued plain pytest command obey the current default policy."""
    existing = shlex.split(env.get("PYTEST_ADDOPTS", ""))
    env["PYTEST_ADDOPTS"] = shlex.join([*existing, *(opt for opt in POLICY_ADDOPTS if opt not in existing)])


def _enable_stress_opt_in(env: dict[str, str]) -> None:
    existing = shlex.split(env.get("PYTEST_ADDOPTS", ""))
    env["PYTEST_ADDOPTS"] = shlex.join([opt for opt in existing if opt not in POLICY_ADDOPTS])


def _pytest_command(argv: list[str]) -> bool:
    executable = Path(argv[0]).name
    if executable in {"pytest", "py.test"}:
        return True
    return executable.startswith("python") and len(argv) > 2 and argv[1:3] == ["-m", "pytest"]


def resolve_validation(argv: list[str], cwd: Path) -> tuple[list[str], dict[str, object]]:
    """Apply the approved current test policy without modifying the source checkout."""
    requested = list(argv)
    if not _pytest_command(argv):
        launcher = Path(argv[0]).name
        if launcher in {"sh", "bash", "dash", *INDIRECT_LAUNCHERS}:
            raise ValidationPolicyError(
                f"validation delegated through {launcher!r} cannot be proven non-pytest; "
                "invoke the underlying command directly through garden.validation"
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
        receipt = {
            "version": 1,
            "source_sha": source_sha,
            "source_dirty": source_dirty,
            "source_changed": False,
            "command": shlex.join(argv),
            "selection": [],
            "policy": {
                "version": 1,
                "source_sha": POLICY_SOURCE_SHA,
                "kind": "blocked",
                "reason": str(exc),
                "requested_selection": argv,
            },
            "exit_code": 2,
            "log_location": str(status_dir),
        }
        (status_dir / "result.json").write_text(json.dumps(receipt, sort_keys=True) + "\n")
        print(f"validation policy block: {exc}", file=sys.stderr)
        return 2
    if policy.get("stress_opt_in"):
        _enable_stress_opt_in(os.environ)
    os.environ["GARDEN_HEAVY_EXECUTION"] = "1"
    os.environ["GARDEN_OWNER_SCOPED"] = "1"
    # Run the ordinary supervisor: nested validation therefore gets
    # the same signal forwarding, subreaper ownership and adopted-descendant drain as an
    # outer run, plus both the authoritative host slot and its owner's serialization lock.
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
