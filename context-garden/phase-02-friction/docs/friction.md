# Friction log

Harvested from worker PR bodies (`## Friction` sections). Empty until the first tasks ship.

## First live run

CG-027, 2026-09-04, a fresh clone in WSL (Ubuntu 26.04, Python 3.14) on the same machine
as an earlier attempt from Windows. One bullet per step; cost at the end.

- **Setup.** `uv` is not on the WSL image; `python3 -m venv .venv` and `pip install -e
  ".[dev,plates]"` worked on Python 3.14 (95 tests pass, lint clean). `gh` is not in the
  image either and there is no passwordless sudo, so it went in as a release tarball under
  `~/.local/bin`. Neither is the garden's fault, but the README only names `uv`.
- **Doctor.** `garden doctor` said "all good" while neither `gh` nor `claude` was logged
  in. It checks that the binaries are on PATH, not that they work: the github line shows
  `gh CLI (path)` with no `as <login>` when `gh.me()` is empty, and nothing looks at the
  harness's auth at all. The brief for CG-012 is ~2,600 tokens, under the 3k target.
- **Harness when logged out.** A probe `claude -p --output-format json` exits 0 with
  `is_error: true` and `result: "Not logged in · Please run /login"`. `Harness.parse`
  turns that into `error`, so the scheduler would mark the run failed and retry, burning
  both `max_attempts` on an environment problem that no retry can fix.
- **Windows (the earlier attempt).** The local runner starts every worker through `sh -c`,
  so a harness installed as `claude.cmd` is found by `shutil.which` in doctor but not by
  the runner. The `checks.pre_pr` commands (`$GARDEN_ROOT`, quoting) assume a POSIX shell.
  Neither bites in WSL; both belong here because doctor passed on Windows too.
- **Garden state in the working tree.** `garden set-status`, `take` and `approve` each
  edit a task file in the main checkout, which sits uncommitted while the loop runs;
  worker branches are cut from `origin/main`, so those edits never ride a PR. The person
  has to commit them by hand, and it is not written down anywhere when.
- **Dispatch.** pending: the run needs `gh auth login` and `claude auth login` in WSL first.
- **Worker run.** pending.
- **Result line.** pending.
- **Push and draft PR.** pending.
- **Automated review.** pending.
- **Triage.** pending.
- **Merge and done.** pending.
- **Cost.** pending (`garden usage CG-012`).
