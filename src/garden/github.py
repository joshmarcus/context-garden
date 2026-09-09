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
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urlencode, urlparse

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
    merge_commit_sha: str = ""
    # The repository which owns ``head``.  An empty value is retained for older
    # providers, but providers that expose it let attachment reject fork heads:
    # a scheduler cannot safely revise a branch it cannot push.
    head_repo: str = ""
    is_draft: bool = False
    node_id: str = ""
    author: str = ""


@dataclass
class Feedback:
    """Review feedback newer than a given timestamp, flattened to markdown."""

    items: list[dict[str, Any]] = field(default_factory=list)
    # Comments that were skipped: a bot notice with no finding (`reason: notice`), or a
    # comment by an author the garden does not trust (`reason: untrusted`). Not feedback,
    # but worth a line in the task log so a human can see what was skipped and why.
    ignored: list[dict[str, Any]] = field(default_factory=list)
    # The newest provider timestamp completely read to produce this response.  It is
    # deliberately independent of the filtered items: an excluded author must not make
    # the scheduler reread the same provider range forever.
    high_water: str = ""

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
            origin = f" (on commit `{i['commit_id']}`)" if i.get("commit_id") else ""
            out.append(f"- **{i.get('author', '?')}** {kind}{state}{where}{origin}:\n\n  " + i.get("body", "").strip().replace("\n", "\n  "))
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


def _check_error_state(exc: GitHubError) -> str:
    """Represent a failed check-rollup request without implying that no checks exist."""
    message = str(exc)
    return "PERMISSION" if " 401 " in message or " 403 " in message else "UNAVAILABLE"


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
    def find_open_pr(self, slug: str, head_branch: str) -> PRInfo | None: ...
    def find_open_pr_by_base(self, slug: str, base_branch: str) -> PRInfo | None: ...
    def list_open_prs(self, slug: str, project_users: list[str] | None = ...) -> list[PRInfo]: ...
    def get_pr(self, slug: str, number: int) -> PRInfo: ...
    def create_pr(self, slug: str, head: str, base: str, title: str, body: str,
                  draft: bool = ..., reviewers: list[str] | None = ...) -> PRInfo: ...
    def feedback_since(self, slug: str, number: int, since_iso: str,
                       exclude_logins: set[str] | None = ..., *,
                       inclusive: bool = ...) -> Feedback: ...
    def incremental_feedback_since(self, slug: str, number: int, since_iso: str,
                                   exclude_logins: set[str] | None = ...) -> Feedback: ...
    def complete_feedback(self, slug: str, number: int) -> dict[str, Any]: ...
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
        # Status is immutable for a completed SHA but pending checks can advance.  Keep a
        # short controller-local cache so many task polls share one authenticated read.
        self._check_cache: dict[tuple[str, str, str], tuple[float, str, list[str]]] = {}
        self._rate_limit_until = 0.0

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
            reset = r.headers.get("x-ratelimit-reset", "")
            suffix = f"; rate_limit_reset={reset}" if reset else ""
            raise GitHubError(f"{method} {path}: {r.status_code} {r.text[:300]}{suffix}")
        return r.json() if r.content else None

    def _rest_pages(self, path: str, *, params: dict[str, str] | None = None) -> list[dict[str, Any]]:
        """Collect every page from a REST list endpoint."""
        items: list[dict[str, Any]] = []
        page = 1
        while True:
            query = {"per_page": 100, "page": page, **(params or {})}
            batch = self._rest("GET", path, params=query) or []
            items.extend(batch)
            if len(batch) < 100:
                return items
            page += 1

    def _checks_for_sha(self, slug: str, sha: str) -> tuple[str, list[str]]:
        """Return a coalesced exact-SHA rollup, failing visibly during rate limits."""
        if not sha:
            return "", []
        now = time.time()
        key = (self.host, slug.lower(), sha)
        cached = self._check_cache.get(key)
        if cached and cached[0] > now:
            return cached[1], list(cached[2])
        if self._rate_limit_until > now:
            wait = max(1, int(self._rate_limit_until - now))
            return "PENDING", [f"GitHub status unavailable; rate limit resets in {wait}s"]
        try:
            if self.gh:
                payload = json.loads(self._gh(
                    "api", f"repos/{slug}/commits/{sha}/check-runs", "-X", "GET",
                    "-f", "per_page=100",
                ) or "{}")
                status_payload = json.loads(self._gh(
                    "api", f"repos/{slug}/commits/{sha}/status", "-X", "GET",
                    "-f", "per_page=100",
                ) or "{}")
            else:
                payload = self._rest("GET", f"/repos/{slug}/commits/{sha}/check-runs",
                                     params={"per_page": 100}) or {}
                status_payload = self._rest("GET", f"/repos/{slug}/commits/{sha}/status",
                                            params={"per_page": 100}) or {}
            runs = payload.get("check_runs", []) if isinstance(payload, dict) else []
            rollup = [{"name": c.get("name"), "conclusion": c.get("conclusion"),
                       "state": c.get("status")} for c in runs]
            statuses = status_payload.get("statuses", []) if isinstance(status_payload, dict) else []
            rollup.extend({"name": s.get("context"), "state": s.get("state")} for s in statuses)
            state, failures = _rollup_state(rollup), _rollup_failed(rollup)
            self._check_cache[key] = (now + 10.0, state, failures)
            return state, failures
        except (GitHubError, ValueError, TypeError, json.JSONDecodeError) as exc:
            message = str(exc)
            reset = re.search(r"rate_limit_reset=(\d+)", message)
            limited = "rate limit" in message.lower() or reset is not None
            if limited:
                self._rate_limit_until = max(now + 10.0, float(reset.group(1)) if reset else now + 60.0)
                detail = "GitHub status unavailable; rate limited"
            else:
                detail = "GitHub status unavailable"
            # Unavailability is pending, never equivalent to absent or green, and a short
            # cache prevents one outage from multiplying requests across tasks.
            unavailable_until = self._rate_limit_until if self._rate_limit_until > now else now + 10.0
            self._check_cache[key] = (min(unavailable_until, now + 60.0), "PENDING", [detail])
            return "PENDING", [detail]

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

    def find_open_pr(self, slug: str, head_branch: str) -> PRInfo | None:
        """Return an open PR for ``head_branch``, regardless of its author.

        Unlike ``list_open_prs``, this exact-head safety query is deliberately not scoped
        to Garden's configured project users. It is used before destructive branch
        operations, where a newly opened PR from any repository collaborator is a claim.
        """
        if self.gh:
            out = self._gh(
                "pr", "list", "-R", self._repo(slug), "--head", head_branch,
                "--state", "open",
                "--json", "number,url,state,title,headRefName,baseRefName,reviewDecision,mergeable,updatedAt,isDraft",
                "--limit", "1",
            )
            prs = json.loads(out or "[]")
            if not prs:
                return None
            p = prs[0]
            return PRInfo(
                number=p["number"], url=p["url"], state=p["state"], title=p.get("title", ""),
                head=p.get("headRefName", ""), base=p.get("baseRefName", ""),
                review_decision=p.get("reviewDecision") or "", mergeable=p.get("mergeable") or "",
                updated_at=p.get("updatedAt", ""), is_draft=bool(p.get("isDraft")),
            )
        owner = slug.split("/")[0]
        prs = self._rest(
            "GET", f"/repos/{slug}/pulls",
            params={"head": f"{owner}:{head_branch}", "state": "open", "per_page": 1},
        )
        return self._pr_from_rest(prs[0]) if prs else None

    def find_open_pr_by_base(self, slug: str, base_branch: str) -> PRInfo | None:
        """Return an open PR targeting ``base_branch``, regardless of its author."""
        if self.gh:
            out = self._gh(
                "pr", "list", "-R", self._repo(slug), "--base", base_branch,
                "--state", "open",
                "--json", "number,url,state,title,headRefName,baseRefName,reviewDecision,mergeable,updatedAt,isDraft",
                "--limit", "1",
            )
            prs = json.loads(out or "[]")
            if not prs:
                return None
            p = prs[0]
            return PRInfo(
                number=p["number"], url=p["url"], state=p["state"], title=p.get("title", ""),
                head=p.get("headRefName", ""), base=p.get("baseRefName", ""),
                review_decision=p.get("reviewDecision") or "", mergeable=p.get("mergeable") or "",
                updated_at=p.get("updatedAt", ""), is_draft=bool(p.get("isDraft")),
            )
        prs = self._rest(
            "GET", f"/repos/{slug}/pulls",
            params={"base": base_branch, "state": "open", "per_page": 1},
        )
        return self._pr_from_rest(prs[0]) if prs else None

    def list_open_prs(self, slug: str, project_users: list[str] | None = None) -> list[PRInfo]:
        """Return relevant open pull requests with review/check state when available.

        Repository observations are scoped to the authenticated user plus configured
        project users. This avoids a repository-wide scan in large shared repositories.
        REST search results omit PR details, so enrich those rows independently. A missing
        permission for one PR's review or check is represented by ``get_pr``; failure to
        fetch the PR itself propagates so callers can retain stale facts.
        """
        authors = {str(user).strip() for user in (project_users or []) if str(user).strip()}
        current_user = self.me()
        if current_user:
            authors.add(current_user)
        if not authors:
            raise GitHubError("cannot scope open PRs: authenticated GitHub user is unknown and github.project_users is empty")
        if self.gh:
            rows: dict[int, PRInfo] = {}
            for author in sorted(authors):
                out = self._gh(
                    "pr", "list", "-R", self._repo(slug), "--state", "open", "--author", author,
                    "--json", "number,url,state,title,author,headRefName,headRefOid,baseRefName,reviewDecision,mergeable,statusCheckRollup,updatedAt,isDraft",
                    "--limit", "1000",
                )
                for p in json.loads(out or "[]"):
                    rows[p["number"]] = PRInfo(
                        number=p["number"], url=p["url"], state=p["state"], title=p.get("title", ""),
                        head=p.get("headRefName", ""), base=p.get("baseRefName", ""),
                        review_decision=p.get("reviewDecision") or "",
                        mergeable=p.get("mergeable") or "", head_sha=p.get("headRefOid") or "",
                        checks=_rollup_state(p.get("statusCheckRollup") or []),
                        failed_checks=_rollup_failed(p.get("statusCheckRollup") or []),
                        updated_at=p.get("updatedAt", ""), is_draft=bool(p.get("isDraft")),
                        author=str((p.get("author") or {}).get("login") or author),
                    )
            return sorted(rows.values(), key=lambda pr: pr.updated_at, reverse=True)
        numbers: set[int] = set()
        for author in sorted(authors):
            page = 1
            while True:
                response = self._rest(
                    "GET", "/search/issues",
                    params={
                        "q": f"repo:{slug} is:pr is:open author:{author}",
                        "per_page": 100,
                        "page": page,
                    },
                ) or {}
                batch = response.get("items", [])
                numbers.update(int(item["number"]) for item in batch)
                if len(batch) < 100:
                    break
                page += 1
        result = [self.get_pr(slug, number) for number in sorted(numbers)]
        return sorted(result, key=lambda pr: pr.updated_at, reverse=True)

    def get_pr(self, slug: str, number: int) -> PRInfo:
        if self.gh:
            out = self._gh(
                "pr", "view", str(number), "-R", self._repo(slug),
                "--json", "number,url,state,title,body,author,headRefName,headRefOid,headRepository,baseRefName,reviewDecision,mergeable,mergeCommit,updatedAt,statusCheckRollup,isDraft,id",
            )
            p = json.loads(out)
            checks, failed_checks = self._checks_for_sha(slug, p.get("headRefOid") or "")
            return PRInfo(
                number=p["number"], url=p["url"], state=p["state"], title=p.get("title", ""),
                head=p.get("headRefName", ""), base=p.get("baseRefName", ""),
                review_decision=p.get("reviewDecision") or "", mergeable=p.get("mergeable") or "",
                checks=checks, failed_checks=failed_checks, updated_at=p.get("updatedAt", ""),
                body=p.get("body") or "", head_sha=p.get("headRefOid") or "",
                merge_commit_sha=(p.get("mergeCommit") or {}).get("oid", ""),
                head_repo=str((p.get("headRepository") or {}).get("nameWithOwner") or ""),
                is_draft=bool(p.get("isDraft")), node_id=str(p.get("id") or ""),
                author=str((p.get("author") or {}).get("login") or ""),
            )
        p = self._rest("GET", f"/repos/{slug}/pulls/{number}")
        info = self._pr_from_rest(p)
        info.body = p.get("body") or ""
        info.head_sha = (p.get("head") or {}).get("sha", "")
        info.merge_commit_sha = p.get("merge_commit_sha") or ""
        info.head_repo = str(((p.get("head") or {}).get("repo") or {}).get("full_name") or "")
        if info.head_sha:
            info.checks, info.failed_checks = self._checks_for_sha(slug, info.head_sha)
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
            mergeable=("MERGEABLE" if p.get("mergeable") else "CONFLICTING")
            if p.get("mergeable") is not None else "",
            updated_at=p.get("updated_at", ""), is_draft=bool(p.get("draft")), node_id=str(p.get("node_id") or ""),
            author=str((p.get("user") or {}).get("login") or ""),
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

    def feedback_since(self, slug: str, number: int, since_iso: str,
                       exclude_logins: set[str] | None = None, *, inclusive: bool = False) -> Feedback:
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
            return created >= since_iso if since_iso and inclusive else (created > since_iso if since_iso else True)

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
            reviews = self._gh_pages(f"repos/{slug}/pulls/{number}/reviews")
            comments = self._gh_pages(f"repos/{slug}/pulls/{number}/comments")
            issue_comments = self._gh_pages(f"repos/{slug}/issues/{number}/comments")
        else:
            reviews = self._rest_pages(f"/repos/{slug}/pulls/{number}/reviews")
            comments = self._rest_pages(f"/repos/{slug}/pulls/{number}/comments")
            issue_comments = self._rest_pages(f"/repos/{slug}/issues/{number}/comments")
        for r in reviews:
            author = r.get("user", {}).get("login", "")
            created = r.get("submitted_at", "") or ""
            body = r.get("body", "") or ""
            state = r.get("state", "")
            if state == "CHANGES_REQUESTED" and newer(created) and author not in exclude:
                if not untrusted(author, created, body or "(changes requested)"):
                    items.append({"id": f"review:{r.get('id', '')}", "kind": "review", "state": state, "author": author, "body": body or "(changes requested)", "created": created,
                                  "commit_id": r.get("commit_id")})
            elif keep(author, created, body) and not untrusted(author, created, body):
                if is_notice(author, body):
                    ignored.append({"author": author, "body": body, "created": created, "reason": "notice"})
                else:
                    items.append({"id": f"review:{r.get('id', '')}", "kind": "review", "state": state, "author": author, "body": body, "created": created,
                                  "commit_id": r.get("commit_id")})
        for c in comments:
            author = c.get("user", {}).get("login", "")
            if keep(author, c.get("created_at", ""), c.get("body", "")) and not untrusted(author, c["created_at"], c["body"]):
                # a comment on a diff line always points at code, notice or not
                items.append({"id": f"line:{c.get('id', '')}", "kind": "line comment", "author": author, "body": c["body"], "path": c.get("path"), "line": c.get("line") or c.get("original_line"), "created": c["created_at"],
                              "commit_id": c.get("commit_id") or c.get("original_commit_id")})
        for c in issue_comments:
            author = c.get("user", {}).get("login", "")
            body = c.get("body", "")
            if keep(author, c.get("created_at", ""), body) and not untrusted(author, c["created_at"], body):
                if is_notice(author, body):
                    ignored.append({"author": author, "body": body, "created": c["created_at"], "reason": "notice"})
                else:
                    items.append({"id": f"comment:{c.get('id', '')}", "kind": "comment", "author": author, "body": body, "created": c["created_at"]})
        items.sort(key=lambda i: i.get("created", ""))
        ignored.sort(key=lambda i: i.get("created", ""))
        timestamps = [str(row.get("submitted_at") or row.get("created_at") or "")
                      for row in [*reviews, *comments, *issue_comments]]
        return Feedback(items=items, ignored=ignored, high_water=max(timestamps, default=""))

    def incremental_feedback_since(self, slug: str, number: int, since_iso: str,
                                   exclude_logins: set[str] | None = None) -> Feedback:
        """Read feedback added at or after a durable high-water timestamp.

        The two comment endpoints provide a ``since`` filter.  Reviews do not, so use
        GraphQL's backwards pagination and stop once the oldest returned review predates
        the cursor.  Keeping the cursor timestamp inclusive and letting stable IDs
        deduplicate it prevents a same-second comment from being lost after a restart.
        """
        if not since_iso:
            return self.feedback_since(slug, number, "", exclude_logins)
        # GitHub's REST ``since`` is exclusive. Request one second earlier so comments
        # sharing the cursor timestamp arrive for identity-based deduplication.
        try:
            since = datetime.fromisoformat(since_iso.replace("Z", "+00:00"))
            request_since = (since.astimezone(UTC).timestamp() - 1)
            request_since_iso = datetime.fromtimestamp(request_since, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            # Older test providers sometimes use synthetic timestamps. They still get
            # the safe complete path rather than a cursor that could skip feedback.
            return self.feedback_since(slug, number, "", exclude_logins)
        reviews = self._reviews_since(slug, number, since_iso)
        query = urlencode({"since": request_since_iso, "per_page": "100"})
        review_path = f"repos/{slug}/pulls/{number}/comments?{query}"
        issue_path = f"repos/{slug}/issues/{number}/comments?{query}"
        if self.gh:
            comments = self._gh_pages(review_path)
            issue_comments = self._gh_pages(issue_path)
        else:
            comments = self._rest_pages(f"/repos/{slug}/pulls/{number}/comments",
                                        params={"since": request_since_iso})
            issue_comments = self._rest_pages(f"/repos/{slug}/issues/{number}/comments",
                                              params={"since": request_since_iso})
        return self._feedback_from_rows(reviews, comments, issue_comments, since_iso,
                                        exclude_logins, inclusive=True)

    def _gh_pages(self, path: str) -> list[dict[str, Any]]:
        """Read all CLI pages as one JSON document, including a cursor boundary page."""
        data = json.loads(self._gh("api", path, "--paginate", "--slurp") or "[]")
        return [row for page in data for row in page] if data and isinstance(data[0], list) else data

    def _reviews_since(self, slug: str, number: int, since_iso: str) -> list[dict[str, Any]]:
        owner, name = slug.split("/", 1)
        query = """query($owner:String!,$name:String!,$number:Int!,$before:String){repository(owner:$owner,name:$name){pullRequest(number:$number){reviews(last:100,before:$before){pageInfo{hasPreviousPage startCursor}nodes{databaseId author{login} submittedAt state body commit{oid}}}}}}"""
        before: str | None = None
        rows: list[dict[str, Any]] = []
        while True:
            if self.gh:
                args = ["api", "graphql", "-f", f"query={query}", "-f", f"owner={owner}",
                        "-f", f"name={name}", "-F", f"number={number}"]
                if before:
                    args += ["-f", f"before={before}"]
                data = json.loads(self._gh(*args) or "{}")
            else:
                data = self._rest("POST", "/graphql", json={"query": query, "variables": {
                    "owner": owner, "name": name, "number": number, "before": before,
                }}) or {}
            if data.get("errors"):
                raise GitHubError(str(data["errors"])[:300])
            reviews = (((data.get("data") or {}).get("repository") or {}).get("pullRequest") or {}).get("reviews") or {}
            batch = reviews.get("nodes") or []
            rows.extend({"id": row.get("databaseId"), "user": row.get("author") or {},
                         "submitted_at": row.get("submittedAt") or "", "state": row.get("state") or "",
                         "body": row.get("body") or "", "commit_id": (row.get("commit") or {}).get("oid")}
                        for row in batch)
            created = [str(row.get("submittedAt") or "") for row in batch]
            page = reviews.get("pageInfo") or {}
            if not page.get("hasPreviousPage") or not created or min(created) < since_iso:
                return rows
            before = page.get("startCursor")
            if not before:
                raise GitHubError("review pagination returned no cursor")

    def _feedback_from_rows(self, reviews: list[dict[str, Any]], comments: list[dict[str, Any]],
                            issue_comments: list[dict[str, Any]], since_iso: str,
                            exclude_logins: set[str] | None, *, inclusive: bool) -> Feedback:
        """Filter provider rows consistently for full and cursor-based fetches."""
        exclude = set(exclude_logins or set()) | self.bot_logins
        items: list[dict[str, Any]] = []
        ignored: list[dict[str, Any]] = []

        def newer(created: str) -> bool:
            return created >= since_iso if inclusive else created > since_iso

        def accepted(author: str, created: str, body: str) -> bool:
            return bool(body.strip()) and GARDEN_MARKER not in body and author not in exclude and newer(created)

        def skipped(author: str, created: str, body: str) -> bool:
            if self.is_trusted(author):
                return False
            ignored.append({"author": author, "body": body, "created": created, "reason": "untrusted"})
            return True

        def notice(author: str, body: str) -> bool:
            return (author.endswith("[bot]") and not FINDING_MARKER_RE.search(body)
                    and any(pattern in body.lower() for pattern in self.bot_notice_patterns))

        for row in reviews:
            author = str((row.get("user") or {}).get("login") or "")
            created, body, state = str(row.get("submitted_at") or ""), str(row.get("body") or ""), str(row.get("state") or "")
            changes_requested = state == "CHANGES_REQUESTED" and newer(created) and author not in exclude
            if changes_requested and not skipped(author, created, body or "(changes requested)"):
                items.append({"id": f"review:{row.get('id', '')}", "kind": "review", "state": state, "author": author,
                              "body": body or "(changes requested)", "created": created, "commit_id": row.get("commit_id")})
            elif accepted(author, created, body) and not skipped(author, created, body):
                entry = {"id": f"review:{row.get('id', '')}", "kind": "review", "state": state, "author": author,
                         "body": body, "created": created, "commit_id": row.get("commit_id")}
                (ignored if notice(author, body) else items).append(
                    {"author": author, "body": body, "created": created, "reason": "notice"} if notice(author, body) else entry)
        for row in comments:
            author, created, body = str((row.get("user") or {}).get("login") or ""), str(row.get("created_at") or ""), str(row.get("body") or "")
            if accepted(author, created, body) and not skipped(author, created, body):
                items.append({"id": f"line:{row.get('id', '')}", "kind": "line comment", "author": author, "body": body,
                              "path": row.get("path"), "line": row.get("line") or row.get("original_line"), "created": created,
                              "commit_id": row.get("commit_id") or row.get("original_commit_id")})
        for row in issue_comments:
            author, created, body = str((row.get("user") or {}).get("login") or ""), str(row.get("created_at") or ""), str(row.get("body") or "")
            if accepted(author, created, body) and not skipped(author, created, body):
                entry = {"id": f"comment:{row.get('id', '')}", "kind": "comment", "author": author, "body": body, "created": created}
                (ignored if notice(author, body) else items).append(
                    {"author": author, "body": body, "created": created, "reason": "notice"} if notice(author, body) else entry)
        timestamps = [str(row.get("submitted_at") or row.get("created_at") or "")
                      for row in [*reviews, *comments, *issue_comments]]
        return Feedback(items=sorted(items, key=lambda item: item.get("created", "")),
                        ignored=sorted(ignored, key=lambda item: item.get("created", "")),
                        high_water=max(timestamps, default=""))

    def _all_rest(self, path: str) -> list[dict[str, Any]]:
        """Read every REST page. This is deliberately separate from incremental polling."""
        rows: list[dict[str, Any]] = []
        page = 1
        while True:
            batch = self._rest("GET", path, params={"per_page": 100, "page": page}) or []
            rows.extend(batch)
            if len(batch) < 100:
                return rows
            page += 1

    def complete_feedback(self, slug: str, number: int) -> dict[str, Any]:
        """Return a complete, metadata-rich PR feedback snapshot for diagnosis.

        Unlike ``feedback_since``, this is not an instruction filter or a cursor-based poll.
        It retains older-head and untrusted text so an investigator can explain what happened;
        each item records whether its author is trusted to direct subsequent worker work.
        """
        errors: list[str] = []

        def fetch(path: str) -> list[dict[str, Any]]:
            try:
                if self.gh:
                    pages = json.loads(self._gh("api", path, "--paginate", "--slurp") or "[]")
                    return [row for page in pages for row in page]
                return self._all_rest("/" + path)
            except (GitHubError, json.JSONDecodeError) as exc:
                errors.append(f"{path}: {exc}")
                return []

        reviews = fetch(f"repos/{slug}/pulls/{number}/reviews")
        line_comments = fetch(f"repos/{slug}/pulls/{number}/comments")
        discussions = fetch(f"repos/{slug}/issues/{number}/comments")
        items: list[dict[str, Any]] = []
        for kind, rows in (("review", reviews), ("line_comment", line_comments),
                           ("discussion_comment", discussions)):
            for row in rows:
                body = str(row.get("body") or "").strip()
                state = str(row.get("state") or "")
                if not body and not (kind == "review" and state == "CHANGES_REQUESTED"):
                    continue
                author = str((row.get("user") or {}).get("login") or "")
                item = {
                    "kind": kind, "id": str(row.get("id") or row.get("node_id") or ""),
                    "author": author, "created_at": row.get("submitted_at") or row.get("created_at") or "",
                    "updated_at": row.get("updated_at") or "", "permalink": row.get("html_url") or "",
                    "body": body or "(changes requested)", "state": state,
                    "commit_id": row.get("commit_id") or row.get("original_commit_id") or "",
                    "path": row.get("path") or "", "line": row.get("line") or row.get("original_line"),
                    "reply_to": str(row.get("in_reply_to_id") or ""), "trusted_instruction": self.is_trusted(author),
                }
                items.append(item)

        # REST exposes reply identity and old commit context, but only GraphQL exposes the
        # current resolved/outdated state of review threads. Enrich matching comments.
        try:
            pr = self.get_pr(slug, number)
            if not pr.node_id:
                raise GitHubError("PR node id unavailable; thread status could not be fetched")
            cursor: str | None = None
            while True:
                query = """query($id:ID!,$after:String){node(id:$id){... on PullRequest{reviewThreads(first:100,after:$after){pageInfo{hasNextPage endCursor}nodes{id isResolved isOutdated comments(first:100){pageInfo{hasNextPage endCursor}nodes{id databaseId}}}}}}}"""
                variables = {"id": pr.node_id, "after": cursor}
                if self.gh:
                    args = ["api", "graphql", "-f", f"query={query}", "-f", f"id={pr.node_id}"]
                    if cursor:
                        args += ["-f", f"after={cursor}"]
                    raw = self._gh(*args)
                    data = json.loads(raw or "{}")
                else:
                    data = self._rest("POST", "/graphql", json={"query": query, "variables": variables}) or {}
                if data.get("errors"):
                    raise GitHubError(str(data["errors"])[:300])
                threads = (((data.get("data") or {}).get("node") or {}).get("reviewThreads") or {})
                for thread in threads.get("nodes") or []:
                    comment_page = thread.get("comments") or {}

                    def apply_thread(comments: list[dict[str, Any]], *,
                                     thread_id: str = str(thread.get("id") or ""),
                                     resolved: bool = bool(thread.get("isResolved")),
                                     outdated: bool = bool(thread.get("isOutdated"))) -> None:
                        """Attach one thread's status to its REST comments and replies."""
                        for comment in comments:
                            keys = {str(comment.get("databaseId") or ""), str(comment.get("id") or "")}
                            for item in items:
                                if item["kind"] == "line_comment" and item["id"] in keys:
                                    item.update({"thread_id": thread_id, "resolved": resolved,
                                                 "outdated": outdated})

                    apply_thread(comment_page.get("nodes") or [])
                    comment_cursor = (comment_page.get("pageInfo") or {}).get("endCursor")
                    while (comment_page.get("pageInfo") or {}).get("hasNextPage"):
                        comment_query = """query($id:ID!,$after:String!){node(id:$id){... on PullRequestReviewThread{comments(first:100,after:$after){pageInfo{hasNextPage endCursor}nodes{id databaseId}}}}}"""
                        comment_variables = {"id": thread.get("id"), "after": comment_cursor}
                        if self.gh:
                            raw = self._gh("api", "graphql", "-f", f"query={comment_query}",
                                           "-f", f"id={thread.get('id')}", "-f", f"after={comment_cursor}")
                            comment_data = json.loads(raw or "{}")
                        else:
                            comment_data = self._rest("POST", "/graphql",
                                                      json={"query": comment_query,
                                                            "variables": comment_variables}) or {}
                        if comment_data.get("errors"):
                            raise GitHubError(str(comment_data["errors"])[:300])
                        comment_page = (((comment_data.get("data") or {}).get("node") or {}).get("comments") or {})
                        apply_thread(comment_page.get("nodes") or [])
                        next_cursor = (comment_page.get("pageInfo") or {}).get("endCursor")
                        if (comment_page.get("pageInfo") or {}).get("hasNextPage") and not next_cursor:
                            raise GitHubError("review comment pagination returned no cursor")
                        comment_cursor = next_cursor
                page = threads.get("pageInfo") or {}
                if not page.get("hasNextPage"):
                    break
                cursor = str(page.get("endCursor") or "")
                if not cursor:
                    raise GitHubError("review thread pagination returned no cursor")
        except (GitHubError, json.JSONDecodeError) as exc:
            errors.append(f"review threads: {exc}")

        unique: dict[tuple[str, str], dict[str, Any]] = {}
        for item in items:
            key = (item["kind"], item["id"] or f"{item['author']}:{item['created_at']}:{item['body']}")
            unique[key] = item
        ordered = sorted(unique.values(), key=lambda row: (str(row["created_at"]), row["kind"], row["id"]))
        fetched_at = datetime.now(UTC).replace(microsecond=0).isoformat()
        return {"repository": slug, "pr": number, "fetched_at": fetched_at, "complete": not errors,
                "errors": errors, "items": ordered}

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
    if states <= {"SUCCESS", "NEUTRAL", "SKIPPED"}:
        return "SUCCESS"
    return "UNKNOWN"
