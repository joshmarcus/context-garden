from __future__ import annotations

from pathlib import Path

from scripts.run_onboarding_demo import demonstrate


def test_disposable_non_python_onboarding_journey(tmp_path: Path) -> None:
    evidence = demonstrate(tmp_path)

    assert evidence["classification"] == {
        "repeatable_compatibility": "PASS",
        "real_user_adoption": "UNPROVEN",
    }
    onboarding = evidence["onboarding"]
    assert onboarding["config_unchanged_after_journey"] is True
    assert onboarding["graph_validation"] == []
    assert onboarding["task"]["provenance"] == "onboard:TODO.md"
    assert onboarding["approval_status"] == "ready"
    assert all(code == 0 for code in evidence["automated_checks"].values())
    assert evidence["scripted_fixture_review"] == {"result": "accepted", "findings": []}
