"""A python CI-check plugin used by the tests."""

import os


def analyse(ctx, spec):
    mode = os.environ.get("FAKE_CI_MODE", "fail")
    if mode == "flaky":
        return {"status": "flaky", "summary": "network timeout in job build", "details": "ETIMEDOUT",
                "retry_command": f"echo rerun > {os.environ['FAKE_CI_RERUN_FILE']}"}
    return {"status": "fail", "summary": f"pytest failed on {ctx['branch']}", "details": "FAILED tests/test_x.py::test_y - AssertionError"}
