"""EC2 adapter for :mod:`garden.hosts`.

The adapter accepts a boto3-compatible client instead of importing an AWS SDK. Credential
resolution (SSO, role assumption, account boundaries) consequently stays with the embedding
workplace. The client is used only by the controller; generated bootstrap receives a scoped
instance role and secret reference, never controller credentials or secret values.
"""

from __future__ import annotations

import json
import shlex
from dataclasses import replace
from typing import Any, Protocol

from .models import CONTRACT_VERSION, HostDeclaration, HostFacts, HostState, ProviderCapabilities
from .provider import ProviderError, ProvisioningUncertain, TransientProviderError

OWNED_TAG = "context-garden:managed"
OWNER_TAG = "context-garden:owner"
POOL_TAG = "context-garden:pool"
HOST_TAG = "context-garden:host-id"
OPERATION_TAG = "context-garden:operation-id"
BOOTSTRAP_TAG = "context-garden:bootstrap-version"


class EC2Client(Protocol):
    def describe_instances(self, **kwargs: Any) -> dict[str, Any]: ...
    def run_instances(self, **kwargs: Any) -> dict[str, Any]: ...
    def terminate_instances(self, **kwargs: Any) -> dict[str, Any]: ...
    def stop_instances(self, **kwargs: Any) -> dict[str, Any]: ...
    def start_instances(self, **kwargs: Any) -> dict[str, Any]: ...


class EC2Provider:
    name = "ec2"
    contract_version = CONTRACT_VERSION
    capabilities = ProviderCapabilities(stop_start=True, persistent_disks=True, spot=False)
    ALLOWED_OPTIONS = {
        "instance_type",
        "subnet_id",
        "security_group_ids",
        "instance_profile_arn",
        "availability_zone",
        "hourly_usd",
        "delete_root_on_termination",
    }

    def __init__(self, client: EC2Client):
        self.client = client

    def validate_options(self, options: dict[str, Any]) -> None:
        unknown = set(options) - self.ALLOWED_OPTIONS
        if unknown:
            raise ValueError(f"unsupported ec2 options: {sorted(unknown)}")

    def estimate_hourly_usd(self, declaration: HostDeclaration) -> float:
        options = {**declaration.pool.provider_options, **declaration.pool.profile.provider_options}
        if "hourly_usd" not in options:
            raise ValueError("ec2.hourly_usd is required for a reviewable, current cost plan")
        return float(options["hourly_usd"])

    def discover(self, owner: str, pool: str) -> list[HostFacts]:
        response = self.client.describe_instances(
            Filters=[
                {"Name": f"tag:{OWNED_TAG}", "Values": ["true"]},
                {"Name": f"tag:{OWNER_TAG}", "Values": [owner]},
                {"Name": f"tag:{POOL_TAG}", "Values": [pool]},
                {
                    "Name": "instance-state-name",
                    "Values": ["pending", "running", "stopping", "stopped"],
                },
            ]
        )
        return [
            self._facts(instance)
            for reservation in response.get("Reservations", [])
            for instance in reservation.get("Instances", [])
        ]

    def provision(self, declaration: HostDeclaration) -> HostFacts:
        options = {**declaration.pool.provider_options, **declaration.pool.profile.provider_options}
        required = {"instance_type", "subnet_id", "security_group_ids", "instance_profile_arn"}
        missing = sorted(required - options.keys())
        if missing:
            raise ValueError(f"missing ec2 options: {missing}")
        tags = {
            OWNED_TAG: "true",
            OWNER_TAG: declaration.pool.owner,
            POOL_TAG: declaration.pool.name,
            HOST_TAG: declaration.host_id,
            OPERATION_TAG: declaration.operation_id,
            BOOTSTRAP_TAG: declaration.pool.profile.bootstrap_version,
        }
        args: dict[str, Any] = {
            "ImageId": declaration.pool.profile.image,
            "InstanceType": options["instance_type"],
            "MinCount": 1,
            "MaxCount": 1,
            "ClientToken": declaration.operation_id,
            "SubnetId": options["subnet_id"],
            "SecurityGroupIds": list(options["security_group_ids"]),
            "IamInstanceProfile": {"Arn": options["instance_profile_arn"]},
            "MetadataOptions": {
                "HttpTokens": "required",
                "HttpEndpoint": "enabled",
                "HttpPutResponseHopLimit": 1,
            },
            "BlockDeviceMappings": [
                {
                    "DeviceName": "/dev/sda1",
                    "Ebs": {
                        "VolumeSize": declaration.pool.profile.disk_gib,
                        "VolumeType": "gp3",
                        "DeleteOnTermination": bool(
                            options.get("delete_root_on_termination", True)
                        ),
                        "Encrypted": True,
                    },
                }
            ],
            "TagSpecifications": [
                {
                    "ResourceType": "instance",
                    "Tags": [{"Key": key, "Value": value} for key, value in tags.items()],
                }
            ],
            "UserData": self._user_data(declaration),
        }
        if options.get("availability_zone"):
            args["Placement"] = {"AvailabilityZone": options["availability_zone"]}
        try:
            response = self.client.run_instances(**args)
        except TimeoutError as exc:
            raise ProvisioningUncertain(str(exc)) from exc
        except ConnectionError as exc:
            raise TransientProviderError(f"temporary EC2 connection failure: {exc}") from exc
        except Exception as exc:
            raise ProviderError(f"EC2 launch failed: {exc}") from exc
        return self._facts(response["Instances"][0])

    def inspect(self, provider_id: str) -> HostFacts:
        response = self.client.describe_instances(InstanceIds=[provider_id])
        return self._facts(response["Reservations"][0]["Instances"][0])

    def stop(self, provider_id: str) -> HostFacts:
        self.client.stop_instances(InstanceIds=[provider_id])
        return replace(self.inspect(provider_id), state=HostState.STOPPED)

    def start(self, provider_id: str) -> HostFacts:
        self.client.start_instances(InstanceIds=[provider_id])
        return replace(self.inspect(provider_id), state=HostState.BOOTSTRAPPING)

    def destroy(self, provider_id: str, *, delete_storage: bool) -> HostFacts:
        before = self.inspect(provider_id)
        self.client.terminate_instances(InstanceIds=[provider_id])
        retained = () if delete_storage else tuple(self._volume_ids(provider_id))
        return replace(before, state=HostState.TERMINATED, retained_resources=retained)

    @staticmethod
    def _volume_ids(provider_id: str) -> list[str]:
        # A retained-volume integration can override this adapter method and enumerate EBS.
        return [f"attached-storage:{provider_id}"]

    @staticmethod
    def _user_data(declaration: HostDeclaration) -> str:
        profile = declaration.pool.profile
        if not profile.endpoint:
            return ""
        endpoint = shlex.quote(profile.endpoint.rstrip("/"))
        secret_ref = shlex.quote(profile.enrollment_secret_ref)
        version = shlex.quote(profile.bootstrap_version)
        script = f"""#!/bin/sh
set -eu
umask 077
token_file=$(mktemp)
trap 'rm -f "$token_file"' EXIT
aws secretsmanager get-secret-value --secret-id {secret_ref} --query SecretString --output text > "$token_file"
curl --fail --silent --show-error --proto '=https' --tlsv1.2 \\
  -H "Authorization: Bearer $(cat "$token_file")" \\
  -H 'Content-Type: application/json' \\
  --output /dev/null \\
  --data '{{"contract":"{CONTRACT_VERSION}","worker_version":"{version}"}}' \\
  {endpoint}/api/workers/enroll
"""
        return script

    @staticmethod
    def _facts(instance: dict[str, Any]) -> HostFacts:
        tags = {t["Key"]: t["Value"] for t in instance.get("Tags", [])}
        aws_state = instance.get("State", {}).get("Name", "pending")
        state = {
            "pending": HostState.PROVISIONING,
            "running": HostState.BOOTSTRAPPING,
            "stopping": HostState.DRAINING,
            "stopped": HostState.STOPPED,
            "terminated": HostState.TERMINATED,
        }.get(aws_state, HostState.FAILED)
        return HostFacts(
            tags.get(HOST_TAG, instance["InstanceId"]),
            instance["InstanceId"],
            tags.get(OPERATION_TAG, ""),
            state,
            instance.get("ImageId", ""),
            tags.get(BOOTSTRAP_TAG, ""),
            detail=json.dumps({"aws_state": aws_state}),
        )
