"""Typed notification delivery stays local under synthetic adapters."""

from __future__ import annotations

import json

from garden.notification_adapters import (
    NotificationDelivery,
    NotificationEvent,
    PermanentDeliveryError,
    TransientDeliveryError,
)


class SyntheticAdapter:
    version = 1

    def __init__(self, outcome: str = "ok"):
        self.outcome = outcome
        self.calls: list[dict] = []

    def deliver(self, destination, fields, timeout):
        self.calls.append({"destination": destination, "fields": fields, "timeout": timeout})
        if self.outcome == "transient":
            raise TransientDeliveryError("temporarily unavailable")
        if self.outcome == "permanent":
            raise PermanentDeliveryError("destination rejected")


def _cfg(**destination):
    return {"notify": {"destinations": {"operator": {"adapter": "synthetic", **destination}}}}


def test_delivery_coalesces_duplicates_and_survives_restart(tmp_path):
    adapter = SyntheticAdapter()
    event = NotificationEvent("CG-520", "waiting_human", "needs a decision")
    delivery = NotificationDelivery(tmp_path / "notifications.json", {"synthetic": adapter})

    assert delivery.deliver(_cfg(), event) == ["operator: delivered"]
    assert delivery.deliver(_cfg(), event) == []
    restarted = NotificationDelivery(tmp_path / "notifications.json", {"synthetic": adapter})
    assert restarted.deliver(_cfg(), event) == []
    assert len(adapter.calls) == 1


def test_delivery_emits_each_lifecycle_kind_once_per_identity(tmp_path):
    adapter = SyntheticAdapter()
    delivery = NotificationDelivery(tmp_path / "notifications.json", {"synthetic": adapter})
    events = [
        NotificationEvent("CG-520", "failed", "delivery failed", kind="failure"),
        NotificationEvent("CG-520", "harness_resumed", "dispatch resumed", kind="recovery"),
        NotificationEvent("CG-520", "needs_human", "choose a path", kind="required_action"),
    ]

    for event in events:
        assert delivery.deliver(_cfg(), event) == ["operator: delivered"]
        assert delivery.deliver(_cfg(), event) == []
    assert [call["fields"]["kind"] for call in adapter.calls] == [
        "failure", "recovery", "required_action",
    ]


def test_delivery_retries_transient_errors_with_bounded_backoff(tmp_path):
    now = [100.0]
    adapter = SyntheticAdapter("transient")
    delivery = NotificationDelivery(tmp_path / "notifications.json", {"synthetic": adapter}, lambda: now[0])
    cfg = _cfg(max_attempts=2, backoff_seconds=5)
    event = NotificationEvent("CG-520", "failed", "retry me")

    assert delivery.deliver(cfg, event) == ["operator: failed"]
    assert delivery.deliver(cfg, event) == []
    now[0] = 106.0
    assert delivery.deliver(cfg, event) == ["operator: failed"]
    assert delivery.deliver(cfg, event) == []
    assert len(adapter.calls) == 2


def test_restart_retries_a_due_failure_without_replaying_the_transition(tmp_path):
    now = [100.0]
    failing = SyntheticAdapter("transient")
    cfg = _cfg(backoff_seconds=5)
    event = NotificationEvent("CG-520", "required_action", "take action")
    NotificationDelivery(tmp_path / "notifications.json", {"synthetic": failing}, lambda: now[0]).deliver(cfg, event)

    now[0] = 106.0
    recovered = SyntheticAdapter()
    restarted = NotificationDelivery(tmp_path / "notifications.json", {"synthetic": recovered}, lambda: now[0])
    assert restarted.retry_pending(cfg) == ["operator: delivered"]
    assert len(recovered.calls) == 1


def test_delivery_filters_worker_text_and_honours_revocation(tmp_path):
    adapter = SyntheticAdapter()
    delivery = NotificationDelivery(tmp_path / "notifications.json", {"synthetic": adapter})
    cfg = _cfg(fields=["task_id", "message"], revoked=False)
    cfg["ssh"] = {"hosts": [{"name": "private", "host": "host-203-0-113-10.internal"}]}
    event = NotificationEvent("CG-520", "failed", "$(danger) host-203-0-113-10.internal token=secret")

    delivery.deliver(cfg, event)
    assert adapter.calls[0]["fields"] == {"task_id": "CG-520", "message": "$(danger) private token=<redacted>"}
    ledger = (tmp_path / "notifications.json").read_text()
    assert "host-203-0-113-10.internal" not in ledger
    assert "token=secret" not in ledger
    cfg["notify"]["destinations"]["operator"]["revoked"] = True
    assert delivery.deliver(cfg, NotificationEvent("CG-520", "recovered", "safe")) == ["operator: revoked"]
    assert len(adapter.calls) == 1


def test_delivery_scrubs_every_string_field_before_delivery_and_persistence(tmp_path):
    adapter = SyntheticAdapter()
    delivery = NotificationDelivery(tmp_path / "notifications.json", {"synthetic": adapter})
    cfg = _cfg(fields=["task_id", "status", "message", "pr_url", "kind"])
    cfg["ssh"] = {"hosts": [{"name": "private", "host": "host-203-0-113-10.internal"}]}
    event = NotificationEvent("CG-520", "failed", "token=secret", "https://user:pass@host-203-0-113-10.internal/pull/1")

    assert delivery.deliver(cfg, event) == ["operator: delivered"]
    fields = adapter.calls[0]["fields"]
    assert fields["message"] == "token=<redacted>"
    assert fields["pr_url"] == "https://<redacted>@private/pull/1"
    ledger = (tmp_path / "notifications.json").read_text()
    assert "secret" not in ledger
    assert "user:pass" not in ledger
    assert "host-203-0-113-10.internal" not in ledger


def test_delivery_records_permanent_adapter_failure_without_external_delivery(tmp_path):
    adapter = SyntheticAdapter("permanent")
    delivery = NotificationDelivery(tmp_path / "notifications.json", {"synthetic": adapter})
    event = NotificationEvent("CG-520", "required_action", "no external message")

    assert delivery.deliver(_cfg(), event) == ["operator: permanent failure"]
    assert delivery.deliver(_cfg(), event) == []
    assert len(adapter.calls) == 1


def test_argv_adapter_records_a_timeout_without_interpreting_event_text(tmp_path):
    delivery = NotificationDelivery(tmp_path / "notifications.json")
    cfg = {"notify": {"destinations": {
        "operator": {"adapter": "argv", "argv": ["sleep", "1"], "timeout_seconds": 0.01},
    }}}

    assert delivery.deliver(cfg, NotificationEvent("CG-520", "failed", "$(not executed)")) == ["operator: failed"]


def test_malformed_destination_policy_is_a_visible_nonfatal_ledger_outcome(tmp_path):
    adapter = SyntheticAdapter()
    delivery = NotificationDelivery(tmp_path / "notifications.json", {"synthetic": adapter})
    event = NotificationEvent("CG-520", "failed", "transition must continue")

    for policy in (
        {"timeout_seconds": "not-a-number"},
        {"max_attempts": "not-a-number"},
        {"backoff_seconds": "not-a-number"},
    ):
        cfg = _cfg(**policy)
        assert delivery.deliver(cfg, event) == ["operator: permanent failure"]
        ledger = json.loads((tmp_path / "notifications.json").read_text())
        assert next(iter(ledger.values()))["reason"] == "invalid destination policy"
        (tmp_path / "notifications.json").unlink()

    assert adapter.calls == []
