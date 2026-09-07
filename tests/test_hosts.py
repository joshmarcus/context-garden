from __future__ import annotations

from dataclasses import replace

import pytest

from garden.hosts import (
    EnvironmentProfile,
    HostLifecycle,
    HostState,
    JsonStateStore,
    PoolDeclaration,
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
