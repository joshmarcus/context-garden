import datetime as dt
import json
from dataclasses import replace

import pytest

from garden.hosts import EnvironmentProfile, HostDeclaration, PoolDeclaration
from garden.hosts.deadline_scheduler import (
    LaunchdManager,
    LocalDeadlineScheduler,
    SystemdUserManager,
    execute,
)


def declaration(deadline="2030-01-01T00:00:00+00:00"):
    profile = EnvironmentProfile("worker", "v1", "ami-1", "boot-1", 3, 12288, 40)
    pool = PoolDeclaration("workers", "owner", "ci", "ec2", profile)
    return HostDeclaration("workers-0", "op-1", pool, deadline)


def test_scheduler_persists_immutable_receipt_before_manager(tmp_path):
    calls = []

    class Manager:
        def arm_and_verify(self, operation_id, receipt, deadline):
            calls.append((operation_id, json.loads(receipt.read_text()), deadline))

    scheduler = LocalDeadlineScheduler(
        tmp_path, "profile", "us-east-1", "350111791226", manager=Manager())
    scheduler.arm_and_verify(declaration())
    assert calls[0][0] == "op-1"
    assert calls[0][1]["deadline_utc"] == "2030-01-01T00:00:00+00:00"
    assert calls[0][2].tzinfo is not None
    with pytest.raises(RuntimeError, match="different inputs"):
        scheduler.arm_and_verify(replace(declaration(), host_id="workers-1"))


def test_systemd_manager_verifies_lingering_and_effective_units(tmp_path):
    receipt = tmp_path / "receipt.json"
    receipt.write_text("{}")
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        output = ""
        if args[0] == "loginctl":
            output = "yes\n"
        elif "TimersCalendar" in args[-1]:
            output = "TimersCalendar={ OnCalendar=2030-01-01 00:00:00 UTC ; }"
        elif "ExecStart" in args[-1]:
            output = f"ExecStart=python -m garden.hosts.deadline_scheduler --execute {receipt}"
        return type("Completed", (), {"returncode": 0, "stdout": output})()

    SystemdUserManager(tmp_path / "units", run=run).arm_and_verify(
        "op-1", receipt, dt.datetime(2030, 1, 1, tzinfo=dt.UTC))
    assert any(call[:3] == ["systemctl", "--user", "enable"] for call in calls)
    assert "Persistent=true" in (tmp_path / "units/garden-deadline-op-1.timer").read_text()


def test_launchd_manager_bootstraps_and_reads_loaded_definition(tmp_path):
    receipt = tmp_path / "receipt.json"
    receipt.write_text("{}")
    count = 0

    def run(args, **kwargs):
        nonlocal count
        if args[:2] == ["launchctl", "print"]:
            count += 1
            output = (f"com.context-garden.deadline.op-1 garden.hosts.deadline_scheduler "
                      f"{receipt}" if count > 1 else "")
            return type("Completed", (), {"returncode": 0 if count > 1 else 1,
                                            "stdout": output})()
        return type("Completed", (), {"returncode": 0, "stdout": ""})()

    LaunchdManager(tmp_path / "agents", run=run).arm_and_verify(
        "op-1", receipt, dt.datetime(2030, 1, 1, tzinfo=dt.UTC))
    assert count == 2


def test_execute_is_scoped_and_terminates_only_after_deadline(tmp_path):
    receipt = tmp_path / "op.json"
    receipt.write_text(json.dumps({"deadline_utc": "2030-01-01T00:00:00+00:00",
        "aws_profile": "profile", "aws_region": "us-east-1", "owner": "owner",
        "aws_account_id": "350111791226",
        "pool": "workers", "host_id": "workers-0", "operation_id": "op-1"}))
    receipt.chmod(0o600)
    calls = []

    class Client:
        state = "running"

        def describe_instances(self, **kwargs):
            calls.append(kwargs)
            tags = {"context-garden:managed": "true", "context-garden:owner": "owner",
                    "context-garden:pool": "workers", "context-garden:host-id": "workers-0",
                    "context-garden:operation-id": "op-1"}
            return {"Reservations": [{"Instances": [{"InstanceId": "i-1",
                "State": {"Name": self.state},
                "Tags": [{"Key": k, "Value": v} for k, v in tags.items()]}]}]}
        def terminate_instances(self, **kwargs):
            calls.append(kwargs)
            self.state = "terminated"
        def describe_volumes(self, **kwargs):
            return {"Volumes": []}
        def describe_network_interfaces(self, **kwargs):
            return {"NetworkInterfaces": []}
        def describe_addresses(self, **kwargs):
            return {"Addresses": []}

    client = Client()

    class STS:
        def get_caller_identity(self):
            return {"Account": "350111791226", "Arn":
                    "arn:aws:sts::350111791226:assumed-role/ContextGardenProvisioner/test"}

    class Session:
        def client(self, name):
            return STS() if name == "sts" else client

    def factory(**kwargs):
        return Session()
    assert execute(receipt, session_factory=factory,
                   now=lambda: dt.datetime(2029, 1, 1, tzinfo=dt.UTC)) == 0
    assert calls == []
    assert execute(receipt, session_factory=factory,
                   now=lambda: dt.datetime(2030, 1, 1, tzinfo=dt.UTC)) == 1
    assert calls[-1] == {"InstanceIds": ["i-1"]}
    names = {row["Name"] for row in calls[0]["Filters"]}
    assert "tag:context-garden:operation-id" in names
    assert "tag:context-garden:host-id" in names
    assert execute(receipt, session_factory=factory,
                   now=lambda: dt.datetime(2030, 1, 1, tzinfo=dt.UTC)) == 0


@pytest.mark.parametrize("account,arn", [
    ("350111791226", "arn:aws:sts::350111791226:assumed-role/Administrator/session"),
    ("999999999999", "arn:aws:sts::999999999999:assumed-role/ContextGardenProvisioner/session"),
    ("350111791226", "arn:aws:iam::350111791226:root"),
])
def test_execute_rejects_wrong_identity_before_ec2(tmp_path, account, arn):
    receipt = tmp_path / "op.json"
    receipt.write_text(json.dumps({"deadline_utc": "2030-01-01T00:00:00+00:00",
        "aws_profile": "profile", "aws_region": "us-east-1",
        "aws_account_id": "350111791226", "owner": "owner", "pool": "workers",
        "host_id": "workers-0", "operation_id": "op-1"}))
    receipt.chmod(0o600)
    clients = []

    class STS:
        def get_caller_identity(self):
            return {"Account": account, "Arn": arn}

    class Session:
        def client(self, name):
            clients.append(name)
            assert name == "sts"
            return STS()

    with pytest.raises(RuntimeError, match="ContextGardenProvisioner"):
        execute(receipt, session_factory=lambda **kwargs: Session(),
                now=lambda: dt.datetime(2030, 1, 1, tzinfo=dt.UTC))
    assert clients == ["sts"]
