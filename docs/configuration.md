# Editable configuration metadata and project policy

`garden.configuration.CONFIG_FIELDS` is the shared inventory for configuration surfaces.
Each entry describes its value shape, default, supported scopes, units, practical meaning,
and whether it applies at runtime, on the next scheduler tick, after restart, or is derived.
Templates and API schemas should consume this inventory instead of maintaining their own
types and help text. Derived entries are display-only. A field that supports only `global`
scope must never be offered as a project override.

## Project values and policy

Project configuration uses the existing product scope in `garden.yaml`:

```yaml
products:
  service:
    configuration:
      overrides:
        max_parallel: 2
      locks:
        max_parallel:
          reason: Protect shared build capacity
          source: platform policy
        auto_dispatch:
          reason: Release hold
          source: release policy
          value: false
```

An override supplies the project's value without changing another project. A lock prohibits
ordinary mutation. A policy `value` is enforced and wins over both the global and project
override values. A lock without an enforced value freezes the effective value the project
currently inherits. Ordinary global and runtime edits, profile selection, and scheduler
reloads are rejected when they would change that value; an explicit project override is not
required. Loading rejects unknown fields, invalid values, project use of global-only fields,
and missing reasons.

`Config.setting(key, product)` returns the effective value and its provenance, lock reason,
and policy source. `apply_changes` is the common mutation boundary for global and project
edits and resets. It validates a complete copy before returning it, preserves unknown
extension keys, and supports a content revision token for stale-write rejection. A project
reset removes the override and resumes inheritance; it never edits the lock collection.

Project policy is not part of the ordinary mutation vocabulary. It is changed only by a
trusted author editing the applicable repository-controlled configuration file under the
existing config-fence and filesystem trust model. Removing the lock there restores normal
editability. Reload validation is the final guard for such direct file changes.

Saved edits use `garden config set KEY YAML [--product NAME]`; project inheritance is
restored with `garden config reset KEY --product NAME`. Both commands validate the complete
layered configuration and replace `garden.yaml` atomically. `--revision` accepts an editor's
previously read revision and rejects a stale write. A running scheduler observes the saved
file through its normal reload and fence gate on the next tick.

Runtime audit events include the changed key, global scope, runtime provenance, and the actor
available to the current CLI/web trust model. Values whose key or metadata identifies a
secret are replaced with `<redacted>` before logging or event emission. Configuration
surfaces must represent secret references rather than return stored plaintext.
