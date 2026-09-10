"""Controller-owned worker enrollment, read afresh so revocation needs no restart."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import secrets
import stat
from pathlib import Path
from typing import Any


def worker_configuration(config: Any) -> dict[str, Any]:
    workers = dict(config.get("workers") or {})
    workers.setdefault("enrollment_registry", str(
        (config.garden_dir / "hosts" / "enrollment" / "controller-hosts.json").resolve()))
    return workers


def enrolled_hosts(workers: dict[str, Any]) -> list[dict[str, Any]]:
    """Read only the explicitly configured private registry, never task-supplied paths."""
    configured = workers.get("enrollment_registry")
    if not configured:
        return []
    path = Path(configured)
    if not path.is_absolute():
        raise ValueError("workers.enrollment_registry must be an absolute path")
    try:
        info = path.stat()
    except FileNotFoundError:
        return []
    if not stat.S_ISREG(info.st_mode) or info.st_size > 1024 * 1024:
        raise ValueError("invalid worker enrollment registry")
    if os.name == "posix":
        parent = path.parent.stat()
        if (info.st_mode & 0o077 or parent.st_mode & 0o077
                or info.st_uid != os.geteuid() or parent.st_uid != os.geteuid()):
            raise ValueError("worker enrollment registry must be private and controller-owned")
    value = json.loads(path.read_text())
    rows = value.get("hosts") if isinstance(value, dict) else None
    if not isinstance(rows, list) or len(rows) > 1024:
        raise ValueError("invalid worker enrollment registry hosts")
    names = set()
    for row in rows:
        if (not isinstance(row, dict)
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", str(row.get("name", "")))
                or not re.fullmatch(r"[0-9a-f]{64}", str(row.get("token_sha256", "")))
                or row["name"] in names):
            raise ValueError("invalid or duplicate worker enrollment")
        names.add(row["name"])
    return rows


def authenticate_worker(workers: dict[str, Any], token: str) -> dict[str, Any] | None:
    """Match static environment credentials or an independently revocable hash."""
    if not token or len(token) > 4096:
        return None
    for host in workers.get("hosts") or []:
        expected = os.environ.get(str(host.get("token_env") or ""), "")
        if expected and secrets.compare_digest(token.encode(), expected.encode()):
            return dict(host)
    digest = hashlib.sha256(token.encode()).hexdigest()
    for host in enrolled_hosts(workers):
        if not secrets.compare_digest(digest, host["token_sha256"]):
            continue
        expiry = host.get("deadline_utc")
        if expiry:
            try:
                deadline = dt.datetime.fromisoformat(expiry.replace("Z", "+00:00"))
            except (TypeError, ValueError):
                return None
            if deadline.tzinfo is None or deadline <= dt.datetime.now(dt.UTC):
                return None
        return dict(host)
    return None
