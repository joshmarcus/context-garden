"""Deterministic reconciliation journeys for the configured healthy worker count."""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import asdict, replace

import pytest

from garden.fleet import (
    FleetController,
    fleet_lines,
    fleet_projection,
    fleet_settings,
    fleet_summary,
)
from garden.hosts import (
    HostLifecycle,
    HostState,
    JsonStateStore,
    ScaleOperation,
    pool_from_dict,
)
from garden.hosts.drain import WorkerDrainStore
from garden.hosts.factory import operation_path_for
from garden.hosts.fake import FakeProvider
from garden.hosts.provider import ProviderError, TransientProviderError
from tests.test_hosts import Enrollments, pool, profile, ready_enrollment

DEADLINE = dt.datetime(2026, 9, 20, tzinfo=dt.UTC)


class Config:
    """The small read-only surface the fleet controller needs from configuration."""

    def __init__(self, root, block):
        self.root = root
        self.garden_dir = root / ".garden"
        self.data = {"workers": {"pool": block}} if block is not None else {}

    def get(self, dotted, default=None):
        node = self.data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node


def declaration(tmp_path, **changes):
    """Write a pool declaration file and return its parsed value."""
    spec = pool(enabled=True, spend_limit_usd=80,
                profile=replace(profile(), enrollment_secret_ref="secret/{host_id}"), **changes)
    path = tmp_path / "pool.json"
    path.write_text(json.dumps({"contract_version": "garden.hosts/v1", **asdict(spec)}))
    return spec


def enrollments_for(*host_ids):
    return Enrollments({host: ready_enrollment(host) for host in host_ids})


def admit(spec, garden_dir, provider, enrollments, clock, deadline=DEADLINE):
    """Admit ``spec`` where the CLI and the factory put it: the file named for the pool."""
    operation_path = operation_path_for(spec.name, garden_dir)
    lifecycle = HostLifecycle({"fake": provider}, JsonStateStore(
        operation_path.with_name(operation_path.stem + "-lifecycle.json")))
    ScaleOperation(lifecycle, operation_path, enrollments, now=clock).request(
        spec, deadline=deadline, aggregate_spend_limit_usd=80)
    return operation_path


def setup(tmp_path, *, desired=2, admitted=2, maximum=2, provider=None, enrollments=None,
          health=None, deadline=DEADLINE, now=None, admit_pool=True, **block):
    """Admit a pool operation, then return a controller configured to maintain it.

    The declaration file is `pool.json` while the pool is named `workers`, so every journey
    here goes through the same path `garden hosts scale` writes rather than one derived from
    the declaration's filename.
    """
    spec = declaration(tmp_path, desired=admitted, maximum=maximum)
    provider = provider or FakeProvider()
    enrollments = enrollments if enrollments is not None else enrollments_for(
        *(f"{spec.name}-{slot}" for slot in range(maximum)))
    clock = now or (lambda: dt.datetime(2026, 9, 12, tzinfo=dt.UTC))
    garden_dir = tmp_path / ".garden"
    if admit_pool:
        admit(spec, garden_dir, provider, enrollments, clock, deadline)
    config = Config(tmp_path, {"contract_version": "garden.fleet/v1", "declaration": "pool.json",
                               "desired": desired, **block})
    controller = FleetController(config, providers={"fake": provider}, enrollments=enrollments,
                                 health_check=health or (lambda host, pool: (True, "ready")),
                                 now=clock)
    return spec, provider, config, controller


def states(record):
    return {row["host_id"]: row["state"] for row in record["observed"]["hosts"]}


# -- configuration contract -----------------------------------------------------------


def test_a_garden_without_a_pool_block_keeps_its_static_behavior(tmp_path):
    config = Config(tmp_path, None)
    assert fleet_settings(config) is None
    assert fleet_projection(config) == {"configured": False}
    assert FleetController(config).converge() == {"configured": False}
    assert not (tmp_path / ".garden" / "fleet.json").exists()


def test_the_pool_contract_is_versioned_and_strict(tmp_path):
    with pytest.raises(ValueError, match="contract_version"):
        fleet_settings(Config(tmp_path, {"declaration": "pool.json", "desired": 1}))
    with pytest.raises(ValueError, match="unknown settings"):
        fleet_settings(Config(tmp_path, {"contract_version": "garden.fleet/v1",
                                         "declaration": "pool.json", "desired": 1, "count": 3}))
    with pytest.raises(ValueError, match="at most"):
        fleet_settings(Config(tmp_path, {"contract_version": "garden.fleet/v1",
                                         "declaration": "pool.json", "desired": 500}))
    settings = fleet_settings(Config(tmp_path, {"contract_version": "garden.fleet/v1",
                                               "declaration": "pool.json", "desired": 2}))
    assert settings is not None
    assert settings.declaration_path == (tmp_path / "pool.json").resolve()
    # The default operation file is named for the pool, exactly as `garden hosts scale` and
    # the factory name it — never for the declaration file it was declared in.
    assert settings.state_path is None
    assert settings.operation_path(pool(name="workers")) == \
        tmp_path / ".garden" / "hosts" / "workers-scale.json"
    assert (settings.interval_seconds, settings.failure_threshold) == (300, 5)
    explicit = fleet_settings(Config(tmp_path, {"contract_version": "garden.fleet/v1",
                                               "declaration": "pool.json", "desired": 2,
                                               "state": "operations/scale.json"}))
    assert explicit is not None
    assert explicit.operation_path(pool(name="workers")) == \
        (tmp_path / "operations" / "scale.json").resolve()


def test_the_controller_resumes_the_admission_the_documented_command_writes(tmp_path, monkeypatch):
    """`garden hosts scale` and the controller must mean the same durable operation.

    The declaration lives in `fleet-pool.json` while the pool is named `workers`, so this
    journey only converges if both sides name the operation after the pool.
    """
    from typer.testing import CliRunner

    from garden.cli import app
    from garden.hosts.factory import scale_operation

    spec = pool(enabled=True, desired=1, maximum=1, spend_limit_usd=80,
                profile=replace(profile(), enrollment_secret_ref="secret/{host_id}"))
    specification = tmp_path / "fleet-pool.json"
    specification.write_text(json.dumps({"contract_version": "garden.hosts/v1", **asdict(spec)}))
    provider, enrollments = FakeProvider(), enrollments_for("workers-0")

    def clock():
        return dt.datetime(2026, 9, 12, tzinfo=dt.UTC)

    def health(host, _pool):
        return True, "ready"

    # Only the provider and the identities are substituted; the command computes its own
    # durable operation path, exactly as an operator's invocation would.
    monkeypatch.setattr("garden.cli.hosts._build_operation",
                        lambda declared, operation_path, *rest: scale_operation(
                            declared, operation_path, garden_dir=tmp_path / ".garden",
                            providers={"fake": provider}, enrollments=enrollments,
                            health_check=health, now=clock))
    monkeypatch.chdir(tmp_path)
    admitted = CliRunner().invoke(app, ["hosts", "scale", str(specification),
                                        "--deadline", "2026-09-20T00:00:00Z"])
    assert admitted.exit_code == 0, admitted.output
    config = Config(tmp_path, {"contract_version": "garden.fleet/v1",
                               "declaration": "fleet-pool.json", "desired": 1})
    controller = FleetController(config, providers={"fake": provider}, enrollments=enrollments,
                                 health_check=health, now=clock)

    record = controller.converge()

    assert (tmp_path / ".garden" / "hosts" / "workers-scale.json").exists()
    assert record["last_outcome"] == "1 healthy, 0 pending, 0 failed of 1 desired"
    assert record["action_required"] == ""
    assert states(record) == {"workers-0": "ready"}
    assert fleet_projection(config)["dispatchable"] == 1


def test_an_unadmitted_pool_stops_with_one_operator_action(tmp_path):
    _, provider, config, controller = setup(tmp_path, admit_pool=False)

    record = controller.converge()

    assert "garden hosts scale pool.json --deadline" in record["action_required"]
    assert record["last_outcome"] == "not admitted"
    assert provider.provision_calls == 0
    assert fleet_projection(config)["action_required"] == record["action_required"]


def test_the_tick_and_the_startup_pass_drive_the_configured_pool(garden, fake_github):
    """The recurring driver is the scheduler's own pass, reading this garden's configuration.

    With nothing admitted the pass says so and surfaces the one operator action; once the
    admission exists where `garden hosts scale` writes it, the same pass resolves that
    operation instead of asking for it again.
    """
    import yaml

    from garden.scheduler import Scheduler
    from garden.store import Store

    spec = declaration(garden, desired=1, maximum=1)
    path = garden / "garden.yaml"
    data = yaml.safe_load(path.read_text())
    data["workers"] = {**(data.get("workers") or {}),
                       "pool": {"contract_version": "garden.fleet/v1",
                                "declaration": "pool.json", "desired": 1}}
    path.write_text(yaml.safe_dump(data))
    scheduler = Scheduler(Store(garden), github=fake_github, log=print)

    report = scheduler.tick(dispatch=False)

    record = json.loads((garden / ".garden" / "fleet.json").read_text())
    assert record["last_outcome"] == "not admitted"
    assert any("garden hosts scale pool.json" in error for error in report.errors)

    def clock():
        return dt.datetime(2026, 9, 12, tzinfo=dt.UTC)

    admit(spec, garden / ".garden", FakeProvider(), enrollments_for("workers-0"), clock)
    scheduler.reap_on_start()

    resumed = json.loads((garden / ".garden" / "fleet.json").read_text())
    assert resumed["pool"] == "workers"
    # Past the admission gate: the startup pass found this admission at the pool's own
    # durable path, and only the in-test fake adapter is not one the factory can build.
    assert "no controller adapter for provider 'fake'" in resumed["last_outcome"]


# -- convergence ----------------------------------------------------------------------


def test_zero_to_n_converges_and_makes_each_host_dispatchable(tmp_path):
    _, provider, config, controller = setup(tmp_path, desired=2)

    record = controller.converge()

    assert provider.provision_calls == 2
    assert record["observed"]["desired"] == 2
    assert (record["observed"]["healthy"], record["observed"]["dispatchable"]) == (2, 2)
    assert record["action_required"] == ""
    assert not WorkerDrainStore(config.garden_dir).contains(
        next(iter(provider.hosts.values())).operation_id)
    projection = fleet_projection(config)
    assert projection["configured_desired"] == 2 and projection["dispatchable"] == 2
    assert projection["generation"] == record["observed"]["operation_id"]


def test_a_converged_pool_is_not_reprovisioned_and_respects_the_cadence(tmp_path):
    _, provider, _, controller = setup(tmp_path, desired=2)
    controller.converge()

    # The cadence has not elapsed, so the second pass is a no-op...
    assert controller.converge()["observed"]["healthy"] == 2
    # ...and a forced pass reuses the same two stable slots rather than creating more.
    record = controller.converge(force=True)

    assert provider.provision_calls == 2
    assert sorted(states(record)) == ["workers-0", "workers-1"]
    assert record["attempts"] == 0


def test_partial_enrollment_reports_the_missing_identity_without_provisioning(tmp_path):
    _, provider, _, controller = setup(tmp_path, desired=2,
                                       enrollments=enrollments_for("workers-0"))

    record = controller.converge()

    assert provider.provision_calls == 1
    assert list(record["observed"]["missing_setup"]) == ["workers-1"]
    assert "dedicated model identity" in record["observed"]["missing_setup"]["workers-1"]
    assert record["action_required"].startswith("complete the private enrollment for workers-1")
    assert record["attempts"] == 1  # an unenrollable slot backs off rather than spinning


def test_uncertain_provisioning_is_reconciled_by_discovery_not_a_second_host(tmp_path):
    _, provider, _, controller = setup(tmp_path, desired=1, admitted=1, maximum=1)
    provider.delay_next_response = True

    first = controller.converge()
    second = controller.converge(force=True)

    # Provider discovery resolved the uncertain acquisition into the slot's one real host.
    assert provider.provision_calls == 1
    assert len(provider.hosts) == 1
    assert first["observed"]["healthy"] == 1 and states(first) == {"workers-0": "ready"}
    assert second["observed"]["healthy"] == 1 and second["attempts"] == 0


# -- replacement ----------------------------------------------------------------------


def test_an_unhealthy_host_is_retired_and_replaced_in_its_stable_slot(tmp_path):
    unhealthy = {"workers-0"}

    def health(host, _pool):
        return (False, "probe failed") if host.host_id in unhealthy else (True, "ready")

    _, provider, config, controller = setup(tmp_path, desired=2, health=health)
    short = controller.converge()
    drains = WorkerDrainStore(config.garden_dir)
    retired = [host for host in provider.hosts.values() if host.host_id == "workers-0"]

    # The unhealthy host is drained and retired; its healthy sibling is untouched.
    assert [host.state for host in retired] == [HostState.TERMINATED]
    assert states(short) == {"workers-1": "ready"}
    assert short["attempts"] == 1 and short["observed"]["dispatchable"] == 1
    assert controller.converge(force=True)["observed"]["healthy"] == 1
    unhealthy.clear()
    record = controller.converge(force=True)

    replacement = next(host for host in provider.hosts.values()
                       if host.host_id == "workers-0" and host.state != HostState.TERMINATED)
    assert record["observed"]["healthy"] == 2 and record["attempts"] == 0
    assert states(record)["workers-0"] == "ready"  # the same stable slot, a new host
    assert replacement.provider_id not in {host.provider_id for host in retired}
    assert not drains.contains(replacement.operation_id)  # dispatchable again
    assert drains._read() == {}  # no fence lingers for a host that is gone


def test_replacement_backs_off_and_then_breaks_the_circuit(tmp_path):
    class Broken(FakeProvider):
        def provision(self, declaration):
            # A definitive provider refusal — a bad image reference, say — is not retried
            # inside the lifecycle, so the controller's own bounds are what hold it back.
            raise ProviderError("image reference is not usable")

    _, provider, config, controller = setup(tmp_path, desired=1, admitted=1, maximum=1,
                                           provider=Broken(), backoff_seconds=60,
                                           backoff_ceiling_seconds=120, failure_threshold=3)
    delays = []
    for _ in range(3):
        record = controller.converge(force=True)
        delays.append(record.get("next_attempt_at"))

    assert delays[:2] == ["2026-09-12T00:01:00+00:00", "2026-09-12T00:02:00+00:00"]
    assert record["breaker"] and record["next_attempt_at"] == ""
    assert "garden hosts fleet --resume" in record["action_required"]
    # A tripped breaker refuses even a forced pass: only an operator clears it.
    assert controller.converge(force=True)["breaker"]
    assert fleet_projection(config)["breaker"] is True

    controller.resume()

    assert controller.converge()["attempts"] == 1


def test_the_ceiling_bounds_the_backoff_delay(tmp_path):
    class Broken(FakeProvider):
        def provision(self, declaration):
            raise ProviderError("image reference is not usable")

    _, _, _, controller = setup(tmp_path, desired=1, admitted=1, maximum=1, provider=Broken(),
                                backoff_seconds=60, backoff_ceiling_seconds=90,
                                failure_threshold=10)
    for _ in range(4):
        record = controller.converge(force=True)

    assert record["next_attempt_at"] == "2026-09-12T00:01:30+00:00"


# -- scale down -----------------------------------------------------------------------


def test_reducing_desired_waits_for_active_work_then_retires_and_revokes(tmp_path):
    from garden.runs import RunStore

    _, provider, config, controller = setup(tmp_path, desired=2)
    controller.converge()
    excess = next(host for host in provider.hosts.values() if host.host_id == "workers-1")
    run = RunStore(config.garden_dir).new_run("DM-001", "remote")
    run.host = "workers-1"
    run.save()
    controller.settings = replace(controller.settings, desired=1)

    draining = controller.converge(force=True)

    assert states(draining)["workers-1"] == "draining"
    assert draining["observed"]["draining"] == 1 and draining["observed"]["healthy"] == 1
    assert WorkerDrainStore(config.garden_dir).contains(excess.operation_id)
    assert any("waiting for host termination" in row
               for row in draining["observed"]["pending_credential_revocations"])
    assert provider.destroy_calls == []  # active work reaches its own boundary first

    run.status = "done"
    run.save()
    RunStore(config.garden_dir).invalidate()
    retired = controller.converge(force=True)

    assert [call[0] for call in provider.destroy_calls] == [excess.provider_id]
    assert retired["observed"]["healthy"] == 1
    assert states(retired) == {"workers-0": "ready"}
    assert retired["observed"]["pending_credential_revocations"] == ["secret:workers-1"]
    # The remaining sibling was untouched and is still dispatchable.
    assert retired["observed"]["dispatchable"] == 1


def test_a_configured_count_cannot_exceed_the_admitted_maximum(tmp_path):
    _, provider, _, controller = setup(tmp_path, desired=4, admitted=2, maximum=2)

    record = controller.converge()

    assert provider.provision_calls == 2
    assert record["observed"]["healthy"] == 2
    assert "exceeds the 2 host(s) this pool admitted" in record["action_required"]
    assert "garden hosts scale" in record["action_required"]


def test_an_edited_declaration_needs_a_new_admission(tmp_path):
    spec, provider, config, controller = setup(tmp_path, desired=2)
    controller.converge()
    (tmp_path / "pool.json").write_text(json.dumps({
        "contract_version": "garden.hosts/v1",
        **asdict(replace(spec, profile=replace(spec.profile, version="2.0.0")))}))

    record = controller.converge(force=True)

    assert "no longer matches the admitted declaration" in record["action_required"]
    assert record["observed"]["exact_version"].endswith("1.0.0/0.1.0+abcdef")
    assert record["observed"]["healthy"] == 2  # existing capacity keeps running
    assert provider.provision_calls == 2


# -- durability -----------------------------------------------------------------------


def test_a_restarted_controller_resumes_the_same_generation_and_backoff(tmp_path):
    class Flaky(FakeProvider):
        fail = True

        def provision(self, declaration):
            if self.fail:
                raise TransientProviderError("provider is unavailable")
            return super().provision(declaration)

    provider = Flaky()
    spec, _, config, controller = setup(tmp_path, desired=1, admitted=1, maximum=1,
                                       provider=provider, backoff_seconds=60,
                                       backoff_ceiling_seconds=3600, failure_threshold=5)
    controller.converge()
    controller.converge(force=True)
    before = json.loads((config.garden_dir / "fleet.json").read_text())

    resumed = FleetController(config, providers={"fake": provider},
                              enrollments=enrollments_for("workers-0"),
                              health_check=lambda host, pool: (True, "ready"),
                              now=controller.now)

    # A restart neither resets the attempt count nor re-attempts before the timer.
    assert resumed.converge() == before
    provider.fail = False
    record = resumed.converge(force=True)

    assert record["generation"] == before["generation"]
    assert record["attempts"] == 0 and record["observed"]["healthy"] == 1
    assert provider.provision_calls == 1


def test_an_expired_admission_stops_with_a_new_admission_request(tmp_path):
    current = [dt.datetime(2026, 9, 12, tzinfo=dt.UTC)]

    _, provider, _, controller = setup(tmp_path, desired=1, admitted=1, maximum=1,
                                       now=lambda: current[0])
    controller.converge()
    current[0] = dt.datetime(2026, 9, 21, tzinfo=dt.UTC)  # past the absolute deadline

    record = controller.converge(force=True)

    assert record["observed"]["phase"] in {"cleaning", "cleaned"}
    assert record["observed"]["dispatchable"] == 0
    assert "admission has ended" in record["action_required"]
    assert provider.destroy_calls
    # Reconciliation cannot extend an admission, so an unmet count is not a retry loop.
    assert record["attempts"] == 0 and record["breaker"] is False


def test_a_zero_count_retires_capacity_without_claiming_the_admission_expired(tmp_path):
    _, provider, _, controller = setup(tmp_path, desired=1, admitted=1, maximum=1)
    controller.converge()
    controller.settings = replace(controller.settings, desired=0)

    record = controller.converge(force=True)

    assert record["observed"]["phase"] in {"cleaning", "cleaned"}
    assert record["action_required"] == ""
    assert [call[0] for call in provider.destroy_calls] == list(provider.hosts)


# -- provider neutrality --------------------------------------------------------------


def test_the_same_boundary_drives_a_command_provider_pool(tmp_path):
    """The public boundary is provider-neutral: only the wrapper differs.

    The wrapper is the deterministic one the command-adapter tests already use, so nothing
    provider-specific — no host name, credential location or organization — is embedded here.
    """
    from garden.hosts import CommandProvider
    from tests.test_command_hosts import Wrapper

    wrapper = Wrapper()
    provider = CommandProvider(wrapper)
    spec = pool(enabled=True, desired=1, maximum=1, spend_limit_usd=80, provider="command",
                provider_options={"command": ["worker-wrapper"], "hourly_usd": 0.2},
                profile=replace(profile(), enrollment_secret_ref="secret/{host_id}"))
    (tmp_path / "pool.json").write_text(
        json.dumps({"contract_version": "garden.hosts/v1", **asdict(spec)}))
    garden_dir = tmp_path / ".garden"
    enrollments = enrollments_for("workers-0")

    def clock():
        return dt.datetime(2026, 9, 12, tzinfo=dt.UTC)

    operation_path = operation_path_for(spec.name, garden_dir)
    lifecycle = HostLifecycle({"command": provider}, JsonStateStore(
        operation_path.with_name(operation_path.stem + "-lifecycle.json")))
    ScaleOperation(lifecycle, operation_path, enrollments,
                   now=clock).request(spec, deadline=DEADLINE, aggregate_spend_limit_usd=80)
    config = Config(tmp_path, {"contract_version": "garden.fleet/v1",
                               "declaration": "pool.json", "desired": 1})
    controller = FleetController(config, providers={"command": provider}, enrollments=enrollments,
                                 health_check=lambda host, pool: (True, "ready"), now=clock)

    record = controller.converge()

    assert record["observed"]["healthy"] == 1
    assert states(record) == {"workers-0": "ready"}
    assert pool_from_dict(json.loads((tmp_path / "pool.json").read_text())).provider == "command"
    assert {call[0][-1] for call in wrapper.calls} >= {"acquire", "inspect"}
    # The wrapper is told which logical host to acquire, never how to authenticate as it.
    assert not any("secret" in json.dumps(call[1]) for call in wrapper.calls)


# -- read-only projection -------------------------------------------------------------


def test_one_projection_serves_status_doctor_and_observe(tmp_path):
    """Status, doctor and observe read the controller's last durable pass, not a provider."""
    _, _, config, controller = setup(tmp_path, desired=2)

    controller.converge()

    lines = fleet_lines(config)
    assert lines[0] == ("fleet: desired 2 · healthy 2 · dispatchable 2 · pending 0 · "
                        "draining 0 · failed 0")
    assert "worker version " in lines[1]
    assert "deadline 2026-09-20T00:00:00+00:00" in lines[1]
    assert "estimated cost $" in lines[1] and " of $80.00" in lines[1]
    assert "next retry 2026-09-12T00:05:00+00:00" in lines[1]
    assert not any("action required" in line for line in lines)
    assert fleet_summary(config) == ("fleet 2/2 healthy · 2 dispatchable · 0 pending · "
                                     "0 draining · 0 failed")


def test_a_blocked_pool_names_its_operator_action_in_every_reading(tmp_path):
    _, _, config, controller = setup(tmp_path, desired=2, admit_pool=False)

    controller.converge()

    lines = fleet_lines(config)
    assert lines[-1] == ("fleet: action required — admit this pool first: garden hosts scale "
                         "pool.json --deadline <absolute ISO-8601 UTC>")
    assert fleet_summary(config).endswith("— action required")
    assert fleet_lines(Config(tmp_path, None)) == [] and fleet_summary(Config(tmp_path, None)) == ""
