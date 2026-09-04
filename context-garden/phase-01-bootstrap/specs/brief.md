# The worker brief

The brief is the complete prompt a worker receives. Sections, in order:

1. Head: task id, title, product, phase.
2. Operating rules: worktree/branch, no pushing, no editing `tasks/`, run checks, the
   `GARDEN_RESULT` contract. (Revise runs add a "Revision round" section.)
3. Principles digest (`principles/00-index.md`).
4. Product overview (`<product>/product.md`).
5. Phase goals (`<product>/<phase>/goals.md`).
6. The task body.
7. Reading list: each file inlined in a fenced block if under `brief.inline_max_chars`,
   otherwise listed under "read these" with its path.
8. Review feedback (revise runs only): comments since the last dispatch, as markdown.

If the whole brief exceeds `brief.total_max_chars`, the inlined reading list collapses to
a path list. `garden brief <id> --stats` reports the size of each section.

## Result contract

The worker's final message must end with one line:

```
GARDEN_RESULT: {"status": "done"|"blocked", "summary": "...", "pr_title": "...", "pr_body": "...", "notes": "..."}
```

`pr_title`/`pr_body` are used verbatim. The runner pushes and opens the PR; the worker
never does.
