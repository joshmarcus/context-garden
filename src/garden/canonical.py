"""Safety boundary for explicitly provisioned, in-place product checkouts."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from pathlib import Path
from typing import Any

from . import gitops


class CanonicalCheckoutError(RuntimeError):
    pass


def configured_root(config: dict[str, Any], garden_root: Path) -> Path | None:
    if str(config.get("strategy") or "worktree") != "in_place":
        return None
    raw = str(config.get("root") or "").strip()
    if not raw:
        raise CanonicalCheckoutError("in-place checkout requires checkout.root")
    root = Path(raw)
    root = root if root.is_absolute() else garden_root / root
    # Refuse aliases: the configured spelling itself is part of the ownership boundary.
    if root.absolute() != root.resolve():
        raise CanonicalCheckoutError(f"canonical checkout root may not contain symlinks: {root}")
    if root.resolve() == garden_root.resolve():
        raise CanonicalCheckoutError("canonical checkout may not be the controller checkout")
    if not gitops.is_repo(root):
        raise CanonicalCheckoutError(f"canonical checkout is not a git repository: {root}")
    return root


def lease_path(root: Path) -> Path:
    return root.parent / f".{root.name}.garden-lease.json"


def claim(root: Path, run_id: str, active_run_ids: set[str]) -> None:
    """Atomically claim ``root``; stale files are recoverable only after run audit."""
    path = lease_path(root)
    if active_run_ids:
        owner = sorted(active_run_ids)[0]
        raise CanonicalCheckoutError(f"canonical checkout is leased by active run {owner}")
    payload = json.dumps({"run_id": run_id, "pid": os.getpid()}) + "\n"
    for _ in range(2):
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            try:
                owner = json.loads(path.read_text())
            except (OSError, ValueError):
                raise CanonicalCheckoutError(f"canonical checkout has an unreadable lease: {path}") from None
            owner_run = str(owner.get("run_id") or "")
            if owner_run in active_run_ids:
                raise CanonicalCheckoutError(
                    f"canonical checkout is leased by active run {owner_run}"
                ) from None
            # The durable run store is authoritative across controller restarts.  A lease
            # whose run is no longer active is deliberately reclaimed, never guessed from
            # the controller PID recorded for diagnostics.
            try:
                path.unlink()
            except OSError as exc:
                raise CanonicalCheckoutError(f"cannot reclaim stale canonical lease {path}: {exc}") from exc
            continue
        else:
            with os.fdopen(fd, "w") as stream:
                stream.write(payload)
            return
    raise CanonicalCheckoutError(f"could not claim canonical checkout: {root}")


def release(root: Path, run_id: str) -> None:
    path = lease_path(root)
    try:
        owner = json.loads(path.read_text())
        if str(owner.get("run_id") or "") == run_id:
            path.unlink()
    except (OSError, ValueError):
        return


def preflight(root: Path, branch: str, base: str) -> None:
    status = gitops.status_lines(root)
    if status:
        raise CanonicalCheckoutError(
            "canonical checkout has uncommitted work; preserve or commit it before retrying: "
            + ", ".join(status)
        )
    current = gitops.git("rev-parse", "--abbrev-ref", "HEAD", cwd=root).strip()
    if current not in {branch, base}:
        raise CanonicalCheckoutError(
            f"canonical checkout branch drift: on {current!r}, expected {branch!r} or base {base!r}; "
            "switch it deliberately before retrying"
        )
    # A canonical checkout may retain the accepted task branch between rounds.  Switching
    # from the configured base is safe only after the clean-tree check above.
    if current == base and current != branch:
        if gitops.branch_exists(root, branch):
            gitops.git("checkout", branch, cwd=root)
        else:
            gitops.git("checkout", "-b", branch, gitops.base_ref(root, base), cwd=root)


def reconcile(root: Path, config: dict[str, Any], env: dict[str, str], log_path: Path) -> None:
    command = str(config.get("reconcile_command") or "").strip()
    if not command:
        return
    timeout = int(config.get("reconcile_timeout_seconds") or 600)
    try:
        proc = subprocess.run(command, shell=True, cwd=root, env=env, capture_output=True,
                              text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        raise CanonicalCheckoutError(f"reconciliation timed out after {timeout}s: {command}") from exc
    output = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
    log_path.write_text(output)
    if proc.returncode:
        raise CanonicalCheckoutError(
            f"reconciliation failed (exit {proc.returncode}): {shlex.join(['sh', '-c', command])}\n"
            + "\n".join(output.splitlines()[-40:])
        )
