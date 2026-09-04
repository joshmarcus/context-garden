# phase-01-bootstrap goals

## Why this phase

There was no tool. This phase builds the minimum loop that lets a human specify work as
context files and have agents execute it without an agent acting as scheduler.

## Goals

1. A file layout for principles, products, phases, specs and tasks that is readable by
   humans and cheap to brief agents from.
2. A CLI that plans a phase into task files, computes the ready set from the dependency
   graph, dispatches headless `claude -p` workers in git worktrees, opens PRs, and reacts
   to review feedback, all without an LLM in the scheduler.
3. A skill so a human-driven Claude Code session can take a task from the same queue.
4. A local web UI and a TUI for the board, the graph, and run/cost history.
5. The tool is its own first product, with a follow-on phase seeded with draft tasks.

## Non-goals

- Multi-user or hosted operation.
- Automatic merging.
- Any runner other than local `claude -p` and a human-driven session.

## Definition of done

- `garden doctor`, `garden status`, `garden graph`, `garden brief` work on this repo.
- The scheduler's transitions are covered by tests using a fake `claude` binary and a
  local bare git remote.
- README documents the loop end to end.
