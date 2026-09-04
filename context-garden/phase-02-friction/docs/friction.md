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
- **Git identity.** The WSL profile had no `user.name`/`user.email`, so the first commit
  in the CG-027 worktree failed with "Author identity unknown". A headless worker would
  fail the same way at its first commit, and so would the scheduler's leftover commit.
  Doctor does not check it. Set repo-locally from the repo's own history.
- **Garden state in the working tree.** `garden set-status`, `take` and `approve` each
  edit a task file in the main checkout, which sits uncommitted while the loop runs;
  worker branches are cut from `origin/main`, so those edits never ride a PR. The person
  has to commit them by hand, and it is not written down anywhere when.
- **Dispatch.** After both logins, `garden serve` dispatched CG-012 on its first tick within
  a few seconds: runner local, harness claude, model `haiku` (the easy tier), brief 2,636
  tokens, worktree cut from `origin/main`. The Inbox answered at once. Nothing to note,
  except that the doctor line for github now shows `as joshmarcus`, which is the only visible
  difference between logged in and not.
- **Worker run, attempt 1.** haiku ran 4.4 minutes, 61 turns, $0.58, and hit
  `max_turns: 60`. Exit code 1, `subtype: error_max_turns`, and the final text is empty,
  so there was no result line at all. It had made three real commits in the worktree
  (usage column, phase header, a refactor). The turn cap is the harness default for every
  tier; an easy task on the cheapest model burned through it on exploration.
- **Nothing shows a running worker's progress.** `stdout.json` stayed at zero bytes for the
  whole run. "Running now · 2 min" on the Inbox was the only signal; a hung worker would
  look the same until the 95-minute kill. The only checks on a worker happen inside a
  tick (exit_code file, then a pid probe, then the timeout); nothing else reaches out.
- **The first failure is invisible.** The tick that reaped attempt 1 dispatched attempt 2
  in the same second, with the task back through `ready`. The Inbox lists CG-012 nowhere,
  and `garden digest` printed "nothing notable" next to "$0.58 spent". A person watching
  the Inbox learns about a failed attempt only when the second one fails too.
- **Attempt 2 starts on attempt 1's commits and is not told.** The worktree is reused, so
  the three commits are on the branch, but the brief differs from the first one only by
  the two new log lines, and the dispatch note says "fresh session, base main". The worker
  has to discover the half-finished work by itself.
- **`in` tokens mislead.** `garden runs` shows 481 input tokens for a run whose cache reads
  were 3.4M; the column should show the total context read, or cache reads next to it.
- **Worker run, attempt 2.** Started on attempt 1's commits, found the work mostly done,
  added nothing, and reported `done` after 29 turns, 94 seconds and $0.28. Its final message
  claimed "No lint/syntax errors".
- **Result line.** Present and well formed on attempt 2 and on the revise run; `pr_title`
  and `pr_body` were usable as written. Neither body carried the `## Friction` section the
  principles digest asks for, so there was nothing for CG-008 to harvest.
- **Pre-PR checks earned their keep.** Tests passed; ruff failed on one unsorted import the
  worker had said was clean. No PR was opened; the task went to `changes_requested` and a
  revise run started in the same tick with ruff's output in its brief. The fix took 23 turns,
  76 seconds and $0.17 for a two-line import swap, which is what a revise round costs on
  haiku even when the feedback is exact.
- **The revise brief lied about the PR.** Its "Revision round" section opened with "This
  branch already has an open pull request: (unknown). Reviewers left feedback", when no PR
  existed and the feedback was a lint check. The wording needs a pre-PR variant.
- **Push and draft PR.** After the revise run, both checks passed and the draft PR opened two
  seconds later: title `CG-012: Brief cost report per phase`, body with Summary, Changes,
  Verification, Review responses and the garden's footer. Readable, if boastful. The log line
  reuses the worker's summary, so the task log now says "Addressed lint error by fixing
  import sorting. All acceptance criteria met" as the description of the PR.
- **Automated review.** Ran on haiku (`review.difficulty` empty means the task's tier),
  one turn, 86 seconds, $0.07, verdict `approve` with no findings and `description_ok`.
  It missed a real bug (the phase header passes the whole first-task brief, 2,935 tokens,
  to the template while the new column uses the fixed part, 1,781) and did not notice the
  missing `## Friction` section, which the description check exists to catch. A one-turn
  review is a read of the diff and a verdict; it never ran anything. The comment on the PR
  is posted under the person's own login, `joshmarcus`, with a tick emoji, so on GitHub the
  human appears to have approved their own PR.
- **Triage.** The Inbox card had the two forms it should: "Ready for review" and a
  "Send back" note. Sending back through the form worked (303 to the task page), the note
  became `pending_feedback` word for word, and the task went to `changes_requested`. The
  log line truncates the note; the full text is only in `state.json` and the next brief.
- **Revise round from triage.** The tick after the send-back dispatched a revise run
  (haiku, 2 minutes, $0.18). It made the one-line fix, added the `## Friction` section and
  answered the note under "Review responses" as the revise template tells it to. Checks
  passed, the push updated the PR body and left a comment. The person marked the PR ready
  on GitHub by hand and the next poll moved the task to `in_review` within a second, which
  is the right way round: marking ready stays a human step.
- **The garden argued with itself.** Review round two (haiku, $0.07) requested changes with
  one blocking finding: the "Review responses" section is scar tissue. The revise template
  in `brief.py` says "Reply to each review point in `pr_body` under a Review responses
  heading"; the review prompt in `review.py` says the description must have "no references
  to earlier review rounds". Every revised PR will fail its next review on this until one
  of the two changes. A third revise run started at once to delete the section, so a
  contradiction between two prompts costs a worker run and a review run per PR.
- **The stall detector fired on a correct run.** The third revise run (haiku, $0.10)
  deleted the "Review responses" section, which is all the review asked for, and touched no
  code. `finalize` compared the diff hash with the last round's, saw no change, and stalled
  the task: `changes_requested` with `needs_human`, "garden retry to resume". So the one
  time the loop did exactly what was asked, it reported itself stuck; and `garden retry`
  would start a fresh work run, not resume. A revise round whose feedback was only about
  the description should be judged on the description, not the diff. Meanwhile the PR on
  GitHub is mergeable, CI is green, and a merge is still noticed from `changes_requested`
  because the poll checks for merged before it returns early.
- **Merge and done.** The person merged on GitHub at 17:02; the next poll saw it, moved the
  task from `changes_requested` straight to `done`, removed the worktree and cleared the
  stall flag with it. So the false stall cost nothing but confusion. Approve to merge took
  24 minutes of wall clock and needed two human touches (the send-back and the merge).
- **Cost.** `garden usage CG-012`: 7 runs, 17.1 minutes, $1.46. Work $0.86 (attempt 1
  $0.58 wasted on the turn cap, attempt 2 $0.28), revise $0.46 over three rounds (lint,
  triage note, scar tissue), review $0.14 over two rounds. Fresh input tokens 1,207; cache
  reads 7.3M; output 61k. Everything ran on haiku. The lint round and the scar-tissue round,
  $0.27 plus a review, were the garden's own doing: the worker's false claim slipped past
  it, and the two prompts disagree.
