"""Durable controller-independent deadline scheduler for managed EC2 hosts."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import plistlib
import re
import shlex
import stat
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Protocol

from .ec2 import HOST_TAG, OPERATION_TAG, OWNED_TAG, OWNER_TAG, POOL_TAG
from .locking import file_lock
from .models import HostDeclaration


def _atomic(path: Path, value: bytes, mode: int = 0o600, *, private_parent: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name == "posix":
        parent = path.parent.stat()
        if parent.st_uid != os.geteuid() or parent.st_mode & 0o022:
            raise RuntimeError(f"deadline scheduler directory is not controller-owned: {path.parent}")
        if private_parent and parent.st_mode & 0o077:
            raise RuntimeError(f"deadline state directory must be private: {path.parent}")
    temporary = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(value)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _deadline(value: str) -> dt.datetime:
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise ValueError("deadline must be an absolute UTC timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != dt.timedelta(0):
        raise ValueError("deadline must use UTC")
    return parsed


class SchedulerManager(Protocol):
    def arm_and_verify(self, operation_id: str, receipt: Path, deadline: dt.datetime) -> None: ...


class _CommandManager:
    def __init__(self, run=subprocess.run) -> None:
        self.run = run

    def _checked(self, args: list[str]) -> str:
        result = self.run(args, text=True, capture_output=True, check=False, timeout=30)
        if result.returncode:
            raise RuntimeError(f"deadline scheduler command failed with status {result.returncode}")
        return result.stdout


class SystemdUserManager(_CommandManager):
    """Install a persistent user timer; user lingering must be configured by the operator."""

    def __init__(self, unit_dir: Path | None = None, run=subprocess.run) -> None:
        super().__init__(run)
        self.unit_dir = unit_dir or Path.home() / ".config/systemd/user"

    def arm_and_verify(self, operation_id: str, receipt: Path, deadline: dt.datetime) -> None:
        label = "garden-deadline-" + operation_id
        command = " ".join(shlex.quote(v) for v in (
            sys.executable, "-m", "garden.hosts.deadline_scheduler", "--execute", str(receipt)))
        service = ("[Unit]\nDescription=Garden external host deadline\n[Service]\nType=oneshot\n"
                   f"ExecStart={command}\nRestart=on-failure\nRestartSec=60\n")
        calendar = deadline.astimezone(dt.UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
        timer = ("[Unit]\nDescription=Garden external host deadline timer\n[Timer]\n"
                 f"OnCalendar={calendar}\nPersistent=true\nUnit={label}.service\n"
                 "[Install]\nWantedBy=timers.target\n")
        _atomic(self.unit_dir / f"{label}.service", service.encode())
        timer_path = self.unit_dir / f"{label}.timer"
        _atomic(timer_path, timer.encode())
        linger = self._checked(
            ["loginctl", "show-user", str(os.getuid()), "--property=Linger", "--value"]
        ).strip()
        if linger.lower() != "yes":
            raise RuntimeError("systemd user lingering is not enabled; operator setup is required")
        self._checked(["systemctl", "--user", "daemon-reload"])
        self._checked(["systemctl", "--user", "enable", "--now", f"{label}.timer"])
        self._checked(["systemctl", "--user", "is-enabled", f"{label}.timer"])
        self._checked(["systemctl", "--user", "is-active", f"{label}.timer"])
        effective_timer = self._checked(
            ["systemctl", "--user", "show", f"{label}.timer", "--property=TimersCalendar"]
        )
        effective_service = self._checked(
            ["systemctl", "--user", "show", f"{label}.service", "--property=ExecStart"]
        )
        if (calendar not in effective_timer
                or str(receipt) not in effective_service
                or "garden.hosts.deadline_scheduler" not in effective_service):
            raise RuntimeError("effective systemd deadline definition differs")


class LaunchdManager(_CommandManager):
    """Install a per-user interval checker which persists across login/reboot."""

    def __init__(self, agent_dir: Path | None = None, run=subprocess.run) -> None:
        super().__init__(run)
        self.agent_dir = agent_dir or Path.home() / "Library/LaunchAgents"

    def arm_and_verify(self, operation_id: str, receipt: Path, deadline: dt.datetime) -> None:
        label = "com.context-garden.deadline." + operation_id
        path = self.agent_dir / f"{label}.plist"
        value = {"Label": label, "ProgramArguments": [sys.executable, "-m",
                 "garden.hosts.deadline_scheduler", "--execute", str(receipt)],
                 "RunAtLoad": True, "StartInterval": 60}
        _atomic(path, plistlib.dumps(value))
        domain = f"gui/{os.getuid()}"
        # bootstrap may report already loaded; bootout then bootstrap is deliberately not
        # used because that would create a gap in an already armed deadline.
        printed = self.run(["launchctl", "print", f"{domain}/{label}"], text=True,
                           capture_output=True, check=False, timeout=30)
        if printed.returncode:
            self._checked(["launchctl", "bootstrap", domain, str(path)])
        output = self._checked(["launchctl", "print", f"{domain}/{label}"])
        if (label not in output or str(receipt) not in output
                or "garden.hosts.deadline_scheduler" not in output
                or plistlib.loads(path.read_bytes()) != value):
            raise RuntimeError("launchd deadline schedule readback differs")


class LocalDeadlineScheduler:
    """Persist an immutable receipt and verify the platform scheduler before provisioning."""

    def __init__(self, state_dir: Path, aws_profile: str, aws_region: str,
                 aws_account_id: str,
                 *, manager: SchedulerManager | None = None) -> None:
        self.state_dir = state_dir.resolve()
        self.aws_profile, self.aws_region, self.aws_account_id = (
            aws_profile, aws_region, aws_account_id)
        if not aws_profile or not aws_region:
            raise ValueError("deadline scheduler requires an AWS profile and region")
        if not re.fullmatch(r"[0-9]{12}", aws_account_id):
            raise ValueError("deadline scheduler requires a 12-digit AWS account id")
        if manager is None:
            if sys.platform.startswith("linux"):
                manager = SystemdUserManager()
            elif sys.platform == "darwin":
                manager = LaunchdManager()
            else:
                raise RuntimeError("no supported local deadline scheduler; configure deadline_command")
        self.manager = manager

    def arm_and_verify(self, declaration: HostDeclaration) -> None:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", declaration.operation_id):
            raise ValueError("operation_id is not safe for an OS scheduler label")
        deadline = _deadline(declaration.deadline_utc)
        receipt = self.state_dir / f"{declaration.operation_id}.json"
        value = {"version": 1, "host_id": declaration.host_id,
                 "operation_id": declaration.operation_id, "deadline_utc": declaration.deadline_utc,
                 "owner": declaration.pool.owner, "pool": declaration.pool.name,
                 "aws_profile": self.aws_profile, "aws_region": self.aws_region,
                 "aws_account_id": self.aws_account_id}
        encoded = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name == "posix" and self.state_dir.stat().st_mode & 0o077:
            raise RuntimeError("deadline state directory must be mode 0700")
        with file_lock(self.state_dir / f".{declaration.operation_id}.lock"):
            if receipt.exists():
                info = receipt.lstat()
                if (not stat.S_ISREG(info.st_mode) or (os.name == "posix" and (
                        info.st_uid != os.geteuid() or info.st_mode & 0o077))):
                    raise RuntimeError("existing deadline receipt is not private and controller-owned")
                if receipt.read_bytes() != encoded:
                    raise RuntimeError(
                        "deadline receipt already binds this operation to different inputs")
            if not receipt.exists():
                _atomic(receipt, encoded, private_parent=True)
            self.manager.arm_and_verify(declaration.operation_id, receipt, deadline)


def execute(receipt_path: Path, *, session_factory=None,
            now=lambda: dt.datetime.now(dt.UTC), sleep=time.sleep) -> int:
    info = receipt_path.lstat()
    if not stat.S_ISREG(info.st_mode) or (os.name == "posix" and (
            info.st_uid != os.geteuid() or info.st_mode & 0o077)):
        raise RuntimeError("deadline receipt must be a controller-owned mode-0600 regular file")
    value = json.loads(receipt_path.read_text())
    deadline = _deadline(value["deadline_utc"])
    if now() < deadline:
        return 0
    if session_factory is None:
        import boto3
        session_factory = boto3.Session
    session = session_factory(profile_name=value["aws_profile"], region_name=value["aws_region"])
    identity = session.client("sts").get_caller_identity()
    arn = str(identity.get("Arn") or "")
    if (identity.get("Account") != value["aws_account_id"]
            or not re.fullmatch(
                rf"arn:[^:]+:sts::{re.escape(value['aws_account_id'])}:"
                rf"assumed-role/ContextGardenProvisioner/[^/]+", arn)):
        raise RuntimeError(
            "deadline executor requires the admitted account ContextGardenProvisioner role")
    client = session.client("ec2")
    expected = {OWNED_TAG: "true", OWNER_TAG: value["owner"], POOL_TAG: value["pool"],
                HOST_TAG: value["host_id"], OPERATION_TAG: value["operation_id"]}
    filters = [{"Name": f"tag:{key}", "Values": [item]} for key, item in expected.items()]
    filters.append({"Name": "instance-state-name",
                    "Values": ["pending", "running", "stopping", "stopped", "terminated"]})
    for attempt in range(3):
        try:
            response = client.describe_instances(Filters=filters)
            instances = [i for r in response.get("Reservations", []) for i in r.get("Instances", [])]
            for instance in instances:
                tags = {t["Key"]: t["Value"] for t in instance.get("Tags", [])}
                if any(tags.get(key) != item for key, item in expected.items()):
                    raise RuntimeError("refusing deadline action on mismatched instance tags")
            ids = [i["InstanceId"] for i in instances
                   if i.get("State", {}).get("Name") != "terminated"]
            if ids:
                client.terminate_instances(InstanceIds=ids)
                return 1  # systemd/launchd retries until termination is observed
            owned_filters = [
                {"Name": f"tag:{OPERATION_TAG}", "Values": [value["operation_id"]]},
                {"Name": f"tag:{OWNED_TAG}", "Values": ["true"]},
            ]
            resources = [
                *client.describe_volumes(Filters=owned_filters).get("Volumes", []),
                *client.describe_network_interfaces(Filters=owned_filters).get(
                    "NetworkInterfaces", []),
                *client.describe_addresses(Filters=owned_filters).get("Addresses", []),
            ]
            for resource in resources:
                tags = {t["Key"]: t["Value"] for t in resource.get("Tags", [])}
                if (tags.get(OPERATION_TAG) != value["operation_id"]
                        or tags.get(OWNED_TAG) != "true"):
                    raise RuntimeError("refusing retained-resource check with mismatched tags")
            retained = [r.get("VolumeId") or r.get("NetworkInterfaceId")
                        or r.get("AllocationId") or r.get("PublicIp") for r in resources
                        if r.get("State") != "deleted"]
            return 1 if any(retained) else 0
        except (ConnectionError, TimeoutError):
            if attempt == 2:
                raise
            sleep(1)
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", type=Path, required=True)
    args = parser.parse_args(argv)
    return execute(args.execute)


if __name__ == "__main__":
    raise SystemExit(main())
