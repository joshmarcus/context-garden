"""Minimal GitHub access: find/create PRs, read reviews, detect merges.

Uses the `gh` CLI when available and authenticated (it inherits the user's login),
otherwise the REST API with GITHUB_TOKEN / GH_TOKEN. Both paths return plain dicts so
the scheduler doesn't care which one is in use.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Any

import httpx

API = "https://api.github.com"


class GitHubError(Exception):
    pass


@dataclass
class PRInfo:
    number: int
    url: str
    state: str  # OPEN | MERGED | CLOSED
    title: str = ""
    head: str = ""
    base: str = ""
    review_decision: str = ""  # APPROVED | CHANGES_REQUESTED | REVIEW_REQUIRED | ""
    mergeable: str = ""
    checks: str = ""  # SUCCESS | FAILURE | PENDING | ""
    failed_checks: list[str] = field(default_factory=list)
    updated_at: str = ""
    body: str = ""
    head_sha: str = ""
    is_draft: bool = False
    node_id: str = ""


@dataclass
class Feedback:
    """Review feedback newer than a given timestamp, flattened to markdown."""

    items: list[dict[str, Any]] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.items)

    @property
    def changes_requested(self) -> bool:
        return any(i.get("state") == "CHANGES_REQUESTED" for i in self.items)

    def to_markdown(self) -> str:
        out = []
        for i in self.items:
            where = f" (`{i['path']}`" + (f":{i['line']}" if i.get("line") else "") + ")" if i.get("path") else ""
            kind = i.get("kind", "comment")
            state = f" [{i['state']}]" if i.get("state") else ""
            out.append(f"- **{i.get('author', '?')}** {kind}{state}{where}:\n\n  " + i.get("body", "").strip().replace("\n", "\n  "))
        return "\n\n".join(out)


def repo_slug_from_remote(url: str) -> str | None:
    m = re.search(r"github\.com[:/]([^/]+)/([^/]+?)(?:\.git)?/?$", url.strip())
    return f"{m.group(1)}/{m.group(2)}" if m else None


class GitHub:
    def __init__(self, use_gh: bool = True, token: str | None = None, bot_logins: list[str] | None = None):
        self.token = token or os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
        self.gh = shutil.which("gh") if use_gh else None
        self.bot_logins = set(bot_logins or [])
        self._me: str | None = None

    @property
    def available(self) -> bool:
        return bool(self.gh or self.token)

    def describe(self) -> str:
        if self.gh:
            return f"gh CLI ({self.gh})"
        if self.token:
            return "REST API with token"
        return "unavailable (install gh or set GITHUB_TOKEN)"

    # ---- low level ---------------------------------------------------------
    def _gh(self, *args: str, input_: str | None = None) -> str:
        assert self.gh
        proc = subprocess.run([self.gh, *args], capture_output=True, text=True, input=input_)
        if proc.returncode != 0:
            raise GitHubError(proc.stderr.strip() or f"gh {' '.join(args)} failed")
        return proc.stdout

    def _rest(self, method: str, path: str, **kw: Any) -> Any:
        if not self.token:
            raise GitHubError("no GitHub token; install gh or set GITHUB_TOKEN")
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        r = httpx.request(method, API + path, headers=headers, timeout=30, **kw)
        if r.status_code >= 400:
            raise GitHubError(f"{method} {path}: {r.status_code} {r.text[:300]}")
        return r.json() if r.content else None

    def me(self) -> str:
        if self._me is None:
            try:
                if self.gh:
                    self._me = self._gh("api", "user", "--jq", ".login").strip()
                else:
                    self._me = str(self._rest("GET", "/user").get("login", ""))
            except GitHubError:
                self._me = ""
        return self._me

    # ---- PRs ---------------------------------------------------------------
    def find_pr(self, slug: str, head_branch: str) -> PRInfo | None:
        if self.gh:
            out = self._gh(
                "pr", "list", "-R", slug, "--head", head_branch, "--state", "all",
                "--json", "number,url,state,title,headRefName,baseRefName,reviewDecision,mergeable,updatedAt,isDraft",
                "--limit", "5",
            )
            prs = json.loads(out or "[]")
            if not prs:
                return None
            prs.sort(key=lambda p: p.get("updatedAt", ""), reverse=True)
            p = prs[0]
            return PRInfo(
                number=p["number"], url=p["url"], state=p["state"], title=p.get("title", ""),
                head=p.get("headRefName", ""), base=p.get("baseRefName", ""),
                review_decision=p.get("reviewDecision") or "", mergeable=p.get("mergeable") or "",
                updated_at=p.get("updatedAt", ""), is_draft=bool(p.get("isDraft")),
            )
        owner = slug.split("/")[0]
        prs = self._rest("GET", f"/repos/{slug}/pulls", params={"head": f"{owner}:{head_branch}", "state": "all", "per_page": 5})
        if not prs:
            return None
        prs.sort(key=lambda p: p.get("updated_at", ""), reverse=True)
        return self._pr_from_rest(prs[0])

    def get_pr(self, slug: str, number: int) -> PRInfo:
        if self.gh:
            out = self._gh(
                "pr", "view", str(number), "-R", slug,
                "--json", "number,url,state,title,body,headRefName,headRefOid,baseRefName,reviewDecision,mergeable,updatedAt,statusCheckRollup,isDraft,id",
            )
            p = json.loads(out)
            rollup = p.get("statusCheckRollup") or []
            return PRInfo(
                number=p["number"], url=p["url"], state=p["state"], title=p.get("title", ""),
                head=p.get("headRefName", ""), base=p.get("baseRefName", ""),
                review_decision=p.get("reviewDecision") or "", mergeable=p.get("mergeable") or "",
                checks=_rollup_state(rollup), failed_checks=_rollup_failed(rollup), updated_at=p.get("updatedAt", ""),
                body=p.get("body") or "", head_sha=p.get("headRefOid") or "", is_draft=bool(p.get("isDraft")), node_id=str(p.get("id") or ""),
            )
        p = self._rest("GET", f"/repos/{slug}/pulls/{number}")
        info = self._pr_from_rest(p)
        info.body = p.get("body") or ""
        info.head_sha = (p.get("head") or {}).get("sha", "")
        if info.head_sha:
            try:
                runs = self._rest("GET", f"/repos/{slug}/commits/{info.head_sha}/check-runs", params={"per_page": 100}) or {}
                rollup = [{"name": c.get("name"), "conclusion": c.get("conclusion"), "state": c.get("status")}
                          for c in runs.get("check_runs", [])]
                info.checks = _rollup_state(rollup)
                info.failed_checks = _rollup_failed(rollup)
            except GitHubError:
                pass
        try:
            reviews = self._rest("GET", f"/repos/{slug}/pulls/{number}/reviews", params={"per_page": 100}) or []
            latest: dict[str, str] = {}
            for r in reviews:
                if r.get("state") in ("APPROVED", "CHANGES_REQUESTED"):
                    latest[r["user"]["login"]] = r["state"]
            if "CHANGES_REQUESTED" in latest.values():
                info.review_decision = "CHANGES_REQUESTED"
            elif "APPROVED" in latest.values():
                info.review_decision = "APPROVED"
        except GitHubError:
            pass
        return info

    def _pr_from_rest(self, p: dict[str, Any]) -> PRInfo:
        state = "MERGED" if p.get("merged_at") else p.get("state", "open").upper()
        return PRInfo(
            number=p["number"], url=p["html_url"], state=state, title=p.get("title", ""),
            head=p.get("head", {}).get("ref", ""), base=p.get("base", {}).get("ref", ""),
            mergeable=("MERGEABLE" if p.get("mergeable") else "") if p.get("mergeable") is not None else "",
            updated_at=p.get("updated_at", ""), is_draft=bool(p.get("draft")), node_id=str(p.get("node_id") or ""),
        )

    def create_pr(self, slug: str, head: str, base: str, title: str, body: str, draft: bool = False,
                  reviewers: list[str] | None = None) -> PRInfo:
        if self.gh:
            args = ["pr", "create", "-R", slug, "--head", head, "--base", base, "--title", title, "--body-file", "-"]
            if draft:
                args.append("--draft")
            for r in reviewers or []:
                args += ["--reviewer", r]
            url = self._gh(*args, input_=body).strip().splitlines()[-1]
            m = re.search(r"/pull/(\d+)", url)
            return PRInfo(number=int(m.group(1)) if m else 0, url=url, state="OPEN", title=title, head=head, base=base, is_draft=draft)
        p = self._rest("POST", f"/repos/{slug}/pulls", json={"title": title, "body": body, "head": head, "base": base, "draft": draft})
        if reviewers:
            try:
                self._rest("POST", f"/repos/{slug}/pulls/{p['number']}/requested_reviewers", json={"reviewers": reviewers})
            except GitHubError:
                pass
        return self._pr_from_rest(p)

    def feedback_since(self, slug: str, number: int, since_iso: str, exclude_logins: set[str] | None = None) -> Feedback:
        """Reviews, review (line) comments and issue comments newer than `since_iso`."""
        exclude = set(exclude_logins or set()) | self.bot_logins
        me = self.me()
        if me:
            exclude.add(me)
        items: list[dict[str, Any]] = []

        def keep(author: str, created: str, body: str) -> bool:
            if not body.strip():
                return False
            if author in exclude or author.endswith("[bot]"):
                return False
            return created > since_iso if since_iso else True

        if self.gh:
            reviews = json.loads(self._gh("api", f"repos/{slug}/pulls/{number}/reviews", "--paginate") or "[]")
            comments = json.loads(self._gh("api", f"repos/{slug}/pulls/{number}/comments", "--paginate") or "[]")
            issue_comments = json.loads(self._gh("api", f"repos/{slug}/issues/{number}/comments", "--paginate") or "[]")
        else:
            reviews = self._rest("GET", f"/repos/{slug}/pulls/{number}/reviews", params={"per_page": 100}) or []
            comments = self._rest("GET", f"/repos/{slug}/pulls/{number}/comments", params={"per_page": 100}) or []
            issue_comments = self._rest("GET", f"/repos/{slug}/issues/{number}/comments", params={"per_page": 100}) or []
        for r in reviews:
            author = r.get("user", {}).get("login", "")
            created = r.get("submitted_at", "") or ""
            body = r.get("body", "") or ""
            state = r.get("state", "")
            if state == "CHANGES_REQUESTED" and (created > since_iso if since_iso else True) and author not in exclude:
                items.append({"kind": "review", "state": state, "author": author, "body": body or "(changes requested)", "created": created})
            elif keep(author, created, body):
                items.append({"kind": "review", "state": state, "author": author, "body": body, "created": created})
        for c in comments:
            author = c.get("user", {}).get("login", "")
            if keep(author, c.get("created_at", ""), c.get("body", "")):
                items.append({"kind": "line comment", "author": author, "body": c["body"], "path": c.get("path"), "line": c.get("line") or c.get("original_line"), "created": c["created_at"]})
        for c in issue_comments:
            author = c.get("user", {}).get("login", "")
            if keep(author, c.get("created_at", ""), c.get("body", "")):
                items.append({"kind": "comment", "author": author, "body": c["body"], "created": c["created_at"]})
        items.sort(key=lambda i: i.get("created", ""))
        return Feedback(items=items)

    def update_pr(self, slug: str, number: int, title: str = "", body: str = "", base: str = "") -> None:
        if not title and not body and not base:
            return
        if self.gh:
            args = ["pr", "edit", str(number), "-R", slug]
            if title:
                args += ["--title", title]
            if base:
                args += ["--base", base]
            if body:
                args += ["--body-file", "-"]
            self._gh(*args, input_=body if body else None)
            return
        payload: dict[str, str] = {}
        if title:
            payload["title"] = title
        if body:
            payload["body"] = body
        if base:
            payload["base"] = base
        self._rest("PATCH", f"/repos/{slug}/pulls/{number}", json=payload)

    def mark_ready(self, slug: str, number: int) -> None:
        """Convert a draft PR to ready for review (the human's triage step)."""
        if self.gh:
            self._gh("pr", "ready", str(number), "-R", slug)
            return
        pr = self._rest("GET", f"/repos/{slug}/pulls/{number}")
        node = pr.get("node_id")
        if not node:
            raise GitHubError("PR has no node id")
        q = "mutation($id: ID!) { markPullRequestReadyForReview(input: {pullRequestId: $id}) { pullRequest { isDraft } } }"
        out = self._rest("POST", "/graphql", json={"query": q, "variables": {"id": node}})
        if out and out.get("errors"):
            raise GitHubError(str(out["errors"])[:300])

    def close_pr(self, slug: str, number: int) -> None:
        if self.gh:
            self._gh("pr", "close", str(number), "-R", slug)
        else:
            self._rest("PATCH", f"/repos/{slug}/pulls/{number}", json={"state": "closed"})

    def comment(self, slug: str, number: int, body: str) -> None:
        if self.gh:
            self._gh("pr", "comment", str(number), "-R", slug, "--body-file", "-", input_=body)
        else:
            self._rest("POST", f"/repos/{slug}/issues/{number}/comments", json={"body": body})


def _rollup_failed(rollup: list[dict[str, Any]]) -> list[str]:
    out = []
    for c in rollup or []:
        s = (c.get("conclusion") or c.get("state") or "").upper()
        if s in ("FAILURE", "ERROR", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED"):
            out.append(str(c.get("name") or c.get("context") or c.get("workflowName") or "check"))
    return out


def _rollup_state(rollup: list[dict[str, Any]]) -> str:
    if not rollup:
        return ""
    states = set()
    for c in rollup:
        s = (c.get("conclusion") or c.get("state") or c.get("status") or "").upper()
        states.add(s)
    if any(s in ("FAILURE", "ERROR", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED") for s in states):
        return "FAILURE"
    if any(s in ("", "PENDING", "IN_PROGRESS", "QUEUED", "EXPECTED", "WAITING") for s in states):
        return "PENDING"
    return "SUCCESS"
