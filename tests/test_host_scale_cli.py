from __future__ import annotations

import datetime as dt
import json
import sys
from dataclasses import asdict, replace
from types import SimpleNamespace

from typer.testing import CliRunner

from garden.cli import app
from garden.hosts import ScaleStatus


def test_single_scale_cli_reports_bounded_resumable_operation(tmp_path, monkeypatch):
    specification = tmp_path / "pool.json"
    specification.write_text(json.dumps({
        "contract_version": "garden.hosts/v1", "name": "workers", "owner": "team",
        "purpose": "ci", "provider": "ec2", "enabled": True, "desired": 2, "maximum": 4,
        "spend_limit_usd": 80, "profile": {"name": "worker", "version": "source-sha",
        "image": "ami-pinned", "bootstrap_version": "bootstrap-sha", "cpu": 4,
        "memory_mib": 16384, "disk_gib": 40, "endpoint": "https://garden.test",
        "enrollment_secret_ref": "arn:secret:{host_id}"},
        "provider_options": {"instance_type": "m6i.xlarge", "subnet_id": "subnet",
        "security_group_ids": ["sg"], "instance_profile_arn": "arn:role",
        "hourly_usd": 0.25, "bootstrap_path": "/opt/bootstrap"}}))
    calls = []

    class Operation:
        def request(self, pool, *, deadline, aggregate_spend_limit_usd):
            calls.append((pool.desired, deadline.isoformat(), aggregate_spend_limit_usd))
            return ScaleStatus("op", 2, 1, 0, 0, "ami-pinned/source-sha/bootstrap-sha",
                               deadline.isoformat(), 1.0, 80, 80, 4, 4, 16384, 40, (),
                               {"workers-1": ("dedicated model identity",)}, (), (),
                               "provider billing can arrive after teardown")

    monkeypatch.setattr("garden.cli.hosts._build_operation", lambda *args: Operation())
    result = CliRunner().invoke(app, ["hosts", "scale", str(specification), "--deadline",
                                             "2026-09-09T00:00:00Z"])

    assert result.exit_code == 0, result.output
    output = json.loads(result.output)
    assert output["desired"] == 2 and output["healthy"] == 1
    assert output["missing_setup"] == {"workers-1": ["dedicated model identity"]}
    assert output["phase"] == "unknown"
    assert calls == [(2, "2026-09-09T00:00:00+00:00", 80.0)]


def test_production_cli_reuses_saved_setup_and_stops_at_missing_identity(tmp_path, monkeypatch):
    from tests.test_hosts import StubEC2, pool, profile

    calls = []
    ec2 = StubEC2()
    ec2_clients = []

    class Session:
        role = "ContextGardenProvisioner"

        def __init__(self, **kwargs):
            calls.append(kwargs)

        def client(self, name):
            if name == "sts":
                return SimpleNamespace(get_caller_identity=lambda: {
                    "Account": "123456789012",
                    "Arn": f"arn:aws:sts::123456789012:assumed-role/{Session.role}/test"})
            assert name == "ec2"
            ec2_clients.append(name)
            return ec2

    monkeypatch.setitem(sys.modules, "boto3", SimpleNamespace(Session=Session))
    monkeypatch.setattr("garden.hosts.enrollment_clients.clients_from_config",
                        lambda config: (object(), object(), object()))
    spec = pool(enabled=True, desired=1, provider="ec2", spend_limit_usd=80,
                profile=replace(profile(), source_head="a" * 40,
                    enrollment_secret_ref="context-garden/phase05/renew-test-{host_id}"),
                provider_options={"hourly_usd": 0.25, "bootstrap_sha256": "b" * 64})
    specification = tmp_path / "pool.json"
    specification.write_text(json.dumps({"contract_version": "garden.hosts/v1", **asdict(spec)}))
    config = tmp_path / "production.json"
    config.write_text(json.dumps({"aws_profile": "scoped-profile", "aws_region": "us-east-1",
                                  "aws_account_id": "123456789012", "github_repo": "owner/repo"}))
    state = tmp_path / "operations/scale.json"
    enrollment = tmp_path / "private/enrollment"
    deadline = (dt.datetime.now(dt.UTC) + dt.timedelta(hours=1)).isoformat()
    result = CliRunner().invoke(app, ["hosts", "scale", str(specification), "--state", str(state),
        "--deadline", deadline, "--enrollment-config", str(config), "--enrollment-dir", str(enrollment)])
    assert result.exit_code == 0, result.output
    saved = json.loads(state.read_text())
    assert saved["enrollment_config_path"] == str(config.resolve())
    assert saved["enrollment_dir"] == str(enrollment.resolve())
    result = CliRunner().invoke(app, ["hosts", "scale", str(specification), "--state", str(state),
                                     "--continue"])
    assert result.exit_code == 0, result.output
    assert "workers-0" in json.loads(result.output)["missing_setup"]
    assert ec2.run_args is None
    assert all(item == {"profile_name": "scoped-profile", "region_name": "us-east-1"}
               for item in calls)
    count_ec2 = len(ec2_clients)
    Session.role = "Administrator"
    refused = CliRunner().invoke(app, ["hosts", "scale", str(specification), "--state", str(state),
                                      "--continue"])
    assert refused.exit_code == 1 and "scoped ContextGardenProvisioner" in refused.output
    assert len(ec2_clients) == count_ec2
    Session.role = "ContextGardenProvisioner"
    count = len(calls)
    changed = json.loads(config.read_text())
    changed["aws_region"] = "eu-west-1"
    config.write_text(json.dumps(changed))
    refused = CliRunner().invoke(app, ["hosts", "scale", str(specification), "--state", str(state),
                                      "--continue"])
    assert refused.exit_code == 1 and "context changed" in refused.output
    assert len(calls) == count  # scope rejection precedes any provider client creation


def test_spot_cli_requires_durable_event_configuration_before_aws_session(tmp_path, monkeypatch):
    from tests.test_hosts import pool

    class Boto3:
        @staticmethod
        def Session(**kwargs):
            raise AssertionError("Spot safety validation must precede AWS access")

    monkeypatch.setitem(sys.modules, "boto3", Boto3)
    specification = tmp_path / "spot-pool.json"
    specification.write_text(json.dumps({
        "contract_version": "garden.hosts/v1",
        **asdict(pool(
            provider="ec2",
            purchase_policy="spot",
            provider_options={"spot_hourly_usd": 0.06, "hourly_usd": 0.20},
        )),
    }))

    result = CliRunner().invoke(app, ["hosts", "scale", str(specification)])

    assert result.exit_code == 1
    assert "--enrollment-config with spot_event_queue_url" in result.output


def test_spot_cli_rejects_invalid_maximum_price_before_aws_access(tmp_path, monkeypatch):
    from tests.test_hosts import pool

    class Boto3:
        @staticmethod
        def Session(**kwargs):
            raise AssertionError("Spot price validation must precede AWS access")

    monkeypatch.setitem(sys.modules, "boto3", Boto3)
    spec = replace(
        pool(provider="ec2", purchase_policy="spot"),
        provider_options={"spot_hourly_usd": 0.06, "spot_max_price_usd": float("inf")},
    )
    specification = tmp_path / "spot-pool.json"
    specification.write_text(json.dumps({"contract_version": "garden.hosts/v1", **asdict(spec)}))

    result = CliRunner().invoke(app, ["hosts", "scale", str(specification)])

    assert result.exit_code == 1
    assert "spot_max_price_usd must be a positive finite price" in result.output


def test_spot_cli_rejects_unpriced_implicit_ceiling_before_aws_access(tmp_path, monkeypatch):
    from tests.test_hosts import pool

    class Boto3:
        @staticmethod
        def Session(**kwargs):
            raise AssertionError("Spot price validation must precede AWS access")

    monkeypatch.setitem(sys.modules, "boto3", Boto3)
    spec = replace(
        pool(provider="ec2", purchase_policy="spot"),
        provider_options={"spot_hourly_usd": 0.06},
    )
    specification = tmp_path / "spot-pool.json"
    specification.write_text(json.dumps({"contract_version": "garden.hosts/v1", **asdict(spec)}))

    result = CliRunner().invoke(app, ["hosts", "scale", str(specification)])

    assert result.exit_code == 1
    assert "hourly_usd is required to price the implicit Spot ceiling" in result.output


def test_provider_error_does_not_render_secret_bearing_locals(tmp_path, monkeypatch):
    from tests.test_hosts import pool

    specification = tmp_path / "pool.json"
    specification.write_text(json.dumps({"contract_version": "garden.hosts/v1", **asdict(pool())}))

    class ProviderFailure(Exception):
        pass

    def build(*args):
        raise ProviderFailure("secret-envelope-that-must-not-be-rendered")

    monkeypatch.setattr("garden.cli.hosts._build_operation", build)
    result = CliRunner().invoke(app, ["hosts", "scale", str(specification)])
    assert result.exit_code == 1
    assert "ProviderFailure" in result.output
    assert "secret-envelope" not in result.output
