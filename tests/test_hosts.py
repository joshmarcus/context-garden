from __future__ import annotations

import datetime as dt
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from garden.hosts import (
    Enrollment,
    EnvironmentProfile,
    HostLifecycle,
    HostState,
    JsonStateStore,
    PoolDeclaration,
    ScaleOperation,
    durable_worker_readiness,
    pool_from_dict,
)
from garden.hosts.ec2 import OPERATION_TAG, OWNER_TAG, POOL_TAG, EC2Provider
from garden.hosts.fake import FakeProvider


def profile(*, persistent: bool = False) -> EnvironmentProfile:
    return EnvironmentProfile(
        name="garden-worker" if not persistent else "remote-dev",
        version="1.0.0",
        image="ami-pinned123",
        bootstrap_version="0.1.0+abcdef",
        cpu=4,
        memory_mib=16384,
        disk_gib=40,
        endpoint="https://garden.example.test",
        enrollment_secret_ref="arn:aws:secretsmanager:region:account:secret:worker",
        persistent_workspace=persistent,
    )


def pool(**changes) -> PoolDeclaration:
    value = PoolDeclaration(
        name="workers",
        owner="team-a",
        purpose="ci-worker",
        provider="fake",
        profile=profile(),
        provider_options={"fixture_label": "contract"},
    )
    return replace(value, **changes)


def test_plan_is_non_mutating_and_defaults_to_zero_with_a_one_host_maximum(tmp_path):
    provider = FakeProvider(hourly_usd=0.42)
    lifecycle = HostLifecycle({"fake": provider}, JsonStateStore(tmp_path / "state.json"))

    plan = lifecycle.plan(pool())

    assert (plan.enabled, plan.current, plan.desired, plan.create) == (False, 0, 0, 0)
    assert pool().maximum == 1
    assert provider.provision_calls == 0
    with pytest.raises(ValueError, match="disabled"):
        lifecycle.reconcile(pool(desired=1))
    with pytest.raises(ValueError, match="exceeds pool spend limit"):
        lifecycle.reconcile(
            pool(enabled=True, desired=1, estimated_runtime_hours=100, spend_limit_usd=1)
        )


def test_duplicate_delayed_request_and_controller_restart_do_not_duplicate_host(tmp_path):
    provider = FakeProvider()
    provider.delay_next_response = True
    state_path = tmp_path / "state.json"
    enabled = pool(enabled=True, desired=1)

    assert (
        HostLifecycle({"fake": provider}, JsonStateStore(state_path)).reconcile(enabled)[0].state
        == HostState.READY
    )
    restarted = HostLifecycle({"fake": provider}, JsonStateStore(state_path))
    hosts = restarted.reconcile(enabled)

    assert len(hosts) == 1
    assert provider.provision_calls == 1
    assert "provisioning_uncertain" in state_path.read_text()


def test_reconcile_replaces_a_missing_lower_slot_without_reusing_an_occupied_operation(tmp_path):
    provider = FakeProvider()
    lifecycle = HostLifecycle({"fake": provider}, JsonStateStore(tmp_path / "state.json"))
    enabled = pool(enabled=True, desired=2, maximum=2)
    initial = lifecycle.reconcile(enabled)
    slot_one = next(host for host in initial if host.host_id == "workers-1")
    slot_zero = next(host for host in initial if host.host_id == "workers-0")
    del provider.hosts[slot_zero.provider_id]
    del provider.ownership[slot_zero.provider_id]

    reconciled = lifecycle.reconcile(enabled)

    assert {host.host_id for host in reconciled} == {"workers-0", "workers-1"}
    assert len({host.operation_id for host in reconciled}) == 2
    assert slot_one.operation_id in {host.operation_id for host in reconciled}
    assert slot_zero.operation_id not in {host.operation_id for host in reconciled}
    assert provider.provision_calls == 3
    assert "host_lost" in (tmp_path / "state.json").read_text()


def test_bootstrap_failure_is_explicit_and_cleans_up_owned_host(tmp_path):
    provider = FakeProvider()
    lifecycle = HostLifecycle(
        {"fake": provider},
        JsonStateStore(tmp_path / "state.json"),
        health_check=lambda _host, _pool: (False, "HTTPS enrollment did not register"),
    )

    hosts = lifecycle.reconcile(pool(enabled=True, desired=1))

    assert hosts[0].state == HostState.TERMINATED
    assert provider.destroy_calls == [("fake-1", True)]
    assert "bootstrap_failed" in (tmp_path / "state.json").read_text()


def test_retirement_retains_a_dev_workspace_and_reports_it(tmp_path):
    provider = FakeProvider()
    lifecycle = HostLifecycle({"fake": provider}, JsonStateStore(tmp_path / "state.json"))
    dev = pool(enabled=True, desired=1, purpose="development", profile=profile(persistent=True))
    assert lifecycle.reconcile(dev)[0].state == HostState.READY

    restarted = HostLifecycle({"fake": provider}, JsonStateStore(tmp_path / "state.json"))
    retired = restarted.reconcile(replace(dev, desired=0))

    assert retired[0].state == HostState.TERMINATED
    assert retired[0].retained_resources == ("disk:fake-1",)
    assert provider.destroy_calls[-1] == ("fake-1", False)
    with pytest.raises(ValueError, match="explicit release"):
        lifecycle.destroy(dev, "fake-1", delete_storage=True)


class StubEC2:
    def __init__(self):
        self.instances = []
        self.run_args = None

    def describe_instances(self, **kwargs):
        if "InstanceIds" in kwargs:
            selected = [i for i in self.instances if i["InstanceId"] in kwargs["InstanceIds"]]
        else:
            filters = {
                f["Name"].removeprefix("tag:"): f["Values"][0]
                for f in kwargs["Filters"]
                if f["Name"].startswith("tag:")
            }
            selected = [
                i
                for i in self.instances
                if filters.items() <= {t["Key"]: t["Value"] for t in i["Tags"]}.items()
            ]
        return {"Reservations": [{"Instances": selected}] if selected else []}

    def run_instances(self, **kwargs):
        self.run_args = kwargs
        instance = {
            "InstanceId": "i-owned",
            "ImageId": kwargs["ImageId"],
            "State": {"Name": "running"},
            "Tags": kwargs["TagSpecifications"][0]["Tags"],
        }
        self.instances.append(instance)
        return {"Instances": [instance]}

    def describe_addresses(self, **kwargs):
        return {"Addresses": []}

    def terminate_instances(self, **kwargs):
        for instance in self.instances:
            if instance["InstanceId"] in kwargs["InstanceIds"]:
                instance["State"] = {"Name": "terminated"}
        return {}

    def stop_instances(self, **kwargs):
        return {}

    def start_instances(self, **kwargs):
        return {}

    def describe_instance_status(self, **kwargs):
        return {"InstanceStatuses": []}


class NoSpotCapacity(RuntimeError):
    response = {"Error": {"Code": "InsufficientInstanceCapacity"}}


def test_ec2_adapter_scopes_discovery_tags_credentials_and_bootstrap(tmp_path):
    client = StubEC2()
    client.instances.append(
        {
            "InstanceId": "i-unrelated",
            "ImageId": "ami-other",
            "State": {"Name": "running"},
            "Tags": [
                {"Key": OWNER_TAG, "Value": "somebody-else"},
                {"Key": POOL_TAG, "Value": "workers"},
            ],
        }
    )
    provider = EC2Provider(client)
    declaration = replace(
        pool(
            enabled=True,
            desired=1,
            provider="ec2",
            provider_options={
                "instance_type": "m6i.xlarge",
                "subnet_id": "subnet-private",
                "security_group_ids": ["sg-egress-only"],
                "instance_profile_arn": "arn:scoped-role",
                "hourly_usd": 0.25,
                "bootstrap_path": "/opt/company/bootstrap-v1",
            },
        ),
    )
    lifecycle = HostLifecycle({"ec2": provider}, JsonStateStore(tmp_path / "state.json"))

    hosts = lifecycle.reconcile(declaration)

    assert [h.provider_id for h in hosts] == ["i-owned"]
    args = client.run_args
    assert args["ClientToken"] == next(
        t["Value"] for t in args["TagSpecifications"][0]["Tags"] if t["Key"] == OPERATION_TAG
    )
    assert args["MetadataOptions"] == {
        "HttpTokens": "required",
        "HttpEndpoint": "enabled",
        "HttpPutResponseHopLimit": 1,
    }
    assert args["BlockDeviceMappings"][0]["Ebs"]["Encrypted"] is True
    assert "secret:worker" in args["UserData"]
    assert "/opt/company/bootstrap-v1 --config" in args["UserData"]
    assert "/api/workers/enroll" not in args["UserData"]
    assert "controller" not in args["UserData"]
    assert lifecycle.plan(replace(declaration, desired=0)).retire == ("workers-0",)
    lifecycle.reconcile(replace(declaration, desired=0))
    unrelated = next(i for i in client.instances if i["InstanceId"] == "i-unrelated")
    assert unrelated["State"]["Name"] == "running"


def test_spot_policy_is_capability_checked_priced_and_explicit(tmp_path):
    client = StubEC2()
    spec = replace(
        pool(provider="ec2", enabled=True, desired=1, purchase_policy="spot",
             profile=replace(profile(), endpoint="", enrollment_secret_ref="")),
        provider_options={
            "instance_type": "m6i.xlarge", "subnet_id": "subnet-test",
            "security_group_ids": ["sg-test"], "instance_profile_arn": "arn:role",
            "spot_hourly_usd": 0.06, "spot_max_price_usd": 0.08,
        },
    )
    lifecycle = HostLifecycle({"ec2": EC2Provider(client)}, JsonStateStore(tmp_path / "state.json"))

    assert lifecycle.plan(spec).estimated_hourly_usd == 0.06
    lifecycle.reconcile(spec)
    assert client.run_args["InstanceMarketOptions"]["MarketType"] == "spot"
    assert client.run_args["InstanceMarketOptions"]["SpotOptions"]["MaxPrice"] == "0.08"

    with pytest.raises(ValueError, match="recoverable_workspace"):
        lifecycle.plan(replace(spec, profile=profile(persistent=True)))
    with pytest.raises(ValueError, match="hourly_usd"):
        lifecycle.plan(replace(spec, on_demand_fallback=True))
    with pytest.raises(ValueError, match="positive finite price"):
        lifecycle.plan(replace(spec, provider_options={**spec.provider_options, "spot_hourly_usd": float("nan")}))


def test_spot_shortage_fallback_is_opt_in_and_uses_bounded_plan_price(tmp_path):
    class Shortage(StubEC2):
        def __init__(self):
            super().__init__()
            self.calls = []

        def run_instances(self, **kwargs):
            self.calls.append(kwargs)
            if "InstanceMarketOptions" in kwargs:
                raise NoSpotCapacity("none")
            return super().run_instances(**kwargs)

    options = {
        "instance_type": "m6i.xlarge", "subnet_id": "subnet-test",
        "security_group_ids": ["sg-test"], "instance_profile_arn": "arn:role",
        "spot_hourly_usd": 0.06, "hourly_usd": 0.20,
    }
    client = Shortage()
    spec = replace(pool(provider="ec2", enabled=True, desired=1, purchase_policy="spot",
                        profile=replace(profile(), endpoint="", enrollment_secret_ref="")),
                   provider_options=options)
    lifecycle = HostLifecycle({"ec2": EC2Provider(client)}, JsonStateStore(tmp_path / "state.json"))
    with pytest.raises(RuntimeError, match="fallback is disabled"):
        lifecycle.provision(spec)

    fallback = replace(spec, on_demand_fallback=True)
    assert lifecycle.plan(fallback).estimated_hourly_usd == 0.20
    lifecycle.reconcile(fallback)
    assert len(client.calls) == 3
    assert "InstanceMarketOptions" not in client.calls[-1]
    assert client.calls[-1]["ClientToken"].endswith("-ondemand")


def test_on_demand_capacity_error_is_not_reported_as_spot_shortage(tmp_path):
    class Shortage(StubEC2):
        def run_instances(self, **kwargs):
            raise NoSpotCapacity("none")

    spec = replace(
        pool(
            provider="ec2",
            enabled=True,
            desired=1,
            profile=replace(profile(), endpoint="", enrollment_secret_ref=""),
        ),
        provider_options={
            "instance_type": "m6i.xlarge",
            "subnet_id": "subnet-test",
            "security_group_ids": ["sg-test"],
            "instance_profile_arn": "arn:role",
            "hourly_usd": 0.20,
        },
    )
    lifecycle = HostLifecycle(
        {"ec2": EC2Provider(Shortage())}, JsonStateStore(tmp_path / "state.json")
    )

    with pytest.raises(RuntimeError, match="EC2 launch failed"):
        lifecycle.reconcile(spec)


def test_interrupted_spot_host_is_retired_and_replaced_once_across_reconciliation(tmp_path):
    client = StubEC2()

    class Events:
        pending = []

        def pending_events(self, owner, pool):
            assert (owner, pool) == ("team-a", "workers")
            return self.pending

    events = Events()
    spec = replace(
        pool(provider="ec2", enabled=True, desired=1, purchase_policy="spot",
             profile=replace(profile(), endpoint="", enrollment_secret_ref="")),
        provider_options={
            "instance_type": "m6i.xlarge", "subnet_id": "subnet-test",
            "security_group_ids": ["sg-test"], "instance_profile_arn": "arn:role",
            "spot_hourly_usd": 0.06,
        },
    )
    state = JsonStateStore(tmp_path / "state.json")
    lifecycle = HostLifecycle({"ec2": EC2Provider(client, event_source=events)}, state)
    original = lifecycle.reconcile(spec)[0]
    events.pending = [{
        "source": "aws.ec2",
        "detail-type": "EC2 Spot Instance Interruption Warning",
        "detail": {"instance-id": original.provider_id, "instance-action": "terminate"},
    }]
    # Give replacement launches distinct fixture identities.
    old_run = client.run_instances

    def replacement_run(**kwargs):
        response = old_run(**kwargs)
        response["Instances"][0]["InstanceId"] = "i-replacement"
        return response

    client.run_instances = replacement_run
    replaced = lifecycle.reconcile(spec)
    events.pending = []
    again = lifecycle.reconcile(spec)

    assert [host.provider_id for host in replaced if host.state != HostState.TERMINATED] == ["i-replacement"]
    assert [host.provider_id for host in again if host.state != HostState.TERMINATED] == ["i-replacement"]
    replacement = next(host for host in again if host.state != HostState.TERMINATED)
    assert original.operation_id != replacement.operation_id
    saved = state.read()
    assert [event["kind"] for event in saved["events"]].count("interruption") == 1


def test_eventbridge_rebalance_event_drains_host_and_ignores_other_events(tmp_path):
    client = StubEC2()

    class Events:
        pending = []

        def pending_events(self, owner, pool):
            return self.pending

    events = Events()

    spec = replace(
        pool(provider="ec2", enabled=True, desired=1, purchase_policy="spot",
             profile=replace(profile(), endpoint="", enrollment_secret_ref="")),
        provider_options={
            "instance_type": "m6i.xlarge", "subnet_id": "subnet-test",
            "security_group_ids": ["sg-test"], "instance_profile_arn": "arn:role",
            "spot_hourly_usd": 0.06,
        },
    )
    lifecycle = HostLifecycle(
        {"ec2": EC2Provider(client, event_source=events)},
        JsonStateStore(tmp_path / "state.json"),
    )
    lifecycle.reconcile(spec)
    events.pending = [
        {
            "source": "aws.ec2",
            "detail-type": "EC2 Instance Rebalance Recommendation",
            "detail": {"instance-id": "i-owned"},
        },
        {
            "source": "aws.ec2",
            "detail-type": "EC2 Instance State-change Notification",
            "detail": {"instance-id": "i-unrelated", "state": "stopping"},
        },
    ]
    lifecycle.reconcile(spec)

    assert client.instances[0]["State"]["Name"] == "terminated"
    assert "EC2 Instance Rebalance Recommendation" in (tmp_path / "state.json").read_text()


def test_stale_host_return_does_not_displace_or_rotate_replacement(tmp_path):
    provider = FakeProvider()
    spec = replace(pool(enabled=True, desired=1), purchase_policy="on_demand")
    state = JsonStateStore(tmp_path / "state.json")
    lifecycle = HostLifecycle({"fake": provider}, state)
    original = lifecycle.reconcile(spec)[0]

    provider.hosts.clear()
    replacement = lifecycle.reconcile(spec)[0]
    assert replacement.operation_id != original.operation_id

    provider.hosts[original.provider_id] = original
    reconciled = lifecycle.reconcile(spec)

    active = [host for host in reconciled if host.state != HostState.TERMINATED]
    assert [host.operation_id for host in active] == [replacement.operation_id]
    assert lifecycle._operation_id(spec, 0) == replacement.operation_id
    saved = state.read()
    assert [event["kind"] for event in saved["events"]].count("stale_host_retired") == 1


def test_provider_and_profile_options_are_namespaced_and_validated(tmp_path):
    lifecycle = HostLifecycle({"fake": FakeProvider()}, JsonStateStore(tmp_path / "state.json"))
    bad = pool(provider_options={"aws_region": "not portable"})
    with pytest.raises(ValueError, match="not supported"):
        lifecycle.plan(bad)
    with pytest.raises(ValueError, match="HTTPS"):
        lifecycle.plan(replace(pool(), profile=replace(profile(), endpoint="http://insecure")))

    ec2 = EC2Provider(StubEC2())
    ec2_pool = replace(
        pool(provider="ec2"),
        profile=replace(profile(), image="ubuntu-latest"),
        provider_options={"hourly_usd": 0.1},
    )
    with pytest.raises(ValueError, match="pinned AMI"):
        HostLifecycle({"ec2": ec2}, JsonStateStore(tmp_path / "ec2.json")).provision(
            replace(ec2_pool, enabled=True)
        )


def test_declarative_contract_rejects_unknown_fields():
    value = {
        "contract_version": "garden.hosts/v1",
        "name": "dev",
        "owner": "workplace",
        "purpose": "development",
        "provider": "fake",
        "profile": {
            "name": "dev",
            "version": "1",
            "image": "fixture-v1",
            "bootstrap_version": "tools-v1",
            "cpu": 2,
            "memory_mib": 4096,
            "disk_gib": 20,
        },
    }
    assert pool_from_dict(value).maximum == 1
    with pytest.raises(ValueError, match="unsupported pool fields"):
        pool_from_dict({**value, "ec2_instance_type": "m6i.xlarge"})


def test_ec2_policy_tags_and_standard_credits(tmp_path):
    from garden.hosts.models import HostDeclaration
    client = StubEC2()
    provider = EC2Provider(client, required_tags={"ManagedBy": "context-garden", "Pool": "phase05"})
    spec = pool(provider="ec2", profile=replace(profile(), endpoint=""), provider_options={
        "instance_type": "t3.xlarge", "subnet_id": "subnet-test", "security_group_ids": ["sg-test"],
        "instance_profile_arn": "arn:role", "hourly_usd": 0.18})
    provider.provision(HostDeclaration("worker-0", "op-test", spec))
    args = client.run_args
    assert args["CreditSpecification"] == {"CpuCredits": "standard"}
    assert {t["ResourceType"] for t in args["TagSpecifications"]} == {"instance", "volume", "network-interface"}
    for spec in args["TagSpecifications"]:
        tags = {t["Key"]: t["Value"] for t in spec["Tags"]}
        assert tags["ManagedBy"] == "context-garden" and tags["Pool"] == "phase05"
        assert tags[OPERATION_TAG] == "op-test"
    with pytest.raises(ValueError, match="reserved"):
        EC2Provider(client, required_tags={OWNER_TAG: "someone-else"})


def test_ec2_deadline_enforcement_is_verified_before_launch():
    from garden.hosts.models import HostDeclaration

    client = StubEC2()
    spec = pool(provider="ec2", profile=replace(profile(), endpoint=""), provider_options={
        "instance_type": "t3.xlarge", "subnet_id": "subnet-test", "security_group_ids": ["sg-test"],
        "instance_profile_arn": "arn:role", "hourly_usd": 0.18})
    declaration = HostDeclaration("worker-0", "op-deadline", spec, "2030-01-01T00:00:00Z")
    with pytest.raises(ValueError, match="external termination enforcer"):
        EC2Provider(client).provision(declaration)
    assert client.run_args is None

    events = []

    class Enforcer:
        def arm_and_verify(self, value):
            assert client.run_args is None
            events.append(value)

    EC2Provider(client, deadline_enforcer=Enforcer()).provision(declaration)
    assert events == [declaration]
    assert client.run_args["InstanceInitiatedShutdownBehavior"] == "terminate"


def test_ec2_does_not_claim_termination_before_aws_confirms():
    client = StubEC2()
    client.instances = [{"InstanceId": "i-owned", "State": {"Name": "running"}, "Tags": [
        {"Key": "context-garden:managed", "Value": "true"},
        {"Key": OWNER_TAG, "Value": "owner"}, {"Key": POOL_TAG, "Value": "pool"},
        {"Key": OPERATION_TAG, "Value": "op"}]}]
    client.terminate_instances = lambda **kw: {}
    provider = EC2Provider(client, wait_seconds=0)
    assert provider.destroy("i-owned", delete_storage=True).state == HostState.DRAINING
    client.instances[0]["Tags"] = []
    with pytest.raises(RuntimeError, match="ownership"):
        provider.destroy("i-owned", delete_storage=True)


def test_ec2_retirement_reports_real_retained_resources():
    client = StubEC2()
    client.instances = [{"InstanceId": "i-owned", "State": {"Name": "running"}, "Tags": [
        {"Key": "context-garden:managed", "Value": "true"},
        {"Key": OWNER_TAG, "Value": "owner"}, {"Key": POOL_TAG, "Value": "pool"},
        {"Key": OPERATION_TAG, "Value": "op"}],
        "BlockDeviceMappings": [{"DeviceName": "/dev/sda1", "Ebs": {"VolumeId": "vol-real"}}],
        "NetworkInterfaces": [{"NetworkInterfaceId": "eni-real"}]}]
    modifications = []
    client.modify_instance_attribute = lambda **kw: modifications.append(kw)
    client.describe_volumes = lambda **kw: {"Volumes": [{"VolumeId": "vol-real"}]}
    client.describe_network_interfaces = lambda **kw: {"NetworkInterfaces": []}
    client.describe_addresses = lambda **kw: {"Addresses": [{"AllocationId": "eipalloc-real"}]}
    result = EC2Provider(client).destroy("i-owned", delete_storage=False)
    assert result.state == HostState.TERMINATED
    assert result.retained_resources == ("vol-real", "eipalloc-real")
    assert modifications[0]["BlockDeviceMappings"][0]["Ebs"]["DeleteOnTermination"] is False


def test_endpoint_bootstrap_requires_prebuilt_contract():
    from garden.hosts.models import HostDeclaration
    with pytest.raises(ValueError, match="verified prebuilt AMI"):
        EC2Provider._user_data(HostDeclaration("host", "op", pool()))


class Enrollments:
    def __init__(self, values=None):
        self.values = values or {}
        self.revoked = []

    def resolve(self, host_id):
        return self.values.get(host_id, Enrollment())

    def ensure(self, host_id, _secret_ref):
        return self.resolve(host_id)

    def revoke(self, host_id):
        self.revoked.append(host_id)
        return (f"secret:{host_id}",)


def ready_enrollment(host_id="workers-0", **changes):
    return replace(Enrollment(secret_ref=f"secret/{host_id}", model_identity="codex-host",
                              repository_identity="github-installation",
                              tailnet_identity="tailscale-tag-worker",
                              controller_identity="worker-token"), **changes)


def test_scale_operation_resumes_missing_and_expired_enrollment_without_duplicates(tmp_path):
    provider = FakeProvider()
    lifecycle = HostLifecycle({"fake": provider}, JsonStateStore(tmp_path / "lifecycle.json"))
    enrollments = Enrollments({"workers-0": ready_enrollment()})
    def clock():
        return dt.datetime(2026, 9, 8, tzinfo=dt.UTC)
    operation = ScaleOperation(lifecycle, tmp_path / "workers-scale.json", enrollments, now=clock)
    requested = pool(enabled=True, desired=2, maximum=2,
                     profile=replace(profile(), enrollment_secret_ref="secret/{host_id}"))
    deadline = dt.datetime(2026, 9, 9, tzinfo=dt.UTC)

    status = operation.request(requested, deadline=deadline, aggregate_spend_limit_usd=80)
    assert status.desired == 2 and status.missing_setup == {
        "workers-1": ("scoped bootstrap secret", "dedicated model identity",
                      "repository installation/key identity", "tag-limited tailnet enrollment",
                      "scoped controller enrollment")}
    assert operation.continue_(requested).healthy == 1
    enrollments.values["workers-1"] = ready_enrollment("workers-1",
        model_expires_at="2026-09-07T00:00:00+00:00")
    assert operation.continue_(requested).missing_setup["workers-1"] == (
        "renew expired model identity",)
    enrollments.values["workers-1"] = ready_enrollment("workers-1",
        model_expires_at="2026-09-10T00:00:00+00:00")
    assert operation.continue_(requested).healthy == 2
    assert operation.continue_(requested).healthy == 2
    assert provider.provision_calls == 2


def test_scale_continuation_rejects_unadmitted_capacity_change(tmp_path):
    provider = FakeProvider()
    lifecycle = HostLifecycle({"fake": provider}, JsonStateStore(tmp_path / "life.json"))
    enrollments = Enrollments({f"workers-{slot}": ready_enrollment(f"workers-{slot}")
                               for slot in range(4)})
    now = dt.datetime(2026, 9, 8, tzinfo=dt.UTC)
    operation = ScaleOperation(lifecycle, tmp_path / "pool-scale.json", enrollments,
                               now=lambda: now)
    admitted = pool(enabled=True, desired=1, maximum=4,
                    profile=replace(profile(), enrollment_secret_ref="secret/{host_id}"))
    operation.request(admitted, deadline=now + dt.timedelta(hours=1),
                      aggregate_spend_limit_usd=80)
    operation.continue_(admitted)

    with pytest.raises(ValueError, match="new admitted scale request"):
        operation.continue_(replace(admitted, desired=4))
    assert provider.provision_calls == 1


def test_concurrent_requests_cannot_race_past_aggregate_admission(tmp_path):
    provider = FakeProvider(hourly_usd=0.4)
    now = dt.datetime(2026, 9, 8, tzinfo=dt.UTC)

    def admit(name):
        requested = replace(
            pool(enabled=True, desired=1), name=name,
            profile=replace(profile(), enrollment_secret_ref="secret/{host_id}"),
        )
        operation = ScaleOperation(
            HostLifecycle({"fake": provider}, JsonStateStore(tmp_path / f"{name}-life.json")),
            tmp_path / f"{name}-scale.json", Enrollments(), now=lambda: now,
        )
        try:
            operation.request(requested, deadline=now + dt.timedelta(hours=1),
                              aggregate_spend_limit_usd=0.5)
            return "admitted"
        except ValueError as exc:
            return str(exc)

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(admit, ("workers-a", "workers-b")))
    assert outcomes.count("admitted") == 1
    assert sum("aggregate admitted worker budget" in item for item in outcomes) == 1


@pytest.mark.parametrize("expiry", ["not-a-date", "2026-09-09T00:00:00"])
def test_model_identity_expiry_must_be_valid_and_timezone_aware(expiry):
    now = dt.datetime(2026, 9, 8, tzinfo=dt.UTC)
    assert ready_enrollment(model_expires_at=expiry).missing(now) == (
        "valid model identity expiry",)


def test_scale_partial_bootstrap_failure_preserves_ready_sibling_and_cleanup(tmp_path):
    provider = FakeProvider()
    def checks(host, _pool):
        return (host.host_id == "workers-0",
                "durable real-task result returned" if host.host_id == "workers-0"
                else "pinned bootstrap failed")
    lifecycle = HostLifecycle({"fake": provider}, JsonStateStore(tmp_path / "life.json"),
                              health_check=checks)
    enrollments = Enrollments({f"workers-{slot}": ready_enrollment(f"workers-{slot}")
                               for slot in range(2)})
    now = dt.datetime(2026, 9, 8, tzinfo=dt.UTC)
    operation = ScaleOperation(lifecycle, tmp_path / "pool-scale.json", enrollments,
                               now=lambda: now)
    requested = pool(enabled=True, desired=2, maximum=2,
                     profile=replace(profile(), enrollment_secret_ref="secret/{host_id}"))
    operation.request(requested, deadline=now + dt.timedelta(hours=1))

    result = operation.continue_(requested)
    assert result.healthy == 1
    assert any(host.state == HostState.TERMINATED for host in result.hosts)
    cleaned = operation.cleanup(requested)
    assert cleaned.healthy == 0
    assert enrollments.revoked == ["workers-0", "workers-1"]
    assert cleaned.pending_credential_revocations == (
        "secret:workers-0", "secret:workers-1")


def test_scale_deadline_and_aggregate_budget_survive_restart(tmp_path):
    provider = FakeProvider(hourly_usd=1)
    state = tmp_path / "pool-scale.json"
    lifecycle_state = tmp_path / "life.json"
    enrollments = Enrollments({"workers-0": ready_enrollment()})
    before = dt.datetime(2026, 9, 8, tzinfo=dt.UTC)
    requested = pool(enabled=True, desired=1, estimated_runtime_hours=2, spend_limit_usd=10,
                     profile=replace(profile(), enrollment_secret_ref="secret/{host_id}"))
    first = ScaleOperation(HostLifecycle({"fake": provider}, JsonStateStore(lifecycle_state)),
                           state, enrollments, now=lambda: before)
    with pytest.raises(ValueError, match="aggregate"):
        first.request(requested, deadline=before + dt.timedelta(hours=1),
                      aggregate_spend_limit_usd=1)
    first.request(requested, deadline=before + dt.timedelta(hours=1),
                  aggregate_spend_limit_usd=80)
    assert first.continue_(requested).healthy == 1
    after = before + dt.timedelta(hours=2)
    restarted = ScaleOperation(HostLifecycle({"fake": provider}, JsonStateStore(lifecycle_state)),
                               state, enrollments, now=lambda: after)
    assert restarted.continue_(requested).healthy == 0
    assert provider.destroy_calls == [("fake-1", True)]


def test_production_readiness_requires_matching_pinned_durable_task_result(tmp_path):
    check = durable_worker_readiness(tmp_path / ".garden")
    requested = pool(profile=replace(profile(), source_head="a" * 40),
                     provider_options={"bootstrap_sha256": "b" * 64})
    host = __import__("garden.hosts", fromlist=["HostFacts"]).HostFacts(
        "workers-0", "i-123", "op", HostState.BOOTSTRAPPING, "ami-pinned123", "0.1.0+abcdef")
    assert check(host, requested)[0] is None
    run = tmp_path / ".garden/runs/CG-1/run-1"
    run.mkdir(parents=True)
    digest = "b" * 64
    (run / "host_facts.json").write_text(json.dumps({
        "schema_version": 1,
        "provider_id": "i-123", "profile_version": "1.0.0",
        "bootstrap_version": "0.1.0+abcdef", "source_head": "a" * 40,
        "operation_id": "op",
        "source_bootstrap": {"source_head": "a" * 40, "bootstrap_sha256": digest,
                             "operation_id": "op"},
        "readiness_attestations": {
            "bootstrap_manifest": {"ok": True, "source_head": "a" * 40,
                                   "profile_version": "1.0.0",
                                   "bootstrap_version": "0.1.0+abcdef",
                                   "bootstrap_sha256": digest,
                                   "installed_distribution": "context-garden",
                                   "direct_url_commit": "a" * 40},
            "authenticated_registration": {"ok": True, "method": "scoped-worker-token"},
            "repository_access": {"ok": True, "repository": "example/project"},
            "ci_provider_read": {"ok": True, "source_head": "a" * 40}}}))
    (run / "run.json").write_text(json.dumps({
        "run_id": "run-1", "host": "workers-0", "mode": "work", "status": "done",
        "finished_at": "2026-09-08T12:00:00Z",
        "final_received_at": "2026-09-08T11:59:59Z"}))
    (run / "remote_result.json").write_text(json.dumps({"result": {"status": "done"}}))
    (run / "exit_code").write_text("0")
    healthy, detail = check(host, requested)
    assert healthy is True and "run-1" in detail
    saved_run = json.loads((run / "run.json").read_text())
    saved_run["mode"] = "revise"
    (run / "run.json").write_text(json.dumps(saved_run))
    assert check(host, requested)[0] is True

    facts = json.loads((run / "host_facts.json").read_text())
    facts["source_head"] = requested.profile.version
    (run / "host_facts.json").write_text(json.dumps(facts))
    assert check(host, requested)[0] is None

    facts["source_head"] = "a" * 40
    facts["readiness_attestations"] = {"bootstrap_manifest": True}
    (run / "host_facts.json").write_text(json.dumps(facts))
    assert check(host, requested)[0] is None
