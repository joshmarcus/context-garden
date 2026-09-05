"""Git plumbing for product repos and per-task worktrees. Deterministic, no LLM."""

from __future__ import annotations

import subprocess
from pathlib import Path

from .github import repo_slug_from_remote


class GitError(Exception):
    pass


def git(*args: str, cwd: Path | None = None, check: bool = True) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise GitError(f"git {' '.join(args)} (in {cwd}): {proc.stderr.strip() or proc.stdout.strip()}")
    return proc.stdout


def is_repo(path: Path) -> bool:
    return (path / ".git").exists()


def remote_url(repo: Path, remote: str = "origin") -> str:
    try:
        return git("remote", "get-url", remote, cwd=repo).strip()
    except GitError:
        return ""


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
    if not remote_url(repo, remote):
        return False
    try:
        git("fetch", "--prune", remote, cwd=repo)
        return True
    except GitError:
        return False


def base_ref(repo: Path, base: str) -> str:
    """Prefer origin/<base> when a remote exists, else the local branch."""
    if remote_url(repo):
        try:
            git("rev-parse", "--verify", f"origin/{base}", cwd=repo)
            return f"origin/{base}"
        except GitError:
            pass
    git("rev-parse", "--verify", base, cwd=repo)
    return base


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
    if _is_ancestor(worktree, ref, "HEAD"):
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
        remote_branch = f"origin/{branch}" if remote_url(repo) else ""
        if remote_branch:
            try:
                git("rev-parse", "--verify", remote_branch, cwd=repo)
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


def _is_ancestor(repo: Path, ref_a: str, ref_b: str) -> bool:
    """Return True if ref_a is an ancestor of ref_b (or equal)."""
    proc = subprocess.run(["git", "merge-base", "--is-ancestor", ref_a, ref_b], cwd=repo, capture_output=True)
    return proc.returncode == 0


def push(worktree: Path, branch: str, force: bool = False, base: str = "") -> str:
    """Push branch to origin. Returns a note if force-with-lease was used due to rebase detection.

    When base is given and force is False, compares origin/<branch> against HEAD:
    - fast-forward (origin/<branch> is ancestor of HEAD): plain push, no note.
    - rebased (origin/<branch> not ancestor, but origin/<base> is): --force-with-lease with
      an expectation ref so we only overwrite the sha we saw; logs "rebased branch force-pushed".
    - other divergence: let the plain push fail with git's own message.
    """
    if not remote_url(worktree):
        raise GitError("no origin remote to push to")
    args = ["push", "-u"]
    note = ""
    if force:
        args.append("--force-with-lease")
    elif base:
        try:
            origin_sha = git("rev-parse", f"origin/{branch}", cwd=worktree).strip()
            if not _is_ancestor(worktree, f"origin/{branch}", "HEAD"):
                if _is_ancestor(worktree, f"origin/{base}", "HEAD"):
                    args.append(f"--force-with-lease={branch}:{origin_sha}")
                    note = "rebased branch force-pushed"
        except GitError:
            pass  # origin/<branch> doesn't exist yet; plain push is fine
    git(*args, "origin", f"HEAD:refs/heads/{branch}", cwd=worktree)
    return note


def sync_remote_branch(worktree: Path, branch: str) -> tuple[bool, list[str]]:
    """Before a rebase round, fold in commits that exist only on `origin/<branch>`.

    A rebase round rewrites the branch in the worktree and force-pushes it. If the remote
    branch has moved on (someone merged into it) those commits are not in the worktree, so
    the force-push would discard them. Rebasing the worktree's local commits onto
    `origin/<branch>` first keeps them. Returns (ok, conflicted files); on conflict the
    rebase is aborted so the worktree is left clean and the round resolves it like any other.
    """
    fetch(worktree)
    if not remote_url(worktree):
        return True, []
    try:
        git("rev-parse", "--verify", f"origin/{branch}", cwd=worktree)
    except GitError:
        return True, []  # no remote branch yet: nothing to fold in
    if _is_ancestor(worktree, f"origin/{branch}", "HEAD"):
        return True, []  # the remote holds nothing the worktree lacks
    try:
        git("rebase", f"origin/{branch}", cwd=worktree)
        return True, []
    except GitError:
        files = [ln.strip() for ln in git("diff", "--name-only", "--diff-filter=U", cwd=worktree, check=False).splitlines() if ln.strip()]
        git("rebase", "--abort", cwd=worktree, check=False)
        return False, files


def rebase_onto_capture(worktree: Path, onto: str) -> tuple[bool, list[str], dict[str, str]]:
    """Rebase the worktree branch onto `onto` (e.g. origin/main). Returns (ok, conflicted files,
    {path: file contents}); on a textual conflict each conflicted file's contents (with conflict
    markers) are captured before aborting, so a rebase brief can carry the conflicting hunks. The
    rebase is aborted on conflict so the worktree is left clean for the agent to redo and resolve."""
    fetch(worktree)
    try:
        git("rebase", onto, cwd=worktree)
        return True, [], {}
    except GitError:
        files = [ln.strip() for ln in git("diff", "--name-only", "--diff-filter=U", cwd=worktree, check=False).splitlines() if ln.strip()]
        hunks: dict[str, str] = {}
        for f in files:
            try:
                hunks[f] = (worktree / f).read_text(errors="replace")
            except OSError:
                hunks[f] = ""
        git("rebase", "--abort", cwd=worktree, check=False)
        return False, files, hunks


def sync_and_rebase(worktree: Path, branch: str, base: str) -> tuple[bool, list[str], dict[str, str]]:
    """Bring the worktree branch onto `base`, folding in commits that live only on
    `origin/<branch>` first (`sync_remote_branch`) so a later force-push never discards them,
    then rebasing onto the base (`rebase_onto_capture`). This is the one sync-then-rebase
    sequence every mechanical-rebase path shares -- the rebase mixin, the restack, the base
    probe and the merge queue all reach it through `Scheduler._rebase_and_record`. Returns
    (ok, conflicted files, {path: contents with markers}); on a conflict at either step the
    rebase is aborted so the worktree is left clean, and the hunks (empty for a sync conflict)
    carry the textual conflict for a rebase brief."""
    ok, files = sync_remote_branch(worktree, branch)
    if not ok:
        return False, files, {}
    return rebase_onto_capture(worktree, base_ref(worktree, base))


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
    proc = subprocess.run(["git", "patch-id", "--stable"], input=diff_text, cwd=worktree,
                          capture_output=True, text=True)
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
    """`git status --porcelain` as a list of non-empty lines (worktree + untracked)."""
    try:
        out = git("status", "--porcelain", cwd=repo)
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
    """True if `rel` exists as a tracked path in `ref`'s tree."""
    proc = subprocess.run(["git", "cat-file", "-e", f"{ref}:{rel}"], cwd=repo, capture_output=True)
    return proc.returncode == 0


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
