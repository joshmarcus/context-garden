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
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urlparse

import httpx

API = "https://api.github.com"

# A bot comment matching one of these (case-insensitive substring) is a notice, not a
# finding: a status line with nothing to act on. Overridden by a finding marker (a
# `[P1]`/`[P2]` badge) or a diff-line comment, either of which means the bot did point at
# code. A work setting can add its own bot's phrasing via `github.bot_notice_patterns`.
DEFAULT_BOT_NOTICE_PATTERNS = [
    "usage limit",
    "no issues",
    "looks good",
    "reviewed and found nothing",
]

FINDING_MARKER_RE = re.compile(r"\[P\d+\]")


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
    # Comments that were skipped: a bot notice with no finding (`reason: notice`), or a
    # comment by an author the garden does not trust (`reason: untrusted`). Not feedback,
    # but worth a line in the task log so a human can see what was skipped and why.
    ignored: list[dict[str, Any]] = field(default_factory=list)

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
            if str(i.get("author", "")).endswith("[bot]"):
                kind = f"{kind} from a bot"
            state = f" [{i['state']}]" if i.get("state") else ""
            out.append(f"- **{i.get('author', '?')}** {kind}{state}{where}:\n\n  " + i.get("body", "").strip().replace("\n", "\n  "))
        return "\n\n".join(out)


def repo_slug_from_remote(url: str, host: str = "github.com") -> str | None:
    """Return an ``owner/repo`` only for an unambiguous remote on ``host``.

    Enterprise remotes occur in HTTPS, SSH URL, and conventional SCP forms.  Rejecting
    credential-bearing URLs and unexpected hosts keeps a configured product from
    borrowing a credential or API route intended for another server. SSH transport
    usernames and explicit SSH ports remain part of the repository URL.
    """
    expected = host.lower().rstrip(".")
    value = url.strip()
    github_ssh_prefix = "ssh://git@ssh.github.com:443/"
    if expected == "github.com" and value.lower().startswith(github_ssh_prefix):
        value = "ssh://git@github.com/" + value[len(github_ssh_prefix):]
    patterns = (
        r"https://(?P<host>[^/@:]+)(?::443)?/(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?/?$",
        r"ssh://(?:[^@/:]+@)?(?P<host>[^/:]+)(?P<port>:\d+)?/(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?/?$",
        r"(?:[^@:]+@)?(?P<host>[^:]+):(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?/?$",
    )
    for pattern in patterns:
        match = re.fullmatch(pattern, value, flags=re.IGNORECASE)
        if not match:
            continue
        port = match.groupdict().get("port")
        if port:
            port_number = port[1:].lstrip("0") or "0"
            if len(port_number) > 5 or not 0 < int(port_number) <= 65535:
                continue
        remote_host = match["host"].lower().rstrip(".")
        if pattern.startswith("ssh://") and expected == "github.com":
            # GitHub documents ssh.github.com:443 for networks where port 22 is
            # blocked. It is the public github.com route, not a separate host.
            valid_host = (
                remote_host == "github.com" and (not port or port == ":22")
            ) or (remote_host == "ssh.github.com" and port == ":443")
        else:
            # The documented public SSH alias is never an enterprise API host.
            valid_host = remote_host == expected and remote_host != "ssh.github.com"
        if valid_host:
            return f"{match['owner']}/{match['repo']}"
    return None


def is_git_remote_url(value: str) -> bool:
    """Whether *value* is an HTTP/SSH Git URL, including SCP-style remotes.

    SCP syntax permits an omitted transport username (``host:path``).  Check a
    Windows drive spelling first, because its colon would otherwise look like
    that form on hosts which support Windows-path configuration.
    """
    if re.match(r"^[a-z]:[\\\\/]", value, re.IGNORECASE):
        return False
    return bool(re.match(
        r"(?:[a-z][a-z0-9+.-]*://|(?:[a-z0-9._-]+@)?[a-z0-9.-]+:[^\s:@])",
        value,
        re.IGNORECASE,
    ))


def pull_request_number(url: str, slug: str, host: str = "github.com") -> int | None:
    """Return a GitHub PR number only when *url* identifies this repository.

    A PR number is meaningful only within its repository.  Validating the complete
    public GitHub URL before looking it up prevents a same-numbered PR in the
    configured repository from being mistaken for an operator-supplied external PR.
    """
    try:
        parsed = urlparse(url)
        port = parsed.port
    except ValueError:
        return None
    # A PR link is an identifier, not a request URL.  Credentials and URL decorations
    # have no identity meaning here and must not be copied into task or run records.
    if (parsed.scheme != "https" or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment
            or parsed.hostname != host.lower().rstrip(".") or port not in (None, 443)):
        return None
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) != 4 or parts[2] != "pull" or "/".join(parts[:2]).lower() != slug.lower():
        return None
    try:
        number = int(parts[3])
    except ValueError:
        return None
    return number if number > 0 else None


def is_safe_pr_url(url: str) -> bool:
    """Whether a provider-returned PR URL is safe to retain as an identifier.

    The CLI additionally requires a canonical URL for the configured repository
    before it asks a provider for PR details.  Scheduler callers may instead be
    handing back the provider's own identity URL, whose path and host need not
    be the browser URL shape.  It still must not contain URL components that
    could carry credentials or alter its identity.
    """
    try:
        parsed = urlparse(url)
        port = parsed.port
    except ValueError:
        return False
    return bool(
        parsed.scheme == "https"
        and parsed.hostname
        and parsed.username is None
        and parsed.password is None
        and not parsed.params
        and not parsed.query
        and not parsed.fragment
        and port in (None, 443)
    )


# Appended to every comment the garden posts, so its own comments can be told apart from a
# person's even when both use the same GitHub login. Invisible on GitHub (an HTML comment).
GARDEN_MARKER = "<!-- context-garden -->"


def mark_garden_comment(body: str, run_id: str = "") -> str:
    """Prefix a comment with a visible marker identifying it as from context-garden.

    The visible marker helps readers see that this is an automated comment. The hidden
    GARDEN_MARKER is used for programmatic detection.
    """
    marker = "> **🌱 context-garden**"
    if run_id:
        marker += f" · run `{run_id}`"
    return marker + "\n\n" + body


@runtime_checkable
class GitHubLike(Protocol):
    """The GitHub surface the scheduler, web and helpers call on `self.github`.

    Both the real `GitHub` and the two test stand-ins — `tests.conftest.FakeGitHub` and
    `garden.qa.sandbox.MemoryGitHub` — implement this contract; `tests/test_github_fakes.py`
    checks that and runs the same scenarios against both fakes so neither drifts from the
    other (the incident behind CG-204: a fake missing `reopen_pr`/`branch_exists` raised
    AttributeError only on the base-deleted path). Keep this in step with the methods the
    scheduler actually calls; a new call site adds its method here and both fakes must follow."""

    @property
    def available(self) -> bool: ...

    def describe(self) -> str: ...
    def me(self) -> str: ...
    def is_authenticated(self) -> bool: ...
    def find_pr(self, slug: str, head_branch: str) -> PRInfo | None: ...
    def get_pr(self, slug: str, number: int) -> PRInfo: ...
    def create_pr(self, slug: str, head: str, base: str, title: str, body: str,
                  draft: bool = ..., reviewers: list[str] | None = ...) -> PRInfo: ...
    def feedback_since(self, slug: str, number: int, since_iso: str,
                       exclude_logins: set[str] | None = ...) -> Feedback: ...
    def update_pr(self, slug: str, number: int, title: str = ..., body: str = ..., base: str = ...) -> None: ...
    def mark_ready(self, slug: str, number: int) -> None: ...
    def close_pr(self, slug: str, number: int) -> None: ...
    def reopen_pr(self, slug: str, number: int) -> None: ...
    def branch_exists(self, slug: str, branch: str) -> bool: ...
    def base_ref_deleted(self, slug: str, number: int) -> bool: ...
    def merge_pr(self, slug: str, number: int, method: str = ..., delete_branch: bool = ...,
                 expected_head: str = ...) -> None: ...
    def delete_branch(self, slug: str, branch: str) -> None: ...
    def issue_comments(self, slug: str, number: int) -> list[str]: ...
    def comment(self, slug: str, number: int, body: str) -> None: ...


class GitHub:
    def __init__(
        self,
        use_gh: bool = True,
        token: str | None = None,
        bot_logins: list[str] | None = None,
        bot_notice_patterns: list[str] | None = None,
        trusted_authors: list[str] | None = None,
        trusted_bots: list[str] | None = None,
        host: str = "github.com",
        api_base: str = "",
        token_env: str = "",
    ):
        self.host = host.lower().rstrip(".")
        if not re.fullmatch(r"[a-z0-9][a-z0-9.-]*", self.host):
            raise ValueError(f"invalid GitHub host: {host!r}")
        self.api_base = (api_base or (API if self.host == "github.com" else f"https://{self.host}/api/v3")).rstrip("/")
        parsed_api = urlparse(self.api_base)
        if parsed_api.scheme != "https" or not parsed_api.netloc:
            raise ValueError("github api_base must be an HTTPS URL")
        if api_base and (
            parsed_api.hostname != self.host or parsed_api.username or parsed_api.password
            or parsed_api.query or parsed_api.fragment or parsed_api.port not in (None, 443)
        ):
            raise ValueError("github api_base must be an HTTPS URL for the configured GitHub host")
        # A product that names a token environment has deliberately scoped its
        # credential. Do not fall through to a public/default token if it is missing.
        self.token = token or (os.environ.get(token_env) if token_env else (os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")))
        self.gh = shutil.which("gh") if use_gh else None
        self.bot_logins = set(bot_logins or [])
        self.bot_notice_patterns = [
            str(p).lower() for p in (bot_notice_patterns if bot_notice_patterns is not None else DEFAULT_BOT_NOTICE_PATTERNS)
        ]
        self.trusted_authors = {str(a).strip() for a in (trusted_authors or []) if str(a).strip()}
        self.trusted_bots = {str(b).strip() for b in (trusted_bots or []) if str(b).strip()}
        self._me: str | None = None

    @property
    def available(self) -> bool:
        return bool(self.gh or self.token)

    def describe(self) -> str:
        if self.gh:
            return f"gh CLI ({self.gh}) for {self.host}"
        if self.token:
            return f"REST API with token for {self.host}"
        return f"unavailable for {self.host} (install gh or set a token)"

    # ---- low level ---------------------------------------------------------
    def _gh(self, *args: str, input_: str | None = None) -> str:
        assert self.gh
        # ``--hostname`` belongs to ``gh api``; PR commands select their host through
        # the fully-qualified ``--repo HOST/OWNER/REPO`` argument instead.
        command = [self.gh, *args]
        if args and args[0] == "api":
            command += ["--hostname", self.host]
        proc = subprocess.run(command, capture_output=True, text=True, input=input_)
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
        base = self.api_base
        if path == "/graphql" and self.host != "github.com":
            # Enterprise GraphQL is a sibling of the REST v3 endpoint.
            base = base.removesuffix("/v3")
        r = httpx.request(method, base + path, headers=headers, timeout=30, **kw)
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

    def is_trusted(self, author: str) -> bool:
        """Whether a PR comment by `author` may become a worker prompt.

        Trusted: the login the garden authenticates as (the person driving it) and a login in
        `github.trusted_authors` (the scheduler adds `github.reviewers`). A `[bot]` account is
        trusted only when `github.trusted_bots` names it — a review app the owner installed and
        opted in by login; the default is empty, so an unlisted app relaying untrusted comment
        text cannot steer a worker. Anyone else who can comment on a PR is not trusted: on a
        public repo that is everyone, and a comment is text a worker would carry out."""
        author = (author or "").strip()
        if not author:
            return False
        if author.endswith("[bot]"):
            return author in self.trusted_bots
        if author in self.trusted_authors:
            return True
        return author == self.me()

    def is_authenticated(self) -> bool:
        if self.gh:
            try:
                subprocess.run([self.gh, "auth", "status", "--hostname", self.host], capture_output=True, text=True, check=True)
                return True
            except (subprocess.CalledProcessError, FileNotFoundError):
                return False
        elif self.token:
            return True
        return False

    # ---- PRs ---------------------------------------------------------------
    def _repo(self, slug: str) -> str:
        return f"{self.host}/{slug}"

    def find_pr(self, slug: str, head_branch: str) -> PRInfo | None:
        if self.gh:
            out = self._gh(
                "pr", "list", "-R", self._repo(slug), "--head", head_branch, "--state", "all",
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
                "pr", "view", str(number), "-R", self._repo(slug),
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
            args = ["pr", "create", "-R", self._repo(slug), "--head", head, "--base", base, "--title", title, "--body-file", "-"]
            if draft:
                args.append("--draft")
            for r in reviewers or []:
                args += ["--reviewer", r]
            url = self._gh(*args, input_=body).strip().splitlines()[-1]
            number = pull_request_number(url, slug, self.host)
            if number is None:
                raise GitHubError(f"gh returned a PR URL outside {self.host}/{slug}")
            return PRInfo(number=number, url=url, state="OPEN", title=title, head=head, base=base, is_draft=draft)
        p = self._rest("POST", f"/repos/{slug}/pulls", json={"title": title, "body": body, "head": head, "base": base, "draft": draft})
        if reviewers:
            try:
                self._rest("POST", f"/repos/{slug}/pulls/{p['number']}/requested_reviewers", json={"reviewers": reviewers})
            except GitHubError:
                pass
        return self._pr_from_rest(p)

    def feedback_since(self, slug: str, number: int, since_iso: str, exclude_logins: set[str] | None = None) -> Feedback:
        """Reviews, review (line) comments and issue comments newer than `since_iso`, from
        trusted authors only (see `is_trusted`); the rest is returned as `ignored`."""
        # The garden's own comments are recognised by GARDEN_MARKER, not by login: the person
        # driving the garden usually is the login `gh` uses, and their comments must count. A
        # bot counts only when `github.trusted_bots` names it (see `is_trusted`); `bot_logins`
        # (`github.bot_logins`) drops accounts entirely, before they are even logged as ignored.
        exclude = set(exclude_logins or set()) | self.bot_logins
        items: list[dict[str, Any]] = []
        ignored: list[dict[str, Any]] = []

        def newer(created: str) -> bool:
            return created > since_iso if since_iso else True

        def keep(author: str, created: str, body: str) -> bool:
            if not body.strip() or GARDEN_MARKER in body:
                return False
            if author in exclude:
                return False
            return newer(created)

        def untrusted(author: str, created: str, body: str) -> bool:
            """Record a comment whose author may not prompt a worker; True when it was."""
            if self.is_trusted(author):
                return False
            ignored.append({"author": author, "body": body, "created": created, "reason": "untrusted"})
            return True

        def is_notice(author: str, body: str) -> bool:
            """A bot comment with no finding: a notice pattern match, unless it points at code."""
            if not author.endswith("[bot]") or FINDING_MARKER_RE.search(body):
                return False
            low = body.lower()
            return any(p in low for p in self.bot_notice_patterns)

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
            if state == "CHANGES_REQUESTED" and newer(created) and author not in exclude:
                if not untrusted(author, created, body or "(changes requested)"):
                    items.append({"kind": "review", "state": state, "author": author, "body": body or "(changes requested)", "created": created})
            elif keep(author, created, body) and not untrusted(author, created, body):
                if is_notice(author, body):
                    ignored.append({"author": author, "body": body, "created": created, "reason": "notice"})
                else:
                    items.append({"kind": "review", "state": state, "author": author, "body": body, "created": created})
        for c in comments:
            author = c.get("user", {}).get("login", "")
            if keep(author, c.get("created_at", ""), c.get("body", "")) and not untrusted(author, c["created_at"], c["body"]):
                # a comment on a diff line always points at code, notice or not
                items.append({"kind": "line comment", "author": author, "body": c["body"], "path": c.get("path"), "line": c.get("line") or c.get("original_line"), "created": c["created_at"]})
        for c in issue_comments:
            author = c.get("user", {}).get("login", "")
            body = c.get("body", "")
            if keep(author, c.get("created_at", ""), body) and not untrusted(author, c["created_at"], body):
                if is_notice(author, body):
                    ignored.append({"author": author, "body": body, "created": c["created_at"], "reason": "notice"})
                else:
                    items.append({"kind": "comment", "author": author, "body": body, "created": c["created_at"]})
        items.sort(key=lambda i: i.get("created", ""))
        ignored.sort(key=lambda i: i.get("created", ""))
        return Feedback(items=items, ignored=ignored)

    def update_pr(self, slug: str, number: int, title: str = "", body: str = "", base: str = "") -> None:
        if not title and not body and not base:
            return
        if self.gh:
            args = ["pr", "edit", str(number), "-R", self._repo(slug)]
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
            self._gh("pr", "ready", str(number), "-R", self._repo(slug))
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
            self._gh("pr", "close", str(number), "-R", self._repo(slug))
        else:
            self._rest("PATCH", f"/repos/{slug}/pulls/{number}", json={"state": "closed"})

    def reopen_pr(self, slug: str, number: int) -> None:
        """Reopen a closed PR. Raises GitHubError if GitHub refuses (e.g. the base branch is
        gone, so the caller falls back to opening a fresh PR from the same head branch)."""
        if self.gh:
            self._gh("pr", "reopen", str(number), "-R", self._repo(slug))
        else:
            self._rest("PATCH", f"/repos/{slug}/pulls/{number}", json={"state": "open"})

    def branch_exists(self, slug: str, branch: str) -> bool:
        """Whether `branch` still exists on the remote."""
        try:
            self._rest_or_gh_branch(slug, branch)
            return True
        except GitHubError:
            return False

    def _rest_or_gh_branch(self, slug: str, branch: str) -> Any:
        if self.gh:
            return self._gh("api", f"repos/{slug}/branches/{branch}")
        return self._rest("GET", f"/repos/{slug}/branches/{branch}")

    def base_ref_deleted(self, slug: str, number: int) -> bool:
        """Whether GitHub closed this PR because its base branch was deleted: the PR timeline
        carries a `base_ref_deleted` event. Best-effort — False when the timeline can't be read."""
        try:
            if self.gh:
                events = json.loads(self._gh("api", f"repos/{slug}/issues/{number}/timeline", "--paginate") or "[]")
            else:
                events = self._rest("GET", f"/repos/{slug}/issues/{number}/timeline", params={"per_page": 100}) or []
        except GitHubError:
            return False
        return any(isinstance(e, dict) and e.get("event") == "base_ref_deleted" for e in events)

    def merge_pr(self, slug: str, number: int, method: str = "squash", delete_branch: bool = True,
                 expected_head: str = "") -> None:
        """Merge an open PR. `method` is squash | merge | rebase. Raises GitHubError if GitHub
        refuses the merge (not mergeable, failing required checks, blocked by a review)."""
        if method not in ("squash", "merge", "rebase"):
            method = "squash"
        if self.gh:
            args = ["pr", "merge", str(number), "-R", self._repo(slug), f"--{method}"]
            if expected_head:
                args += ["--match-head-commit", expected_head]
            if delete_branch:
                args.append("--delete-branch")
            self._gh(*args)
            return
        payload = {"merge_method": method}
        if expected_head:
            payload["sha"] = expected_head
        self._rest("PUT", f"/repos/{slug}/pulls/{number}/merge", json=payload)
        if delete_branch:
            try:
                pr = self._rest("GET", f"/repos/{slug}/pulls/{number}")
                ref = (pr.get("head") or {}).get("ref", "")
                if ref:
                    self.delete_branch(slug, ref)
            except GitHubError:
                pass

    def delete_branch(self, slug: str, branch: str) -> None:
        """Delete a branch on the remote. Best-effort: a branch already gone (or never
        pushed) is not an error worth surfacing."""
        try:
            if self.gh:
                self._gh("api", "-X", "DELETE", f"repos/{slug}/git/refs/heads/{branch}")
            else:
                self._rest("DELETE", f"/repos/{slug}/git/refs/heads/{branch}")
        except GitHubError:
            pass

    def issue_comments(self, slug: str, number: int) -> list[str]:
        """Every issue-comment body on a PR, oldest first. Best-effort: [] on error."""
        try:
            if self.gh:
                data = json.loads(self._gh("api", f"repos/{slug}/issues/{number}/comments", "--paginate") or "[]")
            else:
                data = self._rest("GET", f"/repos/{slug}/issues/{number}/comments", params={"per_page": 100}) or []
        except GitHubError:
            return []
        return [str(c.get("body") or "") for c in data]

    def comment(self, slug: str, number: int, body: str) -> None:
        if GARDEN_MARKER not in body:
            body = body.rstrip() + "\n\n" + GARDEN_MARKER
        if self.gh:
            self._gh("pr", "comment", str(number), "-R", self._repo(slug), "--body-file", "-", input_=body)
        else:
            self._rest("POST", f"/repos/{slug}/issues/{number}/comments", json={"body": body})


class RepositorySlug(str):
    """A repository slug that carries its configured GitHub host for routing."""

    def __new__(cls, slug: str, host: str):
        value = super().__new__(cls, slug)
        value.host = host.lower().rstrip(".")
        return value

    def __getnewargs__(self) -> tuple[str, str]:
        # State snapshots copy values before flushing them; preserve the route when
        # copy/deepcopy or pickle reconstruct this immutable string subclass.
        return str(self), self.host


class GitHubRouter:
    """Route repository operations to the GitHub client configured for that repository.

    Every repository operation includes a slug as its first argument. Keeping the route
    here makes it difficult for a newly added scheduler operation to accidentally fall
    back to whichever host happens to be active in ``gh``.
    """

    def __init__(self, default: GitHub, routes: dict[tuple[str, str] | str, GitHub]):
        self.default = default
        self.routes = {
            ((key[0] if isinstance(key, tuple) else client.host).lower().rstrip("."),
             (key[1] if isinstance(key, tuple) else key).lower()): client
            for key, client in routes.items()
        }
        self._legacy_routes = {
            slug: clients[0]
            for slug, clients in self._routes_by_slug().items()
            if len(clients) == 1
        }

    def _routes_by_slug(self) -> dict[str, list[GitHub]]:
        grouped: dict[str, list[GitHub]] = {}
        for (_, slug), client in self.routes.items():
            grouped.setdefault(slug, []).append(client)
        return grouped

    @property
    def available(self) -> bool:
        return self.default.available or any(client.available for client in self.routes.values())

    def describe(self) -> str:
        return self.default.describe()

    def me(self) -> str:
        return self.default.me()

    def is_authenticated(self) -> bool:
        return self.default.is_authenticated()

    def __getattr__(self, name: str) -> Any:
        """Forward slug-first GitHub operations to their configured client."""
        default_method = getattr(self.default, name)
        if not callable(default_method):
            return default_method

        def routed(slug: str, *args: Any, **kwargs: Any) -> Any:
            host = getattr(slug, "host", "")
            key = slug.lower()
            if host:
                client = self.routes.get((host, key))
                if client is None and host == getattr(self.default, "host", "github.com"):
                    client = self.default
                if client is None:
                    raise GitHubError(f"no GitHub client configured for host {host!r} and repository {slug!r}")
            else:
                if key in self._routes_by_slug() and key not in self._legacy_routes:
                    raise GitHubError(f"ambiguous GitHub host for repository {slug!r}; supply its explicit host")
                client = self._legacy_routes.get(key, self.default)
            return getattr(client, name)(slug, *args, **kwargs)

        return routed


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
