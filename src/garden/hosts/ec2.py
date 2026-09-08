"""EC2 adapter for :mod:`garden.hosts`.

The adapter accepts a boto3-compatible client instead of importing an AWS SDK. Credential
resolution (SSO, role assumption, account boundaries) consequently stays with the embedding
workplace. The client is used only by the controller; generated bootstrap receives a scoped
instance role and secret reference, never controller credentials or secret values.
"""

from __future__ import annotations

import json
import re
import shlex
import time
from dataclasses import replace
from pathlib import Path
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
        "cpu_credits",
        "bootstrap_path",
        "bootstrap_url",
        "bootstrap_sha256",
        "shutdown_behavior",
    }

    def __init__(self, client: EC2Client, *, required_tags: dict[str, str] | None = None,
                 wait_seconds: float = 60, sleep=time.sleep):
        self.client = client
        self.required_tags = dict(required_tags or {})
        if any(k.startswith("context-garden:") or k.startswith("aws:") for k in self.required_tags):
            raise ValueError("policy tags cannot override reserved ownership tags")
        if any(not isinstance(k, str) or not isinstance(v, str) or not k or not v
               for k, v in self.required_tags.items()):
            raise ValueError("policy tags must be nonempty strings")
        self.wait_seconds = wait_seconds
        self.sleep = sleep

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
        if not declaration.pool.profile.image.startswith("ami-"):
            raise ValueError("ec2 profile image must be a pinned AMI id")
        if any(char.isspace() for char in declaration.pool.profile.bootstrap_version):
            raise ValueError("ec2 bootstrap_version must be one pinned version token")
        required = {"instance_type", "subnet_id", "security_group_ids", "instance_profile_arn"}
        missing = sorted(required - options.keys())
        if missing:
            raise ValueError(f"missing ec2 options: {missing}")
        tags = {
            **self.required_tags,
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
                            options.get("delete_root_on_termination", not declaration.pool.profile.persistent_workspace)
                        ),
                        "Encrypted": True,
                    },
                }
            ],
            "TagSpecifications": [
                {
                    "ResourceType": resource_type,
                    "Tags": [{"Key": key, "Value": value} for key, value in tags.items()],
                } for resource_type in ("instance", "volume", "network-interface")
            ],
            "UserData": self._user_data(declaration),
        }
        shutdown = options.get("shutdown_behavior", "stop")
        if shutdown not in {"stop", "terminate"}:
            raise ValueError("shutdown_behavior must be stop or terminate")
        args["InstanceInitiatedShutdownBehavior"] = shutdown
        if str(options["instance_type"]).startswith(("t2.", "t3.", "t3a.", "t4g.")):
            credits = options.get("cpu_credits", "standard")
            if credits not in ("standard", "unlimited"):
                raise ValueError("cpu_credits must be standard or unlimited")
            args["CreditSpecification"] = {"CpuCredits": credits}
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
        instance = self.client.describe_instances(InstanceIds=[provider_id])["Reservations"][0]["Instances"][0]
        tags = {row["Key"]: row["Value"] for row in instance.get("Tags", [])}
        if tags.get(OWNED_TAG) != "true" or not {OWNER_TAG, POOL_TAG, OPERATION_TAG} <= tags.keys():
            raise ProviderError("refusing to terminate an instance without lifecycle ownership")
        volumes = [row["Ebs"]["VolumeId"] for row in instance.get("BlockDeviceMappings", []) if "Ebs" in row]
        interfaces = [row["NetworkInterfaceId"] for row in instance.get("NetworkInterfaces", [])]
        mappings = [{"DeviceName": row["DeviceName"], "Ebs": {"DeleteOnTermination": delete_storage}}
                    for row in instance.get("BlockDeviceMappings", []) if "Ebs" in row]
        if mappings:
            self.client.modify_instance_attribute(InstanceId=provider_id, BlockDeviceMappings=mappings)
        self.client.terminate_instances(InstanceIds=[provider_id])
        deadline = time.monotonic() + self.wait_seconds
        while True:
            after = self.inspect(provider_id)
            if after.state == HostState.TERMINATED:
                break
            if time.monotonic() >= deadline:
                return replace(after, state=HostState.DRAINING,
                               retained_resources=tuple(volumes + interfaces),
                               detail="termination requested; cleanup not yet confirmed")
            self.sleep(min(1, max(0, deadline - time.monotonic())))
        # Query by filters: already deleted IDs yield an empty result rather than NotFound.
        retained = []
        if volumes:
            response = self.client.describe_volumes(Filters=[{"Name": "volume-id", "Values": volumes}])
            retained += [v["VolumeId"] for v in response.get("Volumes", [])]
        if interfaces:
            response = self.client.describe_network_interfaces(
                Filters=[{"Name": "network-interface-id", "Values": interfaces}])
            retained += [n["NetworkInterfaceId"] for n in response.get("NetworkInterfaces", [])]
        response = self.client.describe_addresses(Filters=[{"Name": f"tag:{OPERATION_TAG}",
                                                           "Values": [tags[OPERATION_TAG]]}])
        retained += [a.get("AllocationId", a.get("PublicIp", "")) for a in response.get("Addresses", [])]
        return replace(after, retained_resources=tuple(retained),
                       detail="termination observed; retained AWS resources inventoried")

    @staticmethod
    def _user_data(declaration: HostDeclaration) -> str:
        profile = declaration.pool.profile
        if not profile.endpoint:
            return ""
        options = {**declaration.pool.provider_options, **profile.provider_options}
        bootstrap_path = str(options.get("bootstrap_path") or "")
        if not bootstrap_path.startswith("/") or any(c.isspace() for c in bootstrap_path):
            raise ValueError("endpoint profiles require bootstrap_path on a verified prebuilt AMI")
        installer = ""
        url = str(options.get("bootstrap_url") or "")
        digest = str(options.get("bootstrap_sha256") or "")
        if url or digest:
            from urllib.parse import urlsplit
            parsed = urlsplit(url)
            if (parsed.scheme != "https" or not parsed.hostname or parsed.username
                    or parsed.password or parsed.query or parsed.fragment
                    or not re.fullmatch(r"[0-9a-f]{64}", digest)):
                raise ValueError("bootstrap artifact requires credential-free HTTPS and SHA256")
            # The generic lifecycle verifies a pinned consumer executable. It never
            # interprets worker enrollment or receives secret values.
            installer = ("install -d -m 700 " + shlex.quote(str(Path(bootstrap_path).parent)) + "\n"
                         + "curl --max-time 60 --fail --silent --show-error --proto '=https' --tlsv1.2 "
                         + shlex.quote(url) + " -o " + shlex.quote(bootstrap_path + ".download") + "\n"
                         + "printf '%s\\n' " + shlex.quote(digest + "  " + bootstrap_path + ".download")
                         + " | sha256sum --check --status\n"
                         + "mv " + shlex.quote(bootstrap_path + ".download") + " " + shlex.quote(bootstrap_path) + "\n"
                         + "chmod 700 " + shlex.quote(bootstrap_path) + "\n")
        # Environment-specific setup belongs to the pinned image/profile, not the EC2
        # lifecycle. The executable verifies the image manifest and starts its service.
        # Only secret references cross userdata; never inline auth material.
        config = json.dumps({"contract_version": CONTRACT_VERSION, "host": declaration.host_id,
                             "endpoint": profile.endpoint, "secret_ref": profile.enrollment_secret_ref,
                             "profile_version": profile.version,
                             "bootstrap_version": profile.bootstrap_version})
        return ("#!/bin/sh\nset -eu\numask 077\n"
                + installer + "test -x " + shlex.quote(bootstrap_path) + "\n"
                + "printf '%s' " + shlex.quote(config) + " > /run/host-bootstrap.json\n"
                + shlex.quote(bootstrap_path) + " --config /run/host-bootstrap.json\n")

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
