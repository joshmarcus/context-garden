from __future__ import annotations

import datetime as dt
import json
from dataclasses import replace

import pytest

from garden.hosts import HostLifecycle, HostState, JsonStateStore, ScaleOperation
from garden.hosts.fake import FakeProvider
from tests.test_hosts import Enrollments, StubEC2, pool, profile, ready_enrollment


class CleanEnrollments(Enrollments):
    def revoke(self, host_id):
        self.revoked.append(host_id)
        return ()


def operation(tmp_path, *, hourly=1, name="workers", now=None, enrollments=None, provider=None):
    now = now or dt.datetime(2026, 9, 10, tzinfo=dt.UTC)
    provider = provider or FakeProvider(hourly_usd=hourly)
    lifecycle = HostLifecycle({"fake": provider}, JsonStateStore(tmp_path / f"{name}-life.json"))
    return ScaleOperation(lifecycle, tmp_path / f"{name}-scale.json",
                          enrollments or CleanEnrollments(), now=lambda: now)


def requested(**changes):
    return pool(enabled=True, desired=1, spend_limit_usd=80,
                profile=replace(profile(), enrollment_secret_ref="secret/{host_id}"), **changes)


def test_admission_covers_full_deadline_not_short_runtime_estimate(tmp_path):
    op = operation(tmp_path)
    now = op.now()
    with pytest.raises(ValueError, match="pool spend limit"):
        op.request(replace(requested(), spend_limit_usd=5),
                   deadline=now + dt.timedelta(hours=24))
    assert not op.state_path.exists()
    status = op.request(requested(), deadline=now + dt.timedelta(hours=24))
    assert status.estimated_accrued_usd == 24
    assert op.lifecycle.absolute_deadline == (now + dt.timedelta(hours=24)).isoformat()
    assert op.lifecycle._declaration(requested(), 0).deadline_utc == op.lifecycle.absolute_deadline


def test_new_operation_cannot_relax_previously_admitted_aggregate_cap(tmp_path):
    first = operation(tmp_path, name="first", hourly=1)
    first.request(replace(requested(), name="first"),
                  deadline=first.now() + dt.timedelta(hours=1), aggregate_spend_limit_usd=1.5)
    second = operation(tmp_path, name="second", hourly=1)
    with pytest.raises(ValueError, match="aggregate"):
        second.request(replace(requested(), name="second"),
                       deadline=second.now() + dt.timedelta(hours=1), aggregate_spend_limit_usd=80)


def test_cleanup_and_duplicate_request_keep_admitted_cost(tmp_path):
    op = operation(tmp_path)
    spec = requested()
    deadline = op.now() + dt.timedelta(hours=2)
    op.request(spec, deadline=deadline)
    op.cleanup(spec)
    saved = json.loads(op.state_path.read_text())
    assert saved["phase"] == "cleaned" and saved["estimated_accrued_usd"] == 2
    assert op.request(spec, deadline=deadline).desired == 0
    assert json.loads(op.state_path.read_text()) == saved


def test_noncontiguous_enrollment_can_progress_without_unenrolled_launch(tmp_path):
    provider = FakeProvider()
    enrollments = Enrollments({"workers-1": ready_enrollment("workers-1")})
    op = operation(tmp_path, provider=provider, enrollments=enrollments)
    spec = replace(requested(), desired=2, maximum=2)
    op.request(spec, deadline=op.now() + dt.timedelta(hours=1))
    status = op.continue_(spec)
    assert [h.host_id for h in status.hosts] == ["workers-1"]
    assert status.healthy == 1 and provider.provision_calls == 1
    assert op.continue_(spec).healthy == 1 and provider.provision_calls == 1
    enrollments.values["workers-0"] = ready_enrollment("workers-0")
    assert op.continue_(spec).healthy == 2 and provider.provision_calls == 2


def test_status_rechecks_durable_readiness_without_provider_mutation(tmp_path):
    provider = FakeProvider()
    op = operation(tmp_path, provider=provider,
                   enrollments=Enrollments({"workers-0": ready_enrollment()}))
    spec = requested()
    op.request(spec, deadline=op.now() + dt.timedelta(hours=1))
    op.continue_(spec)
    op.lifecycle.health_check = lambda host, spec: (None, "awaiting durable result")
    status = op.status(spec)
    assert status.healthy == 0 and status.hosts[0].state == HostState.BOOTSTRAPPING
    op.lifecycle.health_check = lambda host, spec: (True, "verified durable result")
    assert op.status(spec).healthy == 1
    assert provider.provision_calls == 1 and provider.destroy_calls == []


def test_next_admission_uses_a_new_host_generation_after_cleanup(tmp_path):
    first = operation(tmp_path)
    spec = requested()
    first.request(spec, deadline=first.now() + dt.timedelta(hours=1))
    first_identity = first.lifecycle._declaration(spec, 0).operation_id
    first.cleanup(spec)
    second = ScaleOperation(first.lifecycle, tmp_path / "renewed-scale.json", Enrollments(),
                            now=first.now)
    second.request(spec, deadline=first.now() + dt.timedelta(hours=2))
    assert second.lifecycle._declaration(spec, 0).operation_id != first_identity


def test_overlapping_admissions_cannot_reuse_one_pool(tmp_path):
    first = operation(tmp_path)
    spec = requested()
    first.request(spec, deadline=first.now() + dt.timedelta(hours=1))
    second = ScaleOperation(first.lifecycle, tmp_path / "overlap-scale.json", Enrollments(),
                            now=first.now)
    with pytest.raises(ValueError, match="active scale operation"):
        second.request(spec, deadline=first.now() + dt.timedelta(hours=2))


def test_scale_admission_deadline_and_generation_reach_ec2_before_launch(tmp_path):
    from garden.hosts.ec2 import OPERATION_TAG, EC2Provider

    calls = []

    class Enforcer:
        def arm_and_verify(self, declaration):
            calls.append(declaration)

    client = StubEC2()
    provider = EC2Provider(client, deadline_enforcer=Enforcer())
    lifecycle = HostLifecycle({"ec2": provider}, JsonStateStore(tmp_path / "life.json"))
    now = dt.datetime(2029, 1, 1, tzinfo=dt.UTC)
    op = ScaleOperation(lifecycle, tmp_path / "scale.json",
                        Enrollments({"workers-0": ready_enrollment()}), now=lambda: now)
    spec = pool(enabled=True, desired=1, provider="ec2", spend_limit_usd=10,
                profile=replace(profile(), source_head="a" * 40,
                                enrollment_secret_ref="secret/{host_id}"),
                provider_options={"instance_type": "m6i.xlarge", "subnet_id": "subnet-test",
                    "security_group_ids": ["sg-test"], "instance_profile_arn": "arn:scoped-role",
                    "hourly_usd": 1, "bootstrap_path": "/opt/garden/bootstrap",
                    "bootstrap_url": "https://example.test/bootstrap", "bootstrap_sha256": "b" * 64})
    deadline = now + dt.timedelta(hours=1)
    op.request(spec, deadline=deadline)
    op.continue_(spec)
    assert len(calls) == 1 and calls[0].deadline_utc == deadline.isoformat()
    tags = {item["Key"]: item["Value"] for item in client.run_args["TagSpecifications"][0]["Tags"]}
    assert tags[OPERATION_TAG] == calls[0].operation_id == client.run_args["ClientToken"]
    assert deadline.isoformat() in client.run_args["UserData"]
    assert client.run_args["InstanceInitiatedShutdownBehavior"] == "terminate"
