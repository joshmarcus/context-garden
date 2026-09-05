"""Unit tests for garden.notify: the command actually runs, and a failure is logged
loudly (not swallowed) — see tests/scheduler/test_notify.py for the scheduler-level
integration (which transitions fire it, and the triage/review-verdict ordering)."""

import logging

from garden.notify import notify, notify_test, unquoted_message_warning


def test_notify_does_nothing_when_unconfigured(caplog):
    notify({}, "T-1", "failed", "msg")  # no command configured; must not raise or log
    assert caplog.text == ""


def test_notify_succeeds_silently(caplog):
    with caplog.at_level(logging.WARNING, logger="garden.notify"):
        notify({"notify": {"command": "true"}}, "T-1", "failed", "msg")
    assert caplog.text == ""


def test_notify_logs_a_warning_on_nonzero_exit(caplog):
    with caplog.at_level(logging.WARNING, logger="garden.notify"):
        notify({"notify": {"command": "exit 3"}}, "T-1", "failed", "msg")
    assert "T-1" in caplog.text and "exited 3" in caplog.text


def test_notify_logs_a_warning_on_timeout(caplog):
    cfg = {"notify": {"command": "sleep 5", "timeout_seconds": 0.1}}
    with caplog.at_level(logging.WARNING, logger="garden.notify"):
        notify(cfg, "T-1", "failed", "msg")
    assert "timed out" in caplog.text


def test_notify_test_returns_none_when_unconfigured():
    assert notify_test({}) is None


def test_notify_test_runs_with_the_synthetic_payload(tmp_path):
    out = tmp_path / "out.txt"
    cfg = {"notify": {"command": f'echo "$GARDEN_TASK_ID $GARDEN_STATUS" > {out}'}}
    ok, detail = notify_test(cfg)
    assert ok and detail == ""
    assert out.read_text().strip() == "DOCTOR-TEST doctor_test"


def test_notify_test_reports_the_failure_detail():
    ok, detail = notify_test({"notify": {"command": "echo failed-thing 1>&2; exit 2"}})
    assert not ok
    assert "exited 2" in detail and "failed-thing" in detail


# --------------------------------------------------------------------------- unquoted_message_warning
def test_warns_on_the_old_hand_built_json_example():
    cmd = 'curl --data "{\\"text\\": \\"[$GARDEN_STATUS] $GARDEN_TASK_ID: $GARDEN_MESSAGE $GARDEN_PR\\"}"  "$SLACK_WEBHOOK_URL"'
    warning = unquoted_message_warning(cmd)
    assert warning and "GARDEN_MESSAGE" in warning


def test_no_warning_once_quoted_with_jq():
    cmd = ('curl --data "$(jq -n --arg status "$GARDEN_STATUS" --arg task "$GARDEN_TASK_ID" '
           '--arg message "$GARDEN_MESSAGE" --arg pr "$GARDEN_PR" '
           '\'{text: "[\\($status)] \\($task): \\($message) \\($pr)"}\')" "$SLACK_WEBHOOK_URL"')
    assert unquoted_message_warning(cmd) is None


def test_no_warning_for_a_plain_non_json_command():
    assert unquoted_message_warning('bash -c \'echo "$GARDEN_TASK_ID $GARDEN_STATUS" >> notify.log\'') is None


def test_no_warning_when_unconfigured():
    assert unquoted_message_warning("") is None
