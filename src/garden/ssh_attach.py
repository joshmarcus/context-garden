"""Resolve and attach to a live SSH worker session without exposing its route."""

from __future__ import annotations

import json
from typing import Any

from .runs import Run


class SSHAttachmentError(RuntimeError):
    """The requested run cannot be safely attached."""


def attachment_problem(run: Run) -> str | None:
    """Return why a run is not proven attachable, or ``None`` when it is.

    The controller's collector is the authority that the remote tmux session exists.
    A session name written before that acknowledgement, or after transport recovery has
    become uncertain, is deliberately not enough to attach.
    """
    if run.runner != "ssh":
        return "run does not use the SSH runner"
    if run.status != "running":
        return f"run is {run.status}, not running"
    if not run.host:
        return "run has no recorded SSH host"
    if not (run.env_snapshot or {}).get("ssh_tmux_session"):
        return "run has no recorded tmux session"
    try:
        state = json.loads((run.path / "ssh-state.json").read_text())
    except (OSError, ValueError, json.JSONDecodeError):
        return "remote session has not been confirmed by the SSH transport"
    if not isinstance(state, dict):
        return "remote session has not been confirmed by the SSH transport"
    if state.get("status") not in {"starting", "running"}:
        if state.get("status") == "held":
            return "remote outcome is uncertain; use garden ssh-recover before attaching"
        return "remote session is not confirmed live"
    if state.get("session") != (run.env_snapshot or {}).get("ssh_tmux_session"):
        return "SSH transport confirmation belongs to a different session"
    return None


def attach_command(run: Run, *, exact: bool = False) -> str:
    """The copyable, host-detail-free command for an already eligible run."""
    command = f"garden attach {run.task_id}"
    return f"{command} --run {run.run_id}" if exact else command


def attachment_argv(run: Run, ssh_config: dict[str, Any]) -> list[str]:
    """Build the trusted transport argv for one proven live run."""
    problem = attachment_problem(run)
    if problem:
        raise SSHAttachmentError(f"cannot attach to {run.run_id}: {problem}")
    matches = [
        host for host in (ssh_config.get("hosts") or [])
        if isinstance(host, dict) and host.get("name") == run.host
    ]
    if len(matches) != 1:
        raise SSHAttachmentError(f"configured SSH route for logical host {run.host!r} is unavailable")
    destination = matches[0].get("host")
    if not isinstance(destination, str) or not destination.strip():
        raise SSHAttachmentError(f"configured SSH route for logical host {run.host!r} is unavailable")
    options = ssh_config.get("options") or ["-o", "BatchMode=yes"]
    if not isinstance(options, list) or not all(isinstance(option, str) for option in options):
        raise SSHAttachmentError("configured SSH options are invalid")
    ssh_bin = ssh_config.get("ssh_bin") or "ssh"
    if not isinstance(ssh_bin, str) or not ssh_bin:
        raise SSHAttachmentError("configured SSH binary is invalid")
    return [ssh_bin, *options, destination, "tmux", "attach-session", "-r", "-t",
            str(run.env_snapshot["ssh_tmux_session"])]
