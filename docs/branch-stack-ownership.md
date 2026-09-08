# Branch-stack ownership

Garden owns stacked branches by default. Set a product's `stack_owner` to `external` when
another stack tool controls dependency branches:

```yaml
products:
  service:
    stack_owner: external
    protected_paths: ["deployment/**", "policy/*.yaml"]
```

External ownership disables garden's dependency stacking, retargeting, restacking, and
automatic rebase/force-push recovery for that product. If a worker finishes after another
tool changes its branch head, garden records the expected and observed heads and leaves the
task waiting for a human; reconcile the branch with the owning tool, then resume or finish
the task through the ordinary human flow. Garden never treats an empty branch diff as shipped:
there must be a committed, reviewed change or independently verified external completion.

`protected_paths` adds shell-style repository-relative patterns to the built-in guarded
paths (`garden*.yaml`, `**/tasks/`, `.github/`, and `principles/`). Matching changes remain
in review and cannot automerge. These additions are additive; they cannot relax the defaults.

Only `garden` and `external` are valid `stack_owner` values, and protected paths must be a
list of non-empty patterns. Invalid combinations fail configuration loading rather than
silently choosing a history writer.
