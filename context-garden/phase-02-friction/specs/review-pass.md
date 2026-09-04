# Automated review pass

When a task reaches `in_review`, optionally run one headless `claude -p` review of the PR
diff *before* a human looks. The reviewer gets: the task brief (no operating rules), the
diff, and a strict instruction to output findings as JSON. The scheduler posts the
findings as one PR review comment and, if any finding is `blocking`, transitions the task
to `changes_requested` so the normal revise path handles it.

Config: `review.enabled` (default false), `review.max_rounds` (default 1),
`review.model`. Cost is one call per PR round; the diff is capped at `review.max_diff_chars`.

Findings schema: `[{"severity": "blocking"|"nit", "file": "...", "line": 123, "summary": "..."}]`.
The reviewer's login is the same as the runner's, so its own comments must be excluded
from feedback detection (the garden already excludes its own login).
