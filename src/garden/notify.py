"""Notification hook for human-needed task transitions.

When a task needs a human (status transitions to awaiting_triage, waiting_human, failed, or
changes_requested, or when needs_human/stall/budget events occur), run a configured command
with the task details in environment variables. See `notify.command` in `garden.yaml` and
"Configuration and environments" in docs/architecture.md.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from pathlib import Path
from typing import Any

from .host_identity import scrub_shared_text
from .notification_adapters import NotificationDelivery, NotificationEvent

LOGGER = logging.getLogger("garden.notify")

# GARDEN_MESSAGE (and the other GARDEN_* env vars) carry worker-written text: they can
# contain quotes, backslashes or newlines. `GARDEN_NOTIFICATION_JSON` is constructed with
# json.dumps before the command runs; send that variable quoted instead of splicing a
# worker-written variable into a hand-built payload.
_GARDEN_VARS = ("GARDEN_MESSAGE", "GARDEN_TASK_ID", "GARDEN_STATUS", "GARDEN_PR", "GARDEN_NOTIFICATION_JSON")
_LOOKS_LIKE_JSON = re.compile(r'\\"|\{\s*\\?"')
_HAS_QUOTING_TOOL = re.compile(r"\bjq\b|\bpython3?\s+-c\b")


def unquoted_message_warning(command: str) -> str | None:
    """None if `command` looks safe, else a one-line warning: a JSON-shaped payload that
    splices a GARDEN_* var in directly, with no `jq` (or `python -c`) in the pipeline to
    quote it properly."""
    if not command or not _LOOKS_LIKE_JSON.search(command) or _HAS_QUOTING_TOOL.search(command):
        return None
    hit = next((v for v in _GARDEN_VARS if f"${v}" in command or f"${{{v}}}" in command), None)
    if not hit:
        return None
    return (f"notify.command splices ${hit} into what looks like a JSON payload without a "
            "quoting tool (jq, python -c, ...); a message containing a quote or newline will "
            "break the JSON, or inject data. Build the payload with `jq -n --arg message "
            '"$GARDEN_MESSAGE" ...` instead — see notify: in examples/garden.work.yaml.')


def should_notify(status: str | None, needs_human: bool = False) -> bool:
    """Return True if this status change should trigger a notification."""
    if status in ("awaiting_triage", "waiting_human", "failed"):
        return True
    if status == "changes_requested" and needs_human:
        return True
    return False


def _run_command(command: str, env: dict[str, str], timeout: float) -> tuple[bool, str]:
    """Run `command` and report what happened. Never raises."""
    try:
        result = subprocess.run(command, shell=True, env=env, timeout=timeout, capture_output=True, text=True)
    except subprocess.TimeoutExpired:
        return False, f"timed out after {timeout:.0f}s"
    except Exception as e:  # noqa: BLE001 — a broken command must not crash the scheduler
        return False, f"could not run: {e}"
    if result.returncode != 0:
        detail = next((line for line in reversed((result.stderr or result.stdout or "").splitlines()) if line.strip()), "")
        return False, f"exited {result.returncode}" + (f": {detail.strip()}" if detail else "")
    return True, ""


def _payload(task_id: str, status: str, message: str, pr_url: str, recipient: str) -> str:
    """Build the delivery payload without interpreting task or worker text as code."""
    return json.dumps({
        "recipient": recipient,
        "task_id": task_id,
        "status": status,
        "message": message,
        "pr_url": pr_url,
    })


def _notification_env(cfg: dict[str, Any], task_id: str, status: str, message: str,
                      pr_url: str) -> dict[str, str]:
    """Return delivery variables, including a JSON payload safe for a static command to send."""
    cmd_config = cfg.get("notify", {}) if isinstance(cfg.get("notify"), dict) else {}
    recipient = str(cmd_config.get("recipient") or "")
    env = os.environ.copy()
    env["GARDEN_TASK_ID"] = task_id
    env["GARDEN_STATUS"] = status
    env["GARDEN_MESSAGE"] = message
    env["GARDEN_PR"] = pr_url
    env["GARDEN_NOTIFICATION_JSON"] = _payload(task_id, status, message, pr_url, recipient)
    return env


def notify(
    cfg: dict[str, Any],
    task_id: str,
    status: str,
    message: str,
    pr_url: str = "",
) -> None:
    """Run the notify.command with task details in environment variables.

    Never raises — a broken notify.command must not block the scheduler — but unlike
    swallowing every failure outright, a command that exits non-zero, times out, or cannot
    even start is logged loudly (`garden.notify`, visible on stderr by default) so a
    misconfigured command is noticed instead of silently doing nothing.
    """
    cmd_config = cfg.get("notify", {}) if isinstance(cfg.get("notify"), dict) else {}
    delivery_path = cfg.get("_notification_delivery_path")
    if delivery_path and isinstance(cmd_config.get("destinations"), dict) and cmd_config["destinations"]:
        results = NotificationDelivery(Path(str(delivery_path))).deliver(
            cfg, NotificationEvent(task_id, status, message, pr_url),
        )
        for result in results:
            if result.endswith(("failed", "permanent failure", "revoked")):
                LOGGER.warning("notification delivery for %s (status=%s): %s", task_id, status, result)
        return
    command = cmd_config.get("command")
    if not command:
        return

    timeout = float(cmd_config.get("timeout_seconds", 30))

    env = _notification_env(cfg, task_id, status, scrub_shared_text(message, cfg), pr_url)

    ok, detail = _run_command(command, env, timeout)
    if not ok:
        LOGGER.warning("notify.command failed for %s (status=%s): %s", task_id, status, detail)


def retry_pending(cfg: dict[str, Any]) -> None:
    """Retry due typed-delivery failures without replaying task transitions."""
    cmd_config = cfg.get("notify", {}) if isinstance(cfg.get("notify"), dict) else {}
    delivery_path = cfg.get("_notification_delivery_path")
    if not (delivery_path and isinstance(cmd_config.get("destinations"), dict) and cmd_config["destinations"]):
        return
    for result in NotificationDelivery(Path(str(delivery_path))).retry_pending(cfg):
        if result.endswith(("failed", "permanent failure", "revoked")):
            LOGGER.warning("notification retry: %s", result)


def notify_test(cfg: dict[str, Any]) -> tuple[bool, str] | None:
    """Run notify.command with a synthetic payload, for `garden doctor`.

    Returns None when notify is not configured, else (ok, detail) — detail is empty on
    success and the failure reason otherwise. Used to catch a broken command (typo, missing
    binary, unreachable webhook) before a human needs the real notification.
    """
    cmd_config = cfg.get("notify", {}) if isinstance(cfg.get("notify"), dict) else {}
    command = cmd_config.get("command")
    if not command:
        return None

    timeout = float(cmd_config.get("timeout_seconds", 30))
    env = _notification_env(
        cfg, "DOCTOR-TEST", "doctor_test", "garden doctor: test notification", "",
    )
    return _run_command(command, env, timeout)
