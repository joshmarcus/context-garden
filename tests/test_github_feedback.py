"""feedback_since tells the garden's comments from a person's by the marker, not the login."""

import json

from garden.github import GARDEN_MARKER, GitHub


def _stub(monkeypatch, gh: GitHub, reviews, comments, issue_comments, login="josh"):
    def fake_gh(*args, input_=None):
        path = args[1] if len(args) > 1 else ""
        if path == "user":
            return login
        if path.endswith("/reviews"):
            return json.dumps(reviews)
        if path.endswith("/pulls/7/comments"):
            return json.dumps(comments)
        if path.endswith("/issues/7/comments"):
            return json.dumps(issue_comments)
        raise AssertionError(args)

    monkeypatch.setattr(gh, "_gh", fake_gh)
    gh.gh = "/usr/bin/gh"


def test_own_login_comments_count_but_garden_marked_ones_do_not(monkeypatch):
    gh = GitHub(use_gh=True)
    _stub(
        monkeypatch, gh,
        reviews=[{"user": {"login": "josh"}, "submitted_at": "2026-09-04T10:00:00Z", "state": "COMMENTED", "body": "looks close"}],
        comments=[{"user": {"login": "josh"}, "created_at": "2026-09-04T10:01:00Z", "body": "Can you add a screenshot?", "path": "a.py", "line": 3}],
        issue_comments=[
            {"user": {"login": "josh"}, "created_at": "2026-09-04T10:02:00Z", "body": f"Automated review: approve\n\n_garden review run r1_\n\n{GARDEN_MARKER}"},
            {"user": {"login": "josh"}, "created_at": "2026-09-04T10:03:00Z", "body": "please also update the README"},
            {"user": {"login": "ci[bot]"}, "created_at": "2026-09-04T10:04:00Z", "body": "build passed"},
        ],
    )
    fb = gh.feedback_since("o/r", 7, "2026-09-04T09:00:00Z")
    bodies = [i["body"] for i in fb.items]
    assert bodies == ["looks close", "Can you add a screenshot?", "please also update the README"]
    assert all(GARDEN_MARKER not in b for b in bodies)


def test_exclude_logins_and_since_still_apply(monkeypatch):
    gh = GitHub(use_gh=True)
    _stub(
        monkeypatch, gh, reviews=[],
        comments=[],
        issue_comments=[
            {"user": {"login": "josh"}, "created_at": "2026-09-04T08:00:00Z", "body": "old, before the dispatch"},
            {"user": {"login": "someone-else"}, "created_at": "2026-09-04T10:00:00Z", "body": "excluded by config"},
            {"user": {"login": "josh"}, "created_at": "2026-09-04T10:05:00Z", "body": "new and mine"},
        ],
    )
    fb = gh.feedback_since("o/r", 7, "2026-09-04T09:00:00Z", exclude_logins={"someone-else"})
    assert [i["body"] for i in fb.items] == ["new and mine"]


def test_comment_appends_marker_once(monkeypatch):
    gh = GitHub(use_gh=True)
    posted = []
    monkeypatch.setattr(gh, "_gh", lambda *a, input_=None: posted.append(input_) or "")
    gh.gh = "/usr/bin/gh"
    gh.comment("o/r", 7, "Pushed a revision round\n\n_garden run r2_")
    gh.comment("o/r", 7, f"already marked {GARDEN_MARKER}")
    assert posted[0].endswith("\n\n" + GARDEN_MARKER) and posted[0].count(GARDEN_MARKER) == 1
    assert posted[1].count(GARDEN_MARKER) == 1
