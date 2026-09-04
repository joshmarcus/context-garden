# Harnesses and model selection

The garden is harness-independent. A **harness** is a data definition of how to run an
agent CLI headlessly: binary, argument shape, how the prompt is delivered (stdin), and how
to parse the output. Built in:

| name | command shape | output |
|---|---|---|
| `claude` | `claude -p --output-format json --max-turns N [--model M] --permission-mode ... --allowedTools ...` | one JSON result (`result`, `usage`, `total_cost_usd`) |
| `codex` | `codex exec --json --skip-git-repo-check --full-auto [-m M] --output-last-message F -` | JSONL events (`item.completed` agent messages, `turn.completed` usage) |

Any other CLI can be added under `harnesses:` with `command: [...]` (placeholders `{model}`,
`{final}`) and `output: text`; the worker contract (`GARDEN_RESULT` last line) is the same.

Selection order: task `harness:` > product `harness:` > garden `harness:`.

## Difficulty -> model

Every task has `difficulty: easy | medium | hard` (planner-assigned, human-editable). Each
harness maps tiers to models (`harnesses.<name>.models`); an explicit task `model:` wins.
The review pass uses the task's tier unless `review.difficulty` or the harness's
`review_model` says otherwise. Runs record `harness`, `model`, usage and cost, so
`garden runs` shows what each tier actually costs.
