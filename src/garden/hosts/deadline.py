"""Fail-closed adapter for controller-independent host termination schedules.

The configured command is an administrator-installed integration with the external
scheduler used by the deployment (for example EventBridge Scheduler plus a scoped
termination target). Garden sends one secret-free JSON declaration on stdin. The command
must durably arm the schedule, read it back, and only then return the matching JSON receipt.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence

from .models import CONTRACT_VERSION, HostDeclaration


class ExternalDeadlineCommand:
    """Arm and verify an external deadline without invoking a shell."""

    def __init__(self, command: Sequence[str], *, timeout_seconds: float = 30) -> None:
        self.command = tuple(command)
        if not self.command or any(not isinstance(part, str) or not part for part in self.command):
            raise ValueError("deadline command must contain nonempty arguments")
        if timeout_seconds <= 0 or timeout_seconds > 60:
            raise ValueError("deadline command timeout must be greater than zero and at most 60 seconds")
        self.timeout_seconds = timeout_seconds

    def arm_and_verify(self, declaration: HostDeclaration) -> None:
        request = {
            "contract_version": CONTRACT_VERSION,
            "host_id": declaration.host_id,
            "operation_id": declaration.operation_id,
            "deadline_utc": declaration.deadline_utc,
            "provider": declaration.pool.provider,
            "owner": declaration.pool.owner,
            "pool": declaration.pool.name,
        }
        try:
            completed = subprocess.run(
                self.command,
                input=json.dumps(request),
                text=True,
                capture_output=True,
                check=False,
                timeout=self.timeout_seconds,
                shell=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError(f"external deadline command failed: {exc}") from exc
        if completed.returncode:
            raise RuntimeError(
                f"external deadline command exited with status {completed.returncode}"
            )
        try:
            receipt = json.loads(completed.stdout)
        except (json.JSONDecodeError, TypeError) as exc:
            raise RuntimeError("external deadline command returned invalid JSON") from exc
        expected = {
            "armed": True,
            "host_id": declaration.host_id,
            "operation_id": declaration.operation_id,
            "deadline_utc": declaration.deadline_utc,
        }
        if not isinstance(receipt, dict) or any(receipt.get(k) != v for k, v in expected.items()):
            raise RuntimeError("external deadline command did not verify the requested schedule")
