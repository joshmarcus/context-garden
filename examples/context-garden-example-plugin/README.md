# Context Garden example plugin

This directory is an independent Python distribution that demonstrates the supported
`garden.plugins` API without relying on Context Garden internals. It is intentionally generic:
the package provides one Python check, one check using the versioned JSON Lines command protocol,
a doctor check, a public-safe context resource, and a data-only initialization profile.

## Build and install locally

Build the core and plugin into one local artifact directory, then install those artifacts without
contacting a package index:

```console
python -m pip wheel --no-deps --no-build-isolation --wheel-dir dist .
python -m pip wheel --no-deps --no-build-isolation --wheel-dir dist \
  examples/context-garden-example-plugin
python -m pip install --no-index --no-deps --find-links dist \
  context-garden context-garden-example-plugin
```

The distribution advertises `example-tools = "context_garden_example:MANIFEST"` in the
`garden.plugins` entry-point group. Installation only makes its metadata discoverable; it
does not import or enable the plugin.

## Enable and configure it

Activation is an explicit, versioned choice in `garden.yaml`:

```yaml
plugins:
  example-tools:
    distribution: context-garden-example-plugin
    version: 0.1.0
    plugin_config:
      label: sample
      credentials:
        token: ${EXAMPLE_TOOLS_TOKEN}
```

The manifest schema rejects missing, mistyped, and unknown settings. It declares
`credentials.token` for redaction, so core output replaces the configured value before it reaches
diagnostics or durable records. After reviewing an installation or configuration change, run
`garden plugins lock`. The generated `garden.lock` pins the core version, distribution version,
installed-file fingerprint, configuration digest, and resource digests; it never stores the token.

## Capabilities and resources

`context_garden_example` imports only names exported by `garden.plugins`. Its `python-check`
returns `ProviderCheckResult` directly. Its `command-check` starts a child through
`JsonLinesCommand`, performs the `garden.plugin-command/v1` handshake, and returns the same typed
result. Core supplies timeouts, cancellation, idempotency keys, redaction, and provenance.

The `starter-context` resource declares its package path, exact digest, size bound, audience,
product, media type, version, and `public_safe: true`. The `starter-profile` resource is JSON data
that creates only small Markdown context files; it contains no executable hook. Enable the plugin,
refresh the lock, and preview/apply it with:

```console
garden init --profile example-tools/starter-profile
```

## Compatibility and rollback

| Core version | Plugin version | Plugin API | Supported |
| --- | --- | --- | --- |
| `>=0.4,<0.5` | `0.1.x` | `garden.plugins/v1` | yes |
| any other core release | `0.1.x` | `garden.plugins/v1` | no |

Keep the wheel set and its reviewed `garden.lock` together. To roll back, reinstall the previous
matching core and plugin wheels with `--no-index --no-deps`, then restore the lock from the same
revision. Read-only status and doctor commands remain available during drift; mutating operations
remain on compatibility hold until all three match.
