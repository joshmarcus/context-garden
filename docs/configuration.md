# Editable configuration metadata and project policy

`garden.configuration.CONFIG_FIELDS` is the shared inventory for configuration surfaces.
Each entry describes its value shape, default, supported scopes, units, practical meaning,
and whether it applies at runtime, on the next scheduler tick, after restart, or is derived.
Templates and API schemas should consume this inventory instead of maintaining their own
types and help text. Derived entries are display-only. A field that supports only `global`
scope must never be offered as a project override.

## Anonymous publication

Public viewing uses an exported projection rather than the live garden. Nothing is
published until a project and every permitted field are named:

```yaml
publication:
  projects:
    context-garden:
      fields:
        - project.summary
        - phase.summary
        - task.summary
        - task.dependencies
```

The summary fields expose project and phase names plus task IDs, titles, and statuses;
dependencies are limited to tasks in the same published projection. Free-form Markdown is
separate and must be added explicitly with `project.content`, `phase.content`, or
`task.content`. Raw logs, transcripts, run data, arbitrary files, configuration, owners,
links, paths, and costs are never projection fields.

Create and serve an isolated snapshot with:

```console
garden publish --output /srv/public-garden
garden serve --public-projection /srv/public-garden --no-watch --host 0.0.0.0
```

The serving process needs only the exported directory. Re-run `garden publish` after a
publication change. Replacement is atomic; pages, exports, searches, and event streams read
the replacement without retaining an application cache. Removing a project revokes it from
subsequent responses and sends the reduced snapshot to connected event subscribers.

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

## Phase execution

`phase_execution` is `concurrent` by default, preserving existing scheduling. Set it to
`sequential` globally or in a product configuration override to admit new model work only
from that product's earliest open phase:

```yaml
phase_execution: sequential
```

The existing product phase discovery order is authoritative; this setting does not define a
second order. The selected phase advances only when its goals document has been explicitly
closed after the normal closing review and blocker gates. A frozen or otherwise blocked phase
therefore remains selected and is reported as the reason later work waits.

Changes apply on the next scheduler tick. Enabling sequential execution never cancels an
active run or discards its result: collection, lease renewal, publication, and recovery keep
running, while new model admission outside the selected phase stops. Disabling it restores
concurrent admission. Reopening an earlier phase selects it for subsequent admission without
cancelling work already in flight in a later phase.
