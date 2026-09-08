from __future__ import annotations

import json

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
                               {"workers-1": ("dedicated model identity",)}, (),
                               "provider billing can arrive after teardown")

    monkeypatch.setattr("garden.cli.hosts._build_operation", lambda *args: Operation())
    result = CliRunner().invoke(app, ["hosts", "scale", str(specification), "--deadline",
                                             "2026-09-09T00:00:00Z"])

    assert result.exit_code == 0, result.output
    output = json.loads(result.output)
    assert output["desired"] == 2 and output["healthy"] == 1
    assert output["missing_setup"] == {"workers-1": ["dedicated model identity"]}
    assert calls == [(2, "2026-09-09T00:00:00+00:00", 80.0)]
