# Context hygiene

The garden exists so that an agent can be productive from a few thousand tokens of
context instead of re-deriving the world every session. That only works if the
context stays sharp.

## Layers, and what belongs where

| Layer | File | Read by | Should contain |
|---|---|---|---|
| Principles digest | `principles/00-index.md` | every brief, every plan | rules that change agent behaviour, under ~60 lines |
| Principles (long form) | `principles/*.md` | humans; tasks that cite them | the reasoning behind the digest |
| Product overview | `<product>/product.md` | every brief for that product | what it is, how to run/test it, conventions |
| Phase goals | `<product>/<phase>/goals.md` | every brief in that phase, the planner | why now, goals, non-goals, definition of done |
| Specs | `<product>/<phase>/specs/*.md` | the planner; tasks that list them | designs, formats, interfaces |
| Tasks | `<product>/<phase>/tasks/*.md` | the assigned worker | goal, context, acceptance criteria, reading list |

## Rules of thumb

- The digest, product overview and phase goals are inlined in *every* brief. Their combined
  size is the fixed cost of every task. Keep them dense.
- A spec goes on a task's reading list only if the worker needs it to make the change.
- When a worker reports friction ("I had to guess X"), fix the spec or the digest, not the task.
- Finished phases are history. Do not delete them, but do not keep linking to them either;
  move anything still relevant into the product overview.
