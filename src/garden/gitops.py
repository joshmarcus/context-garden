"""Git plumbing for product repos and per-task worktrees. Deterministic, no LLM."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from .github import repo_slug_from_remote


class GitError(Exception):
    pass


class LeaseRejected(GitError):
    """A `--force-with-lease` push was rejected: `origin/<branch>` no longer sits at the sha the
    caller expected (`expected`), and now sits at `actual` (CG-220) — another writer (the merge
    queue's rebase, an earlier revise round) pushed to the same branch meanwhile."""

    def __init__(self, branch: str, expected: str, actual: str):
        self.branch = branch
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"lease rejected on {branch}: expected origin at {expected[:12] or '(unknown)'}, "
            f"now at {actual[:12] or '(unknown)'}"
        )


_READ_CACHE: ContextVar[dict[tuple[object, ...], Any] | None] = ContextVar(
    "garden_git_read_cache", default=None,
)


@contextlib.contextmanager
def tick_read_cache():
    """Reuse stable Git metadata and successful fetches within one scheduler tick.

    A tick is one coherent observation of the repositories it operates on. Repeating the
    same remote, ref-selection and fetch probes in later phases adds process-launch cost
    without making that observation more current. The context boundary deliberately keeps
    the cache out of CLI operations and out of the next tick.
    """
    token = _READ_CACHE.set({})
    try:
        yield
    finally:
        _READ_CACHE.reset(token)


def _read_cache_key(kind: str, repo: Path, *parts: object) -> tuple[object, ...]:
    return kind, str(repo.resolve()), *parts


def _invalidate_cached_refs(repo: Path) -> None:
    cache = _READ_CACHE.get()
    if cache is None:
        return
    path = str(repo.resolve())
    for key in [key for key in cache if len(key) > 1 and key[1] == path and key[0] == "base_ref"]:
        cache.pop(key, None)


@contextlib.contextmanager
def _empty_hooks_dir():
    """A freshly created, empty directory for `core.hooksPath` (see `_git_env`), torn down as
    soon as the `git` call it was made for returns. A single fixed path (e.g. under
    `tempfile.gettempdir()`) would be predictable and, since workers run without a sandbox by
    default, writable by any worker's own Bash tool call — planting a hook there would let a
    worker plant code the scheduler later executes with the operator's credentials against any
    clone, not just its own. `mkdtemp` gives each invocation an unpredictable name that does not
    exist until this call creates it, so there is nothing for a worker to have pre-planted into,
    and it is gone again before the next call picks a new name (CG-239)."""
    d = tempfile.mkdtemp(prefix="garden-empty-hooks-")
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


@contextlib.contextmanager
def _git_env():
    """The environment every scheduler-side `git` invocation in this module runs under:
    `core.hooksPath` forced to a fresh, empty directory, `core.fsmonitor` forced off and
    automatic repository maintenance disabled, via
    `GIT_CONFIG_COUNT` (git reads `GIT_CONFIG_KEY_n`/`GIT_CONFIG_VALUE_n` pairs as the
    highest-priority config, above the repo's own `.git/config`). A clone's `.git/config` or
    `.git/hooks` is reachable from inside a worker's own worktree (a worktree shares its
    clone's config unless `extensions.worktreeConfig` is set) — without this, a hook path or
    an fsmonitor command planted there would run with the operator's own credentials the next
    time the scheduler itself runs `git` in that clone (CG-239). Every subprocess call site in
    this module goes through this context manager, so the hooks directory it makes never
    outlives the single `git` invocation it was made for."""
    with _empty_hooks_dir() as hooks_dir:
        yield {
            **os.environ,
            "GIT_CONFIG_COUNT": "3",
            "GIT_CONFIG_KEY_0": "core.hooksPath", "GIT_CONFIG_VALUE_0": hooks_dir,
            "GIT_CONFIG_KEY_1": "core.fsmonitor", "GIT_CONFIG_VALUE_1": "false",
            "GIT_CONFIG_KEY_2": "maintenance.auto", "GIT_CONFIG_VALUE_2": "0",
        }


_BLOCK_MARKER = ".garden-git-blocked"


def worktree_admin_dir(worktree: Path) -> Path | None:
    """The `.git/worktrees/<id>/` administrative directory a linked worktree's `.git` file
    names, or None when `worktree` is not a linked worktree (an ordinary clone, whose `.git`
    is a directory, or no `.git` at all)."""
    dot_git = Path(worktree) / ".git"
    if not dot_git.is_file():
        return None
    try:
        line = dot_git.read_text().strip()
    except OSError:
        return None
    if not line.startswith("gitdir:"):
        return None
    admin = Path(line.split(":", 1)[1].strip())
    if not admin.is_absolute():
        admin = (Path(worktree) / admin).resolve()
    return admin if admin.parent.name == "worktrees" else None


def _common_dir(path: Path) -> Path:
    """The repo `path` actually shares its config and hooks with: `path` itself for an
    ordinary clone, or the clone a linked worktree's admin directory belongs to (`.git/worktrees/<id>`,
    so its `.git` dir is one level up and the clone root another level up from there). Falls
    back to `path` when it is not a git checkout at all, so blocking (or checking) a non-repo
    path is simply a no-op."""
    admin = worktree_admin_dir(path)
    return admin.parent.parent.parent if admin is not None else Path(path)


def block_repo(path: Path, reason: str) -> None:
    """Refuse every future scheduler-side `git` command in `path`'s repo — and in every
    worktree linked to it, since a linked worktree shares the same config and hooks — until a
    person recreates it. A plain marker file on disk, so the block survives a scheduler
    restart; used when the fence finds a clone's `.git/config`, its hooks directory, a
    worktree's `.git` file or its `.git/worktrees/<id>/` admin directory changed since
    dispatch (CG-239) — exactly the surface that would let a worktree write make a *later*
    `git` call run arbitrary code with the operator's own credentials."""
    clone = _common_dir(Path(path))
    try:
        (clone / _BLOCK_MARKER).write_text(reason)
    except OSError:
        pass


def blocked_reason(path: Path) -> str:
    """The reason `path`'s repo was blocked (see `block_repo`), or "" if it was not."""
    marker = _common_dir(Path(path)) / _BLOCK_MARKER
    try:
        return marker.read_text().strip() if marker.exists() else ""
    except OSError:
        return ""


def _ensure_not_blocked(cwd: Path | None) -> None:
    if cwd is None:
        return
    reason = blocked_reason(cwd)
    if reason:
        raise GitError(f"refusing to run git in {cwd}: blocked ({reason})")


def git(*args: str, cwd: Path | None = None, check: bool = True) -> str:
    _ensure_not_blocked(cwd)
    with _git_env() as env:
        proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                              errors="replace", env=env)
    if check and proc.returncode != 0:
        raise GitError(f"git {' '.join(args)} (in {cwd}): {proc.stderr.strip() or proc.stdout.strip()}")
    return proc.stdout


def is_repo(path: Path) -> bool:
    return (path / ".git").exists()


def remote_url(repo: Path, remote: str = "origin") -> str:
    cache = _READ_CACHE.get()
    key = _read_cache_key("remote_url", repo, remote)
    if cache is not None and key in cache:
        return str(cache[key])
    try:
        result = git("remote", "get-url", remote, cwd=repo).strip()
    except GitError:
        result = ""
    if cache is not None:
        cache[key] = result
    return result


def slug(repo: Path) -> str | None:
    url = remote_url(repo)
    return repo_slug_from_remote(url) if url else None


def ensure_repo(repo: Path | str, clone_dir: Path, git_name: str = "", git_email: str = "") -> Path:
    """Return a local checkout for `repo` (a path, or a URL cloned under clone_dir). A fresh
    clone gets `git_name`/`git_email` as its repo-local identity (see CG-147), so a commit
    made inside it — by the scheduler or a worker — never fails with "Author identity
    unknown". An already-existing clone, or a path repo, is left untouched."""
    if isinstance(repo, Path):
        if not is_repo(repo):
            raise GitError(f"{repo} is not a git repository")
        return repo
    name = repo.rstrip("/").split("/")[-1].removesuffix(".git")
    dest = clone_dir / name
    if not dest.exists():
        clone_dir.mkdir(parents=True, exist_ok=True)
        git("clone", repo, str(dest))
        set_identity(dest, git_name, git_email)
    return dest


def identity(repo: Path) -> tuple[str, str]:
    """The effective `user.name` / `user.email` git would commit as in `repo` (local config,
    falling back to global), or "" for either that resolves to nothing."""
    name = git("config", "user.name", cwd=repo, check=False).strip()
    email = git("config", "user.email", cwd=repo, check=False).strip()
    return name, email


def set_identity(repo: Path, name: str, email: str) -> None:
    """Set `repo`'s local git identity. Either may be blank, in which case that half is left
    alone (e.g. no resolvable name or email at all — see Scheduler.git_identity)."""
    if name:
        git("config", "user.name", name, cwd=repo)
    if email:
        git("config", "user.email", email, cwd=repo)


def fetch(repo: Path, remote: str = "origin") -> bool:
    cache = _READ_CACHE.get()
    key = _read_cache_key("fetch", repo, remote)
    if cache is not None and cache.get(key) is True:
        return True
    _invalidate_cached_refs(repo)
    try:
        git("fetch", "--prune", remote, cwd=repo)
        if cache is not None:
            cache[key] = True
        return True
    except GitError:
        return False


def base_ref(repo: Path, base: str) -> str:
    """Prefer origin/<base> when a remote exists, else the local branch."""
    cache = _READ_CACHE.get()
    key = _read_cache_key("base_ref", repo, base)
    if cache is not None and key in cache:
        return str(cache[key])
    try:
        git("rev-parse", "--verify", f"refs/remotes/origin/{base}", cwd=repo)
        result = f"origin/{base}"
    except GitError:
        git("rev-parse", "--verify", base, cwd=repo)
        result = base
    if cache is not None:
        cache[key] = result
    return result


def branch_exists(repo: Path, branch: str) -> bool:
    try:
        git("rev-parse", "--verify", f"refs/heads/{branch}", cwd=repo)
        return True
    except GitError:
        return False


def _ensure_base(repo: Path, worktree: Path, base: str) -> bool:
    """Reconcile `worktree`'s branch with `base`; return True if it was fast-forwarded.

    A stacked task's `base` is its parent's branch, not the product base. But a branch that
    already exists (reused worktree, or a branch left over on disk or origin from an earlier
    dispatch when the parent had no PR yet) can be sitting on a stale base — this is how a
    task whose brief says "based on <parent-branch>" ended up checked out at `main`. When the
    branch has no commits of its own (its HEAD is already contained in `base`) it is safe to
    fast-forward it onto `base`, which is the git surgery a worker otherwise had to do by hand.
    When the branch carries its own commits on a different base we leave it: a reset would
    throw that work away, and rebasing is the caller's job (see rebase_onto_capture / _restack)."""
    if not base:
        return False
    try:
        ref = base_ref(repo, base)
    except GitError:
        return False
    if is_ancestor(worktree, ref, "HEAD"):
        return False  # branch already contains `base`; nothing to do
    ahead = git("rev-list", "--count", f"{ref}..HEAD", cwd=worktree).strip()
    if ahead != "0":
        return False  # branch has its own commits on a different base; leave it for a rebase
    git("merge", "--ff-only", "-q", ref, cwd=worktree)
    return True


def prepare_worktree(repo: Path, path: Path, branch: str, base: str) -> Path:
    """Create (or reuse) a worktree on `branch`, creating the branch from `base` if needed.

    However the worktree is established, the branch is reconciled with `base` (see
    _ensure_base): a branch with no commits of its own is fast-forwarded onto `base`, so a
    stacked task actually sits on its parent's branch instead of a stale base."""
    fetch(repo)
    if path.exists() and (path / ".git").exists():
        # reuse; make sure we're on the right branch
        cur = git("rev-parse", "--abbrev-ref", "HEAD", cwd=path).strip()
        if cur != branch:
            git("checkout", branch, cwd=path)
        _ensure_base(repo, path, base)
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    git("worktree", "prune", cwd=repo)
    if branch_exists(repo, branch):
        git("worktree", "add", str(path), branch, cwd=repo)
        _ensure_base(repo, path, base)
    else:
        remote_branch = f"origin/{branch}"
        try:
            git("rev-parse", "--verify", f"refs/remotes/{remote_branch}", cwd=repo)
            git("worktree", "add", "--track", "-b", branch, str(path), remote_branch, cwd=repo)
            _ensure_base(repo, path, base)
            return path
        except GitError:
            pass
        git("worktree", "add", "-b", branch, str(path), base_ref(repo, base), cwd=repo)
    return path


def add_detached_worktree(repo: Path, path: Path, commit: str) -> Path:
    """Check `commit` out into a throwaway detached worktree (no branch). Used to probe a
    check at the branch's base without disturbing the branch's own worktree."""
    path.parent.mkdir(parents=True, exist_ok=True)
    git("worktree", "prune", cwd=repo)
    git("worktree", "add", "--detach", str(path), commit, cwd=repo)
    return path


def merge_base(worktree: Path, ref: str) -> str:
    """The commit where `worktree`'s HEAD and `ref` diverge — the branch's base commit."""
    return git("merge-base", ref, "HEAD", cwd=worktree).strip()


def rev_parse(worktree: Path, ref: str) -> str:
    return git("rev-parse", ref, cwd=worktree).strip()


def remove_worktree(repo: Path, path: Path) -> None:
    if path.exists():
        try:
            git("worktree", "remove", "--force", str(path), cwd=repo)
        except GitError:
            pass
    try:
        git("worktree", "prune", cwd=repo)
    except GitError:
        pass


def commits_ahead(worktree: Path, base: str) -> int:
    ref = base_ref(worktree, base)
    out = git("rev-list", "--count", f"{ref}..HEAD", cwd=worktree).strip()
    return int(out or 0)


def has_uncommitted_changes(worktree: Path) -> bool:
    return bool(git("status", "--porcelain", cwd=worktree).strip())


def commit_all(worktree: Path, message: str) -> bool:
    if not has_uncommitted_changes(worktree):
        return False
    git("add", "-A", cwd=worktree)
    git("commit", "-q", "-m", message, cwd=worktree)
    return True


def stash_all(worktree: Path, message: str) -> str:
    """Stash every uncommitted change in `worktree` (including untracked files) under a named
    stash and return that stash commit's stable object id, or "" if there was nothing to stash.

    Used when a fresh dispatch lands on a worktree a killed worker left dirty: reconciling the
    branch onto its base (`_ensure_base`) would fail with "Your local changes would be
    overwritten". Stashing sets the abandoned edits aside — recorded by sha so a person can
    recover them with `git stash apply <sha>` — and lets the new run start from a clean tree.

    `refs/stash` is shared by every linked worktree.  Do not resolve it after `stash push`:
    another worker may create a stash before that lookup and make us record its changes.  Git's
    stash reflog records the message and resulting object id atomically with the push, so find
    our uniquely named entry there and retain the object id for all later recovery operations.
    """
    if not has_uncommitted_changes(worktree):
        return ""
    git("stash", "push", "--include-untracked", "-m", message, cwd=worktree)
    suffix = f": {message}"
    reflog = git("reflog", "show", "--format=%H%x00%gs", "refs/stash", cwd=worktree)
    for entry in reflog.splitlines():
        sha, separator, subject = entry.partition("\x00")
        if separator and subject.endswith(suffix):
            if git("cat-file", "-t", sha, cwd=worktree, check=False).strip() == "commit":
                return sha
    raise GitError(f"could not identify stash created for {message!r}")


def is_ancestor(repo: Path, ref_a: str, ref_b: str) -> bool:
    """Return True if ref_a is an ancestor of ref_b (or equal)."""
    _ensure_not_blocked(repo)
    with _git_env() as env:
        proc = subprocess.run(["git", "merge-base", "--is-ancestor", ref_a, ref_b], cwd=repo, capture_output=True, env=env)
    return proc.returncode == 0


def push(worktree: Path, branch: str, force: bool = False, base: str = "", lease: str = "") -> str:
    """Push branch to origin. Returns a note if force-with-lease was used due to rebase detection.

    `lease`, when given, is the sha `origin/<branch>` is expected to still be at — the head this
    run started from (CG-220): the push always goes as `--force-with-lease=<branch>:<lease>`, so
    a writer that moved the branch meanwhile (another revise round, the merge queue's own rebase)
    rejects the push with `LeaseRejected` (naming both heads) instead of silently overwriting or
    failing with a bare "non-fast-forward". `lease` takes precedence over `force`/`base`.

    Without a lease, `base` compares origin/<branch> against HEAD:
    - fast-forward (origin/<branch> is ancestor of HEAD): plain push, no note.
    - rebased (origin/<branch> not ancestor, but origin/<base> is): --force-with-lease with
      an expectation ref so we only overwrite the sha we saw; logs "rebased branch force-pushed".
    - other divergence: let the plain push fail with git's own message.
    """
    if not remote_url(worktree):
        raise GitError("no origin remote to push to")
    args = ["push", "-u"]
    note = ""
    if lease:
        args.append(f"--force-with-lease={branch}:{lease}")
    elif force:
        args.append("--force-with-lease")
    elif base:
        try:
            origin_sha = git("rev-parse", f"origin/{branch}", cwd=worktree).strip()
            if not is_ancestor(worktree, f"origin/{branch}", "HEAD"):
                if is_ancestor(worktree, f"origin/{base}", "HEAD"):
                    args.append(f"--force-with-lease={branch}:{origin_sha}")
                    note = "rebased branch force-pushed"
        except GitError:
            pass  # origin/<branch> doesn't exist yet; plain push is fine
    try:
        git(*args, "origin", f"HEAD:refs/heads/{branch}", cwd=worktree)
    except GitError:
        if lease:
            git("fetch", "origin", branch, cwd=worktree, check=False)
            try:
                actual = git("rev-parse", f"origin/{branch}", cwd=worktree).strip()
            except GitError:
                actual = ""
            raise LeaseRejected(branch, lease, actual) from None
        raise
    return note


def remote_head(worktree: Path, branch: str) -> str:
    """`origin/<branch>`'s current sha, or "" when it does not exist yet (a branch never
    pushed). Assumes a fetch already happened recently enough for the caller's purpose."""
    try:
        return git("rev-parse", f"origin/{branch}", cwd=worktree).strip()
    except GitError:
        return ""


def local_head(repo: Path, branch: str) -> str:
    """Return a local branch's head, or ``""`` when it is absent."""
    try:
        return git("rev-parse", "--verify", f"refs/heads/{branch}", cwd=repo).strip()
    except GitError:
        return ""


def worktree_branches(repo: Path) -> set[str]:
    """Branches currently checked out by any linked or main worktree."""
    out = git("worktree", "list", "--porcelain", cwd=repo)
    prefix = "branch refs/heads/"
    return {line[len(prefix):] for line in out.splitlines() if line.startswith(prefix)}


def delete_local_branch(repo: Path, branch: str, expected_head: str) -> bool:
    """Delete a branch only while it still names ``expected_head``.

    Git has no compare-and-delete porcelain for local refs, so ``update-ref -d`` supplies
    the old object id as its atomic precondition. Missing refs are an idempotent success.
    """
    actual = local_head(repo, branch)
    if not actual:
        return False
    if not expected_head or actual != expected_head:
        raise LeaseRejected(branch, expected_head, actual)
    git("update-ref", "-d", f"refs/heads/{branch}", expected_head, cwd=repo)
    return True


def delete_remote_branch(repo: Path, remote: str, branch: str, expected_head: str) -> bool:
    """Delete a remote branch with an expected-head lease.

    This uses the configured Git remote directly, so it works for non-``origin`` remotes
    and enterprise providers. A stale tracking ref is never authority: ``ls-remote`` is
    read immediately before the guarded push.
    """
    if not remote_url(repo, remote):
        raise GitError(f"remote {remote!r} is not configured")
    ref = f"refs/heads/{branch}"
    line = git("ls-remote", "--heads", remote, ref, cwd=repo).strip()
    actual = line.split()[0] if line else ""
    if not actual:
        return False
    if not expected_head or actual != expected_head:
        raise LeaseRejected(branch, expected_head, actual)
    git("push", f"--force-with-lease={ref}:{expected_head}", remote, f":{ref}", cwd=repo)
    return True


def sync_to_origin_head(worktree: Path, branch: str, backup_ref: str) -> list[str]:
    """Before a revise, rebase or resume run starts (CG-220), bring `worktree` to
    `origin/<branch>`'s head: fetch, then hard-reset onto it, so the run starts from the same
    head any other writer already pushed (a revise dispatched moments earlier, the merge queue's
    own rebase) instead of a stale local copy. Any commits that exist only in the worktree — a
    killed prior run's partial progress, a hand edit — are saved to `backup_ref` first so the
    reset never discards them silently; uncommitted edits are committed before the backup for the
    same reason. Returns the one-line subjects of the commits that were backed up (empty when the
    worktree already sits on origin's head, has no branch of its own on origin yet, or does not
    exist)."""
    if not worktree.exists() or not (worktree / ".git").exists():
        return []
    fetch(worktree)
    origin_head = remote_head(worktree, branch)
    if not origin_head:
        return []  # nothing pushed to origin yet: nothing to sync to
    cur = git("rev-parse", "--abbrev-ref", "HEAD", cwd=worktree).strip()
    if cur != branch:
        git("checkout", branch, cwd=worktree)
    if has_uncommitted_changes(worktree):
        commit_all(worktree, "leftover changes before syncing to origin's head")
    local_head = git("rev-parse", "HEAD", cwd=worktree).strip()
    if local_head == origin_head:
        return []
    subjects: list[str] = []
    if not is_ancestor(worktree, local_head, origin_head):
        subjects = commits_between(worktree, origin_head, local_head)
        if subjects:
            git("branch", backup_ref, local_head, cwd=worktree)
    git("reset", "--hard", origin_head, cwd=worktree)
    return subjects


def sync_remote_branch(worktree: Path, branch: str, *, artifact_dir: Path | None = None) -> tuple[bool, list[str]]:
    """Before a rebase round, fold in commits that exist only on `origin/<branch>`.

    A rebase round rewrites the branch in the worktree and force-pushes it. If the remote
    branch has moved on (someone merged into it) those commits are not in the worktree, so
    the force-push would discard them. Rebasing the worktree's local commits onto
    `origin/<branch>` first keeps them. Returns (ok, conflicted files); on conflict the
    rebase is aborted so the worktree is left clean and the round resolves it like any other.
    When ``artifact_dir`` is given, the unmerged index stages survive the abort there.
    """
    fetch(worktree)
    if not remote_url(worktree):
        return True, []
    try:
        git("rev-parse", "--verify", f"origin/{branch}", cwd=worktree)
    except GitError:
        return True, []  # no remote branch yet: nothing to fold in
    if is_ancestor(worktree, f"origin/{branch}", "HEAD"):
        return True, []  # the remote holds nothing the worktree lacks
    try:
        git("rebase", f"origin/{branch}", cwd=worktree)
        return True, []
    except GitError:
        files = [ln.strip() for ln in git("diff", "--name-only", "--diff-filter=U", cwd=worktree, check=False).splitlines() if ln.strip()]
        if artifact_dir is not None:
            capture_conflict_artifacts(worktree, files, artifact_dir)
        git("rebase", "--abort", cwd=worktree, check=False)
        return False, files


def _index_blob(worktree: Path, stage: int, path: str) -> bytes | None:
    """Return one unmerged index stage without decoding its possibly-binary contents."""
    _ensure_not_blocked(worktree)
    with _git_env() as env:
        proc = subprocess.run(["git", "show", f":{stage}:{path}"], cwd=worktree,
                              capture_output=True, env=env)
    return proc.stdout if proc.returncode == 0 else None


def capture_conflict_artifacts(worktree: Path, files: list[str], artifact_dir: Path) -> dict[str, dict[str, object]]:
    """Persist every available unmerged index blob before a rebase aborts.

    The worktree rendering is useful for a small prompt excerpt, but it is lossy for binary
    files and is gone after ``rebase --abort``.  These stage files are the recovery source of
    truth: each is written byte-for-byte with its digest in a manifest beside it.
    """
    artifact_dir.mkdir(parents=True, exist_ok=True)
    captured: dict[str, dict[str, object]] = {}
    for path in files:
        stages: list[dict[str, object]] = []
        path_key = hashlib.sha256(path.encode("utf-8")).hexdigest()[:16]
        for stage in (1, 2, 3):
            blob = _index_blob(worktree, stage, path)
            if blob is None:
                continue
            digest = hashlib.sha256(blob).hexdigest()
            artifact = artifact_dir / f"{path_key}.stage-{stage}.{digest[:16]}.blob"
            artifact.write_bytes(blob)
            stages.append({"stage": stage, "path": str(artifact), "bytes": len(blob), "sha256": digest})
        captured[path] = {"stages": stages}
    manifest = artifact_dir / "manifest.json"
    manifest.write_text(json.dumps(captured, indent=2, sort_keys=True) + "\n")
    for item in captured.values():
        item["manifest"] = str(manifest)
    return captured


def rebase_onto_capture(worktree: Path, onto: str, *, artifact_dir: Path | None = None) -> tuple[bool, list[str], dict[str, str]]:
    """Rebase the worktree branch onto `onto` (e.g. origin/main). Returns (ok, conflicted files,
    {path: file contents}); on a textual conflict each conflicted file's contents (with conflict
    markers) are captured before aborting, so a rebase brief can carry the conflicting hunks. If
    ``artifact_dir`` is given, every available index stage is also preserved byte-for-byte there.
    The rebase is aborted on conflict so the worktree is left clean for the agent to redo and resolve."""
    fetch(worktree)
    try:
        git("rebase", onto, cwd=worktree)
        return True, [], {}
    except GitError:
        files = [ln.strip() for ln in git("diff", "--name-only", "--diff-filter=U", cwd=worktree, check=False).splitlines() if ln.strip()]
        if artifact_dir is not None:
            capture_conflict_artifacts(worktree, files, artifact_dir)
        hunks: dict[str, str] = {}
        for f in files:
            try:
                hunks[f] = (worktree / f).read_text(errors="replace")
            except OSError:
                hunks[f] = ""
        git("rebase", "--abort", cwd=worktree, check=False)
        return False, files, hunks


def sync_and_rebase(worktree: Path, branch: str, base: str, *, artifact_dir: Path | None = None) -> tuple[bool, list[str], dict[str, str]]:
    """Bring the worktree branch onto `base`, folding in commits that live only on
    `origin/<branch>` first (`sync_remote_branch`) so a later force-push never discards them,
    then rebasing onto the base (`rebase_onto_capture`). This is the one sync-then-rebase
    sequence every mechanical-rebase path shares -- the rebase mixin, the restack, the base
    probe and the merge queue all reach it through `Scheduler._rebase_and_record`. Returns
    (ok, conflicted files, {path: contents with markers}); on a conflict at either step the
    rebase is aborted so the worktree is left clean, and the hunks (empty for a sync conflict)
    carry the textual conflict for a rebase brief."""
    ok, files = sync_remote_branch(worktree, branch, artifact_dir=artifact_dir)
    if not ok:
        return False, files, {}
    return rebase_onto_capture(worktree, base_ref(worktree, base), artifact_dir=artifact_dir)


def diff_hash(worktree: Path, base: str) -> str:
    import hashlib

    return hashlib.sha1(diff(worktree, base).encode("utf-8", "replace")).hexdigest()[:16]


def patch_id(worktree: Path, base: str) -> str:
    """The stable `git patch-id` of the branch's diff against its merge-base with `base`: a hash
    of the diff's added/removed content only, blind to the hunk-header line numbers and context
    lines that shift whenever unrelated commits land on `base` near the branch's own hunks
    (CG-210) — unlike a hash of the raw diff text, this stays the same across such a rebase, so
    comparing it before and after tells whether the rebase changed anything the PR itself owns.
    Empty when the diff is empty or the id cannot be computed."""
    diff_text = diff(worktree, base)
    if not diff_text.strip():
        return ""
    _ensure_not_blocked(worktree)
    with _git_env() as env:
        proc = subprocess.run(["git", "patch-id", "--stable"], input=diff_text, cwd=worktree,
                              capture_output=True, text=True, env=env)
    if proc.returncode != 0 or not proc.stdout.strip():
        return ""
    return proc.stdout.split()[0]


def patch_id_between(repo: Path, base: str, head: str) -> str:
    """Return a stable id for the aggregate change from ``base`` to ``head``."""
    diff_text = git("diff", "--binary", base, head, cwd=repo)
    if not diff_text.strip():
        return ""
    _ensure_not_blocked(repo)
    with _git_env() as env:
        proc = subprocess.run(["git", "patch-id", "--stable"], input=diff_text, cwd=repo,
                              capture_output=True, text=True, env=env)
    if proc.returncode != 0 or not proc.stdout.strip():
        return ""
    return proc.stdout.split()[0]


def log_summary(worktree: Path, base: str, n: int = 20) -> str:
    try:
        ref = base_ref(worktree, base)
        return git("log", "--oneline", f"-{n}", f"{ref}..HEAD", cwd=worktree)
    except GitError:
        return ""


def diff_stat(worktree: Path, base: str) -> str:
    try:
        ref = base_ref(worktree, base)
        return git("diff", "--stat", f"{ref}...HEAD", cwd=worktree)
    except GitError:
        return ""


def diff(worktree: Path, base: str) -> str:
    try:
        ref = base_ref(worktree, base)
        return git("diff", f"{ref}...HEAD", cwd=worktree)
    except GitError:
        return ""


def diff_names(worktree: Path, base: str) -> list[str]:
    """Paths the PR changes against its base (merge-base diff, `base...HEAD`), for gating on
    what a diff touches. Empty when the base ref is unknown."""
    try:
        ref = base_ref(worktree, base)
        out = git("diff", "--name-only", f"{ref}...HEAD", cwd=worktree)
    except GitError:
        return []
    return [ln.strip() for ln in out.splitlines() if ln.strip()]


def head_sha(repo: Path) -> str:
    """The current HEAD commit, or '' if the path is not a usable git repo."""
    try:
        return git("rev-parse", "HEAD", cwd=repo).strip()
    except GitError:
        return ""


def status_lines(repo: Path) -> list[str]:
    """`git status --porcelain` as a list of non-empty lines (worktree + every untracked path)."""
    try:
        out = git("status", "--porcelain", "--untracked-files=all", cwd=repo)
    except GitError:
        return []
    return [ln for ln in out.splitlines() if ln.strip()]


def commits_between(repo: Path, old: str, new: str) -> list[str]:
    """One-line subjects of the commits in old..new (empty if either ref is unknown)."""
    if not old or not new:
        return []
    out = git("log", "--oneline", "--no-decorate", f"{old}..{new}", cwd=repo, check=False)
    return [ln.strip() for ln in out.splitlines() if ln.strip()]


def changed_files(repo: Path, old: str, new: str) -> list[str]:
    """Paths changed between two commits (old..new), for undoing a worker's commits."""
    if not old or not new:
        return []
    out = git("diff", "--name-only", f"{old}..{new}", cwd=repo, check=False)
    return [ln.strip() for ln in out.splitlines() if ln.strip()]


def path_at(repo: Path, ref: str, rel: str) -> bool:
    """True if `rel` exists as a tracked path in `ref`'s tree. Goes through `git()` (not a raw
    `subprocess.run`) so a blocked clone refuses this call too, and hooksPath/fsmonitor are
    forced off the same as every other git call in this module."""
    try:
        git("cat-file", "-e", f"{ref}:{rel}", cwd=repo)
        return True
    except GitError:
        return False


def reset_soft(repo: Path, ref: str) -> None:
    """Move the current branch to `ref` without touching the index or working tree, so a
    worker's commits are dropped from history while unrelated in-flight edits survive."""
    git("reset", "--soft", ref, cwd=repo)


def restore_path(repo: Path, ref: str, rel: str) -> None:
    """Restore one path's content to what it was at `ref`."""
    git("checkout", ref, "--", rel, cwd=repo)


def unstage_and_remove(repo: Path, rel: str) -> None:
    """Drop a path the worker added: remove it from the index and delete it from disk."""
    git("rm", "-f", "--quiet", "--cached", "--", rel, cwd=repo, check=False)
    fp = repo / rel
    if fp.exists():
        fp.unlink()


def uncommitted_task_files(repo: Path) -> list[str]:
    """Return relative paths of task files (under tasks/) with uncommitted changes.

    Uses --untracked-files=all so a task file inside a wholly untracked directory (e.g. a
    new phase) is listed individually rather than collapsed into one `?? dir/` entry.
    """
    try:
        out = git("status", "--porcelain", "--untracked-files=all", cwd=repo)
    except GitError:
        return []
    files = []
    for line in out.splitlines():
        if len(line) < 4:
            continue
        path = line[3:].strip()
        if " -> " in path:  # rename: shows as "old -> new"
            path = path.split(" -> ")[-1]
        if "/tasks/" in path and path.endswith(".md"):
            files.append(path)
    return files


def commit_task_files(repo: Path, message: str) -> list[str]:
    """Commit task files with uncommitted changes. Returns committed paths.

    Stages only the task paths (so a brand-new untracked file is known to git), then
    commits with those same paths as a pathspec, so any unrelated change the operator
    already had staged is left staged rather than swept into the commit.
    """
    files = uncommitted_task_files(repo)
    if not files:
        return []
    for f in files:
        git("add", "--", f, cwd=repo)
    git("commit", "-q", "-m", message, "--", *files, cwd=repo)
    return files
