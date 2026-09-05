"""One shared contract for the two GitHub stand-ins (CG-204).

`tests.conftest.FakeGitHub` (the scheduler tests' scriptable stub) and
`garden.qa.sandbox.MemoryGitHub` (the `garden qa` pretend GitHub) both imitate
`garden.github.GitHub`. They used to drift apart silently — a fake missing a method only
blew up when a code path reached it. `GitHubLike` in github.py is now the written contract;
this module checks both fakes (and the real class) against it and runs the same scenarios
against both, so a gap shows up here rather than in a live loop."""

from __future__ import annotations

import pytest

from garden.github import GitHub, GitHubLike, PRInfo
from garden.qa.sandbox import MemoryGitHub
from tests.conftest import FakeGitHub

FAKES = [FakeGitHub, MemoryGitHub]


def _protocol_members() -> set[str]:
    """The public method/property names GitHubLike declares."""
    return {n for n in vars(GitHubLike) if not n.startswith("_")}


def test_real_github_satisfies_the_contract():
    members = _protocol_members()
    assert members, "GitHubLike declares no members"
    for name in members:
        assert hasattr(GitHub, name), f"GitHub is missing {name!r} declared on GitHubLike"


@pytest.mark.parametrize("fake_cls", FAKES, ids=lambda c: c.__name__)
def test_fake_implements_every_contract_member(fake_cls):
    fake = fake_cls()
    for name in _protocol_members():
        assert hasattr(fake, name), f"{fake_cls.__name__} is missing {name!r} from GitHubLike"
    # runtime_checkable Protocol: the instance carries every member (available included).
    assert isinstance(fake, GitHubLike)


@pytest.mark.parametrize("fake_cls", FAKES, ids=lambda c: c.__name__)
def test_open_find_get_comment_update(fake_cls):
    gh = fake_cls()
    assert gh.available and gh.is_authenticated() and gh.me() and gh.describe()

    pr = gh.create_pr("o/r", "garden/feature", "main", "Feature", "body")
    assert isinstance(pr, PRInfo) and pr.state == "OPEN" and pr.head == "garden/feature"

    assert gh.find_pr("o/r", "garden/feature").number == pr.number
    assert gh.find_pr("o/r", "does-not-exist") is None
    assert gh.get_pr("o/r", pr.number).number == pr.number

    gh.comment("o/r", pr.number, "a note")
    assert any("a note" in c for c in gh.issue_comments("o/r", pr.number))

    gh.update_pr("o/r", pr.number, title="Renamed")
    assert gh.get_pr("o/r", pr.number).title == "Renamed"

    assert gh.feedback_since("o/r", pr.number, "") is not None


@pytest.mark.parametrize("fake_cls", FAKES, ids=lambda c: c.__name__)
def test_draft_ready_and_close(fake_cls):
    gh = fake_cls()
    pr = gh.create_pr("o/r", "garden/draft", "main", "Draft", "body", draft=True)
    assert pr.is_draft
    gh.mark_ready("o/r", pr.number)
    assert not gh.get_pr("o/r", pr.number).is_draft

    gh.close_pr("o/r", pr.number)
    assert gh.get_pr("o/r", pr.number).state == "CLOSED"


@pytest.mark.parametrize("fake_cls", FAKES, ids=lambda c: c.__name__)
def test_merge_deletes_branch_and_closes_stacked_child(fake_cls):
    """A merge that deletes the branch closes any open PR still targeting it and records a
    base_ref_deleted timeline event — the incident behind CG-173, shared by both fakes."""
    gh = fake_cls()
    parent = gh.create_pr("o/r", "garden/parent", "main", "Parent", "body")
    child = gh.create_pr("o/r", "garden/child", "garden/parent", "Child", "body")

    assert gh.branch_exists("o/r", "garden/parent")
    gh.merge_pr("o/r", parent.number, delete_branch=True)

    assert gh.get_pr("o/r", parent.number).state == "MERGED"
    assert not gh.branch_exists("o/r", "garden/parent")
    assert gh.get_pr("o/r", child.number).state == "CLOSED"
    assert gh.base_ref_deleted("o/r", child.number)


@pytest.mark.parametrize("fake_cls", FAKES, ids=lambda c: c.__name__)
def test_reopen_and_refused_reopen(fake_cls):
    from garden.github import GitHubError

    gh = fake_cls()
    child = gh.create_pr("o/r", "garden/child", "garden/parent", "Child", "body")
    gh.close_pr("o/r", child.number)
    gh.reopen_pr("o/r", child.number)
    assert gh.get_pr("o/r", child.number).state == "OPEN"

    # GitHub can refuse a reopen (its base branch is gone); both fakes model that.
    gh.close_pr("o/r", child.number)
    gh.refuse_reopen.add(child.number)
    with pytest.raises(GitHubError):
        gh.reopen_pr("o/r", child.number)
